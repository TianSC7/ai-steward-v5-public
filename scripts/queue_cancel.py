# -*- coding: utf-8 -*-
"""Cancel queued orders in factory_queue.json (queued -> cancelled), atomic.
Usage: python queue_cancel.py <ORDER_ID> [<ORDER_ID> ...]
Refuses to touch anything with status=running (kill the tick first)."""
import json, os, sys, time

Q = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\state\factory_queue.json"
LOG = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\state\promote_g36.log"

def log(msg):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))

force = "--force" in sys.argv
targets = set(a for a in sys.argv[1:] if not a.startswith("--"))
d = json.load(open(Q, "r", encoding="utf-8"))
jobs = d.get("jobs") or []
done, refused = [], []
for j in jobs:
    oid = j.get("order_id")
    if oid in targets:
        st = j.get("status")
        if st == "queued" or (force and st == "running"):
            j["status"] = "cancelled"
            j["cancel_reason"] = "supervisor: G35 premise broken (G34 family needs_human, no lanes on main); G36R2 bad repo field"
            done.append(oid)
        else:
            refused.append((oid, st))
tmp = Q + ".tmp_cancel"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
os.replace(tmp, Q)
chk = json.load(open(Q, "r", encoding="utf-8"))
state = [(j.get("order_id"), j.get("status")) for j in chk.get("jobs") or []]
print("CANCEL_DONE", done, "REFUSED", refused)
print("QUEUE_NOW", state)
log("cancel done=%s refused=%s queue=%s" % (done, refused, state))
