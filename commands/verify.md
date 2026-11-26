---
description: 复核：对指定任务跑确定性 review gate 与证据 sha256 复核，只报 verdict 与理由，FAIL 不放行
argument-hint: "<任务ID> [--json]"
---

加载 `ai-steward` 技能。任务 ID：$ARGUMENTS

步骤：

1. **取卡**：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py --json get --id <任务ID>`
   —— 确认任务存在，读出 `acceptance` / `evidence_path` / `evidence_sha256`；查不到就直接判 FAIL，不臆造。
2. **旁路门（条件步骤）**：若 reviewer 旁路门脚本 `review_gate`（W-20261118-025 交付）已落地到
   `C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/` 目录，优先改跑它
   （`--id <任务ID> --json`，输出 verdict/reasons/checks，口径与本命令一致）；
   未落地则跳过本步，以第 3 步为准，并在汇报里注明"旁路门未落地"。
3. **确定性复核**：`python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_review.py --id <任务ID>`
   —— 退出码 0 = VERDICT PASS，2 = FAIL；机械核对 acceptance 与证据三要素（命令/输出/结论）。
4. **证据 sha256 复核**：把第 1 步读到的 `evidence_sha256` 与文件实测值比对，不一致即 FAIL（证据被改过）。
   本机 git `core.autocrlf=true`，检出盘上的证据文件是 CRLF、而卡片哈希按 LF 内容登记，
   所以先按 LF 归一化再哈希，两种口径任一命中即通过，仅行尾差异不算篡改（但要注明）：
   `python -X utf8 -c "import hashlib;d=open(r'C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/evidence/<任务ID>.md','rb').read().replace(b'\r\n',b'\n');print(hashlib.sha256(d).hexdigest())"`
   （原始字节口径可用 `certutil -hashfile C:/Users/Administrator/Documents/mate/plugins/ai-steward/reports/evidence/<任务ID>.md sha256`；
   归一化与原始两种哈希都与卡片不符，才是真的被改过。）
5. **汇报**：verdict（PASS/FAIL）/ 逐条理由 / sha256 比对结果 / 建议动作。
   任一步 FAIL 都不许把任务推到 `done`——走 `python -X utf8 C:/Users/Administrator/Documents/mate/plugins/ai-steward/scripts/steward_board.py done --id <任务ID> --evidence <证据路径>` 落账之前必须全绿。

用法示例：
- `/verify W-20261118-022` → 全量复核（旁路门若在则跑 + steward_review + sha256）。
- `/verify W-20261118-022 --json` → 只出机器可读结论，供管家回合直接引用。

纪律：全程只读，不写 registry、不改证据文件；sha256 不一致只报告，不自动重封（重封与否由管家决定）。
