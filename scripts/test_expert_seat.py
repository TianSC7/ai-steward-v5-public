# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import expert_seat as seat


class ExpertSeatTests(unittest.TestCase):
    def test_only_marked_specs_call_the_expert(self):
        self.assertTrue(seat.wants_expert("## 驻厂外聘专家\n"))
        self.assertTrue(seat.wants_expert("架构专家升级"))
        self.assertFalse(seat.wants_expert("普通施工单"))

    def test_lease_outlives_the_expert_session(self):
        self.assertGreater(seat.LEASE_SEC, seat.RUN_TIMEOUT_SEC)

    def test_unreachable_contract_becomes_an_objection_not_a_revision(self):
        text = seat.diagnose("验收要求 extra_meta，禁止修改 factory_runner")
        self.assertNotIn("修订验收", text)
        self.assertTrue(seat.notes_complete(text))
        self.assertIn("验收异议", text)
        self.assertIn("交厂里裁决", text)

    def test_invisible_diff_asks_for_evidence(self):
        text = seat.diagnose("v3_data.py diff 被截断，无法核实")
        self.assertIn("缩小改动并补上可复跑证据", text)
        self.assertIn("## 根因", text)

    def test_notes_complete_needs_all_five_sections(self):
        full = seat.notes_template(stuck="a", cause="b", done="c", handoff="d")
        self.assertTrue(seat.notes_complete(full))
        self.assertFalse(seat.notes_complete("## 卡在哪\nx\n\n## 根因\ny\n"))
        self.assertFalse(seat.notes_complete(""))

    def test_prompt_confines_the_expert_to_the_sandbox(self):
        prompt = seat.research_prompt("材料", "底稿", wt_path=r"C:\wt\G1", wt_branch="wt/G1",
                                      targets=["scripts/app.py"], notes_path=r"C:\ev\expert_notes.md",
                                      live_roots=[r"C:\live"], context="- 卡住的原因：白屏", mode="rescue")
        self.assertIn(r"C:\wt\G1", prompt)
        self.assertIn("wt/G1", prompt)
        self.assertIn("`scripts/app.py`", prompt)
        self.assertIn(r"C:\ev\expert_notes.md", prompt)
        self.assertIn("只读", prompt)
        self.assertIn(r"C:\live", prompt)
        self.assertIn("验收条件你无权修改", prompt)
        self.assertIn("卡住的原因：白屏", prompt)
        self.assertIn("厂内施工解决不了", prompt)
        for section in seat.SECTIONS:
            self.assertIn(section, prompt)
        self.assertNotIn("expert_ops.py", prompt)
        self.assertNotIn("权限与外部协调员相同", prompt)
        self.assertNotIn("直接改工厂", prompt)

    def test_prompt_without_targets_forbids_edits(self):
        prompt = seat.research_prompt("材料", "", mode="opening")
        self.assertIn("什么都不能改", prompt)
        self.assertIn("本单点名请你先出手", prompt)

    def test_missing_config_defaults_to_two(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(seat.pool_max(folder), 2)
            self.assertTrue(seat.pool_enabled(folder))
            self.assertIn("活仓只读", seat.pool_note(folder))
            self.assertNotIn("权限与外部协调员相同", seat.pool_note(folder))

    def test_config_raises_the_pool(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "expert_pool.json").write_text('{"enabled": true, "max": 4}', encoding="utf-8")
            rows = seat.seat_items([], [], "18:00", folder)
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[3]["seat"], "驻厂外聘专家 4")

    def test_running_marked_orders_do_not_light_the_seat(self):
        """单子带专家标记只说明它想要专家；专家没出场，席位灯不许亮。"""
        running = [
            {"order_id": "A", "status": "running", "title": "甲·架构升级R1"},
            {"order_id": "B", "status": "running", "title": "乙·驻厂专家"},
        ]
        with tempfile.TemporaryDirectory() as folder:
            rows = seat.seat_items(running, [], "18:00", folder)
        self.assertEqual([row["status"] for row in rows], ["idle", "idle"])

    def test_seat_lights_only_while_an_expert_stage_is_really_on(self):
        with tempfile.TemporaryDirectory() as folder:
            running = [{"order_id": "A", "status": "running", "title": "甲·架构升级R2"}]
            seat.stage_begin("A", 2, "rescue", "甲·架构升级R2", folder)
            rows = seat.seat_items(running, [], "18:00", folder)
            self.assertEqual(rows[0]["status"], "active")
            self.assertEqual(rows[0]["order"], "A")
            self.assertEqual(rows[0]["title"], "甲·架构升级R2")
            self.assertEqual(rows[1]["status"], "idle")
            live = json.loads(Path(folder, seat.LIVE_NAME).read_text(encoding="utf-8"))
            self.assertEqual([r["mode"] for r in live["active"]], ["rescue"])
            seat.stage_end("A", folder)
            rows = seat.seat_items(running, [], "18:00", folder)
            self.assertEqual([row["status"] for row in rows], ["idle", "idle"])

    def test_dead_process_rows_do_not_count(self):
        with tempfile.TemporaryDirectory() as folder:
            dead = 99999999
            Path(folder, seat.LIVE_NAME).write_text(json.dumps({"active": [{"order_id": "Z", "pid": dead, "started_at": 1}]}), encoding="utf-8")
            self.assertEqual(seat.active_stages(folder), [])
            self.assertTrue(all(row["status"] == "idle" for row in seat.seat_items([], [], "18:00", folder)))
            self.assertTrue(seat._alive(os.getpid()))

    def test_probe_only_asks_the_system(self):
        """探活只问系统，不发信号。旧写法 os.kill(pid, 0) 在 Windows 上是给同控制台的其它进程发 Ctrl+C
        （2026-11-26 凌晨 G42R4 跑单进程四次「没正常收口」就是被套件里的探活捅的）。"""
        if os.name != "nt":
            self.skipTest("Windows 专属：POSIX 上 os.kill(pid, 0) 确实只是探活")
        real_kill = seat.os.kill
        try:
            seat.os.kill = lambda pid, sig: self.fail("探活不许调 os.kill(%r, %r)" % (pid, sig))
            self.assertTrue(seat._alive(os.getpid()))
        finally:
            seat.os.kill = real_kill

    def test_bad_or_exited_pids_are_not_alive(self):
        for bad in (0, -5, None, "", "x"):
            self.assertFalse(seat._alive(bad))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=60)              # 已经退出；句柄还攥在手里，pid 不会被别的进程复用
        try:
            self.assertFalse(seat._alive(proc.pid))
        finally:
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream is not None:
                    stream.close()

    def test_third_expert_waits_when_two_are_held(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "expert_pool.json").write_text('{"enabled": true, "max": 2}', encoding="utf-8")
            self.assertTrue(seat.try_acquire("A", folder))
            self.assertTrue(seat.try_acquire("B", folder))
            self.assertFalse(seat.try_acquire("C", folder))
            seat.release("A", folder)
            self.assertTrue(seat.try_acquire("C", folder))
            rows = seat.seat_items([], [], "18:00", folder)
            self.assertEqual(sorted(row["order"] for row in rows if row["status"] == "active"), ["B", "C"])


if __name__ == "__main__":
    unittest.main()
