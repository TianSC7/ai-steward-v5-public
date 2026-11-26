# -*- coding: utf-8 -*-
"""C-20261120-008 R1.2 · 状态推导 R1–R9 边界单测（W-105）

覆盖：
  r1.exact    16 key 全部精确命中（head_exact_match=yes）
  r2.alias    void/failed → blocked（exact=no）
  r2.unknown  未识别词 → blocked + 「未知态」alert（R2 不猜）
  r2.ctest    C-TEST 非字典 key → blocked + 「非字典key」alert，state 仍 PASS
  r3.node     T00 完成→planning；T01 进行→auditing；W 进行→producing；E 完成→qc；A 完成→delivering
  r4.release  审计过未放行→awaiting-release
  r4.closure  质检过未收口→awaiting-closure
  r5.terminal ☑接受→accepted；☒退回→returned（终态优先于回呈位）
  r6.blocked  阻塞记录无解除→blocked；已解除→不判 blocked
  r7.redline  红线/搁置→suspended
  r8.comp     返修单已部分交付→compensating
  r9.missing  缺 §1.2→created；缺 §2.0/§2.1→queued；缺 §11 且 delivering→alert
  state.all   真实 12 单 rebuild 后 state --all --compare-card diff=0，逐单一行
  check.order P2：改 B 卡后 check --order A 仍 rc=0、check --all rc=3

全部写 state/tmp 沙箱；不写活体 state/mes、不碰 8900/8901、不改订单卡。
用法：python -X utf8 test_mes_state_rules.py
"""
import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mes_core  # noqa: E402

ROOT = os.path.dirname(HERE)
TMP_ROOT = os.path.join(ROOT, "state", "tmp")
os.makedirs(TMP_ROOT, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="r12_state_", dir=TMP_ROOT)
ORDERS = os.path.join(TMP, "orders")
os.makedirs(ORDERS, exist_ok=True)
RESULTS = []


def ok(name, detail=""):
    RESULTS.append((True, name, detail))
    print("[PASS] %-14s %s" % (name, detail))


def bad(name, detail=""):
    RESULTS.append((False, name, detail))
    print("[FAIL] %-14s %s" % (name, detail))


def call(fn, *a, **k):
    buf = io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(buf):
        try:
            rc = fn(*a, **k)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
    return rc if isinstance(rc, int) else 0, buf.getvalue()


def write_card(code, head="", sec3="", sec4="", sec11=False,
               sec12=True, sec20=True, sec21=True, root=None):
    d = os.path.join(root or ORDERS, code)
    os.makedirs(os.path.join(d, "signatures"), exist_ok=True)
    rows = ["# 订单｜%s 合成卡" % code, "", "| 字段 | 内容 |", "|---|---|",
            "| 当前状态 | %s |" % head,
            "| 下单时间 | 2026-11-20 12:00:00 |", "| 优先级 | P2 |",
            "| 进度 | 阶段 0/1 · 子需求 0/1 |", "", "## 1 订单内容", ""]
    if sec12:
        rows += ["### 1.2 合同", "", "合成合同。", ""]
    rows += ["## 2 订单合同", ""]
    if sec20:
        rows += ["### 2.0 节点时限表", "", "| 节点 | 责任席 | 执行员工 | 计划开始 | 计划完成 | 时限 | 前置 | 状态 |",
                 "|---|---|---|---|---|---|---|---|", "| R0.1 策划 | B-001 | — | 12:00 | 12:30 | 30 min | — | 待接 |", ""]
    if sec21:
        rows += ["### 2.1 编成与流转签字表", "", "| 序 | 流程节点 | 流到谁 | 员工工号 | 会话号 | 进入时间 | 离开时间 | 结论 | 签字 |",
                 "|---|---|---|---|---|---|---|---|---|", "| 1 | R0 策划 | B-001 | — | — | — | — | 待接 | — |", ""]
    rows += ["## 3 追溯时间轴", ""]
    if sec3:
        rows += ["| 时间 | 节点与状态 |", "|---|---|", sec3, ""]
    rows += ["## 4 三方结论", "", sec4, "", "## 5 变更记录", "", "| 时间 | 变更 | 谁批 | D号 |",
             "|---|---|---|---|", "| — | — | — | — |", ""]
    if sec11:
        rows += ["## 11 提货清单", "", "| # | 项 | 状态 |", "|---|---|---|", "| 1 | — | ✅ |", ""]
    p = os.path.join(d, "order.md")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rows))
    return p


def derive(path, code):
    card = mes_core.parse_card(path, code, ORDERS)
    return card["order"]["status"], card["exact"], card["status_src"], card


# 1 R1 16 key 精确
want = list(mes_core.STATES16)
got = [mes_core.map_state("`%s` 合成" % k)[:2] for k in want]
if got == [(k, True) for k in want]:
    ok("r1.exact", "16 key 全精确 (%d)" % len(want))
