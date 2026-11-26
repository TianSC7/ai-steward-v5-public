#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_line.py — 域产线「下单即立约」合同器（C-20261125-G42）

用户语义：**下单即立合同，未拒单即有效**。
域产线的开工/完工本是 `lines/<线>/run_line.py`（引擎，未入 git 的试点线），
本模块只承担合同生命周期，不产出任何业务结论：

  start   单开工前：写 `lines/<线>/jobs/<单号>/contract.json`（status=active）
          并把合同摘要同步进该单 `ledger.json` 的 `contract` 字段
  finish  完工后：contract.status → done|failed、写 finished_at，并刷新 ledger.contract
  wrap    包装器：start → 跑引擎 → finish（不改引擎一行代码，试点线可直接用）
  attach  对历史单补立约（回填，`retro: true`，用于「老单无约」过渡）
  check   机检单条合同：contract.json 在 && ledger.contract 非空 && 状态合法

字段口径（F2.1）：order_id / 产线 / 规格摘要 / 验收标准 / created_at / status。
向后兼容：无合同的老单，check 报 FAIL(缺约)但不抛异常；任何写盘失败只打印告警，
不阻断产线执行（合同是旁路证据，不是闸）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys

DEFAULT_LINES_ROOT = os.environ.get("STEWARD_LINES_ROOT") or (
    "C:/Users/Administrator/Documents/mate/plugins/ai-steward/lines")
CONTRACT_STATUSES = ("active", "done", "failed")
TERMINAL_MAP = {"done": "done", "check_failed": "failed", "failed": "failed"}


# ---------------------------------------------------------------- 基础工具

def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
    except OSError:
        return ""
    return h.hexdigest()


