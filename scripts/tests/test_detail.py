# -*- coding: utf-8 -*-
"""C-20261120-004 · T05：群聊切分 / 详情聚合回归（动态计数，遵守审计 R-2/R-4）

覆盖：
  1  parse_group_chat 只取 `## 群聊记录` 段；条数 = 文件真实数据行（不写死 11/5/16）
  2  roster 单独取出，不混入聊天流（R-4：chat 行 t 非时间格式才查 (seat,text)）
  3  order_detail 四区齐全：header8 / three_q / §9 全行 / chat 倒序最近 min(12,n)
  4  详情 chat 条数与 parse_group_chat 一致，且 >=10（用 C-002 证；不足按实际）
  5  未知 code → None；缺 group.md → chat 空列表不抛
  6  mtime+size 缓存：同文件重复调用返回同一对象
  7  §9 行数 = 卡内数据行数（独立计数，不借用 _parse_tasks）

用法：python -X utf8 scripts/tests/test_detail.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from fingerprint import announce, arm  # noqa: E402

import mes_orders as MO  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
TIME_RE = re.compile(r"^\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}")


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def group_chat_rows(path: str) -> int:
    """独立数 `## 群聊记录` 段 4 列数据行（不调用被测解析器）。"""
    on, n = False, 0
    for ln in open(path, encoding="utf-8", errors="replace").read().splitlines():
        if re.match(r"^##\s+", ln):
            on = "群聊记录" in ln
            continue
        if not on or not ln.strip().startswith("|"):
            continue
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) == 4 and c[0] not in ("时间",) and not set(c[0]) <= set("-: "):
            n += 1
    return n


def sec9_rows(text: str) -> int:
    """独立数 §9 五列待办数据行（不调用被测解析器，防止同错同过）。

    §9 段内还挂着「### 填写权分段」两列表，不能计入待办表——
    这里按列数（>=5）切，只看待办表本身。
    """
    on, n = False, 0
    for ln in text.splitlines():
        if re.match(r"^##\s+9\s+", ln):
            on = True
            continue
        if on and re.match(r"^##\s+", ln):
            break
        if not on or not ln.strip().startswith("|"):
            continue
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) >= 5 and c[0] not in ("待办",) and not set(c[0]) <= set("-: "):
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fp = announce("test_detail")
    if not fp.get("ok"):
        return 2
    arm(120, "test_detail")

    base = MO.orders_dir()
    p2 = MO.order_path("C-20261120-002", base)          # 2026-11-24：归档单兜底
    g2 = os.path.join(os.path.dirname(p2), "group.md")
    g4 = os.path.join(os.path.dirname(MO.order_path("C-20261120-004", base)), "group.md")
    if not (os.path.isfile(g2) and os.path.isfile(p2)):
        print("[FAIL] 找不到 C-002 的 order.md/group.md：%s" % base)
        return 2

    chat_all = MO.parse_group_chat(g2)
    roster = MO.parse_roster(g2)
    n_chat = group_chat_rows(g2)
    check("chat-count-dynamic", len(chat_all) == n_chat, "chat=%d rows=%d" % (len(chat_all), n_chat))
    check("chat-ge-10", len(chat_all) >= 10, "chat=%d" % len(chat_all))
    check("roster-separate", len(roster) >= 1 and len(chat_all) != len(roster), "roster=%d" % len(roster))
    bad_rows = [c for c in chat_all
                if not TIME_RE.match(c.get("t") or "")
                and not any((c.get("seat"), c.get("text")) == (r.get("seat"), r.get("role")) for r in roster)]
    check("roster-not-in-chat", not bad_rows,
          "suspect=%d" % len(bad_rows))

    det = MO.order_detail(p2)
    check("detail-exists", bool(det), "None" if not det else det.get("code", ""))
    if det:
        check("header8", len(det.get("header8") or {}) == 8
              and all((det["header8"].get(k) or "—") != "—" for k in
                      ("order", "created", "requester", "priority", "eta", "risk", "budget", "route")),
              "keys=%d" % len(det.get("header8") or {}))
        q = det.get("three_q") or {}
        check("three-q", q.get("state") in MO.STATE_MAP and int(q.get("stage") or 0) >= 1
              and int(q.get("stage_n") or 0) == 10, "state=%s stage=%s" % (q.get("state"), q.get("stage")))
        n9 = sec9_rows(open(p2, encoding="utf-8", errors="replace").read())
        check("tasks-all-rows", len(det.get("tasks") or []) == n9,
              "detail=%d card=%d" % (len(det.get("tasks") or []), n9))
        check("chat-window", len(det.get("chat") or []) == min(12, len(chat_all)),
              "detail=%d cap12" % len(det.get("chat") or []))
        check("chat-latest-first", bool(det.get("chat"))
              and det["chat"][0]["t"] == chat_all[-1]["t"], "first=%s" % (det["chat"][0]["t"] if det.get("chat") else "-"))
        check("detail-cache", MO.order_detail(p2) is det, "identity")
    check("unknown-none", MO.order_detail("C-20261120-NOPE") is None, "None")

    if os.path.isfile(g4):
        c4 = MO.parse_group_chat(g4)
        d4 = MO.order_detail(MO.order_path("C-20261120-004", base))
        check("c004-dynamic", bool(d4) and len(d4["chat"]) == min(12, len(c4)),
              "chat=%d" % (len(d4["chat"]) if d4 else -1))

    fx = os.path.join(HERE, "fixtures", "orders_state_accepted")
    dfx = MO.order_detail(os.path.join(fx, "order.md"))
    check("missing-group-empty", bool(dfx) and dfx.get("chat") == [], "chat=%d" % (len(dfx.get("chat") or []) if dfx else -1))
    check("missing-section-empty", MO.parse_group_chat(os.path.join(fx, "order.md")) == [], "no ## 群聊记录")

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-22s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
