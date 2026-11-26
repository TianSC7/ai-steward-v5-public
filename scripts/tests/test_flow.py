# -*- coding: utf-8 -*-
"""C-20261120-005 · T04：§2.1 流程表 / 执行员工 / 会话号 / 签字 / 账实派生回归

覆盖：
  1  parse_flow 只取 `### 2.1` 段，行数 = 卡内真实数据行（独立计数，不写死 8）
  2  九列映射：序/节点/流到谁/员工工号/会话号/进入/离开/结论/签字
  3  签字列：有签字 True；「—/待签」False
  4  §9 八列新表：状态/进度/签字/证据不串列；旧五列表仍按旧口径解析
  5  crew 派生（fixture DB）：员工数只计「W 工号 + 真实会话」，token=model_usage 合计，
     截断会话号按前缀唯一命中还原，查无会话不计员工、进 sess_missing
  6  0 真实员工 → empty=True（渲染层「未编成」）
  7  会话库不可读 → db_ok=False（不当成 0 糊弄）
  8  无 §2.1 的卡（C-004）→ flow=[]、has_table=False

用法：python -X utf8 scripts/tests/test_flow.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from fingerprint import announce, arm  # noqa: E402

import mes_orders as MO  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def flow_rows(path: str) -> int:
    """独立数 §2.1 数据行（不调用被测解析器）。"""
    on, level, n = False, 0, 0
    for ln in open(path, encoding="utf-8", errors="replace").read().splitlines():
        m = re.match(r"^(#{1,6})\s", ln)
        if on and m and len(m.group(1)) <= level:
            break
        if not on and re.match(r"^#{2,4}\s*2\.1\b", ln):
            on, level = True, len(m.group(1)) if m else 3
            continue
        if not on or not ln.strip().startswith("|"):
            continue
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) >= 6 and re.match(r"^\d+$", c[0]):
            n += 1
    return n


def make_db(path: str, sessions: list[tuple[str, str]], usage: list[tuple[str, str, int]]) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("create table session(id text primary key, title text)")
        con.execute("create table model_usage(session_id text, status text, computed_total_tokens int)")
        con.executemany("insert into session values(?,?)", sessions)
        con.executemany("insert into model_usage values(?,?,?)", usage)
        con.commit()
    finally:
        con.close()


def card(flow_rows_text: str, tasks_text: str = "", sec21: bool = True) -> str:
    s = ("# C-20261120-999 ｜ 派生样例\n\n"
         "| 订单编号 | C-20261120-999 |\n"
         "| 当前状态 | `producing` 生产中 · 在谁手上：D 生产 D-101 |\n"
         "| 进度 | 阶段 5/10 · 子需求 1/5 |\n\n"
         "## 2 合同\n\n### 2.0 节点时限\n| 节点 | 责任席 |\n|---|---|\n| T00 | B-001 |\n\n")
    if sec21:
        s += ("### 2.1 编成与流转签字表\n"
              "| 序 | 流程节点 | 流到谁（岗位+工号） | 员工工号 | 会话号 | 进入时间 | 离开时间 | 结论 | 签字（工号+时间） |\n"
              "|---|---|---|---|---|---|---|---|---|\n" + flow_rows_text + "\n")
    s += "\n## 9 待办与流转（实时态）\n"
    if tasks_text:
        s += tasks_text
    else:
        s += ("| 待办 | 责任席 | 执行员工 | 会话号 | 状态 | 进度 | 签字 | 证据 |\n"
              "|---|---|---|---|---|---|---|---|\n| T01 样例 | W-201 | W-201 | 待填 | 未开工 | 0% | — | — |\n")
    return s


def write(tmp: str, text: str, tag: str = "c") -> str:
    p = os.path.join(tmp, "order_%s.md" % tag)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fp = announce("test_flow")
    if not fp.get("ok"):
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp.get("problems") or []))
        return 2
    arm(120, "test_flow")

    base = MO.orders_dir()
    p5 = MO.order_path("C-20261120-005", base)          # 2026-11-24：归档单兜底
    p4 = MO.order_path("C-20261120-004", base)
    if not os.path.isfile(p5):
        print("[FAIL] 找不到 C-005 order.md：%s" % p5)
        return 2

    # 1/2/3 真实卡 §2.1
    fl = MO.parse_flow(p5)
    n = flow_rows(p5)
    check("flow-rows-dynamic", len(fl) == n, "parsed=%d card=%d" % (len(fl), n))
    check("flow-cols", all(set(("seq", "node", "to", "emp", "emp_id", "sess", "sess_ids",
                                "enter", "leave", "concl", "sign", "sign_ok")) <= set(r)
                          for r in fl), "rows=%d" % len(fl))
    w_rows = [r for r in fl if r["emp_id"]]
    check("flow-emp-w", {r["emp_id"] for r in w_rows} <= {"W-103", "W-104"} and w_rows,
          "emps=%s" % sorted({r["emp_id"] for r in w_rows}))
    check("flow-sess-full", all(len(s) > 30 for r in fl for s in r["sess_ids"]),
          "ids=%d" % sum(len(r["sess_ids"]) for r in fl))
    # 2026-11-24：C-005 归档终版 8 行已全签（旧快照才有未签行）→ 未签分支改用解析器判据覆盖
    check("flow-sign-split", any(r["sign_ok"] for r in fl) and all(isinstance(r["sign_ok"], bool) for r in fl),
          "signed=%d unsigned=%d" % (sum(1 for r in fl if r["sign_ok"]),
                                     sum(1 for r in fl if not r["sign_ok"])))
    check("flow-sign-unsigned", MO._sign_ok("—") is False and MO._sign_ok("待签") is False
          and MO._sign_ok("W-103 11:20") is True)

    # 4 §9 八列 vs 五列
    d5 = MO.order_detail(p5)
    t0 = (d5.get("tasks") or [{}])[0]
    check("sec9-8col", t0.get("emp") is not None and "完成" in (t0.get("status") or "")
          and t0.get("sign", "").startswith("B-001") and t0.get("sess", "").startswith("sess_"),
          "status=%r sign=%r" % (t0.get("status"), t0.get("sign")))
    check("sec9-count", len(d5["tasks"]) == 9, "tasks=%d" % len(d5["tasks"]))

    tmp = tempfile.mkdtemp(prefix="mes-flow-")
    old_db = os.environ.get("MES_CLI_DB")
    try:
        five = ("| 待办 | 责任席 | 状态 | 进度 | 证据 |\n|---|---|---|---|---|\n"
                "| T01 旧表 | W1 | 完成 | 100% | e1 |\n"
                "| T02 旧表 | W1 | 进行中 | 50% | e2 |\n")
        o5 = MO.parse_order(write(tmp, card("", five), "five"))
        check("sec9-5col-compat", [(t["status"], t["progress"]) for t in o5["tasks"]]
              == [("完成", "100%"), ("进行中", "50%")]
              and all(t["emp"] == "" and t["sess"] == "" for t in o5["tasks"]),
              "tasks=%d" % len(o5["tasks"]))

        # 5 crew fixture（截断号前缀还原 / 查无 / cancelled 排除）
        full_a = "sess_aaaaaaaa-1111-4111-8111-111111111111"
        full_b = "sess_bbbbbbbb-2222-4222-8222-222222222222"
        full_c = "sess_cccccccc-3333-4333-8333-333333333333"
        db = os.path.join(tmp, "fixture.sqlite")
        make_db(db, [(full_a, "W-201 生产"), (full_b, "W-202 生产")],
                [(full_a, "completed", 100), (full_a, "completed", 50),
                 (full_a, "cancelled", 999), (full_b, "completed", 30)])
        os.environ["MES_CLI_DB"] = db
        MO._SESS_CACHE.update({"key": None, "ts": 0.0, "db_ok": False, "facts": {}})
        rows = ("| 1 | T01 | W-201 | W-201 | `%s` | 10:00 | 10:10 | 完成 | W-201/10:10:00 |\n"
                "| 2 | T02 | W-202 | W-202 | `sess_bbbbbbbb…` | 10:11 | 10:20 | 完成 | — |\n"
                "| 3 | T03 | W-203 | W-203 | `%s` | — | — | 待接 | — |\n"
                % (full_a, full_c))
        pfx = write(tmp, card(rows), "crew")
        d = MO.order_detail(pfx)
        c = d["crew"]
        check("crew-employees", c["n_emp"] == 2 and c["employees"] == ["W-201", "W-202"]
              and c["emp_pending"] == ["W-203"],
              "emp=%s pending=%s" % (c["employees"], c["emp_pending"]))
        check("crew-sessions", c["sess_real"] == 2 and c["sess_total"] == 3
              and c["sess_missing"] == [full_c],
              "real=%d total=%d missing=%s" % (c["sess_real"], c["sess_total"], c["sess_missing"]))
        check("crew-tokens", c["tokens"] == 180 and c["tokens_emp"] == 180,
              "tokens=%s emp=%s" % (c["tokens"], c["tokens_emp"]))
        check("crew-prefix-resolve", [r["sess_ok"] for r in d["flow"]] == [True, True, False],
              "flags=%s" % [r["sess_ok"] for r in d["flow"]])

        # 6 0 员工（W 行但会话待填）：库可读 → db_ok 仍须 True，只是派不出员工
        zero = ("| 1 | T01 | W-203 | W-203 | 待填 | — | — | 待接 | — |\n")
        dz = MO.order_detail(write(tmp, card(zero), "zero"))
        check("crew-zero-empty", dz["crew"]["empty"] is True and dz["crew"]["n_emp"] == 0
              and dz["crew"]["db_ok"] is True,
              "empty=%s n=%d db_ok=%s" % (dz["crew"]["empty"], dz["crew"]["n_emp"], dz["crew"]["db_ok"]))

        # 7 库不可读 → db_ok False，不得伪造 0 成「已验证」
        os.environ["MES_CLI_DB"] = os.path.join(tmp, "nope.sqlite")
        MO._SESS_CACHE.update({"key": None, "ts": 0.0, "db_ok": False, "facts": {}})
        dbad = MO.order_detail(write(tmp, card(rows), "nodbs"))
        check("crew-db-missing", dbad["crew"]["db_ok"] is False and dbad["crew"]["empty"] is True,
              "db_ok=%s empty=%s" % (dbad["crew"]["db_ok"], dbad["crew"]["empty"]))
    finally:
        if old_db is None:
            os.environ.pop("MES_CLI_DB", None)
        else:
            os.environ["MES_CLI_DB"] = old_db
        MO._SESS_CACHE.update({"key": None, "ts": 0.0, "db_ok": False, "facts": {}})
        shutil.rmtree(tmp, ignore_errors=True)

    # 8 无 §2.1 的卡
    d4 = MO.order_detail(p4)
    check("flow-missing-c004", d4["flow"] == [] and d4["flow_meta"]["has_table"] is False
          and d4["crew"]["empty"] is True,
          "flow=%d empty=%s" % (len(d4["flow"]), d4["crew"]["empty"]))

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
