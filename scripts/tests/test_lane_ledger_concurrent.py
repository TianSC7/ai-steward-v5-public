# -*- coding: utf-8 -*-
"""真并发计数测试（C-20261120-003 · 契约验收 1/2）

**不是线程假测**：`multiprocessing.get_context("spawn")` 起 8/4 个**独立进程**（Windows 默认
spawn，非 fork），各进程独立执行 `lane_claim.main()`；`STEWARD_STATE_DIR` 显式指向 tmp，
`lane_caps.json` 显式 copy（R1 夹具腿①）。用 `Barrier` 同步起跑，并记录各自起跑时刻，
断言最大启动偏差 < 500ms —— 证明是并发而非串行。

另附 G3 备胎：`subprocess.Popen` 起独立 CLI 进程 + 文件栅栏（gate file）放行——机制不同，
同样是多进程真并发。

断言口径（plan §C6 / §D）：
  - cap=2 的 NEX 用 4 进程 → OK=2 / REJECT=2（对应契约「N 与 N+2」）
  - cap=6 的 R4 用 8 进程 → OK=6 / REJECT=2
  - 账本 used == cap、活跃 slot_id 无重复、退出码明细可读
  - 同键 4 进程 → OK=1 / REPLAY=3、used==1
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
REPO_CAPS = SCRIPTS.parent / "state" / "lane_caps.json"
CLI = SCRIPTS / "lane_claim.py"


# ── 夹具 ───────────────────────────────────────────────────────────────────
def _fresh_state(tmp_path: Path) -> Path:
    state = tmp_path / "lane_test"
    state.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_CAPS, state / "lane_caps.json")  # caps 显式 copy，不靠「恰好有文件」
    return state


def _ledger(state: Path) -> dict:
    return json.loads((state / "lane_ledger.json").read_text(encoding="utf-8"))


def _active(state: Path) -> list[dict]:
    return [s for s in _ledger(state)["slots"].values()
            if s.get("state") in ("claimed", "leased")]


# ── spawn worker（模块级，Windows 可 pickle）─────────────────────────────────
def _worker(state_dir: str, argv: list, barrier, outfile: str) -> None:
    os.environ["STEWARD_STATE_DIR"] = state_dir
    sys.path.insert(0, str(SCRIPTS))
    import contextlib
    import io

    import lane_claim

    buf = io.StringIO()
    try:
        barrier.wait(timeout=60)
        t0 = time.time()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = lane_claim.main(argv)
        payload = {"rc": rc, "stdout": buf.getvalue(), "argv": argv, "t0": t0}
    except BaseException as exc:  # noqa: BLE001 —— 子进程错误要带回去，不能静默
        payload = {"rc": -1, "error": repr(exc), "argv": argv, "t0": 0.0}
    Path(outfile).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run_spawn(state: Path, argvs: list[list[str]], timeout: float = 180) -> list[dict]:
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(len(argvs))
    procs, outs = [], []
    for i, argv in enumerate(argvs):
        out = str(state / f"_proc_out_{i}.json")
        outs.append(out)
        p = ctx.Process(target=_worker, args=(str(state), argv + ["--json"], barrier, out))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(timeout)
    results = []
    for i, out in enumerate(outs):
        p = Path(out)
        if not p.exists():
            raise AssertionError(f"子进程 {i} 未产出结果（exitcode={procs[i].exitcode}）")
        results.append(json.loads(p.read_text(encoding="utf-8")))
    return results


def _claim_argv(lane: str, seat: str, order: str, session: str, command: str,
                extra: list[str] | None = None) -> list[str]:
    argv = ["claim", "--lane", lane, "--seat", seat, "--order", order,
            "--phase", "reserve", "--session", session, "--command", command]
    return argv + (extra or [])


# ── 契约验收 1（N 与 N+2）：cap=2 用 4 进程 ─────────────────────────────────
def test_overlimit_cap2_four_procs(tmp_path):
    state = _fresh_state(tmp_path)
    argvs = [_claim_argv("NEX", f"E{i}", "C-CONC-N2", f"sess_n{i}", f"cmd_n{i}")
             for i in range(1, 5)]
    res = _run_spawn(state, argvs)

    ok = [r for r in res if r["rc"] == 0]
    rej = [r for r in res if r["rc"] == 3]
    assert len(ok) == 2, f"应恰 2 个成功：{[r['rc'] for r in res]}"
    assert len(rej) == 2, f"应恰 2 个被拒：{[r['rc'] for r in res]}"
    assert all("LANE_FULL" in r.get("stdout", "") and "used=2/2" in r.get("stdout", "")
               for r in rej), [r.get("stdout") for r in rej]

    active = _active(state)
    assert len(active) == 2 and len({s["slot_id"] for s in active}) == 2
    assert all(s["lane"] == "NEX" for s in active)

    spread = max(r["t0"] for r in res) - min(r["t0"] for r in res)
    assert spread < 0.5, f"起跑偏差 {spread:.3f}s ≥ 500ms，疑似串行"
    assert sorted(r["rc"] for r in res) == [0, 0, 3, 3]


# ── 契约验收 1：cap=6 用 8 进程（R4，对应 plan §D E2）─────────────────────
def test_overlimit_cap6_eight_procs(tmp_path):
    state = _fresh_state(tmp_path)
    argvs = [_claim_argv("R4", f"W{i}", "C-CONC-R4", f"sess_r{i}", f"cmd_r{i}")
             for i in range(1, 9)]
    res = _run_spawn(state, argvs)

    ok = [r for r in res if r["rc"] == 0]
    rej = [r for r in res if r["rc"] == 3]
    assert len(ok) == 6, f"OK 应 == 6：{[r['rc'] for r in res]}"
    assert len(rej) == 2, f"REJECT 应 == 2：{[r['rc'] for r in res]}"
    assert all("LANE_FULL" in r.get("stdout", "") for r in rej)

    active = _active(state)
    assert len(active) == 6
    assert len({s["slot_id"] for s in active}) == 6, "slot_id 出现重复"
    assert sorted(r["rc"] for r in res) == [0, 0, 0, 0, 0, 0, 3, 3]

    # 账本自证：stats 的 used 与 cap 一致
    sys.path.insert(0, str(SCRIPTS))
    import lane_ledger as L
    old = os.environ.get("STEWARD_STATE_DIR")
    os.environ["STEWARD_STATE_DIR"] = str(state)
    try:
        st = L.stats()
        assert st["lanes"]["R4"]["used"] == 6 and st["lanes"]["R4"]["cap"] == 6
        assert st["lanes"]["R4"]["free"] == 0
    finally:
        if old is None:
            os.environ.pop("STEWARD_STATE_DIR", None)
        else:
            os.environ["STEWARD_STATE_DIR"] = old


# ── 契约验收 2：同幂等键 4 进程并发 → OK=1 / REPLAY=3 / used==1 ────────────
def test_idempotent_same_key_four_procs(tmp_path):
    state = _fresh_state(tmp_path)
    argvs = [_claim_argv("R4", "W1", "C-CONC-IDEM", f"sess_i{i}", f"cmd_i{i}",
                         ["--prep-id", "prep_fixed_same"])
             for i in range(1, 5)]
    res = _run_spawn(state, argvs)

    ok = [r for r in res if r["rc"] == 0]
    assert len(ok) == 4, f"全部应 exit 0（1 次 CLAIM + 3 次重放）：{[r['rc'] for r in res]}"
    stdout_all = "".join(r.get("stdout", "") for r in res)
    assert stdout_all.count("REPLAY") == 3, stdout_all

    active = _active(state)
    assert len(active) == 1, f"同键并发不得新增占位：{len(active)} 个活跃 slot"
    assert active[0]["lane"] == "R4"

    slots = list(_ledger(state)["slots"])
    assert len(slots) == 1, f"账本 slot 数应恒为 1，实为 {slots}"


# ── G3 备胎：Popen 独立 CLI 进程 + 文件栅栏（机制不同的多进程真并发）────────
_POPEN_WRAPPER = (
    "import os, sys, time\n"
    "gate, scripts = sys.argv[1], sys.argv[2]\n"
    "argv = sys.argv[3:]\n"
    "t0 = time.monotonic()\n"
    "while not os.path.exists(gate):\n"
    "    time.sleep(0.005)\n"
    "    if time.monotonic() - t0 > 60:\n"
    "        break\n"
    "sys.path.insert(0, scripts)\n"
    "import lane_claim\n"
    "raise SystemExit(lane_claim.main(argv))\n"
)


def test_overlimit_popen_fence(tmp_path):
    state = _fresh_state(tmp_path)
    gate = state / "_gate"
    env = os.environ.copy()
    env["STEWARD_STATE_DIR"] = str(state)
    env["PYTHONIOENCODING"] = "utf-8"

    procs = []
    for i in range(1, 5):
        argv = _claim_argv("NEX", f"E{i}", "C-CONC-FENCE", f"sess_f{i}", f"cmd_f{i}")
        procs.append(subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", _POPEN_WRAPPER, str(gate), str(SCRIPTS)]
            + argv,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    time.sleep(0.4)          # 让 4 个进程全部到位（它们在 gate 出现前都阻塞着）
    gate.write_text("go", encoding="utf-8")

    results = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        results.append({"rc": p.returncode, "stdout": out, "stderr": err})

    rcs = sorted(r["rc"] for r in results)
    assert rcs == [0, 0, 3, 3], f"栅栏并发应 2 成功 2 拒绝，实为 {rcs}；{results}"
    assert len(_active(state)) == 2
