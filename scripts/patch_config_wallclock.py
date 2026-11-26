# -*- coding: utf-8 -*-
"""Fix wallclock patch v2: the kernel reads flags from cfg["v4"] (load_v4_flags),
NOT from top level. v1 mistakenly set a top-level key -- remove it.

Sets v4.order_wallclock_sec = 5400 (3-round kernel orders need >2400s:
G34R1 burned ~2500s in rounds 1-2 and was killed at round-3 Gate1 with <60s left).
Backup .bak-20261124-wall already holds the pre-change file."""
import json, os

P = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\config\steward.json"
d = json.load(open(P, "r", encoding="utf-8"))

# remove the stray top-level key added by v1 (original file had none)
if "order_wallclock_sec" in d:
    d.pop("order_wallclock_sec", None)

v4 = d.get("v4")
if not isinstance(v4, dict):
    raise SystemExit("v4 section missing -- abort")
old = v4.get("order_wallclock_sec")
v4["order_wallclock_sec"] = 5400

tmp = P + ".tmp_wall2"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
os.replace(tmp, P)

chk = json.load(open(P, "r", encoding="utf-8"))
print("V4_WALLCLOCK old=%r new=%r top_level_key_present=%s" % (
    old, chk.get("v4", {}).get("order_wallclock_sec"), "order_wallclock_sec" in chk))
print("V4_KEYS", sorted((chk.get("v4") or {}).keys())[:12])
