# -*- coding: utf-8 -*-
"""幽灵派单探针用例（INC-20261118-007）。

覆盖：
  1 probe：tasks 行 + assistant token 输出 -> healthy
  2 probe：tasks 表 0 行（UI 不可见）-> ghost
  3 probe：有 tasks 行但 assistant 全零输出 -> ghost
  4 probe：库缺失/不可读 -> unknown（不误判）
  5 cycle：dispatched 超宽限 + 探针 ghost -> check/level=replace/ghost=true
  6 cycle：探针 healthy -> 不产生动作
  7 cycle：探针 unknown -> 不误判
  8 cycle：未到宽限期 -> 不探针
  9 verify_dispatch：healthy/ghost/stalled；卡片 session 不一致给出告警
用法：python -X utf8 test_dispatch_verify.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import session_probe  # noqa: E402
import steward_cycle as sc  # noqa: E402
import verify_dispatch as vd  # noqa: E402
from fingerprint import announce, arm  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


_SEQ = {"n": 0}


def make_dbs(tmp: str, session: str, tasks_row: int, assistant_output: int,
             messages: int | None = None, last_age_sec: int = 0):
    _SEQ["n"] += 1
    tag = _SEQ["n"]
    tasks_db = os.path.join(tmp, "tasks-%d.sqlite" % tag)
    cli_db = os.path.join(tmp, "cli-%d.sqlite" % tag)
    con = sqlite3.connect(tasks_db)
    con.execute("create table tasks (task_id text)")
    for _ in range(tasks_row):
        con.execute("insert into tasks values (?)", (session,))
    con.commit()
    con.close()
    con = sqlite3.connect(cli_db)
    con.execute("create table message (session_id text, sequence integer, data text)")
    rows = [
        {"role": "assistant", "tokens": {"output": 0, "total": 0}, "semantics": {"origin": "system"}},
        {"role": "user", "tokens": {}},
    ]
    for _ in range(assistant_output):
        rows.append({"role": "assistant", "tokens": {"output": 12, "total": 120}})
    while messages is not None and len(rows) < messages:
        rows.append({"role": "user", "tokens": {}})
    import time as _time
    stamp = int((_time.time() - last_age_sec) * 1000)
    for seq, data in enumerate(rows):
        data.setdefault("time", {"created": stamp})
        con.execute("insert into message values (?,?,?)", (session, seq, json.dumps(data)))
    con.commit()
    con.close()
    return cli_db, tasks_db


def cfg(**kw):
    base = {"max_running": 3, "max_running_per_project": 1, "stale_sec": 1800,
            "max_attempts": 3, "max_pulls": 3, "daily_budget_cny": 0,
            "idle_alert_sec": 7200, "dispatch_grace_sec": 300}
    base.update(kw)
    return {"dispatch": base}


def task(tid, state="dispatched", **kw):
    base = {
        "id": tid, "title": "任务 %s" % tid, "goal": "完成 %s" % tid,
        "acceptance": "证据带 EXIT=PASS", "project": "mate-lib-ai", "priority": 1,
        "state": state, "sessionId": "sess_" + tid, "attempts": 1,
        "created_at": "2026-11-18T11:00:00+08:00",
        "updated_at": "2026-11-18T11:50:00+08:00",
        "last_progress_at": "2026-11-18T11:50:00+08:00",
    }
    base.update(kw)
    return base


def main() -> int:
    fp = announce("test_dispatch_verify")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_dispatch_verify")
    now = sc.now_dt("2026-11-18T12:00:00+08:00")
    tmp = tempfile.mkdtemp(prefix="disp-verify-")
    try:
        sid = "sess_ghost_1"
        cli, tasks = make_dbs(tmp, sid, 1, 2)
        p = session_probe.probe(sid, cli, tasks)
        check("probe-healthy", session_probe.verdict(p) == "healthy", str(p))

        cli2, tasks2 = make_dbs(tmp, sid, 0, 0)
        p = session_probe.probe(sid, cli2, tasks2)
        check("probe-no-tasks-row", session_probe.verdict(p) == "ghost"
              and p["tasks_row"] == 0, str(p))

        cli3, tasks3 = make_dbs(tmp, sid, 1, 0)
        p = session_probe.probe(sid, cli3, tasks3)
        check("probe-no-output", session_probe.verdict(p) == "ghost"
              and p["assistant_with_output"] == 0, str(p))

        p = session_probe.probe(sid, os.path.join(tmp, "missing.sqlite"), tasks3)
        check("probe-unknown", session_probe.verdict(p) == "unknown", str(p))

        # 5 幽灵 -> replace
        reg = {"tasks": [task("W-T-GHOST")]}
        out = sc.build(reg, cfg(), now, None, use_kb=False, materialize=False,
                       session_probe=lambda s: {"tasks_row": 0, "assistant_with_output": 0,
                                                "messages": 3, "error": None})
        acts = [a for a in out["actions"] if a.get("task_id") == "W-T-GHOST"]
        check("cycle-ghost-replace", len(acts) == 1 and acts[0]["level"] == "replace"
              and acts[0].get("ghost") is True, json.dumps(acts, ensure_ascii=False)[:200])

        # 6 健康 -> 无动作
        out = sc.build({"tasks": [task("W-T-OK")]}, cfg(), now, None, use_kb=False,
                       materialize=False,
                       session_probe=lambda s: {"tasks_row": 1, "assistant_with_output": 5,
                                                "messages": 20, "error": None})
        acts = [a for a in out["actions"] if a.get("task_id") == "W-T-OK"]
        check("cycle-healthy-silent", acts == [], str(acts))

        # 7 探针不可读 -> 不误判
        out = sc.build({"tasks": [task("W-T-UNK")]}, cfg(), now, None, use_kb=False,
                       materialize=False,
                       session_probe=lambda s: {"error": "db-locked"})
        acts = [a for a in out["actions"] if a.get("task_id") == "W-T-UNK"]
        check("cycle-unknown-silent", acts == [], str(acts))

        # 8 未到宽限期 -> 不探针（计数为 0）
        calls = {"n": 0}

        def counting(_sid):
            calls["n"] += 1
            return {"tasks_row": 0, "assistant_with_output": 0, "error": None}
        young = task("W-T-YOUNG", last_progress_at="2026-11-18T11:59:00+08:00",
                     updated_at="2026-11-18T11:59:00+08:00")
        out = sc.build({"tasks": [young]}, cfg(), now, None, use_kb=False,
                       materialize=False, session_probe=counting)
        check("cycle-grace-skips", calls["n"] == 0
              and not [a for a in out["actions"] if a.get("task_id") == "W-T-YOUNG"], str(calls))

        # 9 verify_dispatch
        reg_path = os.path.join(tmp, "registry.json")
        json.dump({"tasks": [{"id": "W-T-VD", "sessionId": sid}]},
                  open(reg_path, "w", encoding="utf-8"))
        good = vd.run("W-T-VD", sid, 0, 0, reg_path, cli, tasks)
        check("vd-healthy", good["verdict"] == "healthy" and not good["session_mismatch"],
              str(good["verdict"]))
        bad = vd.run("W-T-VD", sid, 0, 0, reg_path, cli2, tasks2)
        check("vd-ghost", bad["verdict"] == "ghost", str(bad["verdict"]))
        cli4, tasks4 = make_dbs(tmp, sid, 1, 2, last_age_sec=600)
        stalled = vd.run("W-T-VD", sid, 0, 0, reg_path, cli4, tasks4, max_idle=180)
        check("vd-stalled", stalled["verdict"] == "stalled"
              and stalled["probe"]["idle_sec"] > 180, str(stalled["probe"]["idle_sec"]))
        mis = vd.run("W-T-VD", "sess_other", 0, 0, reg_path, cli, tasks)
        check("vd-mismatch-flag", mis["session_mismatch"] is True, str(mis))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    ok = 0
    for status, name, detail in RESULTS:
        print("[%s] %-24s %s" % (status, name, detail))
        ok += status == "PASS"
    print("| %d passed / %d total" % (ok, len(RESULTS)))
    print("EXIT=%d" % (0 if ok == len(RESULTS) else 2))
    return 0 if ok == len(RESULTS) else 2


if __name__ == "__main__":
    sys.exit(main())
