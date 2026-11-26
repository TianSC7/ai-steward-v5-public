# -*- coding: utf-8 -*-
"""C-20261120-008 R1.1 · 七表派生库单测（W-105）

覆盖：
  crossday   23:00→02:00 / 22:00→09:00 / 02:10→03:10 三组跨日窗口
  ddl        七表齐 + user_version=1 + 索引数 + 外键
  cli.order  子命令前的根级 --state-dir 生效（不被子解析器默认值覆盖）
  cli.alias  卡 §1.2-1 字面 --rebuild/--check 等价子命令（含根级选项在前）
  idem       rebuild 两跑 sha256 同值、rebuild.sha256 落盘一致
  check      库↔卡差异 0；手改库 → rc 3 且报字段差异
  events     C-010 冻结 JSONL 导入 5 条；连导两次行数不变；actor 拆分
  state      全量 mapped 差异 0；C-TEST 两单 blocked + head_exact_match=no
  sig        签字解析：真实卡 6 件以上、sig_id 唯一、R0.3 时间/工号对上
  capacity   跨单合并：W-105 夜窗 22:00→次日 09:00、booked=240
  alias      void/failed → blocked（exact=no）；producing 精确
  alerts     缺段卡（C-001）产缺段 alert；C-008 无缺段 alert

全部写 state/tmp 沙箱；不写活体 state/mes、不碰 8900/8901、不改订单卡。
用法：python -X utf8 test_mes_core.py
"""
import contextlib
import datetime
import io
import json
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
TMP = tempfile.mkdtemp(prefix="r11_core_", dir=TMP_ROOT)
ORDERS = mes_core.DEFAULT_ORDERS
DEMO_EVENTS = os.path.join(ROOT, "reports", "evidence", "C-20261120-010",
                           "demo_state", "mes", "events.jsonl")
RESULTS = []


def ok(name, detail=""):
    RESULTS.append((True, name, detail))
    print("[PASS] %-20s %s" % (name, detail))


def bad(name, detail=""):
    RESULTS.append((False, name, detail))
    print("[FAIL] %-20s %s" % (name, detail))


def call(fn, *a, **k):
    buf = io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(buf):
        try:
            rc = fn(*a, **k)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
    rc = rc if isinstance(rc, int) else 0
    return rc, buf.getvalue()


def card(code):
    # 2026-11-24 P4b：orders/ 只留活单，历史单已归档到 Archive_V2_*/orders → 统一走解析器
    return mes_core.parse_card(mes_core.order_card_path(code, ORDERS), code, ORDERS)


