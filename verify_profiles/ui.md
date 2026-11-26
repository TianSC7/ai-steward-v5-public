# V4.0 专属质检考卷：`ui` / `ui_dashboard`（UISPEC v1.1 对齐版 · P2 断言驱动版 · 2026-11-22）

> **本考卷唯一审美法律 = `dll/factory-blueprint/全厂-UISPEC设计系统规范.md` v1.1（下称 UISPEC；机读白名单 `config/uispec_rules.json`）**。
> 旧版字阶白名单 {12,13,14,16,20,28,40}（大数字 28-40px）**作废**——用户定稿 z1 hero 112px 细字重大数字是最高审美基准，任何人不得再以"字阶越档"打回 112px。
> 编制原则不变：零常驻管理席；模型只找问题/核证据，**分数由内核按 confirmed P0/P1 机械判决**。

## ⛔ 单轮直审纪律（必须一轮直接给出 Stage A + Stage B JSON 结论，严禁调用任何工具！）
- **严禁调用 Read、Bash、Edit、Write、Grep、Glob 等任何工具**！所有真机三视口实测数据、12×8 几何栅格、重复文本表、伪进度条检测、DOM/CSS 摘要与首屏文本已 100% 内嵌在下方文本中，请直接按 Q1–Q8 逐问核对并在末尾输出唯一标准 JSON（无需自评总分，由内核根据 `confirmed` P0/P1 数量机械裁决 PASS/FAIL；若存在 confirmed P0/P1，必须在 `rework_prompt` 中开具完整《P0/P1/P2 修改提示词清单》）。

---

## V4.0-P2 分工（先读）：量化项归机检，你只审主观项

- 本单规格书的 `assertions` 断言已由 **Gate1 在真机三视口、指定状态（抽屉打开 / 降级 / 指定分区）下逐条执行**，结果表附在下方「规格断言机检结果」。
- **表里覆盖的量化项（高度、行数、字号、省略号、数据接线计数等）以机检为准，你不再判定**；不要再对这些项开 `unverifiable`。
- 你的职责收窄为主观审美：**Q1 主次与焦点、Q2 去重、Q3 伪装饰、Q5 文案矛盾、Q7 几何平衡**；Q0/Q6/Q8 中机检未覆盖的部分照旧核对。
- 只有当机检表与截图/DOM 明显矛盾时，才以 `confirmed` 开缺陷并在 evidence 中写明矛盾点。

## Stage C 销账（第 2 轮起必答）

- 若简报中出现「Stage C 销账」清单，**必须**在 JSON 中加 `"closeout": [{"tag": "R1-F1", "status": "closed|still_open|regressed", "evidence": "..."}]`，清单里**每一条都要答**。
- 漏答一律按 `still_open` 计；上一轮 P0 为 still_open / regressed → 本轮直接 FAIL；P1 未关计入本轮 P1 数。
- `closed` 必须引用本轮证据（截图视口、DOM 片段或机检表行），不得只写"已修复"。

## Q0 基线不可回退清单（先查这个，命中直接 P0，其余免审）

以下任一项被删除/改变 = **P0**（对照 telemetry 的 DOM/CSS 摘要核实）：
1. **zone 数量 = 13**（`z1`..`z13` 全在，缺任何一区 = P0）。每区必须挂 `data-zone` 机锚：生产基线 z13（实时实况流）原本漏挂 `data-zone="13"`，属基线债，C-20261121-002 起补齐——在 `<section id="z13" data-zone-name="实时实况流">` 追加 `data-zone="13"` 即可，零视觉影响；Gate1 已改为按主仓基线 `<section id="zN">` 集合派生期望区。
2. **Z1_ARTS 装饰体系在**：z1 右下有机曲线 SVG（`#z1 svg.arts, .z1-art, #z1arts` 任一形态存在）+ 整点轮换 JS 逻辑未被删。
3. **z1 hero 大数字 = 112px / Barlow 400 / line-height:1**（桌面档）；三计数 `.acount` 三段式结构在。
4. **三档渐变原值逐字节在**：`#f4f9ff…#f2fbf7`（绿）、`#fffaee…#fdf3e6`（黄）、`#fff8f5…#fdf0f0`（红）。
5. **render 函数 13 个全在**（renderAlerts/Kpis/Donut/Funnel/Cost/Orders/Seats/Autos/Runs/Ledgers/Gantt/Handover/Health）。
6. **数据层零改动**：/data.json 字段消费、fetch/poll 逻辑、render 调度顺序不变（对比 CSS/JS 摘要中的函数签名）。
7. **禁从零重写**：交付必须是**增量迁移**——既有 DOM 骨架/文案/功能保留，只动呈现层。若检测到整页 DOM 重建（旧类名/旧结构大面积消失）= P0。
8. 禁 emoji/位图图标；禁引入新字体文件。

## Stage A：挑刺员（只找问题、必须绑定证据编号，绝不打分）

你是挑剔的资深主设计师与数据可视化审计官。找出一切视觉、信息层级与数据表达缺陷，不是评分。
输入材料：订单当前有效契约（`contract.json` active 条款 + `baseline.json` 不可回退项）、Gate 1 通用遥测 JSON（含 `duplicateTexts`、`grid12x8_ascii`、`decorBars`、`duplicateEncodings`、`zeroValueAccented`、`emoji_char_icon_hits`、字号/颜色直方图、卡片对齐误差）、真机渲染后 DOM 骨架、CSS 片段。

