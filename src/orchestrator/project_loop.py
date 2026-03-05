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

import json
import logging
import os
import time
from typing import Any

from .verifier import Verifier, VerifyReport, VerifyIssue
from .gap_analyzer import GapAnalyzer
from .model_ranking import OuterLoopEscalation

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

    def __init__(self, config: dict, repo_dir: str, spec_path: str | None = None):
        self.config = config
        self.repo_dir = repo_dir
        self.spec_path = spec_path
        self.verifier = Verifier(repo_dir, config)
        self.gap_analyzer = GapAnalyzer(config)
        self.escalation = OuterLoopEscalation(config)
        self.cycle_history: list[dict] = []
        # v1.2: Layer 3 per-gap cache (avoid re-analyzing same gap)
        self._spec_feedback_cache: dict[str, str] = {}  # gap_key → root_cause

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
        convergence_threshold = verify_cfg.get("convergence_threshold", 3)

        logger.info(f"\n{'='*60}")
        logger.info(f"  🔍 Verify Cycle {cycle} (closed-loop)")
        logger.info(f"{'='*60}")

        # Step 0: Mechanical checks only (no LLM spec diff)
        report = await self.verifier.run_mechanical(cycle)
        logger.info(
            f"[ClosedLoop] Mechanical: build={'✅' if report.build_ok else '❌'} "
            f"test={'✅' if report.test_ok else '❌'} lint={'✅' if report.lint_ok else '❌'}"
        )

        # Step 1: Regression check (if previous report exists)
        remaining_gaps = []
        prev_gaps = self._load_prev_gaps(cycle)
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

        # Save report
        report_path = os.path.join(self.repo_dir, f".agent-mesh/verify-report-{cycle}.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, 'w') as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

        logger.info(f"\n{report.summary()}")

        # Step 3: Convergence check
        total_gaps = all_gap_count
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

        for cycle in range(1, max_cycles + 1):
            logger.info(f"\n{'='*60}")
            logger.info(f"  🔄 Project ReAct — Cycle {cycle}/{max_cycles}")
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
            if dispatcher_factory:
                logger.info(f"[ProjectLoop] 🚀 Executing plan: {plan_path}")
                dispatcher = dispatcher_factory(
                    plan_path=plan_path,
                    max_parallel=max_parallel,
                    no_review=no_review,
                )
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

            # ── Check termination ──
            if report.passed:
                total_time = time.time() - t0
                logger.info(f"\n{'='*60}")
                logger.info(f"  ✅ Project Complete! (cycle {cycle}, {total_time:.0f}s)")
                logger.info(f"{'='*60}")
                self._print_convergence_summary()
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

        # Max cycles reached
        total_time = time.time() - t0
        logger.warning(
            f"[ProjectLoop] ⚠️ Max cycles ({max_cycles}) reached. "
            f"{len(self.cycle_history[-1]['report'].issues)} issues remain. "
            f"Total time: {total_time:.0f}s"
        )
        self._print_convergence_summary()
        return False

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
