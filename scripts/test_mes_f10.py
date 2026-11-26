# -*- coding: utf-8 -*-
"""C-20261120-008 F10 · 工牌 + MES 写通道负例矩阵（W-105）

覆盖（全沙箱 registry/state/orders）：
  issue     发牌落 badges 段；重签保 issued_at + reissue_count；非法牌/部门/签发人 exit 3
  archive   收牌 archived；重复收牌幂等 0；收牌后 claim exit 3
  claim     无牌 3 / 挂名 3 / 部门不符 3 / 已被他人 claim 3 / 正常 0 / 重放 REPLAY
  start     未 claim 3 / 正常 0 / 重放 REPLAY
  report    未 start 3 / 证据缺失 3 / 正常 0 / 同证据 REPLAY / 异证据 3
  sign      未 start 3 / 代签 3 / 挂名 3 / 倒签 3 / 未来时间 3 / 缺字段 3 / 正常 0
  seats     经理 seats 行可按 05 §一4 直接 transition（无需另发牌）
  ledger    卡头 mapped == 台账态 PASS 0；不一致 rc 3 + DB alert（I 级）
  card      订单卡字节前后不动；不写活体 registry
用法：python -X utf8 test_mes_f10.py
"""
import contextlib
import datetime
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factory_seat  # noqa: E402
import mes  # noqa: E402
import mes_core  # noqa: E402

ROOT = os.path.dirname(HERE)
TMP_ROOT = os.path.join(ROOT, "state", "tmp")
os.makedirs(TMP_ROOT, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="r11_f10_", dir=TMP_ROOT)
ORDERS = os.path.join(TMP, "orders")
STATE = os.path.join(TMP, "state")
REG = os.path.join(TMP, "registry.json")
CODE = "C-20261120-F10"

SESS_MGR_A = "sess_aaaaaaa1-0000-0000-0000-000000000001"
SESS_MGR_B = "sess_bbbbbbb2-0000-0000-0000-000000000002"
SESS_W105 = "sess_10510500-0000-0000-0000-000000000105"
SESS_W105B = "sess_10510501-0000-0000-0000-000000000106"
SESS_W105_BAD = "sess_105bad00-0000-0000-0000-000000000107"
SESS_W106 = "sess_10610600-0000-0000-0000-000000000106"
SESS_E = "sess_eeeeeee5-0000-0000-0000-000000000005"
SESS_UNKNOWN = "sess_unknown-0000-0000-0000-000000000000"

RESULTS = []


def ok(name, detail=""):
    RESULTS.append((True, name, detail))
    print("[PASS] %-22s %s" % (name, detail))


def bad(name, detail=""):
    RESULTS.append((False, name, detail))
    print("[FAIL] %-22s %s" % (name, detail))


def expect(name, got_rc, want_rc, out, err, needle=""):
    text = (out or "") + (err or "")
    if got_rc == want_rc and (not needle or needle in text):
        ok(name, (text.strip().splitlines() or [""])[0][:100])
        return True
    bad(name, "rc=%s want=%s %s" % (got_rc, want_rc, text.strip()[:160]))
    return False


def A(*argv):
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = mes.main(list(argv))
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 2
    finally:
        sys.stdout, sys.stderr = so, se
    return rc, out.getvalue(), err.getvalue()


def run_mes(cmd, *extra):
    return A(cmd, "--state-dir", STATE, "--registry", REG, "--orders-dir", ORDERS, *extra)


def issue(badge, seat, dept, employee, by=SESS_MGR_A, order=CODE):
    return call_fs(factory_seat.cmd_badge_issue, badge=badge, seat=seat, dept=dept,
                   employee=employee, order=order, model="test", issued_by=by,
                   expires_at="", registry=REG)


def archive(badge, by=SESS_MGR_A):
    return call_fs(factory_seat.cmd_badge_archive, badge=badge, by=by, registry=REG)


def call_fs(fn, **kw):
    out = io.StringIO()
    so = sys.stdout
    sys.stdout = out
    try:
        rc = fn(factory_seat.argparse.Namespace(**kw))
    finally:
        sys.stdout = so
    return rc, out.getvalue(), ""


