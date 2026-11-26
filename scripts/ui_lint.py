#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ui_lint.py —— V4.0-P3 看板源码级 UISPEC 合规检查 + 死声明安全瘦身（stdlib only）。

为什么要它：Gate1 只测「默认态渲染后的 computed style」，抽屉/弹层/降级/hover 等状态里的违规测不到；
v2.html 尾部 15 层订单补丁里藏着彩色边框、表外色值、多层阴影、循环动画等（设计文档 §6.3.1）。
本工具直接读源码：<style> 全部规则 + HTML/JS 里的内联 style="…"，按 config/uispec_rules.json 判定。

子命令：
  lint  <html> [--rules uispec_rules.json] [--json] [--compare <基线.html>]
        --compare：逐规则对比违规数，任何一条比基线多 → 退出码 1（"只许降不许升"）
  slim  <html> --out <新.html> [--strip-comments] [--report <报告.json>]
        删除「被同上下文、同选择器的后续同名声明完全覆盖」的死声明（级联上永远不生效，删除零视觉变化），
        以及因此变空的规则 / @media。不合并规则、不改顺序、不改值。视觉零变化须再由像素比对确认。

库接口（factory_runner 调用）：parse_html_css / lint_html / lint_counts / slim_html / src_metrics
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES_CANDIDATES = [
    os.path.join(HERE, "..", "config", "uispec_rules.json"),
    os.path.join(HERE, "uispec_rules.json"),
]

STYLE_RE = re.compile(r"(<style[^>]*>)(.*?)(</style>)", re.S | re.I)
CONTAINER_AT = ("media", "supports", "layer", "container", "document")
OPAQUE_AT = ("font-face", "keyframes", "-webkit-keyframes", "page", "counter-style", "property", "font-feature-values")

# ------------------------------------------------------------------ CSS 解析（保留原文偏移，供精确删改）


class Decl:
    __slots__ = ("prop", "value", "important", "start", "end", "raw")

    def __init__(self, prop, value, important, start, end, raw):
        self.prop, self.value, self.important, self.start, self.end, self.raw = prop, value, important, start, end, raw


class Rule:
    __slots__ = ("selectors", "selector_text", "decls", "start", "end", "body_start", "body_end", "context", "nested", "order")

    def __init__(self):
        self.selectors: list[str] = []
        self.selector_text = ""
        self.decls: list[Decl] = []
        self.start = self.end = self.body_start = self.body_end = 0
        self.context: tuple = ()
        self.nested = False
        self.order = 0


class AtRule:
    __slots__ = ("name", "prelude", "start", "end", "body_start", "body_end", "children", "context", "raw_body")

    def __init__(self):
        self.name = ""
        self.prelude = ""
        self.start = self.end = self.body_start = self.body_end = 0
        self.children: list = []
        self.context: tuple = ()
        self.raw_body = ""


def _skip_comment(t: str, i: int) -> int:
    j = t.find("*/", i + 2)
    return len(t) if j < 0 else j + 2


def _skip_string(t: str, i: int) -> int:
    q = t[i]
    i += 1
    while i < len(t):
        c = t[i]
        if c == "\\":
            i += 2
            continue
        if c == q or c == "\n":
            return i + 1
        i += 1
    return i


def _scan_until(t: str, i: int, end: int, stops: str) -> int:
    """从 i 向后扫到第一个深度 0 的 stops 字符（跳过注释/字符串/括号内）；返回其下标或 end。"""
    depth = 0
    while i < end:
        c = t[i]
        if c == "/" and t.startswith("/*", i):
            i = _skip_comment(t, i)
            continue
        if c in "\"'":
            i = _skip_string(t, i)
            continue
        if c in "([":
            depth += 1
        elif c in ")]" and depth > 0:
            depth -= 1
        elif depth == 0 and c in stops:
            return i
        i += 1
    return end


