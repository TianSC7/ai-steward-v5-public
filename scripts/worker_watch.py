# -*- coding: utf-8 -*-
"""实时纠偏监控（只读）：读 ZCode CLI 库 part 表，把"会话看着在跑其实已死"变成可判定警报。

背景：管家给 worker 会话派单后，真正怕的不是报错，而是静默死亡——
工具连续失败没人管、同一条命令打转、会话 idle 到天荒地老。part 表是 ZCode CLI
的会话流水（text/tool/step-* 等 part，data 为 JSON，time_created/time_updated 为
epoch 毫秒）。本脚本只读它，按会话算出：
  idle_sec      距最后一条 part 的空闲秒数（now 可由 --now 钉死，测试确定性）
  last_tool     最后一条 tool part 的工具名
  last_status   该 tool 的 state.status（running/completed/error/…，无 tool 记 none）
  fail_streak   自新往旧连续 status=error 的 tool 条数
  repeat_hits   最近 window 条 tool 中，同一"工具名+规范化输入"签名的最大连续重复次数
  last_text     最后一条 text part 的摘要（截断）
对照阈值给 alert(ok/warn/critical) 与中文纠偏建议。

用法：
  python -X utf8 worker_watch.py                              # 全部在办会话（近 --active-min 分钟有流水）
  python -X utf8 worker_watch.py --session sess_xxx           # 指定会话，可重复
  python -X utf8 worker_watch.py --all                        # 含所有有过流水的会话
  python -X utf8 worker_watch.py --json
  python -X utf8 worker_watch.py --warn-idle 120 --crit-idle 600 --warn-fail 1  # 阈值 CLI 覆盖
  python -X utf8 worker_watch.py --card-state done,abandoned  # 只体检指定卡状态（可选）
  python -X utf8 worker_watch.py --db <fixture.sqlite> --now 1795000300000       # 测试

收口卡过滤（A-10 修复）：
  默认读 state/registry.json，把卡状态 done/abandoned 的会话跳过，不进入 warn/critical；
  另读取 ~/.zcode/v2/tasks-index.sqlite 的 task_status：任务已 completed/interrupted 的
  会话同样按“已收口”跳过（task_status 权威，不论 presence 是否残留）。
  --include-closed 可强制纳入（排障用，退出码会照常反映其真实档位）。

控制面幽灵探针：
  对每个被体检会话读 session_runtime_presence（CLI 库）与 tasks.task_status（tasks-index）。
  卡片仍活跃、presence 无记录、task_status 非 completed/interrupted 时标 ghost_suspect；
  若同时 idle>=warn_idle，则至少 warn 并在 reasons 里写明“控制面 ghost”，避免把
  proto.sessionNotFound 误当普通空闲（也避免把已收口会话误报）。

状态（每会话取最重的一档）：
  ok         idle 未过 warn 线、无连续失败、无重复签名
  warn       idle>=warn_idle，或 fail_streak>=warn_fail，或 repeat_hits>=warn_repeat，
             或最后一条工具即 error，或 ghost_suspect 且 idle>=warn_idle
  critical   idle>=crit_idle，或 fail_streak>=crit_fail，或 repeat_hits>=crit_repeat，
             或指定会话在 part 表无流水（NO-DATA）

退出码：0 全部 ok；1 有 warn；3 有 critical；2 库/参数问题。
只读：sqlite 以 mode=ro 打开，不写库、不建 -wal/-shm 之外任何文件。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")
DEFAULT_TASKS_DB = os.path.expanduser("~/.zcode/v2/tasks-index.sqlite")

# 卡/任务已收口的状态：不参与 critical/warn（A-10 误报修复）
CLOSED_CARD_STATES = {"done", "abandoned"}
CLOSED_TASK_STATES = {"completed", "interrupted"}

DEFAULTS = {
    "active_min": 60.0,     # 默认只盯"在办"会话：最近这么久还有流水的
    "warn_idle": 300.0,     # 空闲 warn/critical 线（秒）
    "crit_idle": 900.0,
    "warn_fail": 2,         # 连续失败 warn/critical 条数
    "crit_fail": 3,
    "warn_repeat": 3,       # 同签名连续重复 warn/critical 次数
    "crit_repeat": 5,
    "window": 20,           # 重复签名检测只看最近这么多条 tool part
    "text_len": 120,        # last_text 截断长度
}

REQUIRED_FIELDS = ("session_id", "idle_sec", "last_tool", "last_status",
                   "fail_streak", "repeat_hits", "alert")


# ---------------------------------------------------------------- 基础工具

def _ro_uri(path: str) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def _fmt_ms(v) -> str:
    if not v:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(v)


def _parse_now(raw: str | None) -> int:
    """--now：epoch 毫秒或 ISO 时间；缺省取当前时钟。"""
    if not raw:
        return int(datetime.datetime.now().timestamp() * 1000)
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise SystemExit("--now 只接受 epoch 毫秒或 ISO 时间: %r" % raw)
    return int(dt.timestamp() * 1000)


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 卡状态/控制面探针

def load_registry_cards(path: str) -> tuple[dict[str, dict], str]:
    """读 registry 的 tasks[]，返回 {sessionId: {id,state}}。缺失/坏 JSON 不抛栈。"""
    if not path or not os.path.isfile(path):
        return {}, "registry_missing"
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError) as e:
        return {}, "registry_error:%s" % e
    tasks = obj.get("tasks") if isinstance(obj, dict) else None
    if not isinstance(tasks, list):
        return {}, "registry_no_tasks"
    out: dict[str, dict] = {}
    for c in tasks:
        if not isinstance(c, dict):
            continue
        sid = c.get("sessionId") or c.get("session_id")
        if not sid:
            continue
        out[str(sid)] = {"id": c.get("id"), "state": c.get("state")}
    return out, ""


def probe_presence(con: sqlite3.Connection, session_id: str) -> dict:
    """读 CLI 库 session_runtime_presence；表缺失/坏读按 unknown 处理。"""
    try:
        row = con.execute(
            "select owner_id, process_id from session_runtime_presence where session_id=?",
            (session_id,)).fetchone()
    except sqlite3.Error as e:
        return {"presence": None, "presence_error": str(e)}
    if row:
        return {"presence": True, "owner_id": row[0], "process_id": row[1]}
    return {"presence": False}


def open_tasks_db(path: str):
    """只读打开 tasks-index；不可用返回 None（探针降级，不影响主流程）。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        return sqlite3.connect(_ro_uri(path), uri=True, timeout=5)
    except sqlite3.Error:
        return None


