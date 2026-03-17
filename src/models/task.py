"""
Agent Mesh v0.6.0 — Task Models
新增 DEEPSEEK_AIDER AgentType + WorkspaceType。

v2.0: Task-Gate Architecture
- GateProfile / GateResult dataclasses
- Task 新增 gate metadata 欄位
"""

from __future__ import annotations
import time
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


def _ensure_list(val) -> list[str]:
    """Convert str→[str], pass through list, default to []."""
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        return [val]
    return []


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_REFINEMENT = "needs_refinement"  # v2.1: missing target_files


# ── Gate Architecture (v2.0) ──

@dataclass
class GateProfile:
    """Defines deterministic quality gate checks for a task type."""
    name: str = "coding_basic"
    input_checks: list[str] = field(default_factory=list)
    format_checks: list[str] = field(default_factory=list)
    rule_checks: list[str] = field(default_factory=list)
    verification_checks: list[str] = field(default_factory=list)
    escalation_checks: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> GateProfile:
        if not d:
            return cls()
        return cls(
            name=d.get("name", "coding_basic"),
            input_checks=d.get("input_checks", []),
            format_checks=d.get("format_checks", []),
            rule_checks=d.get("rule_checks", []),
            verification_checks=d.get("verification_checks", []),
            escalation_checks=d.get("escalation_checks", []),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "input_checks": self.input_checks,
            "format_checks": self.format_checks,
            "rule_checks": self.rule_checks,
            "verification_checks": self.verification_checks,
            "escalation_checks": self.escalation_checks,
        }


@dataclass
class GateResult:
    """Result from running a single gate check."""
    gate_name: str = ""
    passed: bool = True
    details: str = ""
    failed_checks: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, d: dict) -> GateResult:
        if not d:
            return cls()
        return cls(
            gate_name=d.get("gate_name", ""),
            passed=d.get("passed", True),
            details=d.get("details", ""),
            failed_checks=d.get("failed_checks", []),
            timestamp=d.get("timestamp", 0.0),
        )

    def to_dict(self) -> dict:
        return {
            "gate_name": self.gate_name,
            "passed": self.passed,
            "details": self.details,
            "failed_checks": self.failed_checks,
            "timestamp": self.timestamp,
        }


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
    merge_commit: str = ""             # merge commit SHA (for resume verification)

    # v2.0: Task-Gate Architecture
    task_type: str = ""                # e.g. "api", "schema", "ui", "auth"
    input_requirements: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    gate_profile: dict = field(default_factory=dict)   # serialized GateProfile
    gate_results: list[dict] = field(default_factory=list)  # list of serialized GateResult
    gate_feedback: dict = field(default_factory=dict)  # serialized GateFeedback for retry
    retry_reason: str = ""
    escalation_reason: str = ""
    verification_artifacts: dict = field(default_factory=dict)

    # v2.1: Task Schema v2 — scope control
    chunk_id: str = ""                                    # 所屬 chunk
    definition_of_done: list[str] = field(default_factory=list)   # DoD criteria list
    verifier_scope: list[str] = field(default_factory=list)       # verifier scope entries
    out_of_scope: list[str] = field(default_factory=list)         # 排除的檔案/模組
    required_target_files: list[str] = field(default_factory=list)  # 必須修改的檔案
    min_changed_files: int = 0                            # 最少要改幾個檔案
    allowed_no_diff: bool = False                         # analysis task = True
    source_gaps: list[str] = field(default_factory=list)  # 產生此 task 的 gap IDs
    depends_on: list[str] = field(default_factory=list)   # 顯式依賴

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
            merge_commit=d.get("merge_commit", ""),
            # v2.0: gate fields (safe fallback for old plan.json)
            task_type=d.get("task_type", ""),
            input_requirements=d.get("input_requirements", []),
            constraints=d.get("constraints", []),
            deliverables=d.get("deliverables", []),
            gate_profile=d.get("gate_profile", {}),
            gate_results=d.get("gate_results", []),
            gate_feedback=d.get("gate_feedback", {}),
            retry_reason=d.get("retry_reason", ""),
            escalation_reason=d.get("escalation_reason", ""),
            verification_artifacts=d.get("verification_artifacts", {}),
            # v2.1: scope control
            chunk_id=d.get("chunk_id", ""),
            definition_of_done=_ensure_list(d.get("definition_of_done", [])),
            verifier_scope=_ensure_list(d.get("verifier_scope", [])),
            out_of_scope=d.get("out_of_scope", []),
            required_target_files=d.get("required_target_files", d.get("must_change_files", [])),
            min_changed_files=d.get("min_changed_files", 0),
            allowed_no_diff=d.get("allowed_no_diff", False),
            source_gaps=d.get("source_gaps", []),
            depends_on=d.get("depends_on", []),
        )

    def to_dict(self) -> dict:
        d = {
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
            "merge_commit": self.merge_commit,
        }
        # v2.0: gate fields (only include if non-empty to keep backward compat)
        if self.task_type:
            d["task_type"] = self.task_type
        if self.input_requirements:
            d["input_requirements"] = self.input_requirements
        if self.constraints:
            d["constraints"] = self.constraints
        if self.deliverables:
            d["deliverables"] = self.deliverables
        if self.gate_profile:
            d["gate_profile"] = self.gate_profile
        if self.gate_results:
            d["gate_results"] = self.gate_results
        if self.gate_feedback:
            d["gate_feedback"] = self.gate_feedback
        if self.retry_reason:
            d["retry_reason"] = self.retry_reason
        if self.escalation_reason:
            d["escalation_reason"] = self.escalation_reason
        if self.verification_artifacts:
            d["verification_artifacts"] = self.verification_artifacts
        # v2.1: scope control (only include non-default)
        if self.chunk_id:
            d["chunk_id"] = self.chunk_id
        if self.definition_of_done:
            d["definition_of_done"] = self.definition_of_done
        if self.verifier_scope:
            d["verifier_scope"] = self.verifier_scope
        if self.out_of_scope:
            d["out_of_scope"] = self.out_of_scope
        if self.required_target_files:
            d["required_target_files"] = self.required_target_files
        if self.min_changed_files:
            d["min_changed_files"] = self.min_changed_files
        if self.allowed_no_diff:
            d["allowed_no_diff"] = self.allowed_no_diff
        if self.source_gaps:
            d["source_gaps"] = self.source_gaps
        if self.depends_on:
            d["depends_on"] = self.depends_on
        return d


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
