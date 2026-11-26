# -*- coding: utf-8 -*-
"""reviewer 独立复核（只读）：按任务卡 acceptance 逐条对证据出 pass/fail/missing。

为什么需要（W-20261118-037）：
  调度台的 done 门是引擎内判定；本脚本是 reviewer 的独立补充意见，不作唯一门禁。
  它不接调度引擎、不调用 LLM、不写任何文件：读 registry 里的任务卡，把 acceptance
  拆成条目，再在证据文件里逐条找支持引文，输出可逐条核对的 JSON。

判定口径（确定性、可解释）：
  - 条目拆分：优先按 1) 2) / （1） / ① / 一、 等编号，其次按行首 - * • 列表，
    再按中英文分号；都拆不开时整段 acceptance 作为一条。
  - 强字面量（文件名、--flag、EXIT=0、sha256、>=N、反引号命令）必须全部出现在证据中；
    EXIT=/[PASS] 这类写法按「通过标记」处理，证据含 EXIT=0 或 [PASS] 即算命中。
  - ASCII 实词（>=4 字符）按覆盖率判定（>=60%）；中文按二元组覆盖率判定（>=1/3）。
  - evidence_quote 取证据中命中信号最多的一行原文（超长截断），便于人工回看。
  - 证据文件缺失/为空：verdict=missing，逐条 status=missing，退出码 2，不抛栈。
  - 证据存在但某条无支撑：该条 status=fail，总 verdict=fail，退出码 2。

用法：
  python -X utf8 scripts/steward_review.py --id W-20261118-037
  python -X utf8 scripts/steward_review.py --id W-... --json
  python -X utf8 scripts/steward_review.py --id W-... --root <插件根> \
      [--registry <registry.json>] [--evidence <证据文件>] [--json]

输出（--json，恰好三个顶层键）：
  {"id": "W-...", "verdict": "pass|fail|missing",
   "items": [{"item": "...", "status": "pass|fail|missing", "evidence_quote": "..."}]}

退出码：0 = 全部条目 pass；2 = fail/missing 或输入错误（均不抛栈）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")

# ---- acceptance 拆条 -------------------------------------------------------

# 编号锚点：行首或非字母数字之后的 1) / (1) / （1） / ① / 一、 等；后随空白。
# 用 (?<![A-Za-z0-9_]) 而不是 (?<=\s)：任务卡常把「）2)」直接连写，无空格。
_NUM_MARKER_RE = re.compile(
    r"(?m)(?<![A-Za-z0-9_])"
    r"(\(?\d{1,2}\)|（\d{1,2}）|[①-⑳]|[一二三四五六七八九十]{1,3}[、.．)）])"
    r"\s+"
)
# 行首列表锚点：- / * / •
_BULLET_RE = re.compile(r"(?m)(?<![A-Za-z0-9_])[-*•]\s+")

# ---- 条目信号提取 ----------------------------------------------------------

# 强字面量：反引号命令、--flag、带扩展名文件/路径、sha256、>=N、EXIT=值、[PASS]/[FAIL]
_LITERAL_RE = re.compile(
    r"`[^`]+`"
    r"|(?<![A-Za-z0-9_])--[A-Za-z][A-Za-z0-9_-]*"
    r"|(?<![A-Za-z0-9_./\\-])(?:[A-Za-z]:)?[A-Za-z0-9_./\\-]+"
    r"\.(?:py|md|json|jsonl|sqlite|sql|sh|ps1|txt|ya?ml)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])sha256(?![A-Za-z0-9_])"
    r"|>=?\s*\d+"
    r"|(?<![A-Za-z0-9_])EXIT=[^\s，。；、）)]+"
    r"|\[(?:PASS|FAIL)\]"
)
# 弱信号：ASCII 实词（首字符 + 至少 3 个后续字符）与中文二元组。
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{3,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_WORD_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "over",
    "must", "shall", "should", "true", "false", "none", "null",
    "test", "tests", "done", "pass", "fail", "ok",
}
# 证据通过/失败标记：EXIT=0 或 [PASS]；EXIT=非0 / [FAIL] 不算通过。
_PASS_MARKER_RE = re.compile(r"EXIT=0(?![0-9])|\[PASS\]")
_FAIL_MARKER_RE = re.compile(r"EXIT=[1-9]\d*(?!\d)|EXIT=FAIL|\[FAIL\]")

_WORD_MIN_RATIO = 0.60
_CJK_MIN_RATIO = 1.0 / 3.0
_QUOTE_MAX = 240


def load_registry(path: str) -> tuple[dict | None, str]:
    """只读加载台账。返回 (data, 错误描述)；成功时错误描述为空串。"""
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
    return data, ""


def split_acceptance(text: str) -> list[str]:
    """把 acceptance 拆成条目；编号 > 列表 > 分号 > 整段。"""
    text = str(text or "").strip()
    if not text:
        return []
    for rx in (_NUM_MARKER_RE, _BULLET_RE):
        marks = list(rx.finditer(text))
        if len(marks) >= 2:
            items: list[str] = []
            for i, mark in enumerate(marks):
                start = 0 if i == 0 else mark.start()
                end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
                seg = " ".join(text[start:end].split())
                if seg:
                    items.append(seg)
            if items:
                return items
    parts = [" ".join(p.split()) for p in re.split(r"[;；]", text)]
    parts = [p.strip("；; ") for p in parts if p.strip("；; ")]
    if len(parts) >= 2:
        return parts
    return [" ".join(text.split())]


def _cjk_bigrams(text: str) -> list[str]:
    """中文连续段内的二元组（不跨标点/英文），按出现序去重。"""
    out: list[str] = []
    seen: set[str] = set()
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in seen:
                seen.add(bg)
                out.append(bg)
    return out


def _strip_placeholders(text: str) -> str:
    """去掉 <task> 这类占位符，避免把参数占位当成实词。"""
    return re.sub(r"<[^>]{1,40}>", " ", text)


def extract_requirements(item: str) -> dict:
    """从一条 acceptance 提取 (强字面量, 是否需通过标记, ASCII 实词, 中文二元组)。"""
    clean = _strip_placeholders(item)
    literals: list[str] = []
    seen: set[str] = set()
    need_marker = False
    spans: list[tuple[int, int]] = []

    for match in _LITERAL_RE.finditer(clean):
        raw = match.group(0)
        tok = raw[1:-1].strip() if raw.startswith("`") and raw.endswith("`") else raw.strip()
        if not tok:
            continue
        spans.append(match.span())
        upper = tok.upper()
        if upper.startswith("EXIT="):
            value = tok[5:].strip()
            # EXIT=/[PASS]、EXIT=... 这类是「通过标记」要求，不是精确字面量。
            if not value or re.search(r"[/|｜]|或", value) or value in ("...", "…"):
                need_marker = True
            else:
                key = ("EXIT=" + value).casefold()
                if key not in seen:
                    seen.add(key)
                    literals.append("EXIT=" + value)
            continue
        if upper in ("[PASS]", "[FAIL]"):
            need_marker = True
            continue
        key = tok.casefold()
        if key not in seen:
            seen.add(key)
            literals.append(tok)

    # 字面量已消费的片段不再参与弱信号，避免把文件名碎片算成实词。
    chars = list(clean)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    rest = "".join(chars)

    words: list[str] = []
    for match in _WORD_RE.finditer(rest):
        word = match.group(0)
        if word.casefold() in _WORD_STOP or word in words:
            continue
        words.append(word)

    return {
        "literals": literals,
        "need_marker": need_marker,
        "words": words,
        "bigrams": _cjk_bigrams(rest),
    }


def _norm(text: str) -> str:
    """路径分隔符归一 + 小写，兼顾 Windows/Unix 写法差异。"""
    return text.replace("\\", "/").casefold()


def _literal_hit(text: str, token: str) -> bool:
    hay = _norm(text)
    needle = _norm(token)
    if needle.startswith(">="):
        m = re.fullmatch(r">=\s*(\d+)", needle)
        if m:
            return re.search(r">=\s*" + m.group(1) + r"(?!\d)", hay) is not None
    return needle in hay


def _self_reference_hit(token: str, evidence_path: str) -> bool:
    """acceptance 要求「证据文件 reports/evidence/<id>.md」时，文件自身即已满足。

    证据正文通常不会重复自己的完整路径；只要求文件名与证据文件同名。
    """
    base = os.path.basename(evidence_path.replace("\\", "/")).casefold()
    needle = os.path.basename(token.replace("\\", "/")).casefold()
    return bool(base) and needle == base


def _literal_ok(text: str, token: str, evidence_path: str) -> bool:
    return _literal_hit(text, token) or _self_reference_hit(token, evidence_path)


def _word_hit(text: str, word: str) -> bool:
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _score_line(line: str, req: dict) -> float:
    score = 0.0
    for token in req["literals"]:
        if _literal_hit(line, token):
            score += 4
    for word in req["words"]:
        if _word_hit(line, word):
            score += 1
    for bg in req["bigrams"]:
        if bg in line:
            score += 0.5
    if req["need_marker"] and _PASS_MARKER_RE.search(line):
        score += 2
    return score


def _clip(text: str, limit: int = _QUOTE_MAX) -> str:
    text = " ".join(text.strip().split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _is_echo_line(line: str, item_norm: str) -> bool:
    """判断某行是否只是把 acceptance 原文抄了一遍（不能算证据支撑）。

    口径：归一化后互为子串，且短侧长度 >= 长侧的 75%。这样能挡住
    「把验收条目原样贴进证据」的假支撑，又不误伤引用条目片段再加实测输出的行。
    """
    line_norm = _norm(line).strip()
    if not line_norm or not item_norm:
        return False
    short, long_ = sorted((len(line_norm), len(item_norm)))
    if item_norm in line_norm or line_norm in item_norm:
        return short >= 0.75 * long_
    return False


def _judge_item(item: str, evidence_text: str, evidence_path: str) -> dict:
    """判一条 acceptance：pass/fail，并给出最支持的一行证据。"""
    req = extract_requirements(item)
    item_norm = _norm(" ".join(item.split()))
    scan_lines = [line for line in evidence_text.splitlines()
                  if line.strip() and not _is_echo_line(line, item_norm)]
    scan_text = "\n".join(scan_lines)

    best_line = ""
    best_score = 0.0
    for line in scan_lines:
        score = _score_line(line, req)
        if score > best_score:
            best_line, best_score = line, score

    problems = 0
    if not (req["literals"] or req["need_marker"] or req["words"] or req["bigrams"]):
        problems += 1  # 抽不出可机检信号，不能算通过
    for token in req["literals"]:
        if not _literal_ok(scan_text, token, evidence_path):
            problems += 1
    if req["need_marker"] and not _PASS_MARKER_RE.search(scan_text):
        problems += 1
    if req["words"]:
        hits = sum(1 for word in req["words"] if _word_hit(scan_text, word))
        if hits < max(1, math.ceil(len(req["words"]) * _WORD_MIN_RATIO)):
            problems += 1
    if req["bigrams"]:
        hits = sum(1 for bg in req["bigrams"] if bg in scan_text)
        if hits < max(1, math.ceil(len(req["bigrams"]) * _CJK_MIN_RATIO)):
            problems += 1

    return {
        "item": item,
        "status": "pass" if problems == 0 else "fail",
        "evidence_quote": _clip(best_line) if best_score > 0 else "",
    }


def _resolve_evidence(task: dict, tid: str, root: str, override: str | None) -> str:
    """证据路径优先级：--evidence > 卡 evidence_path > reports/evidence/<id>.md。"""
    if override:
        return override if os.path.isabs(override) else os.path.join(root, override)
    raw = str(task.get("evidence_path") or "").strip()
    if raw:
        return raw if os.path.isabs(raw) else os.path.join(root, raw)
    return os.path.join(root, "reports", "evidence", "%s.md" % tid)


def _result(tid: str, verdict: str, items: list[dict]) -> dict:
    return {"id": tid, "verdict": verdict, "items": items}


def review(reg_path: str, tid: str, root: str | None = None,
           evidence: str | None = None) -> tuple[dict, str]:
    """只读复核一个任务。返回 (结果, 错误描述)；结果恒含 id/verdict/items。"""
    root = root or ROOT
    data, err = load_registry(reg_path)
    if err:
        return _result(tid, "fail", []), err

    task = next((t for t in data.get("tasks") or []
                 if isinstance(t, dict) and t.get("id") == tid), None)
    if task is None:
        return _result(tid, "fail", []), "任务不存在: %s" % tid

    acceptance = str(task.get("acceptance") or "").strip()
    items = split_acceptance(acceptance)
    if not items:
        return _result(tid, "fail", []), "acceptance 为空/缺失"

    ev_path = _resolve_evidence(task, tid, root, evidence)
    try:
        with open(ev_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        missing = [{"item": it, "status": "missing", "evidence_quote": ""}
                   for it in items]
        return _result(tid, "missing", missing), "证据文件缺失/不可读: %s (%s)" % (ev_path, e)

    if not text.strip():
        missing = [{"item": it, "status": "missing", "evidence_quote": ""}
                   for it in items]
        return _result(tid, "missing", missing), "证据文件为空: %s" % ev_path

    judged = [_judge_item(it, text, ev_path) for it in items]
    verdict = "pass" if all(x["status"] == "pass" for x in judged) else "fail"
    return _result(tid, verdict, judged), ""


def render(result: dict, json_mode: bool = False) -> str:
    if json_mode:
        return json.dumps(result, ensure_ascii=False, indent=2)
    lines = ["ID: %s" % result["id"], "VERDICT: %s" % result["verdict"], "ITEMS:"]
    for item in result["items"]:
        lines.append("[%s] %s" % (item["status"].upper(), item["item"]))
        if item["evidence_quote"]:
            lines.append("  quote: %s" % item["evidence_quote"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="reviewer 独立复核：按 acceptance 逐条核对证据（只读）")
    parser.add_argument("--id", required=True, help="任务 id，如 W-20261118-037")
    parser.add_argument("--root", default=ROOT, help="插件根目录（默认取脚本上级）")
    parser.add_argument("--registry", default=None,
                        help="台账路径（默认 <root>/state/registry.json）")
    parser.add_argument("--evidence", default=None, help="证据文件路径（默认按卡取）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 对象")
    args = parser.parse_args()

    reg = args.registry or os.path.join(args.root, "state", "registry.json")
    try:
        result, err = review(reg, args.id, root=args.root, evidence=args.evidence)
        output = render(result, args.json)
    except Exception as e:  # 任何未预期异常都不抛栈：只报一行错误，退出码 2
        print("ERROR: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2
    if err:
        print("提示: %s" % err, file=sys.stderr)
    print(output)
    return 0 if result["verdict"] == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
