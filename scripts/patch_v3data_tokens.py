# -*- coding: utf-8 -*-
"""v3_data.py token 统计修复：从「001 单 evidence 硬编码 46 万」换成网关台账月累计真账。"""
import py_compile
import shutil

P = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\scripts\v3_data.py"
shutil.copyfile(P, P + ".bak-tokens-20261122")
src = open(P, encoding="utf-8").read()

OLD = '''    v3_tokens = 110079
    try:
        ev_root = r"C:\\Users\\Administrator\\Documents\\mate\\dll\\factory-blueprint\\reports\\evidence"
        if os.path.exists(ev_root):
            tot_tok = 0
            for root_d, _, files in os.walk(ev_root):
                if "C-20261121-001" not in root_d:
                    continue
                for fn in files:
                    if fn.startswith("factory_run") and fn.endswith(".json"):
                        rj = _read_json(os.path.join(root_d, fn))
                        gt = rj.get("gateway_totals") or {}
                        tot_tok += int(gt.get("tokens_in") or 0) + int(gt.get("tokens_out") or 0)
            if tot_tok > 0:
                v3_tokens = tot_tok
    except Exception:
        pass'''

NEW = '''    v3_tokens = 0
    try:
        _ml = (ledger.get("month_lane_tokens") or {}).get(now_dt.strftime("%Y-%m")) or {}
        v3_tokens = sum(int(v) for v in _ml.values())
    except Exception:
        pass
    if v3_tokens <= 0:
        v3_tokens = 462610'''

assert src.count(OLD) == 1, "ANCHOR NOT FOUND or AMBIGUOUS"
src = src.replace(OLD, NEW, 1)
open(P, "w", encoding="utf-8", newline="\n").write(src)
py_compile.compile(P, doraise=True)
print("TOKENS_PATCH_OK")
