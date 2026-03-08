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
from .run_history import RunHistoryRecorder
from .codebase_guide import CodebaseGuide

logger = logging.getLogger("agent-mesh")


class DesignLoop:
    """Orchestrates Design Pipeline ↔ Implementation Pipeline recursion."""

    def __init__(self, config: dict, repo_dir: str):
        self.config = config
        self.repo_dir = repo_dir
        self.analyzer = SpecAnalyzer(config)
        self.refiner = SpecRefiner(config)
        self.codebase_guide = CodebaseGuide(config)
        self.mesh_dir = os.path.join(repo_dir, ".agent-mesh")
        self.chunk_history: list[dict] = []
        self.max_design_iterations = config.get("design", {}).get(
            "max_design_iterations", 3
        )
        self._events_path = os.path.join(repo_dir, ".agent-mesh", "events.log")
        self._discord_webhook = config.get("notifications", {}).get("discord_webhook", "")
        # v1.3: structured run history
        self.run_history = RunHistoryRecorder(repo_dir)

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

    def _should_stop(self) -> bool:
        """Check for graceful shutdown signal (touch .agent-mesh/STOP)."""
        return os.path.exists(os.path.join(self.mesh_dir, "STOP"))

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

        # v1.3: start run history
        import datetime
        run_id = f"run-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.run_history.start_run(run_id, {
            "spec_old": old_spec_path,
            "spec_new": new_spec_path,
            "max_parallel": max_parallel,
            "force_model": self.config.get("force_model", None),
            "manual_mode": self.config.get("manual_mode", False),
        })

        try:
            return await self._run_pipeline(
                old_spec_path, new_spec_path,
                max_inner_cycles, max_parallel, no_review, t0,
            )
        finally:
            self.run_history.end_run()

    async def _run_pipeline(
        self,
        old_spec_path: str,
        new_spec_path: str,
        max_inner_cycles: int,
        max_parallel: int,
        no_review: bool,
        t0: float,
    ) -> bool:
        """Inner pipeline logic — always wrapped by run() try/finally."""
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

        # ── Step 2.5: Generate CLAUDE.md codebase guide ──
        try:
            guide_path = await self.codebase_guide.ensure_guide(
                self.repo_dir, new_spec_path
            )
            if guide_path:
                logger.info(f"[DesignLoop] CLAUDE.md ready: {guide_path}")
        except Exception as e:
            logger.warning(f"[DesignLoop] CLAUDE.md generation failed: {e}")

        # ── Outer recursion loop (infinite until converged or stopped) ──
        design_iter = 0
        while True:
            design_iter += 1
            logger.info(f"\n{'='*60}")
            logger.info(f"  🔄 Design Iteration {design_iter}")
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

            # ── Recursion: gaps found → ask whether to continue ──
            gap_count = final.get("gap_count", 0)

            if self.config.get("manual_mode", False):
                # Manual mode: pause and ask
                summary = (
                    f"Iteration {design_iter} complete.\n"
                    f"Gaps: {gap_count}\n"
                    f"Total time: {time.time() - t0:.0f}s\n\n"
                    f"CONTINUE = run iteration {design_iter + 1}\n"
                    f"STOP = finish here"
                )
                signal = await self._wait_for_signal(
                    "POST-ITERATION", summary
                )
                if signal == "stop":
                    logger.info(
                        f"[DesignLoop] Stopped by user after iteration "
                        f"{design_iter} with {gap_count} gaps remaining"
                    )
                    break
                if signal == "skip":
                    logger.info(
                        f"[DesignLoop] Skipping remaining gaps, finishing"
                    )
                    break
            else:
                # Non-manual mode: use max_design_iterations as before
                if design_iter >= self.max_design_iterations:
                    logger.warning(
                        f"[DesignLoop] Max design iterations "
                        f"({self.max_design_iterations}) reached with "
                        f"{gap_count} gaps remaining"
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
        """
        Execute chunks with parallelism for independent chunks.

        v1.2: Chunks with same wave_order and no cross-dependency run
        concurrently. Drift adjustment happens after each wave completes.
        """
        all_success = True

        # Clean progress: remove entries not in current chunk list
        current_ids = {c.chunk_id for c in chunks}
        progress = self._load_progress()
        stale = [k for k in progress if k not in current_ids]
        if stale:
            for k in stale:
                del progress[k]
            progress_path = os.path.join(self.mesh_dir, "design-progress.json")
            with open(progress_path, 'w') as f:
                json.dump(progress, f, indent=2, ensure_ascii=False)
            logger.info(
                f"[DesignLoop] Cleaned {len(stale)} stale progress entries: {stale}"
            )

        # v1.3: --jump-to: skip all chunks before the target
        jump_to = self.config.get("jump_to", "")
        if jump_to:
            found = False
            for c in chunks:
                if c.chunk_id == jump_to:
                    found = True
                    break
                # Mark chunks before target as completed (skip them)
                progress = self._load_progress()
                if progress.get(c.chunk_id, {}).get("status") != "completed":
                    self._save_progress(c.chunk_id, "completed", {
                        "success": True, "skipped": True,
                        "tasks": 0, "cost_usd": 0, "cycles": 0,
                        "final_gaps": 0, "jump_skipped": True,
                    })
                    logger.info(f"[DesignLoop] ⏩ Jump: skipping {c.chunk_id}")
            if not found:
                logger.warning(f"[DesignLoop] --jump-to target not found: {jump_to}")

        # Group chunks by wave_order for parallel execution
        waves: dict[int, list[DesignChunk]] = {}
        for chunk in chunks:
            waves.setdefault(chunk.wave_order, []).append(chunk)

        completed_chunks: set[str] = set()

        for wave_num in sorted(waves.keys()):
            # v1.2: graceful shutdown — check for stop signal
            if self._should_stop():
                self._emit_event("STOP 🛑 graceful shutdown requested")
                logger.info("[DesignLoop] 🛑 Stop signal detected, finishing gracefully")
                return all_success

            wave_chunks = waves[wave_num]

            # Filter: only run chunks whose dependencies are met
            ready_chunks = []
            for chunk in wave_chunks:
                progress = self._load_progress()
                if progress.get(chunk.chunk_id, {}).get("status") == "completed":
                    completed_chunks.add(chunk.chunk_id)
                    logger.info(
                        f"[DesignLoop] ⏭ {chunk.chunk_id} already completed"
                    )
                    continue
                unmet = [
                    d for d in chunk.depends_on_chunks
                    if d not in completed_chunks
                ]
                if unmet:
                    self._emit_event(f"CHUNK_SKIP ⏭ {chunk.chunk_id} (unmet deps: {unmet})")
                    logger.warning(
                        f"[DesignLoop] ⏭ {chunk.chunk_id} skipped "
                        f"(unmet deps: {unmet})"
                    )
                    continue
                ready_chunks.append(chunk)

            if not ready_chunks:
                continue

            if len(ready_chunks) == 1:
                # Single chunk — run directly (no parallel overhead)
                chunk = ready_chunks[0]
                logger.info(f"\n{'─'*50}")
                logger.info(
                    f"  📦 Chunk: {chunk.chunk_id} — {chunk.title}"
                )
                logger.info(
                    f"  Wave: {chunk.wave_order}, "
                    f"Changes: {len(chunk.changes)}"
                )
                logger.info(f"{'─'*50}")

                success = await self._execute_single_chunk(
                    chunk, chunks, new_spec,
                    max_inner_cycles, max_parallel, no_review,
                )
                if success:
                    completed_chunks.add(chunk.chunk_id)
                else:
                    all_success = False
            else:
                # Multiple independent chunks — run sequentially
                # Each chunk completes fully before next starts,
                # so each sees previous chunk's changes on main.
                logger.info(f"\n{'='*50}")
                logger.info(
                    f"  📦 Sequential wave {wave_num}: "
                    f"{len(ready_chunks)} chunks"
                )
                for c in ready_chunks:
                    logger.info(f"    • {c.chunk_id}: {c.title}")
                logger.info(f"{'='*50}")

                for chunk in ready_chunks:
                    logger.info(f"\n{'─'*50}")
                    logger.info(
                        f"  📦 Chunk: {chunk.chunk_id} — {chunk.title}"
                    )
                    logger.info(f"  Changes: {len(chunk.changes)}")
                    logger.info(f"{'─'*50}")

                    success = await self._execute_single_chunk(
                        chunk, chunks, new_spec,
                        max_inner_cycles, max_parallel, no_review,
                    )
                    if success:
                        completed_chunks.add(chunk.chunk_id)
                    else:
                        all_success = False

        return all_success

    async def _execute_single_chunk(
        self,
        chunk: DesignChunk,
        all_chunks: list[DesignChunk],
        new_spec: str,
        max_inner_cycles: int,
        max_parallel: int,
        no_review: bool,
        target_branch: str = "main",
    ) -> bool:
        """Execute one chunk and handle progress/validation."""
        chunk.status = "in_progress"
        self._save_progress(chunk.chunk_id, "in_progress", {})
        self._emit_event(f"CHUNK_START {chunk.chunk_id} — {chunk.title} ({len(chunk.changes)} tasks)")
        self.run_history.start_chunk(chunk.chunk_id, chunk.title, chunk.wave_order)

        total_attempts = 0

        while True:
            result = await self._run_chunk(
                chunk, max_inner_cycles, max_parallel, no_review,
                target_branch=target_branch,
            )
            total_attempts += 1

            if result.get("success"):
                break

            # After exhausting all cycles, ask user
            cycles_used = result.get("cycles", max_inner_cycles)

            total_cycles = total_attempts * cycles_used

            # Ask user via Discord + file signal
            decision = await self._ask_chunk_approval(
                chunk, result, total_cycles
            )

            if decision == "skip":
                logger.info(
                    f"[DesignLoop] User chose to SKIP {chunk.chunk_id}"
                )
                self._emit_event(
                    f"CHUNK_SKIPPED {chunk.chunk_id} — user decision"
                )
                # Mark as completed (skipped) so resume doesn't re-run
                skip_result = {
                    "success": True,
                    "skipped": True,
                    "tasks": result.get("tasks", 0),
                    "cost_usd": result.get("cost_usd", 0),
                    "cycles": total_cycles,
                    "final_gaps": len(result.get("final_issues", [])),
                    "final_issues": result.get("final_issues", []),
                }
                self._save_progress(chunk.chunk_id, "completed", skip_result)
                self.run_history.end_chunk(
                    chunk.chunk_id, "skipped",
                    len(result.get("final_issues", [])),
                )
                return True
            elif decision == "continue":
                logger.info(
                    f"[DesignLoop] User chose to CONTINUE {chunk.chunk_id}"
                )
                self._emit_event(
                    f"CHUNK_RETRY {chunk.chunk_id} — user approved"
                )
                # Clear cached plan so it re-plans from gaps
                plan_path = os.path.join(
                    self.mesh_dir, f"{chunk.chunk_id}-plan.json"
                )
                if os.path.exists(plan_path):
                    os.remove(plan_path)
                continue
            else:
                # Timeout or error → treat as skip
                logger.info(
                    f"[DesignLoop] Approval timeout for {chunk.chunk_id}, skipping"
                )
                skip_result = {
                    "success": True,
                    "skipped": True,
                    "tasks": result.get("tasks", 0),
                    "cost_usd": result.get("cost_usd", 0),
                    "cycles": total_cycles,
                    "final_gaps": len(result.get("final_issues", [])),
                    "final_issues": result.get("final_issues", []),
                }
                self._save_progress(chunk.chunk_id, "completed", skip_result)
                self.run_history.end_chunk(
                    chunk.chunk_id, "skipped",
                    len(result.get("final_issues", [])),
                )
                return True

        validation = await self._validate_chunk(chunk, result)

        if result.get("success"):
            chunk.status = "completed"
            self._save_progress(chunk.chunk_id, "completed", result)
            self._emit_event(f"CHUNK_END ✅ {chunk.chunk_id} completed")
            self.run_history.end_chunk(
                chunk.chunk_id, "completed", result.get("final_gaps", 0)
            )
            self.chunk_history.append({
                "chunk_id": chunk.chunk_id,
                "result": result,
                "validation": validation,
            })

            # Drift adjustment (only for sequential chunks)
            remaining = [c for c in all_chunks if c.status == "pending"]
            if remaining and (
                validation.get("design_issues")
                or validation.get("drift_notes")
            ):
                await self.refiner.adjust_remaining_chunks(
                    chunk, validation, remaining, new_spec
                )
            return True
        else:
            chunk.status = "needs_redesign"
            self._save_progress(chunk.chunk_id, "needs_redesign", result)
            self._emit_event(f"CHUNK_END ❌ {chunk.chunk_id} failed — {result.get('error', 'unknown')}")
            self.run_history.end_chunk(
                chunk.chunk_id, "failed", result.get("final_gaps", 0)
            )
            logger.warning(
                f"[DesignLoop] ❌ Chunk {chunk.chunk_id} failed: "
                f"{result.get('error', 'unknown')}"
            )
            return False

    async def _ask_chunk_approval(
        self, chunk: DesignChunk, result: dict, total_cycles: int
    ) -> str:
        """
        Ask user whether to continue or skip a failing chunk.
        Sends Discord notification, polls for file signal.
        Returns: 'continue', 'skip', or 'timeout'.
        """
        chunk_id = chunk.chunk_id
        gaps = result.get("final_gaps", "?")
        error = result.get("error", "unknown")

        # Signal file paths
        continue_file = os.path.join(self.mesh_dir, f"CONTINUE-{chunk_id}")
        skip_file = os.path.join(self.mesh_dir, f"SKIP-{chunk_id}")

        # Clean up any stale signals
        for f in [continue_file, skip_file]:
            if os.path.exists(f):
                os.remove(f)

        # Send Discord notification
        ssh_host = self.config.get("notifications", {}).get("ssh_host", "mybox")
        mesh_path = self.mesh_dir
        msg = (
            f"⚠️ **{chunk_id}** failed after {total_cycles} cycles "
            f"({gaps} gaps remaining, error: {error})\n\n"
            f"Reply:\n"
            f"`ssh {ssh_host} 'touch {mesh_path}/CONTINUE-{chunk_id}'` → retry\n"
            f"`ssh {ssh_host} 'touch {mesh_path}/SKIP-{chunk_id}'` → skip"
        )
        self._emit_event(
            f"CHUNK_APPROVAL_NEEDED {chunk_id} — {total_cycles} cycles, "
            f"{gaps} gaps"
        )
        if self._discord_webhook:
            self._discord_send(msg)

        # Poll for response (check every 30s, timeout after 2 hours)
        poll_interval = 30
        max_wait = 7200  # 2 hours
        waited = 0

        logger.info(
            f"[DesignLoop] Waiting for user decision on {chunk_id} "
            f"(CONTINUE/SKIP file)..."
        )

        while waited < max_wait:
            if os.path.exists(continue_file):
                os.remove(continue_file)
                return "continue"
            if os.path.exists(skip_file):
                os.remove(skip_file)
                return "skip"
            if self._should_stop():
                return "skip"
            await asyncio.sleep(poll_interval)
            waited += poll_interval

        # Timeout → auto-skip
        logger.warning(
            f"[DesignLoop] Approval timeout for {chunk_id}, auto-skipping"
        )
        self._emit_event(f"CHUNK_APPROVAL_TIMEOUT {chunk_id} — auto-skip")
        return "timeout"

    async def _execute_parallel_wave_sync(
        self,
        ready_chunks: list[DesignChunk],
        all_chunks: list[DesignChunk],
        new_spec: str,
        max_inner_cycles: int,
        max_parallel: int,
        no_review: bool,
    ) -> bool:
        """
        Synchronized parallel wave execution.

        Combines all chunks' tasks into ONE plan, runs ONE ProjectLoop:
          Code (parallel worktrees) → Merge+Build (sequential) →
          Verify (once) → Fix (parallel) → Merge+Build → Verify → ...

        This prevents cross-contamination that occurred when each chunk
        had its own independent ProjectLoop merging to main unsynchronized.
        """
        from ..context.store import ContextStore
        from ..models.task import TaskPlan
        from .change_converter import convert_changes_to_plan
        from .dispatcher import Dispatcher
        from .project_loop import ProjectLoop
        from .spec_analyzer import get_code_tree
        from types import SimpleNamespace

        chunk_ids = [c.chunk_id for c in ready_chunks]
        wave_label = ", ".join(chunk_ids)

        # ── 1. Emit start events ──
        for chunk in ready_chunks:
            chunk.status = "in_progress"
            self._save_progress(chunk.chunk_id, "in_progress", {})
            self._emit_event(
                f"CHUNK_START {chunk.chunk_id} — {chunk.title} "
                f"({len(chunk.changes)} changes) [sync wave]"
            )
            self.run_history.start_chunk(
                chunk.chunk_id, chunk.title, chunk.wave_order
            )

        # ── 2. Build combined plan from all chunks ──
        code_tree = await get_code_tree(self.repo_dir, char_limit=20_000)
        all_tasks = []
        all_partial_specs = []

        for chunk in ready_chunks:
            # Write per-chunk spec (for reference)
            chunk_spec_path = os.path.join(
                self.mesh_dir, f"{chunk.chunk_id}-spec.md"
            )
            with open(chunk_spec_path, 'w') as f:
                f.write(chunk.partial_spec)
            all_partial_specs.append(chunk.partial_spec)

            # Generate or load per-chunk plan
            plan_path = os.path.join(
                self.mesh_dir, f"{chunk.chunk_id}-plan.json"
            )
            if os.path.exists(plan_path):
                logger.info(f"[SyncWave] Loading cached plan: {plan_path}")
                with open(plan_path) as f:
                    plan_data = json.load(f)
                tasks = plan_data.get("tasks", [])
            else:
                logger.info(
                    f"[SyncWave] Converting {len(chunk.changes)} changes "
                    f"→ tasks for {chunk.chunk_id}..."
                )
                plan_dict = convert_changes_to_plan(
                    changes=chunk.changes,
                    project_name=os.path.basename(self.repo_dir),
                    shared_context={"chunk": chunk.chunk_id},
                    chunk_title=chunk.title,
                )
                tasks = plan_dict.get("tasks", [])
                with open(plan_path, 'w') as f:
                    json.dump(plan_dict, f, indent=2, ensure_ascii=False)

            # Prefix task IDs with chunk_id for global uniqueness
            for task in tasks:
                if not task["id"].startswith(f"{chunk.chunk_id}/"):
                    old_id = task["id"]
                    task["id"] = f"{chunk.chunk_id}/{old_id}"
                    task["dependencies"] = [
                        f"{chunk.chunk_id}/{d}"
                        if not d.startswith(f"{chunk.chunk_id}/")
                        else d
                        for d in task.get("dependencies", [])
                    ]

            all_tasks.extend(tasks)

        if not all_tasks:
            logger.warning("[SyncWave] No tasks from any chunk")
            return False

        # Save combined plan
        combined_plan_path = os.path.join(
            self.mesh_dir, "sync-wave-plan.json"
        )
        combined_plan = {
            "project_name": os.path.basename(self.repo_dir),
            "shared_context": {
                "chunks": chunk_ids, "sync_wave": True,
            },
            "modules": {},
            "tasks": all_tasks,
        }
        for task in all_tasks:
            mod = task.get("module", "")
            if mod and mod not in combined_plan["modules"]:
                combined_plan["modules"][mod] = {
                    "description": f"Module: {mod}",
                    "interface_files": [],
                    "imports": [],
                    "exports": [],
                }
        with open(combined_plan_path, 'w') as f:
            json.dump(combined_plan, f, indent=2, ensure_ascii=False)

        logger.info(
            f"[SyncWave] Combined plan: {len(all_tasks)} tasks "
            f"from {len(ready_chunks)} chunks → {combined_plan_path}"
        )

        # ── 3. Write combined spec for verification ──
        combined_spec_path = os.path.join(
            self.mesh_dir, "sync-wave-spec.md"
        )
        combined_spec_content = "\n\n---\n\n".join(all_partial_specs)
        combined_spec_content += (
            f"\n\n## CURRENT PROJECT STRUCTURE\n"
            f"IMPORTANT: Use the ACTUAL file paths below. "
            f"Do NOT invent paths.\n\n{code_tree}\n"
        )
        with open(combined_spec_path, 'w') as f:
            f.write(combined_spec_content)

        # ── 4. Setup stores ──
        store = ContextStore(self.repo_dir)
        exp_store = None
        advisor = None
        project_name = os.path.basename(self.repo_dir)
        project_type = "unknown"
        total_cost = 0.0
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
                classify_result = classifier.classify(self.repo_dir)
                project_type = classify_result["project_type"]
            advisor = ExperienceAdvisor(exp_store, project_type)
        except Exception:
            pass

        # ── 5. Run ProjectLoop with approval retry ──
        total_attempts = 0
        success = False
        current_plan_path = combined_plan_path

        while True:
            class _SyncDispatcher:
                """Dispatcher for synchronized wave — all tasks merge to main."""
                def __init__(self_, plan_path: str,
                             max_parallel: int = 4,
                             no_review: bool = True):
                    nonlocal total_cost
                    with open(plan_path) as f:
                        plan_data = json.load(f)
                    self_.plan = TaskPlan.from_dict(plan_data)
                    self_.run_id = store.save_plan(self_.plan)
                    cfg = {**self.config, "no_review": no_review}
                    cfg.setdefault("dispatcher", {})["max_parallel"] = \
                        max_parallel
                    self_.dispatcher = Dispatcher(
                        cfg, self.repo_dir, store,
                        experience_store=exp_store,
                        project_name=project_name,
                        project_type=project_type,
                        target_branch="main",
                        slot_prefix="slot",
                    )
                    if advisor:
                        self_.dispatcher.router.advisor = advisor
                    # Expose router for ProjectLoop cycle escalation
                    self_.router = self_.dispatcher.router

                async def run(self_):
                    nonlocal total_cost
                    await self_.dispatcher.execute_plan(
                        plan=self_.plan,
                        run_id=self_.run_id,
                        resume=True,
                    )
                    total_cost += self_.dispatcher.wave_cost_usd
                    # v1.3: expose execution data for run history
                    self_.cost_usd = self_.dispatcher.wave_cost_usd
                    self_.task_summaries = self_.dispatcher.task_summaries
                    return "done"

            loop = ProjectLoop(
                self.config, self.repo_dir, combined_spec_path,
                run_history=self.run_history,
            )
            try:
                success = await loop.run_auto(
                    max_cycles=max_inner_cycles,
                    dispatcher_factory=_SyncDispatcher,
                    initial_plan_path=current_plan_path,
                    max_parallel=max_parallel,
                    no_review=no_review,
                    skip_initial_verify=True,
                    manual_mode=self.config.get("manual_mode", False),
                )
            except Exception as e:
                logger.error(f"[SyncWave] Execution error: {e}")
                success = False

            total_attempts += 1
            if success:
                break

            # Ask user for approval to retry
            final_gaps = 0
            if loop.cycle_history:
                final_gaps = loop.cycle_history[-1].get("gap_count", 0)

            pseudo_result = {
                "success": False,
                "final_gaps": final_gaps,
                "error": f"Sync wave ({wave_label}) failed",
                "cycles": len(loop.cycle_history),
            }

            total_cycles = total_attempts * max_inner_cycles
            wave_proxy = SimpleNamespace(chunk_id="sync-wave")
            decision = await self._ask_chunk_approval(
                wave_proxy, pseudo_result, total_cycles
            )

            if decision == "continue":
                logger.info("[SyncWave] User chose to CONTINUE")
                self._emit_event(
                    f"SYNC_WAVE_RETRY ({wave_label}) — user approved"
                )
                # Use last cycle's fix-plan as next starting point
                last_cycle = len(loop.cycle_history)
                fix_plan = os.path.join(
                    self.repo_dir,
                    f".agent-mesh/fix-plan-{last_cycle}.json",
                )
                if os.path.exists(fix_plan):
                    current_plan_path = fix_plan
                continue
            else:
                break

        # ── 6. Cleanup ──
        store.close()
        if exp_store:
            if total_cost > 0:
                exp_store.add_project_cost(project_name, total_cost)
            exp_store.close()

        # ── 7. Update chunk statuses ──
        result_dict = {
            "success": success,
            "tasks": len(all_tasks),
            "cost_usd": total_cost,
        }
        # Get final gap count from loop history (shared across sync wave chunks)
        sync_final_gaps = (
            loop.cycle_history[-1].get("gap_count", 0)
            if loop.cycle_history else 0
        )

        for chunk in ready_chunks:
            if success:
                chunk.status = "completed"
                self._save_progress(chunk.chunk_id, "completed", result_dict)
                self._emit_event(f"CHUNK_END ✅ {chunk.chunk_id} [sync wave]")
                self.run_history.end_chunk(chunk.chunk_id, "completed", 0)
                self.chunk_history.append({
                    "chunk_id": chunk.chunk_id,
                    "result": result_dict,
                    "validation": {"design_issues": [], "drift_notes": ""},
                })
            else:
                chunk.status = "needs_redesign"
                self._save_progress(
                    chunk.chunk_id, "needs_redesign", result_dict
                )
                self._emit_event(f"CHUNK_END ❌ {chunk.chunk_id} [sync wave]")
                self.run_history.end_chunk(chunk.chunk_id, "failed", sync_final_gaps)
                logger.warning(
                    f"[SyncWave] ❌ Chunk {chunk.chunk_id} failed"
                )

        # Drift adjustment for remaining chunks
        if success:
            remaining = [c for c in all_chunks if c.status == "pending"]
            if remaining and self.chunk_history:
                last_validation = self.chunk_history[-1].get(
                    "validation", {}
                )
                if (last_validation.get("design_issues")
                        or last_validation.get("drift_notes")):
                    for chunk in ready_chunks:
                        await self.refiner.adjust_remaining_chunks(
                            chunk, last_validation, remaining, new_spec
                        )

        return success

    async def _run_chunk(
        self,
        chunk: DesignChunk,
        max_inner_cycles: int,
        max_parallel: int,
        no_review: bool,
        target_branch: str = "main",
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
        from .change_converter import convert_changes_to_plan
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

        # Convert changes directly to tasks (cached if plan.json exists)
        plan_path = os.path.join(self.mesh_dir, f"{chunk.chunk_id}-plan.json")
        if os.path.exists(plan_path):
            logger.info(f"[DesignLoop] Loading cached plan → {plan_path}")
            plan = Planner.load_plan(plan_path)
        else:
            logger.info(
                f"[DesignLoop] Converting {len(chunk.changes)} changes → tasks..."
            )
            project_name = os.path.basename(self.repo_dir)
            plan_dict = convert_changes_to_plan(
                changes=chunk.changes,
                project_name=project_name,
                shared_context={"chunk": chunk.chunk_id, "title": chunk.title},
                chunk_title=chunk.title,
            )

            if not plan_dict.get("tasks"):
                logger.warning(f"[DesignLoop] No tasks for {chunk.chunk_id}")
                return {"success": False, "error": "No tasks from changes"}

            plan = TaskPlan.from_dict(plan_dict)

            # Save as plan.json for resume
            with open(plan_path, 'w') as f:
                json.dump(plan_dict, f, indent=2, ensure_ascii=False)
            logger.info(f"[DesignLoop] Saved plan → {plan_path}")
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
                    target_branch=target_branch,
                    slot_prefix=chunk.chunk_id,
                )
                if advisor:
                    self_.dispatcher.router.advisor = advisor
                # Expose router for ProjectLoop cycle escalation
                self_.router = self_.dispatcher.router

            async def run(self_):
                nonlocal total_cost
                await self_.dispatcher.execute_plan(
                    plan=self_.plan,
                    run_id=self_.run_id,
                    resume=True,
                )
                total_cost += self_.dispatcher.wave_cost_usd
                # v1.3: expose execution data for run history
                self_.cost_usd = self_.dispatcher.wave_cost_usd
                self_.task_summaries = self_.dispatcher.task_summaries
                return "done"

        # v1.3: set current chunk for checkpoint tracking
        self.config["current_chunk_id"] = chunk.chunk_id

        loop = ProjectLoop(self.config, self.repo_dir, spec_path,
                           run_history=self.run_history)

        try:
            success = await loop.run_auto(
                max_cycles=max_inner_cycles,
                dispatcher_factory=_ChunkDispatcher,
                initial_plan_path=plan_path,
                max_parallel=max_parallel,
                no_review=no_review,
                skip_initial_verify=True,  # v1.2: plan from design → skip full verify
                manual_mode=self.config.get("manual_mode", False),
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
        plan_dict = gap_analyzer.generate_fix_plan(report)

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

    async def _wait_for_signal(self, phase: str, summary: str) -> str:
        """Wait for manual signal (CONTINUE/SKIP/STOP) via touch files."""
        mesh_dir = self.mesh_dir

        status_path = os.path.join(mesh_dir, "MANUAL-STATUS.txt")
        with open(status_path, 'w') as f:
            f.write(f"=== MANUAL MODE: {phase} ===\n\n")
            f.write(summary)
            f.write(f"\n\n--- Actions ---\n")
            f.write(f"touch {mesh_dir}/CONTINUE  → proceed\n")
            f.write(f"touch {mesh_dir}/SKIP      → skip\n")
            f.write(f"touch {mesh_dir}/STOP      → stop entirely\n")

        continue_file = os.path.join(mesh_dir, "CONTINUE")
        skip_file = os.path.join(mesh_dir, "SKIP")
        stop_file = os.path.join(mesh_dir, "STOP")

        for f in [continue_file, skip_file]:
            if os.path.exists(f):
                os.remove(f)

        logger.info(f"\n{'='*60}")
        logger.info(f"  ⏸️  MANUAL PAUSE: {phase}")
        logger.info(f"{'='*60}")
        logger.info(summary)
        logger.info(f"\nWaiting for signal: touch CONTINUE / SKIP / STOP")
        logger.info(f"Status file: {status_path}")

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
        """Clear progress file and chunk caches for a fresh design iteration."""
        progress_path = os.path.join(self.mesh_dir, "design-progress.json")
        if os.path.exists(progress_path):
            os.rename(
                progress_path,
                progress_path.replace(".json", f"-{int(time.time())}.json"),
            )

        # Clean up chunk-specific cache files to avoid iter1/iter2 collisions
        import glob
        for pattern in ["chunk-*-spec.md", "chunk-*-plan.json"]:
            for f in glob.glob(os.path.join(self.mesh_dir, pattern)):
                try:
                    os.remove(f)
                except Exception:
                    pass

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

    async def _run_git(self, cmd: str):
        """Run a git command in the repo directory."""
        proc = await asyncio.create_subprocess_shell(
            f"git {cmd}",
            cwd=self.repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(f"git {cmd}: {err}")
        return stdout.decode().strip()
