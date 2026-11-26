#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""会话归档助手——替代本环境缺失的 SetSessionArchived 工具。

归档 = 从默认会话列表收走，不删历史、不中断执行（用户 2026-11-18 明令：
"用完对话要记得归档、不要留一堆在那里，要及时清理"）。实现：把会话在会话库里的
归档字段置位——CLI 会话库（~/.zcode/cli/db/db.sqlite）session.time_archived；
GUI 任务库（~/.zcode/v2/tasks-index.sqlite）tasks.archived=1。两个库都查，命中哪个改哪个。

用法：
  python -X utf8 scripts/archive_session.py --session <sess_id> [--json]   # 归档（幂等）
  python -X utf8 scripts/archive_session.py --list [--all]                # 列会话及归档态
纪律：cron target 会话与当前管家会话永不归档；只归档显式传入的 id。
首次写前备份 .bak-archive-session。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

CLI_DB = os.environ.get("AI_STEWARD_CLI_DB") or os.path.join(
    os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
TASK_DB = os.environ.get("AI_STEWARD_TASK_DB") or os.path.join(
    os.path.expanduser("~"), ".zcode", "v2", "tasks-index.sqlite")


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=5)


def _backup(path):
    bak = path + ".bak-archive-session"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    return bak


def _find(session_id):
    """返回 [(store, path, row-dict)]；row-dict 含 id/title/archived。"""
    hits = []
    if os.path.exists(CLI_DB):
        try:
            con = _ro(CLI_DB)
            try:
                cur = con.cursor()
                cur.execute("SELECT id, title, time_archived, directory FROM session WHERE id=?",
                            (session_id,))
                r = cur.fetchone()
                if r:
                    hits.append(("cli", CLI_DB, {"id": r[0], "title": r[1],
                                                 "archived": bool(r[2]), "directory": r[3]}))
            finally:
                con.close()
        except sqlite3.Error:
            pass
    if os.path.exists(TASK_DB):
        try:
            con = _ro(TASK_DB)
            try:
                cur = con.cursor()
                cur.execute("SELECT task_id, title, archived, deleted, workspace_path FROM tasks "
                            "WHERE task_id=?", (session_id,))
                r = cur.fetchone()
                if r:
                    hits.append(("tasks", TASK_DB, {"id": r[0], "title": r[1], "archived": bool(r[2]),
                                                    "deleted": bool(r[3]), "directory": r[4]}))
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return hits


def archive(session_id, as_json=False):
    hits = _find(session_id)
    if not hits:
        out = {"ok": False, "session": session_id, "error": "会话不在任何会话库"}
        print(json.dumps(out, ensure_ascii=False) if as_json else out["error"])
        return 4
    now = int(time.time() * 1000)
    done, skipped = [], []
    for store, path, row in hits:
        if row.get("deleted"):
            skipped.append({"store": store, "reason": "已删除"})
            continue
        if row["archived"]:
            skipped.append({"store": store, "reason": "已归档"})
            continue
        _backup(path)
        con = sqlite3.connect(path, timeout=15)
        try:
            con.execute("PRAGMA busy_timeout=15000")
            if store == "cli":
                con.execute("UPDATE session SET time_archived=?, time_updated=? WHERE id=?",
                            (now, now, session_id))
            else:
                con.execute("UPDATE tasks SET archived=1, updated_at=? WHERE task_id=?",
                            (now, session_id))
            con.commit()
            done.append(store)
        finally:
            con.close()
    out = {"ok": True, "session": session_id, "archived_stores": done, "skipped": skipped,
           "hits": [h[0] for h in hits]}
    print(json.dumps(out, ensure_ascii=False, indent=1) if as_json else
          "已归档 %s（%s）" % (session_id, ",".join(done) if done else "无需变更"))
    return 0


def list_sessions(as_json=False):
    rows = []
    if os.path.exists(CLI_DB):
        con = _ro(CLI_DB)
        try:
            cur = con.cursor()
            cur.execute("SELECT id, title, time_archived, directory, time_updated FROM session "
                        "ORDER BY time_updated DESC")
            for r in cur.fetchall():
                rows.append({"store": "cli", "id": r[0], "title": r[1], "archived": bool(r[2]),
                             "directory": r[3], "updated_ms": r[4]})
        finally:
            con.close()
    if os.path.exists(TASK_DB):
        con = _ro(TASK_DB)
        try:
            cur = con.cursor()
            cur.execute("SELECT task_id, title, archived, deleted, workspace_path, updated_at "
                        "FROM tasks ORDER BY updated_at DESC")
            for r in cur.fetchall():
                rows.append({"store": "tasks", "id": r[0], "title": r[1], "archived": bool(r[2]),
                             "deleted": bool(r[3]), "directory": r[4], "updated_ms": r[5]})
        finally:
            con.close()
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            flag = "ARCHIVED" if r["archived"] else ("DELETED" if r.get("deleted") else "active")
            print("%s %-8s %s | %s" % (flag, r["store"], r["id"], (r["title"] or "")[:40]))
        print("共 %d 个会话（归档 %d）" % (len(rows), sum(1 for r in rows if r["archived"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="会话归档助手（SetSessionArchived 的本库实现）")
    ap.add_argument("--session", help="要归档的会话 id")
    ap.add_argument("--list", action="store_true", help="列会话及归档态")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.list:
        return list_sessions(args.json)
    if args.session:
        return archive(args.session, args.json)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
