#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V4.0-P1 内核回归测试（factory_runner.py 新增/改动部分）。

覆盖：开关加载与回落 / 观察期熔断 / 墙钟剩余与阶段超时封顶 / Builder 合入前闸判定 /
派单延迟起点 / 审美提示由 uispec_rules.json 生成且不含旧审美数值 / 网关差值估算分列 /
三口径记账 + 观察期熔断不停机 / 全量日志落盘与脱敏截断。

所有写盘路径都重定向到临时目录，不碰真实 state/ 与 logs/。
用法：python -X utf8 test_v4_kernel.py      （stdlib only，退出码 0=全过）
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_runner  # noqa: E402

FR = vars(factory_runner)
TMP = tempfile.mkdtemp(prefix="v4_kernel_test_")
RULES_PATH = os.path.join(HERE, "..", "config", "uispec_rules.json")


def _redirect():
    FR["BUDGET_LEDGER_PATH"] = os.path.join(TMP, "v3_budget_ledger.json")
    FR["PATROL_ALERT"] = os.path.join(TMP, "PATROL_ALERT.txt")
    FR["V4_TRACE_DIR"] = os.path.join(TMP, "v4_trace")
    FR["STEWARD_CONFIG_PATH"] = os.path.join(TMP, "steward.json")
    FR["TOKEN_FILE"] = os.path.join(TMP, "board_token.txt")
    with open(FR["TOKEN_FILE"], "w", encoding="ascii") as f:
        f.write("zc-SECRET-TOKEN")


def _write_flags(v4: dict):
    with open(FR["STEWARD_CONFIG_PATH"], "w", encoding="utf-8") as f:
        json.dump({"stale_sec": 1800, "v4": v4}, f)


def _clear(path):
    if os.path.exists(path):
        os.remove(path)


