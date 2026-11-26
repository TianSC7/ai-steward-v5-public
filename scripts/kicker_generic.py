# -*- coding: utf-8 -*-
"""Detached tick kicker via schtasks - escapes Win32-OpenSSH job-object kill on disconnect.
(DETACHED_PROCESS only detaches the console, NOT the ssh job object; ticks kicked that way
die silently when the ssh session closes. schtasks processes are spawned by the Task
Scheduler service and survive.)
Usage: python kicker_generic.py <order_id>"""
import os
import subprocess as sp
import sys

SCRIPTS = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\scripts"
STATE = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward\state"
order = sys.argv[1]
run_cmd = os.path.join(SCRIPTS, "run_tick_%s.cmd" % order)
LOG = os.path.join(STATE, "tick_%s_console.log" % order)

with open(run_cmd, "w", encoding="ascii", newline="\r\n") as f:
    f.write("@echo off\r\n")
    f.write("cd /d %s\r\n" % SCRIPTS)
    f.write("set PYTHONUNBUFFERED=1\r\n")
    f.write("python -X utf8 factory_runner.py tick --order %s > %s 2>&1\r\n" % (order, LOG))

tn = "v3tick_%s" % order
r1 = sp.run(["schtasks", "/Create", "/TN", tn, "/TR", run_cmd, "/SC", "ONCE", "/ST", "23:59", "/F"],
            capture_output=True, text=True, timeout=30)
if r1.returncode != 0:
    print("CREATE_FAIL", (r1.stderr or r1.stdout or "").strip()[-200:])
    sys.exit(1)
r2 = sp.run(["schtasks", "/Run", "/TN", tn], capture_output=True, text=True, timeout=30)
print("RUN_RC", r2.returncode, (r2.stdout or "").strip()[-120:], (r2.stderr or "").strip()[-120:])
print("log:", LOG)
