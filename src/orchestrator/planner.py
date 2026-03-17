"""
Agent Mesh v0.6.0 — Planner
Reads spec.md + AGENTS.md → produces plan.json (task DAG).

Provider priority:
1. Gemini CLI (pipe mode, verified working)
2. Gemini API (fallback, needs GOOGLE_API_KEY)
3. Claude CLI (final fallback)
"""

from __future__ import annotations
import json
import logging
import os
from typing import Optional

from ..models.task import TaskPlan
from ..auth.cli_runner import run_claude_prompt, run_gemini_prompt
from ..gates.registry import GateRegistry
from .gemini_planner import GeminiPlanner
from .task_normalizer import TaskNormalizer

logger = logging.getLogger(__name__)


class Planner:
    """
    Unified planner that routes to Gemini or Claude based on config.
    """

    def __init__(self, config: dict, repo_dir: str):
        self.config = config
        self.repo_dir = repo_dir
        planner_cfg = config.get("planner", {})
        self.provider = planner_cfg.get("provider", "gemini")
        self.timeout = planner_cfg.get("timeout", 300)

        # Gemini planner (for API fallback)
        self.gemini_planner = GeminiPlanner(config)

        # v2.0: gate registry for task enrichment
        self.gate_registry = GateRegistry()
        # v2.1: task normalizer
        self.normalizer = TaskNormalizer()

    async def plan(
        self,
        spec_path: str,
        agents_md_path: str | None = None,
        project_name: str = "",
    ) -> TaskPlan:
        """
        Read spec file → generate plan → return TaskPlan.
        """
        # Read spec
        with open(spec_path, "r") as f:
            spec_content = f.read()

        # Read AGENTS.md if available
        agents_md = ""
        if agents_md_path and os.path.exists(agents_md_path):
            with open(agents_md_path, "r") as f:
                agents_md = f.read()
        else:
            # Try default location
            default_agents = os.path.join(self.repo_dir, "AGENTS.md")
            if os.path.exists(default_agents):
                with open(default_agents, "r") as f:
                    agents_md = f.read()

        if not project_name:
            project_name = os.path.basename(self.repo_dir)

        # Auto-detect SpecOS canonical planning spec
        spec_basename = os.path.basename(spec_path).lower()
        is_canonical = "planning-spec" in spec_basename
        if is_canonical:
            logger.info("[Planner] Detected SpecOS canonical planning spec")

        logger.info(f"[Planner] Planning with provider={self.provider}")

        # Use GeminiPlanner (handles CLI → API → Claude fallback chain)
        plan_dict = await self.gemini_planner.plan(
            spec_content=spec_content,
            agents_md=agents_md,
            project_name=project_name,
            is_canonical=is_canonical,
        )

        plan = TaskPlan.from_dict(plan_dict)

        # v2.0: enrich tasks with gate profiles
        self._enrich_tasks(plan)

        logger.info(
            f"[Planner] Generated plan: {len(plan.tasks)} tasks, "
            f"modules={list(plan.modules.keys()) if plan.modules else ['core']}"
        )
        return plan

    def _enrich_tasks(self, plan: TaskPlan) -> None:
        """Enrich all tasks with gate_profile and task_type via heuristic."""
        profile_counts: dict[str, int] = {}
        for task in plan.tasks:
            self.gate_registry.enrich_task(task)
            pname = task.gate_profile.get("name", "coding_basic")
            profile_counts[pname] = profile_counts.get(pname, 0) + 1

        logger.info(
            f"[Planner] Gate profiles assigned: "
            + ", ".join(f"{k}={v}" for k, v in sorted(profile_counts.items()))
        )

        # v2.1: normalize tasks (task_type, allowed_no_diff, required_target_files)
        # v2.2: pass repo_dir for path existence filtering
        self.normalizer.normalize_plan(plan.tasks, repo_dir=self.repo_dir)

    @staticmethod
    def save_plan(plan: TaskPlan, output_path: str):
        """Save plan to JSON file."""
        with open(output_path, "w") as f:
            json.dump(plan.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info(f"[Planner] Plan saved to {output_path}")

    @staticmethod
    def load_plan(plan_path: str) -> TaskPlan:
        """Load plan from JSON file."""
        with open(plan_path, "r") as f:
            data = json.load(f)
        plan = TaskPlan.from_dict(data)
        logger.info(f"[Planner] Loaded plan: {len(plan.tasks)} tasks")
        return plan
