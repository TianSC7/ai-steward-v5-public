# -*- coding: utf-8 -*-
"""交接快照：只读 registry / evidence / worktrees，生成换班交接单 markdown。

为什么需要它：
  换班（gò班）时接管方最怕三件事——不知道谁还在办、不知道卡跑了多久、
  不知道哪些卡在等自己拍板。此前这些信息散在 state/HANDOVER.md（人工写）、
  reports/board.html（只读看板）、reports/evidence/*.md（逐卡证据）三处，
  人工汇总既慢又容易漏。本脚本把它们压成一份 reports/HANDOFF_<YYYYMMDD>.md：
  在办卡+会话+卡龄 / 今日完成 / 阻塞与未决 / 明日建议 / 模型与成本口径，
  接班的人读这一页就能上手，不必再翻台账。

输入（纯只读，绝不写台账/配置/工作区）：
  state/registry.json    任务台账（tasks 列表；字段口径同 board_view.py）
  state/worktrees.json   隔离工作区账（在办卡跑在哪个分支、是否已合并清理）
  config/steward.json    管家配置（stale_sec / daily_budget_cny 等）
  reports/evidence/*.md  证据文件（只读头部 200KB，查 EXIT= / [PASS] 标记）
  logs/gateway-<今天>.jsonl  网关流水（成本口径，复用 board_view.today_cost）

输出：
  默认写 reports/HANDOFF_<YYYYMMDD>.md（--out 可改），并往 stderr 打一行摘要；
  --json 打结构化快照到 stdout（字段见 build_snapshot），此时不落盘 md，
  除非显式给 --out。两者同一份数据，保证可复核。

五段口径（验收锁死，勿随意改名）：
  1. 在办卡      state=dispatched/running；卡龄 = now - created_at；
                 带会话 id（可直接 steer）、模型、工作区、心跳静默判定。
  2. 今日完成    state=done/abandoned 且完结时刻（updated_at → evidence_verified_at
                 → created_at，同 board_view.completion_dt）的本地日期 == now；
                 附证据状态与 EXIT=/[PASS] 标记（缺标记的卡接管方要补）。
  3. 阻塞与未决  blocked/failed 单列；未决 = 静默超阈值、缺会话、证据缺失、
                 坏时间戳、依赖未满足、工作区已合并但卡仍在办。
  4. 明日建议    从上面数据机械推导（解阻/查卡死/可派/补证据/队列空规则），
                 每条带依据卡号，不臆造业务方向。
  5. 模型与成本  在办与今日完成按 model 计数；成本 = 网关 ok 行 cost_cny 合计
                 （不含用户窗口直连；budget 取自 dispatch.daily_budget_cny，
                 0 表示不设上限）；口径写清，避免与日报对不上时互相怀疑。

卡号可点：
  卡号渲染为 markdown 链接，目标优先 证据文件（登记的 evidence_path → 约定
  reports/evidence/<id>.md），都没有则回退 state/registry.json；链接一律相对
  交接单所在目录，点开即到，不留死链。

容错口径（不抛栈是硬验收）：
  坏 registry（非 JSON / tasks 非列表）-> HandoffError，CLI 退出 2，
    stderr 打 [ERROR] 可读原因，无 Traceback；
  坏/缺 config、坏/缺 worktrees -> 不阻塞：退默认值，原因进 warnings；
  坏时间戳/缺字段 -> 卡龄标"未知"，该卡照样出现在表里，不丢卡、不抛栈；
  空台账 -> 五段齐全且各段写明"无"，退出码 0。

用法：
  python -X utf8 scripts/handoff.py
  python -X utf8 scripts/handoff.py --out reports/HANDOFF_20261118.md \
      --now 2026-11-18T19:30:00+08:00
  python -X utf8 scripts/handoff.py --json

退出码：0 交接单生成成功（含空台账/降级）；2 台账或参数错误（stderr 可读，无堆栈）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import board_view as bv  # noqa: E402  完成时刻/证据/成本三处口径复用，不另立一套
import review_gate as rg  # noqa: E402  通过/失败标记正则与复核门同口径，避免两处判不一致

DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")
DEFAULT_CONFIG = os.path.join(ROOT, "config", "steward.json")
DEFAULT_WORKTREES = os.path.join(ROOT, "state", "worktrees.json")
DEFAULT_OUT_DIR = os.path.join(ROOT, "reports")
DEFAULT_LOGS = os.path.join(ROOT, "logs")

SCHEMA = 1
TOOL = "handoff"

#: 状态口径（与 board_view.COLUMNS 同义，这里只取交接需要的分组）
ACTIVE_STATES = ("dispatched", "running")
DONE_STATES = ("done", "abandoned")
BLOCK_STATES = ("blocked", "failed")

DEFAULT_STALE_SEC = 1800
EVIDENCE_HEAD_BYTES = 200 * 1024

#: 每段建议/未决的显示上限；超出的用一行"另有 N 项见台账"收口，不刷屏。
SUGGEST_CAP = 8
PENDING_ROW_CAP = 40
ACTIVE_ROW_CAP = 40
DONE_ROW_CAP = 60

#: markdown 表格单元格里的换行/竖线必须压平，否则表格错位。
_CELL_WS_RE = re.compile(r"\s+")


class HandoffError(Exception):
    """台账结构性错误：调用方应打印可读原因并退出 2，不抛栈。"""


# ---------------------------------------------------------------- 读取（只读）


def sha256_of(path: str) -> str:
    """文件 sha256；读取失败返回空串（数据源字段降级，不抛栈）。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def load_registry(path: str) -> dict:
    """严格读取台账：坏 JSON / 非对象 / tasks 非列表都抛 HandoffError。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise HandoffError("台账读取失败 %s: %s" % (path, e))
    except ValueError as e:
        raise HandoffError("台账不是合法 JSON %s: %s" % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise HandoffError("台账根必须是对象且 tasks 必须是列表: %s" % path)
    return data


def load_config(path: str) -> tuple[dict, str]:
    """宽容读取配置：缺失/坏 JSON/非对象退默认值，返回 (配置, 提示)。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return {}, "配置读取失败，使用默认阈值: %s" % e
    except ValueError as e:
        return {}, "配置不是合法 JSON，使用默认阈值: %s" % e
    if not isinstance(data, dict):
        return {}, "配置根不是对象，使用默认阈值"
    return data, ""


