# AI 大管家调度台 v3 运维手册（RUNBOOK）

> 适用对象：值守调度台的会话（人或 AI）。
> 唯一目的：把「每 20 分钟一轮」的日常动作和故障处置写成照做即可的步骤，不靠记忆、不靠临场发挥。
> 事实来源：`state/dispatcher_prompt.md`（作业手册）、`scripts/cron_health.py`、`scripts/steward_cycle.py`、`scripts/steward_board.py`、`config/steward.json` 的实际行为，以及 2026-11-17/18 的事故卡。
> 维护规则：本文件只描述操作，不改任何在役脚本与配置；引擎行为变了先改代码和用例，再回来改本手册。

---

## 0. 路径与口径

所有命令都用绝对路径，不依赖 cwd（worker 和调度台的 cwd 都可能是 newcad 壳工作区）。

| 名称 | 绝对路径 |
|---|---|
| 插件根（管家母本） | `C:/Users/Administrator/Documents/mate/plugins/ai-steward` |
| 台账（唯一任务事实源） | `C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/registry.json` |
| 调度配置 | `C:/Users/Administrator/Documents/mate/plugins/ai-steward/config/steward.json` |
| 每轮作业手册 | `C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/dispatcher_prompt.md` |
| 证据目录 | `C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/evidence/` |
| 日报 | `C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/DAILY_REPORT_YYYYMMDD.md` |
| 看板 | `C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/board.html` |
| 知识库 | `C:/Users/Administrator/Documents/mate/plugins/ai-steward/knowledge/` |

占位符约定：

- `<id>`：任务卡 ID，形如 `W-20261118-008`。
- `<sid>`：会话 ID，形如 `sess_5ec148cb-...`。
- `<commandId>`：`CreateSession` 返回的 command 标识，用于 `claim` 留痕。
- `<项目对象>`：`ListProjects` 返回的项目对象，原样回传给 `CreateSession`，不要自己拼。

时间口径：台账与 cron 都用本机本地时区（+08:00）；`cron_health` 内部用 epoch 毫秒；同一轮里不要混用 UTC。

单轮预算：**不超过 2 分钟**。长活一律 `CreateSession` 派 worker，调度台只做判定和回写。

---

## 1. 每轮开场命令（30 秒）

### 1.1 两条固定命令

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/cron_health.py --from-registry
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_cycle.py --json
```

第一条回答「巡检还会不会再来」，第二条回答「这一轮要做什么」。顺序不能反：cron 停摆时 `steward_cycle` 仍会输出看似正常的结果，但那些动作永远不会被执行。

需要连成本一起判定时，把当日成本显式喂给调度引擎：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/budget_report.py --json
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_cycle.py --json --today-cost 73.98
```

不传 `--today-cost` 时预算判定不生效（`daily_budget_cny=0` 也表示不设上限）。

### 1.2 判读 cron_health

输出形如：

```text
CRON automation-28bd0a92-80cf-4543-8b0a-8b3235c2cedc OK next=2026-11-18 12:13:18 last=2026-11-18 11:53:23 run=39 lag=-18.2m target=sess_5ec148cb-...
HEALTH OK (1 checked)
```

退出码：`0` 全部 OK；`3` 有非 OK；`2` 库/参数问题。逐状态处置见 §3.1。

### 1.3 判读 steward_cycle

`digest` 先看五个数：`active`、`queued`、`slots`、`done_today`、`stale_active`。`actions` 是本轮全部工作，**按数组顺序处理，处理完一个再下一个**。

动作类型共 13 种：`dispatch`、`verify`、`check`、`unblock`、`replace`、`split`、`reject-done`、`replan`、`report`、`budget`、`progress-alert`、`queue-empty`、`idle`。其中前五类是高频主处置，见 §2；其余见 §2.6。

### 1.4 开场三问（防止把停摆当清闲）

1. `cron_health` 是否 `HEALTH OK`？非 OK 先修调度台本身（§3.1）。
2. `actions` 是否为空？为空不等于没事，先确认 `active/queued` 计数和 `last_progress_min`。
3. 是否处于工作时段（默认 08:00-20:00）且出现 `queue-empty`？出现就必须落卡，不能回「无在办任务」。

### 1.5 轮末收尾

- 有状态变化才写台账；台账只经 `steward_board.py` 写。
- 本轮处置写一行到 `reports/NIGHT_LOG.md` 或当日 `DAILY_REPORT`，不刷屏。
- 有任务转 `done` 就跑看板刷新：
  `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/board_view.py`
- 每轮结束前确认 `state/registry.json` 是合法 JSON（脚本原子写正常时不会坏）。

---

## 2. 五类主 action 的处置表

统一原则：先判活（会话还在不在、有没有新证据），再判超时；能验收就验收，不能验收就升级，**不把 worker 的「需要用户确认」当结论**。

### 2.1 dispatch（派新单）

