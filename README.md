# AI 大管家（ai-steward）

ZCode 插件：**你只给目标，管家负责派活、盯进度、纠偏、汇报**；附带一个本地模型聚合网关，做同能力容灾。

> **这是 V5.0 工厂内核的公开快照（已做隐私清洗，只含工厂本体），定格在这一版，不是在役仓。** 哪些内容没带、哪些值换成了占位符，见 [`PUBLISH_NOTES.md`](PUBLISH_NOTES.md)。

> 只想快速上手：看 [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)（四个命令、三个场景、权限与故障处置）。

## 它解决什么

以前的形态：你要盯每个会话，AI 停下来你要去戳一下。
现在的形态：你对管家说「把 X 做完」，管家写成任务卡、挑模型、开会话、定时巡检、卡住就拉起；只有真需要你拍板时才找你。

## 组成

```
ai-steward/
  .zcode-plugin/plugin.json     插件清单
  docs/CHARTER.md              岗位说明书（目的/身份/职责/权限红线）
  docs/ARCHITECTURE_V2.md      v2 自治调度架构（任务台/调度引擎/知识库/选型取舍）
  skills/ai-steward/SKILL.md    管家协议（接令→派单→巡检→纠偏→收尾）
  skills/model-routing/SKILL.md 模型选型规则
  commands/steward.md           /steward   巡检、接管
  commands/dispatch.md          /dispatch  派单
  commands/steer.md             /steer     立即中断并纠偏
  commands/gateway.md           /gateway   网关运维
  hooks/hooks.json              自动放行钩子（PreToolUse / PermissionRequest）
  scripts/steward_board.py      任务台：唯一写入口（原子写+锁+证据门禁+状态机）
  scripts/steward_cycle.py      调度引擎：台账→确定性动作（派单/验收/升级链/预算）
  scripts/steward_kb.py         私人知识库：事实卡/事故卡 + 中文检索
  scripts/test_board.py         任务台用例（19 例）
  scripts/test_cycle.py         调度引擎用例（28 例）
  scripts/test_kb.py            私人知识库用例（14 例）
  scripts/permission_allow.py   自动放行判定（按工作区 + 台账会话）
  scripts/set_permission.py     派单后把会话设为完全访问（yolo，旧值备份）
  scripts/exec_handle.py        无 commandId 时从 ZCode 库列执行句柄候选
  scripts/gateway.py            本地聚合网关（stdlib，零依赖）
  scripts/gateway_ctl.py        起停/状态/开机自启
  scripts/steward_scan.py       项目+会话+网关+成本四源取证
  scripts/usage_facts.py        成本/速度取证（只读 usage-bar + 网关台账）
  scripts/fingerprint.py        测试脚本版本指纹/环境自检/看门狗
  scripts/selftest.py           自检 + 故障注入
  scripts/test_edges.py         边界用例（mock 上游，零 token）
  scripts/test_tools.py         工具边界（句柄/全权/放行钩子，16 例）
  scripts/cron_health.py        定时任务健康检查（停摆/过期/目标漂移）
  scripts/test_cron_health.py   定时健康用例（11 例）
  scripts/key_vault.py         本地 DPAPI 密钥保险库（只出掩码、条目不可删）
  scripts/test_key_vault.py     密钥保险库用例（19 例）
  scripts/worker_worktree.py    worker 隔离工作区：ensure/status/merge/remove/finish（git worktree）
  scripts/test_worktree.py      隔离工作区用例（41 例：并行隔离/冲突回滚/清理不伤主仓）
  scripts/run_all.py            总闸：16 道门全绿才算通过
  config/routing.json           路由配置（不含密钥）
  config/ROUTING_POLICY.md      选型政策与实测画像
  config/steward.json           监控项目 + dispatch 闸（并发/预算/项目映射）
  config/KEY_POLICY.md          密钥信任三档与数据红线（low/high/official）
  state/registry.json           任务台账（只经 steward_board.py 写）
  state/dispatcher_prompt.md    常驻调度台作业手册（每轮照做）
  knowledge/                    私人知识库（entries/ facts/ incidents/ + INDEX）
  reports/evidence/             每个任务的证据文件（done 的硬条件）
```

## 网关

