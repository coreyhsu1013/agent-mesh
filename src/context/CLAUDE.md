# src/context — Persistence Layer

## store.py — ContextStore

SQLite-backed state management. Schema defined in `db/schema.sql`.

### Key Methods
- `save_plan(plan: TaskPlan)` — persist initial plan
- `update_task(task_id, status, agent, attempts, diff, error)` — track execution
- `get_pending_tasks()` — for `--resume` (skip completed)
- `get_execution_stats()` — summary for reporting

### Database Location
`{repo}/.agent-mesh/agent-mesh.db`

### Schema
Tracks per-task: status, agent_used, model, attempts, diff, error, timing (started_at, completed_at).
Enables idempotent resume — a task marked `completed` is never re-executed.
