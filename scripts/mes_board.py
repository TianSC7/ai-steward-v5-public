#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mes_board.py —— 工厂 MES 看板生成器(零依赖,stdlib only;Ant-Design 浅色高级皮肤)

设计语言学习自 GitHub ant-design/ant-design 种子 token(用户指定浅色):
  主色 #4a6fa5、成功 #2f6b45、错误 #b23b40、布局底 #f5f5f5、容器 #ffffff、
  分割线 #f0f0f0/#e5e6eb、正文 #1f1f1f、次级 #595959、弱化 #6b6b6b/#767676。
  (2026-11-20 C-20261120-002: --ok/--bad 调深至白底 ≥4.5;橙灯 #8c5a00)

collect() 现场取数返回结构化数据;render() 产出首屏HTML(内嵌JS每2秒拉 /data.json 局部刷新,无整页重载)。
main() 落盘 dashboard/index.html(静态兜底,供 SSH 拉取通道)。
订单层:mes_orders.load_all() 只读解析 MD 订单卡(头部/§3/§9) → 首屏实时三问 + 15 态泳道条。
"""
import argparse
import datetime
import html
import io
import json
import os
import re
import sqlite3
import time

import mes_orders

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
_DB_CANDIDATES = [
    r"C:\Users\Administrator\.zcode\v2\tasks-index.sqlite",
    os.path.join(os.path.expanduser("~"), ".zcode", "v2", "tasks-index.sqlite"),
]
DB = next((p for p in _DB_CANDIDATES if os.path.isfile(p)), _DB_CANDIDATES[0])
CLI_DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")

PIPELINE = ["queued", "dispatched", "verify", "done"]
PIPE_CN = {"queued": "排队", "dispatched": "已派单", "verify": "执行/验收", "done": "完成"}
BLOCKED = ("blocked", "fail", "reject")
SEAT_TIERS = (("调度台", "厂长", "001"), ("厂长", "厂长", "001"), ("M-PLAN", "经理", "002"),
              ("M-SAFETY", "经理", "003"), ("验收", "验收", "004"))
TEAM_IDS = {"厂长": "001", "M-PLAN": "002", "M-SAFETY": "003", "验收": "004"}
TIER_RANK = {"厂长": 0, "经理": 1, "组长": 2, "员工": 3, "验收": 4}
SKIN = "18"  # 皮肤版本:模板/样式变更时递增,客户端JS检测到不一致会自动整页刷新一次

ST_CN = {"completed": "已完成", "running": "运行中", "failed": "失败", "blocked": "受阻",
         "queued": "排队", "in_progress": "进行中", "dispatched": "已派单", "pending": "等待",
         "overdue": "已超期", "passed": "已通过", "interrupted": "被中断"}
OC_CN = {"succeeded": "成功", "running": "运行中", "failed": "失败", "timeout": "超时",
         "pending": "等待", "dispatched": "已派发", "idle": "空闲"}


def cn_status(s):
    s = (s or "").strip().lower()
    if not s:
        return "-"
    return ST_CN.get(s, s)


def cn_outcome(s):
    s = (s or "").strip().lower()
    if not s:
        return "-"
    return OC_CN.get(s, s)


def seat_meta(title):
    t = title or ""
    for key, tier, no in SEAT_TIERS:
        if key in t:
            return tier, no
    return "员工", "--"


def rd(name, limit=None):
    p = os.path.join(STATE, name)
    if not os.path.isfile(p):
        return ""
    ls = io.open(p, encoding="utf-8", errors="replace").read().splitlines()
    ls = [l for l in ls if l.strip()]
    return ls[-limit:] if limit else ls


def parse_safety(lines):
    out = []
    for l in lines:
        if not re.match(r"^SF-\d+\|", l):
            continue
        parts = l.split("|")
        if len(parts) >= 3:
            out.append({"time": parts[1], "id": parts[0], "kind": parts[2],
                        "obj": parts[3] if len(parts) > 3 else "",
                        "txt": html.escape(parts[4][:110]) if len(parts) > 4 else ""})
    return out[-14:][::-1]


def parse_resolutions(lines):
    """处置台账:编号|紧急程度|处理方式|处理结果|状态|处置时间"""
    res = {}
    for l in lines:
        if not re.match(r"^SF-\d+\|", l):
            continue
        p = l.split("|")
        if len(p) >= 5:
            res[p[0].strip()] = {"urg": p[1].strip(), "method": html.escape(p[2].strip()[:60]),
                                 "result": html.escape(p[3].strip()[:60]), "status": p[4].strip()}
    return res


def parse_decisions(lines):
    out = []
    for l in lines:
        m = re.match(r"(2026-11-\d+ [\d:]+)\|(D-[\d\-]+)\|(.*)", l)
        if m:
            out.append({"time": m.group(1)[-5:], "did": m.group(2)[-4:], "fid": m.group(2),
                        "txt": html.escape(m.group(3)[:160])})
    return out[-10:][::-1]


def parse_plain(lines):
    """决策白话对照:编号|白话说明"""
    out = {}
    for l in lines:
        if not re.match(r"^D-\d{8}-\d+\|", l):
            continue
        p = l.split("|", 1)
        if len(p) == 2:
            out[p[0].strip()] = html.escape(p[1].strip())
    return out


def parse_ledger():
    for l in rd("MODEL_LEDGER.md", 200):
        m = re.search(r"tokens[=＝]\s*([\d\.]+)\s*亿.*violations[=＝]\s*(\d+)", l)
        if m:
            return {"tokens_yi": m.group(1), "violations": m.group(2)}
        m = re.search(r"violations[=＝]\s*(\d+)", l)
        if m:
            return {"tokens_yi": "?", "violations": m.group(1)}
    return {"tokens_yi": "?", "violations": "?"}


def state_of(card):
    s = (card.get("state") or card.get("status") or "").lower()
    if not s:
        return "queued"
    for b in BLOCKED:
        if b in s:
            return "blocked"
    if "done" in s or "complete" in s:
        return "done"
    if "dispatch" in s:
        return "dispatched"
    if "verify" in s or "accept" in s or "running" in s or "progress" in s:
        return "verify"
    return "queued"


def lane_of(card):
    m = (card.get("model") or "").upper()
    if "DEEPSEEK" in m or "R4" in m:
        return "DS"
    if "STEP" in m:
        return "ST"
    if "NEX" in m:
        return "NX"
    if "GLM" in m:
        return "GL"
    if "OUTSOURCE" in m or "MINEKUAI" in m:
        return "OB"
    return "??"


def load_db():
    d = {"autos": [], "runs": [], "seats": [], "unarchived": 0}
    if not os.path.isfile(DB):
        return d
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        for a in con.execute("select automation_id,title,enabled,run_count,last_run_at,next_run_at,last_error from automations order by enabled desc, title").fetchall():
            d["autos"].append({
                "title": (a["title"] or "")[:24], "en": a["enabled"], "runs": a["run_count"],
                "last": datetime.datetime.fromtimestamp(a["last_run_at"] / 1000).strftime("%H:%M") if a["last_run_at"] else "-",
                "next": datetime.datetime.fromtimestamp(a["next_run_at"] / 1000).strftime("%H:%M") if a["next_run_at"] else "-",
                "err": (a["last_error"] or "")[:40]})
        for r in con.execute("select automation_id,scheduled_at,outcome,error from automation_runs order by scheduled_at desc limit 12").fetchall():
            d["runs"].append({
                "sch": datetime.datetime.fromtimestamp(r["scheduled_at"] / 1000).strftime("%H:%M:%S"),
                "auto": (r["automation_id"] or "")[-8:], "outcome": r["outcome"] or "?",
                "err": (r["error"] or "")[:60]})
        for t in con.execute("select task_id,title,task_status,archived,updated_at from tasks where archived=0 and ifnull(title,'')!='' order by updated_at desc limit 8").fetchall():
            tier, no = seat_meta(t["title"])
            d["seats"].append({
                "no": no, "tier": tier, "task_id": t["task_id"], "name": (t["title"] or "")[:26],
                "st": cn_status(t["task_status"]), "arch": t["archived"],
                "upd": datetime.datetime.fromtimestamp((t["updated_at"] or 0) / 1000).strftime("%H:%M:%S") if t["updated_at"] else "-"})
        d["unarchived"] = con.execute("select count(*) c from tasks where archived=0 and ifnull(title,'')!=''").fetchone()["c"]
    finally:
        con.close()
    return d


def collect():
    """现场取数,返回看板全部数据(片段为已转义的HTML字符串,供首屏与 /data.json 共用)。"""
    reg = {}
    p = os.path.join(STATE, "registry.json")
    if os.path.isfile(p):
        try:
            reg = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            reg = {}
    cards = reg.get("tasks") or []
    team = reg.get("team") or {}
    db = load_db()
    ledger = parse_ledger()
    safety = parse_safety(rd("SAFETY_FINDINGS.md"))
    res = parse_resolutions(rd("SAFETY_RESOLUTIONS.md"))
    decisions = parse_decisions(rd("DECISIONS.md", 400))
    plain = parse_plain(rd("DECISIONS_PLAIN.md"))
    ap_path = os.path.join(STATE, "PLAN_1H_APPROVED.md")
    ap_info = ("%dB %s" % (os.path.getsize(ap_path), datetime.datetime.fromtimestamp(os.path.getmtime(ap_path)).strftime("%H:%M:%S")) if os.path.isfile(ap_path) else "缺")

    stages = {k: [] for k in PIPELINE + ["blocked"]}
    for c in cards:
        stages[state_of(c)].append(c)

    def _age_text(c):
        v = c.get("created_at")
        if not v:
            return ""
        try:
            if isinstance(v, (int, float)) or (isinstance(v, str) and str(v).isdigit()):
                t = float(v)
                if t > 10**11:
                    t /= 1000.0
            else:
                t = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
                if t > 10**11:
                    t /= 1000.0
            age = time.time() - t
            if age < 0:
                return ""
            m = int(age // 60)
            if m < 60:
                return "%d分钟" % m
            return "%dh%02dm" % (m // 60, m % 60)
        except Exception:
            return ""

    def item(c):
        bad = " bad" if state_of(c) == "blocked" else ""
        cid = html.escape(str(c.get("id") or "?"))
        age = _age_text(c)
        age_html = ('<span class="age">%s</span>' % html.escape(age)) if (bad and age) else ""
        return ('<div class="titem%s"><span class="dot"></span><a class="mono" href="/card/%s">%s</a>'
                '<span class="lane">%s</span>%s</div>'
                % (bad, cid, cid, lane_of(c), age_html))

    cols = []
    for k in PIPELINE + ["blocked"]:
        cn = PIPE_CN.get(k, "受阻")
        cols.append({"key": k, "html": '<div class="colh">%s<span class="cnt">%d</span></div>%s'
                     % (cn, len(stages[k]), "".join(item(c) for c in stages[k][:40]))})

    order = sorted(db["seats"], key=lambda s: (TIER_RANK.get(s["tier"], 9), s["no"]))
    seat_rows = ""
    seen = []
    for s in order:
        if s["tier"] not in seen:
            seen.append(s["tier"])
            seat_rows += '<tr class="trow"><td colspan="5">%s 档</td></tr>' % s["tier"]
        link = ('<a href="/seat/%s">%s</a>' % (s["task_id"], html.escape(s["name"]))
                if s.get("task_id") else html.escape(s["name"]))
        st_cls = " bad" if s["st"] in ("被中断", "失败", "受阻") else ""
        seat_rows += ('<tr><td class="mono">%s</td><td>%s</td><td class="%s">%s</td><td class="mut2">%s</td><td class="mono">%s</td></tr>'
                      % (s["no"], link, st_cls, s["st"], "已归档" if s["arch"] else "在册", s["upd"]))
    for tier in ("组长", "员工"):
        if tier not in seen:
            seat_rows += ('<tr class="trow"><td colspan="5">%s 档</td></tr>'
                          '<tr><td class="mono">--</td><td class="mut">虚位待编</td><td>-</td><td class="mut">-</td><td>-</td></tr>' % tier)

    team_parts = []
    for k, v in team.items():
        no = TEAM_IDS.get(str(k), "")
        team_parts.append("%s %s:%s" % (no, html.escape(str(k)), html.escape(str(v)[:24])))
    team_txt = " · ".join(team_parts) or "registry.team 无数据"

    def _auto_row(a):
        return ('<tr><td>%s</td><td class="%s">%s</td><td class="mono">%s</td><td class="mono">%s-&gt;%s</td><td class="bad">%s</td></tr>'
                % (html.escape(a["title"]), "ok" if a["en"] else "mut", "启用" if a["en"] else "停用", a["runs"],
                   a["last"], a["next"], html.escape(a["err"] or "-")))

    auto_rows = "".join(_auto_row(a) for a in db["autos"] if a["en"])
    auto_off = [a for a in db["autos"] if not a["en"]]
    auto_off_rows = "".join(_auto_row(a) for a in auto_off)

    run_rows = "".join(
        '<tr><td class="mono">%s</td><td class="mono">%s</td><td class="%s">%s</td><td class="mut">%s</td></tr>'
        % (r["sch"], r["auto"],
           "ok" if r["outcome"] == "succeeded" else ("run" if r["outcome"] == "running" else "bad"),
           cn_outcome(r["outcome"]), html.escape(r["err"] or "-"))
        for r in db["runs"])

    open_sf = [s for s in safety if res.get(s["id"], {}).get("status") != "已处理"]
    archived_sf = len([s for s in safety if res.get(s["id"], {}).get("status") == "已处理"])
    sf_rows = ""
    for s in open_sf:
        r = res.get(s["id"], {})
        urg = r.get("urg", "P2")
        ucls = "bad" if urg == "P1" else ("mut" if urg == "P3" else "")
        sf_rows += ('<tr><td class="mono">%s</td><td class="mono">%s</td><td class="%s">%s</td><td>%s</td>'
                    '<td>%s</td><td>%s</td><td>%s</td></tr>'
                    % (s["time"], s["id"], ucls, urg, html.escape(s["kind"]),
                       html.escape((s["obj"] + " " + s["txt"])[:60]),
                       r.get("method", "<span class='mut'>待处理</span>"),
                       r.get("result", "<span class='bad'>待处理</span>")))
    sf_foot = "已归档 %d 条 · 台账 state/SAFETY_RESOLUTIONS.md(M-SAFETY 巡场维护)" % archived_sf

    dc_rows = "".join(
        '<tr><td class="mono">%s</td><td class="mono">%s</td><td title="%s">%s</td></tr>'
        % (d["time"], d["did"], d["txt"], plain.get(d["fid"], d["txt"])) for d in decisions)

    deltas = {k: "" for k in ("disp", "run", "done", "queue", "blk")}
    try:
        hist = json.load(io.open(os.path.join(STATE, "kpi_history.json"), encoding="utf-8"))
        target = (datetime.datetime.now() - datetime.timedelta(hours=1)).timestamp() * 1000
        base = None
        for h in hist:
            if h.get("ts", 0) <= target:
                base = h
        if base is None and hist:
            base = hist[0]
        for k in deltas:
            dv = kpis[k] - base["kpis"].get(k, 0)
            if dv:
                deltas[k] = ("+" if dv > 0 else "") + str(dv) + " / 1h"
    except Exception:
        pass

    series = {k: [] for k in ("disp", "run", "done", "queue", "blk")}
    try:
        hist = json.load(io.open(os.path.join(STATE, "kpi_history.json"), encoding="utf-8"))
        pts = hist[-40:]
        for k in series:
            series[k] = [h["kpis"].get(k, 0) for h in pts]
    except Exception:
        pass

    ord_all = mes_orders.load_all()
    ofoot = ("订单源 %s · 订单 %d 单 · 子需求=卡头 n/m（已过质检口径）· 任务 m/n=§9 待办行口径（非子需求）"
             " · 16 个状态 key 全量渲染（字典 15 行）"
             % (ord_all["dir"], len(ord_all["orders"])))
    if ord_all["error"]:
        ofoot = "订单源不可读：%s" % ord_all["error"]

    details = {}
    for o in ord_all["orders"]:
        det = mes_orders.order_detail(o.get("file") or "", base=ord_all["dir"])
        if det and det.get("code"):
            details[det["code"]] = det
    g_path = mes_orders.latest_shift_path()
    gantt = mes_orders.parse_shift_gantt(g_path) if g_path else None

    z = _collect_zones_safe()
    d = {
        "skin": SKIN,
        "series": series,
        "deltas": deltas,
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kpis": {"disp": len(stages["dispatched"]), "run": len(stages["verify"]), "done": len(stages["done"]),
                 "queue": len(stages["queued"]), "blk": len(stages["blocked"]),
                 "tokens": ledger["tokens_yi"], "viol": ledger["violations"], "sess": db["unarchived"]},
        "cols": cols, "seats": seat_rows, "team": team_txt, "autos": auto_rows,
        "autosoff": auto_off_rows, "autosoffn": len(auto_off),
        "runs": run_rows, "sf": sf_rows, "sffoot": sf_foot, "dc": dc_rows, "ap": ap_info,
        "orders": ord_all["orders"], "states": ord_all["states"],
        "orders_html": _orders_rows(ord_all["orders"]), "states_html": _state_chips(ord_all["states"]),
        "orders_dir": ord_all["dir"], "orders_err": ord_all["error"], "ofoot": ofoot,
        "details": details, "gantt": gantt, "gantt_path": g_path,
        "gantt_html": _gantt_html(gantt, g_path),
        "contract": z.get("contract"), "zones": z.get("zones"),
        "zone_status": z.get("zone_status"), "sources": z.get("sources"),
        "zones_generated_at": z.get("generated_at"), "zones_error": z.get("error") or "",
    }
    d["ui"] = _ui_payload(d)
    return d


def _esc(s):
    """订单层统一转义：先 html.escape，再把 '→' 变实体，避免 render() 收尾替换链污染(R4)。"""
    return html.escape(str(s if s is not None else "")).replace("→", "&rarr;")


def _state_chips(states):
    """15 态泳道条(16 key 全量渲染，0 计数灰显不隐藏)。"""
    parts = []
    for s in states or []:
        meta = mes_orders.STATE_MAP.get(s.get("k"))
        color = meta["color"] if meta else "#b23b40"
        n = int(s.get("n") or 0)
        cls = "chip" + ("" if n else " zero") + (" unk" if s.get("k") == "unknown" else "")
        title = (meta["cond"] if meta else "状态未识别，请核卡")
        codes = ""
        if n and s.get("codes"):
            codes = " · " + " ".join('<a class="chipcode" href="#order=%s">%s</a>' % (_esc(c), _esc(c))
                                     for c in s["codes"][:6])
        parts.append('<span class="%s" style="--c:%s" title="%s"><b>%s</b><i>%d</i><em>%s</em></span>'
                     % (cls, color, _esc(title), _esc(s.get("cn") or "?"), n, codes))
    return "".join(parts)


def _orders_rows(orders):
    """首屏实时三问表行：走到哪一步 / 完成进度(卡头 n/m) / 在谁手上。"""
    if not orders:
        return ('<tr><td colspan="4" class="mut">未找到订单卡（orders/）——请核 MES_ORDERS_DIR</td></tr>')
    out = []
    for o in orders:
        stage = int(o.get("stage") or 1)
        warn = ""
        if o.get("warn"):
            warn = ('<span class="warnmark" title="卡头 %s ≠ §3 时间轴推导 %s，按头部为主显示">⚠卡内不一致</span>'
                    % (_esc(mes_orders.STATE_MAP.get(o.get("state"), {}).get("cn", o.get("state"))),
                       _esc(mes_orders.STATE_MAP.get(o.get("alt_state"), {}).get("cn", o.get("alt_state")))))
        p0 = '<span class="p0">P0</span>' if o.get("p0") else ""
        track = "".join('<i class="%s"></i>' % ("on" if i <= stage else "") for i in range(1, 11))
        dwell_cls = o.get("dwell_cls") or ""
        who = " · ".join([x for x in (o.get("dept"), o.get("seat")) if x]) or "—"
        out.append(
            '<tr><td class="ocell"><a class="mono ocode" href="#order=%s">%s</a>%s'
            '<div class="otitle">%s</div></td>'
            '<td><span class="schip" style="--c:%s">%s</span>%s'
            '<div class="mini">已历时 %s · 阶段 %d/10</div><div class="track">%s</div></td>'
            '<td><span class="pnum"><b>%d</b>/10</span>'
            '<div class="mini">子需求 %d/%d（%d%%）</div>'
            '<div class="mut2">任务 %d/%d（非子需求口径）</div></td>'
            '<td><div class="mini">%s</div><div class="dwell %s">停留 %s</div></td></tr>'
            % (_esc(o.get("code") or "?"), _esc(o.get("code") or "?"), p0, _esc(o.get("title") or ""),
               o.get("color") or "#4a6fa5", _esc(o.get("state_cn") or "未识别"), warn,
               _esc(o.get("dwell_txt") or "—"), stage, track,
               stage, int(o.get("sub_n") or 0), int(o.get("sub_m") or 0), int(o.get("sub_pct") or 0),
               int(o.get("task_n") or 0), int(o.get("task_m") or 0),
               _esc(who), dwell_cls, _esc(o.get("dwell_txt") or "—")))
    return "".join(out)


def _gantt_html(g, path):
    """排产甘特区块（03 §二 字段 1:1）：无表 → 显式空态（不画空格子）；有表 → 班组×小时三段式。"""
    if not g:
        look = path or mes_orders.shift_dir()
        return ('<div class="gempty"><b>未找到排班单甘特表</b>'
                '<div class="mini">查找路径：<span class="mono">' + _esc(look) + '</span></div>'
                '<div class="mini">按 <span class="mono">MES系统/03-排产甘特图.md §四</span> 模板落盘后自动显示；'
                '当前不画空格子（禁伪造）。</div></div>')
    hours = g.get("hours") or []
    head = ['<th scope="col" class="glab">班组（编成 · 主单）</th>']
    for h in hours:
        mark = "▶" if h.get("cur") else ""
        head.append('<th scope="col" class="ghead ' + h.get("kind", "rolling") + '">' +
                    _esc(h.get("h")) + mark + '</th>')
    body = []
    for r in g.get("rows") or []:
        label = '<span class="gteam">' + _esc(r.get("team") or "") + '</span>'
        if r.get("comp"):
            label += '<span class="gcomp">' + _esc(r["comp"]) + '</span>'
        if r.get("extra"):
            label += '<span class="gbadge">加开</span>'
        tds = ['<th scope="row" class="glab">' + label + '</th>']
        for c, h in zip(r.get("cells") or [], hours):
            cls = "gcell " + c.get("kind", "empty") + " " + h.get("kind", "rolling")
            txt = _esc(c.get("text") or "")
            if c.get("kind") == "predicted":
                txt = "（" + txt + "）"
            if c.get("insert"):
                txt = '<span class="ins" title="插单">⚡</span>' + txt
            tds.append('<td class="' + cls + '">' + txt + '</td>')
        body.append('<tr class="grow' + (' extra' if r.get("extra") else '') +
                    (' cad' if r.get("cad") else '') + '">' + ''.join(tds) + '</tr>')
    snap = g.get("snapshot") or "—"
    src = _esc(os.path.basename(g.get("file") or ""))
    changes = g.get("changes") or []
    if changes:
        ch = "".join('<tr><td class="mono">' + _esc(c.get("t")) + '</td><td>' + _esc(c.get("change")) +
                     '</td><td class="mut">' + _esc(c.get("basis")) + '</td></tr>' for c in changes)
        chblock = ('<details class="fold mt-s"><summary>班中变更（附录 · 已渲染 ' + str(len(changes)) +
                   ' 条）</summary><table><thead><tr><th>时间</th><th>变更</th><th>依据</th></tr></thead>'
                   '<tbody>' + ch + '</tbody></table></details>')
    else:
        chblock = '<div class="foot mt-s">班中变更（附录）：本班无记录</div>'
    cols = '<col class="glabcol">' + '<col>' * len(hours)
    legend = ('<div class="glegend" id="v_glegend">图例：▶=当前时刻（' + _esc(snap) +
              '）｜ <b>加粗列</b>=锁定栏（1h）｜ 普通列=滚动栏（5h）｜ <b>（括号）</b>=预判栏（12h，不承诺交付）｜ '
              '<span class="mono">—</span>=不在场</div>')
    foot = ('<div class="foot mt-s">数据源：' + src + ' · 快照 ' + _esc(snap) +
            ' · 字段对齐 <span class="mono">03 §二</span>（行=班组/席位 × 列=小时；格=订单号+子需求号）</div>')
    return ('<div class="gwrap"><table class="gantt"><colgroup>' + cols + '</colgroup><thead><tr>' +
            ''.join(head) + '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>' +
            legend + foot + chblock)


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>工厂 MES 看板 · ai-steward@aa</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2314a0e6' stroke-width='2'%3E%3Cpath d='M3 21V8l7-5 7 5v13M10 21v-6h4v6'/%3E%3C/svg%3E">
<style>
:root{--bg:#f5f5f5;--card:#ffffff;--inset:#fafafa;--ink:#1f1f1f;--sub:#595959;--mut:#6b6b6b;--mut2:#767676;--dis:#6b6b6b;--line:#e5e6eb;--line2:#f0f0f0;--acc:#4a6fa5;--accbg:#eef3f9;--ok:#2f6b45;--bad:#b23b40;--warn:#8c5a00;--r-s:6px;--r-m:10px;--r-p:999px;--g:8px;--p:12px 16px}
*{box-sizing:border-box;margin:0;padding:0;font-weight:400}
html{background:var(--bg)}
body{background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans",sans-serif;padding:12px}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
svg{width:16px;height:16px;flex:none}
.mono{font-family:Consolas,"Courier New",monospace}
.shell{max-width:1760px;margin:0 auto}
.head{background:var(--card);border:1px solid var(--line);border-radius:var(--r-m);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:8px;box-shadow:0 1px 2px rgba(15,23,42,.03)}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.brand-icon{width:32px;height:32px;display:flex;align-items:center;justify-content:center;color:var(--acc);background:var(--accbg);border-radius:var(--r-s);flex:none}
h1{font-size:18px}
.sub{color:var(--mut2);font-size:12px;margin-top:4px}
.live{display:flex;align-items:center;gap:8px;flex:none;font-size:12px;color:var(--sub)}
.live-dot{width:8px;height:8px;border-radius:var(--r-p);background:var(--ok)}
.live b{color:var(--sub)}
.live .ts{color:var(--mut);font-family:Consolas,monospace}
.kpis{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:8px;margin-top:8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--r-m);padding:12px 12px;display:block;box-shadow:0 1px 2px rgba(15,23,42,.03);transition:border-color .15s ease}
a.kpi:hover{border-color:var(--acc)}
.kicon{width:28px;height:28px;display:flex;align-items:center;justify-content:center;color:var(--acc);background:var(--accbg);border-radius:var(--r-s);margin-bottom:10px}
.kpi .lab{color:var(--mut2);font-size:12px}
.kpi .knum{display:block;font-size:18px;font-family:Consolas,monospace;font-variant-numeric:tabular-nums;color:var(--ink);margin-top:4px}
.kpi .knum.ok{color:var(--ok)}
.kpi .knum.bad{color:var(--bad)}
.kdelta{display:block;font-size:12px;color:var(--mut2);margin-top:2px;min-height:18px}
.spark{width:100%;height:22px;margin-top:6px;color:var(--acc);display:none}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r-m);padding:12px 16px;box-shadow:0 1px 2px rgba(15,23,42,.03)}
.panel h2{font-size:14px;margin-bottom:12px;display:flex;gap:8px;align-items:center;color:var(--ink)}
.mt{margin-top:20px}
.mt-s{margin-top:8px}
.pipe{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}
.col{background:var(--inset);border:1px solid var(--line2);border-radius:var(--r-s);padding:12px;min-height:130px}
.colh{color:var(--mut);font-size:12px;margin-bottom:8px}
.colh .cnt{color:var(--ink);font-family:Consolas,monospace;margin-left:4px}
.titem{display:flex;align-items:center;gap:6px;padding:3px 0;font-size:12px;color:var(--mut)}
.titem .dot{width:6px;height:6px;border-radius:var(--r-p);background:var(--bad);display:none;flex:none}
.titem.bad{color:var(--bad)}
.titem.bad .dot{display:inline-block}
.titem .lane{color:var(--mut);margin-left:auto}
.titem .age{color:var(--mut);font-size:12px;flex:none}
.two{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{height:32px;text-align:left;color:var(--sub);background:var(--inset);border-bottom:1px solid var(--line);font-weight:400}
td{height:36px;border-bottom:1px solid var(--line2);color:var(--sub);vertical-align:middle}
tbody tr:hover td{background:var(--bg)}
tr.trow td{background:var(--accbg);color:var(--acc);font-size:12px;padding:4px 8px}
#tb_dc td:nth-child(1),#tb_dc td:nth-child(2),#tb_sf td:nth-child(1),#tb_sf td:nth-child(2),#tb_runs td:nth-child(1),#tb_runs td:nth-child(2),#tb_autos td:nth-child(3),#tb_autos td:nth-child(4){white-space:nowrap}
#tb_dc td:nth-child(3),#tb_sf td:nth-child(5){display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ok{color:var(--ok)}.run{color:var(--sub)}.bad{color:var(--bad)}.mut{color:var(--mut)}.mut2{color:var(--mut2)}
.fold summary{cursor:pointer;color:var(--mut);font-size:12px}
.fold table{margin-top:8px}
.foot{color:var(--mut);font-size:12px;margin-top:12px}
.foot.mt{margin-top:8px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip{display:inline-flex;align-items:center;gap:4px;font-size:12px;border:1px solid var(--c,#e5e6eb);color:var(--c,#595959);background:var(--card);border-radius:var(--r-p);padding:2px 8px;max-width:100%}
.chip b{font-weight:400;white-space:nowrap}
.chip i{font-style:normal;font-weight:600;font-family:Consolas,monospace}
.chip em{font-style:normal;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:240px}
.chip.zero{color:var(--mut);border-color:var(--line2)}
.chip.unk{border-style:dashed;border-color:var(--bad);color:var(--bad)}
.q3{table-layout:fixed}
.q3 th:nth-child(1){width:31%}.q3 th:nth-child(2){width:29%}.q3 th:nth-child(3){width:22%}.q3 th:nth-child(4){width:18%}
.q3 td{height:auto;padding:8px 10px 8px 0;vertical-align:top;overflow-wrap:anywhere}
.ocode{color:var(--acc)}
.otitle{color:var(--ink);margin-top:2px}
.p0{display:inline-block;margin-left:4px;padding:0 4px;border:1px solid var(--bad);border-radius:3px;color:var(--bad);font-size:12px;line-height:16px}
.schip{display:inline-block;border:1px solid var(--c,#4a6fa5);color:var(--c,#4a6fa5);border-radius:var(--r-p);padding:1px 8px;white-space:nowrap}
.warnmark{display:inline-block;margin-left:6px;color:var(--warn);white-space:nowrap}
.mini{color:var(--sub);margin-top:4px}
.track{display:flex;gap:2px;margin-top:6px;max-width:220px}
.track i{display:block;height:6px;flex:1;background:var(--line);border-radius:2px}
.track i.on{background:var(--acc)}
.pnum{font-family:Consolas,monospace;color:var(--sub)}
.pnum b{font-size:18px;font-weight:600;color:var(--ink)}
.dwell{color:var(--mut);margin-top:2px}
.dwell.warn{color:var(--warn)}
.dwell.bad{color:var(--bad)}
/* C-20261120-004 详情面板 + 排产甘特（字号仅 12/14/18；新增色均已过白底 4.5） */
#detail{max-width:1100px}
.dtop{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.backlink{color:var(--acc);font-size:14px}
.dtitle{color:var(--ink)}
.dzone{margin-top:12px;border-top:1px solid var(--line2);padding-top:8px}
.dzone h3{font-size:14px;color:var(--ink);margin-bottom:8px}
.h8grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 20px}
.h8i{display:flex;gap:8px;padding:4px 0;border-bottom:1px solid var(--line2);min-width:0}
.h8k{color:var(--mut);font-size:12px;flex:none;width:72px}
.h8v{color:var(--ink);overflow-wrap:anywhere;min-width:0}
.dthree{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.dtasks{table-layout:fixed}
.dtasks th:nth-child(1){width:24%}.dtasks th:nth-child(2){width:9%}.dtasks th:nth-child(3){width:8%}.dtasks th:nth-child(4){width:19%}
.dtasks th:nth-child(5){width:11%}.dtasks th:nth-child(6){width:9%}.dtasks th:nth-child(7){width:11%}.dtasks th:nth-child(8){width:9%}
.dtasks td{height:auto;padding:8px 8px 8px 0;vertical-align:top;overflow-wrap:anywhere}
/* C-20261120-005 T04：§2.1 流程表 / 执行员工 / 会话号 / 签字 / 账实派生（字号仍仅 12/14/18） */
.dcrew{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px}
.citem{border:1px solid var(--line);border-radius:var(--r-s);padding:4px 10px;background:var(--inset);color:var(--sub)}
.citem b{color:var(--ink);font-weight:600}
.crewmpty{border:1px dashed var(--bad);border-radius:var(--r-s);padding:10px 14px;color:var(--bad);background:#fdf4f4;margin:0 0 8px}
.flowempty{border:1px dashed var(--line);border-radius:var(--r-s);padding:12px 16px;background:var(--inset);color:var(--mut)}
.dflowwrap{overflow-x:auto}
.dflow{min-width:980px;table-layout:fixed}
.dflow th:nth-child(1){width:4%}.dflow th:nth-child(2){width:17%}.dflow th:nth-child(3){width:10%}.dflow th:nth-child(4){width:7%}
.dflow th:nth-child(5){width:24%}.dflow th:nth-child(6){width:7%}.dflow th:nth-child(7){width:7%}
.dflow th:nth-child(8){width:13%}.dflow th:nth-child(9){width:11%}
.dflow td{height:auto;padding:8px 8px 8px 0;vertical-align:top;overflow-wrap:anywhere}
.full{font-family:Consolas,monospace;overflow-wrap:anywhere}
.sok{display:inline-block;margin-left:4px;border:1px solid var(--ok);color:var(--ok);border-radius:3px;padding:0 4px;font-size:12px;white-space:nowrap}
.sbad{display:inline-block;margin-left:4px;border:1px solid var(--bad);color:var(--bad);border-radius:3px;padding:0 4px;font-size:12px;white-space:nowrap}
.sunk{display:inline-block;margin-left:4px;border:1px solid var(--line);color:var(--mut2);border-radius:3px;padding:0 4px;font-size:12px;white-space:nowrap}
.smiss{color:var(--mut2)}
.pbar{display:inline-block;width:72px;height:6px;background:var(--line);border-radius:2px;vertical-align:middle;margin-right:6px}
.pbar i{display:block;height:6px;background:var(--ok);border-radius:2px}
.chat{list-style:none}
.chat li{display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:1px solid var(--line2)}
.cdot{width:8px;height:8px;border-radius:var(--r-p);background:var(--c,#4a6fa5);margin-top:6px;flex:none}
.ct{color:var(--mut);font-family:Consolas,monospace;font-size:12px;white-space:nowrap;flex:none}
.cseat{color:var(--acc);white-space:nowrap;flex:none}
.ctext{color:var(--ink);overflow-wrap:anywhere;min-width:0;flex:1}
.cto{color:var(--mut2);font-size:12px;white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis;flex:none}
.at{color:var(--acc);font-weight:400}
.dempty{padding:8px 0}
.gwrap{width:100%}
.gantt{table-layout:fixed;border-collapse:collapse}
.gantt col.glabcol{width:240px}
.gantt th,.gantt td{border:1px solid var(--line2);padding:4px;text-align:center;vertical-align:middle;overflow-wrap:anywhere}
.gantt thead th{height:32px;background:var(--inset);color:var(--sub);font-family:Consolas,monospace;font-size:12px}
.gantt th.ghead.predict{color:var(--mut2)}
.gantt th.ghead.lock{background:var(--accbg);color:var(--ink);font-weight:700;border-left:2px solid var(--acc)}
.gantt th.glab,.gantt td.glab{text-align:left;width:240px;background:var(--inset);color:var(--sub);font-size:12px;padding:6px 8px}
.gantt td.glab{height:auto}
.gteam{display:block;color:var(--ink)}
.gcomp{display:block;color:var(--mut);font-size:12px}
.gbadge{display:inline-block;margin-top:2px;border:1px solid var(--warn);color:var(--warn);border-radius:3px;padding:0 4px;font-size:12px}
.gantt tbody tr.cad td.glab{background:#f0f0f2}
.gantt td.gcell{height:auto;font-size:12px;color:var(--sub);background:var(--card)}
.gantt td.gcell.archived{color:var(--mut2)}
.gantt td.gcell.lock{background:#eef3f9}
.gantt td.gcell.locked{background:#dbe6f3;color:var(--ink);font-weight:600}
.gantt td.gcell.predicted{color:var(--mut);border-style:dashed;background:var(--card)}
.gantt td.gcell.absent,.gantt td.gcell.empty{color:var(--mut2)}
.ins{color:var(--bad);margin-right:2px}
.glegend{color:var(--mut);font-size:12px;margin-top:8px;line-height:1.8}
.gempty{border:1px dashed var(--line);border-radius:var(--r-s);padding:12px 16px;background:var(--inset)}
@media (max-width:1280px){.kpis{grid-template-columns:repeat(4,minmax(0,1fr))}.pipe{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:760px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.pipe{grid-template-columns:1fr}.two{grid-template-columns:1fr}.h8grid{grid-template-columns:1fr}.cto{display:none}}
</style></head><body data-skin="__SKIN__">
<div class="shell">
<div class="head">
<div class="brand"><span class="brand-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 21V8l7-5 7 5v13M10 21v-6h4v6"/><path d="M17 12h4v9"/></svg></span>
<div><h1>工厂 MES 看板</h1><div class="sub">ai-steward @ aa · Ant Design 浅色 · 图标全 SVG</div></div></div>
<div class="live"><span class="live-dot"></span><b>实时更新</b><span class="ts" id="v_ts">__TS__</span></div>
</div>
<div class="panel mt-s" id="panel_q3"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3 8-8"/><path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>实时三问（订单卡 · 首屏：走到哪一步 / 进度多少 / 谁卡着）</h2>
<div class="chips" id="v_states">__STATES__</div>
<table class="q3"><thead><tr><th>订单</th><th>① 走到哪一步</th><th>② 完成进度</th><th>③ 在谁手上</th></tr></thead><tbody id="tb_orders">__ORDERS__</tbody></table>
<div class="foot" id="v_ofoot">__OFOOT__</div></div>
<section class="panel mt" id="detail" hidden>
<div class="dtop"><a href="#" id="d_back" class="backlink">&larr; 返回看板</a><span id="d_code" class="mono ocode"></span><span id="d_title" class="dtitle"></span></div>
<div id="d_body" class="detail"></div>
</section>
<div class="panel mt" id="panel_gantt"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><rect x="7" y="5" width="4" height="3"/><rect x="11" y="10" width="6" height="3"/><rect x="7" y="15" width="9" height="3"/></svg>排产甘特（班组 × 小时 · 三段式：锁定 / 滚动 / 预判 · 对齐 03 §二）</h2>
<div id="v_gantt">__GANTT__</div></div>
<div class="kpis">
<a class="kpi" href="/cards/dispatched"><span class="kicon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5M2 12l10 5 10-5"/></svg></span><span class="lab">在制(已派单)</span><span class="knum" id="k_disp">__DISP__</span><svg class="spark" id="sp_disp" viewBox="0 0 100 24" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="2" points=""/></svg><span class="kdelta" id="kd_disp">__DL_DISP__</span></a>
<a class="kpi" href="/cards/verify"><span class="kicon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M9 12l2 2 4-4"/></svg></span><span class="lab">执行/验收中</span><span class="knum" id="k_run">__RUN__</span><svg class="spark" id="sp_run" viewBox="0 0 100 24" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="2" points=""/></svg><span class="kdelta" id="kd_run">__DL_RUN__</span></a>
<a class="kpi" href="/cards/done"><span class="kicon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 22V4h12l4 4v14H4z"/><path d="M9 12l2 2 4-4"/></svg></span><span class="lab">累计完成</span><span class="knum ok" id="k_done">__DONE__</span><svg class="spark" id="sp_done" viewBox="0 0 100 24" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="2" points=""/></svg><span class="kdelta" id="kd_done">__DL_DONE__</span></a>
<a class="kpi" href="/cards/queued"><span class="kicon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h10"/></svg></span><span class="lab">排队</span><span class="knum" id="k_queue">__QUEUE__</span><svg class="spark" id="sp_queue" viewBox="0 0 100 24" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="2" points=""/></svg><span class="kdelta" id="kd_queue">__DL_QUEUE__</span></a>
<a class="kpi" href="/cards/blocked"><span class="kicon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg></span><span class="lab">受阻/失败</span><span class="knum bad" id="k_blk">__BLK__</span><svg class="spark" id="sp_blk" viewBox="0 0 100 24" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="2" points=""/></svg><span class="kdelta" id="kd_blk">__DL_BLK__</span></a>
<div class="kpi"><span class="kicon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg></span><span class="lab">累计 token</span><span class="knum" id="k_tokens">__TOKENS__亿</span></div>
<div class="kpi"><span class="kicon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l8 3v5c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-3z"/><path d="M9 9l6 6M15 9l-6 6"/></svg></span><span class="lab">台账违规</span><span class="knum bad" id="k_viol">__VIOL__</span></div>
<div class="kpi"><span class="kicon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="8" r="4"/><path d="M2 21c0-4 3-6 7-6s7 2 7 6"/><circle cx="17" cy="9" r="3"/><path d="M22 21c0-3-2-5-4.5-5.5"/></svg></span><span class="lab">在册席位</span><span class="knum" id="k_sess">__SESS__</span></div>
</div>
<div class="panel mt"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h10M4 18h6"/></svg>任务卡流水线（registry 任务卡 · 模型道 DS/ST/NX/GL/OB · 红点=受阻 · 卡号可点）</h2>
<div class="pipe">
<div class="col" id="col_queued">__COL_QUEUED__</div>
<div class="col" id="col_dispatched">__COL_DISPATCHED__</div>
<div class="col" id="col_verify">__COL_VERIFY__</div>
<div class="col" id="col_done">__COL_DONE__</div>
<div class="col" id="col_blocked">__COL_BLOCKED__</div>
</div></div>
<div class="two">
<div class="panel"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>核心席位(点击看今日明细)</h2><table><tr><th>工号</th><th>席位</th><th>状态</th><th>档</th><th>最近更新</th></tr><tbody id="tb_seats">__SEATS__</tbody></table>
<div class="foot" id="v_team">__TEAM__</div></div>
<div class="panel"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>闹钟心跳(启用中)</h2><table><tr><th>闹钟</th><th>启停</th><th>轮次</th><th>上次-&gt;下次</th><th>错误</th></tr><tbody id="tb_autos">__AUTOS__</tbody></table>
<details class="fold mt-s"><summary>已停用 <span id="v_autosoffn" class="mono">__AUTOSOFF_N__</span> 条(点击展开/收起)</summary><table><tr><th>闹钟</th><th>启停</th><th>轮次</th><th>上次-&gt;下次</th><th>错误</th></tr><tbody id="tb_autos_off">__AUTOSOFF__</tbody></table></details></div>
</div>
<div class="panel mt"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>安全告警(未关闭 · 最新在上 · 已处理自动归档)</h2><table><tr><th>时间</th><th>编号</th><th>紧急</th><th>类别</th><th>对象/摘要</th><th>处理方式</th><th>处理结果</th></tr><tbody id="tb_sf">__SF__</tbody></table>
<div class="foot" id="v_sffoot">__SFFOOT__</div></div>
<div class="two">
<div class="panel"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 3v12a3 3 0 0 0 3 3h9"/><path d="M18 15l3 3-3 3"/></svg>决策时间线(白话版 · 悬停看原始记录)</h2><table><tr><th>时间</th><th>编号</th><th>决策内容(白话)</th></tr><tbody id="tb_dc">__DC__</tbody></table></div>
<div class="panel"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>调度运行记录(最近 12 次)</h2><table><tr><th>计划时刻</th><th>闹钟</th><th>结果</th><th>错误</th></tr><tbody id="tb_runs">__RUNS__</tbody></table></div>
</div>
<div class="foot mt">批文 PLAN_1H_APPROVED:<span id="v_ap">__AP__</span> · 平台遗留:zcode-site 宿主泄漏(建议 watchdog 清杀)/CreateSession 信任域受限/runs 偶发悬挂 · 生成器 scripts/mes_board.py · 设计 token:Ant Design</div>
</div>
<script>
function setT(id, v){ const e = document.getElementById(id); if(e) e.textContent = v; }
function setH(id, v){ const e = document.getElementById(id); if(e) e.innerHTML = v; }
var DLAB={order:'订单编号',created:'下单时间',requester:'发起人',priority:'优先级',eta:'预期交付',risk:'风险评级',budget:'预算',route:'排班'};
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function orderCode(){ var m=new URLSearchParams(location.search).get('order'); if(m) return m; var h=location.hash||''; if(h.indexOf('#order=')===0) return decodeURIComponent(h.slice(7)); return ''; }
function seatColor(seat){ var p=String(seat||'').charAt(0); var map={A:'#6b4fa8',B:'#4a6fa5',C:'#4a6fa5',D:'#2f6b45',E:'#8c5a00',F:'#b23b40',W:'#4a6fa5'}; return map[p]||'#6b6b6b'; }
function pct(s){ var m=String(s||'').match(/(\\d+)\\s*%/); return m?Math.max(0,Math.min(100,parseInt(m[1],10))):null; }
function sessTag(ok){ if(ok===true) return ' <span class="sok">真实</span>'; if(ok===false) return ' <span class="sbad">查无此会话</span>'; return ' <span class="sunk">未开席/未知</span>'; }
function fmtInt(n){ var s=String(n==null?'':n); return s.replace(/\\B(?=(\\d{3})+(?!\\d))/g, ','); }
function crewHTML(c){
  if(!c){ return '<div class="crewmpty" data-emp="0">未编成 · 无法派生真实会话（数据源缺失）</div>'; }
  if(!c.n_emp){ var why=c.db_ok?'卡内无真实执行员工会话':'会话库不可读，无法派生'; return '<div class="crewmpty" data-emp="0" data-sess-real="'+(c.sess_real||0)+'" data-sess-total="'+(c.sess_total||0)+'">未编成 · '+esc(why)+' · 不得以空表或 0 充数</div>'; }
  var emps=(c.employees||[]).join(' · ');
  var tok=c.db_ok?fmtInt(c.tokens):'—', tokE=c.db_ok?fmtInt(c.tokens_emp):'—';
  var sessTxt=c.db_ok?((c.sess_real||0)+'/'+(c.sess_total||0)+' 真实'):'未知（会话库不可读）';
  return '<div class="dcrew" id="d_crew" data-emp="'+c.n_emp+'" data-sess-real="'+(c.sess_real||0)+'" data-sess-total="'+(c.sess_total||0)+'" data-tokens="'+(c.db_ok?c.tokens:'')+'">'
   +'<span class="citem"><b>执行员工</b> '+c.n_emp+' 人 · '+esc(emps)+'</span>'
   +'<span class="citem"><b>会话</b> '+esc(sessTxt)+'</span>'
   +'<span class="citem" title="model_usage.computed_total_tokens（status != cancelled）；范围=卡面引用的真实会话"><b>token</b> '+tok+' <span class="smiss">（员工席 '+tokE+'）</span></span>'
   +'<span class="citem smiss">派生源 session + model_usage · 只读</span></div>';
}
function flowHTML(x){
  var f=x.flow||[], m=x.flow_meta||{};
  var z='<div class="dzone"><h3>③ §2.1 流程表（节点/责任席/执行员工/会话号/进入离开/结论/签字）</h3>'+crewHTML(x.crew);
  if(!m.has_table){ return z+'<div class="flowempty">卡内无 §2.1 流程表（缺表）——按 D-043「缺表不审」，不得视为已编成</div></div>'; }
  var trs='';
  for(var i=0;i<f.length;i++){ var r=f[i];
    trs+='<tr><td class="mono">'+esc(r.seq)+'</td><td>'+esc(r.node)+'</td><td class="mono">'+esc(r.to)+'</td><td class="mono">'+esc(r.emp)+'</td>'
      +'<td class="full">'+esc(r.sess)+sessTag(r.sess_ok)+'</td><td class="mono">'+esc(r.enter)+'</td><td class="mono">'+esc(r.leave)+'</td>'
      +'<td>'+esc(r.concl)+'</td><td'+(r.sign_ok?'':' class="smiss"')+'>'+esc(r.sign)+'</td></tr>'; }
  if(!f.length) trs='<tr><td colspan="9" class="mut">§2.1 表存在但无数据行（缺表）</td></tr>';
  return z+'<div class="dflowwrap"><table class="dflow"><thead><tr><th>序</th><th>流程节点</th><th>流到谁</th><th>执行员工</th><th>会话号</th><th>进入</th><th>离开</th><th>结论</th><th>签字</th></tr></thead><tbody>'+trs+'</tbody></table></div>'
   +'<div class="mini">「真实」= 该 sess_ 在 ZCode session 表存在；token = model_usage.computed_total_tokens 合计（status≠cancelled），员工 token 只计 W 席会话。</div></div>';
}
function detailHTML(x){
  var h=x.header8||{}, q=x.three_q||{}, ts=x.tasks||[], ch=x.chat||[];
  var hh=''; for(var k in DLAB){ if(!Object.prototype.hasOwnProperty.call(DLAB,k)) continue; hh+='<div class="h8i"><span class="h8k">'+DLAB[k]+'</span><span class="h8v">'+esc(h[k]||'—')+'</span></div>'; }
  var stage=parseInt(q.stage||0,10)||0, tr=''; for(var i=1;i<=10;i++){ tr+='<i class="'+(i<=stage?'on':'')+'"></i>'; }
  var maxq=parseInt(q.sub_m||0,10)||0, subq=parseInt(q.sub_n||0,10)||0, subpct=maxq?Math.round(subq*100/maxq):0;
  var z1='<div class="dzone"><h3>① 头部 8 项</h3><div class="h8grid">'+hh+'</div></div>';
  var z2='<div class="dzone"><h3>② 三问明细</h3><div class="dthree"><span class="schip" style="--c:'+esc(q.color||'#4a6fa5')+'">'+esc(q.state_cn||'未识别')+'</span><span class="pnum"><b>'+stage+'</b>/10</span><div class="track dtrack">'+tr+'</div><span class="mini">子需求 '+subq+'/'+maxq+'（'+subpct+'%）</span><span class="mini">在谁手上：'+esc((q.dept||'')+(q.dept&&q.seat?' · ':'')+(q.seat||'—'))+'</span><span class="dwell">停留 '+esc(q.dwell_txt||'—')+'</span></div></div>';
  var z3=flowHTML(x);
  var trs='';
  for(var j=0;j<ts.length;j++){ var t=ts[j], p=pct(t.progress), bar='';
    if(p===null){ bar='<span class="mut2">'+esc(t.progress||'—')+'</span>'; }
    else { bar='<span class="pbar"><i style="width:'+p+'%"></i></span><span class="pnum">'+p+'%</span>'; }
    trs+='<tr><td>'+esc(t.t)+'</td><td>'+esc(t.seat)+'</td><td class="mono">'+esc(t.emp||'—')+'</td>'
      +'<td class="full">'+esc(t.sess||'—')+sessTag(t.sess_ok)+'</td><td>'+esc(t.status)+'</td><td>'+bar+'</td>'
      +'<td'+(t.sign?'':' class="smiss"')+'>'+esc(t.sign||'—')+'</td><td class="mono">'+esc(t.ev||'—')+'</td></tr>';
  }
  if(!ts.length) trs='<tr><td colspan="8" class="mut">卡内 §9 待办表为空</td></tr>';
  var z4='<div class="dzone"><h3>④ §9 待办（全 '+ts.length+' 行 · 执行员工/会话号/签字）</h3><table class="dtasks"><thead><tr><th>待办</th><th>责任席</th><th>执行员工</th><th>会话号</th><th>状态</th><th>进度</th><th>签字</th><th>证据</th></tr></thead><tbody>'+trs+'</tbody></table></div>';
  var cs='';
  for(var m=0;m<ch.length;m++){ var c=ch[m]; var txt=esc(c.text||''); txt=txt.replace(/@([^\\s，。；、:|]+)/g,'<b class="at">@$1</b>');
    cs+='<li><span class="cdot" style="--c:'+seatColor(c.seat)+'"></span><span class="ct">'+esc(c.t)+'</span><span class="cseat mono">'+esc(c.seat)+'</span><span class="ctext">'+txt+'</span><span class="cto">'+esc(c.to||'')+'</span></li>';
  }
  if(!ch.length) cs='<li class="mut">group.md 无群聊记录（或文件缺失）</li>';
  var z5='<div class="dzone"><h3>⑤ 群聊流（倒序最近 '+ch.length+' 条 · 不含成员表）</h3><ul class="chat">'+cs+'</ul></div>';
  return z1+z2+z3+z4+z5;
}
function updateDetail(d){
  var code=orderCode(), sec=document.getElementById('detail');
  if(!sec) return;
  if(!code){ sec.hidden=true; return; }
  sec.hidden=false;
  var x=(d.details||{})[code], body=document.getElementById('d_body');
  document.getElementById('d_code').textContent=code;
  document.getElementById('d_title').textContent=x?(x.title||''):'';
  if(!x){ var ks=Object.keys(d.details||{}); body.innerHTML='<div class="dempty"><b>未找到该订单 '+esc(code)+'</b><div class="mini">已加载订单：'+(ks.length?ks.map(function(v){return esc(v);}).join(' · '):'（无）')+'</div><div class="mini">链接格式：/?k=…#order=&lt;订单号&gt;（不新增路由，鉴权同首屏）</div></div>'; }
  else { body.innerHTML=detailHTML(x); }
}
async function tick(){
  try{
    const d = await (await fetch('/data.json?'+Date.now(),{cache:'no-store'})).json();
    if(d.skin && document.body.getAttribute('data-skin') !== d.skin){
      if(sessionStorage.getItem('skin') !== d.skin){
        sessionStorage.setItem('skin', d.skin);
        location.reload();
        return;
      }
      document.body.setAttribute('data-skin', d.skin);
    }
    setT('v_ts', d.ts);
    if(typeof d.states_html === 'string') setH('v_states', d.states_html);
    if(typeof d.orders_html === 'string') setH('tb_orders', d.orders_html);
    if(typeof d.ofoot === 'string') setT('v_ofoot', d.ofoot);
    const k = d.kpis;
    setT('k_disp', k.disp); setT('k_run', k.run); setT('k_done', k.done); setT('k_queue', k.queue);
    setT('k_blk', k.blk); setT('k_tokens', k.tokens+'亿'); setT('k_viol', k.viol); setT('k_sess', k.sess);
    if(d.deltas){ setT('kd_disp', d.deltas.disp); setT('kd_run', d.deltas.run); setT('kd_done', d.deltas.done); setT('kd_queue', d.deltas.queue); setT('kd_blk', d.deltas.blk); }
    if(d.series){ for(const c of ['disp','run','done','queue','blk']){ const arr=d.series[c]||[]; const el=document.getElementById('sp_'+c); if(!el) continue; if(arr.length>1){ const mx=Math.max.apply(null,arr.concat([1])); const pts=arr.map((v,i)=>((i*100/(arr.length-1)).toFixed(1))+','+((22-(v/mx)*20)+1).toFixed(1)).join(' '); const pl=el.querySelector('polyline'); if(pl) pl.setAttribute('points',pts); el.style.display='block'; } else { el.style.display='none'; } } }
    for(const c of d.cols){ setH('col_'+c.key, c.html); }
    setH('tb_seats', d.seats);
    setT('v_team', d.team);
    setH('tb_autos', d.autos);
    setH('tb_autos_off', d.autosoff);
    setT('v_autosoffn', d.autosoffn);
    setH('tb_runs', d.runs);
    setH('tb_sf', d.sf);
    setT('v_sffoot', d.sffoot);
    setH('tb_dc', d.dc);
    setT('v_ap', d.ap);
    if(typeof d.gantt_html === 'string') setH('v_gantt', d.gantt_html);
    if(d.details) updateDetail(d);
  }catch(e){}
}
setInterval(tick, 2000);
var dBtn=document.getElementById('d_back');
if(dBtn){ dBtn.addEventListener('click', function(e){ e.preventDefault(); var u=new URL(location.href); u.searchParams.delete('order'); history.replaceState(null,'',u.pathname+u.search); if(location.hash){ location.hash=''; } var sec=document.getElementById('detail'); if(sec) sec.hidden=true; window.scrollTo(0,0); tick(); }); }
window.addEventListener('hashchange', function(){ tick(); });
tick();
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# C-20261120-009 七区真数据 UI（契约 c009-zones-v1；只读 board_zones 数据层）
# 设计口径：暗色工业风；一屏三问；红黄置顶；数字可溯源角标；缺源灰条不补 0。
# ---------------------------------------------------------------------------
UI_CSS = r"""
:root{--bg:#0a0d11;--panel:#111720;--panel2:#0e131a;--line:#24303d;--line2:#1a232e;
--tx:#e9eff6;--mut:#9fadbd;--dim:#7d8b9c;--gry:#8d9aab;--red:#ff6b6b;--yel:#ffc857;
--grn:#45d483;--blu:#6fb6ff;--mono:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}
html{color-scheme:dark}
html,body{margin:0;padding:0;max-width:100%;overflow-x:hidden;background:var(--bg);color:var(--tx);
font:13px/1.45 -apple-system,"Segoe UI","Microsoft YaHei",Arial,sans-serif}
body{padding-bottom:28px}
body.lock{overflow:hidden}
.mono{font-family:var(--mono)}
.mut{color:var(--mut)}
.sm{font-size:10px}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:700}
.num[data-source]{border-bottom:1px dotted #536274;cursor:help}
.num[data-source]:hover{color:var(--yel)}
.src{display:inline-flex;align-items:center;justify-content:center;min-width:15px;height:15px;
padding:0 3px;border:1px solid #3a4757;border-radius:3px;color:var(--mut);
font:9px/1 var(--mono);cursor:help;vertical-align:middle;flex:none}
.src:hover{border-color:var(--yel);color:var(--yel)}
.src:hover::after,.num[data-source]:hover::after{content:attr(data-source);position:fixed;left:16px;right:16px;
bottom:14px;z-index:200;background:#1b2430;border:1px solid #3d4b5c;border-radius:5px;padding:6px 10px;
color:#e9eff6;font:11px/1.5 var(--mono);white-space:normal;pointer-events:none;
box-shadow:0 8px 30px rgba(0,0,0,.6)}
#alertbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:8px;min-height:42px;
padding:7px 16px;border-bottom:1px solid var(--line);background:#121821;font-size:13px;cursor:pointer;
white-space:nowrap;overflow:hidden}
#alertbar[data-level="red"]{background:#2a1114;border-bottom-color:#7a2a2f;color:#ffd9d9}
#alertbar[data-level="yellow"]{background:#241f0f;border-bottom-color:#6b5720;color:#ffe9b8}
#alertbar[data-level="green"]{background:#0f1d16;border-bottom-color:#245c3d;color:#c9f5dc}
#alertbar[data-level="gray"]{background:#151a21;border-bottom-color:#3a4655;color:#c3ccd7}
#alertbar[data-level="blue"]{background:#0d1e2a;border-bottom-color:#2a5a80;color:#cfe6ff}
.ab-blue{color:var(--blu)}
#alertbar b{font-weight:800}
.ab-ico{font-size:15px;flex:none}
.ab-sep{opacity:.55;flex:none}
.ab-msg{overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1}
.top{display:flex;align-items:center;gap:12px;padding:8px 16px;min-height:44px;
border-bottom:1px solid var(--line2);background:linear-gradient(180deg,#101720,#0c1118)}
.brand{font-weight:800;letter-spacing:.6px;font-size:14px;white-space:nowrap}
.brand span{color:var(--mut);font-weight:600}
.live{display:flex;align-items:center;gap:5px;font:10px/1 var(--mono);color:var(--grn);
border:1px solid #245c3d;border-radius:9px;padding:2px 7px;flex:none}
.live i{width:6px;height:6px;border-radius:50%;background:var(--grn);animation:pl 1.8s infinite}
@keyframes pl{0%,100%{opacity:1}50%{opacity:.35}}
.ts,.cd{font-size:11px;color:var(--mut);white-space:nowrap}
.ts b,.cd b{color:var(--tx);font-family:var(--mono);font-weight:700}
.spacer{flex:1}
.btn{background:#1a2330;color:var(--tx);border:1px solid #344355;border-radius:4px;
padding:4px 9px;font-size:11px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--yel);color:var(--yel)}
.questions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:8px 16px 4px}
.q{background:linear-gradient(180deg,#121a24,#0f151d);border:1px solid var(--line);
border-left:3px solid var(--line);border-radius:6px;padding:8px 10px;min-height:74px;min-width:0}
.q1{border-left-color:var(--grn)}.q2{border-left-color:var(--yel)}.q3{border-left-color:var(--gry)}
.q h2{margin:0 0 2px;font-size:10px;line-height:14px;letter-spacing:.8px;color:var(--mut);font-weight:600;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qbig{font-size:24px;line-height:1.1;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qbig .num{font-size:24px}
.qbig .slash{color:var(--dim);font-weight:400;margin:0 2px}
.uq{font-size:11px;color:var(--mut);font-weight:600}
.qsub{font-size:11px;line-height:16px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.miss{font-size:15px;color:var(--gry);font-weight:700}
.zones{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:8px;padding:2px 16px 14px}
.zone{background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden;min-width:0}
.z-orders,.z-handover{grid-column:span 12}
.z-seats,.z-events,.z-alerts{grid-column:span 4}
.z-capacity,.z-cost{grid-column:span 6}
.zh{display:flex;align-items:center;gap:7px;padding:7px 10px;border-bottom:1px solid var(--line2);
background:var(--panel2);min-width:0}
.zh h2{margin:0;font-size:12px;font-weight:700;letter-spacing:.3px;white-space:nowrap;flex:none}
.zno{color:var(--dim);font-family:var(--mono);margin-right:4px}
.zst{font-size:10px;line-height:16px;padding:0 6px;border-radius:8px;border:1px solid var(--line);
color:var(--mut);flex:none}
.zst.ok{color:var(--grn);border-color:#245c3d}
.zst.degraded{color:var(--yel);border-color:#5c4a1e}
.zst.missing{color:var(--gry);border-color:#3a4655}
.wip{font-size:9px;line-height:14px;padding:0 5px;margin-left:4px;border:1px dashed #8a6d2a;border-radius:3px;
color:#d8b45a;background:#1b1a12;white-space:nowrap}
.z-heartbeat{grid-column:span 12}
.z-heartbeat .seats{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.zsub{margin-left:auto;color:var(--mut);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.zb{padding:7px 10px 9px;min-width:0}
.zbar{display:flex;gap:8px;align-items:baseline;margin:4px 0 7px;padding:6px 8px;border:1px dashed #3a4655;
border-radius:4px;background:#161b24;color:var(--mut);font-size:11px;line-height:1.45}
.zbar b{color:var(--gry);white-space:nowrap;flex:none}
.zbar span{min-width:0}
.zbar.degraded{border-color:#4a4020;background:#1b1a12}
.zbar.degraded b{color:var(--yel)}
.st{display:inline-flex;align-items:center;padding:0 6px;border-radius:3px;border:1px solid;font-size:10px;
line-height:16px;white-space:nowrap;font-weight:600}
.st.red{color:var(--red);border-color:#6d2b2f;background:#241316}
.st.yellow{color:var(--yel);border-color:#5c4a1e;background:#211d10}
.st.green{color:var(--grn);border-color:#245c3d;background:#0f1d16}
.st.gray{color:var(--gry);border-color:#3a4655;background:#151a21}
.st.blue{color:var(--blu);border-color:#2a5a80;background:#0d1e2a}
.red{color:var(--red)}.yellow{color:var(--yel)}.green{color:var(--grn)}.gray{color:var(--gry)}
.otwrap{max-height:304px;overflow:auto;border:1px solid var(--line2);border-radius:4px}
.ot{width:100%;border-collapse:collapse;table-layout:fixed;font-size:11px}
.ot th{position:sticky;top:0;z-index:1;background:#131b25;color:var(--mut);font-weight:600;text-align:left;
padding:5px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.ot td{padding:5px 6px;border-bottom:1px solid var(--line2);vertical-align:middle;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.ot tbody tr{cursor:pointer}
.ot tbody tr:hover td{background:#18202b}
.ot tbody tr.blue td{background:#0d1b28}
.ot tbody tr.blue:hover td{background:#122436}
.ot tbody tr.blue td:first-child{box-shadow:inset 2px 0 0 #3b82c4}
.dlb{display:inline-block;margin-left:5px;padding:0 5px;border:1px solid #2a5a80;border-radius:3px;
background:#0d1e2a;color:var(--blu);font:10px/16px var(--mono);white-space:nowrap;vertical-align:middle}
.lgd{font-size:10px;color:var(--mut);margin-left:8px}
.lgd.blue{color:var(--blu)}
.ot tbody tr:last-child td{border-bottom:0}
.p0{font:900 9px/1 var(--mono);color:#1b1205;background:var(--yel);border-radius:2px;padding:1px 3px;margin-left:3px}
.states{display:flex;flex-wrap:wrap;gap:4px;padding:7px 0 0}
.sx{font-size:10px;line-height:16px;color:var(--mut);border:1px solid var(--line2);border-radius:3px;
padding:0 5px;white-space:nowrap}
.sx b{font-family:var(--mono);color:var(--tx);font-weight:700}
.sx.red{color:var(--red);border-color:#5e282c}.sx.red b{color:var(--red)}
.sx.yellow{color:var(--yel);border-color:#4f411d}.sx.yellow b{color:var(--yel)}
.sx.green{color:var(--grn);border-color:#24503a}.sx.green b{color:var(--grn)}
.seats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;max-height:342px;overflow:auto;
scrollbar-width:thin;scrollbar-color:#2b3746 transparent}
.seat{border:1px solid var(--line2);border-radius:4px;padding:6px 7px;background:#0f151d;min-width:0}
.seat .sid{font-family:var(--mono);font-size:11px;font-weight:700;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
.seat .meta{font-size:10px;line-height:15px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.seat .meta b{color:var(--tx)}
.seat .chan .src{font-size:9px;margin-left:2px}
.seat .zbar.chmiss{margin:2px 0 0;padding:2px 5px;font-size:9px;line-height:12px;gap:4px}
.seat .zbar.chmiss b{font-size:9px}
.list{max-height:300px;overflow:auto;border-top:1px solid var(--line2);
scrollbar-width:thin;scrollbar-color:#2b3746 transparent}
.ev{display:grid;grid-template-columns:52px minmax(0,1fr);gap:6px;padding:4px 1px;border-bottom:1px solid var(--line2);
font-size:11px;line-height:1.35;min-width:0}
.ev>div{min-width:0}
.ev:last-child{border-bottom:0}
.et{color:var(--mut);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.art{font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.caprow{display:flex;justify-content:space-between;gap:8px;font-size:11px;padding:2px 0;color:var(--mut)}
.caprow b{color:var(--tx);font-weight:700}
.lanes{margin:5px 0 4px}
.lane{display:grid;grid-template-columns:56px minmax(0,1fr) 58px;gap:6px;align-items:center;
font-size:11px;padding:2px 0;min-width:0}
.lane .r{text-align:right;color:var(--tx);font-size:10px}
.bar{height:6px;background:#1b2430;border-radius:3px;overflow:hidden;min-width:0}
.bar i{display:block;height:100%;background:var(--grn);border-radius:3px}
.bar i.warn{background:var(--yel)}.bar i.full{background:var(--red)}
.pool{margin-top:5px;padding-top:5px;border-top:1px solid var(--line2);font-size:10px;line-height:1.55;color:var(--mut)}
.costbig{font-size:20px;line-height:1.2;font-weight:800;margin:1px 0 3px;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
.costbig .num{font-size:20px}
.foot{font-size:10px;line-height:1.5;color:var(--dim);padding-top:5px}
.okbox{font-size:12px;color:var(--grn);border:1px solid #245c3d;background:#0f1d16;border-radius:4px;
padding:7px 9px;margin:4px 0}
.al{display:grid;grid-template-columns:22px minmax(0,1fr);gap:7px;padding:5px 1px;border-bottom:1px solid var(--line2);
font-size:11px;line-height:1.4;min-width:0}
.al>div{min-width:0;overflow-wrap:anywhere}
.al:last-child{border-bottom:0}
.al .lv{font:700 10px/16px var(--mono);text-align:center;border-radius:3px;height:16px;margin-top:1px}
.al.red .lv{color:var(--red);border:1px solid #6d2b2f}
.al.yellow .lv{color:var(--yel);border:1px solid #5c4a1e}
.al.info .lv{color:var(--gry);border:1px solid #3a4655}
.al.blue .lv{color:var(--blu);border:1px solid #2a5a80;background:#0d1e2a}
.holine{font-size:11px;line-height:1.55;color:var(--mut);padding:1px 0;border-bottom:1px solid var(--line2);
overflow-wrap:anywhere}
.holine:last-child{border-bottom:0}
#drill{position:fixed;inset:0;z-index:80;display:flex;justify-content:flex-end;background:rgba(3,6,10,.66)}
#drill[hidden]{display:none}
.drillbox{width:min(780px,95vw);height:100%;overflow:auto;background:#0d1219;border-left:1px solid var(--line);
padding:12px 16px 40px;box-shadow:-20px 0 60px rgba(0,0,0,.55)}
.drillhead{display:flex;gap:10px;align-items:flex-start;position:sticky;top:0;z-index:2;background:#0d1219;
padding:2px 0 8px;border-bottom:1px solid var(--line2)}
.dtitle{font-size:16px;font-weight:800;line-height:1.3}
.dmeta{font-size:11px;line-height:1.5;color:var(--mut);margin-top:2px}
.x{margin-left:auto;background:#1a2330;color:var(--tx);border:1px solid #344355;border-radius:4px;
padding:5px 10px;cursor:pointer;font-size:12px;flex:none}
.x:hover{border-color:var(--red);color:var(--red)}
.q4{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:10px 0 2px}
.ans{border:1px solid var(--line2);border-radius:4px;padding:7px 9px;background:#111820;min-width:0}
.ans .al{display:block;font-size:10px;color:var(--mut);letter-spacing:.3px;border:0;padding:0;line-height:1.4}
.ans .av{font-size:12px;font-weight:700;margin-top:3px;overflow-wrap:anywhere;line-height:1.45}
.sec{margin:12px 0 5px;font-size:12px;font-weight:700;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.sec .mut{font-weight:400;font-size:10px}
.flowwrap{overflow:auto;border:1px solid var(--line2);border-radius:4px;max-height:340px}
.ft{width:100%;border-collapse:collapse;font-size:10.5px;min-width:620px}
.ft th{position:sticky;top:0;z-index:1;background:#131b25;color:var(--mut);font-weight:600;text-align:left;
padding:4px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.ft td{padding:4px 6px;border-bottom:1px solid var(--line2);vertical-align:top;white-space:nowrap}
.ft tbody tr:last-child td{border-bottom:0}
.ft td.nd{white-space:normal;min-width:150px;line-height:1.4}
.ft tr.cur td{background:#211f12;box-shadow:inset 3px 0 0 var(--yel)}
.ft .mut{font-size:9.5px}
@media(max-width:980px){
.z-seats,.z-events,.z-alerts,.z-capacity,.z-cost{grid-column:span 12}
.questions{grid-template-columns:1fr;gap:5px}
.q{min-height:0;padding:6px 10px}
.qbig,.qbig .num{font-size:20px}
.qsub{white-space:normal}
.seats{grid-template-columns:1fr}
.zsub{display:none}
}
@media(max-width:700px){
.top{gap:8px;padding:7px 10px;flex-wrap:wrap}
.ts{display:none}
#alertbar{padding:6px 10px;font-size:12px}
.questions,.zones{padding-left:10px;padding-right:10px}
.otwrap{max-height:none;border:0}
.ot thead{display:none}
.ot,.ot tbody{display:block}
.ot tr{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1px 8px;padding:6px 2px;
border-bottom:1px solid var(--line)}
.ot td{border:0;padding:1px 0;white-space:normal;display:flex;gap:5px;min-width:0}
.ot td::before{content:attr(data-label);color:var(--mut);font-size:10px;min-width:38px;flex:none}
.ot td.c-code{grid-column:span 2;font-weight:800}
.ot td.c-title{grid-column:span 2}
.ot td.c-node{grid-column:span 2}
.q4{grid-template-columns:1fr}
.drillbox{width:100vw;border-left:0;padding:10px 10px 40px}
.ft{min-width:560px}
}
@media(prefers-reduced-motion:reduce){.live i{animation:none}}
"""

UI_JS = r"""
var DETAILS=__DETAILS_JSON__, REFRESH=__REFRESH__, left=REFRESH, cur='';
function byId(id){return document.getElementById(id);}
function jumpTo(id){var el=byId(id);if(el){el.scrollIntoView({behavior:'smooth',block:'start'});}}
function paint(d){
  if(!d){return;}
  var ui=d.ui||{};
  var ab=byId('alertbar');
  if(ab&&ui.alert_level){ab.setAttribute('data-level',ui.alert_level);}
  if(ab&&ui.alertbar!==undefined){ab.innerHTML=ui.alertbar;}
  if(ui.questions){byId('questions').innerHTML=ui.questions;}
  if(ui.zones){byId('zones').innerHTML=ui.zones;}
  if(ui.details){DETAILS=ui.details;}
  if(ui.ts){byId('ts').textContent=ui.ts;}
  if(cur&&DETAILS[cur]){byId('drill_body').innerHTML=DETAILS[cur];}
  else if(cur&&!DETAILS[cur]){closeDrill();}
}
function pollNow(){
  fetch('/data.json?_='+Date.now(),{cache:'no-store'}).then(function(r){
    if(!r.ok){throw new Error('HTTP '+r.status);}
    return r.json();
  }).then(function(d){paint(d);left=REFRESH;var c=byId('cd');if(c){c.textContent=left;}})
  .catch(function(e){var t=byId('ts');if(t){t.textContent='数据源异常 · 重试中';}});
}
function openDrill(code){
  if(!DETAILS[code]){return;}
  cur=code;
  byId('drill_body').innerHTML=DETAILS[code];
  byId('drill').hidden=false;
  document.body.classList.add('lock');
  try{history.replaceState(null,'','#order='+encodeURIComponent(code));}catch(_){}
}
function closeDrill(){
  cur='';
  var dr=byId('drill');if(dr){dr.hidden=true;}
  document.body.classList.remove('lock');
  if(location.hash){try{history.replaceState(null,'',location.pathname+location.search);}catch(_){}}
}
document.addEventListener('click',function(e){
  var c=e.target.closest('[data-code]');
  if(c){openDrill(c.getAttribute('data-code'));return;}
  if(e.target.closest('[data-close]')){closeDrill();return;}
  if(e.target.id==='drill'){closeDrill();}
});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){closeDrill();}});
window.addEventListener('hashchange',function(){
  var m=/^#order=(.+)$/.exec(location.hash||'');
  if(m){openDrill(decodeURIComponent(m[1]));}else{closeDrill();}
});
setInterval(pollNow,REFRESH*1000);
setInterval(function(){left=Math.max(0,left-1);var c=byId('cd');if(c){c.textContent=left;}},1000);
(function(){var m=/^#order=(.+)$/.exec(location.hash||'');if(m){openDrill(decodeURIComponent(m[1]));}})();
"""

UI_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>MES 工厂面板 · 七区真数据</title>
<style>__UI_CSS__</style></head>
<body data-skin="__SKIN__" data-contract="__CONTRACT__">
<div id="alertbar" data-level="__ALERT_LEVEL__" onclick="jumpTo('z-alerts')" title="点击跳到告警区">__ALERTBAR__</div>
<header class="top">
  <div class="brand">MES <span>工厂面板</span></div>
  <div class="live"><i></i>LIVE</div>
  <div class="ts">更新 <b id="ts">__TS__</b></div>
  <div class="cd">刷新 <b id="cd">__REFRESH__</b>s</div>
  <div class="spacer"></div>
  <button class="btn" type="button" onclick="pollNow()">立即刷新</button>
  <span class="src" data-source="契约 __CONTRACT__ · /data.json（每 __REFRESH__s 轮询）" title="契约 __CONTRACT__">源</span>
</header>
<main>
<section class="questions" id="questions">__QUESTIONS__</section>
<section class="zones" id="zones">__ZONES__</section>
</main>
<div id="drill" hidden><div class="drillbox" role="dialog" aria-modal="true" aria-label="订单钻取">
  <div id="drill_body"></div>
</div></div>
<script>__UI_JS__</script>
</body></html>
"""


ZONE_KEYS = ("orders", "seats", "events", "capacity", "alerts", "cost", "handover")
REFRESH_SEC = 60


def _zone_default(note):
    return {"status": "missing", "as_of": "", "source": "", "sources": [],
            "summary": {}, "items": [], "note": note}


def _collect_zones_safe():
    """取七区数据；数据层异常时整页降级为七个灰条，绝不补 0/造假数组。"""
    try:
        import board_zones
        z = board_zones.collect_zones()
        if not isinstance(z, dict) or z.get("contract") != "c009-zones-v1":
            raise ValueError("契约不符：%r" % (z.get("contract") if isinstance(z, dict) else type(z).__name__))
        zones = z.get("zones") or {}
        for k in ZONE_KEYS:
            if not isinstance(zones.get(k), dict):
                zones[k] = _zone_default("数据层未返回该区")
        z["zones"] = zones
        return z
    except Exception as e:
        note = "数据层不可用：%s: %s" % (type(e).__name__, str(e)[:160])
        return {"contract": "c009-zones-v1",
                "generated_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "zones": {k: _zone_default(note) for k in ZONE_KEYS},
                "zone_status": {k: "missing" for k in ZONE_KEYS},
                "sources": {k: [] for k in ZONE_KEYS}, "error": note}


def _zfmt_min(v):
    if v is None or v == "":
        return "—"
    try:
        m = int(round(float(v)))
    except (TypeError, ValueError):
        return _esc(v)
    if m < 0:
        return "—"
    if m < 60:
        return "%dm" % m
    return "%dh%02dm" % (m // 60, m % 60)


def _zfmt_tok(v):
    if v is None or v == "":
        return "—"
    if isinstance(v, str):
        return _esc(v)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return _esc(v)
    if n >= 100000000:
        return "%.2f 亿" % (n / 100000000.0)
    if n >= 10000:
        return "%.1f 万" % (n / 10000.0)
    return "{:,}".format(n)


def _zsrc(zone, extra=""):
    srcs = zone.get("sources") or [zone.get("source") or ""]
    srcs = [str(s) for s in srcs if s]
    if extra:
        srcs.append(str(extra))
    if not srcs:
        return ""
    s = " · ".join(srcs)
    return '<span class="src" data-source="%s" title="%s">源</span>' % (_esc(s), _esc(s))


def _znum(val, src, cls="", title=""):
    t = title or src
    return ('<b class="num %s" data-source="%s" title="%s">%s</b>'
            % (cls, _esc(src), _esc(t), val))


def _zsess(s):
    s = str(s or "")
    m = re.search(r"sess_([0-9a-fA-F]{8})", s)
    if m:
        return m.group(1)
    return (s[:13] if s else "—")


def _zsess_tag(s):
    z = _zsess(s)
    return z if z == "—" else "sess_" + z


def _clean_md(s):
    return re.sub(r"[*`]+", "", str(s or ""))


def _zstate_cls(o):
    st0 = str(o.get("state_raw") or o.get("state") or "")
    if o.get("delay_note") and st0 not in _TERMINAL:
        return "blue"
    if o.get("overdue"):
        try:
            x = float(o.get("sla_x") or 0)
        except (TypeError, ValueError):
            x = 0
        return "red" if x >= 2.0 else "yellow"
    if o.get("warn"):
        return "yellow"
    st = str(o.get("state_raw") or o.get("state") or "")
    if st in ("delivered", "accepted"):
        return "green"
    if st in ("returned", "blocked", "failed", "void"):
        return "red"
    if st in ("suspended", "compensating", "awaiting-release", "awaiting-closure"):
        return "yellow"
    if st == "created":
        return "gray"
    return "green"


def _zst_chip(o, extra=""):
    cls = _zstate_cls(o)
    return '<span class="st %s">%s%s</span>' % (cls, _esc(o.get("state_cn") or o.get("state") or "?"), extra)


def _norm_sign(v):
    """签字字段归一：'—'/''/'-' 等占位不算已签。"""
    s = str(v or "").strip()
    return "" if s in ("", "—", "-", "–", "无", "未签", "None") else s


def _zone_has_data(zone):
    """区里是否真有可展示数据：items 非空，或非 missing 且班次已实算（capacity 无 items）。"""
    if zone.get("items"):
        return True
    sm = zone.get("summary") or {}
    return ((zone.get("status") or "") != "missing"
            and bool(sm.get("shift")) and sm.get("remain_min") is not None)


def _zhead(no, title, zone, sub=""):
    st = zone.get("status") or "missing"
    if st != "ok" and not _zone_has_data(zone):
        st = "missing"
    st_cn = {"ok": "实源", "degraded": "部分接入", "missing": "缺源"}.get(st, st)
    wip = '<span class="wip">施工中</span>' if st != "ok" else ""
    return ('<div class="zh"><h2><span class="zno">%s</span>%s</h2>'
            '<span class="zst %s">%s</span>%s%s<span class="zsub">%s</span></div>'
            % (no, _esc(title), _esc(st), _esc(st_cn), wip, _zsrc(zone), _esc(sub)))


def _zbar(zone, missing_txt="数据源建设中"):
    st = zone.get("status") or "missing"
    if st == "ok":
        return ""
    if st != "missing" and _zone_has_data(zone):
        tag = "部分字段未接入"
    else:
        st = "missing"
        tag = missing_txt
    note = zone.get("note") or "无说明"
    return '<div class="zbar %s"><b>%s</b><span>%s</span></div>' % (_esc(st), _esc(tag), _esc(note))


def _limit_min(txt):
    if not txt:
        return None
    t = str(txt)
    m = re.search(r"(\d+)\s*h\s*(\d+)", t, re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*h", t, re.I)
    if m:
        return float(m.group(1)) * 60
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|分钟)", t, re.I)
    if m:
        return float(m.group(1))
    return None


def _s20_table(path):
    """解析卡 §2.0 节点时限表：T 号 -> 时限分钟；缺表返回 {}。"""
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except (OSError, TypeError):
        return {}
    m = re.search(r"###\s*2\.0(.*?)(?=\n###\s*2\.1|\n##\s|\Z)", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        tm = re.match(r"\**\s*(T\d+)", cells[0])
        if not tm:
            continue
        out[tm.group(1)] = {"node": cells[0], "seat": cells[1],
                            "plan_due": cells[4] if len(cells) > 4 else "",
                            "limit_txt": cells[5], "limit": _limit_min(cells[5]),
                            "status": cells[7] if len(cells) > 7 else ""}
    return out


def _base_day(det):
    s = ((det or {}).get("header8") or {}).get("created") or ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _hhmm(s, base):
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(s or ""))
    if not m:
        return None
    try:
        return base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0))
    except ValueError:
        return None


def _flow_view(det, s20):
    """§2.1 流程 → 停留/时限/倍数/级别；当前节点=首个未完成节点。"""
    base = _base_day(det)
    now = datetime.datetime.now()
    rows = []
    for r in ((det or {}).get("flow") or []):
        enter = _hhmm(r.get("enter"), base)
        leave = _hhmm(r.get("leave"), base)
        if enter and leave and leave < enter:
            leave += datetime.timedelta(days=1)
        if enter and enter > now + datetime.timedelta(hours=6):
            enter -= datetime.timedelta(days=1)
            if leave:
                leave -= datetime.timedelta(days=1)
        done = bool(leave or r.get("sign_ok"))
        end = leave or (now if enter else None)
        mins = int((end - enter).total_seconds() // 60) if (enter and end and end >= enter) else None
        tm = re.match(r"\s*(T\d+)", r.get("node") or "")
        tcode = tm.group(1) if tm else ""
        lim = (s20.get(tcode) or {}).get("limit")
        x = (float(mins) / lim) if (mins is not None and lim) else None
        if done:
            lvl = "gray" if x is None else ("green" if x <= 1.0 else ("red" if x >= 2.0 else "yellow"))
        else:
            lvl = "gray" if x is None else ("red" if x >= 2.0 else ("yellow" if x >= 1.5 else "green"))
        rows.append({"seq": r.get("seq") or "", "node": r.get("node") or "",
                     "seat": r.get("to") or "", "emp": r.get("emp") or "",
                     "sess": r.get("sess") or "", "enter": r.get("enter") or "",
                     "leave": r.get("leave") or "", "sign": r.get("sign") or "",
                     "sign_ok": bool(r.get("sign_ok")), "concl": r.get("concl") or "",
                     "done": done, "mins": mins, "limit": lim, "x": x,
                     "limit_txt": (s20.get(tcode) or {}).get("limit_txt") or "",
                     "level": lvl, "tcode": tcode})
    cur = None
    for r in rows:
        if not r["done"]:
            cur = r
            break
    if cur is None and rows:
        cur = rows[-1]
    return rows, cur


def _delay_new_deadline(text):
    """从卡 §10/§3 原文取顺延后的新时限 HH:MM（MES/08 合理延期）。

    取全文最靠后的一处：同一张卡可多次顺延，后一次才是当前新时限
    （如 C-007「可提货 19:30 改 20:30」后又「20:30 改 22:30」→ 22:30）。
    """
    best = None
    for pat in (r"(\d{1,2}:\d{2})\s*改\s*(\d{1,2}:\d{2})",
                r"顺延至\s*(\d{1,2}:\d{2})",
                r"新时限\s*[:：]?\s*(\d{1,2}:\d{2})"):
        for m in re.finditer(pat, text):
            if best is None or m.end() > best[0]:
                best = (m.end(), m.group(m.lastindex))
    return best[1] if best else ""


_TERMINAL = ("delivered", "accepted", "returned", "void", "failed")


def _deferred_orders(zones):
    """合理延期（MES/08 §三）：非终态 + 卡 §10 已留痕 → {单号: 新时限或「见§10」}。

    数据源固定为卡 §10/§3（board_zones 只透传 120 字 delay_note 摘要，够判
    有无留痕、不够取新时限，故这里只读回原文补时限）。蓝色优先级高于红/黄：
    已按令留痕顺延的单不再计红（红=超时且无人决策）。
    """
    out = {}
    try:
        odb = mes_orders.orders_dir()
    except Exception:
        odb = ""
    for o in (zones.get("orders") or {}).get("items") or []:
        code = o.get("code") or ""
        st = str(o.get("state_raw") or o.get("state") or "")
        if not code or not o.get("delay_note") or st in _TERMINAL:
            continue
        nd = ""
        try:
            p = os.path.join(odb, code, "order.md")
            if odb and os.path.isfile(p):
                t = open(p, encoding="utf-8", errors="replace").read()
                m = re.search(r"(?ms)^##\s*10\b.*?(?=^##\s*11\b|\Z)", t)
                nd = _delay_new_deadline(m.group(0) if m else t)
        except OSError:
            nd = ""
        out[code] = nd or "见§10"
    return out


def _risk_lists(zones):
    """红黄风险：逐一对应告警区可见行（同级同源，数量可直接点数核对）。

    口径：红/黄 = alerts.items 中 level 为 red/yellow 的行（PATROL 原文逐条 +
    超时单追加行）；仅对完全相同的 (单号, 文案) 去重，不再按单号合并——
    保证「横条 N 红」与告警区 N 条红行一一对应，便于外部抽检。
    订单 warn（停留 1.5×）只在订单区着色，不并入横条，以免出现无对应行的数字。
    合理延期（MES/08）：已留痕顺延的单其「超交付截止」派生行转蓝，不进红/黄。
    """
    orders = zones.get("orders") or {}
    alerts = zones.get("alerts") or {}
    deferred = _deferred_orders(zones)
    red, yellow, seen = [], [], set()

    def _add(bucket, code, msg, src):
        key = (code or "—", msg or "")
        if key in seen:
            return
        seen.add(key)
        bucket.append({"code": code or "—", "msg": msg, "src": src})

    alert_codes = set()
    for a in alerts.get("items") or []:
        lv = a.get("level")
        if lv in ("red", "yellow"):
            if a.get("order") in deferred and "超交付截止" in (a.get("msg") or ""):
                continue
            _add(red if lv == "red" else yellow, a.get("order"), a.get("msg") or "",
                 "state/PATROL_ALERT.txt")
            if a.get("order") and a.get("order") != "—":
                alert_codes.add(a.get("order"))
    for o in orders.get("items") or []:
        if o.get("code") in deferred:
            continue
        if not o.get("overdue") or o.get("code") in alert_codes:
            continue
        try:
            x = float(o.get("sla_x") or 0)
        except (TypeError, ValueError):
            x = 0
        msg = "%s %s 超交付截止 %s（%s）%s" % (
            o.get("code"), o.get("title", "")[:24], o.get("deadline") or "—",
            o.get("state_cn") or "", (" %.2f×" % x) if x else "")
        _add(red if x >= 2.0 else yellow, o.get("code"), msg, "orders/*/order.md")
    return red, yellow


def _alertbar(zones):
    red, yellow = _risk_lists(zones)
    deferred = _deferred_orders(zones)
    alerts = zones.get("alerts") or {}
    src = _zsrc(alerts, "orders/*/order.md")
    blue = len(deferred)
    blue_txt = ('<span class="ab-sep">·</span><b class="ab-blue" title="合理延期·MES/08，不计红">%d 蓝</b>'
                % blue) if blue else ""
    if red:
        first = red[0]["msg"]
        return ("red", '<span class="ab-ico">🚨</span><b>%d 红</b><span class="ab-sep">·</span>'
                       '<b>%d 黄</b>%s<span class="ab-sep">·</span><span class="ab-msg">%s</span>%s'
                % (len(red), len(yellow), blue_txt, _esc(first[:110]), src))
    if yellow:
        first = yellow[0]["msg"]
        return ("yellow", '<span class="ab-ico">⚠️</span><b>%d 黄</b>%s<span class="ab-sep">·</span>'
                          '<span class="ab-msg">%s</span>%s'
                % (len(yellow), blue_txt, _esc(first[:120]), src))
    if blue:
        dl = " · ".join("%s 延期·新时限 %s" % (c, t) for c, t in list(deferred.items())[:4])
        return ("blue", '<span class="ab-ico">🔵</span><b>%d 蓝</b><span class="ab-sep">·</span>'
                        '<span class="ab-msg">合理延期（MES/08，不计红）：%s</span>%s'
                % (blue, _esc(dl[:130]), src))
    if alerts.get("status") == "missing":
        return ("gray", '<span class="ab-ico">◻</span><b>告警台账未接入</b>'
                        '<span class="ab-sep">·</span><span class="ab-msg">%s</span>%s'
                % (_esc(alerts.get("note") or "数据源建设中"), src))
    return ("green", '<span class="ab-ico">✅</span><b>无红黄告警</b>'
                     '<span class="ab-sep">·</span><span class="ab-msg">全厂运行正常</span>%s' % src)


def _questions(zones):
    orders = zones.get("orders") or {}
    seats = zones.get("seats") or {}
    cap = zones.get("capacity") or {}
    cost = zones.get("cost") or {}
    os_ = orders.get("summary") or {}
    ss = seats.get("summary") or {}
    red, yellow = _risk_lists(zones)
    total = os_.get("orders")
    wip = os_.get("wip")
    active = ss.get("active")
    orders_missing = (orders.get("status") == "missing") or total is None
    seats_missing = (seats.get("status") == "missing") or active is None
    alerts = zones.get("alerts") or {}
    osum_src = "orders/*/order.md（mes_orders.load_all 16 态派生；board_zones.py:218 zone_orders）"
    ssum_src = "state/seat_registry.json + 在制卡 §2.1 会话（board_zones.py:282 zone_seats）"
    shift = cap.get("shift") or "—"
    shift_short = shift.split(" ")[0] if shift else "—"
    okn = sum(1 for v in (zones or {}).values() if (v or {}).get("status") == "ok")

    if orders_missing:
        sub = "订单源不可读"
        if not seats_missing:
            sub += " · 在役 %s 席" % _znum(active, ssum_src)
        q1 = ('<div class="qbig"><span class="miss">数据源建设中</span></div>'
              '<div class="qsub">%s</div>' % sub)
    else:
        q1 = ('<div class="qbig">%s <span class="uq">在制</span> <span class="slash">/</span> %s <span class="uq">单</span></div>'
              '<div class="qsub">在役 %s 席 · %s · 实源 %d/7</div>'
              % (_znum(wip if wip is not None else "—", osum_src, "big"),
                 _znum(total, osum_src, "big"),
                 _znum(active, ssum_src) if not seats_missing else "—",
                 _esc(shift_short), okn))
    items = orders.get("items") or []
    live_st = ("awaiting-release", "producing", "qc", "awaiting-closure", "delivering",
               "blocked", "suspended", "compensating")
    cand = [o for o in items if o.get("state") in live_st and not ((o.get("node") or {}).get("done"))]
    top = max(cand, key=lambda x: (x.get("station_min") or 0)) if cand else None
    stuck = ("%s 停留 %s" % (top.get("code"), _zfmt_min(top.get("station_min")))
             if top else "最久在制 —")
    deferred = _deferred_orders(zones)
    if red or yellow or deferred:
        q2 = ('<div class="qbig"><span class="st red">%s 红</span> <span class="st yellow">%s 黄</span>'
              ' <span class="st blue" title="合理延期（MES/08），不计红">%s 蓝</span></div>'
              '<div class="qsub">%s · 明细见 ④</div>'
              % (_znum(len(red), "state/PATROL_ALERT.txt + orders/*/order.md（board_zones.py:498 zone_alerts）", "red"),
                 _znum(len(yellow), "state/PATROL_ALERT.txt + orders/*/order.md（board_zones.py:498 zone_alerts）", "yellow"),
                 _znum(len(deferred), "orders/*/order.md §10 延期说明（MES/08 合理延期）", "blue"),
                 _esc(stuck)))
    elif orders_missing:
        q2 = ('<div class="qbig"><span class="miss">告警源未接入</span></div>'
              '<div class="qsub">%s</div>'
              % _esc("告警台账缺失，且订单源不可读；红黄数不明（不补 0）" if alerts.get("status") == "missing"
                     else "订单源不可读，红黄数不明（不补 0）"))
    else:
        note = " · 告警台账未接入" if alerts.get("status") == "missing" else ""
        q2 = ('<div class="qbig"><span class="st green">0 红</span> <span class="st gray">0 黄</span></div>'
              '<div class="qsub">%s · 当前无红黄%s</div>' % (_esc(stuck), note))
    if cost.get("status") == "missing":
        q3 = ('<div class="qbig"><span class="miss">数据源建设中</span></div>'
              '<div class="qsub">%s</div>' % _esc((cost.get("note") or "token 统计无源")[:60]))
    else:
        today = cost.get("today_tokens")
        csrc = (cost.get("source") or "ZCode model_usage") + "；board_zones.py:557 zone_cost"
        q3 = ('<div class="qbig">%s <span class="uq">tokens 今日</span></div>'
              '<div class="qsub">本班 %s · 累计 %s</div>'
              % (_znum(_zfmt_tok(today), csrc),
                 _znum(_zfmt_tok(cost.get("shift_tokens")), csrc),
                 _znum(_zfmt_tok(cost.get("cumulative_tokens")), csrc)))
    return ('<div class="q q1" id="panel_q1"><h2>① 厂现在怎么样</h2>%s</div>'
            '<div class="q q2" id="panel_q2"><h2>② 卡在哪 / 有无红黄</h2>%s</div>'
            '<div class="q q3" id="panel_q3"><h2>③ 花了多少 token</h2>%s</div>' % (q1, q2, q3))


def _zone_orders(z):
    items = z.get("items") or []
    sm = z.get("summary") or {}
    deferred = _deferred_orders({"orders": z})
    if z.get("status") == "missing" or not items:
        sub = "订单 — · 在制 — · 超期 —"
    else:
        sub = "订单%s·在制%s·超期%s" % (sm.get("orders", "—"), sm.get("wip", "—"), sm.get("overdue", "—"))
        if deferred:
            sub += "·延期%d" % len(deferred)
    head = _zhead("①", "订单池", z, sub)
    if not items:
        return '<section class="zone z-orders" id="z-orders">%s<div class="zb">%s</div></section>' % (head, _zbar(z))
    by_state = sm.get("by_state") or {}
    chips = []
    tone = {"created": "gray", "queued": "gray", "planning": "gray", "contract-review": "gray",
            "auditing": "gray", "awaiting-release": "yellow", "producing": "green", "qc": "green",
            "awaiting-closure": "yellow", "delivering": "green", "delivered": "green",
            "accepted": "green", "returned": "red", "blocked": "red", "suspended": "yellow",
            "compensating": "yellow"}
    for s in mes_orders.STATES:
        n = by_state.get(s["k"])
        cls = tone.get(s["k"], "gray")
        chips.append('<span class="sx %s">%s <b>%s</b></span>'
                     % (cls, _esc(s["cn"]), "—" if n is None else n))
    rows = []
    for o in items:
        node = o.get("node") or {}
        emp = node.get("emp") or ""
        if emp in ("", "—"):
            emp = node.get("seat") or "—"
        badge = _zsess(node.get("sess"))
        sg = _norm_sign(node.get("sign"))
        if sg:
            sign = '<span class="green" title="%s">✅</span>' % _esc(sg)
        elif node.get("done"):
            sign = '<span class="mut" title="节点已完成·卡内无签字记录">—</span>'
        else:
            sign = '<span class="gray" title="当前节点未签">未</span>'
        try:
            x = float(o.get("sla_x") or 0)
        except (TypeError, ValueError):
            x = 0
        xcls = "red" if o.get("overdue") and x >= 2.0 else ("yellow" if (o.get("overdue") or o.get("warn")) else "gray")
        nd = deferred.get(o.get("code") or "")
        if nd:
            xcls = "blue"
        dtag = ('<span class="dlb" title="合理延期（MES/08 §三）：§10 已留痕，不计红">延期·新时限 %s</span>'
                % _esc(nd)) if nd else ""
        rows.append(
            '<tr class="orow %s" data-code="%s" tabindex="0" title="点开订单钻取">'
            '<td class="c-code mono" data-label="单号">%s%s%s</td>'
            '<td class="c-title" data-label="标题" title="%s">%s</td>'
            '<td data-label="状态">%s</td>'
            '<td class="c-node" data-label="当前节点" title="%s">%s</td>'
            '<td data-label="在办">%s <span class="mut mono">%s</span></td>'
            '<td data-label="签">%s</td>'
            '<td class="%s" data-label="停留">%s</td>'
            '<td class="%s" data-label="交付比">%s</td>'
            '<td data-label="提货">%s</td></tr>'
            % (xcls, _esc(o.get("code")), _esc(o.get("code")),
               ' <span class="p0">P0</span>' if o.get("p0") else "", dtag,
               _esc(o.get("title")), _esc(o.get("title")),
               _zst_chip(o), _esc(node.get("t") or ""), _esc((node.get("t") or "—")[:34]),
               _esc(emp), _esc(badge), sign,
               xcls, _zfmt_min(o.get("station_min")),
               xcls, ("%.2f×" % x) if x else "—",
               _esc(o.get("pickup") or "—")))
    body = ('<div class="zb">%s<div class="otwrap"><table class="ot"><thead><tr>'
            '<th style="width:10%%">单号</th><th style="width:21%%">标题</th><th style="width:10%%">状态</th>'
            '<th style="width:15%%">当前节点</th><th style="width:13%%">在办 / 工牌</th><th style="width:4%%">签</th>'
            '<th style="width:8%%">停留</th><th style="width:7%%">交付比</th><th style="width:12%%">提货</th>'
            '</tr></thead><tbody>%s</tbody></table></div>'
            '<div class="states" title="16 态全量（字典 15 行）">%s<span class="lgd blue" '
            'title="MES/08：已留痕顺延的单显蓝，不计红">蓝=合理延期（MES/08，不计红）</span></div></div>'
            % (_zbar(z), "".join(rows), "".join(chips)))
    return '<section class="zone z-orders" id="z-orders">%s%s</section>' % (head, body)


def _zone_seats(z):
    sm = z.get("summary") or {}
    items = z.get("items") or []
    if z.get("status") == "missing":
        sub = "席位 — · 在役 — · 在册 — · 退役 —"
    else:
        sub = "席位%s·在役%s·在册%s·退役%s" % (
            sm.get("seats", "—"), sm.get("active", "—"), sm.get("registry_active", "—"), sm.get("retired", "—"))
    head = _zhead("②", "席位 / 工牌", z, sub)
    if not items:
        return '<section class="zone z-seats" id="z-seats">%s<div class="zb">%s</div></section>' % (head, _zbar(z))
    zn = dict(z)
    zn["note"] = ("tasks-index.sqlite 缺失仅影响实时 status/updated_at；心跳=model_usage 最近调用（已接入，见独立区「❤ 心跳 / 当前通道」）；"
                  "badges 段未建，工牌短码由会话号派生")
    cards = []
    for s in items:
        st = str(s.get("status") or "")
        cls = "green" if st in ("active", "from-card") else ("yellow" if st == "stale" else "gray")
        stcn = {"active": "在役", "from-card": "卡内席", "stale": "心跳旧", "retired": "退役"}.get(st, st or "—")
        cards.append(
            '<div class="seat"><div class="sid">%s <span class="mut">%s</span></div>'
            '<div class="meta">工牌 <b class="mono">%s</b></div>'
            '<div class="meta">状态 <span class="st %s">%s</span> · 单 %s</div></div>'
            % (_esc(s.get("seat") or "—"), _esc(s.get("dept") or ""),
               _esc(s.get("badge") or "—"),
               cls, _esc(stcn), _esc(s.get("order") or "—")))
    return ('<section class="zone z-seats" id="z-seats">%s<div class="zb">%s</div>'
            '<div class="seats">%s</div>'
            '<div class="foot">工牌短码=会话号前 13 位（badges 段未建）；心跳与当前通道见独立区「❤ 心跳 / 当前通道」</div></div></section>'
            % (head, _zbar(zn), "".join(cards)))


def _zone_heartbeat(z):
    items = z.get("items") or []
    beats = [s for s in items if str(s.get("last_beat") or "").strip() not in ("", "—")]
    ch_ok = sum(1 for s in items
                if str((s.get("channel") or {}).get("status") or "") == "ok"
                and (s.get("channel") or {}).get("provider") and (s.get("channel") or {}).get("model"))
    hz = {"status": ("ok" if beats else ("degraded" if items else "missing")),
          "sources": z.get("sources") or [], "items": items,
          "note": "" if beats else "model_usage 无任何最近调用记录（禁 0 禁猜）"}
    sub = ("席位%d·心跳%d·通道%d/%d" % (len(items), len(beats), ch_ok, len(items))
           if items else "席位 — · 心跳 — · 通道 —")
    head = _zhead("❤", "心跳 / 当前通道", hz, sub)
    if not items:
        return '<section class="zone z-heartbeat" id="z-heartbeat">%s<div class="zb">%s</div></section>' % (head, _zbar(hz))
    chan_bar = "" if ch_ok else ('<div class="zbar missing"><b>当前通道源未接入</b>'
                                 '<span>model_usage 无该批席位记录（禁 0 禁猜）</span></div>')
    cards = []
    for s in items:
        st = str(s.get("status") or "")
        cls = "green" if st in ("active", "from-card") else ("yellow" if st == "stale" else "gray")
        stcn = {"active": "在役", "from-card": "卡内席", "stale": "心跳旧", "retired": "退役"}.get(st, st or "—")
        age = s.get("beat_age_min")
        beat = str(s.get("last_beat") or "")
        beat_hm = beat[11:16] if len(beat) >= 16 and beat[10] == " " else beat
        if isinstance(age, int):
            age_txt = ("%dh%02dm 前" % (age // 60, age % 60)) if age >= 120 else ("%dm 前" % age)
        else:
            age_txt = "—"
        ch = s.get("channel") or {}
        cprov = str(ch.get("provider") or "").strip()
        cmid = str(ch.get("model") or "").strip()
        if str(ch.get("status") or "") == "ok" and cprov and cmid:
            asof = str(ch.get("as_of") or "")
            asof_hm = asof[11:16] if len(asof) >= 16 and asof[10] == "T" else (asof or "—")
            chan = ('<div class="meta chan" title="model_usage 最近记录 as_of %s">当前通道 '
                    '<b class="mono">%s</b>'
                    '<span class="src" data-source="board_zones.py:314 zone_seats.channel（model_usage 最近记录）">源</span>'
                    ' · 最近 %s</div>' % (_esc(asof), _esc(cprov + "/" + cmid), _esc(asof_hm)))
        else:
            chan = ('<div class="zbar missing chmiss"><b>当前通道缺源</b>'
                    '<span>model_usage 无该会话记录（禁猜）</span></div>')
        cards.append(
            '<div class="seat"><div class="sid">%s <span class="mut">%s</span></div>'
            '<div class="meta">工牌 <b class="mono">%s</b> · 状态 <span class="st %s">%s</span></div>'
            '<div class="meta">单 %s</div>'
            '<div class="meta" title="last_beat %s">心跳 %s · %s</div>%s</div>'
            % (_esc(s.get("seat") or "—"), _esc(s.get("dept") or ""),
               _esc(s.get("badge") or "—"), cls, _esc(stcn), _esc(s.get("order") or "—"),
               _esc(beat), _esc(beat_hm or "—"), _esc(age_txt), chan))
    return ('<section class="zone z-heartbeat" id="z-heartbeat">%s<div class="zb">%s%s'
            '<div class="seats">%s</div>'
            '<div class="foot">心跳=model_usage 最近模型调用（last_beat，实时派生）；当前通道=model_usage 最近一条 provider/model；无记录一律灰条禁猜</div></div></section>'
            % (head, _zbar(hz), chan_bar, "".join(cards)))


def _zone_events(z):
    sm = z.get("summary") or {}
    if z.get("status") == "missing" or not (z.get("items") or []):
        sub = "事件 — · 扫 —"
    else:
        sub = "事件%s·扫%s" % (sm.get("events", "—"), sm.get("scan", "—"))
    head = _zhead("③", "事件流水", z, sub)
    items = z.get("items") or []
    if not items:
        return '<section class="zone z-events" id="z-events">%s<div class="zb">%s</div></section>' % (head, _zbar(z))
    rows = []
    for e in items[:40]:
        ts = str(e.get("ts") or "—")
        ts_hm = ts[11:16] if len(ts) >= 16 and ts[10] == " " else ts
        art = str(e.get("artifact") or "")
        art_show = art.replace("\\", "/").rsplit("/", 1)[-1] if art else "—"
        rows.append('<div class="ev"><span class="mono et" title="%s">%s</span><div><b>%s</b> '
                    '<span class="mono mut">%s</span> %s<div class="mut art" title="%s">%s</div></div></div>'
                    % (_esc(ts), _esc(ts_hm), _esc(e.get("action") or "—"),
                       _esc(e.get("badge") or "—"), _esc(e.get("order") or ""),
                       _esc(art), _esc(art_show)))
    return ('<section class="zone z-events" id="z-events">%s<div class="zb">%s'
            '<div class="list">%s</div></div></section>' % (head, _zbar(z), "".join(rows)))


def _zone_capacity(z):
    lanes = z.get("lanes") or []
    sm = z.get("summary") or {}
    head = _zhead("⑤", "产能 / 班次", z, "交班%sm·下班%sm" % (sm.get("handover_in_min", "—"), sm.get("remain_min", "—")))
    if z.get("status") == "missing" and not lanes:
        return '<section class="zone z-capacity" id="z-capacity">%s<div class="zb">%s</div></section>' % (head, _zbar(z))
    bars = []
    for l in lanes:
        used, cap = l.get("used") or 0, l.get("cap") or 0
        pct = int(min(100, (used * 100.0 / cap) if cap else 0))
        cls = "full" if (cap and used >= cap) else ("warn" if (cap and used >= cap * 0.8) else "")
        bars.append('<div class="lane"><span class="mono">%s</span><div class="bar"><i class="%s" style="width:%d%%"></i></div>'
                    '<span class="mono r">%s/%s</span></div>'
                    % (_esc(l.get("lane")), cls, pct, used, cap or "—"))
    pool = z.get("pool") or {}
    body = ('<div class="zb">%s<div class="caprow"><span>班次</span><b>%s</b></div>'
            '<div class="caprow"><span>距交班</span><b class="mono">%sm</b></div>'
            '<div class="caprow"><span>距下班</span><b class="mono">%sm</b></div>'
            '<div class="lanes">%s</div>'
            '<div class="pool">R4：%s<br>R4-2：%s<br>空闲位 %s（按 lane 上限−近 5min 占用）</div>'
            '</div>'
            % (_zbar(z), _esc(z.get("shift") or "—"), sm.get("handover_in_min", "—"), sm.get("remain_min", "—"),
               "".join(bars), _esc(pool.get("R4") or "—"), _esc(pool.get("R4-2") or "—"),
               _znum(sm.get("free", "—"), "state/lane_caps.json + 近 5min model_usage（board_zones.py:441 zone_capacity）")))
    return '<section class="zone z-capacity" id="z-capacity">%s%s</section>' % (head, body)


def _zone_alerts(z, zones):
    sm = z.get("summary") or {}
    if z.get("status") == "missing":
        sub = "巡场 — · 超期 —"
    else:
        sub = "巡场%s·超期%s" % (sm.get("patrol", "—"), sm.get("overdue", "—"))
    head = _zhead("④", "告警 / 红线", z, sub)
    items = list(z.get("items") or [])
    red, yellow = _risk_lists(zones)
    have = set((i.get("order") or "") + "|" + (i.get("msg") or "")[:30] for i in items)
    for r in red + yellow:
        key = (r.get("code") or "") + "|" + (r.get("msg") or "")[:30]
        if key not in have:
            items.append({"level": "red" if r in red else "yellow", "order": r.get("code"),
                          "ts": "", "msg": r.get("msg") + "（订单源派生）"})
    if not items:
        if z.get("status") == "missing":
            return '<section class="zone z-alerts" id="z-alerts">%s<div class="zb">%s</div></section>' % (head, _zbar(z))
        return ('<section class="zone z-alerts" id="z-alerts">%s<div class="zb">%s'
                '<div class="okbox">✅ 无红黄告警</div></div></section>' % (head, _zbar(z)))
    rows = []
    deferred = _deferred_orders(zones)
    for a in items[:60]:
        lv = a.get("level") or "info"
        msg = a.get("msg") or ""
        if lv in ("red", "yellow") and a.get("order") in deferred and "超交付截止" in msg:
            lv = "blue"
            msg = "合理延期（MES/08 §三，不计红）：" + msg
        rows.append('<div class="al %s"><span class="lv">%s</span><div><b class="mono">%s</b> '
                    '<span class="mut mono">%s</span><div>%s</div></div></div>'
                    % (lv, {"red": "红", "yellow": "黄", "blue": "延", "info": "讯"}.get(lv, lv),
                       _esc(a.get("order") or "—"), _esc(a.get("ts") or ""), _esc(msg)))
    return ('<section class="zone z-alerts" id="z-alerts">%s<div class="zb">%s'
            '<div class="list">%s</div></div></section>' % (head, _zbar(z), "".join(rows)))


def _zone_cost(z):
    sm = z.get("summary") or {}
    head = _zhead("⑥", "成本 / token", z, "今日 %s" % _zfmt_tok(sm.get("today_tokens")))
    if z.get("status") == "missing":
        return '<section class="zone z-cost" id="z-cost">%s<div class="zb">%s</div></section>' % (head, _zbar(z))
    src = (z.get("source") or "ZCode model_usage") + "；board_zones.py:557 zone_cost"
    by = z.get("by_lane") or []
    mx = max([b.get("tokens") or 0 for b in by] + [1])
    bars = []
    for b in by:
        tok = b.get("tokens") or 0
        bars.append('<div class="lane"><span class="mono">%s</span><div class="bar"><i style="width:%d%%"></i></div>'
                    '<span class="mono r">%s</span></div>'
                    % (_esc(b.get("lane")), int(tok * 100 / mx), _esc(_zfmt_tok(tok))))
    body = ('<div class="zb"><div class="costbig">%s <span class="uq">今日 tokens</span></div>'
            '<div class="caprow"><span>本班</span><b class="mono">%s</b></div>'
            '<div class="caprow"><span>累计</span><b class="mono">%s</b></div>'
            '<div class="lanes">%s</div><div class="foot">%s</div></div>'
            % (_znum(_zfmt_tok(z.get("today_tokens")), src, "big"),
               _znum(_zfmt_tok(z.get("shift_tokens")), src),
               _znum(_zfmt_tok(z.get("cumulative_tokens")), src),
               "".join(bars), _esc(z.get("note") or "全部由 model_usage.computed_total_tokens 实算")))
    return '<section class="zone z-cost" id="z-cost">%s%s</section>' % (head, body)


def _zone_handover(z):
    sm = z.get("summary") or {}
    its = z.get("items") or []
    shown = sum(min(14, len(it.get("summary") or [])) for it in its[:1])
    if z.get("status") == "missing":
        sub = "交接单 —"
    else:
        sub = "交接单%s·摘%s行" % (sm.get("handover", "—"), shown)
    head = _zhead("⑦", "交接 / 交班", z, sub)
    if z.get("status") == "missing":
        return '<section class="zone z-handover" id="z-handover">%s<div class="zb">%s</div></section>' % (head, _zbar(z))
    lines = []
    for it in its[:1]:
        for ln in (it.get("summary") or [])[:14]:
            t = re.sub(r"^[#>\-\*\s|]+", "", str(ln)).strip()
            if t and not re.match(r"^[-:|\s]+$", t):
                lines.append('<div class="holine">%s</div>' % _esc(t[:180] + ("…" if len(t) > 180 else "")))
        lines.insert(0, '<div class="foot mono">%s · %s</div>' % (_esc(it.get("ts") or ""), _esc(it.get("file") or "")))
    return ('<section class="zone z-handover" id="z-handover">%s<div class="zb">%s%s</div></section>'
            % (head, _zbar(z), "".join(lines) or '<div class="okbox">交接单为空</div>'))


def _zones_html(zones):
    return "".join([_zone_orders(zones.get("orders") or _zone_default("")),
                    _zone_seats(zones.get("seats") or _zone_default("")),
                    _zone_heartbeat(zones.get("seats") or _zone_default("")),
                    _zone_events(zones.get("events") or _zone_default("")),
                    _zone_alerts(zones.get("alerts") or _zone_default(""), zones),
                    _zone_capacity(zones.get("capacity") or _zone_default("")),
                    _zone_cost(zones.get("cost") or _zone_default("")),
                    _zone_handover(zones.get("handover") or _zone_default(""))])


def _detail_html(o, det, path, zones):
    code = o.get("code") or "?"
    det = det or {}
    s20 = _s20_table(path)
    rows, cur = _flow_view(det, s20)
    src = "orders/%s/order.md" % code
    terminal = str(o.get("state_raw") or o.get("state") or "") in (
        "delivered", "accepted", "returned", "void", "failed")
    if cur:
        who = cur.get("emp") or cur.get("seat") or "—"
        if who in ("", "—"):
            who = cur.get("seat") or "—"
        node_txt = cur.get("node") or "—"
        dur = _zfmt_min(cur.get("mins"))
        if cur.get("limit"):
            dur += " / %s" % _zfmt_min(cur.get("limit"))
            if cur.get("x") is not None:
                dur += " · %.2f×" % cur["x"]
        else:
            dur += " / 时限未解析"
        a1 = ("已到终态：%s" % (o.get("state_cn") or o.get("state"))) if terminal else (
            "%s <span class='mut'>· 第 %s 节点</span>" % (_esc(node_txt[:60]), _esc(cur.get("seq") or "—")))
        if terminal:
            a2 = "无（终态·无在办） <span class='mut'>末节点 %s</span>" % _esc(who)
        else:
            a2 = "%s <span class='mut mono'>工牌 %s</span>" % (_esc(who), _esc(_zsess_tag(cur.get("sess"))))
        sg = _norm_sign(cur.get("sign"))
        if sg:
            a3 = "✅ 已签 <span class='mono'>%s</span>" % _esc(sg)
        elif terminal:
            a3 = "— <span class='mut'>终态·卡内无签字记录</span>"
        else:
            a3 = "❌ 未签"
        a4 = '<span class="%s">%s</span>' % (cur.get("level") or "gray", _esc(dur))
    else:
        a1, a2, a3, a4 = "无 §2.1 流程", "—", "—", "—"
    ans = ('<div class="q4">'
           '<div class="ans"><div class="al">① 到哪个流程</div><div class="av">%s</div></div>'
           '<div class="ans"><div class="al">② 谁在办（工牌+会话）</div><div class="av">%s</div></div>'
           '<div class="ans"><div class="al">③ 签没签</div><div class="av">%s</div></div>'
           '<div class="ans"><div class="al">④ 停多久（对照 §2.0 1.5×黄 2×红）</div><div class="av">%s</div></div>'
           '</div>' % (a1, a2, a3, a4))

    def _flow_rows_html():
        if not rows:
            return '<div class="zbar missing"><b>无 §2.1 流程表</b><span>卡内未编成，无法判定当前节点</span></div>'
        out = []
        for r in rows:
            cls = "cur" if (cur and r["seq"] == cur["seq"] and r["node"] == cur["node"]) else ""
            dwell = _zfmt_min(r["mins"])
            lim = _zfmt_min(r["limit"]) if r["limit"] else "—"
            x = ("%.2f×" % r["x"]) if r["x"] is not None else "—"
            out.append('<tr class="%s"><td class="mono">%s</td><td class="nd"><b>%s</b>'
                       '<div class="mut">%s%s</div></td>'
                       '<td class="mono sm">%s</td><td class="mono">%s</td><td class="mono">%s</td>'
                       '<td class="mono %s">%s<div class="mut">/ %s</div></td>'
                       '<td class="mono %s">%s</td><td>%s</td></tr>'
                       % (cls, _esc(r["seq"]), _esc(r["node"]),
                          _esc(r["seat"]), (" · " + _esc(r["concl"][:40])) if r["concl"] else "",
                          _esc(_zsess_tag(r["sess"])), _esc(r["enter"]), _esc(r["leave"] or "—"),
                          r["level"], _esc(dwell), _esc(lim), r["level"],
                          _esc(x), "✅" if r["sign_ok"] else "—"))
        return ('<div class="flowwrap"><table class="ft"><thead><tr>'
                '<th>序</th><th>节点 / 岗位</th><th>会话</th><th>进</th><th>出</th><th>停留</th><th>倍</th><th>签</th>'
                '</tr></thead><tbody>%s</tbody></table></div>' % "".join(out))

    def _tasks_html():
        tasks = det.get("tasks") or []
        if not tasks:
            return '<div class="zbar missing"><b>无 §9 任务表</b><span>卡内无任务行</span></div>'
        out = []
        for t in tasks[:24]:
            out.append('<tr><td class="nd"><b>%s</b></td><td>%s</td><td>%s</td>'
                       '<td class="mono sm">%s</td><td>%s</td><td class="mono">%s</td><td class="mono sm">%s</td></tr>'
                       % (_esc(t.get("t") or "—"), _esc(t.get("seat") or "—"),
                          _esc(t.get("emp") or "—"), _esc(_zsess_tag(t.get("sess"))),
                          _esc(t.get("status") or "—"), _esc(t.get("progress") or "—"),
                          _esc(t.get("sign") or "—")))
        return ('<div class="flowwrap"><table class="ft"><thead><tr>'
                '<th>任务</th><th>席位</th><th>员工</th><th>会话</th><th>状态</th><th>进度</th><th>签字</th>'
                '</tr></thead><tbody>%s</tbody></table></div>' % "".join(out))

    def _s20_html():
        if not s20:
            return '<div class="zbar missing"><b>无 §2.0 时限表</b><span>节点时限不可判（停留只报实测，不套阈值）</span></div>'
        out = []
        for k in sorted(s20.keys(), key=lambda x: int(x[1:]) if x[1:].isdigit() else 999):
            r = s20[k]
            cls = "cur" if (cur and cur.get("tcode") == k) else ""
            out.append('<tr class="%s"><td class="mono">%s</td><td>%s</td><td class="mono">%s</td>'
                       '<td class="mono">%s</td><td class="nd">%s</td></tr>'
                       % (cls, _esc(k), _esc(r.get("seat") or "—"), _esc(r.get("plan_due") or "—"),
                          _esc(r.get("limit_txt") or "—"), _esc((r.get("status") or "—")[:90])))
        return ('<div class="flowwrap"><table class="ft"><thead><tr>'
                '<th>T</th><th>责任席</th><th>计划完成</th><th>时限</th><th>状态</th>'
                '</tr></thead><tbody>%s</tbody></table></div>' % "".join(out))

    crew = det.get("crew") or {}
    crow = ("员工 %s · 会话 %s/%s · tokens %s" % (
        crew.get("n_emp", "—"), crew.get("sess_real", "—"), crew.get("sess_total", "—"),
        _zfmt_tok(crew.get("tokens"))) if crew.get("db_ok") else "员工库不可达")
    head = ('<div class="drillhead"><div><div class="dtitle">%s <span class="mono">%s</span></div>'
            '<div class="dmeta">%s %s · %s · 交付 %s · 提货 %s %s</div></div>'
            '<button class="x" type="button" data-close="1">关闭 ✕</button></div>'
            % (_esc(o.get("title") or ""), _esc(code), _zst_chip(o),
               ("P0" if o.get("p0") else _esc(_clean_md(o.get("priority") or ""))),
               _esc(_clean_md(o.get("route") or "—")), _esc(o.get("deadline") or "—"), _esc(o.get("pickup") or "—"), _zsrc({"sources": [src]})))
    return (head + ans +
            '<div class="sec">§2.1 编成与流转 <span class="mut">%d 节点 · 已签 %d</span></div>%s'
            '<div class="sec">§2.0 节点时限 <span class="mut">阈值：1.5× 黄 / 2× 红</span></div>%s'
            '<div class="sec">§9 任务 <span class="mut">%s</span></div>%s'
            '<div class="foot">%s · 只读卡面，不改数据 · 源 %s</div>'
            % (len(rows), sum(1 for r in rows if r["sign_ok"]), _flow_rows_html(), _s20_html(),
               _esc(crow), _tasks_html(), _esc(crow), _esc(src)))


def _ui_payload(d):
    zones = d.get("zones") or {}
    orders_zone = zones.get("orders") or _zone_default("")
    files = {o.get("code"): o.get("file") for o in (d.get("orders") or [])}
    details = {}
    for o in orders_zone.get("items") or []:
        code = o.get("code")
        if not code:
            continue
        try:
            details[code] = _detail_html(o, (d.get("details") or {}).get(code) or {},
                                         files.get(code), zones)
        except Exception as e:
            details[code] = ('<div class="zbar missing"><b>详情渲染失败</b><span>%s</span></div>'
                             % _esc("%s: %s" % (type(e).__name__, str(e)[:160])))
    level, inner = _alertbar(zones)
    return {"alert_level": level, "alertbar": inner,
            "questions": _questions(zones), "zones": _zones_html(zones),
            "details": details, "ts": d.get("ts") or "",
            "contract": d.get("contract") or "c009-zones-v1",
            "generated_at": d.get("zones_generated_at") or ""}


def render(d=None):
    """七区真数据首屏；客户端每 60s 拉 /data.json 局部替换（无整页重载）。"""
    d = d or collect()
    ui = d.get("ui") or _ui_payload(d)
    doc = UI_TEMPLATE.replace("__UI_CSS__", UI_CSS).replace("__UI_JS__", UI_JS)
    doc = (doc.replace("__SKIN__", SKIN)
              .replace("__TS__", _esc(d.get("ts") or ""))
              .replace("__ALERT_LEVEL__", ui.get("alert_level") or "gray")
              .replace("__ALERTBAR__", ui.get("alertbar") or "")
              .replace("__QUESTIONS__", ui.get("questions") or "")
              .replace("__ZONES__", ui.get("zones") or "")
              .replace("__CONTRACT__", _esc(ui.get("contract") or ""))
              .replace("__REFRESH__", str(REFRESH_SEC))
              .replace("__DETAILS_JSON__", json.dumps(ui.get("details") or {}, ensure_ascii=False)
                       .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")))
    return doc


def _legacy_render():
    """旧 15 态浅色页（TEMPLATE）保留只作回滚参照；当前 render() 不再走此处。"""
    d = collect()
    doc = TEMPLATE
    doc = doc.replace("__TS__", d["ts"]).replace("__SKIN__", SKIN)
    for key, val in d["kpis"].items():
        doc = doc.replace("__%s__" % key.upper(), str(val))
    for c in d["cols"]:
        doc = doc.replace("__COL_%s__" % c["key"].upper(), c["html"])
    doc = (doc.replace("__SEATS__", d["seats"])
              .replace("__TEAM__", d["team"])
              .replace("__STATES__", d["states_html"])
              .replace("__ORDERS__", d["orders_html"])
              .replace("__OFOOT__", d["ofoot"])
              .replace("__GANTT__", d["gantt_html"])
              .replace("__AUTOS__", d["autos"])
              .replace("__AUTOSOFF__", d["autosoff"])
              .replace("__AUTOSOFF_N__", str(d["autosoffn"]))
              .replace("__DL_DISP__", d["deltas"]["disp"])
              .replace("__DL_RUN__", d["deltas"]["run"])
              .replace("__DL_DONE__", d["deltas"]["done"])
              .replace("__DL_QUEUE__", d["deltas"]["queue"])
              .replace("__DL_BLK__", d["deltas"]["blk"])
              .replace("__RUNS__", d["runs"])
              .replace("__SF__", d["sf"])
              .replace("__SFFOOT__", d["sffoot"])
              .replace("__DC__", d["dc"])
              .replace("__AP__", d["ap"]))
    return doc.replace("→", "-&gt;")


def _epoch_ms(v):
    """库内时间戳兼容秒/毫秒:统一到毫秒。"""
    return v * 1000 if v and v < 10**11 else (v or 0)


def seat_activity(task_id):
    """某席位今日活动:心跳运行/工具调用/待办/消息数(只读,不含对话原文,防泄密)。"""
    today0 = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    today0_ms = int(today0.timestamp() * 1000)
    out = {"autos": [], "runs": [], "tools": [], "todos": [], "msgs": 0, "errs": []}

    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        autos = con.execute("select automation_id,title,enabled,run_count,last_run_at,next_run_at "
                            "from automations where target_task_id=? order by enabled desc, title", (task_id,)).fetchall()
        ids = [a["automation_id"] for a in autos]
        out["autos"] = [{"title": (a["title"] or "")[:30], "en": a["enabled"], "runs": a["run_count"],
                         "last": datetime.datetime.fromtimestamp(a["last_run_at"] / 1000).strftime("%H:%M") if a["last_run_at"] else "-",
                         "next": datetime.datetime.fromtimestamp(a["next_run_at"] / 1000).strftime("%H:%M") if a["next_run_at"] else "-"} for a in autos]
        if ids:
            q = ("select scheduled_at,outcome,error from automation_runs where automation_id in (%s) "
                 "and scheduled_at>=? order by scheduled_at desc limit 40" % ",".join("?" * len(ids)))
            for r in con.execute(q, ids + [today0_ms]):
                out["runs"].append({"t": datetime.datetime.fromtimestamp(r["scheduled_at"] / 1000).strftime("%H:%M:%S"),
                                    "oc": cn_outcome(r["outcome"]), "err": (r["error"] or "")[:60]})
    finally:
        con.close()

    try:
        c2 = sqlite3.connect("file:" + CLI_DB + "?mode=ro", uri=True)
        c2.row_factory = sqlite3.Row
        probe = c2.execute("select max(time_created) m from message").fetchone()["m"]
        t0 = today0_ms if (probe and probe > 10**11) else int(today0.timestamp())
        for r in c2.execute("select started_at,tool_name,status from tool_usage where session_id=? and started_at>=? "
                            "order by started_at desc limit 25", (task_id, t0)):
            ts = _epoch_ms(r["started_at"])
            out["tools"].append({"t": datetime.datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S") if ts else "-",
                                 "name": r["tool_name"], "st": r["status"]})
        for r in c2.execute("select content,status,priority from todo where session_id=? order by position limit 12", (task_id,)):
            out["todos"].append({"c": (r["content"] or "")[:70], "st": r["status"], "p": r["priority"]})
        out["msgs"] = c2.execute("select count(*) c from message where session_id=? and time_created>=?",
                                 (task_id, t0)).fetchone()["c"]
        c2.close()
    except Exception as exc:
        out["errs"].append(str(exc)[:80])
    return out


def seat_page(task_id):
    """席位详情页:今天做了什么(心跳/工具调用/待办)。"""
    if not re.match(r"^[A-Za-z0-9_\-]+$", task_id or ""):
        return "<html><body style='background:#f5f5f5;color:#1f1f1f'>非法席位ID</body></html>"
    tier, no = "员工", "--"
    name, st, upd = "(未知席)", "-", "-"
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        t = con.execute("select title,task_status,updated_at from tasks where task_id=?", (task_id,)).fetchone()
        if t:
            tier, no = seat_meta(t["title"])
            name = (t["title"] or "")[:40]
            st = cn_status(t["task_status"])
            upd = datetime.datetime.fromtimestamp((t["updated_at"] or 0) / 1000).strftime("%H:%M:%S") if t["updated_at"] else "-"
    finally:
        con.close()
    act = seat_activity(task_id)

    def rows(items, empty, fmt):
        return "".join(fmt(**x) for x in items) or "<tr><td colspan='3' class='mut'>%s</td></tr>" % empty

    auto_rows = rows(act["autos"], "无闹钟",
                     lambda **a: "<tr><td>%s</td><td class='%s'>%s</td><td class='mono'>%s</td><td class='mono'>%s-&gt;%s</td></tr>"
                     % (html.escape(a["title"]), "ok" if a["en"] else "mut", "启用" if a["en"] else "停用",
                        a["runs"], a["last"], a["next"]))
    run_rows = rows(act["runs"], "今日暂无运行记录",
                    lambda **a: "<tr><td class='mono'>%s</td><td class='%s'>%s</td><td class='mut'>%s</td></tr>"
                    % (a["t"], "ok" if a["oc"] == "成功" else ("run" if a["oc"] == "运行中" else "bad"),
                       a["oc"], html.escape(a["err"] or "-")))
    tool_rows = rows(act["tools"], "今日暂无工具调用",
                     lambda **a: "<tr><td class='mono'>%s</td><td>%s</td><td class='mut'>%s</td></tr>"
                     % (a["t"], html.escape(str(a["name"])), html.escape(str(a["st"]))))
    todo_rows = rows(act["todos"], "无待办",
                     lambda **a: "<tr><td>%s</td><td class='mut'>%s</td><td class='mut'>%s</td></tr>"
                     % (html.escape(a["c"]), html.escape(str(a["st"])), html.escape(str(a["p"]))))

    return ("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>席位详情 · %s</title>
<style>body{background:#f5f5f5;color:#1f1f1f;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans",sans-serif;padding:20px}
*{font-weight:400}.card{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:16px 20px;margin-bottom:20px;box-shadow:0 1px 2px rgba(15,23,42,.03)}
h1{font-size:18px}h2{font-size:14px;margin:0 0 8px;color:#1f1f1f}
table{border-collapse:collapse;font-size:12px;margin-top:4px}th{height:32px;text-align:left;color:#595959;background:#fafafa;border-bottom:1px solid #e5e6eb;font-weight:400}
td{height:36px;border-bottom:1px solid #f0f0f0;color:#595959;vertical-align:middle}
.meta{color:#6b6b6b;font-size:12px;margin:4px 0 2px}.back{color:#4a6fa5;text-decoration:none;font-size:14px}
.ok{color:#47875c}.run{color:#595959}.bad{color:#c0575c}.mut{color:#6b6b6b}.mono{font-family:Consolas,monospace}</style></head><body>
<a class="back" href="/">&lt;- 返回看板</a>
<div class="card" style="margin-top:12px"><h1><span class="mono">%s</span> %s</h1>
<div class="meta">档位:%s · 状态:%s · 最近更新:%s · 今日消息轮次:%d · 今日工具调用:%d 次</div></div>
<div class="card"><h2>今天的调度运行(心跳/闹钟)</h2><table><tr><th>时刻</th><th>结果</th><th>错误</th></tr>%s</table></div>
<div class="card"><h2>名下闹钟</h2><table><tr><th>闹钟</th><th>启停</th><th>累计轮次</th><th>上次-&gt;下次</th></tr>%s</table></div>
<div class="card"><h2>今天的工具调用(最近25条)</h2><table><tr><th>时刻</th><th>工具</th><th>状态</th></tr>%s</table></div>
<div class="card"><h2>当前待办</h2><table><tr><th>内容</th><th>状态</th><th>优先级</th></tr>%s</table></div>
</body></html>""" % (html.escape(name), no, html.escape(name), tier, st, upd, act["msgs"], len(act["tools"]),
                     run_rows, auto_rows, tool_rows, todo_rows))


def _load_cards():
    reg = {}
    p = os.path.join(STATE, "registry.json")
    if os.path.isfile(p):
        try:
            reg = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            reg = {}
    return reg.get("tasks") or []


def _card_rows(cards):
    out = ""
    for c in cards:
        cid = html.escape(str(c.get("id") or "?"))
        st = cn_status("blocked" if state_of(c) == "blocked" else state_of(c))
        out += ('<tr><td class="mono">%s</td><td><a href="/card/%s">%s</a></td><td>%s</td>'
                '<td class="mono">%s</td><td class="mut">%s</td></tr>'
                % (cid, cid, html.escape(str(c.get("title") or "")[:34]), st, lane_of(c),
                   html.escape(str(c.get("priority") or "-"))))
    return out


def cards_page(state):
    """某流水线状态的任务卡列表页(KPI 点击进入)。"""
    cn = PIPE_CN.get(state, "受阻")
    cards = [c for c in _load_cards() if state_of(c) == state]
    rows = _card_rows(cards) or "<tr><td colspan='5' class='mut'>当前无任务卡</td></tr>"
    return ("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>%s任务卡 · MES</title>
<style>body{background:#f5f5f5;color:#1f1f1f;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans",sans-serif;padding:20px}
*{font-weight:400}.card{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:16px 20px;margin-bottom:20px;box-shadow:0 1px 2px rgba(15,23,42,.03)}
h1{font-size:18px}h2{font-size:14px;margin:0 0 8px}table{border-collapse:collapse;font-size:12px;margin-top:4px}
th{height:32px;text-align:left;color:#595959;background:#fafafa;border-bottom:1px solid #e5e6eb;font-weight:400}
td{height:36px;border-bottom:1px solid #f0f0f0;color:#595959;vertical-align:middle}
.mut{color:#6b6b6b}.mono{font-family:Consolas,monospace}a{color:#4a6fa5;text-decoration:none}a:hover{text-decoration:underline}
.back{color:#4a6fa5;text-decoration:none;font-size:14px}</style></head><body>
<a class="back" href="/">&lt;- 返回看板</a>
<div class="card" style="margin-top:12px"><h1>%s · %d 张任务卡</h1></div>
<div class="card"><table><tr><th>编号</th><th>标题</th><th>状态</th><th>模型道</th><th>优先级</th></tr>%s</table></div>
</body></html>""" % (cn, cn, len(cards), rows))


def card_page(card_id):
    """单张任务卡详情页。"""
    if not re.match(r"^[A-Za-z0-9_\-]+$", card_id or ""):
        return "<html><body style='background:#f5f5f5;color:#1f1f1f'>非法卡号</body></html>"
    card = None
    for c in _load_cards():
        if str(c.get("id") or "") == card_id:
            card = c
            break
    if not card:
        return ("<html><body style='background:#f5f5f5;color:#1f1f1f;padding:20px'>"
                "<a href='/' style='color:#4a6fa5'>&lt;- 返回看板</a><p>未找到任务卡 %s</p></body></html>" % html.escape(card_id))
    st_raw = state_of(card)
    st = cn_status("blocked" if st_raw == "blocked" else st_raw)

    def row(k, v):
        return "<tr><td class='mut' style='width:96px'>%s</td><td>%s</td></tr>" % (k, v if v else "-")

    body = "".join([
        row("编号", "<span class='mono'>%s</span>" % html.escape(card_id)),
        row("状态", "<span class='%s'>%s</span>" % ("bad" if st_raw == "blocked" else "", st)),
        row("模型道", lane_of(card)),
        row("优先级", html.escape(str(card.get("priority") or "-"))),
        row("项目", html.escape(str(card.get("project") or "-"))),
        row("目标", html.escape(str(card.get("goal") or "-"))),
        row("验收标准", html.escape(str(card.get("acceptance") or "-"))),
        row("边界", html.escape(str(card.get("boundary") or "-"))),
        row("工作区", html.escape(str(card.get("workspace") or "-"))),
        row("最近进展", html.escape(str(card.get("last_progress_at") or "-"))),
        row("下一步", html.escape(str(card.get("next_action") or "-"))),
        row("尝试次数", html.escape("%s / 上限 %s" % (card.get("attempts", "-"), card.get("max_attempts", "-")))),
        row("最近核对", html.escape(str(card.get("last_check") or "-"))),
    ])
    return ("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>任务卡 %s</title>
<style>body{background:#f5f5f5;color:#1f1f1f;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans",sans-serif;padding:20px}
*{font-weight:400}.card{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:16px 20px;box-shadow:0 1px 2px rgba(15,23,42,.03)}
h1{font-size:18px}table{border-collapse:collapse;font-size:12px}td{height:36px;padding:6px 10px;border-bottom:1px solid #f0f0f0;vertical-align:top}
.mut{color:#6b6b6b}.mono{font-family:Consolas,monospace}.bad{color:#c0575c}a{color:#4a6fa5;text-decoration:none}</style></head><body>
<a href="/">&lt;- 返回看板</a>
<div class="card" style="margin-top:12px"><h1>%s</h1><table>%s</table></div>
</body></html>""" % (html.escape(card_id), html.escape(str(card.get("title") or card_id)), body))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dashboard", "index.html"))
    args = ap.parse_args()
    d = collect()
    doc = render(d)
    try:
        hist_path = os.path.join(STATE, "kpi_history.json")
        hist = json.load(io.open(hist_path, encoding="utf-8")) if os.path.isfile(hist_path) else []
        hist.append({"ts": time.time() * 1000, "kpis": d["kpis"]})
        io.open(hist_path, "w", encoding="utf-8").write(json.dumps(hist[-480:], ensure_ascii=False))
    except Exception:
        pass
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(doc)
    print("written:", args.out, os.path.getsize(args.out), "bytes")


if __name__ == "__main__":
    main()
