# -*- coding: utf-8 -*-
import unittest
import line_details as ld


class LineDetailTests(unittest.TestCase):
    def test_ledger_keeps_created_at_and_checks(self):
        detail = ld.detail_from_ledger({
            "order_id": "STK-1",
            "created_at": "2026-11-25 12:41:13",
            "status": "done",
            "gate": {"verdict": "PASS", "checks": {"G1_files": {"ok": True, "detail": "4/4"}}},
        }, "2026-11-25 12:50:00")
        self.assertEqual(detail["ordered_at"], "2026-11-25 12:41:13")
        self.assertEqual(len(detail["tasks"]), 1)
        self.assertEqual(len(detail["group_chat"]), 2)

    def test_missing_gate_stays_empty(self):
        detail = ld.detail_from_ledger({"order_id": "STK-2", "created_at": "2026-11-25 12:00:00"})
        self.assertEqual(detail["tasks"], [])
        self.assertEqual(len(detail["group_chat"]), 1)

    def test_avg_is_zero_when_only_line_orders(self):
        rows = [{"order_id": "STK-1", "e2e_total_sec": 30}]
        self.assertEqual(ld.factory_avg_sec(rows, ["STK-1"]), 0.0)

    def test_avg_skips_line_orders(self):
        rows = [
            {"order_id": "STK-1", "e2e_total_sec": 30},
            {"order_id": "C-20261125-G48BR1", "e2e_total_sec": 100},
            {"order_id": "C-20261125-G48BR1b", "e2e_total_sec": 300},
        ]
        self.assertEqual(ld.factory_avg_sec(rows, ["STK-1"]), 200.0)

    def test_missing_events_add_nothing(self):
        self.assertEqual(ld.events_to_chat("G1", ["{", ""]), [])

    def test_event_line_is_kept(self):
        raw = '{"order":"G1","ts":"2026-11-25 17:00:00","actor":"Builder施工","summary":"第 1 轮施工结束"}'
        chat = ld.events_to_chat("G1", [raw])
        self.assertEqual(chat[0]["msg"], "第 1 轮施工结束")


if __name__ == "__main__":
    unittest.main()
