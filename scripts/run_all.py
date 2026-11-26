# -*- coding: utf-8 -*-
"""总闸：一条命令跑完管家系统的全部验收。

包含：
  0. 巡检边界（test_scan.py）—— fresh/stale/missing/坏配置 判定
  1. 边界用例（test_edges.py）—— 确定性、零 token，覆盖失败分类/粘性/空流/gzip/令牌
  2. 网关自检（selftest.py）—— 健康/别名/通道/故障注入
  2b. 网关粘性（test_gateway_sticky.py）—— 兜底 lane 不参与粘性提升（policy.sticky_exclude）5 例
  3. 巡检取证（steward_scan.py --json）—— 输出必须是合法 JSON 且含 projects/sessions
  4. 插件出厂校验（validate_plugin.py）
  5. 成本取证（usage_facts.py；usage-bar 缺席时降级为 PASS+说明）
  6. 台账边界（test_ledger.py）—— 状态机/证据文件/哈希/中断留痕 10 例
  7. 工具边界（test_tools.py）—— 执行句柄/全权脚本/自动放行钩子 10 例
  8. 定时健康（test_cron_health.py）—— 自动化停摆检测 11 例
  9. 任务台（test_board.py）—— 状态机/证据门/重试/原子写/workspace 补全/claim 边界 24 例
  9b. 交接快照（test_handoff.py）—— 空台账/在办/坏时间戳/五段齐全/卡号可点/只读/成本 19 例
  10. 调度引擎（test_cycle.py）—— 并发/依赖/升级链/预算/证据即验/纪律三闸 50 例
  10b. 派单可见性（test_dispatch_verify.py）—— 幽灵会话探针/tasks 行/输出判据/stalled/replace 动作 12 例
  11. 私人知识库（test_kb.py）—— 事实卡/检索/索引 14 例
  11b. 知识卡体检（test_kb_lint.py）—— 坏卡/重复/过期/superseded 链 25 例
  12. 密钥保险库（test_key_vault.py）—— DPAPI 往返/明文不落盘/不可删 19 例
  13. 隔离工作区（test_worktree.py）—— 建/复用/并行隔离/合并/冲突回滚/清理 41 例
  13b. 自理闹钟与归档（test_cron_selfwake.py）—— 闹钟幂等建/校、拒死会话、registry 指针、会话归档 13 例
  14. pytest 套件（scripts/tests）—— ai_store/ai_locks/board 并发/worker 纪律三闸 41 例
  15. 台账（state/registry.json）—— 结构合法、done 有证据且哈希一致

用法：
  python -X utf8 run_all.py            # 全量
  python -X utf8 run_all.py --fast     # 跳过故障注入
退出码 0 = 全绿；非 0 = 有失败（可直接给管家当门禁）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402
from ledger_check import check as ledger_check  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable


def run(name: str, args: list, timeout: float = 300, require_fp: bool = False) -> tuple[bool, str, str, str]:
    """返回 (ok, 摘要, 完整stdout, stderr)。摘要只用于展示，解析必须用完整输出。

    require_fp=True 时，子进程 stderr 必须含 `FP ` 指纹行且 env 不是 BAD——
    防止"跑的是旧副本/旧环境"导致假 PASS。
    """
    try:
        r = subprocess.run([PY, "-X", "utf8"] + args, cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时 %.0fs" % timeout, "", ""
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    tail = out.splitlines()[-3:] or err.splitlines()[-1:]
    ok = r.returncode == 0
    detail = (" | ".join(tail) if tail else "")[:300]
    if require_fp:
        if "FP " not in err:
            ok, detail = False, "缺版本指纹（stderr 无 FP 行） | " + detail
        elif "env=BAD" in err:
            ok, detail = False, "子进程指纹自检 BAD | " + detail
    return ok, detail, out, err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    args = ap.parse_args()

    fp = announce("run_all")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(1800, "run_all")

    results: list[tuple[bool, str, str]] = []

    ok, d, _, _ = run("scan_edges", [os.path.join(HERE, "test_scan.py")], require_fp=True)
    results.append((ok, "巡检边界", d))

    ok, d, _, _ = run("edges", [os.path.join(HERE, "test_edges.py")], require_fp=True)
    results.append((ok, "边界用例", d))

    self_args = [os.path.join(HERE, "selftest.py")] + (["--skip-fault"] if args.fast else [])
    ok, d, _, _ = run("selftest", self_args, require_fp=True)
    results.append((ok, "网关自检", d))

    ok, d, _, _ = run("gateway_sticky", [os.path.join(HERE, "test_gateway_sticky.py")], timeout=120)
    results.append((ok, "网关粘性", d))

    ok, d, full, _ = run("scan", [os.path.join(HERE, "steward_scan.py"), "--json", "--no-gateway", "--no-usage"],
                         require_fp=True)
    if ok:
        try:
            data = json.loads(full)
        except ValueError:
            ok, d = False, "steward_scan 输出不是合法 JSON（可能被日志污染）"
        else:
            if "projects" not in data or "sessions" not in data:
                ok, d = False, "steward_scan 输出缺 projects/sessions"
            elif not data.get("projects"):
                ok, d = False, "steward_scan 返回 0 个项目（空清单不得当通过）"
            else:
                ok, d = True, "projects=%d sessions=%d" % (len(data["projects"]), len(data["sessions"]))
    results.append((ok, "巡检取证", d))

    ok, d, _, _ = run("plugin", [os.path.join(HERE, "validate_plugin.py")], require_fp=True)
    results.append((ok, "插件出厂", d))

    ok, d, full, _ = run("usage", [os.path.join(HERE, "usage_facts.py"), "--json"], require_fp=True)
    if ok:
        try:
            u = json.loads(full)
            ok = True
            t = u.get("today") or {}
            g = u.get("gateway") or {}
            if u.get("available"):
                d = "今日 ¥%s | 请求 %s" % (t.get("cost_cny"), t.get("requests"))
            else:
                d = "usage-bar 不可用（降级）：" + str(u.get("reason"))[:80]
            if g.get("up"):
                if "cost_cny" not in g:
                    ok, d = False, "网关在线但 /admin/status 缺成本字段（成本核算回归）"
                else:
                    d += " | 网关累计 ¥%s（%s 次，未定价 %s）" % (
                        g.get("cost_cny"), g.get("requests"), g.get("unpriced"))
        except ValueError:
            ok, d = False, "usage_facts 输出不是合法 JSON"
    results.append((ok, "成本取证", d))

    ok, d, full, _ = run("budget", [os.path.join(HERE, "budget_report.py"), "--json", "--days", "1"], require_fp=True)
    if ok:
        try:
            b = json.loads(full)
            if not b.get("available"):
                ok, d = False, "budget_report 报不可用（usage-bar 缺席未降级）"
            elif "by_task" not in b or "gateway" not in b or "total_cny" not in b:
                ok, d = False, "budget_report 缺 by_task/gateway/total_cny 字段"
            else:
                gw = b.get("gateway") if isinstance(b.get("gateway"), dict) else {}
                d = "今日 ¥%.2f | 任务桶 %d | 网关 tier 桶 %d" % (
                    float(b.get("total_cny") or 0), len(b.get("by_task") or {}),
                    len(gw.get("by_tier") or {}))
        except ValueError:
            ok, d = False, "budget_report 输出不是合法 JSON"
    results.append((ok, "预算分桶", d))

    ok, d, _, _ = run("ledger_edges", [os.path.join(HERE, "test_ledger.py")], timeout=120, require_fp=True)
    results.append((ok, "台账边界", d))

    ok, d, _, _ = run("tool_edges", [os.path.join(HERE, "test_tools.py")], timeout=180, require_fp=True)
    results.append((ok, "工具边界", d))

    ok, d, _, _ = run("cron_edges", [os.path.join(HERE, "test_cron_health.py")], timeout=120, require_fp=True)
    results.append((ok, "定时健康", d))

    ok, d, _, _ = run("board_edges", [os.path.join(HERE, "test_board.py")], timeout=120, require_fp=True)
    results.append((ok, "任务台", d))

    ok, d, _, _ = run("handoff_snapshot", [os.path.join(HERE, "test_handoff.py")], timeout=120)
    results.append((ok, "交接快照", d))

    ok, d, _, _ = run("cycle_edges", [os.path.join(HERE, "test_cycle.py")], timeout=120, require_fp=True)
    results.append((ok, "调度引擎", d))

    ok, d, _, _ = run("dispatch_verify", [os.path.join(HERE, "test_dispatch_verify.py")], timeout=120, require_fp=True)
    results.append((ok, "派单可见性", d))

    ok, d, _, _ = run("kb_edges", [os.path.join(HERE, "test_kb.py")], timeout=120, require_fp=True)
    results.append((ok, "私人知识库", d))

    ok, d, _, _ = run("kb_lint_edges", [os.path.join(HERE, "test_kb_lint.py")], timeout=120, require_fp=True)
    results.append((ok, "知识卡体检", d))

    ok, d, _, _ = run("vault_edges", [os.path.join(HERE, "test_key_vault.py")], timeout=120, require_fp=True)
    results.append((ok, "密钥保险库", d))

    ok, d, _, _ = run("worktree_edges", [os.path.join(HERE, "test_worktree.py")], timeout=300, require_fp=True)
    results.append((ok, "隔离工作区", d))

    ok, d, _, _ = run("selfwake_edges", [os.path.join(HERE, "test_cron_selfwake.py")], timeout=180, require_fp=True)
    results.append((ok, "自理闹钟与归档", d))

    # pytest 套件（scripts/tests）：ai_store/ai_locks/board 并发/worker 纪律三闸。
    # 子进程是 pytest 本身，不打印 FP 行，故 require_fp=False（其余门禁仍强制指纹）。
    ok, d, _, _ = run("pytest_tests", ["-m", "pytest", "scripts/tests", "-q"], timeout=300)
    results.append((ok, "pytest 套件", d))

    reg_path = os.path.join(ROOT, "state", "registry.json")
    ok, d = ledger_check(reg_path, ROOT)
    results.append((ok, "台账", d))

    for ok, name, detail in results:
        print("[%s] %-10s %s" % ("PASS" if ok else "FAIL", name, detail))
    failed = [r for r in results if not r[0]]
    print("\n%d/%d 通过" % (len(results) - len(failed), len(results)))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
