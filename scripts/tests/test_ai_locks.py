# -*- coding: utf-8 -*-
"""ai_locks 边界用例：TTL 生命周期、过期抢占、强制接管、release_all_for、续期、参数校验。"""
from __future__ import annotations

import time

import pytest

import ai_locks
import ai_store


def test_acquire_release_lifecycle():
    got = ai_locks.acquire("sess_a", "build", ttl_min=30, note="编译")
    assert got["success"] is True
    assert got["stolen_from"] == ""

    st = ai_locks.state("build")
    assert st["owner"] == "sess_a"
    assert st["note"] == "编译"
    assert 0 < st["remaining_sec"] <= 30 * 60
    assert st["expired"] is False

    denied = ai_locks.acquire("sess_b", "build")
    assert denied["success"] is False
    assert denied["holder"] == "sess_a"
    assert denied["remaining_min"] >= 1

    assert ai_locks.release("sess_b", "build")["success"] is False
    rel = ai_locks.release("sess_a", "build")
    assert rel["success"] is True and rel["released"] is True
    assert ai_locks.state("build") is None
    assert ai_locks.acquire("sess_b", "build")["success"] is True


def test_expired_lock_is_preempted():
    ai_locks.acquire("sess_old", "display", ttl_min=30)
    path = ai_locks._lock_path("display")
    rec = ai_store.load_json(path)
    rec["expires_at"] = ai_store.now() - 5  # 模拟 TTL 已过
    ai_store.atomic_write_json(path, rec)

    assert ai_locks.state("display")["expired"] is True
    got = ai_locks.acquire("sess_new", "display", ttl_min=30)
    assert got["success"] is True
    assert got["stolen_from"] == "sess_old"
    assert ai_locks.state("display")["owner"] == "sess_new"


def test_force_takeover():
    ai_locks.acquire("sess_a", "build", ttl_min=30)
    assert ai_locks.acquire("sess_b", "build")["success"] is False
    forced = ai_locks.acquire("sess_b", "build", force=True)
    assert forced["success"] is True
    assert forced["stolen_from"] == "sess_a"
    assert ai_locks.state("build")["owner"] == "sess_b"


def test_release_all_for_only_own_locks():
    ai_locks.acquire("sess_a", "build")
    ai_locks.acquire("sess_a", "display")
    ai_locks.acquire("sess_b", "capture")

    released = sorted(ai_locks.release_all_for("sess_a"))
    assert released == ["build", "display"]
    assert ai_locks.state("build") is None
    assert ai_locks.state("display") is None
    assert ai_locks.state("capture")["owner"] == "sess_b"
    assert ai_locks.release_all_for("sess_a") == []


def test_extend_refreshes_ttl_and_rejects_non_owner():
    ai_locks.acquire("sess_a", "build", ttl_min=1)
    before = ai_locks.state("build")["expires_at"]
    time.sleep(0.01)  # expires_at 由浮点时间计算，确保续期后严格更大
    ext = ai_locks.extend("sess_a", "build", ttl_min=10)
    assert ext["success"] is True
    assert ext["expires_at"] > before
    assert ai_locks.state("build")["remaining_sec"] > 9 * 60

    bad = ai_locks.extend("sess_b", "build", ttl_min=10)
    assert bad["success"] is False
    assert "无权续期" in bad["error"]


def test_list_locks_drops_expired():
    ai_locks.acquire("sess_a", "build")
    path = ai_locks._lock_path("build")
    rec = ai_store.load_json(path)
    rec["expires_at"] = ai_store.now() - 1
    ai_store.atomic_write_json(path, rec)

    assert ai_locks.list_locks() == []
    assert not path.exists()


@pytest.mark.parametrize("bad", ["", "Build", "9bad", "with-dash", "a" * 30, "../x", "build lock"])
def test_invalid_resource_name_rejected(bad):
    with pytest.raises(ValueError):
        ai_locks.acquire("sess_a", bad)


def test_invalid_owner_rejected():
    with pytest.raises(ValueError):
        ai_locks.acquire("", "build")
    with pytest.raises(ValueError):
        ai_locks.acquire("bad owner!", "build")
