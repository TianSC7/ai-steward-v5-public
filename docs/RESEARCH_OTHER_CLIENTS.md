# 其它客户端 MCP / CLI 非交互能力调研（WorkBuddy / Trae / Comate）

- 任务号：W-20261118-006
- 调研日期：2026-11-18
- 调研方式：纯桌面调研。只读官方文档站与官网，不安装、不部署、不登录、不实测运行任何客户端。
- 证据口径：优先官方文档与官网原文；检索引擎结果仅用于产品识别。所有引文均为抓取当日原文。
- 配套证据：`reports/evidence/W-20261118-006.md`（抓取过程、关键引文、验收自测）。
- 复核：2026-11-18 10:38–10:42 由续跑会话用 WebFetch 直抓官方文档逐条回验 11 处引文，旗标与结论无修订（见证据文件第 6 节）。

## 0. 一页结论

| 客户端 | 厂商 | 是否支持 MCP | 是否支持 CLI 非交互 | 结果回收方式 | 结论 |
|---|---|---|---|---|---|
| WorkBuddy（任务单写法 "Workbody"，未检索到该拼写的独立产品） | 腾讯（workbuddy.cn） | 支持，但仅以 MCP 客户端角色消费工具（连接器 = MCP + CLI 形态） | 本体不支持（无 CLI / 无 OpenAPI）；旁路 CodeBuddy Code CLI 支持 | 本体：客户端内产物空间 / 移动端遥控，无程序化回收；旁路 cbc：stdout JSON / stream-json / 结构化 JSON | WorkBuddy 本体不可用；旁路 CodeBuddy Code CLI 可用（待装机验证） |
| Trae | 字节跳动 | 支持，IDE / Work / CLI / 企业版全线可作为 MCP 客户端 | 支持（TRAE CLI 2.0 `traecli exec`，官方明示"支持非交互模式，可嵌入自动化脚本或持续集成流水线"） | stdout JSONL 事件流 / `-o` 末条消息落盘 / `--output-schema` 结构化 / trajectory JSON / 会话 resume、fork | 可用（文档证据齐备，装机与计费口径待验证） |
| Comate（文心快码） | 百度 | 支持（MCP 市场 + `.comate/mcp.json` 项目级/全局级，stdio / SSE / Streamable HTTP） | 支持（`comatecli run -q`，文档定位"管道、脚本与 CI"） | stdout=结果 / stderr=错误、`-d task-json` 等五种输出模式、退出码 0/1、`serve` 本机 HTTP SSE、一次性 license | 可用（文档证据齐备，装机与计费口径待验证） |

一个关键判读，避免误读：**"支持 MCP"只说明该客户端能把外部工具接进来（客户端角色），不等于它能被管家程序化驱动。** 管家要的是后者的镜像——由外部程序驱动客户端并回收结果，因此 CLI 非交互（或 HTTP API）才是硬门槛。WorkBuddy 有前者无后者；Trae 与 Comate 两者都有。

## 1. WorkBuddy（腾讯）——任务单中的 "Workbody"

### 1.1 产品识别

- 未检索到名为 "Workbody" 的独立产品；中文检索（CN Bing，2026-11-18）全部结果指向腾讯 **WorkBuddy**，判定任务单中的 "Workbody" 即其变体拼写。
- 补强确认（2026-11-18 复核轮，WebSearch）：36kr 报道 WorkBody 产品经理此前负责 CodeBuddy（腾讯 AI 编程助手）；腾讯 2026 年 Q2 财报电话会同步点名 "Workbody AI Office Productivity Service and CodeBuddy AI Coding"——**官方口径本身即使用 "Workbody" 拼写**，与 CodeBuddy 同门的判定成立。
- 官网 workbuddy.cn 标题："WorkBuddy - AI Agent 办公新范式"；腾讯云产品页（cloud.tencent.cn/product/workbuddy）描述："腾讯出品的全场景 AI 办公工作台"，覆盖日常办公、代码开发与设计创意。
- 注意存在同名海外站 workbuddy.ai（"AI Agent for Everyday Office Work"），与腾讯版的关系未核实（部分检索结果提示为同一产品的海外拼写形态），本调研以 workbuddy.cn / 腾讯云口径为准（见未决）。
- WorkBuddy 与 CodeBuddy（腾讯云代码助手）同源：WorkBuddy 文档站顶部导航含 IDE / 插件 / CLI 三个板块，其中 CLI 板块页面标题为"腾讯云代码助手 CodeBuddy – AI 代码编辑器"，canonical 指向 codebuddy.cn/docs/cli/。

