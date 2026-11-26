#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V4.0-P2 断言驱动质检回归测试（factory_runner.py 断言引擎 / 销账 / 基线写入 / 探针转义修复 / 拒收）。

关键回放：
  - G13（V3.2.1 时 PASS 却「验不了」）：修复前 DOM 形态 → 断言必须 FAIL；修复后 → 全 PASS；
    抽屉打不开 → 全部 UNVERIFIED（证据不足，不得当 PASS）。
  - G12：导航高 40→32 相对生产版降 20% → PASS；只降到 34 → FAIL；基线没测到 → UNVERIFIED。
  - V3.2.1 探针脚本：16 处丢失转义全部补回，parseRgb / 单号正则恢复，修复幂等。

所有写盘重定向到临时目录。用法：python -X utf8 test_v4_p2_assertions.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_runner  # noqa: E402

FR = vars(factory_runner)
TMP = tempfile.mkdtemp(prefix="v4_p2_test_")
TPL_PATH = os.path.join(HERE, "..", "config", "assertion_templates.json")
EX_DIR = next((p for p in (os.path.join(HERE, "..", "examples"), os.path.join(FR["BP_ROOT"], "spec_examples"))
               if os.path.isdir(p)), os.path.join(HERE, "..", "examples"))   # 部署后示例在 BLUEPRINT/spec_examples


def _tpls():
    return json.load(open(TPL_PATH, encoding="utf-8"))["templates"]


def _spec(assertions) -> str:
    return "# 规格\n正文\n\n```assertions\n" + json.dumps({"assertions": assertions}, ensure_ascii=False) + "\n```\n"


def _raw(sandbox: dict, baseline: dict | None = None, errors: dict | None = None) -> dict:
    t = {"sandbox": sandbox}
    if baseline is not None:
        t["baseline"] = baseline
    return {"targets": t, "errors": errors or {}}


def _m(*values, n=None):
    return {"values": list(values), "n": len(values) if n is None else n}


def _by_id(results):
    return {r["id"]: r for r in results}


def _redirect():
    FR["V4_TRACE_DIR"] = os.path.join(TMP, "v4_trace")
    FR["STEWARD_CONFIG_PATH"] = os.path.join(TMP, "steward.json")
    FR["PATROL_ALERT"] = os.path.join(TMP, "PATROL_ALERT.txt")
    FR["TOKEN_FILE"] = os.path.join(TMP, "board_token.txt")
    FR["ASSERT_TEMPLATES_CANDIDATES"] = [TPL_PATH]
    FR["INBOX_DIR"] = os.path.join(TMP, "inbox")
    with open(FR["TOKEN_FILE"], "w", encoding="ascii") as f:
        f.write("zc-TEST")


