#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_store.py — 管家状态存储原语（无 git 依赖，状态目录可注入）。

职责单一：目录定位、原子读写、孤儿临时文件回收、跨平台文件锁。
不含任何业务语义（锁在 ai_locks，任务台在 steward_board）。

- 状态目录：环境变量 STEWARD_STATE_DIR 优先（每次调用实时读取，测试/多实例可注入），
  缺省 <插件根>/state/。本模块不跑 git 命令：插件不是 git 仓库，任何位置都能用。
- 原子写沿用 newcad 2026-09-16 审计 F6-5 设计：Windows 下 os.replace 目标被并发读者/
  索引器/杀软句柄占用会抛 [WinError 5]，单次换名在并发下高概率失败；故
  唯一临时名（PID + 纳秒 + 随机）+ 指数退避重试。读者永远只会看到完整文件。
- 临时文件名标记 TMP_MARK 是写路径与孤儿回收路径共用的唯一真相源。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR_ENV = "STEWARD_STATE_DIR"
DEFAULT_STATE_DIR = PLUGIN_ROOT / "state"

ATOMIC_WRITE_RETRY = 12    # 换名重试次数（退避总计约 2.2s，远小于一次调度周期）
ATOMIC_FIRST_DELAY = 0.02  # 首次退避间隔（秒），指数退避
ATOMIC_TMP_KEEP_SEC = 600  # 孤儿临时文件保留窗（秒）：只清远老孤儿，绝不删在途写
TMP_MARK = ".tmp-"         # 临时文件名标记（写路径与回收路径共用）


def state_dir() -> Path:
    """状态根目录：STEWARD_STATE_DIR 优先，缺省 <插件根>/state/。

    每次调用实时读取环境变量，因此测试可在 import 之后注入；绝不缓存到模块常量，
    避免「测试改了 env、模块还拿旧值」这类配置陈旧问题。
    """
    raw = os.environ.get(STATE_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_STATE_DIR


# import 时快照，仅作只读观测；内部逻辑一律走 state_dir() 动态取。
STATE_DIR = state_dir()


def state_path(*parts: str) -> Path:
    return state_dir().joinpath(*parts)


def now() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# ── 原子写 ──────────────────────────────────────────────────────────────────
def _retriable(exc: BaseException) -> bool:
    """判定异常是否属「换名/写临时文件被占用」类可重试错误。

    只放行权限拒绝与共享冲突；其它异常（如非法路径）立即抛出，不把真错误吞成重试。
    """
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) in (5, 32, 33):
            return True
        text = str(exc)
        return ("WinError 32" in text or "WinError 33" in text
                or "being used by another" in text or "in use" in text)
    return False


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except BaseException:
        pass


def tmp_name(path: Path) -> Path:
    """带 PID 与纳秒时间戳的唯一临时文件名（并发写者的临时文件绝不互相撞名）。"""
    return path.with_name(f"{path.name}{TMP_MARK}{os.getpid()}-{time.time_ns()}-{secrets.token_hex(3)}")


def atomic_write_text(path: str | os.PathLike, text: str, *,
                      retries: int = ATOMIC_WRITE_RETRY, fsync: bool = True) -> None:
    """原子写文本：唯一临时名 + fsync + 退避重试的 os.replace。

    不变量：
      - 并发写者互不撞名（临时名含 PID/纳秒/随机），失败路径清理临时文件不留孤儿；
      - 读者永不见半成品（同卷换名原子）；
      - 可重试错误在退避后重试，重试耗尽抛最后一次异常；
      - 非可重试错误立即抛出（不掩盖真故障）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    delay = ATOMIC_FIRST_DELAY
    last: BaseException | None = None
    attempts = max(1, int(retries))
    for _ in range(attempts):
        tmp = tmp_name(path)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                if fsync:
                    os.fsync(f.fileno())
        except BaseException as exc:
            _unlink_quiet(tmp)
            last = exc
            if not _retriable(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
            continue
        try:
            os.replace(tmp, path)  # 同卷原子换名：并发读到的永远是完整文本
            return
        except BaseException as exc:
            _unlink_quiet(tmp)  # 换名失败不留孤儿
            last = exc
            if not _retriable(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    raise last if last is not None else OSError(f"原子写失败：{path}")


def atomic_write_json(path: str | os.PathLike, data: Any, *, indent: int | None = 2,
                      ensure_ascii: bool = False, trailing_newline: bool = False,
                      retries: int = ATOMIC_WRITE_RETRY, fsync: bool = True) -> None:
    """dict/list 的原子写（JSON 序列化后走 atomic_write_text）。"""
    text = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, retries=retries, fsync=fsync)


def load_json(path: str | os.PathLike) -> Any:
    """容错读：文件缺失/残片/编码坏都返回 None（心跳换名瞬间不炸调用方）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def clean_orphan_tmps(max_age_sec: float = ATOMIC_TMP_KEEP_SEC,
                      root: str | os.PathLike | None = None) -> int:
    """回收写失败或进程被杀遗留的孤儿临时文件（返回删除数）。

    只清「超过保留窗的**非当前 pid**」的 TMP_MARK 文件：同 pid 的临时文件可能正被
    本进程在途写持有；保留窗内他人正在写的临时文件也绝不删。
    """
    base = Path(root) if root is not None else state_dir()
    if not base.exists():
        return 0
    me = os.getpid()
    t = now()
    removed = 0
    for p in base.rglob("*" + TMP_MARK + "*"):
        if p.is_dir():
            continue
        owner = p.name.split(TMP_MARK)[-1].split("-")[0]
        if owner == str(me):
            continue
        try:
            if t - p.stat().st_mtime <= max_age_sec:
                continue
            p.unlink(missing_ok=True)
            removed += 1
        except BaseException:
            pass
    return removed


# ── 跨平台文件锁（锁文件集中 <状态目录>/.locks/，单字节排它）────────────────
_IS_WINDOWS = os.name == "nt" or sys.platform.startswith("win")
if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def _safe_lock_name(lock_name: str) -> str:
    name = re.sub(r"[^0-9A-Za-z._-]", "_", lock_name or "")
    if not name:
        raise ValueError("锁名不能为空")
    return name


def lock_name_for(path: str | os.PathLike) -> str:
    """为任意状态文件派生锁名：文件名 + 绝对路径摘要（同名不同目录不互相锁）。"""
    p = Path(path).expanduser().resolve()
    digest = hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:10]
    base = re.sub(r"[^0-9A-Za-z._-]", "_", p.name)[:40] or "state"
    return f"file-{base}-{digest}"


@contextmanager
def file_lock(lock_name: str, timeout: float = 10.0, poll_interval: float = 0.05):
    """对 <状态目录>/.locks/<lock_name>.lock 取排它锁（lock_name 是名字，不是路径）。

    OS 级锁：进程崩溃由内核释放，不需要「陈旧锁超时清理」。
    """
    lock_dir = state_dir() / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = str(lock_dir / (_safe_lock_name(lock_name) + ".lock"))

    start = time.time()
    fd = None
    while True:
        try:
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o666)
            if _IS_WINDOWS:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except (IOError, OSError):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
            if time.time() - start >= timeout:
                raise TimeoutError(f"获取锁超时: {_safe_lock_name(lock_name)}（{timeout}s）")
            time.sleep(poll_interval)
    try:
        yield
    finally:
        if fd is not None:
            try:
                if _IS_WINDOWS:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
