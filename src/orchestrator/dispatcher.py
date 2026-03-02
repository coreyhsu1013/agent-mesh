"""
Agent Mesh v0.7 — Dispatcher (Wave-based)

v0.7 改進：
- Wave-based merge: 執行期間 main 不動，Wave 結束統一 merge
- 每個 task 一個 slot（不回收），Wave 結束才 merge + cleanup
- merge 順序可控，衝突大幅減少
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Optional

from ..models.task import Task, TaskPlan, TaskStatus, AgentType
from ..context.store import ContextStore
from ..auth.aider_runner import AiderRunner, ClaudeRunner
from .router import ModelRouter
from .react_loop import ReactLoop, TaskResult
from .reviewer import Reviewer
from .workspace import WorkspacePool

logger = logging.getLogger(__name__)


class Dispatcher:

    def __init__(self, config: dict, repo_dir: str, store: ContextStore):
        self.config = config
        self.repo_dir = repo_dir
        self.store = store

        self.router = ModelRouter(config)
        self.react_loop = ReactLoop(config)
        self.reviewer = Reviewer(config, repo_dir)
        self.pool = WorkspacePool(repo_dir, config)

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

            logger.info(
                f"\n{'='*60}\n"
                f"  Wave {wave_num}: {len(ready)} tasks\n"
                f"{'='*60}"
            )

            # ═══════════════════════════════════════════
            # ★ Phase 1: Setup wave slots (one per task)
            # ═══════════════════════════════════════════
            slot_map = await self.pool.setup_wave(len(ready))
            # Assign slot_id to each task
            task_slot: dict[str, int] = {}  # task.id → slot_id
            for idx, task in enumerate(ready):
                task_slot[task.id] = idx

            # ═══════════════════════════════════════════
            # ★ Phase 2: Execute all tasks (semaphore limits parallelism)
            # ═══════════════════════════════════════════
            results = await asyncio.gather(
                *[
                    self._execute_task_in_slot(
                        task=t,
                        slot_id=task_slot[t.id],
                        workspace_dir=slot_map[task_slot[t.id]],
                    )
                    for t in ready
                ],
                return_exceptions=True,
            )

            # Collect results
            completed_slots = []     # slot_ids that succeeded
            task_labels = {}         # slot_id → task title (for commit msgs)
            wave_results = {}        # task.id → TaskResult

            for task, result in zip(ready, results):
                sid = task_slot[task.id]
                if isinstance(result, Exception):
                    logger.error(f"[Dispatcher] '{task.title}' exception: {result}")
                    task.status = TaskStatus.FAILED.value
                    task.error = str(result)
                    self.store.update_task(task)
                    failed_ids.add(task.id)
                elif result and result.status == "completed":
                    completed_slots.append(sid)
                    model_short = (result.final_model or "unknown").split("/")[-1]
                    task_labels[sid] = f"{model_short}: {task.title}"
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
            # ★ Phase 3: Merge all completed slots → main
            # ═══════════════════════════════════════════
            if completed_slots:
                merge_results = await self.pool.merge_wave(completed_slots, task_labels)

                # Update task status based on merge result
                for task in ready:
                    sid = task_slot[task.id]
                    result = wave_results.get(task.id)
                    if result and result.status == "completed":
                        if merge_results.get(sid, False):
                            task.status = TaskStatus.COMPLETED.value
                            task.diff = result.final_diff[:5000]
                            completed_ids.add(task.id)
                            dur = task.duration_sec
                            model = result.final_model or "unknown"
                            logger.info(
                                f"[Dispatcher] ✅ '{task.title}' "
                                f"({model.split('/')[-1]}, {result.attempts} att, {dur:.0f}s)"
                            )
                        else:
                            task.status = TaskStatus.FAILED.value
                            task.error = "Merge failed"
                            failed_ids.add(task.id)
                            logger.warning(f"[Dispatcher] ❌ '{task.title}' merge failed")
                        self.store.update_task(task)

            # ═══════════════════════════════════════════
            # ★ Phase 4: Cleanup wave slots
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

        # 1) Preview first model for logging + semaphore
        complexity = getattr(task, "complexity", "M")
        first_decision = self.router.get_model_for_attempt(complexity, 1, log=False)
        task.routed_by = "manual" if task.agent_type else "auto"
        task.agent_used = f"{first_decision.agent_type.value}:{first_decision.model_short}"

        semaphore = self._get_semaphore(first_decision.agent_type)
        max_att = self.router.get_max_attempts(complexity)

        # Build chain preview for log
        chain_preview = []
        for i in range(1, max_att + 1):
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
                    )

                    task.attempts = result.attempts
                    task.duration_sec = time.time() - start_time
                    if result.history:
                        task.react_history = result.history.to_json()
                    if result.final_model:
                        model_short = result.final_model.split("/")[-1]
                        task.agent_used = f"{'escalated:' if result.attempts > 1 else ''}{model_short}"

                    # 3) Review (optional, no merge here)
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
            logger.info(f"""
  🔄 ReAct Loop:
    First attempt: {react['first_attempt_success']} ({first_pct:.0f}%)
    Retry needed:  {react['required_retry']} ({retry_pct:.0f}%)
    Avg attempts:  {react['avg_attempts']}
{'='*60}""")
