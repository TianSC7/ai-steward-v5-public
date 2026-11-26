# -*- coding: utf-8 -*-
"""autopilot_pump 的 V5.0 口径：
- 重派自动带种（--seed-from；上一代双门已过的再加 --seed-gates，只补双门 + 合入）；
- 新一代规格带上代质检结论（缺陷逐条销账）；专家提示词不再让专家「修订验收」、不再「只诊断不改文件」；
- 重派代数从「最近一次人下的那一代」往后数 MAX_GEN 代：带种复活的家族重新有额度，没被复活的老家族行为不变；
- 代数上限保持 3。

全程在临时目录里跑：队列 / 事件 / 日志 / 考卷都指到临时目录，RUNNER 换成记账脚本——不碰真队列，不派真单。"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import autopilot_pump as ap  # noqa: E402

STUB = r'''import json, os, sys
with open(os.environ["PUMP_TEST_CALLS"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
print("ok")
'''
BASE = "C-20261125-G"
OLD = "20261122_090000"          # 三天前：超出「人下的那一代」的有效期
NOW_TS = time.strftime("%Y%m%d_%H%M%S")


def _report(folder, name, verdict, defects=(), rework="", summary="", vsummary=""):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"final_summary": summary,
                   "attempts": [{"attempt": 1, "verifier_verdict": {"verdict": verdict, "defects": list(defects),
                                                                    "rework_prompt": rework, "summary": vsummary}}]}, fh,
                  ensure_ascii=False)
    return path


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


class PumpTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="pumpv5_")
        self.addCleanup(shutil.rmtree, self.home, True)
        state = os.path.join(self.home, "state")
        os.makedirs(state)
        self.calls = os.path.join(self.home, "calls.jsonl")
        stub = os.path.join(self.home, "fake_runner.py")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(STUB)
        os.environ["PUMP_TEST_CALLS"] = self.calls
        for name, value in (("ROOT", self.home), ("STATE", state), ("QF", os.path.join(state, "factory_queue.json")),
                            ("EV", os.path.join(state, "events.jsonl")), ("LOG", os.path.join(state, "pump.log")),
                            ("SPEC_DIR", os.path.join(state, "spec_autopilot")), ("RUNNER", stub),
                            ("STARVE_MARK", os.path.join(state, "autopilot_last_starve.ts"))):
            self._patch(name, value)
        self._patch("runner_busy", lambda: False)

    def _patch(self, name, value):
        orig = getattr(ap, name)
        setattr(ap, name, value)
        self.addCleanup(setattr, ap, name, orig)

    def _last(self, oid, status, ts=NOW_TS, pump=False, **over):
        spec = os.path.join(ap.SPEC_DIR if pump else self.home, oid + ".md")
        return {"order_id": oid, "job_id": "%s_%s" % (oid, ts), "status": status, "kind": "code", "title": "看板修复 " + oid,
                "repo": r"C:\factory", "targets": ["dashboard/v2.html", "scripts/v3_data.py"], "spec_file": spec, **over}

    def _run(self, jobs, hist):
        with open(ap.QF, "w", encoding="utf-8") as fh:
            json.dump({"jobs": jobs, "history": hist}, fh, ensure_ascii=False)
        ap.main()
        out = []
        if os.path.isfile(self.calls):
            with open(self.calls, encoding="utf-8") as fh:
                out = [json.loads(ln) for ln in fh if ln.strip()]
        return {_flag(c, "--order"): c for c in out}

    def _spec(self, oid):
        with open(os.path.join(ap.SPEC_DIR, oid + ".md"), encoding="utf-8") as fh:
            return fh.read()

    def _phases(self):
        with open(ap.EV, encoding="utf-8") as fh:
            return [json.loads(ln)["phase"] for ln in fh if ln.strip()]

    def test_redispatch_carries_seed_and_last_verdict(self):
        a = BASE + "A1R2"
        rep = _report(self.home, "a.json", "FAIL", ["P0 页面白屏：手机端打开即白", "P1 标题过长"],
                      "修掉顶层的 TypeError，再补真机截图", "两轮都没把白屏降下去")
        calls = self._run([], [self._last(BASE + "A1", "failed", OLD), self._last(a, "failed", pump=True, report_json=rep)])
        new = BASE + "A1R3"
        self.assertEqual(list(calls), [new])
        cmd = calls[new]
        self.assertEqual(cmd[0], "enqueue")
        self.assertEqual(_flag(cmd, "--seed-from"), a, cmd)                   # 带种重下：新一代不从头做
        self.assertNotIn("--seed-gates", cmd, cmd)                             # 上代没过双门 → 先施工再过双门
        self.assertEqual(_flag(cmd, "--targets"), "dashboard/v2.html,scripts/v3_data.py")
        self.assertEqual(_flag(cmd, "--max-attempts"), "2")
        spec = self._spec(new)
        self.assertIn("## 上代质检结论（必须逐条销账）", spec)
        self.assertIn("- [ ] P0 页面白屏：手机端打开即白", spec)
        self.assertIn("两轮都没把白屏降下去", spec)
        self.assertIn("修掉顶层的 TypeError", spec)
        self.assertIn("## 过期证据", spec)                                       # 上代的通过记录不能当本代验收

    def test_pass_verdict_only_refills_gates(self):
        b = BASE + "B1R2"
        rep = _report(self.home, "b.json", "PASS", summary="双门已过，合入被工厂侧挡住")
        calls = self._run([], [self._last(BASE + "B1", "failed", OLD), self._last(b, "failed", pump=True, report_json=rep)])
        new = BASE + "B1R3"
        self.assertEqual(list(calls), [new])
        self.assertEqual(_flag(calls[new], "--seed-from"), b)
        self.assertIn("--seed-gates", calls[new], calls[new])                 # 上一代只差合入：新一代只补双门 + 合入
        self.assertNotIn("## 上代质检结论（必须逐条销账）", self._spec(new))     # 没有缺陷清单就别硬塞一段

    def test_expert_note_no_longer_edits_acceptance(self):
        c = BASE + "C1R2"
        rep = _report(self.home, "c.json", "FAIL", ["P0 订单抽屉空白"], "改 details 的映射", "内部两轮零收敛")
        calls = self._run([], [self._last(BASE + "C1", "failed", OLD), self._last(c, "needs_human", pump=True, report_json=rep)])
        new = BASE + "C1R3"
        self.assertEqual(list(calls), [new])                                    # 专家单排在前，且照常派
        self.assertEqual(_flag(calls[new], "--seed-from"), c)
        self.assertIn("架构升级R3", _flag(calls[new], "--title"))
        spec = self._spec(new)
        self.assertIn("## 驻厂外聘专家（R3）", spec)
        self.assertIn("活工厂对专家只读", spec)
        self.assertIn("验收条件不归专家改", spec)
        self.assertIn("P0 订单抽屉空白", spec)
        self.assertNotIn("由你修订验收", spec)                                    # 旧写法：专家可以改验收——越权
        self.assertNotIn("你只诊断、不改文件", spec)                              # 旧写法：专家只出主意不动手

    def test_unrevived_old_family_stays_dead(self):
        """人下单已太早的家族：泵派到 R3 之后照旧判死，不因为历史上有人重下过而被拉起来。"""
        self.assertEqual(ap.MAX_GEN, 3)
        d = BASE + "D1"
        calls = self._run([], [self._last(d, "failed", OLD), self._last(d + "R2", "failed", OLD),       # 人下的 R2（三天前）
                               self._last(d + "R3", "failed", "20261124_090000", pump=True)])          # 泵派的 R3
        self.assertEqual(calls, {})
        self.assertTrue(os.path.isfile(os.path.join(ap.SPEC_DIR, d + ".dead")))
        self.assertEqual(self._phases(), ["autopilot_dead"])

    def test_manual_revival_refills_the_generation_budget(self):
        """工程师带种手动复活到 R4：R4 再败，泵带着 R4 的成果派 R5，而不是复活一次就又判死。"""
        d = BASE + "E1"
        os.makedirs(ap.SPEC_DIR, exist_ok=True)
        open(os.path.join(ap.SPEC_DIR, d + ".dead"), "w").close()              # 上一次判死留下的标记
        calls = self._run([], [self._last(d + "R3", "failed", OLD, pump=True), self._last(d + "R4", "failed", "20261125_100000")])
        self.assertEqual(list(calls), [d + "R5"])
        self.assertEqual(_flag(calls[d + "R5"], "--seed-from"), d + "R4")
        self.assertFalse(os.path.exists(os.path.join(ap.SPEC_DIR, d + ".dead")))   # 旧标记作废，下次判死事件照发

    def test_revived_family_dies_again_after_three_more_rounds(self):
        d = BASE + "F1"
        hist = [self._last(d + "R3", "failed", OLD, pump=True), self._last(d + "R4", "failed", "20261125_100000")]
        hist += [self._last(d + "R%d" % n, "failed", "20261125_1%d0000" % n, pump=True) for n in (5, 6, 7)]
        calls = self._run([], hist)
        self.assertEqual(calls, {})
        self.assertEqual(self._phases(), ["autopilot_dead"])

    def test_live_family_is_left_alone(self):
        e = BASE + "G1"
        calls = self._run([{"order_id": e + "R1", "status": "running"}], [self._last(e, "failed", "20261125_100000")])
        self.assertEqual(calls, {})

    def test_occupied_generation_is_skipped(self):
        f = BASE + "H1"
        calls = self._run([], [self._last(f, "failed", "20261125_100000"), self._last(f + "R1", "cancelled", "20261125_090000")])
        self.assertEqual(list(calls), [f + "R2"])
        self.assertEqual(_flag(calls[f + "R2"], "--seed-from"), f)

    def test_report_missing_is_tolerated(self):
        g = BASE + "I1"
        calls = self._run([], [self._last(g, "failed", "20261125_100000", report_json=os.path.join(self.home, "nope.json"))])
        self.assertEqual(list(calls), [g + "R1"])
        self.assertEqual(_flag(calls[g + "R1"], "--seed-from"), g)
        self.assertEqual(ap.last_verdict_material({"report_json": "nope"}), ("", "", "", []))


if __name__ == "__main__":
    atexit.register(lambda: os.environ.pop("PUMP_TEST_CALLS", None))
    unittest.main()