else:
    bad("r1.exact", str([(k, g) for k, g in zip(want, got) if g != (k, True)]))

# 2 R2 别名
m1 = mes_core.map_state("`void` 作废")
m2 = mes_core.map_state("`failed` 全签但验收不过")
if m1[:2] == ("blocked", False) and m2[:2] == ("blocked", False):
    ok("r2.alias", "void/failed → blocked exact=no")
else:
    bad("r2.alias", "%s / %s" % (m1, m2))

# 3 R2 未知 + alert
p = write_card("C-SYN-UNKNOWN", head="`flying` 未知词")
st, exact, src, card = derive(p, "C-SYN-UNKNOWN")
kinds = [a[1] for a in card["alerts"]]
if (st, exact, src) == ("blocked", False, "unknown") and "未知态" in kinds:
    ok("r2.unknown", "blocked + 未知态 alert（R2 不猜）")
else:
    bad("r2.unknown", "st=%s exact=%s src=%s alerts=%s" % (st, exact, src, card["alerts"]))

# 4 C-TEST 非字典 key + alert
p = write_card("C-TEST-20261120-99", head="`void` 作废")
st, exact, src, card = derive(p, "C-TEST-20261120-99")
kinds = [a[1] for a in card["alerts"]]
if st == "blocked" and "非字典key" in kinds:
    ok("r2.ctest", "C-TEST void → blocked + 非字典key alert")
else:
    bad("r2.ctest", "st=%s exact=%s alerts=%s" % (st, exact, card["alerts"]))

# 5 R3 节点推进
r3_cases = (
    ("| 11-20 12:00 | T00 完成 |", "planning"),
    ("| 11-20 12:00 | T01 进行中 |", "auditing"),
    ("| 11-20 12:00 | W-103 生产中 |", "producing"),
    ("| 11-20 12:00 | E-101 进行中 |", "qc"),
    ("| 11-20 12:00 | A-001 完成 |", "delivering"),
)
r3_bad = []
for i, (row, exp) in enumerate(r3_cases):
    p = write_card("C-SYN-R3-%d" % i, head="", sec3=row)
    st, exact, src, _ = derive(p, "C-SYN-R3-%d" % i)
    if not (st == exp and src == "derived"):
        r3_bad.append((row, st, src))
if not r3_bad:
    ok("r3.node", "T00/T01/W/E/A 5/5")
else:
    bad("r3.node", str(r3_bad))

# 6 R4 回呈位
p = write_card("C-SYN-REL", head="", sec3="| 11-20 12:00 | 审计 T01 通过（C-101） |")
st, _, _, _ = derive(p, "C-SYN-REL")
p2 = write_card("C-SYN-CLO", head="", sec3="| 11-20 12:00 | 质检 E-101 通过 |")
st2, _, _, _ = derive(p2, "C-SYN-CLO")
if (st, st2) == ("awaiting-release", "awaiting-closure"):
    ok("r4.hold", "审计过未放行→awaiting-release；质检过未收口→awaiting-closure")
else:
    bad("r4.hold", "%s / %s" % (st, st2))

# 7 R5 终态（且优先于回呈位）
p = write_card("C-SYN-ACC", head="", sec4="发起人验收：☐接受 ☐有条件 ☑接受")
st, _, _, _ = derive(p, "C-SYN-ACC")
p2 = write_card("C-SYN-RET", head="", sec3="| 11-20 12:00 | 质检 E-101 通过 |",
                sec4="发起人验收：☐接受 ☐有条件 ☒退回")
st2, _, _, _ = derive(p2, "C-SYN-RET")
if (st, st2) == ("accepted", "returned"):
    ok("r5.terminal", "☑接受→accepted；☒退回→returned（压过 awaiting-closure）")
else:
    bad("r5.terminal", "%s / %s" % (st, st2))

# 8 R6 阻塞
p = write_card("C-SYN-BLK", head="", sec3="| 11-20 12:00 | 阻塞：等待第三方回执 |")
st, _, _, _ = derive(p, "C-SYN-BLK")
p2 = write_card("C-SYN-BLK2", head="",
                sec3="| 11-20 12:00 | 阻塞：等待回执 |\n| 11-20 13:00 | 已解除阻塞 |")
st2, _, _, _ = derive(p2, "C-SYN-BLK2")
if st == "blocked" and st2 != "blocked":
    ok("r6.blocked", "无解除→blocked；已解除→回前态(%s)" % st2)
else:
    bad("r6.blocked", "%s / %s" % (st, st2))

# 9 R7 搁置（含 D 号恢复）
p = write_card("C-SYN-SUS", head="", sec3="| 11-20 12:00 | 红线：暂停生产待厂长 |")
st, _, _, _ = derive(p, "C-SYN-SUS")
p2 = write_card("C-SYN-SUS2", head="",
                sec3="| 11-20 12:00 | 红线：暂停生产待厂长 |\n"
                     "| 11-20 13:00 | 恢复生产（D-20261121-001） |")
