# -*- coding: utf-8 -*-
"""steward_board.py 并发写黑盒护栏（W-20261119-004 / NEXT-03L 派生卡）。

定位：基线 3 条用例（文件末尾，10 进程 add / 互斥读改写 / 备份保留）只覆盖
"单类操作不丢不坏"，没有任何用例证明 **8 个进程交错做 claim/state/progress/
add/done** 时不丢卡、不半写。本文件上半部分补这一格：

  正向：8 进程 × 20 轮交错写，全部经 subprocess 驱动**真实**
        scripts/steward_board.py 的 CLI（零 mock、零内存态、零改脚本）。
        每轮 8 个写进程全部静止后（文件门 barrier）由主进程快照，机械校验：
          - 快照 json.load 必须成功            -> 原子写，无半写
          - 已登记卡 ID 集合零丢失、零重复      -> 无 lost update
          - 每张卡字段集合 == cmd_add 全量 schema -> 无半字段、无野字段
          - state 合法、时间戳可解析、种子卡状态随轮次推进
  负向：只在**冻结副本**的 ai_store.py 上打补丁（禁锁 / 同名临时文件），
        真实 scripts/steward_board.py 与 scripts/ai_store.py 逐字节不动，
        必须复现 lost update / partial write 失败标识——证明护栏真的在承力，
        而不是"测试从来没见过失败"。
  红线：真实 state/registry.json 与 scripts/steward_board.py 的 sha256
        运行前后不变；运行中被他人改动 -> 立即中止并报「并发写者」。

夹具：冻结副本落 state/.audit_tmp/conc_<id>/（脚本副本 + ai_store 副本 +
合成台账 + 每卡证据文件 + 轮次门 + 每进程结果日志）；测试结束整目录删除，
只清自己的 conc_<id>，不动 .audit_tmp 里他人文件。锁目录经 STEWARD_STATE_DIR
重导到冻结副本内，真实 state/.locks 不被触碰。

用法：python -X utf8 -m pytest -q -s scripts/tests/test_board_concurrency.py
（预期耗时约 1-2 分钟：8×20 轮正向 + 两个负向对照 + 既有两套件复跑；
 STATS 行需 -s 才显示，供报告取数。）
"""
from __future__ import annotations
import os as _os
import sys as _sys
_SCRIPTS = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _SCRIPTS not in _sys.path:
    _sys.path.insert(0, _SCRIPTS)


import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest

import steward_board as sb

SCRIPTS = Path(sb.__file__).resolve().parent
BOARD = SCRIPTS / "steward_board.py"

# ---------------------------------------------------------------- 常量

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_BOARD = REPO_ROOT / "scripts" / "steward_board.py"
REAL_STORE = REPO_ROOT / "scripts" / "ai_store.py"
REAL_REGISTRY = REPO_ROOT / "state" / "registry.json"
AUDIT_TMP = REPO_ROOT / "state" / ".audit_tmp"

WORKERS = 8                      # 并发写进程数
ROUNDS = 20                      # 每进程轮数
ADD_ROUNDS = (2, 6, 10, 14, 18)  # 交错 add 的轮次（与 progress 交替）
NEG_ROUNDS = 10                  # 负向对照轮数（纯争抢，够复现即可）
FAT_NOTE = 16 * 1024             # 负向对照加肥 note：拉宽读-改-写窗口。
                                 # 上限受 Windows CreateProcess 32767 字符约束
                                 # （note 经命令行传给 CLI），16KB 留足余量。
BARRIER_TIMEOUT = 180.0

LEGAL_STATES = ("queued", "dispatched", "running", "blocked", "done",
                "failed", "abandoned")

# cmd_add 落卡的全量字段（scripts/steward_board.py:216-249）；少一个即半写
ADD_KEYS = {
    "id", "title", "goal", "acceptance", "boundary", "project", "workspace",
    "tier", "model", "priority", "depends_on", "max_attempts", "attempts",
    "state", "created_at", "updated_at", "last_check", "next_retry_at",
    "pulls", "sessionId", "last_command_id", "steer_events",
    "evidence_path", "evidence_sha256", "evidence_verified_at",
    "next_action", "note",
}
# 允许出现的附加字段：--source（本测试不传）/ state running 补的
# last_progress_at / done 补的 evidence_source（均见 steward_board.py 源码）
EXTRA_OK_KEYS = {"source", "last_progress_at", "evidence_source",
                 "evidence_sha_mode", "evidence_frozen_path",
                 "evidence_frozen_sha256", "evidence_frozen_at",
                 "reattest_history"}

