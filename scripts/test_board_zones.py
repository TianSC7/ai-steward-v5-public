#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_board_zones.py —— C-009 七区数据层自测（脚本式，非 pytest）

覆盖：① 真实源七区可取数且字段齐；② 七区缺源各自降级（status ∈ missing/degraded，note 非空，不补假数）。
用法：python -X utf8 scripts/test_board_zones.py
退出码：0 全过；1 有失败。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_zones  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def with_env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: v for k, v in kw.items() if v is not None})
    return old


def restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_real():
    z = board_zones.collect_zones()
    check("契约号", z.get("contract") == "c009-zones-v1")
    check("七区齐全", set(z["zones"]) == {"orders", "seats", "events", "capacity", "alerts", "cost", "handover"})
    check("zone_status 七键", set(z["zone_status"]) == set(z["zones"]))
    o = z["zones"]["orders"]
    it = (o.get("items") or [{}])[0]
    need = {"code", "title", "state", "priority", "deadline", "route", "owner",
            "station_min", "overdue", "delay_note", "pickup", "signature", "node", "sla_x"}
    check("订单字段齐（派单契约）", need <= set(it), str(sorted(need - set(it))))
    check("订单有真数据", (o.get("summary") or {}).get("orders", 0) > 0)
    c = z["zones"]["cost"]
    check("token 非 0 非空", isinstance(c.get("today_tokens"), int) and c["today_tokens"] > 0,
          str(c.get("today_tokens")))
    a = z["zones"]["alerts"]
    check("告警可解析", len(a.get("items") or []) > 0, "n=%d" % len(a.get("items") or []))
    for k, v in z["zones"].items():
        check("区带 source：%s" % k, bool(v.get("source")))


