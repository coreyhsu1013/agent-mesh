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
from .gemini_planner import GeminiPlanner

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

        logger.info(f"[Planner] Planning with provider={self.provider}")

        # Use GeminiPlanner (handles CLI → API → Claude fallback chain)
        plan_dict = await self.gemini_planner.plan(
            spec_content=spec_content,
            agents_md=agents_md,
            project_name=project_name,
        )

        plan = TaskPlan.from_dict(plan_dict)
        logger.info(
            f"[Planner] Generated plan: {len(plan.tasks)} tasks, "
            f"modules={list(plan.modules.keys()) if plan.modules else ['core']}"
        )
        return plan

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