| 步 | 动作 | 命令/工具 |
|---|---|---|
| 1 | 按 `action.zcode_project` 在 `ListProjects` 里找标题匹配的项目对象 | `ListProjects` |
| 2 | 找不到项目：判失败并写日报，不硬派 | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py fail --id <id> --reason "项目不存在"` |
| 3 | 写码任务：核对/补建隔离工作区（`action.worktree.path` 已给出就不用重跑） | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/worker_worktree.py ensure --id <id> --repo <action.worktree.repo> --json`（幂等；退出码 3 = 不是 git 仓，照常派） |
| 4 | 建会话，模型用 `action.model`（卡上没写才按手册 §0b 默认） | `CreateSession(project=<项目对象>, model=<action.model>, prompt=<action.prompt>)` |
| 5 | 立刻给会话开全权 | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/set_permission.py --session <sid>` |
| 6 | 确保项目级权限（子智能体/新窗口只继承项目级；有隔离工作区时补 `--workspace <worktree>`） | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/set_permission.py --workspace <项目 workspacePath> --allow-tool WebSearch --allow-tool WebFetch` |
| 7 | 回填台账（有隔离工作区时带三个 worktree 字段） | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py claim --id <id> --session <sid> --command <commandId> --worktree <path> --worktree-branch <branch> --worktree-repo <repo>` |
| 8 | 最多回一行 | `派单 <id> -> <sid>`，不要贴 prompt |

判定要点：

- `CreateSession` 的 accepted/queued 只表示已受理，不表示完成。
- 第 5 步是幂等的：已设置会输出「已是/已有通配」。缺这一步的典型表现是 worker 的子智能体弹授权、任务卡死等人工点（INC-20261118-004）。
- `claim` 只接受 `queued`；返回 `BAD-STATE` 说明状态已被别处改过，先 `get` 查清再动。
- 同一项目默认只允许 1 个写手（`max_running_per_project=1`）；要并行改代码必须各自 worktree（`dispatch.worktree.enabled=true` 已自动拆）。
- 派单后不要在同一轮反复重跑 `steward_cycle` 仍沿用旧 prompt；确需重取就在同一轮重跑一次，以最新输出为准。

### 2.2 verify（有证据，核验收尾）

| 步 | 动作 | 命令/工具 |
|---|---|---|
| 1 | 读摘要，确认会话是否还在跑 | `ReadSession(sessionId=<sid>, view="summary")` |
| 2 | 仍 `running` 且只是中间产物：登记进展，本轮不打扰 | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py progress --id <id> --note "证据已现，仍在跑"` |
| 3 | `idle`/`needs_attention`：读最后几条消息，找三段式回执（结论/证据/未决） | `ReadSession(sessionId=<sid>, view="messages")` |
| 4 | 逐条对照任务卡 `acceptance` | 任务卡 `python -X utf8 .../steward_board.py get --id <id> --json` |
| 5 | 满足：先收隔离工作区（有 `worktree` 时） | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/worker_worktree.py finish --id <id> --json`；非 0 不算 done（dirty/冲突/主仓不在基准分支按 hint 处理） |
| 6 | 再收尾并登记证据 | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py done --id <id> --evidence C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/evidence/<id>.md --summary "<一句结论>"` |
| 7 | 不满足：按 §3.3 打回 | 见 §3.3 |

判定要点：

- 只有非空证据文件才算完成；`done` 会自动登记 `evidence_path`/`evidence_sha256`，并把证据复制到只读冻结区 `state/archive/evidence/`。
- 冻结副本是验收锚；工作树文件之后可以继续追加，不会因此被 `reject-done`。
- 证据文件不存在时，引擎会按约定路径 `reports/evidence/<id>.md` 兜底查找，所以证据必须写在这个位置。
- 验收看行为与结果，不看「源码编译通过」或「我打算怎么做」。
- 收尾后写一行日报：`verify <id> -> done`。

### 2.3 check（在办超时，按 level 升级）

先 `ReadSession(view="summary")` 取 `executionId`（只在运行中出现），再按 `action.level` 执行：

| level | 判定 | 动作 | 命令 |
|---|---|---|---|
| `nudge` | pulls=0，先提醒不打断 | 发提醒话术后登记拉取 | `SendToSession` + `python -X utf8 .../steward_board.py pull --id <id>` |
| `steer` | pulls=1，已提醒仍无产出 | 中断并三段式纠偏 | `InterruptSession(sessionId=<sid>, expectedExecutionId=<executionId>)` → `SendToSession` → `python -X utf8 .../steward_board.py steer --id <id> --reason "<哪里跑偏>" --interrupt applied --ack "<回执>" --command <commandId>` → `pull --id <id>` |
| `replace` | pulls=2，同一会话救不回来 | 打回队列换新会话 | `python -X utf8 .../steward_board.py state --id <id> --state queued --force --note "连续无进展，换新会话"` → `pull --id <id>` |
| `split` | pulls>=3（`max_pulls`），到上限 | 拆 2-3 个可独立验收的子任务 | `python -X utf8 .../steward_board.py add ... --depends-on <父或兄弟>`；父任务 `state --id <id> --state abandoned --force`；`pull --id <id>` |

nudge 话术（不中断）：

```text
【管家巡检 HH:MM】你已 N 分钟没有产出可复核证据。
不要停下来等用户：按任务卡继续推进；卡住就换一种做法，并把已尝试方案写进“未决”。
验收标准：<acceptance>
```

steer 三段式（必须同时说清停止项与替代做法）：

```text
【立即纠偏】<哪里跑偏/哪条红线>
立即停止：<不要继续做的事>
改为：<正确做法>；验收：<可机械判定标准>
```

判定要点：

- **判活优先于判超时**：summary 是 `idle` 且验收已满足 → 直接 `done`；`idle` 但没证据 → 按 `nudge`；`running` 且确实有新证据 → `progress`，不打扰。
- `InterruptSession` 必须带当前 `executionId`；拿不到就说明会话没在执行，改用 `SendToSession` 投递。
- 纠偏必须由另一个会话发起，目标会话自己抢不了自己的当前轮（K-20261118-002）。
- 每级动作最后都要 `pull`，否则下一轮还会在同一级重复。

### 2.4 unblock（blocked 不是终点）

按 `action.checklist` 逐条排查，先读 `ReadSession(view="messages")` 最后几条找真实卡点：

| 卡点类型 | 处置 |
|---|---|
| 权限 | 查 `state/autoallow.on` 是否存在；重跑 `set_permission.py --session <sid>` 与项目级 `--workspace ... --allow-tool WebSearch --allow-tool WebFetch` |
| 缺信息 | 管家按最保守假设代拍，把决定写进 `registry.decisions`，再 `SendToSession` 告诉 worker |
| 工具失败/路径错 | 换命令、换路径、换等价工具；同一手段失败 2 次必须换 |
| 验收不清 | 把 `acceptance` 拆成可执行断言再发 |
| 会话失联 | 按 §3.2 的 replace 流程处理 |

