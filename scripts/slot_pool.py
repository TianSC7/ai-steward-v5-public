#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""slot_pool.py — 劳务派遣监督池（每模型道并发席位实时台账）

用户令（2026-11-19 07:3x）：STEP 3 并发、GLM-5.3 3 并发、NEX 3 并发；
经理/组长/员工每占用一个席位，池子就少一个；不许三个经理挤同一条道。

口径：
  - 席位持有者 = 最近 N 分钟（默认 3）内有 model_usage 调用的会话（不同 session 计 1 席）。
  - 道容量：GLM=3、STEP=3、NEX=3（state/slot_pool_config.json 可改）。
  - 角色分道（D-031）：GLM=验收员×2+安全×1；STEP=策划/生产/质量三经理；
    NEX=审计经理+组长+员工；DeepSeek 付费道单列，不计入免费池。
  - 超容 / 角色错道 / GLM 验收>2 → 违规，exit 2。
输出：
  - state/SLOT_POOL.md     当前池面（每次运行重写“当前”段 + 追加历史行）
  - state/slot_pool.jsonl  机器快照
用法：
  python -X utf8 scripts/slot_pool.py [--active-min 3] [--json] [--check]
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
DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
CONFIG = os.path.join(STATE, "slot_pool_config.json")
MD = os.path.join(STATE, "SLOT_POOL.md")
JSONL = os.path.join(STATE, "slot_pool.jsonl")

DEFAULT_CAP = {"GLM": 3, "STEP": 3, "NEX": 3, "R4": 6, "R4-2": 6, "OUTSOURCE": 60}
LANE_OF = {
    ("account:bigmodel-start-plan", "GLM-5.3-Flash"): "GLM",
    ("new-provider-10", "step-5-preview"): "STEP",
    ("new-provider-10", "step-3.7-flash"): "STEP",
    ("new-provider-5", "nex-n2.6"): "NEX",
    ("new-provider-6", "deepseek-v4.1-flash"): "R4",
    ("new-provider-11", "deepseek-v4.1-flash"): "R4-2",
    # 外包员工（minekuai 非信任中转）：并发 60，发起人 2026-11-25 口径（原 D-035 单并发已废）
    ("outsource-minekuai", "deepseek-v4.1-flash-1017"): "OUTSOURCE",
}

# 角色分道政策（D-031/D-032/D-033）：管理层统一 DeepSeek（R4+R4-2 共 12）；执行席用免费道。
ROLE_LANE = {
    "R4": {"steward", "planner", "production", "quality", "audit", "safety"},
    "R4-2": {"steward", "planner", "production", "quality", "audit", "safety"},
    "GLM": {"verifier", "safety", "leader", "worker"},
    "STEP": {"verifier", "leader", "worker"},
    "NEX": {"verifier", "leader", "worker"},
    "OUTSOURCE": {"outsource"},
}
DEPT_NAME = {
    "planner": "M-PLAN", "production": "M-PRODUCT", "quality": "M-QUALITY",
    "audit": "M-AUDIT", "safety": "M-SAFETY", "verifier": "验收员",
    "leader": "组长", "worker": "员工", "outsource": "外包员工",
}
MANAGER_ROLES = {"steward", "planner", "production", "quality", "audit", "safety"}
EXEC_ROLES = {"verifier", "leader", "worker", "outsource"}