def _match_brace(t: str, i: int, end: int) -> int:
    """t[i] == '{'；返回匹配的 '}' 下标。"""
    depth = 0
    while i < end:
        c = t[i]
        if c == "/" and t.startswith("/*", i):
            i = _skip_comment(t, i)
            continue
        if c in "\"'":
            i = _skip_string(t, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return end - 1


def _strip_comments(s: str) -> str:
    return re.sub(r"/\*.*?\*/", "", s, flags=re.S)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", _strip_comments(s)).strip()


def split_top(s: str, sep: str = ",") -> list[str]:
    out, cur, depth, i = [], [], 0, 0
    while i < len(s):
        c = s[i]
        if c in "\"'":
            j = _skip_string(s, i)
            cur.append(s[i:j])
            i = j
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth = max(0, depth - 1)
        if c == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [x for x in (y.strip() for y in out) if x]


def _parse_decls(t: str, s: int, e: int, rule: Rule) -> None:
    i = s
    while i < e:
        while i < e and (t[i].isspace() or t[i] == ";"):
            i += 1
        if i >= e:
            break
        if t.startswith("/*", i):
            i = _skip_comment(t, i)
            continue
        j = _scan_until(t, i, e, ";{}")
        if j < e and t[j] == "{":
            rule.nested = True               # CSS 嵌套：整条规则不参与瘦身
            k = _match_brace(t, j, e)
            i = k + 1
            continue
        raw = t[i:j]
        dend = j + 1 if (j < e and t[j] == ";") else j
        txt = _strip_comments(raw).strip()
        if ":" in txt:
            prop, val = txt.split(":", 1)
            val = val.strip()
            imp = False
            m = re.search(r"!\s*important\s*$", val, re.I)
            if m:
                imp = True
                val = val[: m.start()].strip()
            p = prop.strip()
            rule.decls.append(Decl(p if p.startswith("--") else p.lower(), val, imp, i, dend, raw))
        i = dend


def parse_css(t: str, s: int = 0, e: int | None = None, context: tuple = (), counter: list | None = None) -> list:
    e = len(t) if e is None else e
    counter = counter if counter is not None else [0]
    nodes: list = []
    i = s
    while i < e:
        c = t[i]
        if c.isspace():
            i += 1
            continue
        if t.startswith("/*", i):
            i = _skip_comment(t, i)
            continue
        if t.startswith("<!--", i) or t.startswith("-->", i):
            i += 4 if t.startswith("<!--", i) else 3
            continue
        if c == "}":
            i += 1
            continue
        if c == "@":
            j = _scan_until(t, i, e, ";{")
            name_m = re.match(r"@([\w-]+)", t[i:j])
            node = AtRule()
            node.name = (name_m.group(1) if name_m else "").lower()
            node.prelude = _norm(t[i + len(node.name) + 1:j])
            node.start, node.context = i, context
            if j >= e or t[j] == ";":
                node.end = min(e, j + 1)
                nodes.append(node)
                i = node.end
                continue
            k = _match_brace(t, j, e)
            node.body_start, node.body_end, node.end = j + 1, k, k + 1
            node.raw_body = t[j + 1:k]
            if node.name in CONTAINER_AT:
                node.children = parse_css(t, j + 1, k, context + (f"@{node.name} {node.prelude}",), counter)
            nodes.append(node)
            i = k + 1
            continue
        j = _scan_until(t, i, e, "{;")
        if j >= e or t[j] == ";":
            i = j + 1
            continue
        k = _match_brace(t, j, e)
        r = Rule()
        r.selector_text = _norm(t[i:j])
        r.selectors = [_norm(x) for x in split_top(_strip_comments(t[i:j]))]
        r.start, r.body_start, r.body_end, r.end = i, j + 1, k, k + 1
        r.context = context
        counter[0] += 1
        r.order = counter[0]
        _parse_decls(t, j + 1, k, r)
        nodes.append(r)
        i = k + 1
    return nodes


def iter_rules(nodes):
    for n in nodes:
        if isinstance(n, Rule):
            yield n
        elif isinstance(n, AtRule) and n.children:
            yield from iter_rules(n.children)


def iter_at(nodes):
    for n in nodes:
        if isinstance(n, AtRule):
            yield n
            if n.children:
                yield from iter_at(n.children)


def parse_html_css(html: str) -> list[dict]:
    """-> [{"css": 文本, "offset": 在 html 中的起点, "nodes": [...]}]（每个 <style> 一项）"""
    out = []
    for m in STYLE_RE.finditer(html):
        css = m.group(2)
        out.append({"css": css, "offset": m.start(2), "nodes": parse_css(css)})
    return out


# ------------------------------------------------------------------ 规则表


def load_rules(path: str = "") -> dict:
    for p in ([path] if path else []) + DEFAULT_RULES_CANDIDATES:
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError("找不到 uispec_rules.json")


NAMED_COLORS = {"white": "#ffffff", "black": "#000000", "red": "#ff0000", "green": "#008000", "blue": "#0000ff",
                "gray": "#808080", "grey": "#808080", "orange": "#ffa500", "yellow": "#ffff00"}
COLOR_TOKEN_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)|\b(?:white|black|red|green|blue|gray|grey|orange|yellow)\b", re.I)
COLOR_PROPS_RE = re.compile(r"^(?:color|background(?:-color|-image)?|border(?:-(?:top|right|bottom|left|block|inline)(?:-(?:start|end))?)?(?:-color)?"
                            r"|outline(?:-color)?|fill|stroke|box-shadow|text-shadow|text-decoration(?:-color)?|caret-color|accent-color"
                            r"|column-rule(?:-color)?|--[\w-]+)$")
BORDER_PROPS_RE = re.compile(r"^border(?:-(?:top|right|bottom|left|block|inline)(?:-(?:start|end))?)?(?:-width)?$")
DUR_RE = re.compile(r"(?<![\w.-])(\d*\.?\d+)(ms|s)\b")
PX_RE = re.compile(r"(-?\d*\.?\d+)px\b")
MEDIA_W_RE = re.compile(r"(min|max)-width\s*:\s*(\d+(?:\.\d+)?)px")


