# -*- coding: utf-8 -*-
"""Queue promoter v3 (2026-11-24): move a chosen queued order to the front.

Guards (must all hold before ANY write):
  - zero running jobs
  - no live 'factory_runner.py tick' process (stale tick rewrites clobber the queue)
  - target queued and not already queued[0]

Usage: python queue_promote_once.py --once --target <ORDER_ID>
Verdicts: PROMOTED | TARGET_RUNNING | TARGET_GONE | WAITING | RACE_ABORT | ERROR ...
"""
import json, os, subprocess, sys, time

STATE = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\state"
Q = os.path.join(STATE, "factory_queue.json")
LOG = os.path.join(STATE, "promote_g36.log")

def target_id():
    for i, a in enumerate(sys.argv):
        if a == "--target" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return "C-20261124-G36R1"

def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass

def load():
    with open(Q, "r", encoding="utf-8") as fh:
        return json.load(fh)

def tick_alive():
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*factory_runner.py tick*' } | "
          "Measure-Object | Select-Object -ExpandProperty Count")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip().splitlines()
        return int(out[-1]) > 0 if out else True
    except Exception:
        return True  # conservative: assume busy

def one_pass(target):
    d = load()
    jobs = d.get("jobs") or []
    running = [j for j in jobs if j.get("status") == "running"]
    queued = [j for j in jobs if j.get("status") == "queued"]
    if any(j.get("order_id") == target for j in running):
        return "TARGET_RUNNING"
    if not any(j.get("order_id") == target for j in queued):
        return "TARGET_GONE"
    if queued[0].get("order_id") == target:
        return "PROMOTED"
    if running:
        return "WAITING"
    if tick_alive():
        return "WAITING"
    # all guards hold -> promote on the freshest copy, re-checking guards
    d2 = load()
    jobs2 = d2.get("jobs") or []
    run2 = [j for j in jobs2 if j.get("status") == "running"]
    q2 = [j for j in jobs2 if j.get("status") == "queued"]
    rest2 = [j for j in jobs2 if j.get("status") not in ("running", "queued")]
    tg2 = [j for j in q2 if j.get("order_id") == target]
    if run2:
        return "RACE_ABORT"
    if not tg2:
        return "TARGET_GONE"
    if q2[0].get("order_id") == target:
        return "PROMOTED"
    if tick_alive():
        return "RACE_ABORT"
    before = [j.get("order_id") for j in q2]
    d2["jobs"] = tg2 + [j for j in q2 if j is not tg2[0]] + rest2
    tmp = Q + ".tmp_promote"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d2, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, Q)
    chk = load()
    ids = [j.get("order_id") for j in (chk.get("jobs") or []) if j.get("status") == "queued"]
    if ids and ids[0] == target:
        log("PROMOTED %s ok. before=%s after=%s" % (target, before, ids))
        return "PROMOTED"
    log("write landed but order mismatch: %s" % (ids,))
    return "WAITING"

def main():
    target = target_id()
    if "--once" in sys.argv:
        try:
            r = one_pass(target)
        except Exception as e:
            r = "ERROR %r" % (e,)
        print(r)
        return 0
    deadline = time.time() + 2.5 * 3600
    log("promoter loop start, target=%s" % target)
    while time.time() < deadline:
        try:
            r = one_pass(target)
        except Exception as e:
            r = "ERROR %r" % (e,)
        log(r)
        if r in ("PROMOTED", "TARGET_RUNNING", "TARGET_GONE"):
            break
        time.sleep(15)
    log("promoter loop end")
    return 0

if __name__ == "__main__":
    sys.exit(main())
