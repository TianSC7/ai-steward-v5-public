#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""lane_status.py —— MES「并发数」页数据源（只读，不起服务）

口径（2026-11-25 发起人要求：MES 里看到每个模型的并发数与总额）：
  - 并发上限 cap：取 state/lane_caps.json（排班设计上限，R4 6 / R4-2 6 / STEP 3 / NEX 2 / GLM 3 / 外包 60）。
  - 已用 used：最近 N 分钟（默认 5，沿用 concurrency_probe.py 口径）内该通道
    （provider_id + model_id 精确匹配）有 model_usage 记录的不同会话数；只读 ~/.zcode/cli/db/db.sqlite。
  - 进行中 live：model_usage 要等一次模型调用结束才落库（滞后十几秒），所以再并一版
    「最近 live_min 分钟（默认 3）内有 part 步骤活动」的会话（part 在工具调用/推理时即写），
    按该会话最近一条 model_usage 归属通道 → 对话一开始就能在面板上看到占用，不必等轮次结束。
  - 额度：优先取官方额度接口（providerId 的 baseUrl + /usage，后台线程 5 分钟刷新，
    不阻塞面板）：STEP 的 quota.credit_buckets（total/remaining/used_percent/resets_at）、
    兜底 APIgoto 的 subscription/remaining。取不到就回落到台账口径。
  - 台账：月包 Token 取 state/v3_budget_ledger.json 的 month_lane_tokens[当月]；
    配额常量从 factory_runner.py 的 LANE_TOKEN_QUOTA_MONTH 用 ast 读取（避免 import 副作用）。
    用到 90% = 预警（换道分流），100% = 不可用；NEX 限时免费、外包按量，不设月包。
  - GLM：智谱 GLM-5.3-Flash 官方直连、不进网关，无可用额度 → 恒为「不可用」，不计入可用上限。
  - 速度（C-20261126-G57）：近 window_min 分钟 logs/gateway-YYYYMMDD.jsonl 里 event=ok 的
    请求，lane 映射到通道后算 tps = tokens_out / (Σms/1000)；日志缺失/坏行只降级标 source，
    不抛错。日志目录可用环境变量 LANE_LOG_DIR 覆盖（测试/隔离演练用）。
  - auto 族归并（C-20261126-G57）：auto / auto-heavy / auto-night / auto-cheap / auto-reason
    同属 new-provider-7 的网关别名，不是真实通道 → 不再单列 lane，按窗口内网关日志里该别名
    落点最多的真实 lane 归并其会话与 tokens（basis=log）；窗口内无日志时按 routing.json 的
    aliases.<别名>.order[0] 主选归并（basis=routing）。落点 lane 用 merged 数组记账。
  - 未归属：窗口内出现在路由表外、且不属于 auto 族的通道单独列出，不静默并进任何 lane。
  - 网关模型（C-20261126-G58）：gateway_models 为别名维度（routing.json aliases 键在前、
    ALIAS_FALLBACK 缺的补后，relay 即使 routing 未写也出行）。会话/进行中/tokens 取会话库
    provider=new-provider-7 且 model=别名（与 lanes 同窗口同口径，库不可读时 None 不伪造 0）；
    reqs/fails/tok_out/ms_sum/tps 取网关日志按 req_model 聚合（event=ok 计 reqs/tokens/ms，
    event=fail 只计 fails，无 req_model 的失败不归别名）；status= reqs>0 且无失败→ok /
    有失败→warn / 无样本→idle；merged_to=lanes[].merged 里出现该别名的通道（relay 恒为 []）。
    gateway_totals 是别名行的汇总（不重复计容量，不参与 cap/可用上限）。

不写任何文件；board_server 的 /api/lanes.json 直接调用 collect()。
用法：python -X utf8 scripts/lane_status.py [--window 5] [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
STATE = os.path.join(ROOT, "state")
LOGS = os.environ.get("LANE_LOG_DIR") or os.path.join(ROOT, "logs")
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")

# 渲染顺序＝真实通道；auto 族（网关别名）已归并到落点 lane，不再单列（C-20261126-G57）
LANE_ORDER = ("R4", "R4-2", "STEP", "NEX", "GLM", "OUTSOURCE")
DEFAULT_CAPS = {"R4": 6, "R4-2": 6, "STEP": 3, "NEX": 2, "GLM": 3, "OUTSOURCE": 60}

# 网关日志 lane → 页面通道（speed 段与 auto 族归并落点共用这套映射）
GW_LANE_MAP = {
    "r4-dsv41": "R4", "r42-dsv41": "R4-2", "step-flash": "STEP",
    "nex-n26": "NEX", "apigoto-deepseek-flash": "APIgoto",
}
# auto 族别名：同属 new-provider-7，只是路由不是通道（发起人 2026-11-26 口径）
# 只有这一族归并进 lanes[].merged；别名全集见 ALIAS_FALLBACK / _alias_ids（含 relay）
AUTO_ALIASES = ("auto", "auto-heavy", "auto-night", "auto-cheap", "auto-reason")
AUTO_PROVIDER = "new-provider-7"
# 网关别名全集（C-20261126-G58「网关模型」表）：以 routing.json 的 aliases 键为准，
# 键缺失的别名也要出行（status=idle），所以不能只遍历 AUTO_ALIASES（它不含 relay）。
# 顺序：routing.json 的键在前（保持文件顺序），这里只补缺。
ALIAS_FALLBACK = ("auto", "auto-cheap", "auto-heavy", "auto-reason", "auto-night", "relay")
# relay：本地网关承接（provider 仍是 new-provider-7），但不并入任何 ZCode 通道 → merged_to=[]
NO_MERGE_ALIASES = ("relay",)
# routing.json 缺失/别名未定义时的主选顺序（与 config/routing.json 现状同值，仅降级用）
DEFAULT_ALIAS_ORDER = {
    "auto": ["r4-dsv41", "r42-dsv41", "step-flash", "nex-n26", "apigoto-deepseek-flash"],
    "auto-heavy": ["r4-dsv41", "r42-dsv41", "step-flash", "nex-n26", "apigoto-deepseek-flash"],
    "auto-night": ["step-flash", "nex-n26", "r4-dsv41", "r42-dsv41", "apigoto-deepseek-flash"],
    "auto-cheap": ["nex-n26", "r4-dsv41", "r42-dsv41", "apigoto-deepseek-flash"],
    "auto-reason": ["r4-dsv41", "r42-dsv41", "step-flash", "nex-n26", "apigoto-deepseek-flash"],
}
SPEED_READ_CAP = 8 * 1024 * 1024   # 倒读预算：任何大小的日志最多读尾部 8MB（>50MB 的大文件即「只读尾 8MB」，小文件自然读全）
SPEED_DEADLINE = 0.025             # 秒：解析硬上限，超时即标 partial 返回（/api/lanes.json 单次 ≤50ms）

# lane id → config/routing.json 里的 lane id（cap 与路由是两套命名）
ROUTING_LANE = {"R4": "r4-dsv41", "R4-2": "r42-dsv41", "STEP": "step-flash", "NEX": "nex-n26"}

# 路由缺省/读不到时的兜底 (provider_id, model_id) 集合；routing.json 的配置会并进来
DEFAULT_PAIRS = {
    "R4": {("new-provider-6", "deepseek-v4.1-flash")},
    "R4-2": {("new-provider-11", "deepseek-v4.1-flash")},
    "STEP": {("new-provider-10", "step-5-preview"), ("new-provider-10", "step-3.7-flash")},
    "NEX": {("new-provider-5", "nex-n2.6")},
    "GLM": {("account:bigmodel-start-plan", "GLM-5.3-Flash")},
    "OUTSOURCE": {("outsource-minekuai", "deepseek-v4.1-flash-1017")},
}
# lane id → factory_runner.LANE_TOKEN_QUOTA_MONTH 的键
QUOTA_KEY = {"R4": "r4", "R4-2": "r42", "STEP": "step", "NEX": "nex"}
DEFAULT_QUOTAS = {"r4": 6_000_000_000, "r42": 12_000_000_000, "step": 40_000_000_000,
                  "weekend-glm": 500_000_000, "nex": None, "apigoto": None}

DESC = {
    "R4": "主力重活 · DeepSeek V4.1 Flash",
    "R4-2": "R4 第二账号 · DeepSeek V4.1 Flash",
    "STEP": "阶跃 Token-Max 月付 · Step-5-Preview",
    "NEX": "Nex 通道 · 限时免费",
    "GLM": "智谱 GLM-5.3-Flash · 官方直连",
    "OUTSOURCE": "外包并发 60 · deepseek-v4.1-flash-1017",
}
FUSE_PCT = 0.90

# 有官方额度接口的通道：lane/通道名 → providerId + 路径（2026-11-25 实测）。
#   R4 / R4-2：/v1/cli/key → limit_usd/spent_usd/remaining_usd + today + 近 7 日 series
#   STEP     ：/usage    → quota.credit_buckets（token 月包）
#   APIgoto  ：/usage    → subscription/remaining（USD 余额，兜底通道）
# NEX（仅 /health + /v1/models）、GLM（无 apiKey，仅 enc OAuth）、
# 外包（base 410/免费组维护）没有可用余额接口。
QUOTA_API_PROVIDERS = {
    "R4": {"provider": "new-provider-6", "path": "/cli/key"},
    "R4-2": {"provider": "new-provider-11", "path": "/cli/key"},
    "STEP": {"provider": "new-provider-10", "path": "/usage"},
    "APIgoto": {"provider": "00000000-0000-0000-0000-000000000003", "path": "/usage"},
}
QUOTA_TTL_S = 300
QUOTA_TIMEOUT_S = 6
# 不在 6 lane 排班 cap 内的兜底通道（单列展示，不计入总上限）
QUOTA_EXTRA_META = {
    "APIgoto": {"model": "deepseek-flash", "label": "兜底通道",
                "note": "不在排班 cap 内，未计入总上限"},
}


def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else (default if default is not None else {})
    except (OSError, ValueError):
        return default if default is not None else {}


def _caps(state_dir):
    """→ (caps, note, defaulted, sources)。

    caps 只收「有据可查」的上限：lane_caps.json 有条目就用条目值；
    整份 caps 缺失/损坏才全量回落 DEFAULT_CAPS（与历史口径一致，sources 标 default）；
    文件可读但缺某 lane（如新立通道尚未落条目）→ 该 lane 不进 caps，
    defaulted 里登记，页面 cap 显示「—」不造数（降级铁律，不抛错）。
    只按 LANE_ORDER 取条目：lane_caps.json 里残留的非通道条目（AUTO-HEAVY = 工厂施工道数）
    不再进 caps/cap 合计（C-20261126-G57：auto 族已归并为别名，不是通道）。
    """
    d = _load_json(os.path.join(state_dir, "lane_caps.json"))
    raw = d.get("caps") if isinstance(d.get("caps"), dict) else {}
    file_ok = bool(raw)
    out, defaulted, src = {}, [], {}
    for k in LANE_ORDER:
        if k in raw:
            try:
                out[k] = max(0, int(raw[k]))
                src[k] = "file"
                continue
            except (TypeError, ValueError):
                pass
        if file_ok:
            defaulted.append(k)
        else:
            out[k] = DEFAULT_CAPS.get(k, 0)
            src[k] = "default"
    return out, str(d.get("note") or ""), defaulted, src


def _pairs(root):
    """routing.json 的 provider/model 并进兜底集合（路由改了也能归属到 lane）。

    → (pairs, primary)：primary 为该 lane 当前路由的 provider/model（展示用）。
    兜底集合与路由取并集：窗口内的历史调用即使路由刚改过也仍能归属。

    auto 族（new-provider-7 的别名）不在 pairs 里：它们由 _auto_merge 归并到真实
    落点 lane，不再单列通道（C-20261126-G57）。
    """
    path = os.path.join(root, "config", "routing.json")
    d = _load_json(path)
    lanes = d.get("lanes") if isinstance(d.get("lanes"), list) else []
    by_id = {str(x.get("id")): x for x in lanes if isinstance(x, dict)}
    out = {k: set(v) for k, v in DEFAULT_PAIRS.items()}
    primary = {}
    for lane, rid in ROUTING_LANE.items():
        x = by_id.get(rid) or {}
        p, m = x.get("providerId"), x.get("model")
        if isinstance(p, str) and p and isinstance(m, str) and m:
            out[lane].add((p, m))
            primary[lane] = "%s/%s" % (p, m)
    return out, primary


def _aliases(root):
    """→ {别名: [候选 routing lane id…]}（routing.json 的 aliases，缺失回落内置顺序）。

    只读，不改路由：order 列表优先；auto 用的是 tiers（按 REASONING→COMPLEX→MEDIUM→
    SIMPLE 展开，等价于该别名的主选区），没写 order/tiers 用 DEFAULT_ALIAS_ORDER。
    """
    d = _load_json(os.path.join(root, "config", "routing.json"))
    raw = d.get("aliases") if isinstance(d.get("aliases"), dict) else {}
    out = {}
    for alias in AUTO_ALIASES:
        order = []
        e = raw.get(alias)
        if isinstance(e, dict):
            o = e.get("order")
            if isinstance(o, list):
                order = [str(x) for x in o if isinstance(x, str) and x]
            if not order:
                tiers = e.get("tiers") if isinstance(e.get("tiers"), dict) else {}
                for t in ("REASONING", "COMPLEX", "MEDIUM", "SIMPLE"):
                    seq = tiers.get(t)
                    if isinstance(seq, list):
                        for x in seq:
                            if isinstance(x, str) and x and x not in order:
                                order.append(x)
        if not order:
            order = list(DEFAULT_ALIAS_ORDER.get(alias, []))
        out[alias] = order
    return out


def _alias_ids(root):
    """→ [别名…]（网关模型表的行序，C-20261126-G58）。

    routing.json 的 aliases 键在前（dict 保持文件顺序），ALIAS_FALLBACK 里缺的补在后
    → 键缺失的别名也出行（status=idle），relay 即使 routing 还没写也照常列出。
    """
    raw = _load_json(os.path.join(root, "config", "routing.json")).get("aliases")
    raw = raw if isinstance(raw, dict) else {}
    out = [str(k) for k in raw.keys() if isinstance(k, str) and k]
    for a in ALIAS_FALLBACK:
        if a not in out:
            out.append(a)
    return out


def _quotas():
    """从 factory_runner 源码读月包常量；读不到用内置值（同值兜底）。"""
    out = dict(DEFAULT_QUOTAS)
    path = os.path.join(SCRIPTS, "factory_runner.py")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "LANE_TOKEN_QUOTA_MONTH" for t in node.targets):
                val = ast.literal_eval(node.value)
                if isinstance(val, dict):
                    out = {str(k): (int(v) if v is not None else None) for k, v in val.items()}
                break
    except (OSError, SyntaxError, ValueError):
        pass
    return out


