# -*- coding: utf-8 -*-
"""evidence_audit 用例：一致/哈希漂移/缺失/孤儿/空文件/缺标记/ done 缺证据/
路径解析/JSON 字段稳定/只读不变性/真实台账冒烟。

覆盖：
  1  clean-consistent       两卡全绿（一卡 EXIT=0、一卡 [PASS]）-> 0 issue
  2  hash-drift             登记哈希与实测不符 -> hash_drift（error）
  3  evidence-missing       登记了 evidence_path 但文件不存在 -> missing_evidence
  4  evidence-empty         证据 0 字节 -> empty_evidence
  5  marker-missing         证据不含 EXIT=/[PASS] -> missing_pass_marker
  6  orphan-evidence        目录里无卡引用的文件 -> orphan_evidence（warn）
  7  done-missing-path      done 卡空 evidence_path 判红；dispatched 空路径不判红
  8  absolute-path          卡登记绝对 evidence_path 正常解析、不算孤儿
  9  json-schema-stable     --json 的 issue 含 id/level/kind，counts 键恒定，rc=2
  10 cli-clean-exit0        无 issue 时 CLI rc=0 且 issues 为空
  11 read-only-invariance   跑前后 registry/证据 sha256 与目录清单不变
  12 real-registry-smoke    对真实台账实跑（只读）：JSON 可解析、registry 不变

用法：python -X utf8 test_evidence_audit.py
退出码 0 = 全部符合预期；2 = 有偏差。

自包含说明：不 import fingerprint（隔离工作区上级无 marketplace.json 会整体返 2）；
本用例只测 evidence_audit 的确定性逻辑，自行搭临时夹具、自管退出码。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import evidence_audit as ea  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []

GOOD = """# W-T 证据

## 命令
python -X utf8 scripts/evidence_audit.py --json

[PASS] issues=0
EXIT=0
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def sha256_of(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def task(tid: str = "W-T1", state: str = "done", **kw) -> dict:
    base = {"id": tid, "title": "任务 " + tid, "project": "ai-steward",
            "state": state, "created_at": "2026-11-18T18:00:00"}
    base.update(kw)
    return base


def build_root(tasks: list[dict], evidence: dict | None = None) -> str:
    """搭临时插件根：state/registry.json + reports/evidence/*（二进制写）。"""
    tmp = tempfile.mkdtemp(prefix="ev-audit-")
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "reports", "evidence"), exist_ok=True)
    with open(os.path.join(tmp, "state", "registry.json"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2) + "\n")
    for name, content in (evidence or {}).items():
        with open(os.path.join(tmp, "reports", "evidence", name), "wb") as f:
            f.write(content.encode("utf-8"))
    return tmp


def good_card(tid: str = "W-T1", content: str = GOOD, **kw) -> dict:
    kw.setdefault("evidence_path", "reports/evidence/%s.md" % tid)
    kw.setdefault("evidence_sha256",
                  hashlib.sha256(content.encode("utf-8")).hexdigest())
    return task(tid, **kw)


def run_cli(root: str, json_mode: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-X", "utf8",
           os.path.join(HERE, "evidence_audit.py"), "--root", root]
    if json_mode:
        cmd.append("--json")
    return subprocess.run(cmd, capture_output=True, encoding="utf-8")


def kinds(result: dict) -> list[str]:
    return [i["kind"] for i in result["issues"]]


