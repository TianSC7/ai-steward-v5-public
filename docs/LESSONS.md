# 知识卡（LESSONS）

> 踩过的坑与可复用的判据。新增坑必须写"现象 / 根因 / 处置 / 如何验证"。

## K-001 ZCode rollout 文件名带前缀，别按目录名取会话 ID

- 现象：巡检只看到几十天前的 transcript，当前活跃会话全丢。
- 根因：rollout 文件名是 `model-io-sess_xxx.jsonl`，会话 ID 在**文件名**里，不在目录名里。
- 处置：`steward_scan.py` 先查路径段，再剥 `model-io-` 前缀取 basename。
- 验证：扫描结果里出现 0s/1s 的活跃会话。

## K-002 Git Bash 下的 Windows 命令会被路径转换

- 现象：`schtasks /Query` 变成 `C:/Program Files/Git/Query`。
- 根因：MSYS 把 `/Xxx` 当路径。
- 处置：用 Python `subprocess` 直接调，或 `MSYS_NO_PATHCONV=1`；开机自启改用用户级 Startup 的 VBS，免管理员也免路径转换。
- 验证：`gateway_ctl.py install` 后启动文件夹出现 `AI-Steward-Gateway.vbs`。

## K-003 补丁脚本写出的源码可能含真实 0x00

- 现象：`py_compile` 报 `source code string cannot contain null bytes`。
- 根因：多层字符串转义把 `\x00` 变成了真实空字节。
- 处置：补丁后用二进制检查 `b.count(b'\x00')`；需要空字符分隔时写转义文本而不是真实字节。
- 验证：`null_left 0` + 编译通过。

## K-004 假 PASS 比 FAIL 更危险

- 现象：`timeout-failover` 断言通过但耗时 0.0s。
- 根因：状态目录串了，慢通道已在上轮被冷却，本轮直接跳过——测试没真正覆盖超时路径。
- 处置：失败转移类断言必须带**时间下界**（如 `elapsed >= 1.0s`）+ 目标 lane 断言。
- 验证：修复后 1.5s。

## K-005 粘性必须绑定别名

- 现象（设计期发现）：`auto` 建立的粘性会被 `auto-heavy` 继承，导致重活落到快模型。
- 处置：指纹 = `conv_key + "|" + req_model`。
- 验证：边界用例显式使用同一对话、不同别名时不串。

## K-006 会话工具的能力边界

- `SendToSession` 保留目标会话原有模型，不能远程换模型。
- `CreateSession` 才是指定模型的入口。
- `sessionNotFound` 不等于会话消亡，先 `OpenSession` 重绑再重发。
- `ListSessions` 按标题子串搜，不搜 ID 片段。

## K-007 项目路径漂移是派单级事故

- 现象：ZCode `dll` 项目指向 `D:/Github/dll`，实际是 cad-suite 仓库线。
- 判据：派单前必须比对 `workspacePath` 与 `dispatch_guard.canonical`；不一致就停。
- 处置：`dispatch_guard.blocked_paths` + SKILL 第四节强制校验。

## K-008 上游不一定会尊重 Accept-Encoding

- 现象：已发 `identity`，仍返回 gzip。
- 处置：永远按响应头解压，不要假设；流式用增量解压器，避免整包缓冲。

## K-009 总闸解析 JSON 必须用完整 stdout

- 现象：`run_all.py` 拿"最后 3 行摘要"去 `json.loads`，把正常输出判成"被日志污染"。
- 处置：子进程返回 `(ok, 摘要, 完整stdout)` 三元组，展示用摘要、解析用完整输出。
- 判据：任何"摘要用于人看、原文用于机器解析"的分离。

## K-010 技能注入的 base dir 不是插件根

- 现象：SKILL.md 写 `scripts/xxx.py`，实际 base dir 是 `skills/<name>`，路径解析会错。
- 处置：技能里给插件根绝对路径；命令里也用绝对路径（本机固定安装位置）。

## K-011 usage-bar 按"请求模型名"记账，不看响应里的 model

