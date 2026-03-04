"""
Agent Mesh v0.7.6 — Model Ranking & Outer-Loop Escalation

按個別模型排名（不是按公司），從最便宜到最強：
  Rank 0: grok-4-fast-non-reasoning       （scaffolding）
  Rank 1: grok-4-1-fast-non-reasoning
  Rank 2: grok-code-fast-1                （code 專精）
  Rank 3: grok-4-fast-reasoning           （有推理）
  Rank 4: grok-4-1-fast-reasoning         （強推理）
  Rank 5: deepseek-reasoner              （長思考）
  Rank 6: claude-sonnet-4-6              （高品質）
  Rank 7: claude-opus-4-6               （最強）

Escalation rules:
  - Below top rank: 1 failed cycle → escalate rank_step ranks up
  - At top rank: extend timeout, up to N retries before giving up
  - "Failed cycle" = gap reduction < threshold (default 15%)
"""

from __future__ import annotations
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Default Model Ranking (low → high quality) ──
DEFAULT_RANKS: list[str] = [
    "xai/grok-4-fast-non-reasoning",       # rank 0
    "xai/grok-4-1-fast-non-reasoning",     # rank 1
    "xai/grok-code-fast-1",                # rank 2
    "xai/grok-4-fast-reasoning",           # rank 3
    "xai/grok-4-1-fast-reasoning",         # rank 4
    "deepseek/deepseek-reasoner",          # rank 5
    "claude-sonnet-4-6",                   # rank 6
    "claude-opus-4-6",                     # rank 7
]


def get_model_rank(model: str, ranks: list[str] | None = None) -> int:
    """Return the rank (0-based) for a given model string.
    Higher rank = stronger model. Unknown models → 0."""
    ranks = ranks or DEFAULT_RANKS
    try:
        return ranks.index(model)
    except ValueError:
        # Partial match fallback
        for idx, ranked_model in enumerate(ranks):
            if model in ranked_model or ranked_model in model:
                return idx
        return 0


def get_rank_label(rank: int, ranks: list[str] | None = None) -> str:
    """Return short model name for a rank index."""
    ranks = ranks or DEFAULT_RANKS
    if 0 <= rank < len(ranks):
        return ranks[rank].split("/")[-1]
    return "unknown"


@dataclass
class EscalationDecision:
    """Result of evaluating a cycle's convergence."""
    escalate: bool = False
    min_rank: int = 0
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
            config["routing"]["outer_loop_min_rank"] = decision.min_rank
        if decision.extend_timeout:
            config["routing"]["outer_loop_timeout_multiplier"] = decision.timeout_multiplier
        if decision.give_up:
            break
    """

    def __init__(self, config: dict):
        ranking_cfg = config.get("model_ranking", {})
        esc_cfg = ranking_cfg.get("escalation", {})

        self.ranks: list[str] = ranking_cfg.get("ranks", DEFAULT_RANKS)
        self.max_rank = len(self.ranks) - 1

        # Escalation thresholds
        self.gap_reduction_threshold = esc_cfg.get("gap_reduction_threshold", 0.15)
        self.rank_step = esc_cfg.get("rank_step", 2)  # jump N ranks per escalation
        self.max_retries_at_top = esc_cfg.get("max_retries_at_top", 3)
        self.timeout_extension = esc_cfg.get("timeout_extension", 1.5)

        # State
        self.current_min_rank: int = 0
        self.failures_at_top: int = 0
        self.gap_history: list[int] = []

    def record_cycle(self, gap_count: int) -> EscalationDecision:
        """
        Record this cycle's gap count and return escalation decision.
        """
        self.gap_history.append(gap_count)

        # First cycle — no comparison yet
        if len(self.gap_history) < 2:
            label = get_rank_label(self.current_min_rank, self.ranks)
            logger.info(
                f"[Ranking] Cycle 1 baseline: {gap_count} gaps "
                f"(min rank {self.current_min_rank}: {label})"
            )
            return EscalationDecision(min_rank=self.current_min_rank)

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
            self.failures_at_top = 0
            label = get_rank_label(self.current_min_rank, self.ranks)
            logger.info(
                f"[Ranking] ✅ Good convergence, staying at "
                f"rank {self.current_min_rank} ({label})"
            )
            return EscalationDecision(min_rank=self.current_min_rank)

        # Not enough progress — escalation logic
        if self.current_min_rank < self.max_rank:
            # Below top rank → escalate by rank_step
            new_rank = min(self.current_min_rank + self.rank_step, self.max_rank)
            self.current_min_rank = new_rank
            self.failures_at_top = 0
            label = get_rank_label(new_rank, self.ranks)
            reason = (
                f"Gap reduction {reduction_rate:.0%} < "
                f"{self.gap_reduction_threshold:.0%}, "
                f"escalating to rank {new_rank} ({label})"
            )
            logger.info(f"[Ranking] ⬆️ {reason}")
            return EscalationDecision(
                escalate=True,
                min_rank=new_rank,
                reason=reason,
            )
        else:
            # At top rank — extend timeout, count failures
            self.failures_at_top += 1
            label = get_rank_label(self.current_min_rank, self.ranks)

            if self.failures_at_top >= self.max_retries_at_top:
                reason = (
                    f"Top rank ({label}) failed "
                    f"{self.failures_at_top} consecutive cycles"
                )
                logger.warning(f"[Ranking] ⛔ {reason}")
                return EscalationDecision(
                    min_rank=self.current_min_rank,
                    give_up=True,
                    reason=reason,
                )

            multiplier = self.timeout_extension ** self.failures_at_top
            reason = (
                f"Top rank ({label}) retry "
                f"{self.failures_at_top}/{self.max_retries_at_top}, "
                f"timeout ×{multiplier:.1f}"
            )
            logger.info(f"[Ranking] 🔄 {reason}")
            return EscalationDecision(
                min_rank=self.current_min_rank,
                extend_timeout=True,
                timeout_multiplier=multiplier,
                reason=reason,
            )

    def get_status(self) -> dict:
        """Return current escalation state for logging."""
        label = get_rank_label(self.current_min_rank, self.ranks)
        return {
            "current_min_rank": self.current_min_rank,
            "rank_label": label,
            "failures_at_top": self.failures_at_top,
            "gap_history": self.gap_history,
        }
