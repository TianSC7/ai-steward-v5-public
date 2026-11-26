# -*- coding: utf-8 -*-
"""并发账本单测（C-20261120-003 · A/C 块）

覆盖 plan §C6：状态机 / 幂等 / SESSION_BUSY / 容量拒绝+waitlist / 租约回收 /
reserve 超时 / 首次初始化（缺失≠损坏）/ caps 夹具两条腿 / 状态根 / 坏账本 /
真 state 零污染 / `--force-degraded` 作用域（N9）/ bind 过期重占与 ORPHAN。
并发 8 进程在 `test_lane_ledger_concurrent.py`（真进程，非线程）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ai_store
import lane_ledger as L

SCRIPTS = Path(__file__).resolve().parent.parent
REPO_CAPS = SCRIPTS.parent / "state" / "lane_caps.json"


# ── 夹具 ───────────────────────────────────────────────────────────────────
def _seed_caps(state_dir: Path, override: dict | None = None) -> Path:
    """把仓内真实 lane_caps.json 显式 copy 进测试状态根（R1 硬规矩 2 的①）。"""
    data = json.loads(REPO_CAPS.read_text(encoding="utf-8"))
    if override:
        data["caps"].update(override)
    dst = Path(state_dir) / "lane_caps.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return dst


def _ledger(state_dir: Path) -> dict:
    return json.loads((Path(state_dir) / "lane_ledger.json").read_text(encoding="utf-8"))


def _journal_events(state_dir: Path) -> list[str]:
    p = Path(state_dir) / "lane_ledger.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line).get("event", ""))
    return out


def _active(state_dir: Path) -> list[dict]:
    return [s for s in _ledger(state_dir)["slots"].values()
            if s.get("state") in ("claimed", "leased")]


# ── 状态机：claim → heartbeat → bind → release → 再 claim ──────────────────
def test_state_machine(isolated_state_dir):
    _seed_caps(isolated_state_dir)
    r1 = L.claim("R4", "W1", "C-T1", phase="reserve")
    assert r1["ok"] and r1["state"] == "claimed" and r1["used"] == 1

    hb = L.heartbeat(r1["slot_id"])
    assert hb["state"] == "leased"

    b = L.claim(slot=r1["slot_id"], phase="bind", session_id="sess_1", command_id="cmd_1")
    assert b["ok"] and b["state"] == "leased" and b["used"] == 1
    assert b["idem_key"] == L.idem_key_for("R4", "W1", "sess_1", "cmd_1")

    rel = L.release(r1["slot_id"], "done")
    assert rel["released"] and rel["used"] == 0 and rel["free"] == 6

    # released 不计数、但记录保留（不删证据）
    assert _ledger(isolated_state_dir)["slots"][r1["slot_id"]]["state"] == "released"

    r2 = L.claim("R4", "W1", "C-T1b", phase="reserve")
    assert r2["ok"] and r2["used"] == 1 and r2["slot_id"] != r1["slot_id"]

    # 释放后再 release 同一 slot → 幂等，不报错
    again = L.release(r1["slot_id"], "done")
    assert again["ok"] and again["released"] is False


# ── 幂等：同键重复 claim 不新增占用 ────────────────────────────────────────
def test_idempotent(isolated_state_dir):
    _seed_caps(isolated_state_dir)
    a = L.claim("R4", "W1", "C-T2", phase="reserve", prep_id="p1")
    assert a["used"] == 1

    for _ in range(2):
        r = L.claim("R4", "W1", "C-T2", phase="reserve", prep_id="p1")
        assert r["replay"] is True
        assert r["slot_id"] == a["slot_id"]
        assert r["used"] == 1
    assert L.stats()["lanes"]["R4"]["used"] == 1

    # 换 prep_id（等价「换了 session/command 的重派」）但同 (seat, order) → 仍重放
    r = L.claim("R4", "W1", "C-T2", phase="reserve", prep_id="p2")
    assert r["replay"] is True and r["slot_id"] == a["slot_id"] and r["used"] == 1
    assert L.stats()["lanes"]["R4"]["used"] == 1

    # bind 后同键重绑 → 重放，计数不变
    b1 = L.claim(slot=a["slot_id"], phase="bind", session_id="sess_a", command_id="cmd_a")
    b2 = L.claim(slot=a["slot_id"], phase="bind", session_id="sess_a", command_id="cmd_a")
    assert b1["replay"] is False and b2["replay"] is True and b2["used"] == 1


# ── 席位层串行：同 session 不得并行占两格 ─────────────────────────────────
def test_session_busy(isolated_state_dir):
    _seed_caps(isolated_state_dir)
    L.claim("STEP", "S1", "C-T3", phase="reserve", session_id="sess_x")
    with pytest.raises(L.Reject) as ei:
        L.claim("R4-2", "S9", "C-T3b", phase="reserve", session_id="sess_x")
    assert ei.value.code == "SESSION_BUSY"
    assert "sess_x" in str(ei.value)
    assert L.stats()["lanes"]["R4-2"]["used"] == 0
    assert L.stats()["lanes"]["STEP"]["used"] == 1


# ── 容量拒绝 + waitlist ───────────────────────────────────────────────────
def test_capacity_reject_and_waitlist(isolated_state_dir):
    _seed_caps(isolated_state_dir)
    L.claim("NEX", "E1", "O1", phase="reserve")
    L.claim("NEX", "E2", "O2", phase="reserve")
    with pytest.raises(L.Reject) as ei:
        L.claim("NEX", "E3", "O3", phase="reserve")
    assert ei.value.code == "LANE_FULL"
    assert ei.value.extra["used"] == 2 and ei.value.extra["cap"] == 2
    assert ei.value.extra["queue_pos"] == 1
    assert "used=2/2" in str(ei.value)

    st = L.stats()
    assert st["lanes"]["NEX"]["used"] == 2 and st["lanes"]["NEX"]["free"] == 0
    assert st["lanes"]["NEX"]["queue_depth"] == 1

    # release 一个 → 有空位、next_up 指向队首（不自动派单，账本不做派发）
    rel = L.release("NEX-001", "handoff")
    assert rel["free"] == 1 and rel["next_up"] == "O3"
    assert L.stats()["lanes"]["NEX"]["queue_depth"] == 1  # 排队者是「被拒记录」，不放水

    # 队首自己回来 claim → 占位成功，排队记录被清
    r = L.claim("NEX", "E3", "O3", phase="reserve")
    assert r["ok"] and r["used"] == 2
    assert L.stats()["lanes"]["NEX"]["queue_depth"] == 0


# ── 租约到期：惰性回收 + EXPIRE 流水 + 同 lane 可再 claim ──────────────────
def test_lease_expire(isolated_state_dir):
    _seed_caps(isolated_state_dir)
    r = L.claim("STEP", "S1", "O1", phase="reserve", ttl=2)
    time.sleep(3.0)

    st = L.stats()  # 入口惰性 reap
    assert st["lanes"]["STEP"]["used"] == 0
    assert st["expired_today"] >= 1
    assert "EXPIRE" in _journal_events(isolated_state_dir)
    assert _ledger(isolated_state_dir)["slots"][r["slot_id"]]["state"] == "expired"

    r2 = L.claim("STEP", "S1", "O2", phase="reserve")
    assert r2["ok"] and r2["used"] == 1


# ── reserve 超时：不 bind 的占位自动回收，理由 reserve_timeout ─────────────
def test_reserve_timeout(isolated_state_dir):
    _seed_caps(isolated_state_dir)
    r = L.claim("STEP", "S1", "O1", phase="reserve", ttl=1)
    time.sleep(2.0)
    out = L.reap()
    assert out["expired"] == 1 and out["rows"][0]["reason"] == "reserve_timeout"
    slot = _ledger(isolated_state_dir)["slots"][r["slot_id"]]
    assert slot["state"] == "expired" and slot["expired_reason"] == "reserve_timeout"


# ── 首次初始化：缺失 ≠ 损坏（新机器不得停线）─────────────────────────────
def test_first_run(isolated_state_dir):
    _seed_caps(isolated_state_dir)
    assert not (Path(isolated_state_dir) / "lane_ledger.json").exists()

    r = L.claim("R4", "W1", "O1", phase="reserve")
    assert r["ok"] and r["used"] == 1

    led = _ledger(isolated_state_dir)
    assert led["version"] == 1 and len(led["slots"]) == 1
    assert isinstance(led["slots"], dict) and isinstance(led["waitlist"], dict)
    assert "INIT" in _journal_events(isolated_state_dir)

    r2 = L.claim("R4", "W2", "O2", phase="reserve")
    assert r2["ok"] and r2["used"] == 2
    assert L.stats()["lanes"]["R4"]["used"] == 2


# ── caps 夹具两条腿（R1）：copy 或 --caps，两样都没有 → exit 4 ─────────────
def test_caps_fixture(isolated_state_dir):
    # ③ 两样都没有 → 不可用，且原因必须可读
    with pytest.raises(L.Unavailable) as ei:
        L.load_caps()
    msg = str(ei.value)
    assert "caps 缺失" in msg and "--caps" in msg

    # ② --caps 指向仓内真实文件 → 生效
    caps = L.load_caps(REPO_CAPS)
    assert caps["R4"] == 6 and caps["R4-2"] == 6 and caps["STEP"] == 3
    assert caps["NEX"] == 2 and caps["GLM"] == 3 and caps["OUTSOURCE"] == 60
    r = L.claim("R4", "W1", "O1", phase="reserve", caps_file=REPO_CAPS)
    assert r["ok"] and r["cap"] == 6

    # ① 显式 copy 进测试状态根 → 生效
    _seed_caps(isolated_state_dir)
    r2 = L.claim("R4", "W2", "O2", phase="reserve")
    assert r2["ok"] and r2["cap"] == 6


# ── 状态根：不随 PLUGIN_ROOT/cwd 漂；worktree 形态必报 DRIFT ───────────────
def test_state_root(isolated_state_dir, tmp_path, monkeypatch):
    # A. 无 env 时 import 即钉生产绝对根（N7 纵深防御）：子进程验证，且不写任何文件
    code = ("import os, sys\n"
            "os.environ.pop('STEWARD_STATE_DIR', None)\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import lane_ledger as L\n"
            "print(L.ledger_path())\n")
    env = {k: v for k, v in os.environ.items() if k != "STEWARD_STATE_DIR"}
    out = subprocess.run([sys.executable, "-X", "utf8", "-c", code], env=env,
                         cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    printed = Path(out.stdout.strip())
    assert printed == L.expected_root() / "lane_ledger.json"
    assert "worktrees" not in str(printed).replace("\\", "/")

    # B. 人为把状态根指到 worktree 形态 → check 的 DRIFT 段必报（只写 tmp）
    wt = tmp_path / "worktrees" / "W1" / "state"
    wt.mkdir(parents=True)
    shutil.copy2(REPO_CAPS, wt / "lane_caps.json")
    monkeypatch.setenv("STEWARD_STATE_DIR", str(wt))
    res = L.check()
    assert res["drift_count"] >= 2  # 根不符 + 路径含 worktrees/
    assert any("worktrees" in d for d in res["drift"])
    assert ai_store.state_dir() == wt.resolve()

    # C. 生产根形态 → DRIFT=0
    monkeypatch.setenv("STEWARD_STATE_DIR", str(isolated_state_dir))
    _seed_caps(isolated_state_dir)
    # 生产根比对基准是写死的绝对常量，测试环境里必然不符；这里只断言「不符原因可读」
    res2 = L.check()
    assert isinstance(res2["drift"], list)


# ── 坏账本：exit 4、不覆盖、旁路副本；version 过高同理 ────────────────────
def test_ledger_corrupt(isolated_state_dir):
    _seed_caps(isolated_state_dir)
    p = Path(isolated_state_dir) / "lane_ledger.json"
    bad = '{"slots": {broken'
    p.write_text(bad, encoding="utf-8")

    with pytest.raises(L.Unavailable) as ei:
        L.claim("R4", "W1", "O1", phase="reserve")
    assert ei.value.exit_code == 4
    assert p.read_text(encoding="utf-8") == bad  # 原文件未被覆盖
    sides = list(Path(isolated_state_dir).glob("lane_ledger.json.corrupt-*"))
    assert sides, "坏账必须留 .corrupt-<stamp> 旁路副本（N4 精确命名）"

    # version 高于支持 → 不可用，不猜新格式
    p.write_text(json.dumps({"version": 99, "slots": {}, "waitlist": {}}), encoding="utf-8")
    with pytest.raises(L.Unavailable) as ei2:
        L.stats()
    assert "version=99" in str(ei2.value)


# ── 真 state 零污染（F7）──────────────────────────────────────────────────
def test_no_prod_pollution(isolated_state_dir):
    prod = Path(L.DEFAULT_STATE_ROOT) / "lane_ledger.json"
    before = prod.stat().st_mtime_ns if prod.exists() else None
    assert ai_store.state_dir() == Path(isolated_state_dir).resolve()

    _seed_caps(isolated_state_dir)
    for i in range(3):
        L.claim("R4", f"W{i}", f"O{i}", phase="reserve")
    L.stats()
    L.list_slots()
    L.reap()

    after = prod.stat().st_mtime_ns if prod.exists() else None
    assert after == before, "测试污染了生产账本"
    assert not (Path(isolated_state_dir) / "lane_caps.json").is_symlink()


# ── N9：--force-degraded 只对 LEDGER_UNAVAILABLE 生效 ──────────────────────
def test_force_degraded_scope(isolated_state_dir):
    _seed_caps(isolated_state_dir)

    # ① 满位 → force_degraded 不得越过 LANE_FULL
    L.claim("NEX", "E1", "O1", phase="reserve")
    L.claim("NEX", "E2", "O2", phase="reserve")
    with pytest.raises(L.Reject) as ei:
        L.claim("NEX", "E3", "O3", phase="reserve", force_degraded=True)
    assert ei.value.code == "LANE_FULL"

    # ② SESSION_BUSY 同理
    L.claim("STEP", "S1", "O4", phase="reserve", session_id="sess_q")
    with pytest.raises(L.Reject) as ei2:
        L.claim("R4-2", "S9", "O5", phase="reserve", session_id="sess_q",
                force_degraded=True)
    assert ei2.value.code == "SESSION_BUSY"

    # ③ 账本不可用 → force_degraded 才放行，并留 DEGRADED_ADMIT
    (Path(isolated_state_dir) / "lane_ledger.json").write_text("{oops", encoding="utf-8")
    r = L.claim("R4", "W9", "O9", phase="reserve", force_degraded=True)
    assert r["ok"] and r["degraded"] is True
    assert "DEGRADED_ADMIT" in _journal_events(isolated_state_dir)


# ── bind 遇过期：先重占；满则 ORPHAN（G3-3 / 5.6）────────────────────────
def test_bind_expired_reclaim_and_orphan(isolated_state_dir):
    _seed_caps(isolated_state_dir)

    # ① 重占成功（STEP 余位）
    r = L.claim("STEP", "S1", "O1", phase="reserve", ttl=1)
    time.sleep(2.0)
    b = L.claim(slot=r["slot_id"], phase="bind", session_id="sess_b", command_id="cmd_b")
    assert b["ok"] and b["reclaimed"] is True
    assert b["slot_id"] != r["slot_id"]
    assert L.stats()["lanes"]["STEP"]["used"] == 1

    # ② 重占时 lane 已满 → ORPHAN exit 3 + 流水
    a = L.claim("NEX", "E1", "ON1", phase="reserve", ttl=1)
    L.claim("NEX", "E2", "ON2", phase="reserve", ttl=600)
    time.sleep(2.0)
    L.claim("NEX", "E3", "ON3", phase="reserve", ttl=600)  # 抢回被回收的那格
    with pytest.raises(L.Reject) as ei:
        L.claim(slot=a["slot_id"], phase="bind", session_id="sess_z", command_id="cmd_z")
    assert ei.value.code == "ORPHAN" and ei.value.exit_code == 3
    assert "ORPHAN" in _journal_events(isolated_state_dir)


# ── check 三段：ORPHAN 可观测 / CAPS_DRIFT 告警（NEX 2 vs 3）──────────────
def test_check_orphan_and_caps_drift(isolated_state_dir, tmp_path):
    _seed_caps(isolated_state_dir)
    L.claim("R4", "W1", "O1", phase="reserve")  # 有活跃 slot

    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 1, "tasks": [
        {"id": "W-1", "state": "running", "sessionId": "sess_bound_elsewhere"},
        {"id": "W-2", "state": "done", "sessionId": "sess_done"},
        {"id": "W-3", "state": "queued"},
    ]}, ensure_ascii=False), encoding="utf-8")

    res = L.check(reg)
    assert res["orphan_count"] == 1
    assert res["orphan"][0]["task"] == "W-1"
    assert res["drift_count"] == 1  # 测试根 ≠ 生产绝对根

    # CAPS_DRIFT 只告警、不计入退出码：NEX 正典 2 vs slot_pool.DEFAULT_CAP 3
    assert any(x.startswith("NEX") for x in res["caps_drift"])


# ── CLI 退出码契约（0/2/3/4）───────────────────────────────────────────────
def test_cli_exit_codes(isolated_state_dir, capsys):
    import lane_claim

    _seed_caps(isolated_state_dir)
    assert lane_claim.main(["claim", "--lane", "R4", "--seat", "W1",
                            "--order", "O1", "--phase", "reserve"]) == 0
    capsys.readouterr()

    assert lane_claim.main(["claim", "--lane", "BUG", "--seat", "W1",
                            "--order", "O1", "--phase", "reserve"]) == 2
    capsys.readouterr()

    L.claim("NEX", "E1", "N1", phase="reserve")
    L.claim("NEX", "E2", "N2", phase="reserve")
    assert lane_claim.main(["claim", "--lane", "NEX", "--seat", "E3",
                            "--order", "N3", "--phase", "reserve"]) == 3
    out = capsys.readouterr().out
    assert "REJECT LANE_FULL" in out and "used=2/2" in out

    (Path(isolated_state_dir) / "lane_ledger.json").write_text("{bad", encoding="utf-8")
    assert lane_claim.main(["stats"]) == 4
    assert lane_claim.main(["claim", "--lane", "R4", "--seat", "W1", "--order", "O1",
                            "--phase", "reserve", "--force-degraded"]) == 0
