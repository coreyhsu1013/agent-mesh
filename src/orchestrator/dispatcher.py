"""
Agent Mesh v0.7 — Dispatcher (Wave-based)

v0.7 改進：
- Wave-based merge: 執行期間 main 不動，Wave 結束統一 merge
- Worker pool: slot 數量上限 = max_parallel，完成即回收填入下一個 task
- 記憶體用量永遠 <= max_parallel × 300MB
- merge 順序可控，衝突大幅減少
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Optional

from ..models.task import Task, TaskPlan, TaskStatus, AgentType
from ..context.store import ContextStore
from ..auth.aider_runner import AiderRunner, ClaudeRunner, heartbeat_wait
from ..gates.runner import GateRunner
from ..gates.registry import GateRegistry
from .router import ModelRouter
from .react_loop import ReactLoop, TaskResult
from .reviewer import Reviewer
from .workspace import WorkspacePool
from .cost_tracker import CostTracker

logger = logging.getLogger(__name__)

# ── Non-code file patterns (safe to skip build check) ──
_DOCS_ONLY_EXTENSIONS = frozenset({
    ".md", ".txt", ".rst", ".adoc",          # documentation
    ".png", ".jpg", ".jpeg", ".gif", ".svg", # images
    ".ico", ".webp",
})
_DOCS_ONLY_DIRS = ("docs/", "doc/", ".github/", ".vscode/")
_DOCS_ONLY_BASENAMES = frozenset({
    "license", "changelog", "authors", "contributors",
    "code_of_conduct", ".gitignore", ".gitattributes",
    ".editorconfig",
})


def _should_run_build_check(changed_files: list[str]) -> bool:
    """
    Return True if build check should run after merge.
    Returns False (skip build) only when ALL changed files are non-code.
    Conservative: empty list or any ambiguous file → run build.
    """
    if not changed_files:
        return True  # no info → be safe, run build

    for f in changed_files:
        f_lower = f.lower()
        basename = os.path.basename(f_lower)
        _, ext = os.path.splitext(f_lower)

        # Check extension
        if ext in _DOCS_ONLY_EXTENSIONS:
            continue
        # Check directory prefix
        if any(f_lower.startswith(d) for d in _DOCS_ONLY_DIRS):
            continue
        # Check known non-code basenames
        if basename in _DOCS_ONLY_BASENAMES:
            continue

        # This file could affect build → must run
        return True

    return False


class Dispatcher:

    def __init__(self, config: dict, repo_dir: str, store: ContextStore,
                 experience_store=None, project_name: str = "",
                 project_type: str = "",
                 target_branch: str = "main",
                 slot_prefix: str = "slot"):
        self.config = config
        self.repo_dir = repo_dir
        self.store = store

        self.router = ModelRouter(config)
        self.react_loop = ReactLoop(config)
        self.reviewer = Reviewer(config, repo_dir)
        self.gate_runner = GateRunner(GateRegistry())
        self.pool = WorkspacePool(repo_dir, config, target_branch, slot_prefix)

        aider = AiderRunner(config)
        self.runners = {
            AgentType.CLAUDE_CODE: ClaudeRunner(config),
            AgentType.DEEPSEEK_AIDER: aider,
            AgentType.GROK_AIDER: aider,  # same runner, different AgentType
        }

        disp_cfg = config.get("dispatcher", {})
        self.max_parallel = disp_cfg.get("max_parallel", 4)
        self.semaphore_claude = asyncio.Semaphore(disp_cfg.get("semaphore_claude", 2))
        self.semaphore_deepseek = asyncio.Semaphore(disp_cfg.get("semaphore_deepseek", 3))
        self.global_semaphore = asyncio.Semaphore(self.max_parallel)

        self.shared_context = ""
        self.no_review = config.get("no_review", False)

        # v0.9: cost tracking + experience
        self.cost_tracker = CostTracker()
        self.experience_store = experience_store  # ExperienceStore | None
        self.project_name = project_name or os.path.basename(repo_dir)
        self.project_type = project_type
        self.wave_cost_usd = 0.0  # accumulates per wave
        # v1.3: per-task execution summary for run history
        self.task_summaries: list[dict] = []

    async def execute_plan(
        self,
        plan: TaskPlan,
        run_id: str,
        modules: list[str] | None = None,
        waves: list[int] | None = None,
        resume: bool = False,
    ):
        if plan.shared_context:
            self.shared_context = json.dumps(plan.shared_context, indent=2)

        tasks = plan.tasks
        if modules:
            tasks = [t for t in tasks if t.module in modules]
        if resume:
            db_tasks = self.store.get_all_tasks()
            completed_from_db = {t.id for t in db_tasks if t.status == TaskStatus.COMPLETED.value}
            tasks = [t for t in tasks if t.id not in completed_from_db]
            logger.info(f"[Dispatcher] Resume: {len(completed_from_db)} already done, {len(tasks)} remaining")

        if not tasks:
            logger.info("[Dispatcher] No tasks to execute")
            return

        # Apply complexity floor for foundational tasks
        for t in tasks:
            self.router.apply_complexity_floor(t)

        self._print_routing_preview(tasks)

        # Build completed/failed sets
        completed_ids = set()
        failed_ids = set()
        if resume:
            completed_ids = {t.id for t in self.store.get_completed_tasks()}
        else:
            completed_ids = {t.id for t in plan.tasks if t.status == TaskStatus.COMPLETED.value}

        pending = list(tasks)
        wave_num = 0

        while pending:
            wave_num += 1

            # ★ Cascade propagation: skip tasks blocked by failed deps
            while True:
                blocked = [
                    t for t in pending
                    if any(dep in failed_ids for dep in t.dependencies)
                ]
                if not blocked:
                    break
                for t in blocked:
                    t.status = TaskStatus.FAILED.value
                    t.error = "Blocked: upstream dependency failed"
                    self.store.update_task(t)
                    failed_ids.add(t.id)
                    pending.remove(t)
                    logger.warning(f"[Dispatcher] ⏭️ '{t.title}' skipped (upstream failed)")

            if not pending:
                break

            ready = [
                t for t in pending
                if all(dep in completed_ids for dep in t.dependencies)
            ]

            if not ready:
                logger.error(
                    f"[Dispatcher] Wave {wave_num}: deadlock — "
                    f"{len(pending)} tasks stuck"
                )
                for t in pending:
                    unmet = [d for d in t.dependencies if d not in completed_ids]
                    logger.error(f"  - {t.title}: waiting on {unmet}")
                break

            if waves and wave_num not in waves:
                for t in ready:
                    pending.remove(t)
                    completed_ids.add(t.id)
                continue

            # ═══════════════════════════════════════════
            # ★ Phase 1: Setup worker slots (capped at max_parallel)
            # ═══════════════════════════════════════════
            n_workers = min(len(ready), self.max_parallel)
            logger.info(
                f"\n{'='*60}\n"
                f"  Wave {wave_num}: {len(ready)} tasks ({n_workers} slots)\n"
                f"{'='*60}"
            )

            await self.pool.setup_wave(n_workers)

            # ═══════════════════════════════════════════
            # ★ Phase 2: Worker pool — slot recycling
            #   Workers pick tasks from queue; each finishes a task,
            #   commits, then picks the next. At most max_parallel
            #   worktrees exist at any time.
            # ═══════════════════════════════════════════
            task_queue: asyncio.Queue[tuple[int, Task]] = asyncio.Queue()
            for idx, task in enumerate(ready):
                task_queue.put_nowait((idx, task))

            results_lock = asyncio.Lock()
            task_results: dict[int, tuple[Task, object]] = {}

            async def _worker(slot_id: int):
                while True:
                    try:
                        task_idx, task = task_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    result = None
                    try:
                        ws_dir = await self.pool.prepare_slot_for_task(
                            slot_id, task_idx
                        )
                        result = await self._execute_task_in_slot(
                            task=task,
                            slot_id=slot_id,
                            workspace_dir=ws_dir,
                        )
                    except Exception as exc:
                        result = exc

                    # Always commit to keep slot clean for recycling
                    try:
                        await self.pool.commit_slot_task(
                            slot_id, f"[agent-mesh] {task.title}"
                        )
                    except Exception:
                        pass

                    async with results_lock:
                        task_results[task_idx] = (task, result)

            await asyncio.gather(
                *[_worker(i) for i in range(n_workers)]
            )

            # ── Collect results ──
            completed_indices: list[int] = []
            task_labels: dict[int, str] = {}
            wave_results: dict[str, TaskResult] = {}

            for task_idx, (task, result) in task_results.items():
                if isinstance(result, Exception):
                    logger.error(
                        f"[Dispatcher] '{task.title}' exception: {result}"
                    )
                    task.status = TaskStatus.FAILED.value
                    task.error = str(result)
                    self.store.update_task(task)
                    failed_ids.add(task.id)
                elif result and result.status == "completed":
                    completed_indices.append(task_idx)
                    model_short = (
                        result.final_model or "unknown"
                    ).split("/")[-1]
                    task_labels[task_idx] = f"{model_short}: {task.title}"
                    wave_results[task.id] = result
                else:
                    task.status = TaskStatus.FAILED.value
                    task.error = result.error if result else "Unknown failure"
                    self.store.update_task(task)
                    failed_ids.add(task.id)
                    logger.warning(
                        f"[Dispatcher] ❌ '{task.title}' failed "
                        f"({result.attempts if result else 0} attempts)"
                    )

            # ═══════════════════════════════════════════
            # ★ Phase 3: Sequential merge + build check
            #   Merge one task → build → fix if needed → next
            # ═══════════════════════════════════════════
            if completed_indices:
                build_cmd = self.config.get("verify", {}).get("build_cmd", "")
                merge_results: dict[int, bool] = {}

                logger.info(f"\n{'─'*40}")
                logger.info(f"  🔀 Merge+Build: {len(completed_indices)} tasks → main")
                logger.info(f"{'─'*40}")

                # Sort merge order: foundation files first (schema,
                # types, entities), then services, then routes/pages.
                # This reduces build errors from missing dependencies.
                def _merge_priority(idx: int) -> int:
                    task, _ = task_results[idx]
                    files = getattr(task, 'target_files', []) or []
                    files_str = ' '.join(files).lower()
                    if any(k in files_str for k in [
                        'schema', 'prisma', 'entity', 'entities',
                        'types', 'type.ts', 'enum', 'model',
                    ]):
                        return 0  # merge first
                    if any(k in files_str for k in [
                        'service', 'utils', 'helper', 'lib',
                        'middleware', 'config',
                    ]):
                        return 1
                    if any(k in files_str for k in [
                        'route', 'controller', 'api', 'handler',
                    ]):
                        return 2
                    if any(k in files_str for k in [
                        'page', 'component', 'view', 'screen',
                        'frontend', '.tsx', '.jsx',
                    ]):
                        return 3
                    return 2  # default: middle
                sorted_indices = sorted(
                    completed_indices, key=_merge_priority
                )

                for idx in sorted_indices:
                    label = task_labels.get(idx, f"task_{idx}")
                    commit_msg = f"[agent-mesh] {label}"

                    # 3a. Save checkpoint before merge (for rollback)
                    try:
                        pre_merge_head = await self.pool._run_git(
                            "rev-parse HEAD", cwd=self.repo_dir
                        )
                    except Exception:
                        pre_merge_head = ""

                    # 3b. Merge single branch
                    success = await self.pool.merge_single(idx, commit_msg)
                    if not success:
                        merge_results[idx] = False
                        logger.warning(f"  ❌ task_{idx}: {label} (merge failed)")
                        continue

                    # 3c. Capture merge context for potential fix
                    try:
                        merged_diff = await self.pool._run_git(
                            "diff HEAD~1 HEAD", cwd=self.repo_dir
                        )
                    except Exception:
                        merged_diff = ""
                    changed_files = []
                    for line in merged_diff.split('\n'):
                        if line.startswith('diff --git'):
                            parts = line.split(' b/')
                            if len(parts) > 1:
                                changed_files.append(parts[1])

                    # 3d. Build check after merge (skip for docs-only)
                    if not _should_run_build_check(changed_files):
                        merge_results[idx] = True
                        logger.info(
                            f"  ✅ task_{idx}: {label} (docs-only, build skipped)"
                        )
                        continue

                    build_ok, build_output = await self.pool.run_build_check(build_cmd)
                    if build_ok:
                        merge_results[idx] = True
                        logger.info(f"  ✅ task_{idx}: {label}")
                        continue

                    # 3e. Build broke — fix with context + ReAct loop
                    logger.warning(
                        f"  ⚠️ task_{idx}: {label} — build broke, fixing..."
                    )
                    fixed = await self._fix_build_on_main(
                        build_output=build_output,
                        task_title=label,
                        build_cmd=build_cmd,
                        merged_diff=merged_diff,
                        changed_files=changed_files,
                    )
                    if fixed:
                        merge_results[idx] = True
                        logger.info(f"  🔧 task_{idx}: {label} (build fixed)")
                    else:
                        # 3f. Rollback: reset main to pre-merge state
                        if pre_merge_head:
                            logger.warning(
                                f"  ↩️ task_{idx}: {label} — rolling back merge "
                                f"(reset to {pre_merge_head[:8]})"
                            )
                            try:
                                await self.pool._run_git(
                                    f"reset --hard {pre_merge_head}",
                                    cwd=self.repo_dir,
                                )
                            except Exception as e:
                                logger.error(
                                    f"  ❌ Rollback failed: {e}"
                                )
                        merge_results[idx] = False

                # Post-merge: scan for conflict markers
                conflicts = await self.pool._scan_conflict_markers()
                if conflicts:
                    logger.warning(
                        f"[Dispatcher] ⚠️ {len(conflicts)} files with conflict markers"
                    )

                merged_count = sum(1 for v in merge_results.values() if v)
                failed_count = sum(1 for v in merge_results.values() if not v)
                logger.info(f"\n  Merge result: {merged_count} ✅ / {failed_count} ❌")

                for task_idx in completed_indices:
                    task, _ = task_results[task_idx]
                    tr = wave_results.get(task.id)
                    if merge_results.get(task_idx, False):
                        task.status = TaskStatus.COMPLETED.value
                        task.diff = tr.final_diff[:5000] if tr else ""
                        completed_ids.add(task.id)
                        dur = task.duration_sec
                        model = (
                            tr.final_model or "unknown"
                        ) if tr else "unknown"
                        logger.info(
                            f"[Dispatcher] ✅ '{task.title}' "
                            f"({model.split('/')[-1]}, "
                            f"{tr.attempts if tr else 0} att, {dur:.0f}s)"
                        )
                    else:
                        task.status = TaskStatus.FAILED.value
                        task.error = "Merge failed"
                        failed_ids.add(task.id)
                        logger.warning(
                            f"[Dispatcher] ❌ '{task.title}' merge failed"
                        )
                    self.store.update_task(task)

            # ═══════════════════════════════════════════
            # ★ Phase 3.5: Wave cost summary (v0.9)
            # ═══════════════════════════════════════════
            wave_cost = 0.0
            for task_idx, (task, result) in task_results.items():
                if isinstance(result, TaskResult) and result.total_cost_usd > 0:
                    wave_cost += result.total_cost_usd
            self.wave_cost_usd += wave_cost
            if wave_cost > 0:
                logger.info(
                    f"[Cost] Wave {wave_num} total: ${wave_cost:.4f} "
                    f"(cumulative: ${self.wave_cost_usd:.4f})"
                )

            # v1.3: collect per-task summaries for run history
            for task_idx, (task, result) in task_results.items():
                if isinstance(result, TaskResult):
                    self.task_summaries.append({
                        "task_id": task.id,
                        "title": task.title,
                        "status": result.status,
                        "attempts": result.attempts,
                        "final_model": result.final_model,
                        "duration_sec": round(result.total_duration_sec, 1),
                        "cost_usd": round(result.total_cost_usd, 4),
                    })

            # v0.9: refresh experience stats after each wave
            if self.experience_store:
                try:
                    self.experience_store.refresh_model_stats()
                except Exception:
                    pass

            # ═══════════════════════════════════════════
            # ★ Phase 4: Cleanup worker slots + task branches
            # ═══════════════════════════════════════════
            await self.pool.cleanup_wave()

            # Remove processed tasks from pending
            for task in ready:
                if task in pending:
                    pending.remove(task)

        self._print_summary(plan)

    async def _execute_task_in_slot(
        self, task: Task, slot_id: int, workspace_dir: str
    ) -> Optional[TaskResult]:
        """
        Execute a single task in its assigned slot.
        No merge here — just run the agent and return result.
        """
        task.status = TaskStatus.RUNNING.value
        self.store.update_task(task)
        start_time = time.time()

        # 1) Compute start_attempt (skip Grok for foundational/fix tasks)
        complexity = getattr(task, "complexity", "M")
        start_attempt = self.router.get_start_attempt(task)
        first_decision = self.router.get_model_for_attempt(complexity, start_attempt, log=False)
        task.routed_by = "manual" if task.agent_type else "auto"
        task.agent_used = f"{first_decision.agent_type.value}:{first_decision.model_short}"

        semaphore = self._get_semaphore(first_decision.agent_type)
        max_att = self.router.get_max_attempts(complexity)

        # Build chain preview for log (from start_attempt onward)
        chain_preview = []
        for i in range(start_attempt, max_att + 1):
            d = self.router.get_model_for_attempt(complexity, i, log=False)
            chain_preview.append(d.model_short)
        chain_str = " → ".join(chain_preview)

        try:
            async with self.global_semaphore:
                async with semaphore:
                    logger.info(
                        f"[Dispatcher] 🚀 '{task.title}' → "
                        f"{first_decision.agent_type.value} ({first_decision.model_short}) "
                        f"[slot_{slot_id}, {complexity}] chain: {chain_str}"
                    )

                    # 2) ReAct Loop — router picks model per attempt
                    result = await self.react_loop.execute_task(
                        task=task,
                        runners=self.runners,
                        router=self.router,
                        workspace_dir=workspace_dir,
                        shared_context=self.shared_context,
                        start_attempt=start_attempt,
                    )

                    task.attempts = result.attempts
                    task.duration_sec = time.time() - start_time
                    if result.history:
                        task.react_history = result.history.to_json()
                    if result.final_model:
                        model_short = result.final_model.split("/")[-1]
                        task.agent_used = f"{'escalated:' if result.attempts > 1 else ''}{model_short}"

                    # v0.9: estimate cost if not already tracked
                    if not result.cost_results and result.final_model:
                        # Fallback: estimate from task output
                        stdout_len = sum(
                            len(a.get("diff_summary", ""))
                            for a in (result.history.attempts if result.history else [])
                        )
                        est = self.cost_tracker.estimate_from_chars(
                            "x" * max(stdout_len, 500), result.final_model
                        )
                        result.cost_results = [est]
                        result.total_cost_usd = est.estimated_usd

                    # v0.9: log cost
                    if result.total_cost_usd > 0:
                        logger.info(
                            f"[Cost] {task.id}: ${result.total_cost_usd:.4f} "
                            f"({len(result.cost_results)} attempt(s), "
                            f"{result.final_model or 'unknown'})"
                        )

                    # v0.9: record to experience store
                    if self.experience_store and result.final_model:
                        try:
                            last_cost = result.cost_results[-1] if result.cost_results else None
                            error_type = None
                            if result.error:
                                if "timeout" in result.error.lower():
                                    error_type = "timeout"
                                elif "build" in result.error.lower():
                                    error_type = "build_fail"
                                elif "test" in result.error.lower():
                                    error_type = "test_fail"
                                elif "review" in result.error.lower():
                                    error_type = "review_reject"
                                else:
                                    error_type = "other"
                            self.experience_store.record_task_run(
                                project_name=self.project_name,
                                project_type=self.project_type,
                                task_id=task.id,
                                task_title=task.title,
                                complexity=complexity,
                                category=getattr(task, "category", ""),
                                module=getattr(task, "module", ""),
                                model_used=result.final_model,
                                attempt_number=result.attempts,
                                success=result.status == "completed",
                                duration_sec=task.duration_sec,
                                cost=last_cost,
                                error_type=error_type,
                            )
                        except Exception as e:
                            logger.debug(f"[Experience] Record failed: {e}")

                    # 3) Deterministic Gate (v2.0, before reviewer)
                    #    Gate retry: up to N retries with structured feedback.
                    #    If all gate retries fail, escalate to next model in
                    #    routing chain (e.g. Sonnet → Opus) before giving up.
                    max_gate_retries = self.config.get("gates", {}).get(
                        "max_retries", 2
                    )
                    gate_attempt = 0
                    # Track model level — use the attempt that actually
                    # produced this result (may differ from start_attempt
                    # if ReAct internally escalated).
                    current_chain_attempt = max(
                        result.attempts, start_attempt
                    )
                    max_total_gate_runs = (max_gate_retries + 1) * max_att + 1
                    total_gate_runs = 0
                    while True:
                        total_gate_runs += 1
                        if total_gate_runs > max_total_gate_runs:
                            logger.error(
                                f"[Gate] Safety limit reached for "
                                f"'{task.title}'"
                            )
                            result.status = "failed"
                            result.error = "Gate safety limit reached"
                            break

                        # ReAct failed — try escalating before giving up
                        if result.status != "completed":
                            next_chain_attempt = (
                                current_chain_attempt + 1
                            )
                            if next_chain_attempt <= max_att:
                                next_decision = (
                                    self.router.get_model_for_attempt(
                                        complexity, next_chain_attempt,
                                        log=False,
                                    )
                                )
                                logger.warning(
                                    f"[Gate] ⬆️ '{task.title}' ReAct "
                                    f"failed, escalating to "
                                    f"{next_decision.model_short} "
                                    f"(attempt {next_chain_attempt})"
                                )
                                current_chain_attempt = next_chain_attempt
                                gate_attempt = 0
                                result = (
                                    await self.react_loop.execute_task(
                                        task=task,
                                        runners=self.runners,
                                        router=self.router,
                                        workspace_dir=workspace_dir,
                                        shared_context=self.shared_context,
                                        start_attempt=current_chain_attempt,
                                        single_attempt=True,
                                    )
                                )
                                continue
                            else:
                                # No more models — keep failed result
                                break

                        gate_attempt += 1

                        gate_summary = await self.gate_runner.run(
                            task=task,
                            diff=result.final_diff,
                            workspace_dir=workspace_dir,
                        )
                        # Persist gate results (always keep latest)
                        task.gate_results = [
                            r.to_dict() for r in gate_summary.results
                        ]

                        if gate_summary.overall_passed:
                            task.gate_feedback = {}  # clear on success
                            break

                        # Gate failed — check if we have retries left
                        if gate_attempt <= max_gate_retries:
                            # Normal gate retry with same model
                            feedback = gate_summary.to_feedback(
                                attempt=gate_attempt
                            )
                            task.gate_feedback = feedback.to_dict()
                            logger.info(
                                f"[Gate] 🔄 '{task.title}' gate retry "
                                f"{gate_attempt}/{max_gate_retries} — "
                                f"failed: {', '.join(feedback.failed_checks)}"
                            )
                            result = await self.react_loop.execute_task(
                                task=task,
                                runners=self.runners,
                                router=self.router,
                                workspace_dir=workspace_dir,
                                shared_context=self.shared_context,
                                start_attempt=current_chain_attempt,
                                single_attempt=True,
                            )
                            continue

                        # Gate retries exhausted — try escalating model
                        next_chain_attempt = current_chain_attempt + 1
                        if next_chain_attempt <= max_att:
                            next_decision = self.router.get_model_for_attempt(
                                complexity, next_chain_attempt, log=False
                            )
                            logger.warning(
                                f"[Gate] ⬆️ '{task.title}' gate failed after "
                                f"{max_gate_retries} retries, escalating to "
                                f"{next_decision.model_short} "
                                f"(attempt {next_chain_attempt})"
                            )
                            feedback = gate_summary.to_feedback(
                                attempt=gate_attempt
                            )
                            task.gate_feedback = feedback.to_dict()
                            current_chain_attempt = next_chain_attempt
                            gate_attempt = 0  # reset gate retries for new model
                            result = await self.react_loop.execute_task(
                                task=task,
                                runners=self.runners,
                                router=self.router,
                                workspace_dir=workspace_dir,
                                shared_context=self.shared_context,
                                start_attempt=current_chain_attempt,
                                single_attempt=True,
                            )
                            continue
                        else:
                            # No more models — final failure
                            logger.warning(
                                f"[Gate] ❌ '{task.title}' gate failed after "
                                f"{max_gate_retries} retries (no escalation "
                                f"left): "
                                f"{', '.join(gate_summary.failed_checks)}"
                            )
                            result.status = "failed"
                            result.error = (
                                f"Gate failed: "
                                f"{', '.join(gate_summary.failed_checks)}"
                            )
                            break

                    # 4) Review (optional, only if gate passed)
                    if result.status == "completed" and not self.no_review:
                        review = await self.reviewer.review(
                            diff=result.final_diff,
                            task_title=task.title,
                            task_description=task.description,
                            acceptance_criteria=task.acceptance_criteria,
                            attempt=result.attempts,
                        )
                        if not review.approved:
                            logger.warning(f"[Reviewer] Rejected: {review.feedback}")
                            result.status = "failed"
                            result.error = f"Review rejected: {review.feedback}"

                    return result

        except Exception as e:
            task.status = TaskStatus.FAILED.value
            task.error = str(e)
            task.duration_sec = time.time() - start_time
            self.store.update_task(task)
            logger.error(f"[Dispatcher] Exception '{task.title}': {e}")
            raise

    async def _fix_build_on_main(
        self, build_output: str, task_title: str,
        build_cmd: str = "",
        merged_diff: str = "", changed_files: list[str] | None = None,
    ) -> bool:
        """
        Fix build errors on main after merge — keep trying until fixed.

        Escalation: Sonnet (att 1-2) → Opus (att 3+)
        Timeout:    300s base, ×1.5 each attempt, cap 1800s
        No rollback: each attempt builds on previous fixes.
        """
        if not build_cmd:
            build_cmd = "pnpm build"

        MAX_ATTEMPTS = 10
        SONNET_ATTEMPTS = 2
        BASE_TIMEOUT = 300
        total_fix_cost = 0.0
        prev_error_sig = ""  # v1.2: track for no-progress early exit

        # ── Context strings (stable across attempts) ──
        files_ctx = ""
        if changed_files:
            files_ctx = (
                "## Files Changed in This Merge\n"
                + "\n".join(f"- {f}" for f in changed_files[:30])
                + "\n\n"
            )
        diff_ctx = ""
        if merged_diff:
            diff_ctx = (
                f"## Merged Diff (partial)\n```\n"
                f"{merged_diff[:3000]}\n```\n\n"
            )

        # ── ReAct loop: fix → build → evaluate → escalate ──
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # Escalate model
            if attempt <= SONNET_ATTEMPTS:
                model = "claude-sonnet-4-6"
                model_label = "Sonnet"
            else:
                model = "claude-opus-4-6"
                model_label = "Opus"

            # Increase timeout: 300 → 450 → 675 → 600(Opus) → 900 → ...
            timeout = int(BASE_TIMEOUT * (1.5 ** (attempt - 1)))
            timeout = min(timeout, 1800)

            prompt = (
                f"The build broke after merging task \"{task_title}\".\n\n"
                f"## Build Error\n```\n{build_output[:4000]}\n```\n\n"
                f"{files_ctx}"
                f"{diff_ctx}"
            )
            if attempt > 1:
                prompt += (
                    f"## ⚠️ This is fix attempt #{attempt}. "
                    f"Previous {attempt-1} attempt(s) FAILED.\n"
                    f"The build error above is the CURRENT error after "
                    f"all previous fixes. Try a DIFFERENT approach.\n\n"
                )
            prompt += (
                "## Instructions\n"
                "1. Read the error messages carefully\n"
                "2. Fix the build errors (likely type errors, missing "
                "imports, or interface mismatches)\n"
                "3. Make minimal changes — only fix what's broken\n"
                "4. Do NOT add new features or refactor\n"
            )

            logger.info(
                f"[BuildFix] Attempt {attempt}/{MAX_ATTEMPTS} "
                f"[{model_label}, {timeout}s]"
            )

            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', delete=False
            ) as f:
                f.write(prompt)
                prompt_file = f.name

            # ── ACT (heartbeat-based timeout) ──
            # idle_timeout: Sonnet 120s, Opus 600s (thinks longer)
            # max_timeout: safety net (same as before)
            idle_t = 600 if "opus" in model else 120
            stdout_text = ""
            try:
                proc = await asyncio.create_subprocess_shell(
                    f'cat {prompt_file} | claude -p '
                    f'--dangerously-skip-permissions '
                    f'--model {model} --output-format text',
                    cwd=self.repo_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                stdout_text, stderr_text, timed_out, reason = await heartbeat_wait(
                    proc,
                    idle_timeout=idle_t,
                    max_timeout=timeout,
                    label=f"BuildFix/{model_label}",
                )
                stdout_text = stdout_text[:5000]
                if timed_out:
                    logger.warning(
                        f"[BuildFix] Attempt {attempt} heartbeat timeout "
                        f"({reason}, idle={idle_t}s, max={timeout}s)"
                    )
                    continue
            except Exception as e:
                logger.warning(
                    f"[BuildFix] Attempt {attempt} error: {e}"
                )
                continue
            finally:
                try:
                    os.unlink(prompt_file)
                except Exception:
                    pass

            # ── COST ──
            est = self.cost_tracker.estimate_from_chars(
                prompt + stdout_text, model, source="build_fix"
            )
            total_fix_cost += est.estimated_usd
            logger.info(
                f"[BuildFix] Attempt {attempt} cost: "
                f"${est.estimated_usd:.4f} [{model_label}]"
            )

            # Commit fixes
            try:
                from .workspace import GIT_ADD_PATHSPEC
                await self.pool._run_git(GIT_ADD_PATHSPEC, cwd=self.repo_dir)
                await self.pool._run_git(
                    f'commit -m "[agent-mesh] build fix #{attempt} '
                    f'({model_label}): {task_title}"',
                    cwd=self.repo_dir,
                )
            except Exception:
                pass

            # ── OBSERVE ──
            build_ok, build_output = await self.pool.run_build_check(
                build_cmd
            )
            if build_ok:
                self.wave_cost_usd += total_fix_cost
                logger.info(
                    f"[BuildFix] ✅ Fixed on attempt {attempt} "
                    f"[{model_label}] (fix cost: ${total_fix_cost:.4f})"
                )
                return True

            # ── EVALUATE: still failing, check for progress ──
            # v1.2: extract error signature for no-progress detection
            error_lines = sorted(set(
                line.strip() for line in build_output.split('\n')
                if any(kw in line.lower() for kw in ['error', 'cannot find', 'failed'])
            ))
            error_sig = "\n".join(error_lines[:20])

            if attempt >= 3 and error_sig == prev_error_sig:
                logger.warning(
                    f"[BuildFix] No progress: same errors after attempt "
                    f"{attempt}, stopping early"
                )
                break
            prev_error_sig = error_sig

            logger.warning(
                f"[BuildFix] Attempt {attempt} failed [{model_label}], "
                f"continuing..."
            )

        self.wave_cost_usd += total_fix_cost
        logger.error(
            f"[BuildFix] ❌ Failed after {MAX_ATTEMPTS} attempts "
            f"for '{task_title}' (fix cost: ${total_fix_cost:.4f})"
        )
        return False

    def _get_semaphore(self, agent_type: AgentType) -> asyncio.Semaphore:
        if agent_type == AgentType.CLAUDE_CODE:
            return self.semaphore_claude
        elif agent_type in (AgentType.DEEPSEEK_AIDER, AgentType.GROK_AIDER):
            return self.semaphore_deepseek
        return self.global_semaphore

    def _print_routing_preview(self, tasks: list[Task]):
        summary = self.router.get_routing_summary(tasks)
        total = len(tasks)
        logger.info("\n🤖 Routing Preview:")
        for agent_model, titles in summary.items():
            pct = len(titles) / total * 100
            logger.info(f"  {agent_model}: {len(titles)} tasks ({pct:.0f}%)")
            for title in titles[:5]:
                logger.info(f"    • {title}")
            if len(titles) > 5:
                logger.info(f"    ... and {len(titles) - 5} more")

    def _print_summary(self, plan: TaskPlan):
        stats = self.store.get_execution_stats()
        status = stats["status"]
        agents = stats["agents"]
        react = stats["react"]
        total = sum(status.values())

        logger.info(f"""
{'='*60}
📊 Execution Summary (v0.7)
{'='*60}
  Total:     {total}
  Completed: {status.get('completed', 0)} ✅
  Failed:    {status.get('failed', 0)} ❌
  Pending:   {status.get('pending', 0)} ⏳

  🤖 Agent Distribution:""")

        for agent, count in sorted(agents.items()):
            pct = count / total * 100 if total > 0 else 0
            logger.info(f"    {agent}: {count} ({pct:.0f}%)")

        completed = status.get("completed", 0)
        if completed > 0:
            first_pct = react["first_attempt_success"] / completed * 100
            retry_pct = react["required_retry"] / completed * 100
            cost_str = f"${self.wave_cost_usd:.4f}" if self.wave_cost_usd > 0 else "N/A"
            logger.info(f"""
  🔄 ReAct Loop:
    First attempt: {react['first_attempt_success']} ({first_pct:.0f}%)
    Retry needed:  {react['required_retry']} ({retry_pct:.0f}%)
    Avg attempts:  {react['avg_attempts']}

  💰 Cost: {cost_str}
{'='*60}""")
