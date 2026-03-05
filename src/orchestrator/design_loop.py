"""
Agent Mesh v1.0 — Design Loop

Orchestrates Design Pipeline ↔ Implementation Pipeline recursion.
Takes old spec + new spec, analyzes delta, chunks into batches,
and runs each batch through the existing Implementation Pipeline (Layer 1-4).

Flow:
  1. SpecAnalyzer.analyze_delta(old, new, repo) → list[DesignChange]
  2. SpecAnalyzer.review_feasibility(changes, repo)
  3. SpecRefiner.plan_chunks(changes, new_spec) → list[DesignChunk]
  4. For each chunk:
     a. Write chunk.partial_spec → temp file
     b. Planner.plan(partial_spec) → plan.json
     c. ProjectLoop.run_auto(plan, spec=partial_spec, cycles=N)
     d. Validate results
     e. If design drift → adjust remaining chunks
  5. Final validation against full new spec
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any

from .spec_analyzer import SpecAnalyzer, DesignChange
from .spec_refiner import SpecRefiner, DesignChunk

logger = logging.getLogger("agent-mesh")


class DesignLoop:
    """Orchestrates Design Pipeline ↔ Implementation Pipeline recursion."""

    def __init__(self, config: dict, repo_dir: str):
        self.config = config
        self.repo_dir = repo_dir
        self.analyzer = SpecAnalyzer(config)
        self.refiner = SpecRefiner(config)
        self.mesh_dir = os.path.join(repo_dir, ".agent-mesh")
        self.chunk_history: list[dict] = []
        self.max_design_iterations = config.get("design", {}).get(
            "max_design_iterations", 3
        )

    async def run(
        self,
        old_spec_path: str,
        new_spec_path: str,
        max_inner_cycles: int = 3,
        max_parallel: int = 4,
        no_review: bool = True,
    ) -> bool:
        """
        Main entry: Design Pipeline → Implementation Pipeline → Validation.
        Recursive: final validation gaps → re-analyze → new chunks → re-implement.

        Returns True if all chunks completed successfully.
        """
        t0 = time.time()
        os.makedirs(self.mesh_dir, exist_ok=True)

        # Load specs
        with open(old_spec_path) as f:
            old_spec = f.read()
        with open(new_spec_path) as f:
            new_spec = f.read()

        logger.info(f"\n{'='*60}")
        logger.info(f"  🏗️ Design Pipeline — Spec Evolution")
        logger.info(f"  Old: {old_spec_path}")
        logger.info(f"  New: {new_spec_path}")
        logger.info(f"  Max design iterations: {self.max_design_iterations}")
        logger.info(f"{'='*60}")

        # ── Step 1: Analyze delta (cached if design-changes.json exists) ──
        changes_path = os.path.join(self.mesh_dir, "design-changes.json")
        if os.path.exists(changes_path):
            logger.info(f"\n📊 Step 1: Loading cached delta → {changes_path}")
            with open(changes_path) as f:
                changes = [DesignChange.from_dict(c) for c in json.load(f)]
            logger.info(f"[DesignLoop] Loaded {len(changes)} cached changes")
        else:
            logger.info("\n📊 Step 1: Analyzing spec delta...")
            changes = await self.analyzer.analyze_delta(old_spec, new_spec, self.repo_dir)

            if not changes:
                logger.info("[DesignLoop] No changes detected between specs")
                return True

            with open(changes_path, 'w') as f:
                json.dump([c.to_dict() for c in changes], f, indent=2, ensure_ascii=False)
            logger.info(f"[DesignLoop] {len(changes)} changes → {changes_path}")

            # ── Step 2: Feasibility review ──
            logger.info("\n🔍 Step 2: Reviewing feasibility...")
            changes = await self.analyzer.review_feasibility(changes, self.repo_dir)

            # Save reviewed changes back
            with open(changes_path, 'w') as f:
                json.dump([c.to_dict() for c in changes], f, indent=2, ensure_ascii=False)

        blocked = [c for c in changes if c.feasibility_notes.startswith("⚠️ BLOCKED")]
        if blocked:
            logger.warning(
                f"[DesignLoop] {len(blocked)} changes blocked: "
                + ", ".join(c.change_id for c in blocked)
            )

        # ── Outer recursion loop ──
        for design_iter in range(1, self.max_design_iterations + 1):
            logger.info(f"\n{'='*60}")
            logger.info(f"  🔄 Design Iteration {design_iter}/{self.max_design_iterations}")
            logger.info(f"{'='*60}")

            # ── Step 3: Plan chunks (cached if design-chunks-iter{N}.json exists) ──
            chunks_path = os.path.join(
                self.mesh_dir, f"design-chunks-iter{design_iter}.json"
            )
            if os.path.exists(chunks_path):
                logger.info(f"\n📦 Step 3: Loading cached chunks → {chunks_path}")
                with open(chunks_path) as f:
                    chunks = [DesignChunk.from_dict(c) for c in json.load(f)]
                logger.info(f"[DesignLoop] Loaded {len(chunks)} cached chunks")
            else:
                logger.info("\n📦 Step 3: Planning implementation chunks...")
                chunks = await self.refiner.plan_chunks(changes, new_spec)
                self.refiner.map_changes_to_chunks(chunks, changes)

                with open(chunks_path, 'w') as f:
                    json.dump([c.to_dict() for c in chunks], f, indent=2, ensure_ascii=False)
                # Also save as latest
                with open(os.path.join(self.mesh_dir, "design-chunks.json"), 'w') as f:
                    json.dump([c.to_dict() for c in chunks], f, indent=2, ensure_ascii=False)
                logger.info(f"[DesignLoop] {len(chunks)} chunks → {chunks_path}")

            # ── Step 4: Execute chunks sequentially ──
            all_success = await self._execute_chunks(
                chunks, new_spec, max_inner_cycles, max_parallel, no_review
            )

            # ── Step 4.5: Residual gap sweep ──
            residual_gaps = await self._collect_residual_gaps()
            if residual_gaps:
                logger.info(
                    f"\n🧹 Step 4.5: Sweeping {len(residual_gaps)} residual gaps..."
                )
                real_gaps = await self._filter_false_positives(residual_gaps, new_spec)
                filtered_count = len(residual_gaps) - len(real_gaps)
                logger.info(
                    f"[DesignLoop] {len(real_gaps)} real gaps "
                    f"(filtered {filtered_count} false positives)"
                )

                if real_gaps:
                    fix_result = await self._run_residual_fix(
                        real_gaps, new_spec_path, max_parallel, no_review
                    )
                    remaining = fix_result.get("remaining", [])
                    if remaining:
                        remaining_path = os.path.join(
                            self.mesh_dir, "remaining-gaps.json"
                        )
                        with open(remaining_path, 'w') as f:
                            json.dump(
                                remaining, f, indent=2, ensure_ascii=False
                            )
                        logger.info(
                            f"[DesignLoop] ⚠️ {len(remaining)} gaps "
                            f"couldn't be fixed → {remaining_path}"
                        )

            # ── Step 5: Final validation ──
            logger.info("\n🔍 Step 5: Final validation against full spec...")
            final = await self._final_validation(new_spec_path, design_iter)

            if final.get("passed"):
                total_time = time.time() - t0
                logger.info(f"\n{'='*60}")
                logger.info(
                    f"  ✅ Design Pipeline Complete! "
                    f"(iter {design_iter}, {total_time:.0f}s)"
                )
                logger.info(f"{'='*60}")
                return True

            # ── Recursion: gaps found → convert to new changes → re-chunk ──
            gap_count = final.get("gap_count", 0)
            if design_iter >= self.max_design_iterations:
                logger.warning(
                    f"[DesignLoop] Max design iterations ({self.max_design_iterations}) "
                    f"reached with {gap_count} gaps remaining"
                )
                break

            logger.info(
                f"\n🔄 Final validation found {gap_count} gaps — "
                f"recursing into design iteration {design_iter + 1}..."
            )

            # Convert remaining gaps into new DesignChanges
            changes = await self._gaps_to_changes(
                final.get("issues_detail", []), new_spec
            )
            if not changes:
                logger.warning(
                    "[DesignLoop] Could not convert gaps to changes, stopping"
                )
                break

            # Clear progress for new iteration (old chunks are done)
            self._clear_progress()

            logger.info(
                f"[DesignLoop] {len(changes)} new changes from gaps, "
                f"re-entering chunking..."
            )

        total_time = time.time() - t0
        logger.warning(
            f"[DesignLoop] ⚠️ Design Pipeline finished with remaining gaps. "
            f"Total time: {total_time:.0f}s"
        )
        return False

    async def _execute_chunks(
        self,
        chunks: list[DesignChunk],
        new_spec: str,
        max_inner_cycles: int,
        max_parallel: int,
        no_review: bool,
    ) -> bool:
        """Execute all chunks sequentially with drift feedback between chunks."""
        all_success = True

        for i, chunk in enumerate(chunks):
            logger.info(f"\n{'─'*50}")
            logger.info(
                f"  📦 Chunk {i+1}/{len(chunks)}: {chunk.chunk_id} — {chunk.title}"
            )
            logger.info(f"  Wave: {chunk.wave_order}, Changes: {len(chunk.changes)}")
            logger.info(f"{'─'*50}")

            # Load progress (for resume)
            progress = self._load_progress()
            if progress.get(chunk.chunk_id, {}).get("status") == "completed":
                logger.info(f"[DesignLoop] ⏭ Chunk {chunk.chunk_id} already completed, skipping")
                continue

            chunk.status = "in_progress"
            self._save_progress(chunk.chunk_id, "in_progress", {})

            # Execute chunk
            result = await self._run_chunk(
                chunk, max_inner_cycles, max_parallel, no_review
            )

            # Validate chunk
            validation = await self._validate_chunk(chunk, result)

            if result.get("success"):
                chunk.status = "completed"
                self._save_progress(chunk.chunk_id, "completed", result)
                self.chunk_history.append({
                    "chunk_id": chunk.chunk_id,
                    "result": result,
                    "validation": validation,
                })

                # Adjust remaining chunks if design drift detected
                remaining = [c for c in chunks if c.status == "pending"]
                if remaining and (validation.get("design_issues") or validation.get("drift_notes")):
                    await self.refiner.adjust_remaining_chunks(
                        chunk, validation, remaining, new_spec
                    )
            else:
                chunk.status = "needs_redesign"
                self._save_progress(chunk.chunk_id, "needs_redesign", result)
                all_success = False
                logger.warning(
                    f"[DesignLoop] ❌ Chunk {chunk.chunk_id} failed: "
                    f"{result.get('error', 'unknown')}"
                )

        return all_success

    async def _run_chunk(
        self,
        chunk: DesignChunk,
        max_inner_cycles: int,
        max_parallel: int,
        no_review: bool,
    ) -> dict:
        """
        Execute one chunk through the Implementation Pipeline.

        1. Write chunk.partial_spec to temp file
        2. Call Planner.plan(partial_spec) → plan.json
        3. Call ProjectLoop.run_auto(plan, spec=partial_spec, cycles=max_inner_cycles)
        4. Return result summary
        """
        from ..context.store import ContextStore
        from ..models.task import TaskPlan
        from .planner import Planner
        from .dispatcher import Dispatcher
        from .project_loop import ProjectLoop

        # Get repo structure so planner generates correct file paths
        from .spec_analyzer import get_code_tree
        code_tree = await get_code_tree(self.repo_dir, char_limit=20_000)

        # Write partial spec with repo structure appended
        spec_path = os.path.join(self.mesh_dir, f"{chunk.chunk_id}-spec.md")
        spec_with_context = (
            chunk.partial_spec
            + f"\n\n## CURRENT PROJECT STRUCTURE\n"
            f"IMPORTANT: Use the ACTUAL file paths below. "
            f"Do NOT invent paths like src/ or db/ — match existing structure.\n\n"
            f"{code_tree}\n"
        )
        with open(spec_path, 'w') as f:
            f.write(spec_with_context)
        logger.info(f"[DesignLoop] Wrote partial spec: {spec_path}")

        # Plan from partial spec (cached if plan.json exists)
        plan_path = os.path.join(self.mesh_dir, f"{chunk.chunk_id}-plan.json")
        if os.path.exists(plan_path):
            logger.info(f"[DesignLoop] Loading cached plan → {plan_path}")
            plan = Planner.load_plan(plan_path)
        else:
            logger.info(f"[DesignLoop] Planning from partial spec...")
            planner = Planner(self.config, self.repo_dir)
            try:
                plan = await planner.plan(spec_path)
            except Exception as e:
                logger.error(f"[DesignLoop] Planning failed for {chunk.chunk_id}: {e}")
                return {"success": False, "error": f"Planning failed: {e}"}

            if plan is None or not plan.tasks:
                logger.warning(f"[DesignLoop] Empty plan for {chunk.chunk_id}")
                return {"success": False, "error": "Empty plan generated"}

            Planner.save_plan(plan, plan_path)
        logger.info(
            f"[DesignLoop] Plan: {len(plan.tasks)} tasks → {plan_path}"
        )

        # Run Implementation Pipeline (reuse run_cycles pattern from main.py)
        store = ContextStore(self.repo_dir)

        # v0.9: experience tracking (optional)
        exp_store = None
        advisor = None
        project_name = os.path.basename(self.repo_dir)
        project_type = "unknown"
        try:
            from .experience_store import ExperienceStore
            from .project_classifier import ProjectClassifier
            from .experience_advisor import ExperienceAdvisor

            exp_store = ExperienceStore()
            classifier = ProjectClassifier()
            profile = exp_store.get_project_profile(project_name)
            if profile and profile.get("project_type"):
                project_type = profile["project_type"]
            else:
                result = classifier.classify(self.repo_dir)
                project_type = result["project_type"]
            advisor = ExperienceAdvisor(exp_store, project_type)
        except Exception:
            pass

        total_cost = 0.0

        class _ChunkDispatcher:
            """Adapts Dispatcher for chunk execution."""
            def __init__(self_, plan_path: str, max_parallel: int = 4, no_review: bool = True):
                nonlocal total_cost
                with open(plan_path) as f:
                    plan_data = json.load(f)
                self_.plan = TaskPlan.from_dict(plan_data)
                self_.run_id = store.save_plan(self_.plan)
                cfg = {**self.config, "no_review": no_review}
                cfg.setdefault("dispatcher", {})["max_parallel"] = max_parallel
                self_.dispatcher = Dispatcher(
                    cfg, self.repo_dir, store,
                    experience_store=exp_store,
                    project_name=project_name,
                    project_type=project_type,
                )
                if advisor:
                    self_.dispatcher.router.advisor = advisor

            async def run(self_):
                nonlocal total_cost
                await self_.dispatcher.execute_plan(
                    plan=self_.plan,
                    run_id=self_.run_id,
                    resume=True,
                )
                total_cost += self_.dispatcher.wave_cost_usd
                return "done"

        loop = ProjectLoop(self.config, self.repo_dir, spec_path)

        try:
            success = await loop.run_auto(
                max_cycles=max_inner_cycles,
                dispatcher_factory=_ChunkDispatcher,
                initial_plan_path=plan_path,
                max_parallel=max_parallel,
                no_review=no_review,
            )
        except Exception as e:
            logger.error(f"[DesignLoop] Execution error for {chunk.chunk_id}: {e}")
            success = False

        # Cleanup
        store.close()
        if exp_store:
            if total_cost > 0:
                exp_store.add_project_cost(project_name, total_cost)
            exp_store.close()

        # Collect gap details from last cycle
        final_issues = []
        if loop.cycle_history:
            last_report = loop.cycle_history[-1].get("report")
            if last_report and hasattr(last_report, "issues"):
                final_issues = [i.to_dict() for i in last_report.issues]

        return {
            "success": success,
            "tasks": len(plan.tasks) if plan else 0,
            "cost_usd": total_cost,
            "cycles": len(loop.cycle_history),
            "final_gaps": (
                loop.cycle_history[-1].get("gap_count", 0)
                if loop.cycle_history else 0
            ),
            "final_issues": final_issues,
        }

    async def _validate_chunk(
        self, chunk: DesignChunk, impl_result: dict
    ) -> dict:
        """
        Compare implementation result against chunk's design intent.

        Returns:
        - code_issues: list (handled by inner loop)
        - design_issues: list (need spec adjustment)
        - drift_notes: str (feed into next chunk)
        """
        if not impl_result.get("success"):
            return {
                "design_issues": [
                    f"Chunk {chunk.chunk_id} failed: {impl_result.get('error', 'unknown')}"
                ],
                "drift_notes": (
                    f"Chunk {chunk.chunk_id} could not be implemented. "
                    f"Remaining gaps: {impl_result.get('final_gaps', '?')}. "
                    f"Subsequent chunks may need to work around this."
                ),
            }

        final_gaps = impl_result.get("final_gaps", 0)
        if final_gaps > 0:
            return {
                "design_issues": [],
                "drift_notes": (
                    f"Chunk {chunk.chunk_id} completed with {final_gaps} remaining gaps. "
                    f"These may affect dependent chunks."
                ),
            }

        return {"design_issues": [], "drift_notes": ""}

    async def _collect_residual_gaps(self) -> list[dict]:
        """Collect all remaining gaps from completed chunks."""
        progress = self._load_progress()
        all_issues = []
        for chunk_id, info in progress.items():
            if info.get("status") in ("completed", "needs_redesign"):
                issues = info.get("result", {}).get("final_issues", [])
                for issue in issues:
                    issue = dict(issue)  # copy to avoid mutation
                    issue["source_chunk"] = chunk_id
                    all_issues.append(issue)
        return all_issues

    async def _filter_false_positives(
        self, issues: list[dict], spec_content: str
    ) -> list[dict]:
        """Use LLM to quickly filter out false positive gaps.

        False positives happen when:
        - Verifier can't see actual implementation (file path mismatch)
        - Spec wording is vague, code implements it differently
        - Build passes but spec_gap scanner is too strict
        """
        if not issues:
            return []

        from .spec_analyzer import get_code_tree

        code_tree = await get_code_tree(self.repo_dir, char_limit=40_000)
        issues_json = json.dumps(issues[:50], indent=2, ensure_ascii=False)

        prompt = f"""You are a senior QA engineer reviewing spec gap reports.
