"""
Agent Mesh v0.7 — Project-level ReAct Loop

Outer loop that orchestrates the full cycle:
  Spec → Plan → Execute → Verify → Fix-Plan → Execute → Verify → ... → Done

This wraps the existing dispatcher (inner loop) with a verification
and fix-plan generation layer.

Expected convergence:
  Cycle 1: spec.md → 20 tasks → execute → 8 gaps (60% done)
  Cycle 2: fix-plan → 8 tasks → execute → 3 gaps (85% done)
  Cycle 3: fix-plan → 3 tasks → execute → 1 gap  (95% done)
  Cycle 4: fix-plan → 1 task  → execute → 0 gaps  → Done ✅
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .verifier import Verifier, VerifyReport
from .gap_analyzer import GapAnalyzer

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
        self.cycle_history: list[dict] = []

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

    async def run_auto(
        self,
        max_cycles: int = 5,
        dispatcher_factory=None,
        initial_plan_path: str | None = None,
        max_parallel: int = 3,
        no_review: bool = True,
    ):
        """
        Auto mode: run cycles until convergence or max_cycles.
        
        Args:
            max_cycles: Maximum number of cycles before giving up
            dispatcher_factory: Callable that creates a Dispatcher for execution
            initial_plan_path: Path to initial plan.json (cycle 1)
            max_parallel: Max parallel workers
            no_review: Skip manual review
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
                    self.repo_dir, f".agent-mesh/fix-plan-{cycle}.json"
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
            report, plan = await self.verify_and_plan(cycle)

            # Record cycle
            self.cycle_history.append({
                "cycle": cycle,
                "report": report,
                "plan": plan,
                "issues": len(report.issues),
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

            # Show convergence progress
            logger.info(
                f"[ProjectLoop] Cycle {cycle}: {len(report.issues)} issues remaining, "
                f"{len(plan['tasks']) if plan else 0} fix tasks generated"
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
            status = "✅" if entry["passed"] else f"❌ {entry['issues']} issues"
            logger.info(f"  Cycle {entry['cycle']}: {status}")
