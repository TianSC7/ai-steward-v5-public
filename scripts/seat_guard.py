# -*- coding: utf-8 -*-
"""五经理制 PreToolUse 硬闸：CreateSession 前拦住“新建经理”。

正本：dll/factory-blueprint/席位/00-五经理制.md
注册表：state/seat_registry.json

规则：
  - 经理席（B/C/D/E/F 的“经理/生产/质检/审计/策划/运保”）一律 deny；
    经理只能复用注册表中的持久会话（SendToSession），不得按单新建。
  - A-001 已存在且未归档时，再建“厂长”也 deny，防止重复厂长。
  - 员工席（W-xxx）放行；测试/其他会话放行。
  - 钩子异常一律放行（exit 0），但写日志；结构违规由 factory_seat.py verify 兜底。

输入：stdin JSON（ZCode PreToolUse），字段含 tool_name/tool_input/session_id。
输出：deny 时输出 hookSpecificOutput；放行时输出空。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
import sys

sys.dont_write_bytecode = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
REG_PATH = os.path.join(ROOT, "state", "seat_registry.json")
DB_PATH = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
LOG_PATH = os.path.join(ROOT, "logs", "seat_guard.jsonl")

# 强经理声明：标题【...C101经理...】/“你是生产经理”/“你是 C-101 审计经理”/“你是…的策划经理 B-005”
STRONG_MANAGER = [
    re.compile(r"(?i)【[^】]{0,90}([BCDEF])[-－]?\d{0,3}\s*(?:经理|生产|质检|审计|策划|运保)[^】]*】"),
    re.compile(r"(?i)(?:你是|角色)[^\n]{0,40}?(策划|审计|生产|质检|运保)\s*经理"),
    re.compile(r"(?i)(?:你是|角色)[^\n]{0,40}?([BCDEF])[-－]?\d{2,3}(?![0-9])"),
]
# 席位声明：席位/工号/SEAT + B-F 编号
SEAT_DECL = re.compile(r"(?i)(?:席位|工号|SEAT)\s*[=:：]?\s*([BCDEF])[-－]?(\d{2,3})(?![0-9])")
# 任意席位号（用于“非正席一律拒”）
SEAT_ANY = re.compile(r"(?i)(?<![A-Z0-9])([BCDEF])[-－]?(\d{2,3})(?![0-9])")
CANON = {"A-001", "B-001", "C-101", "D-101", "E-101", "F-001"}
DECL_NEAR = re.compile(r"(?i)(你是|角色|席位|工号|SEAT|新开|新建|新拉|拉一[个位]|派一[个位]|补一[个位])")
ROLE_AFTER = re.compile(r"(?i)(经理|生产|质检|审计|策划|运保)")
# 旧称
LEGACY = re.compile(r"(?i)M-(PLAN|SAFETY)")
EXEC_DECL = re.compile(r"(?i)(?:你是工厂厂长|席位\s*A-001|A-001\s*厂长)")
WORKER_DECL = re.compile(r"(?i)(?:你是|SEAT\s*=)\s*W[-－]?\d{2,3}")


def _log(payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _deny(session: str, reason: str, prompt: str) -> None:
    _log({
        "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "session": (session or "")[:24],
        "decision": "deny",
        "reason": reason,
        "prompt_head": (prompt or "").replace("\n", " ")[:160],
    })
    out = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


def _registry() -> dict:
    try:
        with open(REG_PATH, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _exec_active() -> bool:
    """A-001 是否已有未归档会话（有则禁止再建厂长）。"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
        try:
            row = con.execute(
                "select count(*) from session where time_deleted is null and time_archived is null "
                "and (title like '%A-001%' or title like '%厂长%')"
            ).fetchone()
            return bool(row and row[0] > 0)
        finally:
            con.close()
    except Exception:
        return False


def _seat_status(seat: str):
    """注册表中该正席的状态；缺省返回 None。"""
    try:
        for s in _registry().get("seats", []):
            if isinstance(s, dict) and s.get("seat") == seat:
                return s.get("status")
    except Exception:
        return None
    return None


