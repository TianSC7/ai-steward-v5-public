# -*- coding: utf-8 -*-
"""Idempotent caddy guard for port 8900: if dead, clear stale caddy.exe and restart the task."""
import ctypes
import os
import socket
import subprocess
import sys
import time

PORT = 8900
TASK = "MES-TLS-8900"
LOG = r"C:\Users\Administrator\caddy_guard.log"


def alive():
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def log(msg):
    with open(LOG, "ab") as f:
        f.write((time.strftime("%m-%d %H:%M:%S ") + msg + "\n").encode("utf-8"))


def caddy_pids():
    out = subprocess.run(["tasklist", "/fi", "IMAGENAME eq caddy.exe", "/fo", "csv", "/nh"],
                         capture_output=True).stdout.decode("gbk", "ignore")
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "caddy.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def terminate(pid):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0001, False, pid)
    if not h:
        return False
    ok = k32.TerminateProcess(h, 1)
    k32.CloseHandle(h)
    return bool(ok)


def main():
    if alive():
        sys.exit(0)
    log("DEAD -> reviving")
    for pid in caddy_pids():
        log("terminate stale pid=%d -> %s" % (pid, terminate(pid)))
    time.sleep(1)
    subprocess.run(["schtasks", "/end", "/tn", TASK], capture_output=True)
    time.sleep(1)
    subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True)
    for _ in range(20):
        time.sleep(1)
        if alive():
            log("revive done alive=True")
            return
    log("revive FAILED alive=False")


if __name__ == "__main__":
    main()
