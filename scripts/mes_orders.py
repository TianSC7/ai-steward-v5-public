#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mes_orders.py —— MD 订单卡只读适配层（stdlib only）

MES 蓝图（factory-blueprint/MES系统/00-MES系统设计.md）的订单层数据源是工单 MD 卡：
  头部（编号/状态/进度/在谁手上）+ §3 追溯时间轴 + §9 待办与流转。
本模块只**读**卡，不写卡、不改格式（契约不做清单第 1 条）。

对外：
  STATES / STATE_MAP      15 行字典、16 个 key（accepted/returned 同表行）常量表
  load_all()              orders/<code>/order.md 全量解析（mtime+size 进程内缓存）
  parse_order(path)       单卡解析（缺段/坏表/空文件一律降级不抛）
  derive_state(order)     状态推导：头部反引号 token 为主 → §3 时间轴关键词兜底 → 未识别
  stage_of()/sub_progress()  10 段阶段折算 + 子需求 n/m（头部口径）
  dict_rows()             字典对照表 15 行（证据 A1 用，注 00 §二 原表行号）
  parse_group(path)       group.md 群聊流占位解析（C 块用，本单不渲染）

口径（审计 R-1/R-2 订正）：
  - 子需求 n/m 只取卡头「进度：子需求 n/m」（已过质检），§9 行数另列「任务 m/n」；
  - 兜底映射按 00 §二 驳回规则：审计驳回→planning，质检不过→producing；
    returned 只用于交付后被发起人退回。
"""
import datetime
import os
import re
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_ORDERS = os.path.normpath(os.path.join(ROOT, "..", "..", "dll", "factory-blueprint", "orders"))

# 16 个 key，按状态机顺序；cn 对齐 00 §二（plan 措辞 + R-9 原表优先：suspended=红项搁置）
STATES = [
    {"k": "created", "cn": "契约落卡", "color": "#6b6b6b", "tone": "灰",
     "owner": "厂长", "cond": "厂长受理完成", "line": 36, "stage": 1, "row": 1},
    {"k": "queued", "cn": "排队待排产", "color": "#4f6b8a", "tone": "灰蓝（P0 加急标）",
     "owner": "B 策划", "cond": "契约生效即入排队池，带优先级 P0/P1/P2", "line": 37, "stage": 2, "row": 2},
    {"k": "planning", "cn": "策划中", "color": "#4a6fa5", "tone": "蓝",
     "owner": "B 策划", "cond": "排入 1h 锁定栏（P0 插单 ≤30min 评估）", "line": 38, "stage": 2, "row": 3},
    {"k": "contract-review", "cn": "合同呈批中", "color": "#6b4fa8", "tone": "紫（>24h 温和提醒）",
     "owner": "厂长/发起人", "cond": "合同草案完成，厂长呈报发起人；商讨/返修循环不限次数",
     "line": 39, "stage": 2, "row": 4},
    {"k": "auditing", "cn": "审计中", "color": "#4a6fa5", "tone": "蓝",
     "owner": "C 审计", "cond": "方案完成直送审计", "line": 40, "stage": 3, "row": 5},
    {"k": "awaiting-release", "cn": "审结回呈·待放行", "color": "#8c5a00", "tone": "橙（>30min 变红）",
     "owner": "厂长", "cond": "审计通过回呈厂长", "line": 41, "stage": 4, "row": 6},
    {"k": "producing", "cn": "生产中", "color": "#4a6fa5", "tone": "蓝",
     "owner": "D 生产", "cond": "厂长放行", "line": 42, "stage": 5, "row": 7},
    {"k": "qc", "cn": "质检中", "color": "#4a6fa5", "tone": "蓝",
     "owner": "E 质检", "cond": "产物送达质检经理", "line": 43, "stage": 6, "row": 8},
    {"k": "awaiting-closure", "cn": "质结回呈·待闭环", "color": "#8c5a00", "tone": "橙（>30min 变红）",
     "owner": "厂长", "cond": "质检全过回呈厂长", "line": 44, "stage": 7, "row": 9},
    {"k": "delivering", "cn": "发布闸检查中", "color": "#6b4fa8", "tone": "紫",
     "owner": "厂长", "cond": "厂长启动 6 查", "line": 45, "stage": 8, "row": 10},
    {"k": "delivered", "cn": "已交付待验收", "color": "#2f6b45", "tone": "绿",
     "owner": "厂长", "cond": "交付单签发", "line": 46, "stage": 9, "row": 11},
    {"k": "accepted", "cn": "验收接受", "color": "#2f6b45", "tone": "绿",
     "owner": "发起人", "cond": "发起人验收（00 原表与 returned 同行「你验收完毕」）",
     "line": 47, "stage": 10, "row": 12},
    {"k": "returned", "cn": "验收退回", "color": "#b23b40", "tone": "红",
     "owner": "发起人", "cond": "发起人验收（00 原表与 accepted 同行「你验收完毕」）",
     "line": 47, "stage": 10, "row": 12},
    {"k": "blocked", "cn": "阻塞", "color": "#b23b40", "tone": "红",
     "owner": "—", "cond": "无席/依赖/预算挂起", "line": 48, "stage": None, "row": 13},
    {"k": "suspended", "cn": "红项搁置", "color": "#1f1f1f", "tone": "黑（+即时通知）",
     "owner": "F", "cond": "触碰红线自动搁置", "line": 49, "stage": None, "row": 14},
    {"k": "compensating", "cn": "补偿池", "color": "#7a5300", "tone": "黄",
     "owner": "F", "cond": "部分交付挂补偿", "line": 50, "stage": None, "row": 15},
]
STATE_MAP = {s["k"]: s for s in STATES}
ABNORMAL = ("blocked", "suspended", "compensating")

# 兜底映射：§3 时间轴节点关键词 → 状态（顺序=优先级；R-2：审计驳回→planning、质检不过→producing）
TIMELINE_RULES = (
    ("审计驳回", "planning"),
    ("方案修订", "planning"),
    ("重提", "planning"),
    ("质检不过", "producing"),
    ("质检不通过", "producing"),
    ("返工", "producing"),
    ("验收退回", "returned"),
    ("退回", "returned"),
    ("红线", "suspended"),
    ("搁置", "suspended"),
    ("阻塞", "blocked"),
    ("挂起", "blocked"),
    ("补偿", "compensating"),
    ("质结回呈", "awaiting-closure"),
    ("质检中", "qc"),
    ("送检", "qc"),
    ("产物提交", "qc"),
    ("审结回呈", "awaiting-release"),
    ("审结通过", "awaiting-release"),
    ("发布闸", "delivering"),
    ("交付单签发", "delivering"),
    ("已交付", "delivered"),
    ("交付完成", "delivered"),
    ("用户验收", "accepted"),
    ("验收接受", "accepted"),
    ("验收通过", "accepted"),
    ("放行", "producing"),
    ("生产开工", "producing"),
    ("合同呈批", "contract-review"),
    ("呈批", "contract-review"),
    ("用户批准", "auditing"),
    ("合同定稿", "auditing"),
    ("送审", "auditing"),
    ("审计中", "auditing"),
    ("方案挂载", "auditing"),
    ("派单开工", "planning"),
    ("排入锁定栏", "planning"),
    ("开工", "planning"),
    ("入排队池", "queued"),
    ("排队", "queued"),
    ("下单", "created"),
    ("契约签发", "created"),
    ("受理", "created"),
)

DEPT = {"A": "厂长", "B": "策划", "C": "审计", "D": "生产", "E": "质检", "F": "运保"}
_ROW_RE = re.compile(r"^\|(?!\s*-{3,}\s*\|)(.+)\|\s*$")
_KV_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*$")
_SECTION_RE = re.compile(r"^##\s+(\d+)\s+.*$", re.M)
_DWELL_MIN_RE = re.compile(r"(?:已)?停留\s*(\d+)\s*(?:min|分钟|m)\b", re.I)
_DWELL_CLOCK_RE = re.compile(r"停留\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*起")
_DWELL_H_RE = re.compile(r"(?:已)?停留\s*(\d+(?:\.\d+)?)\s*(?:h|小时)", re.I)

_CACHE = {}


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _section(text, num):
    """取 '## <num> ...' 到下一个同级 '## ' 之间的正文（不含 ### 子节标题行）。"""
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


