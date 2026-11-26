#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quota_probe.py — 查官方套餐额度（GLM-5.3 官方套餐 + MCP 额度）。

两个口径，来源不同，诚实分列：
1. 官方口径：ZCode 客户端每几分钟轮询 zcode.z.ai，响应体写在 v2/logs/<今日>.log。
   - /api/v1/mcp/usage          → 官方 MCP 额度（level/used/limit/remaining，每日刷新）
   - /api/v1/zcode-plan/billing/balance → 计费余额
   注意：编程套餐 prompt 额度端点 https://bigmodel.cn/api/monitor/usage/quota/limit
   需要 OAuth Authorization（enc:v2 加密，拿不到）；且凭据 scope 失效时客户端自己也
   报 coding_plan_account_changed —— 该项抓不到时如实标 unavailable，不编数。
2. 本地口径：db.sqlite model_usage 按天聚合（管家自有账本，非官方额度数字）。

用法：python quota_probe.py [YYYY-MM-DD]
"""
import json
import os
import re
import sqlite3
import sys
import datetime as dt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.expanduser(r"~\.zcode\cli\db\db.sqlite")
V2LOG = os.path.expanduser(r"~\.zcode\v2\logs")

RE_MCP = re.compile(r"官方 MCP 额度响应 \{\"body\":\"(.*?)\",\"durationMs")
RE_BAL = re.compile(r"billing/balance 请求完成 (\{.*\})\s*$")


def safe_json(s):
    try:
        return json.loads(s)
    except Exception:
        return None


def official(day):
    """从 v2/logs/<day>.log 扫全文件取**最新**一条 MCP 额度与 billing 余额响应。

    billing 的 body 是日志行内二次转义的 JSON，需再反转义一层。
    """
    out = {"mcp_usage": None, "billing_balance": None}
    path = os.path.join(V2LOG, day + ".log")
    if not os.path.exists(path):
        return out
    mcp = bal = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "官方 MCP 额度响应" in line:
                m = RE_MCP.search(line)
                if m:
                    body = safe_json(json.loads('"' + m.group(1) + '"'))
                    if body:
                        mcp = body
            if "billing/balance 请求完成" in line:
                m = RE_BAL.search(line.rstrip())
                if m:
                    d = safe_json(m.group(1))
                    # 部分轮询响应 plans 为空，优先保留带数据的最新一条
                    if d and (d.get("plans") or d.get("balances") or bal is None):
                        bal = d
    out["mcp_usage"], out["billing_balance"] = mcp, bal
    return out


def local_usage(day):
    """db.sqlite 今日按 provider 聚合（本地记账口径，started_at 为毫秒 epoch）。"""
    if not os.path.exists(DB):
        return []
    con = sqlite3.connect(DB, timeout=5)
    try:
        rows = con.execute(
            "select provider_id, model_id, count(*), sum(input_tokens),"
            " sum(output_tokens), sum(coalesce(cache_read_input_tokens,0))"
            " from model_usage"
            " where date(started_at/1000,'unixepoch','localtime')=?"
            " group by provider_id, model_id order by sum(input_tokens) desc",
            (day,),
        ).fetchall()
        if not rows:  # model_usage 停写时退 turn_usage（无 provider 维度，按会话聚合）
            rows = con.execute(
                "select session_id, '', count(*), sum(input_tokens),"
                " sum(output_tokens), sum(coalesce(cache_read_input_tokens,0))"
                " from turn_usage"
                " where date(started_at/1000,'unixepoch','localtime')=?"
                " group by session_id order by sum(input_tokens) desc",
                (day,),
            ).fetchall()
    finally:
        con.close()
    return [
        {
            "provider": r[0], "model": r[1] or "(turn聚合)", "requests": r[2],
            "input": r[3] or 0, "output": r[4] or 0, "cache_read": r[5] or 0,
        }
        for r in rows
    ]


DUE_DATES = [
    # (事件, 到期时刻, 提醒提前天数)
    ("step 额度重置(回满400亿)", dt.datetime(2026, 11, 19, 13, 20), 0),
    ("apigoto TCredits 重置(回满100M)", dt.datetime(2026, 11, 23, 16, 10), 0),
    ("R4 到期(worker主力,需续费/迁移)", dt.datetime(2026, 12, 17, 19, 41), 3),
    ("R4-2 到期", dt.datetime(2026, 12, 18, 10, 41), 3),
    ("step 订阅到期", dt.datetime(2027, 2, 5, 0, 0), 7),
]


def due_calendar():
    now = dt.datetime.now()
    print("=" * 8, "到期日历", "=" * 8)
    for name, t, alert_days in DUE_DATES:
        delta = t - now
        if delta.total_seconds() < 0:
            print(f"[已过] {name} ({t:%Y-%m-%d %H:%M})")
            continue
        days = delta.total_seconds() / 86400
        mark = " <<<< 提醒用户" if days <= alert_days else ""
        print(f"[{days:5.1f}天后] {name} ({t:%Y-%m-%d %H:%M}){mark}")


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    due_calendar()
    off = official(day)
    mcp = off["mcp_usage"]
    print("=" * 8, "官方口径", day, "=" * 8)
    if mcp:
        d = mcp.get("data", mcp)
        tu = d.get("total_usage", {})
        nra = d.get("next_refresh_at")
        nra_s = (
            dt.datetime.fromtimestamp(nra).strftime("%m-%d %H:%M") if nra else "?"
        )
        print(f"[MCP额度] level={d.get('level')} used={tu.get('used')}/"
              f"{tu.get('limit')} remaining={tu.get('remaining')} "
              f"每日刷新(下次 {nra_s})")
    else:
        print("[MCP额度] 今日日志暂无响应（客户端未轮询到或未开 GUI）")
    bal = off["billing_balance"]
    if bal:
        print(f"[计费余额] provider={bal.get('providerId')}")
        for pl in bal.get("plans") or []:
            print(f"  套餐: {pl.get('name')} ({pl.get('plan_id')}) status={pl.get('status')}")
        for bb in bal.get("balances") or []:
            tot, used = bb.get("total_units"), bb.get("used_units")
            rem = bb.get("remaining_units")
            pct = f" 剩{rem/tot:.1%}" if tot else ""
            print(f"  额度: {bb.get('show_name')} 总{tot:,} 已用{used:,} 剩{rem:,}{pct}")
    print("[编程套餐prompt额度] 需OAuth(加密)——不可直查；客户端快照失效时报"
          "coding_plan_account_changed，恢复登录后此处日志可见")
    print("=" * 8, "本地账本", day, "=" * 8)
    rows = local_usage(day)
    if not rows:
        print("（无记录）")
    for r in rows:
        print(f"{r['provider']}/{r['model']}: {r['requests']}次 "
              f"in={r['input']:,} out={r['output']:,} cache_read={r['cache_read']:,}")
    burn_progress(rows)
    return 0


# 烧量进度（MODEL_SCORECARD §三 基准）：限时作废池仅 R4 系 180 亿，
# 需日烧 >= 6.2 亿 tokens（in+out 合计），对基准输出进度。
BURN_TARGET = 6.2e8


def burn_progress(rows):
    r4_total = sum(r["input"] + r["output"] for r in rows
                   if str(r["provider"]) in ("new-provider-6", "new-provider-11"))
    step_total = sum(r["input"] + r["output"] for r in rows
                     if str(r["provider"]) == "new-provider-10")
    nex_total = sum(r["input"] + r["output"] for r in rows
                    if str(r["provider"]) == "new-provider-5")
    pct = r4_total / BURN_TARGET
    print("=" * 8, "烧量进度(对 R4系 6.2亿/日 基准)", "=" * 8)
    print(f"R4系今日 {r4_total/1e8:.2f} 亿 ({pct:.0%}) | step-5 {step_total/1e8:.2f} 亿 | "
          f"nex {nex_total/1e8:.2f} 亿（两池不计基准：用不完/免费）")
    if pct < 0.5:
        print(">> 提示：高级任务并行度不足——按记分卡 §四 加开多版本/并行 worker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
