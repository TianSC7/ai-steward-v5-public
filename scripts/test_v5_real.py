# -*- coding: utf-8 -*-
"""V5.0 真化（2026-11-25）回归：下午「全厂跑一下午零交付」的每一道墙，各钉一颗钉子。

  合入  M1 主干手改与交付不冲突 → 自动保护性提交后干净合入
        M2 真冲突 → 只报冲突文件，abort 干净，其余文件不进主干，主干手改原样保留
        M3 主干未跟踪文件撞上分支新文件 → 如实报冲突，主干那份字节不动
        M4 主干别处的手改不挡合入，也不动它
        M5 保护性提交落不下 → target-dirty-overlap（工厂侧问题），不硬合并
  收口  C1 合入成功但沙箱没收拾干净 → 仍算交付
        C2 非 code 单的目标外改动留在沙箱，不进主干也不删
        C3 目标文件引用到的越界文件 → 拒合入并点名
        C4 code 单的运行态文件（state/ logs/）不跟着 git add -A 进主干；声明为目标的照常进
        C5 主干早已是同样内容 → 按已交付处理
        C6 合入阶段工厂侧原因原样透传，且在 MERGE_INFRA_REASONS 里
  证据  E1 累计 diff 全量落盘：已提交 + 新建文件都看得见，分段 + 索引对得上
        E2 施工回执进简报；不是本轮写的、没法比对时的，都如实标注
  调度  E3 内核崩溃后的自愈重排有上限，超过转 failed（交给泵）
        E4 多目标沙箱：只有第二个目标改了也算有成果，不当空沙箱抹掉
        E5 Gate1 口径：累计改动里的已提交文件带 committed 标记，工人自提交不再挨冤
  简报  V1 质检简报装配段（从 execute_job 原样抽出真跑）：全文落盘，纪律 / diff / 回执 / 清单齐全
        V2 超长规格：命令行回落通道发缩减版，如实写明缩了什么、全文在哪，全文照常落盘
        V3 UI 单：折叠栏摘要 / DOM 快照 / 累计改动证据 / 实拍路径都进简报
  事故  G3 合入时 git 的三种「没东西可提交」都算数（2026-11-26 G44R4 / G44R5 卡在合入）
        G4 隐藏窗口里的跑单进程收到控制台 Ctrl+C 只留痕、不退出（2026-11-26 G42R4 四次「没正常收口」）
        G5 专家席探活只问系统、不发信号（os.kill(pid, 0) 在 Windows 上是发 Ctrl+C）
        G6 验收脚本本轮真被改了 → 质检简报事前单列（V5.0「验收脚本落在改动范围内不当作验收」做实，2026-11-26）

用法：python -X utf8 test_v5_real.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
无 LLM、无网络、不碰真实台账：每个用例各用临时 git 仓。
"""
from __future__ import annotations

import argparse
import ast
import atexit
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
# 共享文件锁 / 槽位账本日志落在 STEWARD_STATE_DIR 指的目录（官方测试注入点）：指到临时目录，别落在源码树的 state/ 里
os.environ["STEWARD_STATE_DIR"] = tempfile.mkdtemp(prefix="v5real_state_")
atexit.register(lambda: shutil.rmtree(os.environ["STEWARD_STATE_DIR"], ignore_errors=True))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_runner as fr  # noqa: E402
import worker_worktree as ww  # noqa: E402

# factory_runner 里写死的绝对路径（活仓 / 档案仓 / 队列 / 台账 / 日志）一律改指临时目录：
# 这套测试要能在真机上跑，绝不往生产的队列、台账、日志里写一个字节。
_TEMP_HOME = tempfile.mkdtemp(prefix="v5real_home_")
atexit.register(lambda: shutil.rmtree(_TEMP_HOME, ignore_errors=True))
_LIVE_PREFIX = r"c:\users\administrator\documents\mate"
# 先记下再改指：AA 上代码自己所在目录也在这个前缀下（镜像就放在 plugins 里），下面的循环会把它一并改走
_SCRIPTS_DIR0 = fr.SCRIPTS_DIR
for _k, _v in list(vars(fr).items()):
    if isinstance(_v, str) and _v.lower().startswith(_LIVE_PREFIX):
        setattr(fr, _k, os.path.join(_TEMP_HOME, _v[len(_LIVE_PREFIX):].strip("\\/")))
for _k in ("DB_SQLITE", "TASKS_INDEX_SQLITE", "RUNTIME_PROVIDER_ROOT", "ZCODE_V2_LOG_DIR", "ZCODE_CJS"):
    if isinstance(getattr(fr, _k, None), str):
        setattr(fr, _k, os.path.join(_TEMP_HOME, "nodb", _k))       # 会话库 / 供应商目录：指向不存在的地方
_left = sorted(k for k, v in vars(fr).items()
               if not k.startswith(("__", "NATIVE_")) and isinstance(v, str) and len(v) > 3 and v[1:3] in (":\\", ":/")
               and k not in ("NODE_EXE", "DOTNET_EXE", "SCRIPTS_DIR")   # SCRIPTS_DIR = 代码自己所在目录，不是生产状态
               and not os.path.normcase(v).startswith(os.path.normcase(_TEMP_HOME)))
