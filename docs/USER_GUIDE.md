# AI 大管家 · 使用手册（给人看的）

> 一句话：**你只对管家说目标；它负责开窗口、盯进度、纠偏、只把要你拍板的事报给你。**
> 操作细则在 `skills/ai-steward/SKILL.md`，岗位边界在 `docs/CHARTER.md`；本页只讲怎么用。

## 一、每天早上看什么

1. 新开一个任务，输入 `/steward`（或选 `ai-steward` 技能）→ 得到五段式汇报：
   `【结论】【证据】【成本】【已处置】【需要你】`。
2. 详细日报：`reports/DAILY_REPORT_YYYYMMDD.md`。
2b. 一页看板：`reports/board.html`（浏览器打开；`python -X utf8 scripts/board_view.py` 随时重生成）。
3. 任务台账：`state/registry.json` 的 `tasks[]`（`state` 只能是
   `queued/dispatched/running/blocked/done/failed/abandoned`；done 必须有非空证据文件）。
4. 只验收结果：看 `done` 任务的 `evidence_path` 与 `evidence_sha256`；过程（选型、踩坑、
   换过几个会话）不用看，需要你拍板的才会出现在【需要你】。

## 二、四个命令

| 命令 | 用途 | 典型说法 |
|---|---|---|
| `/steward` | 巡检、接管、汇报 | “现在到哪了？只报需要我拍板的。” |
| `/dispatch` | 把一句话变成任务卡并开 worker | “把 X 做完，验收是 Y。” |
| `/steer` | 立即中断并纠偏（不排队） | “第三个窗口跑偏了，改成 Z。” |
| `/gateway` | 模型网关运维（状态/起停） | “网关还活着吗？成本多少？” |

## 三、三个高频场景

### 1. 派活

```text
把「修复 CadPerception 桥接超时」做完。
验收：pytest tests/test_bridge.py 全绿。
边界：别动 D:/Github 下的旧仓库。
```

管家会：写六字段任务卡并落卡（`steward_board.py add`）→ 选模型 → 调度引擎排到它 →
`CreateSession` 开 worker → 立刻 `set_permission.py` 开全权 → `claim` 回填 sessionId/commandId
→ 之后调度台每 20 分钟自走（派单/验收/升级链），你不看过程。

**并发**：默认最多 3 个 worker；同一项目默认只跑 1 个写手，避免两个会话踩同一工作区。

### 2. 查进度（不想被打扰）

```text
巡检一遍，没有新证据就别汇报。
```

管家只回：成了/卡住/需要你一句话；不会把“我打算怎么做”当汇报。

### 3. 现场纠偏

```text
窗口 sess_xxx 在乱改配置，立即停，只让它改 state/registry.json。
```

`/steer` 的实际链路：`ReadSession(summary)` 取运行中 `executionId` → `InterruptSession`
→ `SendToSession`（`start_now`）→ 20 秒内读回执。**不是排队等它做完。**

## 四、权限与安全（为什么 worker 不再弹授权）

- ZCode 的 `CreateSession` 固定 build 模式；管家派单后立刻把该会话改成 `yolo`（完全访问），
  旧值备份在 `~/.zcode/cli/permission-backup-*.json`，可还原。
- 自动放行钩子只覆盖：台账里的管家会话 + `autoallow.roots`（mate/cadsys/newcad）下的未登记会话。
- 文件类工具检查目标路径：未登记会话只能写 roots/临时目录；`protected_paths`（`~/.ssh`、
  `provider_config.json`、ZCode 配置/会话库、`C:/Windows`）对任何会话都不放行。
- **一键停用自动放行**：把 `state/autoallow.on` 改名或删掉，立刻恢复逐项询问。
- **密钥保险库**：中转站/官方密钥只存 `scripts/key_vault.py` 管的本地 DPAPI 加密库
  （`%LOCALAPPDATA%/ai-steward/key_vault.json`，不同步）。输出永远只有掩码；条目只能
  停用/轮换、不能删除。信任三档与"哪档能用什么"见 `config/KEY_POLICY.md`
  （v2：内容三档随便传——个人项目没秘密；要护的只是密钥本身，低信任中转只放便宜限量的 key 并盯消耗）。
  给密钥的标准姿势：放进临时 txt → 管家 `add --key-file` → 你删 txt。
- 铁律：不删除用户文件、不外传密钥、不擅自部署/外发消息。

## 五、巡检与自动化

- 常驻调度台：`state/registry.json → dispatcher.sessionId`，每 20 分钟跑
  `cron_health.py --from-registry` + `steward_cycle.py --json`，按 actions 执行：
  `dispatch` 派新单、`verify` 有证据就验收、`check` 超时才升级（nudge/steer/replace/split）。
  无任务只回一行“巡检 HH:MM：无在办任务”。
- 新的闭环：worker 写完 `reports/evidence/<id>.md`，下一轮 `verify` 自动核验并 `done`
  （登记 sha256），不用等你回来点完成。
- 健康检查：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/cron_health.py --from-registry
```

  `OK` 正常；`STALE/DISABLED/ERROR/NO-NEXT/NOTFOUND/TARGET-MISMATCH` 需要处置（退出码 3）。
- 调度台停摆时：看 `registry.dispatcher`，重开一个会话，用 `CronCreate` 重建 `*/20`，
  更新 `dispatcher.sessionId/cron_id`。旧会话停用不删。

## 六、常见故障

| 现象 | 先看什么 | 处置 |
|---|---|---|
| 会话卡在 `needs_attention` | 最后一条消息/工具名 | 多半是权限或“变更前确认”；修钩子或手动授权一次，再发续跑指令 |
| worker 不回话 | `ReadSession(summary)` 是否 running | 有 executionId 就中断+三段式纠偏；idle 则查台账验收是否已满足 |
| 网关 502/超时 | `gateway_ctl.py status` | `start`/`restart`；lanes 全灭才考虑换 provider |
| 成本显示“未定价” | 请求模型是否是 `auto*` 别名 | 正常；网关在 `/admin/status.totals` 自算真实成本 |
| 命令/技能不出现 | 是否旧会话 | 命令与配置钩子只在会话启动时加载，新开会话 |
| 定时不再触发 | `cron_health.py --from-registry` | 按上面“调度台停摆”重建 |

## 七、停止与恢复

- 停止某个自动化：ZCode「自动化」页暂停/删除；夜班自查也可在会话里 `CronPauseSelf`。
- 恢复权限询问：移走 `state/autoallow.on`，或把 `autoallow.roots` 清空。
- 还原某会话权限：从 `~/.zcode/cli/permission-backup-*.json` 找到旧值，改回 `session.permission`。
- **无人值守（unattended）技能已按用户要求停用，不要恢复**；大管家不需要它。

## 八、验收基线（对外声称“可用”前必须全绿）

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/run_all.py
```

- 总闸 **14/14**；插件出厂 21/21；工具边界 16/16；定时健康 11/11；台账边界 10/10；
  任务台 21/21；调度引擎 29/29；私人知识库 14/14；密钥保险库 19/19。
- 最近实测：2026-11-18 08:2x 总闸 13/13 + 双 worker 并发派单/验收闭环（`reports/evidence/W-20261118-001.md`、`-002.md`）。
