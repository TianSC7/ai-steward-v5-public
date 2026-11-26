#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""collect_logs.py —— V4.0 全量运行日志打包（P2 起摘要含断言/销账/证据不足统计）（供后续升级研究）。只读，不改任何运行文件。

把工厂运行期间的全部日志 / 状态 / 证据 / 质检原文 / 发起人打回原话，脱敏后打成一个 zip，
并自动生成 SUMMARY.md（订单终态分布、各阶段耗时、通道用量、真实 vs 估算占比、三口径成本、
Gate1 失败项排行、Verifier 发现排行、合入前闸 / 墙钟 / 观察期熔断命中次数）。

收录范围（存在才收）：
  STEWARD_ROOT/logs/v4_trace/*.jsonl        V4 结构化全量日志（每阶段起止、判决、成本、开关快照）
  STEWARD_ROOT/logs/factory_runner/*.log    每轮 Builder / Verifier 会话原始日志
  STEWARD_ROOT/logs/gateway-*.jsonl         网关逐请求流水（含 usage_source）
  STEWARD_ROOT/logs/gateway-state.json      网关累计状态
  STEWARD_ROOT/state/{events.jsonl, PATROL_ALERT.txt, factory_queue.json, v3_budget_ledger.json,
                      MODEL_LEDGER.md, lane_ledger.jsonl, worktrees.json}
  STEWARD_ROOT/config/{steward.json, routing.json, uispec_rules.json}   （脱敏）
  STEWARD_ROOT/dashboard/v2.html            当前生产看板
  STEWARD_ROOT/verify_profiles/*.md
  BP_ROOT/reports/evidence/**               每单运行账单 / 派工单 / 质检简报 / 遥测 / 截图
  BP_ROOT/reports/transcripts/**            每轮 Builder / Verifier 完整输出
  BP_ROOT/orders/**                         契约 contract.json / baseline.json / ledger.jsonl
  BP_ROOT/golden/**                         打回黄金样本（--no-golden 可跳过）

脱敏：看板令牌、网关 client_token、apiKey、Bearer 头、board_k cookie 一律替换为 ***REDACTED***。
用法：
  python -X utf8 collect_logs.py                         # 全量
  python -X utf8 collect_logs.py --since 2026-11-22      # 只收该日起（按文件名日期或修改时间）
  python -X utf8 collect_logs.py --no-png --out D:\\pack   # 不带截图，输出到指定目录
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import sys
import zipfile

DEFAULT_STEWARD_ROOT = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
DEFAULT_BP_ROOT = r"C:\Users\Administrator\Documents\mate\dll\factory-blueprint"

TEXT_EXT = {".json", ".jsonl", ".md", ".txt", ".log", ".html", ".htm", ".css", ".js", ".py", ".csv", ".yaml", ".yml"}
DATE_IN_NAME = re.compile(r"(20\d{2})-?(\d{2})-?(\d{2})")


def build_redactor(steward_root: str):
    secrets = set()
    tok_p = os.path.join(steward_root, "config", "board_token.txt")
    try:
        t = open(tok_p, encoding="ascii").read().strip()
        if t:
            secrets.add(t)
    except OSError:
        pass
    try:
        rt = json.load(open(os.path.join(steward_root, "config", "routing.json"), encoding="utf-8"))
        ct = str((rt.get("policy") or {}).get("client_token") or "")
        if ct and ct != "***REDACTED***":
            secrets.add(ct)
    except (OSError, ValueError):
        pass
    pats = [
        (re.compile(r'("(?:apiKey|api_key|client_token|authorization|Authorization)"\s*:\s*")([^"]+)(")'), r"\1***REDACTED***\3"),
        (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***REDACTED***"),
        (re.compile(r"(board_k=)[^;\s\"']+"), r"\1***REDACTED***"),
        (re.compile(r"([?&]k=)[^&\s\"'<>]+"), r"\1***REDACTED***"),
        (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "sk-***REDACTED***"),
    ]

    def redact(text: str) -> str:
        for s in secrets:
            text = text.replace(s, "***REDACTED***")
        for p, r in pats:
            text = p.sub(r, text)
        return text

    return redact


def file_day(path: str):
    m = DATE_IN_NAME.search(os.path.basename(path))
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    try:
        return dt.date.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


def walk(root: str, pattern: str = "*"):
    if os.path.isfile(root):
        yield root
        return
    if not os.path.isdir(root):
        return
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fnmatch.fnmatch(fn, pattern):
                yield os.path.join(dp, fn)


def sources(sr: str, bp: str, golden: bool):
    j = os.path.join
    plan = [
        ("logs/v4_trace", j(sr, "logs", "v4_trace"), "*.jsonl", True),
        ("logs/factory_runner", j(sr, "logs", "factory_runner"), "*", True),
        ("logs/gateway", j(sr, "logs"), "gateway-*.jsonl", True),
        ("logs/gateway", j(sr, "logs", "gateway-state.json"), "*", False),
        ("logs/board", j(sr, "logs"), "board_access-*.jsonl", True),
        ("state", j(sr, "state", "events.jsonl"), "*", False),
        ("state", j(sr, "state", "PATROL_ALERT.txt"), "*", False),
        ("state", j(sr, "state", "factory_queue.json"), "*", False),
        ("state", j(sr, "state", "v3_budget_ledger.json"), "*", False),
        ("state", j(sr, "state", "MODEL_LEDGER.md"), "*", False),
        ("state", j(sr, "state", "lane_ledger.jsonl"), "*", False),
        ("state", j(sr, "state", "worktrees.json"), "*", False),
        ("config", j(sr, "config", "steward.json"), "*", False),
        ("config", j(sr, "config", "routing.json"), "*", False),
        ("config", j(sr, "config", "uispec_rules.json"), "*", False),
        ("dashboard", j(sr, "dashboard", "v2.html"), "*", False),
        ("verify_profiles", j(sr, "verify_profiles"), "*.md", False),
        ("blueprint/reports/evidence", j(bp, "reports", "evidence"), "*", True),
        ("blueprint/reports/transcripts", j(bp, "reports", "transcripts"), "*", True),
        ("blueprint/orders", j(bp, "orders"), "*", False),
        ("blueprint/reports/spec_probe", j(bp, "reports", "spec_probe"), "*", True),
        ("blueprint/reports/candidate_probe", j(bp, "reports", "candidate_probe"), "*.json", True),
        ("blueprint/reports/p3", j(bp, "reports", "p3"), "*", True),
        ("blueprint/inbox_rejected", j(sr, "state", "factory_inbox", "_rejected"), "*", False),
        ("config", j(sr, "config", "assertion_templates.json"), "*", False),
    ]
    if golden:
        plan.append(("blueprint/golden", j(bp, "golden"), "*", True))
    for arc_dir, root, pat, dated in plan:
        base = root if os.path.isdir(root) else os.path.dirname(root)
        for p in walk(root, pat):
            if os.path.basename(p) == "board_token.txt":
                continue
            rel = os.path.relpath(p, base).replace("\\", "/")
            yield arc_dir + "/" + rel, p, dated


def summarize_board(rows: list) -> list[str]:
    """V4.0-P4：看板访问审计摘要（登录成败、锁定、拒绝原因、写操作、旧口令使用）。"""
    if not rows:
        return []
    ev = collections.Counter(r.get("event") for r in rows)
    deny = collections.Counter(f"{r.get('code')} {r.get('reason')}" for r in rows if r.get("event") == "denied")
    srcs = collections.Counter(r.get("src") for r in rows if r.get("event") in ("login_fail", "login_locked", "denied"))
    L = ["## 看板访问审计（V4.0-P4）", "",
         f"- 登录成功 {ev.get('login_ok', 0)} · 失败 {ev.get('login_fail', 0)} · 锁定 {ev.get('login_locked', 0)} · 退出 {ev.get('logout', 0)}",
         f"- 写操作 {ev.get('write', 0)}（打回/归档）· 旧口令（?k= / 旧 cookie）使用 {ev.get('legacy_token_use', 0)} · 口令轮换 {ev.get('token_rotated', 0)}",
         f"- 页面访问 {ev.get('page_view', 0)} · 服务启动 {ev.get('start', 0)} · 公网监听告警 {ev.get('public_bind_warning', 0)}", ""]
    if deny:
        L += ["拒绝原因排行："] + [f"- {n} × {k}" for k, n in deny.most_common(10)] + [""]
    if srcs:
        L += ["可疑来源（失败/拒绝次数）："] + [f"- {n} × {k}" for k, n in srcs.most_common(8)] + [""]
    w = [r for r in rows if r.get("event") == "write"]
    if w:
        L += ["写操作明细："] + [f"- {r.get('ts')} {r.get('action')} {r.get('order', '')} rc={r.get('rc', '')} 来源 {r.get('src')}：{str(r.get('reason') or '')[:60]}" for r in w[-20:]] + [""]
    return L


def summarize(trace_rows: list, gw_rows: list, board_rows: list | None = None) -> str:
    L = ["# 运行日志摘要（collect_logs.py 自动生成）", ""]
    ev = collections.Counter(r.get("event") for r in trace_rows)
    runs = [r for r in trace_rows if r.get("event") == "run_end"]
    L += [f"- trace 事件 {len(trace_rows)} 条，订单运行 {len(runs)} 次", ""]
    if runs:
        L += ["## 订单终态", "", "| 订单 | run_id | 终态 | 轮次 | 端到端 s | 现金 ¥ | 账面 ¥ | 估算 ¥ | 各轮判决 |", "|---|---|---|---|---|---|---|---|---|"]
        for r in runs:
            cc = ((r.get("billing") or {}).get("tokens_and_cost") or {}).get("cost_columns") or {}
            L.append(f"| {r.get('order_id')} | {r.get('run_id')} | {r.get('final_status')} | {r.get('attempts_used')} | "
                     f"{r.get('e2e_total_sec')} | {cc.get('cash_cny', '')} | {cc.get('book_cny', '')} | {cc.get('est_cny', '')} | "
                     f"{' / '.join(str(v) for _, v in (r.get('attempt_verdicts') or []))} |")
        st = collections.Counter(r.get("final_status") for r in runs)
        L += ["", "终态分布：" + "，".join(f"{k} {v}" for k, v in st.most_common()), ""]
    stages = [r for r in trace_rows if r.get("event") == "stage_end"]
    if stages:
        agg = collections.defaultdict(lambda: [0, 0.0, 0])
        for r in stages:
            res = r.get("result") or {}
            k = (r.get("role"), res.get("lane_selected") or "gateway")
            agg[k][0] += 1
            agg[k][1] += float(res.get("duration_sec") or 0)
            agg[k][2] += 1 if (res.get("timed_out") or res.get("stall_timeout") or (res.get("exit_code") not in (0, None))) else 0
        L += ["## 阶段耗时与异常", "", "| 角色 | 通道 | 次数 | 平均耗时 s | 超时/停滞/非0退出 |", "|---|---|---|---|---|"]
        for (role, lane), (n, s, bad) in sorted(agg.items(), key=lambda x: str(x[0])):
            L.append(f"| {role} | {lane} | {n} | {round(s / n, 1)} | {bad} |")
        L.append("")
    g1 = collections.Counter()
    for r in trace_rows:
        if r.get("event") == "gate1_result":
            for f in r.get("failures") or []:
                g1[re.sub(r"[\d.]+", "#", str(f))[:80]] += 1
    if g1:
        L += ["## Gate1 失败项排行", ""] + [f"- {n} × {k}" for k, n in g1.most_common(15)] + [""]
    vf = collections.Counter()
    for r in trace_rows:
        if r.get("event") == "gate2_verdict":
            for f in r.get("findings") or []:
                if isinstance(f, dict):
                    vf[f"{f.get('severity')}/{f.get('status')} {str(f.get('question') or '')[:30]}"] += 1
    if vf:
        L += ["## Verifier 发现分布（严重度/状态 问题）", ""] + [f"- {n} × {k}" for k, n in vf.most_common(20)] + [""]
    a_stat = collections.Counter()
    a_bad = collections.Counter()
    for r in trace_rows:
        if r.get("event") == "gate1_result":
            for a in r.get("assertions") or []:
                if isinstance(a, dict):
                    a_stat[a.get("status")] += 1
                    if a.get("status") in ("FAIL", "UNVERIFIED"):
                        a_bad[f"{a.get('status')} · {r.get('order_id')}/{a.get('id')} {str(a.get('desc') or a.get('selector') or '')[:40]}"] += 1
    if a_stat:
        L += ["## 规格断言（V4.0-P2）", "",
              "断言判定次数：" + "，".join(f"{k} {v}" for k, v in a_stat.most_common()), ""]
        L += [f"- {n} × {k}" for k, n in a_bad.most_common(20)] + [""]
    co = [r for r in trace_rows if r.get("event") == "closeout"]
    if co:
        flips = sum(1 for r in co if r.get("verdict_before") != r.get("verdict_after"))
        L += ["## 销账（V4.0-P2）", "", f"- 销账核对 {len(co)} 次，因上一轮缺陷未关而改判 {flips} 次", ""]
    L += ["## V4 闸门命中", "",
          f"- 合入前闸拦截 merge_gate_block：{ev.get('merge_gate_block', 0)}",
          f"- 墙钟停止 wallclock_stop：{ev.get('wallclock_stop', 0)}",
          f"- 观察期熔断（只告警）fuse_dry_run：{ev.get('fuse_dry_run', 0)}",
          f"- 契约更新跳过 contract_update_skipped：{ev.get('contract_update_skipped', 0)}",
          f"- 内核崩溃 kernel_crash：{ev.get('kernel_crash', 0)}",
          f"- 发起人打回 initiator_reject：{ev.get('initiator_reject', 0)}",
          f"- 规格拒收（缺/错断言）spec_rejected：{ev.get('spec_rejected', 0)}",
          f"- 断言补证 assertion_reprobe：{ev.get('assertion_reprobe', 0)}",
          f"- 证据不足转人工 needs_evidence：{ev.get('needs_evidence', 0)}",
          f"- 基线写入 baseline_write：{ev.get('baseline_write', 0)}",
          f"- 派单前试跑 spec_probe：{ev.get('spec_probe', 0)}",
          f"- 候选直验 candidate_probe（P3）：{ev.get('candidate_probe', 0)}",
          f"- 受控重写授权 rewrite_authorization（P3）：{ev.get('rewrite_authorization', 0)}",
          f"- 像素单墙钟延长 wallclock_extend（P3）：{ev.get('wallclock_extend', 0)}",
          f"- 暴露面验收 exposure_check（P4）：{ev.get('exposure_check', 0)}", ""]
    cps = [r for r in trace_rows if r.get("event") == "candidate_probe"]
    if cps:
        L += ["## 候选直验（P3）", "", "| 时间 | 候选 | 噪声底 | 通过 | 最大像素差% | 已提交 |", "|---|---|---|---|---|---|"]
        L += [f"| {r.get('ts')} | {r.get('candidate_sha16')} | {r.get('noise_mode')} | {r.get('passed')} | {r.get('visual_max_pct')} | {r.get('applied')} |" for r in cps]
        L.append("")
    rej = [r for r in trace_rows if r.get("event") == "initiator_reject"]
    if rej:
        L += ["## 发起人打回原话", ""] + [f"- {r.get('ts')} {r.get('order_id')} → {r.get('new_order_id')}：{r.get('reason_full')}" for r in rej] + [""]
    if gw_rows:
        ok = [r for r in gw_rows if r.get("event") == "ok"]
        rep = [r for r in ok if r.get("usage_source", "legacy") == "reported"]
        est = [r for r in ok if r.get("usage_source") == "estimated"]
        legacy = [r for r in ok if "usage_source" not in r]
        lanes = collections.defaultdict(lambda: [0, 0, 0, 0.0, 0])
        for r in ok:
            a = lanes[r.get("lane")]
            a[0] += 1
            a[1] += int(r.get("tokens_in") or 0)
            a[2] += int(r.get("tokens_out") or 0)
            a[3] += float(r.get("cost_cny") or 0)
            a[4] += 1 if r.get("usage_source") == "estimated" else 0
        fails = collections.Counter(r.get("lane") for r in gw_rows if r.get("event") == "fail")
        L += ["## 网关用量", "",
              f"- 成功请求 {len(ok)}：真实上报 {len(rep)}，估算 {len(est)}，V3 旧口径(无标记) {len(legacy)}"
              + (f"；真实占比 {round(100 * len(rep) / max(1, len(rep) + len(est)), 1)}%（V4 验收要求 ≥95%）" if (rep or est) else ""),
              f"- 失败请求 {sum(fails.values())}", "",
              "| lane | 成功 | 输入 tok | 输出 tok | 成本 ¥ | 估算次数 | 失败 |", "|---|---|---|---|---|---|---|"]
        for ln, (n, ti, to, c, e) in sorted(lanes.items(), key=lambda x: -x[1][0]):
            L.append(f"| {ln} | {n} | {ti:,} | {to:,} | {round(c, 4)} | {e} | {fails.get(ln, 0)} |")
        caps = [r for r in gw_rows if r.get("event") == "capability"]
        if caps:
            L += ["", "不支持 include_usage 的 lane：" + "，".join(sorted({str(r.get('lane')) for r in caps}))]
        L.append("")
    L += summarize_board(board_rows or [])
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打包工厂全量运行日志（脱敏）")
    ap.add_argument("--steward-root", default=DEFAULT_STEWARD_ROOT)
    ap.add_argument("--bp-root", default=DEFAULT_BP_ROOT)
    ap.add_argument("--since", default="", help="YYYY-MM-DD，只收该日起的带日期文件")
    ap.add_argument("--out", default="", help="输出目录（默认 STEWARD_ROOT/reports/log_packs）")
    ap.add_argument("--no-png", action="store_true", help="不收截图等图片")
    ap.add_argument("--no-golden", action="store_true", help="不收打回黄金样本")
    ap.add_argument("--max-file-mb", type=float, default=50.0)
    args = ap.parse_args(argv)

    since = dt.date.fromisoformat(args.since) if args.since else None
    out_dir = args.out or os.path.join(args.steward_root, "reports", "log_packs")
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(out_dir, f"factory_logs_{stamp}.zip")
    redact = build_redactor(args.steward_root)

    manifest, skipped = [], []
    trace_rows, gw_rows, board_rows = [], [], []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for arc, path, dated in sources(args.steward_root, args.bp_root, not args.no_golden):
            ext = os.path.splitext(path)[1].lower()
            if args.no_png and ext in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            if dated and since:
                d = file_day(path)
                if d and d < since:
                    continue
            try:
                size = os.path.getsize(path)
                if size > args.max_file_mb * 1024 * 1024:
                    skipped.append({"file": arc, "reason": f"超过 {args.max_file_mb}MB", "bytes": size})
                    continue
                raw = open(path, "rb").read()
            except OSError as e:
                skipped.append({"file": arc, "reason": str(e)})
                continue
            if ext in TEXT_EXT:
                txt = redact(raw.decode("utf-8", "replace"))
                data = txt.encode("utf-8")
                if arc.startswith("logs/v4_trace/"):
                    trace_rows += [json.loads(x) for x in txt.splitlines() if x.strip().startswith("{")]
                elif arc.startswith("logs/board/board_access-"):
                    for x in txt.splitlines():
                        try:
                            board_rows.append(json.loads(x))
                        except ValueError:
                            pass
                elif arc.startswith("logs/gateway/gateway-") and arc.endswith(".jsonl"):
                    for x in txt.splitlines():
                        try:
                            gw_rows.append(json.loads(x))
                        except ValueError:
                            pass
            else:
                data = raw
            z.writestr(arc, data)
            manifest.append({"file": arc, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        summary = summarize(trace_rows, gw_rows, board_rows)
        z.writestr("SUMMARY.md", summary)
        z.writestr("MANIFEST.json", json.dumps({
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "steward_root": args.steward_root, "bp_root": args.bp_root, "since": args.since,
            "files": manifest, "skipped": skipped, "python": sys.version.split()[0],
        }, ensure_ascii=False, indent=1))
    total = sum(m["bytes"] for m in manifest)
    print(f"[collect_logs] {len(manifest)} 个文件 / 原始 {total / 1024 / 1024:.1f} MB → {zip_path} "
          f"({os.path.getsize(zip_path) / 1024 / 1024:.1f} MB)；跳过 {len(skipped)} 个")
    print(f"[collect_logs] trace {len(trace_rows)} 行，网关流水 {len(gw_rows)} 行；摘要见包内 SUMMARY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
