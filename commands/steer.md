---
description: 立即纠偏：中断正在跑偏的 worker，立刻投递修正指令（不排在该轮任务后面）
argument-hint: "<sessionId|任务关键词> <纠偏指令>"
---

加载 `ai-steward` 技能，按第七节「实时纠偏」执行。参数：$ARGUMENTS

步骤：

1. **定位会话**：参数以 `sess_` / `session_` 开头则直接使用；否则 `ListSessions(query="<关键词>")`，
   多个命中时选最近更新且未归档的一个，并在汇报里写明选了哪个。
2. **取执行句柄**：读 `state/registry.json` 中该任务的 `last_command_id`；
   没有就 `SendToSession` 一条"先报告当前进度与正在执行的命令"拿到 `operation.commandId`。
3. **查当前执行**：`WaitSessions(operations=[{sessionId, commandId}], mode="any", timeoutMs=0)`；
   只有 `status="running"` 且有 `executionId` 才进入中断。
4. **中断**：`InterruptSession(sessionId, expectedExecutionId)`；
   若返回 execution changed / 不匹配，重取一次再试，仍失败则跳过中断。
5. **立即投递纠偏**：`SendToSession` 三段话术——
   「哪里跑偏了 / 立即停什么 / 改成怎么做 + 验收标准」；把返回的 `commandId` 写回
   `registry.last_command_id`。
6. **验证**：`WaitSessions(..., timeoutMs=30000)` + `ReadSession(sessionId, view="messages")`，
   确认 worker 回执；在任务的 `steer_events` 追加一条：
   `{ts, reason, interrupt: applied|queued|failed, ack, latency_sec}`。
7. **汇报**：中断结果（applied/queued/failed）、worker 回执原文、是否还需要人工介入。

纪律：只对「跑偏 / 危险操作 / 卡死」使用；不因风格差异打断；不打断用户正在看的最终答复；
每次中断必须留痕，没有执行句柄时如实说"已排队，未能即时中断"。
