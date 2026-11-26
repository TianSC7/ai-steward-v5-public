#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""model_ledger.py — 模型使用台账（按时间段/任务标题/模型统计）+ 车道合规检查

用法:
  python -X utf8 scripts/model_ledger.py                 # 更新今天的台账段并打印摘要
  python -X utf8 scripts/model_ledger.py --date 2026-11-19
  python -X utf8 scripts/model_ledger.py --json          # 机器可读（巡检用）
  python -X utf8 scripts/model_ledger.py --check         # 只做合规检查，违规 exit 2

口径（用户 2026-11-19 06:43 令，D-029）:
  夜间 21:00–09:00 只允许三档：① account:bigmodel-start-plan/GLM-5.3-Flash
  ② new-provider-10/step-5-preview（或 step-3.7-flash）③ new-provider-5/nex-n2.6；
  白天同三档；DeepSeek Flash 仅经理（M-*）可用；其余模型道一律视为违规。
数据源: ~/.zcode/cli/db/db.sqlite 的 model_usage + session.title（只读）。
输出: state/MODEL_LEDGER.md（按天分节，重建当天段，历史天保留）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
LEDGER = os.path.join(STATE, "MODEL_LEDGER.md")
DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")

GLM = ("account:bigmodel-start-plan", "GLM-5.3-Flash")
STEP = {("new-provider-10", "step-5-preview"), ("new-provider-10", "step-3.7-flash")}
NEX = ("new-provider-5", "nex-n2.6")
DEEPSEEK = {("new-provider-6", "deepseek-v4.1-flash"), ("new-provider-11", "deepseek-v4.1-flash")}
OUTSOURCE = ("outsource-minekuai", "deepseek-v4.1-flash-1017")  # 发起人 2026-11-25：外包并发 60（D-035 单并发已废）
ALLOWED = {GLM, *STEP, NEX, OUTSOURCE}
RANK = {GLM: 1, ("new-provider-10", "step-5-preview"): 2, ("new-provider-10", "step-3.7-flash"): 2, NEX: 3}

# V4.0-P1（修 D-10，决策 D6 2026-11-22）：V3 流水线（factory_runner）的 Builder/Verifier 会话
# 按 config/routing.json 登记的 lane 判合规——网关路由表就是 V3 的模型授权清单。
ROUTING_PATH = os.path.join(ROOT, "config", "routing.json")
V3_ROLES = {"v3_builder", "v3_verifier"}


def v3_allowed_lanes(path=ROUTING_PATH):
    """routing.json lanes -> {(providerId, model)}；另含 weekend-glm 免费票（官方账号直连，不进网关）。"""
    out = {GLM}
    try:
        cfg = json.load(open(path, encoding="utf-8"))
        for ln in cfg.get("lanes") or []:
            if ln.get("providerId") and ln.get("model"):
                out.add((ln["providerId"], ln["model"]))
    except (OSError, ValueError):
        pass
    return out


def role_of(title):
    t = title or ""
    # V4.0-P1：V3 内核会话标题形如「【V3-生产施工】…」「【V3-UI审美质检员】…」「【V3-复用既有沙箱草稿】…」
    if t.startswith("【V3-") or t.startswith("【V4-"):
        return "v3_verifier" if ("质检" in t or "QC#" in t) else "v3_builder"
    # F-63 整改(2026-11-19,TS 外部维修): 薄版调度台是 D-043 控制面的管理席,按管理席口径放行 DeepSeek。
    if "调度台" in t:
        return "steward"
    # F-52 整改(2026-11-19,TS 外部维修): 用户直派调研席「查看同行解决方案」走 DeepSeek,
    # 登记专属角色供 classify 豁免,消除持续违规告警。
    if "同行解决方案" in t:
        return "research"
    if "厂长" in t:
        return "steward"
    if re.search(r"M-[A-Z]+", t) or "经理" in t:
        return "manager"
    if "验收" in t:
        return "verifier"
    if re.search(r"worker", t, re.I) or "开发" in t or "返工" in t:
        return "worker"
    if "SW1" in t or re.search(r"\bS1\b", t) or "安全" in t:
        return "safety"
    if "外包" in t or "OB1" in t:
        return "outsource"
    if "组长" in t or re.search(r"\bL\d\b", t):
        return "leader"
    if t.startswith("A1") or "A1 " in t:
        return "assistant"
    return "unknown"


MANAGEMENT_ROLES = {"steward", "manager", "planner", "production", "quality", "audit", "safety"}


def classify(provider, model, role):
    """返回 (是否合规, 原因)；D-032：管理席统一 DeepSeek（≤6）。"""
    pm = (provider, model)
    if role in V3_ROLES:
        if pm in v3_allowed_lanes():
            return True, "V3 流水线按 routing.json 授权（D6, 2026-11-22）"
        return False, "V3 流水线使用了 routing.json 未登记的模型道"
    if pm == GLM:
        return True, "夜间①GLM"
    if pm in STEP:
        return True, "夜间②STEP"
    if pm == NEX:
        return True, "夜间③NEX"
    if pm in DEEPSEEK:
        if role in MANAGEMENT_ROLES:
            return True, "管理席DeepSeek（D-032）"
        if role == "research":
            return True, "用户直派调研席豁免（F-52,2026-11-19）"
        return False, "DeepSeek 非管理席使用"
    if pm == OUTSOURCE:
        if role in ("outsource", "worker", "unknown"):
            return True, "外包道（2026-11-25 起并发 60）"
        return False, "外包道角色错配"
    return False, "未授权模型道"