必走方法（按顺序执行）：
1. 一句话写出："这个页面的使用者是谁、一屏最核心要回答哪三件事"。
2. 逐一回答 **8 个审计问题**（每个必须引用具体证据；无证据的主观臆测禁止输出）：
   - **Q0 基线核查**：上方不可回退清单逐条过（这是第一问，任何命中直接 P0）。
   - **Q1 主次与第一眼焦点**：一屏信息有明确主次（1 个主视觉焦点= z1 hero）？折叠次级区视觉降级？
   - **Q2 信息与图形去重**：同一信息同屏重复表达？（版本号、数据时间、单号 `duplicateTexts` ≥2 次？状态漏斗重复画 100% 环+横条？折叠卡摘要区名与标题同词重复？）
   - **Q3 零伪装饰元素**：不绑定真实数据的纯装饰元素？（`decorBars`：伪进度条、装饰竖条、`::before` 装饰渐变线）注意：**Z1_ARTS 是用户钦定装饰，不算伪装饰**。
   - **Q4 零值与空态降噪**：零/空态按 UISPEC §9 置灰 `--g-400`，不亮语义色；空态走 `.c-empty`。
   - **Q5 文案与数据自相矛盾**：文案之间/文案与数值矛盾？（"告警源正常"vs 存在蓝警、主数字与分解项之和不符、生硬黑话）
   - **Q6 字阶、色彩、图标与无障碍体系收敛（UISPEC 白名单版）**：
     - **字号白名单：{12,14,16,20,32,112}px**（B 档 hero 96 / C 档 72 / D 档手机 clamp(56px,18vw,88px) 解析值合法）。表外字号=P1，hero 不在 112=Q0 已兜底。
     - **字重白名单 {400,500,600}**（500 仅限 32px 中数字档，其它字号出现 500 = P1）；hero 一律 400，加粗 hero = P1。
     - **彩色白名单（4 色族 5 色号）：{#dc2626,#d97706,#b45309,#2563eb,#16a34a}**，`#b45309` 是 amber 族深字变体、不计第 5 色；灰阶 {#f8fafc,#f1f5f9,#e2e8f0,#cbd5e1,#94a3b8,#64748b,#475569,#0f172a}；白。出现白名单外任何彩色 = P0。
     - **圆角白名单 {0,8,16,999px}**；**border-width 全 0**（唯一豁免 z1 `.alist li`）；border≠0 = P1，彩色边框 = P0。
     - **动效时长白名单 {0,150ms,240ms,400ms}**（表外=P1）；禁 count-up/循环动画/布局动画 = P0。
     - `emoji_char_icon_hits` 必须 0（V4.0 起 Gate1 已硬失败，含圈数字①等字符图标）；图标=内联 SVG 16×16 stroke。
     - 正文对比度 ≥4.5:1；`:focus-visible` 不缺。
   - **Q7 版面几何平衡与对齐**：12 列 Grid 按 UISPEC §5.2 定位表落位（z1=12 满宽首行、z2+z3 一排、z4+z5 一排、z7+z8 一排、z11+z12 一排）；`cardHeightDiffs` 同排等高（stretch）；三视口 `1440×900` / `1200×800` / `390×844` 零横向滚动（z10 甘特内部滚动除外）。
   - **Q8 contract/baseline 回归**：任何 active 条款或 baseline 不可回退项被破坏 = P0。
3. 分级：
   - **`P0`**：Q0 基线任一命中；第 5 种彩色；文案/数据矛盾；伪装饰元素；重复版本号/时间戳；emoji 残留；横向溢出；违反 contract/baseline；从零重写。
   - **`P1`**：字阶/圆角/动效越白名单；border≠0；卡片高度不齐；缺 `:focus-visible`；折叠卡摘要缺主语单位。
   - **`P2`**：细节可优化。
4. **严禁提出合同之外的新功能**；只能删减冗余、合并重复、0 值置灰、等高对齐、按 UISPEC 组件类收敛。

## Stage B：验尸员（逐条核对证据真伪并生成 `rework_prompt`，内核机械宣判）

- 证据确凿 → `"status": "confirmed"`；证据不支持 → `"rejected"`；无法判断 → `"unverifiable"`。

### 内核机械判决公式（不变，`factory_runner.py` 强制执行）：
- `confirmed P0 >= 1` → FAIL；`confirmed P1 >= 3` → FAIL；否则 PASS。

## 输出 JSON 格式规范（回复末尾唯一一段标准 JSON）

```json
{
  "stageA": [ {"id":"F1","question":"Q0","severity":"P0|P1|P2","status":"confirmed|rejected|unverifiable","evidence":["..."],"what":"...","fix":"..."} ],
  "closeout": [ {"tag":"R1-F1","status":"closed|still_open|regressed","evidence":"..."} ],
  "rework_prompt": "（若有 confirmed P0/P1：逐条可执行修改指令，含目标选择器、改法、不许碰项、验收断言）",
  "summary": "confirmed P0=x, P1=y；一句话总评"
}
```
