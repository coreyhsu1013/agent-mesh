"""
Agent Mesh v0.7.5 — Model Router (Matrix-based)

從 config.yaml routing.matrix 讀取 escalation chain。
每個 complexity (L/S/M/H) 有自己的 model chain。
attempt 1 → chain[0], attempt 2 → chain[1], ...
最後一級 retry: timeout_multiplier = 2.0

v0.7.5: outer_loop_min_tier — 外層循環根據收斂情況動態提升最低模型等級
"""

from __future__ import annotations
import logging
from dataclasses import dataclass

from ..models.task import Task, AgentType
from .model_ranking import get_model_rank

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """路由結果：agent + model + 原因 + timeout 倍率。"""
    agent_type: AgentType
    model: str
    reason: str
    timeout_multiplier: float = 1.0
    force_timeout_seconds: int = 0  # v1.3: absolute timeout override (0 = use default)

    @property
    def model_short(self) -> str:
        return self.model.split("/")[-1]


# ── Default chains (smoke-tested model strings) ──

DEFAULT_MATRIX: dict[str, list[str]] = {
    "L": [
        "xai/grok-4-fast-non-reasoning",
        "xai/grok-4-1-fast-non-reasoning",
        "xai/grok-code-fast-1",
        "claude-sonnet-4-6",
    ],
    "S": [
        "xai/grok-code-fast-1",
        "xai/grok-4-1-fast-non-reasoning",
        "deepseek/deepseek-reasoner",
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


# ── Complexity floor & force-sonnet rules ──
_COMPLEXITY_ORDER = {"L": 0, "S": 1, "M": 2, "H": 3}

DEFAULT_COMPLEXITY_FLOOR: dict[str, str] = {
    "schema": "H",
    "prisma": "H",
    "domain entit": "M",
    "migration": "M",
    "auth": "M",
    "hmac": "M",
    "security": "M",
    "payment": "H",
}

# Tasks matching these keywords skip Grok/DeepSeek, start from Sonnet
DEFAULT_FORCE_SONNET: list[str] = [
    "schema", "prisma", "domain entit", "migration",
    "auth", "hmac", "security", "payment",
]


class ModelRouter:

    def __init__(self, config: dict, advisor=None):
        self.config = config
        self.advisor = advisor  # ExperienceAdvisor | None — v0.9
        routing_cfg = config.get("routing", {})
        raw_matrix = routing_cfg.get("matrix", {})

        # Build matrix from config, fallback to defaults
        self.matrix: dict[str, list[str]] = {}
        for level in ("L", "S", "M", "H"):
            entry = raw_matrix.get(level, {})
            self.matrix[level] = entry.get("chain", DEFAULT_MATRIX[level])

        # Complexity floor from config, merged with defaults
        self.complexity_floor: dict[str, str] = {**DEFAULT_COMPLEXITY_FLOOR}
        self.complexity_floor.update(routing_cfg.get("complexity_floor", {}))

        # Force-sonnet keywords: skip Grok/DeepSeek for these tasks
        self.force_sonnet: list[str] = routing_cfg.get(
            "force_sonnet", DEFAULT_FORCE_SONNET
        )

        # Keep opus model ref for review
        agents_cfg = config.get("agents", {})
        claude_cfg = agents_cfg.get("claude_code", {})
        self.model_opus = claude_cfg.get("model_opus", "claude-opus-4-6")

        # ★ Outer-loop escalation: minimum model rank (set by ProjectLoop)
        self.outer_loop_min_rank: int = routing_cfg.get("outer_loop_min_rank", 0)
        self.outer_loop_timeout_mul: float = routing_cfg.get(
            "outer_loop_timeout_multiplier", 1.0
        )

        # v1.2: cycle-based fix escalation (set by ProjectLoop)
        # cycle 2 → start from Sonnet, cycle 3+ → start from Opus
        self.fix_cycle: int = 0

        # v1.3: force model override (from --force-model CLI)
        self.force_model: str = config.get("force_model", "")

        # v1.3: force timeout override (from --force-timeout CLI)
        self.force_timeout: int = config.get("force_timeout", 0)

    def apply_complexity_floor(self, task: Task) -> str:
        """Bump task complexity if title/module matches foundational keywords."""
        original = getattr(task, "complexity", "M")
        title_lower = (task.title or "").lower()
        module_lower = (task.module or "").lower()
        text = title_lower + " " + module_lower

        floor = original
        for keyword, min_level in self.complexity_floor.items():
            if keyword in text:
                if _COMPLEXITY_ORDER.get(min_level, 0) > _COMPLEXITY_ORDER.get(floor, 0):
                    floor = min_level

        if floor != original:
            logger.info(
                f"[Router] Complexity floor: '{task.title}' {original} → {floor} "
                f"(foundational task)"
            )
            task.complexity = floor

        return floor

    def get_start_attempt(self, task: Task) -> int:
        """
        Determine which attempt to start from.
        Three rules are evaluated and the HIGHEST start attempt wins:
          1. Foundational task keywords → force Sonnet
          2. Fix tasks (cycle 2+) → force Sonnet
          3. Outer-loop escalation → enforce min tier for ALL tasks
        Returns 1-based attempt index.

        v1.3: force_model → always start at 1 (bypass all routing).
        """
        if self.force_model:
            return 1

        complexity = getattr(task, "complexity", "M")
        chain = self.matrix.get(complexity, self.matrix["M"])
        start = 1

        title_lower = (task.title or "").lower()
        module_lower = (task.module or "").lower()
        task_id = (task.id or "").lower()
        text = title_lower + " " + module_lower

        should_force = False

        # Rule 1: foundational task keywords → force Sonnet
        for keyword in self.force_sonnet:
            if keyword in text:
                should_force = True
                break

        # Rule 2: fix tasks — escalate by cycle
        #   cycle 1 fix → skip Grok, start from DeepSeek+
        #   cycle 2 fix → start from Sonnet
        #   cycle 3+ fix → start from Opus
        if task_id.startswith("fix-"):
            should_force = True

        if should_force:
            if self.fix_cycle >= 3:
                # Cycle 3+: start from Opus
                target_prefix = "claude-opus"
            elif self.fix_cycle >= 2:
                # Cycle 2: start from Sonnet
                target_prefix = "claude-sonnet"
            else:
                # Cycle 1 / non-fix: skip Grok → DeepSeek+
                target_prefix = "deepseek/"

            for idx, model in enumerate(chain):
                if model.startswith(target_prefix) or \
                   (target_prefix == "deepseek/" and model.startswith("claude-")):
                    candidate = idx + 1  # 1-based
                    if candidate > start:
                        start = candidate
                    logger.info(
                        f"[Router] Force skip → '{task.title}' → attempt {candidate} "
                        f"({model}) [cycle {self.fix_cycle or 1}]"
                    )
                    break

        # Rule 3: experience advisor — skip historically poor models
        if self.advisor and not should_force:
            suggested = self.advisor.suggest_start_attempt(complexity, chain)
            if suggested > start:
                start = suggested
                logger.info(
                    f"[Router] Experience skip: '{task.title}' → "
                    f"attempt {start} (historical data)"
                )

        # Rule 4: outer-loop escalation — enforce min rank for ALL tasks
        if self.outer_loop_min_rank > 0:
            for idx, model in enumerate(chain):
                if get_model_rank(model) >= self.outer_loop_min_rank:
                    candidate = idx + 1  # 1-based
                    if candidate > start:
                        start = candidate
                        logger.info(
                            f"[Router] Outer-loop rank {self.outer_loop_min_rank}: "
                            f"'{task.title}' → attempt {candidate} ({model})"
                        )
                    break

        return start

    def get_model_for_attempt(
        self, complexity: str, attempt: int, *, log: bool = True,
    ) -> RoutingDecision:
        """
        查表返回第 attempt 次該用哪個 model。
        attempt 從 1 開始。最後一級 retry: timeout_multiplier = 2.0。
        v0.9: advisor can cause model skipping.
        """
        # v1.3: force model override — bypass all routing logic
        if self.force_model:
            model, agent_type = self._resolve_force_model(self.force_model)
            reason = f"forced:{self.force_model}"
            force_secs = self.force_timeout
            if force_secs:
                reason += f" (timeout={force_secs}s)"
            decision = RoutingDecision(
                agent_type=agent_type,
                model=model,
                reason=reason,
                force_timeout_seconds=force_secs,
            )
            if log:
                logger.info(
                    f"[Router] FORCED → {agent_type.value} ({model}) [{reason}]"
                )
            return decision

        chain = self.matrix.get(complexity, self.matrix["M"])
        idx = min(attempt - 1, len(chain) - 1)
        model = chain[idx]

        # v0.9: skip models flagged by experience advisor
        if self.advisor and log:
            skip_models = self.advisor.get_skip_models(complexity)
            if skip_models:
                original_model = model
                while model in skip_models and idx < len(chain) - 1:
                    idx += 1
                    model = chain[idx]
                if model != original_model:
                    logger.info(
                        f"[Router] Advisor skip: {original_model} → {model} "
                        f"(historical poor performance)"
                    )

        agent_type = _model_to_agent_type(model)

        # Last slot in chain + not first attempt → 2× timeout
        is_last_retry = (idx == len(chain) - 1) and (attempt > 1)
        timeout_multiplier = 2.0 if is_last_retry else 1.0

        # v1.2: cycle 4+ fix → extra 2× timeout (Opus needs more time)
        if self.fix_cycle >= 4:
            timeout_multiplier *= 2.0

        # ★ Outer-loop timeout extension (top tier retry)
        if self.outer_loop_timeout_mul > 1.0:
            timeout_multiplier *= self.outer_loop_timeout_mul

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

    def _resolve_force_model(self, shorthand: str) -> tuple[str, AgentType]:
        """Resolve --force-model shorthand to (model_id, AgentType)."""
        mapping = {
            "opus": ("claude-opus-4-6", AgentType.CLAUDE_CODE),
            "sonnet": ("claude-sonnet-4-6", AgentType.CLAUDE_CODE),
            "deepseek": ("deepseek/deepseek-reasoner", AgentType.DEEPSEEK_AIDER),
            "grok": ("xai/grok-4-1-fast-reasoning", AgentType.GROK_AIDER),
        }
        if shorthand in mapping:
            return mapping[shorthand]
        # Fallback: treat as full model ID
        agent_type = _model_to_agent_type(shorthand)
        return shorthand, agent_type

    def get_max_attempts(self, complexity: str) -> int:
        """Return chain length for this complexity."""
        if self.force_model:
            return 1  # forced model: one attempt only
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
