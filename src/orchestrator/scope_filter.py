"""
Agent Mesh v2.1 — Scope Filter

Classifies verify issues into:
  - executable:       in-scope, send to gap_analyzer
  - out_of_scope:     deferred to later chunks
  - false_positives:  dropped (not in any spec section)
  - contradictions:   flagged for manual review
  - needs_manual:     saved for human

Replaces the inline scope filter in project_loop.py (lines 277-305).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from .verifier import VerifyReport, VerifyIssue

logger = logging.getLogger("agent-mesh")


@dataclass
class ScopeFilterResult:
    executable: list[VerifyIssue] = field(default_factory=list)
    out_of_scope: list[VerifyIssue] = field(default_factory=list)
    false_positives: list[VerifyIssue] = field(default_factory=list)
    contradictions: list[VerifyIssue] = field(default_factory=list)
    needs_manual: list[VerifyIssue] = field(default_factory=list)


class ScopeFilter:
    """Filters verify issues by chunk scope."""

    def __init__(self, config: dict, repo_dir: str):
        self.config = config
        self.repo_dir = repo_dir
        verify_cfg = config.get("verify", {})
        self.exclude_modules: list[str] = verify_cfg.get("exclude_modules", [])

    def filter(
        self,
        report: VerifyReport,
        chunk_id: str,
        spec_sections: list[str] | None = None,
    ) -> ScopeFilterResult:
        """
        Filter report issues by chunk scope.
        Non-spec_gap issues (build, conflict, test, lint) are always executable.
        """
        result = ScopeFilterResult()
        scope_modules = self._get_chunk_scope_modules(chunk_id)

        if not scope_modules:
            # No scope info — treat all as executable
            result.executable = list(report.issues)
            return result

        for issue in report.issues:
            # Non-gap issues always pass through
            if issue.category != "spec_gap":
                result.executable.append(issue)
                continue

            classification = self._classify_issue(issue, scope_modules, spec_sections)
            if classification == "executable":
                result.executable.append(issue)
            elif classification == "out_of_scope":
                result.out_of_scope.append(issue)
            elif classification == "false_positive":
                result.false_positives.append(issue)
            elif classification == "contradiction":
                result.contradictions.append(issue)
            else:
                result.needs_manual.append(issue)

        logger.info(
            f"[ScopeFilter] chunk={chunk_id}: "
            f"executable={len(result.executable)}, "
            f"deferred={len(result.out_of_scope)}, "
            f"false_positives={len(result.false_positives)}, "
            f"contradictions={len(result.contradictions)}"
        )

        return result

    def _classify_issue(
        self,
        issue: VerifyIssue,
        scope_modules: set[str],
        spec_sections: list[str] | None,
    ) -> str:
        """Classify a single spec_gap issue."""
        # Check if issue belongs to an excluded module
        if self._is_excluded(issue):
            return "false_positive"

        # Check if issue is in chunk scope
        if self._issue_in_scope(issue, scope_modules):
            return "executable"

        # Out of scope
        return "out_of_scope"

    def _is_excluded(self, issue: VerifyIssue) -> bool:
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

    @staticmethod
    def _issue_in_scope(issue: VerifyIssue, scope_modules: set[str]) -> bool:
        """Check if an issue's module matches the current chunk scope."""
        if not issue.module:
            return True  # no module info → keep (conservative)
        mod_lower = issue.module.lower()
        for scope_mod in scope_modules:
            if scope_mod in mod_lower:
                return True
        return False

    def _get_chunk_scope_modules(self, chunk_id: str) -> set[str]:
        """
        Extract module scope from chunk_id and spec file.
        Extracted from project_loop._get_chunk_scope_modules.
        """
        modules: set[str] = set()

        # 1. From chunk_id: "chunk-4-notification-backend" → "notification"
        parts = chunk_id.split("-")[2:]  # skip "chunk" and number
        for part in parts:
            if part not in ("backend", "frontend", "api", "schema", "dependent",
                            "foundation", "and"):
                modules.add(part.lower())

        # 2. From spec title + scope lines
        spec_path = os.path.join(
            self.repo_dir, ".agent-mesh", f"{chunk_id}-spec.md"
        )
        if os.path.exists(spec_path):
            try:
                with open(spec_path) as f:
                    header = f.read(500)
                # Extract module names from "# Chunk N: Module Name" or "Scope: ..."
                for match in re.findall(r'(?:scope|module|chunk)[:\s]+([^\n]+)', header, re.I):
                    for word in match.split(","):
                        word = word.strip().lower()
                        word = re.sub(r'[^a-z0-9]', '', word)
                        if word and len(word) > 2:
                            modules.add(word)
            except Exception:
                pass

        return modules
