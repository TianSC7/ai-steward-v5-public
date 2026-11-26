# -*- coding: utf-8 -*-
"""evidence_audit：证据盘点（纯只读，不写任何文件）。

定位（与既有件的关系）：
  ledger_check.evidence_problem() 是调度引擎内的单卡证据复核；review_gate.py 是
  单卡旁路门（输入一个 id）；steward_review.py 是管家/人工复核。本脚本是"全库盘点"：
  一次把 registry 的所有任务卡与 reports/evidence/ 目录对账，回答三个问题——
    1) 每张卡的 evidence_path 是否真实存在、非空、含通过标记、哈希与登记一致；
    2) reports/evidence/ 里哪些文件没有任务卡引用（孤儿证据）；
    3) 哪些 done 卡没有登记 evidence_path（done 缺证据）。
  只读 registry 与证据目录，不修台账、不补文件、不改哈希。

逐卡检查口径（state 不限；有 evidence_path 的卡都查）：
  missing_evidence       evidence_path 登记了但文件不存在
  empty_evidence         证据文件 0 字节
  missing_pass_marker    证据不含 EXIT=<值> 或 [PASS]
  hash_drift             卡登记 evidence_sha256 且与实测 sha256 不符（未登记不判红）
  done_missing_evidence  state=done 但 evidence_path 为空/缺失
目录级检查：
  orphan_evidence        reports/evidence/ 下的文件没有任何任务卡引用（level=warn）

issue 字段恒定：id / level / kind / detail / path。level ∈ error|warn。
counts 字段恒定：cards_total / done_total / cards_with_evidence / evidence_files /
                  issues_total / by_level{error,warn} / by_kind{六种 kind 全列}。

用法：
  python -X utf8 scripts/evidence_audit.py
  python -X utf8 scripts/evidence_audit.py --root <插件根> --json
  python -X utf8 scripts/evidence_audit.py --registry <path> --evidence-dir <path> --json

选项：
  --root          插件根目录（默认取脚本上级）；相对路径以此为基准
  --registry      台账路径（默认 <root>/state/registry.json）
  --evidence-dir  证据目录（默认 <root>/reports/evidence）
  --json          输出 JSON 对象（issues/counts 等字段恒定存在）

退出码：0 = 零 issue；2 = 有 issue 或台账不可读（问题不静默放行）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_PASS_MARKER_RE = re.compile(r"EXIT=\S|\[PASS\]")

KINDS = (
    "missing_evidence",
    "empty_evidence",
    "missing_pass_marker",
    "hash_drift",
    "done_missing_evidence",
    "orphan_evidence",
    "registry_error",
)
LEVEL_OF = {
    "missing_evidence": "error",
    "empty_evidence": "error",
    "missing_pass_marker": "error",
    "hash_drift": "error",
    "done_missing_evidence": "error",
    "orphan_evidence": "warn",
    "registry_error": "error",
}
COUNT_KEYS = ("cards_total", "done_total", "cards_with_evidence",
              "evidence_files", "issues_total")


def norm_abs(root: str, path: str) -> tuple[str, str]:
    """返回 (展示用绝对路径, 比较用归一化键)。相对路径以 root 为基准。"""
    full = path if os.path.isabs(path) else os.path.join(root, path)
    full = os.path.abspath(full)
    return full, os.path.normcase(os.path.normpath(full))


def load_registry(reg_path: str) -> tuple[dict | None, str | None]:
    """读台账。成功返回 (data, None)；失败返回 (None, 错误描述)。"""
    try:
        with open(reg_path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return None, "台账读取失败: %s" % e
    except ValueError as e:
        return None, "台账坏 JSON: %s" % e
    if not isinstance(data, dict):
        return None, "台账结构非法（根非对象）"
    if not isinstance(data.get("tasks"), list):
        return None, "台账结构非法（tasks 非列表）"
    return data, None


def _issue(tid: str, kind: str, detail: str, path: str) -> dict:
    return {"id": tid, "level": LEVEL_OF.get(kind, "error"),
            "kind": kind, "detail": detail, "path": path}


def audit(reg_path: str, root: str | None = None,
          evidence_dir: str | None = None) -> dict:
    """对全库出盘点结果。纯只读，返回 dict（issues/counts 恒在）。"""
    root = root or ROOT
    ev_dir = evidence_dir or os.path.join(root, "reports", "evidence")
    ev_dir = ev_dir if os.path.isabs(ev_dir) else os.path.join(root, ev_dir)

    issues: list[dict] = []
    by_kind = {k: 0 for k in KINDS}
    cards_total = done_total = with_ev = files_total = 0

    data, err = load_registry(reg_path)
    if err:
        issues.append(_issue("*", "registry_error", err, reg_path))
    else:
        cards = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
        cards_total = len(cards)
        referenced: set[str] = set()

        for t in cards:
            tid = str(t.get("id") or "?")
            state = str(t.get("state") or "")
            if state == "done":
                done_total += 1
            ep = str(t.get("evidence_path") or "").strip()

            if not ep:
                if state == "done":
                    issues.append(_issue(tid, "done_missing_evidence",
                                         "state=done 但未登记 evidence_path", ""))
                continue

            full, key = norm_abs(root, ep)
            referenced.add(key)
            with_ev += 1
            if not os.path.isfile(full):
                issues.append(_issue(tid, "missing_evidence",
                                     "登记的证据文件不存在", full))
                continue
            try:
                size = os.path.getsize(full)
            except OSError as e:
                issues.append(_issue(tid, "missing_evidence",
                                     "证据文件不可读: %s" % e, full))
                continue
            if size == 0:
                issues.append(_issue(tid, "empty_evidence",
                                     "证据文件为 0 字节", full))
                continue

            try:
                with open(full, "rb") as f:
                    raw = f.read()
            except OSError as e:
                issues.append(_issue(tid, "missing_evidence",
                                     "证据文件不可读: %s" % e, full))
                continue
            text = raw.decode("utf-8", "replace")
            if not _PASS_MARKER_RE.search(text):
                issues.append(_issue(tid, "missing_pass_marker",
                                     "证据不含 EXIT=<值> 或 [PASS]", full))

            registered = str(t.get("evidence_sha256") or "").strip().lower()
            if registered:
                actual = hashlib.sha256(raw).hexdigest()
                if actual != registered:
                    issues.append(_issue(
                        tid, "hash_drift",
                        "登记 %s… 实测 %s…" % (registered[:12], actual[:12]),
                        full))

        # 孤儿证据：目录里的文件没有卡引用
        if os.path.isdir(ev_dir):
            for name in sorted(os.listdir(ev_dir)):
                full = os.path.join(ev_dir, name)
                if not os.path.isfile(full):
                    continue
                if name.startswith("."):
                    continue
                files_total += 1
                _, key = norm_abs(root, full)
                if key not in referenced:
                    rel = os.path.relpath(full, root)
                    issues.append(_issue(name, "orphan_evidence",
                                         "无任务卡引用该证据文件", rel))
        else:
            issues.append(_issue("*", "registry_error",
                                 "证据目录不存在: %s" % ev_dir, ev_dir))

    for it in issues:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    by_level = {lv: sum(1 for i in issues if i["level"] == lv)
                for lv in ("error", "warn")}

    issues.sort(key=lambda i: (0 if i["level"] == "error" else 1,
                               i["kind"], str(i["id"]), str(i["path"])))
    counts = {
        "cards_total": cards_total,
        "done_total": done_total,
        "cards_with_evidence": with_ev,
        "evidence_files": files_total,
        "issues_total": len(issues),
        "by_level": by_level,
        "by_kind": by_kind,
    }
    return {"ok": not issues, "root": os.path.abspath(root),
            "registry": os.path.abspath(reg_path),
            "evidence_dir": os.path.abspath(ev_dir),
            "issues": issues, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="证据盘点：缺失/空文件/缺标记/哈希漂移/孤儿/ done 缺证据（只读）")
    ap.add_argument("--root", default=ROOT, help="插件根目录（默认脚本上级）")
    ap.add_argument("--registry", default=None, help="台账路径（默认 <root>/state/registry.json）")
    ap.add_argument("--evidence-dir", default=None, help="证据目录（默认 <root>/reports/evidence）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    reg = args.registry or os.path.join(root, "state", "registry.json")
    reg = reg if os.path.isabs(reg) else os.path.join(root, reg)
    result = audit(reg, root=root, evidence_dir=args.evidence_dir)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        c = result["counts"]
        print("evidence_audit root=%s" % result["root"])
        print("cards=%d done=%d with_evidence=%d evidence_files=%d"
              % (c["cards_total"], c["done_total"],
                 c["cards_with_evidence"], c["evidence_files"]))
        print("issues total=%d error=%d warn=%d"
              % (c["issues_total"], c["by_level"]["error"], c["by_level"]["warn"]))
        for it in result["issues"]:
            print("[%s] %-22s %-22s %s :: %s"
                  % (it["level"], it["kind"], it["id"], it["path"], it["detail"]))
        print("EXIT=%s" % ("PASS" if result["ok"] else "FAIL"))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
