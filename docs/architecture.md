# Agent Mesh Architecture (v2.2)

## Five-Layer Architecture
```
Design Pipeline (v1.0): spec delta → chunk → implement per chunk  ← 處理 spec 變更
  ↓ each chunk calls Layer 1-4
Layer 1 (ReAct):       task → escalate → complete           ← 確保跑得完
Layer 2 (ProjectLoop): plan → execute → verify → fix        ← 確保跑得好
Layer 3 (SpecFeedback): stuck gaps → analyze → spec fix     ← 確保寫得對
Layer 4 (Integration):  cross-module → contract check       ← 確保用得起來
```

## Execution Flow

### Mode 1: Standard (--plan + --cycles)
```
Spec (.md) → Planner (Gemini) → plan.json
  → Dispatcher (wave-based, parallel worktrees)
    → ModelRouter (matrix-based escalation chain per complexity)
      ├─ Grok (aider CLI)      ← L/S, cheapest
      ├─ DeepSeek (aider CLI)  ← M fallback
      ├─ Claude (claude CLI)   ← M/H + review
      └─ ReAct loop (think→act→observe→evaluate, max attempts per chain)
  → Reviewer (always Opus)
  → Git merge (sequential per wave, v1.2: build check after each merge + auto-fix)
```

### Mode 2: Spec Evolution (--evolve)
```
Step 1: SpecAnalyzer.analyze_delta(old_spec, new_spec, repo)
  → list[DesignChange] (每個 spec 差異結構化)
  → 快取: design-changes.json

Step 2: SpecAnalyzer.review_feasibility(changes, repo)
  → 標註依賴衝突、模糊、不可行的 change

Step 3: SpecRefiner.plan_chunks(changes, spec)
  → list[DesignChunk] (dependency-ordered batches)
  → 每個 chunk 有 self-contained partial_spec
  → 快取: design-chunks-iter{N}.json

Step 4: For each chunk (sequential):
  a. Write chunk partial spec → {chunk-id}-spec.md
  b. Planner.plan(partial_spec) → {chunk-id}-plan.json (快取)
  c. ProjectLoop.run_auto(plan, spec) ← Layer 1-4 inner loop
  d. Validate chunk results
  e. If design drift → adjust remaining chunks' specs

Step 5: Final validation against full new spec
  → If gaps remain → convert to DesignChanges → loop back to Step 3
  → max_design_iterations (default 3) controls outer recursion

Optional: --deploy → rsync + SSH deploy to target host
```

### Design Pipeline Concepts
- **Chunking 分塊**: 把大量 spec 變更依相依性分成可獨立實作的批次
  - Schema changes → always chunk-1 (foundation)
  - Backend CRUD per module → one chunk each
  - Frontend → after backend dependency
  - Independent features → separate chunk
- **Why chunking**: 避免 40+ task 同時跑導致相依性衝突、verify 無法收斂
- **Partial spec**: 每個 chunk 只看相關的 spec 段落，縮小 agent context
- **Two-layer recursion**:
  - Inner loop: Layer 1-4 per chunk (max_cycles)
  - Outer loop: final validation → re-chunk → re-implement (max_design_iterations)
- **Full resume**: 每一步都有快取檔，中斷後重跑自動跳過已完成的步驟

## Layer Details
- **Layer 1** (ReAct): per-task escalation chain. Grok→DeepSeek→Sonnet→Opus
- **Layer 2** (ProjectLoop): plan→execute→verify→fix-plan→repeat
  - Cycle 1: full open-ended verify (baseline)
  - Cycle 2+: closed-loop verify (regression + bounded scan)
  - Model ranking: auto-escalate rank if convergence < 15%
- **Layer 3** (SpecFeedback): gaps stuck N+ cycles → Opus analyzes root cause
  - CODE_BUG → keep fixing, SPEC_AMBIGUOUS/CONTRADICTION → spec_feedback task
  - SPEC_IMPOSSIBLE → spec_question saved for human
- **Layer 4** (Integration): after spec gaps converge → cross-module checks
  - Typecheck cmd (optional), LLM API contract check (Sonnet)
  - Issues become integration-fix tasks (complexity H)

