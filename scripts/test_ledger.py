# -*- coding: utf-8 -*-
"""台账校验边界用例：证据门/状态机/中断留痕不能形同虚设。

覆盖 10 个形态：
  1 合法台账（done 带证据+哈希，running 带 steer_events） -> PASS
  2 done 缺 evidence_path                                  -> FAIL
  3 done 证据文件不存在                                    -> FAIL
  4 evidence_sha256 与文件不一致                           -> FAIL
  5 非法 state                                             -> FAIL
  6 重复 task id                                           -> FAIL
  7 steer_events.interrupt 非法                            -> FAIL
  8 台账不是合法 JSON                                      -> FAIL
  9 根不是对象                                             -> FAIL
  10 tasks 不是列表                                        -> FAIL

用法：python -X utf8 test_ledger.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ledger_check import check  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def sha256(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def write(path: str, obj) -> None:
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False)


def case(name: str, reg: dict | list | str, root: str, expect_ok: bool) -> None:
    p = os.path.join(root, "registry-%s.json" % name)
    if isinstance(reg, str):
        open(p, "w", encoding="utf-8").write(reg)
    else:
        write(p, reg)
    ok, detail = check(p, root)
    good = ok is expect_ok
    RESULTS.append(("PASS" if good else "FAIL", name,
                    "ok=%s(期望%s) %s" % (ok, expect_ok, detail)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("test_ledger")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(120, "test_ledger")

    tmp = tempfile.mkdtemp(prefix="steward-ledger-edge-")
    try:
        ev = os.path.join(tmp, "ev.md")
        open(ev, "w", encoding="utf-8").write("evidence")
        h = sha256(ev)

        base_done = {"id": "T-1", "state": "done", "evidence_path": "ev.md",
                     "evidence_sha256": h, "steer_events": []}
        base_run = {"id": "T-2", "state": "running", "last_command_id": "app-control-x",
                    "steer_events": [{"ts": "2026-11-17T23:00:00", "interrupt": "applied",
                                      "ack": "OK"}]}
        case("valid", {"tasks": [base_done, base_run]}, tmp, True)

        case("done-no-path", {"tasks": [{"id": "T", "state": "done"}]}, tmp, False)

        case("done-missing-file", {"tasks": [{"id": "T", "state": "done",
                                              "evidence_path": "nope.md"}]}, tmp, False)

        case("sha-mismatch", {"tasks": [dict(base_done, evidence_sha256="0" * 64)]}, tmp, False)

        case("bad-state", {"tasks": [{"id": "T", "state": "finished"}]}, tmp, False)

        case("dup-id", {"tasks": [base_done, dict(base_done)]}, tmp, False)

        case("bad-steer", {"tasks": [{"id": "T", "state": "running",
                                      "steer_events": [{"ts": "x", "interrupt": "maybe"}]}]}, tmp, False)

        case("bad-json", "{not json", tmp, False)

        case("root-list", [{"id": "T"}], tmp, False)

        case("tasks-not-list", {"tasks": {"id": "T"}}, tmp, False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails = [r for r in RESULTS if r[0] != "PASS"]
    if args.json:
        print(json.dumps([{"status": s, "case": n, "detail": d} for s, n, d in RESULTS],
                         ensure_ascii=False, indent=2))
    else:
        for s, n, d in RESULTS:
            print("[%s] %-16s %s" % (s, n, d))
        print("\n%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails and RESULTS else 2


if __name__ == "__main__":
    raise SystemExit(main())
