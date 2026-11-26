# -*- coding: utf-8 -*-
"""网关健康快照探针（W-20261118-028）。

只读巡检 http://127.0.0.1:8788 的 /health 与 /admin/status（短超时），
输出结构化快照；异常时不抛栈，给出 lane 冷却/降级清单。
纯 HTTP GET 客户端：不改网关、不碰路由、不重启任何进程。

快照四字段（验收口径）：
  health_ok  —— /health 通且 ok=true
  status_ok  —— /admin/status 通且是合法 JSON 对象
  cooldowns  —— 冷却中的 lane 清单 [{"lane","left_sec","last_error"}]
  degraded   —— 降级清单：不可用 lane，或"全部可用 lane 均在冷却"

退出码：
  0 = 健康（两端点可用，无冷却/降级 lane）
  2 = 降级（端点可达，但有 lane 冷却中或不可用）
  3 = 不可用（连不上 / 超时 / 非 200 / 响应不是合法 JSON）

用法：
  python -X utf8 gateway_probe.py [--host 127.0.0.1] [--port 8788]
                                  [--timeout 3.0] [--json] [--write [PATH]]
  --write 不带参数落 <插件根>/state/gateway_health.json；带参数落指定路径。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime

sys.dont_write_bytecode = True

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
DEFAULT_TIMEOUT = 3.0
DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "gateway_health.json")

# 本机回环探测不走系统代理（HTTP_PROXY 会把 127.0.0.1 也绕出去）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

LEVEL_EXIT = {"healthy": 0, "degraded": 2, "unreachable": 3, "broken": 3}


def _fetch(url: str, timeout: float) -> dict:
    """GET 一个 URL。返回 {"http","body","error"}；连接级失败只写 error，不抛。"""
    req = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "steward-gateway-probe/1.0"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return {"http": resp.status, "body": resp.read(), "error": None}
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except OSError:
            body = b""
        return {"http": e.code, "body": body, "error": None}
    except OSError as e:  # 拒连/超时/DNS 全在这里收口，绝不往上传
        reason = getattr(e, "reason", None)
        text = "%s: %s" % (type(e).__name__, reason if reason is not None else e)
        if isinstance(e, (socket.timeout, TimeoutError)):
            text = "timeout: %s" % (reason if reason is not None else e)
        return {"http": None, "body": b"", "error": text}


def _endpoint(url: str, timeout: float, name: str) -> dict:
    """取一个管理端点。ok=True 时 data 是 JSON 对象；否则 errors 说明原因。"""
    out = {"ok": False, "data": None, "errors": [], "conn_error": False}
    r = _fetch(url, timeout)
    if r["error"]:
        out["errors"].append("%s 不可达: %s" % (name, r["error"]))
        out["conn_error"] = True
        return out
    if r["http"] != 200:
        out["errors"].append("%s 返回 HTTP %s" % (name, r["http"]))
        return out
    try:
        data = json.loads(r["body"].decode("utf-8", "replace") or "")
    except ValueError as e:
        out["errors"].append("%s 响应不是合法 JSON: %s" % (name, e))
        return out
    if not isinstance(data, dict):
        out["errors"].append("%s JSON 顶层不是对象" % name)
        return out
    out["ok"], out["data"] = True, data
    return out


def _lane_rows(status: dict | None) -> list:
    return list((status or {}).get("lanes") or [])


def probe(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          timeout: float = DEFAULT_TIMEOUT) -> dict:
    """巡检网关，返回结构化快照（永不抛栈）。"""
    base = "http://%s:%d" % (host, port)
    health = _endpoint(base + "/health", timeout, "/health")
    status = _endpoint(base + "/admin/status", timeout, "/admin/status")
    errors = health["errors"] + status["errors"]

    health_ok = bool(health["ok"] and health["data"].get("ok") is True)
    if health["ok"] and not health_ok:
        errors.append("/health ok=%r（网关自报不健康）" % health["data"].get("ok"))
    status_ok = bool(status["ok"])

    rows = _lane_rows(status["data"] if status_ok else None)
    cooldowns, degraded = [], []
    for l in rows:
        left = l.get("cooldown_left_sec") or 0
        if left > 0:
            cooldowns.append({"lane": l.get("id"), "left_sec": left,
                              "last_error": l.get("last_error") or ""})
        if not l.get("usable", True):
            degraded.append({"lane": l.get("id"),
                             "reason": l.get("why_unusable") or "不可用"})
    usable = [l for l in rows if l.get("usable", True)]
    if usable and all((l.get("cooldown_left_sec") or 0) > 0 for l in usable):
        degraded.append({"lane": "*", "reason": "全部 %d 条可用 lane 均在冷却" % len(usable)})

    if not health_ok or not status_ok:
        level = "unreachable" if (health["conn_error"] or status["conn_error"]) else "broken"
    elif cooldowns or degraded:
        level = "degraded"
    else:
        level = "healthy"

    alerts = ["lane %s 冷却中，剩余 %ss" % (c["lane"], c["left_sec"]) for c in cooldowns]
    alerts += ["lane %s 降级: %s" % (d["lane"], d["reason"]) for d in degraded]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "endpoint": {"host": host, "port": port, "timeout_sec": timeout},
        "health_ok": health_ok,
        "status_ok": status_ok,
        "unreachable": bool(health["conn_error"] or status["conn_error"]),
        "service": (health["data"] or {}).get("service") if health_ok else None,
        "version": (health["data"] or {}).get("version") if health_ok else None,
        "uptime_sec": (health["data"] or {}).get("uptime_sec") if health_ok else None,
        "lanes_total": len(rows),
        "lanes_usable": len(usable),
        "totals": (status["data"] or {}).get("totals") if status_ok else None,
        "cooldowns": cooldowns,
        "degraded": degraded,
        "alerts": alerts,
        "errors": errors,
        "level": level,
    }


def verdict(snap: dict) -> int:
    """快照 → 退出码：0 健康 / 2 降级 / 3 不可用。"""
    return LEVEL_EXIT.get(snap.get("level"), 3)


def render(snap: dict) -> str:
    """人读摘要（异常时附错误与冷却/降级清单）。"""
    ep = snap["endpoint"]
    lines = ["[gateway-probe] http://%s:%s  level=%s  health=%s  status=%s"
             % (ep["host"], ep["port"], snap["level"],
                "OK" if snap["health_ok"] else "FAIL",
                "OK" if snap["status_ok"] else "FAIL")]
    if snap.get("service"):
        lines.append("  service=%s version=%s uptime=%ss"
                     % (snap["service"], snap["version"], snap["uptime_sec"]))
    lines.append("  lanes: %d total / %d usable" % (snap["lanes_total"], snap["lanes_usable"]))
    if snap.get("totals"):
        t = snap["totals"]
        lines.append("  totals: requests=%s ok=%s fail=%s"
                     % (t.get("requests"), t.get("ok"), t.get("fail")))
    lines.append("  冷却: " + ("；".join("lane %s 剩 %ss" % (c["lane"], c["left_sec"])
                                          for c in snap["cooldowns"]) or "(无)"))
    lines.append("  降级: " + ("；".join("%s: %s" % (d["lane"], d["reason"])
                                          for d in snap["degraded"]) or "(无)"))
    for e in snap["errors"]:
        lines.append("  ! %s" % e)
    return "\n".join(lines)


def write_snapshot(snap: dict, path: str | None = None) -> str:
    """原子落盘（tmp + replace），返回写入路径。"""
    path = path or DEFAULT_STATE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="网关健康快照巡检（只读，不改网关）")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="单端点超时秒数（默认 3.0）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 快照")
    ap.add_argument("--write", nargs="?", const=DEFAULT_STATE_PATH, default=None,
                    metavar="PATH", help="落盘快照；不带参数写 state/gateway_health.json")
    args = ap.parse_args(argv)

    snap = probe(args.host, args.port, args.timeout)
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        print(render(snap))
    if args.write:
        print("已写入 %s" % write_snapshot(snap, args.write), file=sys.stderr)
    return verdict(snap)


if __name__ == "__main__":
    raise SystemExit(main())
