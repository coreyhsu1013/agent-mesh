"""
Agent Mesh v0.7.5 — Model Ranking & Outer-Loop Escalation

Monitors convergence across project-loop cycles and escalates
the minimum model tier when gap reduction rate is too slow.

Quality Tiers (low → high):
  Tier 0: Budget   (Grok)        — default starting point
  Tier 1: Standard (Sonnet)      — first escalation
  Tier 2: Premium  (Opus)        — final escalation

Escalation rules:
  - Below top tier: 1 failed cycle → escalate one tier up
  - At top tier (#1): extend timeout, up to N retries before giving up
  - "Failed cycle" = gap reduction < threshold (default 15%)
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Quality Tiers ──
# Each tier defines model prefixes. A model belongs to the HIGHEST
# tier whose prefix matches.

DEFAULT_TIERS = [
    {"name": "budget",   "prefixes": ["xai/"]},
    {"name": "standard", "prefixes": ["deepseek/", "claude-sonnet"]},
    {"name": "premium",  "prefixes": ["claude-opus"]},
]


def get_model_tier(model: str, tiers: list[dict] | None = None) -> int:
    """Return the tier index (0-based) for a given model string."""
    tiers = tiers or DEFAULT_TIERS
    for tier_idx in range(len(tiers) - 1, -1, -1):
        for prefix in tiers[tier_idx]["prefixes"]:
            if model.startswith(prefix):
                return tier_idx
    return 0  # unknown → budget


@dataclass
class EscalationDecision:
    """Result of evaluating a cycle's convergence."""
    escalate: bool = False
    min_tier: int = 0
    extend_timeout: bool = False
    timeout_multiplier: float = 1.0
    give_up: bool = False
    reason: str = ""


class OuterLoopEscalation:
    """
    Tracks gap count across cycles and decides when to escalate.

    Usage:
        esc = OuterLoopEscalation(config)

        # After each verify cycle:
        decision = esc.record_cycle(gap_count)
        if decision.escalate:
            # Apply min_tier to next cycle's routing
            config["routing"]["outer_loop_min_tier"] = decision.min_tier
        if decision.extend_timeout:
            config["routing"]["outer_loop_timeout_multiplier"] = decision.timeout_multiplier
        if decision.give_up:
            # Stop cycling
            break
    """

    def __init__(self, config: dict):
        ranking_cfg = config.get("model_ranking", {})
        esc_cfg = ranking_cfg.get("escalation", {})

        self.tiers = ranking_cfg.get("tiers", DEFAULT_TIERS)
        self.max_tier = len(self.tiers) - 1

        # Escalation thresholds
        self.gap_reduction_threshold = esc_cfg.get("gap_reduction_threshold", 0.15)
        self.max_retries_at_top = esc_cfg.get("max_retries_at_top_tier", 3)
        self.timeout_extension = esc_cfg.get("timeout_extension", 1.5)

        # State
        self.current_min_tier: int = 0
        self.failures_at_top_tier: int = 0
        self.gap_history: list[int] = []

    def record_cycle(self, gap_count: int) -> EscalationDecision:
        """
        Record this cycle's gap count and return escalation decision.

        Call this after each verify cycle with the total gap count.
        Returns an EscalationDecision indicating what to do next.
        """
        self.gap_history.append(gap_count)

        # First cycle — no comparison yet
        if len(self.gap_history) < 2:
            logger.info(
                f"[Ranking] Cycle 1 baseline: {gap_count} gaps "
                f"(tier {self.current_min_tier}: {self.tiers[self.current_min_tier]['name']})"
            )
            return EscalationDecision(min_tier=self.current_min_tier)

        prev = self.gap_history[-2]
        curr = self.gap_history[-1]
        reduction_rate = (prev - curr) / prev if prev > 0 else 0

        logger.info(
            f"[Ranking] Gap: {prev} → {curr} "
            f"(reduction: {reduction_rate:.0%}, "
            f"threshold: {self.gap_reduction_threshold:.0%})"
        )

        # Good progress? No escalation needed
        if reduction_rate >= self.gap_reduction_threshold:
            self.failures_at_top_tier = 0
            logger.info(
                f"[Ranking] ✅ Good convergence, staying at tier "
                f"{self.current_min_tier} ({self.tiers[self.current_min_tier]['name']})"
            )
            return EscalationDecision(min_tier=self.current_min_tier)

        # Not enough progress — escalation logic
        if self.current_min_tier < self.max_tier:
            # Below top tier → escalate one tier up immediately
            self.current_min_tier += 1
            self.failures_at_top_tier = 0
            tier_name = self.tiers[self.current_min_tier]["name"]
            reason = (
                f"Gap reduction {reduction_rate:.0%} < "
                f"{self.gap_reduction_threshold:.0%}, "
                f"escalating to tier {self.current_min_tier} ({tier_name})"
            )
            logger.info(f"[Ranking] ⬆️ {reason}")
            return EscalationDecision(
                escalate=True,
                min_tier=self.current_min_tier,
                reason=reason,
            )
        else:
            # At top tier — extend timeout, count failures
            self.failures_at_top_tier += 1
            tier_name = self.tiers[self.current_min_tier]["name"]

            if self.failures_at_top_tier >= self.max_retries_at_top:
                reason = (
                    f"Top tier ({tier_name}) failed "
                    f"{self.failures_at_top_tier} consecutive cycles"
                )
                logger.warning(f"[Ranking] ⛔ {reason}")
                return EscalationDecision(
                    min_tier=self.current_min_tier,
                    give_up=True,
                    reason=reason,
                )

            multiplier = self.timeout_extension ** self.failures_at_top_tier
            reason = (
                f"Top tier ({tier_name}) retry "
                f"{self.failures_at_top_tier}/{self.max_retries_at_top}, "
                f"timeout ×{multiplier:.1f}"
            )
            logger.info(f"[Ranking] 🔄 {reason}")
            return EscalationDecision(
                min_tier=self.current_min_tier,
                extend_timeout=True,
                timeout_multiplier=multiplier,
                reason=reason,
            )

    def get_status(self) -> dict:
        """Return current escalation state for logging."""
        return {
            "current_min_tier": self.current_min_tier,
            "tier_name": self.tiers[self.current_min_tier]["name"],
            "failures_at_top_tier": self.failures_at_top_tier,
            "gap_history": self.gap_history,
        }
