---
description: 网关运维：看通道健康/额度冷却/成功率，热重载路由，跑自检与故障注入
argument-hint: "[status|reload|test|live|start|stop]"
---

加载 `ai-steward` 与 `model-routing` 技能。动作：$ARGUMENTS

- `status`（默认）：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/gateway_ctl.py status`，再用一句话说清"哪条通道不健康、要不要换首选"。
- `reload`：`curl -X POST http://127.0.0.1:8788/admin/reload`，然后 `status` 复核。
- `test`：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/selftest.py`（零成本检查 + 故障注入）。
- `live`：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/selftest.py --live`（真实请求，消耗少量 token）。
- `start` / `stop`：`C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/gateway_ctl.py start|stop`。

汇报格式：在线状态 / 通道数 / 冷却中的 lane / 成功率最低的 lane / **累计成本与未定价次数**（`/admin/status.totals`）/ 建议动作。
