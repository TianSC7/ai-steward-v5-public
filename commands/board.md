---
description: 看板：一键汇总任务台账并刷新 reports/board.html 只读看板（浏览器直接打开，60 秒自刷）
argument-hint: "[refresh|summary] [--now <ISO时间>]"
---

加载 `ai-steward` 技能。动作：$ARGUMENTS

- `refresh`（默认；无参数时同此）：
  `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/board_view.py`
  只读渲染 `C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/registry.json` +
  `C:/Users/Administrator/Documents/mate/plugins/ai-steward/config/steward.json` →
  `C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/board.html`
  （坏台账时落一篇可读错误页，不抛栈；退出码 0=成功，2=台账/参数错误）。
  跑完用一句话汇报三栏摘要：Working / Blocked / Done 各几张卡、今日完结数与今日成本、哪张卡最久不动。
- `summary`：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py --json list`
  机器可读汇总；三栏口径与看板一致（Working=queued/dispatched/running，Blocked=blocked/failed，Done=done/abandoned）。
- 回放/测试：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/board_view.py --now 2026-11-18T12:00:00+08:00`。

用法示例：
- `/board` → 刷新看板 + 口头三栏摘要。
- `/board summary` → 只要 JSON 汇总，不重画 HTML。
- `/board refresh --now 2026-11-18T12:00:00+08:00` → 按指定时刻渲染（只测试用）。

纪律：看板生成脚本只读，不写 registry、不改配置；`reports/board.html` 是 gitignore 产物、随时可重建，别手改；
看板打不开或长得像错误页时，把错误页首行念给用户，不要静默重试。