def _hex6(tok: str) -> str | None:
    t = tok.strip().lower()
    if t in NAMED_COLORS:
        return NAMED_COLORS[t]
    if t.startswith("#"):
        h = t[1:]
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h[:3])
        elif len(h) in (6, 8):
            h = h[:6]
        else:
            return None
        return "#" + h
    m = re.match(r"rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)", t)
    if m:
        return "#%02x%02x%02x" % tuple(min(255, int(x)) for x in m.groups())
    return None


def _is_transparent_color(tok: str) -> bool:
    t = tok.strip().lower()
    m = re.match(r"rgba\([^,]+,[^,]+,[^,]+,\s*(0?\.?0+)\s*\)", t)
    return bool(m) or (t.startswith("#") and len(t) == 9 and t.endswith("00"))


class Ctx:
    def __init__(self, rules: dict):
        sc = rules.get("semantic_colors") or {}
        self.semantic = {c.lower() for v in sc.values() for c in (v if isinstance(v, list) else [v])}
        self.grays = {c.lower() for c in rules.get("grays") or []}
        self.white = {(rules.get("white") or "#ffffff").lower()}
        self.allowed_colors = self.semantic | self.grays | self.white
        self.gradients = {_norm(g).lower().replace(" ", "") for g in (rules.get("z1_gradients") or {}).values()}
        self.font_sizes = {float(x) for x in rules.get("font_sizes_px") or []}
        self.weights = {int(x) for x in rules.get("font_weights") or []}
        self.radii = {float(x) for x in rules.get("radii_px") or []}
        self.durs = {float(x) for x in rules.get("durations_ms") or []}
        self.shadow = _norm(rules.get("shadow") or "").lower().replace(" ", "")
        self.border_exempt = {_norm(s) for s in rules.get("border_exempt_selectors") or []}
        self.bp_allowed = {1600.0, 1599.0, 1599.98, 1200.0, 1199.0, 1199.98, 768.0, 767.0, 767.98}
        stacks = rules.get("font_stacks") or {}
        fams = set()
        for v in stacks.values():
            for f in split_top(v):
                fams.add(f.strip().strip("'\"").lower())
        self.font_families = fams
        self.uispec_tokens = re.compile(r"^--(?:g-\d+|c-(?:red|amber|blue|green)|grad-(?:green|yellow|red)|sp-\d|r-(?:card|ctl|chip)|fs-(?:\d+|hero)|shadow-1|dur-\d|ease-std|font-(?:num|ui))$")


# ------------------------------------------------------------------ Lint


def _line_of(html: str, pos: int) -> int:
    return html.count("\n", 0, pos) + 1


def _decl_violations(cx: Ctx, prop: str, val: str, selectors: list[str], where: str) -> list[tuple[str, str]]:
    """单条声明 -> [(rule, 说明)]。where ∈ css|inline。"""
    v = val.strip()
    vl = v.lower()
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"var\(\s*(--[\w-]+)", v):
        if not cx.uispec_tokens.match(m.group(1)):
            out.append(("legacy_token_use", m.group(1)))
    if "var(" in vl and re.fullmatch(r"var\([^()]*\)", vl):
        return out                                   # 纯 token 引用：值由 token 定义处负责
    if prop == "font-size":
        for m in PX_RE.finditer(vl):
            if float(m.group(1)) not in cx.font_sizes and not vl.startswith("clamp("):
                out.append(("font_size", f"{prop}:{v}"))
                break
        if not PX_RE.search(vl) and re.search(r"\d(?:em|rem|%|pt|vw)\b", vl) and not vl.startswith("clamp("):
            out.append(("font_size_unit", f"{prop}:{v}"))
    elif prop == "font-weight":
        w = {"normal": 400, "bold": 700, "bolder": 700, "lighter": 300}.get(vl)
        if w is None and re.fullmatch(r"\d{3}", vl):
            w = int(vl)
        if w is not None and w not in cx.weights:
            out.append(("font_weight", f"{prop}:{v}"))
    elif prop in ("border-radius",) or prop.startswith("border-") and prop.endswith("-radius"):
        for m in PX_RE.finditer(vl):
            if float(m.group(1)) not in cx.radii:
                out.append(("radius", f"{prop}:{v}"))
                break
        if re.search(r"\d%", vl) and "50%" not in vl:
            out.append(("radius", f"{prop}:{v}"))
    if BORDER_PROPS_RE.match(prop):
        if not any(_norm(s) in cx.border_exempt for s in selectors):
            if vl not in ("0", "none", "0 none", "none 0", "0px", "initial", "unset", "inherit", "revert") and not vl.startswith("0 ") \
                    and not re.fullmatch(r"0(px)?(\s+\w+)?(\s+\S+)?", vl):
                if re.search(r"(?<![\d.])(?:[1-9]\d*(?:\.\d+)?|0?\.\d*[1-9])px|\bthin\b|\bmedium\b|\bthick\b", vl) or (
                        prop.endswith("-width") is False and re.search(r"\b(solid|dashed|dotted|double)\b", vl)):
                    out.append(("border_width", f"{prop}:{v}"))
                    for ct in COLOR_TOKEN_RE.findall(re.sub(r"var\([^()]*\)", " ", v)):
                        hx = _hex6(ct)
                        if hx and hx in cx.semantic | {"#fca5a5", "#93c5fd", "#86efac", "#bbf7d0", "#fcd34d"} or (hx and hx not in cx.grays | cx.white):
                            out.append(("colored_border", f"{prop}:{v}"))
                            break
    if prop in ("box-shadow",) and vl not in ("none", "0 0 0 transparent") and vl.replace(" ", "") != cx.shadow:
        if not vl.startswith("var("):
            out.append(("shadow", f"{prop}:{v}"))
    if prop.startswith("transition") or prop.startswith("animation"):
        for m in DUR_RE.finditer(vl):
            ms = float(m.group(1)) * (1000 if m.group(2) == "s" else 1)
            if ms not in cx.durs:
                out.append(("duration", f"{prop}:{v}"))
                break
        if prop.startswith("animation") and "infinite" in vl:
            out.append(("infinite_animation", f"{prop}:{v}"))
    if COLOR_PROPS_RE.match(prop):
        compact = vl.replace(" ", "")
        if "gradient(" in vl and compact in cx.gradients:
            pass
        else:
            bad = []
            for ct in COLOR_TOKEN_RE.findall(re.sub(r"var\([^()]*\)", " ", v)):
                if _is_transparent_color(ct):
                    continue
                hx = _hex6(ct)
                if hx and hx not in cx.allowed_colors:
                    bad.append(ct)
            if bad:
                rule = "color_gradient" if "gradient(" in vl else "color"
                out.append((rule, f"{prop}:{v[:120]}  ← {', '.join(sorted(set(bad))[:4])}"))
    if prop.startswith("--") and not cx.uispec_tokens.match(prop) and where == "css":
        out.append(("legacy_token_def", prop))
    return out


