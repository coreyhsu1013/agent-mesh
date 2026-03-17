"""
Agent Mesh v1.3 — Gap Analyzer
Converts a VerifyReport into a plan.json that can be directly
executed by the dispatcher.

Flow:
  VerifyReport → file-based clustering (union-find)
  → schema task first → clustered logic tasks → output plan.json

Clustering:
  Gaps touching the same file are merged into one task (union-find).
  This prevents parallel agents from overwriting each other's changes.

Phases:
  0: Mechanical fixes (conflicts, build, test)
  1+2: File-based clustering — schema first, then logic clusters
  3: Lint fixes
  4: Spec feedback tasks (Layer 3 — spec corrections)
  5: Integration fix tasks (Layer 4 — cross-module)
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from .verifier import VerifyReport, VerifyIssue
from .task_normalizer import TaskNormalizer

logger = logging.getLogger("agent-mesh")

# File path pattern for extracting paths from gap messages
# Matches: src/foo/bar.py, apps/api/src/services/contract.ts, packages/database/prisma/schema.prisma
FILE_PATH_PATTERN = re.compile(r'(?:^|[\s,;:(])([a-zA-Z][\w./\-]*\.\w{1,10})(?:[\s,;:)]|$)')

# Issue categories that indicate schema changes (must run first)
SCHEMA_KEYWORDS = ["model ", "field ", "prisma", "schema", " enum ", " fk ", "fk →", "fk )", "missing field"]


class GapAnalyzer:
    """Analyzes verification report and generates executable plan.json."""

    def __init__(self, config: dict, repo_dir: str = ""):
        self.config = config
        self._repo_dir_hint = repo_dir  # for existence checks on legacy artifact paths
        verify_cfg = config.get("verify", {})
        self.exclude_modules: list[str] = verify_cfg.get("exclude_modules", [])
        self.fix_cycle: int = 0
        self.chunk_id: str = ""  # set by project_loop for unique task IDs

    def generate_fix_plan(self, report: VerifyReport) -> dict:
        """
        Generate a plan.json from a verify report.
        Returns TaskPlan-compatible dict that dispatcher can execute.
        """
        if report.passed:
            return {
                "project_name": "fix-cycle",
                "shared_context": {"cycle": report.cycle, "status": "PASSED"},
                "tasks": [],
            }

        # v2.2: Filter verify_false_positive (never produce fix tasks for phantom paths)
        false_positives = [i for i in report.issues if i.category == "verify_false_positive"]
        if false_positives:
            report.issues = [i for i in report.issues if i.category != "verify_false_positive"]
            logger.info(
                f"[GapAnalyzer] Filtered {len(false_positives)} verify_false_positive issues"
            )

        # v2.2: legacy_artifact_mismatch — MUST NOT produce fix tasks against stale paths.
        # These have already been rewritten to canonical runtime paths by verifier.
        # Treat them as regular spec_gap for fix-plan generation.
        for issue in report.issues:
            if issue.category == "legacy_artifact_mismatch":
                # Safety: ensure file is the resolved canonical path (not stale)
                if not issue.file or not os.path.exists(os.path.join(self._repo_dir_hint, issue.file)):
                    # No trustworthy canonical path — exclude
                    issue.category = "verify_false_positive"
                else:
                    # Rewrite to spec_gap so downstream clustering picks it up
                    issue.category = "spec_gap"

        # Re-filter after legacy rewrite
        report.issues = [i for i in report.issues if i.category != "verify_false_positive"]

        # Safety net: filter out excluded modules before generating tasks
        if self.exclude_modules:
            before_count = len(report.issues)
            report.issues = [
                i for i in report.issues
                if not self._is_excluded_module(i)
            ]
            filtered = before_count - len(report.issues)
            if filtered > 0:
                logger.info(
                    f"[GapAnalyzer] Filtered {filtered} issues from excluded modules: "
                    f"{self.exclude_modules}"
                )

        all_tasks = []
        task_counter = 0

        # Unique prefix: include chunk_id to avoid ID collision across chunks
        # "chunk-3-contract-backend" → "c3"
        chunk_prefix = ""
        if self.chunk_id:
            parts = self.chunk_id.split("-")
            if len(parts) >= 2:
                chunk_prefix = f"c{parts[1]}-"

        def _tid() -> str:
            nonlocal task_counter
            task_counter += 1
            return f"fix-{chunk_prefix}{report.cycle}-{task_counter}"

        # ── Phase 0: Mechanical fixes (conflicts, build errors) ──
        conflict_issues = [i for i in report.issues if i.category == "conflict"]
        build_issues = [i for i in report.issues if i.category == "build"]
        test_issues = [i for i in report.issues if i.category == "test"]

        phase0_ids = []

        if conflict_issues:
            tid = _tid()
            phase0_ids.append(tid)
            all_tasks.append(self._make_task(
                id=tid,
                title="Resolve merge conflict markers",
                description=self._build_conflict_desc(conflict_issues),
                complexity="L",
                module="infrastructure",
                target_files=[i.file for i in conflict_issues if i.file],
                depends_on=[],
            ))

        if build_issues:
            tid = _tid()
            phase0_ids.append(tid)
            all_tasks.append(self._make_task(
                id=tid,
                title="Fix build errors",
                description=self._build_error_desc(build_issues),
                complexity="M",
                module="infrastructure",
                depends_on=[t for t in phase0_ids if t != tid],
            ))

        if test_issues:
            tid = _tid()
            phase0_ids.append(tid)
            all_tasks.append(self._make_task(
                id=tid,
                title="Fix test failures",
                description=self._build_test_desc(test_issues),
                complexity="M",
                module="infrastructure",
                depends_on=[t for t in phase0_ids if t != tid],
            ))

        # ── Phase 1+2: File-based clustering ──
        # Gaps that touch the same file MUST be in the same task (prevent parallel conflicts).
        # Uses union-find: if gap A touches file X and gap B also touches file X → same cluster.
        spec_issues = [i for i in report.issues if i.category == "spec_gap"]

        # Separate schema vs logic issues
        all_schema_issues = [i for i in spec_issues if self._is_schema_issue(i)]
        logic_issues = [i for i in spec_issues if not self._is_schema_issue(i)]

        schema_task_ids = []
        if all_schema_issues:
            tid = _tid()
            schema_task_ids.append(tid)
            all_tasks.append(self._make_task(
                id=tid,
                title="Schema migration: all modules",
                description=self._build_spec_desc("All Modules — Schema Changes", all_schema_issues),
                complexity="H",
                module="database",
                depends_on=phase0_ids.copy(),
                acceptance_criteria="Schema changes applied; build passes",
            ))

        # Cluster logic issues by file overlap (union-find)
        clusters = self._cluster_by_files(logic_issues)
        logic_tasks_pending = []
        all_schema_deps = phase0_ids + schema_task_ids

        for cluster in clusters:
            tid = _tid()

            # Derive module name from cluster issues
            mod_names = list(dict.fromkeys(i.module or "General" for i in cluster))
            primary_mod = mod_names[0]
            short_mod = self._short_module_name(primary_mod)

            has_incorrect = any("INCORRECT" in i.message for i in cluster)
            complexity = "H" if has_incorrect else "M"
            target_files = self._guess_target_files(short_mod, cluster)

            title = f"Fix: {short_mod} ({len(cluster)} gaps)"

            logic_tasks_pending.append(self._make_task(
                id=tid,
                title=title,
                description=self._build_spec_desc(primary_mod, cluster),
                complexity=complexity,
                module=short_mod,
                target_files=target_files,
                depends_on=all_schema_deps.copy(),
                acceptance_criteria=self._build_ac(cluster),
            ))

        all_tasks.extend(logic_tasks_pending)

        logger.info(
            f"[GapAnalyzer] File clustering: {len(logic_issues)} gaps → {len(clusters)} task(s)"
        )

        # ── Phase 3: Lint fixes (lowest priority, parallel) ──
        lint_issues = [i for i in report.issues if i.category == "lint"]
        if lint_issues:
            all_tasks.append(self._make_task(
                id=_tid(),
                title="Fix lint warnings",
                description=self._build_lint_desc(lint_issues),
                complexity="L",
                module="infrastructure",
                depends_on=[],
            ))

        # ── Phase 4: Spec feedback tasks (Layer 3) ──
        spec_feedback_issues = [i for i in report.issues if i.category == "spec_feedback"]
        if spec_feedback_issues:
            for idx, issue in enumerate(spec_feedback_issues):
                tid = _tid()
                all_tasks.append(self._make_task(
                    id=tid,
                    title=f"Spec correction: {(issue.message[:60])}",
                    description=(
                        f"The spec analysis identified a spec quality issue:\n\n"
                        f"{issue.message}\n\n"
                        f"Implement the code change according to the suggested correction above. "
                        f"The original spec may be ambiguous or contradictory in this area."
                    ),
                    complexity="H",  # spec corrections need strong reasoning (Sonnet/Opus)
                    module=issue.module or "spec",
                    depends_on=all_schema_deps.copy(),
                    acceptance_criteria="Code matches corrected spec interpretation; build passes",
                ))

        # ── Phase 5: Integration fix tasks (Layer 4) ──
        integration_issues = [i for i in report.issues if i.category == "integration"]
        if integration_issues:
            # Integration tasks depend on ALL other fix tasks
            all_prior_ids = [t["id"] for t in all_tasks]
            for idx, issue in enumerate(integration_issues):
                tid = _tid()
                all_tasks.append(self._make_task(
                    id=tid,
                    title=f"Integration: {(issue.message[:60])}",
                    description=(
                        f"Cross-module integration issue:\n\n"
                        f"{issue.message}\n\n"
                        f"Fix the integration issue between modules. "
                        f"Ensure types, API contracts, and imports are consistent."
                    ),
                    complexity="H",  # cross-module always hard
                    module=issue.module or "integration",
                    depends_on=all_prior_ids.copy(),
                    acceptance_criteria="Cross-module integration passes; build passes; no type errors",
                ))

        plan = {
            "project_name": f"fix-cycle-{report.cycle}",
            "shared_context": {
                "cycle": report.cycle,
                "original_issue_count": len(report.issues),
                "note": "Auto-generated fix plan from verify report. "
                        "Schema tasks must complete before logic tasks.",
            },
            "tasks": all_tasks,
        }

        # v2.1: normalize fix tasks (v2.2: with repo_dir for path inference)
        from ..models.task import Task
        normalizer = TaskNormalizer()
        task_objects = [Task.from_dict(t) for t in all_tasks]
        normalizer.normalize_plan(task_objects, chunk_id=self.chunk_id, repo_dir=self._repo_dir_hint)
        plan["tasks"] = [t.to_dict() for t in task_objects]

        logger.info(
            f"[GapAnalyzer] Generated plan.json: {len(all_tasks)} tasks "
            f"({len(schema_task_ids)} schema + {len(logic_tasks_pending)} logic) "
            f"from {len(report.issues)} issues (cycle {report.cycle})"
        )
        return plan

    def save_fix_plan(self, plan: dict, output_path: str) -> str:
        """Save plan to JSON file."""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)
        logger.info(f"[GapAnalyzer] Saved fix-plan to {output_path}")
        return output_path

    # ── Task Builder ──

    def _make_task(self, id: str, title: str, description: str,
                   complexity: str, module: str,
                   target_files: list[str] | None = None,
                   depends_on: list[str] | None = None,
                   acceptance_criteria: str = "") -> dict:
        """Build a task dict compatible with TaskPlan.from_dict()."""
        return {
            "id": id,
            "title": title,
            "description": description,
            "agent_type": "",  # auto-route
            "complexity": complexity,
            "module": module,
            "target_files": target_files or [],
            "dependencies": depends_on or [],
            "acceptance_criteria": acceptance_criteria,
            "priority": 1,
        }

    # ── Issue Classification ──

    def _is_excluded_module(self, issue: VerifyIssue) -> bool:
        """Check if issue belongs to an excluded module."""
        if not self.exclude_modules:
            return False
        issue_module = (issue.module or "").lower()
        issue_message = issue.message.lower()
        for excluded in self.exclude_modules:
            excluded_lower = excluded.lower()
            if excluded_lower in issue_module or excluded_lower in issue_message:
                return True
        return False

    def _is_schema_issue(self, issue: VerifyIssue) -> bool:
        """Check if issue is about data model / schema changes."""
        msg = issue.message.lower()
        return any(kw in msg for kw in SCHEMA_KEYWORDS)

    def _short_module_name(self, mod_name: str) -> str:
        """Extract short module name from full name."""
        # "Module 1: Auth & Identity" → "auth"
        # "Module 10 — Notification" → "notification"
        # "Contract Management" → "contract"
        lower = mod_name.lower()

        # Strip common prefixes: "Module N:", "Module N —", "Module N -"
        cleaned = re.sub(r'^module\s+\d+\s*[:\-—]+\s*', '', lower).strip()
        if cleaned:
            # Take first meaningful word (skip articles/prepositions)
            skip = {"the", "a", "an", "and", "or", "of", "for", "in", "on", "to"}
            for word in cleaned.split():
                word = re.sub(r'[^a-z0-9]', '', word)
                if word and word not in skip:
                    return word

        # Last fallback: take last word after colon or dash
        parts = re.split(r'[:\-—]', mod_name)
        if len(parts) > 1:
            last = parts[-1].strip().split()[0].lower()
            return re.sub(r'[^a-z0-9]', '', last) or "general"
        return "general"

    def _cluster_by_files(self, issues: list[VerifyIssue]) -> list[list[VerifyIssue]]:
        """
        Cluster issues by file overlap using union-find.
        Two gaps sharing any target file → same cluster.
        Gaps with no files → grouped by module (fallback).
        """
        if not issues:
            return []

        n = len(issues)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Extract files for each issue
        issue_files: list[set[str]] = []
        for issue in issues:
            files: set[str] = set()
            if issue.file:
                files.add(issue.file)
            for match in FILE_PATH_PATTERN.findall(issue.message):
                if '/' in match and not match.startswith('http'):
                    files.add(match)
            issue_files.append(files)

        # Union issues that share any file
        file_to_issue: dict[str, int] = {}
        for i, files in enumerate(issue_files):
            for f in files:
                if f in file_to_issue:
                    union(i, file_to_issue[f])
                else:
                    file_to_issue[f] = i

        # Issues with NO files: group by module
        for i in range(n):
            if not issue_files[i]:
                mod = issues[i].module or "General"
                # Find another issue with same module to union with
                for j in range(i):
                    if not issue_files[j] and (issues[j].module or "General") == mod:
                        union(i, j)
                        break

        # Collect clusters
        clusters: dict[int, list[int]] = {}
        for i in range(n):
            root = find(i)
            clusters.setdefault(root, []).append(i)

        return [[issues[i] for i in indices] for indices in clusters.values()]

    def _guess_target_files(self, module: str, issues: list[VerifyIssue]) -> list[str]:
        """Extract target files from gap messages. Project-agnostic."""
        files: set[str] = set()

        for issue in issues:
            # 1. Use issue.file if available
            if issue.file:
                files.add(issue.file)

            # 2. Extract file paths from message text
            for match in FILE_PATH_PATTERN.findall(issue.message):
                # Filter out version numbers, URLs, etc.
                if '/' in match and not match.startswith('http'):
                    files.add(match)

        # Return extracted paths (empty = aider will use auto mode)
        return list(files)[:8]

    # ── Description Builders ──

    def _build_conflict_desc(self, issues: list[VerifyIssue]) -> str:
        files = [i.file for i in issues if i.file]
        return (
            f"Resolve git conflict markers in {len(files)} files.\n"
            f"Files: {', '.join(files)}\n\n"
            "For each file:\n"
            "1. Find <<<<<<< / ======= / >>>>>>> markers\n"
            "2. Merge both sides (keep all valid code)\n"
            "3. Remove all conflict markers\n"
            "4. Ensure file compiles correctly"
        )

    def _build_error_desc(self, issues: list[VerifyIssue]) -> str:
        errors = [i.message for i in issues[:10]]
        return (
            f"Fix {len(issues)} build errors.\n\n"
            "Errors:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    def _build_test_desc(self, issues: list[VerifyIssue]) -> str:
        failures = [i.message for i in issues[:10]]
        return (
            f"Fix {len(issues)} test failures.\n\n"
            "Failures:\n" + "\n".join(f"  - {f}" for f in failures)
        )

    def _build_lint_desc(self, issues: list[VerifyIssue]) -> str:
        warnings = [i.message for i in issues[:10]]
        return (
            f"Fix {len(issues)} lint warnings.\n\n"
            "Warnings:\n" + "\n".join(f"  - {w}" for w in warnings)
        )

    def _build_spec_desc(self, mod_name: str, issues: list[VerifyIssue]) -> str:
        requirements = []
        for i in issues:
            confidence = "high confidence (both models)" if len(i.found_by) > 1 else "single model"
            requirements.append(f"  - [{i.severity}] {i.message} ({confidence})")

        return (
            f"Module: {mod_name}\n\n"
            f"Requirements to implement ({len(issues)}):\n"
            + "\n".join(requirements)
            + "\n\nImplement all requirements listed above. "
            "Ensure type safety and proper error handling."
        )

    def _build_ac(self, issues: list[VerifyIssue]) -> str:
        """Build acceptance criteria from issues."""
        criteria = []
        for i in issues:
            # Extract the core requirement
            msg = i.message
            if " — " in msg:
                parts = msg.split(" — ")
                if len(parts) >= 2:
                    criteria.append(parts[1].split(" — ")[0].strip())
                else:
                    criteria.append(msg)
            else:
                criteria.append(msg)

        return "; ".join(criteria[:5])