class Parse(unittest.TestCase):
    def test_no_block(self):
        self.assertEqual(FR["extract_assertions"]("# 规格\n没有断言", _tpls()), ([], []))

    def test_template_expansion_and_defaults(self):
        a, e = FR["extract_assertions"](_spec([{"id": "A1", "clause": "F1", "template": "nav_height_shrink"}]), _tpls())
        self.assertEqual(e, [])
        x = a[0]
        self.assertEqual((x["selector"], x["metric"], x["op"], x["value"], x["tolerance"]), ("#nav", "rect.height", "<=", "baseline*0.82", 0.5))
        self.assertEqual((x["state"], x["viewports"], x["required"]), ("default", [1440, 1200, 390], True))

    def test_explicit_fields_override_template(self):
        a, _ = FR["extract_assertions"](_spec([{"id": "A1", "template": "max_height", "params": {"selector": "#z6 tbody tr"}, "viewports": [390], "value": 50}]), _tpls())
        self.assertEqual((a[0]["viewports"], a[0]["value"]), ([390], 50))

    def test_missing_template_param(self):
        _, e = FR["extract_assertions"](_spec([{"id": "A1", "template": "max_height"}]), _tpls())
        self.assertTrue(any("模板参数缺失" in x and "selector" in x for x in e))

    def test_template_cannot_reach_attributes(self):
        tpl = {"evil": {"assertion": {"selector": "{x.__class__}", "metric": "count", "op": ">=", "value": "1"}}}
        a, e = FR["extract_assertions"](_spec([{"id": "A1", "template": "evil", "params": {"x": "s"}}]), tpl)
        self.assertEqual(a[0]["selector"], "{x.__class__}")   # 原样保留，不做属性求值

    def test_validation_errors(self):
        bad = [
            {"id": "1bad", "metric": "count", "selector": "a", "op": ">=", "value": 1},
            {"id": "A2", "metric": "colour", "selector": "a", "op": ">=", "value": 1},
            {"id": "A3", "metric": "count", "op": ">=", "value": 1},
            {"id": "A4", "metric": "count", "selector": "a", "op": "≈", "value": 1},
            {"id": "A5", "metric": "count", "selector": "a", "op": ">=", "value": 1, "viewports": [800]},
            {"id": "A6", "metric": "count", "selector": "a", "op": ">=", "value": 1, "state": "teleport"},
            {"id": "A7", "metric": "rect.height", "selector": "a", "op": "<=", "value": "baseline**2"},
            {"id": "A8", "metric": "js", "op": "==", "value": 1},
        ]
        a, e = FR["extract_assertions"](_spec(bad), {})
        self.assertEqual(a, [])
        self.assertEqual(len(e), 8)

    def test_duplicate_ids(self):
        one = {"id": "A1", "metric": "count", "selector": "a", "op": ">=", "value": 1}
        _, e = FR["extract_assertions"](_spec([one, dict(one)]), {})
        self.assertTrue(any("重复" in x for x in e))

    def test_bad_json(self):
        _, e = FR["extract_assertions"]("```assertions\n{oops}\n```", {})
        self.assertTrue(e and "JSON" in e[0])

    def test_state_chain(self):
        a, e = FR["extract_assertions"](_spec([{"id": "A1", "metric": "count", "selector": ".task", "op": ">=", "value": 1,
                                               "state": "zone:5 + drawer:C-20261121-001"}]), {})
        self.assertEqual((e, a[0]["state"]), ([], "zone:5+drawer:C-20261121-001"))

    def test_examples_are_valid(self):
        for fn in ("spec_C-20261122-G12.assertions.md", "spec_C-20261122-G13.assertions.md"):
            a, e = FR["extract_assertions"](open(os.path.join(EX_DIR, fn), encoding="utf-8").read(), _tpls())
            self.assertEqual(e, [], fn)
            self.assertGreaterEqual(len(a), 8, fn)


class Plan(unittest.TestCase):
    def test_grouping(self):
        a, _ = FR["extract_assertions"](open(os.path.join(EX_DIR, "spec_C-20261122-G12.assertions.md"), encoding="utf-8").read(), _tpls())
        plan = FR["build_assertion_plan"](a)
        st = {g["state"]: g for g in plan["groups"]}
        self.assertEqual(set(st), {"default", "zone:6", "zone:8", "zone:13"})
        self.assertIn("baseline", st["default"]["targets"])
        self.assertEqual(st["zone:8"]["targets"], ["sandbox"])
        self.assertEqual(st["zone:8"]["viewports"], [390])
        self.assertTrue(plan["need_baseline"])
        self.assertFalse(plan["need_degraded"])


class Data(unittest.TestCase):
    D = {"details": {"C-20261121-001": {"tasks": [{"id": "T01"}, {"id": "T02"}, {"id": "T03"}], "group_chat": [1, 2]}}, "orders": [{"code": "x"}]}

    def test_paths(self):
        rd = FR["resolve_data_path"]
        self.assertEqual(rd(self.D, "details[C-20261121-001].tasks|len"), 3)
        self.assertEqual(rd(self.D, "details[C-20261121-001].tasks[0].id"), "T01")
        self.assertEqual(rd(self.D, "orders[0].code"), "x")
        with self.assertRaises(KeyError):
            rd(self.D, "details[C-NOPE].tasks|len")


class Compare(unittest.TestCase):
    def test_ops(self):
        c = FR["compare_values"]
        self.assertTrue(c(32, "<=", 32.8))
        self.assertFalse(c(34, "<=", 32.8))
        self.assertTrue(c(32.9, "<=", 32.8, 0.5))
        self.assertTrue(c("T01 · 施工", "contains", "T01"))
        self.assertTrue(c("abc", "not_matches", r"\d"))
        self.assertTrue(c(True, "==", True))
        self.assertFalse(c(1, "==", True))
        self.assertTrue(c(14.0, "==", "14"))
        with self.assertRaises(ValueError):
            c("x", "<", 3)


G13_ASSERTS = None


