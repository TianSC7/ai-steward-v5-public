#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_slot_dispatch.py — G40b 派发接线离线单测：槽占满/第三拒/释放可再占、同家族顺延、
家族口径（同泵 base_of）、_lane_qcommit 并行道行级合并防互覆、reconcile live_orders 双态。
临时目录注入状态根 + QUEUE_PATH 重定向，真实 state/ 零触碰；全 [PASS] → exit 0。"""
import json, os, shutil, sys, tempfile
from pathlib import Path

STATE = Path(tempfile.mkdtemp(prefix="slot_dispatch_test_")).resolve()
os.environ["STEWARD_STATE_DIR"] = str(STATE)         # import 前注入：slot_ledger 状态根隔离
sys.path.insert(0, str(Path(__file__).resolve().parent))
import slot_ledger as L                              # noqa: E402
import factory_runner as R                           # noqa: E402

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" -> {extra}"))

REAL_Q = Path(R.STEWARD_ROOT) / "state" / "factory_queue.json"
REAL_Q_SHA = REAL_Q.read_bytes() if REAL_Q.exists() else b"<absent>"
REAL_S = Path(R.STEWARD_ROOT) / "state" / "runner_slots.json"
REAL_S_SHA = REAL_S.read_bytes() if REAL_S.exists() else b"<absent>"

try:
    # ---- A) 槽位生命周期：两道占满 → 第三拒 → 释放后可再占 ----
    r1 = L.slot_claim("T-O1", "FAM-A", max_slots=2)
    r2 = L.slot_claim("T-O2", "FAM-B", max_slots=2)
    check("1 两道 claim 成功(used=2)", r1.get("ok") is True and r2.get("ok") is True and r2.get("used") == 2, (r1, r2))
    r3 = L.slot_claim("T-O3", "FAM-C", max_slots=2)
    check("2 满槽第三单拒绝(reason=full)", r3.get("ok") is False and r3.get("reason") == "full", r3)
    check("3 同家族另单也拒(family_busy 优先于 full)", L.slot_claim("T-O1b", "FAM-A", max_slots=2).get("reason") == "family_busy")
    L.slot_release("T-O2")
    r4 = L.slot_claim("T-O3", "FAM-C", max_slots=2)
    check("4 释放后可再占", r4.get("ok") is True and r4.get("used") == 2, r4)

    # ---- B) 家族口径（同泵 base_of / monitor5）----
    fam_cases = [
        ("C-20261124-G34R2", "C-20261124-G34"),      # 紧贴真代数
        ("C-20261124-G34R12", "C-20261124-G34"),
        ("C-20261124-G34-R60", "C-20261124-G34"),    # 24h 打回 Rev
        ("C-20261124-G34-R8617", "C-20261124-G34"),
        ("C-20261124-G34", "C-20261124-G34"),        # 母单
        ("C-20261124-L1", "C-20261124-L1"),          # 无后缀
        ("C-X-R1", "C-X-R1"),                        # 短 Rev（-R1 两位以下不算）同泵口径
        ("", ""), (None, ""),
    ]
    check("5 家族口径 9 例全对", all(R._family_of(k) == v for k, v in fam_cases),
          [(k, R._family_of(k), v) for k, v in fam_cases if R._family_of(k) != v])

    # ---- C) _lane_pick：同家族第二单顺延取下一单（规格验收项）----
    def J(oid, jid): return {"order_id": oid, "job_id": jid, "status": "queued", "kind": "code"}
    q = [J("C-T-G34R2", "J1"), J("C-T-G34R3", "J2"), J("C-T-L9", "J3")]
    pick = R._lane_pick(q, {"C-T-G34"})              # G34R2 在跑：R3 同族跳过，取 L9
    check("6 同家族第二单顺延,取下一单", pick is not None and pick["job_id"] == "J3", pick)
    check("7 全家族忙→None(本轮静默退出)", R._lane_pick(q, {"C-T-G34", "C-T-L9"}) is None)
    check("8 空队列→None", R._lane_pick([], set()) is None)
    check("9 母单在跑,代数单同族也跳", R._lane_pick([J("C-T-G34R1", "J9"), J("C-T-L2", "J8")], {"C-T-G34"})["job_id"] == "J8")

    # ---- D) reconcile live_orders 双态 ----
    qq = {"jobs": [{"order_id": "A1", "job_id": "a1", "status": "running"},
                   {"order_id": "A2", "job_id": "a2", "status": "running"}]}
    R.reconcile_queue_state(qq, live_orders={"A1"})
    st = {j["order_id"]: j["status"] for j in qq["jobs"]}
    check("10 活槽单保留 running,非活单回 queued", st == {"A1": "running", "A2": "queued"}, st)
    qq2 = {"jobs": [{"order_id": "B1", "job_id": "b1", "status": "running"}]}
    R.reconcile_queue_state(qq2)                     # 默认 None=旧行为
    check("11 live_orders 缺省时照旧重置(两态等价)", qq2["jobs"][0]["status"] == "queued")

    # ---- E) _lane_qcommit 并行道行级合并（陈旧内存副本不得覆写他道行）----
    R._LANE["mode"] = True
    R.QUEUE_PATH = str(STATE / "factory_queue.json")
    q0 = {"jobs": [{"job_id": "J1", "order_id": "C-T-A", "status": "running"},
                   {"job_id": "J2", "order_id": "C-T-B", "status": "running"}],
          "history": []}
    R.atomic_write_json(R.QUEUE_PATH, q0)
    import copy
    j1a = dict(q0["jobs"][0]); j1a["run_id"] = "RUN-A"        # 道 A：J1 起跑落账
    R._lane_qcommit(j1a, copy.deepcopy(q0))
    q_b_stale = copy.deepcopy(q0)                              # 道 B 的内存副本（J1 还没 run_id）
    j2b = dict(q0["jobs"][1]); j2b["run_id"] = "RUN-B"        # 道 B：J2 起跑落账
    R._lane_qcommit(j2b, q_b_stale)
    disk = json.loads(Path(R.QUEUE_PATH).read_text(encoding="utf-8"))
    rows = {j["job_id"]: j for j in disk["jobs"]}
    check("12 陈旧副本不覆写他道行(J1 保有 RUN-A)", rows.get("J1", {}).get("run_id") == "RUN-A" and rows.get("J2", {}).get("run_id") == "RUN-B", disk)
    check("13 行位次保持(J1 在前)", [j["job_id"] for j in disk["jobs"]] == ["J1", "J2"])
    j1a["status"] = "done"                                    # 道 A 收口
    R._lane_qcommit(j1a, copy.deepcopy(q0))
    j2b["status"] = "done"                                    # 道 B 收口（用更陈旧的 q0 副本）
    R._lane_qcommit(j2b, copy.deepcopy(q0))
    disk = json.loads(Path(R.QUEUE_PATH).read_text(encoding="utf-8"))
    check("14 双收口后 jobs 清空,history 各一行", disk["jobs"] == [] and [h["job_id"] for h in disk["history"]] == ["J1", "J2"], disk)

    # ---- F) 单道态（mode=False）保持原行为：fresh 收口 reload 合并 / 全量写 ----
    R._LANE["mode"] = False
    R.atomic_write_json(R.QUEUE_PATH, {"jobs": [dict(q0["jobs"][1])], "history": []})   # 磁盘只剩 J2（他道已收 J1）
    j1a2 = dict(q0["jobs"][0]); j1a2["status"] = "done"
    R._lane_qcommit(j1a2, copy.deepcopy(q0), fresh=True)      # 旧 closeout：reload 后合并
    disk = json.loads(Path(R.QUEUE_PATH).read_text(encoding="utf-8"))
    check("15 单道 fresh 收口=旧口径(reload 后合并)", [h["job_id"] for h in disk["history"]] == ["J1"] and [j["job_id"] for j in disk["jobs"]] == ["J2"], disk)
    qq3 = {"jobs": [dict(q0["jobs"][0])], "history": []}      # 非收口=原全量写
    R._lane_qcommit(dict(q0["jobs"][0]), qq3)
    check("16 单道非收口=原全量写(q 即落盘)", json.loads(Path(R.QUEUE_PATH).read_text(encoding="utf-8")) == qq3)

    # ---- G) 隔离：真实 state/ 零触碰 ----
    check("17 真实 factory_queue.json 未被改动", (not REAL_Q.exists() and REAL_Q_SHA == b"<absent>") or REAL_Q.read_bytes() == REAL_Q_SHA)
    # 2026-11-24 P4b：N=2 上线后真实账本本来就会存在，判据改为「测试前后未被本测试改动」（存在性不再是前提）
    check("18 真实 runner_slots.json 未被本测试改动",
          (not REAL_S.exists() and REAL_S_SHA == b"<absent>") or REAL_S.read_bytes() == REAL_S_SHA,
          {"exists": REAL_S.exists(), "before": REAL_S_SHA[:8].decode("latin-1"), "after": (REAL_S.read_bytes()[:8].decode("latin-1") if REAL_S.exists() else "<absent>")})
    check("19 _LANE 状态复位(不影响后续 import)", R._LANE == {"mode": False, "order": ""})
finally:
    shutil.rmtree(str(STATE), ignore_errors=True)
print(f"[SUMMARY] PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