Some of these gaps may be FALSE POSITIVES — the code actually implements the feature
but the automated scanner missed it (different file path, different naming, etc).

## Task
For each gap below, check the codebase and determine:
- TRUE: the gap is real, the feature is genuinely missing or broken
- FALSE: the feature exists in code, the scanner was wrong

## Output Format
Respond ONLY with a JSON array. No other text.
Each object:
{{
  "index": 0,
  "verdict": "TRUE" | "FALSE",
  "reason": "brief explanation"
}}

## GAPS TO CHECK
{issues_json}

## CODEBASE
{code_tree}
"""
        design_cfg = self.config.get("design", {})
        model = design_cfg.get("refiner_model", "claude-sonnet-4-6")

        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            proc = await asyncio.create_subprocess_shell(
                f'cat {prompt_file} | claude -p --dangerously-skip-permissions '
                f'--model {model} --output-format text',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            raw = stdout.decode().strip()
        except Exception as e:
            logger.warning(f"[DesignLoop] False positive filter failed: {e}, keeping all gaps")
            return issues
        finally:
            try:
                os.unlink(prompt_file)
            except Exception:
                pass

        # Parse verdicts
        from .spec_analyzer import _parse_json_array
        verdicts = _parse_json_array(raw)
        verdict_map = {}
        for v in verdicts:
            if isinstance(v, dict) and "index" in v:
                verdict_map[v["index"]] = v.get("verdict", "TRUE")

        real_gaps = []
        for i, issue in enumerate(issues):
            verdict = verdict_map.get(i, "TRUE")
            if verdict == "TRUE":
                real_gaps.append(issue)
            else:
                logger.debug(
                    f"[DesignLoop] Filtered false positive: {issue.get('message', '')[:80]}"
                )

        return real_gaps

    async def _run_residual_fix(
        self,
        issues: list[dict],
        spec_path: str,
        max_parallel: int,
        no_review: bool,
    ) -> dict:
        """Run a single fix pass on all residual gaps from all chunks.

        Returns: { "fixed": int, "remaining": list[dict] }
        """
        from ..context.store import ContextStore
        from ..models.task import TaskPlan
        from .dispatcher import Dispatcher
        from .gap_analyzer import GapAnalyzer
        from .verifier import VerifyReport, VerifyIssue

        # Convert issue dicts back to VerifyIssue objects
        verify_issues = []
        for issue_dict in issues:
            verify_issues.append(VerifyIssue(
                category=issue_dict.get("category", "spec_gap"),
                severity=issue_dict.get("severity", "MEDIUM"),
                message=issue_dict.get("message", ""),
                file=issue_dict.get("file"),
                module=issue_dict.get("module"),
            ))

        # Create a synthetic VerifyReport
        report = VerifyReport(
            cycle=999,
            issues=verify_issues,
            build_ok=True,  # build already passes
            test_ok=True,
            lint_ok=True,
            spec_gap_count=len(verify_issues),
            duration_s=0,
        )

        # Generate fix plan
        gap_analyzer = GapAnalyzer(self.config)
        plan_dict = gap_analyzer.generate_fix_plan(report, cycle=999)

        if not plan_dict or not plan_dict.get("tasks"):
            logger.warning("[DesignLoop] No fix tasks generated from residual gaps")
            return {"fixed": 0, "remaining": issues}

        # Save fix plan
        plan_path = os.path.join(self.mesh_dir, "residual-fix-plan.json")
        gap_analyzer.save_fix_plan(plan_dict, plan_path)
        logger.info(
            f"[DesignLoop] Residual fix plan: {len(plan_dict['tasks'])} tasks → {plan_path}"
        )

        # Force minimum rank to Sonnet (skip Grok for residual fixes)
        fix_config = {**self.config}
        fix_config.setdefault("routing", {})["outer_loop_min_rank"] = 6  # Sonnet rank
        fix_config.setdefault("dispatcher", {})["max_parallel"] = max_parallel
        fix_config["no_review"] = no_review

        # Execute
        store = ContextStore(self.repo_dir)
        plan = TaskPlan.from_dict(plan_dict)
        run_id = store.save_plan(plan)

        dispatcher = Dispatcher(fix_config, self.repo_dir, store)
        try:
            await dispatcher.execute_plan(plan=plan, run_id=run_id, resume=False)
        except Exception as e:
            logger.error(f"[DesignLoop] Residual fix execution error: {e}")

        total_cost = dispatcher.wave_cost_usd
        store.close()

        # Quick verify to see what's left
        from .verifier import Verifier
        verifier = Verifier(self.repo_dir, self.config)
        post_report = await verifier.run_mechanical()

        # Check which original gaps are still present
        # (simplified: if build still passes, remaining = spec gaps only)
        remaining = []
        if post_report.build_ok:
            # Re-run bounded scan against spec for remaining gaps
            try:
                remaining_issues = await verifier.run_bounded_scan(
                    spec_path=spec_path,
                    prev_issues=[VerifyIssue(**i) if isinstance(i, dict) else i for i in verify_issues],
                    max_gaps=50,
                )
                remaining = [i.to_dict() if hasattr(i, 'to_dict') else i for i in remaining_issues]
            except Exception as e:
                logger.warning(f"[DesignLoop] Post-fix scan failed: {e}")
                remaining = issues  # assume all still remain

        fixed_count = len(issues) - len(remaining)
        logger.info(
            f"[DesignLoop] Residual fix: {fixed_count} fixed, "
            f"{len(remaining)} remaining (cost: ${total_cost:.4f})"
        )

        return {
            "fixed": fixed_count,
            "remaining": remaining,
            "cost_usd": total_cost,
        }

    async def _final_validation(
        self, new_spec_path: str, design_iter: int = 1
    ) -> dict:
        """
        After all chunks done, run full verify against original v2.0 spec.
        Catches any gaps that fell through chunk boundaries.
        Returns issues_detail for recursion into new design iteration.
        """
        from .verifier import Verifier

        verifier = Verifier(self.repo_dir, self.config)
        report = await verifier.run(cycle=900 + design_iter, spec_path=new_spec_path)

        # Save final report
        report_path = os.path.join(
            self.mesh_dir, f"design-final-report-iter{design_iter}.json"
        )
        with open(report_path, 'w') as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

        logger.info(f"\n{report.summary()}")
        logger.info(f"[DesignLoop] Final report → {report_path}")

        return {
            "passed": report.passed,
            "gap_count": report.spec_gap_count,
            "issues": len(report.issues),
            "issues_detail": [i.to_dict() for i in report.issues],
        }

    async def _gaps_to_changes(
        self, issues: list[dict], new_spec: str
    ) -> list[DesignChange]:
        """
        Convert final validation gaps into DesignChanges for the next
        design iteration. Uses Opus to understand what each gap means
        and produce actionable changes.
        """
        if not issues:
            return []

        issues_text = json.dumps(issues[:30], indent=2, ensure_ascii=False)
        code_tree = await self.analyzer._get_code_tree(self.repo_dir)

        prompt = f"""You are a senior software architect. The implementation has gaps compared to the spec.
