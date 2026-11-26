# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

import expert_ops


def _root(folder):
    state = Path(folder) / "state"
    state.mkdir()
    (state / "factory_queue.json").write_text(json.dumps({
        "jobs": [
            {"order_id": "Q1", "status": "queued", "title": "排队"},
            {"order_id": "R1", "status": "running", "title": "施工中"},
        ]
    }, ensure_ascii=False), encoding="utf-8")
    return folder


class ExpertOpsTests(unittest.TestCase):
    def test_hold_stops_a_queued_order_and_spares_a_running_one(self):
        with tempfile.TemporaryDirectory() as folder:
            root = _root(folder)
            held = expert_ops.hold("Q1", "先改工厂再开工", root)
            spared = expert_ops.hold("R1", "想杀掉", root)
            data = json.loads((Path(root) / "state" / "factory_queue.json").read_text(encoding="utf-8"))
            queued = next(row for row in data["jobs"] if row["order_id"] == "Q1")
            running = next(row for row in data["jobs"] if row["order_id"] == "R1")
            self.assertTrue(held["ok"])
            self.assertEqual(queued["expert_hold"], "先改工厂再开工")
            self.assertFalse(spared["ok"])
            self.assertNotIn("expert_hold", running)

    def test_release_clears_the_hold(self):
        with tempfile.TemporaryDirectory() as folder:
            root = _root(folder)
            expert_ops.hold("Q1", "先停", root)
            expert_ops.release("Q1", root)
            data = json.loads((Path(root) / "state" / "factory_queue.json").read_text(encoding="utf-8"))
            queued = next(row for row in data["jobs"] if row["order_id"] == "Q1")
            self.assertNotIn("expert_hold", queued)

    def test_enqueue_uses_the_factory_runner(self):
        command = expert_ops.enqueue_command(["--order", "X"], Path(r"C:\factory"))
        self.assertEqual(command[3], r"C:\factory\scripts\factory_runner.py")
        self.assertEqual(command[4], "enqueue")
        self.assertIn("--order", command)


if __name__ == "__main__":
    unittest.main()
