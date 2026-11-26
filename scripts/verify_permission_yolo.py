# -*- coding: utf-8 -*-
"""权限与派发边界验证(zai-org/ZCode 源码口径):四层 yolo 不变量 + 运行时卫生。

检查项(任一 FAIL 即退出码 2):
  1 config.json permission.mode=yolo 且 autoApproveHighRisk=true
  2 所有 enabled 自动化的 mode=yolo(scheduler/index.ts:195 派发权威字段)
  3 自动化目标会话 session.permission={"mode":"yolo"}
  4 mate 项目 local_setting permission.mode=yolo
  5 session_runtime_presence 无死进程占用(重启后悬挂会致派发即中断)
  6 automation_runs 无超过 30 分钟的 running 悬挂行
用法: python -X utf8 scripts/verify_permission_yolo.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CFG = os.path.join(HOME, ".zcode", "cli", "config.json")
IDX = os.path.join(HOME, ".zcode", "v2", "tasks-index.sqlite")
CLI = os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite")
fails = []
oks = []


def check(name, ok, detail=""):
    (oks if ok else fails).append(name + ((" | " + detail) if detail else ""))
    print(("[PASS] " if ok else "[FAIL] ") + name + ((" | " + detail) if detail else ""))


try:
    cfg = json.load(open(CFG, encoding="utf-8"))
    perm = cfg.get("permission") or {}
    check("config.permission.mode=yolo", perm.get("mode") == "yolo", str(perm))
    check("config.permission.autoApproveHighRisk", perm.get("autoApproveHighRisk") is True)
except Exception as e:
    check("config readable", False, str(e))

try:
    con = sqlite3.connect("file:" + IDX + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    bad = [r["automation_id"][-8:] for r in con.execute("select automation_id,mode from automations where enabled=1 and ifnull(mode,'')!='yolo'")]
    check("enabled automations mode=yolo", not bad, ",".join(bad))
    tgts = [r["target_task_id"] for r in con.execute("select distinct target_task_id from automations where enabled=1 and target_task_id is not null")]
    con.close()
    c = sqlite3.connect("file:" + CLI + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    badsess = []
    for t in tgts:
        r = c.execute("select permission from session where id=?", (t,)).fetchone()
        if not r:
            badsess.append(t[-8:] + ":missing")
            continue
        try:
            m = json.loads(r["permission"] or "{}").get("mode")
        except Exception:
            m = None
        if m != "yolo":
            badsess.append(t[-8:] + ":" + str(m))
    check("target sessions permission=yolo", not badsess, ",".join(badsess))
    n = c.execute("select count(*) from local_setting where namespace='permission' and key='mode' and scope_id like ? and value not like ?", ("%mate%", "%yolo%")).fetchone()[0]
    check("mate project permission=yolo", n == 0, "bad rows=%d" % n)
    p = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Process | Select-Object -ExpandProperty Id"], capture_output=True)
    alive = set()
    for tok in p.stdout.decode("gbk", errors="replace").split():
        if tok.isdigit():
            alive.add(int(tok))
    stale = [r["session_id"][-8:] for r in c.execute("select session_id,process_id from session_runtime_presence") if r["process_id"] not in alive]
    check("session_runtime_presence no dead pid", not stale, ",".join(stale[:6]))
    c.close()
    con = sqlite3.connect("file:" + IDX + "?mode=ro", uri=True)
    cutoff = int(time.time() * 1000) - 30 * 60 * 1000
    hung = con.execute("select count(*) from automation_runs where outcome='running' and scheduled_at < ?", (cutoff,)).fetchone()[0]
    check("no stale running runs(>30min)", hung == 0, "count=%d" % hung)
    con.close()
except Exception as e:
    check("db checks", False, str(e))

print("\n%d passed / %d checks" % (len(oks), len(oks) + len(fails)))
raise SystemExit(0 if not fails else 2)
