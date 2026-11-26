#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mes.py —— 订单状态机 + MES 强制写通道（stdlib only）· C-010 T01 / C-008 R1.1

职责：
  1. repair-open  原单 returned → 立返修单（D 号 + 退回原因），**原单状态不变**，只追加一条事件；
  2. transition   单张订单的合法边推进（16 态字典口径），写 append-only 台账；
  3. check        逐单重放 from→to 链，断点/缺号/重复 event_id 计入 diff；
  4. claim/start/report/sign  报工四动作（F10）：谁的牌、哪个节点、什么时刻，一行不漏；
  5. check-ledger 卡头 mapped 态 ↔ 台账迁移态对账，差异 = I 级 + alert（rc 3）。

台账：<state-dir>/mes/events.jsonl，schema 见 C-010 plan §2.2（冻结，C-008 可导入）：
  {"event_id","seq","ts","actor","action","order","from","to","ref_order","d_no","reason","note","idem_key"}

纪律：
  - 台账只追加，不删不改；同 idem_key + 同 actor 重放 → REPLAY（不追加、exit 0）；
    同 idem_key + 不同 actor → exit 3（防跨席顶替）。
  - returned 的出边一律 exit 3：出路只有 repair-open（原单终态不变）。
  - 工牌校验：actor 必须在 registry badges 段在册且 status=active（经理 seats 行按 05 §一4 同等视作工牌），
    且部门 == 目标态 owner 部门；两段皆无 → exit 3 并说明，绝不默认放行。
  - 本模块不碰 8900、不碰 mes_board.py、不写订单卡。
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
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(HERE, "..", "state"))
DEFAULT_REGISTRY = os.path.join(DEFAULT_STATE_DIR, "seat_registry.json")
DEFAULT_ORDERS = os.path.normpath(os.path.join(HERE, "..", "..", "..", "dll", "factory-blueprint", "orders"))

# 16 态合法出边（returned 不在任何 to 位置；blocked/suspended 可回队）
LEGAL_EDGES = {
    "created": ("queued", "blocked", "suspended"),
    "queued": ("planning", "blocked", "suspended"),
    "planning": ("contract-review", "auditing", "producing", "blocked", "suspended"),
    "contract-review": ("auditing", "planning", "blocked", "suspended"),
    "auditing": ("awaiting-release", "planning", "blocked"),
    "awaiting-release": ("producing", "planning", "blocked"),
    "producing": ("qc", "blocked", "suspended"),
    "qc": ("awaiting-closure", "producing", "blocked"),
    "awaiting-closure": ("delivering", "producing", "blocked"),
    "delivering": ("delivered", "blocked"),
    "delivered": ("accepted", "blocked"),
    "accepted": (),
    "returned": (),  # 出路只有 repair-open
    "blocked": ("queued", "planning", "producing", "suspended"),
    "suspended": ("queued", "planning", "producing"),
    "compensating": ("delivering", "delivered"),
}

# 状态 owner 部门（与 mes_orders.STATES 的 owner 列同源：00 §二）
DEPT_OF_STATE = {
    "created": ("A",),
    "queued": ("B",),
    "planning": ("B",),
    "contract-review": ("A",),
    "auditing": ("C",),
    "awaiting-release": ("A",),
    "producing": ("D",),
    "qc": ("E",),
    "awaiting-closure": ("A",),
    "delivering": ("A",),
    "delivered": ("A",),
    "accepted": (),   # 发起人，无工牌，transition 不可达
    "returned": (),   # 同上
    "blocked": ("A", "B", "D", "E", "F"),
    "suspended": ("F",),
    "compensating": ("F",),
}


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _die(msg, code=3):
    sys.stderr.write("[FAIL] %s\n" % msg)
    sys.exit(code)


def events_path(state_dir):
    return os.path.join(state_dir, "mes", "events.jsonl")