解卡写回：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py state --id <id> --state running --note "已解卡：<原因>"
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py progress --id <id> --note "续跑指令已发"
```

连续 2 轮解不动 → `replace`（新会话）；仍不行 → `split`。

**绝对禁止**：worker 说「需要用户确认」就把它当结论停下。只有删除用户文件、外发消息、上线部署三类允许升级给用户，其余一律代拍并继续。

### 2.5 replace（换会话/换路线）

| 步 | 动作 | 命令/工具 |
|---|---|---|
| 1 | 旧会话打回队列（保留次数统计） | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py state --id <id> --state queued --force --note "连续无进展/失联，换新会话"` |
| 2 | 能发消息就通知旧会话停止 | `SendToSession(sessionId=<旧sid>, prompt="任务已转交，停止当前工作。")` |
| 3 | 登记拉取，下一轮自动按 dispatch 重派 | `python -X utf8 .../steward_board.py pull --id <id>` |
| 4 | 下一轮 dispatch 使用新会话，prompt 已带【上次失败】 | `CreateSession` + `set_permission.py` + `claim` |

判定要点：

- `attempts` 到 `max_attempts`（默认 3）时 `claim` 会拒绝（`MAX-ATTEMPTS`），此时必须 `split` 或人工裁决，不能继续换会话。
- 重派前先查交付物是否已落盘（`ls -l`、sha256、mtime）。已有部分成果时让新会话在原有产物上补齐，不要推倒重写。

### 2.6 其余 action 处置附表

| action | 含义 | 处置 |
|---|---|---|
| `split` | 到达重试上限，必须拆任务/换路线 | 用 `steward_board.py add --title ... --acceptance ... --depends-on ...` 建 2-3 个子任务；父任务 `state --state abandoned --force`；子任务下一轮自动派 |
| `reject-done` | 任务已 done 但证据缺失/为空/哈希不符 | 按 §3.3/§3.4 打回或重登记，不直接改台账 |
| `replan` | 依赖不存在或已 failed/abandoned | 要么补建缺失的依赖卡（`add`），要么把当前卡 `state --state abandoned --force` 后用新 `--depends-on` 重建；`steward_board.py` 没有 edit 子命令 |
| `report` | 失败/阻塞需要进日报 | 追加一行到当日 `DAILY_REPORT`，然后 `python -X utf8 .../steward_board.py reported --id <id>`；不给用户发消息 |
| `budget` | 今日成本到达上限 | 停止派新单；在办任务继续盯；写日报；等用户调预算或次日重置 |
| `progress-alert` | 有活但长时间（默认 2 小时）零进展 | 强制换路线：先 nudge，无回应转 steer；仍不动就 split，不要继续等 |
| `queue-empty` | 工作时段队列为空 | 从日报「明日主线」、`docs/ARCHITECTURE_V2.md` backlog 或 `BACKLOG_POOL.md` 挑 1 项不需要用户授权的活落卡；一项都挑不出才写一行「队列空，无 backlog」；**不得当 idle 放过** |
| `idle` | 非工作时段且无在办/排队 | 只回一行 `巡检 HH:MM：无在办任务`，结束 |
| `worktree-cleanup` | 任务已 done/abandoned，但隔离工作区还在 | `worker_worktree.py finish --id <id>`；分支/目录已不存在则 `worker_worktree.py prune`；冲突解不掉就 `remove --force`（保留分支等人工）并写日报 |

**隔离工作区（2026-11-18 W-011）判定要点**：

- 工作区根 `C:/Users/Administrator/worktrees/ai-steward/`，分支 `wt/<task-id>`，台账 `state/worktrees.json`（与 `registry.json` 分开，只经 `worker_worktree.py` 写）。
- 不是 git 仓的任务 `ensure` 返回 `skipped=not-a-git-repo`，照常派单（无隔离）；建失败返回 `error=...`，仍可派但必须按单写手模式跑并在日报记一行。
- `merge` 只在主仓 HEAD 等于基准分支时执行；`worktree-dirty` 说明 worker 没提交，先打回补 commit，不要 `--allow-dirty` 糊过去。
- `remove` 只解 `node_modules` 联接再删目录，绝不递归删主仓实体；删不掉必须如实报失败，不许假报成功。

---

## 3. 故障处置

### 3.1 cron 停摆（STALE / DISABLED / ERROR / NO-NEXT / NOTFOUND / TARGET-MISMATCH）

**先判定，再动手。** 运行：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/cron_health.py --from-registry --json
```

| 状态 | 含义 | 处置 |
|---|---|---|
| `STALE` | `next_run_at` 已过期超过 grace（默认 5 分钟） | 先看 `last_error`/`dispatch_status`；无错误则重建 automation（见下） |
| `DISABLED` | `enabled=0` 或 `lifecycle_status != active` | 在 ZCode「自动化」页恢复；不可用时重建；最后手段才是改库 |
| `ERROR` | `last_error` 非空或 dispatch 失败 | 按错误文本修目标会话/提示词；修好后重建 |
| `NO-NEXT` | `next_run_at` 为空 | 该 automation 不会再有下一跑，必须重建 |
| `NOTFOUND` | 库里没有这个 `automation_id` | `registry.dispatcher.cron_id` 过期，重建并回写 |
| `TARGET-MISMATCH` | automation 的目标会话与 `registry.dispatcher.sessionId` 不一致 | 以 registry 为准核对实际调度台会话；要么重建到新会话，要么回写正确的 sessionId |

标准重建流程（优先用工具，不直接改库）：

1. `CronList` 确认现有 automation 的 id、标题、目标会话；旧的不删也可以，避免历史丢失。
2. 在**当前在用的调度台会话**里创建新 automation：

```text
CronCreate(
  title="每20分钟管家巡检",
  prompt="读取 C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/dispatcher_prompt.md 并按其第 0-7 节完成本轮巡检；本轮不创建、不修改任何自动化。",
  intervalUnit="minute",
  interval=20,
  cron="*/20 * * * *",
  recurring=true
)
```

   说明：间隔按**创建锚点**走，不按整点对齐（K-20261118-003）。`intervalUnit/interval` 是真实排程，`cron` 只是合法展示表达式；创建后必须 `CronList` 核对实际 `scheduleRule` 与 `nextRunAt`。若工具拒绝该组合，改用 `cron="* * * * *"` 加同样的 `intervalUnit/interval`。
3. 把新的 `sessionId` 与 `cron_id` 回写 `registry.dispatcher`。**禁止手写 JSON**，先备份再 python roundtrip 原子替换：

```bash
cp C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/registry.json \
   C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/registry.json.bak-$(date +%Y%m%d-%H%M%S)
