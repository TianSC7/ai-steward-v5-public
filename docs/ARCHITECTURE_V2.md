# 大管家 v2 · 自治调度架构

> 目的（用户 2026-11-18 原话）：**我只发目标，剩下的你安排；08:00–20:00 电脑没人，AI 停下找理由
> 你要继续推进它，而不是等用户；我每天只看结果验收。** 技术选型/问题/方向由管家处理。

## 一、v1 缺什么

v1 已经有：会话派单、实时纠偏、自动放行、网关容灾、20 分钟巡检、台账与证据。
但它是**手动管家**：

- 任务靠聊天记录，换会话就丢；
- 巡检查到停工只会“拉起”，没有换会话/拆任务/换路线的升级链；
- 没有并发上限与项目级写锁，多个 worker 可能踩同一工作区；
- 没有预算闸，成本只能事后看；
- 没有私人知识库，踩过的坑下次还会踩。

v2 补的是**任务台 + 调度引擎 + 私人知识库**，让“接令→派单→盯办→纠偏→验收→复盘”
变成可恢复、可审计、可并发的闭环。

## 二、总体结构

```text
用户（只发目标）
   │
   ▼
大管家会话（接令/拆卡/选型/汇报）
   │  board add（落卡）
   ▼
state/registry.json  ◄──── 调度台（常驻空闲会话，每 20 分钟 cron 唤醒）
   ▲                          │  steward_cycle.py --json（只读，算动作）
   │                          │  按 actions 执行 ZCode 工具
   └──── board claim/done/steer/pull/progress（原子写+锁）
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
          worker A        worker B        worker C       （并发上限 3，同项目 1 个写手）
              │               │               │
              └──── 证据文件 + 三段式回执 ─────┘
                              │
                              ▼
                 reports/evidence/<task>.md + sha256
                              │
                              ▼
                 日报 / 异常才找人（默认不打扰）
```

## 三、三件套

### 1. 任务台 `scripts/steward_board.py`

- 唯一写入口；读改写全程 `registry.json.lock` 排他锁，超 60 秒视为陈旧锁自动清理。
- 写盘：同目录临时文件 + `flush/fsync` + `os.replace`，写前留 `.bak-<时间>`。
- 状态机：`queued → dispatched → running → done`；`blocked/failed` 可回 `queued` 重试；
  `done/abandoned` 终态。非法流转必须 `--force`。
- `done` 强制要求证据文件存在且非空，自动登记 sha256——没有证据不算完成。
- `fail --retry` 按尝试次数退避（60s/120s/240s…上限 30 分钟），到 `max_attempts` 停止重派。
- `steer/pull/progress` 分别记录中断、拉起、真实进展，供升级链判定。

### 2. 调度引擎 `scripts/steward_cycle.py`

只读，不调用 ZCode 工具；输出确定性的 `actions`，调度台照着执行。核心判定：

| 动作 | 触发 | 调度台怎么做 |
|---|---|---|
| `dispatch` | queued + 依赖 done + 有空位 + 未超预算 | CreateSession → set_permission → claim |
| `verify` | 在办任务已出现非空证据文件 | 读回执核验收尾：达标 `done`，不达标打回 |
| `check` | 在办任务超过 `stale_sec` 无进展 | 按 pulls 分级 nudge/steer/replace/split |
| `unblock` | blocked | 按诊断清单解卡，代拍后继续 |
| `replace` | failed 未到上限 / 连续无进展 | 换新会话/新路线重派 |
| `split` | 到尝试/拉起上限 | 拆 2-3 个可独立验收的子任务 |
| `reject-done` | done 但证据缺失/哈希不符 | 打回 running，要求补证据 |
| `replan` | 依赖失败/不存在 | 重排依赖或改派 |
| `report` | failed/blocked 未进日报 | 写日报，不打扰用户 |
| `budget` | 今日成本 ≥ 预算 | 停止派新单，写日报 |
| `progress-alert` | 有活但长时间零进展 | 强制换路线 |
| `queue-empty` | 工作时段（08:00–20:00）队列为空 | 从日报/backlog 取一项落卡，杜绝夜班零产出 |
| `idle` | 无在办、无排队（非工作时段） | 只回一行“无在办任务” |

**升级链（用户头号要求）**：`pulls=0 → nudge`，`1 → steer`，`2 → replace`，`3+ → split`。
worker 说“需要用户确认”不是结论；只有删除用户文件、外发消息、上线部署三类允许升级给用户。

