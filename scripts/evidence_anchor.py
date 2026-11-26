# -*- coding: utf-8 -*-
"""evidence_anchor：证据锚三口径校验器（纯只读，只写报告/JSON 两个输出文件）。

定位（与既有件的关系）：evidence_audit.py 做"存在性/通过标记/哈希"盘点，
ledger_check.evidence_problem() 做单卡复核。本脚本专治一件事——
**registry 的 evidence_sha256 到底对得上哪种口径**，把 M-AUDIT
`state/AUDIT_EVIDENCE_ANCHORS_0719.md` §2.3 建议的口径整改落成工具：
  1) 对 registry 全部 state=done 卡计算三种口径的 sha256：
       sha_raw   工作树文件原始字节（Windows 下常见 CRLF）
       sha_lf    工作树字节做 \\r\\n -> \\n 归一后的 sha256
       sha_blob  HEAD:<仓库相对路径> git blob 内容的 sha256（跨行尾稳定）
  2) evidence_path 解析：仓库相对路径直接用；绝对 worktree 路径失效时
     自动回退到仓库内同名文件（先按 worktree 尾段、再按 reports/evidence/
     同名、最后全仓唯一同名匹配），回退轨迹记 resolution 字段。
  3) 逐卡分类（优先级从上到下，取首个命中）：
       MATCH_RAW   sha_raw  == registry sha（字节级一致）
       MATCH_LF    sha_lf   == registry sha（仅行尾形态差异，非内容变化）
       BLOB_ONLY   sha_blob == registry sha（工作树有未提交改动，入库版才是锚）
       STALE       文件在、三种口径无一命中（锚陈旧/不可复现）
       MISSING     解析后的文件不存在
  4) 输出 JSON + Markdown 报告；报告严格"一行一卡"，行数 == done 卡数。

只读保证：registry 与证据文件全程只读；运行前后各算一次 registry sha256
并写入输出（前后哈希相等即零写入自证）；不重锚、不改 registry、不修文件。

用法：
  python -X utf8 scripts/evidence_anchor.py --check
  python -X utf8 scripts/evidence_anchor.py --check --registry <path> --repo <root>
  python -X utf8 scripts/evidence_anchor.py --check --out-md <path> --out-json <path>

退出码：0 = 校验器自身一致性通过（报告行数==done 数、分类计数可勾稽、
registry 前后哈希一致）；2 = 台账不可读或一致性失败（问题不静默放行）。
stdout 末行恒为 EXIT=PASS / EXIT=FAIL。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# 分类常量（五类，互斥且穷尽）
MATCH_RAW = "MATCH_RAW"
MATCH_LF = "MATCH_LF"
BLOB_ONLY = "BLOB_ONLY"
STALE = "STALE"
MISSING = "MISSING"
CLASSES = (MATCH_RAW, MATCH_LF, BLOB_ONLY, STALE, MISSING)

# Markdown 报告列序（一行一卡；前三口径 sha / path_exists / class 为必含列）
REPORT_COLUMNS = (
    "id",
    "evidence_path",
    "resolved_path",
    "path_exists",
    "sha_raw",
    "sha_lf",
    "sha_blob",
    "registry_sha256",
    "class",
    "registry_sha256_before",
    "registry_sha256_after",
)

_WORKTREE_TAIL_RE = re.compile(r"/worktrees/[^/]+/[^/]+/(.+)$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


# ---------------------------------------------------------------- 基础口径

def sha256_hex(data: bytes) -> str:
    """字节流的 sha256 十六进制（小写）。"""
    return hashlib.sha256(data).hexdigest()


def normalize_lf(data: bytes) -> bytes:
    """CRLF -> LF 归一（不动其他字节）。"""
    return data.replace(b"\r\n", b"\n")


def git_blob_bytes(relpath_posix: str, repo_root: Path) -> bytes | None:
    """HEAD:<relpath> 的 blob 内容；不在 HEAD / git 不可用 -> None。"""
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{relpath_posix}"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_head(repo_root: Path) -> str | None:
    """当前 HEAD 提交号（best-effort，失败返回 None）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


# ---------------------------------------------------------------- 路径解析

