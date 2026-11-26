# -*- coding: utf-8 -*-
"""统一测试跑手：版本指纹自检 + 逐项超时保护 + 严格退出码断言。

指纹 = git 短哈希 + 每个被测脚本 sha256 前 8 位；任一项 rc!=0 或超时即整体失败。
用法：python -X utf8 scripts/run_tests.py [--timeout 180] [--json]
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CASES = [
    ("test_board", os.path.join(HERE, "test_board.py")),
    ("test_cycle", os.path.join(HERE, "test_cycle.py")),
    ("test_board_concurrency", os.path.join(HERE, "tests", "test_board_concurrency.py")),
    # C-20261120-004 新增（R-10：新测试必须登记进 CASES，否则等于没跑）
    ("test_mes_orders", os.path.join(HERE, "tests", "test_mes_orders.py")),
    ("test_detail", os.path.join(HERE, "tests", "test_detail.py")),
    ("test_gantt", os.path.join(HERE, "tests", "test_gantt.py")),
    # C-20261120-005 T04：§2.1 流程表 + 执行员工/会话/签字 + 账实派生（R-10 登记）
    ("test_flow", os.path.join(HERE, "tests", "test_flow.py")),
    # C-20261120-008 R1.1：七表派生库 + F10 工牌/写通道（R-10 登记）
    ("test_mes_core", os.path.join(HERE, "test_mes_core.py")),
    ("test_mes_f10", os.path.join(HERE, "test_mes_f10.py")),
    # C-20261120-008 R1.2：状态推导 R1–R9 边界（R-10 登记）
    ("test_mes_state_rules", os.path.join(HERE, "test_mes_state_rules.py")),
    # 2026-11-24 merge-guard 修复回归（G41a：未合并不删 worktree / 不删未声明交付 / Gate1 认已提交 diff）
    ("test_merge_guard", os.path.join(HERE, "test_merge_guard.py")),
    # 2026-11-24 P4b 并行道回归（空槽扇出计划 / 终态单归档 / 预算账本并发零丢写）
    ("test_p4b_ops", os.path.join(HERE, "test_p4b_ops.py")),
    # 2026-11-25 MES「并发数」页数据源（cap/已用/月包/GLM 不可用/未归属）
    ("test_lane_status", os.path.join(HERE, "test_lane_status.py")),
    # 2026-11-26 C-20261126-SPD1 派工单「少往返工作法」常量与拼入顺序（R-10 登记）
    ("test_build_method_note", os.path.join(HERE, "test_build_method_note.py")),
]


def sha8(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:8]
    except OSError:
        return "missing"


def fingerprint():
    try:
        g = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        head = g.stdout.strip() or "nogit"
    except OSError:
        head = "nogit"
    parts = ["ai-steward@" + head]
    for name, path in CASES:
        parts.append("%s=%s" % (name, sha8(path)))
    return " | ".join(parts)


def fail_lines(out, limit=10, width=300):
    """从用例输出里挑出断点行（[FAIL] / [MISSING] / Traceback / AssertionError），给内核硬闸的返工话用。

    只报「某某用例失败」等于让工人自己从头找断点。挑不到断点行时退回输出末尾 6 行。"""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    lines = [ln.strip() for ln in str(out or "").replace("\r\n", "\n").splitlines() if ln.strip()]
    picked = [ln for ln in lines
              if ln.startswith(("[FAIL]", "[MISSING]", "[TIMEOUT]")) or ln.startswith("Traceback")
              or "AssertionError" in ln or ln.startswith("assert ")]
    if not picked:
        picked = lines[-6:]
    return [ln[:width] for ln in picked[:limit]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fp = fingerprint()
    print("FP", fp, flush=True)
    results = []
    t0 = time.time()
    for name, path in CASES:
        if not os.path.isfile(path):
            results.append({"case": name, "rc": None, "sec": 0.0, "status": "MISSING"})
            print("[MISSING]", name, flush=True)
            continue
        start = time.time()
        out = b""
        try:
            p = subprocess.run([sys.executable, "-X", "utf8", path], capture_output=True, timeout=args.timeout)
            rc = p.returncode
            status = "PASS" if rc == 0 else "FAIL"
            out = p.stdout + p.stderr
        except subprocess.TimeoutExpired as e:
            rc = None
            status = "TIMEOUT"
            out = e.stdout or b""
        sec = round(time.time() - start, 1)
        row = {"case": name, "rc": rc, "sec": sec, "status": status}
        if status != "PASS":
            row["fails"] = fail_lines(out)        # 断点行进汇总，内核硬闸据此给工人指路
        results.append(row)
        print("[%s] %s rc=%s %.1fs" % (status, name, rc, sec), flush=True)
    bad = [r for r in results if r["status"] != "PASS"]
    total = len(results)
    summary = {"fp": fp, "total": total, "passed": total - len(bad),
               "failed": len(bad), "cases": results, "elapsed_sec": round(time.time() - t0, 1)}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print("ALL PASS (%d/%d) %.1fs" % (total - len(bad), total, summary["elapsed_sec"])
              if not bad else "FAILED (%d/%d): %s" % (total - len(bad), total, [b["case"] for b in bad]))
    # 严格退出码：任一项非 PASS 即非零
    raise SystemExit(0 if not bad else 2)


if __name__ == "__main__":
    main()
