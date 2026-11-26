# -*- coding: utf-8 -*-
"""修复 Factory-Autopilot-Pump：TR 参数被 PS 拆吃导致 0x80070002。
方案：where python 拿绝对路径 -> 生成 pump_run.cmd 启动器 -> 删旧任务重建 /TR 指向 .cmd -> 立即触发验证。
"""
import os
import subprocess

ROOT = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
SCRIPT = os.path.join(ROOT, "scripts", "autopilot_pump.py")
CMD = os.path.join(ROOT, "scripts", "pump_run.cmd")

# 1) python 绝对路径
w = subprocess.run(["where", "python"], capture_output=True).stdout.decode("gbk", "ignore").strip().splitlines()
py = next((p.strip('"') for p in w if p.strip()), "python")
print("PY_PATH", py)

# 2) 启动器
with open(CMD, "w", encoding="ascii", newline="\r\n") as f:
    f.write('@"%s" -X utf8 "%s"\n' % (py, SCRIPT))
print("CMD_WRITTEN", CMD)

# 3) 重建任务
subprocess.run(["schtasks", "/Delete", "/tn", "Factory-Autopilot-Pump", "/f"], capture_output=True)
r = subprocess.run(["schtasks", "/Create", "/f", "/tn", "Factory-Autopilot-Pump",
                    "/tr", CMD, "/sc", "minute", "/mo", "5"], capture_output=True)
print("CREATE", r.returncode, r.stdout.decode("gbk", "ignore").strip()[-80:])

# 4) 立即触发 + 回读
subprocess.run(["schtasks", "/Run", "/tn", "Factory-Autopilot-Pump"], capture_output=True)
q = subprocess.run(["schtasks", "/query", "/tn", "Factory-Autopilot-Pump", "/fo", "list", "/v"],
                   capture_output=True).stdout.decode("gbk", "ignore")
for ln in q.splitlines():
    s = ln.strip()
    if any(k in s for k in ("要运行的任务", "上次运行", "状态")):
        print("V:", s[:130])
