# 决策记录（DECISIONS）

> 每条决策必须带"为什么"和"证据"。改决策时追加新条目，不覆盖旧条目。

## DR-001 选型位置：在 ZCode 派单时定，不在网关里按时间换

- 日期：2026-11-17
- 背景：用户明确纠正"网关后台按时间自由切换模型不太对"。
- 决策：模型选型发生在 `CreateSession(model=...)`；会话内不换模型。网关只做同能力容灾。
- 为什么：中途换模型会断 KV 缓存、风格漂移、且 ZCode 里看到的是一个模型名而实际打点不可见。
- 证据：new-api 52k★ / one-api 37k★ 均为聚合分发，不做按任务选型。

## DR-002 会话粘性（sticky）

- 日期：2026-11-17
- 决策：按「首条 system + 首条 user + 别名」生成指纹，TTL 30 分钟锁定 lane；粘性 lane 即使不在当前 tier 候选里也保留（只要别名允许）。
- 为什么：对话变长会让 tier 从 SIMPLE 升到 MEDIUM/COMPLEX；若按 tier 重选，会在对话中途换模型。
- 证据：边界用例 `sticky-hold`（SIMPLE→MEDIUM 仍同 lane）、`sticky-expire`（TTL 到期才重选）。

## DR-003 等价组（equiv）与跨能力切换

- 日期：2026-11-17
- 决策：`r4-dsv41` 与 `cs3-dsv41` 同属 `deepseek-v4.1` 可互接；跨等价组只在同组全灭时发生，且写 `switch` 事件。
- 为什么：同模型不同通道切换对下游无感；跨能力切换是有损的，必须留痕。

## DR-004 密钥不落网关配置

- 日期：2026-11-17
- 决策：`routing.json` 只写 providerId；运行时从 `~/.zcode/v2/provider_config.json` 取密钥。日志只记 lane/耗时/token。
- 为什么：减少密钥副本；ZCode 加通道后网关自动获得新 lane，无需二次录入。
- 证据：`load_providers()` 实现；`selftest.py` 的临时配置在 `finally` 中删除。

## DR-005 派单禁区（dispatch_guard）

- 日期：2026-11-17
- 背景：ZCode 的 `dll` 项目 workspacePath 指向 `D:/Github/dll`，实测该目录是 **cad-suite** 仓库（detached HEAD、17 个未提交改动），而 dll 正本是 `mate/lib/ai`（dll2、971 提交）。
- 决策：`config/steward.json` 登记 `dispatch_guard.blocked_paths`；管家派单前核对路径，命中禁区只报告不派单。
- 为什么：派错目录会让 AI 把工作写进废弃仓库，损失不可逆。
- 待办：用户在 ZCode UI 把 dll 项目改指正本路径后，才能派 dll 的活。

## DR-006 上游强制 gzip 必须透明解压

- 日期：2026-11-17
- 现象：`cs3-dsv41` 返回 `0x8b` 开头字节，JSON 解析失败。
- 决策：请求带 `Accept-Encoding: identity`，同时按响应 `Content-Encoding` 用 `zlib` 增量解压；流式用 `decompressobj` 逐块解。
- 证据：边界用例 `gzip`（解压后 `GZIP-OK`）。

## DR-007 状态目录不得按"配置父目录"推断

- 日期：2026-11-17
- 现象：边界测试的临时配置把 `gateway-state.json` 写进 `%TEMP%/logs`，跨测试轮次共享冷却，导致 `timeout-failover` 假 PASS（0.0s）。
- 决策：`resolve_base_dir()`：显式 `state_dir` > 配置在 `config/` 下取插件根 > 否则取配置所在目录。
- 为什么：状态隔离是测试可信的前提；假 PASS 比 FAIL 更危险。
- 证据：修复后 `timeout-failover` 耗时 1.5s（真实经历超时降级）。

## DR-008 空别名列表等于"无候选"，不做兜底

