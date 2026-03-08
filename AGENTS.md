# Agent Mesh — Claude Code 操作手冊

> 此文件供 Claude Code 在 coreyllm 上操作 Agent Mesh 使用。
> 用戶通常從手機透過 Claude remote 下達指令。

---

## 環境

- Agent Mesh 位置：`~/work/agent-mesh`
- 專案工作區：`~/work/{project}/`
- Python 虛擬環境：`cd ~/work/agent-mesh && source .venv/bin/activate`
- 設定檔：`~/work/agent-mesh/config.yaml`（v0.7.1，四級路由 L/S/M/H）

---

## 核心工作流程

### 1. 新專案啟動

用戶會說類似「讀 Notion 上的 XXX spec，建新專案跑 agent mesh」。

```bash
# Step 1: 從 Notion 讀 spec（用 notion-fetch 或 notion-search 取得內容）
# Step 2: 建立專案目錄
mkdir -p ~/work/{project}
# Step 3: 寫入 spec.md
cat > ~/work/{project}/spec.md << 'EOF'
{Notion 內容}
EOF
# Step 4: 初始化 git
cd ~/work/{project}
git init && git checkout -b main && git add -A && git commit -m "initial: spec.md"
# Step 5: 跑 plan-only
cd ~/work/agent-mesh && source .venv/bin/activate
python3 -m src.orchestrator.main \
  --spec ~/work/{project}/spec.md \
  --repo ~/work/{project} \
  --plan-only -v 2>&1 | tee ~/work/{project}/plan.log
# Step 6: 回報 routing preview，等用戶確認
```

### 2. 執行（用戶確認後）

```bash
cd ~/work/agent-mesh && source .venv/bin/activate
python3 -m src.orchestrator.main \
  --plan ~/work/{project}/plan.json \
  --repo ~/work/{project} \
  --max-parallel 3 --no-review -v 2>&1 | tee ~/work/{project}/run.log
```

### 3. 從中斷恢復

```bash
cd ~/work/agent-mesh && source .venv/bin/activate
python3 -m src.orchestrator.main \
  --plan ~/work/{project}/plan.json \
  --repo ~/work/{project} \
  --max-parallel 3 --no-review --resume -v 2>&1 | tee -a ~/work/{project}/run.log
```

如果 `--resume` 失敗（沒有 state.json），用 `git log --oneline` 確認已完成的 wave，
然後清掉殘留重跑：
```bash
cd ~/work/{project}
rm -rf .agent-mesh
git worktree prune
```

---

## 日誌與監控

所有執行都必須加 `2>&1 | tee ~/work/{project}/run.log`。

| 用戶問的 | 你做的 |
|---------|--------|
| "跑到哪了" / "進度" | `tail -30 ~/work/{project}/run.log` |
| "有錯誤嗎" | `grep -i "error\|failed\|❌" ~/work/{project}/run.log` |
| "完整 summary" | `tail -50 ~/work/{project}/run.log` |
| "哪些 task 完成了" | `grep "✅" ~/work/{project}/run.log` |
| "哪些 task 失敗了" | `grep "❌" ~/work/{project}/run.log` |
| "現在跑第幾個 wave" | `grep "Wave" ~/work/{project}/run.log \| tail -5` |
| "用了多少 Claude" | `grep "claude" ~/work/{project}/run.log \| grep "✅"` |

---

## 回報規則

### Plan 完成後回報格式：

```
✅ Plan 產生完成
- {N} tasks，{M} modules
- L: {n} tasks（scaffolding）
- S: {n} tasks（import context）
- M: {n} tasks（reasoning）
- H: {n} tasks（architecture）
- Claude 預估使用：{n} tasks（僅 escalation）
要開始執行嗎？
```

### Execute 完成後回報格式：

```
✅ 執行完成
- 完成：{N}/{N}
- 一次成功率：{X}%
- Agent 分佈：Grok {X}% / DeepSeek {X}% / Claude {X}%
- 平均 escalation：{X}
- 耗時：{X} 分鐘
```

### 執行失敗時：

```
❌ 執行中斷
- 完成：{done}/{total}
- 失敗 task：{task_name}（{error}）
- 建議：{resume 指令或修復建議}
```

---

## Notion 整合

### 讀取 Spec

用 `notion-search` 找到 spec page，用 `notion-fetch` 讀取完整內容，寫入 `~/work/{project}/spec.md`。

### 更新開發紀錄

執行完成後，將 summary 寫入 Notion 開發紀錄頁面。
目前的開發紀錄頁面：`https://www.notion.so/3143577c477b8126ae73f2bcc836127b`

---

## 重要參數

| 參數 | 說明 | 建議值 |
|------|------|--------|
| `--max-parallel` | 同時跑的 slot 數 | coreyllm: 5，MacBook Air: 3 |
| `--no-review` | 跳過人工 review | 遠端執行時永遠加 |
| `--plan-only` | 只產 plan 不執行 | 第一步永遠先跑這個 |
| `--resume` | 從中斷恢復 | 需要 state.json 存在 |
| `-v` | verbose log | 永遠加 |

---

## 故障排除

### git worktree 殘留
```bash
cd ~/work/{project}
rm -rf .agent-mesh
git worktree prune
git branch -D $(git branch | grep agent-mesh/)
```

### aider process 殘留
```bash
pkill -f aider
```

### 記憶體過高
```bash
# 檢查活著的 aider process 數量
ps aux | grep aider | grep -v grep | wc -l
# 應該 <= max_parallel，超過就 kill 多餘的
pkill -f aider
```

### Gemini API 卡住（Phase 1 超過 5 分鐘）
Ctrl+C 重跑。Gemini 冷啟動偶爾會很慢。

---

## 四級複雜度分類參考

| 級別 | 定義 | 首發 Model |
|------|------|-----------|
| L | 純 scaffolding，不 import 其他 task 產出 | grok-4-fast-non-reasoning |
| S | 邏輯簡單，但需 import 其他 task 的 types/services | grok-code-fast-1 |
| M | 中等邏輯，需推理（auth、business rules、tests） | grok-4-fast-reasoning |
| H | 架構、安全、支付、跨模組整合 | grok-4-1-fast-reasoning |

Escalation chain 會自動升級，最終由 Claude Sonnet/Opus 救回。不需要手動干預。
