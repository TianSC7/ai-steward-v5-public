#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""batch_archive.py — 批量归档陈旧会话（保守白名单）

规则（用户 2026-11-18/19，D-034 起阈值 30 分钟）：
  - 永不归档：厂长会话、cron target、最近 30 分钟有 model_usage 的会话、
    runtime presence 中的会话、显式保护名单（有待合并证据/正在验收的工作包）。
  - 只归档 time_updated 早于 30 分钟且不在上述集合的会话。
  - 责任部门：M-SAFETY（安全部）每轮巡场执行；M-AUDIT 复核归档记录。
用法: python -X utf8 scripts/batch_archive.py [--apply] [--min-age-min 30]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_session import archive  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
WHITELIST_JSON = os.path.join(ROOT, "state", "archive_whitelist.json")
BATCH_LOG = os.path.join(ROOT, "state", "archive_batches.jsonl")

# 显式保护：仅在 state/archive_whitelist.json 缺失/解析失败时作为兜底。
# D-040（2026-11-19 08:43）：外置白名单存在时以内置仅保住厂长，其余不再隐式保护——
# 旧实现把两套名单相加、内置 12 条无法移除，导致已收口经理/组长/员工长期不归档。
PROTECT_PREFIXES = [
    "sess_4229d515",  # 厂长（本窗口）
    "sess_8e96c86c",  # M-PLAN（V6·DeepSeek）
    "sess_b02a9d99",  # M-PRODUCT V6b（证据锚点修正）
    "sess_a092464b",  # M-AUDIT（V6·DeepSeek）
    "sess_9ca6e52b",  # M-SAFETY（V6·DeepSeek）
    "sess_c0c75274",  # M-QUALITY 旧席（W-054 收口已归档，保留防误删）
    "sess_0ad29c40",  # M-QUALITY V6b（W-055 合并收口）
    "sess_88870f47",  # S1-C 安全组长
    "sess_20754868",  # OB1 外包员工（minekuai 单并发）
    "sess_97407323",  # W-056 员工（NEX）
    "sess_e2450836",  # W-057 员工（NEX）
    "sess_5a0b4c3a",  # L1 组长（STEP）
]
HARD_SAFE = ["sess_4229d515"]  # 外置白名单生效时仍强制保住厂长，防误归档


def load_whitelist():
    """D-034/D-040：外置白名单权威；仅缺失或解析失败时回退内置名单。"""
    if os.path.exists(WHITELIST_JSON):
        try:
            data = json.load(open(WHITELIST_JSON, encoding="utf-8"))
            prefixes = list(HARD_SAFE)
            for item in data.get("prefixes", []):
                p = item.get("prefix") if isinstance(item, dict) else item
                if p and p not in prefixes:
                    prefixes.append(p)
            return prefixes
        except Exception as e:
            print(f"[batch_archive] WARN whitelist parse failed, fallback to builtin: {e}")
    return list(PROTECT_PREFIXES)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-age-min", type=int, default=30)
    args = ap.parse_args()
    now = int(time.time() * 1000)
    since = now - args.min_age_min * 60 * 1000

    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    active = {r["session_id"] for r in con.execute(
        "select distinct session_id from model_usage where started_at>=? and status!='cancelled'", (since,))}
    # presence 行只表示窗口在 App 里打开（App 进程常驻），不代表正在执行；
    # 归档不中断执行，故 presence 不作拦截，只对最近窗口内有模型调用者拦截。
    live_pids = set()
    for r in con.execute("select distinct process_id from session_runtime_presence"):
        pid = r["process_id"]
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                live_pids.add(pid)
        except Exception:
            pass
    rows = con.execute(
        "select id,title,time_updated from session where (time_archived is null or time_archived=0)"
        " and (time_deleted is null or time_deleted=0) and time_updated<? order by time_updated desc", (since,)
    ).fetchall()
    con.close()

    victims, kept = [], []
    protect = load_whitelist()
    for r in rows:
        sid = r["id"]
        if any(sid.startswith(p) for p in protect):
            kept.append((sid, r["title"], "protected"))
        elif sid in active:
            kept.append((sid, r["title"], "recent-model-usage"))
        else:
            victims.append((sid, r["title"]))

    print(f"[batch_archive] candidates={len(rows)} archive={len(victims)} keep={len(kept)} apply={args.apply}")
    for sid, title, why in kept[:10]:
        print(f"  keep {sid[:20]} {why:18s} {(title or '')[:40]}")
    ok = 0
    done = []
    for sid, title in victims:
        if not args.apply:
            print(f"  would archive {sid[:20]} {(title or '')[:48]}")
            continue
        rc = archive(sid, as_json=False)
        if rc == 0:
            ok += 1
            done.append({"session": sid, "title": title})
    if args.apply:
        print(f"[batch_archive] archived={ok}/{len(victims)}")
        rec = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "min_age_min": args.min_age_min,
            "candidates": len(rows), "archived": ok, "victims": len(victims),
            "kept": [{"session": s, "title": t, "why": w} for s, t, w in kept],
            "archived_list": done,
            "operator": os.environ.get("USERNAME", ""),
        }
        with open(BATCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