def _providers(root):
    """providerId → {base, key}；只收同时有 apiKey 与 baseUrl 的条目（key 不落日志）。"""
    d = _load_json(os.path.join(root, "config", "routing.json"))
    path = d.get("providers_config")
    if not (isinstance(path, str) and path):
        path = os.path.join(os.path.expanduser("~"), ".zcode", "v2", "provider_config.json")
    cfg = _load_json(os.path.expanduser(path))
    out = {}

    def walk(o):
        if isinstance(o, dict):
            if "providerId" in o and isinstance(o.get("config"), dict):
                c = o["config"]
                acc = c.get("access") if isinstance(c.get("access"), dict) else {}
                api = c.get("api") if isinstance(c.get("api"), dict) else {}
                key, base = acc.get("apiKey"), api.get("baseUrl")
                if isinstance(key, str) and key and isinstance(base, str) and base:
                    out.setdefault(str(o["providerId"]), {"base": base.rstrip("/"), "key": key})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(cfg)
    return out


def _http_json(url, key):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "ai-steward-lane-status/1.0",
    })
    with urllib.request.urlopen(req, timeout=QUOTA_TIMEOUT_S,
                                context=ssl.create_default_context()) as r:
        raw = r.read(128 * 1024)
    return json.loads(raw.decode("utf-8", "replace"))


def _parse_usage(payload):
    """官方额度载荷 → 统一快照（R4 /cli/key、STEP credit_buckets、APIgoto subscription）。"""
    if not isinstance(payload, dict):
        raise ValueError("载荷不是对象")
    if "spent_usd" in payload and isinstance(payload.get("today"), dict):
        def _num(v):
            if v in (None, "", "null"):
                return None
            return float(v)
        try:
            spent = _num(payload.get("spent_usd")) or 0.0
            limit = _num(payload.get("limit_usd"))
            remaining = _num(payload.get("remaining_usd"))
        except (TypeError, ValueError):
            raise ValueError("cli/key 数值不可解析")
        if remaining is None and limit is not None:
            remaining = max(0.0, limit - spent)
        today = payload["today"]
        series = [s for s in (payload.get("series") or []) if isinstance(s, dict)]

        def _sum(key):
            try:
                return sum(float(s.get(key) or 0) for s in series)
            except (TypeError, ValueError):
                return 0.0
        try:
            today_usd = float(today.get("charged_usd_total") or 0)
            today_req = int(today.get("request_count") or 0)
            today_tok = int(today.get("prompt_tokens_total") or 0) + \
                int(today.get("completion_tokens_total") or 0)
        except (TypeError, ValueError):
            today_usd, today_req, today_tok = 0.0, 0, 0
        return {
            "unit": "usd", "remaining_unit": "USD", "plan": "Key 限额",
            "total": limit, "remaining": remaining, "used": spent,
            "used_pct": (round(spent * 100.0 / limit, 2) if limit else None),
            "allowed": (remaining is None or remaining > 0),
            "resets_at_ms": None, "expires_at_ms": None,
            "today_usd": round(today_usd, 4), "today_requests": today_req,
            "today_tokens": today_tok, "week_usd": round(_sum("charged_usd_total"), 4),
            "week_requests": int(_sum("request_count")), "observed_at_ms": None,
        }
    quota = payload.get("quota")
    if isinstance(quota, dict) and isinstance(quota.get("credit_buckets"), list):
        buckets = [b for b in quota["credit_buckets"] if isinstance(b, dict)]
        if not buckets:
            raise ValueError("credit_buckets 为空")
        total = sum(int(float(b.get("total") or 0)) for b in buckets)
        remaining = sum(int(float(b.get("remaining") or 0)) for b in buckets)
        if total <= 0:
            raise ValueError("credit_buckets total 非正")
        used = max(0, total - remaining)
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        sub = plan.get("subscription") if isinstance(plan.get("subscription"), dict) else {}
        resets = max([int(b.get("resets_at_ms") or 0) for b in buckets] + [0])
        expires = max([int(b.get("expires_at_ms") or 0) for b in buckets]
                      + [int(sub.get("expires_at_ms") or 0)])
        return {
            "unit": "token", "remaining_unit": "token",
            "plan": str(plan.get("name") or ""),
            "total": total, "remaining": remaining, "used": used,
            "used_pct": round(used * 100.0 / total, 2),
            "allowed": quota.get("allowed") is not False,
            "resets_at_ms": resets or None, "expires_at_ms": expires or None,
            "observed_at_ms": int(payload.get("observed_at_ms") or 0) or None,
        }
    sub = payload.get("subscription")
    if isinstance(sub, dict) and "remaining" in payload:
        try:
            remaining = float(payload.get("remaining") or 0)
            limit = float(sub.get("limit") or 0)
            used = float(sub.get("used") or 0)
        except (TypeError, ValueError):
            raise ValueError("subscription 数值不可解析")
        return {
            "unit": str(sub.get("unit") or ""), "remaining_unit": str(payload.get("unit") or ""),
            "plan": str(sub.get("plan_name") or ""),
            "total": limit, "remaining": remaining, "used": used,
            "used_pct": (round(used * 100.0 / limit, 2) if limit > 0 else None),
            "allowed": payload.get("is_active") is not False,
            "resets_at_ms": None, "expires_at_ms": None,
            "expires_at": str(sub.get("expires_at") or ""),
            "observed_at_ms": None,
        }
    raise ValueError("未知的 /usage 载荷形状")