**并发与写锁**：`max_running=3`，`max_running_per_project=1`。worker 共享工作区，
同仓库并行改代码会互相踩；要并行先拆 worktree/分支（借 agent-orchestrator 的 per-task worktree 模式）。

### 3b. 密钥保险库 `scripts/key_vault.py`（2026-11-18）

- 中转站/官方密钥的唯一落盘点：Windows DPAPI 密文（绑定本机当前用户），存
  `%LOCALAPPDATA%/ai-steward/key_vault.json`（不同步目录）。
- 输出永远只掩码；没有 delete 命令（只 disable/rotate）；明文不进 argv/日志/报告/对话。
- 信任三档 low/high/official 落在条目上，数据红线见 `config/KEY_POLICY.md`；
  网关 lane 接 trust 字段后由调度引擎在派单前机械校验（backlog）。

### 3. 私人知识库 `scripts/steward_kb.py` + `knowledge/`

- 事实卡（`facts/patterns/preferences`）+ 事故复盘（`incidents`），markdown 可读、可随仓库走。
- 检索：标签加权 5 / 标题 3 / 正文 1；中文按 2-4 字滑窗，避免整句匹配不到。
- 调度引擎派单时自动取最相关 3 条注入 worker prompt 的 `【管家知识】`。
- 自我迭代协议：止血 → 加用例锁死 → 写事故卡 → 改手册 → 总闸全绿（见 `knowledge/README.md`）。

## 四、GitHub 调研与取舍（2026-11-18）

不重复造轮子，但也不为了用框架而用框架。结论：**借模式，不搬运行时**。

| 项目 | 值得借的 | 不搬的原因 |
|---|---|---|
| `OrchestratorInc/agent-orchestrator`（14.9k★） | Kanban 分区（Working/Needs you/In review/Ready）、主 agent 拆活、每个任务独立分支/worktree、reviewer 先看 PR、只在需要人时回来 | 它的执行器是自己的桌面/云 runtime；我们用 ZCode 会话，工具面不同 |
| `Yeachan-Heo/oh-my-claudecode`（40.9k★） | `plan → prd → exec → verify → fix` 流水线、单一 loop authority、`.omc/` 状态目录、rate-limit 自动续跑、friction report | 依赖 tmux/Claude CLI；Windows/ZCode 不适用 |
| `ruvnet/claude-flow` | queen-worker 层级、hooks 编排、持久记忆、成本追踪与预算告警 | 联邦/共识/向量库对单机单用户过重 |
| `crewAIInc/crewAI`（61k★） | 角色化任务卡、hierarchical process（经理派活） | Python 框架自带执行循环；我们的执行循环必须是 ZCode 会话 |
| `mem0ai/mem0` | 事实卡模型（ADD-only）、user/session/agent 三级作用域、多信号检索 | 依赖 LLM/embedding 服务；几十到几百条知识用 markdown+关键词足够，且可审计 |
| `musistudio/claude-code-router`（38k★） | 本地控制面、别名路由、失败换道、每请求日志 | 已用于网关层（`scripts/gateway.py`） |
| `stablyai/orca`（127k★） | 用自有订阅跑 coding agent 舰队、桌面/移动端监管 | 商业产品；作为“其它客户端积分”分支参考 |
| `omnigent-ai/omnigent` | meta-harness 统一编排 Claude Code/Codex/Cursor 等 | 分支 backlog，先把 ZCode 主线跑稳 |

**自建部分与理由**：

- 任务台不用 Huey/RQ/Celery：执行器是 ZCode 会话，不是 Python worker；消息队列再强也调不动
  `CreateSession/SendToSession`。用 JSON+锁+原子写即可，零依赖、可手改、可审计。
- 调度器不用 APScheduler/Temporal：唯一能唤醒 ZCode 会话的是 ZCode automation；Temporal 的
  durable workflow 很好，但接不进这个执行面。用 automation + `cron_health` 把停摆变成可判定。
- 记忆不用向量库：知识量小、要求可读可审；先 markdown+标签检索，条目过千再考虑 SQLite FTS5。
- 验收不用 LLM judge 做唯一门禁：先证据文件+sha+可执行用例；LLM reviewer 只做补充意见，
  避免“评审也幻觉”。

## 五、分支 backlog（先不动主线）

