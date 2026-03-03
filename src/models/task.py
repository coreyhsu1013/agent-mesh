"""
Agent Mesh v0.6.0 — Task Models
新增 DEEPSEEK_AIDER AgentType + WorkspaceType。
"""

from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AgentType(str, Enum):
    CLAUDE_CODE = "claude_code"
    GEMINI_CLI = "gemini_cli"
    CODEX = "codex"
    DEEPSEEK_AIDER = "deepseek_aider"   # ← v0.6.0 新增
    GROK_AIDER = "grok_aider"           # ← v0.7.1 新增


class WorkspaceType(str, Enum):
    MASTER = "master"
    CLAUDE_CODE = "claude_code"
    GEMINI_CLI = "gemini_cli"
    CODEX_WORKER = "codex_worker"
    DEEPSEEK_AIDER = "deepseek_aider"   # ← v0.6.0 新增


# Agent → Workspace 映射
AGENT_TO_WORKSPACE = {
    AgentType.CLAUDE_CODE: WorkspaceType.CLAUDE_CODE,
    AgentType.GEMINI_CLI: WorkspaceType.GEMINI_CLI,
    AgentType.CODEX: WorkspaceType.CODEX_WORKER,
    AgentType.DEEPSEEK_AIDER: WorkspaceType.DEEPSEEK_AIDER,  # ← v0.6.0 新增
    AgentType.GROK_AIDER: WorkspaceType.DEEPSEEK_AIDER,      # shares aider workspace
}


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description: str = ""
    agent_type: str = ""               # 空 = 自動路由
    complexity: str = "M"              # L | S | M | H
    category: str = ""                 # backend | frontend | fullstack (optional, from planner)
    module: str = "core"
    target_files: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    acceptance_criteria: str = ""
    priority: int = 1
    status: str = TaskStatus.PENDING.value

    # v0.6.0 新增
    agent_used: str = ""               # 實際用了哪個 agent (v0.6.4: agent:model)
    attempts: int = 0                  # ReAct loop 嘗試次數
    react_history: str = "[]"          # JSON: 每輪嘗試的紀錄
    routed_by: str = "auto"            # auto | manual

    # v0.6.4 新增
    diff: str = ""                     # 最終 git diff（截斷）
    duration_sec: float = 0.0          # 執行耗時
    error: str = ""                    # 錯誤訊息

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            title=d.get("title", ""),
            description=d.get("description", ""),
            agent_type=d.get("agent_type", ""),
            complexity=d.get("complexity", "M"),
            category=d.get("category", ""),
            module=d.get("module", "core"),
            target_files=d.get("target_files", []),
            dependencies=d.get("dependencies", []),
            acceptance_criteria=d.get("acceptance_criteria", ""),
            priority=d.get("priority", 1),
            status=d.get("status", TaskStatus.PENDING.value),
            agent_used=d.get("agent_used", ""),
            attempts=d.get("attempts", 0),
            react_history=d.get("react_history", "[]"),
            routed_by=d.get("routed_by", "auto"),
            diff=d.get("diff", ""),
            duration_sec=d.get("duration_sec", 0.0),
            error=d.get("error", ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "agent_type": self.agent_type,
            "complexity": self.complexity,
            "category": self.category,
            "module": self.module,
            "target_files": self.target_files,
            "dependencies": self.dependencies,
            "acceptance_criteria": self.acceptance_criteria,
            "priority": self.priority,
            "status": self.status,
            "agent_used": self.agent_used,
            "attempts": self.attempts,
            "react_history": self.react_history,
            "routed_by": self.routed_by,
            "diff": self.diff,
            "duration_sec": self.duration_sec,
            "error": self.error,
        }


@dataclass
class TaskPlan:
    project_name: str = ""
    shared_context: dict = field(default_factory=dict)
    modules: dict = field(default_factory=dict)
    tasks: list[Task] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> TaskPlan:
        return cls(
            project_name=d.get("project_name", ""),
            shared_context=d.get("shared_context", {}),
            modules=d.get("modules", {}),
            tasks=[Task.from_dict(t) for t in d.get("tasks", [])],
        )

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "shared_context": self.shared_context,
            "modules": self.modules,
            "tasks": [t.to_dict() for t in self.tasks],
        }
