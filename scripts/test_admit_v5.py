# -*- coding: utf-8 -*-
import re
import unittest
import admit_v5 as a


def fam(order_id: str) -> str:
    return re.sub(r"R\d+$", "", order_id or "")


class AdmitV5Test(unittest.TestCase):
    def test_plus_and_comma_split(self):
        job = {"order_id": "G1", "target": "scripts/a.py+dashboard/v2.html", "targets": ["scripts/a.py, lines/registry.json"]}
        self.assertEqual(
            a.job_paths(job),
            ["scripts/a.py", "dashboard/v2.html", "lines/registry.json"],
        )

    def test_running_path_blocks_and_does_not_start(self):
        running = [{"order_id": "G51", "status": "running", "targets": ["scripts/factory_runner.py"]}]
        queued = [{"order_id": "G48BR1", "status": "queued", "targets": ["scripts/factory_runner.py"]}]
        primary, extras, holds = a.plan_starts(queued, running, set(), 2, fam)
        self.assertIsNone(primary)
        self.assertEqual(extras, [])
        self.assertIn("G51 占用 scripts/factory_runner.py", holds["G48BR1"])

    def test_same_order_is_not_a_conflict(self):
        running = [{"order_id": "G50", "status": "running", "targets": ["a.md"]}]
        queued = [{"order_id": "G50", "status": "queued", "targets": ["a.md"]}]
        primary, extras, holds = a.plan_starts(queued, running, set(), 1, fam)
        self.assertEqual(primary["order_id"], "G50")
        self.assertEqual(holds, {})
        self.assertEqual(extras, [])

    def test_two_queued_same_path_only_one_starts(self):
        queued = [
            {"order_id": "A", "targets": ["dashboard/v2.html"]},
            {"order_id": "B", "targets": ["scripts/v3_data.py+dashboard/v2.html"]},
            {"order_id": "C", "targets": ["scripts/split_order.py"]},
        ]
        primary, extras, holds = a.plan_starts(queued, [], set(), 3, fam)
        self.assertEqual(primary["order_id"], "A")
        self.assertEqual([j["order_id"] for j in extras], ["C"])
        self.assertIn("A 占用 dashboard/v2.html", holds["B"])

    def test_family_waits_without_failing(self):
        queued = [{"order_id": "G46R2", "targets": ["scripts/other.py"]}]
        primary, extras, holds = a.plan_starts(queued, [], {"G46"}, 1, fam)
        self.assertIsNone(primary)
        self.assertEqual(holds["G46R2"], "同家族在制")
        self.assertEqual(extras, [])

    def test_predecessor_must_be_delivered(self):
        queued = [{"order_id": "CHILD", "depends_on": ["PARENT"], "targets": ["a.py"]}]
        history = [{"order_id": "PARENT", "status": "failed"}]
        primary, extras, holds = a.plan_starts(queued, [], set(), 1, fam, history)
        self.assertIsNone(primary)
        self.assertIn("前置未交付：PARENT", holds["CHILD"])
        history[0]["status"] = "done"
        primary, extras, holds = a.plan_starts(queued, [], set(), 1, fam, history)
        self.assertEqual(primary["order_id"], "CHILD")
        self.assertEqual(holds, {})

    def test_four_dispositions_and_one_line(self):
        self.assertEqual(a.disposition("done", "判据通过。后面还有话"), "完全实现")
        self.assertEqual(a.disposition("done", "confirmed P1=2"), "部分实现")
        self.assertEqual(a.disposition("needs_human", "P0 没降"), "转驻厂专家")
        self.assertEqual(a.disposition("failed", "门禁没过"), "未实现打回")
        self.assertEqual(a.one_line("第一句。第二句很长"), "第一句")

    def test_script_inside_targets_is_not_acceptance(self):
        warn = a.script_in_scope("python scripts/check_gate.py", ["scripts/check_gate.py"])
        self.assertIn("验收脚本", warn)
        self.assertEqual(a.script_in_scope("python scripts/check_gate.py", ["scripts/other.py"]), "")

    def test_scripts_in_scope_names_the_files_and_ignores_case(self):
        """质检简报事前单列用：验收命令点名的文件里哪些本轮被改了，原样列出来；Windows 路径不区分大小写，重复的只留一个。"""
        changed = ["scripts/app.py", "Scripts\\Test_App.py", "scripts/test_app.py", "scripts/run_tests.py"]
        cmd = r"python -X utf8 scripts\test_app.py && python scripts/run_tests.py"
        self.assertEqual(a.scripts_in_scope(cmd, changed), ["Scripts/Test_App.py", "scripts/run_tests.py"])
        self.assertEqual(a.scripts_in_scope("", changed), [])
        self.assertEqual(a.scripts_in_scope(cmd, []), [])

    def test_retry_marks_old_evidence_stale(self):
        text = a.stale_clause({"order_id": "G1", "status": "failed", "targets": ["scripts/a.py"]})
        self.assertIn("已经过期", text)
        self.assertIn("scripts/a.py", text)

    def test_pk_stays_single_lane_unless_enabled(self):
        self.assertEqual(a.lanes_to_run({"pk_lanes": 3}, 6), 1)
        self.assertEqual(a.lanes_to_run({"pk_enable": True, "pk_lanes": 3}, 6), 3)
        self.assertEqual(a.lanes_to_run({"pk_enable": True, "pk_lanes": 9}, 2), 2)

    def test_no_declared_path_does_not_block(self):
        running = [{"order_id": "G51", "targets": ["scripts/factory_runner.py"]}]
        queued = [{"order_id": "G50", "title": "research"}]
        primary, extras, holds = a.plan_starts(queued, running, set(), 1, fam)
        self.assertEqual(primary["order_id"], "G50")
        self.assertEqual(holds, {})



class LiveLineTests(unittest.TestCase):
    def test_empty_counts_minutes(self):
        self.assertEqual(a.live_line("", 125), "施工进行中，已 2 分")

    def test_verifier_keeps_one_sentence(self):
        got = a.live_line("正在核对门缝。下一句不要", 60, "verifier")
        self.assertEqual(got, "质检进行中，已 1 分：正在核对门缝")

    def test_long_line_stays_short(self):
        got = a.live_line("字" * 200, 0)
        self.assertLessEqual(len(got), 81)
        self.assertTrue(got.endswith("…"))


if __name__ == "__main__":
    unittest.main()