### 1.2 是否支持 MCP：支持（仅客户端角色）

证据（官方文档"连接器"页，workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector，页面构建 2026-11-12）：

- "连接器分为 **MCP + CLI（标准化协议）** 和 **Skill + CLI（内置脚本）** 两种形式"。
- 已内置 QQ 邮箱、腾讯文档、腾讯乐享、腾讯会议、TAPD、腾讯网盘等连接器；乐享连接器的授权页明示"通过乐享知识库 MCP，WorkBuddy 将能够……"。
- "自定义连接器……安装自定义的 MCP 服务。自定义连接器的配置方式与 MCP 配置类似"。

即 WorkBuddy 能通过 MCP 使用外部工具，但全部发生在它的客户端任务执行内部。

### 1.3 是否支持 CLI 非交互：本体不支持

- 腾讯云产品文档（cloud.tencent.com/document/product/1831/134384，产品简介）通篇无 MCP / CLI / OpenAPI / headless 表述。
- WorkBuddy 完整文档目录（入门指南、从入门到精通、订阅与费用等）中无 CLI / API / 开放接口章节；自动化能力仅有客户端内"定时任务"（Automation-Guide）。
- "远程遥控 WorkBuddy"实践案例（Practice-Six）给出的方案是**移动端远程对话**，不是程序接口。
- 开放平台（open.workbuddy.cn）面向的是**生态入驻**："首期已开放五大生态能力：Buddy 应用、专家、Skill、连接器、硬件"——开发者向 WorkBuddy 内上架能力，而非外部程序驱动 WorkBuddy。

### 1.4 旁路：同门的 CodeBuddy Code CLI 具备完整非交互能力

WorkBuddy 文档站 CLI 板块即 CodeBuddy Code 文档（VitePress，canonical codebuddy.cn）：

- 安装：`npm install -g @tencent-ai/codebuddy-code`；命令 `codebuddy` / `cbc`；要求 Node.js 18+。
- 无头模式（docs/cli/headless，最后更新 2026-09-17）："无头模式允许您通过命令行脚本和自动化工具以编程方式运行 CodeBuddy Code"。
- 关键旗标：`-p/--print` 非交互执行；`--output-format text|json|stream-json`；`--json-schema` 结构化输出（结果在 `structured_output` 字段）；`--resume`/`--continue` 多轮；`--mcp-config servers.json` 挂 MCP；`--allowedTools/--disallowedTools` 工具白名单（含 `mcp__*`）；`--append-system-prompt`。
- 高危开关："`-y`（或 `--dangerously-skip-permissions`）是非交互模式的必需参数"；文档同时给出 `CODEBUDDY_IS_SANDBOX=1` 沙箱环境变量，且提示 HIGH/CRITICAL 危险命令仍可能要求确认。
- 其余集成面（侧栏）：GitLab CI/CD 集成、ACP 协议集成、Daemon 模式、Web UI、远程控制、HTTP API (Beta)、Python/TypeScript SDK、SDK MCP 集成。

### 1.5 结论

- WorkBuddy 本体：**不可用**（作为管家派单 worker 无程序化入口；客户端内定时任务与移动端遥控不可审计，与管家证据链冲突）。
- 旁路：**CodeBuddy Code CLI 可用**（文档证据齐备；装机、登录态与腾讯系 Credits 扣减口径待验证）。若目标是消耗腾讯系积分，应接 `cbc -p` 而不是 WorkBuddy。