assert not _left, "factory_runner 还有没改指临时目录的路径常量：%s" % _left
assert os.path.normcase(_SCRIPTS_DIR0) == os.path.normcase(HERE), "SCRIPTS_DIR 应等于内核自己所在目录：%s" % _SCRIPTS_DIR0

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def run(cmd, cwd, must=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if must:
        assert r.returncode == 0, "%s failed:\n%s\n%s" % (cmd, r.stdout, r.stderr)
    return r.stdout


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


APP = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"


def make_repo(case):
    repo = os.path.join(case, "repo")
    write(os.path.join(repo, "scripts", "app.py"), APP)
    write(os.path.join(repo, "scripts", "other.py"), "o = 1\n")
    write(os.path.join(repo, "state", "queue.json"), "{}\n")
    write(os.path.join(repo, "docs", "readme.md"), "readme\n")
    run(["git", "init", "-q", repo], case)
    run(["git", "-C", repo, "config", "user.email", "t@example.com"], case)
    run(["git", "-C", repo, "config", "user.name", "t"], case)
    run(["git", "-C", repo, "config", "core.autocrlf", "false"], case)
    run(["git", "-C", repo, "add", "-A"], case)
    run(["git", "-C", repo, "commit", "-qm", "base"], case)
    return repo


def ctx(case, name):
    d = os.path.join(case, name)
    os.makedirs(d, exist_ok=True)
    cfg = {"root": os.path.join(d, "wt"), "links": [], "branch_prefix": "wt/"}
    return cfg, os.path.join(d, "ledger.json")


def sandbox(name, repo, cfg, ledger):
    res = ww.ensure(name, repo, cfg=cfg, ledger_path=ledger)
    assert res.get("ok"), res
    return res["path"]


def commit_all(wt, case, msg="delivery"):
    run(["git", "-C", wt, "add", "-A"], case)
    run(["git", "-C", wt, "commit", "-qm", msg], case)


def sl(paths):
    return [str(p).replace("\\", "/") for p in (paths or [])]


# ------------------------------------------------------------------ 合入
def m1(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "m1")
    wt = sandbox("M1", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n")
    commit_all(wt, wt)
    write(os.path.join(repo, "scripts", "app.py"), "a = 1\nb = 2\nc = 3\nd = 4\ne = 500\n")   # 主干手改：隔开的另一行
    m = ww.merge("M1", repo=repo, ledger_path=ledger)
    ok = bool(m.get("ok")) and m.get("merged") is True
    check("M1 主干手改不冲突时自动保护性提交后干净合入", ok, json.dumps(m, ensure_ascii=False)[:400])
    check("M1 保护性提交记在结果里", (m.get("protective_commit") or {}).get("committed") is True, str(m.get("protective_commit")))
    body = read(os.path.join(repo, "scripts", "app.py"))
    check("M1 主干同时含手改与交付", body == "a = 1\nb = 20\nc = 3\nd = 4\ne = 500\n", repr(body))
    st = run(["git", "-C", repo, "status", "--porcelain", "--", "scripts/app.py"], case).strip()
    check("M1 合入后该文件工作区干净", st == "", st)


def m2(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "m2")
    wt = sandbox("M2", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 222\nb = 2\nc = 3\nd = 4\ne = 5\n")
    write(os.path.join(wt, "scripts", "extra.py"), "x = 1\n")
    commit_all(wt, wt)
    write(os.path.join(repo, "scripts", "app.py"), "a = 111\nb = 2\nc = 3\nd = 4\ne = 5\n")   # 主干手改：同一行
    m = ww.merge("M2", repo=repo, ledger_path=ledger)
    check("M2 真冲突报 merge-conflict", m.get("ok") is False and m.get("reason") == "merge-conflict", json.dumps(m, ensure_ascii=False)[:400])
    check("M2 只点名冲突文件", sl(m.get("conflicts")) == ["scripts/app.py"], str(m.get("conflicts")))
    check("M2 abort 干净（无 MERGE_HEAD）", m.get("aborted") is True and not os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD")))
    check("M2 没冲突的新文件也没进主干", not os.path.exists(os.path.join(repo, "scripts", "extra.py")))
    body = read(os.path.join(repo, "scripts", "app.py"))
    check("M2 主干手改原样保留", body.startswith("a = 111\n") and "<<<<<<<" not in body, repr(body))


def m3(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "m3")
    wt = sandbox("M3", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "new.py"), "branch\n")
    commit_all(wt, wt)
    write(os.path.join(repo, "scripts", "new.py"), "live\n")        # 主干上未跟踪的同名文件
    m = ww.merge("M3", repo=repo, ledger_path=ledger)
    check("M3 未跟踪文件撞分支新文件 → 如实报冲突", m.get("ok") is False and m.get("reason") == "merge-conflict", json.dumps(m, ensure_ascii=False)[:400])
    check("M3 冲突名单就是那个文件", sl(m.get("conflicts")) == ["scripts/new.py"], str(m.get("conflicts")))
    body = read(os.path.join(repo, "scripts", "new.py"))
    check("M3 主干那份字节不动", body == "live\n", repr(body))


def m4(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "m4")
    wt = sandbox("M4", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n")
    commit_all(wt, wt)
    write(os.path.join(repo, "docs", "readme.md"), "hand edit\n")    # 别处的手改
    m = ww.merge("M4", repo=repo, ledger_path=ledger)
    check("M4 别处的手改不挡合入", m.get("ok") is True and m.get("merged") is True, json.dumps(m, ensure_ascii=False)[:400])
    check("M4 不需要保护性提交", not m.get("protective_commit"), str(m.get("protective_commit")))
    st = run(["git", "-C", repo, "status", "--porcelain", "--", "docs/readme.md"], case).rstrip("\n")
    check("M4 别处的手改原样留着（仍未提交）", st.startswith(" M") and read(os.path.join(repo, "docs", "readme.md")) == "hand edit\n", st)


def m5(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "m5")
    wt = sandbox("M5", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n")
    commit_all(wt, wt)
    live = "a = 100\nb = 2\nc = 3\nd = 4\ne = 5\n"
    write(os.path.join(repo, "scripts", "app.py"), live)
    orig = ww.protect_live_edits
    ww.protect_live_edits = lambda *a, **k: {"ok": False, "error": "simulated index.lock", "files": []}
    try:
        m = ww.merge("M5", repo=repo, ledger_path=ledger)
    finally:
        ww.protect_live_edits = orig
    check("M5 保护性提交落不下 → target-dirty-overlap（工厂侧原因）",
          m.get("ok") is False and m.get("reason") == "target-dirty-overlap" and "simulated index.lock" in str(m.get("error")),
          json.dumps(m, ensure_ascii=False)[:400])
    check("M5 该原因在 MERGE_INFRA_REASONS 里（不回炉冤枉工人）", "target-dirty-overlap" in fr.MERGE_INFRA_REASONS)
    check("M5 主干手改没被动过", read(os.path.join(repo, "scripts", "app.py")) == live)


# ------------------------------------------------------------------ 收口
def c1(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "c1")
    wt = sandbox("C1", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "deliver.py"), "def f():\n    return 1\n")
    write(os.path.join(wt, "build.log"), "noise\n")                 # 施工垃圾：不进提交，沙箱因此收不干净
    ok, info = fr._commit_and_merge_worktree("C1", repo, wt, ["scripts/deliver.py"], {"scripts/deliver.py": ""},
                                             kind="code", ledger_path=ledger)
    check("C1 合入成功但沙箱没收拾干净 → 仍算交付", ok is True, json.dumps(info, ensure_ascii=False)[:500])
    check("C1 交付在主干", os.path.exists(os.path.join(repo, "scripts", "deliver.py")))
    check("C1 沙箱垃圾没进主干", not os.path.exists(os.path.join(repo, "build.log")))
    check("C1 标注 remove_pending 且沙箱保留", info.get("remove_pending") is True and os.path.isdir(wt))


def c2(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "c2")
    wt = sandbox("C2", repo, cfg, ledger)
    write(os.path.join(wt, "dashboard", "v2.html"), "<html>new</html>\n")
    write(os.path.join(wt, "dashboard", "notes.txt"), "scratch\n")   # 目标外改动，目标文件没引用它
    ok, info = fr._commit_and_merge_worktree("C2", repo, wt, ["dashboard/v2.html"], {"dashboard/v2.html": ""},
                                             kind="ui_dashboard", ledger_path=ledger)
    check("C2 非 code 单有目标外改动也能交付", ok is True, json.dumps(info, ensure_ascii=False)[:500])
    check("C2 目标在主干", os.path.exists(os.path.join(repo, "dashboard", "v2.html")))
    check("C2 目标外改动没进主干", not os.path.exists(os.path.join(repo, "dashboard", "notes.txt")))
    check("C2 目标外改动留在沙箱没被删", os.path.exists(os.path.join(wt, "dashboard", "notes.txt")))


def c3(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "c3")
    wt = sandbox("C3", repo, cfg, ledger)
    write(os.path.join(wt, "dashboard", "v3.html"), '<html><script src="helper.js"></script></html>\n')
    write(os.path.join(wt, "dashboard", "helper.js"), "x\n")          # 目标文件真引用到的越界文件
    ok, info = fr._commit_and_merge_worktree("C3", repo, wt, ["dashboard/v3.html"], {"dashboard/v3.html": ""},
                                             kind="ui_dashboard", ledger_path=ledger)
    check("C3 目标引用到越界文件 → 拒合入并点名",
          ok is False and info.get("reason") == "unexpected-files" and sl(info.get("files")) == ["dashboard/helper.js"],
          json.dumps(info, ensure_ascii=False)[:500])
    check("C3 主干没被改动", not os.path.exists(os.path.join(repo, "dashboard", "v3.html")))


def c4(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "c4")
    wt = sandbox("C4", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "deliver.py"), "x = 1\n")
    write(os.path.join(wt, "state", "config_v5.json"), "{}\n")        # 声明为目标的 state 文件
    write(os.path.join(wt, "state", "queue.json"), '{"changed": 1}\n')  # 运行态文件：不该进主干
    write(os.path.join(wt, "logs", "run.log"), "x\n")
    ok, info = fr._commit_and_merge_worktree("C4", repo, wt, ["scripts/deliver.py", "state/config_v5.json"],
                                             {"scripts/deliver.py": "", "state/config_v5.json": ""},
                                             kind="code", ledger_path=ledger)
    check("C4 多目标 code 单交付成功", ok is True, json.dumps(info, ensure_ascii=False)[:500])
    check("C4 代码目标在主干", os.path.exists(os.path.join(repo, "scripts", "deliver.py")))
    check("C4 声明为目标的 state 文件照常进主干", os.path.exists(os.path.join(repo, "state", "config_v5.json")))
    check("C4 运行态 state 文件没被改", read(os.path.join(repo, "state", "queue.json")) == "{}\n")
    check("C4 日志没进主干", not os.path.exists(os.path.join(repo, "logs", "run.log")))


def c5(case):
    repo = make_repo(case)
    same = "a = 42\nb = 2\nc = 3\nd = 4\ne = 5\n"
    write(os.path.join(repo, "scripts", "app.py"), same)               # 主干早就是这份内容
    run(["git", "-C", repo, "add", "-A"], case)
    run(["git", "-C", repo, "commit", "-qm", "already on main"], case)
    cfg, ledger = ctx(case, "c5")
    wt = sandbox("C5", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), same)                 # 沙箱交来一模一样的内容
    sha = fr.sha256_file(os.path.join(repo, "scripts", "app.py"))
    ok, info = fr._commit_and_merge_worktree("C5", repo, wt, ["scripts/app.py"], {"scripts/app.py": sha},
                                             kind="code", ledger_path=ledger)
    check("C5 沙箱与主干同头、无可提交 → 按已交付处理", ok is True and info.get("reason") == "already-up-to-date",
          json.dumps(info, ensure_ascii=False)[:500])

    # 另一种：沙箱是早先从旧主干分叉的，成果在分支上；重跑时主干早已经有同样内容
    repo = make_repo(os.path.join(case, "b"))
    cfg, ledger = ctx(os.path.join(case, "b"), "c5b")
    wt = sandbox("C5b", repo, cfg, ledger)
    same = "a = 43\nb = 2\nc = 3\nd = 4\ne = 5\n"
    write(os.path.join(wt, "scripts", "app.py"), same)
    commit_all(wt, wt)
    write(os.path.join(repo, "scripts", "app.py"), same)                  # 主干后来落了同样内容
    run(["git", "-C", repo, "add", "-A"], case)
    run(["git", "-C", repo, "commit", "-qm", "landed by other means"], case)
    sha = fr.sha256_file(os.path.join(repo, "scripts", "app.py"))       # 本单开工时主干就已经是这份
    ok, info = fr._commit_and_merge_worktree("C5b", repo, wt, ["scripts/app.py"], {"scripts/app.py": sha},
                                             kind="code", ledger_path=ledger)
    check("C5 分支成果主干早已有了 → 按已交付处理", ok is True and info.get("reason") == "main-target-sha-unchanged",
          json.dumps(info, ensure_ascii=False)[:500])


def c6(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "c6")
    wt = sandbox("C6", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n")
    sha = fr.sha256_file(os.path.join(repo, "scripts", "app.py"))
    orig = ww.merge
    ww.merge = lambda *a, **k: {"ok": False, "reason": "repo-busy", "id": "C6", "error": "index.lock 被占用", "conflicts": []}
    try:
        ok, info = fr._commit_and_merge_worktree("C6", repo, wt, ["scripts/app.py"], {"scripts/app.py": sha},
                                                 kind="code", ledger_path=ledger)
    finally:
        ww.merge = orig
    reason = info.get("reason") or (info.get("merge") or {}).get("reason")
    check("C6 合入阶段工厂侧原因原样透传", ok is False and reason == "repo-busy", json.dumps(info, ensure_ascii=False)[:500])
    check("C6 该原因在 MERGE_INFRA_REASONS 里", reason in fr.MERGE_INFRA_REASONS)


# ------------------------------------------------------------------ 证据
def e1(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "e1")
    ev = os.path.join(case, "e1", "ev")
    os.makedirs(ev, exist_ok=True)
    wt = sandbox("E1", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 9\nb = 2\nc = 3\nd = 4\ne = 5\n")
    commit_all(wt, wt)
    write(os.path.join(wt, "scripts", "new.py"), "".join("line %d\n" % i for i in range(300)))   # 新建未跟踪
    old = fr._EVIDENCE_PART_CHARS
    fr._EVIDENCE_PART_CHARS = 400
    try:
        res = fr.write_diff_evidence(wt, {}, ev, 1, main_repo=repo)
    finally:
        fr._EVIDENCE_PART_CHARS = old
    check("E1 已提交 + 新建文件都在证据里", sl(res.get("files")) == ["scripts/app.py", "scripts/new.py"], str(res.get("files")))
    idx = os.path.join(ev, "verify_diff_attempt1.index.md")
    check("E1 索引落盘", res.get("index_path") == idx and os.path.isfile(idx))
    parts = res.get("parts") or []
    check("E1 超长 diff 分成多段", len(parts) >= 2 and all(os.path.isfile(os.path.join(ev, p)) for p in parts), str(parts))
    body = "".join(read(os.path.join(ev, p)) for p in parts)
    check("E1 分段里有已提交的改动", "+a = 9" in body)
    check("E1 分段里有新建文件全文", "+line 299" in body and "==== FILE: scripts/new.py" in body)
    text = read(idx)
    check("E1 索引写明每段含哪些文件", "| `verify_diff_attempt1.part1.diff` |" in text and "scripts/new.py" in text and "scripts/app.py" in text)
    emb = res.get("embedded") or ""
    check("E1 简报内嵌写明「没内嵌 ≠ 没做」与分段位置", "没内嵌 ≠ 没做" in emb and "verify_diff_attempt1.part<k>.diff" in emb and "+a = 9" in emb)


def e2(case):
    ev = os.path.join(case, "e2", "ev")
    os.makedirs(ev, exist_ok=True)
    t = fr.embed_delivery_receipt(ev, 1, 0)
    check("E2 没写回执时如实说明", "没有写 `FACTORY_W_DELIVERY.md`" in t, t)
    p = os.path.join(ev, "FACTORY_W_DELIVERY.md")
    write(p, "# 回执\n原始输出贴这里\n")
    now_ms = time.time() * 1000
    check("E2 无法比对本轮开工时刻时如实标注", "未能比对本轮开工时刻" in fr.embed_delivery_receipt(ev, 1, 0))
    check("E2 本轮写的回执标「本轮回执」", "本轮回执" in fr.embed_delivery_receipt(ev, 1, now_ms - 60000))
    check("E2 更早轮次留下的回执如实标注", "更早轮次" in fr.embed_delivery_receipt(ev, 1, now_ms + 3600000))
    check("E2 回执正文进了简报", "原始输出贴这里" in fr.embed_delivery_receipt(ev, 1, now_ms - 60000))
    write(p, "x" * (fr._RECEIPT_EMBED_CHARS + 500))
    longt = fr.embed_delivery_receipt(ev, 1, now_ms - 60000)
    check("E2 超长回执截断时说真话", "此处只嵌前" in longt and len(longt) < fr._RECEIPT_EMBED_CHARS + 600, str(len(longt)))


# ------------------------------------------------------------------ 质检简报（内联代码块抽出来真跑）
def _brief_block_runner():
    """把 execute_job 里「质检简报装配」那一段源码原样抽出来，包成函数：任何笔误（NameError / 关键字错）在这里现形。

    模块级的名字（write_diff_evidence / atomic_write_text / REWRITE_NOTE_VERIFIER …）直接用 factory_runner 的真实模块命名空间；
    execute_job 的局部变量按用例传进去。
    """
    import ast
    import builtins
    lines = read(os.path.join(HERE, "factory_runner.py")).split("\n")
    s = next(i for i, ln in enumerate(lines) if ln.strip() == "if norm_kind == \"ui\" and os.path.exists(wt_target_abs):")
    e = next(i for i, ln in enumerate(lines) if "full brief on disk" in ln)
    block = "\n".join(ln[4:] if ln.startswith("    ") else ln for ln in lines[s:e + 1])
    src = ("def _blk(**ns):\n" + block
           + "\n    return {'prompt': v_prompt, 'brief': v_brief, 'level': _v_level, 'ev': _ev}\n")
    tree = ast.parse(src, "<verifier_brief_block>")

    def _scope(fn_node):
        """一层函数作用域自己绑定的名字 / 自己读的名字 / 只被 += 过的名字（不下钻嵌套函数）。"""
        bound, plain, read, aug = set(), set(), set(), set()
        args = fn_node.args
        bound |= {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        plain |= bound
        todo = [(c, fn_node) for c in ast.iter_child_nodes(fn_node)]
        while todo:
            n, parent = todo.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                bound.add(getattr(n, "name", ""))
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):     # 块里自己 import 的名字也是块里绑定的，不找调用方要
                for alias in n.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
                continue
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Store):
                    bound.add(n.id)
                    if isinstance(parent, ast.AugAssign):
                        aug.add(n.id)          # 只认本层的 +=：嵌套函数里的由它自己的作用域处理
                    else:
                        plain.add(n.id)
                else:
                    read.add(n.id)
            todo.extend((c, n) for c in ast.iter_child_nodes(n))
        return bound, plain, read, aug

    def _free(fn_node):
        bound, _plain, read, _aug = _scope(fn_node)
        out = set(read)
        for n in ast.walk(fn_node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn_node:
                out |= _free(n)
        return out - bound

    free = _free(tree.body[0])
    # 只被 += 过、本层没先赋过初值的名字（extra_telemetry）：必须由调用方传进来
    _b, _p, _r, _a = _scope(tree.body[0])
    free |= (_a - _p)
    stores = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            stores.add(n.id)
    params = sorted(k for k in free
                    if not hasattr(builtins, k) and k not in vars(fr) and k not in ("_ev_exc", "_rc_exc"))
    src = ("def _blk(%s):\n" % ", ".join(params)) + "\n".join("    " + ln for ln in src.split("\n")[1:])
    tree = ast.parse(src, "<verifier_brief_block>")
    left = {k for k in _free(tree.body[0]) if not hasattr(builtins, k) and k not in vars(fr)} - {"_ev_exc", "_rc_exc"}
    if left:
        raise AssertionError("简报段引用了 execute_job 里不存在的名字：%s" % sorted(left))
    exec(compile(src, "<verifier_brief_block>", "exec"), vars(fr))   # noqa: S102 - 跑的是本仓内核自己的代码
    return vars(fr)["_blk"], params


def _run_brief(ns):
    fn, params = _brief_block_runner()
    return fn(**{k: ns[k] for k in params})


def _brief_ns(case, tag, kind, spec_len):
    repo = make_repo(os.path.join(case, tag))
    cfg, ledger = ctx(os.path.join(case, tag), "wt")
    wt = sandbox(tag.upper(), repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 9\nb = 2\nc = 3\nd = 4\ne = 5\n")
    commit_all(wt, case)                                                   # 工人自己提交过
    write(os.path.join(wt, "scripts", "new.py"), "".join("line %d\n" % i for i in range(400)))   # 新建未跟踪
    ev = os.path.join(case, tag, "ev")
    os.makedirs(ev, exist_ok=True)
    write(os.path.join(ev, "FACTORY_W_DELIVERY.md"), "# 回执\npytest: 12 passed\n")
    targets = ["scripts/app.py", "scripts/new.py"]
    probe = fr.probe_code_delivery(wt, repo, targets, builder_rc=0)
    spec_file = os.path.join(case, tag, "spec.md")
    spec = "# 规格\n" + "".join("第 %d 条验收要求：页面要好看。\n" % i for i in range(spec_len))
    write(spec_file, spec)
    return {
        "repo_root": repo, "wt_path": wt, "ev_dir": ev, "attempt": 2, "order_id": tag.upper(),
        "job": {"spec_file": spec_file}, "targets": targets, "target_rel": targets[0],
        "wt_target_abs": os.path.join(wt, targets[0]), "kind": kind, "norm_kind": kind,
        "probe_res": probe, "extra_telemetry": "- 遥测：首屏 820ms\n", "w_res": {"start_ms": time.time() * 1000 - 60000},
        "probe_summary_obj": {k: v for k, v in probe.items() if k not in ("visible_text_preview", "cdp_telemetry_summary")},
        "spec_text": spec, "profile_block": "# 考卷模板\nQ1 信息去重\n", "profile_ref": "profiles/qc_ui.md",
        "v_role_label": "质检", "v4": {"closeout_gate": False}, "prior_items": [], "rw_state": "ok",
        "shots_list": "（无）", "spec_asserts": [], "v_brief_path": os.path.join(ev, "verify_brief_attempt2.md"),
    }


def v1(case):
    out = _run_brief(_brief_ns(case, "v1", "code", spec_len=20))
    text = read(os.path.join(case, "v1", "ev", "verify_brief_attempt2.md"))
    check("V1 全文简报落盘", out["level"] == 0 and out["prompt"] == out["brief"] and text == out["brief"],
          "level=%s len=%s" % (out["level"], len(text)))
    for key, needle in (("质检纪律（只许 Read/Grep）", "只允许调用 Read 和 Grep"),
                        ("「没内嵌 ≠ 没做」", "没内嵌 ≠ 没做"),
                        ("已提交的改动", "+a = 9"), ("新建文件全文", "+line 399"),
                        ("分段文件位置", "verify_diff_attempt2.part<k>.diff"),
                        ("施工回执正文", "pytest: 12 passed"), ("本轮回执标注", "本轮回执"),
                        ("证据文件清单", "订单规格全文"), ("最终 JSON 格式说明", "user_and_core_task"),
                        ("声明目标清单（全部目标）", "scripts/new.py")):
        check("V1 简报含：%s" % key, needle in text)
    check("V1 证据分段与索引已落盘", bool(out["ev"].get("parts")) and os.path.isfile(out["ev"].get("index_path") or ""),
          str(out["ev"].get("parts")))
    check("V1 最终 JSON 例子是合法 JSON", isinstance(json.loads(text.split("直接输出唯一一段完整标准 JSON 对象：`")[1].split("`")[0]), dict))


def v2(case):
    ns = _brief_ns(case, "v2", "code", spec_len=900)                 # 规格超长：命令行回落通道要缩减
    out = _run_brief(ns)
    prompt, brief = out["prompt"], out["brief"]
    sent = os.path.join(case, "v2", "ev", "verify_brief_attempt2.sent.md")
    check("V2 超长时命令行回落版不超上限", len(brief) > 20000 and len(prompt) <= 20000 and out["level"] >= 1,
          "level=%s brief=%s prompt=%s" % (out["level"], len(brief), len(prompt)))
    check("V2 缩减版如实写明是缩减版、全文在哪", "缩减版" in prompt and ns["v_brief_path"] in prompt)
    check("V2 缩减版仍带质检纪律与「没内嵌 ≠ 没做」", "只允许调用 Read 和 Grep" in prompt and "没内嵌 ≠ 没做" in prompt)
    check("V2 缩减版仍带 diff 证据开头与回执开头", "+a = 9" in prompt and "pytest: 12 passed" in prompt)
    check("V2 全文简报完整落盘（没被缩减）", read(ns["v_brief_path"]) == brief and "+line 399" in brief)
    check("V2 发出去的缩减版也留底", os.path.isfile(sent) and read(sent) == prompt)


def v3(case):
    # UI 单：目标文件 + DOM 快照 + 遥测摘要都要进简报；证据口径换成累计 diff 后同样有 diff 可看
    repo = make_repo(os.path.join(case, "v3"))
    cfg, ledger = ctx(os.path.join(case, "v3"), "wt")
    wt = sandbox("V3", repo, cfg, ledger)
    html = ("<html><head><style>.a{color:red}\n.b{margin:0}</style></head><body>\n"
            "<details><summary>订单区</summary>x</details><details><summary>告警区</summary>y</details>\n"
            "</body></html>\n")
    write(os.path.join(wt, "dashboard", "v2.html"), html)
    ev = os.path.join(case, "v3", "ev")
    os.makedirs(ev, exist_ok=True)
    dom = os.path.join(ev, "dom.html")
    write(dom, "<html><body><style>x{}</style><script>1</script><h1>看板</h1>\n\n<p>正文</p></body></html>")
    spec = "# 规格\n手机首屏要能看到订单。\n"
    out = _run_brief({
        "repo_root": repo, "wt_path": wt, "ev_dir": ev, "attempt": 1, "order_id": "V3",
        "job": {"spec_file": os.path.join(case, "v3", "spec.md")}, "targets": ["dashboard/v2.html"],
        "target_rel": "dashboard/v2.html", "wt_target_abs": os.path.join(wt, "dashboard", "v2.html"),
        "kind": "ui_dashboard", "norm_kind": "ui", "probe_res": {"checks": {}, "telemetry_path": os.path.join(ev, "t.json"),
                                                                "dom_dump_path": dom},
        "extra_telemetry": "- 三视口：200 / 390 / 1280 均正常\n", "w_res": {},
        "probe_summary_obj": {"gate": "probe_ui_dashboard", "all_passed": True, "failures": []},
        "spec_text": spec, "profile_block": "# 视觉审计考卷\n", "profile_ref": "profiles/qc_ui.md", "v_role_label": "视觉审计",
        "v4": {"closeout_gate": False}, "prior_items": [], "rw_state": "", "shots_list": "a.png, b.png", "spec_asserts": [],
        "v_brief_path": os.path.join(ev, "verify_brief_attempt1.md"),
    })
    text = out["brief"]
    check("V3 UI 简报带折叠栏摘要与 DOM 快照摘要", "订单区" in text and "告警区" in text and "真机渲染后 DOM 结构快照" in text
          and "<h1>看板</h1> <p>正文</p>" in text)
    check("V3 UI 简报也带累计改动证据（新建文件全文）", "++++ b/dashboard" in text or "+++ b/dashboard/v2.html（新建文件，全文）" in text,
          text[text.find("### 沙箱累计改动证据"):][:300])
    check("V3 UI 简报带 CSS 片段、遥测文件路径与实拍路径", "color:red" in text and "t.json" in text and "a.png" in text)
    check("V3 没写回执时如实说明", "工人没有写 `FACTORY_W_DELIVERY.md`" in text)


# ------------------------------------------------------------------ 调度
def e3(case):
    q = {"jobs": [{"job_id": "j1", "order_id": "E3", "status": "running"}], "history": []}
    fr.reconcile_queue_state(q)
    j = q["jobs"][0]
    check("E3 第一次崩溃后自愈重排（queued）", j["status"] == "queued" and j.get("requeued_reason") == "stale_running_recovered", json.dumps(j))
    for _ in range(fr.MAX_CRASH_REQUEUES):
        if j.get("status") != "queued":
            break
        j["status"] = "running"
        fr.reconcile_queue_state(q)
    check("E3 连续崩溃超过上限后转 failed 并出队列",
          not q["jobs"] and len(q["history"]) == 1 and q["history"][0]["status"] == "failed"
          and "停止自动重排" in q["history"][0].get("result_reason", ""), json.dumps(q, ensure_ascii=False)[:400])
    q2 = {"jobs": [{"job_id": "j2", "order_id": "E3b", "status": "running"}], "history": []}
    fr.reconcile_queue_state(q2, live_orders={"E3b"})
    check("E3 有活槽的在制单不动", q2["jobs"][0]["status"] == "running")


def use_test_ctx(cfg, ledger):
    """_prepare_worktree 走的是 worker_worktree 的默认配置/台账；换成用例自己的，绝不碰真实工作区根。"""
    st0, rm0, en0 = ww.status, ww.remove, ww.ensure
    books = {"E4": (cfg, ledger)}

    def _book(tid):
        return books.get(tid, (cfg, ledger))

    def _status(tid, repo="", cfg=None, ledger_path=None, **kw):
        _c, l = _book(tid)
        return st0(tid, repo=repo, ledger_path=ledger_path or l)

    def _remove(tid, repo="", cfg=None, ledger_path=None, **kw):
        _c, l = _book(tid)
        return rm0(tid, repo=repo, ledger_path=ledger_path or l, **kw)

    def _ensure(tid, repo="", cfg=None, ledger_path=None, **kw):
        c, l = _book(tid)
        return en0(tid, repo=repo, cfg=cfg or c, ledger_path=ledger_path or l, **kw)

    ww.status, ww.remove, ww.ensure = _status, _remove, _ensure
    return st0, rm0, en0, books


def e4(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "e4")
    wt = sandbox("E4", repo, cfg, ledger)
    shas = {"scripts/app.py": fr.sha256_file(os.path.join(repo, "scripts", "app.py")),
            "scripts/other.py": fr.sha256_file(os.path.join(repo, "scripts", "other.py"))}
    check("E4 空沙箱不算有成果", fr._sandbox_has_draft(wt, shas, branch="wt/E4", repo_root=repo) is False)
    write(os.path.join(wt, "scripts", "other.py"), "o = 2\n")          # 只改了第二个目标（很小的文件）
    check("E4 只有第二个目标改了也算有成果", fr._sandbox_has_draft(wt, shas, branch="wt/E4", repo_root=repo) is True)
    orig = use_test_ctx(cfg, ledger)
    try:
        books = orig[3]
        ok, path, branch = fr._prepare_worktree("E4", repo, target_rel="scripts/app.py",
                                                main_sha_before=shas["scripts/app.py"], preserve_if_modified=True,
                                                main_shas_before=shas)
        check("E4 重跑时复用沙箱而不是抹掉", ok is True and os.path.normcase(path) == os.path.normcase(wt)
              and read(os.path.join(path, "scripts", "other.py")) == "o = 2\n", "%s %s" % (path, branch))
        write(os.path.join(wt, "scripts", "other.py"), "o = 1\n")          # 还原：目标文件都与主干一致
        write(os.path.join(wt, "scripts", "brand.py"), "b = 1\n")          # 但有已提交的新成果
        commit_all(wt, wt)
        check("E4 目标之外有已提交成果也算有成果", fr._sandbox_has_draft(wt, shas, branch="wt/E4", repo_root=repo) is True)
        # 换一个干净沙箱：目标没动、分支没提交、只有施工垃圾 → 判定为空沙箱，重跑时清掉重来
        cfg2, ledger2 = ctx(case, "e4b")
        books["E4b"] = (cfg2, ledger2)
        wt2 = sandbox("E4b", repo, cfg2, ledger2)
        write(os.path.join(wt2, "build.log"), "junk\n")
        ok2, path2, branch2 = fr._prepare_worktree("E4b", repo, target_rel="scripts/app.py",
                                                   main_sha_before=shas["scripts/app.py"], preserve_if_modified=True,
                                                   main_shas_before={"scripts/app.py": shas["scripts/app.py"]})
        check("E4 真正的空沙箱重跑时清掉重来", ok2 is True and os.path.normcase(path2) == os.path.normcase(wt2)
              and not os.path.exists(os.path.join(path2, "build.log"))
              and read(os.path.join(path2, "scripts", "app.py")) == APP, "%s %s" % (path2, branch2))
    finally:
        ww.status, ww.remove, ww.ensure = orig[0], orig[1], orig[2]


def e5(case):
    repo = make_repo(case)
    cfg, ledger = ctx(case, "e5")
    wt = sandbox("E5", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 7\nb = 2\nc = 3\nd = 4\ne = 5\n")
    commit_all(wt, wt)                                                # 工人自己提交
    probe = fr.probe_code_delivery(wt, repo, ["scripts/app.py"], builder_rc=0)
    check("E5 工人自提交过 Gate1", probe.get("all_passed") is True, json.dumps(probe.get("failures"), ensure_ascii=False))
    ch = probe.get("checks") or {}
    check("E5 累计口径里标出已提交", ch.get("cumulative", {}).get("committed_files") == ["scripts/app.py"]
          and bool(ch.get("files_changed", [{}])[0].get("committed")), json.dumps(ch.get("cumulative"), ensure_ascii=False))


# ------------------------------------------------------------------ 导入封闭 + 回归镜像
def h1(case):
    """同仓模块必须取 factory_runner 自己旁边那份。把内核拷到别处、旁边另造一个「活仓」诱饵（只放旧模块），
    再把内核里写死的活仓根改指诱饵——导入到的仍须是自己那份。2026-11-25 AA 镜像回归假红即此：镜像里的新内核
    配的是活仓旧合并模块，待上线代码从没被测到。"""
    copy = os.path.join(case, "copy")
    decoy = os.path.join(case, "decoy")
    os.makedirs(os.path.join(copy, "scripts"), exist_ok=True)
    for fn in os.listdir(HERE):
        if fn.endswith(".py"):
            shutil.copy2(os.path.join(HERE, fn), os.path.join(copy, "scripts", fn))
    # 替换串用函数给：诱饵路径里的反斜杠不能被当成正则转义
    text, n = re.subn(r'^STEWARD_ROOT = r"[^"]*"', lambda _m: 'STEWARD_ROOT = r"%s"' % decoy,
                      read(os.path.join(HERE, "factory_runner.py")), count=1, flags=re.M)
    assert n == 1, "内核里没找到写死的 STEWARD_ROOT"
    write(os.path.join(copy, "scripts", "factory_runner.py"), text)
    for mod in ("worker_worktree.py", "ai_store.py", "slot_ledger.py"):
        write(os.path.join(decoy, "scripts", mod), "DECOY = True\n")
    probe = ("import sys, json; sys.path.insert(0, sys.argv[1]); import factory_runner as fr; import worker_worktree as ww; "
             "import ai_store, slot_ledger; print(json.dumps({'fr': fr.__file__, 'ww': ww.__file__, 'ai': ai_store.__file__, "
             "'slot': slot_ledger.__file__, 'protect': hasattr(ww, 'protect_live_edits'), 'scripts_dir': fr.SCRIPTS_DIR}))")
    env = dict(os.environ, STEWARD_STATE_DIR=os.path.join(case, "state"), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", probe, os.path.join(copy, "scripts")],
                       cwd=case, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert r.returncode == 0, "probe failed:\n%s\n%s" % (r.stdout, r.stderr[-1500:])
    got = json.loads(r.stdout.strip().splitlines()[-1])
    own = os.path.normcase(os.path.join(copy, "scripts"))
    same = lambda p: os.path.normcase(os.path.dirname(os.path.abspath(p))) == own  # noqa: E731
    check("H1 内核在别的目录运行时，合并模块取自己旁边那份", same(got["ww"]), got["ww"])
    check("H1 新合并模块确实被导入", got["protect"] is True, str(got["protect"]))
    check("H1 ai_store / slot_ledger 同样取自己旁边那份", same(got["ai"]) and same(got["slot"]), "%s | %s" % (got["ai"], got["slot"]))
    check("H1 SCRIPTS_DIR 指向内核自己所在目录", os.path.normcase(got["scripts_dir"]) == own, got["scripts_dir"])


def h2(case):
    """回归镜像：沙箱现状镜像与主干归档必须同料——取证样本要带（test_mes_core 用它），块头不带，.git 不带。"""
    repo = make_repo(case)
    keep = ("reports/evidence/C-1/demo_state/mes/events.jsonl", "reports/evidence/C-1/x.md", "scripts/app.py")
    drop = ("state/db-backup-20261125/db.sqlite", "reports/log_packs/a.zip", "state/packages/p.zip")
    for rel in keep + drop:
        write(os.path.join(repo, rel), "x\n")
    run(["git", "-C", repo, "add", "-A"], case)
    run(["git", "-C", repo, "commit", "-qm", "bulk"], case)
    write(os.path.join(repo, "reports/evidence/C-1/untracked.md"), "u\n")
    for tag, sub, kwargs in (("现状", "wt", {}), ("归档", "arc", {"archive_of": "HEAD"})):
        dest = os.path.join(case, "mirror_" + sub)
        fr._make_mirror(repo, dest, **kwargs)
        has = lambda rel: os.path.isfile(os.path.join(dest, rel.replace("/", os.sep)))  # noqa: E731
        check("H2 %s镜像带上取证样本" % tag, all(has(r) for r in keep), str([r for r in keep if not has(r)]))
        check("H2 %s镜像不带库备份/日志包/套装包" % tag, not any(has(r) for r in drop), str([r for r in drop if has(r)]))
        check("H2 %s镜像不带 .git" % tag, not os.path.exists(os.path.join(dest, ".git")))
        if not kwargs:
            check("H2 现状镜像带上未跟踪的新文件（沙箱里新建的）", has("reports/evidence/C-1/untracked.md"))


# ------------------------------------------------------------------ V5.0 第二批：轮次表 / 越界核对 / 带种 / 外来工作区 / 按目标定仓 / 补合入
def n1(case):
    """轮次表可以中途延长；命令行附加字段；路径占用不区分大小写。"""
    rounds = fr._Rounds(2)
    seen = []
    for i in rounds:
        seen.append(i)
        if i == 2:
            rounds.extend(1)
    check("N1 轮次表按计划走完、中途延长的那一轮确实跑到", seen == [1, 2, 3] and list(fr._Rounds(3)) == [1, 2, 3] and list(fr._Rounds(0)) == [],
          str(seen))
    meta = fr._extra_meta("G1R2", "G9;G10, G11", True)
    check("N1 命令行附加字段：带种 / 前置 / 双门直通", meta == {"seed_from": "G1R2", "depends_on": ["G9", "G10", "G11"], "seed_gates": True},
          json.dumps(meta, ensure_ascii=False))
    check("N1 没给就不带；没带种时双门直通不生效", fr._extra_meta("", "", False) is None and fr._extra_meta("", "G9", True) == {"depends_on": ["G9"]})
    import admit_v5
    hit = admit_v5.claim_conflicts({"order_id": "A", "targets": ["Dashboard/v2.html", "scripts\\v3_data.py"]},
                                   [{"order_id": "B", "targets": ["dashboard/v2.html"]}, {"order_id": "C", "targets": ["scripts/v3_data.py"]}])
    check("N1 路径占用：大小写 / 斜杠写法不同也算同一条路径", len(hit) == 2 and "B 占用 dashboard/v2.html" in hit[0]
          and "C 占用 scripts/v3_data.py" in hit[1], str(hit))
    check("N1 路径占用：同一张单不算冲突", admit_v5.claim_conflicts(
        {"order_id": "A", "targets": ["dashboard/v2.html"]}, [{"order_id": "A", "targets": ["dashboard/v2.html"]}]) == [])


def o1(case):
    """专家越界核对：只认源码路径；快照前就有的手改、运行态目录、期间别的道合入改到的文件都不动。"""
    repo = make_repo(case)
    write(os.path.join(repo, "docs", "readme.md"), "hand edit before\n")          # 快照前就带着的手改
    before = fr._live_tree_snapshot(repo)
    check("O1 快照记下了 HEAD 与已有的手改", bool(before["head"]) and "docs/readme.md" in before["tracked"], json.dumps(before, ensure_ascii=False)[:200])
    write(os.path.join(repo, "scripts", "app.py"), "a = 1\nb = 999\n")            # 越界：改了跟踪的源码（出场前是干净的）
    write(os.path.join(repo, "scripts", "sneaky.py"), "S = 1\n")                  # 越界：活仓里新建文件
    write(os.path.join(repo, "docs", "readme.md"), "hand edit changed\n")         # 出场前就脏的那个：期间又变了
    write(os.path.join(repo, "state", "queue.json"), "{\"a\": 1}\n")              # 运行态：不算
    write(os.path.join(repo, "state", "new_runtime.json"), "{}\n")               # 运行态新文件：不算
    ev = os.path.join(case, "ev")
    out = fr._expert_overreach_check(repo, before, ev, "O1")
    check("O1 越界改的跟踪源码已恢复到 HEAD", out["restored"] == ["scripts/app.py"] and out["restore_failed"] == []
          and read(os.path.join(repo, "scripts", "app.py")) == APP, json.dumps(out, ensure_ascii=False)[:300])
    check("O1 越界新建的文件挪进了证据目录", out["moved"] == ["scripts/sneaky.py"] and not os.path.exists(os.path.join(repo, "scripts", "sneaky.py"))
          and read(os.path.join(ev, "expert_overreach", "scripts", "sneaky.py")) == "S = 1\n")
    check("O1 运行态目录的写入不算越界", read(os.path.join(repo, "state", "queue.json")) == "{\"a\": 1}\n"
          and os.path.isfile(os.path.join(repo, "state", "new_runtime.json")))
    check("O1 出场前就带着手改的文件：只留证，不替人回滚", out["changed_during"] == ["docs/readme.md"]
          and read(os.path.join(repo, "docs", "readme.md")) == "hand edit changed\n"
          and read(os.path.join(ev, "expert_overreach", "docs", "readme.md")) == "hand edit changed\n")
    check("O1 这一轮真有越界痕迹 → 记命中", list(fr._overreach_hits({repo: out})) == [repo])
    # 期间别的道合入主干：合入改到的文件不算越界，也不算命中
    before2 = fr._live_tree_snapshot(repo)
    write(os.path.join(repo, "scripts", "other.py"), "o = 2\n")
    run(["git", "-C", repo, "add", "-A"], case)
    run(["git", "-C", repo, "commit", "-qm", "lane merge"], case)
    out2 = fr._expert_overreach_check(repo, before2, ev, "O1")
    check("O2 别的道合入主干改到的文件不算越界", out2["restored"] == [] and out2["moved"] == [] and out2["changed_during"] == []
          and len(out2["commits_during"]) == 1 and "lane merge" in out2["commits_during"][0]
          and read(os.path.join(repo, "scripts", "other.py")) == "o = 2\n", json.dumps(out2, ensure_ascii=False)[:300])
    check("O2 期间只有别的道合入 → 不报专家越界", fr._overreach_hits({repo: out2}) == {} and fr._overreach_hits({}) == {})
    check("O2 仓库没了 / 快照读不了 → 什么都不动", fr._expert_overreach_check(os.path.join(case, "nope"), before2, ev, "O1")["restored"] == []
          and fr._expert_overreach_check(repo, {"error": "x"}, ev, "O1")["restored"] == [])
    check("O2 越界记录为空时不报警", fr._expert_overreach_check(repo, fr._live_tree_snapshot(repo), ev, "O1")["restored"] == [])
    # 别的道正在主仓里合并、冲突还没收口：越界的文件只留证不动手；未决路径 / 缓存文件本身不算越界
    main_branch = ww.head_branch(repo)
    run(["git", "-C", repo, "checkout", "-q", "-b", "feat"], case)
    write(os.path.join(repo, "scripts", "other.py"), "o = 100\n")
    commit_all(repo, case, "feat work")
    run(["git", "-C", repo, "checkout", "-q", main_branch], case)
    write(os.path.join(repo, "scripts", "other.py"), "o = 101\n")
    commit_all(repo, case, "main work")
    before3 = fr._live_tree_snapshot(repo)
    run(["git", "-C", repo, "merge", "feat"], case, must=False)
    write(os.path.join(repo, "scripts", "app.py"), "a = 1\nb = 777\n")
    write(os.path.join(repo, "scripts", "__pycache__", "app.cpython-313.pyc"), "x")
    out3 = fr._expert_overreach_check(repo, before3, ev, "O1")
    check("O3 主仓合并没收口时：越界文件只留证不动手，内容原样还在、照样记命中", out3["untouched"] == ["scripts/app.py"]
          and out3["restored"] == [] and read(os.path.join(repo, "scripts", "app.py")) == "a = 1\nb = 777\n"
          and read(os.path.join(ev, "expert_overreach", "scripts", "app.py")) == "a = 1\nb = 777\n"
          and list(fr._overreach_hits({repo: out3})) == [repo], json.dumps(out3, ensure_ascii=False)[:300])
    check("O3 未决的冲突路径 / 新生成的缓存文件不算越界", "scripts/other.py" not in out3["untouched"] and out3["moved"] == []
          and os.path.isfile(os.path.join(repo, "scripts", "__pycache__", "app.cpython-313.pyc"))
          and read(os.path.join(repo, "scripts", "other.py")) != "o = 2\n")
    run(["git", "-C", repo, "merge", "--abort"], case)
    out4 = fr._expert_overreach_check(repo, before3, ev, "O1")
    check("O3 收口之后再核对：越界文件正常还原", out4["restored"] == ["scripts/app.py"] and out4["untouched"] == []
          and read(os.path.join(repo, "scripts", "app.py")) == APP, json.dumps(out4, ensure_ascii=False)[:300])


def s1(case):
    """带种重下：并得拢 / 并不拢 / 没东西可带 / 上代分支没了，四种情形都如实交代。"""
    repo = make_repo(case)
    cfg, ledger = ctx(case, "s1")
    wt = sandbox("S1", repo, cfg, ledger)
    info = fr._seed_sandbox(wt, repo, "NOPE")
    check("S1 上代分支不存在 → 如实说明，沙箱不动", info["ok"] is False and "不存在" in info["reason"]
          and run(["git", "-C", wt, "status", "--porcelain"], case).strip() == "", json.dumps(info, ensure_ascii=False))
    prev = sandbox("PREV", repo, cfg, ledger)
    info = fr._seed_sandbox(wt, repo, "PREV")
    check("S1 上代分支没有新提交 → 无需带入", info["noop"] is True and info["ok"] is True, json.dumps(info, ensure_ascii=False))
    write(os.path.join(prev, "scripts", "other.py"), "o = 7\n")
    commit_all(prev, case, "prev work")
    info = fr._seed_sandbox(wt, repo, "PREV")
    block = fr._seed_brief_block(info, wt)
    check("S1 并得拢 → 沙箱多了一笔带种提交、上代内容带进来了", info["ok"] is True and info["noop"] is False
          and info["commit"] == ww.head_commit(wt) and read(os.path.join(wt, "scripts", "other.py")) == "o = 7\n", json.dumps(info, ensure_ascii=False))
    check("S1 派工单说明段：并得拢时写明上一代在哪、不要推翻重做", "上一代成果已带进沙箱" in block and "wt/PREV" in block and "不要推翻重做" in block, block[:200])
    check("S1 没带种 / 主干已含 → 派工单不出这一段", fr._seed_brief_block({}, wt) == "" and fr._seed_brief_block({"ok": True, "noop": True}, wt) == "")
    # 并不拢：主干与上一代改了同一行（新沙箱从新主干起）
    write(os.path.join(repo, "scripts", "other.py"), "o = 9\n")
    run(["git", "-C", repo, "add", "-A"], case)
    run(["git", "-C", repo, "commit", "-qm", "main moved"], case)
    wt2 = sandbox("S2", repo, cfg, ledger)
    write(os.path.join(prev, "scripts", "other.py"), "o = 8\n")
    commit_all(prev, case, "prev work 2")
    info = fr._seed_sandbox(wt2, repo, "PREV")
    block = fr._seed_brief_block(info, wt2)
    check("S2 并不拢 → 回退到干净主干、点名冲突文件", info["ok"] is False and info["conflicts"] == ["scripts/other.py"]
          and read(os.path.join(wt2, "scripts", "other.py")) == "o = 9\n"
          and run(["git", "-C", wt2, "status", "--porcelain"], case).strip() == "", json.dumps(info, ensure_ascii=False))
    check("S2 派工单说明段：有冲突时让工人先并在沙箱解决", "需要你先并进沙箱" in block and "git merge wt/PREV" in block and "scripts/other.py" in block, block[:300])


def w1(case):
    """别的仓留下的同名目录：不收养、不往本仓合；本仓自己的照常认。"""
    repo_a = make_repo(case)
    repo_b = os.path.join(case, "repo_b")
    write(os.path.join(repo_b, "scripts", "app.py"), APP)
    run(["git", "init", "-q", repo_b], case)
    run(["git", "-C", repo_b, "config", "user.email", "t@example.com"], case)
    run(["git", "-C", repo_b, "config", "user.name", "t"], case)
    run(["git", "-C", repo_b, "config", "core.autocrlf", "false"], case)
    run(["git", "-C", repo_b, "add", "-A"], case)
    run(["git", "-C", repo_b, "commit", "-qm", "base b"], case)
    cfg, ledger = ctx(case, "w1")
    foreign = os.path.join(cfg["root"], "W1")
    os.makedirs(cfg["root"], exist_ok=True)
    run(["git", "-C", repo_b, "worktree", "add", "-q", "-b", "wt/W1", foreign], case)
    check("W1 前提：那个目录是 B 仓的工作区，不是 A 仓的", ww.worktree_belongs(repo_b, foreign)[0] is True and ww.worktree_belongs(repo_a, foreign)[0] is False)
    res = ww.ensure("W1", repo_a, cfg=cfg, ledger_path=ledger)
    check("W1 别的仓留下的同名目录不被收养（当场拒收）", res.get("ok") is False and res.get("reason") == "worktree-foreign-repo" and os.path.isdir(foreign),
          json.dumps(res, ensure_ascii=False)[:300])
    with open(ledger, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "worktrees": {"W1": {"id": "W1", "repo": repo_a.replace("\\", "/"), "path": foreign.replace("\\", "/"),
                                                         "branch": "wt/W1"}}}, f)
    check("W1 台账里有它 → 复用那一支同样拒收", ww.ensure("W1", repo_a, cfg=cfg, ledger_path=ledger).get("reason") == "worktree-foreign-repo")
    merged = ww.merge("W1", repo_a, ledger_path=ledger)
    check("W1 台账说它是 A 仓的沙箱、实际不是 → 拒绝往 A 仓合", merged.get("ok") is False and merged.get("reason") == "worktree-foreign-repo"
          and read(os.path.join(repo_a, "scripts", "app.py")) == APP, json.dumps(merged, ensure_ascii=False)[:300])
    own = ww.ensure("W2", repo_a, cfg=cfg, ledger_path=ledger)
    check("W1 本仓自己的沙箱照常认、照常复用", own.get("ok") is True and ww.worktree_belongs(repo_a, own["path"])[0] is True
          and ww.ensure("W2", repo_a, cfg=cfg, ledger_path=ledger).get("reused") is True)
    os.remove(ledger)                       # 台账丢了：本仓自己的工作区照常认领回来
    back = ww.ensure("W2", repo_a, cfg=cfg, ledger_path=ledger)
    check("W1 台账丢了的本仓工作区照常认领", back.get("ok") is True and back.get("adopted") is True and back.get("path") == own["path"])


def t1(case):
    """按目标定仓：不写 --repo 时看目标文件实际在哪个仓，不再让活仓的 code 单默认进档案仓。"""
    live, arc = fr.STEWARD_ROOT, fr.BP_ROOT
    write(os.path.join(live, "scripts", "x_live.py"), "x = 1\n")
    write(os.path.join(arc, "outputs", "r.md"), "# r\n")
    norm = os.path.normcase
    check("T1 显式给了仓就用显式的", norm(fr.route_repo_by_targets(["scripts/x_live.py"], "code", arc)) == norm(os.path.abspath(arc)))
    check("T1 目标只在活仓 → 活仓", norm(fr.route_repo_by_targets(["scripts/x_live.py"], "code")) == norm(live))
    check("T1 目标只在档案仓 → 档案仓", norm(fr.route_repo_by_targets(["outputs/r.md"], "ui")) == norm(arc))
    write(os.path.join(arc, "scripts", "both.py"), "b = 1\n")
    write(os.path.join(live, "scripts", "both.py"), "b = 1\n")
    check("T1 两个仓都有 / 都没有 → 退回老规矩（UI=活仓，其余=档案仓）", norm(fr.route_repo_by_targets(["scripts/both.py"], "code")) == norm(arc)
          and norm(fr.route_repo_by_targets(["scripts/both.py"], "ui")) == norm(live)
          and norm(fr.route_repo_by_targets(["nope/zzz.py"], "code")) == norm(arc))
    spec = os.path.join(case, "spec.md")
    write(spec, "# 规格\n")
    job = fr.enqueue_job("T1", "code", "路由", spec, "", 2, targets=["scripts/x_live.py"])
    job2 = fr.enqueue_job("T1b", "code", "路由", spec, "", 2, targets=["scripts/x_live.py"], repo=arc)
    check("T1 入库：目标在活仓的 code 单进活仓；写了 --repo 的仍以 --repo 为准", norm(job.get("repo")) == norm(live)
          and norm(job2.get("repo")) == norm(os.path.abspath(arc)), "%s | %s" % (job.get("repo"), job2.get("repo")))
    for _f in ("x_live.py", "both.py"):
        os.remove(os.path.join(live, "scripts", _f))
    os.remove(os.path.join(arc, "scripts", "both.py"))


def r1(case):
    """补合入命令：质检已过、只是没合进去的单重新排队，只补双门 + 合入。"""
    repo = make_repo(case)
    cfg, ledger = ctx(case, "r1")
    wt = sandbox("G9", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n")
    commit_all(wt, case, "verified work")
    old = {"job_id": "G9_1", "order_id": "G9", "status": "needs_human", "needs_reason": "merge_infra", "kind": "code", "title": "补合入测试",
           "targets": ["scripts/app.py"], "repo": repo, "spec_file": os.path.join(case, "spec.md"), "max_attempts": 2,
           "disposition": "转驻厂专家", "result_reason": "x", "finished_at": "2026-11-25 20:00:00", "merge_requeues": 2}

    def queue():
        return json.load(open(fr.QUEUE_PATH, encoding="utf-8"))

    def call(oid, reason=""):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fr.cmd_remerge(oid, reason)
        return rc

    fr.atomic_write_json(fr.QUEUE_PATH, {"jobs": [], "history": [old]})
    rc = call("G9", "测试补合入")
    j = queue()["jobs"][0]
    check("R1 补合入：重新排进队列、只补双门 + 合入", rc == 0 and len(queue()["jobs"]) == 1 and j["status"] == "queued"
          and j["resume_at"] == "gates" and j["job_id"] != "G9_1" and j["remerge_of"] == "G9_1" and j["order_id"] == "G9",
          json.dumps({k: j.get(k) for k in ("status", "resume_at", "job_id", "remerge_of", "order_id")}, ensure_ascii=False))
    check("R1 上一轮的终态字段不带进新单，目标 / 规格 / 仓照旧", all(k not in j for k in ("needs_reason", "disposition", "result_reason", "finished_at", "merge_requeues"))
          and j["remerge_reason"] == "测试补合入" and j["targets"] == ["scripts/app.py"] and j["repo"] == repo)
    check("R1 这张单还在队列里 → 不重复排", call("G9") == 1 and len(queue()["jobs"]) == 1)
    fr.atomic_write_json(fr.QUEUE_PATH, {"jobs": [], "history": [dict(old, order_id="G8", job_id="G8_1")]})
    check("R1 沙箱分支已不在 → 拒绝，队列不动", call("G8") == 1 and queue()["jobs"] == [])
    check("R1 历史里没有这张单 → 拒绝", call("NOPE") == 1 and queue()["jobs"] == [])


def k1(case):
    """单道也过进场官：前置没交付 / 专家停单的单留在队列并写明原因，条件解除后照常开工。"""
    repo = make_repo(case)
    spec = os.path.join(case, "spec.md")
    write(spec, "# 规格\n")
    fr.enqueue_job("KA", "code", "前置单", spec, "", 2, targets=["scripts/app.py"], repo=repo, extra_meta={"depends_on": ["DEP1"]})
    fr.enqueue_job("KB", "code", "专家停单", spec, "", 2, targets=["scripts/app.py"], repo=repo, extra_meta={"expert_hold": "等厂里裁决"})
    started = []
    orig = (fr._slots_enabled, fr.execute_job)
    fr._slots_enabled = lambda: False

    def fake_exec(q, job):                  # 假内核：记一笔、把这张单按交付出队（真内核也是自己写队列）
        started.append(job["order_id"])
        qc = json.load(open(fr.QUEUE_PATH, encoding="utf-8"))
        qc["jobs"] = [j for j in qc["jobs"] if j["job_id"] != job["job_id"]]
        qc["history"] = (qc.get("history") or []) + [dict(job, status="done")]
        fr.atomic_write_json(fr.QUEUE_PATH, qc)
        return {"final_status": "done"}

    fr.execute_job = fake_exec

    def tick():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fr.cmd_tick()
        return rc, buf.getvalue()

    def rows():
        return {j["order_id"]: j for j in json.load(open(fr.QUEUE_PATH, encoding="utf-8"))["jobs"]}

    try:
        rc, out = tick()
        r = rows()
        check("K1 单道：前置没交付 / 专家停单 → 都留在队列不派工，原因写回给面板", rc == 0 and started == [] and '"HELD"' in out
              and r["KA"]["status"] == "queued" and r["KA"].get("hold_reason") == "前置未交付：DEP1"
              and r["KB"]["status"] == "queued" and r["KB"].get("hold_reason") == "专家停单：等厂里裁决", out[:300])
        q = json.load(open(fr.QUEUE_PATH, encoding="utf-8"))
        q["history"] = [{"order_id": "DEP1", "job_id": "DEP1_1", "status": "needs_human"}]
        fr.atomic_write_json(fr.QUEUE_PATH, q)
        rc, out = tick()
        check("K1 前置单转 needs_human 不算交付 → 仍留着", rc == 0 and started == [] and '"HELD"' in out)
        q["history"] = [{"order_id": "DEP1", "job_id": "DEP1_1", "status": "done"}]
        fr.atomic_write_json(fr.QUEUE_PATH, q)
        rc, out = tick()
        qc = json.load(open(fr.QUEUE_PATH, encoding="utf-8"))
        done_ka = [h for h in qc.get("history") or [] if h.get("order_id") == "KA"]
        check("K1 前置交付 → 照常开工、留队原因撤掉；专家停单的那张继续留着", rc == 0 and started == ["KA"] and len(done_ka) == 1
              and "hold_reason" not in done_ka[0] and "KA" not in rows()
              and rows()["KB"].get("hold_reason") == "专家停单：等厂里裁决", "%s | %s" % (started, out[:200]))
        rc, out = tick()
        check("K1 单道一次只开一张；另一张还停着 → 这一拍空转不报错", rc == 0 and started == ["KA"] and '"HELD"' in out, out[:200])
        q = json.load(open(fr.QUEUE_PATH, encoding="utf-8"))
        q["jobs"] = [j for j in q["jobs"] if j["order_id"] == "KB"]
        q["jobs"][0].pop("expert_hold")
        fr.atomic_write_json(fr.QUEUE_PATH, q)
        rc, out = tick()
        check("K1 专家放行 → 开工", rc == 0 and started == ["KA", "KB"] and rows() == {}, str(started))
    finally:
        fr._slots_enabled, fr.execute_job = orig


def g2(case):
    """硬闸把话说清楚 + 回归用例随口径同步（2026-11-26 G47R4 第 1 轮踩出来的坑）。"""
    import run_tests as rt

    # G2a 断点行：用例自己打印的 [FAIL] 行优先；没有就退回末尾 6 行
    fl = rt.fail_lines(("[PASS] L1 ok\r\n[PASS] L2 ok\r\n[FAIL] L6 未归属 ({'used': 0}, 9)\r\n26 passed / 27 total\r\n").encode("utf-8"))
    check("G2a 断点行只挑 [FAIL] 行（多字节不坏）", fl == ["[FAIL] L6 未归属 ({'used': 0}, 9)"], repr(fl))
    fl = rt.fail_lines("Traceback (most recent call last):\n  File x\nAssertionError: boom\n")
    check("G2a 没打印 [FAIL] 的崩溃也挑得出来", fl == ["Traceback (most recent call last):", "AssertionError: boom"], repr(fl))
    fl = rt.fail_lines("a\nb\nc\nd\ne\nf\ng\nh\n")
    check("G2a 没有断点行时退回末尾 6 行", fl == ["c", "d", "e", "f", "g", "h"], repr(fl))
    check("G2a 空输出不抛错", rt.fail_lines(None) == [] and rt.fail_lines(b"") == [])

    # G2b 套件汇总：失败用例带 fails → _run_regress_suite 把断点明细带出来
    mir = os.path.join(case, "mirror")
    write(os.path.join(mir, "scripts", "run_tests.py"), (
        "import json\nprint('FP x')\nprint('[FAIL] test_x rc=2 0.1s')\n"
        "print(json.dumps({'cases': [{'case': 'test_x', 'rc': 2, 'status': 'FAIL', 'fails': ['[FAIL] L6 something (1, 2)']},"
        " {'case': 'test_y', 'rc': 0, 'status': 'PASS'}]}))\nraise SystemExit(2)\n"))
    rr = fr._run_regress_suite(mir)
    check("G2b 套件结果：失败用例名 + 断点明细",
          rr.get("ran") is True and rr.get("failed") == ["test_x"] and rr.get("passed") == 1
          and rr.get("detail") == {"test_x": ["[FAIL] L6 something (1, 2)"]}, json.dumps(rr, ensure_ascii=False)[:300])
    write(os.path.join(mir, "scripts", "run_tests.py"),
          "import json\nprint(json.dumps({'cases': [{'case': 'test_y', 'rc': 0, 'status': 'PASS'}]}))\n")
    rr = fr._run_regress_suite(mir)
    check("G2b 全绿时没有 detail", rr.get("failed") == [] and "detail" not in rr, json.dumps(rr)[:200])

    # G2c 用例名 → 文件
    files = fr._regress_case_files(["test_lane_status", "test_board_concurrency", "test_nope"])
    check("G2c 用例名换成仓内路径（登记过的查 CASES，没登记的按约定）",
          files == ["scripts/test_lane_status.py", "scripts/tests/test_board_concurrency.py", "scripts/test_nope.py"], str(files))
    check("G2c 只认回归套件自己的用例文件",
          fr._is_regress_test_path("scripts/test_lane_status.py") and fr._is_regress_test_path("scripts\\tests\\test_gantt.py")
          and not fr._is_regress_test_path("scripts/app.py") and not fr._is_regress_test_path("tests/test_x.py")
          and not fr._is_regress_test_path("scripts/test_x.txt") and not fr._is_regress_test_path("scripts/tests/x.py"), "")
    _keep = fr.__file__
    fr.__file__ = os.path.join(case, "nowhere", "scripts", "factory_runner.py")   # 本文件位置被改指（AA 夹具会这样）：换算不该受影响
    try:
        files2 = fr._regress_case_files(["test_lane_status", "test_board_concurrency"])
    finally:
        fr.__file__ = _keep
    check("G2c 内核文件位置被改指也不影响换算", files2 == ["scripts/test_lane_status.py", "scripts/tests/test_board_concurrency.py"],
          str(files2))

    # G2d 返工话 / 范围声明
    probe = {"strict_checks": {"kernel_tests": {"new_failed": ["test_lane_status"], "detail": {
        "test_lane_status": ["[FAIL] L6 未归属单独列出且计入会话总数 ({'used': 0, 'pairs': []}, 9)"]}}}}
    lines, files = fr._regress_rework_lines(probe)
    text = "\n".join(lines)
    check("G2d 返工话带断点明细", "断点 `test_lane_status`：[FAIL] L6 未归属单独列出且计入会话总数" in text, text[:300])
    check("G2d 返工话点名允许同步的用例文件",
          files == ["scripts/test_lane_status.py"] and "`scripts/test_lane_status.py`" in text
          and "仅此豁免越界" in text and "不许删弱" in text and "run_tests.py" in text, text[-420:])
    check("G2d 没被硬闸拦下 → 不添话",
          fr._regress_rework_lines({"strict_checks": {"kernel_tests": {"new_failed": []}}}) == ([], [])
          and fr._regress_rework_lines({}) == ([], []))
    cl = fr._sync_clause_of(text)
    check("G2d 硬闸点名过 → 范围声明留出口", "回归用例文件可以同步修改" in cl, cl)
    check("G2d 没点名过 → 范围声明原样",
          fr._sync_clause_of("- [Gate1 机检 FAIL] 声明目标零改动") == "" and fr._sync_clause_of("") == "")

    # G2e Gate1 分类：改了内核 + 同步了用例 → 不算越界、单列；没改内核 → 用例文件照旧算越界
    suite = "import json\nprint(json.dumps({'cases': [{'case': 'test_lane_status', 'rc': 0, 'status': 'PASS'}]}))\n"

    def kernel_repo(tag):
        repo = make_repo(os.path.join(case, tag))
        write(os.path.join(repo, "scripts", "factory_runner.py"), "# kernel\n")
        write(os.path.join(repo, "scripts", "run_tests.py"), suite)
        write(os.path.join(repo, "scripts", "lane_status.py"), "x = 1\n")
        write(os.path.join(repo, "scripts", "test_lane_status.py"), "assert True\n")
        run(["git", "-C", repo, "add", "-A"], case)
        run(["git", "-C", repo, "commit", "-qm", "kernel"], case)
        return repo

    repo = kernel_repo("e1")
    cfg, ledger = ctx(os.path.join(case, "e1"), "wt")
    wt = sandbox("G2E1", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "lane_status.py"), "x = 2\n")               # 声明目标：内核文件
    write(os.path.join(wt, "scripts", "test_lane_status.py"), "assert x == 2\n")  # 同步用例
    write(os.path.join(wt, "scripts", "other.py"), "o = 2\n")                     # 真越界
    pr = fr.probe_code_delivery(wt, repo, ["scripts/lane_status.py"], builder_rc=0)
    ch = pr.get("checks") or {}
    check("G2e 改了内核又同步了用例 → 不算越界、单列 tests_synced",
          [t["path"] for t in ch.get("tests_synced") or []] == ["scripts/test_lane_status.py"]
          and ch.get("files_outside_declared") == ["scripts/other.py"], json.dumps(ch, ensure_ascii=False)[:400])
    check("G2e 硬闸照常跑（沙箱镜像里的套件全绿）",
          (pr.get("strict_checks") or {}).get("kernel_tests", {}).get("ok") is True
          and (pr.get("strict_checks") or {}).get("kernel_files") == ["scripts/lane_status.py"],
          json.dumps(pr.get("strict_checks"), ensure_ascii=False)[:300])
    repo = kernel_repo("e2")
    cfg, ledger = ctx(os.path.join(case, "e2"), "wt")
    wt = sandbox("G2E2", repo, cfg, ledger)
    write(os.path.join(wt, "scripts", "app.py"), "a = 9\n")                       # 没改内核
    write(os.path.join(wt, "scripts", "test_lane_status.py"), "assert False\n")
    pr = fr.probe_code_delivery(wt, repo, ["scripts/app.py"], builder_rc=0)
    ch = pr.get("checks") or {}
    check("G2e 没改内核 → 同步用例不算工序，照旧算越界",
          ch.get("tests_synced") == [] and ch.get("files_outside_declared") == ["scripts/test_lane_status.py"],
          json.dumps(ch, ensure_ascii=False)[:300])
    check("G2e 没碰内核文件 → 硬闸不触发", "kernel_tests" not in (pr.get("strict_checks") or {}), str(pr.get("strict_checks")))

    # G2f 质检简报：同步过用例时单列一段；没同步就不添
    ns = _brief_ns(case, "g2", "code", spec_len=20)
    out = _run_brief(ns)
    check("G2f 没同步用例 → 简报不添这一段", "本轮同步了内核回归用例" not in out["brief"], "")
    ns["probe_res"] = dict(ns["probe_res"], checks=dict(ns["probe_res"].get("checks") or {},
                                                        tests_synced=[{"path": "scripts/test_lane_status.py", "added": 3, "deleted": 1}]))
    ns["probe_summary_obj"] = {k: v for k, v in ns["probe_res"].items() if k not in ("visible_text_preview", "cdp_telemetry_summary")}
    out = _run_brief(ns)
    text = out["brief"]
    check("G2f 同步过用例 → 简报单列一段请质检专门核对",
          "本轮同步了内核回归用例（请专门核对）" in text and "`scripts/test_lane_status.py`（+3 / −1）" in text
          and "没有依据地放宽断言 → P0" in text, text[text.find("### 本轮同步了内核回归用例"):][:300])
    ns["probe_res"] = dict(ns["probe_res"], strict_checks={"kernel_tests": {"ran": True, "ok": True}},
                           checks={"files_changed": [{"path": "scripts/run_tests.py", "added": 2, "deleted": 0}], "tests_synced": []})
    ns["probe_summary_obj"] = {k: v for k, v in ns["probe_res"].items() if k not in ("visible_text_preview", "cdp_telemetry_summary")}
    text = _run_brief(ns)["brief"]
    check("G2f 改了回归套件跑手本身 → 简报点名请质检核对（拆登记 = 拆硬闸）",
          "本轮改了回归套件的跑手 `scripts/run_tests.py`" in text and "拆硬闸 → P0" in text
          and "本轮同步了内核回归用例" not in text, "")
    ns["probe_res"] = dict(ns["probe_res"], strict_checks={"py_syntax": {"checked": 0, "bad": []}})
    ns["probe_summary_obj"] = {k: v for k, v in ns["probe_res"].items() if k not in ("visible_text_preview", "cdp_telemetry_summary")}
    check("G2f 硬闸没跑（非内核仓）→ 同名跑手不添这段", "本轮改了回归套件的跑手" not in _run_brief(ns)["brief"], "")


def g3(case):
    """2026-11-26：工人自己提交过、沙箱只剩没跟踪的运行态文件 → git 的三种「没东西可提交」都算数（G44R4 / G44R5 卡在合入）。"""
    f = fr._git_nothing_to_commit
    check("G3 「nothing added to commit … but untracked files present」算没东西可提交",
          f('On branch wt/X\nUntracked files:\n  state/tmp/\n\nnothing added to commit but untracked files present (use "git add" to track)\n'))
    check("G3 「nothing to commit, working tree clean」算没东西可提交", f("On branch wt/X\nnothing to commit, working tree clean\n"))
    check("G3 「no changes added to commit」算没东西可提交", f('no changes added to commit (use "git add" and/or "git commit -a")\n'))
    check("G3 身份没配 / 真的提交失败 / 空输出 → 不算",
          not f("Author identity unknown\n*** Please tell me who you are.\nfatal: unable to auto-detect email address")
          and not f("") and not f(None))


def _hidden_console_python(args):
    """起一个独占「隐藏新控制台」的 python 子进程：控制台事件只在这个新控制台里生效，碰不到跑测试的人的终端；新控制台不弹窗。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                                   # SW_HIDE
    return subprocess.Popen([sys.executable, "-X", "utf8"] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            startupinfo=si, creationflags=subprocess.CREATE_NEW_CONSOLE)


def _fill(code, **kw):
    for k, v in kw.items():
        code = code.replace("<<%s>>" % k.upper(), repr(v))
    return code


_CTRL_PRELUDE = r'''
import ctypes, os, subprocess, sys, time
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
k32.SetConsoleCtrlHandler.restype = ctypes.c_int
k32.SetConsoleCtrlHandler(ctypes.c_void_p(None), 0)      # 清掉继承来的「忽略 Ctrl+C」：不清的话事件根本送不到处理器
'''
# 「敲门的」：0.8 秒后给整个控制台发一次 Ctrl+C（发的人自己不收），然后退出
_KNOCKER_CODE = "import os, signal, time\ntime.sleep(0.8)\nos.kill(0, signal.CTRL_C_EVENT)\ntime.sleep(0.5)\n"
_G4_GUARDED = _CTRL_PRELUDE + r'''
sys.path.insert(0, <<SCRIPTS>>)
import factory_runner as fr
installed = fr._install_console_guard()
knocker = subprocess.Popen([sys.executable, "-c", <<KNOCKER>>])
time.sleep(0.3)
peers = fr._console_peers()
time.sleep(1.5)
knocker.wait(timeout=5)
print("RESULT installed=%s events=%d peers=%d has_self=%s" % (
    installed, len(fr._CONSOLE_GUARD["events"]), len(peers),
    any(p.startswith("%d:" % os.getpid()) for p in peers)))
'''
_G4_CONTROL = _CTRL_PRELUDE + r'''
knocker = subprocess.Popen([sys.executable, "-c", <<KNOCKER>>])
time.sleep(1.8)
knocker.wait(timeout=5)
print("RESULT survived")
'''
# 「被探活的」：新进程组的组长，收到任何控制台事件就往 marker 里记一笔
_G5_KNOCKEE = _CTRL_PRELUDE + r'''
HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)


def on_ctrl(evt):
    with open(<<MARKER>>, "a", encoding="utf-8") as fh:
        fh.write("ctrl event %d\n" % int(evt))
    return 1


_h = HANDLER(on_ctrl)
k32.SetConsoleCtrlHandler(ctypes.c_void_p(ctypes.cast(_h, ctypes.c_void_p).value), 1)
with open(<<READY>>, "w", encoding="utf-8") as fh:
    fh.write("ready")
time.sleep(4)
'''
_G5_MAIN = _CTRL_PRELUDE + r'''
sys.path.insert(0, <<SCRIPTS>>)
import expert_seat as seat
knockee = subprocess.Popen([sys.executable, "-X", "utf8", <<KNOCKEE>>], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
for _ in range(60):
    if os.path.exists(<<READY>>):
        break
    time.sleep(0.1)
time.sleep(0.3)
alive = seat._alive(knockee.pid)
time.sleep(0.8)
print("RESULT alive=%s hit=%s still_running=%s" % (alive, os.path.exists(<<MARKER>>), knockee.poll() is None))
knockee.kill()
'''


def g4(case):
    """2026-11-26：隐藏窗口里的跑单进程收到控制台 Ctrl+C 只留痕、不退出（G42R4 四次「没正常收口」的护栏）。"""
    check("G4 判断控制台是否隐藏：不抛错、给的是布尔值", isinstance(fr._console_hidden(), bool))
    check("G4 同控制台进程清单：给的是列表，查不了就是空表", isinstance(fr._console_peers(), list))
    if os.name != "nt":
        return
    p = _hidden_console_python(["-c", _fill(_G4_GUARDED, scripts=HERE, knocker=_KNOCKER_CODE)])
    out, _ = p.communicate(timeout=90)
    m = re.search(r"events=(\d+)", out)
    check("G4 装了护栏：同一个 Ctrl+C 打不死，事件被记下",
          p.returncode == 0 and "RESULT installed=True" in out and bool(m) and int(m.group(1)) >= 1,
          "rc=%s | %s" % (p.returncode, out.strip()[-300:]))
    check("G4 留痕里有同控制台进程清单（含自己和敲门的）", "peers=2" in out and "has_self=True" in out, out.strip()[-200:])
    p = _hidden_console_python(["-c", _fill(_G4_CONTROL, knocker=_KNOCKER_CODE)])
    out, _ = p.communicate(timeout=90)
    check("G4 对照：不装护栏，同一个 Ctrl+C 就把进程打死了", p.returncode != 0 and "RESULT" not in out,
          "rc=%s | %s" % (p.returncode, out.strip()[-200:]))


def g5(case):
    """2026-11-26：专家席探活只问系统、不发信号（os.kill(pid, 0) 在 Windows 上是发 Ctrl+C，捅死过跑单进程）。"""
    import expert_seat as seat
    check("G5 自己活着", seat._alive(os.getpid()) is True)
    check("G5 空 / 0 / 负数 / 不是数 → 一律不算活",
          seat._alive(0) is False and seat._alive(-5) is False and seat._alive(None) is False
          and seat._alive("") is False and seat._alive("x") is False)
    p = subprocess.Popen([sys.executable, "-c", "pass"])     # 已经退出：句柄还攥在手里，pid 不会被别的进程复用
    p.wait(timeout=60)
    check("G5 已经退出的进程不算活", seat._alive(p.pid) is False, "pid=%s" % p.pid)
    if os.name != "nt":
        return
    ready, marker = os.path.join(case, "ready.txt"), os.path.join(case, "marker.txt")
    knockee = os.path.join(case, "g5_knockee.py")
    write(knockee, _fill(_G5_KNOCKEE, ready=ready, marker=marker))
    p = _hidden_console_python(["-c", _fill(_G5_MAIN, scripts=HERE, knockee=knockee, ready=ready, marker=marker)])
    out, _ = p.communicate(timeout=90)
    check("G5 探活不惊动对方：被探的进程没收到 Ctrl+C、照常活着", "RESULT alive=True hit=False still_running=True" in out,
          "rc=%s | %s" % (p.returncode, out.strip()[-300:]))


def g6(case):
    """2026-11-26：验收脚本本轮真被改了 → 质检简报事前单列（「验收脚本落在改动范围内不当作验收」不再只是收尾贴标注）。"""
    head = "### 本单的验收脚本本轮被改了（请专门核对）"
    ns = _brief_ns(case, "g6", "code", spec_len=20)
    ns["probe_res"] = dict(ns["probe_res"],
                           checks={"files_changed": [{"path": "scripts/app.py", "added": 3, "deleted": 1},
                                                     {"path": "scripts/test_app.py", "added": 5, "deleted": 0},
                                                     {"path": "scripts/run_tests.py", "added": 2, "deleted": 0}],
                                  "tests_synced": []},
                           strict_checks={"kernel_tests": {"ran": True, "ok": True}})
    ns["probe_summary_obj"] = {k: v for k, v in ns["probe_res"].items() if k not in ("visible_text_preview", "cdp_telemetry_summary")}
    ns["job"] = dict(ns["job"], test_cmd="python -X utf8 scripts/test_app.py && python scripts\\run_tests.py")
    text = _run_brief(ns)["brief"]
    check("G6 验收命令点名的文件本轮被改了 → 简报单列这一节", head in text, text[text.find("### 本轮"):][:300])
    sec = text.split(head, 1)[1].split("\n### ", 1)[0] if head in text else ""
    check("G6 节里点名被改的验收脚本；没被验收命令点名的改动不进来；写明它的通过不算独立验收",
          "`scripts/test_app.py`" in sec and "`scripts/run_tests.py`" in sec and "scripts/app.py" not in sec
          and "不算独立验收" in sec, sec[:300])
    ns["job"] = dict(ns["job"], test_cmd="python -X utf8 scripts/test_other.py")
    text = _run_brief(ns)["brief"]
    check("G6 验收命令点名的文件本轮没被改 → 不添这一节", head not in text, text[text.find("### 本轮"):][:200])


_CONTRAST_FAIL = "存在 WCAG 对比度不足元素 (实测最低 4.43 < 4.5:1): [{'sel': '.x'}]"      # 页面通检的毛病（生产页本来就有）


def _z_ui(status, **over):
    """一条断言的真引擎判定结果（结构同 evaluate_assertions 的产出）。"""
    row = {"viewport": "390", "status": status, "measured": "demo" if status == "PASS" else "—", "expected": "demo"}
    if status == "UNVERIFIED":
        row["reason"] = "选择器 0 命中: #t"
    res = [{"id": "R7-1", "clause": "F1", "desc": "交付时间在", "required": True, "state": "default", "selector": "#t", "metric": "text",
            "op": "contains", "value": "demo", "aggregate": "first", "status": status, "per_viewport": [row]}]
    summ = {"total": 1, "pass": int(status == "PASS"), "fail": int(status == "FAIL"), "unverified": int(status == "UNVERIFIED"),
            "required_unverified": ["R7-1"] if status == "UNVERIFIED" else []}
    base = {"all_passed": status == "PASS", "checks": {"http_200_auth": True, "data_orders_count": 5, "assertions": summ},
            "failures": [] if status == "PASS" else ["[断言 %s · R7-1/F1] 交付时间在 @ 390 状态=default：实测 —，期望 contains demo" % status],
            "screenshots": [{"file": os.path.join(_TEMP_HOME, "shots", "v21_390x844.png"), "ok": True}], "assertions": res, "assert_summary": summ}
    base.update(over)
    return base


def g7(case):
    """2026-11-26：code 单的真引擎取证结果并进 Gate1 结论——施工问题判 FAIL，探针自己出的状况不冤枉工人。"""
    code = {"all_passed": True, "checks": {"files_changed": [{"path": "scripts/v3_data.py", "added": 3, "deleted": 1}]},
            "failures": [], "screenshots": [], "strict_checks": {}}
    tel = os.path.join(case, "cdp_telemetry.json")
    write(tel, json.dumps({"jsErrors": ["boom"], "viewports": {}}))
    res = fr._merge_code_runtime(code, _z_ui("PASS", telemetry_path=tel))
    check("G7 真引擎全过：截图 / 断言 / 遥测并进来，机检结论原样保留，不改传入的对象",
          res["all_passed"] is True and res["screenshots"][0]["ok"] is True and res["assert_summary"]["pass"] == 1
          and res["telemetry_path"] == tel and res["checks"]["files_changed"] == code["checks"]["files_changed"]
          and code["screenshots"] == [] and "runtime_evidence" not in code["checks"], json.dumps(res["checks"], ensure_ascii=False)[:300])
    ev = res["checks"]["runtime_evidence"]
    check("G7 runtime_evidence 记档：跑了、全过、布尔 / 数字类页面检查、JS 报错数",
          ev["ran"] is True and ev["all_passed"] is True and ev["ui_checks"] == {"http_200_auth": True, "data_orders_count": 5}
          and ev["js_errors"] == 1 and ev["assert_summary"]["pass"] == 1, str(ev)[:300])
    res = fr._merge_code_runtime(code, _z_ui("FAIL", failures=["[断言 FAIL · R7-1/F1] 交付时间在 @ 390 状态=default：实测 —，期望 contains demo",
                                                                "首屏可见正文仅 0 字符，疑似脚本报错白屏"]))
    check("G7 真引擎查出施工问题（断言不过 / 白屏）→ 判 FAIL，失败项带「[看板真引擎]」前缀",
          res["all_passed"] is False and len(res["failures"]) == 2 and all(f.startswith("[看板真引擎] ") for f in res["failures"])
          and code["failures"] == [], str(res["failures"])[:300])
    res = fr._merge_code_runtime(code, {"all_passed": False, "failures": ["[ENV] Edge CDP 3次启动均未就绪（环境故障，非工件缺陷）stderr=x"],
                                        "screenshots": [], "env_fail": True})
    ev = res["checks"]["runtime_evidence"]
    check("G7 取证环境故障 → 不判 FAIL、不并失败项，如实记档",
          res["all_passed"] is True and res["failures"] == [] and ev["ran"] is False and ev["env_fail"] is True and "Edge CDP" in ev["reason"],
          str(ev)[:300])
    ns = _brief_ns(case, "g7", "code", spec_len=6)
    ns["spec_asserts"] = [{"id": "R7-1", "clause": "F1", "desc": "交付时间在", "metric": "text", "selector": "#t", "op": "contains", "value": "x",
                           "viewports": [390]}]
    ns["probe_res"] = res
    ns["probe_summary_obj"] = {k: v for k, v in res.items() if k not in ("visible_text_preview", "cdp_telemetry_summary")}
    text = _run_brief(ns)["brief"]
    check("G7 质检考卷（code 单）带断言机检一节：取证没取到时直说原因，不给一张空表",
          "规格断言机检结果" in text and "本轮真引擎取证没取到" in text and "Edge CDP 3次启动均未就绪" in text and '"env_fail": true' in text,
          text[text.find("### 规格断言机检结果"):][:300])
    ns["probe_res"] = fr._merge_code_runtime(code, _z_ui("PASS"))
    ns["probe_summary_obj"] = {k: v for k, v in ns["probe_res"].items() if k not in ("visible_text_preview", "cdp_telemetry_summary")}
    text = _run_brief(ns)["brief"]
    check("G7 取到了证 → 质检考卷里是断言结果表", "| R7-1 |" in text and "本轮真引擎取证没取到" not in text,
          text[text.find("### 规格断言机检结果"):][:300])
    res = fr._merge_code_runtime(code, _z_ui("PASS", all_passed=False, failures=[
        "CDP 浏览器探针执行异常: boom", "违反 Worktree 隔离纪律：Builder 原地篡改了主仓 C:/x/dashboard/v2.html",
        "静态正本被修改: ['flow.html']", "HTML 源码硬编码了看板 token"]))
    ev = res["checks"]["runtime_evidence"]
    check("G7 探针自身异常 / 主仓页面与六页正本在施工期间变了 → 不判 FAIL 但留痕；硬编码口令照判 FAIL",
          res["all_passed"] is False and len(res["failures"]) == 1 and "硬编码了看板 token" in res["failures"][0]
          and len(ev["probe_error"]) == 3 and ev["all_passed"] is False, str(ev.get("probe_error"))[:300] + " | " + str(res["failures"])[:200])
    afl = "[断言 FAIL · R7-1/F1] 交付时间在 @ 390 状态=default：实测 —，期望 contains demo"
    res = fr._merge_code_runtime(code, dict(_z_ui("PASS", all_passed=False, failures=[_CONTRAST_FAIL]), baseline_failures=[_CONTRAST_FAIL]))
    ev = res["checks"]["runtime_evidence"]
    check("G7 页面通检的毛病生产页本来就有 → 不算本单的（不判 FAIL），记档交质检",
          res["all_passed"] is True and res["failures"] == [] and ev["preexisting_failures"] == [_CONTRAST_FAIL], str(ev)[:300])
    res = fr._merge_code_runtime(code, dict(_z_ui("PASS", all_passed=False, failures=[_CONTRAST_FAIL, afl, "首屏可见正文仅 0 字符，疑似脚本报错白屏"]),
                                            baseline_failures=[_CONTRAST_FAIL, afl]))
    ev = res["checks"]["runtime_evidence"]
    check("G7 既有毛病剔除后剩下的都是本单的：断言 FAIL 即使生产页也不过，照样算本单的",
          res["all_passed"] is False and len(res["failures"]) == 2 and afl in res["failures"][0] and "白屏" in res["failures"][1]
          and ev["preexisting_failures"] == [_CONTRAST_FAIL], str(res["failures"])[:300])
    res = fr._merge_code_runtime(code, dict(_z_ui("PASS", all_passed=False, failures=[_CONTRAST_FAIL]), baseline_failures=None))
    ev = res["checks"]["runtime_evidence"]
    check("G7 生产页基线探针自己没跑成 → 区分不了既有问题，毛病按本单处理并写明",
          res["all_passed"] is False and len(res["failures"]) == 1 and "preexisting_failures" not in ev and "基线探针环境故障" in ev["baseline_note"],
          str(ev)[:300])
    same = fr._merge_code_runtime(code, {})
    check("G7 没有取证结果 → 原样返回，不添 runtime_evidence", same["all_passed"] is True and "runtime_evidence" not in same["checks"])


def g8(case):
    """2026-11-26：code 单真引擎取证——环境故障重试 2 次；一直不行就如实带回，不装作取到了证；execute_job 里确实接了线。"""
    calls, slept = [], []
    real_probe, real_sleep = fr.probe_ui_dashboard, fr.time.sleep
    env_fail = {"all_passed": False, "failures": ["[ENV] Edge CDP 3次启动均未就绪（环境故障，非工件缺陷）stderr=x"], "screenshots": [], "env_fail": True}
    script = [env_fail, env_fail, _z_ui("PASS")]
    fr.probe_ui_dashboard = lambda **kw: (calls.append(kw) or script[min(len(calls), len(script)) - 1])
    fr.time.sleep = lambda s: slept.append(s)
    try:
        asserts = [{"id": "R7-1"}]
        kw = dict(wt_repo=os.path.join(case, "wt"), repo_root=os.path.join(case, "live"), page="dashboard/v2.html",
                  out_dir=os.path.join(case, "runner_a2_ui"), six_baseline={"flow.html": "x"}, main_sha_before="sha0")
        res = fr._code_runtime_evidence(assertions=asserts, **kw)
        check("G8 环境故障重试到第 3 次成了 → 拿到那一次的结果", res.get("all_passed") is True and len(calls) == 3 and slept == [5, 5],
              "calls=%s slept=%s" % (len(calls), slept))
        k = calls[0]
        check("G8 传给真引擎探针的参数名一个不多一个不少（probe_ui_dashboard 的签名）",
              set(k) == {"wt_repo", "main_repo", "target_rel", "out_dir", "six_baseline", "old_sha", "main_file_sha_before", "assertions"},
              str(sorted(k)))
        check("G8 探的是沙箱里的那个页面；old_sha 传空（页面没改不算问题）；断言原样交给探针",
              k["target_rel"] == "dashboard/v2.html" and k["wt_repo"] == kw["wt_repo"] and k["main_repo"] == kw["repo_root"]
              and k["old_sha"] == "" and k["main_file_sha_before"] == "sha0" and k["assertions"] is asserts
              and k["out_dir"] == kw["out_dir"] and k["six_baseline"] == {"flow.html": "x"}, str({x: k[x] for x in ("target_rel", "old_sha")}))
        calls.clear()
        slept.clear()
        script[:] = [env_fail]
        res = fr._code_runtime_evidence(assertions=[], **kw)
        check("G8 一直起不来 → 3 次为止，如实带回 env_fail；没断言时传 None",
              len(calls) == 3 and res.get("env_fail") is True and calls[0]["assertions"] is None and slept == [5, 5],
              "calls=%s res=%s" % (len(calls), str(res)[:120]))
        calls.clear()

        def fake_by_repo(**k):
            calls.append(k)
            return dict(_z_ui("PASS", all_passed=False, failures=[_CONTRAST_FAIL]))
        fr.probe_ui_dashboard = fake_by_repo
        res = fr._code_runtime_evidence(assertions=asserts, **kw)
        check("G8 沙箱页有通检毛病 → 再对主仓生产页跑一遍同款探针（不带断言、证据另放），毛病原样挂在 baseline_failures 上",
              len(calls) == 2 and calls[1]["wt_repo"] == kw["repo_root"] and calls[1]["main_repo"] == kw["repo_root"]
              and calls[1]["target_rel"] == "dashboard/v2.html" and calls[1]["assertions"] is None
              and calls[1]["out_dir"] == kw["out_dir"] + "_base" and calls[1]["old_sha"] == "" and res["baseline_failures"] == [_CONTRAST_FAIL],
              "calls=%s | %s" % (len(calls), str(calls[1] if len(calls) > 1 else {})[:200]))
        calls.clear()
        fr.probe_ui_dashboard = lambda **k: calls.append(k) or dict(_z_ui("FAIL"))
        res = fr._code_runtime_evidence(assertions=asserts, **kw)
        check("G8 只有断言不过 → 不跑生产页基线（断言 FAIL 一律算本单的）",
              len(calls) == 1 and "baseline_failures" not in res and res["all_passed"] is False, "calls=%s" % len(calls))
    finally:
        fr.probe_ui_dashboard, fr.time.sleep = real_probe, real_sleep
    # 内核源码按测试文件自己所在目录取：AA 上测试壳会把活仓前缀下的路径常量改指临时目录，fr.__file__ 也被改走
    ksrc_path = os.path.join(HERE, "factory_runner.py")
    tree = ast.parse(read(ksrc_path).replace("\r\n", "\n"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute_job")
    called = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    check("G8 execute_job 里接了线：取证 1 处、并结论 2 处（机检后 + 补证后）",
          called.count("_code_runtime_evidence") == 1 and called.count("_merge_code_runtime") == 2,
          str({c: called.count(c) for c in ("_code_runtime_evidence", "_merge_code_runtime", "probe_ui_dashboard")}))
    src = read(ksrc_path).replace("\r\n", "\n")
    check("G8 code 单的断言块也解析（ui 单仍必须带断言；code 单写坏了不拦单）；补证探的是同一个看板页面",
          'if v4.get("assertion_gate") and _assert_kind in ("ui", "code"):' in src
          and "if _a_errs and _assert_kind == \"code\":" in src and "spec_asserts, _a_errs = [], []" in src
          and '(_a_errs or (not spec_asserts and _assert_kind == "ui"))' in src
          and src.count("probe_ui_dashboard(wt_repo=wt_path, main_repo=repo_root, target_rel=ui_probe_rel, out_dir=_rp_dir,") == 1)


def g9(case):
    """2026-11-26：后端合入 ≠ 上线——合入了看板服务加载的模块时，核看板服务进程是不是比盘上数据层还旧；只留痕、不动服务。"""
    live = os.path.join(case, "live")
    write(os.path.join(live, "scripts", "board_server.py"), "import os\n\ndef data():\n    import v3_data\n    return v3_data\n")
    write(os.path.join(live, "scripts", "v3_data.py"), "import line_details\nimport factory_seat\nimport os, sys\nX = 1\n")
    write(os.path.join(live, "scripts", "line_details.py"), "import json\nY = 2\n")
    write(os.path.join(live, "scripts", "factory_seat.py"), "Z = 3\n")
    write(os.path.join(live, "scripts", "unrelated.py"), "Q = 4\n")
    write(os.path.join(live, "scripts", "v3_data_test.py"), "T = 5\n")
    loaded = fr.board_server_loaded_modules(live)
    check("G9 看板服务加载的模块：从 board_server 沿 import 走得到（含函数里的延迟导入）+ 保底名；不相干的脚本、标准库不算",
          {"board_server.py", "v3_data.py", "line_details.py", "factory_seat.py", "lane_status.py", "mes_board.py"} <= loaded
          and not ({"unrelated.py", "v3_data_test.py", "os.py", "json.py", "sys.py"} & loaded), str(sorted(loaded)))
    keep = {k: getattr(fr, k) for k in ("STEWARD_ROOT", "_board_server_procs", "subprocess", "append_v3_event", "_append_patrol", "v4_trace")}
    asked, ev, pat, tr = [], [], [], []
    srv = os.path.join(live, "scripts", "board_server.py")

    class _Res:
        def __init__(self, out):
            self.stdout, self.returncode = out, 0

    class _FakeSub:
        def __init__(self, out=None, exc=None):
            self.out, self.exc = out, exc

        def run(self, *a, **k):
            if self.exc:
                raise self.exc
            return _Res(self.out)

    try:
        fr.STEWARD_ROOT = live
        fr.subprocess = _FakeSub("12345|2026-11-25 18:49:29|\"C:\\py\\python.exe\" -X utf8 %s\r\nnoise line\n7|2026-11-26 12:00:00|python C:\\tmp\\x\\scripts\\board_server.py\n"
                                 % srv)
        procs = fr._board_server_procs(live)
        check("G9 进程表解析：pid / 启动时刻 / 是不是本仓跑的看板服务（认命令行里的路径）；命令行只用来认人，不外传",
              [(p["pid"], p["started"], p["in_repo"]) for p in procs] == [(12345, "2026-11-25 18:49:29", True), (7, "2026-11-26 12:00:00", False)]
              and all(set(p) == {"pid", "started", "in_repo"} for p in procs), str(procs))
        fr.subprocess = _FakeSub(exc=OSError("powershell 起不来"))
        check("G9 查不了进程表 → 空表，不抛", fr._board_server_procs(live) == [])

        def fake_procs(root, table):
            asked.append(root)
            if isinstance(table, Exception):
                raise table
            return list(table)
        old, fresh = dict(pid=1, started="2000-01-01 00:00:00", in_repo=True), dict(pid=2, started="2999-01-01 00:00:00", in_repo=True)
        fr._board_server_procs = lambda root: fake_procs(root, [old])
        n = fr.merge_not_live_note("C-9", 1, live, ["scripts/unrelated.py"])
        check("G9 合入的东西看板服务不加载 → 不核（不去查进程表）", n["checked"] is False and n["stale"] is None and asked == [], str(n))
        n = fr.merge_not_live_note("C-9", 1, os.path.join(case, "blueprint"), ["scripts/v3_data.py"])
        check("G9 合的不是看板服务跑的那个仓（档案仓）→ 不核", n["checked"] is False and asked == [], str(n))
        n = fr.merge_not_live_note("C-9", 1, live, ["scripts\\V3_DATA.py"])
        check("G9 合入了数据层、服务进程比盘上数据层旧 → 判「未生效」；路径写法不挑（反斜杠、大小写）",
              n["checked"] is True and n["stale"] is True and n["files"] == ["scripts/v3_data.py"] and n["started"] == old["started"]
              and n["data_layer_at"] >= "2026" and asked == [live], str(n))
        fr._board_server_procs = lambda root: fake_procs(root, [fresh])
        n = fr.merge_not_live_note("C-9", 1, live, ["scripts/v3_data.py"])
        check("G9 服务进程比数据层新（合入后重启过）→ 不算未生效", n["checked"] is True and n["stale"] is False, str(n))
        fr._board_server_procs = lambda root: fake_procs(root, [])
        n = fr.merge_not_live_note("C-9", 1, live, ["scripts/v3_data.py"])
        check("G9 看板服务没在跑 / 认不出 → 不下结论（stale=None），不瞎报", n["checked"] is True and n["stale"] is None, str(n))
        fr._board_server_procs = lambda root: fake_procs(root, [old, dict(pid=3, started="2999-01-01 00:00:00", in_repo=False)])
        n = fr.merge_not_live_note("C-9", 1, live, ["scripts/v3_data.py"])
        check("G9 另有一个别处起的看板服务进程（测试 / 镜像用的）→ 只认本仓跑的那个", n["stale"] is True and n["pid"] == 1 and n["procs"] == 2, str(n))
        fr._board_server_procs = lambda root: fake_procs(root, RuntimeError("CIM 查询崩了"))
        n = fr.merge_not_live_note("C-9", 1, live, ["scripts/v3_data.py"])
        check("G9 核的过程里出了岔子 → 带着错误标记回来，不抛、不影响合入", n["checked"] is True and n["stale"] is None and "CIM" in n.get("error", ""), str(n))

        fr._board_server_procs = lambda root: fake_procs(root, [old])
        fr.append_v3_event = lambda *a, **k: ev.append((a, k))
        fr._append_patrol = lambda line: pat.append(line)
        fr.v4_trace = lambda event, oid="", **f: tr.append((event, f))
        nl = fr.note_merge_not_live("C-9", 1, live, ["scripts/v3_data.py"])
        check("G9 判「未生效」→ 事件「合入未生效提醒」+ 巡检 [MERGE_NOT_LIVE]，话说清：合了什么、服务哪一刻起的、要重启才生效、内核不自己动",
              nl.get("stale") is True and len(ev) == 1 and ev[0][0][:4] == ("C-9", 1, "merge_not_live", "合入未生效提醒")
              and "scripts/v3_data.py" in ev[0][0][4] and old["started"] in ev[0][0][4] and len(pat) == 1
              and pat[0].startswith("[MERGE_NOT_LIVE ") and "C-9" in pat[0] and "内核不自己动" in pat[0]
              and len(tr) == 1 and tr[0][0] == "merge_not_live" and tr[0][1]["stale"] is True and tr[0][1]["attempt"] == 1,
              "ev=%s | pat=%s | tr=%s" % (ev[:1], pat[:1], tr[:1]))
        fr._board_server_procs = lambda root: fake_procs(root, [fresh])
        fr.note_merge_not_live("C-9", 1, live, ["scripts/v3_data.py"])
        fr.note_merge_not_live("C-9", 1, live, ["scripts/unrelated.py"])
        check("G9 服务是新的 / 合入的东西看板服务不加载 → 不记事件、不告警", len(ev) == 1 and len(pat) == 1, "ev=%s pat=%s" % (len(ev), len(pat)))
        fr._board_server_procs = lambda root: fake_procs(root, [old])
        fr.append_v3_event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("事件文件写不进去"))
        check("G9 记事件时出了岔子 → 吞掉，合入结论不受影响", fr.note_merge_not_live("C-9", 1, live, ["scripts/v3_data.py"]) == {})
    finally:
        for k, v in keep.items():
            setattr(fr, k, v)
    # 接线：execute_job 合入成功那一段正好调一次（在「内核合入主干」事件之后）
    ksrc_path = os.path.join(HERE, "factory_runner.py")
    tree = ast.parse(read(ksrc_path).replace("\r\n", "\n"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute_job")
    src_lines = read(ksrc_path).replace("\r\n", "\n").splitlines()
    calls = [(n.lineno) for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "note_merge_not_live"]
    merged_at = [i + 1 for i, ln in enumerate(src_lines) if 'append_v3_event(order_id, attempt, "merged", "内核合入主干"' in ln]
    check("G9 execute_job 合入成功那一段接了线（恰好 1 处，紧跟「内核合入主干」事件）",
          len(calls) == 1 and len(merged_at) == 1 and calls[0] == merged_at[0] + 1, "calls=%s merged=%s" % (calls, merged_at))


def g10(case):
    """2026-11-26：Gate1 真引擎的 /data.json 必须用沙箱自己的 v3_data 现算——活仓缓存模块会把数据层改动判成没做。"""
    wt = os.path.join(case, "wt")
    write(os.path.join(wt, "dashboard", "v2.html"), "<html><body>sandbox</body></html>\n")
    write(os.path.join(wt, "scripts", "v3_data.py"),
          "def collect_v3_data(base_data=None, target_override=''):\n"
          "    return {'marker': 'SANDBOX_V3', 'target': target_override, 'orders': [], 'details': {}}\n")
    srv = fr._SandboxServer(wt, "dashboard/v2.html", token="tk")
    with srv:
        url = "http://127.0.0.1:%d/data.json?k=tk" % srv.port
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
    want_target = os.path.join(wt, "dashboard", "v2.html")
    check("G10 沙箱 /data.json 由被测 worktree 的 v3_data 现算，不是内核进程里缓存的活仓模块",
          d.get("marker") == "SANDBOX_V3" and os.path.normcase(str(d.get("target") or "")) == os.path.normcase(want_target),
          str(d)[:300])

    write(os.path.join(wt, "scripts", "v3_data.py"),
          "def collect_v3_data(base_data=None, target_override=''):\n"
          "    raise RuntimeError('sandbox boom')\n")
    srv = fr._SandboxServer(wt, "dashboard/v2.html", token="tk")
    code = None
    with srv:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/data.json?k=tk" % srv.port, timeout=30)
        except urllib.error.HTTPError as exc:
            code = exc.code
    check("G10 沙箱数据层自己报错 → 预览服务 500 如实暴露，不静默回落到活仓旧数据", code == 500, "code=%s" % code)


def g11(case):
    """2026-11-26：ui 单的 Gate1 也做生产页基线——生产页本来就有的通检毛病不算本单的；
    断言 FAIL 与「比生产页更差」的正文墙照判本单。"""
    soft = "违反 Worktree 隔离纪律：Builder 原地篡改了主仓 C:/x/dashboard/v2.html"
    afl = "[断言 FAIL · R8-1/F1] 交付时间在 @ 390 状态=default：实测 —，期望 contains demo"
    new = "页面色彩对比度不足（AA）3 处"
    wall_s = "首屏默认可见正文 5886 字符过多 (上限 4500，文字墙未压密)"
    wall_b = "首屏默认可见正文 5928 字符过多 (上限 4500，文字墙未压密)"
    wall_bad = "首屏默认可见正文 7000 字符过多 (上限 4500，文字墙未压密)"

    res = {"all_passed": False, "failures": [soft, wall_s, afl, new],
           "baseline_failures": [soft, wall_b]}
    pre = fr._ui_preexisting(res)
    check("G11 生产页本来就有的正文墙（计数在漂移容差内）不计本单；软告警不进失败项；断言 FAIL 与新毛病照留",
          pre == [wall_s] and fr._ui_preexisting({"failures": [wall_bad], "baseline_failures": [wall_b]}) == [],
          "pre=%s" % pre)

    calls = []
    real_probe = fr.probe_ui_dashboard

    def fake_probe(**kw):
        calls.append(kw)
        return {"all_passed": False, "failures": [soft, wall_b], "screenshots": []}

    fr.probe_ui_dashboard = fake_probe
    try:
        out = fr._ui_gate1_with_baseline(
            dict(res), repo_root=os.path.join(case, "live"), page="dashboard/v2.html",
            out_dir=os.path.join(case, "runner_a1_base"), six_baseline={"flow.html": "x"}, main_sha_before="sha0")
    finally:
        fr.probe_ui_dashboard = real_probe
    bl = out["checks"]["gate1_baseline"]
    check("G11 ui 单沙箱页有硬毛病 → 对主仓生产页跑同款探针（不带断言），只剔除生产页也有的毛病",
          len(calls) == 1 and calls[0]["wt_repo"] == os.path.join(case, "live")
          and calls[0]["main_repo"] == os.path.join(case, "live") and calls[0]["assertions"] is None
          and calls[0]["out_dir"] == os.path.join(case, "runner_a1_base") and calls[0]["old_sha"] == ""
          and bl["ran"] is True and bl["excluded"] == [wall_s]
          and out["failures"] == [soft, afl, new] and out["all_passed"] is False,
          "calls=%s | %s" % (len(calls), str(bl)[:220]))

    fr.probe_ui_dashboard = lambda **kw: {"all_passed": False, "failures": ["[ENV] Edge CDP 3次启动均未就绪"], "env_fail": True}
    try:
        out2 = fr._ui_gate1_with_baseline(
            dict(res), repo_root=os.path.join(case, "live"), page="dashboard/v2.html",
            out_dir=os.path.join(case, "runner_a1_base2"), six_baseline={}, main_sha_before="")
    finally:
        fr.probe_ui_dashboard = real_probe
    check("G11 生产页基线探针自己起不来 → 不装作区分过：失败项原样留着，写明基线没跑成",
          out2["all_passed"] is False and out2["failures"] == [soft, wall_s, afl, new]
          and out2["baseline_failures"] is None and out2["checks"]["gate1_baseline"]["ran"] is False,
          str(out2.get("checks"))[:240])

    ksrc_path = os.path.join(HERE, "factory_runner.py")
    tree = ast.parse(read(ksrc_path).replace("\r\n", "\n"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute_job")
    called = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    check("G11 execute_job 里接了线：ui 单硬毛病后调用 1 次生产页基线",
          called.count("_ui_gate1_with_baseline") == 1, str(called.count("_ui_gate1_with_baseline")))


def g12(case):
    """2026-11-26：code 单剔生产页既有毛病与 ui 单同一口径——正文墙按计数 + 漂移容差判，不再逐字比对。
    G57 实况：沙箱 5841 / 生产页 59xx，逐字比对对不上 → 生产页自带的正文墙算到本单头上，白开一轮去砍首页文字。"""
    wall_s = "首屏默认可见正文 5841 字符过多 (上限 4500，文字墙未压密)"
    wall_b = "首屏默认可见正文 5928 字符过多 (上限 4500，文字墙未压密)"
    wall_bad = "首屏默认可见正文 7000 字符过多 (上限 4500，文字墙未压密)"
    afl = "[断言 FAIL · R7-1/F1] 交付时间在 @ 390 状态=default：实测 —，期望 contains demo"
    contrast = "页面色彩对比度不足（AA）3 处"
    got = fr._rt_preexisting({"failures": [wall_s], "baseline_failures": [wall_b]})
    check("G12 code 单：沙箱正文墙计数不高于生产页 + 容差 → 生产页本来就有，不计本单（与 ui 单同一口径）",
          got == [wall_s] and got == fr._ui_preexisting({"failures": [wall_s], "baseline_failures": [wall_b]}), str(got))
    mixed = {"failures": [contrast, wall_s], "baseline_failures": [wall_b]}
    check("G12 比生产页明显更差（超出 500 字容差）/ 基线里没有的毛病 → 照样算本单的；断言 FAIL 即使生产页也不过；没基线不剔",
          fr._rt_preexisting({"failures": [wall_bad], "baseline_failures": [wall_b]}) == []
          and fr._rt_preexisting(dict(mixed)) == [wall_s]
          and fr._rt_preexisting({"failures": [afl, wall_s], "baseline_failures": [wall_b, afl]}) == [wall_s]
          and fr._rt_preexisting({"failures": [wall_s], "baseline_failures": None}) == [],
          str(fr._rt_preexisting(dict(mixed))))
    code = {"all_passed": True, "checks": {"files_changed": [{"path": "scripts/lane_status.py", "added": 302, "deleted": 45}]},
            "failures": [], "screenshots": [], "strict_checks": {}}
    res = fr._merge_code_runtime(code, dict(_z_ui("PASS", all_passed=False, failures=[wall_s]), baseline_failures=[wall_b]))
    ev = res["checks"]["runtime_evidence"]
    check("G12 G57 第 2 轮复现：断言全过、只剩生产页自带的正文墙 → Gate1 不判 FAIL，记档交质检",
          res["all_passed"] is True and res["failures"] == [] and ev["preexisting_failures"] == [wall_s]
          and ev["assert_summary"]["pass"] == 1, "%s | %s" % (res["failures"], str(ev)[:200]))
    res = fr._merge_code_runtime(code, dict(_z_ui("PASS", all_passed=False, failures=[wall_bad, afl]), baseline_failures=[wall_b]))
    ev = res["checks"]["runtime_evidence"]
    check("G12 本单把正文墙做得比生产页更差 + 断言不过 → 两条都判 FAIL（原顺序），不记既有",
          res["all_passed"] is False and len(res["failures"]) == 2 and wall_bad in res["failures"][0] and afl in res["failures"][1]
          and all(f.startswith("[看板真引擎] ") for f in res["failures"]) and "preexisting_failures" not in ev, str(res["failures"])[:260])
    ksrc_path = os.path.join(HERE, "factory_runner.py")
    tree = ast.parse(read(ksrc_path).replace("\r\n", "\n"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute_job")
    evs = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "append_v3_event"
           and len(n.args) >= 3 and isinstance(n.args[2], ast.Constant) and n.args[2].value == "gate1_baseline"]
    check("G12 execute_job 里接了线：gate1_baseline 事件 ui 单 1 处 + code 单 2 处（剔了既有 / 基线探针没跑成）", len(evs) == 3, str(len(evs)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    base = tempfile.mkdtemp(prefix="v5real_")
    cases = [("M1", m1), ("M2", m2), ("M3", m3), ("M4", m4), ("M5", m5),
             ("C1", c1), ("C2", c2), ("C3", c3), ("C4", c4), ("C5", c5), ("C6", c6),
             ("E1", e1), ("E2", e2), ("E3", e3), ("E4", e4), ("E5", e5),
             ("V1", v1), ("V2", v2), ("V3", v3),
             ("H1", h1), ("H2", h2),
             ("N1", n1), ("O1", o1), ("S1", s1), ("W1", w1), ("T1", t1), ("R1", r1), ("K1", k1), ("G2", g2),
             ("G3", g3), ("G4", g4), ("G5", g5), ("G6", g6), ("G7", g7), ("G8", g8), ("G9", g9), ("G10", g10),
             ("G11", g11), ("G12", g12)]
    try:
        for name, fn in cases:
            case = os.path.join(base, name.lower())
            os.makedirs(case, exist_ok=True)
            try:
                fn(case)
            except Exception as exc:  # noqa: BLE001 - 用例崩了也算失败，别打断后面的
                RESULTS.append(("FAIL", "%s 用例自身崩溃" % name, "%s: %s" % (type(exc).__name__, exc)))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    n_pass = sum(1 for r in RESULTS if r[0] == "PASS")
    n_fail = len(RESULTS) - n_pass
    if args.json:
        print(json.dumps({"total": len(RESULTS), "passed": n_pass, "failed": n_fail,
                          "cases": [{"case": r[1], "status": r[0], "detail": r[2][:300]} for r in RESULTS]},
                         ensure_ascii=False))
        return 0 if n_fail == 0 else 1
    print("PASS %d/%d" % (n_pass, len(RESULTS)))
    for st, name, detail in RESULTS:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail[:300]) if (st == "FAIL" and detail) else ""))
    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
