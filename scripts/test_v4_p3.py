#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V4.0-P3 回归测试：ui_lint（解析 / 合规检查 / 死声明瘦身 / 静态等价证明）+ 内核像素比对与源码指标断言。

用法：python -X utf8 test_v4_p3.py      （stdlib only；若同目录有 v2.html 副本或设置 P3_REAL_V2，则额外跑真实看板）
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
import ui_lint  # noqa: E402
import factory_runner  # noqa: E402

UL = vars(ui_lint)
FR = vars(factory_runner)
RULES = json.load(open(os.path.join(HERE, "..", "config", "uispec_rules.json"), encoding="utf-8"))
REAL_V2 = os.environ.get("P3_REAL_V2") or os.path.join(HERE, "..", "dashboard", "v2.html")
TMP = tempfile.mkdtemp(prefix="v4_p3_test_")


def page(css: str, body: str = "<div></div>") -> str:
    return f"<!doctype html><html><head><style>{css}</style></head><body>{body}</body></html>"


def counts(css: str, body: str = "") -> dict:
    return UL["lint_counts"](UL["lint_html"](page(css, body), RULES))


class Parser(unittest.TestCase):
    def test_comments_strings_media(self):
        css = ('/* a{color:red} */ .x{content:"}{";color:#0f172a} @media (max-width:767px){.y{padding:8px !important}}'
               ' @font-face{font-family:"Barlow";src:url(data:x)} @keyframes k{from{opacity:0}to{opacity:1}}')
        nodes = UL["parse_css"](css)
        rules = list(UL["iter_rules"](nodes))
        self.assertEqual([r.selector_text for r in rules], [".x", ".y"])
        self.assertEqual(rules[0].decls[0].value, '"}{"')
        self.assertEqual(rules[1].context, ("@media (max-width:767px)",))
        self.assertTrue(rules[1].decls[0].important)
        self.assertEqual(sorted(a.name for a in UL["iter_at"](nodes)), ["font-face", "keyframes", "media"])

    def test_selector_list_and_data_uri(self):
        r = list(UL["iter_rules"](UL["parse_css"]('a, b:is(.p,.q) , c{background:url("data:image/svg+xml;a,b{c}")}')))[0]
        self.assertEqual(r.selectors, ["a", "b:is(.p,.q)", "c"])
        self.assertEqual(len(r.decls), 1)


class Lint(unittest.TestCase):
    def test_clean(self):
        c = counts(".a{font-size:14px;font-weight:600;border-radius:8px;border:0;color:#0f172a;background:#f8fafc;"
                   "transition:background 150ms;box-shadow:0 1px 2px rgba(15,23,42,.05)}")
        self.assertEqual(c["total"], 0)

    def test_each_rule(self):
        c = counts(".a{font-size:13px}.b{font-weight:700}.c{border-radius:6px}.d{border:1px solid #fca5a5}"
                   ".e{box-shadow:0 4px 12px rgba(0,0,0,.2)}.f{transition:color 200ms}.g{animation:k 1s linear infinite}"
                   ".h{color:#15803d}.i{background:linear-gradient(90deg,#3b82f6,#1d4ed8)}.j{color:#fff!important}"
                   "@media (max-width:960px){.k{color:#0f172a}}")
        for k in ("font_size", "font_weight", "radius", "border_width", "colored_border", "shadow", "duration",
                  "infinite_animation", "color", "color_gradient", "important", "breakpoint"):
            self.assertGreaterEqual(c.get(k, 0), 1, k)

    def test_whitelists_respected(self):
        c = counts("#z1 .alist li{border-bottom:1px solid #e2e8f0}"                       # 豁免选择器
                   ".z{background:linear-gradient(90deg,#f4f9ff 0%,#f2fbf7 100%)}"         # z1 三档渐变
                   ".w{color:#b45309;font-size:clamp(56px,18vw,88px)}"                     # amber 深字 + hero clamp
                   "@media (max-width:767px){.q{color:#0f172a}}@media (min-width:1600px){.q{color:#0f172a}}"
                   ".t{border:0;outline:0;color:rgba(0,0,0,0)}")
        self.assertEqual(c["total"], 0, c)

    def test_font_face_outside_stack(self):
        c = counts('@font-face{font-family:"Inter";src:url(x)}@font-face{font-family:"Barlow";src:url(y)}')
        self.assertEqual(c["font_face"], 1)

    def test_legacy_tokens(self):
        rep = UL["lint_html"](page(":root{--bg:#f8fafc;--g-50:#f8fafc}.a{color:var(--mut)}.b{color:var(--g-600)}"), RULES)
        self.assertEqual(rep["rules"]["legacy_token_def"]["count"], 1)
        self.assertEqual(rep["rules"]["legacy_token_use"]["count"], 1)