- 地址：`http://127.0.0.1:8788/v1`，OpenAI 协议，ZCode 里注册为 `Steward Auto`（`new-provider-7`）。
- 模型别名：`auto` / `auto-cheap` / `auto-heavy` / `auto-reason`；也可直接点名 lane（`r4-dsv41` 等）。
- 密钥不落网关配置：运行时从 `~/.zcode/v2/provider_config.json` 按 providerId 读取。
- **会话粘性**：同一对话锁定同一模型，不因对话变长而中途换模型；只有通道真失败才在同能力组内切换。
- 失败冷却：auth 1h / quota 15min / rate 5min / 5xx 2min / conn 1min。
- 台账：`logs/gateway-YYYYMMDD.jsonl`；查询：`/admin/status`、`/admin/ledger`。

常用命令：

```bash
python -X utf8 scripts/gateway_ctl.py status
python -X utf8 scripts/gateway_ctl.py start|stop|restart
python -X utf8 scripts/gateway_ctl.py install     # 登录自启（用户级，免管理员）
python -X utf8 scripts/selftest.py                # 零成本检查 + 故障注入
python -X utf8 scripts/selftest.py --live         # 真实请求（少量 token）
python -X utf8 scripts/test_edges.py              # 边界用例（零 token，15 项）
python -X utf8 scripts/run_all.py                 # 总闸，改配置后必须全绿
```

测试脚本启动时会在 stderr 打一行
`FP ai-steward@0.1.0 script=<sha12> schema=1 env=OK`：清单/市场版本漂移、脚本不在插件目录、
路由配置损坏都会直接以退出码 2 拒绝运行；每个脚本另有整体看门狗（超时退出码 3），
总闸对十六个门禁子进程（巡检边界/边界用例/网关自检/巡检取证/插件出厂/成本取证/预算分桶/台账边界/工具边界/定时健康/任务台/调度引擎/私人知识库/密钥保险库/隔离工作区/台账）逐一断言
`FP ` 指纹行存在且 `env` 非 BAD，防止"旧副本/旧进程假 PASS"。

## 实时纠偏（/steer）

巡检发现 worker 跑偏时，普通 `SendToSession` 只会排队，等它做完这一轮才看到。`/steer` 走的是立即路径：

```text
ReadSession(sessionId, view="summary")     → 运行中必带 status + executionId
InterruptSession(sessionId, executionId)   → 立即中断当前执行
SendToSession(sessionId, "哪里错 / 立即停什么 / 改为做什么+验收")  → delivery=start_now
WaitSessions + ReadSession                 → 验证回执
```

备用句柄：台账里的 `last_command_id` + `WaitSessions(timeoutMs=0)`；没有句柄的历史会话用
`scripts/exec_handle.py --session sess_...` 从 ZCode 的 `session_input` 表只读列候选。

实测：`InterruptSession` 返回 `applied`，随后 `SendToSession` 返回 `delivery="start_now"`，
worker 在 20s 窗口内回 `STEER-OK`；新会话实测 `CUTIN-OK`（证据
`reports/evidence/W-20261117-STEER.md`，调度台闭环 `W-20261117-DISPATCH.md`）。

**发送方纪律**：同一会话无法抢占自己的当前轮，巡检/纠偏必须跑在常驻空闲的调度台会话里
（`registry.dispatcher` 记录 sessionId + cron_id）。旧调度台在钩子安装前启动、不具备免授权能力，
已停用待命。

## 权限与自动放行

ZCode 的 `CreateSession` 固定以 build 模式建会话，build 下写文件/会话管理工具会弹授权，
无人值守会直接卡死。三层处理：

1. **派单即开全权**：`scripts/set_permission.py --session <id>` 把该会话改成 `{"mode":"yolo"}`
   （只改本机 ZCode 库的会话行，旧值自动备份到 `~/.zcode/cli/permission-backup-*.json`，运行中实时生效）。
2. **自动放行钩子**：`hooks/hooks.json` 注册 PreToolUse / PermissionRequest，对
   `config/steward.json → autoallow.roots`（mate/cadsys/newcad）与 registry 已登记会话返回 allow；
   文件类工具还会检查**目标路径**：未登记会话必须在 `roots`（或系统临时目录）内；
   已登记会话不受 roots 限制，但 `protected_paths`（`~/.ssh`、`provider_config.json`、
   ZCode 配置/会话库、`C:/Windows` 等）对任何会话都永不放行。
   未登记会话的兜底由 `workspace_fallback` 控制（默认 true；设为 false 则只放行登记会话）。
   总开关 `state/autoallow.on`，删掉即恢复逐项询问。
