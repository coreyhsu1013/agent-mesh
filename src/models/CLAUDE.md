# src/models — Data Models

All models are `@dataclass` with `from_dict()`/`to_dict()` for JSON serialization.

## task.py

### AgentType (Enum)
`claude_code`, `deepseek_aider`, `grok_aider`

### TaskStatus (Enum)
`pending` → `running` → `completed` | `failed` | `skipped`

### Task
Core unit of work. Key fields:
- `id`, `title`, `description`
- `complexity`: L / S / M / H
- `category`: backend / frontend / fullstack
- `module`: logical grouping
- `dependencies`: list of task IDs (DAG)
- `target_files`: files to create/modify
- `acceptance_criteria`: list of strings
- ReAct tracking: `react_history`, `attempts`, `current_attempt`
- Result: `diff`, `error`, `agent_used`, `model_used`

### TaskPlan
Container for a planning session:
- `project_name`, `shared_context`
- `modules`: dict of module metadata
- `tasks`: list of Task