TS_FIELDS = ("created_at", "updated_at", "last_check", "next_retry_at",
             "last_progress_at", "evidence_verified_at")

# 半写指纹：CLI 进程读台账时撞上半截/粘连内容时报的错（负向对照 N2 实测出现）
PARTIAL_WRITE_SIGS = ("JSONDecodeError", "Extra data", "Expecting value",
                      "Unterminated", "Errno 13")
# 同名临时文件撞车指纹：错误里出现固定临时名自身（os.replace 撞 WinError 32/5）
TMP_COLLISION_SIGS = (".CONC-FIXEDTMP",)

# 负向对照补丁：只追加到冻结副本 ai_store.py 末尾（真实文件零改动）。
# 依据：steward_board.locked() 走 ai_store.file_lock（steward_board.py:129-131），
# atomic_write_text 每轮重试都调 ai_store.tmp_name 取临时名（ai_store.py:91/107）。
PATCH_NO_LOCK = '''
# ---- 负向对照补丁 N1：禁锁（仅存在于冻结副本，真实 ai_store.py 零改动）----
import contextlib as _contextlib


def file_lock(lock_name, timeout=10.0, poll_interval=0.05):
    """禁锁：读-改-写不再互斥 -> 后写者覆盖先写者，必然丢更新（lost update）。"""

    @_contextlib.contextmanager
    def _noop():
        yield

    return _noop()
'''

PATCH_NO_LOCK_FIXED_TMP = PATCH_NO_LOCK + '''
# ---- 负向对照补丁 N2：同名临时文件（叠加禁锁，仅冻结副本）----
from pathlib import Path as _Path


def tmp_name(path):
    """固定临时名：多进程并发写同一 tmp，写-截断互相交错 -> 必然半写/乱码。"""
    p = _Path(path)
    return p.with_name(p.name + ".CONC-FIXEDTMP")
'''

# 并发写驱动进程源码（python -c 启动）：argv = 冻结根 worker序号 轮数 模式。
DRIVER_SRC = r'''
"""8 个并发写进程之一：独立驱动真实 steward_board.py CLI，轮末落门等放行。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    frozen = Path(sys.argv[1])
    worker = int(sys.argv[2])
    rounds = int(sys.argv[3])
    mode = sys.argv[4]
    fat = int(sys.argv[5]) if len(sys.argv) > 5 else 0

    script = frozen / "scripts" / "steward_board.py"
    reg = frozen / "state" / "registry.json"
    evdir = frozen / "evidence"
    gate = frozen / "gate"
    resdir = frozen / "results"
    resdir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["STEWARD_STATE_DIR"] = str(frozen / "locks")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    log = open(resdir / ("w%d.jsonl" % worker), "w", encoding="utf-8",
               newline="\n")

    def run(*args):
        cmd = [sys.executable, "-X", "utf8", str(script),
               "--registry", str(reg), "--json"] + [str(a) for a in args]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               env=env, timeout=180)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired as exc:
            return 124, "TIMEOUT %s" % (exc,)
        except OSError as exc:
            # 例如命令行超 Windows 32767 字符上限：记下来继续，绝不静默死亡
            # （驱动进程一死，轮次门永不落，主进程只能看到 barrier 超时）
            return 125, "OSERROR %s" % (exc,)

    def rec(round_no, op, rc, out):
        log.write(json.dumps({"round": round_no, "worker": worker, "op": op,
                              "rc": rc, "tail": out[-500:]},
                             ensure_ascii=False) + "\n")
        log.flush()

    sid = "SEED-%02d" % worker
    ev = evdir / (sid + ".md")
    ops = 0
    for r in range(rounds):
        if mode == "positive":
            if r == 0:
                rc, out = run("claim", "--id", sid, "--session", "sess-w%d" % worker)
                rec(r, "claim", rc, out); ops += 1
            elif r == 1:
                rc, out = run("state", "--id", sid, "--state", "running")
                rec(r, "state", rc, out); ops += 1
            elif r < rounds - 1:
                rc, out = run("progress", "--id", sid, "--note", "w%d-r%d" % (worker, r))
                rec(r, "progress", rc, out); ops += 1
                if r in (2, 6, 10, 14, 18):
                    aid = "ADD-%02d-%d" % (worker, r)
                    rc2, out2 = run("add", "--id", aid, "--title", "add card " + aid,
                                    "--acceptance", "mechanical")
                    rec(r, "add", rc2, out2); ops += 1
            else:
                rc, out = run("done", "--id", sid, "--evidence", str(ev))
                rec(r, "done", rc, out); ops += 1
        elif mode == "neg1":
            # 禁锁：8 进程纯 add 争抢同一台账 -> lost update
            aid = "NEG1-%02d-%d" % (worker, r)
            rc, out = run("add", "--id", aid, "--title", "neg add " + aid,
                          "--acceptance", "mechanical")
            rec(r, "add", rc, out); ops += 1
        else:
            # 禁锁 + 同名临时文件：加肥 note 拉宽窗口 -> 半写/乱码
            if r % 2 == 1:
                rc, out = run("progress", "--id", sid, "--note", "F" * fat)
                rec(r, "progress", rc, out); ops += 1
            aid = "NEG2-%02d-%d" % (worker, r)
            rc2, out2 = run("add", "--id", aid, "--title", "neg2 add " + aid,
                            "--acceptance", "mechanical")
            rec(r, "add", rc2, out2); ops += 1
        (gate / ("r%d.done.%d" % (r, worker))).write_text(str(os.getpid()),
                                                          encoding="utf-8")
        openf = gate / ("r%d.open" % r)
        for _ in range(120000):
            if openf.exists():
                break
            time.sleep(0.002)
    log.write(json.dumps({"round": rounds, "worker": worker, "op": "exit",
                          "rc": 0, "tail": "total_ops=%d" % ops}) + "\n")
    log.close()
    print("worker %d done ops=%d" % (worker, ops))


main()
'''


