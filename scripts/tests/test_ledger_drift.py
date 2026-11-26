# -*- coding: utf-8 -*-
"""ledger_drift 单测（W-20261119-003 / E-056）：口径漂移巡检器。

覆盖：①合成 fixture 复现"归档刷新 time_updated 导致旧口径失真"（旧口径<30m、
新口径>=30m）；②status 台账 sha 漂移扫描命中 W-055 两处旧 7cc8f956 且其余卡
零误报；③白名单生效；④registry 权威指向；⑤last-activity 取 max 语义。
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger_drift as ld  # noqa: E402

FIXED_NOW = 1_800_000_000_000  # 固定"当前时间"，保证断言确定性
MIN = ld.MINUTE_MS


# ---------------------------------------------------------------- ② fixture

def test_fixture_old_standard_under_30m():
    """失真复现：归档刷新把 time_updated 顶新后，旧口径误判为活跃（<30m）。"""
    old_m, _ = ld.repro_distortion(now_ms=FIXED_NOW)
    assert old_m < 30.0


def test_fixture_new_standard_at_least_30m():
    """新口径按 max(message.time_created, model_usage.started_at) 判定，>=30m 判 idle。"""
    _, new_m = ld.repro_distortion(now_ms=FIXED_NOW)
    assert new_m >= 30.0


def test_fixture_control_sessions_agree():
    """无刷新对照组：两口径对"真闲置/真活跃"判定一致，不产生误报。"""
    con = ld.build_fixture(FIXED_NOW)
    try:
        res = {r["id"]: r for r in ld.classify_sessions(con, now_ms=FIXED_NOW)}
    finally:
        con.close()
    assert res["sess_ctrl_idle"]["old_idle"] and res["sess_ctrl_idle"]["new_idle"]
    assert not res["sess_ctrl_active"]["old_idle"] and not res["sess_ctrl_active"]["new_idle"]


def test_last_activity_takes_max_of_message_and_usage():
    """last-activity 语义 = max(message.time_created, model_usage.started_at)。"""
    con = sqlite3.connect(":memory:")
    con.executescript(ld.FIXTURE_SCHEMA)
    con.execute("INSERT INTO session VALUES ('s1', 'max语义样例', ?, ?, NULL, NULL)",
                (FIXED_NOW - 60 * MIN, FIXED_NOW - 60 * MIN))
    con.execute("INSERT INTO message VALUES ('m1', 's1', ?)", (FIXED_NOW - 40 * MIN,))
    con.execute("INSERT INTO model_usage VALUES ('u1', 's1', ?)", (FIXED_NOW - 20 * MIN,))
    try:
        act = ld.activity_map(con)
    finally:
        con.close()
    assert act["s1"] == FIXED_NOW - 20 * MIN  # 取较新者（model_usage）


# ---------------------------------------------------------------- ① sha 漂移

def test_scan_status_drift_hits_exactly_two_w055_refs():
    """命中 W-055 两处旧 7cc8f956（W-055.md 第 6/7 行），应指向合并后 c0ee2344。"""
    findings = ld.scan_status_drift()
    assert len(findings) == 2, findings
    assert all(f["file"] == "W-055.md" for f in findings)
    assert {f["line"] for f in findings} == {6, 7}
    assert all(f["token"].startswith("7cc8f956") for f in findings)
    assert all(f["expect"] == "c0ee2344" for f in findings)


def test_scan_status_drift_zero_false_positives_other_cards():
    """其余卡零误报：除 W-055.md 外，任何状态文件都不得出现在漂移发现中。"""
    findings = ld.scan_status_drift()
    assert {f["file"] for f in findings} == {"W-055.md"}


def test_scan_status_drift_whitelist_skips_audit_records():
    """白名单生效：L1.md 的审计勾稽记录（描述漂移本身）不计入漂移。"""
    default_findings = ld.scan_status_drift()  # 默认白名单含 L1.md
    assert all(f["file"] != "L1.md" for f in default_findings)
    no_wl = ld.scan_status_drift(whitelist=set())  # 关白名单 → L1.md 的历史提及显形
    assert any(f["file"] == "L1.md" and f["token"].startswith("7cc8f956") for f in no_wl)


def test_registry_records_merged_commit():
    """registry 已登记合并后权威指向：W-055 merge_commit = c0ee2344…。"""
    merged = ld.registry_merge_commit()
    assert merged is not None and merged.startswith("c0ee2344")
