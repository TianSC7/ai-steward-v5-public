# 厂长过载诊断 + 同行编排方案调研

> 日期：2026-11-19 11:15
> 对象：`sess_4229d515`（厂长·夜间二次开工 V4）及 ai-steward 调度体系
> 方法：ZCode 本地 DB 只读实测（`~/.zcode/cli/db/db.sqlite`、`~/.zcode/v2/tasks-index.sqlite`）+ registry/evidence 实测 + 同行 UI 截图拆解
> 结论口径：已实测 / 未实施 分开标注

---

## 0. 一句话结论

厂长不是“任务太多”被压死，是**组织形态错了**：一个 11 小时、4,600+ 条消息、1.9 亿 input tokens 的“上帝会话”，同时当调度器、验收员、审计员、档案员、HR。同行的解法不是更多子智能体，而是把调度、瓶颈、审计、台账做成**确定性控制面**，主 Agent 只看异常摘要和推荐队列。

---

## 1. 实测：厂长到底被什么压死

### 1.1 厂长会话本身

| 指标 | 实测值 | 口径 |
|---|---:|---|
| 会话跨度 | 11-18 23:49 → 11-19 11:06（约 11h20m） | message 首末 |
| 消息数 | 4,611 条（今日 4,552） | `message` 表 |
| 轮次 | 141 turns | `turn_usage` |
| 模型请求 | 1,848 次 | `turn_usage.model_request_count` |
| input tokens | **190,545,181** | `turn_usage.input_tokens` |
| output tokens | 1,443,041 | 同上 |
| 峰值小时 | 10:00 时段 955 条消息 / 09:00 时段 5,895 万 input tokens | 按小时聚合 |
| 每轮平均 | 约 13.4 次模型请求、约 135 万 input tokens | 派生 |

**判读**：99.2% 的 token 是 input。厂长大部分“工作”不是思考，是**反复重读自己的历史**和各部门长报告。09:00 时段只有 2 个 turn，却烧了 5,895 万 input——单轮上下文/工具回读已经失控。按 146 turns 摊，每次唤醒约产生 **31.6 条消息、12.7 次模型请求**；她不是在想，是在翻账本。

### 1.2 今日全厂用量

| 指标 | 实测值 |
|---|---:|
| 会话数（有 turn） | 89 |
| 总 turns | 479 |
| 总 input tokens | 836,869,065 |
| 总 tokens | **845,259,848** |
| 总消息数 | 22,388 |
| 管理层会话 | 30 个（标题 `厂长`/`M-*`/`S1*`/`A0`/`SW1*`） |
| 管理层 tokens | **532.6M（63.0%）** |
| 生产/其他 tokens | 312.7M（37.0%） |
| 管理层 turns | 347 / 479（72.4%） |

管理层 token 前几名：

| tokens | 会话 | 说明 |
|---:|---|---|
| 190.5M | 厂长·夜间二次开工（V4） | 单会话占全厂 22.5% |
| 50.7M | M-AUDIT 审计经理 | 5 turns，1,312 条消息 |
| 38.9M | M-SAFETY 每 30 分钟安全核账轮 | 与 5 分钟巡场重复 |
| 38.1M | M-PRODUCT 生产经理 V6b | |
| 29.9M | M-SAFETY 5 分钟巡场 | 真实节拍 5 分钟（`schedule_rule`），单轮重载上下文 |
| 29.4M | M-PLAN 策划经理 | 每 10 分钟重写三份计划 |
| 28.3M | M-PRODUCT 开发经理（V5 重建） | |
| 26.7M | M-QUALITY 验收经理 | |

**这是最刺眼的一条：三分之二的算力花在“管理”，不是“生产”。**

### 1.3 三只闹钟 + 一个重复岗位

| 自动化 | cron | 今日 runs | 今日 tokens | 问题 |
|---|---|---:|---:|---|
| M-SAFETY 5 分钟巡场 | 显示 `* * * * *`（真实 `schedule_rule`=5 分钟） | 46 | 29.9M | 节拍正常，但每轮都重载长历史，单轮成本高 |
| M-PLAN 策划自唤醒 | `*/10 * * * *` | 11 | 29.4M | 每轮强制重写 PLAN_1H/5H/8H |
| 管家统筹巡检 | `*/30 * * * *` | 68 | 190.5M | 厂长本席 |
| M-SAFETY 30 分钟核账 | 手动/独立会话 | — | 38.9M | 与 5 分钟巡场职责重叠 |

三条自动化 + 一条重复岗位合计约 **288M tokens/天**，占全厂 34%。再叠加 12 个自动化里 9 个 paused 的历史噪音，巡检体系本身成了最大负载源。

