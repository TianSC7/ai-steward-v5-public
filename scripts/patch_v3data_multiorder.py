# -*- coding: utf-8 -*-
"""v3_data.py 多单化补丁（G18 病根修复：订单分区失明=数据层硬编码单订单）
锚点精确替换，改前备份 .bak-multiorder-20261122，改后 py_compile 语法闸。"""
import py_compile
import shutil

P = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\scripts\v3_data.py"
shutil.copyfile(P, P + ".bak-multiorder-20261122")
src = open(P, encoding="utf-8").read()
orig = src

def rep(old, new, cnt=1):
    global src
    n = src.count(old)
    assert n >= cnt, "ANCHOR NOT FOUND (%d): %r" % (n, old[:80])
    src = src.replace(old, new, cnt)

# R1 活单分桶
rep('    active_jobs = [j for j in (q.get("jobs") or []) if j.get("status") in ("running", "queued")]',
    '    active_jobs = [j for j in (q.get("jobs") or []) if j.get("status") in ("running", "queued")]\n'
    '    running_jobs = [j for j in active_jobs if j.get("status") == "running"]\n'
    '    queued_jobs = [j for j in active_jobs if j.get("status") == "queued"]')

# R2 主单选择：running 优先，主单信息真实化
rep('    is_running = len(active_jobs) > 0\n'
    '    cur_job = active_jobs[-1] if active_jobs else (hist[-1] if hist else {})',
    '    is_running = bool(running_jobs)\n'
    '    cur_job = running_jobs[0] if running_jobs else (queued_jobs[0] if queued_jobs else (hist[-1] if hist else {}))\n'
    '    cur_code = str(cur_job.get("order_id") or "C-20261121-001")\n'
    '    cur_title = str(cur_job.get("title") or f"V3首单：MES工厂面板优化（当前 {v3_ver} · 第{build_no}版）")')

# R3 states 真实分桶（16 态结构保留）
rep('    states_arr = []\n'
    '    for sk, scn in ALL_16_STATES:\n'
    '        if sk == ord_state:\n'
    '            states_arr.append({"k": sk, "cn": scn, "n": 1, "codes": ["C-20261121-001"]})\n'
    '        else:\n'
    '            states_arr.append({"k": sk, "cn": scn, "n": 0, "codes": []})',
    '    state_buckets = {}\n'
    '    for j in running_jobs:\n'
    '        state_buckets.setdefault("producing", []).append(str(j.get("order_id") or "?"))\n'
    '    for j in queued_jobs:\n'
    '        state_buckets.setdefault("queued", []).append(str(j.get("order_id") or "?"))\n'
    '    for h in hist:\n'
    '        k = {"done": "delivered", "needs_human": "needs_human"}.get(h.get("status"))\n'
    '        if k:\n'
    '            state_buckets.setdefault(k, []).append(str(h.get("order_id") or "?"))\n'
    '    states_arr = []\n'
    '    for sk, scn in ALL_16_STATES:\n'
    '        _codes = state_buckets.get(sk) or []\n'
    '        states_arr.append({"k": sk, "cn": scn, "n": len(_codes), "codes": _codes})')

# R4 order_item 硬编码换真实主单
rep('        "code": "C-20261121-001",\n'
    '        "title": f"V3首单：MES工厂面板优化（当前 {v3_ver} · 第{build_no}版）",',
    '        "code": cur_code,\n'
    '        "title": cur_title,')

# R5 queued 单生成订单卡（多单数组）
rep('    order_detail = {',
    '    orders_arr = [order_item]\n'
    '    for _j in queued_jobs:\n'
    '        _oc = str(_j.get("order_id") or "?")\n'
    '        orders_arr.append({\n'
    '            "code": _oc, "title": str(_j.get("title") or _oc), "state": "queued", "state_cn": "待排产",\n'
    '            "color": "#8d8d8d", "priority": "P1", "p0": False,\n'
    '            "node": {"t": "排队等待施工（自动调度）"}, "dwell_txt": "—", "task_n": 0, "task_m": 0,\n'
    '            "sub_n": 0, "sub_m": 0, "overdue": False, "delay_note": "", "pickup": "—",\n'
    '            "owner": "V3调度泵", "signature": "—",\n'
    '        })\n'
    '    order_detail = {')

# R6 order_detail 硬编码换真实
rep('        "code": "C-20261121-001",\n'
    '        "title": order_item["title"],',
    '        "code": cur_code,\n'
    '        "title": cur_title,')

# R7 kpis 真实计数
rep('            "run": 1 if is_running else 0,\n'
    '            "done": 0 if is_running else 1,\n'
    '            "queue": 0,\n'
    '            "blk": 0,',
    '            "run": len(running_jobs),\n'
    '            "done": len(done_runs),\n'
    '            "queue": len(queued_jobs),\n'
    '            "blk": len(state_buckets.get("needs_human") or []),')

# R8 orders/states/details 装真实
rep('        "orders": [order_item],\n'
    '        "states": states_arr,\n'
    '        "details": {"C-20261121-001": order_detail},',
    '        "orders": orders_arr,\n'
    '        "states": states_arr,\n'
    '        "details": {_oc: dict(order_detail, code=_oc, title=str(_j.get("title") or _oc))\n'
    '                    for _j in ([cur_job] + queued_jobs) for _oc in [str(_j.get("order_id") or "?")]},')

# R9 告警单号换真实（两处）
src = src.replace('"order": "C-20261121-001"', '"order": cur_code')

assert src != orig
open(P, "w", encoding="utf-8", newline="\n").write(src)
py_compile.compile(P, doraise=True)
print("PATCH_OK anchors applied, syntax OK")