## 2. Trae（字节跳动）

### 2.1 产品形态与官方文档

docs.trae.cn 产品矩阵明确四条线：TRAE（IDE，Agent 窗口 + IDE 窗口双形态）、TRAE Plugin（VS Code/JetBrains/Android Studio 插件）、TRAE CLI、TRAE 企业版。其中 TRAE CLI 官方定位原文："在终端输入自然语言指令……**支持非交互模式，可嵌入自动化脚本或持续集成流水线中**。**通过 MCP 集成第三方工具与服务**，也支持配置自定义智能体、命令等。"（docs.trae.cn 首页/llms.txt，2026-11-18 抓取）

### 2.2 是否支持 MCP：全线支持

- IDE 线（docs.trae.cn/ide_model-context-protocol.md）："TraeCode 中的智能体可作为 MCP 客户端向 MCP Server 发起请求"；配套页：添加 MCP Server（MCP 市场 + 手动配置，stdio 与 HTTP 两类字段，支持项目级 MCP Server）、MCP Server 安装链接（Schema 链接一键安装）、在智能体中使用 MCP Server、查看 MCP Server 日志、MCP Server 相关问题排查。
- Work 线：work_mcp-overview.md / work_remote-mcp-server.md（stdio 与 HTTP、超时配置、变量引用）。
- CLI 2.0：cli_tools-and-extensions.md —— CLI 通过插件、技能和 MCP 扩展能力，TUI 内 `/plugins`、`/skills`、`/mcp` 管理；配置文件参考（cli_config-file.md）含"项目级信任配置和 MCP 服务配置示例"。
- 企业版：企业控制台统一管理 MCP Server（enterprise_model-context-protocol.md），并有 MCP 白名单（enterprise_mcp-allowlist.md）；"自动运行 MCP"设置（ide_auto-run-and-security.md）可免审批执行 MCP 工具。

### 2.3 是否支持 CLI 非交互：支持（traecli exec）

证据（docs.trae.cn/cli_command-line-parameters.md，2026-11-18 抓取）：

```text
traecli exec [选项] [PROMPT]
traecli exec --json --sandbox workspace-write "运行测试并修复失败用例"
echo "总结这个仓库的架构" | traecli exec -o out.md
```

- `--json`："把事件以 JSONL 流式打印到 stdout"。
- `-o, --output-last-message <FILE>`：把 agent 最终消息写入文件。
- `--output-schema <FILE>`：用 JSON Schema 约束最终回复结构。
- `--ephemeral`：不落盘会话文件；`resume` / `fork` 会话管理；`review` 子命令（--uncommitted / --base / --commit）。
- 权限与沙箱：`-s, --sandbox read-only|workspace-write|danger-full-access`；`-y, --dangerously-bypass-approvals-and-sandbox`（别名 `--yolo`）；`--allowed-tool/--disallowed-tool`。
- 自动化登录：`traecli login --with-trae-pat`（文档标注为"自动化场景登录"的官方路径）；环境变量 `TRAECLI_PERSONAL_ACCESS_TOKEN`（cli_environment-variables.md，文档明确警告 PAT 不得写入公开仓库与日志）。

### 2.4 结果回收方式

1. stdout JSONL 事件流（`--json`）——程序化逐事件解析；
2. 末条消息落盘（`-o`）——直接当回执草稿；
3. `--output-schema` 结构化输出——可机器校验 acceptance；
4. 会话文件默认落盘，`traecli resume --last` 可续查；
5. 补充：开源版 bytedance/trae-agent（MIT，约 12.2k stars）亦支持 `trae-cli run` 非交互、`mcp_servers` 配置、trajectory JSON 全量落盘（`trajectories/trajectory_YYYYMMDD_HHMMSS.json`，含 LLM 交互/步骤/工具调用/元数据）与 `--must-patch`。注意 trae-agent 用自有 LLM API key，不消耗 Trae 客户端订阅积分（判断，待验证）。

### 2.5 结论：可用（待装机验证）