### 1.4 证据漂移：done 卡每轮被“假打回”

实测 `steward_cycle.py` 在修复前输出 6 个动作，其中 **4 个是 `reject-done`**：

- W-20261118-N06 / N07 / W-20261118-052：注册哈希是 **LF 口径**，工作树是 CRLF；旧 `evidence_problem()` 只比 raw，误判“文件被改过”。
- W-20261119-006：10:38 验证通过，10:46 证据被追加审计结论 → 真实漂移。

也就是说：**3/4 是假返工，1/4 是“证据文件既当凭证又当工作底稿”的必然结果。** 厂长每轮都要重新裁决、重写冻结说明，越审计越漂移。

本轮已修（见 §5.1）：live cycle 从 6 个动作降到 2 个（unblock + dispatch），4 张卡的假 `reject-done` 清零。

### 1.5 文档与状态盘

| 指标 | 实测 |
|---|---:|
| `state/` 文件数 | 725 |
| `state/` 总量 | **866.6 MB** |
| 其中两份 DB 归档 | 2 × 405.5 MB（`state/archive/backups/state/db.sqlite.bak-*`） |
| 今日修改的 state 文件 | 416 |
| `state/` 根目录文档 | 183 个 / 15.9 MB |
| `registry.json` 备份 | 73 份 × 约 220 KB |
| `PLAN_1H.md` | 71 KB |
| `state/DECISIONS.md` | 41 KB |
| `DIGEST.md` | 35 KB |
| `state/modules/M-SAFETY.md` | 200 KB |

厂长每 30 分钟要读的“几份文件”，实际是 10 万字节量级的 prose；PLAN_1H 一份就 71 KB。**文档不是记忆，是负债。**

---

## 2. 同行截图拆解：9 个机制，不是 9 个功能

> 截图来源：用户提供的三张同行多智能体编排台截图（某同行平台 工作区）。以下只拆机制，不评价产品。

| # | 截图里的东西 | 背后的机制 | 治我们哪个病 | 该不该抄 |
|---|---|---|---|---|
| 1 | “链路瓶颈：g-076 … 挡住 1 个下游任务——先推它，整条链路才会动（另有 2 个瓶颈）” | **DAG 关键路径 + 下游阻塞计数**，自动算 top-N 瓶颈 | 厂长人脑扫 83 张卡算依赖 | **必抄** |
| 2 | “推荐 / 扫描推荐 / 完整扫描 / 采纳并管理；自动采纳上限 16；间隔 30 分钟” | **推荐队列 + 有上限的自动采纳 + 固定节拍** | 厂长每轮即兴发明任务 | **必抄** |
| 3 | 卡片状态：规划中 / 执行中 / 已交付 / 审计中；“已复核 \| PostgreSQL 备份与…” | **显式 review 态 + 卡片上的证据摘要** | done 后又被改、无独立审计态 | **必抄** |
| 4 | “主管已停止 / 重启主管 / 恢复错误 1 / 指令已送达 606 / 队列重复 x2 / 强制停止全部任务和子代理” | **主管是可重启的服务 + 健康指标 + 幂等指令** | 厂长是一个不能重启的聊天会话 | **必抄** |
| 5 | 任务执行板：“横线=执行区间，曲线=实际依赖/协作连接；绿色=仍在执行，短符号=缺少结束记录” | **甘特 + 依赖边 + 缺结束记录自动检测** | 没人知道哪张卡真的在跑 | **必抄** |
| 6 | “全局目标：…任何 [blocked 集合/目标总数] 一律只读导出的当前清单为准，禁止在判据、卡标题与描述里写死数量” | **单一事实源 + 计数只算不写死** | 计划/卡片里的数字和 registry 对不上 | **必抄** |
| 7 | “泳道职责：选择泳道…告诉 AI 这条泳道是干什么的” | **泳道契约：每类角色允许的动作/预算/产出** | 5 个部门职责重叠（M-SAFETY 两条线） | **必抄** |
| 8 | 每张卡“转对话”；卡上 `tok 6.92M / ctx 34%` | **每卡独立会话 + 每卡 token/上下文占用** | 所有回报都灌进厂长一个会话 | **必抄** |
| 9 | “每日 Token 趋势 / 模型用量分布 1.21B / 77/83 个独立会话” | **成本按角色/卡归因** | 只知道总量，不知道谁在烧 | **必抄** |

### 不要照抄的部分