def probe_task_status(tcon, session_id: str) -> dict:
    if tcon is None:
        return {"task_status": None, "task_status_error": "tasks_db_missing"}
    try:
        row = tcon.execute(
            "select task_status from tasks where task_id=? order by updated_at desc limit 1",
            (session_id,)).fetchone()
    except sqlite3.Error as e:
        return {"task_status": None, "task_status_error": str(e)}
    return {"task_status": row[0] if row else None}


def control_plane_state(meta: dict | None, presence, task_status) -> tuple[str, bool, str]:
    """返回 (control_plane, ghost_suspect, reason)。"""
    if presence is True:
        return "present", False, ""
    if presence is None:
        return "unknown", False, "presence_unavailable"
    card_state = (meta or {}).get("state")
    if task_status in CLOSED_TASK_STATES:
        return "closed_by_task", False, "task_status=%s" % task_status
    if meta and card_state not in CLOSED_CARD_STATES:
        return ("ghost_suspect", True,
                "卡片 state=%s 仍活跃，但 session_runtime_presence 无记录、task_status=%r"
                % (card_state, task_status))
    return "unknown", False, ""


def closed_reason(meta: dict | None, presence, task_status) -> str:
    """已收口判定：done/abandoned 卡，或任务已 completed/interrupted（task_status 权威）。"""
    if meta and meta.get("state") in CLOSED_CARD_STATES:
        return "card_state=%s" % meta.get("state")
    if task_status in CLOSED_TASK_STATES:
        return "task_status=%s" % task_status
    return ""