def _strip_md(s):
    return (s or "").replace("**", "").replace("`", "").strip()


def _parse_time(s):
    """时间轴时间：'11-20 00:36:09' / '2026-11-20 00:05:34' / '00:48' → datetime（缺年补当前年）。"""
    s = (s or "").strip()
    now = datetime.datetime.now()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        return datetime.datetime(*[int(x or 0) for x in m.groups()])
    m = re.match(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        mo, d, hh, mm, ss = [int(x or 0) for x in m.groups()]
        return datetime.datetime(now.year, mo, d, hh, mm, ss)
    m = re.match(r"^(\d{2}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        hh, mm, ss = [int(x or 0) for x in m.groups()]
        return datetime.datetime(now.year, now.month, now.day, hh, mm, ss)
    return None


def _parse_header(text):
    h = {}
    for ln in text.splitlines():
        if ln.startswith("## "):
            break
        m = _KV_RE.match(ln)
        if not m:
            continue
        key = _strip_md(m.group(1))
        if key and key not in h:
            h[key] = m.group(2).strip()
    return h


def _parse_timeline(text):
    rows = []
    for ln in _section(text, 3).splitlines():
        if not ln.strip().startswith("|"):
            continue
        c = _cells(ln)
        if len(c) < 4 or not re.match(r"^\d", c[0]):
            continue
        dt = _parse_time(c[0])
        rows.append({"time": c[0], "dt": dt, "node": c[1], "who": c[2], "basis": c[3]})
    return rows


def _parse_tasks(text):
    """§9 待办表：8 列（C-20261120-005 起：待办/责任席/执行员工/会话号/状态/进度/签字/证据）
    兼容旧 5 列（待办/责任席/状态/进度/证据）。缺列降级为空串，不抛。"""
    rows = []
    for ln in _section(text, 9).splitlines():
        if not ln.strip().startswith("|"):
            continue
        c = _cells(ln)
        if len(c) < 5 or c[0] in ("待办",) or set(c[0]) <= set("-: "):
            continue
        if len(c) >= 8:  # 8 列新表：末列证据可能含 '|'，并回最后一列
            task, seat, emp, sess = c[0], c[1], c[2], c[3]
            status, progress, sign = c[4], c[5], c[6]
            evidence = "|".join(c[7:])
        else:  # 旧 5 列
            task, seat, emp, sess = c[0], c[1], "", ""
            status, progress, sign = c[2], c[3], ""
            evidence = "|".join(c[4:])
        done = ("完成" in status) or bool(re.search(r"100\s*%", progress))
        rows.append({"task": task, "seat": seat, "emp": _strip_md(emp),
                     "sess": _strip_md(sess), "status": status, "progress": progress,
                     "sign": _strip_md(sign), "evidence": evidence, "done": done})
    return rows


def _derive_from_timeline(timeline):
    """最后节点关键词映射；无命中向前回看 5 行。返回 (state, matched_row) 或 (None, None)。"""
    for row in reversed(timeline[-6:]):
        node = row.get("node") or ""
        for kw, st in TIMELINE_RULES:
            if kw in node:
                return st, row
    return None, None


def _parse_dwell(cell, timeline, now):
    """停留时长（分钟）：头部「停留 HH:MM 起 / 已停留 47min / 停留 1h」→ §3 最后节点时间。"""
    txt = _strip_md(cell or "")
    m = _DWELL_MIN_RE.search(txt)
    if m:
        return int(m.group(1)), "%dmin" % int(m.group(1))
    m = _DWELL_H_RE.search(txt)
    if m:
        mins = int(float(m.group(1)) * 60)
        return mins, ("%dh%02dm" % (mins // 60, mins % 60))
    m = _DWELL_CLOCK_RE.search(txt)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t > now:
            t -= datetime.timedelta(days=1)
        mins = int((now - t).total_seconds() // 60)
        return mins, _fmt_min(mins)
    for row in reversed(timeline):
        if row.get("dt"):
            mins = max(0, int((now - row["dt"]).total_seconds() // 60))
            return mins, _fmt_min(mins)
    return None, "—"


def _fmt_min(mins):
    if mins is None:
        return "—"
    if mins < 60:
        return "%dmin" % mins
    return "%dh%02dm" % (mins // 60, mins % 60)


def _parse_who(cell):
    txt = _strip_md(cell or "")
    m = re.search(r"在谁手上[：:]\s*([^·|]+)", txt)
    who_raw = (m.group(1) if m else "").strip()
    who_raw = re.sub(r"（sess_[^）]*）", "", who_raw).strip()
    dept = ""
    sm = re.search(r"\b([A-F])-(\d{3})\b", who_raw)
    if sm:
        dept = DEPT.get(sm.group(1), sm.group(1))
        seat = "%s-%s" % (sm.group(1), sm.group(2))
    elif re.search(r"厂长\s*(\d{3})", who_raw):
        seat = re.search(r"厂长\s*(\d{3})", who_raw).group(1)
        dept = "厂长"
    else:
        seat = who_raw
    sess = re.search(r"(sess_[0-9a-zA-Z]+)", txt)
    sess8 = (sess.group(1)[:8] + "…") if sess else ""
    return dept, seat, sess8


def parse_order(path):
    """单卡解析。任何缺段/坏表/空文件都降级为空值，不抛异常。"""
    text = _read(path)
    out = {"file": path, "code": "", "title": "", "priority": "", "p0": False,
           "header": {}, "timeline": [], "tasks": [],
           "sub_n": 0, "sub_m": 0, "stage_hdr": None,
           "dept": "", "seat": "", "sess8": "", "dwell_min": None, "dwell_txt": "—",
           "state_hdr": "", "state_tl": "", "warn": False}
    if not text.strip():
        return out
    now = datetime.datetime.now()
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        t = lines[0][2:].strip()
        if "｜" in t:
            code, _, title = t.partition("｜")
            out["code"], out["title"] = code.strip(), title.strip()
        else:
            out["title"] = t
    h = _parse_header(text)
    out["header"] = h
    out["code"] = out["code"] or _strip_md(h.get("订单编号", "")).split("（")[0].strip()
    out["priority"] = (_strip_md(h.get("优先级", "")).split() or [""])[0]
    out["p0"] = out["priority"].upper().startswith("P0")

    cur = h.get("当前状态", "")
    m = re.search(r"`([A-Za-z][A-Za-z0-9\-]*)`", cur)
    if m and m.group(1).lower() in STATE_MAP:
        out["state_hdr"] = m.group(1).lower()
    out["dept"], out["seat"], out["sess8"] = _parse_who(cur)

    prog = _strip_md(h.get("进度", ""))
    m = re.search(r"阶段\s*(\d+)\s*/\s*(\d+)", prog)
    if m:
        out["stage_hdr"] = int(m.group(1))
    m = re.search(r"子需求\s*(\d+)\s*/\s*(\d+)", prog)
    if m:
        out["sub_n"], out["sub_m"] = int(m.group(1)), int(m.group(2))

    out["timeline"] = _parse_timeline(text)
    out["tasks"] = _parse_tasks(text)
    st, _row = _derive_from_timeline(out["timeline"])
    out["state_tl"] = st or ""
    out["dwell_min"], out["dwell_txt"] = _parse_dwell(cur, out["timeline"], now)
    out["warn"] = bool(out["state_hdr"] and out["state_tl"] and out["state_hdr"] != out["state_tl"])
    return out


def _stage_keep_last(order):
    """blocked/suspended/compensating 保持上一已知阶段（不倒退、不虚增）。"""
    last = None
    for row in order.get("timeline") or []:
        node = row.get("node") or ""
        for kw, st in TIMELINE_RULES:
            if kw in node and st not in ABNORMAL and STATE_MAP[st]["stage"]:
                last = STATE_MAP[st]["stage"]
                break
    if last:
        return last
    return order.get("stage_hdr") or 1


def derive_state(order):
    """头部为主 → 时间轴兜底 → 未识别。返回 dict(state/state_cn/color/stage/warn/src)。"""
    st = order.get("state_hdr") or order.get("state_tl") or ""
    if st in STATE_MAP:
        meta = STATE_MAP[st]
        stage = meta["stage"] if meta["stage"] else _stage_keep_last(order)
        return {"state": st, "state_cn": meta["cn"], "color": meta["color"],
                "stage": stage, "warn": bool(order.get("warn")),
                "src": "header" if order.get("state_hdr") else "timeline",
                "alt_state": order.get("state_tl") if order.get("warn") else ""}
    return {"state": "unknown", "state_cn": "未识别", "color": "#b23b40",
            "stage": order.get("stage_hdr") or 1, "warn": False, "src": "unknown",
            "alt_state": ""}


def sub_progress(order):
    """子需求 n/m（卡头口径，已过质检）；任务 n/m 单独给（§9 行口径，非子需求口径）。"""
    n, m = int(order.get("sub_n") or 0), int(order.get("sub_m") or 0)
    pct = int(round(n * 100.0 / m)) if m else 0
    tasks = order.get("tasks") or []
    return {"n": n, "m": m, "pct": pct,
            "task_n": sum(1 for t in tasks if t.get("done")), "task_m": len(tasks)}


def stage_of(order):
    return derive_state(order)["stage"]


def _dwell_cls(state, mins):
    if mins is None:
        return ""
    if state in ("awaiting-release", "awaiting-closure") and mins > 30:
        return "bad"
    if mins > 120 and state not in ABNORMAL:
        return "warn"
    return ""


def _enrich(order):
    d = dict(order)
    d["timeline"] = [{k: v for k, v in row.items() if k != "dt"}
                     for row in (order.get("timeline") or [])]
    d["timeline_count"] = len(d["timeline"])
    ds = derive_state(order)
    sp = sub_progress(order)
    d.update({"state": ds["state"], "state_cn": ds["state_cn"], "color": ds["color"],
              "stage": ds["stage"], "stage_n": ds["stage"], "warn": ds["warn"],
              "state_src": ds["src"], "alt_state": ds["alt_state"],
              "sub_n": sp["n"], "sub_m": sp["m"], "sub_pct": sp["pct"],
              "task_n": sp["task_n"], "task_m": sp["task_m"],
              "dwell_cls": _dwell_cls(ds["state"], d.get("dwell_min")),
              "dwell_txt": d.get("dwell_txt") or "—",
              "owner": STATE_MAP[ds["state"]]["owner"] if ds["state"] in STATE_MAP else "—"})
    d.pop("dt", None)
    d.pop("header", None)
    d.pop("sess8", None)  # 会话标识不外泄（看板只显示部门+席位号）
    return d


def orders_dir():
    return os.environ.get("MES_ORDERS_DIR") or _DEFAULT_ORDERS


def scan_roots(path=None):
    """订单扫描口径（2026-11-24 P4b）：活单目录 + 同级 `Archive_V2_*/orders` 归档单。

    11-21 起 `orders/` 只留活单，历史单移入 `Archive_V2_20261121/orders/`。若不并扫归档，
    重建/看板会把 13 单账本缩成 1 单（已实测）。活单目录优先，同名单号以活单为准；
    仅当扫描默认目录或 MES_ORDERS_DIR 等于默认目录时并扫归档，显式传入的其他目录保持原行为。
    """
    base = path or orders_dir()
    roots = [base]
    if os.path.normpath(base) != os.path.normpath(_DEFAULT_ORDERS):
        return roots
    parent = os.path.dirname(os.path.normpath(base))
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return roots
    for name in names:
        if name.startswith("Archive_V2_"):
            p = os.path.join(parent, name, "orders")
            if os.path.isdir(p):
                roots.append(p)
    return roots


def order_path(code, base=None):
    """订单号→order.md 绝对路径（活单优先，`Archive_V2_*/orders` 归档兜底）。"""
    base = base or orders_dir()
    for root in scan_roots(base):
        p = os.path.join(root, str(code), "order.md")
        if os.path.isfile(p):
            return p
    return os.path.join(base, str(code), "order.md")


def load_all(path=None):
    """扫 orders/*/order.md（含 Archive_V2_*/orders 归档单）；按 (mtime_ns,size) 缓存；
    返回结构 + 16 态计数。"""
    base = path or orders_dir()
    out = {"dir": base, "orders": [], "states": [], "error": ""}
    counts = {s["k"]: [] for s in STATES}
    alive = set()
    seen = set()
    entries, read_ok = [], False
    for root in scan_roots(base):
        try:
            found = sorted(os.scandir(root), key=lambda e: e.name, reverse=True)
            read_ok = True
        except OSError:
            continue
        for e in found:
            if e.name in seen or not e.is_dir():
                continue
            seen.add(e.name)
            entries.append(e)
    if not read_ok:
        out["error"] = "orders dir 不可读: %s" % base
        for s in STATES:
            out["states"].append({"k": s["k"], "cn": s["cn"], "n": 0, "codes": []})
        return out
    for e in entries:
        fp = os.path.join(e.path, "order.md")
        if not os.path.isfile(fp):
            continue
        alive.add(fp)
        try:
            st = os.stat(fp)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
        cached = _CACHE.get(fp)
        if cached and cached[0] == key:
            order = cached[1]
        else:
            order = _enrich(parse_order(fp))
            _CACHE[fp] = (key, order)
        out["orders"].append(order)
        if order["state"] in counts:
            counts[order["state"]].append(order["code"])
        else:
            counts.setdefault("unknown", []).append(order["code"])
    for gone in [p for p in _CACHE if p not in alive]:
        _CACHE.pop(gone, None)
    for s in STATES:
        codes = counts.get(s["k"]) or []
        out["states"].append({"k": s["k"], "cn": s["cn"], "n": len(codes), "codes": codes})
    if counts.get("unknown"):
        out["states"].append({"k": "unknown", "cn": "未识别", "n": len(counts["unknown"]),
                              "codes": counts["unknown"]})
    return out


def dict_rows():
    """字典对照表 15 行（16 key 合并 accepted/returned 同表行；注 00 §二 原表行号）。"""
    rows, seen = [], set()
    for s in STATES:
        key = (s["row"], s["line"])
        if key in seen:
            # accepted/returned 同表行：并入上一行
            rows[-1]["keys"].append(s["k"])
            rows[-1]["cn"] += " / " + s["cn"]
            rows[-1]["tone"] += " / " + s["tone"]
            rows[-1]["color"] += " / " + s["color"]
            continue
        seen.add(key)
        rows.append({"row": s["row"], "line": s["line"], "keys": [s["k"]], "cn": s["cn"],
                     "tone": s["tone"], "color": s["color"], "owner": s["owner"],
                     "cond": s["cond"]})
    return rows


def parse_group(path):
    """group.md 群聊流（占位，C 块用）：'| 时间 | 席位 | 内容 | 指向 |' → [{t,seat,text,to}]。"""
    rows = []
    for ln in _read(path).splitlines():
        if not ln.strip().startswith("|"):
            continue
        c = _cells(ln)
        if len(c) < 4 or c[0] in ("时间",) or set(c[0]) <= set("-: "):
            continue
        rows.append({"t": c[0], "seat": c[1], "text": c[2], "to": c[3]})
    return rows


# =====================================================================================
# C-20261120-004 追加段（只追加，不改上文任何既有函数签名；整段删除即可回滚）
#   parse_group_chat(path) / parse_roster(path) / order_detail(code|path)
#   parse_shift_gantt(path) / latest_shift_path() / parse_latest_shift()
#   口径：group.md 按 `## 群聊记录` 段切分（修 T00 A2「parse_group 混 roster」）；
#         甘特按 MES系统/03-排产甘特图.md §二/§四 模板，仅认模板格式，无表一律 None（禁伪造）。
# =====================================================================================
_G_CACHE = {}        # path -> ((mtime_ns,size), text)   群聊/排班单共用
_DETAIL_CACHE = {}   # order.md path -> ((stat key...), detail)
_GANTT_CACHE = {}    # shift path  -> ((mtime_ns,size), parsed|None)

_DEFAULT_SHIFT = os.path.normpath(os.path.join(ROOT, "..", "..", "dll", "factory-blueprint", "排班单"))
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _cached_read(path):
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return ""
    hit = _G_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    text = _read(path)
    _G_CACHE[path] = (key, text)
    return text


def _heading_section(text, keyword):
    """取 `## ...keyword...` 标题行到下一个 `## ` 之间的正文（### 子标题不截断）。"""
    out, on = [], False
    for ln in text.splitlines():
        if re.match(r"^##\s+", ln):
            if on:
                break
            on = keyword in ln
            continue
        if on:
            out.append(ln)
    return "\n".join(out)


def _group_rows(path, keyword, ncols):
    rows = []
    for ln in _heading_section(_cached_read(path), keyword).splitlines():
        if not ln.strip().startswith("|"):
            continue
        c = _cells(ln)
        if len(c) < ncols or c[0] in ("时间", "席位") or set(c[0]) <= set("-: "):
            continue
        if len(c) > ncols:  # 内容含 '|' 时并回正文列，末列仍为「指向」
            c = c[:ncols - 2] + ["|".join(c[ncols - 2:-1])] + [c[-1]]
        rows.append(c)
    return rows


def parse_group_chat(path):
    """群聊段：`## 群聊记录` 起至下一个 `##`；只认 4 列；保持文件顺序。

    返回 [{t,seat,text,to}]；缺段/坏行 → 空列表，不抛。roster 行天然不在该段内。
    """
    return [{"t": c[0], "seat": c[1], "text": c[2], "to": c[3]}
            for c in _group_rows(path, "群聊记录", 4)]


def parse_roster(path):
    """成员表段：`## 成员表` → [{seat,role,joined,note}]；不混入聊天流。"""
    return [{"seat": c[0], "role": c[1], "joined": c[2], "note": c[3]}
            for c in _group_rows(path, "成员表", 4)]


_HEADER8 = (("order", "订单编号"), ("created", "下单时间"), ("requester", "发起人"),
            ("priority", "优先级"), ("eta", "预期交付"), ("risk", "风险评级"),
            ("budget", "预算"), ("route", "排班"))


def order_detail(code_or_path, base=None):
    """订单详情聚合（只读）：header8 / three_q / tasks(§9 全行) / chat(倒序最近 12)。

    code_or_path 传订单号或 order.md 路径；未知订单返回 None（渲染层出可读空态）。
    """
    if os.path.isfile(code_or_path):
        path = code_or_path
    else:
        base = base or orders_dir()
        path = order_path(str(code_or_path), base)   # 2026-11-24：归档单兜底
        if not os.path.isfile(path):
            return None
    gpath = os.path.join(os.path.dirname(path), "group.md")
    try:
        so = os.stat(path)
        sg = os.stat(gpath) if os.path.isfile(gpath) else None
        key = (so.st_mtime_ns, so.st_size, sg.st_mtime_ns if sg else 0, sg.st_size if sg else 0)
    except OSError:
        key = None
    hit = _DETAIL_CACHE.get(path)
    # TTL：员工/token 由真实会话库派生，库会变而 order.md 不变 → 2s 内复用，过期重算。
    if hit and key and hit[0] == key and time.time() - hit[2] < 2.0:
        return hit[1]

    raw = parse_order(path)
    o = _enrich(raw)
    h = raw.get("header") or {}  # _enrich 会剔除 header，这里取原始头部
    header8 = {}
    for k, label in _HEADER8:
        v = _strip_md(h.get(label, ""))
        if k == "order":
            v = v.split("（")[0].strip() or o.get("code") or ""
        header8[k] = v or "—"

    chat = list(reversed(parse_group_chat(gpath)))[:12] if os.path.isfile(gpath) else []
    tasks = [{"t": t.get("task", ""), "seat": t.get("seat", ""),
              "emp": t.get("emp", ""), "sess": t.get("sess", ""),
              "status": t.get("status", ""), "progress": t.get("progress", ""),
              "sign": t.get("sign", ""), "ev": t.get("evidence", ""),
              "done": bool(t.get("done"))}
             for t in (o.get("tasks") or [])]
    flow = parse_flow(path)
    _mark_flow_sessions(flow, tasks)
    crew = crew_of(flow, tasks)
    detail = {
        "code": o.get("code") or "",
        "title": o.get("title") or "",
        "header8": header8,
        "three_q": {
            "state": o.get("state") or "", "state_cn": o.get("state_cn") or "",
            "color": o.get("color") or "#4a6fa5",
            "stage": int(o.get("stage") or 1), "stage_n": 10,
            "sub_n": int(o.get("sub_n") or 0), "sub_m": int(o.get("sub_m") or 0),
            "dept": o.get("dept") or "", "seat": o.get("seat") or "",
            "dwell_min": o.get("dwell_min"), "dwell_txt": o.get("dwell_txt") or "—",
        },
        "tasks": tasks,
        "flow": flow,
        "flow_meta": {"has_table": bool(flow), "rows": len(flow),
                      "with_emp": sum(1 for r in flow if r.get("emp_id")),
                      "with_sess": sum(1 for r in flow if r.get("sess_ids")),
                      "with_sign": sum(1 for r in flow if r.get("sign_ok"))},
        "crew": crew,
        "chat": chat,
    }
    if key:
        _DETAIL_CACHE[path] = (key, detail, time.time())
    return detail


def shift_dir():
    return os.environ.get("MES_SHIFT_DIR") or _DEFAULT_SHIFT


def latest_shift_path(d=None):
    """排班单目录下最新 mtime 的 SHIFT-*.md；MES_SHIFT_FILE 可显式指定（fixture 测试用）。"""
    env = os.environ.get("MES_SHIFT_FILE")
    if env:
        return env
    base = d or shift_dir()
    try:
        names = [n for n in os.listdir(base) if n.startswith("SHIFT-") and n.endswith(".md")]
        paths = [os.path.join(base, n) for n in names]
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            return ""
        return max(paths, key=lambda p: os.stat(p).st_mtime_ns)
    except OSError:
        return ""


def _gantt_cell(raw):
    s = (raw or "").strip()
    out = {"raw": s, "kind": "empty", "text": "", "code": "", "sub": "", "insert": False}
    if not s:
        return out
    if s in ("—", "-", "–", "--"):
        out["kind"], out["text"] = "absent", "—"
        return out
    out["insert"] = ("插单" in s) or ("⚡" in s)
    s2 = s.replace("⚡", "").replace("插单", "").strip()
    if s2.startswith("**") and s2.endswith("**") and len(s2) >= 4:
        out["kind"], s2 = "locked", s2[2:-2].strip()
    elif s2.startswith("（") and s2.endswith("）") and len(s2) >= 2:
        out["kind"], s2 = "predicted", s2[1:-1].strip()
    else:
        out["kind"] = "scheduled"
    out["text"] = s2
    m = re.match(r"^([A-Za-z][A-Za-z0-9\-]*?)([%s])$" % _CIRCLED, s2)
    if m:
        out["code"], out["sub"] = m.group(1), m.group(2)
    else:
        m = re.match(r"^([A-Za-z][A-Za-z0-9\-]*)$", s2)
        if m:
            out["code"] = m.group(1)
    return out


def parse_shift_gantt(path):
    """排班单甘特表解析（03 §二/§四 模板）。

    识别：表头首列含「班组」，且列头为 HH / HH▶，满足含 ▶ 或 ≥8 个小时列。
    无匹配表 / 坏表 / 空文件 → **None**（渲染层出空态，绝不返回全空格子表）。
    返回 {file,snapshot,hours,rows,changes}；cells 与 hours 等长。
    """
    if not path:
        return None
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    hit = _GANTT_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]

    text = _cached_read(path)
    parsed = None
    if text.strip():
        lines = text.splitlines()
        hours, start = [], -1
        for i, ln in enumerate(lines):
            if not ln.strip().startswith("|"):
                continue
            c = _cells(ln)
            if not c or "班组" not in c[0]:
                continue
            cols, ok = [], True
            for h in c[1:]:
                m = re.match(r"^(\d{1,2})(▶)?$", h)
                if not m:
                    ok = False
                    break
                cols.append({"h": "%02d" % int(m.group(1)), "cur": bool(m.group(2))})
            if ok and cols and ("▶" in ln or len(cols) >= 8):
                hours, start = cols, i
                break
        if start >= 0 and hours:
            cur_idx = next((i for i, c in enumerate(hours) if c["cur"]), None)
            for i, c in enumerate(hours):
                if c["cur"]:
                    c["kind"] = "lock"
                elif cur_idx is None:
                    c["kind"] = "rolling"
                elif i < cur_idx:
                    c["kind"] = "archived"
                elif i <= cur_idx + 4:
                    c["kind"] = "rolling"
                else:
                    c["kind"] = "predict"
            rows = []
            for ln in lines[start + 1:]:
                if not ln.strip().startswith("|"):
                    if rows:
                        break
                    continue
                c = _cells(ln)
                if len(c) < 2:
                    continue
                if c[0].startswith("班组") or set(c[0]) <= set("-: "):
                    continue
                label = c[0]
                cells = [_gantt_cell(c[j + 1] if j + 1 < len(c) else "") for j in range(len(hours))]
                rows.append({
                    "label": label,
                    "team": label.split("（")[0].strip(),
                    "comp": (label.split("（", 1)[1].rstrip("）") if "（" in label else ""),
                    "extra": "加开" in label,
                    "cad": "CAD" in label.upper(),
                    "cells": cells,
                })
            if rows:
                m = re.search(r"快照\s*(\d{1,2}:\d{2})", text)
                changes = []
                for ln in _heading_section(text, "班中变更").splitlines():
                    if not ln.strip().startswith("|"):
                        continue
                    c = _cells(ln)
                    if len(c) < 3 or c[0] in ("时间",) or set(c[0]) <= set("-: "):
                        continue
                    changes.append({"t": c[0], "change": c[1], "basis": c[2]})
                parsed = {"file": path, "snapshot": m.group(1) if m else "",
                          "hours": hours, "rows": rows, "changes": changes}
    _GANTT_CACHE[path] = (key, parsed)
    return parsed


def parse_latest_shift(d=None):
    """最新排班单 → 甘特结构；无文件/无表 → None。"""
    return parse_shift_gantt(latest_shift_path(d))


# =====================================================================================
# C-20261120-005 追加段（只追加，不改上文既有函数签名；整段删除即可回滚）
#   parse_flow(path|text) / session_facts(ids) / crew_of(flow, tasks)
#   口径：§2.1 流程表 9 列（序/节点/流到谁/员工工号/会话号/进入/离开/结论/签字）；
#         员工数/token 只从真实会话库（ZCode session 表）+ model_usage 派生，
#         0 真实员工 = 未编成（渲染层出空态，绝不用空表或 0 充数）。
# =====================================================================================
_SESS_RE = re.compile(r"(sess_[0-9a-zA-Z-]{8,})")
_EMP_RE = re.compile(r"\bW-\d{3}\b")
_TS_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
_DEFAULT_CLI_DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")


def _cli_db_candidates():
    """候选库路径：MES_CLI_DB → 当前用户 → C:\\Users\\* 各用户（按 mtime 新→旧）。"""
    env = os.environ.get("MES_CLI_DB")
    if env:
        return [env]
    cands = [_DEFAULT_CLI_DB]
    root = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users")
    try:
        for name in os.listdir(root):
            p = os.path.join(root, name, ".zcode", "cli", "db", "db.sqlite")
            if os.path.isfile(p) and p not in cands:
                cands.append(p)
    except OSError:
        pass
    return cands


def cli_db_path():
    """真实会话库路径；MES_CLI_DB 可覆盖（测试/fixture 用）。

    SYSTEM/服务账户下 `~` 指向 systemprofile，真实库在主用户目录 → 逐候选取
    「存在的、mtime 最新」那一个；全不存在则回默认路径（保持 db_ok=false 语义）。
    """
    exist = [p for p in _cli_db_candidates() if os.path.isfile(p)]
    if not exist:
        return _DEFAULT_CLI_DB if not os.environ.get("MES_CLI_DB") else os.environ["MES_CLI_DB"]
    return max(exist, key=lambda p: os.path.getmtime(p))


def _head_block(text, num):
    """取 `## 2.1 ...` / `### 2.1 ...` 标题到下一个同级或更高级标题之间的正文。"""
    lines = text.splitlines()
    out, on, level = [], False, 0
    for ln in lines:
        m = re.match(r"^(#{1,6})\s", ln)
        if on and m and len(m.group(1)) <= level:
            break
        if not on and re.match(r"^#{2,4}\s*%s\b" % re.escape(num), ln):
            on, level = True, len(m.group(1)) if m else 3
            continue
        if on:
            out.append(ln)
    return "\n".join(out)


def _sign_ok(txt):
    s = _strip_md(txt or "")
    return bool(s) and s not in ("—", "-", "–", "--", "待签", "未签", "待填")


def parse_flow(path_or_text):
    """§2.1 编成与流转签字表 → 行列表。

    列：序/流程节点/流到谁（岗位+工号）/员工工号/会话号/进入时间/离开时间/结论/签字。
    行首必须为序号；表头/分隔行/短行跳过。缺段 → []（渲染层出「缺表」空态）。
    """
    text = path_or_text
    if "\n" not in text and os.path.isfile(text):
        text = _cached_read(text)
    rows = []
    for ln in _head_block(text, "2.1").splitlines():
        if not ln.strip().startswith("|"):
            continue
        c = _cells(ln)
        if len(c) < 6 or not re.match(r"^\d+$", c[0]):
            continue
        if len(c) >= 9:
            seq, node, to, emp, sess, enter, leave, concl = c[:8]
            sign = "|".join(c[8:])
        else:  # 短表降级：缺列补空
            c = c + [""] * (9 - len(c))
            seq, node, to, emp, sess, enter, leave, concl, sign = c[:9]
        sess_ids = _SESS_RE.findall(sess or "")
        emp_clean = _strip_md(emp)
        emp_id = (_EMP_RE.search(emp_clean).group(0) if _EMP_RE.search(emp_clean) else "")
        rows.append({
            "seq": seq, "node": _strip_md(node), "to": _strip_md(to),
            "emp": emp_clean or "—", "emp_id": emp_id,
            "sess": _strip_md(sess) or "—", "sess_ids": sess_ids,
            "enter": _strip_md(enter) or "—", "leave": _strip_md(leave) or "—",
            "concl": _strip_md(concl) or "—", "sign": _strip_md(sign) or "—",
            "sign_ok": _sign_ok(sign),
        })
    return rows


_SESS_CACHE = {"key": None, "ts": 0.0, "db_ok": False, "facts": {}}


def session_facts(ids, ttl=3.0):
    """真实会话核验（只读 session + model_usage）。

    返回 (db_ok, {sid: {exists,title,tokens,usage_rows}})。
    token 口径 = sum(computed_total_tokens) where status != 'cancelled'（与 model_ledger 一致）。
    库缺失/读失败 → db_ok=False（渲染层标「未知」，绝不伪造 0）。
    """
    ids = sorted({i for i in (ids or []) if i})
    key = tuple(ids)
    now = time.time()
    if (_SESS_CACHE["key"] == key and _SESS_CACHE["ts"]
            and now - _SESS_CACHE["ts"] < ttl):
        return _SESS_CACHE["db_ok"], _SESS_CACHE["facts"]

    facts = {i: {"exists": False, "title": "", "tokens": 0, "usage_rows": 0,
                 "resolved": ""} for i in ids}
    db_ok = False
    db = cli_db_path()
    # 没有会话号也要探一次库：db_ok 表示「库是否可读」，不能把「无会话」误报成「库不可读」。
    if os.path.isfile(db):
        con = None
        try:
            con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db), uri=True)
            # 卡面常见 `sess_abc12345…` 截断写法：按前缀唯一命中还原（76 行全表在内存比对）。
            all_sess = {sid: (title or "") for sid, title in
                        con.execute("select id,title from session")}
            resolved = {}
            for req in ids:
                if req in all_sess:
                    matched = req
                else:
                    hits = [sid for sid in all_sess if sid.startswith(req)]
                    matched = hits[0] if len(hits) == 1 else ""
                if matched:
                    resolved[req] = matched
                    facts[req]["exists"] = True
                    facts[req]["title"] = all_sess[matched]
                    facts[req]["resolved"] = matched
            full_ids = sorted(set(resolved.values()))
            if full_ids:
                ph = ",".join("?" * len(full_ids))
                usage = {sid: (int(tok or 0), int(n or 0)) for sid, tok, n in con.execute(
                    "select session_id, sum(ifnull(computed_total_tokens,0)), count(*) "
                    "from model_usage where session_id in (%s) and status != 'cancelled' "
                    "group by session_id" % ph, full_ids)}
                for req, full in resolved.items():
                    tok, n = usage.get(full, (0, 0))
                    facts[req]["tokens"] = tok
                    facts[req]["usage_rows"] = n
            db_ok = True
        except sqlite3.Error:
            db_ok = False
        finally:
            if con is not None:
                con.close()
    _SESS_CACHE.update({"key": key, "ts": now, "db_ok": db_ok, "facts": facts})
    return db_ok, facts


def crew_of(flow, tasks=None):
    """执行员工 / 会话 / token 派生（§2.1 ∪ §9，按会话去重）。

    - 员工数：工号 W-xxx 且名下至少 1 个会话在真实会话库中存在（否则不计）。
    - 真实会话：卡面引用的 sess_ 在 session 表存在即真；「待填/—」不计。
    - token：真实会话的 model_usage.computed_total_tokens 合计（status≠cancelled）。
    0 真实员工 → empty=True，渲染层显示「未编成」。
    """
    rows = list(flow or [])
    for t in (tasks or []):
        rows.append({"emp_id": (_EMP_RE.search(t.get("emp") or "").group(0)
                                if _EMP_RE.search(t.get("emp") or "") else ""),
                     "sess_ids": _SESS_RE.findall(t.get("sess") or ""),
                     "emp": t.get("emp") or "", "sess": t.get("sess") or ""})
    all_ids = set()
    emp_sess = {}
    for r in rows:
        ids = list(r.get("sess_ids") or [])
        all_ids.update(ids)
        e = r.get("emp_id") or ""
        if e:
            emp_sess.setdefault(e, set()).update(ids)
    db_ok, facts = session_facts(all_ids)

    def _targets(ids):
        """请求号 → 唯一目标（截断号按前缀还原）；未命中的保留原号，便于报「查无」。"""
        return {s: (facts[s].get("resolved") or s) for s in ids}

    def _real(ids):
        return {facts[s]["resolved"] for s in ids if facts.get(s, {}).get("exists")}

    employees = sorted(e for e, ids in emp_sess.items() if _real(ids))
    real_targets = _real(all_ids)
    emp_targets = set()
    for e in employees:
        emp_targets |= _real(emp_sess[e])
    tok_by = {facts[s]["resolved"]: facts[s]["tokens"] for s in all_ids if facts[s].get("exists")}
    tokens = sum(tok_by.get(t, 0) for t in real_targets)
    tokens_emp = sum(tok_by.get(t, 0) for t in emp_targets)
    all_targets = set(_targets(all_ids).values())
    return {
        "db_ok": db_ok, "db": cli_db_path(),
        "employees": employees, "n_emp": len(employees),
        "emp_pending": sorted(e for e, ids in emp_sess.items() if not _real(ids)),
        "sess_total": len(all_targets), "sess_real": len(real_targets),
        "sess_missing": sorted(s for s in all_ids if facts.get(s, {}).get("exists") is False),
        "tokens": tokens, "tokens_emp": tokens_emp,
        "token_sess": len({facts[s]["resolved"] for s in all_ids
                           if facts[s].get("exists") and facts[s]["usage_rows"]}),
        "empty": len(employees) == 0,
        "source": "session+model_usage",
    }


def _mark_flow_sessions(flow, tasks):
    """给 §2.1 行与 §9 行挂真实会话核验结果：True 真 / False 查无 / None 未填或库不可读。"""
    ids = set()
    for r in list(flow or []) + list(tasks or []):
        ids.update(r.get("sess_ids") or [])
    db_ok, facts = session_facts(ids)

    def mark(row):
        sids = row.get("sess_ids") or []
        if not db_ok or not sids:
            row["sess_ok"] = None
        else:
            row["sess_ok"] = any(facts.get(s, {}).get("exists") for s in sids)
        return row
    for r in (flow or []):
        mark(r)
    for t in (tasks or []):
        t["sess_ids"] = _SESS_RE.findall(t.get("sess") or "")
        mark(t)

# --- C-20261120-010 T01 追加（只追加：常量 + 一个查询函数 + 导出；解析/推导/渲染不改） ---
# returned 的法定出路不是 transition，而是「立返修单」：原单终态不变，返修单从 created 起跑。
RETURN_ACTIONS = ("repair-open",)


def allowed_actions(state):
    """给定状态返回合法动作元组（returned 只有 repair-open，其余走 transition）。"""
    if state == "returned":
        return RETURN_ACTIONS
    if state in STATE_MAP:
        return ("transition",)
    return ()


__all__ = [
    "STATES", "STATE_MAP", "ABNORMAL", "TIMELINE_RULES", "RETURN_ACTIONS",
    "allowed_actions", "parse_order", "derive_state", "stage_of",
    "sub_progress", "dict_rows", "load_all", "orders_dir",
    "parse_group", "parse_group_chat",
]
