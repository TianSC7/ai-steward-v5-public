# -*- coding: utf-8 -*-
"""知识卡体检：查坏卡、重复卡、过期卡与 superseded 链完整性。

为什么单独做 lint 而不是塞进 steward_kb.verify：
  verify 是「写入后自检」，只保新增卡合法；历史卡可能被手改/迁移污染，
  需要一把只读的体检刀，能对整库跑、能给机器读的 JSON、能进总闸。

检查项（level=error 拦门，level=warning 只提示）：
  error   frontmatter 缺失/未闭合、必填字段缺失或为空、id 与文件名不一致、
          updated_at 格式非法、重复 id、重复标题、superseded_by 指向不存在/
          指向自身/成环、非法 type
  warning created_at 格式非法、非法 confidence、source 为空、id 命名不规范、
          updated_at 早于 created_at、超过 --stale-days 天未更新
          （已被 superseded 的卡不计过期，它们本就该冻结）

必填字段与 steward_kb.add 的落盘 schema 对齐：
  id, title, type, tags, confidence, source, created_at, updated_at
  tags 允许空数组；source 为空只告警（历史卡常见）。

只读承诺：本脚本不写 INDEX、不改卡片、不建缓存（sys.dont_write_bytecode）。

用法：
  python -X utf8 scripts/kb_lint.py
  python -X utf8 scripts/kb_lint.py --json
  python -X utf8 scripts/kb_lint.py --dir knowledge/entries --stale-days 30 \
      --now 2026-12-31T00:00:00+08:00

退出码：0 = 无 error（warning 不拦）；1 = 有 error；2 = 参数/目录错误。
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
ENTRIES = os.path.join(ROOT, "knowledge", "entries")

REQUIRED_FIELDS = ("id", "title", "type", "tags", "confidence", "source",
                   "created_at", "updated_at")
LEGAL_TYPES = ("fact", "pattern", "preference", "incident")
LEGAL_CONF = ("high", "medium", "low")
DEFAULT_STALE_DAYS = 30

# ISO8601 且必须带时区：2026-11-18T09:10:00+08:00 / ...Z / 带小数秒
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
ID_RE = re.compile(r"^(?:K|INC)-\d{8}-\d{3}$")


def parse_ts(value: str) -> datetime.datetime | None:
    """严格解析带时区的 ISO8601；不合格式返回 None。"""
    value = (value or "").strip()
    if not TS_RE.match(value):
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_frontmatter(text: str) -> tuple[str, dict, str]:
    """返回 (status, meta, body)。

    status: ok | missing（开头不是 ---）| unterminated（缺结束 ---）。
    只解析扁平 key: value，与 steward_kb.parse_entry 同口径；列表值原样保留字符串。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        return "missing", {}, text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return "unterminated", {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        meta[key] = val.strip()
    return "ok", meta, parts[2].lstrip("\n")


def _issue(level: str, field: str, msg: str, eid: str, path: str) -> dict:
    return {"id": eid, "level": level, "field": field, "msg": msg, "path": path}


def _add(record: dict, level: str, field: str, msg: str) -> None:
    record["issues"].append(_issue(level, field, msg, record["id"], record["path"]))


def _scan_file(full: str, stem: str, now: datetime.datetime,
               stale_days: int) -> dict:
    record = {"id": stem, "title": "", "path": full, "meta": {},
              "issues": [], "updated": None, "superseded_by": ""}
    try:
        with open(full, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        _add(record, "error", "file", "无法按 UTF-8 读取: %s" % str(exc)[:120])
        return record

    status, meta, _body = parse_frontmatter(text)
    record["meta"] = meta
    if status == "missing":
        _add(record, "error", "frontmatter", "缺 frontmatter（文件开头不是 ---）")
        return record
    if status == "unterminated":
        _add(record, "error", "frontmatter", "frontmatter 未闭合（缺结束 ---）")
        return record

    eid = meta.get("id", "").strip()
    if eid:
        record["id"] = eid
    record["title"] = meta.get("title", "").strip()
    record["superseded_by"] = meta.get("superseded_by", "").strip()

    # 1) 必填字段：缺 key 与空值分开报；tags 允许为空，source 空只告警。
    for field in REQUIRED_FIELDS:
        if field not in meta:
            _add(record, "error", field, "缺必填字段 %s" % field)
            continue
        val = meta[field].strip()
        if val:
            continue
        if field == "tags":
            continue
        if field == "source":
            _add(record, "warning", "source", "source 为空（建议补来源）")
        else:
            _add(record, "error", field, "必填字段 %s 为空" % field)

    # 2) id 与文件名一致 + 命名规范。
    if eid:
        if eid != stem:
            _add(record, "error", "id",
                 "id=%s 与文件名 %s.md 不一致" % (eid, stem))
        if not ID_RE.match(eid):
            _add(record, "warning", "id",
                 "id 不符合 K-YYYYMMDD-NNN / INC-YYYYMMDD-NNN 格式: %s" % eid)

    # 3) 时间格式与先后关系。
    created_raw = meta.get("created_at", "").strip()
    created = parse_ts(created_raw) if created_raw else None
    if created_raw and created is None:
        _add(record, "warning", "created_at",
             "created_at 格式非法: %r（要求 ISO8601 带时区，如 "
             "2026-11-18T09:10:00+08:00）" % created_raw)

    updated_raw = meta.get("updated_at", "").strip()
    updated = parse_ts(updated_raw) if updated_raw else None
    if updated_raw and updated is None:
        _add(record, "error", "updated_at",
             "updated_at 格式非法: %r（要求 ISO8601 带时区，如 "
             "2026-11-18T09:10:00+08:00）" % updated_raw)
    record["updated"] = updated
    if updated is not None:
        if created is not None and updated < created:
            _add(record, "warning", "updated_at",
                 "updated_at（%s）早于 created_at（%s）" % (updated_raw, created_raw))
        if not record["superseded_by"]:
            age_sec = (now - updated).total_seconds()
            if age_sec > stale_days * 86400:
                _add(record, "warning", "updated_at",
                     "超过 %d 天未更新（最后更新 %s）" % (stale_days, updated_raw))

    # 4) 枚举值。
    typ = meta.get("type", "").strip()
    if typ and typ not in LEGAL_TYPES:
        _add(record, "error", "type",
             "非法 type=%r（只能是 %s）" % (typ, ",".join(LEGAL_TYPES)))
    conf = meta.get("confidence", "").strip()
    if conf and conf not in LEGAL_CONF:
        _add(record, "warning", "confidence",
             "非法 confidence=%r（只能是 %s）" % (conf, ",".join(LEGAL_CONF)))

    return record


def lint_dir(path: str = ENTRIES, now: datetime.datetime | None = None,
             stale_days: int = DEFAULT_STALE_DAYS) -> dict:
    """只读扫描目录下全部 *.md，返回 {ok, dir, issues, counts}。"""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    now_dt = now or datetime.datetime.now().astimezone()
    if now_dt.tzinfo is None:
        now_dt = now_dt.astimezone()

    records = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".md"):
            continue
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        records.append(_scan_file(full, name[:-3], now_dt, stale_days))

    # 5) 重复 id / 重复标题（每个涉事文件各报一条，便于逐卡修）。
    by_id: dict[str, list] = {}
    by_title: dict[str, list] = {}
    for rec in records:
        by_id.setdefault(rec["id"], []).append(rec)
        if rec["title"]:
            by_title.setdefault(rec["title"], []).append(rec)

    for eid, group in by_id.items():
        if len(group) > 1:
            names = ", ".join(os.path.basename(r["path"]) for r in group)
            for rec in group:
                _add(rec, "error", "id",
                     "重复 id：%s 出现在 %d 个文件（%s）" % (eid, len(group), names))
    for title, group in by_title.items():
        if len(group) > 1:
            names = ", ".join(os.path.basename(r["path"]) for r in group)
            for rec in group:
                _add(rec, "error", "title",
                     "重复标题：%r 出现在 %d 个文件（%s）" % (title, len(group), names))

    # 6) superseded 链：目标存在、不指向自身、不成环。
    idmap = {rec["id"]: rec for rec in records}
    for rec in records:
        target = rec["superseded_by"]
        if not target:
            continue
        if target == rec["id"]:
            _add(rec, "error", "superseded_by", "superseded_by 指向自身: %s" % target)
            continue
        if target not in idmap:
            _add(rec, "error", "superseded_by",
                 "superseded_by 指向不存在的卡片: %s" % target)
            continue
        chain = [rec["id"]]
        cur = target
        while cur in idmap:
            if cur in chain:
                chain.append(cur)
                _add(rec, "error", "superseded_by",
                     "superseded_by 链成环: %s" % " -> ".join(chain))
                break
            chain.append(cur)
            cur = idmap[cur]["superseded_by"]
        # 链尾指向一个不存在的 id 时，该条错误由链尾卡自己报，这里不重复。

    issues = [iss for rec in records for iss in rec["issues"]]
    issues.sort(key=lambda i: (i["path"], i["field"], i["level"], i["msg"], i["id"]))
    errors = sum(1 for i in issues if i["level"] == "error")
    warnings = sum(1 for i in issues if i["level"] == "warning")
    return {
        "ok": errors == 0,
        "dir": path,
        "issues": issues,
        "counts": {
            "entries": len(records),
            "errors": errors,
            "warnings": warnings,
            "total": len(issues),
        },
    }


def parse_now(raw: str | None) -> datetime.datetime:
    if not raw:
        return datetime.datetime.now().astimezone()
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def main() -> int:
    ap = argparse.ArgumentParser(description="知识卡体检（只读）")
    ap.add_argument("--dir", default=ENTRIES, help="知识卡目录，默认 knowledge/entries")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                    help="超过多少天未更新告警，默认 %d" % DEFAULT_STALE_DAYS)
    ap.add_argument("--now", default=None, help="基准时间（ISO8601），测试/补跑用")
    args = ap.parse_args()

    try:
        now = parse_now(args.now)
        result = lint_dir(args.dir, now=now, stale_days=args.stale_days)
    except FileNotFoundError:
        print("ERROR 目录不存在: %s" % os.path.abspath(args.dir), file=sys.stderr)
        return 2
    except ValueError as exc:
        print("ERROR --now 解析失败: %s" % exc, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        c = result["counts"]
        print("KB_LINT dir=%s entries=%d errors=%d warnings=%d" % (
            result["dir"], c["entries"], c["errors"], c["warnings"]))
        for iss in result["issues"]:
            print("[%s] %s %-12s %s  (%s)" % (
                iss["level"].upper(), iss["id"], iss["field"], iss["msg"],
                os.path.basename(iss["path"])))
        if not result["issues"]:
            print("OK 无坏卡/重复/过期")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
