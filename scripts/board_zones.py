#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""board_zones.py —— C-20261120-009 T01 七区真数据层（只读聚合，零硬编码数据）

契约 c009-zones-v1（B-001 plan 18:25 冻结；派单契约字段名不改，只增）：
  collect_zones() -> {"contract":"c009-zones-v1","generated_at":ISO,
                      "zones":{orders,seats,events,capacity,alerts,cost,handover},
                      "zone_status":{7 keys},"sources":{7 keys}}
  collect_v2()    -> 派单 v2 外形：{"generated_at","meta","zones","recon"}

纪律：
- 只读正本（orders/*/order.md · state/* · ~/.zcode/cli/db/db.sqlite · reports/*），
  不写任何生产数据；写测试只经 MES_ORDERS_DIR 指沙箱副本。
- 每个 zone 必须带 source（字符串，派单契约）+ sources（数组，plan §1）+ status + note。
- 缺源 -> status=missing/degraded + 中文 note；**禁止补 0、禁止假数组**。
- 所有可注入根（state/reports/DB/orders/shift）走 env，便于单测缺源降级。
"""
import argparse
import datetime
import glob
import io
import json
import os
import re
import sqlite3
import sys

import mes_orders

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = "c009-zones-v1"
# 在制 = 非下列终态；void/failed 是卡头真实用词（C-TEST-02/03），同样不算在制
TOKEN_TERMINAL = ("delivered", "accepted", "returned", "void", "failed")
PROVIDER_LANE = {  # 来源：排班单 SHIFT-20261120-N「模型分配」段（B-001=new-provider-6→R4 / B-002=new-provider-11→R4-2）
    "new-provider-6": "R4",
    "new-provider-11": "R4-2",
    "account:bigmodel-start-plan": "GLM",
}
TERMINAL_CN = {"已交付待验收", "验收接受", "验收退回"}


def now():
    return datetime.datetime.now()


def iso(dt=None):
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def state_dir():
    return os.environ.get("MES_STATE_DIR") or os.path.join(ROOT, "state")


def reports_dir():
    return os.environ.get("MES_REPORTS_DIR") or os.path.join(ROOT, "reports")


def cli_db():
    # P0 fix (ops-04 21:40): candidate chain - SYSTEM context expanduser points to systemprofile (no .zcode)
    # T05: 显式注入优先——MES_CLI_DB 指不存在文件时保持该路径（缺源=null），不回落真库，单测隔离依赖此语义
    env = os.environ.get("MES_CLI_DB")
    if env:
        return env
    cands = [os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite"),
             r"C:\Users\Administrator\.zcode\cli\db\db.sqlite"]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return cands[-1]


def read_text(path):
    try:
        return io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def read_json(path, default=None):
    try:
        return json.load(io.open(path, encoding="utf-8"))
    except Exception:
        return default


def _cells(line):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return []
    return [c.strip() for c in s.strip("|").split("|")]


def parse_header(text):
    """卡头两列表：首个表格 | key | value |（跳过表头分隔行）。"""
    out = {}
    for line in text.splitlines():
        cs = _cells(line)
        if len(cs) < 2:
            if out and line.strip() and not line.strip().startswith("|"):
                break
            continue
        k = re.sub(r"\*\*|`", "", cs[0]).strip()
        v = cs[1].strip()
        if not k or set(k) <= {"-", ":", " "} or not k:
            continue
        if k in ("订单编号", "下单时间", "发起人", "优先级", "预期交付", "风险评级", "预算", "排班", "当前状态", "进度", "交付截止"):
            out[k] = v
        if "## 1" in line:
            break
    return out


def header_state(hdr):
    """卡头「当前状态」的反引号状态词（原词，未做 16 态映射）。"""
    m = re.search(r"`([A-Za-z][A-Za-z0-9\-]*)`", (hdr or {}).get("当前状态", ""))
    return (m.group(1).lower() if m else "")


def parse_flow_rows(text):
    """§2.1 编成流转表（### 2.1 起至下一个 ###/##）：返回 dict 行。"""
    rows, on, hdr = [], False, False
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^#{2,3}\s+2\.1", s):
            on = True
            continue
        if on and re.match(r"^#{2,3}\s", s):
            break
        if not on:
            continue
        cs = _cells(line)
        if len(cs) < 4:
            continue
        if not hdr:
            hdr = True
            continue
        rows.append(cs)
    return rows


def parse_section_rows(text, num):
    """取 `## <num>` 到下一个 `## ` 之间的表格数据行（跳过表头/分隔行）。"""
    body = []
    on = False
    for line in text.splitlines():
        if re.match(r"^##\s+%s(\D|$)" % num, line.strip()):
            on = True
            continue
        if on and re.match(r"^##\s+\d", line.strip()):
            break
        if on:
            body.append(line)
    rows, header_seen = [], False
    for line in body:
        cs = _cells(line)
        if len(cs) < 2:
            continue
        if not header_seen:
            header_seen = True
            continue
        if all(set(c) <= {"-", ":", " "} or not c for c in cs):
            continue
        rows.append(cs)
    return rows


def parse_dt(s):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", s or "")
    if not m:
        return None
    try:
        return datetime.datetime(*[int(x) for x in m.groups()])
    except ValueError:
        return None


def order_card_info(path):
    """单卡附加字段：交付截止/下单时间/延期说明/提货清单/§2.1 会话/当前节点。"""
    text = read_text(path)
    hdr = parse_header(text)
    dl_raw = re.sub(r"\*|`", "", hdr.get("交付截止", "")) or hdr.get("交付截止", "")
    deadline = parse_dt(dl_raw)
    created = parse_dt(hdr.get("下单时间", ""))
    delay_rows = [r for r in parse_section_rows(text, 10) if any(c.strip() not in ("", "—", "-", "（暂无）") for c in r[1:])]
    delay_note = ""
    if delay_rows:
        delay_note = re.sub(r"\s+", " ", " | ".join(delay_rows[0])).strip()[:120]
    pick = [r for r in parse_section_rows(text, 11) if len(r) >= 3]
    pick_total = len(pick)
    pick_done = sum(1 for r in pick if "✅" in " ".join(r))
    flow_sess, flow_emp = set(), {}
    for r in parse_flow_rows(text):
        emp = r[3].strip() if len(r) > 3 else ""
        if not emp or emp in ("—", "-"):
            emp = r[2].strip() if len(r) > 2 else ""
        for cell in r:
            for s8 in re.findall(r"sess_[0-9a-f]{8}", cell):
                flow_sess.add(s8)
                if emp:
                    flow_emp.setdefault(s8, emp)
    return {"header": hdr, "deadline": deadline, "created": created,
            "delay_note": delay_note, "pick_total": pick_total, "pick_done": pick_done,
            "flow_sess": flow_sess, "flow_emp": flow_emp, "state_raw": header_state(hdr),
            "deadline_txt": (hdr.get("交付截止", "") or hdr.get("预期交付", "") or "—")}


def _node(tasks):
    """当前 T 节点 = §9 首个未完成行（全部完成取末行）；无 §9 行 -> None。"""
    if not tasks:
        return None
    for t in tasks:
        if not t.get("done"):
            return {"t": t.get("task", ""), "seat": t.get("seat", ""), "emp": t.get("emp", ""),
                    "sess": t.get("sess", ""), "status": t.get("status", ""),
                    "sign": t.get("sign", ""), "done": False}
    t = tasks[-1]
    return {"t": t.get("task", ""), "seat": t.get("seat", ""), "emp": t.get("emp", ""),
            "sess": t.get("sess", ""), "status": t.get("status", ""),
            "sign": t.get("sign", ""), "done": True}


def _sla(created, deadline, state):
    if not deadline:
        return None, False
    n = now()
    overdue = n > deadline and state not in TOKEN_TERMINAL
    ratio = None
    if created and deadline > created:
        ratio = round((n - created).total_seconds() / (deadline - created).total_seconds(), 2)
    return ratio, overdue


def zone_orders():
    base = mes_orders.orders_dir()
    all_ = mes_orders.load_all()
    zone = {"status": "ok" if not all_.get("error") else "missing",
            "as_of": iso(), "source": "orders/*/order.md（%s）" % base,
            "sources": [base, "mes_orders.load_all（16 态派生）"],
            "summary": {}, "items": []}
    if all_.get("error"):
        zone["note"] = "订单源不可读：%s" % all_["error"]
        zone["summary"] = {"orders": 0, "wip": 0}
        return zone
    states = {s["k"]: s for s in all_.get("states") or []}
    for o in all_.get("orders") or []:
        info = order_card_info(o.get("file") or "")
        ratio, overdue = _sla(info["created"], info["deadline"], o.get("state"))
        pick = ("%d/%d ✅" % (info["pick_done"], info["pick_total"])) if info["pick_total"] else "无提货清单"
        node = _node(o.get("tasks") or [])
        dwell_min = o.get("dwell_min")
        try:  # mes_orders.load_all 按卡 (mtime,size) 缓存，dwell 会冻在缓存时刻；这里按当前钟点重算
            dm, _ = mes_orders._parse_dwell(info["header"].get("当前状态", ""), o.get("timeline") or [], now())
            if dm is not None:
                dwell_min = dm
        except Exception:
            pass
        zone["items"].append({
            "code": o.get("code") or "?", "title": o.get("title") or "",
            "state": o.get("state") or "unknown", "state_cn": o.get("state_cn") or "",
            "state_raw": info["state_raw"] or o.get("state") or "unknown",
            "priority": o.get("priority") or "", "p0": bool(o.get("p0")),
            "deadline": info["deadline"].strftime("%Y-%m-%d %H:%M") if info["deadline"] else (info["deadline_txt"] or "—"),
            "route": info["header"].get("排班", "") or "—",
            "owner": " · ".join([x for x in (o.get("dept"), o.get("seat")) if x]) or "—",
            "station_min": dwell_min,
            "overdue": bool(overdue), "sla_x": ratio,
            "delay_note": info["delay_note"] or "",
            "pickup": pick, "pickup_n": info["pick_done"], "pickup_m": info["pick_total"],
            "stage": int(o.get("stage") or 1), "sub_n": int(o.get("sub_n") or 0), "sub_m": int(o.get("sub_m") or 0),
            "task_n": int(o.get("task_n") or 0), "task_m": int(o.get("task_m") or 0),
            "warn": bool(o.get("warn")), "node": node,
            "signature": (node or {}).get("sign") or "—",
            "card": "orders/%s/order.md" % (o.get("code") or ""),
        })
    wip = [i for i in zone["items"] if (i["state_raw"] or i["state"]) not in TOKEN_TERMINAL]
    zone["summary"] = {"orders": len(zone["items"]), "wip": len(wip),
                       "by_state": {k: (states.get(k) or {}).get("n", 0) for k in states},
                       "overdue": sum(1 for i in zone["items"] if i["overdue"])}
    if not zone["items"]:
        zone["status"] = "missing"
        zone["note"] = "无订单数据（orders/*/order.md 0 张）"
    return zone


