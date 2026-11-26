# -*- coding: utf-8 -*-
"""夜间排班助手边界用例：排序/并发/空队列/坏配置/依赖阻塞/只读/坏台账。

覆盖：
  1  字段契约：plan[]/concurrency/skipped[] 顶层键与卡内键稳定（schema=1）
  2  泳道排序：batch → long-text → scan → standard → light
  3  泳道内排序：tier 权重 → priority → created_at → id
  4  拓扑保序：queued 依赖必须排在前面（跨泳道也拦得住）
  5  依赖已 done -> 放行
  6  依赖不在台账 -> skipped 且原因点名
  7  依赖 failed/abandoned -> skipped 且原因"不会完成"
  8  依赖进行中(dispatched/running) -> skipped 且"待回轮"
  9  依赖成环（A→B→A）-> 双双 skipped
  10 自依赖（A→A）-> skipped 成环
  11 重试耗尽（attempts ≥ max_attempts）-> skipped
  12 非 queued 七态 -> 全部 skipped 且各有明确原因
  13 空队列 -> plan=[]、reason 明确、不抛栈；CLI 退出码 0
  14 坏配置（非 JSON）-> 降级默认并发档、warnings 非空、不抛栈；CLI 退出码 0
  15 并发档：serial/normal/batch 与同项目上限压低
  16 预计时长：确定性、tier+泳道修正、totals 求和一致、wall=ceil(total/suggested)
  17 只读：CLI 跑前后 registry/config sha256 不变
  18 坏台账（非 JSON / tasks 非列表）-> NightShiftError；CLI 退出码 2、无堆栈
  19 --out 落盘：内容与 stdout 一致、原子写无残留
  20 真实台账冒烟（只读）：结构不变量 + 真实 registry sha256 不变

用法：python -X utf8 test_night_shift.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import night_shift as ns  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
NOW = "2026-11-18T22:00:00+08:00"
NOW_DT = ns.parse_now(NOW)
CFG = {"dispatch": {"max_running": 15, "max_running_per_project": 4,
                    "max_attempts": 3, "stale_sec": 1800}}


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def task(tid: str, state: str = "queued", **kw) -> dict:
    base = {
        "id": tid,
        "title": "任务 " + tid,
        "project": "ai-steward",
        "state": state,
        "tier": "MEDIUM",
        "attempts": 0,
        "created_at": "2026-11-18T08:00:00+08:00",
    }
    base.update(kw)
    return base


def plan_of(tasks: list[dict], config: dict | None = None) -> dict:
    return ns.build_plan({"tasks": tasks},
                         config if config is not None else CFG, NOW_DT)


def ids(plan_obj: dict) -> list[str]:
    return [c["id"] for c in plan_obj["plan"]]


def skip_reason(plan_obj: dict, tid: str) -> str:
    for s in plan_obj["skipped"]:
        if s["id"] == tid:
            return s["reason"]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_night_shift")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_night_shift")

    # 1 字段契约
    p = plan_of([task("W-A"), task("W-B", "done")])
    check("field-contract",
          p["schema"] == ns.SCHEMA and p["tool"] == ns.TOOL
          and set(p) >= {"schema", "tool", "generated_at", "window", "sources",
                         "concurrency", "plan", "skipped", "totals", "reason",
                         "warnings"}
          and all(set(c) == set(ns.PLAN_FIELDS) for c in p["plan"])
          and set(p["concurrency"]) == set(ns.CONCURRENCY_FIELDS)
          and all(set(s) == set(ns.SKIPPED_FIELDS) for s in p["skipped"])
          and set(p["totals"]) == set(ns.TOTALS_FIELDS)
          and p["window"]["hours"] == 24,
          "plan=%d skipped=%d" % (len(p["plan"]), len(p["skipped"])))

    # 2 泳道排序：批量 → 长文 → 扫描 → 常规 → 轻活
    lanes = plan_of([
        task("W-LIGHT", tier="SIMPLE", title="顺手小修"),
        task("W-STD", title="成本守卫 v1：按项目聚合当日成本"),
        task("W-SCAN", title="网关健康快照：巡检 /health 与 /admin/status"),
        task("W-LONG", title="夜间摘要：night_digest 生成交接日报"),
        task("W-BATCH", title="dll 仓 62 fixture 填槽批量辅助生成"),
    ])
    got = ids(lanes)
    check("lane-order",
          got == ["W-BATCH", "W-LONG", "W-SCAN", "W-STD", "W-LIGHT"],
          "order=%s" % ",".join(got))
    check("lane-tags",
          [c["lane"] for c in lanes["plan"]] ==
          ["batch", "long-text", "scan", "standard", "light"],
          "lanes=%s" % ",".join(c["lane"] for c in lanes["plan"]))
    check("model-hint-step",
          all(c["model_hint"] == ns.STEP_MODEL for c in lanes["plan"][:3])
          and lanes["plan"][4]["model_hint"] == "new-provider-5/nex-n2.6",
          "前三泳道走 step-5，轻活走 nex")

    # 3 泳道内：tier → priority → created_at → id
    inner = plan_of([
        task("W-S", tier="SIMPLE", title="回归顺手批量改"),
        task("W-M2", tier="MEDIUM", title="回归顺手批量改",
             created_at="2026-11-18T09:00:00+08:00"),
        task("W-R", tier="REASONING", title="回归顺手批量改"),
        task("W-C", tier="COMPLEX", title="回归顺手批量改"),
        task("W-M1", tier="MEDIUM", title="回归顺手批量改", priority=1),
    ])
    check("within-lane-order",
          ids(inner) == ["W-R", "W-C", "W-M1", "W-M2", "W-S"],
          "order=%s" % ",".join(ids(inner)))

    # 4 拓扑保序：queued 依赖（轻活）必须排在依赖它的批量卡前面
    topo = plan_of([
        task("W-BATCH", title="批量回归收尾", depends_on=["W-DEP"]),
        task("W-DEP", tier="SIMPLE", title="搭好台子"),
    ])
    check("dep-topo-order",
          ids(topo) == ["W-DEP", "W-BATCH"]
          and "依赖 W-DEP 已排在前" in topo["plan"][1]["reason"],
          "order=%s" % ",".join(ids(topo)))

    # 5 依赖已 done -> 放行
    done_dep = plan_of([
        task("W-D", "done"),
        task("W-X", depends_on=["W-D"]),
    ])
    check("dep-done-ok", ids(done_dep) == ["W-X"], "plan=%s" % ids(done_dep))

    # 6-8 外部依赖闸
    miss = plan_of([task("W-X", depends_on=["W-NOPE"])])
    check("dep-missing",
          ids(miss) == [] and "W-NOPE(不在台账)" in skip_reason(miss, "W-X"),
          skip_reason(miss, "W-X")[:40])
    dead = plan_of([
        task("W-F", "failed"),
        task("W-AB", "abandoned"),
        task("W-X", depends_on=["W-F", "W-AB"]),
    ])
    check("dep-dead",
          ids(dead) == [] and "不会完成" in skip_reason(dead, "W-X")
          and "W-F(failed" in skip_reason(dead, "W-X"),
          skip_reason(dead, "W-X")[:50])
    flight = plan_of([
        task("W-RUN", "running"),
        task("W-DIS", "dispatched"),
        task("W-X", depends_on=["W-RUN", "W-DIS"]),
    ])
    check("dep-inflight",
          ids(flight) == [] and "待回轮后重排" in skip_reason(flight, "W-X"),
          skip_reason(flight, "W-X")[:50])

    # 9-10 成环
    cyc = plan_of([
        task("W-A", depends_on=["W-B"]),
        task("W-B", depends_on=["W-A"]),
    ])
    check("dep-cycle",
          ids(cyc) == [] and "依赖成环" in skip_reason(cyc, "W-A")
          and "依赖成环" in skip_reason(cyc, "W-B"),
          "A/B 双双跳过")
    self_cyc = plan_of([task("W-A", depends_on=["W-A"])])
    check("dep-self-cycle",
          ids(self_cyc) == [] and "依赖成环" in skip_reason(self_cyc, "W-A"),
          skip_reason(self_cyc, "W-A")[:30])

    # 11 重试耗尽
    ex = plan_of([task("W-X", attempts=3, max_attempts=3)])
    check("attempts-exhausted",
          ids(ex) == [] and "重试次数已耗尽" in skip_reason(ex, "W-X"),
          skip_reason(ex, "W-X")[:40])

    # 12 非 queued 七态
    states = plan_of([
        task("W-DONE", "done"), task("W-AB", "abandoned"),
        task("W-DIS", "dispatched"), task("W-RUN", "running"),
        task("W-BLK", "blocked"), task("W-FAIL", "failed"),
        task("W-WEIRD", "weird"), task("W-Q", "queued"),
    ])
    reasons = {s["id"]: s["reason"] for s in states["skipped"]}
    check("states-skipped",
          ids(states) == ["W-Q"] and len(states["skipped"]) == 7
          and "已完结" in reasons["W-DONE"] and "已完结" in reasons["W-AB"]
          and "不重复派单" in reasons["W-DIS"] and "不重复派单" in reasons["W-RUN"]
          and "解阻" in reasons["W-BLK"] and "换路线" in reasons["W-FAIL"]
          and "未知状态" in reasons["W-WEIRD"],
          "7 态各有明确原因")

    tmp = tempfile.mkdtemp(prefix="steward-nightshift-")
    try:
        # 13 空队列
        empty = plan_of([])
        check("empty-queue",
              empty["plan"] == [] and "队列为空" in empty["reason"]
              and empty["concurrency"]["tier"] == "idle"
              and empty["concurrency"]["suggested"] == 0
              and empty["totals"]["est_wall_minutes"] == 0,
              "reason=%s" % empty["reason"][:30])
        reg_e = os.path.join(tmp, "empty.json")
        cfg_p = os.path.join(tmp, "cfg.json")
        open(reg_e, "w", encoding="utf-8").write(json.dumps({"tasks": []}))
        open(cfg_p, "w", encoding="utf-8").write(json.dumps(CFG))
        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            code_e = ns.main(["--registry", reg_e, "--config", cfg_p, "--now", NOW])
        body = json.loads(out_buf.getvalue())
        check("cli-empty-queue",
              code_e == 0 and body["plan"] == [] and "队列为空" in body["reason"],
              "exit=%d reason=%s" % (code_e, body["reason"][:24]))

        # 14 坏配置：降级不抛栈（config={} 走默认并发档，4 张卡落 normal）
        bad_cfg = os.path.join(tmp, "bad-cfg.json")
        open(bad_cfg, "w", encoding="utf-8").write("{not json")
        cfg_bad, warn = ns.load_config(bad_cfg)
        check("bad-config-load",
              cfg_bad == {} and bool(warn) and "JSON" in warn,
              "warn=%s" % warn[:30])
        deg = ns.attach_notes(
            plan_of([task("W-A"), task("W-B"), task("W-C"), task("W-D")],
                    config={}),
            warn)
        check("bad-config-tolerant",
              deg["warnings"] and deg["reason"] and len(deg["plan"]) == 4
              and deg["concurrency"]["max_running"] == ns.DEFAULT_MAX_RUNNING
              and deg["concurrency"]["max_running_per_project"]
              == ns.DEFAULT_MAX_PER_PROJECT
              and deg["concurrency"]["tier"] == "normal",
              "降级后仍可排班 tier=%s" % deg["concurrency"]["tier"])
        out_bad = io.StringIO()
        with contextlib.redirect_stdout(out_bad):
            code_bad = ns.main(["--registry", reg_e, "--config", bad_cfg,
                                "--now", NOW])
        body_bad = json.loads(out_bad.getvalue())
        check("cli-bad-config",
              code_bad == 0 and body_bad["warnings"] and body_bad["reason"],
              "exit=%d warn=%s" % (code_bad, body_bad["warnings"][0][:24]))
        miss_cfg = os.path.join(tmp, "no-such.json")
        _, warn2 = ns.load_config(miss_cfg)
        check("config-missing", bool(warn2) and "读取失败" in warn2, warn2[:30])

        # 15 并发档
        serial = plan_of([task("W-A")])
        check("concurrency-serial",
              serial["concurrency"]["tier"] == "serial"
              and serial["concurrency"]["suggested"] == 1,
              "1 张卡串行")
        normal = plan_of([task("W-%d" % i) for i in range(5)])
        check("concurrency-normal",
              normal["concurrency"]["tier"] == "normal"
              and normal["concurrency"]["suggested"] == 3,
              "5 张卡 normal/3")
        many = plan_of([task("W-%02d" % i, project="p%02d" % i) for i in range(10)])
        check("concurrency-batch",
              many["concurrency"]["tier"] == "batch"
              and many["concurrency"]["suggested"] == 8,
              "10 张跨 10 项目 batch/8（CONC_BATCH_CAP）")
        cap = plan_of([task("W-%d" % i, project="p%d" % (i % 5)) for i in range(10)],
                      {"dispatch": {"max_running": 4, "max_running_per_project": 2}})
        check("concurrency-config-cap",
              cap["concurrency"]["suggested"] == 4
              and "全局上限 4" in cap["concurrency"]["reason"],
              "suggested=%d" % cap["concurrency"]["suggested"])
        same_proj = plan_of([task("W-%d" % i, project="p1") for i in range(10)])
        check("concurrency-per-project",
              same_proj["concurrency"]["suggested"] == 4
              and "同项目上限压低" in same_proj["concurrency"]["reason"],
              "同项目 10 张压到 4")

        # 16 预计时长
        est = plan_of([
            task("W-S", tier="SIMPLE", title="小修"),
            task("W-M", tier="MEDIUM", title="常规活"),
            task("W-B", tier="MEDIUM", title="批量回归"),
        ])
        mins = {c["id"]: c["est_minutes"] for c in est["plan"]}
        check("est-minutes",
              mins == {"W-S": 15, "W-M": 45, "W-B": 75}
              and est["totals"]["est_minutes"] == 135
              and est["totals"]["est_wall_minutes"]
              == math.ceil(135 / est["concurrency"]["suggested"]),
              "mins=%s wall=%d" % (mins, est["totals"]["est_wall_minutes"]))

        # 17-19 CLI：真实文件、只读、--out、坏台账
        tasks = [task("W-Q1", title="批量回归"), task("W-Q2", tier="SIMPLE"),
                 task("W-D", "done")]
        reg_p = os.path.join(tmp, "registry.json")
        open(reg_p, "w", encoding="utf-8").write(json.dumps(
            {"tasks": tasks}, ensure_ascii=False))
        before_reg = ns.sha256_of(reg_p)
        before_cfg = ns.sha256_of(cfg_p)
        out_p = os.path.join(tmp, "night.json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ns.main(["--registry", reg_p, "--config", cfg_p,
                            "--out", out_p, "--now", NOW])
        check("cli-out-atomic",
              code == 0 and os.path.isfile(out_p)
              and open(out_p, encoding="utf-8").read() == buf.getvalue()
              and not [x for x in os.listdir(tmp) if x.startswith(".night-shift-")],
              "exit=%d out=%dB" % (code, os.path.getsize(out_p)))
        check("read-only",
              ns.sha256_of(reg_p) == before_reg
              and ns.sha256_of(cfg_p) == before_cfg,
              "registry/config sha256 跑前后一致")

        bad_reg = os.path.join(tmp, "bad.json")
        open(bad_reg, "w", encoding="utf-8").write("{nope")
        check("bad-registry-json",
              _raises(ns.load_registry, bad_reg), "NightShiftError")
        notlist = os.path.join(tmp, "notlist.json")
        open(notlist, "w", encoding="utf-8").write(json.dumps({"tasks": "x"}))
        check("bad-registry-tasks", _raises(ns.load_registry, notlist),
              "tasks 非列表")
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
            code_bad2 = ns.main(["--registry", bad_reg, "--config", cfg_p,
                                 "--now", NOW])
        err_text = err_buf.getvalue()
        check("cli-bad-registry",
              code_bad2 == 2 and "Traceback" not in err_text
              and "[ERROR]" in err_text and "合法 JSON" in err_text,
              "exit=%d" % code_bad2)
        badnow_buf = io.StringIO()
        with contextlib.redirect_stderr(badnow_buf), contextlib.redirect_stdout(io.StringIO()):
            code_bn = ns.main(["--registry", reg_p, "--config", cfg_p,
                               "--now", "not-a-time"])
        check("cli-bad-now",
              code_bn == 2 and "Traceback" not in badnow_buf.getvalue(),
              "exit=%d" % code_bn)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 20 真实台账冒烟（只读结构不变量；内容随台账演进不锁死）
    if os.path.isfile(ns.DEFAULT_REGISTRY):
        reg_live = ns.load_registry(ns.DEFAULT_REGISTRY)
        before = ns.sha256_of(ns.DEFAULT_REGISTRY)
        live = ns.build_plan(reg_live, ns.load_config(ns.DEFAULT_CONFIG)[0],
                             NOW_DT, ns.DEFAULT_REGISTRY, ns.DEFAULT_CONFIG)
        after = ns.sha256_of(ns.DEFAULT_REGISTRY)
        queued_ids = {str(t.get("id")) for t in reg_live["tasks"]
                      if isinstance(t, dict) and str(t.get("state")) == "queued"}
        check("live-registry",
              live["schema"] == ns.SCHEMA
              and set(c["id"] for c in live["plan"]) <= queued_ids
              and all(c["est_minutes"] >= ns.EST_FLOOR_MIN for c in live["plan"])
              and all(s["reason"] for s in live["skipped"])
              and live["totals"]["planned"] == len(live["plan"])
              and live["totals"]["skipped"] == len(live["skipped"])
              and live["sources"]["registry_sha256"] == before == after,
              "plan=%d skipped=%d sha=%s" % (len(live["plan"]),
                                             len(live["skipped"]), before[:12]))
    else:
        check("live-registry", True, "台账不在，跳过冒烟")

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-24s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


def _raises(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except ns.NightShiftError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
