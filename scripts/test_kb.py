# -*- coding: utf-8 -*-
"""私人知识库用例：事实卡/检索/索引/校验。

覆盖：
  1 add fact 落盘 + 索引生成
  2 add incident 进 incidents/ + INC id
  3 同日 id 递增
  4 英文检索（标签加权）
  5 中文检索（2-4 字滑窗）
  6 match 给任务找经验
  7 list 按 type/tag 过滤
  8 verify 正常
  9 非法 type/confidence 拒绝
  10 空 title/body 拒绝
  11 缺失 source 在 verify 中报出
  12 frontmatter tags 解析

用法：python -X utf8 test_kb.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import steward_kb as kb  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def raises(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except ValueError:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_kb")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_kb")

    tmp = tempfile.mkdtemp(prefix="steward-kb-")
    old = (kb.KB, kb.ENTRIES, kb.INCIDENTS, kb.INDEX)
    kb.KB = tmp
    kb.ENTRIES = os.path.join(tmp, "entries")
    kb.INCIDENTS = os.path.join(tmp, "incidents")
    kb.INDEX = os.path.join(tmp, "INDEX.md")
    os.makedirs(kb.ENTRIES)
    os.makedirs(kb.INCIDENTS)
    try:
        e1 = kb.add("Worker 写文件被拒先查目标路径", "autoallow 钩子按 tool_input 的目标路径判定；"
                    "未登记会话只能写 roots 或临时目录，protected_paths 永不放行。",
                    ["permission", "hook", "write"], "fact", "实测 2026-11-17", "high",
                    "2026-11-18T09:00:00+08:00")
        check("add-fact", e1["id"] == "K-20261118-001" and os.path.isfile(e1["path"])
              and e1["confidence"] == "high", e1["id"])
        check("index-created", os.path.isfile(kb.INDEX)
              and "Worker 写文件被拒" in open(kb.INDEX, encoding="utf-8").read())

        e2 = kb.add("cron 静默停摆", "next_run_at 过期但会话 idle；巡检前先跑 cron_health。",
                    ["cron", "health"], "incident", "日报 2026-11-17", "high",
                    "2026-11-18T09:01:00+08:00")
        check("add-incident", e2["id"] == "INC-20261118-001"
              and os.path.dirname(e2["path"]).endswith("incidents"), e2["id"])

        e3 = kb.add("第二张事实卡", "同日 id 应递增。", ["misc"], "fact", "", "medium",
                    "2026-11-18T09:02:00+08:00")
        check("id-increment", e3["id"] == "K-20261118-002", e3["id"])

        hits = kb.search("hook permission", 5)
        check("search-en", bool(hits) and hits[0]["id"] == e1["id"],
              hits[0]["id"] if hits else "none")

        hits = kb.search("权限 钩子 写文件", 5)
        check("search-zh", bool(hits) and hits[0]["id"] == e1["id"],
              hits[0]["id"] if hits else "none")

        hits = kb.match("worker 写文件被拦，权限不对", 3)
        check("match-task", bool(hits) and hits[0]["id"] == e1["id"],
              hits[0]["id"] if hits else "none")

        facts = [x for x in kb.load_entries() if x["type"] == "fact"]
        tagged = [x for x in facts if "permission" in x["tags"]]
        check("list-filter", len(facts) == 2 and len(tagged) == 1,
              "facts=%d tagged=%d" % (len(facts), len(tagged)))

        check("verify-ok", kb.verify() == [], str(kb.verify()))

        check("bad-type", raises(kb.add, "x", "y", [], "unknown", "", "high"))
        check("bad-confidence", raises(kb.add, "x", "y", [], "fact", "", "certain"))
        check("empty-body", raises(kb.add, "x", "", [], "fact", "", "high"))

        kb.add("坏来源", "source 指向不存在的文件。", ["source"], "fact",
               "nope/missing.md", "low", "2026-11-18T09:03:00+08:00")
        problems = kb.verify()
        check("verify-missing-source", any("source 不存在" in p for p in problems),
              " | ".join(problems[:2]))

        parsed = kb.parse_entry(e1["path"])
        check("frontmatter-tags", parsed["tags"] == ["permission", "hook", "write"]
              and parsed["title"].startswith("Worker"), str(parsed["tags"]))
    finally:
        kb.KB, kb.ENTRIES, kb.INCIDENTS, kb.INDEX = old
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-22s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
