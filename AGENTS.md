# AGENTS.md — Agent Mesh v0.6.5 Task Assignment Rules

## Overview
Agent Mesh uses multiple CLI agents with **4 model tiers** to complete development tasks in parallel.
Each task gets the right model for its complexity — no overspending Opus on boilerplate.

---

## Section 1: Model Tiers

### Claude Opus (`claude-opus-4-6`)
- **Strengths**: 最強推理、架構設計、安全審計、複雜邏輯
- **When**: H complexity, auth core logic, payment, architecture, code review
- **Cost**: $$$（最貴）
- **Timeout**: 600s
- **Target**: ~15% of tasks

### Claude Sonnet (`claude-sonnet-4-6`)
- **Strengths**: 快速 coding、good enough for M complexity Claude tasks
- **When**: M complexity Claude-routed tasks (auth endpoints, JWT middleware)
- **Cost**: $ （Opus 的 1/5 價格, 3x 速度）
- **Timeout**: 300s
- **Target**: ~15% of tasks

### DeepSeek Reasoner (`deepseek/deepseek-reasoner`)
- **Strengths**: 深度思考、refactoring、debug
- **When**: M complexity non-Claude tasks
- **Cost**: ¢ （極便宜）
- **Timeout**: 300s
- **Target**: ~20% of tasks

### DeepSeek Chat (`deepseek/deepseek-chat`)
- **Strengths**: 速度快、boilerplate、CRUD、tests
- **When**: L complexity, scaffold, config, docs, test writing
- **Cost**: ¢¢（便宜 10x vs reasoner）
- **Timeout**: 120s
- **Target**: ~50% of tasks

---

## Section 2: Routing Decision Tree

```
Task enters Router
  │
  ├─ manual override (plan.json agent_type)? → use specified agent + auto model
  │
  ├─ complexity == "H"? → Claude Opus
  │
  ├─ DeepSeek disabled? → Claude (Opus if H, Sonnet otherwise)
  │
  ├─ DeepSeek keyword match?
  │   (test/config/validation/crud/scaffold/setup/docs/schema/entry point)
  │   → DeepSeek Chat (便宜快速)
  │
  ├─ Claude keyword match?
  │   (auth/security/payment/architect/jwt/websocket/encryption)
  │   ├─ complexity H → Claude Opus
  │   └─ complexity M → Claude Sonnet
  │
  ├─ complexity == "L"? → DeepSeek Chat
  │
  └─ default (M) → DeepSeek Reasoner
```

---

## Section 3: Example Routing (TODO API, 11 tasks)

| Task | Complexity | Agent | Model | Reason |
|------|-----------|-------|-------|--------|
| Initialize project | L | deepseek_aider | chat | DeepSeek keyword |
| Database schema | L | deepseek_aider | chat | DeepSeek keyword |
| Zod validation | L | deepseek_aider | chat | DeepSeek keyword |
| Auth business logic | M | claude_code | **sonnet** | Claude keyword + M |
| JWT Auth Middleware | M | claude_code | **sonnet** | Claude keyword + M |
| Auth API endpoints | M | claude_code | **sonnet** | Claude keyword + M |
| Todo business logic | M | deepseek_aider | reasoner | default M |
| Todo API endpoints | M | deepseek_aider | reasoner | default M |
| Express entry point | L | deepseek_aider | chat | DeepSeek keyword |
| Auth integration tests | M | deepseek_aider | chat | DeepSeek keyword |
| Todo CRUD tests | M | deepseek_aider | chat | DeepSeek keyword |

**Distribution**: Opus 0% / Sonnet 27% / Reasoner 18% / Chat 55%

---

## Section 4: Planner
- **Provider**: Gemini CLI (`echo "prompt" | gemini`)
- **Fallback**: Gemini API → Claude CLI
- **Output**: plan.json with tasks, modules, DAG dependencies

---

## Section 5: Reviewer
- **Provider**: Always **Claude Opus** (最高品質 review)
- Attempt 1-2: Normal review, reject → retry
- Attempt 3: Auto-approve
- Review parse failure: Auto-approve
- `--no-review`: Skip entirely (for testing)

---

## Section 6: Workspace Isolation (v0.6.3+)

**WorkspacePool**: 每個並行 task 拿到自己專屬的 git worktree slot。

```
.agent-mesh/workspaces/
├── slot_0/   ← Task A 獨佔
├── slot_1/   ← Task B 獨佔
├── slot_2/   ← Task C 獨佔
└── slot_3/   ← Task D 獨佔
```

- Slot 數量 = `config.dispatcher.max_parallel`
- acquire → reset to latest main → agent works → merge to main → release
- Merge lock: 一次只有一個 task 可以 merge（防衝突）
- Merge 前先 commit main、rebase、fallback force-merge

---

## Section 7: ReAct Loop + Model Escalation
1. **THINK**: Build prompt (task + history of previous failures)
2. **ACT**: Agent executes in isolated slot
3. **OBSERVE**: git diff, build output, test results
4. **EVALUATE**: Pass or fail?
5. **ESCALATE & RETRY**: 失敗 → 升級 model → 帶著 error 重試 (max 4 attempts)

### Escalation Chain（3 級）
```
Attempt 1: 原始 model (e.g. deepseek-reasoner)
Attempt 2: ⬆ claude-sonnet        ← 帶前次 build error
Attempt 3: ⬆ claude-opus          ← 帶前兩次 error
Attempt 4: ⬆ claude-opus          ← 帶前三次 error（再試一次）
```

| 起始 model | Attempt 2 | Attempt 3 | Attempt 4 |
|------------|-----------|-----------|-----------|
| reasoner | sonnet | **opus** | opus |
| sonnet | opus | opus | opus |
| opus | opus | opus | opus |

---

## Section 8: Heartbeat Timeout
不再用固定時間 timeout。改用心跳機制：

```
Agent 輸出 stdout ──→ 重置 idle 計時器
Agent 寫檔案 ──────→ (透過 stdout 反映)
連續 120s 沒輸出 ──→ 判定卡住 → kill process
max_timeout ───────→ 安全網（絕對上限）
```

| 設定 | 值 | 說明 |
|------|-----|------|
| idle_timeout | 120s | 連續無輸出判定卡住 |
| max_timeout (chat) | 300s | 安全網 |
| max_timeout (reasoner) | 600s | 安全網 |
| max_timeout (sonnet) | 600s | 安全網 |
| max_timeout (opus) | 1200s | 安全網 |

好處：agent 思考 5 分鐘再寫 code 不會被殺，只要有持續輸出。

---

## Section 9: CLI Quick Reference
```bash
# Plan
python -m src.orchestrator.main --spec spec.md --repo ~/project --plan-only

# Execute
python -m src.orchestrator.main --plan plan.json --repo ~/project --no-review -v

# Resume
python -m src.orchestrator.main --plan plan.json --repo ~/project --resume

# Specific modules
python -m src.orchestrator.main --plan plan.json --repo ~/project --module auth api
```
