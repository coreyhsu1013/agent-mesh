"""
Agent Mesh v0.6.5 — Model Router
回傳 RoutingDecision（包含 agent type + 具體 model name + 原因）。

Model 分配（v0.6.5 — no chat tier）：
  Claude Opus      → H complexity, auth/security/payment 核心邏輯
  Claude Sonnet    → M complexity Claude tasks（快 3x, 省 5x）
  DeepSeek Reasoner → L/M complexity, all DeepSeek tasks
"""

from __future__ import annotations
import logging
import re
from dataclasses import dataclass

from ..models.task import Task, AgentType

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """路由結果：agent + model + 原因。"""
    agent_type: AgentType
    model: str
    reason: str
    use_chat: bool = False   # for DeepSeek: True=chat, False=reasoner


# ── Keywords ──

# Claude 核心任務（title-level matching）
CLAUDE_KEYWORDS = [
    "architect", "architecture",
    "security",
    "auth",
    "payment", "billing", "transaction",
    "database migration", "db migration",
    "encryption", "jwt middleware", "jwt auth",
    "websocket",
]

# DeepSeek 優先匹配（先檢查，優先級高於 Claude keywords）
DEEPSEEK_KEYWORDS = [
    "crud", "boilerplate", "scaffold",
    "readme", "documentation", "docs",
    "seed", "mock", "fixture",
    "css", "style", "tailwind",
    "i18n", "translation", "locale",
    "test", "spec", "unit test", "integration test",
    "validation", "validator", "schema",
    "config", "configuration", "setup", "initialize",
    "entry point", "index",
]


def _title_matches(title_lower: str, keywords: list[str]) -> bool:
    for kw in keywords:
        if " " in kw:
            if kw in title_lower:
                return True
        else:
            if re.search(rf'\b{re.escape(kw)}\b', title_lower):
                return True
    return False


class ModelRouter:

    def __init__(self, config: dict):
        self.config = config
        agents_cfg = config.get("agents", {})

        # Claude models
        claude_cfg = agents_cfg.get("claude_code", {})
        self.claude_enabled = claude_cfg.get("enabled", True)
        self.model_opus = claude_cfg.get("model_opus", "claude-opus-4-6")
        self.model_sonnet = claude_cfg.get("model_sonnet", "claude-sonnet-4-6")

        # DeepSeek models
        ds_cfg = agents_cfg.get("deepseek_aider", {})
        self.deepseek_enabled = ds_cfg.get("enabled", False)
        self.model_reasoner = ds_cfg.get("model_reasoner", "deepseek/deepseek-reasoner")
        self.model_chat = ds_cfg.get("model_chat", "deepseek/deepseek-chat")

    def route(self, task: Task) -> RoutingDecision:
        """完整路由決策，回傳 agent + model + 原因。"""

        complexity = getattr(task, "complexity", "M")
        title_lower = task.title.lower()

        # ── 規則 0：手動指定 ──
        if hasattr(task, "agent_type") and task.agent_type:
            try:
                explicit = AgentType(task.agent_type)
                model = self._default_model(explicit, complexity)
                decision = RoutingDecision(explicit, model, "manual override")
                self._log(task, decision)
                return decision
            except ValueError:
                pass

        # ── 規則 1：H complexity → Claude Opus ──
        if complexity == "H":
            d = RoutingDecision(AgentType.CLAUDE_CODE, self.model_opus, "complexity=H → Opus")
            self._log(task, d)
            return d

        # ── 規則 2：DeepSeek 未啟用 → 全走 Claude ──
        if not self.deepseek_enabled:
            model = self.model_opus if complexity == "H" else self.model_sonnet
            d = RoutingDecision(AgentType.CLAUDE_CODE, model, "deepseek disabled")
            self._log(task, d)
            return d

        # ── 規則 3：DeepSeek keywords 優先（test/config/validation 等）──
        if _title_matches(title_lower, DEEPSEEK_KEYWORDS):
            d = RoutingDecision(
                AgentType.DEEPSEEK_AIDER, self.model_reasoner,
                "DeepSeek keyword → reasoner", use_chat=False
            )
            self._log(task, d)
            return d

        # ── 規則 4：Claude keywords（auth/security/payment）──
        if _title_matches(title_lower, CLAUDE_KEYWORDS):
            if complexity == "H":
                d = RoutingDecision(AgentType.CLAUDE_CODE, self.model_opus, "Claude keyword + H → Opus")
            else:
                d = RoutingDecision(AgentType.CLAUDE_CODE, self.model_sonnet, "Claude keyword + M → Sonnet")
            self._log(task, d)
            return d

        # ── 規則 5：L complexity → DeepSeek reasoner ──
        if complexity == "L":
            d = RoutingDecision(
                AgentType.DEEPSEEK_AIDER, self.model_reasoner,
                "complexity=L → reasoner", use_chat=False
            )
            self._log(task, d)
            return d

        # ── 規則 6：M complexity default → DeepSeek reasoner ──
        d = RoutingDecision(
            AgentType.DEEPSEEK_AIDER, self.model_reasoner,
            "default M → reasoner", use_chat=False
        )
        self._log(task, d)
        return d

    def route_for_review(self) -> RoutingDecision:
        """Review 永遠用 Opus。"""
        return RoutingDecision(
            AgentType.CLAUDE_CODE, self.model_opus, "review → Opus"
        )

    def get_routing_summary(self, tasks: list[Task]) -> dict:
        """路由統計（不重複 log）。"""
        summary: dict[str, list[str]] = {}
        for task in tasks:
            d = self._route_quiet(task)
            key = f"{d.agent_type.value} ({d.model.split('/')[-1]})"
            summary.setdefault(key, []).append(task.title)
        return summary

    def _route_quiet(self, task: Task) -> RoutingDecision:
        """Same as route() but without logging."""
        old_log = self._log
        self._log = lambda *a: None
        d = self.route(task)
        self._log = old_log
        return d

    def _default_model(self, agent: AgentType, complexity: str) -> str:
        if agent == AgentType.CLAUDE_CODE:
            return self.model_opus if complexity == "H" else self.model_sonnet
        elif agent == AgentType.DEEPSEEK_AIDER:
            return self.model_reasoner  # v0.6.5: always reasoner, no chat tier
        return self.model_sonnet

    @staticmethod
    def _log(task: Task, d: RoutingDecision):
        model_short = d.model.split("/")[-1]
        logger.info(f"[Router] '{task.title}' → {d.agent_type.value} ({model_short}) [{d.reason}]")