# 1 跨日
cases = (("23:00", "02:00", 180), ("22:00", "09:00", 660), ("02:10", "03:10", 60))
base = datetime.datetime(2026, 11, 20, 12, 0)
cross = []
for a, b, want in cases:
    st, en = mes_core.parse_window(a, b, base)
    cross.append(int((en - st).total_seconds() // 60) == want)
if all(cross):
    ok("crossday", "23:00→02:00=180 / 22:00→09:00=660 / 02:10→03:10=60")
else:
    bad("crossday", str(cross))

# 2 DDL
db = mes_core.db_path(os.path.join(TMP, "ddl"))
con = mes_core.connect(db)
mes_core.init_db(con)
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
idx = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'").fetchone()[0]
uv = con.execute("PRAGMA user_version").fetchone()[0]
fk = con.execute("PRAGMA foreign_keys").fetchone()[0]
need = {"orders", "nodes", "signatures", "hours", "capacity", "events", "alerts"}
if need <= tables and idx >= 18 and uv == 1 and fk == 1:
    ok("ddl", "7 表齐 / 索引 %d / user_version=1 / foreign_keys=ON" % idx)
else:
    bad("ddl", "tables=%s idx=%d uv=%s fk=%s" % (sorted(tables), idx, uv, fk))
con.close()

# 2b CLI 选项顺序：子命令前的 --state-dir 必须生效（子解析器默认值不得覆盖）
scli = os.path.join(TMP, "cli_order")
rc, out = call(mes_core.main, ["--state-dir", scli, "rebuild"])
cli_db = os.path.exists(os.path.join(scli, "mes", "mes.sqlite"))
if rc == 0 and cli_db:
    ok("cli.order", "根级 --state-dir 在子命令前生效 → %s" % os.path.basename(scli))
else:
    bad("cli.order", "rc=%s db=%s %s" % (rc, cli_db, out.strip()[:80]))

# 2c 契约字面兼容入口：--rebuild / --check（根级 --state-dir 在前也须生效）
sali = os.path.join(TMP, "cli_alias")
rc1, out1 = call(mes_core.main, ["--state-dir", sali, "--rebuild"])
rc2, out2 = call(mes_core.main, ["--state-dir", sali, "--check"])
alias_db = os.path.exists(os.path.join(sali, "mes", "mes.sqlite"))
if rc1 == 0 and rc2 == 0 and alias_db and "diff=0" in out2:
    ok("cli.alias", "--rebuild/--check 等价子命令，check diff=0")
else:
    bad("cli.alias", "rc=%s/%s db=%s %s" % (rc1, rc2, alias_db, (out1 + out2).strip()[:100]))

s1 = os.path.join(TMP, "s1")
sha1 = mes_core.rebuild(s1, ORDERS, quiet=True)
sha2 = mes_core.rebuild(s1, ORDERS, quiet=True)
fp = os.path.join(s1, "mes", "rebuild.sha256")
disk = open(fp, encoding="utf-8").read().strip()
if sha1 == sha2 == disk and len(sha1) == 64:
    ok("idem", "两跑 sha 同值 %s…（rebuild.sha256 一致）" % sha1[:16])
else:
    bad("idem", "%s / %s / %s" % (sha1[:16], sha2[:16], disk[:16]))

# 4 check 0 / 手改库 rc 3
rc, out = call(mes_core.check, s1, ORDERS)
if rc == 0 and "diff=0" in out:
    ok("check.clean", out.strip().replace("\n", " | "))
else:
    bad("check.clean", "rc=%s %s" % (rc, out))

s1b = os.path.join(TMP, "s1_drift")
shutil.copytree(s1, s1b)
con = sqlite3.connect(mes_core.db_path(s1b))
con.execute("UPDATE orders SET title='手改标题' WHERE order_id='C-20261120-005'")
con.commit()
con.close()
rc, out = call(mes_core.check, s1b, ORDERS)
if rc == 3 and "orders C-20261120-005.title" in out:
    ok("check.drift", "手改库被抓 rc=3：%s" % out.strip().splitlines()[1].strip()[:60])
else:
    bad("check.drift", "rc=%s %s" % (rc, out))

# 5 events 导入幂等
s2 = os.path.join(TMP, "s2")
os.makedirs(os.path.join(s2, "mes"), exist_ok=True)
shutil.copy(DEMO_EVENTS, os.path.join(s2, "mes", "events.jsonl"))
mes_core.rebuild(s2, ORDERS, quiet=True)
con = mes_core.connect(mes_core.db_path(s2), create=False)
n1 = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
first = con.execute("SELECT actor_seat,actor_badge,source,ts FROM events ORDER BY event_id LIMIT 1").fetchone()
n_again = mes_core.import_events(con, s2, quiet=True)
con.commit()
n2 = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
con.close()
if n1 == n2 == 5 and n_again == 0 and first[0] == "A-001" and first[1] == "" and first[3].startswith("2026-11-20 21:47:43"):
    ok("events.idem", "5 条；再导 0；首行 actor_seat=A-001 badge='' source=%s" % first[2])
else:
    bad("events.idem", "n1=%s n2=%s again=%s first=%s" % (n1, n2, n_again, first))

# 6 state 全量
rc, out = call(mes_core.state_rows, s1, ORDERS, True)
tail = [l for l in out.splitlines() if l.startswith("state ")]
test_rows = [l for l in out.splitlines() if l.startswith("C-TEST-")]
if rc == 0 and tail and "diff=0" in tail[0] and all("blocked" in l and "no" in l for l in test_rows):
    ok("state", tail[0])
else:
    bad("state", "rc=%s %s" % (rc, tail))

# 7 签字解析
c8 = card("C-20261120-008")
sigs = c8["signatures"]
ids = [s["sig_id"] for s in sigs]
r03 = [s for s in sigs if s["task_code"] == "R0.3"]
r03_ok = (len(r03) == 1 and r03[0]["employee_id"] == "A-001"
          and r03[0]["signed_at"] == "2026-11-20 23:16:21"
          and r03[0]["path"].endswith("A-001-R03.md")
          and len(r03[0]["file_sha256"]) == 64)
if len(sigs) >= 6 and len(ids) == len(set(ids)) and r03_ok:
    ok("sig", "%d 件 / sig_id 唯一 / R0.3=A-001@23:16:21" % len(sigs))
else:
    bad("sig", "n=%d uniq=%s r03=%s" % (len(sigs), len(ids) == len(set(ids)), r03))

# 8 capacity 跨单合并（C-008 夜档）
caps = {c["cap_id"]: c for c in c8["capacity"]}
cap = caps.get("2026-11-20#N#W-105")
if cap and cap["window_start"] == "2026-11-20 22:00:00" and cap["window_end"] == "2026-11-21 09:00:00" \
        and cap["booked_min"] == 240 and cap["model_lane"] == "GLM" and abs(cap["load_pct"] - 36.36) < 0.01:
    ok("capacity", "2026-11-20#N#W-105 22:00→09:00 booked=240 load=36.36 GLM")
else:
    bad("capacity", str(cap))

# 9 别名/未知
m1, e1, t1 = mes_core.map_state("`void` 作废")
m2, e2, _ = mes_core.map_state("`failed` 全签但验收不过")
m3, e3, _ = mes_core.map_state("`producing` 生产中")
if (m1, e1) == ("blocked", False) and (m2, e2) == ("blocked", False) and (m3, e3) == ("producing", True):
    ok("alias", "void/failed→blocked(exact=no)；producing 精确")
else:
    bad("alias", "%s/%s/%s" % ((m1, e1), (m2, e2), (m3, e3)))

# 10 缺段 alert
c1 = card("C-20261120-001")
kinds1 = [a[1] for a in c1["alerts"]]
kinds8 = [a[1] for a in c8["alerts"]]
if "缺段" in kinds1 and "缺段" not in kinds8:
    ok("alerts", "C-001 缺段 alert %d 条；C-008 无" % len(c1["alerts"]))
else:
    bad("alerts", "C-001=%s C-008=%s" % (c1["alerts"], c8["alerts"]))

passed = sum(1 for r in RESULTS if r[0])
print("\n%d passed / %d total" % (passed, len(RESULTS)))
print("tmp=%s" % TMP)
return_code = 0 if passed == len(RESULTS) else 2
sys.exit(return_code)