INLINE_STYLE_RE = re.compile(r"""style\s*=\s*(\\?["'])(.*?)\1""", re.S)


def lint_html(html: str, rules: dict | None = None) -> dict:
    """-> {"rules": {rule: {"count": n, "samples": [...]}}, "stats": {...}}"""
    cx = Ctx(rules or load_rules())
    found: dict[str, list] = collections.defaultdict(list)

    def add(rule, line, msg):
        found[rule].append({"line": line, "msg": msg[:220]})

    blocks = parse_html_css(html)
    css_bytes = 0
    n_rules = 0
    sel_count = collections.Counter()
    for b in blocks:
        css, off = b["css"], b["offset"]
        css_bytes += len(css.encode("utf-8"))
        for r in iter_rules(b["nodes"]):
            n_rules += 1
            for s in r.selectors:
                sel_count[(r.context, s)] += 1
            for d in r.decls:
                ln = _line_of(html, off + d.start)
                if d.important:
                    add("important", ln, f"{r.selector_text[:60]} {{{d.prop}: {d.value[:60]} !important}}")
                for rule, msg in _decl_violations(cx, d.prop, d.value, r.selectors, "css"):
                    add(rule, ln, f"{r.selector_text[:70]} → {msg}")
        for a in iter_at(b["nodes"]):
            ln = _line_of(html, off + a.start)
            if a.name == "media":
                for mm in MEDIA_W_RE.finditer(a.prelude):
                    if float(mm.group(2)) not in cx.bp_allowed:
                        add("breakpoint", ln, f"@media {a.prelude}")
                        break
            elif a.name == "font-face":
                fam = re.search(r"font-family\s*:\s*([^;]+)", a.raw_body or "", re.I)
                fname = (fam.group(1).strip().strip("'\"") if fam else "?")
                size = len(a.raw_body or "")
                if fname.lower() not in cx.font_families:
                    add("font_face", ln, f"@font-face {fname}（{size // 1024} KB，不在 UISPEC 字体栈）")
            elif a.name in ("keyframes", "-webkit-keyframes"):
                pass
    for (ctx, s), n in sel_count.items():
        if n > 1:
            found["dup_selector"].append({"line": 0, "msg": f"{s[:80]} ×{n}" + (f" @ {' / '.join(ctx)[:60]}" if ctx else "")})
    for m in INLINE_STYLE_RE.finditer(html):
        body = m.group(2)
        if "'+" in body or '"+' in body:
            body = re.sub(r"['\"]\s*\+.*?\+\s*['\"]", "X", body)
        ln = _line_of(html, m.start())
        for part in split_top(body, ";"):
            if ":" not in part:
                continue
            p, v = part.split(":", 1)
            p = p.strip().lower()
            if not re.fullmatch(r"-?[a-z][a-z-]*", p):
                continue
            for rule, msg in _decl_violations(cx, p, v.strip(), [], "inline"):
                add("inline_" + rule, ln, f"style=\"{p}:{v.strip()[:60]}\" → {msg}")
    dead = dead_declarations(blocks)
    for d in dead["items"]:
        found["dead_decl"].append({"line": _line_of(html, d["abs"]), "msg": d["msg"]})
    out = {k: {"count": len(v), "samples": v[:12]} for k, v in sorted(found.items())}
    stats = {"css_bytes": css_bytes, "css_rules": n_rules, "style_blocks": len(blocks),
             "font_face_bytes": sum(len(a.raw_body or "") for b in blocks for a in iter_at(b["nodes"]) if a.name == "font-face"),
             "comment_bytes": sum(len(m.group(0)) for b in blocks for m in re.finditer(r"/\*.*?\*/", b["css"], re.S)),
             "media_queries": sum(1 for b in blocks for a in iter_at(b["nodes"]) if a.name == "media"),
             "distinct_breakpoints": sorted({float(mm.group(2)) for b in blocks for a in iter_at(b["nodes"]) if a.name == "media" for mm in MEDIA_W_RE.finditer(a.prelude)}),
             "dead_decl_bytes": dead["bytes"]}
    return {"rules": out, "stats": stats}