def load_worktrees(path: str) -> tuple[dict, str]:
    """宽容读取工作区账：缺失/坏 JSON -> 空表 + 提示（交接单不靠它硬拦）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return {}, "工作区账读取失败，工作区列按空处理: %s" % e
    except ValueError as e:
        return {}, "工作区账不是合法 JSON，工作区列按空处理: %s" % e
    if not isinstance(data, dict) or not isinstance(data.get("worktrees"), dict):
        return {}, "工作区账结构异常（缺 worktrees 对象），工作区列按空处理"
    return data["worktrees"], ""


def parse_now(raw: str | None) -> datetime.datetime:
    """生成时刻：缺省取本机当前时间；--now 非法直接抛 ValueError（CLI 退 2）。"""
    if not raw:
        return datetime.datetime.now().astimezone().replace(microsecond=0)
    dt = datetime.datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.replace(microsecond=0)


def parse_ts(raw) -> datetime.datetime | None:
    """宽松解析时间戳：None/空串/脏值一律 None（调用方标"未知"，不抛栈）。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


# ---------------------------------------------------------------- 字段口径


def task_id(task: dict) -> str:
    return str(task.get("id") or "").strip()


def task_state(task: dict) -> str:
    return str(task.get("state") or "unknown").strip()


def tasks_of(registry: dict) -> list[dict]:
    return [t for t in registry.get("tasks") or [] if isinstance(t, dict)]


def stale_sec_of(config: dict) -> float:
    """静默阈值：dispatch.stale_sec → 顶层 stale_sec → 默认 1800；脏值退默认。"""
    dispatch = config.get("dispatch") if isinstance(config, dict) else None
    for raw in ((dispatch or {}).get("stale_sec"), config.get("stale_sec")):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return float(DEFAULT_STALE_SEC)


def budget_of(config: dict) -> float:
    """日预算（元）；0/缺省/脏值 = 不设上限。"""
    dispatch = config.get("dispatch") if isinstance(config, dict) else None
    try:
        value = float((dispatch or {}).get("daily_budget_cny"))
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def age_of(task: dict, now: datetime.datetime) -> tuple[float | None, str]:
    """卡龄 = now - created_at；坏/缺时间戳返回 (None, "未知")，不猜。"""
    created = parse_ts(task.get("created_at"))
    if created is None:
        return None, "未知"
    return (now - created).total_seconds(), human_age(created, now)