python -X utf8 - <<'PY'
import json, os, tempfile
p = "C:/Users/Administrator/Documents/mate/plugins/ai-steward/state/registry.json"
d = json.load(open(p, encoding="utf-8"))
d.setdefault("dispatcher", {})
d["dispatcher"]["sessionId"] = "sess_新的调度台会话"
d["dispatcher"]["cron_id"] = "automation-新的自动化ID"
d["dispatcher"]["updated_at"] = "2026-11-18T12:00:00+08:00"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
os.replace(tmp, p)
print("dispatcher updated")
PY
```

4. 复验：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/cron_health.py --from-registry
```

   必须回到 `HEALTH OK`，且 `target=` 等于新的 `dispatcher.sessionId`。

5. 修不好时：只写日报说明「调度台停摆，已重建/待重建」，不要发明任务、不要假装巡检过。

最后手段（改 ZCode 库，需用户同意）：库在 `~/.zcode/v2/tasks-index.sqlite`。先关闭 ZCode，复制一份带时间戳的备份，再只改 `automations` 表的 `enabled`、`lifecycle_status`、`next_run_at` 三列；改完立刻跑 `cron_health` 复验。不得改表结构、不得删历史行。

### 3.2 会话 App control failed（会话失联）

现象：`ReadSession(<sid>, view="summary")` 直接返回 `App control failed`，不是 `sessionNotFound`。同时台账里 `sessionId` 仍是旧值，调度引擎可能已把任务打回 `queued` 并重新给出 `dispatch`。

判定：这是**会话控制通道失联**，不是任务失败，也不是「等一会儿就好」。不要在失联会话上 `OpenSession` 或反复重发。

处置（按 replace 重派）：

1. 确认台账状态；不是 `queued` 就先打回：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py get --id <id> --json
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py state --id <id> --state queued --force --note "原会话 <旧sid> 失联(App control failed)，按 replace 换新会话重派"
```

2. `ListProjects` 找项目对象，`CreateSession` 建新会话。
3. 立刻 `set_permission.py --session <新sid>`；必要时补项目级 `--workspace ... --allow-tool WebSearch --allow-tool WebFetch`。
4. 回填台账并写明原因：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py claim --id <id> --session <新sid> --command <新commandId> --note "原会话 <旧sid> 失联(App control failed)，按 replace 换新会话重派"
```

5. 输出应显示 `att=2/3`（次数递增）。`att` 到上限仍失败，转 `split`。

补充：旧会话不要删除，也不要再向它发指令；保留现场便于复盘。

### 3.3 证据不合格（reject-done）

触发：`steward_cycle` 报 `reject-done`，或 verify 时发现证据空泛、缺命令/输出/结论。

处置：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py state --id <id> --state running --force --note "证据不合格：<缺什么>"
```

然后 `SendToSession` 三段式要求补齐：

```text
【证据不合格】<缺命令/缺关键输出/结论与验收不对应>
立即停止：不要再贴计划或复述任务。
改为：把「执行命令、关键输出、结论」三部分写进 C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/evidence/<id>.md，并逐条对应 acceptance。
验收：文件非空，命令可复跑，输出能支持结论。
```

最后：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py pull --id <id>
```

补齐后重新 `verify`，满足再 `done`。不要为了让队列变干净而放行空证据。

### 3.4 哈希漂移（done 后证据被改）

触发：任务已是 `done`，`steward_cycle` 仍报「冻结证据缺失/为空/哈希不匹配」。
（2026-11-19 起：工作树证据被追加**不再**触发打回，`evidence_drift` 只作提示。）

处置顺序：

1. 先看卡上有没有 `evidence_frozen_path`。有 → 验收锚是冻结副本；工作树怎么改都不影响，最多在日报记一行 drift。
2. 冻结副本丢失或损坏 → 找到验收时的原件（备份/其他副本），核对内容后再重新冻结：
   ```bash
   python -X utf8 .../steward_board.py reattest --id <id> --evidence <验收原件> --reason "冻结副本丢失，按验收原件重建" --actor steward
   ```
3. 验收后确有新版本证据需要取代旧锚（补测、返工复验）→ 只走 `reattest`，必须写原因；旧哈希/操作者自动进 `reattest_history`：
   ```bash
   python -X utf8 .../steward_board.py reattest --id <id> --reason "<为什么改锚>" --actor <谁>
   ```
4. 无法解释的内容替换、结论被改、关键输出消失 → 视为证据不可信，打回重做：
   ```bash
   python -X utf8 .../steward_board.py state --id <id> --state running --force --note "证据不可信，要求重做并重新取证"
   ```

**禁止**：直接编辑 `state/registry.json` 改 `evidence_sha256`；覆盖冻结副本；用 `done --force` 重登记绕过 reattest。

历史卡迁移：`steward_board.py freeze --all-done`（幂等；工作树已漂移的卡会被跳过，要求先 reattest）。

### 3.5 queue-empty（工作时段空队列）

这不是 idle。处置顺序：

1. 读当日 `C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/DAILY_REPORT_YYYYMMDD.md` 的「明日主线」。
2. 读 `C:/Users/Administrator/Documents/mate/plugins/ai-steward/docs/ARCHITECTURE_V2.md` 的 backlog；如有 `BACKLOG_POOL.md` 一并读。
3. 只挑 **1 项不需要用户授权** 的活落卡：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py add \
  --title "<标题>" \
  --goal "<目标>" \
  --acceptance "<可机械判定的验收>" \
  --project ai-steward \
  --tier MEDIUM \
  --priority 2 \
  --source "queue-empty 自主落卡"