st2, _, _, _ = derive(p2, "C-SYN-SUS2")
if st == "suspended" and st2 != "suspended":
    ok("r7.redline", "红线→suspended；D 号恢复→解除(%s)" % st2)
else:
    bad("r7.redline", "%s / %s" % (st, st2))

# 10 R8 补偿
p = write_card("C-SYN-COMP", head="", sec3="| 11-20 12:00 | 返修单已部分交付 1/3 |")
st, _, _, _ = derive(p, "C-SYN-COMP")
if st == "compensating":
    ok("r8.comp", "返修单已部分交付→compensating")
else:
    bad("r8.comp", st)

# 11 R9 缺段
p = write_card("C-SYN-M12", head="", sec12=False)
st, _, _, _ = derive(p, "C-SYN-M12")
p2 = write_card("C-SYN-M20", head="", sec20=False)
st2, _, _, _ = derive(p2, "C-SYN-M20")
p3 = write_card("C-SYN-M11", head="`delivering` 交付中", sec11=False)
st3, _, _, c3 = derive(p3, "C-SYN-M11")
if (st, st2, st3) == ("created", "queued", "delivering") and "缺段" in [a[1] for a in c3["alerts"]]:
    ok("r9.missing", "缺 1.2→created；缺 2.0→queued；缺 11+delivering→alert")
else:
    bad("r9.missing", "%s / %s / %s / %s" % (st, st2, st3, c3["alerts"]))

# 12 全量 state --all --compare-card（基线单必须齐全；额外在册单只打印不判红）
EXPECTED_CARDS = {
    "C-20261120-001", "C-20261120-002", "C-20261120-003", "C-20261120-004",
    "C-20261120-005", "C-20261120-006", "C-20261120-007", "C-20261120-008",
    "C-20261120-009", "C-20261120-010",
    "C-TEST-20261120-02", "C-TEST-20261120-03",
}
s = os.path.join(TMP, "state")
mes_core.rebuild(s, mes_core.DEFAULT_ORDERS, quiet=True)
rc, out = call(mes_core.state_rows, s, mes_core.DEFAULT_ORDERS, True)
lines = out.splitlines()
tail = [l for l in lines if l.startswith("state ")]
data = [l for l in lines[1:] if l and not l.startswith("state ")]
codes = {l.split(" | ")[0] for l in data}
con = sqlite3.connect(mes_core.db_path(s))
nkey = con.execute("SELECT COUNT(*) FROM alerts WHERE kind='非字典key'").fetchone()[0]
con.close()
missing = EXPECTED_CARDS - codes
extra = codes - EXPECTED_CARDS
if rc == 0 and tail and "diff=0" in tail[0] and len(data) >= len(EXPECTED_CARDS) and not missing and nkey == 2:
    ok("state.all", "%s（逐单 %d 行；基线 %d 单齐全；额外 %s；非字典key alert=%d）"
       % (tail[0], len(data), len(EXPECTED_CARDS), sorted(extra) or "无", nkey))
else:
    bad("state.all", "rc=%s %s rows=%d missing=%s extra=%s nkey=%s %s"
        % (rc, tail, len(data), sorted(missing), sorted(extra), nkey, out.strip()[:200]))

# 13 P2 check --order 单过滤：改 B 后 check --order A=0、check --all=3
P2 = os.path.join(TMP, "p2")
P2_ORDERS = os.path.join(P2, "orders")
P2_STATE = os.path.join(P2, "state")
os.makedirs(P2_ORDERS, exist_ok=True)
write_card("C-P2-A", head="`producing` 生产中", root=P2_ORDERS)
write_card("C-P2-B", head="`producing` 生产中", root=P2_ORDERS)
mes_core.rebuild(P2_STATE, P2_ORDERS, quiet=True)
rc_a0, _out_a0 = call(mes_core.check, P2_STATE, P2_ORDERS, "C-P2-A")
# 改 B 卡字节（改状态词即可触发 check --all 差异），A 卡与库均未动
write_card("C-P2-B", head="`delivered` 可提货", root=P2_ORDERS)
rc_a, out_a = call(mes_core.check, P2_STATE, P2_ORDERS, "C-P2-A")
rc_all, out_all = call(mes_core.check, P2_STATE, P2_ORDERS, None)
if rc_a0 == 0 and rc_a == 0 and rc_all == 3:
    ok("check.order", "--order A rc=0（B 改后仍 0）；--all rc=%d" % rc_all)
else:
    bad("check.order", "A0 rc=%s / A rc=%s / all rc=%s | %s | %s"
        % (rc_a0, rc_a, rc_all, out_a.strip()[:120], out_all.strip()[:120]))

passed = sum(1 for r in RESULTS if r[0])
print("\n%d passed / %d total" % (passed, len(RESULTS)))
print("tmp=%s" % TMP)
sys.exit(0 if passed == len(RESULTS) else 2)
