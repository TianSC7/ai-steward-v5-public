#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_locks.py — 管家资源锁（单机段）：TTL 默认 30 分钟，过期可抢占。

治「编译互相杀进程、截图互相抢窗口」的根：note 喊话靠自觉，锁才是机制。调用方凡
杀进程/编译/替换/重启应先 acquire("build")；驱动主窗口等独占资源可自定义资源名。

语义：
  - 锁记录落 <STEWARD_STATE_DIR>/locks/<resource>.json（原子写），互斥由
    ai_store.file_lock 保证；不做跨机协调（P0 只做单机段）。
  - TTL 默认 30 分钟：到期后其它持有者可自动接管；正常持有方应定期 extend。
  - owner 为自由标识（会话 id / outside-<pid> / 进程名），限安全字符，幂等重入刷新 TTL。
  - release_all_for(owner) 供会话收尾时一次性释放自己的全部锁。
  - 过期锁在 list_locks() 时惰性清理；state() 只标注 expired，不做删除。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import ai_store
from ai_store import atomic_write_json, file_lock, iso_now, load_json, now

RES_RE = re.compile(r"[a-z][a-z0-9_]{0,23}")        # 资源名：build / display / 自定义小写
OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
DEFAULT_TTL_MIN = 30
MAX_TTL_MIN = 240
LOCK_GROUP = "locks"                                 # 互斥组名（所有资源共用一把进程锁）


def _clamp_ttl(ttl_min: int) -> int:
    return max(1, min(int(ttl_min), MAX_TTL_MIN))


def _lock_path(resource: str) -> Path:
    if not RES_RE.fullmatch(resource or ""):
        raise ValueError(f"非法资源名「{resource}」：限小写字母开头的 [a-z0-9_]{1,24}，如 build / display")
    return ai_store.state_dir() / "locks" / f"{resource}.json"


def _check_owner(owner: str) -> str:
    owner = str(owner or "").strip()
    if not OWNER_RE.fullmatch(owner):
        raise ValueError(f"非法 owner「{owner}」：限字母数字开头、[A-Za-z0-9._:-] 共 64 字符内")
    return owner


def state(resource: str) -> dict | None:
    """读单个资源锁现状（含剩余秒数；过期不做清理，仅标注 expired）。"""
    p = _lock_path(resource)
    rec = load_json(p)
    if not isinstance(rec, dict):
        return None
    rec = dict(rec)
    rec["remaining_sec"] = int(float(rec.get("expires_at", 0)) - now())
    rec["expired"] = rec["remaining_sec"] <= 0
    return rec


def acquire(owner: str, resource: str, ttl_min: int = DEFAULT_TTL_MIN,
            wait_sec: int = 0, note: str = "", force: bool = False,
            poll_sec: float = 0.2) -> dict:
    """抢锁；被占则每 poll_sec 重试，直到 wait_sec 用尽。返回 {success, ...}。

    可取情形：不存在 / 自己已持有（幂等刷新 TTL）/ 已过期（过期抢占）/ force。
    """
    owner = _check_owner(owner)
    p = _lock_path(resource)
    ttl = _clamp_ttl(ttl_min)
    deadline = now() + max(0, wait_sec)

    while True:
        outcome: dict | None = None
        with file_lock(LOCK_GROUP):
            cur = load_json(p)
            taken, stolen_from = False, ""
            if not isinstance(cur, dict):
                taken = True
            else:
                remaining = float(cur.get("expires_at", 0)) - now()
                if cur.get("owner") == owner:
                    taken = True  # 幂等重入：刷新 TTL
                elif remaining <= 0:
                    taken, stolen_from = True, str(cur.get("owner", "?"))  # 过期抢占
                elif force:
                    taken, stolen_from = True, str(cur.get("owner", "?"))  # 人工强制接管
                else:
                    outcome = {"success": False, "resource": resource,
                               "holder": cur.get("owner"),
                               "holder_note": cur.get("note", ""),
                               "expires_at": cur.get("expires_at"),
                               "remaining_min": max(1, int(remaining / 60))}
            if taken:
                rec = {"resource": resource, "owner": owner,
                       "note": note.strip(), "acquired_at": iso_now(),
                       "expires_at": now() + ttl * 60, "stolen_from": stolen_from}
                atomic_write_json(p, rec)
                outcome = {"success": True, "resource": resource, "owner": owner,
                           "ttl_min": ttl, "expires_at": rec["expires_at"],
                           "stolen_from": stolen_from}
        if outcome is not None:
            return outcome
        if now() >= deadline:
            return {"success": False, "resource": resource, "holder": "?", "remaining_min": 0}
        time.sleep(poll_sec)


def release(owner: str, resource: str, force: bool = False) -> dict:
    """释放锁；非持有者无权释放（force 例外，留给人工兜底）。"""
    p = _lock_path(resource)
    with file_lock(LOCK_GROUP):
        cur = load_json(p)
        if not isinstance(cur, dict):
            return {"success": True, "released": False, "resource": resource}
        if cur.get("owner") != owner and not force:
            return {"success": False,
                    "error": f"{resource} 锁的持有者是 {cur.get('owner')}，{owner} 无权释放（确需强制加 force=True）"}
        holder = cur.get("owner")
        p.unlink(missing_ok=True)
    return {"success": True, "released": True, "resource": resource, "holder": holder}


def extend(owner: str, resource: str, ttl_min: int = DEFAULT_TTL_MIN) -> dict:
    """续期：只有当前持有者可以延长，期满时间从当前时刻重新计算。"""
    p = _lock_path(resource)
    ttl = _clamp_ttl(ttl_min)
    with file_lock(LOCK_GROUP):
        cur = load_json(p)
        if not isinstance(cur, dict):
            return {"success": False, "error": f"{resource} 锁不存在"}
        if cur.get("owner") != owner:
            return {"success": False,
                    "error": f"{resource} 锁的持有者是 {cur.get('owner')}，{owner} 无权续期"}
        cur["expires_at"] = now() + ttl * 60
        cur["extended_at"] = iso_now()
        atomic_write_json(p, cur)
    return {"success": True, "resource": resource, "owner": owner, "expires_at": cur["expires_at"]}


def list_locks() -> list[dict]:
    """列出有效锁（过期锁顺带清理，不返回）。"""
    folder = ai_store.state_dir() / "locks"
    out: list[dict] = []
    if not folder.exists():
        return out
    with file_lock(LOCK_GROUP):
        for p in sorted(folder.glob("*.json")):
            rec = load_json(p)
            if not isinstance(rec, dict):
                continue
            remaining = float(rec.get("expires_at", 0)) - now()
            if remaining <= 0:
                p.unlink(missing_ok=True)  # 过期锁惰性清理
                continue
            out.append({"resource": rec.get("resource", p.stem),
                        "owner": rec.get("owner"),
                        "note": rec.get("note", ""),
                        "remaining_min": max(1, int(remaining / 60)),
                        "acquired_at": rec.get("acquired_at", "")})
    return out


def release_all_for(owner: str) -> list[str]:
    """会话收尾调用：释放该 owner 持有的全部锁，返回被释放的资源名。"""
    released: list[str] = []
    for rec in list_locks():
        if rec.get("owner") == owner:
            r = release(owner, rec["resource"])
            if r.get("success") and r.get("released"):
                released.append(rec["resource"])
    return released
