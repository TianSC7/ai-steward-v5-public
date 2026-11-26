# -*- coding: utf-8 -*-
"""kb_lint 用例：坏卡 / 重复 / 过期 / superseded 链 / 只读 / CLI 契约。

覆盖：
  1  干净卡零 issue、计数正确
  2  缺 frontmatter
  3  frontmatter 未闭合
  4  必填字段缺失（confidence）
  5  必填字段为空（title）
  6  id 与文件名不一致
  7  updated_at 格式非法
  8  created_at 格式非法（warning）
  9  重复 id
  10 重复标题
  11 superseded_by 指向不存在
  12 superseded_by 指向自身
  13 superseded 链成环
  14 superseded 合法链（A→B→C）不误报、过期豁免
  15 超过 30 天未更新 warning
  16 新鲜卡不误报过期
  17 空 tags 合法、空 source 只告警
  18 非法 type（error）/ 非法 confidence（warning）
  19 id 命名不规范（warning）
  20 JSON 结构稳定（issues[] 字段 / counts 键）
  21 只读：lint 前后文件集合与 sha256 完全一致
  22 CLI：干净库 --json 退出 0
  23 CLI：有 error 退出 1 且 stdout 是合法 JSON
  24 CLI：目录不存在退出 2
  25 非 .md 文件不扫描

用法：python -X utf8 test_kb_lint.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kb_lint  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
NOW = datetime.datetime.fromisoformat("2026-11-18T12:00:00+08:00")
NOW_ARG = "2026-11-18T12:00:00+08:00"
STAMP = "2026-11-18T09:00:00+08:00"


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def meta_lines(eid: str, **over) -> list[str]:
    fields = {
        "id": eid,
        "title": "卡片 " + eid,
        "type": "fact",
        "tags": "[test]",
        "confidence": "high",
        "source": "实测 2026-11-18",
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    fields.update(over)
    return ["%s: %s" % (k, v) for k, v in fields.items() if v is not None]


def write_card(folder: str, stem: str, lines: list[str], body: str = "正文") -> str:
    path = os.path.join(folder, stem + ".md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n")
    return path


def write_raw(folder: str, name: str, text: str) -> str:
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


@contextlib.contextmanager
def make_kb(cards: list[tuple[str, list[str]]], extras: dict | None = None):
    tmp = tempfile.mkdtemp(prefix="kb-lint-")
    try:
        for stem, lines in cards:
            write_card(tmp, stem, lines)
        for name, text in (extras or {}).items():
            write_raw(tmp, name, text)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pick(result: dict, field: str | None = None, level: str | None = None) -> list[dict]:
    out = []
    for iss in result["issues"]:
        if field is not None and iss["field"] != field:
            continue
        if level is not None and iss["level"] != level:
            continue
        out.append(iss)
    return out


def run_cli(folder: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", os.path.join(HERE, "kb_lint.py"),
         "--dir", folder, "--json", "--now", NOW_ARG],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def snapshot(folder: str) -> dict:
    out = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, folder).replace("\\", "/")
            with open(full, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_kb_lint")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_kb_lint")

    # 1 干净卡
    with make_kb([("K-20261118-001", meta_lines("K-20261118-001"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        check("clean-ok", r["ok"] and r["issues"] == []
              and r["counts"] == {"entries": 1, "errors": 0, "warnings": 0, "total": 0},
              json.dumps(r["counts"], ensure_ascii=False))

    # 2 缺 frontmatter
    with make_kb([], extras={"K-20261118-001.md": "只有正文，没有 frontmatter。\n"}) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "frontmatter", "error")
        check("no-frontmatter", len(got) == 1 and not r["ok"] and "缺 frontmatter" in got[0]["msg"],
              got[0]["msg"] if got else "none")

    # 3 frontmatter 未闭合
    with make_kb([], extras={"K-20261118-001.md":
                             "---\nid: K-20261118-001\ntitle: 半截卡\n\n正文\n"}) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "frontmatter", "error")
        check("unterminated-frontmatter",
              len(got) == 1 and "未闭合" in got[0]["msg"],
              got[0]["msg"] if got else "none")

    # 4 必填字段缺失
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", confidence=None))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "confidence", "error")
        check("missing-required", len(got) == 1 and "缺必填字段 confidence" in got[0]["msg"],
              got[0]["msg"] if got else "none")

    # 5 必填字段为空
    with make_kb([("K-20261118-001", meta_lines("K-20261118-001", title=""))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "title", "error")
        check("empty-title", len(got) == 1 and "为空" in got[0]["msg"],
              got[0]["msg"] if got else "none")

    # 6 id 与文件名不一致
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-002", title="文件名与 id 不符"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = [i for i in pick(r, "id", "error") if "不一致" in i["msg"]]
        check("id-filename-mismatch", len(got) == 1 and "K-20261118-002" in got[0]["msg"],
              got[0]["msg"] if got else "none")

    # 7 updated_at 格式非法
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", updated_at="2026/11/18 09:00"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "updated_at", "error")
        check("bad-updated-at", len(got) == 1 and "格式非法" in got[0]["msg"],
              got[0]["msg"] if got else "none")

    # 8 created_at 格式非法 -> warning（不拦门）
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", created_at="昨天"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "created_at", "warning")
        check("bad-created-at-warning",
              len(got) == 1 and r["ok"] and r["counts"]["errors"] == 0,
              got[0]["msg"] if got else "none")

    # 9 重复 id
    with make_kb([
        ("K-20261118-001", meta_lines("K-20261118-001")),
        ("K-20261118-002", meta_lines("K-20261118-001", title="另一张")),
    ]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = [i for i in pick(r, "id", "error") if "重复 id" in i["msg"]]
        check("duplicate-id", len(got) == 2 and all("K-20261118-001" in i["msg"] for i in got),
              "dups=%d" % len(got))

    # 10 重复标题
    with make_kb([
        ("K-20261118-001", meta_lines("K-20261118-001", title="同名卡")),
        ("K-20261118-002", meta_lines("K-20261118-002", title="同名卡")),
    ]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "title", "error")
        check("duplicate-title", len(got) == 2 and all("重复标题" in i["msg"] for i in got),
              "dups=%d" % len(got))

    # 11 superseded_by 指向不存在
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", superseded_by="K-20261118-999"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "superseded_by", "error")
        check("superseded-missing-target",
              len(got) == 1 and "不存在" in got[0]["msg"],
              got[0]["msg"] if got else "none")

    # 12 superseded_by 指向自身
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", superseded_by="K-20261118-001"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = pick(r, "superseded_by", "error")
        check("superseded-self", len(got) == 1 and "自身" in got[0]["msg"],
              got[0]["msg"] if got else "none")

    # 13 superseded 链成环
    with make_kb([
        ("K-20261118-001", meta_lines("K-20261118-001", superseded_by="K-20261118-002")),
        ("K-20261118-002", meta_lines("K-20261118-002", superseded_by="K-20261118-001")),
    ]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = [i for i in pick(r, "superseded_by", "error") if "成环" in i["msg"]]
        check("superseded-cycle", len(got) == 2, "cycles=%d" % len(got))

    # 14 superseded 合法链 A→B→C：不报链错误；被取代的旧卡不报过期
    old = "2026-01-01T00:00:00+08:00"
    with make_kb([
        ("K-20261118-001", meta_lines("K-20261118-001", updated_at=old,
                                      superseded_by="K-20261118-002")),
        ("K-20261118-002", meta_lines("K-20261118-002", updated_at=old,
                                      superseded_by="K-20261118-003")),
        ("K-20261118-003", meta_lines("K-20261118-003", updated_at=old)),
    ]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        chain_errs = pick(r, "superseded_by", "error")
        stale = [i for i in pick(r, "updated_at", "warning") if "未更新" in i["msg"]]
        check("superseded-valid-chain",
              chain_errs == [] and len(stale) == 1 and stale[0]["id"] == "K-20261118-003",
              "chain=%d stale=%s" % (len(chain_errs), [i["id"] for i in stale]))

    # 15 超过 30 天未更新
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", created_at="2026-09-01T00:00:00+08:00",
                              updated_at="2026-09-01T00:00:00+08:00"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = [i for i in pick(r, "updated_at", "warning") if "超过 30 天未更新" in i["msg"]]
        check("stale-warning", len(got) == 1 and r["ok"],
              got[0]["msg"] if got else "none")

    # 16 新鲜卡不误报过期
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", updated_at="2026-11-10T00:00:00+08:00"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = [i for i in pick(r, "updated_at") if "未更新" in i["msg"]]
        check("fresh-no-stale", got == [], "stale=%d" % len(got))

    # 17 空 tags 合法；空 source 只告警
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", tags="[]", source=""))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        src = pick(r, "source", "warning")
        tags = pick(r, "tags")
        check("empty-tags-source",
              tags == [] and len(src) == 1 and r["ok"] and r["counts"]["errors"] == 0,
              "tags=%d src=%d" % (len(tags), len(src)))

    # 18 非法 type / confidence
    with make_kb([
        ("K-20261118-001", meta_lines("K-20261118-001", type="note")),
        ("K-20261118-002", meta_lines("K-20261118-002", confidence="certain")),
    ]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        typ = pick(r, "type", "error")
        conf = pick(r, "confidence", "warning")
        check("enum-legality",
              len(typ) == 1 and len(conf) == 1 and not r["ok"],
              "type=%d conf=%d" % (len(typ), len(conf)))

    # 19 id 命名不规范
    with make_kb([("K-2026-11-18-001",
                   meta_lines("K-2026-11-18-001", title="id 格式不规范"))]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        got = [i for i in pick(r, "id", "warning") if "格式" in i["msg"]]
        check("id-format-warning", len(got) == 1 and r["ok"],
              got[0]["msg"] if got else "none")

    # 20 JSON 结构稳定
    with make_kb([
        ("K-20261118-001", meta_lines("K-20261118-001", confidence=None)),
        ("K-20261118-002", meta_lines("K-20261118-002")),
    ]) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        shape_ok = (
            set(r) == {"ok", "dir", "issues", "counts"}
            and set(r["counts"]) == {"entries", "errors", "warnings", "total"}
            and all(set(i) == {"id", "level", "field", "msg", "path"} for i in r["issues"])
            and isinstance(r["issues"], list)
        )
        check("json-shape", shape_ok, "keys=%s" % sorted(r))

    # 21 只读：前后文件集合与 sha256 不变
    with make_kb([("K-20261118-001", meta_lines("K-20261118-001")),
                  ("K-20261118-002", meta_lines("K-20261118-002"))],
                 extras={"notes.txt": "不应被扫描/修改\n"}) as d:
        before = snapshot(d)
        kb_lint.lint_dir(d, now=NOW)
        after = snapshot(d)
        check("read-only", before == after and "notes.txt" in before,
              "files=%d" % len(before))

    # 22 CLI 干净库：退出 0 + JSON 合法
    with make_kb([("K-20261118-001", meta_lines("K-20261118-001"))]) as d:
        cp = run_cli(d)
        try:
            data = json.loads(cp.stdout)
        except ValueError:
            data = None
        check("cli-clean",
              cp.returncode == 0 and data is not None and data["ok"]
              and data["counts"]["errors"] == 0,
              "rc=%d" % cp.returncode)

    # 23 CLI 坏卡：退出 1 + stdout 仍是合法 JSON
    with make_kb([("K-20261118-001",
                   meta_lines("K-20261118-001", updated_at="bad"))]) as d:
        cp = run_cli(d)
        try:
            data = json.loads(cp.stdout)
        except ValueError:
            data = None
        check("cli-error-json",
              cp.returncode == 1 and data is not None and not data["ok"]
              and data["counts"]["errors"] >= 1,
              "rc=%d" % cp.returncode)

    # 24 CLI 目录不存在：退出 2
    missing = os.path.join(tempfile.gettempdir(), "kb-lint-no-such-dir-" + uuid.uuid4().hex)
    cp = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.join(HERE, "kb_lint.py"),
         "--dir", missing, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    check("cli-missing-dir",
          cp.returncode == 2 and "目录不存在" in (cp.stderr or ""),
          "rc=%d" % cp.returncode)

    # 25 非 .md 文件不扫描
    with make_kb([("K-20261118-001", meta_lines("K-20261118-001"))],
                 extras={"bad.txt": "不是卡片\n"}) as d:
        r = kb_lint.lint_dir(d, now=NOW)
        check("ignore-non-md", r["counts"]["entries"] == 1 and r["issues"] == [],
              "entries=%d" % r["counts"]["entries"])

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-26s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