def g13_asserts():
    global G13_ASSERTS
    if G13_ASSERTS is None:
        G13_ASSERTS, e = FR["extract_assertions"](open(os.path.join(EX_DIR, "spec_C-20261122-G13.assertions.md"), encoding="utf-8").read(), _tpls())
        assert not e, e
    return G13_ASSERTS


G13_DATA = {"details": {"C-20261121-001": {"tasks": [{"id": "T01"}, {"id": "T02"}, {"id": "T03"}], "group_chat": [{"ts": "08:09"}, {"ts": "08:20"}]}}}
DRAWER = "drawer:C-20261121-001"


def g13_raw(per_vp: dict, degraded_text="正常降级态", extra_errors=None):
    vps = {str(v): dict(per_vp) for v in (1440, 1200, 390)}
    deg = {str(v): {"A7": _m(degraded_text)} for v in (1440, 1200, 390)}
    dflt = {str(v): {"A8": _m(0)} for v in (1440, 1200, 390)}
    return _raw({DRAWER: vps, "degraded": deg, "default": dflt}, errors=extra_errors)


class ReplayG13(unittest.TestCase):
    def test_before_fix_fails(self):
        """V3.2.1 修复前形态：三条「[待办] —」空行、时间线 0 条。V3 判 PASS（验不了），V4 必须 FAIL。"""
        raw = g13_raw({"A1": _m(3), "A2": _m(""), "A3": _m(3), "A4": _m("任务 0/3 [待办] — [待办] — [待办] — 时间线 0 · 未接入"),
                       "A5": _m(0), "A6": _m(True)})
        r = _by_id(FR["evaluate_assertions"](g13_asserts(), raw, G13_DATA))
        self.assertEqual(r["A1"]["status"], "PASS")
        for aid in ("A2", "A4", "A5"):
            self.assertEqual(r[aid]["status"], "FAIL", aid)
        self.assertEqual(r["A3"]["status"], "PASS")      # 3 行 = 3 条，计数巧合一致，其余断言把它抓住
        s = FR["summarize_assertions"](list(r.values()))
        self.assertEqual(sorted(s["failed_ids"]), ["A2", "A4", "A5"])
        lines = FR["assertion_result_lines"](list(r.values()))
        self.assertTrue(any("A5" in ln and "实测 0" in ln and "期望 == 2" in ln for ln in lines))

    def test_after_fix_passes(self):
        raw = g13_raw({"A1": _m(3), "A2": _m("T01", "T02", "T03"), "A3": _m(3),
                       "A4": _m("任务 1/3 T01 · Git Worktree 沙箱隔离施工 · 已完成 · Builder"), "A5": _m(2), "A6": _m(True, True)})
        res = FR["evaluate_assertions"](g13_asserts(), raw, G13_DATA)
        self.assertEqual({r["id"]: r["status"] for r in res}, {f"A{i}": "PASS" for i in range(1, 9)})

    def test_drawer_cannot_open_is_unverified_not_pass(self):
        raw = g13_raw({}, extra_errors={f"sandbox|{DRAWER}": "state prep failed: order card not found: C-20261121-001"})
        r = _by_id(FR["evaluate_assertions"](g13_asserts(), raw, G13_DATA))
        for aid in ("A1", "A2", "A3", "A4", "A5", "A6"):
            self.assertEqual(r[aid]["status"], "UNVERIFIED", aid)
        s = FR["summarize_assertions"](list(r.values()))
        self.assertEqual(s["required_unverified"], ["A1", "A2", "A3", "A4", "A5", "A6"])
        self.assertEqual(s["fail"], 0)

    def test_non_required_unverified_does_not_block(self):
        raw = g13_raw({"A1": _m(3), "A2": _m("T01"), "A3": _m(3), "A4": _m("ok"), "A5": _m(2), "A6": _m(True)},
                      extra_errors={"sandbox|degraded": "no preview url for sandbox_degraded"})
        s = FR["summarize_assertions"](FR["evaluate_assertions"](g13_asserts(), raw, G13_DATA))
        self.assertEqual((s["unverified"], s["required_unverified"], s["fail"]), (1, [], 0))

    def test_data_missing_is_unverified(self):
        raw = g13_raw({"A1": _m(3), "A2": _m("T01"), "A3": _m(3), "A4": _m("ok"), "A5": _m(2), "A6": _m(True)})
        r = _by_id(FR["evaluate_assertions"](g13_asserts(), raw, None))
        self.assertEqual((r["A3"]["status"], r["A5"]["status"]), ("UNVERIFIED", "UNVERIFIED"))


