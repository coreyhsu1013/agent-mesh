"""
Agent Mesh v1.0 — Deployer

Deploys project to target host via rsync + SSH.
Runs migration, build, pm2 restart, and health check.

Usage:
  - Standalone: --deploy --repo ~/afatech-erp
  - After Design Pipeline: --evolve ... --deploy
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent-mesh")


@dataclass
class DeployResult:
    """Result of a deployment attempt."""
    success: bool = False
    steps: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "steps": self.steps,
            "duration_s": self.duration_s,
            "error": self.error,
        }

    def summary(self) -> str:
        status = "✅ DEPLOYED" if self.success else f"❌ FAILED: {self.error}"
        parts = [f"[Deploy] {status} ({self.duration_s:.0f}s)"]
        for step in self.steps:
            icon = "✅" if step.get("ok") else "❌"
            parts.append(f"  {icon} {step['name']}: {step.get('detail', '')}")
        return "\n".join(parts)


class Deployer:
    """Deploys project to target host."""

    def __init__(self, config: dict, repo_dir: str):
        self.config = config
        self.repo_dir = repo_dir
        deploy_cfg = config.get("deploy", {})
        self.host = deploy_cfg.get("host", "")
        self.user = deploy_cfg.get("user", "")
        self.password = deploy_cfg.get("password", "")
        self.remote_dir = deploy_cfg.get("remote_dir", "")
        self.port = deploy_cfg.get("port", 22)
        self.exclude = deploy_cfg.get("exclude", [
            ".git", ".agent-mesh", "node_modules", "__pycache__",
            ".env", "*.pyc", "uploads", ".next",
        ])
        self.pre_deploy_cmd = deploy_cfg.get("pre_deploy_cmd", "")
        self.post_deploy_cmd = deploy_cfg.get("post_deploy_cmd", "")
        self.health_check_url = deploy_cfg.get("health_check_url", "")
        self.backup_before_deploy = deploy_cfg.get("backup", True)
        self.model_chain = deploy_cfg.get("model_chain", [
            "xai/grok-code-fast-1",
            "deepseek/deepseek-reasoner",
            "claude-sonnet-4-6",
        ])
        self.max_retries = deploy_cfg.get("max_retries", 10)

    async def deploy(self) -> DeployResult:
        """
        Full deployment flow:
        1. Pre-check (host, remote_dir configured)
        2. Backup remote (optional)
        3. Rsync files to remote
        4. Run deploy script on remote (pip install, migration, npm build, pm2)
        5. Health check
        """
        t0 = time.time()
        result = DeployResult()

        if not self.host or not self.user or not self.remote_dir:
            result.error = (
                "Deploy not configured. Set deploy.host, deploy.user, "
                "deploy.remote_dir in config.yaml"
            )
            logger.error(f"[Deployer] {result.error}")
            return result

        logger.info(f"\n{'='*60}")
        logger.info(f"  🚀 Deploying to {self.user}@{self.host}:{self.remote_dir}")
        logger.info(f"{'='*60}")

        ssh_prefix = self._ssh_prefix()

        # Step 1: Backup
        if self.backup_before_deploy:
            step = await self._run_step(
                "backup",
                f'{ssh_prefix} "cd {self.remote_dir} && '
                f'tar czf ../afatech-backup-$(date +%Y%m%d-%H%M%S).tar.gz '
                f'--exclude=node_modules --exclude=.next --exclude=uploads '
                f'--exclude=__pycache__ . 2>/dev/null || true"',
            )
            result.steps.append(step)
            if not step["ok"]:
                logger.warning("[Deployer] Backup failed, continuing anyway")

        # Step 2: Pre-deploy command (e.g., build frontend locally)
        if self.pre_deploy_cmd:
            step = await self._run_step_with_retry(
                "pre-deploy", self.pre_deploy_cmd, result,
                cwd=self.repo_dir, is_remote=False,
            )
            if not step["ok"]:
                result.error = f"Pre-deploy failed: {step.get('detail', '')}"
                result.duration_s = time.time() - t0
                return result

        # Step 3: Rsync
        exclude_args = " ".join(f'--exclude="{e}"' for e in self.exclude)
        rsync_cmd = (
            f'rsync -avz --delete {exclude_args} '
            f'-e "sshpass -p \'{self.password}\' ssh -p {self.port} -o StrictHostKeyChecking=no" '
            f'{self.repo_dir}/ {self.user}@{self.host}:{self.remote_dir}/'
        )
        step = await self._run_step_with_retry(
            "rsync", rsync_cmd, result, timeout=300, is_remote=False,
        )
        if not step["ok"]:
            result.error = f"Rsync failed: {step.get('detail', '')}"
            result.duration_s = time.time() - t0
            return result

        # Step 4: Remote deploy script
        deploy_script = (
            f'cd {self.remote_dir} && '
            f'bash scripts/deploy.sh'
        )
        step = await self._run_step_with_retry(
            "remote-deploy",
            f'{ssh_prefix} "{deploy_script}"',
            result, timeout=600, is_remote=True,
        )
        if not step["ok"]:
            result.error = f"Remote deploy failed: {step.get('detail', '')}"
            result.duration_s = time.time() - t0
            return result

        # Step 5: Post-deploy command
        if self.post_deploy_cmd:
            step = await self._run_step_with_retry(
                "post-deploy",
                f'{ssh_prefix} "{self.post_deploy_cmd}"',
                result, is_remote=True,
            )

        # Step 6: Health check
        if self.health_check_url:
            step = await self._health_check_with_retry(result)
            if not step["ok"]:
                result.error = f"Health check failed: {step.get('detail', '')}"
                result.duration_s = time.time() - t0
                return result

        result.success = True
        result.duration_s = time.time() - t0
        logger.info(f"\n{result.summary()}")

        # Save deploy result
        mesh_dir = os.path.join(self.repo_dir, ".agent-mesh")
        os.makedirs(mesh_dir, exist_ok=True)
        result_path = os.path.join(mesh_dir, "deploy-result.json")
        with open(result_path, 'w') as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)

        return result

    async def _run_step_with_retry(
        self, name: str, cmd: str, result: DeployResult,
        timeout: int = 120, cwd: str | None = None, is_remote: bool = False,
    ) -> dict:
        """Run a deploy step with LLM-assisted retry on failure. Never gives up."""
        step = await self._run_step(name, cmd, timeout=timeout, cwd=cwd)
        result.steps.append(step)

        if step["ok"]:
            return step

        # Accumulate history so later retries have full context
        attempt_history: list[dict] = []

        # Retry with LLM troubleshooting (escalate through model chain)
        for retry in range(1, self.max_retries + 1):
            # Pick model: retry 1 → chain[0], retry 2 → chain[1], beyond chain → last
            model_idx = min(retry - 1, len(self.model_chain) - 1)
            model = self.model_chain[model_idx]
            logger.info(
                f"[Deployer] 🔧 Troubleshooting {name} "
                f"(retry {retry}/{self.max_retries}, model={model})..."
            )
            fix = await self._troubleshoot(
                name, cmd, step["detail"], is_remote,
                model=model, attempt_history=attempt_history,
            )

            if not fix:
                logger.warning(f"[Deployer] No fix suggested for {name}")
                attempt_history.append({
                    "retry": retry, "model": model,
                    "error": step["detail"], "fix": "no suggestion",
                })
                continue

            # Record this attempt
            attempt_history.append({
                "retry": retry, "model": model,
                "error": step["detail"],
                "diagnosis": fix.get("diagnosis", ""),
                "commands": fix.get("commands", []),
            })

            # Apply fix commands
            for fix_cmd in fix.get("commands", []):
                fix_step = await self._run_step(
                    f"{name}-fix-{retry}", fix_cmd,
                    timeout=timeout, cwd=cwd,
                )
                result.steps.append(fix_step)
                if not fix_step["ok"]:
                    logger.warning(
                        f"[Deployer] Fix command failed: {fix_cmd[:100]}"
                    )

            # Retry original command (or replacement if provided)
            retry_cmd = fix.get("retry_cmd", cmd)
            step = await self._run_step(
                f"{name}-retry-{retry}", retry_cmd,
                timeout=timeout, cwd=cwd,
            )
            result.steps.append(step)

            if step["ok"]:
                logger.info(f"[Deployer] ✅ {name} succeeded on retry {retry}")
                return step

        return step

    async def _health_check_with_retry(self, result: DeployResult) -> dict:
        """Health check with LLM-assisted retry. Never gives up."""
        step = await self._health_check()
        result.steps.append(step)

        if step["ok"]:
            return step

        ssh_prefix = self._ssh_prefix()
        attempt_history: list[dict] = []

        for retry in range(1, self.max_retries + 1):
            model_idx = min(retry - 1, len(self.model_chain) - 1)
            model = self.model_chain[model_idx]
            logger.info(
                f"[Deployer] 🔧 Troubleshooting health-check "
                f"(retry {retry}/{self.max_retries}, model={model})..."
            )
            # Gather remote diagnostics for LLM
            diag = await self._run_step(
                "diagnostics",
                f'{ssh_prefix} "cd {self.remote_dir} && '
                f'pm2 list 2>/dev/null; echo ===LOGS===; '
                f'pm2 logs --nostream --lines 20 2>/dev/null; echo ===PORTS===; '
                f'ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null"',
                timeout=30,
            )
            fix = await self._troubleshoot(
                "health-check",
                f"curl {self.health_check_url}",
                f"Health check failed: {step['detail']}\n\nDiagnostics:\n{diag.get('detail', '')}",
                is_remote=True,
                model=model,
                attempt_history=attempt_history,
            )

            if fix:
                attempt_history.append({
                    "retry": retry, "model": model,
                    "error": step["detail"],
                    "diagnosis": fix.get("diagnosis", ""),
                    "commands": fix.get("commands", []),
                })
                for fix_cmd in fix.get("commands", []):
                    fix_step = await self._run_step(
                        f"health-fix-{retry}", fix_cmd, timeout=120,
                    )
                    result.steps.append(fix_step)
            else:
                attempt_history.append({
                    "retry": retry, "model": model,
                    "error": step["detail"], "fix": "no suggestion",
                })

            # Wait and retry health check
            await asyncio.sleep(10)
            step = await self._health_check()
            result.steps.append(step)

            if step["ok"]:
                logger.info(
                    f"[Deployer] ✅ Health check passed on retry {retry}"
                )
                return step

        return step

    async def _troubleshoot(
        self, step_name: str, cmd: str, error_detail: str,
        is_remote: bool = False, model: str = "",
        attempt_history: list[dict] | None = None,
    ) -> dict | None:
        """Ask LLM to diagnose a deploy failure and suggest fix commands."""
        ssh_prefix = self._ssh_prefix()

        history_section = ""
        if attempt_history:
            history_section = "\n## Previous Attempts (all failed — try a DIFFERENT approach)\n"
            for att in attempt_history:
                history_section += (
                    f"- Retry {att['retry']} ({att['model']}): "
                    f"{att.get('diagnosis', att.get('fix', 'N/A'))} "
                    f"→ commands: {att.get('commands', 'none')}\n"
                )
            history_section += "\nDo NOT repeat the same fixes. Try something different.\n"

        prompt = f"""You are a DevOps engineer diagnosing a deployment failure.

