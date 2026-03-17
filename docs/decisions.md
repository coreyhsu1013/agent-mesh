# Key Decisions & Bug Fix History

## Version History

### v2.2 — Target Files Inference + Path Resolution (2026-03-18)
- **Problem**: evolve-run-19 19/118 tasks fail — weak target_files inference, strict gate paths, stale verifier paths
- **Solution 1**: 6-layer target_files inference (required → source_gaps → description → module → regex), repo-aware filtering
- **Solution 2**: Conservative allowed_paths_only relaxation — sibling shared/common + task-provided related_dirs
- **Solution 3**: Canonical path resolution in verifier — VERIFY_FALSE_POSITIVE (phantom) + LEGACY_ARTIFACT_MISMATCH (stale→canonical)
- **Key decisions**: related_dirs must be repo-aware filtered; legacy_artifact_mismatch never generates fix tasks against stale paths; canonical path uses chunk/module context from VerifyContext
- Commit: 45c1e87

### v2.1 — Task Schema v2: Scope Control + Gate Routing (2026-03-16)
- **Problem**: Tasks lack scope metadata, verifier can't scope to chunks, no deterministic quality gates
- **Solution**: Task Schema v2 fields (chunk_id, definition_of_done, verifier_scope, required_target_files), TaskNormalizer, Gate Architecture (GateProfile/GateRunner/check registry)
- Commits: cdd7929

### v1.0 — Design Pipeline: Spec Evolution (2026-03-04)
- **Problem**: 有固定 spec 的 Layer 1-4 無法處理 spec 大改版（如 v1→v2，新增 3 模組、26 API、改 8 張 table）
- **Solution**: Design Pipeline — delta analysis → chunking → per-chunk implementation (inner Layer 1-4) → validation → recursion
- **Architecture**: 向上堆疊，不替換 Layer 1-4。--evolve 是新模式，--plan+--cycles 照常
- **Key decision**: 採用「方案 B 結構 + 方案 C 遞迴」—— Design ↔ Implementation 兩條 Pipeline 遞迴
- **Resume**: 每步快取（design-changes.json, design-chunks-iter{N}.json, {chunk-id}-plan.json）
- **Deploy**: --deploy flag 整合 rsync + SSH 部署
- Commits: a41127e (initial), edfe795 (recursion), a121ecc (partial spec bug fix), a202338 (deployer)

### v0.9 — Experience Learning System (2026-03-04)
- **Problem**: 不同 model 對不同 project type 的表現不一，需要經驗累積
- **Solution**: cost tracking + project classification + adaptive routing
- Commits: 5191435

### v0.7.4 — Closed-Loop Verify (2026-03-04)
- **Problem**: Open-ended LLM spec-diff diverges (33→39→37→45→58 gaps)
- **Solution**: Two-phase closed-loop (regression + bounded scan)
- Commits: 8652d54, 940910c, 2bc63c6, 90b4818

### v0.7.4 — Quality Enforcement (2026-03-04)
- **Problem**: Grok 寫 foundational code 品質太差，cascading fix 修不完
- **Solution**: 三層保護 — complexity floor + force Sonnet + fix task detection
- Commits: ba95824, 66e4ac1

### v0.7.5 — Model Ranking (2026-03-04)
- **Problem**: 外層循環也需要升級機制，不能一直用同一等級 model
- **Solution**: OuterLoopEscalation — gap 收斂 < 15% 自動升 tier
- Commits: 9d6de16

### v0.7.5 — Config-based max_parallel (2026-03-04)
- **Problem**: max_parallel 寫死會綁硬體
- **Solution**: CLI > config.yaml > default 4
- Commits: d2f604a, c622465

## Bug Fixes (重要)

### v1.0 partial spec extraction empty (critical)
- `_extract_partial_spec` 在 `plan_chunks()` 內呼叫，此時 `chunk.changes` 為空（只有 `_change_ids` strings）
- 導致 chunk spec 只有 295 bytes 垃圾內容
- Fix: 用 `getattr(chunk, '_change_ids', ...)` 從 `all_changes` 參數查找實際 DesignChange 物件
- Commit: a121ecc

### v1.0 missing recursion
- 初版 design_loop.py 是線性的（chunk-1→2→...→done），沒有外層遞迴
- Fix: 用 `for design_iter in range(1, max_design_iterations+1)` 包住 Steps 3-5
- 加 `_gaps_to_changes()` 把 final validation gaps 轉回 DesignChange
- Commit: edfe795

### fix-plan path off-by-one
- Cycle N 找 fix-plan-{N}.json 但實際是 cycle N-1 產生的
- Fix: 改成 `fix-plan-{cycle - 1}.json`

### Bounded scan re-reporting known gaps
- New scan 會重複報 regression 已知的 gap
- Fix: 傳 `known_gaps` 參數，prompt 裡加 "ALREADY KNOWN GAPS" 排除區

### Dedup remaining + new gaps
- module|message 相同的 gap 會重複
- Fix: seen_keys set with `module.lower() + "|" + message.lower()`

### Dispatcher factory not wired
- run_cycles() 沒傳 dispatcher_factory → "verify only mode"
- Fix: 加 _DispatcherWrapper class

### SSH env vars not loaded
- 非互動 SSH 不載 .bashrc
- Fix: inline `export XAI_API_KEY=... DEEPSEEK_API_KEY=... PATH=...`

## Run History (carplate project)
| Run | Cycles | Result | Notes |
|-----|--------|--------|-------|
| run-7 | 3 | 40→38→33 gaps | 舊 code，全 Grok，收斂慢 |
| run-8 | 5 | 進行中 | 新 code (force Sonnet + complexity floor + ranking) |
