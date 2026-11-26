#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""alert_owner.py —— 红灯必须有主（C-20261120-010 T02）

职责：把 `state/PATROL_ALERT.txt` 的条目按「级别 → 责任席 → 时限」赋主并落
`<state-dir>/mes/alert_actions.jsonl`（append-only），并提供 `--check` 对
「红灯超时仍无处置」判 exit 3。**单写文件 ≠ 处置**（值守制 §5.3 v1.1）。

级别口径（席位/02 §4 v1.1 + MES/05 §三 + MES/08 §二2）：
  ratio ≥ 2.0  → red    owner=厂长        sla=30min  三选（改派/加席/合理顺延）
  ratio ≥ 1.5  → yellow owner=值班厂长席  sla=30min  三选（工时类不升级发起人）

台账行：{"ts","alert_key","level","owner","sla_min","due_at","three_choice_required",
        "text","disposition","closed_at"}
用法：
  alert_owner.py scan [--patrol P] [--out O] [--now TS]
  alert_owner.py dispose --key K --disposition 文本 [--actor 席位] [--out O] [--now TS]
  alert_owner.py check [--out O] [--now TS] [--grace-min 15]
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE = os.path.normpath(os.path.join(HERE, "..", "state"))
DEFAULT_PATROL = os.path.join(DEFAULT_STATE, "PATROL_ALERT.txt")
RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[×xX]")


def die(msg, code=3):
    sys.stderr.write("[FAIL] %s\n" % msg)
    sys.exit(code)


def now_ts(explicit=""):
    if explicit:
        return explicit
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def parse_ts(s):
    return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def classify(ratio):
    """ratio → (level, owner, sla_min, three_choice)。未达 1.5× 不算红灯。"""
    if ratio >= 2.0:
        return "red", "厂长", 30, True
    if ratio >= 1.5:
        return "yellow", "值班厂长席", 30, True
    return "", "", 0, False


def read_rows(out):
    rows = []
    if not os.path.exists(out):
        return rows
    with open(out, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                die("台账坏行 %s:%d" % (out, ln))
    return rows


def append_rows(out, rows):
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "a", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def cmd_scan(a):
    if not os.path.exists(a.patrol):
        die("PATROL 文件不存在：%s" % a.patrol)
    now = now_ts(a.now)
    existing = {r["alert_key"] for r in read_rows(a.out)}
    new = []
    with open(a.patrol, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("["):
                continue
            m = RATIO_RE.search(line)
            if not m:
                continue
            ratio = float(m.group(1))
            level, owner, sla, three = classify(ratio)
            if not level:
                continue
            key = hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]
            if key in existing:
                continue
            due = (parse_ts(now) + datetime.timedelta(minutes=sla)).strftime("%Y-%m-%dT%H:%M:%S")
            new.append({"ts": now, "alert_key": key, "level": level, "owner": owner,
                        "sla_min": sla, "due_at": due, "three_choice_required": three,
                        "ratio": ratio, "text": line[:180],
                        "disposition": "", "closed_at": ""})
    if new:
        append_rows(a.out, new)
    print("scanned=%d new=%d out=%s" % (len(new), len(new), a.out))
    for r in new:
        print("  %s %s owner=%s due=%s ratio=%.1f×"
              % (r["alert_key"], r["level"], r["owner"], r["due_at"], r["ratio"]))
    return 0


def cmd_dispose(a):
    rows = read_rows(a.out)
    if not any(r["alert_key"] == a.key and r["level"] for r in rows):
        die("alert_key=%s 不在台账" % a.key)
    now = now_ts(a.now)
    src = [r for r in rows if r["alert_key"] == a.key][-1]
    append_rows(a.out, [{
        "ts": now, "alert_key": a.key, "level": src["level"], "owner": src["owner"],
        "sla_min": src["sla_min"], "due_at": src["due_at"],
        "three_choice_required": src["three_choice_required"], "ratio": src.get("ratio"),
        "text": src["text"], "disposition": a.disposition, "closed_at": now, "actor": a.actor,
    }])
    print("OK dispose %s by %s: %s" % (a.key, a.actor, a.disposition))
    return 0


def cmd_check(a):
    rows = read_rows(a.out)
    now = parse_ts(now_ts(a.now))
    bad = []
    for r in rows:
        if not r.get("level") or r.get("disposition"):
            continue
        # 同 key 后续已有处置行 → 视为已处置
        if any(x.get("alert_key") == r["alert_key"] and x.get("disposition") for x in rows):
            continue
        age = (now - parse_ts(r["ts"])).total_seconds() / 60.0
        if r["level"] == "red" and age > a.grace_min:
            bad.append((r["alert_key"], r["text"][:60], age, r["owner"]))
    print("red_unhandled=%d grace_min=%d" % (len(bad), a.grace_min))
    for k, t, age, own in bad:
        print("  - %s [%s] %.0f min 无处置 owner=%s · %s" % (k, "red", age, own, t))
    return 3 if bad else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="alert_owner.py", description="红灯有主（C-010 T02）")
    p.add_argument("--state-dir", default=os.environ.get("MES_STATE_DIR") or DEFAULT_STATE)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("--patrol", default=DEFAULT_PATROL)
    s.add_argument("--out", default="")
    s.add_argument("--now", default="")
    s.add_argument("--state-dir", default=p.get_default("state_dir"))
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("dispose")
    d.add_argument("--key", required=True)
    d.add_argument("--disposition", required=True)
    d.add_argument("--actor", default="值班厂长席")
    d.add_argument("--out", default="")
    d.add_argument("--now", default="")
    d.add_argument("--state-dir", default=p.get_default("state_dir"))
    d.set_defaults(func=cmd_dispose)

    c = sub.add_parser("check")
    c.add_argument("--out", default="")
    c.add_argument("--now", default="")
    c.add_argument("--grace-min", type=int, default=15)
    c.add_argument("--state-dir", default=p.get_default("state_dir"))
    c.set_defaults(func=cmd_check)

    a = p.parse_args(argv)
    if not getattr(a, "out", ""):
        a.out = os.path.join(a.state_dir, "mes", "alert_actions.jsonl")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
