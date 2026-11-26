# -*- coding: utf-8 -*-
"""steward-gateway —— 本地模型聚合网关（零第三方依赖，stdlib only）

职责：ZCode 只连 http://127.0.0.1:8788/v1 一个地址；网关按复杂度/关键词把请求
路由到最合适的供应商模型，额度耗尽或报错自动降级到下一个，并把每次"实际打到
哪个模型、耗时、token、失败原因"写进台账，供 AI 大管家做选型与汇报。

设计来源（GitHub 实战验证）：
  - musistudio/claude-code-router: 本地网关 + 别名/前缀/有序 fallback + 每次请求日志
  - BerriAI/litellm: model_name 别名映射多 deployment；complexity_router 四分层
  - 本项目增量：密钥不落网关配置，运行时从 ZCode provider_config.json 按 providerId 取；
    冷却状态持久化；/admin/ledger 给管家读的能力画像。

启动：python -X utf8 gateway.py --config ../config/routing.json
      python -X utf8 gateway.py --config ../config/routing.json --print-config  # 干跑校验
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import zlib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
from urllib.parse import urlparse

sys.dont_write_bytecode = True
VERSION = "0.3.0"  # V4.0-P1 计量修复 + V4.0-P4 安全收口（去 CORS、Host 白名单、admin 鉴权、恒时比对、拒绝公网监听）

# ---------------------------------------------------------------- 配置与状态


def now() -> float:
    return time.time()


def today() -> str:
    return datetime.now().strftime("%Y%m%d")


class ProviderError(Exception):
    """单个 lane 转发失败；retryable=False 表示该 lane 应立即长冷却。"""

    def __init__(self, msg: str, status: int = 0, body: str = "", retryable: bool = True):
        super().__init__(msg)
        self.status = status
        self.body = body
        self.retryable = retryable

    @property
    def cls(self) -> str:
        if self.status in (401, 403):
            return "auth"
        if self.status == 402:
            return "quota"
        if self.status == 429:
            return "rate"
        if self.status >= 500:
            return "upstream5xx"
        if self.status == 0:
            return "conn"
        return "http%d" % self.status


class Lane:
    """一个可调用的 provider+model 组合。"""

    def __init__(self, raw: dict, providers: dict):
        self.id = raw["id"]
        self.provider_id = raw["providerId"]
        self.model = raw["model"]
        self.tier = list(raw.get("tier") or [])
        self.ctx = int(raw.get("ctx") or 0)
        self.weight = float(raw.get("weight") or 1.0)
        self.tags = list(raw.get("tags") or [])
        self.equiv = raw.get("equiv") or self.model
        self.note = raw.get("note", "")
        prov = providers.get(self.provider_id)
        self.base_url = (prov or {}).get("baseUrl", "")
        self.api_key = (prov or {}).get("apiKey", "")
        self.api_type = (prov or {}).get("apiType", "")
        self.display = (prov or {}).get("name", self.provider_id)
        self.usable = bool(self.base_url and self.api_key and self.api_type == "openai-chat-completions")
        self.reason_unusable = "" if self.usable else (
            "provider 未找到" if not prov else
            ("非 openai-chat-completions 协议" if self.api_type != "openai-chat-completions" else "缺 baseUrl/apiKey"))


class State:
    """网关共享状态；所有读写走锁。"""

    def __init__(self, cfg: dict, base_dir: str):
        self.lock = threading.RLock()
        self.cfg = cfg
        self.base_dir = base_dir
        self.started = now()
        self.aliases = cfg.get("aliases", {})
        self.policy = cfg.get("policy", {})
        self.providers, self.prov_errors = load_providers(cfg)
        # 红线防呆（2026-11-18 用户令）：官方账号型通道（account:*，如 GLM 编程套餐）
        # 只能走 ZCode 官方渠道，挂进本聚合路由会有封号风险 —— 拒绝启动。
        _banned = [r.get("id") or r.get("providerId")
                   for r in cfg.get("lanes", [])
                   if str(r.get("providerId", "")).startswith("account:")]
        if _banned:
            raise SystemExit("FATAL: lanes 引用了官方账号通道 %s —— "
                             "官方 GLM 只能走 ZCode 官方渠道，进聚合路由会封号。"
                             "请从 routing.json lanes 移除。" % _banned)
        self.lanes = [Lane(r, self.providers) for r in cfg.get("lanes", [])]
        self.lane_by_id = {l.id: l for l in self.lanes}
        self.cool = {}          # lane_id -> {"until": ts, "reason": str}
        self.stats = {}         # lane_id -> counters
        self.recent = []        # 最近 100 条，供 /admin/status 看
        self.sticky = {}        # conv_key -> {"lane": id, "ts": ts}；会话粘性，防中途换模型
        self.total = {"requests": 0, "ok": 0, "fail": 0, "tokens_in": 0, "tokens_out": 0,
                      "tokens_cached": 0, "cost_cny": 0.0, "unpriced": 0,
                      # V4.0-P1：估算部分单列（已含在 tokens_in/out/cost_cny 内，便于拆出“账面=总-估算”）
                      "tokens_est_in": 0, "tokens_est_out": 0, "cost_est_cny": 0.0,
                      "estimated_requests": 0, "usage_schema": 2}
        self.no_stream_usage = set()   # 不接受 stream_options.include_usage 的 lane（运行时探测）
        self.log_dir = os.path.join(base_dir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.load_state()

    # ---- 冷却/计数持久化 ----
    def state_path(self) -> str:
        return os.path.join(self.log_dir, "gateway-state.json")

    def load_state(self) -> None:
        try:
            d = json.load(open(self.state_path(), encoding="utf-8"))
            self.cool = {k: v for k, v in d.get("cool", {}).items() if v.get("until", 0) > now()}
            self.stats = d.get("stats", {})
            self.sticky = {k: v for k, v in (d.get("sticky") or {}).items()
                           if now() - v.get("ts", 0) < 86400}
            self.total = {**self.total, **d.get("total", {})}
            self.total["usage_schema"] = 2
            self.no_stream_usage = set(d.get("no_stream_usage") or [])
        except (OSError, ValueError):
            pass

    def save_state(self) -> None:
        tmp = self.state_path() + ".tmp"
        with self.lock:
            payload = {"cool": self.cool, "stats": self.stats, "total": self.total,
                       "sticky": self.sticky, "no_stream_usage": sorted(self.no_stream_usage),
                       "savedAt": now()}
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path())
        except OSError:
            pass

    # ---- lane 可用性 ----
    def cooldown_left(self, lane_id: str) -> float:
        c = self.cool.get(lane_id)
        if not c:
            return 0.0
        left = c["until"] - now()
        if left <= 0:
            self.cool.pop(lane_id, None)
            return 0.0
        return left

    def mark_fail(self, lane: Lane, err: ProviderError, ms: int) -> None:
        p = self.policy.get("cooldown_sec", {})
        table = {"auth": p.get("auth", 3600), "quota": p.get("quota", 900),
                 "rate": p.get("rate", 300), "upstream5xx": p.get("upstream5xx", 120),
                 "conn": p.get("conn", 60)}
        sec = table.get(err.cls, p.get("other", 120))
        if err.status == 0 and isinstance(err.__cause__, socket.timeout):
            sec = p.get("timeout", 120)
        with self.lock:
            self.cool[lane.id] = {"until": now() + sec, "reason": "%s %s" % (err.cls, str(err)[:160])}
            st = self.stats.setdefault(lane.id, self.blank_lane_stat())
            st["fail"] += 1
            st["last_error"] = "%s %s" % (err.cls, str(err)[:160])
            st["last_error_at"] = now()
            st["fail_streak"] += 1
            self.total["fail"] += 1
        self.record({"event": "fail", "lane": lane.id, "class": err.cls, "status": err.status,
                     "ms": ms, "error": str(err)[:300]})
        self.save_state()

    def mark_no_stream_usage(self, lane_id: str, status: int, snippet: str) -> None:
        """记下该 lane 不接受 stream_options（V4.0-P1）；之后对它不再注入，usage 走估算并打标记。"""
        with self.lock:
            self.no_stream_usage.add(lane_id)
        self.record({"event": "capability", "lane": lane_id, "no_stream_usage": True,
                     "status": status, "error": (snippet or "")[:200]})
        self.save_state()

    def mark_ok(self, lane: Lane, ms: int, tin: int, tout: int, tcached: int,
                tier: str, req_model: str, stream: bool, usage_source: str = "reported") -> None:
        """记一次成功请求。成本按**上游真实模型**结算（回传别名会被 usage-bar 记为未定价）：
        未命中输入 + 输出 + 缓存命中分别计价，峰值时段取 peak 价。"""
        cost, priced = cost_of(lane.model, tin, tout, tcached)
        with self.lock:
            st = self.stats.setdefault(lane.id, self.blank_lane_stat())
            st["ok"] += 1
            st["fail_streak"] = 0
            st["lat_ms_sum"] += ms
            st["lat_ms_n"] += 1
            st["tokens_in"] += tin
            st["tokens_out"] += tout
            st["tokens_cached"] = st.get("tokens_cached", 0) + tcached
            st["cost_cny"] = round(st.get("cost_cny", 0.0) + cost, 6)
            self.total["ok"] += 1
            self.total["tokens_in"] += tin
            self.total["tokens_out"] += tout
            self.total["tokens_cached"] = self.total.get("tokens_cached", 0) + tcached
            self.total["cost_cny"] = round(self.total.get("cost_cny", 0.0) + cost, 6)
            if not priced:
                self.total["unpriced"] = self.total.get("unpriced", 0) + 1
            if usage_source == "estimated":
                st["estimated"] = st.get("estimated", 0) + 1
                st["tokens_est_in"] = st.get("tokens_est_in", 0) + tin
                st["tokens_est_out"] = st.get("tokens_est_out", 0) + tout
                st["cost_est_cny"] = round(st.get("cost_est_cny", 0.0) + cost, 6)
                self.total["estimated_requests"] = self.total.get("estimated_requests", 0) + 1
                self.total["tokens_est_in"] = self.total.get("tokens_est_in", 0) + tin
                self.total["tokens_est_out"] = self.total.get("tokens_est_out", 0) + tout
                self.total["cost_est_cny"] = round(self.total.get("cost_est_cny", 0.0) + cost, 6)
        self.record({"event": "ok", "lane": lane.id, "tier": tier, "req_model": req_model,
                     "ms": ms, "tokens_in": tin, "tokens_out": tout, "tokens_cached": tcached,
                     "cost_cny": round(cost, 6), "priced": priced, "stream": stream,
                     "usage_source": usage_source})

    def get_sticky(self, key: str):
        if not key:
            return None
        v = self.sticky.get(key)
        ttl = float(self.policy.get("sticky_ttl_sec", 1800))
        if v and now() - v.get("ts", 0) <= ttl:
            return v.get("lane")
        if v:
            self.sticky.pop(key, None)
        return None

    def set_sticky(self, key: str, lane_id: str) -> None:
        if not key:
            return
        with self.lock:
            self.sticky[key] = {"lane": lane_id, "ts": now()}
            if len(self.sticky) > 500:
                for k in sorted(self.sticky, key=lambda x: self.sticky[x]["ts"])[:100]:
                    self.sticky.pop(k, None)
        self.save_state()

    @staticmethod
    def blank_lane_stat() -> dict:
        return {"ok": 0, "fail": 0, "fail_streak": 0, "lat_ms_sum": 0, "lat_ms_n": 0,
                "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_cny": 0.0,
                "tokens_est_in": 0, "tokens_est_out": 0, "cost_est_cny": 0.0, "estimated": 0,
                "last_error": "", "last_error_at": 0}

    def record(self, row: dict) -> None:
        row = {"ts": round(now(), 3), **row}
        with self.lock:
            self.recent.append(row)
            if len(self.recent) > 100:
                del self.recent[: len(self.recent) - 100]
        try:
            with open(os.path.join(self.log_dir, "gateway-%s.jsonl" % today()), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass


def resolve_base_dir(cfg_path: str, cfg: dict) -> str:
    """状态/日志目录：显式 state_dir 优先；配置在 config/ 下则用插件根，否则用配置所在目录。

    早期版本固定取"配置目录的父目录"，导致临时配置把状态写进 %TEMP%/logs，
    跨进程共享冷却，测试出现假 PASS（2026-11-17 边界用例实测）。
    """
    if cfg.get("state_dir"):
        return os.path.abspath(cfg["state_dir"])
    d = os.path.dirname(os.path.abspath(cfg_path))
    if os.path.basename(d).lower() == "config":
        return os.path.dirname(d)
    return d


def load_providers(cfg: dict) -> tuple[dict, list]:
    """从 ZCode provider_config.json 按 providerId 提取 baseUrl/apiKey（密钥不进网关配置）。"""
    path = cfg.get("providers_config") or os.path.expanduser("~/.zcode/v2/provider_config.json")
    out, errs = {}, []
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {}, ["读不到 %s: %s" % (path, e)]
    rules = d.get("config", {}).get("providerConfigRules", {}).get("providerRules", [])
    for pr in rules:
        pid = pr.get("providerId")
        cf = pr.get("config") or {}
        api = cf.get("api") or {}
        acc = cf.get("access") or {}
        out[pid] = {"name": pr.get("providerName") or pid,
                    "baseUrl": api.get("baseUrl") or "",
                    "apiType": api.get("type") or "",
                    "apiKey": acc.get("apiKey") or "",
                    "models": cf.get("personalModelIds") or []}
    if not out:
        errs.append("provider_config 中无 providerRules")
    return out, errs


# ---------------------------------------------------------------- 复杂度分类

REASON_HINTS = ("分析", "评审", "设计", "权衡", "为什么", "推理", "证明", "架构",
                "diagnose", "reason", "prove", "trade-off", "root cause", "why ")
SIMPLE_HINTS = ("翻译", "改写", "润色", "格式", "总结成", "提取", "列出", "translate", "rewrite", "format")


def classify(payload: dict, policy: dict) -> tuple[str, dict]:
    """返回 (tier, 特征)。启发式，与 LiteLLM complexity_router 同思路。"""
    msgs = payload.get("messages") or []
    chars, has_img, n_tool_calls = 0, False, 0
    text_all = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
            text_all.append(c)
        elif isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("image_url", "input_image"):
                    has_img = True
                t = part.get("text") or ""
                chars += len(t)
                text_all.append(t)
        n_tool_calls += len(m.get("tool_calls") or [])
    tools = len(payload.get("tools") or [])
    est_tok = int(chars / policy.get("chars_per_token", 2.2))
    low = (" ".join(text_all[-3:])).lower()
    feat = {"est_tokens": est_tok, "msgs": len(msgs), "tools": tools,
            "tool_calls": n_tool_calls, "image": has_img}

    thr = policy.get("tiers", {})
    simple_max = int(thr.get("simple_max_chars", 3000))
    complex_chars = int(thr.get("complex_min_chars", 60000))

    if any(k in low for k in REASON_HINTS) and chars <= simple_max * 2:
        return "REASONING", feat
    if chars <= simple_max and tools == 0 and len(msgs) <= 3:
        return "SIMPLE", feat
    if chars >= complex_chars or tools > 8 or n_tool_calls > 6:
        return "COMPLEX", feat
    return "MEDIUM", feat


def conv_key(payload: dict) -> str:
    """同一对话稳定指纹：首条 system + 首条 user。会话增长不影响结果。"""
    sys_txt, user_txt = "", ""
    for m in payload.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
        if m.get("role") == "system" and not sys_txt:
            sys_txt = c or ""
        elif m.get("role") == "user" and not user_txt:
            user_txt = c or ""
        if sys_txt and user_txt:
            break
    return hashlib.sha1((sys_txt[:2000] + "\x00" + user_txt[:2000]).encode("utf-8", "replace")).hexdigest()[:16]


def alias_allows(alias_cfg: dict, lane_id: str) -> bool:
    """该别名是否有资格使用这条 lane（tier 别名看并集，order 别名看列表）。"""
    if "order" in alias_cfg:
        return lane_id in (alias_cfg.get("order") or [])
    union = set()
    for lst in (alias_cfg.get("tiers") or {}).values():
        union.update(lst or [])
    return lane_id in union


def order_for_sticky(cands: list, lane_id: str, st: State, allow_cross: bool,
                     alias_cfg: dict | None = None) -> list:
    """粘性优先：上次用的 lane 排最前（即使当前 tier 候选里没有它，只要别名允许）。

    这修的是"对话变长导致 tier 升级、原 lane 不在新候选里"的中途换模型问题。
    """
    lane = st.lane_by_id.get(lane_id or "")
    if not lane:
        return cands
    # 兜底 lane 不参与粘性提升：被降级用过一次后，下一轮仍按正常顺序选回主力，
    # 而不是被粘住 30 分钟（policy.sticky_exclude，2026-11-18 用户令：apigoto 只做末位兜底）。
    if lane.id in set((st.policy or {}).get("sticky_exclude") or []):
        return cands
    allowed = alias_allows(alias_cfg, lane.id) if alias_cfg else (lane.id in {c.id for c in cands})
    if not allowed:
        return cands
    usable = lane.usable and st.cooldown_left(lane.id) == 0
    same = [c for c in cands if c.equiv == lane.equiv and c.id != lane.id]
    other = [c for c in cands if c.equiv != lane.equiv]
    if usable:
        return [lane] + same + other
    if not same and not allow_cross:
        return []
    return same + other


def solve(alias_cfg: dict, tier: str, st: State, skip: set) -> list:
    """按别名配置给出有序候选 lane 列表（跳过冷却/不可用/已试）。"""
    if "order" in alias_cfg:
        ids = alias_cfg.get("order") or []
        if not ids:      # 显式空列表 = 无候选，不做兜底
            return []
    else:
        ids = (alias_cfg.get("tiers") or {}).get(tier) or []
        if not ids:      # tier 未配置才兜底取同 tier lane
            ids = [l.id for l in st.lanes if tier in l.tier]
    out = []
    for lid in ids:
        lane = st.lane_by_id.get(lid)
        if not lane or lid in skip or not lane.usable:
            continue
        if st.cooldown_left(lid) > 0:
            continue
        out.append(lane)
    return out


# ---------------------------------------------------------------- 转发

def upstream_url(base: str) -> str:
    b = base.rstrip("/")
    return b if b.endswith("/chat/completions") else b + "/chat/completions"


def open_upstream(lane: Lane, payload: dict, stream: bool, timeout: float):
    u = urlparse(upstream_url(lane.base_url))
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    port = u.port or (443 if u.scheme == "https" else 80)
    conn = conn_cls(u.hostname, port, timeout=timeout)
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + lane.api_key,
        "User-Agent": "steward-gateway/%s" % VERSION,
    }
    headers["Accept-Encoding"] = "identity"
    if stream:
        headers["Accept"] = "text/event-stream"
    path = u.path + (("?" + u.query) if u.query else "")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn.request("POST", path, body=body, headers=headers)
    return conn, conn.getresponse()


def content_encoding(resp) -> str:
    return (resp.getheader("Content-Encoding") or "").lower()


def decode_all(resp, raw: bytes) -> bytes:
    """上游无视 identity 硬 gzip 时的兜底解压。"""
    enc = content_encoding(resp)
    try:
        if "gzip" in enc:
            return zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        if "deflate" in enc:
            return zlib.decompress(raw)
    except zlib.error:
        pass
    return raw


def make_decompressor(resp):
    enc = content_encoding(resp)
    if "gzip" in enc:
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if "deflate" in enc:
        return zlib.decompressobj()
    return None


# ---------------------------------------------------------------- 成本核算
# 目录价只读用户已装的 usage-bar/prices.json（可用 STEWARD_PRICES 覆盖）。
# 口径与其一致：input_tokens 含缓存命中，未命中 = input - cached；
# 费用 = 未命中×in + 输出×out + 缓存命中×cache_read（元/百万 tokens）。
PRICES_PATH = os.environ.get("STEWARD_PRICES") or os.path.expanduser("~/.zcode/plugins/usage-bar/prices.json")
_PRICE_CACHE = {"mt": None, "data": {}}


def load_prices() -> dict:
    try:
        mt = os.path.getmtime(PRICES_PATH)
    except OSError:
        return {}
    if _PRICE_CACHE["mt"] != mt:
        try:
            _PRICE_CACHE["data"] = json.load(open(PRICES_PATH, encoding="utf-8"))
            _PRICE_CACHE["mt"] = mt
        except (OSError, ValueError):
            return {}
    return _PRICE_CACHE["data"]


def price_entry(model: str):
    """模型名 → 价格条目：精确 → aliases 表 → 互相包含（容忍中转站前缀）。"""
    m = str(model or "").strip().lower()
    if not m:
        return None
    pr = load_prices()
    models = pr.get("models") or {}
    if m in models:
        return models[m]
    al = pr.get("aliases") or {}
    if m in al and al[m] in models:
        return models[al[m]]
    for k, ent in models.items():
        if k in m or m in k:
            return ent
    return None


def is_peak() -> bool:
    """DeepSeek 峰谷口径（北京时间）：周末全谷；工作日 9-12 / 14-18 点峰。"""
    t = time.gmtime(now() + 8 * 3600)
    return t.tm_wday < 5 and (9 <= t.tm_hour <= 11 or 14 <= t.tm_hour <= 17)


def cost_of(model: str, tin: int, tout: int, tcached: int) -> tuple:
    ent = price_entry(model)
    if not ent:
        return 0.0, False
    rates = ent.get("peak" if is_peak() else "idle") if isinstance(ent.get("peak"), dict) else ent
    cached = max(0, min(tcached, tin))
    miss = max(0, tin - cached)
    c = (miss * rates.get("in", 0) + tout * rates.get("out", 0)
         + cached * rates.get("cache_read", 0)) / 1e6
    return c, True


def _cached_tokens(u: dict) -> int:
    """各家中转/官方字段名不一，全部兜住；拿不到就是 0（宁少算不多算）。"""
    for k in ("prompt_cache_hit_tokens", "cache_read_input_tokens"):
        if u.get(k) is not None:
            return int(u.get(k) or 0)
    for k in ("prompt_tokens_details", "input_tokens_details"):
        d = u.get(k)
        if isinstance(d, dict) and d.get("cached_tokens") is not None:
            return int(d.get("cached_tokens") or 0)
    return 0


def _usage_triplet(u: dict) -> tuple:
    """usage 字典 -> (输入, 输出, 缓存命中)。输入含缓存命中，与 usage-bar 口径一致。"""
    tin = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    tout = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    return tin, tout, _cached_tokens(u)


def _has_usage(u) -> bool:
    return isinstance(u, dict) and any(
        int(u.get(k) or 0) > 0 for k in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"))


def extract_usage(data: bytes) -> tuple:
    """非流式响应体 -> (输入, 输出, 缓存命中)；拿不到 usage 返回 (0, 0, 0)。"""
    try:
        d = json.loads(data.decode("utf-8", "replace"))
        u = d.get("usage") or {}
        return _usage_triplet(u)
    except (ValueError, AttributeError, TypeError):
        return 0, 0, 0


def _find_event_sep(buf: bytes) -> tuple:
    """返回 (事件结束位置, 分隔符长度)；同时兼容 \n\n 与 \r\n\r\n，取最早出现者。"""
    best = (-1, 0)
    for sep in (b"\r\n\r\n", b"\n\n"):
        i = buf.find(sep)
        if i >= 0 and (best[0] < 0 or i < best[0]):
            best = (i, len(sep))
    return best


class SSEUsageTracker:
    """V4.0-P1（修 D-01）：边透传边按 SSE 事件解析 usage。

    V3.2.1 只保留最后 8192 字节再整体 json.loads，SSE 原文必然解析失败，
    于是回退成 tout=8192//4=2048、tin=0——账本里 daily_tokens_in=0 即由此而来。
    现在逐个 data: 事件单独解析，取**最后一个**带 usage 的事件；
    同时累计 delta 文本字符数，供上游完全不给 usage 时做“估算”兜底（并打标记，不混入真实值）。
    """

    MAX_PENDING = 1 << 20  # 单个事件超过 1MB 视为异常流，丢弃积压防内存膨胀

    def __init__(self) -> None:
        self.buf = b""
        self.usage = None
        self.delta_chars = 0
        self.events = 0
        self.parse_errors = 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.buf += chunk
        while True:
            idx, sep_len = _find_event_sep(self.buf)
            if idx < 0:
                break
            ev, self.buf = self.buf[:idx], self.buf[idx + sep_len:]
            self._handle(ev)
        if len(self.buf) > self.MAX_PENDING:
            self.parse_errors += 1
            self.buf = b""

    def close(self) -> None:
        if self.buf.strip():
            self._handle(self.buf)
        self.buf = b""

    def _handle(self, ev: bytes) -> None:
        datas = []
        for ln in ev.split(b"\n"):
            ln = ln.rstrip(b"\r")
            if ln.startswith(b"data:"):
                datas.append(ln[5:].lstrip())
        if not datas:
            return
        data = b"\n".join(datas).strip()
        if not data or data == b"[DONE]":
            return
        self.events += 1
        try:
            d = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            self.parse_errors += 1
            return
        if not isinstance(d, dict):
            return
        u = d.get("usage")
        if _has_usage(u):
            self.usage = u
        for ch in d.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            dl = ch.get("delta") or {}
            if not isinstance(dl, dict):
                continue
            for k in ("content", "reasoning_content", "reasoning"):
                v = dl.get(k)
                if isinstance(v, str):
                    self.delta_chars += len(v)
            for tc in dl.get("tool_calls") or []:
                fn = (tc or {}).get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict):
                    self.delta_chars += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))

    def result(self, est_in: int, chars_per_token: float) -> tuple:
        """-> (输入, 输出, 缓存命中, usage_source)；usage_source ∈ {reported, estimated}。"""
        if self.usage is not None:
            tin, tout, tc = _usage_triplet(self.usage)
            return tin, tout, tc, "reported"
        cpt = chars_per_token if chars_per_token and chars_per_token > 0 else 2.2
        return max(0, int(est_in)), int(self.delta_chars / cpt), 0, "estimated"


class Handler(BaseHTTPRequestHandler):
    server_version = "steward-gateway/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默，统一走 jsonl
        pass

    # ---- 工具 ----
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8", "replace") or "{}")

    def _auth_ok(self) -> bool:
        """V4.0-P4：恒时比对；Authorization: Bearer <token>（兼容 x-api-key）。未配置 client_token 时只放行本机非浏览器请求。"""
        tok = (self.server.state.policy.get("client_token") or "").strip()
        if not tok:
            return self._local_non_browser()
        auth = (self.headers.get("Authorization") or "").strip()
        got = auth[7:].strip() if auth[:7].lower() == "bearer " else (self.headers.get("x-api-key") or "").strip()
        return bool(got) and hmac.compare_digest(got.encode("utf-8"), tok.encode("utf-8"))

    def _host_ok(self) -> bool:
        """防 DNS 重绑定：Host 只能是本机名（端口任意）。"""
        h = (self.headers.get("Host") or "").strip().lower()
        if h.startswith("["):
            h = h[1:h.find("]")] if "]" in h else h
        elif h.count(":") == 1:
            h = h.rsplit(":", 1)[0]
        extra = [str(x).lower() for x in (self.server.state.policy.get("allowed_hosts") or [])]
        return h in ("127.0.0.1", "localhost", "::1") or h in extra

    def _local_non_browser(self) -> bool:
        """本机直连且不是浏览器页面发起（浏览器跨源请求必带 Origin；本机脚本/内核/ZCode 主进程不带）。"""
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and not self.headers.get("Origin")

    def _guard(self, need_token: bool) -> bool:
        """统一闸：Host 白名单 → 令牌（或本机非浏览器）。拒绝时已写响应并记账，返回 False。"""
        p = urlparse(self.path).path
        if not self._host_ok():
            self.server.state.record({"event": "denied", "path": p, "reason": "host", "host": (self.headers.get("Host") or "")[:80]})
            self._json(421, {"error": {"message": "host not allowed"}})
            return False
        if self._auth_ok() or (not need_token and self._local_non_browser()):
            return True
        self.server.state.record({"event": "denied", "path": p, "reason": "auth",
                                  "origin": (self.headers.get("Origin") or "")[:80]})
        self._json(401 if need_token else 403, {"error": {"message": "unauthorized"}})
        return False

    # ---- GET ----
    def do_GET(self) -> None:
        p = urlparse(self.path).path
        if p not in ("/health", "/") and not self._guard(need_token=False):
            return
        if p in ("/health", "/"):
            self._json(200, {"ok": True, "service": "steward-gateway", "version": VERSION,
                             "uptime_sec": round(now() - self.server.state.started)})
        elif p == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": k, "object": "model", "owned_by": "steward"} for k in
                                                        list(self.server.state.aliases.keys())]})
        elif p == "/admin/status":
            self._json(200, snapshot(self.server.state, detailed=True))
        elif p == "/admin/ledger":
            self._json(200, ledger(self.server.state))
        else:
            self._json(404, {"error": {"message": "not found: " + p}})

    def do_POST(self) -> None:
        p = urlparse(self.path).path
        if p == "/admin/reload" and not self._guard(need_token=False):
            return
        if p == "/admin/reload":
            try:
                reload_config(self.server)
                self._json(200, {"ok": True, "lanes": len(self.server.state.lanes)})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(e)})
            return
        if p != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        if not self._host_ok():
            self._json(421, {"error": {"message": "host not allowed"}})
            return
        if not self._auth_ok():
            self.server.state.record({"event": "denied", "path": p, "reason": "auth", "origin": (self.headers.get("Origin") or "")[:80]})
            self._json(401, {"error": {"message": "bad gateway client token"}})
            return
        try:
            payload = self._body()
        except ValueError:
            self._json(400, {"error": {"message": "invalid json"}})
            return
        serve_chat(self, payload)


def serve_chat(h: Handler, payload: dict) -> None:
    st: State = h.server.state
    req_model = str(payload.get("model") or "")
    stream = bool(payload.get("stream"))
    policy = st.policy
    tier, feat = classify(payload, policy)
    forced = (h.headers.get("X-Steward-Tier") or "").strip().upper()
    if forced in ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING"):
        tier = forced

    # 别名 -> 候选；显式 providerId/model -> 单 lane
    skip, tried = set(), []
    ckey = conv_key(payload)
    sticky_key = (ckey + "|" + req_model) if ckey else ""
    sticky_lane = st.get_sticky(sticky_key)
    if req_model in st.aliases:
        cands = solve(st.aliases[req_model], tier, st, skip)
        if policy.get("sticky", True):
            cands = order_for_sticky(cands, sticky_lane, st,
                                     bool(policy.get("allow_cross_class_failover", True)),
                                     st.aliases[req_model])
        mode = "alias"
    elif req_model in st.lane_by_id:
        cands = [st.lane_by_id[req_model]] if st.lane_by_id[req_model].usable else []
        mode = "lane"
    elif "/" in req_model:
        pid, mid = req_model.split("/", 1)
        lane = next((l for l in st.lanes if l.provider_id == pid and l.model == mid), None)
        cands = [lane] if lane and lane.usable else []
        mode = "explicit"
    else:
        lane = next((l for l in st.lanes if l.model == req_model), None)
        cands = [lane] if lane and lane.usable else []
        mode = "model"

    if not cands:
        h._json(503, {"error": {"message": "no usable lane for model=%s tier=%s" % (req_model, tier),
                                 "lanes": [{"id": l.id, "usable": l.usable, "why": l.reason_unusable}
                                           for l in st.lanes]}})
        return

    max_attempts = int(policy.get("max_attempts", 4))
    timeout = float(policy.get("timeout_sec", 180))
    last_err = None
    with st.lock:
        st.total["requests"] += 1

    for lane in cands[:max_attempts]:
        tried.append(lane.id)
        if req_model in st.aliases and policy.get("sticky", True) and sticky_key:
            if sticky_lane and sticky_lane != lane.id:
                st.record({"event": "switch", "conv": ckey, "from": sticky_lane, "to": lane.id,
                           "tier": tier, "reason": "sticky_lane_unavailable_or_failed"})
            st.set_sticky(sticky_key, lane.id)
        out_payload = dict(payload)
        out_payload["model"] = lane.model
        # V4.0-P1：流式请求补 stream_options.include_usage，让上游在末块回报真实 usage
        injected = bool(stream and policy.get("inject_stream_usage", True)
                        and "stream_options" not in payload and lane.id not in st.no_stream_usage)
        if injected:
            out_payload["stream_options"] = {"include_usage": True}
        t0 = now()
        conn = None
        try:
            conn, resp = open_upstream(lane, out_payload, stream, timeout)
            if injected and resp.status in (400, 422):
                # 可能是上游不认 stream_options：去掉后同 lane 重发一次；重发成功才记能力位
                snippet0 = resp.read(1200).decode("utf-8", "replace")
                conn.close()
                out_payload.pop("stream_options", None)
                conn, resp = open_upstream(lane, out_payload, stream, timeout)
                if resp.status < 400:
                    st.mark_no_stream_usage(lane.id, 400, snippet0)
            if resp.status >= 400:
                snippet = resp.read(1200).decode("utf-8", "replace")
                raise ProviderError("upstream %s: %s" % (resp.status, snippet[:300]),
                                    status=resp.status, body=snippet,
                                    retryable=resp.status not in (400, 404, 422))
            ms = int((now() - t0) * 1000)
            if stream:
                relay_stream(h, st, lane, resp, t0, tier, req_model, conn,
                             est_in=int(feat.get("est_tokens") or 0))
            else:
                raw = decode_all(resp, resp.read())
                tin, tout, tcached = extract_usage(raw)
                usage_src = "reported"
                if tin == 0 and tout == 0:
                    # 非流式拿不到 usage：输入按请求字符估、输出按响应体字符估，并打估算标记
                    cpt = float(policy.get("chars_per_token", 2.2)) or 2.2
                    tin, tout, usage_src = int(feat.get("est_tokens") or 0), int(len(raw) / cpt), "estimated"
                out = raw  # 回传上游真实模型名：ZCode 的 usage-bar 按模型计价，改写别名会显示"未定价"
                h.send_response(200)
                h.send_header("Content-Type", "application/json; charset=utf-8")
                h.send_header("Content-Length", str(len(out)))
                h.send_header("X-Steward-Lane", lane.id)
                h.send_header("X-Steward-Tier", tier)
                h.end_headers()
                h.wfile.write(out)
                st.mark_ok(lane, int((now() - t0) * 1000), tin, tout, tcached, tier, req_model, False,
                           usage_source=usage_src)
            if conn:
                conn.close()
            return
        except ProviderError as e:
            last_err = e
            if conn:
                conn.close()
            st.mark_fail(lane, e, int((now() - t0) * 1000))
            if not e.retryable:
                break
        except (socket.timeout, TimeoutError) as e:
            last_err = ProviderError("timeout: %s" % e, status=0)
            if conn:
                conn.close()
            st.mark_fail(lane, last_err, int((now() - t0) * 1000))
        except (ConnectionError, OSError, ssl.SSLError) as e:
            last_err = ProviderError("conn: %s" % e, status=0)
            if conn:
                conn.close()
            st.mark_fail(lane, last_err, int((now() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            last_err = ProviderError("internal: %s" % e, status=0)
            st.mark_fail(lane, last_err, 0)
            st.record({"event": "traceback", "lane": lane.id, "tb": traceback.format_exc()[-800:]})

    msg = "all lanes failed after %d attempts: %s" % (len(tried), last_err)
    h._json(502, {"error": {"message": msg, "tier": tier, "tried": tried}})


def relay_stream(h: Handler, st: State, lane: Lane, resp, t0: float, tier: str,
                 req_model: str, conn, est_in: int = 0) -> None:
    """SSE 透传：读到首块才提交响应头，首块前的错误仍可降级。"""
    dec = make_decompressor(resp)

    def pull(n: int = 4096) -> bytes:
        chunk = resp.read(n)
        return dec.decompress(chunk) if dec else chunk

    first = pull()
    if not first:
        raise ProviderError("empty stream body", status=resp.status)
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "close")
    h.send_header("X-Steward-Lane", lane.id)
    h.end_headers()
    tracker = SSEUsageTracker()
    tracker.feed(first)
    h.wfile.write(first)
    h.wfile.flush()
    while True:
        chunk = pull()
        if not chunk:
            break
        tracker.feed(chunk)
        h.wfile.write(chunk)
        h.wfile.flush()
    tracker.close()
    tin, tout, tcached, src = tracker.result(est_in, float(st.policy.get("chars_per_token", 2.2)))
    st.mark_ok(lane, int((now() - t0) * 1000), tin, tout, tcached, tier, req_model, True,
               usage_source=src)


# ---------------------------------------------------------------- 管理接口


def snapshot(st: State, detailed: bool = False) -> dict:
    lanes = []
    for l in st.lanes:
        s = st.stats.get(l.id, {})
        lanes.append({
            "id": l.id, "provider": l.display, "providerId": l.provider_id, "model": l.model,
            "tier": l.tier, "tags": l.tags, "usable": l.usable,
            "why_unusable": l.reason_unusable,
            "cooldown_left_sec": round(st.cooldown_left(l.id)),
            "ok": s.get("ok", 0), "fail": s.get("fail", 0),
            "avg_ms": round(s["lat_ms_sum"] / s["lat_ms_n"]) if s.get("lat_ms_n") else None,
            "tokens": [s.get("tokens_in", 0), s.get("tokens_out", 0)],
            "tokens_cached": s.get("tokens_cached", 0),
            "cost_cny": s.get("cost_cny", 0.0),
            "tokens_estimated": [s.get("tokens_est_in", 0), s.get("tokens_est_out", 0)],
            "cost_est_cny": s.get("cost_est_cny", 0.0),
            "estimated": s.get("estimated", 0),
            "stream_usage_supported": l.id not in st.no_stream_usage,
            "last_error": s.get("last_error", ""),
        })
    out = {"service": "steward-gateway", "version": VERSION,
           "uptime_sec": round(now() - st.started), "listen_port": st.cfg.get("listen", {}).get("port"),
           "aliases": list(st.aliases.keys()), "totals": st.total, "lanes": lanes,
           "provider_config_errors": st.prov_errors}
    if detailed:
        out["recent"] = st.recent[-20:]
    return out


def ledger(st: State) -> dict:
    """管家用的能力画像：按 lane 聚合成功率/延迟/额度信号。"""
    rows = []
    for l in st.lanes:
        s = st.stats.get(l.id, {})
        ok, fail = s.get("ok", 0), s.get("fail", 0)
        total = ok + fail
        rows.append({"lane": l.id, "provider": l.display, "model": l.model,
                     "tier": l.tier, "tags": l.tags, "ctx": l.ctx,
                     "ok": ok, "fail": fail, "success_rate": round(ok / total, 3) if total else None,
                     "avg_ms": round(s["lat_ms_sum"] / s["lat_ms_n"]) if s.get("lat_ms_n") else None,
                     "cost_cny": s.get("cost_cny", 0.0),
                     "cooldown_left_sec": round(st.cooldown_left(l.id)),
                     "note": l.note})
    return {"generated_at": datetime.now().isoformat(timespec="seconds"), "rows": rows}


def reload_config(srv) -> None:
    cfg = json.load(open(srv.cfg_path, encoding="utf-8"))
    old = srv.state
    new = State(cfg, srv.base_dir)
    # 继承累积统计与冷却，避免热重载丢账
    new.stats.update(old.stats)
    new.cool.update({k: v for k, v in old.cool.items() if v.get("until", 0) > now()})
    new.total.update(old.total)
    new.recent = old.recent[-100:]
    srv.state = new
    new.record({"event": "reload", "lanes": len(new.lanes), "aliases": list(new.aliases)})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config", "routing.json"))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--print-config", action="store_true")
    args = ap.parse_args()

    cfg_path = os.path.abspath(args.config)
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    listen = cfg.get("listen", {})
    host = args.host or listen.get("host", "127.0.0.1")
    port = args.port or int(listen.get("port", 8788))
    if host not in ("127.0.0.1", "localhost", "::1") and not listen.get("allow_public_bind"):
        print("FATAL: 网关拒绝监听 %s（只允许本机；确需对外请在 routing.json listen.allow_public_bind=true）" % host, file=sys.stderr)
        return 2
    base_dir = resolve_base_dir(cfg_path, cfg)

    st = State(cfg, base_dir)
    if args.print_config:
        print(json.dumps(snapshot(st, detailed=False), ensure_ascii=False, indent=2))
        bad = [l for l in st.lanes if not l.usable]
        print("\n不可用 lane: %d" % len(bad), file=sys.stderr)
        return 0 if st.lanes else 2

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.state = st
    srv.cfg_path = cfg_path
    srv.base_dir = base_dir

    print("[steward-gateway] %s listening on http://%s:%d/v1  lanes=%d aliases=%s"
          % (VERSION, host, port, len(st.lanes), ",".join(st.aliases)), flush=True)
    for l in st.lanes:
        print("  - %-18s %-12s %-28s tiers=%s %s"
              % (l.id, l.display, l.model, ",".join(l.tier), "" if l.usable else "[SKIP] " + l.reason_unusable),
              flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[steward-gateway] bye", flush=True)
    finally:
        srv.server_close()
        st.save_state()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
