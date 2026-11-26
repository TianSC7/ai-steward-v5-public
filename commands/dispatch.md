---
description: 派单：把一句话需求转成任务卡、挑模型、开 worker 会话并登记台账
argument-hint: "<要做什么> [--project <项目路径>] [--tier cheap|auto|heavy|reason]"
---

加载 `ai-steward` 技能，按第一至五节执行。需求：$ARGUMENTS

步骤：
1. 写成六字段任务卡（目标/验收/边界/优先级/依赖/风险）。
2. 查 `config/steward.json` 的 `dispatch_guard`：命中 `blocked_paths` 不派，改用 canonical 路径。
3. 按 `model-routing` 技能选别名（派单写 `new-provider-7/<alias>`）；用户指定 `--tier` 时以其为准。
4. `ListSessions(query="<任务关键词>", archived="include")` 查重，避免重复开窗。
4.4 **五经理制席位闸（不可跳过；2026-11-20 发起人令）**
   `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/factory_seat.py check --seat <席位号>`
   - 经理席只有 **B-001 / C-101 / D-101 / E-101 / F-001** 五个；`check` 返 3（冻结席/不在册）→ **不得派**；
   - **经理席只复用，不 CreateSession**：用 `SendToSession` 把任务发给注册表里的持久会话；
   - 只有 W 员工席才允许 `CreateSession`，prompt 必须带 `SEAT=W-...` 与 `ORDER=...`；开完 `record-worker` 登记；
   - 提示词出现“你是X经理 / 席位 X-101 / X101经理 / 非正席编号”会被 `seat_guard.py` 识别；**实测 ZCode 本版对 CreateSession 不触发用户 PreToolUse，钩子拦不住真实创建**——所以本步靠人（check）+ 5 分钟 `MES-Seat-Audit` 普查兜底，发现注册表外经理立即冻结。
4.5 **并发占位（先查后派，不可跳过）**——状态根必须是生产绝对路径
   CMD:        set STEWARD_STATE_DIR=C:/Users/Administrator/Documents/mate/plugins/ai-steward/state
   PowerShell: $env:STEWARD_STATE_DIR="C:/Users/Administrator/Documents/mate/plugins/ai-steward/state"
   `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/lane_claim.py claim --lane <R4|R4-2|STEP|NEX|GLM|OUTSOURCE> --seat <席位号> --order <订单号> --phase reserve --ttl 300 [--prep-id <id>]`
   - 退出码 0 → 记下回显的 `slot_id` 与 `prep_id`，进步骤 5；
   - 退出码 3（`LANE_FULL` / `SESSION_BUSY`）→ **不 CreateSession**，单留 queued，回一行「lane <L> 满 used/cap，排队第 n 位」；
   - 退出码 4（`LEDGER_UNAVAILABLE`）→ 默认**不派**（fail-closed）；确需继续须显式加 `--force-degraded` 并在回执写「降级放行」，
     且该开关**只对账本不可用生效**，不越过 `LANE_FULL`/`SESSION_BUSY`/`LEASE_EXPIRED`。
5. `ListProjects` 取 project 对象。**经理席：`SendToSession` 到注册表会话，禁止 `CreateSession`**；**W 员工席：才用 `CreateSession`**，prompt 按第三节模板改写并带 `SEAT=W-...` / `ORDER=...`。
5.5 **立刻开全权**（跳过就会卡在「等待授权」）：
   `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/set_permission.py --session <返回的 sessionId>`
   确认输出 `{"mode":"yolo"}`；该命令只改本机 ZCode 库的会话权限（旧值自动备份到
   `~/.zcode/cli/permission-backup-*.json`，可还原），运行中的会话实时生效。
5.6 **会后（拿到 sessionId）立即绑定**（把真实幂等键 `sha1(lane|seat|sessionId|commandId)[:16]` 写进账本）：
   `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/lane_claim.py claim --slot <slot_id> --session <sessionId> --command <commandId> --phase bind --ttl 900`
   - 退出码 0 → 绑定完成（`BIND`/`REPLAY`），长任务进 6.5 的心跳；
   - 退出码 3 `LEASE_EXPIRED`（slot 已被回收）→ **先按新 claim 重占**（同 lane/seat/order、新 `--prep-id`、`--phase reserve`），重占成功再 bind；
     重占时 lane 已满 → 返 3 并在流水记 `ORPHAN`（该 session 已在跑但无格），交 F 巡场认领，**不得**「无格继续跑」被静默接受。
5.7 **W 员工席登记（仅 CreateSession 的 W 席）**：
   `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/factory_seat.py record-worker --seat W-xxx --order <订单号> --session <sessionId>`
   - 经理席不走本步；经理席只 SendToSession，不产生新 session。
6. 写入 `C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/registry.json`（id 用 `W-YYYYMMDD-NN`），
   并把 `operation.commandId` 记到 `last_command_id`（`/steer` 中断时要用它取 executionId）。
6.2 **订单签字闸（写 delivered 前必跑）**：
   `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/factory_seat.py check-order --order <订单号>`
   - 订单卡必须有 `## 流程表`（环节/责任席/执行员工/模型/起/止/交付物/签字）与 `## 员工签字`（员工/任务/模型/起/止/会话/证据/签字）；
   - exit 3（缺表/缺 W 签字/缺会话/缺证据/缺模型）→ 只能 `internal-done`，不得写 `delivered`；
   - 一席一模型：R4/STEP 混写会被 WARN，须改到一致。
6.5 **收尾（或交班）必须释放**：
   `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/lane_claim.py release --slot <slot_id> --reason done`
   长任务每 10 分钟续租：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/lane_claim.py heartbeat --slot <slot_id>`
   漏做由惰性 reap 回收并留 `EXPIRE` 流水（reserve 300s / 正式 900s）。
7. 向用户回一行：`已派单 <id> → <sessionId>，模型 <alias>，验收 <一句话>`。