```

4. 本轮不派单，下一轮 `dispatch` 自动接走。
5. 一项都挑不出，写一行日报「队列空，无 backlog」再结束。

可自主：新增独立脚本/用例/文档/调研报告（只新增文件）。
不可自主：CAD 开关实测、部署/合并/删除用户文件、修改在役引擎脚本（`steward_board/cycle/kb`、`run_all`、hooks、配置）。最后一类先落卡但不派，等管家接线或拆 worktree。

### 3.6 其他高频故障速查

| 现象 | 先看 | 处置 |
|---|---|---|
| worker 子智能体弹权限 | 项目级 `mode=yolo` 是否缺 | 补 `set_permission.py --workspace ... --allow-tool WebSearch --allow-tool WebFetch`，必要时重派 |
| 会话 `needs_attention` | 最后一条消息/工具名 | 多为权限或「变更前确认」；修钩子或授权一次，再发续跑指令 |
| worker 长时间不回话 | `ReadSession(summary)` 是否 running | 有 `executionId` 就中断+三段式；idle 则查验收是否已满足 |
| 网关 502/超时 | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/gateway_ctl.py status` | `start`/`restart`；全 lane 失败才考虑换 provider |
| 命令/技能不出现 | 是否旧会话 | 命令与钩子只在会话启动时加载，新开会话 |
| 自动化不触发但 DB 显示 active | `cron_health --json` 的 `lag_min` | 按 §3.1 重建，不要等 |

---

## 4. 证据与台账规范

### 4.1 证据文件（worker 交付物）

路径固定：`C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/evidence/<id>.md`。

必须包含三要素：

1. **执行命令**：可原样复跑的完整命令（绝对路径）。
2. **关键输出**：真实终端输出摘要，含退出码；不要只写「测试通过」。
3. **结论**：逐条对应任务卡 `acceptance` 的判定。

附加要求：

- 文件非空；`done` 会拒绝空文件。
- 涉及文件交付时，列出每个文件的 sha256。
- 有字数/行数验收时，写实测数字，不估算。
- 时间戳必须与文件 mtime 一致；补写后同步更新。
- 不写密钥、令牌、账号；需要展示时只给掩码。

### 4.2 done 的硬条件

`steward_board.py done` 会检查：

1. `--evidence` 指向的文件存在且非空；
2. 自动计算并写入 `evidence_sha256`；
3. 把相对路径转成相对插件根的路径，绝对路径原样登记。

任一不满足即退出码 2，不得绕过。`--force` 只用于状态回退/重登记，不用于跳过证据存在性检查。

### 4.3 台账写入规则

- `state/registry.json` 是唯一任务事实源，**只经 `steward_board.py` 写**。
- 读单卡：`get --id <id> [--json]`（`get` 默认只回一行摘要，完整字段用 `--json` 或直接读文件）。
- 读列表：`list [--state queued] [--project ...] [--json]`。
- 脚本自动生成 `registry.json.bak-YYYYMMDD-HHMMSS` 备份；不要手工清理最近备份。
- 手工改任何 JSON（仅限 `dispatcher` 这类脚本不覆盖的元数据）必须：先备份、python roundtrip、`os.replace` 原子替换、改完复跑 `cron_health`。

### 4.4 状态机与 force

合法状态：`queued / dispatched / running / blocked / done / failed / abandoned`。

合法前进：`queued -> dispatched -> running -> done`；`dispatched/running -> blocked -> queued/running`；`failed -> queued（重试）或 abandoned`。

`done`、`abandoned` 是终态。任何回退（如 `done -> running`）必须显式 `--force`，并在 `--note` 写清原因。

### 4.5 日报、看板与决策记录

- 日报：`reports/DAILY_REPORT_YYYYMMDD.md`，一行一条：任务/状态/原因/下一步；失败与阻塞必须出现。
- 看板：有 `done` 变化后跑 `board_view.py` 刷新 `reports/board.html`。
- 决策：管家代拍的缺信息决定写入 `registry.decisions`（由脚本维护）或 `DECISIONS.md`，并告知 worker。
- 知识：修了 bug 按 `knowledge/README.md` 补用例与事故卡，再跑总闸。

### 4.6 自检门禁