def resolve_evidence_path(evidence_path: str, repo_root: Path) -> tuple[Path | None, str]:
    """把 registry 的 evidence_path 解析成真实文件。

    返回 (resolved, mode)；mode ∈ relative|absolute|fallback-worktree-tail|
    fallback-evidence-dir|fallback-basename|unresolved。
    绝对路径存在则直接用；绝对路径失效时回退仓库内同名文件（任务要求）；
    仓库相对路径不做同名回退（缺失即 MISSING）。
    """
    if not evidence_path:
        return None, "unresolved"
    raw = evidence_path.replace("\\", "/")
    is_abs = bool(_DRIVE_RE.match(raw)) or raw.startswith("/")
    if not is_abs:
        cand = repo_root / raw
        if cand.is_file():
            return cand, "relative"
        return None, "unresolved"
    cand = Path(raw)
    if cand.is_file():
        return cand, "absolute"
    # 绝对路径失效 → 仓库相对同名文件回退
    m = _WORKTREE_TAIL_RE.search(raw)
    if m:
        tail = m.group(1)
        cand = repo_root / tail
        if cand.is_file():
            return cand, "fallback-worktree-tail"
    name = Path(raw).name
    cand = repo_root / "reports" / "evidence" / name
    if cand.is_file():
        return cand, "fallback-evidence-dir"
    hits = [
        p for p in repo_root.rglob(name)
        if p.is_file() and ".git" not in p.parts
    ]
    if len(hits) == 1:
        return hits[0], "fallback-basename"
    return None, "unresolved"


# ---------------------------------------------------------------- 逐卡校验

def classify(path_exists: bool, sha_raw: str, sha_lf: str, sha_blob: str,
             registry_sha: str) -> str:
    """五分类；raw > lf > blob 优先级，文件缺失优先判 MISSING。"""
    if not path_exists:
        return MISSING
    if not registry_sha:
        return STALE  # 无锚可校（当前 73 张 done 卡均有锚，防御分支）
    if sha_raw and sha_raw == registry_sha:
        return MATCH_RAW
    if sha_lf and sha_lf == registry_sha:
        return MATCH_LF
    if sha_blob and sha_blob == registry_sha:
        return BLOB_ONLY
    return STALE


def check_card(card: dict, repo_root: Path) -> dict:
    """单卡三口径计算 + 分类；返回 REPORT_COLUMNS 的超集（含 resolution）。"""
    evidence_path = card.get("evidence_path") or ""
    registry_sha = (card.get("evidence_sha256") or "").strip().lower()
    resolved, mode = resolve_evidence_path(evidence_path, repo_root)
    path_exists = bool(resolved and resolved.is_file())
    sha_raw = sha_lf = sha_blob = ""
    if path_exists:
        data = resolved.read_bytes()
        sha_raw = sha256_hex(data)
        sha_lf = sha256_hex(normalize_lf(data))
        try:
            rel = resolved.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel = ""  # 文件在仓库外（绝对路径直存），无 HEAD 对应物
        if rel:
            blob = git_blob_bytes(rel, repo_root)
            if blob is not None:
                sha_blob = sha256_hex(blob)
    return {
        "id": card.get("id", ""),
        "evidence_path": evidence_path,
        "resolved_path": resolved.as_posix() if resolved else "",
        "resolution": mode,
        "path_exists": "yes" if path_exists else "no",
        "sha_raw": sha_raw,
        "sha_lf": sha_lf,
        "sha_blob": sha_blob,
        "registry_sha256": registry_sha,
        "class": classify(path_exists, sha_raw, sha_lf, sha_blob, registry_sha),
    }


# ---------------------------------------------------------------- 全量校验

