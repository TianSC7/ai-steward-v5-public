# 大管家私有知识库

> 这是管家自己的“私人库”，不是公共经验库。公共库（`docs/LESSONS.md`、仓库里的日报）
> 记录项目事实；这里记录**管家怎么干活**：工具怪癖、调度模式、踩过的坑、代位拍板的依据。

## 放什么

- 工具/环境的硬事实：钩子加载时机、权限库位置、automation 锚定、会话句柄规则。
- 调度模式：并发上限、路径冲突、升级链、验收口径。
- 事故复盘（`incidents/`）：现象 / 根因 / 修复 / 防复发（哪条用例或脚本锁住了它）。
- 偏好与红线：用户明确说过的“不要做什么”。

## 不放什么

- 仓库结构、代码怎么改——这些在项目文档和 git 里。
- 一次性的会话流水——只留结论与可复用事实。
- 密钥原文、任何敏感值——只允许掩码。

## 自我迭代协议（每次出问题必须走完）

1. **止血**：先修到能跑，别让任务空转。
2. **锁死**：能加用例就加用例（`test_*.py` 挂进 `run_all.py`）；不能加就加一条巡检断言。
3. **复盘**：`python -X utf8 scripts/steward_kb.py add --type incident ...`
   写清现象/根因/修复/防复发。
4. **改手册**：如果是流程问题，改 `state/dispatcher_prompt.md` 或 `skills/ai-steward/SKILL.md`，
   不只改代码。
5. **复跑总闸**：`python -X utf8 scripts/run_all.py`，全绿才算收口。

## 用法

```bash
python -X utf8 scripts/steward_kb.py add --title "..." --body "..." \
  --tags hook,permission --type fact --confidence high --source "实测 2026-11-18"
python -X utf8 scripts/steward_kb.py search --query "权限 钩子"
python -X utf8 scripts/steward_kb.py match --text "worker 写文件被拦"
python -X utf8 scripts/steward_kb.py index
python -X utf8 scripts/steward_kb.py verify
```

调度引擎会在派单时自动把最相关的 3 条经验注入 worker prompt（`【管家知识】`）。