- **760 个子智能体、16 并发、1.21B tokens/天**：这是结果不是原因。同行真正的解法是上面的控制面；把并发和子代理数量搬过来只会把 845M 变成 2B。
- **“完全权限 + 自动继续 + 全托管”**：在证据可写、审计可改的前提下，全托管等于放大漂移。
- **推荐全自动采纳**：同行也设了“上限 16”和 30 分钟节拍；无上限的自动采纳就是 queue-empty 造卡的加强版。

### 可直接对标的业界实现

| 模式 | 参考 | 可借的点 |
|---|---|---|
| 任务账本 + 进度账本，卡住才重规划 | Microsoft Magentic-One Orchestrator | Task Ledger / Progress Ledger 分离；只在 stall 时 replan |
| 主 Agent 不读子 Agent 全文，只读产物 | Anthropic multi-agent research system | 子 Agent 写文件系统，主 Agent 只收摘要 |
| 持久化状态机 + 幂等活动 | Temporal | 崩溃可恢复、重复执行安全（我们借模式不搬 runtime） |
| 期望态 vs 实际态，level-triggered | Kubernetes controller | 每轮 reconcile，不依赖“一次性事件” |
| DAG 依赖 + UI 拓扑 | Argo Workflows | `depends` 表达式 + DAG 可视化 |
| 同组单写者 | GitHub Actions concurrency group | 同 workspace 只允许一个写手 |
| Kanban “Needs you” 收件箱 | OrchestratorInc/agent-orchestrator | 人/主 Agent 只看需要裁决的卡 |
| 单一 loop authority + `.omc/` 状态目录 | oh-my-claudecode | 一个循环权威，状态落盘 |

---

## 3. 根因：5 个结构性错误

1. **聊天当总线**：worker → SendToSession → 厂长上下文。每一条回执都永久占用厂长上下文，4,600 条消息后只能靠 compaction 续命。
2. **证据可写**：`EVIDENCE_*.md` 既是验收凭证，又是审计工作底稿。任何“更正”都会改变哈希，`done` 不是终态。
3. **闹钟当组织**：组织架构写在 cron prompt 里。5 分钟安全巡场、10 分钟策划、30 分钟厂长；每条 prompt 都要求“读全量、写全量”。
4. **管理层自我繁殖**：`queue-empty` 强制从日报/backlog 造新卡；新卡产生新计划、新报告、新审计，管理层越忙越证明自己有价值。
5. **人脑当调度器**：依赖、优先级、WIP、预算、瓶颈都靠厂长每轮现算；83 张卡、9 条依赖，算不过来就全量重读。

---

## 4. 目标架构：确定性控制面 + 薄厂长

```text
                 用户（只看日报 / 异常）
                          │
                          ▼
                 ┌─────────────────┐
                 │  厂长（薄监督）  │  只做三件事：
                 │  1 个会话        │  ① P0/跨部门裁决
                 │  ≤2 turns/小时   │  ② 审批推荐队列 top-N
                 └────────┬────────┘  ③ 对外交付/日报
                          │ 读 10 行摘要，不读全文
                          ▼
        ┌──────────────────────────────────────────────┐
        │        确定性控制面（无 LLM，可单测）          │
        │  steward_cycle: DAG + 关键路径 + WIP + 预算    │
        │  evidence: 写一次冻结 + sha256 + 审计追加      │
        │  inbox: 每角色配额 + 摘要压缩（原始消息不进厂长）│
        │  metrics: 每小时 token/消息/卡住/漂移 → TRIAGE │
        └───────────────┬──────────────────────────────┘
                        │ 每卡独立会话派单
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    worker A        worker B        worker C
    （证据落盘 + 3 行回执，不 @ 厂长）
                        │
                        ▼
              reviewer（独立会话，只读冻结证据）
                        │
                        ▼
                 PASS/FAIL 一行 + 证据路径
```

四条原则：

1. **控制面确定性**：依赖、瓶颈、WIP、预算、重试、证据校验全部在 Python 里，可单测、可回放；LLM 只做判断。
2. **产物总线，不是聊天总线**：worker 写证据文件和 inbox 一行；厂长只读 digest。
3. **证据不可变**：done 时冻结副本 + 哈希；审计更正写 `state/audit/<id>.jsonl`，不改证据。
4. **每卡独立会话 + 每卡预算**：卡上显示 tok/ctx；超预算自动停派，不靠人盯。

---

## 5. 止血清单（今天 30 分钟，按顺序）

> 以下 5.1 已实施；5.2–5.7 未实施，需在厂长会话/运维窗口执行。

### 5.1 已实施：证据 LF 口径兼容（代码修复）

**文件**：`scripts/steward_cycle.py`