class ReplayG12(unittest.TestCase):
    A = [{"id": "A1", "clause": "F1", "selector": "#nav a.active", "metric": "rect.height", "aggregate": "max", "op": "<=",
          "value": "baseline*0.82", "tolerance": 0.5, "state": "default", "viewports": [1440, 390], "required": True},
         {"id": "A4", "clause": "F2", "selector": "#z6 tbody tr", "metric": "rect.height", "op": "<=", "value": 56,
          "state": "zone:6", "viewports": [390], "required": True}]

    def _run(self, sb_nav, base_nav, rows):
        sb = {"default": {"1440": {"A1": _m(sb_nav)}, "390": {"A1": _m(sb_nav)}}, "zone:6": {"390": {"A4": _m(*rows)}}}
        bl = {"default": {"1440": {"A1": _m(base_nav)}, "390": {"A1": _m(base_nav)}}} if base_nav is not None else {}
        return _by_id(FR["evaluate_assertions"](self.A, _raw(sb, bl)))

    def test_shrunk_20pct_passes(self):
        r = self._run(32, 40, [40, 44, 52])
        self.assertEqual((r["A1"]["status"], r["A4"]["status"]), ("PASS", "PASS"))
        self.assertEqual(r["A1"]["per_viewport"][0]["baseline"], 40)

    def test_shrunk_15pct_fails(self):
        self.assertEqual(self._run(34, 40, [40])["A1"]["status"], "FAIL")

    def test_row_too_tall_fails(self):
        r = self._run(32, 40, [40, 61])
        self.assertEqual(r["A4"]["status"], "FAIL")
        self.assertEqual(r["A4"]["per_viewport"][0]["measured"], 61)

    def test_baseline_missing_unverified(self):
        self.assertEqual(self._run(32, None, [40])["A1"]["status"], "UNVERIFIED")

    def test_zero_rows_unverified_unless_allow_empty(self):
        self.assertEqual(self._run(32, 40, [])["A4"]["status"], "UNVERIFIED")
        a2 = [dict(self.A[1], allow_empty=True)]
        r = FR["evaluate_assertions"](a2, _raw({"zone:6": {"390": {"A4": _m()}}}))
        self.assertEqual(r[0]["status"], "PASS")


class Closeout(unittest.TestCase):
    R1 = {"verdict": "FAIL", "findings": [
        {"id": "F1", "severity": "P0", "status": "confirmed", "what": "字符图标①"},
        {"id": "F2", "severity": "P1", "status": "unverifiable", "what": "x"},
        {"id": "F3", "severity": "P1", "status": "confirmed", "what": "卡片不等高"},
        {"id": "F4", "severity": "P2", "status": "confirmed", "what": "微调"}]}

    def prior(self):
        return FR["prior_confirmed_items"](self.R1, 1)

    def test_prior_only_confirmed_p0_p1(self):
        self.assertEqual([p["tag"] for p in self.prior()], ["R1-F1", "R1-F3"])

    def test_g13_round2_forgot_p0_flips_to_fail(self):
        """G13 第 2 轮真实形态：没提第 1 轮的 P0「①」就 PASS —— V4 漏答按未关，判 FAIL。"""
        v2 = {"verdict": "PASS", "confirmed_p0": 0, "confirmed_p1": 0, "findings": []}
        co = FR["apply_closeout"](v2, self.prior())
        self.assertEqual((co["extra_p0"], co["extra_p1"], v2["verdict"], v2["verdict_before_closeout"]), (1, 1, "FAIL", "PASS"))
        self.assertTrue(any("R1-F1" in d for d in v2["defects"]))

    def test_all_closed_keeps_pass(self):
        v2 = {"verdict": "PASS", "confirmed_p0": 0, "confirmed_p1": 0,
              "closeout": [{"tag": "R1-F1", "status": "closed"}, {"tag": "F3", "status": "closed"}]}
        co = FR["apply_closeout"](v2, self.prior())
        self.assertEqual((co["extra_p0"], co["extra_p1"], v2["verdict"]), (0, 0, "PASS"))

    def test_p1_only_open_needs_three(self):
        v2 = {"verdict": "PASS", "confirmed_p0": 0, "confirmed_p1": 1,
              "closeout": [{"tag": "R1-F1", "status": "closed"}, {"tag": "R1-F3", "status": "still_open"}]}
        FR["apply_closeout"](v2, self.prior())
        self.assertEqual((v2["verdict"], v2["confirmed_p1_with_closeout"]), ("PASS", 2))

    def test_regressed_p0_fails(self):
        v2 = {"verdict": "PASS", "confirmed_p0": 0, "confirmed_p1": 0,
              "closeout": [{"tag": "R1-F1", "status": "regressed"}, {"tag": "R1-F3", "status": "closed"}]}
        FR["apply_closeout"](v2, self.prior())
        self.assertEqual(v2["verdict"], "FAIL")

    def test_brief_lists_all(self):
        md = FR["closeout_brief_md"](self.prior())
        self.assertIn("R1-F1", md)
        self.assertIn("R1-F3", md)
        self.assertIn("still_open", md)