# ---------------------------------------------------------------- 基础工具

def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_registry_bytes(root: Path, tries: int = 100) -> bytes:
    """读冻结台账；写入方用 os.replace，Windows 上极短窗口内可能看不到文件。

    短暂重试后仍不存在则返回空字节：正向用例会因快照不合格而失败，负向用例
    记一次 JSON 错误（正是“无锁+同名临时文件”应复现的失败标识）。
    """
    path = root / "state" / "registry.json"
    for _ in range(tries):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            time.sleep(0.005)
    return b""


def driver_env(frozen_root: Path) -> dict:
    """驱动进程环境：锁目录重导到冻结副本内，真实 state/.locks 不碰。"""
    env = dict(os.environ)
    env["STEWARD_STATE_DIR"] = str(frozen_root / "locks")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _parse_iso(value) -> bool:
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def seed_card(cid: str) -> dict:
    """合成种子卡：字段与 cmd_add 落卡完全一致（steward_board.py:216-249）。"""
    now = "2026-11-19T09:00:00+08:00"
    return {
        "id": cid, "title": "seed " + cid, "goal": "goal " + cid,
        "acceptance": "mechanical", "boundary": "test-only",
        "project": "CONC", "workspace": "", "tier": "MEDIUM",
        "model": "new-provider-7/auto", "priority": 1, "depends_on": [],
        "max_attempts": 3, "attempts": 0, "state": "queued",
        "created_at": now, "updated_at": now, "last_check": None,
        "next_retry_at": None, "pulls": 0, "sessionId": None,
        "last_command_id": None, "steer_events": [], "evidence_path": None,
        "evidence_sha256": None, "evidence_verified_at": None,
        "next_action": "", "note": "seed",
    }


class FrozenBoard:
    """state/.audit_tmp/conc_<id>/ 下的冻结副本：脚本副本 + ai_store 副本 + 合成台账。"""

    def __init__(self, root: Path, patch_store: str | None = None):
        self.root = root
        for sub in ("scripts", "state", "evidence", "gate", "results", "locks"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        # 脚本副本逐字节复制（真实脚本零改动的物证：sha256 见 self.script_sha）
        shutil.copy2(REAL_BOARD, root / "scripts" / "steward_board.py")
        self.script_sha = sha256_file(root / "scripts" / "steward_board.py")
        assert self.script_sha == sha256_file(REAL_BOARD), "冻结副本与真实脚本不一致"
        src = REAL_STORE.read_text(encoding="utf-8")
        if patch_store:
            src = src + "\n\n" + patch_store.lstrip("\n")
        (root / "scripts" / "ai_store.py").write_text(
            src, encoding="utf-8", newline="\n")
        self.store_patched = bool(patch_store)
        # 合成台账 + 每张种子卡一份非空证据
        tasks = []
        for w in range(WORKERS):
            cid = "SEED-%02d" % w
            tasks.append(seed_card(cid))
            (root / "evidence" / (cid + ".md")).write_text(
                "frozen evidence for %s\n" % cid, encoding="utf-8", newline="\n")
        reg = root / "state" / "registry.json"
        reg.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False,
                                  indent=2) + "\n",
                       encoding="utf-8", newline="\n")


