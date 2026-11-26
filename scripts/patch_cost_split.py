# -*- coding: utf-8 -*-
"""成本区三口径改造：白班/夜班残留(shift_tokens)删除，换 免费/付费/合计 真账。
数据层 v3_data.py + 前端 v2.html renderCost 双补丁，改前备份，py_compile 闸。"""
import py_compile
import shutil

S = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\scripts\v3_data.py"
H = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\dashboard\v2.html"

# ---------- 1) 数据层 ----------
shutil.copyfile(S, S + ".bak-costsplit-20261122")
src = open(S, encoding="utf-8").read()

OLDV = '    daily_cny = float(ledger.get("daily_cost_cny") or 0.0)'
NEWV = (OLDV + '\n'
        '    free_tok = int(ledger.get("daily_weekend_glm_tokens") or 0)\n'
        '    paid_tok = int(ledger.get("daily_gateway_tokens") or 0)')
assert src.count(OLDV) == 1
src = src.replace(OLDV, NEWV, 1)

OLDC = '''            "cost": {
                "status": "ok",
                "as_of": now_iso,
                "today_tokens": v3_tokens,
                "shift_tokens": v3_tokens,
                "cumulative_tokens": v3_tokens,
                "by_lane": [
                    {
                        "lane": "V3生产与质检主力道",
                        "tokens": v3_tokens,
                        "providers": [{"provider": "V3计费网关", "model": f"今日实耗 \xa5{daily_cny:.2f} / 日限 \xa530.00"}],
                    }
                ],
                "note": f"V3 真实账单：今日实耗 \xa5{daily_cny:.4f}（单卡上限 \xa52.00 · 全厂日限 \xa530.00 · 管理税 0.0%）",
            },'''
NEWC = '''            "cost": {
                "status": "ok",
                "as_of": now_iso,
                "free_tokens": free_tok,
                "paid_tokens": paid_tok,
                "paid_cny": round(daily_cny, 2),
                "today_tokens": free_tok + paid_tok,
                "cumulative_tokens": v3_tokens,
                "by_lane": [
                    {"lane": "免费道 weekend-glm", "tokens": free_tok,
                     "providers": [{"provider": "免费通道", "model": "\xa50.00"}]},
                    {"lane": "付费网关池", "tokens": paid_tok,
                     "providers": [{"provider": "付费通道", "model": f"今日 \xa5{daily_cny:.2f}"}]},
                ],
                "note": f"今日：免费 {free_tok:,} / 付费 {paid_tok:,}（\xa5{daily_cny:.2f}）/ 合计 {free_tok + paid_tok:,} · 月累计 {v3_tokens:,}",
            },'''
if src.count(OLDC) != 1:
    OLDC = OLDC.replace("\xa5", "\uffe5")
    NEWC = NEWC.replace("\xa5", "\uffe5")
assert src.count(OLDC) == 1, "COST ANCHOR NOT FOUND"
src = src.replace(OLDC, NEWC, 1)
open(S, "w", encoding="utf-8", newline="\n").write(src)
py_compile.compile(S, doraise=True)
print("DATA_PATCH_OK")

# ---------- 2) 前端 ----------
shutil.copyfile(H, H + ".bak-costsplit-20261122")
html = open(H, encoding="utf-8").read()

OLDG = "(!has(c.today_tokens)&&!has(c.shift_tokens)&&!has(c.cumulative_tokens))"
NEWG = "(!has(c.today_tokens)&&!has(c.free_tokens)&&!has(c.paid_tokens))"
assert html.count(OLDG) == 1
html = html.replace(OLDG, NEWG, 1)

OLDBOX = ("'<div class=\"cbox tile\"><div class=\"cv\">'+esc(fmtTokens(c.today_tokens))+'</div><div class=\"cl\">今日消耗</div></div>'+\n"
          "    '<div class=\"cbox tile\"><div class=\"cv\">'+esc(fmtTokens(c.shift_tokens))+'</div><div class=\"cl\">本班消耗</div></div>'+\n"
          "    '<div class=\"cbox tile\"><div class=\"cv\">'+esc(fmtTokens(c.cumulative_tokens))+'</div><div class=\"cl\">累计消耗</div></div></div>'+")
NEWBOX = ("'<div class=\"cbox tile\"><div class=\"cv\">'+esc(fmtTokens(c.free_tokens||0))+'</div><div class=\"cl\">免费消耗</div></div>'+\n"
          "    '<div class=\"cbox tile\"><div class=\"cv\">'+esc(fmtTokens(c.paid_tokens||0))+'</div><div class=\"cl\">付费消耗 \xa5'+esc((c.paid_cny!=null&&c.paid_cny!==undefined)?Number(c.paid_cny).toFixed(2):'0.00')+'</div></div>'+\n"
          "    '<div class=\"cbox tile\"><div class=\"cv\">'+esc(fmtTokens(c.today_tokens||0))+'</div><div class=\"cl\">今日合计</div></div></div>'+")
if html.count(OLDBOX) != 1:
    OLDBOX = OLDBOX.replace("\xa5", "\uffe5")
    NEWBOX = NEWBOX.replace("\xa5", "\uffe5")
assert html.count(OLDBOX) == 1, "BOX ANCHOR NOT FOUND"
html = html.replace(OLDBOX, NEWBOX, 1)
open(H, "w", encoding="utf-8", newline="\n").write(html)
print("FRONT_PATCH_OK")
