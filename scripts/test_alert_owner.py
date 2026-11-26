# -*- coding: utf-8 -*-
"""A2 · alert_owner 红灯赋主/处置/超时判罚（C-20261120-010 T02）

断言：
  1 classify(1.6) → yellow / 值班厂长席 / 30 min / 三选
  2 classify(2.1) → red / 厂长 / 30 min / 三选（三选=改派/加席/合理顺延）
  3 scan 夹具 2 条（1.6×/2.1×）→ 2 行，due = ts+30min，未达 1.5× 的行忽略
  4 重复 scan → 不重复落行
  5 check（+10 min，红未处置但未过 15 min 宽限）→ rc 0
  6 check（+40 min，红超时未处置）→ rc 3
  7 dispose 后 check（+40 min）→ rc 0（有处置即不再判罚）

全部写 tmp 沙箱，不碰活体 state。
用法：python -X utf8 test_alert_owner.py
"""
import io
import json
import os
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import alert_owner as AO  # noqa: E402

RESULTS = []


def ok(n, d=""):
    RESULTS.append(True); print("[PASS] %-20s %s" % (n, d))


def bad(n, d=""):
    RESULTS.append(False); print("[FAIL] %-20s %s" % (n, d))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    rc = 0
    try:
        rc = AO.main(argv)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 2
    finally:
        sys.stdout, sys.stderr = so, se
    return rc, out.getvalue(), err.getvalue()


def main():
    tmp = tempfile.mkdtemp(prefix="c010_t02_")
    patrol = os.path.join(tmp, "PATROL_ALERT.txt")
    out = os.path.join(tmp, "mes", "alert_actions.jsonl")
    with open(patrol, "w", encoding="utf-8", newline="\n") as f:
        f.write("[RW 21:00] C-007 节点超 1.6× 时限，需三选回执\n")
        f.write("[RW 21:01] C-006 已超 2.1× 时限，红灯升级\n")
        f.write("[RW 21:02] C-009 超 1.2× 时限，未达红线\n")

    # 1 / 2
    for ratio, want in ((1.6, ("yellow", "值班厂长席")), (2.1, ("red", "厂长"))):
        lv, own, sla, three = AO.classify(ratio)
        if (lv, own) == want and sla == 30 and three:
            ok("classify %.1f×" % ratio, "%s / %s / 30min / 三选" % (lv, own))
        else:
            bad("classify %.1f×" % ratio, "%s/%s/%s/%s" % (lv, own, sla, three))

    # 3
    now = "2026-11-20T22:00:00"
    rc, o, e = run(["scan", "--patrol", patrol, "--out", out, "--now", now])
    rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
    levels = sorted(r["level"] for r in rows)
    if rc == 0 and len(rows) == 2 and levels == ["red", "yellow"] and all(
            r["due_at"] == "2026-11-20T22:30:00" for r in rows):
        ok("scan", "2 行（1.2× 忽略），due=+30min")
    else:
        bad("scan", "rc=%s rows=%d %s" % (rc, len(rows), levels))

    # 4
    rc, o, e = run(["scan", "--patrol", patrol, "--out", out, "--now", now])
    rows2 = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
    if len(rows2) == len(rows):
        ok("scan 幂等", "重扫不重复落行（%d 行）" % len(rows2))
    else:
        bad("scan 幂等", "%d → %d" % (len(rows), len(rows2)))

    # 5
    rc, o, e = run(["check", "--out", out, "--now", "2026-11-20T22:10:00"])
    if rc == 0:
        ok("check +10min", "宽限内不判罚")
    else:
        bad("check +10min", "rc=%s %s" % (rc, o))

    # 6
    rc, o, e = run(["check", "--out", out, "--now", "2026-11-20T22:40:00"])
    if rc == 3 and "red_unhandled=1" in o:
        ok("check +40min", "1 条红灯超时 → exit 3（黄灯不判罚）")
    else:
        bad("check +40min", "rc=%s %s" % (rc, o.strip()[:80]))

    # 7
    red = [r for r in rows2 if r["level"] == "red"][0]
    rc, o, e = run(["dispose", "--key", red["alert_key"], "--disposition",
                    "改派 W-104 处理", "--actor", "值班厂长席", "--out", out, "--now", now])
    rc2, o2, e2 = run(["check", "--out", out, "--now", "2026-11-20T22:40:00"])
    if rc == 0 and rc2 == 0:
        ok("dispose 后 check", "红灯有处置 → 不再判罚")
    else:
        bad("dispose 后 check", "rc=%s/%s %s" % (rc, rc2, o2.strip()[:80]))

    passed = sum(1 for x in RESULTS if x)
    print("\n%d passed / %d total" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 2


if __name__ == "__main__":
    sys.exit(main())
