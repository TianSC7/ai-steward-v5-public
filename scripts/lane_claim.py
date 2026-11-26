#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lane_claim.py — 并发账本 CLI（C-20261120-003 · B/C 块薄壳）

七个业务子命令（`reserve`/`bind` 是 `claim --phase` 的两阶段，不单列）：
    claim    占位：--phase reserve（容量闸，CreateSession 之前）/ --phase bind（补真实键）
    heartbeat 续租（claimed → leased）
    release  释放（→ released），返回同 lane 的 next_up 建议
    list     活跃占位清单
    stats    每 lane used/cap/free + 队列深度 + 今日回收/degraded
    reap     手工回收（每个入口的惰性 reap 同源）
    check    三段对账 ORPHAN / STALE / DRIFT（+ CAPS_DRIFT 告警）

退出码契约（质检按此判）
    0 = 成功（含幂等重放）
    2 = 用法/参数错
    3 = 业务拒绝：LANE_FULL / SESSION_BUSY / LEASE_EXPIRED / NOT_OWNER / BAD_STATE / ORPHAN（原因必可读）
    4 = 账本不可用 LEDGER_UNAVAILABLE（fail-closed，默认拒派）

状态根（plan §C0）：本脚本在任何业务 import 之前就 `setdefault("STEWARD_STATE_DIR",
生产绝对根)` —— 账本路径只经 `ai_store.state_dir()`，不经 `PLUGIN_ROOT`，故不随 worktree 漂。
    CMD:        set STEWARD_STATE_DIR=C:/Users/Administrator/Documents/mate/plugins/ai-steward/state
    PowerShell: $env:STEWARD_STATE_DIR="C:/Users/Administrator/Documents/mate/plugins/ai-steward/state"
测试/多实例请显式把 STEWARD_STATE_DIR 指到 tmp，并用 `--caps <path>` 或把 `lane_caps.json`
显式 copy 到该状态根（两条夹具腿，见 plan §C0 硬规矩 2）。

读命令有写副作用（不藏）：`list/stats/check` 入口先做一次惰性 reap，可能标 `expired`
并追加 `EXPIRE` 流水；除此之外的写只发生在 `claim/heartbeat/release`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_STATE_ROOT = "C:/Users/Administrator/Documents/mate/plugins/ai-steward/state"
# 必须在 import lane_ledger/ai_store 之前钉住状态根（N7）
os.environ.setdefault("STEWARD_STATE_DIR", DEFAULT_STATE_ROOT)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane_ledger as L  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_REJECT, EXIT_UNAVAILABLE = 0, 2, 3, 4


