# -*- coding: utf-8 -*-
"""执行句柄解析：给任意会话找出「可用于 WaitSessions 的 commandId 候选」。

背景：InterruptSession 需要 running 状态的 executionId，而 executionId 只能由
WaitSessions({sessionId, commandId}) 取得。派单时保存的 commandId 只覆盖自己派出的
会话；对自己没派过的会话（用户手工开的），ZCode 把每次输入记在 session_input 表里，
id 形如 `queue_<commandId>`（automation 输入是 `queue_automation-...`，不可 wait）。

本脚本只读地列出某会话最近的输入 commandId（新→旧），由调用方逐个
WaitSessions(timeoutMs=0)，第一个返回 status=running 的就是当前执行；
取其 executionId 即可 InterruptSession + SendToSession 立即投递。

用法：
  python -X utf8 exec_handle.py --session sess_xxx [--limit 10] [--json]
退出码：0 = 正常（count 可能为 0）；2 = 数据库不可读/参数错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")


def resolve_db() -> str:
    return os.environ.get("STEWARD_ZCODE_DB") or DEFAULT_DB


def _connect(path: str) -> sqlite3.Connection:
    """优先只读+WAL 可见；immutable 兜底（可能看不到最新 WAL，但不会锁库）。"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=2)
        con.execute("pragma query_only=1")
        con.execute("select 1 from session_input limit 1").fetchone()
        return con
    except sqlite3.Error:
        con = sqlite3.connect("file:%s?immutable=1" % path.replace("\\", "/"), uri=True)
        con.execute("pragma query_only=1")
        return con


def candidates(session_id: str, limit: int = 10) -> list[dict]:
    con = _connect(resolve_db())
    try:
        rows = con.execute(
            "select id, delivery, status, time_created from session_input "
            "where session_id=? order by time_created desc limit ?",
            (session_id, max(1, min(limit * 3, 90))),
        ).fetchall()
    finally:
        con.close()

    out: list[dict] = []
    for sid, delivery, status, ts in rows:
        sid = str(sid)
        if sid.startswith("queue_automation-"):
            continue  # 自动化消息不是 commandId，WaitSessions 不认
        if not sid.startswith("queue_"):
            continue
        out.append({
            "commandId": sid[len("queue_"):],
            "delivery": delivery,
            "status": status,
            "time_created": ts,
        })
        if len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("exec_handle")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(60, "exec_handle")

    db = resolve_db()
    if not os.path.exists(db):
        print("数据库不存在: %s" % db, file=sys.stderr)
        return 2
    try:
        items = candidates(args.session, args.limit)
    except sqlite3.Error as e:
        print("数据库读取失败: %s" % e, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"sessionId": args.session, "db": db, "count": len(items),
                          "candidates": items}, ensure_ascii=False, indent=2))
    else:
        print("session %s 候选 %d 个（新→旧）:" % (args.session, len(items)))
        for it in items:
            print("  %s  delivery=%-8s status=%-8s ts=%s"
                  % (it["commandId"], it["delivery"], it["status"], it["time_created"]))
        if not items:
            print("  （无候选：会话可能从未有过输入，或数据库未含该会话）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