GATING_RULES = ("font_size", "font_weight", "radius", "border_width", "colored_border", "shadow", "duration",
                "infinite_animation", "color", "color_gradient", "breakpoint", "font_face", "important",
                "inline_font_size", "inline_color", "inline_border_width", "inline_colored_border", "inline_shadow")


def lint_counts(report: dict) -> dict:
    c = {k: v["count"] for k, v in report["rules"].items()}
    c["total"] = sum(c.get(k, 0) for k in GATING_RULES)
    return c


def src_metrics(html: str, rules: dict | None = None) -> dict:
    """给内核断言用：{"lint.<rule>": n, "lint.total": n, "src.css_bytes": n, "src.font_face_bytes": n, ...}"""
    rep = lint_html(html, rules)
    out = {f"lint.{k}": v for k, v in lint_counts(rep).items()}
    for k in GATING_RULES + ("dead_decl", "dup_selector", "legacy_token_def", "legacy_token_use"):
        out.setdefault(f"lint.{k}", 0)
    out["src.css_bytes"] = rep["stats"]["css_bytes"]
    out["src.font_face_bytes"] = rep["stats"]["font_face_bytes"]
    out["src.css_bytes_no_fonts"] = rep["stats"]["css_bytes"] - rep["stats"]["font_face_bytes"]
    out["src.css_rules"] = rep["stats"]["css_rules"]
    out["src.media_queries"] = rep["stats"]["media_queries"]
    out["src.breakpoints"] = len(rep["stats"]["distinct_breakpoints"])
    out["src.html_bytes"] = len(html.encode("utf-8"))
    return out


# ------------------------------------------------------------------ 死声明分析与安全瘦身

UNSAFE_PROP_RE = re.compile(r"^(?:-webkit-|-moz-|-ms-|-o-)|^(?:line-clamp|text-wrap|field-sizing|interpolate-size|view-transition-name)$")


def _ctx_key(ctx: tuple) -> tuple:
    return tuple(c.lower() for c in ctx)


def dead_declarations(blocks: list[dict]) -> dict:
    """声明 D（规则 R、上下文 C、选择器表 S）是死的 ⇔ 对 S 中每个选择器 s，都存在一条**更靠后**的规则 R'：
    选择器表含 s（原文规范化后完全相同 ⇒ 特异性相同）、上下文 == C 或为无条件顶层、声明了同名属性、
    重要性不低于 D，且该同名声明本身值不含厂商前缀（防「后者不被解析 → 前者回退生效」）。
    跳过：嵌套规则、厂商前缀属性/值、同一规则块内的重复（那是有意的回退写法）、opaque at-rule 内的一切。"""
    items, total = [], 0
    for b in blocks:
        rules = list(iter_rules(b["nodes"]))
        later: dict[tuple, list] = collections.defaultdict(list)   # (selector, prop) -> [(order, ctxkey, important)]
        rules = [r for r in rules if not any(c.lower().startswith("@layer") for c in r.context)]   # 层叠层顺序≠源码顺序，整体跳过
        for r in rules:
            if r.nested:
                continue
            for d in r.decls:
                if UNSAFE_PROP_RE.search(d.prop) or re.search(r"-webkit-|-moz-|-ms-", d.value):
                    continue
                for s in r.selectors:
                    later[(s, d.prop)].append((r.order, _ctx_key(r.context), d.important))
        for r in rules:
            if r.nested:
                continue
            ck = _ctx_key(r.context)
            for d in r.decls:
                if UNSAFE_PROP_RE.search(d.prop):
                    continue
                ok_all = True
                for s in r.selectors:
                    hit = False
                    for (o, c2, imp2) in later.get((s, d.prop), ()):
                        if o > r.order and (c2 == ck or c2 == ()) and (imp2 or not d.important):
                            hit = True
                            break
                    if not hit:
                        ok_all = False
                        break
                if ok_all and r.selectors:
                    total += d.end - d.start
                    items.append({"rule": r, "decl": d, "abs": b["offset"] + d.start,
                                  "msg": f"{r.selector_text[:70]} {{{d.prop}: {d.value[:50]}}} 被后续同选择器覆盖"})
    return {"items": items, "bytes": total}