## Failed Step
- Step: {step_name}
- Command: {cmd}
- Error: {error_detail}

## Environment
- Remote host: {self.user}@{self.host}:{self.remote_dir}
- SSH prefix: {ssh_prefix}
- Is remote command: {is_remote}
{history_section}
## Task
Analyze the error and suggest shell commands to fix it.

## Rules
1. Only suggest commands that fix the IMMEDIATE problem
2. Do NOT suggest destructive commands (rm -rf, drop database, etc.)
3. For remote fixes, wrap commands with the SSH prefix
4. Keep it minimal — 1-3 commands max
5. If the error is unfixable (e.g. wrong host, network down), return empty commands

## Output Format
Respond ONLY with JSON. No other text.
{{
  "diagnosis": "one-line explanation of the problem",
  "commands": ["shell command 1", "shell command 2"],
  "retry_cmd": null
}}

- "commands": fix commands to run BEFORE retrying
- "retry_cmd": if the original command needs modification, put the new command here; otherwise null
"""
        raw = await self._call_llm(prompt, model=model)
        try:
            # Strip markdown fences if present
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text
                if text.endswith("```"):
                    text = text[:-3]
                elif "```" in text:
                    text = text[:text.rfind("```")]
                text = text.strip()
                if text.startswith("json"):
                    text = text[4:].strip()
            data = json.loads(text)
            diagnosis = data.get("diagnosis", "")
            commands = data.get("commands", [])
            if diagnosis:
                logger.info(f"[Deployer] 🔍 Diagnosis: {diagnosis}")
            return {
                "commands": commands if isinstance(commands, list) else [],
                "retry_cmd": data.get("retry_cmd"),
            }
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"[Deployer] Failed to parse troubleshoot response: {e}")
            return None

    async def _call_llm(self, prompt: str, model: str = "") -> str:
        """Call LLM for troubleshooting. Supports Claude CLI and aider (DeepSeek/Grok)."""
        model = model or self.model_chain[0]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            if model.startswith("claude") or model.startswith("claude-"):
                # Claude CLI
                cmd = f'cat {prompt_file} | claude -p --dangerously-skip-permissions --model {model} --output-format text'
            else:
                # Non-Claude models: use claude CLI with --model flag
                # (aider doesn't support one-shot prompts well, but claude CLI
                #  only works with Anthropic models)
                # Fallback: use environment-aware API call via python
                cmd = self._build_api_cmd(prompt_file, model)

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
            result = stdout.decode().strip()
            if proc.returncode != 0:
                logger.warning(
                    f"[Deployer] LLM ({model}) returned code {proc.returncode}: "
                    f"{stderr.decode()[:200]}"
                )
            return result
        except asyncio.TimeoutError:
            logger.error(f"[Deployer] LLM ({model}) timed out (120s)")
            return "{}"
        except Exception as e:
            logger.error(f"[Deployer] LLM ({model}) error: {e}")
            return "{}"
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

    def _build_api_cmd(self, prompt_file: str, model: str) -> str:
        """Build a python one-liner to call non-Claude models via their API."""
        # Map model prefix to env var and base URL
        MODEL_API_MAP = {
            "deepseek/": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
            "xai/": ("XAI_API_KEY", "https://api.x.ai/v1"),
        }
        api_key_env = ""
        base_url = ""
        api_model = model
        for prefix, (env_var, url) in MODEL_API_MAP.items():
            if model.startswith(prefix):
                api_key_env = env_var
                base_url = url
                api_model = model[len(prefix):]
                break

        if not api_key_env:
            # Unknown model, fallback to claude sonnet
            logger.warning(
                f"[Deployer] Unknown model {model}, falling back to claude-sonnet-4-6"
            )
            return f'cat {prompt_file} | claude -p --dangerously-skip-permissions --model claude-sonnet-4-6 --output-format text'

        # Python one-liner using openai-compatible API
        return (
            f'python3 -c "'
            f'import json, os, sys; '
            f'from openai import OpenAI; '
            f'c = OpenAI(api_key=os.environ[\"{api_key_env}\"], base_url=\"{base_url}\"); '
            f'prompt = open(\"{prompt_file}\").read(); '
            f'r = c.chat.completions.create('
            f'model=\"{api_model}\", '
            f'messages=[dict(role=\"user\", content=prompt)], '
            f'temperature=0.2); '
            f'print(r.choices[0].message.content)'
            f'"'
        )

    async def _run_step(
        self, name: str, cmd: str, timeout: int = 120, cwd: str | None = None
    ) -> dict:
        """Run a deploy step and return result dict."""
        logger.info(f"[Deployer] Running: {name}...")
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            ok = proc.returncode == 0
            output = stdout.decode().strip()
            err = stderr.decode().strip()
            detail = output[-500:] if ok else (err or output)[-500:]
            if ok:
                logger.info(f"[Deployer] ✅ {name}")
            else:
                logger.warning(f"[Deployer] ❌ {name}: {detail[:200]}")
            return {"name": name, "ok": ok, "detail": detail}
        except asyncio.TimeoutError:
            logger.error(f"[Deployer] ❌ {name}: timeout ({timeout}s)")
            return {"name": name, "ok": False, "detail": f"timeout ({timeout}s)"}
        except Exception as e:
            logger.error(f"[Deployer] ❌ {name}: {e}")
            return {"name": name, "ok": False, "detail": str(e)}

    async def _health_check(self) -> dict:
        """Check if the deployed service is responding."""
        logger.info(f"[Deployer] Health check: {self.health_check_url}")
        # Wait a few seconds for services to start
        await asyncio.sleep(5)
        try:
            proc = await asyncio.create_subprocess_shell(
                f'curl -sf -o /dev/null -w "%{{http_code}}" {self.health_check_url}',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            status_code = stdout.decode().strip()
            ok = status_code.startswith("2") or status_code == "301"
            detail = f"HTTP {status_code}"
            if ok:
                logger.info(f"[Deployer] ✅ Health check: {detail}")
            else:
                logger.warning(f"[Deployer] ❌ Health check: {detail}")
            return {"name": "health-check", "ok": ok, "detail": detail}
        except Exception as e:
            return {"name": "health-check", "ok": False, "detail": str(e)}

    def _ssh_prefix(self) -> str:
        """Build SSH command prefix with sshpass."""
        return (
            f'sshpass -p \'{self.password}\' ssh '
            f'-p {self.port} -o StrictHostKeyChecking=no '
            f'{self.user}@{self.host}'
        )
