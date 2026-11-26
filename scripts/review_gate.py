# -*- coding: utf-8 -*-
"""review_gate：reviewer 旁路门——按任务卡 acceptance 对证据出确定性复核意见（纯只读）。

定位（与既有件的关系）：
  steward_cycle.evidence_problem() 是调度引擎内的 done 前证据复核；steward_review.py
  是管家/人工独立复核。本脚本是"旁路门"：不接调度引擎、不调用 LLM、不写任何文件，
  输入任务卡 ID，机械核对 registry 的 acceptance 与 reports/evidence/<id>.md，
  输出 verdict/reasons/checks 三字段稳定的 JSON 复核意见 + 建议动作，供 reviewer
  在 done 前独立把关（引擎意见与旁路意见可互相印证，任一飘红都不放行）。

确定性检查（顺序短路，未及执行的检查记 False）：
  registry_ok        台账可读且结构合法（根为对象、tasks 为列表）
  card_found         任务 id 存在于台账
  acceptance_present acceptance 非空
  evidence_exists    证据文件存在；路径优先级 --evidence > 卡 evidence_path >
                     <root>/reports/evidence/<id>.md（相对路径以 --root 为基准）
  evidence_nonempty  证据文件非空
  marker_hit         证据含通过标记（EXIT=<值> 或 [PASS]）且不含失败标记
                     （EXIT=FAIL / [FAIL]；两者同时出现同样判红，交人工判读）
  keywords_hit       acceptance 关键词命中：字面量（EXIT=…/[PASS]/--flag/文件名/
                     sha256/>=N）全部命中，且实词（>=4 字母 ASCII 词）至少命中一个；
                     acceptance 抽不出任何关键词时判红（不可机检）
  hash_ok            sha256 可算、卡上已登记 evidence_sha256、且与文件实测一致
                     （口径同 steward_cycle.evidence_problem：未登记/不符均判红）

用法：
  python -X utf8 scripts/review_gate.py --id W-20261118-025
  python -X utf8 scripts/review_gate.py --id W-... --root <插件根> --json
  python -X utf8 scripts/review_gate.py --id W-... --evidence <path> --json

选项：
  --id        必填，任务 id
  --root      插件根目录（默认取脚本上级）；相对 evidence_path 以此为基准
  --registry  台账路径（默认 <root>/state/registry.json）
  --evidence  证据文件路径（默认取卡 evidence_path，再退约定路径）
  --json      输出 JSON 对象（verdict/reasons/checks 三字段恒定存在）

退出码：0 = verdict PASS；2 = verdict FAIL 或台账/参数问题。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")

# 检查键固定且有序：JSON 消费者可依赖这 8 个键恒定存在；未执行的检查记 False。
CHECK_KEYS = ("registry_ok", "card_found", "acceptance_present", "evidence_exists",
              "evidence_nonempty", "marker_hit", "keywords_hit", "hash_ok")

# 通过标记：EXIT=<非空值> 或 [PASS]；失败标记：EXIT=FAIL 或 [FAIL]。
_PASS_MARKER_RE = re.compile(r"EXIT=\S|\[PASS\]")
_FAIL_MARKER_RE = re.compile(r"EXIT=FAIL|\[FAIL\]")

# acceptance 关键词提取口径：
#   字面量（强信号，须全部命中）：EXIT=…、[PASS]/[FAIL]、--flag、文件名、sha256、>=N
#   实词（弱信号，至少命中一个）：长度 >=4 的 ASCII 词，去停用词与字面量重复
_LITERAL_RE = re.compile(
    r"EXIT=\w*"
    r"|\[PASS\]|\[FAIL\]"
    r"|--[A-Za-z][\w-]*"
    r"|[\w.+-]+\.(?:py|md|json|sh|ps1)"
    r"|sha256"
    r"|>=?\s*\d+"
)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{3,}")
_STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "into", "over",
              "must", "shall", "should", "true", "false", "none", "null",
              "test", "tests", "done", "pass", "fail", "ok"}


def load_registry(path: str) -> tuple[dict | None, str | None]:
    """读台账。成功返回 (data, None)；失败返回 (None, 错误描述)。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return None, "台账读取失败: %s" % e
    except ValueError as e:
        return None, "台账坏 JSON: %s" % e
    if not isinstance(data, dict):
        return None, "台账结构非法（根非对象）"
    if not isinstance(data.get("tasks"), list):
        return None, "台账结构非法（tasks 非列表）"
    return data, None


