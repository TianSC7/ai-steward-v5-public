---
description: 管家巡检：查任务台账、取证项目/会话活跃度、必要时拉起卡住的会话，只报需要拍板的事
argument-hint: "[巡检|汇报|接管 <目标>]"
---

加载 `ai-steward` 技能，按第六节执行巡检。参数：$ARGUMENTS

默认动作（无参数时）：
1. 读 `C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/registry.json` 取在办任务。
2. 运行 `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_scan.py --json` 取证（含项目/会话/网关/成本四段）。
3. 逐个裁决：healthy 不打扰；stalled 发拉起话术并更新台账（同一任务最多 3 次）。
4. 用决策优先格式汇报：**结论 / 证据 / 成本 / 已处置 / 需要你**；没有要拍板的就写"无"。

若参数是"接管 <目标>"：把目标转成任务卡并派单（见 `/dispatch`）。
