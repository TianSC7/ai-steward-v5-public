# AI 命令面一页速查（COMMANDS）

`ai-steward` 通过 `.zcode-plugin/plugin.json` 的 `"commands": "./commands"` 注册命令目录：
每个 `commands/*.md` 就是一条 `/命令`，frontmatter 的 `description` 与 `argument-hint`
显示在命令面板里，正文是加载技能后的执行步骤。本页是全部命令的速查索引
（W-20261118-026 建立，同批新增 `/board` 与 `/verify`）。

## 命令清单（与 `ls commands/*.md` 一致，排除 .bak）

| 命令 | 参数 | 用途 | 主要脚本 |
|---|---|---|---|
| `/dispatch` | `<要做什么> [--project <项目路径>] [--tier cheap\|auto\|heavy\|reason]` | 派单：一句话需求 → 任务卡 + 挑模型 + 开 worker 会话 + 登记台账 | `scripts/set_permission.py` |
| `/gateway` | `[status\|reload\|test\|live\|start\|stop]` | 网关运维：通道健康/额度冷却/成功率，热重载路由，自检与故障注入 | `scripts/gateway_ctl.py`、`scripts/selftest.py` |
| `/steer` | `<sessionId\|任务关键词> <纠偏指令>` | 立即纠偏：中断正在跑偏的 worker，立刻投递修正指令 | 台账 `state/registry.json` + 会话工具 |
| `/steward` | `[巡检\|汇报\|接管 <目标>]` | 管家巡检：查台账、取证项目/会话/网关/成本，只报需要拍板的事 | `scripts/steward_scan.py` |
| `/board` | `[refresh\|summary] [--now <ISO时间>]` | 看板：一键汇总台账并刷新 `reports/board.html` 只读看板 | `scripts/board_view.py`、`scripts/steward_board.py` |
| `/verify` | `<任务ID> [--json]` | 复核：确定性 review gate + 证据 sha256 复核，FAIL 不放行 | `scripts/steward_board.py`、`scripts/steward_review.py` |

## 常用组合

- 派单后跟盯：`/dispatch <需求>` → `/steward` 巡检 → 跑偏时 `/steer <会话> <指令>` → 收尾 `/verify <任务ID>`。
- 每日一看：`/board` 刷新看板（`reports/board.html`，浏览器打开后每 60 秒自刷）。
- 网关异常：`/gateway status` → 需要时 `/gateway reload` → `/gateway test` 自检。

## 维护纪律

- 新增命令 = 在 `commands/` 放一个 `.md`（frontmatter 必须带 `description` 与 `argument-hint`，
  正文引用真实存在的脚本路径并附用法示例），并同步更新本页表格。
- 本页清单必须与目录一致；自查：
  `ls C:/Users/Administrator/Documents/mate/plugins/ai-steward/commands/*.md`（排除 `.bak`）。
- 命令正文引用的脚本路径改名前，先改命令页；台账写入口只有 `scripts/steward_board.py`，
  命令默认只读，写动作一律走台账脚本留痕。
