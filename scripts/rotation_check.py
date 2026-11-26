#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""轮转体检：按会话统计 assistant 消息数与 part 数，标注轮转阈值。

阈值（TEAM_CHARTER_20261118.md §12）：
  assistant >= 30 或 part >= 800 -> ROTATE
  70% 阈值（21 / 560）           -> WARN
  130% 阈值（39 / 1040）         -> OVER（已卡顿风险）
用法：
  python -X utf8 scripts/rotation_check.py            # 人类可读表
  python -X utf8 scripts/rotation_check.py --json     # JSON（A0 台账用）
  python -X utf8 scripts/rotation_check.py --no-workers
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "state", "registry.json")
DB = os.path.expanduser("~/.zcode/cli/db/db.sqlite")

A_MSGS = 30
A_PARTS = 800


def classify(assistant, parts):
    if assistant >= int(A_MSGS * 1.3) or parts >= int(A_PARTS * 1.3):
        return "OVER"
    if assistant >= A_MSGS or parts >= A_PARTS:
        return "ROTATE"
    if assistant >= int(A_MSGS * 0.7) or parts >= int(A_PARTS * 0.7):
        return "WARN"
    return "OK"


def load_roles():
    with open(REGISTRY, encoding="utf-8") as f:
        reg = json.load(f)
    team = reg.get("team", {})
    rows = []
    for name, info in (team.get("roles") or {}).items():
        if not isinstance(info, dict):
            continue
        sid = info.get("sessionId") or ""
        if not sid:
            continue
        rows.append({
            "role": name,
            "session": sid,
            "model": info.get("model", ""),
            "reports_to": info.get("reports_to", ""),
            "kind": "role",
        })
    tasks = reg.get("tasks", [])
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    for t in tasks:
        if t.get("state") not in ("dispatched", "running", "in_progress"):
            continue
        sid = t.get("sessionId") or t.get("session_id") or ""
        if not sid:
            continue
        rows.append({
            "role": "worker:" + str(t.get("id")),
            "session": sid,
            "model": t.get("model", ""),
            "reports_to": t.get("owner") or t.get("assignee") or "",
            "kind": "worker",
            "task_state": t.get("state"),
        })
    return rows


def measure(con, sid):
    a = con.execute(
        "select count(*) from message where session_id=? and "
        "json_extract(data,'$.role')='assistant'", (sid,)
    ).fetchone()[0]
    p = con.execute("select count(*) from part where session_id=?", (sid,)).fetchone()[0]
    return a, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-workers", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("DB missing: " + DB, file=sys.stderr)
        return 2
    rows = load_roles()
    if args.no_workers:
        rows = [r for r in rows if r["kind"] != "worker"]
    # 去重：同一会话既是角色窗口又是卡片 target 时，只保留角色行并记卡片号
    dedup = {}
    for r in rows:
        cur = dedup.get(r["session"])
        if cur is None or (cur["kind"] == "worker" and r["kind"] == "role"):
            if cur is not None and cur["kind"] == "worker" and r["kind"] == "role":
                r = dict(r, worker_cards=[cur["role"]])
            dedup[r["session"]] = r
        elif cur["kind"] == "role" and r["kind"] == "worker":
            cur.setdefault("worker_cards", []).append(r["role"])
    rows = list(dedup.values())
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    out = []
    for r in rows:
        a, p = measure(con, r["session"])
        r["assistant"] = a
        r["parts"] = p
        r["status"] = classify(a, p)
        out.append(r)
    con.close()

    active_roles = sum(1 for r in out if r["kind"] == "role")
    active_workers = sum(1 for r in out if r["kind"] == "worker")
    summary = {
        "ts": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": {"assistant": A_MSGS, "parts": A_PARTS},
        "counts": {
            "roles": active_roles,
            "workers": active_workers,
            "total": active_roles + active_workers,
        },
        "need_rotation": [r["role"] for r in out if r["status"] in ("ROTATE", "OVER")],
        "rows": out,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print("== 轮转体检 %s ==" % summary["ts"])
    print("角色窗口 %d + 在办 worker %d = %d" % (active_roles, active_workers,
                                                 active_roles + active_workers))
    print("%-22s %-10s %-8s %6s %6s %8s" % ("ROLE", "SESSION", "MODEL", "ASST", "PARTS", "STATUS"))
    for r in out:
        if r["status"] == "OK":
            continue
        print("%-22s %-10s %-8s %6d %6d %8s" % (
            r["role"][:22], r["session"][:10], (r["model"] or "").split("/")[-1][:8],
            r["assistant"], r["parts"], r["status"]))
    need = summary["need_rotation"]
    print("需要轮转：%s" % (", ".join(need) if need else "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