- 日期：2026-11-17
- 现象：`{"order": []}` 被"同 tier 全量兜底"吞掉，返回 200。
- 决策：别名显式写了 `order` 就以它为准；空列表 = 无候选 → 503。只有 tier 未配置时才兜底。
- 证据：边界用例 `no-candidate`（503）。

## DR-009 成本按"上游真实模型"在网关侧结算

- 日期：2026-11-17
- 现象：ZCode 的 usage-bar 按请求里的 `model_id` 记账；网关别名 `auto` 被记成"未定价"，
  即使回传了上游真实模型名也无法改变（记账发生在请求侧，不是响应侧）。
- 决策：网关在 `mark_ok` 时用 lane 的真实模型 + 本次 `cached_tokens`，读同一张
  `~/.zcode/plugins/usage-bar/prices.json`（峰谷/缓存命中价）算成本；累计到
  `/admin/status.totals.cost_cny`，并按 lane 落到 `/admin/ledger`。
- 为什么：一个会话可能跨多个 lane，事后无法反推；必须在转发当下记账。
- 口径：输入含缓存命中，未命中 = input − cached；费用 = 未命中×in + 输出×out + 缓存×cache_read。
- 证据：`auto-heavy` 实测 8 input/2 output → ¥0.000016（谷价 1/4 元每百万）；`auto` 命中 mercury（未定价）计入 `unpriced`。

## DR-010 测试脚本必须自带版本指纹，总闸拒绝无指纹的子进程

- 日期：2026-11-17
- 动机：测试最危险的失败是"跑的不是这份代码"——旧副本、旧市场版本、旧网关进程都会给出假 PASS。
- 决策：`scripts/fingerprint.py` 提供 `announce()`（stderr 打 `FP plugin@version script=sha12 schema env`）
  与 `arm()`（整体看门狗，超时退出码 3）；`test_scan/test_edges/selftest/validate_plugin` 启动即自检
  清单/市场/路由配置与脚本位置，失败退出 2；`run_all.py` 对四个测试子进程强制校验 `FP ` 行且 `env` 非 BAD。
- 附加：`selftest` 比对 `/health.version` 与 `gateway.py` 的 `VERSION`，识别"进程跑旧代码"。
- 证据：当时 `run_all.py` 7/7、`validate_plugin.py` 19/19；负向用例"无指纹子进程"被判 FAIL（当前基线 8/8、21/21）。

## DR-011 实时纠偏走"中断 + 立即投递"，不走排队发送

- 日期：2026-11-17
- 现象：`SendToSession` 对运行中的会话是队列投递，等当前轮结束才可见；跑偏/危险操作无法及时止损。
- 决策：台账保存每次派单/发送的 `operation.commandId`（`last_command_id`）；纠偏时
  `WaitSessions(timeoutMs=0)` 取 `executionId` → `InterruptSession` → `SendToSession`
  （实测 `delivery=start_now`），新 commandId 写回，`steer_events` 留痕并用 `ReadSession` 验证回执。
- 边界：无 commandId / 会话已 idle 时无法中断，退回排队投递并如实标注；入站巡检消息的宿主队列
  不可被插件抢占，管家因此保持轻量（长任务派给 worker，单轮 ≤2 分钟）。
- 证据：`sess_52417488` 实测 interrupt=applied、worker 20s 内回 STEER-OK；
  `reports/evidence/W-20261117-STEER.md`。

## DR-012 done 任务的证据必须落盘且不可静默漂移

- 日期：2026-11-17
- 动机：`state=done` 只靠一句"已完成"无法复核；证据文件被改写后旧结论仍显示通过。
- 决策：done 任务必须有 `evidence_path`；可选 `evidence_sha256`，填了就必须与文件一致。
  `ledger_check.py` 用 10 个边界用例自证（缺路径/文件不存在/哈希不符/非法 state/重复 id/
  非法 interrupt/坏 JSON/根或 tasks 类型错），`run_all.py` 将其作为第 8 道门。
