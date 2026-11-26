# -*- coding: utf-8 -*-
"""挂权限哨兵（2026-11-20 权限事故防复发，工厂加固 H8）。

原理：permission_allow.py 对"不放行"的调用不输出任何内容，交回 ZCode 正常权限流
（桌面/远程端表现为弹窗等人）。若某会话**最后一次**钩子事件是不放行（且非睡眠硬闸
——那类是显式拒绝，会话会继续走），之后长时间无任何新事件，大概率正挂在权限弹窗上。

用法：
  python -X utf8 perm_stall_check.py [--stale-min 10] [--write] [--json]
退出码：0 无告警；3 有疑似挂权限会话（对齐 factory_seat.py patrol 惯例）。
--write 时把告警追加到 state/PATROL_ALERT.txt（值守制 §5.3 同一落点）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.dont_write_bytecode = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
LOG = os.path.join(ROOT, "logs", "autoallow.jsonl")
ALERT = os.path.join(ROOT, "state", "PATROL_ALERT.txt")

# 这些 reason 会产生"等待人确认"的弹窗；sleep-banned 是显式拒绝，会话会继续
STALL_REASONS = {"target-out-of-scope", "protected", "out-of-scope", "no-target"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-min", type=int, default=10)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(LOG):
        print("no autoallow log")
        return 0

    # 只看尾部足够覆盖的窗口（每行一事件，取最近 2 万行）
    with open(LOG, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-20000:]

    last = {}  # session -> (ts, tool, reason, allow)
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        s = str(d.get("session") or "")
        if s:
            last[s] = (d.get("ts") or "", d.get("tool"), d.get("reason"), bool(d.get("allow")))

    now = time.time()
    # 只报在役任务席（tasks 表 archived=0）——死席/归档席挂着无害，不进告警
    live_ids = None
    try:
        import sqlite3
        candidates = [
            os.path.join(os.path.expanduser("~"), ".zcode", "v2", "tasks-index.sqlite"),
            r"C:\Users\Administrator\.zcode\v2\tasks-index.sqlite",
        ]
        idx = next((c for c in candidates if os.path.exists(c)), candidates[0])
        con = sqlite3.connect("file:" + idx + "?mode=ro", uri=True)
        cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
        id_col = "task_id" if "task_id" in cols else "id"
        conds = ["archived=0"]
        if "deleted" in cols:
            conds.append("deleted=0")
        live_ids = {r[0] for r in con.execute(
            f"select {id_col} from tasks where " + " and ".join(conds))}
        con.close()
    except Exception:
        pass  # 库不可得就按原始名单报（宁可误报不漏报）

    flagged = []
    for s, (ts, tool, reason, allowed) in last.items():
        if allowed or reason not in STALL_REASONS:
            continue
        if live_ids is not None and s not in live_ids:
            continue
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").timestamp()
        except ValueError:
            continue
        silent_min = (now - t) / 60
        if silent_min >= args.stale_min:
            flagged.append((s, ts, tool, reason, round(silent_min)))

    if args.json:
        print(json.dumps({"flagged": flagged, "stale_min": args.stale_min}, ensure_ascii=False))
    else:
        if not flagged:
            print("[OK] 无疑似挂权限会话（%d 会话在窗内）" % len(last))
        for s, ts, tool, reason, m in flagged:
            print("[STALL] %s 最后事件 %s %s %s，静默 %d 分钟（疑似挂权限弹窗）" % (s[:18], ts, tool, reason, m))

    if flagged and args.write:
        os.makedirs(os.path.dirname(ALERT), exist_ok=True)
        with open(ALERT, "a", encoding="utf-8") as f:
            f.write("[%s] perm_stall_check: %d 个会话疑似挂权限：%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), len(flagged),
                "; ".join("%s(%s,%s,静默%d分)" % (s[:18], t, r, m) for s, _, t, r, m in flagged)))
    return 3 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