class RealFileGuard:
    """红线看护：真实 registry 与真实脚本运行中不得被改动（否则报「并发写者」）。

    归因诊断：改动内容若含本测试的合成标记（SEED-/ADD-/NEG1-/NEG2-/"CONC"），
    判为本测试泄漏写入（真 bug，必须中止）；否则判为外部并发写者（他人会话）。
    两种都按验收第 d 项立即中止，但报告里必须分开说。
    """

    #: 本测试合成台账独有的标记（冻结副本里才有，真实 registry 绝不应出现）
    LEAK_MARKERS = ("SEED-", "ADD-", "NEG1-", "NEG2-", '"CONC"')

    def __init__(self, paths, interval: float = 0.05):
        self.items = [(Path(p), sha256_file(p)) for p in paths]
        self.interval = interval
        self.trip = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _attribute(self, path: Path) -> str:
        """改动内容是否含本测试合成标记（仅对 registry 有意义）。"""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "unreadable"
        return ("leak-from-this-test" if any(m in text for m in self.LEAK_MARKERS)
                else "external-writer")

    def _loop(self) -> None:
        while not self._stop.is_set():
            for path, before in self.items:
                try:
                    now = sha256_file(path)
                except OSError as exc:
                    self.trip = {"path": str(path), "before": before,
                                 "after": "UNREADABLE:%s" % exc,
                                 "attribution": "unreadable",
                                 "at": datetime.now().isoformat(timespec="seconds")}
                    return
                if now != before:
                    self.trip = {"path": str(path), "before": before, "after": now,
                                 "attribution": self._attribute(path),
                                 "at": datetime.now().isoformat(timespec="seconds")}
                    return
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def verify_final(self) -> None:
        for path, before in self.items:
            assert sha256_file(path) == before, (
                "并发写者：%s 在测试前后发生变化" % path)


# ---------------------------------------------------------------- 快照校验

def expected_ids(round_idx: int, mode: str) -> set:
    """到本轮为止必须已在册的卡 ID 集合（正向模式）。"""
    ids = {"SEED-%02d" % w for w in range(WORKERS)}
    if mode == "positive":
        for w in range(WORKERS):
            for r in ADD_ROUNDS:
                if r <= round_idx:
                    ids.add("ADD-%02d-%d" % (w, r))
    return ids


def expected_seed_state(round_idx: int, rounds: int) -> str:
    if round_idx == 0:
        return "dispatched"
    if round_idx >= rounds - 1:
        return "done"
    return "running"


