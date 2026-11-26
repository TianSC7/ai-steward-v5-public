# -*- coding: utf-8 -*-
"""review_gate 旁路门用例：通过/缺证据/空证据/缺标记/关键词不全/坏卡（多种）/
哈希未登记/哈希不符/失败标记/路径解析三态/只读不变性/JSON 三字段稳定/CLI 正反例。

覆盖：
  1  pass-convention-path   全绿（约定路径 reports/evidence/<id>.md）-> PASS
  2  pass-evidence-path     卡登记相对 evidence_path 被采纳 -> PASS
  3  pass-absolute-path     卡登记绝对 evidence_path 被采纳 -> PASS
  4  pass-cli-override      --evidence 优先于卡登记路径 -> PASS
  5  evidence-missing       证据文件缺失 -> FAIL（evidence_exists=False）
  6  evidence-empty         证据 0 字节 -> FAIL（evidence_nonempty=False）
  7  marker-missing         有证据/关键词/哈希但缺 EXIT=/[PASS] -> FAIL（仅 marker 红）
  8  keywords-incomplete    缺 acceptance 字面量关键词 -> FAIL（仅 keywords 红）
  9  card-not-found         id 不在台账 -> FAIL（card_found=False）
  10 registry-bad-json      台账坏 JSON -> FAIL（registry_ok=False）
  11 registry-tasks-not-list tasks 非列表 -> FAIL（registry_ok=False）
  12 acceptance-empty       acceptance 为空 -> FAIL
  13 acceptance-no-keywords acceptance 抽不出关键词 -> FAIL（keywords 红）
  14 hash-not-registered    卡未登记 evidence_sha256 -> FAIL（hash_ok=False）
  15 hash-mismatch          登记哈希与实测不符 -> FAIL（hash_ok=False）
  16 fail-marker            证据含 [FAIL] -> FAIL（marker 红）
  17 read-only-sha256       跑前后 registry+证据 sha256 不变、scripts 目录零新文件
  18 json-fields-stable     --json 输出 verdict/reasons/checks 三字段与 8 检查键恒定
  19 cli-exit-codes         PASS 退出码 0、FAIL 退出码 2、文本首行 VERDICT:
  20 real-registry-smoke    对真实台账实跑（只读）：结构合法且 sha256 不变

用法：python -X utf8 test_review_gate.py
退出码 0 = 全部符合预期；2 = 有偏差。

自包含说明：本文件不 import fingerprint——隔离工作区上级无 marketplace.json，
fingerprint.selfcheck 必挂（test_steward_review 在该工作区即因此整体返 2）；
本用例只需要 review_gate 的确定性逻辑，故自行搭临时夹具、自管退出码。
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
import review_gate as rg  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []

# 夹具 acceptance：字面量 = review_gate.py/--id/--json/[PASS]/EXIT=0/sha256，
# 实词 = verdict/reasons/checks。GOOD_EVIDENCE 对上述全命中。
ACCEPTANCE = ("验收：scripts/review_gate.py --id W-RG --json 输出 "
              "verdict/reasons/checks 三字段；证据含 [PASS] 与 EXIT=0；"
              "sha256 已登记且一致")

# 缺标记用例专用 acceptance：字面量不含 [PASS]/EXIT=0，才能把"缺标记"从
# "关键词不全"里隔离出来（否则缺标记必然连带缺这两个字面量）。
ACCEPTANCE_NO_MARKER = ("验收：scripts/review_gate.py --id W-RG --json 输出 "
                        "verdict/reasons/checks 三字段；sha256 已登记且一致")

GOOD_EVIDENCE = """# W-RG 证据

## 命令
python -X utf8 scripts/review_gate.py --id W-RG --json

## 关键输出
[PASS] verdict=PASS
EXIT=0

## 哈希
sha256 已登记，与文件实测一致。
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def task(tid: str = "W-RG", **kw) -> dict:
    base = {
        "id": tid,
        "title": "任务 " + tid,
        "project": "ai-steward",
        "state": "done",
        "acceptance": ACCEPTANCE,
        "created_at": "2026-11-18T18:00:00+08:00",
    }
    base.update(kw)
    return base


