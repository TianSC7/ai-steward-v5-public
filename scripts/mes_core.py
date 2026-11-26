#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mes_core.py —— C-008 R1.1 七表派生库（stdlib only）

职责：
  1. 卡 → 五表幂等重建（orders/nodes/signatures/hours/capacity），events 台账导入，alerts 只追加；
  2. `check` 库 vs 现时卡快照逐行比对（差异 0 → rc 0，否则 rc 3）；
  3. `state` 状态推导 R1–R9 + `--compare-card` 全量一致性；
  4. 规范化导出 sha256 写 `state/mes/rebuild.sha256`（排除 orders.updated_at / capacity.updated_at）。

纪律：卡是正本、库是派生；events/alerts append-only；不碰 8900/8901；不写订单卡。
CLI：mes_core.py rebuild|check|state|events-import [--order X|--all] [--state-dir D] [--orders-dir O]
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "dll", "factory-blueprint", "state"))
DEFAULT_ORDERS = os.path.normpath(os.path.join(HERE, "..", "..", "..", "dll", "factory-blueprint", "orders"))

STATES16 = ("created", "queued", "planning", "contract-review", "auditing",
            "awaiting-release", "producing", "qc", "awaiting-closure",
            "delivering", "delivered", "accepted", "returned", "blocked",
            "suspended", "compensating")
ALIASES = {
    "待受理": "created", "待接": "created", "待受理建卡": "created",
    "已受理": "queued",
    "停产": "suspended", "停工": "suspended",
    "作废": "blocked", "void": "blocked",
    "失败": "blocked", "failed": "blocked",
}
TEST_CARDS = ("C-TEST-20261120-02", "C-TEST-20261120-03")


def is_test_card(code):
    """C-TEST-* 全体属测试单：非字典 key 只记 alert，不判未知态失败。"""
    return code.startswith("C-TEST-") or code in TEST_CARDS

DDL = """
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  title TEXT, priority TEXT, owner_seat TEXT, shift TEXT,
  status TEXT NOT NULL,
  created_at TEXT, due_at TEXT, delivered_at TEXT, accepted_at TEXT,
  stage_n INTEGER, stage_m INTEGER, sub_n INTEGER, sub_m INTEGER,
  risk TEXT, budget_tokens INTEGER,
  card_path TEXT UNIQUE, card_sha256 TEXT, card_mtime TEXT,
  head_status_text TEXT, ledger_ok INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  seq INTEGER NOT NULL, node_code TEXT, dept TEXT, seat TEXT, employee_id TEXT,
  session_id TEXT, plan_start TEXT, plan_end TEXT, limit_min INTEGER,
  entered_at TEXT, left_at TEXT, status TEXT, conclusion TEXT,
  evidence_path TEXT, sig_ref TEXT, src_line INTEGER, card_sha256 TEXT,
  UNIQUE(order_id, seq)
);
CREATE TABLE IF NOT EXISTS signatures (
  sig_id TEXT PRIMARY KEY,
  order_id TEXT REFERENCES orders(order_id),
  employee_id TEXT, role TEXT, task_code TEXT, session_id TEXT, badge_short TEXT,
  conclusion TEXT, signed_at TEXT, file_birth TEXT, file_mtime TEXT, file_sha256 TEXT,
  path TEXT UNIQUE, check_sig TEXT, badge_check TEXT
);
CREATE TABLE IF NOT EXISTS hours (
  hour_id TEXT PRIMARY KEY,
  order_id TEXT REFERENCES orders(order_id),
  employee_id TEXT, task_code TEXT, shift TEXT, date TEXT,
  plan_hours REAL, actual_hours REAL, wait_hours REAL,
  start_at TEXT, end_at TEXT, derived_from TEXT,
  standard_hours REAL, ratio REAL, status TEXT
);
CREATE TABLE IF NOT EXISTS capacity (
  cap_id TEXT PRIMARY KEY,
  date TEXT, shift TEXT, crew TEXT, model_lane TEXT,
  window_start TEXT, window_end TEXT, seat_cap INTEGER,
  order_id TEXT, task_code TEXT, lock_type TEXT,
  booked_min INTEGER, load_pct REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_event_id TEXT UNIQUE,
  ts TEXT, order_id TEXT, from_state TEXT, to_state TEXT,
  actor_seat TEXT, actor_badge TEXT, action TEXT, basis TEXT, source TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY,
  ts TEXT, order_id TEXT, node_id TEXT, level TEXT, kind TEXT,
  detail TEXT, source TEXT, status TEXT, closed_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_owner ON orders(owner_seat);
CREATE INDEX IF NOT EXISTS idx_orders_due ON orders(due_at);
CREATE INDEX IF NOT EXISTS idx_nodes_seat ON nodes(seat);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_window ON nodes(plan_start, plan_end);
CREATE INDEX IF NOT EXISTS idx_sig_order ON signatures(order_id);
CREATE INDEX IF NOT EXISTS idx_sig_emp ON signatures(employee_id);
CREATE INDEX IF NOT EXISTS idx_sig_time ON signatures(signed_at);
CREATE INDEX IF NOT EXISTS idx_hours_emp ON hours(employee_id, date);
CREATE INDEX IF NOT EXISTS idx_hours_order ON hours(order_id);
CREATE INDEX IF NOT EXISTS idx_cap_date_shift ON capacity(date, shift);
CREATE INDEX IF NOT EXISTS idx_cap_crew ON capacity(date, crew);
CREATE INDEX IF NOT EXISTS idx_events_order_ts ON events(order_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_action ON events(action);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_alerts_order ON alerts(order_id);
"""

