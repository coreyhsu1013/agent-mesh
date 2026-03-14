"""
Agent Mesh v0.6.5 — ReAct Agent Loop
Think → Act → Observe → Evaluate → Retry

v0.6.5 改進：
- 空輸出判定為失敗（之前會通過）
- agent_kwargs 透傳 model/use_chat 給 runner
- 更好的 build/test 錯誤偵測
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol, Any

logger = logging.getLogger(__name__)

# ── Must-change task detection ──
# These tasks MUST produce file changes; 0-file diff = failed attempt.
_MUST_CHANGE_KEYWORDS = [
    "scaffold", "bootstrap", "foundation", "project setup",
    "init project", "boilerplate", "layout", "initial setup",
]


def _is_must_change_task(task: Any) -> bool:
    """Check if task metadata matches must-change keywords.
    Checks title, task_type, category, and module.
    """
    parts = [
        getattr(task, "title", None) or "",
        getattr(task, "task_type", None) or "",
        getattr(task, "category", None) or "",
        getattr(task, "module", None) or "",
    ]
    text = " ".join(parts).lower()
    return any(kw in text for kw in _MUST_CHANGE_KEYWORDS)


def _has_meaningful_changes(observation: "Observation") -> bool:
    """True if observation contains actual file changes (diff or files_changed)."""
    has_files = bool(observation.files_changed)
    has_diff = bool(observation.diff and observation.diff.strip() and len(observation.diff.strip()) > 10)
    return has_files or has_diff


@dataclass
class RunResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


@dataclass
class Observation:
    diff: str
    build_output: str
    test_output: str
    lint_output: str
    files_changed: list[str]
    success: bool
    error: Optional[str] = None
    duration_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "files_changed": self.files_changed,
            "success": self.success,
            "error": self.error,
            "duration_sec": round(self.duration_sec, 1),
            "diff_lines": len(self.diff.split("\n")) if self.diff else 0,
        }


@dataclass
class LoopHistory:
    attempts: list[dict] = field(default_factory=list)

    def add_attempt(self, thinking: str, observation: Observation):
        self.attempts.append({
            "round": len(self.attempts) + 1,
            "thinking": thinking[:500],
            "diff_summary": observation.diff[:1000] if observation.diff else "",
            "build_error": self._extract_errors(observation.build_output),
            "test_error": self._extract_errors(observation.test_output),
            "error": observation.error,
            "success": observation.success,
            "files_changed": observation.files_changed,
            "duration_sec": observation.duration_sec,
        })

    def to_context(self) -> str:
        if not self.attempts:
            return ""
        lines = ["## ⚠️ Previous Attempts (DO NOT repeat the same mistakes):"]
        for a in self.attempts:
            status = "✅ Success" if a["success"] else "❌ Failed"
            lines.append(f"\n### Round {a['round']} ({status}):")
            lines.append(f"- Approach: {a['thinking']}")
            if a.get("files_changed"):
                lines.append(f"- Files changed: {', '.join(a['files_changed'][:10])}")
            if a.get("build_error"):
                lines.append(f"- Build errors:\n```\n{a['build_error']}\n```")
            if a.get("test_error"):
                lines.append(f"- Test errors:\n```\n{a['test_error']}\n```")
            if a.get("error"):
                lines.append(f"- Agent error: {a['error']}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(self.attempts, ensure_ascii=False)

    @staticmethod
    def _extract_errors(output: str, max_lines: int = 20) -> str:
        if not output:
            return ""
        error_keywords = ["error", "Error", "ERROR", "FAIL", "fail", "TypeError",
                          "SyntaxError", "ReferenceError", "Cannot find", "not found",
                          "Module not found", "TS2", "TS7"]
        lines = output.split("\n")
        error_lines = []
        for i, line in enumerate(lines):
            if any(kw in line for kw in error_keywords):
                start = max(0, i - 1)
                end = min(len(lines), i + 3)
                for ctx_line in lines[start:end]:
                    if ctx_line.strip() and ctx_line not in error_lines:
                        error_lines.append(ctx_line)
            if len(error_lines) >= max_lines:
                break
        return "\n".join(error_lines[:max_lines])


@dataclass
class TaskResult:
    task_id: str
    status: str              # "completed" | "failed"
    attempts: int
    final_diff: str = ""
    error: Optional[str] = None
    history: Optional[LoopHistory] = None
    total_duration_sec: float = 0.0
    final_model: str = ""    # ★ 最終成功的 model label
    # v0.9: cost tracking
    cost_results: list = field(default_factory=list)  # list[CostResult]
    total_cost_usd: float = 0.0
    # v2.0: observation artifacts for gate runner
    observation_artifacts: dict = field(default_factory=dict)


class AgentRunner(Protocol):
    async def execute(self, prompt: str, workspace_dir: str,
                      target_files: list[str] | None = None, **kwargs) -> RunResult:
        ...


class ReactLoop:
    """
    ReAct Loop: Think → Act → Observe → Evaluate → Retry

    v0.7.1: Matrix-based routing — router.get_model_for_attempt(complexity, attempt)
    每個 attempt 從 routing matrix 查表取 model + runner。
    """

    def __init__(self, config: dict | None = None):
        react_cfg = (config or {}).get("react", {})
        self.run_tests = react_cfg.get("run_tests", True)
        self.run_build = react_cfg.get("run_build", True)
        self.run_lint = react_cfg.get("run_lint", False)
        self.retry_delay_base = react_cfg.get("retry_delay_base", 5)
        # v1.2: skip build in worktree — real build at merge time
        self.skip_worktree_build = react_cfg.get(
            "skip_worktree_build", False
        )

    async def execute_task(
        self,
        task: Any,
        runners: dict,       # AgentType → runner instance
        router: Any,         # ModelRouter
        workspace_dir: str,
        shared_context: str = "",
        start_attempt: int = 1,
        single_attempt: bool = False,
    ) -> TaskResult:
        from ..models.task import AgentType

        history = LoopHistory()
        cost_results: list = []  # list[CostResult] — v0.9
        start_time = time.time()
        complexity = getattr(task, "complexity", "M")
        max_attempts = router.get_max_attempts(complexity)
        # single_attempt: pin to one model (used by gate retry/escalation)
        if single_attempt:
            max_attempts = start_attempt

        for attempt in range(start_attempt, max_attempts + 1):
            # ★ Query routing matrix for this attempt
            decision = router.get_model_for_attempt(complexity, attempt)
            current_runner = runners.get(decision.agent_type)
            if not current_runner:
                current_runner = runners.get(AgentType.CLAUDE_CODE)

            current_kwargs: dict[str, Any] = {"model": decision.model, "task_id": task.id}
            if decision.timeout_multiplier != 1.0:
                current_kwargs["timeout_multiplier"] = decision.timeout_multiplier
            # v1.3: absolute timeout override
            if decision.force_timeout_seconds > 0:
                current_kwargs["force_timeout_seconds"] = decision.force_timeout_seconds

            model_label = decision.model_short
            escalation_info = ""
            if attempt > 1:
                escalation_info = f" [⬆ {model_label}]"
            if decision.force_timeout_seconds > 0:
                escalation_info += f" [timeout={decision.force_timeout_seconds}s]"
            elif decision.timeout_multiplier > 1:
                escalation_info += f" [timeout ×{decision.timeout_multiplier:.0f}]"

            logger.info(
                f"[ReAct] Task '{task.title}' — Attempt {attempt}/{max_attempts}"
                f"{escalation_info}"
            )

            # ── THINK ──
            prompt = self._build_prompt(task, shared_context, history)

            # ── ACT ──
            act_start = time.time()
            run_result = await current_runner.execute(
                prompt=prompt,
                workspace_dir=workspace_dir,
                target_files=getattr(task, "target_files", None),
                **current_kwargs,
            )
            act_duration = time.time() - act_start

            # ── COST ── (v0.9: track per-attempt cost)
            if hasattr(run_result, "cost") and run_result.cost:
                cost_results.append(run_result.cost)

            if not run_result.success and run_result.error:
                logger.warning(
                    f"[ReAct] Runner error for '{task.title}': {run_result.error}"
                )

            # ── OBSERVE ──
            observation = await self._observe(workspace_dir, run_result)
            observation.duration_sec = act_duration

            # ── NO-FILE-CHANGES guard for must-change tasks ──
            if (observation.success
                    and not _has_meaningful_changes(observation)
                    and _is_must_change_task(task)):
                observation.success = False
                observation.error = "no_file_changes"
                logger.warning(
                    f"[ReAct] Task '{task.title}' — "
                    f"Execution failed: no file changes produced"
                )

            history.add_attempt(
                thinking=f"Attempt {attempt} ({model_label}): {task.title}",
                observation=observation,
            )

            logger.info(
                f"[ReAct] Task '{task.title}' — Attempt {attempt} → "
                f"{'✅ Success' if observation.success else '❌ Failed'} "
                f"({observation.duration_sec:.1f}s, {len(observation.files_changed)} files)"
                f"{escalation_info}"
            )

            # ── EVALUATE ──
            if observation.success:
                total_cost = sum(c.estimated_usd for c in cost_results if hasattr(c, 'estimated_usd'))
                return TaskResult(
                    task_id=task.id, status="completed", attempts=attempt,
                    final_diff=observation.diff, history=history,
                    total_duration_sec=time.time() - start_time,
                    final_model=decision.model,
                    cost_results=cost_results,
                    total_cost_usd=total_cost,
                    observation_artifacts=observation.to_dict(),
                )

            if attempt == max_attempts:
                logger.warning(f"[ReAct] Task '{task.title}' — Max attempts reached")
                total_cost = sum(c.estimated_usd for c in cost_results if hasattr(c, 'estimated_usd'))
                return TaskResult(
                    task_id=task.id, status="failed", attempts=attempt,
                    error=observation.error or "Max attempts reached",
                    final_diff=observation.diff, history=history,
                    total_duration_sec=time.time() - start_time,
                    final_model=decision.model,
                    cost_results=cost_results,
                    total_cost_usd=total_cost,
                )

            delay = self.retry_delay_base * attempt
            logger.info(f"[ReAct] Waiting {delay}s before retry...")
            await asyncio.sleep(delay)

        return TaskResult(
            task_id=task.id, status="failed", attempts=max_attempts,
            error="Unexpected exit", history=history,
            total_duration_sec=time.time() - start_time,
        )

    def _build_prompt(self, task: Any, shared_context: str, history: LoopHistory) -> str:
        parts = [f"# Task: {task.title}", f"\n## Description:\n{task.description}"]

        if hasattr(task, "acceptance_criteria") and task.acceptance_criteria:
            parts.append(f"\n## Acceptance Criteria:\n{task.acceptance_criteria}")

        target_files = getattr(task, "target_files", None)
        if target_files:
            parts.append("\n## Target Files:")
            for f in target_files:
                parts.append(f"- {f}")

        if shared_context:
            parts.append(f"\n## Project Context:\n{shared_context[:2000]}")

        # v2.0: inject gate feedback from previous gate failure
        gate_fb = getattr(task, "gate_feedback", None)
        if gate_fb:
            from ..gates.runner import GateFeedback
            feedback = GateFeedback.from_dict(gate_fb)
            if feedback.failed_checks:
                parts.append(f"\n{feedback.to_prompt_block()}")

        history_ctx = history.to_context()
        if history_ctx:
            parts.append(f"\n{history_ctx}")
            parts.append(
                "\n## ⚠️ IMPORTANT: Previous attempts failed. "
                "Try a DIFFERENT approach. Do NOT repeat the same mistakes."
            )

        return "\n".join(parts)

    async def _observe(self, workspace_dir: str, run_result: RunResult) -> Observation:
        # Agent 失敗或空輸出
        if not run_result.success:
            return Observation(
                diff="", build_output="", test_output="", lint_output="",
                files_changed=[], success=False, error=run_result.error,
            )

        # ★ 空輸出 = agent 沒做任何改動 → 失敗
        if not run_result.stdout or len(run_result.stdout.strip()) < 10:
            return Observation(
                diff="", build_output="", test_output="", lint_output="",
                files_changed=[], success=False,
                error="Agent produced empty or minimal output",
            )

        # 1) Git diff (stage untracked files first so scaffold tasks show up)
        #    Exclude runtime files and build artifacts from staging
        from .workspace import GIT_ADD_PATHSPEC
        await self._run_cmd(
            f"cd {workspace_dir} && git {GIT_ADD_PATHSPEC} 2>/dev/null"
        )
        diff = await self._run_cmd(
            f"cd {workspace_dir} && git diff --cached HEAD 2>/dev/null || git diff --cached 2>/dev/null || echo ''"
        )

        # 2) Build (skip in worktree if configured — real build at merge)
        build_output = ""
        is_worktree = "/.agent-mesh/workspaces/" in workspace_dir
        if self.run_build and not (self.skip_worktree_build and is_worktree):
            build_output = await self._run_cmd(
                f"cd {workspace_dir} && "
                f"(pnpm run build 2>&1 || npm run build 2>&1 || "
                f"npx tsc --noEmit 2>&1 || echo 'NO_BUILD_SCRIPT')"
            )

        # 3) Test (only if test framework exists)
        test_output = ""
        if self.run_tests:
            test_output = await self._run_cmd(
                f"cd {workspace_dir} && "
                f"(grep -q vitest package.json 2>/dev/null && pnpm test --passWithNoTests 2>&1 || "
                f"grep -q jest package.json 2>/dev/null && pnpm test -- --passWithNoTests 2>&1 || "
                f"echo 'NO_TEST_SCRIPT')"
            )

        # 4) Lint
        lint_output = ""
        if self.run_lint:
            lint_output = await self._run_cmd(
                f"cd {workspace_dir} && (pnpm run lint 2>&1 || echo 'NO_LINT')"
            )

        # 5) Evaluate
        has_build_error = self._has_errors(build_output)
        has_test_error = (
            test_output
            and "NO_TEST_SCRIPT" not in test_output
            and ("FAIL" in test_output or "Error" in test_output[:200])
        )

        files_changed = self._parse_changed_files(diff)

        overall_success = (
            run_result.success
            and not has_build_error
            and not has_test_error
        )

        error_msg = None
        if not overall_success:
            errors = []
            if has_build_error:
                errors.append(f"Build: {build_output[:300]}")
            if has_test_error:
                errors.append(f"Test: {test_output[:300]}")
            error_msg = " | ".join(errors) if errors else None

        return Observation(
            diff=diff[:5000], build_output=build_output[:3000],
            test_output=test_output[:3000], lint_output=lint_output[:1000],
            files_changed=files_changed, success=overall_success,
            error=error_msg,
        )

    @staticmethod
    def _has_errors(output: str) -> bool:
        if not output or output.strip() == "NO_BUILD_SCRIPT":
            return False
        indicators = [
            "error TS", "Error:", "ERROR", "SyntaxError",
            "TypeError", "ReferenceError", "Cannot find module",
            "Module not found", "ENOENT", "failed with exit code",
        ]
        return any(ind in output for ind in indicators)

    @staticmethod
    def _parse_changed_files(diff: str) -> list[str]:
        files = []
        for line in diff.split("\n"):
            if line.startswith("diff --git"):
                parts = line.split(" b/")
                if len(parts) > 1:
                    files.append(parts[1])
        return files

    @staticmethod
    async def _run_cmd(cmd: str, timeout: int = 60) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode(errors="replace")[:5000] if stdout else ""
        except asyncio.TimeoutError:
            return "TIMEOUT"
        except Exception as e:
            return f"CMD_ERROR: {e}"
