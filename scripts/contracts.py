#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contracts.py — 主队列「下单即立约」合同层（C-20261125-G42 · F2.2）

用户语义：**下单即立合同，未拒单即有效**。

合同文件 = `state/contracts/<单号>.json`，字段口径（F2.2）：
    order_id / title / spec_file / kind / created_at / status:active
生命周期：enqueue 立约(active) → 接单记 started_at → 完工 status=done|failed + finished_at。

合同是**旁路证据，不是闸**：
  * 任何读写失败都返回 {}（调用方自己 trace），绝不抛异常打断 enqueue/tick/收口；
  * 老单（无合同文件）一律按「无约」处理——read 返回 {}，sync 绝不自造合同；
  * 合同根默认跟队列同目录（把 QUEUE_PATH 重定向到临时目录的测试/沙箱运行会连带把合同
    落进临时目录，不写生产 state）；env `STEWARD_CONTRACTS_DIR` 可显式覆盖。

本模块自包含（不 import factory_runner，避免内核 ↔ 合同层循环依赖）：
内核 `factory_runner.py` 只留三处挂点（enqueue 落账后 / 队列落账 running / 队列落账终态），
把 `queue_path` 传进来即可。
"""
from __future__ import annotations

import datetime as _dt
import json
import os

CONTRACT_STATUS_ACTIVE = "active"
CONTRACT_STATUS_TERMINAL = ("done", "failed")
# 队列终态口径（与内核 _LANE_TERMINAL 同值；本地复制一份，合同层可独立使用）
_QUEUE_TERMINAL = ("done", "failed", "needs_human", "infra_error")


def _iso_now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: str, data: dict) -> None:
    """原子写（tmp + os.replace）：读者永不见半成品。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def default_contracts_dir() -> str:
    """兜底合同根：<仓根>/state/contracts（= 内核 STEWARD_ROOT/state/contracts）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "state", "contracts")


def contracts_dir(queue_path: str = "") -> str:
    """合同根优先级：env STEWARD_CONTRACTS_DIR > 队列同目录/contracts > 仓根 state/contracts。"""
    env = os.environ.get("STEWARD_CONTRACTS_DIR")
    if env:
        return env
    qdir = os.path.dirname(str(queue_path or ""))
    return os.path.join(qdir, "contracts") if qdir else default_contracts_dir()


def contract_path(order_id: str, queue_path: str = "") -> str:
    return os.path.join(contracts_dir(queue_path), f"{order_id}.json")


def read_contract(order_id: str, queue_path: str = "") -> dict:
    """读合同；缺失/损坏返回 {}（老单无合同按无约处理，不报错）。"""
    oid = str(order_id or "").strip()
    if not oid:
        return {}
    p = contract_path(oid, queue_path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def update_contract(order_id: str, patch: dict, create: dict | None = None,
                    queue_path: str = "") -> dict:
    """合并写合同。文件不存在且给了 create 才新建。失败返回 {} 不抛（旁路证据）。"""
    oid = str(order_id or "").strip()
    if not oid:
        return {}
    try:
        cur = read_contract(oid, queue_path)
        if not cur:
            if create is None:
                return {}
            cur = {k: v for k, v in create.items() if v is not None}
        cur.update({k: v for k, v in (patch or {}).items() if v is not None})
        cur["order_id"] = oid
        cur["updated_at"] = _iso_now()
        _atomic_write_json(contract_path(oid, queue_path), cur)
        return cur
    except (OSError, ValueError):
        return {}


def ensure_contract(job: dict, queue_path: str = "") -> dict:
    """enqueue 即立约：status=active。同单号重排队保留签约时间 created_at，
    清掉上一轮开工/完工时刻（合同对象代表「这一轮的约」）。"""
    job = job if isinstance(job, dict) else {}
    oid = str(job.get("order_id") or "").strip()
    if not oid:
        return {}
    targets = list(job.get("targets") or [])
    one = str(job.get("target") or "").strip()
    if one and one not in targets:
        targets.insert(0, one)
    base = {
        "order_id": oid,
        "title": job.get("title") or "",
        "spec_file": job.get("spec_file") or "",
        "kind": job.get("kind") or "",
        "targets": targets,
        "status": CONTRACT_STATUS_ACTIVE,
        "started_at": "",
        "finished_at": "",
        "rejects": 0,
    }
    prev = read_contract(oid, queue_path)
    if prev.get("created_at"):
        base["created_at"] = prev["created_at"]
        base["rejects"] = int(prev.get("rejects") or 0)
    else:
        base["created_at"] = job.get("enqueued_at") or _iso_now()
    return update_contract(oid, base, create=base, queue_path=queue_path)


def mark_contract_rejected(order_id: str, reason: str = "", queue_path: str = "") -> dict:
    """发起人 24h 打回：合同记 rejects+1（合同本身不因打回失效，返工单另立新约）。"""
    cur = read_contract(order_id, queue_path)
    if not cur:
        return {}
    return update_contract(order_id, {
        "rejects": int(cur.get("rejects") or 0) + 1,
        "last_reject_at": _iso_now(),
        "last_reject_reason": str(reason or "")[:200],
    }, queue_path=queue_path)


def sync_contract_for_job(job: dict, queue_path: str = "") -> dict:
    """队列落账尾钩：running→记 started_at（开工）；终态→status done|failed + finished_at（完工）。
    只同步「已立约」的单；无合同的单（老单/外派单）绝不自造合同。"""
    job = job if isinstance(job, dict) else {}
    oid = str(job.get("order_id") or "").strip()
    if not oid:
        return {}
    cur = read_contract(oid, queue_path)
    if not cur:
        return {}
    st = str(job.get("status") or "")
    if st == "running":
        if not cur.get("started_at"):
            return update_contract(oid, {"started_at": job.get("started_at") or _iso_now(),
                                         "status": CONTRACT_STATUS_ACTIVE}, queue_path=queue_path)
        return cur
    if st in _QUEUE_TERMINAL:
        patch = {
            "status": "done" if st == "done" else "failed",
            "final_status": st,
            "finished_at": job.get("finished_at") or _iso_now(),
        }
        if job.get("needs_reason"):
            patch["needs_reason"] = job["needs_reason"]
        return update_contract(oid, patch, queue_path=queue_path)
    return cur


def contract_mark(order_id: str, queue_path: str = "") -> dict:
    """看板「约」标（F2.3 数据面）：无约返回 has=False，绝不抛错。"""
    cur = read_contract(order_id, queue_path)
    if not cur:
        return {"has": False, "status": "", "created_at": "", "started_at": "",
                "finished_at": "", "label": ""}
    return {
        "has": True,
        "status": str(cur.get("status") or ""),
        "created_at": str(cur.get("created_at") or ""),
        "started_at": str(cur.get("started_at") or ""),
        "finished_at": str(cur.get("finished_at") or ""),
        "label": "约",
    }
