# -*- coding: utf-8 -*-
"""worker_watch 边界用例：健康/停滞/连续失败/重复签名/坏数据一个都不能形同虚设。

覆盖 20 个用例：
  1  healthy         在办会话 + 近期 completed -> ok，退出码 0，字段齐全
  2  stall-critical   idle>=crit_idle -> critical，退出码 3，建议重启派单
  3  stall-warn      warn<=idle<crit -> warn，退出码 1
  4  running-stale   最后一条工具 running 且超 crit_idle -> critical（独立原因串）
  5  fail-streak-crit 连续 crit_fail 次 error -> critical
  6  fail-streak-warn fail_streak=warn_fail（新→旧 error,error,completed）-> warn
  7  single-error     最后一条 error 但 fail_streak=1 -> warn
  8  repeat-crit      同一签名连续 crit_repeat 次 -> critical
  9  repeat-nonconsec 同签名非连续出现 -> repeat_hits=1，不算打转
 10  bad-data         坏 JSON/非对象/无 state/input 为 null/data 为空 -> 不崩且字段稳定
 11  json-fields      --json 的 sessions[] 七个必备字段类型稳定，顶层 ok/thresholds 齐全
 12  thresholds       --warn-idle/--crit-idle/--warn-repeat/--crit-repeat 可 CLI 覆盖改判
 13  session-filter   --session 只出指定会话；未知会话 -> NO-DATA critical
 14  active-filter    默认只盯在办；--all 含旧会话；空 part 表 -> sessions=0 rc 0
 15  readonly+det     跑前后夹具 sha256 不变且不新生 -wal/-shm；--now 钉死结果确定
 16  done-card-skip   registry 卡 done 默认跳过；--include-closed 恢复真实档位
 17  task-closed-skip 卡仍 dispatched 但 task_status=completed -> 跳过（presence 残留也跳）
 18  ghost-probe      活跃卡+无 presence+task 未收口 -> ghost_suspect 且过线升档
 19  card-state-filter --card-state 白名单只留目标卡状态，其余记 skipped
 20  （15 拆为 empty+readonly 与 now-determinism 两例，合计 20 次执行）

用法：python -X utf8 test_worker_watch.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import worker_watch as ww  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []

NOW = 1795000300000  # 固定"现在"（epoch ms），保证 idle_sec 确定
ALERTS = {"ok", "warn", "critical"}
REQUIRED = ("session_id", "idle_sec", "last_tool", "last_status",
            "fail_streak", "repeat_hits", "alert")


def sha256(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def case(name: str, fn) -> None:
    try:
        ok, msg = fn()
    except Exception as e:  # noqa: BLE001 用例自身崩了也算 FAIL，要看见栈
        ok, msg = False, "EXC %s: %s" % (type(e).__name__, e)
    RESULTS.append((name, "PASS" if ok else "FAIL", msg))
    print("[%s] %-18s %s" % ("PASS" if ok else "FAIL", name, msg))


def check(ok: bool, msg: str) -> tuple[bool, str]:
    return (bool(ok), msg)


# ---------------------------------------------------------------- 夹具

def part(session: str, t_ms: int, data, seq: int = 0, updated: int | None = None) -> tuple:
    """一行 part；data 为 dict 时序列化，为 str/None 时原样入库（坏数据用例）。"""
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False)
    return (session, t_ms, updated if updated is not None else t_ms, data, seq)


def make_db(path: str, rows: list[tuple]) -> str:
    con = sqlite3.connect(path)
    con.execute("create table part (id text primary key, message_id text, session_id text, "
                "time_created integer, time_updated integer, data text, sequence integer)")
    con.executemany("insert into part (id, message_id, session_id, time_created, "
                    "time_updated, data, sequence) values (?,?,?,?,?,?,?)",
                    [("p%d" % i, "m%d" % i, r[0], r[1], r[2], r[3], r[4])
                     for i, r in enumerate(rows)])
    con.commit()
    con.close()
    return path


def tool(name: str, status: str, cmd: str | None = "echo hi") -> dict:
    return {"type": "tool", "tool": name,
            "state": {"status": status, "input": {"command": cmd}}}


def add_presence(db_path: str, session_ids: list[str]) -> None:
    con = sqlite3.connect(db_path)
    con.execute("create table if not exists session_runtime_presence "
                "(session_id text, owner_id text, process_id integer)")
    con.executemany("insert into session_runtime_presence values (?,?,?)",
                    [(sid, "test-owner", 1234) for sid in session_ids])
    con.commit()
    con.close()


def make_registry(path: str, cards: list[dict]) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"tasks": cards}, f, ensure_ascii=False)
    return path


def make_tasks_db(path: str, rows: list[tuple]) -> str:
    con = sqlite3.connect(path)
    con.execute("create table tasks (task_id text, task_status text, updated_at integer)")
    con.executemany("insert into tasks values (?,?,?)", rows)
    con.commit()
    con.close()
    return path


def run_json(db: str, extra: list[str] | None = None) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ww.main(["--db", db, "--json", "--now", str(NOW)] + (extra or []))
    return rc, json.loads(buf.getvalue())


def run_text(db: str, extra: list[str] | None = None) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ww.main(["--db", db, "--now", str(NOW)] + (extra or []))
    return rc, buf.getvalue()


def one_session(payload: dict) -> dict:
    sess = payload["sessions"]
    if len(sess) != 1:
        raise AssertionError("期望 1 个会话，实得 %d" % len(sess))
    return sess[0]


def ago(sec: float) -> int:
    return NOW - int(sec * 1000)


# ---------------------------------------------------------------- 用例

def c_healthy():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(60), {"type": "step-start"}),
            part("s1", ago(30), tool("Bash", "completed")),
            part("s1", ago(10), {"type": "text", "text": "正在改 worker_watch.py"}),
        ])
        rc, out = run_text(db)
        s = None
        rc2, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if rc != 0 or rc2 != 0:
            errs.append("rc=%d/%d 期望 0" % (rc, rc2))
        if s["alert"] != "ok":
            errs.append("alert=%s 期望 ok" % s["alert"])
        if s["idle_sec"] != 10:
            errs.append("idle_sec=%r 期望 10" % s["idle_sec"])
        if (s["last_tool"], s["last_status"]) != ("Bash", "completed"):
            errs.append("last=%s/%s" % (s["last_tool"], s["last_status"]))
        if s["fail_streak"] or s["repeat_hits"] != 1:
            errs.append("fail=%d repeat=%d" % (s["fail_streak"], s["repeat_hits"]))
        if "worker_watch" not in s["last_text"]:
            errs.append("last_text=%r" % s["last_text"])
        if not out.startswith("sess s1"):
            errs.append("人读输出缺 sess 行: %r" % out[:40])
        return check(not errs, "; ".join(errs) or "ok 档：idle=10s last=Bash/completed fail=0 repeat=1")


def c_stall_critical():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(5000), tool("Bash", "completed")),
            part("s1", ago(1000), {"type": "text", "text": "卡住了"}),
        ])
        rc, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if rc != 3:
            errs.append("rc=%d 期望 3" % rc)
        if s["alert"] != "critical" or s["idle_sec"] != 1000:
            errs.append("alert=%s idle=%r" % (s["alert"], s["idle_sec"]))
        if not any("idle=1000s" in r for r in s["reasons"]):
            errs.append("reasons=%r" % s["reasons"])
        if not any("重启派单" in a for a in s["advice"]):
            errs.append("advice=%r" % s["advice"])
        return check(not errs, "; ".join(errs) or "critical：idle=1000s≥900s，建议重启派单")


def c_stall_warn():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(400), tool("Read", "completed", cmd=None)),
        ])
        rc, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if rc != 1:
            errs.append("rc=%d 期望 1" % rc)
        if s["alert"] != "warn":
            errs.append("alert=%s" % s["alert"])
        if not any("idle=400s≥warn 300s" in r for r in s["reasons"]):
            errs.append("reasons=%r" % s["reasons"])
        return check(not errs, "; ".join(errs) or "warn：idle=400s 落在 [300,900)")


def c_running_stale():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(2000), tool("Bash", "completed")),
            part("s1", ago(950), tool("Bash", "running", cmd="sleep 999")),
        ])
        rc, payload = run_json(db, ["--crit-idle", "800"])
        s = one_session(payload)
        errs = []
        if rc != 3 or s["alert"] != "critical":
            errs.append("rc=%d alert=%s" % (rc, s["alert"]))
        if not any("running" in r for r in s["reasons"]):
            errs.append("reasons=%r" % s["reasons"])
        if s["last_status"] != "running":
            errs.append("last_status=%s" % s["last_status"])
        return check(not errs, "; ".join(errs) or "critical：tool running 超时 950s（crit 线降到 800）")


def c_fail_streak_crit():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(300), tool("Bash", "completed")),
            part("s1", ago(20), tool("Bash", "error", cmd="bad --x")),
            part("s1", ago(10), tool("Bash", "error", cmd="bad --x")),
            part("s1", ago(5), tool("Edit", "error", cmd=None)),
        ])
        rc, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if rc != 3 or s["alert"] != "critical":
            errs.append("rc=%d alert=%s" % (rc, s["alert"]))
        if s["fail_streak"] != 3:
            errs.append("fail_streak=%d 期望 3" % s["fail_streak"])
        if s["last_tool"] != "Edit":
            errs.append("last_tool=%s" % s["last_tool"])
        if not any("fail_streak=3" in r for r in s["reasons"]):
            errs.append("reasons=%r" % s["reasons"])
        return check(not errs, "; ".join(errs) or "critical：fail_streak=3≥3（旧的 completed 打断计数）")


def c_fail_streak_warn():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(300), tool("Bash", "completed")),
            part("s1", ago(20), tool("Bash", "error", cmd="bad")),
            part("s1", ago(10), tool("Bash", "error", cmd="bad")),
        ])
        rc, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if rc != 1 or s["alert"] != "warn":
            errs.append("rc=%d alert=%s" % (rc, s["alert"]))
        if s["fail_streak"] != 2:
            errs.append("fail_streak=%d 期望 2" % s["fail_streak"])
        return check(not errs, "; ".join(errs) or "warn：fail_streak=2 落在 [2,3)")


def c_single_error():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(100), tool("Bash", "error", cmd="bad")),
            part("s1", ago(10), {"type": "text", "text": "重试中"}),
        ])
        rc, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if rc != 1 or s["alert"] != "warn":
            errs.append("rc=%d alert=%s" % (rc, s["alert"]))
        if s["fail_streak"] != 1 or s["last_status"] != "error":
            errs.append("fail=%d status=%s" % (s["fail_streak"], s["last_status"]))
        if not any("last_status=error" in r for r in s["reasons"]):
            errs.append("reasons=%r" % s["reasons"])
        return check(not errs, "; ".join(errs) or "warn：末条 error、fail_streak=1")


def c_repeat_crit():
    with tempfile.TemporaryDirectory() as tmp:
        rows = [part("s1", ago(60), {"type": "text", "text": "loop"})]
        for i in range(5):  # 新→旧同一命令 5 次
            rows.append(part("s1", ago(50 - i * 5), tool("Bash", "completed", cmd="pytest -q")))
        db = make_db(os.path.join(tmp, "f.sqlite"), rows)
        rc, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if rc != 3 or s["alert"] != "critical":
            errs.append("rc=%d alert=%s" % (rc, s["alert"]))
        if s["repeat_hits"] != 5:
            errs.append("repeat_hits=%d 期望 5" % s["repeat_hits"])
        if "pytest -q" not in s["repeat_sig"]:
            errs.append("repeat_sig=%r" % s["repeat_sig"])
        if not any("死循环" in a for a in s["advice"]):
            errs.append("advice=%r" % s["advice"])
        return check(not errs, "; ".join(errs) or "critical：同签名连续 5 次 ≥5")


def c_repeat_nonconsec():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(30), tool("Bash", "completed", cmd="A")),
            part("s1", ago(20), tool("Bash", "completed", cmd="B")),
            part("s1", ago(10), tool("Bash", "completed", cmd="A")),
        ])
        rc, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if rc != 0 or s["alert"] != "ok":
            errs.append("rc=%d alert=%s 期望 ok" % (rc, s["alert"]))
        if s["repeat_hits"] != 1:
            errs.append("repeat_hits=%d 期望 1（非连续不算打转）" % s["repeat_hits"])
        return check(not errs, "; ".join(errs) or "ok：A,B,A 非连续 repeat_hits=1")


def c_bad_data():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(120), "not a json at all"),
            part("s1", ago(110), "[1,2,3]"),
            part("s1", ago(100), ""),
            part("s1", ago(95), None),
            part("s1", ago(90), {"type": "tool", "tool": "Bash"}),  # 无 state
            part("s1", ago(80), tool("Bash", "error", cmd=None)),   # input 为 null
            part("s1", ago(10), {"type": "text", "text": "  "}),    # 纯空白 text
        ])
        rc, payload = run_json(db)
        s = one_session(payload)
        errs = []
        if s["alert"] not in ALERTS:
            errs.append("alert=%r 非法" % s["alert"])
        for f in REQUIRED:
            if f not in s:
                errs.append("缺字段 %s" % f)
        if s["last_tool"] != "Bash":
            errs.append("last_tool=%r" % s["last_tool"])
        if s["fail_streak"] != 1:
            errs.append("fail_streak=%d 期望 1（null input 的 error 仍计数）" % s["fail_streak"])
        if s["idle_sec"] != 10:
            errs.append("idle_sec=%r 期望 10（空白 text 仍是最后活动）" % s["idle_sec"])
        if s["last_text"] != "":
            errs.append("last_text=%r 期望空串" % s["last_text"])
        return check(not errs, "; ".join(errs) or "坏数据不崩：7 行脏数据 + 字段稳定")


def c_json_fields():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(10), tool("Bash", "completed")),
            part("s2", ago(1000), tool("Read", "completed", cmd=None)),
            part("s3", ago(5), tool("Edit", "error", cmd=None)),
            part("s3", ago(4), tool("Edit", "error", cmd=None)),
            part("s3", ago(3), tool("Edit", "error", cmd=None)),
        ])
        rc, payload = run_json(db)
        errs = []
        if rc != 3:
            errs.append("rc=%d 期望 3（有 critical）" % rc)
        for k in ("ok", "checked_at", "db", "thresholds", "sessions"):
            if k not in payload:
                errs.append("顶层缺 %s" % k)
        if payload.get("ok") is not False:
            errs.append("ok=%r 期望 False" % payload.get("ok"))
        if len(payload.get("sessions", [])) != 3:
            errs.append("sessions=%d 期望 3" % len(payload.get("sessions", [])))
        for s in payload.get("sessions", []):
            for f in REQUIRED:
                if f not in s:
                    errs.append("%s 缺 %s" % (s.get("session_id"), f))
            if s["alert"] not in ALERTS:
                errs.append("%s alert=%r" % (s.get("session_id"), s["alert"]))
            if not isinstance(s["idle_sec"], int):
                errs.append("%s idle_sec 类型 %r" % (s.get("session_id"), s["idle_sec"]))
            if not isinstance(s["fail_streak"], int) or not isinstance(s["repeat_hits"], int):
                errs.append("%s fail/repeat 非 int" % s.get("session_id"))
        th = payload.get("thresholds", {})
        for k in ("warn_idle", "crit_idle", "warn_fail", "crit_fail", "warn_repeat", "crit_repeat"):
            if k not in th:
                errs.append("thresholds 缺 %s" % k)
        by_id = {s["session_id"]: s for s in payload["sessions"]}
        if by_id.get("s1", {}).get("alert") != "ok" or by_id.get("s3", {}).get("alert") != "critical":
            errs.append("分级错: %r" % {k: v["alert"] for k, v in by_id.items()})
        return check(not errs, "; ".join(errs) or "--json：3 会话 ok/critical 分级 + 7 字段类型稳定")


def c_thresholds():
    with tempfile.TemporaryDirectory() as tmp:
        idle_db = make_db(os.path.join(tmp, "idle.sqlite"), [
            part("s1", ago(400), tool("Bash", "completed", cmd="A")),
        ])
        rep_db = make_db(os.path.join(tmp, "rep.sqlite"), [
            part("s2", ago(30), tool("Bash", "completed", cmd="A")),
            part("s2", ago(20), tool("Bash", "completed", cmd="A")),
            part("s2", ago(10), tool("Bash", "completed", cmd="A")),
        ])
        errs = []
        rc0, base = run_json(idle_db)
        if rc0 != 1 or base["sessions"][0]["alert"] != "warn":
            errs.append("默认 idle 400 应 warn: rc=%d" % rc0)
        rc1, hot = run_json(idle_db, ["--warn-idle", "100", "--crit-idle", "200"])
        if rc1 != 3 or hot["sessions"][0]["alert"] != "critical":
            errs.append("收紧 idle 阈值未改判: rc=%d alert=%s" % (rc1, hot["sessions"][0]["alert"]))
        rc2, cold = run_json(idle_db, ["--warn-idle", "1000", "--crit-idle", "2000"])
        if rc2 != 0 or cold["sessions"][0]["alert"] != "ok":
            errs.append("放宽 idle 阈值未改判: rc=%d" % rc2)
        rc3, rep = run_json(rep_db, ["--warn-repeat", "2", "--crit-repeat", "3"])
        if rc3 != 3 or rep["sessions"][0]["repeat_hits"] != 3:
            errs.append("repeat 阈值覆盖失效: rc=%d hits=%d" % (rc3, rep["sessions"][0]["repeat_hits"]))
        rc4, _ = run_text(idle_db, ["--warn-idle", "1000", "--crit-idle", "100"])
        if rc4 != 2:
            errs.append("warn>crit 未拦: rc=%d 期望 2" % rc4)
        rc5, _ = run_text(idle_db, ["--window", "0"])
        if rc5 != 2:
            errs.append("--window 0 未拦: rc=%d 期望 2" % rc5)
        return check(not errs, "; ".join(errs) or "阈值可 CLI 覆盖：warn→critical→ok，repeat 线生效，非法参数 rc2")


def c_session_filter():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("ok1", ago(10), tool("Bash", "completed")),
            part("dead1", ago(1000), tool("Bash", "completed")),
        ])
        errs = []
        rc, payload = run_json(db, ["--session", "ok1"])
        if rc != 0 or [s["session_id"] for s in payload["sessions"]] != ["ok1"]:
            errs.append("--session ok1: rc=%d ids=%r" % (rc, [s["session_id"] for s in payload["sessions"]]))
        rc, payload = run_json(db, ["--session", "ghost"])
        s = one_session(payload)
        if rc != 3 or s["alert"] != "critical" or s["reasons"] != ["NO-DATA"]:
            errs.append("幽灵会话: rc=%d alert=%s reasons=%r" % (rc, s["alert"], s["reasons"]))
        if not any("核对 session_id" in a for a in s["advice"]):
            errs.append("幽灵 advice=%r" % s["advice"])
        return check(not errs, "; ".join(errs) or "--session 精确过滤；未知会话 NO-DATA critical")


def c_active_filter():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("live", ago(10), tool("Bash", "completed")),
            part("old", ago(3 * 3600), tool("Bash", "completed")),
        ])
        errs = []
        rc, payload = run_json(db)
        ids = [s["session_id"] for s in payload["sessions"]]
        if rc != 0 or ids != ["live"]:
            errs.append("默认在办过滤: rc=%d ids=%r" % (rc, ids))
        if not any("已跳过 1 个" in w for w in payload["warnings"]):
            errs.append("缺跳过提示: %r" % payload["warnings"])
        rc, payload = run_json(db, ["--all"])
        ids = sorted(s["session_id"] for s in payload["sessions"])
        by_id = {s["session_id"]: s for s in payload["sessions"]}
        if rc != 3 or ids != ["live", "old"]:
            errs.append("--all: rc=%d ids=%r" % (rc, ids))
        if by_id.get("old", {}).get("alert") != "critical":
            errs.append("--all 的旧会话应按 idle 判 critical: %r" % by_id.get("old"))
        rc, payload = run_json(db, ["--active-min", "30"])
        ids = [s["session_id"] for s in payload["sessions"]]
        if rc != 0 or ids != ["live"]:
            errs.append("--active-min 30: rc=%d ids=%r" % (rc, ids))
        return check(not errs, "; ".join(errs) or "默认只盯在办（近60min），--all/--active-min 可调")


def c_empty_and_readonly():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [])
        rc, payload = run_json(db)
        errs = []
        if rc != 0 or payload["sessions"] != []:
            errs.append("空 part 表: rc=%d sessions=%r" % (rc, payload["sessions"]))
        before = sha256(db)
        listing_before = sorted(os.listdir(tmp))
        db2 = make_db(os.path.join(tmp, "g.sqlite"), [
            part("s1", ago(10), tool("Bash", "completed")),
            part("s2", ago(1000), tool("Bash", "error", cmd="x")),
        ])
        b2 = sha256(db2)
        run_json(db2)
        run_text(db2, ["--all"])
        if sha256(db2) != b2:
            errs.append("只读被破坏: %s -> %s" % (b2, sha256(db2)))
        new_files = sorted(set(os.listdir(tmp)) - set(listing_before) - {"g.sqlite"})
        if new_files:
            errs.append("新生副文件: %r" % new_files)
        rc, _ = run_text(os.path.join(tmp, "none.sqlite"))
        if rc != 2:
            errs.append("缺库 rc=%d 期望 2" % rc)
        return check(not errs, "; ".join(errs) or "空表 rc0；sha256 前后一致无副文件；缺库 rc2")


def c_now_determinism():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s1", ago(10), tool("Bash", "completed")),
            part("s2", ago(700), tool("Bash", "completed")),
        ])
        errs = []
        _, a = run_json(db)
        _, b = run_json(db)
        if a["sessions"] != b["sessions"]:
            errs.append("同 --now 两次结果不一致")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ww.main(["--db", db, "--json", "--now", str(NOW + 5000)])
        c = json.loads(buf.getvalue())
        m = {s["session_id"]: s["idle_sec"] for s in c["sessions"]}
        if m.get("s1") != 15 or m.get("s2") != 705:
            errs.append("--now 平移 5s 后 idle=%r 期望 s1=15 s2=705" % m)
        return check(not errs, "; ".join(errs) or "--now 钉死：重复跑一致，+5s 后 idle 同步 +5")


def c_done_card_skip():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s_done", ago(1000), tool("Bash", "completed")),
        ])
        add_presence(db, [])
        reg = make_registry(os.path.join(tmp, "registry.json"), [
            {"id": "W-DONE", "sessionId": "s_done", "state": "done"}])
        tasks = make_tasks_db(os.path.join(tmp, "tasks.sqlite"),
                              [("s_done", "completed", NOW)])
        rc, payload = run_json(db, ["--registry", reg, "--tasks-db", tasks])
        errs = []
        if rc != 0 or payload["sessions"] != []:
            errs.append("done 卡未跳过: rc=%d sessions=%r" % (rc, payload["sessions"]))
        if len(payload.get("skipped", [])) != 1 or "card_state=done" not in payload["skipped"][0]["reason"]:
            errs.append("skipped=%r" % payload.get("skipped"))
        rc2, p2 = run_json(db, ["--registry", reg, "--tasks-db", tasks, "--include-closed"])
        if rc2 != 3 or not p2["sessions"] or p2["sessions"][0]["alert"] != "critical":
            errs.append("--include-closed 未恢复真实档位: rc=%d" % rc2)
        return check(not errs, "; ".join(errs) or "done 卡默认跳过（skipped=1, rc0）；--include-closed 时仍按 idle 判 critical")

def c_task_closed_skip():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s_closed", ago(1000), tool("Bash", "completed")),
            part("s_closed2", ago(1000), tool("Bash", "completed")),
        ])
        add_presence(db, ["s_closed2"])  # 残留 presence 也不能压过 task_status=completed
        reg = make_registry(os.path.join(tmp, "registry.json"), [
            {"id": "W-ACTIVE", "sessionId": "s_closed", "state": "dispatched"},
            {"id": "W-ACTIVE2", "sessionId": "s_closed2", "state": "dispatched"}])
        tasks = make_tasks_db(os.path.join(tmp, "tasks.sqlite"),
                              [("s_closed", "completed", NOW),
                               ("s_closed2", "completed", NOW)])
        rc, payload = run_json(db, ["--registry", reg, "--tasks-db", tasks])
        errs = []
        if rc != 0 or payload["sessions"] != []:
            errs.append("task_status=completed 未跳过: rc=%d sessions=%r" % (rc, payload["sessions"]))
        skipped = {x.get("session_id"): x for x in payload.get("skipped", [])}
        if set(skipped) != {"s_closed", "s_closed2"}:
            errs.append("skipped=%r" % payload.get("skipped"))
        elif any("task_status=completed" not in skipped[s]["reason"] for s in skipped):
            errs.append("reason=%r" % skipped)
        return check(not errs, "; ".join(errs) or "卡仍 dispatched 但 task_status=completed -> 跳过（无论 presence 是否残留）")

def c_ghost_probe():
    with tempfile.TemporaryDirectory() as tmp:
        reg = make_registry(os.path.join(tmp, "registry.json"), [
            {"id": "W-GHOST", "sessionId": "s_ghost", "state": "dispatched"}])
        tasks = make_tasks_db(os.path.join(tmp, "tasks.sqlite"),
                              [("s_ghost", "running", NOW)])
        errs = []
        for idle, want_rc, want_alert in ((400, 1, "warn"), (1000, 3, "critical")):
            db = make_db(os.path.join(tmp, "f%d.sqlite" % idle), [
                part("s_ghost", ago(idle), tool("Bash", "completed")),
            ])
            add_presence(db, [])
            rc, payload = run_json(db, ["--registry", reg, "--tasks-db", tasks])
            s = one_session(payload)
            if rc != want_rc or s["alert"] != want_alert:
                errs.append("idle=%d: rc=%d alert=%s" % (idle, rc, s["alert"]))
            if not s.get("ghost_suspect") or s.get("control_plane") != "ghost_suspect":
                errs.append("idle=%d: ghost=%r cp=%r" % (idle, s.get("ghost_suspect"), s.get("control_plane")))
            if not any("控制面 ghost" in r for r in s.get("reasons", [])):
                errs.append("idle=%d: reasons=%r" % (idle, s.get("reasons")))
        return check(not errs, "; ".join(errs) or "活跃卡 + 无 presence + task_status=running -> ghost_suspect，idle 过线升 warn/critical")

def c_card_state_filter():
    with tempfile.TemporaryDirectory() as tmp:
        db = make_db(os.path.join(tmp, "f.sqlite"), [
            part("s_disp", ago(10), tool("Bash", "completed")),
            part("s_run", ago(10), tool("Bash", "completed")),
        ])
        add_presence(db, ["s_disp", "s_run"])
        reg = make_registry(os.path.join(tmp, "registry.json"), [
            {"id": "W-DISP", "sessionId": "s_disp", "state": "dispatched"},
            {"id": "W-RUN", "sessionId": "s_run", "state": "running"}])
        tasks = make_tasks_db(os.path.join(tmp, "tasks.sqlite"), [])
        rc, payload = run_json(db, ["--registry", reg, "--tasks-db", tasks,
                                    "--card-state", "dispatched"])
        errs = []
        ids = [s["session_id"] for s in payload["sessions"]]
        if rc != 0 or ids != ["s_disp"]:
            errs.append("--card-state 过滤: rc=%d ids=%r" % (rc, ids))
        if not any(x.get("session_id") == "s_run" for x in payload.get("skipped", [])):
            errs.append("skipped=%r" % payload.get("skipped"))
        return check(not errs, "; ".join(errs) or "--card-state dispatched 只留 s_disp，s_run 记 skipped")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_worker_watch")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(180, "test_worker_watch")

    case("healthy", c_healthy)
    case("stall-critical", c_stall_critical)
    case("stall-warn", c_stall_warn)
    case("running-stale", c_running_stale)
    case("fail-streak-crit", c_fail_streak_crit)
    case("fail-streak-warn", c_fail_streak_warn)
    case("single-error", c_single_error)
    case("repeat-crit", c_repeat_crit)
    case("repeat-nonconsec", c_repeat_nonconsec)
    case("bad-data", c_bad_data)
    case("json-fields", c_json_fields)
    case("thresholds", c_thresholds)
    case("session-filter", c_session_filter)
    case("active-filter", c_active_filter)
    case("empty+readonly", c_empty_and_readonly)
    case("now-determinism", c_now_determinism)
    case("done-card-skip", c_done_card_skip)
    case("task-closed-skip", c_task_closed_skip)
    case("ghost-probe", c_ghost_probe)
    case("card-state-filter", c_card_state_filter)

    bad = [r for r in RESULTS if r[1] == "FAIL"]
    print("SUMMARY %d/%d PASS" % (len(RESULTS) - len(bad), len(RESULTS)))
    if args.json:
        print(json.dumps({"cases": [{"name": n, "result": r, "msg": m}
                                    for n, r, m in RESULTS]},
                         ensure_ascii=False, indent=2))
    return 2 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
