# -*- coding: utf-8 -*-
"""订单适配层 + 首屏三问回归护栏（C-20261120-002 · T05）

覆盖：
  1  16 个状态样例卡（fixtures/orders_state_*）头部反引号推导
  2  §3 时间轴兜底映射（含 R-2：审计驳回→planning、质检不过→producing、退回只用于交付后）
  3  冲突取头部 + ⚠卡内不一致标记（R9）
  4  未识别状态不静默塞 queued
  5  10 段阶段折算 + blocked/suspended/compensating 保持上一已知阶段
  6  子需求 n/m 取卡头（R-1）；§9 另行计任务 m/n
  7  坏输入降级（空文件/缺 §3/缺 §9/短表）不抛异常
  8  mtime+size 缓存：文件变了才重解析
  9  render 冒烟：16 个中文名全在、/data.json 新增 orders/states、旧 16 key 零回归、
     C-004 legacy 12/14/18；C-009 12 档上限、SKIN=18（C-005 T04：§2.1 流程表/执行员工/会话/签字）、状态色对比度 ≥4.5（F9 的 token 级静态核）
  10 collect() 单次中位数 < 300ms（F4）
  11 parse_group 占位解析（C 块备用）

用法：python -X utf8 scripts/tests/test_mes_orders.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from fingerprint import announce, arm  # noqa: E402

import mes_board  # noqa: E402
import mes_orders as MO  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
RESULTS: list[tuple[str, str, str]] = []
OLD_KEYS = ("skin", "series", "deltas", "ts", "kpis", "cols", "seats", "team", "autos",
            "autosoff", "autosoffn", "runs", "sf", "sffoot", "dc", "ap")


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def card_text(node: str, state: str = "", sub: str = "0/1", tasks: str = "") -> str:
    """最小订单卡：头部 state 留空时走 §3 兜底。"""
    cur = ("`%s` 样例" % state) if state else "见 §3"
    return (
        "# C-20261120-999 ｜ 兜底样例卡\n\n"
        "| 订单编号 | C-20261120-999（唯一码·终身不变） |\n"
        "| 当前状态 | %s · 在谁手上：D 生产经理 D-101（sess_abcdef12…） · 停留 — |\n"
        "| 进度 | 阶段 3/10 · 子需求 %s |\n\n"
        "## 3 追溯时间轴（只追加）\n"
        "| 时间 | 节点 | 谁干 | 依据 |\n|---|---|---|---|\n"
        "| 11-20 00:00:00 | %s | 测试席 | 测试 |\n\n"
        "## 9 待办与流转（实时态）\n"
        "| 待办 | 责任席 | 状态 | 进度 | 证据 |\n|---|---|---|---|---|\n%s"
        % (cur, sub, node, tasks or "| T01 样例 | D 生产 | 未开工 | 0%% | — |\n")
    )


def parse_text(tmp: str, text: str, tag: str = "c"):
    p = os.path.join(tmp, "order_%s.md" % tag)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return MO._enrich(MO.parse_order(p))


def contrast(fg: str, bg: str = "#ffffff") -> float:
    def lum(h):
        h = h.lstrip("#")
        rgb = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
        f = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _font_map(doc: str) -> dict[str, set[str]]:
    """解析 <style> 块，返回 {选择器: {font-size 值}}；@media 内规则键加 " >> " 前缀。"""
    out = {}
    for style in re.findall(r"<style[^>]*>(.*?)</style>", doc, re.S):
        css = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        pos = 0
        while True:
            ob = css.find("{", pos)
            if ob < 0:
                break
            sel = css[pos:ob].strip()
            depth, j = 1, ob + 1
            while j < len(css) and depth:
                depth += {"{": 1, "}": -1}.get(css[j], 0)
                j += 1
            body = css[ob + 1:j - 1]
            if sel.startswith("@media"):
                p = 0
                while True:
                    o = body.find("{", p)
                    if o < 0:
                        break
                    s = body[p:o].strip()
                    d, k = 1, o + 1
                    while k < len(body) and d:
                        d += {"{": 1, "}": -1}.get(body[k], 0)
                        k += 1
                    for m in re.finditer(r"font-size\s*:\s*([0-9.]+)px", body[o + 1:k - 1]):
                        out.setdefault(sel + " >> " + s, set()).add(m.group(1))
                    p = k
            else:
                for m in re.finditer(r"font-size\s*:\s*([0-9.]+)px", body):
                    out.setdefault(sel, set()).add(m.group(1))
            pos = j
    return out


# SKIN18 现行 51 个字号选择器 → 档位；C-009 回归闸（§1.2 无字号项，不是合同验收）。
# 表外新选择器只允许 12/14/18；映射内选择器改档即 FAIL。
MICRO = {
    "#alertbar": "13",
    ".ab-ico": "15",
    ".al": "11",
    ".ans .al": "10",
    ".ans .av": "12",
    ".art": "10",
    ".brand": "14",
    ".btn": "11",
    ".caprow": "11",
    ".costbig": "20",
    ".costbig .num": "20",
    ".dmeta": "11",
    ".dtitle": "16",
    ".et": "10",
    ".ev": "11",
    ".foot": "10",
    ".ft": "10.5",
    ".ft .mut": "9.5",
    ".holine": "11",
    ".lane": "11",
    ".lane .r": "10",
    ".lgd": "10",
    ".miss": "15",
    ".okbox": "12",
    ".ot": "11",
    ".pool": "10",
    ".q h2": "10",
    ".qbig": "24",
    ".qbig .num": "24",
    ".qsub": "11",
    ".seat .chan .src": "9",
    ".seat .meta": "10",
    ".seat .sid": "11",
    ".seat .zbar.chmiss": "9",
    ".seat .zbar.chmiss b": "9",
    ".sec": "12",
    ".sec .mut": "10",
    ".sm": "10",
    ".st": "10",
    ".sx": "10",
    ".ts,.cd": "11",
    ".uq": "11",
    ".wip": "9",
    ".x": "12",
    ".zbar": "11",
    ".zh h2": "12",
    ".zst": "10",
    ".zsub": "10",
    "@media(max-width:700px) >> #alertbar": "12",
    "@media(max-width:700px) >> .ot td::before": "10",
    "@media(max-width:980px) >> .qbig,.qbig .num": "20",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_mes_orders")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_mes_orders")

    # 1 16 个状态样例卡
    got = {}
    for key in [s["k"] for s in MO.STATES]:
        p = os.path.join(FIX, "orders_state_" + key, "order.md")
        got[key] = MO.derive_state(MO.parse_order(p))
    bad = [k for k, v in got.items() if v["state"] != k]
    check("fixtures-16-state", not bad, "mismatch=%s" % (bad or "-"))

    # 2 §3 时间轴兜底映射（R-2 三行为重点）
    tl_cases = [
        ("下单", "created"), ("入排队池（P1·待排产）", "queued"), ("派单开工（queued→planning）", "planning"),
        ("合同呈批", "contract-review"), ("送审", "auditing"), ("审结回呈·通过", "awaiting-release"),
        ("厂长放行", "producing"), ("质检中", "qc"), ("质结回呈", "awaiting-closure"),
        ("发布闸 6 查", "delivering"), ("已交付", "delivered"), ("用户验收·接受", "accepted"),
        ("验收退回", "returned"), ("阻塞（依赖缺失）", "blocked"), ("红线搁置", "suspended"),
        ("补偿挂账", "compensating"), ("审计驳回", "planning"), ("质检不过·返工", "producing"),
    ]
    tmp = tempfile.mkdtemp(prefix="mes-orders-")
    try:
        tl_bad = []
        for i, (node, want) in enumerate(tl_cases):
            o = parse_text(tmp, card_text(node), "tl%d" % i)
            if o["state"] != want:
                tl_bad.append("%s→%s(期望 %s)" % (node, o["state"], want))
        check("timeline-fallback", not tl_bad, "; ".join(tl_bad) or "%d 例" % len(tl_cases))

        # 3 冲突取头部 + warn
        o = parse_text(tmp, card_text("审结回呈·通过", state="planning"), "conflict")
        check("conflict-header-wins", o["state"] == "planning" and o["warn"] is True
              and o["alt_state"] == "awaiting-release",
              "state=%s warn=%s alt=%s" % (o["state"], o["warn"], o["alt_state"]))

        # 4 未识别
        o = parse_text(tmp, card_text("某不存在的节点", state="mystery"), "unknown")
        check("unknown-state", o["state"] == "unknown" and o["state_cn"] == "未识别", o["state"])

        # 5 阶段折算 + 异常保持上一阶段
        stage_bad = []
        for s in MO.STATES:
            if s["stage"] is None:
                continue
            o = parse_text(tmp, card_text(s["cn"], state=s["k"]), "st_" + s["k"])
            if o["stage"] != s["stage"]:
                stage_bad.append("%s=%s(期望 %s)" % (s["k"], o["stage"], s["stage"]))
        check("stage-map", not stage_bad, "; ".join(stage_bad) or "13 档")
        blocked = ("# C-20261120-999 ｜ 阻塞阶段保持\n\n| 订单编号 | C-20261120-999 |\n"
                   "| 当前状态 | `blocked` 阻塞 · 在谁手上：— |\n| 进度 | 阶段 1/10 · 子需求 0/1 |\n\n"
                   "## 3 追溯时间轴（只追加）\n| 时间 | 节点 | 谁干 | 依据 |\n|---|---|---|---|\n"
                   "| 11-20 00:00:00 | 厂长放行 | 厂长 001 | D-1 |\n"
                   "| 11-20 00:10:00 | 阻塞（依赖缺失） | 策划 002 | 挂起 |\n")
        o = parse_text(tmp, blocked, "blocked_stage")
        check("blocked-keeps-stage", o["state"] == "blocked" and o["stage"] == 5, "stage=%s" % o["stage"])

        # 6 子需求 vs 任务口径（R-1）
        tasks = ("| T01 解析 | W1 | 完成 | 100% | e1 |\n"
                 "| T02 推导 | W1 | 完成 | 100% | e2 |\n"
                 "| T03 泳道 | W2 | 进行中 | 50% | e3 |\n")
        o = parse_text(tmp, card_text("派单开工", state="planning", sub="2/4", tasks=tasks), "subs")
        check("sub-progress-header", (o["sub_n"], o["sub_m"], o["sub_pct"]) == (2, 4, 50)
              and (o["task_n"], o["task_m"]) == (2, 3),
              "sub=%d/%d pct=%d task=%d/%d" % (o["sub_n"], o["sub_m"], o["sub_pct"], o["task_n"], o["task_m"]))

        # 7 坏输入降级
        bads = {
            "empty": "",
            "no-sec3": card_text("", state="planning").replace("## 3 追溯时间轴（只追加）", "## 3x"),
            "no-sec9": card_text("", state="planning").split("## 9")[0],
            "short-table": "# C-20261120-999 ｜ 短表\n\n| 当前状态 | `qc` |\n\n## 9 待办与流转\n| 待办 | 责任席 |\n|---|---|\n| T01 | W1 |\n",
        }
        bad_res = []
        for tag, txt in bads.items():
            try:
                o = parse_text(tmp, txt, "bad_" + tag)
                ok = isinstance(o.get("state"), str)
            except Exception as exc:  # noqa: BLE001 - 这里就是要证明不抛
                ok = False
                o = {"state": "EXC:%s" % exc}
            if not ok:
                bad_res.append("%s=%s" % (tag, o.get("state")))
        check("bad-input-degrade", not bad_res, "; ".join(bad_res) or "%d 例" % len(bads))

        # 8 缓存：未变不重解析；变了要重解析
        od = os.path.join(tmp, "orders")
        os.makedirs(os.path.join(od, "C-X"))
        p = os.path.join(od, "C-X", "order.md")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(card_text("派单开工", state="planning"))
        a1 = MO.load_all(od)["orders"][0]
        a2 = MO.load_all(od)["orders"][0]
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(card_text("派单开工", state="planning", sub="1/4"))
        time.sleep(0.01)
        os.utime(p, None)
        a3 = MO.load_all(od)["orders"][0]
        check("mtime-cache", a1 is a2 and a3["sub_n"] == 1,
              "cached=%s resub=%d" % (a1 is a2, a3["sub_n"]))

        # 11 parse_group 占位
        gp = os.path.join(tmp, "group.md")
        with open(gp, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 项目群\n\n| 时间 | 席位 | 内容 | 指向 |\n|---|---|---|---|\n"
                    "| 11-20 00:05 | A-001 | 建群 | order.md |\n")
        check("parse-group-stub", len(MO.parse_group(gp)) == 1, str(len(MO.parse_group(gp))))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 9 render 冒烟 + F9/F10
    d = mes_board.collect()
    doc = mes_board.render()
    missing_cn = [s["cn"] for s in MO.STATES if s["cn"] not in doc]
    check("render-16-cn", not missing_cn, "missing=%s" % (missing_cn or "-"))
    check("data-new-keys", isinstance(d.get("orders"), list) and isinstance(d.get("states"), list)
          and len(d["states"]) >= 16 and d["skin"] == "18", "orders=%d states=%d" % (len(d["orders"]), len(d["states"])))
    missing_old = [k for k in OLD_KEYS if k not in d]
    check("zero-regression-keys", not missing_old, "missing=%s" % (missing_old or "-"))
    # C-004 基线 = 现行 legacy（_legacy_render()），不是 HEAD 30f71cf 的逐字节模板；
    # 现行 legacy 比 HEAD 多 3 条字号声明（增量归属未逐行核），均仍在 12/14/18 内。
    legacy_doc = mes_board._legacy_render()
    lm = _font_map(legacy_doc)
    c004_bad = []
    for sel in ("table", "tr.trow td", ".titem", ".sub"):
        if lm.get(sel) != {"12"}:
            c004_bad.append("%s=%s" % (sel, sorted(lm.get(sel, ()))))
    for sel, want in (("h1", "18"), (".panel h2", "14"), (".dzone h3", "14")):
        if lm.get(sel) != {want}:
            c004_bad.append("%s=%s(want %s)" % (sel, sorted(lm.get(sel, ())), want))
    legacy_sizes = set().union(*lm.values()) if lm else set()
    if legacy_sizes != {"12", "14", "18"}:
        c004_bad.append("all=%s" % sorted(legacy_sizes, key=float))
    check("font-sizes-c004-legacy", not c004_bad, "bad=%s" % (c004_bad or "-"))

    # C-009 §1.2 八条验收无字号项；此块是新增回归闸（12 档上限），不是 C-009 合同验收。
    # 正文仅抽样 .ans .av/.okbox 两槽，不主张全页正文均 12。
    cm = _font_map(doc)
    c009_bad = []
    for sel in (".ans .av", ".okbox"):
        if cm.get(sel) != {"12"}:
            c009_bad.append("body_sampling:%s=%s" % (sel, sorted(cm.get(sel, ()))))
    allowed = {"9", "9.5", "10", "10.5", "11", "12", "13", "14", "15", "16", "20", "24"}
    used = set().union(*cm.values()) if cm else set()
    if not used <= allowed:
        c009_bad.append("scale:extra=%s" % sorted(used - allowed, key=float))
    # 已出现的映射项逐项比对；整项删除（删档）不误伤，新增未知选择器另判。
    map_bad = []
    for sel, tier in MICRO.items():
        if sel in cm and cm.get(sel) != {tier}:
            map_bad.append("%s=%s(want %s)" % (sel, sorted(cm.get(sel, ())), tier))
    if map_bad:
        c009_bad.append("map=%s" % map_bad)
    unknown_bad = [sel for sel in sorted(set(cm) - set(MICRO))
                   if cm.get(sel) not in ({"12"}, {"14"}, {"18"})]
    if unknown_bad:
        c009_bad.append("unknown=%s" % [(sel, sorted(cm.get(sel, ()))) for sel in unknown_bad])
    check("font-sizes-c009-sampling-scale-map", not c009_bad,
          "bad=%s used=%s" % (c009_bad or "-", sorted(used, key=float)))

    # 两域共用：<style> 外不得出现 font-size，防 inline 绕过选择器解析。
    outside_bad = {}
    for tag, text in (("c004", legacy_doc), ("c009", doc)):
        rest = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S)
        hits = re.findall(r"font-size\s*:\s*([0-9.]+)px", rest)
        if hits:
            outside_bad[tag] = hits
    check("font-size-no-inline", not outside_bad, "hits=%s" % (outside_bad or "-"))
    check("skin-18-in-doc", 'data-skin="18"' in doc, "F7b")
    check("no-raw-arrow", "→" not in doc, "count=%d" % doc.count("→"))
    colors = {k: MO.STATE_MAP[k]["color"] for k in ("created", "queued", "planning", "contract-review",
                                                    "awaiting-release", "producing",
                                                    "qc", "delivered", "returned", "suspended", "compensating")}
    low = ["%s %s=%.2f" % (k, c, contrast(c)) for k, c in colors.items() if contrast(c) < 4.5]
    for hexv in ("#2f6b45", "#b23b40", "#8c5a00"):
        if contrast(hexv) < 4.5:
            low.append("%s=%.2f" % (hexv, contrast(hexv)))
    check("contrast-tokens-F9", not low, "; ".join(low) or "min=%.2f" % min(contrast(c) for c in colors.values()))
    rows = MO.dict_rows()
    check("dict-15-rows", len(rows) == 15 and rows[11]["keys"] == ["accepted", "returned"],
          "rows=%d merged=%s" % (len(rows), rows[11]["keys"]))

    # 10 性能（F4）
    times = []
    for _ in range(5):
        t0 = time.time()
        mes_board.collect()
        times.append((time.time() - t0) * 1000)
    times.sort()
    med = times[len(times) // 2]
    check("perf-collect", med < 300.0, "median=%.1fms max=%.1fms" % (med, times[-1]))

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d2} for s, n, d2 in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d2 in RESULTS:
            print("[%s] %-24s %s" % (s, n, d2))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