class Flags(unittest.TestCase):
    def setUp(self):
        _redirect()

    def test_missing_config_falls_back_to_v321(self):
        _clear(FR["STEWARD_CONFIG_PATH"])
        f = FR["load_v4_flags"]()
        self.assertFalse(any(f[k] for k in ("merge_gate", "wallclock_live", "char_icon_hard_fail", "uispec_prompt", "drop_details_gate")))

    def test_partial_flags(self):
        _write_flags({"merge_gate": True, "unknown_key": 1})
        f = FR["load_v4_flags"]()
        self.assertTrue(f["merge_gate"])
        self.assertFalse(f["wallclock_live"])
        self.assertNotIn("unknown_key", f)

    def test_corrupt_config(self):
        with open(FR["STEWARD_CONFIG_PATH"], "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertFalse(FR["load_v4_flags"]()["merge_gate"])

    def test_fuse_dry_run_window(self):
        now = dt.datetime(2026, 11, 22, 12, 0, 0)
        self.assertTrue(FR["fuse_dry_run_active"]({"fuse_dry_run_until": "2026-11-23 12:00:00"}, now))
        self.assertFalse(FR["fuse_dry_run_active"]({"fuse_dry_run_until": "2026-11-22 11:59:59"}, now))
        self.assertFalse(FR["fuse_dry_run_active"]({"fuse_dry_run_until": ""}, now))
        self.assertFalse(FR["fuse_dry_run_active"]({"fuse_dry_run_until": "garbage"}, now))


class Wallclock(unittest.TestCase):
    def test_remaining_and_cap(self):
        rem = FR["wallclock_remaining"](1000.0, 900, now_v=1500.0)
        self.assertEqual(rem, 400.0)
        self.assertEqual(FR["cap_stage_timeout"](900, rem), 400)        # Builder 900s 被封顶到剩余 400s
        self.assertEqual(FR["cap_stage_timeout"](600, 5000), 600)       # 剩余充足不放大
        self.assertEqual(FR["cap_stage_timeout"](600, 3), 30)           # 下限 30s（更小由启动前闸拦截）

    def test_order_wallclock_override(self):
        self.assertEqual(FR["order_wallclock_cap"]({"order_wallclock_sec": 0}), FR["ORDER_WALL_CLOCK_MAX_SEC"])
        self.assertEqual(FR["order_wallclock_cap"]({"order_wallclock_sec": 1500}), 1500)


class MergeGate(unittest.TestCase):
    def test_g13_round2_is_blocked(self):
        """G13 第 2 轮真实形态：rc=124 timed_out=True dur=900.31 —— V3.2.1 照常合入，V4 必须拦。"""
        w = {"shift": "day", "exit_code": 124, "timed_out": True, "duration_sec": 900.31}
        self.assertIn("timed_out", FR["builder_incomplete"](w))
        self.assertIn("rc=124", FR["builder_incomplete"](w))

    def test_stall_blocked(self):
        self.assertEqual(FR["builder_incomplete"]({"exit_code": 0, "stall_timeout": True}), "stall")

    def test_clean_run_passes(self):
        self.assertEqual(FR["builder_incomplete"]({"exit_code": 0, "timed_out": False}), "")

    def test_reuse_draft_exempt(self):
        self.assertEqual(FR["builder_incomplete"]({"shift": "reuse", "exit_code": 0}), "")


class DispatchLatency(unittest.TestCase):
    def test_requeued_ts_preferred(self):
        self.assertEqual(FR["dispatch_base_ts"]({"enqueued_ts": 100.0, "requeued_ts": 900.0}), 900.0)

    def test_requeued_at_parsed(self):
        """G12 真实形态：只有 requeued_at 字符串（人工重派），旧口径算出 4382.94s 虚高。"""
        job = {"enqueued_ts": dt.datetime(2026, 11, 22, 8, 36, 49).timestamp(), "requeued_at": "2026-11-22 09:49:48"}
        started = dt.datetime(2026, 11, 22, 9, 49, 52).timestamp()
        self.assertAlmostEqual(started - FR["dispatch_base_ts"](job), 4.0, places=3)

    def test_fallback_enqueued(self):
        self.assertEqual(FR["dispatch_base_ts"]({"enqueued_ts": 123.0}), 123.0)


class Aesthetic(unittest.TestCase):
    def test_note_generated_from_rules_and_no_legacy_numbers(self):
        rules = json.load(open(RULES_PATH, encoding="utf-8"))
        note = FR["build_aesthetic_note"](rules, RULES_PATH)
        self.assertIn("112px", note)
        self.assertIn("#dc2626", note)
        self.assertIn("#b45309", note)                     # amber 族深字变体在白名单内
        for legacy in ("13–14px", "24–30px", "1 个主强调色", "<details>"):
            self.assertNotIn(legacy, note)                 # D-05：旧审美硬编码不再进入提示词

    def test_note_without_rules_is_generic(self):
        note = FR["build_aesthetic_note"]({}, "")
        self.assertIn("UISPEC", note)
        self.assertNotIn("24–30px", note)


class GatewayDelta(unittest.TestCase):
    def test_estimates_split(self):
        before = {"totals": {"requests": 1, "tokens_in": 10, "tokens_out": 10, "cost_cny": 0.1,
                             "tokens_est_in": 0, "tokens_est_out": 0, "cost_est_cny": 0.0, "estimated_requests": 0},
                  "lanes": [{"id": "r4-dsv41", "ok": 1, "fail": 0, "tokens": [10, 10], "cost_cny": 0.1}]}
        after = {"totals": {"requests": 3, "tokens_in": 1010, "tokens_out": 510, "cost_cny": 0.6,
                            "tokens_est_in": 100, "tokens_est_out": 50, "cost_est_cny": 0.05, "estimated_requests": 1},
                 "lanes": [{"id": "r4-dsv41", "ok": 3, "fail": 0, "tokens": [1010, 510], "cost_cny": 0.6}]}
        d = FR["gateway_delta"](before, after)
        self.assertEqual((d["tokens_in"], d["tokens_est_in"], d["tokens_est_out"], d["estimated_requests"]), (1000, 100, 50, 1))
        self.assertAlmostEqual(d["cost_est_cny"], 0.05)

    def test_old_gateway_compatible(self):
        d = FR["gateway_delta"]({"totals": {"cost_cny": 0.0}}, {"totals": {"cost_cny": 0.2, "tokens_out": 2048}})
        self.assertEqual((d["tokens_est_in"], d["cost_est_cny"]), (0, 0.0))


class Budget(unittest.TestCase):
    def setUp(self):
        _redirect()
        _clear(FR["BUDGET_LEDGER_PATH"])

    def _gw(self, cash=0.0, book=0.0, est=0.0):
        lanes = {"r4-dsv41": {"ok": 1, "fail": 0, "tokens_in": 1000, "tokens_out": 200, "cost_cny": book}}
        if cash:
            lanes["apigoto-deepseek-flash"] = {"ok": 1, "fail": 0, "tokens_in": 10, "tokens_out": 10, "cost_cny": cash}
        return {"requests": 2, "tokens_in": 1010, "tokens_out": 210, "tokens_cached": 0, "cost_cny": book + cash,
                "tokens_est_in": 5, "tokens_est_out": 7, "cost_est_cny": est, "estimated_requests": 1, "lanes": lanes}

    def test_three_columns(self):
        _write_flags({})
        led, card, fuse = FR["record_stage_budget"]("T-1", self._gw(cash=0.5, book=0.3, est=0.01), 0.0)
        self.assertEqual(fuse, "")
        self.assertAlmostEqual(card, 0.5)                       # 现金口径只认 apigoto
        self.assertAlmostEqual(led["daily_cash_cny"], 0.5)
        self.assertAlmostEqual(led["daily_cost_cny"], 0.8)
        self.assertAlmostEqual(led["daily_est_cost_cny"], 0.01)
        self.assertEqual(led["daily_tokens_est"], 12)
        self.assertEqual(led["schema_version"], 2)

    def test_card_fuse_live(self):
        _write_flags({})
        _, _, fuse = FR["record_stage_budget"]("T-2", self._gw(cash=1.0), FR["CARD_BUDGET_CAP_CNY"])
        self.assertIn("单卡钱闸", fuse)

    def test_daily_fuse_dry_run_does_not_stop(self):
        _write_flags({"fuse_dry_run_until": (dt.datetime.now() + dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")})
        big = FR["DAILY_BUDGET_CAP_CNY"] + 1
        led, _, fuse = FR["record_stage_budget"]("T-3", self._gw(cash=big), 0.0)
        self.assertEqual(fuse, "")
        self.assertFalse(led["circuit_open"])
        self.assertEqual(len(led["dry_run_fuse_events"]), 1)
        self.assertIn("V4_FUSE_DRYRUN", open(FR["PATROL_ALERT"], encoding="utf-8").read())

    def test_daily_fuse_live_after_window(self):
        _write_flags({"fuse_dry_run_until": "2020-01-01 00:00:00"})
        led, _, fuse = FR["record_stage_budget"]("T-4", self._gw(cash=FR["DAILY_BUDGET_CAP_CNY"] + 1), 0.0)
        self.assertIn("整线停机", fuse)
        self.assertTrue(led["circuit_open"])


class Trace(unittest.TestCase):
    def setUp(self):
        _redirect()

    def _rows(self):
        d = FR["V4_TRACE_DIR"]
        rows = []
        for fn in sorted(os.listdir(d)):
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                rows += [json.loads(x) for x in f if x.strip()]
        return rows

    def test_trace_written_redacted_truncated(self):
        FR["v4_trace"]("unit", "C-TEST", note="令牌 zc-SECRET-TOKEN 不得落盘", big="x" * 25000, nested={"a": [1, "zc-SECRET-TOKEN"]})
        row = self._rows()[-1]
        self.assertEqual((row["event"], row["order_id"], row["kernel"]), ("unit", "C-TEST", FR["KERNEL_VERSION"]))
        blob = json.dumps(row, ensure_ascii=False)
        self.assertNotIn("zc-SECRET-TOKEN", blob)
        self.assertIn("truncated", row["big"])
        self.assertLess(len(row["big"]), 20100)

    def test_trace_never_raises(self):
        FR["V4_TRACE_DIR"] = os.path.join(FR["TOKEN_FILE"], "cannot_be_dir")   # 路径非法
        FR["v4_trace"]("unit", "X", v=object())                                  # 不抛


if __name__ == "__main__":
    unittest.main(verbosity=2)
