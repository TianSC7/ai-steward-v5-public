# -*- coding: utf-8 -*-
"""steward_review 用例（18 例）：逐条验收/引文/缺失/只读/CLI 契约。

覆盖：
   1 JSON 形状与单条 pass（顶层恰 id/verdict/items，条目恰 item/status/evidence_quote）
   2 编号 acceptance 拆成 N 条且顺序不变
   3 某条无证据支撑 -> 该条 fail、总 fail、退出码非 0
   4 证据文件缺失 -> verdict=missing、逐条 missing、不抛栈
   5 证据文件为空 -> verdict=missing
   6 任务 id 不存在 -> fail、items 空、不抛栈
   7 台账坏 JSON -> fail、items 空、不抛栈
   8 只读：registry/证据字节与 mtime 不变，目录不新增文件
   9 acceptance 为空 -> fail、items 空
  10 无编号但分号分隔 -> 拆成多条
  11 EXIT=/[PASS] 按通过标记处理：EXIT=0 或 [PASS] 均可，EXIT=2 不可
  12 卡上无 evidence_path -> 回落 reports/evidence/<id>.md
  13 evidence_quote 取自证据原文而非模板
  14 人类可读输出含 VERDICT/条目状态
  15 --evidence 覆盖卡上路径
  16 acceptance 抽不出任何可机检信号 -> fail（不可假装通过）
  17 acceptance 要求证据文件自身 -> 文件存在即满足（不要求正文重复全路径）
  18 把 acceptance 原文抄进证据不算支撑，必须另有实测输出

为什么不用 fingerprint：本任务要求在隔离工作区内跑，隔离工作区上级没有
marketplace.json，fingerprint.selfcheck 必挂（同 W-20261118-025 的取证）。

用法：
  python -X utf8 scripts/test_steward_review.py   # 独立运行，EXIT=0 全过
  python -X utf8 -m pytest scripts/test_steward_review.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import steward_review as sr  # noqa: E402

SCRIPT = os.path.join(HERE, "steward_review.py")


def card(tid: str = "W-A", acceptance: str = "证据 EXIT=0",
         evidence_path: str | None = "reports/evidence/W-A.md", **kw) -> dict:
    base = {"id": tid, "title": "用例任务", "project": "ai-steward",
            "state": "done", "acceptance": acceptance,
            "evidence_path": evidence_path}
    base.update(kw)
    return base


def make_root(tasks: list[dict], evidence: dict | None = None) -> str:
    """搭临时插件根：state/registry.json + reports/evidence/*。"""
    root = tempfile.mkdtemp(prefix="steward-review-")
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    os.makedirs(os.path.join(root, "reports", "evidence"), exist_ok=True)
    with open(os.path.join(root, "state", "registry.json"), "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    for name, content in (evidence or {}).items():
        with open(os.path.join(root, "reports", "evidence", name),
                  "w", encoding="utf-8") as f:
            f.write(content)
    return root


def cli(root: str, tid: str, *extra: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-X", "utf8", SCRIPT, "--id", tid, "--root", root, *extra]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def snapshot(root: str) -> dict:
    """文件树快照：相对路径 -> (size, mtime_ns)。"""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            out[os.path.relpath(path, root)] = (st.st_size, st.st_mtime_ns)
    return out


class TestStewardReview(unittest.TestCase):
    def root(self, tasks: list[dict], evidence: dict | None = None) -> str:
        root = make_root(tasks, evidence)
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def run_review(self, root: str, tid: str = "W-A", **kw):
        reg = os.path.join(root, "state", "registry.json")
        return sr.review(reg, tid, root=root, **kw)

    # 1
    def test_01_json_shape_and_single_pass(self):
        root = self.root([card(acceptance="回复 STEER-OK 且 EXIT=0")],
                         {"W-A.md": "# 证据\nworker 回复 STEER-OK；EXIT=0\n"})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        obj = json.loads(p.stdout)
        self.assertEqual(set(obj), {"id", "verdict", "items"})
        self.assertEqual(obj["id"], "W-A")
        self.assertEqual(obj["verdict"], "pass")
        self.assertEqual(len(obj["items"]), 1)
        item = obj["items"][0]
        self.assertEqual(set(item), {"item", "status", "evidence_quote"})
        self.assertEqual(item["status"], "pass")
        self.assertIn("STEER-OK", item["evidence_quote"])

    # 2
    def test_02_numbered_items_one_to_one(self):
        root = self.root(
            [card(acceptance="1) 新增 scripts/a.py 2) CLI 支持 --json 3) 证据含 EXIT=0")],
            {"W-A.md": "# W-A\n新增 scripts/a.py 文件\nCLI 支持 --json 输出\n证据 EXIT=0\n[PASS]\n"})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        items = json.loads(p.stdout)["items"]
        self.assertEqual(len(items), 3)
        self.assertTrue(items[0]["item"].startswith("1)"))
        self.assertTrue(items[1]["item"].startswith("2)"))
        self.assertTrue(items[2]["item"].startswith("3)"))
        self.assertEqual([x["status"] for x in items], ["pass", "pass", "pass"])

    # 3
    def test_03_unsupported_item_fails(self):
        root = self.root([card(acceptance="1) scripts/a.py 2) --json")],
                         {"W-A.md": "scripts/a.py 已新增\n"})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 2, p.stderr)
        obj = json.loads(p.stdout)
        self.assertEqual(obj["verdict"], "fail")
        self.assertEqual([x["status"] for x in obj["items"]], ["pass", "fail"])

    # 4
    def test_04_missing_evidence_is_missing(self):
        root = self.root([card(acceptance="1) 证据非空 2) EXIT=0")], {})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 2)
        obj = json.loads(p.stdout)
        self.assertEqual(obj["verdict"], "missing")
        self.assertEqual([x["status"] for x in obj["items"]], ["missing", "missing"])
        self.assertEqual([x["evidence_quote"] for x in obj["items"]], ["", ""])
        self.assertNotIn("Traceback", p.stderr)
        self.assertNotIn("Traceback", p.stdout)

    # 5
    def test_05_empty_evidence_is_missing(self):
        root = self.root([card(acceptance="1) 证据非空 2) EXIT=0")],
                         {"W-A.md": ""})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["verdict"], "missing")
        self.assertNotIn("Traceback", p.stderr)

    # 6
    def test_06_unknown_task_no_traceback(self):
        root = self.root([card()], {"W-A.md": "# 证据\nEXIT=0\n"})
        p = cli(root, "W-Z", "--json")
        self.assertEqual(p.returncode, 2)
        obj = json.loads(p.stdout)
        self.assertEqual(obj["verdict"], "fail")
        self.assertEqual(obj["items"], [])
        self.assertNotIn("Traceback", p.stderr)

    # 7
    def test_07_bad_registry_no_traceback(self):
        root = self.root([card()], {"W-A.md": "# 证据\nEXIT=0\n"})
        with open(os.path.join(root, "state", "registry.json"), "w",
                  encoding="utf-8") as f:
            f.write("{not-json")
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 2)
        obj = json.loads(p.stdout)
        self.assertEqual(obj["verdict"], "fail")
        self.assertEqual(obj["items"], [])
        self.assertNotIn("Traceback", p.stderr)

    # 8
    def test_08_review_is_read_only(self):
        root = self.root([card(acceptance="证据 EXIT=0")],
                         {"W-A.md": "# 证据\nEXIT=0\n"})
        reg = os.path.join(root, "state", "registry.json")
        ev = os.path.join(root, "reports", "evidence", "W-A.md")
        reg_bytes = open(reg, "rb").read()
        ev_bytes = open(ev, "rb").read()
        before = snapshot(root)
        result, err = self.run_review(root)
        self.assertEqual(result["verdict"], "pass", err)
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(open(reg, "rb").read(), reg_bytes)
        self.assertEqual(open(ev, "rb").read(), ev_bytes)
        self.assertEqual(snapshot(root), before)

    # 9
    def test_09_empty_acceptance_fails(self):
        root = self.root([card(acceptance="")], {"W-A.md": "# 证据\nEXIT=0\n"})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 2)
        obj = json.loads(p.stdout)
        self.assertEqual(obj["verdict"], "fail")
        self.assertEqual(obj["items"], [])
        self.assertNotIn("Traceback", p.stderr)

    # 10
    def test_10_semicolon_acceptance_splits(self):
        root = self.root([card(acceptance="A 条件满足；B 条件满足")],
                         {"W-A.md": "A 条件满足\nB 条件满足\n"})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        items = json.loads(p.stdout)["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual([x["status"] for x in items], ["pass", "pass"])

    # 11
    def test_11_marker_alternatives(self):
        acceptance = "证据含 EXIT=/[PASS]"
        cases = [
            ("[PASS] ok\n", "pass"),
            ("EXIT=0\n", "pass"),
            ("EXIT=2\n", "fail"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                root = self.root([card(acceptance=acceptance)],
                                 {"W-A.md": "# 证据\n" + body})
                p = cli(root, "W-A", "--json")
                obj = json.loads(p.stdout)
                self.assertEqual(obj["verdict"], expected,
                                 "body=%r items=%r" % (body, obj["items"]))
                self.assertEqual(obj["items"][0]["status"],
                                 "pass" if expected == "pass" else "fail")
                self.assertEqual(p.returncode, 0 if expected == "pass" else 2)

    # 12
    def test_12_default_evidence_path(self):
        root = self.root([card(acceptance="证据齐全 EXIT=0", evidence_path=None)],
                         {"W-A.md": "# 证据\n证据齐全\nEXIT=0\n"})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["verdict"], "pass")

    # 13
    def test_13_quote_comes_from_evidence(self):
        body = "# 证据\n第一行无关内容\nworker 回执 STEER-OK 完成\n"
        root = self.root([card(acceptance="回执 STEER-OK")], {"W-A.md": body})
        result, err = self.run_review(root)
        self.assertEqual(result["verdict"], "pass", err)
        quote = result["items"][0]["evidence_quote"]
        self.assertIn("worker 回执 STEER-OK 完成", quote)
        self.assertIn(quote, body)

    # 14
    def test_14_human_output(self):
        root = self.root([card(acceptance="证据 EXIT=0")],
                         {"W-A.md": "# 证据\nEXIT=0\n"})
        p = cli(root, "W-A")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("VERDICT: pass", p.stdout)
        self.assertIn("[PASS]", p.stdout)

    # 15
    def test_15_evidence_override(self):
        root = self.root([card(acceptance="证据 EXIT=0",
                               evidence_path="reports/evidence/missing.md")],
                         {"override.md": "# 证据\nEXIT=0\n"})
        p = cli(root, "W-A", "--json", "--evidence", "reports/evidence/override.md")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["verdict"], "pass")

    # 16
    def test_16_no_checkable_signal_fails(self):
        root = self.root([card(acceptance="???")], {"W-A.md": "# 证据\nEXIT=0\n"})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 2)
        obj = json.loads(p.stdout)
        self.assertEqual(obj["verdict"], "fail")
        self.assertEqual(obj["items"][0]["status"], "fail")

    # 17
    def test_17_self_evidence_path_counts(self):
        """acceptance 要求证据文件本身时，文件自身即满足，不要求正文重复全路径。"""
        root = self.root([card(acceptance="证据文件 reports/evidence/W-A.md 含 EXIT=0")],
                         {"W-A.md": "# 证据\nEXIT=0\n"})
        p = cli(root, "W-A", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["verdict"], "pass")

    # 18
    def test_18_echoing_acceptance_is_not_evidence(self):
        """把 acceptance 原文抄进证据不算支撑；必须另有实测输出。"""
        acceptance = "worker 回执 STEER-OK 且 EXIT=0"
        echo_only = self.root([card(acceptance=acceptance)],
                              {"W-A.md": "# 证据\n" + acceptance + "\n"})
        p = cli(echo_only, "W-A", "--json")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["verdict"], "fail")

        with_output = self.root(
            [card(acceptance=acceptance)],
            {"W-A.md": "# 证据\n" + acceptance + "\nworker 回执 STEER-OK；EXIT=0\n"})
        p = cli(with_output, "W-A", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["verdict"], "pass")


def run_tests() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestStewardReview)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    if failed:
        print("[FAIL] %d passed / %d total" % (total - failed, total))
        return 2
    print("[PASS] %d passed / %d total" % (total, total))
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