def build_root(tasks: list[dict], evidence: dict | None = None,
               registry_text: str | None = None) -> str:
    """搭临时插件根：state/registry.json + reports/evidence/*（二进制写，哈希确定）。"""
    tmp = tempfile.mkdtemp(prefix="review-gate-")
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "reports", "evidence"), exist_ok=True)
    if registry_text is None:
        registry_text = json.dumps({"tasks": tasks}, ensure_ascii=False,
                                   indent=2) + "\n"
    with open(os.path.join(tmp, "state", "registry.json"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(registry_text)
    for name, content in (evidence or {}).items():
        with open(os.path.join(tmp, "reports", "evidence", name), "wb") as f:
            f.write(content.encode("utf-8"))
    return tmp


def good_card(**kw) -> dict:
    """带正确哈希登记的全绿卡（默认走约定路径，不设 evidence_path）。"""
    kw.setdefault("evidence_sha256",
                  hashlib.sha256(GOOD_EVIDENCE.encode("utf-8")).hexdigest())
    return task(**kw)


def run_cli(root: str, tid: str, json_mode: bool = False,
            evidence: str | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-X", "utf8", os.path.join(HERE, "review_gate.py"),
           "--id", tid, "--root", root]
    if evidence:
        cmd += ["--evidence", evidence]
    if json_mode:
        cmd.append("--json")
    return subprocess.run(cmd, capture_output=True, encoding="utf-8")


def sha256_of(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main() -> int:
    # 1 全绿：约定路径 reports/evidence/W-RG.md
    root = build_root([good_card()], evidence={"W-RG.md": GOOD_EVIDENCE})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("pass-convention-path", r["verdict"] == "PASS"
          and all(r["checks"].values())
          and r["evidence"] == os.path.join(root, "reports", "evidence", "W-RG.md"),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 2 全绿：卡登记相对 evidence_path
    root = build_root([good_card(evidence_path="reports/evidence/custom.md")],
                      evidence={"custom.md": GOOD_EVIDENCE})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("pass-evidence-path", r["verdict"] == "PASS"
          and r["evidence"].endswith("custom.md"),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 3 全绿：卡登记绝对 evidence_path
    tmp = tempfile.mkdtemp(prefix="review-gate-")
    abs_ev = os.path.join(tmp, "abs-evidence.md")
    with open(abs_ev, "wb") as f:
        f.write(GOOD_EVIDENCE.encode("utf-8"))
    root = build_root([good_card(evidence_path=abs_ev)])
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("pass-absolute-path", r["verdict"] == "PASS"
          and r["evidence"] == abs_ev, "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

    # 4 全绿：--evidence 覆盖优先于卡登记路径（卡指向不存在的文件）
    root = build_root([good_card(evidence_path="reports/evidence/nope.md")],
                      evidence={"W-RG.md": GOOD_EVIDENCE})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root,
                  evidence="reports/evidence/W-RG.md")
    check("pass-cli-override", r["verdict"] == "PASS"
          and r["evidence"].endswith("W-RG.md"), "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 5 缺证据：路径登记了但文件不存在
    root = build_root([good_card(evidence_path="reports/evidence/W-RG.md")])
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("evidence-missing", r["verdict"] == "FAIL"
          and r["checks"]["evidence_exists"] is False
          and any("证据文件缺失" in x for x in r["reasons"])
          and "补写" in r["action"],
          "verdict=%s action=%s" % (r["verdict"], r["action"][:40]))
    shutil.rmtree(root, ignore_errors=True)

    # 6 空证据：0 字节
    root = build_root([good_card()], evidence={"W-RG.md": ""})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("evidence-empty", r["verdict"] == "FAIL"
          and r["checks"]["evidence_exists"] is True
          and r["checks"]["evidence_nonempty"] is False
          and any("证据文件为空" in x for x in r["reasons"]),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 7 缺标记：证据/关键词/哈希俱备，仅无 EXIT= 与 [PASS]
    #    （acceptance 专用不含标记字面量，确保只有 marker 一项判红）
    no_marker = """# W-RG 证据
python -X utf8 scripts/review_gate.py --id W-RG --json
verdict 字段正常输出，未见异常。
sha256 登记一致。
"""
    root = build_root([good_card(acceptance=ACCEPTANCE_NO_MARKER,
                                 evidence_sha256=hashlib.sha256(
                                     no_marker.encode("utf-8")).hexdigest())],
                      evidence={"W-RG.md": no_marker})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("marker-missing", r["verdict"] == "FAIL"
          and r["checks"]["marker_hit"] is False
          and r["checks"]["keywords_hit"] is True
          and r["checks"]["hash_ok"] is True
          and any("通过标记" in x for x in r["reasons"]),
          "verdict=%s reasons=%s" % (r["verdict"], r["reasons"]))
    shutil.rmtree(root, ignore_errors=True)

    # 8 关键词不全：标记/哈希俱备，缺字面量 review_gate.py/--id/--json
    few_kw = """# W-RG 证据
[PASS] 复核通过
EXIT=0
verdict 字段输出正常。
sha256 登记一致。
"""
    root = build_root([good_card(evidence_sha256=hashlib.sha256(
        few_kw.encode("utf-8")).hexdigest())],
        evidence={"W-RG.md": few_kw})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("keywords-incomplete", r["verdict"] == "FAIL"
          and r["checks"]["marker_hit"] is True
          and r["checks"]["keywords_hit"] is False
          and r["checks"]["hash_ok"] is True
          and any("关键词不全" in x for x in r["reasons"])
          and all(k in "".join(r["reasons"])
                  for k in ("review_gate.py", "--id", "--json")),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 9 坏卡：id 不在台账
    root = build_root([task("W-OTHER")], evidence={"W-RG.md": GOOD_EVIDENCE})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("card-not-found", r["verdict"] == "FAIL"
          and r["checks"]["card_found"] is False
          and any("任务不存在" in x for x in r["reasons"]),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 10 坏卡：台账坏 JSON
    root = build_root([], registry_text="{not-json")
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("registry-bad-json", r["verdict"] == "FAIL"
          and r["checks"]["registry_ok"] is False
          and any("台账坏 JSON" in x for x in r["reasons"]),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 11 坏卡：tasks 非列表
    root = build_root([], registry_text=json.dumps({"tasks": "oops"}))
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("registry-tasks-not-list", r["verdict"] == "FAIL"
          and r["checks"]["registry_ok"] is False
          and any("tasks 非列表" in x for x in r["reasons"]),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 12 坏卡：acceptance 为空
    root = build_root([good_card(acceptance="")],
                      evidence={"W-RG.md": GOOD_EVIDENCE})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("acceptance-empty", r["verdict"] == "FAIL"
          and r["checks"]["acceptance_present"] is False
          and any("acceptance 为空" in x for x in r["reasons"]),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 13 坏卡：acceptance 纯中文散文，抽不出可机检关键词
    root = build_root([good_card(acceptance="把任务做好做完整")],
                      evidence={"W-RG.md": GOOD_EVIDENCE})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("acceptance-no-keywords", r["verdict"] == "FAIL"
          and r["checks"]["keywords_hit"] is False
          and any("可机检关键词" in x for x in r["reasons"]),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 14 哈希未登记
    root = build_root([task()], evidence={"W-RG.md": GOOD_EVIDENCE})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("hash-not-registered", r["verdict"] == "FAIL"
          and r["checks"]["hash_ok"] is False
          and any("哈希未登记" in x for x in r["reasons"])
          and r["sha256"] in r["action"],
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 15 哈希不符：登记一个错的
    root = build_root([good_card(evidence_sha256="0" * 64)],
                      evidence={"W-RG.md": GOOD_EVIDENCE})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("hash-mismatch", r["verdict"] == "FAIL"
          and r["checks"]["hash_ok"] is False
          and any("哈希不符" in x for x in r["reasons"])
          and r["sha256"] == hashlib.sha256(GOOD_EVIDENCE.encode("utf-8")).hexdigest(),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 16 失败标记：证据含 [FAIL]（同时缺字面量，关键词并列判红，属预期）
    fail_ev = """# W-RG 证据
[FAIL] 复核未过，详见日志。
"""
    root = build_root([good_card(evidence_sha256=hashlib.sha256(
        fail_ev.encode("utf-8")).hexdigest())],
        evidence={"W-RG.md": fail_ev})
    r = rg.review(os.path.join(root, "state", "registry.json"), "W-RG", root=root)
    check("fail-marker", r["verdict"] == "FAIL"
          and r["checks"]["marker_hit"] is False
          and any("失败标记" in x for x in r["reasons"]),
          "verdict=%s" % r["verdict"])
    shutil.rmtree(root, ignore_errors=True)

    # 17 只读不变性：跑前后 registry+证据 sha256 不变；scripts 目录零新文件
    root = build_root([good_card(), task("W-BAD", evidence_path="reports/evidence/x.md")],
                      evidence={"W-RG.md": GOOD_EVIDENCE})
    reg_p = os.path.join(root, "state", "registry.json")
    ev_p = os.path.join(root, "reports", "evidence", "W-RG.md")

    def snap_scripts() -> set:
        out = set()
        for dirpath, dirnames, filenames in os.walk(HERE):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                out.add(os.path.relpath(os.path.join(dirpath, fn), HERE))
        return out

    before_files = snap_scripts()
    before = {p: sha256_of(p) for p in (reg_p, ev_p)}
    p1 = run_cli(root, "W-RG")
    p2 = run_cli(root, "W-RG", json_mode=True)
    p3 = run_cli(root, "W-BAD")
    after = {p: sha256_of(p) for p in (reg_p, ev_p)}
    after_files = snap_scripts()
    check("read-only-sha256", before == after
          and before_files == after_files
          and p1.returncode == 0 and p3.returncode == 2,
          "sha256-stable=%s files-stable=%s rc=%d/%d"
          % (before == after, before_files == after_files,
             p1.returncode, p3.returncode))
    shutil.rmtree(root, ignore_errors=True)

    # 18 JSON 三字段稳定：PASS/FAIL 两态都恒定，checks 8 键恒定
    root = build_root([good_card()], evidence={"W-RG.md": GOOD_EVIDENCE})
    p_ok = run_cli(root, "W-RG", json_mode=True)
    p_bad = run_cli(root, "W-NOPE", json_mode=True)
    stable = True
    detail = ""
    try:
        ok = json.loads(p_ok.stdout)
        bad = json.loads(p_bad.stdout)
        stable = (all(k in ok for k in ("verdict", "reasons", "checks"))
                  and all(k in bad for k in ("verdict", "reasons", "checks"))
                  and list(ok["checks"]) == list(rg.CHECK_KEYS)
                  and list(bad["checks"]) == list(rg.CHECK_KEYS)
                  and ok["verdict"] == "PASS" and bad["verdict"] == "FAIL"
                  and isinstance(ok["reasons"], list)
                  and isinstance(bad["reasons"], list)
                  and isinstance(ok["action"], str) and ok["action"])
    except ValueError as e:
        stable = False
        detail = "JSON 解析失败: %s" % e
    check("json-fields-stable", stable,
          detail or "verdict=%s/%s keys=%d"
          % (ok.get("verdict"), bad.get("verdict"), len(ok.get("checks", {}))))
    shutil.rmtree(root, ignore_errors=True)

    # 19 CLI 正反例：退出码与文本首行
    root = build_root([good_card()], evidence={"W-RG.md": GOOD_EVIDENCE})
    p_ok = run_cli(root, "W-RG")
    p_bad = run_cli(root, "W-NOPE")
    check("cli-exit-codes", p_ok.returncode == 0
          and p_ok.stdout.splitlines()[0] == "VERDICT: PASS"
          and p_bad.returncode == 2
          and p_bad.stdout.splitlines()[0] == "VERDICT: FAIL"
          and "ACTION:" in p_ok.stdout and "CHECKS:" in p_ok.stdout,
          "rc=%d/%d" % (p_ok.returncode, p_bad.returncode))
    shutil.rmtree(root, ignore_errors=True)

    # 20 真实台账冒烟（只读）：结构合法、退出码合规、registry sha256 不变
    real_reg = os.path.join(ROOT, "state", "registry.json")
    real_before = sha256_of(real_reg)
    p_real = run_cli(ROOT, "W-20261118-011", json_mode=True)
    real_after = sha256_of(real_reg)
    smoke_ok = False
    detail = ""
    try:
        obj = json.loads(p_real.stdout)
        smoke_ok = (p_real.returncode in (0, 2)
                    and obj.get("verdict") in ("PASS", "FAIL")
                    and list(obj.get("checks", {})) == list(rg.CHECK_KEYS)
                    and real_before == real_after)
        detail = "verdict=%s rc=%d" % (obj.get("verdict"), p_real.returncode)
    except ValueError as e:
        detail = "JSON 解析失败: %s" % e
    check("real-registry-smoke", smoke_ok, detail)

    fails = [x for x in RESULTS if x[0] == "FAIL"]
    for status, name, detail in RESULTS:
        print("[%s] %-24s %s" % (status, name, detail))
    total = len(RESULTS)
    print("%d passed / %d total" % (total - len(fails), total))
    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())
