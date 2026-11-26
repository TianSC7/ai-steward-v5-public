# -*- coding: utf-8 -*-
"""网关生命周期管理：起停/状态/开机自启（Windows 用户级启动项，免管理员）

用法：
  python -X utf8 gateway_ctl.py status
  python -X utf8 gateway_ctl.py start | stop | restart
  python -X utf8 gateway_ctl.py install     # 登录即静默启动
  python -X utf8 gateway_ctl.py uninstall
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG = os.path.join(ROOT, "config", "routing.json")
LOG_DIR = os.path.join(ROOT, "logs")
STARTUP = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
VBS = os.path.join(STARTUP, "AI-Steward-Gateway.vbs")
NL = chr(10)


def port(cfg_path: str = CFG) -> int:
    try:
        return int(json.load(open(cfg_path, encoding="utf-8")).get("listen", {}).get("port", 8788))
    except (OSError, ValueError):
        return 8788


def http(path: str, timeout: float = 4):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port(), path), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def find_pid() -> int:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in out.splitlines():
        if (":%d" % port()) in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return 0


def pythonw() -> str:
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def start() -> int:
    if http("/health"):
        print("已在运行（端口 %d）" % port())
        return 0
    os.makedirs(LOG_DIR, exist_ok=True)
    out = open(os.path.join(LOG_DIR, "gateway.stdout.log"), "a", encoding="utf-8")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([pythonw(), "-X", "utf8", os.path.join(HERE, "gateway.py"), "--config", CFG],
                            stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                            creationflags=flags, close_fds=True)
    for _ in range(40):
        if http("/health", timeout=1):
            print("已启动 pid=%s 端口=%d" % (proc.pid, port()))
            return 0
        time.sleep(0.25)
    print("启动失败，看 logs/gateway.stdout.log")
    return 2


def stop() -> int:
    pid = find_pid()
    if not pid:
        print("未在运行")
        return 0
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    time.sleep(0.5)
    print("已停止 pid=%d" % pid)
    return 0


def status() -> int:
    s = http("/admin/status", timeout=6)
    if not s:
        print("离线（端口 %d 无响应）" % port())
        return 2
    t = s.get("totals", {})
    print("在线 v%s uptime=%ss 请求=%s ok=%s fail=%s tokens_in=%s tokens_out=%s"
          % (s.get("version"), s.get("uptime_sec"), t.get("requests"), t.get("ok"),
             t.get("fail"), t.get("tokens_in"), t.get("tokens_out")))
    for l in s.get("lanes", []):
        err = (" !" + l["last_error"][:40]) if l.get("last_error") and l.get("fail") else ""
        print("  %-24s %-10s %-28s ok=%-4s fail=%-3s avg=%-7s cool=%ss%s"
              % (l["id"], l["provider"], l["model"], l.get("ok"), l.get("fail"),
                 (str(l.get("avg_ms")) + "ms") if l.get("avg_ms") else "-",
                 l.get("cooldown_left_sec"), err))
    return 0


def install() -> int:
    """写用户级启动项：VBS 静默拉起，不需要管理员权限。"""
    os.makedirs(STARTUP, exist_ok=True)
    cmd = '"%s" -X utf8 "%s" --config "%s"' % (pythonw(), os.path.join(HERE, "gateway.py"), CFG)
    lines = [
        'Set sh = CreateObject("WScript.Shell")',
        'sh.CurrentDirectory = "%s"' % ROOT.replace('"', '""'),
        'sh.Run "%s", 0, False' % cmd.replace('"', '""'),
    ]
    with open(VBS, "w", encoding="utf-8-sig") as f:
        f.write(NL.join(lines) + NL)
    print("已安装开机自启: " + VBS)
    return 0


def uninstall() -> int:
    if os.path.exists(VBS):
        os.remove(VBS)
        print("已移除开机自启: " + VBS)
    else:
        print("未安装开机自启")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "stop", "restart", "status", "install", "uninstall"])
    a = ap.parse_args()
    if a.cmd == "restart":
        stop()
        return start()
    return {"start": start, "stop": stop, "status": status,
            "install": install, "uninstall": uninstall}[a.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