3. **钩子加载时机**：配置钩子只在会话启动时读取；装钩子/改钩子后必须新开会话验证。

实测：新 worker 用 Write 写工作区内、外两个文件，全程无 `needs_attention`，回执 `WRITE-DONE`
（`reports/evidence/W-20261117-PERM.md`）。

## 定时任务健康（巡检不会静默停摆）

巡检跑在 ZCode 自动化上；自动化会因禁用、派发失败、目标会话被删或 `next_run_at` 过期而**静默停摆**
（会话看起来 idle，但永远不会再巡检）。把这种静默失败变成可判定结果：

```bash
python -X utf8 scripts/cron_health.py --from-registry
# CRON automation-... OK next=2026-11-17 23:33:18 last=- run=0 ...
# HEALTH OK (1 checked)
```

状态：`OK` / `STALE`（过期超 grace，默认 5 分钟）/ `DISABLED` / `ERROR` / `NO-NEXT` /
`NOTFOUND` / `TARGET-MISMATCH`（目标会话与 registry.dispatcher 不一致）。退出码 0 全 OK、3 需处置、2 库不可读。
总闸的「定时健康」门用 11 个临时库用例锁住这些判定（`test_cron_health.py`）。

## 成本与速度（复用你已装的 usage-bar）

管家直接只读调用 `~/.zcode/plugins/usage-bar/zusage.py`：

```bash
python -X utf8 scripts/usage_facts.py          # 今日成本 + 当前会话 speed/token/工具错误
python -X utf8 scripts/usage_facts.py --json   # 机器可读
```

- 今日总额、按模型成本、缓存命中量、当前会话 token/上下文/工具耗时与错误数。
- **别名流量由网关自己计价**：ZCode 按所选 `model_id`（`auto` 等）记账，usage-bar 会显示"未定价"；
  网关转发时拿到上游真实模型与 `cached_tokens`，按同一张 `prices.json`（含峰谷、缓存命中价）算成本，
  暴露在 `/admin/status` 的 `totals.cost_cny` / `tokens_cached` / `unpriced`。
- usage-bar 缺席时管家降级为"成本不可用"，不阻塞派单与巡检。

## 安装（用户在 UI 里手动完成）

1. **插件市场 → 添加 → 添加插件市场**，粘贴市场根目录：`C:/Users/Administrator/Documents/mate/plugins`
2. **个人 → dev-mate-ai-steward → AI 大管家 → 安装**
3. 新建任务，在输入框的技能选择器里选 `ai-steward`，发送试用提示（也可先试 `/steer`）：

```text
巡检一遍：运行 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/run_all.py，
把结果和今日成本报给我，不要修改任何文件。
```

预期：五段式汇报（结论/证据/成本/已处置/需要你），总闸 16/16 通过。

当前状态：**市场已添加（dev-mate-ai-steward）、插件已安装并启用（ai-steward 0.1.0）**；
`zcode plugins validate` 通过；试用待用户在 UI 里发第一条 `/steward`。

> 技能与命令里的脚本路径按绝对路径写死在本目录；安装后请保留
> `C:/Users/Administrator/Documents/mate/plugins/ai-steward`，不要移动或删除。

## 设计来源（不是拍脑袋）

- `musistudio/claude-code-router`：本地网关 + 别名 + 有序 fallback + 每请求日志
- `BerriAI/litellm`：`model_name` 别名映射多 deployment；complexity_router 四分层
- `QuantumNous/new-api` 52k★ / `songquanpeng/one-api` 37k★：聚合与分发，不做按任务选型
- 我们的差异：**选型在 ZCode 派单时完成；网关只保证同能力不断线**

## 已知边界

- 网关只透传 `openai-chat-completions` 协议；`anthropic-messages` 通道由 ZCode 直连，不经过网关。
- `SendToSession` 会保留目标会话原模型，不能远程换模型；换模型 = 新开会话。
- 插件市场需用户在 ZCode 里手动添加（Plugin Marketplace → Add → 填 `C:/Users/Administrator/Documents/mate/plugins`）。
- 插件清单**不声明** `hooks` 字段：ZCode 会自动发现 `hooks/hooks.json`；显式声明会报
  `Duplicate plugin hooks file ignored` 警告（已实测，`diagnostics: []` 才是干净状态）。
- `zcode plugins list` 里的 `commands: 1` 是**命令根目录数**，不是命令文件数（4 个 `/命令` 都正常注册）。
