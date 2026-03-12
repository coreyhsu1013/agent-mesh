"""
Agent Mesh v2.0 — Gate Runner
Executes deterministic quality gates for a task.

Flow:
1. Resolve profile for task (explicit or heuristic)
2. Run input_checks → rule_checks → verification_checks
3. Run escalation_checks (advisory, don't block)
4. Return GateRunSummary with overall pass/fail + details
"""

from __future__ import annotations
import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field

from ..models.task import Task, GateProfile, GateResult
from .registry import GateRegistry
from .checks.basic import CHECK_REGISTRY

logger = logging.getLogger(__name__)


# ── Actionable hints per check ──
# Maps check name → instruction the agent can act on.
_CHECK_HINTS: dict[str, str] = {
    "allowed_paths_only": (
        "Only modify files listed in target_files. "
        "Do not create or touch files outside the task scope."
    ),
    "no_new_dependency": (
        "Do not add new package dependencies (package.json / requirements.txt). "
        "Use only packages already present in the project."
    ),
    "no_secret_leak": (
        "Do not hardcode API keys, tokens, passwords, or private keys. "
        "Use environment variables or config references instead."
    ),
    "diff_not_empty": (
        "Ensure your changes produce a non-empty diff. "
        "The task must create or modify at least one file."
    ),
    "build_pass": (
        "Fix the build errors shown below. "
        "The project must compile/build without errors."
    ),
    "tests_pass": (
        "Fix the failing tests shown below. "
        "All existing tests must pass after your changes."
    ),
}