def slim_html(html: str, strip_comments: bool = False) -> tuple[str, dict]:
    """返回 (新 html, 报告)。只删死声明 + 因此变空的规则/@media（+ 可选注释），不动其余一个字节。"""
    blocks = parse_html_css(html)
    pieces = []
    cursor = 0
    report = {"removed_decls": 0, "removed_rules": 0, "removed_media": 0, "removed_comment_bytes": 0,
              "bytes_before": len(html.encode("utf-8"))}
    for b in blocks:
        css, off = b["css"], b["offset"]
        dead = dead_declarations([b])
        kill: list[tuple[int, int]] = [(x["decl"].start, x["decl"].end) for x in dead["items"]]
        report["removed_decls"] += len(kill)
        dead_by_rule = collections.Counter(id(x["rule"]) for x in dead["items"])

        def _walk(nodes) -> bool:
            """返回该节点列表是否全部被删空（用于删空 @media）。"""
            all_gone = True
            for n in nodes:
                if isinstance(n, Rule):
                    if n.decls and dead_by_rule.get(id(n), 0) == len(n.decls) and not n.nested:
                        kill.append((n.start, n.end))
                        report["removed_rules"] += 1
                    else:
                        all_gone = False
                elif isinstance(n, AtRule) and n.name in CONTAINER_AT and n.children:
                    if _walk(n.children):
                        kill.append((n.start, n.end))
                        report["removed_media"] += 1
                    else:
                        all_gone = False
                else:
                    all_gone = False
            return all_gone

        _walk(b["nodes"])
        if strip_comments:
            for m in re.finditer(r"/\*.*?\*/", css, re.S):
                kill.append((m.start(), m.end()))
                report["removed_comment_bytes"] += m.end() - m.start()
        kill.sort()
        merged: list[list[int]] = []
        for s, e in kill:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        new_css, cur = [], 0
        for s, e in merged:
            new_css.append(css[cur:s])
            cur = e
        new_css.append(css[cur:])
        new_css_s = "".join(new_css)
        new_css_s = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", new_css_s)
        pieces.append(html[cursor:off])
        pieces.append(new_css_s)
        cursor = off + len(css)
    pieces.append(html[cursor:])
    out = "".join(pieces)
    report["bytes_after"] = len(out.encode("utf-8"))
    report["saved_bytes"] = report["bytes_before"] - report["bytes_after"]
    return out, report


def archive_comments(html: str) -> str:
    """CSS 注释存档：每条注释带所在行号与紧随其后的选择器，便于删注释后仍能查到订单号与改法说明。"""
    lines = ["# v2.html CSS 注释存档（ui_lint slim --strip-comments 自动生成）", ""]
    for b in parse_html_css(html):
        css, off = b["css"], b["offset"]
        for m in re.finditer(r"/\*(.*?)\*/", css, re.S):
            nxt = css[m.end():m.end() + 200].strip().split("{", 1)[0].strip()[:80]
            lines.append(f"## L{_line_of(html, off + m.start())} → `{nxt}`")
            lines.append(m.group(1).strip())
            lines.append("")
    return "\n".join(lines) + "\n"


def _decl_table(html: str) -> dict:
    tab = collections.defaultdict(list)          # (selector, prop) -> [(order, ctxkey, important, value)]
    order = 0
    for b in parse_html_css(html):
        for r in iter_rules(b["nodes"]):
            if r.nested:
                continue
            for d in r.decls:
                order += 1
                for s in r.selectors:
                    tab[(s, d.prop)].append((order, _ctx_key(r.context), d.important, d.value))
    return tab


def _winner(decls: list, active: frozenset):
    best = None
    for o, c, imp, v in decls:
        if c == () or c in active:
            if best is None or (imp, o) > (best[0], best[1]):
                best = (imp, o, v)
    return None if best is None else (best[2], best[0])


def verify_slim_equivalence(before: str, after: str, max_ctx: int = 10) -> dict:
    """静态等价证明：对每个 (选择器, 属性)，枚举其出现过的条件上下文（@media…）的**所有激活组合**，
    比较级联胜出的 (值, important) —— 前后完全一致才算等价。上下文 > max_ctx 个的键只比较全激活/全不激活两种。"""
    A, B = _decl_table(before), _decl_table(after)
    diffs, checked = [], 0
    for k in set(A) | set(B):
        da, db = A.get(k, []), B.get(k, [])
        ctxs = sorted({c for _, c, _, _ in da + db if c})
        if len(ctxs) <= max_ctx:
            combos = [frozenset(ctxs[i] for i in range(len(ctxs)) if m >> i & 1) for m in range(1 << len(ctxs))]
        else:
            combos = [frozenset(), frozenset(ctxs)]
        for act in combos:
            checked += 1
            if _winner(da, act) != _winner(db, act):
                diffs.append({"selector": k[0], "prop": k[1], "active": sorted(" & ".join(x) for x in act),
                              "before": _winner(da, act), "after": _winner(db, act)})
                break
    return {"equivalent": not diffs, "keys": len(set(A) | set(B)), "combos_checked": checked, "diff_samples": diffs[:10]}


