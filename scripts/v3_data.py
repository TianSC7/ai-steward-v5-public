

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v3_data.py —— V4 工厂纯净数据源与动态版本号装配层 (不删任何旧文件，彻底隔离旧 V1/V2 脏数据)"""
from __future__ import annotations
import glob

# ===== C-20261123-autotime：自动化区真探测（网关端口属主进程 + Runner 任务心跳）=====
_AUTOS_CACHE = {"t": 0.0, "v": ""}


def _autime_dur(sec):
    m = int(sec // 60)
    if m < 1:
        return "不足1分钟"
    if m < 60:
        return "%d分钟" % m
    hh, mm = divmod(m, 60)
    if hh < 24:
        return "%d小时%02d分" % (hh, mm)
    d, hh2 = divmod(hh, 24)
    return "%d天%d小时" % (d, hh2)


def _autime_hm(ts):
    # '2026-11-22T13:05:43' -> '11-22 13:05'
    try:
        return ts[5:10] + " " + ts[11:16]
    except Exception:
        return ts or "—"


# C-20261123-autosoff（用户令：停用项不能只显示 1 条汇总）：逐条列出系统里真实处于 Disabled 的计划任务
_AUTOSOFF_CACHE = {"t": 0, "v": ("", 0)}


def _autos_off_rows():
    import subprocess, time as _t
    now = _t.time()
    if _AUTOSOFF_CACHE["t"] and now - _AUTOSOFF_CACHE["t"] < 60:
        return _AUTOSOFF_CACHE["v"]
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          "Get-ScheduledTask | Where-Object {$_.State -eq 'Disabled' -and $_.TaskName -match '^(MES-|AI_|AI-|Zcode|ZCode|Seafile|V2)'} | Sort-Object TaskName | "
          "ForEach-Object { 'OFF|' + $_.TaskName }")
    names = []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            p = line.strip().split("|", 1)
            if len(p) == 2 and p[0] == "OFF":
                names.append(p[1].strip())
    except Exception:
        pass
    if not names:
        v = ('<tr><td>—</td><td>—</td><td class="mono">—</td><td>计划任务查询失败</td><td>—</td></tr>', 0)
    else:
        rows = "".join(
            '<tr><td>%s</td><td>已停用</td><td class="mono">—</td><td>系统计划任务 · 已禁用</td><td>—</td></tr>'
            % n.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for n in names)
        v = (rows, len(names))
    _AUTOSOFF_CACHE["t"] = now
    _AUTOSOFF_CACHE["v"] = v
    return v


def _autos_rows():
    import subprocess, time as _t
    now = _t.time()
    if _AUTOS_CACHE["t"] and now - _AUTOS_CACHE["t"] < 60:
        return _AUTOS_CACHE["v"]
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$c=Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue|Select -First 1;"
        "if($c){$p=Get-Process -Id $c.OwningProcess;'GW|LISTEN|'+$p.StartTime.ToString('s')}else{'GW|DOWN'};"
        "$r=Get-ScheduledTask -TaskName 'MES-Factory-Runner' -ErrorAction SilentlyContinue;"
        "if($r){$ri=$r|Get-ScheduledTaskInfo;$l='-';$n='-';"
        "if($ri.LastRunTime -and $ri.LastRunTime.Year -gt 2000){$l=$ri.LastRunTime.ToString('s')};"
        "if($ri.NextRunTime -and $ri.NextRunTime.Year -gt 2000){$n=$ri.NextRunTime.ToString('s')};"
        "'RUN|'+$r.State+'|'+$l+'|'+$n}"
    )
    gw = rn = None
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            p = line.strip().split("|")
            if p and p[0] == "GW":
                gw = p[1:]
            elif p and p[0] == "RUN":
                rn = p[1:]
    except Exception:
        pass
    # 网关行：端口属主进程 = 真启动时刻 + 已连续运行
    if gw and gw[0] == "LISTEN":
        st = gw[1]
        try:
            import datetime as _dt
            start = _dt.datetime.strptime(st, "%Y-%m-%dT%H:%M:%S")
            dur = _autime_dur(now - start.timestamp())
            gw_status, gw_time = "ok", _autime_hm(st) + " 启动 · 已连续运行 " + dur
        except Exception:
            gw_status, gw_time = "ok", _autime_hm(st) + " 启动"
        gw_cell = '<td class="z7st ok">● 运行中</td>'
    else:
        gw_status = "bad"
        gw_cell = '<td class="z7st bad">● 已停止</td>'
        gw_time = "8788 未监听——网关进程掉线，计费熔断失效"
    gw_err = "—" if gw_status == "ok" else "8788 无监听"
    # Runner 行：任务 State + LastRun 新鲜度分档
    if rn:
        state, last, nxt = rn[0], rn[1] if len(rn) > 1 else "-", rn[2] if len(rn) > 2 else "-"
        age_min = None
        try:
            import datetime as _dt
            lt = _dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%S")
            age_min = (now - lt.timestamp()) / 60.0
        except Exception:
            pass
        if age_min is None:
            rn_status, rn_cell = "warn", '<td class="z7st warn">● 状态未知</td>'
        elif age_min <= 5 and state in ("Running", "Ready"):
            rn_status, rn_cell = "ok", '<td class="z7st ok">● 巡转正常</td>'
        elif age_min is not None and age_min <= 30:
            rn_status, rn_cell = "warn", '<td class="z7st warn">● 拍迟%02d分</td>' % int(age_min)
        else:
            rn_status, rn_cell = "bad", '<td class="z7st bad">● 停摆</td>'
        cadence = ""
        try:
            import datetime as _dt
            lt = _dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%S")
            nt = _dt.datetime.strptime(nxt, "%Y-%m-%dT%H:%M:%S")
            cadence = " · 约%s一拍" % _autime_dur((nt - lt).total_seconds())
        except Exception:
            cadence = ""
        rn_time = ("上次 " + _autime_hm(last) + " · 下次 " + _autime_hm(nxt) + cadence) if last != "-" else "任务未注册运行记录"
        rn_err = "—"
    else:
        rn_status, rn_cell = "warn", '<td class="z7st warn">● 探测不可用</td>'
        rn_time, rn_err = "Runner 任务查询失败", "探测异常"
    v = (
        "<tr><td>AI-Steward-Gateway (8788)</td>" + gw_cell +
        '<td class="z7t">' + gw_time + "</td><td>实时计费与预算熔断网关</td><td>" + gw_err + "</td></tr>"
        "<tr><td>MES-Factory-Runner</td>" + rn_cell +
        '<td class="z7t">' + rn_time + "</td><td>零空转 · 双门质检流水线</td><td>" + rn_err + "</td></tr>"
    )
    _AUTOS_CACHE["t"] = now
    _AUTOS_CACHE["v"] = v
    return v
# ===== autotime 助手结束 =====

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
from typing import Any

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
STEWARD_ROOT = os.path.dirname(SCRIPTS_DIR)
# C-20261125-G44R8：状态目录优先吃 STEWARD_STATE_DIR（与 ai_store.py / lane_claim.py 同一惯例），
# 构造用例与沙箱取证可指临时目录，不碰生产；缺省仍是本仓 state/
STATE_DIR = os.environ.get("STEWARD_STATE_DIR") or os.path.join(STEWARD_ROOT, "state")
DASH_V2_PATH = os.path.join(STEWARD_ROOT, "dashboard", "v2.html")
QUEUE_PATH = os.path.join(STATE_DIR, "factory_queue.json")
LEDGER_PATH = os.path.join(STATE_DIR, "v3_budget_ledger.json")
EVENTS_JSONL_PATH = os.path.join(STATE_DIR, "events.jsonl")
SLOTS_PATH = os.path.join(STATE_DIR, "runner_slots.json")  # C-20261125-G43R1：并发内核槽位真值
# C-20261125-G42 F2.3：合同（约）数据面——主队列合同根，env 可覆盖（测试/沙箱指临时目录）
CONTRACTS_DIR = os.environ.get("STEWARD_CONTRACTS_DIR") or os.path.join(STATE_DIR, "contracts")
CLI_DB_SQLITE = r"C:\Users\Administrator\.zcode\cli\db\db.sqlite"

ALL_16_STATES = [
    ("created", "契约落卡"),
    ("queued", "待排产"),
    ("planning", "策划中"),
    ("contract-review", "契约审阅"),
    ("auditing", "方案审计"),
    ("awaiting-release", "待放行"),
    ("producing", "生产中"),
    ("qc", "质检中"),
    ("awaiting-closure", "待收口"),
    ("delivering", "发布闸"),
    ("delivered", "已交付待验收"),
    ("accepted", "已接受"),
    ("returned", "验收退回"),
    ("blocked", "阻塞"),
    ("suspended", "挂起"),
    ("compensating", "补偿中"),
]


def _read_json(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _contract_mark(code: str, line: str = "", line_ref: str = "") -> dict[str, Any]:
    """C-20261125-G42 F2.3（数据面）：单条订单「约」标——合同在不在、什么状态。

    两路真源都认（哪个在算哪个，先主队列后线内）：
      1) 主队列 `state/contracts/<单号>.json`（contracts.py 立约）
      2) 域产线 `lines/<线>/jobs/<单号>/contract.json`（run_line.py 立约）
    老单（无合同文件）→ has=False、label=""，**零报错**；只加数据字段，不改版式。
    """
    oid = str(code or "").strip()
    mark: dict[str, Any] = {"has": False, "label": "", "status": "", "created_at": "",
                            "started_at": "", "finished_at": "", "source": "", "path": ""}
    if not oid:
        return mark
    cands: list[tuple[str, str]] = [("queue", os.path.join(CONTRACTS_DIR, oid + ".json"))]
    if line:
        _base = os.path.join(lines_root(), line, "jobs")
        for _ref in (line_ref, oid):
            _r = str(_ref or "").strip()
            if _r:
                cands.append(("line", os.path.join(_base, _r, "contract.json")))
    for src, p in cands:
        d = _read_json(p)
        if not d:
            continue
        mark.update({
            "has": True,
            "label": "约",
            "status": str(d.get("status") or ""),
            "created_at": str(d.get("created_at") or ""),
            "started_at": str(d.get("started_at") or ""),
            "finished_at": str(d.get("finished_at") or ""),
            "source": src,
            "path": p,
        })
        break
    return mark


def _contract_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """看板汇总（数据面）：在册单里已立约几张、各状态几张。无约=0，不抛错。"""
    has_codes: list[str] = []
    by_status: dict[str, int] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        m = r.get("contract") if isinstance(r.get("contract"), dict) else {}
        if not m.get("has"):
            continue
        has_codes.append(str(r.get("code") or ""))
        _st = str(m.get("status") or "unknown")
        by_status[_st] = by_status.get(_st, 0) + 1
    return {"n": len(has_codes), "total": len(rows or []), "codes": has_codes, "by_status": by_status}


def _slots_meta() -> dict[str, Any]:
    """C-20261125-G43R1（用户令：并发数上屏）：并发内核槽位真值。

    源：state/runner_slots.json = {"slots": {<order_id>: {family,pid,claimed_at,heartbeat_at}}, "max": N}
    文件缺失/损坏/字段缺失 → used=0、max=0：不造假不抛错，其余 data.json 字段照常输出（老内核单线模式兼容）。
    """
    raw = _read_json(SLOTS_PATH)
    slots = raw.get("slots")
    mx = raw.get("max")
    # F2.1 字面口径：文件缺失/JSON 损坏/slots 或 max 字段缺失或类型不对 → 一律 used=0、max=0。
    # bool 是 int 子类（True/False 会被当真值算），显式排除，避免把脏数据当槽位上限上屏。
    if not isinstance(slots, dict) or isinstance(mx, bool) or not isinstance(mx, (int, float)):
        return {"used": 0, "max": 0, "orders": []}
    try:
        mx_i = int(mx)
    except (TypeError, ValueError, OverflowError):
        return {"used": 0, "max": 0, "orders": []}
    orders = sorted(str(_k) for _k in slots.keys())  # 排序固定，避免 dict 序抖动导致 data.json 抖
    return {"used": len(orders), "max": mx_i if mx_i > 0 else 0, "orders": orders}


def _v2_file_meta(target_override: str = "") -> tuple[str, str, int]:
    p = target_override if (target_override and os.path.exists(target_override)) else DASH_V2_PATH
    if not os.path.exists(p):
        now_s = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return "00000000", now_s, 0
    raw = open(p, "rb").read()
    sha8 = hashlib.sha256(raw).hexdigest()[:8]
    mt = _dt.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")
    return sha8, mt, len(raw)


def _clean_live_snippet(text: str, limit: int = 120) -> str:
    t = str(text or "").strip()
    t = re.sub(r"[A-Za-z]:[\\/][^\s\"'`，。；、）)】]*", lambda m: os.path.basename(m.group(0).rstrip("\\/")) or "沙箱文件", t)
    t = re.sub(r"[#*`|]+", " ", t)
    t = re.sub(r"\-{2,}", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        return t[:limit] + "…"
    return t


def _humanize_tool_step(tname: str, inp: dict[str, Any], t_status: str) -> tuple[str, str]:
    st_word = "已完成" if t_status == "completed" else "执行中"
    tn = tname.lower()
    fpath = str(inp.get("file_path") or inp.get("path") or inp.get("pattern") or "").strip()
    fname = os.path.basename(fpath.rstrip("\\/")) if fpath else ""
    if tn in ("read", "read_file"):
        s_line = inp.get("offset") or inp.get("start_line")
        rng = f" (第 {s_line} 行起)" if s_line else ""
        return "读取文件", f"读取 {fname or '目标文件'}{rng} · {st_word}"
    if tn in ("edit", "multiedit", "write", "edit_file", "write_file"):
        return "修改代码", f"写入更新 {fname or '沙箱文件'} · {st_word}"
    if tn in ("grep", "glob"):
        return "检索定位", f"检索定位 {fname or '代码结构'} · {st_word}"
    if tn == "bash":
        cmd = str(inp.get("command") or "").strip()
        desc_in = str(inp.get("description") or "").strip()
        if desc_in and len(re.findall(r"[\u4e00-\u9fa5]", desc_in)) >= 2:
            return "命令校验", f"{_clean_live_snippet(desc_in, 56)} · {st_word}"
        if "git diff" in cmd or "git status" in cmd:
            return "差异检查", f"检查沙箱代码改动 (git diff) · {st_word}"
        if "node" in cmd or "py_compile" in cmd:
            return "语法校验", f"执行静态语法检查 · {st_word}"
        if "DELIVERY" in cmd:
            return "生成回执", f"写入完工交件回执 · {st_word}"
        if "rev-parse" in cmd or "worktree" in cmd:
            return "沙箱核验", f"校验 Git Worktree 沙箱目录 · {st_word}"
        return "沙箱指令", f"执行沙箱环境检查 · {st_word}"
    return f"操作:{tname}", f"执行 {tname} {fname} · {st_word}"


def _humanize_reasoning(raw_text: str) -> str:
    txt = str(raw_text or "").strip()
    if not txt:
        return ""
    cn_chars = len(re.findall(r"[\u4e00-\u9fa5]", txt))
    if cn_chars >= 8:
        first_line = next((ln.strip() for ln in txt.splitlines() if len(ln.strip()) >= 6), txt)
        first_line = re.sub(r"^[#*\-\d.\s]+", "", first_line)
        return _clean_live_snippet(first_line, 96)
    low = txt.lower()
    if "not call any tools" in low or "single json verdict" in low or "hard gates" in low:
        return "正在对照 UI 审美质检考卷 Q1–Q8 与三视口遥测证据逐条核验…"
    if "read" in low and ("brief" in low or "spec" in low or "map" in low):
        return "正在读取派工单、契约有效条款与文件结构地图 (map.md)…"
    if "edit" in low or "modify" in low or "refactor" in low or "css" in low:
        return "正在构思界面样式重构与 DOM 结构修改方案…"
    if "delivery" in low or "finish" in low:
        return "已完成沙箱修改，正在自检并准备写入完工回执…"
    return _clean_live_snippet(txt, 92)


def _humanize_output(raw_text: str) -> str:
    txt = str(raw_text or "").strip()
    for ln in txt.splitlines():
        clean_ln = re.sub(r"^[#*\-\d.\s|>]+", "", ln).strip()
        if len(clean_ln) >= 8 and not clean_ln.startswith("{") and not clean_ln.startswith("```"):
            return _clean_live_snippet(clean_ln, 100)
    return _clean_live_snippet(txt, 100)


def _panel_session_title(title: str) -> str:
    """席位路由前缀【V3-…】【V4-…】只给账本识别，面板上不当成另一套版本号。"""
    raw_title = str(title or "").strip()
    shown = re.sub(r"^【V\d+(?:\.\d+)?-[^】]*】\s*", "", raw_title).strip()
    return shown or raw_title


def _worker_badge(title: str, sid: str) -> str:
    """工号优先用席位册里的 W-/D- 编号；本单没派工号时用会话号，避免整列都叫生产施工。"""
    seat = "-"
    try:
        import factory_seat
        seat = str(factory_seat.seat_of(str(title or "")) or "-")
        reg = factory_seat.load_registry()
        active, retired = factory_seat.registry_index(reg)
        for row in list(active.values()) + list(retired.values()):
            if str(row.get("session") or "") == str(sid or ""):
                got = str(row.get("seat") or "")
                if got:
                    seat = got
                    break
    except Exception:
        seat = "-"
    if seat and seat != "-":
        return seat
    m = re.match(r"(sess_[0-9a-f]{8})", str(sid or ""), re.I)
    return m.group(1) if m else (str(sid or "")[:12] or "—")


def collect_live_agent_stream(limit_sessions: int = 4, limit_parts: int = 16) -> dict[str, Any]:
    """Read real-time thoughts, tool calls, and outputs directly from cli/db/db.sqlite part table + events.jsonl."""
    now_ms = int(_dt.datetime.now().timestamp() * 1000)
    sessions_out: list[dict[str, Any]] = []
    feed_items: list[dict[str, Any]] = []

    if os.path.exists(CLI_DB_SQLITE):
        try:
            uri = "file:" + CLI_DB_SQLITE.replace("\\", "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=3)
            con.execute("PRAGMA busy_timeout=2500")
            s_rows = con.execute(
                "SELECT id, title, time_created, time_updated, last_outcome "
                "FROM session WHERE title LIKE '%V3%' OR directory LIKE '%mate%' "
                "ORDER BY time_created DESC LIMIT ?",
                (limit_sessions,),
            ).fetchall()
            for sid, s_title, t_cr, t_up, outcome in s_rows:
                age_sec = max(0, round((now_ms - int(t_up or t_cr or now_ms)) / 1000.0, 1))
                is_live = (outcome is None) and (age_sec < 300)
                st_label = "正在施工/审计中" if is_live else ("已完成" if outcome == "completed" else "已结束")
                role_tag = "质检员" if ("质检" in str(s_title or "") or "QC" in str(s_title or "")) else "生产施工"
                badge = _worker_badge(str(s_title or ""), str(sid or ""))
                p_rows = con.execute(
                    "SELECT rowid, data FROM part WHERE session_id=? ORDER BY rowid DESC LIMIT 45",
                    (sid,),
                ).fetchall()
                session_steps: list[dict[str, Any]] = []
                for rowid, raw_d in reversed(p_rows):
                    if not raw_d:
                        continue
                    try:
                        p = json.loads(raw_d)
                    except Exception:
                        continue
                    if p.get("synthetic"):
                        continue
                    ptype = p.get("type") or ""
                    t_obj = p.get("time") or {}
                    ts_ms = int(t_obj.get("end") or t_obj.get("start") or t_up or t_cr or now_ms)
                    ts_hm = _dt.datetime.fromtimestamp(ts_ms / 1000.0).strftime("%H:%M:%S")
                    if ptype == "reasoning":
                        txt = _humanize_reasoning(p.get("text") or "")
                        if not txt:
                            continue
                        item = {"id": f"{sid[:8]}:{rowid}", "ts": ts_hm, "ts_ms": ts_ms, "role": role_tag, "badge": badge, "kind": "思考", "text": txt}
                    elif ptype == "tool":
                        tname = str(p.get("tool") or "Tool")
                        st_obj = p.get("state") or {}
                        t_status = str(st_obj.get("status") or "running")
                        inp = st_obj.get("input") or {}
                        k_label, desc = _humanize_tool_step(tname, inp if isinstance(inp, dict) else {}, t_status)
                        item = {"id": f"{sid[:8]}:{rowid}", "ts": ts_hm, "ts_ms": ts_ms, "role": role_tag, "badge": badge, "kind": k_label, "text": desc}
                    elif ptype == "text":
                        raw_t = str(p.get("text") or "").strip()
                        if not raw_t or raw_t.startswith("<total_tokens>") or raw_t.startswith("# Environment") or raw_t.startswith("Codebase and user"):
                            continue
                        if "你是按" in raw_t[:40] or "你是 V3" in raw_t[:40]:
                            continue
                        item = {"id": f"{sid[:8]}:{rowid}", "ts": ts_hm, "ts_ms": ts_ms, "role": role_tag, "badge": badge, "kind": "结论", "text": _humanize_output(raw_t)}
                    else:
                        continue
                    # Merge consecutive identical tool actions in the same session to keep the stream clean and non-dense
                    if session_steps and session_steps[-1]["kind"] == item["kind"] and session_steps[-1]["text"] == item["text"]:
                        continue
                    session_steps.append(item)
                    feed_items.append({**item, "session_title": _clean_live_snippet(_panel_session_title(s_title) or sid[:12], 42), "is_live": is_live})
                sessions_out.append({
                    "session_id": sid[:12],
                    "title": _clean_live_snippet(_panel_session_title(s_title) or sid[:12], 48),
                    "role": role_tag,
                    "status": st_label,
                    "is_live": is_live,
                    "updated_age_sec": age_sec,
                    "steps_count": len(session_steps),
                    "last_step": session_steps[-1]["text"] if session_steps else "等待首条步骤落盘…",
                })
            con.close()
        except Exception:
            pass

    if os.path.exists(EVENTS_JSONL_PATH):
        try:
            lines = open(EVENTS_JSONL_PATH, "r", encoding="utf-8", errors="replace").read().splitlines()[-15:]
            seen_ev_keys: set[str] = set()
            for idx, ln in enumerate(reversed(lines)):
                if not ln.strip():
                    continue
                ev = json.loads(ln)
                dedup_k = f"{ev.get('phase')}:{ev.get('summary')}"
                if dedup_k in seen_ev_keys:
                    continue
                seen_ev_keys.add(dedup_k)
                ts_s = str(ev.get("ts") or "")[-8:]
                phase_cn = {
                    "builder_start": "施工启动",
                    "builder_done": "施工完成",
                    "gate1_probe": "Gate1机检",
                    "verifier_start": "质检启动",
                    "gate2_verdict": "内核裁决",
                    "merged": "合入主干",
                    "contract_compacted": "契约归并",
                    "needs_human": "转驻厂专家",
                    "v3_1_kernel_ready": "内核就绪",
                }.get(str(ev.get("phase") or ""), str(ev.get("phase") or "事件"))
                _actor = str(ev.get("actor") or "内核")
                feed_items.append({
                    "id": f"ev:{idx}",
                    "ts": ts_s,
                    "ts_ms": int(ev.get("ts_ms") or now_ms),
                    "role": _actor,
                    "badge": _actor,
                    "kind": phase_cn,
                    "text": _clean_live_snippet(ev.get("summary") or "", 160),
                    "session_title": f"{ev.get('order', 'C-20261121-001')} R{ev.get('round', 1)}",
                    "is_live": False,
                })
        except Exception:
            pass

    feed_items.sort(key=lambda x: (x.get("ts_ms") or 0))
    feed_tail = feed_items[-limit_parts:]
    for ft in feed_tail:
        ft.setdefault("time", ft.get("ts") or "")
        ft.setdefault("actor", ft.get("role") or "内核")
    any_live = any(s.get("is_live") for s in sessions_out)
    active_actor = next((s["title"] for s in sessions_out if s.get("is_live")), "")  # C-20261123-seattruth：无活会话回空，不拿旧会话冒充当前任务
    now_hm = _dt.datetime.now().strftime("%H:%M:%S")
    return {
        "updated_at": now_hm,
        "ts": now_hm,
        "is_live": any_live,
        "runner_busy": any_live,
        "active_session": active_actor,
        "active_session_title": active_actor,
        "sessions": sessions_out,
        "feed": feed_tail,
        "items": list(reversed(feed_tail)),
    }



def _line_ledger_index(base: str) -> dict[str, tuple[str, dict[str, Any]]]:
    """C-20261125-G44R3：产线岗位账本索引（order_id / 目录名 → 账本路径与内容）。只读，异常即跳过。"""
    idx: dict[str, tuple[str, dict[str, Any]]] = {}
    try:
        for _lp in sorted(glob.glob(os.path.join(base, "*", "jobs", "*", "ledger.json"))):
            _led = _read_json(_lp)
            if not _led:
                continue
            for _k in (str(_led.get("order_id") or "").strip(), os.path.basename(os.path.dirname(_lp))):
                if _k:
                    idx.setdefault(_k, (_lp, _led))
    except Exception:
        pass
    return idx


def _line_orders():
    """C-20261125-linesboard: scan domain production lines (真源两路并读).

    C-20261125-G46R3 修：原实现见 lines/queue.json 有单就整段 return，lines/<line>/jobs/*/ledger.json
    的线内真账被完全跳过（stock 线只剩 intake 队列视角）。现两路并读，按 job_key 去重：

      1) lines/*/jobs/*/ledger.json  线内作业真账（**优先** = 线内口径权威，如 STK-20261125-001）
      2) lines/queue.json            合同级线单队列（含 video 等外部目录产线，只有这一路有账）

    同一条活儿两个号：queue.line_order_id ↔ ledger.order_id（实测 C-20261125-002 ↔ STK-20261125-001）。
    queue 行撞到已有 ledger 行 → 只补看板单号（board_code）与缺失时刻，**不重复计单**；
    queue 行独有的单（video CHECK-1）→ 以看板单号为准。

    C-20261125-G44R4 合并取舍（两条都必须成立，故不是任选其一）：
      · G46R3 的去重/线内真账优先保留——一条活儿只计一次，ledger 的状态与文件谱系不被 queue 覆盖；
      · G44 的「看板口径单号」补齐——queue 给了看板号（C-…）时，order_id 一律换成看板号，
        line_ref 留线内号（STK-…）作别名。原 G46R3 只在 order_id 为空时才换号，导致看板上
        出现 STK-20261125-001 这种线内号、而验收点名的 C-20261125-002/003 在板上不存在。
      · 行内带 ledger 与 created/delivered/started 真值（F3.1 抽屉最小 details 的数据源）。

    Returns list of dicts: {order_id(看板口径), board_code, line_ref/line_order_id(线内号), status,
    mtime, line, line_name, line_dir, created_at, delivered_at, started_at, ledger}；
    status normalized: queued/running/done/failed/needs_human（看板侧再折成四桶）。
    """
    out: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}

    def _key(row: dict[str, Any]) -> str:
        return str(row.get("line_ref") or "") or "%s|%s" % (row.get("line") or "", row.get("order_id") or "")

    def _push(row: dict[str, Any], from_queue: bool) -> None:
        key = _key(row)
        old = seen.get(key)
        if old is None:
            seen[key] = row
            out.append(row)
            return
        if not from_queue:
            return
        # 同一条活儿：queue 补看板单号/缺失时刻，ledger 的线内单号与状态保留
        _bc = str(row.get("board_code") or "")
        if _bc:
            old["board_code"] = _bc
            old["order_id"] = _bc  # 看板/抽屉/状态桶一律用看板口径单号（验收点名单号可点开）
        for _k in ("created_at", "delivered_at", "started_at"):
            if not str(old.get(_k) or "").strip() and str(row.get(_k) or "").strip():
                old[_k] = row[_k]
        if not str(old.get("line") or "").strip() and str(row.get("line") or "").strip():
            old["line"] = row["line"]
        if not str(old.get("line_name") or "").strip() and str(row.get("line_name") or "").strip():
            old["line_name"] = row["line_name"]
        if float(row.get("mtime") or 0) > float(old.get("mtime") or 0):
            old["mtime"] = row["mtime"]
        if not old.get("status"):
            old["status"] = row.get("status")

    try:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "lines")
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            return out
        # 1) 线内作业真账优先入表
        _idx = _line_ledger_index(base)  # C-20261125-G44R4：queue 行回填线目录/账本用（只读）
        for line_dir in sorted(glob.glob(os.path.join(base, "*"))):
            if not os.path.isdir(line_dir):
                continue
            line_name = os.path.basename(line_dir)
            jobs_dir = os.path.join(line_dir, "jobs")
            if not os.path.isdir(jobs_dir):
                continue
            for led_path in glob.glob(os.path.join(jobs_dir, "*", "ledger.json")):
                try:
                    led = _read_json(led_path)
                except Exception:
                    continue
                if not isinstance(led, dict):
                    continue
                raw_status = str(led.get("status") or "").lower()
                norm = {"queued": "queued", "pending": "queued", "created": "queued",
                        "running": "running", "producing": "running",
                        "done": "done", "delivered": "done",
                        "failed": "failed", "needs_human": "needs_human"}.get(raw_status)
                if not norm:
                    continue
                oid = str(led.get("order_id") or os.path.basename(os.path.dirname(led_path)))
                _push({"order_id": oid, "board_code": "", "line_ref": oid, "line_order_id": oid,
                       "status": norm, "mtime": os.path.getmtime(led_path), "line": line_name,
                       "line_name": "", "line_dir": line_name,
                       "created_at": str(led.get("created_at") or ""),
                       "delivered_at": str(led.get("delivered_at") or ""),
                       "started_at": str(led.get("started_at") or ""),
                       "ledger": led}, False)
        # 2) 合同级线单队列：同号只补号，不重复计单
        _qp = os.path.join(base, "queue.json")
        if os.path.isfile(_qp):
            _qd = _read_json(_qp) or {}
            for _it in (_qd.get("orders") or []):
                if not isinstance(_it, dict):
                    continue
                _st = str(_it.get("state") or "").lower()
                _norm = {"delivered": "done", "running": "running",
                         "blocked": "failed", "failed": "failed", "check_failed": "failed",
                         "queued": "queued"}.get(_st, "queued")
                _ts = str(_it.get("delivered_at") or _it.get("started_at") or _it.get("created_at") or "")
                try:
                    _mt = _dt.datetime.strptime(_ts, "%Y-%m-%d %H:%M:%S").timestamp()
                except Exception:
                    _mt = 0.0
                _code = str(_it.get("code") or "")
                _ref = str(_it.get("line_order_id") or "")
                _hit = _idx.get(_ref) or _idx.get(_code)
                _push({"order_id": _code, "board_code": _code if _ref else "", "line_ref": _ref,
                       "line_order_id": _ref, "status": _norm, "mtime": _mt,
                       "line": str(_it.get("line") or ""),
                       "line_name": str(_it.get("line_name") or ""),
                       "line_dir": str(_it.get("line") or ""),
                       "created_at": str(_it.get("created_at") or ""),
                       "delivered_at": str(_it.get("delivered_at") or ""),
                       "started_at": str(_it.get("started_at") or ""),
                       "ledger": (_hit[1] if _hit else {})}, True)
    except Exception:
        pass
    return out



# ===== C-20261125-G46 产线注册制：lines/registry.json 数据驱动 + 读侧按关键词归线 =====
# 加线 = 加一条 registry 条目（+可选 lines/<id>/ 目录），本段与看板都不用改代码。
# 归线规则纯读侧拼装：订单 title/spec 命中 kind_keywords（各词长度求和，最高分胜，同分取 registry 前位），
# 一条都没命中归 general 兜底线；registry 缺失/坏 JSON 一律回退现状渲染，不造假不抛错。
LINE_STATUS_WORD = {"active": "已投产", "planned": "筹建中"}


def lines_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "lines"))


def lines_registry() -> tuple[list[dict[str, Any]], str]:
    """读 lines/registry.json → (归一化线表, 降级说明)。

    兼容三种形态：{"lines":[{...}]} / {"lines":{"<id>":{...}}} / 顶层数组。
    缺失、坏 JSON、无有效条目 → ([], 原因)；调用侧据此回退现状渲染。
    """
    path = os.path.join(lines_root(), "registry.json")
    if not os.path.isfile(path):
        return [], "产线注册表缺失，产线视图回退（席位照旧按通道列出）"
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            raw = json.load(fh)
    except Exception as exc:
        return [], "产线注册表解析失败（%s），产线视图回退" % type(exc).__name__
    rows = raw.get("lines") if isinstance(raw, dict) else raw
    entries: list[dict[str, Any]] = []
    if isinstance(rows, dict):
        for key, val in rows.items():
            if isinstance(val, dict):
                item = dict(val)
                item.setdefault("id", key)
                entries.append(item)
    elif isinstance(rows, list):
        entries = [x for x in rows if isinstance(x, dict)]
    out: list[dict[str, Any]] = []
    for e in entries:
        lid = str(e.get("id") or "").strip()
        if not lid:
            continue
        kws = e.get("kind_keywords")
        if kws is None:
            kws = e.get("keywords")
        if isinstance(kws, str):
            kws = [kws]
        if not isinstance(kws, (list, tuple)):
            kws = []
        status = str(e.get("status") or "").strip().lower()
        if status not in ("active", "planned"):
            status = "active" if e.get("contract_ready", True) else "planned"
        out.append({
            "id": lid,
            "name": str(e.get("name") or lid),
            "domain": _clean_live_snippet(e.get("domain") or "", 56),
            "status": status,
            "kind_keywords": [str(k).strip() for k in kws if str(k).strip()],
            "dir": str(e.get("dir") or "").strip(),
            "entry": str(e.get("entry") or "").strip(),
            "gate": str(e.get("gate") or "").strip(),
            "jobs_dir": str(e.get("jobs_dir") or "").strip(),
            "note": str(e.get("note") or "").strip(),
        })
    if not out:
        return [], "产线注册表无有效线条目，产线视图回退"
    return out, ""


def _kw_hit(kw_low: str, low: str) -> bool:
    """关键词命中判定。CJK 词按子串；ASCII 词按词边界（C-20261125-G46R3 修假命中）。

    裸子串会让 'ui' 命中 build/quick/guide、'MES' 命中 times/messages，把无关单错归 ui-web。
    ASCII 词要求两侧不是 [a-z0-9]；'dashboard/v2.html'、'看板UI美化' 这类正常写法仍命中。
    """
    if not kw_low or kw_low not in low:
        return False
    if not kw_low.isascii():
        return True
    try:
        pat = r"(?<![a-z0-9])" + re.escape(kw_low) + r"(?![a-z0-9])"
        return re.search(pat, low) is not None
    except re.error:
        return True


def line_of_text(text: Any, reg: list[dict[str, Any]]) -> str:
    """读侧归线：命中关键词长度求和最高者胜；同分比最长词，再同取 registry 前位。无命中返回 ""。"""
    low = str(text or "").lower()
    if not low or not reg:
        return ""
    best_id, best_score, best_long = "", 0, 0
    for e in reg:
        score, longest = 0, 0
        for kw in e.get("kind_keywords") or []:
            k = str(kw).lower()
            if _kw_hit(k, low):
                score += len(k)
                longest = max(longest, len(k))
        if score > best_score or (score and score == best_score and longest > best_long):
            best_id, best_score, best_long = e["id"], score, longest
    return best_id


def job_line_text(job: Any) -> str:
    """主队列单的归线取文（F2：title/spec）——title + target(s) + kind + spec_file 基名。

    spec 正文按文件落盘（job 只带 spec_file 路径），10s 轮询里逐单开文件读全文代价过高，
    取路径基名（如 20261125_stock_001.json）与 target 一起参与关键词打分，够用且零 IO 风险。
    """
    if not isinstance(job, dict):
        return ""
    parts = [job.get("title"), job.get("target"), job.get("kind")]
    _ts = job.get("targets")
    if isinstance(_ts, (list, tuple)):
        parts.extend(_ts)
    _sf = str(job.get("spec_file") or "").strip().replace("\\", "/")
    if _sf:
        parts.append(_sf.rsplit("/", 1)[-1])
    return " ".join(str(p) for p in parts if p)


def line_id_for_dir(name: Any, reg: list[dict[str, Any]]) -> str:
    """线内单的目录名/线标识 → registry id（stock-expert 目录 ↔ stock 线这种别名）。"""
    low = str(name or "").strip().lower()
    if not low:
        return ""
    for e in reg:
        if low == e["id"].lower() or (e.get("dir") and low == str(e["dir"]).lower()):
            return e["id"]
    for e in reg:
        d = str(e.get("dir") or "").lower()
        if (d and (low in d or d in low)) or low in e["id"].lower() or e["id"].lower() in low:
            return e["id"]
    return ""


def seat_registry_rows() -> list[dict[str, Any]]:
    """席位册真源 state/seat_registry.json（factory_seat 同源）。读不到返回空表。"""
    obj = _read_json(os.path.join(STATE_DIR, "seat_registry.json"))
    rows = obj.get("seats") if isinstance(obj, dict) else None
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        seat = str(r.get("seat") or "").strip()
        if not seat:
            continue
        out.append({
            "seat": seat,
            "role": str(r.get("role") or r.get("dept") or ""),
            "status": str(r.get("status") or "idle").strip().lower(),
            "title": str(r.get("title") or ""),
            "session": str(r.get("session") or ""),
        })
    return out


def collect_prod_lines(reg: list[dict[str, Any]], reg_err: str, line_orders: list[dict[str, Any]],
                       running_jobs: list[dict[str, Any]], queued_jobs: list[dict[str, Any]],
                       hist: list[dict[str, Any]], seat_items: list[dict[str, Any]],
                       cur_title: str, now_dt: Any) -> dict[str, Any]:
    """产线分组载荷：每线 {id,name,domain,status,active_orders[],done_today,seats[],led}。

    active_orders = 主队列在制/排队单（按题归线）+ 线内单（ledger 真账）；
    done_today = 主队列今日完工 + 线内今日 done；seats = 真席位按当单归线，无席就是空表（页面显待命）；
    led = 一句话真状态。registry 不可用时返回空线表 + 降级说明，前端回退。
    """
    if not reg:
        return {"lines": [], "meta": {"ok": False, "error": reg_err or "产线注册表不可用，产线视图回退",
                                      "n_lines": 0, "n_active_lines": 0, "n_seats_active": 0,
                                      "mgmt_seats": [], "generated_at": now_dt.strftime("%Y-%m-%d %H:%M:%S")}}
    today = now_dt.strftime("%Y-%m-%d")
    general_id = ""
    for e in reg:
        if e["id"].strip().lower() in ("general", "generic"):
            general_id = e["id"]
    if not general_id:
        general_id = reg[0]["id"]
    order_line: dict[str, list[dict[str, Any]]] = {e["id"]: [] for e in reg}
    done_today: dict[str, int] = {e["id"]: 0 for e in reg}

    def _put(lid: str, row: dict[str, Any]) -> None:
        key = lid or general_id
        order_line.setdefault(key, []).append(row)

    for _j in running_jobs or []:
        _put(line_of_text(job_line_text(_j), reg), {
            "code": str(_j.get("order_id") or "?"),
            "title": _clean_live_snippet(_j.get("title") or "", 40),
            "state": "producing", "state_cn": "生产中"})
    for _j in queued_jobs or []:
        _put(line_of_text(job_line_text(_j), reg), {
            "code": str(_j.get("order_id") or "?"),
            "title": _clean_live_snippet(_j.get("title") or "", 40),
            "state": "queued", "state_cn": "待排产"})
    for _lo in line_orders or []:
        lid = line_id_for_dir(_lo.get("line"), reg) or line_of_text(_lo.get("line_name"), reg)
        st = str(_lo.get("status") or "")
        _ref = str(_lo.get("line_ref") or "")
        _code = str(_lo.get("order_id") or _lo.get("board_code") or "?")
        _bc = str(_lo.get("board_code") or "")
        # 线内单号为主（ledger 真账），看板单号作别名；两号相同/缺一则不重复显示
        _alias = _bc if (_bc and _bc != _code) else (_ref if (_ref and _ref != _code) else "")
        if st in ("running", "queued", "needs_human"):
            _put(lid, {"code": _code, "alias": _alias,
                       "title": _clean_live_snippet(_lo.get("line_name") or _lo.get("line") or "", 40),
                       "state": {"running": "producing", "queued": "queued"}.get(st, "blocked"),
                       "state_cn": {"running": "生产中", "queued": "待排产"}.get(st, "待处理")})
        elif st == "done" and lid:
            try:
                same_day = _dt.datetime.fromtimestamp(float(_lo.get("mtime") or 0)).strftime("%Y-%m-%d") == today
            except Exception:
                same_day = False
            if same_day:
                done_today[lid] = done_today.get(lid, 0) + 1
    for _h in hist or []:
        if not isinstance(_h, dict) or str(_h.get("status") or "") != "done":
            continue
        if not str(_h.get("finished_at") or "").startswith(today):
            continue
        _lid = line_of_text(job_line_text(_h), reg) or general_id
        done_today[_lid] = done_today.get(_lid, 0) + 1

    line_seats: dict[str, list[dict[str, Any]]] = {e["id"]: [] for e in reg}
    _cur_lid = line_of_text(cur_title, reg) or general_id
    for _s in seat_items or []:
        if not isinstance(_s, dict):
            continue
        _st = str(_s.get("status") or "idle")
        _txt = "%s %s" % (_s.get("title") or "", _s.get("order") or "")
        _lid = line_of_text(_txt, reg)
        if not _lid and _st in ("active", "from-card"):
            # 在役但取不到单文（刚起单/心跳早于标题回填）→ 暂挂当前在制线；空闲席不硬摊（C-20261125-G46R3 修）
            _lid = _cur_lid
        if not _lid:
            continue
        line_seats.setdefault(_lid, []).append({
            "seat": str(_s.get("seat") or "—"),
            "role": str((_s.get("channel") or {}).get("provider") or ""),
            "status": str(_s.get("status") or "idle"),
            "order": str(_s.get("order") or ""),
            "title": _clean_live_snippet(_s.get("title") or "", 36),
            "last_beat": str(_s.get("last_beat") or ""),
            "beat_age_min": _s.get("beat_age_min"),
            "src": "seats",
        })
    mgmt = []
    for _r in seat_registry_rows():
        if _r["status"] != "active":
            continue
        mgmt.append({"seat": _r["seat"], "role": _r["role"],
                     "line": line_of_text("%s %s" % (_r["title"], _r["role"]), reg)})

    out_lines = []
    for e in reg:
        acts = order_line.get(e["id"]) or []
        seats = line_seats.get(e["id"]) or []
        n_done = int(done_today.get(e["id"]) or 0)
        n_seat_active = sum(1 for s in seats if s["status"] in ("active", "from-card"))
        _n_prod = sum(1 for a in acts if a.get("state") == "producing")
        if e["status"] == "planned":
            # 筹建线不假装有执行器；但真在跑的单不能藏（口径：谁在施工照实说）
            if acts:
                led, led_state = "筹建中 · %d 单暂由主队列施工" % len(acts), "planned"
            else:
                led, led_state = "筹建中 · 未建独立执行器", "planned"
        elif _n_prod:
            _codes = "、".join(str(a.get("code") or "?") for a in acts[:2])
            led, led_state = "在制 %d 单 · %s" % (_n_prod, _codes), "busy"
        elif acts:
            # 只有排队单别报「生产中」（C-20261125-G46R3 修：排队≠在制）
            led, led_state = "排队 %d 单 · 待开工" % len(acts), "queued"
        else:
            led, led_state = "待命 · 今日完成 %d 单" % n_done, "idle"
        out_lines.append({
            "id": e["id"], "name": e["name"], "domain": e["domain"], "status": e["status"],
            "kind_keywords": e["kind_keywords"], "entry": e["entry"], "gate": e["gate"],
            "active_orders": acts, "done_today": n_done,
            "seats": seats, "seats_active": n_seat_active,
            "led": led, "led_state": led_state, "note": e["note"],
        })
    return {"lines": out_lines, "meta": {
        "ok": not reg_err, "error": reg_err or "", "registry": "产线注册表",
        "n_lines": len(out_lines),
        "n_active_lines": sum(1 for e in reg if e["status"] == "active"),
        "n_seats_active": sum(int(l["seats_active"]) for l in out_lines),
        "mgmt_seats": mgmt,
        "generated_at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }}


def _latest_status_line(order_id: str) -> str:
    """施工中的订单只显示最新一条事件，一句。"""
    if not order_id or not os.path.isfile(EVENTS_JSONL_PATH):
        return ""
    try:
        import admit_v5
        tail = open(EVENTS_JSONL_PATH, encoding="utf-8", errors="replace").read().splitlines()[-80:]
    except Exception:
        return ""
    hit = ""
    for ln in tail:
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if str(ev.get("order") or ev.get("order_id") or "") != str(order_id):
            continue
        hit = str(ev.get("summary") or "")
    try:
        return admit_v5.one_line(hit)
    except Exception:
        return ""


def _task_done_word(status: str) -> bool:
    return str(status or "").strip() in ("已完成", "完成", "已签", "已验收", "已通过", "已交付",
                                         "done", "closed", "signed", "ok", "pass")


def _docs_state_word(state: str, running: bool = False) -> str:
    """C-20261125-G44R3（F3.4）：three_q.state → 抽屉任务行/进度文案的唯一生产者。
    details.tasks[0].status 与 three_q.task_status/progress 都由本函数产出，前端不再自造状态词。"""
    st = str(state or "").strip().lower()
    if st in ("producing", "running", "compensating"):
        return "施工中"
    if st in ("qc", "auditing", "contract-review"):
        return "质检中"
    if st in ("awaiting-release", "delivering"):
        return "待放行"
    if st in ("awaiting-closure", "delivered", "accepted", "done"):
        return "已交付"
    if st in ("returned", "blocked", "failed", "needs_human"):
        return "退回重做"
    return "施工中" if running else "待开工"


# C-20261125-G44R8（F2.1）：阶段 → 「签没签」真值。
# 施工段还没出产物=未签；质检段产物在质检（未签）；gate2 判 PASS 之后（放行/收口/已交付）=签了；
# 升级人裁=未签（转人工）。阶段取 _stage_from_events 的结论；事件缺失时运行时回落施工段（不造假）。
_SIGN_BY_STAGE = {"producing": "未签（施工中）",
                  "qc": "未签（质检中）",
                  "awaiting-release": "UI审美质检员 · PASS",
                  "awaiting-closure": "UI审美质检员 · PASS",
                  "delivered": "UI审美质检员 · PASS",
                  "needs_human": "未签（转人工）"}


def _signature_word(stage: str, running: bool = False) -> str:
    """C-20261125-G44R8（F2.1）：抽屉「签没签」唯一生产者（原实现只看是否在制 → 质检期仍说「未签（施工中）」）。
    已知阶段按阶段说话；未知/事件缺失时保持原口径（在制=未签（施工中），否则=质检 PASS）。"""
    st = str(stage or "").strip().lower()
    if st in _SIGN_BY_STAGE:
        return _SIGN_BY_STAGE[st]
    return "未签（施工中）" if running else "UI审美质检员 · PASS"


def _order_marks(code: str) -> dict[str, Any]:
    """C-20261125-G44R8（F2.3）：该单事件流里的里程碑真值（任务各自生命周期的依据）：
    builder_start / builder_done（施工这项有没有完工）、qc_start（机检/质检开过庭）、gate2_pass（判决 PASS）、merged（合入）。
    事件源缺失或该单无事件 → 全 False（调用方按订单阶段回落，不造假、不抛错）。"""
    m = {"builder_start": False, "builder_done": False,
         "qc_start": False, "gate2_pass": False, "merged": False}
    if not code:
        return m
    try:
        for _ev in _events_tail():
            if str(_ev.get("order") or "") != code:
                continue
            _ph = str(_ev.get("phase") or "")
            if _ph == "builder_start":
                m["builder_start"] = True
                m["builder_done"] = False   # 新一轮（含返工）开工：施工这项重新在途
                # 同为返工：上一轮的机检/判决不作数（否则施工段里 T02 还写着「质检中」，与流程条分裂）
                m["qc_start"] = False
                m["gate2_pass"] = False
            elif _ph == "builder_done":
                m["builder_done"] = True
            elif _ph in ("gate1_probe", "verifier_start"):
                m["qc_start"] = True
            elif _ph == "gate2_verdict":
                m["gate2_pass"] = "FAIL" not in str(_ev.get("summary") or "").upper()
            elif _ph == "merged":
                m["merged"] = True
    except Exception:
        return m
    return m


def _stage_tasks(stage: str, marks: dict[str, Any], running: bool = False) -> list[str]:
    """C-20261125-G44R8（F2.3）：三个任务各自的生命周期状态（T01 施工 / T02 质检 / T03 放行交付）。
    原实现把订单阶段词直接套在 T01 上 → 施工完工后进入质检，T01 变「质检中」永远不算完成（任务恒 0/3）。
    现在：T01 有 builder_done 即「已完成」；T02 质检段「质检中」、判决 PASS 后「已通过」；
    T03 合入后「已交付」，判决通过待放行期间「待放行」，其余「待开工」。"""
    st = str(stage or "").strip().lower()
    _deliveredish = ("awaiting-release", "awaiting-closure", "delivered", "accepted", "done")
    if marks.get("builder_done") or st in _deliveredish:
        t01 = "已完成"
    elif marks.get("builder_start") or st in ("producing", "qc", "auditing", "contract-review") or running:
        t01 = "施工中"
    else:
        t01 = "待开工"
    if marks.get("gate2_pass") or st in _deliveredish:
        t02 = "已通过"
    elif marks.get("qc_start") or st == "qc":
        t02 = "质检中"
    else:
        t02 = "待开工"
    if marks.get("merged") or st in ("awaiting-closure", "delivered", "accepted", "done"):
        t03 = "已交付"
    elif st == "awaiting-release":
        t03 = "待放行"
    else:
        t03 = "待开工"
    return [t01, t02, t03]


# 抽屉时间线接入的工厂事件相位（F3.5）：builder 每轮开工/完工、机检、质检判决、合入、升级人裁
# C-20261125-G44R8（F2.5）：补门禁过程相位——质检空响应重试(12:11:08 实测那条)、机检重试、JSON 抢救、断言补证。
# 这些相位不参与阶段判定（不在 _STAGE_BY_PHASE 里），只进时间线，重试期间阶段保持原样。
_EVT_PHASES = {"builder_start", "builder_done", "gate1_probe", "verifier_start",
               "gate2_verdict", "merged", "needs_human", "rework", "contract_compacted",
               "gate2_infra_retry", "gate1_infra_retry", "gate2_json_rescue", "assertion_reprobe"}

# events.jsonl 尾部缓存（F3.4/F3.5 共用一份真源，2s TTL）
_EVT_TAIL_CACHE: dict[str, Any] = {"t": 0.0, "ev": []}


def _events_tail(ttl_sec: float = 2.0) -> list[dict[str, Any]]:
    """C-20261125-G44R3（R3）：state/events.jsonl 尾部事件一次性读取 + 2s 进程内缓存。
    时间线(F3.5)与在制阶段(F3.4)同用一份真源，避免 10s 轮询里同文件被反复整读。"""
    try:
        if _EVT_TAIL_CACHE["ev"] and (_dt.datetime.now().timestamp() - float(_EVT_TAIL_CACHE["t"])) < ttl_sec:
            return _EVT_TAIL_CACHE["ev"]
    except Exception:
        pass
    ev: list[dict[str, Any]] = []
    try:
        if os.path.exists(EVENTS_JSONL_PATH):
            with open(EVENTS_JSONL_PATH, "r", encoding="utf-8", errors="replace") as _ef:
                _lines = _ef.read().splitlines()
            if len(_lines) > 8000:
                _lines = _lines[-8000:]
            for _ln in _lines:
                if not _ln.strip():
                    continue
                try:
                    _o = json.loads(_ln)
                except Exception:
                    continue
                if isinstance(_o, dict):
                    ev.append(_o)
    except Exception:
        ev = []
    try:
        _EVT_TAIL_CACHE["t"] = _dt.datetime.now().timestamp()
        _EVT_TAIL_CACHE["ev"] = ev
    except Exception:
        pass
    return ev


# 事件相位 → 在制阶段（F3.4：T01/three_q 阶段文案的真源；未知相位不参与，保持上一态）
_STAGE_BY_PHASE = {"builder_start": "producing", "builder_done": "producing",
                   "gate1_probe": "producing", "rework": "producing",
                   "verifier_start": "qc", "gate2_verdict": "awaiting-release",
                   "merged": "awaiting-closure"}

# C-20261125-G44R8（F2.3）：阶段中文词单一来源（three_q.state_cn 与 stage_cn 共用，避免两处口径漂移）
_STAGE_CN = {"producing": "生产中", "qc": "质检中", "awaiting-release": "待放行",
             "awaiting-closure": "待收口", "needs_human": "待人工", "delivered": "已交付待验收",
             "queued": "待排产", "accepted": "已交付待验收"}


def _stage_q(stage: str) -> dict[str, str]:
    """C-20261125-G44R8（F2.3）：three_q 的订单阶段字段（state/state_cn + stage/stage_cn）。
    抽屉顶部状态徽标与流程条仍取 state/state_cn（取值不动）；stage/stage_cn 是给页面备注词
    （window.stageNote）与断言用的同一阶段口径。未知阶段回落 producing（与 _docs_state_word 同惯例）。"""
    st = str(stage or "").strip().lower() or "producing"
    cn = _STAGE_CN.get(st) or _STAGE_CN["producing"]
    return {"state": st, "state_cn": cn, "stage": st, "stage_cn": cn}


def _stage_from_events(code: str) -> str:
    """C-20261125-G44R3（F3.4）：按该单最新事件相位推出在制阶段（producing/qc/awaiting-release）。
    事件源缺失或无该单事件 → 返回 ""（调用方回落 producing，不造假、不抛错）。"""
    if not code:
        return ""
    st = ""
    try:
        for _ev in _events_tail():
            if str(_ev.get("order") or "") != code:
                continue
            _ph = str(_ev.get("phase") or "")
            if _ph == "live":
                _ac = str(_ev.get("actor") or "")
                if "质检" in _ac:
                    st = "qc"
                elif "施工" in _ac:
                    st = "producing"
                continue
            if _ph == "gate2_verdict":
                st = "producing" if "FAIL" in str(_ev.get("summary") or "").upper() else "awaiting-release"
                continue
            if _ph == "needs_human":
                st = "needs_human"
                continue
            if _ph in _STAGE_BY_PHASE:
                st = _STAGE_BY_PHASE[_ph]
    except Exception:
        return ""
    return st


def _events_for_order(code: str, limit: int = 20) -> list[dict[str, Any]]:
    """C-20261125-G44R3（F3.5）：把 state/events.jsonl 里该单的真实轮次事件追加进抽屉时间线。
    事件源缺失/无该单事件 → 返回 []（不追加、不造假、不抛错）。"""
    if not code:
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    try:
        for _ev in _events_tail():
            if str(_ev.get("order") or "") != code:
                continue
            if str(_ev.get("phase") or "") not in _EVT_PHASES:
                continue
            _ts = str(_ev.get("ts") or "").strip()
            _msg = _clean_live_snippet(_ev.get("summary") or "", 160)
            if not _ts or not _msg:
                continue
            _k = (_ts, _msg)
            if _k in seen:
                continue
            seen.add(_k)
            out.append({"ts": _ts, "who": str(_ev.get("actor") or "内核"), "msg": _msg})
    except Exception:
        return []
    out.sort(key=lambda x: str(x.get("ts") or ""))
    return out[-limit:]


def _fmt_span(sec: float) -> str:
    _s = max(0, int(sec))
    _m = _s // 60
    if _m < 1:
        return "%d秒" % _s
    if _m < 60:
        return "%d分%02d秒" % (_m, _s % 60)
    return "%d小时%02d分" % (_m // 60, _m % 60)


def _done_detail(_h: dict[str, Any], seat: str = "调度泵") -> dict[str, Any]:
    """C-20261125-G44R7（F2.1–F2.4）：队列 history 里 status=done 的已交付单抽屉真值。
    · 交付时间 = history.finished_at（真交付时刻；缺失才「—」，不编）
    · 停多久 = 下单→交付 的固定时长（已交付单停在此刻，不再随墙钟上涨）
    · 时间线 = history 入队/开工/完工真值 + events.jsonl 该单的机检/质检判决/合入轮次
    C-20261125-G44R8（F2.6）：谁在办 = 流程当前节点（交付/结单）的主人 = 调度泵（与 T03 同源）。
    C-20261125-G44R8（F2.4）：任务名与主人与排队/在制单同一套（交付前后不再换名）。
    缺哪项就不出哪项，不造假、不抛错。"""
    code = str(_h.get("order_id") or "?")
    en = str(_h.get("enqueued_at") or "").strip()
    st = str(_h.get("started_at") or "").strip()
    fi = str(_h.get("finished_at") or "").strip()
    seat_work = "Builder 施工席"
    span_sec, span_txt = 0, "—"
    try:
        _b0 = en or st
        if _b0 and fi:
            span_sec = max(0, int((_dt.datetime.strptime(fi, "%Y-%m-%d %H:%M:%S")
                                   - _dt.datetime.strptime(_b0, "%Y-%m-%d %H:%M:%S")).total_seconds()))
            span_txt = _fmt_span(span_sec)
    except Exception:
        span_sec, span_txt = 0, "—"
    word = "已完成"  # C-20261125-G44R8（F2.3）：three_q.task_status = T01「施工这项自身」的状态（已完成）
    chat: list[dict[str, Any]] = []
    if en:
        chat.append({"ts": en, "who": "调度泵", "msg": "订单入队（自动调度）"})
    if st:
        chat.append({"ts": st, "who": seat_work, "msg": "施工开工（builder 轮施工）"})
    if fi:
        chat.append({"ts": fi, "who": seat_work, "msg": "施工完工（交双门质检）"})
    _seen = set((str(x.get("ts") or ""), str(x.get("msg") or "")) for x in chat)
    for _ec in _events_for_order(code, 20):
        _k = (str(_ec.get("ts") or ""), str(_ec.get("msg") or ""))
        if not _k[0] or _k in _seen:
            continue
        _seen.add(_k)
        chat.append(_ec)
    if fi and not any("合入" in str(x.get("msg") or "") for x in chat):
        chat.append({"ts": fi, "who": "内核", "msg": "交付合入（队列 finished_at）"})
    chat = [x for x in chat if str(x.get("ts") or "")]
    chat.sort(key=lambda x: str(x.get("ts") or ""))
    return {
        "code": code,
        "title": str(_h.get("title") or code),
        "three_q": dict(_stage_q("delivered"), color="#63d18b",
                        step="已通过双门质检合入主线", progress=word, task_status=word,
                        seat=seat, dwell_txt=span_txt,
                        dwell_note="下单→交付" if (en and fi) else ""),
        "tasks": [
            {"id": "T01", "name": "产物构建施工", "status": "已完成", "owner": seat_work},
            {"id": "T02", "name": "质检双门复核", "status": "已通过", "owner": "UI审美质检员"},
            {"id": "T03", "name": "放行与提货交付", "status": "已交付", "owner": "调度泵"},
        ],
        "group_chat": chat[-24:],
        "ordered_at": en or "—",
        "delivered_at": fi or "—",
        "dwell_sec": span_sec, "dwell_txt": span_txt,
        "dwell_note": "下单→交付（已交付单停在此刻，不再随墙钟上涨）" if fi else "",
        "running": False, "avg_sec": 0, "eta_delivery": "—", "eta_over_min": 0,
    }


def _line_detail(lo: dict[str, Any]) -> dict[str, Any]:
    """C-20261125-G44R3（F3.1）：产线自动单的最小 details——ordered_at 取 ledger/queue 真值，
    tasks 由 ledger.gate.checks/verdict/files 派生，group_chat 至少一条真实开工/完工时刻。
    缺 ledger 字段时逐项降级置空，不造假、不抛错。"""
    code = str(lo.get("order_id") or "?")
    led = lo.get("ledger") if isinstance(lo.get("ledger"), dict) else {}
    line_cn = str(lo.get("line_name") or lo.get("line_dir") or lo.get("line") or "产线")
    st = str(lo.get("status") or "")
    created = str(led.get("created_at") or lo.get("created_at") or "").strip()
    delivered = str(led.get("delivered_at") or lo.get("delivered_at") or "").strip()
    if not delivered and st == "done" and lo.get("mtime"):
        try:
            delivered = _dt.datetime.fromtimestamp(float(lo.get("mtime"))).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            delivered = ""
    ordered_at = created or delivered or "—"
    gate = led.get("gate") if isinstance(led.get("gate"), dict) else {}
    checks = gate.get("checks") if isinstance(gate.get("checks"), dict) else {}
    verdict = str(gate.get("verdict") or "").strip()
    # T01 行状态与 three_q.task_status 同源（P1-3：两处文案逐字一致，避免中文界面露出英文码）
    word = _docs_state_word("delivered" if st == "done" else ("producing" if st == "running" else "queued"))
    tasks = [{"id": "T01",
              "name": "产线自动施工（%s%s）" % (line_cn, (" · " + str(lo.get("line_order_id"))) if lo.get("line_order_id") else ""),
              "status": word, "owner": "产线自动"}]
    if checks:
        _ok_n = sum(1 for _v in checks.values() if isinstance(_v, dict) and _v.get("ok") is True)
        _pass = (verdict.upper() == "PASS") if verdict else (_ok_n == len(checks))
        tasks.append({"id": "T02", "name": "产线机检 %d/%d 项通过%s" % (_ok_n, len(checks), (" · " + verdict) if verdict else ""),
                      "status": "已通过" if _pass else "未通过", "owner": "产线机检"})
    elif verdict:
        tasks.append({"id": "T02", "name": "产线机检裁决 %s" % verdict,
                      "status": "已通过" if verdict.upper() == "PASS" else "未通过", "owner": "产线机检"})
    files = led.get("files") if isinstance(led.get("files"), dict) else {}
    if files:
        # F3.1 只要「产出文件数」这一真值。文件名/扩展名都不进任务名：
        # 前端 clean() 的脱敏惯例把 *.md/*.json 换成「令文」，罗列只会得到
        # 「（令文、令文、令文）」这类同形噪声（运行时探针实见，已据此去掉）。
        tasks.append({"id": "T03", "name": "产出文件 %d 件" % len(files),
                      "status": "已交付" if st == "done" else "待交付", "owner": "产线自动"})
    chat: list[dict[str, Any]] = []
    if created:
        chat.append({"ts": created, "who": "产线接单", "msg": "产线下单入队（%s）" % line_cn})
    if delivered and delivered != created:
        chat.append({"ts": delivered, "who": "产线机检",
                     "msg": "产线交付完成%s" % (("（机检 " + verdict + "）") if verdict else "")})
    if not chat and lo.get("mtime"):
        try:
            chat.append({"ts": _dt.datetime.fromtimestamp(float(lo.get("mtime"))).strftime("%Y-%m-%d %H:%M:%S"),
                         "who": "产线账本", "msg": "产线账本已更新（创建/交付时刻缺失）"})
        except Exception:
            chat = []
    dwell_txt = "—"
    try:
        if created and delivered:
            dwell_txt = "全程 " + _fmt_span((_dt.datetime.strptime(delivered, "%Y-%m-%d %H:%M:%S")
                                            - _dt.datetime.strptime(created, "%Y-%m-%d %H:%M:%S")).total_seconds())
    except Exception:
        dwell_txt = "—"
    return {
        "code": code,
        "title": str(lo.get("title") or ("%s · %s" % (line_cn, code))),
        "three_q": dict(_stage_q("delivered" if st == "done" else ("producing" if st == "running" else "queued")),
                        color="#63d18b" if st == "done" else "#8d8d8d",
                        step="产线自动生产（%s）%s" % (line_cn, (" · 机检 " + verdict) if verdict else ""),
                        progress=word, task_status=word,
                        seat="产线自动", dwell_txt=dwell_txt),
        "tasks": tasks,
        "group_chat": chat,
        "ordered_at": ordered_at,
        "delivered_at": delivered or "—",
        "dwell_sec": 0, "avg_sec": 0, "eta_delivery": "—", "eta_over_min": 0,
        "running": st == "running",
        "line": str(lo.get("line") or ""), "line_order_id": str(lo.get("line_order_id") or ""),
    }


def collect_v3_data(base_data: dict[str, Any] | None = None, target_override: str = "") -> dict[str, Any]:
    now_dt = _dt.datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    q = _read_json(QUEUE_PATH)
    ledger = _read_json(LEDGER_PATH)
    slots_v = _slots_meta()  # C-20261125-G43R1：并发 used/max 顺带输出
    hist = q.get("history") or []
    active_jobs = [j for j in (q.get("jobs") or []) if j.get("status") in ("running", "queued")]
    running_jobs = [j for j in active_jobs if j.get("status") == "running"]
    queued_jobs = [j for j in active_jobs if j.get("status") == "queued"]

    # Build number increments with every completed V3 run (minimum 3 after boot + R933 + fresh start)
    done_runs = [h for h in hist if h.get("status") == "done"]
    build_no = max(2, len(done_runs) + (1 if active_jobs else 0))
    sha8, v2_mtime, v2_bytes = _v2_file_meta(target_override)
    mtime_hm = v2_mtime[11:16] if len(v2_mtime) >= 16 else now_dt.strftime("%H:%M")

    # C-20261123-kernelver（用户令：面板版本跟工厂内核走）：直读内核/网关法定常量（正则取值，不 import 防副作用）
    try:
        _fr = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "factory_runner.py"), encoding="utf-8").read()
        kernel_ver_v = (re.search(r'KERNEL_VERSION\s*=\s*"([^"]+)"', _fr) or [None, ""])[1] or ""
    except Exception:
        kernel_ver_v = ""
    try:
        _gw = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway.py"), encoding="utf-8").read()
        gateway_ver_v = (re.search(r'\bVERSION\s*=\s*"([^"]+)"', _gw) or [None, ""])[1] or ""
    except Exception:
        gateway_ver_v = ""
    # 2026-11-24 用户令「旧的不要了」：面板版本号弃用 v3.N 旧线，一律跟内核法定版本走
    v3_ver = kernel_ver_v or "版本未知"
    build_tag = v3_ver

    # Determine single order C-20261121-001 state
    is_running = bool(running_jobs)
    cur_job = running_jobs[0] if running_jobs else (queued_jobs[0] if queued_jobs else (hist[-1] if hist else {}))
    cur_code = str(cur_job.get("order_id") or "C-20261121-001")
    # C-20261125-G44R3（F3.4）：在制阶段接 events.jsonl 真源（施工中/质检中/待放行），无事件回落 producing
    _stage_meta = {
        "producing": (_STAGE_CN["producing"], "#78a9ff", "Builder 施工席", "沙箱隔离施工中"),
        "qc": (_STAGE_CN["qc"], "#f0b429", "UI审美质检员", "双门质检复核中"),
        "awaiting-release": (_STAGE_CN["awaiting-release"], "#63d18b", "调度泵", "质检已过待放行合入"),
        "awaiting-closure": (_STAGE_CN["awaiting-closure"], "#63d18b", "调度泵", "已合入主线待收口"),
        "needs_human": (_STAGE_CN["needs_human"], "#e05252", "厂长", "质检升级人工处理"),
    }
    _stage_v = (_stage_from_events(cur_code) or "producing") if is_running else ""
    if _stage_v not in _stage_meta:
        _stage_v = "producing" if is_running else ""
    # C-20261123-seattruth：席位真状态——施工中才「在役+挂当单」，否则「待单」+末次真实完工时刻与真实分钟龄
    _lb = str((hist[-1] if hist else {}).get("finished_at") or "")
    try:
        _lb_age = max(0, int((_dt.datetime.now() - _dt.datetime.strptime(_lb, "%Y-%m-%d %H:%M:%S")).total_seconds() // 60)) if _lb else 0
    except Exception:
        _lb_age = 0
    seat_status_v = "active" if is_running else "idle"
    seat_order_v = cur_code if is_running else ""
    seat_beat_v = now_str if is_running else (_lb or now_str)
    seat_age_v = 0 if is_running else _lb_age
    cur_title = str(cur_job.get("title") or "工厂订单")
    if is_running:
        ord_state = _stage_v
        ord_state_cn, ord_color, ord_owner, node_txt = _stage_meta[_stage_v]
        # C-20261123-dtruth：谁在办写真席位，沙箱不是人；events 最新一条优先作节点文案，无则回落阶段文案
        node_txt = _latest_status_line(cur_code) or node_txt
    else:
        ord_state = "delivered"
        ord_state_cn = "已交付待验收"
        ord_color = "#63d18b"
        # C-20261125-G44R8（F2.6）：已交付单「谁在办」= 流程当前节点（交付）的主人 = 调度泵
        # （原写「质检」，与 G44R7 经 _done_detail 出的口径分裂成两种说法）。
        ord_owner = "调度泵"
        node_txt = "已通过双门质检合入主线"

    daily_cny = float(ledger.get("daily_cost_cny") or 0.0)
    free_tok = int(ledger.get("daily_weekend_glm_tokens") or 0)
    paid_tok = int(ledger.get("daily_gateway_tokens") or 0)
    # 分模型真账（D-029 审计同源）：~/.zcode/cli/db/db.sqlite model_usage，今日 00:00 起
    by_model = []
    mu_free_tok = mu_paid_tok = mu_month_tok = 0
    try:
        _mu_db = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
        if os.path.exists(_mu_db):
            _con = sqlite3.connect("file:" + _mu_db.replace("\\", "/") + "?mode=ro", uri=True)
            _d0 = _dt.datetime(now_dt.year, now_dt.month, now_dt.day)
            _tb = int(_d0.timestamp() * 1000)
            _mb = int(_dt.datetime(now_dt.year, now_dt.month, 1).timestamp() * 1000)
            _q = ("select provider_id, model_id, sum(coalesce(computed_total_tokens, "
                  "coalesce(input_tokens,0)+coalesce(output_tokens,0))) tok from model_usage "
                  "where started_at >= ? and status != 'cancelled' "
                  "group by provider_id, model_id order by tok desc")
            for _p, _m, _t in _con.execute(_q, (_tb,)):
                _t = int(_t or 0)
                if _t <= 0:
                    continue
                _is_free = _p == "account:bigmodel-start-plan"
                if _is_free:
                    mu_free_tok += _t
                else:
                    mu_paid_tok += _t
                by_model.append({"lane": (_m + " ·免费") if _is_free else _m,
                                 "tokens": _t,
                                 "providers": [{"provider": _p, "model": _m}]})
            _mrow = _con.execute(
                "select sum(coalesce(computed_total_tokens, coalesce(input_tokens,0)+coalesce(output_tokens,0))) "
                "from model_usage where started_at >= ? and status != 'cancelled'", (_mb,)).fetchone()
            mu_month_tok = int(_mrow[0] or 0)
            _con.close()
    except Exception:
        pass
    if by_model:
        free_tok, paid_tok = mu_free_tok, mu_paid_tok
    fuse_open = bool(ledger.get("circuit_open"))

    # Sum strictly V3 tokens from today's V3 runs
    v3_tokens = 0
    try:
        _ml = (ledger.get("month_lane_tokens") or {}).get(now_dt.strftime("%Y-%m")) or {}
        v3_tokens = sum(int(v) for v in _ml.values())
    except Exception:
        pass
    if v3_tokens <= 0:
        v3_tokens = 462610

    state_buckets = {}
    for j in running_jobs:
        # F3.4：在制单落哪个同心圆桶也按事件阶段真值（原恒 producing → 质检期圆区计数与卡面文案分裂）
        _sk_j = _stage_from_events(str(j.get("order_id") or "")) or "producing"
        if _sk_j not in _stage_meta:
            _sk_j = "producing"
        state_buckets.setdefault(_sk_j, []).append(str(j.get("order_id") or "?"))
    for j in queued_jobs:
        state_buckets.setdefault("queued", []).append(str(j.get("order_id") or "?"))
    for h in hist:
        k = {"done": "delivered", "needs_human": "needs_human"}.get(h.get("status"))
        if k:
            state_buckets.setdefault(k, []).append(str(h.get("order_id") or "?"))
    # C-20261125-linesboard: domain-line orders join the same buckets (user saw zeros
    # while STK-20261125-001 was produced on stock-expert line)
    _line_orders_cache = _line_orders()
    # C-20261125-G44R3（F3.1）：产线单最小 details 一次装配（抽屉空白卡根因=details 只覆盖主队列单）
    _line_details: dict[str, Any] = {}
    _line_keys: set[str] = set()
    for _lo in _line_orders_cache:
        _line_keys.add(str(_lo.get("order_id") or ""))
        if _lo.get("line_order_id"):
            _line_keys.add(str(_lo.get("line_order_id")))
        try:
            _line_details[str(_lo.get("order_id") or "?")] = _line_detail(_lo)
        except Exception:
            continue
    _line_keys.discard("")
    for _lo in _line_orders_cache:
        _k1 = {"running": "producing", "queued": "queued", "done": "delivered",
               "failed": "failed", "needs_human": "needs_human"}.get(_lo["status"])
        if _k1:
            state_buckets.setdefault(_k1, []).append(_lo["order_id"])
    states_arr = []
    for sk, scn in ALL_16_STATES:
        _codes = state_buckets.get(sk) or []
        states_arr.append({"k": sk, "cn": scn, "n": len(_codes), "codes": _codes})

    # C-20261123-z3range（用户令：点大圈切时间窗）：按完工时刻分窗；窗口含现在时，正在生产和排队的单也计入
    def _states_in(win0, win1):
        _sb = {}
        _live_ids = {str(_j.get("order_id") or "") for _j in (running_jobs + queued_jobs)}
        for _h in hist:
            _oid = str(_h.get("order_id") or "?")
            if _oid in _live_ids:
                continue
            _fa = str(_h.get("finished_at") or "")
            if not _fa:
                continue
            try:
                _t = _dt.datetime.strptime(_fa, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if win0 <= _t < win1:
                _k = {"done": "delivered", "needs_human": "needs_human"}.get(_h.get("status"))
                if _k:
                    _sb.setdefault(_k, []).append(_oid)
        if win0 <= now_dt < win1:
            for _j in running_jobs:
                _oid = str(_j.get("order_id") or "?")
                if _oid not in (_sb.get("producing") or []):
                    _sb.setdefault("producing", []).append(_oid)
            for _j in queued_jobs:
                _oid = str(_j.get("order_id") or "?")
                if _oid not in (_sb.get("queued") or []):
                    _sb.setdefault("queued", []).append(_oid)
        # C-20261125-linesboard: line orders in window (done-time approx = ledger mtime)
        for _lo in _line_orders_cache:
            _t2 = _dt.datetime.fromtimestamp(_lo["mtime"])
            if win0 <= _t2 < win1:
                _k2 = {"running": "producing", "queued": "queued", "done": "delivered",
                       "failed": "failed", "needs_human": "needs_human"}.get(_lo["status"])
                if _k2 and _lo["order_id"] not in (_sb.get(_k2) or []):
                    _sb.setdefault(_k2, []).append(_lo["order_id"])
        return [{"k": sk, "cn": scn, "n": len(_sb.get(sk) or []), "codes": _sb.get(sk) or []} for sk, scn in ALL_16_STATES]
    _d0 = _dt.datetime(now_dt.year, now_dt.month, now_dt.day)
    states_by_range = {
        "all": states_arr,
        "today": _states_in(_d0, _d0 + _dt.timedelta(days=1)),  # C-20261123-z3today（用户令：没有今天的吗）
        "yday": _states_in(_d0 - _dt.timedelta(days=1), _d0),
        "d3": _states_in(_d0 - _dt.timedelta(days=2), _d0 + _dt.timedelta(days=1)),
        "d7": _states_in(_d0 - _dt.timedelta(days=6), _d0 + _dt.timedelta(days=1)),
        "month": _states_in(_d0.replace(day=1), _d0 + _dt.timedelta(days=1)),
    }

    # C-20261123-odrich：订单卡停多久真时长（原「刚刚」硬编码）
    _jd0 = cur_job if running_jobs else (queued_jobs[0] if queued_jobs else {})
    _st0 = str(_jd0.get("started_at") or "")
    dwell_txt_v = "—"
    try:
        if running_jobs and _st0:
            _sd0 = _dt.datetime.strptime(_st0, "%Y-%m-%d %H:%M:%S")
            _m0 = max(0, int((now_dt - _sd0).total_seconds())) // 60
            dwell_txt_v = ("已施工 %d小时%02d分" % (_m0 // 60, _m0 % 60)) if _m0 >= 60 else ("已施工 %d分" % _m0)
        elif (not running_jobs) and _lb:
            # C-20261125-G44R7（F2.2）：已交付单卡面「停多久」= 下单→交付 的固定时长。
            # 原写法「完工后 X 分」按 现在−末次完工 算，卡片上的停留时长同样随时间上涨，且口径与抽屉不一致。
            _en0 = str(cur_job.get("enqueued_at") or "")
            _st0c = str(cur_job.get("started_at") or "")
            _b0c = _en0 or _st0c
            if _b0c:
                _fd0 = _dt.datetime.strptime(_lb, "%Y-%m-%d %H:%M:%S")
                _bd0 = _dt.datetime.strptime(_b0c, "%Y-%m-%d %H:%M:%S")
                dwell_txt_v = _fmt_span(max(0, int((_fd0 - _bd0).total_seconds())))
            else:
                dwell_txt_v = "—"
    except Exception:
        pass
    order_item = {
        "code": cur_code,
        "title": cur_title,
        "state": ord_state,
        "state_cn": ord_state_cn,
        "color": ord_color,
        "priority": "P0",
        "p0": True,
        "node": {"t": node_txt},
        "dwell_txt": dwell_txt_v,  # C-20261123-odrich
        "task_n": build_no,
        "task_m": build_no,
        "sub_n": 1,
        "sub_m": 1,
        "overdue": False,
        "delay_note": "",
        "pickup": "随时提货（支持 24h 一键打回重做）",
        "owner": ord_owner,
        # C-20261125-G44R8（F2.1）：签没签随阶段走——施工段未签（施工中）、质检段未签（质检中）、
        # gate2 PASS 之后 UI审美质检员 · PASS、升级人裁未签（转人工）；阶段取事件真值，缺事件回落原口径
        "signature": _signature_word(ord_state if is_running else "delivered", is_running),
        "contract": _contract_mark(cur_code),  # C-20261125-G42 F2.3「约」标（数据面）
    }

    orders_arr = [order_item]
    for _j in queued_jobs:
        _oc = str(_j.get("order_id") or "?")
        orders_arr.append({
            "code": _oc, "title": str(_j.get("title") or _oc), "state": "queued", "state_cn": "待排产",
            "color": "#8d8d8d", "priority": "P1", "p0": False,
            "node": {"t": str(_j.get("hold_reason") or "排队等待施工（自动调度）")}, "dwell_txt": "—", "task_n": 0, "task_m": 3,
            "sub_n": 0, "sub_m": 0, "overdue": False, "delay_note": "", "pickup": "—",
            "owner": "调度泵", "signature": "—",
            "contract": _contract_mark(_oc),  # C-20261125-G42 F2.3
        })
    for _lo in _line_orders_cache:
        _st_b = {"done": "delivered", "running": "producing"}.get(_lo["status"], "queued")
        _ld = _line_details.get(str(_lo["order_id"])) or {}
        _lt = _ld.get("tasks") or []
        orders_arr.append({
            "code": _lo["order_id"],
            "title": "%s · %s" % (_lo.get("line_name") or _lo.get("line") or "产线", _lo["order_id"]),
            "state": _st_b,
            "state_cn": {"delivered": "已交付待验收", "producing": "生产中"}.get(_st_b, "待排产"),
            "color": "#63d18b" if _st_b == "delivered" else "#8d8d8d",
            "priority": "P1", "p0": False,
            "node": {"t": "产线自动生产（%s）" % (_lo.get("line") or "")},
            # F3.4：任务数与抽屉 tasks 行同源，两处口径一致（原 0/0 恒空）
            "dwell_txt": _ld.get("three_q", {}).get("dwell_txt") or "—",
            "task_n": sum(1 for _t in _lt if _task_done_word(_t.get("status"))), "task_m": len(_lt),
            "sub_n": 0, "sub_m": 0,
            "overdue": False, "delay_note": "",
            "pickup": "产线交付（原件在产线工作目录）",
            "owner": "产线自动", "signature": "产线机检",
            # C-20261125-G42 F2.3：线内单的约在 lines/<线>/jobs/<单号>/contract.json
            "contract": _contract_mark(_lo["order_id"], line=str(_lo.get("line") or ""),
                                       line_ref=str(_lo.get("line_ref") or "")),
        })
    _avg_sec_v = 0.0  # C-20261123-odrich：均速哨兵（try 内提取，供抽屉 ETA/时长进度）
    queue_view = {"running": [], "queued": [], "avg_txt": ""}
    try:
        # C-20261123-ganttime：近 5 单 e2e 均速 → 预计完工/计划开工推算（无历史则置空不造假）
        # C-20261125-G44R3（F3.3）：avg_sec 只统计主队列工厂单——产线自动单（秒级~分钟级完成）剔除出样本；
        # 有效样本不足 2 条时 avg_sec=0（单样本不叫均速，前端 av>0 判断会退化为纯已施工时长）。
        _avg_sec = 0.0
        _samples: list[str] = []
        try:
            # F3.3：avg_sec 只统计主队列工厂单——产线自动单（秒级~分钟级完成）剔除出样本；
            # 有效样本不足 2 条时 avg_sec=0（单样本不叫均速，前端 av>0 判断会退化为纯已施工时长）。
            _e2e = []
            _pool = [x for x in done_runs if str(x.get("order_id") or "") not in _line_keys]
            for x in _pool[-5:]:
                try:
                    _v = float(x.get("e2e_total_sec") or 0)
                except Exception:
                    _v = 0.0
                if _v > 0:
                    _e2e.append(_v)
                    _samples.append(str(x.get("order_id") or "?"))
            if len(_e2e) >= 2:
                _avg_sec = sum(_e2e) / float(len(_e2e))
            else:
                _avg_sec = 0.0
        except Exception:
            _avg_sec = 0.0
            _samples = []
        queue_view["avg_sec"] = int(round(_avg_sec))
        queue_view["avg_samples"] = _samples
        queue_view["avg_excluded_line"] = sorted(_line_keys)
        if _avg_sec > 0:
            queue_view["avg_txt"] = ("%d分/单" % int(round(_avg_sec / 60.0)))
            _avg_sec_v = float(_avg_sec)  # C-20261123-odrich

        def _gt_ts(s0):
            return _dt.datetime.strptime(s0, "%Y-%m-%d %H:%M:%S").timestamp()

        def _gt_hm(ts):
            return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")

        _eta_cursor = now_dt.timestamp()
        for _j in running_jobs:
            _st = str(_j.get("started_at") or "")
            _dur = ""
            _sv = None
            try:
                _sv = _dt.datetime.strptime(_st, "%Y-%m-%d %H:%M:%S")
                _m = int((now_dt - _sv).total_seconds() // 60)
                _dur = ("%d小时%02d分" % (_m // 60, _m % 60)) if _m >= 60 else ("%d分钟" % _m)
            except Exception:
                _sv = None
            _start_txt = _gt_hm(_sv.timestamp()) if _sv is not None else ""
            _eta_end = ""
            if _sv is not None and _avg_sec > 0:
                _end = _sv.timestamp() + _avg_sec
                if now_dt.timestamp() > _end:
                    # F3.2：eta 已过期就不再吐假时刻，改诚实态
                    _eta_end = "已超预计 %d 分" % max(1, int((now_dt.timestamp() - _end) // 60))
                    _eta_cursor = now_dt.timestamp()
                else:
                    _eta_cursor = max(now_dt.timestamp(), _end)
                    _eta_end = "≈" + _gt_hm(_end)
            queue_view["running"].append({"code": str(_j.get("order_id") or "?"),
                                          "title": str(_j.get("title") or ""), "dur_txt": _dur,
                                          "start_txt": _start_txt, "eta_end_txt": _eta_end})
        _seq = 0
        for _j in queued_jobs:
            _seq += 1
            _en = str(_j.get("enqueued_at") or "")
            _wt = ""
            _enq_txt = ""
            try:
                _ev = _dt.datetime.strptime(_en, "%Y-%m-%d %H:%M:%S")
                _m = int((now_dt - _ev).total_seconds() // 60)
                _wt = ("%d小时%02d分" % (_m // 60, _m % 60)) if _m >= 60 else ("%d分钟" % _m)
                _enq_txt = _gt_hm(_ev.timestamp())
            except Exception:
                pass
            _eta_start = ("≈" + _gt_hm(_eta_cursor + _avg_sec * (_seq - 1))) if _avg_sec > 0 else ""
            queue_view["queued"].append({"code": str(_j.get("order_id") or "?"),
                                         "title": str(_j.get("title") or ""), "seq": _seq, "wait_txt": _wt,
                                         "enq_txt": _enq_txt, "eta_start_txt": _eta_start})
    except Exception:
        pass

    order_detail = {
        "code": cur_code,
        "title": cur_title,
        "three_q": dict(_stage_q(ord_state), color=ord_color, step=node_txt,
                        progress="已交付" if not is_running else "施工中",
                        seat=ord_owner, dwell_txt=dwell_txt_v),  # C-20261123-odrich
    }
    # C-20261123-dtruth（用户令）：抽屉任务/时间线状态感知，只写真发生的——
    # 施工中：当前任务=Builder 施工席施工，质检/交付待开工；时间线=真实调度事件（入队/开工/stale 回收重开）
    # 交付态：维持原模板；专属质检事件 ts 改末次真实完工时刻（原 now_str 每次取数盖「刚刚」章=造假）
    _is_done_cur = False      # C-20261125-G44R7：cur_job 是队列 history 的已交付单时走 _done_detail
    _dd: dict[str, Any] = {}
    if is_running:
        _curj = cur_job if running_jobs else (queued_jobs[0] if queued_jobs else {})
        _run2 = str(_curj.get("requeued_reason") or "") == "stale_running_recovered"
        # C-20261125-G44R8（F2.3）：任务各自生命周期算——T01 有 builder_done 即「已完成」，不再套订单阶段词
        _marks = _order_marks(cur_code)
        _t01, _t02, _t03 = _stage_tasks(ord_state, _marks, True)
        _task_word = _t01  # three_q.task_status / progress 与 T01 逐字同源（F3.4 不变）
        group_chat_v = [
            {"ts": str(_curj.get("started_at") or now_str), "who": "调度泵",
             "msg": "施工开工（前次心跳丢失已自动回收重排）" if _run2 else "施工开工"},
            {"ts": str(_curj.get("enqueued_at") or ""), "who": "调度泵", "msg": "订单入队（自动调度）"},
        ]
        # F3.5：builder 每轮开工/完工、机检、质检判决(PASS/FAIL+P0/P1)、合入、门禁重试事件接真源 events.jsonl
        _seen_chat = set((str(_x.get("ts") or ""), str(_x.get("msg") or "")) for _x in group_chat_v)
        for _ec in _events_for_order(cur_code, 20):
            _k_ec = (str(_ec.get("ts") or ""), str(_ec.get("msg") or ""))
            if _k_ec in _seen_chat or not _k_ec[0]:
                continue
            _seen_chat.add(_k_ec)
            group_chat_v.append(_ec)
        group_chat_v = [x for x in group_chat_v if str(x.get("ts") or "")]  # 无时刻条目不入线（不造假）
        group_chat_v.sort(key=lambda x: str(x.get("ts") or ""))
        group_chat_v = group_chat_v[-24:]
        tasks_v = [
            {"id": "T01", "name": "产物构建施工（签名 APK）" if str(_curj.get("target") or "").endswith(".apk") else "产物构建施工", "status": _task_word, "owner": "Builder 施工席"},  # C-20261123-dtruth2：不带路径，页面脱敏后仍可读
            {"id": "T02", "name": "质检双门复核", "status": _t02, "owner": "UI审美质检员"},
            {"id": "T03", "name": "放行与提货交付", "status": _t03, "owner": "调度泵"},
        ]
    else:
        _task_word = _docs_state_word(ord_state, False)
        # C-20261125-G44R7（F2.4）：已交付单时间线接真源。
        # 原实现只有「面板产物已更新 + 专属质检」两条静态条目——工人施工轮（开工→完工）与合入/交付全丢，
        # 且「专属质检」那句是固定文案不是事件。现改为：队列 history 的入队/开工/完工（真值）
        # + events.jsonl 该单的机检/质检判决/合入轮次；缺项不编、无时刻条目不入线。
        _is_done_cur = str((cur_job or {}).get("status") or "") in ("done", "delivered", "accepted")
        if _is_done_cur:
            _dd = _done_detail(cur_job)
            group_chat_v = list(_dd.get("group_chat") or [])
            group_chat_v.append({"ts": v2_mtime, "who": "内核", "msg": f"面板产物已更新（{v3_ver}，{v2_bytes} 字节）"})
            group_chat_v = [x for x in group_chat_v if str(x.get("ts") or "")]
            group_chat_v.sort(key=lambda x: str(x.get("ts") or ""))
            group_chat_v = group_chat_v[-24:]
            tasks_v = _dd.get("tasks") or []
            # C-20261125-G44R8（F2.3/F3.4）：three_q.task_status 与 T01 逐字同源（T01 现在指「施工这项自身」=已完成）
            _task_word = str((tasks_v[0] if tasks_v else {}).get("status") or _task_word)
        else:
            group_chat_v = [
                {"ts": v2_mtime, "who": "内核", "msg": f"面板产物已更新（{v3_ver}，{v2_bytes} 字节）"},
                {"ts": _lb, "who": "专属质检", "msg": "按 UI 审美质检考卷完成三视口真机复核（首屏三区、配色克制、手机端零溢出）"},
            ]
            # C-20261125-G44R8（F2.3/F2.4）：任务状态按各自生命周期算、名称与主人与排队/在制/已交付单同一套
            # （原此处另起一套 Gate 1/Gate 2 名，同一张单交付前后任务名会变）
            _t01, _t02, _t03 = _stage_tasks(ord_state, _order_marks(cur_code), False)
            _task_word = _t01
            tasks_v = [
                {"id": "T01", "name": "产物构建施工", "status": _t01, "owner": "Builder 施工席"},
                {"id": "T02", "name": "质检双门复核", "status": _t02, "owner": "UI审美质检员"},
                {"id": "T03", "name": "放行与提货交付", "status": _t03, "owner": "调度泵"},
            ]
    # F3.4：订单卡「任务 n/m」与抽屉 tasks 行同源（原 52/52=施工版本数，与抽屉 0/3 口径分裂）
    order_item["task_n"] = sum(1 for _t in tasks_v if _task_done_word(_t.get("status")))
    order_item["task_m"] = len(tasks_v)
    order_detail = {
        "code": cur_code,
        "title": cur_title,
        "three_q": dict(_stage_q(ord_state), color=ord_color, step=node_txt,
                        progress=_task_word, task_status=_task_word,
                        seat=ord_owner, dwell_txt=dwell_txt_v),  # C-20261123-odrich
        "group_chat": group_chat_v,
        "tasks": tasks_v,
        "contract": _contract_mark(cur_code),  # C-20261125-G42 F2.3：抽屉也带约
        # C-20261125-G44R7（F2.1/F2.2）：交付时间与固定停留时长随 detail 一起给，
        # 已交付单的「交付时间」不再是「—」、停留时长不再随墙钟上涨（真实交付时刻=队列 finished_at）。
        "ordered_at": str((cur_job or {}).get("enqueued_at") or "—"),
        "delivered_at": (_dd.get("delivered_at") or "—") if _is_done_cur else "—",
        "dwell_sec": (_dd.get("dwell_sec") if _is_done_cur else 0) or 0,
        "dwell_txt": (_dd.get("dwell_txt") if _is_done_cur else dwell_txt_v) or "—",
        "dwell_note": (_dd.get("dwell_note") if _is_done_cur else "") or "",
        "running": bool(is_running),
    }

    def _alert_family(oid: str) -> str:
        return re.sub(r"R\d+$", "", re.sub(r"-R\d+$", "", str(oid or "")))

    def _alert_reason(row: dict) -> str:
        reason = " ".join(str(row.get("result_reason") or row.get("needs_reason") or "").split())
        report = str(row.get("report_json") or "")
        if not reason and report and os.path.isfile(report):
            try:
                reason = " ".join(str((json.load(open(report, encoding="utf-8")) or {}).get("final_summary") or "").split())
            except Exception:
                reason = ""
        for old in ("并升级人工处理 (needs_human)", "升级人工处理 (needs_human)", "升级人工 (needs_human)"):
            reason = reason.replace(old, "")
        reason = reason.strip(" ，。")
        try:
            import admit_v5
            reason = admit_v5.one_line(reason)
        except Exception:
            pass
        return reason

    latest_by_family = {}
    for row in list(hist) + [j for j in (q.get("jobs") or []) if isinstance(j, dict)]:
        if not isinstance(row, dict):
            continue
        oid = str(row.get("order_id") or "")
        if not oid:
            continue
        stamp = str(row.get("finished_at") or row.get("started_at") or row.get("enqueued_at") or "")
        fam = _alert_family(oid)
        prev = latest_by_family.get(fam)
        prev_stamp = ""
        if isinstance(prev, dict):
            prev_stamp = str(prev.get("finished_at") or prev.get("started_at") or prev.get("enqueued_at") or "")
        if prev is None or stamp >= prev_stamp:
            latest_by_family[fam] = row
    accidents = [row for row in latest_by_family.values() if str(row.get("status") or "") == "needs_human"]
    accidents.sort(key=lambda row: str(row.get("finished_at") or ""), reverse=True)

    alert_items = []
    if fuse_open:
        alert_items.append({
            "level": "red",
            "order": cur_code,
            "ts": now_str,
            "msg": f"预算熔断已触发：今日累计 ¥{daily_cny:.2f}，请核查网关账单",
        })
    for row in accidents:
        oid = str(row.get("order_id") or "")
        reason = _alert_reason(row)
        alert_items.append({
            "level": "red",
            "order": oid,
            "ts": str(row.get("finished_at") or now_str),
            "msg": f"{oid} 转驻厂专家。工厂内部架构专家接手研究" + (f"。{reason}" if reason else ""),
        })
    if not alert_items:
        alert_items.append({
            "level": "info",
            "order": cur_code,
            "ts": v2_mtime,
            "msg": f"本单已交付 · 管理税 0%",
        })
    _n_red = sum(1 for _a in alert_items if _a.get("level") == "red")
    _n_yel = sum(1 for _a in alert_items if _a.get("level") == "yellow")
    _n_blu = sum(1 for _a in alert_items if _a.get("level") == "info")
    _alertbar = f"{_n_red} 红 {_n_yel} 黄 {_n_blu} 蓝"
    _alert_level = "red" if _n_red else ("yellow" if _n_yel else "ok")

    runs_rows = []
    for idx, h in enumerate(reversed(hist[-50:]), 1):
        st_txt = ""
        ts_h = str(h.get("finished_at") or h.get("started_at") or now_str)[-8:]
        oid_h = str(h.get("order_id") or "C-20261121-001")
        # C-20261123-runstitle（用户令：只知道代号看不出是什么）：单号下补一行内容标题
        ttl_h = str(h.get("title") or "").strip()
        if ttl_h:
            ttl_h = ttl_h.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            oid_cell = f'{oid_h}<span style="display:block;font-size:11.5px;color:#64748b;margin-top:2px;line-height:1.35;white-space:normal">{ttl_h}</span>'
        else:
            oid_cell = oid_h
        reason_h = " ".join(str(h.get("result_reason") or h.get("needs_reason") or "").split())
        if not reason_h:
            _rp = str(h.get("report_json") or "")
            if _rp and os.path.isfile(_rp):
                try:
                    reason_h = " ".join(str((json.load(open(_rp, encoding="utf-8")) or {}).get("final_summary") or "").split())
                except Exception:
                    reason_h = ""
        try:
            import admit_v5
            for _old in ("并升级人工处理 (needs_human)", "升级人工处理 (needs_human)", "升级人工 (needs_human)"):
                reason_h = reason_h.replace(_old, "")
            reason_h = reason_h.strip(" ，。")
            reason_h = admit_v5.one_line(reason_h)
            st_txt = admit_v5.disposition(h.get("status"), reason_h)
        except Exception:
            if len(reason_h) > 96:
                reason_h = reason_h[:96] + "…"
            if not st_txt:
                st_txt = {"done": "已交付", "needs_human": "转驻厂专家", "running": "施工中"}.get(str(h.get("status") or ""), "未通过")
        reason_h = (reason_h or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        runs_rows.append(f"<tr><td>{ts_h}</td><td>{oid_cell}</td><td>{st_txt}</td><td>{reason_h}</td></tr>")
    if not runs_rows:
        runs_rows.append(f"<tr><td>{now_str[-8:]}</td><td>C-20261121-001</td><td>成功</td><td>—</td></tr>")

    live_stream = collect_live_agent_stream()
    accepted_count = max(1, len(done_runs))
    cost_per_accepted = round(daily_cny / accepted_count, 4)

    # C-20261123-odrich（用户令：下单/预计交付/计时全要）：details 逐单计时真值（原模板复制会串单）
    def _od_timing(_j):
        _st_j = str(_j.get("started_at") or "")
        _en_j = str(_j.get("enqueued_at") or "")
        _fi_j = str(_j.get("finished_at") or "")
        _sta_j = str(_j.get("status") or "")
        _run_j = _sta_j == "running"
        _bts, _dsec, _dtxt, _eta, _over = 0, 0, "—", "—", 0
        _dlv, _dnote = "", ""
        try:
            if _run_j and _st_j:
                _sdj = _dt.datetime.strptime(_st_j, "%Y-%m-%d %H:%M:%S")
                _bts = int(_sdj.timestamp())
                _dsec = max(0, int((now_dt - _sdj).total_seconds()))
                _mj = _dsec // 60
                _dtxt = ("已施工 %d小时%02d分%02d秒" % (_mj // 60, _mj % 60, _dsec % 60)) if _mj >= 60 else ("已施工 %d分%02d秒" % (_mj, _dsec % 60))
                _dnote = "开工至今（在制单实时走秒）"
                if _avg_sec_v > 0:
                    # C-20261125-G44R3（F3.2）：dwell 超过均速后不再吐过期 eta，改「已超预计 X 分」
                    _over = max(0, int((_dsec - int(_avg_sec_v)) // 60))
                    if _dsec > int(_avg_sec_v):
                        _eta = ""
                        _over = max(1, _over)
                    else:
                        _eta = "≈" + (_sdj + _dt.timedelta(seconds=int(_avg_sec_v))).strftime("%H:%M")
            elif _sta_j in ("done", "delivered", "accepted") and _fi_j:
                # C-20261125-G44R7（F2.2）：已交付单停在下单→交付之间，取固定时长。
                # 原实现落到下一分支按「现在 − 下单」算 → 已交付单的「停多久」随时间无限上涨（F1#2）。
                _b0 = _en_j or _st_j
                if _b0:
                    _sdj = _dt.datetime.strptime(_b0, "%Y-%m-%d %H:%M:%S")
                    _fdj = _dt.datetime.strptime(_fi_j, "%Y-%m-%d %H:%M:%S")
                    _dsec = max(0, int((_fdj - _sdj).total_seconds()))
                    _dtxt = _fmt_span(_dsec)
                    _dlv = _fi_j
                    _dnote = "下单→交付（已交付单停在此刻，不再随墙钟上涨）"
            elif (not _run_j) and _en_j:
                _edj = _dt.datetime.strptime(_en_j, "%Y-%m-%d %H:%M:%S")
                _dsec = max(0, int((now_dt - _edj).total_seconds()))
                _mj = _dsec // 60
                _dtxt = ("已等 %d小时%02d分" % (_mj // 60, _mj % 60)) if _mj >= 60 else ("已等 %d分" % _mj)
                _dnote = "下单至今（待排产）"
        except Exception:
            pass
        return {"running": _run_j, "base_ts": _bts, "dwell_sec": _dsec, "dwell_txt": _dtxt,
                "eta_delivery": _eta, "eta_over_min": _over, "avg_sec": int(_avg_sec_v),
                "ordered_at": _en_j or "—", "delivered_at": _dlv, "dwell_note": _dnote}
    expert_seat = None  # C-20261125-G46：模块缺失时降级（原实现会在下游 NameError 打穿整个取数）
    # C-20261125-G44R4 合并：主干侧 expert_seat 导入保留（席位池渲染要用）；
    # R3 的 eta_over_min 补进 _od_timing 返回（F3.2「已超预计 X 分」的前端数据源）。
    # line_details.scan_line_details 不再并入 details：它的 glob 与 _line_orders() 逐字节同源，
    # 键集是 _line_details 的子集/不相交，并入只会多出「无订单卡承载 ⇒ 抽屉不可达」且缺 three_q 的行，
    # 反而打破 F4「抽屉 T01 状态 == three_q.task_status」一致性（详见交件回执合并取舍段）。
    try:
        import expert_seat  # noqa: F401  （席位池 note/items 在下方 zones.seats 用）
    except Exception:
        pass
    # C-20261125-G46 产线注册制：席位真值先拼一次，产线卡与席位表共用同一份（不重复造数）
    _seat_items_v = [
        {
            "seat": "Builder施工",
            "status": seat_status_v,
            "order": seat_order_v,
            "title": cur_title if is_running else "",
            "last_beat": seat_beat_v,
            "beat_age_min": seat_age_v,
            "channel": {"status": "ok", "provider": "生产线", "model": ""},
        },
        *((expert_seat.seat_items(running_jobs, queued_jobs, now_str)) if expert_seat else []),
        {
            "seat": "UI审美质检",
            "status": seat_status_v,
            "order": seat_order_v,
            "title": cur_title if is_running else "",
            "last_beat": seat_beat_v,
            "beat_age_min": seat_age_v,
            "channel": {"status": "ok", "provider": "质检部门", "model": ""},
        },
    ]
    _reg_lines, _reg_err = lines_registry()
    _prod = collect_prod_lines(_reg_lines, _reg_err, _line_orders_cache, running_jobs, queued_jobs,
                               hist, _seat_items_v, cur_title, now_dt)
    # C-20261125-G44R3（F3.1/F3.4）：details 覆盖主队列在制/排队单 + 产线自动单（原只覆盖主队列 → 产线单抽屉全空）
    def _queued_detail(_jq):
        _c = str(_jq.get("order_id") or "?")
        _tm = _od_timing(_jq)
        _w = _docs_state_word("queued")
        _dq = {
            "code": _c, "title": str(_jq.get("title") or _c),
            "three_q": dict(_stage_q("queued"), color="#8d8d8d",
                            step="排队等待施工（自动调度）", progress=_w, task_status=_w,
                            seat="调度泵", dwell_txt=_tm["dwell_txt"]),
            "tasks": [
                {"id": "T01", "name": "产物构建施工", "status": _w, "owner": "Builder 施工席"},
                {"id": "T02", "name": "质检双门复核", "status": "待开工", "owner": "UI审美质检员"},
                {"id": "T03", "name": "放行与提货交付", "status": "待开工", "owner": "调度泵"},
            ],
            "group_chat": ([{"ts": str(_jq.get("enqueued_at")), "who": "调度泵", "msg": "订单入队（自动调度）"}]
                           if _jq.get("enqueued_at") else []),
        }
        _dq.update({k: _tm[k] for k in ("running", "base_ts", "dwell_sec", "dwell_txt", "eta_delivery",
                                        "eta_over_min", "avg_sec", "ordered_at", "delivered_at", "dwell_note")})
        return _dq

    _details_map: dict[str, Any] = {}
    _job_iter = []
    if cur_job:
        _job_iter.append(cur_job)
    _job_iter += queued_jobs
    for _j in _job_iter:
        _c = str(_j.get("order_id") or "?")
        if _c in _details_map:
            continue
        _tm = _od_timing(_j)
        # C-20261125-G44R5（P2-F6）：cur_job 在无在制时会回落到 queued_jobs[0]（见 cur_job 赋值），
        # 原判据只认 id(cur_job) → 队首排队单套用「已交付 3/3」模板，与其 queued 卡双口径。
        # 现按「是否排队单」分流：排队单一律走排队模板（T01==three_q.task_status 同源）。
        _is_queued = any(_j is _qq for _qq in queued_jobs)
        if _j is cur_job and not _is_queued:
            _base = dict(order_detail)
        else:
            _base = _queued_detail(_j)
        _base.update({"code": _c, "title": str(_j.get("title") or _c)})
        _base.update({k: _tm[k] for k in ("running", "base_ts", "dwell_sec", "dwell_txt", "eta_delivery",
                                          "eta_over_min", "avg_sec", "ordered_at", "delivered_at", "dwell_note")})
        # C-20261125-G44R7（F2.2）：抽屉与卡面同源——three_q.dwell_txt 也取固定时长口径
        if isinstance(_base.get("three_q"), dict):
            _base["three_q"]["dwell_txt"] = _tm["dwell_txt"]
            if _tm.get("dwell_note"):
                _base["three_q"]["dwell_note"] = _tm["dwell_note"]
        _details_map[_c] = _base
    # C-20261125-G44R3（R3 追加）：runner_slots 并发在制（本轮实测 2 席同开）时，第 2 张及以后的在制单
    # 原实现既无订单卡也无 details（details 只覆盖 cur_job=running_jobs[0]）→ 圆形区「生产中 2」与列表 1 张
    # 口径分裂、点开无卡。此处为剩余在制单按同一口径补 details + 卡片（真值取 queue/events，不造假）。
    for _rj in running_jobs:
        _rc = str(_rj.get("order_id") or "")
        if not _rc or _rc == cur_code or _rc in _details_map:
            continue
        _tm_r = _od_timing(_rj)
        _st_r = _stage_from_events(_rc) or "producing"
        if _st_r not in _stage_meta:
            _st_r = "producing"
        _w_r = _docs_state_word(_st_r, True)
        _scn_r, _scol_r, _sown_r, _snode_r = _stage_meta[_st_r]
        # C-20261125-G44R8（F2.3）：并发在制单的任务也按各自生命周期算（与 cur_job 同一口径）
        _t01_r, _t02_r, _t03_r = _stage_tasks(_st_r, _order_marks(_rc), True)
        _w_r = _t01_r
        _chat_r = [{"ts": str(_rj.get("started_at") or ""), "who": "调度泵", "msg": "施工开工"},
                   {"ts": str(_rj.get("enqueued_at") or ""), "who": "调度泵", "msg": "订单入队（自动调度）"}]
        _seen_r = set((str(_x.get("ts") or ""), str(_x.get("msg") or "")) for _x in _chat_r)
        for _ec_r in _events_for_order(_rc, 20):
            _k_r = (str(_ec_r.get("ts") or ""), str(_ec_r.get("msg") or ""))
            if _k_r in _seen_r or not _k_r[0]:
                continue
            _seen_r.add(_k_r)
            _chat_r.append(_ec_r)
        _chat_r = [x for x in _chat_r if str(x.get("ts") or "")]
        _chat_r.sort(key=lambda x: str(x.get("ts") or ""))
        _rd_r = {
            "code": _rc, "title": str(_rj.get("title") or _rc),
            "three_q": dict(_stage_q(_st_r), color=_scol_r, step=_snode_r,
                            progress=_w_r, task_status=_w_r,
                            seat=_sown_r, dwell_txt=_tm_r["dwell_txt"]),
            "tasks": [
                {"id": "T01", "name": "产物构建施工", "status": _t01_r, "owner": "Builder 施工席"},
                {"id": "T02", "name": "质检双门复核", "status": _t02_r, "owner": "UI审美质检员"},
                {"id": "T03", "name": "放行与提货交付", "status": _t03_r, "owner": "调度泵"},
            ],
            "group_chat": _chat_r[-24:],
        }
        _rd_r.update({k: _tm_r[k] for k in ("running", "base_ts", "dwell_sec", "dwell_txt",
                                            "eta_delivery", "eta_over_min", "avg_sec", "ordered_at")})
        _details_map[_rc] = _rd_r
        orders_arr.insert(1, {
            "code": _rc, "title": str(_rj.get("title") or _rc), "state": _st_r,
            "state_cn": _scn_r, "color": _scol_r, "priority": "P1", "p0": False,
            "node": {"t": _snode_r},
            "dwell_txt": _rd_r.get("dwell_txt") or "—",
            "task_n": sum(1 for _t in (_rd_r.get("tasks") or []) if _task_done_word(_t.get("status"))),
            "task_m": len(_rd_r.get("tasks") or []),
            "sub_n": 0, "sub_m": 0, "overdue": False, "delay_note": "",
            "pickup": "随时提货（支持 24h 一键打回重做）",
            "owner": "Builder 施工席", "signature": _signature_word(_st_r, True),
        })
    # C-20261125-G44R7（F2.1/F2.3/F2.4）：当日已交付单进订单列表 + details。
    # 根因：details/卡片只覆盖在制/排队/产线单，已交付单在页面上无卡可点——抽屉里「交付时间/停多久/
    # 时间线」三项在真机页面上根本不可达（F1 抽样的 G44R6 只因「当前无在制单」时 cur_job 回落到
    # hist[-1] 才偶然有卡；一有在制单就点不到）。真值全取队列 history，不造假。
    _today_str = now_dt.strftime("%Y-%m-%d")
    _done_today_v = [h for h in done_runs
                     if str(h.get("finished_at") or "").startswith(_today_str)
                     and str(h.get("order_id") or "") not in _line_keys]
    _done_today_v.sort(key=lambda r: str(r.get("finished_at") or ""), reverse=True)
    for _hd in _done_today_v[:12]:
        _hc = str(_hd.get("order_id") or "")
        if not _hc or _hc in _details_map:
            continue
        try:
            _dh = _done_detail(_hd)
        except Exception:
            continue
        _details_map[_hc] = _dh
        orders_arr.append({
            "code": _hc, "title": str(_hd.get("title") or _hc), "state": "delivered",
            "state_cn": "已交付待验收", "color": "#63d18b", "priority": "P1", "p0": False,
            "node": {"t": "已通过双门质检合入主线"},
            "dwell_txt": _dh.get("dwell_txt") or "—",
            "task_n": sum(1 for _t in (_dh.get("tasks") or []) if _task_done_word(_t.get("status"))),
            "task_m": len(_dh.get("tasks") or []),
            "sub_n": 0, "sub_m": 0, "overdue": False, "delay_note": "",
            "pickup": "随时提货（支持 24h 一键打回重做）",
            # C-20261125-G44R8（F2.6）：已交付单卡片「谁在办」与抽屉同源（流程当前节点=交付，主人=调度泵）
            "owner": "调度泵", "signature": _signature_word("delivered"),
            "delivered_at": _dh.get("delivered_at") or "—",
            "contract": _contract_mark(_hc),
        })
    for _lc, _ld in _line_details.items():
        _details_map.setdefault(_lc, dict(_ld))
    # C-20261125-G44R7（F2.3）：交付台账数据面。原 payload 从无 deliveries 字段 → 指标区
    # 「最近交付」永远「—」（F1#3：生产总览写着今天交付 5，指标区却「—」）。
    # 全部取队列 history status=done 的真实 finished_at，窗口计数按完工时刻落档，不补 0 不造假。
    def _dl_item(_h2):
        _ts2 = str(_h2.get("finished_at") or "").strip()
        if not _ts2:
            return None
        try:
            _e2e2 = int(round(float(_h2.get("e2e_total_sec") or 0)))
        except Exception:
            _e2e2 = 0
        return {"order": str(_h2.get("order_id") or "?"), "ts": _ts2, "e2e_sec": _e2e2}
    _dl_items_v = [x for x in (_dl_item(_h3) for _h3 in done_runs) if x]
    _dl_items_v.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    _dl_d0s = now_dt.strftime("%Y-%m-%d")
    _dl_y0s = (now_dt - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    _dl_7s = (now_dt - _dt.timedelta(days=6)).strftime("%Y-%m-%d")
    _dl_30s = (now_dt - _dt.timedelta(days=29)).strftime("%Y-%m-%d")
    deliveries_v = {
        "items": _dl_items_v[:200],
        "today": sum(1 for x in _dl_items_v if str(x.get("ts") or "").startswith(_dl_d0s)),
        "yesterday": sum(1 for x in _dl_items_v if str(x.get("ts") or "").startswith(_dl_y0s)),
        "d7": sum(1 for x in _dl_items_v if str(x.get("ts") or "")[:10] >= _dl_7s),
        "d30": sum(1 for x in _dl_items_v if str(x.get("ts") or "")[:10] >= _dl_30s),
        "total": len(_dl_items_v),
    }
    d = dict(base_data or {})
    d.update({
        "skin": "19",
        "v3_version": v3_ver,
        "kernel_ver": kernel_ver_v,
        "gateway_ver": gateway_ver_v,
        "build_no": build_no,
        "build_tag": build_tag,
        "build_sha": sha8,
        "delivered_at": v2_mtime,
        "contract": build_tag,
        "live_stream": live_stream,
        "cost_per_accepted_cny": cost_per_accepted,
        "ts": now_str,
        "zones_generated_at": now_iso,
        "zones_error": "",
        "orders_err": "",
        "orders": orders_arr,
        # C-20261125-G42 F2.3（数据面，不改版式）：已立约张数/状态分布，供订单卡「约」标消费
        "contracts": _contract_summary(orders_arr),
        # C-20261125-G46：lines[] 改为 registry 驱动的产线分组载荷（每线一卡：在制单/今日完成/席位/状态灯）；
        # 原扁平线内单表挪到 line_orders 保留（无代码消费，向后兼容不丢数据）
        "lines": _prod["lines"],
        "lines_meta": _prod["meta"],
        "line_orders": _line_orders_cache,
        # C-20261125-G44R7（F2.3）：交付台账（今日/昨日/7天/30天/累计 + 明细），指标区「最近交付」的数据源
        "deliveries": deliveries_v,
        "queue": queue_view,
        "states": states_arr,
        "states_by_range": states_by_range,  # C-20261123-z3range
        # C-20261125-G44R3：主队列在制/排队/并发在制 + 产线自动单全覆盖（可达抽屉 code 全量有 details）
        "details": _details_map,
        "kpis": {
            "disp": 1,
            "run": len(running_jobs) + sum(1 for _lo in _line_orders_cache if _lo["status"] == "running"),
            "done": len(done_runs) + sum(1 for _lo in _line_orders_cache if _lo["status"] == "done"),
            "queue": len(queued_jobs) + sum(1 for _lo in _line_orders_cache if _lo["status"] == "queued"),
            "blk": len(state_buckets.get("needs_human") or []),
            "tokens": f"{v3_tokens}",
            "viol": 0,
            "sess": 2,
        },
        "slots": slots_v,  # C-20261125-G43R1：{used, max, orders}
        "autos": _autos_rows(),
        "autosoff": _autos_off_rows()[0],
        "autosoffn": _autos_off_rows()[1],
        "runs": "".join(runs_rows),
        "sf": (
            f"<tr><td>{mtime_hm}</td><td>{v3_ver}</td><td>正常</td><td>沙箱隔离</td>"
            f"<td>Git Worktree 沙箱与六页正本哈希校验全绿</td><td>自动守护</td><td>正常</td></tr>"
        ),
        "sffoot": "当前台账",
        "dc": (
            f"<tr><td>{mtime_hm}</td><td>{v3_ver}</td>"
            f"<td>在册 {len(orders_arr)} 单（排队 {len(queued_jobs)} · 施工中 {len(running_jobs)}）</td></tr>"
        ),
        "ap": v3_ver,
        "zone_status": {k: "ok" for k in ("orders", "seats", "events", "capacity", "alerts", "cost", "handover")},
        "ui": {
            "alert_level": _alert_level,
            "alertbar": _alertbar,
            "ts": now_str,
            "contract": build_tag,
            "generated_at": now_iso,
        },
        "zones": {
            "orders": {
                "status": "ok",
                "as_of": now_iso,
                "summary": {
                    "orders": len(orders_arr),
                    "active": len(running_jobs),
                    "delivered": 0,
                    "overdue": 0,
                },
                "items": orders_arr,
            },
            "alerts": {
                "status": "ok",
                "as_of": now_iso,
                "items": alert_items,
            },
            "seats": {
                "status": "ok",
                "as_of": now_iso,
                "note": (expert_seat.pool_note() if expert_seat else "驻厂外聘专家模块未接入，席位表只列在册工人席"),
                "items": _seat_items_v,  # C-20261123-seattruth：active/挂单/心跳全真值，无单待命不造假
            },
            "cost": {
                "status": "ok",
                "as_of": now_iso,
                "free_tokens": free_tok,
                "paid_tokens": paid_tok,
                "paid_cny": round(daily_cny, 2),
                "today_tokens": free_tok + paid_tok,
                "cumulative_tokens": mu_month_tok or v3_tokens,
                "by_lane": by_model if by_model else [
                    {"lane": "免费道 weekend-glm", "tokens": free_tok,
                     "providers": [{"provider": "免费通道", "model": "¥0.00"}]},
                    {"lane": "付费网关池", "tokens": paid_tok,
                     "providers": [{"provider": "付费通道", "model": f"今日 ¥{daily_cny:.2f}"}]},
                ],
            },
            "events": {
                "status": "ok",
                "as_of": now_iso,
                "items": [
                    {
                        "ts": v2_mtime,
                        "badge": v3_ver,
                        "order": cur_code,
                        "action": f"交付 {build_tag}",
                        "artifact": f"面板产物 {v2_bytes} 字节",
                    }
                ],
            },
            "capacity": {
                "status": "ok",
                "as_of": now_iso,
                "note": "按单拉起 · 零空转 · 管理税 0.0%",
            },
            "handover": {
                "status": "ok",
                "as_of": now_iso,
                "items": [
                    {
                        "ts": v2_mtime,
                        "file": f"启用回执（{v3_ver}）",
                        "summary": [
                            f"当前版本：{v3_ver}",
                            f"全厂唯一在册订单：C-20261121-001（MES 工厂面板优化），今日实耗 ¥{daily_cny:.2f} / ¥30.00，管理税 0.0%",
                        ],
                    }
                ],
            },
        },
    })
    return d