对外声称「可用」前必须全绿：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/run_all.py
```

当前 15 门：巡检边界、边界用例、网关自检、巡检取证、插件出厂、成本取证、预算分桶、台账边界、工具边界、定时健康、任务台、调度引擎、私人知识库、密钥保险库、台账。任何一门 FAIL 都不算通过；`--fast` 只跳过网关故障注入，不改变其余门禁。门数会随脚本演进增加，以 `run_all.py` 实际输出的「N/N 通过」为准，不要背死数字。

---

## 5. 红线清单（每轮重申）

### 5.1 允许升级给用户的只有三类

1. 删除用户文件；
2. 对外发送消息/发布；
3. 上线部署。

其余一律按最保守假设代拍并继续，把不确定项写进「未决」。

### 5.2 文件与配置

- 不删除用户文件；不 `git reset --hard`；不覆盖未备份配置。
- 改 JSON 一律 python roundtrip + `os.replace`，禁止手写字符串或 `open(wb)` 直写。
- 搬路径前先 grep 全部运行时引用（hooks/cron/registry/memory-root/steward.json），搬后立即垫片。
- 构建日志、临时脚本、探针输出不落在源码仓；日志写仓外。

### 5.3 密钥与权限

- 密钥只掩码显示，不外传，不写进证据文件、日志、任务卡。
- 官方 GLM 账号（`account:*`）只能在 ZCode 官方渠道使用，**永不进 8788 聚合路由**（封号红线）。
- `protected_paths`（`~/.ssh`、provider 配置、ZCode 配置/会话库、`C:/Windows`）对任何会话都不放行。
- 不恢复 unattended 技能，不重建 stop-hook 死令循环。

### 5.4 调度纪律

- 不发明任务；不把「我打算怎么做」当汇报；不虚报完成。
- 同一项目默认只跑 1 个写手；并行改代码先拆 worktree。
- 派单必须带绝对 workspace；会话建完立即设置权限。
- 同一无害错误只解释 1 次，第 2 次必须动手修。
- 会话内不换模型；模型路由按 `docs/MODEL_SCORECARD.md` 与 `dispatcher_prompt.md` §0b。

### 5.5 路径禁区

- `D:/Github/dll`、`D:/Github` 下的旧副本是风暴前遗物，永远不要当参照或派单目标。
- 工作路径不明确时，用 `config/steward.json` 的 `canonical` 与 `dispatch_guard` 为准。
- `mate/Private` 是用户业务文件夹，不碰内容。

---

## 6. 常见误判

| 现象 | 错误判断 | 正确判断与做法 |
|---|---|---|
| 调度台很久没动静，`steward_cycle` 显示 idle | 「没有任务」 | cron 可能早已停摆。先跑 `cron_health --from-registry`；`STALE/DISABLED/NOTFOUND` 都要修（§3.1） |
| `ReadSession` 返回 `App control failed` | 「会话不存在，放弃」 | 是控制通道失联，按 replace 重派；不要反复重发或 OpenSession（§3.2） |
| 会话级已设 yolo | 「不会弹权限了」 | 子智能体/新窗口只继承**项目级**权限。必须同时设 `--workspace ... --allow-tool`（INC-20261118-004） |
| 台账里已有 `done` 卡 | 「任务确实完成了」 | `done` 可能证据缺失/为空/哈希漂移。以 `evidence_problem` 与证据文件内容为准（§3.3/§3.4） |
| worker 回「需要用户确认/信息不足」 | 「这是结论，停下等用户」 | 这是升级信号。先代拍、换路、拆任务；只有删文件/外发/部署才允许停 |
| 同一轮连续跑两次 `steward_cycle` | 「两次 actions 不一样，引擎坏了」 | 派单/回写会立刻改变 digest，输出本就会变。取 `action.prompt` 请在**同一轮**重跑一次再取 |
| `steward_board.py get` 只回一行 | 「字段丢了/台账坏了」 | `get` 默认就是摘要；完整字段用 `--json` 或读 `registry.json` |
| 工作时段出现 `queue-empty` | 「等于 idle，回一行结束」 | 必须从 backlog 落 1 项自主任务再结束；这正是夜班零产出的根因（INC-20261118-002） |
| 证据文件 mtime 比文中时间早 | 「时间戳无所谓」 | 说明内容是事后补写或时间写超前。任何补写后都要重算字数/sha256，并修正时间戳 |
| 在 `%TEMP%` 写巡检临时文件 | 「临时目录天然允许」 | 未登记会话才允许系统临时目录；已登记会话曾因 roots 约束被误杀（INC-20261118-001）。优先写到插件根内的临时路径或直接用管道 |
| 用 grep 检查文档插入是否成功 | 「grep 到了就是成功」 | 锚点不存在时 replace 会无效却报 updated。写后必须 grep 目标段落并读回文件确认 |
| worker 的 cwd 看起来是插件根/项目根 | 「相对路径可以省事」 | worker 的 cwd 是 ZCode 壳项目（常为 newcad），正本可能在别处。一律绝对路径，不 `cd` |
| 自动化每 20 分钟应在整点 :00/:20/:40 触发 | 「没按整点就是坏了」 | 间隔按创建锚点走，不按整点对齐（K-20261118-003）；只看 `next_run_at` 是否持续前进 |
| `attempts` 到上限还在 replace | 「再换一个会话试试」 | `claim` 会直接拒绝（`MAX-ATTEMPTS`）；此时必须 split 或人工裁决 |
| 任务卡 project 写了 mate/dll | 「会自动在正本目录跑」 | `project_map` 会把 dll 映射到 newcad 壳；真正的工作目录由任务卡 `workspace` 与 prompt 里的绝对路径决定 |
| 看板/日报没更新 | 「调度台没干活」 | 看板只在手动跑 `board_view.py` 后刷新；日报是追加写。以台账与证据文件为准 |
| 证据里写「测试全过」但没输出 | 「结论有了就行」 | 无命令、无退出码、无关键输出的证据按不合格打回（§3.3） |

---

## 7. git 回滚（基线 tag / 单文件 checkout / reflog）

> 适用：worker 提交了坏改动、合并后需要回退、误改了不该改的文件。
> 红线约束（§5.2）：本节操作只增不删——不 `git reset --hard`、不强推、不删除用户文件。未提交的误改用 `checkout <tag> -- <路径>` 恢复；已提交的坏改动用 `revert` 生成反向提交。

### 7.1 基线 tag：baseline-20261118

2026-11-18 应用户要求建立的可回滚基线。动手改代码前先确认基线存在、看清基线以来累计改了多少：

```bash
git -C <仓> rev-parse --verify baseline-20261118^{commit}
git -C <仓> log --oneline -1 baseline-20261118
git -C <仓> diff --stat baseline-20261118..HEAD | tail -2
```

实测（2026-11-18，`<仓>`=本插件仓）：

```text
bb09321856b28d0ee8b12169250e60df4c0fda31
bb09321 chore: 建立 git 基线，工作区可回滚（用户令 2026-11-18）
 14 files changed, 2381 insertions(+), 31 deletions(-)
```

要点：

- 回滚前先固定现场：`git status --porcelain`（未提交改动）+ `git log --oneline -5`（近期提交）。
- 每个基准日新建当日 tag（`git tag baseline-YYYYMMDD`）；旧 tag 只读，不删不改。
- tag 是仓级引用：在任何 worktree 里建/看 tag，全仓（含主仓）生效。

### 7.2 未提交误改：单文件 checkout 恢复（首选）

只恢复指定路径，不碰其他文件：

```bash
git -C <仓> checkout baseline-20261118 -- <路径>
```

实测（故意污染 `docs/RUNBOOK.md` 再恢复）：

```text
$ git status --porcelain                     # 污染后
 M docs/RUNBOOK.md