class Dead(unittest.TestCase):
    def dead_props(self, css):
        return sorted((x["rule"].selector_text, x["decl"].prop) for x in UL["dead_declarations"](UL["parse_html_css"](page(css)))["items"])

    def test_basic_override(self):
        self.assertEqual(self.dead_props(".a{color:#0f172a;padding:8px}.a{color:#475569}"), [(".a", "color")])

    def test_unconditional_later_kills_conditional_earlier(self):
        self.assertEqual(self.dead_props("@media (max-width:767px){.a{color:#0f172a}}.a{color:#475569}"),
                         [(".a", "color")])

    def test_conditional_later_does_not_kill_unconditional(self):
        self.assertEqual(self.dead_props(".a{color:#0f172a}@media (max-width:767px){.a{color:#475569}}"), [])

    def test_different_media_not_dead(self):
        self.assertEqual(self.dead_props("@media (max-width:767px){.a{color:#0f172a}}@media (min-width:1600px){.a{color:#475569}}"), [])

    def test_importance(self):
        self.assertEqual(self.dead_props(".a{color:#0f172a!important}.a{color:#475569}"), [])
        self.assertEqual(self.dead_props(".a{color:#0f172a}.a{color:#475569!important}"), [(".a", "color")])

    def test_selector_list_needs_all(self):
        self.assertEqual(self.dead_props(".a,.b{color:#0f172a}.a{color:#475569}"), [])
        self.assertEqual(self.dead_props(".a,.b{color:#0f172a}.b,.a{color:#475569}"), [(".a,.b", "color")])

    def test_shorthand_not_assumed(self):
        self.assertEqual(self.dead_props(".a{padding-top:8px}.a{padding:4px}"), [])

    def test_vendor_and_same_block_fallbacks_kept(self):
        self.assertEqual(self.dead_props(".a{display:-webkit-box}.a{display:-webkit-box}"), [])
        self.assertEqual(self.dead_props(".a{display:block;display:flex}"), [])   # 同块重复视为有意回退写法，保守不删
        self.assertEqual(self.dead_props(".a{width:100px}.a{width:-webkit-fill-available}"), [])

    def test_layer_skipped(self):
        self.assertEqual(self.dead_props("@layer x{.a{color:#0f172a}}.a{color:#475569}"), [])