def _model_by_session():
    """session -> (provider, model, last_ms)；tasks-index 缺失时的真实心跳/模型来源。"""
    p = cli_db()
    out = {}
    if not os.path.isfile(p):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("?", "%3F").replace("#", "%23"), uri=True)
        for sid, prov, mid, last in con.execute(
                "select session_id, provider_id, model_id, max(started_at) from model_usage group by session_id"):
            out[sid] = (prov or "", mid or "", last or 0)
    except sqlite3.Error:
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def _ms_iso(ms):
    """epoch 毫秒 -> 本地 ISO；坏值返回 None（禁补 0）。"""
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    except (TypeError, ValueError, OSError):
        return None


def _runtime_channels():
    """T05 当前通道索引：session 前缀(13) -> model_usage 最近一条 + turn_usage 最近一轮 + 运行时在场。

    只读 CLI DB；无库/无表 -> 空索引，调用方一律 null（禁 0 禁猜）。
    """
    out = {}
    p = cli_db()
    if not os.path.isfile(p):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("?", "%3F").replace("#", "%23"), uri=True)
    except sqlite3.Error:
        return out
    try:
        try:
            for sid, prov, mid, last in con.execute(
                    "select session_id, provider_id, model_id, max(started_at) from model_usage group by session_id"):
                out.setdefault((sid or "")[:13], {})["model"] = {
                    "provider": (prov or "").strip() or None,
                    "model": (mid or "").strip() or None,
                    "as_of": _ms_iso(last)}
        except sqlite3.Error:
            pass
        try:
            for sid, st, done, req, last in con.execute(
                    "select session_id, status, completed_at, model_request_count, max(started_at) "
                    "from turn_usage group by session_id"):
                out.setdefault((sid or "")[:13], {})["turn"] = {
                    "started": last, "completed": done,
                    "status": (st or "").strip() or None,
                    "requests": req}
        except sqlite3.Error:
            pass
        try:
            for sid, owner, pid in con.execute(
                    "select session_id, owner_id, process_id from session_runtime_presence"):
                out.setdefault((sid or "")[:13], {})["presence"] = {
                    "present": True, "owner": owner or None,
                    "pid": str(pid) if pid is not None else None}
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def _channel_for(idx):
    """把通道索引条目规范化成契约字段；无 model_usage 记录 -> provider/model/as_of/source 全 null。"""
    ch = {"provider": None, "model": None, "as_of": None, "source": None,
          "status": "missing", "note": "model_usage 无该会话最近记录（禁 0 禁猜）",
          "turn": None, "presence": {"present": False, "owner": None, "pid": None}}
    if not idx:
        return ch
    m = idx.get("model") or {}
    if m.get("provider") or m.get("model"):
        ch.update({"provider": m.get("provider"), "model": m.get("model"),
                   "as_of": m.get("as_of"), "source": "model_usage",
                   "status": "ok", "note": ""})
    t = idx.get("turn")
    if t:
        ch["turn"] = {"at": _ms_iso(t.get("started")),
                      "completed_at": _ms_iso(t.get("completed")),
                      "status": t.get("status"),
                      "requests": t.get("requests") if isinstance(t.get("requests"), int) else None}
    if idx.get("presence"):
        ch["presence"] = idx["presence"]
    return ch