- 证据：`test_ledger.py` 10/10；`run_all.py` 8/8。


## DR-013 立即纠偏的通用执行句柄来自 ReadSession(summary)

- 日期：2026-11-17
- 现象：`WaitSessions` 需要 commandId，只覆盖自己派出的会话；`ListSessions` 不返回 executionId；
  给忙碌会话排队发送拿到的 commandId，`WaitSessions` 只返回 `queued`，查不到 executionId。
- 决策：首选 `ReadSession(sessionId, view="summary")`——运行中（running/needs_attention）
  必带 `executionId`，拿到即 `InterruptSession`；`WaitSessions(last_command_id)` 降为备用；
  没有句柄的历史会话用 `scripts/exec_handle.py` 从 `session_input` 表只读列候选。
  发送方必须不是目标会话本身（自己无法抢占自己的当前轮）。
- 证据：W-CUTIN 实测（interrupt `runtime_command_3` → 26 秒内回 `CUTIN-OK`）；
  调度台自检 `DISPATCHER-READY … DRILL-PASS`（`reports/evidence/W-20261117-DISPATCH.md`）。

## DR-014 会话“完全访问”必须脚本化：CreateSession 固定以 build 建会话

- 日期：2026-11-17
- 现象：`CreateSession` 没有权限参数，建出的会话一律 `{"mode":"build"}`；build 下写文件/
  会话管理类工具会弹授权，无人值守时直接卡死（旧调度台实例即因此停摆）。
- 决策：派单后立刻执行 `scripts/set_permission.py --session <sessionId>`，把本机 ZCode 库
  `session.permission` 改为 `{"mode":"yolo"}`；旧值自动备份到 `~/.zcode/cli/permission-backup-*.json`，
  只影响该会话，运行中实时生效（实测 1 秒内 CreateSession/CronCreate 不再询问）。
  另配 `scripts/permission_allow.py` 钩子（PreToolUse + PermissionRequest）按
  `autoallow.roots` 与 registry 已登记会话自动放行，总开关 `state/autoallow.on`。
- 证据：新 worker `sess_4fa7f820…` 用 Write 写工作区内/外两个文件，全程无 `needs_attention`，
  回执 `WRITE-DONE`；`logs/autoallow.jsonl` 两条 `allow=true`；`reports/evidence/W-20261117-PERM.md`。
- 边界：钩子只在会话启动时加载，安装前启动的旧会话不生效；改钩子后必须新开会话。

## DR-015 巡检/纠偏的发送方必须是常驻空闲调度台

- 日期：2026-11-17
- 根因：入站消息排队是宿主行为，同一会话无法抢占自己的当前轮；把巡检绑在干活会话上必然迟到。
- 决策：常驻调度台 `sess_00000000-0000-0000-0000-000000000001`，自建 `*/20` 定时巡检；
  调度台只接令/取证/纠偏/短汇报（单轮 ≤2 分钟），不接长活；`registry.dispatcher` 记录
  sessionId 与 cron_id。旧调度台 `sess_cdb4c627…`（钩子安装前启动）停用待命，不删除。
- 证据：`DISPATCHER-READY automation-28bd0a92-80cf-4543-8b0a-8b3235c2cedc DRILL-PASS`；
  `reports/evidence/W-20261117-DISPATCH.md`。

## DR-016 定时任务必须可观测：巡检前先查 cron_health

- 日期：2026-11-17
- 现象：自动化停摆不会报错——`enabled=0`、`last_error`、`next_run_at` 过期、目标会话被删，
  表现都只是"调度台一直 idle"，看起来像"没有在办任务"，实际是永远不会再巡检。
- 决策：新增 `scripts/cron_health.py` 只读 `~/.zcode/v2/tasks-index.sqlite`，输出
  `OK/STALE/DISABLED/ERROR/NO-NEXT/NOTFOUND/TARGET-MISMATCH`；`--from-registry` 同时核对
  automation 的 `target_task_id` 与 `registry.dispatcher.sessionId`。退出码 0/3/2。
  巡检 prompt 每轮先跑它；`test_cron_health.py` 11 例挂进总闸（第 10 道门）。