def snapshot_check(raw: bytes, round_idx: int, mode: str, rounds: int) -> dict:
    """一轮快照的机械校验：原子性 / ID 零丢失 / 字段完整 / 状态合法。"""
    out = {"round": round_idx, "ok": True, "json_error": "", "task_count": 0,
           "missing_ids": [], "dup_ids": [], "field_problems": [],
           "state_problems": [], "ts_problems": [], "seed_state_problems": []}
    try:
        reg = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # 半写/乱码在此现形
        out["ok"] = False
        out["json_error"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    tasks = reg.get("tasks")
    if not isinstance(tasks, list):
        out["ok"] = False
        out["json_error"] = "tasks 不是列表"
        return out
    out["task_count"] = len(tasks)
    ids = [t.get("id") for t in tasks if isinstance(t, dict)]
    seen, dups = set(), []
    for cid in ids:
        if cid in seen:
            dups.append(cid)
        seen.add(cid)
    out["dup_ids"] = dups
    out["missing_ids"] = sorted(expected_ids(round_idx, mode) - seen)
    for t in tasks:
        if not isinstance(t, dict):
            out["field_problems"].append("非 dict 条目")
            continue
        cid = t.get("id", "?")
        keys = set(t.keys())
        lack = ADD_KEYS - keys
        if lack:
            out["field_problems"].append("%s 缺 %s" % (cid, sorted(lack)))
        extra = keys - ADD_KEYS - EXTRA_OK_KEYS
        if extra:
            out["field_problems"].append("%s 多 %s" % (cid, sorted(extra)))
        if t.get("state") not in LEGAL_STATES:
            out["state_problems"].append("%s state=%r" % (cid, t.get("state")))
        for f in TS_FIELDS:
            if not _parse_iso(t.get(f)):
                out["ts_problems"].append("%s %s=%r" % (cid, f, t.get(f)))
        if mode == "positive" and isinstance(cid, str) and cid.startswith("SEED-"):
            want = expected_seed_state(round_idx, rounds)
            if t.get("state") != want:
                out["seed_state_problems"].append(
                    "%s r%d state=%s 期望 %s" % (cid, round_idx, t.get("state"), want))
            if round_idx == 0 and (t.get("attempts") != 1
                                   or not str(t.get("sessionId") or "").startswith("sess-")):
                out["seed_state_problems"].append(
                    "%s r0 attempts=%r sessionId=%r" % (cid, t.get("attempts"),
                                                        t.get("sessionId")))
            if round_idx >= rounds - 1 and not (t.get("evidence_path")
                                                and t.get("evidence_sha256")
                                                and t.get("evidence_verified_at")):
                out["seed_state_problems"].append(
                    "%s done 缺证据字段" % cid)
    bad = (out["json_error"] or out["missing_ids"] or out["dup_ids"]
           or out["field_problems"] or out["state_problems"]
           or out["ts_problems"] or out["seed_state_problems"])
    out["ok"] = not bad
    return out


# ---------------------------------------------------------------- 并发编排

def _kill(procs) -> None:
    for p in procs:
        if p.poll() is None:
            p.kill()


def run_concurrency(frozen: FrozenBoard, mode: str, rounds: int,
                    guard: RealFileGuard) -> tuple:
    """8 进程 × rounds 轮驱动真实 CLI；每轮主进程快照并机械校验。"""
    gate = frozen.root / "gate"
    started = time.time()
    procs = [
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", DRIVER_SRC,
             str(frozen.root), str(w), str(rounds), mode, str(FAT_NOTE)],
            cwd=str(REPO_ROOT), env=driver_env(frozen.root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        for w in range(WORKERS)
    ]
    snaps = []
    try:
        try:
            for r in range(rounds):
                deadline = time.time() + BARRIER_TIMEOUT
                while True:
                    if all((gate / ("r%d.done.%d" % (r, w))).exists()
                           for w in range(WORKERS)):
                        break
                    if guard.trip:
                        raise AssertionError(
                            "并发写者：%s 于 %s 被改动（before=%s after=%s，归因=%s）"
                            "——按验收立即中止"
                            % (guard.trip["path"], guard.trip["at"],
                               guard.trip["before"][:16], guard.trip["after"][:16],
                               guard.trip.get("attribution", "?")))
                    if time.time() > deadline:
                        missing = [w for w in range(WORKERS)
                                   if not (gate / ("r%d.done.%d" % (r, w))).exists()]
                        tails = {}
                        for w in missing:
                            log = frozen.root / "results" / ("w%d.jsonl" % w)
                            if log.exists():
                                lines = [x for x in
                                         log.read_text(encoding="utf-8").split("\n") if x]
                                tails[w] = lines[-1][-200:] if lines else "(空日志)"
                            else:
                                tails[w] = "(无日志)"
                        raise AssertionError(
                            "轮 %d barrier 超时：未落完成标记的 worker=%s；末条日志=%s"
                            % (r, missing, tails))
                    time.sleep(0.002)
                raw = read_registry_bytes(frozen.root)
                snaps.append(snapshot_check(raw, r, mode, rounds))
                (gate / ("r%d.open" % r)).write_text("1", encoding="utf-8")
        except BaseException:
            _kill(procs)   # 失败路径不放任驱动进程挂在门上
            raise
    finally:
        for p in procs:
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                _kill(procs)
        for p in procs:
            for pipe in (p.stdout, p.stderr):
                if pipe:
                    pipe.close()

    # 汇总每进程结果日志
    ops_total = ops_failed = lock_timeouts = 0
    failures = []
    for w in range(WORKERS):
        log = frozen.root / "results" / ("w%d.jsonl" % w)
        for line in log.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["op"] == "exit":
                continue
            ops_total += 1
            if rec["rc"] != 0:
                ops_failed += 1
                failures.append({"worker": w, "round": rec["round"], "op": rec["op"],
                                 "rc": rec["rc"], "tail": rec["tail"][-300:]})
            if "LOCK-TIMEOUT" in rec["tail"]:
                lock_timeouts += 1
    exit_nonzero = [w for (w, p) in enumerate(procs) if p.returncode != 0]
    # 半写指纹：CLI 读台账时撞上半截/粘连内容（快照层之外的第二种观测点）
    partial_write_sigs = sum(
        1 for f in failures
        if any(s in f["tail"] for s in PARTIAL_WRITE_SIGS))
    # 同名临时文件撞车：错误落在固定临时名自身（os.replace 分享冲突）
    tmp_collision_sigs = sum(
        1 for f in failures
        if any(s in f["tail"] for s in TMP_COLLISION_SIGS))

    final_raw = (frozen.root / "state" / "registry.json").read_bytes()
    try:
        final_tasks = len(json.loads(final_raw.decode("utf-8"))["tasks"])
    except Exception:
        final_tasks = -1
    expected_tasks = WORKERS + (WORKERS * rounds if mode != "positive"
                                else WORKERS * len(ADD_ROUNDS))
    stats = {
        "mode": mode, "workers": WORKERS, "rounds": rounds,
        "ops_total": ops_total, "ops_failed": ops_failed,
        "lock_timeouts": lock_timeouts, "op_failures": failures[:10],
        "worker_exit_nonzero": exit_nonzero,
        "snapshots": len(snaps), "json_errors": sum(1 for s in snaps if s["json_error"]),
        "id_loss_events": sum(1 for s in snaps if s["missing_ids"]),
        "dup_id_events": sum(1 for s in snaps if s["dup_ids"]),
        "field_problem_events": sum(1 for s in snaps if s["field_problems"]),
        "state_problem_events": sum(1 for s in snaps if s["state_problems"]),
        "ts_problem_events": sum(1 for s in snaps if s["ts_problems"]),
        "seed_state_mismatch_events": sum(1 for s in snaps if s["seed_state_problems"]),
        "final_tasks": final_tasks, "expected_tasks": expected_tasks,
        "lost_updates": max(0, expected_tasks - final_tasks),
        "partial_write_signatures": partial_write_sigs,
        "tmp_collision_signatures": tmp_collision_sigs,
        "wall_seconds": round(time.time() - started, 2),
        "frozen_script_sha256": frozen.script_sha,
        "store_patched": frozen.store_patched,
        "guard_trip": guard.trip,
    }
    return stats, snaps


def _emit(tag: str, stats: dict) -> None:
    print("BOARD-CONC-%s %s" % (tag, json.dumps(stats, ensure_ascii=False)))


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def frozen_factory():
    made: list[Path] = []

    def _make(tag: str, patch_store: str | None = None) -> FrozenBoard:
        root = AUDIT_TMP / ("conc_%s_%s" % (uuid.uuid4().hex[:8], tag))
        fb = FrozenBoard(root, patch_store=patch_store)
        made.append(root)
        return fb

    yield _make
    for root in made:  # 只删自己的 conc_<id>，不动 .audit_tmp 他人文件
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def real_guard():
    guard = RealFileGuard([REAL_REGISTRY, REAL_BOARD])
    guard.start()
    try:
        yield guard
    finally:
        guard.stop()
        guard.verify_final()


# ---------------------------------------------------------------- 正向主用例

def test_positive_8_workers_x_20_rounds_no_loss_no_halfwrite(frozen_factory,
                                                            real_guard):
    """8 进程 × 20 轮交错写：零丢卡、零半写、零锁超时、零字段残缺。"""
    frozen = frozen_factory("pos")
    stats, snaps = run_concurrency(frozen, "positive", ROUNDS, real_guard)

    assert real_guard.trip is None, (
        "并发写者：真实文件在运行中被改动 %s" % (real_guard.trip,))
    for s in snaps:
        assert s["ok"], "轮 %s 快照不合格: %s" % (
            s["round"], {k: v for k, v in s.items() if v and k != "round"})
    assert stats["json_errors"] == 0, "出现半写/乱码快照: %s" % stats
    assert stats["id_loss_events"] == 0
    assert stats["dup_id_events"] == 0
    assert stats["field_problem_events"] == 0
    assert stats["state_problem_events"] == 0
    assert stats["ts_problem_events"] == 0
    assert stats["seed_state_mismatch_events"] == 0
    assert stats["ops_failed"] == 0, "CLI 调用失败: %s" % stats["op_failures"]
    assert stats["lock_timeouts"] == 0, "出现 LOCK-TIMEOUT（锁等待超 10s）"
    assert stats["worker_exit_nonzero"] == []
    assert stats["final_tasks"] == stats["expected_tasks"] == WORKERS + 40
    # 终态抽点：8 种子 done 带证据，40 新增 queued
    final = json.loads((frozen.root / "state" / "registry.json")
                       .read_text(encoding="utf-8"))["tasks"]
    by_id = {t["id"]: t for t in final}
    assert sum(1 for t in final if t["state"] == "done") == WORKERS
    assert sum(1 for t in final if t["state"] == "queued") == 40
    for w in range(WORKERS):
        card = by_id["SEED-%02d" % w]
        assert card["attempts"] == 1 and card["evidence_sha256"]
        assert card["evidence_path"].endswith("SEED-%02d.md" % w)
    _emit("STATS", stats)


# ---------------------------------------------------------------- 负向对照

def test_negative_control_lock_disabled_reproduces_lost_update(frozen_factory,
                                                               real_guard):
    """N1 禁锁（仅冻结副本）：必须复现 lost update，否则护栏未经受证。"""
    frozen = frozen_factory("neg1", patch_store=PATCH_NO_LOCK)
    stats, snaps = run_concurrency(frozen, "neg1", NEG_ROUNDS, real_guard)

    assert real_guard.trip is None, "并发写者：%s" % (real_guard.trip,)
    markers = []
    if stats["lost_updates"] > 0:
        markers.append("lost_update")
    if stats["json_errors"] > 0 or stats["partial_write_signatures"] > 0:
        markers.append("partial_write")
    stats["markers"] = markers
    assert markers, ("负向对照未复现任何失败标识（lost_updates=%d json_errors=%d "
                     "partial_write_signatures=%d）——护栏可能未被真正检验"
                     % (stats["lost_updates"], stats["json_errors"],
                        stats["partial_write_signatures"]))
    assert "lost_update" in markers, (
        "禁锁后未复现 lost update：expected=%d final=%d"
        % (stats["expected_tasks"], stats["final_tasks"]))
    assert stats["frozen_script_sha256"] == sha256_file(REAL_BOARD)
    _emit("NEG1", stats)


def test_negative_control_same_tmp_name_reproduces_failure(frozen_factory,
                                                           real_guard):
    """N2 同名临时文件 + 禁锁（仅冻结副本）：必须复现半写或 lost update。"""
    frozen = frozen_factory("neg2", patch_store=PATCH_NO_LOCK_FIXED_TMP)
    stats, snaps = run_concurrency(frozen, "neg2", NEG_ROUNDS, real_guard)

    assert real_guard.trip is None, "并发写者：%s" % (real_guard.trip,)
    markers = []
    if stats["lost_updates"] > 0:
        markers.append("lost_update")
    if stats["json_errors"] > 0 or stats["partial_write_signatures"] > 0:
        markers.append("partial_write")
    if stats["tmp_collision_signatures"] > 0:
        markers.append("tmp_name_collision")
    stats["markers"] = markers
    assert markers, ("负向对照 N2 未复现任何失败标识（lost_updates=%d "
                     "json_errors=%d partial_write_signatures=%d "
                     "tmp_collision_signatures=%d dup=%d）"
                     % (stats["lost_updates"], stats["json_errors"],
                        stats["partial_write_signatures"],
                        stats["tmp_collision_signatures"],
                        sum(1 for s in snaps if s["dup_ids"])))
    assert stats["frozen_script_sha256"] == sha256_file(REAL_BOARD)
    _emit("NEG2", stats)


def test_frozen_script_is_byte_identical_and_store_patch_is_local(frozen_factory):
    """冻结副本的 steward_board.py 与真实脚本逐字节一致；补丁只落在副本。"""
    plain = frozen_factory("ident")
    patched = frozen_factory("identp", patch_store=PATCH_NO_LOCK)
    real_board_sha = sha256_file(REAL_BOARD)
    assert plain.script_sha == real_board_sha
    assert patched.script_sha == real_board_sha
    assert plain.store_patched is False
    assert patched.store_patched is True
    # 未打补丁的副本 ai_store 与真实一致；打补丁的副本只在末尾追加
    assert (plain.root / "scripts" / "ai_store.py").read_bytes() == \
        REAL_STORE.read_bytes()
    assert "负向对照补丁 N1：禁锁" in \
        (patched.root / "scripts" / "ai_store.py").read_text(encoding="utf-8")
    # 真实 ai_store.py 绝不被写入补丁（补丁只存在于冻结副本）
    assert "负向对照补丁" not in REAL_STORE.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 既有套件复跑

def _run_existing_suite(script: Path, tmp_path: Path) -> dict:
    """既有套件是独立脚本（非 pytest 用例）：python -X utf8 <script> -> 退出码 0。"""
    env = dict(os.environ)
    env["STEWARD_STATE_DIR"] = str(tmp_path / ("locks-" + script.stem))
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=600)
    tail = (proc.stdout or "").strip().split("\n")[-3:]
    return {"script": script.name, "rc": proc.returncode, "tail": tail}


