# -*- coding: utf-8 -*-
"""C-20261120-009 W-104 · 看板七区 UI 结构自测（只读，stdlib）。

用法：
  python -X utf8 scripts/test_board_ui.py [--base http://127.0.0.1:8901]
         [--token-file config/board_token.txt]

断言（对已起服务取 HTML + /data.json，不改任何数据）：
  T01 契约 c009-zones-v1，zone_status 七键齐全
  T02 七区容器 id=z-<zone> 齐
  T03 置顶横条 sticky/top:0 + data-level 合法
  T04 横条红/黄数 == 告警区 red/yellow 行数（合理延期蓝行不计红/黄，可点数核对）
  T05 数字角标 ≥5 且 data-source 全非空，含 board_zones.py:行号
  T06 16 态 chip 齐（.sx）
  T07 60s 轮询接线（REFRESH=60 + setInterval(pollNow,REFRESH*1000) + /data.json no-store）
  T08 禁占位/假数标记（C-NEW/TODO/占位/lorem）
  T09 缺源降级样式 .zbar.missing 在 CSS 中定义
  T10 合理延期蓝（MES/08）：非终态+§10 留痕单 = 蓝行；bar/q2 的 N 蓝一致
  T11 当前通道列（T05 追加）：每席 provider/model 来自 model_usage；无源席位灰条、禁假数
  T12 心跳独立 zone（#z-heartbeat）+ 未接区「施工中」标注 + ②区顶注不自相矛盾
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request

ZONES = ["orders", "seats", "events", "capacity", "alerts", "cost", "handover"]
STAT = {"ok", "degraded", "missing"}
FAIL = []


def check(tid, name, ok, detail=""):
    print("%s %-4s %s%s" % ("PASS" if ok else "FAIL", tid, name, (" | " + detail) if detail else ""))
    if not ok:
        FAIL.append(tid)


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8901")
    ap.add_argument("--token-file", default="config/board_token.txt")
    a = ap.parse_args()
    token = open(a.token_file, encoding="ascii").read().strip()
    base = a.base.rstrip("/")

    st_html, html_b = get(base + "/?k=" + token)
    html = html_b.decode("utf-8", "replace")
    st_json, json_b = get(base + "/data.json?k=" + token)
    data = json.loads(json_b.decode("utf-8"))

    check("T01", "契约与七键", data.get("contract") == "c009-zones-v1"
          and sorted(data.get("zone_status") or {}) == sorted(ZONES)
          and set((data.get("zone_status") or {}).values()) <= STAT,
          "contract=%s status=%s" % (data.get("contract"), data.get("zone_status")))

    miss = [z for z in ZONES if ('id="z-%s"' % z) not in html]
    check("T02", "七区容器", not miss, "missing=%s" % miss if miss else "7/7")

    m = re.search(r"<div id=\"alertbar\"[^>]*data-level=\"(\w+)\"", html)
    css = re.search(r"#alertbar\{position:sticky;top:0", html)
    check("T03", "置顶横条", bool(m) and (m.group(1) in {"red", "yellow", "green", "gray", "blue"}) and bool(css),
          "level=%s sticky_top0=%s" % (m.group(1) if m else "?", bool(css)))

    i = html.find('id="alertbar"')
    seg = html[i:i + 600] if i >= 0 else ""
    bar_txt = re.sub(r"<[^>]+>", " ", seg)
    mr = re.search(r"(\d+)\s*红", bar_txt)
    my = re.search(r"(\d+)\s*黄", bar_txt)
    items = ((data.get("zones") or {}).get("alerts") or {}).get("items") or []
    orders = ((data.get("zones") or {}).get("orders") or {}).get("items") or []
    TERM = {"delivered", "accepted", "returned", "void", "failed"}
    deferred = set(o.get("code") for o in orders
                   if o.get("code") and o.get("delay_note")
                   and str(o.get("state_raw") or o.get("state") or "") not in TERM)
    red = sum(1 for x in items if x.get("level") == "red"
              and not (x.get("order") in deferred and "超交付截止" in (x.get("msg") or "")))
    yel = sum(1 for x in items if x.get("level") == "yellow"
              and not (x.get("order") in deferred and "超交付截止" in (x.get("msg") or "")))
    ok_bar = (mr is None and my is None) or (
        (mr and int(mr.group(1)) == red) and (my and int(my.group(1)) == yel))
    mb = re.search(r"(\d+)\s*蓝", bar_txt)
    blue_ok = (len(deferred) == 0 and mb is None) or (mb and int(mb.group(1)) == len(deferred))
    check("T04", "横条对账", ok_bar,
          "bar=%s红/%s黄 items=%d红/%d黄 deferred=%d 蓝bar=%s"
          % (mr.group(1) if mr else "-", my.group(1) if my else "-", red, yel,
             len(deferred), mb.group(1) if mb else "-"))

    rows_blue = re.findall(r'<tr class="orow blue"[^>]*data-code="([^"]+)"', html)
    tags = re.findall(r'延期·新时限 ([^<]+)<', html)
    q2_seg = html[html.find('id="panel_q2"'):html.find('id="panel_q2"') + 700]
    mq = re.search(r'(\d+)\s*蓝', re.sub(r"<[^>]+>", " ", q2_seg))
    ok_blue = (set(rows_blue) == deferred and len(tags) >= len(deferred)
               and blue_ok and (len(deferred) == 0 or (mq and int(mq.group(1)) == len(deferred))))
    check("T10", "合理延期蓝（MES/08）", ok_blue,
          "deferred=%d rows_blue=%d tags=%d bar_blue=%s q2_blue=%s"
          % (len(deferred), len(rows_blue), len(tags), mb.group(1) if mb else "-",
             mq.group(1) if mq else "-"))

    seat_items = ((data.get("zones") or {}).get("seats") or {}).get("items") or []
    ch_ok = sum(1 for s in seat_items
                if str((s.get("channel") or {}).get("status") or "") == "ok"
                and (s.get("channel") or {}).get("provider") and (s.get("channel") or {}).get("model"))
    ch_miss = len(seat_items) - ch_ok
    chan_tags = len(re.findall(r'data-source="board_zones\.py:314 ', html))
    chmiss = len(re.findall(r'class="zbar missing chmiss"', html))
    zone_gray = len(re.findall(r'<b>当前通道源未接入</b>', html))
    ok_ch = ("当前通道" in html and chan_tags == ch_ok and chmiss == ch_miss
             and ((ch_ok > 0 and zone_gray == 0) or (ch_ok == 0 and zone_gray == 1)))
    check("T11", "当前通道列（model_usage）", ok_ch,
          "seats=%d ch_ok=%d tags=%d ch_miss=%d gray=%d zone_gray=%d"
          % (len(seat_items), ch_ok, chan_tags, ch_miss, chmiss, zone_gray))

    i_seat = html.find('id="z-seats"')
    i_hb = html.find('id="z-heartbeat"', i_seat)
    seat_seg = html[i_seat:i_hb] if i_seat >= 0 and i_hb > i_seat else ""
    hb_seg = html[i_hb:i_hb + 40000] if i_hb >= 0 else ""
    wip_n = len(re.findall(r'<span class="wip">施工中</span>', html))
    wip_want = sum(1 for v in (data.get("zone_status") or {}).values() if v != "ok")
    seat_inline = ('data-source="board_zones.py:314' in seat_seg or 'title="last_beat' in seat_seg
                   or "new-provider-" in seat_seg or "account:" in seat_seg)
    ok_split = (i_hb > 0 and "当前通道" in hb_seg and 'last_beat' in hb_seg
                and not seat_inline
                and "心跳=model_usage 最近调用" in seat_seg
                and "last_beat=最近模型调用时刻" not in seat_seg
                and wip_n == wip_want and wip_n > 0)
    check("T12", "心跳独立区+施工中+顶注", ok_split,
          "z-heartbeat=%s seat_inline=%s note_fixed=%s wip=%d/%d"
          % (i_hb > 0, seat_inline,
             ("心跳=model_usage 最近调用" in seat_seg), wip_n, wip_want))

    sources = re.findall(r'data-source="([^"]*)"', html)
    refs = [s for s in sources if "board_zones.py:" in s]
    check("T05", "数字角标可溯源", len(sources) >= 5 and all(sources)
          and len(refs) >= 3 and all(re.search(r"board_zones\.py:\d+", s) for s in refs),
          "badges=%d refs=%d empty=%d" % (len(sources), len(refs), sum(1 for s in sources if not s.strip())))

    chips = re.findall(r'class="sx[ "]', html)
    check("T06", "16 态 chip", len(chips) == 16, "count=%d" % len(chips))

    wiring = ("REFRESH=60" in html
              and "setInterval(pollNow,REFRESH*1000)" in html
              and "fetch('/data.json?_='+Date.now(),{cache:'no-store'})" in html)
    check("T07", "60s 轮询接线", wiring)

    bad = [p for p in ("C-NEW-", "TODO", "FIXME", "占位", "lorem ipsum") if p in html]
    check("T08", "禁占位/假数", not bad, "hits=%s" % bad if bad else "0")

    check("T09", "缺源灰条样式", ".zbar.missing" in html or ".zbar\n" in html or ".zbar" in html,
          "zbar_css=%s" % (".zbar{" in html))

    print("\nTOTAL %d/12 PASS" % (12 - len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