class Baseline(unittest.TestCase):
    def setUp(self):
        _redirect()
        self.d = tempfile.mkdtemp(dir=TMP)
        json.dump({"order_id": "C-X", "rev": 2, "clauses": [
            {"id": "C_R1_1", "topic": "F1", "status": "active", "text": "导航收敛"},
            {"id": "C_R2_1", "topic": "颜色", "status": "active", "text": "颜色太乱"},
            {"id": "C_R0_9", "topic": "old", "status": "superseded"}]}, open(os.path.join(self.d, "contract.json"), "w", encoding="utf-8"))
        json.dump({"items": [{"summary": "保持 13 区"}]}, open(os.path.join(self.d, "baseline.json"), "w", encoding="utf-8"))

    def _results(self):
        mk = lambda aid, cl, st: {"id": aid, "clause": cl, "desc": aid, "required": True, "state": "default", "selector": "#nav",
                                  "metric": "rect.height", "op": "<=", "value": 32, "aggregate": "max", "status": st,
                                  "per_viewport": [{"viewport": 390, "status": st, "measured": 31, "expected": 32}]}
        return [mk("A1", "F1", "PASS"), mk("A2", "F2", "FAIL"), mk("A3", "F2", "UNVERIFIED")]

    def test_only_passed_written_and_contract_marked(self):
        out = FR["write_verified_baseline"]("C-X-R77", self._results(), [{"id": "A1", "metric": "rect.height"}], ord_dir=self.d)
        self.assertEqual((out["baseline_added"], out["baseline_total"]), (1, 2))
        items = json.load(open(os.path.join(self.d, "baseline.json"), encoding="utf-8"))["items"]
        self.assertEqual(items[-1]["assertion_id"], "A1")
        self.assertEqual(items[-1]["kind"], "verified_assertion")
        cl = {c["id"]: c["status"] for c in json.load(open(os.path.join(self.d, "contract.json"), encoding="utf-8"))["clauses"]}
        self.assertEqual(cl, {"C_R1_1": "done", "C_R2_1": "done_unasserted", "C_R0_9": "superseded"})

    def test_rerun_is_idempotent(self):
        FR["write_verified_baseline"]("C-X", self._results(), [], ord_dir=self.d)
        out = FR["write_verified_baseline"]("C-X", self._results(), [], ord_dir=self.d)
        self.assertEqual(out["baseline_added"], 0)

    def test_orders_dir_strips_rework_suffix(self):
        self.assertTrue(FR["orders_dir_for"]("C-20261121-001-R933").endswith(os.path.join("orders", "C-20261121-001")))


class Inherit(unittest.TestCase):
    def setUp(self):
        _redirect()

    def test_parent_assertions_inherited(self):
        sp = os.path.join(TMP, "parent_spec.md")
        open(sp, "w", encoding="utf-8").write(_spec([{"id": "A1", "metric": "count", "selector": ".task", "op": ">=", "value": 1}]))
        q = {"history": [{"order_id": "C-P", "spec_file": sp}]}
        a, e, src = FR["resolve_job_assertions"]({"parent_order_id": "C-P"}, "# 返工契约，无断言", q)
        self.assertEqual((len(a), e, src), (1, [], "parent:C-P"))

    def test_own_assertions_win(self):
        a, _, src = FR["resolve_job_assertions"]({"parent_order_id": "C-P"},
                                                 _spec([{"id": "B1", "metric": "count", "selector": "x", "op": ">=", "value": 0}]), {"history": []})
        self.assertEqual((a[0]["id"], src), ("B1", "spec"))

    def test_fence_text_roundtrip(self):
        s = _spec([{"id": "A1", "metric": "count", "selector": ".t", "op": ">=", "value": 1}])
        fence = FR["assertion_fence_text"](s)
        self.assertTrue(fence.startswith("```assertions"))
        self.assertEqual(len(FR["extract_assertions"](fence, {})[0]), 1)


