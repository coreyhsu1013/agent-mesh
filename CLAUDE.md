# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Agent Mesh is a multi-agent CLI orchestration system that takes a spec file, plans tasks via Gemini, dispatches them to multiple AI agents (Claude, DeepSeek, Grok) in parallel using git worktrees, reviews results with Claude Opus, and merges everything back to main. It optimizes cost by routing tasks to the cheapest capable model based on complexity.

@docs/architecture.md

## Commands

```bash
# Plan only (spec → plan.json)
python -m src.orchestrator.main --spec spec.md --repo ~/project --plan-only

# Execute a plan
python -m src.orchestrator.main --plan plan.json --repo ~/project

# Resume (skips completed tasks via SQLite state)
python -m src.orchestrator.main --plan plan.json --repo ~/project --resume

# Spec evolution (delta → chunk → implement → validate)
python -m src.orchestrator.main --evolve --spec-old old.md --spec-new new.md --repo ~/project

# Auto-cycle mode (plan → execute → verify → fix, N iterations)
python -m src.orchestrator.main --plan plan.json --repo ~/project --cycles 3

# Execute specific modules only
python -m src.orchestrator.main --plan plan.json --repo ~/project --module auth api

# Verbose output (always recommended)
python -m src.orchestrator.main --plan plan.json --repo ~/project -v
```

No unit tests for the orchestrator itself. Testing happens at the target repo level via the verify step.

## Project Structure

```
src/
├── orchestrator/   # 28 modules: planning, dispatch, verify, evolve (see CLAUDE.md inside)
├── auth/           # CLI agent runners: aider, claude, gemini (see CLAUDE.md inside)
├── context/        # SQLite persistence layer (see CLAUDE.md inside)
└── models/         # Dataclass models: Task, TaskPlan (see CLAUDE.md inside)
docs/
├── architecture.md # Five-layer architecture, routing, config details
├── decisions.md    # Version history, key design decisions, bug fixes
└── runbook/        # Operations guide, troubleshooting
config.yaml         # Routing matrix, concurrency limits, verify thresholds
```

## Dependencies

- Python >=3.11, asyncio throughout
- `pyyaml` (config), `google-generativeai` (optional, Gemini API)
- External CLIs: `claude`, `aider`, `gemini` (must be installed and authenticated)
- Env vars: `DEEPSEEK_API_KEY`, `XAI_API_KEY` (Claude/Gemini auth via their CLIs)

## Conventions

- All orchestrator code is async (asyncio). Entry point uses `asyncio.run()`.
- Full type hints, dataclass-based models (Python 3.11+).
- Mixed Chinese/English comments throughout — this is intentional.
- No unit tests; quality validated at target project level.
- See `.claude/rules/python-style.md` for coding conventions.
- See `.claude/rules/reporting.md` for output format conventions.

# Allowed Tools
All bash commands are pre-approved for this project.
