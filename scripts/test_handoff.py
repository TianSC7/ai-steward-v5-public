# -*- coding: utf-8 -*-
"""交接快照边界用例：空台账/在办/坏时间戳/五段齐全/卡号可点/只读/成本。

覆盖（验收要 >=7 例，这里 18 例锁死口径）：
  1  空台账 -> 五段齐全、各段写明"无"、摘要全零、CLI 退出 0 且落盘
  2  有在办 -> 在办表含卡号/会话/模型/卡龄，且最老的排前面
  3  卡龄格式：<1m / 45m / 2h13m / 3d4h（now − created_at）
  4  坏时间戳 -> 卡龄标"未知"、卡不丢、进未决"坏时间戳"、不抛栈
  5  今日完成筛选：只数完结时刻本地日期 == today；昨天/脏时间不混入
  6  阻塞表：blocked/failed 落 3.1；无处置说明的阻塞进未决
  7  静默卡：last_check 超阈值 -> 标静默 + 未决"静默超阈值" + 建议先查
  8  证据四态：有标记 / 缺标记 / 文件缺失 / 含 EXIT=FAIL 各归各位
  9  卡号可点：渲染出的每个 markdown 链接都能相对交接单目录解析到真实文件
  10 明日建议：可派卡给派单建议；队列空给 queue-empty 建议；超预算给降耗建议
  11 模型与成本：by_model 计数正确；成本只算 event=ok，坏行/脏值/非 ok 跳过
  12 worktrees：已合并但卡仍在办 -> 未决"工作区已合并"；分支进在办表
  13 只读：CLI 跑前后 registry/config/worktrees 的 sha256 不变、mtime 不变
  14 坏台账：非 JSON / tasks 非列表 -> HandoffError；CLI 退出 2、无堆栈、不落半截文件
  15 坏配置/坏工作区账：降级 + warnings 非空，仍退出 0
  16 CLI --out 原子写：内容与 render 一致、无 .handoff-* 残留
  17 CLI --json：stdout 是合法 JSON 且含 schema/tool/summary；无 --out 时不落盘
  18 真实台账冒烟（只读）：五段齐全、真实 registry sha256 不变、在办链接可解析

用法：python -X utf8 test_handoff.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import handoff as ho  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
NOW = "2026-11-18T19:30:00+08:00"
NOW_DT = ho.parse_now(NOW)
CFG = {"stale_sec": 1800,
       "dispatch": {"stale_sec": 1800, "daily_budget_cny": 0}}

#: markdown 链接目标（用于"卡号可点"全量校验）
LINK_RE = re.compile(r"\]\(([^)]+)\)")


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def task(tid: str, state: str = "dispatched", **kw) -> dict:
    base = {
        "id": tid,
        "title": "任务 " + tid,
        "project": "ai-steward",
        "state": state,
        "tier": "MEDIUM",
        "model": "new-provider-6/deepseek-v4.1-flash",
        "sessionId": "sess_" + tid.lower().replace("-", ""),
        "workspace": "C:/Users/Administrator/Documents/mate/plugins/ai-steward",
        "created_at": "2026-11-18T17:17:00+08:00",   # 卡龄 2h13m
        "last_check": "2026-11-18T19:28:00+08:00",   # 2 分钟前，未静默
        "attempts": 1,
        "max_attempts": 3,
    }
    base.update(kw)
    return base


def build(tasks: list[dict], tmp: str, config: dict | None = None,
          worktrees: dict | None = None, logs_dir: str | None = None,
          registry_path: str | None = None) -> dict:
    """在 tmp 里落一份台账并生成快照（report_dir=tmp，链接可本地解析）。"""
    reg = registry_path or os.path.join(tmp, "registry.json")
    if registry_path is None:
        _write(reg, json.dumps({"tasks": tasks}, ensure_ascii=False))
    return ho.build_snapshot(
        ho.load_registry(reg), config if config is not None else CFG,
        worktrees if worktrees is not None else {}, NOW_DT,
        registry_path=reg, report_dir=tmp,
        config_path=os.path.join(tmp, "steward.json"),
        worktrees_path=os.path.join(tmp, "worktrees.json"),
        logs_dir=logs_dir)


def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def sha(path: str) -> str:
    return ho.sha256_of(path)


def ids(rows: list[dict]) -> list[str]:
    return [r["id"] for r in rows]


def pending_kinds(snap: dict) -> set[str]:
    return {p["kind"] for p in snap["pending"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_handoff")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_handoff")

    tmp = tempfile.mkdtemp(prefix="steward-handoff-")
    try:
        cfg_path = _write(os.path.join(tmp, "steward.json"), json.dumps(CFG))
        wt_path = _write(os.path.join(tmp, "worktrees.json"),
                         json.dumps({"version": 1, "worktrees": {}}))

        # 1 空台账：五段齐全 + 各段"无" + CLI 退出 0
        reg0 = _write(os.path.join(tmp, "empty.json"), json.dumps({"tasks": []}))
        snap0 = build([], tmp, registry_path=reg0)
        page0 = ho.render(snap0)
        out0 = os.path.join(tmp, "HANDOFF_EMPTY.md")
        err0 = io.StringIO()
        with contextlib.redirect_stderr(err0):
            code0 = ho.main(["--registry", reg0, "--config", cfg_path,
                             "--worktrees", wt_path, "--out", out0, "--now", NOW])
        check("empty-five-sections",
              code0 == 0 and os.path.getsize(out0) > 500
              and all(h in page0 for h in ("## 1. 在办卡", "## 2. 今日完成",
                                           "## 3. 阻塞与未决", "## 4. 明日建议",
                                           "## 5. 模型与成本口径"))
              and page0.count("无在办卡") == 1 and "今日无完结卡" in page0
              and "无阻塞卡" in page0 and "无未决项" in page0,
              "exit=%d bytes=%d" % (code0, os.path.getsize(out0)))
        check("empty-summary-zero",
              snap0["summary"]["active"] == 0 and snap0["summary"]["done_today"] == 0
              and snap0["summary"]["blocked"] == 0 and snap0["summary"]["queued"] == 0
              and "| 0 | 0 | 0 | 0 | 0 |" in page0,
              "summary=%s" % snap0["summary"])

        # 2 有在办：卡号/会话/模型/卡龄 + 最老在前
        active_tasks = [
            task("W-NEW", created_at="2026-11-18T19:00:00+08:00"),
            task("W-OLD", created_at="2026-11-18T16:00:00+08:00"),
            task("W-RUN", state="running", created_at="2026-11-18T18:00:00+08:00"),
        ]
        ev_pass = _write(os.path.join(tmp, "ev-pass.md"),
                         "命令...\nEXIT=0\n全部通过\n")
        snap1 = build(active_tasks, tmp)
        page1 = ho.render(snap1)
        has_sess = all(("sess_" + i.lower().replace("-", "")) in page1
                       for i in ("W-NEW", "W-OLD", "W-RUN"))
        check("active-columns",
              ids(snap1["active"]) == ["W-OLD", "W-RUN", "W-NEW"]
              and has_sess and "new-provider-6/deepseek-v4.1-flash" in page1
              and "卡龄" in page1,
              "order=%s" % ids(snap1["active"]))

        # 3 卡龄格式
        check("age-format",
              ho.human_age(NOW_DT - __import__("datetime").timedelta(seconds=30),
                           NOW_DT) == "<1m"
              and ho.human_age(NOW_DT - __import__("datetime").timedelta(minutes=45),
                               NOW_DT) == "45m"
              and ho.human_age(NOW_DT - __import__("datetime").timedelta(
                  hours=2, minutes=13), NOW_DT) == "2h13m"
              and ho.human_age(NOW_DT - __import__("datetime").timedelta(
                  days=3, hours=4), NOW_DT) == "3d4h"
              and snap1["active"][0]["age"] == "3h30m",
              "oldest=%s" % snap1["active"][0]["age"])

        # 4 坏时间戳：不丢卡、卡龄未知、进未决、不抛栈
        bad_tasks = [task("W-BAD", created_at="脏时间"),
                     task("W-NONE", created_at=None)]
        try:
            snap_bad = build(bad_tasks, tmp)
            page_bad = ho.render(snap_bad)
            raised = False
        except Exception as e:  # noqa: BLE001  用例就是要证明不抛栈
            snap_bad, page_bad, raised = {}, "", True
            print("[detail] 坏时间戳抛栈: %r" % e)
        check("bad-timestamp-tolerant",
              not raised and ids(snap_bad.get("active", [])) == ["W-BAD", "W-NONE"]
              and all(x["age"] == "未知" and x["age_seconds"] is None
                      for x in snap_bad.get("active", []))
              and "坏时间戳" in pending_kinds(snap_bad)
              and page_bad.count("| 未知 |") >= 2,
              "raised=%s pending=%s" % (raised, sorted(pending_kinds(snap_bad))))

        # 5 今日完成筛选
        done_tasks = [
            task("W-T1", state="done", updated_at="2026-11-18T09:00:00+08:00"),
            task("W-T2", state="done", updated_at="2026-11-17T23:00:00+08:00"),
            task("W-T3", state="abandoned", updated_at="2026-11-18T08:00:00+08:00"),
            task("W-T4", state="done", updated_at="脏时间",
                 evidence_verified_at="2026-11-18T10:00:00+08:00"),
            task("W-T5", state="done", updated_at=None, evidence_verified_at=None,
                 created_at="2026-11-17T10:00:00+08:00"),
        ]
        snap_done = build(done_tasks, tmp)
        page_done = ho.render(snap_done)
        sec2 = page_done.split("## 2. 今日完成")[1].split("## 3.")[0]
        check("done-today-filter",
              ids(snap_done["done_today"]) == ["W-T4", "W-T1", "W-T3"]
              and "W-T2" not in sec2 and "W-T5" not in sec2,
              "done_today=%s" % ids(snap_done["done_today"]))

        # 6 阻塞：3.1 收录 + 无处置说明进未决
        block_tasks = [
            task("W-B1", state="blocked", next_action="等用户开 CAD"),
            task("W-B2", state="failed"),
        ]
        snap_block = build(block_tasks, tmp)
        page_block = ho.render(snap_block)
        sec31 = page_block.split("### 3.1 阻塞")[1].split("### 3.2")[0]
        check("blocked-section",
              ids(snap_block["blocked"]) == ["W-B1", "W-B2"]
              and "W-B1" in sec31 and "等用户开 CAD" in sec31
              and "W-B2" in sec31 and "无处置说明" in page_block
              and "阻塞无处置说明" in pending_kinds(snap_block),
              "pending=%s" % sorted(pending_kinds(snap_block)))

        # 7 静默卡：超阈值 -> stale + 未决 + 建议先查
        stale = build([task("W-SILENT", last_check="2026-11-18T18:00:00+08:00")], tmp)
        fresh = build([task("W-ALIVE", last_check="2026-11-18T19:29:00+08:00")], tmp)
        check("stale-active",
              stale["active"][0]["stale"] is True
              and "静默 90m" in stale["active"][0]["heartbeat"]
              and fresh["active"][0]["stale"] is False
              and "静默超阈值" in pending_kinds(stale)
              and any(s["text"].startswith("先查 W-SILENT")
                      for s in stale["suggestions"]),
              "heartbeat=%s" % stale["active"][0]["heartbeat"])

        # 8 证据四态
        ev_plain = _write(os.path.join(tmp, "ev-plain.md"), "跑完了，没写标记\n")
        ev_fail = _write(os.path.join(tmp, "ev-fail.md"), "[PASS] 3 例\n[FAIL] 1 例\n")
        ev_tasks = [
            task("W-E1", state="done", updated_at=NOW, evidence_path=ev_pass),
            task("W-E2", state="done", updated_at=NOW, evidence_path=ev_plain),
            task("W-E3", state="done", updated_at=NOW,
                 evidence_path=os.path.join(tmp, "not-there.md")),
            task("W-E4", state="done", updated_at=NOW, evidence_path=ev_fail),
            task("W-E5", state="done", updated_at=NOW),   # 未登记
        ]
        snap_ev = build(ev_tasks, tmp)
        by_id = {x["id"]: x for x in snap_ev["done_today"]}
        page_ev = ho.render(snap_ev)
        check("evidence-four-states",
              by_id["W-E1"]["marker_note"] == "有标记"
              and by_id["W-E2"]["marker_note"] == "缺 EXIT=/[PASS]"
              and by_id["W-E3"]["evidence_status"] == "missing"
              and by_id["W-E4"]["has_fail_marker"] is True
              and {"证据缺标记", "证据缺失", "证据判失败"} <= pending_kinds(snap_ev)
              and "有标记" in page_ev and "缺 EXIT=/[PASS]" in page_ev
              and "含 EXIT=FAIL/[FAIL]" in page_ev,
              "pending=%s" % sorted(pending_kinds(snap_ev)))

        # 9 卡号可点：每个链接目标都能相对交接单目录解析
        bad_links = []
        for target in LINK_RE.findall(page_ev):
            local = target if os.path.isabs(target) else os.path.join(tmp, target)
            if not os.path.isfile(local):
                bad_links.append(target)
        check("links-resolvable",
              not bad_links and page_ev.count("](ev-") >= 3
              and page_ev.count("registry.json)") >= 1,
              "links=%d bad=%s" % (len(LINK_RE.findall(page_ev)), bad_links[:2]))

        # 10 明日建议：可派 / queue-empty / 超预算
        suggestable = build([task("W-Q1", state="queued"),
                             task("W-Q2", state="queued")], tmp)
        texts = [s["text"] for s in suggestable["suggestions"]]
        budget_cfg = {"dispatch": {"stale_sec": 1800, "daily_budget_cny": 0.001}}
        logs_dir = os.path.join(tmp, "logs-budget")
        os.makedirs(logs_dir, exist_ok=True)
        _write(os.path.join(logs_dir, "gateway-%s.jsonl" % NOW_DT.strftime("%Y%m%d")),
               json.dumps({"event": "ok", "cost_cny": 0.003}) + "\n")
        over = build([], tmp, config=budget_cfg, logs_dir=logs_dir)
        check("suggestions",
              any(t.startswith("可派 W-Q1") for t in texts)
              and "队列空（queued=0）" in ho.render(build([], tmp))
              and any("超预算" in s["text"] for s in over["suggestions"])
              and len(texts) == len(set(texts)),
              "n=%d first=%s" % (len(texts), texts[:1]))

        # 11 模型与成本：只算 ok 行
        rich_logs = os.path.join(tmp, "logs-rich")
        os.makedirs(rich_logs, exist_ok=True)
        _write(os.path.join(rich_logs, "gateway-%s.jsonl" % NOW_DT.strftime("%Y%m%d")),
               "\n".join([
                   json.dumps({"event": "ok", "cost_cny": 0.001}),
                   json.dumps({"event": "ok", "cost_cny": 0.002}),
                   json.dumps({"event": "error", "cost_cny": 99.0}),
                   "not json at all",
                   json.dumps({"event": "ok", "cost_cny": "oops"}),
               ]) + "\n")
        mixed = build([task("W-M1"), task("W-M2", state="running"),
                       task("W-M3", state="done", updated_at=NOW,
                            model="new-provider-10/step-5-preview")],
                      tmp, logs_dir=rich_logs)
        model_map = {r["model"]: r for r in mixed["models"]["by_model"]}
        check("models-and-cost",
              abs(mixed["models"]["cost"]["cny"] - 0.003) < 1e-9
              and model_map["new-provider-6/deepseek-v4.1-flash"]["active"] == 2
              and model_map["new-provider-10/step-5-preview"]["done_today"] == 1
              and "¥0.0030" in ho.render(mixed),
              "cost=%.4f models=%d" % (mixed["models"]["cost"]["cny"], len(model_map)))

        # 12 worktrees：已合并但仍在办
        wts = {"W-WT": {"branch": "wt/W-WT", "path": "C:/Users/x/wt/W-WT",
                        "merged_at": "2026-11-18T19:00:00+08:00",
                        "removed_at": ""}}
        snap_wt = build([task("W-WT")], tmp, worktrees=wts)
        page_wt = ho.render(snap_wt)
        check("worktree-merged-pending",
              snap_wt["active"][0]["worktree"]["branch"] == "wt/W-WT"
              and "wt/W-WT" in page_wt
              and "工作区已合并" in pending_kinds(snap_wt)
              and "已合并" in page_wt,
              "worktree=%s" % snap_wt["active"][0]["worktree"]["branch"])

        # 13 只读：sha/mtime 前后不变
        ro_dir = tempfile.mkdtemp(prefix="steward-handoff-ro-", dir=tmp)
        ro_reg = _write(os.path.join(ro_dir, "registry.json"),
                        json.dumps({"tasks": [task("W-RO")]}, ensure_ascii=False))
        ro_cfg = _write(os.path.join(ro_dir, "steward.json"), json.dumps(CFG))
        ro_wt = _write(os.path.join(ro_dir, "worktrees.json"),
                       json.dumps({"version": 1, "worktrees": {}}))
        before = [sha(ro_reg), sha(ro_cfg), sha(ro_wt)]
        before_mtime = [os.path.getmtime(p) for p in (ro_reg, ro_cfg, ro_wt)]
        with contextlib.redirect_stderr(io.StringIO()):
            code_ro = ho.main(["--registry", ro_reg, "--config", ro_cfg,
                               "--worktrees", ro_wt, "--now", NOW,
                               "--out", os.path.join(ro_dir, "H.md")])
        after = [sha(ro_reg), sha(ro_cfg), sha(ro_wt)]
        check("readonly",
              code_ro == 0 and before == after
              and before_mtime == [os.path.getmtime(p) for p in (ro_reg, ro_cfg, ro_wt)],
              "sha=%s" % (before[0][:12]))

        # 14 坏台账：HandoffError + CLI 退出 2 无堆栈
        bad_json = _write(os.path.join(tmp, "bad.json"), "{not json")
        not_list = _write(os.path.join(tmp, "notlist.json"),
                          json.dumps({"tasks": "nope"}))
        try:
            ho.load_registry(bad_json)
            raised_bad = False
        except ho.HandoffError:
            raised_bad = True
        try:
            ho.load_registry(not_list)
            raised_list = False
        except ho.HandoffError:
            raised_list = True
        bad_out = os.path.join(tmp, "bad-handoff.md")
        err_bad = io.StringIO()
        with contextlib.redirect_stderr(err_bad):
            code_bad = ho.main(["--registry", bad_json, "--config", cfg_path,
                                "--worktrees", wt_path, "--out", bad_out, "--now", NOW])
        stderr_bad = err_bad.getvalue()
        check("bad-registry",
              raised_bad and raised_list and code_bad == 2
              and "[ERROR]" in stderr_bad and "Traceback" not in stderr_bad
              and "台账不是合法 JSON" in stderr_bad
              and not os.path.exists(bad_out),
              "exit=%d stderr=%s" % (code_bad, stderr_bad.strip()[:50]))

        # 15 坏配置/坏工作区账：降级 + warnings，仍 0
        bad_cfg = _write(os.path.join(tmp, "badcfg.json"), "{oops")
        bad_wt = _write(os.path.join(tmp, "badwt.json"), "[]")
        snap_deg = build([task("W-DEG")], tmp)
        page_deg = ho.render(snap_deg)
        err_deg = io.StringIO()
        with contextlib.redirect_stderr(err_deg):
            code_deg = ho.main(["--registry", ro_reg, "--config", bad_cfg,
                                "--worktrees", bad_wt, "--now", NOW,
                                "--out", os.path.join(tmp, "deg.md")])
        cfg_bad, warn_bad = ho.load_config(bad_cfg)
        wt_bad, warn_wt = ho.load_worktrees(bad_wt)
        check("bad-config-degraded",
              code_deg == 0 and cfg_bad == {} and bool(warn_bad) and wt_bad == {}
              and bool(warn_wt) and "W-DEG" in page_deg
              and snap_deg["thresholds"]["stale_sec"] == 1800,
              "warn=%s/%s" % (warn_bad[:24], warn_wt[:24]))

        # 16 CLI --out 原子写
        out16 = os.path.join(tmp, "atomic.md")
        with contextlib.redirect_stderr(io.StringIO()):
            code16 = ho.main(["--registry", ro_reg, "--config", ro_cfg,
                              "--worktrees", ro_wt, "--out", out16, "--now", NOW])
        snap16 = ho.build_snapshot(ho.load_registry(ro_reg), CFG, {}, NOW_DT,
                                   registry_path=ro_reg,
                                   report_dir=os.path.dirname(out16),
                                   config_path=ro_cfg, worktrees_path=ro_wt)
        leftovers = [x for x in os.listdir(tmp) if x.startswith(".handoff-")]
        check("cli-out-atomic",
              code16 == 0 and open(out16, encoding="utf-8").read() == ho.render(snap16)
              and not leftovers,
              "exit=%d leftovers=%d" % (code16, len(leftovers)))

        # 17 CLI --json：stdout 合法 JSON；无 --out 时不落盘
        json_dir = tempfile.mkdtemp(prefix="steward-handoff-json-", dir=tmp)
        old_default = ho.DEFAULT_OUT_DIR
        ho.DEFAULT_OUT_DIR = json_dir
        out_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(out_buf), \
                    contextlib.redirect_stderr(io.StringIO()):
                code17 = ho.main(["--registry", ro_reg, "--config", ro_cfg,
                                  "--worktrees", ro_wt, "--now", NOW, "--json"])
        finally:
            ho.DEFAULT_OUT_DIR = old_default
        try:
            payload17 = json.loads(out_buf.getvalue())
        except ValueError:
            payload17 = {}
        check("cli-json",
              code17 == 0 and payload17.get("schema") == 1
              and payload17.get("tool") == "handoff"
              and set(payload17.get("summary", {})) >= {"active", "done_today",
                                                        "blocked", "queued",
                                                        "pending", "cost_cny"}
              and payload17.get("sources", {}).get("registry_sha256") == sha(ro_reg)
              and os.listdir(json_dir) == [],
              "exit=%d keys=%d" % (code17, len(payload17)))

        # 18 真实台账冒烟（只读）
        real_reg = os.path.join(os.path.dirname(HERE), "state", "registry.json")
        if os.path.isfile(real_reg):
            before_real = sha(real_reg)
            real_snap = ho.build_snapshot(
                ho.load_registry(real_reg), CFG, {}, NOW_DT,
                registry_path=real_reg, report_dir=tmp)
            real_page = ho.render(real_snap)
            after_real = sha(real_reg)
            real_bad = [t for t in LINK_RE.findall(real_page)
                        if not os.path.isfile(t if os.path.isabs(t)
                                              else os.path.join(tmp, t))]
            check("live-registry",
                  before_real == after_real
                  and all(h in real_page for h in ("## 1. 在办卡", "## 2. 今日完成",
                                                   "## 3. 阻塞与未决", "## 4. 明日建议",
                                                   "## 5. 模型与成本口径"))
                  and real_snap["summary"]["active"] >= 0
                  and not real_bad,
                  "active=%d done=%d links_bad=%d" % (
                      real_snap["summary"]["active"],
                      real_snap["summary"]["done_today"], len(real_bad)))
        else:
            check("live-registry", True, "台账不在，跳过冒烟")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [x for x in RESULTS if x[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-24s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    raise SystemExit(main())