def extract_keywords(acceptance: str) -> tuple[list[str], list[str]]:
    """从 acceptance 抽 (字面量, 实词)，各按出现序去重。"""
    literals: list[str] = []
    for m in _LITERAL_RE.finditer(acceptance):
        tok = re.sub(r"\s+", "", m.group(0))
        if tok and tok not in literals:
            literals.append(tok)
    generals: list[str] = []
    for m in _WORD_RE.finditer(acceptance):
        tok = m.group(0)
        if tok.lower() in _STOPWORDS or tok in literals:
            continue
        if tok not in generals:
            generals.append(tok)
    return literals, generals


def resolve_evidence(task: dict, tid: str, root: str, override: str | None) -> str:
    """证据绝对路径：--evidence > 卡 evidence_path > <root>/reports/evidence/<id>.md。"""
    if override:
        return override if os.path.isabs(override) else os.path.join(root, override)
    ev = str(task.get("evidence_path") or "").strip()
    if ev:
        return ev if os.path.isabs(ev) else os.path.join(root, ev)
    return os.path.join(root, "reports", "evidence", "%s.md" % tid)


def review(reg_path: str, tid: str, root: str | None = None,
           evidence: str | None = None) -> dict:
    """对任务 tid 出复核意见。纯只读，返回结果 dict。

    verdict = PASS|FAIL；reasons 逐条理由（FAIL 列问题、PASS 列支持项）；
    checks 8 个布尔键恒定存在；action 为建议动作（取第一个失败检查）。
    """
    root = root or ROOT
    problems: list[str] = []
    notes: list[str] = []
    action = ""
    checks = {k: False for k in CHECK_KEYS}
    result = {"verdict": "FAIL", "task": tid, "state": "", "acceptance": "",
              "evidence": "", "sha256": "", "checks": checks,
              "reasons": [], "action": ""}

    def fail(msg: str, act: str) -> None:
        nonlocal action
        problems.append(msg)
        if not action:
            action = act

    data, err = load_registry(reg_path)
    if err:
        fail(err, "修复台账后重跑（本工具只读，不代修）")
        result["reasons"] = problems
        result["action"] = action
        return result
    checks["registry_ok"] = True

    task = next((t for t in (data.get("tasks") or [])
                 if isinstance(t, dict) and t.get("id") == tid), None)
    if task is None:
        fail("任务不存在: %s" % tid, "核对 --id 是否与 registry 登记一致")
        result["reasons"] = problems
        result["action"] = action
        return result
    checks["card_found"] = True

    result["state"] = str(task.get("state") or "")
    acceptance = str(task.get("acceptance") or "").strip()
    result["acceptance"] = acceptance
    if not acceptance:
        fail("acceptance 为空/缺失",
             "在卡上补写 acceptance（含可机检关键词：EXIT=/ [PASS]/ 文件名/ sha256/ >=N）")
        result["reasons"] = problems
        result["action"] = action
        return result
    checks["acceptance_present"] = True

    ev_abs = resolve_evidence(task, tid, root, evidence)
    result["evidence"] = ev_abs
    if not os.path.isfile(ev_abs):
        fail("证据文件缺失: %s" % ev_abs, "要求 worker 补写证据文件: %s" % ev_abs)
        result["reasons"] = problems
        result["action"] = action
        return result
    checks["evidence_exists"] = True
    try:
        with open(ev_abs, "rb") as f:
            raw = f.read()
    except OSError as e:
        fail("证据文件不可读: %s" % e, "排查证据文件读取权限后重跑")
        result["reasons"] = problems
        result["action"] = action
        return result
    if not raw:
        fail("证据文件为空: %s" % ev_abs, "要求 worker 补全证据内容（当前 0 字节）")
        result["reasons"] = problems
        result["action"] = action
        return result
    checks["evidence_nonempty"] = True
    notes.append("证据存在且非空（%d 字节）" % len(raw))
    text = raw.decode("utf-8", "replace")

    # 通过/失败标记
    fail_m = _FAIL_MARKER_RE.search(text)
    pass_m = _PASS_MARKER_RE.search(text)
    if fail_m and pass_m:
        fail("证据同时含通过与失败标记（%s / %s），需人工判读"
             % (pass_m.group(0), fail_m.group(0)),
             "要求 worker 清理证据中的失败标记或人工判读后重交")
    elif fail_m:
        fail("证据含失败标记: %s" % fail_m.group(0),
             "证据为失败结论，要求 worker 修复后重交")
    elif not pass_m:
        fail("证据缺通过标记（EXIT=<值> 或 [PASS]）",
             "要求 worker 在证据中补通过标记 EXIT=0 或 [PASS]")
    else:
        checks["marker_hit"] = True
        notes.append("通过标记命中: %s" % pass_m.group(0))

    # acceptance 关键词命中
    literals, generals = extract_keywords(acceptance)
    miss = [k for k in literals if k not in text]
    gen_hits = [k for k in generals if k in text]
    if not literals and not generals:
        fail("acceptance 未给出可机检关键词（无字面量/实词）",
             "在 acceptance 写明可机检关键词（EXIT=/ [PASS]/ 文件名/ sha256/ >=N）")
    elif miss:
        fail("关键词不全: 缺 %s" % ", ".join(miss),
             "要求 worker 在证据中覆盖 acceptance 关键词: %s" % ", ".join(miss))
    elif generals and not gen_hits:
        fail("关键词命中不足: 字面量全中但实词 0 命中",
             "要求 worker 在证据中覆盖 acceptance 实词: %s" % ", ".join(generals))
    else:
        checks["keywords_hit"] = True
        notes.append("关键词命中: 字面量 %d/%d，实词 %d/%d"
                     % (len(literals) - len(miss), len(literals),
                        len(gen_hits), len(generals)))

    # 哈希可登记：可算 + 卡上已登记 + 与实测一致
    sha = hashlib.sha256(raw).hexdigest()
    result["sha256"] = sha
    reg_sha = str(task.get("evidence_sha256") or "").strip().lower()
    if not reg_sha:
        fail("哈希未登记（卡上缺 evidence_sha256）",
             "在卡上登记 evidence_sha256=%s（本工具只读，不代写台账）" % sha)
    elif reg_sha != sha:
        fail("哈希不符: 卡登记 %s…，实测 %s…" % (reg_sha[:12], sha[:12]),
             "证据与登记哈希不符：确认改动后重登记，或排查文件被改")
    else:
        checks["hash_ok"] = True
        notes.append("哈希与登记一致: %s" % sha)

    if problems:
        result["verdict"] = "FAIL"
        result["reasons"] = problems
    else:
        result["verdict"] = "PASS"
        result["reasons"] = notes
        action = "复核通过：可交由管家标记 done（review_gate 旁路确定性复核）"
    result["action"] = action
    return result