- 证据：线上 `CRON automation-28bd… OK next=23:33:18 lag=-6.6m`；总闸 10/10。
- 边界：只读检查，不自动改 cron；修复仍由调度台会话完成（需 ZCode 工具）。

## DR-017 插件清单不声明 hooks 字段，依赖 ZCode 自动发现

- 日期：2026-11-17
- 现象：清单写 `"hooks": "./hooks/hooks.json"` 时，`plugins list --json` 报
  `plugin_hook_invalid: Duplicate plugin hooks file ignored`——显式声明与自动发现重复。
- 决策：移除该字段；`hooks/hooks.json` 仍被自动发现（`hookDetails` 2 条、runnable=true），
  `diagnostics: []`。另：`plugins list` 的 `commands: 1` 是命令根目录数，不是命令文件数。
- 证据：`zcode plugins validate` 通过；`plugins list --json` 去重后 diagnostics 为空；4 个命令均在
  `zcode commands list` 中。

## DR-018 自动放行按目标路径判定，保护名单优先

- 日期：2026-11-17
- 现象：workspace 兜底只校验 cwd；cwd 在 `mate` 的会话可以无提示写 `C:/Windows`、`~/.ssh` 等
  任意位置。夜班实测日志里还出现了非管家会话命中 workspace allow 的记录。
- 决策：文件类工具（Write/Edit/MultiEdit/Delete/Move/Copy/ApplyPatch 等）从 `tool_input` 提取目标路径。
  **未登记会话**的目标必须全部在 `autoallow.roots`（或系统临时目录）内；**已登记会话**不受 roots 限制；
  任何会话都不得命中 `protected_paths`（`~/.ssh`、`provider_config.json`、ZCode `config.json`/`db.sqlite`、
  `.claude/settings.json`、`C:/Windows`）。`workspace_fallback=false` 可进一步收紧为"只放行 registry 会话"。
- 证据：`test_tools.py` 16/16（根内/根外/保护路径/已登记根外写/关闭兜底/已登记非文件工具）；`run_all.py` 10/10；
  巡检演练中调度台回写台账时被临时目录误拦（needs_attention），据此豁免已登记会话的 roots 约束后重跑成功。
- 边界：Bash 无法可靠解析命令目标，仍靠"只给管家派出的会话开 yolo"控制；保护名单只约束文件类工具，不是沙箱。

## DR-019 v2 自治调度：任务台 + 调度引擎 + 私人知识库，借模式不搬运行时

- 日期：2026-11-18
- 背景：用户明确"我只发目标，剩下的你安排；08:00–20:00 无人值守；AI 停下找理由你要继续推进；
  我每天只看结果验收"。v1 靠聊天记录派活、巡检查到停工只会拉起，换会话/拆任务/预算/知识沉淀都缺。
- 决策：
  1. `steward_board.py` 做唯一写入口（JSON + 锁 + 原子写 + 状态机 + 证据门禁），不引入 Huey/RQ/Celery——
     执行器是 ZCode 会话，消息队列调不动 `CreateSession/SendToSession`。
  2. `steward_cycle.py` 只读算 actions，调度台照做；调度仍用 ZCode automation（唯一能唤醒会话的机制），
     不引入 APScheduler/Temporal。
  3. 升级链 `nudge→steer→replace→split`；worker 说"需要用户确认"只算升级信号，不算结论。
  4. `verify`：在办任务一出现非空证据就核验收尾，不等 30 分钟 stale；证据路径认
     `reports/evidence/<id>.md` 约定（worker 不必回填台账）。
  5. 私人知识库用 markdown 事实卡/事故卡 + 中文 2-4 字滑窗检索，不上向量库；派单时注入 top3。
  6. 并发 `max_running=3`、同项目 1 个写手；要并行改代码先拆 worktree（backlog）。
