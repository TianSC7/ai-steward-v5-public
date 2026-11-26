# -*- coding: utf-8 -*-
"""evidence_anchor 三口径校验器测试。

覆盖（任务要求 4 例 + 扩展）：
  1) CRLF 行尾伪差异      -> MATCH_LF（raw 不等、LF 归一相等）
  2) 路径缺失             -> MISSING（path_exists=no）
  3) 陈旧哈希             -> STALE（三口径无一命中）
  4) 绝对 worktree 路径回退 -> fallback-worktree-tail（解析到仓库相对同名文件）
扩展：MATCH_RAW / BLOB_ONLY（真实临时 git 仓）/ 分类优先级矩阵 /
      run_check 计数勾稽 / 报告一行一卡 / registry 零写入 / main 端到端。
测试全程只碰 tmp_path，不读不改插件真实 registry 与证据。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import evidence_anchor as ea  # noqa: E402

HAS_GIT = shutil.which("git") is not None


# ---------------------------------------------------------------- fixtures

def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "reports" / "evidence").mkdir(parents=True)
    return root


def put_evidence(root: Path, name: str, content: bytes) -> Path:
    p = root / "reports" / "evidence" / name
    p.write_bytes(content)
    return p


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_registry(root: Path, cards: list[dict]) -> Path:
    reg = root / "state" / "registry.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"tasks": cards}, ensure_ascii=False), encoding="utf-8")
    return reg


def card(cid: str, name: str, sha256: str, path: str | None = None,
         state: str = "done") -> dict:
    return {
        "id": cid,
        "title": cid,
        "state": state,
        "evidence_path": path if path is not None else f"reports/evidence/{name}",
        "evidence_sha256": sha256,
    }


def git_commit_all(root: Path) -> None:
    run = lambda *a: subprocess.run(a, cwd=str(root), check=True,
                                    capture_output=True, timeout=120)
    run("git", "init", "-q")
    run("git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    run("git", "-c", "user.email=t@example.com", "-c", "user.name=t",
        "commit", "-q", "-m", "init")


# ---------------------------------------------------------------- 基础口径

def test_normalize_lf_only_rewrites_crlf():
    assert ea.normalize_lf(b"a\r\nb\nc") == b"a\nb\nc"
    assert ea.normalize_lf(b"no-eol") == b"no-eol"


def test_sha256_hex_matches_hashlib():
    assert ea.sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


# ---------------------------------------------------------------- 要求例 1：CRLF

def test_crlf_worktree_matches_lf_caliber_real(tmp_path):
    root = make_repo(tmp_path)
    content = b"line1\r\nline2\r\nline3\r\n"
    put_evidence(root, "W-CRLF.md", content)
    reg_sha = sha(ea.normalize_lf(content))  # registry 记 LF 口径（N06/N07 同型）
    row = ea.check_card(card("W-CRLF", "W-CRLF.md", reg_sha), root)
    assert row["path_exists"] == "yes"
    assert row["sha_raw"] == sha(content)
    assert row["sha_lf"] == reg_sha
    assert row["class"] == ea.MATCH_LF


# ---------------------------------------------------------------- 要求例 2：路径缺失

def test_missing_path_classified_missing(tmp_path):
    root = make_repo(tmp_path)
    row = ea.check_card(
        card("W-GONE", "W-GONE.md", sha(b"whatever")), root)
    assert row["path_exists"] == "no"
    assert row["resolved_path"] == ""
    assert row["resolution"] == "unresolved"
    assert row["class"] == ea.MISSING
    assert row["sha_raw"] == "" and row["sha_lf"] == "" and row["sha_blob"] == ""


# ---------------------------------------------------------------- 要求例 3：陈旧哈希

def test_stale_hash_when_no_caliber_matches(tmp_path):
    root = make_repo(tmp_path)
    put_evidence(root, "W-STALE.md", b"current bytes\n")
    row = ea.check_card(
        card("W-STALE", "W-STALE.md", "0" * 64), root)
    assert row["path_exists"] == "yes"
    assert row["class"] == ea.STALE


# ---------------------------------------------------------------- 要求例 4：绝对路径回退

def test_absolute_worktree_path_falls_back_to_repo_relative(tmp_path):
    root = make_repo(tmp_path)
    content = b"head\r\nbody\r\n"
    put_evidence(root, "W-ABS.md", content)
    stale_abs = ("C:/Users/Administrator/worktrees/ai-steward/"
                 "W-20261118-ABS/reports/evidence/W-ABS.md")
    row = ea.check_card(
        card("W-ABS", "W-ABS.md", sha(ea.normalize_lf(content)), path=stale_abs),
        root)
    assert row["resolution"] == "fallback-worktree-tail"
    assert row["path_exists"] == "yes"
    assert row["resolved_path"].endswith("reports/evidence/W-ABS.md")
    assert row["class"] == ea.MATCH_LF


def test_absolute_path_fallback_to_evidence_dir(tmp_path):
    root = make_repo(tmp_path)
    put_evidence(root, "W-Y.md", b"x\n")
    row = ea.check_card(
        card("W-Y", "W-Y.md", sha(b"x\n"), path="D:/gone/W-Y.md"), root)
    assert row["resolution"] == "fallback-evidence-dir"
    assert row["class"] == ea.MATCH_RAW


def test_absolute_path_fallback_unique_basename(tmp_path):
    root = make_repo(tmp_path)
    (root / "deep").mkdir()
    (root / "deep" / "Z.md").write_bytes(b"z\n")
    row = ea.check_card(
        card("W-Z", "Z.md", sha(b"z\n"), path="D:/gone/nowhere/Z.md"), root)
    assert row["resolution"] == "fallback-basename"
    assert row["class"] == ea.MATCH_RAW


def test_absolute_path_present_used_directly(tmp_path):
    root = make_repo(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"out\n")
    row = ea.check_card(
        card("W-OUT", "outside.md", sha(b"out\n"), path=str(outside)), root)
    assert row["resolution"] == "absolute"
    assert row["path_exists"] == "yes"
    assert row["class"] == ea.MATCH_RAW
    assert row["sha_blob"] == ""  # 仓库外文件无 HEAD 对应物


# ---------------------------------------------------------------- MATCH_RAW / BLOB_ONLY

def test_raw_bytes_match_is_match_raw(tmp_path):
    root = make_repo(tmp_path)
    put_evidence(root, "W-RAW.md", b"a\r\nb\r\n")
    row = ea.check_card(
        card("W-RAW", "W-RAW.md", sha(b"a\r\nb\r\n")), root)
    assert row["class"] == ea.MATCH_RAW


@pytest.mark.skipif(not HAS_GIT, reason="git 不可用")
def test_blob_only_when_worktree_drifted_but_head_matches(tmp_path):
    root = make_repo(tmp_path)
    committed = b"accepted\nlines\n"
    put_evidence(root, "W-BLOB.md", committed)
    git_commit_all(root)
    put_evidence(root, "W-BLOB.md", committed + b"uncommitted drift\n")
    row = ea.check_card(
        card("W-BLOB", "W-BLOB.md", sha(committed)), root)
    assert row["path_exists"] == "yes"
    assert row["sha_raw"] == sha(committed + b"uncommitted drift\n")
    assert row["sha_lf"] != row["registry_sha256"]
    assert row["sha_blob"] == row["registry_sha256"]
    assert row["class"] == ea.BLOB_ONLY


# ---------------------------------------------------------------- 分类优先级矩阵

def test_classify_priority_matrix():
    reg = "r" * 64
    assert ea.classify(False, "", "", "", reg) == ea.MISSING
    assert ea.classify(True, "", "", "", "") == ea.STALE      # 无锚
    assert ea.classify(True, reg, "l", "b", reg) == ea.MATCH_RAW
    assert ea.classify(True, "x", reg, "b", reg) == ea.MATCH_LF
    assert ea.classify(True, "x", "y", reg, reg) == ea.BLOB_ONLY
    assert ea.classify(True, "x", "y", "z", reg) == ea.STALE


# ---------------------------------------------------------------- run_check / 报告

def _mixed_registry(root: Path) -> Path:
    cards = [
        card("W-A", "W-A.md", sha(b"a\n")),                       # MATCH_RAW
        card("W-B", "W-B.md", sha(ea.normalize_lf(b"b\r\n"))),    # MATCH_LF
        card("W-C", "W-C.md", "1" * 64),                          # STALE
        card("W-D", "W-D.md", sha(b"d\n")),                       # MISSING
        card("W-E", "W-E.md", sha(b"e\n"), state="dispatched"),   # 非 done，不计
    ]
    put_evidence(root, "W-A.md", b"a\n")
    put_evidence(root, "W-B.md", b"b\r\n")
    put_evidence(root, "W-C.md", b"c\n")
    return write_registry(root, cards)


def test_run_check_counts_reconcile_and_report_one_line_per_card(tmp_path):
    root = make_repo(tmp_path)
    reg = _mixed_registry(root)
    result = ea.run_check(reg, root)
    meta, rows = result["meta"], result["rows"]
    assert meta["done_cards"] == 4
    assert meta["report_rows"] == 4
    assert sum(meta["class_counts"].values()) == meta["report_rows"]
    assert meta["class_counts"] == {
        ea.MATCH_RAW: 1, ea.MATCH_LF: 1, ea.BLOB_ONLY: 0,
        ea.STALE: 1, ea.MISSING: 1,
    }
    assert meta["consistent"] is True
    md = ea.render_markdown(result)
    lines = md.rstrip("\n").split("\n")
    assert len(lines) == meta["done_cards"] == 4
    for line in lines:
        assert "| yes |" in line or "| no |" in line
        assert any(f"| {c} |" in line for c in ea.CLASSES)
        assert line.count("|") >= 11
    # 三口径 sha 列非空可辨（MISSING 行为空串，但列位仍在）
    assert lines[0].count("|") == lines[3].count("|")


def test_run_check_leaves_registry_untouched(tmp_path):
    root = make_repo(tmp_path)
    reg = _mixed_registry(root)
    before = reg.read_bytes()
    before_files = sorted(p.as_posix() for p in root.rglob("*") if p.is_file())
    ea.run_check(reg, root)
    assert reg.read_bytes() == before
    after_files = sorted(p.as_posix() for p in root.rglob("*") if p.is_file())
    assert after_files == before_files  # 未新生任何文件


def test_report_embeds_registry_before_after_hashes(tmp_path):
    root = make_repo(tmp_path)
    reg = _mixed_registry(root)
    result = ea.run_check(reg, root)
    before = result["meta"]["registry_sha256_before"]
    after = result["meta"]["registry_sha256_after"]
    assert before == after  # 零写入
    for line in ea.render_markdown(result).rstrip("\n").split("\n"):
        assert before in line and after in line  # 前后哈希随每行落报


def test_main_end_to_end_writes_report_and_json(tmp_path, capsys):
    root = make_repo(tmp_path)
    reg = _mixed_registry(root)
    out_md = tmp_path / "out" / "CHECK_0800.md"
    out_json = tmp_path / "out" / "CHECK_0800.json"
    rc = ea.main(["--check", "--registry", str(reg), "--repo", str(root),
                  "--out-md", str(out_md), "--out-json", str(out_json)])
    assert rc == 0
    captured = capsys.readouterr().out.strip().split("\n")
    assert captured[-1] == "EXIT=PASS"
    lines = out_md.read_text(encoding="utf-8").rstrip("\n").split("\n")
    assert len(lines) == 4
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["meta"]["done_cards"] == 4
    assert payload["meta"]["registry_unchanged"] is True
    assert sum(payload["meta"]["class_counts"].values()) == 4


def test_main_requires_check_flag():
    with pytest.raises(SystemExit):
        ea.main([])