def main() -> int:
    # 1 全绿：两卡一致（EXIT= 与 [PASS] 各一）
    alt = GOOD.replace("[PASS] issues=0\n", "").strip() + "\n"
    root = build_root(
        [good_card("W-T1"), good_card("W-T2", content=alt)],
        evidence={"W-T1.md": GOOD, "W-T2.md": alt})
    r = ea.audit(os.path.join(root, "state", "registry.json"), root=root)
    check("clean-consistent",
          r["ok"] and not r["issues"] and r["counts"]["cards_total"] == 2
          and r["counts"]["done_total"] == 2
          and r["counts"]["cards_with_evidence"] == 2
          and r["counts"]["evidence_files"] == 2
          and r["counts"]["issues_total"] == 0,
          "issues=%d" % len(r["issues"]))
    shutil.rmtree(root, ignore_errors=True)

    # 2 哈希漂移
    root = build_root([good_card(evidence_sha256="0" * 64)],
                      evidence={"W-T1.md": GOOD})
    r = ea.audit(os.path.join(root, "state", "registry.json"), root=root)
    it = r["issues"][0] if r["issues"] else {}
    check("hash-drift", kinds(r) == ["hash_drift"]
          and it.get("level") == "error" and it.get("id") == "W-T1",
          "kinds=%s" % kinds(r))
    shutil.rmtree(root, ignore_errors=True)

    # 3 证据文件缺失
    root = build_root([good_card()], evidence={})
    r = ea.audit(os.path.join(root, "state", "registry.json"), root=root)
    check("evidence-missing", kinds(r) == ["missing_evidence"]
          and r["issues"][0]["level"] == "error",
          "kinds=%s" % kinds(r))
    shutil.rmtree(root, ignore_errors=True)

    # 4 证据 0 字节
    root = build_root([good_card(evidence_sha256="")],
                      evidence={"W-T1.md": ""})
    r = ea.audit(os.path.join(root, "state", "registry.json"), root=root)
    check("evidence-empty", kinds(r) == ["empty_evidence"],
          "kinds=%s" % kinds(r))
    shutil.rmtree(root, ignore_errors=True)

    # 5 缺通过标记
    no_marker = "# W-T1 证据\n\n只有描述，没有标记。\n"
    root = build_root(
        [good_card(evidence_sha256=hashlib.sha256(
            no_marker.encode("utf-8")).hexdigest())],
        evidence={"W-T1.md": no_marker})
    r = ea.audit(os.path.join(root, "state", "registry.json"), root=root)
    check("marker-missing", kinds(r) == ["missing_pass_marker"],
          "kinds=%s" % kinds(r))
    shutil.rmtree(root, ignore_errors=True)

    # 6 孤儿证据（合法卡引用的文件不算孤儿）
    root = build_root([good_card()],
                      evidence={"W-T1.md": GOOD, "stale-old.md": GOOD})
    r = ea.audit(os.path.join(root, "state", "registry.json"), root=root)
    orphans = [i for i in r["issues"] if i["kind"] == "orphan_evidence"]
    check("orphan-evidence",
          kinds(r) == ["orphan_evidence"] and len(orphans) == 1
          and orphans[0]["level"] == "warn"
          and "stale-old.md" in orphans[0]["path"]
          and r["counts"]["evidence_files"] == 2,
          "kinds=%s" % kinds(r))
    shutil.rmtree(root, ignore_errors=True)

    # 7 done 缺 evidence_path；dispatched 空路径不判红
    root = build_root([task("W-DONE"), task("W-DISP", state="dispatched")],
                      evidence={})
    r = ea.audit(os.path.join(root, "state", "registry.json"), root=root)
    check("done-missing-path",
          kinds(r) == ["done_missing_evidence"]
          and r["issues"][0]["id"] == "W-DONE",
          "kinds=%s ids=%s" % (kinds(r), [i["id"] for i in r["issues"]]))
    shutil.rmtree(root, ignore_errors=True)

    # 8 卡登记绝对 evidence_path：正常解析、不算孤儿
    tmp = tempfile.mkdtemp(prefix="ev-audit-abs-")
    os.makedirs(os.path.join(tmp, "state"))
    os.makedirs(os.path.join(tmp, "reports", "evidence"))
    abs_ev = os.path.join(tmp, "reports", "evidence", "W-ABS.md")
    with open(abs_ev, "w", encoding="utf-8", newline="\n") as f:
        f.write(GOOD)
    with open(os.path.join(tmp, "state", "registry.json"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"tasks": [good_card(
            "W-ABS", evidence_path=abs_ev,
            evidence_sha256=hashlib.sha256(
                GOOD.encode("utf-8")).hexdigest())]}, ensure_ascii=False) + "\n")
    r = ea.audit(os.path.join(tmp, "state", "registry.json"), root=tmp)
    check("absolute-path", r["ok"] and not r["issues"],
          "kinds=%s" % kinds(r))
    shutil.rmtree(tmp, ignore_errors=True)

    # 9 JSON 字段稳定（有 issue 时 rc=2）
    root = build_root([good_card(evidence_sha256="0" * 64)],
                      evidence={"W-T1.md": GOOD, "orphan.md": GOOD})
    p = run_cli(root, json_mode=True)
    obj = {}
    try:
        obj = json.loads(p.stdout)
    except ValueError as e:
        check("json-schema-stable", False, "JSON 解析失败: %s" % e)
    else:
        c = obj.get("counts", {})
        stable = (p.returncode == 2
                  and isinstance(obj.get("issues"), list)
                  and all({"id", "level", "kind"} <= set(i) for i in obj["issues"])
                  and all(k in c for k in ea.COUNT_KEYS)
                  and set(c.get("by_level", {})) == {"error", "warn"}
                  and set(c.get("by_kind", {})) == set(ea.KINDS)
                  and c.get("issues_total") == len(obj["issues"]))
        check("json-schema-stable", stable,
              "rc=%d issues=%d keys=%s" % (
                  p.returncode, len(obj.get("issues", [])), sorted(c)))
    shutil.rmtree(root, ignore_errors=True)

    # 10 干净库 CLI rc=0
    root = build_root([good_card()], evidence={"W-T1.md": GOOD})
    p = run_cli(root, json_mode=True)
    try:
        obj = json.loads(p.stdout)
    except ValueError:
        obj = {}
    check("cli-clean-exit0",
          p.returncode == 0 and obj.get("issues") == [] and obj.get("ok") is True,
          "rc=%d" % p.returncode)
    shutil.rmtree(root, ignore_errors=True)

    # 11 只读不变性：跑前后 registry/证据 sha256 与清单一致
    root = build_root([good_card()], evidence={"W-T1.md": GOOD})
    reg = os.path.join(root, "state", "registry.json")
    ev = os.path.join(root, "reports", "evidence", "W-T1.md")
    before = (sha256_of(reg), sha256_of(ev),
              sorted(os.listdir(os.path.join(root, "reports", "evidence"))))
    ea.audit(reg, root=root)
    ea.audit(reg, root=root, evidence_dir=os.path.join(root, "reports", "evidence"))
    after = (sha256_of(reg), sha256_of(ev),
             sorted(os.listdir(os.path.join(root, "reports", "evidence"))))
    check("read-only-invariance", before == after, "unchanged=%s" % (before == after))
    shutil.rmtree(root, ignore_errors=True)

    # 12 真实台账冒烟（只读）
    real_reg = os.path.join(ROOT, "state", "registry.json")
    real_sha_before = sha256_of(real_reg)
    p = run_cli(ROOT, json_mode=True)
    real_sha_after = sha256_of(real_reg)
    ok = False
    detail = ""
    try:
        obj = json.loads(p.stdout)
        c = obj.get("counts", {})
        ok = (p.returncode in (0, 2) and real_sha_before == real_sha_after
              and c.get("issues_total") == len(obj.get("issues", []))
              and all(k in c for k in ea.COUNT_KEYS))
        detail = "rc=%d issues=%d" % (p.returncode, len(obj.get("issues", [])))
    except ValueError as e:
        detail = "JSON 解析失败: %s" % e
    check("real-registry-smoke", ok, detail)

    fails = [x for x in RESULTS if x[0] == "FAIL"]
    for status, name, detail in RESULTS:
        print("[%s] %-22s %s" % (status, name, detail))
    print("%d passed / %d total" % (len(RESULTS) - len(fails), len(RESULTS)))
    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())