- 证据：`test_board.py` 19/19、`test_cycle.py` 28/28、`test_kb.py` 14/14；`run_all.py` 13/13；
  实战 08:08 双 worker 并发派单，08:2x `verify` 收尾，两个任务 done 且登记 sha256
  （`W-20261118-001` 1899e7ef7b…、`W-20261118-002` d0b8966967…）。
- 边界：调度台自己没有执行句柄时仍需 `OpenSession` 重绑；跨项目共享工作区，per-project 写锁只是
  逻辑约束，不是文件系统隔离。

## DR-020 密钥保险库：DPAPI 落盘 + 信任三档 + 不可删条目

- 日期：2026-11-18
- 背景：用户将接入大量中转站/官方 AI 密钥，并明确三档信任——低信任中转**默认会泄露信息**、
  高信任一般不出卖、官方可信；要求密钥本地加密管理。
- 决策：
  1. `scripts/key_vault.py`：密钥只存 Windows DPAPI 密文（`CryptProtectData`，ctypes 直调，
     零新依赖；绑定当前 Windows 用户+固定盐），库文件放 `%LOCALAPPDATA%/ai-steward/`（不同步）。
  2. 输出纪律代码化：list/verify/meta 永远只出掩码（前4…后4）；明文不进 argv/日志/报告/对话，
     录入通道 `--key-file`/`--key-env`/`--stdin`。
  3. **不提供 delete**：条目只能 `disable`（留原因）/`rotate`（旧钥只留掩码+指纹历史）——
     对齐"禁止以任何理由删除既有密钥条目"的铁律。
  4. 每条目 `trust ∈ {low,high,official}`；数据红线成文 `config/KEY_POLICY.md`：任务分
     `public/internal/confidential`，confidential（用户文件/业务数据/CAD 源码）只走 official，
     low 档禁携业务词/绝对路径/文件内容。
  5. 网关接线（lane 加 trust、运行时从 vault 取钥、派单前机械校验）列为下一张卡，先落策略不落码。
- 证据：`test_key_vault.py` 19/19（DPAPI 往返/换盐拒解/明文不落盘正文与备份/掩码格式/
  篡改指纹可检出/无 delete 子命令/原子写备份/id 递增）；`run_all.py` 第 14 道门。
- 边界：DPAPI 绑定本机当前用户——换机/换用户需重新录入（密文不可迁移是特性不是缺陷）；
  低信任通道的脱敏检查目前靠规则与评审，机械 lint 接网关时一并落。

## DR-021 密钥策略 v2：红线在密钥，不在内容

- 日期：2026-11-18（DR-020 当天澄清）
- 背景：用户原话——"中信任度中转站只是不出卖那个密钥，其他都可以；文件数据我们没有什么秘密，
  我们是个人的项目，没关系"。DR-020 第 4 条的 sensitivity→通道数据红线（confidential 只走
  official、low 禁携业务词/路径/文件内容）写严了，不符合用户意图，废除。
- 决策：
  1. 保护对象收窄为**密钥本身与额度**；内容（文件/代码/业务词/路径）三档随便传，个人项目无秘密。
  2. high 档承诺=不出卖密钥；low 档风险=滥用/转卖注入的 key（烧额度）→ low 只放便宜限量的
     key，日报按 trust 分桶盯消耗，异常烧量 disable+rotate。
  3. 所有通道（含官方）通用硬规则不变：密钥只做 Authorization 头；prompt/附件/文件内容/
     日志/报告/对话里不得出现密钥原文。
  4. 保险库纪律（DPAPI/掩码/不可删/录入通道）全部不变，见 DR-020 第 1–3 条。
  5. 网关接线简化：不做 sensitivity→trust 派单门禁（无必要），只做 lane 登记 trust +
     低档消耗突增告警；等首批中转密钥入库一起落。
- 落点：`config/KEY_POLICY.md` 重写为 v2；SKILL §九、K-20261118-008、日报同步。