$ git checkout baseline-20261118 -- docs/RUNBOOK.md
$ echo $?
0
$ git status --porcelain                     # 恢复后
（空）
$ grep -c ROLLBACK-DEMO docs/RUNBOOK.md
0
```

要点：

- 整目录同理：`git checkout baseline-20261118 -- docs/`。
- 恢复后必须 `git status --porcelain` 复核为空，不凭感觉认为「应该恢复了」。
- 误改已横跨多个提交时，先 `git log --oneline -- <路径>` 找该文件最后一次正确的提交，再对该提交 checkout。

### 7.3 已提交坏改动：revert 生成反向提交

```bash
git -C <仓> revert --no-edit <坏提交>
```

实测（临时分支 `tmp/rollback-demo` 上：提交一行污染 → revert 撤销 → 删除临时分支，主分支无痕）：

```text
[tmp/rollback-demo 104ce14] tmp: revert demo 污染提交
[tmp/rollback-demo 144be6d] Revert "tmp: revert demo 污染提交"
（演示分支已删除，内容已还原；完整回显见 reports/evidence/W-20261118-032.md）
```

要点：

- revert 冲突时先 `git revert --abort` 回原状，再改用 §7.2 按文件恢复或人工处理；不要带冲突硬提交。
- **换行符陷阱**：Windows 上部分编辑工具会把整文件 CRLF/LF 改写，diff 显示成全文件重写（如本例出现 `577 deletions`）。提交前先归一化回 LF，确保 diff 只含业务新增行。
- 需要整体回到基线时，用 `git checkout -b recovery/<id> baseline-20261118` 新建分支，不要对在役分支做 reset。

### 7.4 reflog：找回「消失」的提交

误删分支、rebase 后找不到提交时，提交仍在 reflog（默认保留 90 天）：

```bash
git -C <仓> reflog --date=iso -8
```

实测（§7.3 演示留下的真实轨迹）：

```text
af9919f HEAD@{2026-11-18 19:15:10 +0800}: checkout: moving from tmp/rollback-demo to wt/W-20261118-032
144be6d HEAD@{2026-11-18 19:14:43 +0800}: revert: Revert "tmp: revert demo 污染提交"
104ce14 HEAD@{2026-11-18 19:14:43 +0800}: commit: tmp: revert demo 污染提交
af9919f HEAD@{2026-11-18 19:14:29 +0800}: checkout: moving from wt/W-20261118-032 to tmp/rollback-demo
```

恢复按安全度排序：

1. 只看内容：`git show <commit>`、`git show <commit>:<路径>`，不动工作区。
2. 拿回内容：`git checkout -b recovery/<id> <commit>` 建新分支，再 cherry-pick/合并，原分支不动。
3. `git reset --hard <commit>` 属于红线操作（§5.2）：未经管家明确授权且未先备份，禁用。

---

## 8. 5 分钟自理巡检与实时纠偏 SOP

> 适用对象：被派任务卡的 worker 会话。管家每 20 分钟巡检一轮，worker 自己每 5 分钟自检一轮；等管家发现问题再纠偏，通常已烧掉 15-30 分钟。

### 8.1 每五分钟五个动作

| 步 | 动作 | 做法 |
|---|---|---|
| 1 | 读 part 表 | part 表 = 当前任务的拆分清单：任务卡 `acceptance` 逐条 + `depends_on` 子卡。`steward_board.py get --id <id> --json` 看本卡，`list --json` 看兄弟子卡；给每项标「已完成/进行中/未开始/卡住」 |
| 2 | 判活 | **蓝点≠运行**（K-20261118-040）：应用任务列表的蓝点是「未读」标记；模型思考/工具执行的间隙转圈会消失退回蓝点。判活只看最近输出时间：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/verify_dispatch.py --id <id> --session <sid> --max-idle 180`，退出 4 = stalled |
| 3 | 对验收 | 把这几分钟的产出逐条对 `acceptance`；对不上的不算进展，不要自我计分 |
| 4 | 产证据 | 每完成一项立即把「命令/关键输出/结论」写进 `reports/evidence/<id>.md`，不要攒到最后补写 |
| 5 | 记进展 | 状态有变化就 `steward_board.py progress --id <id> --note "<一句话>"`，一行即可，不刷屏 |

### 8.2 自纠偏话术

连续 1 轮（5 分钟）无可复核产出 → 给自己发 nudge（不中断当前轮）：

```text
【自理巡检 HH:MM】已 N 分钟无可复核产出。回到 part 表第 X 项；
卡住就换做法，把已尝试方案写进「未决」。验收：<acceptance 原文>
```

连续 2 轮（10 分钟）仍无产出 → 给自己发 steer 三段式（停止项与替代做法都要写清）：

```text
【立即纠偏】<哪里跑偏/踩了哪条红线>
立即停止：<不要继续做的事>
改为：<替代做法>；验收：<可机械判定标准>
```

连续 3 轮（15 分钟）仍无产出 → 在回执「未决」里明确请求管家 steer 或拆分，不要硬扛到被 replace。

其他自查红线：

- 「需要用户确认/信息不足」不是结论：除删用户文件、外发消息、上线部署外，按最保守假设代拍继续（§2.4）。
- 每次纠偏后把「改了什么方向」追加进证据文件，保证轨迹可审计。

### 8.3 worktree 并行

同一项目多 worker 并行时，每张卡一个隔离工作区（分支 `wt/<task-id>`），不共享工作目录：

| 动作 | 命令 |
|---|---|
| 建/复用工作区（幂等） | `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/worker_worktree.py ensure --id <id> --repo <主仓> --json` |
| 查路径 | `python -X utf8 .../worker_worktree.py path --id <id>` |
| 查状态 | `python -X utf8 .../worker_worktree.py status --id <id> --ledger <主仓>/state/worktrees.json --json` |
| 收尾（合并+清理） | `python -X utf8 .../worker_worktree.py finish --id <id> --json`（见 §9） |

实测（status，本任务卡，从主仓台账只读查询）：

```json
{
  "ok": true,
  "id": "W-20261118-032",
  "repo": "C:/Users/Administrator/Documents/mate/plugins/ai-steward",
  "path": "C:/Users/Administrator/worktrees/ai-steward/W-20261118-032",
  "branch": "wt/W-20261118-032",
  "base_branch": "main",
  "exists": true,
  "valid": true,
  "dirty": [],
  "ahead": 0,
  "behind": 1
}
```

并行纪律：

- 只在自己的 `wt/<task-id>` 分支和目录里写；不碰其他 worktree，不直接改主仓工作区。
- 会动公共文件（同一份 RUNBOOK/台账/配置）要在任务卡里提前声明，减少合并冲突。
- 长活小步提交（每完成一个 part 就 commit），避免 finish 时巨大 diff 难审、冲突难解。

