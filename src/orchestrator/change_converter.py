"""
Agent Mesh v1.1 — Change-to-Task Converter

Converts DesignChange objects (from Opus delta analysis) directly into
TaskPlan-compatible dicts, bypassing the Gemini planner entirely.

DesignChange → Task field mapping:
  change_id           → id
  title               → title
  description + spec  → description
  estimated_complexity → complexity
  module              → module
  dependencies        → dependencies (filtered to same chunk)
  target_files        → target_files
  category/change_type → category
"""
from __future__ import annotations

import logging
from typing import Any

from .spec_analyzer import DesignChange
from .task_normalizer import TaskNormalizer

logger = logging.getLogger("agent-mesh")

# change_type → category fallback
_CHANGE_TYPE_TO_CATEGORY = {
    "ALTER_SCHEMA": "backend",
    "NEW_MODULE": "backend",
    "NEW_API": "backend",
    "MODIFY_BEHAVIOR": "backend",
    "NEW_FRONTEND": "frontend",
}


def convert_changes_to_plan(
    changes: list[DesignChange],
    project_name: str = "",
    shared_context: dict | None = None,
    chunk_title: str = "",
) -> dict:
    """
    Convert DesignChange list into a TaskPlan-compatible dict.
    Returns a dict that TaskPlan.from_dict() can consume directly.
    """
    tasks = []
    valid_ids = {c.change_id for c in changes}

    for i, change in enumerate(changes):
        category = change.category or _CHANGE_TYPE_TO_CATEGORY.get(
            change.change_type, "backend"
        )

        target_files = list(change.target_files) if change.target_files else []

        # Build description with spec context
        full_description = change.description
        if change.spec_section:
            full_description += f"\n\n## Spec Reference:\n{change.spec_section}"
        if change.feasibility_notes and not change.feasibility_notes.startswith("⚠️ BLOCKED"):
            full_description += f"\n\n## Notes:\n{change.feasibility_notes}"

        # Build acceptance criteria
        criteria_parts = []
        if change.affected_endpoints:
            criteria_parts.append(f"Endpoints working: {', '.join(change.affected_endpoints)}")
        if change.affected_tables:
            criteria_parts.append(f"Tables created/updated: {', '.join(change.affected_tables)}")
        criteria_parts.append("Build passes")
        acceptance_criteria = "; ".join(criteria_parts)

        # Filter dependencies to only those in this chunk
        deps = [d for d in change.dependencies if d in valid_ids]

        task = {
            "id": change.change_id,
            "title": change.title,
            "description": full_description,
            "agent_type": "",  # auto-route
            "complexity": change.estimated_complexity,
            "category": category,
            "module": change.module,
            "target_files": target_files,
            "dependencies": deps,
            "acceptance_criteria": acceptance_criteria,
            "priority": i + 1,
        }
        tasks.append(task)

    # Build modules dict
    modules = {}
    for task in tasks:
        mod = task["module"]
        if mod not in modules:
            modules[mod] = {
                "description": f"Module: {mod}",
                "interface_files": [],
                "imports": [],
                "exports": [],
            }

    plan = {
        "project_name": project_name,
        "shared_context": shared_context or {},
        "modules": modules,
        "tasks": tasks,
    }

    # v2.1: normalize tasks
    from ..models.task import Task
    normalizer = TaskNormalizer()
    task_objects = [Task.from_dict(t) for t in tasks]
    normalizer.normalize_plan(task_objects, chunk_id=chunk_title or "")
    plan["tasks"] = [t.to_dict() for t in task_objects]

    logger.info(
        f"[Converter] {len(changes)} changes → {len(tasks)} tasks "
        f"(project: {project_name})"
    )
    return plan