def test_missing_all():
    tmp = tempfile.mkdtemp(prefix="c009-missing-")
    try:
        empty = os.path.join(tmp, "orders")
        os.makedirs(empty)
        old = with_env(MES_ORDERS_DIR=empty,
                       MES_STATE_DIR=os.path.join(tmp, "state"),
                       MES_REPORTS_DIR=os.path.join(tmp, "reports"),
                       MES_CLI_DB=os.path.join(tmp, "none.sqlite"),
                       MES_SHIFT_DIR=os.path.join(tmp, "shift"),
                       MES_SHIFT_FILE=os.path.join(tmp, "shift", "none.md"))
        z = board_zones.collect_zones()
        want = {"orders": {"missing"}, "seats": {"missing"}, "events": {"degraded", "missing"},
                "capacity": {"missing"}, "alerts": {"missing"}, "cost": {"missing"}, "handover": {"missing"}}
        for k, ok in want.items():
            v = z["zones"][k]
            check("缺源降级 %s" % k, v.get("status") in ok, "status=%s note=%s" % (v.get("status"), (v.get("note") or "")[:40]))
            check("缺源有 note %s" % k, bool(v.get("note")))
        check("缺源不补假 token", z["zones"]["cost"].get("today_tokens") == "不可达")
        check("缺源订单 0（不是假数组）", z["zones"]["orders"]["summary"].get("orders") == 0)
    finally:
        restore(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_orders_partial():
    """有卡但缺 §10/§11/§2.1：该单字段降级为可读空态，不抛异常。"""
    tmp = tempfile.mkdtemp(prefix="c009-partial-")
    try:
        d = os.path.join(tmp, "orders", "C-TEST-PARTIAL")
        os.makedirs(d)
        open(os.path.join(d, "order.md"), "w", encoding="utf-8").write(
            "# C-TEST-PARTIAL ｜ 降级样例\n\n| 订单编号 | C-TEST-PARTIAL |\n|---|---|\n"
            "| 优先级 | P2 |\n| 当前状态 | `queued` 排队 |\n| 进度 | 阶段 1/6 · 子需求 0/1 |\n")
        old = with_env(MES_ORDERS_DIR=os.path.join(tmp, "orders"),
                       MES_STATE_DIR=os.path.join(tmp, "state"),
                       MES_REPORTS_DIR=os.path.join(tmp, "reports"),
                       MES_CLI_DB=os.path.join(tmp, "none.sqlite"))
        z = board_zones.collect_zones()
        o = z["zones"]["orders"]
        check("部分卡可解析", o["summary"]["orders"] == 1)
        it = o["items"][0]
        check("缺提货清单话术", it["pickup"] == "无提货清单")
        check("缺节点不抛错", it["node"] is None)
        check("缺延期说明为空串", it["delay_note"] == "")
    finally:
        restore(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_channel_fields():
    """T05：每席 channel 契约；A-001/W-104 有实测通道；禁 0 禁猜（无源=null）。"""
    z = board_zones.collect_zones()
    seats = z["zones"]["seats"]["items"]
    need = {"provider", "model", "as_of", "source", "status", "note", "turn", "presence"}
    bad = [i.get("seat") for i in seats if not (need <= set(i.get("channel") or {}))]
    check("席位 channel 字段齐", not bad, str(bad[:3]))
    bad2 = []
    for i in seats:
        ch = i["channel"]
        if ch["status"] not in ("ok", "missing"):
            bad2.append(i.get("seat"))
        elif ch["status"] == "ok" and not (isinstance(ch["provider"], str) and ch["provider"]
                                           and isinstance(ch["model"], str) and ch["model"]):
            bad2.append(i.get("seat"))
        elif ch["status"] == "missing" and ch["provider"] is not None:
            bad2.append(i.get("seat"))
    check("channel 三态/null 纪律", not bad2, str(bad2[:3]))
    byseat = {i.get("seat"): i for i in seats}
    for seat in ("A-001", "W-104"):
        it = byseat.get(seat)
        check("通道样例 %s 在席" % seat, bool(it))
        if it:
            ch = it["channel"]
            check("通道样例 %s ok" % seat,
                  ch["status"] == "ok" and bool(ch["provider"]) and bool(ch["model"]),
                  "%s/%s @%s" % (ch["provider"], ch["model"], ch["as_of"]))
            check("通道样例 %s 与 model 列同源" % seat, it["model"] == "%s/%s" % (ch["provider"], ch["model"]))
            check("通道样例 %s 有 presence 布尔" % seat, isinstance(ch["presence"].get("present"), bool))
    nums = [i.get("seat") for i in seats
            if isinstance(i["channel"].get("provider"), (int, float))
            or isinstance(i["channel"].get("model"), (int, float))]
    check("channel 禁 0 禁数字", not nums, str(nums[:3]))


def test_channel_missing():
    """T05：CLI DB 不可达时 channel 一律 missing + provider/model/as_of/source 全 null。"""
    tmp = tempfile.mkdtemp(prefix="c009-chan-")
    try:
        empty = os.path.join(tmp, "orders")
        os.makedirs(empty)
        sd = os.path.join(tmp, "state")
        os.makedirs(sd)
        open(os.path.join(sd, "seat_registry.json"), "w", encoding="utf-8").write(json.dumps(
            {"seats": [{"seat": "X-001", "dept": "X", "status": "active",
                        "session": "sess_deadbeef-0000-4000-8000-000000000000"}], "retired": []}))
        old = with_env(MES_ORDERS_DIR=empty, MES_STATE_DIR=sd,
                       MES_REPORTS_DIR=os.path.join(tmp, "reports"),
                       MES_CLI_DB=os.path.join(tmp, "none.sqlite"))
        z = board_zones.collect_zones()
        items = z["zones"]["seats"]["items"]
        it = items[0] if items else {}
        ch = it.get("channel") or {}
        check("无 DB 席位数=1", len(items) == 1, str(len(items)))
        check("无 DB channel missing", ch.get("status") == "missing")
        check("无 DB provider/model/as_of/source null",
              ch.get("provider") is None and ch.get("model") is None
              and ch.get("as_of") is None and ch.get("source") is None)
        check("无 DB 不补 0", ch.get("turn") is None and not isinstance(ch.get("provider"), (int, float)))
        check("无 DB presence=false", (ch.get("presence") or {}).get("present") is False)
    finally:
        restore(old)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [test_real, test_missing_all, test_orders_partial, test_channel_fields, test_channel_missing]
    for t in tests:
        t()
    print("[%s] total=%d fail=%d" % ("PASS" if not FAIL else "FAIL", len(tests), len(FAIL)))
    sys.exit(0 if not FAIL else 1)
