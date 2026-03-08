# Agent Mesh — 操作指南

## Manual Mode 信號機制
- **重要**: 用 `touch` 建立檔案，不是寫內容到 STATUS.txt
- 信號檔案位置: `{repo}/.agent-mesh/CONTINUE`, `SKIP`, `STOP`
- 操作:
  - 繼續: `touch ~/afatech-erp/.agent-mesh/CONTINUE`
  - 跳過當前 chunk: `touch ~/afatech-erp/.agent-mesh/SKIP`
  - 停止: `touch ~/afatech-erp/.agent-mesh/STOP`
- Manual mode 會在兩個時間點暫停:
  1. **PRE-EXECUTE**: 顯示 plan 內容，等確認
  2. **POST-VERIFY**: 顯示 verify 結果 + gaps，等確認
- 每次 CONTINUE 後信號檔會被清除，下次暫停要重新 touch

## 常用監控指令
```bash
# 查進度
ssh mybox "tail -30 ~/afatech-erp/evolve-run-11.log"

# 查 process
ssh mybox "ps aux | grep 'src.orchestrator' | grep -v grep"

# 查目前跑到哪個 chunk
ssh mybox "cat ~/afatech-erp/.agent-mesh/design-progress.json | python3 -m json.tool"

# 查 DB tables
ssh mybox "docker exec test-postgres psql -U afatech -d afatech_erp -c 'SELECT tablename FROM pg_tables WHERE schemaname='\''public'\'' ORDER BY tablename'"

# 查 git 最新 commits
ssh mybox "cd ~/afatech-erp && git log --oneline -10"

# 查 verify report
ssh mybox "cat ~/afatech-erp/.agent-mesh/verify-report-*.json | python3 -m json.tool"

# 查 fix plan
ssh mybox "cat ~/afatech-erp/.agent-mesh/fix-plan-*.json | python3 -m json.tool"
```

## Evolve 執行模式
```bash
# 啟動 evolve (manual mode)
ssh mybox "cd ~/agent-mesh && nohup ./run.sh --evolve \
  --spec-old ~/afatech-erp/spec-v1.0.md \
  --spec-new ~/afatech-erp/spec.md \
  --repo ~/afatech-erp --manual \
  > ~/afatech-erp/evolve-run-N.log 2>&1 &"

# 啟動 evolve (auto mode, 不暫停)
ssh mybox "cd ~/agent-mesh && nohup ./run.sh --evolve \
  --spec-old ~/afatech-erp/spec-v1.0.md \
  --spec-new ~/afatech-erp/spec.md \
  --repo ~/afatech-erp \
  > ~/afatech-erp/evolve-run-N.log 2>&1 &"
```

## Fix Cycle 模型升級規則
- Cycle 1: 從 routing matrix 的起點開始（通常 Grok）
- Fix tasks (cycle 2+): force skip Grok/DeepSeek，直接 **Sonnet**
- Sonnet 失敗 → escalate to **Opus**
- Gap 收斂 < 15% → 全局升 tier (OuterLoopEscalation)
- Opus 最多重試 3 次，timeout × 1.5

## Git 同步流程
- 本機改 agent-mesh code → `git push origin main`
- coreyllm 拉: `ssh mybox "cd ~/agent-mesh && git pull origin main"`
- **注意**: coreyllm 跑 evolve 時不要 pull，等跑完再同步
- 目前 coreyllm 有未 commit 的手動改動，pull 前要 `git stash`

## Log 檔案命名
- afatech-erp: `~/afatech-erp/evolve-run-{N}.log`
- carplate (舊): `~/carplate/.agent-mesh/cycles-run-{N}.log`

## 核心工作流程

### 1. 新專案啟動
```bash
# Step 1: 從 Notion 讀 spec
# Step 2: 建立專案目錄
mkdir -p ~/work/{project}
# Step 3: 寫入 spec.md
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

## 重要參數
| 參數 | 說明 | 建議值 |
|------|------|--------|
| `--max-parallel` | 同時跑的 slot 數 | coreyllm: 5，MacBook Air: 3 |
| `--no-review` | 跳過人工 review | 遠端執行時永遠加 |
| `--plan-only` | 只產 plan 不執行 | 第一步永遠先跑這個 |
| `--resume` | 從中斷恢復 | 需要 state.json 存在 |
| `-v` | verbose log | 永遠加 |

## Directory Structure on coreyllm
```
~/agent-mesh/          # orchestrator code (git repo)
~/carplate/            # target project (git repo)
  ├── plan.json        # initial plan
  ├── spec.md          # project spec
  └── .agent-mesh/
      ├── workspaces/  # git worktree slots (slot_0, slot_1, ...)
      ├── agent-mesh.db           # SQLite state (task status, diffs)
      ├── verify-report-{N}.json  # verify results per cycle
      ├── fix-plan-{N}.json       # generated fix plans
      └── cycles-run-{N}.log      # execution logs
```