def test_existing_board_and_cycle_suites_stay_green(tmp_path, real_guard):
    """既有 scripts/test_board.py 与 scripts/test_cycle.py 必须全绿。"""
    board = _run_existing_suite(REPO_ROOT / "scripts" / "test_board.py", tmp_path)
    cycle = _run_existing_suite(REPO_ROOT / "scripts" / "test_cycle.py", tmp_path)
    for res in (board, cycle):
        assert res["rc"] == 0, "%s 退出码 %d，尾部: %s" % (
            res["script"], res["rc"], res["tail"])
        assert any("passed /" in line for line in res["tail"]), (
            "%s 未见 'N passed / M total' 汇总行" % res["script"])
        assert not any("[FAIL]" in line for line in res["tail"])
    assert real_guard.trip is None, "并发写者：%s" % (real_guard.trip,)
    print("BOARD-CONC-SUITES %s" % json.dumps(
        {"board": board, "cycle": cycle}, ensure_ascii=False))


# ================================================================
# 以下为 git 基线既有 3 条用例（提交 bb09321，原样保留，不删不减）
# ================================================================

def _empty_registry(path: Path) -> None:
    sb.save(str(path), {"tasks": [], "decisions": []})


def _add_cmd(reg_path: Path, idx: int) -> list[str]:
    return [sys.executable, "-X", "utf8", str(BOARD),
            "--registry", str(reg_path), "--json",
            "add", "--title", f"并发卡-{idx}",
            "--acceptance", "pytest scripts/tests -q 全绿",
            "--project", "ai-steward"]


