#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""slot_ledger.py — V4.0-P4 槽位账本（自包含，可独立单测）。
落盘 state/runner_slots.json = {"max": N, "slots": {order_id: {family, pid, claimed_at, heartbeat_at}}}；
读改写全在 ai_store.file_lock(SLOT_LOCK_NAME) 内 + 唯一 tmp/os.replace 原子落盘；ai_store 缺失 fail-fast
（ok=False + WARN 落 slot_ledger_warn.log，禁裸 pass）；心跳失败落 runner_slots_heartbeat_err.jsonl 并计数。"""
from contextlib import contextmanager
from pathlib import Path
import json, os, sys, time

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT / "scripts") not in sys.path: sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
SLOT_LOCK_NAME = "runner_slots"                  # ai_store.file_lock 的锁名（名字，不是路径）
LOCK_STALE_SEC = 1800                            # 心跳超 1800s 判僵尸槽
SLOTS_FILE, WARN_FILE = "runner_slots.json", "slot_ledger_warn.log"
HB_ERR_FILE, DEFAULT_MAX = "runner_slots_heartbeat_err.jsonl", 2
try:                                             # 锁原语唯一来源；导入失败=账本不可用，绝不无锁写盘
    import ai_store
    from ai_store import file_lock
    AI_STORE_ERR = ""
except Exception as exc:                         # noqa: BLE001
    ai_store = file_lock = None
    AI_STORE_ERR = f"ai_store unavailable: {type(exc).__name__}: {exc}"
def _now(): return time.strftime("%Y-%m-%dT%H:%M:%S")
def state_dir() -> Path:
    """STEWARD_STATE_DIR 优先（测试注入点），缺省沿用 ai_store.state_dir()。"""
    raw = os.environ.get("STEWARD_STATE_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else (ai_store.state_dir() if ai_store is not None else PLUGIN_ROOT / "state")
def _log(name, line):
    try:
        d = state_dir(); d.mkdir(parents=True, exist_ok=True)
        with open(d / name, "a", encoding="utf-8") as fh:
            fh.write(line if line.endswith("\n") else line + "\n")
    except OSError as exc:                        # 日志自身失败退 stderr 留痕，绝不静默吞掉
        sys.stderr.write(f"{line} (log failed: {exc})\n")
def _warn(tag, exc):
    msg = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
    _log(WARN_FILE, f"{_now()} WARN {tag}: {msg}")
    return {"ok": False, "error": msg}
def _read():
    p = state_dir() / SLOTS_FILE
    try: d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError: d = {}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _log(WARN_FILE, f"{_now()} WARN slot_load 账本损坏 {exc}（按空账重建）"); d = {}
    d = d if isinstance(d, dict) else {}
    d["slots"] = d["slots"] if isinstance(d.get("slots"), dict) else {}
    try: d["max"] = max(1, int(d.get("max") or slots_max()))
    except (TypeError, ValueError): d["max"] = slots_max()
    return d
def _write(d):
    p = state_dir() / SLOTS_FILE; p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{int(time.time() * 1e6)}")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try: os.replace(tmp, p)                       # 原子换名：读者只会看到完整账本
    except OSError: tmp.unlink(missing_ok=True); raise    # 换名失败清临时件，不留半份账
@contextmanager
def _lock():
    if file_lock is None: raise RuntimeError(AI_STORE_ERR or "ai_store.file_lock 不可用")
    with file_lock(SLOT_LOCK_NAME): yield          # 释放点：file_lock 内部 finally（异常路径同）

def _txn(tag, body):
    """锁内读改写：body(d) -> (resp, dirty)；dirty=True 落 d / dict 整份替换 / False 不落盘。"""
    if AI_STORE_ERR: return _warn(tag, RuntimeError(AI_STORE_ERR))
    try: cm = _lock(); cm.__enter__()              # 取锁：OS 锁不可重入，每个公开接口只取一次
    except Exception as exc:                       # noqa: BLE001 —— 取锁失败同样转 ok=False
        return _warn(tag, exc)
    try:
        d = _read(); resp, dirty = body(d)
        if dirty: _write(d if dirty is True else dirty)
        return resp
    except Exception as exc:                       # noqa: BLE001 —— 统一转 ok=False + WARN
        return _warn(tag, exc)
    finally:
        cm.__exit__(None, None, None)              # 异常也释放锁（try/finally 硬保证）

def _alive(pid):
    try:
        if int(pid or 0) <= 0: return False
        try:
            import psutil
            return bool(psutil.pid_exists(int(pid)))            # 首选：psutil 可用
        except ImportError:
            k32 = __import__("ctypes").windll.kernel32          # 兜底：Win32 OpenProcess
            h = k32.OpenProcess(0x1000, False, int(pid))         # QUERY_LIMITED_INFORMATION
            if h: k32.CloseHandle(h)
            return bool(h)
    except (TypeError, ValueError, AttributeError, OSError): return False

def slots_max(config_path=None):
    """v4.slots.max；enabled=false → 1；配置缺省/不可读 → DEFAULT_MAX(2)。"""
    path = Path(config_path) if config_path else PLUGIN_ROOT / "config" / "steward.json"
    try:
        s = (json.loads(path.read_text(encoding="utf-8")).get("v4") or {}).get("slots") or {}
        return 1 if (isinstance(s, dict) and s.get("enabled") is False) else max(1, int(s.get("max", DEFAULT_MAX)))
    except (OSError, ValueError, UnicodeDecodeError, TypeError, AttributeError) as exc:
        _log(WARN_FILE, f"{_now()} WARN slots_max 配置不可用 {path}: {exc}")
        return DEFAULT_MAX

def slot_load():
    return _txn("slot_load", lambda d: ({"ok": True, "max": d["max"], "slots": d["slots"]}, False))

def _slot_save(data):
    return _txn("slot_save", lambda d: ({"ok": True}, data))

def slot_claim(order_id, family, *, max_slots=None, pid=None):
    def body(d):
        s = d["slots"]
        cap = d["max"] = max(1, int(max_slots)) if max_slots else slots_max()
        if order_id in s or any(isinstance(v, dict) and v.get("family") == family for v in s.values()):
            return {"ok": False, "reason": "family_busy", "order_id": order_id, "family": family}, False
        if len(s) >= cap:
            return {"ok": False, "reason": "full", "max": cap, "used": len(s)}, False
        ts = time.time()
        s[order_id] = {"family": family, "pid": int(os.getpid() if pid is None else pid), "claimed_at": ts, "heartbeat_at": ts}
        return {"ok": True, "slot": dict(s[order_id]), "max": cap, "used": len(s)}, True
    return _txn(f"slot_claim({order_id})", body)

def slot_release(order_id):
    def body(d):
        v = d["slots"].pop(order_id, None)
        return {"ok": True, "released": v is not None, "order_id": order_id, **({"slot": v} if v else {})}, v is not None
    return _txn(f"slot_release({order_id})", body)

def slot_reap(stale_sec=LOCK_STALE_SEC, now_ts=None):
    def body(d):
        ts, out = (time.time() if now_ts is None else float(now_ts)), []
        for oid, v in list(d["slots"].items()):
            v = v if isinstance(v, dict) else {}
            try: hb = float(v.get("heartbeat_at") or 0)
            except (TypeError, ValueError): hb = 0.0
            why = "stale" if ts - hb > stale_sec else ("" if _alive(v.get("pid")) else "dead_pid")
            if why: out.append({"order_id": oid, "family": v.get("family"), "pid": v.get("pid"), "reason": why}); del d["slots"][oid]
        return {"ok": True, "reaped": out, "used": len(d["slots"]), "max": d["max"]}, bool(out)
    return _txn("slot_reap", body)

def slot_heartbeat(order_id):
    def body(d):
        v = d["slots"].get(order_id)
        if not isinstance(v, dict): raise LookupError("no_such_slot")
        v["heartbeat_at"] = time.time()
        return {"ok": True, "order_id": order_id, "heartbeat_at": v["heartbeat_at"], "errors": 0}, True
    r = _txn(f"slot_heartbeat({order_id})", body)
    if r.get("ok"): return r
    err = r.get("error", "heartbeat_failed")
    _log(HB_ERR_FILE, json.dumps({"ts": time.time(), "iso": _now(), "order": order_id, "error": err}, ensure_ascii=False))
    return {"ok": False, "reason": "heartbeat_failed", "order_id": order_id, "errors": 1, "error": err}

def slot_snapshot():
    return _txn("slot_snapshot", lambda d: ({"ok": True, "max": d["max"], "used": len(d["slots"]),
                "slots": [{"order_id": o, **v} for o, v in d["slots"].items() if isinstance(v, dict)]}, False))
