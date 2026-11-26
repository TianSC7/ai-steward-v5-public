# -*- coding: utf-8 -*-
"""产线单抽屉、均速剔除、施工时间线。缺字段就留空，不补造。"""
import glob
import json
import os
from datetime import datetime


def tasks_from_ledger(ledger: dict) -> list[dict]:
    gate = ledger.get("gate") if isinstance(ledger.get("gate"), dict) else {}
    checks = gate.get("checks") if isinstance(gate.get("checks"), dict) else {}
    tasks = []
    for index, (name, row) in enumerate(checks.items(), 1):
        info = row if isinstance(row, dict) else {}
        ok = bool(info.get("ok"))
        detail = str(info.get("detail") or "").strip()
        status = "通过" if ok else "未通过"
        if detail:
            status = f"{status} · {detail}"
        tasks.append({
            "id": f"G{index:02d}",
            "name": str(name),
            "status": status,
            "owner": "产线机检",
        })
    verdict = str(gate.get("verdict") or "").strip()
    if not tasks and verdict:
        tasks.append({"id": "G00", "name": "产线门禁", "status": verdict, "owner": "产线机检"})
    return tasks


def chat_from_ledger(ledger: dict, finished_at: str = "") -> list[dict]:
    chat = []
    created = str(ledger.get("created_at") or "").strip()
    if created:
        chat.append({"ts": created, "who": "产线", "msg": "产线开工"})
    status = str(ledger.get("status") or "").lower()
    gate = ledger.get("gate") if isinstance(ledger.get("gate"), dict) else {}
    verdict = str(gate.get("verdict") or "").strip()
    if finished_at and status in ("done", "delivered", "failed"):
        msg = "产线完工" if not verdict else f"产线完工 · {verdict}"
        chat.append({"ts": finished_at, "who": "产线", "msg": msg})
    return chat


def detail_from_ledger(ledger: dict, finished_at: str = "") -> dict:
    order_id = str(ledger.get("order_id") or "")
    return {
        "code": order_id,
        "title": order_id,
        "ordered_at": str(ledger.get("created_at") or ""),
        "tasks": tasks_from_ledger(ledger),
        "group_chat": chat_from_ledger(ledger, finished_at),
    }


def scan_line_details(lines_root: str) -> dict:
    root = os.path.abspath(lines_root)
    found = {}
    if not os.path.isdir(root):
        return found
    pattern = os.path.join(root, "*", "jobs", "*", "ledger.json")
    for led_path in glob.glob(pattern):
        try:
            ledger = json.load(open(led_path, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(ledger, dict):
            continue
        order_id = str(ledger.get("order_id") or os.path.basename(os.path.dirname(led_path)))
        ledger = dict(ledger)
        ledger["order_id"] = order_id
        finished = ""
        if str(ledger.get("status") or "").lower() in ("done", "delivered", "failed"):
            finished = datetime.fromtimestamp(os.path.getmtime(led_path)).strftime("%Y-%m-%d %H:%M:%S")
        found[order_id] = detail_from_ledger(ledger, finished)
    return found


def factory_avg_sec(done_runs, line_ids) -> float:
    banned = {str(item) for item in (line_ids or [])}
    samples = []
    for row in done_runs or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("order_id") or "") in banned:
            continue
        raw = row.get("e2e_total_sec")
        if raw in (None, "", 0, 0.0):
            continue
        try:
            samples.append(float(raw))
        except (TypeError, ValueError):
            continue
    samples = samples[-5:]
    if not samples:
        return 0.0
    return sum(samples) / float(len(samples))


def events_to_chat(order_id: str, lines) -> list[dict]:
    chat = []
    for line in lines or []:
        try:
            event = json.loads(line)
        except Exception:
            continue
        if str(event.get("order") or event.get("order_id") or "") != str(order_id):
            continue
        summary = str(event.get("summary") or "").strip()
        if not summary:
            continue
        chat.append({
            "ts": str(event.get("ts") or ""),
            "who": str(event.get("actor") or "内核"),
            "msg": summary,
        })
    return chat