def _build_parser() -> argparse.ArgumentParser:
    epilog = (
        "退出码：0 成功/幂等重放 · 2 用法错 · 3 业务拒绝(LANE_FULL/SESSION_BUSY/"
        "LEASE_EXPIRED/NOT_OWNER/BAD_STATE/ORPHAN) · 4 账本不可用(LEDGER_UNAVAILABLE)\n"
        f"生产状态根（唯一合法账本根）：{DEFAULT_STATE_ROOT}\n"
        "   CMD:        set STEWARD_STATE_DIR=" + DEFAULT_STATE_ROOT + "\n"
        "   PowerShell: $env:STEWARD_STATE_DIR=\"" + DEFAULT_STATE_ROOT + "\"\n"
        "读命令 list/stats/check 入口会惰性 reap，可能写 EXPIRE 流水（有意为之）。\n"
        "lane 上限单一真相源：<状态根>/lane_caps.json（入 git）；测试用 --caps <path>。"
    )
    p = argparse.ArgumentParser(
        prog="lane_claim.py",
        description="并发账本 CLI：占位/续租/释放/清单/统计/回收/对账",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="机器可读输出（单行 JSON）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def caps_opt(sp):
        sp.add_argument("--caps", default=None, metavar="PATH",
                        help="lane 上限文件（缺省 <状态根>/lane_caps.json）")
        sp.add_argument("--force-degraded", action="store_true",
                        help="账本不可用时放行并留 DEGRADED_ADMIT 流水；"
                             "只对 LEDGER_UNAVAILABLE 生效，不得越过 LANE_FULL/SESSION_BUSY/LEASE_EXPIRED")
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="机器可读输出")

    sp = sub.add_parser("claim", help="占位（--phase reserve|bind 两段式）")
    sp.add_argument("--lane", default=None, help="R4/R4-2/STEP/NEX/GLM/OUTSOURCE（reserve 必填）")
    sp.add_argument("--seat", default=None, help="席位号，如 W1 / C-103（reserve 必填）")
    sp.add_argument("--order", default=None, dest="order_id",
                    help="订单唯一码，如 C-20261120-003（reserve 必填）")
    sp.add_argument("--phase", default="reserve", choices=("reserve", "bind"),
                    help="reserve=容量闸（CreateSession 之前）/ bind=补 sessionId+commandId")
    sp.add_argument("--slot", default=None, help="bind 必填：reserve 回显的 slot_id")
    sp.add_argument("--prep-id", default=None, dest="prep_id",
                    help="reserve 调用方自定标识（缺省自动生成并在回显返回，重试时原样重放）")
    sp.add_argument("--session", default="", dest="session_id", help="bind 必填：sessionId")
    sp.add_argument("--command", default="", dest="command_id", help="bind 必填：commandId")
    sp.add_argument("--model", default="", help="providerId/modelId 原样（可缺省）")
    sp.add_argument("--priority", default="P1", help="P0/P1/P2（满位排队排序用）")
    sp.add_argument("--ttl", type=int, default=None,
                    help="租约秒数（reserve 缺省 300 / bind 缺省 900）")
    sp.add_argument("--owner", default="", help="持有者标识（可选；给定时做持有者校验）")
    sp.add_argument("--note", default="", help="备注（进 meta.note）")
    caps_opt(sp)

    sp = sub.add_parser("heartbeat", help="续租（claimed → leased）")
    sp.add_argument("--slot", required=True)
    sp.add_argument("--ttl", type=int, default=None, help="缺省 900")
    sp.add_argument("--owner", default="")
    sp.add_argument("--force", action="store_true", help="非持有者强制续租")
    caps_opt(sp)

    sp = sub.add_parser("release", help="释放（→ released）")
    sp.add_argument("--slot", required=True)
    sp.add_argument("--reason", default="done", help="done/handoff/cancel/force")
    sp.add_argument("--owner", default="")
    sp.add_argument("--force", action="store_true", help="非持有者强制释放")
    caps_opt(sp)

    sp = sub.add_parser("list", help="活跃占位清单")
    sp.add_argument("--lane", default=None)
    caps_opt(sp)

    sp = sub.add_parser("stats", help="每 lane used/cap/free + 队列深度 + 今日回收")
    caps_opt(sp)

    sp = sub.add_parser("reap", help="手工回收（惰性回收同源）")
    caps_opt(sp)

    sp = sub.add_parser("check", help="对账 ORPHAN / STALE / DRIFT")
    sp.add_argument("--registry", default=None, help="registry.json 路径（ORPHAN 段）")
    sp.add_argument("--presence-json", default=None, dest="presence_json",
                    help="presence 会话清单 JSON（STALE 段）")
    sp.add_argument("--presence-db", default=None, dest="presence_db",
                    help="ZCode sqlite（只读 mode=ro，读 session_runtime_presence 表）")
    caps_opt(sp)
    return p


