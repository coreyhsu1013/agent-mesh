# Agent Mesh

Multi-agent CLI orchestration system — spec → plan → parallel agents → review → merge.

Takes a markdown spec, plans tasks via Gemini/Opus, dispatches them to multiple AI agents (Grok, DeepSeek, Claude) in parallel using git worktrees, reviews with Opus, and merges back to main.

## Architecture

```
Five-Layer Architecture:

Design Pipeline: spec delta → chunk → implement per chunk
  ↓ each chunk calls Layer 1-4
Layer 1 (ReAct):       task → escalate → complete
Layer 2 (ProjectLoop): plan → execute → verify → fix
Layer 3 (SpecFeedback): stuck gaps → analyze → spec fix
Layer 4 (Integration):  cross-module → contract check
```

### Execution Modes

**Standard** (`--plan + --cycles`):
```
Spec (.md) → Planner (Gemini) → plan.json
  → Dispatcher (wave-based, parallel worktrees)
    → ModelRouter (matrix-based escalation chain)
      ├─ Grok (aider CLI)      ← L/S, cheapest first
      ├─ DeepSeek (aider CLI)  ← M fallback
      ├─ Claude (claude CLI)   ← M/H + review
      └─ ReAct loop (think → act → observe → evaluate)
  → Reviewer (always Opus)
  → Git merge (sequential, build check after each merge)
```

**Spec Evolution** (`--evolve`):
```
Delta analysis (old spec vs new spec)
  → Feasibility review
  → Dependency-ordered chunking
  → Per-chunk: plan → execute (Layer 1-4) → validate
  → Final validation → re-chunk if gaps remain
```

## Model Routing

| Complexity | Escalation Chain | Use Case |
|-----------|-----------------|----------|
| L | Grok → DeepSeek → Sonnet | Scaffolding, config, boilerplate |
| S | Grok → DeepSeek → Sonnet | Simple logic, imports |
| M | Grok → DeepSeek → Sonnet → Opus | Business rules, middleware |
| H | Grok → DeepSeek → Sonnet → Opus | Architecture, security, payment |

Model ranking (0-7): Grok variants → DeepSeek → Sonnet → Opus.
Auto-escalation when gap reduction < 15%.

## Quick Start

```bash
# Prerequisites
claude auth status              # Claude CLI logged in
gemini --version                # Gemini CLI installed
aider --version                 # aider >= 0.86
export XAI_API_KEY="xai-..."
export DEEPSEEK_API_KEY="sk-..."

# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install pyyaml google-generativeai

# Plan only
python -m src.orchestrator.main --spec spec.md --repo ~/project --plan-only

# Execute plan with auto-fix cycles
python -m src.orchestrator.main --plan plan.json --repo ~/project --cycles 3

# Spec evolution (delta → chunk → implement)
python -m src.orchestrator.main --evolve --spec-old old.md --spec-new new.md --repo ~/project

# Resume after crash
python -m src.orchestrator.main --plan plan.json --repo ~/project --resume
```

## CLI Options

| Flag | Description |
|------|-------------|
| `--spec` | Path to spec.md for planning |
| `--plan` | Path to existing plan.json |
| `--repo` | Target git repository |
| `--config` | Path to config.yaml |
| `--plan-only` | Generate plan only, don't execute |
| `--resume` | Resume pending/failed tasks |
| `--evolve` | Spec evolution mode (delta → chunk → implement) |
| `--spec-old` / `--spec-new` | Old and new spec for evolution |
| `--cycles` | Number of verify-fix cycles |
| `--module` | Filter by module names |
| `--max-parallel` | Override max parallel tasks |
| `--no-review` | Skip code review |
| `--deploy` | Deploy after completion (rsync + SSH) |
| `-v` | Verbose logging |

## Key Features

- **Multi-agent routing**: 8 model ranks with automatic escalation
- **Multi-account Claude pool**: Round-robin accounts with 15% balance threshold
- **Closed-loop verify**: Regression check → bounded scan → convergence
- **Spec feedback (Layer 3)**: Stuck gaps → root cause analysis → auto-fix spec
- **Integration check (Layer 4)**: Cross-module typecheck + API contract validation
- **Chunked evolution**: Large spec changes split into dependency-ordered batches
- **Full resume**: Every step cached, crash-safe restart
- **Build-after-merge**: Build check after each merge, auto-rollback on failure
- **Docs-only detection**: Skip build check for non-code merges
- **SpecOS-aware planning**: Auto-detects `planning-spec.md` from SpecOS, uses full normative content with domain-specific complexity signals
- **Manual mode signals**: CONTINUE / SKIP / STOP for human-in-the-loop

## Project Structure

```
src/
├── orchestrator/   # Core pipeline (28 modules)
│   ├── main.py              # CLI entry
│   ├── design_loop.py       # Spec evolution orchestrator
│   ├── dispatcher.py        # Wave-based parallel execution
│   ├── project_loop.py      # Verify-fix outer loop
│   ├── react_loop.py        # Per-task ReAct loop
│   ├── router.py            # Model routing matrix
│   ├── model_ranking.py     # 8-rank escalation
│   ├── verifier.py          # Mechanical + LLM verification
│   ├── gap_analyzer.py      # Gap → fix-plan conversion
│   ├── spec_analyzer.py     # Delta analysis (Opus)
│   ├── spec_refiner.py      # Chunking (Sonnet)
│   ├── workspace.py         # Git worktree pool
│   └── ...
├── auth/           # CLI agent runners + multi-account pool
├── context/        # SQLite persistence
└── models/         # Dataclass models (Task, TaskPlan)
```

## Dependencies

- Python ≥ 3.11, asyncio throughout
- `pyyaml`, `google-generativeai` (optional)
- CLI tools: `claude`, `aider`, `gemini`, `git`
- Env vars: `XAI_API_KEY`, `DEEPSEEK_API_KEY`
