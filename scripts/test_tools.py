# -*- coding: utf-8 -*-
"""管家工具边界用例：exec_handle / set_permission / permission_allow。

覆盖 18 个形态：
  1 exec_handle 候选新→旧、跳过 automation 输入
  2 exec_handle --limit 生效
  3 exec_handle 数据库不存在 -> exit 2
  4 set_permission build -> yolo 且生成备份
  5 set_permission 已 yolo -> 无需变更、exit 0
  6 set_permission --check 只读输出
  7 permission_allow 已登记会话放行（PreToolUse 形状）
  8 permission_allow 工作区 cwd 放行（PermissionRequest 形状）
  9 permission_allow 范围外不放行（输出为空）
  10 permission_allow 总开关文件缺失不放行
  11 工作区 Write 目标在根内 -> 放行
  12 工作区 Write 目标在根外 -> 不放行
  13 目标命中 protected_paths -> 不放行（已登记会话也不放行）
  14 已登记会话 Write 目标在根外（非保护路径）-> 放行
  15 workspace_fallback=false 时未登记会话 -> 不放行
  16 已登记会话的非文件工具、cwd 在根外 -> 仍放行
  17 硬闸：Bash sleep -> 显式 deny（run() 真实流程，非空输出）
  18 硬闸：PowerShell Start-Sleep -> 显式 deny

用法：python -X utf8 test_tools.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import permission_allow as pa  # noqa: E402

PY = sys.executable
RESULTS: list[tuple[str, str, str]] = []


def case(name: str, ok: bool, detail: str) -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))


def run(script: str, args: list[str], db: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, STEWARD_ZCODE_DB=db)
    return subprocess.run([PY, "-X", "utf8", os.path.join(HERE, script)] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=60)


def make_input_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute("create table session_input (id text, session_id text, kind text, delivery text,"
                " status text, time_created integer)")
    rows = [
        ("queue_cmd-new", "s1", "sendText", "startNow", "promoted", 300),
        ("queue_automation-abc:111", "s1", "automation", "queue", "promoted", 400),
        ("queue_cmd-mid", "s1", "sendText", "queue", "promoted", 200),
        ("queue_cmd-old", "s1", "sendText", "startNow", "promoted", 100),
        ("plainid", "s1", "sendText", "startNow", "promoted", 500),
    ]
    con.executemany("insert into session_input values (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def make_session_db(path: str, perm: str) -> None:
    con = sqlite3.connect(path)
    con.execute("create table session (id text primary key, permission text)")
    con.execute("insert into session values (?,?)", ("sess_a", perm))
    con.commit()
    con.close()


def test_exec_handle(tmp: str) -> None:
    db = os.path.join(tmp, "in.sqlite")
    make_input_db(db)

    r = run("exec_handle.py", ["--session", "s1", "--json"], db)
    ok = r.returncode == 0
    ids = []
    if ok:
        try:
            ids = [c["commandId"] for c in json.loads(r.stdout)["candidates"]]
        except (ValueError, KeyError):
            ok = False
    case("exec_handle-候选顺序", ok and ids == ["cmd-new", "cmd-mid", "cmd-old"],
         "rc=%s ids=%s" % (r.returncode, ids))

    r = run("exec_handle.py", ["--session", "s1", "--limit", "2", "--json"], db)
    n = -1
    try:
        n = len(json.loads(r.stdout)["candidates"])
    except ValueError:
        pass
    case("exec_handle-limit", r.returncode == 0 and n == 2, "rc=%s n=%s" % (r.returncode, n))

    missing = os.path.join(tmp, "nope.sqlite")
    r = run("exec_handle.py", ["--session", "s1"], missing)
    case("exec_handle-库缺失", r.returncode == 2, "rc=%s" % r.returncode)


def test_set_permission(tmp: str) -> None:
    db = os.path.join(tmp, "sess.sqlite")
    make_session_db(db, '{"mode":"build"}')

    r = run("set_permission.py", ["--session", "sess_a"], db)
    con = sqlite3.connect(db)
    perm = con.execute("select permission from session where id='sess_a'").fetchone()[0]
    con.close()
    backups = [f for f in os.listdir(tmp) if f.startswith("permission-backup-")]
    case("set_permission-改yolo+备份",
         r.returncode == 0 and perm == '{"mode":"yolo"}' and backups,
         "rc=%s perm=%s backups=%d" % (r.returncode, perm, len(backups)))

    r = run("set_permission.py", ["--session", "sess_a"], db)
    case("set_permission-幂等", r.returncode == 0 and "无需变更" in r.stdout,
         "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:80]))

    r = run("set_permission.py", ["--session", "sess_a", "--check"], db)
    case("set_permission-check", r.returncode == 0 and '{"mode":"yolo"}' in r.stdout,
         "rc=%s out=%s" % (r.returncode, r.stdout.strip()[:80]))


def allow_out(data: dict, flag: bool, event: str, extra: dict | None = None) -> str:
    tmp = tempfile.mkdtemp(prefix="allow-")
    try:
        reg = os.path.join(tmp, "registry.json")
        cfg = os.path.join(tmp, "steward.json")
        work = os.path.join(tmp, "work")
        os.makedirs(work, exist_ok=True)

        def fix(v):
            if isinstance(v, str):
                return v.replace("__WORK__", work)
            if isinstance(v, dict):
                return {k: fix(x) for k, x in v.items()}
            if isinstance(v, list):
                return [fix(x) for x in v]
            return v

        json.dump({"tasks": [{"id": "T", "sessionId": "sess_reg"}],
                   "dispatcher": {"sessionId": "sess_disp"}}, open(reg, "w"))
        autoallow = {"roots": [work], "workspace_fallback": True}
        if extra:
            autoallow.update(fix(extra))
        json.dump({"autoallow": autoallow}, open(cfg, "w"))
        data = fix(data)
        old = (pa.REGISTRY, pa.CONFIG, pa.FLAG, pa.LOG)
        pa.REGISTRY, pa.CONFIG = reg, cfg
        pa.FLAG = os.path.join(tmp, "flag.on")
        pa.LOG = os.path.join(tmp, "log.jsonl")
        if flag:
            open(pa.FLAG, "w").write("on")
        buf = io.StringIO()
        with redirect_stdout(buf):
            # 走真实 run() 流程（睡眠硬闸 -> decide -> 放行才 emit），
            # 不直接调 emit(False)：main() 对不放行本就不产出（不干预）。
            if flag:
                pa.run(data, event)
        pa.REGISTRY, pa.CONFIG, pa.FLAG, pa.LOG = old
        return buf.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_allow() -> None:
    def is_allow(out: str) -> bool:
        try:
            h = json.loads(out)["hookSpecificOutput"]
            return h.get("permissionDecision") == "allow" or \
                (h.get("decision") or {}).get("behavior") == "allow"
        except (ValueError, KeyError, TypeError):
            return False

    def is_deny(out: str) -> bool:
        try:
            return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
        except (ValueError, KeyError, TypeError):
            return False

    out = allow_out({"session_id": "sess_reg", "cwd": "/elsewhere"}, True, "PreToolUse")
    case("allow-已登记会话", is_allow(out), out[:120])

    tmp = tempfile.mkdtemp(prefix="allow2-")
    try:
        out = allow_out({"session_id": "other", "cwd": "__WORK__"}, True, "PermissionRequest")
        case("allow-工作区cwd", is_allow(out), out[:120])

        out = allow_out({"session_id": "other", "cwd": os.path.join(tmp, "outside")}, True, "PreToolUse")
        case("allow-范围外", out.strip() == "", "out=%r" % out[:80])

        out = allow_out({"session_id": "sess_reg", "cwd": "__WORK__"}, False, "PreToolUse")
        case("allow-总开关关闭", out.strip() == "", "out=%r" % out[:80])

        out = allow_out({"session_id": "other", "cwd": "__WORK__", "tool_name": "Write",
                         "tool_input": {"file_path": "__WORK__/a.txt"}}, True, "PreToolUse")
        case("allow-Write根内", is_allow(out), out[:120])

        outside = "C:/steward-outside-test/outside.txt"
        out = allow_out({"session_id": "other", "cwd": "__WORK__", "tool_name": "Write",
                         "tool_input": {"file_path": outside}}, True, "PreToolUse")
        case("allow-Write根外", out.strip() == "", "out=%r" % out[:80])

        out = allow_out({"session_id": "other", "cwd": "__WORK__", "tool_name": "Write",
                         "tool_input": {"file_path": "__WORK__/protected/key.txt"}},
                        True, "PreToolUse", extra={"protected_paths": ["__WORK__/protected"]})
        case("allow-保护路径", out.strip() == "", "out=%r" % out[:80])

        out = allow_out({"session_id": "sess_reg", "cwd": "__WORK__", "tool_name": "Write",
                         "tool_input": {"file_path": outside}}, True, "PreToolUse")
        case("allow-已登记根外写", is_allow(out), out[:120])

        out = allow_out({"session_id": "other", "cwd": "__WORK__", "tool_name": "Bash"},
                        True, "PreToolUse", extra={"workspace_fallback": False})
        case("allow-关闭兜底", out.strip() == "", "out=%r" % out[:80])

        out = allow_out({"session_id": "sess_reg", "cwd": "C:/elsewhere", "tool_name": "Bash"},
                        True, "PreToolUse")
        case("allow-已登记非文件", is_allow(out), out[:120])

        # 17/18 睡眠硬闸（用户 2026-11-18 三次加码）：前台 sleep/wait 必须显式 deny，
        # 与"不放行=不干预（空输出）"是两条契约，曾因 emit 改动漂移过 4 例，这里锁死。
        out = allow_out({"session_id": "sess_reg", "cwd": "__WORK__", "tool_name": "Bash",
                         "tool_input": {"command": "sleep 5 && echo done"}}, True, "PreToolUse")
        case("硬闸-sleep显式deny", is_deny(out) and "硬闸" in out, out[:120])
        out = allow_out({"session_id": "other", "cwd": "__WORK__", "tool_name": "Shell",
                         "tool_input": {"command": "Start-Sleep -Seconds 3"}}, True, "PreToolUse")
        case("硬闸-StartSleep显式deny", is_deny(out) and "硬闸" in out, out[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_tools")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_tools")

    tmp = tempfile.mkdtemp(prefix="steward-tools-")
    try:
        test_exec_handle(tmp)
        test_set_permission(tmp)
        test_allow()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [r for r in RESULTS if r[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-22s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails and RESULTS else 2


if __name__ == "__main__":
    raise SystemExit(main())
