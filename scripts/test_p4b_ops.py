# -*- coding: utf-8 -*-
"""P4b 回归：并行道扇出计划、终态单归档、预算账本并发串行化。

无 LLM/无网络；预算并发用例只写临时账本文件，不碰真实 state/v3_budget_ledger.json。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_runner as R  # noqa: E402

PASS = []


def check(name, cond, extra=None):
    if not cond:
        raise AssertionError("%s -> %s" % (name, extra))
    PASS.append(name)


def J(oid, jid, status="queued"):
    return {"order_id": oid, "job_id": jid, "status": status, "kind": "code", "title": oid}


def test_fill_plan():
    q = [J("C-T-G34R2", "J1"), J("C-T-G34R3", "J2"), J("C-T-L9", "J3"), J("C-T-L10", "J4"), J("C-T-L9R1", "J5")]
    prim = q[0]
    got = R._lane_fill_plan(q, prim, {"C-T-OTHER"}, 2)
    check("A 扇出计划按 FIFO 取 2 个异家族单", [x["job_id"] for x in got] == ["J3", "J4"], got)
    got = R._lane_fill_plan(q, prim, set(), 9)
    check("B 同家族（含 primary 家族）绝不并行", [x["job_id"] for x in got] == ["J3", "J4"], got)
    check("C 空槽=0 不扇出", R._lane_fill_plan(q, prim, set(), 0) == [])
    check("D 单槽(扣掉 primary 后 0)不扇出", R._lane_fill_plan(q, prim, set(), 0) == [])
    check("E primary=None 时也按家族去重", [x["job_id"] for x in R._lane_fill_plan(q, None, set(), 2)] == ["J1", "J3"])


def test_reap():
    q = {
        "jobs": [J("C-T-A", "A1", "cancelled"), J("C-T-B", "B1", "queued"),
                 J("C-T-C", "C1", "needs_human"), J("C-T-D", "D1", "done"),
                 J("C-T-E", "E1", "failed")],
        "history": [{"order_id": "C-T-D", "job_id": "D1", "status": "done"}],
    }
    moved = R.reap_terminal_jobs(q)
    check("F 终态单出 jobs", [j["job_id"] for j in q["jobs"]] == ["B1", "C1"], q["jobs"])
    check("G 终态单打归档戳", all(m.get("archived_from_jobs") is True and m.get("archived_at") for m in moved))
    check("H history 不重复登记同 job_id", [h["job_id"] for h in q["history"]].count("D1") == 1, q["history"])
    check("I needs_human 不归档（done/cancelled/failed 归档）", [m["job_id"] for m in moved] == ["A1", "D1", "E1"],
          [m["job_id"] for m in moved])
    q2 = {"jobs": [], "history": []}
    check("J 空队列安全", R.reap_terminal_jobs(q2) == [] and q2 == {"jobs": [], "history": []})


CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import factory_runner as R
R.BUDGET_LEDGER_PATH = sys.argv[2]
for _ in range(10):
    R.record_stage_budget("T-CONC", {"lanes": {}, "requests": 1, "cost_cny": 0.0,
                                     "cost_est_cny": 0.0, "tokens_in": 0, "tokens_out": 0,
                                     "tokens_cached": 0, "tokens_est_in": 0,
                                     "tokens_est_out": 0, "estimated_requests": 0}, 0.0)
print("ok")
'''


def test_budget_serialized():
    tmp = tempfile.mkdtemp(prefix="p4b_budget_")
    try:
        script = os.path.join(tmp, "child.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(CHILD)
        ledger = os.path.join(tmp, "ledger.json")
        procs = [subprocess.Popen([sys.executable, "-I", "-X", "utf8", script, HERE, ledger],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
        outs = [p.communicate(timeout=180) for p in procs]
        rcs = [p.returncode for p in procs]
        check("K 8 个并发进程全部成功", rcs == [0] * 8, [(rc, (o[1] or b"").decode("utf-8", "replace")[-200:]) for rc, o in zip(rcs, outs)])
        d = json.load(open(ledger, encoding="utf-8"))
        check("L 80 次并发读改写零丢失(requests=80)", int(d.get("daily_requests") or 0) == 80, d.get("daily_requests"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_budget_month_carry():
    """2026-11-25 修：跨日滚动只清日计数，当月月包计数必须保留（否则月包 90% 熔断永不触发）。"""
    import datetime as _dt
    tmp = tempfile.mkdtemp(prefix="p4b_month_")
    old = R.BUDGET_LEDGER_PATH
    try:
        ledger = os.path.join(tmp, "ledger.json")
        now = _dt.datetime.now()
        cur = now.strftime("%Y-%m")
        prev = (now.replace(day=1) - _dt.timedelta(days=1)).strftime("%Y-%m")
        data = {"date": "2000-01-01", "daily_requests": 77, "daily_cost_cny": 1.5,
                "month_lane_tokens": {cur: {"r4": 123, "r42": 456}, prev: {"r4": 999}}}
        with open(ledger, "w", encoding="utf-8") as f:
            json.dump(data, f)
        R.BUDGET_LEDGER_PATH = ledger
        d = R.load_budget_ledger()
        check("R 跨日滚动保留当月月包计数", d["month_lane_tokens"].get(cur) == {"r4": 123, "r42": 456},
              d.get("month_lane_tokens"))
        check("S 跨日滚动丢弃他月并清零日计数",
              prev not in (d.get("month_lane_tokens") or {}) and d.get("daily_requests") == 0
              and d.get("date") == R.today_str(), (d.get("date"), d.get("daily_requests")))
    finally:
        R.BUDGET_LEDGER_PATH = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_order_scan_union():
    """2026-11-24 P4b：orders/ 归档后，重建/看板扫描口径必须并扫 Archive_V2_*/orders。"""
    import mes_core
    import mes_orders
    base = mes_core.DEFAULT_ORDERS
    roots = mes_core.order_scan_dirs(base)
    check("M 默认 orders 扫描含归档根", len(roots) >= 2 and any("Archive_V2_" in r for r in roots), roots)
    other = os.path.join(HERE, "tests")
    check("N 显式其他目录不并扫归档（零行为变更）", mes_core.order_scan_dirs(other) == [other])
    live_n = len([n for n in os.listdir(base) if n.startswith("C-") and os.path.isfile(os.path.join(base, n, "order.md"))])
    cards = mes_core.list_cards(base)
    check("O 重建口径含归档单（多于活单目录）", len(cards) > live_n, (len(cards), live_n))
    got = {c for c, _p in cards}
    check("P 已知归档单可解析到卡路径", os.path.isfile(mes_core.order_card_path("C-20261120-008", base))
          and "C-20261120-008" in got)
    loaded = mes_orders.load_all()
    check("Q 看板口径 load_all 同样含归档单", {o["code"] for o in loaded["orders"]} >= got, len(loaded["orders"]))


def main():
    test_fill_plan()
    test_reap()
    test_budget_serialized()
    test_budget_month_carry()
    test_order_scan_union()
    print("PASS %d/%d" % (len(PASS), len(PASS)))
    for p in PASS:
        print("  [PASS]", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
