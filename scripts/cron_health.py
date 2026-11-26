# -*- coding: utf-8 -*-
"""定时任务健康检查（只读）：确认调度台巡检 cron 真的还在路上。

背景：巡检靠 ZCode 自动化（`~/.zcode/v2/tasks-index.sqlite` 的 automations 表）。
自动化会因禁用、dispatch 失败、目标会话被删、next_run_at 过期而静默停摆——
此时"调度台看起来 idle"但永远不会再巡检。这个脚本把这种静默失败变成可判定结果。

用法：
  python -X utf8 cron_health.py --id automation-xxx
  python -X utf8 cron_health.py --from-registry
  python -X utf8 cron_health.py --from-registry --json
  python -X utf8 cron_health.py --id automation-xxx --grace-min 5 --strict
  python -X utf8 cron_health.py --db <temp.sqlite> --id a1 --now 1794929000000   # 测试

状态：
  OK         enabled + active + 无 last_error + next_run_at 未过期（允许 grace）
  STALE      next_run_at 已过期超过 grace（默认 5 分钟）
  DISABLED   enabled=0 或 lifecycle_status 不是 active
  ERROR      last_error 非空，或 dispatch_status 为 failed/error
  NO-NEXT    next_run_at 为空（不会再有下一跑）
  NOTFOUND   库里没有这个 automation_id
  TARGET-MISMATCH  --from-registry 时 automation 的目标会话 != registry.dispatcher.sessionId

退出码：0 全部 OK；3 有非 OK；2 库/参数问题。
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
DEFAULT_DB = os.path.expanduser("~/.zcode/v2/tasks-index.sqlite")
DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")
FIELDS = ("automation_id, title, cron_expr, enabled, lifecycle_status, next_run_at, "
          "last_run_at, run_count, running, dispatch_status, last_error, target_task_id")


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


def _registry_ids(path: str) -> tuple[str, str]:
    """返回 (cron_id, dispatcher_sessionId)；读不到就抛 ValueError。"""
    data = json.load(open(path, encoding="utf-8"))
    disp = data.get("dispatcher") or {}
    cron_id = str(disp.get("cron_id") or "")
    session_id = str(disp.get("sessionId") or "")
    if not cron_id:
        raise ValueError("registry.dispatcher.cron_id 为空")
    return cron_id, session_id


def check_one(row: dict, now_ms: int, grace_min: float) -> dict:
    aid = row.get("automation_id") or "?"
    status = "OK"
    detail = ""
    enabled = int(row.get("enabled") or 0)
    lifecycle = str(row.get("lifecycle_status") or "")
    err = str(row.get("last_error") or "")
    dispatch = str(row.get("dispatch_status") or "")
    next_ms = row.get("next_run_at")
    lag_min = None
    if next_ms:
        lag_min = round((now_ms - int(next_ms)) / 60000.0, 1)

    if not enabled or lifecycle != "active":
        status, detail = "DISABLED", "enabled=%s lifecycle=%s" % (enabled, lifecycle or "-")
    elif err or dispatch in ("failed", "error"):
        status, detail = "ERROR", (err or ("dispatch=" + dispatch))[:160]
    elif not next_ms:
        status, detail = "NO-NEXT", "next_run_at 为空"
    elif lag_min is not None and lag_min > grace_min:
        status, detail = "STALE", "已过期 %.1f 分钟（grace %.0f）" % (lag_min, grace_min)

    return {
        "id": aid,
        "title": row.get("title") or "",
        "status": status,
        "detail": detail,
        "cron": row.get("cron_expr") or "",
        "enabled": enabled,
        "lifecycle": lifecycle,
        "next_run_at": _fmt_ms(next_ms),
        "last_run_at": _fmt_ms(row.get("last_run_at")),
        "run_count": row.get("run_count"),
        "running": int(row.get("running") or 0),
        "dispatch_status": dispatch,
        "last_error": err,
        "lag_min": lag_min,
        "target": row.get("target_task_id") or "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", action="append", default=[], help="automation id，可重复")
    ap.add_argument("--from-registry", action="store_true", help="读取 registry.dispatcher.cron_id")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--grace-min", type=float, default=5.0)
    ap.add_argument("--now", default=None, help="测试用：epoch 毫秒或 ISO 时间")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="非 OK 时退出 3（默认也退出 3，保留参数兼容）")
    args = ap.parse_args()

    now_ms = _parse_now(args.now)
    ids = list(args.id)
    expected_target = ""
    if args.from_registry:
        try:
            cron_id, expected_target = _registry_ids(args.registry)
        except (OSError, ValueError) as e:
            print("REGISTRY-ERROR %s" % e)
            return 2
        ids.append(cron_id)
    if not ids:
        print("用法错误：至少给 --id 或 --from-registry")
        return 2

    if not os.path.isfile(args.db):
        print("DB-MISSING %s" % args.db)
        return 2
    try:
        con = sqlite3.connect(_ro_uri(args.db), uri=True, timeout=5)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        print("DB-ERROR %s" % e)
        return 2

    results: list[dict] = []
    try:
        for aid in dict.fromkeys(ids):
            row = con.execute("select %s from automations where automation_id=?" % FIELDS,
                              (aid,)).fetchone()
            if row is None:
                results.append({"id": aid, "status": "NOTFOUND", "detail": "automations 表无此 id",
                                "title": "", "next_run_at": "-", "last_run_at": "-",
                                "run_count": None, "target": ""})
                continue
            item = check_one(dict(row), now_ms, args.grace_min)
            if expected_target and item.get("target") and item["target"] != expected_target:
                item["status"] = "TARGET-MISMATCH"
                item["detail"] = "target=%s != registry=%s" % (item["target"], expected_target)
            results.append(item)
    finally:
        con.close()

    ok = all(r["status"] == "OK" for r in results)
    if args.json:
        print(json.dumps({"ok": ok, "checked_at": _fmt_ms(now_ms), "results": results},
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            print("CRON %s %s next=%s last=%s run=%s lag=%s target=%s%s" % (
                r["id"], r["status"], r.get("next_run_at", "-"), r.get("last_run_at", "-"),
                r.get("run_count"), ("%sm" % r["lag_min"]) if r.get("lag_min") is not None else "-",
                r.get("target") or "-",
                (" | " + r["detail"]) if r.get("detail") else ""))
        print("HEALTH %s (%d checked)" % ("OK" if ok else "ATTENTION", len(results)))
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