- 日期：2026-11-17
- 现象：网关回传上游真实模型名后，usage-bar 仍把 5 次请求记在 `auto`（new-provider-7）名下、显示"未定价"。
- 根因：`model_usage` 表由 ZCode 在请求侧写入 `provider_id + model_id`，网关改响应体不影响记账。
- 处置：网关自己按 lane 真实模型 + `cached_tokens` 计价，走 `/admin/status` 暴露；不再依赖改响应名。
- 教训：先确认数据是谁、在什么时刻写的，再决定改哪一层；否则改得再对也不会生效。

## K-012 没有指纹的测试等于"可能测了旧代码"

- 日期：2026-11-17
- 现象：网关改完代码后若忘记重启，selftest 仍会 PASS；测试脚本被复制到别处运行也会 PASS。
- 处置：子进程 stderr 打 `FP` 指纹（插件版本 + 脚本 sha12 + schema + env 自检），总闸强制校验；
  selftest 额外比对运行中网关的 `version` 与源码 `VERSION`。
- 教训：PASS 只证明"这段代码在我眼前跑通了"，指纹才能证明"跑的是哪段代码"。

## K-013 空结果/空列表不得当"通过"

- 日期：2026-11-17
- 现象：`steward_scan` 返回 `{}` 时测试脚本跳过全部断言仍报 PASS；`/admin/status` 返回空 lane 列表时自检也 PASS。
- 处置：`test_scan` 断言输出是含 4 个项目的非空结构；`selftest` 对空 lane 列表判 FAIL；
  `run_all` 对 0 项目直接判失败。
- 教训：断言必须覆盖"该有东西却没有"的形态；只验键存在不看内容，等于没验。

## K-014 入站消息队列插件抢不了，只能让自己别长时间占线

- 日期：2026-11-17
- 现象：定时巡检消息在管家跑长任务时排队，等轮次结束才被看到，救援晚了。
- 根因：队列在宿主侧；插件（skills/commands/hooks）没有抢占入站执行的 API。
- 处置：出站用 `InterruptSession`+`SendToSession` 立即纠偏；管家自身保持"调度台"体量——
  长活派给 worker、单轮尽量 ≤2 分钟；入站紧急插话用 UI 的"立即"。
- 教训：先分清"哪一侧的队列"，再决定做插件还是改工作方式；对宿主队列只能适配，不能硬抢。

## K-015 证据文件会被改写，"done + 路径存在"还不够

- 日期：2026-11-17
- 现象：任务标记 done、证据文件也在，但内容被后续编辑覆盖，结论无法追溯。
- 处置：`ledger_check` 强制 `evidence_path` 存在；填了 `evidence_sha256` 就逐字节校验，
  不一致直接判失败；边界用例 10/10 覆盖坏形态。
- 教训：验收证据要么不可变，要么带哈希；"有文件"只是最低门槛。


## K-016 executionId 只在会话运行时可见

- 日期：2026-11-17
- 现象：会话 idle 时 `ReadSession(summary)` 不含 executionId；`ListSessions` 从不返回；
  给忙碌会话排队发送得到的 commandId，`WaitSessions` 只回 `queued`，拿不到当前执行句柄。
- 处置：要中断就趁目标在跑时读 summary；没有句柄时用 `exec_handle.py` 从 `session_input`
  表列候选，逐个试探到 `running` 为止。
- 教训：工具返回的字段随状态变化，别拿 idle 时的形状推断运行时能力。

## K-017 权限模式是"每会话一行"的库状态，不是全局设置

- 日期：2026-11-17
- 现象：ZCode 库里 `session.permission` 是 `{"mode":"build"|"yolo"}`；`CreateSession` 一律 build，
  不受调用方运行时模式影响；会话运行中改这一行会实时生效。
- 处置：`set_permission.py` 统一改库并备份旧值；配自动放行钩子兜底；UI 的“完全访问”仍是最终
  手动开关。
- 教训：遇到"每个会话都问"的开关，先找按会话存储的字段，别找全局 setting。

## K-018 配置钩子只在会话启动时加载

- 日期：2026-11-17
- 现象：钩子装好后，已存在的会话仍不生效（旧调度台继续等授权）；新开会话立刻正常。
- 处置：改钩子后必须新开会话验证；常驻角色（调度台）在钩子变更后重建，旧会话停用不删。
- 教训：钩子排障先问"这个会话是什么时候启动的"。

