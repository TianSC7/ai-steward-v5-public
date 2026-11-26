# -*- coding: utf-8 -*-
"""C-20261120-004 · T05：排产甘特解析 + 渲染回归

覆盖：
  1  fixture 解析：快照 10:00 / 12 小时列 / 4 班组行 / cells = hours×rows
  2  三段式列判定：▶=lock、其前=archived、后 4 列=rolling、再后=predict
  3  单元格 5 类：`—`不可用 / `（…）`预判 / `**…**`锁定 / 普通已排 / 插单记号；订单号+子需求号
  4  行头：加开组 extra、CAD 专席 cad、编成文本
  5  无表 / 坏表 / 空文件 / 文件不存在 → None（禁返回全空格子表）
  6  真实排班单（今日无甘特表）→ None
  7  渲染：图例 5 项、三段式 class、加开徽标、⚡ 插单、班中变更附录、空态卡（含查找路径、无表格）

用法：python -X utf8 scripts/tests/test_gantt.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from fingerprint import announce, arm  # noqa: E402

import mes_board  # noqa: E402
import mes_orders as MO  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
FIX = os.path.join(HERE, "fixtures", "SHIFT-fixture-gantt.md")


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fp = announce("test_gantt")
    if not fp.get("ok"):
        return 2
    arm(120, "test_gantt")

    g = MO.parse_shift_gantt(FIX)
    check("fixture-parsed", bool(g), "None" if not g else os.path.basename(g["file"]))
    if g:
        check("snapshot", g.get("snapshot") == "10:00", str(g.get("snapshot")))
        check("hours-12", len(g["hours"]) == 12, "hours=%d" % len(g["hours"]))
        check("rows-4", len(g["rows"]) == 4, "rows=%d" % len(g["rows"]))
        cells = sum(len(r["cells"]) for r in g["rows"])
        check("cells-hours-x-rows", cells == len(g["hours"]) * len(g["rows"]),
              "cells=%d expect=%d" % (cells, len(g["hours"]) * len(g["rows"])))
        kinds = [h["kind"] for h in g["hours"]]
        check("three-stage-cols", kinds[2] == "lock" and kinds[3] == "rolling"
              and kinds[6] == "rolling" and kinds[7] == "predict" and kinds[1] == "archived", str(kinds))
        check("row-extra", any(r["extra"] for r in g["rows"]), "extra=%d" % sum(1 for r in g["rows"] if r["extra"]))
        check("row-cad", any(r["cad"] for r in g["rows"]), "cad=%d" % sum(1 for r in g["rows"] if r["cad"]))
        all_cells = [c for r in g["rows"] for c in r["cells"]]
        n_locked = sum(1 for c in all_cells if c["kind"] == "locked")
        n_pred = sum(1 for c in all_cells if c["kind"] == "predicted")
        n_absent = sum(1 for c in all_cells if c["kind"] == "absent")
        n_insert = sum(1 for c in all_cells if c["insert"])
        check("cells-5-kinds", n_locked >= 1 and n_pred >= 1 and n_absent >= 1 and n_insert >= 1,
              "locked=%d pred=%d absent=%d insert=%d" % (n_locked, n_pred, n_absent, n_insert))
        first = g["rows"][0]["cells"][0]
        check("code-sub", first["code"] == "C-20261120-002" and first["sub"] == "①",
              "%s %s" % (first["code"], first["sub"]))
        ins = next(c for c in all_cells if c["insert"])
        check("insert-stripped", ins["code"] == "C-20261120-003" and "插单" not in ins["text"],
              "text=%s" % ins["text"])
        locked = next(c for c in all_cells if c["kind"] == "locked")
        check("locked-stripped", "**" not in locked["text"] and locked["code"], str(locked["text"]))
        check("changes-parsed", len(g.get("changes") or []) == 1, "changes=%d" % len(g.get("changes") or []))

        html = mes_board._gantt_html(g, FIX)
        legend_ok = all(x in html for x in ("▶=当前时刻", "锁定栏（1h）", "滚动栏（5h）",
                                           "预判栏（12h，不承诺交付）", "不在场"))
        check("render-legend-5", legend_ok, "5 项")
        check("render-classes", 'class="ghead lock"' in html and "td class=\"gcell locked" in html
              and "gcell predicted" in html and 'class="grow extra"' in html
              and 'class="grow cad"' in html and 'class="gbadge"' in html and 'class="ins"' in html,
              "三段式/加开/CAD/插单")
        check("render-changes", "班中变更" in html and "已渲染 1 条" in html, "附录已渲染")

    with tempfile.TemporaryDirectory(prefix="c004-gantt-") as td:
        empty = os.path.join(td, "empty.md")
        open(empty, "w", encoding="utf-8").write("")
        prose = os.path.join(td, "prose.md")
        open(prose, "w", encoding="utf-8").write("# 排班单\n\n| 席位 | 模型 | lane |\n|---|---|---|\n| D-101 | x | R4-2 |\n")
        header_only = os.path.join(td, "header.md")
        open(header_only, "w", encoding="utf-8").write("| 班组（编成 · 主单） | 08 | 09 |\n|---|---|---|\n")
        check("empty-none", MO.parse_shift_gantt(empty) is None, "empty")
        check("no-table-none", MO.parse_shift_gantt(prose) is None, "prose")
        check("header-only-none", MO.parse_shift_gantt(header_only) is None, "no data rows")
        check("missing-none", MO.parse_shift_gantt(os.path.join(td, "nope.md")) is None, "missing")

    real = MO.latest_shift_path()
    if real and os.path.isfile(real):
        rg = MO.parse_shift_gantt(real)
        check("real-shift-no-fabrication", rg is None, "file=%s parsed=%s" % (os.path.basename(real), bool(rg)))

    empty_html = mes_board._gantt_html(None, real or os.path.join(HERE, "fixtures"))
    check("empty-state", "未找到排班单甘特表" in empty_html and "查找路径" in empty_html
          and "<table" not in empty_html, "无表格/给路径")

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-24s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
