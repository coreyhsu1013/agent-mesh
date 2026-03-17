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
# NOTE: These heuristics only run AFTER the explicit precedence checks
# in resolve_profile() (scaffold → coding_basic, frontend → ui_operability_basic).
# So these rules only apply to non-scaffold, non-frontend tasks.
#
# Priority rationale (most specific / least ambiguous first):
#   1. e2e/playwright/smoke — unambiguous test infra
#   2. docs/architecture   — catch doc tasks before "api" in descriptions
#   3. ui/selector/testid  — UI operability signals
#   4. webhook/integration — before api (webhook descriptions mention "api" incidentally)
#   5. schema/migration    — specific DB lifecycle keywords
#   6. auth/security       — backend auth/security (bare "payment" excluded, see rule 7)
#   7. payment backend     — only backend-specific: gateway, callback, mpg, refund, invoice
#   8. api/crud            — broadest backend catch-all, checked last

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
    # 6. Auth / security / payment — backend-only critical checks
    #    "payment" removed here; handled context-sensitively in resolve_profile
    (["authentication", "authorization", "security", "hmac", "jwt", "oauth"], "critical_backend"),
    # 7. Payment — only backend gateway/callback/webhook/refund/invoice
    (["payment gateway", "payment callback", "payment notify", "mpg", "newebpay",
      "refund", "invoice", "allowance"], "critical_backend"),
    # 8. API / CRUD — broadest catch-all, must be last among backend profiles
    (["api", "crud", "endpoint"], "api_basic"),
]

# ── Early-exit signals: scaffold/foundation → coding_basic ──
_SCAFFOLD_KEYWORDS = [
    "scaffold", "bootstrap", "foundation", "project setup",
    "project scaffold", "init project", "boilerplate",
]

# ── Frontend signals (beyond category field) ──
# Only high-confidence prefixes that unambiguously indicate frontend work.
# Generic words like "page", "component", "client" are intentionally excluded.
_FRONTEND_SIGNALS = [
    "frontend:", "frontend ", "admin frontend",
    "next.js", "react page",
    "admin layout", "admin dashboard", "admin login",
    "admin a",  # "Admin A2 — Dashboard", "Admin A3 — Product management"
]


# ── task_type → profile mapping (v2.1) ──
# Takes precedence over heuristic-based resolution, but after explicit gate_profile.
_TASK_TYPE_TO_PROFILE: dict[str, str] = {
    "analysis":   "analysis_gate",
    "schema":     "schema_critical",
    "migration":  "schema_critical",
    "model":      "coding_basic",
    "service":    "critical_backend",
    "router":     "api_basic",
    "listener":   "integration_basic",
    "scheduler":  "integration_basic",
    "frontend":   "ui_operability_basic",
    "test_only":  "test_gate",
    "config":     "coding_basic",
    "general":    "coding_basic",       # keyword-unmatched fallback
}


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
        1. task.gate_profile dict (explicit from plan)
        2. Scaffold/foundation → coding_basic (adding deps is expected)
        3. Frontend category/signals → ui_operability_basic
        4. Title-based heuristic (high confidence)
        5. Full-text heuristic (lower confidence)
        6. Default: coding_basic
        """
        # 1. Explicit gate_profile on task
        if task.gate_profile:
            if isinstance(task.gate_profile, dict) and task.gate_profile.get("name"):
                profile_name = task.gate_profile["name"]
                if profile_name in self.profiles:
                    return self.profiles[profile_name]
                return GateProfile.from_dict(task.gate_profile)

        # 1.5 (v2.1): task_type → profile mapping (before heuristic)
        if task.task_type:
            mapped_profile = _TASK_TYPE_TO_PROFILE.get(task.task_type)
            if mapped_profile and mapped_profile in self.profiles:
                logger.debug(
                    f"[GateRegistry] '{task.title}' → {mapped_profile} "
                    f"(task_type={task.task_type})"
                )
                return self.profiles[mapped_profile]

        title = (task.title or "").lower()
        category = (task.category or "").lower()

        # 2. Scaffold/foundation tasks → coding_basic (no_new_dependency must not block)
        if any(kw in title for kw in _SCAFFOLD_KEYWORDS):
            logger.debug(f"[GateRegistry] '{task.title}' → coding_basic (scaffold)")
            return CODING_BASIC

        # 3. Frontend tasks → ui_operability_basic
        #    Check category field AND title signals (planner may not set category)
        if category == "frontend" or any(sig in title for sig in _FRONTEND_SIGNALS):
            logger.debug(
                f"[GateRegistry] '{task.title}' → ui_operability_basic (frontend)"
            )
            return self.profiles.get("ui_operability_basic", CODING_BASIC)

        full_text = " ".join([
            title,
            (task.description or "").lower(),
            category,
            (task.task_type or "").lower(),
            (task.module or "").lower(),
        ])

        # 4. Title-based match (high confidence)
        result = self._match_heuristics(title, task.title or "")
        if result:
            return result

        # 5. Full-text match (lower confidence fallback)
        result = self._match_heuristics(full_text, task.title or "")
        if result:
            return result

        # 6. Default
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
        title = (task.title or "").lower()
        category = (task.category or "").lower()
        text = f"{title} {category} {task.module or ''}".lower()

        # Frontend tasks: check category first, then title signals
        if category == "frontend" or any(sig in title for sig in _FRONTEND_SIGNALS):
            return "ui"
        if any(kw in text for kw in ["schema", "prisma", "migration"]):
            return "schema"
        if any(kw in text for kw in ["scaffold", "bootstrap", "foundation"]):
            return "setup"
        if any(kw in text for kw in ["auth", "security"]):
            return "auth"
        # "payment" alone doesn't mean auth — only backend payment infra
        if any(kw in text for kw in ["payment gateway", "payment callback", "mpg",
                                      "newebpay", "refund", "invoice"]):
            return "auth"
        if any(kw in text for kw in ["api", "crud", "endpoint", "route"]):
            return "api"
        if any(kw in text for kw in ["test", "e2e", "playwright"]):
            return "test"
        if any(kw in text for kw in ["integration", "webhook", "sync"]):
            return "integration"
        return "general"