`traecli exec` 与管家"派单 → 证据 → 复核"模型契合度最高：JSONL 事件流可回填台账 progress，`-o` 产物可直接作为 evidence 文件雏形，`--output-schema` 可映射 acceptance 的机器校验。

## 3. Comate（百度 文心快码）

### 3.1 产品形态与官方文档

百度智能云文档站 COMATE 产品文档（cloud.baidu.com/doc/COMATE）侧栏显示完整形态：Comate AI IDE、VSCode / JetBrains / Visual Studio / Xcode 插件，以及独立的 **CLI 快速入门**（/doc/COMATE/s/0mrcv8nsa）；扩展能力含 Subagent、Skills、Plugin、Rules、**MCP**（/doc/COMATE/s/3msr9pbuy）、Command、Memory、Automation。官网（comate.baidu.com）主打"编程智能体"与 Code Mode / Mission Mode（任务模式）。

### 3.2 是否支持 CLI 非交互：支持（comatecli run）

证据（CLI 快速入门，/doc/COMATE/s/0mrcv8nsa，2026-11-18 抓取）：

- 安装：`npm i -g @comate/comatecli`（Node.js >= 20.10），命令 `comatecli`。
- 无头入口：`comatecli run`，定位"一条命令完成一次（或多轮）AI 任务……管道、脚本与 CI"。
- 核心用法：`comatecli run -q "解释这个仓库的构建流程"`；多轮 `--continue` / `-c, --conversation <id>`；`--mode Agent|Ask|Plan|Goal`；`--spawn-mode LOCAL|WORKTREE`（原生 worktree 并行，与管家 worktree 分支路线同构）；`--retry <N> --retry-query <文本>`；`--attach-image/--attach-file`。
- 输出模式：`-d, --display task|task-json|delta-stream|event-stream|record-stream|record`，示例 `comatecli run -q "总结变更" -d task-json | jq .`。
- 约定：**"错误信息走 stderr，正常输出走 stdout"**；退出码 0=成功、1=校验/鉴权/运行失败。
- 自动化鉴权：`comatecli login`（一次登录长期有效）/ `comatecli login --no-browser`（无浏览器 SSH 环境）/ `comatecli status` 验证；或每次 `-l, --license <license>`"不落盘的一次性鉴权"，文档明示适用于 CI / 临时环境。
- 服务化：`comatecli serve [--port]` 在本机暴露 HTTP SSE 服务，`serve kill` 关闭——可作常驻 worker 的备选通道。

### 3.3 是否支持 MCP：支持

证据（MCP 章节，/doc/COMATE/s/3msr9pbuy）：

- "作为 MCP 客户端"向 MCP Server 请求工具与数据；传输支持 STDIO、SSE（上游规范已弃用，推荐 Streamable HTTP）、Streamable HTTP。
- MCP 市场：插件右上角 `···` > MCP > 市场页签，检索并安装，支持"JSON 配置预览"。
- 手动配置：项目级 `.comate/mcp.json`（项目目录）与全局级 `.comate/mcp.json`（用户主目录），结构为 `"mcpServers"` 键，字段含 `command` / `args`（示例 `npx -y @modelcontextprotocol/server-sequential-thinking`）。
- 运行治理：安装后自动启动，状态灯四种（未启动/已启动/启动中/启动失败），失败可"查看日志"；对话中工具调用默认需批准，可信服务可勾选"自动执行"。

### 3.4 结果回收方式

stdout（正常输出）/ stderr（错误）+ 退出码是主通道；`task-json` 等 display 模式给结构化事件；`serve` 提供 HTTP SSE 常驻订阅。文档未见 `-o` 式单文件落盘旗标（与 Trae 的差异点），落盘回收需由调用方重定向 stdout。

### 3.5 结论：可用（待装机验证）

CLI 非交互、结构化输出、退出码、一次性 license 与 worktree 双 spawn 模式都齐备；缺口是结构化输出的 schema 约束能力未见文档（待装机核对 `comatecli run --help`）。

## 4. 横向对比