class Inbox(unittest.TestCase):
    def setUp(self):
        _redirect()
        os.makedirs(FR["INBOX_DIR"], exist_ok=True)
        for fn in os.listdir(FR["INBOX_DIR"]):
            p = os.path.join(FR["INBOX_DIR"], fn)
            if os.path.isfile(p):
                os.remove(p)

    def _drop(self, oid, spec_text, kind="ui_dashboard"):
        sp = os.path.join(TMP, f"{oid}.md")
        open(sp, "w", encoding="utf-8").write(spec_text)
        json.dump({"order_id": oid, "kind": kind, "spec_file": sp, "title": oid}, open(os.path.join(FR["INBOX_DIR"], f"{oid}.json"), "w", encoding="utf-8"))

    def _flags(self, **kw):
        json.dump({"v4": kw}, open(FR["STEWARD_CONFIG_PATH"], "w", encoding="utf-8"))

    def test_rejects_ui_spec_without_assertions(self):
        self._flags(assertion_gate=True, require_assertions=True)
        self._drop("C-NOASSERT", "# 没断言")
        self._drop("C-OK", _spec([{"id": "A1", "metric": "count", "selector": ".t", "op": ">=", "value": 1}]))
        self._drop("C-CAD", "# cad 单不要求", kind="cad")
        q = {"jobs": [], "history": []}
        added = FR["ingest_inbox"](q)
        self.assertEqual((added, sorted(j["order_id"] for j in q["jobs"])), (2, ["C-CAD", "C-OK"]))
        rej = os.listdir(os.path.join(FR["INBOX_DIR"], "_rejected"))
        self.assertIn("C-NOASSERT.json", rej)
        self.assertIn("C-NOASSERT.json.reason.txt", rej)

    def test_flag_off_accepts_everything(self):
        self._flags(assertion_gate=True, require_assertions=False)
        self._drop("C-NOASSERT2", "# 没断言")
        q = {"jobs": [], "history": []}
        self.assertEqual(FR["ingest_inbox"](q), 1)


class ProbeEscapeFix(unittest.TestCase):
    def test_v321_style_template(self):
        tpl = ("const auditExpr = `(() => {\n"
               "  const t = (x || '').replace(/\\s+/g, ' ');\n"
               "  const m = String(s).match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);\n"
               "  if (/^(?:C-\\d{4,8}-\\d{2,4}|x)$/.test(txt)) {}\n"
               "  if (ratio < minContrast) minContrast = ratio;\n"
               "  if (ratio < req && lowContrastSamples.length < 6) {\n"
               "  const nl = 'a\\nb';\n"
               "})()`;\n")
        fixed, info = FR["fix_probe_template_escapes"](tpl)
        self.assertEqual(info["escapes_fixed"], 12)   # 1 + 9 + 2 处非法转义
        self.assertTrue(info["g400_exempt_patch"])
        self.assertTrue(info.get("order_id_g_series"))
        self.assertIn("replace(/\\\\s+/g", fixed)
        self.assertIn("'a\\nb'", fixed)                         # 合法转义 \n 不动
        again, info2 = FR["fix_probe_template_escapes"](fixed)
        self.assertEqual((info2["escapes_fixed"], again.count("isG400")), (0, fixed.count("isG400")))

    def test_absent_template(self):
        out, info = FR["fix_probe_template_escapes"]("no audit here")
        self.assertEqual((out, info.get("error")), ("no audit here", "auditExpr not found"))


class CheckSpecCli(unittest.TestCase):
    def setUp(self):
        _redirect()

    def test_exit_codes(self):
        good = os.path.join(TMP, "good.md")
        open(good, "w", encoding="utf-8").write(_spec([{"id": "A1", "template": "count_at_least", "params": {"selector": ".t"}}]))
        bad = os.path.join(TMP, "bad.md")
        open(bad, "w", encoding="utf-8").write("# 无断言")
        self.assertEqual(FR["cmd_check_spec"](good), 0)
        self.assertEqual(FR["cmd_check_spec"](bad), 2)
        self.assertEqual(FR["cmd_check_spec"](os.path.join(TMP, "missing.md")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
