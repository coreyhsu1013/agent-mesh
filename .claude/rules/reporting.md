---
description: Report format conventions for agent-mesh operations
globs: "**/*"
---

# Reporting Rules

## Plan 完成後回報格式
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

## Execute 完成後回報格式
```
✅ 執行完成
- 完成：{N}/{N}
- 一次成功率：{X}%
- Agent 分佈：Grok {X}% / DeepSeek {X}% / Claude {X}%
- 平均 escalation：{X}
- 耗時：{X} 分鐘
```

## 執行失敗時
```
❌ 執行中斷
- 完成：{done}/{total}
- 失敗 task：{task_name}（{error}）
- 建議：{resume 指令或修復建議}
```

## Log Monitoring Queries
| 用戶問的 | 你做的 |
|---------|--------|
| "跑到哪了" / "進度" | `tail -30 {log}` |
| "有錯誤嗎" | `grep -i "error\|failed\|❌" {log}` |
| "完整 summary" | `tail -50 {log}` |
| "哪些 task 完成了" | `grep "✅" {log}` |
| "哪些 task 失敗了" | `grep "❌" {log}` |