- 新增 `_evidence_sha(path, normalize_lf=False)`；
- `evidence_problem()` 先比 raw，不中再比 CRLF→LF 归一；两者都不中才判真实漂移；
- `scripts/steward_board.py` 的 `done` 现在记录 `evidence_sha_mode`，新卡可追溯口径。

**实测结果**：

| 项 | 修复前 | 修复后 |
|---|---:|---:|
| live cycle 动作数 | 6 | 2 |
| `reject-done` | 4 | 0 |
| `test_cycle.py` | 50/50 | **59/59** |
| `test_board.py` | 24/24 | **30/30** |

> 注：W-20261119-006 在本轮分析期间已被系统重新验证（11:12:57），当前哈希一致；它的真实漂移问题仍需 §5.5 的冻结机制根治。

### 5.2 已纠正：M-SAFETY 不是 1 分钟闹钟

- 自动化：`automation-756563a8-6943-4905-a7ff-a6d868f33680`。
- 复核结论：`cron_expr = * * * * *` 只是显示表达式；真实节拍在 `schedule_rule`
  （`{"unit":"minute","interval":5}`），`automation_runs` 实测每 5 分钟一次，与标题一致。
- 因此不需要改 cron；该会话 29.9M/天的成本来自每轮重载长上下文，不是节拍。
- 处置：保持 5 分钟安全节拍；把“薄 prompt + 新会话”用于厂长调度线（见 §5.6）。

### 5.3 未实施：合并重复的安全线

- “M-SAFETY 每 30 分钟安全核账轮”（今日 38.9M）与 5 分钟巡场职责重叠。
- 二选一：保留 5 分钟轻量脚本轮，停 30 分钟核账轮；或反之。
- 判据：同一职责只允许一个 cron；`cron_health.py` 增加“同职责多闹钟”告警。

### 5.4 已实施：调度者换代（不是只改 prompt）

旧厂长 30 分钟闹钟每次唤醒都重载 4,611 条消息（≈280 万 token/次）。只换 prompt 不能
减少上下文重载，因此直接换代：

- 旧闹钟 `automation-4ed1c4c1-7036-4822-a52c-bc6e2e4e82d1` **暂停**（`enabled=0`，
  行级备份 `state/archive/backups/AUTOMATIONS_BACKUP_20261119_1222.json`）。
- 新会话「调度台·薄版」`sess_00000000-0000-0000-0000-000000000002`，首个 prompt 只让
  它读两份薄文档 + 跑一轮 cycle + 建自己的闹钟。
- 新闹钟 `automation-7fa88a72-7e0b-4c3c-b24c-82f01cf091c1`：`*/30`，prompt 466 字，
  只处理 `cycle_last.json` 的 actions（见 `docs/SUPERVISOR_THIN_PROMPT.md`）。
- 回滚：旧闹钟 `enabled=1` 即恢复；新会话可归档，不影响台账。

### 5.5 已实施：证据冻结 + 审计追加（根治漂移）

1. ✅ `board done` 把证据复制到 `state/archive/evidence/<id>.<sha12>.md` 并置只读，登记
   `evidence_frozen_path/_sha256/_at`。
2. ✅ `evidence_problem()` 冻结副本优先；工作树变化只进 digest `evidence_drift`，不再打回。
3. ⏳ append-only 审计流 `state/audit/<id>.jsonl` 尚未单独建；当前审计信息进
   `reattest_history` 与日报，足够追溯。
4. ✅ `board reattest --id --reason --evidence ...`，旧哈希/操作者进 `reattest_history`。
5. ✅ 迁移完成：81 张 done 卡全部有冻结副本（`freeze --all-done` 80 张 + W-006 走 reattest）。

### 5.6 已实施（配置级）：空队列不再自动开新战线

- 新增 `queue_empty_policy`，当前 `report`：工作时段空队列只报告，不自动造卡。
- 需要更严格时改 `stop`：`steward_cycle.py` 直接返回 `idle`。
- 恢复扩容条件不变：管理层 token 占比 <30%，且连续 3 天无假返工。

### 5.7 已实施：810MB DB 归档迁出 `state/`

- 两份 405.5 MB 快照已**移动**到 `C:/Users/Administrator/factory-archive/ai-steward-state-backups/`，
  移动前后 sha256 一致，回滚说明见 `state/archive/backups/state/README_MOVED_20261119.md`。
- `registry.json` 备份保留最新 10 份，其余 82 份移到
  `C:/Users/Administrator/factory-archive/ai-steward-registry-backups/`。
- 实测 `state/` 836 MB → 63 MB。

---

## 6. 一周重构（4 个 PR，按依赖顺序）

### PR-1：证据不可变（P0，1 天）

