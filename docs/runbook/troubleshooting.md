# Agent Mesh — Troubleshooting

## git worktree 殘留
```bash
cd ~/work/{project}
rm -rf .agent-mesh
git worktree prune
git branch -D $(git branch | grep agent-mesh/)
```

## aider process 殘留
```bash
pkill -f aider
```

## 記憶體過高
```bash
# 檢查活著的 aider process 數量
ps aux | grep aider | grep -v grep | wc -l
# 應該 <= max_parallel，超過就 kill 多餘的
pkill -f aider
```

## Gemini API 卡住（Phase 1 超過 5 分鐘）
Ctrl+C 重跑。Gemini 冷啟動偶爾會很慢。

## SSH env vars not loaded
非互動 SSH 不載 .bashrc。解法：inline env vars。
```bash
ssh mybox 'export PATH="/home/coreyllm/.local/bin:$PATH" && \
  export XAI_API_KEY="..." && export DEEPSEEK_API_KEY="..." && \
  cd ~/agent-mesh && python3 -m src.orchestrator.main ...'
```

## resume 失敗（沒有 state.json）
用 `git log --oneline` 確認已完成的 wave，然後清掉殘留重跑：
```bash
cd ~/work/{project}
rm -rf .agent-mesh
git worktree prune
```

## 四級複雜度分類參考

| 級別 | 定義 | 首發 Model |
|------|------|-----------|
| L | 純 scaffolding，不 import 其他 task 產出 | grok-4-fast-non-reasoning |
| S | 邏輯簡單，但需 import 其他 task 的 types/services | grok-code-fast-1 |
| M | 中等邏輯，需推理（auth、business rules、tests） | grok-4-fast-reasoning |
| H | 架構、安全、支付、跨模組整合 | grok-4-1-fast-reasoning |

Escalation chain 會自動升級，最終由 Claude Sonnet/Opus 救回。不需要手動干預。
