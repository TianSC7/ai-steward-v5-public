#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""concurrency_randprobe.py — 随机间隔并发采样器（用户 2026-11-19 07:0x 令）

规则：**不得定时**。每次采样后随机等待 1–8 分钟再做下一次，避免与 5 分钟巡检
同相位、永远采到高并发场景。统计窗口 = 距上次采样的实际间隔（分钟，向上取整）。

实现：常驻进程，循环 sample → sleep(uniform(60,480)s)。日志：
  - state/concurrency.jsonl（每次追加 JSON，含 interval_sec 与 window_min）
  - state/CONCURRENCY_LOG.md（追加一行 markdown）
用法：
  python -X utf8 scripts/concurrency_randprobe.py            # 常驻
  python -X utf8 scripts/concurrency_randprobe.py --once     # 只采一次（调试）
"""
import argparse
import math
import os
import random
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "scripts", "concurrency_probe.py")
SLOT = os.path.join(ROOT, "scripts", "slot_pool.py")


def sample(window_min):
    subprocess.run([sys.executable, "-X", "utf8", PROBE, "--window", str(max(1, window_min))],
                   cwd=ROOT, check=False)
    # 劳务派遣监督池：与并发采样同窗同频，实时记录各道席位占用（用户 07:3x 令）
    subprocess.run([sys.executable, "-X", "utf8", SLOT, "--active-min", str(max(1, window_min))],
                   cwd=ROOT, check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--min-sec", type=int, default=60)
    ap.add_argument("--max-sec", type=int, default=480)
    args = ap.parse_args()

    last = time.time()
    sample(1)  # 启动即采一次（窗口 1 分钟）
    if args.once:
        return

    while True:
        wait = random.randint(args.min_sec, args.max_sec)
        time.sleep(wait)
        gap = max(1, math.ceil((time.time() - last) / 60.0))
        sample(gap)
        last = time.time()
        # 记录本次随机间隔，便于审计随机性
        with open(os.path.join(ROOT, "state", "concurrency_rand.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} wait={wait}s window={gap}min\n")


if __name__ == "__main__":
    main()