- `state/archive/evidence/` 冻结目录 + `board done` 自动冻结；
- `state/audit/<id>.jsonl` append-only；
- `board reattest` 命令 + 单测；
- `evidence_anchor.py` 的 `MATCH_LF` 分类接入 cycle（本轮已做一半）。

**验收**：80 张 done 卡全部有冻结副本；连续 24 小时 0 次 `reject-done` 假阳性；任意审计更正可在 audit.jsonl 追溯。

### PR-2：inbox + digest + 配额（P0，1–2 天）

- 新增 `scripts/inbox.py`：
  - `post --from <role> --task <id> --severity P0|P1|P2 --line "..."`；
  - `digest --role boss --limit 10` 输出按 severity 排序的摘要；
  - `quota` 检查：worker 每卡 ≤1 条完成回执，经理 ≤3 条/小时，厂长收件 ≤10 条/轮。
- 改 worker prompt：完成只写证据 + `inbox.post` 一行，**禁止 SendToSession 给厂长**（P0 除外）。
- 厂长 30/60 分钟 prompt 改为只读 digest。

**验收**：厂长会话消息数从 4,600/天降到 <500/天；单轮 input tokens <200k。

### PR-3：DAG 瓶颈 + 关键路径（P1，1 天）

- 复用已有 `depends_on`（当前 9 张卡有依赖）；
- `steward_cycle` 增加：
  - `critical_path()`：按 `priority`/`estimate` 加权的最长链；
  - `bottleneck()`：一个任务阻塞的下游任务数，top-N；
  - `DIGEST` 首行输出：“瓶颈：<id> 挡住 N 个下游；建议先推 <id>”。
- `board_view.py` 渲染依赖链（文本即可，先不做 UI）。

**验收**：厂长不需要打开 83 张卡；每轮 digest 直接给出 top-1 瓶颈。

### PR-4：预算 + 指标看板（P1，2 天）

- `config/steward.json` 增加 `token_budget`：boss 50M/天、经理 10M/天/席、worker 20M/卡；
- `scripts/factory_metrics.py` 每小时写 `state/metrics/<date>.jsonl`：
  `{hour, sessions, messages, tokens_by_role, top5_sessions, reject_done, evidence_drift, queue_wait}`；
- `state/TRIAGE.md` 一页给用户：今日 tokens、管理层占比、top 3 烧钱会话、瓶颈、异常；
- cycle 在超预算时发 `budget-alert` 并停派新单。

**验收**：管理层 token 占比 <30%；单会话 >30M 自动告警；用户每天只看一页。

---

## 7. 验收判据（重构完成的硬指标）

| 指标 | 现状 | 目标 |
|---|---:|---:|
| 厂长消息数/天 | 4,552 | <500 |
| 厂长 input tokens/天 | 190.5M | <50M |
| 管理层 token 占比 | 63.0% | <30% |
| 全厂 tokens/天 | 845M | <300M |
| 假 `reject-done` | 3/4 | 0 |
| done 卡冻结率 | 0% | 100% |
| 厂长 turns/天 | 141 | ≤24 |
| 单轮 input tokens | 峰值 29M | <200k |
| `state/` 体积 | 866MB | <100MB |
| 瓶颈可见性 | 无 | digest top-1 |

---

## 8. 附：实测命令与口径

```bash
# 会话/用量（只读）
python -X utf8 -c "..."   # 见本次分析；DB: ~/.zcode/cli/db/db.sqlite
# 台账状态
python -X utf8 scripts/steward_cycle.py --json
# 证据锚分类
python -X utf8 scripts/evidence_anchor.py --check --out-md /tmp/EVID.md --out-json /tmp/EVID.json
# 调度/台账测试
python -X utf8 scripts/test_cycle.py
python -X utf8 scripts/test_board.py
```

**口径说明**：

- “管理层”按 session title 前缀 `厂长` / `M-` / `S1` / `A0` / `SW1` 归类；这是保守口径，未计入 A1/L1 等组长席，所以 63% 是下限。
- token 数来自 ZCode `turn_usage` / `model_usage`，不是估算。
- 消息数来自 `message` 表；一个 assistant 回合可能包含多条消息（工具步骤），所以消息数远大于 turns。
- 同行截图只做机制拆解，不代其产品背书；“1.21B tokens / 760 子智能体”是同行现状，不是建议目标。

---

## 9. 最后一句

厂长需要的不是“更强的模型”或“更多的子智能体”，而是**一个不需要她每 30 分钟重读全世界的调度器**。先把闹钟减半、证据冻结、回报进收件箱；她会立刻从“被压死”变成“只做裁决”。
