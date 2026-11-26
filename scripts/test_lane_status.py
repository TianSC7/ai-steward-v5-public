# -*- coding: utf-8 -*-
"""「并发数」页数据源回归：lane_status.collect（无网络/无 LLM，全部临时 fixture）

覆盖：
  1 cap 取 state/lane_caps.json；总 cap=21、可用 cap 扣除 GLM
  2 已用 = 窗口内 provider+model 精确匹配的不同会话数；cancelled 不计；超窗不计
  3 GLM 恒不可用（官方直连无额度）；NEX/外包不设月包
  4 月包 90% → 预警、100% → 耗尽不可用；只读当月键
  5 routing.json 改道后仍可归属（并集口径）
  6 未归属通道单独列出，不静默并入 lane
  7 会话库不可读 → db_ok=False、used=None（不伪造 0）
  8 与 factory_runner 的月包常量一致（防两处漂移）
  9 进行中会话：part 近窗活动即计入（按该会话最近一条 usage 归属），超 live 窗不计
  10 官方额度：STEP credit_buckets / R4 cli/key USD / APIgoto；API 优先，无 key 上限回落
     本地设定（state/lane_quota_target.json），平台限额与本地目标不一致时注明
  11 速度段（C-20261126-G57）：网关日志 event=ok 的 tok/s，分道 + 总计 + tps 算法
  12 auto 族归并（C-20261126-G57）：日志落点 priority（basis=log）/ 无日志按 routing
     aliases.order 主选（basis=routing）；lanes 无 AUTO-HEAVY；unattributed 无 auto 族；
     日志缺失标 missing、坏行标 partial 且不抛错
  13 网关模型（C-20261126-G58）：别名全集（routing 键 + 缺的补齐，含 relay）；sessions/tokens
     取会话库 new-provider-7 别名维度、reqs/fails/tps 取网关日志 req_model（fail 只计 fails、
     ms_sum=0→None）；status ok/warn/idle；merged_to（auto 族非空、relay 空）；
     gateway_totals 汇总；别名不进 cap/可用上限；库不可读时 None 不伪造 0；
     relay 不并进任何通道 → 仍按旧语义归「未归属」，totals 闭环保持

用法：python -X utf8 scripts/test_lane_status.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import lane_status as LS  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def make_db(path, rows, parts=()):
    con = sqlite3.connect(path)
    try:
        con.execute("""create table model_usage(
            session_id text, provider_id text, model_id text, started_at integer,
            status text, computed_total_tokens integer)""")
        con.executemany("insert into model_usage values(?,?,?,?,?,?)", rows)
        con.execute("create table part(session_id text, time_created integer)")
        con.executemany("insert into part values(?,?)", parts)
        con.commit()
    finally:
        con.close()


def make_root(tmp, *, caps=None, ledger=None, routing=None, slots=None):
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
    d = {"version": 1,
         "caps": caps or {"R4": 6, "R4-2": 6, "STEP": 3, "NEX": 2, "GLM": 3, "OUTSOURCE": 1},
         "note": "fixture"}
    json.dump(d, open(os.path.join(tmp, "state", "lane_caps.json"), "w", encoding="utf-8"), ensure_ascii=False)
    if ledger is not None:
        json.dump(ledger, open(os.path.join(tmp, "state", "v3_budget_ledger.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    if routing is not None:
        json.dump(routing, open(os.path.join(tmp, "config", "routing.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    if slots is not None:
        json.dump(slots, open(os.path.join(tmp, "state", "runner_slots.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    json.dump({"v4": {"slots": {"enabled": True, "max": 2}}},
              open(os.path.join(tmp, "config", "steward.json"), "w", encoding="utf-8"))


def make_logs(path, lines, day=None):
    """写一份网关日志夹具（gateway-<day>.jsonl）；lines 为 dict 或原始字符串（模拟坏行）。"""
    os.makedirs(path, exist_ok=True)
    day = day or NOW.strftime("%Y%m%d")
    with open(os.path.join(path, "gateway-%s.jsonl" % day), "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write((ln if isinstance(ln, str) else json.dumps(ln, ensure_ascii=False)) + "\n")


def now_str(offset_s=0):
    """窗口内的 ts（秒，浮点）；offset_s 为相对 NOW 的偏移。"""
    return NOW_MS / 1000.0 + offset_s


def before_window():
    """窗口外（默认 5 分钟窗，取 10 分钟前的 ts）。"""
    return (NOW_MS - 10 * 60 * 1000) / 1000.0


NOW = dt.datetime(2026, 11, 25, 9, 0, 0)
NOW_MS = int(NOW.timestamp() * 1000)
IN = NOW_MS - 60 * 1000          # 窗口内
OUT = NOW_MS - 30 * 60 * 1000    # 窗口外
CUR_M = NOW.strftime("%Y-%m")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="lane_status_")
    try:
        # 默认日志目录指到空目录：速度段/auto 归并不受本机真实网关日志影响（用例自己给 log_dir）
        LS.LOGS = os.path.join(tmp, "nologs")
        db = os.path.join(tmp, "cli.sqlite")
        rows = [
            # R4 两会话（一条 cancelled 不计，一条超窗不计）
            ("s-r4-a", "new-provider-6", "deepseek-v4.1-flash", IN, "completed", 10),
            ("s-r4-b", "new-provider-6", "deepseek-v4.1-flash", IN, "error", 10),
            ("s-r4-c", "new-provider-6", "deepseek-v4.1-flash", IN, "cancelled", 10),
            ("s-r4-d", "new-provider-6", "deepseek-v4.1-flash", OUT, "completed", 10),
            # R4-2 一会话
            ("s-r42", "new-provider-11", "deepseek-v4.1-flash", IN, "completed", 10),
            # STEP 两模型各一会话（都归 STEP）
            ("s-step1", "new-provider-10", "step-5-preview", IN, "completed", 10),
            ("s-step2", "new-provider-10", "step-3.7-flash", IN, "completed", 10),
            # NEX / GLM / 外包
            ("s-nex", "new-provider-5", "nex-n2.6", IN, "completed", 10),
            ("s-glm", "account:bigmodel-start-plan", "GLM-5.3-Flash", IN, "completed", 10),
            ("s-out", "outsource-minekuai", "deepseek-v4.1-flash-1017", IN, "completed", 10),
            # 未归属（路由表外的 provider/model；auto 族不在此列，已归并到落点通道）
            ("s-gw", "new-provider-9", "mystery-model", IN, "completed", 10),
        ]
        make_db(db, rows)
        ledger = {"date": "2026-11-24",
                  "month_lane_tokens": {CUR_M: {"r4": 5_400_000_000,   # 90% → 预警
                                                "r42": 12_000_000_000,  # 100% → 耗尽
                                                "step": 1_000},
                                        "2026-10": {"r4": 999}}}
        routing = {"lanes": [
            {"id": "r4-dsv41", "providerId": "new-provider-6", "model": "deepseek-v4.1-flash"},
            {"id": "r42-dsv41", "providerId": "new-provider-11", "model": "deepseek-v4.1-flash"},
            {"id": "step-flash", "providerId": "new-provider-10", "model": "step-5-preview"},
            {"id": "nex-n26", "providerId": "new-provider-5", "model": "nex-n2.6"},
        ]}
        # 1/2/3/4/6 主 fixture
        root = os.path.join(tmp, "root")
        make_root(root, ledger=ledger, routing=routing, slots={"slots": {"a": {}, "b": {}}, "max": 2})
        d = LS.collect(root=root, db_path=db, now_ms=NOW_MS, window_min=5, quota={})
        lanes = {x["id"]: x for x in d["lanes"]}
        check("L1 cap 读取与总 cap", lanes["R4"]["cap"] == 6 and d["totals"]["cap"] == 21, d["totals"])
        check("L2 已用按会话去重且排除 cancelled/超窗", lanes["R4"]["used"] == 2, lanes["R4"]["used"])
        check("L2b R4-2/STEP/NEX/外包已用", (lanes["R4-2"]["used"], lanes["STEP"]["used"],
                                            lanes["NEX"]["used"], lanes["OUTSOURCE"]["used"]) == (1, 2, 1, 1),
              [lanes[k]["used"] for k in ("R4-2", "STEP", "NEX", "OUTSOURCE")])
        check("L3 GLM 恒不可用", lanes["GLM"]["available"] is False and lanes["GLM"]["status"] == "不可用",
              lanes["GLM"]["status"])
        check("L3b 可用上限扣除不可用通道（GLM 3 + R4-2 耗尽 6）", d["totals"]["usable_cap"] == 12
              and d["totals"]["down_lanes"] == 2, (d["totals"]["usable_cap"], d["totals"]["down_lanes"]))
        check("L3c NEX 不限量且不设月包", lanes["NEX"]["month_quota"] is None and lanes["NEX"]["available"] is True,
              (lanes["NEX"]["month_quota"], lanes["NEX"]["status"]))
        check("L4 90% 预警 / 100% 耗尽", lanes["R4"]["status"] == "预警" and lanes["R4"]["available"] is True
              and lanes["R4-2"]["available"] is False and lanes["R4-2"]["status"] == "耗尽",
              (lanes["R4"]["status"], lanes["R4-2"]["status"]))
        check("L4b 月包只取当月键", lanes["R4"]["month_tokens"] == 5_400_000_000, lanes["R4"]["month_tokens"])
        check("L6 未归属单独列出且计入会话总数", d["unattributed"]["pairs"]
              and d["unattributed"]["pairs"][0]["pair"] == "new-provider-9/mystery-model"
              and d["totals"]["used"] == 9, (d["unattributed"], d["totals"]["used"]))
        check("L7 工厂槽位读取", d["factory"]["max"] == 2 and d["factory"]["active"] == 2, d["factory"])

        # 5 routing 改道：旧 provider 仍归属，新 provider 也归属（并集）
        routing2 = {"lanes": [{"id": "r4-dsv41", "providerId": "new-provider-99", "model": "new-model"}]}
        root2 = os.path.join(tmp, "root2")
        make_root(root2, routing=routing2)
        db2 = os.path.join(tmp, "cli2.sqlite")
        make_db(db2, [("s-new", "new-provider-99", "new-model", IN, "completed", 1),
                      ("s-old", "new-provider-6", "deepseek-v4.1-flash", IN, "completed", 1)])
        d2 = LS.collect(root=root2, db_path=db2, now_ms=NOW_MS, quota={})
        l2 = {x["id"]: x for x in d2["lanes"]}
        check("L5 routing 改道后新旧通道均归属 R4", l2["R4"]["used"] == 2, l2["R4"]["used"])

        # 7 会话库不可读 → used=None，不伪造 0
        d3 = LS.collect(root=root, db_path=os.path.join(tmp, "nope.sqlite"), now_ms=NOW_MS, quota={})
        check("L7b 库不可读不伪造 0", d3["db_ok"] is False and all(x["used"] is None for x in d3["lanes"])
              and d3["totals"]["used"] is None, (d3["db_ok"], d3["db_error"][:40]))

        # 8 常量与 factory_runner 一致
        import factory_runner as R
        check("L8 月包常量与 factory_runner 同值", LS._quotas() == R.LANE_TOKEN_QUOTA_MONTH,
              LS._quotas())

        # 9 进行中会话：model_usage 还没落窗，但 part 近 live 窗有活动 → 按最近一条 usage 归属
        db4 = os.path.join(tmp, "cli4.sqlite")
        make_db(db4, [("s-live-old", "new-provider-6", "deepseek-v4.1-flash", OUT, "completed", 1)],
                parts=[("s-live-old", NOW_MS - 30 * 1000),
                       ("s-live-nomap", NOW_MS - 20 * 1000),
                       ("s-live-stale", NOW_MS - 10 * 60 * 1000)])
        d4 = LS.collect(root=root, db_path=db4, now_ms=NOW_MS, window_min=5, live_min=3, quota={})
        l4 = {x["id"]: x for x in d4["lanes"]}
        check("L9 进行中会话并入（part 近窗 + 最近 usage 归属）",
              l4["R4"]["used"] == 1 and l4["R4"]["live"] == 1 and d4["totals"]["used"] == 2
              and d4["totals"]["live"] == 2 and d4["totals"]["live_only"] == 2,
              (l4["R4"]["used"], l4["R4"].get("live"), d4["totals"]))
        check("L9b 超出 live 窗的 part 不计", d4["totals"]["live"] == 2, d4["totals"]["live"])

        # 10/11 官方 /usage 解析与优先级（STEP credit_buckets / APIgoto subscription）
        sp = {"plan": {"name": "Token-Max-月付"},
              "quota": {"allowed": True, "credit_buckets": [
                  {"total": "1000", "remaining": "800", "used_percent": 20.0,
                   "resets_at_ms": 1797657600000, "expires_at_ms": 1801826766000}]}}
        q1 = LS._parse_usage(sp)
        check("L10 STEP /usage 解析", q1["total"] == 1000 and q1["remaining"] == 800
              and q1["used_pct"] == 20.0 and q1["plan"] == "Token-Max-月付"
              and q1["resets_at_ms"] == 1797657600000 and q1["allowed"] is True, q1)
        ap = {"is_active": True, "mode": "subscription", "remaining": 12.5, "unit": "USD",
              "subscription": {"plan_name": "RouterCode PLUS", "used": 50, "limit": 100,
                               "expires_at": "2027-06-01T07:59:45Z", "unit": "rpm_credits"}}
        q2 = LS._parse_usage(ap)
        check("L10b APIgoto /usage 解析", q2["remaining"] == 12.5 and q2["used_pct"] == 50.0
              and q2["plan"] == "RouterCode PLUS" and q2["remaining_unit"] == "USD", q2)
        kp = {"key": {"name": "tina"}, "limit_usd": "40.000000", "spent_usd": "20.607920",
              "remaining_usd": "19.392080",
              "today": {"request_count": 444, "charged_usd_total": "0.559614"},
              "series": [{"charged_usd_total": "1.5", "request_count": 2},
                         {"charged_usd_total": "2.5", "request_count": 3}]}
        k1 = LS._parse_usage(kp)
        check("L10c R4 /cli/key 解析", k1["total"] == 40 and abs(k1["remaining"] - 19.39208) < 1e-9
              and k1["used_pct"] == 51.52 and k1["today_usd"] == 0.5596
              and k1["today_requests"] == 444 and k1["week_usd"] == 4.0
              and k1["allowed"] is True, k1)
        kp0 = dict(kp, limit_usd=None, remaining_usd=None, spent_usd="32.067403")
        k2 = LS._parse_usage(kp0)
        check("L10d R4 无 key 上限解析", k2["total"] is None and k2["used_pct"] is None
              and k2["remaining"] is None and k2["allowed"] is True
              and abs(k2["used"] - 32.067403) < 1e-9, k2)
        dq = LS.collect(root=root, db_path=db, now_ms=NOW_MS, quota={
            "STEP": {"ok": True, "source": "api", "plan": "Token-Max-月付",
                     "total": 1000, "remaining": 800, "used_pct": 20.0, "allowed": True,
                     "resets_at_ms": 1797657600000}})
        lq = {x["id"]: x for x in dq["lanes"]}
        check("L11 官方额度覆盖台账并保留台账值", lq["STEP"]["quota_source"] == "api"
              and lq["STEP"]["quota_pct"] == 20.0 and lq["STEP"]["month_tokens"] == 200
              and lq["STEP"]["month_quota"] == 1000 and lq["STEP"]["ledger_tokens"] == 1_000
              and lq["STEP"]["status"] == "可用", (
                  lq["STEP"]["quota_source"], lq["STEP"]["quota_pct"],
                  lq["STEP"]["month_tokens"], lq["STEP"]["ledger_tokens"]))
        dq2 = LS.collect(root=root, db_path=db, now_ms=NOW_MS,
                         quota={"STEP": {"ok": False, "source": "api", "error": "timeout"}})
        lq2 = {x["id"]: x for x in dq2["lanes"]}
        check("L11b 官方失败回落台账", lq2["STEP"]["quota_source"] == "ledger"
              and lq2["STEP"]["month_tokens"] == 1_000 and lq2["STEP"]["quota_pct"] is not None,
              (lq2["STEP"]["quota_source"], lq2["STEP"]["month_tokens"]))
        check("L11c 非官方通道标台账/无来源", lq2["R4"]["quota_source"] == "ledger"
              and lq2["NEX"]["quota_source"] == "none" and lq2["GLM"]["quota_source"] == "none",
              [lq2[k]["quota_source"] for k in ("R4", "NEX", "GLM")])
        # USD key 限额：官方优先于台账（root 台账里 R4-2 是 100% 耗尽）
        dq4 = LS.collect(root=root, db_path=db, now_ms=NOW_MS, quota={
            "R4-2": {"ok": True, "source": "api", "unit": "usd", "plan": "Key 限额",
                     "total": 40.0, "remaining": 19.39208, "used": 20.60792,
                     "used_pct": 51.52, "allowed": True, "today_usd": 0.56,
                     "today_requests": 444, "week_usd": 17.76}})
        lq4 = {x["id"]: x for x in dq4["lanes"]}
        check("L11d USD 官方限额覆盖台账耗尽", lq4["R4-2"]["quota_source"] == "api"
              and lq4["R4-2"]["quota_basis"] == "api" and lq4["R4-2"]["month_quota"] == 40.0
              and lq4["R4-2"]["quota_pct"] == 51.52 and lq4["R4-2"]["status"] == "可用"
              and abs(lq4["R4-2"]["quota_remaining"] - 19.39208) < 1e-9
              and lq4["R4-2"]["quota_today_usd"] == 0.56,
              (lq4["R4-2"]["quota_source"], lq4["R4-2"]["month_quota"],
               lq4["R4-2"]["quota_pct"], lq4["R4-2"]["status"]))
        # API 返回 limit_usd=null → 按本地设定 $60 计
        root4 = os.path.join(tmp, "root4")
        make_root(root4)
        json.dump({"usd": {"R4": 60, "R4-2": 120}},
                  open(os.path.join(root4, "state", "lane_quota_target.json"), "w", encoding="utf-8"))
        dq5 = LS.collect(root=root4, db_path=db, now_ms=NOW_MS, quota={
            "R4": {"ok": True, "source": "api", "unit": "usd", "plan": "Key 限额",
                   "total": None, "remaining": None, "used": 32.0674, "used_pct": None,
                   "allowed": True, "today_usd": 0.02, "today_requests": 8, "week_usd": 22.08}})
        lq5 = {x["id"]: x for x in dq5["lanes"]}
        check("L11e 无 key 上限回落本地设定", lq5["R4"]["quota_basis"] == "target"
              and lq5["R4"]["month_quota"] == 60.0 and lq5["R4"]["quota_target"] == 60.0
              and abs(lq5["R4"]["month_tokens"] - 32.0674) < 1e-6
              and abs(lq5["R4"]["quota_pct"] - 53.45) < 0.01
              and abs(lq5["R4"]["quota_remaining"] - 27.9326) < 1e-6
              and lq5["R4"]["status"] == "可用",
              (lq5["R4"]["quota_basis"], lq5["R4"]["month_quota"],
               lq5["R4"]["quota_pct"], lq5["R4"]["quota_remaining"]))
        dq6 = LS.collect(root=root4, db_path=db, now_ms=NOW_MS, quota={
            "R4-2": {"ok": True, "source": "api", "unit": "usd", "total": 40.0,
                     "remaining": 19.39208, "used": 20.60792, "used_pct": 51.52,
                     "allowed": True}})
        lq6 = {x["id"]: x for x in dq6["lanes"]}
        check("L11f 平台限额与本地目标不一致时注明", "本地目标 $120.00" in lq6["R4-2"]["reason"]
              and "平台当前限额 $40.00" in lq6["R4-2"]["reason"], lq6["R4-2"]["reason"])
        check("L12 兜底额度单列不并入 lane", dq.get("quota_extra") == [], dq.get("quota_extra"))
        dq3 = LS.collect(root=root, db_path=db, now_ms=NOW_MS, quota={
            "APIgoto": {"ok": True, "source": "api", "plan": "RouterCode PLUS",
                        "remaining": 12.5, "remaining_unit": "USD", "used_pct": 16.0}})
        check("L12b 兜底额度出现在 quota_extra", dq3["quota_extra"]
              and dq3["quota_extra"][0]["id"] == "APIgoto"
              and dq3["quota_extra"][0]["remaining"] == 12.5, dq3.get("quota_extra"))

        # ---- 11/12 C-20261126-G57：速度段 + auto 族归并 ----
        # 13 tps 算法：tok_out / (ms_sum/1000)；ms_sum=0/缺失 → None（不造数）
        check("N1 tps = tok_out/(ms_sum/1000) 且 ms_sum<=0 → None",
              LS._tps(1000, 2000) == 500.0 and LS._tps(777, 0) is None
              and LS._tps(0, 1000) == 0.0 and LS._tps(5, None) is None,
              (LS._tps(1000, 2000), LS._tps(777, 0)))

        # auto 族夹具：DB 里 auto-heavy 3 会话 / auto-night 1 会话，网关日志落点 r4 ×3、r42 ×1
        db5 = os.path.join(tmp, "cli5.sqlite")
        make_db(db5, [
            ("s-ah1", "new-provider-7", "auto-heavy", IN, "completed", 100),
            ("s-ah2", "new-provider-7", "auto-heavy", IN, "completed", 200),
            ("s-ah3", "new-provider-7", "auto-heavy", IN, "completed", 300),
            ("s-an1", "new-provider-7", "auto-night", IN, "completed", 50),
        ])
        logs5 = os.path.join(tmp, "logs5")
        make_logs(logs5, [          # 网关日志按时间顺序追加（append-only），夹具同样按 ts 升序写
            {"ts": before_window(), "event": "ok", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 1000, "tokens_out": 9999},                      # 超窗不计
            {"ts": now_str(-50), "event": "ok", "lane": "step-flash", "req_model": "auto-night",
             "ms": 1000, "tokens_out": 250},
            {"ts": now_str(-40), "event": "ok", "lane": "r42-dsv41", "req_model": "auto-heavy",
             "ms": 1000, "tokens_out": 100},
            {"ts": now_str(-30), "event": "ok", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 2000, "tokens_out": 1000},
            {"ts": now_str(-20), "event": "ok", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 1000, "tokens_out": 500},
            {"ts": now_str(-10), "event": "ok", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 1000, "tokens_out": 500},
            {"ts": now_str(-8), "event": "fail", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 1000, "tokens_out": 8888},                      # 非 ok 不计
            {"ts": now_str(-6), "event": "ok", "lane": "mystery-lane", "req_model": "auto",
             "ms": 1000, "tokens_out": 42},                        # 未知 lane → unmapped
        ])
        d5 = LS.collect(root=root, db_path=db5, now_ms=NOW_MS, window_min=5, quota={},
                        log_dir=logs5)
        l5 = {x["id"]: x for x in d5["lanes"]}
        sp5 = d5["speed"]
        check("N2 speed 段结构/source/只含有请求的通道", sp5["source"] == "gateway-log"
              and sp5["window_min"] == 5
              and sorted(sp5["lanes"]) == ["R4", "R4-2", "STEP"]
              and sp5["lanes"]["R4"]["reqs"] == 3 and sp5["lanes"]["R4"]["tok_out"] == 2000
              and sp5["lanes"]["R4"]["ms_sum"] == 4000
              and sp5["lanes"]["R4"]["tps"] == 500.0, sp5["lanes"])
        check("N2b speed.total = 各 lane 之和且 tps 同算法",
              sp5["total"]["reqs"] == 5 and sp5["total"]["tok_out"] == 2350
              and sp5["total"]["ms_sum"] == 6000 and sp5["total"]["tps"] == 391.67
              and d5["totals"]["tok_out"] == 2350 and d5["totals"]["tps"] == 391.67,
              sp5["total"])
        check("N2c 未知 lane 进 unmapped 明细不静默丢",
              sp5["unmapped"]["reqs"] == 1 and sp5["unmapped"]["tok_out"] == 42
              and sp5["unmapped"]["lanes"]["mystery-lane"]["reqs"] == 1, sp5["unmapped"])
        check("N3 lanes 数组不再包含 AUTO-HEAVY",
              all(x["id"] != "AUTO-HEAVY" for x in d5["lanes"])
              and "AUTO-HEAVY" not in [x["id"] for x in d5["lanes"]], [x["id"] for x in d5["lanes"]])
        check("N4 auto-heavy 按日志落点并入 R4（basis=log）",
              l5["R4"]["merged"] and l5["R4"]["merged"][0]["alias"] == "auto-heavy"
              and l5["R4"]["merged"][0]["basis"] == "log"
              and l5["R4"]["merged"][0]["sessions"] == 3
              and l5["R4"]["merged"][0]["tokens"] == 600
              and l5["R4"]["merged"][0]["reqs"] == 3
              and l5["R4"]["used"] == 3 and l5["R4"]["live"] == 0, l5["R4"]["merged"])
        check("N4b auto-night 并入日志落点 STEP", l5["STEP"]["merged"]
              and l5["STEP"]["merged"][0]["alias"] == "auto-night"
              and l5["STEP"]["merged"][0]["basis"] == "log"
              and l5["STEP"]["merged"][0]["sessions"] == 1 and l5["STEP"]["used"] == 1,
              l5["STEP"]["merged"])
        check("N5 auto 族不进 unattributed", all(
            "new-provider-7" not in (p.get("pair") or "") for p in d5["unattributed"]["pairs"])
              and d5["unattributed"]["used"] == 0, d5["unattributed"])
        check("N5b 归并会话计入 totals.used（去重）", d5["totals"]["used"] == 4,
              d5["totals"]["used"])

        # 14 无日志 → 按 routing.json aliases.order[0] 归并，basis=routing
        routing5 = {"lanes": [{"id": "r4-dsv41", "providerId": "new-provider-6",
                               "model": "deepseek-v4.1-flash"}],
                    "aliases": {"auto-heavy": {"order": ["nex-n26", "r4-dsv41"]}}}
        root5 = os.path.join(tmp, "root5")
        make_root(root5, routing=routing5)
        d6 = LS.collect(root=root5, db_path=db5, now_ms=NOW_MS, window_min=5, quota={},
                        log_dir=os.path.join(tmp, "nologs"))
        l6 = {x["id"]: x for x in d6["lanes"]}
        n6 = l6["NEX"]["merged"][0] if l6["NEX"]["merged"] else {}
        check("N6 无日志按 routing aliases.order[0] 归并且标 basis=routing",
              n6.get("alias") == "auto-heavy" and n6.get("basis") == "routing"
              and n6.get("reqs") == 0 and n6.get("sessions") == 3
              and l6["NEX"]["used"] == 3 and d6["speed"]["source"] == "missing",
              (n6, d6["speed"]["source"]))
        check("N6b 无日志时 auto-night 回落内置主选（step-flash → STEP）",
              l6["STEP"]["merged"] and l6["STEP"]["merged"][0]["basis"] == "routing"
              and l6["STEP"]["merged"][0]["alias"] == "auto-night", l6["STEP"]["merged"])

        # 15 坏行/半行 → source=partial，好行照算、不抛错
        logs6 = os.path.join(tmp, "logs6")
        make_logs(logs6, [
            {"ts": now_str(-10), "event": "ok", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 1000, "tokens_out": 400},
            "{不是 JSON",
            '{"ts": 1, "event": "ok", "lane": "r4-dsv4',
        ])
        d7 = LS.collect(root=root, db_path=db5, now_ms=NOW_MS, window_min=5, quota={},
                        log_dir=logs6)
        check("N7 坏行跳过并标 source=partial，好行照算",
              d7["speed"]["source"] == "partial"
              and d7["speed"]["total"]["tok_out"] == 400
              and d7["speed"]["total"]["tps"] == 400.0, (d7["speed"]["source"],
                                                         d7["speed"]["total"]))
        # 16 日志缺失不抛错（速度全 None，会话归并仍成立）
        d8 = LS.collect(root=root, db_path=db5, now_ms=NOW_MS, window_min=5, quota={},
                        log_dir=os.path.join(tmp, "no-such-dir"))
        check("N8 日志缺失不抛错且速度显示 None", d8["speed"]["source"] == "missing"
              and d8["speed"]["total"]["tps"] is None
              and all(x["speed"]["tps"] is None for x in d8["lanes"])
              and d8["totals"]["used"] == 4, (d8["speed"]["source"], d8["totals"]["used"]))
        # 17 解析时限到头（deadline 已过）→ 立即返回且标 partial，不抛错也不假全量
        p9 = os.path.join(logs6, "gateway-%s.jsonl" % LS.datetime.fromtimestamp(
            NOW_MS / 1000.0).strftime("%Y%m%d"))
        rows9, bad9 = LS._gw_rows(p9, (NOW_MS - 5 * 60 * 1000) / 1000.0,
                                  deadline=LS.time.monotonic() - 1)
        check("N9 解析超时即返回并标 partial（不抛错）", bad9 is True and (rows9 == [] or rows9),
              (bad9, len(rows9)))
        d9 = LS.collect(root=root, db_path=db5, now_ms=NOW_MS, window_min=5, quota={},
                        log_dir=logs6)
        check("N9b 正常无时限时同目录仍算全量", d9["speed"]["source"] == "partial"
              and d9["speed"]["total"]["tok_out"] == 400,
              (d9["speed"]["source"], d9["speed"]["total"]["tok_out"]))

        # ---- 18/19 C-20261126-G58：网关模型（别名维度）+ gateway_totals ----
        # 别名行序：routing.json aliases 键在前（夹具故意 auto-night 先、auto-heavy 后），
        # 缺的别名按 ALIAS_FALLBACK 补后（auto / auto-cheap / auto-reason / relay）
        db7 = os.path.join(tmp, "cli7.sqlite")
        make_db(db7, [
            ("s-ah1", "new-provider-7", "auto-heavy", IN, "completed", 100),
            ("s-ah2", "new-provider-7", "auto-heavy", IN, "completed", 200),
            ("s-ah2", "new-provider-7", "auto-heavy", IN + 1000, "completed", 50),   # 同会话两行 → 去重
            ("s-xu", "new-provider-7", "relay", IN, "completed", 500),
            ("s-ac", "new-provider-7", "auto-cheap", IN, "completed", 70),
            ("s-xu-old", "new-provider-7", "relay", OUT, "completed", 999),           # 超窗不计
        ], parts=[("s-xu", NOW_MS - 30 * 1000)])                                     # relay 有会话在跑
        logs7 = os.path.join(tmp, "logs7")
        make_logs(logs7, [
            {"ts": now_str(-60), "event": "ok", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 1000, "tokens_out": 500},
            {"ts": now_str(-50), "event": "ok", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 1000, "tokens_out": 500},
            {"ts": now_str(-40), "event": "fail", "lane": "r4-dsv41", "req_model": "auto-heavy",
             "ms": 900, "tokens_out": 7777},     # 失败只计 fails，不计 reqs/tokens/ms
            {"ts": now_str(-30), "event": "ok", "lane": "nex-n26", "req_model": "relay",
             "ms": 0, "tokens_out": 300},        # ms_sum=0 → tps=None
            {"ts": now_str(-20), "event": "ok", "lane": "mystery-lane", "req_model": "auto",
             "ms": 2000, "tokens_out": 100},     # 落点不在通道表内也照算别名请求
        ])
        routing7 = {"lanes": [{"id": "r4-dsv41", "providerId": "new-provider-6",
                               "model": "deepseek-v4.1-flash"}],
                    "aliases": {"auto-night": {"order": ["nex-n26"]},
                                "auto-heavy": {"order": ["r4-dsv41"]}}}
        root7 = os.path.join(tmp, "root7")
        make_root(root7, routing=routing7)
        d10 = LS.collect(root=root7, db_path=db7, now_ms=NOW_MS, window_min=5, quota={},
                         log_dir=logs7)
        gm = d10["gateway_models"]
        gm_by = {x["id"]: x for x in gm}
        check("G1 别名全集＝routing 键在前 + 缺的补齐（含 relay，共 6 行）",
              [x["id"] for x in gm] == ["auto-night", "auto-heavy", "auto", "auto-cheap",
                                        "auto-reason", "relay"], [x["id"] for x in gm])
        check("G2 别名 sessions/live/tokens 取会话库（同会话去重、超窗不计、含 relay）",
              gm_by["auto-heavy"]["sessions"] == 2 and gm_by["auto-heavy"]["tokens"] == 350
              and gm_by["relay"]["sessions"] == 1 and gm_by["relay"]["tokens"] == 500
              and gm_by["relay"]["live"] == 1 and gm_by["auto-reason"]["sessions"] == 0,
              {k: (v["sessions"], v["tokens"], v["live"]) for k, v in gm_by.items()})
        check("G3 别名 reqs/fails/tps 取网关日志（fail 只计 fails；落点不在通道表也照算）",
              gm_by["auto-heavy"]["reqs"] == 2 and gm_by["auto-heavy"]["fails"] == 1
              and gm_by["auto-heavy"]["tok_out"] == 1000 and gm_by["auto-heavy"]["ms_sum"] == 2000
              and gm_by["auto-heavy"]["tps"] == 500.0 and gm_by["auto"]["reqs"] == 1,
              {k: (v["reqs"], v["fails"], v["tps"]) for k, v in gm_by.items()})
        check("G3b ms_sum=0 → tps=None（不造数）", gm_by["relay"]["ms_sum"] == 0
              and gm_by["relay"]["tps"] is None and gm_by["relay"]["tok_out"] == 300,
              gm_by["relay"])
        check("G4 status：有失败→warn / 有样本无失败→ok / 无样本→idle",
              gm_by["auto-heavy"]["status"] == "warn" and gm_by["auto"]["status"] == "ok"
              and gm_by["relay"]["status"] == "ok"
              and gm_by["auto-cheap"]["status"] == "idle"
              and gm_by["auto-reason"]["status"] == "idle",
              {k: v["status"] for k, v in gm_by.items()})
        check("G5 merged_to：auto 族指向落点通道，relay 恒空",
              gm_by["auto-heavy"]["merged_to"] == ["R4"] and gm_by["auto-cheap"]["merged_to"] == ["NEX"]
              and gm_by["relay"]["merged_to"] == [] and gm_by["auto-reason"]["merged_to"] == [],
              {k: v["merged_to"] for k, v in gm_by.items()})
        gt = d10["gateway_totals"]
        check("G6 gateway_totals 为别名行汇总（sessions/reqs/tok_out/tps）",
              gt["sessions"] == 4 and gt["reqs"] == 4 and gt["tok_out"] == 1400
              and gt["tps"] == 350.0, gt)
        check("G7 别名不计入 cap/可用上限，也不作为通道行",
              d10["totals"]["cap"] == 21 and d10["totals"]["usable_cap"] == 18
              and all(x["id"] not in gm_by for x in d10["lanes"])
              and all("relay" not in [m.get("alias") for m in x["merged"]] for x in d10["lanes"]),
              (d10["totals"]["cap"], d10["totals"]["usable_cap"], [x["id"] for x in d10["lanes"]]))
        check("G8 relay 仍归「未归属」（unattributed/totals 语义不动，Σ各卡+未归属 闭环不破）",
              any(p.get("pair") == "new-provider-7/relay" for p in d10["unattributed"]["pairs"])
              and d10["unattributed"]["used"] == 1
              and d10["totals"]["used_reconciled"] is True,
              (d10["unattributed"], d10["totals"]["used_reconciled"]))
        d11 = LS.collect(root=root7, db_path=os.path.join(tmp, "nope7.sqlite"), now_ms=NOW_MS,
                         window_min=5, quota={}, log_dir=logs7)
        g11 = {x["id"]: x for x in d11["gateway_models"]}
        check("G9 会话库不可读 → 别名 sessions/live/tokens=None 且不伪造 0（日志口径照算）",
              d11["db_ok"] is False
              and all(x["sessions"] is None and x["live"] is None and x["tokens"] is None
                      for x in d11["gateway_models"])
              and d11["gateway_totals"]["sessions"] is None
              and g11["auto-heavy"]["reqs"] == 2 and g11["auto-heavy"]["tps"] == 500.0
              and g11["auto-heavy"]["status"] == "warn",
              (d11["db_ok"], d11["gateway_totals"],
               (g11["auto-heavy"]["reqs"], g11["auto-heavy"]["tps"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-34s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
