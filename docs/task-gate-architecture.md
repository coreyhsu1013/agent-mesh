# Task-Gate Architecture (v2.0)

## 1. 現況問題

原本的 Agent Mesh 架構：
```
Spec → Planner → TaskPlan → Dispatcher → ReactLoop → Reviewer → Merge
```

問題：
1. **Planner 產出的 Task 缺乏 gate metadata** — 只有 title/description/target_files，沒有 task-specific 驗收條件
2. **ReactLoop 的 _observe 負責所有驗證** — generic build/test 檢查，無法做 task-specific 驗證
3. **Reviewer 是 LLM review** — 不應承擔 deterministic quality gate（build/test/path/dependency）
4. **缺少 gate profile 系統** — 沒有「不同類型 task 用不同檢查策略」的機制
5. **缺少 gate result persistence** — failure 原因沒有結構化記錄

## 2. 重構後架構

```
Spec → Planner → Task Enrichment (GateProfile) → TaskPlan
  → Dispatcher
    → ReactLoop / Agents (generic observe for retry)
    → Deterministic Gate Runner (task-specific validation)
    → Reviewer (high-level LLM sanity check)
    → Merge
```

### 角色分工

| 元件 | 角色 | 負責 |
|------|------|------|
| ReactLoop._observe | Fast-fail for retry | build/test error → 讓 agent 在 loop 內重試 |
| GateRunner | Deterministic gate | task-specific path/rule/secret/build/test 驗證 |
| Reviewer | LLM review | 高階 code quality、architecture sanity check |

### 執行順序（在 Dispatcher._execute_task_in_slot）

1. ReactLoop 執行 task（內含 observe → retry 循環）
2. Task completed → **GateRunner.run()** 跑 deterministic checks
3. Gate pass → **Reviewer.review()** 跑 LLM review（可選）
4. Review pass → 進 merge

## 3. Task Model 新欄位

| 欄位 | 類型 | 說明 |
|------|------|------|
| `task_type` | str | 任務類型: api, schema, auth, ui, test, integration, general |
| `input_requirements` | list[str] | 輸入需求（預留） |
| `constraints` | list[str] | 限制條件（預留） |
| `deliverables` | list[str] | 交付物（預留） |
| `gate_profile` | dict | 序列化的 GateProfile |
| `gate_results` | list[dict] | 每次 gate run 的結果 |
| `retry_reason` | str | retry 原因 |
| `escalation_reason` | str | 需要 escalate 的原因 |
| `verification_artifacts` | dict | 驗證用的 artifacts（預留） |

所有欄位都有 safe fallback — 舊 plan.json 完全相容。

## 4. Gate Profile 概念

GateProfile 定義一組 deterministic checks，分為五個階段：

```python
@dataclass
class GateProfile:
    name: str                        # e.g. "api_basic"
    input_checks: list[str]          # 檢查 task 輸入（target_files, acceptance_criteria）
    format_checks: list[str]         # 輸出格式檢查（預留）
    rule_checks: list[str]           # 規則檢查（path, dependency, secret）
    verification_checks: list[str]   # 驗證檢查（diff, build, test）
    escalation_checks: list[str]     # 升級標記（auth, migration — advisory）
```

### 內建 Profiles

| Profile | 用途 | Rule Checks | Verification | Escalation |
|---------|------|-------------|--------------|------------|
| `coding_basic` | 通用 coding | path, secret | diff | — |
| `api_basic` | API / CRUD | path, secret | diff, build | — |
| `critical_backend` | auth/security/payment | path, dep, secret | diff, build, test | auth/payment |
| `schema_critical` | schema/migration | path, secret | diff, build | migration |
| `integration_basic` | webhook/sync | path, secret | diff, build, test | — |
| `ui_operability_basic` | UI/page | path, secret | diff, build | — |
| `playwright_infra_basic` | playwright infra | path | diff | — |
| `e2e_smoke_gate` | e2e test | path, secret | diff, build, test | — |

### Profile 解析優先順序

1. Task 上明確設定的 `gate_profile.name`
2. Heuristic：從 title/description/category 關鍵字推斷
3. 預設：`coding_basic`

## 5. Deterministic Checks vs Reviewer

### Deterministic Gate（GateRunner）

- **Hard validation** — fail = task fails
- 檢查項目：
  - `target_files_defined` — task 有定義 target files
  - `acceptance_defined` — task 有驗收條件
  - `allowed_paths_only` — 改動在預期路徑內
  - `no_new_dependency` — 沒有新增依賴
  - `no_secret_leak` — 沒有 secret 洩漏
  - `diff_not_empty` — 有實際改動
  - `build_pass` — build 通過
  - `tests_pass` — test 通過
  - `auth_or_payment_touched` — 碰到敏感檔案（advisory）
  - `migration_detected` — 偵測到 migration（advisory）

### Reviewer（LLM）

- **Soft validation** — high-level sanity check
- 不負責 build/test/path/dependency
- 保留既有 auto-approve 策略
- 只在 gate pass 後才執行

## 6. 如何新增 Gate

### 新增 Check

1. 在 `src/gates/checks/basic.py`（或新檔案）加入 check function：
   ```python
   def my_check(task, diff="", workspace_dir="", **kwargs) -> tuple[bool, str]:
       # return (passed, detail_message)
       return True, "Check passed"
   ```
2. 註冊到 `CHECK_REGISTRY`
3. 在需要的 profile 裡加入 check name

### 新增 Profile

1. 在 `src/gates/profiles.py` 定義 GateProfile instance
2. 加入 `ALL_PROFILES` dict
3. 如需 heuristic 匹配，在 `src/gates/registry.py` 的 `_PROFILE_HEURISTICS` 加入規則

### 新增 Check 類別

目前 checks 都在 `basic.py`。如果需要更複雜的 check（例如 AST 分析、security scan），建議：
1. 新增 `src/gates/checks/advanced.py`
2. 註冊到 `CHECK_REGISTRY`
3. 相應 profile 引用新 check

## 7. 檔案清單

```
src/
├── models/task.py          # GateProfile, GateResult, Task 新欄位
├── gates/
│   ├── __init__.py         # 模組入口
│   ├── profiles.py         # 8 種 gate profile 定義
│   ├── registry.py         # Profile 查表 + heuristic 解析
│   ├── runner.py           # GateRunner — 執行 gate checks
│   └── checks/
│       ├── __init__.py
│       └── basic.py        # 10 個 deterministic check 實作
├── orchestrator/
│   ├── planner.py          # _enrich_tasks() 自動補 gate_profile
│   ├── gemini_planner.py   # _apply_defaults 加 gate 欄位
│   ├── dispatcher.py       # gate runner 插入 reviewer 前
│   └── react_loop.py       # TaskResult.observation_artifacts
docs/
└── task-gate-architecture.md  # 本文件
```