def render(result: dict, json_mode: bool = False) -> str:
    if json_mode:
        return json.dumps(result, ensure_ascii=False, indent=2)
    lines = [
        "VERDICT: %s" % result["verdict"],
        "TASK: %s" % result["task"],
        "STATE: %s" % (result["state"] or ""),
        "ACCEPTANCE: %s" % (result["acceptance"] or ""),
        "EVIDENCE: %s" % (result["evidence"] or ""),
        "SHA256: %s" % (result["sha256"] or ""),
        "CHECKS:",
    ]
    for k in CHECK_KEYS:
        lines.append("- %s = %s" % (k, "true" if result["checks"][k] else "false"))
    lines.append("REASONS:")
    for r in result["reasons"]:
        lines.append("- " + r)
    lines.append("ACTION: %s" % result["action"])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="reviewer 旁路门：按 acceptance 与证据文件出确定性复核意见（只读）")
    ap.add_argument("--id", required=True, help="任务 id，如 W-20261118-025")
    ap.add_argument("--root", default=ROOT, help="插件根目录（默认取脚本上级）")
    ap.add_argument("--registry", default=None,
                    help="台账路径（默认 <root>/state/registry.json）")
    ap.add_argument("--evidence", default=None,
                    help="证据文件路径（默认取卡 evidence_path，再退约定路径）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 对象")
    args = ap.parse_args()

    reg = args.registry or os.path.join(args.root, "state", "registry.json")
    result = review(reg, args.id, root=args.root, evidence=args.evidence)
    print(render(result, args.json))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