def write_card():
    d = os.path.join(ORDERS, CODE)
    os.makedirs(os.path.join(d, "signatures"), exist_ok=True)
    p = os.path.join(d, "order.md")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("# %s ｜ F10 fixture\n\n" % CODE)
        f.write("| 订单编号 | %s |\n|---|---|\n" % CODE)
        f.write("| 当前状态 | `producing` 生产中 |\n")
        f.write("| 进度 | 阶段 1/3 · 子需求 0/2 |\n\n")
        f.write("### 2.1 编成与流转签字表\n")
        f.write("| 序 | 流程节点 | 流到谁（岗位+工号） | 员工工号 | 会话号 | 进入时间 | 离开时间 | 结论 | 签字（工号+时间） |\n")
        f.write("|---|---|---|---|---|---|---|---|---|\n")
        f.write("| 1 | T01 做活 | W-105 | W-105 | — | — | — | 待接 | — |\n")
        f.write("| 2 | T02 复核 | W-106 | W-106 | — | — | — | 待接 | — |\n")
        f.write("| 3 | T03 补活 | W-106 | W-106 | — | — | — | 待接 | — |\n")
    return p


def write_sig(name, order=CODE, emp="W-105", sign="W-105", when=None, omit=""):
    d = os.path.join(ORDERS, CODE, "signatures")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%s.md" % name)
    fields = [("订单", order), ("任务", "T01 做活"), ("工号", emp), ("角色", "生产员工"),
              ("结论", "完成"), ("时间", when or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
              ("签字", sign)]
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        for k, v in fields:
            if k != omit:
                f.write("- %s：%s\n" % (k, v))
    return p


# 0 fixture
card_path = write_card()
card_sha = hashlib.sha256(open(card_path, "rb").read()).hexdigest()
reg = {"version": 1, "seats": [
    {"seat": "A-001", "dept": "A", "session": SESS_MGR_A, "status": "active"},
    {"seat": "B-001", "dept": "B", "session": SESS_MGR_B, "status": "active"},
], "badges": None}
with open(REG, "w", encoding="utf-8") as f:
    json.dump(reg, f, ensure_ascii=False, indent=2)

# 1 issue
rc, out, err = issue(SESS_W105, "W-105", "D", "W-105")
badges = json.load(open(REG, encoding="utf-8"))["badges"]
if rc == 0 and badges[SESS_W105]["status"] == "active":
    ok("issue.ok", "badges[%s…] active" % SESS_W105[:14])
else:
    bad("issue.ok", "rc=%s" % rc)
first_issued = badges[SESS_W105]["issued_at"]
rc, out, err = issue(SESS_W105, "W-105", "D", "W-105")
b2 = json.load(open(REG, encoding="utf-8"))["badges"][SESS_W105]
if rc == 0 and b2["issued_at"] == first_issued and b2.get("reissue_count") == 1:
    ok("issue.reissue", "issued_at 保留 %s，reissue_count=1" % first_issued)
else:
    bad("issue.reissue", str(b2))
rc, out, err = issue("W-105-xxx", "W-105", "D", "W-105")
expect("issue.bad-prefix", rc, 3, out, err, "sess_")
rc, out, err = issue(SESS_W105B, "W-105", "X", "W-105")
expect("issue.bad-dept", rc, 3, out, err, "A–F")
rc, out, err = issue(SESS_W105B, "W-105", "D", "W-105", by=SESS_UNKNOWN)
expect("issue.bad-issuer", rc, 3, out, err, "不在册")
# 预发 W-106 牌，供 start/report/sign 的前置负例走到「未 claim/未 start」而不是「不在册」
issue(SESS_W106, "W-106", "D", "W-106")

# 2 claim 负例
rc, out, err = run_mes("claim", "--order", CODE, "--node", "T01", "--actor", SESS_UNKNOWN)
expect("claim.no-badge", rc, 3, out, err, "不在册")
rc, out, err = run_mes("claim", "--order", CODE, "--node", "T01", "--actor", SESS_MGR_A)
expect("claim.manager-node", rc, 3, out, err, "挂名")
rc, out, err = issue(SESS_W105_BAD, "W-105", "B", "W-105")
rc, out, err = run_mes("claim", "--order", CODE, "--node", "T01", "--actor", SESS_W105_BAD)
expect("claim.wrong-dept", rc, 3, out, err, "部门不符")
# 正常 claim + 重放 + 他人抢
rc, out, err = run_mes("claim", "--order", CODE, "--node", "T01", "--actor", SESS_W105)
expect("claim.ok", rc, 0, out, err, "OK claim")
rc, out, err = run_mes("claim", "--order", CODE, "--node", "T01", "--actor", SESS_W105)
expect("claim.replay", rc, 0, out, err, "REPLAY")
issue(SESS_W105B, "W-105", "D", "W-105")
rc, out, err = run_mes("claim", "--order", CODE, "--node", "T01", "--actor", SESS_W105B)
expect("claim.taken", rc, 3, out, err, "已被")

# 3 start
rc, out, err = run_mes("start", "--order", CODE, "--node", "T02", "--actor", SESS_W106)
expect("start.no-claim", rc, 3, out, err, "未由当前 actor claim")
rc, out, err = run_mes("start", "--order", CODE, "--node", "T01", "--actor", SESS_W105)
expect("start.ok", rc, 0, out, err, "OK start")
rc, out, err = run_mes("start", "--order", CODE, "--node", "T01", "--actor", SESS_W105)
expect("start.replay", rc, 0, out, err, "REPLAY")

# 4 report
rc, out, err = run_mes("report", "--order", CODE, "--node", "T02", "--actor", SESS_W106,
                       "--evidence", os.path.join(TMP, "missing.txt"))
expect("report.no-start", rc, 3, out, err, "先 start")
ev1 = os.path.join(TMP, "ev1.txt")
ev2 = os.path.join(TMP, "ev2.txt")
open(ev1, "w").write("evidence-1\n")
open(ev2, "w").write("evidence-2\n")
rc, out, err = run_mes("report", "--order", CODE, "--node", "T01", "--actor", SESS_W105,
                       "--evidence", os.path.join(TMP, "missing.txt"))
expect("report.no-file", rc, 3, out, err, "不存在或为空")
rc, out, err = run_mes("report", "--order", CODE, "--node", "T01", "--actor", SESS_W105, "--evidence", ev1)
expect("report.ok", rc, 0, out, err, "OK report")
rc, out, err = run_mes("report", "--order", CODE, "--node", "T01", "--actor", SESS_W105, "--evidence", ev1)
expect("report.replay", rc, 0, out, err, "REPLAY")
rc, out, err = run_mes("report", "--order", CODE, "--node", "T01", "--actor", SESS_W105, "--evidence", ev2)
expect("report.overwrite", rc, 3, out, err, "禁止覆盖")

# 5 sign 负例
rc, out, err = run_mes("sign", "--order", CODE, "--node", "T02", "--employee", "W-106",
                       "--sig-file", write_sig("neg-nostart", emp="W-106", sign="W-106"),
                       "--actor", SESS_W106)
expect("sign.no-start", rc, 3, out, err, "先 start")
p = write_sig("neg-wrongemp", order=CODE, emp="W-106", sign="W-105")
rc, out, err = run_mes("sign", "--order", CODE, "--node", "T01", "--employee", "W-105",
                       "--sig-file", p, "--actor", SESS_W105)
expect("sign.file-name", rc, 3, out, err, "挂名")
p = write_sig("neg-forged", order=CODE, emp="W-105", sign="G-001")
rc, out, err = run_mes("sign", "--order", CODE, "--node", "T01", "--employee", "W-105",
                       "--sig-file", p, "--actor", SESS_W105)
expect("sign.forged", rc, 3, out, err, "代签")
past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
p = write_sig("neg-backdate", order=CODE, emp="W-105", sign="W-105", when=past)
rc, out, err = run_mes("sign", "--order", CODE, "--node", "T01", "--employee", "W-105",
                       "--sig-file", p, "--actor", SESS_W105)
expect("sign.backdate", rc, 3, out, err, "倒签")
future = (datetime.datetime.now() + datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
p = write_sig("neg-future", order=CODE, emp="W-105", sign="W-105", when=future)
rc, out, err = run_mes("sign", "--order", CODE, "--node", "T01", "--employee", "W-105",
                       "--sig-file", p, "--actor", SESS_W105)
expect("sign.future", rc, 3, out, err, "未来时间")
p = write_sig("neg-missfield", order=CODE, emp="W-105", sign="W-105", omit="工号")
rc, out, err = run_mes("sign", "--order", CODE, "--node", "T01", "--employee", "W-105",
                       "--sig-file", p, "--actor", SESS_W105)
expect("sign.miss-field", rc, 3, out, err, "缺字段")
p = write_sig("ok", order=CODE, emp="W-105", sign="W-105")
rc, out, err = run_mes("sign", "--order", CODE, "--node", "T01", "--employee", "W-105",
                       "--sig-file", p, "--actor", SESS_W105)
expect("sign.ok", rc, 0, out, err, "OK sign")

# 6 archive
rc, out, err = run_mes("claim", "--order", CODE, "--node", "T02", "--actor", SESS_W106)
expect("claim.w106", rc, 0, out, err, "OK claim")
rc, out, err = archive(SESS_W106)
expect("archive.ok", rc, 0, out, err, "ARCHIVED")
rc, out, err = archive(SESS_W106)
expect("archive.idem", rc, 0, out, err, "ALREADY")
rc, out, err = run_mes("claim", "--order", CODE, "--node", "T03", "--actor", SESS_W106)
expect("claim.archived", rc, 3, out, err, "非 active")

# 7 seats 行视作工牌：B 经理 sess 直接 transition producing→blocked（卡头态；不再依赖空台账 created 兜底）
rc, out, err = run_mes("transition", "--order", CODE, "--to", "blocked", "--actor", SESS_MGR_B)
expect("seats.transition", rc, 0, out, err, "producing->blocked")

# 8 check-ledger：PASS（台账=producing）→ rc 0；再推 qc → rc 3 + alert
ev_list = mes.load_events(STATE)
cur_state, seq = "blocked", mes.load_events(STATE)
for to in ("producing",):
    ev = mes.make_event(seq, CODE, "transition", SESS_MGR_B, cur_state, to)
    mes.append_event(STATE, ev)
    seq.append(ev)
    cur_state = to
mes_core.rebuild(STATE, ORDERS, quiet=True)
rc, out, err = run_mes("check-ledger", "--order", CODE)
expect("ledger.pass", rc, 0, out, err, "diff=0")
con = sqlite3.connect(mes_core.db_path(STATE))
ok1 = con.execute("SELECT ledger_ok FROM orders WHERE order_id=?", (CODE,)).fetchone()[0]
con.close()
ev = mes.make_event(mes.load_events(STATE), CODE, "transition", SESS_E, "producing", "qc")
mes.append_event(STATE, ev)
rc, out, err = run_mes("check-ledger", "--order", CODE)
con = sqlite3.connect(mes_core.db_path(STATE))
n_alert = con.execute("SELECT COUNT(*) FROM alerts WHERE kind='对账' AND order_id=?", (CODE,)).fetchone()[0]
ok0 = con.execute("SELECT ledger_ok FROM orders WHERE order_id=?", (CODE,)).fetchone()[0]
con.close()
if rc == 3 and "台账=qc" in out and n_alert == 1 and ok1 == 1 and ok0 == 0:
    ok("ledger.diff", "rc=3 + alerts 对账 1 条（I 级）+ ledger_ok 1→0")
else:
    bad("ledger.diff", "rc=%s alert=%s ok=%s→%s %s" % (rc, n_alert, ok1, ok0, out.strip()[:100]))

# 9 卡字节未动 / 活体 registry 未动
if hashlib.sha256(open(card_path, "rb").read()).hexdigest() == card_sha:
    ok("card.bytes", "订单卡 sha 前后同值 %s" % card_sha[:12])
else:
    bad("card.bytes", "卡被改")
live = os.path.join(ROOT, "state", "seat_registry.json")
live_has = os.path.exists(live) and json.load(open(live, encoding="utf-8")).get("badges")
if not live_has:
    ok("registry.live", "活体 registry badges 未动（仍为空）")
else:
    bad("registry.live", "活体 registry 出现 badges")

passed = sum(1 for r in RESULTS if r[0])
print("\n%d passed / %d total" % (passed, len(RESULTS)))
print("tmp=%s" % TMP)
sys.exit(0 if passed == len(RESULTS) else 2)
