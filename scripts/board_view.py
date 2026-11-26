# -*- coding: utf-8 -*-
"""任务台只读看板：把 state/registry.json + config/steward.json 渲染成静态 HTML。

为什么需要它：
  台账是给机器读的 JSON，人要看队列全貌得开终端敲 list；一个不写任何状态的
  HTML 快照可以直接用浏览器打开、发给别人，又不至于变成第二个写入口。

实时化（2026-11-18 起）：
  页面内嵌 <meta http-equiv="refresh" content="60">，浏览器打开后每 60 秒自动
  重拉——看板是纯静态文件，刷新即重新读盘上的 registry/网关日志，无需服务端。
  顶部摘要条四字段现算：
    active      进行中（queued/dispatched/running）
    queued      排队中（仅 queued）
    done_today  今日完结（done/abandoned 且 updated_at→证据核验→创建 落在今天）
    今日成本    logs/gateway-<今天>.jsonl 里 event=ok 的 cost_cny 合计

约束：
  只读——不写 registry、不改配置，产物只有 reports/board.html（坏台账时改为
  落一篇可读的错误页，同样是完整 HTML、不含堆栈）；
  零依赖——只用标准库，不联网、不引 CDN；中文靠 meta charset 正常显示。

三栏口径（与 steward_board.py 状态机对齐）：
  Working = queued / dispatched / running
  Blocked = blocked / failed（含未知状态，未知一律当"需要人看"）
  Done    = done / abandoned

用法：
  python -X utf8 scripts/board_view.py
  python -X utf8 scripts/board_view.py --registry state/registry.json \
      --config config/steward.json --out reports/board.html [--now 2026-11-18T12:00:00+08:00]

退出码：0 生成成功；2 台账/参数错误（stderr 打可读原因，并落错误页，不抛栈）。
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_REGISTRY = os.path.join(ROOT, "state", "registry.json")
DEFAULT_CONFIG = os.path.join(ROOT, "config", "steward.json")
DEFAULT_OUT = os.path.join(ROOT, "reports", "board.html")
DEFAULT_LOGS = os.path.join(ROOT, "logs")

COLUMNS = (
    ("working", "Working 进行中", ("queued", "dispatched", "running")),
    ("blocked", "Blocked 受阻", ("blocked", "failed")),
    ("done", "Done 已完结", ("done", "abandoned")),
)
KNOWN_STATES = tuple(s for _, _, states in COLUMNS for s in states)
DONE_STATES = ("done", "abandoned")

#: 自动刷新周期（秒）。看板是静态文件，刷新=重新读盘，无需常驻服务。
REFRESH_SEC = 60


class BoardError(Exception):
    """台账/配置结构性错误：调用方应打印并退出 2。"""


def esc(value) -> str:
    """HTML 转义；None 视为空串。所有来自台账的文本都必须先过这里。"""
    return html.escape("" if value is None else str(value), quote=True)


def parse_now(raw: str | None) -> datetime.datetime:
    if not raw:
        return datetime.datetime.now().astimezone().replace(microsecond=0)
    dt = datetime.datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.replace(microsecond=0)


def load_registry(path: str) -> dict:
    """严格读取台账：坏 JSON / 非对象 / tasks 非列表都抛 BoardError。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise BoardError("台账读取失败 %s: %s" % (path, e))
    except ValueError as e:
        raise BoardError("台账不是合法 JSON %s: %s" % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise BoardError("台账根必须是对象且 tasks 必须是列表: %s" % path)
    return data


def load_config(path: str) -> tuple[dict, str]:
    """宽容读取配置：缺失/坏 JSON/非对象都退化为默认值，返回 (配置, 提示)。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        return {}, "配置读取失败，使用默认值: %s" % e
    except ValueError as e:
        return {}, "配置不是合法 JSON，使用默认值: %s" % e
    if not isinstance(data, dict):
        return {}, "配置根不是对象，使用默认值"
    return data, ""


def dispatch_cfg(config: dict) -> dict:
    d = config.get("dispatch") if isinstance(config, dict) else None
    return d if isinstance(d, dict) else {}


def bucket_of(state: str) -> str:
    for key, _, states in COLUMNS:
        if state in states:
            return key
    return "blocked"


def bucket_tasks(registry: dict) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {key: [] for key, _, _ in COLUMNS}
    for task in registry.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        state = str(task.get("state") or "unknown")
        buckets[bucket_of(state)].append(task)
    return buckets


def attempts_of(task: dict) -> tuple[int, int]:
    """返回 (已尝试, 上限)；字段缺失/脏值按 0 处理，上限取任务自身或配置。"""
    raw = task.get("attempts")
    try:
        attempts = int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        attempts = 0
    limit = task.get("max_attempts")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    return attempts, limit


def evidence_info(task: dict) -> dict:
    """证据状态：empty(未登记) / missing(路径登记了但文件不在) / ok。"""
    raw = " ".join(str(task.get("evidence_path") or "").split())
    if not raw:
        return {"path": "", "status": "empty", "label": "证据缺失（未登记）"}
    local = raw if os.path.isabs(raw) else os.path.join(ROOT, raw.replace("/", os.sep))
    if os.path.isfile(local):
        return {"path": raw, "status": "ok", "label": "已登记"}
    return {"path": raw, "status": "missing", "label": "证据缺失（文件未找到）"}


def stale_seconds(task: dict, now: datetime.datetime) -> float | None:
    raw = task.get("last_check") or task.get("last_progress_at")
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return (now - dt).total_seconds()


# ---------------------------------------------------------------- 摘要条数据


def gateway_log_path(now: datetime.datetime, logs_dir: str | None = None) -> str:
    """今天（按 now 的本地日期）的网关流水账路径。"""
    folder = logs_dir or DEFAULT_LOGS
    return os.path.join(folder, "gateway-%s.jsonl" % now.strftime("%Y%m%d"))


def today_cost(now: datetime.datetime, logs_dir: str | None = None) -> tuple[float, str]:
    """今日成本现算：logs/gateway-<今天>.jsonl 里 event=ok 的 cost_cny 合计。

    宽容到底：日志缺失/坏行/脏数值/读不了，一律退化为 (0.0, 可读原因)，
    绝不抛栈——摘要条坏掉不该把整页看板拖垮。
    """
    path = gateway_log_path(now, logs_dir)
    name = os.path.basename(path)
    if not os.path.isfile(path):
        return 0.0, "网关日志缺失（%s），按 0 元计" % name
    total, n = 0.0, 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("event") != "ok":
                    continue
                try:
                    total += float(row.get("cost_cny") or 0.0)
                except (TypeError, ValueError):
                    continue
                n += 1
    except OSError as e:
        return 0.0, "网关日志读取失败（%s），按 0 元计" % e
    return round(total, 4), "网关 %s：%d 次请求" % (name, n)


def completion_dt(task: dict) -> datetime.datetime | None:
    """完结时刻：优先 updated_at（状态机落款），退化 evidence_verified_at / created_at。

    解析不了的脏时间返回 None（不计入今日完结，也不抛栈）。
    """
    for key in ("updated_at", "evidence_verified_at", "created_at"):
        raw = task.get(key)
        if not raw:
            continue
        try:
            dt = datetime.datetime.fromisoformat(str(raw).strip())
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    return None


def done_today_count(registry: dict, now: datetime.datetime) -> int:
    """今日完结数：done/abandoned 且完结时刻的本地日期 == now 的本地日期。"""
    today = now.date()
    n = 0
    for task in registry.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("state") or "unknown") not in DONE_STATES:
            continue
        dt = completion_dt(task)
        if dt is not None and dt.date() == today:
            n += 1
    return n


def summary_stats(registry: dict, now: datetime.datetime,
                  cost_cny: float, cost_note: str = "") -> dict:
    """摘要条四字段 + 三栏计数（渲染与 CLI 打印共用同一口径）。"""
    buckets = bucket_tasks(registry)
    queued = sum(1 for t in registry.get("tasks") or []
                 if isinstance(t, dict) and str(t.get("state") or "unknown") == "queued")
    return {
        "active": len(buckets["working"]),
        "queued": queued,
        "done_today": done_today_count(registry, now),
        "cost_cny": cost_cny,
        "cost_note": cost_note,
        "counts": {k: len(buckets[k]) for k, _, _ in COLUMNS},
        "tasks": sum(len(v) for v in buckets.values()),
    }


# ---------------------------------------------------------------- 卡片渲染


def render_card(task: dict, now: datetime.datetime, stale_sec: float, default_limit: int) -> str:
    task_id = str(task.get("id") or "(无 id)")
    title = task.get("title") or "(无标题)"
    project = task.get("project") or "(未标注项目)"
    state = str(task.get("state") or "unknown")
    state_class = state if state in KNOWN_STATES else "unknown"
    attempts, limit = attempts_of(task)
    limit = limit or default_limit
    ev = evidence_info(task)
    age = stale_seconds(task, now)
    stale = age is not None and stale_sec > 0 and age > stale_sec

    meta = [
        "<div><dt>项目</dt><dd>%s</dd></div>" % esc(project),
        "<div><dt>尝试</dt><dd>%d/%d</dd></div>" % (attempts, limit),
    ]
    if ev["path"]:
        meta.append("<div><dt>证据</dt><dd><code>%s</code> <span class=\"ev %s\">%s</span></dd></div>"
                    % (esc(ev["path"]), ev["status"], esc(ev["label"])))
    else:
        meta.append("<div><dt>证据</dt><dd><span class=\"ev %s\">%s</span></dd></div>"
                    % (ev["status"], esc(ev["label"])))
    if task.get("last_check"):
        badge = " <span class=\"stale\">静默 &gt; %d 分钟</span>" % int(stale_sec // 60) if stale else ""
        meta.append("<div><dt>最近检查</dt><dd>%s%s</dd></div>"
                    % (esc(task.get("last_check")), badge))
    if task.get("next_action"):
        meta.append("<div><dt>下一步</dt><dd>%s</dd></div>" % esc(task.get("next_action")))

    return "\n".join([
        "<article class=\"card\" data-id=\"%s\" data-state=\"%s\" data-ev=\"%s\">"
        % (esc(task_id), esc(state), esc(ev["status"])),
        "  <div class=\"card-head\"><span class=\"tid\">%s</span>"
        "<span class=\"badge %s\">%s</span></div>" % (esc(task_id), esc(state_class), esc(state)),
        "  <h3 class=\"title\">%s</h3>" % esc(title),
        "  <dl class=\"meta\">%s</dl>" % "".join(meta),
        "</article>",
    ])


STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font: 15px/1.55 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
       background: #f4f5f7; color: #1f2328; }
header { max-width: 1280px; margin: 0 auto 18px; }
h1 { margin: 0 0 6px; font-size: 24px; }
.sub { margin: 0; color: #57606a; font-size: 13px; }
.totals { margin: 10px 0 0; font-size: 14px; font-weight: 600; }
.summary { margin: 12px 0 0; display: grid; gap: 10px;
           grid-template-columns: repeat(4, minmax(0, 1fr)); }
@media (max-width: 900px) { .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.summary .cell { background: #ffffff; border: 1px solid #d0d7de; border-radius: 8px;
                 padding: 10px 14px; display: flex; flex-direction: column; gap: 2px; }
.summary .k { color: #57606a; font-size: 12px; }
.summary .v { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.summary .cell[data-k="active"] .v { color: #1a7f37; }
.summary .cell[data-k="queued"] .v { color: #0969da; }
.summary .cell[data-k="done_today"] .v { color: #8250df; }
.summary .cell[data-k="cost"] .v { color: #9a6700; font-size: 20px; }
.summary .note { grid-column: 1 / -1; margin: 0; color: #8c959f; font-size: 12px; }
.warn { margin: 10px 0 0; padding: 8px 12px; border-left: 4px solid #d29922;
        background: #fff8c5; color: #6b4e00; font-size: 13px; border-radius: 4px; }
.board { max-width: 1280px; margin: 0 auto; display: grid; gap: 16px;
         grid-template-columns: repeat(3, minmax(0, 1fr)); }
@media (max-width: 900px) { .board { grid-template-columns: 1fr; } }
.col { background: #ffffff; border: 1px solid #d0d7de; border-radius: 8px; padding: 14px; }
.col h2 { margin: 0 0 12px; font-size: 16px; display: flex; justify-content: space-between; }
.count { color: #57606a; font-weight: 400; }
.empty { margin: 0; padding: 18px 8px; text-align: center; color: #8c959f; font-size: 13px;
         border: 1px dashed #d0d7de; border-radius: 6px; }
.card { border: 1px solid #d0d7de; border-left: 4px solid #8c959f; border-radius: 6px;
        padding: 10px 12px; margin-bottom: 10px; background: #fbfcfd; }
.col.working .card { border-left-color: #1a7f37; }
.col.blocked .card { border-left-color: #cf222e; }
.col.done .card { border-left-color: #0969da; }
.card-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.tid { font-family: Consolas, monospace; font-size: 12px; color: #57606a; }
.badge { font-size: 11px; padding: 1px 7px; border-radius: 999px; background: #eaeef2;
         color: #1f2328; white-space: nowrap; }
.badge.running, .badge.dispatched { background: #dafbe1; color: #116329; }
.badge.queued { background: #ddf4ff; color: #0a3069; }
.badge.blocked, .badge.failed { background: #ffebe9; color: #82071e; }
.badge.done { background: #ddf4ff; color: #0a3069; }
.badge.abandoned, .badge.unknown { background: #eaeef2; color: #57606a; }
.title { margin: 6px 0 8px; font-size: 14px; }
.meta { margin: 0; font-size: 12px; color: #57606a; }
.meta div { display: flex; gap: 6px; margin-top: 2px; }
.meta dt { flex: 0 0 4.2em; color: #8c959f; }
.meta dd { margin: 0; overflow-wrap: anywhere; }
.meta code { font-family: Consolas, monospace; font-size: 11px; }
.ev.ok { color: #1a7f37; }
.ev.empty, .ev.missing { color: #cf222e; font-weight: 600; }
.stale { color: #9a6700; }
footer { max-width: 1280px; margin: 18px auto 0; color: #8c959f; font-size: 12px; }
"""

SUMMARY_LABELS = (
    ("active", "Active 进行中"),
    ("queued", "Queued 排队"),
    ("done_today", "Done 今日完结"),
    ("cost", "今日成本"),
)


def render_summary(stats: dict) -> str:
    """顶部摘要条：四字段现算值 + data-* 属性（供测试/脚本断言）。"""
    cells = []
    for key, label in SUMMARY_LABELS:
        attr = key.replace("_", "-")
        if key == "cost":
            value = "¥%.4f" % float(stats.get("cost_cny") or 0.0)
            raw = "%.4f" % float(stats.get("cost_cny") or 0.0)
        else:
            value = str(int(stats.get(key) or 0))
            raw = value
        cells.append("<div class=\"cell\" data-k=\"%s\" data-%s=\"%s\">"
                     "<span class=\"k\">%s</span><b class=\"v\">%s</b></div>"
                     % (esc(key), esc(attr), esc(raw), esc(label), esc(value)))
    note = stats.get("cost_note") or ""
    if note:
        cells.append("<p class=\"note\">%s</p>" % esc(note))
    return "<div class=\"summary\">%s</div>" % "".join(cells)


def page_head(title: str) -> list[str]:
    """公共 <head>：charset/viewport/自动刷新/样式，看板页与错误页共用。"""
    return [
        "<!DOCTYPE html>",
        "<html lang=\"zh-CN\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<meta http-equiv=\"refresh\" content=\"%d\">" % REFRESH_SEC,
        "<title>%s</title>" % esc(title),
        "<style>%s</style>" % STYLE,
        "</head>",
    ]


def build_board(registry: dict, config: dict, now: datetime.datetime | None = None,
                cost_cny: float | None = None, cost_note: str | None = None) -> str:
    """渲染整页 HTML（不落盘）。

    cost_cny 传 None 时按 now 的日期现算网关日志成本（默认 logs/ 目录），
    测试/调用方可用显式值锁定输出。
    """
    now = now or parse_now(None)
    if cost_cny is None:
        cost_cny, live_note = today_cost(now)
        cost_note = live_note if cost_note is None else cost_note
    cfg = dispatch_cfg(config)
    stale_sec = cfg.get("stale_sec") or config.get("stale_sec") or 0
    try:
        stale_sec = float(stale_sec)
    except (TypeError, ValueError):
        stale_sec = 0.0
    try:
        default_limit = int(cfg.get("max_attempts") or 3)
    except (TypeError, ValueError):
        default_limit = 3

    stats = summary_stats(registry, now, cost_cny, cost_note or "")
    buckets = bucket_tasks(registry)
    counts = stats["counts"]

    parts = page_head("任务台只读看板")
    parts += [
        "<body>",
        "<header>",
        "<h1>任务台只读看板</h1>",
        "<p class=\"sub\">生成时间 %s ｜ 数据源 state/registry.json + config/steward.json + "
        "logs/gateway ｜ 每 %d 秒自动刷新（重读台账与网关日志）</p>"
        % (esc(now.strftime("%Y-%m-%d %H:%M:%S %z")), REFRESH_SEC),
        render_summary(stats),
        "<p class=\"totals\">任务 %d ｜ Working %d ｜ Blocked %d ｜ Done %d</p>"
        % (stats["tasks"], counts["working"], counts["blocked"], counts["done"]),
    ]
    warnings = registry.get("_warnings") or []
    for w in warnings:
        parts.append("<p class=\"warn\">%s</p>" % esc(w))
    parts.append("</header>")
    parts.append("<main class=\"board\">")

    for key, label, _ in COLUMNS:
        items = buckets[key]
        parts.append("<section class=\"col %s\" data-state=\"%s\" data-count=\"%d\">"
                     % (key, key, len(items)))
        parts.append("<h2>%s <span class=\"count\">%d</span></h2>" % (esc(label), len(items)))
        if not items:
            parts.append("<p class=\"empty\">无任务</p>")
        else:
            for task in items:
                parts.append(render_card(task, now, stale_sec, default_limit))
        parts.append("</section>")

    parts.append("</main>")
    parts.append("<footer>由 scripts/board_view.py 生成；看板只读取证，任何状态变更请走 "
                 "scripts/steward_board.py。</footer>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


def build_error_page(message: str, now: datetime.datetime | None = None) -> str:
    """坏台账/坏参数时的可读错误页：完整 HTML、无堆栈、同样 60 秒重试。

    看板是派生产物，覆盖它没有不可逆损失；留着旧页反而会掩盖"台账已坏"。
    """
    now = now or parse_now(None)
    parts = page_head("任务台看板·生成失败")
    parts += [
        "<body>",
        "<header>",
        "<h1>任务台看板·生成失败</h1>",
        "<p class=\"sub\">生成时间 %s ｜ 台账或参数不可读，本篇为可读错误页（不含堆栈）</p>"
        % esc(now.strftime("%Y-%m-%d %H:%M:%S %z")),
        "</header>",
        "<main class=\"board\" style=\"grid-template-columns: 1fr;\">",
        "<section class=\"col blocked\" style=\"border-left: 4px solid #cf222e;\">",
        "<h2>错误原因</h2>",
        "<p class=\"err\">%s</p>" % esc(message or "未知错误"),
        "<p class=\"empty\">修好台账后本页每 %d 秒重试即自动恢复；"
        "也可以手动跑 <code>python -X utf8 scripts/board_view.py</code> 重生成。</p>"
        % REFRESH_SEC,
        "</section>",
        "</main>",
        "<footer>由 scripts/board_view.py 生成；看板只读取证，任何状态变更请走 "
        "scripts/steward_board.py。</footer>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


def write_atomic(path: str, text: str) -> None:
    """临时文件 + fsync + os.replace，避免留下半截 HTML。"""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".board-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def generate(registry_path: str, config_path: str, out_path: str,
             now: datetime.datetime | None = None,
             logs_dir: str | None = None) -> tuple[str, dict]:
    """读台账/配置 -> 现算摘要 -> 渲染 -> 原子落盘；返回 (html, 摘要)。"""
    registry = load_registry(registry_path)
    config, warning = load_config(config_path)
    if warning:
        registry = dict(registry)
        registry["_warnings"] = list(registry.get("_warnings") or []) + [warning]
    cost_cny, cost_note = today_cost(now or parse_now(None), logs_dir)
    page = build_board(registry, config, now=now, cost_cny=cost_cny, cost_note=cost_note)
    write_atomic(out_path, page)
    stats = summary_stats(registry, now or parse_now(None), cost_cny, cost_note)
    summary = {
        "out": out_path,
        "bytes": len(page.encode("utf-8")),
        "tasks": stats["tasks"],
        "counts": stats["counts"],
        "active": stats["active"],
        "queued": stats["queued"],
        "done_today": stats["done_today"],
        "cost_cny": cost_cny,
        "cost_note": cost_note,
    }
    return page, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成任务台只读看板 reports/board.html")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--now", default=None, help="ISO 时间（测试用，固定生成时间）")
    args = ap.parse_args(argv)
    now = None
    try:
        now = parse_now(args.now)
        _, summary = generate(args.registry, args.config, args.out, now=now)
    except (BoardError, ValueError, OSError) as e:
        message = str(e) or e.__class__.__name__
        print("[ERROR] %s" % message, file=sys.stderr)
        try:
            write_atomic(args.out, build_error_page(message, now=now))
        except OSError:
            pass  # 错误页也写不了（如磁盘/权限问题）：stderr 已有可读原因，不抛栈
        return 2
    print("BOARD %s tasks=%d working=%d blocked=%d done=%d active=%d queued=%d "
          "done_today=%d cost=%.4f bytes=%d"
          % (summary["out"], summary["tasks"], summary["counts"]["working"],
             summary["counts"]["blocked"], summary["counts"]["done"], summary["active"],
             summary["queued"], summary["done_today"], summary["cost_cny"],
             summary["bytes"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