def load_events(state_dir):
    p = events_path(state_dir)
    out = []
    if not os.path.exists(p):
        return out
    with open(p, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                _die("台账坏行 %s:%d 不是合法 JSON" % (p, ln))
    return out


def append_event(state_dir, ev):
    p = events_path(state_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")


def make_event(ev_list, order, action, actor, frm, to, seq=None,
               ref_order="", d_no="", reason="", note="", idem_key="", ts=None):
    ts = ts or _now()
    if seq is None:
        seq = 1 + max([e.get("seq", 0) for e in ev_list if e.get("order") == order] or [0])
    raw = "%s|%s|%s|%s|%s" % (order, action, ts, actor, seq)
    return {
        "event_id": hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16],
        "seq": seq, "ts": ts, "actor": actor, "action": action,
        "order": order, "from": frm, "to": to,
        "ref_order": ref_order, "d_no": d_no, "reason": reason,
        "note": note, "idem_key": idem_key,
    }


def card_state(orders_dir, code):
    """台账无记录时以卡头状态为前置（真实卡面 `returned` 是发起人退回的事实载体）。"""
    try:
        sys.path.insert(0, HERE)
        import mes_core
        path = mes_core.order_card_path(code, orders_dir)   # 2026-11-24：归档单兜底
    except Exception:
        path = os.path.join(orders_dir, code, "order.md")
    if not os.path.exists(path):
        return ""
    try:
        sys.path.insert(0, HERE)
        import mes_orders  # 同目录，只读复用
        return mes_orders.derive_state(mes_orders.parse_order(path)).get("state") or ""
    except Exception:
        return ""


def load_badges(registry_path):
    """返回 (ok, badges dict 或 原因)。badges 段 + seats 段合并（05 §一4：6 席注册行即工牌行）。"""
    if not os.path.exists(registry_path):
        return False, "工牌注册表不存在：%s" % registry_path
    with open(registry_path, "r", encoding="utf-8") as f:
        try:
            reg = json.load(f)
        except ValueError as e:
            return False, "工牌注册表非法 JSON：%s" % e
    raw = reg.get("badges")
    if isinstance(raw, list):
        badges = {b.get("id") or b.get("code") or b.get("badge"): b
                  for b in raw if isinstance(b, dict)}
    elif isinstance(raw, dict):
        badges = dict(raw)
    else:
        badges = {}
    for s in (reg.get("seats") or []):
        if not isinstance(s, dict):
            continue
        sess = s.get("session") or s.get("session_id") or ""
        if not sess.startswith("sess_"):
            continue
        badges.setdefault(sess, {
            "badge": sess, "seat": s.get("seat") or "", "dept": s.get("dept") or "",
            "employee": s.get("employee") or s.get("seat") or "",
            "status": s.get("status") or "active", "source": "seats",
        })
    if not badges:
        return False, "badges 段未建且 seats 段无可视会话（MES/05 发牌未做），无法验牌"
    return True, badges


def check_badge(registry_path, actor, target_state):
    ok, badges = load_badges(registry_path)
    if not ok:
        _die(badges)
    b = badges.get(actor)
    if not b:
        _die("badge=%s 不在册" % actor)
    if (b.get("status") or "").lower() != "active":
        _die("badge=%s 状态非 active（%s）" % (actor, b.get("status")))
    want = DEPT_OF_STATE.get(target_state, ())
    if want and (b.get("dept") or "").upper() not in want:
        _die("edge 部门不符：badge=%s dept=%s，目标态 %s 需 dept∈%s"
             % (actor, b.get("dept"), target_state, "/".join(want)))
    return b


def find_replay(ev_list, idem_key, actor):
    if not idem_key:
        return None
    for e in ev_list:
        if e.get("idem_key") == idem_key:
            if e.get("actor") != actor:
                _die("idem_key=%s 已被 actor=%s 使用，禁止跨席顶替" % (idem_key, e.get("actor")))
            return e
    return None


def cmd_repair_open(a):
    ev_list = load_events(a.state_dir)
    rep = find_replay(ev_list, a.idem_key, a.actor)
    if rep:
        print("REPLAY %s" % rep["event_id"])
        return 0
    if not a.d:
        _die("repair-open 缺 D 号（--d）")
    if not (a.reason or "").strip():
        _die("repair-open 缺退回原因（--reason）")
    if a.to_order == a.order:
        _die("返修单号不得等于原单号")
    # 原单必须处于 returned（以台账重放态为准；台账无记录则视为不合法前提）
    state = replay_state(ev_list, a.order) or card_state(a.orders_dir, a.order)
    if state != "returned":
        _die("原单 %s 状态=%s（台账优先、卡头兜底），非 returned，不得 repair-open"
             % (a.order, state or "未知"))
    for e in ev_list:
        if e.get("action") == "repair-open" and e.get("order") == a.order:
            _die("原单 %s 已有返修单 %s（%s），禁止二次 repair-open"
                 % (a.order, e.get("ref_order"), e.get("ts")))
    check_badge(a.registry, a.actor, "created")
    ev = make_event(ev_list, a.order, "repair-open", a.actor, "returned", "returned",
                    ref_order=a.to_order, d_no=a.d, reason=a.reason,
                    note=a.note, idem_key=a.idem_key)
    append_event(a.state_dir, ev)
    print("OK repair-open %s -> %s (%s) event=%s" % (a.order, a.to_order, a.d, ev["event_id"]))
    return 0


def replay_state(ev_list, order):
    """台账迁移态：只看 transition（claim/start/report/sign/repair-open 不改订单态）。"""
    st = None
    for e in sorted([x for x in ev_list if x.get("order") == order], key=lambda x: x.get("seq", 0)):
        if e.get("action") != "transition":
            continue
        st = e.get("to")
    return st


def cmd_transition(a):
    ev_list = load_events(a.state_dir)
    rep = find_replay(ev_list, a.idem_key, a.actor)
    if rep:
        print("REPLAY %s" % rep["event_id"])
        return 0
    if a.to == "returned":
        _die("edge 非法：returned 无入边（发起人退回走 repair-open 反向记录，不由 transition 推进）")
    cur = replay_state(ev_list, a.order) or card_state(a.orders_dir, a.order) or "created"
    frm = a.frm or cur
    if a.frm and a.frm != cur:
        _die("from 与台账不符：台账=%s，命令=%s" % (cur, a.frm))
    if a.to not in LEGAL_EDGES.get(frm, ()):
        _die("edge 非法：%s -> %s（合法出边：%s）"
             % (frm, a.to, ", ".join(LEGAL_EDGES.get(frm, ())) or "无"))
    check_badge(a.registry, a.actor, a.to)
    ev = make_event(ev_list, a.order, "transition", a.actor, frm, a.to,
                    note=a.note, idem_key=a.idem_key)
    append_event(a.state_dir, ev)
    print("OK transition %s %s->%s event=%s" % (a.order, frm, a.to, ev["event_id"]))
    return 0


def cmd_check(a):
    ev_list = load_events(a.state_dir)
    orders = sorted({e.get("order") for e in ev_list if e.get("order")})
    if a.order:
        orders = [o for o in orders if o == a.order]
    diffs = []
    seen = {}
    for e in ev_list:
        eid = e.get("event_id")
        if eid in seen:
            diffs.append("重复 event_id=%s（seq %s/%s）" % (eid, seen[eid], e.get("seq")))
        seen[eid] = e.get("seq")
    for o in orders:
        evs = sorted([e for e in ev_list if e.get("order") == o], key=lambda x: x.get("seq", 0))
        seqs = [e.get("seq") for e in evs]
        if seqs != list(range(1, len(seqs) + 1)):
            diffs.append("%s 序号断档：%s" % (o, seqs))
        prev = None
        for e in evs:
            if e.get("action") == "repair-open":
                if not (e.get("ref_order") and e.get("d_no") and e.get("reason")):
                    diffs.append("%s repair-open 字段缺失（ref_order/d_no/reason）" % o)
                continue
            if e.get("action") != "transition":
                continue
            if prev is None:
                if e.get("from") != "created":
                    diffs.append("%s 首条 transition from=%s（应为 created）" % (o, e.get("from")))
            elif e.get("from") != prev:
                diffs.append("%s 链断点：seq=%s from=%s 上一条 to=%s"
                             % (o, e.get("seq"), e.get("from"), prev))
            if e.get("to") not in LEGAL_EDGES.get(e.get("from"), ()):
                diffs.append("%s seq=%s 非法边 %s->%s" % (o, e.get("seq"), e.get("from"), e.get("to")))
            prev = e.get("to")
    print("orders=%d events=%d diff=%d" % (len(orders), len(ev_list), len(diffs)))
    for d in diffs:
        print("  - " + d)
    return 0 if not diffs else 3


# ---------- F10 报工四动作 + 对账 ----------

NODE_ACTIONS = ("claim", "start", "report", "sign")
SIG_KEYS = ("订单", "任务", "工号", "角色", "结论", "时间", "签字")


def _badge_of(registry_path, actor):
    ok, badges = load_badges(registry_path)
    if not ok:
        _die(badges)
    b = badges.get(actor)
    if not b:
        _die("badge=%s 不在册" % actor)
    if (b.get("status") or "").lower() != "active":
        _die("badge=%s 状态非 active（%s）" % (actor, b.get("status")))
    return b


def _node_hits(order, ref, orders_dir):
    sys.path.insert(0, HERE)
    import mes_core
    p = mes_core.order_card_path(order, orders_dir)   # 2026-11-24：归档单兜底
    if not os.path.isfile(p):
        _die("订单卡不存在：%s" % p)
    nodes = mes_core.parse_card(p, order, orders_dir)["nodes"]
    if "#" in ref:
        hits = [n for n in nodes if n["node_id"] == ref]
    else:
        hits = [n for n in nodes if (n.get("node_code") or "").upper() == ref.strip().upper()]
    if not hits:
        _die("节点 %s 不在 %s 卡内（可解析 %d 节点）" % (ref, order, len(nodes)))
    return hits


def _node_for(order, ref, employee, orders_dir):
    hits = _node_hits(order, ref, orders_dir)
    if employee:
        hit = [n for n in hits if (n.get("employee_id") or "") == employee]
        if len(hit) == 1:
            return hit[0]
    if len(hits) == 1:
        return hits[0]
    _die("节点 %s 不唯一（%d 行），请用完整 node_id（%s）"
         % (ref, len(hits), ", ".join(n["node_id"] for n in hits)))


def _act_events(ev_list, order, node_id, action=None):
    out = []
    for e in ev_list:
        if e.get("order") != order or e.get("action") not in NODE_ACTIONS:
            continue
        if (e.get("note") or "").split(";")[0] != "node=%s" % node_id:
            continue
        if action and e.get("action") != action:
            continue
        out.append(e)
    return out


def _require_node_badge(a, node, action):
    b = _badge_of(a.registry, a.actor)
    emp = node.get("employee_id") or ""
    if not emp:
        _die("节点 %s 无执行员工（经理节点禁 %s）" % (node["node_id"], action))
    if (b.get("employee") or "") != emp:
        _die("挂名/代工：badge employee=%s ≠ node employee=%s" % (b.get("employee"), emp))
    if node.get("dept") and (b.get("dept") or "").upper() != node["dept"]:
        _die("部门不符：badge dept=%s，节点 %s 需 dept=%s"
             % (b.get("dept"), node["node_id"], node["dept"]))
    return b


def cmd_claim(a):
    ev_list = load_events(a.state_dir)
    rep = find_replay(ev_list, a.idem_key, a.actor)
    if rep:
        print("REPLAY %s" % rep["event_id"])
        return 0
    b = _badge_of(a.registry, a.actor)
    node = _node_for(a.order, a.node, b.get("employee") or "", a.orders_dir)
    _require_node_badge(a, node, "claim")
    prev = _act_events(ev_list, a.order, node["node_id"], "claim")
    if prev:
        if prev[0].get("actor") != a.actor:
            _die("节点 %s 已被 %s claim（%s）" % (node["node_id"], prev[0].get("actor"), prev[0].get("ts")))
        print("REPLAY %s" % prev[0]["event_id"])
        return 0
    cur = replay_state(ev_list, a.order) or card_state(a.orders_dir, a.order) or "created"
    ev = make_event(ev_list, a.order, "claim", a.actor, cur, cur,
                    note="node=%s" % node["node_id"], idem_key=a.idem_key)
    append_event(a.state_dir, ev)
    print("OK claim %s %s event=%s" % (a.order, node["node_id"], ev["event_id"]))
    return 0


def cmd_start(a):
    ev_list = load_events(a.state_dir)
    rep = find_replay(ev_list, a.idem_key, a.actor)
    if rep:
        print("REPLAY %s" % rep["event_id"])
        return 0
    b = _badge_of(a.registry, a.actor)
    node = _node_for(a.order, a.node, b.get("employee") or "", a.orders_dir)
    _require_node_badge(a, node, "start")
    claims = _act_events(ev_list, a.order, node["node_id"], "claim")
    if not claims or claims[0].get("actor") != a.actor:
        _die("节点 %s 未由当前 actor claim，先 claim 再 start" % node["node_id"])
    pre = _act_events(ev_list, a.order, node["node_id"], "start")
    if pre:
        if pre[0].get("actor") != a.actor:
            _die("节点 %s 已被 %s start" % (node["node_id"], pre[0].get("actor")))
        print("REPLAY %s" % pre[0]["event_id"])
        return 0
    cur = replay_state(ev_list, a.order) or card_state(a.orders_dir, a.order) or "created"
    ev = make_event(ev_list, a.order, "start", a.actor, cur, cur,
                    note="node=%s" % node["node_id"], idem_key=a.idem_key)
    append_event(a.state_dir, ev)
    print("OK start %s %s event=%s" % (a.order, node["node_id"], ev["event_id"]))
    return 0


def cmd_report(a):
    ev_list = load_events(a.state_dir)
    rep = find_replay(ev_list, a.idem_key, a.actor)
    if rep:
        print("REPLAY %s" % rep["event_id"])
        return 0
    b = _badge_of(a.registry, a.actor)
    node = _node_for(a.order, a.node, b.get("employee") or "", a.orders_dir)
    _require_node_badge(a, node, "report")
    starts = _act_events(ev_list, a.order, node["node_id"], "start")
    if not starts or starts[0].get("actor") != a.actor:
        _die("节点 %s 未由当前 actor start，先 start 再 report" % node["node_id"])
    ev_path = os.path.abspath(a.evidence)
    if not os.path.isfile(ev_path) or os.path.getsize(ev_path) <= 0:
        _die("证据路径不存在或为空：%s" % a.evidence)
    pre = _act_events(ev_list, a.order, node["node_id"], "report")
    if pre:
        if pre[0].get("actor") != a.actor:
            _die("节点 %s 已被 %s report" % (node["node_id"], pre[0].get("actor")))
        if ("evidence=%s" % ev_path) in (pre[0].get("note") or ""):
            print("REPLAY %s" % pre[0]["event_id"])
            return 0
        _die("节点 %s 已有不同证据的 report，禁止覆盖" % node["node_id"])
    cur = replay_state(ev_list, a.order) or card_state(a.orders_dir, a.order) or "created"
    ev = make_event(ev_list, a.order, "report", a.actor, cur, cur,
                    note="node=%s;evidence=%s" % (node["node_id"], ev_path),
                    idem_key=a.idem_key)
    append_event(a.state_dir, ev)
    print("OK report %s %s event=%s evidence=%s" % (a.order, node["node_id"], ev["event_id"], ev_path))
    return 0


def _sig_field(text, key):
    m = re.search(r"(?m)^\s*-\s*%s\s*[：:]\s*(.+?)\s*$" % re.escape(key), text)
    return m.group(1).strip() if m else ""


def _sig_time(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _file_birth(path):
    st = os.stat(path)
    ts = getattr(st, "st_birthtime", None)
    return datetime.datetime.fromtimestamp(ts) if ts else None


def cmd_sign(a):
    ev_list = load_events(a.state_dir)
    rep = find_replay(ev_list, a.idem_key, a.actor)
    if rep:
        print("REPLAY %s" % rep["event_id"])
        return 0
    if not os.path.isfile(a.sig_file):
        _die("签字文件不存在：%s" % a.sig_file)
    with open(a.sig_file, "r", encoding="utf-8") as f:
        text = f.read()
    b = _badge_of(a.registry, a.actor)
    if (b.get("employee") or "") != a.employee:
        _die("代签：badge employee=%s ≠ --employee=%s" % (b.get("employee"), a.employee))
    node = _node_for(a.order, a.node, a.employee, a.orders_dir)
    _require_node_badge(a, node, "sign")
    if (node.get("employee_id") or "") != a.employee:
        _die("挂名：节点 %s 员工=%s ≠ --employee=%s"
             % (node["node_id"], node.get("employee_id"), a.employee))
    starts = _act_events(ev_list, a.order, node["node_id"], "start")
    if not starts or starts[0].get("actor") != a.actor:
        _die("节点 %s 未由当前 actor start，先 start 再 sign" % node["node_id"])
    f_order = _sig_field(text, "订单")
    f_emp = _sig_field(text, "工号")
    f_time = _sig_field(text, "时间")
    f_sign = _sig_field(text, "签字")
    miss = [k for k, v in (("订单", f_order), ("工号", f_emp), ("时间", f_time), ("签字", f_sign)) if not v]
    if miss:
        _die("签字文件缺字段：%s" % ",".join(miss))
    if f_order != a.order:
        _die("签字文件订单=%s ≠ --order=%s" % (f_order, a.order))
    if f_emp != a.employee:
        _die("挂名：签字文件工号=%s ≠ --employee=%s" % (f_emp, a.employee))
    if f_sign != a.employee:
        _die("代签：签字字段=%s ≠ --employee=%s" % (f_sign, a.employee))
    st = _sig_time(f_time)
    if not st:
        _die("签字时间不可解析：%s" % f_time)
    start_dt = _sig_time((starts[0].get("ts") or "").replace("T", " "))
    if start_dt and st < start_dt:
        _die("倒签：签字时间 %s 早于 start %s" % (f_time, starts[0].get("ts")))
    now = datetime.datetime.now()
    if st > now + datetime.timedelta(seconds=10):
        _die("未来时间：签字时间 %s 晚于本机 %s" % (f_time, now.strftime("%Y-%m-%d %H:%M:%S")))
    birth = _file_birth(a.sig_file)
    if birth and abs((birth - st).total_seconds()) > 10:
        _die("倒签：文件生成 %s 与文内时间 %s 相差 >10s"
             % (birth.strftime("%Y-%m-%d %H:%M:%S"), f_time))
    sha = hashlib.sha256(open(a.sig_file, "rb").read()).hexdigest()
    cur = replay_state(ev_list, a.order) or card_state(a.orders_dir, a.order) or "created"
    ev = make_event(ev_list, a.order, "sign", a.actor, cur, cur,
                    note="node=%s;sig=%s;file=%s" % (node["node_id"], sha[:16], os.path.abspath(a.sig_file)),
                    idem_key=a.idem_key)
    append_event(a.state_dir, ev)
    print("OK sign %s %s event=%s sig=%s" % (a.order, node["node_id"], ev["event_id"], sha[:16]))
    return 0


def _alert_ledger(state_dir, code, detail):
    db = os.path.join(state_dir, "mes", "mes.sqlite")
    if not os.path.isfile(db):
        return False
    aid = "ledger#%s#%s" % (code, datetime.date.today().isoformat())
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT OR IGNORE INTO alerts (alert_id,ts,order_id,node_id,level,kind,detail,source,status,closed_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, _now().replace("T", " "), code, "", "红", "对账", detail,
             "mes.check-ledger", "open", ""))
        con.commit()
    finally:
        con.close()
    return True


def _set_ledger_ok(state_dir, code, ok):
    """对账结果回写 orders.ledger_ok（派生表；库不存在则跳过）。"""
    db = os.path.join(state_dir, "mes", "mes.sqlite")
    if not os.path.isfile(db):
        return
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE orders SET ledger_ok=? WHERE order_id=?", (1 if ok else 0, code))
        con.commit()
    finally:
        con.close()


def cmd_check_ledger(a):
    sys.path.insert(0, HERE)
    import mes_core
    ev_list = load_events(a.state_dir)
    cards = mes_core.list_cards(a.orders_dir)
    if a.order:
        cards = [(c, p) for c, p in cards if c == a.order]
    rows, diffs, skipped = [], [], 0
    for code, p in cards:
        card = mes_core.parse_card(p, code, a.orders_dir)
        mapped, exact, token = mes_core.map_state(card["raw"])
        ledger = replay_state(ev_list, code)
        if not ledger:
            skipped += 1
            continue
        ok = (ledger == mapped)
        rows.append((code, mapped, ledger, token, "PASS" if ok else "FAIL"))
        _set_ledger_ok(a.state_dir, code, ok)
        if not ok:
            diffs.append("%s 台账=%s 卡头=%s（token=%s）" % (code, ledger, mapped, token))
            _alert_ledger(a.state_dir, code, "I 级对账差异：台账=%s 卡头=%s" % (ledger, mapped))
    print("order | card_mapped | ledger_state | token | verdict")
    for r in rows:
        print(" | ".join(r))
    print("check-ledger orders=%d skipped(无台账)=%d diff=%d" % (len(rows), skipped, len(diffs)))
    for d in diffs:
        print("  [I] " + d)
    return 0 if not diffs else 3


def main(argv=None):
    p = argparse.ArgumentParser(prog="mes.py", description="订单状态机最小实现（C-010 T01）")
    p.add_argument("--state-dir", default=os.environ.get("MES_STATE_DIR") or DEFAULT_STATE_DIR)
    p.add_argument("--registry", default=DEFAULT_REGISTRY)
    p.add_argument("--orders-dir", default=DEFAULT_ORDERS)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("repair-open", help="原单 returned → 立返修单")
    r.add_argument("--order", required=True)
    r.add_argument("--to-order", required=True)
    r.add_argument("--d", required=True)
    r.add_argument("--reason", required=True)
    r.add_argument("--actor", required=True)
    r.add_argument("--note", default="")
    r.add_argument("--idem-key", default="")
    r.add_argument("--state-dir", default=argparse.SUPPRESS)
    r.add_argument("--registry", default=argparse.SUPPRESS)
    r.add_argument("--orders-dir", default=argparse.SUPPRESS)
    r.set_defaults(func=cmd_repair_open)

    t = sub.add_parser("transition", help="合法边推进")
    t.add_argument("--order", required=True)
    t.add_argument("--to", required=True)
    t.add_argument("--from", dest="frm", default="")
    t.add_argument("--actor", required=True)
    t.add_argument("--note", default="")
    t.add_argument("--idem-key", default="")
    t.add_argument("--state-dir", default=argparse.SUPPRESS)
    t.add_argument("--registry", default=argparse.SUPPRESS)
    t.add_argument("--orders-dir", default=argparse.SUPPRESS)
    t.set_defaults(func=cmd_transition)

    c = sub.add_parser("check", help="台账重放核验")
    c.add_argument("--order", default="")
    c.add_argument("--state-dir", default=argparse.SUPPRESS)
    c.add_argument("--registry", default=argparse.SUPPRESS)
    c.add_argument("--orders-dir", default=argparse.SUPPRESS)
    c.set_defaults(func=cmd_check)

    for name, fn, hlp in (("claim", cmd_claim, "接单（F10 四动作之一）"),
                          ("start", cmd_start, "开工（F10 四动作之一）")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--order", required=True)
        sp.add_argument("--node", required=True, help="节点码（如 R1）或完整 node_id（order#seq）")
        sp.add_argument("--actor", required=True, help="工牌=完整会话 ID（sess_…）")
        sp.add_argument("--idem-key", default="")
        sp.add_argument("--state-dir", default=argparse.SUPPRESS)
        sp.add_argument("--registry", default=argparse.SUPPRESS)
        sp.add_argument("--orders-dir", default=argparse.SUPPRESS)
        sp.set_defaults(func=fn)

    rp = sub.add_parser("report", help="报工 + 证据路径（F10 四动作之一）")
    rp.add_argument("--order", required=True)
    rp.add_argument("--node", required=True)
    rp.add_argument("--actor", required=True)
    rp.add_argument("--evidence", required=True)
    rp.add_argument("--idem-key", default="")
    rp.add_argument("--state-dir", default=argparse.SUPPRESS)
    rp.add_argument("--registry", default=argparse.SUPPRESS)
    rp.add_argument("--orders-dir", default=argparse.SUPPRESS)
    rp.set_defaults(func=cmd_report)

    sg = sub.add_parser("sign", help="签字核验（代签/挂名/倒签全拦）")
    sg.add_argument("--order", required=True)
    sg.add_argument("--node", required=True)
    sg.add_argument("--employee", required=True)
    sg.add_argument("--sig-file", required=True)
    sg.add_argument("--actor", required=True)
    sg.add_argument("--idem-key", default="")
    sg.add_argument("--state-dir", default=argparse.SUPPRESS)
    sg.add_argument("--registry", default=argparse.SUPPRESS)
    sg.add_argument("--orders-dir", default=argparse.SUPPRESS)
    sg.set_defaults(func=cmd_sign)

    cl = sub.add_parser("check-ledger", help="卡头 mapped 态 ↔ 台账迁移态对账（差异 I 级 + rc 3）")
    cl.add_argument("--order", default="")
    cl.add_argument("--all", action="store_true")
    cl.add_argument("--state-dir", default=argparse.SUPPRESS)
    cl.add_argument("--registry", default=argparse.SUPPRESS)
    cl.add_argument("--orders-dir", default=argparse.SUPPRESS)
    cl.set_defaults(func=cmd_check_ledger)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
