#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pong_probe.py - submit one prompt to the CadPerception WebSocket bridge and capture the aiResult.

Usage:
  python -X utf8 scripts/pong_probe.py --prompt "Reply with exactly: PONG" \
      --out state/packages/PKG-NEW-20261119/EVIDENCE_T3_PONG_WS.jsonl

Exit codes: 0 = aiResult ok with non-empty content; 1 = no/invalid result; 2 = connect/send error.
"""
import argparse
import asyncio
import json
import time
import uuid

import websockets


async def run(args):
    rid = "pong-" + uuid.uuid4().hex[:12]
    events = []
    t0 = time.time()
    payload = {
        "action": args.action,
        "requestId": rid,
        "prompt": args.prompt,
        "history": [],
    }
    if args.preset:
        payload["presetId"] = args.preset
    if args.model:
        payload["model"] = args.model

    out = open(args.out, "w", encoding="utf-8")
    out.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "dir": "send", "payload": payload},
                         ensure_ascii=False) + "\n")
    out.flush()

    try:
        async with websockets.connect(args.url, open_timeout=10, close_timeout=3) as ws:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            print("[pong] connected, requestId=%s" % rid)
            deadline = time.time() + args.timeout
            result = None
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                try:
                    ev = json.loads(raw)
                except Exception:
                    ev = {"event": "raw", "raw": raw}
                events.append(ev)
                out.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "dir": "recv", "event": ev},
                                     ensure_ascii=False) + "\n")
                out.flush()
                name = ev.get("event") or ev.get("type") or "?"
                if name == "aiStreamDelta":
                    print("[pong] stream delta len=%d" % len(ev.get("delta") or ""))
                else:
                    print("[pong] event=%s rid=%s" % (name, ev.get("requestId")))
                if name in ("aiResult", "aiActionResult") and ev.get("requestId") == rid:
                    result = ev
                    break
            elapsed = time.time() - t0
            out.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "dir": "summary",
                                  "requestId": rid, "elapsed_sec": round(elapsed, 2),
                                  "ok": bool(result and result.get("ok")),
                                  "content": (result or {}).get("content", ""),
                                  "usage": (result or {}).get("usage")},
                                 ensure_ascii=False) + "\n")
            out.close()
            content = (result or {}).get("content") or (result or {}).get("message") or ""
            if result and result.get("ok") and content.strip():
                print("[pong] PASS requestId=%s elapsed=%.1fs content=%r"
                      % (rid, elapsed, content[:200]))
                return 0
            print("[pong] FAIL requestId=%s elapsed=%.1fs result=%s"
                  % (rid, elapsed, json.dumps(result, ensure_ascii=False)[:300] if result else "no aiResult"))
            return 1
    except Exception as e:
        out.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "dir": "error", "requestId": rid,
                              "error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False) + "\n")
        out.close()
        print("[pong] ERROR requestId=%s %s: %s" % (rid, type(e).__name__, e))
        return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8765/cad/")
    ap.add_argument("--action", default="userLoop")
    ap.add_argument("--prompt", default="Reply with exactly: PONG")
    ap.add_argument("--preset", default="apigoto-deepseek-flash")
    ap.add_argument("--model", default="")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--out", default="state/packages/PKG-NEW-20261119/EVIDENCE_T3_PONG_WS.jsonl")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
