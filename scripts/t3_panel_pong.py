#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""t3_panel_pong.py - drive the CadPerception AI panel over WS (same path as a human submit).

Flow: runCommand "_.AI" -> aiSubmit text -> poll aiTranscript -> panelShot PNG.
Writes one JSONL evidence file with every requestId / timestamp / reply.
Exit 0 only when an assistant message containing PONG appears AND the panel PNG exists.
"""
import argparse
import asyncio
import hashlib
import json
import os
import time
import uuid

import websockets

EV = []


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(kind, **kw):
    rec = {"ts": now(), "kind": kind}
    rec.update(kw)
    EV.append(rec)
    print("[t3] %s %s" % (kind, json.dumps(kw, ensure_ascii=False)[:300]))


async def call(url, payload, want, timeout=30.0):
    """Send one WS message, wait for an event with matching requestId in `want`."""
    rid = payload["requestId"]
    async with websockets.connect(url, open_timeout=10, close_timeout=3) as ws:
        await ws.send(json.dumps(payload, ensure_ascii=False))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if ev.get("requestId") != rid:
                continue
            name = ev.get("event") or ev.get("type") or ""
            if name in want:
                return ev
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8765/cad/")
    ap.add_argument("--prompt", default="Reply with exactly: PONG")
    ap.add_argument("--png", required=True, help="absolute path for panel PNG")
    ap.add_argument("--out", required=True, help="evidence JSONL path")
    ap.add_argument("--poll", type=int, default=75)
    args = ap.parse_args()

    tag = uuid.uuid4().hex[:8]

    # 1) baseline transcript; if the panel is not created yet, open it via "_.AI"
    tr0 = await call(args.url, {"action": "aiTranscript", "requestId": "t3-tr0-" + tag},
                     ("aiActionResult",), 20)
    if tr0 and "面板未创建" in json.dumps((tr0.get("data") or {}), ensure_ascii=False):
        r1 = await call(args.url, {"action": "runCommand", "command": "_.AI",
                                   "requestId": "t3-open-" + tag}, ("commandResult",), 20)
        log("openPanel", requestId="t3-open-" + tag, reply=r1)
        await asyncio.sleep(5.0)
        tr0 = await call(args.url, {"action": "aiTranscript", "requestId": "t3-tr0b-" + tag},
                         ("aiActionResult",), 20)

    # 2) baseline (so we can tell the new bubble apart)
    base_n = -1
    tr0d = (tr0 or {}).get("data") or {}
    if tr0d.get("transcript"):
        try:
            base_n = len(json.loads(tr0d["transcript"]).get("messages", []))
        except Exception:
            pass
    log("baseline", requestId="t3-tr0-" + tag, messages=base_n,
        visible=tr0d.get("visible"), reply=tr0d.get("message"))

    # 3) submit the prompt through the panel's own SubmitPrompt path
    rid_submit = "t3-submit-" + tag
    r2 = await call(args.url, {"action": "aiSubmit", "text": args.prompt,
                               "requestId": rid_submit}, ("aiActionResult",), 30)
    log("submit", requestId=rid_submit, prompt=args.prompt, reply=r2)
    r2d = (r2 or {}).get("data") or {}
    if not r2 or not r2.get("ok") or r2d.get("count", 0) < 1:
        log("abort", stage="submit")
        return 3

    # 4) poll the transcript for the assistant reply
    hit = None
    last_tr = None
    for i in range(args.poll):
        await asyncio.sleep(2.0)
        r = await call(args.url, {"action": "aiTranscript",
                                  "requestId": "t3-tr%d-%s" % (i + 1, tag)},
                       ("aiActionResult",), 20)
        rd = (r or {}).get("data") or {}
        if not rd.get("transcript"):
            continue
        try:
            tr = json.loads(rd["transcript"])
        except Exception:
            continue
        last_tr = tr
        msgs = tr.get("messages", [])
        new_msgs = msgs[base_n:] if base_n >= 0 else msgs
        for m in new_msgs:
            c = (m.get("content") or "")
            if m.get("role") == "assistant" and "PONG" in c.upper():
                hit = {"index": msgs.index(m), "content": c, "model": tr.get("model")}
                break
        if hit:
            log("pong", requestId="t3-tr%d-%s" % (i + 1, tag), **hit)
            break
        if i % 5 == 0:
            log("polling", round=i + 1, messages=len(msgs), model=tr.get("model"))

    # 5) panel PNG (offscreen render; works without foreground focus)
    r3 = await call(args.url, {"action": "panelShot", "file": args.png,
                               "requestId": "t3-shot-" + tag}, ("aiActionResult",), 30)
    log("panelShot", requestId="t3-shot-" + tag, file=args.png, reply=r3)

    png_ok = os.path.isfile(args.png) and os.path.getsize(args.png) > 1000
    png_meta = {}
    if png_ok:
        h = hashlib.sha256(open(args.png, "rb").read()).hexdigest()[:16]
        png_meta = {"size": os.path.getsize(args.png), "sha256_16": h}
    log("summary", pass_=(hit is not None and png_ok),
        submit_requestId=rid_submit, transcript_model=(last_tr or {}).get("model"),
        png=png_meta, messages=(len(last_tr.get("messages", [])) if last_tr else None))

    with open(args.out, "w", encoding="utf-8") as f:
        for rec in EV:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 0 if (hit is not None and png_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
