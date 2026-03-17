"""
Agent Mesh v2.1 — Task Normalizer

Normalizes tasks after planning/conversion/gap-analysis:
- Infers task_type from title/description keywords (11 types)
- Applies type-specific rules (allowed_no_diff, required_target_files)
- Builds verifier_scope from task metadata
- Sets chunk_id if provided

Integration points:
  1. planner.py: after gate enrichment
  2. change_converter.py: after conversion
  3. gap_analyzer.py: after fix-plan generation
"""
from __future__ import annotations

import logging
import re

from ..models.task import Task

logger = logging.getLogger(__name__)


# ── task_type inference rules (ordered by specificity) ──

_TASK_TYPE_RULES: list[tuple[list[str], str]] = [
    # Analysis / research — no code changes expected
    (["analysis", "research", "investigate", "evaluate", "audit", "review spec"], "analysis"),
    # Schema / DB
    (["schema", "prisma", "migration", "alembic", "drizzle"], "schema"),
    # Data models
    (["model", "entity", "dataclass", "pydantic", "type definition"], "model"),
    # Business logic
    (["service", "business logic", "use case", "domain logic"], "service"),
    # Routes / API
    (["route", "router", "endpoint", "controller", "api handler"], "router"),
    # Event-driven
    (["listener", "event handler", "subscriber", "event listener"], "listener"),
    # Background jobs
    (["scheduler", "cron", "periodic", "worker", "background job"], "scheduler"),
    # Frontend
    (["frontend", "page", "component", "next.js", "react", "vue", "svelte"], "frontend"),
    # Test only
    (["test", "e2e", "playwright", "jest", "vitest", "spec.ts"], "test_only"),
    # Migration (data, not schema)
    (["migrate data", "seed", "backfill"], "migration"),
    # Config
    (["config", "env", "yaml", "settings", ".env"], "config"),
]


class TaskNormalizer:
    """Normalizes task metadata for scope control and gate routing."""

    def normalize(self, task: Task, chunk_id: str = "") -> None:
        """
        Normalize a single task in-place.

        task_type resolution priority:
          1. Explicit task_type already set (from planner or plan.json) — kept as-is
          2. Keyword inference from title+description — _infer_task_type()
          3. Fallback: "general" → routes to coding_basic gate
        """
        # Set chunk_id
        if chunk_id and not task.chunk_id:
            task.chunk_id = chunk_id

        # Infer task_type if empty (priority 2-3; priority 1 = already set)
        if not task.task_type:
            task.task_type = self._infer_task_type(task)

        # Apply type-specific rules
        self._apply_type_rules(task)

        # Build verifier_scope if empty
        if not task.verifier_scope:
            task.verifier_scope = self._build_verifier_scope(task)

    def normalize_plan(self, tasks: list[Task], chunk_id: str = "") -> dict:
        """
        Normalize all tasks in a plan. Returns stats dict.
        """
        type_counts: dict[str, int] = {}
        no_target_files = 0

        for task in tasks:
            self.normalize(task, chunk_id=chunk_id)
            task_type = task.task_type or "unknown"
            type_counts[task_type] = type_counts.get(task_type, 0) + 1
            if not task.target_files and not task.allowed_no_diff:
                no_target_files += 1

        logger.info(
            f"[Normalizer] {len(tasks)} tasks normalized: "
            + ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items()))
        )
        if no_target_files:
            logger.warning(
                f"[Normalizer] ⚠️ {no_target_files} implementation tasks have no target_files"
            )

        return {
            "type_distribution": type_counts,
            "no_target_files": no_target_files,
            "total": len(tasks),
        }

    @staticmethod
    def _infer_task_type(task: Task) -> str:
        """Infer task_type from title + description keywords."""
        title = (task.title or "").lower()
        desc = (task.description or "").lower()[:500]  # limit scan
        text = f"{title} {desc}"

        for keywords, task_type in _TASK_TYPE_RULES:
            for kw in keywords:
                if " " in kw:
                    if kw in text:
                        return task_type
                else:
                    if re.search(r'\b' + re.escape(kw) + r'\b', text):
                        return task_type

        return "general"

    @staticmethod
    def _apply_type_rules(task: Task) -> None:
        """Apply type-specific defaults."""
        task_type = task.task_type

        # Analysis tasks: no diff required
        if task_type == "analysis":
            task.allowed_no_diff = True
            task.required_target_files = []
            task.min_changed_files = 0
            return

        # All implementation types: populate required_target_files from target_files if empty
        if not task.required_target_files and task.target_files:
            task.required_target_files = list(task.target_files)
            task.min_changed_files = max(task.min_changed_files, 1)

    @staticmethod
    def _build_verifier_scope(task: Task) -> list[str]:
        """Build verifier scope entries from task metadata."""
        parts: list[str] = []

        if task.module and task.module != "core":
            parts.append(f"module: {task.module}")

        if task.target_files:
            files_str = ", ".join(task.target_files[:5])
            if len(task.target_files) > 5:
                files_str += f" (+{len(task.target_files) - 5} more)"
            parts.append(f"files: {files_str}")

        if task.acceptance_criteria:
            parts.append(f"criteria: {task.acceptance_criteria[:100]}")

        return parts
