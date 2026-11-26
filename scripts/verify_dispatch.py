# -*- coding: utf-8 -*-
"""派单后 45 秒自检：worker 会话是否真的跑起来、在 UI 里可见（INC-20261118-007）。

用法（dispatcher_prompt §1 第 4b 步，派单/claim 之后立刻跑）：
  python -X utf8 scripts/verify_dispatch.py --id W-20261118-021 --session sess_xxx --grace 45

退出码：
  0  healthy   tasks 行存在且 assistant 有 token 输出，且最近输出未超 --max-idle
  2  ghost     会话没跑起来（UI 也看不到）-> 回 queued 重派，别等下一轮
  4  stalled   有历史输出但最近一条消息超 --max-idle 秒（无转圈）-> 先 nudge，无果 replace
  3  unknown   本地库读不到 / 参数不符 -> 记日报，改用 ReadSession 兜底

轮询间隔默认 5 秒；--grace 0 表示只查一次。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import session_probe  # noqa: E402

DEFAULT_REGISTRY = os.path.join(os.path.dirname(HERE), "state", "registry.json")


def card_session(registry: str, task_id: str) -> tuple[str | None, str | None]:
    """返回 (卡片上的 sessionId, 错误说明)。"""
    try:
        data = json.load(open(registry, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "registry: %s" % exc
    for task in data.get("tasks") or []:
        if isinstance(task, dict) and str(task.get("id")) == task_id:
            return str(task.get("sessionId") or "") or None, None
    return None, "task-not-found"


def run(task_id: str, session_id: str, grace: float, interval: float,
        registry: str, cli_db: str | None, tasks_db: str | None,
        max_idle: float = 180.0) -> dict:
    deadline = time.monotonic() + max(0.0, grace)
    info = session_probe.probe(session_id, cli_db, tasks_db)
    verdict = session_probe.verdict(info)
    while verdict != "healthy" and verdict != "unknown" and time.monotonic() < deadline:
        left = deadline - time.monotonic()
        time.sleep(min(interval, max(0.2, left)))
        info = session_probe.probe(session_id, cli_db, tasks_db)
        verdict = session_probe.verdict(info)
    # 转圈判据（用户令 2026-11-18）：healthy 但最近一条消息已超 max_idle 秒，
    # UI 会退回蓝点/无转圈；按“疑似停滞”处理，不解释、不等待。
    if verdict == "healthy" and info.get("idle_sec") is not None and info["idle_sec"] > max_idle:
        verdict = "stalled"

    card_sid, card_error = card_session(registry, task_id)
    mismatched = bool(card_sid and card_sid != session_id)
    if mismatched and verdict == "unknown":
        verdict = "unknown"
    return {
        "task_id": task_id,
        "session_id": session_id,
        "verdict": verdict,
        "grace_sec": grace,
        "max_idle_sec": max_idle,
        "registry_session": card_sid,
        "registry_error": card_error,
        "session_mismatch": mismatched,
        "probe": info,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="派单后会话健康自检（只读）")
    ap.add_argument("--id", required=True, help="任务卡 ID，如 W-20261118-021")
    ap.add_argument("--session", required=True, help="CreateSession 返回的 sessionId")
    ap.add_argument("--grace", type=float, default=45.0, help="最长等待秒数（默认 45）")
    ap.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒（默认 5）")
    ap.add_argument("--max-idle", type=float, default=180.0,
                    help="healthy 但最近输出超过该秒数 -> stalled（默认 180）")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--cli-db", default=None)
    ap.add_argument("--tasks-db", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out = run(args.id, args.session, args.grace, args.interval,
              args.registry, args.cli_db, args.tasks_db, args.max_idle)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        p = out["probe"]
        print("VERDICT %-7s %s session=%s" % (out["verdict"], args.id, args.session))
        print("  probe tasks_row=%s messages=%s assistant=%s assistant_with_output=%s idle=%ss error=%s"
              % (p.get("tasks_row"), p.get("messages"), p.get("assistant"),
                 p.get("assistant_with_output"), p.get("idle_sec"), p.get("error") or "-"))
        if out["registry_error"]:
            print("  registry=%s" % out["registry_error"])
        if out["session_mismatch"]:
            print("  WARN 卡片 session=%s 与传入不一致（以传入为准，需回写台账）"
                  % out["registry_session"])
        if out["verdict"] == "ghost":
            print("  处置：board state --state queued --force --note \"派单幽灵：无 tasks 行/无输出\" 后重派")
        elif out["verdict"] == "stalled":
            print("  处置：疑似停滞（无转圈/最近输出超 %.0fs）：先 nudge，仍无输出则 replace"
                  % args.max_idle)
        elif out["verdict"] == "unknown":
            print("  处置：库不可读，改用 ReadSession(view=summary) 兜底并记日报")
    return 0 if out["verdict"] == "healthy" else (
        2 if out["verdict"] == "ghost" else (4 if out["verdict"] == "stalled" else 3))


if __name__ == "__main__":
    raise SystemExit(main())