| 维度 | WorkBuddy（腾讯） | Trae（字节） | Comate（百度） |
|---|---|---|---|
| MCP 客户端角色 | 有（连接器 = MCP + CLI；自定义连接器即 MCP 服务） | 有（IDE/Work/CLI/企业版全线） | 有（市场 + .comate/mcp.json） |
| 可被程序化驱动（管家视角硬门槛） | 本体无；旁路 cbc 有 | `traecli exec` | `comatecli run` |
| 非交互命令 | cbc：`codebuddy -p "..." -y` | `traecli exec "..."` | `comatecli run -q "..."` |
| 结构化输出 | cbc：`--output-format json/stream-json` + `--json-schema` | `--json`（JSONL）+ `--output-schema` | `-d task-json`（无 schema 约束） |
| 末条消息落盘 | cbc：无（stdout 重定向） | `-o <FILE>` | 无（stdout 重定向） |
| 多轮 / 续跑 | `--resume`/`--continue` | `resume`/`fork` | `--continue`/`-c` |
| 免审批开关 | `-y` 必需（高危）+ `CODEBUDDY_IS_SANDBOX=1` | `--yolo` / sandbox 三档 | "自动执行"勾选（客户端内） |
| 工具白名单 | `--allowedTools/--disallowedTools` | `--allowed-tool/--disallowed-tool` | 未见旗标（自动执行粒度为服务级） |
| MCP 挂载 | cbc：`--mcp-config` | 配置文件 mcp 服务示例 | `.comate/mcp.json` |
| 自动化鉴权 | 未核实（登录态/PAT 待装机确认） | `login --with-trae-pat` + `TRAECLI_PERSONAL_ACCESS_TOKEN` | `login` / `--no-browser` / `-l license` |
| 常驻通道 | Daemon 模式、HTTP API (Beta)、Web UI | ACP 协议 | `serve` 本机 HTTP SSE |
| 官方 CI/自动化背书 | GitLab CI/CD 集成文档 | "可嵌入自动化脚本或持续集成流水线" | "管道、脚本与 CI" |
| 结论 | 本体不可用；旁路 CodeBuddy CLI 可用（待验证） | 可用（待验证） | 可用（待验证） |

## 5. 对管家 v3 的落地建议

1. **worker 二型化**：台账 `registry.json` 给任务增加 `worker_kind`（`zcode` 会话型 / `cli` 进程型）与 `worker.launch` 声明。会话型维持现有 CreateSession/SendToSession 链路；cli 型由调度台直接 spawn，用退出码 + stdout 判活。
2. **接入优先级**：`traecli exec`（`--json` + `-o` + `--output-schema` 与现有证据模型最贴合）> `comatecli run -d task-json`（退出码/stderr 口径清晰，license 一次性鉴权适合无人值守）> `cbc -p --output-format json --json-schema`（能力最全但 `-y` 必需、高危面最大，必须叠加 `CODEBUDDY_IS_SANDBOX=1` 与 allowedTools 白名单）。WorkBuddy 本体不接。
3. **统一结果回收契约**：cli worker 强制两条产线——(a) 结构化 stdout 落到 `reports/evidence/<task>.md` 的"命令/输出/结论"三要素中（steward_review.py 的既有校验直接复用）；(b) 末条消息或结构化输出单独落盘为回执附件并登记 sha256。Trae 走 `-o`，Comate/cbc 由调度台重定向 stdout。
4. **安全边界**：三家免审批开关都属于高危面，cli worker 只允许 `--sandbox workspace-write`（Trae）/沙箱环境变量（cbc）级别的授权；`--cwd` 一律指向独立 worktree（与分支 backlog 的 worktree 并行方案合流，Comate 原生 `--spawn-mode WORKTREE` 可直接用）；`--allowed-tool` 白名单按任务卡声明，最小授权。
5. **密钥纪律**：PAT / license / API key 全部走 key_vault 以环境变量注入进程，不进 argv、不进日志、不进证据文件（TRAE 官方文档对 PAT 有同样的警告口径，两家口径一致）。
6. **预算与计费**：三家计费口径不同（TRAE 积分套餐、Comate 个人/企业订阅、腾讯 Credits），CLI 侧没有统一用量回传接口；先按任务粒度在台账记估分，人工周对账，跑通一家再接第二家。
7. **不建议做的对接**：WorkBuddy 客户端内定时任务、移动端遥控、TRAE 自动化任务（Agent 窗口）等"客户端内自动化"——发生在对方 UI 内、管家无法取证，违背"没有证据不算完成"的底线。
8. **装机验证清单**（下一步小卡）：Windows 下三家安装与版本；`traecli login --with-trae-pat` / `comatecli login --no-browser` / cbc 登录态；各跑一条 `-p/exec/run` 冒烟单并核退出码与输出；核对 Comate 是否支持 schema 约束与 `-o` 等价物；核对 cbc 自动化鉴权是否有非交互路径。