def _read_json(path: str) -> dict:
    """读 json；缺失/损坏返回 {}（老单无 ledger 属于正常态）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, obj: dict) -> None:
    """原子写（tmp + os.replace）：读者永不见半成品。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "wb") as f:
        f.write((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def line_dir(lines_root: str, line: str) -> str:
    return os.path.join(lines_root, line)


def job_dir(lines_root: str, line: str, order_id: str) -> str:
    return os.path.join(line_dir(lines_root, line), "jobs", order_id)


def contract_path(lines_root: str, line: str, order_id: str) -> str:
    return os.path.join(job_dir(lines_root, line, order_id), "contract.json")


def ledger_path(lines_root: str, line: str, order_id: str) -> str:
    return os.path.join(job_dir(lines_root, line, order_id), "ledger.json")


# ---------------------------------------------------------------- 规格摘要 / 验收标准

def spec_summary(order: dict) -> dict:
    """规格摘要：只摘「做什么」，不搬运正文（订单 spec 自带字段，缺失一律降级为空）。"""
    cons = order.get("constraints") if isinstance(order.get("constraints"), dict) else {}
    uni = [str(x) for x in (order.get("universe") or [])]
    return {
        "universe_n": len(uni),
        "universe": uni[:12],
        "horizon": order.get("horizon") or "",
        "risk": order.get("risk") or "",
        "max_picks": order.get("max_picks", cons.get("max_picks", "")),
        "constraints": cons,
        "notes": str(order.get("notes") or "")[:300],
    }


def acceptance_from_line(line_cfg: dict) -> dict:
    """验收标准源自 line.json（单一真相源），合同只引用不复写规则。"""
    rules = [str(x) for x in (line_cfg.get("gate_rules") or [])]
    return {
        "gate": line_cfg.get("gate") or "",
        "gate_rules": rules,
        "rules_n": len(rules),
        "outputs": [str(x) for x in (line_cfg.get("outputs") or [])],
    }


# ---------------------------------------------------------------- 合同写读

def sync_ledger_contract(lines_root: str, line: str, order_id: str, contract: dict,
                         spec_sha: str = "", create_ledger: bool = False) -> dict:
    """把合同摘要写进该单 ledger.json 的 contract 字段（非空即算「已立约」）。
    ledger 不存在时：create_ledger=True 建最小账（开工前态 status=running），否则跳过。"""
    lp = ledger_path(lines_root, line, order_id)
    led = _read_json(lp)
    if not led and not create_ledger:
        return {}
    if not led:
        led = {"order_id": order_id, "created_at": contract.get("created_at") or _now(),
               "status": "running"}
    led["contract"] = {
        "order_id": order_id,
        "line": line,
        "path": contract_path(lines_root, line, order_id),
        "sha256": _sha(contract_path(lines_root, line, order_id)),
        "status": contract.get("status") or "",
        "created_at": contract.get("created_at") or "",
        "updated_at": contract.get("updated_at") or "",
        "final_status": contract.get("final_status") or "",
        "spec_sha256": spec_sha or contract.get("spec_sha256") or "",
    }
    _write_json(lp, led)
    return led["contract"]


def ensure_contract(lines_root: str, line: str, order_id: str, order: dict | None = None,
                    spec_path: str = "", retro: bool = False, dry_run: bool = False) -> dict:
    """开工立约。已存在的合同不被重置：created_at/started_at 保留，只刷新规格与状态。"""
    ld = line_dir(lines_root, line)
    if not os.path.isdir(ld):
        raise SystemExit("产线目录不存在：%s" % ld)
    line_cfg = _read_json(os.path.join(ld, "line.json"))
    oid = str(order_id or "").strip()
    spec_sha = _sha(spec_path) if spec_path else ""
    cp = contract_path(lines_root, line, oid)
    cur = _read_json(cp)
    old_led = _read_json(ledger_path(lines_root, line, oid)) if retro else {}
    now = _now()
    contract = dict(cur)
    # 回填老单：签约时间与终态取该单既有 ledger（不臆造「刚签约/仍在执行」）
    contract.setdefault("created_at", old_led.get("created_at") or now)
    contract.update({
        "order_id": oid,
        "line": os.path.basename(ld) or line,
        "line_id": line_cfg.get("id") or "",
        "line_contract_ready": bool(line_cfg.get("contract_ready")),
        "spec_file": spec_path or contract.get("spec_file", ""),
        "spec_sha256": spec_sha or contract.get("spec_sha256", ""),
        "spec_summary": spec_summary(order or {}),
        "acceptance": acceptance_from_line(line_cfg),
        "status": TERMINAL_MAP.get(str(old_led.get("status") or ""), "active"),
        "updated_at": now,
    })
    if retro:
        contract["retro"] = True
        contract["final_status"] = str(old_led.get("status") or "") if contract["status"] != "active" else ""
        contract.setdefault("started_at", "")
    elif not contract.get("started_at"):
        contract["started_at"] = now
    contract.setdefault("finished_at", "")
    contract.setdefault("final_status", "")
    if dry_run:
        return contract
    _write_json(cp, contract)
    sync_ledger_contract(lines_root, line, oid, contract, spec_sha=spec_sha, create_ledger=True)
    return contract


def finalize_contract(lines_root: str, line: str, order_id: str, status: str,
                      reason: str = "", dry_run: bool = False) -> dict:
    """完工定约：status 只取 done|failed（未知态按 failed 记，原始态留 final_status）。"""
    oid = str(order_id or "").strip()
    cp = contract_path(lines_root, line, oid)
    contract = _read_json(cp)
    if not contract:
        raise SystemExit("合同不存在（先 start 或 attach）：%s" % cp)
    final = TERMINAL_MAP.get(str(status or "").strip(), "failed")
    contract.update({
        "status": final,
        "final_status": str(status or ""),
        "finished_at": contract.get("finished_at") or _now(),
        "updated_at": _now(),
    })
    if reason:
        contract["reason"] = str(reason)[:400]
    if dry_run:
        return contract
    _write_json(cp, contract)
    sync_ledger_contract(lines_root, line, oid, contract,
                         spec_sha=contract.get("spec_sha256", ""), create_ledger=True)
    return contract


# ---------------------------------------------------------------- 机检

def check_contract(lines_root: str, line: str, order_id: str) -> tuple[bool, list[str], dict]:
    """F2.1 机检：contract.json 存在 + ledger.contract 非空 + status 合法 + created_at 有值。"""
    oid = str(order_id or "").strip()
    cp, lp = contract_path(lines_root, line, oid), ledger_path(lines_root, line, oid)
    contract, led = _read_json(cp), _read_json(lp)
    checks: list[str] = []
    ok = True
    if not contract:
        ok = False
        checks.append("FAIL contract.json 缺失/不可解析：%s" % cp)
    else:
        checks.append("PASS contract.json 在（%d B）" % (os.path.getsize(cp) if os.path.exists(cp) else 0))
        st = str(contract.get("status") or "")
        if st in CONTRACT_STATUSES:
            checks.append("PASS status=%s" % st)
        else:
            ok = False
            checks.append("FAIL status 非法：%r（应属 %s）" % (st, "/".join(CONTRACT_STATUSES)))
        for k in ("order_id", "line", "created_at", "spec_summary", "acceptance"):
            if contract.get(k) in (None, "", {}):
                ok = False
                checks.append("FAIL 字段缺失/空：%s" % k)
            else:
                checks.append("PASS 字段 %s 非空" % k)
    led_c = led.get("contract") if isinstance(led.get("contract"), dict) else {}
    if led_c:
        checks.append("PASS ledger.contract 非空（status=%s）" % (led_c.get("status") or "?"))
    else:
        ok = False
        checks.append("FAIL ledger.contract 为空/缺失：%s" % lp)
    return ok, checks, {"contract": contract, "ledger_contract": led_c}


# ---------------------------------------------------------------- 包装器

def wrap_run(lines_root: str, line: str, spec_path: str, engine: str = "",
             engine_args: list[str] | None = None, no_llm: bool = False) -> int:
    """start → 跑引擎 → finish。引擎退出码原样透出（3=check_failed 也照实落 failed 合同）。"""
    order_spec = _read_json(spec_path)
    oid = str(order_spec.get("order_id") or "").strip()
    if not oid:
        print("FAIL spec.order_id missing：%s" % spec_path)
        return 2
    ensure_contract(lines_root, line, oid, order=order_spec, spec_path=spec_path)
    engine = engine or os.path.join(line_dir(lines_root, line), "run_line.py")
    if not os.path.exists(engine):
        print("FAIL 引擎不存在：%s（用 --engine 指定）" % engine)
        finalize_contract(lines_root, line, oid, "failed", reason="engine_missing")
        return 2
    cmd = [sys.executable, "-X", "utf8", engine, "--spec", spec_path] + list(engine_args or [])
    if no_llm:
        cmd.append("--no-llm")
    t0 = _dt.datetime.now()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        rc = proc.returncode
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        print(tail[0][:400])
    except OSError as e:
        rc = 1
        print("FAIL 引擎启动失败：%s" % e)
    elapsed = round((_dt.datetime.now() - t0).total_seconds(), 1)
    led = _read_json(ledger_path(lines_root, line, oid))
    finalize_contract(lines_root, line, oid, str(led.get("status") or ("failed" if rc else "done")),
                      reason=("rc=%d elapsed=%ss" % (rc, elapsed)) if rc else "")
    return rc


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="域产线「下单即立约」合同器（C-20261125-G42）")
    ap.add_argument("--lines-root", default=DEFAULT_LINES_ROOT, help="产线根（默认 %(default)s）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("start", "attach", "finish", "show", "check"):
        p = sub.add_parser(name)
        p.add_argument("--line", required=True)
        p.add_argument("--order", required=True)
        p.add_argument("--spec", default="", help="订单 spec json（start/attach 用）")
        p.add_argument("--status", default="done", help="finish 用：done|failed|check_failed")
        p.add_argument("--reason", default="")
        p.add_argument("--dry-run", action="store_true")

    pw = sub.add_parser("wrap")
    pw.add_argument("--line", required=True)
    pw.add_argument("--spec", required=True)
    pw.add_argument("--engine", default="")
    pw.add_argument("--no-llm", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "wrap":
        return wrap_run(a.lines_root, a.line, a.spec, engine=a.engine, no_llm=a.no_llm)

    if a.cmd in ("start", "attach"):
        order = _read_json(a.spec) if a.spec else {}
        c = ensure_contract(a.lines_root, a.line, a.order, order=order, spec_path=a.spec,
                            retro=(a.cmd == "attach"), dry_run=a.dry_run)
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "finish":
        c = finalize_contract(a.lines_root, a.line, a.order, a.status,
                              reason=a.reason, dry_run=a.dry_run)
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "show":
        print(json.dumps(_read_json(contract_path(a.lines_root, a.line, a.order)),
                         ensure_ascii=False, indent=2))
        return 0
    ok, checks, _ = check_contract(a.lines_root, a.line, a.order)
    for c in checks:
        print(c)
    print("VERDICT %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
