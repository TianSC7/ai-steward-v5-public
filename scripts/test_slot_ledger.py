#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_slot_ledger.py — 槽位账本离线单测：临时目录注入状态根，不碰真实 state；全 [PASS] → exit 0。"""
import json, os, shutil, sys, tempfile, time
from pathlib import Path
STATE = Path(tempfile.mkdtemp(prefix="slot_ledger_test_")).resolve()
os.environ["STEWARD_STATE_DIR"] = str(STATE)         # import 前注入：真实 state/ 零触碰
sys.path.insert(0, str(Path(__file__).resolve().parent))
import slot_ledger as L                              # noqa: E402
PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" -> {extra}"))
def cfg(text):
    p = STATE / "steward_cfg.json"; p.write_text(text, encoding="utf-8"); return L.slots_max(p)
try:
    check("0 状态根隔离到临时目录", L.state_dir() == STATE)
    r = L.slot_claim("O1", "A", max_slots=2)
    check("1 空账本 claim 成功", r.get("ok") is True and r["slot"]["family"] == "A" and r["slot"]["pid"] == os.getpid(), r)
    check("2 同家族第二单拒绝(family_busy)", L.slot_claim("O2", "A", max_slots=2).get("reason") == "family_busy")
    check("3 不同家族补满 used=2", L.slot_claim("O2", "B", max_slots=2).get("used") == 2)
    check("4 到 max 后拒绝(full)", L.slot_claim("O3", "C", max_slots=2).get("reason") == "full")
    raw = json.loads((STATE / "runner_slots.json").read_text(encoding="utf-8"))
    check("5 落盘结构 {max,slots{family,pid,claimed_at,heartbeat_at}}", raw["max"] == 2 and set(raw["slots"]) == {"O1", "O2"} and set(raw["slots"]["O1"]) == {"family", "pid", "claimed_at", "heartbeat_at"}, raw)
    check("6 release 后可再 claim", L.slot_release("O2").get("released") is True and L.slot_claim("O3", "C", max_slots=2).get("ok") is True)
    check("7 release 不存在即 no-op", L.slot_release("NO-SUCH").get("released") is False)
    with L._lock():                                  # 锁内人为造 stale 心跳
        d = L._read(); d["slots"]["O1"]["heartbeat_at"] = time.time() - L.LOCK_STALE_SEC - 600; L._write(d)
    got = {x["order_id"]: x["reason"] for x in L.slot_reap()["reaped"]}
    check("8 stale heartbeat 被 reap 回收", got.get("O1") == "stale", got)
    check("9 reap 后快照不含 O1", "O1" not in [x["order_id"] for x in L.slot_snapshot()["slots"]])
    check("10 心跳成功刷新(errors=0)", L.slot_heartbeat("O3").get("errors") == 0)
    bad = L.slot_heartbeat("NO-SUCH")
    lines = (STATE / "runner_slots_heartbeat_err.jsonl").read_text(encoding="utf-8").splitlines()
    check("11 心跳失败写 err jsonl(ts/order/error)+计数", bad.get("errors") == 1 and len(lines) == 1 and json.loads(lines[0]).get("order") == "NO-SUCH" and {"ts", "order", "error"} <= set(json.loads(lines[0])), (bad, lines))
    check("12 slots_max: max=3→3 / enabled=false→1 / 缺省→2", (cfg('{"v4":{"slots":{"max":3}}}'), cfg('{"v4":{"slots":{"max":3,"enabled":false}}}'), cfg('{"v4":{}}')) == (3, 1, 2))
    rb = L._txn("boom", lambda _d: (_ for _ in ()).throw(RuntimeError("boom-in-lock")))   # 锁内抛异常
    rc = L.slot_claim("O9", "Z", max_slots=3)        # 若能拿到锁=前一次异常已释放
    check("13 锁内异常后锁已释放(可再 claim)且 WARN 落盘", rb.get("ok") is False and "boom-in-lock" in rb.get("error", "") and rc.get("ok") is True and "boom-in-lock" in (STATE / "slot_ledger_warn.log").read_text(encoding="utf-8"), (rb, rc))
    L.slot_claim("D1", "P", max_slots=4, pid=999999)                # 心跳新鲜但 pid 必死
    got2 = {x["order_id"]: x["reason"] for x in L.slot_reap()["reaped"]}
    check("14 死 pid 槽被 reap 回收(dead_pid)且活槽保留", got2.get("D1") == "dead_pid" and "D1" not in json.loads((STATE / "runner_slots.json").read_text(encoding="utf-8"))["slots"] and "O9" in json.loads((STATE / "runner_slots.json").read_text(encoding="utf-8"))["slots"], got2)
    check("15 真实 state/runner_slots.json 未被创建", not (Path(L.PLUGIN_ROOT) / "state" / "runner_slots.json").exists())
finally:
    shutil.rmtree(str(STATE), ignore_errors=True)
print(f"[SUMMARY] PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