# ------------------------------------------------------------------ 旧 token → UISPEC token 等值改名（零视觉变化）


def _norm_val(v: str) -> str:
    return re.sub(r"\s+", "", v.strip().lower())


def plan_retoken(html: str, rules: dict | None = None) -> dict:
    """找出「值与某 UISPEC token 完全相等」的旧 token，计划把 var(--旧) 全部改成 var(--新) 并删掉旧定义。
    安全条件（任一不满足就跳过该 token，并写明原因）：
      1) 新旧 token 都只在 :root 无条件定义一次（无 @media / 其他选择器重定义 → 任何元素上计算值必然相同）；
      2) 旧 token 名在 var() 之外不以字符串出现（防 JS getPropertyValue/setProperty 读写）；
      3) 新 token 名同样不被 JS 改写。"""
    cx = Ctx(rules or load_rules())
    defs: dict[str, list] = collections.defaultdict(list)
    for b in parse_html_css(html):
        for r in iter_rules(b["nodes"]):
            for d in r.decls:
                if d.prop.startswith("--"):
                    defs[d.prop].append({"sel": r.selector_text, "ctx": r.context, "val": d.value, "abs": b["offset"] + d.start, "end": b["offset"] + d.end})
    root_only = {k: v[0] for k, v in defs.items() if len(v) == 1 and v[0]["sel"] == ":root" and not v[0]["ctx"]}
    by_val: dict[str, list[str]] = collections.defaultdict(list)
    for k, d in root_only.items():
        if cx.uispec_tokens.match(k):
            by_val[_norm_val(d["val"])].append(k)
    prefer = ("--c-", "--g-", "--r-", "--sp-", "--fs-", "--dur-", "--shadow-", "--ease-")
    plan, skipped = [], []
    for k, v in sorted(defs.items()):
        if cx.uispec_tokens.match(k):
            continue
        if k not in root_only:
            skipped.append({"token": k, "why": "非 :root 唯一无条件定义"})
            continue
        cands = sorted(by_val.get(_norm_val(v[0]["val"]), []), key=lambda t: next((i for i, p in enumerate(prefer) if t.startswith(p)), 99))
        if not cands:
            skipped.append({"token": k, "why": f"值 {v[0]['val']} 没有等值的 UISPEC token（换值即视觉变化，留给人工/工厂单）"})
            continue
        tgt = cands[0]
        bare = re.compile(r"(?<![\w-])" + re.escape(k) + r"(?![\w-])")
        uses_var = len(re.findall(r"var\(\s*" + re.escape(k) + r"\s*[,)]", html))
        uses_all = len(bare.findall(html))
        if uses_all - uses_var - 1 > 0:
            skipped.append({"token": k, "why": f"var() 之外还有 {uses_all - uses_var - 1} 处引用（可能被 JS 读写）"})
            continue
        if re.search(r"(?:setProperty|getPropertyValue)\(\s*['\"]" + re.escape(tgt) + r"['\"]", html):
            skipped.append({"token": k, "why": f"目标 {tgt} 被 JS 改写"})
            continue
        plan.append({"from": k, "to": tgt, "value": v[0]["val"], "uses": uses_var, "def_abs": v[0]["abs"], "def_end": v[0]["end"]})
    return {"plan": plan, "skipped": skipped}


def apply_retoken(html: str, rules: dict | None = None) -> tuple[str, dict]:
    p = plan_retoken(html, rules)
    out = html
    for it in sorted(p["plan"], key=lambda x: -x["def_abs"]):     # 先删定义（从后往前，偏移不乱）
        out = out[:it["def_abs"]] + out[it["def_end"]:]
    n = 0
    for it in p["plan"]:
        out, c = re.subn(r"var\(\s*" + re.escape(it["from"]) + r"(\s*[,)])", "var(" + it["to"] + r"\1", out)
        n += c
    out = re.sub(r":root\s*\{\s*\}", "", out)
    return out, {"renamed_tokens": len(p["plan"]), "replaced_uses": n, "plan": [{k: v for k, v in x.items() if k not in ("def_abs", "def_end")} for x in p["plan"]],
                 "skipped": p["skipped"]}