Convert these gaps into actionable design changes.

## Output Format
Respond ONLY with a JSON array. No other text.
Each change object:
{{
  "change_id": "fix-gap-kebab-case-id",
  "change_type": "NEW_API" | "ALTER_SCHEMA" | "MODIFY_BEHAVIOR" | "NEW_FRONTEND" | "NEW_MODULE",
  "module": "affected module",
  "title": "what needs to be done",
  "description": "detailed description",
  "dependencies": [],
  "affected_tables": [],
  "affected_endpoints": [],
  "estimated_complexity": "L" | "S" | "M" | "H",
  "spec_section": "relevant spec excerpt"
}}

## GAPS FOUND
{issues_text}

## SPEC (target)
{new_spec[:20000]}

## CURRENT CODEBASE
{code_tree[:30000]}
"""
        raw = await self.analyzer._call_claude(prompt, self.repo_dir)
        changes = self.analyzer._parse_changes(raw)
        logger.info(
            f"[DesignLoop] Converted {len(issues)} gaps → {len(changes)} new changes"
        )
        return changes

    def _clear_progress(self):
        """Clear progress file for a fresh design iteration."""
        progress_path = os.path.join(self.mesh_dir, "design-progress.json")
        if os.path.exists(progress_path):
            os.rename(
                progress_path,
                progress_path.replace(".json", f"-{int(time.time())}.json"),
            )

    def _save_progress(self, chunk_id: str, status: str, result: dict):
        """Save to .agent-mesh/design-progress.json for resume."""
        progress = self._load_progress()
        progress[chunk_id] = {
            "status": status,
            "result": result,
            "timestamp": time.time(),
        }
        progress_path = os.path.join(self.mesh_dir, "design-progress.json")
        with open(progress_path, 'w') as f:
            json.dump(progress, f, indent=2, ensure_ascii=False)

    def _load_progress(self) -> dict:
        """Load progress from .agent-mesh/design-progress.json."""
        progress_path = os.path.join(self.mesh_dir, "design-progress.json")
        if os.path.exists(progress_path):
            try:
                with open(progress_path) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}