def day_bounds(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = datetime(d.year, d.month, d.day)
    return start, start + timedelta(days=1)


def collect(date_str):
    start, end = day_bounds(date_str)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select session_id, provider_id, model_id, started_at, computed_total_tokens tok, input_tokens, output_tokens
           from model_usage where started_at >= ? and started_at < ? and status != 'cancelled'""",
        (int(start.timestamp() * 1000), int(end.timestamp() * 1000)),
    ).fetchall()
    titles = {}
    for sid in {r["session_id"] for r in rows}:
        t = con.execute("select title from session where id=?", (sid,)).fetchone()
        titles[sid] = (t["title"] if t else "") or ""
    con.close()

    buckets = {}
    for r in rows:
        hour = datetime.fromtimestamp(r["started_at"] / 1000).strftime("%H")
        key = (hour, r["session_id"], r["provider_id"], r["model_id"])
        b = buckets.setdefault(key, {"calls": 0, "tok": 0, "in": 0, "out": 0})
        b["calls"] += 1
        b["tok"] += r["tok"] or 0
        b["in"] += r["input_tokens"] or 0
        b["out"] += r["output_tokens"] or 0

    out = []
    for (hour, sid, prov, model), b in sorted(buckets.items()):
        title = titles.get(sid, "")
        role = role_of(title)
        ok, why = classify(prov, model, role)
        out.append(
            {
                "hour": hour,
                "session_id": sid,
                "title": title,
                "role": role,
                "provider": prov,
                "model": model,
                "calls": b["calls"],
                "tokens": b["tok"],
                "ok": ok,
                "reason": why,
            }
        )
    return out


def fmt_tokens(n):
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.1f}万"
    return str(int(n))


def render_day(date_str, rows):
    lines = [f"## {date_str}（夜间窗口 21:00–09:00；夜间只允许 GLM→STEP→NEX，DeepSeek 仅经理）", ""]
    if not rows:
        lines.append("（无模型调用记录）")
        return "\n".join(lines)
    lines.append("| 时间段 | 会话/标题 | 角色 | 模型 | 调用 | tokens | 判定 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        mark = "OK" if r["ok"] else "**违规**"
        title = (r["title"] or "(无标题)").replace("|", "/")[:34]
        lines.append(
            f"| {r['hour']}:00-{r['hour']}:59 | {title} `{r['session_id'][:14]}` | {r['role']} | "
            f"{r['provider']}/{r['model']} | {r['calls']} | {fmt_tokens(r['tokens'])} | {mark}（{r['reason']}） |"
        )
    total = sum(r["tokens"] for r in rows)
    by_model = {}
    for r in rows:
        k = f"{r['provider']}/{r['model']}"
        by_model[k] = by_model.get(k, 0) + r["tokens"]
    lines.append("")
    lines.append(f"- 当日合计：{fmt_tokens(total)} tokens；分道：" + "；".join(f"{k} {fmt_tokens(v)}" for k, v in sorted(by_model.items(), key=lambda x: -x[1])))
    bad = [r for r in rows if not r["ok"]]
    lines.append(f"- 违规记录：{len(bad)} 条" + ("（见上表加粗行）" if bad else "（无）"))
    return "\n".join(lines)


def update_ledger(date_str, section):
    header = (
        "# MODEL_LEDGER（模型使用台账）\n\n"
        "> 由 `scripts/model_ledger.py` 从 `model_usage` 只读生成，按天分节；每 30 分钟巡检重跑当天段。\n"
        "> 规则（D-029）：夜间 21:00–09:00 只允许 ①智谱体验 GLM-5.3-Flash ②STEP ③NEX；白天同三档；DeepSeek Flash 仅经理。\n"
        "> 生效：D-029 于 2026-11-19 06:45 立规。本日 06:43 前产生的 DeepSeek 非经理调用为**历史违规**，如实登记、不追溯不洗白；06:45 起新增即违规。\n"
    )
    if os.path.exists(LEDGER):
        text = open(LEDGER, encoding="utf-8").read()
    else:
        text = header
    pat = re.compile(rf"^## {re.escape(date_str)}.*?(?=^## |\Z)", re.S | re.M)
    if pat.search(text):
        text = pat.sub(section + "\n\n", text, count=1)
    else:
        if not text.rstrip().endswith("）") and header not in text:
            text = header + "\n" + text
        text = text.rstrip() + "\n\n" + section + "\n"
    open(LEDGER, "w", encoding="utf-8").write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows = collect(args.date)
    bad = [r for r in rows if not r["ok"]]
    summary = {
        "date": args.date,
        "rows": len(rows),
        "tokens": sum(r["tokens"] for r in rows),
        "by_model": {},
        "violations": [
            {"hour": r["hour"], "title": r["title"], "role": r["role"], "provider": r["provider"], "model": r["model"], "tokens": r["tokens"], "reason": r["reason"]}
            for r in bad
        ],
    }
    for r in rows:
        k = f"{r['provider']}/{r['model']}"
        summary["by_model"][k] = summary["by_model"].get(k, 0) + r["tokens"]

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        section = render_day(args.date, rows)
        if not args.check:
            update_ledger(args.date, section)
        print(f"[MODEL_LEDGER] {args.date} rows={len(rows)} tokens={fmt_tokens(summary['tokens'])} violations={len(bad)}")
        for r in bad[:20]:
            print(f"  ! {r['hour']}:xx {r['role']:9s} {r['provider']}/{r['model']} {fmt_tokens(r['tokens'])} — {r['reason']} — {(r['title'] or '')[:30]}")
    if bad and args.check:
        sys.exit(2)


if __name__ == "__main__":
    main()
