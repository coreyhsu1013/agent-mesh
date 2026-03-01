"""
Agent Mesh v0.7 — Gap Analyzer
Converts a VerifyReport into a fix-plan.json that can be executed
by the dispatcher as a new set of tasks.

Flow:
  VerifyReport → group issues by module → generate fix tasks → fix-plan.json
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .verifier import VerifyReport, VerifyIssue

logger = logging.getLogger("agent-mesh")


@dataclass
class FixTask:
    """A task to fix issues found during verification."""
    id: str
    title: str
    description: str
    complexity: str  # L, M, H
    depends_on: list[str]
    issues: list[dict]  # Original issues this task addresses

    def to_plan_entry(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "complexity": self.complexity,
            "depends_on": self.depends_on,
        }


class GapAnalyzer:
    """Analyzes verification report and generates fix-plan."""

    def __init__(self, config: dict):
        self.config = config

    def generate_fix_plan(self, report: VerifyReport) -> dict:
        """
        Generate a fix-plan.json from a verify report.
        Groups related issues into fix tasks.
        """
        if report.passed:
            return {"tasks": [], "cycle": report.cycle, "status": "PASSED"}

        tasks: list[FixTask] = []
        task_counter = 0

        # Priority 1: Conflict markers (must fix first)
        conflict_issues = [i for i in report.issues if i.category == "conflict"]
        if conflict_issues:
            task_counter += 1
            tasks.append(FixTask(
                id=f"fix-{report.cycle}-{task_counter}",
                title="Resolve merge conflict markers",
                description=self._build_conflict_description(conflict_issues),
                complexity="L",
                depends_on=[],
                issues=[i.to_dict() for i in conflict_issues],
            ))

        # Priority 2: Build errors (must fix before tests can run)
        build_issues = [i for i in report.issues if i.category == "build"]
        if build_issues:
            task_counter += 1
            depends = [tasks[-1].id] if conflict_issues else []
            tasks.append(FixTask(
                id=f"fix-{report.cycle}-{task_counter}",
                title="Fix build errors",
                description=self._build_error_description(build_issues),
                complexity="M",
                depends_on=depends,
                issues=[i.to_dict() for i in build_issues],
            ))

        # Priority 3: Test failures
        test_issues = [i for i in report.issues if i.category == "test"]
        if test_issues:
            task_counter += 1
            depends = [t.id for t in tasks]  # after conflicts + build
            tasks.append(FixTask(
                id=f"fix-{report.cycle}-{task_counter}",
                title="Fix test failures",
                description=self._build_test_description(test_issues),
                complexity="M",
                depends_on=depends,
                issues=[i.to_dict() for i in test_issues],
            ))

        # Priority 4: Spec gaps — group by module
        spec_issues = [i for i in report.issues if i.category == "spec_gap"]
        if spec_issues:
            modules: dict[str, list[VerifyIssue]] = {}
            for issue in spec_issues:
                mod = issue.module or "General"
                modules.setdefault(mod, []).append(issue)

            # Mechanical fixes must complete first
            base_depends = [t.id for t in tasks]

            for mod_name, mod_issues in modules.items():
                task_counter += 1
                # Determine complexity by severity
                has_high = any(i.severity == "HIGH" for i in mod_issues)
                complexity = "H" if has_high else "M"

                tasks.append(FixTask(
                    id=f"fix-{report.cycle}-{task_counter}",
                    title=f"Implement missing: {mod_name}",
                    description=self._build_spec_gap_description(mod_name, mod_issues),
                    complexity=complexity,
                    depends_on=base_depends,
                    issues=[i.to_dict() for i in mod_issues],
                ))

        # Priority 5: Lint warnings (lowest priority)
        lint_issues = [i for i in report.issues if i.category == "lint"]
        if lint_issues:
            task_counter += 1
            tasks.append(FixTask(
                id=f"fix-{report.cycle}-{task_counter}",
                title="Fix lint warnings",
                description=self._build_lint_description(lint_issues),
                complexity="L",
                depends_on=[],  # can run in parallel
                issues=[i.to_dict() for i in lint_issues],
            ))

        plan = {
            "cycle": report.cycle,
            "status": "FIX_NEEDED",
            "issue_count": len(report.issues),
            "task_count": len(tasks),
            "tasks": [t.to_plan_entry() for t in tasks],
        }

        logger.info(
            f"[GapAnalyzer] Generated fix-plan: {len(tasks)} tasks "
            f"from {len(report.issues)} issues (cycle {report.cycle})"
        )
        return plan

    def save_fix_plan(self, plan: dict, output_path: str) -> str:
        """Save fix-plan to JSON file."""
        with open(output_path, 'w') as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)
        logger.info(f"[GapAnalyzer] Saved fix-plan to {output_path}")
        return output_path

    # ── Description Builders ──

    def _build_conflict_description(self, issues: list[VerifyIssue]) -> str:
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

    def _build_error_description(self, issues: list[VerifyIssue]) -> str:
        errors = [i.message for i in issues[:10]]
        return (
            f"Fix {len(issues)} build errors.\n\n"
            "Errors:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    def _build_test_description(self, issues: list[VerifyIssue]) -> str:
        failures = [i.message for i in issues[:10]]
        return (
            f"Fix {len(issues)} test failures.\n\n"
            "Failures:\n" + "\n".join(f"  - {f}" for f in failures)
        )

    def _build_spec_gap_description(self, module: str, issues: list[VerifyIssue]) -> str:
        requirements = []
        for i in issues:
            confidence = "high" if len(i.found_by) > 1 else "single-model"
            requirements.append(f"  - [{i.severity}] {i.message} ({confidence})")

        return (
            f"Module: {module}\n\n"
            f"Missing/incomplete requirements ({len(issues)}):\n"
            + "\n".join(requirements)
            + "\n\nImplement all missing requirements listed above."
        )

    def _build_lint_description(self, issues: list[VerifyIssue]) -> str:
        warnings = [i.message for i in issues[:10]]
        return (
            f"Fix {len(issues)} lint warnings.\n\n"
            "Warnings:\n" + "\n".join(f"  - {w}" for w in warnings)
        )