## 6. 未决与风险

1. "Workbody" 拼写已由双源交叉确认指向腾讯产品（36kr 报道 + 腾讯 2026 Q2 财报电话会口径）；仅 workbuddy.ai 海外站与腾讯版的关系仍未核实，如需消耗海外站积分需单独验证。
2. 三家均为文档结论，未装机实测：旗标行为（尤其退出码、超时、stderr 口径）以装机冒烟为准。
3. Comate 结构化输出的 schema 约束能力、cbc 非交互自动化鉴权的官方路径，官方文档未给全，列为装机核对项。
4. 积分/credits 扣减无法从 CLI 统一取回，预算闸对 cli worker 只能做任务粒度估算，存在对账误差。

## 7. 证据来源索引

Trae：
- docs.trae.cn（TRAE 概览/产品矩阵，含 CLI 非交互与 MCP 定位原文）
- docs.trae.cn/ide_model-context-protocol.md、ide_add-mcp-servers.md、ide_mcp-server-install-links.md、ide_use-mcp-servers-in-agents.md、ide_check-mcp-server-logs.md
- docs.trae.cn/cli_command-line-parameters.md（exec 全旗标）、cli_tools-and-extensions.md（/mcp 等）、cli_config-file.md、cli_environment-variables.md、cli_permission-and-sandbox.md
- docs.trae.cn/work_mcp-overview.md、work_remote-mcp-server.md、enterprise_mcp-allowlist.md、enterprise_model-context-protocol.md
- github.com/bytedance/trae-agent（开源 CLI agent：run / mcp_servers / trajectory）

Comate：
- cloud.baidu.com/doc/COMATE/s/0mrcv8nsa（CLI 快速入门）
- cloud.baidu.com/doc/COMATE/s/3msr9pbuy（MCP）
- cloud.baidu.com/doc/COMATE/s/9lnvs245z（功能介绍页侧栏，确认 CLI/MCP/Subagent/Automation 章节）
- comate.baidu.com（官网，编程智能体与 Code/Mission Mode）

WorkBuddy / CodeBuddy：
- www.workbuddy.cn（官网）、cloud.tencent.cn/product/workbuddy（产品页）
- eu.36kr.com/en/p/3847122859542788（WorkBody 产品经理曾负责 CodeBuddy）；腾讯 2026 Q2 财报电话会点名 "Workbody AI Office Productivity Service and CodeBuddy AI Coding"（WebSearch 检索摘要，2026-11-18）
- cloud.tencent.com/document/product/1831/134384（产品简介，无 MCP/CLI/OpenAPI）
- workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector（连接器 = MCP + CLI；自定义连接器 = MCP 服务）
- workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Practice-Cases/Practice-Six（远程遥控 = 移动端）
- workbuddy.cn/docs/cli/ 与 workbuddy.cn/docs/cli/headless（CodeBuddy Code CLI：安装、-p 无头模式、--output-format、--json-schema、--mcp-config、-y、沙箱）
- open.workbuddy.cn（开放平台：Buddy 应用/专家/Skill/连接器/硬件 生态入驻）
