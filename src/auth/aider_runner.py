"""
Agent Mesh v0.6.5 — Agent Runners (Aider + Claude)

Heartbeat Timeout:
- 讀 stdout/stderr，每收到新輸出就重置 idle 計時器
- idle_timeout: 連續 N 秒沒輸出 → 判定卡住 → kill
- max_timeout: 絕對上限（安全網）
- 好處：agent 在思考/寫 code 時不會被誤殺
"""

from __future__ import annotations
import asyncio
import logging
import os
import signal
import json as _json
import tempfile
import time
from typing import Optional

logger = logging.getLogger(__name__)


def _extract_stream_json_result(raw_stdout: str) -> str:
    """
    Extract final text result from claude stream-json output.

    stream-json emits one JSON object per line. We look for 'result' type
    messages which contain the final text output, and also collect 'assistant'
    type messages as fallback.
    """
    result_text = ""
    assistant_parts = []

    for line in raw_stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = _json.loads(line)
        except (ValueError, _json.JSONDecodeError):
            continue

        msg_type = obj.get("type", "")

        # Final result message
        if msg_type == "result":
            result_text = obj.get("result", "")
            break

        # Collect assistant content blocks as fallback
        if msg_type == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    assistant_parts.append(block.get("text", ""))

        # Content block delta (streaming text chunks)
        if msg_type == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                assistant_parts.append(delta.get("text", ""))

    if result_text:
        return result_text
    if assistant_parts:
        return "".join(assistant_parts)
    # Fallback: return raw (might be plain text if format wasn't stream-json)
    return raw_stdout[:5000]


class RunResult:
    """Agent 執行結果。"""
    def __init__(self, success: bool, stdout: str = "", stderr: str = "",
                 error: Optional[str] = None, cost=None):
        self.success = success
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.cost = cost  # CostResult | None — set by dispatcher after execution


# ══════════════════════════════════════════════════════════
# Heartbeat Process Monitor
# ══════════════════════════════════════════════════════════