## K-019 自动化停摆是静默的，idle 不等于健康

- 日期：2026-11-17
- 现象：cron 被禁用、派发失败或 next_run_at 过期时，目标会话只是不再收到消息；
  `ReadSession(summary)` 仍显示 idle，巡检报告会误判为"无在办任务"。
- 处置：把自动化自身状态纳入巡检：`cron_health.py` 检查 enabled/lifecycle/next_run_at/
  last_error/target 一致性；过期超过 grace 即 STALE；总闸用 11 个用例锁住判定。
- 教训：监控系统要先监控"监控还活着"，否则静默失败会被当成一切正常。

## K-020 插件清单的 hooks 字段与自动发现会重复

- 日期：2026-11-17
- 现象：清单显式声明 `hooks` 路径后，CLI 报 `Duplicate plugin hooks file ignored`；
  钩子实际只加载一份，但诊断里带 warning，容易让人误判"钩子没装上"。
- 处置：只保留 `hooks/hooks.json` 文件、清单不写 `hooks` 字段；以
  `plugins list --json` 的 `hookDetails` 与 `diagnostics` 为准验收。
- 教训：插件"看起来配置了"不等于"只配置了一次"；以 CLI 的 diagnostics 为准。

## K-021 只看 cwd 的自动放行等于给整个工作区发通行证

- 日期：2026-11-17
- 现象：钩子按会话 cwd 放行后，cwd 在 `mate` 的任意会话（包括非管家会话）都能无提示写文件；
  目标路径完全没有校验，`C:/Windows`、`~/.ssh` 也在放行范围内。
- 处置：文件类工具改为从 `tool_input` 提取目标路径；未登记会话限定在 `autoallow.roots`/临时目录内，
  已登记会话不受 roots 限制，但 `protected_paths` 对任何会话都优先拦截；兜底开关可关。
- 教训：权限判定的对象是"这次操作的目标"，不是"会话站在哪"；收紧时别把"已登记会话"一起误伤，
  否则调度台回写临时脚本会直接卡成 needs_attention（实测踩到）。

## K-022 锚点操作只认原始字节，空白归一化 grep 禁用

- 日期：2026-11-22
- 现象：锚点校验先做空白归一再比对，假绿假红都有；C3 事故里真差异被归一化吃掉。
- 根因：HTML/JS 锚点差异藏在空白、转义与不可见字节里，归一化后的文本不再等于盘上字节。
- 处置：锚点一律对原始字节 repr；源码检查先 `.count()` 确认存在再按下标切上下文。
- 教训："看到的"不等于"盘上的"；锚点来自原始字节。

## K-023 大段 JS 必过 node --check 语法闸再上线

- 日期：2026-11-23
- 现象：z12 大段 JS 重写后页面局部哑火，肉眼看代码无异常。
- 根因：内联 JS 拼接处一个语法伤在浏览器才爆，静态"三绿"不查 JS 语法。
- 处置：大段 JS 改动后必跑 node --check（对现盘内联脚本抽取件），EXIT=0 才许上线。
- 教训：语法闸是廉价的保险丝，跳过它省的时间会加倍还回来。

## K-024 三绿不等于页面活：上线必过真引擎真页运行时探针

- 日期：2026-11-23
- 现象：kpifit 删静态 rejectbtn 后顶层绑定残留，TypeError 把顶层打断——钟/取数/委托全灭，壳照画，全浏览器白屏 11 小时无人察觉。
- 根因：编译/资源/路由三绿验证不了运行时；"局部还活着"（天气壳）恰恰是误导源。
- 处置：上线验收加运行时探针（钟走字/版本回填/取数回填三证）；删 DOM 必同步扫绑定；顶层绑定加空安全+保险丝 setTimeout(0)。
- 教训：页面死活的判据是"它还在动"，不是"它没报错"。

## K-025 Edge headless 不可信：面板验收走 IAB 真渲染配方

