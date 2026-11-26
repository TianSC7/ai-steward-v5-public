# -*- coding: utf-8 -*-
"""一次性：hooks.json 的 python 路径 TIAN->AA（带备份）。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
p = os.path.join(ROOT, "hooks", "hooks.json")
OLD = "C:\\Users\\TIAN\\AppData\\Local\\Programs\\Python\\Python313\\python.exe"
NEW = "C:\\Program Files\\Python313\\python.exe"

with open(p, encoding="utf-8") as f:
    d = json.load(f)
n = 0
for ev, mats in d["hooks"].items():
    for m in mats:
        for h in m["hooks"]:
            if h.get("command") == OLD:
                h["command"] = NEW
                n += 1
if n == 0:
    print("no TIAN python ref found (already fixed?)")
    sys.exit(0)
bak = p + ".bak-20261118aa"
if not os.path.exists(bak):
    with open(bak, "w", encoding="utf-8", newline="") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print("backup:", bak)
tmp = p + ".tmp"
with open(tmp, "w", encoding="utf-8", newline="") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
os.replace(tmp, p)
print("repointed %d hook commands -> %s" % (n, NEW))
