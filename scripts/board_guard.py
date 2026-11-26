# -*- coding: utf-8 -*-
# board_guard: every minute - regenerate MES board (pull channel) + keep the BOARD server (8901) healthy
# v3 2026-11-25: fix watch target. v2 watched :8900 which is Caddy (public entry), so the
# board itself was never actually guarded and the kill branch aimed at the wrong process.
# v3 watches :8901 (board.json port, loopback). Caddy stays CaddyGuard8900's job.
import os
import subprocess
import sys
import time
import urllib.request

ROOT = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
SCRIPTS = os.path.join(ROOT, "scripts")
BOARD_DIR = os.path.join(ROOT, "dashboard")
SCRIPT = os.path.join(SCRIPTS, "mes_board.py")
SERVER = os.path.join(SCRIPTS, "board_server.py")
PORT = "8901"

# 1) static fallback board (for SSH pull channel) - unchanged from v2
subprocess.run([sys.executable, "-X", "utf8", SCRIPT],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# 1b) keepalive alarm clock self-heal (four checks + one dial) - unchanged from v2
try:
    subprocess.run([sys.executable, "-X", "utf8", os.path.join(SCRIPTS, "verify_keepalive.py")],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
except Exception:
    pass


def listening():
    ns = subprocess.run(["netstat", "-ano"], capture_output=True).stdout.decode("gbk", "replace")
    for l in ns.splitlines():
        if ":" + PORT in l and "LISTENING" in l:
            try:
                return int(l.split()[-1])
            except (ValueError, IndexError):
                return -1
    return None


def healthy():
    try:
        r = urllib.request.urlopen("http://127.0.0.1:" + PORT + "/healthz", timeout=4)
        return r.status == 200 and len(r.read()) > 0
    except Exception:
        return False


pid = listening()
if pid is None:
    subprocess.Popen([sys.executable, "-X", "utf8", SERVER],
                     creationflags=0x00000008,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
elif not healthy():
    # port listening but requests fail/hang: kill the board pid (8901 owner), restart
    if pid > 0:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        time.sleep(1)
    subprocess.Popen([sys.executable, "-X", "utf8", SERVER],
                     creationflags=0x00000008,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