1. **其它客户端积分**：Workbody / Trae / Comate 等通过 MCP 或 CLI 适配成 worker，
   由管家统一派单，优先消耗已有积分；需要各自客户端支持非交互调用与结果回收。
2. **worktree 并行**：同仓库多写手时自动 `git worktree add`，任务结束合并/丢弃。
3. **reviewer agent**：worker 交付后先由独立 reviewer 会话按验收单复核，再进 done。
4. **看板 UI**：✅ 已落地（`scripts/board_view.py` → `reports/board.html`，只读、零依赖；
   2026-11-18 W-20261118-004 全自主闭环交付）。后续可加自动刷新。
5. **预算细化**：按项目/任务记账，超预算自动降档模型或排队到次日。

## 六、验收基线

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/run_all.py
```

14 道门：巡检边界 / 边界用例 / 网关自检 / 巡检取证 / 插件出厂 / 成本取证 / 台账边界 /
工具边界 / 定时健康 / 任务台 21 例 / 调度引擎 29 例 / 私人知识库 14 例 / 台账。
全绿才允许对外说“管家可用”。

## 七、2026-11-19 架构整改：证据冻结与确定性控制面

**病灶（实测）**：厂长会话 11h20m / 4,611 条消息 / 1.91 亿输入 token；管理类会话占当日
63% token、72% 轮次。三个自动化按 5/10/30 分钟节拍唤醒（**cron_expr 只是显示表达式，
真实节拍在 `schedule_rule`**），今天合计 140+ 次唤醒；厂长 70 次≈1.9 亿输入 token，
平均约 280 万/次——问题不是秒级风暴，而是每次唤醒都重载 4,600 条消息的上下文。
`state/` 836 MB，其中两份 405 MB 会话库快照常驻同步盘。证据每次验收后被追加，旧锚只比
raw，一轮能打回 4 张 done 卡。

**本次落地（代码已实测）**：

1. **证据冻结（immutable evidence）**：`steward_board.py done` 验收时把证据复制到
   `state/archive/evidence/<id>.<sha12>.md` 并置只读，登记 `evidence_frozen_path/_sha256/_at`。
   调度复核**冻结副本优先**；工作树后续被追加/换行尾不再触发 `reject-done`。
2. **唯一改锚入口**：`steward_board.py reattest --id --reason [--evidence --sha-mode --actor]`，
   旧哈希/原因/操作者写入 `reattest_history`；`freeze --all-done` 只做迁移，遇到工作树
   已漂移的卡会跳过并要求先 reattest（禁止把漂移洗成新锚）。
3. **DAG 视角**：`steward_cycle.py` digest 新增 `bottleneck`（传递下游数 Top5）与
   `critical_path`（最长未完成依赖链），渲染 `BOTTLENECK/CRITICAL` 行；另出
   `evidence_drift` 只读提示，不再作为打回依据。
4. **并发收敛**：`max_running` 15→6、`max_running_per_project` 12→2，新增
   `queue_empty_policy=report`（只报告，不自动开新战线）。
5. **调度者换代**：原厂长 30 分钟闹钟（`automation-4ed1c4c1`）暂停（enabled=0，可回滚）；
   新建「调度台·薄版」会话 `sess_00000000-0000-0000-0000-000000000002` 与
   `automation-7fa88a72-7e0b-4c3c-b24c-82f01cf091c1`（`*/30`，prompt 466 字，只跑
   `steward_cycle.py` 并处理 actions）。M-SAFETY 保持 5 分钟安全节拍；M-PLAN 10 分钟暂不动，
   观察一周再定。
6. **state 瘦身**：两份 405 MB 会话库快照移到
   `C:/Users/Administrator/factory-archive/ai-steward-state-backups/`（移动非删除，sha256 一致）；
   registry 备份保留最新 10 份，其余 82 份移到 `factory-archive/ai-steward-registry-backups/`。
   `state/` 836 MB→约 63 MB。

**新不变量**：

- done 卡的验收锚 = 冻结副本；工作树只是可继续编辑的工作文档。
- 任何改锚必须走 `reattest` 并留原因；禁止直接改 registry 或覆盖冻结文件。
- 调度器只消费台账与冻结证据，不读会话历史；厂长每轮只处理 actions，不重读 PLAN 全文。
- 并发上限是成本闸：先证明 6 路能跑满，再谈扩容。

**回滚**：代码备份在 `state/archive/backups/arch-20261119/`；配置改动为单行数值；
快照与 registry 备份均可原路移回。
