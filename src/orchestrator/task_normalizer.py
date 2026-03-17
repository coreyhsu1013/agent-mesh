"""
Agent Mesh v2.2 — Task Normalizer

Normalizes tasks after planning/conversion/gap-analysis:
- Infers task_type from title/description keywords (11 types)
- Infers target_files via 6-layer priority chain (v2.2)
- Infers related_dirs with repo-aware existence filtering (v2.2)
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
import os
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

# ── target_files inference patterns (Layer 6 = last resort) ──

_FILE_PATH_RE = re.compile(r'(?:^|[\s\'"`(])([a-zA-Z][\w./\-]*\.\w{1,10})(?:[\s\'"`),:]|$)', re.MULTILINE)
_DIR_PATH_RE = re.compile(r'(?:^|[\s\'"`(])([a-zA-Z][\w./\-]+/)(?:[\s\'"`),:]|$)', re.MULTILINE)

# Sibling shared directories to infer as related_dirs
_SIBLING_SHARED_DIRS = {"shared", "common"}

# Max target_files to keep from inference
_MAX_INFERRED_FILES = 8


class TaskNormalizer:
    """Normalizes task metadata for scope control and gate routing."""

    def normalize(self, task: Task, chunk_id: str = "", repo_dir: str = "") -> None:
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

        # Infer target_files if missing (skip analysis tasks)
        if not task.target_files and task.task_type != "analysis":
            inferred = self._infer_target_files(task, repo_dir=repo_dir)
            if inferred:
                task.target_files = inferred
                logger.info(
                    f"[Normalizer] Inferred {len(inferred)} target_files for '{task.title}': "
                    f"{inferred[:3]}{'...' if len(inferred) > 3 else ''}"
                )
            else:
                # Record structured inference miss
                task.inference_miss = {
                    "reason": "no_paths_found",
                    "searched": ["source_gaps", "description", f"module={task.module}"],
                    "suggested_module": task.module or "",
                    "suggested_paths": self._module_to_dirs(task.module) if task.module else [],
                }
                logger.warning(
                    f"[Normalizer] ⚠️ No target_files inferred for '{task.title}' "
                    f"(module={task.module}, task_type={task.task_type})"
                )

        # Infer related_dirs (repo-aware: only keep existing dirs)
        if not task.related_dirs and task.target_files:
            related = self._infer_related_dirs(task, repo_dir=repo_dir)
            if related:
                task.related_dirs = related
                logger.debug(
                    f"[Normalizer] Inferred related_dirs for '{task.title}': {related}"
                )

        # Apply type-specific rules
        self._apply_type_rules(task)

        # Build verifier_scope if empty
        if not task.verifier_scope:
            task.verifier_scope = self._build_verifier_scope(task)

    def normalize_plan(self, tasks: list[Task], chunk_id: str = "", repo_dir: str = "") -> dict:
        """
        Normalize all tasks in a plan. Returns stats dict.
        """
        type_counts: dict[str, int] = {}
        no_target_files = 0
        inference_misses = 0

        for task in tasks:
            self.normalize(task, chunk_id=chunk_id, repo_dir=repo_dir)
            task_type = task.task_type or "unknown"
            type_counts[task_type] = type_counts.get(task_type, 0) + 1
            if not task.target_files and not task.allowed_no_diff:
                no_target_files += 1
            if task.inference_miss:
                inference_misses += 1

        logger.info(
            f"[Normalizer] {len(tasks)} tasks normalized: "
            + ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items()))
        )
        if no_target_files:
            logger.warning(
                f"[Normalizer] ⚠️ {no_target_files} implementation tasks have no target_files"
            )
        if inference_misses:
            logger.warning(
                f"[Normalizer] ⚠️ {inference_misses} tasks with inference_miss"
            )

        return {
            "type_distribution": type_counts,
            "no_target_files": no_target_files,
            "inference_misses": inference_misses,
            "total": len(tasks),
        }

    # ── target_files inference (6-layer priority chain) ──

    def _infer_target_files(self, task: Task, repo_dir: str = "") -> list[str]:
        """
        6-layer priority inference for target_files. Cap at _MAX_INFERRED_FILES.
        repo_dir used for existence filtering (layer 5).

        Priority:
          1. task.target_files          — already set, caller should skip this method
          2. task.required_target_files  — promote to target_files
          3. source_gaps metadata        — extract file paths from gap IDs/text
          4. description verifier refs   — "file: app/sales/models.py" patterns
          5. module/chunk canonical map  — module name → candidate dirs (repo-aware)
          6. title/description regex     — last resort fallback
        """
        collected: list[str] = []

        # Layer 2: promote required_target_files
        if task.required_target_files:
            collected.extend(task.required_target_files)
            logger.debug(f"[Normalizer] Layer 2: promoted {len(task.required_target_files)} required_target_files")

        # Layer 3: extract from source_gaps
        if not collected and task.source_gaps:
            for gap_text in task.source_gaps:
                collected.extend(self._extract_paths_from_text(gap_text))
            if collected:
                logger.debug(f"[Normalizer] Layer 3: extracted {len(collected)} paths from source_gaps")

        # Layer 4: extract from description (verifier references)
        if not collected and task.description:
            collected = self._extract_paths_from_text(task.description)
            if collected:
                logger.debug(f"[Normalizer] Layer 4: extracted {len(collected)} paths from description")

        # Layer 5: module → candidate dirs (repo-aware)
        if not collected and task.module and task.module != "core":
            candidates = self._module_to_dirs(task.module)
            if repo_dir:
                candidates = self._filter_existing(candidates, repo_dir)
            if candidates:
                collected = candidates
                logger.debug(f"[Normalizer] Layer 5: inferred {len(collected)} dirs from module={task.module}")

        # Layer 6: regex fallback from title + description
        if not collected:
            text = f"{task.title or ''} {task.description or ''}"
            collected = self._extract_paths_from_text(text)
            if collected:
                logger.debug(f"[Normalizer] Layer 6: regex extracted {len(collected)} paths")

        # Deduplicate and cap
        seen: set[str] = set()
        result: list[str] = []
        for p in collected:
            if p not in seen:
                seen.add(p)
                result.append(p)
            if len(result) >= _MAX_INFERRED_FILES:
                break

        # Repo-aware filter (if repo_dir provided, remove paths that don't exist)
        if repo_dir and result:
            result = self._filter_existing(result, repo_dir)

        return result

    def _infer_related_dirs(self, task: Task, repo_dir: str = "") -> list[str]:
        """
        Infer sibling shared/common directories from target_files.
        Repo-aware: only keep directories that actually exist.
        """
        if not task.target_files:
            return []

        candidates: set[str] = set()
        parent_dirs: set[str] = set()

        for tf in task.target_files:
            parts = tf.rstrip("/").split("/")
            # Collect parent directories at various levels
            if len(parts) > 1:
                parent_dirs.add("/".join(parts[:-1]))
            if len(parts) > 2:
                parent_dirs.add("/".join(parts[:-2]))

        # Add sibling shared/common under each parent
        for pdir in parent_dirs:
            for sd in _SIBLING_SHARED_DIRS:
                candidates.add(f"{pdir}/{sd}")

        # Repo-aware existence filtering (refinement #3)
        if repo_dir:
            candidates = {d for d in candidates if os.path.isdir(os.path.join(repo_dir, d))}

        return sorted(candidates)

    @staticmethod
    def _extract_paths_from_text(text: str) -> list[str]:
        """Extract file paths and directory paths from text (Layer 4+6)."""
        paths: list[str] = []

        # File paths (e.g., app/sales/models.py)
        for match in _FILE_PATH_RE.findall(text):
            # Filter out version numbers, URLs, common false positives
            if '/' in match and not match.startswith('http') and not match.startswith('v0.'):
                paths.append(match)

        # Directory paths (e.g., app/sales/)
        for match in _DIR_PATH_RE.findall(text):
            if not match.startswith('http'):
                paths.append(match.rstrip("/"))

        return paths

    @staticmethod
    def _module_to_dirs(module: str) -> list[str]:
        """
        Generate candidate directory paths from a module name (Layer 5 raw candidates).
        Generic: works across project types.
        """
        if not module or module == "core":
            return []

        mod_lower = module.lower().strip()
        # Common project layouts
        candidates = [
            f"app/{mod_lower}",
            f"apps/{mod_lower}",
            f"src/{mod_lower}",
            f"src/modules/{mod_lower}",
            f"packages/{mod_lower}",
            f"lib/{mod_lower}",
            mod_lower,
        ]
        return candidates

    @staticmethod
    def _filter_existing(paths: list[str], repo_dir: str) -> list[str]:
        """Repo-aware: only keep paths/dirs that actually exist."""
        result = []
        for p in paths:
            full = os.path.join(repo_dir, p)
            if os.path.exists(full):
                result.append(p)
        return result

    # ── task_type inference ──

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