def _emit(args, payload: dict, human: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(human)


def _iso(ts) -> str:
    """epoch 秒 → 本地 ISO（人读回显用；--json 仍给原始浮点）。"""
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return str(ts)


def _human_claim(r: dict) -> str:
    if r.get("degraded"):
        return (f"DEGRADED_ADMIT lane={r.get('lane')} seat={r.get('seat')} "
                f"phase={r.get('phase')} —— 账本不可用，本次未落账（原因：{r.get('reason')}）")
    tag = "REPLAY" if r.get("replay") else "OK"
    extra = " reclaimed=slot_expired" if r.get("reclaimed") else ""
    if r.get("phase") == "bind":
        return (f"{tag} {r['slot_id']} bound used={r['used']}/{r['cap']} free={r['free']} "
                f"idem={r.get('idem_key')} lease_until={_iso(r.get('lease_until'))}{extra}")
    return (f"{tag} {r['slot_id']} used={r['used']}/{r['cap']} free={r['free']} "
            f"prep_id={r.get('prep_id')} lease_until={_iso(r.get('lease_until'))}{extra}")


def _dispatch(args) -> tuple[int, dict, str]:
    caps = args.caps
    if args.cmd == "claim":
        r = L.claim(args.lane, args.seat, args.order_id, phase=args.phase, slot=args.slot,
                    prep_id=args.prep_id, session_id=args.session_id,
                    command_id=args.command_id, model=args.model, priority=args.priority,
                    ttl=args.ttl, caps_file=caps, force_degraded=args.force_degraded,
                    owner=args.owner, note=args.note)
        return EXIT_OK, r, _human_claim(r)

    if args.cmd == "heartbeat":
        r = L.heartbeat(args.slot, args.ttl, owner=args.owner, force=args.force,
                        caps_file=caps, force_degraded=args.force_degraded)
        if r.get("degraded"):
            return EXIT_OK, r, f"DEGRADED_ADMIT heartbeat slot={args.slot}（未落账）"
        return EXIT_OK, r, (f"OK {r['slot_id']} state=leased "
                            f"lease_until={_iso(r['lease_until'])} (+{r['lease_ttl']}s)")

    if args.cmd == "release":
        r = L.release(args.slot, args.reason, force=args.force, owner=args.owner,
                      caps_file=caps, force_degraded=args.force_degraded)
        if r.get("degraded"):
            return EXIT_OK, r, f"DEGRADED_ADMIT release slot={args.slot}（未落账）"
        if not r.get("released"):
            return EXIT_OK, r, (f"OK {r['slot_id']} 已是终态 state={r.get('state')} "
                                f"used={r['used']}/{r['cap']}（幂等）")
        nxt = f" next_up={r['next_up']}" if r.get("next_up") else ""
        return EXIT_OK, r, (f"OK {r['slot_id']} released used={r['used']}/{r['cap']} "
                            f"free={r['free']}{nxt}")

    if args.cmd == "list":
        r = L.list_slots(args.lane, caps_file=caps)
        if not r["rows"]:
            return EXIT_OK, r, "（无活跃占位）"
        lines = [f"{'SLOT':<12} {'LANE':<9} {'SEAT':<10} {'ORDER':<18} {'STATE':<8} "
                 f"{'PHASE':<8} {'SESS':<9} REMAIN"]
        for x in r["rows"]:
            lines.append(f"{x['slot_id']:<12} {x['lane']:<9} {str(x['seat']):<10} "
                         f"{str(x['order_id']):<18} {x['state']:<8} {str(x['phase']):<8} "
                         f"{x['session']:<9} {x['remaining_sec']}s")
        if r.get("reaped"):
            lines.append(f"（本次惰性回收 {r['reaped']} 条，已写 EXPIRE 流水）")
        return EXIT_OK, r, "\n".join(lines)

    if args.cmd == "stats":
        r = L.stats(caps_file=caps)
        return EXIT_OK, r, L.summary_line(r)

    if args.cmd == "reap":
        r = L.reap(caps_file=caps, force_degraded=args.force_degraded)
        if r.get("degraded"):
            return EXIT_OK, r, "DEGRADED_ADMIT reap（账本不可用，未回收）"
        detail = " ".join(f"({x['slot_id']} {x['over_sec']}s>{x['reason']})" for x in r["rows"])
        return EXIT_OK, r, f"OK expired={r['expired']} {detail}".rstrip()

    if args.cmd == "check":
        r = L.check(args.registry, presence_json=args.presence_json,
                    presence_db=args.presence_db, caps_file=caps)
        head = (f"ORPHAN={r['orphan_count']} STALE={r['stale_count']} "
                f"DRIFT={r['drift_count']} (root={r['root']}, presence_checked={r['presence_checked']})")
        lines = [head]
        for x in r["orphan"]:
            lines.append(f"  ORPHAN task={x['task']} session={x['session']} state={x['state']}")
        for x in r["stale"]:
            lines.append(f"  STALE slot={x['slot_id']} session={x['session']} lane={x['lane']}")
        for x in r["drift"]:
            lines.append(f"  DRIFT {x}")
        for x in r["caps_drift"]:
            lines.append(f"  CAPS_DRIFT {x}（告警，不计入退出码；本单不改 slot_pool.py）")
        dirty = (r["orphan_count"] + r["stale_count"] + r["drift_count"]) > 0
        return (EXIT_USAGE if dirty else EXIT_OK), r, "\n".join(lines)

    raise L.UsageError(f"未知子命令 {args.cmd}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        code, payload, human = _dispatch(args)
    except L.UsageError as exc:
        print(f"ERROR {exc.code}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except L.Reject as exc:
        _emit_reject(args, exc)
        return EXIT_REJECT
    except L.Unavailable as exc:
        print(f"ERROR {exc.code}: {exc}"
              + ("" if getattr(args, "force_degraded", False)
                 else "  —— fail-closed，拒派；确需继续加 --force-degraded 并留痕"),
              file=sys.stderr)
        return EXIT_UNAVAILABLE
    _emit(args, payload, human)
    return code


def _emit_reject(args, exc: "L.Reject") -> None:
    payload = exc.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
        return
    bits = " ".join(f"{k}={v}" for k, v in exc.extra.items()
                    if isinstance(v, (str, int, float, bool)))
    print(f"REJECT {exc.code} {exc.reason}" + (f" [{bits}]" if bits else ""))


if __name__ == "__main__":
    raise SystemExit(main())
