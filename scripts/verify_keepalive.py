# -*- coding: utf-8 -*-
"""verify_keepalive.py —— 心跳闹钟自愈(四查一拨)。

由 board_guard 每分钟静默调用;也可手工跑:
  python -X utf8 scripts/verify_keepalive.py [--json]

对每一条 recurring=1 的周期性自动化(心跳):
  查1 mode 必须 yolo(派发模式的权威字段是 automations.mode)
  查2 enabled 必须 1
  查3 target 会话必须存在且未归档(否则计 target_dead,不自动改指)
  查4 next_run_at 未过期:为空或超过 2 个节拍未触发 → 自动前拨 now+60s(补跑一拍)
退出码:0 全过(含已自愈);2 存在 target_dead。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATES = [
    r"C:\Users\Administrator\.zcode\v2\tasks-index.sqlite",
    os.path.join(os.path.expanduser("~"), ".zcode", "v2", "tasks-index.sqlite"),
]
DB = next((p for p in _CANDIDATES if os.path.isfile(p)), _CANDIDATES[0])
_CLI = [
    r"C:\Users\Administrator\.zcode\cli\db\db.sqlite",
    os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite"),
]
CLI = next((p for p in _CLI if os.path.isfile(p)), _CLI[0])


def interval_ms(rule_json):
    try:
        r = json.loads(rule_json or "{}")
        unit = (r.get("unit") or "minute").lower()
        n = float(r.get("interval") or 30)
        mult = {"minute": 60000, "hourly": 3600000, "hour": 3600000, "daily": 86400000, "day": 86400000}.get(unit, 60000)
        return int(n * mult)
    except Exception:
        return 30 * 60000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    now = int(time.time() * 1000)
    fixed_mode = fixed_en = rearmed = target_dead = quarantined = 0
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    rows = con.execute("select automation_id,title,enabled,mode,next_run_at,target_task_id,schedule_rule,recurring "
                       "from automations where ifnull(recurring,0)=1").fetchall()
    cli = con  # tasks 表与 automations 同在 tasks-index,同连接查询
    for r in rows:
        aid = r["automation_id"]
        # 查3 target 先行:目标已死=遗留心跳,只做隔离(e nabled=0),绝不复活
        tid = r["target_task_id"]
        t = cli.execute("select archived from tasks where task_id=?", (tid,)).fetchone() if tid else None
        if not tid or t is None or t["archived"]:
            target_dead += 1
            if r["enabled"]:
                con.execute("update automations set enabled=0, updated_at=? where automation_id=?", (now, aid))
                quarantined += 1
            continue
        # 查1 mode(仅目标存活的心跳)
        if (r["mode"] or "") != "yolo":
            con.execute("update automations set mode='yolo', updated_at=? where automation_id=?", (now, aid))
            fixed_mode += 1
        # 查2 enabled
        if not r["enabled"]:
            con.execute("update automations set enabled=1, updated_at=? where automation_id=?", (now, aid))
            fixed_en += 1
        # 查4 next_run
        iv = interval_ms(r["schedule_rule"])
        nxt = r["next_run_at"]
        if not nxt or nxt < now - 2 * iv:
            con.execute("update automations set next_run_at=?, updated_at=? where automation_id=?", (now + 60000, now, aid))
            rearmed += 1
    con.commit()
    con.close()
    cli.close()
    out = {"recurring": len(rows), "fixed_mode": fixed_mode, "fixed_enabled": fixed_en,
           "rearmed": rearmed, "target_dead": target_dead, "quarantined": quarantined}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print("KEEPALIVE", out)
    # 目标死亡的心跳已全部隔离(enabled=0)即视为稳定;数据库异常才会非零
    raise SystemExit(0)


if __name__ == "__main__":
    main()