def decide(prompt: str) -> tuple[bool, str]:
    # 不再只看前 700 字：厂长把角色声明写在长 prompt 中段，首版因此漏检 B-005（08:32）
    head = prompt or ""
    first = head.splitlines()[0] if head else ""
    worker = bool(WORKER_DECL.search(head[:1200]))

    strong = any(p.search(head) for p in STRONG_MANAGER)
    seat_decl = SEAT_DECL.search(head)
    legacy = LEGACY.search(head)
    bad_seats = []
    for m in SEAT_ANY.finditer(head):
        seat = "%s-%s" % (m.group(1).upper(), m.group(2))
        if seat in CANON or seat in bad_seats:
            continue
        before = head[max(0, m.start() - 30):m.start()]
        after = head[m.end():m.end() + 12]
        # 只在“声明/新建/派席”语境下拒；纯引用旧席（如“B-002 的产物归并到 B-001”）放行
        if DECL_NEAR.search(before) or ROLE_AFTER.search(after):
            bad_seats.append(seat)

    # 员工声明优先：W-101 的 prompt 里可以提到“向 C-101 经理汇报”，不算建经理
    if worker and not strong and not bad_seats and not seat_decl and not legacy:
        return True, "worker"

    if strong or seat_decl or legacy or bad_seats:
        # 故障重建白名单（D-20261120-042）：仅当 prompt 显式声明正席、且注册表该席
        # status != active（blocked-model/dead/vacant）时放行；重建后必须立即改注册表，
        # 否则下一次就会因 status=active 被拒。普通“再建一个经理”仍 deny。
        m_decl = SEAT_DECL.search(head)
        if m_decl and not bad_seats and not legacy:
            canon = "%s-%s" % (m_decl.group(1).upper(), m_decl.group(2))
            if canon in CANON:
                st = _seat_status(canon)
                if st and st != "active":
                    return True, "rebuild:%s(status=%s，旧席不可投递)" % (canon, st)
        detail = ""
        m = SEAT_DECL.search(head)
        if m:
            detail = "（命中席位 %s-%s）" % (m.group(1).upper(), m.group(2))
        elif bad_seats:
            detail = "（命中非正席 %s）" % ",".join(bad_seats)
        elif legacy:
            detail = "（命中旧称 %s）" % legacy.group(0)
        return False, (
            "五经理制硬闸：禁止用 CreateSession 新建经理席%s。"
            "经理只有 B-001/C-101/D-101/E-101/F-001 五个持久席，"
            "派活请用 SendToSession 复用注册表中的会话；"
            "第 6 位经理=违规，厂长无权自行补位。正本：席位/00-五经理制.md" % detail
        )

    if EXEC_DECL.search(first) and _exec_active():
        return False, (
            "五经理制硬闸：A-001 厂长会话已存在且未归档，禁止再建第二个厂长。"
            "续用现有会话；如需继任，先归档旧席并更新 state/seat_registry.json。"
        )

    return True, "allow"


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return 0
        if str(data.get("tool_name") or "") != "CreateSession":
            return 0
        ti = data.get("tool_input") or {}
        prompt = ti.get("prompt") if isinstance(ti, dict) else ""
        if not isinstance(prompt, str):
            prompt = ""
        sid = str(data.get("session_id") or data.get("sessionId") or "")
        allow, reason = decide(prompt)
        if not allow:
            _deny(sid, reason, prompt)
        else:
            _log({
                "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "session": sid[:24],
                "decision": "allow",
                "reason": reason,
                "prompt_head": prompt.replace("\n", " ")[:160],
            })
        return 0
    except Exception as exc:  # 钩子异常不锁死工厂；verify 兜底
        try:
            _log({
                "ts": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "decision": "error-allow",
                "error": "%s: %s" % (type(exc).__name__, exc),
            })
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
