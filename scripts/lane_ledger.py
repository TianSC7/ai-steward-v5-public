#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lane_ledger.py — 并发账本内核（C-20261120-003 · A 块）

治发起人问的三件事：**并发用完了没（可查）/ 会不会重复占格（幂等）/ 满了还硬塞（超限拒绝）**。
现状口径是「近 N 分钟有 model_usage 的会话数」（`slot_pool.py:8-13`）——那是事后观测，
不是占用；本模块做的是**先占后跑**的写账。

文件布局（全部经 `ai_store.state_dir()`，路径不经 `PLUGIN_ROOT`，故不随 worktree 漂）
  - `lane_ledger.json`   主账/权威快照：`{version, seq, updated_at, slots{}, waitlist{},
                         waitlist_overflow{}}`（`waitlist_overflow` 为防膨胀的附加计数位）
  - `lane_ledger.jsonl`  审计流水（只追加）：INIT/CLAIM/REPLAY/BIND/HEARTBEAT/RELEASE/
                         EXPIRE/REJECT/DEGRADED_ADMIT/ORPHAN
  - `lane_caps.json`     lane 上限单一真相源（入 git；测试用 `--caps <path>` 或显式 copy）

不变式（与 plan §C1–C3 逐条对齐）
  1. 一切读改写整体在 `ai_store.file_lock(lock_name_for(ledger))` 内 —— 「读-判-写」是
     「不重复占格」的唯一线性化点；写走 `atomic_write_json`（读者永不见半成品）。
  2. 计数口径 `used(lane) = count(slots where lane==L and state in {claimed, leased})`；
     `released`/`expired` 不计、记录保留（防证据自毁）。
  3. 回收**惰性**：每个入口先 reap（实测无常驻采样器，今晚不能新起守护）。
  4. 初始化判定序：**缺失 ≠ 损坏** —— 缺失→锁内建空账 + INIT 流水并继续；
     损坏→复制 `.corrupt-<stamp>` 旁路副本 + 不可用（exit 4），不覆盖不重建；version 高→不可用。
  5. fail-closed：账本不可用即拒绝；`force_degraded` 是唯一出口，且**只对
     LEDGER_UNAVAILABLE 生效**，绝不越过 LANE_FULL / SESSION_BUSY / LEASE_EXPIRED。

状态根（plan §C0 + N7 纵深防御）：import 时即 `setdefault` 生产绝对根，禁止无 env 时静默
落 `PLUGIN_ROOT/state`；`check` 的 DRIFT 段比对「实际根 vs 生产绝对根」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

# N7：必须在 import ai_store 之前钉住状态根（ai_store 在 import 时不缓存，但纵深防御）
DEFAULT_STATE_ROOT = "C:/Users/Administrator/Documents/mate/plugins/ai-steward/state"
os.environ.setdefault("STEWARD_STATE_DIR", DEFAULT_STATE_ROOT)

import ai_store  # noqa: E402  （刻意放在 setdefault 之后）
from ai_store import atomic_write_json, file_lock, iso_now, load_json, now  # noqa: E402

LEDGER_VERSION = 1
SUPPORTED_MAX_VERSION = 1
LOCK_TIMEOUT = 10.0

LANES = ("R4", "R4-2", "STEP", "NEX", "GLM", "OUTSOURCE")
TTL_RESERVE = 300
TTL_LEASE = 900
HEARTBEAT_HINT = 300
WAITLIST_MAX_PER_LANE = 50

ACTIVE_STATES = ("claimed", "leased")
TERMINAL_STATES = ("released", "expired")

LEDGER_NAME = "lane_ledger.json"
JOURNAL_NAME = "lane_ledger.jsonl"
CAPS_NAME = "lane_caps.json"


# ── 异常（→ CLI 退出码契约 0/2/3/4）──────────────────────────────────────────
class LedgerError(Exception):
    """账本层异常的基类；CLI 按 `exit_code` 映射进程退出码。"""

    code = "LEDGER_ERROR"
    exit_code = 4

    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason = str(reason)
        self.extra = dict(extra)

    def to_dict(self) -> dict:
        d = {"ok": False, "code": self.code, "reason": self.reason}
        d.update(self.extra)
        return d


class UsageError(LedgerError):
    """用法/参数错（exit 2）。"""

    code = "USAGE"
    exit_code = 2


class Reject(LedgerError):
    """业务拒绝（exit 3）：LANE_FULL / SESSION_BUSY / LEASE_EXPIRED / NOT_OWNER / BAD_STATE / ORPHAN。"""

    code = "REJECT"
    exit_code = 3

    def __init__(self, code: str, reason: str, **extra):
        super().__init__(reason, **extra)
        self.code = code


class Unavailable(LedgerError):
    """账本不可用（exit 4）：读不出/写不进/锁超时/损坏/version 过高/caps 缺失。"""

    code = "LEDGER_UNAVAILABLE"
    exit_code = 4


# ── 路径 ────────────────────────────────────────────────────────────────────
def state_root() -> Path:
    """账本实际根（唯一来源＝ai_store.state_dir()，env-first）。"""
    return ai_store.state_dir()


def expected_root() -> Path:
    """生产绝对根（DRIFT 比对基准）。"""
    return Path(DEFAULT_STATE_ROOT).expanduser().resolve()


def ledger_path() -> Path:
    return state_root() / LEDGER_NAME


def journal_path() -> Path:
    return state_root() / JOURNAL_NAME


def caps_path(caps_file: str | os.PathLike | None = None) -> Path:
    return Path(caps_file).expanduser().resolve() if caps_file else state_root() / CAPS_NAME


