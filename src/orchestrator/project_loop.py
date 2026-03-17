"""
Agent Mesh v0.8 — Project-level ReAct Loop

Outer loop that orchestrates the full cycle:
  Spec → Plan → Execute → Verify → Fix-Plan → Execute → Verify → ... → Done

Four-layer architecture:
  Layer 1 (ReAct):     task → escalate → complete           ← 確保跑得完
  Layer 2 (ProjectLoop): plan → execute → verify → fix      ← 確保跑得好
  Layer 3 (SpecFeedback): stuck gaps → analyze → spec fix   ← 確保寫得對
  Layer 4 (Integration): cross-module → contract check      ← 確保用得起來

v0.7.5: Model ranking & outer-loop escalation
v0.8:   Layer 3 (spec feedback) + Layer 4 (integration validation)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from .verifier import Verifier, VerifyReport, VerifyIssue
from .gap_analyzer import GapAnalyzer
from .model_ranking import OuterLoopEscalation
from .retrospective import RetrospectiveAnalyzer
from .run_history import RunHistoryRecorder

logger = logging.getLogger("agent-mesh")


class ProjectLoop:
    """
    Project-level ReAct loop controller.
    
    Usage:
        loop = ProjectLoop(config, repo_dir, spec_path)
        
        # Auto mode: run cycles until convergence
        await loop.run_auto(max_cycles=5, dispatcher_factory=make_dispatcher)
        
        # Manual mode: verify only
        report = await loop.verify()
        
        # Manual mode: verify + generate fix plan
        plan = await loop.verify_and_plan()
    """

    def __init__(self, config: dict, repo_dir: str, spec_path: str | None = None,
                 run_history: RunHistoryRecorder | None = None):
        self.config = config
        self.repo_dir = repo_dir
        self.spec_path = spec_path
        self.verifier = Verifier(repo_dir, config)
        self.gap_analyzer = GapAnalyzer(config)
        self.escalation = OuterLoopEscalation(config)
        self.retrospective = RetrospectiveAnalyzer(config, repo_dir)
        self.cycle_history: list[dict] = []
        # v1.3: structured run history (shared from DesignLoop if provided)
        self.run_history = run_history or RunHistoryRecorder(repo_dir)
        # v1.2: Layer 3 per-gap cache (avoid re-analyzing same gap)
        self._spec_feedback_cache: dict[str, str] = {}  # gap_key → root_cause
        # v1.2: event log for monitoring
        self._events_path = os.path.join(repo_dir, ".agent-mesh", "events.log")
        self._discord_webhook = config.get("notifications", {}).get("discord_webhook", "")

    def _emit_event(self, event: str):
        """Append a timestamped event to events.log + Discord webhook."""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            with open(self._events_path, 'a') as f:
                f.write(f"[{ts}] {event}\n")
        except Exception:
            pass
        if self._discord_webhook:
            self._discord_send(f"[{ts}] {event}")

    def _discord_send(self, message: str):
        """Fire-and-forget Discord webhook notification."""
        import threading
        def _send():
            try:
                import subprocess, json as _json
                payload = _json.dumps({"content": message})
                subprocess.run(
                    ["curl", "-s", "-H", "Content-Type: application/json",
                     "-d", payload, self._discord_webhook],
                    timeout=10, capture_output=True,
                )
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()

    async def verify(self, cycle: int = 1) -> VerifyReport:
        """Run verification only."""
        logger.info(f"\n{'='*60}")
        logger.info(f"  🔍 Verify Cycle {cycle}")
        logger.info(f"{'='*60}")

        report = await self.verifier.run(
            cycle=cycle,
            spec_path=self.spec_path,
        )

        logger.info(f"\n{report.summary()}")

        # Save report
        report_path = os.path.join(self.repo_dir, f".agent-mesh/verify-report-{cycle}.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, 'w') as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

        return report

    async def verify_and_plan(self, cycle: int = 1) -> tuple[VerifyReport, dict | None]:
        """Run verification and generate fix-plan if needed."""
        report = await self.verify(cycle)

        if report.passed:
            logger.info("[ProjectLoop] ✅ All checks passed — no fix-plan needed")
            return report, None

        # Generate fix-plan
        # v1.3: pass cycle + chunk_id to gap_analyzer for unique fix task IDs
        self.gap_analyzer.fix_cycle = cycle
        self.gap_analyzer.chunk_id = self.config.get("current_chunk_id", "")
        plan = self.gap_analyzer.generate_fix_plan(report)
        plan_path = os.path.join(self.repo_dir, f".agent-mesh/fix-plan-{cycle}.json")
        self.gap_analyzer.save_fix_plan(plan, plan_path)

        logger.info(
            f"[ProjectLoop] 📋 Fix-plan generated: {len(plan['tasks'])} tasks "
            f"→ {plan_path}"
        )
        return report, plan

    async def verify_closed_loop(self, cycle: int) -> tuple[VerifyReport, dict | None]:
        """
        Multi-layer closed-loop verification.
        Step 0: Mechanical checks (build, test, lint)
        Step 1: Regression check — are previous gaps fixed?
        Step 2: Bounded scan — any NEW critical gaps? (capped)
        Step 2.5: Layer 3 — Spec feedback for stuck gaps
        Step 3: Convergence check
        Step 3.5: Layer 4 — Integration validation
        Step 4: Generate fix-plan
        """
        verify_cfg = self.config.get("verify", {})
        exclude_modules = verify_cfg.get("exclude_modules", [])
        max_new = verify_cfg.get("max_new_gaps_per_cycle", 5)
        convergence_threshold = verify_cfg.get("convergence_threshold", 0)

        logger.info(f"\n{'='*60}")
        logger.info(f"  🔍 Verify Cycle {cycle} (closed-loop)")
        logger.info(f"{'='*60}")

        # Step 0: Mechanical checks only (no LLM spec diff)
        report = await self.verifier.run_mechanical(cycle)
        logger.info(
            f"[ClosedLoop] Mechanical: build={'✅' if report.build_ok else '❌'} "
            f"test={'✅' if report.test_ok else '❌'} lint={'✅' if report.lint_ok else '❌'}"
        )

        # Step 0.5: Load deferred gaps from previous chunks (if any)
        chunk_id = self.config.get("current_chunk_id", "")
        deferred_for_us = []
        if chunk_id and cycle == 1:
            deferred_for_us = self._load_deferred_gaps(chunk_id)

        # Step 1: Regression check (if previous report exists)
        remaining_gaps = []
        prev_gaps = self._load_prev_gaps(cycle)
        # Merge deferred gaps into prev_gaps for regression
        if deferred_for_us:
            prev_gaps.extend(deferred_for_us)
            logger.info(
                f"[ScopeFilter] Added {len(deferred_for_us)} deferred gaps "
                f"from previous chunks to regression check"
            )
        code_tree = await self.verifier._get_code_tree()

        if prev_gaps:
            remaining_gaps = await self.verifier.run_regression(
                prev_gaps, self.spec_path, code_tree
            )
            logger.info(
                f"[ClosedLoop] Regression: {len(prev_gaps)} previous → "
                f"{len(remaining_gaps)} remaining"
            )
        else:
            logger.info("[ClosedLoop] No previous gaps to regress against")

        # Step 2: Bounded new gap scan (pass remaining_gaps so it won't re-report)
        new_gap_issues = []
        if report.build_ok:  # only scan if build passes
            new_gap_issues = await self.verifier.run_bounded_scan(
                self.spec_path, code_tree, exclude_modules, max_new,
                known_gaps=remaining_gaps,
            )
            logger.info(f"[ClosedLoop] New scan: {len(new_gap_issues)} new gaps (max {max_new})")
        else:
            logger.info("[ClosedLoop] Skipping new gap scan — build failed")

        # Step 2.5: Layer 3 — Spec feedback for stuck gaps (v1.2: per-gap cache)
        layer3_cfg = self.config.get("layer3", {})
        if layer3_cfg.get("enabled", False) and remaining_gaps:
            stuck_threshold = layer3_cfg.get("stuck_threshold", 2)
            stuck_gaps = self._find_stuck_gaps(remaining_gaps, stuck_threshold)

            # v1.2: filter out gaps already analyzed as CODE_BUG (won't help)
            uncached_stuck = [
                g for g in stuck_gaps
                if self._gap_key(g) not in self._spec_feedback_cache
            ]
            cached_count = len(stuck_gaps) - len(uncached_stuck)
            if cached_count > 0:
                logger.info(
                    f"[Layer3] {cached_count} stuck gaps already analyzed (cached), "
                    f"{len(uncached_stuck)} new to analyze"
                )

            if uncached_stuck:
                logger.info(
                    f"[Layer3] {len(uncached_stuck)} gaps stuck for >= {stuck_threshold} cycles, "
                    f"analyzing root cause..."
                )
                feedback_issues = await self.verifier.run_spec_feedback(
                    uncached_stuck, self.spec_path, code_tree
                )
                # Cache results: gaps not in feedback_issues are CODE_BUG
                for g in uncached_stuck:
                    self._spec_feedback_cache[self._gap_key(g)] = "CODE_BUG"
                for issue in feedback_issues:
                    # Spec issues get different cache status
                    key = (issue.module or "").lower() + "|" + issue.message.lower()
                    self._spec_feedback_cache[key] = issue.category
                spec_questions = []
                for issue in feedback_issues:
                    if issue.category == "spec_question":
                        spec_questions.append(issue)
                        logger.warning(f"  ❓ SPEC QUESTION: {issue.message}")
                    else:
                        new_gap_issues.append(issue)

                # Save spec questions to file for human review
                if spec_questions:
                    self._save_spec_questions(cycle, spec_questions)

        # Add remaining gaps back to report as VerifyIssues
        # Use module+message key to dedup against new_gap_issues
        seen_keys: set[str] = set()

        for gap in remaining_gaps:
            key = (gap.get("module") or "").lower() + "|" + (gap.get("message") or "").lower()
            if key not in seen_keys:
                seen_keys.add(key)
                report.issues.append(VerifyIssue(
                    category=gap.get("category", "spec_gap"),
                    severity=gap.get("severity", "MEDIUM"),
                    message=gap.get("message", ""),
                    module=gap.get("module"),
                    found_by=gap.get("found_by", ["regression"]),
                ))

        for issue in new_gap_issues:
            key = (issue.module or "").lower() + "|" + issue.message.lower()
            if key not in seen_keys:
                seen_keys.add(key)
                report.issues.append(issue)

        all_gap_count = len([i for i in report.issues if i.category == "spec_gap"])
        report.spec_gap_count = all_gap_count

        # Step 2.6: Scope filter — remove gaps not belonging to current chunk
        chunk_id = self.config.get("current_chunk_id", "")
        if chunk_id and all_gap_count > 0:
            scope_modules = self._get_chunk_scope_modules(chunk_id)
            if scope_modules:
                in_scope = []
                deferred = []
                for issue in report.issues:
                    if issue.category != "spec_gap":
                        in_scope.append(issue)
                        continue
                    if self._issue_in_scope(issue, scope_modules):
                        in_scope.append(issue)
                    else:
                        deferred.append(issue)

                if deferred:
                    logger.info(
                        f"[ScopeFilter] {len(deferred)} gaps out of chunk scope, "
                        f"deferring to later chunks"
                    )
                    for d in deferred:
                        logger.info(f"  ↳ deferred: [{d.module}] {d.message[:80]}")
                    self._save_deferred_gaps(deferred)
                    report.issues = in_scope
                    all_gap_count = len([
                        i for i in report.issues if i.category == "spec_gap"
                    ])
                    report.spec_gap_count = all_gap_count

        # Save report
        report_path = os.path.join(self.repo_dir, f".agent-mesh/verify-report-{cycle}.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, 'w') as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

        logger.info(f"\n{report.summary()}")

        # Step 2.75: Retrospective — when gaps are diverging
        retro_cfg = self.config.get("retrospective", {})
        if retro_cfg.get("enabled", True) and self._is_diverging(all_gap_count):
            logger.info(
                f"[Retro] Gaps diverging (cycle history: "
                f"{[e.get('gap_count', 0) for e in self.cycle_history]}), "
                f"running retrospective analysis..."
            )
            retro = await self.retrospective.analyze(
                remaining_gaps + [
                    {"message": i.message, "module": i.module, "file": i.file}
                    for i in new_gap_issues
                ],
                self.spec_path,
                code_tree,
                self.cycle_history,
            )

            if retro.diagnoses:
                # Handle SPEC_ISSUE: amend spec
                if retro.spec_amendments:
                    self._amend_spec(retro.spec_amendments)
                    logger.info(
                        f"[Retro] Amended spec with {len(retro.spec_amendments)} fixes"
                    )

                # Handle UNFIXABLE: remove from report
                unfixable_msgs = {
                    d.gap_message.lower()
                    for d in retro.diagnoses
                    if d.root_cause == "UNFIXABLE" and d.gap_message
                }
                if unfixable_msgs:
                    before = len(report.issues)
                    report.issues = [
                        i for i in report.issues
                        if i.message.lower() not in unfixable_msgs
                    ]
                    removed = before - len(report.issues)
                    if removed:
                        logger.warning(
                            f"[Retro] Removed {removed} unfixable gaps"
                        )
                        all_gap_count = len([
                            i for i in report.issues
                            if i.category == "spec_gap"
                        ])
                        report.spec_gap_count = all_gap_count

                # Handle FIXABLE: enrich description with root cause
                for diagnosis in retro.diagnoses:
                    if diagnosis.root_cause == "FIXABLE" and diagnosis.fix_strategy:
                        for issue in report.issues:
                            if (issue.message.lower()
                                    == diagnosis.gap_message.lower()):
                                issue.message = (
                                    f"{issue.message}\n"
                                    f"[ROOT CAUSE] {diagnosis.analysis}\n"
                                    f"[FIX STRATEGY] {diagnosis.fix_strategy}"
                                )
                                break

                self._emit_event(
                    f"RETRO cycle={cycle} "
                    f"fixable={retro.fixable_count} "
                    f"spec_issue={retro.spec_issue_count} "
                    f"unfixable={retro.unfixable_count}"
                )

        # Step 3: Convergence check
        total_gaps = all_gap_count
        is_last_chunk = self.config.get("is_last_chunk", False)
        defer_base = self.config.get("verify", {}).get("defer_remaining_threshold", 2)
        # Progressive tolerance: threshold increases each cycle (cycle 2=base, 3=base+1, ...)
        defer_threshold = defer_base + max(0, cycle - 2)

        if (not is_last_chunk
                and total_gaps > convergence_threshold
                and total_gaps <= defer_threshold
                and cycle >= 2
                and report.build_ok):
            # Defer remaining gaps to next chunk instead of cycling
            remaining_issues = [
                i for i in report.issues if i.category == "spec_gap"
            ]
            self._save_deferred_gaps(remaining_issues)
            logger.info(
                f"✅ Deferred: {total_gaps} gaps <= defer threshold {defer_threshold} "
                f"(base={defer_base}, cycle={cycle}), passing to next chunk"
            )
            report.issues = []  # mark as passed
            return report, None

        if total_gaps <= convergence_threshold and report.build_ok:
            # Step 3.5: Layer 4 — Integration validation (only when spec gaps converged)
            layer4_cfg = self.config.get("layer4", {})
            if (layer4_cfg.get("enabled", False)
                    and cycle >= layer4_cfg.get("run_after_cycle", 2)):
                logger.info("[Layer4] Spec gaps converged, running integration check...")
                integration_issues = await self._run_layer4(layer4_cfg, code_tree)

                if integration_issues:
                    logger.info(
                        f"[Layer4] {len(integration_issues)} integration issues — "
                        f"not converged yet"
                    )
                    for issue in integration_issues:
                        report.issues.append(issue)
                    # Don't mark as passed — need integration fixes
                else:
                    logger.info("[Layer4] ✅ Integration check passed")
                    report.issues = []  # mark as passed
                    return report, None
            else:
                logger.info(
                    f"✅ Converged: {total_gaps} gaps <= threshold {convergence_threshold}"
                )
                report.issues = []  # mark as passed
                return report, None

        # Step 4: Generate fix-plan from ONLY remaining + new (not full rescan)
        if report.passed:
            return report, None

        # v1.3: pass cycle + chunk_id to gap_analyzer for unique fix task IDs
        self.gap_analyzer.fix_cycle = cycle
        self.gap_analyzer.chunk_id = self.config.get("current_chunk_id", "")
        plan = self.gap_analyzer.generate_fix_plan(report)
        plan_path = os.path.join(self.repo_dir, f".agent-mesh/fix-plan-{cycle}.json")
        self.gap_analyzer.save_fix_plan(plan, plan_path)

        logger.info(
            f"[ClosedLoop] Fix-plan generated: {len(plan['tasks'])} tasks → {plan_path}"
        )
        return report, plan

    @staticmethod
    def _gap_key(gap: dict) -> str:
        """Stable key for gap dedup and caching."""
        return (gap.get("module") or "").lower() + "|" + (gap.get("message") or "").lower()

    def _find_stuck_gaps(self, remaining_gaps: list[dict], threshold: int) -> list[dict]:
        """Find gaps that persisted for >= threshold consecutive cycles."""
        if len(self.cycle_history) < threshold - 1:
            return []  # not enough history yet

        stuck = []
        for gap in remaining_gaps:
            gap_key = (
                (gap.get("module") or "").lower() + "|"
                + (gap.get("message") or "").lower()
            )
            appearances = 0
            for entry in reversed(self.cycle_history):
                report = entry.get("report")
                if not report:
                    break
                found = any(
                    (i.module or "").lower() + "|" + i.message.lower() == gap_key
                    for i in report.issues if i.category == "spec_gap"
                )
                if found:
                    appearances += 1
                else:
                    break
            # +1 for current cycle (remaining_gaps = this cycle's regression result)
            appearances += 1
            if appearances >= threshold:
                gap_copy = dict(gap)
                gap_copy["stuck_cycles"] = appearances
                stuck.append(gap_copy)
        return stuck

    def _save_spec_questions(self, cycle: int, questions: list[VerifyIssue]):
        """Save spec questions to JSON for human review."""
        questions_path = os.path.join(
            self.repo_dir, f".agent-mesh/spec-questions-{cycle}.json"
        )
        os.makedirs(os.path.dirname(questions_path), exist_ok=True)
        data = [
            {
                "module": q.module,
                "message": q.message,
                "severity": q.severity,
                "found_by": q.found_by,
            }
            for q in questions
        ]
        with open(questions_path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(
            f"[Layer3] Saved {len(questions)} spec questions → {questions_path}"
        )

    def _get_chunk_scope_modules(self, chunk_id: str) -> set[str]:
        """
        Extract module scope from chunk's spec title line and chunk_id.
        Only uses the FIRST line / Scope declaration to avoid picking up
        context-only module references.
        """
        import re
        modules = set()

        # 1. From chunk_id: "chunk-4-notification-backend" → "notification"
        parts = chunk_id.split("-")[2:]  # skip "chunk" and number
        for part in parts:
            if part not in ("backend", "frontend", "api", "schema", "dependent",
                            "foundation", "and"):
                modules.add(part.lower())

        # 2. From spec title + scope lines (first 500 chars only)
        spec_path = os.path.join(
            self.repo_dir, ".agent-mesh", f"{chunk_id}-spec.md"
        )
        if os.path.exists(spec_path):
            try:
                with open(spec_path) as f:
                    header = f.read(500)
                # Only match "Module N" in title (# line) or "Scope:" line
                for line in header.split("\n")[:10]:
                    if line.startswith("#") or line.lower().startswith("> scope"):
                        for match in re.finditer(
                            r'module\s+(\d+)', line, re.IGNORECASE
                        ):
                            modules.add(f"module {match.group(1)}")
            except Exception:
                pass

        return modules

    @staticmethod
    def _issue_in_scope(issue: VerifyIssue, scope_modules: set[str]) -> bool:
        """Check if an issue's module matches the current chunk scope."""
        if not issue.module:
            return True  # no module info → keep it (conservative)

        mod_lower = issue.module.lower()
        for scope_mod in scope_modules:
            if scope_mod in mod_lower:
                return True
        return False

    def _save_deferred_gaps(self, gaps: list[VerifyIssue]):
        """Save out-of-scope gaps to deferred-gaps.json for later chunks."""
        mesh_dir = os.path.join(self.repo_dir, ".agent-mesh")
        path = os.path.join(mesh_dir, "deferred-gaps.json")

        existing = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        # Dedup by module+message
        existing_keys = {
            (g.get("module") or "").lower() + "|" + (g.get("message") or "").lower()
            for g in existing
        }
        for gap in gaps:
            key = (gap.module or "").lower() + "|" + gap.message.lower()
            if key not in existing_keys:
                existing.append(gap.to_dict())
                existing_keys.add(key)

        with open(path, 'w') as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        logger.info(f"[ScopeFilter] Deferred gaps saved: {len(existing)} total")

    def _load_deferred_gaps(self, chunk_id: str) -> list[dict]:
        """Load deferred gaps that belong to this chunk's scope."""
        path = os.path.join(self.repo_dir, ".agent-mesh", "deferred-gaps.json")
        if not os.path.exists(path):
            return []

        try:
            with open(path) as f:
                all_deferred = json.load(f)
        except Exception:
            return []

        if not all_deferred:
            return []

        scope_modules = self._get_chunk_scope_modules(chunk_id)
        if not scope_modules:
            return []

        # Pick gaps that match this chunk's scope
        matched = []
        remaining = []
        for gap in all_deferred:
            mod = (gap.get("module") or "").lower()
            in_scope = any(s in mod for s in scope_modules)
            if in_scope:
                matched.append(gap)
            else:
                remaining.append(gap)

        if matched:
            # Save back only unmatched gaps
            with open(path, 'w') as f:
                json.dump(remaining, f, indent=2, ensure_ascii=False)
            logger.info(
                f"[ScopeFilter] Loaded {len(matched)} deferred gaps for {chunk_id}"
            )

        return matched

    def _is_diverging(self, current_gap_count: int) -> bool:
        """Check if gaps are getting worse instead of converging."""
        if not self.cycle_history:
            return False
        prev = self.cycle_history[-1].get("gap_count", 0)
        return current_gap_count >= prev and current_gap_count > 0

    def _amend_spec(self, amendments: list[str]):
        """Append clarifications to the spec file."""
        if not self.spec_path or not amendments:
            return
        try:
            with open(self.spec_path, 'a') as f:
                f.write("\n\n## AUTO-AMENDMENTS (from retrospective analysis)\n")
                for amendment in amendments:
                    f.write(f"- {amendment}\n")
            logger.info(f"[Retro] Amended spec: {self.spec_path}")
        except Exception as e:
            logger.warning(f"[Retro] Failed to amend spec: {e}")

    async def _run_layer4(
        self, layer4_cfg: dict, code_tree: str
    ) -> list[VerifyIssue]:
        """Run Layer 4 integration checks."""
        issues = []

        # Mechanical typecheck (if configured)
        typecheck_cmd = layer4_cfg.get("typecheck_cmd", "")
        if typecheck_cmd:
            typecheck_ok, errors = await self.verifier._run_cmd(typecheck_cmd)
            if not typecheck_ok:
                issues.append(VerifyIssue(
                    category="integration",
                    severity="HIGH",
                    message=f"Typecheck failed: {'; '.join(errors[:5])}",
                    found_by=["layer4"],
                ))

        # LLM contract check
        if layer4_cfg.get("api_contract_check", True):
            contract_issues = await self.verifier.run_integration_check(
                self.spec_path, code_tree
            )
            issues.extend(contract_issues)

        return issues

    def _load_prev_gaps(self, cycle: int) -> list[dict]:
        """Load spec_gap issues from the previous cycle's verify report."""
        prev_cycle = cycle - 1
        if prev_cycle < 1:
            return []

        report_path = os.path.join(
            self.repo_dir, f".agent-mesh/verify-report-{prev_cycle}.json"
        )
        if not os.path.exists(report_path):
            logger.info(f"[ClosedLoop] No previous report at {report_path}")
            return []

        try:
            with open(report_path, 'r') as f:
                data = json.load(f)
            issues = data.get("issues", [])
            spec_gaps = [i for i in issues if i.get("category") == "spec_gap"]
            logger.info(
                f"[ClosedLoop] Loaded {len(spec_gaps)} spec gaps from cycle {prev_cycle}"
            )
            return spec_gaps
        except Exception as e:
            logger.warning(f"[ClosedLoop] Failed to load previous report: {e}")
            return []

    async def run_auto(
        self,
        max_cycles: int = 5,
        dispatcher_factory=None,
        initial_plan_path: str | None = None,
        max_parallel: int = 3,
        no_review: bool = True,
        skip_initial_verify: bool = False,
        manual_mode: bool = False,
    ):
        """
        Auto mode: run cycles until convergence or max_cycles.

        Args:
            max_cycles: Maximum number of cycles before giving up
            dispatcher_factory: Callable that creates a Dispatcher for execution
            initial_plan_path: Path to initial plan.json (cycle 1)
            max_parallel: Max parallel workers
            no_review: Skip manual review
            skip_initial_verify: v1.2 — skip full verify on cycle 1 when plan
                is pre-generated (e.g. from design pipeline). Uses closed-loop
                bounded scan instead of expensive open-ended spec diff.
        """
        t0 = time.time()

        # v1.3: auto-start run if not already started (standalone mode)
        if self.run_history._current_run is None:
            import datetime as _dt
            _run_id = f"run-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            self.run_history.start_run(_run_id, self.config)

        # v1.3: check STOP before cleaning — respect active stop signal
        mesh_dir = os.path.join(self.repo_dir, ".agent-mesh")
        stop_path = os.path.join(mesh_dir, "STOP")
        if os.path.exists(stop_path):
            self._emit_event("STOP 🛑 stop signal detected at startup")
            logger.info("[ProjectLoop] 🛑 Stop signal exists, not starting")
            return False

        # Clean stale CONTINUE/SKIP from previous runs (not STOP)
        for sig in ["CONTINUE", "SKIP"]:
            sig_path = os.path.join(mesh_dir, sig)
            if os.path.exists(sig_path):
                os.remove(sig_path)
                logger.info(f"[ProjectLoop] Cleaned stale signal: {sig}")

        cycle = 0
        while True:
            cycle += 1

            # v1.2: graceful shutdown check
            stop_file = os.path.join(self.repo_dir, ".agent-mesh", "STOP")
            if os.path.exists(stop_file):
                self._emit_event("STOP 🛑 graceful shutdown requested")
                logger.info("[ProjectLoop] 🛑 Stop signal detected, finishing gracefully")
                return False

            logger.info(f"\n{'='*60}")
            logger.info(f"  🔄 Project ReAct — Cycle {cycle}")
            logger.info(f"{'='*60}")

            # ── THINK: What's the current plan? ──
            if cycle == 1 and initial_plan_path:
                plan_path = initial_plan_path
                logger.info(f"[ProjectLoop] Using initial plan: {plan_path}")
            elif cycle > 1:
                # Generate fix-plan from previous verify
                prev_report = self.cycle_history[-1].get("report")
                if prev_report and prev_report.passed:
                    logger.info("[ProjectLoop] ✅ Previous cycle passed — done!")
                    break

                plan_path = os.path.join(
                    self.repo_dir, f".agent-mesh/fix-plan-{cycle - 1}.json"
                )
                if not os.path.exists(plan_path):
                    logger.error(f"[ProjectLoop] No fix-plan found for cycle {cycle}")
                    break
            else:
                logger.error("[ProjectLoop] No initial plan provided")
                break

            # ── ACT: Execute the plan ──
            cycle_start = time.time()
            commit_before = self.run_history.get_current_commit()
            dispatcher = None  # will be set if dispatcher_factory exists

            if dispatcher_factory:
                # v1.3: manual mode — pause before execution
                if manual_mode:
                    # Read plan to show summary
                    try:
                        import json as _json
                        with open(plan_path) as _f:
                            _plan_data = _json.load(_f)
                        _tasks = _plan_data.get("tasks", [])
                        _task_lines = []
                        for _t in _tasks:
                            _task_lines.append(
                                f"  • {_t.get('title', '?')} "
                                f"[{_t.get('complexity', '?')}] "
                                f"files: {', '.join(_t.get('target_files', [])[:3]) or 'auto'}"
                            )
                        _model_hint = "DeepSeek→Sonnet→Opus" if cycle == 1 else (
                            "Sonnet start" if cycle == 2 else "Opus start"
                        )
                        _summary = (
                            f"Cycle {cycle}\n"
                            f"Plan: {plan_path}\n"
                            f"Tasks: {len(_tasks)}\n"
                            f"Model chain: {_model_hint}\n"
                            f"Max parallel: {max_parallel}\n\n"
                            f"Tasks:\n" + "\n".join(_task_lines)
                        )
                    except Exception:
                        _summary = f"Cycle {cycle}\nPlan: {plan_path}"

                    decision = await self._manual_pause("PRE-EXECUTE", _summary)
                    if decision == "skip":
                        return False
                    if decision == "stop":
                        return False

                logger.info(f"[ProjectLoop] 🚀 Executing plan: {plan_path}")
                dispatcher = dispatcher_factory(
                    plan_path=plan_path,
                    max_parallel=max_parallel,
                    no_review=no_review,
                )
                # v1.2: cycle escalation — cycle 2 → Sonnet, cycle 3+ → Opus
                if cycle >= 2 and hasattr(dispatcher, 'router'):
                    dispatcher.router.fix_cycle = cycle
                exec_result = await dispatcher.run()
                logger.info(f"[ProjectLoop] Execution complete: {exec_result}")
            else:
                logger.info("[ProjectLoop] ⏭ No dispatcher — verify only mode")

            # ── OBSERVE: Verify the result ──
            # v1.2: skip_initial_verify → use closed-loop even for cycle 1
            # (saves ~1 expensive open-ended LLM scan when plan is pre-generated)
            # Cycle 2+: always closed-loop (regression + bounded scan)
            if cycle >= 2 or (cycle == 1 and skip_initial_verify):
                report, plan = await self.verify_closed_loop(cycle)
            else:
                report, plan = await self.verify_and_plan(cycle)

            # v1.2: invalidate verifier caches between cycles
            self.verifier.invalidate_caches()

            # Record cycle
            gap_count = len([
                i for i in report.issues if i.category == "spec_gap"
            ]) if not report.passed else 0

            self.cycle_history.append({
                "cycle": cycle,
                "report": report,
                "plan": plan,
                "issues": len(report.issues),
                "gap_count": gap_count,
                "passed": report.passed,
            })

            # v1.2: emit event for monitoring
            fix_count = len(plan['tasks']) if plan else 0
            status = "✅ PASSED" if report.passed else f"❌ {gap_count} gaps, {fix_count} fix tasks"
            self._emit_event(f"VERIFY cycle={cycle} build={'✅' if report.build_ok else '❌'} {status}")

            # v1.3: save checkpoint for restore
            self._save_checkpoint(cycle, gap_count, fix_count, report)

            # v1.3: record cycle to run history
            commit_after = self.run_history.get_current_commit()
            chunk_id = self.config.get("current_chunk_id", "unknown")

            # Collect execution data from dispatcher (if available)
            exec_cost = getattr(dispatcher, 'cost_usd', 0) if dispatcher else 0
            exec_tasks = getattr(dispatcher, 'task_summaries', []) if dispatcher else []
            exec_completed = sum(1 for t in exec_tasks if t.get("status") == "completed")

            self.run_history.record_cycle(
                chunk_id=chunk_id,
                cycle=cycle,
                duration_sec=time.time() - cycle_start,
                cost_usd=exec_cost,
                commit_before=commit_before,
                commit_after=commit_after,
                execution={
                    "task_count": len(exec_tasks) if exec_tasks else _count_plan_tasks(plan_path),
                    "completed": exec_completed,
                    "failed": len(exec_tasks) - exec_completed,
                    "tasks": exec_tasks,
                },
                verify={
                    "build_ok": report.build_ok,
                    "test_ok": report.test_ok,
                    "lint_ok": report.lint_ok,
                    "total_gaps": gap_count,
                },
                escalation=self.escalation.get_status(),
            )

            # ── Check termination ──
            if report.passed:
                total_time = time.time() - t0
                logger.info(f"\n{'='*60}")
                logger.info(f"  ✅ Project Complete! (cycle {cycle}, {total_time:.0f}s)")
                logger.info(f"{'='*60}")
                self._print_convergence_summary()
                self._emit_event(f"CHUNK_DONE ✅ passed after {cycle} cycles ({total_time:.0f}s)")
                return True

            # ── EVALUATE: Model ranking escalation ──
            decision = self.escalation.record_cycle(gap_count)

            if decision.give_up:
                total_time = time.time() - t0
                logger.warning(
                    f"[ProjectLoop] ⛔ Escalation exhausted: {decision.reason}. "
                    f"Stopping after {cycle} cycles ({total_time:.0f}s)"
                )
                self._print_convergence_summary()
                self._emit_event(f"CHUNK_DONE ❌ gave up after {cycle} cycles ({total_time:.0f}s) — {gap_count} gaps remain")
                return False

            if decision.escalate:
                # Apply new min rank to config for next cycle's dispatcher
                self.config.setdefault("routing", {})["outer_loop_min_rank"] = decision.min_rank
                logger.info(
                    f"[ProjectLoop] ⬆️ Escalating: {decision.reason}"
                )

            if decision.extend_timeout:
                self.config.setdefault("routing", {})["outer_loop_timeout_multiplier"] = (
                    decision.timeout_multiplier
                )
                logger.info(
                    f"[ProjectLoop] ⏱️ Extending timeout: ×{decision.timeout_multiplier:.1f}"
                )

            # Show convergence progress
            esc_status = self.escalation.get_status()
            logger.info(
                f"[ProjectLoop] Cycle {cycle}: {gap_count} gaps remaining, "
                f"{len(plan['tasks']) if plan else 0} fix tasks, "
                f"min rank: {esc_status['current_min_rank']} ({esc_status['rank_label']})"
            )

            # v1.4: manual mode — pause after every cycle that doesn't pass
            if manual_mode:
                # Classify each gap: NEW / RECURRING / SPEC / OUT_OF_SCOPE
                _gap_lines = []
                _chunk_id = self.config.get("current_chunk_id", "")
                _chunk_modules = self._get_chunk_modules(_chunk_id)
                _prev_gap_msgs = self._collect_previous_gap_messages()
                _fixed_gap_msgs = self._collect_fixed_gap_messages(cycle)

                for _i in report.issues:
                    if _i.category != "spec_gap":
                        continue
                    _tag = self._classify_gap(
                        _i, _chunk_modules, _prev_gap_msgs, _fixed_gap_msgs
                    )
                    _gap_lines.append(f"  • [{_tag}] {_i.message[:120]}")

                _gap_history = " → ".join(
                    str(e.get("gap_count", "?")) for e in self.cycle_history
                )
                _next_model = "Sonnet" if cycle + 1 == 2 else (
                    "Opus" if cycle + 1 >= 3 else "DeepSeek"
                )
                _verify_summary = (
                    f"Cycle {cycle} complete.\n"
                    f"Build: {'✅' if report.build_ok else '❌'}\n"
                    f"Gaps: {gap_count}\n"
                    f"Gap trend: {_gap_history}\n"
                    f"Fix tasks generated: {fix_count}\n"
                    f"Next cycle model: {_next_model}\n\n"
                    f"Gap details:\n" + "\n".join(_gap_lines[:15])
                )

                decision = await self._manual_pause("POST-VERIFY", _verify_summary)
                if decision == "skip":
                    return False
                if decision == "stop":
                    return False

    def _get_chunk_modules(self, chunk_id: str) -> set[str]:
        """Extract module keyword(s) that belong to this chunk."""
        keywords = set()
        # Parse from chunk_id like "chunk-3-notification-module"
        parts = chunk_id.replace("chunk-", "").split("-", 1)
        if len(parts) > 1:
            # Extract meaningful words (skip generic: module, backend, frontend)
            skip = {"module", "backend", "frontend", "fullstack", "behavior", "rules"}
            for word in parts[1].split("-"):
                if word.lower() not in skip and len(word) > 2:
                    keywords.add(word.lower())
        # Also check config for explicit module list
        chunk_modules = self.config.get("chunk_modules", [])
        for m in chunk_modules:
            keywords.add(m.lower())
        return keywords

    def _collect_previous_gap_messages(self) -> set[str]:
        """Collect all gap messages from all previous cycles."""
        msgs = set()
        for entry in self.cycle_history:
            report = entry.get("report")
            if report:
                for issue in report.issues:
                    if issue.category == "spec_gap":
                        # Normalize: first 80 chars for fuzzy matching
                        msgs.add(issue.message[:80].strip().lower())
        return msgs

    def _collect_fixed_gap_messages(self, current_cycle: int) -> set[str]:
        """Collect gaps that were fixed (appeared before but not in latest cycle).
        These are gaps that appeared in earlier cycles but got resolved."""
        if len(self.cycle_history) < 2:
            return set()

        # Gaps from all cycles except the latest
        prev_msgs = set()
        for entry in self.cycle_history[:-1]:
            report = entry.get("report")
            if report:
                for issue in report.issues:
                    if issue.category == "spec_gap":
                        prev_msgs.add(issue.message[:80].strip().lower())

        # Gaps in the latest cycle
        latest = self.cycle_history[-1].get("report")
        latest_msgs = set()
        if latest:
            for issue in latest.issues:
                if issue.category == "spec_gap":
                    latest_msgs.add(issue.message[:80].strip().lower())

        # Fixed = appeared before but not in latest
        return prev_msgs - latest_msgs

    def _classify_gap(self, issue, chunk_modules: set[str],
                      prev_gap_msgs: set[str], fixed_gap_msgs: set[str]) -> str:
        """Classify a gap as NEW / RECURRING / PERSISTENT.

        - RECURRING: this gap was fixed before but came back (loop detected)
        - PERSISTENT: gap seen in previous cycles, not yet fixed
        - NEW: first time seeing this gap

        Note: OUT_OF_SCOPE classification removed — the verifier's scoped prompt
        already ensures only in-scope gaps are reported. chunk_id-based keyword
        matching was too crude and caused false OUT_OF_SCOPE labels.
        """
        msg_key = issue.message[:80].strip().lower()

        # Check if this gap was previously fixed but came back
        if msg_key in fixed_gap_msgs:
            return "🔄 RECURRING"

        # Check if this gap has been seen in previous cycles (persistent, not fixed)
        if msg_key in prev_gap_msgs:
            return "⚠️ PERSISTENT"

        return "🆕 NEW"

    def _print_convergence_summary(self):
        """Print convergence progress across all cycles."""
        logger.info("\n📊 Convergence History:")
        for entry in self.cycle_history:
            gaps = entry.get("gap_count", entry["issues"])
            status = "✅" if entry["passed"] else f"❌ {gaps} gaps"
            logger.info(f"  Cycle {entry['cycle']}: {status}")

        esc = self.escalation.get_status()
        if esc["gap_history"]:
            logger.info(
                f"\n📈 Gap trend: {' → '.join(str(g) for g in esc['gap_history'])}"
            )
            logger.info(
                f"🏆 Final min rank: {esc['current_min_rank']} ({esc['rank_label']})"
            )

    async def _manual_pause(self, phase: str, summary: str) -> str:
        """
        v1.3: Manual mode — pause and wait for user confirmation.
        Writes a status file with details, waits for CONTINUE/SKIP signal.

        Args:
            phase: Phase name (e.g. "pre-execute", "post-verify")
            summary: Human-readable summary of what's about to happen / just happened

        Returns: 'continue', 'skip', or 'timeout'
        """
        mesh_dir = os.path.join(self.repo_dir, ".agent-mesh")
        os.makedirs(mesh_dir, exist_ok=True)

        # Write status file for user to review
        status_path = os.path.join(mesh_dir, "MANUAL-STATUS.txt")
        with open(status_path, 'w') as f:
            f.write(f"=== MANUAL MODE: {phase} ===\n\n")
            f.write(summary)
            f.write(f"\n\n--- Actions ---\n")
            f.write(f"touch {mesh_dir}/CONTINUE  → proceed\n")
            f.write(f"touch {mesh_dir}/SKIP      → skip to next chunk\n")
            f.write(f"touch {mesh_dir}/STOP      → stop entirely\n")

        continue_file = os.path.join(mesh_dir, "CONTINUE")
        skip_file = os.path.join(mesh_dir, "SKIP")
        stop_file = os.path.join(mesh_dir, "STOP")

        # Clean stale signals
        for f in [continue_file, skip_file]:
            if os.path.exists(f):
                os.remove(f)

        # Notify
        logger.info(f"\n{'='*60}")
        logger.info(f"  ⏸️  MANUAL PAUSE: {phase}")
        logger.info(f"{'='*60}")
        logger.info(summary)
        logger.info(f"\nWaiting for signal: touch CONTINUE / SKIP / STOP")
        logger.info(f"Status file: {status_path}")
        self._emit_event(f"MANUAL_PAUSE {phase}")

        # Discord notification
        if self._discord_webhook:
            self._discord_send(
                f"⏸️ **Manual pause: {phase}**\n\n{summary}\n\n"
                f"`touch CONTINUE` / `touch SKIP` / `touch STOP`"
            )

        # Poll (every 10s, no timeout — wait forever in manual mode)
        while True:
            if os.path.exists(stop_file):
                return "stop"
            if os.path.exists(continue_file):
                os.remove(continue_file)
                logger.info("[Manual] ▶️ CONTINUE received")
                return "continue"
            if os.path.exists(skip_file):
                os.remove(skip_file)
                logger.info("[Manual] ⏭️ SKIP received")
                return "skip"
            await asyncio.sleep(10)

    def _save_checkpoint(self, cycle: int, gap_count: int,
                         fix_count: int, report) -> None:
        """
        v1.3: Save checkpoint after each cycle for restore capability.
        Stores git commit hash + cycle metadata so user can jump back.
        """
        import datetime
        import subprocess

        mesh_dir = os.path.join(self.repo_dir, ".agent-mesh")
        checkpoints_path = os.path.join(mesh_dir, "checkpoints.json")

        # Get current git commit hash
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_dir, capture_output=True, text=True
            )
            commit_hash = result.stdout.strip()
        except Exception:
            commit_hash = "unknown"

        # Get chunk_id from config (set by design_loop)
        chunk_id = self.config.get("current_chunk_id", "unknown")

        # Build gap summary
        gap_details = []
        if not report.passed:
            for issue in report.issues:
                if issue.category == "spec_gap":
                    gap_details.append({
                        "severity": issue.severity,
                        "message": issue.message[:200],
                        "module": issue.module or "",
                    })

        checkpoint = {
            "chunk_id": chunk_id,
            "cycle": cycle,
            "timestamp": datetime.datetime.now().isoformat(),
            "commit": commit_hash,
            "gaps": gap_count,
            "fix_tasks": fix_count,
            "build_ok": report.build_ok,
            "passed": report.passed,
            "gap_details": gap_details[:20],
            "fix_plan_path": os.path.join(mesh_dir, f"fix-plan-{cycle}.json"),
        }

        # Load existing checkpoints
        checkpoints = []
        if os.path.exists(checkpoints_path):
            try:
                with open(checkpoints_path) as f:
                    checkpoints = json.load(f)
            except Exception:
                checkpoints = []

        checkpoints.append(checkpoint)

        with open(checkpoints_path, 'w') as f:
            json.dump(checkpoints, f, indent=2, ensure_ascii=False)

        logger.info(
            f"[Checkpoint] Saved: {chunk_id}/cycle-{cycle} "
            f"commit={commit_hash[:8]} gaps={gap_count}"
        )

    @staticmethod
    def list_checkpoints(repo_dir: str) -> list[dict]:
        """List all saved checkpoints for display."""
        path = os.path.join(repo_dir, ".agent-mesh", "checkpoints.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return []

    @staticmethod
    async def restore_checkpoint(repo_dir: str, chunk_id: str, cycle: int) -> dict | None:
        """
        Restore code to a specific checkpoint state.
        Returns the checkpoint dict if found, None otherwise.
        """
        import asyncio

        checkpoints = ProjectLoop.list_checkpoints(repo_dir)

        # Find matching checkpoint
        target = None
        for cp in checkpoints:
            if cp["chunk_id"] == chunk_id and cp["cycle"] == cycle:
                target = cp

        if not target:
            logger.error(
                f"[Checkpoint] Not found: {chunk_id}/cycle-{cycle}. "
                f"Available: {[(c['chunk_id'], c['cycle']) for c in checkpoints]}"
            )
            return None

        commit = target["commit"]
        if commit == "unknown":
            logger.error("[Checkpoint] No git commit hash saved for this checkpoint")
            return None

        # Restore git state
        proc = await asyncio.create_subprocess_shell(
            f"git reset --hard {commit}",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"[Checkpoint] git reset failed: {stderr.decode()}")
            return None

        logger.info(
            f"[Checkpoint] Restored to {chunk_id}/cycle-{cycle} "
            f"(commit {commit[:8]})"
        )
        return target


def _count_plan_tasks(plan_path: str) -> int:
    """Count tasks in a plan file (safe fallback)."""
    try:
        with open(plan_path) as f:
            return len(json.load(f).get("tasks", []))
    except Exception:
        return 0
