"""
Agent Mesh v2.0 — Gate Registry
Maps profile names to GateProfile instances.
Provides heuristic-based profile resolution from task metadata.
"""

from __future__ import annotations
import logging
import re

from ..models.task import GateProfile, Task
from .profiles import ALL_PROFILES, CODING_BASIC

logger = logging.getLogger(__name__)


# ── Heuristic mapping: keyword patterns → profile name ──
#
# Two-pass matching (see resolve_profile):
#   Pass 1: title only (high confidence — title is the primary intent signal)
#   Pass 2: full text incl. description (lower confidence, catches incidental mentions)
#
# Priority rationale (most specific / least ambiguous first):
#   1. e2e/playwright/smoke — unambiguous test infra, must win over "auth"/"api" in description
#   2. docs/architecture   — catch doc tasks before "page"/"api" in descriptions cause mismatch
#   3. ui/selector/testid  — UI operability; removed "page"/"component" (too generic)
#   4. webhook/integration — before api, because webhook descriptions often mention "api" incidentally
#   5. schema/migration    — before auth, specific DB keywords
#   6. auth/security       — removed "session" (catches login/e2e tests); use "authentication" instead
#   7. api/crud            — broadest backend catch-all, checked last to avoid swallowing others

_PROFILE_HEURISTICS: list[tuple[list[str], str]] = [
    # 1. E2E / Playwright — most specific; always a test task, never an auth task
    (["playwright", "e2e", "smoke test", "spec.ts"], "e2e_smoke_gate"),
    # 2. Docs / architecture — catch doc tasks before generic keywords cause mismatch
    (["documentation", "architecture doc", "runbook"], "coding_basic"),
    # 3. UI operability — testid/selector/aria are unambiguous UI signals
    (["testid", "test-id", "data-testid", "selector", "accessibility", "aria-", "marker"], "ui_operability_basic"),
    # 4. Integration / webhook — before api (webhook descriptions often mention "api endpoint")
    (["webhook", "integration", "cross-module", "logistics"], "integration_basic"),
    # 5. Schema / migration — specific DB lifecycle keywords
    (["migration", "prisma", "migrate", "db schema"], "schema_critical"),
    # 6. Auth / security / payment — "authentication" not "auth" to avoid matching "author"
    (["authentication", "authorization", "security", "payment", "hmac", "jwt", "oauth"], "critical_backend"),
    # 7. API / CRUD — broadest catch-all, must be last among backend profiles
    (["api", "crud", "endpoint"], "api_basic"),
]


class GateRegistry:
    """Registry for gate profiles."""

    def __init__(self, extra_profiles: dict[str, GateProfile] | None = None):
        self.profiles: dict[str, GateProfile] = {**ALL_PROFILES}
        if extra_profiles:
            self.profiles.update(extra_profiles)

    def get_profile(self, name: str) -> GateProfile:
        """Get profile by name, fallback to coding_basic."""
        return self.profiles.get(name, CODING_BASIC)

    def resolve_profile(self, task: Task) -> GateProfile:
        """
        Resolve the gate profile for a task.
        Priority:
        1. task.gate_profile dict (explicit)
        2. Title-based heuristic (high confidence — title is the primary intent signal)
        3. Full-text heuristic (lower confidence — description can mention keywords incidentally)
        4. Default: coding_basic
        """
        # 1. Explicit gate_profile on task
        if task.gate_profile:
            if isinstance(task.gate_profile, dict) and task.gate_profile.get("name"):
                profile_name = task.gate_profile["name"]
                if profile_name in self.profiles:
                    return self.profiles[profile_name]
                # Unknown profile name but has check lists → use as-is
                return GateProfile.from_dict(task.gate_profile)

        title = (task.title or "").lower()
        full_text = " ".join([
            title,
            (task.description or "").lower(),
            (task.category or "").lower(),
            (task.task_type or "").lower(),
            (task.module or "").lower(),
        ])

        # 2. Title-based match (high confidence — prevents description noise from overriding)
        result = self._match_heuristics(title, task.title or "")
        if result:
            return result

        # 3. Full-text match (lower confidence fallback)
        result = self._match_heuristics(full_text, task.title or "")
        if result:
            return result

        # 4. Default
        return CODING_BASIC

    def _match_heuristics(self, text: str, title: str) -> GateProfile | None:
        """Match text against heuristic rules using word-boundary regex."""
        for keywords, profile_name in _PROFILE_HEURISTICS:
            for kw in keywords:
                # Multi-word keywords (e.g. "smoke test") use substring match;
                # single-word keywords use word boundary to avoid partial matches
                # (e.g. "api" shouldn't match "capability")
                if " " in kw or "-" in kw:
                    matched = kw in text
                else:
                    matched = bool(re.search(r'\b' + re.escape(kw) + r'\b', text))
                if matched:
                    logger.debug(
                        f"[GateRegistry] '{title}' → {profile_name} "
                        f"(matched '{kw}')"
                    )
                    return self.profiles[profile_name]
        return None

    def enrich_task(self, task: Task) -> None:
        """
        Enrich a task with gate_profile if not already set.
        Called by planner post-processing.
        """
        profile = self.resolve_profile(task)
        if not task.gate_profile:
            task.gate_profile = profile.to_dict()

        # Infer task_type if empty
        if not task.task_type:
            task.task_type = self._infer_task_type(task)

    @staticmethod
    def _infer_task_type(task: Task) -> str:
        """Infer task_type from title/category."""
        text = f"{task.title} {task.category} {task.module}".lower()
        if any(kw in text for kw in ["schema", "prisma", "migration"]):
            return "schema"
        if any(kw in text for kw in ["auth", "security", "payment"]):
            return "auth"
        if any(kw in text for kw in ["api", "crud", "endpoint", "route"]):
            return "api"
        if any(kw in text for kw in ["ui", "page", "component", "frontend"]):
            return "ui"
        if any(kw in text for kw in ["test", "e2e", "playwright"]):
            return "test"
        if any(kw in text for kw in ["integration", "webhook", "sync"]):
            return "integration"
        return "general"
