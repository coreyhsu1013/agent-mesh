# src/orchestrator — Orchestration Engine

28 modules that implement the five-layer architecture. All async (asyncio), fully typed.

## Data Flow

```
spec.md → GeminiPlanner → TaskPlan
  → Dispatcher (wave-based parallel worktrees)
    → ModelRouter → RoutingDecision (agent/model/timeout)
      → ReactLoop (think→act→observe→evaluate)
        → AiderRunner or ClaudeRunner
      → Reviewer (Opus, quality gate)
    → WorkspacePool.merge (sequential)
  → ProjectLoop (verify→gap-analyze→fix-plan→repeat)
    → Verifier → VerifyReport
    → GapAnalyzer → fix-plan.json
    → OuterLoopEscalation (model upgrade on stall)
  → DesignLoop (--evolve mode)
    → SpecAnalyzer → DesignChange[]
    → SpecRefiner → DesignChunk[]
    → per-chunk: plan→execute→verify (inner ProjectLoop)
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `main.py` | CLI entry, mode routing (--plan/--evolve/--deploy), PID lock |
| `dispatcher.py` | Wave-based parallel execution, slot pool, sequential merge |
| `router.py` | Matrix routing per complexity (L/S/M/H), escalation chains |
| `react_loop.py` | Per-task ReAct loop, max attempts, error context forwarding |
| `project_loop.py` | Outer loop: verify→fix→execute→repeat, Layer 3/4 integration |
| `verifier.py` | Mechanical (build/test/lint) + LLM spec-diff checks |
| `gap_analyzer.py` | Cluster gaps → phase-based fix-plan (0:mechanical → 5:integration) |
| `model_ranking.py` | Rank 0-7 models, OuterLoopEscalation on stalled convergence |
| `workspace.py` | Git worktree pool, slot recycling, merge lock |
| `reviewer.py` | Always Opus, auto-approve on attempt 3 |
| `design_loop.py` | --evolve orchestrator: delta→chunk→implement→validate→recurse |
| `spec_analyzer.py` | Delta analysis + feasibility review (Opus) |
| `spec_refiner.py` | Chunking + partial spec extraction (Sonnet) |
| `change_converter.py` | DesignChange → TaskPlan (bypass Gemini planning) |
| `planner.py` / `gemini_planner.py` | Two-phase planning (classify→detail) |
| `cost_tracker.py` | Parse CLI output for token counts, lookup pricing |
| `run_history.py` | Append JSON run entries to .agent-mesh/run-history.json |
| `retrospective.py` | Divergence analysis when gaps worsen |
| `experience_store.py` | Global ~/.agent-mesh/experience.db for cross-project learning |
| `experience_advisor.py` | Query experience DB for routing recommendations |
| `project_classifier.py` | Classify project type for experience matching |
| `deployer.py` | rsync + SSH deploy to target host |
| `codebase_guide.py` | Generate codebase overview for agent context |

## Key Patterns

- **Wave parallelism**: tasks grouped into dependency waves, each wave runs N slots in parallel, merges sequentially at wave end
- **Escalation chain**: attempt N uses `routing.matrix[complexity][N]`, last slot gets 2× timeout
- **Closed-loop verify**: cycle 1 = open-ended, cycle 2+ = regression + bounded scan (dedup by module|message)
- **Resume**: SQLite state (ContextStore) + file-based cache (design-changes.json, design-chunks-iter{N}.json)
- **Cost tracking**: every agent run returns CostResult, aggregated per wave and per run