def _fmt_ms(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _amt(v, unit):
    """额度金额：USD 显示 $x.xx，token 走 _fmt。"""
    if unit == "usd":
        try:
            return "$%.2f" % float(v or 0)
        except (TypeError, ValueError):
            return "$0.00"
    return _fmt(v)


def _short_err(exc):
    return ("%s: %s" % (type(exc).__name__, exc)).replace("\n", " ")[:160]


_QUOTA = {"ts": 0.0, "data": {}, "thread": None}
_QUOTA_LOCK = threading.Lock()


def _refresh_quota(root):
    """后台刷新：网络/解析失败只降级该通道，保留上一版好值并置 stale。"""
    provs = _providers(root)
    data = {}
    for name, cfg in QUOTA_API_PROVIDERS.items():
        pid = cfg["provider"]
        p = provs.get(pid)
        if not p:
            data[name] = {"ok": False, "source": "api",
                          "error": "provider_config 无 %s 的 key/baseUrl" % pid}
            continue
        try:
            snap = _parse_usage(_http_json(p["base"] + cfg.get("path", "/usage"), p["key"]))
            snap.update({"ok": True, "source": "api", "fetched_at": int(time.time())})
            data[name] = snap
        except urllib.error.HTTPError as exc:
            data[name] = {"ok": False, "source": "api",
                          "error": "HTTP %s" % exc.code, "fetched_at": int(time.time())}
        except Exception as exc:  # noqa: BLE001 网络/解析失败都只降级，不影响主数据
            data[name] = {"ok": False, "source": "api",
                          "error": _short_err(exc), "fetched_at": int(time.time())}
    with _QUOTA_LOCK:
        merged = dict(data)
        for name, v in data.items():
            prev = _QUOTA["data"].get(name)
            if not v.get("ok") and prev and prev.get("ok"):
                keep = dict(prev)
                keep["stale"] = True
                keep["error"] = v.get("error", "")
                merged[name] = keep
        _QUOTA["data"] = merged
        _QUOTA["ts"] = time.time()
        _QUOTA["thread"] = None


def _quota_snapshot(root, now_ts=None, ttl=QUOTA_TTL_S):
    """→ ({name: snapshot}, age_s)。过期只起后台线程刷新，本次先回上一版，不阻塞请求。"""
    now_ts = time.time() if now_ts is None else now_ts
    with _QUOTA_LOCK:
        have = bool(_QUOTA["data"])
        fresh = have and (now_ts - _QUOTA["ts"] < ttl)
        th = _QUOTA["thread"]
        if not fresh and not (th is not None and th.is_alive()):
            th = threading.Thread(target=_refresh_quota, args=(root,), daemon=True,
                                  name="lane-quota-refresh")
            _QUOTA["thread"] = th
            th.start()
        data = {k: dict(v) for k, v in _QUOTA["data"].items()}
        age = int(now_ts - _QUOTA["ts"]) if have else None
    return data, age


def _usd_targets(state_dir):
    """本地设定的 USD 额度（API 未返回 key 限额时兜底）；键为 lane，值为美元。"""
    d = _load_json(os.path.join(state_dir, "lane_quota_target.json"))
    usd = d.get("usd") if isinstance(d.get("usd"), dict) else {}
    out = {}
    for k, v in usd.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def _activity(db_path, since_ms, live_since_ms):
    """→ (db_ok, usage_rows|[], live_rows|[], err)

    usage_rows = [(session_id, provider_id, model_id, calls, tokens)]：窗口内已完成的模型调用
    live_rows  = [(session_id, provider_id|None, model_id|None)]：最近有 part 活动的会话，
                 通道按该会话最近一条 model_usage 推定（还没写成用量的新会话为 None）。
    part 查询失败只降级 live 信号，不推翻主口径。
    """
    if not os.path.isfile(db_path):
        return False, [], [], "会话库不存在: %s" % db_path
    uri = "file:" + db_path.replace(os.sep, "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        return False, [], [], "会话库打不开: %r" % (exc,)
    try:
        rows = con.execute(
            """select session_id, provider_id, model_id, count(*) calls,
                      sum(coalesce(computed_total_tokens, 0)) tokens
               from model_usage where started_at >= ? and status != 'cancelled'
               group by session_id, provider_id, model_id""",
            (int(since_ms),),
        ).fetchall()
    except sqlite3.Error as exc:
        con.close()
        return False, [], [], "model_usage 查询失败: %r" % (exc,)
    live = []
    try:
        live_ids = [r[0] for r in con.execute(
            "select distinct session_id from part where time_created >= ?",
            (int(live_since_ms),),
        ).fetchall()]
        if live_ids:
            live_ids = live_ids[:900]  # sqlite 变量上限保护
            latest = {}
            marks = ",".join("?" * len(live_ids))
            q = ("select session_id, provider_id, model_id, started_at from model_usage "
                 "where session_id in (%s)" % marks)
            for sid, prov, model, started in con.execute(q, live_ids):
                cur = latest.get(sid)
                if cur is None or (started or 0) >= cur[0]:
                    latest[sid] = (started or 0, prov, model)
            live = [(sid, latest.get(sid, (0, None, None))[1], latest.get(sid, (0, None, None))[2])
                    for sid in live_ids]
    except sqlite3.Error:
        live = []
    finally:
        con.close()
    return True, rows, live, ""


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _tps(tok_out, ms_sum):
    """tps = tok_out / (ms_sum/1000)；耗时合计非正 → None（不造数）。"""
    if not ms_sum or ms_sum <= 0:
        return None
    return round(float(tok_out) / (float(ms_sum) / 1000.0), 2)


def _gw_rows(path, since, budget=SPEED_READ_CAP, deadline=None, fail_sink=None):
    """从文件尾往前分块读网关日志，凑够窗口即停（最多读 budget=8MB，等效「大文件只读尾部」）。

    → (rows, partial)：rows 为 [(ts, lane, req_model, tok_out, ms)]（新→旧，求和与顺序无关）；
    partial=True 表示读到过半行/坏 JSON/预算或时限到头（已跳过未读部分），供 speed.source 标注；不抛错。
    分块倒读 + 遇到 ts < since 即返回，使解析耗时只跟窗口内请求量相关，不随日志体积膨胀
    （网关日志是 append-only、ts 单调递增：2026-11-26 当天 2,761 行实测逆序 0 条）；
    deadline（time.monotonic 绝对值）到点即返回，保证极端满窗口时也不会超时。
    fail_sink（C-20261126-G58）：传入 dict 时，窗口内 event=="fail" 的行按 req_model 累加失败次数
    （别名维度 fails）；不传则与旧行为一致（fail 行直接忽略）。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], True
    pos, carry = size, b""
    rows, partial = [], False
    with open(path, "rb") as fh:
        while pos > 0 and (size - pos) < budget:
            if deadline is not None and time.monotonic() > deadline:
                return rows, True                # 时限到头：已读部分照算，标 partial 不静默
            step = min(128 * 1024, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + carry
            parts = buf.split(b"\n")
            if pos == 0:
                segs, carry = parts, b""          # 文件首行前无换行，parts[0] 也是完整行
            else:
                carry, segs = parts[0], parts[1:]  # 块边界半行留到下一轮拼接
            for raw in reversed(segs):
                if not raw.strip():
                    continue
                txt = raw.decode("utf-8", "replace")
                if '"event"' not in txt:
                    continue                       # 便宜预筛：非网关事件行不必解析
                try:
                    o = json.loads(txt)
                except ValueError:
                    partial = True                 # 半行/坏行：跳过并标 partial
                    continue
                if not isinstance(o, dict):
                    continue
                ev = o.get("event")
                if ev != "ok" and not (ev == "fail" and fail_sink is not None):
                    continue
                try:
                    ts = float(o.get("ts"))
                except (TypeError, ValueError):
                    partial = True
                    continue
                if ts < since:
                    return rows, partial           # 倒读到窗口外 → 提前收工
                if ev != "ok":
                    req = str(o.get("req_model") or "")
                    if req:                        # 无 req_model 的失败无法归到别名，不造行
                        fail_sink[req] = fail_sink.get(req, 0) + 1
                    continue
                rows.append((ts, str(o.get("lane") or ""), str(o.get("req_model") or ""),
                             _int(o.get("tokens_out")), _int(o.get("ms"))))
            if pos == 0:
                break
    if pos > 0:
        partial = True      # 8MB 预算内没倒读到窗口起点 → 窗口数据可能不全，标 partial 不静默
    return rows, partial


def _speed_scan(log_dir, now_ms, window_min):
    """扫网关日志算近 window_min 分钟速度 + 别名维度聚合。

    → (speed, req_by_model, alias_log)
      speed        = {"window_min","source","lanes","total","unmapped"}（字段名对页面/单测固定）
      req_by_model = {req_model: {原始网关 lane: 请求数}}（auto 族归并取落点用，内部字段）
      alias_log    = {req_model: {"reqs","fails","tok_out","ms_sum","tps"}}（C-20261126-G58 网关模型表：
                     按 req_model 聚合，含落点不在通道表内的请求；event=ok 计 reqs/tokens/ms，
                     event=fail 只计 fails；tps=tok_out/(ms_sum/1000)，ms_sum<=0 → None）
    只读文件；跨午夜先读昨天再读今天；缺失 → source="missing"，截尾/坏行/半行 → "partial"。
    """
    since = (now_ms - window_min * 60 * 1000) / 1000.0
    now_s = now_ms / 1000.0
    deadline = time.monotonic() + SPEED_DEADLINE
    lanes, req_by_model, alias_log, fails = {}, {}, {}, {}
    rt_lanes, rt_alias = {}, {}          # 近 60 秒实时窗口（页面「实时速度」）
    unmapped = {"reqs": 0, "tok_out": 0, "lanes": {}}
    source, found, partial = "gateway-log", 0, False
    d0 = datetime.fromtimestamp(since).date()
    d1 = datetime.fromtimestamp(now_s).date()
    days, cur = [], d0
    while cur <= d1:
        days.append(cur.strftime("%Y%m%d"))
        cur = datetime.fromordinal(cur.toordinal() + 1).date()
    for day in days:
        path = os.path.join(log_dir, "gateway-%s.jsonl" % day)
        if not os.path.isfile(path):
            continue
        found += 1
        try:
            rows, bad = _gw_rows(path, since, deadline=deadline, fail_sink=fails)
        except OSError:
            partial = True
            continue
        if bad:
            partial = True
        for _ts, lane, req, tok, ms in rows:
            if _ts >= now_s - 60:
                rk = GW_LANE_MAP.get(lane)
                if rk:
                    r = rt_lanes.setdefault(rk, {"reqs": 0, "tok_out": 0, "ms_sum": 0})
                    r["reqs"] += 1; r["tok_out"] += tok; r["ms_sum"] += ms
                if req:
                    ra = rt_alias.setdefault(req, {"reqs": 0, "tok_out": 0, "ms_sum": 0})
                    ra["reqs"] += 1; ra["tok_out"] += tok; ra["ms_sum"] += ms
            if req:
                a = alias_log.setdefault(req, {"reqs": 0, "fails": 0, "tok_out": 0, "ms_sum": 0})
                a["reqs"] += 1
                a["tok_out"] += tok
                a["ms_sum"] += ms
            key = GW_LANE_MAP.get(lane)
            if key:
                a = lanes.setdefault(key, {"reqs": 0, "tok_out": 0, "ms_sum": 0})
                a["reqs"] += 1
                a["tok_out"] += tok
                a["ms_sum"] += ms
            else:
                a = unmapped["lanes"].setdefault(lane or "—", {"reqs": 0, "tok_out": 0})
                a["reqs"] += 1
                a["tok_out"] += tok
                unmapped["reqs"] += 1
                unmapped["tok_out"] += tok
            if req:
                m = req_by_model.setdefault(req, {})
                m[lane] = m.get(lane, 0) + 1
    for req, n in fails.items():
        alias_log.setdefault(req, {"reqs": 0, "fails": 0, "tok_out": 0, "ms_sum": 0})["fails"] += n
    if not found:
        source = "missing"
    elif partial:
        source = "partial"
    for v in lanes.values():
        v["tps"] = _tps(v["tok_out"], v["ms_sum"])
    for v in alias_log.values():
        v["tps"] = _tps(v["tok_out"], v["ms_sum"])
    for v in rt_lanes.values():
        v["tps"] = _tps(v["tok_out"], v["ms_sum"])
    for v in rt_alias.values():
        v["tps"] = _tps(v["tok_out"], v["ms_sum"])
    tok_sum = sum(v["tok_out"] for v in lanes.values())
    ms_sum = sum(v["ms_sum"] for v in lanes.values())
    speed = {
        "window_min": window_min, "source": source,
        "lanes": dict(sorted(lanes.items(), key=lambda kv: -kv[1]["tok_out"])),
        "total": {"reqs": sum(v["reqs"] for v in lanes.values()), "tok_out": tok_sum,
                  "ms_sum": ms_sum, "tps": _tps(tok_sum, ms_sum)},
        "unmapped": unmapped,
    }
    rt_tok = sum(v["tok_out"] for v in rt_lanes.values())
    rt_ms = sum(v["ms_sum"] for v in rt_lanes.values())
    speed["realtime"] = {
        "window_min": 1, "source": source,
        "lanes": dict(sorted(rt_lanes.items(), key=lambda kv: -kv[1]["tok_out"])),
        "total": {"reqs": sum(v["reqs"] for v in rt_lanes.values()), "tok_out": rt_tok,
                  "ms_sum": rt_ms, "tps": _tps(rt_tok, rt_ms)},
        "aliases": dict(sorted(rt_alias.items(), key=lambda kv: -kv[1]["tok_out"])),
    }
    return speed, req_by_model, alias_log


def _merge_direct_speed(speed, db_path, now_ms, window_min, pairs):
    """把 ZCode 直连调用（provider_id/model_id 命中通道 pairs）的速度并进网关口径。

    网关停用后 logs/gateway-*.jsonl 不再增长；工厂直连的 model_usage 仍在写，
    输出 tokens_out ÷ duration_ms 就是真实速度。网关日志与直连 DB 不重叠
    （网关会话 provider=new-provider-7，不在通道 pairs 内），相加不重复计。
    """
    prov_lane = {}
    for lane, ps in (pairs or {}).items():
        for pid, mid in ps:
            prov_lane.setdefault(str(pid), {})[str(mid)] = lane
    if not prov_lane or not db_path or not os.path.exists(db_path):
        return speed
    since = int(now_ms - window_min * 60 * 1000)
    rt_since = int(now_ms - 60 * 1000)
    try:
        con = sqlite3.connect("file:" + str(db_path).replace("\\", "/") + "?mode=ro", uri=True, timeout=3)
        rows = con.execute(
            "SELECT provider_id, model_id, COALESCE(output_tokens,0), COALESCE(duration_ms,0), "
            "COALESCE(first_token_at,0), COALESCE(completed_at,0), started_at "
            "FROM model_usage WHERE started_at>=? AND status='completed' "
            "AND output_tokens>0 AND duration_ms>0",
            (since,),
        ).fetchall()
        con.close()
    except Exception:
        return speed
    agg5, agg1 = {}, {}
    for pid, mid, tout, dur, fts, cts, ts in rows:
        lane = (prov_lane.get(str(pid)) or {}).get(str(mid))
        if not lane:
            continue
        # 真实解码速度：首字到完成；没有 first_token_at 的旧记录回退整段 duration_ms
        dec_ms = (int(cts) - int(fts)) if int(cts) > int(fts) > 0 else int(dur or 0)
        a = agg5.setdefault(lane, {"reqs": 0, "tok_out": 0, "ms_sum": 0})
        a["reqs"] += 1
        a["tok_out"] += int(tout or 0)
        a["ms_sum"] += dec_ms
        if int(ts or 0) >= rt_since:
            b = agg1.setdefault(lane, {"reqs": 0, "tok_out": 0, "ms_sum": 0})
            b["reqs"] += 1
            b["tok_out"] += int(tout or 0)
            b["ms_sum"] += dec_ms
    if not agg5:
        return speed
    for lane, a in agg5.items():
        cur = speed.setdefault("lanes", {}).setdefault(lane, {"reqs": 0, "tok_out": 0, "ms_sum": 0})
        cur["reqs"] += a["reqs"]
        cur["tok_out"] += a["tok_out"]
        cur["ms_sum"] += a["ms_sum"]
    rt = speed.setdefault("realtime", {})
    rt_lanes = rt.setdefault("lanes", {})
    for lane, a in agg1.items():
        cur = rt_lanes.setdefault(lane, {"reqs": 0, "tok_out": 0, "ms_sum": 0})
        cur["reqs"] += a["reqs"]
        cur["tok_out"] += a["tok_out"]
        cur["ms_sum"] += a["ms_sum"]
    for v in speed["lanes"].values():
        v["tps"] = _tps(v["tok_out"], v["ms_sum"])
    for v in rt_lanes.values():
        v["tps"] = _tps(v["tok_out"], v["ms_sum"])
    tok_sum = sum(v["tok_out"] for v in speed["lanes"].values())
    ms_sum = sum(v["ms_sum"] for v in speed["lanes"].values())
    speed["total"] = {"reqs": sum(v["reqs"] for v in speed["lanes"].values()),
                      "tok_out": tok_sum, "ms_sum": ms_sum, "tps": _tps(tok_sum, ms_sum)}
    rt_tok = sum(v["tok_out"] for v in rt_lanes.values())
    rt_ms = sum(v["ms_sum"] for v in rt_lanes.values())
    rt["total"] = {"reqs": sum(v["reqs"] for v in rt_lanes.values()), "tok_out": rt_tok,
                   "ms_sum": rt_ms, "tps": _tps(rt_tok, rt_ms)}
    src = str(speed.get("source") or "")
    if "zcode-db" not in src:
        speed["source"] = "zcode-db" if src in ("", "missing") else ("partial+zcode-db" if src == "partial" else "gateway-log+zcode-db")
        rt["source"] = speed["source"]
    return speed


def _alias_target(alias, req_by_model, alias_order):
    """→ (落点 lane, basis, reqs)：窗口内该别名请求最多的真实 lane 优先，否则按路由主选。"""
    per = req_by_model.get(alias) or {}
    cands = []
    for i, lane in enumerate(LANE_ORDER):
        n = per.get(_ROUTE_ID_OF.get(lane, ""), 0)
        if n > 0:
            cands.append((n, i, lane))
    if cands:
        cands.sort(key=lambda t: (-t[0], t[1]))
        return cands[0][2], "log", cands[0][0]
    for rid in alias_order.get(alias) or []:
        lane = GW_LANE_MAP.get(rid)
        if lane in LANE_ORDER:      # APIgoto 不在通道表内 → 顺延下一个主选
            return lane, "routing", 0
    return "R4", "routing", 0


_ROUTE_ID_OF = {v: k for k, v in GW_LANE_MAP.items()}


def _month_tokens(state_dir, month_key):
    d = _load_json(os.path.join(state_dir, "v3_budget_ledger.json"))
    m = (d.get("month_lane_tokens") or {}).get(month_key)
    if not isinstance(m, dict):
        return {}, str(d.get("date") or "")
    return {str(k): int(v or 0) for k, v in m.items()}, str(d.get("date") or "")


_ORDER_RE = re.compile(r"(C-\d{8}-[A-Za-z0-9]+)")


def _order_speeds(db_path, now_ms, window_min, pairs=None):
    """按订单聚合 ZCode 直连会话速度：近 60 秒实时 + 近 window_min 分钟均速。

    只统计通道 pairs 内的 provider/model（网关 new-provider-7 不计），
    会话标题里的订单号（C-YYYYMMDD-XXX）决定归属；同一订单多阶段会话合并。
    """
    out = {}
    if not db_path or not os.path.exists(db_path):
        return out
    allow = None
    if pairs:
        allow = set()
        for ps in pairs.values():
            for pid, mid in ps:
                allow.add((str(pid), str(mid)))
    since = int(now_ms - window_min * 60 * 1000)
    rt_since = int(now_ms - 60 * 1000)
    try:
        con = sqlite3.connect("file:" + str(db_path).replace("\\", "/") + "?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT u.session_id, s.title, u.provider_id, u.model_id, "
            "COALESCE(u.output_tokens,0), COALESCE(u.duration_ms,0), "
            "COALESCE(u.first_token_at,0), COALESCE(u.completed_at,0), u.started_at "
            "FROM model_usage u LEFT JOIN session s ON s.id=u.session_id "
            "WHERE u.started_at>=? AND u.status='completed' "
            "AND u.output_tokens>0 AND u.duration_ms>0",
            (since,),
        ).fetchall()
        con.close()
    except Exception:
        return out
    for sid, title, pid, mid, tout, dur, fts, cts, ts in rows:
        if allow is not None and (str(pid), str(mid)) not in allow:
            continue
        m = _ORDER_RE.search(str(title or ""))
        if not m:
            continue
        oid = m.group(1)
        dec_ms = (int(cts) - int(fts)) if int(cts) > int(fts) > 0 else int(dur or 0)
        a = out.setdefault(oid, {
            "rt_tok_out": 0, "rt_ms_sum": 0, "tok_out": 0, "ms_sum": 0,
            "rt_tps": None, "tps": None, "session_id": "", "session_title": "",
            "provider_id": "", "model_id": "", "sessions": set(), "last_ts": 0,
        })
        tout, dur, ts = int(tout or 0), dec_ms, int(ts or 0)
        a["tok_out"] += tout
        a["ms_sum"] += dur
        if ts >= rt_since:
            a["rt_tok_out"] += tout
            a["rt_ms_sum"] += dur
        if ts >= a["last_ts"]:
            a["last_ts"] = ts
            a["session_id"] = str(sid or "")
            a["session_title"] = str(title or "")
            a["provider_id"] = str(pid or "")
            a["model_id"] = str(mid or "")
        a["sessions"].add(str(sid))
    for a in out.values():
        a["rt_tps"] = _tps(a["rt_tok_out"], a["rt_ms_sum"])
        a["tps"] = _tps(a["tok_out"], a["ms_sum"])
        a["sessions"] = len(a["sessions"])
        a.pop("last_ts", None)
    return out


def _zcode_sessions(db_path, now_ms, window_min):
    """逐会话真遥测：直接来自 ZCode model_usage 的 provider/model/tokens/TTFT/解码速度。

    decode_tps = output_tokens / (completed_at - first_token_at)，即首字后的真实生成速度；
    没有 first_token_at 的记录回退 duration_ms，并在 tps_basis 标 wall。
    """
    out = []
    if not db_path or not os.path.exists(db_path):
        return out
    since = int(now_ms - window_min * 60 * 1000)
    try:
        con = sqlite3.connect("file:" + str(db_path).replace("\\", "/") + "?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT u.session_id, s.title, MAX(u.provider_id), MAX(u.model_id), COUNT(*), "
            "SUM(COALESCE(u.output_tokens,0)), SUM(COALESCE(u.duration_ms,0)), "
            "SUM(CASE WHEN u.first_token_at>0 AND u.completed_at>u.first_token_at "
            "THEN u.completed_at-u.first_token_at ELSE 0 END), "
            "SUM(CASE WHEN u.first_token_at>0 AND u.completed_at>u.first_token_at THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN u.time_to_first_token_ms>0 THEN u.time_to_first_token_ms ELSE 0 END), "
            "SUM(CASE WHEN u.time_to_first_token_ms>0 THEN 1 ELSE 0 END), "
            "MAX(u.started_at), MAX(u.status) "
            "FROM model_usage u LEFT JOIN session s ON s.id=u.session_id "
            "WHERE u.started_at>=? AND u.status!='cancelled' "
            "GROUP BY u.session_id ORDER BY MAX(u.started_at) DESC LIMIT 30",
            (since,),
        ).fetchall()
        con.close()
    except Exception:
        return out
    for (sid, title, pid, mid, reqs, tout, dur, dec_ms, dec_n, ttft_sum, ttft_n, last_ts, status) in rows:
        tout, dur, dec_ms, last_ts = int(tout or 0), int(dur or 0), int(dec_ms or 0), int(last_ts or 0)
        decode_tps = _tps(tout, dec_ms) if dec_ms > 0 else None
        wall_tps = _tps(tout, dur) if dur > 0 else None
        m = _ORDER_RE.search(str(title or ""))
        out.append({
            "session_id": str(sid or ""),
            "title": str(title or ""),
            "order_id": m.group(1) if m else "",
            "provider_id": str(pid or ""),
            "model_id": str(mid or ""),
            "reqs": int(reqs or 0),
            "tokens_out": tout,
            "duration_ms": dur,
            "decode_ms": dec_ms,
            "decode_tps": decode_tps,
            "wall_tps": wall_tps,
            "tps_basis": "decode" if dec_n else "wall",
            "ttft_ms": round(float(ttft_sum or 0) / float(ttft_n), 1) if ttft_n else None,
            "last_ts": last_ts,
            "age_s": round(max(0.0, (now_ms - last_ts) / 1000.0), 1) if last_ts else None,
            "status": str(status or ""),
        })
    return out


def _factory_with_speeds(root, state_dir, db_path, now_ms, window_min, pairs=None):
    f = _factory_slots(root, state_dir)
    try:
        speeds = _order_speeds(db_path, now_ms, window_min, pairs)
        for o in f.get("orders") or []:
            o["speed"] = speeds.get(str(o.get("id"))) or {}
    except Exception:
        pass
    return f


def _factory_slots(root, state_dir):
    cfg = _load_json(os.path.join(root, "config", "steward.json"))
    v4 = cfg.get("v4") if isinstance(cfg.get("v4"), dict) else {}
    slots = v4.get("slots") if isinstance(v4.get("slots"), dict) else {}
    rs = _load_json(os.path.join(state_dir, "runner_slots.json"))
    raw_slots = rs.get("slots") if isinstance(rs.get("slots"), dict) else {}
    active = len(raw_slots)
    try:
        mx = int(rs.get("max") or slots.get("max") or 1)
    except (TypeError, ValueError):
        mx = 1
    # 在制单号（slot_ledger 落盘结构 {order_id: {family, pid, claimed_at, heartbeat_at}}）
    orders = []
    for oid, meta in raw_slots.items():
        meta = meta if isinstance(meta, dict) else {}
        orders.append({"id": str(oid), "family": str(meta.get("family") or "")})
    orders.sort(key=lambda x: x["id"])
    return {"enabled": bool(slots.get("enabled")), "max": mx, "active": active,
            "orders": orders}


def collect(root=None, db_path=None, now_ms=None, window_min=5, state_dir=None, live_min=3,
            quota=None, log_dir=None):
    """quota=None 走官方接口缓存；测试传 dict 注入、传 {} 关闭取数（不发网络请求）。
    log_dir=None 走 LANE_LOG_DIR / 仓库 logs（速度段与 auto 族归并的日志来源）。"""
    root = root or ROOT
    state_dir = state_dir or os.path.join(root, "state")
    db_path = db_path or DEFAULT_DB
    log_dir = log_dir or LOGS
    now = datetime.fromtimestamp((now_ms or time.time() * 1000) / 1000.0)
    now_ms = int(now.timestamp() * 1000)
    month_key = now.strftime("%Y-%m")
    caps, caps_note, caps_defaulted, cap_sources = _caps(state_dir)
    pairs, primary = _pairs(root)
    alias_order = _aliases(root)
    alias_ids = _alias_ids(root)
    speed, req_by_model, alias_log = _speed_scan(log_dir, now_ms, window_min)
    speed = _merge_direct_speed(speed, db_path, now_ms, window_min, pairs)
    quotas = _quotas()
    usd_targets = _usd_targets(state_dir)
    month_tokens, ledger_date = _month_tokens(state_dir, month_key)
    if quota is None:
        quota_api, quota_age = _quota_snapshot(root, now_ts=now_ms / 1000.0)
    else:
        quota_api, quota_age = dict(quota), None

    db_ok, rows, live_rows, db_err = _activity(
        db_path, now_ms - window_min * 60 * 1000, now_ms - live_min * 60 * 1000)

    # pair → lane；同一会话跨道计两次仅影响 lane_used_sum，totals.used 用去重会话数
    pair_lane = {}
    for lane, ps in pairs.items():
        for p in ps:
            pair_lane[p] = lane
    sess_by_lane = {k: set() for k in LANE_ORDER}
    sess_unknown = set()
    unknown_pairs = {}
    all_sessions = set()
    auto_sess = {a: {} for a in alias_ids}         # 别名 → {session_id: tokens}
    for sid, prov, model, calls, tokens in rows:
        all_sessions.add(sid)
        if prov == AUTO_PROVIDER and model in auto_sess:
            # 别名（auto 族 + relay）：不是通道，先记账；auto 族稍后按日志落点/路由归并进真实 lane，
            # relay 只进 gateway_models，不并入任何通道（merged_to=[]）。relay 不走 auto 族归并，
            # 按旧语义仍归「未归属」——unattributed/totals 口径不动，且 Σ各卡+未归属=去重会话 闭环不破。
            auto_sess[model][sid] = auto_sess[model].get(sid, 0) + _int(tokens)
            if model in NO_MERGE_ALIASES:
                sess_unknown.add(sid)
                key = "%s/%s" % (prov, model)
                unknown_pairs[key] = unknown_pairs.get(key, 0) + int(calls)
            continue
        lane = pair_lane.get((prov, model))
        if lane:
            sess_by_lane[lane].add(sid)
        else:
            sess_unknown.add(sid)
            key = "%s/%s" % (prov, model)
            unknown_pairs[key] = unknown_pairs.get(key, 0) + int(calls)

    # 进行中会话并入：不必等 model_usage 落库（那要等一次调用结束）
    all_usage = set(all_sessions)
    live_now = set()
    sess_live_by_lane = {k: set() for k in LANE_ORDER}
    auto_live = {a: set() for a in alias_ids}
    for sid, prov, model in live_rows:
        live_now.add(sid)
        all_sessions.add(sid)
        if prov == AUTO_PROVIDER and model in auto_sess:
            auto_sess[model].setdefault(sid, 0)
            auto_live[model].add(sid)
            if model in NO_MERGE_ALIASES:   # 同 usage 口径：relay 仍算未归属，闭环不破
                sess_unknown.add(sid)
                unknown_pairs.setdefault("%s/%s" % (prov, model), 0)
            continue
        lane = pair_lane.get((prov, model)) if (prov and model) else None
        if lane:
            sess_live_by_lane[lane].add(sid)
            sess_by_lane[lane].add(sid)
        else:
            sess_unknown.add(sid)
            if prov and model:
                key = "%s/%s" % (prov, model)
                unknown_pairs.setdefault(key, 0)

    # auto 族归并：落点 lane 的 used/live 按去重会话集合并入，merged 记账（会话/tokens/basis/reqs）
    merged_by_lane = {k: [] for k in LANE_ORDER}
    for alias in AUTO_ALIASES:
        sess = auto_sess.get(alias) or {}
        per = req_by_model.get(alias) or {}
        mapped = sum(n for rid, n in per.items() if rid in GW_LANE_MAP)
        if not sess and not mapped:
            continue        # 窗口内既无会话也无已映射请求 → 不落空条目（未映射请求走 speed.unmapped）
        target, basis, reqs = _alias_target(alias, req_by_model, alias_order)
        merged_by_lane[target].append({
            "alias": alias, "sessions": len(sess), "tokens": sum(sess.values()),
            "basis": basis, "reqs": reqs})
        sess_by_lane[target] |= set(sess)
        sess_live_by_lane[target] |= auto_live.get(alias) or set()
    for v in merged_by_lane.values():
        v.sort(key=lambda m: (-m["sessions"], m["alias"]))

    # 网关模型（C-20261126-G58）：别名维度，行序＝_alias_ids（routing 键在前、缺的补后）。
    # 会话/进行中/tokens 取会话库 provider=new-provider-7 且 model=别名（与 lanes 同窗口）；
    # reqs/fails/tok_out/ms_sum/tps 取网关日志按 req_model 聚合；别名不占容量、不进 cap 汇总。
    gateway_models = []
    for alias in alias_ids:
        sess_alias = auto_sess.get(alias) or {}
        lg = alias_log.get(alias) or {}
        rt = ((speed.get("realtime") or {}).get("aliases") or {}).get(alias) or {}
        reqs, fails = int(lg.get("reqs") or 0), int(lg.get("fails") or 0)
        tok_out, ms_sum = int(lg.get("tok_out") or 0), int(lg.get("ms_sum") or 0)
        merged_to = [lane for lane in LANE_ORDER
                     if alias not in NO_MERGE_ALIASES
                     and any(m["alias"] == alias for m in (merged_by_lane.get(lane) or []))]
        gateway_models.append({
            "id": alias,
            # 会话库不可读 → None（与 lanes.used 同口径，不伪造 0）
            "sessions": len(sess_alias) if db_ok else None,
            "live": len(auto_live.get(alias) or set()) if db_ok else None,
            "tokens": sum(sess_alias.values()) if db_ok else None,
            "reqs": reqs, "fails": fails, "tok_out": tok_out, "ms_sum": ms_sum,
            "tps": _tps(tok_out, ms_sum),
            "rt_reqs": int(rt.get("reqs") or 0),
            "rt_tps": _tps(int(rt.get("tok_out") or 0), int(rt.get("ms_sum") or 0)),
            "merged_to": merged_to,
            # reqs>0 且无失败 → ok；有失败 → warn；无样本 → idle
            "status": ("warn" if fails > 0 else ("ok" if reqs > 0 else "idle")),
        })
    gw_sessions = (sum(x["sessions"] for x in gateway_models)
                   if all(x["sessions"] is not None for x in gateway_models) else None)
    gw_tok_out = sum(x["tok_out"] for x in gateway_models)
    gw_ms_sum = sum(x["ms_sum"] for x in gateway_models)
    gateway_totals = {
        "sessions": gw_sessions,
        "reqs": sum(x["reqs"] for x in gateway_models),
        "tok_out": gw_tok_out,
        "tps": _tps(gw_tok_out, gw_ms_sum),
    }

    lanes = []
    for lane in LANE_ORDER:
        cap = caps.get(lane)  # None = lane_caps.json 未立条目 → 页面显示「—」不造数
        cap_source = cap_sources.get(lane, "none")
        # cap 是会话并发上限，在飞会话数 > cap 判超限
        cap_mode = "sessions"
        qkey = QUOTA_KEY.get(lane, "")
        quota = quotas.get(qkey, None) if qkey else None
        mtok = month_tokens.get(qkey, 0) if qkey else None
        ledger_tokens = mtok
        ledger_pct = (float(mtok) / quota * 100.0) if (quota and mtok is not None) else None
        pct = ledger_pct
        real = quota_api.get(lane) if isinstance(quota_api.get(lane), dict) else None
        quota_source, quota_plan, quota_remaining, quota_error = "none", "", None, ""
        quota_resets_at = quota_expires_at = ""
        quota_unit = ""
        quota_basis, quota_target = "", None
        quota_today_usd = quota_today_requests = quota_week_usd = None
        if real and real.get("ok"):
            # 官方额度优先：token（STEP）或 USD（R4/R4-2 key 限额、APIgoto 余额）
            quota_source = "api"
            quota_plan = str(real.get("plan") or "")
            quota_remaining = real.get("remaining")
            quota_error = str(real.get("error") or "")
            quota_unit = str(real.get("unit") or "")
            quota_today_usd = real.get("today_usd")
            quota_today_requests = real.get("today_requests")
            quota_week_usd = real.get("week_usd")
            quota_resets_at = _fmt_ms(real.get("resets_at_ms")) or str(real.get("resets_at") or "")
            quota_expires_at = _fmt_ms(real.get("expires_at_ms")) or str(real.get("expires_at") or "")
            try:
                total = float(real.get("total")) if real.get("total") is not None else None
            except (TypeError, ValueError):
                total = None
            try:
                quota_target = (float(usd_targets[lane]) if lane in usd_targets else None)
            except (TypeError, ValueError):
                quota_target = None
            # 平台限额优先；API 未返回（R4 limit_usd=null）时用本地设定
            effective = total if (total and total > 0) else (
                quota_target if (quota_target and quota_target > 0) else None)
            quota_basis = ("api" if (total and total > 0)
                           else ("target" if effective else ""))
            if effective:
                quota = effective
                if quota_unit == "usd":
                    mtok = round(float(real.get("used") or 0), 4)
                    if quota_remaining is None:
                        quota_remaining = round(max(0.0, effective - float(mtok)), 4)
                else:
                    quota = int(effective)
                    mtok = max(0, int(effective - float(quota_remaining or 0)))
                pct = (mtok * 100.0 / effective) if effective else None
            else:
                # 官方明确“未设上限”且无本地设定 → 不画假进度条
                quota, mtok, pct = None, None, None
        elif qkey and quota is not None:
            quota_source = "ledger"
            if mtok is not None:
                quota_remaining = max(0, quota - mtok)

        used = len(sess_by_lane[lane]) if db_ok else None
        available, status, kind, reason = True, "可用", "ok", ""
        if lane == "GLM":
            # GLM 只有 enc OAuth、无 apiKey，官方额度接口不可达 → 恒不可用
            available, status, kind = False, "不可用", "down"
            reason = "无可用额度：智谱 GLM-5.3-Flash 官方直连、不进网关"
        elif quota_source == "api":
            label_src = "本地设定额度" if quota_basis == "target" else "官方额度"
            if real.get("allowed") is False:
                available, status, kind = False, "不可用", "down"
                reason = "官方额度不可用" + ("：" + quota_error if quota_error else "")
            elif quota is None:
                if quota_unit == "usd" and quota_today_usd is not None:
                    reason = "官方：今日 $%.2f" % quota_today_usd
                    if quota_today_requests is not None:
                        reason += " · %d 次" % quota_today_requests
                    if quota_week_usd is not None:
                        reason += " · 近7日 $%.2f" % quota_week_usd
                    reason += " · 未设 key 上限"
                else:
                    status, kind = "待核", "unknown"
                    reason = "官方未返回可用上限"
            elif pct is None:
                status, kind = "待核", "unknown"
                reason = "官方额度缺 used_percent"
            elif pct >= 100.0:
                available, status, kind = False, "耗尽", "down"
                reason = "%s已用尽（剩余 %s）" % (label_src, _amt(quota_remaining, quota_unit))
            elif pct >= FUSE_PCT * 100.0:
                status, kind = "预警", "warn"
                reason = "%s已用 %.1f%%，触 %.0f%% 熔断线，换道分流（不停机）" % (
                    label_src, pct, FUSE_PCT * 100)
            else:
                if quota_unit == "usd":
                    if quota_basis == "target":
                        reason = "按本地设定 $%.2f 计：已用 $%.2f（剩余 $%.2f）" % (
                            float(quota), float(mtok or 0), float(quota_remaining or 0))
                    else:
                        reason = "官方额度：已用 $%.2f / $%.2f（剩余 $%.2f）" % (
                            float(mtok or 0), float(quota), float(quota_remaining or 0))
                    if quota_today_usd is not None:
                        reason += "，今日 $%.2f" % quota_today_usd
                    if (quota_target is not None and quota_basis == "api"
                            and abs(float(quota_target) - float(quota)) > 0.005):
                        reason += "；本地目标 $%.2f，平台当前限额 $%.2f（调整后自动跟随）" % (
                            float(quota_target), float(quota))
                else:
                    reason = "%s：剩余 %s / %s（%.1f%%）" % (
                        label_src, _fmt(quota_remaining), _fmt(quota), pct)
                    if quota_resets_at:
                        reason += "，%s 重置" % quota_resets_at
            if real.get("stale"):
                reason += "；本次取数失败沿用上次值" + ("（%s）" % quota_error if quota_error else "")
        elif quota is not None:
            if pct is None:
                status, kind = "待核", "unknown"
                reason = "月包台账不可读，用量未知"
            elif pct >= 100.0:
                available, status, kind = False, "耗尽", "down"
                reason = "月包 Token 已用尽（%s/%s）" % (_fmt(mtok), _fmt(quota))
            elif pct >= FUSE_PCT * 100.0:
                status, kind = "预警", "warn"
                reason = "已触 %.0f%% 熔断线，换道分流（不停机）" % (FUSE_PCT * 100)
        elif lane == "NEX":
            status, kind, reason = "不限量", "ok", "限时免费，token 不设闸"
        elif lane == "OUTSOURCE":
            status, kind, reason = "按量", "ok", "外包通道按量结算，不计月包"
        if db_ok and cap is not None and used > cap and cap_mode == "sessions":
            kind = "warn" if kind == "ok" else kind
            status = "超限" if status == "可用" else status
            reason = (reason + "；" if reason else "") + "在飞 %d > cap %d" % (used, cap)
        spd = speed["lanes"].get(lane) or {}
        lanes.append({
            "id": lane, "desc": DESC.get(lane, ""),
            "model": _display_model(primary.get(lane), pairs.get(lane) or set()),
            "pairs": sorted("%s/%s" % p for p in (pairs.get(lane) or set())),
            "cap": cap, "used": used, "cap_source": cap_source, "cap_mode": cap_mode,
            "live": (len(sess_live_by_lane[lane]) if db_ok else None),
            "merged": merged_by_lane.get(lane) or [],
            "speed": ({"reqs": spd.get("reqs"), "tok_out": spd.get("tok_out"),
                       "ms_sum": spd.get("ms_sum"), "tps": spd.get("tps")}
                      if spd else {"reqs": 0, "tok_out": 0, "ms_sum": 0, "tps": None}),
            "available": available, "status": status, "kind": kind, "reason": reason,
            "month_tokens": mtok, "month_quota": quota,
            "quota_pct": round(pct, 2) if pct is not None else None,
            "quota_source": quota_source, "quota_plan": quota_plan,
            "quota_unit": quota_unit, "quota_basis": quota_basis,
            "quota_target": quota_target, "quota_remaining": quota_remaining,
            "quota_today_usd": quota_today_usd, "quota_today_requests": quota_today_requests,
            "quota_week_usd": quota_week_usd, "quota_error": quota_error,
            "quota_resets_at": quota_resets_at, "quota_expires_at": quota_expires_at,
            "ledger_tokens": ledger_tokens,
            "ledger_pct": round(ledger_pct, 2) if ledger_pct is not None else None,
        })

    usable_cap = sum(x["cap"] or 0 for x in lanes if x["available"])
    used_total = len(all_sessions) if db_ok else None
    lane_used_sum = sum(x["used"] or 0 for x in lanes) if db_ok else None
    unattr_used = len(sess_unknown) if db_ok else None
    # 口径闭环：totals.used == Σ各卡 used + unattributed.used
    # （同一会话跨通道时 Σ各卡 会重复计一次，此时 reconciled=False，totals.used 仍按去重会话数）
    used_reconciled = None
    if db_ok and used_total is not None and lane_used_sum is not None and unattr_used is not None:
        used_reconciled = (lane_used_sum + unattr_used == used_total)
    ok_sources = sorted(k for k, v in quota_api.items() if isinstance(v, dict) and v.get("ok"))
    fail_sources = sorted(k for k, v in quota_api.items() if isinstance(v, dict) and not v.get("ok"))
    extra = []
    for name, snap in quota_api.items():
        if name in LANE_ORDER or not isinstance(snap, dict):
            continue
        meta = QUOTA_EXTRA_META.get(name, {})
        extra.append({
            "id": name, "ok": bool(snap.get("ok")),
            "model": meta.get("model", ""), "label": meta.get("label", "兜底通道"),
            "note": meta.get("note", ""),
            "plan": str(snap.get("plan") or ""),
            "remaining": snap.get("remaining"),
            "unit": str(snap.get("remaining_unit") or snap.get("unit") or ""),
            "pct": snap.get("used_pct"),
            "expires_at": _fmt_ms(snap.get("expires_at_ms")) or str(snap.get("expires_at") or ""),
            "error": str(snap.get("error") or ""), "stale": bool(snap.get("stale")),
        })
    merged_notes = []
    for lane in LANE_ORDER:
        for m in merged_by_lane.get(lane) or []:
            merged_notes.append("%s→%s %d 会话 / %s tok（%s%s）" % (
                m["alias"], lane, m["sessions"], _fmt(m["tokens"]),
                "日志" if m["basis"] == "log" else "路由主选",
                ("，%d 次请求" % m["reqs"]) if m["reqs"] else ""))
    notes = [
        "在跑 = 最近 %d 分钟内有调用、或最近 %d 分钟内有步骤活动（对话进行中即计入）的不同会话数；只读会话库"
        % (window_min, live_min),
        "总并发上限 = 各通道 cap 之和（auto 族别名不是通道，已不再计入；"
        "工厂施工道数另见 v4.slots）；GLM 标不可用后不计入「可用上限」",
        "本页 cap 是排班设计上限，工厂实际并行执行槽另见顶部 v4.slots（当前 N=%d）" % _factory_slots(root, state_dir)["max"],
        "auto/auto-heavy/auto-night/auto-cheap/auto-reason 是网关别名（new-provider-7）不是通道："
        "按窗口内网关日志的实际落点归并到真实通道，无日志时按 routing.json 别名主选归并；"
        "会话与 tokens 计入落点通道并在 merged 里逐别名记账",
        "口径闭环：totals.used = Σ各通道在跑 + 未归属在跑（同一会话跨通道时 Σ各通道 会重复计，"
        "此时 totals.used 仍按去重会话数，reconciled=False）",
    ]
    if merged_notes:
        notes.append("auto 族归并明细（近 %d 分钟）：%s" % (window_min, "；".join(merged_notes)))
    notes.append("网关模型（近 %d 分钟）：%d 个别名，会话/token 取会话库 provider=new-provider-7 的别名维度，"
                 "请求/失败/速度取网关日志 req_model；别名不是通道，不计入总上限/可用上限"
                 % (window_min, len(gateway_models)))
    notes.append("速度：近 %d 分钟 event=ok 的 tokens_out ÷ Σms；来源 %s" % (
        window_min, {"gateway-log": "网关日志 logs/gateway-*.jsonl",
                     "partial": "网关日志（含截尾/坏行，已跳过）",
                     "missing": "缺失（速度显示 —）",
                     "gateway-log+zcode-db": "网关日志 + ZCode 直连 model_usage",
                     "partial+zcode-db": "网关日志（含坏行）+ ZCode 直连 model_usage",
                     "zcode-db": "ZCode 直连 model_usage（网关已不参与派单）"
                     }.get(speed["source"], speed["source"])))
    if quota_api:
        notes.append(
            "额度来源：%s 取官方额度接口 /usage、/cli/key（缓存 %d 秒，后台刷新）%s；其余通道官方无余额接口，按台账/定额口径"
            % ("、".join(ok_sources) or "无", QUOTA_TTL_S,
               ("；本次失败：" + "、".join(fail_sources)) if fail_sources else ""))
        if quota_age is not None:
            notes.append("官方额度最近一次取数：%d 秒前" % quota_age)
    if extra:
        notes.append("兜底通道（%s）单列展示，不计入总上限/可用上限：%s"
                     % ("、".join(q["id"] for q in extra),
                        "；".join(q["note"] for q in extra if q.get("note"))))
    if usd_targets:
        notes.append("USD 额度：平台 key 限额优先；API 未返回时按本地设定（%s）；目标与平台不一致时以平台为准并在悬停注明"
                     % "、".join("%s $%.0f" % (k, v) for k, v in sorted(usd_targets.items())))
    if ledger_date and ledger_date != now.strftime("%Y-%m-%d"):
        notes.append("月包台账日期 %s（今日 %s）；factory_runner 跨日滚动只清日计数、保留当月月包计数"
                     % (ledger_date, now.strftime("%Y-%m-%d")))
    if caps_note:
        notes.append("caps 来源备注：" + caps_note)
    if caps_defaulted:
        notes.append("cap 缺条目：%s 未在 state/lane_caps.json 立卡 → 该卡 cap 显示「—」"
                     "（代码默认值 %s，仅注记不造数）"
                     % ("、".join(caps_defaulted),
                        "、".join("%s=%s" % (k, DEFAULT_CAPS.get(k, "—")) for k in caps_defaulted)))
    fallback_lanes = [k for k in LANE_ORDER if cap_sources.get(k) == "default"]
    if fallback_lanes:
        notes.append("state/lane_caps.json 缺失或不可读 → %s 的 cap 回落代码默认值（%s），"
                     "非账本口径，恢复该文件即自动回落账本值"
                     % ("、".join(fallback_lanes),
                        "、".join("%s=%s" % (k, DEFAULT_CAPS.get(k, "—")) for k in fallback_lanes)))

    return {
        "ok": True,
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "window_min": window_min,
        "live_min": live_min,
        "db_ok": db_ok,
        "db_error": db_err,
        "month": month_key,
        "ledger_date": ledger_date,
        "quota_age_s": quota_age,
        "quota_extra": extra,
        "speed": speed,
        "gateway_models": gateway_models,
        "gateway_totals": gateway_totals,
        "lanes": lanes,
        "totals": {
            "cap": sum(v for v in caps.values() if isinstance(v, int)),
            "usable_cap": usable_cap,
            "used": used_total,
            "live": len(live_now) if db_ok else None,
            "live_only": (len(live_now - all_usage) if db_ok else None),
            "lane_used_sum": lane_used_sum,
            "unattributed_used": unattr_used,
            "used_reconciled": used_reconciled,
            "caps_defaulted": caps_defaulted,
            "available_lanes": sum(1 for x in lanes if x["available"]),
            "down_lanes": sum(1 for x in lanes if not x["available"]),
            "tok_out": speed["total"]["tok_out"],
            "tps": speed["total"]["tps"],
        },
        "unattributed": {
            "used": unattr_used,
            "pairs": [{"pair": k, "calls": v} for k, v in sorted(unknown_pairs.items(), key=lambda kv: -kv[1])],
        },
        "factory": _factory_with_speeds(root, state_dir, db_path, now_ms, window_min, pairs),
        "zcode_sessions": _zcode_sessions(db_path, now_ms, max(int(window_min), 15)),
        "notes": notes,
    }


def _display_model(primary, pairs):
    if primary:
        return primary.split("/", 1)[1]
    for p in sorted("%s/%s" % t for t in pairs):
        return p.split("/", 1)[1]
    return "—"


def _fmt(n):
    if n is None:
        return "—"
    n = float(n)
    if abs(n) >= 1e8:
        return "%.2f亿" % (n / 1e8)
    if abs(n) >= 1e4:
        return "%.1f万" % (n / 1e4)
    return str(int(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--live", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--wait-quota", action="store_true",
                    help="等官方额度首取返回再输出（面板请求不走这里，永不阻塞）")
    args = ap.parse_args()
    d = collect(window_min=max(1, args.window), live_min=max(1, args.live))
    if args.wait_quota:
        with _QUOTA_LOCK:
            th = _QUOTA.get("thread")
        if th is not None:
            th.join(QUOTA_TIMEOUT_S + 1)
            d = collect(window_min=max(1, args.window), live_min=max(1, args.live))
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    print("并发数 @ %s  （窗口 %d 分钟 / 进行中 %d 分钟，会话库 %s）" % (
        d["ts"], d["window_min"], d["live_min"], "OK" if d["db_ok"] else "不可读: " + d["db_error"]))
    print("  %-9s %-14s %6s %12s %8s  %s" % ("通道", "模型", "已用/上限", "月包/官方", "状态", "说明"))
    for x in d["lanes"]:
        used = "—" if x["used"] is None else str(x["used"])
        src = ("[设定]" if x.get("quota_basis") == "target"
               else {"api": "[官方]", "ledger": "[台账]", "none": ""}.get(x.get("quota_source"), ""))
        if x["month_quota"] is None:
            mt = "—"
        elif x.get("quota_unit") == "usd":
            mt = "%s/%s%s" % (_amt(x["month_tokens"], "usd"), _amt(x["month_quota"], "usd"), src)
        else:
            mt = "%s/%s%s" % (_fmt(x["month_tokens"]), _fmt(x["month_quota"]), src)
        used_txt = "%s/%s" % (used, "—" if x["cap"] is None else x["cap"])
        spd = x.get("speed") or {}
        spd_txt = "—" if spd.get("tps") is None else "%.1f tok/s" % spd["tps"]
        desc = ("%s · %s" % (spd_txt, x["reason"])) if x.get("reason") else spd_txt
        print("  %-9s %-14s %6s %12s %8s  %s" % (
            x["id"], x["model"][:14], used_txt,
            mt, x["status"], desc[:48]))
        for m in x.get("merged") or []:
            print("     ↳ 归并 %s：%d 会话 / %s tok · %s%s" % (
                m["alias"], m["sessions"], _fmt(m["tokens"]),
                "日志落点" if m["basis"] == "log" else "路由主选",
                (" · %d 次" % m["reqs"]) if m["reqs"] else ""))
    sp = d.get("speed") or {}
    st = sp.get("total") or {}
    gms = d.get("gateway_models") or []
    if gms:
        gt = d.get("gateway_totals") or {}
        print("网关模型（别名维度，近 %d 分钟）：" % d["window_min"])
        for g in gms:
            print("  %-12s 会话 %-4s 请求 %-4s 失败 %-4s %-10s %s" % (
                g["id"],
                "—" if g.get("sessions") is None else g["sessions"],
                g.get("reqs", 0), g.get("fails", 0),
                "—" if g.get("tps") is None else "%.1f tok/s" % g["tps"],
                g.get("status", "")))
        print("  汇总：%s 会话 / %s 次请求 / %s tok / %s tok/s" % (
            "—" if gt.get("sessions") is None else gt["sessions"],
            gt.get("reqs"), _fmt(gt.get("tok_out")),
            "—" if gt.get("tps") is None else "%.2f" % gt["tps"]))
    print("速度：总 %s tok/s（近 %d 分钟 %s 次请求 / %s tok / %s ms，来源 %s）" % (
        "—" if st.get("tps") is None else "%.2f" % st["tps"], d["window_min"],
        st.get("reqs"), _fmt(st.get("tok_out")), st.get("ms_sum"), sp.get("source")))
    if (sp.get("unmapped") or {}).get("reqs"):
        print("速度未映射 lane：%s" % ", ".join(
            "%s×%d" % (k, v["reqs"]) for k, v in sp["unmapped"]["lanes"].items()))
    for q in d.get("quota_extra") or []:
        if q.get("ok"):
            rv = ("$%.2f" % float(q["remaining"])) if (q.get("unit") == "USD"
                                                       and q.get("remaining") is not None) else "%s %s" % (
                q["remaining"], q["unit"])
            print("  兜底 %s：余额 %s · %s%s" % (
                q["id"], rv, q["plan"],
                (" · %s 到期" % q["expires_at"]) if q.get("expires_at") else ""))
        else:
            print("  兜底 %s：额度取数失败（%s）" % (q["id"], q.get("error") or "未知"))
    t = d["totals"]
    print("合计：cap %d（可用 %d）· 在跑 %s（进行中 %s）· 分道占用合计 %s · 工厂槽 %d/%d" % (
        t["cap"], t["usable_cap"],
        "—" if t["used"] is None else t["used"],
        "—" if t.get("live") is None else t["live"],
        "—" if t["lane_used_sum"] is None else t["lane_used_sum"],
        d["factory"]["active"], d["factory"]["max"]))
    if d["unattributed"]["pairs"]:
        print("未归属：" + ", ".join("%s×%d" % (p["pair"], p["calls"]) for p in d["unattributed"]["pairs"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