async def heartbeat_wait(
    proc: asyncio.subprocess.Process,
    idle_timeout: int = 120,
    max_timeout: int = 1200,
    label: str = "agent",
    max_stdout: int = 8000,
    max_stderr: int = 3000,
    stdout_log_path: str | None = None,
    workspace_dir: str | None = None,
) -> tuple[str, str, bool, Optional[str]]:
    """
    等待 process 完成，用 heartbeat 機制偵測是否卡住。

    每收到 stdout/stderr 新行就重置 idle 計時器。
    連續 idle_timeout 秒沒輸出 → 判定卡住。
    超過 max_timeout → 強制結束（安全網）。

    stdout_log_path: 如果指定，即時寫入 stdout 到檔案（供未來分析）。
    workspace_dir: 如果指定，監控 worktree 檔案變動作為額外 heartbeat。

    Returns: (stdout, stderr, timed_out, timeout_reason)
    """
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_len = 0
    stderr_len = 0
    last_activity = time.time()
    start_time = time.time()
    finished = asyncio.Event()

    # Open log file for real-time stdout recording
    _log_file = None
    if stdout_log_path:
        try:
            os.makedirs(os.path.dirname(stdout_log_path), exist_ok=True)
            _log_file = open(stdout_log_path, "w")
        except Exception as e:
            logger.warning(f"[Heartbeat] Cannot open log file {stdout_log_path}: {e}")

    async def _read_stream(stream, chunks: list, max_len: int, is_stdout: bool):
        nonlocal last_activity, stdout_len, stderr_len
        try:
            while True:
                try:
                    line = await asyncio.wait_for(stream.readline(), timeout=5.0)
                except asyncio.TimeoutError:
                    if finished.is_set():
                        break
                    continue
                if not line:
                    break
                last_activity = time.time()
                decoded = line.decode(errors="replace")
                # Real-time log to file
                if is_stdout and _log_file:
                    try:
                        _log_file.write(decoded)
                        _log_file.flush()
                    except Exception:
                        pass
                current_len = stdout_len if is_stdout else stderr_len
                if current_len < max_len:
                    chunks.append(decoded)
                    if is_stdout:
                        stdout_len += len(decoded)
                    else:
                        stderr_len += len(decoded)
        except Exception:
            pass

    def _check_workspace_activity() -> float:
        """Check most recent file mtime in workspace as secondary heartbeat."""
        if not workspace_dir:
            return 0.0
        try:
            latest = 0.0
            for root, _dirs, files in os.walk(workspace_dir):
                # Skip .git and __pycache__
                if "/.git" in root or "__pycache__" in root:
                    continue
                for f in files:
                    if f.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".sql", ".md")):
                        try:
                            mt = os.path.getmtime(os.path.join(root, f))
                            if mt > latest:
                                latest = mt
                        except OSError:
                            pass
            return latest
        except Exception:
            return 0.0

    def _is_process_alive() -> bool:
        """Check if process is actually alive via OS."""
        try:
            os.kill(proc.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True  # alive but can't signal

    async def _monitor() -> Optional[str]:
        """監控 idle timeout 和 max timeout。"""
        nonlocal last_activity
        check_count = 0
        while not finished.is_set():
            await asyncio.sleep(5)
            check_count += 1
            elapsed = time.time() - start_time
            idle = time.time() - last_activity

            # Every 30s: check workspace file activity as secondary heartbeat
            if workspace_dir and check_count % 6 == 0 and idle > 30:
                latest_mtime = _check_workspace_activity()
                if latest_mtime > last_activity:
                    logger.info(
                        f"[Heartbeat] {label}: file activity detected "
                        f"(idle was {idle:.0f}s), resetting idle timer"
                    )
                    last_activity = latest_mtime
                    idle = time.time() - last_activity

            # Every 30s: check if process is actually dead (proc.wait() stuck bug)
            if check_count % 6 == 0 and not _is_process_alive():
                logger.warning(
                    f"[Heartbeat] {label}: process {proc.pid} is dead "
                    f"but wait() stuck — forcing completion"
                )
                # Close pipes to unblock readers and proc.wait()
                for pipe in [proc.stdout, proc.stderr]:
                    if pipe:
                        try:
                            pipe.feed_eof()
                        except Exception:
                            pass
                return None  # Not a timeout — treat as normal exit

            # Log activity every 60s (12 checks × 5s)
            if check_count % 12 == 0:
                logger.debug(
                    f"[Heartbeat] {label}: elapsed={elapsed:.0f}s "
                    f"idle={idle:.0f}s stdout={stdout_len}B"
                )

            if idle > idle_timeout:
                logger.warning(
                    f"[Heartbeat] {label}: no output for {idle:.0f}s "
                    f"(idle_timeout={idle_timeout}s) — killing"
                )
                return "idle"
            if elapsed > max_timeout:
                logger.warning(
                    f"[Heartbeat] {label}: max_timeout {max_timeout}s reached — killing"
                )
                return "max"
        return None

    # Launch readers + monitor
    read_out = asyncio.create_task(
        _read_stream(proc.stdout, stdout_chunks, max_stdout, True)
    )
    read_err = asyncio.create_task(
        _read_stream(proc.stderr, stderr_chunks, max_stderr, False)
    )
    monitor_task = asyncio.create_task(_monitor())
    wait_task = asyncio.create_task(proc.wait())

    # Race: process exit vs monitor timeout
    done, pending = await asyncio.wait(
        [wait_task, monitor_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    timeout_reason = None
    for task in done:
        result = task.result()
        if isinstance(result, str):  # monitor returned "idle" or "max"
            timeout_reason = result

    if timeout_reason:
        # Monitor won → kill process tree + close pipes
        try:
            # Kill entire process group (aider may spawn children)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except Exception:
                    pass
            # Close pipes explicitly to unblock readline()
            for pipe in [proc.stdout, proc.stderr]:
                if pipe:
                    try:
                        pipe.feed_eof()
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                pass
        except Exception:
            pass

    # Signal readers to stop, give them a moment to flush
    finished.set()
    await asyncio.sleep(0.5)

    # Cancel all pending tasks (with timeout to prevent hang)
    for task in list(pending) + [read_out, read_err, monitor_task]:
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

    # Close log file
    if _log_file:
        try:
            _log_file.close()
            logger.debug(f"[Heartbeat] {label}: saved stdout to {stdout_log_path}")
        except Exception:
            pass

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    timed_out = timeout_reason is not None

    elapsed = time.time() - start_time
    if not timed_out:
        logger.debug(f"[Heartbeat] {label}: completed in {elapsed:.1f}s")

    return stdout, stderr, timed_out, timeout_reason


# ══════════════════════════════════════════════════════════
# DeepSeek via Aider
# ══════════════════════════════════════════════════════════

class AiderRunner:
    """
    Generic aider CLI runner — supports DeepSeek, Grok, and any aider-compatible model.
    Model can be overridden per-call via kwargs['model'].
    API key auto-detected from model prefix (xai/ → XAI_API_KEY, deepseek/ → DEEPSEEK_API_KEY).
    """

    # Model prefix → env var mapping (auto-inject correct API key)
    MODEL_ENV_MAP = {
        "xai/": "XAI_API_KEY",
        "deepseek/": "DEEPSEEK_API_KEY",
        "openrouter/": "OPENROUTER_API_KEY",
    }

    def __init__(self, config: dict):
        ds_cfg = config.get("agents", {}).get("deepseek_aider", {})
        grok_cfg = config.get("agents", {}).get("grok_aider", {})
        hb_cfg = config.get("heartbeat", {})

        self.idle_timeout = hb_cfg.get("idle_timeout", 120)

        # DeepSeek defaults
        self.model_reasoner = ds_cfg.get("model_reasoner", "deepseek/deepseek-reasoner")
        self.model_chat = ds_cfg.get("model_chat", "deepseek/deepseek-chat")
        self.timeout_reasoner = ds_cfg.get("timeout_reasoner", 600)
        self.timeout_chat = ds_cfg.get("timeout_chat", 300)

        # Grok defaults
        self.model_grok = grok_cfg.get("model", "xai/grok-code-fast-1")
        self.timeout_grok = grok_cfg.get("timeout", 600)

    def _resolve_api_key(self, model: str) -> tuple[str | None, str]:
        """Find the right API key env var for a given model prefix."""
        for prefix, env_var in self.MODEL_ENV_MAP.items():
            if model.startswith(prefix):
                return os.environ.get(env_var), env_var
        return None, f"(no MODEL_ENV_MAP entry for '{model}')"

    def _resolve_timeout(self, model: str, use_chat: bool) -> int:
        """Get the right timeout for a given model."""
        if model.startswith("xai/"):
            return self.timeout_grok
        if use_chat:
            return self.timeout_chat
        return self.timeout_reasoner

    async def execute(
        self,
        prompt: str,
        workspace_dir: str,
        target_files: list[str] | None = None,
        use_chat: bool = False,
        **kwargs,
    ) -> RunResult:
        # ★ Model override from kwargs (used by escalation chain / router)
        model = kwargs.pop("model", None)
        if not model:
            model = self.model_chat if use_chat else self.model_reasoner

        # ★ Auto-detect API key from model prefix
        api_key, key_env = self._resolve_api_key(model)
        if not api_key:
            return RunResult(
                success=False,
                error=f"Environment variable {key_env} is not set (needed for {model})"
            )

        max_timeout = self._resolve_timeout(model, use_chat)
        model_short = model.split("/")[-1]

        # ★ Retry with extended timeout
        timeout_multiplier = kwargs.pop("timeout_multiplier", 1)
        force_timeout = kwargs.pop("force_timeout_seconds", 0)
        if force_timeout > 0:
            max_timeout = force_timeout
        else:
            max_timeout = int(max_timeout * timeout_multiplier)

        env = {**os.environ}
        # Set all possible API key env vars so aider/litellm can find the right one
        env[key_env] = api_key
        env["BROWSER"] = ""

        cmd = [
            "aider",
            "--model", model,
            "--yes-always",
            "--no-auto-commits",
            "--no-auto-lint",
            "--no-suggest-shell-commands",
            "--no-show-model-warnings",
            "--message", prompt,
        ]

        # ★ Pass CLAUDE.md as read-only context (so Grok/DeepSeek get codebase guide too)
        claude_md = os.path.join(workspace_dir, "CLAUDE.md")
        if os.path.isfile(claude_md):
            cmd.extend(["--read", claude_md])
            logger.info(f"[AiderRunner] Added CLAUDE.md as read-only context")

        if target_files:
            # ★ Expand directories to actual files (aider needs specific file paths)
            expanded = []
            for f in target_files:
                full_path = os.path.join(workspace_dir, f)
                if os.path.isdir(full_path):
                    # Find .ts/.tsx/.js/.json/.prisma/.sol files in directory
                    for root, _, files in os.walk(full_path):
                        # Skip node_modules and hidden dirs
                        if "node_modules" in root or "/." in root:
                            continue
                        for fname in files:
                            if fname.endswith(('.ts', '.tsx', '.js', '.json', '.prisma', '.sol')):
                                rel = os.path.relpath(os.path.join(root, fname), workspace_dir)
                                expanded.append(rel)
                elif os.path.isfile(full_path):
                    expanded.append(f)
                # else: skip non-existent paths
            target_files = expanded[:20]  # cap at 20 files to avoid token overflow
            if not target_files:
                logger.warning(f"[AiderRunner] No files found after expanding directories")
            cmd.extend(target_files)

        # Log file for stdout analysis
        task_id = kwargs.get("task_id", "unknown")
        log_dir = os.path.join(os.path.dirname(workspace_dir), "logs")
        timestamp = int(time.time())
        stdout_log = os.path.join(log_dir, f"{task_id}_{model_short}_{timestamp}.log")

        logger.info(
            f"[AiderRunner] {model_short} in {workspace_dir}, "
            f"files={target_files or 'auto'}, "
            f"idle={self.idle_timeout}s, max={max_timeout}s"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=workspace_dir, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # ★ allow killpg to kill child processes
            )

            stdout, stderr, timed_out, reason = await heartbeat_wait(
                proc,
                idle_timeout=self.idle_timeout,
                max_timeout=max_timeout,
                label=f"aider/{model_short}",
                stdout_log_path=stdout_log,
                workspace_dir=workspace_dir,
            )

            if timed_out:
                return RunResult(
                    success=False, stdout=stdout[:5000], stderr=stderr[:2000],
                    error=f"Heartbeat timeout ({reason}): "
                          f"idle>{self.idle_timeout}s or max>{max_timeout}s"
                )

            stdout = stdout[:5000]
            stderr = stderr[:2000]
            success = proc.returncode == 0

            if success:
                logger.info(f"[AiderRunner] ✅ {model_short} output: {len(stdout)} chars")
            else:
                error_msg = self._parse_error(stdout, stderr, proc.returncode)
                logger.warning(f"[AiderRunner] ❌ {error_msg}")
                return RunResult(success=False, stdout=stdout, stderr=stderr, error=error_msg)

            return RunResult(success=True, stdout=stdout, stderr=stderr)

        except FileNotFoundError:
            return RunResult(success=False, error="aider not found. Install: pip install aider-chat")
        except Exception as e:
            return RunResult(success=False, error=f"{type(e).__name__}: {e}")

    @staticmethod
    def _parse_error(stdout: str, stderr: str, code: int) -> str:
        combined = stdout + stderr
        if "API key" in combined or "Unauthorized" in combined:
            return "DeepSeek API key invalid or expired"
        if "rate limit" in combined.lower() or "429" in combined:
            return "DeepSeek API rate limit hit"
        if "yes:" in combined and "yes-always" in combined:
            return "aider config: change 'yes:' to 'yes-always:' in ~/.aider.conf.yml"
        return f"Exit code {code}: {stderr[:300]}"


# ══════════════════════════════════════════════════════════
# Claude Code CLI
# ══════════════════════════════════════════════════════════

class ClaudeRunner:
    """
    Claude Code CLI runner.
    v1.4: plain text output (stream-json pipe breaks at ~160KB).
    Heartbeat uses workspace file activity as secondary signal.
    """

    IDLE_TIMEOUT = 600

    def __init__(self, config: dict):
        claude_cfg = config.get("agents", {}).get("claude_code", {})
        hb_cfg = config.get("heartbeat", {})
        self.idle_timeout = hb_cfg.get("idle_timeout", 600)  # v1.3: 600s with stream-json
        self.idle_timeout_opus = claude_cfg.get("idle_timeout_opus", 1200)  # v1.3: 1200s
        self.model_opus = claude_cfg.get("model_opus", "claude-opus-4-6")
        self.model_sonnet = claude_cfg.get("model_sonnet", "claude-sonnet-4-6")
        self.timeout_opus = claude_cfg.get("timeout_opus", 1200)
        self.timeout_sonnet = claude_cfg.get("timeout_sonnet", 600)

    async def execute(
        self,
        prompt: str,
        workspace_dir: str,
        target_files: list[str] | None = None,
        model: str | None = None,
        **kwargs,
    ) -> RunResult:
        if model and "opus" in model:
            use_model = self.model_opus
            max_timeout = self.timeout_opus
            idle_timeout = self.idle_timeout_opus  # ★ Opus 需要更長思考時間
        elif model and "sonnet" in model:
            use_model = self.model_sonnet
            max_timeout = self.timeout_sonnet
            idle_timeout = self.idle_timeout
        else:
            use_model = self.model_sonnet
            max_timeout = self.timeout_sonnet
            idle_timeout = self.idle_timeout

        # ★ Retry with extended timeout (e.g. opus retry 2×)
        timeout_multiplier = kwargs.pop("timeout_multiplier", 1)
        force_timeout = kwargs.pop("force_timeout_seconds", 0)
        if force_timeout > 0:
            max_timeout = force_timeout
            idle_timeout = force_timeout  # user controls everything
        else:
            max_timeout = int(max_timeout * timeout_multiplier)
            idle_timeout = int(idle_timeout * timeout_multiplier)

        model_short = "opus" if "opus" in use_model else "sonnet"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        # v1.4: plain text output (stream-json pipe breaks at ~160KB)
        # heartbeat uses workspace file activity as secondary signal
        cmd = (
            f"cat {prompt_file} | claude -p --verbose "
            f"--model {use_model} "
            f"--dangerously-skip-permissions"
        )

        # Log file for stdout analysis
        task_id = kwargs.get("task_id", "unknown")
        log_dir = os.path.join(os.path.dirname(workspace_dir), "logs")
        timestamp = int(time.time())
        stdout_log = os.path.join(log_dir, f"{task_id}_{model_short}_{timestamp}.jsonl")

        logger.info(
            f"[ClaudeRunner] {model_short} in {workspace_dir}, "
            f"idle={idle_timeout}s, max={max_timeout}s"
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=workspace_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # ★ allow killpg to kill child processes
            )

            stdout, stderr, timed_out, reason = await heartbeat_wait(
                proc,
                idle_timeout=idle_timeout,
                max_timeout=max_timeout,
                label=f"claude/{model_short}",
                max_stdout=10_000_000,
                stdout_log_path=stdout_log,
                workspace_dir=workspace_dir,
            )

            if timed_out:
                return RunResult(
                    success=False, stdout=stdout[:5000], stderr=stderr[:2000],
                    error=f"Heartbeat timeout ({reason}): "
                          f"idle>{idle_timeout}s or max>{max_timeout}s"
                )

            stdout_text = stdout
            stderr = stderr[:2000]
            success = proc.returncode == 0

            if success:
                logger.info(f"[ClaudeRunner] ✅ {model_short} output: {len(stdout_text)} chars")
            else:
                logger.warning(f"[ClaudeRunner] ❌ Exit {proc.returncode}: {stderr[:300]}")

            return RunResult(
                success=success, stdout=stdout_text[:5000], stderr=stderr,
                error=None if success else f"Exit {proc.returncode}: {stderr[:500]}",
            )
        finally:
            os.unlink(prompt_file)
