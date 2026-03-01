"""
Agent Mesh v0.6.5 — Dispatcher

v0.6.5 改進：
- RoutingDecision 的 model/use_chat 正確傳給 runner
- Claude Opus vs Sonnet 根據 task 複雜度分流
- DeepSeek reasoner vs chat 根據 task 類型分流
- Merge lock 防止並行 merge 衝突
- WorkspacePool 每 task 一個 slot
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
from .router import ModelRouter, RoutingDecision
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

        self.runners = {
            AgentType.CLAUDE_CODE: ClaudeRunner(config),
            AgentType.DEEPSEEK_AIDER: AiderRunner(config),
        }

        disp_cfg = config.get("dispatcher", {})
        self.max_parallel = disp_cfg.get("max_parallel", 4)
        self.semaphore_claude = asyncio.Semaphore(disp_cfg.get("semaphore_claude", 2))
        self.semaphore_deepseek = asyncio.Semaphore(disp_cfg.get("semaphore_deepseek", 3))
        self.global_semaphore = asyncio.Semaphore(self.max_parallel)
        self.merge_lock = asyncio.Lock()

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
        await self.pool.setup()

        if plan.shared_context:
            self.shared_context = json.dumps(plan.shared_context, indent=2)

        tasks = plan.tasks
        if modules:
            tasks = [t for t in tasks if t.module in modules]
        if resume:
            # ★ Resume: 從 DB 讀 completed 狀態，只跑 pending/failed
            db_tasks = self.store.get_all_tasks()
            completed_from_db = {t.id for t in db_tasks if t.status == TaskStatus.COMPLETED.value}
            tasks = [t for t in tasks if t.id not in completed_from_db]
            logger.info(f"[Dispatcher] Resume: {len(completed_from_db)} already done, {len(tasks)} remaining")

        if not tasks:
            logger.info("[Dispatcher] No tasks to execute")
            return

        self._print_routing_preview(tasks)

        # Build completed set (from DB for resume, or from plan)
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

            # ★ Cascade propagation: 連鎖跳過所有被 failed 擋住的 task
            while True:
                blocked = [
                    t for t in pending
                    if any(dep in failed_ids for dep in t.dependencies)
                ]
                if not blocked:
                    break
                for t in blocked:
                    t.status = TaskStatus.FAILED.value
                    t.error = f"Blocked: upstream dependency failed"
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

            results = await asyncio.gather(
                *[self._execute_single_task(t) for t in ready],
                return_exceptions=True,
            )

            for task, result in zip(ready, results):
                if isinstance(result, Exception):
                    logger.error(f"[Dispatcher] '{task.title}' exception: {result}")
                    task.status = TaskStatus.FAILED.value
                    task.error = str(result)
                    self.store.update_task(task)
                    failed_ids.add(task.id)
                elif result and result.status == "completed":
                    completed_ids.add(task.id)
                else:
                    # Task failed normally
                    failed_ids.add(task.id)
                pending.remove(task)

        self._print_summary(plan)

    async def _execute_single_task(self, task: Task) -> Optional[TaskResult]:
        task.status = TaskStatus.RUNNING.value
        self.store.update_task(task)
        start_time = time.time()

        # 1) Route → 拿到 agent + model + use_chat
        decision = self.router.route(task)
        task.agent_used = f"{decision.agent_type.value}:{decision.model.split('/')[-1]}"
        task.routed_by = "manual" if task.agent_type else "auto"

        runner = self.runners.get(decision.agent_type)
        if not runner:
            runner = self.runners[AgentType.CLAUDE_CODE]
            decision = RoutingDecision(AgentType.CLAUDE_CODE, "claude-sonnet-4-6", "fallback")

        semaphore = self._get_semaphore(decision.agent_type)

        # ★ 建構 runner kwargs（model/use_chat）
        agent_kwargs = {}
        if decision.agent_type == AgentType.DEEPSEEK_AIDER:
            agent_kwargs["use_chat"] = decision.use_chat
        elif decision.agent_type == AgentType.CLAUDE_CODE:
            agent_kwargs["model"] = decision.model

        # ★ 建構 escalation chain: 失敗就升級 model
        escalation_chain = self._build_escalation_chain(decision)

        slot_id = -1
        try:
            async with self.global_semaphore:
                async with semaphore:
                    slot_id, workspace_dir = await self.pool.acquire()

                    model_short = decision.model.split("/")[-1]
                    esc_info = ""
                    if escalation_chain:
                        esc_models = [e[2] for e in escalation_chain]
                        esc_info = f" escalation: {' → '.join(esc_models)}"
                    logger.info(
                        f"[Dispatcher] 🚀 '{task.title}' → "
                        f"{decision.agent_type.value} ({model_short}) "
                        f"[slot_{slot_id}, {task.complexity}]{esc_info}"
                    )

                    # 2) ReAct Loop（★ escalation_chain 傳入）
                    result = await self.react_loop.execute_task(
                        task=task,
                        agent_runner=runner,
                        workspace_dir=workspace_dir,
                        shared_context=self.shared_context,
                        agent_kwargs=agent_kwargs,
                        escalation_chain=escalation_chain,
                    )

                    task.attempts = result.attempts
                    task.duration_sec = time.time() - start_time
                    if result.history:
                        task.react_history = result.history.to_json()
                    # ★ Track actual model (may have escalated)
                    if result.final_model and result.final_model != "original":
                        task.agent_used = f"escalated:{result.final_model}"

                    if result.status == "completed":
                        # 3) Review
                        approved = True
                        if not self.no_review:
                            review = await self.reviewer.review(
                                diff=result.final_diff,
                                task_title=task.title,
                                task_description=task.description,
                                acceptance_criteria=task.acceptance_criteria,
                                attempt=result.attempts,
                            )
                            approved = review.approved
                            if not approved:
                                logger.warning(f"[Reviewer] Rejected: {review.feedback}")

                        if approved:
                            # 4) Merge（★ 加鎖，一次一個）
                            async with self.merge_lock:
                                commit_msg = f"[agent-mesh] {model_short}: {task.title}"
                                merged = await self.pool.merge_to_main(slot_id, commit_msg)

                            if merged:
                                task.status = TaskStatus.COMPLETED.value
                                task.diff = result.final_diff[:5000]
                                dur = time.time() - start_time
                                logger.info(
                                    f"[Dispatcher] ✅ '{task.title}' "
                                    f"({model_short}, {result.attempts} att, {dur:.0f}s)"
                                )
                            else:
                                task.status = TaskStatus.FAILED.value
                                task.error = "Merge failed"
                                logger.warning(f"[Dispatcher] ❌ '{task.title}' merge failed")
                        else:
                            task.status = TaskStatus.FAILED.value
                            task.error = "Review rejected"
                            logger.warning(f"[Dispatcher] ❌ '{task.title}' review rejected")
                    else:
                        task.status = TaskStatus.FAILED.value
                        task.error = result.error or "Unknown failure"
                        logger.warning(
                            f"[Dispatcher] ❌ '{task.title}' failed "
                            f"({result.attempts} attempts): {result.error}"
                        )

                    self.store.update_task(task)
                    return result

        except Exception as e:
            task.status = TaskStatus.FAILED.value
            task.error = str(e)
            task.duration_sec = time.time() - start_time
            self.store.update_task(task)
            logger.error(f"[Dispatcher] Exception '{task.title}': {e}")
            raise
        finally:
            if slot_id >= 0:
                self.pool.release(slot_id)

    def _get_semaphore(self, agent_type: AgentType) -> asyncio.Semaphore:
        if agent_type == AgentType.CLAUDE_CODE:
            return self.semaphore_claude
        elif agent_type == AgentType.DEEPSEEK_AIDER:
            return self.semaphore_deepseek
        return self.global_semaphore

    def _build_escalation_chain(self, decision: RoutingDecision) -> list:
        """
        2-level model escalation（3 attempts total, no duplicate opus）。
        回傳 [(runner, kwargs, label), ...] for attempt 2, 3

        Escalation paths:
          reasoner → sonnet → opus
          sonnet   → opus
          opus     → (retry same)
        """
        chain = []
        claude_runner = self.runners.get(AgentType.CLAUDE_CODE)

        model = decision.model

        if "deepseek" in model:
            # reasoner → sonnet → opus
            if claude_runner:
                chain.append((claude_runner, {"model": self.router.model_sonnet}, "claude-sonnet"))
                chain.append((claude_runner, {"model": self.router.model_opus}, "claude-opus"))

        elif "sonnet" in model:
            # sonnet → opus
            if claude_runner:
                chain.append((claude_runner, {"model": self.router.model_opus}, "claude-opus"))

        # opus → no escalation, retry same model

        return chain

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
📊 Execution Summary (v0.6.5)
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