def role_of(title):
    t = title or ""
    # F-48 整改(2026-11-19,TS 外部维修): 员工席标题含"直属厂长"曾被最先命中的厂长分支
    # 误判为 steward 触发假错道告警;worker 判定提前。厂长本席标题不含 员工/worker/开发,不受影响。
    # F-48-01 补(14:0x,新厂长): 执行席标题统一为「W-xxxx 执行席（道），直属厂长」，未含上述三词
    # 仍被误判 steward → 加"执行席"入 worker 判定。
    if "worker" in t.lower() or "员工" in t or "开发" in t or "执行席" in t:
        return "worker"
    if "厂长" in t:
        return "steward"
    if "M-PLAN" in t or "策划" in t:
        return "planner"
    if "M-PRODUCT" in t or "生产经理" in t:
        return "production"
    if "M-QUALITY" in t or "质量经理" in t:
        return "quality"
    if "M-AUDIT" in t:
        return "audit"
    if "验收" in t:
        return "verifier"
    if "M-SAFETY" in t or re.search(r"\bS1\b", t) or "SW1" in t:
        return "safety"
    if "外包" in t or "OB1" in t:
        return "outsource"
    if "组长" in t or re.search(r"\bL\d\b", t):
        return "leader"
    if "worker" in t.lower() or "员工" in t or "开发" in t:
        return "worker"
    if "审计" in t:
        return "audit"
    if "安全" in t:
        return "safety"
    if re.search(r"M-[A-Z]+", t) or "经理" in t:
        return "manager"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--active-min", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    caps = dict(DEFAULT_CAP)
    if os.path.exists(CONFIG):
        try:
            caps.update(json.load(open(CONFIG, encoding="utf-8")))
        except Exception:
            pass

    now = datetime.now()
    since = int((now - timedelta(minutes=args.active_min)).timestamp() * 1000)
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select session_id, provider_id, model_id, count(*) calls, sum(computed_total_tokens) tok,
                  max(started_at) last
           from model_usage where started_at>=? and status!='cancelled'
           group by session_id, provider_id, model_id""", (since,)).fetchall()
    titles = {}
    for r in rows:
        if r["session_id"] not in titles:
            t = con.execute("select title from session where id=?", (r["session_id"],)).fetchone()
            titles[r["session_id"]] = (t["title"] if t else "") or ""
    con.close()

    holders = {lane: {} for lane in caps}
    paid_holders = []
    for r in rows:
        key = (r["provider_id"], r["model_id"])
        title = titles.get(r["session_id"], "")
        role = role_of(title)
        lane = LANE_OF.get(key)
        if not lane or lane not in holders:
            continue
        h = holders[lane].setdefault(r["session_id"], {
            "session": r["session_id"], "title": title, "role": role,
            "model": f"{r['provider_id']}/{r['model_id']}", "calls": 0, "tokens": 0,
        })
        h["calls"] += r["calls"]
        h["tokens"] += r["tok"] or 0

    violations = []
    table = {}
    for lane, cap in caps.items():
        hs = sorted(holders.get(lane, {}).values(), key=lambda x: -x["tokens"])
        used = len(hs)
        free = cap - used
        if used > cap:
            violations.append(f"{lane} 超容：{used}>{cap}")
        allowed = ROLE_LANE.get(lane, set())
        for h in hs:
            if h["role"] not in allowed and h["role"] != "other":
                violations.append(f"{lane} 错道：{h['role']} {h['session'][:16]}（{h['title'][:24]}）")
        if lane == "GLM":
            v = sum(1 for h in hs if h["role"] == "verifier")
            if v > 2:
                violations.append(f"GLM 验收员超配：{v}>2")
        table[lane] = {"cap": cap, "used": used, "free": free, "holders": hs}

    snap = {
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "active_min": args.active_min,
        "lanes": table, "paid": paid_holders, "violations": violations,
    }
    with open(JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    lines = ["# SLOT_POOL（劳务派遣监督池·实时席位）", "",
             f"> 更新 {snap['ts']}（活跃窗口 {args.active_min} 分钟；容量 R4/R4-2/GLM/STEP/NEX/外包 = "
             f"{caps['R4']}/{caps['R4-2']}/{caps['GLM']}/{caps['STEP']}/{caps['NEX']}/{caps['OUTSOURCE']}，DeepSeek 合计 "
             f"{caps['R4']+caps['R4-2']}）", "",
             "| 道 | 占用/容量 | 空闲 | 持有席（角色·会话·调用/tokens） |",
             "|---|---|---|---|"]
    for lane in ("R4", "R4-2", "GLM", "STEP", "NEX", "OUTSOURCE"):
        t = table[lane]
        hold = "；".join(
            f"{h['role']} {h['session'][:14]}({h['calls']}c/{_fmt(h['tokens'])})" for h in t["holders"]
        ) or "—"
        lines.append(f"| {lane} | {t['used']}/{t['cap']} | {t['free']} | {hold} |")
    ds_used = table["R4"]["used"] + table["R4-2"]["used"]
    lines.append(f"| **DeepSeek 合计** | **{ds_used}/{caps['R4']+caps['R4-2']}** | {caps['R4']+caps['R4-2']-ds_used} | — |")
    lines.append("")
    role_cnt = snapshot_roles(table)
    lines.append("## 人力统计（活跃窗口内实际在干活的席）")
    lines.append("")
    lines.append("| 管理层 | 验收员 | 组长 | 员工 | 外包员工 | **执行员工合计** |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(f"| {role_cnt['managers']} | {role_cnt['verifier']} | {role_cnt['leader']} | "
                 f"{role_cnt['worker']} | {role_cnt['outsource']} | **{role_cnt['employees']}** |")
    lines.append("")
    if violations:
        lines.append("## 违规")
        lines += [f"- {v}" for v in violations]
    else:
        lines.append("## 池面正常（无超容/错道）")
    lines.append("")
    lines.append("规则（D-031/D-032/D-033/D-035）：DeepSeek=厂长+五部经理，R4 6 + R4-2 6 合计 12；"
                 "GLM=验收员（≤2）+安全/组长/员工；STEP/NEX=验收/组长/员工；"
                 "外包 minekuai=外包员工并发 60（非信任中转，只许 Read/Write 白名单任务）；各占 1 席；满员改道或排队（state/SLOT_QUEUE.md），不得硬开。")
    open(MD, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    if args.json:
        snap["roles"] = role_cnt
        print(json.dumps(snap, ensure_ascii=False, indent=1))
    else:
        print(f"[SLOT_POOL] {snap['ts']} active={args.active_min}m")
        for lane in ("R4", "R4-2", "GLM", "STEP", "NEX", "OUTSOURCE"):
            t = table[lane]
            print(f"  {lane}: {t['used']}/{t['cap']} free={t['free']}")
            for h in t["holders"]:
                print(f"    {h['role']:10s} {h['session'][:18]} {h['calls']}c {_fmt(h['tokens'])} {h['title'][:34]}")
        print(f"  DeepSeek 合计: {table['R4']['used']+table['R4-2']['used']}/{caps['R4']+caps['R4-2']}")
        print(f"  人力：管理层 {role_cnt['managers']}｜验收 {role_cnt['verifier']}｜组长 {role_cnt['leader']}｜"
              f"员工 {role_cnt['worker']}｜外包员工 {role_cnt['outsource']}｜执行员工合计 {role_cnt['employees']}")
        for v in violations:
            print("  ! " + v)
    if args.check and violations:
        sys.exit(2)


def snapshot_roles(table):
    cnt = {"managers": 0, "verifier": 0, "leader": 0, "worker": 0, "outsource": 0, "other": 0, "employees": 0}
    for lane in table:
        for h in table[lane]["holders"]:
            role = h["role"]
            if role in MANAGER_ROLES:
                cnt["managers"] += 1
            elif role in ("verifier", "leader", "worker", "outsource"):
                cnt[role] += 1
            else:
                cnt["other"] += 1
    cnt["employees"] = cnt["leader"] + cnt["worker"] + cnt["outsource"]
    return cnt


def _fmt(n):
    return f"{n/1e4:.1f}万" if n >= 1e4 else str(int(n))


if __name__ == "__main__":
    main()