TABLES5 = ("orders", "nodes", "signatures", "hours", "capacity")
PK = {"orders": "order_id", "nodes": "node_id", "signatures": "sig_id",
      "hours": "hour_id", "capacity": "cap_id"}


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _strip(s):
    return (s or "").replace("**", "").replace("`", "").strip()


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def db_path(state_dir):
    return os.path.join(state_dir, "mes", "mes.sqlite")


def events_path(state_dir):
    return os.path.join(state_dir, "mes", "events.jsonl")


def connect(db, create=True):
    if create:
        os.makedirs(os.path.dirname(db), exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(con):
    con.executescript(DDL)
    con.execute("PRAGMA user_version=1")
    con.commit()


# ---------- 时间 ----------

_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?")
_MD_RE = re.compile(r"^\s*(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?")
_HMS_RE = re.compile(r"^\s*(\d{2}):(\d{2})(?::(\d{2}))?")


def parse_dt(s, base=None):
    """卡内时间：完整日期 / MM-DD HH:MM / HH:MM（用 base 日期）。缺省返回 None。"""
    s = _strip(s)
    if not s or s in ("—", "-", "–"):
        return None
    s = s.replace("次日", "").strip()
    m = _DATE_RE.match(s)
    if m:
        y, mo, d, hh, mm, ss = [int(x or 0) for x in m.groups()]
        return datetime.datetime(y, mo, d, hh, mm, ss)
    m = _MD_RE.match(s)
    if m:
        mo, d, hh, mm, ss = [int(x or 0) for x in m.groups()]
        y = (base or datetime.datetime.now()).year
        return datetime.datetime(y, mo, d, hh, mm, ss)
    m = _HMS_RE.match(s)
    if m:
        hh, mm, ss = [int(x or 0) for x in m.groups()]
        b = base or datetime.datetime.now()
        return datetime.datetime(b.year, b.month, b.day, hh, mm, ss)
    return None


def parse_window(start_s, end_s, base=None):
    """返回 (start_dt, end_dt)；end<=start 自动 +1 天（跨日 23:00→02:00 / 22:00→09:00）。"""
    st = parse_dt(start_s, base)
    en = parse_dt(end_s, base)
    if st and en and en <= st:
        en += datetime.timedelta(days=1)
    return st, en


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _mins(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 60.0, 2)


def _overlap_min(a1, a2, b1, b2):
    if not (a1 and a2 and b1 and b2):
        return 0.0
    lo = max(a1, b1)
    hi = min(a2, b2)
    return max(0.0, (hi - lo).total_seconds() / 60.0)


# ---------- 卡解析 ----------

def _section(text, num):
    lines = text.splitlines()
    out, on = [], False
    for ln in lines:
        m = re.match(r"^##\s+(\d+)\s+", ln)
        if m:
            if on and int(m.group(1)) != num:
                break
            on = int(m.group(1)) == num
            continue
        if on:
            out.append(ln)
    return "\n".join(out)


def _subsection(text, num, sub):
    """取 '### <num>.<sub>' 到下一个 '### ' 或 '## ' 之间。返回 (正文, 起始行号)。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^###\s+%s\.%s\b" % (num, sub), ln):
            start = i + 1
            break
    if start is None:
        return "", None
    out = []
    for ln in lines[start:]:
        if re.match(r"^###\s+", ln) or re.match(r"^##\s+", ln):
            break
        out.append(ln)
    return "\n".join(out), start


def _table_rows(block, start_line=0):
    """表格行 [(cells, line_no)]，跳过表头与分隔行。"""
    rows = []
    for i, ln in enumerate(block.splitlines()):
        if not ln.strip().startswith("|"):
            continue
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if not c or set(c[0]) <= set("-: ") or c[0] in ("节点", "序", "字段", "项"):
            continue
        rows.append((c, start_line + i + 1))
    return rows


_KV_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*$")
_CODE_RE = re.compile(r"^([A-Za-z]{1,3}-?\d+(?:\.\d+)?)")


def extract_code(s):
    s = _strip(s)
    m = _CODE_RE.match(s)
    return m.group(1) if m else ""


def dept_of(seat):
    s = _strip(seat)
    m = re.match(r"^([A-F])\b", s)
    if m:
        return m.group(1)
    if re.match(r"^W-", s):
        return "D"
    return ""


def map_node_status(s):
    s = _strip(s)
    if "✅" in s or "通过" in s or "完成" in s:
        return "完成"
    if "退回" in s or "不通过" in s:
        return "退回"
    if "阻塞" in s or "挂起" in s:
        return "阻塞"
    if "进行" in s or "复核中" in s or "🔄" in s:
        return "进行中"
    return "待接"


def parse_header(text):
    h = {}
    for ln in text.splitlines():
        if ln.startswith("## "):
            break
        m = _KV_RE.match(ln)
        if not m:
            continue
        k = _strip(m.group(1))
        if k and k not in h:
            h[k] = m.group(2).strip()
    return h


def head_token(raw):
    s = _strip(raw)
    m = re.search(r"[A-Za-z][A-Za-z0-9\-]+", s)
    if m:
        return m.group(0)
    m = re.match(r"([^\s（(]+)", s)
    return m.group(1) if m else ""


def map_state(raw):
    """返回 (mapped, exact, token)。R1 精确 → R2 别名 → 未识别 blocked。"""
    t = head_token(raw)
    if t in STATES16:
        return t, True, t
    if t.lower() in STATES16:
        return t.lower(), True, t
    if t in ALIASES:
        return ALIASES[t], False, t
    if t.lower() in ALIASES:
        return ALIASES[t.lower()], False, t
    return "blocked", False, t


_NODE_RULES = (("T00", "planning"), ("T01", "auditing"),
               ("W-", "producing"), ("E-", "qc"), ("A-", "delivering"))


def _derive_from_timeline(text):
    """R3：最晚完成/进行中节点 → 推导态（只认 T00/T01/W-/E-/A-）。

    扫 §3 时间轴 + §2.0/§2.1 表的前两列；进行中优先于已完成。
    未列出的节点码（T02+ 等）不猜，直接跳过。
    """
    blocks = [_section(text, 3)]
    for sub in (0, 1):
        b, _ = _subsection(text, 2, sub)
        blocks.append(b)
    last_done, active = "", ""
    for block in blocks:
        for ln in block.splitlines():
            if not ln.strip().startswith("|"):
                continue
            c = [x.strip() for x in ln.strip().strip("|").split("|")]
            if len(c) < 2:
                continue
            probe = " ".join(c[:2])
            st = ""
            for kw, mapped in _NODE_RULES:
                if re.search(r"(^|[\s`（(])%s" % re.escape(kw), probe):
                    st = mapped
                    break
            if not st:
                continue
            row = " ".join(c)
            doing = re.search(r"进行中|生产中|🔄|复核中", row)
            done = re.search(r"完成|通过|✅|已交付", row) and not re.search(
                r"不通过|未通过|驳回|未完成", row)
            if doing:
                active = st
            elif done:
                last_done = st
    return active or last_done


def _rule_suspended(text):
    """R7：§3 红线/搁置/停产记录且无 D 号恢复 → suspended。"""
    suspended = False
    for ln in _section(text, 3).splitlines():
        if re.search(r"红线|搁置|暂停生产", ln) and not re.search(r"解除|恢复", ln):
            suspended = True
        elif suspended and re.search(r"恢复", ln) and re.search(r"D-\d{8}-\d+", ln):
            suspended = False
    return suspended


def _rule_blocked(text):
    """R6：§3 阻塞记录；最后一条有解除/已消除则不算。"""
    hits = [ln for ln in _section(text, 3).splitlines()
            if re.search(r"(?<!非)阻塞", ln)]
    if not hits:
        return False
    return not re.search(r"解除|已消除|已恢复", hits[-1])


def _rule_terminal(text):
    """R5：§4 发起人验收勾选 → accepted / returned（终态）。"""
    for ln in _section(text, 4).splitlines():
        if "验收" not in ln:
            continue
        m = re.search(r"[☒☑]\s*(接受|有条件|退回)", ln)
        if m:
            return {"接受": "accepted", "退回": "returned"}.get(m.group(1), "")
        if re.search(r"\baccepted\b", ln, re.I):
            return "accepted"
        if re.search(r"\breturned\b", ln, re.I):
            return "returned"
    return ""


def _rule_compensating(text):
    """R8：同一行写明返修/追账且部分交付 → compensating（不跨行猜）。"""
    for ln in text.splitlines():
        if re.search(r"返修|追账", ln) and re.search(r"部分交付|部分完成", ln):
            return True
    return False


def _rule_hold(text):
    """R4：质检过未收口 → awaiting-closure；审计过未放行 → awaiting-release。"""
    qc_pass = audit_pass = released = False
    for ln in _section(text, 3).splitlines():
        neg = re.search(r"不通过|未通过|FAIL|驳回", ln)
        if re.search(r"质检|E-\d", ln) and not neg and re.search(r"通过|PASS|✅", ln):
            qc_pass = True
        if re.search(r"审计|T01", ln) and not neg and re.search(r"通过|PASS|✅", ln):
            audit_pass = True
        if re.search(r"放行", ln) and not re.search(r"未放行|待放行", ln):
            released = True
    if qc_pass and not re.search(r"收口|验收通过|已交付|delivered", text):
        return "awaiting-closure"
    if audit_pass and not released:
        return "awaiting-release"
    return ""


def _derive_no_head(text):
    """卡头缺失时的 R3–R9 保守推导（优先级 R7→R6→R5→R8→R4→R3→R9）。"""
    if _rule_suspended(text):
        return "suspended"
    if _rule_blocked(text):
        return "blocked"
    term = _rule_terminal(text)
    if term:
        return term
    if _rule_compensating(text):
        return "compensating"
    hold = _rule_hold(text)
    if hold:
        return hold
    st = _derive_from_timeline(text)
    if st:
        return st
    # R9 缺段降级：缺 §1.2 → created；缺 §2.0/§2.1 → queued；
    # 缺 §7 的「过质检」分支已由 R4 归 awaiting-closure；其余无信号取保守前态 created。
    if "### 1.2" not in text:
        return "created"
    if "### 2.0" not in text or "### 2.1" not in text:
        return "queued"
    return "created"


def derive_status_info(text, header):
    """返回 (mapped, exact, src)；src=head|alias|derived|unknown。"""
    raw = header.get("当前状态", "")
    mapped, exact, token = map_state(raw)
    if exact:
        return mapped, True, "head"
    if raw.strip():
        if token in ALIASES or token.lower() in ALIASES:
            return mapped, False, "alias"
        return "blocked", False, "unknown"
    return _derive_no_head(text), False, "derived"


def derive_status(text, header):
    """R1 卡头 → R2 别名/未知 → R3–R9 推导（卡头缺失时）。"""
    st, exact, _ = derive_status_info(text, header)
    return st, exact


def _parse_nodes(text, code, card_sha):
    nodes = []
    base = parse_dt(parse_header(text).get("下单时间", ""))
    sec20, ln20 = _subsection(text, 2, 0)
    if ln20 is not None:
        for c, line_no in _table_rows(sec20, ln20):
            if len(c) < 8:
                continue
            node_raw, seat, emp = c[0], c[1], c[2]
            ps, pe = parse_window(c[3], c[4], base)
            limit = _limit_min(c[5])
            emp_id = _strip(emp) if re.match(r"^[A-Z]-\d+", _strip(emp)) else ""
            seat_id = _seat_token(seat) or _seat_token(emp)
            nodes.append({
                "node_id": "", "order_id": code, "seq": len(nodes) + 1,
                "node_code": extract_code(node_raw) or "N%02d" % (len(nodes) + 1),
                "dept": dept_of(seat_id), "seat": seat_id, "employee_id": emp_id,
                "session_id": "", "plan_start": fmt(ps), "plan_end": fmt(pe),
                "limit_min": limit, "entered_at": "", "left_at": "",
                "status": map_node_status(c[7]), "conclusion": _strip(c[7]),
                "evidence_path": "", "sig_ref": "", "src_line": line_no,
                "card_sha256": card_sha,
            })
    sec21, ln21 = _subsection(text, 2, 1)
    if ln21 is not None:
        for c, line_no in _table_rows(sec21, ln21):
            if len(c) < 9:
                continue
            node_raw, who, emp = c[1], c[2], c[3]
            emp_id = _strip(emp) if re.match(r"^[A-Z]-\d+", _strip(emp)) else ""
            seat_id = _seat_token(who) or _seat_token(emp)
            sess = _sess_token(c[4])
            entered = parse_dt(c[5], base)
            left = parse_dt(c[6], base)
            concl = _strip(c[7])
            nodes.append({
                "node_id": "", "order_id": code, "seq": len(nodes) + 1,
                "node_code": extract_code(node_raw) or "N%02d" % (len(nodes) + 1),
                "dept": dept_of(seat_id), "seat": seat_id, "employee_id": emp_id,
                "session_id": sess, "plan_start": "", "plan_end": "",
                "limit_min": None, "entered_at": fmt(entered), "left_at": fmt(left),
                "status": map_node_status(concl), "conclusion": concl,
                "evidence_path": "", "sig_ref": "", "src_line": line_no,
                "card_sha256": card_sha,
            })
    for n in nodes:
        n["node_id"] = "%s#%d" % (code, n["seq"])
    return nodes


def _seat_token(s):
    m = re.search(r"\b([A-FW]-\d{2,3})\b", _strip(s))
    return m.group(1) if m else ""


def _sess_token(s):
    m = re.search(r"(sess_[A-Za-z0-9\-]+)", _strip(s))
    return m.group(1) if m else ""


def _limit_min(s):
    s = _strip(s).lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    if m:
        return int(round(float(m.group(1)) * 60))
    m = re.search(r"(\d+)\s*min", s)
    if m:
        return int(m.group(1))
    return None


def _parse_signatures(code, card_dir, order_code):
    out = []
    sdir = os.path.join(card_dir, "signatures")
    if not os.path.isdir(sdir):
        return out
    fields = ("订单", "任务", "工号", "角色", "结论", "时间", "签字", "工牌")
    for fn in sorted(os.listdir(sdir)):
        if not fn.lower().endswith(".md"):
            continue
        p = os.path.join(sdir, fn)
        text = _read(p)
        vals = {}
        for f in fields:
            m = re.search(r"^\s*-\s*%s\s*[：:]\s*(.+)$" % re.escape(f), text, re.M)
            vals[f] = _strip(m.group(1)) if m else ""
        emp = vals.get("工号", "")
        task = vals.get("任务", "").split()[0] if vals.get("任务") else fn[:-3]
        signed = parse_dt(vals.get("时间", ""))
        st = os.stat(p)
        birth = getattr(st, "st_birthtime", None) or st.st_ctime
        birth_dt = datetime.datetime.fromtimestamp(birth)
        mtime_dt = datetime.datetime.fromtimestamp(st.st_mtime)
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        check = "FAIL(缺字段)"
        if signed and emp:
            diff = abs((birth_dt - signed).total_seconds())
            check = "OK" if diff <= 10 else "FAIL(差%ds)" % int(round(diff))
        badge = vals.get("工牌", "")
        out.append({
            "sig_id": "%s#%s#%s" % (code, emp or fn[:-3], task),
            "order_id": code, "employee_id": emp, "role": vals.get("角色", ""),
            "task_code": task, "session_id": _sess_token(badge),
            "badge_short": (_sess_token(badge) or "")[:13],
            "conclusion": vals.get("结论", ""), "signed_at": fmt(signed),
            "file_birth": fmt(birth_dt), "file_mtime": fmt(mtime_dt),
            "file_sha256": h, "path": "%s/signatures/%s" % (order_code, fn),
            "check_sig": check, "badge_check": "在册" if badge else "未登记",
            "stem": fn[:-3],
        })
    return out


def _shift_of(dt):
    if not dt:
        return "", ""
    h = dt.hour
    if 8 <= h < 22:
        return dt.strftime("%Y-%m-%d"), "D"
    if h >= 22:
        return dt.strftime("%Y-%m-%d"), "N"
    return (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d"), "N"


def _shift_window(date_s, shift):
    d = datetime.datetime.strptime(date_s, "%Y-%m-%d")
    if shift == "D":
        return d.replace(hour=8), d.replace(hour=22)
    return d.replace(hour=22), d.replace(hour=22) + datetime.timedelta(hours=11)


def _build_hours_capacity(nodes, code):
    hours, caps = [], {}
    prev_left = None
    for n in nodes:
        emp = n.get("employee_id") or ""
        ps = parse_dt(n.get("plan_start", ""))
        pe = parse_dt(n.get("plan_end", ""))
        ent = parse_dt(n.get("entered_at", ""))
        lft = parse_dt(n.get("left_at", ""))
        if emp:
            plan_min = _mins(ps, pe)
            act_min = _mins(ent, lft)
            wait_min = _mins(prev_left, ent) if (prev_left and ent) else None
            plan_h = round(plan_min / 60.0, 2) if plan_min is not None else None
            act_h = round(act_min / 60.0, 2) if act_min is not None else None
            wait = round(wait_min / 60.0, 2) if wait_min is not None else None
            date_s, shift = _shift_of(ps or ent)
            status = "缺时间戳" if act_h is None else "ok"
            hours.append({
                "hour_id": "%s#%s#%s" % (code, emp, n.get("node_code") or n["seq"]),
                "order_id": code, "employee_id": emp,
                "task_code": n.get("node_code") or "", "shift": shift, "date": date_s,
                "plan_hours": plan_h, "actual_hours": act_h, "wait_hours": wait,
                "start_at": fmt(ent) or n.get("plan_start", ""),
                "end_at": fmt(lft) or n.get("plan_end", ""),
                "derived_from": json.dumps({"node": n["node_id"], "src": "§2.0/§2.1"},
                                           ensure_ascii=False, sort_keys=True),
                "standard_hours": None, "ratio": None, "status": status,
                "_seq": n["seq"],
            })
        if emp and ps and pe:
            date_s, shift = _shift_of(ps)
            w1, w2 = _shift_window(date_s, shift)
            key = (date_s, shift, emp)
            cap = caps.setdefault(key, {
                "cap_id": "%s#%s#%s" % (date_s, shift, emp),
                "date": date_s, "shift": shift, "crew": emp,
                "model_lane": "GLM" if emp in ("W-105", "W-106", "W-107") else "R4",
                "window_start": fmt(w1), "window_end": fmt(w2), "seat_cap": 1,
                "order_id": code, "task_code": n.get("node_code") or "",
                "lock_type": "锁定", "booked_min": 0, "load_pct": 0.0,
                "updated_at": _now(),
            })
            cap["booked_min"] = int(round(cap["booked_min"] + _overlap_min(ps, pe, w1, w2)))
            total = (w2 - w1).total_seconds() / 60.0
            cap["load_pct"] = round(cap["booked_min"] / total * 100.0, 2) if total else 0.0
        if lft:
            prev_left = lft
    return hours, list(caps.values())


def _dedupe(rows, key, suffix):
    """真实卡存在同工号同任务的多次签字/同名节点：撞主键时用 suffix 去重（保 plan 格式优先）。"""
    seen = {}
    for r in rows:
        seen[r[key]] = seen.get(r[key], 0) + 1
    for r in rows:
        if seen[r[key]] > 1:
            r[key] = "%s#%s" % (r[key], r.get(suffix, ""))
    return rows


def _merge_caps(cards):
    """capacity 为日×班×席一行：跨单占用合并（plan §1.2 cap_id 口径）。"""
    merged = {}
    for _code, card in cards:
        for c in card["capacity"]:
            k = c["cap_id"]
            if k not in merged:
                merged[k] = dict(c)
                merged[k]["_orders"] = {c["order_id"]}
                merged[k]["_tasks"] = {c["task_code"]} if c["task_code"] else set()
            else:
                m = merged[k]
                m["booked_min"] += c["booked_min"]
                m["_orders"].add(c["order_id"])
                if c["task_code"]:
                    m["_tasks"].add(c["task_code"])
    for m in merged.values():
        m["order_id"] = ",".join(sorted(m.pop("_orders")))
        m["task_code"] = ",".join(sorted(m.pop("_tasks")))
        w1, w2 = parse_dt(m["window_start"]), parse_dt(m["window_end"])
        total = (w2 - w1).total_seconds() / 60.0 if (w1 and w2) else 0
        m["load_pct"] = round(m["booked_min"] / total * 100.0, 2) if total else 0.0
    return list(merged.values())


def parse_card(card_path, order_code, orders_dir):
    text = _read(card_path)
    h = parse_header(text)
    base = parse_dt(h.get("下单时间", ""))
    raw = h.get("当前状态", "")
    status, exact, src = derive_status_info(text, h)
    title = ""
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        t = lines[0][2:].strip()
        if "｜" in t:
            title = t.split("｜", 1)[1].strip()
        else:
            title = t
    st = os.stat(card_path)
    sha = hashlib.sha256(open(card_path, "rb").read()).hexdigest()
    prog = _strip(h.get("进度", ""))
    m = re.search(r"阶段\s*(\d+)\s*/\s*(\d+)", prog)
    stg = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    m = re.search(r"子需求\s*(\d+)\s*/\s*(\d+)", prog)
    sub = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    budget = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*M\b", _strip(h.get("预算", "")))
    if m:
        budget = int(round(float(m.group(1)) * 1000000))
    due = parse_dt(h.get("交付截止", "")) or parse_dt(h.get("预期交付", ""))
    owner = ""
    m = re.search(r"在谁手上[：:]\s*\**\s*([A-Z]-\d{3})", raw)
    if m:
        owner = m.group(1)
    shift = ""
    m = re.search(r"SHIFT-\d{8}-([DN])", h.get("排班", ""))
    if m:
        shift = m.group(1)
    order = {
        "order_id": order_code, "title": title, "priority": _strip(h.get("优先级", "")),
        "owner_seat": owner, "shift": shift, "status": status,
        "created_at": fmt(base), "due_at": fmt(due),
        "delivered_at": "", "accepted_at": "",
        "stage_n": stg[0], "stage_m": stg[1], "sub_n": sub[0], "sub_m": sub[1],
        "risk": _strip(h.get("风险评级", "")), "budget_tokens": budget,
        "card_path": "%s/order.md" % order_code, "card_sha256": sha,
        "card_mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "head_status_text": _strip(raw), "ledger_ok": 0, "updated_at": _now(),
    }
    nodes = _parse_nodes(text, order_code, sha)
    sigs = _parse_signatures(order_code, os.path.dirname(card_path), order_code)
    hours, caps = _build_hours_capacity(nodes, order_code)
    _dedupe(sigs, "sig_id", "stem")
    _dedupe(hours, "hour_id", "_seq")
    for s in sigs:
        s.pop("stem", None)
    for h in hours:
        h.pop("_seq", None)
    alerts = []
    today = (base or datetime.datetime.now()).strftime("%Y-%m-%d")
    if not _section(text, 1) or "### 1.2" not in text:
        alerts.append((today + "#" + order_code + "#q12", "缺段", "缺 §1.2 合同"))
    if "### 2.0" not in text:
        alerts.append((today + "#" + order_code + "#q20", "缺段", "缺 §2.0 节点时限表"))
    if "### 2.1" not in text:
        alerts.append((today + "#" + order_code + "#q21", "缺段", "缺 §2.1 编成签字表"))
    token = head_token(raw)
    known = token in STATES16 or token.lower() in STATES16
    if raw.strip() and not known:
        if token in ALIASES or token.lower() in ALIASES:
            if is_test_card(order_code):
                alerts.append((today + "#" + order_code + "#qkey", "非字典key",
                               "卡头 `%s` 非 16 key → %s（C-TEST 例外，R2 别名映射）"
                               % (token, status)))
        else:
            alerts.append((today + "#" + order_code + "#qkey", "未知态",
                           "卡头 `%s` 未识别 → blocked（R2 不猜）" % token))
    if not re.search(r"^##\s+11\s+", text, re.M) and status == "delivering":
        alerts.append((today + "#" + order_code + "#q11", "缺段",
                       "缺 §11 提货清单（delivered 前须补）"))
    return {"order": order, "nodes": nodes, "signatures": sigs, "hours": hours,
            "capacity": caps, "alerts": alerts, "exact": exact, "raw": raw,
            "status_src": src}


def order_scan_dirs(orders_dir):
    """重建/核对扫描口径（2026-11-24 P4b）：活单目录 + 同级 `Archive_V2_*/orders`。

    11-21 起 orders/ 只留活单；若重建不并扫归档，13 单账本会缩成 1 单（已实测）。
    仅默认目录（或 MES_ORDERS_DIR 指向默认目录）并扫归档，显式传入的其他目录保持原行为。
    """
    dirs = [orders_dir]
    if os.path.normpath(orders_dir) != os.path.normpath(DEFAULT_ORDERS):
        return dirs
    parent = os.path.dirname(os.path.normpath(orders_dir))
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return dirs
    for name in names:
        if name.startswith("Archive_V2_"):
            p = os.path.join(parent, name, "orders")
            if os.path.isdir(p):
                dirs.append(p)
    return dirs


def order_card_path(code, orders_dir):
    """单号→order.md 路径（活单优先，归档兜底）；找不到时回落默认拼法。"""
    for d in order_scan_dirs(orders_dir):
        p = os.path.join(d, code, "order.md")
        if os.path.isfile(p):
            return p
    return os.path.join(orders_dir, code, "order.md")


def list_cards(orders_dir):
    out = []
    seen = set()
    for d in order_scan_dirs(orders_dir):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.startswith("C-") or name in seen:
                continue
            p = os.path.join(d, name, "order.md")
            if os.path.isfile(p):
                seen.add(name)
                out.append((name, p))
    return out


# ---------- DB 写入 ----------

_ORDER_COLS = ("order_id", "title", "priority", "owner_seat", "shift", "status",
               "created_at", "due_at", "delivered_at", "accepted_at", "stage_n",
               "stage_m", "sub_n", "sub_m", "risk", "budget_tokens", "card_path",
               "card_sha256", "card_mtime", "head_status_text", "ledger_ok", "updated_at")


def _insert(con, table, row):
    cols = list(row.keys())
    sql = "INSERT INTO %s (%s) VALUES (%s)" % (
        table, ",".join(cols), ",".join("?" * len(cols)))
    con.execute(sql, [row[c] for c in cols])


def _upsert_order(con, row):
    cols = [c for c in _ORDER_COLS if c != "order_id"]
    sets = ",".join("%s=?" % c for c in cols)
    sql = "INSERT INTO orders (%s) VALUES (%s) ON CONFLICT(order_id) DO UPDATE SET %s" % (
        ",".join(_ORDER_COLS), ",".join("?" * len(_ORDER_COLS)), sets)
    con.execute(sql, [row[c] for c in _ORDER_COLS] + [row[c] for c in cols])


def _delete_order(con, code):
    con.execute("DELETE FROM nodes WHERE order_id=?", (code,))
    con.execute("DELETE FROM signatures WHERE order_id=?", (code,))
    con.execute("DELETE FROM hours WHERE order_id=?", (code,))
    con.execute("DELETE FROM orders WHERE order_id=?", (code,))


def import_events(con, state_dir, quiet=False):
    p = events_path(state_dir)
    n = 0
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts = (e.get("ts") or "").replace("T", " ")
                actor = e.get("actor") or ""
                badge = actor if actor.startswith("sess_") else ""
                seat = "" if badge else actor
                basis = " | ".join(x for x in (e.get("d_no", ""), e.get("ref_order", ""),
                                               e.get("reason", ""), e.get("idem_key", "")) if x)
                row = {
                    "src_event_id": e.get("event_id") or hashlib.sha1(line.encode()).hexdigest()[:16],
                    "ts": ts, "order_id": e.get("order", ""),
                    "from_state": e.get("from", ""), "to_state": e.get("to", ""),
                    "actor_seat": seat, "actor_badge": badge,
                    "action": e.get("action", ""), "basis": basis,
                    "source": "events.jsonl#%s" % e.get("seq", ln),
                    "raw": line[:500],
                }
                cur = con.execute(
                    "INSERT OR IGNORE INTO events (src_event_id,ts,order_id,from_state,to_state,"
                    "actor_seat,actor_badge,action,basis,source,raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (row["src_event_id"], row["ts"], row["order_id"], row["from_state"],
                     row["to_state"], row["actor_seat"], row["actor_badge"], row["action"],
                     row["basis"], row["source"], row["raw"]))
                n += cur.rowcount
    if not quiet:
        print("events_import=%d (file=%s)" % (n, "yes" if os.path.isfile(p) else "no"))
    return n


def _add_alerts(con, card, ts):
    for aid, kind, detail in card["alerts"]:
        con.execute(
            "INSERT OR IGNORE INTO alerts (alert_id,ts,order_id,node_id,level,kind,detail,source,status,closed_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, ts, card["order"]["order_id"], "", "黄", kind, detail,
             "mes_core.rebuild", "open", ""))


def rebuild(state_dir, orders_dir, order=None, force=False, quiet=False):
    db = db_path(state_dir)
    if os.path.exists(db):
        bdir = os.path.join(state_dir, "mes", "backup")
        os.makedirs(bdir, exist_ok=True)
        bak = os.path.join(bdir, "mes-%s.sqlite" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(db, "rb") as src, open(bak, "wb") as dst:
            dst.write(src.read())
    con = connect(db)
    try:
        init_db(con)
        all_cards = list_cards(orders_dir)
        cards = [(c, p) for c, p in all_cards if not order or c == order]
        parsed = []
        for code, p in all_cards:
            parsed.append((code, parse_card(p, code, orders_dir)))
        selected = [(c, card) for c, card in parsed if not order or c == order]
        # capacity 是全局日×班×席派生：每次重建整表重算（跨单合并占用）
        con.execute("DELETE FROM capacity")
        for c in _merge_caps(parsed):
            _insert(con, "capacity", c)
        for code, card in selected:
            # alerts 是 append-only 派生：即使卡字节未变也要按当前规则补记（OR IGNORE 幂等）
            _add_alerts(con, card, _now())
            row = con.execute("SELECT card_sha256 FROM orders WHERE order_id=?", (code,)).fetchone()
            if row and row[0] == card["order"]["card_sha256"] and not force:
                # 卡字节未变但 signatures/ 目录可能新增/改写签字件：签字表每次重同步
                con.execute("DELETE FROM signatures WHERE order_id=?", (code,))
                for s in card["signatures"]:
                    _insert(con, "signatures", s)
                continue
            _delete_order(con, code)
            _upsert_order(con, card["order"])
            for n in card["nodes"]:
                _insert(con, "nodes", n)
            for s in card["signatures"]:
                _insert(con, "signatures", s)
            for h in card["hours"]:
                _insert(con, "hours", h)
        n_ev = import_events(con, state_dir, quiet=True)
        con.commit()
        sha = export_sha(con)
        with open(os.path.join(state_dir, "mes", "rebuild.sha256"), "w", encoding="utf-8") as f:
            f.write(sha + "\n")
        if not quiet:
            counts = {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in TABLES5}
            print("rebuild ok orders=%d nodes=%d signatures=%d hours=%d capacity=%d events=%d imported=%d sha=%s"
                  % (counts["orders"], counts["nodes"], counts["signatures"], counts["hours"],
                     counts["capacity"],
                     con.execute("SELECT COUNT(*) FROM events").fetchone()[0], n_ev, sha[:16]))
        return sha
    finally:
        con.close()


def export_sha(con):
    """五表规范化导出（PK 排序；updated_at 置空）→ sha256。"""
    h = hashlib.sha256()
    for t in TABLES5:
        pk = PK[t]
        cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % t)]
        q = "SELECT %s FROM %s ORDER BY %s" % (",".join(cols), t, pk)
        for row in con.execute(q):
            d = dict(zip(cols, row))
            if "updated_at" in d:
                d["updated_at"] = ""
            h.update((t + "|" + json.dumps(d, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")) + "\n").encode("utf-8"))
    return h.hexdigest()


# ---------- check ----------

def _snapshot(cards, cap_cards=None):
    snap = {t: {} for t in TABLES5}
    for _code, card in cards:
        snap["orders"][card["order"]["order_id"]] = card["order"]
        for n in card["nodes"]:
            snap["nodes"][n["node_id"]] = n
        for s in card["signatures"]:
            snap["signatures"][s["sig_id"]] = s
        for h in card["hours"]:
            snap["hours"][h["hour_id"]] = h
    for c in _merge_caps(cap_cards if cap_cards is not None else cards):
        snap["capacity"][c["cap_id"]] = c
    return snap


def check(state_dir, orders_dir, order=None, limit=50):
    con = connect(db_path(state_dir), create=False)
    try:
        all_cards = list_cards(orders_dir)
        parsed = [(c, parse_card(p, c, orders_dir)) for c, p in all_cards]
        sel = [(c, card) for c, card in parsed if not order or c == order]
        snap = _snapshot(sel, parsed)
        if order:
            # 单过滤模式：capacity 是跨单合并行，只保留本单卡引用到的 cap_id，
            # 但值仍取全库合并结果（否则共享行 booked_min/load_pct 会对不上）。
            cap_ids = {c["cap_id"] for _code, card in sel for c in card["capacity"]}
            snap["capacity"] = {k: v for k, v in snap["capacity"].items() if k in cap_ids}
        diffs = []
        for t in TABLES5:
            pk = PK[t]
            cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % t)]
            dbrows = {}
            if order and t == "capacity":
                cap_ids = sorted(snap["capacity"])
                rows = (con.execute("SELECT %s FROM %s WHERE cap_id IN (%s)"
                                    % (",".join(cols), t, ",".join("?" * len(cap_ids))), cap_ids)
                        if cap_ids else ())
            elif order:
                rows = con.execute("SELECT %s FROM %s WHERE order_id=?" % (",".join(cols), t),
                                   (order,))
            else:
                rows = con.execute("SELECT %s FROM %s" % (",".join(cols), t))
            for row in rows:
                d = dict(zip(cols, row))
                dbrows[d[pk]] = d
            for k, want in snap[t].items():
                got = dbrows.pop(k, None)
                if got is None:
                    diffs.append("%s %s 库缺行" % (t, k))
                    continue
                for c in cols:
                    if c == "updated_at":
                        continue
                    if got.get(c) != want.get(c):
                        diffs.append("%s %s.%s 库=%r 卡=%r" % (t, k, c, got.get(c), want.get(c)))
                        if len(diffs) >= limit:
                            break
            for k in dbrows:
                diffs.append("%s %s 卡无此行" % (t, k))
        print("check orders=%d diff=%d" % (len(sel), len(diffs)))
        for d in diffs[:limit]:
            print("  - " + d)
        return 0 if not diffs else 3
    finally:
        con.close()


# ---------- state ----------

def _ledger_state(con, code):
    row = con.execute("SELECT to_state FROM events WHERE order_id=? AND action='transition' "
                      "ORDER BY event_id DESC LIMIT 1", (code,)).fetchone()
    return row[0] if row else ""


def state_rows(state_dir, orders_dir, compare_card=False):
    con = connect(db_path(state_dir), create=False)
    try:
        rows = []
        bad = 0
        cards = list_cards(orders_dir)
        for code, p in cards:
            card = parse_card(p, code, orders_dir)
            raw = card["raw"]
            mapped = card["order"]["status"]
            exact = card["exact"]
            src = card.get("status_src", "head")
            token = head_token(raw)
            ledger = _ledger_state(con, code)
            dbrow = con.execute("SELECT status FROM orders WHERE order_id=?", (code,)).fetchone()
            dbst = dbrow[0] if dbrow else ""
            expect = ledger or mapped
            ok = (dbst == expect)
            verdict = "PASS" if ok else "FAIL"
            if src == "unknown":
                verdict = "PASS(C-TEST非key)" if is_test_card(code) else "FAIL(未知态)"
            if verdict.startswith("FAIL"):
                bad += 1
            rows.append({"order": code, "raw_head": raw, "token": token, "mapped": mapped,
                         "ledger_state": ledger, "db_state": dbst,
                         "diff": "0" if ok else "1",
                         "head_exact_match": "yes" if exact else "no", "verdict": verdict})
        if compare_card:
            # 默认 GBK 控制台遇 ✅/⚠ 会 UnicodeEncodeError（rc=1，误判红灯）。
            # 仅调整 stdout 编码，不改状态规则；StringIO/无 reconfigure 的流跳过。
            if hasattr(sys.stdout, "reconfigure"):
                try:
                    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            print("order | raw_head | token | mapped | ledger_state | db_state | diff | "
                  "head_exact_match | verdict")
            for r in rows:
                print(" | ".join([r["order"], r["raw_head"][:40], r["token"], r["mapped"],
                                  r["ledger_state"], r["db_state"], r["diff"],
                                  r["head_exact_match"], r["verdict"]]))
            print("state orders=%d diff=%d" % (len(rows), bad))
        else:
            for r in rows:
                print("%s\t%s\t%s" % (r["order"], r["db_state"], r["verdict"]))
        return 0 if bad == 0 else 3
    finally:
        con.close()


# ---------- CLI ----------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 卡 §1.2-1 字面：mes_core.py --rebuild / --check；等价映射到子命令（无显式子命令时）
    known = ("rebuild", "check", "state", "events-import")
    if not any(t in known for t in argv):
        for i, t in enumerate(argv):
            if t in ("--rebuild", "--check", "--state", "--events-import"):
                argv[i] = t[2:]
                break
    p = argparse.ArgumentParser(
        prog="mes_core.py", description="C-008 七表派生库",
        epilog="兼容入口：--rebuild 等价 rebuild，--check 等价 check（卡 §1.2-1 字面）")
    p.add_argument("--state-dir", default=os.environ.get("MES_STATE_DIR") or DEFAULT_STATE_DIR)
    p.add_argument("--orders-dir", default=DEFAULT_ORDERS)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("rebuild")
    r.add_argument("--order", default="")
    r.add_argument("--all", action="store_true")
    r.add_argument("--force", action="store_true")
    r.add_argument("--state-dir", default=argparse.SUPPRESS)
    r.add_argument("--orders-dir", default=argparse.SUPPRESS)
    r.set_defaults(func=lambda a: (rebuild(a.state_dir, a.orders_dir, a.order or None, a.force), 0)[1])

    c = sub.add_parser("check")
    c.add_argument("--order", default="")
    c.add_argument("--all", action="store_true")
    c.add_argument("--state-dir", default=argparse.SUPPRESS)
    c.add_argument("--orders-dir", default=argparse.SUPPRESS)
    c.set_defaults(func=lambda a: check(a.state_dir, a.orders_dir, a.order or None))

    s = sub.add_parser("state")
    s.add_argument("--order", default="")
    s.add_argument("--all", action="store_true")
    s.add_argument("--compare-card", action="store_true")
    s.add_argument("--state-dir", default=argparse.SUPPRESS)
    s.add_argument("--orders-dir", default=argparse.SUPPRESS)
    s.set_defaults(func=lambda a: state_rows(a.state_dir, a.orders_dir, a.compare_card))

    e = sub.add_parser("events-import")
    e.add_argument("--state-dir", default=argparse.SUPPRESS)
    e.set_defaults(func=lambda a: _cmd_events_import(a.state_dir))

    a = p.parse_args(argv)
    return a.func(a)


def _cmd_events_import(state_dir):
    con = connect(db_path(state_dir))
    try:
        init_db(con)
        n = import_events(con, state_dir)
        con.commit()
        print("events_total=%d" % con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return 0 if n >= 0 else 3
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