def annotate_control_plane(st: dict, meta: dict | None, probe: dict, th: dict) -> None:
    """把卡/控制面字段挂到会话结果；ghost 且空闲过 warn 线时升到至少 warn。"""
    st["card_id"] = (meta or {}).get("id")
    st["card_state"] = (meta or {}).get("state")
    st["presence"] = probe.get("presence")
    st["task_status"] = probe.get("task_status")
    st["control_plane"] = probe.get("control_plane")
    st["ghost_suspect"] = bool(probe.get("ghost_suspect"))
    if probe.get("control_plane_error"):
        st["control_plane_error"] = probe["control_plane_error"]
    if st["ghost_suspect"]:
        idle = st.get("idle_sec") or 0
        if idle >= th["warn_idle"]:
            if st["alert"] == "ok":
                st["alert"] = "warn"
            st.setdefault("reasons", []).append(
                "控制面 ghost：%s" % (probe.get("control_plane_reason") or "session_runtime_presence 无记录"))
            st.setdefault("advice", []).append(
                "控制面不可寻址（sessionNotFound 类 ghost）：先 OpenSession 重绑，勿直接判死/重建")


# ---------------------------------------------------------------- part 解析

def _signature(part: dict) -> str:
    """工具签名 = 工具名 + 规范化输入（key 排序的 JSON）；用于识别"同一条命令打转"。"""
    tool = str(part.get("tool") or "")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = state.get("input")
    if isinstance(inp, dict):
        try:
            canon = json.dumps(inp, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            canon = str(inp)
    elif inp is None:
        canon = ""
    else:
        canon = str(inp)
    return tool + "|" + canon


def _iter_parts(con: sqlite3.Connection, session_id: str, window: int) -> list[dict]:
    """取某会话最近 window 条 part（新→旧）。坏 JSON / 非对象 data 跳过不当崩。"""
    out: list[dict] = []
    try:
        cur = con.execute(
            "select time_created, time_updated, data from part "
            "where session_id=? order by max(time_created, time_updated) desc limit ?",
            (session_id, window))
    except sqlite3.Error:
        return out
    for tc, tu, data in cur.fetchall():
        try:
            d = json.loads(data) if data else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        out.append({
            "type": d.get("type"),
            "tool": d.get("tool"),
            "status": (d.get("state") or {}).get("status") if isinstance(d.get("state"), dict) else None,
            "signature": _signature(d) if d.get("type") == "tool" else "",
            "text": d.get("text") if isinstance(d.get("text"), str) else "",
            "time": max(int(tc or 0), int(tu or 0)),
        })
    return out


def _last_activity(con: sqlite3.Connection, session_id: str) -> int:
    row = con.execute(
        "select max(max(time_created, time_updated)) from part where session_id=?",
        (session_id,)).fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------- 会话体检

def watch_session(con: sqlite3.Connection, session_id: str, now_ms: int,
                  th: dict) -> dict:
    """单会话体检：产出稳定字段 + 原因 + 建议。坏数据不抛栈，按最保守档处理。"""
    parts = _iter_parts(con, session_id, max(int(th["window"]), 1))
    last_at = _last_activity(con, session_id) if parts else 0
    idle_sec = max(0, int((now_ms - last_at) / 1000)) if last_at else None

    tools = [p for p in parts if p["type"] == "tool"]
    last_tool = tools[0]["tool"] if tools else "-"
    last_status = tools[0]["status"] if tools else "none"
    if tools and not last_status:
        last_status = "unknown"

    fail_streak = 0
    for p in tools:
        if p["status"] == "error":
            fail_streak += 1
        else:
            break

    repeat_hits = 0
    repeat_sig = ""
    if tools:
        run_sig, run_n = tools[0]["signature"], 1
        for p in tools[1:]:
            if p["signature"] == run_sig:
                run_n += 1
            else:
                if run_n > repeat_hits:
                    repeat_hits, repeat_sig = run_n, run_sig
                run_sig, run_n = p["signature"], 1
        if run_n > repeat_hits:
            repeat_hits, repeat_sig = run_n, run_sig

    last_text = ""
    for p in parts:
        if p["type"] == "text" and p["text"].strip():
            last_text = _clip(p["text"], int(th["text_len"]))
            break

    st = {
        "session_id": session_id,
        "idle_sec": idle_sec,
        "last_tool": last_tool,
        "last_status": last_status,
        "fail_streak": fail_streak,
        "repeat_hits": repeat_hits,
        "repeat_sig": _clip(repeat_sig, 80),
        "last_text": last_text,
        "last_at": _fmt_ms(last_at),
    }
    if not parts:
        st["alert"] = "critical"
        st["reasons"] = ["NO-DATA"]
        st["advice"] = ["part 表无此会话流水：会话不存在、库被清或从未启动；"
                        "核对 session_id 与库路径，必要时重新派单"]
        return st

    alert, reasons, advice = _verdict(st, th)
    st["alert"], st["reasons"], st["advice"] = alert, reasons, advice
    return st


def _verdict(st: dict, th: dict) -> tuple[str, list[str], list[str]]:
    """按阈值定级；critical 任一命中即 critical，否则 warn，再否则 ok。"""
    reasons: list[str] = []
    advice: list[str] = []
    level = "ok"

    idle = st["idle_sec"] or 0
    if idle >= th["crit_idle"]:
        level = "critical"
        reasons.append("idle=%ds≥crit %ds" % (idle, th["crit_idle"]))
        advice.append("会话已空闲 %ds：极可能卡死或派单丢失；中断会话并按任务卡重启派单" % idle)
    elif idle >= th["warn_idle"]:
        level = "warn"
        reasons.append("idle=%ds≥warn %ds" % (idle, th["warn_idle"]))
        advice.append("会话空闲 %ds：确认 worker 是否仍在办；超 %ds 无进展即按停滞处理" % (idle, th["crit_idle"]))

    fs, rs = st["fail_streak"], st["repeat_hits"]
    if fs >= th["crit_fail"]:
        level = "critical"
        reasons.append("fail_streak=%d≥crit %d" % (fs, th["crit_fail"]))
        advice.append("工具连续失败 %d 次：查看最近报错（权限/路径/命令），人工介入或改写任务卡后重派" % fs)
    elif fs >= th["warn_fail"]:
        level = max(level, "warn", key=lambda x: {"ok": 0, "warn": 1, "critical": 2}[x])
        reasons.append("fail_streak=%d≥warn %d" % (fs, th["warn_fail"]))
        advice.append("工具已连续失败 %d 次：再失败 %d 次即 critical，提前核对命令与环境" % (fs, th["crit_fail"]))
    elif fs == 1 or st["last_status"] == "error":
        level = max(level, "warn", key=lambda x: {"ok": 0, "warn": 1, "critical": 2}[x])
        reasons.append("last_status=error(fail_streak=%d)" % fs)
        advice.append("最后一条工具报错：看报错决定重试还是改写命令")

    if rs >= th["crit_repeat"]:
        level = "critical"
        reasons.append("repeat_hits=%d≥crit %d" % (rs, th["crit_repeat"]))
        advice.append("同一命令签名连续重复 %d 次（%s）：疑似死循环，中断会话并细化任务卡" % (rs, st["repeat_sig"]))
    elif rs >= th["warn_repeat"]:
        level = max(level, "warn", key=lambda x: {"ok": 0, "warn": 1, "critical": 2}[x])
        reasons.append("repeat_hits=%d≥warn %d" % (rs, th["warn_repeat"]))
        advice.append("同一命令签名已连续重复 %d 次（%s）：观察是否在打转，必要时中断" % (rs, st["repeat_sig"]))

    if st["last_status"] == "running" and idle >= th["crit_idle"]:
        level = "critical"
        reasons.append("tool running 超时 %ds" % idle)
        advice.append("最后一条工具仍 running 且空闲 %ds：疑似卡死，中断后重试" % idle)

    if level == "ok":
        advice.append("无需干预")
    return level, reasons, advice


# ---------------------------------------------------------------- 汇总

def collect(db: str, sessions: list[str], now_ms: int, th: dict,
            active_min: float, include_all: bool,
            registry_path: str = "", tasks_db: str = "",
            include_closed: bool = False,
            card_state_filter: set[str] | None = None
            ) -> tuple[list[dict], list[str], list[dict]]:
    """返回 (sessions 结果, 警告, 已收口跳过)。库不存在/打不开由调用方转退出码 2。"""
    warns: list[str] = []
    skipped: list[dict] = []
    if not os.path.isfile(db):
        raise FileNotFoundError(db)
    cards, reg_err = load_registry_cards(registry_path)
    if reg_err == "registry_missing" and registry_path:
        warns.append("registry 不存在，卡状态过滤降级：%s" % registry_path)
    elif reg_err and reg_err != "registry_missing":
        warns.append("registry 读取失败（%s），卡状态过滤降级" % reg_err)
    tcon = open_tasks_db(tasks_db)
    con = sqlite3.connect(_ro_uri(db), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        ids = list(dict.fromkeys(sessions))
        if not ids:
            rows = con.execute(
                "select session_id, max(max(time_created, time_updated)) as last "
                "from part group by session_id order by last desc").fetchall()
            cutoff = now_ms - int(active_min * 60000)
            for r in rows:
                if include_all or (r["last"] or 0) >= cutoff:
                    ids.append(r["session_id"])
            if not include_all:
                n_skipped = len(rows) - len(ids)
                if n_skipped > 0:
                    warns.append("已跳过 %d 个超 %g 分钟无流水的会话（--all 可见）" % (n_skipped, active_min))

        results: list[dict] = []
        for sid in ids:
            meta = cards.get(sid)
            if card_state_filter is not None:
                card_state = (meta or {}).get("state")
                if card_state not in card_state_filter:
                    skipped.append({"session_id": sid, "card_id": (meta or {}).get("id"),
                                    "card_state": card_state,
                                    "reason": "card_state 不在 --card-state 过滤内"})
                    continue
            presence = probe_presence(con, sid).get("presence")
            task = probe_task_status(tcon, sid).get("task_status")
            closed = closed_reason(meta, presence, task)
            if closed and not include_closed:
                skipped.append({"session_id": sid, "card_id": (meta or {}).get("id"),
                                "card_state": (meta or {}).get("state"),
                                "task_status": task, "presence": presence,
                                "reason": "已收口（%s）" % closed})
                continue
            st = watch_session(con, sid, now_ms, th)
            cp, ghost, cp_reason = control_plane_state(meta, presence, task)
            probe = {"presence": presence, "task_status": task,
                     "control_plane": cp, "ghost_suspect": ghost,
                     "control_plane_reason": cp_reason}
            annotate_control_plane(st, meta, probe, th)
            results.append(st)
    finally:
        con.close()
        if tcon is not None:
            tcon.close()
    return results, warns, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="worker 会话实时纠偏监控（只读 part 表）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--session", action="append", default=[], help="指定 session_id，可重复")
    ap.add_argument("--all", action="store_true", help="含所有有过流水的会话（默认只在办）")
    ap.add_argument("--active-min", type=float, default=DEFAULTS["active_min"],
                    help="在办判定：最近这么多分钟还有流水（默认 %(default)s）")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY,
                    help="卡台账路径（默认 state/registry.json；缺失则卡状态过滤降级）")
    ap.add_argument("--tasks-db", default=DEFAULT_TASKS_DB,
                    help="tasks-index 路径（默认 ~/.zcode/v2/tasks-index.sqlite）")
    ap.add_argument("--include-closed", action="store_true",
                    help="把 done/abandoned 或 task_status=completed 的会话也纳入（排障用）")
    ap.add_argument("--card-state", default="",
                    help="逗号分隔的卡状态白名单，如 dispatched,running；缺省=全部（收口除外）")
    ap.add_argument("--warn-idle", type=float, default=DEFAULTS["warn_idle"])
    ap.add_argument("--crit-idle", type=float, default=DEFAULTS["crit_idle"])
    ap.add_argument("--warn-fail", type=int, default=DEFAULTS["warn_fail"])
    ap.add_argument("--crit-fail", type=int, default=DEFAULTS["crit_fail"])
    ap.add_argument("--warn-repeat", type=int, default=DEFAULTS["warn_repeat"])
    ap.add_argument("--crit-repeat", type=int, default=DEFAULTS["crit_repeat"])
    ap.add_argument("--window", type=int, default=DEFAULTS["window"])
    ap.add_argument("--now", default=None, help="测试用：epoch 毫秒或 ISO 时间")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.warn_idle > args.crit_idle or args.warn_fail > args.crit_fail \
            or args.warn_repeat > args.crit_repeat:
        print("PARAM-ERROR warn 阈值不得大于 crit 阈值")
        return 2
    if args.window < 1:
        print("PARAM-ERROR --window 必须 >=1")
        return 2

    now_ms = _parse_now(args.now)
    th = {
        "active_min": args.active_min,
        "warn_idle": args.warn_idle, "crit_idle": args.crit_idle,
        "warn_fail": args.warn_fail, "crit_fail": args.crit_fail,
        "warn_repeat": args.warn_repeat, "crit_repeat": args.crit_repeat,
        "window": args.window, "text_len": DEFAULTS["text_len"],
    }

    card_state_filter = {s.strip() for s in args.card_state.split(",") if s.strip()} or None
    try:
        results, warns, skipped = collect(args.db, args.session, now_ms, th,
                                          args.active_min, args.all,
                                          registry_path=args.registry,
                                          tasks_db=args.tasks_db,
                                          include_closed=args.include_closed,
                                          card_state_filter=card_state_filter)
    except FileNotFoundError:
        print("DB-MISSING %s" % args.db)
        return 2
    except sqlite3.Error as e:
        print("DB-ERROR %s" % e)
        return 2

    counts = {"ok": 0, "warn": 0, "critical": 0}
    for r in results:
        counts[r["alert"]] += 1
    ok = counts["warn"] == 0 and counts["critical"] == 0

    if args.json:
        print(json.dumps({
            "ok": ok,
            "checked_at": _fmt_ms(now_ms),
            "db": args.db,
            "thresholds": {k: th[k] for k in sorted(th)},
            "warnings": warns,
            "skipped": skipped,
            "sessions": [{k: r.get(k) for k in REQUIRED_FIELDS} |
                         {"last_at": r.get("last_at"), "last_text": r.get("last_text"),
                          "repeat_sig": r.get("repeat_sig"), "reasons": r.get("reasons"),
                          "advice": r.get("advice"),
                          "card_id": r.get("card_id"), "card_state": r.get("card_state"),
                          "presence": r.get("presence"), "task_status": r.get("task_status"),
                          "control_plane": r.get("control_plane"),
                          "ghost_suspect": r.get("ghost_suspect")} for r in results],
        }, ensure_ascii=False, indent=2))
    else:
        for w in warns:
            print("WATCH-NOTE %s" % w)
        for s in skipped:
            print("SKIP %s card=%s state=%s task=%s presence=%r | %s" % (
                s.get("session_id"), s.get("card_id"), s.get("card_state"),
                s.get("task_status"), s.get("presence"), s.get("reason")))
        for r in results:
            print("sess %s idle=%s last=%s/%s fail=%d repeat=%d alert=%s%s" % (
                r["session_id"],
                ("%ds" % r["idle_sec"]) if r["idle_sec"] is not None else "n/a",
                r["last_tool"], r["last_status"], r["fail_streak"], r["repeat_hits"],
                r["alert"],
                (" | " + "；".join(r["reasons"] + r["advice"])) if r["reasons"] else ""))
        print("WATCH sessions=%d skipped=%d ok=%d warn=%d critical=%d -> %s" % (
            len(results), len(skipped), counts["ok"], counts["warn"], counts["critical"],
            "OK" if ok else "ATTENTION"))
    if counts["critical"]:
        return 3
    return 1 if counts["warn"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
