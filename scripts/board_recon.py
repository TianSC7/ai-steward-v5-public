#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""board_recon.py —— C-20261120-009 数字对源（5 抽样，独立复算，全中才 PASS）

抽 5 个数字：单数 / 席位数 / 今日 token / 告警数 / 在制数。
每个数字由本脚本**独立读源**复算（不复用 board_zones 的聚合函数），与看板值逐项比对。
用法：
  python -X utf8 scripts/board_recon.py                 # 现算 zones + 对源
  python -X utf8 scripts/board_recon.py --zones F.json  # 对已落盘 zones JSON
  python -X utf8 scripts/board_recon.py --json OUT.json
退出码：0 = 5/5 全中；1 = 有偏差（明细列出）；2 = 输入/参数错误。
"""
import argparse
import datetime
import io
import json
import os
import re
import sqlite3
import sys

import board_zones

TERMINAL = ("delivered", "accepted", "returned", "void", "failed")


def _cells(line):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return []
    return [c.strip() for c in s.strip("|").split("|")]


def _header_deadline(text):
    dl = st = None
    started = False
    for line in text.splitlines():
        cs = _cells(line)
        if len(cs) < 2:
            if started and line.strip() and not line.strip().startswith("|"):
                break
            continue
        started = True
        k = re.sub(r"\*\*|`", "", cs[0]).strip()
        if k == "交付截止":
            dl = cs[1]
        elif k == "当前状态":
            st = cs[1]
    m = re.search(r"`([A-Za-z][A-Za-z0-9\-]*)`", st or "")
    return dl or "", (m.group(1).lower() if m else "")


def _scan_orders():
    base = board_zones.mes_orders.orders_dir()
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for n in names:
        p = os.path.join(base, n, "order.md")
        if not os.path.isfile(p):
            continue
        try:
            text = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        dl, state = _header_deadline(text)
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", dl or "")
        deadline = None
        if m:
            try:
                deadline = datetime.datetime(*[int(x) for x in m.groups()])
            except ValueError:
                deadline = None
        out.append({"code": n, "state": state or "unknown", "deadline": deadline})
    return out


def _registry_active():
    p = os.path.join(board_zones.state_dir(), "seat_registry.json")
    try:
        d = json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return None
    return sum(1 for s in (d.get("seats") or []) if s.get("status") == "active")


def _today_tokens():
    p = board_zones.cli_db()
    if not os.path.isfile(p):
        return None
    day0 = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("?", "%3F").replace("#", "%23"), uri=True)
        v = con.execute("select coalesce(sum(computed_total_tokens),0) from model_usage where started_at>=?",
                        (day0.timestamp() * 1000,)).fetchone()[0]
        return int(v)
    except sqlite3.Error:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _patrol_count():
    p = os.path.join(board_zones.state_dir(), "PATROL_ALERT.txt")
    try:
        text = io.open(p, encoding="utf-8", errors="replace").read().lstrip("\ufeff")
    except OSError:
        return None
    blocks, cur = 0, False
    for line in text.splitlines():
        if line.strip():
            if not cur:
                blocks += 1
                cur = True
        else:
            cur = False
    return blocks


def reconcile(zones_doc):
    z = zones_doc.get("zones") or zones_doc
    scans = _scan_orders()
    now = datetime.datetime.now()
    detail = []

    def chk(name, board, source, src_path, equal=True, note=""):
        ok = (board == source) if equal else (board is not None and source is not None and abs(board - source) / max(1, source) < 0.10)
        detail.append({"item": name, "board": board, "source": source, "matched": bool(ok),
                       "source_file": src_path, "note": note})

    o = z.get("orders") or {}
    chk("单数", (o.get("summary") or {}).get("orders"), len(scans),
        board_zones.mes_orders.orders_dir())

    s = z.get("seats") or {}
    chk("席位数", (s.get("summary") or {}).get("registry_active"), _registry_active(),
        os.path.join(board_zones.state_dir(), "seat_registry.json"), note="registry seats status=active")

    c = z.get("cost") or {}
    chk("今日 token", c.get("today_tokens"), _today_tokens(), board_zones.cli_db(),
        note="model_usage.computed_total_tokens 当日 0 点起")

    patrol = _patrol_count()
    over = sum(1 for r in scans if r["deadline"] and now > r["deadline"] and r["state"] not in TERMINAL)
    a = z.get("alerts") or {}
    board_alerts = len(a.get("items") or [])
    chk("告警数", board_alerts, (patrol or 0) + over,
        os.path.join(board_zones.state_dir(), "PATROL_ALERT.txt") + " + 卡交付截止", note="PATROL 条目 + 超时单")

    wip = sum(1 for r in scans if r["state"] not in TERMINAL)
    chk("在制数", (o.get("summary") or {}).get("wip"), wip,
        board_zones.mes_orders.orders_dir(), note="非 delivered/accepted/returned")

    matched = sum(1 for d in detail if d["matched"])
    return {"checked": len(detail), "matched": matched,
            "pass": matched == len(detail), "generated_at": board_zones.iso(),
            "detail": detail}


def main():
    ap = argparse.ArgumentParser(description="C-009 五数字对源")
    ap.add_argument("--zones", help="已落盘的 zones/v2 JSON")
    ap.add_argument("--json", help="明细落盘")
    a = ap.parse_args()
    if a.zones:
        try:
            doc = json.load(io.open(a.zones, encoding="utf-8"))
        except Exception as exc:
            sys.stderr.write("zones JSON 不可读: %s\n" % exc)
            return 2
    else:
        doc = board_zones.collect_zones()
    r = reconcile(doc)
    if a.json:
        io.open(a.json, "w", encoding="utf-8").write(json.dumps(r, ensure_ascii=False, indent=2))
    for d in r["detail"]:
        print("[%s] %-8s board=%s source=%s  <- %s" % ("PASS" if d["matched"] else "FAIL", d["item"],
                                                        d["board"], d["source"], d["source_file"]))
    print("[%s] recon %d/%d" % ("PASS" if r["pass"] else "FAIL", r["matched"], r["checked"]))
    return 0 if r["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