class Slim(unittest.TestCase):
    CSS = (".a{color:#0f172a;padding:8px}/* note */.a{color:#475569}"
           "@media (max-width:767px){.b{margin:0}}.b{margin:4px}"
           ".c{color:#0f172a}.c{color:#475569}")

    def test_slim_and_prove(self):
        html = page(self.CSS, "<p>保持</p>")
        out, rep = UL["slim_html"](html)
        self.assertEqual((rep["removed_decls"], rep["removed_rules"], rep["removed_media"]), (3, 2, 1))
        self.assertIn("<p>保持</p>", out)
        self.assertIn("/* note */", out)
        eq = UL["verify_slim_equivalence"](html, out)
        self.assertTrue(eq["equivalent"], eq)
        out2, rep2 = UL["slim_html"](out)
        self.assertEqual((out2, rep2["removed_decls"]), (out, 0))           # 幂等

    def test_strip_comments(self):
        out, rep = UL["slim_html"](page(self.CSS), strip_comments=True)
        self.assertNotIn("note", out)
        self.assertGreater(rep["removed_comment_bytes"], 0)

    def test_equivalence_catches_bad_edit(self):
        before = page(".a{color:#0f172a}@media (max-width:767px){.a{color:#475569}}")
        after = page("@media (max-width:767px){.a{color:#475569}}")
        eq = UL["verify_slim_equivalence"](before, after)
        self.assertFalse(eq["equivalent"])
        self.assertEqual(eq["diff_samples"][0]["prop"], "color")

    def test_equivalence_catches_media_dependent_change(self):
        before = page("@media (max-width:767px){.a{color:#0f172a}}.a{color:#475569}@media (min-width:1600px){.a{color:#64748b}}")
        after = page(".a{color:#475569}")
        self.assertFalse(UL["verify_slim_equivalence"](before, after)["equivalent"])

    @unittest.skipUnless(os.path.exists(REAL_V2), "无真实 v2.html")
    def test_real_dashboard(self):
        html = open(REAL_V2, encoding="utf-8").read()
        out, rep = UL["slim_html"](html, strip_comments=True)
        self.assertTrue(UL["verify_slim_equivalence"](html, out)["equivalent"])
        strip = lambda x: re.sub(r"<style[^>]*>.*?</style>", "", x, flags=re.S)
        self.assertEqual(strip(html), strip(out))                          # 非 CSS 字节一个不动
        before, after = UL["lint_counts"](UL["lint_html"](html, RULES)), UL["lint_counts"](UL["lint_html"](out, RULES))
        self.assertTrue(all(after.get(k, 0) <= v for k, v in before.items() if k != "dead_decl"))


class Retoken(unittest.TestCase):
    ROOT = ":root{--g-900:#0f172a;--c-blue:#2563eb;--tx:#0f172a;--acc:#2563eb;--bg:#f4f7fb;--odd:#0f172a}"

    def test_equal_values_renamed(self):
        html = page(self.ROOT + ".a{color:var(--tx)}.b{border:1px solid var(--acc , red)}.c{background:var(--bg)}"
                    "@media (max-width:767px){:root{--odd:#475569}}.d{color:var(--odd)}",
                    "<i style=\"color:var(--tx)\"></i>")
        out, rep = UL["apply_retoken"](html, RULES)
        self.assertEqual(sorted((x["from"], x["to"]) for x in rep["plan"]), [("--acc", "--c-blue"), ("--tx", "--g-900")])
        self.assertNotIn("--tx", out)
        self.assertIn('style="color:var(--g-900)"', out)               # HTML/JS 里的引用一并改
        self.assertIn("var(--c-blue , red)", out)                        # 回退值写法保留原样
        self.assertIn("var(--bg)", out)                                  # 无等值 token → 不动
        self.assertIn("var(--odd)", out)                                 # 有条件重定义 → 不动
        why = {x["token"]: x["why"] for x in rep["skipped"]}
        self.assertIn("没有等值", why["--bg"])
        self.assertIn("非 :root", why["--odd"])
        self.assertTrue(UL["verify_retoken"](html, out, RULES)["equivalent"])

    def test_js_access_blocks_rename(self):
        html = page(self.ROOT + ".a{color:var(--tx)}", "<script>getComputedStyle(document.body).getPropertyValue('--tx')</script>")
        _, rep = UL["apply_retoken"](html, RULES)
        self.assertNotIn("--tx", [x["from"] for x in rep["plan"]])

    def test_verify_catches_tamper(self):
        html = page(self.ROOT + ".a{color:var(--tx)}")
        out, _ = UL["apply_retoken"](html, RULES)
        self.assertFalse(UL["verify_retoken"](html, out.replace("--g-900:#0f172a", "--g-900:#111111"), RULES)["equivalent"])

    def test_var_name_not_mistaken_for_color(self):
        self.assertEqual(counts(":root{--c-blue:#2563eb}.a{outline:2px solid var(--c-blue)}").get("color", 0), 0)   # var(--c-blue) 里的 blue 不是颜色名

    def test_comment_archive(self):
        arc = UL["archive_comments"](page("/* C-20261122-G12 F1 导航收敛 */#nav{height:32px}"))
        self.assertIn("C-20261122-G12", arc)
        self.assertIn("`#nav`", arc)

    @unittest.skipUnless(os.path.exists(REAL_V2), "无真实 v2.html")
    def test_real_dashboard_retoken(self):
        html = open(REAL_V2, encoding="utf-8").read()
        slim, _ = UL["slim_html"](html, strip_comments=True)
        out, rep = UL["apply_retoken"](slim, RULES)
        # 2026-11-23 监理：现网已由 P3-A 执行过 retoken（16 token / 183 处引用），
        # 二次执行 renamed_tokens=0 属预期；保留未瘦身时的原断言，已瘦身时断言幂等。
        self.assertTrue(UL["verify_retoken"](slim, out, RULES)["equivalent"])
        if rep["renamed_tokens"] >= 10:
            a, b = UL["lint_counts"](UL["lint_html"](html, RULES)), UL["lint_counts"](UL["lint_html"](out, RULES))
            self.assertLess(b["total"], a["total"])
            self.assertLess(b["legacy_token_use"], a["legacy_token_use"])
        else:
            self.assertEqual(rep["renamed_tokens"], 0)
            self.assertEqual(out, slim)