def test_ten_concurrent_adds_no_loss(tmp_path, isolated_state_dir):
    reg = tmp_path / "registry.json"
    _empty_registry(reg)

    procs = [
        subprocess.Popen(_add_cmd(reg, i), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
        for i in range(10)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        results.append((p.returncode, out, err))

    assert all(rc == 0 for rc, _, _ in results), [
        (rc, err[-500:]) for rc, _, err in results if rc != 0]

    data = sb.load(str(reg))
    assert len(data["tasks"]) == 10
    assert sorted(t["title"] for t in data["tasks"]) == sorted(f"并发卡-{i}" for i in range(10))
    ids = [t["id"] for t in data["tasks"]]
    assert len(set(ids)) == 10
    assert all(t["state"] == "queued" for t in data["tasks"])
    assert data["decisions"] == []

    # 互斥真的生效过：状态目录下留下 .locks/；台账目录没有原子写孤儿
    assert (isolated_state_dir / ".locks").is_dir()
    leftovers = [p.name for p in reg.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def _bump_read_modify_write(reg_path: Path) -> None:
    with sb.locked(str(reg_path)):
        reg = sb.load(str(reg_path))
        value = int(reg.get("counter", 0))
        time.sleep(0.02)  # 放大读改写窗口：无锁实现必然丢更新
        reg["counter"] = value + 1
        sb.save(str(reg_path), reg)


def test_locked_serializes_read_modify_write(tmp_path):
    reg = tmp_path / "registry.json"
    _empty_registry(reg)

    threads = [threading.Thread(target=_bump_read_modify_write, args=(reg,))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert sb.load(str(reg)).get("counter") == 10


def test_save_keeps_backup_and_roundtrips(tmp_path):
    reg = tmp_path / "registry.json"
    _empty_registry(reg)
    sb.save(str(reg), {"tasks": [{"id": "W-1"}], "decisions": []})

    assert sb.load(str(reg))["tasks"][0]["id"] == "W-1"
    assert list(reg.parent.glob("registry.json.bak-*")), "覆盖写应留下同秒备份"
    assert not (reg.parent / (reg.name + ".tmp")).exists()