def zone_seats():
    p = os.path.join(state_dir(), "seat_registry.json")
    reg = read_json(p)
    # P0 fix (ops-04 21:40): tasks-index candidate chain (state_dir first, then user v2 stores)
    _tc = [os.path.join(state_dir(), "tasks-index.sqlite"),
           os.path.join(os.path.expanduser("~"), ".zcode", "v2", "tasks-index.sqlite"),
           r"C:\Users\Administrator\.zcode\v2\tasks-index.sqlite",
           r"C:\Users\Administrator\AppData\Roaming\zcode\tasks-index.sqlite"]
    tidx = next((x for x in _tc if os.path.exists(x)), _tc[0])
    models = _model_by_session()
    channels = _runtime_channels()
    mshort = {k[:13]: v for k, v in models.items()}
    zone = {"as_of": iso(),
            "source": "state/seat_registry.json（seats/retired）+ 在制卡 §2.1 会话 + model_usage/turn_usage/session_runtime_presence（模型/心跳/当前通道 join）",
            "sources": [p, cli_db()], "summary": {}, "items": [],
            "note": "tasks-index.sqlite 缺失：席位实时 status/updated_at 未接入，last_beat=最近模型调用时刻；badges 段未建，工牌短码由会话号派生"}
    if not reg:
        zone["status"] = "missing"
        zone["note"] = "seat_registry.json 不可读（或不存在）"
        zone["summary"] = {"seats": 0, "active": 0}
        return zone
    zone["status"] = "degraded"
    now_ts = now().timestamp()
    # 在制卡 §2.1 当前编成会话 -> 订单号（只取在制单，历史单不生成伪席位）
    sess_order, sess_seat = {}, {}
    for o in (mes_orders.load_all().get("orders") or []):
        info = order_card_info(o.get("file") or "")
        if (info["state_raw"] or o.get("state")) in TOKEN_TERMINAL:
            continue
        for s8 in info["flow_sess"]:
            sess_order[s8] = o.get("code") or ""
        for s8, emp in (info.get("flow_emp") or {}).items():
            sess_seat.setdefault(s8, emp)
    rows, seen = [], set()
    for bucket, lst in (("active", reg.get("seats") or []), ("retired", reg.get("retired") or [])):
        for s in lst:
            sid = s.get("session") or ""
            seen.add(sid[:13])
            rows.append((s, bucket, sid))
    for s, bucket, sid in rows:
        prov, mid, last = models.get(sid, mshort.get(sid[:13], ("", "", 0)))
        beat = datetime.datetime.fromtimestamp(last / 1000).strftime("%Y-%m-%d %H:%M:%S") if last else "—"
        zone["items"].append({
            "seat": s.get("seat", ""), "dept": s.get("dept", ""),
            "session": sid, "badge": sid[:13] if sid else "",
            "model": ("%s/%s" % (prov, mid)) if prov or mid else "—",
            "status": s.get("status", "") or bucket,
            "order": sess_order.get(sid[:13], "—"),
            "last_beat": beat,
            "beat_age_min": int((now_ts - last / 1000) // 60) if last else None,
            "channel": _channel_for(channels.get(sid[:13]))})
    for s8, code in sorted(sess_order.items()):
        if s8 in seen:
            continue
        seen.add(s8)
        prov, mid, last = mshort.get(s8, ("", "", 0))
        beat = datetime.datetime.fromtimestamp(last / 1000).strftime("%Y-%m-%d %H:%M:%S") if last else "—"
        fresh = bool(last and (now_ts - last / 1000) < 7200)
        zone["items"].append({"seat": sess_seat.get(s8) or "—", "dept": "", "session": s8, "badge": s8,
                              "model": ("%s/%s" % (prov, mid)) if prov or mid else "—",
                              "status": "active" if fresh else "stale", "order": code,
                              "last_beat": beat,
                              "beat_age_min": int((now_ts - last / 1000) // 60) if last else None,
                              "channel": _channel_for(channels.get(s8))})
    active = sum(1 for r in zone["items"] if r["status"] in ("active", "from-card"))
    zone["summary"] = {"seats": len(zone["items"]), "active": active,
                       "registry_active": sum(1 for s in (reg.get("seats") or []) if s.get("status") == "active"),
                       "retired": len(reg.get("retired") or []),
                       "channel_ok": sum(1 for r in zone["items"] if r["channel"]["status"] == "ok"),
                       "channel_missing": sum(1 for r in zone["items"] if r["channel"]["status"] == "missing")}
    return zone


def _events_from_ledger():
    for rel in ("events/ledger.jsonl", "state/events.jsonl", "state/events/ledger.jsonl"):
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            return p
    return ""


def zone_events(limit=50):
    ledger = _events_from_ledger()
    zone = {"as_of": iso(), "items": []}
    if ledger:
        zone.update({"status": "ok", "source": "MES events 台账（%s）" % ledger, "sources": [ledger]})
        rows = []
        for line in read_text(ledger).splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            rows.append({"ts": d.get("ts") or d.get("time") or "", "badge": d.get("badge") or d.get("actor") or "—",
                         "order": d.get("order") or "", "action": d.get("action") or "?"})
        zone["items"] = sorted(rows, key=lambda r: r["ts"], reverse=True)[:limit]
        zone["summary"] = {"events": len(zone["items"]), "ledger": ledger}
        return zone
    # 台账未建（C-008 F10 前）：按派单令降级抽签字件 + 证据 mtime（真实事件，非 §3 时间轴）
    base = mes_orders.orders_dir()
    ev = []
    for sp in glob.glob(os.path.join(base, "*", "signatures", "*.md")):
        code = os.path.basename(os.path.dirname(os.path.dirname(sp)))
        st = os.stat(sp)
        m = re.match(r"^([A-Z]-\d{3})", os.path.basename(sp))
        ev.append({"ts": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                   "badge": m.group(1) if m else "—", "order": code, "action": "sign",
                   "artifact": sp})
    for ep in glob.glob(os.path.join(reports_dir(), "evidence", "C-*", "*")):
        if not os.path.isfile(ep):
            continue
        name = os.path.basename(ep)
        if name in ("MANIFEST.md",) or name.lower().endswith((".png",)):
            continue
        code = os.path.basename(os.path.dirname(ep))
        st = os.stat(ep)
        m = re.search(r"([A-Z]-\d{3})", name)
        ev.append({"ts": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                   "badge": m.group(1) if m else "—", "order": code, "action": "report", "artifact": ep})
    ev.sort(key=lambda r: r["ts"], reverse=True)
    zone.update({"status": "degraded",
                 "source": "orders/*/signatures/*.md + reports/evidence/* mtime（events 台账未建）",
                 "sources": [os.path.join(base, "*", "signatures", "*.md"), os.path.join(reports_dir(), "evidence", "C-*")],
                 "note": "数据源建设中（MES events 台账未建；按派单令以签字件/证据 mtime 抽真实事件，台账上线后切正源）",
                 "items": ev[:limit],
                 "summary": {"events": min(len(ev), limit), "scan": len(ev)}})
    return zone


def _shift():
    """班次规则：MES/06 日 08–22 / 夜 22–09；交接 21:50 / 08:50。"""
    n = now()
    h = n.hour
    if 8 <= h < 22:
        start, end, hand = n.replace(hour=8, minute=0, second=0, microsecond=0), n.replace(hour=22, minute=0, second=0, microsecond=0), n.replace(hour=21, minute=50, second=0, microsecond=0)
        label = "日班 08:00–22:00"
    else:
        if h >= 22:
            start = n.replace(hour=22, minute=0, second=0, microsecond=0)
        else:
            start = (n - datetime.timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        end = (start + datetime.timedelta(hours=11))
        hand = end - datetime.timedelta(minutes=10)
        label = "夜班 22:00–09:00"
    return {"label": label, "start": start, "end": end, "handover": hand,
            "remain_min": int((end - n).total_seconds() // 60),
            "handover_min": int((hand - n).total_seconds() // 60)}


def _lane_used():
    """最近 5 分钟有模型调用的去重会话 -> lane（tasks-index 缺失时唯一真实占用口径）。"""
    p = cli_db()
    used = {}
    if not os.path.isfile(p):
        return used
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("?", "%3F").replace("#", "%23"), uri=True)
        since = (now() - datetime.timedelta(minutes=5)).timestamp() * 1000
        for prov, sid in con.execute("select distinct provider_id, session_id from model_usage where started_at>=?", (since,)):
            lane = PROVIDER_LANE.get(prov)
            if lane:
                used.setdefault(lane, set()).add(sid)
    except sqlite3.Error:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass
    return {k: len(v) for k, v in used.items()}


def zone_capacity():
    caps_p = os.path.join(state_dir(), "lane_caps.json")
    caps = read_json(caps_p) or {}
    capd = caps.get("caps") or {}
    sh = _shift()
    used = _lane_used()
    lanes = [{"lane": k, "used": used.get(k, 0), "cap": v} for k, v in capd.items()]
    free = sum(max(0, v - used.get(k, 0)) for k, v in capd.items())
    shift_p = mes_orders.latest_shift_path()
    zone = {"as_of": iso(), "status": "degraded",
            "source": "MES系统/06 班次规则 + state/lane_caps.json + model_usage（5min 占用）",
            "sources": [shift_p or "（排班单目录无 SHIFT-*.md）", caps_p, cli_db()],
            "shift": sh["label"], "remain_min": sh["remain_min"],
            "handover_in_min": sh["handover_min"],
            "lanes": lanes,
            "pool": {"R4": "起算 38亿 · 余额未接入（F 成本账未建）",
                     "R4-2": "起算 110亿 · 余额未接入（F 成本账未建）",
                     "free": free},
            "note": "lane 实名未接入（tasks-index.sqlite 缺失）：used=最近5分钟有模型调用的去重会话；成本账未建，付费池余额不给假数"}
    if not caps:
        zone["status"] = "missing"
        zone["note"] = "lane_caps.json 不可读：lane 上下限无源"
    if not shift_p:
        zone["note"] += "；当日排班单缺失（最新为 11-20 夜班卡）"
    zone["summary"] = {"shift": sh["label"], "remain_min": sh["remain_min"],
                       "handover_in_min": sh["handover_min"], "free": free,
                       "lanes_used": {l["lane"]: l["used"] for l in lanes}}
    return zone


def _patrol_items():
    p = os.path.join(state_dir(), "PATROL_ALERT.txt")
    text = read_text(p).lstrip("\ufeff")
    blocks, cur = [], []
    for line in text.splitlines():
        if line.strip():
            cur.append(line.strip())
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    try:
        mt = datetime.datetime.fromtimestamp(os.stat(p).st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        mt = ""
    out = []
    for b in blocks:
        msg = re.sub(r"\s+", " ", " ".join(b)).strip()
        dm = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", msg)
        lvl = "red" if re.search(r"P0|加急|红线", msg) else ("yellow" if re.search(r"P1|令|黄", msg) else "info")
        om = re.search(r"(C-\d{8}-\d{3}|C-TEST-\d{8}-\d{2}|ITER-\d{8}-\d{3})", msg)
        out.append({"level": lvl, "order": om.group(1) if om else "—", "msg": msg[:200],
                    "ts": dm.group(1) if dm else mt})
    return p, out


def zone_alerts(orders_zone):
    p, patrol = _patrol_items()
    zone = {"as_of": iso(), "items": list(patrol),
            "source": "state/PATROL_ALERT.txt + 超时单（卡 §2.0/交付截止 1.5×黄 2×红）",
            "sources": [p, "orders/*/order.md 交付截止/下单时间"]}
    if not os.path.isfile(p):
        zone["status"] = "missing"
        zone["note"] = "无告警文件（state/PATROL_ALERT.txt 不存在）"
        zone["summary"] = {"patrol": 0, "overdue": len([i for i in (orders_zone.get("items") or []) if i.get("overdue")])}
        return zone
    zone["status"] = "ok"
    n_over = 0
    for o in (orders_zone.get("items") or []):
        if not o.get("overdue"):
            continue
        n_over += 1
        ratio = o.get("sla_x")
        lvl = "red" if (ratio is not None and ratio >= 2.0) else "yellow"
        zone["items"].append({"level": lvl, "order": o.get("code"), "ts": o.get("deadline"),
                              "msg": "%s 超交付截止（%s）%s" % (o.get("code"), o.get("deadline"),
                                                             (" · 时间比 %.2f×" % ratio) if ratio else "")})
    stall = os.path.join(ROOT, "scripts", "perm_stall_check.py")
    zone["summary"] = {"patrol": len(patrol), "overdue": n_over, "perm_stall": "脚本在（未运行，只读模式）" if os.path.isfile(stall) else "缺失"}
    if not os.path.isfile(stall):
        zone["note"] = "权限哨兵脚本缺失：挂权限哨兵未接入"
    return zone


def _token_sums():
    p = cli_db()
    if not os.path.isfile(p):
        return None
    n = now()
    day0 = n.replace(hour=0, minute=0, second=0, microsecond=0)
    sh = _shift()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("?", "%3F").replace("#", "%23"), uri=True)
        q = "select coalesce(sum(computed_total_tokens),0) from model_usage where started_at>=?"
        today = con.execute(q, (day0.timestamp() * 1000,)).fetchone()[0]
        shift = con.execute(q, (sh["start"].timestamp() * 1000,)).fetchone()[0]
        cum = con.execute("select coalesce(sum(computed_total_tokens),0) from model_usage").fetchone()[0]
        by = {}
        for prov, mid, tok in con.execute(
                "select provider_id, model_id, sum(computed_total_tokens) from model_usage where started_at>=? group by 1,2",
                (day0.timestamp() * 1000,)):
            lane = PROVIDER_LANE.get(prov, "未归类")
            by.setdefault(lane, {"lane": lane, "tokens": 0, "providers": []})
            by[lane]["tokens"] += int(tok or 0)
            by[lane]["providers"].append({"provider": prov, "model": mid, "tokens": int(tok or 0)})
    except sqlite3.Error:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass
    return {"today": int(today), "shift": int(shift), "cumulative": int(cum), "by_lane": list(by.values())}


def zone_cost():
    p = cli_db()
    zone = {"as_of": iso(), "source": "ZCode 库 model_usage（%s，只读实算）" % p,
            "sources": [p], "items": [], "note": "禁显 0、禁估算：全部由 model_usage.computed_total_tokens 实算"}
    t = _token_sums()
    if not t:
        zone["status"] = "missing"
        zone["note"] = "ZCode 库不可达或查询失败：token 统计无源（不显 0）"
        zone["summary"] = {"today_tokens": "不可达", "by_lane": []}
        zone["today_tokens"] = "不可达"
        zone["by_lane"] = []
        return zone
    zone["status"] = "ok"
    zone["today_tokens"] = t["today"]
    zone["shift_tokens"] = t["shift"]
    zone["cumulative_tokens"] = t["cumulative"]
    zone["by_lane"] = t["by_lane"]
    zone["summary"] = {"today_tokens": t["today"], "shift_tokens": t["shift"],
                       "cumulative_tokens": t["cumulative"],
                       "by_lane": [{"lane": b["lane"], "tokens": b["tokens"]} for b in t["by_lane"]]}
    return zone


def zone_handover():
    files = sorted(glob.glob(os.path.join(reports_dir(), "HANDOFF_*.md")), key=os.path.getmtime, reverse=True)
    zone = {"as_of": iso(), "items": [],
            "source": "reports/HANDOFF_*.md（最近交接单）", "sources": [os.path.join(reports_dir(), "HANDOFF_*.md")]}
    if not files:
        zone["status"] = "missing"
        zone["note"] = "无交接单（待 21:50 交班生成）"
        zone["summary"] = {"handover": 0}
        return zone
    p = files[0]
    st = os.stat(p)
    dt = datetime.datetime.fromtimestamp(st.st_mtime)
    fresh = dt.date() == now().date()
    heads = [l.strip() for l in read_text(p).splitlines() if l.strip()][:14]
    zone["status"] = "ok" if fresh else "degraded"
    if not fresh:
        zone["note"] = "最近交接单为 %s 旧件（非本班），21:50 交班后更新" % dt.strftime("%Y-%m-%d")
    zone["items"] = [{"ts": dt.strftime("%Y-%m-%d %H:%M:%S"), "file": p, "summary": heads}]
    zone["summary"] = {"handover": 1, "file": p, "fresh": fresh}
    return zone


def collect_zones():
    o = zone_orders()
    zones = {
        "orders": o,
        "seats": zone_seats(),
        "events": zone_events(),
        "capacity": zone_capacity(),
        "alerts": zone_alerts(o),
        "cost": zone_cost(),
        "handover": zone_handover(),
    }
    zs = {k: v.get("status", "missing") for k, v in zones.items()}
    src = {k: v.get("sources") or [v.get("source", "")] for k, v in zones.items()}
    return {"contract": CONTRACT, "generated_at": iso(), "zones": zones,
            "zone_status": zs, "sources": src}


def collect_v2(with_recon=True):
    z = collect_zones()
    out = {"generated_at": z["generated_at"],
           "meta": {"refresh_sec": 60, "auth": "unchanged", "contract": CONTRACT},
           "zones": z["zones"], "zone_status": z["zone_status"], "sources": z["sources"]}
    if with_recon:
        try:
            import board_recon
            out["recon"] = board_recon.reconcile(z)
        except Exception as exc:  # recon 失败不拖垮数据层
            out["recon"] = {"checked": 0, "matched": 0, "detail": [], "error": str(exc)}
    return out


def main():
    ap = argparse.ArgumentParser(description="C-009 七区真数据层（只读）")
    ap.add_argument("--check", action="store_true", help="打印七区 status/sources")
    ap.add_argument("--v2", action="store_true", help="输出派单 v2 外形（含 recon）")
    ap.add_argument("--json", metavar="OUT", help="落盘 JSON 路径")
    ap.add_argument("--no-recon", action="store_true")
    a = ap.parse_args()
    data = collect_v2(with_recon=not a.no_recon) if a.v2 else collect_zones()
    if a.v2 and a.no_recon:
        data.pop("recon", None)
    if a.json:
        d = os.path.dirname(os.path.abspath(a.json))
        if d:
            os.makedirs(d, exist_ok=True)
        io.open(a.json, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2))
    if a.check or not a.json:
        zs = data.get("zone_status") or {}
        print("contract=%s generated_at=%s" % (data.get("contract") or data.get("meta", {}).get("contract"), data.get("generated_at")))
        for k in ("orders", "seats", "events", "capacity", "alerts", "cost", "handover"):
            v = data["zones"][k]
            print("%-9s %-9s %s" % (k, zs.get(k, v.get("status")), " | ".join(v.get("sources") or [v.get("source", "")])[:160]))
            if v.get("note"):
                print("          note: %s" % v["note"][:180])
        if "recon" in data:
            print("recon %s/%s" % (data["recon"].get("matched"), data["recon"].get("checked")))
    if a.json and not a.check:
        n = sum(len(data["zones"][k].get("items") or []) for k in data["zones"])
        print("wrote %s (%d zones, %d items)" % (a.json, len(data["zones"]), n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
