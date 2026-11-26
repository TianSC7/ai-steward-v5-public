# -*- coding: utf-8 -*-
"""ai_store 边界用例：目录注入、原子写（重试/失败清理/孤儿回收）、容错读、文件锁、并发写。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import ai_store

SCRIPTS = Path(ai_store.__file__).resolve().parent


# ── 状态目录注入 ────────────────────────────────────────────────────────────
def test_state_dir_env_injection(isolated_state_dir):
    assert ai_store.state_dir() == isolated_state_dir


def test_state_dir_default_is_plugin_state(monkeypatch):
    monkeypatch.delenv("STEWARD_STATE_DIR", raising=False)
    assert ai_store.state_dir() == ai_store.PLUGIN_ROOT / "state"
    assert ai_store.state_dir().name == "state"


# ── 原子写 ──────────────────────────────────────────────────────────────────
def test_atomic_write_text_roundtrip(tmp_path):
    target = tmp_path / "sub" / "a.json"
    ai_store.atomic_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    ai_store.atomic_write_text(target, '{"a": 2}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert list(target.parent.glob("*" + ai_store.TMP_MARK + "*")) == []


def test_atomic_write_retries_transient_replace(monkeypatch, tmp_path):
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 4:
            raise PermissionError(13, "Access is denied")  # WinError 5 同族
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    target = tmp_path / "retry.json"
    ai_store.atomic_write_text(target, "ok")
    assert target.read_text(encoding="utf-8") == "ok"
    assert calls["n"] == 4
    assert list(tmp_path.glob("*" + ai_store.TMP_MARK + "*")) == []


def test_atomic_write_gives_up_cleanly(monkeypatch, tmp_path):
    def always_denied(src, dst):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(os, "replace", always_denied)
    target = tmp_path / "never.json"
    with pytest.raises(PermissionError):
        ai_store.atomic_write_text(target, "x", retries=3)
    assert not target.exists()
    assert list(tmp_path.glob("*" + ai_store.TMP_MARK + "*")) == []


def test_clean_orphan_tmps_only_old_foreign(isolated_state_dir):
    old = isolated_state_dir / "x.json.tmp-999999-1-abcdef"
    old.write_text("junk", encoding="utf-8")
    stale = ai_store.now() - 3600
    os.utime(old, (stale, stale))

    fresh = isolated_state_dir / "locks" / "y.json.tmp-999998-1-abcdef"
    fresh.parent.mkdir(parents=True)
    fresh.write_text("junk", encoding="utf-8")

    mine = isolated_state_dir / f"z.json.tmp-{os.getpid()}-1-abcdef"
    mine.write_text("junk", encoding="utf-8")
    os.utime(mine, (stale, stale))  # 自己 pid 的临时文件即使很老也不清

    assert ai_store.clean_orphan_tmps(600) == 1
    assert not old.exists()
    assert fresh.exists()
    assert mine.exists()


def test_load_json_tolerates_missing_and_corrupt(tmp_path):
    assert ai_store.load_json(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ai_store.load_json(bad) is None
    good = tmp_path / "good.json"
    ai_store.atomic_write_json(good, {"k": "v"}, trailing_newline=True)
    assert ai_store.load_json(good) == {"k": "v"}


# ── 文件锁 ──────────────────────────────────────────────────────────────────
def test_file_lock_excludes_second_holder(isolated_state_dir):
    outcomes: list[str] = []

    def contender():
        try:
            with ai_store.file_lock("demo", timeout=0.2):
                outcomes.append("acquired")
        except TimeoutError:
            outcomes.append("timeout")

    with ai_store.file_lock("demo"):
        t = threading.Thread(target=contender)
        t.start()
        t.join()
    assert outcomes == ["timeout"]
    with ai_store.file_lock("demo", timeout=1.0):
        pass  # 释放后可再取


def test_lock_name_for_is_path_specific(tmp_path):
    a = ai_store.lock_name_for(tmp_path / "one" / "registry.json")
    b = ai_store.lock_name_for(tmp_path / "two" / "registry.json")
    assert a != b
    assert a == ai_store.lock_name_for(tmp_path / "one" / "registry.json")


# ── 并发写：20 个写者同一文件 ───────────────────────────────────────────────
def test_twenty_concurrent_writers_never_corrupt(tmp_path):
    target = tmp_path / "hot.json"
    barrier = threading.Barrier(20)
    failures: list[tuple[int, str]] = []

    def writer(idx: int) -> None:
        try:
            barrier.wait(timeout=20)
            for seq in range(10):
                ai_store.atomic_write_json(
                    target, {"writer": idx, "seq": seq, "pad": "x" * 512})
        except BaseException as exc:  # noqa: BLE001 - 收集所有并发异常统一断言
            failures.append((idx, repr(exc)))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert failures == []
    data = json.loads(target.read_text(encoding="utf-8"))
    assert 0 <= data["writer"] < 20
    assert 0 <= data["seq"] < 10
    assert list(tmp_path.glob("*" + ai_store.TMP_MARK + "*")) == []


_WRITER_CODE = """
import sys
sys.path.insert(0, sys.argv[1])
import ai_store
target, tag = sys.argv[2], int(sys.argv[3])
for seq in range(10):
    ai_store.atomic_write_json(target, {"writer": tag, "seq": seq, "pad": "y" * 256})
print("OK")
"""


def test_cross_process_writers_never_corrupt(tmp_path):
    target = tmp_path / "cross.json"
    procs = [
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", _WRITER_CODE,
             str(SCRIPTS), str(target), str(i)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(8)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        results.append((p.returncode, out.strip(), err.strip()))
    assert all(rc == 0 and out == "OK" for rc, out, err in results), results
    data = json.loads(target.read_text(encoding="utf-8"))
    assert 0 <= data["writer"] < 8
    assert list(tmp_path.glob("*" + ai_store.TMP_MARK + "*")) == []
