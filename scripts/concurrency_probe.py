#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""concurrency_probe.py — 每 5 分钟并发快照（只读 DB）

口径（用户 2026-11-19 07:0x 令）:
  - 活跃并发 = 最近 N 分钟（默认 5）内有 model_usage 调用的**不同会话数**；
  - 同时列出这些会话的角色/标题/模型；
  - 另报 runtime presence=1 的会话数（在跑但可能空闲）；
  - 未归档会话总数 + 分年龄桶（idle<30m / 30m-2h / >2h）；
  - 当日各模型道 tokens 累计（GLM/STEP/NEX/DeepSeek）。

输出:
  - state/concurrency.jsonl  每行一条 JSON 快照（机器可读）
  - state/CONCURRENCY_LOG.md 追加一行 markdown 表（人读）
用法:
  python -X utf8 scripts/concurrency_probe.py [--window 5]
"""
import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
JSONL = os.path.join(STATE, "concurrency.jsonl")
MD = os.path.join(STATE, "CONCURRENCY_LOG.md")


def role_of(title):
    t = title or ""
    if "厂长" in t:
        return "steward"
    if "M-PLAN" in t or "策划" in t:
        return "planner"
    if "M-PRODUCT" in t or "生产经理" in t:
        return "production"
    if "M-QUALITY" in t or "质量经理" in t:
        return "quality"
    if "M-AUDIT" in t:
        return "audit"
    if "验收" in t:
        return "verifier"
    if "M-SAFETY" in t or re.search(r"\bS1\b", t) or "SW1" in t:
        return "safety"
    if "外包" in t or "OB1" in t:
        return "outsource"
    if "组长" in t or re.search(r"\bL\d\b", t):
        return "leader"
    if re.search(r"worker", t, re.I) or "员工" in t or "开发" in t:
        return "worker"
    if "审计" in t:
        return "audit"
    if "安全" in t:
        return "safety"
    if re.search(r"M-[A-Z]+", t) or "经理" in t:
        return "manager"
    return "other"


MANAGER_ROLES = {"steward", "planner", "production", "quality", "audit", "safety", "manager"}
EXEC_ROLES = {"verifier", "leader", "worker", "outsource"}


def fmt(n):
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.1f}万"
    return str(int(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=5)
    args = ap.parse_args()
    now = datetime.now()
    since = int((now - timedelta(minutes=args.window)).timestamp() * 1000)
    day_start = int(datetime(now.year, now.month, now.day).timestamp() * 1000)

    con = sqlite3.connect(f"file:{DB.replace(chr(92), '/')}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row

    # active by model calls in window
    rows = con.execute(
        """select session_id, provider_id, model_id, count(*) calls, sum(computed_total_tokens) tok
           from model_usage where started_at >= ? and status != 'cancelled'
           group by session_id, provider_id, model_id""",
        (since,),
    ).fetchall()
    sessions = {}
    for r in rows:
        s = sessions.setdefault(r["session_id"], {"calls": 0, "tok": 0, "models": set()})
        s["calls"] += r["calls"]
        s["tok"] += r["tok"] or 0
        s["models"].add(f"{r['provider_id']}/{r['model_id']}")

    detail = []
    for sid, s in sessions.items():
        t = con.execute("select title from session where id=?", (sid,)).fetchone()
        title = (t["title"] if t else "") or ""
        detail.append({"session": sid[:20], "title": title[:40], "role": role_of(title),
                       "models": sorted(s["models"]), "calls": s["calls"], "tokens": s["tok"]})
    detail.sort(key=lambda x: -x["tokens"])

    presence = con.execute("select count(*) c from session_runtime_presence").fetchone()["c"]
    unarchived = con.execute(
        "select count(*) c from session where (time_archived is null or time_archived=0) and (time_deleted is null or time_deleted=0)"
    ).fetchone()["c"]
    rows2 = con.execute(
        "select time_updated from session where (time_archived is null or time_archived=0) and (time_deleted is null or time_deleted=0)"
    ).fetchall()
    b = {"lt30": 0, "m30_2h": 0, "gt2h": 0}
    for r in rows2:
        age = (now - datetime.fromtimestamp(r["time_updated"] / 1000)).total_seconds()
        if age < 1800:
            b["lt30"] += 1
        elif age < 7200:
            b["m30_2h"] += 1
        else:
            b["gt2h"] += 1

    # day totals by model lane
    lane = {}
    for r in con.execute(
        """select provider_id, model_id, sum(computed_total_tokens) tok from model_usage
           where started_at >= ? and status != 'cancelled' group by provider_id, model_id""",
        (day_start,),
    ):
        lane[f"{r['provider_id']}/{r['model_id']}"] = r["tok"] or 0
    con.close()

    # role / employee counts (用户 07:0x 令：统计里必须显示到底多少员工在干活)
    role_cnt = {"managers": 0, "verifier": 0, "leader": 0, "worker": 0, "outsource": 0, "other": 0,
                "employees": 0, "total_active": len(detail)}
    for d in detail:
        r = d["role"]
        if r in MANAGER_ROLES:
            role_cnt["managers"] += 1
        elif r in ("verifier", "leader", "worker", "outsource"):
            role_cnt[r] += 1
        else:
            role_cnt["other"] += 1
    role_cnt["employees"] = role_cnt["leader"] + role_cnt["worker"] + role_cnt["outsource"]

    snap = {
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "window_min": args.window,
        "active_sessions": len(detail),
        "roles": role_cnt,
        "presence_rows": presence,
        "unarchived": unarchived,
        "age_buckets": b,
        "by_model": {m: sum(d["tokens"] for d in detail if m in d["models"]) for m in {m for d in detail for m in d["models"]}},
        "sessions": detail,
        "day_tokens_by_lane": lane,
    }
    with open(JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    # markdown append
    new = not os.path.exists(MD)
    with open(MD, "a", encoding="utf-8") as f:
        if new:
            f.write("# CONCURRENCY_LOG（并发快照，随机间隔 1–8 分钟）\n\n")
            f.write("> 由 `scripts/concurrency_randprobe.py` 以随机间隔（60–480s）调用本脚本追加一行；只读 DB。\n\n")
            f.write("| 时间 | 窗口 | 活跃会话 | 管理层 | 验收 | 组长 | 员工 | 外包 | **执行员工合计** | 在跑presence | 未归档 | <30m | 30m-2h | >2h | 分道tokens(窗口) |\n")
            f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        by = "；".join(f"{k.split('/')[-1]} {fmt(v)}" for k, v in sorted(snap["by_model"].items(), key=lambda x: -x[1])) or "—"
        f.write(f"| {snap['ts']} | {args.window}m | {len(detail)} | {role_cnt['managers']} | {role_cnt['verifier']} | "
                f"{role_cnt['leader']} | {role_cnt['worker']} | {role_cnt['outsource']} | **{role_cnt['employees']}** | "
                f"{presence} | {unarchived} | {b['lt30']} | {b['m30_2h']} | {b['gt2h']} | {by} |\n")

    print(json.dumps(snap, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