## Key Files
| File | Role |
|------|------|
| `main.py` | CLI entry, mode routing (--plan/--evolve/--deploy) |
| `design_loop.py` | **v1.0** Design Pipeline orchestrator, recursion + resume |
| `spec_analyzer.py` | **v1.0** Delta analysis + feasibility review (Opus) |
| `spec_refiner.py` | **v1.0** Chunking + partial spec extraction (Sonnet) |
| `deployer.py` | **v1.0** rsync + SSH deploy to target host |
| `dispatcher.py` | Wave-based parallel execution, slot pool, v1.2: merge+build+fix |
| `change_converter.py` | **v1.1** DesignChange → TaskPlan converter (skip Gemini) |
| `router.py` | Matrix routing, complexity floor, force Sonnet, outer-loop min tier |
| `react_loop.py` | Inner ReAct loop, start_attempt support |
| `model_ranking.py` | Rank 0-7 individual models, OuterLoopEscalation, gap tracking |
| `project_loop.py` | Outer loop, verify_closed_loop, Layer 3/4 integration |
| `verifier.py` | run_mechanical, run_regression, run_bounded_scan, run_spec_feedback, run_integration_check |
| `gap_analyzer.py` | Convert verify issues → fix-plan.json (Phase 0-5) |
| `workspace.py` | Git worktree pool, slot recycling |
| `planner.py` / `gemini_planner.py` | Two-phase planning |
| `aider_runner.py` | Aider CLI runner (Grok/DeepSeek), heartbeat timeout |
| `cli_runner.py` | Claude CLI + Gemini CLI runner |

## Routing Matrix (config.yaml)
- L: grok-non-reasoning → grok-non-reasoning → grok-code → sonnet
- S: grok-code → grok-non-reasoning → deepseek → sonnet
- M: grok-reasoning → grok-code → deepseek → sonnet → opus
- H: grok-reasoning → sonnet → opus → opus(2x timeout)

## Quality Enforcement (v0.7.4+)
1. **Complexity floor**: foundational keywords auto-bump (schema/prisma→H, auth→M)
2. **Force Sonnet**: foundational tasks + all fix-* tasks skip Grok/DeepSeek
3. **Outer-loop escalation**: gap reduction < 15% → bump min tier for ALL tasks

## Model Ranking (v0.7.6+, individual model ranks)
- Rank 0: grok-4-fast-non-reasoning (scaffolding)
- Rank 1-4: Grok variants (increasingly capable)
- Rank 5: deepseek-reasoner (long thinking)
- Rank 6: claude-sonnet-4-6 (high quality)
- Rank 7: claude-opus-4-6 (strongest)
- Escalation: rank_step=2, gap reduction < 15% → bump rank
- At top (Opus): extend timeout × 1.5, max 3 retries

## Closed-Loop Verify (v0.7.4+)
- Step 0: Mechanical checks (build, test, lint)
- Step 1: Regression check (Sonnet, cheap yes/no on old gaps)
- Step 2: Bounded scan (Opus, max N new gaps, excludes known gaps)
- Step 2.5: Layer 3 spec feedback (stuck gaps → root cause analysis)
- Step 3: Convergence check (remaining + new <= 3 → done)
- Step 3.5: Layer 4 integration check (typecheck + API contract)
- Dedup: module|message key

## Config Priority
- CLI `--max-parallel` > `config.yaml dispatcher.max_parallel` > default 4

## Required Environment Variables
| Var | Used By | Notes |
|-----|---------|-------|
| `XAI_API_KEY` | Grok (aider) | xai- prefix |
| `DEEPSEEK_API_KEY` | DeepSeek (aider) | sk- prefix |
| `ANTHROPIC_API_KEY` | Claude (claude CLI) | claude CLI 自己管 auth |
| `GEMINI_API_KEY` | Gemini (gemini CLI) | gemini CLI 自己管 auth |

## Required CLI Tools (must be in PATH)
- `claude` — Claude Code CLI (Sonnet/Opus tasks + review)
- `aider` — Aider CLI (Grok/DeepSeek tasks), coreyllm 裝在 `~/.local/bin/aider`
- `gemini` — Gemini CLI (planning + verify)
- `git` — worktree isolation

## config.yaml Key Sections
```yaml
routing.matrix:          # 每個 complexity 的 escalation chain
dispatcher.max_parallel: # 並行 slot 數（依機器 RAM 調整）
dispatcher.semaphore_claude: 2   # Claude 並行上限
dispatcher.semaphore_deepseek: 3 # DeepSeek 並行上限
verify.convergence_threshold: 3  # gap <= N → 收斂完成
verify.max_new_gaps_per_cycle: 5 # bounded scan 上限
model_ranking.escalation.gap_reduction_threshold: 0.15  # < 15% → 升級
model_ranking.escalation.max_retries_at_top: 3          # Opus 最多重試 3 次
layer3.enabled: true                  # spec feedback for stuck gaps
layer3.stuck_threshold: 2             # gap 連續 N cycle 沒修好 → 觸發
layer4.enabled: false                 # 跨模組整合驗證（需專案配置）
layer4.typecheck_cmd: ""              # e.g. "npx tsc --noEmit"
layer4.run_after_cycle: 2             # cycle N 後才跑
```
