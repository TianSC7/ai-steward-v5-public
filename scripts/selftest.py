# -*- coding: utf-8 -*-
"""steward-gateway 自检：健康 / 通道 / 流式 / 故障转移

用法：
  python -X utf8 selftest.py                 # 只做零成本检查（health/models/别名解析/故障注入）
  python -X utf8 selftest.py --live          # 额外对每条 lane 发一次真实请求（消耗少量 token）
  python -X utf8 selftest.py --config ../config/routing.json --json

故障注入原理：复制一份临时 provider 配置，塞入一条指向不可达端口的假通道，
起一个临时网关（默认 :8789），请求别名 auto-fault，断言：首条失败→自动落到真通道→返回 200，
且假通道进入冷却。临时文件含密钥，测试结束必定删除（finally）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True

from fingerprint import announce, arm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def gw_version() -> str:
    """从 gateway.py 源码读 VERSION，用于识别"网关进程还是旧版本"。"""
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"',
                      open(os.path.join(HERE, "gateway.py"), encoding="utf-8").read(4096), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def http_json(url: str, payload: dict | None = None, timeout: float = 130) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, body[:400]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body[:400]


def wait_health(port: int, sec: float = 15) -> bool:
    end = time.time() + sec
    while time.time() < end:
        try:
            code, _ = http_json("http://127.0.0.1:%d/health" % port, timeout=2)
            if code == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.4)
    return False


def test_failover(cfg: dict, base_dir: str, out: list) -> None:
    """临时配置 + 临时网关，验证 死通道→真通道 的降级。"""
    port = int(cfg.get("listen", {}).get("port", 8788)) + 1
    good = next((l for l in cfg.get("lanes", []) if l.get("providerId") and l.get("id") == "r4-dsv41"),
                (cfg.get("lanes") or [None])[0])
    if not good:
        out.append(("FAIL", "fault", "配置里没有可用的真实 lane，跳过故障注入"))
        return
    tmpdir = tempfile.mkdtemp(prefix="steward-fault-")
    try:
        prov_src = cfg.get("providers_config") or os.path.expanduser("~/.zcode/v2/provider_config.json")
        prov = json.load(open(prov_src, encoding="utf-8"))
        rules = prov["config"]["providerConfigRules"]["providerRules"]
        rules.append({
            "providerId": "zz-fault", "providerName": "fault-inject",
            "config": {"api": {"type": "openai-chat-completions", "baseUrl": "http://127.0.0.1:9/v1"},
                       "access": {"type": "api-key", "apiKey": "fault-key"}},
        })
        tmp_prov = os.path.join(tmpdir, "providers.json")
        json.dump(prov, open(tmp_prov, "w", encoding="utf-8"), ensure_ascii=False)

        tmp_cfg = {
            "listen": {"host": "127.0.0.1", "port": port},
            "providers_config": tmp_prov.replace("\\", "/"),
            "policy": cfg.get("policy", {}),
            "aliases": {"auto-fault": {"order": ["zz-fault", good["id"]]}},
            "lanes": [
                {"id": "zz-fault", "providerId": "zz-fault", "model": "fault-model", "tier": ["SIMPLE"]},
                {"id": good["id"], "providerId": good["providerId"], "model": good["model"], "tier": good.get("tier", [])},
            ],
        }
        tmp_cfg_path = os.path.join(tmpdir, "routing.json")
        json.dump(tmp_cfg, open(tmp_cfg_path, "w", encoding="utf-8"), ensure_ascii=False)

        proc = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(base_dir, "scripts", "gateway.py"),
                                 "--config", tmp_cfg_path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not wait_health(port):
                out.append(("FAIL", "fault", "临时网关未起来（端口 %d）" % port))
                return
            code, body = http_json("http://127.0.0.1:%d/v1/chat/completions" % port,
                                    {"model": "auto-fault", "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                                     "max_tokens": 512, "temperature": 0}, timeout=150)
            if code != 200:
                out.append(("FAIL", "fault", "降级后仍失败 HTTP %s %s" % (code, str(body)[:200])))
                return
            code2, status = http_json("http://127.0.0.1:%d/admin/status" % port, timeout=10)
            fault = next((l for l in status.get("lanes", []) if l["id"] == "zz-fault"), {})
            ok = fault.get("fail", 0) >= 1 and fault.get("cooldown_left_sec", 0) > 0
            out.append(("PASS" if ok else "WARN", "fault",
                        "死通道已计失败并冷却 %ss，请求由 %s 接管" % (fault.get("cooldown_left_sec"), good["id"])))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config", "routing.json"))
    ap.add_argument("--live", action="store_true", help="对每条 lane 发真实请求")
    ap.add_argument("--skip-fault", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    fp = announce("selftest")
    if not fp["ok"]:
        print("[FAIL] 指纹/环境自检未通过: " + "; ".join(fp["problems"]))
        return 2
    arm(600 if args.live else 300, "selftest")

    cfg_path = os.path.abspath(args.config)
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    base_dir = os.path.dirname(os.path.dirname(cfg_path))
    port = int(cfg.get("listen", {}).get("port", 8788))
    base = "http://127.0.0.1:%d" % port
    out: list[tuple[str, str, str]] = []

    code, h = http_json(base + "/health", timeout=3)
    out.append(("PASS" if code == 200 else "FAIL", "health", str(h)[:160]))
    if code != 200:
        emit(out, args.json)
        return 2

    want = gw_version()
    got = h.get("version") if isinstance(h, dict) else None
    if not want:
        out.append(("WARN", "version", "读不到 gateway.py 的 VERSION，跳过进程版本比对"))
    else:
        out.append(("PASS" if got == want else "FAIL", "version",
                    "进程=%s 代码=%s%s" % (got, want, "" if got == want else "（进程跑的是旧代码，请重启网关）")))

    code, models = http_json(base + "/v1/models", timeout=5)
    ids = [m.get("id") for m in (models.get("data") or [])] if isinstance(models, dict) else []
    out.append(("PASS" if "auto" in ids else "FAIL", "models", "别名: " + ",".join(ids)))

    code, st = http_json(base + "/admin/status", timeout=10)
    lanes = st.get("lanes", []) if isinstance(st, dict) else []
    bad = [l for l in lanes if not l.get("usable")]
    out.append(("PASS" if lanes and not bad else ("FAIL" if not lanes else "WARN"), "lanes",
                "%d 条可用 / %d 条（不可用: %s）%s" % (len(lanes) - len(bad), len(lanes),
                                                     ",".join(l["id"] for l in bad) or "无",
                                                     "" if lanes else " 空 lane 列表不得当通过")))

    if args.live:
        for l in lanes:
            if not l.get("usable"):
                continue
            t0 = time.time()
            code, body = http_json(base + "/v1/chat/completions",
                                    {"model": l["id"], "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                                     "max_tokens": 512, "temperature": 0})
            ms = int((time.time() - t0) * 1000)
            if code == 200 and isinstance(body, dict):
                txt = ((body.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
                out.append(("PASS", "live:" + l["id"], "%dms text=%r" % (ms, txt[:30])))
            else:
                out.append(("FAIL", "live:" + l["id"], "HTTP %s %s" % (code, str(body)[:160])))

    if not args.skip_fault:
        test_failover(cfg, base_dir, out)

    emit(out, args.json)
    return 0 if all(s != "FAIL" for s, _, _ in out) else 2


def emit(out: list, as_json: bool) -> None:
    if as_json:
        print(json.dumps([{"status": s, "check": c, "detail": d} for s, c, d in out], ensure_ascii=False, indent=2))
    else:
        for s, c, d in out:
            print("[%s] %-22s %s" % (s, c, d))


if __name__ == "__main__":
    raise SystemExit(main())
