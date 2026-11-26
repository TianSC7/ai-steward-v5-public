#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""state_digest.py — 把各角色/工作包的留痕文件压成一页 DIGEST，供厂长“瞄一眼”。

用法:
  python -X utf8 scripts/state_digest.py            # 增量：只显示上次以来新增的行
  python -X utf8 scripts/state_digest.py --all      # 全量：每个文件最近 20 行
  python -X utf8 scripts/state_digest.py --json     # 机器可读

数据来源（只看文件，不读会话、不发消息）:
  state/status/*.md
  state/packages/*/STATUS.md
  state/DECISIONS.md
输出:
  state/DIGEST.md
  state/.digest_offsets.json （增量游标）
"""
import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
STATUS_DIR = os.path.join(STATE, "status")
PKG_DIR = os.path.join(STATE, "packages")
DIGEST = os.path.join(STATE, "DIGEST.md")
OFFSETS = os.path.join(STATE, ".digest_offsets.json")

P0_WORDS = ("P0", "紧急", "阻塞", "不可逆", "生产事故", "数据丢失")
DECISION_WORDS = ("请示", "裁决", "决策", "待批", "冲突")
FAIL_WORDS = ("失败", "FAIL", "报错", "中断", "ghost", "超时")


def collect_files():
    files = []
    if os.path.isdir(STATUS_DIR):
        for name in sorted(os.listdir(STATUS_DIR)):
            if name.endswith(".md") and name.upper() != "TEMPLATE.MD":
                files.append(os.path.join(STATUS_DIR, name))
    if os.path.isdir(PKG_DIR):
        for pkg in sorted(os.listdir(PKG_DIR)):
            p = os.path.join(PKG_DIR, pkg, "STATUS.md")
            if os.path.isfile(p):
                files.append(p)
    return files


def classify(line):
    up = line.upper()
    if any(w in line or w.upper() in up for w in P0_WORDS):
        return "P0"
    if any(w in line for w in ("已裁决", "裁决 D-", "决定：", "决定=")):
        return "PROGRESS"
    if any(w in line for w in DECISION_WORDS):
        return "DECISION"
    # “280 PASS/0 FAIL”“FAIL=0”“0 失败”是健康指标，不能当失败告警
    if "0 FAIL" in up or "FAIL=0" in up or "0 失败" in line:
        return "PROGRESS"
    if any(w in line or w.upper() in up for w in FAIL_WORDS):
        return "FAIL"
    return "PROGRESS"


def read_new(path, offsets, full):
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if full:
        start = 0
    else:
        start = offsets.get(path, 0)
        if start > size:  # 文件被重写
            start = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            data = f.read()
            new_off = f.tell()
    except OSError:
        return []
    offsets[path] = new_off
    lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全量最近 20 行")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    offsets = {}
    if os.path.isfile(OFFSETS):
        try:
            offsets = json.load(open(OFFSETS, encoding="utf-8"))
        except Exception:
            offsets = {}

    buckets = {"P0": [], "DECISION": [], "FAIL": [], "PROGRESS": []}
    per_file = {}
    for path in collect_files():
        lines = read_new(path, offsets, args.all)
        if args.all:
            lines = lines[-20:]
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        per_file[rel] = len(lines)
        for ln in lines:
            buckets[classify(ln)].append((rel, ln))

    # 包状态摘要
    pkg_lines = []
    if os.path.isdir(PKG_DIR):
        for pkg in sorted(os.listdir(PKG_DIR)):
            plan = os.path.join(PKG_DIR, pkg, "PLAN.md")
            if os.path.isdir(os.path.join(PKG_DIR, pkg)):
                pkg_lines.append(pkg + ("（有 PLAN）" if os.path.isfile(plan) else "（缺 PLAN）"))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_new = sum(per_file.values())
    out = []
    out.append("# DIGEST %s" % now)
    out.append("")
    out.append("新增行：%d；文件：%d；包：%d" % (total_new, len(per_file), len(pkg_lines)))
    out.append("")
    for key, title in (("P0", "P0 / 紧急"), ("DECISION", "待裁决"), ("FAIL", "失败 / 卡住"), ("PROGRESS", "进度")):
        items = buckets[key]
        out.append("## %s（%d）" % (title, len(items)))
        if not items:
            out.append("- 无")
        else:
            for rel, ln in items[:10]:
                out.append("- `%s` %s" % (os.path.basename(rel), ln[:300]))
            if len(items) > 10:
                out.append("- …另有 %d 条" % (len(items) - 10))
        out.append("")
    if pkg_lines:
        out.append("## 工作包")
        for p in pkg_lines:
            out.append("- " + p)
        out.append("")

    text = "\n".join(out) + "\n"
    with open(DIGEST, "w", encoding="utf-8") as f:
        f.write(text)

    if not args.all:
        json.dump(offsets, open(OFFSETS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    if args.json:
        print(json.dumps({"digest": DIGEST, "counts": {k: len(v) for k, v in buckets.items()},
                          "files": per_file}, ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
