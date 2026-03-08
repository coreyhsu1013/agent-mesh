# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Agent Mesh is a multi-agent CLI orchestration system that takes a spec file, plans tasks via Gemini, dispatches them to multiple AI agents (Claude, DeepSeek, Grok) in parallel using git worktrees, reviews results with Claude Opus, and merges everything back to main. It optimizes cost by routing tasks to the cheapest capable model based on complexity.

## Commands

```bash
# Plan only (spec → plan.json)
python -m src.orchestrator.main --spec spec.md --repo ~/project --plan-only

# Execute a plan
python -m src.orchestrator.main --plan plan.json --repo ~/project

# Resume (skips completed tasks via SQLite state)
python -m src.orchestrator.main --plan plan.json --repo ~/project --resume

# Execute specific modules only
python -m src.orchestrator.main --plan plan.json --repo ~/project --module auth api

# Execute without review
python -m src.orchestrator.main --plan plan.json --repo ~/project --no-review

# Auto-cycle mode (plan → execute → verify → fix, N iterations)
python -m src.orchestrator.main --plan plan.json --repo ~/project --cycles 3

# Verbose output
python -m src.orchestrator.main --plan plan.json --repo ~/project -v
```

There are no unit tests for the orchestrator itself. Testing happens at the target repo level via the verify step (`pnpm build`, `npx turbo test`).

## Architecture

```
Spec (.md) → Planner (Gemini two-phase) → plan.json
  → Dispatcher (wave-based, parallel worktrees)
    → ModelRouter picks agent per task based on complexity escalation chain
      ├─ Grok Agent (aider CLI)      ← L/S complexity, cheapest
      ├─ DeepSeek Agent (aider CLI)  ← M complexity fallback
      ├─ Claude Agent (claude CLI)   ← M/H complexity + review
      └─ Each task runs in ReAct loop (think → act → observe → evaluate)
  → Reviewer (always Claude Opus)
  → Git merge (sequential, one task at a time)
```

### Execution layers

- **L0 entry** (`main.py`): CLI arg parsing, mode selection (plan-only / execute / resume / verify / cycles)
- **Planner** (`planner.py`, `gemini_planner.py`): Two-phase — classify tasks with Flash, then detail with specialized models (Flash for backend, Opus for frontend)
- **Dispatcher** (`dispatcher.py`): Wave-based parallel execution. Creates a `WorkspacePool` of git worktree slots, assigns tasks to slots, runs them via ReAct, merges sequentially
- **Router** (`router.py`): Matrix-based routing. Each complexity tier (L/S/M/H) has an escalation chain of models. Attempt N uses chain[N]. Last slot gets 2× timeout
- **ReAct Loop** (`react_loop.py`): Think → Act → Observe → Evaluate cycle. Max 3 attempts. Failed attempts escalate to stronger model and carry error context forward
- **Workspace** (`workspace.py`): Git worktree pool. Each parallel slot is an isolated worktree. Slots are recycled within a wave. Merge lock ensures sequential merges
- **Reviewer** (`reviewer.py`): Always Opus. Auto-approves on attempt 3 or parse failure
- **Verifier** (`verifier.py`): Mechanical checks (conflict markers, build, test, lint) + LLM spec-diff analysis
- **Gap Analyzer** (`gap_analyzer.py`): Converts verify failures into a new fix-plan.json
- **Project Loop** (`project_loop.py`): Outer loop: verify → gap-analyze → plan → execute → repeat

### Agent runners (`src/auth/`)

- `aider_runner.py`: Runs aider CLI for DeepSeek/Grok agents with heartbeat-based timeout (idle detection, not fixed duration)
- `cli_runner.py`: Runs `claude -p` for Claude agent and `gemini` CLI for planning
- `check.py`: Auth checks for CLI tools

### Data models (`src/models/task.py`)

- `Task`: id, title, complexity (L/S/M/H), category (backend/frontend/fullstack), module, dependencies (DAG), target_files, acceptance_criteria, react tracking fields
- `TaskPlan`: project_name, shared_context, modules dict, list of Tasks
- `TaskStatus`: pending → running → completed/failed/skipped
- `AgentType`: claude_code, deepseek_aider, grok_aider

### Persistence (`src/context/store.py`)

SQLite database tracks task status, agent used, attempts, diffs, errors, and timing. Schema in `db/schema.sql`. Enables `--resume` to skip completed tasks.

## Key Config: `config.yaml`

Routing matrix defines escalation chains per complexity tier. Example for M complexity:
```
grok-4-fast-reasoning → grok-code-fast-1 → deepseek-reasoner → claude-sonnet → claude-opus (2× timeout)
```

Concurrency limits: `max_parallel=4`, `semaphore_claude=2`, `semaphore_deepseek=3`.

## Dependencies

- Python >=3.11, asyncio throughout
- `pyyaml` (config), `google-generativeai` (optional, Gemini API)
- External CLIs: `claude`, `aider`, `gemini` (must be installed and authenticated)
- Env vars: `DEEPSEEK_API_KEY`, `XAI_API_KEY` (Claude/Gemini auth via their CLIs)

## Conventions

- All orchestrator code is async (asyncio). Entry point uses `asyncio.run()`.
- Full type hints, dataclass-based models (Python 3.11+).
- Mixed Chinese/English comments throughout the codebase and config — this is intentional.
- The system has no unit tests for itself; quality is validated at the target project level.
- `MODEL_ENV_MAP` in router.py maps model prefixes to environment variable names for API keys.

# Allowed Tools
All bash commands are pre-approved for this project.
```

