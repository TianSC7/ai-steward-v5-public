# -*- coding: utf-8 -*-
"""成本守卫边界用例：聚合口径/预算阈值/坏行宽容/空日志/时间窗一个都不能形同虚设。

覆盖 12 个形态：
  1  aggregate-basic    多 lane/tier + task/project 打标 + fail/switch 的聚合口径
  2  verdict-ok         总成本低于 warn 线 -> ok，退出码 0
  3  verdict-warn       warn 线 <= 总成本 < block 线 -> warn，退出码 1
  4  verdict-block      总成本 >= block 线 -> block，退出码 2
  5  bad-lines          坏 JSON/非对象/脏 cost/脏 tokens/无 ts/窗外/非请求事件全跳过不崩
  6  empty-log          空文件 -> 0 请求 0 成本 verdict ok
  7  missing-log        日志缺失 -> log_exists=false + warning，不抛栈
  8  time-window        跨午夜的记录按 ts 归属，只计目标日期
  9  registry-today     台账当日活跃任务/状态分布/dispatcher 会话
 10  registry-bad       台账坏 JSON/根非对象 -> 退化为空清单 + warning
 11  downgrade          高花费 lane 有同 tier 显著更便宜的有价替代 -> 候选；未定价不当便宜证据
 12  readonly+advisory  跑前后日志与台账 sha256 不变；lane block 不抬高总体 verdict

用法：python -X utf8 test_budget_guard.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import budget_guard as bg  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []

BASE_TH = {
    "daily_budget_cny": 10.0, "warn_at_cny": 8.0, "block_at_cny": 10.0,
    "min_success_rate": 0.9, "min_lane_requests": 20,
    "downgrade_margin": 0.2, "dominance_share": 0.6,
}

TODAY = dt.date.today()
T_NOON = dt.datetime.combine(TODAY, dt.time(12, 0)).timestamp()
T_MIDNIGHT = dt.datetime.combine(TODAY, dt.time(0, 30)).timestamp()
T_YESTERDAY = dt.datetime.combine(TODAY - dt.timedelta(days=1), dt.time(23, 30)).timestamp()
T_TOMORROW = dt.datetime.combine(TODAY + dt.timedelta(days=1), dt.time(0, 30)).timestamp()


def sha256(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def mk_log(path: str, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


def ok_row(lane: str, tier: str = "COMPLEX", cost: float = 0.01, ts: float = T_NOON,
           priced: bool = True, tin: int = 100, tout: int = 10, tcached: int = 50,
           task=None, project=None) -> dict:
    r = {"ts": ts, "event": "ok", "lane": lane, "tier": tier, "req_model": "auto",
         "ms": 1000, "tokens_in": tin, "tokens_out": tout, "tokens_cached": tcached,
         "cost_cny": cost, "priced": priced, "stream": True}
    if task:
        r["task"] = task
    if project:
        r["project"] = project
    return r


def fail_row(lane: str, ts: float = T_NOON) -> dict:
    return {"ts": ts, "event": "fail", "lane": lane, "class": "conn", "status": 0,
            "ms": 100, "error": "boom"}


def basic_fixture(root: str) -> tuple[str, str]:
    """6 请求（4 ok 2 fail）+ 1 switch + 2 坏行；成本合计 0.035。"""
    logs = os.path.join(root, "logs")
    os.makedirs(logs, exist_ok=True)
    mk_log(os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d")), [
        ok_row("L1", cost=0.01, tin=100, tout=10, tcached=50),
        ok_row("L1", cost=0.02, tin=200, tout=20, tcached=60),
        ok_row("L2", tier="SIMPLE", cost=0.005, task="W-1", project="p1"),
        ok_row("L2", tier="SIMPLE", cost=0.0, priced=False, tin=10, tout=1, tcached=0),
        fail_row("L1"),
        fail_row("L2"),
        {"ts": T_NOON, "event": "switch", "conv": "abc", "from": "L1", "to": "L2"},
        "{not json",
        "[1,2,3]",
    ])
    reg = os.path.join(root, "registry.json")
    with open(reg, "w", encoding="utf-8") as f:
        json.dump({"tasks": [], "dispatcher": {"sessionId": "sess-disp"}}, f)
    return logs, reg


def check(cond: bool, detail: str) -> tuple[bool, str]:
    return (bool(cond), detail)


def case(name: str, fn) -> None:
    try:
        good, detail = fn()
    except Exception as e:  # 用例自身炸了也算 FAIL，不掩盖
        good, detail = False, "用例异常: %r" % e
    RESULTS.append(("PASS" if good else "FAIL", name, detail))


# ---------------------------------------------------------------- 用例

def c_basic() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-basic-")
    try:
        logs, reg = basic_fixture(root)
        r = bg.analyze(logs, reg, TODAY, BASE_TH)
        errs = []
        if r["requests"] != 6 or r["ok"] != 4 or r["fail"] != 2:
            errs.append("requests=%s ok=%s fail=%s" % (r["requests"], r["ok"], r["fail"]))
        if r["success_rate"] != round(4 / 6, 4):
            errs.append("success_rate=%s" % r["success_rate"])
        if r["total_cny"] != 0.035:
            errs.append("total_cny=%s" % r["total_cny"])
        if r["unpriced_requests"] != 1:
            errs.append("unpriced=%s" % r["unpriced_requests"])
        l1 = r["by_lane"].get("L1") or {}
        if (l1.get("requests"), l1.get("ok"), l1.get("fail"), l1.get("cost_cny")) != (3, 2, 1, 0.03):
            errs.append("by_lane L1=%s" % l1)
        if (l1.get("tokens_in"), l1.get("tokens_out"), l1.get("tokens_cached")) != (300, 30, 110):
            errs.append("L1 tokens=%s" % [l1.get("tokens_in"), l1.get("tokens_out"),
                                          l1.get("tokens_cached")])
        l2 = r["by_lane"].get("L2") or {}
        if (l2.get("cost_cny"), l2.get("unpriced_requests")) != (0.005, 1):
            errs.append("by_lane L2=%s" % l2)
        if l1.get("success_rate") != round(2 / 3, 4):
            errs.append("L1 sr=%s" % l1.get("success_rate"))
        cx = r["by_tier"].get("COMPLEX") or {}
        if (cx.get("requests"), cx.get("cost_cny")) != (2, 0.03):
            errs.append("by_tier COMPLEX=%s" % cx)
        sp = r["by_tier"].get("SIMPLE") or {}
        if (sp.get("requests"), sp.get("cost_cny")) != (2, 0.005):
            errs.append("by_tier SIMPLE=%s" % sp)
        q = r["by_tier"].get("?") or {}  # fail 行无 tier 字段，归 "?" 桶
        if (q.get("requests"), q.get("fail"), q.get("cost_cny")) != (2, 2, 0.0):
            errs.append("by_tier ?=%s" % q)
        if (r["by_task"].get("W-1") or {}).get("cost_cny") != 0.005:
            errs.append("by_task W-1=%s" % r["by_task"].get("W-1"))
        if (r["by_task"].get("unattributed") or {}).get("requests") != 5:
            errs.append("by_task unattributed=%s" % r["by_task"].get("unattributed"))
        if (r["by_project"].get("p1") or {}).get("requests") != 1:
            errs.append("by_project p1=%s" % r["by_project"].get("p1"))
        sk = r["skipped"]
        if (sk["bad_json"], sk["non_dict"], sk["not_request"]) != (1, 1, 1):
            errs.append("skipped=%s" % sk)
        if r["verdict"] != "ok":
            errs.append("verdict=%s" % r["verdict"])
        return check(not errs, "; ".join(errs) or "聚合口径全部命中")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_main(root: str, extra: list[str]) -> tuple[int, dict]:
    logs, reg = basic_fixture(root)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bg.main(["--json", "--logs-dir", logs, "--registry", reg] + extra)
    return code, json.loads(buf.getvalue())


def c_ok() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-ok-")
    try:
        code, r = _run_main(root, ["--budget", "1.0"])
        errs = []
        if code != 0 or r["verdict"] != "ok":
            errs.append("code=%s verdict=%s" % (code, r["verdict"]))
        return check(not errs, "; ".join(errs) or "低于 warn 线 -> ok/退出码0")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_warn() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-warn-")
    try:
        code, r = _run_main(root, ["--budget", "0.04"])  # warn 0.032 <= 0.035 < 0.04
        errs = []
        if code != 1 or r["verdict"] != "warn":
            errs.append("code=%s verdict=%s" % (code, r["verdict"]))
        if r["thresholds"]["warn_at_cny"] != 0.032:
            errs.append("warn_at=%s" % r["thresholds"]["warn_at_cny"])
        return check(not errs, "; ".join(errs) or "warn 区间 -> warn/退出码1")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_block() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-block-")
    try:
        code, r = _run_main(root, ["--budget", "0.02"])  # 0.035 >= 0.02
        errs = []
        if code != 2 or r["verdict"] != "block":
            errs.append("code=%s verdict=%s" % (code, r["verdict"]))
        code2, r2 = _run_main(root, ["--budget", "9.0", "--warn-at", "0.01", "--block-at", "0.03"])
        if code2 != 2 or r2["verdict"] != "block":
            errs.append("显式阈值: code=%s verdict=%s" % (code2, r2["verdict"]))
        return check(not errs, "; ".join(errs) or "超 block 线 -> block/退出码2（含显式阈值）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_bad_lines() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-bad-")
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs)
        mk_log(os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d")), [
            ok_row("L1", cost="abc"),                       # 脏 cost -> 0
            ok_row("L1", cost=None),                        # 缺 cost -> 0
            ok_row("L1", cost=-5.0),                        # 负成本 -> 0
            dict(ok_row("L1"), tokens_in="x", tokens_out=None),  # 脏 tokens -> 0
            {"event": "ok", "lane": "L1", "tier": "COMPLEX", "cost_cny": 0.01,
             "priced": True},                               # 无 ts
            dict(ok_row("L1"), ts="not-a-number"),          # ts 脏
            ok_row("L1", ts=T_YESTERDAY),                   # 窗外
            ok_row("L1", ts=T_TOMORROW),                    # 窗外
            {"ts": T_NOON, "event": "reload", "lanes": 3},  # 非请求事件
            "   ",                                          # 空白行
            "",
        ])
        reg = os.path.join(root, "registry.json")
        with open(reg, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        r = bg.analyze(logs, reg, TODAY, BASE_TH)
        errs = []
        if r["requests"] != 4 or r["ok"] != 4:
            errs.append("requests=%s ok=%s" % (r["requests"], r["ok"]))
        if r["total_cny"] != 0.01:
            errs.append("total_cny=%s" % r["total_cny"])
        l1 = r["by_lane"].get("L1") or {}
        if (l1.get("tokens_in"), l1.get("tokens_out")) != (300, 30):
            errs.append("脏 tokens 未按行归零: %s" % [l1.get("tokens_in"), l1.get("tokens_out")])
        sk = r["skipped"]
        if (sk["no_ts"], sk["out_of_window"], sk["not_request"]) != (2, 2, 1):
            errs.append("skipped=%s" % sk)
        if any("跳过坏行" in w for w in r["warnings"]) is False and sk["bad_json"]:
            errs.append("坏行未进 warnings")
        return check(not errs, "; ".join(errs) or "脏数据全跳过不崩，口径正确")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_empty_log() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-empty-")
    try:
        logs, reg = basic_fixture(root)
        p = os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d"))
        open(p, "w", encoding="utf-8").close()  # 空文件
        r = bg.analyze(logs, reg, TODAY, BASE_TH)
        errs = []
        if not r["log_exists"] or r["requests"] or r["total_cny"] != 0.0:
            errs.append("exists=%s requests=%s total=%s"
                        % (r["log_exists"], r["requests"], r["total_cny"]))
        if r["verdict"] != "ok" or r["by_lane"]:
            errs.append("verdict=%s by_lane=%s" % (r["verdict"], r["by_lane"]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bg.main(["--json", "--logs-dir", logs, "--registry", reg])
        if code != 0:
            errs.append("main code=%s" % code)
        return check(not errs, "; ".join(errs) or "空日志 -> 0 请求 0 成本 ok")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_missing_log() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-miss-")
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs)
        reg = os.path.join(root, "registry.json")
        with open(reg, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        r = bg.analyze(logs, reg, TODAY, BASE_TH)
        errs = []
        if r["log_exists"] or r["total_cny"] != 0.0 or r["verdict"] != "ok":
            errs.append("exists=%s total=%s verdict=%s"
                        % (r["log_exists"], r["total_cny"], r["verdict"]))
        if not any("缺失" in w for w in r["warnings"]):
            errs.append("缺缺失 warning: %s" % r["warnings"])
        return check(not errs, "; ".join(errs) or "日志缺失 -> warning + 0 元 ok")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_time_window() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-win-")
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs)
        mk_log(os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d")), [
            ok_row("L1", cost=0.01, ts=T_MIDNIGHT),    # 今日凌晨
            ok_row("L1", cost=0.02, ts=T_NOON),        # 今日中午
            ok_row("L1", cost=9.99, ts=T_YESTERDAY),   # 昨日 23:30
            ok_row("L1", cost=9.99, ts=T_TOMORROW),    # 明日 00:30
        ])
        reg = os.path.join(root, "registry.json")
        with open(reg, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        r = bg.analyze(logs, reg, TODAY, BASE_TH)
        errs = []
        if r["requests"] != 2 or r["total_cny"] != 0.03:
            errs.append("requests=%s total=%s" % (r["requests"], r["total_cny"]))
        if r["skipped"]["out_of_window"] != 2:
            errs.append("out_of_window=%s" % r["skipped"]["out_of_window"])
        if not any("窗外记录 2 条" in w for w in r["warnings"]):
            errs.append("缺窗外 warning: %s" % r["warnings"])
        return check(not errs, "; ".join(errs) or "跨午夜记录按 ts 归属，窗外剔除")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_registry_today() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-reg-")
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs)
        mk_log(os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d")), [
            ok_row("L1"),
        ])
        reg = os.path.join(root, "registry.json")
        stamp_today = dt.datetime.combine(TODAY, dt.time(9, 0)).isoformat()
        stamp_yday = dt.datetime.combine(TODAY - dt.timedelta(days=1), dt.time(9, 0)).isoformat()
        with open(reg, "w", encoding="utf-8") as f:
            json.dump({"tasks": [
                {"id": "T1", "state": "done", "tier": "COMPLEX", "last_check": stamp_today,
                 "project": "p1", "title": "今日完成"},
                {"id": "T2", "state": "done", "tier": "SIMPLE", "last_check": stamp_yday},
                {"id": "T3", "state": "running", "tier": "MEDIUM", "created_at": stamp_today},
                {"id": "T4", "state": "queued", "tier": "SIMPLE", "last_check": "脏时间"},
            ], "dispatcher": {"sessionId": "sess-disp"}}, f)
        r = bg.analyze(logs, reg, TODAY, BASE_TH)
        rt = r["registry"]
        errs = []
        if not rt["available"] or rt["tasks_total"] != 4:
            errs.append("tasks_total=%s" % rt["tasks_total"])
        ids = [t["id"] for t in rt["tasks_today"]]
        if ids != ["T1", "T3"]:
            errs.append("tasks_today=%s" % ids)
        if rt["by_state"] != {"done": 2, "queued": 1, "running": 1}:
            errs.append("by_state=%s" % rt["by_state"])
        if rt["dispatcher_session"] != "sess-disp":
            errs.append("dispatcher=%s" % rt["dispatcher_session"])
        return check(not errs, "; ".join(errs) or "台账当日活跃/状态分布/dispatcher 正确")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_registry_bad() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-regbad-")
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs)
        mk_log(os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d")),
               [ok_row("L1")])
        errs = []
        for name, content in (("bad", "{not json"), ("list", "[1,2]")):
            reg = os.path.join(root, "registry-%s.json" % name)
            with open(reg, "w", encoding="utf-8") as f:
                f.write(content)
            r = bg.analyze(logs, reg, TODAY, BASE_TH)
            rt = r["registry"]
            if rt["available"] or rt["tasks_today"] or rt["tasks_total"] != 0:
                errs.append("%s: available=%s today=%s" % (name, rt["available"],
                                                           rt["tasks_today"]))
            if not any("registry" in w for w in r["warnings"]):
                errs.append("%s: 缺台账 warning" % name)
        return check(not errs, "; ".join(errs) or "坏台账 -> 空清单 + warning 不抛栈")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_downgrade() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-down-")
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs)
        rows = []
        for _ in range(10):
            rows.append(ok_row("EXP", cost=0.01))     # 均价 0.01，占比 2/3
        for _ in range(10):
            rows.append(ok_row("CHEAP", cost=0.005))  # 便宜 50% > 20% margin
        for _ in range(10):
            rows.append(ok_row("FREE", cost=0.0, priced=False))  # 未定价，不当便宜证据
        for _ in range(10):
            rows.append(ok_row("SICK", cost=0.001))   # 最便宜但 sr=0.5，不健康
        rows += [fail_row("SICK") for _ in range(10)]
        mk_log(os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d")), rows)
        reg = os.path.join(root, "registry.json")
        with open(reg, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        r = bg.analyze(logs, reg, TODAY, BASE_TH)
        cands = r["downgrade_candidates"]
        errs = []
        if len(cands) != 1 or cands[0]["lane"] != "EXP":
            errs.append("cands=%s" % [c["lane"] for c in cands])
        else:
            c = cands[0]
            if c["suggest"] != "CHEAP":
                errs.append("suggest=%s" % c["suggest"])
            peers = [p["lane"] for p in c["cheaper_peers"]]
            if peers != ["CHEAP"]:
                errs.append("peers=%s（未定价/不健康 lane 不应入选）" % peers)
            if c["cheaper_peers"][0]["saving_pct"] != 50.0:
                errs.append("saving=%s" % c["cheaper_peers"][0]["saving_pct"])
        if not any("降档候选" in w for w in r["warnings"]):
            errs.append("缺降档 warning")
        return check(not errs, "; ".join(errs) or "降档候选命中 EXP→CHEAP，未定价不当证据")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def c_readonly_advisory() -> tuple[bool, str]:
    root = tempfile.mkdtemp(prefix="steward-bg-ro-")
    try:
        logs = os.path.join(root, "logs")
        os.makedirs(logs)
        rows = [ok_row("BAD", cost=0.001) for _ in range(20)]
        rows += [fail_row("BAD") for _ in range(20)]  # sr=0.5 < 0.9，n=40 >= 20
        mk_log(os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d")), rows)
        reg = os.path.join(root, "registry.json")
        with open(reg, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        log_p = os.path.join(logs, "gateway-%s.jsonl" % TODAY.strftime("%Y%m%d"))
        before = (sha256(log_p), sha256(reg), sorted(os.listdir(logs)))
        r = bg.analyze(logs, reg, TODAY, BASE_TH)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bg.main(["--json", "--logs-dir", logs, "--registry", reg])
        after = (sha256(log_p), sha256(reg), sorted(os.listdir(logs)))
        errs = []
        if before != after:
            errs.append("只读被破坏: %s -> %s" % (before, after))
        if (r["by_lane"].get("BAD") or {}).get("verdict") != "block":
            errs.append("lane verdict=%s" % (r["by_lane"].get("BAD") or {}).get("verdict"))
        if r["verdict"] != "ok":
            errs.append("lane block 抬高了总体 verdict: %s" % r["verdict"])
        if not any("成功率" in w and "BAD" in w for w in r["warnings"]):
            errs.append("缺 lane 健康 warning")
        return check(not errs, "; ".join(errs) or "只读不变；lane block 仅 advisory")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_budget_guard")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(180, "test_budget_guard")

    case("aggregate-basic", c_basic)
    case("verdict-ok", c_ok)
    case("verdict-warn", c_warn)
    case("verdict-block", c_block)
    case("bad-lines", c_bad_lines)
    case("empty-log", c_empty_log)
    case("missing-log", c_missing_log)
    case("time-window", c_time_window)
    case("registry-today", c_registry_today)
    case("registry-bad", c_registry_bad)
    case("downgrade", c_downgrade)
    case("readonly+advisory", c_readonly_advisory)

    fails = [r for r in RESULTS if r[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-20s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails and RESULTS else 2


if __name__ == "__main__":
    raise SystemExit(main())
