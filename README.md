# Agent Mesh v0.6.5

Multi-Agent CLI Orchestration — Planner → DAG → Dispatcher → CLI Agents → Reviewer → Git Merge

## Architecture

```
Spec (.md) → Planner (Gemini CLI) → plan.json
  → Dispatcher (ModelRouter)
    ├─ DeepSeek Agent (aider CLI) ← primary code writer (L/M complexity)
    │   └─ ReAct Loop (Think → Act → Observe → Retry)
    ├─ Claude Agent (claude -p)   ← architecture/security/review (H complexity)
    │   └─ ReAct Loop
    └─ Codex Agent (pending)
  → Reviewer (Claude)
  → Git Merge (main)
```

## Model Loading Distribution

| Agent | Loading | Role |
|-------|---------|------|
| Claude | ~30% | H complexity + review |
| DeepSeek | ~50% | L/M complexity code writing |
| Gemini | ~20% | Planning |

## Quick Start

```bash
# Prerequisites
claude auth status           # Claude CLI logged in
gemini --version            # Gemini CLI installed
aider --version             # aider >= 0.86
export DEEPSEEK_API_KEY="sk-..."

# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install pyyaml google-generativeai

# Plan (Gemini)
python -m src.orchestrator.main --spec ~/work/project/spec.md --repo ~/work/project --plan-only

# Execute (DeepSeek + Claude mixed)
python -m src.orchestrator.main --plan plan.json --repo ~/work/project

# Resume failed tasks
python -m src.orchestrator.main --plan plan.json --repo ~/work/project --resume
```

## CLI Options

| Flag | Description |
|------|-------------|
| `--spec` | Path to spec.md for planning |
| `--plan` | Path to existing plan.json |
| `--repo` | Target git repository |
| `--config` | Path to config.yaml |
| `--plan-only` | Generate plan only |
| `--resume` | Resume pending/failed tasks |
| `--module` | Filter by module names |
| `--waves` | Filter by wave numbers |
| `--max-parallel` | Override max parallel tasks |
| `--no-review` | Skip code review |
| `-v` | Verbose logging |

## Version History

| Version | Date | Changes |
|---------|------|---------|
| v0.1.0 | 2026-02-27 | Initial (API key mode) |
| v0.2.0 | 2026-02-27 | CLI OAuth2 migration |
| v0.3.0 | 2026-02-27 | Resume + version management |
| v0.4.0 | 2026-02-27 | Modular architecture (--module, --waves) |
| v0.5.0 | 2026-02-28 | Rate limit fixes (--max-parallel, retry delay) |
| **v0.6.0** | **2026-03-01** | **Gemini Planner + DeepSeek aider + ReAct Loop** |
| **v0.6.1** | **2026-03-01** | **Router precision fix, worktree merge rebase, test detection, --no-review** |
| **v0.6.2** | **2026-03-01** | **Fix main uncommitted changes blocking merge** |
| **v0.6.3** | **2026-03-01** | **WorkspacePool: per-task slot isolation + merge lock** |
| **v0.6.4** | **2026-03-01** | **Claude Opus/Sonnet split, DeepSeek reasoner/chat分流, RoutingDecision, timeout分級** |
| **v0.6.5** | **2026-03-01** | **index.lock cleanup, failed dependency propagation, model escalation (4級), heartbeat timeout** |
