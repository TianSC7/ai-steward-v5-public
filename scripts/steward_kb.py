# -*- coding: utf-8 -*-
"""大管家私有知识库（markdown 可读 + 关键词检索 + 自动索引）。

为什么不用 mem0/Letta：
  它们的价值在语义检索，但依赖 LLM/embedding 服务；管家的知识量是几十到几百条，
  且必须可读、可审计、可随仓库走。这里采用它们的「事实卡」模型（id/tags/confidence/
  source/时间），存储用 markdown，检索用标签+关键词打分，零依赖、零外传。

目录：
  knowledge/entries/K-YYYYMMDD-NNN.md     事实/模式/偏好
  knowledge/incidents/INC-YYYYMMDD-NNN.md 事故复盘（现象/根因/修复/防复发）
  knowledge/INDEX.md                       自动生成索引

用法：
  python -X utf8 steward_kb.py add --title ... --body ... --tags hook,permission \
      --type fact --confidence high --source "实测 2026-11-17"
  python -X utf8 steward_kb.py add --title ... --body-file note.md --type incident
  python -X utf8 steward_kb.py search --query "权限 钩子" --limit 5
  python -X utf8 steward_kb.py match --text "worker 写文件被拦" --limit 3
  python -X utf8 steward_kb.py index
  python -X utf8 steward_kb.py list [--type fact] [--tag hook]
  python -X utf8 steward_kb.py stats
  python -X utf8 steward_kb.py verify

退出码：0 成功；2 参数/解析/校验问题。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KB = os.path.join(ROOT, "knowledge")
ENTRIES = os.path.join(KB, "entries")
INCIDENTS = os.path.join(KB, "incidents")
INDEX = os.path.join(KB, "INDEX.md")
LEGAL_TYPES = ("fact", "pattern", "preference", "incident")
LEGAL_CONF = ("high", "medium", "low")


def now_iso(raw: str | None = None) -> str:
    if raw:
        return datetime.datetime.fromisoformat(raw.strip()).replace(microsecond=0).isoformat()
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _tag_list(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not raw:
        return []
    return [x.strip() for x in re.split(r"[,，\s]+", str(raw)) if x.strip()]


def parse_entry(path: str) -> dict:
    text = open(path, encoding="utf-8").read()
    meta: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            raw = parts[1]
            body = parts[2].lstrip("\n")
            for line in raw.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    tags = _tag_list(meta.get("tags", ""))
    if isinstance(meta.get("tags"), str):
        tags = _tag_list(meta["tags"].strip("[]"))
    eid = str(meta.get("id") or os.path.splitext(os.path.basename(path))[0])
    return {
        "id": eid,
        "title": str(meta.get("title") or eid),
        "type": str(meta.get("type") or ("incident" if os.sep + "incidents" + os.sep in path else "fact")),
        "tags": tags,
        "confidence": str(meta.get("confidence") or "medium"),
        "source": str(meta.get("source") or ""),
        "created_at": str(meta.get("created_at") or ""),
        "updated_at": str(meta.get("updated_at") or ""),
        "path": path,
        "body": body.strip(),
    }


def load_entries() -> list[dict]:
    out = []
    for folder in (ENTRIES, INCIDENTS):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".md"):
                out.append(parse_entry(os.path.join(folder, name)))
    return out


def _next_id(kind: str, now: datetime.datetime) -> str:
    prefix = ("INC-" if kind == "incident" else "K-") + now.strftime("%Y%m%d") + "-"
    nums = []
    for e in load_entries():
        if e["id"].startswith(prefix):
            try:
                nums.append(int(e["id"].rsplit("-", 1)[1]))
            except ValueError:
                pass
    return "%s%03d" % (prefix, (max(nums) + 1) if nums else 1)


def add(title: str, body: str, tags: list[str], kind: str = "fact",
        source: str = "", confidence: str = "medium", now: str | None = None) -> dict:
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise ValueError("add 需要 title 与 body")
    if kind not in LEGAL_TYPES:
        raise ValueError("type 只能是 %s" % ",".join(LEGAL_TYPES))
    if confidence not in LEGAL_CONF:
        raise ValueError("confidence 只能是 %s" % ",".join(LEGAL_CONF))
    stamp = now_iso(now)
    dt = datetime.datetime.fromisoformat(stamp)
    eid = _next_id(kind, dt)
    folder = INCIDENTS if kind == "incident" else ENTRIES
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, eid + ".md")
    fm = "\n".join([
        "---",
        "id: %s" % eid,
        "title: %s" % title,
        "type: %s" % kind,
        "tags: [%s]" % ", ".join(tags),
        "confidence: %s" % confidence,
        "source: %s" % source.replace("\n", " "),
        "created_at: %s" % stamp,
        "updated_at: %s" % stamp,
        "---",
    ])
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm + "\n\n" + body + "\n")
    rebuild_index()
    return parse_entry(path)


def _score(entry: dict, tokens: list[str]) -> int:
    title = entry["title"].lower()
    body = entry["body"].lower()
    tags = [t.lower() for t in entry["tags"]]
    score = 0
    for tok in tokens:
        tok = tok.lower()
        if not tok:
            continue
        if tok in tags:
            score += 5
        if tok in title:
            score += 3
        if tok in body:
            score += 1
    return score


def _tokens(query: str) -> list[str]:
    """英文按词、中文按 2-4 字滑窗，避免整句匹配不到。"""
    query = (query or "").strip()
    toks = [x for x in re.split(r"[^\w\u4e00-\u9fff]+", query) if x]
    cjk = re.findall(r"[\u4e00-\u9fff]+", query)
    for run in cjk:
        if len(run) <= 4:
            toks.append(run)
        else:
            for n in (4, 3, 2):
                toks.extend(run[i:i + n] for i in range(0, len(run) - n + 1))
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def search(query: str, limit: int = 5) -> list[dict]:
    toks = _tokens(query)
    scored = []
    for e in load_entries():
        s = _score(e, toks)
        if s > 0:
            scored.append((s, e))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    out = []
    for s, e in scored[:limit]:
        item = dict(e)
        item["score"] = s
        out.append(item)
    return out


def match(text: str, limit: int = 3) -> list[dict]:
    """给任务卡找相关经验：优先标签命中，其次标题/正文。"""
    return search(text, limit)


def rebuild_index() -> str:
    entries = load_entries()
    lines = [
        "# 大管家私有知识库索引",
        "",
        "> 自动生成，勿手改；条目在 `entries/` 与 `incidents/`。",
        "> 生成时间：%s；条目数：%d" % (now_iso(), len(entries)),
        "",
        "| id | type | confidence | title | tags | updated |",
        "|---|---|---|---|---|---|",
    ]
    for e in sorted(entries, key=lambda x: (x["type"], x["id"])):
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            e["id"], e["type"], e["confidence"], e["title"].replace("|", "/"),
            ", ".join(e["tags"]).replace("|", "/"), e["updated_at"] or e["created_at"]))
    text = "\n".join(lines) + "\n"
    os.makedirs(KB, exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, INDEX)
    return INDEX


def verify() -> list[str]:
    problems = []
    ids = []
    for e in load_entries():
        ids.append(e["id"])
        if e["type"] not in LEGAL_TYPES:
            problems.append("%s 非法 type=%r" % (e["id"], e["type"]))
        if e["confidence"] not in LEGAL_CONF:
            problems.append("%s 非法 confidence=%r" % (e["id"], e["confidence"]))
        if not e["title"] or not e["body"]:
            problems.append("%s 缺 title/body" % e["id"])
        src = e["source"]
        # source 可能是 URL、描述、或「路径 + 备注」；只对看起来像路径的第一段做存在性校验。
        if src and not src.startswith(("http://", "https://", "session:", "实测", "日志",
                                       "日报", "用户", "GitHub", "记忆", "配置", "代码")):
            first = src.split()[0]
            if "/" in first or "\\" in first or first.endswith((".md", ".py", ".json", ".txt", ".log")):
                full = first if os.path.isabs(first) else os.path.join(ROOT, first)
                if not os.path.exists(full):
                    problems.append("%s source 不存在: %s" % (e["id"], first))
    dup = sorted({x for x in ids if ids.count(x) > 1})
    if dup:
        problems.append("重复 id: " + ",".join(dup))
    return problems


def render(e: dict) -> str:
    return "%s [%s/%s] %s  tags=%s" % (
        e["id"], e["type"], e["confidence"], e["title"], ",".join(e["tags"]) or "-")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("--title", required=True)
    a.add_argument("--body", default="")
    a.add_argument("--body-file", default="")
    a.add_argument("--tags", default="")
    a.add_argument("--type", default="fact", choices=LEGAL_TYPES)
    a.add_argument("--confidence", default="medium", choices=LEGAL_CONF)
    a.add_argument("--source", default="")
    a.add_argument("--now", default=None)

    s = sub.add_parser("search")
    s.add_argument("--query", required=True)
    s.add_argument("--limit", type=int, default=5)

    m = sub.add_parser("match")
    m.add_argument("--text", required=True)
    m.add_argument("--limit", type=int, default=3)

    sub.add_parser("index")

    ls = sub.add_parser("list")
    ls.add_argument("--type", choices=LEGAL_TYPES)
    ls.add_argument("--tag")

    sub.add_parser("stats")
    sub.add_parser("verify")

    args = ap.parse_args()
    if args.cmd == "add":
        body = args.body
        if args.body_file:
            body = open(args.body_file, encoding="utf-8").read()
        try:
            e = add(args.title, body, _tag_list(args.tags), args.type, args.source,
                    args.confidence, args.now)
        except ValueError as exc:
            print("ERROR %s" % exc)
            return 2
        print(json.dumps(e, ensure_ascii=False, indent=2) if args.json else render(e))
        return 0

    if args.cmd in ("search", "match"):
        q = args.query if args.cmd == "search" else args.text
        items = search(q, args.limit)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            for e in items:
                print("%s score=%s %s" % (render(e), e.get("score", 0), e["path"]))
            if not items:
                print("(no match)")
        return 0

    if args.cmd == "index":
        print(rebuild_index())
        return 0

    if args.cmd == "list":
        items = load_entries()
        if args.type:
            items = [e for e in items if e["type"] == args.type]
        if args.tag:
            items = [e for e in items if args.tag in e["tags"]]
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            for e in sorted(items, key=lambda x: x["id"]):
                print(render(e))
        return 0

    if args.cmd == "stats":
        items = load_entries()
        by_type: dict = {}
        for e in items:
            by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        data = {"total": len(items), "by_type": by_type, "index": INDEX}
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json
              else "total=%d %s" % (len(items), by_type))
        return 0

    problems = verify()
    print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False, indent=2)
          if args.json else ("OK entries=%d" % len(load_entries()) if not problems
                             else "BAD " + " | ".join(problems)))
    return 0 if not problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