# ── 键与序号 ─────────────────────────────────────────────────────────────────
def idem_key_for(lane: str, seat: str, session_id: str, command_id: str) -> str:
    """幂等键 `sha1(lane|seat|session_id|command_id)[:16]`（bind 后确定）。"""
    raw = f"{lane}|{seat}|{session_id}|{command_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def prep_key_for(lane: str, seat: str, order_id: str, prep_id: str) -> str:
    """reserve 阶段临时键 `sha1(lane|seat|order_id|prep_id)[:16]`。"""
    raw = f"{lane}|{seat}|{order_id}|{prep_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def normalize_lane(lane: str) -> str:
    lane = str(lane or "").strip()
    if lane not in LANES:
        raise UsageError(f"非法 lane「{lane}」：限 {'/'.join(LANES)}", lane=lane)
    return lane


def normalize_priority(priority: str) -> str:
    p = str(priority or "P1").strip().upper()
    return p if p in ("P0", "P1", "P2") else "P1"


def _next_slot_id(led: dict, lane: str) -> str:
    """`<lane>-<3 位序号>`：取同 lane 历史最大序号 +1，不回收复用（防指认错位）。"""
    top = 0
    for sid, slot in (led.get("slots") or {}).items():
        if not isinstance(slot, dict) or slot.get("lane") != lane:
            continue
        tail = str(sid).rsplit("-", 1)[-1]
        if tail.isdigit():
            top = max(top, int(tail))
    return f"{lane}-{top + 1:03d}"


# ── caps ────────────────────────────────────────────────────────────────────
def load_caps(caps_file: str | os.PathLike | None = None) -> dict:
    """读 lane 上限。缺失/损坏一律不可用（exit 4）——不猜默认值（R1）。"""
    p = caps_path(caps_file)
    data = load_json(p)
    if not isinstance(data, dict) or not isinstance(data.get("caps"), dict) or not data["caps"]:
        raise Unavailable(
            f"caps 缺失或不可读：期望 {p} 或 --caps <path>"
            f"（caps 是 lane 上限单一真相源，缺失时 fail-closed，不猜默认值）"
        )
    caps: dict[str, int] = {}
    for lane, cap in data["caps"].items():
        lane = str(lane).strip()
        if lane not in LANES:
            continue
        try:
            ival = int(cap)
        except (TypeError, ValueError):
            raise Unavailable(f"caps 非法：lane {lane} 的 cap={cap!r} 不是整数")
        if ival <= 0:
            raise Unavailable(f"caps 非法：lane {lane} 的 cap={ival} 必须 > 0")
        caps[lane] = ival
    if not caps:
        raise Unavailable(f"caps 缺失或不可读：期望 {p} 或 --caps <path>（无任何合法 lane）")
    return caps


def _code_caps() -> dict:
    """只读解析 `slot_pool.py` 的 DEFAULT_CAP（不 import，避免其副作用）。"""
    p = Path(__file__).resolve().parent / "slot_pool.py"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.search(r"DEFAULT_CAP\s*=\s*\{(.*?)\}", text, re.S)
    if not m:
        return {}
    out: dict[str, int] = {}
    for k, v in re.findall(r"[\"']([A-Za-z0-9_-]+)[\"']\s*:\s*(\d+)", m.group(1)):
        out[k] = int(v)
    return out


def caps_drift(caps: dict) -> list[str]:
    """caps（正典）vs 代码 DEFAULT_CAP 的口径漂移；只告警不改代码（plan §E2 注记）。"""
    code = _code_caps()
    out = []
    for lane, cap in sorted(caps.items()):
        if lane in code and code[lane] != cap:
            out.append(f"{lane}: lane_caps={cap} slot_pool.DEFAULT_CAP={code[lane]}")
    return out


# ── 流水 / 主账读写（全部在锁内调用）────────────────────────────────────────
def _journal(entry: dict) -> None:
    """追加一行审计流水。单写者由主锁保证；fsync 后再算落盘。"""
    rec = {"ts": iso_now(), "ts_unix": round(now(), 3)}
    rec.update(entry)
    p = journal_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _new_ledger() -> dict:
    return {"version": LEDGER_VERSION, "seq": 0, "updated_at": iso_now(),
            "slots": {}, "waitlist": {}, "waitlist_overflow": {}}


def _save_locked(led: dict) -> None:
    led["seq"] = int(led.get("seq") or 0) + 1
    led["updated_at"] = iso_now()
    atomic_write_json(ledger_path(), led)


def _load_locked() -> dict:
    """锁内取主账；判序：缺失→建账+INIT 并继续 ＞ 损坏→.corrupt-* + 不可用 ＞ version 高→不可用。"""
    p = ledger_path()
    try:
        exists = p.exists()
    except OSError as exc:
        raise Unavailable(f"账本路径不可用：{p}（{exc}）")

    if not exists:
        led = _new_ledger()
        _save_locked(led)
        _journal({"event": "INIT", "root": str(state_root()), "pid": os.getpid(),
                  "version": LEDGER_VERSION})
        return led

    data = load_json(p)
    if not isinstance(data, dict):
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        side = p.with_name(f"{p.name}.corrupt-{stamp}")
        kept = ""
        try:
            shutil.copy2(p, side)
            kept = str(side)
        except OSError:
            kept = "(旁路副本失败：无写权限)"
        raise Unavailable(
            f"账本损坏（无法解析或顶层非对象）：{p}；已留旁路副本 {kept}；"
            f"拒绝覆盖重建（防一次写把坏账洗成空账）"
        )

    ver = data.get("version")
    if not isinstance(ver, int):
        raise Unavailable(f"账本 version 字段缺失/非整数：{ver!r}（{p}）")
    if ver > SUPPORTED_MAX_VERSION:
        raise Unavailable(f"账本 version={ver} 高于本代码支持 {SUPPORTED_MAX_VERSION}；不猜新格式")

    data.setdefault("slots", {})
    data.setdefault("waitlist", {})
    data.setdefault("waitlist_overflow", {})
    data.setdefault("seq", 0)
    if not isinstance(data["slots"], dict):
        raise Unavailable(f"账本 slots 段非对象：{type(data['slots']).__name__}")
    if not isinstance(data["waitlist"], dict):
        data["waitlist"] = {}
    if not isinstance(data["waitlist_overflow"], dict):
        data["waitlist_overflow"] = {}
    return data