- 日期：2026-11-22
- 现象：Edge headless 下 blob-Worker 不回、virtual-time-budget 假象、动画 mid 帧拍不到、#hash 导航不重载——三绿全是假象。
- 根因：headless 快进与真实渲染路径不同；同文档 #hash navigate 不触发 reload。
- 处置：真渲染用 IAB：pathToFileURL + 本地 harness 注假 DATA + 点击走 cua 坐标 + 流式 #dbg 探针；需硬刷时加 &r= 参数；拍前重新 scrollTo（10s 重渲染会重置滚动位）。
- 教训：验证工具本身要先过"它测得出真问题吗"这一关。

## K-026 enqueue 一律显式 --target

- 日期：2026-11-22
- 现象：enqueue 不传 --target 时默认覆写 v2.html，专改其他文件的单把面板冲了。
- 根因：默认值是历史遗留，不是"无目标"。
- 处置：任何 enqueue 显式传 --target，多文件用 + 连接；入队后回读 queue 核对 target 字段。
- 教训：带默认值的危险参数，宁可啰嗦也要显式。

## K-027 热修须恢复字节惯例，验收 builder 只认 "starting builder" 日志行

- 日期：2026-11-22
- 现象：c4 数据通道一行热修改了文件 EOL 字节惯例，验收 builder 丢锚，单子永不绿。
- 根因：验收链认特定日志行/字节形态；热修顺手换了行尾=换了锚。
- 处置：热修前后逐字节比对惯例（EOL/缩进/BOM）；验收锚丢失时先查字节差异再查逻辑。
- 教训：一行修复也要尊重文件的原有字节脾气。

## K-028 二进制文件禁入 prompt

- 日期：2026-11-23
- 现象：二进制（APK）内容进 builder prompt 直接崩内核，表现成 stale 泵自愈重开再崩的循环。
- 根因：prompt 是纯文本通道，二进制进=data 毒丸；自愈重开对"输入不变"的失败无效。
- 处置：派单前校验 spec/brief 纯文本；附件走文件路径引用，不内联内容。
- 教训：自愈救不了"每次都喂同一颗毒丸"的单。

## K-029 统计口径必须同族：avg_sec 被秒级单污染成假 ETA

- 日期：2026-11-25
- 现象：看板在制长单永远"99% 卡死 + 预计交付过期照挂"；时长进度与任务进度互相矛盾。
- 根因：avg_sec=128 混入了秒级完成的产线单，长单 dwell/avg 恒超 1 → pct=min(99,..) 恒 99；eta_delivery 过期不重算。
- 处置：均速样本只取同族单（产线自动单剔除），样本不足 avg_sec=0 走纯时长显示；eta 过期改"已超预计 X 分，继续施工中"诚实态。
- 教训：一个被污染的均值能把整面看板变成"冻结的谎言"。

## K-030 远端运维四坑（TIAN→AA ssh 通道）

- 日期：2026-11-25
- 现象：inline PowerShell 的 `$vars` 被本地 bash 吃掉写坏文件；远端 inline `python -c` 莫名死；Git Bash /tmp 里的文件 Windows python 找不到；中文 GBK 输出乱码。
- 根因：本地 shell 先展开远端内容；远端无交互 tty；/tmp 是 MSYS 虚拟路径；控制台代码页 GBK。
- 处置：复杂远端操作一律 scp 推 .ps1/.py 再 `-File` 执行；临时文件放 `C:/Users/<远端用户>/AppData/Local/Temp/`；读输出 encoding="utf-8", errors="replace"。
- 教训：远端执行的"脚本文件化"是唯一稳态，inline 只配 echo。

## K-031 Python 字符串处理四小坑（高频低级但真翻车）

- 日期：2026-11-25
- 现象：%-格式串里的字面 `%` 报 not enough arguments；`"C:\Users"` 路径 SyntaxError（\U 转义）；heredoc 落盘剥掉复杂转义；插桩 print 位置不对ImportError。
- 根因：% 格式化把裸 % 当占位符；非 raw 字符串吃 \U；heredoc 做一层反斜杠剥除；`from __future__` 必须在最前。
- 处置：字面 % 写 %%；Windows 路径用 raw 字符串或正斜杠；复杂转义内容用 Write 工具落盘不走 heredoc；插桩放 `from __future__` 之后。
- 教训：低级坑的杀伤力按"出现在多长的命令链里"计。
