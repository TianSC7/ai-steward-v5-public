# -*- coding: utf-8 -*-
"""steward_board.py `--sha-mode raw|lf` / `--extra KEY=VALUE` / PROTECTED_FIELDS 定版测试。

卡面依据：W-20261119-007（解锁 NEXT-03 原卡）。覆盖验收第 2 项四项 + 第 4 项只读兼容：

  1) CRLF 文件 raw 与 lf 两口径 sha256 差异（lf == git blob 口径）；
  2) `--extra KEY=VALUE` 正常追加/覆盖自定义字段（值去空格）；
  3) `--extra state/attempts/evidence_path/evidence_sha256` 等 PROTECTED_FIELDS
     被拒（非零退出 + PROTECTED-FIELD 报错，且台账字节不变——save 未被执行）；
  4) 默认无 `--sha-mode` 时行为与旧版一致（raw 口径，按工作区字节）；
  5) 只读向后兼容：`get`/`list` 对现有台账回读零差异，真 registry sha256 前后不变。

写操作全部落在 tmp_path 下的临时台账副本；真实 state/registry.json 全程只读
（第 5 项显式前后哈希自证）。锁/状态目录由 conftest 的 isolated_state_dir 隔到 tmp。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import steward_board as sb  # noqa: E402

BOARD = Path(sb.__file__).resolve()
REAL_REGISTRY = Path(sb.DEFAULT_REGISTRY)
NOW = "2026-11-19T10:00:00+08:00"
HAS_GIT = shutil.which("git") is not None

# cmd_done 里 --extra 的拒绝分支先于任何字段落账；这些键一个都不能被改
PROTECTED = sorted(sb.PROTECTED_FIELDS)


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_cli(registry: Path, *args: str, now: str | None = NOW) -> subprocess.CompletedProcess:
    """驱动真实 steward_board.py CLI（零 mock）；--registry 始终指向临时副本。

    统一带 --json，stdout 恒为可解析 JSON（渲染行不带 id 字段，无法断言）。
    """
    cmd = [sys.executable, "-X", "utf8", str(BOARD),
           "--registry", str(registry), "--json"]
    if now:
        cmd += ["--now", now]
    return subprocess.run(cmd + list(args), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


@pytest.fixture
def reg(tmp_path: Path) -> Path:
    """空台账副本（写测试只碰它）。"""
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"tasks": [], "decisions": []}, ensure_ascii=False),
                 encoding="utf-8")
    return p


def seed_card(reg: Path, title: str = "定版卡") -> str:
    """add → claim → state running，返回卡 ID（done 由各用例自己调）。"""
    r = run_cli(reg, "add", "--title", title, "--acceptance", "pytest scripts/tests -q 全绿")
    assert r.returncode == 0, r.stderr
    cid = json.loads(r.stdout)["id"]
    assert run_cli(reg, "claim", "--id", cid, "--session", "sess_w007").returncode == 0
    assert run_cli(reg, "state", "--id", cid, "--state", "running").returncode == 0
    return cid


def done_card(reg: Path, cid: str, evidence: Path, *extra: str) -> subprocess.CompletedProcess:
    return run_cli(reg, "done", "--id", cid, "--evidence", str(evidence),
                   "--summary", "定版自测", *extra)


def card_of(reg: Path, cid: str) -> dict:
    r = run_cli(reg, "get", "--id", cid)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---------------------------------------------------------------- 验收 2.1：双口径

def test_sha256_modes_unit_raw_lf_and_chunked_path(tmp_path: Path):
    """raw=工作区字节；lf=CRLF→LF 归一；两者在纯 LF 文件上恒等，在 CRLF 上必不等。"""
    lf_only = tmp_path / "lf.md"
    lf_only.write_bytes(b"line1\nline2\n")
    assert sb.sha256(str(lf_only)) == sb.sha256(str(lf_only), "lf")
    assert sb.sha256(str(lf_only)) == hashlib.sha256(b"line1\nline2\n").hexdigest()

    crlf = tmp_path / "crlf.md"
    body = b"line1\r\nline2\r\n"
    crlf.write_bytes(body)
    raw = sb.sha256(str(crlf))
    lf = sb.sha256(str(crlf), "lf")
    assert raw == hashlib.sha256(body).hexdigest()
    assert lf == hashlib.sha256(b"line1\nline2\n").hexdigest()
    assert raw != lf, "CRLF 文件两口径必须不同，否则 --sha-mode 无意义"

    # 覆盖 raw 的 64KB 分块读路径（大文件走 iter(read(65536))）
    big = tmp_path / "big.md"
    chunk = (b"x" * 63 + b"\r\n") * 1500          # ≈94.5 KB
    big.write_bytes(chunk)
    assert sb.sha256(str(big)) == hashlib.sha256(chunk).hexdigest()
    assert sb.sha256(str(big), "lf") == hashlib.sha256(chunk.replace(b"\r\n", b"\n")).hexdigest()


def test_sha256_rejects_unknown_mode(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_bytes(b"x\n")
    with pytest.raises(SystemExit) as exc:
        sb.sha256(str(p), "md5")
    assert "BAD-SHA-MODE" in str(exc.value)


@pytest.mark.skipif(not HAS_GIT, reason="git 不可用")
def test_lf_caliber_equals_git_blob_of_committed_crlf_file(tmp_path: Path):
    """lf 口径 == HEAD git blob 口径（跨行尾稳定），raw 则不等。"""
    root = tmp_path / "repo"
    (root / "reports" / "evidence").mkdir(parents=True)
    ev = root / "reports" / "evidence" / "W-LF.md"
    ev.write_bytes("【结论】成功\r\n【证据】pytest 12 passed\r\n".encode("utf-8"))

    def git(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *a], cwd=str(root), capture_output=True,
                              text=True, timeout=120)

    assert git("init", "-q").returncode == 0
    assert git("add", "-A").returncode == 0
    assert git("-c", "user.email=t@example.com", "-c", "user.name=t",
               "commit", "-q", "-m", "init").returncode == 0

    blob = git("show", "HEAD:reports/evidence/W-LF.md")
    assert blob.returncode == 0, blob.stderr
    blob_bytes = blob.stdout.encode("utf-8")
    assert sb.sha256(str(ev), "lf") == hashlib.sha256(blob_bytes).hexdigest()
    assert sb.sha256(str(ev)) != sb.sha256(str(ev), "lf")


# ---------------------------------------------------------------- 验收 2.4：默认 raw

def test_done_default_sha_mode_is_raw(reg: Path, tmp_path: Path):
    """无 --sha-mode 时按旧版 raw 口径记哈希（= 工作区字节），与 lf 不同。"""
    ev = tmp_path / "W-DEFAULT.md"
    ev.write_bytes("【结论】成功\r\n【证据】raw 默认\r\n".encode("utf-8"))
    cid = seed_card(reg)
    r = done_card(reg, cid, ev)
    assert r.returncode == 0, r.stderr
    t = card_of(reg, cid)
    assert t["state"] == "done"
    assert t["evidence_sha256"] == sb.sha256(str(ev)) == sb.sha256(str(ev), "raw")
    assert t["evidence_sha256"] != sb.sha256(str(ev), "lf")


def test_done_sha_mode_lf_records_lf_caliber(reg: Path, tmp_path: Path):
    ev = tmp_path / "W-LFMODE.md"
    ev.write_bytes("【结论】成功\r\n【证据】lf 口径\r\n".encode("utf-8"))
    cid = seed_card(reg)
    r = done_card(reg, cid, ev, "--sha-mode", "lf")
    assert r.returncode == 0, r.stderr
    assert card_of(reg, cid)["evidence_sha256"] == sb.sha256(str(ev), "lf")


def test_done_rejects_unknown_sha_mode(reg: Path, tmp_path: Path):
    ev = tmp_path / "W-BADMODE.md"
    ev.write_bytes(b"x\n")
    cid = seed_card(reg)
    r = done_card(reg, cid, ev, "--sha-mode", "md5")
    assert r.returncode != 0
    assert "invalid choice" in r.stderr and "'md5'" in r.stderr


# ---------------------------------------------------------------- 验收 2.2：--extra 正常

def test_extra_appends_custom_fields_and_trims_values(reg: Path, tmp_path: Path):
    ev = tmp_path / "W-EXTRA.md"
    ev.write_bytes("【结论】成功\r\n".encode("utf-8"))
    cid = seed_card(reg)
    r = done_card(reg, cid, ev,
                  "--extra", "canonical_sha256=b13a7623",
                  "--extra", "merge_commit=c0ee2344",
                  "--extra", "  anchor_note  =  留档说明  ")
    assert r.returncode == 0, r.stderr
    t = card_of(reg, cid)
    assert t["canonical_sha256"] == "b13a7623"
    assert t["merge_commit"] == "c0ee2344"
    assert t["anchor_note"] == "留档说明"          # KEY/VALUE 两侧都去空格
    assert "canonical_sha256" not in sb.PROTECTED_FIELDS
    # 追加不影响 done 自己写的主字段
    assert t["evidence_sha256"] == sb.sha256(str(ev))
    assert t["state"] == "done"


def test_extra_overwrites_existing_nonprotected_field(reg: Path, tmp_path: Path):
    """非保护字段可被 --extra 覆盖（锚点修复场景：补字段/改值）。"""
    ev = tmp_path / "W-OVER.md"
    ev.write_bytes(b"x\n")
    cid = seed_card(reg)
    assert done_card(reg, cid, ev, "--extra", "anchor_note=旧值").returncode == 0
    assert card_of(reg, cid)["anchor_note"] == "旧值"
    # 回到 running 再 done 一次，验证覆盖而非只追加
    assert run_cli(reg, "state", "--id", cid, "--state", "running", "--force").returncode == 0
    assert done_card(reg, cid, ev, "--extra", "anchor_note=新值").returncode == 0
    assert card_of(reg, cid)["anchor_note"] == "新值"


# ---------------------------------------------------------------- 验收 2.3：保护字段拒绝

@pytest.mark.parametrize("key", PROTECTED)
def test_extra_rejects_protected_field(reg: Path, tmp_path: Path, key: str):
    """每个 PROTECTED_FIELDS 键都必须被拒：非零退出 + PROTECTED-FIELD 报错 + 台账字节不变。"""
    ev = tmp_path / "W-PROT.md"
    ev.write_bytes(b"x\n")
    cid = seed_card(reg)
    before = reg.read_bytes()
    r = done_card(reg, cid, ev, "--extra", "%s=篡改值" % key)
    assert r.returncode != 0, "保护字段 %s 竟被接受" % key
    assert "PROTECTED-FIELD %s" % key in r.stderr, r.stderr
    assert reg.read_bytes() == before, "拒绝必须发生在 save 之前（台账零写入）"


@pytest.mark.parametrize("bad", ["NOEQUALS", "=onlyvalue", "   =onlyvalue"])
def test_extra_rejects_malformed_kv(reg: Path, tmp_path: Path, bad: str):
    ev = tmp_path / "W-MALFORMED.md"
    ev.write_bytes(b"x\n")
    cid = seed_card(reg)
    before = reg.read_bytes()
    r = done_card(reg, cid, ev, "--extra", bad)
    assert r.returncode != 0
    assert "BAD-EXTRA" in r.stderr, r.stderr
    assert reg.read_bytes() == before


def test_extra_key_is_trimmed_before_protected_check(reg: Path, tmp_path: Path):
    """带空格的键先 strip 再比对——不能用 "  state  " 绕过保护。"""
    ev = tmp_path / "W-TRIM.md"
    ev.write_bytes(b"x\n")
    cid = seed_card(reg)
    before = reg.read_bytes()
    r = done_card(reg, cid, ev, "--extra", "  state  = queued")
    assert r.returncode != 0
    assert "PROTECTED-FIELD state" in r.stderr, r.stderr
    assert reg.read_bytes() == before


def test_extra_case_variant_creates_distinct_key_not_a_bypass(reg: Path, tmp_path: Path):
    """大小写变体不在 PROTECTED_FIELDS 内：写入的是新键，真实 state 字段未被改动。

    即"State=queued"不会把卡踢回 queued（读方只认小写 state）——记为已知边界，
    不是绕过；真实主字段仍由 done 自身逻辑独占。
    """
    ev = tmp_path / "W-CASE.md"
    ev.write_bytes(b"x\n")
    cid = seed_card(reg)
    r = done_card(reg, cid, ev, "--extra", "State=queued", "--extra", "STATE=queued")
    assert r.returncode == 0, r.stderr
    t = card_of(reg, cid)
    assert t["state"] == "done" and t["State"] == "queued" and t["STATE"] == "queued"
    assert t["evidence_sha256"] == sb.sha256(str(ev))


# ---------------------------------------------------------------- 验收 4：只读向后兼容

def test_read_commands_leave_copy_byte_identical(reg: Path, tmp_path: Path):
    """get/list 对台账副本零写入（无锁、无 save）；回读内容与文件一致。"""
    ev = tmp_path / "W-READ.md"
    ev.write_bytes("【结论】成功\r\n".encode("utf-8"))
    cid = seed_card(reg)
    assert done_card(reg, cid, ev, "--sha-mode", "lf",
                     "--extra", "merge_commit=c0ee2344").returncode == 0
    before = reg.read_bytes()

    ls = run_cli(reg, "list")
    assert ls.returncode == 0 and cid in ls.stdout
    ls_done = run_cli(reg, "list", "--state", "done")
    assert ls_done.returncode == 0
    assert [t["id"] for t in json.loads(ls_done.stdout)] == [cid]

    got = run_cli(reg, "get", "--id", cid)
    assert got.returncode == 0
    assert json.loads(got.stdout) == json.loads(before.decode("utf-8"))["tasks"][0]

    assert reg.read_bytes() == before, "只读命令不得改动台账"


def test_real_registry_untouched_by_reads():
    """真 state/registry.json 前后 sha256 不变；本文件所有写测试都用临时副本。"""
    assert REAL_REGISTRY.is_file(), "真实台账缺失：%s" % REAL_REGISTRY
    before = sha_file(REAL_REGISTRY)

    ls = run_cli(REAL_REGISTRY, "list")
    assert ls.returncode == 0, ls.stderr
    tasks = json.loads(ls.stdout)
    assert isinstance(tasks, list) and tasks, "现有台账应至少有一张卡"

    done_ids = [t["id"] for t in tasks if t.get("state") == "done"]
    assert done_ids, "现有台账应有 done 卡"
    got = run_cli(REAL_REGISTRY, "get", "--id", done_ids[0])
    assert got.returncode == 0, got.stderr
    assert json.loads(got.stdout)["id"] == done_ids[0]

    assert sha_file(REAL_REGISTRY) == before, "只读回读改动了真实台账"