def verify_retoken(before: str, after: str, rules: dict | None = None) -> dict:
    """证明：after 中不再出现被改名的旧 token；每个替换目标在 after 中仍是 :root 唯一定义且值与旧 token 原值相等；
    除 var(--旧)→var(--新) 与删定义外，文档其余字节不变（把 after 里的新名还原回旧名、补回定义后应与 before 一致）。"""
    _, rep = apply_retoken(before, rules)
    errs = []
    defs_after = collections.defaultdict(list)
    for b in parse_html_css(after):
        for r in iter_rules(b["nodes"]):
            for d in r.decls:
                if d.prop.startswith("--"):
                    defs_after[d.prop].append((r.selector_text, r.context, d.value))
    for it in rep["plan"]:
        if re.search(r"var\(\s*" + re.escape(it["from"]) + r"\s*[,)]", after):
            errs.append(f"{it['from']} 仍有引用")
        d = defs_after.get(it["to"]) or []
        if len(d) != 1 or d[0][0] != ":root" or d[0][1] or _norm_val(d[0][2]) != _norm_val(it["value"]):
            errs.append(f"{it['to']} 定义不唯一或值不等于 {it['value']}")
    redo, _ = apply_retoken(before, rules)
    if redo != after:
        errs.append("after 不是 before 的确定性改名产物")
    return {"equivalent": not errs, "errors": errs}


# ------------------------------------------------------------------ CLI


def _print_report(rep: dict, base: dict | None = None) -> None:
    st = rep["stats"]
    print(f"CSS {st['css_bytes']:,} B · 规则 {st['css_rules']} · @media {st['media_queries']} · 断点 {len(st['distinct_breakpoints'])} 种 {st['distinct_breakpoints']}")
    print(f"内嵌字体 {st['font_face_bytes']:,} B · 注释 {st['comment_bytes']:,} B · 死声明 {st['dead_decl_bytes']:,} B")
    print()
    bc = lint_counts(base) if base else {}
    c = lint_counts(rep)
    keys = sorted(set(c) | set(bc), key=lambda k: (k not in GATING_RULES and k != "total", k))
    print(f"{'规则':<24}{'现在':>6}" + (f"{'基线':>8}{'变化':>8}" if base else ""))
    for k in keys:
        line = f"{k:<24}{c.get(k, 0):>6}"
        if base:
            dlt = c.get(k, 0) - bc.get(k, 0)
            line += f"{bc.get(k, 0):>8}{('+' if dlt > 0 else '') + str(dlt):>8}" + ("  ← 升了" if dlt > 0 and (k in GATING_RULES or k == "total") else "")
        print(line)
    print()
    for k in GATING_RULES:
        v = rep["rules"].get(k)
        if v and v["count"]:
            print(f"[{k}] ×{v['count']}")
            for s in v["samples"][:4]:
                print(f"    L{s['line']}: {s['msg']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="看板源码 UISPEC 合规检查 / 死声明瘦身")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("lint")
    lp.add_argument("html")
    lp.add_argument("--rules", default="")
    lp.add_argument("--json", action="store_true")
    lp.add_argument("--compare", default="")
    sp = sub.add_parser("slim")
    sp.add_argument("html")
    sp.add_argument("--out", required=True)
    sp.add_argument("--strip-comments", action="store_true")
    sp.add_argument("--report", default="")
    sp.add_argument("--rules", default="")
    sp.add_argument("--retoken", action="store_true", help="同时把值相等的旧 token 改名为 UISPEC token（零视觉变化）")
    sp.add_argument("--comments-out", default="", help="把删掉的 CSS 注释原文存档到该文件（订单号说明不丢）")
    a = ap.parse_args(argv)
    rules = load_rules(a.rules)
    src = open(a.html, encoding="utf-8").read()
    if a.cmd == "lint":
        rep = lint_html(src, rules)
        base = lint_html(open(a.compare, encoding="utf-8").read(), rules) if a.compare else None
        if a.json:
            print(json.dumps({"report": rep, "counts": lint_counts(rep), "baseline_counts": lint_counts(base) if base else None}, ensure_ascii=False, indent=1))
        else:
            _print_report(rep, base)
        if base:
            c, bc = lint_counts(rep), lint_counts(base)
            up = [k for k in list(GATING_RULES) + ["total"] if c.get(k, 0) > bc.get(k, 0)]
            if up:
                print(f"\n违规数上升：{up}", file=sys.stderr)
                return 1
        return 0
    if a.strip_comments and a.comments_out:
        with open(a.comments_out, "w", encoding="utf-8") as f:
            f.write(archive_comments(src))
    out, rep = slim_html(src, a.strip_comments)
    eq = verify_slim_equivalence(src, out)
    rep["static_equivalence"] = eq
    if not eq["equivalent"]:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        print("静态等价校验失败，未写出文件", file=sys.stderr)
        return 2
    if a.retoken:
        out2, rt = apply_retoken(out, rules)
        vr = verify_retoken(out, out2, rules)
        rep["retoken"] = {**{k: v for k, v in rt.items()}, "verify": vr}
        if not vr["equivalent"]:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
            print("token 改名校验失败，未写出文件", file=sys.stderr)
            return 2
        out = out2
        rep["bytes_after"] = len(out.encode("utf-8"))
        rep["saved_bytes"] = rep["bytes_before"] - rep["bytes_after"]
    with open(a.out, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    rep["lint_before"] = lint_counts(lint_html(src, rules))
    rep["lint_after"] = lint_counts(lint_html(out, rules))
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k not in ("lint_before", "lint_after")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
