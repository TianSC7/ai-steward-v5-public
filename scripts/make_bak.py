# -*- coding: utf-8 -*-
"""Create pre-patch backup of factory_runner.py from git HEAD (2026-11-24)."""
import os
import subprocess

REPO = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
SRC = os.path.join(REPO, "scripts", "factory_runner.py")
BAK = os.path.join(REPO, "scripts", "factory_runner.py.bak-20261124-preevid")

data = subprocess.check_output(["git", "-C", REPO, "show", "HEAD:scripts/factory_runner.py"])
open(BAK, "wb").write(data)
cur = open(SRC, "rb").read()
print("BAK bytes=%d  current bytes=%d  same=%s" % (len(data), len(cur), data == cur))
print("BAK lines=%d  current lines=%d" % (data.count(b"\n") + 1, cur.count(b"\n") + 1))
