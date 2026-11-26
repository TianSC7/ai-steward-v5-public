# -*- coding: utf-8 -*-
"""派单后会话健康探针（INC-20261118-007）。

只读 ZCode 本地库，不发消息、不改任何状态：
  - tasks-index.tasks 行数：应用任务列表按这张表渲染，缺行 = UI 里看不到子代理
  - cli message 行数 / assistant 带 token 输出条数：0 条 = 这个会话从没真正跑起来

判据（2026-11-18 W-20261118-021 幽灵派单实测）：
  tasks_row == 0 或 assistant_with_output == 0  -> ghost
  error（库读不到）                              -> unknown（不误判）
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

DEFAULT_CLI_DB = "~/.zcode/cli/db/db.sqlite"
DEFAULT_TASKS_DB = "~/.zcode/v2/tasks-index.sqlite"


def _ro(path: str) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % os.path.expanduser(path).replace("\\", "/")
    return sqlite3.connect(uri, uri=True, timeout=3.0)


def probe(session_id: str, cli_db: str | None = None,
          tasks_db: str | None = None) -> dict:
    """返回会话可见性/存活证据；任何库错误写进 error，由调用方判 unknown。"""
    info = {
        "session_id": session_id,
        "tasks_row": 0,
        "messages": 0,
        "assistant": 0,
        "assistant_with_output": 0,
        "last_role": None,
        "last_message_at": None,
        "idle_sec": None,
        "error": None,
    }
    if not session_id:
        info["error"] = "empty-session-id"
        return info
    try:
        con = _ro(tasks_db or DEFAULT_TASKS_DB)
        try:
            info["tasks_row"] = int(con.execute(
                "select count(*) from tasks where task_id=?", (session_id,)).fetchone()[0])
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        info["error"] = "tasks-db: %s" % exc
        return info
    try:
        con = _ro(cli_db or DEFAULT_CLI_DB)
        try:
            rows = con.execute(
                "select data from message where session_id=? order by sequence",
                (session_id,)).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        info["error"] = "cli-db: %s" % exc
        return info
    for row in rows:
        raw = row[0] if isinstance(row, (tuple, list)) else row
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        role = data.get("role")
        info["messages"] += 1
        info["last_role"] = role
        ts = (data.get("time") or {}).get("created")
        if isinstance(ts, (int, float)) and ts > 0:
            info["last_message_at"] = max(info["last_message_at"] or 0, ts)
        if role != "assistant":
            continue
        info["assistant"] += 1
        tokens = data.get("tokens") or {}
        if (tokens.get("output") or 0) > 0 or (tokens.get("total") or 0) > 0:
            info["assistant_with_output"] += 1
    if info["last_message_at"]:
        info["idle_sec"] = round(time.time() - info["last_message_at"] / 1000.0, 1)
    return info


def verdict(info: dict) -> str:
    """healthy | ghost | unknown。unknown 不触发任何自动动作。"""
    if info.get("error"):
        return "unknown"
    if int(info.get("tasks_row") or 0) == 0:
        return "ghost"
    if int(info.get("assistant_with_output") or 0) == 0:
        return "ghost"
    return "healthy"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="只读探查一个 worker 会话是否真的跑起来了")
    ap.add_argument("--session", required=True)
    ap.add_argument("--cli-db", default=None)
    ap.add_argument("--tasks-db", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    info = probe(args.session, args.cli_db, args.tasks_db)
    info["verdict"] = verdict(info)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["verdict"] == "healthy" else (2 if info["verdict"] == "ghost" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
