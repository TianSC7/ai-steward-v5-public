# -*- coding: utf-8 -*-
"""Print one line per active queue job: order_id|status"""
import json

Q = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\state\factory_queue.json"
d = json.load(open(Q, "r", encoding="utf-8"))
for j in d.get("jobs") or []:
    print("%s|%s" % (j.get("order_id"), j.get("status")))