---

## 9. 多 worker 合并与清理

### 9.1 合并：逐卡 finish，不攒批

`finish` = `merge` + `remove`，是验收通过后的标准收尾。多 worker 并行时按依赖顺序逐卡执行：

```bash
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/worker_worktree.py status --id <id> --json
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/worker_worktree.py finish --id <id> --json
```

（模板：`finish` 会合并并删除当前工作区，不要在自己的工作区里对自己执行。）

判定要点：

- `merge` 只在主仓 HEAD 等于基准分支时执行；报 `worktree-dirty` = worker 没提交，先打回补 commit，不要 `--allow-dirty` 糊过去。
- 合并顺序：被依赖的子卡先合并；互不依赖的按任务号顺序，降低同文件冲突概率。
- 冲突：先在自己的 worktree 里 `git merge main` 解冲突并 commit，再重新 `finish`；解不掉升级给管家，不强行合并。

### 9.2 清理与台账核对

```bash
git worktree list
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/worker_worktree.py list
```

实测（2026-11-18，本插件仓）：

```text
C:/Users/Administrator/Documents/mate/plugins/ai-steward   af9919f [main]
C:/Users/Administrator/worktrees/ai-steward/W-20261118-027 d8a77b8 [wt/W-20261118-027]
C:/Users/Administrator/worktrees/ai-steward/W-20261118-028 e2fa829 [wt/W-20261118-028]
C:/Users/Administrator/worktrees/ai-steward/W-20261118-029 af9919f [wt/W-20261118-029]
C:/Users/Administrator/worktrees/ai-steward/W-20261118-030 af9919f [wt/W-20261118-030]
C:/Users/Administrator/worktrees/ai-steward/W-20261118-031 af9919f [wt/W-20261118-031]
C:/Users/Administrator/worktrees/ai-steward/W-20261118-032 af9919f [wt/W-20261118-032]
C:/Users/Administrator/worktrees/ai-steward/W-20261118-033 af9919f [wt/W-20261118-033]
```

```text
W-20261118-012         wt/W-20261118-012            removed C:/Users/TIAN/worktrees/ai-steward/W-20261118-012
W-20261118-014         wt/W-20261118-014            removed C:/Users/TIAN/worktrees/ai-steward/W-20261118-014
W-20261118-015         wt/W-20261118-015            removed C:/Users/TIAN/worktrees/ai-steward/W-20261118-015
W-20261118-018         wt/W-20261118-018            removed C:/Users/Administrator/worktrees/ai-steward/W-20261118-018
W-20261118-020         wt/W-20261118-020            removed C:/Users/Administrator/worktrees/ai-steward/W-20261118-020
W-20261118-024         wt/W-20261118-024            active C:/Users/Administrator/worktrees/ai-steward/W-20261118-024
W-20261118-025         wt/W-20261118-025            active C:/Users/Administrator/worktrees/ai-steward/W-20261118-025
W-20261118-026         wt/W-20261118-026            active C:/Users/Administrator/worktrees/ai-steward/W-20261118-026
W-20261118-027         wt/W-20261118-027            active C:/Users/Administrator/worktrees/ai-steward/W-20261118-027
9 个记录
```

判定要点：

- 台账 `state/worktrees.json` 与 `git worktree list` 可能短暂不一致（台账 active 但目录已删等）：先 `status --id <id>` 核对，失效记录用 `prune` 清，不手改 JSON。
- `remove` 只解 `node_modules` 联接再删工作区目录，绝不递归删主仓实体；删不掉必须如实报失败并写日报。
- 已合并的分支（台账 `branch_deleted=true`）不要手动重建；要找回内容走 §7.4 reflog。

### 9.3 失败兜底

| 现象 | 处置 |
|---|---|
| `finish` 报 dirty | 打回 worker 补 commit；确属误改垃圾先按 §7.2 恢复 |
| 主仓不在基准分支 | 先把主仓恢复到基准分支，再 merge |
| 工作区目录删不掉 | 如实报失败写日报，保留现场等人工，不假报成功 |

---

## 附录 A. 一页速查

```bash
# 每轮开场
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/cron_health.py --from-registry
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_cycle.py --json

# 查卡
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py get --id <id> --json
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py list --state queued

# 落卡
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py add --title "<标题>" --acceptance "<验收>" --project ai-steward

# 认领/验收/失败
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py claim --id <id> --session <sid> --command <commandId>
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py done --id <id> --evidence C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/evidence/<id>.md --summary "<结论>"
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py fail --id <id> --reason "<原因>" [--retry]

# 升级链
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py progress --id <id> --note "<进展>"
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py pull --id <id>
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py steer --id <id> --reason "<跑偏>" --interrupt applied --ack "<回执>" --command <commandId>
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py state --id <id> --state queued --force --note "<原因>"

# 收尾
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/board_view.py
python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/run_all.py
```

## 附录 B. 变更记录

| 日期 | 变更 | 证据 |
|---|---|---|
| 2026-11-18 | 首版：v3 调度日常、五类 action、故障处置、证据规范、红线、误判 | `reports/evidence/W-20261118-008.md` |
| 2026-11-18 | dispatch 自动建 git worktree（分支 `wt/<id>`），验收 `finish` 合并+清理，新增 `worktree-cleanup` action | `reports/evidence/W-20261118-011.md` |
| 2026-11-18 | 新增 §7-§9：git 回滚（基线 tag/单文件 checkout/revert/reflog）、5 分钟自理巡检与实时纠偏 SOP（part 表/蓝点≠运行/nudge/steer/worktree 并行）、多 worker 合并与清理 | `reports/evidence/W-20261118-032.md` |

| 2026-11-19 | 证据冻结 + reattest 唯一改锚入口；调度冻结优先、drift 只提示；digest 增 bottleneck/critical_path；并发 15→6、同项目 12→2；state 836MB→63MB | `docs/ARCHITECTURE_V2.md` §七、`scripts/test_board.py` 30/30、`scripts/test_cycle.py` 59/59 |
