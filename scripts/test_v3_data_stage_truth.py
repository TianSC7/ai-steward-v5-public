# -*- coding: utf-8 -*-
"""C-20261125-G44R8（F2.7）：抽屉「阶段真值」构造用例。

在制态没法在真数据上稳定抽样（机检时本单自己还在施工段），所以用合成事件流把同一张单
截到不同时刻，断言数据层的阶段/签没签/任务进度三套口径：

  质检段   → 签没签=未签（质检中）、T01=已完成、任务 1/3、T02=质检中、state=qc、stage_cn=质检中
  判决后   → 签没签=UI审美质检员 · PASS、任务 2/3
  合入后   → 任务 3/3、谁在办=调度泵
  重试相位 → 进时间线（且条数上限不截掉施工开工/合入）
  排队单   → 不受影响

状态目录指临时目录（STEWARD_STATE_DIR），全程不读写生产 state/。
跑法：python -X utf8 scripts/test_v3_data_stage_truth.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

# 先钉死临时状态根，再 import v3_data（模块级常量按它展开，保证不碰生产）
_TMP_STATE = tempfile.mkdtemp(prefix="g44r8_stage_truth_")
os.environ["STEWARD_STATE_DIR"] = _TMP_STATE

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v3_data  # noqa: E402

CODE = "C-20991231-STAGE01"   # 合成单号：不写死任何样本单的文案或时刻
YES = "C-20991231-STAGE02"    # 另一张单，用来证明事件流互不串单

# 一张单的完整事件流（按时刻升序）：入队/开工 → 施工完工 → 机检 → 质检开庭 → 空响应重试 → 判决 → 合入
_FULL = [
    {"order": CODE, "ts": "2026-01-01 10:00:00", "phase": "builder_start", "actor": "Builder施工", "summary": "第 1 轮施工开工"},
    {"order": CODE, "ts": "2026-01-01 10:05:00", "phase": "builder_done", "actor": "Builder施工", "summary": "第 1 轮施工结束"},
    {"order": CODE, "ts": "2026-01-01 10:05:30", "phase": "gate1_probe", "actor": "内核机械裁决", "summary": "Gate1 机检 PASS"},
    {"order": CODE, "ts": "2026-01-01 10:06:00", "phase": "verifier_start", "actor": "StageA/B质检", "summary": "启动质检双门复核"},
    {"order": CODE, "ts": "2026-01-01 10:11:00", "phase": "gate2_infra_retry", "actor": "Verifier", "summary": "Verifier 返回空响应，第 1/2 次重试"},
    {"order": CODE, "ts": "2026-01-01 10:12:00", "phase": "gate2_verdict", "actor": "内核机械裁决", "summary": "内核裁决 PASS (confirmed P0=0, P1=0)"},
    {"order": CODE, "ts": "2026-01-01 10:12:30", "phase": "merged", "actor": "内核", "summary": "双门全绿合入主干"},
    # 另一张单的事件：不得串进上面这张单的阶段判断
    {"order": YES, "ts": "2026-01-01 10:00:05", "phase": "builder_start", "actor": "Builder施工", "summary": "别的单开工"},
]


def _write_events(evs):
    """把合成事件写进临时状态根的 events.jsonl，并清掉进程内 2s 尾巴缓存（夹具必须即时生效）。"""
    path = os.path.join(_TMP_STATE, "events.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for e in evs:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    v3_data._EVT_TAIL_CACHE["t"] = 0.0
    v3_data._EVT_TAIL_CACHE["ev"] = []


def _upto(phase, summary_contains=""):
    """取完整事件流里到某个相位（含）为止的那一段；带 summary 过滤时按该条件截。"""
    out = []
    for e in _FULL:
        if e["order"] != CODE:
            continue
        if e["phase"] == phase and e["summary"] != summary_contains and summary_contains:
            continue
        out.append(e)
        if e["phase"] == phase and (not summary_contains or e["summary"] == summary_contains):
            break
    # 别的单的事件照常留在同一份事件源里（串单防护要能在截断夹具里被验到）
    out.extend(e for e in _FULL if e["order"] != CODE)
    return out


def _done_word(tasks):
    return sum(1 for t in tasks if v3_data._task_done_word(t.get("status")))


class StageTruthTests(unittest.TestCase):
    def setUp(self):
        _write_events(list(_FULL))

    def test_state_root_is_temp_not_production(self):
        """状态根确实指到临时目录（本条防止夹具误写生产 state/）。"""
        self.assertTrue(os.path.abspath(v3_data.EVENTS_JSONL_PATH).lower().startswith(
            os.path.abspath(_TMP_STATE).lower()))
        self.assertNotEqual(os.path.abspath(v3_data.EVENTS_JSONL_PATH),
                            os.path.abspath(os.path.join(v3_data.STEWARD_ROOT, "state", "events.jsonl")))

    def test_qc_stage_truth(self):
        """质检段：签没签=未签（质检中）、T01=已完成、1/3、T02=质检中、state=qc、stage_cn=质检中。"""
        _write_events(_upto("gate2_infra_retry"))
        stage = v3_data._stage_from_events(CODE)
        self.assertEqual(stage, "qc")
        self.assertEqual(v3_data._signature_word(stage, True), "未签（质检中）")
        three_q = v3_data._stage_q(stage)
        self.assertEqual(three_q["state"], "qc")
        self.assertEqual(three_q["stage"], "qc")
        self.assertEqual(three_q["stage_cn"], "质检中")
        marks = v3_data._order_marks(CODE)
        self.assertTrue(marks["builder_done"])
        self.assertFalse(marks["gate2_pass"])
        t01, t02, t03 = v3_data._stage_tasks(stage, marks, True)
        self.assertEqual((t01, t02, t03), ("已完成", "质检中", "待开工"))
        self.assertEqual(_done_word([{"status": t01}, {"status": t02}, {"status": t03}]), 1)
        # 串单防护：另一张单的阶段不被这张单的事件带跑
        self.assertEqual(v3_data._stage_from_events(YES), "producing")

    def test_rework_round_resets_qc_marks(self):
        """返工开工（新的 builder_start）后：上一轮的机检/判决里程碑作废——
        施工段里 T02 不得继续写「质检中」（否则流程条在施工、任务表在质检，正是本单要消灭的口径分裂）。"""
        _write_events(_upto("gate2_verdict") + [
            {"order": CODE, "ts": "2026-01-01 10:20:00", "phase": "builder_start",
             "actor": "Builder施工", "summary": "第 2 轮施工开工（返工）"},
        ])
        stage = v3_data._stage_from_events(CODE)
        self.assertEqual(stage, "producing")
        marks = v3_data._order_marks(CODE)
        self.assertTrue(marks["builder_start"])
        self.assertFalse(marks["builder_done"])
        self.assertFalse(marks["qc_start"])
        self.assertFalse(marks["gate2_pass"])
        t01, t02, t03 = v3_data._stage_tasks(stage, marks, True)
        self.assertEqual((t01, t02, t03), ("施工中", "待开工", "待开工"))
        self.assertEqual(_done_word([{"status": t01}, {"status": t02}, {"status": t03}]), 0)

    def test_after_verdict_pass(self):
        """gate2 判 PASS 之后：签没签=UI审美质检员 · PASS、任务 2/3。"""
        _write_events(_upto("gate2_verdict"))
        stage = v3_data._stage_from_events(CODE)
        self.assertEqual(stage, "awaiting-release")
        self.assertEqual(v3_data._signature_word(stage, True), "UI审美质检员 · PASS")
        marks = v3_data._order_marks(CODE)
        self.assertTrue(marks["gate2_pass"])
        t01, t02, t03 = v3_data._stage_tasks(stage, marks, True)
        self.assertEqual((t01, t02), ("已完成", "已通过"))
        self.assertEqual(_done_word([{"status": t01}, {"status": t02}, {"status": t03}]), 2)

    def test_after_merge_three_of_three_and_pump_owns(self):
        """合入后：任务 3/3；谁在办 = 流程当前节点（交付）的主人 = 调度泵。"""
        stage = v3_data._stage_from_events(CODE)
        self.assertEqual(stage, "awaiting-closure")
        marks = v3_data._order_marks(CODE)
        self.assertTrue(marks["merged"])
        t01, t02, t03 = v3_data._stage_tasks(stage, marks, True)
        self.assertEqual(_done_word([{"status": t01}, {"status": t02}, {"status": t03}]), 3)
        detail = v3_data._done_detail({"order_id": CODE, "title": "合成单",
                                       "enqueued_at": "2026-01-01 09:59:00",
                                       "started_at": "2026-01-01 10:00:00",
                                       "finished_at": "2026-01-01 10:12:40",
                                       "status": "done"})
        self.assertEqual(detail["three_q"]["seat"], "调度泵")
        self.assertEqual(len(detail["tasks"]), 3)
        self.assertEqual(_done_word(detail["tasks"]), 3)
        # three_q.task_status 与 T01 行逐字同源（F3.4 不变）
        self.assertEqual(detail["three_q"]["task_status"], detail["tasks"][0]["status"])
        # 同一套任务名/主人：交付前后不再换名
        self.assertEqual([(t["name"], t["owner"]) for t in detail["tasks"]],
                         [("产物构建施工", "Builder 施工席"),
                          ("质检双门复核", "UI审美质检员"),
                          ("放行与提货交付", "调度泵")])
        self.assertEqual(detail["three_q"]["stage_cn"], "已交付待验收")

    def test_retry_phase_enters_timeline(self):
        """重试相位进时间线（gate2_infra_retry 等四个门禁过程相位），且条数上限不截掉开工与合入。"""
        timeline = v3_data._events_for_order(CODE)
        msgs = [x["msg"] for x in timeline]
        self.assertTrue(any("重试" in m for m in msgs), msgs)
        self.assertTrue(any("施工开工" in m for m in msgs), msgs)
        self.assertTrue(any("合入" in m for m in msgs), msgs)
        # 时刻升序（时间线渲染依赖）
        self.assertEqual([x["ts"] for x in timeline], sorted(x["ts"] for x in timeline))
        for _ph in ("gate1_infra_retry", "gate2_json_rescue", "assertion_reprobe"):
            self.assertIn(_ph, v3_data._EVT_PHASES)

    def test_retry_burst_keeps_start_and_merge(self):
        """多次重试（挤占条数）时，施工开工与合入仍留在时间线里（上限已调大）。"""
        burst = []
        for i, e in enumerate(_FULL):
            burst.append(e)
            if e["order"] == CODE and e["phase"] == "verifier_start":
                for k in range(12):
                    burst.append({"order": CODE, "ts": "2026-01-01 10:0%d:%02d" % (6 + k // 60, k % 60),
                                  "phase": "gate2_infra_retry", "actor": "Verifier",
                                  "summary": "Verifier 返回空响应，第 %d 次重试" % (k + 1)})
        _write_events(burst)
        msgs = [x["msg"] for x in v3_data._events_for_order(CODE)]
        self.assertTrue(any("施工开工" in m for m in msgs), msgs)
        self.assertTrue(any("合入" in m for m in msgs), msgs)

    def test_queued_order_unaffected(self):
        """排队单不受影响：待开工、无阶段事件、签没签字段照旧（卡片维持「—」）。"""
        self.assertEqual(v3_data._stage_from_events("C-20991231-QUEUED"), "")
        self.assertEqual(v3_data._docs_state_word("queued"), "待开工")
        self.assertEqual(v3_data._stage_tasks("queued", {}), ["待开工", "待开工", "待开工"])
        self.assertEqual(v3_data._stage_q("queued")["stage_cn"], "待排产")
        self.assertEqual(v3_data._events_for_order("C-20991231-QUEUED"), [])

    def test_signature_falls_back_without_events(self):
        """事件缺失不造假：在制回落「未签（施工中）」，已交付回落质检 PASS。"""
        self.assertEqual(v3_data._signature_word("", True), "未签（施工中）")
        self.assertEqual(v3_data._signature_word("", False), "UI审美质检员 · PASS")
        self.assertEqual(v3_data._signature_word("needs_human", True), "未签（转人工）")
        self.assertEqual(v3_data._signature_word("producing", True), "未签（施工中）")

    def test_missing_event_file_does_not_raise(self):
        """事件源缺失：不抛错、返回空（阶段回落由调用方按在制处理）。"""
        os.remove(os.path.join(_TMP_STATE, "events.jsonl"))
        v3_data._EVT_TAIL_CACHE["t"] = 0.0
        v3_data._EVT_TAIL_CACHE["ev"] = []
        self.assertEqual(v3_data._stage_from_events(CODE), "")
        self.assertEqual(v3_data._events_for_order(CODE), [])
        self.assertFalse(v3_data._order_marks(CODE)["builder_done"])


class StageTruthWiringTests(unittest.TestCase):
    """装配级联调：上面的口径必须真的从 collect_v3_data 走到卡片/抽屉字段，而不是只停在校验函数里。
    （在制态在真数据上抽不到样本，所以用合成队列 + 合成事件流喂真装配函数。）"""

    DONE = "C-20991231-DONE01"

    def setUp(self):
        # 产线单目录是仓外的真实生产账本：联调用例只验本单口径，按夹具隔离掉（只读也没必要读）
        self._orig_line_orders = v3_data._line_orders
        v3_data._line_orders = lambda: []
        qc = _upto("gate2_infra_retry")
        done = [
            {"order": self.DONE, "ts": "2026-01-02 09:00:00", "phase": "builder_start", "actor": "Builder施工", "summary": "第 1 轮施工开工"},
            {"order": self.DONE, "ts": "2026-01-02 09:20:00", "phase": "builder_done", "actor": "Builder施工", "summary": "第 1 轮施工结束"},
            {"order": self.DONE, "ts": "2026-01-02 09:20:30", "phase": "gate1_probe", "actor": "Gate1几何机检", "summary": "Gate1 机检 PASS"},
            {"order": self.DONE, "ts": "2026-01-02 09:21:00", "phase": "verifier_start", "actor": "StageA/B质检", "summary": "启动质检双门复核"},
            {"order": self.DONE, "ts": "2026-01-02 09:25:00", "phase": "gate2_infra_retry", "actor": "Gate2空响应重试", "summary": "Verifier 返回空响应，第 1/2 次重试"},
            {"order": self.DONE, "ts": "2026-01-02 09:30:00", "phase": "gate2_verdict", "actor": "内核机械裁决", "summary": "内核裁决 PASS (confirmed P0=0, P1=0)"},
            {"order": self.DONE, "ts": "2026-01-02 09:30:30", "phase": "merged", "actor": "内核合入主干", "summary": "双门全绿合入主干"},
        ]
        _write_events(qc + done)
        # 已交付单只认「今天完工」的 history 行 → 完工时刻由夹具现算，不写死任何样本时刻
        today = v3_data._dt.datetime.now().strftime("%Y-%m-%d")
        queue = {
            "jobs": [{"job_id": "j-run", "order_id": CODE, "kind": "code", "title": "合成在制单",
                      "status": "running", "enqueued_at": today + " 09:58:00",
                      "started_at": today + " 10:00:00", "round": 1,
                      "target": "dashboard/v2.html", "targets": ["dashboard/v2.html"], "repo": "."}],
            "history": [{"job_id": "j-done", "order_id": self.DONE, "kind": "code", "title": "合成已交付单",
                         "status": "done", "enqueued_at": today + " 08:50:00",
                         "started_at": today + " 09:00:00", "finished_at": today + " 09:30:40",
                         "run_id": "r1", "disposition": "done",
                         "target": "dashboard/v2.html", "targets": ["dashboard/v2.html"], "repo": "."}],
        }
        with open(os.path.join(_TMP_STATE, "factory_queue.json"), "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False)
        self.data = v3_data.collect_v3_data()

    def tearDown(self):
        v3_data._line_orders = self._orig_line_orders
        try:
            os.remove(os.path.join(_TMP_STATE, "factory_queue.json"))
        except OSError:
            pass

    def _row(self, code):
        return [r for r in (self.data.get("orders") or []) if r.get("code") == code][0]

    def test_running_qc_card_and_drawer_wired(self):
        """在制质检段：卡片「签没签」随阶段走，抽屉 state/stage_cn/任务 1/3 真落到装配字段上。"""
        row = self._row(CODE)
        self.assertEqual(row.get("signature"), "未签（质检中）")
        self.assertEqual(row.get("owner"), "UI审美质检员")
        d = self.data["details"][CODE]
        q = d["three_q"]
        self.assertEqual((q.get("state"), q.get("stage"), q.get("stage_cn")), ("qc", "qc", "质检中"))
        self.assertEqual(q.get("task_status"), d["tasks"][0].get("status"))
        self.assertEqual([t.get("status") for t in d["tasks"]], ["已完成", "质检中", "待开工"])
        self.assertEqual(sum(1 for t in d["tasks"] if v3_data._task_done_word(t.get("status"))), 1)
        self.assertIn("重试", " ".join(str(x.get("msg")) for x in d.get("group_chat") or []))

    def test_delivered_card_and_drawer_wired(self):
        """已交付单：卡片签名/谁在办与抽屉同源（调度泵），任务名前后不变，时间线含门禁重试。"""
        row = self._row(self.DONE)
        self.assertEqual(row.get("signature"), "UI审美质检员 · PASS")
        self.assertEqual(row.get("owner"), "调度泵")
        d = self.data["details"][self.DONE]
        self.assertEqual(d["three_q"].get("seat"), "调度泵")
        self.assertEqual(d["three_q"].get("stage_cn"), "已交付待验收")
        self.assertEqual([(t.get("id"), t.get("name"), t.get("owner")) for t in d["tasks"]],
                         [("T01", "产物构建施工", "Builder 施工席"),
                          ("T02", "质检双门复核", "UI审美质检员"),
                          ("T03", "放行与提货交付", "调度泵")])
        self.assertEqual(sum(1 for t in d["tasks"] if v3_data._task_done_word(t.get("status"))), 3)
        self.assertEqual(d["three_q"].get("task_status"), d["tasks"][0].get("status"))
        text = " ".join(str(x.get("msg")) for x in d.get("group_chat") or [])
        for kw in ("入队", "施工开工", "机检", "重试", "裁决", "合入"):
            self.assertIn(kw, text)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(_TMP_STATE, ignore_errors=True)