def human_age(created: datetime.datetime, now: datetime.datetime) -> str:
    """人读卡龄：<1m / 45m / 2h13m / 3d4h。"""
    seconds = max(0.0, (now - created).total_seconds())
    if seconds < 60:
        return "<1m"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh%02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd%dh" % (days, hours)


def stale_of(task: dict, now: datetime.datetime, stale_sec: float) -> tuple[bool, str]:
    """心跳是否静默超阈值；返回 (是否静默, 人读说明)。

    用 board_view.stale_seconds（last_check → last_progress_at 同口径），
    坏时间戳退化为"无心跳记录"，不算静默（不冤枉正在干活的卡）。
    """
    age = bv.stale_seconds(task, now)
    if age is None:
        return False, "无心跳记录"
    if age > stale_sec:
        return True, "静默 %dm" % int(age // 60)
    return False, "%dm 前" % int(age // 60)


def evidence_of(task: dict) -> dict:
    """证据三态 + 通过/失败标记（只读文件头 200KB，大文件不整读）。

    标记判定复用 review_gate 的正则（EXIT=\\S / [PASS]；失败 EXIT=FAIL / [FAIL]），
    交接单说"有标记"就必须过得了复核门；两者同时出现按复核门口径标红。
    """
    info = bv.evidence_info(task)
    info["has_marker"] = False
    info["has_fail_marker"] = False
    info["marker_note"] = "—"
    if info["status"] == "ok":
        local = info["path"] if os.path.isabs(info["path"]) else os.path.join(
            ROOT, info["path"].replace("/", os.sep))
        try:
            with open(local, "rb") as f:
                head = f.read(EVIDENCE_HEAD_BYTES).decode("utf-8", "replace")
        except OSError:
            info["marker_note"] = "读不了"
            return info
        info["has_marker"] = bool(rg._PASS_MARKER_RE.search(head))
        info["has_fail_marker"] = bool(rg._FAIL_MARKER_RE.search(head))
        if info["has_fail_marker"]:
            info["marker_note"] = "含 EXIT=FAIL/[FAIL]"
        elif info["has_marker"]:
            info["marker_note"] = "有标记"
        else:
            info["marker_note"] = "缺 EXIT=/[PASS]"
    return info


def link_target(task: dict, report_dir: str, registry_path: str) -> str:
    """卡号链接目标：登记的 evidence_path → 约定 reports/evidence/<id>.md → 台账。

    一律相对交接单所在目录（跨盘才退绝对路径），保证点开即到、不留死链。
    """
    candidates: list[str] = []
    raw = " ".join(str(task.get("evidence_path") or "").split())
    if raw:
        candidates.append(raw)
    tid = task_id(task)
    if tid:
        candidates.append(os.path.join("reports", "evidence", "%s.md" % tid))
    for item in candidates:
        local = item if os.path.isabs(item) else os.path.join(
            ROOT, item.replace("/", os.sep))
        if os.path.isfile(local):
            return rel_md_path(local, report_dir)
    return rel_md_path(os.path.abspath(registry_path), report_dir)


def rel_md_path(target: str, base_dir: str) -> str:
    """目标相对 base_dir 的 markdown 路径（正斜杠）；跨盘退绝对路径。"""
    try:
        rel = os.path.relpath(os.path.abspath(target), os.path.abspath(base_dir))
    except ValueError:
        rel = os.path.abspath(target)
    return rel.replace("\\", "/")


def cell(value) -> str:
    """markdown 单元格压平：换行/多空白收一格，竖线转义，空值给破折号。"""
    text = _CELL_WS_RE.sub(" ", str(value if value is not None else "")).strip()
    if not text:
        return "—"
    return text.replace("|", "\\|")


# ---------------------------------------------------------------- 快照构建


def build_snapshot(registry: dict, config: dict, worktrees: dict,
                   now: datetime.datetime, registry_path: str = "",
                   report_dir: str | None = None,
                   config_path: str = "", worktrees_path: str = "",
                   logs_dir: str | None = None) -> dict:
    """把台账压成交接快照（纯函数：不写盘、不抛栈——坏台账在 load 层已拦）。

    返回字段即 --json 契约（test_handoff.py 锁定），markdown 由 render() 从同一份
    数据渲染，避免"文档与 JSON 对不上"。
    """
    report_dir = report_dir or DEFAULT_OUT_DIR
    stale_sec = stale_sec_of(config)
    budget = budget_of(config)
    tasks = tasks_of(registry)
    # 成本只算一次：建议段的预算比较与第 5 段展示必须同一数，避免"建议说超了、
    # 成本段没超"的自相矛盾。
    cost_cny, cost_note = bv.today_cost(now, logs_dir=logs_dir)

    def link_of(task: dict) -> str:
        return link_target(task, report_dir, registry_path or DEFAULT_REGISTRY)

    def row(task: dict) -> dict:
        age_seconds, age_text = age_of(task, now)
        is_stale, heartbeat = stale_of(task, now, stale_sec)
        tid = task_id(task)
        wt = worktrees.get(tid) if isinstance(worktrees, dict) else None
        return {
            "id": tid or "(无 id)",
            "title": str(task.get("title") or "(无标题)"),
            "state": task_state(task),
            "tier": str(task.get("tier") or "").strip(),
            "model": str(task.get("model") or "").strip(),
            "sessionId": str(task.get("sessionId") or "").strip(),
            "project": str(task.get("project") or "").strip(),
            "workspace": str(task.get("workspace") or "").strip(),
            "created_at": str(task.get("created_at") or "").strip(),
            "age_seconds": age_seconds,
            "age": age_text,
            "last_check": str(task.get("last_check") or "").strip(),
            "heartbeat": heartbeat,
            "stale": is_stale,
            "attempts": task.get("attempts"),
            "max_attempts": task.get("max_attempts"),
            "next_action": str(task.get("next_action") or "").strip(),
            "note": str(task.get("note") or "").strip(),
            "depends_on": [str(x) for x in (task.get("depends_on") or [])]
            if isinstance(task.get("depends_on"), (list, tuple)) else [],
            "evidence_path": str(task.get("evidence_path") or "").strip(),
            "evidence_status": evidence_of(task)["status"],
            "link": link_of(task),
            "worktree": {
                "branch": str((wt or {}).get("branch") or "") if isinstance(wt, dict) else "",
                "path": str((wt or {}).get("path") or "") if isinstance(wt, dict) else "",
                "merged_at": str((wt or {}).get("merged_at") or "") if isinstance(wt, dict) else "",
                "removed_at": str((wt or {}).get("removed_at") or "") if isinstance(wt, dict) else "",
            } if isinstance(wt, dict) else None,
        }

    # 1) 在办：最老的排前面（最可能是卡死的那张），坏时间戳垫后
    active = [row(t) for t in tasks if task_state(t) in ACTIVE_STATES]
    active.sort(key=lambda r: (0, -r["age_seconds"]) if r["age_seconds"] is not None
                else (1, 0.0))

    # 2) 今日完成：完结时刻本地日期 == now 的本地日期，新的在前
    by_id = {task_id(t): t for t in tasks if task_id(t)}
    done_today: list[dict] = []
    for t in tasks:
        if task_state(t) not in DONE_STATES:
            continue
        finished = bv.completion_dt(t)
        if finished is None or finished.date() != now.date():
            continue
        item = row(t)
        item["finished_at"] = finished.isoformat()
        ev = evidence_of(t)
        item["has_marker"] = ev["has_marker"]
        item["has_fail_marker"] = ev["has_fail_marker"]
        item["marker_note"] = ev["marker_note"]
        item["evidence_label"] = ev["label"]
        done_today.append(item)
    done_today.sort(key=lambda r: r["finished_at"], reverse=True)

    # 3) 阻塞与未决
    blocked = [row(t) for t in tasks if task_state(t) in BLOCK_STATES]
    blocked.sort(key=lambda r: r["id"])

    pending: list[dict] = []

    def add_pending(kind: str, item: dict, detail: str) -> None:
        pending.append({"kind": kind, "id": item["id"], "title": item["title"],
                        "state": item["state"], "detail": detail,
                        "link": item["link"]})

    for item in active:
        if item["stale"]:
            add_pending("静默超阈值", item,
                        "%s 无进展（阈值 %dm），先 steer，无响应再 replace"
                        % (item["heartbeat"], int(stale_sec // 60)))
        if not item["sessionId"]:
            add_pending("缺会话", item, "卡上没有 sessionId，接管方无法 steer，需补派")
        if item["evidence_status"] == "missing":
            add_pending("证据路径失效", item, "登记了 %s 但文件不在" % item["evidence_path"])
        if item["worktree"] and item["worktree"]["merged_at"] and not item["worktree"]["removed_at"]:
            add_pending("工作区已合并", item,
                        "worktree 已在 %s 合并，但卡仍 %s，需确认收尾或重派"
                        % (item["worktree"]["merged_at"], item["state"]))
    for item in blocked:
        if not item["next_action"] and not item["note"]:
            add_pending("阻塞无处置说明", item,
                        "%s 且无 next_action/note，接管方需自己判断解阻路径" % item["state"])

    for t in tasks:
        if parse_ts(t.get("created_at")) is None:
            add_pending("坏时间戳", row(t), "created_at=%r 解析不了，卡龄标未知"
                        % (t.get("created_at"),))
        if task_state(t) == "done":
            # 只查 done：abandoned 是弃案，本就不要求证据，查它只会刷噪音。
            ev = evidence_of(t)
            if ev["status"] != "ok":
                add_pending("证据缺失", row(t),
                            ev["label"] + ("（路径 %s）" % ev["path"] if ev["path"] else ""))
            elif ev["has_fail_marker"]:
                add_pending("证据判失败", row(t),
                            "%s 含 EXIT=FAIL/[FAIL]，复核必红，需重跑或人工判读" % ev["path"])
            elif not ev["has_marker"]:
                add_pending("证据缺标记", row(t),
                            "%s 无 EXIT= 或 [PASS] 标记，复核会被打回" % ev["path"])

    # 依赖未满足的排队卡：排队本身不是错，但接管方要知道卡在哪
    state_by_id = {task_id(t): task_state(t) for t in tasks if task_id(t)}
    for t in tasks:
        if task_state(t) != "queued":
            continue
        deps = t.get("depends_on") if isinstance(t.get("depends_on"), (list, tuple)) else []
        unmet = [str(d) for d in deps
                 if state_by_id.get(str(d)) not in (None, "done")]
        if unmet:
            add_pending("等依赖", row(t),
                        "依赖 %s 未完成（%s）"
                        % (",".join(unmet),
                           ", ".join("%s=%s" % (d, state_by_id.get(d, "缺卡")) for d in unmet)))

    # 4) 明日建议：机械推导，每条带依据，不臆造业务方向
    suggestions: list[dict] = []

    def suggest(text: str, ids: list[str]) -> None:
        suggestions.append({"rank": len(suggestions) + 1, "text": text, "ids": ids})

    for item in [x for x in active if x["stale"]][:3]:
        suggest("先查 %s 是否卡死（%s，会话 %s）：steer 纠偏，无响应再 replace"
                % (item["id"], item["heartbeat"], item["sessionId"] or "缺"),
                [item["id"]])
    for item in blocked[:3]:
        why = item["next_action"] or item["note"] or "无处置说明，需人判断"
        suggest("解阻 %s（%s，尝试 %s/%s）：%s"
                % (item["id"], item["state"], item["attempts"], item["max_attempts"], why),
                [item["id"]])
    if not active:
        ready = [row(t) for t in tasks if task_state(t) == "queued"
                 and not (t.get("depends_on") if isinstance(
                     t.get("depends_on"), (list, tuple)) else [])]
        for item in ready[:3]:
            suggest("可派 %s（%s / %s）：%s"
                    % (item["id"], item["tier"] or "未标 tier",
                       item["model"] or "未标模型", item["title"]),
                    [item["id"]])
    if not any(t for t in tasks if task_state(t) == "queued"):
        suggest("队列空（queued=0）：按 queue-empty 规则从 reports/DAILY_REPORT_*.md "
                "或 backlog 取一项落卡，别让调度台空转", [])

    evidence_fixes = 0
    for item in done_today:
        if evidence_fixes >= 3:
            break
        if item["evidence_status"] != "ok":
            suggest("补证据 %s：%s" % (item["id"], item.get("evidence_label") or "证据缺失"),
                    [item["id"]])
            evidence_fixes += 1
        elif item.get("has_fail_marker"):
            suggest("修证据 %s：%s（复核必红，重跑或人工判读）"
                    % (item["id"], item.get("marker_note") or "含失败标记"), [item["id"]])
            evidence_fixes += 1
        elif not item["has_marker"]:
            suggest("补标记 %s：%s（复核门要 EXIT=/[PASS]）"
                    % (item["id"], item.get("marker_note") or "缺通过标记"), [item["id"]])
            evidence_fixes += 1
    if budget > 0 and cost_cny > budget:
        suggest("今日成本 ¥%.2f 已超预算 ¥%.2f，明日先做不烧额度的轻活（nex 池）"
                % (cost_cny, budget), [])

    # 5) 模型与成本口径
    by_model: dict[str, dict] = {}
    for item, bucket in [(x, "active") for x in active] + [(x, "done_today") for x in done_today]:
        key = item["model"] or "(未标模型)"
        slot = by_model.setdefault(key, {"model": key, "active": 0, "done_today": 0})
        slot[bucket] += 1
    model_rows = sorted(by_model.values(),
                        key=lambda r: (-(r["active"] + r["done_today"]), r["model"]))

    snapshot = {
        "schema": SCHEMA,
        "tool": TOOL,
        "generated_at": now.isoformat(),
        "sources": {
            "registry": registry_path,
            "config": config_path,
            "worktrees": worktrees_path,
            "registry_sha256": sha256_of(registry_path) if registry_path else "",
            "config_sha256": sha256_of(config_path) if config_path else "",
            "worktrees_sha256": sha256_of(worktrees_path) if worktrees_path else "",
        },
        "thresholds": {"stale_sec": stale_sec, "daily_budget_cny": budget},
        "summary": {
            "active": len(active),
            "done_today": len(done_today),
            "blocked": len(blocked),
            "queued": sum(1 for t in tasks if task_state(t) == "queued"),
            "pending": len(pending),
            "cost_cny": cost_cny,
        },
        "active": active,
        "done_today": done_today,
        "blocked": blocked,
        "pending": pending,
        "suggestions": suggestions,
        "models": {"by_model": model_rows, "cost": {"cny": cost_cny, "note": cost_note},
                   "budget_cny": budget},
        "warnings": [],
    }
    return snapshot


# ---------------------------------------------------------------- 渲染


def render(snapshot: dict) -> str:
    """把快照渲染成交接单 markdown（五段齐全；空段也写"无"，不留空标题）。"""
    s = snapshot
    day = str(s["generated_at"])[:10]
    lines: list[str] = [
        "# 交接单 %s（换班接管用）" % day,
        "",
        "> 生成：`%s` · 工具：`scripts/handoff.py`（只读快照，不写台账/不改任务）" % s["generated_at"],
        "> 数据源：`state/registry.json`（sha256 `%s`）· `state/worktrees.json` · "
        "`reports/evidence/`（只读）" % (s["sources"]["registry_sha256"][:16] or "读不到"),
        "> 卡号可点：链接优先落证据文件，没有证据则回退台账。",
        "",
        "## 摘要",
        "",
        "| 在办 | 今日完成 | 阻塞 | 待派(queued) | 未决项 | 今日成本 |",
        "|---|---|---|---|---|---|",
        "| %d | %d | %d | %d | %d | ¥%.4f |" % (
            s["summary"]["active"], s["summary"]["done_today"],
            s["summary"]["blocked"], s["summary"]["queued"],
            s["summary"]["pending"], s["summary"]["cost_cny"]),
        "",
    ]

    # 1. 在办卡
    lines += ["## 1. 在办卡（%d 张）" % s["summary"]["active"], ""]
    if not s["active"]:
        lines += ["无在办卡（state=dispatched/running 为空）。", ""]
    else:
        lines += ["| 卡号 | 任务 | 状态 | 卡龄 | 会话（可直接 steer） | 模型 | 工作区/分支 | 心跳 |",
                  "|---|---|---|---|---|---|---|---|"]
        for item in s["active"][:ACTIVE_ROW_CAP]:
            branch = (item.get("worktree") or {}).get("branch") or ""
            where = item["workspace"] or branch or "—"
            if branch and item["workspace"]:
                where = "%s（%s）" % (item["workspace"], branch)
            lines.append("| [%s](%s) | %s | %s | %s | %s | %s | %s | %s |" % (
                cell(item["id"]), item["link"], cell(item["title"]), cell(item["state"]),
                cell(item["age"]), cell("`%s`" % item["sessionId"] if item["sessionId"] else ""),
                cell(item["model"]), cell(where), cell(item["heartbeat"])))
        if len(s["active"]) > ACTIVE_ROW_CAP:
            lines.append("| … 另有 %d 张 | 见台账 | | | | | | |"
                         % (len(s["active"]) - ACTIVE_ROW_CAP))
        lines.append("")
    lines += ["> 卡龄 = now − created_at；心跳取 last_check（退化 last_progress_at），"
              "超过 %d 分钟标静默。" % int(s["thresholds"]["stale_sec"] // 60), ""]

    # 2. 今日完成
    lines += ["## 2. 今日完成（%d 张）" % s["summary"]["done_today"], ""]
    if not s["done_today"]:
        lines += ["今日无完结卡（按完结时刻本地日期判定）。", ""]
    else:
        lines += ["| 卡号 | 任务 | 状态 | 完结时刻 | 模型 | 证据 | 标记 |",
                  "|---|---|---|---|---|---|---|"]
        for item in s["done_today"][:DONE_ROW_CAP]:
            lines.append("| [%s](%s) | %s | %s | %s | %s | %s | %s |" % (
                cell(item["id"]), item["link"], cell(item["title"]), cell(item["state"]),
                cell(item["finished_at"][11:19]), cell(item["model"]),
                cell(item.get("evidence_label") or ""), cell(item.get("marker_note") or "")))
        if len(s["done_today"]) > DONE_ROW_CAP:
            lines.append("| … 另有 %d 张 | 见台账 | | | | | |"
                         % (len(s["done_today"]) - DONE_ROW_CAP))
        lines.append("")
    lines += ["> 标记列查证据文件头 200KB 是否含 `EXIT=` 或 `[PASS]`；"
              "`缺 EXIT=/[PASS]` 的卡复核会被打回，接管方先补。", ""]

    # 3. 阻塞与未决
    lines += ["## 3. 阻塞与未决", ""]
    lines += ["### 3.1 阻塞（%d 张）" % s["summary"]["blocked"], ""]
    if not s["blocked"]:
        lines += ["无阻塞卡。", ""]
    else:
        lines += ["| 卡号 | 任务 | 状态 | 尝试 | 处置/备注 |", "|---|---|---|---|---|"]
        for item in s["blocked"]:
            why = item["next_action"] or item["note"] or "无处置说明"
            lines.append("| [%s](%s) | %s | %s | %s/%s | %s |" % (
                cell(item["id"]), item["link"], cell(item["title"]), cell(item["state"]),
                cell(item["attempts"]), cell(item["max_attempts"]), cell(why)))
        lines.append("")

    lines += ["### 3.2 未决（%d 项）" % s["summary"]["pending"], ""]
    if not s["pending"]:
        lines += ["无未决项。", ""]
    else:
        lines += ["| 类型 | 卡号 | 说明 |", "|---|---|---|"]
        for item in s["pending"][:PENDING_ROW_CAP]:
            lines.append("| %s | [%s](%s) | %s |" % (
                cell(item["kind"]), cell(item["id"]), item["link"], cell(item["detail"])))
        if len(s["pending"]) > PENDING_ROW_CAP:
            lines.append("| … 另有 %d 项 | | 见台账 |"
                         % (len(s["pending"]) - PENDING_ROW_CAP))
        lines.append("")

    # 4. 明日建议
    lines += ["## 4. 明日建议（按序）", ""]
    if not s["suggestions"]:
        lines += ["无待办建议（队列空、无阻塞、证据齐全）。", ""]
    else:
        for item in s["suggestions"][:SUGGEST_CAP]:
            basis = ("　依据：%s" % "、".join(item["ids"])) if item["ids"] else ""
            lines.append("%d. %s%s" % (item["rank"], item["text"], basis))
        if len(s["suggestions"]) > SUGGEST_CAP:
            lines.append("%d. 另有 %d 条低优先建议，见台账自行取舍。"
                         % (SUGGEST_CAP + 1, len(s["suggestions"]) - SUGGEST_CAP))
        lines.append("")

    # 5. 模型与成本口径
    lines += ["## 5. 模型与成本口径", "", "### 5.1 模型分布（在办 + 今日完成）", ""]
    if not s["models"]["by_model"]:
        lines += ["无可统计卡片。", ""]
    else:
        lines += ["| 模型 | 在办 | 今日完成 |", "|---|---|---|"]
        for item in s["models"]["by_model"]:
            lines.append("| %s | %d | %d |" % (
                cell(item["model"]), item["active"], item["done_today"]))
        lines.append("")
    lines += ["### 5.2 成本", ""]
    lines.append("- 今日网关成本：**¥%.4f**（%s）" % (
        s["models"]["cost"]["cny"], cell(s["models"]["cost"]["note"])))
    budget = s["models"]["budget_cny"]
    lines.append("- 日预算：%s" % ("不设上限（daily_budget_cny=0）" if budget <= 0
                                  else "¥%.2f" % budget))
    lines += [
        "- 口径：成本 = `logs/gateway-<今日>.jsonl` 中 `event=ok` 的 `cost_cny` 合计，"
        "**不含用户窗口直连**；模型计数取台账 `model` 字段。",
        "- 对账：按任务/按 tier 的细账看 `scripts/budget_report.py --json`，"
        "两者同日应一致；不一致先查网关日志时区。",
        "",
    ]

    # 数据源与只读声明
    lines += ["## 数据源与只读声明", ""]
    for label, path, sha in (
            ("台账", s["sources"]["registry"], s["sources"]["registry_sha256"]),
            ("配置", s["sources"]["config"], s["sources"]["config_sha256"]),
            ("工作区账", s["sources"]["worktrees"], s["sources"]["worktrees_sha256"])):
        if path:
            lines.append("- %s：`%s`（sha256 `%s`）" % (label, path, (sha or "读不到")[:16]))
    lines += ["- 本文件由 `scripts/handoff.py` 生成：**只读**输入，不写台账、不改任务、"
              "不碰工作区；重跑结果只随时间与台账变化。", ""]
    if s["warnings"]:
        lines += ["## 生成告警", ""]
        lines += ["- %s" % cell(w) for w in s["warnings"]]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 落盘与 CLI


def write_atomic(path: str, text: str) -> None:
    """临时文件 + fsync + os.replace，不留下半截 markdown。"""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".handoff-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_out(now: datetime.datetime) -> str:
    """默认产物：reports/HANDOFF_<YYYYMMDD>.md（按 now 的本地日期）。"""
    return os.path.join(DEFAULT_OUT_DIR, "HANDOFF_%s.md" % now.strftime("%Y%m%d"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="交接快照：只读 registry/evidence/worktrees，"
                    "生成 reports/HANDOFF_<YYYYMMDD>.md（五段：在办/今日完成/"
                    "阻塞未决/明日建议/模型成本）")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--worktrees", default=DEFAULT_WORKTREES)
    ap.add_argument("--out", default=None,
                    help="交接单落盘路径（默认 reports/HANDOFF_<YYYYMMDD>.md）")
    ap.add_argument("--logs", default=None,
                    help="网关流水目录（默认 logs/；成本口径同 board_view）")
    ap.add_argument("--json", action="store_true",
                    help="打结构化快照到 stdout（不落盘，除非同时给 --out）")
    ap.add_argument("--now", default=None, help="ISO 时间（测试用，固定生成时刻）")
    args = ap.parse_args(argv)

    try:
        now = parse_now(args.now)
        registry = load_registry(args.registry)
    except (HandoffError, ValueError) as e:
        print("[ERROR] %s" % (str(e) or e.__class__.__name__), file=sys.stderr)
        return 2

    config, config_warning = load_config(args.config)
    worktrees, worktrees_warning = load_worktrees(args.worktrees)
    out_path = args.out or default_out(now)
    snapshot = build_snapshot(registry, config, worktrees, now,
                              registry_path=args.registry,
                              report_dir=os.path.dirname(os.path.abspath(out_path)),
                              config_path=args.config,
                              worktrees_path=args.worktrees,
                              logs_dir=args.logs)
    snapshot["warnings"] = [w for w in (config_warning, worktrees_warning) if w]

    text = render(snapshot)
    wrote = ""
    if not args.json or args.out:
        try:
            write_atomic(out_path, text)
        except OSError as e:
            print("[ERROR] 交接单落盘失败 %s: %s" % (out_path, e), file=sys.stderr)
            return 2
        wrote = out_path

    if args.json:
        payload = dict(snapshot)
        payload["out"] = wrote
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if wrote:
        print("HANDOFF active=%d done_today=%d blocked=%d queued=%d pending=%d "
              "cost=¥%.4f -> %s" % (
                  snapshot["summary"]["active"], snapshot["summary"]["done_today"],
                  snapshot["summary"]["blocked"], snapshot["summary"]["queued"],
                  snapshot["summary"]["pending"], snapshot["summary"]["cost_cny"],
                  wrote), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
