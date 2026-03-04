"""
Agent Mesh v0.9 — Experience Advisor

Queries experience.db to advise routing decisions based on
accumulated cross-project data.

Features:
- Skip models with historically poor success rates
- Suggest starting attempt based on historical failures
- Estimate task cost from historical averages
"""

from __future__ import annotations

import logging

from .experience_store import ExperienceStore

logger = logging.getLogger("agent-mesh")

# Minimum sample size before trusting statistics
MIN_CONFIDENCE_SAMPLES = 5

# Skip models with success rate below this threshold
SKIP_SUCCESS_THRESHOLD = 0.20


class ExperienceAdvisor:
    """Queries experience.db to advise routing decisions."""

    def __init__(self, store: ExperienceStore, project_type: str):
        self.store = store
        self.project_type = project_type

    def get_skip_models(self, complexity: str) -> list[str]:
        """
        Models to skip for this complexity + project_type.
        Skip if: success_rate < 20% AND sample_count >= 5 (confidence threshold).
        """
        skip = []
        stats = self.store.get_all_model_stats(self.project_type)
        for s in stats:
            if s["complexity"] != complexity:
                continue
            rate = s.get("success_rate", 0) or 0
            runs = s.get("total_runs", 0) or 0
            if runs >= MIN_CONFIDENCE_SAMPLES and rate < SKIP_SUCCESS_THRESHOLD:
                skip.append(s["model"])
                logger.info(
                    f"[Advisor] Skip {s['model']} for {complexity}/{self.project_type}: "
                    f"success={rate:.0%} ({runs} runs)"
                )
        return skip

    def suggest_start_attempt(self, complexity: str, chain: list[str]) -> int:
        """
        If historical data shows first N models in the chain always fail,
        suggest starting at attempt N+1 to save time/money.
        Returns 1-based attempt index (1 = no skip).
        """
        for idx, model in enumerate(chain):
            model_key = model.split("/")[-1] if "/" in model else model
            rate, count = self.store.get_model_success_rate(
                self.project_type, complexity, model_key
            )
            # If we have enough data and the model always fails, skip it
            if count >= MIN_CONFIDENCE_SAMPLES and rate < SKIP_SUCCESS_THRESHOLD:
                continue
            else:
                # This model is either untested or has reasonable success
                if idx > 0:
                    logger.info(
                        f"[Advisor] Suggest start_attempt={idx + 1} for {complexity} "
                        f"(skipping {idx} historically poor models)"
                    )
                return idx + 1  # 1-based

        # All models in chain are poor — start from last one anyway
        return len(chain)

    def estimate_task_cost(self, complexity: str, model: str) -> float:
        """Predict cost based on historical average for this complexity + model."""
        stats = self.store.get_all_model_stats(self.project_type)
        for s in stats:
            if s["complexity"] == complexity and s["model"] == model:
                return s.get("avg_cost_usd", 0) or 0
        return 0.0
