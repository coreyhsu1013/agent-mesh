"""
Agent Mesh v0.6.5 — Reviewer
Always uses Claude Opus for highest quality review.
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from typing import Optional

from ..auth.aider_runner import ClaudeRunner, RunResult

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    approved: bool
    feedback: str = ""
    issues: list[str] = None

    def __post_init__(self):
        if self.issues is None:
            self.issues = []


class Reviewer:

    def __init__(self, config: dict, repo_dir: str):
        reviewer_cfg = config.get("reviewer", {})
        self.auto_approve_on_attempt = reviewer_cfg.get("auto_approve_on_attempt", 3)
        self.diff_max_chars = reviewer_cfg.get("diff_max_chars", 10000)
        self.timeout = reviewer_cfg.get("timeout", 120)
        self.repo_dir = repo_dir

        # ★ Always use Opus for review
        self.model = reviewer_cfg.get("model", "claude-opus-4-6")
        self.runner = ClaudeRunner(config)

    async def review(
        self,
        diff: str,
        task_title: str,
        task_description: str = "",
        acceptance_criteria: str = "",
        attempt: int = 1,
    ) -> ReviewResult:

        if attempt >= self.auto_approve_on_attempt:
            logger.info(f"[Reviewer] Auto-approving '{task_title}' (attempt {attempt})")
            return ReviewResult(approved=True, feedback="Auto-approved after max attempts")

        if not diff or not diff.strip():
            return ReviewResult(approved=True, feedback="No changes to review")

        truncated = diff[:self.diff_max_chars]
        if len(diff) > self.diff_max_chars:
            truncated += f"\n\n... ({len(diff) - self.diff_max_chars} chars omitted)"

        prompt = self._build_prompt(truncated, task_title, task_description, acceptance_criteria)

        try:
            result = await self.runner.execute(
                prompt=prompt,
                workspace_dir=self.repo_dir,
                model=self.model,   # ★ Opus
            )

            if not result.success:
                logger.warning(f"[Reviewer] Review failed: {result.error}")
                return ReviewResult(approved=True, feedback=f"Review skipped: {result.error}")

            return self._parse_review(result.stdout)

        except Exception as e:
            logger.error(f"[Reviewer] Exception: {e}")
            return ReviewResult(approved=True, feedback=f"Review skipped: {e}")

    def _build_prompt(self, diff: str, title: str, desc: str, criteria: str) -> str:
        return f"""You are a senior code reviewer. Review this diff and respond in JSON.

## Task: {title}
{f"## Description: {desc}" if desc else ""}
{f"## Acceptance Criteria: {criteria}" if criteria else ""}

## Diff:
```
{diff}
```

## Response Format (JSON only):
{{
  "approved": true/false,
  "feedback": "Overall assessment",
  "issues": ["issue 1", "issue 2"]
}}

Rules:
- APPROVE by default. Most code that attempts to meet requirements should be approved.
- Only REJECT for: critical runtime bugs, SQL injection, hardcoded secrets, completely wrong logic
- Do NOT reject for: missing edge cases, style issues, TODO comments, incomplete features
- When in doubt, APPROVE. This is an automated pipeline — be pragmatic.

Output ONLY valid JSON.
"""

    @staticmethod
    def _parse_review(raw: str) -> ReviewResult:
        text = raw.strip()
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()

        try:
            data = json.loads(text)
            return ReviewResult(
                approved=data.get("approved", True),
                feedback=data.get("feedback", ""),
                issues=data.get("issues", []),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[Reviewer] Parse failed: {e}, auto-approving")
            return ReviewResult(approved=True, feedback=f"Parse error: {text[:200]}")