class SrcMetrics(unittest.TestCase):
    def test_keys(self):
        m = UL["src_metrics"](page(".a{color:#15803d}"), RULES)
        self.assertEqual(m["lint.color"], 1)
        self.assertIn("src.css_bytes", m)
        self.assertEqual(m["lint.shadow"], 0)


class VisualAssertions(unittest.TestCase):
    def parse(self, items):
        return FR["extract_assertions"]("```assertions\n" + json.dumps(items) + "\n```", {})

    def test_defaults_and_validation(self):
        a, e = self.parse([{"id": "V1", "metric": "visual.diff_pct", "op": "<=", "value": 0.2, "state": "zone:6"}])
        self.assertEqual(e, [])
        self.assertEqual((a[0]["threshold"], a[0]["full_page"]), (24, True))
        self.assertIn("#upd", a[0]["mask"])
        _, e = self.parse([{"id": "V1", "metric": "visual.diff_pct", "op": ">=", "value": "baseline"}])
        self.assertEqual(len(e), 2)

    def test_src_metric_validation(self):
        a, e = self.parse([{"id": "L1", "metric": "lint.total", "op": "<", "value": "baseline"},
                           {"id": "L2", "metric": "src.css_bytes", "op": "<=", "value": 60000}])
        self.assertEqual((e, a[0]["viewports"]), ([], [1440]))
        _, e = self.parse([{"id": "L3", "metric": "lint.total", "op": "<", "value": 1, "state": "zone:2"}])
        self.assertTrue(e)

    def test_plan_separates_visual_and_src(self):
        a, _ = self.parse([{"id": "V1", "metric": "visual.diff_pct", "op": "<=", "value": 0.2},
                           {"id": "M1", "metric": "count", "selector": ".x", "op": ">=", "value": 1},
                           {"id": "L1", "metric": "lint.total", "op": "<", "value": "baseline"}])
        plan = FR["build_assertion_plan"](a)
        g = plan["groups"][0]
        self.assertEqual((g["ids"], g["visual"], g["targets"]), (["M1"], ["V1"], ["sandbox", "baseline"]))
        self.assertNotIn("V1", plan["defs"])
        self.assertIn("V1", plan["visual_defs"])
        self.assertTrue(plan["need_src"])
        self.assertEqual(plan["visual_count"], 3)

    def test_evaluate_visual(self):
        a, _ = self.parse([{"id": "V1", "metric": "visual.diff_pct", "op": "<=", "value": 0.2, "viewports": [1440, 390]}])
        raw = {"targets": {}, "errors": {}, "visual": {"default": {
            "1440": {"V1": {"diff_pct": 0.0, "diff_px": 0, "total_px": 100}},
            "390": {"V1": {"diff_pct": 3.4, "diff_px": 34, "total_px": 1000, "bbox": [0, 0, 9, 9], "diff_png": "d.png"}}}}}
        r = FR["evaluate_assertions"](a, raw)[0]
        self.assertEqual([v["status"] for v in r["per_viewport"]], ["PASS", "FAIL"])
        self.assertEqual(r["per_viewport"][1]["visual"]["diff_png"], "d.png")
        raw["errors"]["baseline|default"] = "state prep failed"
        r = FR["evaluate_assertions"](a, raw)[0]
        self.assertEqual(r["status"], "UNVERIFIED")

    def test_evaluate_src(self):
        a, _ = self.parse([{"id": "L1", "metric": "lint.total", "op": "<", "value": "baseline"},
                           {"id": "L2", "metric": "src.css_bytes", "op": "<=", "value": "baseline*0.5"}])
        raw = {"targets": {}, "src": {"sandbox": {"lint.total": 150, "src.css_bytes": 70000},
                                      "baseline": {"lint.total": 203, "src.css_bytes": 132912}}}
        r = {x["id"]: x for x in FR["evaluate_assertions"](a, raw)}
        self.assertEqual((r["L1"]["status"], r["L2"]["status"]), ("PASS", "FAIL"))
        self.assertEqual(r["L2"]["per_viewport"][0]["baseline"], 132912)
        r = FR["evaluate_assertions"](a, {"src": {"error": "ui_lint 模块不可用"}})
        self.assertEqual({x["status"] for x in r}, {"UNVERIFIED"})

    def test_freeze_all(self):
        p = os.path.join(TMP, "v2.html")
        open(p, "w", encoding="utf-8").write("".join(f'<section class="zone" id="z{i}"></section>' for i in range(1, 14)))
        a = FR["freeze_all_assertions"](p, 0.2)
        states = [x["state"] for x in a]
        self.assertEqual(states[0], "default")
        self.assertEqual(states[-1], "degraded")
        self.assertEqual(len(a), 14)                    # default + z2..z13 + degraded
        self.assertFalse(a[-1]["required"])

    def test_rewrite_authorization(self):
        vis = [{"id": "V1", "metric": "visual.diff_pct", "required": True}]
        self.assertEqual(FR["rewrite_authorized"]("C-P3-B", {"rewrite_authorized_orders": []}, vis), "")
        self.assertEqual(FR["rewrite_authorized"]("C-P3-B-R12", {"rewrite_authorized_orders": ["C-P3-B"]}, vis), "ok")
        self.assertIn("缺必验", FR["rewrite_authorized"]("C-P3-B", {"rewrite_authorized_orders": ["C-P3-B"]}, []))

    def test_src_pair(self):
        a, b = os.path.join(TMP, "a.html"), os.path.join(TMP, "b.html")
        open(a, "w", encoding="utf-8").write(page(".x{color:#15803d}"))
        open(b, "w", encoding="utf-8").write(page(".x{color:#16a34a}"))
        FR["UISPEC_RULES_CANDIDATES"] = [os.path.join(HERE, "..", "config", "uispec_rules.json")]
        pr = FR["_src_metrics_pair"](a, b)
        self.assertEqual((pr["sandbox"]["lint.color"], pr["baseline"]["lint.color"]), (1, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