# ── 计数 / 回收 ─────────────────────────────────────────────────────────────
def used_of(led: dict, lane: str) -> int:
    return sum(1 for s in (led.get("slots") or {}).values()
               if isinstance(s, dict) and s.get("lane") == lane and s.get("state") in ACTIVE_STATES)


def _active_slots(led: dict) -> list[dict]:
    return [s for s in (led.get("slots") or {}).values()
            if isinstance(s, dict) and s.get("state") in ACTIVE_STATES]


def _reap_locked(led: dict) -> list[dict]:
    """惰性回收：租约到期 → expired（reserve 阶段记 reserve_timeout），并留 EXPIRE 流水。"""
    t = now()
    expired: list[dict] = []
    for slot in (led.get("slots") or {}).values():
        if not isinstance(slot, dict) or slot.get("state") not in ACTIVE_STATES:
            continue
        try:
            until = float(slot.get("lease_until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        if until > t:
            continue
        meta = slot.get("meta") if isinstance(slot.get("meta"), dict) else {}
        slot["state"] = "expired"
        slot["expired_at"] = iso_now()
        slot["expired_reason"] = "reserve_timeout" if meta.get("phase") == "reserve" else "lease_timeout"
        slot["expired_over_sec"] = round(t - until, 1)
        expired.append(slot)
    if expired:
        for slot in expired:
            _journal({"event": "EXPIRE", "slot_id": slot.get("slot_id"), "lane": slot.get("lane"),
                      "seat": slot.get("seat"), "order_id": slot.get("order_id"),
                      "reason": slot.get("expired_reason"),
                      "over_sec": slot.get("expired_over_sec"),
                      "used": used_of(led, str(slot.get("lane")))})
        _save_locked(led)
    return expired


def _waitlist_push(led: dict, lane: str, order_id: str, prep_key: str, seat: str, priority: str) -> int:
    """入队（同 prep_key 去重）；超上限只增计数不存明细。返回队列位次（1 起）。"""
    wl = led["waitlist"].setdefault(lane, [])
    if not isinstance(wl, list):
        wl = led["waitlist"][lane] = []
    for pos, item in enumerate(wl, 1):
        if isinstance(item, dict) and item.get("prep_key") == prep_key:
            return pos
    if len(wl) >= WAITLIST_MAX_PER_LANE:
        ov = led["waitlist_overflow"]
        ov[lane] = int(ov.get(lane) or 0) + 1
        return WAITLIST_MAX_PER_LANE + int(ov[lane])
    wl.append({"order_id": order_id, "prep_key": prep_key, "seat": seat,
               "priority": priority, "requested_at": iso_now()})
    return len(wl)


def _waitlist_drop(led: dict, lane: str, prep_key: str) -> None:
    wl = led.get("waitlist", {}).get(lane)
    if not isinstance(wl, list):
        return
    led["waitlist"][lane] = [x for x in wl if not (isinstance(x, dict) and x.get("prep_key") == prep_key)]


def _waitlist_drop_requester(led: dict, lane: str, seat: str, order_id: str, prep_key: str) -> None:
    """占位成功后清掉该请求者的排队记录（队列是「被拒记录」，不是调度队列）。"""
    wl = led.get("waitlist", {}).get(lane)
    if not isinstance(wl, list):
        return
    led["waitlist"][lane] = [
        x for x in wl
        if not (isinstance(x, dict)
                and (x.get("prep_key") == prep_key
                     or (x.get("seat") == seat and x.get("order_id") == order_id)))
    ]


def _next_up(led: dict, lane: str) -> str:
    wl = led.get("waitlist", {}).get(lane)
    if not isinstance(wl, list) or not wl:
        return ""
    head = wl[0] if isinstance(wl[0], dict) else {}
    return str(head.get("order_id") or head.get("prep_key") or "")


# ── 占位（reserve / bind）──────────────────────────────────────────────────
def _require(value, name: str, phase: str) -> str:
    s = str(value or "").strip()
    if not s:
        raise UsageError(f"{phase} 阶段必填 --{name}")
    return s


def _new_slot(led: dict, lane: str, seat: str, order_id: str, prep_id: str, prep_key: str,
              ttl: int, priority: str, model: str, owner: str, note: str,
              session_id: str = "", command_id: str = "", pid: int | None = None) -> dict:
    slot_id = _next_slot_id(led, lane)
    t = now()
    slot = {
        "slot_id": slot_id,
        "idem_key": "",
        "prep_key": prep_key,
        "prep_id": prep_id,
        "lane": lane,
        "seat": seat,
        "session_id": session_id,
        "command_id": command_id,
        "order_id": order_id,
        "model": model,
        "state": "claimed",
        "priority": priority,
        "claimed_at": iso_now(),
        "claimed_ts": round(t, 3),
        "heartbeat_at": iso_now(),
        "heartbeat_ts": round(t, 3),
        "lease_ttl": int(ttl),
        "lease_until": round(t + float(ttl), 3),
        "released_at": "",
        "release_reason": "",
        "expired_at": "",
        "expired_reason": "",
        "pid": int(pid if pid is not None else os.getpid()),
        "degraded": False,
        "owner": owner,
        "meta": {"phase": "reserve", "note": note},
    }
    led["slots"][slot_id] = slot
    return slot


def _reserve_in_locked(led: dict, caps: dict, lane: str, seat: str, order_id: str,
                       prep_id: str, ttl: int, priority: str, model: str,
                       owner: str, note: str, session_id: str, command_id: str,
                       pid: int | None) -> tuple[dict, str]:
    """锁内 reserve 判定序：①键 ②session ③(seat,order) ④容量 ⑤落账。返回 (slot, event)。"""
    prep_key = prep_key_for(lane, seat, order_id, prep_id)
    cap = int(caps.get(lane, 0))

    # ① 幂等：prep_key 命中活跃 slot → 重放（刷新心跳，计数不变）
    for slot in _active_slots(led):
        if slot.get("prep_key") == prep_key:
            return _replay(led, caps, slot, ttl, "REPLAY", reason="prep_key_replay"), "REPLAY"

    # ② 同 session_id 已有其它活跃 slot（任意 lane）→ 席位层串行
    if session_id:
        for slot in _active_slots(led):
            if slot.get("session_id") and slot["session_id"] == session_id:
                _journal({"event": "REJECT", "code": "SESSION_BUSY", "lane": lane, "seat": seat,
                          "order_id": order_id, "session_id": session_id,
                          "holder_slot": slot.get("slot_id"), "used": used_of(led, lane), "cap": cap})
                raise Reject("SESSION_BUSY",
                             f"session {session_id} 已由 slot {slot.get('slot_id')}"
                             f"（lane {slot.get('lane')} / seat {slot.get('seat')}）占用，同一会话不得并行占两格",
                             lane=lane, used=used_of(led, lane), cap=cap)

    # ③ 同 (seat, order) 已有活跃 slot（不同键，常见于「重派」）→ 重放，不新增占位
    for slot in _active_slots(led):
        if slot.get("seat") == seat and slot.get("order_id") == order_id:
            meta = slot.get("meta") if isinstance(slot.get("meta"), dict) else {}
            meta["duplicate_of"] = slot.get("slot_id")
            meta["duplicate_prep_id"] = prep_id
            slot["meta"] = meta
            return _replay(led, caps, slot, ttl, "REPLAY", reason="seat_order_duplicate"), "REPLAY"

    # ④ 容量
    used = used_of(led, lane)
    if used >= cap:
        pos = _waitlist_push(led, lane, order_id, prep_key, seat, priority)
        _journal({"event": "REJECT", "code": "LANE_FULL", "lane": lane, "seat": seat,
                  "order_id": order_id, "used": used, "cap": cap, "queue_pos": pos})
        _save_locked(led)
        raise Reject("LANE_FULL", f"lane {lane} 满 used={used}/{cap}，排队第 {pos} 位（不硬塞）",
                     lane=lane, used=used, cap=cap, free=0, queue_pos=pos)

    # ⑤ 落账
    slot = _new_slot(led, lane, seat, order_id, prep_id, prep_key, ttl, priority, model,
                     owner, note, session_id=session_id, command_id=command_id, pid=pid)
    _waitlist_drop_requester(led, lane, seat, order_id, prep_key)
    _journal({"event": "CLAIM", "slot_id": slot["slot_id"], "lane": lane, "seat": seat,
              "order_id": order_id, "prep_id": prep_id, "prep_key": prep_key, "ttl": ttl,
              "used": used + 1, "cap": cap, "pid": slot["pid"]})
    return slot, "CLAIM"


def _replay(led: dict, caps: dict, slot: dict, ttl: int, event: str, reason: str = "") -> dict:
    """幂等重放：刷新租约，计数不变。"""
    t = now()
    slot["heartbeat_at"] = iso_now()
    slot["heartbeat_ts"] = round(t, 3)
    slot["lease_ttl"] = int(ttl)
    slot["lease_until"] = round(t + float(ttl), 3)
    lane = str(slot.get("lane"))
    used = used_of(led, lane)
    cap = int(caps.get(lane, 0))
    _journal({"event": event, "slot_id": slot.get("slot_id"), "lane": lane,
              "seat": slot.get("seat"), "order_id": slot.get("order_id"),
              "prep_key": slot.get("prep_key"), "idem_key": slot.get("idem_key"),
              "reason": reason, "used": used, "cap": cap, "ttl": ttl})
    return slot


def claim(lane: str | None = None, seat: str | None = None, order_id: str | None = None, *,
          phase: str = "reserve", slot: str | None = None, prep_id: str | None = None,
          session_id: str = "", command_id: str = "", model: str = "",
          priority: str = "P1", ttl: int | None = None,
          caps_file: str | os.PathLike | None = None, force_degraded: bool = False,
          owner: str = "", note: str = "", pid: int | None = None) -> dict:
    """占位。`phase=reserve`（容量闸，CreateSession 之前）/ `phase=bind`（补真实幂等键）。

    返回 dict 含 `ok/slot_id/used/cap/free/prep_id/replay/reclaimed/state`。
    业务拒绝抛 `Reject`（exit 3）；账本不可用抛 `Unavailable`（exit 4）；
    `force_degraded=True` **只在** Unavailable 时放行，绝不越过业务拒绝（N9）。
    """
    phase = str(phase or "reserve").strip().lower()
    if phase not in ("reserve", "bind"):
        raise UsageError(f"非法 --phase「{phase}」：限 reserve / bind")

    caps = load_caps(caps_file)  # caps 缺失即不可用（锁外先判，省一次锁）
    priority = normalize_priority(priority)
    if phase == "reserve":
        lane = normalize_lane(_require(lane, "lane", phase))
        seat = _require(seat, "seat", phase)
        order_id = _require(order_id, "order", phase)
        prep_id = str(prep_id or "").strip() or uuid.uuid4().hex[:12]
        ttl = int(ttl or TTL_RESERVE)
        slot_id_in = ""
    else:
        slot_id_in = _require(slot, "slot", phase)
        session_id = _require(session_id, "session", phase)
        command_id = _require(command_id, "command", phase)
        ttl = int(ttl or TTL_LEASE)
        order_id = str(order_id or "").strip()
        seat = str(seat or "").strip()
        lane = normalize_lane(lane) if lane else None

    try:
        with file_lock(ai_store.lock_name_for(ledger_path()), timeout=LOCK_TIMEOUT):
            led = _load_locked()
            _reap_locked(led)

            if phase == "reserve":
                sl, ev = _reserve_in_locked(led, caps, lane, seat, order_id, prep_id, ttl,
                                            priority, model, owner or str(seat), note,
                                            session_id, command_id, pid)
                _save_locked(led)
                cap = int(caps.get(lane, 0))
                used = used_of(led, lane)
                return {"ok": True, "event": ev,
                        "slot_id": sl["slot_id"], "lane": lane, "seat": sl["seat"],
                        "order_id": order_id, "state": sl["state"], "phase": "reserve",
                        "used": used, "cap": cap, "free": max(0, cap - used),
                        "prep_id": sl.get("prep_id") or prep_id, "prep_key": sl.get("prep_key"),
                        "lease_until": sl.get("lease_until"), "ttl": sl.get("lease_ttl"),
                        "replay": ev == "REPLAY"}

            # ── bind ──
            rec = (led.get("slots") or {}).get(slot_id_in)
            if not isinstance(rec, dict):
                _journal({"event": "REJECT", "code": "BAD_STATE", "slot_id": slot_id_in,
                          "reason": "slot 不存在"})
                raise Reject("BAD_STATE", f"slot {slot_id_in} 不存在于账本（{ledger_path()}）",
                             slot_id=slot_id_in)

            lane = normalize_lane(lane or str(rec.get("lane")))
            if lane != rec.get("lane"):
                raise Reject("BAD_STATE", f"slot {slot_id_in} 属于 lane {rec.get('lane')}，"
                                          f"与 --lane {lane} 不符", slot_id=slot_id_in)
            seat = seat or str(rec.get("seat"))
            order_id = order_id or str(rec.get("order_id"))
            cap = int(caps.get(lane, 0))

            reclaimed = False
            if rec.get("state") not in ACTIVE_STATES:
                # G3-3：slot 已被回收（会话已建、占位没了）→ 先按新 claim 重占；满则 ORPHAN
                new_prep = uuid.uuid4().hex[:12]
                try:
                    rec, _ev = _reserve_in_locked(led, caps, lane, seat or str(rec.get("seat")),
                                                  order_id, new_prep, TTL_RESERVE, priority, model,
                                                  owner or str(seat), f"reclaim_of:{slot_id_in}",
                                                  session_id, command_id, pid)
                except Reject as exc:
                    if exc.code != "LANE_FULL":
                        raise
                    _journal({"event": "ORPHAN", "slot_id": slot_id_in, "lane": lane,
                              "seat": seat, "order_id": order_id, "session_id": session_id,
                              "command_id": command_id, "used": used_of(led, lane), "cap": cap,
                              "reason": "slot_expired_and_lane_full"})
                    _save_locked(led)
                    raise Reject(
                        "ORPHAN",
                        f"slot {slot_id_in} 已回收（{rec.get('expired_reason') or rec.get('state')}）"
                        f"且 lane {lane} 满 {used_of(led, lane)}/{cap}：session {session_id} 在跑但无格，"
                        f"交 F 巡场认领；不得静默无格继续跑",
                        lane=lane, used=used_of(led, lane), cap=cap, slot_id=slot_id_in,
                        orphan=True)
                reclaimed = True
                slot_id_in = rec["slot_id"]

            idem = idem_key_for(lane, str(rec.get("seat")), session_id, command_id)

            # 幂等：同键重绑 → 重放（计数不变）
            if rec.get("idem_key") == idem:
                _replay(led, caps, rec, ttl, "REPLAY")
                _save_locked(led)
                used = used_of(led, lane)
                return {"ok": True, "event": "REPLAY", "slot_id": rec["slot_id"], "lane": lane,
                        "seat": rec.get("seat"), "order_id": order_id, "state": rec["state"],
                        "phase": "bind", "used": used, "cap": cap, "free": max(0, cap - used),
                        "idem_key": idem, "lease_until": rec.get("lease_until"),
                        "ttl": rec.get("lease_ttl"), "replay": True, "reclaimed": reclaimed}

            # ② session busy：该 session 已占别的格
            for other in _active_slots(led):
                if other is rec:
                    continue
                if other.get("session_id") and other["session_id"] == session_id:
                    _journal({"event": "REJECT", "code": "SESSION_BUSY", "slot_id": rec["slot_id"],
                              "lane": lane, "session_id": session_id,
                              "holder_slot": other.get("slot_id"),
                              "used": used_of(led, lane), "cap": cap})
                    raise Reject("SESSION_BUSY",
                                 f"session {session_id} 已由 slot {other.get('slot_id')}"
                                 f"（lane {other.get('lane')}）占用", lane=lane,
                                 used=used_of(led, lane), cap=cap)

            t = now()
            rec["session_id"] = session_id
            rec["command_id"] = command_id
            rec["idem_key"] = idem
            rec["order_id"] = order_id
            rec["seat"] = str(rec.get("seat") or seat)
            if model:
                rec["model"] = model
            rec["state"] = "leased" if rec.get("state") == "leased" else "claimed"
            rec["heartbeat_at"] = iso_now()
            rec["heartbeat_ts"] = round(t, 3)
            rec["lease_ttl"] = int(ttl)
            rec["lease_until"] = round(t + float(ttl), 3)
            meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
            meta["phase"] = "bound"
            meta["bind_session"] = session_id
            rec["meta"] = meta
            used = used_of(led, lane)
            _journal({"event": "BIND", "slot_id": rec["slot_id"], "lane": lane,
                      "seat": rec.get("seat"), "order_id": order_id, "session_id": session_id,
                      "command_id": command_id, "idem_key": idem, "used": used, "cap": cap,
                      "ttl": ttl, "reclaimed": reclaimed})
            _save_locked(led)
            return {"ok": True, "event": "BIND", "slot_id": rec["slot_id"], "lane": lane,
                    "seat": rec.get("seat"), "order_id": order_id, "state": rec["state"],
                    "phase": "bind", "used": used, "cap": cap, "free": max(0, cap - used),
                    "idem_key": idem, "lease_until": rec.get("lease_until"), "ttl": ttl,
                    "replay": False, "reclaimed": reclaimed}

    except (Unavailable, TimeoutError, OSError, sqlite3.Error) as exc:
        # N9：force_degraded 只对「账本不可用」生效；Reject 已在上面抛出，不进这里
        if force_degraded:
            _degraded_admit(exc, lane=lane or "", seat=seat or "", order_id=order_id or "",
                            phase=phase, session_id=session_id, slot_id=slot or "")
            return {"ok": True, "event": "DEGRADED_ADMIT", "degraded": True, "lane": lane or "",
                    "seat": seat or "", "order_id": order_id or "", "phase": phase,
                    "slot_id": slot or "", "used": None, "cap": None, "free": None,
                    "reason": str(exc), "replay": False, "note": "降级放行：账本不可用，本次未落账"}
        raise exc if isinstance(exc, Unavailable) else Unavailable(f"账本不可用：{exc}")


def _degraded_admit(exc: BaseException, **fields) -> None:
    """降级放行留痕（best-effort，绝不清洗坏账本）。"""
    try:
        _journal({"event": "DEGRADED_ADMIT", "code": "LEDGER_UNAVAILABLE",
                  "error": str(exc), "pid": os.getpid(), **fields})
    except OSError:
        pass


# ── 心跳 / 释放 ─────────────────────────────────────────────────────────────
def heartbeat(slot: str, ttl: int | None = None, *, owner: str = "", force: bool = False,
              caps_file: str | os.PathLike | None = None, force_degraded: bool = False) -> dict:
    """续租：`claimed → leased`；到期/终态不可续。"""
    caps = load_caps(caps_file)
    slot = _require(slot, "slot", "heartbeat")
    ttl = int(ttl or TTL_LEASE)
    try:
        with file_lock(ai_store.lock_name_for(ledger_path()), timeout=LOCK_TIMEOUT):
            led = _load_locked()
            _reap_locked(led)
            rec = (led.get("slots") or {}).get(slot)
            if not isinstance(rec, dict):
                raise Reject("BAD_STATE", f"slot {slot} 不存在于账本", slot_id=slot)
            if owner and rec.get("owner") and rec["owner"] != owner and not force:
                raise Reject("NOT_OWNER", f"slot {slot} 属 {rec.get('owner')}，{owner} 无权续租"
                                          f"（确需强制加 --force）", slot_id=slot)
            if rec.get("state") not in ACTIVE_STATES:
                code = "LEASE_EXPIRED" if rec.get("state") == "expired" else "BAD_STATE"
                raise Reject(code, f"slot {slot} 当前 state={rec.get('state')}，不可续租"
                                   f"（{rec.get('expired_reason') or rec.get('release_reason') or '-'}）",
                             slot_id=slot)
            t = now()
            rec["state"] = "leased"
            rec["heartbeat_at"] = iso_now()
            rec["heartbeat_ts"] = round(t, 3)
            rec["lease_ttl"] = ttl
            rec["lease_until"] = round(t + float(ttl), 3)
            lane = str(rec.get("lane"))
            _journal({"event": "HEARTBEAT", "slot_id": slot, "lane": lane, "seat": rec.get("seat"),
                      "state": "leased", "ttl": ttl, "lease_until": rec["lease_until"],
                      "used": used_of(led, lane)})
            _save_locked(led)
            return {"ok": True, "slot_id": slot, "lane": lane, "state": "leased",
                    "lease_ttl": ttl, "lease_until": rec["lease_until"]}
    except (Unavailable, TimeoutError, OSError) as exc:
        if force_degraded:
            _degraded_admit(exc, slot_id=slot, phase="heartbeat")
            return {"ok": True, "degraded": True, "slot_id": slot, "reason": str(exc)}
        raise exc if isinstance(exc, Unavailable) else Unavailable(f"账本不可用：{exc}")


def release(slot: str, reason: str = "done", *, force: bool = False, owner: str = "",
            caps_file: str | os.PathLike | None = None, force_degraded: bool = False) -> dict:
    """释放：`claimed/leased → released`；已终态则幂等返回 released=false。"""
    caps = load_caps(caps_file)
    slot = _require(slot, "slot", "release")
    reason = str(reason or "done").strip() or "done"
    try:
        with file_lock(ai_store.lock_name_for(ledger_path()), timeout=LOCK_TIMEOUT):
            led = _load_locked()
            _reap_locked(led)
            rec = (led.get("slots") or {}).get(slot)
            if not isinstance(rec, dict):
                raise Reject("BAD_STATE", f"slot {slot} 不存在于账本", slot_id=slot)
            if owner and rec.get("owner") and rec["owner"] != owner and not force:
                raise Reject("NOT_OWNER", f"slot {slot} 属 {rec.get('owner')}，{owner} 无权释放"
                                          f"（确需强制加 --force）", slot_id=slot)
            lane = str(rec.get("lane"))
            cap = int(caps.get(lane, 0))
            if rec.get("state") not in ACTIVE_STATES:
                return {"ok": True, "released": False, "slot_id": slot, "lane": lane,
                        "state": rec.get("state"), "used": used_of(led, lane), "cap": cap,
                        "free": max(0, cap - used_of(led, lane)), "next_up": "",
                        "note": "已是终态，幂等返回"}
            rec["state"] = "released"
            rec["released_at"] = iso_now()
            rec["release_reason"] = reason
            _waitlist_drop(led, lane, str(rec.get("prep_key") or ""))
            used = used_of(led, lane)
            nxt = _next_up(led, lane) if used < cap else ""
            _journal({"event": "RELEASE", "slot_id": slot, "lane": lane, "seat": rec.get("seat"),
                      "reason": reason, "used": used, "cap": cap, "next_up": nxt})
            _save_locked(led)
            return {"ok": True, "released": True, "slot_id": slot, "lane": lane,
                    "state": "released", "reason": reason, "used": used, "cap": cap,
                    "free": max(0, cap - used), "next_up": nxt}
    except (Unavailable, TimeoutError, OSError) as exc:
        if force_degraded:
            _degraded_admit(exc, slot_id=slot, phase="release")
            return {"ok": True, "degraded": True, "slot_id": slot, "released": False,
                    "reason": str(exc)}
        raise exc if isinstance(exc, Unavailable) else Unavailable(f"账本不可用：{exc}")


def reap(*, caps_file: str | os.PathLike | None = None, force_degraded: bool = False) -> dict:
    """手工回收（每个入口的惰性 reap 同源）。"""
    caps = load_caps(caps_file)
    try:
        with file_lock(ai_store.lock_name_for(ledger_path()), timeout=LOCK_TIMEOUT):
            led = _load_locked()
            expired = _reap_locked(led)
            rows = [{"slot_id": s.get("slot_id"), "lane": s.get("lane"),
                     "reason": s.get("expired_reason"), "over_sec": s.get("expired_over_sec")}
                    for s in expired]
            return {"ok": True, "expired": len(rows), "rows": rows}
    except (Unavailable, TimeoutError, OSError) as exc:
        if force_degraded:
            _degraded_admit(exc, phase="reap")
            return {"ok": True, "degraded": True, "expired": 0, "reason": str(exc), "rows": []}
        raise exc if isinstance(exc, Unavailable) else Unavailable(f"账本不可用：{exc}")


# ── 读：list / stats / check ───────────────────────────────────────────────
def list_slots(lane: str | None = None, *, caps_file: str | os.PathLike | None = None) -> dict:
    """活跃占位清单（入口先惰性 reap，故**可能写** EXPIRE 流水——文档已明示）。"""
    caps = load_caps(caps_file)
    lane_f = normalize_lane(lane) if lane else ""
    with file_lock(ai_store.lock_name_for(ledger_path()), timeout=LOCK_TIMEOUT):
        led = _load_locked()
        expired = _reap_locked(led)
        t = now()
        rows = []
        for slot in sorted(_active_slots(led), key=lambda s: (str(s.get("lane")), str(s.get("slot_id")))):
            if lane_f and slot.get("lane") != lane_f:
                continue
            try:
                remain = int(float(slot.get("lease_until") or 0) - t)
            except (TypeError, ValueError):
                remain = 0
            meta = slot.get("meta") if isinstance(slot.get("meta"), dict) else {}
            rows.append({"slot_id": slot.get("slot_id"), "lane": slot.get("lane"),
                         "seat": slot.get("seat"), "order_id": slot.get("order_id"),
                         "state": slot.get("state"), "phase": meta.get("phase", ""),
                         "session": str(slot.get("session_id") or "")[:8],
                         "remaining_sec": remain, "lease_until": slot.get("lease_until")})
        return {"ok": True, "rows": rows, "count": len(rows), "reaped": len(expired)}


def _journal_today() -> dict:
    p = journal_path()
    out = {"EXPIRE": 0, "DEGRADED_ADMIT": 0, "REJECT": 0, "ORPHAN": 0}
    if not p.exists():
        return out
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not str(rec.get("ts", "")).startswith(today):
                    continue
                ev = str(rec.get("event") or "")
                if ev in out:
                    out[ev] += 1
    except OSError:
        return out
    return out


def stats(*, caps_file: str | os.PathLike | None = None) -> dict:
    """每 lane `used/cap/free` + 等待深度 + 今日回收数 + degraded 数（入口先惰性 reap）。"""
    caps = load_caps(caps_file)
    with file_lock(ai_store.lock_name_for(ledger_path()), timeout=LOCK_TIMEOUT):
        led = _load_locked()
        _reap_locked(led)
        acts = _journal_today()
        lanes = {}
        for lane, cap in caps.items():
            used = used_of(led, lane)
            wl = led.get("waitlist", {}).get(lane)
            depth = len(wl) if isinstance(wl, list) else 0
            depth += int(led.get("waitlist_overflow", {}).get(lane) or 0)
            lanes[lane] = {"lane": lane, "used": used, "cap": int(cap),
                           "free": max(0, int(cap) - used), "queue_depth": depth}
        degraded = sum(1 for s in (led.get("slots") or {}).values()
                       if isinstance(s, dict) and s.get("degraded"))
        return {"ok": True, "lanes": lanes, "expired_today": acts["EXPIRE"],
                "rejected_today": acts["REJECT"], "orphan_today": acts["ORPHAN"],
                "degraded_today": acts["DEGRADED_ADMIT"], "degraded_slots": degraded,
                "root": str(state_root())}


def _registry_tasks(path: Path) -> list[dict]:
    data = load_json(path)
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("tasks") or []
    else:
        raw = []
    if isinstance(raw, dict):
        raw = [{**v, "id": k} for k, v in raw.items() if isinstance(v, dict)]
    return [t for t in raw if isinstance(t, dict)]


def check(registry: str | os.PathLike | None = None, *,
          presence_json: str | os.PathLike | None = None,
          presence_db: str | os.PathLike | None = None,
          caps_file: str | os.PathLike | None = None) -> dict:
    """三段对账：ORPHAN（绕过 4.5）/ STALE（会话不在 presence）/ DRIFT（账本根漂移）。

    附加 `CAPS_DRIFT`：caps 正典 vs `slot_pool.DEFAULT_CAP` 的口径告警（**不计入退出码**）。
    """
    caps = load_caps(caps_file)
    with file_lock(ai_store.lock_name_for(ledger_path()), timeout=LOCK_TIMEOUT):
        led = _load_locked()
        _reap_locked(led)
        active = _active_slots(led)
        active_sessions = {str(s.get("session_id")) for s in active if s.get("session_id")}

        orphan: list[dict] = []
        if registry:
            reg_path = Path(registry).expanduser().resolve()
            for task in _registry_tasks(reg_path):
                if str(task.get("state") or "").lower() not in ("running", "dispatched"):
                    continue
                sid = str(task.get("sessionId") or task.get("session_id") or "")
                if not sid:
                    continue
                if sid in active_sessions:
                    continue
                if any(str(((s.get("meta") if isinstance(s.get("meta"), dict) else {})
                            .get("bind_session")) or "") == sid for s in active):
                    continue
                orphan.append({"task": task.get("id"), "session": sid[:12],
                               "state": task.get("state"), "model": task.get("model", "")})

        presence = _presence_set(presence_json, presence_db)
        stale: list[dict] = []
        if presence is not None:
            for s in active:
                sid = str(s.get("session_id") or "")
                if sid and sid not in presence:
                    stale.append({"slot_id": s.get("slot_id"), "session": sid[:12],
                                  "lane": s.get("lane"), "seat": s.get("seat")})

        drift: list[str] = []
        actual = state_root()
        if os.path.normcase(str(actual)) != os.path.normcase(str(expected_root())):
            drift.append(f"state_root={actual} != 生产绝对根 {expected_root()}")
        if "worktrees" in str(actual).replace("\\", "/"):
            drift.append(f"账本根落在 worktree 内：{actual}")

        return {"ok": True, "orphan": orphan, "stale": stale, "drift": drift,
                "caps_drift": caps_drift(caps),
                "orphan_count": len(orphan), "stale_count": len(stale),
                "drift_count": len(drift), "presence_checked": presence is not None,
                "root": str(actual), "registry": str(registry or ""),
                "used": {lane: used_of(led, lane) for lane in caps}}


def _presence_set(presence_json: str | os.PathLike | None,
                  presence_db: str | os.PathLike | None) -> set[str] | None:
    """presence 来源：JSON 列表优先，其次 sqlite 只读（mode=ro）。都没有 → None（不冒充已查）。"""
    if presence_json:
        data = load_json(Path(presence_json).expanduser().resolve())
        if isinstance(data, list):
            return {str(x.get("session_id") or x.get("sessionId") or x) for x in data
                    if isinstance(x, (str, dict))}
        if isinstance(data, dict):
            items = data.get("sessions") or data.get("presence") or []
            if isinstance(items, (list, dict)):
                return {str(x.get("session_id") if isinstance(x, dict) else x) for x in
                        (items if isinstance(items, list) else items.keys())}
        return None
    if presence_db:
        p = Path(presence_db).expanduser().resolve()
        if not p.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            rows = con.execute("SELECT session_id FROM session_runtime_presence").fetchall()
            return {str(r[0]) for r in rows}
        except sqlite3.Error:
            return None
        finally:
            con.close()
    return None


# ── 观测辅助（证据包/CLI 摘要用）──────────────────────────────────────────
def summary_line(st: dict) -> str:
    parts = []
    for lane, item in st.get("lanes", {}).items():
        parts.append(f"{lane} {item['used']}/{item['cap']} free={item['free']} wait={item['queue_depth']}")
    tail = (f"⏱expired_today={st.get('expired_today', 0)} "
            f"⚠degraded={st.get('degraded_today', 0)}")
    return " | ".join(parts) + " | " + tail