@dataclass
class GateFeedback:
    """Structured, actionable feedback from a gate failure for agent retry."""
    failed_checks: list[str] = field(default_factory=list)
    summary: str = ""
    actionable_hints: list[str] = field(default_factory=list)
    raw_details: str = ""
    attempt: int = 1

    def to_dict(self) -> dict:
        return {
            "failed_checks": self.failed_checks,
            "summary": self.summary,
            "actionable_hints": self.actionable_hints,
            "raw_details": self.raw_details,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GateFeedback:
        if not d:
            return cls()
        return cls(
            failed_checks=d.get("failed_checks", []),
            summary=d.get("summary", ""),
            actionable_hints=d.get("actionable_hints", []),
            raw_details=d.get("raw_details", ""),
            attempt=d.get("attempt", 1),
        )

    def to_prompt_block(self) -> str:
        """Format as a prompt block for agent consumption."""
        lines = [
            f"## ⚠️ GATE CHECK FAILURE (retry #{self.attempt})",
            f"Your previous output PASSED execution but FAILED {len(self.failed_checks)} "
            f"deterministic quality gate check(s):",
            "",
            "### Failed Checks:",
        ]
        for check in self.failed_checks:
            lines.append(f"- {check}")
        lines.append("")
        lines.append("### Required Fixes:")
        for i, hint in enumerate(self.actionable_hints, 1):
            lines.append(f"{i}. {hint}")
        if self.raw_details:
            lines.append("")
            lines.append("### Error Details:")
            lines.append("```")
            lines.append(self.raw_details[:2000])
            lines.append("```")
        lines.append("")
        lines.append(
            "Fix ONLY the issues above. Do NOT introduce new problems. "
            "Do NOT rewrite code that was already correct."
        )
        return "\n".join(lines)


class GateRunSummary:
    """Aggregated result from running all gate checks."""

    def __init__(self):
        self.overall_passed: bool = True
        self.results: list[GateResult] = []
        self.escalations: list[str] = []  # advisory, not blocking
        self.duration_sec: float = 0.0

    @property
    def failed_checks(self) -> list[str]:
        failed = []
        for r in self.results:
            if not r.passed:
                failed.extend(r.failed_checks)
        return failed

    def to_dict(self) -> dict:
        return {
            "overall_passed": self.overall_passed,
            "results": [r.to_dict() for r in self.results],
            "escalations": self.escalations,
            "failed_checks": self.failed_checks,
            "duration_sec": round(self.duration_sec, 2),
        }

    def to_feedback(self, attempt: int = 1) -> GateFeedback:
        """Convert gate failure into structured, actionable feedback for retry."""
        checks = self.failed_checks
        # Collect raw details from failed phases
        raw_parts = []
        for r in self.results:
            if not r.passed and r.details:
                raw_parts.append(r.details)
        raw_details = "; ".join(raw_parts)

        # Build actionable hints from check-specific mappings
        hints = []
        for check in checks:
            hint = _CHECK_HINTS.get(check)
            if hint:
                hints.append(hint)
            else:
                hints.append(f"Fix the '{check}' gate check failure.")

        summary = (
            f"{len(checks)} gate check(s) failed: {', '.join(checks)}"
        )

        return GateFeedback(
            failed_checks=checks,
            summary=summary,
            actionable_hints=hints,
            raw_details=raw_details,
            attempt=attempt,
        )


class GateRunner:
    """Runs deterministic gate checks for tasks."""

    def __init__(self, registry: GateRegistry | None = None):
        self.registry = registry or GateRegistry()
        self.check_registry = CHECK_REGISTRY

    async def run(
        self,
        task: Task,
        diff: str = "",
        workspace_dir: str = "",
    ) -> GateRunSummary:
        """
        Run all gate checks for a task.
        Returns GateRunSummary with overall pass/fail.
        """
        start = time.time()
        summary = GateRunSummary()

        profile = self.registry.resolve_profile(task)
        logger.info(
            f"[Gate] Running '{profile.name}' for '{task.title}'"
        )

        # Run check phases in order (input → rule → verification)
        # Input and rule checks run on the diff; verification needs workspace
        check_kwargs = {
            "task": task,
            "diff": diff,
            "workspace_dir": workspace_dir,
        }

        # Phase 1: Input checks
        input_result = await self._run_phase(
            "input", profile.input_checks, **check_kwargs
        )
        summary.results.append(input_result)
        if not input_result.passed:
            # Input check failure is a warning, not a blocker
            # (tasks from old plans may lack metadata)
            logger.warning(
                f"[Gate] Input check warning for '{task.title}': "
                f"{input_result.details}"
            )
            # Don't fail overall for input checks — be conservative
            input_result.passed = True

        # Phase 2: Rule checks (on diff)
        rule_result = await self._run_phase(
            "rule", profile.rule_checks, **check_kwargs
        )
        summary.results.append(rule_result)
        if not rule_result.passed:
            summary.overall_passed = False

        # Phase 3: Verification checks (build/test)
        verify_result = await self._run_phase(
            "verification", profile.verification_checks, **check_kwargs
        )
        summary.results.append(verify_result)
        if not verify_result.passed:
            summary.overall_passed = False

        # Phase 4: Escalation checks (advisory only — don't block)
        # Note: escalation checks return (True, reason) when escalation IS needed,
        # so we invert: "passed=True" means "should escalate".
        escalation_result = await self._run_phase(
            "escalation", profile.escalation_checks,
            invert_pass=True, **check_kwargs,
        )
        for check_name in escalation_result.failed_checks:
            summary.escalations.append(
                f"{check_name}: {escalation_result.details}"
            )
        # Don't add to overall_passed — escalations are advisory

        summary.duration_sec = time.time() - start

        # Note: gate_results persistence is handled by the caller (dispatcher).
        # GateRunner only sets escalation_reason as a convenience.
        if summary.escalations:
            task.escalation_reason = "; ".join(summary.escalations)

        status = "✅ PASS" if summary.overall_passed else "❌ FAIL"
        logger.info(
            f"[Gate] {status} '{task.title}' "
            f"({profile.name}, {summary.duration_sec:.1f}s)"
        )
        if summary.failed_checks:
            logger.info(
                f"[Gate] Failed checks: {', '.join(summary.failed_checks)}"
            )
        if summary.escalations:
            logger.info(
                f"[Gate] ⚠️ Escalations: {', '.join(summary.escalations)}"
            )

        return summary

    async def _run_phase(
        self,
        phase_name: str,
        check_names: list[str],
        invert_pass: bool = False,
        **kwargs,
    ) -> GateResult:
        """
        Run a list of checks for a phase, return aggregated GateResult.

        invert_pass: for escalation checks where True = "should escalate" (flagged).
        """
        result = GateResult(
            gate_name=phase_name,
            passed=True,
            timestamp=time.time(),
        )

        if not check_names:
            result.details = "No checks defined"
            return result

        details_parts = []
        for check_name in check_names:
            check_fn = self.check_registry.get(check_name)
            if not check_fn:
                logger.warning(f"[Gate] Unknown check: {check_name}")
                continue

            try:
                # Support both sync and async check functions
                if inspect.iscoroutinefunction(check_fn):
                    raw_passed, detail = await check_fn(**kwargs)
                else:
                    raw_passed, detail = check_fn(**kwargs)

                # For escalation: True = flagged = treat as "failed"
                passed = (not raw_passed) if invert_pass else raw_passed

                if not passed:
                    result.passed = False
                    result.failed_checks.append(check_name)
                details_parts.append(f"{check_name}: {detail}")

            except Exception as e:
                logger.warning(f"[Gate] Check '{check_name}' error: {e}")
                # Don't fail on check errors — be conservative
                details_parts.append(f"{check_name}: ERROR ({e})")

        result.details = "; ".join(details_parts)
        return result
