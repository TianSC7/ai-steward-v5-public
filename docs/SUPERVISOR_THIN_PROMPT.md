# 薄版厂长巡检 prompt（2026-11-19 架构整改）

> 用途：替换“管家统筹巡检（每30分钟）”旧 prompt。原则：厂长只做**动作分发**，
> 不重读 PLAN/DECISIONS/DIGEST 全文，不扫会话列表，不重复巡检。确定性判断全部
> 交给 `steward_cycle.py`。

```text
【厂长巡检·薄版】cd C:/Users/Administrator/Documents/mate/plugins/ai-steward；
python -X utf8 scripts/steward_cycle.py --json > state/cycle_last.json；
只按 actions 行动：
- dispatch → 按 action.prompt 建会话/发单，claim 后立刻 set_permission yolo；
- unblock → 给 action.sessionId 发一条短指令（≤10 行），不展开讨论；
- reject-done → 按 reason 处理：冻结副本问题走 reattest，工作树 drift 只记一行；
- report → 记入当日日报，不回复 worker；
- budget/queue-empty/progress-alert → 各记一行，不自行扩编。
不读 PLAN/DECISIONS/DIGEST 全文，不扫会话列表，不重复巡检。回执 ≤5 行。
```

## 配套自动化节拍（2026-11-19 起）

| 自动化 | 真实节拍 | 处置 | 理由 |
|---|---|---|---|
| 旧厂长统筹巡检 `automation-4ed1c4c1` | 30 分钟 | **暂停（enabled=0）** | 每次唤醒重载 4,600 条消息，约 280 万 token/次 |
| 调度台·薄版 `automation-7fa88a72` | 30 分钟 | **启用（接替）** | 新会话零历史，prompt 466 字，只处理 cycle actions |
| M-SAFETY 巡场 `automation-756563a8` | 5 分钟 | 保持 | `cron_expr` 是 `* * * * *` 显示表达式，真实节拍在 `schedule_rule`（interval=5） |
| M-PLAN 自唤醒 `automation-9b7c1e5d` | 10 分钟 | 暂不动 | 观察一周再定；若仍高耗再降到 60 分钟 |

## 纪律

- 一轮只处理 `cycle_last.json` 里的 actions；没有 action 就回 `IDLE` 结束。
- 任何“再确认/再汇总”都不做；状态以 board 与冻结证据为准。
- P0（不可逆/生产风险/上级失联）才允许主动 `SendToSession`，每小时 ≤3 条。
