"""
Agent Mesh v0.7.1 — Model Router (Matrix-based)

從 config.yaml routing.matrix 讀取 escalation chain。
每個 complexity (L/M/H) 有自己的 model chain。
attempt 1 → chain[0], attempt 2 → chain[1], ...
最後一級 retry: timeout_multiplier = 2.0
"""

from __future__ import annotations
import logging
from dataclasses import dataclass

from ..models.task import Task, AgentType

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """路由結果：agent + model + 原因 + timeout 倍率。"""
    agent_type: AgentType
    model: str
    reason: str
    timeout_multiplier: float = 1.0

    @property
    def model_short(self) -> str:
        return self.model.split("/")[-1]


# ── Default chains (smoke-tested model strings) ──

DEFAULT_MATRIX: dict[str, list[str]] = {
    "L": [
        "xai/grok-4-fast-non-reasoning",
        "xai/grok-4-1-fast-non-reasoning",
        "deepseek/deepseek-reasoner",
        "xai/grok-code-fast-1",
        "claude-sonnet-4-6",
    ],
    "M": [
        "xai/grok-4-fast-reasoning",
        "xai/grok-code-fast-1",
        "deepseek/deepseek-reasoner",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    ],
    "H": [
        "xai/grok-4-1-fast-reasoning",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-opus-4-6",
    ],
}


def _model_to_agent_type(model: str) -> AgentType:
    """Model string prefix → AgentType."""
    if model.startswith("xai/"):
        return AgentType.GROK_AIDER
    if model.startswith("deepseek/"):
        return AgentType.DEEPSEEK_AIDER
    return AgentType.CLAUDE_CODE


class ModelRouter:

    def __init__(self, config: dict):
        self.config = config
        routing_cfg = config.get("routing", {})
        raw_matrix = routing_cfg.get("matrix", {})

        # Build matrix from config, fallback to defaults
        self.matrix: dict[str, list[str]] = {}
        for level in ("L", "M", "H"):
            entry = raw_matrix.get(level, {})
            self.matrix[level] = entry.get("chain", DEFAULT_MATRIX[level])

        # Keep opus model ref for review
        agents_cfg = config.get("agents", {})
        claude_cfg = agents_cfg.get("claude_code", {})
        self.model_opus = claude_cfg.get("model_opus", "claude-opus-4-6")

    def get_model_for_attempt(
        self, complexity: str, attempt: int, *, log: bool = True,
    ) -> RoutingDecision:
        """
        查表返回第 attempt 次該用哪個 model。
        attempt 從 1 開始。最後一級 retry: timeout_multiplier = 2.0。
        """
        chain = self.matrix.get(complexity, self.matrix["M"])
        idx = min(attempt - 1, len(chain) - 1)
        model = chain[idx]
        agent_type = _model_to_agent_type(model)

        # Last slot in chain + not first attempt → 2× timeout
        is_last_retry = (idx == len(chain) - 1) and (attempt > 1)
        timeout_multiplier = 2.0 if is_last_retry else 1.0

        reason = f"chain[{complexity}][{idx}]"
        if timeout_multiplier > 1:
            reason += f" (timeout ×{timeout_multiplier:.0f})"

        decision = RoutingDecision(
            agent_type=agent_type,
            model=model,
            reason=reason,
            timeout_multiplier=timeout_multiplier,
        )

        if log:
            logger.info(
                f"[Router] complexity={complexity} attempt={attempt}/{len(chain)} → "
                f"{agent_type.value} ({decision.model_short}) [{reason}]"
            )

        return decision

    def get_max_attempts(self, complexity: str) -> int:
        """Return chain length for this complexity."""
        chain = self.matrix.get(complexity, self.matrix["M"])
        return len(chain)

    def route_for_review(self) -> RoutingDecision:
        """Review 永遠用 Opus。"""
        return RoutingDecision(
            AgentType.CLAUDE_CODE, self.model_opus, "review → Opus"
        )

    def get_routing_summary(self, tasks: list[Task]) -> dict:
        """路由統計（第一個 attempt 的 model，不 log）。"""
        summary: dict[str, list[str]] = {}
        for task in tasks:
            complexity = getattr(task, "complexity", "M")
            d = self.get_model_for_attempt(complexity, 1, log=False)
            key = f"{d.agent_type.value} ({d.model_short})"
            summary.setdefault(key, []).append(task.title)
        return summary