def run_check(registry_path: Path, repo_root: Path) -> dict:
    """全量三口径校验；返回 {meta, rows}。registry 零写入自证内含前后哈希。"""
    registry_path = Path(registry_path)
    repo_root = Path(repo_root)
    before_bytes = registry_path.read_bytes()
    registry = json.loads(before_bytes.decode("utf-8"))
    tasks = registry.get("tasks", [])
    done = [t for t in tasks if t.get("state") == "done"]
    rows = [check_card(t, repo_root) for t in sorted(done, key=lambda t: t.get("id", ""))]
    after_bytes = registry_path.read_bytes()
    reg_before, reg_after = sha256_hex(before_bytes), sha256_hex(after_bytes)
    for row in rows:  # 零写入自证哈希随每行落报（报告严格一行一卡，无表头位可放）
        row["registry_sha256_before"] = reg_before
        row["registry_sha256_after"] = reg_after
    counts = Counter(r["class"] for r in rows)
    class_counts = {c: counts.get(c, 0) for c in CLASSES}
    consistent = (
        len(rows) == len(done)
        and sum(class_counts.values()) == len(rows)
        and reg_before == reg_after
        and all((r["class"] in CLASSES) for r in rows)
    )
    return {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "checker": "scripts/evidence_anchor.py --check",
            "registry_path": registry_path.as_posix(),
            "registry_sha256_before": reg_before,
            "registry_sha256_after": reg_after,
            "registry_unchanged": reg_before == reg_after,
            "repo_root": repo_root.as_posix(),
            "head": git_head(repo_root),
            "done_cards": len(done),
            "report_rows": len(rows),
            "class_counts": class_counts,
            "class_sum": sum(class_counts.values()),
            "consistent": consistent,
            "columns": list(REPORT_COLUMNS),
        },
        "rows": rows,
    }


def render_markdown(result: dict) -> str:
    """一行一卡的 Markdown（管道分隔行）；行数恒等于 done 卡数。"""
    meta = result["meta"]
    lines = []
    for row in result["rows"]:
        cells = [str(row.get(col, "")) for col in REPORT_COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve()
    default_repo = here.parent.parent
    ap = argparse.ArgumentParser(
        prog="evidence_anchor.py",
        description="证据锚三口径校验器（只读；--check 为唯一动作）",
    )
    ap.add_argument("--check", action="store_true", required=True,
                    help="执行三口径校验（raw / LF 归一 / HEAD git blob）")
    ap.add_argument("--registry", default=None,
                    help="registry.json 路径（默认 <repo>/state/registry.json）")
    ap.add_argument("--repo", default=None,
                    help="仓库根（默认脚本上级目录）")
    ap.add_argument("--out-md", default=None,
                    help="Markdown 报告输出路径（默认 state/reports/EVIDENCE_ANCHOR_CHECK_<HHMM>.md）")
    ap.add_argument("--out-json", default=None,
                    help="JSON 输出路径（默认与报告同基名 .json）")
    ap.add_argument("--stdout", action="store_true", help="额外打印 JSON 到 stdout")
    args = ap.parse_args(argv)

    repo = Path(args.repo) if args.repo else default_repo
    registry = Path(args.registry) if args.registry else repo / "state" / "registry.json"
    stamp = datetime.now().strftime("%H%M")
    out_md = (Path(args.out_md) if args.out_md
              else repo / "state" / "reports" / f"EVIDENCE_ANCHOR_CHECK_{stamp}.md")
    out_json = (Path(args.out_json) if args.out_json
                else out_md.with_suffix(".json"))

    try:
        result = run_check(registry, repo)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[evidence_anchor] 台账不可读或解析失败: {exc}")
        print("EXIT=FAIL")
        return 2

    meta = result["meta"]
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(result), encoding="utf-8", newline="\n")
    out_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )

    counts = meta["class_counts"]
    print(f"[evidence_anchor] done_cards={meta['done_cards']} "
          f"report_rows={meta['report_rows']} "
          f"class_sum={meta['class_sum']} "
          f"registry_unchanged={meta['registry_unchanged']}")
    print("[evidence_anchor] class_counts="
          + " ".join(f"{c}={counts[c]}" for c in CLASSES))
    print(f"[evidence_anchor] report={out_md.as_posix()}")
    print(f"[evidence_anchor] json={out_json.as_posix()}")
    if args.stdout:
        print(json.dumps(result, ensure_ascii=False))
    ok = meta["consistent"] and meta["report_rows"] == meta["done_cards"]
    print("EXIT=PASS" if ok else "EXIT=FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
