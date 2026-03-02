"""
Agent Mesh v0.7 — Gap Analyzer
Converts a VerifyReport into a plan.json that can be directly
executed by the dispatcher.

Flow:
  VerifyReport → group issues by module → split large groups
  → add dependencies (schema first) → output plan.json

Key improvements over v1:
  - Outputs TaskPlan-compatible format (not raw fix-plan)
  - Splits large modules (>5 issues) into sub-tasks
  - Adds proper dependencies (Prisma schema → API routes → tests)
  - Includes target_files hints based on module structure
  - Sets complexity based on issue severity
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from .verifier import VerifyReport, VerifyIssue

logger = logging.getLogger("agent-mesh")

# Known module → file path mappings for common project structures
MODULE_FILE_HINTS = {
    "auth": ["apps/api/src/services/", "apps/api/src/routes/", "packages/database/prisma/schema.prisma"],
    "groupbuy": ["apps/api/src/services/", "apps/api/src/routes/", "packages/database/prisma/schema.prisma"],
    "yaopoint": ["apps/api/src/services/", "apps/api/src/routes/", "packages/database/prisma/schema.prisma"],
    "leader": ["apps/api/src/services/", "apps/api/src/routes/", "apps/leader/"],
    "contract": ["packages/contracts/contracts/", "packages/contracts/test/"],
    "admin": ["apps/admin/", "apps/api/src/routes/", "apps/api/src/services/"],
    "erp": ["packages/erp-connector/", "apps/api/src/routes/", "apps/api/src/workers/"],
    "ui": ["packages/ui/"],
}

# Issue categories that indicate schema changes (must run first)
SCHEMA_KEYWORDS = ["model ", "field ", "prisma", "schema", " enum ", " fk ", "fk →", "fk )", "missing field"]


class GapAnalyzer:
    """Analyzes verification report and generates executable plan.json."""

    def __init__(self, config: dict):
        self.config = config
        self.max_issues_per_task = 5  # split if more

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

        all_tasks = []
        task_counter = 0

        # ── Phase 0: Mechanical fixes (conflicts, build errors) ──
        conflict_issues = [i for i in report.issues if i.category == "conflict"]
        build_issues = [i for i in report.issues if i.category == "build"]
        test_issues = [i for i in report.issues if i.category == "test"]

        phase0_ids = []

        if conflict_issues:
            task_counter += 1
            tid = f"fix-{report.cycle}-{task_counter}"
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
            task_counter += 1
            tid = f"fix-{report.cycle}-{task_counter}"
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
            task_counter += 1
            tid = f"fix-{report.cycle}-{task_counter}"
            phase0_ids.append(tid)
            all_tasks.append(self._make_task(
                id=tid,
                title="Fix test failures",
                description=self._build_test_desc(test_issues),
                complexity="M",
                module="infrastructure",
                depends_on=[t for t in phase0_ids if t != tid],
            ))

        # ── Phase 1: Group spec gaps by module ──
        spec_issues = [i for i in report.issues if i.category == "spec_gap"]
        modules: dict[str, list[VerifyIssue]] = {}
        for issue in spec_issues:
            mod = issue.module or "General"
            modules.setdefault(mod, []).append(issue)

        # ── Phase 2: Two-pass — ONE schema task, then parallel logic tasks ──
        # Pass 1: Collect ALL schema issues across modules into ONE task
        # (schema.prisma is a single file — parallel edits = guaranteed conflicts)
        all_schema_issues: list[VerifyIssue] = []
        logic_groups: list[tuple[str, list[VerifyIssue]]] = []

        for mod_name, mod_issues in modules.items():
            schema_issues = [i for i in mod_issues if self._is_schema_issue(i)]
            logic_issues = [i for i in mod_issues if not self._is_schema_issue(i)]

            all_schema_issues.extend(schema_issues)
            if logic_issues:
                logic_groups.append((mod_name, logic_issues))

        schema_task_ids = []
        if all_schema_issues:
            task_counter += 1
            tid = f"fix-{report.cycle}-{task_counter}"
            schema_task_ids.append(tid)
            all_tasks.append(self._make_task(
                id=tid,
                title="Schema migration: all modules",
                description=self._build_spec_desc("All Modules — Schema Changes", all_schema_issues),
                complexity="H",  # single critical-path task
                module="database",
                target_files=["packages/database/prisma/schema.prisma"],
                depends_on=phase0_ids.copy(),
                acceptance_criteria="npx prisma validate passes; npx prisma generate succeeds; pnpm build passes",
            ))

        # Pass 2: Create logic tasks (depend on schema task)
        logic_tasks_pending = []
        all_schema_deps = phase0_ids + schema_task_ids

        for mod_name, logic_issues in logic_groups:
            chunks = self._chunk_issues(logic_issues, self.max_issues_per_task)
            for chunk_idx, chunk in enumerate(chunks):
                task_counter += 1
                tid = f"fix-{report.cycle}-{task_counter}"
                short_mod = self._short_module_name(mod_name)
                suffix = f" ({chunk_idx + 1})" if len(chunks) > 1 else ""

                # ★ Fix tasks: M (Sonnet) by default
                # Only INCORRECT items need H (Opus) — wrong logic requires careful reasoning
                has_incorrect = any("INCORRECT" in i.message for i in chunk)
                complexity = "H" if has_incorrect else "M"
                target_files = self._guess_target_files(short_mod, chunk)

                logic_tasks_pending.append(self._make_task(
                    id=tid,
                    title=f"Implement: {short_mod}{suffix}",
                    description=self._build_spec_desc(mod_name, chunk),
                    complexity=complexity,
                    module=short_mod,
                    target_files=target_files,
                    depends_on=all_schema_deps.copy(),
                    acceptance_criteria=self._build_ac(chunk),
                ))

        all_tasks.extend(logic_tasks_pending)

        # ── Phase 3: Lint fixes (lowest priority, parallel) ──
        lint_issues = [i for i in report.issues if i.category == "lint"]
        if lint_issues:
            task_counter += 1
            all_tasks.append(self._make_task(
                id=f"fix-{report.cycle}-{task_counter}",
                title="Fix lint warnings",
                description=self._build_lint_desc(lint_issues),
                complexity="L",
                module="infrastructure",
                depends_on=[],
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

    def _is_schema_issue(self, issue: VerifyIssue) -> bool:
        """Check if issue is about data model / schema changes."""
        msg = issue.message.lower()
        return any(kw in msg for kw in SCHEMA_KEYWORDS)

    def _short_module_name(self, mod_name: str) -> str:
        """Extract short module name from full name."""
        # "Module 1: Auth & Identity" → "auth"
        # "Module 2: GroupBuy Engine" → "groupbuy"
        lower = mod_name.lower()
        for key in MODULE_FILE_HINTS:
            if key in lower:
                return key
        # Fallback: take last word
        parts = mod_name.split(":")
        if len(parts) > 1:
            return parts[-1].strip().split()[0].lower()
        return mod_name.lower().replace(" ", "-")[:20]

    def _guess_target_files(self, module: str, issues: list[VerifyIssue]) -> list[str]:
        """Guess target files based on module and issue content."""
        files = MODULE_FILE_HINTS.get(module, []).copy()

        # Add hints from issue messages
        for issue in issues:
            msg = issue.message.lower()
            if "route" in msg or "api" in msg or "endpoint" in msg:
                files.append(f"apps/api/src/routes/{module}.ts")
            if "service" in msg or "logic" in msg:
                files.append(f"apps/api/src/services/{module}.ts")
            if "worker" in msg or "cron" in msg:
                files.append("apps/api/src/workers/")
            if "contract" in msg or "solidity" in msg:
                files.append("packages/contracts/contracts/")

        return list(set(files))[:5]  # deduplicate, cap at 5

    def _chunk_issues(self, issues: list[VerifyIssue], max_size: int) -> list[list[VerifyIssue]]:
        """Split issues into chunks of max_size."""
        if len(issues) <= max_size:
            return [issues]
        return [issues[i:i + max_size] for i in range(0, len(issues), max_size)]

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
