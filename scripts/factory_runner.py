# -*- coding: utf-8 -*-
"""AI 工厂 V3 调度内核（零 token 控制面 × Git Worktree 沙箱 × 预算双保险熔断）。

核心能力：
  1. 开机零开销：队列为空立即退出（0 LLM token）；所有调度/隔离/探针/记账全走确定性 Python。
  2. 两级预算熔断：每轮 Builder/Verifier 后按网关 /admin/status 差值结算 cost_cny；
     **只按现金通道（MONEY_LANE_PREFIX）计闸**：单卡 > CARD_BUDGET_CAP_CNY 停该单、全厂当日 >
     DAILY_BUDGET_CAP_CNY 整线停机（V4.0-P1 起账单分列 现金/账面/估算 三口径，见 record_stage_budget）。
  3. Worktree 物理隔离（复用 worker_worktree.py）：每单建 wt/<order_id> 沙箱，严禁 Builder 改主仓；
     双门全绿自动合入 main 并清理；失败保留 worktree 与日志供人工排障。
  4. CAD / 代码通道（kind=cad）：Gate 1 执行 dotnet/msbuild 编译 exit=0 + 离线单测通过 + diff 非空；
     不通过直接短路 Verifier 自动返工，零浪费复核算力。
  5. UI 看板通道（kind=ui_dashboard）：沙箱独立 HTTP 预览 + 无头 Edge --dump-dom 与 3 分辨率截图；
     除裸路径外，可见区出现 SQL 语句或数据库/内部蛇形表字段名同样判 FAIL。
  6. 24h 一键打回后门（reject）：DELIVERED 后 24h 内免罚重新入队返工，不弹确认卡、不计扣分。

V4.0-P1 增量（全部挂在 config/steward.json 的 "v4" 开关下，开关关闭 = V3.2.1 原行为）：
  - merge_gate          Builder 超时/停滞/rc≠0 不得进入 Gate1/合入（修 D-03）
  - wallclock_live      整单墙钟上限传入每个阶段超时，剩余 <60s 不再启动新阶段（修 D-06）
  - char_icon_hard_fail 可见区字符图标/emoji 命中即 Gate1 FAIL（修 D-04）
  - uispec_prompt       Builder 审美提示改由 uispec_rules.json 生成，删旧审美硬编码（修 D-05）
  - drop_details_gate   撤销「折叠 <details> ≥6」旧闸（与 UISPEC v1.0 冲突）
  - fuse_dry_run_until  到期前现金熔断只告警不停机（计量修复后 24h 观察期，决策 D2）
  - 派单延迟改从最近一次重排队时刻起算；网关估算用量单列（usage_schema=2）

V4.0-P2 增量（断言驱动质检，修 D-02；同样挂在 "v4" 开关下）：
  - assertion_gate      规格书 ```assertions``` 块由 Gate1 在指定状态 × 视口真机执行；断言 FAIL → 返工（回传实测/期望）；
                        required 断言 UNVERIFIED → 自动补证 1 次，仍不行 → 转人工（证据不足，不合入）；
                        PASS 后只把「断言 PASS」的条款写进 baseline.json，其余契约条款标 done_unasserted
  - require_assertions  UI 单规格缺断言块 → inbox 拒收（_rejected/ + 原因文件）/ 执行期不开工
  - closeout_gate       第 2 轮起 Verifier 必须对上一轮 confirmed P0/P1 逐条答 closed|still_open|regressed，漏答按未关

V4.0-P4 增量（安全收口，修 D-12）：内核访问 8900 看板改用 X-Board-Token 请求头，口令不再拼进网址。

V5.0-real.3 增量（2026-11-26，两次真实事故的修复）：
  - 合入时 git 的三种「没东西可提交」说法都算数（工人自己提交过、沙箱只剩运行态文件时不再误判合入失败）
  - 控制台事件护栏：隐藏窗口里的跑单进程收到 Ctrl+C 只留痕不退出（tick 不再「没正常收口」）
  - 顶层致命异常留痕：轨迹 kernel_fatal + 巡检告警，不再查无实据

V5.0-real.4 增量（2026-11-26，V5.0 清单里最后一条名义规则做实）：
  - 验收脚本本轮真被改了 → 质检简报事前单列请质检专门核对（不再只在收尾贴一句标注）

V5.0-real.5 增量（2026-11-26，补 Gate1 的一个取证缺口）：
  - code 单的目标里带看板页面（dashboard/*.html）且规格书的 ```assertions``` 块是合法 JSON 数组 → 机检过了再上真引擎：
    对沙箱里的这个页面跑完整 Gate1 探针并逐条判断言，截图 / 遥测 / 断言表随简报交质检
    （此前只有 ui 单过真引擎，code 单零运行时证据也机械放行；质检连挂两代 P1「不放行」也拦不住）
    断言块写成散文清单的 code 单照旧不机检（历史考卷多半如此），不拦单
  - 不冤枉工人：取证环境故障（Edge / CDP 起不来）、探针自身异常不算施工缺陷（不判 FAIL，如实记进 checks.runtime_evidence）；
    页面通检查出的毛病，生产页本来就有的不算本单的（沙箱页有毛病时再对生产页跑一遍同款探针比对）；断言 FAIL 一律算本单的
  - 质检上限 VERIFIER_TIMEOUT_SEC 600 → 900 秒（G44 家族的质检多次贴着 600 秒；G44R7 首检 38 次请求 600 秒被斩）

V5.0-real.6 增量（2026-11-26，后端合入 ≠ 上线）：
  - 合入了看板服务加载的模块（v3_data.py 等）时核一下看板服务进程的启动时刻：进程比盘上的数据层还旧 →
    事件「合入未生效提醒」+ 巡检 [MERGE_NOT_LIVE]（board_server 把数据层模块缓存在进程里，不重启就一直用旧的；
    2026-11-25 18:49 起的进程让之后合入的抽屉修复对访客不可见）。内核只留痕不重启（守护面归 BoardGuard / 厂长）；
    查不了进程表就不下结论，合入结论不受影响

V5.0-real.7 增量（2026-11-26，Gate1 真引擎的数据层口径）：
  - 沙箱预览服务的 /data.json 改用**被测 worktree 自己的 v3_data.py** 现算（独立子进程，状态目录仍指生产 state）；
    旧实现 import 到的是内核进程里缓存的活仓模块，沙箱改 v3_data.py 等于没测——G44R8 R8-2 因此被冤判 FAIL
  - 沙箱数据层自己报错 → 预览服务如实 500（不再静默回落到活仓旧数据）；只有主仓自测才允许退回进程内同版模块

V5.0-real.8 增量（2026-11-26，ui 单也剔生产页既有毛病）：
  - ui 单 Gate1 查出通检毛病时，先对主仓生产页跑一遍同款探针（不带断言）：生产页本来就有的毛病不计本单；
    断言 FAIL 永不剔除；正文墙按「沙箱计数 ≤ 基线计数 + 500 字漂移容差」判既有（计数含实时文本，逐字节比对会误判）
  - 基线探针自己起不来时如实留痕（checks.gate1_baseline），毛病仍按本单处理；G53 因生产页既有正文墙 5886>4500 被误判 FAIL 的教训

V5.0-real.9 增量（2026-11-26，code 单也按同一口径剔生产页既有毛病）：
  - code 单（real.5 那条路）的「生产页本来就有」原是逐字比对；正文墙那条消息里带字数（沙箱 5841 / 生产页 59xx），
    永远对不上 → 生产页自带的正文墙全算到本单头上，白开一轮施工去砍首页文字（G57 第 3 轮实况）。现在与 ui 单同一口径：
    沙箱计数 ≤ 基线计数 + 500 字漂移容差即算既有；断言 FAIL 照旧永不剔除；比生产页更差的照判本单
  - code 单的生产页基线结论也单独记一条事件「Gate1生产页基线」（gate1_baseline），与 ui 单同名同文案

V4.0-P3 增量（看板受控重构的验收工具，修 D-07）：
  - 断言新增 metric：visual.diff_pct（与主仓生产版逐像素比对，可遮罩动态区）、lint.<规则> / lint.total、src.<体量>
  - lint_gate           Gate1 源码级 UISPEC 检查：任何计数规则比生产版多 → FAIL（只许降不许升）
  - probe-candidate     CLI：对一个现成的候选 html 跑完整 Gate1（含断言/像素比对），--apply 通过才提交主仓
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as _dt
import functools
import glob
import hashlib
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True

# Preserve native profile paths (e.g. REPLACE_WITH_USER during SSH runs) before normalizing HOME/USERPROFILE for worker_worktree.
NATIVE_USERPROFILE = os.environ.get("USERPROFILE", r"C:\Users\Administrator")
NATIVE_APPDATA = os.environ.get("APPDATA", r"C:\Users\Administrator\AppData\Roaming")
NATIVE_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", r"C:\Users\Administrator\AppData\Local")

# Normalize profile paths before importing worker_worktree so DEFAULT_ROOT is consistent
# across SSH (REPLACE_WITH_USER), Scheduled Task (SYSTEM), and desktop runs.
os.environ["USERPROFILE"] = r"C:\Users\Administrator"
os.environ["HOME"] = r"C:\Users\Administrator"

STEWARD_ROOT = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
BP_ROOT = r"C:\Users\Administrator\Documents\mate\dll\factory-blueprint"
MATE_ROOT = r"C:\Users\Administrator\Documents\mate"
# V5.0：同仓模块按「本文件所在目录」导入。旧写法把活仓 scripts 硬插到 sys.path 最前——AA 上镜像/测试/沙箱里
# import factory_runner 拉进来的是活仓旧模块，待上线那份代码从来没被测到（2026-11-25 镜像回归假红即此）。
def _import_own_scripts_first() -> str:
    own = os.path.dirname(os.path.abspath(__file__))
    live = os.path.join(STEWARD_ROOT, "scripts")
    primary = own if os.path.isfile(os.path.join(own, "worker_worktree.py")) else live
    for path, front in ((primary, True), (live, False)):      # 活仓目录只做兜底，排最后
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0 if front else len(sys.path), path)
    return primary


SCRIPTS_DIR = _import_own_scripts_first()

try:
    import worker_worktree
except Exception:
    worker_worktree = None

try:
    import ui_lint  # V4.0-P3：源码级 UISPEC 合规检查 / 死声明瘦身
except Exception:
    ui_lint = None

QUEUE_PATH = os.path.join(STEWARD_ROOT, "state", "factory_queue.json")
INBOX_DIR = os.path.join(STEWARD_ROOT, "state", "factory_inbox")
LOCK_PATH = os.path.join(STEWARD_ROOT, "state", "factory_runner.lock")
PROVIDER_CFG_PATH = os.path.join(STEWARD_ROOT, "state", "factory_provider_config.json")
VERIFIER_CFG_PATH = os.path.join(STEWARD_ROOT, "state", "factory_provider_config_verifier.json")  # vthink1: verifier专用道(auto), 防auto-night夜间思考螺旋烧穿输出帽
BUDGET_LEDGER_PATH = os.path.join(STEWARD_ROOT, "state", "v3_budget_ledger.json")
PATROL_ALERT = os.path.join(STEWARD_ROOT, "state", "PATROL_ALERT.txt")
RUN_LOG_DIR = os.path.join(STEWARD_ROOT, "logs", "factory_runner")
DASHBOARD_DIR = os.path.join(STEWARD_ROOT, "dashboard")
TOKEN_FILE = os.path.join(STEWARD_ROOT, "config", "board_token.txt")
VERIFY_PROFILES_DIR = os.path.join(STEWARD_ROOT, "verify_profiles")
BP_VERIFY_PROFILES_DIR = os.path.join(BP_ROOT, "verify_profiles")


def normalize_order_kind(kind: str) -> str:
    k = str(kind or "ui_dashboard").strip().lower()
    if k in ("ui", "ui_dashboard", "dashboard"):
        return "ui"
    if k == "cad":
        return "cad"
    return "code"


def load_verify_profile(kind: str) -> tuple[str, str, str, str]:
    """Load order-type-specific Verifier checklist template without adding any permanent seats."""
    pk = normalize_order_kind(kind)
    role_map = {
        "ui": "UI审美质检员",
        "cad": "代码审计质检员",
        "code": "逻辑质检员",
    }
    role_label = role_map.get(pk, "逻辑质检员")
    for root in (VERIFY_PROFILES_DIR, BP_VERIFY_PROFILES_DIR):
        p = os.path.join(root, f"{pk}.md")
        if os.path.exists(p):
            try:
                return pk, role_label, p, open(p, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                pass
    return pk, role_label, "", ""

GATEWAY_STATUS_URL = "http://127.0.0.1:8788/admin/status"
BOARD_BASE_URL = "http://127.0.0.1:8901"

import socket
import tempfile
import uuid

NODE_EXE = r"C:\Program Files\nodejs\node.exe"
ZCODE_CJS = r"C:\Users\Administrator\AppData\Local\Programs\ZCode\resources\glm\zcode.cjs"
RUNTIME_PROVIDER_ROOT = r"C:\Users\Administrator\.zcode\v2\runtime\provider\windows-x86_64"
DB_SQLITE = r"C:\Users\Administrator\.zcode\cli\db\db.sqlite"
TASKS_INDEX_SQLITE = r"C:\Users\Administrator\.zcode\v2\tasks-index.sqlite"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
DOTNET_EXE = r"C:\Program Files\dotnet\dotnet.exe"
SIX_STATIC_PAGES = [
    "architecture.html",
    "flow.html",
    "simple.html",
    "plans.html",
    "keepalive.html",
    "concurrency.html",
]

RAW_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|C:/|\.sqlite\b|\\\\Users\\\\)")
RAW_SQL_SCHEMA_RE = re.compile(
    r"(?:"
    r"\bSELECT\b.{1,80}?\bFROM\b"
    r"|\b(?:LEFT|INNER|RIGHT|FULL|CROSS)\s+JOIN\b"
    r"|\b(?:GROUP|ORDER)\s+BY\b"
    r"|\bINSERT\s+INTO\b"
    r"|\bUPDATE\s+\w+\s+SET\b"
    r"|\bPRAGMA\s+table_info\b"
    r"|\b(?:tasks_index|session_input|session_runtime_presence|turn_usage|model_usage|"
    r"tool_usage|automation_runs|local_setting|time_created|time_updated|time_archived|"
    r"time_deleted|task_status|target_task_id|provider_config|seat_registry|next_run_at|"
    r"last_run_at|cost_cny|tokens_in|tokens_out|tokens_cached|prompt_tokens|completion_tokens)\b"
    r")",
    re.I,
)

KERNEL_VERSION = "V5.0"
# V5.0 真化批次号：每笔订单的 run_start / tick 输出都带它，用来确认在制的单跑的是哪一版内核
KERNEL_BUILD = "v5.0-real.9"
# ---- V4.0-P4 lanes（C-20261124-G40b）：N 道并行派发。v4.slots（enabled!=false 且 max>1）生效时
# cmd_tick 不吃全局锁，由槽位账本管并发+同家族串行；队列落账锁内按行合并防互覆；开关关=原单道。
_LANE = {"mode": False, "order": ""}          # 本进程并行道状态（mode 由 cmd_tick 现场判定）
_LANE_REV_RE = re.compile(r"-R\d{2,}$")       # 24h 打回 Rev 号（-R60/-R8617，口径同泵 base_of）
_LANE_GEN_RE = re.compile(r"(?<!-)R\d+$")     # 真代数（G34R2 -> G34）
_LANE_TERMINAL = ("done", "failed", "needs_human", "infra_error")

# ---- C-20261125-G42 下单即立约（用户语义：下单即立合同，未拒单即有效）
# 合同层实现全在独立模块 scripts/contracts.py（合同文件 state/contracts/<单号>.json）。
# 内核只留挂点：① enqueue 落账成功后立约 ② 队列落账 running=记开工 ③ 队列落账终态=定 done|failed。
# 合同是旁路证据：模块缺失/读写失败只记 trace，绝不阻断 enqueue/tick/收口（老单无约零报错）。
_CONTRACTS = {"loaded": False, "mod": None}


def contracts_layer():
    """惰性加载合同层（按本文件同目录定位，不依赖 sys.path；缺失返回 None 内核照常工作）。"""
    if _CONTRACTS["loaded"]:
        return _CONTRACTS["mod"]
    _CONTRACTS["loaded"] = True
    try:
        import importlib.util
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contracts.py")
        _spec = importlib.util.spec_from_file_location("steward_contracts", _p)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _CONTRACTS["mod"] = _mod
    except Exception as e:  # noqa: BLE001 —— 合同层不可用不阻断内核
        v4_trace("contracts_layer_unavailable", "", error=repr(e))
        _CONTRACTS["mod"] = None
    return _CONTRACTS["mod"]


def _contract_hook(action: str, job_or_id, **kw) -> None:
    """三处挂点的唯一入口：合同异常一律吞掉并 trace，队列/产线的权威性不受合同旁路影响。"""
    mod = contracts_layer()
    if mod is None:
        return
    is_job = isinstance(job_or_id, dict)
    oid = str((job_or_id.get("order_id") if is_job else job_or_id) or "")
    try:
        if action == "enqueue":
            mod.ensure_contract(job_or_id, queue_path=QUEUE_PATH)
        elif action == "sync":
            mod.sync_contract_for_job(job_or_id, queue_path=QUEUE_PATH)
        elif action == "reject":
            mod.mark_contract_rejected(oid, kw.get("reason", ""), queue_path=QUEUE_PATH)
    except Exception as e:  # noqa: BLE001 —— 合同旁路绝不阻断内核
        v4_trace("contract_hook_skipped", oid, action=action, error=repr(e))


def _family_of(order_id: str) -> str:
    """家族口径（同泵 base_of/monitor5）：先剥 -R<Rev> 再剥尾部真代数 R<代>。"""
    return _LANE_GEN_RE.sub("", _LANE_REV_RE.sub("", order_id or ""))


def _slots_enabled() -> bool:
    """v4.slots 并行道开关：slots_max()>1 才启用。"""
    try:
        import slot_ledger
        return slot_ledger.slots_max() > 1
    except Exception:
        return False


def _lane_pick(queued: list, fam_busy: set) -> "dict | None":
    """G40b：FIFO 扫描——跳过同家族在制（fam_busy=活动槽家族集），全忙返回 None。"""
    for cand in queued:
        if _family_of(str(cand.get("order_id") or "")) not in fam_busy:
            return cand
    return None


def _lane_fill_plan(queued: list, primary: "dict | None", fam_busy: set, free_slots: int) -> list:
    """P4b：本轮 tick 除 primary 外还能并行的单（同家族不并行；free_slots=扣掉 primary 后的空槽数）。"""
    if not queued or free_slots <= 0:
        return []
    fams = {f for f in (fam_busy or set()) if f}
    if primary is not None:
        fams.add(_family_of(str(primary.get("order_id") or "")))
    out = []
    for cand in queued:
        if cand is primary:
            continue
        f = _family_of(str(cand.get("order_id") or ""))
        if f in fams:
            continue
        fams.add(f)
        out.append(cand)
        if len(out) >= free_slots:
            break
    return out


def _spawn_lane_tick(order_id: str) -> bool:
    """P4b：为并行道拉起独立 tick 子进程（--order 锁死目标单，子进程自 claim/自 release）。

    背景：Windows 计划任务同一任务默认串行（IgnoreNew），一次 tick 只跑一单，N=2 永远用不上。
    由本 tick 主动扇出，两条道都真正跑起来；子进程与父进程之间靠槽位账本互斥。
    """
    if not order_id:
        return False
    try:
        cmd = [sys.executable or "python", "-X", "utf8", os.path.abspath(__file__), "tick", "--order", order_id]
        flags = 0
        if os.name == "nt":
            flags = int(getattr(subprocess, "DETACHED_PROCESS", 0)) | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(__file__)),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
        append_v3_event(order_id, 0, "lane_spawn", "并行道派发",
                        f"空槽扇出：拉起独立 tick 进程执行 {order_id}")
        return True
    except Exception as exc:  # noqa: BLE001 —— 扇出失败只降级为串行，不拖垮本轮
        _append_patrol(f"[LANE_SPAWN_FAIL {iso_now()}] {order_id}: {exc!r}")
        return False


def _budget_lock_cm():
    """P4b：预算账本读改写串行化（N 道并发时两个进程同时 load→write 会互相覆盖，成本少记=熔断失灵）。"""
    try:
        import ai_store
        return ai_store.file_lock("v3_budget_ledger", 120)
    except Exception:
        return contextlib.nullcontext()


def _budget_serialized(fn):
    """装饰器：整段读改写持有预算账本锁（含熔断判定与落盘）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _budget_lock_cm():
            return fn(*args, **kwargs)
    return wrapper


def _lane_qcommit(job: dict, q: dict, fresh: bool = False) -> None:
    """G40b 队列落账统一口：并行道=锁内 reload 只动本单行；单道=原全量写（两态等价）。

    C-20261125-G42：同一口顺带同步合同状态（running=记开工 / 终态=定 done|failed），
    合同异常一律吞掉——队列落账的权威性不受合同旁路影响。
    """
    try:
        _contract_hook("sync", job)
    except Exception as e:  # noqa: BLE001 —— 合同旁路绝不阻断队列落账
        v4_trace("contract_sync_skipped", str(job.get("order_id") or ""), error=repr(e))
    jid, term = job.get("job_id"), job.get("status") in _LANE_TERMINAL
    if not _LANE["mode"]:
        if fresh:                                  # 旧收口口径：reload 后合并（原 closeout 行为）
            q_fresh = load_queue()
            if not term:
                # V5.0 工厂故障原单重排：单子还没完，必须留在队列里（与并行道口径一致）。
                # 旧写法不看终态一律归档进 history，重排的单子就成了没人再跑的孤儿。
                q_fresh["jobs"] = ([job if j.get("job_id") == jid else j for j in (q_fresh.get("jobs") or [])]
                                   if any(j.get("job_id") == jid for j in (q_fresh.get("jobs") or []))
                                   else (q_fresh.get("jobs") or []) + [job])
            else:
                q_fresh["history"] = [h for h in (q_fresh.get("history") or []) if h.get("job_id") != jid] + [job]
                q_fresh["jobs"] = [j for j in (q_fresh.get("jobs") or []) if j.get("job_id") != jid]
            q["history"], q["jobs"] = q_fresh["history"], q_fresh["jobs"]
            atomic_write_json(QUEUE_PATH, q_fresh)
            return
        if term:
            q["history"] = (q.get("history") or []) + [job]
            q["jobs"] = [j for j in q["jobs"] if j.get("job_id") != jid]
        atomic_write_json(QUEUE_PATH, q)
        return
    import slot_ledger
    with slot_ledger.file_lock("factory_queue", 30):
        qc = load_queue()
        if term:
            qc["jobs"] = [j for j in (qc.get("jobs") or []) if j.get("job_id") != jid]
            qc["history"] = [h for h in (qc.get("history") or []) if h.get("job_id") != jid] + [job]
        else:
            qc["jobs"] = ([job if j.get("job_id") == jid else j for j in (qc.get("jobs") or [])]
                          if any(j.get("job_id") == jid for j in (qc.get("jobs") or []))
                          else (qc.get("jobs") or []) + [job])
        q["history"], q["jobs"] = qc.get("history") or [], qc.get("jobs") or []
        atomic_write_json(QUEUE_PATH, qc)
PAUSE_FLAG_PATH = os.path.join(STEWARD_ROOT, "state", "factory_paused.flag")
MAX_GATE1_INFRA_RETRIES = 2
MAX_VERIFIER_INFRA_RETRIES = 2
MAX_JOB_INFRA_REQUEUES = 2            # 工厂侧故障（探针 / 质检输出 / 回归镜像）原单自动重跑的次数上限
JOB_INFRA_REQUEUE_DELAY_SEC = 120     # 每次重跑前等一会儿，避开正在恢复的环境
MAX_CRASH_REQUEUES = 3                # 内核崩溃后的自愈重排封顶，超过转 failed（交给泵重派或专家）
MAX_MERGE_REQUEUES = 2                # 质检已过、合入被工厂侧挡住：原单自动回来只补「双门 + 合入」的次数上限
JOB_MERGE_REQUEUE_DELAY_SEC = 180     # 每次补合入前等一会儿（仓库锁 / 索引占用多半自己会松）
EXPERT_TIMEOUT_SEC = 900              # 专家段一次会话的上限（与 expert_seat.RUN_TIMEOUT_SEC 一致）
EXPERT_RESCUE_EXT_SEC = 2400          # 专家救场轮给整单墙钟加时：专家 ≤15 分钟 + 一轮施工 + 双门
EXPERT_RESCUE_MIN_SEC = 1800          # 墙钟加不了时（非 live 模式）至少要剩这么多才请专家
ZCODE_V2_LOG_DIR = r"C:\Users\Administrator\.zcode\v2\logs"
WEEKEND_GLM_MIN_REMAINING_PCT = 5.0
WEEKEND_GLM_COOLDOWN_SEC = 1200
WEEKEND_GLM_BUILDER_TIMEOUT_SEC = 240
WEEKEND_GLM_VERIFIER_TIMEOUT_SEC = 150
WEEKEND_GLM_STALL_SEC = 75

LOCK_STALE_SEC = 1800
BUILDER_TIMEOUT_SEC = 2400  # 2026-11-22 calibrate: code-lane builder runs 700-900s, 900s wall falsely fused finished orders (G16 896s, G17 854s)
VERIFIER_TIMEOUT_SEC = 900  # 2026-11-26 再校准：近 137 次质检里 G44 家族 5 次跑满 440-523s，G44R7 首检 38 次请求 600s 被斩（慢但在推进 ≠ 空响应）；真卡死另有停滞检测。v7验尸: verifier 169s压哨过关/180s两度斩首(002v6) -> 600s
INITIAL_VISIBLE_CHAR_LIMIT = 4500

CARD_BUDGET_CAP_CNY = 100.0   # 预算治理v2: 现金闸-单卡（仅真金白银通道）
DAILY_BUDGET_CAP_CNY = 300.0  # 预算治理v2: 现金闸-全厂日（唯一 circuit_open 触发器）
REJECT_WINDOW_SEC = 86400

# ------------------------------------------------------------------ V4.0-P1 开关与审美单一源

STEWARD_CONFIG_PATH = os.path.join(STEWARD_ROOT, "config", "steward.json")
UISPEC_RULES_CANDIDATES = [
    os.path.join(STEWARD_ROOT, "config", "uispec_rules.json"),
    os.path.join(BP_ROOT, "uispec_rules.json"),
]
V4_DEFAULT_FLAGS = {
    "merge_gate": False,
    "wallclock_live": False,
    "char_icon_hard_fail": False,
    "uispec_prompt": False,
    "drop_details_gate": False,
    "fuse_dry_run_until": "",
    "order_wallclock_sec": 0,
    # V4.0-P2 断言驱动质检（修 D-02）
    "assertion_gate": False,       # Gate1 执行规格断言 + 判决 v2 + 只把已验证条款写基线
    "require_assertions": False,   # UI 单规格必须带断言块，否则拒收（inbox）/ 不开工（执行期）
    "closeout_gate": False,        # 第 2 轮起 Verifier 必须对上一轮 confirmed P0/P1 逐条销账
    "probe_regex_fix": "off",      # off | observe | enforce：修复 V3 探针页面脚本丢失的 16 处转义（见 fix_probe_template_escapes）
    # V4.0-P3
    "lint_gate": False,            # Gate1 源码 UISPEC 违规计数只许降不许升（对比主仓生产版）
    "rewrite_authorized_orders": [],  # 决策 D4：允许「受控重写」的订单号（Q0#7 禁从零重写对其豁免，须同时带 visual.diff_pct 断言）
}
WALLCLOCK_MIN_STAGE_SEC = 60


def load_v4_flags(path: str = "") -> dict:
    """读 steward.json 的 "v4" 段；缺失/损坏一律回落 V3.2.1 行为（全部关闭）。每次调用现读，改开关免重启。"""
    flags = dict(V4_DEFAULT_FLAGS)
    try:
        cfg = json.load(open(path or STEWARD_CONFIG_PATH, "r", encoding="utf-8"))
        v4 = cfg.get("v4") if isinstance(cfg, dict) else None
        if isinstance(v4, dict):
            for k in V4_DEFAULT_FLAGS:
                if k in v4:
                    flags[k] = v4[k]
    except Exception:
        pass
    return flags


def fuse_dry_run_active(flags: dict, now_dt: "_dt.datetime | None" = None) -> bool:
    """fuse_dry_run_until = "YYYY-MM-DD HH:MM:SS"（本地时间）；早于该时刻时熔断只告警。"""
    raw = str(flags.get("fuse_dry_run_until") or "").strip()
    if not raw:
        return False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return (now_dt or _dt.datetime.now()) < _dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return False


def order_wallclock_cap(flags: dict) -> int:
    v = int(flags.get("order_wallclock_sec") or 0)
    return v if v > 0 else ORDER_WALL_CLOCK_MAX_SEC


def wallclock_remaining(e2e_t0: float, cap_sec: int, now_v: float | None = None) -> float:
    return float(cap_sec) - ((now_v if now_v is not None else now_ts()) - float(e2e_t0))


def cap_stage_timeout(timeout_sec: int, remaining_sec: float) -> int:
    """阶段超时不超过整单剩余墙钟；至少给 30s（再小的阶段由调用方在启动前拦截）。"""
    return int(max(30, min(int(timeout_sec), int(remaining_sec))))


def builder_incomplete(w_res: dict) -> str:
    """Builder 未正常结束则返回原因串，否则返回空串。复用草稿轮（shift=reuse）不受约束。"""
    if (w_res or {}).get("shift") == "reuse":
        return ""
    reasons = []
    if (w_res or {}).get("timed_out"):
        reasons.append("timed_out")
    if (w_res or {}).get("stall_timeout"):
        reasons.append("stall")
    rc = (w_res or {}).get("exit_code", 0)
    if rc not in (0, None):
        reasons.append(f"rc={rc}")
    return ",".join(reasons)


def _parse_local_ts(s: str) -> float:
    try:
        return _dt.datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def dispatch_base_ts(job: dict) -> float:
    """派单延迟起点：最近一次重排队时刻（requeued_ts / requeued_at）优先，否则首次入队时刻。"""
    for v in (job.get("requeued_ts"), _parse_local_ts(job.get("requeued_at") or "")):
        try:
            if v and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    return float(job.get("enqueued_ts") or job.get("started_ts") or now_ts())


def load_uispec_rules() -> tuple[dict, str]:
    for p in UISPEC_RULES_CANDIDATES:
        if os.path.exists(p):
            try:
                return json.load(open(p, "r", encoding="utf-8")), p
            except Exception:
                continue
    return {}, ""


def build_aesthetic_note(rules: dict, rules_path: str = "") -> str:
    """由 uispec_rules.json 生成 Builder 审美前置红线（唯一来源；内核不再硬编码任何审美数值）。"""
    if not rules:
        return (
            "\n## UI 审美前置红线（UISPEC 单一来源）\n"
            "- 唯一审美依据：`全厂-UISPEC设计系统规范.md`（当前机读白名单文件缺失，按规范原文执行）。\n"
            "- 只动呈现层，增量迁移，禁从零重写；禁 emoji/字符图标；三视口零横向溢出。\n"
        )
    def _j(key, unit=""):
        vals = rules.get(key) or []
        return "{" + ",".join(f"{v}{unit}" if isinstance(v, (int, float)) else str(v) for v in vals) + "}"
    colors = rules.get("semantic_colors") or {}
    col_txt = "、".join(f"{k}={'/'.join(v) if isinstance(v, list) else v}" for k, v in colors.items())
    hero = rules.get("hero_font_size") or {}
    hero_txt = " / ".join(f"{k}:{v}" for k, v in hero.items())
    return (
        f"\n## UI 审美前置红线（UISPEC {rules.get('uispec_version', '')} 机读白名单 · `{os.path.basename(rules_path) or 'uispec_rules.json'}`）\n"
        f"1. 字号只许 {_j('font_sizes_px', 'px')}；hero 断点 {hero_txt}；字重只许 {_j('font_weights')}（大数字一律 400）。\n"
        f"2. 彩色只许语义色族 {col_txt}；灰阶 {_j('grays')} + 白；大面积底色只许 z1 三档渐变。\n"
        f"3. 圆角只许 {_j('radii_px', 'px')}；border-width 全 0（唯一豁免 {', '.join(rules.get('border_exempt_selectors') or [])}）；阴影只许 `{rules.get('shadow', '')}`。\n"
        f"4. 动效时长只许 {_j('durations_ms', 'ms')}，缓动 `{rules.get('ease', '')}`；禁 count-up / 循环动画 / 布局动画。\n"
        f"5. 间距只许 {_j('spacing_px', 'px')}；SVG 图标 {_j('svg_icon_px', 'px')}；按钮高 {_j('button_heights_px', 'px')}。\n"
        "6. 禁 emoji / 字符图标（含圈数字①等）/ 图标字体 / 位图图标；禁引入新字体文件。\n"
        "7. 只动呈现层：/data.json 消费、fetch/poll、render 调度与 data-zone 锚点零改动；增量迁移，禁从零重写。\n"
        "8. 默认可见区 0 裸路径、0 SQL、0 下划线库表字段名；三视口 1440/1200/390 零横向溢出。\n"
    )


# ------------------------------------------------------------------ V4.0-P4 经验卡注入（G45）
# 派单前按订单 title+spec 关键词检索 docs/LESSONS.md（标题行）与 knowledge/INDEX.md（title/tags），
# 命中卡以固定标题注入 Builder prompt 头部。纯读取；库缺失/解析失败/零命中一律整段跳过（fail-open）。
KB_INJECT_HEADER = "## 相关经验卡（自动注入，开工必读）"
KB_INJECT_TOP_N = 6            # 每座库最多注入条数
KB_INJECT_CARD_CHARS = 400     # knowledge 卡正文截断上限（字符）
_KB_INJECT_LAST: dict = {"lessons": 0, "kb": 0}   # 最近一次注入命中数（供 KB-INJECT 派单日志核对）


def _kb_inject(title: str, spec_text: str) -> str:
    """返回经验卡注入段（零命中/任何异常 → ""）。只读 STEWARD_ROOT 下两座库，绝不回写。"""
    stats = {"lessons": 0, "kb": 0}
    _stop_ascii = {
        "the", "and", "for", "with", "that", "this", "from", "into", "your", "you", "are", "not",
        "all", "any", "can", "has", "have", "was", "were", "will", "but", "its", "our", "out",
        "may", "must", "please", "read", "write", "file", "files", "true", "false", "none",
    }
    _stop_cjk = {
        "操作", "文件", "页面", "数据", "配置", "检查", "验收", "命令", "版本", "时间", "问题",
        "工作", "项目", "系统", "使用", "需要", "可以", "必须", "这个", "进行", "如果", "因为",
        "所以", "但是", "没有", "已经", "一次", "所有", "全部", "内容", "输出", "输入", "结果",
        "情况", "方式", "做法", "方面", "步骤", "相关", "自动", "注入", "经验",
    }
    _aliases = (("javascript", "js"), ("typescript", "ts"), ("nodejs", "node"))
    _keep_ascii = {"js", "ts", "py", "ui", "cdp", "dom", "sql", "git", "ssh", "api", "css", "svg"}

    def _norm(text: str) -> str:
        t = str(text or "").casefold()
        for _a, _b in _aliases:
            t = t.replace(_a, _b)
        return t

    def _terms(text: str) -> set:
        """词元：ASCII 词（≥3，白名单 2 字）+ 中文 2~4 字 n-gram（过滤高频二元词）。"""
        t = _norm(text)
        out = set()
        for w in re.findall(r"[a-z][a-z0-9_.\-]{1,}", t):
            w = w.strip("._-")
            if not w or w in _stop_ascii:
                continue
            if len(w) >= 3 or w in _keep_ascii:
                out.add(w)
        for run in re.findall(r"[\u4e00-\u9fff]+", t):
            for n in (2, 3, 4):
                for i in range(len(run) - n + 1):
                    g = run[i:i + n]
                    if n == 2 and g in _stop_cjk:
                        continue
                    out.add(g)
        return out

    def _rank(rows: list) -> list:
        # 命中分降序（词元越长越具体），同分按 id 升序 → 结果确定性
        return [r[2] for r in sorted(rows, key=lambda r: (-r[0], r[1]))[:KB_INJECT_TOP_N]]

    try:
        q_cf = _norm(f"{title or ''}\n{spec_text or ''}")
        if not q_cf.strip():
            return ""
        lessons = []
        lessons_path = os.path.join(STEWARD_ROOT, "docs", "LESSONS.md")
        if os.path.exists(lessons_path):
            with open(lessons_path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            for m in re.finditer(r"^##\s+(K-\d{3})\s+(.+?)\s*$", raw, flags=re.M):
                kid, ktitle = m.group(1), m.group(2)
                score = sum(len(t) for t in _terms(ktitle) if t in q_cf)
                if score >= 2:
                    lessons.append((score, kid, (kid, ktitle)))
        lessons = _rank(lessons)

        cards = []
        index_path = os.path.join(STEWARD_ROOT, "knowledge", "INDEX.md")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
                raw_idx = fh.read()
            for ln in raw_idx.splitlines():
                cols = [c.strip() for c in ln.strip().strip("|").split("|")]
                if len(cols) < 5 or not re.match(r"^(?:K|INC)-\d", cols[0]):
                    continue
                cid, ctitle, ctags = cols[0], cols[3], cols[4]
                score = sum(len(t) for t in _terms(f"{ctitle} {ctags}") if t in q_cf)
                if score >= 2:
                    cards.append((score, cid, (cid, ctitle)))
        cards = _rank(cards)
        card_rows = []
        for cid, ctitle in cards:
            body = ""
            for sub in ("entries", "incidents", "private-memory"):
                p = os.path.join(STEWARD_ROOT, "knowledge", sub, f"{cid}.md")
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        txt = fh.read()
                    txt = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", txt, flags=re.S)  # 去 YAML 头
                    txt = re.sub(r"\s+", " ", txt).strip()
                    body = (txt[:KB_INJECT_CARD_CHARS - 1] + "…") if len(txt) > KB_INJECT_CARD_CHARS else txt
                    break
            card_rows.append((cid, ctitle, body))

        stats["lessons"], stats["kb"] = len(lessons), len(card_rows)
        if not lessons and not card_rows:
            return ""
        out = [KB_INJECT_HEADER, ""]
        if lessons:
            out.append(f"### 工程坑（docs/LESSONS.md 标题命中 {len(lessons)} 条）")
            out.extend(f"- **{kid}** {ktitle}" for kid, ktitle in lessons)
            out.append("")
        if card_rows:
            out.append(f"### 私有知识库（knowledge/ title·tags 命中 {len(card_rows)} 条）")
            for cid, ctitle, body in card_rows:
                out.append(f"- **{cid}** {ctitle}")
                if body:
                    out.append(f"  > {body}")
            out.append("")
        out.append("> 经验卡为只读自动注入，与本次规格冲突时以规格正本为准；禁止回写任何库卡。")
        return "\n".join(out) + "\n"
    except Exception as _e:                                    # fail-open：异常只记一行日志放行
        print(f"[factory_runner] KB-INJECT-FAIL {type(_e).__name__}: {_e}", flush=True)
        return ""
    finally:
        _KB_INJECT_LAST.update(stats)


# ------------------------------------------------------------------ V4.0 全量运行日志（供后续升级研究）

V4_TRACE_DIR = os.path.join(STEWARD_ROOT, "logs", "v4_trace")
_V4_RUN_CTX: dict = {}


def v4_trace(event: str, order_id: str = "", **fields) -> None:
    """结构化全量日志：logs/v4_trace/trace-YYYYMMDD.jsonl，一行一事件，只追加，永不抛错。

    与 events.jsonl（看板实况流，人话摘要）分工：trace 记机读全量字段——阶段起止、耗时、
    通道、真实/估算 token、现金/账面/估算成本、Gate1 各检查项、Verifier 发现明细、判决、开关快照。
    所有字符串经 redact() 脱敏（看板令牌）；单字段超过 20,000 字符截断并标 truncated。
    """
    try:
        os.makedirs(V4_TRACE_DIR, exist_ok=True)
        tok = read_token_mem()
        row = {"ts": iso_now(), "ts_ms": now_ms(), "kernel": KERNEL_VERSION, "event": event,
               "order_id": order_id, "run_id": _V4_RUN_CTX.get("run_id", ""), "pid": os.getpid()}

        def _clean(v):
            if isinstance(v, str):
                v = redact(v, tok) if tok else v
                return v if len(v) <= 20000 else (v[:20000] + f"...[truncated {len(v) - 20000} chars]")
            if isinstance(v, dict):
                return {str(k): _clean(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_clean(x) for x in v]
            if isinstance(v, (int, float, bool)) or v is None:
                return v
            return _clean(str(v))

        row.update({k: _clean(v) for k, v in fields.items()})
        path = os.path.join(V4_TRACE_DIR, f"trace-{_dt.datetime.now().strftime('%Y%m%d')}.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _res_digest(res: dict) -> dict:
    """阶段结果摘要（去 stdout，保留耗时/退出码/通道/网关差值/会话）。"""
    r = res or {}
    return {k: r.get(k) for k in ("role", "shift", "lane_selected", "fallback_from", "exit_code", "timed_out",
                                  "stall_timeout", "duration_sec", "gateway_delta", "direct_usage",
                                  "sessions", "task_title", "model_order")}


# ------------------------------------------------------------------ V4.0-P2 断言引擎（纯 Python 部分，可离线单测）
#
# 规格书末尾放一个 ```assertions 围栏块（JSON 数组，或 {"assertions": [...]}）。每条断言：
#   id        唯一编号（A1…）                         clause    对应规格条款（F1…），用于写基线
#   desc      人话说明（进账单/反馈）                  required  默认 true；false 的断言 UNVERIFIED 不阻断
#   state     进入什么状态再测：default | degraded（/data.json 回 404）| zone:N | drawer:<订单号>
#             | click:<CSS>，可用 "+" 串联，如 "zone:5+drawer:C-20261121-001"
#   viewports 子集 [1440, 1200, 390]，默认三档全测
#   selector  CSS 选择器（metric=doc.overflowX / js 时可省）；visible_only 默认 true（只算可见元素）
#   metric    count | rect.height|width|top|left|bottom|right | style.<css属性> | text | text.truncated
#             | text.ellipsis_ok | doc.overflowX | js（expr 字段给页面内表达式，返回值或数组）
#   aggregate first | max | min | sum | len | all | any（默认按 op 推断：<,<= 取 max；>,>= 取 min；布尔取 all）
#   op        < <= > >= == != contains not_contains matches not_matches
#   value     数字 / 字符串 / true|false / "baseline" / "baseline*0.82" / "baseline-10"
#             / "data:details[C-20261121-001].tasks|len"（从沙箱 /data.json 取值）
#   tolerance 数值比较容差（默认 0）            allow_empty  选择器 0 命中时视为 PASS（默认 false → UNVERIFIED）
#   template + params  引用 config/assertion_templates.json 的模板，显式字段覆盖模板字段

ASSERT_FENCE_RE = re.compile(r"```assertions[^\n]*\n(.*?)```", re.S)
ASSERT_VIEWPORTS = (1440, 1200, 390)
ASSERT_OPS = ("<", "<=", ">", ">=", "==", "!=", "contains", "not_contains", "matches", "not_matches")
ASSERT_AGGS = ("first", "max", "min", "sum", "len", "all", "any")
ASSERT_BOOL_METRICS = ("text.truncated", "text.ellipsis_ok")
ASSERT_NO_SELECTOR = ("doc.overflowX", "js")
ASSERT_RECT_KEYS = ("height", "width", "top", "left", "bottom", "right")
ASSERT_FIELDS = ("id", "clause", "desc", "required", "state", "viewports", "selector", "visible_only", "metric",
                 "expr", "aggregate", "op", "value", "tolerance", "allow_empty", "mask", "threshold", "full_page")
VISUAL_METRIC = "visual.diff_pct"
# 像素比对默认遮罩：时钟 / 实况流 / 活体点等每秒都在变的区域（v2.html 实测 id）
VISUAL_DEFAULT_MASK = ["#upd", "#coreVer", "#livedot", "#v3LivePollTime", "#v3LiveSessionInfo", "#v3LiveMetaSub", "#z13 tbody"]


def is_src_metric(m: str) -> bool:
    return bool(re.fullmatch(r"(?:lint|src)\.[a-z_]{2,40}", str(m or "")))
ASSERT_STATE_STEP_RE = re.compile(r"^(default|degraded|zone:\d{1,2}|drawer:[A-Za-z0-9_\-]{1,64}|click:.{1,200})$")
ASSERT_TEMPLATES_CANDIDATES = [
    os.path.join(STEWARD_ROOT, "config", "assertion_templates.json"),
    os.path.join(BP_ROOT, "assertion_templates.json"),
]


def load_assertion_templates() -> dict:
    for pth in ASSERT_TEMPLATES_CANDIDATES:
        if os.path.exists(pth):
            try:
                d = json.load(open(pth, "r", encoding="utf-8"))
                return d.get("templates") or {} if isinstance(d, dict) else {}
            except Exception:
                continue
    return {}


_TPL_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _fmt_deep(obj, params: dict):
    """模板 {参数} 替换（只认 {名字}，不走 str.format，杜绝 {x.attr} 式属性访问）。"""
    if isinstance(obj, str):
        missing = [m.group(1) for m in _TPL_PARAM_RE.finditer(obj) if m.group(1) not in params]
        if missing:
            raise ValueError(f"模板参数缺失: {sorted(set(missing))}")
        return _TPL_PARAM_RE.sub(lambda m: str(params[m.group(1)]), obj)
    if isinstance(obj, list):
        return [_fmt_deep(x, params) for x in obj]
    if isinstance(obj, dict):
        return {k: _fmt_deep(v, params) for k, v in obj.items()}
    return obj


def _coerce_scalar(v):
    if isinstance(v, str):
        t = v.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
            return float(t) if "." in t else int(t)
        if t in ("true", "false"):
            return t == "true"
    return v


def expand_assertion_template(a: dict, templates: dict) -> dict:
    name = a.get("template")
    if not name:
        return dict(a)
    tpl = templates.get(name)
    if not isinstance(tpl, dict):
        raise ValueError(f"未知断言模板: {name}")
    params = dict(tpl.get("defaults") or {})
    params.update(a.get("params") or {})
    base = _fmt_deep(tpl.get("assertion") or {}, params)
    for k in ("value", "tolerance"):
        if k in base:
            base[k] = _coerce_scalar(base[k])
    for k in ASSERT_FIELDS:
        if k in a:
            base[k] = a[k]
    base["template"] = name
    return base


def validate_assertion(a: dict) -> list[str]:
    errs = []
    aid = str(a.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{0,31}", aid):
        errs.append(f"断言 id 非法或缺失: {a.get('id')!r}")
    m = str(a.get("metric") or "")
    ok_metric = (m in ("count", "text", "doc.overflowX", "js", VISUAL_METRIC) or m in ASSERT_BOOL_METRICS or is_src_metric(m)
                 or (m.startswith("rect.") and m[5:] in ASSERT_RECT_KEYS)
                 or re.fullmatch(r"style\.[a-z][a-z\-]{1,40}", m or "") is not None)
    if not ok_metric:
        errs.append(f"{aid}: metric 不支持: {m!r}")
    if m not in ASSERT_NO_SELECTOR and m != VISUAL_METRIC and not is_src_metric(m) and not str(a.get("selector") or "").strip():
        errs.append(f"{aid}: 缺 selector")
    if m == VISUAL_METRIC:
        if not isinstance(a.get("value"), (int, float)) or isinstance(a.get("value"), bool):
            errs.append(f"{aid}: visual.diff_pct 的 value 必须是数字（允许的差异像素百分比，如 0.5）")
        if a.get("op") not in ("<", "<="):
            errs.append(f"{aid}: visual.diff_pct 只支持 op < / <=")
        if not isinstance(a.get("mask", []), list):
            errs.append(f"{aid}: mask 必须是选择器数组")
    if is_src_metric(m) and str(a.get("state") or "default") != "default":
        errs.append(f"{aid}: {m} 是源码指标，state 只能是 default")
    if m == "js" and not str(a.get("expr") or "").strip():
        errs.append(f"{aid}: metric=js 需要 expr")
    if a.get("op") not in ASSERT_OPS:
        errs.append(f"{aid}: op 不支持: {a.get('op')!r}")
    if "value" not in a:
        errs.append(f"{aid}: 缺 value")
    if a.get("aggregate") is not None and a.get("aggregate") not in ASSERT_AGGS:
        errs.append(f"{aid}: aggregate 不支持: {a.get('aggregate')!r}")
    vps = a.get("viewports")
    if not isinstance(vps, list) or not vps or any(v not in ASSERT_VIEWPORTS for v in vps):
        errs.append(f"{aid}: viewports 只能取 {list(ASSERT_VIEWPORTS)} 的非空子集")
    for step in str(a.get("state") or "default").split("+"):
        if not ASSERT_STATE_STEP_RE.match(step.strip()):
            errs.append(f"{aid}: state 步骤非法: {step!r}")
    val = a.get("value")
    if isinstance(val, str) and val.strip().startswith("baseline") and not re.fullmatch(r"baseline(\s*[*+\-/]\s*-?\d+(?:\.\d+)?)?", val.strip()):
        errs.append(f"{aid}: baseline 表达式只支持 baseline / baseline*k / baseline+k / baseline-k / baseline/k")
    if isinstance(val, str) and val.strip().startswith("baseline") and a.get("aggregate") in ("all", "any"):
        errs.append(f"{aid}: baseline 引用不能配 aggregate=all/any")
    return errs


def extract_assertions(spec_text: str, templates: dict | None = None) -> tuple[list[dict], list[str]]:
    """规格书 -> (已展开并补默认值的断言列表, 错误列表)。无断言块返回 ([], [])。"""
    blocks = ASSERT_FENCE_RE.findall(spec_text or "")
    if not blocks:
        return [], []
    tpls = load_assertion_templates() if templates is None else templates
    out: list[dict] = []
    errs: list[str] = []
    for bi, blk in enumerate(blocks, 1):
        try:
            data = json.loads(blk)
        except ValueError as exc:
            errs.append(f"断言块 #{bi} 不是合法 JSON: {exc}")
            continue
        items = data.get("assertions") if isinstance(data, dict) else data
        if not isinstance(items, list):
            errs.append(f"断言块 #{bi} 应为数组或 {{\"assertions\": [...]}}")
            continue
        for raw in items:
            if not isinstance(raw, dict):
                errs.append(f"断言块 #{bi} 含非对象条目")
                continue
            try:
                a = expand_assertion_template(raw, tpls)
            except ValueError as exc:
                errs.append(f"{raw.get('id')}: {exc}")
                continue
            a.setdefault("required", True)
            a.setdefault("state", "default")
            a.setdefault("viewports", [1440] if is_src_metric(a.get("metric")) else list(ASSERT_VIEWPORTS))
            if a.get("metric") == VISUAL_METRIC:
                a.setdefault("mask", list(VISUAL_DEFAULT_MASK))
                a.setdefault("threshold", 24)
                a.setdefault("full_page", True)
            a.setdefault("visible_only", True)
            a.setdefault("clause", "")
            a.setdefault("desc", "")
            a["state"] = "+".join(s.strip() for s in str(a["state"]).split("+"))
            e = validate_assertion(a)
            if e:
                errs.extend(e)
                continue
            out.append(a)
    ids = [a["id"] for a in out]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        errs.append(f"断言 id 重复: {dup}")
    return out, errs


def assertion_fence_text(spec_text: str) -> str:
    """原样取出规格书里全部 ```assertions 围栏（打回返工时继承给返工单）。"""
    return "\n\n".join(m.group(0) for m in re.finditer(r"```assertions[^\n]*\n.*?```", spec_text or "", re.S))


def assertion_needs_baseline(a: dict) -> bool:
    if a.get("metric") == VISUAL_METRIC:
        return True
    v = a.get("value")
    return isinstance(v, str) and v.strip().startswith("baseline")


def build_assertion_plan(assertions: list[dict]) -> dict:
    """按 state 分组：同组只导航/备状态一次，再依次切视口测量。"""
    groups: dict[str, dict] = {}
    for a in assertions:
        if is_src_metric(a.get("metric")):
            continue                                   # 源码指标在 Python 侧算，不进浏览器
        g = groups.setdefault(a["state"], {"state": a["state"], "viewports": [], "ids": [], "visual": [], "targets": ["sandbox"]})
        for vp in a["viewports"]:
            if vp not in g["viewports"]:
                g["viewports"].append(vp)
        if a.get("metric") == VISUAL_METRIC:
            g["visual"].append(a["id"])
        else:
            g["ids"].append(a["id"])
        if assertion_needs_baseline(a) and "baseline" not in g["targets"]:
            g["targets"].append("baseline")
    for g in groups.values():
        g["viewports"].sort(key=lambda v: ASSERT_VIEWPORTS.index(v))
    gl = list(groups.values())
    return {
        "groups": gl,
        "need_baseline": any("baseline" in g["targets"] for g in gl),
        "need_degraded": any("degraded" in g["state"].split("+") for g in gl),
        "defs": {a["id"]: {k: a.get(k) for k in ("id", "selector", "metric", "expr", "visible_only")} for a in assertions
                 if not is_src_metric(a.get("metric")) and a.get("metric") != VISUAL_METRIC},
        "visual_defs": {a["id"]: {"id": a["id"], "selector": a.get("selector") or "", "mask": a.get("mask") or [],
                                  "threshold": int(a.get("threshold") or 24), "full_page": bool(a.get("full_page", True))}
                        for a in assertions if a.get("metric") == VISUAL_METRIC},
        "need_src": any(is_src_metric(a.get("metric")) for a in assertions),
        "visual_count": sum(len(a["viewports"]) for a in assertions if a.get("metric") == VISUAL_METRIC),
    }


_DATA_TOK_RE = re.compile(r"\[([^\]]+)\]|([^.\[\]]+)")


def resolve_data_path(data, path: str):
    """"details[C-20261121-001].tasks|len" -> 值；路径不存在抛 KeyError。"""
    p = path.strip()
    post = ""
    if "|" in p:
        p, post = [x.strip() for x in p.split("|", 1)]
    cur = data
    for m in _DATA_TOK_RE.finditer(p):
        key = (m.group(1) or m.group(2) or "").strip()
        if key == "":
            continue
        if isinstance(cur, list):
            if not re.fullmatch(r"-?\d+", key):
                raise KeyError(f"列表只能用数字下标: {key}")
            cur = cur[int(key)]
        elif isinstance(cur, dict):
            if key not in cur:
                raise KeyError(f"数据路径不存在: {key}")
            cur = cur[key]
        else:
            raise KeyError(f"无法在 {type(cur).__name__} 上取 {key}")
    if post in ("len", "count"):
        return len(cur) if cur is not None else 0
    if post:
        raise KeyError(f"不支持的后缀: |{post}")
    return cur


def _baseline_expr(expr: str, base: float) -> float:
    m = re.fullmatch(r"baseline(?:\s*([*+\-/])\s*(-?\d+(?:\.\d+)?))?", expr.strip())
    if not m:
        raise ValueError(f"baseline 表达式非法: {expr}")
    if not m.group(1):
        return float(base)
    k = float(m.group(2))
    return {"*": base * k, "+": base + k, "-": base - k, "/": base / k if k else float("inf")}[m.group(1)]


def _as_num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", v):
        return float(v)
    return None


def default_aggregate(a: dict) -> str:
    if a.get("aggregate"):
        return a["aggregate"]
    if a.get("metric") == VISUAL_METRIC or is_src_metric(a.get("metric")):
        return "first"
    if a.get("metric") in ASSERT_BOOL_METRICS:
        return "all"
    if a.get("op") in ("<", "<="):
        return "max"
    if a.get("op") in (">", ">="):
        return "min"
    return "first"


def aggregate_values(vals: list, how: str):
    if how == "len":
        return len(vals)
    if not vals:
        raise ValueError("空集合")
    if how == "first":
        return vals[0]
    nums = [_as_num(v) for v in vals]
    if any(n is None for n in nums):
        raise ValueError("含非数值，无法做 " + how)
    return {"max": max(nums), "min": min(nums), "sum": sum(nums)}[how]


def compare_values(actual, op: str, expected, tol: float = 0.0) -> bool:
    if op in ("<", "<=", ">", ">="):
        x, y = _as_num(actual), _as_num(expected)
        if x is None or y is None:
            raise ValueError(f"数值比较需要数字（实测 {actual!r} / 期望 {expected!r}）")
        return {"<": x < y + tol, "<=": x <= y + tol, ">": x > y - tol, ">=": x >= y - tol}[op]
    if op in ("==", "!="):
        x, y = _as_num(actual), _as_num(expected)
        if x is not None and y is not None:
            eq = abs(x - y) <= tol
        elif isinstance(expected, bool) or isinstance(actual, bool):
            eq = bool(actual) == bool(expected) and isinstance(actual, bool) == isinstance(expected, bool)
        else:
            eq = str(actual) == str(expected)
        return eq if op == "==" else not eq
    s = "" if actual is None else str(actual)
    if op == "contains":
        return str(expected) in s
    if op == "not_contains":
        return str(expected) not in s
    if op == "matches":
        return re.search(str(expected), s) is not None
    if op == "not_matches":
        return re.search(str(expected), s) is None
    raise ValueError(f"未知 op {op}")


def _fmt_measure(v):
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, str) and len(v) > 80:
        return v[:80] + "…"
    return v


def evaluate_assertions(assertions: list[dict], raw: dict, data=None) -> list[dict]:
    """raw = 探针输出 {"targets": {"sandbox"|"baseline": {state: {vp: {aid: {values,n,samples}|{error}}}}},
    "errors": {"<target>|<state>": msg}, "fatal": msg}。返回每条断言的判定（PASS/FAIL/UNVERIFIED + 逐视口明细）。"""
    raw = raw or {}
    targets = raw.get("targets") or {}
    errors = raw.get("errors") or {}
    fatal = raw.get("fatal") or ""
    results = []
    for a in assertions:
        per_vp = []
        agg = default_aggregate(a)
        for vp in a["viewports"]:
            row = {"viewport": vp, "status": "UNVERIFIED", "measured": None, "expected": None, "n": 0, "reason": ""}
            try:
                if is_src_metric(a["metric"]):
                    src = (raw.get("src") or {})
                    if src.get("error"):
                        raise LookupError(f"源码指标计算失败: {src['error']}")
                    sv = (src.get("sandbox") or {}).get(a["metric"])
                    if sv is None:
                        raise LookupError(f"源码指标不存在: {a['metric']}")
                    exp = a["value"]
                    if assertion_needs_baseline(a):
                        bv = (src.get("baseline") or {}).get(a["metric"])
                        if bv is None:
                            raise LookupError("生产版源码指标缺失")
                        row["baseline"] = bv
                        exp = _baseline_expr(exp, float(bv))
                    row.update(measured=_fmt_measure(sv), expected=_fmt_measure(exp), n=1)
                    row["status"] = "PASS" if compare_values(sv, a["op"], exp, float(a.get("tolerance") or 0.0)) else "FAIL"
                    per_vp.append(row)
                    continue
                if a["metric"] == VISUAL_METRIC:
                    if fatal:
                        raise LookupError(fatal)
                    for tgt in ("sandbox", "baseline"):
                        e2 = errors.get(f"{tgt}|{a['state']}")
                        if e2:
                            raise LookupError(f"{'沙箱' if tgt == 'sandbox' else '生产版'}状态准备/截图失败: {e2}")
                    vm = (((raw.get("visual") or {}).get(a["state"]) or {}).get(str(vp)) or {}).get(a["id"])
                    if not isinstance(vm, dict):
                        raise LookupError("探针未返回像素比对结果")
                    if vm.get("error"):
                        raise LookupError(f"像素比对失败: {vm['error']}")
                    act = float(vm.get("diff_pct"))
                    row.update(measured=round(act, 3), expected=a["value"], n=int(vm.get("total_px") or 0))
                    row["visual"] = {k: vm.get(k) for k in ("diff_px", "total_px", "size_sandbox", "size_baseline", "bbox", "diff_png", "sandbox_png", "baseline_png")}
                    row["status"] = "PASS" if compare_values(act, a["op"], a["value"], float(a.get("tolerance") or 0.0)) else "FAIL"
                    per_vp.append(row)
                    continue
                if fatal:
                    raise LookupError(fatal)
                err = errors.get(f"sandbox|{a['state']}")
                if err:
                    raise LookupError(f"状态准备/测量失败: {err}")
                m = ((targets.get("sandbox") or {}).get(a["state"]) or {}).get(str(vp), {}).get(a["id"])
                if not isinstance(m, dict):
                    raise LookupError("探针未返回该断言的测量值")
                if m.get("error"):
                    raise LookupError(f"页面内测量报错: {m['error']}")
                vals = list(m.get("values") or [])
                row["n"] = int(m.get("n") or len(vals))
                if a["metric"] != "count" and a["metric"] not in ASSERT_NO_SELECTOR and not vals:
                    if a.get("allow_empty"):
                        row.update(status="PASS", reason="选择器 0 命中（allow_empty）")
                        per_vp.append(row)
                        continue
                    raise LookupError(f"选择器 0 命中: {a.get('selector')}")
                exp = a["value"]
                if isinstance(exp, str) and exp.startswith("data:"):
                    if data is None:
                        raise LookupError("沙箱 /data.json 不可用，无法解析 data: 引用")
                    try:
                        exp = resolve_data_path(data, exp[5:])
                    except (KeyError, IndexError, TypeError) as exc:
                        raise LookupError(f"data 引用解析失败: {exc}")
                elif assertion_needs_baseline(a):
                    b_err = errors.get("baseline|" + a["state"])
                    if b_err:
                        raise LookupError("基线版测量失败: " + str(b_err))
                    bm = ((targets.get("baseline") or {}).get(a["state"]) or {}).get(str(vp), {}).get(a["id"])
                    if not isinstance(bm, dict) or bm.get("error") or not (bm.get("values") or a["metric"] == "count"):
                        raise LookupError("基线版（主仓生产版）未测到该指标")
                    try:
                        bval = aggregate_values(list(bm.get("values") or []), agg)
                    except ValueError as exc:
                        raise LookupError(f"基线值聚合失败: {exc}")
                    row["baseline"] = _fmt_measure(bval)
                    exp = _baseline_expr(exp, float(bval))
                row["expected"] = _fmt_measure(exp)
                tol = float(a.get("tolerance") or 0.0)
                if agg in ("all", "any"):
                    oks = [compare_values(v, a["op"], exp, tol) for v in vals]
                    ok = all(oks) if agg == "all" else any(oks)
                    bad = [_fmt_measure(v) for v, o in zip(vals, oks) if not o][:3]
                    row["measured"] = {"n": len(vals), "pass": sum(oks), "fail_samples": bad}
                    if not ok and m.get("samples"):
                        row["samples"] = m["samples"][:3]
                else:
                    try:
                        act = aggregate_values(vals, agg)
                    except ValueError as exc:
                        raise LookupError(f"实测值聚合失败: {exc}")
                    row["measured"] = _fmt_measure(act)
                    ok = compare_values(act, a["op"], exp, tol)
                row["status"] = "PASS" if ok else "FAIL"
            except LookupError as exc:
                row["reason"] = str(exc)[:300]
            except ValueError as exc:
                row["reason"] = f"比较失败: {exc}"[:300]
            per_vp.append(row)
        sts = [r["status"] for r in per_vp]
        status = "FAIL" if "FAIL" in sts else ("UNVERIFIED" if "UNVERIFIED" in sts else "PASS")
        results.append({
            "id": a["id"], "clause": a.get("clause", ""), "desc": a.get("desc", ""), "required": bool(a.get("required", True)),
            "state": a["state"], "selector": a.get("selector", ""), "metric": a["metric"], "op": a["op"],
            "value": a["value"], "aggregate": agg, "status": status, "per_viewport": per_vp,
        })
    return results


def summarize_assertions(results: list[dict]) -> dict:
    return {
        "total": len(results),
        "pass": sum(1 for r in results if r["status"] == "PASS"),
        "fail": sum(1 for r in results if r["status"] == "FAIL"),
        "unverified": sum(1 for r in results if r["status"] == "UNVERIFIED"),
        "required_unverified": [r["id"] for r in results if r["status"] == "UNVERIFIED" and r["required"]],
        "failed_ids": [r["id"] for r in results if r["status"] == "FAIL"],
    }


def assertion_result_lines(results: list[dict], only: tuple = ("FAIL", "UNVERIFIED")) -> list[str]:
    lines = []
    for r in results:
        if r["status"] not in only:
            continue
        for v in r["per_viewport"]:
            if v["status"] == "PASS":
                continue
            what = f"{r['selector'] or r['metric']} · {r['metric']}" + (f" · {r['aggregate']}" if r["aggregate"] != "first" else "")
            base = f"（基线 {v['baseline']}）" if "baseline" in v else ""
            if v["status"] == "FAIL":
                lines.append(f"[断言 FAIL · {r['id']}/{r['clause'] or '-'}] {r['desc'] or what} @ {v['viewport']} 状态={r['state']}："
                             f"实测 {v['measured']}，期望 {r['op']} {v['expected']}{base}")
            else:
                lines.append(f"[断言 未验证 · {r['id']}/{r['clause'] or '-'}] {r['desc'] or what} @ {v['viewport']} 状态={r['state']}：{v['reason']}")
    return lines


def assertion_table_md(results: list[dict]) -> str:
    if not results:
        return ""
    rows = ["| 断言 | 条款 | 说明 | 状态 | 视口明细 |", "|---|---|---|---|---|"]
    for r in results:
        det = "；".join(
            f"{v['viewport']}:{v['status']}" + (f" 实测{v['measured']}/期望{r['op']}{v['expected']}" if v["status"] != "UNVERIFIED" else f"（{v['reason'][:60]}）")
            for v in r["per_viewport"])
        rows.append(f"| {r['id']} | {r['clause'] or '-'} | {(r['desc'] or r['metric'])[:40]} | **{r['status']}** | {det[:400]} |")
    return "\n".join(rows) + "\n"


def _assert_table_for_brief(probe_res: dict) -> str:
    """质检简报里的断言机检结果表。真引擎取证没取到（环境故障）时表是空的——直说，别让质检对着空表以为机检过了。"""
    ev = (probe_res.get("checks") or {}).get("runtime_evidence") or {}
    if ev and not ev.get("ran"):
        return "（本轮真引擎取证没取到：%s）\n" % str(ev.get("reason") or "未知原因")[:300]
    return assertion_table_md(probe_res.get("assertions") or [])


def assertions_brief_md(assertions: list[dict]) -> str:
    """给 Builder 的验收断言清单：Gate1 会逐条真机执行。"""
    if not assertions:
        return ""
    lines = ["## 本单机检断言（Gate1 将在真机上逐条执行，任一条 FAIL 即返工；请确保选择器与结构可被测到）", ""]
    for a in assertions:
        lines.append(f"- **{a['id']}**（{a.get('clause') or '-'}）{a.get('desc') or ''} —— 状态 `{a['state']}` · 视口 {a['viewports']} · "
                     f"`{a.get('selector') or a.get('expr') or ''}` · {a['metric']} {default_aggregate(a)} {a['op']} `{a['value']}`")
    return "\n".join(lines) + "\n"


def prior_confirmed_items(verifier_parsed: dict, attempt: int) -> list[dict]:
    """上一轮 confirmed P0/P1 → 销账清单（编号加轮次前缀 R<n>-F<k>，防与本轮新编号混淆）。"""
    items = []
    for fd in (verifier_parsed or {}).get("findings") or (verifier_parsed or {}).get("stageA") or []:
        if not isinstance(fd, dict):
            continue
        st = str(fd.get("status") or "confirmed").lower().strip()
        sev = str(fd.get("severity") or "P1").upper().strip()
        if st == "confirmed" and sev in ("P0", "P1"):
            items.append({"tag": f"R{attempt}-{fd.get('id') or 'F'}", "severity": sev,
                          "what": str(fd.get("what") or "")[:300], "fix": str(fd.get("fix") or "")[:300]})
    return items


def closeout_brief_md(prior: list[dict]) -> str:
    if not prior:
        return ""
    lines = ["## Stage C 销账（必答）：上一轮已确认缺陷逐条答复", "",
             "在 JSON 中增加 `\"closeout\": [{\"tag\": \"R1-F1\", \"status\": \"closed|still_open|regressed\", \"evidence\": \"引用本轮证据\"}]`，"
             "**下列每一条都必须答**；漏答一律按 still_open 计。still_open / regressed 的 P0 直接判 FAIL。", ""]
    for it in prior:
        lines.append(f"- `{it['tag']}` [{it['severity']}] {it['what']}")
    return "\n".join(lines) + "\n"


def apply_closeout(verifier_parsed: dict, prior: list[dict]) -> dict:
    """按销账结果改写机械判决；返回 {extra_p0, extra_p1, rows}，并就地更新 verifier_parsed。"""
    if not prior:
        return {"extra_p0": 0, "extra_p1": 0, "rows": []}
    answers = {}
    for c in (verifier_parsed or {}).get("closeout") or []:
        if isinstance(c, dict):
            t = str(c.get("tag") or c.get("id") or "").strip()
            if t:
                answers[t] = c
    rows, x0, x1 = [], 0, 0
    for it in prior:
        ans = answers.get(it["tag"]) or answers.get(it["tag"].split("-", 1)[-1])
        st = str((ans or {}).get("status") or "missing").lower().strip()
        if st not in ("closed", "still_open", "regressed"):
            st = "missing"
        open_ = st != "closed"
        if open_ and it["severity"] == "P0":
            x0 += 1
        elif open_ and it["severity"] == "P1":
            x1 += 1
        rows.append({"tag": it["tag"], "severity": it["severity"], "status": st, "evidence": str((ans or {}).get("evidence") or "")[:300]})
    if verifier_parsed.get("verdict") in ("PASS", "FAIL"):
        p0 = int(verifier_parsed.get("confirmed_p0") or 0) + x0
        p1 = int(verifier_parsed.get("confirmed_p1") or 0) + x1
        verifier_parsed["confirmed_p0_with_closeout"] = p0
        verifier_parsed["confirmed_p1_with_closeout"] = p1
        new_v = "FAIL" if (p0 >= 1 or p1 >= 3) else "PASS"
        if new_v != verifier_parsed["verdict"]:
            verifier_parsed["verdict_before_closeout"] = verifier_parsed["verdict"]
            verifier_parsed["verdict"] = new_v
        if x0 or x1:
            verifier_parsed.setdefault("defects", [])
            verifier_parsed["defects"] = list(verifier_parsed["defects"]) + [
                f"[销账未关 · {r['tag']} · {r['severity']}] {r['status']}" for r in rows if r["status"] != "closed"]
    verifier_parsed["closeout_rows"] = rows
    return {"extra_p0": x0, "extra_p1": x1, "rows": rows}


def orders_dir_for(order_id: str) -> str:
    return os.path.join(BP_ROOT, "orders", re.sub(r"-R\d+$", "", order_id))


def write_verified_baseline(order_id: str, results: list[dict], assertions: list[dict], ord_dir: str = "") -> dict:
    """V4.0-P2 只把「断言 PASS」写进基线（修 D-02）：
    - baseline.json items 追加 kind=verified_assertion（含断言定义与实测值，供后续回归回放）；
    - contract.json 中被已过断言覆盖（clause 命中 id/topic）的 active 条款标 done，其余标 done_unasserted。"""
    d = ord_dir or orders_dir_for(order_id)
    os.makedirs(d, exist_ok=True)
    defs = {a["id"]: a for a in assertions}
    passed = [r for r in results if r["status"] == "PASS"]
    passed_clauses = {str(r.get("clause") or "").strip() for r in passed if r.get("clause")}
    b_path = os.path.join(d, "baseline.json")
    try:
        b_obj = json.load(open(b_path, "r", encoding="utf-8")) if os.path.exists(b_path) else {}
    except Exception:
        b_obj = {}
    if not isinstance(b_obj, dict):
        b_obj = {}
    items = list(b_obj.get("items") or [])
    seen = {(it.get("order_id"), it.get("assertion_id")) for it in items if isinstance(it, dict)}
    added = 0
    for r in passed:
        key = (order_id, r["id"])
        if key in seen:
            continue
        meas = {str(v["viewport"]): v.get("measured") for v in r["per_viewport"]}
        items.append({
            "kind": "verified_assertion", "order_id": order_id, "assertion_id": r["id"], "clause": r.get("clause", ""),
            "summary": f"[{r['id']}/{r.get('clause') or '-'}] {r.get('desc') or r['metric']}（已机检 {r['op']} {r['value']}）",
            "assertion": {k: defs.get(r["id"], {}).get(k) for k in ASSERT_FIELDS},
            "measured": meas, "verified_at": iso_now(),
        })
        added += 1
    b_obj["items"] = items
    b_obj["order_id"] = b_obj.get("order_id") or re.sub(r"-R\d+$", "", order_id)
    b_obj["updated_at"] = iso_now()
    atomic_write_json(b_path, b_obj)
    c_path = os.path.join(d, "contract.json")
    marked = {"done": 0, "done_unasserted": 0}
    if os.path.exists(c_path):
        try:
            c_obj = json.load(open(c_path, "r", encoding="utf-8"))
            for cl in c_obj.get("clauses") or []:
                if cl.get("status") != "active":
                    continue
                hit = bool({str(cl.get("id") or ""), str(cl.get("topic") or "")} & passed_clauses)
                cl["status"] = "done" if hit else "done_unasserted"
                cl["closed_by_order"] = order_id
                cl["closed_at"] = iso_now()
                marked[cl["status"]] += 1
            c_obj["updated_at"] = iso_now()
            atomic_write_json(c_path, c_obj)
        except Exception as exc:
            marked["error"] = repr(exc)
    return {"baseline_added": added, "baseline_total": len(items), "contract": marked, "dir": d}


def resolve_job_assertions(job: dict, spec_text: str, q: dict | None = None) -> tuple[list[dict], list[str], str]:
    """本单断言：规格自带优先；否则沿 parent_order_id 继承原单规格的断言块。返回 (断言, 错误, 来源)。"""
    a, e = extract_assertions(spec_text)
    if a or e:
        return a, e, "spec"
    parent = job.get("parent_order_id")
    if parent and q:
        for h in reversed(q.get("history") or []):
            if h.get("order_id") == parent and h.get("spec_file") and os.path.exists(h["spec_file"]):
                try:
                    pa, pe = extract_assertions(open(h["spec_file"], "r", encoding="utf-8", errors="replace").read())
                except Exception:
                    continue
                if pa or pe:
                    return pa, pe, f"parent:{parent}"
    return [], [], ""


# ------------------------------------------------------------------ V4.0-P2 探针转义修复（V3 起 Gate1 部分检查失明）
#
# mjs_src 是 Python 普通三引号字符串，auditExpr 又是 JS 模板字符串：源码里的 "\\s" 到 Python 变 "\s"，
# 再经 JS 模板字符串 cooking 变成字母 "s"。结果（V3.2.1 实测还原）：
#   parseRgb 正则 /rgba?((d+),s*(d+)…)/ 永不匹配 → 对比度恒 21、body_bg_lum 恒取兜底深色 0.009；
#   订单号折行检测 /^(?:C-d{4,8}-…)$/ 永不匹配；可见文本 .replace(/s+/g,' ') 把每个字母 s 换成空格。
# 修复方式：运行时把 auditExpr 模板体内「非法转义」的反斜杠补一层（不改源码原文，开关可退）。

def rewrite_authorized(order_id: str, flags: dict, assertions: list[dict]) -> str:
    """决策 D4：受控重写授权。返回 "" / "ok" / 拒绝原因。必须在名单内且带至少 1 条必验 visual.diff_pct 断言。"""
    names = {str(x).strip() for x in (flags.get("rewrite_authorized_orders") or [])}
    base = re.sub(r"-R\d+$", "", order_id)
    if order_id not in names and base not in names:
        return ""
    if not any(a.get("metric") == VISUAL_METRIC and a.get("required", True) for a in assertions or []):
        return "在受控重写名单内，但规格缺必验 visual.diff_pct 断言——授权不生效"
    return "ok"


REWRITE_NOTE_BUILDER = ("\n## 受控重写授权（发起人决策 D4）\n"
                        "本单允许整理 / 重写呈现层源码（合并补丁层、统一 token、收敛断点、删冗余），**不受「禁从零重写」限制**；"
                        "但 /data.json 消费、fetch/poll、render 函数签名、data-zone 锚点、/api/v3/* 调用一律不得改。"
                        "验收以像素比对断言为准：与生产版逐像素差异超过阈值即返工。\n")
REWRITE_NOTE_VERIFIER = ("\n## 受控重写授权（发起人决策 D4）\n"
                         "本单已获受控重写授权：ui.md Q0 第 7 条「禁从零重写 / 整页 DOM 重建」**对本单豁免**，"
                         "旧类名/旧结构大面积变化不再判 P0；视觉零变化由 Gate1 像素比对断言（已附结果表）保证。其余 Q0 项照常。\n")

# C-20261126-SPD1（提速方案 P1-4）：派工单固定的少往返工作法。拼在 work_brief 的文件地图之后、规格正本之前。
# 实测依据：G60 第 2 轮 81 次模型调用 / 模型时间 1187s，其中首字等待 689s（58%），本地工具仅 121s。
BUILD_METHOD_NOTE = (
    "## 少往返工作法（每轮施工照此做）\n\n"
    "实测一轮施工 80–90 次模型调用，每次调用光等首字就要 5–9 秒，小步读改占了其中一大半。每一轮都按下面五条做：\n\n"
    "1. **开工一次读够**：目标文件、考卷点名的函数、相关测试，并进 1–2 条命令读完；无关目录不逛。\n"
    "2. **能并的读取并成一条命令**：多个文件一次输出、一次 grep 查多个词；不一次一条地试。\n"
    "3. **改动合并落盘**：同一文件的多处修改写成一个脚本一次落盘，改完不逐处回看；全部改完后统一看一次 diff。\n"
    "4. **验证一次做完**：语法检查 → 新增/相关测试 → 回归，连成一条命令跑完再看结果；失败先读报错再改，不盲目重试。\n"
    "5. **交卷一次写清**：回执附录一次性放 git status、diff 概览、测试原始输出。\n\n"
    "以做对为准：拿不准的地方先读再改，不许为了省调用跳过验证或漏做断言。\n"
)


def _src_metrics_pair(wt_target_path: str, main_target_path: str) -> dict:
    """V4.0-P3：沙箱版 / 主仓生产版 的源码指标（ui_lint.src_metrics）。"""
    if ui_lint is None:
        return {"error": "ui_lint 模块不可用（scripts/ui_lint.py 缺失）"}
    try:
        rules, _ = load_uispec_rules()
        out = {}
        for k, pth in (("sandbox", wt_target_path), ("baseline", main_target_path)):
            out[k] = ui_lint.src_metrics(open(pth, "r", encoding="utf-8", errors="replace").read(), rules or None)
        return out
    except Exception as exc:
        return {"error": repr(exc)[:300]}


_TPL_ESC_OK = set("ntrbfv0\\`$'\"\nux")
_G400_RGB = "rgb(148, 163, 184)"  # UISPEC --g-400：法定零值/空态/禁用灰


def fix_template_escapes(tpl_body: str) -> tuple[str, int]:
    out, i, n = [], 0, 0
    while i < len(tpl_body):
        c = tpl_body[i]
        if c == "\\" and i + 1 < len(tpl_body):
            nx = tpl_body[i + 1]
            if nx in _TPL_ESC_OK:
                out.append(tpl_body[i:i + 2])
            else:
                out.append("\\\\" + nx)
                n += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out), n


def fix_probe_template_escapes(mjs: str) -> tuple[str, dict]:
    """只处理 auditExpr 模板体；另给对比度统计加 g-400 豁免计数（UISPEC §1.1/§9 法定灰，决策 D8 待定）。"""
    info = {"escapes_fixed": 0, "g400_exempt_patch": False}
    k = mjs.find("const auditExpr = `")
    e = mjs.find("})()`;", k)
    if k < 0 or e < 0:
        info["error"] = "auditExpr not found"
        return mjs, info
    s0 = k + len("const auditExpr = `")
    body, n = fix_template_escapes(mjs[s0:e + len("})()")])
    info["escapes_fixed"] = n
    oid_old = "(?:C-\\\\d{4,8}-\\\\d{2,4}|"
    if oid_old in body:
        body = body.replace(oid_old, "(?:C-\\\\d{4,8}-G?\\\\d{2,4}|", 1)   # G 系列单号（C-20261122-G13）
        info["order_id_g_series"] = True
    old_min = "if (ratio < minContrast) minContrast = ratio;"
    old_req = "if (ratio < req && lowContrastSamples.length < 6) {"
    if old_min in body and old_req in body:
        body = body.replace(old_min, "const isG400 = cs.color === '" + _G400_RGB + "';\n      if (isG400) { window.__v4G400 = (window.__v4G400 || 0) + 1; }\n      if (ratio < minContrast && !isG400) minContrast = ratio;", 1)
        body = body.replace(old_req, "if (ratio < req && !isG400 && lowContrastSamples.length < 6) {", 1)
        info["g400_exempt_patch"] = True
    return mjs[:s0] + body + mjs[e + len("})()"):], info


def now_ts() -> float:
    return time.time()


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def ensure_dirs() -> None:
    for d in (os.path.dirname(QUEUE_PATH), INBOX_DIR, RUN_LOG_DIR):
        os.makedirs(d, exist_ok=True)


def read_token_mem() -> str:
    if not os.path.exists(TOKEN_FILE):
        return ""
    with open(TOKEN_FILE, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def redact(text: str, token: str) -> str:
    if not text or not token:
        return text
    return text.replace(token, "[REDACTED_TOKEN]")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path: str, data: dict | list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def atomic_write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, path)


def snapshot_six_pages() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in SIX_STATIC_PAGES:
        p = os.path.join(DASHBOARD_DIR, name)
        out[name] = sha256_file(p) if os.path.exists(p) else "MISSING"
    return out


# V5.0-real.6：后端合入 ≠ 上线。board_server 在请求处理函数里延迟 import 数据层（v3_data 等），模块缓存在进程里、
# 不重新加载；/v2.html 每次从盘上读（页面是新的），/data.json 却还是进程启动时那一版。2026-11-25 18:49 起的进程一直在用
# 旧数据层——之后合入的抽屉修复（G44R4–R7 等）单子「已交付」，看板访客却看不到，探针（自己现算 /data.json）也照不到。
# 内核不自己重启看板服务（守护面归 BoardGuard / 厂长），只在合入了看板服务加载的模块、且服务进程比盘上数据层还旧时留痕。
_BOARD_SERVER_CORE = ("board_server.py", "v3_data.py", "lane_status.py", "mes_board.py")
_BOARD_SERVER_MAX_SRC = 2 * 1024 * 1024


def board_server_loaded_modules(repo_root: str) -> set[str]:
    """看板服务会加载的 scripts/ 模块（小写文件名）：从 board_server.py 出发沿 import 走一遍（含函数里的延迟导入），
    外加一组写死的保底名。只认 scripts/ 下真有文件的模块，标准库 / 三方包不会混进来。"""
    scripts = os.path.join(repo_root, "scripts")
    names = set(_BOARD_SERVER_CORE)
    seen: set[str] = set()
    todo = ["board_server"]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = os.path.join(scripts, mod + ".py")
        if not os.path.isfile(path) or os.path.getsize(path) > _BOARD_SERVER_MAX_SRC:
            continue
        try:
            with open(path, "rb") as fh:
                tree = ast.parse(fh.read())
        except Exception:
            continue
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.append(node.module)
        for name in imported:
            head = name.split(".")[0]
            if os.path.isfile(os.path.join(scripts, head + ".py")):
                names.add((head + ".py").lower())
                todo.append(head)
    return names


def _board_server_procs(repo_root: str) -> list[dict]:
    """看板服务进程表：[{pid, started, in_repo}]（started = 本机时间 'YYYY-MM-DD HH:MM:SS'；in_repo = 命令行里跑的
    就是 repo_root 下的 board_server.py）。命令行只用来认人，不外传（里面可能带口令）。查不到 / 查不了 → 空表，不抛。"""
    out: list[dict] = []
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
              "Where-Object { $_.CommandLine -match 'board_server' } | "
              "ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.CreationDate.ToString('yyyy-MM-dd HH:mm:ss'), $_.CommandLine }")
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=90)
        want = os.path.normcase(os.path.join(repo_root, "scripts", "board_server.py"))
        for ln in (res.stdout or "").splitlines():
            m = re.match(r"\s*(\d+)\|(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\|(.*)$", ln)
            if m:
                out.append({"pid": int(m.group(1)), "started": m.group(2), "in_repo": want in os.path.normcase(m.group(3))})
    except Exception:
        return []
    return out


def merge_not_live_note(order_id: str, attempt: int, repo_root: str, targets) -> dict:
    """合入后核一件容易漏的事：这次合入了看板服务加载的模块，看板服务进程是不是比盘上的数据层还旧。
    只读（查进程启动时刻、看盘上文件时间），不动任何服务；查不了就不下结论（stale=None）。"""
    info: dict = {"order": order_id, "attempt": attempt, "checked": False, "stale": None, "files": []}
    try:
        if os.path.normcase(os.path.abspath(repo_root)) != os.path.normcase(os.path.abspath(STEWARD_ROOT)):
            return info                       # 合的不是看板服务跑的那个仓（档案仓等）：与本服务无关
        loaded = board_server_loaded_modules(repo_root)
        hit = []
        for t in (targets or []):
            rel = _norm_repo_path(t)
            if rel.rsplit("/", 1)[-1].lower() in loaded and os.path.isfile(os.path.join(repo_root, rel.replace("/", os.sep))):
                hit.append(rel)
        info["files"] = hit
        if not hit:
            return info
        info["checked"] = True
        newest = 0.0
        for name in loaded:
            p = os.path.join(repo_root, "scripts", name)
            if os.path.isfile(p):
                newest = max(newest, os.path.getmtime(p))
        info["data_layer_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest))
        procs = _board_server_procs(repo_root)
        same = [p for p in procs if p.get("in_repo")] or procs
        info["procs"] = len(procs)
        if not same:
            return info                       # 看板服务没在跑或认不出：不下结论（守护面自会处理）
        srv = max(same, key=lambda x: x["started"])
        info.update(pid=srv["pid"], started=srv["started"])
        info["stale"] = srv["started"] < info["data_layer_at"]
    except Exception as exc:  # noqa: BLE001
        info["error"] = repr(exc)[:200]
    return info


def note_merge_not_live(order_id: str, attempt: int, repo_root: str, targets) -> dict:
    """合入成功后调用：服务进程比盘上数据层旧 → 事件「合入未生效提醒」+ 巡检 [MERGE_NOT_LIVE]。任何情况不影响合入结论。"""
    try:
        nl = merge_not_live_note(order_id, attempt, repo_root, targets)
        if nl.get("checked"):
            v4_trace("merge_not_live", order_id, attempt=attempt, **{k: v for k, v in nl.items() if k not in ("order", "attempt")})
        if nl.get("stale"):
            msg = ("合入了看板服务加载的模块（%s），但看板服务进程自 %s 起没重启——这批后端改动对看板访客不可见；"
                   "重启看板服务后才生效（守护面归 BoardGuard / 厂长，内核不自己动）" % ("、".join(nl["files"]), nl["started"]))
            append_v3_event(order_id, attempt, "merge_not_live", "合入未生效提醒", msg)
            _append_patrol("[MERGE_NOT_LIVE %s] 单 %s：%s" % (iso_now(), order_id, msg))
        return nl
    except Exception:
        return {}


def _append_patrol(line: str) -> None:
    try:
        with open(PATROL_ALERT, "a", encoding="utf-8", newline="\n") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ Lock


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        res = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return bool(re.search(rf"\b{pid}\b", res.stdout or ""))
    except Exception:
        return False


def acquire_lock() -> bool:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    if os.path.exists(LOCK_PATH):
        try:
            old = json.load(open(LOCK_PATH, "r", encoding="utf-8"))
            old_pid = int(old.get("pid", 0))
            old_ts = float(old.get("ts", 0))
            if (now_ts() - old_ts) < LOCK_STALE_SEC and old_pid != os.getpid() and _pid_alive(old_pid):
                return False
        except Exception:
            pass
    atomic_write_json(LOCK_PATH, {"pid": os.getpid(), "ts": now_ts(), "at": iso_now()})
    return True


def release_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            old = json.load(open(LOCK_PATH, "r", encoding="utf-8"))
            if int(old.get("pid", 0)) == os.getpid():
                os.remove(LOCK_PATH)
    except Exception:
        pass


def refresh_lock() -> None:
    """Refresh runner lock timestamp at stage boundaries so active long jobs never look stale."""
    try:
        if os.path.exists(LOCK_PATH):
            old = json.load(open(LOCK_PATH, "r", encoding="utf-8"))
            if int(old.get("pid", 0)) == os.getpid():
                atomic_write_json(LOCK_PATH, {"pid": os.getpid(), "ts": now_ts(), "at": iso_now()})
    except Exception:
        pass
    if _LANE["order"]:                    # G40b：并行道持槽心跳（防长单被 stale 误回收）
        import slot_ledger
        slot_ledger.slot_heartbeat(_LANE["order"])


# ------------------------------------------------------------------ Budget Circuit Breaker



# ---- 预算治理 v2（c002 定调）：按通道 Token 月配额管配额资产，钱闸只管真金白银 ----
# 配额资产（月包）：R4 60亿 / R4-2 120亿 / STEP 400亿 / weekend-glm 5亿 / NEX 限时免费不设闸 / GLM 官方只走 ZCode 渠道不进网关
LANE_TOKEN_QUOTA_MONTH = {
    "r4": 6_000_000_000,
    "r42": 12_000_000_000,
    "step": 40_000_000_000,
    "weekend-glm": 500_000_000,
    "nex": None,       # 限时免费，token 不设闸
    "apigoto": None,   # 按量付费现金通道，走钱闸
}
LANE_FUSE_PCT = 0.90                 # 配额 90% 预警：换道分流，不停机
MONEY_LANE_PREFIX = ("apigoto",)     # 真金白银通道前缀


def load_budget_ledger() -> dict:
    d_today = today_str()
    blank = {
        "date": d_today,
        "daily_cost_cny": 0.0,
        "daily_cash_cny": 0.0,
        "month_lane_tokens": {},
        "daily_tokens_in": 0,
        "daily_tokens_out": 0,
        "daily_tokens_cached": 0,
        "daily_requests": 0,
        "daily_gateway_tokens": 0,
        "daily_weekend_glm_tokens": 0,
        "daily_weekend_glm_requests": 0,
        "lane_quota_warnings": [],
        "weekend_glm_consec_fails": 0,
        "weekend_glm_cooldown_until": 0.0,
        "weekend_glm_last_fail_reason": "",
        "circuit_open": False,
        "fuse_reason": "",
        "cards": {},
        # V4.0-P1：三口径分列（现金=熔断口径；账面=已上报 usage 计价；估算=上游未回 usage 的兜底）
        "schema_version": 2,
        "daily_est_cost_cny": 0.0,
        "daily_tokens_est": 0,
        "daily_est_requests": 0,
        "dry_run_fuse_events": [],
    }
    if not os.path.exists(BUDGET_LEDGER_PATH):
        return blank
    try:
        d = json.load(open(BUDGET_LEDGER_PATH, "r", encoding="utf-8"))
        if isinstance(d, dict) and d.get("date") == d_today:
            for k, v in blank.items():
                d.setdefault(k, v)
            return d
        if isinstance(d, dict):
            # 2026-11-25 修：跨日滚动只重置日计数，月包计数必须按月保留。
            # 旧行为把 month_lane_tokens 一并清零 → 月包 90% 熔断跨日永远不触发，
            # 且次日首笔写入会把盘上月累计覆盖成 0（实测盘上 11-24 仍留 11 月计数）。
            cur = (d.get("month_lane_tokens") or {}).get(_month_key())
            if isinstance(cur, dict):
                blank["month_lane_tokens"] = {
                    _month_key(): {str(k): int(v or 0) for k, v in cur.items()}}
    except Exception:
        pass
    return blank


def _lane_prefix(lane_id: str) -> str:
    s = str(lane_id or "").strip().lower()
    if s in LANE_TOKEN_QUOTA_MONTH:
        return s
    return s.split("-")[0]


def _month_key() -> str:
    return _dt.datetime.now().strftime("%Y-%m")


@_budget_serialized
def _record_weekend_glm_health(ok: bool, reason: str = "", severe: bool = False) -> dict:
    """Track consecutive weekend-glm instabilities and activate 20m cross-order cooldown when flaky."""
    ledger = load_budget_ledger()
    if ok:
        ledger["weekend_glm_consec_fails"] = 0
        ledger["weekend_glm_cooldown_until"] = 0.0
        ledger["weekend_glm_last_fail_reason"] = ""
    else:
        fails = int(ledger.get("weekend_glm_consec_fails") or 0) + 1
        ledger["weekend_glm_consec_fails"] = fails
        ledger["weekend_glm_last_fail_reason"] = str(reason or "")[:160]
        if severe or fails >= 2:
            ledger["weekend_glm_cooldown_until"] = round(now_ts() + WEEKEND_GLM_COOLDOWN_SEC, 2)
            _append_patrol(
                f"[V3_LANE_QUOTA {iso_now()}] weekend-glm 不稳定/拥堵 ({reason})，"
                f"触发 {WEEKEND_GLM_COOLDOWN_SEC // 60} 分钟跨单冷却熔断，冷却期内直走网关池（不停机）"
            )
    ledger["updated_at"] = iso_now()
    atomic_write_json(BUDGET_LEDGER_PATH, ledger)
    return ledger


_RE_WEEKEND_BAL = re.compile(r"billing/balance 请求完成 (\{.*\})\s*$")


def probe_weekend_glm(
    day: str | None = None,
    log_path: str | None = None,
    min_remaining_pct: float = WEEKEND_GLM_MIN_REMAINING_PCT,
    skip_cooldown: bool = False,
) -> dict:
    """Probe ZCode v2 daily log for weekend free ticket quota; never raises or blocks dispatch."""
    fallback = {
        "available": False,
        "lane": "gateway",
        "reason": "unknown",
        "remaining_pct": None,
        "provider_id": "",
        "model_id": "",
        "plan_id": "",
        "plan_name": "",
        "total_units": 0,
        "used_units": 0,
        "remaining_units": 0,
        "prior_recovered_err": "",
    }
    try:
        if not skip_cooldown:
            led_cd = load_budget_ledger()
            cd_until = float(led_cd.get("weekend_glm_cooldown_until") or 0.0)
            t_now_cd = now_ts()
            if cd_until > t_now_cd:
                return {**fallback, "reason": f"cooldown_active:{int(cd_until - t_now_cd)}s_left"}

        lp = log_path or os.environ.get("V3_WEEKEND_GLM_LOG_OVERRIDE") or os.path.join(ZCODE_V2_LOG_DIR, f"{day or today_str()}.log")
        if not lp or not os.path.isfile(lp):
            return {**fallback, "reason": "log_missing"}

        last_bal_idx = -1
        last_bal_data: dict | None = None
        last_err_idx = -1
        last_err_code = ""

        with open(lp, "r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh):
                if "coding_plan_account_changed" in line:
                    last_err_idx = idx
                    last_err_code = "coding_plan_account_changed"
                elif "project_token_scope_invalidated" in line:
                    last_err_idx = idx
                    last_err_code = "project_token_scope_invalidated"

                if ("[usage-stats]" in line or "[coding-plan-availability]" in line) and "billing/balance 请求完成" in line:
                    m = _RE_WEEKEND_BAL.search(line.rstrip())
                    if not m:
                        continue
                    try:
                        d = json.loads(m.group(1))
                    except Exception:
                        continue
                    if not isinstance(d, dict):
                        continue
                    if d.get("success") is False:
                        continue
                    code_v = d.get("code", 0)
                    if code_v not in (0, None):
                        continue
                    pdata = (d.get("payload") or {}).get("data") or {}
                    bals = d.get("balances") or pdata.get("balances") or []
                    if bals:
                        last_bal_idx = idx
                        last_bal_data = d

        if last_bal_data is None:
            return {**fallback, "reason": "no_balance_data"}

        if last_err_idx > last_bal_idx:
            return {**fallback, "reason": f"auth_error:{last_err_code}"}

        prior_err = last_err_code if (0 <= last_err_idx < last_bal_idx) else ""
        pdata = (last_bal_data.get("payload") or {}).get("data") or {}
        plans = last_bal_data.get("plans") or pdata.get("plans") or []
        balances = last_bal_data.get("balances") or pdata.get("balances") or []

        active_plans = [p for p in plans if isinstance(p, dict) and str(p.get("status") or "").lower() == "active"]
        if plans and not active_plans:
            return {**fallback, "reason": "no_active_plan", "prior_recovered_err": prior_err}

        t_now = now_ts()
        for obj in active_plans + balances:
            if not isinstance(obj, dict):
                continue
            for ek in ("ends_at", "expires_at", "period_end"):
                ev = obj.get(ek)
                if isinstance(ev, (int, float)) and ev > 0 and t_now >= float(ev):
                    return {**fallback, "reason": f"plan_expired:{ek}", "prior_recovered_err": prior_err}

        picked_bb = None
        for bb in balances:
            if isinstance(bb, dict) and int(bb.get("total_units") or 0) > 0:
                picked_bb = bb
                break
        if not picked_bb:
            return {**fallback, "reason": "zero_total_units", "prior_recovered_err": prior_err}

        total_units = int(picked_bb.get("total_units") or 0)
        used_units = int(picked_bb.get("used_units") or 0)
        rem_raw = picked_bb.get("remaining_units")
        if rem_raw is None:
            rem_raw = picked_bb.get("available_units")
        remaining_units = int(rem_raw) if rem_raw is not None else max(0, total_units - used_units)
        remaining_pct = round(remaining_units * 100.0 / total_units, 2) if total_units > 0 else 0.0

        provider_id = str(last_bal_data.get("providerId") or "account:bigmodel-start-plan").strip()
        model_id = str(picked_bb.get("show_name") or "").strip()
        if not model_id:
            for ap in active_plans:
                for ent in ap.get("entitlements") or []:
                    if isinstance(ent, dict) and ent.get("show_name"):
                        model_id = str(ent.get("show_name")).strip()
                        break
                if model_id:
                    break
        if not model_id:
            return {**fallback, "reason": "missing_model_id", "prior_recovered_err": prior_err}

        plan_src = active_plans[0] if active_plans else (plans[0] if plans else {})
        plan_id = str((plan_src or {}).get("plan_id") or picked_bb.get("plan_id") or "").strip()
        plan_name = str((plan_src or {}).get("name") or "").strip()

        ledger = load_budget_ledger()
        mk = _month_key()
        ledger_used = int(((ledger.get("month_lane_tokens") or {}).get(mk) or {}).get("weekend-glm", 0))
        used_ratio = (used_units / total_units) if total_units > 0 else 1.0
        base_info = {
            "provider_id": provider_id,
            "model_id": model_id,
            "plan_id": plan_id,
            "plan_name": plan_name,
            "total_units": total_units,
            "used_units": used_units,
            "remaining_units": remaining_units,
            "remaining_pct": remaining_pct,
            "prior_recovered_err": prior_err,
        }

        if ledger_used >= total_units * LANE_FUSE_PCT or used_ratio >= LANE_FUSE_PCT:
            _append_patrol(
                f"[V3_LANE_QUOTA {iso_now()}] weekend-glm 已触 {LANE_FUSE_PCT:.0%} 熔断线 "
                f"(ledger={ledger_used}, used={used_units}/{total_units}, rem={remaining_pct}%)，静默回落网关池（不停机）"
            )
            return {**base_info, "available": False, "lane": "gateway", "reason": "lane_quota_90pct_fuse"}

        if remaining_pct < min_remaining_pct:
            _append_patrol(
                f"[V3_LANE_QUOTA {iso_now()}] weekend-glm 剩余额度 {remaining_pct}% < {min_remaining_pct}%，回落网关池（不停机）"
            )
            return {**base_info, "available": False, "lane": "gateway", "reason": f"remaining_below_{min_remaining_pct}pct"}

        return {**base_info, "available": True, "lane": "weekend-glm", "reason": "ok"}
    except Exception as exc:
        return {**fallback, "reason": f"probe_error:{exc}"}


def _query_direct_lane_usage(session_ids: list[str], start_ms: int, end_ms: int, provider_id: str) -> dict:
    """Read direct provider (non-gateway) token usage from ZCode db.sqlite model_usage."""
    out = {
        "provider_id": provider_id,
        "requests": 0,
        "ok": 0,
        "fail": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
        "error_codes": [],
        "observed_providers": [],
    }
    if not provider_id or not os.path.exists(DB_SQLITE):
        return out
    try:
        uri = "file:" + DB_SQLITE.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        rows = []
        clean_sids = [str(s).strip() for s in (session_ids or []) if str(s or "").strip()]
        if clean_sids:
            ph = ",".join("?" for _ in clean_sids)
            rows = con.execute(
                f"SELECT provider_id, status, error_code, COALESCE(input_tokens,0), COALESCE(output_tokens,0), COALESCE(cache_read_input_tokens,0) "
                f"FROM model_usage WHERE provider_id=? AND session_id IN ({ph})",
                [provider_id] + clean_sids,
            ).fetchall()
            if not rows:
                all_rows = con.execute(
                    f"SELECT DISTINCT provider_id FROM model_usage WHERE session_id IN ({ph})",
                    clean_sids,
                ).fetchall()
                out["observed_providers"] = [str(r[0]) for r in all_rows if r and r[0]]
        else:
            rows = con.execute(
                "SELECT provider_id, status, error_code, COALESCE(input_tokens,0), COALESCE(output_tokens,0), COALESCE(cache_read_input_tokens,0) "
                "FROM model_usage WHERE provider_id=? AND started_at BETWEEN ? AND ?",
                (provider_id, int(start_ms) - 2000, int(end_ms) + 5000),
            ).fetchall()
        con.close()
        errs: list[str] = []
        for _pid, st, ec, tin, tout, tcache in rows:
            out["requests"] += 1
            if str(st or "").lower() == "completed":
                out["ok"] += 1
            else:
                out["fail"] += 1
            if ec and str(ec) not in errs:
                errs.append(str(ec))
            out["tokens_in"] += int(tin or 0)
            out["tokens_out"] += int(tout or 0)
            out["tokens_cached"] += int(tcache or 0)
        out["error_codes"] = errs
    except Exception:
        pass
    return out


@_budget_serialized
def record_stage_budget(
    order_id: str,
    gw_delta: dict,
    card_cost_so_far: float,
    direct_usage: dict | None = None,
    lane_quota_override: dict | None = None,
) -> tuple[dict, float, str]:
    """预算治理 v2：通道 Token 月配额闸 + 真金白银现金闸。

    - Token 闸: R4 60亿 / R4-2 120亿 / STEP 400亿 / weekend-glm 5亿 月包 90% 预警（NEX 免费、apigoto 走钱闸）
    - 现金闸: 仅统计 MONEY_LANE_PREFIX 通道，单卡 ¥100 / 全厂日 ¥300
    - 单通道配额触线：只记巡逻事件 [V3_LANE_QUOTA] 不停机（换道分流）；只有现金日闸才 circuit_open
    """
    ledger = load_budget_ledger()
    lanes = gw_delta.get("lanes") or {}

    stage_cash = 0.0
    mk = _month_key()
    for lid, dv in lanes.items():
        pfx = _lane_prefix(lid)
        tin = int(dv.get("tokens_in", 0) or 0)
        tout = int(dv.get("tokens_out", 0) or 0)
        if pfx in LANE_TOKEN_QUOTA_MONTH:
            lane_map = ledger.setdefault("month_lane_tokens", {}).setdefault(mk, {})
            lane_map[pfx] = int(lane_map.get(pfx, 0)) + tin + tout
        if pfx in MONEY_LANE_PREFIX:
            stage_cash += float(dv.get("cost_cny", 0.0) or 0.0)
    if not lanes and float(gw_delta.get("cost_cny", 0.0) or 0.0) > 0:
        # 旧格式无 lanes 明细：保守按网关窗口成本计现金（weekend-glm 零网关成本不计）
        stage_cash = float(gw_delta.get("cost_cny", 0.0) or 0.0)

    # 免费票 / 直连通道入账 month_lane_tokens 与 daily_weekend_glm_*，绝不入现金账
    if isinstance(direct_usage, dict) and (direct_usage.get("lane") == "weekend-glm" or int(direct_usage.get("tokens_in", 0) or 0) + int(direct_usage.get("tokens_out", 0) or 0) > 0):
        w_tok = int(direct_usage.get("tokens_in", 0) or 0) + int(direct_usage.get("tokens_out", 0) or 0)
        w_req = int(direct_usage.get("requests", 0) or 0)
        lane_key = str(direct_usage.get("lane") or "weekend-glm").strip().lower()
        lane_map = ledger.setdefault("month_lane_tokens", {}).setdefault(mk, {})
        lane_map[lane_key] = int(lane_map.get(lane_key, 0)) + w_tok
        if lane_key == "weekend-glm":
            ledger["daily_weekend_glm_tokens"] = int(ledger.get("daily_weekend_glm_tokens", 0)) + w_tok
            ledger["daily_weekend_glm_requests"] = int(ledger.get("daily_weekend_glm_requests", 0)) + w_req

    gw_in = int(gw_delta.get("tokens_in", 0) or 0)
    gw_out = int(gw_delta.get("tokens_out", 0) or 0)
    # 账面成本（网关全通道，报表口径）与现金成本（治理口径）分列
    ledger["daily_cost_cny"] = round(float(ledger.get("daily_cost_cny", 0.0)) + float(gw_delta.get("cost_cny", 0.0) or 0.0), 6)
    ledger["daily_cash_cny"] = round(float(ledger.get("daily_cash_cny", 0.0)) + stage_cash, 6)
    ledger["daily_tokens_in"] = int(ledger.get("daily_tokens_in", 0)) + gw_in
    ledger["daily_tokens_out"] = int(ledger.get("daily_tokens_out", 0)) + gw_out
    ledger["daily_gateway_tokens"] = int(ledger.get("daily_gateway_tokens", 0)) + gw_in + gw_out
    ledger["daily_tokens_cached"] = int(ledger.get("daily_tokens_cached", 0)) + int(gw_delta.get("tokens_cached", 0) or 0)
    ledger["daily_requests"] = int(ledger.get("daily_requests", 0)) + int(gw_delta.get("requests", 0) or 0)
    ledger["cards"][order_id] = round(float(ledger["cards"].get(order_id, 0.0)) + stage_cash, 6)
    ledger["schema_version"] = 2
    ledger["daily_est_cost_cny"] = round(float(ledger.get("daily_est_cost_cny", 0.0)) + float(gw_delta.get("cost_est_cny", 0.0) or 0.0), 6)
    ledger["daily_tokens_est"] = int(ledger.get("daily_tokens_est", 0)) + int(gw_delta.get("tokens_est_in", 0) or 0) + int(gw_delta.get("tokens_est_out", 0) or 0)
    ledger["daily_est_requests"] = int(ledger.get("daily_est_requests", 0)) + int(gw_delta.get("estimated_requests", 0) or 0)
    dry_run = fuse_dry_run_active(load_v4_flags())

    eff_quotas = dict(LANE_TOKEN_QUOTA_MONTH)
    if lane_quota_override:
        for k_ov, v_ov in lane_quota_override.items():
            if v_ov:
                eff_quotas[k_ov] = int(v_ov)

    fuse_reason = ""
    # 1) 通道 Token 月配额 90% 预警：通报+换道，绝不置 fuse_reason 停机
    warnings_list = ledger.setdefault("lane_quota_warnings", [])
    for pfx, quota in eff_quotas.items():
        if not quota:
            continue
        used = int((ledger.get("month_lane_tokens", {}).get(mk, {}) or {}).get(pfx, 0))
        if used >= quota * LANE_FUSE_PCT:
            warn_msg = f"通道 {pfx} 本月 Token {used} 已触及月包 {quota}*{LANE_FUSE_PCT:.0%}，换道分流（不停机）"
            _append_patrol(f"[V3_LANE_QUOTA {iso_now()}] {warn_msg}")
            if pfx not in warnings_list:
                warnings_list.append(pfx)
    # 2) 单卡现金闸
    new_card_cost = round(card_cost_so_far + stage_cash, 6)
    if new_card_cost > CARD_BUDGET_CAP_CNY:
        fuse_reason = f"订单 {order_id} 现金通道累计 ¥{new_card_cost:.4f} 超过 ¥{CARD_BUDGET_CAP_CNY:.2f} 单卡钱闸"
        _append_patrol(f"[V3_CARD_FUSE {iso_now()}] {fuse_reason}")
    # 3) 全厂现金日闸：唯一整线停机触发器
    if float(ledger.get("daily_cash_cny", 0.0)) > DAILY_BUDGET_CAP_CNY:
        fuse_reason = f"全厂当日现金通道花费 ¥{ledger['daily_cash_cny']:.4f} 超过 ¥{DAILY_BUDGET_CAP_CNY:.2f} 整线停机熔断线"
        if not dry_run:
            ledger["circuit_open"] = True
            ledger["fuse_reason"] = fuse_reason
        _append_patrol(f"[V3_DAILY_FUSE {iso_now()}] {fuse_reason}")
    # V4.0-P1 观察期：熔断只告警、不停机（计量修复后按真实数据重定熔断线，决策 D2）
    if fuse_reason and dry_run:
        ledger.setdefault("dry_run_fuse_events", []).append({"at": iso_now(), "order_id": order_id, "reason": fuse_reason})
        _append_patrol(f"[V4_FUSE_DRYRUN {iso_now()}] 观察期内熔断不生效：{fuse_reason}")
        v4_trace("fuse_dry_run", order_id, reason=fuse_reason, card_cash=new_card_cost,
                 daily_cash=ledger.get("daily_cash_cny"))
        fuse_reason = ""
    v4_trace("budget_stage", order_id, stage_cash=round(stage_cash, 6), card_cash=new_card_cost,
             gw_cost=float(gw_delta.get("cost_cny", 0.0) or 0.0), gw_cost_est=float(gw_delta.get("cost_est_cny", 0.0) or 0.0),
             tokens_in=gw_in, tokens_out=gw_out, tokens_est_in=int(gw_delta.get("tokens_est_in", 0) or 0),
             tokens_est_out=int(gw_delta.get("tokens_est_out", 0) or 0), lanes=lanes, direct_usage=direct_usage,
             daily_cash=ledger.get("daily_cash_cny"), daily_cost=ledger.get("daily_cost_cny"),
             daily_est_cost=ledger.get("daily_est_cost_cny"), dry_run=dry_run)

    ledger["updated_at"] = iso_now()
    atomic_write_json(BUDGET_LEDGER_PATH, ledger)
    return ledger, new_card_cost, fuse_reason


# ------------------------------------------------------------------ Provider & ZCode


def find_builtin_provider_cfg() -> str:
    if not os.path.isdir(RUNTIME_PROVIDER_ROOT):
        return ""
    dirs = sorted(
        [d for d in os.listdir(RUNTIME_PROVIDER_ROOT) if os.path.isdir(os.path.join(RUNTIME_PROVIDER_ROOT, d))],
        reverse=True,
    )
    for d in dirs:
        for root, _, files in os.walk(os.path.join(RUNTIME_PROVIDER_ROOT, d)):
            if "zcode-builtin.json" in files:
                return os.path.join(root, "zcode-builtin.json")
    return ""


def write_provider_config(role: str) -> tuple[str, list[str]]:
    hour = _dt.datetime.now().hour
    is_night = hour >= 22 or hour < 9
    if is_night:
        shift = "night"
        model_order = ["auto-night", "auto-cheap", "auto", "auto-heavy", "auto-reason"]
        if role == "verifier":
            # vthink2: 夜间verifier不押auto-night(深思螺旋烧穿22K输出帽,02:08双证) -> 押auto
            model_order = ["auto", "auto-heavy", "auto-cheap", "auto-night", "auto-reason"]
    elif role == "worker":
        shift = "day"
        model_order = ["auto", "auto-heavy", "auto-cheap", "auto-night", "auto-reason"]
    else:
        shift = "day"
        model_order = ["auto", "auto-heavy", "auto-cheap", "auto-night", "auto-reason"]

    model_rules = [
        {
            "providerId": "new-provider-7",
            "modelId": mid,
            "config": {
                "enabled": True,
                "properties": {
                    "contextWindow": 200000,
                    "inputFormat": {"supportsImage": True},
                },
            },
        }
        for mid in model_order
    ]
    cfg = {
        "schemaVersion": 1,
        "config": {
            "providerOrder": ["new-provider-7"],
            "providerConfigRules": {
                "providerRules": [
                    {
                        "providerId": "new-provider-7",
                        "providerName": "Steward Auto",
                        "config": {
                            "group": "standard-personal",
                            "access": {"type": "api-key", "apiKey": "local-gateway"},
                            "api": {"type": "openai-chat-completions", "baseUrl": "http://127.0.0.1:8788/v1"},
                            "personalModelIds": model_order,
                            "modelOrder": model_order,
                        },
                    }
                ]
            },
            "modelConfigRules": {
                "providerModelRules": model_rules,
                "manualProviderModelRules": [],
            },
            "defaultModelSelection": {
                "providerId": "new-provider-7",
                "modelId": model_order[0],
            },
        },
    }
    atomic_write_json(PROVIDER_CFG_PATH, cfg)
    return shift, model_order


# 2026-11-26 直连模式：工厂不再把施工/质检送进本地 8788 网关，直接用 ZCode 本机
# provider_config.json 里已经配好的 provider/model。网关只保留给其他客户端，不参与派单。
_DIRECT_LANES = (
    ("R4", "r4", "new-provider-6", "deepseek-v4.1-flash"),
    ("R4-2", "r42", "new-provider-11", "deepseek-v4.1-flash"),
    ("STEP", "step", "new-provider-10", "step-5-preview"),   # 首选 step-5-preview：夜班实测快、有交付记录；step-3.7-flash 没验证过
    ("NEX", "nex", "new-provider-5", "nex-n2.6"),
)


def _global_provider_model_lists() -> dict[str, list[str]]:
    """ZCode 本机已配置 provider 的模型表（只读 modelOrder，不碰 apiKey/baseUrl）。"""
    paths = [
        r"C:\Users\Administrator\.zcode\v2\provider_config.json",
        os.path.join(os.path.expanduser("~"), ".zcode", "v2", "provider_config.json"),
    ]
    for p in paths:
        try:
            pc = json.load(open(p, "r", encoding="utf-8"))
            rules = (((pc.get("config") or {}).get("providerConfigRules") or {}).get("providerRules") or [])
            out = {}
            for pr in rules:
                pid = str(pr.get("providerId") or "")
                c = pr.get("config") or {}
                mods = c.get("modelOrder") or c.get("personalModelIds") or []
                if pid and mods:
                    out[pid] = [str(m) for m in mods]
            if out:
                return out
        except Exception:
            continue
    return {}


def _global_provider_models() -> dict[str, str]:
    """ZCode 本机已配置 provider 的首选模型名 = 模型表的第一个。"""
    return {pid: mods[0] for pid, mods in _global_provider_model_lists().items() if mods}


# 2026-11-26 换线 A/B（提速方案 P0-1）：白天施工隔单交替「STEP 先 / R4 先」，同一张单各轮次同一臂。
# 依据：同时段 step 首字比 r4 快 2–4 秒/次；白天 step 的施工质量没有数据，所以交替着跑、各满 10 单再定去留。
# 臂记在 state/direct_ab.json（运行态，不进主干）。读写出任何问题都退回原顺序（R4 先），A/B 不能挡派单。
DIRECT_AB_PATH = os.path.join(STEWARD_ROOT, "state", "direct_ab.json")
_DIRECT_AB_LOCK = threading.Lock()
_DIRECT_AB_ARMS: dict[str, str] = {}


def _direct_ab_arm(order_id: str) -> str:
    """这张单的臂：'step-first' 或 'r4-first'。按单号记住（重排队不翻臂），新单号按已登记张数交替，第一张 STEP 先。"""
    if not order_id:
        return "r4-first"
    with _DIRECT_AB_LOCK:
        cached = _DIRECT_AB_ARMS.get(order_id)
        if cached in ("step-first", "r4-first"):
            return cached
        arms: dict = {}
        try:
            if os.path.isfile(DIRECT_AB_PATH):
                data = json.load(open(DIRECT_AB_PATH, encoding="utf-8"))
                arms = dict(data.get("orders") or {}) if isinstance(data, dict) else {}
        except Exception:
            arms = {}
        arms.update(_DIRECT_AB_ARMS)
        arm = arms.get(order_id)
        if arm not in ("step-first", "r4-first"):
            arm = "r4-first" if (len(arms) % 2) else "step-first"
        arm = str(arm)
        _DIRECT_AB_ARMS[order_id] = arm
        arms[order_id] = arm
        try:
            tmp = "%s.%d.tmp" % (DIRECT_AB_PATH, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"orders": arms}, fh, ensure_ascii=False)
            os.replace(tmp, DIRECT_AB_PATH)
        except Exception:
            pass
        return arm


def _direct_lane_pool(role: str, simple: bool = False, ab: str = "") -> list[dict]:
    """直连择道池。免费 NEX 放最后兜底；小单先试 NEX，失败下一阶段自动换道。
    白天普通施工单按 ab（隔单交替的臂）定 STEP 先还是 R4 先；夜班、质检、小单不参加 A/B。"""
    models = _global_provider_model_lists()
    hour = _dt.datetime.now().hour
    night = hour >= 22 or hour < 9
    if role == "verifier":
        seq = ("R4-2", "R4", "STEP", "NEX")
    elif night:
        seq = ("STEP", "R4", "R4-2", "NEX")
    elif simple:
        seq = ("NEX", "R4", "R4-2", "STEP")
    elif ab == "step-first":
        seq = ("STEP", "R4", "R4-2", "NEX")
    else:
        seq = ("R4", "R4-2", "STEP", "NEX")
    by_name = {name: (lane_id, pid, default_mid) for name, lane_id, pid, default_mid in _DIRECT_LANES}
    pool = []
    for name in seq:
        lane_id, pid, default_mid = by_name[name]
        pool.append({
            "lane": lane_id,
            "provider_id": pid,
            # 首选模型在该 provider 的模型表里就用它，不在才退回模型表第一个；模型表读不到用首选名兜底
            "model_id": default_mid if (default_mid in (models.get(pid) or []) or not models.get(pid)) else (models[pid][0] or default_mid),
            "direct": True,
            "shift": "night" if night else "day",
        })
    return pool


def _global_provider_rules() -> dict[str, dict]:
    """providerId -> 本机 ZCode provider rule（仅内存里用；写临时配置时才落盘）。"""
    paths = [
        r"C:\Users\Administrator\.zcode\v2\provider_config.json",
        os.path.join(os.path.expanduser("~"), ".zcode", "v2", "provider_config.json"),
    ]
    for p in paths:
        try:
            pc = json.load(open(p, "r", encoding="utf-8"))
            rules = (((pc.get("config") or {}).get("providerConfigRules") or {}).get("providerRules") or [])
            out = {str(r.get("providerId")): r for r in rules if r.get("providerId")}
            if out:
                return out
        except Exception:
            continue
    return {}


def write_direct_provider_config(provider_id: str, model_id: str) -> str:
    """把 ZCode 本机配置里的目标 provider 复制成一次性 personal config（含密钥，只落临时目录）。

    返回临时文件路径；调用方在子进程结束后删除。失败返回 ""，退回 ZCode 全局配置。
    """
    rule = _global_provider_rules().get(str(provider_id))
    if not rule:
        print("[factory_runner] direct provider %s not found in ZCode config; fallback to global default" % provider_id)
        return ""
    try:
        rule = json.loads(json.dumps(rule, ensure_ascii=False))
        rc = rule.setdefault("config", {})
        mods = rc.get("modelOrder") or rc.get("personalModelIds") or []
        if model_id not in mods:
            mods = [model_id] + list(mods)
        rc["modelOrder"] = list(mods)
        rc["personalModelIds"] = list(mods)
        cfg = {
            "schemaVersion": 1,
            "config": {
                "providerOrder": [provider_id],
                "providerConfigRules": {"providerRules": [rule]},
                "modelConfigRules": {
                    "providerModelRules": [{
                        "providerId": provider_id,
                        "modelId": model_id,
                        "config": {
                            "enabled": True,
                            "properties": {
                                "contextWindow": 200000,
                                "inputFormat": {"supportsImage": True},
                            },
                        },
                    }],
                    "manualProviderModelRules": [],
                },
                "defaultModelSelection": {"providerId": provider_id, "modelId": model_id},
            },
        }
        fd, path = tempfile.mkstemp(prefix="zcode-direct-", suffix=".json")
        os.close(fd)
        atomic_write_json(path, cfg)
        return path
    except Exception:
        return ""


def get_gateway_status() -> dict:
    try:
        with urllib.request.urlopen(GATEWAY_STATUS_URL, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return {}


def gateway_delta(before: dict, after: dict) -> dict:
    bt = before.get("totals") or {}
    at = after.get("totals") or {}
    keys = ("requests", "ok", "fail", "tokens_in", "tokens_out", "tokens_cached")
    out = {k: int(at.get(k, 0)) - int(bt.get(k, 0)) for k in keys}
    out["cost_cny"] = round(float(at.get("cost_cny", 0.0)) - float(bt.get("cost_cny", 0.0)), 6)
    # V4.0-P1：网关 usage_schema=2 起有估算分列；老网关无这些键 → 差值为 0
    for k in ("tokens_est_in", "tokens_est_out", "estimated_requests"):
        out[k] = int(at.get(k, 0) or 0) - int(bt.get(k, 0) or 0)
    out["cost_est_cny"] = round(float(at.get("cost_est_cny", 0.0) or 0.0) - float(bt.get("cost_est_cny", 0.0) or 0.0), 6)
    lane_before = {l.get("id"): l for l in (before.get("lanes") or []) if l.get("id")}
    lane_deltas = {}
    for l in after.get("lanes") or []:
        lid = l.get("id")
        if not lid:
            continue
        prev = lane_before.get(lid) or {}
        d_ok = int(l.get("ok", 0)) - int(prev.get("ok", 0))
        d_fail = int(l.get("fail", 0)) - int(prev.get("fail", 0))
        d_in = int((l.get("tokens") or [0, 0])[0]) - int((prev.get("tokens") or [0, 0])[0])
        d_out = int((l.get("tokens") or [0, 0])[1]) - int((prev.get("tokens") or [0, 0])[1])
        d_cost = round(float(l.get("cost_cny", 0.0)) - float(prev.get("cost_cny", 0.0)), 6)
        if d_ok or d_fail or d_in or d_out:
            lane_deltas[lid] = {"ok": d_ok, "fail": d_fail, "tokens_in": d_in, "tokens_out": d_out, "cost_cny": d_cost}
    out["lanes"] = lane_deltas
    return out


def detect_sessions_in_window(start_ms: int, end_ms: int) -> list[dict]:
    if not os.path.exists(DB_SQLITE):
        return []
    found: list[dict] = []
    try:
        uri = "file:" + DB_SQLITE.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, title, directory, time_created, time_updated "
            "FROM session WHERE time_created BETWEEN ? AND ? ORDER BY time_created DESC",
            (start_ms - 3000, end_ms + 3000),
        ).fetchall()
        msg_cols = [r[1] for r in cur.execute("PRAGMA table_info(message)").fetchall()]
        for sid, title, directory, tc, tu in rows:
            info = {"session_id": sid, "title": (title or "")[:120], "time_created": tc, "time_updated": tu}
            if "data" in msg_cols:
                mrows = cur.execute(
                    "SELECT data FROM message WHERE session_id=? ORDER BY time_created DESC LIMIT 6",
                    (sid,),
                ).fetchall()
                models_seen = []
                for (raw,) in mrows:
                    try:
                        md = json.loads(raw)
                        p = md.get("providerID") or md.get("providerId") or (md.get("model") or {}).get("providerID")
                        m = md.get("modelID") or md.get("modelId") or (md.get("model") or {}).get("modelID")
                        if p or m:
                            tag = f"{p}/{m}"
                            if tag not in models_seen:
                                models_seen.append(tag)
                    except Exception:
                        pass
                if models_seen:
                    info["models"] = models_seen
            found.append(info)
        con.close()
    except Exception:
        pass
    return found


def _desktop_workspace_candidates(ti_con: sqlite3.Connection, fallback_dir: str) -> list[tuple[str, str | None]]:
    """Return active ZCode Desktop workspace roots and project_ids ordered by priority (always mate\\lib first)."""
    primary = (r"C:\Users\Administrator\Documents\mate\lib", "00000000-0000-0000-0000-000000000001")
    secondary = (r"C:\Users\Administrator\Documents\mate", "00000000-0000-0000-0000-000000000002")
    out: list[tuple[str, str | None]] = [primary]
    seen: set[str] = {os.path.normcase(primary[0])}
    try:
        rows = ti_con.execute(
            "SELECT pr.root_path, p.project_id FROM project_roots pr "
            "JOIN projects p ON p.project_id = pr.project_id "
            "WHERE COALESCE(p.deleted, 0)=0 ORDER BY p.position ASC, pr.is_primary DESC"
        ).fetchall()
        by_norm = {os.path.normcase(r[0]): (r[0], r[1]) for r in rows if r[0]}
        if os.path.normcase(primary[0]) in by_norm:
            out[0] = by_norm[os.path.normcase(primary[0])]
        for cand in (secondary[0], fallback_dir):
            nc = os.path.normcase(cand) if cand else ""
            if nc and nc in by_norm and nc not in seen:
                out.append(by_norm[nc])
                seen.add(nc)
    except Exception:
        pass
    return out


def sync_session_to_tasks_index(
    session_id: str,
    title: str,
    status: str = "running",
    wt_path: str = "",
    model_hint: str = "",
) -> bool:
    """Register/update a CLI or Desktop session with host_id='local' and group ordering so ZCode Desktop shows it."""
    if not session_id:
        return False
    now_t = now_ms()
    s_title = title.strip() or f"【V4】{session_id[:16]}"
    s_created = now_t
    s_updated = now_t
    s_dir = MATE_ROOT
    s_trace = str(uuid.uuid4())
    s_model = model_hint or "new-provider-7/auto"
    s_search = s_title
    s_outcome = "completed" if status == "completed" else None

    if not os.path.exists(TASKS_INDEX_SQLITE):
        return False
    ws_path = r"C:\Users\Administrator\Documents\mate\lib"
    proj_id = "00000000-0000-0000-0000-000000000001"
    try:
        ti_c = sqlite3.connect(TASKS_INDEX_SQLITE, timeout=10)
        ti_c.execute("PRAGMA busy_timeout=5000")
        cands = _desktop_workspace_candidates(ti_c, fallback_dir=s_dir)
        ws_path, proj_id = cands[0]
        ti_c.close()
    except Exception:
        pass
    cli_proj_id = "proj_" + re.sub(r"[^a-z0-9]+", "-", ws_path.lower()).strip("-")

    if os.path.exists(DB_SQLITE):
        try:
            con = sqlite3.connect(DB_SQLITE, timeout=10)
            con.execute("PRAGMA busy_timeout=5000")
            cur = con.cursor()
            row = cur.execute(
                "SELECT directory, time_created, time_updated, trace_id, last_outcome FROM session WHERE id=?",
                (session_id,),
            ).fetchone()
            if row:
                s_dir = row[0] or s_dir
                s_created = int(row[1] or now_t)
                s_updated = max(now_t, int(row[2] or now_t))
                if row[3]:
                    s_trace = row[3]
                if row[4]:
                    s_outcome = row[4]
                prow = cur.execute(
                    "SELECT substr(json_extract(data, '$.text'), 1, 600) FROM part WHERE session_id=? "
                    "AND json_extract(data, '$.type')='text' ORDER BY rowid ASC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if prow and prow[0]:
                    s_search = prow[0]
                cur.execute(
                    "UPDATE session SET title=?, title_source='custom', time_title_updated=?, directory=?, path=?, project_id=? WHERE id=?",
                    (s_title, now_t, ws_path, ws_path, cli_proj_id, session_id),
                )
                con.commit()
            con.close()
        except Exception:
            pass

    try:
        ti = sqlite3.connect(TASKS_INDEX_SQLITE, timeout=10)
        ti.execute("PRAGMA busy_timeout=5000")
        meta_obj = {
            "taskId": session_id,
            "traceId": s_trace,
            "title": s_title,
            "titleOverridden": True,
            "workspacePath": ws_path,
            "createdAt": s_created,
            "updatedAt": s_updated,
            "mode": "yolo",
            "model": s_model,
            "thoughtLevel": "max",
            "provider": "glm",
            "status": status,
            "target": None,
        }
        ti.execute(
            """
            INSERT INTO tasks (
                workspace_key, workspace_path, workspace_identity, task_id, title,
                task_status, provider, mode, model, migration_source, forked_from_task_id,
                created_at, updated_at, unread_at, last_unread_at, pinned, archived, deleted,
                title_overridden, meta_json, searchable_text, host_id, root_path, project_id,
                cli_title, cli_title_source, cli_created_at, cli_updated_at,
                cli_last_outcome, cli_deleted, worktree_root
            ) VALUES (
                ?, ?, NULL, ?, ?,
                ?, 'glm', 'yolo', ?, NULL, NULL,
                ?, ?, ?, ?, 0, 0, 0,
                1, ?, ?, 'local', ?, ?,
                ?, 'custom', ?, ?,
                ?, 0, NULL
            )
            ON CONFLICT(workspace_key, task_id) DO UPDATE SET
                title = excluded.title,
                cli_title = excluded.cli_title,
                title_overridden = 1,
                task_status = excluded.task_status,
                model = excluded.model,
                host_id = 'local',
                project_id = COALESCE(excluded.project_id, tasks.project_id),
                root_path = COALESCE(excluded.root_path, tasks.root_path),
                updated_at = excluded.updated_at,
                cli_created_at = COALESCE(tasks.cli_created_at, excluded.cli_created_at),
                cli_updated_at = excluded.cli_updated_at,
                cli_last_outcome = COALESCE(excluded.cli_last_outcome, tasks.cli_last_outcome),
                meta_json = excluded.meta_json,
                worktree_root = NULL
            """,
            (
                ws_path, ws_path, session_id, s_title,
                status, s_model,
                s_created, s_updated, s_updated if status == "completed" else None, s_updated,
                json.dumps(meta_obj, ensure_ascii=False), s_search, ws_path, proj_id,
                s_title, s_created, s_updated,
                s_outcome,
            ),
        )
        ti.execute("DELETE FROM tasks WHERE task_id=? AND workspace_key<>?", (session_id, ws_path))
        ti.execute(
            "INSERT OR IGNORE INTO task_group_admissions(workspace_key, task_id, system_kind, user_organized, creation_id) VALUES (?, ?, NULL, 0, NULL)",
            (ws_path, session_id),
        )
        node_key = json.dumps([ws_path, session_id], separators=(",", ":"), ensure_ascii=False)
        has_ord = ti.execute(
            "SELECT 1 FROM task_group_view_node_orders WHERE node_type='task' AND node_key=?",
            (node_key,),
        ).fetchone()
        if not has_ord:
            min_ord = ti.execute("SELECT COALESCE(MIN(sort_order), 0) - 1 FROM task_group_view_node_orders").fetchone()[0]
            ti.execute(
                "INSERT INTO task_group_view_node_orders(node_type, node_key, sort_order, created_at, updated_at) VALUES ('task', ?, ?, ?, ?)",
                (node_key, min_ord, now_t, now_t),
            )
        ti.commit()
        ti.close()
        return True
    except Exception:
        return False


def backfill_historical_v3_sessions() -> int:
    """Repair host_id='local' & group ordering for all existing and recent V3 sessions."""
    if not os.path.exists(DB_SQLITE) or not os.path.exists(TASKS_INDEX_SQLITE):
        return 0
    count = 0
    try:
        known_hist = {
            "sess_00000000-0000-0000-0000-000000000003": ("【V3-生产施工】C-20261121-001 v2.1面板初版草稿 (Builder R1)", r"C:\Users\Administrator\worktrees\ai-steward\C-20261121-001", "auto-heavy", "completed"),
            "sess_00000000-0000-0000-0000-000000000004": ("【V3-生产施工】C-20261121-001 v2.1面板施工记录 (Builder #2)", r"C:\Users\Administrator\worktrees\ai-steward\C-20261121-001", "auto-heavy", "completed"),
            "sess_00000000-0000-0000-0000-000000000005": ("【V3-生产施工】C-20261121-001 v2.1修复版 (Builder R2)", r"C:\Users\Administrator\worktrees\ai-steward\C-20261121-001", "auto-heavy", "completed"),
            "sess_00000000-0000-0000-0000-000000000006": ("【V3-UI审美质检】C-20261121-001 视觉审计记录 (QC#2)", r"C:\Users\Administrator\worktrees\ai-steward\C-20261121-001", "auto", "error"),
            "sess_00000000-0000-0000-0000-000000000007": ("【V3-UI审美质检】C-20261121-001 DOM复核记录 (QC#2)", r"C:\Users\Administrator\worktrees\ai-steward\C-20261121-001", "auto", "error"),
            "sess_00000000-0000-0000-0000-000000000008": ("【V3-生产施工】C-20261121-001 v2.1打磨版 (Builder R3)", r"C:\Users\Administrator\worktrees\ai-steward\C-20261121-001", "auto-heavy", "completed"),
        }
        ti_ro = sqlite3.connect(f"file:{TASKS_INDEX_SQLITE.replace(chr(92), '/')}?mode=ro", uri=True, timeout=5)
        bad_rows = ti_ro.execute(
            "SELECT task_id, title, worktree_root, model, task_status FROM tasks WHERE host_id IS NULL OR title LIKE '%V3%'"
        ).fetchall()
        ti_ro.close()
        done_sids = set()
        for tid, t_title, wt_r, mdl, tst in bad_rows:
            if tid in known_hist:
                t_title, wt_r, mh, tst = known_hist[tid]
                mdl = f"new-provider-7/{mh}"
            elif tst == "running":
                tst = "completed"
            if sync_session_to_tasks_index(tid, title=t_title or "", status=tst or "completed", wt_path=wt_r or "", model_hint=mdl or "new-provider-7/auto"):
                count += 1
            done_sids.add(tid)

        con = sqlite3.connect(f"file:{DB_SQLITE.replace(chr(92), '/')}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT id, title, directory, last_outcome FROM session "
            "WHERE title LIKE '%V3%' OR directory LIKE '%worktrees%ai-steward%' "
            "ORDER BY time_created DESC LIMIT 20"
        ).fetchall()
        con.close()
        for sid, title, directory, lo in rows:
            if sid in done_sids:
                continue
            if sid in known_hist:
                pretty, directory, mh, st = known_hist[sid]
            else:
                pretty = title if title and not title.startswith("你是 ") else f"【V4-历史作业】{os.path.basename(directory or 'task')} ({sid[5:13]})"
                mh, st = "auto", ("completed" if lo == "completed" else "completed")
            if sync_session_to_tasks_index(sid, title=pretty, status=st, wt_path=directory or "", model_hint=f"new-provider-7/{mh}"):
                count += 1
    except Exception:
        pass
    return count


def _list_recent_session_ids(since_ms: int) -> set[str]:
    if not os.path.exists(DB_SQLITE):
        return set()
    try:
        uri = "file:" + DB_SQLITE.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=3)
        rows = con.execute("SELECT id FROM session WHERE time_created >= ?", (since_ms,)).fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _desktop_supported_model(desired: str) -> str:
    """Resolve modelId present in AA desktop provider_config.json under new-provider-7."""
    pc_path = r"C:\Users\Administrator\.zcode\v2\provider_config.json"
    supported = ["auto", "auto-cheap", "auto-heavy", "auto-reason"]
    try:
        pc = json.load(open(pc_path, "r", encoding="utf-8"))
        rules = (((pc.get("config") or {}).get("providerConfigRules") or {}).get("providerRules") or [])
        for pr in rules:
            if pr.get("providerId") == "new-provider-7":
                mods = (pr.get("config") or {}).get("modelOrder") or (pr.get("config") or {}).get("personalModelIds") or []
                if mods:
                    supported = list(mods)
                break
    except Exception:
        pass
    if desired in supported:
        return desired
    if desired == "auto-night" and "auto-cheap" in supported:
        return "auto-cheap"
    return supported[0] if supported else "auto"


def _read_cli_session_assistant_text(session_id: str) -> str:
    if not session_id or not os.path.exists(DB_SQLITE):
        return ""
    try:
        con = sqlite3.connect(f"file:{DB_SQLITE.replace(chr(92), '/')}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT p.data, m.data FROM part p LEFT JOIN message m ON p.message_id = m.id "
            "WHERE p.session_id=? ORDER BY p.rowid ASC",
            (session_id,),
        ).fetchall()
        con.close()
        chunks: list[str] = []
        for p_raw, m_raw in rows:
            try:
                m_obj = json.loads(m_raw) if m_raw else {}
                if m_obj.get("role") != "assistant":
                    continue
                p_obj = json.loads(p_raw) if p_raw else {}
                if p_obj.get("type") == "text":
                    txt = str(p_obj.get("text") or "").strip()
                    if txt and not txt.startswith("<total_tokens>"):
                        chunks.append(txt)
            except Exception:
                pass
        return "\n".join(chunks)
    except Exception:
        return ""



def _latest_assistant_line(session_id: str) -> str:
    """只取这个会话最新一条助手正文，给施工中的一句话用。失败就当没有。"""
    if not session_id or not os.path.exists(DB_SQLITE):
        return ""
    try:
        con = sqlite3.connect(f"file:{DB_SQLITE.replace(chr(92), '/')}?mode=ro", uri=True, timeout=3)
        rows = con.execute(
            "SELECT p.data, m.data FROM part p LEFT JOIN message m ON p.message_id = m.id "
            "WHERE p.session_id=? ORDER BY p.rowid DESC LIMIT 8",
            (session_id,),
        ).fetchall()
        con.close()
        for p_raw, m_raw in rows:
            try:
                m_obj = json.loads(m_raw) if m_raw else {}
                if m_obj.get("role") != "assistant":
                    continue
                p_obj = json.loads(p_raw) if p_raw else {}
                if p_obj.get("type") != "text":
                    continue
                txt = str(p_obj.get("text") or "").strip()
                if txt and not txt.startswith("<total_tokens>"):
                    return txt
            except Exception:
                continue
    except Exception:
        return ""
    return ""


def _run_via_zcode_desktop(
    prompt: str,
    cwd: str,
    timeout_sec: int,
    role: str,
    model_order: list[str],
    shift: str,
    effective_title: str,
    gw_before: dict,
    t0: float,
    ms0: int,
    token: str,
    log_path: str | None,
    lane_override: dict | None = None,
    stall_timeout_sec: int = 0,
    pulse_order: str = "",
    pulse_round: int = 0,
) -> dict | None:
    """Trigger native live session inside running ZCode Desktop via manual automation run (visible spinner + UI)."""
    if not os.path.exists(TASKS_INDEX_SQLITE):
        return None
    auto_id = f"automation-v3-{uuid.uuid4().hex[:12]}"
    is_direct_lane = bool(lane_override and lane_override.get("provider_id") and lane_override.get("model_id"))
    if is_direct_lane:
        desktop_provider = str(lane_override["provider_id"])
        desktop_model = str(lane_override["model_id"])
    else:
        desktop_provider = "new-provider-7"
        desktop_model = _desktop_supported_model(model_order[0])
    msel_json = json.dumps({"providerId": desktop_provider, "modelId": desktop_model, "options": {"reasoningLevel": "max"}}, ensure_ascii=False)
    model_hint_full = f"{desktop_provider}/{desktop_model}"
    ws_path, proj_id = r"C:\Users\Administrator\Documents\mate\lib", "00000000-0000-0000-0000-000000000001"
    pre_sids = _list_recent_session_ids(ms0 - 15000)
    try:
        ti = sqlite3.connect(TASKS_INDEX_SQLITE, timeout=10)
        ti.execute("PRAGMA busy_timeout=5000")
        cands = _desktop_workspace_candidates(ti, fallback_dir=cwd)
        ws_path, proj_id = cands[0]
        cnt = ti.execute("SELECT COUNT(*) FROM automations").fetchone()[0]
        if cnt >= 19:
            ti.close()
            return None
        ti.execute(
            """
            INSERT INTO automations (
                automation_id, title, cron_expr, prompt, model, provider, mode, thought_level, model_selection,
                workspace_key, workspace_path, workspace_identity, target_task_id, bot_delivery_target, location_kind,
                recurring, max_runs, end_at, schedule_rule, schedule_edited_by_user, run_count, scheduled_run_count,
                enabled, lifecycle_status, next_run_at, last_run_at, running, claimed_at, dispatch_status,
                dispatch_attempts, retry_at, last_error, created_at, updated_at, project_id, host_id, additional_directories
            ) VALUES (
                ?, ?, '* * * * *', ?, NULL, NULL, 'yolo', NULL, ?,
                ?, ?, NULL, NULL, NULL, 'local',
                0, NULL, NULL, NULL, 0, 0, 0,
                1, 'active', ?, NULL, 0, NULL, 'idle',
                0, NULL, NULL, ?, ?, ?, 'local', '[]'
            )
            """,
            (auto_id, effective_title, prompt, msel_json, ws_path, ws_path, ms0, ms0, ms0, proj_id),
        )
        ti.commit()
        ti.close()
    except Exception:
        return None

    def _cleanup_temp_auto(sid_found: str = ""):
        try:
            ti_c = sqlite3.connect(TASKS_INDEX_SQLITE, timeout=8)
            ti_c.execute("PRAGMA busy_timeout=5000")
            ti_c.execute("DELETE FROM automation_runs WHERE automation_id=?", (auto_id,))
            ti_c.execute("DELETE FROM automations WHERE automation_id=?", (auto_id,))
            if sid_found:
                ti_c.execute("UPDATE tasks SET cron_automation_id=NULL WHERE task_id=?", (sid_found,))
            ti_c.commit()
            ti_c.close()
        except Exception:
            pass

    # Wait up to 24s for running ZCode Desktop scheduler (20s poll claimDue) to pick up active one-shot run
    sid = ""
    claimed_ok = False
    claim_deadline = now_ts() + 24.0
    while now_ts() < claim_deadline:
        try:
            ti_r = sqlite3.connect(f"file:{TASKS_INDEX_SQLITE.replace(chr(92), '/')}?mode=ro", uri=True, timeout=3)
            row = ti_r.execute(
                "SELECT dispatch_status, outcome, session_id, error FROM automation_runs WHERE automation_id=? ORDER BY created_at DESC LIMIT 1",
                (auto_id,),
            ).fetchone()
            arow = ti_r.execute(
                "SELECT target_task_id, running, dispatch_status FROM automations WHERE automation_id=?",
                (auto_id,),
            ).fetchone()
            ti_r.close()
            if arow and arow[0]:
                sid = arow[0]
            if row:
                d_st, outc, s_id, err = row
                if s_id:
                    sid = s_id
                if d_st in ("dispatched", "claimed") and sid:
                    claimed_ok = True
                    break
                if d_st == "failed_to_dispatch" or outc == "failed":
                    _cleanup_temp_auto(sid)
                    return None
            if not sid and arow and (arow[1] == 1 or arow[2] in ("claimed", "dispatched")):
                new_sids = _list_recent_session_ids(ms0 - 1000) - pre_sids
                if new_sids:
                    sid = sorted(new_sids)[-1]
                    claimed_ok = True
                    break
        except Exception:
            pass
        time.sleep(1.0)

    if not claimed_ok or not sid:
        _cleanup_temp_auto(sid)
        return None

    print(f"[factory_runner] ZCode Desktop native session LIVE: {sid} ({effective_title}) [{model_hint_full}]", flush=True)
    sync_session_to_tasks_index(sid, title=effective_title, status="running", wt_path=cwd, model_hint=model_hint_full)

    # Wait for desktop run completion
    terminal = False
    final_outcome = "running"
    timed_out = False
    last_progress_ts = now_ts()
    last_part_cnt = -1
    last_sess_upd = 0
    last_pulse_ts = 0.0
    while (now_ts() - t0) < timeout_sec:
        try:
            ti_r = sqlite3.connect(f"file:{TASKS_INDEX_SQLITE.replace(chr(92), '/')}?mode=ro", uri=True, timeout=3)
            row = ti_r.execute("SELECT outcome, error FROM automation_runs WHERE automation_id=? ORDER BY created_at DESC LIMIT 1", (auto_id,)).fetchone()
            ti_r.close()
            if row and row[0] in ("succeeded", "completed", "failed", "cancelled", "error"):
                final_outcome = "completed" if row[0] == "succeeded" else row[0]
                terminal = True
                break
        except Exception:
            pass
        if os.path.exists(DB_SQLITE):
            try:
                cd = sqlite3.connect(f"file:{DB_SQLITE.replace(chr(92), '/')}?mode=ro", uri=True, timeout=3)
                srow = cd.execute("SELECT last_outcome, time_updated FROM session WHERE id=?", (sid,)).fetchone()
                prow = cd.execute("SELECT COUNT(*) FROM part WHERE session_id=?", (sid,)).fetchone() if stall_timeout_sec > 0 else None
                cd.close()
                if srow and srow[0] in ("completed", "failed", "cancelled", "error", "interrupted"):
                    final_outcome = srow[0]
                    terminal = True
                    break
                cur_upd = int((srow[1] if srow and len(srow) > 1 else 0) or 0)
                cur_parts = int((prow[0] if prow else 0) or 0)
                if cur_upd > last_sess_upd or cur_parts > last_part_cnt:
                    last_sess_upd = max(last_sess_upd, cur_upd)
                    last_part_cnt = max(last_part_cnt, cur_parts)
                    last_progress_ts = now_ts()
            except Exception:
                pass
        if stall_timeout_sec > 0 and (now_ts() - last_progress_ts) >= stall_timeout_sec:
            timed_out = True
            final_outcome = "stall_timeout"
            print(f"[factory_runner] weekend-glm desktop session stalled ({stall_timeout_sec}s no progress): {sid}", flush=True)
            break
        if pulse_order and (now_ts() - last_pulse_ts) >= 60:
            last_pulse_ts = now_ts()
            try:
                import admit_v5
                _snip = _latest_assistant_line(sid)
                _actor = "质检中" if role == "verifier" else "施工中"
                append_v3_event(pulse_order, pulse_round, "live", _actor, admit_v5.live_line(_snip, int(now_ts() - t0), role))
            except Exception:
                pass
        time.sleep(1.5)

    if not terminal and not timed_out:
        timed_out = True
        final_outcome = "timeout"

    t1 = now_ts()
    ms1 = now_ms()
    # c004b: 结算段全护——桌面引擎此刻正提交会话终态，活库读/写撞锁曾把整只 tick 炸死（G12 R2 实证）
    try:
        gw_after = {} if is_direct_lane else get_gateway_status()
    except Exception:
        gw_after = dict(gw_before)
    try:
        assistant_text = _read_cli_session_assistant_text(sid)
    except Exception:
        assistant_text = ""
    clean_out = redact(assistant_text or "", token)
    if log_path:
        try:
            atomic_write_text(log_path, clean_out)
        except Exception:
            pass

    rc = 0 if final_outcome == "completed" else (124 if timed_out else 1)
    try:
        sync_session_to_tasks_index(sid, title=effective_title, status=("completed" if rc == 0 else "error"), wt_path=cwd, model_hint=model_hint_full)
    except Exception:
        pass  # c004b: sync 撞锁不打死 tick
    _cleanup_temp_auto(sid)
    direct_usage = None
    if is_direct_lane:
        du = _query_direct_lane_usage([sid], ms0, ms1, desktop_provider)
        direct_usage = {"lane": str(lane_override.get("lane") or "weekend-glm"), **du}
    return {
        "role": role,
        "shift": shift,
        "model_order": [desktop_model],
        "lane_selected": str(lane_override.get("lane")) if is_direct_lane else "gateway",
        "direct_usage": direct_usage,
        "stall_timeout": (final_outcome == "stall_timeout"),
        "exit_code": rc,
        "timed_out": timed_out,
        "duration_sec": round(t1 - t0, 2),
        "start_ms": ms0,
        "end_ms": ms1,
        "gateway_delta": gateway_delta(gw_before, gw_after),
        "sessions": [{"session_id": sid, "title": effective_title, "models": [model_hint_full], "native_desktop": True}],
        "task_title": effective_title,
        "stdout": clean_out,
    }


def run_headless_zcode(
    prompt: str,
    cwd: str,
    timeout_sec: int,
    role: str,
    attachments: list[str] | None = None,
    log_path: str | None = None,
    task_title: str = "",
    disallowed_tools: str = "",
    prefer_desktop: bool = True,
    lane_override: dict | None = None,
    stall_timeout_sec: int = 0,
    pulse_order: str = "",
    pulse_round: int = 0,
    prompt_desktop: str = "",
) -> dict:
    # BINFIX 20261123: prompt 可能内嵌二进制目标(如 APK)含 NUL 字节, CreateProcess 必拒 -> 源头剥除
    prompt = str(prompt or "").replace("\x00", "")
    # V5.0：桌面通道没有命令行长度上限，可以发全文；命令行通道仍发（已控制长度的）prompt
    prompt_desktop = str(prompt_desktop or "").replace("\x00", "")
    token = read_token_mem()
    is_direct_lane = bool(lane_override and lane_override.get("provider_id") and lane_override.get("model_id"))
    if is_direct_lane:
        shift = "direct"
        model_order = [str(lane_override["model_id"])]
        model_hint_cli = f"{lane_override['provider_id']}/{lane_override['model_id']}"
        if str(lane_override.get("lane") or "") == "weekend-glm" and stall_timeout_sec <= 0:
            stall_timeout_sec = WEEKEND_GLM_STALL_SEC
    else:
        shift, model_order = write_provider_config(role)
        model_hint_cli = f"new-provider-7/{model_order[0]}"
    builtin_cfg = find_builtin_provider_cfg()
    effective_title = task_title or (f"【V4-{'施工' if role == 'worker' else '质检'}】{_dt.datetime.now().strftime('%H:%M:%S')}")

    gw_before = {} if is_direct_lane else get_gateway_status()
    t0 = now_ts()
    ms0 = now_ms()

    if prefer_desktop and not attachments and os.path.exists(TASKS_INDEX_SQLITE):
        desk_res = _run_via_zcode_desktop(
            prompt=(prompt_desktop or prompt),
            cwd=cwd,
            timeout_sec=timeout_sec,
            role=role,
            model_order=model_order,
            shift=shift,
            effective_title=effective_title,
            gw_before=gw_before,
            t0=t0,
            ms0=ms0,
            token=token,
            log_path=log_path,
            lane_override=lane_override,
            stall_timeout_sec=stall_timeout_sec,
            pulse_order=pulse_order,
            pulse_round=pulse_round,
        )
        if desk_res is not None:
            return desk_res
        print("[factory_runner] ZCode Desktop scheduler did not claim in 26s; falling back to CLI + tasks-index sync...", flush=True)

    env = os.environ.copy()
    env["USERPROFILE"] = r"C:\Users\Administrator"
    env["HOME"] = r"C:\Users\Administrator"
    env["APPDATA"] = r"C:\Users\Administrator\AppData\Roaming"
    env["LOCALAPPDATA"] = r"C:\Users\Administrator\AppData\Local"
    direct_cfg_tmp = ""
    if is_direct_lane:
        # 直连：把目标 provider 复制成一次性 personal config（只落临时目录，用完即删）
        direct_cfg_tmp = write_direct_provider_config(str(lane_override["provider_id"]), str(lane_override["model_id"]))
        if direct_cfg_tmp:
            env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = direct_cfg_tmp
        else:
            env.pop("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", None)
    else:
        env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = PROVIDER_CFG_PATH
    if builtin_cfg:
        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = builtin_cfg

    desktop_ws = r"C:\Users\Administrator\Documents\mate\lib"
    cli_cwd = desktop_ws if os.path.exists(desktop_ws) else cwd
    # 2026-11-23 supervisor fix: Windows CreateProcess 命令行 ~32767 上限, 超长简报保头保尾掐中间
    if len(prompt) > 24000:
        _budget = 24000
        _head_n = int(_budget * 0.6)
        _tail_n = _budget - _head_n - 120
        _omit = len(prompt) - _head_n - _tail_n
        prompt = (prompt[:_head_n]
                  + "\n\n[... 中间 %d 字符因 Windows 命令行长度上限被截断: 三视口遥测明细等长字段略去, 请以 Gate1 JSON 摘要、断言结果与规格验收条款为准 ...]\n\n" % _omit
                  + prompt[-_tail_n:])
    cmd = [NODE_EXE, ZCODE_CJS, "-p", prompt, "--mode", "yolo", "--surface", "desktop", "--no-color", "--cwd", cli_cwd]
    if disallowed_tools:
        cmd.extend(["--disallowed-tools", disallowed_tools])
    for ap in attachments or []:
        if ap and os.path.exists(ap):
            cmd.extend(["--attach", ap])

    gw_before = {} if is_direct_lane else get_gateway_status()
    t0 = now_ts()
    ms0 = now_ms()
    pre_sids = _list_recent_session_ids(ms0 - 30000)
    timed_out = False
    rc = -1
    combined = ""
    tracked_sids: set[str] = set()
    stop_sync = threading.Event()

    def _sync_worker():
        while not stop_sync.is_set():
            try:
                cur_sids = _list_recent_session_ids(ms0 - 2000) - pre_sids
                for sid in cur_sids:
                    if sid not in tracked_sids:
                        if sync_session_to_tasks_index(sid, title=effective_title, status="running", wt_path=cli_cwd, model_hint=model_hint_cli):
                            tracked_sids.add(sid)
            except Exception:
                pass
            stop_sync.wait(1.5)

    stop_pulse = threading.Event()

    def _pulse_worker():
        if not pulse_order:
            return
        while not stop_pulse.wait(60):
            try:
                import admit_v5
                _actor = "质检中" if role == "verifier" else "施工中"
                append_v3_event(pulse_order, pulse_round, "live", _actor, admit_v5.live_line("", int(now_ts() - t0), role))
            except Exception:
                pass

    pulse_thread = threading.Thread(target=_pulse_worker, daemon=True)
    sync_thread = threading.Thread(target=_sync_worker, daemon=True)
    proc = subprocess.Popen(
        cmd,
        cwd=cli_cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sync_thread.start()
    pulse_thread.start()
    try:
        combined, _ = proc.communicate(timeout=timeout_sec)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        except Exception:
            proc.kill()
        try:
            combined, _ = proc.communicate(timeout=5)
        except Exception:
            combined = ""
        rc = 124
    finally:
        stop_sync.set()
        stop_pulse.set()
        sync_thread.join(timeout=2.0)
        pulse_thread.join(timeout=1.0)
        if direct_cfg_tmp:
            try:
                os.unlink(direct_cfg_tmp)
            except OSError:
                pass

    t1 = now_ts()
    ms1 = now_ms()
    gw_after = {} if is_direct_lane else get_gateway_status()
    clean_out = redact(combined or "", token)
    if log_path:
        atomic_write_text(log_path, clean_out)

    sessions = detect_sessions_in_window(ms0, ms1)
    final_task_status = "completed" if rc == 0 else "error"
    for s in sessions:
        sid = s.get("session_id")
        if sid and (sid not in pre_sids or sid in tracked_sids):
            sync_session_to_tasks_index(sid, title=effective_title, status=final_task_status, wt_path=cwd, model_hint=(s.get("models") or [model_hint_cli])[0])
            s["title"] = effective_title
    for sid in tracked_sids:
        sync_session_to_tasks_index(sid, title=effective_title, status=final_task_status, wt_path=cwd, model_hint=model_hint_cli)

    direct_usage = None
    if is_direct_lane:
        cli_sids = list({s.get("session_id") for s in sessions if s.get("session_id")} | tracked_sids)
        du = _query_direct_lane_usage(cli_sids, ms0, ms1, str(lane_override["provider_id"]))
        direct_usage = {"lane": str(lane_override.get("lane") or "weekend-glm"), **du}

    return {
        "role": role,
        "shift": shift,
        "model_order": model_order,
        "lane_selected": str(lane_override.get("lane")) if is_direct_lane else "gateway",
        "direct_usage": direct_usage,
        "exit_code": rc,
        "timed_out": timed_out,
        "duration_sec": round(t1 - t0, 2),
        "start_ms": ms0,
        "end_ms": ms1,
        "gateway_delta": gateway_delta(gw_before, gw_after),
        "sessions": sessions,
        "task_title": effective_title,
        "stdout": clean_out,
    }


# ------------------------------------------------------------------ Sandbox UI Preview Server


# V5.0-real.7：Gate1 真引擎必须用「被测沙箱自己的数据层」算 /data.json。
# 旧实现在内核进程里直接 import v3_data——命中的是活仓已缓存的模块，沙箱里改的 v3_data.py 从没被执行。
# 2026-11-26 G44R8 R8-2 就是这么冤死的：沙箱数据层已把 gate2_infra_retry 放进时间线，探针拿活仓旧数据判 FAIL。
# 子进程隔离还能避免 sys.modules 缓存和兄弟模块串仓；状态目录仍指生产 state（沙箱 worktree 不带 state/）。
_SANDBOX_V3_WRAPPER = r'''
import json, os, sys
wt, live, state, target, out = sys.argv[1:6]
sys.path.insert(0, os.path.join(wt, "scripts"))
import v3_data
v3_data.STEWARD_ROOT = live
v3_data.STATE_DIR = state
v3_data.DASH_V2_PATH = target
for _name, _value in {
    "QUEUE_PATH": os.path.join(state, "factory_queue.json"),
    "LEDGER_PATH": os.path.join(state, "v3_budget_ledger.json"),
    "EVENTS_JSONL_PATH": os.path.join(state, "events.jsonl"),
    "SLOTS_PATH": os.path.join(state, "runner_slots.json"),
    "CONTRACTS_DIR": os.path.join(state, "contracts"),
}.items():
    if hasattr(v3_data, _name):
        setattr(v3_data, _name, _value)
data = v3_data.collect_v3_data(target_override=target)
with open(out, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)
'''


def collect_sandbox_v3_data(wt_repo: str, target_file: str) -> bytes:
    """用沙箱 worktree 里的 scripts/v3_data.py 现算一份 /data.json（生产 state）。"""
    v3_path = os.path.join(wt_repo, "scripts", "v3_data.py")
    if not os.path.isfile(v3_path):
        raise FileNotFoundError("沙箱缺 scripts/v3_data.py: %s" % wt_repo)
    state_dir = os.path.join(STEWARD_ROOT, "state")
    fd, out_path = tempfile.mkstemp(prefix="sandbox_v3_", suffix=".json")
    os.close(fd)
    env = os.environ.copy()
    env["STEWARD_STATE_DIR"] = state_dir
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", _SANDBOX_V3_WRAPPER,
             wt_repo, STEWARD_ROOT, state_dir, target_file, out_path],
            cwd=wt_repo, env=env, capture_output=True, timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-800:]
            raise RuntimeError("沙箱 v3_data 退出码 %s: %s" % (proc.returncode, err))
        with open(out_path, "rb") as f:
            data = f.read()
        if not data:
            raise RuntimeError("沙箱 v3_data 产出为空")
        return data
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


class _SandboxServer:
    """In-process authenticated preview server serving worktree dashboard files + worktree /data.json."""

    def __init__(self, wt_repo: str, target_rel: str, token: str, data_mode: str = "live") -> None:
        self.data_mode = data_mode  # V4.0-P2："404" = 降级态预览（/data.json 恒回 404，供 state=degraded 断言）
        self.wt_repo = wt_repo
        self.wt_dash = os.path.join(wt_repo, "dashboard")
        self.target_rel = target_rel.replace("\\", "/")
        self.token = token
        self.srv: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    def __enter__(self) -> "_SandboxServer":
        wt_dash = self.wt_dash
        wt_repo = self.wt_repo
        target_file = os.path.join(os.path.dirname(wt_dash), self.target_rel)
        if not os.path.exists(target_file):
            target_file = os.path.join(wt_dash, os.path.basename(self.target_rel))
        tok = self.token
        data_mode = self.data_mode

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                path = u.path
                k = urllib.parse.parse_qs(u.query).get("k", [""])[0]
                if path == "/healthz":
                    self._send(200, "text/plain", b"ok")
                    return
                via_cookie = bool(tok) and (f"board_k={tok}" in self.headers.get("Cookie", ""))
                via_url = bool(tok) and (k == tok)
                if tok and not (via_url or via_cookie):
                    self.send_error(403)
                    return

                if path in ("/v2", "/v2.html", "/" + os.path.basename(target_file)):
                    if not os.path.exists(target_file):
                        self.send_error(404)
                        return
                    body = open(target_file, "rb").read()
                    self._send(200, "text/html; charset=utf-8", body, seed_cookie=(via_url and not via_cookie))
                elif path in ("/data.json", "/api/v2/data", "/api/data") and data_mode == "404":
                    self.send_error(404)
                elif path in ("/data.json", "/api/v2/data", "/api/data"):
                    try:
                        data_bytes = collect_sandbox_v3_data(wt_repo, target_file)
                    except Exception as exc:  # noqa: BLE001
                        # 生产页自测（wt_repo 就是活仓）才允许退回进程内模块，两者等价；
                        # 沙箱数据层坏了就如实 500，绝不静默拿活仓旧数据把工人判绿。
                        if os.path.realpath(wt_repo) != os.path.realpath(STEWARD_ROOT):
                            err = json.dumps({"error": "sandbox_v3_data_failed", "detail": str(exc)[:300]},
                                             ensure_ascii=False).encode("utf-8")
                            self._send(500, "application/json; charset=utf-8", err)
                            return
                        try:
                            import v3_data
                            data_bytes = json.dumps(v3_data.collect_v3_data(target_override=target_file),
                                                    ensure_ascii=False).encode("utf-8")
                        except Exception as exc2:  # noqa: BLE001
                            err = json.dumps({"error": "v3_data_failed", "detail": str(exc2)[:300]},
                                             ensure_ascii=False).encode("utf-8")
                            self._send(500, "application/json; charset=utf-8", err)
                            return
                    self._send(200, "application/json; charset=utf-8", data_bytes)
                elif path == "/api/v3/events":
                    data_bytes = b'{"live":[]}'
                    try:
                        import v3_data
                        ev = v3_data.collect_live_agent_stream()
                        try:
                            d_now = v3_data.collect_v3_data(target_override=target_file)
                            ev["cost_per_accepted_cny"] = d_now.get("cost_per_accepted_cny", 0.0)
                        except Exception:
                            ev["cost_per_accepted_cny"] = 0.0
                        data_bytes = json.dumps(ev, ensure_ascii=False).encode("utf-8")
                    except Exception:
                        try:
                            q_url = f"{BOARD_BASE_URL}/api/v3/events"
                            with urllib.request.urlopen(board_request(q_url, tok), timeout=8) as r:
                                data_bytes = r.read()
                        except Exception:
                            data_bytes = b'{"live":[]}'
                    self._send(200, "application/json; charset=utf-8", data_bytes)
                elif path.startswith("/api/"):
                    try:
                        qs = urllib.parse.urlencode([(qk, qv) for qk, qv in urllib.parse.parse_qsl(u.query) if qk != "k"])
                        p_url = f"{BOARD_BASE_URL}{path}" + (f"?{qs}" if qs else "")
                        with urllib.request.urlopen(board_request(p_url, tok), timeout=8) as r:
                            p_ctype = r.headers.get("Content-Type", "application/json; charset=utf-8")
                            p_body = r.read()
                        self._send(200, p_ctype, p_body)
                    except urllib.error.HTTPError as he:
                        self._send(int(he.code), "application/json; charset=utf-8", he.read() if hasattr(he, "read") else b"{}")
                    except Exception:
                        self._send(200, "application/json; charset=utf-8", b"{}")
                else:
                    self.send_error(404)

            def _send(self, code: int, ctype: str, body: bytes, seed_cookie: bool = False):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                if seed_cookie and tok:
                    self.send_header("Set-Cookie", f"board_k={tok}; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        if self.srv:
            try:
                self.srv.shutdown()
                self.srv.server_close()
            except Exception:
                pass


# ------------------------------------------------------------------ Mechanical Probes (Gate 1)


class _DomAuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.zones_found: set[str] = set()
        self.collapsed_details = 0
        self.total_details = 0
        self.visible_chunks: list[str] = []
        self.has_viewport_meta = False
        self.in_zone3_depth = 0
        self.zone3_visual_bars = 0
        self.style_blocks: list[str] = []
        self.in_style = False

    def _is_hidden_attr(self, attrs: dict[str, str | None]) -> bool:
        if "hidden" in attrs:
            return True
        st = (attrs.get("style") or "").replace(" ", "").lower()
        return "display:none" in st or "visibility:hidden" in st

    def _inside_hidden(self) -> bool:
        for item in self.stack:
            if item["skip"]:
                return True
        in_closed = any(item["closed_details"] for item in self.stack)
        if not in_closed:
            return False
        in_summary = any(item["tag"] == "summary" for item in self.stack)
        if in_summary:
            last_closed = max(i for i, it in enumerate(self.stack) if it["closed_details"])
            last_summary = max(i for i, it in enumerate(self.stack) if it["tag"] == "summary")
            if last_summary > last_closed:
                return False
        return True

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attrs = {k.lower(): v for k, v in attrs_list}

        if t == "meta" and (attrs.get("name") or "").lower() == "viewport":
            self.has_viewport_meta = True

        dz = attrs.get("data-zone")
        if dz is not None:
            self.zones_found.add(str(dz).strip())

        is_closed_details = False
        if t == "details":
            self.total_details += 1
            if "open" not in attrs:
                self.collapsed_details += 1
                is_closed_details = True

        entered_z3 = dz == "3"
        if entered_z3:
            self.in_zone3_depth += 1

        skip = t in ("script", "style", "noscript", "template") or self._is_hidden_attr(attrs)
        self.stack.append({"tag": t, "skip": skip, "closed_details": is_closed_details, "z3": entered_z3})
        if t == "style":
            self.in_style = True

        if self.in_zone3_depth > 0 and not self._inside_hidden():
            cls = (attrs.get("class") or "").lower()
            st = (attrs.get("style") or "").lower()
            if t in ("progress", "svg") or attrs.get("role") == "progressbar":
                self.zone3_visual_bars += 1
            elif ("bar" in cls or "fill" in cls or "meter" in cls) and ("width" in st or "bar" in cls):
                if "wrap" not in cls and "row" not in cls:
                    self.zone3_visual_bars += 1

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "style":
            self.in_style = False
        while self.stack:
            item = self.stack.pop()
            if item["z3"] and self.in_zone3_depth > 0:
                self.in_zone3_depth -= 1
            if item["tag"] == t:
                break

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self.style_blocks.append(data)
            return
        if not self._inside_hidden():
            stripped = data.strip()
            if stripped:
                self.visible_chunks.append(stripped)


def find_edge_exe() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def board_request(url: str, tok: str = "") -> "urllib.request.Request":
    """V4.0-P4：访问 8900 看板一律用请求头 X-Board-Token 带口令（不再拼进网址）。"""
    req = urllib.request.Request(url, method="GET")
    if tok:
        req.add_header("X-Board-Token", tok)
    return req


def http_get_status(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    try:
        req = urllib.request.Request(url, method="GET")
        for hk, hv in (headers or {}).items():
            req.add_header(hk, hv)
        with urllib.request.urlopen(req, timeout=8) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read() if hasattr(e, "read") else b""
    except Exception:
        return 0, b""


def png_dimensions(path: str) -> tuple[bool, int, int]:
    if not os.path.exists(path):
        return False, 0, 0
    with open(path, "rb") as f:
        hdr = f.read(24)
    if len(hdr) < 24 or hdr[:8] != b"\x89PNG\r\n\x1a\n" or hdr[12:16] != b"IHDR":
        return False, 0, 0
    w, h = struct.unpack(">II", hdr[16:24])
    return True, w, h


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def probe_ui_dashboard(
    wt_repo: str,
    main_repo: str,
    target_rel: str,
    out_dir: str,
    six_baseline: dict[str, str],
    old_sha: str,
    main_file_sha_before: str,
    assertions: list[dict] | None = None,
) -> dict:
    token = read_token_mem()
    target_rel_norm = target_rel.replace("\\", "/")
    wt_target_path = os.path.join(wt_repo, target_rel_norm)
    main_target_path = os.path.join(main_repo, target_rel_norm)
    edge_exe = find_edge_exe()

    failures: list[str] = []
    checks: dict = {}

    # 0. Sandbox isolation verify: main repo target must be untouched before merge
    main_sha_now = sha256_file(main_target_path) if os.path.exists(main_target_path) else ""
    sandbox_clean = (main_sha_now == main_file_sha_before)
    checks["main_repo_untouched_before_merge"] = sandbox_clean
    if not sandbox_clean:
        failures.append(f"违反 Worktree 隔离纪律：Builder 原地篡改了主仓 {main_target_path}")

    # 1. Six static pages integrity
    six_now = snapshot_six_pages()
    six_diffs = [k for k in SIX_STATIC_PAGES if six_now.get(k) != six_baseline.get(k)]
    checks["six_pages_intact"] = len(six_diffs) == 0
    if six_diffs:
        failures.append(f"静态正本被修改: {six_diffs}")

    # 2. Sandbox deliverable disk & diff check
    if not os.path.exists(wt_target_path):
        failures.append(f"沙箱目标文件不存在: {wt_target_path}")
        return {"all_passed": False, "checks": checks, "failures": failures}

    raw_bytes = open(wt_target_path, "rb").read()
    file_sha = hashlib.sha256(raw_bytes).hexdigest()
    file_sha16 = file_sha[:16]
    file_size = len(raw_bytes)
    mtime_iso = _dt.datetime.fromtimestamp(os.path.getmtime(wt_target_path)).strftime("%Y-%m-%d %H:%M:%S")
    raw_text = raw_bytes.decode("utf-8", errors="replace")

    token_leaked = bool(token and token in raw_text)
    checks["token_leaked_in_html"] = token_leaked
    if token_leaked:
        failures.append("HTML 源码硬编码了看板 token")

    if old_sha and file_sha == old_sha:
        failures.append(f"沙箱内 {target_rel_norm} SHA 与旧版完全一致 ({file_sha16})，未产生有效修改")

    if not edge_exe:
        failures.append("未找到 msedge.exe")
        return {
            "all_passed": False,
            "bytes": file_size,
            "sha16": file_sha16,
            "mtime": mtime_iso,
            "checks": checks,
            "failures": failures,
        }

    shots_dir = os.path.join(out_dir, "shots")
    os.makedirs(shots_dir, exist_ok=True)
    dom_dump_path = os.path.join(out_dir, "dom_dump_redacted.html")
    telemetry_path = os.path.join(out_dir, "cdp_telemetry.json")
    assert_raw_path = os.path.join(out_dir, "assertions_raw.json")
    _assert_stack = contextlib.ExitStack()
    _v4f = load_v4_flags()
    probe_fix_info: dict = {}
    if os.path.exists(assert_raw_path):
        try:
            os.remove(assert_raw_path)
        except OSError:
            pass
    # c003fix: 清除上一轮遗留证据（陈旧 telemetry/shots 曾伪装成本轮产物，22:10 幽灵数据）
    probe_t0 = _dt.datetime.now().timestamp()
    for _stale in (dom_dump_path, telemetry_path):
        try:
            if os.path.exists(_stale):
                os.remove(_stale)
        except Exception:
            pass
    try:
        for _f in os.listdir(shots_dir):
            try:
                os.remove(os.path.join(shots_dir, _f))
            except Exception:
                pass
    except Exception:
        pass

    dom_html = ""
    cdp_data: dict = {}
    shots_info: list[dict] = []
    url_page = os.path.basename(target_rel_norm)

    data_body = b""
    with _SandboxServer(wt_repo, target_rel_norm, token) as sb:
        base_sb = f"http://127.0.0.1:{sb.port}"
        url_no_auth = f"{base_sb}/{url_page}"
        url_auth = f"{base_sb}/{url_page}?k={token}" if token else url_no_auth
        code_unauth, _ = http_get_status(url_no_auth)
        code_auth, live_body = http_get_status(url_auth)
        live_sha = hashlib.sha256(live_body).hexdigest() if live_body else ""

        data_url = f"{base_sb}/data.json?k={token}" if token else f"{base_sb}/data.json"
        code_data, data_body = http_get_status(data_url)
        orders_in_data = 0
        if code_data == 200 and data_body:
            try:
                djson = json.loads(data_body.decode("utf-8", errors="replace"))
                ord_obj = djson.get("orders")
                if isinstance(ord_obj, dict):
                    orders_in_data = len(ord_obj.get("rows") or ord_obj.get("items") or ord_obj)
                elif isinstance(ord_obj, list):
                    orders_in_data = len(ord_obj)
            except Exception:
                pass

        checks["http_403_unauth"] = (code_unauth == 403) if token else True
        checks["http_200_auth"] = code_auth == 200
        checks["sandbox_live_sha_matches"] = live_sha == file_sha
        checks["data_json_200"] = code_data == 200
        checks["data_orders_count"] = orders_in_data

        if token and code_unauth != 403:
            failures.append(f"无 token 访问未返回 403 (实际 {code_unauth})")
        if code_auth != 200:
            failures.append(f"带 token 访问未返回 200 (实际 {code_auth})")
        if code_data != 200:
            failures.append(f"/data.json 返回异常 (实际 {code_data})")

        # True Edge CDP probe across 1440x900, 1200x800, 390x844
        # c002 修复: 不再把档案环境覆盖为 Administrator（SSH REPLACE_WITH_USER 身份无权读取该目录，
        # 无头 Edge 启动即死，CDP 探针必挂）。Edge 已带专属 --user-data-dir，继承进程真实身份即可。
        # c003fix: Edge 冷启动监听可能首次不通（22:25-22:46 三连挂），加 /json/version 预检
        # （最多 14s），不通则换端口+换档案重启一次再进探针；探针脚本自身另有 48s 容忍。
        import time as _time_mod

        def _edge_alive(port: int) -> bool:
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as _r:
                    return getattr(_r, "status", 200) == 200
            except Exception:
                return False

        def _launch_edge(port: int, udd: str, stderr_path: str = ""):
            edge_env = os.environ.copy()
            edge_env["USERPROFILE"] = NATIVE_USERPROFILE
            edge_env["HOME"] = NATIVE_USERPROFILE
            edge_env["APPDATA"] = NATIVE_APPDATA
            edge_env["LOCALAPPDATA"] = NATIVE_LOCALAPPDATA
            err_target = subprocess.DEVNULL
            err_fh = None
            if stderr_path:
                try:
                    err_fh = open(stderr_path, "wb")
                    err_target = err_fh
                except Exception:
                    err_fh = None
            try:
                return subprocess.Popen(
                    [
                        edge_exe,
                        f"--user-data-dir={udd}",
                        f"--remote-debugging-port={port}",
                        "--headless=new",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "about:blank",
                    ],
                    env=edge_env,
                    stdout=subprocess.DEVNULL,
                    stderr=err_target,
                )
            finally:
                if err_fh is not None:
                    try:
                        err_fh.close()
                    except Exception:
                        pass

        def _cleanup_orphan_probe_edges():
            try:
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { $_.CommandLine -like '*v3_edge_cdp_*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
                    ],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass

        _cleanup_orphan_probe_edges()
        cdp_port = _free_local_port()
        edge_udd = tempfile.mkdtemp(prefix="v3_edge_cdp_")
        edge_proc = None
        _alive = False
        last_stderr_path = ""
        for _launch_try in range(3):
            if _launch_try:
                if _launch_try >= 2:
                    _time_mod.sleep(15)  # c004fix: 3rd try backoff (Edge update window)
                if edge_proc is not None:
                    try:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(edge_proc.pid)], capture_output=True, timeout=8)
                    except Exception:
                        pass
                    edge_proc = None
                shutil.rmtree(edge_udd, ignore_errors=True)
                cdp_port = _free_local_port()
                edge_udd = tempfile.mkdtemp(prefix="v3_edge_cdp_")
            last_stderr_path = os.path.join(out_dir, f"edge_stderr_try{_launch_try}.log")
            try:
                edge_proc = _launch_edge(cdp_port, edge_udd, last_stderr_path)
            except Exception:
                edge_proc = None
            _alive = False
            for _ in range(14):
                if edge_proc is not None and _edge_alive(cdp_port):
                    _alive = True
                    break
                _time_mod.sleep(1)
            if _alive:
                break
        if not _alive:
            if edge_proc is not None:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(edge_proc.pid)], capture_output=True, timeout=8)
                except Exception:
                    pass
            shutil.rmtree(edge_udd, ignore_errors=True)
            _cleanup_orphan_probe_edges()
            err_tail = ""
            if last_stderr_path and os.path.exists(last_stderr_path):
                try:
                    err_tail = open(last_stderr_path, "r", encoding="utf-8", errors="replace").read().strip()[-300:]
                except Exception:
                    pass
            failures.append(f"[ENV] Edge CDP 3次启动均未就绪（环境故障，非工件缺陷）stderr={err_tail}")
            return {"all_passed": False, "checks": checks, "failures": failures, "screenshots": [], "env_fail": True}
        # c003fix: 重新打开外层 try——原 probe_mjs 起的后续块仍居此 try 内
        try:

            probe_mjs = os.path.join(out_dir, "_cdp_probe.mjs")
            shot_specs = [
                (1440, 900, False, "v21_1440x900.png"),
                (1200, 800, False, "v21_1200x800.png"),
                (390, 844, True, "v21_390x844.png"),
            ]
            # c003fix: 就绪等待阈值同样基线派生（section 数）
            try:
                min_zones = len(set(re.findall(r'<section[^>]*\bid="z(\d+)"', open(main_target_path, encoding="utf-8", errors="replace").read()))) or 13
            except Exception:
                min_zones = 13
            cfg_obj = {
                "cdpPort": cdp_port,
                "url": url_auth,
                "shotsDir": shots_dir,
                "telemetryPath": telemetry_path,
                "domPath": dom_dump_path,
                "specs": [{"w": w, "h": h, "mobile": mob, "file": fn} for w, h, mob, fn in shot_specs],
            }
            # V4.0-P2：规格断言执行计划（基线版 = 主仓当前生产版；降级版 = /data.json 恒 404）
            node_timeout = 70
            if assertions:
                a_plan = build_assertion_plan(assertions)
                a_urls = {"sandbox": url_auth}
                q_tok = f"?k={token}" if token else ""

                def _srv_url(repo, mode):
                    s_ = _assert_stack.enter_context(_SandboxServer(repo, target_rel_norm, token, data_mode=mode))
                    return f"http://127.0.0.1:{s_.port}/{url_page}{q_tok}"

                if a_plan["need_degraded"]:
                    a_urls["sandbox_degraded"] = _srv_url(wt_repo, "404")
                if a_plan["need_baseline"]:
                    a_urls["baseline"] = _srv_url(main_repo, "live")
                    if a_plan["need_degraded"]:
                        a_urls["baseline_degraded"] = _srv_url(main_repo, "404")
                cfg_obj["assert"] = {"urls": a_urls, "groups": a_plan["groups"], "defs": a_plan["defs"],
                                     "vdefs": a_plan.get("visual_defs") or {}, "outPath": assert_raw_path}
                node_timeout = (70 + sum(12 + 4 * len(g["viewports"]) for g in a_plan["groups"] for _ in g["targets"])
                                + 10 * int(a_plan.get("visual_count") or 0))
            mjs_src = """import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const cfg = __CFG_JSON__;
const sleep = ms => new Promise(r => setTimeout(r, ms));
// ---------------- V4.0-P3 PNG 解码 / 像素比对 / 编码（仅 node:zlib，无第三方依赖）
const V4_CRC_TABLE = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
function v4Crc32(buf) { let c = 0xffffffff; for (let i = 0; i < buf.length; i++) c = V4_CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; }
function v4PngDecode(buf) {
  if (buf.length < 8 || buf.readUInt32BE(0) !== 0x89504e47) throw new Error('not a png');
  let p = 8, w = 0, h = 0, bd = 0, ct = 0, il = 0; const idat = [];
  while (p + 8 <= buf.length) {
    const len = buf.readUInt32BE(p); const type = buf.toString('ascii', p + 4, p + 8); const data = buf.subarray(p + 8, p + 8 + len);
    if (type === 'IHDR') { w = data.readUInt32BE(0); h = data.readUInt32BE(4); bd = data[8]; ct = data[9]; il = data[12]; }
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    p += 12 + len;
  }
  if (bd !== 8 || il !== 0 || (ct !== 2 && ct !== 6)) throw new Error('unsupported png bitDepth=' + bd + ' colorType=' + ct + ' interlace=' + il);
  const bpp = ct === 6 ? 4 : 3, stride = w * bpp, raw = zlib.inflateSync(Buffer.concat(idat)), out = Buffer.alloc(h * stride);
  let rp = 0;
  for (let y = 0; y < h; y++) {
    const f = raw[rp++], o = y * stride, po = o - stride;
    for (let x = 0; x < stride; x++) {
      const r = raw[rp++], a = x >= bpp ? out[o + x - bpp] : 0, b = y > 0 ? out[po + x] : 0, c = (x >= bpp && y > 0) ? out[po + x - bpp] : 0;
      let v;
      switch (f) {
        case 0: v = r; break;
        case 1: v = r + a; break;
        case 2: v = r + b; break;
        case 3: v = r + ((a + b) >> 1); break;
        case 4: { const pa = Math.abs(b - c), pb = Math.abs(a - c), pc = Math.abs(a + b - 2 * c); v = r + ((pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c)); break; }
        default: throw new Error('bad png filter ' + f);
      }
      out[o + x] = v & 255;
    }
  }
  return { w, h, bpp, data: out };
}
function v4PngEncodeRGB(w, h, rgb) {
  const rs = w * 3 + 1, rows = Buffer.alloc(rs * h);
  for (let y = 0; y < h; y++) { rows[y * rs] = 0; rgb.copy(rows, y * rs + 1, y * w * 3, (y + 1) * w * 3); }
  const chunk = (type, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const td = Buffer.concat([Buffer.from(type, 'ascii'), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(v4Crc32(td)); return Buffer.concat([len, td, crc]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(rows, { level: 6 })), chunk('IEND', Buffer.alloc(0))]);
}
// 比对：重叠区逐像素 RGB 通道最大差 > thr 计为差异；尺寸不同的多出区域全部计为差异（页面高度变化 = 视觉变化）
function v4PngDiff(fa, fb, thr, outPath) {
  const A = v4PngDecode(fs.readFileSync(fa)), B = v4PngDecode(fs.readFileSync(fb));
  const W = Math.max(A.w, B.w), H = Math.max(A.h, B.h), mw = Math.min(A.w, B.w), mh = Math.min(A.h, B.h);
  if (A.w === B.w && A.h === B.h && A.bpp === B.bpp && A.data.equals(B.data))
    return { diff_px: 0, total_px: W * H, diff_pct: 0, size_sandbox: [A.w, A.h], size_baseline: [B.w, B.h], bbox: null, diff_png: '', identical: true };
  const img = Buffer.alloc(W * H * 3); let diff = 0, x0 = W, y0 = H, x1 = -1, y1 = -1;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const o = (y * W + x) * 3; let d = true, g = 255;
    if (x < mw && y < mh) {
      const ia = (y * A.w + x) * A.bpp, ib = (y * B.w + x) * B.bpp;
      const dr = Math.abs(A.data[ia] - B.data[ib]), dg = Math.abs(A.data[ia + 1] - B.data[ib + 1]), db = Math.abs(A.data[ia + 2] - B.data[ib + 2]);
      d = Math.max(dr, dg, db) > thr; g = (A.data[ia] * 77 + A.data[ia + 1] * 150 + A.data[ia + 2] * 29) >> 8;
    }
    if (d) { diff++; img[o] = 220; img[o + 1] = 38; img[o + 2] = 38; if (x < x0) x0 = x; if (y < y0) y0 = y; if (x > x1) x1 = x; if (y > y1) y1 = y; }
    else { const v = 192 + (g >> 2); img[o] = v; img[o + 1] = v; img[o + 2] = v; }
  }
  const total = W * H;
  if (outPath && diff > 0) fs.writeFileSync(outPath, v4PngEncodeRGB(W, H, img));
  return { diff_px: diff, total_px: total, diff_pct: total ? Math.round(diff / total * 1e5) / 1e3 : 0,
           size_sandbox: [A.w, A.h], size_baseline: [B.w, B.h], bbox: diff ? [x0, y0, x1, y1] : null, diff_png: diff > 0 ? outPath : '' };
}


let page = null;
for (let i = 0; i < 120; i++) {
  try {
    const list = await fetch(`http://127.0.0.1:${cfg.cdpPort}/json/list`).then(r => r.json());
    page = list.find(t => t.type === 'page' && !String(t.url || '').startsWith('chrome-extension://'));
    if (page && page.webSocketDebuggerUrl) break;
    if (i >= 10 && i % 10 === 0) {
      try {
        let np = await fetch(`http://127.0.0.1:${cfg.cdpPort}/json/new?about:blank`, { method: 'PUT' });
        if (!np.ok) np = await fetch(`http://127.0.0.1:${cfg.cdpPort}/json/new?about:blank`);
      } catch {}
    }
  } catch {}
  await sleep(400);
}
if (!page) throw new Error('No Edge CDP page target available');

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let seq = 0;
const pending = new Map();
const jsErrors = [];
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const p = pending.get(m.id);
    pending.delete(m.id);
    m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
  } else if (m.method === 'Runtime.exceptionThrown') {
    const ex = m.params?.exceptionDetails;
    jsErrors.push((ex?.text || 'exception') + ': ' + (ex?.exception?.description || '').slice(0, 200));
  } else if (m.method === 'Log.entryAdded' && m.params?.entry?.level === 'error') {
    jsErrors.push('LogError: ' + (m.params.entry.text || '').slice(0, 200));
  }
};

function send(method, params = {}, timeout = 30000) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }
    }, timeout);
  });
}

async function ev(expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error('JS eval err: ' + JSON.stringify(r.exceptionDetails).slice(0, 500));
  return r.result ? r.result.value : null;
}

const auditExpr = `(() => {
  const docEl = document.documentElement;
  const scrollW = docEl.scrollWidth;
  const clientW = docEl.clientWidth;
  const vh = window.innerHeight;
  const scrollH = docEl.scrollHeight;

  const zones = [...document.querySelectorAll('[data-zone]')];
  const zoneIds = [...new Set(zones.map(z => String(z.getAttribute('data-zone') || '').trim()))];
  const aboveFoldZones = zones.filter(z => {
    const r = z.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top < vh;
  }).map(z => String(z.getAttribute('data-zone') || '').trim());

  const detailsEls = [...document.querySelectorAll('details')];
  const collapsedDetails = detailsEls.filter(d => !d.hasAttribute('open')).length;

  const clone = document.body.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,template').forEach(x => x.remove());
  clone.querySelectorAll('details:not([open])').forEach(d => {
    [...d.children].forEach(ch => { if (ch.tagName !== 'SUMMARY') ch.remove(); });
  });
  const visibleText = (clone.innerText || clone.textContent || '').replace(/\\s+/g, ' ').trim();

  // Visual bars in Zone 3
  const z3 = document.querySelector('[data-zone="3"]');
  let zone3Bars = 0;
  if (z3) {
    zone3Bars = [...z3.querySelectorAll('progress,svg,[role="progressbar"],.bar-fill,.fill,.bar,[style*="width"]')].filter(el => {
      const r = el.getBoundingClientRect();
      return r.width > 2 && r.height > 2;
    }).length;
  }

  // Aesthetic telemetry: typography, wrapping order IDs, colors, contrast
  function isVisibleNode(el) {
    const det = el.closest('details:not([open])');
    if (det && !el.closest('summary')) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function parseRgb(str) {
    const m = String(str || '').match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
    if (!m) return null;
    return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] };
  }
  function relLum(c) {
    const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  }
  function contrastRatio(c1, c2) {
    const l1 = relLum(c1), l2 = relLum(c2);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }
  function effectiveBg(el) {
    let cur = el;
    while (cur && cur.nodeType === 1) {
      const cs = getComputedStyle(cur);
      if (cs.backgroundImage && cs.backgroundImage !== 'none' && cs.backgroundImage.includes('gradient')) return null;
      const c = parseRgb(cs.backgroundColor);
      if (c && c.a > 0.85) return c;
      cur = cur.parentElement;
    }
    return parseRgb(getComputedStyle(document.body).backgroundColor) || { r: 15, g: 23, b: 42, a: 1 };
  }

  const fontHist = {};
  const textColors = new Set();
  const bgColors = new Set();
  const tinySamples = [];
  const oversizedSamples = [];
  const lowContrastSamples = [];
  const wrappedOrderIds = [];
  let minContrast = 21;

  const allVis = [...document.body.querySelectorAll('*')].filter(isVisibleNode);
  for (const el of allVis.slice(0, 450)) {
    const cs = getComputedStyle(el);
    const bg = parseRgb(cs.backgroundColor);
    if (bg && bg.a > 0.2) bgColors.add(cs.backgroundColor);

    const hasOwnText = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (!hasOwnText) continue;

    const txt = el.textContent.trim();
    const fsPx = Math.round(parseFloat(cs.fontSize) * 10) / 10;
    const fk = String(fsPx) + 'px';
    fontHist[fk] = (fontHist[fk] || 0) + 1;
    textColors.add(cs.color);

    if (fsPx < 11) tinySamples.push({ fs: fsPx, txt: txt.slice(0, 30) });
    if (fsPx > 48) oversizedSamples.push({ fs: fsPx, txt: txt.slice(0, 30) });

    if (/^(?:C-\\d{4,8}-\\d{2,4}|…-\\d{2,4}|C-21-\\d{2,4})$/.test(txt)) {
      const r = el.getBoundingClientRect();
      const lh = parseFloat(cs.lineHeight) || (fsPx * 1.4);
      if (r.height > Math.max(26, lh * 1.65)) {
        wrappedOrderIds.push({ txt, h: Math.round(r.height), w: Math.round(r.width) });
      }
    }

    const fg = parseRgb(cs.color);
    const ebg = effectiveBg(el);
    if (fg && fg.a > 0.6 && ebg) {
      const ratio = Math.round(contrastRatio(fg, ebg) * 100) / 100;
      if (ratio < minContrast) minContrast = ratio;
      const fw = parseInt(cs.fontWeight, 10) || 400;
      const isLarge = fsPx >= 24 || (fsPx >= 18.66 && fw >= 700);
      const req = isLarge ? 3.0 : 4.5;
      if (ratio < req && lowContrastSamples.length < 6) {
        lowContrastSamples.push({ ratio, req, fs: fsPx, fg: cs.color, txt: txt.slice(0, 30) });
      }
    }
  }

  // Max line density estimate across Zone 1 alerts & Zone 2 cards
  const tallFirstScreenCards = [...document.querySelectorAll('[data-zone="1"] > *, [data-zone="2"] > *, .kpi-card, .card, .alert-item')].filter(el => {
    if (!isVisibleNode(el)) return false;
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const lh = parseFloat(cs.lineHeight) || 20;
    return el.children.length <= 4 && r.height > lh * 5.2 && r.width < 460;
  }).length;

  // V3.1 General Geometric & Semantic Probes (P0-1)
  // 1. Duplicate non-nav visible text detection
  const textFreq = new Map();
  for (const el of allVis.slice(0, 450)) {
    if (el.closest('nav')) continue;
    const hasOwn = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (!hasOwn) continue;
    const norm = el.textContent.replace(/\\s+/g, ' ').trim();
    if (norm.length >= 4 && norm.length <= 80) {
      textFreq.set(norm, (textFreq.get(norm) || 0) + 1);
    }
  }
  const duplicateTexts = [...textFreq.entries()].filter(([_, c]) => c >= 2).map(([t, c]) => ({ text: t, count: c })).slice(0, 12);

  // 2. 12x8 Viewport Occupancy Grid, Blank Ratio, Centroid, Card Height Alignment
  const cols = 12, rows = 8;
  const grid = Array.from({ length: rows }, () => Array(cols).fill(0));
  const cellW = Math.max(1, clientW / cols), cellH = Math.max(1, vh / rows);
  for (const el of allVis) {
    if (el === document.body || el === document.documentElement || el.id === 'main' || el.id === 'screen1' || el.classList.contains('wrap')) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= vh || r.width > clientW * 0.96 && r.height > vh * 0.7) continue;
    const hasInk = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 0) || el.tagName === 'SVG' || el.tagName === 'BUTTON' || el.id === 'donut';
    if (!hasInk) continue;
    const c0 = Math.max(0, Math.floor(r.left / cellW)), c1 = Math.min(cols - 1, Math.floor(Math.max(0, r.right - 1) / cellW));
    const r0 = Math.max(0, Math.floor(r.top / cellH)), r1 = Math.min(rows - 1, Math.floor(Math.max(0, r.bottom - 1) / cellH));
    for (let rr = r0; rr <= r1; rr++) for (let cc = c0; cc <= c1; cc++) grid[rr][cc] = 1;
  }
  const grid12x8_ascii = grid.map(row => row.map(v => v ? '#' : '.').join('')).join('\\\\n');
  let filledCells = 0, sumX = 0, sumY = 0, leftInk = 0, rightInk = 0;
  for (let rr = 0; rr < rows; rr++) {
    for (let cc = 0; cc < cols; cc++) {
      if (grid[rr][cc]) {
        filledCells++;
        sumX += (cc + 0.5) / cols;
        sumY += (rr + 0.5) / rows;
        if (cc < cols / 2) leftInk++; else rightInk++;
      }
    }
  }
  const blankRatio = Math.round((1 - filledCells / (cols * rows)) * 1000) / 1000;
  const cx = filledCells ? sumX / filledCells : 0.5, cy = filledCells ? sumY / filledCells : 0.5;
  const centroidOffset = [Math.round(Math.abs(cx - 0.5) * 1000) / 1000, Math.round(Math.abs(cy - 0.5) * 1000) / 1000];
  const lrWeightRatio = rightInk ? Math.round((leftInk / rightInk) * 100) / 100 : leftInk;

  // Sibling card height differences (KPI cards & Fold cards)
  const kpiHeights = [...document.querySelectorAll('.kpi')].filter(isVisibleNode).map(e => Math.round(e.getBoundingClientRect().height));
  const foldHeights = [...document.querySelectorAll('details.zf, details.zone')].filter(isVisibleNode).map(e => Math.round(e.getBoundingClientRect().height));
  const kpiHeightSpread = kpiHeights.length > 1 ? Math.max(...kpiHeights) - Math.min(...kpiHeights) : 0;
  const foldHeightSpread = foldHeights.length > 1 ? Math.max(...foldHeights) - Math.min(...foldHeights) : 0;

  // 3. Decorative unbound bars / stripes & duplicate encodings
  const decorBars = [];
  for (const el of allVis) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const hasTxt = (el.innerText || '').trim().length > 0;
    const hasAria = el.hasAttribute('aria-valuenow') || el.tagName === 'PROGRESS';
    const hasBg = (cs.backgroundImage && cs.backgroundImage !== 'none') || (parseRgb(cs.backgroundColor) && parseRgb(cs.backgroundColor).a > 0.15);
    if (!hasTxt && !hasAria && hasBg && r.width >= 80 && r.height >= 2 && r.height <= 12) {
      decorBars.push({ selector: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).join('.') : ''), w: Math.round(r.width), h: Math.round(r.height) });
    }
    if (el.classList && el.classList.contains('kpi') && parseFloat(cs.borderLeftWidth) >= 2.5) {
      decorBars.push({ selector: '.kpi[border-left]', borderLeft: cs.borderLeftWidth + ' ' + cs.borderLeftColor });
    }
  }
  const z3HasDonut = !!(z3 && z3.querySelector('#donut, .donut'));
  const z3FullBars = z3 ? [...z3.querySelectorAll('.fr, .bar-fill')].filter(isVisibleNode).length : 0;
  const duplicateEncodings = (z3HasDonut && z3FullBars > 0 && (z3.innerText || '').includes('100%')) ? ['zone3_donut_plus_100pct_bar'] : [];

  // 4. Zero-value accented check (0 value still using saturated semantic color)
  const zeroValueAccented = [];
  for (const el of document.querySelectorAll('.kpi-val, .acount .num')) {
    if (!isVisibleNode(el)) continue;
    const vTxt = (el.textContent || '').trim();
    if (vTxt === '0') {
      const c = parseRgb(getComputedStyle(el).color);
      if (c) {
        const mx = Math.max(c.r, c.g, c.b), mn = Math.min(c.r, c.g, c.b);
        const sat = mx > 0 ? (mx - mn) / mx : 0;
        if (sat > 0.32) {
          zeroValueAccented.push({ selector: el.className || el.id || el.tagName, color: getComputedStyle(el).color, sat: Math.round(sat * 100) / 100 });
        }
      }
    }
  }

  const bodyBg = parseRgb(getComputedStyle(document.body).backgroundColor) || { r: 15, g: 23, b: 42, a: 1 };
  const rejectHook = (document.documentElement.innerHTML || '').includes('/api/v3/reject');
  const hasDataErr = Boolean(document.querySelector('#err.on, .data-err.on, [data-degraded="1"]'));
  const degradedBanner = /数据加载失败|降级模式|接口不可达/.test(visibleText);

  return {
    scrollW, clientW, overflowX: scrollW > clientW + 1, scrollH, innerHeight: vh,
    dataReady: document.body.getAttribute('data-ready'),
    zoneIds, aboveFoldZones: [...new Set(aboveFoldZones)],
    collapsedDetails, totalDetails: detailsEls.length,
    visibleText, visibleChars: visibleText.length,
    zone3Bars,
    fontHist,
    tinyTextCount: tinySamples.length, tinySamples: tinySamples.slice(0, 5),
    oversizedTextCount: oversizedSamples.length, oversizedSamples: oversizedSamples.slice(0, 5),
    distinctTextColors: textColors.size, distinctBgColors: bgColors.size,
    bodyBgLum: Math.round(relLum(bodyBg) * 1000) / 1000,
    minContrast: Math.round(minContrast * 100) / 100,
    lowContrastSamples,
    wrappedOrderIds: wrappedOrderIds.slice(0, 5),
    tallFirstScreenCards,
    hasRejectHook: rejectHook,
    hasDataErr,
    degradedBanner,
    duplicateTexts,
    grid12x8_ascii,
    blankRatio,
    centroidOffset,
    lrWeightRatio,
    kpiHeightSpread,
    foldHeightSpread,
    decorBars: decorBars.slice(0, 8),
    duplicateEncodings,
    zeroValueAccented,
  };
})()`;

await send('Page.enable');
await send('Runtime.enable');
await send('Log.enable');

const viewports = {};
for (let idx = 0; idx < cfg.specs.length; idx++) {
  const sp = cfg.specs[idx];
  await send('Emulation.setDeviceMetricsOverride', {
    width: sp.w, height: sp.h, deviceScaleFactor: 1, mobile: sp.mobile
  });
  if (idx === 0) {
    await send('Page.navigate', { url: cfg.url });
    const tStart = Date.now();
    const deadline = tStart + 18000;
    while (Date.now() < deadline) {
      const waitFallback = Date.now() - tStart >= 10000;
      try {
        const ok = await ev(`(() => {
          if (!document.body || document.readyState !== 'complete') return false;
          const r = document.body.getAttribute('data-ready') === '1';
          if (r) return true;
          const z = document.querySelectorAll('[data-zone]').length >= __MINZ__;
          const t = (document.body.innerText || '').trim();
          return ${waitFallback} && z && !t.startsWith('加载中') && !t.includes('刷新中');
        })()`);
        if (ok) break;
      } catch {}
      await sleep(250);
    }
    await sleep(600);
  } else {
    await sleep(450);
  }

  const metrics = await ev(auditExpr);
  const shot = await send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false }, 60000);
  fs.writeFileSync(path.join(cfg.shotsDir, sp.file), Buffer.from(shot.data, 'base64'));
  viewports[`${sp.w}x${sp.h}`] = metrics;
}

const outerHtml = await ev('document.documentElement.outerHTML');
fs.writeFileSync(cfg.domPath, outerHtml || '', 'utf8');
fs.writeFileSync(cfg.telemetryPath, JSON.stringify({ jsErrors, viewports }, null, 2), 'utf8');

// ---------------- V4.0-P2 规格断言执行（与上方通用遥测互不影响：任何异常只记入 assert 输出）
if (cfg.assert && Array.isArray(cfg.assert.groups) && cfg.assert.groups.length) {
  const A = cfg.assert;
  const out = { targets: {}, errors: {}, shots: [], prep: {}, vshots: {}, vshot_err: {} };
  const VP = { 1440: { w: 1440, h: 900, mobile: false }, 1200: { w: 1200, h: 800, mobile: false }, 390: { w: 390, h: 844, mobile: true } };
  const safeName = s => String(s).replace(/[^A-Za-z0-9_-]+/g, '_').slice(0, 60);
  const waitReady = async (degraded) => {
    const t0 = Date.now();
    while (Date.now() - t0 < 18000) {
      try {
        const ok = await ev(`(() => {
          if (!document.body || document.readyState !== 'complete') return false;
          const z = document.querySelectorAll('[data-zone]').length >= __MINZ__;
          if (${degraded ? 'true' : 'false'}) return z && (Date.now() - performance.timeOrigin) > 3000;
          if (document.body.getAttribute('data-ready') === '1') return true;
          const t = (document.body.innerText || '').trim();
          return ${Date.now() - t0 >= 10000} && z && !t.startsWith('加载中') && !t.includes('刷新中');
        })()`);
        if (ok) break;
      } catch {}
      await sleep(250);
    }
    await sleep(600);
  };
  const prepStep = async (step) => {
    if (step === 'default' || step === 'degraded') return true;
    if (step.startsWith('zone:')) {
      const n = step.slice(5);
      const r = await ev(`(() => { const el = document.getElementById('z${n}'); if (!el) return 'zone not found: z${n}';
        location.hash = '#z${n}'; window.dispatchEvent(new HashChangeEvent('hashchange')); return true; })()`);
      await sleep(500);
      return r;
    }
    if (step.startsWith('drawer:')) {
      const code = step.slice(7);
      const r = await ev(`(() => { const el = document.querySelector('.ocard[data-code=' + JSON.stringify(${JSON.stringify(code)}) + ']');
        if (!el) return 'order card not found: ' + ${JSON.stringify(code)}; el.click(); return true; })()`);
      if (r !== true) return r;
      for (let i = 0; i < 12; i++) {
        await sleep(250);
        const open = await ev(`(() => { const d = document.getElementById('drawer'); return !!d && (d.classList.contains('open') || d.getAttribute('aria-hidden') === 'false'); })()`);
        if (open) return true;
      }
      return 'drawer did not open for ' + code;
    }
    if (step.startsWith('click:')) {
      const sel = step.slice(6);
      const r = await ev(`(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return 'click target not found: ' + ${JSON.stringify(sel)}; el.click(); return true; })()`);
      await sleep(450);
      return r;
    }
    return 'unknown state step: ' + step;
  };
  const measureExpr = defs => `(() => {
    const DEFS = ${JSON.stringify(defs)};
    const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; };
    const camel = s => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    const num = v => { if (typeof v === 'number') return v; const m = /^\\\\s*(-?\\\\d+(?:\\\\.\\\\d+)?)(px|ms|s)?\\\\s*$/.exec(String(v));
      if (!m) return v; let x = parseFloat(m[1]); if (m[2] === 's') x *= 1000; return x; };
    const over = el => (el.scrollWidth > el.clientWidth + 1) || (el.scrollHeight > el.clientHeight + 1);
    const measure = (el, metric) => {
      if (metric.startsWith('rect.')) { const r = el.getBoundingClientRect(); return Math.round(r[metric.slice(5)] * 10) / 10; }
      if (metric.startsWith('style.')) return num(getComputedStyle(el)[camel(metric.slice(6))]);
      if (metric === 'text') return (el.innerText || el.textContent || '').replace(/\\\\s+/g, ' ').trim().slice(0, 500);
      if (metric === 'text.truncated') return over(el);
      if (metric === 'text.ellipsis_ok') { if (!over(el)) return true; const cs = getComputedStyle(el);
        return cs.textOverflow === 'ellipsis' || (!!cs.webkitLineClamp && cs.webkitLineClamp !== 'none'); }
      return null;
    };
    const desc = el => (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\\\s+/).slice(0, 2).join('.') : '')
      + ' "' + (el.innerText || el.textContent || '').replace(/\\\\s+/g, ' ').trim().slice(0, 40) + '"');
    const res = {};
    for (const d of DEFS) {
      try {
        if (d.metric === 'doc.overflowX') { const e = document.documentElement; res[d.id] = { values: [Math.max(0, e.scrollWidth - e.clientWidth)], n: 1 }; continue; }
        if (d.metric === 'js') { const v = (0, eval)(d.expr); const arr = Array.isArray(v) ? v : [v]; res[d.id] = { values: arr.slice(0, 200), n: arr.length }; continue; }
        let els = [...document.querySelectorAll(d.selector)];
        if (d.visible_only !== false) els = els.filter(vis);
        if (d.metric === 'count') { res[d.id] = { values: [els.length], n: els.length, samples: els.slice(0, 3).map(desc) }; continue; }
        res[d.id] = { values: els.slice(0, 200).map(el => measure(el, d.metric)), n: els.length, samples: els.slice(0, 3).map(desc) };
      } catch (e) { res[d.id] = { error: String(e && e.message || e).slice(0, 300) }; }
    }
    return res;
  })()`;
  for (const g of A.groups) {
    for (const tgt of g.targets) {
      const steps = g.state.split('+');
      const urlKey = tgt + (steps.includes('degraded') ? '_degraded' : '');
      const url = A.urls[urlKey];
      out.targets[tgt] = out.targets[tgt] || {};
      out.targets[tgt][g.state] = {};
      const ekey = tgt + '|' + g.state;
      if (!url) { out.errors[ekey] = 'no preview url for ' + urlKey; continue; }
      try {
        const v0 = VP[g.viewports[0]];
        await send('Emulation.setDeviceMetricsOverride', { width: v0.w, height: v0.h, deviceScaleFactor: 1, mobile: v0.mobile });
        await send('Page.navigate', { url });
        await waitReady(steps.includes('degraded'));
        let prepOk = true;
        for (const st of steps) {
          const r = await prepStep(st);
          if (r !== true) { prepOk = false; out.errors[ekey] = 'state prep failed: ' + r; break; }
        }
        out.prep[ekey] = prepOk;
        if (!prepOk) continue;
        const defs = g.ids.map(id => A.defs[id]);
        let frozen = false;
        for (let vi = 0; vi < g.viewports.length; vi++) {
          const v = VP[g.viewports[vi]];
          if (vi > 0) { await send('Emulation.setDeviceMetricsOverride', { width: v.w, height: v.h, deviceScaleFactor: 1, mobile: v.mobile }); await sleep(450); }
          out.targets[tgt][g.state][String(g.viewports[vi])] = await ev(measureExpr(defs));
          if (tgt === 'sandbox') {
            const fn = `assert_${safeName(g.state)}_${v.w}x${v.h}.png`;
            try {
              const shot = await send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false }, 60000);
              fs.writeFileSync(path.join(cfg.shotsDir, fn), Buffer.from(shot.data, 'base64'));
              out.shots.push({ state: g.state, viewport: v.w, file: path.join(cfg.shotsDir, fn) });
            } catch (e) { out.shots.push({ state: g.state, viewport: v.w, error: String(e).slice(0, 200) }); }
          }
          // V4.0-P3 像素比对截图：冻结动效与定时器、隐藏动态区（保留占位），再整页/元素裁剪截图
          for (const vid of (g.visual || [])) {
            const vd = A.vdefs[vid];
            const vkey = tgt + '|' + g.state + '|' + v.w + '|' + vid;
            try {
              if (!frozen) {
                await ev(`(() => { const st = document.createElement('style'); st.id = '__v4freeze';
                  st.textContent = '*,*::before,*::after{transition:none!important;animation:none!important;caret-color:transparent!important}';
                  document.head.appendChild(st); const hi = setTimeout(() => 0, 0);
                  for (let i = 1; i <= hi + 50; i++) { clearTimeout(i); clearInterval(i); } return true; })()`);
                frozen = true;
                await sleep(200);
              }
              const clip = await ev(`(() => { const MASK = ${JSON.stringify(vd.mask || [])};
                let st = document.getElementById('__v4mask'); if (!st) { st = document.createElement('style'); st.id = '__v4mask'; document.head.appendChild(st); }
                st.textContent = MASK.map(s => s + '{visibility:hidden!important}').join('');
                const sel = ${JSON.stringify(vd.selector || '')};
                if (sel) { const el = document.querySelector(sel); if (!el) return { error: 'clip target not found: ' + sel };
                  const r = el.getBoundingClientRect(); return { x: Math.max(0, Math.floor(r.left + scrollX)), y: Math.max(0, Math.floor(r.top + scrollY)), width: Math.max(1, Math.ceil(r.width)), height: Math.max(1, Math.ceil(r.height)) }; }
                const e = document.documentElement;
                return ${vd.full_page ? 'true' : 'false'} ? { x: 0, y: 0, width: Math.max(e.scrollWidth, innerWidth), height: Math.min(Math.max(e.scrollHeight, innerHeight), 12000) }
                                                         : { x: 0, y: 0, width: innerWidth, height: innerHeight }; })()`);
              if (clip && clip.error) { out.vshot_err[vkey] = clip.error; }
              else {
                await sleep(120);
                const shot = await send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: true,
                                                                   clip: { x: clip.x, y: clip.y, width: clip.width, height: clip.height, scale: 1 } }, 90000);
                const vf = path.join(cfg.shotsDir, `vis_${tgt}_${safeName(g.state)}_${v.w}_${vid}.png`);
                fs.writeFileSync(vf, Buffer.from(shot.data, 'base64'));
                out.vshots[vkey] = vf;
              }
              await ev(`(() => { const st = document.getElementById('__v4mask'); if (st) st.textContent = ''; return true; })()`);
            } catch (e) { out.vshot_err[vkey] = String(e && e.message || e).slice(0, 300); }
          }
        }
      } catch (e) { out.errors[ekey] = String(e && e.message || e).slice(0, 400); }
    }
  }
  // V4.0-P3 逐像素比对（沙箱 vs 主仓生产版，同状态同视口）
  out.visual = {};
  for (const g of A.groups) for (const vid of (g.visual || [])) for (const vp of g.viewports) {
    const ks = 'sandbox|' + g.state + '|' + vp + '|' + vid, kb = 'baseline|' + g.state + '|' + vp + '|' + vid;
    let r;
    if (!out.vshots[ks] || !out.vshots[kb]) {
      r = { error: out.vshot_err[ks] ? ('沙箱截图失败: ' + out.vshot_err[ks]) : (out.vshot_err[kb] ? ('生产版截图失败: ' + out.vshot_err[kb]) : ('缺截图: ' + (!out.vshots[ks] ? 'sandbox' : 'baseline'))) };
    } else {
      try {
        r = v4PngDiff(out.vshots[ks], out.vshots[kb], A.vdefs[vid].threshold, path.join(cfg.shotsDir, `visdiff_${safeName(g.state)}_${vp}_${vid}.png`));
        r.sandbox_png = out.vshots[ks]; r.baseline_png = out.vshots[kb];
      } catch (e) { r = { error: String(e && e.message || e).slice(0, 300) }; }
    }
    out.visual[g.state] = out.visual[g.state] || {};
    out.visual[g.state][String(vp)] = out.visual[g.state][String(vp)] || {};
    out.visual[g.state][String(vp)][vid] = r;
  }
  fs.writeFileSync(A.outPath, JSON.stringify(out, null, 1), 'utf8');
}
try { ws.close(); } catch {}
process.exit(0);
""".replace("__CFG_JSON__", json.dumps(cfg_obj, ensure_ascii=False)).replace("__MINZ__", str(min_zones))
            if str(_v4f.get("probe_regex_fix") or "off") in ("observe", "enforce"):
                mjs_src, probe_fix_info = fix_probe_template_escapes(mjs_src)
            atomic_write_text(probe_mjs, mjs_src)
            node_res = subprocess.run(
                [NODE_EXE, probe_mjs],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=node_timeout,
            )
            checks["cdp_node_rc"] = node_res.returncode
            if node_res.returncode != 0:
                _tel_fresh = os.path.exists(telemetry_path) and os.path.getmtime(telemetry_path) >= probe_t0
                if _tel_fresh:
                    checks["probe_degraded"] = True
                else:
                    # c004fix: 探针盲(Edge CDP未起)≠工件缺陷——env_fail 短路，不生成幻影清单打回返工
                    failures.append("[ENV] Edge CDP 探针未起（探针环境故障，非工件缺陷；勿按缺陷返工）rc=%s" % node_res.returncode)
                    return {"all_passed": False, "checks": checks, "failures": failures, "screenshots": [], "env_fail": True}
        except Exception as exc:
            failures.append(f"CDP 浏览器探针执行异常: {exc}")
        finally:
            if edge_proc is not None:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(edge_proc.pid)], capture_output=True, timeout=8)
                except Exception:
                    try:
                        edge_proc.kill()
                    except Exception:
                        pass
            shutil.rmtree(edge_udd, ignore_errors=True)
            _assert_stack.close()

        for w, h, _, fname in [
            (1440, 900, False, "v21_1440x900.png"),
            (1200, 800, False, "v21_1200x800.png"),
            (390, 844, True, "v21_390x844.png"),
        ]:
            sp = os.path.join(shots_dir, fname)
            ok_png, pw, ph = png_dimensions(sp)
            sz = os.path.getsize(sp) if os.path.exists(sp) else 0
            shots_info.append({"file": sp, "ok": ok_png and pw == w and ph == h, "width": pw, "height": ph, "bytes": sz})
            if not ok_png or sz < 5000 or pw != w or ph != h:
                failures.append(f"截图失败或尺寸不符: {fname} (ok={ok_png}, {pw}x{ph} vs {w}x{h}, {sz}B)")

    if os.path.exists(dom_dump_path):
        dom_html = redact(open(dom_dump_path, "r", encoding="utf-8", errors="replace").read(), token)
        atomic_write_text(dom_dump_path, dom_html)
    if os.path.exists(telemetry_path):
        try:
            cdp_data = json.load(open(telemetry_path, "r", encoding="utf-8"))
        except Exception:
            cdp_data = {}

    parser = _DomAuditParser()
    parser.feed(dom_html)
    parser.close()

    viewports_data = cdp_data.get("viewports") or {}
    vp_desktop = viewports_data.get("1440x900") or viewports_data.get("1200x800") or {}
    js_errors = cdp_data.get("jsErrors") or []

    # Prefer live rendered visible text from CDP DOM; fall back to HTMLParser
    visible_text = redact(vp_desktop.get("visibleText") or " ".join(parser.visible_chunks), token)
    vis_chars = len(visible_text)
    path_hits = RAW_PATH_RE.findall(visible_text)
    sql_hits = RAW_SQL_SCHEMA_RE.findall(visible_text)
    char_icon_hits = re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2460-\u24FF\uFE0F]", visible_text)

    zones_raw = set(vp_desktop.get("zoneIds") or []) | parser.zones_found
    zones_ok = sorted([z for z in zones_raw if z.isdigit() and 1 <= int(z) <= 99], key=int)
    # c003fix: 期望区改为基线派生——以主仓 v2.html 的 <section id="zN"> 集合为准
    # （当前基线 z1..z13；z13 实况区在生产缺 data-zone 属性属基线债，002 起补齐）。
    _baseline_txt = ""
    try:
        _baseline_txt = open(main_target_path, encoding="utf-8", errors="replace").read()
    except Exception:
        pass
    baseline_zones = set(re.findall(r'<section[^>]*\bid="z(\d+)"', _baseline_txt)) or set(
        re.findall(r'data-zone="(\d+)"', _baseline_txt)
    )
    missing_zones = sorted([z for z in baseline_zones if z not in zones_raw], key=int)
    collapsed_details = int(vp_desktop.get("collapsedDetails", parser.collapsed_details))
    total_details = int(vp_desktop.get("totalDetails", parser.total_details))
    zone3_bars = int(vp_desktop.get("zone3Bars", parser.zone3_visual_bars))

    style_all = "\n".join(parser.style_blocks) + "\n" + raw_text
    has_media = "@media" in style_all
    has_overflow_guard = "overflow-x" in style_all and "hidden" in style_all
    stuck_loading = (
        visible_text.strip().startswith("加载中")
        or visible_text.count("加载中") >= 3
        or (vp_desktop.get("dataReady") != "1" and ("刷新中" in visible_text or "待取数" in visible_text))
    )

    overflow_vps = [k for k, v in viewports_data.items() if v.get("overflowX")]
    min_contrast = min([float(v.get("minContrast", 21.0)) for v in viewports_data.values()], default=21.0)
    low_contrast_all = []
    for k, v in viewports_data.items():
        for samp in (v.get("lowContrastSamples") or []):
            low_contrast_all.append({**samp, "viewport": k})
    tiny_count = max([int(v.get("tinyTextCount", 0)) for v in viewports_data.values()], default=0)
    wrapped_ids = vp_desktop.get("wrappedOrderIds") or []

    css_only = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", raw_text, flags=re.S | re.I))
    css_non_font = re.sub(r"@font-face\s*\{[^{}]*\}", "", css_only, flags=re.S | re.I)
    data_degraded_on_200 = bool(checks.get("data_json_200") and (vp_desktop.get("hasDataErr") or vp_desktop.get("degradedBanner")))

    checks.update(
        {
            "zones_present_count": len(zones_ok),
            "missing_zones": missing_zones,
            "baseline_zone_count": len(baseline_zones),
            "extra_zones": sorted([z for z in zones_raw if z.isdigit() and z not in baseline_zones], key=int),
            "above_fold_zones_1440": (viewports_data.get("1440x900") or {}).get("aboveFoldZones", []),
            "above_fold_zones_1200": (viewports_data.get("1200x800") or {}).get("aboveFoldZones", []),
            "collapsed_details_count": collapsed_details,
            "total_details_count": total_details,
            "initial_visible_chars": vis_chars,
            "raw_path_hits": len(path_hits),
            "raw_sql_schema_hits": len(sql_hits),
            "raw_sql_schema_samples": sql_hits[:5],
            "emoji_char_icon_hits": len(char_icon_hits),
            "emoji_char_icon_samples": char_icon_hits[:8],
            "zone3_visual_bars": zone3_bars,
            "has_viewport_meta": parser.has_viewport_meta or ('name="viewport"' in raw_text.lower() or "name='viewport'" in raw_text.lower()),
            "has_responsive_css": has_media and has_overflow_guard,
            "stuck_loading": stuck_loading,
            "data_degraded_on_200": data_degraded_on_200,
            "deliverable_bytes": len(raw_bytes),
            "non_font_css_bytes": len(css_non_font.encode("utf-8")),
            "js_errors": js_errors[:5],
            "overflow_viewports": overflow_vps,
            "min_contrast_ratio": min_contrast,
            "low_contrast_samples": low_contrast_all[:4],
            "tiny_text_count": tiny_count,
            "oversized_text_count": int(vp_desktop.get("oversizedTextCount", 0)),
            "font_size_hist": vp_desktop.get("fontHist") or {},
            "distinct_text_colors": vp_desktop.get("distinctTextColors"),
            "distinct_bg_colors": vp_desktop.get("distinctBgColors"),
            "body_bg_lum": vp_desktop.get("bodyBgLum"),
            "wrapped_order_ids": wrapped_ids,
            "has_reject_backdoor_hook": bool(vp_desktop.get("hasRejectHook")) or ("/api/v3/reject" in raw_text),
            "duplicate_texts": vp_desktop.get("duplicateTexts") or [],
            "blank_ratio_1440": vp_desktop.get("blankRatio"),
            "centroid_offset_1440": vp_desktop.get("centroidOffset"),
            "lr_weight_ratio_1440": vp_desktop.get("lrWeightRatio"),
            "kpi_height_spread_px": vp_desktop.get("kpiHeightSpread", 0),
            "fold_height_spread_px": vp_desktop.get("foldHeightSpread", 0),
            "decor_bars": vp_desktop.get("decorBars") or [],
            "duplicate_encodings": vp_desktop.get("duplicateEncodings") or [],
            "zero_value_accented": vp_desktop.get("zeroValueAccented") or [],
        }
    )

    if js_errors:
        failures.append(f"页面运行时 JS 报错 ({len(js_errors)} 处): {js_errors[:2]}")
    if data_degraded_on_200:
        failures.append("接口 /data.json 返回 200 但页面显示数据加载失败/降级告警（前端取数或脚本逻辑异常）")
    if missing_zones:
        failures.append(f"缺少 data-zone 锚点: {missing_zones}")
    if overflow_vps:
        failures.append(f"以下视口存在横向滚动溢出 (scrollWidth > clientWidth): {overflow_vps}")
    if path_hits:
        failures.append(f"默认可见区存在 {len(path_hits)} 处裸路径/文件名 (例: {path_hits[:3]})")
    if sql_hits:
        failures.append(f"默认可见区存在 {len(sql_hits)} 处 SQL/库表字段名裸露 (例: {sql_hits[:5]})")
    _v4p = load_v4_flags()
    if collapsed_details < 6 and not _v4p.get("drop_details_gate"):
        failures.append(f"默认折叠的次级区/工程细节 <details> 不足 ({collapsed_details} < 6，违反规则 B/E)")
    if char_icon_hits and _v4p.get("char_icon_hard_fail"):
        failures.append(f"默认可见区存在 {len(char_icon_hits)} 处 emoji/字符图标 (例: {char_icon_hits[:5]})，违反 UISPEC §10.1")
    if vis_chars > INITIAL_VISIBLE_CHAR_LIMIT:
        failures.append(f"首屏默认可见正文 {vis_chars} 字符过多 (上限 {INITIAL_VISIBLE_CHAR_LIMIT}，文字墙未压密)")
    if vis_chars < 80:
        failures.append(f"首屏可见正文仅 {vis_chars} 字符，疑似脚本报错白屏")
    if zone3_bars < 1:
        failures.append("Zone 3 (漏斗区) 未检测到图形条元素 (<progress>/<svg>/带 width 进度条)")
    if not checks["has_viewport_meta"] or not checks["has_responsive_css"]:
        failures.append("缺少 viewport meta 或响应式 overflow-x:hidden 保护")
    if stuck_loading:
        failures.append("渲染 DOM 仍卡在'加载中'占位，未完成 /data.json 数据注入")
    _fix_mode = str(_v4f.get("probe_regex_fix") or "off")
    if low_contrast_all and _fix_mode == "observe":
        checks["contrast_observe"] = {"min_contrast": min_contrast, "samples": low_contrast_all[:6],
                                      "note": "probe_regex_fix=observe：对比度检测已复明但暂不判 FAIL"}
    elif low_contrast_all:
        failures.append(f"存在 WCAG 对比度不足元素 (实测最低 {min_contrast} < 4.5:1): {low_contrast_all[:2]}")
    if tiny_count > 0:
        failures.append(f"存在字号过小 (<11px) 元素 ({tiny_count} 处)")
    if wrapped_ids and _fix_mode == "observe":
        checks["wrapped_ids_observe"] = wrapped_ids[:5]
    elif wrapped_ids:
        failures.append(f"存在订单号换行折行: {wrapped_ids[:3]}")
    checks["probe_regex_fix"] = {"mode": _fix_mode, **probe_fix_info}

    # V4.0-P3：源码级 UISPEC 违规计数只许降不许升（对比主仓生产版）
    if _v4f.get("lint_gate"):
        _lp = _src_metrics_pair(wt_target_path, main_target_path)
        if _lp.get("error"):
            checks["ui_lint"] = {"error": _lp["error"]}
        else:
            _rules = list(getattr(ui_lint, "GATING_RULES", ())) + ["total"]
            _up = {k[5:]: [_lp["baseline"].get(k, 0), _lp["sandbox"].get(k, 0)] for k in (f"lint.{r}" for r in _rules)
                   if _lp["sandbox"].get(k, 0) > _lp["baseline"].get(k, 0)}
            checks["ui_lint"] = {"sandbox_total": _lp["sandbox"].get("lint.total"), "baseline_total": _lp["baseline"].get("lint.total"),
                                 "increased": _up, "css_bytes": [_lp["baseline"].get("src.css_bytes"), _lp["sandbox"].get("src.css_bytes")]}
            if _up:
                failures.append("源码 UISPEC 违规数比生产版上升（只许降不许升）: " + "；".join(f"{k} {a}→{b}" for k, (a, b) in list(_up.items())[:6]))

    # V4.0-P2：规格断言判定
    assert_results: list[dict] = []
    assert_summary: dict = {}
    raw_a: dict = {}
    if assertions:
        if os.path.exists(assert_raw_path):
            try:
                raw_a = json.load(open(assert_raw_path, "r", encoding="utf-8"))
            except Exception as exc:
                raw_a = {"fatal": f"断言测量输出无法解析: {exc}"}
        else:
            raw_a = {"fatal": f"断言测量输出缺失（探针未跑到断言阶段，node rc={checks.get('cdp_node_rc')}）"}
        _data_obj = None
        try:
            _data_obj = json.loads((data_body or b"").decode("utf-8", errors="replace")) if data_body else None
        except Exception:
            _data_obj = None
        if any(is_src_metric(a.get("metric")) for a in assertions):
            raw_a["src"] = _src_metrics_pair(wt_target_path, main_target_path)
        assert_results = evaluate_assertions(assertions, raw_a, _data_obj)
        assert_summary = summarize_assertions(assert_results)
        failures.extend(assertion_result_lines(assert_results, only=("FAIL",)))
        checks["assertions"] = assert_summary

    return {
        "all_passed": len(failures) == 0,
        "assertions": assert_results,
        "assert_summary": assert_summary,
        "assert_shots": (raw_a or {}).get("shots") or [],
        "assert_raw_path": assert_raw_path if assertions else "",
        "bytes": file_size,
        "sha256": file_sha,
        "sha16": file_sha16,
        "mtime": mtime_iso,
        "checks": checks,
        "failures": failures,
        "screenshots": shots_info,
        "dom_dump_path": dom_dump_path,
        "telemetry_path": telemetry_path,
        "visible_text_preview": visible_text[:550],
    }


EVENTS_JSONL_PATH = os.path.join(STEWARD_ROOT, "state", "events.jsonl")
GOLDEN_ROOT_DIR = os.path.join(BP_ROOT, "golden")
TRANSCRIPTS_ROOT_DIR = os.path.join(BP_ROOT, "reports", "transcripts")
ORDER_WALL_CLOCK_MAX_SEC = 2400  # 2026-11-22 calibrate: independent order-wide wall clock killed G19 at 899s despite builder-stage fix; aligned with BUILDER_TIMEOUT_SEC 2400


def append_v3_event(order_id: str, round_no: int, phase: str, actor: str, summary: str, files_changed: list[str] | None = None, cost_cny: float = 0.0) -> None:
    try:
        os.makedirs(os.path.dirname(EVENTS_JSONL_PATH), exist_ok=True)
        rec = {
            "ts": iso_now(),
            "ts_ms": now_ms(),
            "order": order_id,
            "round": round_no,
            "phase": phase,
            "actor": actor,
            "summary": str(summary or "")[:240],
            "files_changed": files_changed or [],
            "cost": round(float(cost_cny or 0.0), 6),
        }
        with open(EVENTS_JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def save_golden_sample(order_id: str, reason: str) -> str:
    """Save current dashboard snapshot + telemetry + human rejection reason into golden/ calibration set (P2-5)."""
    try:
        ts_tag = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        gdir = os.path.join(GOLDEN_ROOT_DIR, f"{order_id}_{ts_tag}")
        os.makedirs(gdir, exist_ok=True)
        v2_p = os.path.join(STEWARD_ROOT, "dashboard", "v2.html")
        if os.path.exists(v2_p):
            shutil.copy2(v2_p, os.path.join(gdir, "v2.html"))
        ev_dir = os.path.join(BP_ROOT, "reports", "evidence", order_id)
        if os.path.exists(ev_dir):
            for sub in sorted(os.listdir(ev_dir), reverse=True):
                sp = os.path.join(ev_dir, sub)
                if sub.startswith("runner_a") and os.path.isdir(sp):
                    for fn in ("cdp_telemetry.json", "dom_dump_redacted.html"):
                        if os.path.exists(os.path.join(sp, fn)):
                            shutil.copy2(os.path.join(sp, fn), os.path.join(gdir, fn))
                    sdir = os.path.join(sp, "shots")
                    if os.path.exists(sdir):
                        for pfn in os.listdir(sdir):
                            shutil.copy2(os.path.join(sdir, pfn), os.path.join(gdir, pfn))
                    break
        atomic_write_json(os.path.join(gdir, "golden_meta.json"), {
            "order_id": order_id,
            "captured_at": iso_now(),
            "human_reject_reason": reason,
        })
        return gdir
    except Exception:
        return ""


def _infer_clause_topic(text: str, idx: int) -> str:
    t = text.lower()
    if any(k in t for k in ("版本", "v3ver", "contract", "构建时间")):
        return "top_bar_version"
    if any(k in t for k in ("时间", "时钟", "更新", "upd")):
        return "top_bar_time"
    if any(k in t for k in ("24h", "打回", "打印重做", "reject")):
        return "top_bar_reject_btn"
    if any(k in t for k in ("导航", "nav", "12 项", "圆形数字")):
        return "top_bar_nav"
    if any(k in t for k in ("告警", "蓝警", "管理税", "管理班", "工程细节")):
        return "zone1_alerts"
    if any(k in t for k in ("指标顶板", "四卡", "分解", "0 值", "降噪", "kpi")):
        return "zone2_kpis"
    if any(k in t for k in ("漏斗", "环形", "双栏", "非零态", "15 个状态")):
        return "zone3_funnel"
    if any(k in t for k in ("折叠", "9 区", "主语", "等高", "视觉降级")):
        return "zone4_12_folds"
    if any(k in t for k in ("emoji", "字符图标", "svg", "带圈数字")):
        return "global_icons_svg"
    if any(k in t for k in ("字阶", "字号", "颜色", "对比度", "装饰")):
        return "global_visual_system"
    if any(k in t for k in ("实时", "实况流", "直播", "part")):
        return "live_agent_stream"
    return f"general_clause_{idx}"


def compact_contract_on_reject(order_id: str, rework_id: str, clean_reason: str, target_rel: str) -> tuple[dict, str]:
    """Deterministic Spec Compaction State Machine: contract.json + ledger.jsonl + baseline.json (P0-2)."""
    base_ord = order_id.split("-R")[0]
    ord_dir = os.path.join(BP_ROOT, "orders", base_ord)
    os.makedirs(ord_dir, exist_ok=True)
    contract_p = os.path.join(ord_dir, "contract.json")
    ledger_p = os.path.join(ord_dir, "ledger.jsonl")
    baseline_p = os.path.join(ord_dir, "baseline.json")

    if os.path.exists(contract_p):
        try:
            contract = json.load(open(contract_p, "r", encoding="utf-8"))
        except Exception:
            contract = {}
    else:
        contract = {}

    rev = int(contract.get("rev") or 0) + 1
    old_clauses: list[dict] = contract.get("clauses") or []
    baseline: list[dict] = []
    if os.path.exists(baseline_p):
        try:
            baseline = (json.load(open(baseline_p, "r", encoding="utf-8")).get("items") or [])
        except Exception:
            baseline = []

    raw_lines = [ln.strip() for ln in clean_reason.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    new_items: list[tuple[str, str]] = []
    if len(raw_lines) <= 3:
        new_items.append((_infer_clause_topic(clean_reason, 1), clean_reason.strip()))
    else:
        chunks = re.split(r"\n(?=###\s+\d+\.|\d+\.\d+【P[012]】)", clean_reason)
        for ci, ch in enumerate(chunks, 1):
            ch_s = ch.strip()
            if not ch_s or len(ch_s) < 6:
                continue
            new_items.append((_infer_clause_topic(ch_s[:160], ci), ch_s[:900]))

    by_topic: dict[str, dict] = {c.get("topic") or c.get("id"): c for c in old_clauses if c.get("status") == "active"}
    superseded_records: list[dict] = []

    for idx, (topic, txt) in enumerate(new_items, 1):
        cid = f"C_R{rev}_{idx}"
        if topic in by_topic:
            old_c = by_topic[topic]
            old_c["status"] = "superseded"
            old_c["superseded_by"] = cid
            old_c["superseded_at_rev"] = rev
            superseded_records.append(old_c)
        by_topic[topic] = {
            "id": cid,
            "topic": topic,
            "text": txt,
            "status": "active",
            "src": rework_id,
            "rev": rev,
        }

    if superseded_records:
        with open(ledger_p, "a", encoding="utf-8") as lf:
            for sr in superseded_records:
                lf.write(json.dumps({"ts": iso_now(), "event": "superseded", "clause": sr}, ensure_ascii=False) + "\n")

    active_clauses = list(by_topic.values())
    needs_resign = (rev > 15) or (len(active_clauses) > 15)
    contract_obj = {
        "order_id": base_ord,
        "rev": rev,
        "updated_at": iso_now(),
        "goal": "一屏先回答三件事：有几单、卡在哪个状态、有没有需要处理的告警；再给细节与实时施工/质检实况流。",
        "touch_scope": [target_rel.replace("\\", "/")],
        "needs_human_resign": needs_resign,
        "clauses": active_clauses,
        "forbidden": [
            "不得改动 /data.json 数据层接口与字段名",
            "不得使用 Emoji 或 Unicode 带圈数字字符图标 (①-⑫、↺、▸)",
            "不得添加无数据绑定的满宽装饰色条或伪进度条",
            "不得在同屏重复显示版本号或数据更新时间",
            "不得直接修改主仓文件、index.html、六页静态正本、Private1/ 或 board_token.txt",
        ],
    }
    atomic_write_json(contract_p, contract_obj)

    base_summary = " · ".join([b.get("summary", "") for b in baseline[-5:] if b.get("summary")]) or "保持 13 区顺序、登录鉴权、/api/v3/reject 打回按钮与三视口零横向滚动。"
    rendered_md = (
        f"# V3.1 有效契约（`contract.json` · Rev {rev} · 单号 `{rework_id}`）\n\n"
        f"> **硬性覆盖规则**：合同以本 `contract.json` (Rev {rev}) 为唯一准绳；`rev` 越大优先级越高。本单仅含当前 `active` 条款，一切历史旧条款已归入 `ledger.jsonl` 作废。\n"
        f"> **允许修改文件范围 (`touch_scope`)**：`{contract_obj['touch_scope']}`（严禁越界修改其他文件）。\n\n"
        f"## 一、总目标\n{contract_obj['goal']}\n\n"
        f"## 二、当前有效条款（Active Clauses · 共 {len(active_clauses)} 项，必须 100% 满足）\n\n"
        + "\n\n".join([f"### [{c['id']} · topic=`{c['topic']}` · rev={c['rev']}]\n{c['text']}" for c in active_clauses])
        + "\n\n## 三、不可回退基线（`baseline.json` 回归红线，破坏任一项直接判 FAIL）\n"
        + f"- {base_summary}\n\n"
        + "## 四、绝对禁令（Forbidden）\n"
        + "\n".join([f"- {fb}" for fb in contract_obj["forbidden"]])
        + "\n"
    )
    return contract_obj, rendered_md


def generate_file_map_md(target_abs: str, out_map_path: str) -> str:
    """Pre-generate file line map (map.md) so Builder reads targeted line ranges instead of full 85KB file (P1-4)."""
    if not os.path.exists(target_abs):
        return ""
    try:
        lines = open(target_abs, "r", encoding="utf-8", errors="replace").read().splitlines()
        entries: list[str] = [f"# 目标文件结构地图 (`map.md` · 共 {len(lines)} 行)", "- **施工提速铁律**：请先根据下方行号范围定位，用 `Read` 按区间读取，并用一次性集中编辑修改，避免全文反复通读！", ""]
        for idx, ln in enumerate(lines, 1):
            s = ln.strip()
            if any(tag in s for tag in ("<style", "</style>", "<header", "</header>", "<nav", "<main", "</main>", "<script", "</script>", 'id="screen1"', 'id="z1"', 'id="z2"', 'id="z3"', 'id="z4"', 'id="z5"', 'id="z6"', 'id="z7"', 'id="z8"', 'id="z9"', 'id="z10"', 'id="z11"', 'id="z12"', 'id="v3Live')):
                entries.append(f"- 行 `{idx}`: `{s[:95]}`")
            elif s.startswith("function ") or s.startswith("/* ---"):
                entries.append(f"- 行 `{idx}`: `{s[:85]}`")
        txt = "\n".join(entries[:120]) + "\n"
        atomic_write_text(out_map_path, txt)
        return txt
    except Exception:
        return ""


def probe_cad_project(wt_repo: str, job: dict, out_dir: str) -> dict:
    """CAD/Code Channel Gate 1: diff non-empty + dotnet/msbuild build exit=0 + offline unit test exit=0."""
    failures: list[str] = []
    checks: dict = {}

    diff_res = subprocess.run(["git", "-C", wt_repo, "status", "--porcelain"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    changed_files = [ln.strip() for ln in (diff_res.stdout or "").splitlines() if ln.strip()]
    checks["changed_files_count"] = len(changed_files)
    checks["changed_files"] = changed_files[:20]
    if not changed_files:
        failures.append("Worktree 无任何代码改动 (diff 为空)")

    # Locate solution / csproj
    build_target = job.get("build_target") or ""
    if build_target and not os.path.isabs(build_target):
        build_target = os.path.join(wt_repo, build_target)
    if not build_target:
        slns = sorted(glob.glob(os.path.join(wt_repo, "**", "*.sln"), recursive=True))
        csprojs = sorted(glob.glob(os.path.join(wt_repo, "**", "*.csproj"), recursive=True))
        build_target = slns[0] if slns else (csprojs[0] if csprojs else "")

    checks["build_target"] = build_target
    if not build_target or not os.path.exists(build_target):
        failures.append(f"未找到可编译的 .sln/.csproj 目标: {build_target}")
        return {"all_passed": False, "checks": checks, "failures": failures}

    dotnet = DOTNET_EXE if os.path.exists(DOTNET_EXE) else shutil.which("dotnet")
    msbuild = shutil.which("msbuild")
    if dotnet:
        b_cmd = [dotnet, "build", build_target, "-c", "Release", "--nologo"]
    elif msbuild:
        b_cmd = [msbuild, build_target, "/p:Configuration=Release", "/nologo"]
    else:
        failures.append("系统未安装 dotnet 或 msbuild")
        return {"all_passed": False, "checks": checks, "failures": failures}

    b_res = subprocess.run(b_cmd, cwd=wt_repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    build_log_path = os.path.join(out_dir, "cad_build.log")
    atomic_write_text(build_log_path, (b_res.stdout or "") + "\n" + (b_res.stderr or ""))
    checks["build_rc"] = b_res.returncode
    checks["build_log"] = build_log_path
    if b_res.returncode != 0:
        tail = "\n".join((b_res.stdout + "\n" + b_res.stderr).splitlines()[-15:])
        failures.append(f"编译失败 (rc={b_res.returncode}): {tail[:400]}")
        return {"all_passed": False, "checks": checks, "failures": failures}

    # Unit test check
    test_cmd_cfg = job.get("test_cmd")
    test_projs = [
        p for p in sorted(glob.glob(os.path.join(wt_repo, "**", "*.csproj"), recursive=True))
        if re.search(r"test", os.path.basename(p), re.I)
    ]
    if test_cmd_cfg:
        t_cmd = test_cmd_cfg if isinstance(test_cmd_cfg, list) else str(test_cmd_cfg)
        t_res = subprocess.run(t_cmd, shell=isinstance(t_cmd, str), cwd=wt_repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    elif dotnet and test_projs:
        t_res = subprocess.run([dotnet, "test", test_projs[0], "-c", "Release", "--nologo", "--no-build"], cwd=wt_repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    else:
        failures.append("未配置 test_cmd 且工作区内未发现 *Test*.csproj 离线单测工程")
        checks["test_rc"] = -1
        return {"all_passed": False, "checks": checks, "failures": failures}

    test_log_path = os.path.join(out_dir, "cad_test.log")
    atomic_write_text(test_log_path, (t_res.stdout or "") + "\n" + (t_res.stderr or ""))
    checks["test_rc"] = t_res.returncode
    checks["test_log"] = test_log_path
    if t_res.returncode != 0:
        tail = "\n".join((t_res.stdout + "\n" + t_res.stderr).splitlines()[-15:])
        failures.append(f"离线单测未通过 (rc={t_res.returncode}): {tail[:400]}")

    return {"all_passed": len(failures) == 0, "checks": checks, "failures": failures, "screenshots": []}


# ------------------------------------------------------------------ Queue & Pipeline


def load_queue() -> dict:
    if not os.path.exists(QUEUE_PATH):
        return {"version": 1, "jobs": [], "history": []}
    try:
        d = json.load(open(QUEUE_PATH, "r", encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("jobs"), list):
            return d
    except Exception:
        pass
    return {"version": 1, "jobs": [], "history": []}


def reconcile_queue_state(q: dict, live_orders: set | None = None) -> bool:
    """Reconcile stale running/terminal items inside jobs. G40b：live_orders 给定时（并行道），有活槽的在制单不重置回 queued；缺省 None=原逻辑。"""
    changed = False
    jobs = list(q.get("jobs") or [])
    history = list(q.get("history") or [])
    hist_ids = {h.get("job_id") for h in history if isinstance(h, dict) and h.get("job_id")}
    kept_jobs: list[dict] = []
    terminal_states = ("done", "failed", "needs_human", "infra_error")
    for j in jobs:
        if not isinstance(j, dict):
            changed = True
            continue
        st = j.get("status")
        if st in terminal_states:
            jid = j.get("job_id")
            if jid not in hist_ids:
                history.append(j)
                if jid:
                    hist_ids.add(jid)
            changed = True
            continue
        if st == "running":
            if live_orders is not None and j.get("order_id") in live_orders:
                kept_jobs.append(j); continue      # G40b：有活槽的在制单不动
            # V5.0：自愈重排封顶。内核若对某单必崩，旧逻辑会「崩 → 重排 → 崩」无限循环，每次还占一道。
            _crashes = int(j.get("crash_requeues") or 0) + 1
            j["crash_requeues"] = _crashes
            if _crashes > MAX_CRASH_REQUEUES:
                j["status"] = "failed"
                j["result_reason"] = f"内核连续 {_crashes} 次未正常收口（崩溃或被强杀），停止自动重排"
                j["disposition"] = "redispatch"
                j["finished_at"] = iso_now()
                j["finished_ts"] = now_ts()
                v4_trace("crash_loop_stop", str(j.get("order_id") or ""), crashes=_crashes)
                if j.get("job_id") not in hist_ids:
                    history.append(j)
                    hist_ids.add(j.get("job_id"))
                changed = True
                continue
            j["status"] = "queued"
            j["requeued_reason"] = "stale_running_recovered"
            j["requeued_at"] = iso_now()
            j["requeued_ts"] = now_ts()
            changed = True
        kept_jobs.append(j)
    if changed:
        q["jobs"] = kept_jobs
        q["history"] = history
    return changed


def ingest_inbox(q: dict) -> int:
    if not os.path.isdir(INBOX_DIR):
        return 0
    added = 0
    existing_ids = {j.get("order_id") for j in q["jobs"] if j.get("status") in ("queued", "running")}
    for fp in sorted(glob.glob(os.path.join(INBOX_DIR, "*.json"))):
        try:
            item = json.load(open(fp, "r", encoding="utf-8"))
            oid = item.get("order_id")
            _v4i = load_v4_flags()
            if (oid and oid not in existing_ids and _v4i.get("assertion_gate") and _v4i.get("require_assertions")
                    and normalize_order_kind(item.get("kind") or "ui_dashboard") == "ui"):
                _sp = item.get("spec_file") or ""
                try:
                    _st = open(_sp, "r", encoding="utf-8", errors="replace").read() if _sp and os.path.exists(_sp) else ""
                except Exception:
                    _st = ""
                _aa, _ae = extract_assertions(_st)
                if not _st or _ae or not _aa:
                    _why = ("规格文件不存在: " + _sp) if not _st else ("；".join(_ae[:6]) if _ae else "规格书缺 ```assertions``` 断言块")
                    rej_dir = os.path.join(INBOX_DIR, "_rejected")
                    os.makedirs(rej_dir, exist_ok=True)
                    os.replace(fp, os.path.join(rej_dir, os.path.basename(fp)))
                    atomic_write_text(os.path.join(rej_dir, os.path.basename(fp) + ".reason.txt"),
                                      f"{iso_now()} 拒收 {oid}：{_why}\n按《规格断言编写指南》补断言后重新投递。\n")
                    _append_patrol(f"[V4_SPEC_REJECT {iso_now()}] inbox 拒收 {oid}: {_why}")
                    v4_trace("spec_rejected", oid, stage="inbox", errors=_ae, reason=_why, spec_file=_sp)
                    continue
            if oid and oid not in existing_ids:
                item.setdefault("status", "queued")
                item.setdefault("enqueued_ts", now_ts())
                item.setdefault("enqueued_at", iso_now())
                item.setdefault("max_attempts", 2)
                q["jobs"].append(item)
                existing_ids.add(oid)
                added += 1
            proc_dir = os.path.join(INBOX_DIR, "_ingested")
            os.makedirs(proc_dir, exist_ok=True)
            os.replace(fp, os.path.join(proc_dir, os.path.basename(fp)))
        except Exception:
            pass
    return added


def route_repo_by_targets(targets: list, kind: str = "", explicit: str = "") -> str:
    """订单归哪个仓：显式给了就用显式的；否则看目标文件实际在哪个仓（只在一个仓里有的才算数）；
    分不出来退回老规矩（UI 单=活仓，其余=档案仓）。
    2026-11-25：code 单不写 --repo 就默认进档案仓，目标文件在活仓的单就这么空跑一整代。"""
    if str(explicit or "").strip():
        return os.path.abspath(explicit)
    votes = {"live": 0, "archive": 0}
    for t in targets or []:
        rel = str(t).replace("\\", "/").strip("/")
        if not rel:
            continue
        in_live = os.path.isfile(os.path.join(STEWARD_ROOT, rel.replace("/", os.sep)))
        in_arc = os.path.isfile(os.path.join(BP_ROOT, rel.replace("/", os.sep)))
        if in_live and not in_arc:
            votes["live"] += 1
        elif in_arc and not in_live:
            votes["archive"] += 1
    if votes["live"] and not votes["archive"]:
        return STEWARD_ROOT
    if votes["archive"] and not votes["live"]:
        return BP_ROOT
    return STEWARD_ROOT if normalize_order_kind(kind) == "ui" else BP_ROOT


def enqueue_job(
    order_id: str,
    kind: str,
    title: str,
    spec_file: str,
    target: str,
    max_attempts: int,
    repo: str = "",
    build_target: str = "",
    test_cmd: str = "",
    extra_meta: dict | None = None,
    targets: list[str] | None = None,
) -> dict:
    ensure_dirs()
    q = load_queue()
    q["jobs"] = [j for j in q["jobs"] if not (j.get("order_id") == order_id and j.get("status") == "queued")]
    # G51R2：`+` 为多文件拼接分隔符（如 "scripts/v3_data.py+dashboard/v2.html"），入库即拆成独立路径
    norm_targets = [p.strip().replace("\\", "/") for t in (targets or []) for p in str(t).split("+") if p.strip()]
    norm_target = (target or "").replace("\\", "/")
    _tparts = [p.strip().replace("\\", "/") for p in norm_target.split("+") if p.strip()] if "+" in norm_target \
        else ([norm_target] if norm_target else [])
    for _p in reversed(_tparts):
        if _p and _p not in norm_targets:
            norm_targets.insert(0, _p)
    norm_targets = expand_target_tokens(norm_targets)
    if normalize_order_kind(kind) == "ui":
        norm_targets = [t if "/" in t else "dashboard/" + t for t in norm_targets]
        if not norm_targets:
            norm_targets = ["dashboard/v2.html"]
    norm_target = norm_targets[0] if norm_targets else ""
    default_repo = route_repo_by_targets(norm_targets, kind, repo)
    job = {
        "job_id": f"{order_id}_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "order_id": order_id,
        "kind": kind,
        "title": title,
        "repo": os.path.abspath(default_repo),
        "spec_file": os.path.abspath(spec_file),
        "target": norm_target,
        "targets": norm_targets,
        "build_target": build_target,
        "test_cmd": test_cmd,
        "max_attempts": max_attempts,
        "status": "queued",
        "enqueued_ts": now_ts(),
        "enqueued_at": iso_now(),
    }
    if extra_meta:
        job.update(extra_meta)
    if _slots_enabled():                 # G40b：并行道下 enqueue 与在跑 tick 并发，锁内合并防覆写
        import slot_ledger
        with slot_ledger.file_lock("factory_queue", 30):
            qc = load_queue()
            qc["jobs"] = [j for j in (qc.get("jobs") or []) if not (j.get("order_id") == order_id and j.get("status") == "queued")] + [job]
            atomic_write_json(QUEUE_PATH, qc)
    else:
        q["jobs"].append(job)
        atomic_write_json(QUEUE_PATH, q)
    # C-20261125-G42：入队落账成功即立约（合同写失败只 trace，不回滚入队）
    try:
        _contract_hook("enqueue", job)
    except Exception as e:  # noqa: BLE001
        v4_trace("contract_enqueue_skipped", str(job.get("order_id") or ""), error=repr(e))
    return job


def expand_target_tokens(items: list[str]) -> list[str]:
    """V4.1：把 a.py+b.html 和逗号串拆成相对路径，去重并保持顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        text_item = str(item).strip().replace('\\', "/")
        if not text_item:
            continue
        parts: list[str] = []
        for chunk in text_item.split(","):
            parts.extend(chunk.split("+"))
        for part in parts:
            rel = part.strip().strip("/")
            if not rel or rel in seen:
                continue
            seen.add(rel)
            out.append(rel)
    return out


def resolve_job_targets(job: dict) -> list[str]:
    """订单声明的目标文件。支持 targets 列表 / target 单值；code/cad 单缺声明即报错，不再静默回落。"""
    raw = job.get("targets")
    if isinstance(raw, str):
        raw = [raw]
    # G51R2：同 `+` 拼接规则，拆成独立路径后再参与 diff 比对（否则整串当单个文件名 → 永远查无此文件）
    items = [p.strip().replace("\\", "/") for t in (raw or []) for p in str(t).split("+") if p.strip()]
    one = str(job.get("target") or "").strip().replace("\\", "/")
    _oparts = [p.strip().replace("\\", "/") for p in one.split("+") if p.strip()] if "+" in one else ([one] if one else [])
    for _p in reversed(_oparts):
        if _p and _p not in items:
            items.insert(0, _p)
    items = expand_target_tokens(items)
    if not items:
        if normalize_order_kind(job.get("kind") or "ui_dashboard") == "ui":
            items = ["dashboard/v2.html"]
        else:
            raise ValueError("订单未声明 target/targets：code/cad 单必须显式给出目标文件")
    return items


REDLINE_PATH_PREFIXES = ("Private1/", "Private1", "plugins/ai-steward/board_token")

# V5.0：改到这些文件 = 改到工厂内核 / 闸门本体。碰了就要过硬闸——沙箱现状的镜像里跑一遍工厂自己的
# 回归套件，不许带进新失败。一笔坏合入会让全厂后面每一单都跑在坏内核上，这一道值得花 20 秒。
KERNEL_GATE_FILES = (
    "scripts/factory_runner.py", "scripts/worker_worktree.py", "scripts/slot_ledger.py",
    "scripts/ai_store.py", "scripts/admit_v5.py", "scripts/expert_seat.py", "scripts/expert_ops.py",
    "scripts/gateway.py", "scripts/board_server.py", "scripts/autopilot_pump.py", "scripts/lane_status.py",
    "scripts/lane_claim.py", "scripts/v3_data.py", "scripts/review_gate.py", "scripts/line_details.py",
    "scripts/queue_cancel.py", "scripts/enqueue_kernel.py", "scripts/run_tests.py",
)
# 镜像回归不必拷的大块头（历史库备份 / 日志包 / 套装包），套件用不到。
# reports/evidence/ 不在其中：test_mes_core 要用里面的 demo_state 样本，且沙箱镜像与主干归档必须同料，否则每张内核单都「新失败」。
_REGRESS_SKIP_PREFIXES = ("state/db-backup-", "reports/log_packs/", "state/packages/")
_REGRESS_MIRROR_PREFIX = ".v5regress-"


def _norm_repo_path(path: str) -> str:
    """仓库相对路径归一键：去引号、统一斜杠、去首尾 ./ 与 /；Windows 下大小写不敏感。"""
    text = str(path or "").strip().strip('"').replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    return text.lower() if os.name == "nt" else text


def _target_matches(target: str, path_key: str) -> bool:
    """声明目标与改动路径是否对得上：全等，或目标写的是目录（其下任意文件都算）。"""
    key = _norm_repo_path(target)
    if not key or not path_key:
        return False
    return path_key == key or path_key.startswith(key.rstrip("/") + "/")


# 内核回归套件自己的用例文件（scripts/test_*.py、scripts/tests/test_*.py）
_REGRESS_TEST_RE = re.compile(r"^scripts/(?:tests/)?test_[A-Za-z0-9_]+\.py$")
_REGRESS_SYNC_MARK = "[回归用例同步]"


def _is_regress_test_path(path: str) -> bool:
    return bool(_REGRESS_TEST_RE.match(_norm_repo_path(path)))


def _regress_case_files(cases) -> list:
    """回归用例名 → 仓内相对路径。先查 run_tests.CASES（套件的正本登记），查不到按命名约定给一个。

    相对路径按套件模块自己的位置算（CASES 里的路径都建在它所在目录之下），不从本文件的 __file__ 推：
    测试夹具会把内核模块里以活仓路径开头的字符串（含 __file__）改指临时目录，从 __file__ 推在夹具里会算出怪路径。"""
    table: dict = {}
    try:
        try:
            import run_tests as _rt                                   # 只读它的 CASES，不跑套件
        except ImportError:
            if SCRIPTS_DIR not in sys.path:
                sys.path.insert(0, SCRIPTS_DIR)
            import run_tests as _rt
        _rt_dir = os.path.dirname(os.path.abspath(getattr(_rt, "__file__", "") or ""))
        for _name, _path in getattr(_rt, "CASES", []) or []:
            if not _rt_dir:
                continue
            _rel = os.path.relpath(_path, _rt_dir).replace("\\", "/")
            if not _rel.startswith(".."):
                table[str(_name)] = "scripts/" + _rel
    except Exception:  # noqa: BLE001
        table = {}
    return [table.get(str(c)) or ("scripts/test_%s.py" % str(c).replace("test_", "", 1)) for c in (cases or [])]


def _regress_rework_lines(probe_res: dict) -> tuple:
    """硬闸拦下时给工人的返工话：断点明细 + 「回归用例随口径同步」的规矩。

    2026-11-26 G47R4：给 auto-heavy 立卡的单被还按旧口径写的 test_lane_status L6 拦住；派工单又禁止它碰用例。
    只报一个用例名 = 让工人自己从头找断点。→ (返工话各行, 点名放行的用例文件)；没被硬闸拦下 → ([], [])。"""
    kt = ((probe_res or {}).get("strict_checks") or {}).get("kernel_tests") or {}
    if not kt.get("new_failed"):
        return [], []
    files = _regress_case_files(kt["new_failed"])
    lines: list = []
    for case in kt["new_failed"]:
        for detail in (kt.get("detail") or {}).get(case, [])[:4]:
            lines.append("  - 断点 `%s`：%s" % (case, detail))
    lines.append(
        "- %s 先分清是哪一种：代码真写坏了（→ 改代码到用例通过），还是用例断言写的还是旧口径、"
        "而本单就是要改这个口径（→ 同步用例）。后者**允许修改这几个回归用例文件，仅此豁免越界**：%s。"
        "只改与本单新口径直接相关的断言，并让断言继续管住新口径（不是删掉）；别的断言一条都不许删弱、跳过；"
        "不许为了让用例通过去改坏本单要交付的行为。回执里写明改了哪条断言、新旧口径各是什么、依据是规格哪一条。"
        "交付前在沙箱实跑 `python -X utf8 scripts/run_tests.py`，贴出原始输出末段和退出码。"
        % (_REGRESS_SYNC_MARK, "、".join("`%s`" % f for f in files)))
    return lines, files


def _sync_clause_of(prev_feedback: str) -> str:
    """硬闸点名了回归用例时，「严禁越界」要留出口——否则和返工话自相矛盾（工人按红线执行就永远过不了门）。"""
    if _REGRESS_SYNC_MARK not in str(prev_feedback or ""):
        return ""
    return "；**例外：上一轮机检点名的回归用例文件可以同步修改**，见下方「回归用例同步」，仅限点名的那几个"


def _same_bytes(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _rm_tree(path: str) -> bool:
    """删整棵目录：先去只读再删；返回是否删干净（删不掉的留着，下次回归顺手清）。"""
    if not os.path.isdir(path):
        return True
    left = False
    for dp, _dns, fns in os.walk(path, topdown=False):
        for name in fns:
            p = os.path.join(dp, name)
            try:
                os.chmod(p, 0o666)
                os.remove(p)
            except OSError:
                left = True
    for dp, dns, _fns in os.walk(path, topdown=False):
        for name in dns:
            try:
                os.rmdir(os.path.join(dp, name))
            except OSError:
                left = True
    try:
        os.rmdir(path)
    except OSError:
        left = True
    return not left and not os.path.exists(path)


def _make_mirror(src_repo: str, dest: str, archive_of: str = "") -> int:
    """把 src_repo 的现状（已跟踪 + 未跟踪未忽略）拷成一份不带 .git 的镜像。

    给了 archive_of 则改用该提交的归档（主干基线用）。返回拷到的文件数。
    """
    _rm_tree(dest)
    os.makedirs(dest, exist_ok=True)
    if archive_of:
        import io
        import tarfile
        r = subprocess.run(["git", "-C", src_repo, "archive", "--format=tar", archive_of],
                           capture_output=True, timeout=300)
        if r.returncode != 0:
            return 0
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
            members = [m for m in tf.getmembers() if not m.name.startswith(_REGRESS_SKIP_PREFIXES)]
            try:
                tf.extractall(dest, members=members, filter="data")
            except TypeError:          # 老版本 tarfile 没有 filter 形参
                tf.extractall(dest, members=members)
        return 1
    rels: list[str] = []
    for args in (["ls-files", "-z"], ["ls-files", "-z", "--others", "--exclude-standard"]):
        r = subprocess.run(["git", "-C", src_repo, "-c", "core.quotepath=false"] + args,
                           capture_output=True, timeout=120)
        rels += [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    n = 0
    for rel in dict.fromkeys(rels):
        if rel.startswith(_REGRESS_SKIP_PREFIXES):
            continue
        src = os.path.join(src_repo, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest, rel.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        except OSError:
            pass
    return n


def _run_regress_suite(mirror: str, timeout_sec: int = 600) -> dict:
    """在镜像里跑 scripts/run_tests.py --json，回 {ran, failed:[用例名], passed, total, elapsed_sec}。"""
    runner = os.path.join(mirror, "scripts", "run_tests.py")
    if not os.path.isfile(runner):
        return {"ran": False, "failed": [], "passed": 0, "total": 0}
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", runner, "--json"],
                           cwd=os.path.join(mirror, "scripts"), capture_output=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return {"ran": True, "failed": ["<套件超时 %ds>" % timeout_sec], "passed": 0, "total": 0, "timeout": True}
    summary: dict = {}
    for ln in reversed((r.stdout or b"").decode("utf-8", "replace").strip().splitlines()):
        ln = ln.strip()
        if ln.startswith("{") and '"cases"' in ln:
            try:
                summary = json.loads(ln)
            except ValueError:
                summary = {}
            break
    cases = summary.get("cases") or []
    failed = [str(c.get("case")) for c in cases if c.get("status") != "PASS"]
    if r.returncode != 0 and not failed:
        failed = ["<套件非零退出 rc=%s>" % r.returncode]
    res = {"ran": True, "failed": failed, "passed": len(cases) - len(failed), "total": len(cases),
           "elapsed_sec": round(time.time() - t0, 1)}
    # 断点明细（用例自己打印的 [FAIL] 行）留给返工话用：只报「某某用例失败」等于让工人自己从头找。
    detail = {str(c.get("case")): [str(x)[:300] for x in (c.get("fails") or [])[:6]]
              for c in cases if c.get("status") != "PASS" and c.get("fails")}
    if detail:
        res["detail"] = detail
    return res


def run_kernel_regression(wt_repo: str, main_repo: str, tag: str = "") -> dict:
    """内核 / 闸门文件被改时的硬闸：沙箱现状镜像里跑工厂自己的回归套件，不许带进新失败。

    镜像放在主仓的兄弟目录（相对路径与活仓解析得一样一致）、不带 .git。套件写出来的临时库落在镜像里，
    不会弄脏沙箱，更不会跟着 git add -A 混进合入。沙箱有失败时再跑一遍主干基线，只拦「新出现」的那几个。
    """
    res: dict = {"ran": False, "ok": True, "failed": [], "new_failed": [], "preexisting_failed": [],
                 "passed": 0, "total": 0}
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tag or "x"))[:60] or "x"
    parent = os.path.dirname(os.path.abspath(main_repo))
    mirror = os.path.join(parent, _REGRESS_MIRROR_PREFIX + safe)
    base_mirror = os.path.join(parent, _REGRESS_MIRROR_PREFIX + safe + "-base")
    try:
        for name in os.listdir(parent):          # 上次没删干净的镜像顺手清掉
            p = os.path.join(parent, name)
            if name.startswith(_REGRESS_MIRROR_PREFIX) and os.path.isdir(p) \
                    and time.time() - os.path.getmtime(p) > 900:
                _rm_tree(p)
        res["files"] = _make_mirror(wt_repo, mirror)
        res.update(_run_regress_suite(mirror))
        if not res.get("ran"):
            res.update(ok=False, error="镜像里没有 scripts/run_tests.py")
        elif res["failed"]:
            _make_mirror(main_repo, base_mirror, archive_of="HEAD")
            base = _run_regress_suite(base_mirror)
            if not base.get("ran"):
                res.update(ok=False, error="主干基线套件未能运行，无法判断失败是否本次带入")
            else:
                res["baseline_failed"] = base.get("failed") or []
                res["new_failed"] = [c for c in res["failed"] if c not in res["baseline_failed"]]
                res["preexisting_failed"] = [c for c in res["failed"] if c in res["baseline_failed"]]
                res["ok"] = not res["new_failed"]
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        res.update(ok=False, error="%s: %s" % (type(e).__name__, str(e)[:200]))
    finally:
        res["mirror_removed"] = bool(_rm_tree(mirror)) and bool(_rm_tree(base_mirror))
    return res


def probe_code_delivery(wt_repo: str, main_repo: str, targets: list[str], out_dir: str = "", builder_rc: int = 0) -> dict:
    """Gate1 for code orders：量「累计交付」——沙箱相对与主干的分叉点以来，已提交+暂存+未暂存+未跟踪的全部改动。

    V4.1 的尺子只量「本轮未提交」，于是工人自己 commit 就被判零改动（2026-11-24 G41a 修过一次，
    11-25 的尺子又改了回去：G44R3 / G51R1 双双冤死）。本轮增量只留作参考，不再定生死。
    """
    def _g(*args: str) -> str:
        r = subprocess.run(["git", "-C", wt_repo, "-c", "core.quotepath=false"] + list(args),
                           capture_output=True, timeout=60)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""

    def _numstat(text: str) -> list[dict]:
        rows = []
        for ln in (text or "").splitlines():
            parts = ln.split("\t")
            if len(parts) >= 3 and parts[2].strip():
                try:
                    add = int(parts[0]) if parts[0].isdigit() else 0
                    dele = int(parts[1]) if parts[1].isdigit() else 0
                except ValueError:
                    add = dele = 0
                rows.append({"path": parts[2].strip(), "added": add, "deleted": dele})
        return rows

    def _abs(rel: str) -> str:
        return os.path.join(wt_repo, rel.replace("/", os.sep))

    def _lines(rel: str) -> int:
        try:
            with open(_abs(rel), "rb") as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0

    head_branch = ""
    try:
        _hb = subprocess.run(["git", "-C", main_repo, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, timeout=30)
        head_branch = (_hb.stdout or b"").decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        head_branch = ""
    base_ref = _g("merge-base", head_branch or "main", "HEAD").strip()

    files: dict[str, dict] = {}        # 归一键 -> 累计记录

    def _rec(path: str) -> dict:
        return files.setdefault(_norm_repo_path(path), {
            "path": str(path).strip().strip('"').replace("\\", "/"), "added": 0, "deleted": 0,
            "committed": 0, "staged": 0, "unstaged": 0, "untracked": 0})

    if base_ref:
        for row in _numstat(_g("diff", "--no-renames", "--numstat", base_ref)):
            cur = _rec(row["path"])
            cur["added"] += row["added"]
            cur["deleted"] += row["deleted"]
            cur["committed"] = 1
    round_files: list[str] = []
    for label, args in (("staged", ("diff", "--cached", "--numstat")), ("unstaged", ("diff", "--numstat"))):
        for row in _numstat(_g(*args)):
            cur = _rec(row["path"])
            cur[label] += max(row["added"], row["deleted"], 1)
            round_files.append(cur["path"])
            if not base_ref:               # 没有分叉点可依时，退回只认未提交改动
                cur["added"] += row["added"]
                cur["deleted"] += row["deleted"]
    for rel in _g("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not rel:
            continue
        cur = _rec(rel)
        cur["added"] += _lines(rel)
        cur["untracked"] = 1
        round_files.append(cur["path"])

    targets_norm = expand_target_tokens(list(targets or []))
    changed = sorted(files.values(), key=lambda f: _norm_repo_path(f["path"]))
    touched_keys = {_norm_repo_path(f["path"]) for f in changed}
    declared_touched = [t for t in targets_norm if any(_target_matches(t, k) for k in touched_keys)]
    # 只对工厂内核仓生效：别的仓里同名文件（scripts/ai_store.py 之类）不是工厂内核，
    # 也没有回归套件可跑——硬套上去只会把好单判成「工厂故障」。
    _is_kernel_repo = os.path.isfile(os.path.join(main_repo or "", "scripts", "factory_runner.py")) \
        and os.path.isfile(os.path.join(main_repo or "", "scripts", "run_tests.py"))
    gate_keys = {_norm_repo_path(x) for x in KERNEL_GATE_FILES}
    kernel_touched = [f for f in changed if _norm_repo_path(f["path"]) in gate_keys] if _is_kernel_repo else []
    outside: list[str] = []
    live_synced: list[str] = []
    tests_synced: list[dict] = []
    for f in changed:
        key = _norm_repo_path(f["path"])
        if any(_target_matches(t, key) for t in targets_norm):
            continue
        # 与活仓现存文件逐字节相同 = 工人做的「基线同步」，不是他的交付，也不算越界
        if main_repo and _same_bytes(_abs(f["path"]), os.path.join(main_repo, f["path"].replace("/", os.sep))):
            live_synced.append(f["path"])
        elif kernel_touched and _is_regress_test_path(f["path"]):
            # 改了内核、又同步了回归用例：用例跟着口径走是正常工序，不算越界——但单列出来交质检核对
            # （只许同步与本单口径直接相关的断言）。
            tests_synced.append({"path": f["path"], "added": f["added"], "deleted": f["deleted"]})
        else:
            outside.append(f["path"])
    redline = [f["path"] for f in changed
               if _norm_repo_path(f["path"]).startswith(tuple(p.lower() for p in REDLINE_PATH_PREFIXES))]
    live_synced_set = set(live_synced)

    failures: list[str] = []
    env_fail = False
    if builder_rc != 0:
        failures.append(f"Builder 退出码 {builder_rc}")
    if not targets_norm:
        failures.append("订单未声明目标文件")
    elif not declared_touched:
        failures.append("声明目标零改动（自分叉点以来累计，含已提交）：" + "、".join(targets_norm))
    if redline:
        failures.append("禁区改动：" + "、".join(redline[:5]))

    # 语法硬闸：沙箱里被改动的 .py 一个都不许是坏的（code 单 git add -A 会把它们全带进活仓）
    strict: dict = {}
    bad_py: list[str] = []
    py_checked = 0
    for f in changed:
        if not f["path"].lower().endswith(".py") or f["path"] in live_synced_set:
            continue
        try:
            with open(_abs(f["path"]), "rb") as fh:
                src = fh.read()
            compile(src, f["path"], "exec")
            py_checked += 1
        except (OSError, SyntaxError, ValueError) as e:
            bad_py.append("%s: %s" % (f["path"], (str(e).splitlines() or [type(e).__name__])[0][:160]))
    strict["py_syntax"] = {"checked": py_checked, "bad": bad_py}
    if bad_py:
        failures.append("Python 语法错误（合入后工厂会起不来）：" + "；".join(bad_py[:4]))

    gate_hit = ([f["path"] for f in changed if _norm_repo_path(f["path"]) in gate_keys and f["path"] not in live_synced_set]
                if _is_kernel_repo else [])
    if gate_hit:
        strict["kernel_files"] = gate_hit
        kr = run_kernel_regression(wt_repo, main_repo, tag=os.path.basename(os.path.abspath(wt_repo)))
        strict["kernel_tests"] = kr
        if not kr.get("ok"):
            if kr.get("new_failed"):
                failures.append("内核回归未全绿（本次改动新带进来的失败：%s）" % "、".join(kr["new_failed"][:5]))
            else:
                # 闸门自己没能给出判定：不是工人的错，按环境故障处理（不计入施工缺陷）
                env_fail = True
                failures.append("内核回归闸未能完成判定（环境故障）：%s"
                                % (kr.get("error") or "、".join(kr.get("failed") or ["原因不明"]))[:200])

    checks = {
        "files_changed": [{"path": f["path"], "added": f["added"], "deleted": f["deleted"],
                           "untracked": f["untracked"], "committed": f["committed"]} for f in changed],
        "declared_touched": declared_touched,
        "declared_untouched": [t for t in targets_norm if t not in declared_touched],
        "files_outside_declared": outside,
        "live_synced_files": live_synced,
        "tests_synced": tests_synced,
        "redline_touched": redline,
        "base_ref": base_ref[:12],
        "added_total": sum(f["added"] for f in changed),
        "legacy_committed": [],          # V4.1 遗留键：已提交改动现在计入累计交付，不再单列
        "cumulative": {
            "base_ref": base_ref[:12],
            "files": len(changed),
            "committed_files": [f["path"] for f in changed if f["committed"]],
            "uncommitted_files": sorted(set(round_files)),
        },
        "round_uncommitted_files": sorted(set(round_files)),
    }
    res = {"all_passed": not failures, "checks": checks, "failures": failures, "screenshots": [],
           "strict_checks": strict}
    if env_fail:
        res["env_fail"] = True
        res["env_fail_reason"] = "kernel_regression"
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
            atomic_write_json(os.path.join(out_dir, "code_probe.json"), res)
        except OSError:
            pass
    return res


# 真引擎探针自己出的状况（不是工件的问题）：取证环境故障、探针自身异常、
# 以及两条「过程」检查——主仓页面 / 六页静态正本在施工期间变了。后者多半是别的单合入所致（六条产线并行），
# code 单的过程检查归 probe_code_delivery（禁区改动、越界改动都在那里量），合入冲突另有回炉逻辑，不在这里冤枉工人。
_RUNTIME_SOFT_PREFIXES = ("[ENV]", "CDP 浏览器探针执行异常", "违反 Worktree 隔离纪律", "静态正本被修改")


def _rt_soft(f) -> bool:
    return str(f).startswith(_RUNTIME_SOFT_PREFIXES)


def _rt_hard_failures(ui: dict) -> list[str]:
    """真引擎查出的施工问题（去掉探针自己出的状况）。"""
    return [str(f) for f in (ui.get("failures") or []) if not _rt_soft(f)]


def _rt_preexisting(ui: dict) -> list[str]:
    """页面通检的毛病里，生产页（基线）本来就有的那些。断言 FAIL 从不算既有——那正是本单要修的。
    V5.0-real.9：code 单与 ui 单同一口径（见 _ui_preexisting：正文墙按计数 + 漂移容差判，不逐字比对）。"""
    return _ui_preexisting(ui)


def _code_runtime_evidence(wt_repo: str, repo_root: str, page: str, out_dir: str, six_baseline: dict,
                           main_sha_before: str, assertions: list[dict]) -> dict:
    """V5.0-real.5：code 单的看板真引擎取证——对沙箱里的看板页面跑一遍完整 Gate1 探针（截图 / 遥测 / 断言）。

    old_sha 传空：code 单的页面可能没改（只改了给它供数的脚本），「页面没变」不算问题。
    沙箱页有页面通检查出的毛病时，再对主仓生产页跑一遍同款探针（不带断言），结果挂在 baseline_failures 上，
    由调用方剔除生产页本来就有的毛病。Edge / CDP 起不来是取证环境故障，不是施工缺陷：重试 2 次仍不行就如实带回 env_fail。
    """
    def _run(repo: str, out: str, asserts) -> dict:
        last: dict = {}
        for _try in range(MAX_GATE1_INFRA_RETRIES + 1):
            if _try:
                time.sleep(5)
            last = probe_ui_dashboard(
                wt_repo=repo, main_repo=repo_root, target_rel=page, out_dir=out, six_baseline=six_baseline,
                old_sha="", main_file_sha_before=main_sha_before, assertions=asserts,
            )
            if not last.get("env_fail"):
                break
        return last

    res = _run(wt_repo, out_dir, assertions or None)
    _hard = [f for f in _rt_hard_failures(res) if not f.startswith("[断言 FAIL")]
    if _hard and not res.get("env_fail"):
        base = _run(repo_root, out_dir + "_base", None)
        res["baseline_failures"] = None if base.get("env_fail") else [str(f) for f in (base.get("failures") or [])]
    return res


_TEXTWALL_RE = re.compile(r"首屏默认可见正文\s+(\d+)\s+字符过多")
_TEXTWALL_DRIFT_TOL = 500     # 正文墙计数含实时文本（时长/事件数），两次探针之间会漂；只有明显变差才算本单的


def _ui_preexisting(res: dict) -> list[str]:
    """ui 单与 code 单共用的「生产页本来就有」清单：断言 FAIL 永不剔除；正文墙按「不高于基线 + 漂移容差」判，
    因为计数里含会随时间/数据变化的文本，逐字节比对会误判。"""
    baseline = res.get("baseline_failures")
    if not baseline:
        return []
    hard = [f for f in _rt_hard_failures(res) if not f.startswith("[断言 FAIL")]
    out: list[str] = [f for f in hard if f in baseline]
    for f in hard:
        if f in out:
            continue
        m1 = _TEXTWALL_RE.search(f)
        if not m1:
            continue
        for b in baseline:
            m2 = _TEXTWALL_RE.search(str(b))
            if m2 and int(m1.group(1)) <= int(m2.group(1)) + _TEXTWALL_DRIFT_TOL:
                out.append(f)
                break
    return out


def _ui_gate1_with_baseline(res: dict, repo_root: str, page: str, out_dir: str, six_baseline: dict,
                            main_sha_before: str) -> dict:
    """V5.0-real.8：ui 单的 Gate1 也做生产页基线——沙箱页被通检查出毛病时，对主仓生产页跑一遍同款探针（不带断言），
    把生产页本来就有的毛病从本单失败项里剔除。生产页基线探针自己起不来时如实留痕，毛病仍按本单处理。"""
    last: dict = {}
    for _try in range(MAX_GATE1_INFRA_RETRIES + 1):
        if _try:
            time.sleep(5)
        last = probe_ui_dashboard(
            wt_repo=repo_root, main_repo=repo_root, target_rel=page, out_dir=out_dir,
            six_baseline=six_baseline, old_sha="", main_file_sha_before=main_sha_before, assertions=None,
        )
        if not last.get("env_fail"):
            break
    out = dict(res)
    checks = dict(out.get("checks") or {})
    if last.get("env_fail"):
        checks["gate1_baseline"] = {
            "ran": False, "env_fail": True,
            "reason": "；".join(str(f) for f in (last.get("failures") or []))[:300] or "生产页基线探针环境故障",
        }
        out["checks"] = checks
        out["baseline_failures"] = None
        return out
    base_failures = [str(f) for f in (last.get("failures") or [])]
    out["baseline_failures"] = base_failures
    preexisting = _ui_preexisting(out)
    if preexisting:
        keep = [f for f in (out.get("failures") or []) if str(f) not in preexisting]
        out["failures"] = keep
        out["all_passed"] = not keep
    checks["gate1_baseline"] = {"ran": True, "excluded": preexisting[:8], "baseline_failures": base_failures[:8]}
    out["checks"] = checks
    return out


def _merge_code_runtime(code_res: dict, ui: dict) -> dict:
    """把真引擎取证结果并进 code 单的 Gate1 结论：机检结论原样保留；本单引入的真引擎问题加「[看板真引擎]」前缀并入 failures；
    探针自己出的状况、生产页本来就有的通检毛病不判 FAIL，只如实记进 checks.runtime_evidence 交质检。"""
    res = dict(code_res)
    checks = dict(res.get("checks") or {})
    if not ui:
        return res
    if ui.get("env_fail"):
        checks["runtime_evidence"] = {"ran": False, "env_fail": True,
                                      "reason": "；".join(str(f) for f in (ui.get("failures") or []))[:300] or "真引擎取证环境故障"}
        res["checks"] = checks
        return res
    for k in ("screenshots", "assertions", "assert_summary", "assert_shots", "assert_raw_path", "dom_dump_path", "telemetry_path"):
        if ui.get(k):
            res[k] = ui[k]
    ev: dict = {"ran": True, "all_passed": bool(ui.get("all_passed")), "assert_summary": dict(ui.get("assert_summary") or {}),
                "ui_checks": {k: v for k, v in (ui.get("checks") or {}).items() if isinstance(v, (bool, int, float))}}
    tel = ui.get("telemetry_path") or ""
    if tel and os.path.exists(tel):
        try:
            ev["js_errors"] = len(json.load(open(tel, "r", encoding="utf-8")).get("jsErrors") or [])
        except (OSError, ValueError):
            pass
    soft = [str(f) for f in (ui.get("failures") or []) if _rt_soft(f)]
    if soft:
        ev["probe_error"] = soft[:3]
    preexisting = _rt_preexisting(ui)
    if preexisting:
        ev["preexisting_failures"] = preexisting[:6]
    if "baseline_failures" in ui and ui.get("baseline_failures") is None:
        ev["baseline_note"] = "生产页基线探针环境故障，未能区分既有问题；沙箱页的通检毛病按本单处理"
    checks["runtime_evidence"] = ev
    res["checks"] = checks
    hard = [f for f in _rt_hard_failures(ui) if f not in preexisting]
    if hard:
        res["failures"] = list(res.get("failures") or []) + ["[看板真引擎] " + f for f in hard]
        res["all_passed"] = False
    return res


def reject_order(order_id: str, reason: str = "", dry_run: bool = False) -> dict:
    """Initiator 24h one-click rejection backdoor: penalty-free rework re-queue."""
    ensure_dirs()
    q = load_queue()
    hist = [h for h in (q.get("history") or []) if h.get("order_id") == order_id]
    latest = hist[-1] if hist else None
    age_sec = 0.0
    if latest and latest.get("finished_ts"):
        age_sec = now_ts() - float(latest["finished_ts"])
        if not dry_run and age_sec > REJECT_WINDOW_SEC:
            return {"ok": False, "error": f"订单 {order_id} 交付已超 24h 窗口 ({round(age_sec/3600, 1)}h)"}

    base_spec = (latest or {}).get("spec_file") or os.path.join(STEWARD_ROOT, "state", "c20261121_001_v21_spec.md")
    base_kind = (latest or {}).get("kind") or "ui_dashboard"
    base_target = (latest or {}).get("target") or "dashboard/v2.html"
    base_repo = (latest or {}).get("repo") or STEWARD_ROOT
    clean_reason = (reason or "发起人24h视检退回重做").strip()

    rework_id = f"{order_id}-R{int(now_ts()) % 10000}"
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "parent_order_id": order_id,
            "would_enqueue_id": rework_id,
            "penalty_exempt": True,
            "within_24h": age_sec <= REJECT_WINDOW_SEC,
            "reason": clean_reason,
            "target": base_target,
            "repo": base_repo,
        }

    rework_spec_path = os.path.join(STEWARD_ROOT, "state", f"rework_{rework_id}.md")
    golden_dir = save_golden_sample(order_id, clean_reason)
    contract_obj, compacted_md = compact_contract_on_reject(order_id, rework_id, clean_reason, base_target)
    if load_v4_flags().get("assertion_gate"):
        # V4.0-P2：返工单继承原单机检断言（发起人的新意见进契约条款由 Verifier 审，已有量化验收继续由 Gate1 执行）
        try:
            _fence = assertion_fence_text(open(base_spec, "r", encoding="utf-8", errors="replace").read()) if os.path.exists(base_spec) else ""
        except Exception:
            _fence = ""
        if _fence:
            compacted_md += f"\n## 五、机检断言（继承自 `{order_id}`，Gate1 逐条真机执行）\n\n{_fence}\n"
    atomic_write_text(rework_spec_path, compacted_md)
    init_status = "needs_human" if contract_obj.get("needs_human_resign") else "queued"
    new_job = enqueue_job(
        order_id=rework_id,
        kind=base_kind,
        title=f"24h打回免罚返工·{order_id} (Rev{contract_obj['rev']})",
        spec_file=rework_spec_path,
        target=base_target,
        max_attempts=2,
        repo=base_repo,
        extra_meta={
            "penalty_exempt": True,
            "parent_order_id": order_id,
            "rework_reason": clean_reason,
            "contract_rev": contract_obj["rev"],
            "golden_sample_dir": golden_dir,
            "status": init_status,
        },
    )
    append_v3_event(rework_id, 0, "contract_compacted", "契约状态机", f"打回归并至 contract.json Rev {contract_obj['rev']}（有效条款 {len(contract_obj['clauses'])} 项，样本已入 golden/）")
    # C-20261125-G42：打回记在原单合同上（rejects+1），返工单由 enqueue_job 另立新约
    try:
        _contract_hook("reject", order_id, reason=clean_reason)
    except Exception as e:  # noqa: BLE001
        v4_trace("contract_reject_skipped", order_id, error=repr(e))
    _append_patrol(f"[V3_INITIATOR_REJECT {iso_now()}] parent={order_id} -> new={rework_id} rev={contract_obj['rev']} penalty_exempt=True reason={clean_reason[:120]}")
    v4_trace("initiator_reject", order_id, new_order_id=rework_id, reason_full=reason, reason_clean=clean_reason,
             contract_rev=contract_obj.get("rev"), active_clauses=len(contract_obj.get("clauses") or []), golden_dir=golden_dir)
    return {"ok": True, "dry_run": False, "penalty_exempt": True, "contract_rev": contract_obj["rev"], "golden_dir": golden_dir, "job": new_job}


def _normalize_verifier_obj(obj: dict) -> dict | None:
    if not isinstance(obj, dict):
        return None
    findings = obj.get("findings")
    if isinstance(findings, list):
        c_p0 = 0
        c_p1 = 0
        defects_list: list[str] = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            st = str(f.get("status") or "confirmed").lower().strip()
            sev = str(f.get("severity") or "P1").upper().strip()
            if st == "confirmed":
                if sev == "P0":
                    c_p0 += 1
                elif sev == "P1":
                    c_p1 += 1
                if sev in ("P0", "P1"):
                    ev_val = f.get("evidence")
                    if isinstance(ev_val, list):
                        ev_s = ", ".join(str(x) for x in ev_val[:2])
                    else:
                        ev_s = str(ev_val or "")[:140]
                    defects_list.append(f"[{sev}·{f.get('id', 'F')}] {f.get('what', '')} → 改法: {f.get('fix', '')} (证据: {ev_s})")
        mech_verdict = "FAIL" if (c_p0 >= 1 or c_p1 >= 3) else "PASS"
        out = dict(obj)
        out["verdict"] = mech_verdict
        out["confirmed_p0"] = c_p0
        out["confirmed_p1"] = c_p1
        out["empty_output"] = False
        out["parse_error"] = False
        if not out.get("defects"):
            out["defects"] = defects_list
        out["summary"] = f"[内核机械裁决 {mech_verdict}: confirmed P0={c_p0}, P1={c_p1}] " + str(out.get("summary") or "")
        return out
    v = str(obj.get("verdict", "")).upper().strip()
    if v in ("PASS", "FAIL"):
        out = dict(obj)
        out["verdict"] = v
        d_cnt = len(out.get("defects") or [])
        out.setdefault("confirmed_p0", d_cnt if v == "FAIL" else 0)
        out.setdefault("confirmed_p1", 0)
        out["empty_output"] = False
        out["parse_error"] = False
        return out
    return None


def _scan_balanced_json_dicts(blob: str) -> list[dict]:
    dec = json.JSONDecoder()
    objs: list[dict] = []
    pos = 0
    n = len(blob)
    while pos < n:
        idx = blob.find("{", pos)
        if idx == -1:
            break
        try:
            obj, end_idx = dec.raw_decode(blob, idx)
            if isinstance(obj, dict) and ("findings" in obj or "verdict" in obj):
                objs.append(obj)
            pos = idx + 1
        except Exception:
            pos = idx + 1
    return objs


def _parse_verifier_json(text: str) -> dict:
    if not (text or "").strip():
        return {
            "verdict": "INFRA_ERROR",
            "empty_output": True,
            "parse_error": True,
            "confirmed_p0": 0,
            "confirmed_p1": 0,
            "defects": ["独立复核无输出（空响应）"],
            "summary": "",
        }
    fenced_blocks = [m.group(1) for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.I)]
    candidates: list[dict] = []
    for blk in fenced_blocks:
        candidates.extend(_scan_balanced_json_dicts(blk))
    if not candidates:
        candidates.extend(_scan_balanced_json_dicts(text))

    # Prefer objects carrying structured findings first, newest first inside same tier
    for obj in sorted(candidates, key=lambda o: 0 if isinstance(o.get("findings"), list) else 1):
        norm = _normalize_verifier_obj(obj)
        if norm is not None:
            return norm

    # Truncated JSON recovery: extract complete finding dicts inside "findings": [ ...
    idx_f = text.rfind('"findings"')
    if idx_f != -1:
        idx_arr = text.find("[", idx_f)
        if idx_arr != -1:
            dec = json.JSONDecoder()
            rec_findings: list[dict] = []
            p = idx_arr + 1
            n = len(text)
            while p < n:
                nx = text.find("{", p)
                if nx == -1:
                    break
                try:
                    f_obj, end_p = dec.raw_decode(text, nx)
                    if isinstance(f_obj, dict) and any(k in f_obj for k in ("severity", "what", "status", "id")):
                        rec_findings.append(f_obj)
                    p = max(end_p, nx + 1)
                except Exception:
                    p = nx + 1
            if rec_findings:
                norm = _normalize_verifier_obj({"findings": rec_findings, "summary": "[部分截断JSON已恢复findings]"})
                if norm is not None:
                    norm["truncated_recovered"] = True
                    return norm

    return {
        "verdict": "INFRA_ERROR",
        "empty_output": False,
        "parse_error": True,
        "confirmed_p0": 0,
        "confirmed_p1": 0,
        "defects": ["独立复核未输出规范 JSON 结论"],
        "summary": (text or "")[-400:],
    }


def _sandbox_has_draft(wt_path: str, main_shas_before: dict | None = None, branch: str = "", repo_root: str = "") -> bool:
    """沙箱里有没有上一轮留下的成果。

    V5.0：多目标单不能只看第一个目标——第一个没动、其余改了，旧逻辑会当成空沙箱直接抹掉。
    目标文件之外还有已提交的成果（新文件 / 测试 / 配套改动），同样算有成果。
    """
    for rel, main_sha in (main_shas_before or {}).items():
        cand = os.path.join(wt_path, str(rel).replace("\\", "/"))
        try:
            if not os.path.isfile(cand) or os.path.getsize(cand) == 0:   # 空文件不算成果（小文件照算）
                continue
        except OSError:
            continue
        if sha256_file(cand) != (main_sha or ""):
            return True
    if branch and repo_root:
        r = subprocess.run(["git", "-C", repo_root, "rev-parse", "--verify", "-q", branch + "^{commit}"],
                           capture_output=True, timeout=30)
        tip = (r.stdout or b"").decode("utf-8", "replace").strip() if r.returncode == 0 else ""
        if tip:
            anc = subprocess.run(["git", "-C", repo_root, "merge-base", "--is-ancestor", tip, "HEAD"],
                                 capture_output=True, timeout=60)
            return anc.returncode != 0      # 分支上有主干还没有的提交
    return False


def _prepare_worktree(order_id: str, repo_root: str, target_rel: str = "", main_sha_before: str = "", preserve_if_modified: bool = True,
                      main_shas_before: dict | None = None) -> tuple[bool, str, str]:
    """Create or reuse an isolated git worktree wt/<order_id> via worker_worktree.py."""
    if not worker_worktree:
        return False, "", "worker_worktree 模块不可用"
    st = worker_worktree.status(order_id, repo=repo_root)
    if preserve_if_modified and st.get("ok") and st.get("valid") and st.get("path"):
        wt_existing = st["path"]
        wt_branch0 = st.get("branch", "wt/" + worker_worktree.safe_name(order_id))
        shas = dict(main_shas_before or ({target_rel: main_sha_before} if target_rel else {}))
        if _sandbox_has_draft(wt_existing, shas, branch=wt_branch0, repo_root=repo_root):
            print(f"[factory_runner] reusing existing modified sandbox worktree {wt_existing} (branch {wt_branch0})", flush=True)
            return True, wt_existing, wt_branch0
    # Clean up any leftover worktree/branch for same order_id before starting fresh
    if st.get("ok"):
        worker_worktree.remove(order_id, repo=repo_root, delete_branch=True, force=True)
    # Also ensure git branch -D if leftover without ledger entry
    subprocess.run(["git", "-C", repo_root, "worktree", "prune"], capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_root, "branch", "-D", "wt/" + worker_worktree.safe_name(order_id)], capture_output=True, timeout=30)

    res = worker_worktree.ensure(order_id, repo=repo_root, force=True)
    if not res.get("ok"):
        return False, "", f"worktree ensure 失败: {res}"
    return True, res["path"], res.get("branch", "")


_JUNK_PATTERNS = ("*.log", "*.tmp", "*.bak-*", "*.pyc", "*/__pycache__/*", "__pycache__/")
# 运行态前缀：队列 / 事件 / 槽位 / 账本 / 日志，绝不能跟着 code 单的 git add -A 进主干（除非它就是声明目标）
_VOLATILE_PREFIXES = ("state/", "logs/")

# 合入阶段里属于「工厂侧问题、不是施工缺陷」的原因：成果是好的，只是没合进去。不耗轮次、不回炉。
MERGE_INFRA_REASONS = (
    "target-dirty-overlap", "repo-busy", "not-a-git-repo", "branch-missing", "no-branch",
    "no-target-branch", "main-repo-not-on-target", "worktree-invalid", "not-found",
    "merge-no-commits", "worktree-commit-failed", "merge-not-ancestor", "repo-conflict",
    "squash-commit-failed", "worktree-foreign-repo",
)


def _porcelain_paths(cwd: str) -> list[tuple[str, str]]:
    """git status --porcelain -z -> [(XY 状态码, 仓库相对路径)]。改名取新路径；不做引号解码（已关 quotepath）。"""
    out: list[tuple[str, str]] = []
    try:
        r = subprocess.run(["git", "-C", cwd, "-c", "core.quotepath=false", "status", "--porcelain=v1", "-z",
                            "--untracked-files=all"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return out
    parts = [p for p in (r.stdout or b"").decode("utf-8", "replace").split("\0") if p]
    i = 0
    while i < len(parts):
        xy, path = parts[i][:2], parts[i][3:]
        i += 1
        if "R" in xy or "C" in xy:          # 改名 / 拷贝：下一段是原路径
            i += 1
        if path:
            out.append((xy, path.replace("\\", "/")))
    return out


def _referenced_by_targets(wt_path: str, allowed: list[str], extras: list[str]) -> list[str]:
    """目标文件正文里按文件名引用到的越界文件（script src / link href / import 等）——这些是真依赖。"""
    corpus = ""
    for rel in allowed:
        try:
            with open(os.path.join(wt_path, rel.replace("/", os.sep)), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        corpus += raw.decode("utf-8", "replace") + "\n"
    hits: list[str] = []
    for p in extras:
        name = p.rsplit("/", 1)[-1]
        if name and name in corpus:
            hits.append(p)
    return hits


def _git_nothing_to_commit(out: str) -> bool:
    """git commit「没东西可提交」的三种说法都算数：不用提交，接着合。

    工人自己把活提交了、沙箱里只剩没跟踪的运行态文件（state/tmp/…，内核故意不收）时，git 打印的是
    「nothing added to commit … but untracked files present」，不是「nothing to commit」——
    2026-11-26 凌晨 G44R4 / G44R5 质检过了却卡在合入，就是被这句措辞挡的。"""
    low = str(out or "").lower()
    return "nothing to commit" in low or "nothing added to commit" in low or "no changes added to commit" in low


def _commit_and_merge_worktree(
    order_id: str,
    repo_root: str,
    wt_path: str,
    allowed_rels,
    main_shas_before: dict | str | None = None,
    kind: str = "ui_dashboard",
    ledger_path: str = "",
) -> tuple[bool, dict]:
    """Commit deliverable(s) inside worktree, merge into main via worker_worktree.finish().

    红线：绝不删除工作区里的任何文件（旧实现会 os.remove 目标外未跟踪文件，直接把新交付删掉）。
    V5.0：合入已经成功、只是沙箱没收拾干净，不再记成「合入失败」（旧实现会把它回炉，下一轮必死）；
    非 code 单的目标外改动留在沙箱不合并也不删除，除非目标文件自己引用到了它。
    """
    if not worker_worktree:
        return False, {"reason": "worker_worktree missing"}

    allowed = ([allowed_rels] if isinstance(allowed_rels, str) else list(allowed_rels or []))
    allowed = [r.replace("\\", "/") for r in allowed if r]
    if isinstance(main_shas_before, str):
        main_shas_before = {allowed[0]: main_shas_before} if allowed else {}
    is_code = normalize_order_kind(kind) == "code"

    extras = [p for _xy, p in _porcelain_paths(wt_path) if not any(_target_matches(t, p) for t in allowed)]
    if extras and not is_code:
        _needed = _referenced_by_targets(wt_path, allowed, extras)
        if _needed:
            return False, {"reason": "unexpected-files", "files": _needed[:20],
                           "hint": "目标文件引用到了声明范围之外的新文件：把依赖并回目标文件，或在回执里说明需要扩大声明范围"}

    for rel in allowed:
        if os.path.exists(os.path.join(wt_path, rel.replace("/", os.sep))):
            subprocess.run(["git", "-C", wt_path, "add", "--", rel], check=False, capture_output=True, timeout=30)
    if is_code:
        # code 单：新文件 / 单测都要进提交，绝不做「清理」
        subprocess.run(["git", "-C", wt_path, "add", "-A"], check=False, capture_output=True, timeout=30)
        # 只把施工垃圾留在沙箱里，别带进 main；运行态文件同理（声明为目标的那几个再加回来）
        subprocess.run(["git", "-C", wt_path, "reset", "-q", "--"] + list(_JUNK_PATTERNS)
                       + [p + "*" for p in _VOLATILE_PREFIXES], check=False, capture_output=True, timeout=30)
        for rel in allowed:
            subprocess.run(["git", "-C", wt_path, "add", "--", rel], check=False, capture_output=True, timeout=30)

    cm = subprocess.run(
        ["git", "-C", wt_path, "commit", "-q", "-m", f"v3({order_id}): verified delivery via headless pipeline"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if cm.returncode != 0 and not _git_nothing_to_commit(cm.stdout + cm.stderr):
        return False, {"reason": "worktree-commit-failed", "err": (cm.stdout + cm.stderr)[-300:]}

    # 提交之后沙箱里还留着的东西 = 施工垃圾 / 未声明的改动。它们不参与合并，也不删；合并后 worktree 会因此保留。
    left = [p for _xy, p in _porcelain_paths(wt_path)]
    fin = (worker_worktree.finish(order_id, repo=repo_root, ledger_path=ledger_path, allow_dirty=bool(left))
           if ledger_path else worker_worktree.finish(order_id, repo=repo_root, allow_dirty=bool(left)))
    if not fin.get("ok"):
        _m = fin.get("merge") if isinstance(fin.get("merge"), dict) else {}
        if fin.get("stage") == "remove" and _m.get("merged") is True:
            # 合入已经成功，只是沙箱里有没提交的东西收不干净：交付已在主干，不能记成合入失败
            fin = dict(fin)
            fin["ok"] = True
            fin["remove_pending"] = True
        elif fin.get("stage") == "merge" and _m.get("ok") and _m.get("same_head"):
            # 分支与主干同头、沙箱里也没有可提交的东西：主干早已是这份内容。按已交付处理，不回炉。
            fin = dict(fin)
            fin["ok"] = True
            fin["already_up_to_date"] = True
        else:
            fin = dict(fin)
            if extras:
                fin["extras"] = extras[:40]
            return False, fin
    fin = dict(fin)
    if extras and not is_code:
        fin["undeclared_left_in_sandbox"] = extras[:40]
    m_info = fin.get("merge") if isinstance(fin.get("merge"), dict) else {}
    if m_info.get("merged") is False:
        if m_info.get("same_head"):
            return True, {"reason": "already-up-to-date", "finish": fin, "extras": extras}
        return False, {"reason": "merge-no-commits", "finish": fin, "extras": extras,
                       "hint": "分支无新提交，现场已保留（未被删除）"}
    branch_sha = m_info.get("branch_sha") or ""
    commit_sha = m_info.get("commit") or ""
    if branch_sha and commit_sha:
        anc = subprocess.run(["git", "-C", repo_root, "merge-base", "--is-ancestor", branch_sha, commit_sha],
                             capture_output=True, timeout=60)
        if anc.returncode != 0:
            return False, {"reason": "merge-not-ancestor", "branch_sha": branch_sha,
                           "commit": commit_sha, "finish": fin}
    if main_shas_before and allowed:
        changed_any = False
        for rel in allowed:
            main_target = os.path.join(repo_root, rel.replace("/", os.sep))
            sha_after = sha256_file(main_target) if os.path.exists(main_target) else ""
            if sha_after != (main_shas_before.get(rel) or ""):
                changed_any = True
        if not changed_any:
            # 合并成功但主干目标文件字节没变 = 主干早已含同样内容，没有新东西可交。按已交付处理。
            return True, {"reason": "main-target-sha-unchanged", "finish": fin, "merged": True}
    return True, fin



_EVIDENCE_PART_CHARS = 48000        # 单个证据分段文件上限（Read 一次读得完，也不撑爆上下文）
_EVIDENCE_EMBED_BUDGET = 9000       # 简报里内嵌 diff 的预算：按文件分摊，保证每个文件都看得到开头
_RECEIPT_EMBED_CHARS = 12000        # 施工回执内嵌上限


def write_diff_evidence(wt_repo: str, checks: dict, ev_dir: str, attempt: int, main_repo: str = "") -> dict:
    """把沙箱的累计 diff（自分叉点以来，含已提交 + 新建未跟踪文件）完整落盘：分段文件 + 索引 + 简报内嵌片段。

    旧实现只嵌「未提交」diff 的前 8000 字符：工人一旦 commit，质检就什么都看不见；多文件单只看得到第一个文件。
    2026-11-25 G52R1 的回执里写着「diff 超长，已截断 23510 字符」，质检据此把整单判成「用行数冒充落地」。
    """
    checks = checks or {}

    def _git(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", wt_repo, "-c", "core.quotepath=false"] + list(args),
                               capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""

    base_ref = str(((checks.get("cumulative") or {}).get("base_ref")) or "")
    synced = set(checks.get("live_synced_files") or [])
    changed = [dict(f) for f in (checks.get("files_changed") or []) if f.get("path") not in synced]
    if not changed:
        # 非 code 单的 Gate1 不产出 files_changed：这里按同一口径自己量一遍（自分叉点以来的累计改动 + 新建未跟踪文件）
        if not base_ref:
            head_branch = "main"
            try:
                hb = subprocess.run(["git", "-C", main_repo or wt_repo, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, timeout=30)
                cand = (hb.stdout or b"").decode("utf-8", "replace").strip() if hb.returncode == 0 else ""
                if cand and cand != "HEAD":
                    head_branch = cand
            except (OSError, subprocess.SubprocessError):
                pass
            base_ref = _git("merge-base", head_branch, "HEAD").strip()
        seen: set = set()
        for ln in (_git("diff", "--no-renames", "--numstat", base_ref).splitlines() if base_ref else []):
            parts = ln.split("\t")
            if len(parts) >= 3 and parts[2].strip() and parts[2].strip() not in seen:
                seen.add(parts[2].strip())
                changed.append({"path": parts[2].strip(), "added": 0 if parts[0] == "-" else int(parts[0]),
                                "deleted": 0 if parts[1] == "-" else int(parts[1])})
        for rel in _git("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            rel = rel.strip()
            if not rel or rel in seen:
                continue
            seen.add(rel)
            try:
                with open(os.path.join(wt_repo, rel.replace("/", os.sep)), "rb") as fh:
                    n_lines = fh.read().count(b"\n")
            except OSError:
                n_lines = 0
            changed.append({"path": rel, "added": n_lines, "deleted": 0})
    files = [f["path"] for f in changed]

    tracked = set(_git("ls-files", "-z").split("\0"))
    sections: dict[str, str] = {}
    for rel in files:
        abs_p = os.path.join(wt_repo, rel.replace("/", os.sep))
        if rel not in tracked and os.path.isfile(abs_p):      # 新建未跟踪文件：合成整文件 diff
            try:
                with open(abs_p, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                text = "（二进制新文件，%d B，正文不内嵌；核对大小与来源即可）\n" % len(raw)
            else:
                text = ("+++ b/%s（新建文件，全文）\n" % rel
                        + "\n".join("+" + ln for ln in raw.decode("utf-8", "replace").splitlines()) + "\n")
        else:
            text = _git("diff", "--no-renames", base_ref, "--", rel) if base_ref else ""
            if not text.strip():
                text = "（该文件相对分叉点无文本差异：可能只是改名或二进制内容变化）\n"
        sections[rel] = text

    order = sorted(sections)
    parts: list[dict] = []
    cur_lines: list[str] = []
    cur_chars = 0
    where: dict[str, list[str]] = {p: [] for p in order}

    def _flush() -> None:
        nonlocal cur_lines, cur_chars
        if cur_lines:
            parts.append({"lines": cur_lines, "chars": cur_chars})
        cur_lines, cur_chars = [], 0

    for rel in order:
        text = sections[rel]
        body = ["==== FILE: %s （共 %d 行）====\n" % (rel, text.count("\n"))] + [ln + "\n" for ln in text.splitlines()]
        for ln in body:
            if cur_chars + len(ln) > _EVIDENCE_PART_CHARS and cur_lines:
                _flush()
            cur_lines.append(ln)
            cur_chars += len(ln)
            tag = str(len(parts) + 1)
            if not where[rel] or where[rel][-1] != tag:
                where[rel].append(tag)
    _flush()

    stem = "verify_diff_attempt%d" % int(attempt or 0)
    rows: list[dict] = []
    for i, p in enumerate(parts, 1):
        name = "%s.part%d.diff" % (stem, i)
        with open(os.path.join(ev_dir, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(p["lines"])
        rows.append({"name": name, "chars": p["chars"], "lines": len(p["lines"])})
    total_chars = sum(r["chars"] for r in rows)
    total_lines = sum(r["lines"] for r in rows)
    commits = [ln for ln in _git("log", "--oneline", "%s..HEAD" % base_ref).splitlines() if ln.strip()] if base_ref else []

    idx = [
        "# 累计 diff 证据索引 · 第 %s 轮" % attempt, "",
        "- 口径：`git diff %s`（沙箱自分叉点以来的全部改动：已提交 + 暂存 + 未提交）+ 新建未跟踪文件全文" % (base_ref or "<base>"),
    ]
    idx += ["- 沙箱里的提交：`%s`" % ln for ln in commits[:40]] or ["- 沙箱里没有新提交（改动未提交）"]
    if synced:
        idx.append("- 与活仓逐字节相同的基线同步文件（不是本次交付，不列 diff）：%s" % "、".join(sorted(synced)[:30]))
    idx += [
        "- 共 %d 个文件 / %d 行 / %d 字符，分 %d 段" % (len(order), total_lines, total_chars, len(parts)), "",
        "| 分段文件 | 行 | 字符 | 含哪些文件 |", "|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        inside = [rel for rel in order if str(i) in where[rel]]
        idx.append("| `%s` | %d | %d | %s |" % (r["name"], r["lines"], r["chars"], "<br>".join(inside)))
    idx += ["", "## 按文件（行数 = 该文件 diff 的行数；分段 = 在第几段里）", ""]
    for rel in order:
        idx.append("- `%s` — %d 行 — 第 %s 段" % (rel, sections[rel].count("\n"), "、".join(where[rel]) or "-"))
    index_path = os.path.join(ev_dir, stem + ".index.md")
    atomic_write_text(index_path, "\n".join(idx) + "\n")

    # 简报内嵌：每个文件分摊一段预算，只贴开头，贴了多少如实写
    budget = _EVIDENCE_EMBED_BUDGET
    per_file = max(1200, min(4000, budget // max(1, len(order))))
    heads: list[str] = []
    embed: list[str] = []
    for rel in order:
        text = sections[rel]
        total = text.count("\n")
        if budget <= 0:
            heads.append("- `%s`：共 %d 行，本段未内嵌（见索引第 %s 段）" % (rel, total, "、".join(where[rel]) or "-"))
            continue
        out_lines: list[str] = []
        used = 0
        for ln in text.splitlines():
            if used + len(ln) + 1 > per_file:
                break
            out_lines.append(ln)
            used += len(ln) + 1
        budget -= used
        note = "全文已内嵌" if len(out_lines) >= total else "内嵌前 %d 行，其余 %d 行在分段文件里" % (len(out_lines), total - len(out_lines))
        heads.append("- `%s`：%s" % (rel, note))
        embed.append("#### `%s`（共 %d 行；%s）\n```diff\n%s\n```\n" % (rel, total, note, "\n".join(out_lines)))

    if order:
        embedded = (
            "\n### 沙箱累计改动证据（自分叉点 %s 以来：已提交 + 暂存 + 未提交 + 新建文件）\n" % (base_ref or "?")
            + ("- 沙箱里的提交：\n```\n%s\n```\n" % "\n".join(commits[:30]) if commits else "- 沙箱里没有新提交（改动尚未提交，同样计入）\n")
            + "- 改动文件 %d 个：%s\n" % (len(order), "、".join("`%s`(+%s/−%s)" % (f["path"], f.get("added", 0), f.get("deleted", 0)) for f in changed))
            + "- **完整 diff 已落盘**：索引 `%s`；分段文件 `%s`（共 %d 段 / %d 行 / %d 字符）。\n"
            % (index_path, os.path.join(ev_dir, stem + ".part<k>.diff"), len(parts), total_lines, total_chars)
            + "- 下面只内嵌每个文件的**开头**，合计不超过 %d 字符。**没内嵌 ≠ 没做**：要核对某个函数、某段改动，"
              "用 Read 打开对应分段文件（按索引定位），或用 Grep 在 `%s` 里搜 `FILE: <路径>` / 函数名。"
              "严禁因为「内嵌里没看到」就判未实现。\n\n" % (_EVIDENCE_EMBED_BUDGET, ev_dir)
            + "\n".join(heads) + "\n\n" + "\n".join(embed)
        )
    else:
        embedded = "\n### 沙箱累计改动证据\n- 沙箱相对分叉点没有文本改动。\n"
    return {"embedded": embedded, "index_path": index_path, "parts": [r["name"] for r in rows],
            "files": order, "total_chars": total_chars, "total_lines": total_lines}


def embed_delivery_receipt(ev_dir: str, attempt: int, start_ms: float) -> str:
    """施工交件回执进质检简报。工人被硬规要求写回执贴原始输出，旧简报却从不给质检看——证据链在这里断的。"""
    path = os.path.join(ev_dir, "FACTORY_W_DELIVERY.md")
    if not os.path.isfile(path):
        return "\n### 施工交件回执\n- 工人没有写 `FACTORY_W_DELIVERY.md`（规格若把回执证据列为验收项，按考卷处理）。\n"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    if start_ms:
        note = "本轮回执" if os.path.getmtime(path) * 1000 >= float(start_ms) else "注意：这是更早轮次留下的，本轮工人没有重写"
    else:
        note = "未能比对本轮开工时刻"
    if len(text) > _RECEIPT_EMBED_CHARS:
        cut = text[:_RECEIPT_EMBED_CHARS]
        text = cut + "\n\n[... 回执共 %d 字符，此处只嵌前 %d；全文在 `%s`，需要时用 Read 查看 ...]" % (len(text), _RECEIPT_EMBED_CHARS, path)
    return "### 施工交件回执 FACTORY_W_DELIVERY.md（%s）\n路径：`%s`\n```markdown\n%s\n```\n\n" % (note, path, text)


# ------------------------------------------------------------------ V5.0：轮次表 / 驻厂外聘专家做实 / 带种重下
class _Rounds:
    """for attempt in _Rounds(n)——轮次表可以在循环中途延长（专家救场轮、合入冲突的加时轮）。"""

    def __init__(self, stop: int):
        self.cur = 0
        self.stop = int(stop)

    def __iter__(self):
        return self

    def __next__(self):
        if self.cur >= self.stop:
            raise StopIteration
        self.cur += 1
        return self.cur

    def extend(self, n: int = 1) -> int:
        self.stop += int(n)
        return self.stop


# 专家越界只看源码类路径：state/ logs/ reports/ 这些运行态、证据目录，看板和内核随时在写，不算
_SOURCE_TOP_DIRS = ("scripts", "dashboard", "config", "docs", "knowledge", "lines", "hooks", "commands",
                    "collab", "comm", "verify_profiles")


def _is_source_path(path: str) -> bool:
    rel = str(path or "").replace("\\", "/").strip("/")
    if not rel:
        return False
    parts = rel.split("/")
    if "__pycache__" in parts[:-1]:                 # 新模块第一次被 import 就会生成，不是谁写了活仓
        return False
    if parts[-1].lower().endswith((".pyc", ".pyo", ".log", ".tmp")):
        return False
    return "/" not in rel or parts[0] in _SOURCE_TOP_DIRS


def _live_tree_snapshot(repo_root: str) -> dict:
    """活仓工作区指纹：{head, tracked: {路径: 内容sha 或 ""}, untracked: {路径: 1}}（只记 git status 看得见的）。"""
    snap: dict = {"head": "", "tracked": {}, "untracked": {}}
    if not worker_worktree or not os.path.isdir(repo_root):
        snap["error"] = "仓库不可用"
        return snap
    try:
        snap["head"] = worker_worktree.head_commit(repo_root) or ""
        tracked, untracked = worker_worktree.main_dirty_paths(repo_root)
        for _key, (_xy, path) in tracked.items():
            full = os.path.join(repo_root, str(path).replace("/", os.sep))
            snap["tracked"][str(path)] = sha256_file(full) if os.path.isfile(full) else ""
        snap["untracked"] = {str(path): 1 for _key, path in untracked}
        snap["unmerged"] = {str(path): 1 for _key, (_xy, path) in tracked.items() if "U" in _xy}
    except (OSError, subprocess.SubprocessError) as exc:
        snap["error"] = repr(exc)
    return snap


def _expert_overreach_check(repo_root: str, before: dict, evidence_dir: str, order_id: str) -> dict:
    """专家段结束后核对活仓。与快照相比新冒出来 / 变了内容的**源码**路径 = 专家越界（运行态目录不算；
    期间别的道合入主干改到的文件也不算）。越界文件挪进证据目录，跟踪文件恢复到当前 HEAD。返回动了谁。"""
    out: dict = {"repo": repo_root, "restored": [], "moved": [], "changed_during": [], "restore_failed": [],
                 "untouched": [], "commits_during": []}
    if not worker_worktree or not before or before.get("error"):
        return out
    after = _live_tree_snapshot(repo_root)
    if after.get("error"):
        return out
    try:
        # 主仓里有没结束的合并（别的道合入撞上冲突、正在收口）：这时候 checkout 会搅了人家的冲突解决
        merging = os.path.exists(worker_worktree.git_dir_file(repo_root, "MERGE_HEAD"))
    except (OSError, subprocess.SubprocessError):
        merging = True
    merged: set = set()
    if after["head"] and before.get("head") and after["head"] != before["head"]:
        r = subprocess.run(["git", "--no-optional-locks", "-C", repo_root, "-c", "core.quotepath=false",
                            "diff", "--name-only", "-z", before["head"], after["head"]], capture_output=True, timeout=60)
        merged = {p for p in (r.stdout or b"").decode("utf-8", "replace").split("\0") if p}
        r = subprocess.run(["git", "--no-optional-locks", "-C", repo_root, "-c", "core.quotepath=false",
                            "log", "--format=%h %an %s", "-5", "%s..%s" % (before["head"], after["head"])],
                           capture_output=True, timeout=60)
        out["commits_during"] = [ln[:120] for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines() if ln.strip()]
    ev_norm = os.path.normcase(os.path.abspath(evidence_dir)) if evidence_dir else ""

    def _under(path: str, folder_norm: str) -> bool:
        if not folder_norm:
            return False
        full = os.path.normcase(os.path.abspath(os.path.join(repo_root, str(path).replace("/", os.sep))))
        return full.startswith(folder_norm.rstrip("\\/") + os.sep)

    spill = os.path.join(evidence_dir, "expert_overreach") if evidence_dir else ""
    for path, digest in after["tracked"].items():
        if path in merged or path in after.get("unmerged", {}) or not _is_source_path(path) or _under(path, ev_norm):
            continue
        if before["tracked"].get(path) == digest:
            continue                                  # 快照里就是这个样子，不是这次动的
        full = os.path.join(repo_root, path.replace("/", os.sep))
        if spill and os.path.isfile(full) and digest:
            os.makedirs(os.path.dirname(os.path.join(spill, path.replace("/", os.sep))), exist_ok=True)
            shutil.copy2(full, os.path.join(spill, path.replace("/", os.sep)))
        if path in before["tracked"]:
            # 快照前它就带着别人的手改，期间又变了：说不清是谁动的，只留证，不替人回滚
            out["changed_during"].append(path)
            continue
        if merging:
            out["untouched"].append(path)            # 只留证：主仓合并没结束，不能动工作区
            continue
        if worker_worktree.wait_index_lock(repo_root, 5):
            r = subprocess.run(["git", "--no-optional-locks", "-C", repo_root, "checkout", "HEAD", "--", path],
                               capture_output=True, timeout=60)
            failed = r.returncode != 0
        else:
            failed = True
        out["restore_failed" if failed else "restored"].append(path)
    for path in after["untracked"]:
        if path in before["untracked"] or not _is_source_path(path) or _under(path, ev_norm) or not spill:
            continue
        full = os.path.join(repo_root, path.replace("/", os.sep))
        dst = os.path.join(spill, path.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(full, dst)
            out["moved"].append(path)
        except OSError:
            pass
    return out


def _overreach_hits(overreach: dict) -> dict:
    """专家段结束后，哪些仓真有越界痕迹（没还原 / 只留证的也算）。期间别的道正常合入主干不算。"""
    return {r: o for r, o in (overreach or {}).items()
            if o.get("restored") or o.get("moved") or o.get("changed_during")
            or o.get("restore_failed") or o.get("untouched")}


def _rescue_context(why: str, verdict: dict, feedback: str) -> str:
    """救场时交给专家的「现在卡在哪」。"""
    verdict = verdict or {}
    lines = ["- 卡住的原因：" + str(why),
             "- 质检确认的缺陷：confirmed P0=%s、P1=%s" % (verdict.get("confirmed_p0"), verdict.get("confirmed_p1"))]
    lines += ["  - " + str(d)[:300] for d in (verdict.get("defects") or [])[:12]]
    rework = str(verdict.get("rework_prompt") or "")
    if rework:
        lines.append("- 质检开的修改清单（厂内已照着试过、没解决，别原样再抄一遍）：" + rework[:1500])
    if feedback:
        lines.append("- 上一轮退回意见：" + str(feedback)[:1200])
    return "\n".join(lines)


def _run_expert_stage(order_id: str, attempt: int, *, spec_text: str, wt_path: str, wt_branch: str, targets: list,
                      ev_dir: str, log_path: str, repos: list, dispatch, context: str = "", mode: str = "opening",
                      title: str = "") -> dict:
    """驻厂外聘专家出场：在本单沙箱的声明目标里动手，活仓只读。每单每种模式只跑一次。
    返回 {ran, reason, mode, notes_path, notes, notes_source, changed, overreach, duration_sec}；
    没排上席 / 池子关着时 notes 是按规格给的底稿（ran=False），施工照底稿继续。"""
    out: dict = {"ran": False, "reason": "", "mode": mode, "notes": "", "notes_source": "", "changed": [], "overreach": {}}
    try:
        import expert_seat
    except Exception as _exc:  # noqa: BLE001
        out["reason"] = "expert_seat 模块不可用：%r" % (_exc,)
        return out
    fallback = expert_seat.diagnose(spec_text)
    out.update(notes=fallback, notes_source="fallback")
    notes_path = os.path.join(ev_dir, "expert_notes_rescue.md" if mode == "rescue" else "expert_notes.md")
    out["notes_path"] = notes_path
    repos = [r for r in dict.fromkeys([STEWARD_ROOT] + list(repos or [])) if r and os.path.isdir(r)]
    if not expert_seat.pool_enabled():
        out["reason"] = "pool-disabled"
    elif not expert_seat.try_acquire(order_id):
        out["reason"] = "pool-full"
    else:
        out["held"] = True
    if not out.get("held"):
        atomic_write_text(notes_path, fallback)
        append_v3_event(order_id, attempt, "expert_wait", "驻厂外聘专家", "专家席没排上（%s），本单先用底稿" % out["reason"])
        return out
    before = {r: _live_tree_snapshot(r) for r in repos}
    prompt = expert_seat.research_prompt(spec_text, fallback, wt_path=wt_path, wt_branch=wt_branch, targets=targets,
                                         notes_path=notes_path, live_roots=repos, context=context, mode=mode)
    expert_seat.stage_begin(order_id, attempt, mode, title)
    append_v3_event(order_id, attempt, "expert_start", "驻厂外聘专家",
                    ("救场" if mode == "rescue" else "开工先手") + "：在本单沙箱的声明目标里动手（活仓只读）")
    t0 = now_ts()
    try:
        res, fuse = dispatch(attempt_no=attempt, prompt=prompt, cwd=wt_path, timeout_sec=EXPERT_TIMEOUT_SEC, role="worker",
                             log_path=log_path, task_title=f"【V5-驻厂专家】{order_id}")
    finally:
        # 不论专家怎么收场，活仓核对、席位记录、租约放回都要做
        out["overreach"] = {r: _expert_overreach_check(r, before[r], ev_dir, order_id) for r in repos}
        expert_seat.stage_end(order_id)
        expert_seat.release(order_id)
        out["held"] = False
    out["duration_sec"] = round(now_ts() - t0, 1)
    spoken = "" if fuse else (res.get("stdout") or "").strip()
    out.update(ran=not fuse, stdout_chars=len(spoken))
    file_notes = ""
    if os.path.isfile(notes_path):
        try:
            file_notes = open(notes_path, encoding="utf-8", errors="replace").read().strip()
        except OSError:
            file_notes = ""
    if file_notes and expert_seat.notes_complete(file_notes):
        out.update(notes=file_notes, notes_source="file")
    elif spoken and expert_seat.notes_complete(spoken):
        out.update(notes=spoken, notes_source="stdout")
        atomic_write_text(notes_path, spoken)
    else:
        if file_notes:
            atomic_write_text(notes_path + ".incomplete", file_notes)     # 专家写了但五节不全，原文留底
        atomic_write_text(notes_path, fallback)
    try:
        out["changed"] = [p for _xy, p in _porcelain_paths(wt_path)][:20]
    except (OSError, subprocess.SubprocessError):
        out["changed"] = []
    hit = _overreach_hits(out["overreach"])
    out["overreach_hit"] = hit
    v4_trace("expert_stage", order_id, attempt=attempt, mode=mode, ran=out["ran"], notes_source=out["notes_source"],
             notes_path=notes_path, changed=out["changed"], overreach=hit,
             lane_merges={r: o.get("commits_during") for r, o in out["overreach"].items() if o.get("commits_during")},
             duration_sec=out["duration_sec"])
    if hit:
        _what = "；".join("%s 还原 %s / 挪走 %s / 只留证 %s / 没还原 %s" % (
            os.path.basename(r), o.get("restored"), o.get("moved"), o.get("changed_during"),
            (o.get("restore_failed") or []) + (o.get("untouched") or []))
            for r, o in hit.items())
        _left = sum(len(o.get("restore_failed") or []) + len(o.get("untouched") or []) for o in hit.values())
        append_v3_event(order_id, attempt, "expert_overreach", "驻厂外聘专家",
                        ("专家越界写了活仓，" + ("有 %d 处没还原、须人工处理：" % _left if _left else "已回滚并留证：") + _what)[:400])
        _append_patrol(f"[V5_EXPERT_OVERREACH {iso_now()}] {order_id}: {_what[:600]}")
    append_v3_event(order_id, attempt, "expert_done", "驻厂外聘专家",
                    ("救场结束：" if mode == "rescue" else "先手结束：") + (out["notes"].splitlines()[0][:80] if out["notes"] else "")
                    + "（沙箱改动 %d 处，%ss）" % (len(out["changed"]), out["duration_sec"]))
    return out


def _expert_brief_block(res: dict, rescue: bool = False) -> str:
    """施工派工单里转述专家结论的那一段。"""
    if not res or not res.get("notes"):
        return ""
    if res.get("ran") and rescue:
        head = "## 驻厂外聘专家已救场出手——不要回退这些改动，按「交给施工」继续"
    elif res.get("ran"):
        head = "## 驻厂外聘专家已在本单沙箱先动手——不要回退这些改动，按「交给施工」继续"
    else:
        head = "## 驻厂外聘专家底稿（专家席没排上，这是按规格给的判断，照「交给施工」做）"
    body = "\n" + head + "\n"
    if res.get("changed"):
        body += "专家改过（未提交，已在沙箱里）：" + "、".join(str(p) for p in res["changed"][:12]) + "\n"
    return body + str(res["notes"]) + "\n"


def _seed_sandbox(wt_path: str, repo_root: str, seed_from: str) -> dict:
    """带种重下：新一代订单的沙箱先把上一代分支上已提交的成果并进来。并得拢 → 双门直接在它上面验；
    并不拢 → 沙箱保持干净的主干状态，把冲突文件交给工人在沙箱里解决。"""
    out: dict = {"seed_from": seed_from, "ok": False, "noop": False, "conflicts": [], "commit": "", "reason": ""}
    if not worker_worktree or not seed_from or not os.path.isdir(wt_path):
        return out
    branch = "wt/" + worker_worktree.safe_name(seed_from)
    if not worker_worktree.branch_exists(repo_root, branch):
        out["reason"] = "上代分支不存在（沙箱已清理）"
        return out
    r = subprocess.run(["git", "--no-optional-locks", "-C", wt_path, "rev-list", "--count", "HEAD.." + branch],
                       capture_output=True, timeout=60)
    ahead = (r.stdout or b"").decode("utf-8", "replace").strip()
    if ahead.isdigit() and int(ahead) == 0:
        out.update(ok=True, noop=True, reason="主干已含上代成果")
        return out
    r = subprocess.run(["git", "--no-optional-locks", "-C", wt_path, "merge", "--no-ff", "-m",
                        "seed: 带入上一代 %s 已提交的成果（带种重下，工人不必从零重做）" % seed_from, branch],
                       capture_output=True, timeout=300)
    if r.returncode == 0:
        out.update(ok=True, commits=ahead)
        out["commit"] = worker_worktree.head_commit(wt_path) or ""
        return out
    c = subprocess.run(["git", "--no-optional-locks", "-C", wt_path, "-c", "core.quotepath=false", "diff", "--name-only", "--diff-filter=U"],
                       capture_output=True, timeout=60)
    out["conflicts"] = [ln.strip() for ln in (c.stdout or b"").decode("utf-8", "replace").splitlines() if ln.strip()][:30]
    subprocess.run(["git", "--no-optional-locks", "-C", wt_path, "merge", "--abort"], capture_output=True, timeout=60)
    out["reason"] = "与主干有冲突，已回退到干净主干，交给工人在沙箱里解决"
    return out


def _seed_brief_block(info: dict, wt_path: str) -> str:
    """派工单里说明「沙箱里已经带着上一代的成果」。"""
    if not info or info.get("noop") or (not info.get("ok") and not info.get("conflicts")):
        return ""
    branch = "wt/" + (worker_worktree.safe_name(str(info.get("seed_from") or "")) if worker_worktree else str(info.get("seed_from") or ""))
    if info.get("ok"):
        return ("\n## 上一代成果已带进沙箱（带种重下）\n"
                f"- 来自分支 `{branch}`（{info.get('commits', '?')} 个提交，并入提交 {str(info.get('commit') or '')[:10]}）。\n"
                "- **不要推翻重做**：先 `git log --oneline -8` 和 `git diff main...HEAD --stat` 看清上一代做了什么，在它基础上继续、补齐验收。\n")
    return ("\n## 上一代成果需要你先并进沙箱（带种重下·有冲突）\n"
            f"- 上一代已提交的成果在分支 `{branch}`，与当前主干有冲突，内核没有自动并入。\n"
            "- 第一步：在沙箱里执行 `git merge " + branch + "`，逐个解决下列文件的冲突并提交（以上一代的功能为准、同时保住主干的新改动），"
            "然后在其基础上继续施工：\n  - " + "\n  - ".join(str(p) for p in (info.get("conflicts") or [])[:12]) + "\n")


def execute_job(q: dict, job: dict) -> dict:
    order_id = job["order_id"]
    run_id = f"{order_id}_{_dt.datetime.now().strftime('%H%M%S')}"
    ev_dir = os.path.join(BP_ROOT, "reports", "evidence", order_id)
    os.makedirs(ev_dir, exist_ok=True)

    # Pre-check factory daily budget fuse BEFORE launching anything
    ledger = load_budget_ledger()
    if ledger.get("circuit_open") or float(ledger.get("daily_cash_cny", 0.0)) > DAILY_BUDGET_CAP_CNY:
        fuse_msg = ledger.get("fuse_reason") or f"全厂当日花费已超 ¥{DAILY_BUDGET_CAP_CNY:.2f}，拒绝新开单"
        _append_patrol(f"[V3_FUSE_BLOCK {iso_now()}] {order_id}: {fuse_msg}")
        job["status"] = "failed"
        job["finished_at"] = iso_now()
        job["finished_ts"] = now_ts()
        _lane_qcommit(job, q)
        return {"run_id": run_id, "order_id": order_id, "final_status": "failed", "final_summary": fuse_msg, "e2e_total_sec": 0.0, "dispatch_latency_sec": 0.0, "attempts_used": 0, "gateway_totals": {}}

    repo_root = job.get("repo") or STEWARD_ROOT
    try:
        targets = resolve_job_targets(job)
    except ValueError as _t_exc:
        _append_patrol(f"[TARGET_MISSING {iso_now()}] {order_id}: {_t_exc}")
        append_v3_event(order_id, 0, "needs_human", "目标文件缺失", str(_t_exc))
        job["status"] = "needs_human"
        job["needs_reason"] = "target_missing"
        job["finished_at"] = iso_now()
        job["finished_ts"] = now_ts()
        _lane_qcommit(job, q)
        return {"run_id": run_id, "order_id": order_id, "final_status": "needs_human",
                "final_summary": str(_t_exc), "e2e_total_sec": 0.0, "dispatch_latency_sec": 0.0,
                "attempts_used": 0, "gateway_totals": {}}
    _repo_norm = os.path.normcase(os.path.abspath(repo_root))
    _escaped = [t for t in targets
                if not os.path.normcase(os.path.abspath(os.path.join(repo_root, t.replace("/", os.sep)))).startswith(_repo_norm + os.sep)]
    if _escaped:
        _msg = "目标路径越出仓库：" + "、".join(_escaped)
        _append_patrol(f"[TARGET_ESCAPE {iso_now()}] {order_id}: {_msg}")
        append_v3_event(order_id, 0, "needs_human", "目标路径非法", _msg)
        job["status"] = "needs_human"
        job["needs_reason"] = "target_escape"
        job["finished_at"] = iso_now()
        job["finished_ts"] = now_ts()
        _lane_qcommit(job, q)
        return {"run_id": run_id, "order_id": order_id, "final_status": "needs_human",
                "final_summary": _msg, "e2e_total_sec": 0.0, "dispatch_latency_sec": 0.0,
                "attempts_used": 0, "gateway_totals": {}}
    target_rel = targets[0]
    main_target_abs = os.path.join(repo_root, target_rel.replace("/", os.sep))  # P0热修2026-11-24：G38/G39多目标重构遗失定义→run_report NameError 崩溃回环（运维直修）
    main_shas_before = {}
    for _t in targets:
        _abs = os.path.join(repo_root, _t.replace("/", os.sep))
        main_shas_before[_t] = sha256_file(_abs) if os.path.exists(_abs) else ""
    main_file_sha_before = main_shas_before[target_rel]
    six_baseline = snapshot_six_pages()
    spec_text = open(job["spec_file"], "r", encoding="utf-8", errors="replace").read()

    job["status"] = "running"
    job["started_ts"] = now_ts()
    job["started_at"] = iso_now()
    job["run_id"] = run_id
    _lane_qcommit(job, q)                  # G40b：running 落账（并行道=锁内按行合并）

    gw_run_start = get_gateway_status()
    e2e_t0 = now_ts()
    run_start_ms = now_ms()
    v4 = load_v4_flags()
    wall_cap = order_wallclock_cap(v4)
    _V4_RUN_CTX["run_id"] = run_id
    _V4_RUN_CTX["order_id"] = order_id
    if v4.get("wallclock_live") or any(v4.get(k) for k in ("merge_gate", "char_icon_hard_fail", "uispec_prompt", "drop_details_gate")):
        dispatch_latency = round(float(job["started_ts"]) - dispatch_base_ts(job), 2)
    else:
        dispatch_latency = round(float(job["started_ts"]) - float(job.get("enqueued_ts") or job["started_ts"]), 2)
    v4_trace("run_start", order_id, flags=v4, wallclock_cap_sec=wall_cap, dispatch_latency_sec=dispatch_latency,
             kernel_build=KERNEL_BUILD, infra_retries=int(job.get("infra_retries") or 0),
             job={k: v for k, v in job.items() if k not in ("stdout",)}, spec_sha16=hashlib.sha256(spec_text.encode("utf-8", "replace")).hexdigest()[:16],
             spec_chars=len(spec_text), main_target_sha16=main_file_sha_before[:16], gateway_status_at_start=gw_run_start.get("totals") if isinstance(gw_run_start, dict) else None)

    # V4.0-P2：解析本单规格断言（自带 / 继承原单）；require_assertions 下缺断言不开工
    spec_asserts: list[dict] = []
    assert_src = ""
    # V5.0-real.5：code 单的断言块也解析——目标里带看板页面的 code 单，Gate1 同样上真引擎逐条判（见 _code_runtime_evidence）
    _assert_kind = normalize_order_kind(job.get("kind") or "ui_dashboard")
    if v4.get("assertion_gate") and _assert_kind in ("ui", "code"):
        spec_asserts, _a_errs, assert_src = resolve_job_assertions(job, spec_text, q)
        v4_trace("assertions_loaded", order_id, source=assert_src, count=len(spec_asserts), errors=_a_errs, assertions=spec_asserts)
        if _a_errs and _assert_kind == "code":
            # code 单的断言块是可选的，而且实际上多是散文清单（2026-11-26 扫历史：48 张带块的 code 考卷只有 1 张是合法 JSON）：
            # 不拦单、不上真引擎机检，照旧只当验收条款交质检；要机检就写成 JSON 数组
            spec_asserts, _a_errs = [], []
        # ui 单必须带断言；code 单断言可选
        if v4.get("require_assertions") and (_a_errs or (not spec_asserts and _assert_kind == "ui")):
            _why = "；".join(_a_errs[:6]) if _a_errs else "规格书缺 ```assertions``` 断言块"
            _msg = f"[规格不合格·未开工] {_why}。请按《规格断言编写指南》补断言后重派（V4 require_assertions）"
            _append_patrol(f"[V4_SPEC_REJECT {iso_now()}] {order_id}: {_why}")
            append_v3_event(order_id, 0, "needs_human", "规格断言校验", _msg)
            v4_trace("spec_rejected", order_id, errors=_a_errs, spec_file=job.get("spec_file"))
            job["status"] = "needs_human"
            job["needs_reason"] = "spec_missing_assertions"
            job["finished_at"] = iso_now()
            job["finished_ts"] = now_ts()
            _lane_qcommit(job, q)
            _V4_RUN_CTX.pop("run_id", None)
            _V4_RUN_CTX.pop("order_id", None)
            return {"run_id": run_id, "order_id": order_id, "final_status": "needs_human", "final_summary": _msg, "e2e_total_sec": round(now_ts() - e2e_t0, 2), "dispatch_latency_sec": dispatch_latency, "attempts_used": 0, "gateway_totals": {}}

    _vis_n = sum(len(a["viewports"]) for a in spec_asserts if a.get("metric") == VISUAL_METRIC)
    if _vis_n:
        # V4.0-P3：像素比对单 Gate1 要多截 2×N 张整页图，墙钟自动延长（含一次补证），上限 +30 分钟
        _ext = min(1800, 2 * (60 + 10 * _vis_n))
        wall_cap += _ext
        v4_trace("wallclock_extend", order_id, visual_shots=_vis_n * 2, extend_sec=_ext, cap_sec=wall_cap)
    rw_state = rewrite_authorized(order_id, v4, spec_asserts)
    if rw_state:
        v4_trace("rewrite_authorization", order_id, state=rw_state)
        if rw_state != "ok":
            _append_patrol(f"[V4_REWRITE_AUTH {iso_now()}] {order_id}: {rw_state}")

    ok_wt, wt_path, wt_branch = _prepare_worktree(order_id, repo_root, target_rel=target_rel, main_sha_before=main_file_sha_before,
                                                   preserve_if_modified=True, main_shas_before=main_shas_before)
    if not ok_wt:
        job["status"] = "failed"
        job["finished_at"] = iso_now()
        job["finished_ts"] = now_ts()
        _lane_qcommit(job, q)
        return {"run_id": run_id, "order_id": order_id, "final_status": "failed", "final_summary": wt_branch, "e2e_total_sec": round(now_ts() - e2e_t0, 2), "dispatch_latency_sec": dispatch_latency, "attempts_used": 0, "gateway_totals": {}}

    wt_target_abs = os.path.join(wt_path, target_rel)
    # V5.0 带种重下：新一代订单的沙箱先把上一代已提交的成果并进来。2026-11-25 下午 G44R3 / G46R3 双门全过
    # 却合不进，泵再派一代时工人从零重做——同一件活做了四遍。现在新一代接着上一代的成果干。
    seed_info = _seed_sandbox(wt_path, repo_root, str(job.get("seed_from") or "")) if job.get("seed_from") else {}
    if seed_info:
        v4_trace("seed", order_id, **seed_info)
        append_v3_event(order_id, 0, "seed", "带种重下",
                        ("已带入上一代 %s 的成果（%s 个提交）" % (seed_info.get("seed_from"), seed_info.get("commits"))) if seed_info.get("ok") and not seed_info.get("noop")
                        else ("主干已含上一代成果，无需带入" if seed_info.get("noop") else ("上一代成果与主干冲突（%d 个文件），交给工人在沙箱解决" % len(seed_info.get("conflicts") or [])
                                                                                             if seed_info.get("conflicts") else str(seed_info.get("reason") or "未能带入"))))
    seed_note = _seed_brief_block(seed_info, wt_path)
    prev_feedback = ""
    _last_regress_files: list = []          # 上一轮硬闸点名的回归用例文件（返工话 / 救场段据此放行）
    attempts_log: list[dict] = []
    final_status = "failed"
    final_summary = ""
    fuse_tripped = False
    card_cost_cny = 0.0
    merge_dur_sec = 0.0
    merge_result: dict = {}
    max_attempts = int(job.get("max_attempts") or 2)
    reused_existing_draft = _sandbox_has_draft(wt_path, main_shas_before, branch=wt_branch, repo_root=repo_root)
    if seed_info.get("ok") and not seed_info.get("noop") and not job.get("seed_gates"):
        # 带种但上一代没过双门：不跳过施工。先让施工照上一代的质检结论在带种成果上改，再过双门——
        # 否则白跑一轮双门把同样的缺陷再确认一遍。泵只在上一代双门已过（只差合入）时才给 seed_gates。
        reused_existing_draft = False
    if str(job.get("resume_at") or "") == "gates" and not reused_existing_draft:
        # 本该只补「双门 + 合入」的单，沙箱里却没有可复用的成果（被清理 / 没提交上）：如实记一笔，改走正常施工
        append_v3_event(order_id, 0, "resume_fallback", "内核", "resume_at=gates 但沙箱没有可复用成果，改走正常施工轮")
        job["resume_at"] = ""
    loop_max = max_attempts + (1 if reused_existing_draft else 0)
    v4_trace("sandbox_ready", order_id, path=wt_path, branch=wt_branch, reused_draft=reused_existing_draft,
             targets=targets, main_shas={k: (v or "")[:16] for k, v in main_shas_before.items()}, seed=seed_info,
             resume_at=str(job.get("resume_at") or ""))

    kind = job.get("kind") or "ui_dashboard"
    norm_kind = normalize_order_kind(kind)
    v_profile_key, v_role_label, v_profile_path, v_profile_text = load_verify_profile(kind)
    prev_p0_count: int | None = None
    prior_items: list[dict] = []   # V4.0-P2 销账：上一轮 confirmed P0/P1

    wk_probe = probe_weekend_glm()
    _simple_order = (norm_kind == "code" and len(targets or []) <= 2 and len(spec_asserts or []) <= 6)
    direct_pools = {
        "worker": _direct_lane_pool("worker", simple=_simple_order, ab="" if _simple_order else _direct_ab_arm(order_id)),
        "verifier": _direct_lane_pool("verifier"),
    }
    direct_idx = {"worker": 0, "verifier": 0}
    active_lane: dict | None = (dict(direct_pools["worker"][0]) if direct_pools.get("worker") else None)
    append_v3_event(
        order_id,
        0,
        "lane_select",
        "择道探针",
        f"probe={'available' if wk_probe.get('available') else 'unavailable'} "
        f"direct={(active_lane or {}).get('lane', 'none')} "
        f"model={(active_lane or {}).get('provider_id','')}/{(active_lane or {}).get('model_id','')} "
        f"reason={wk_probe.get('reason','')}",
    )
    _wk0 = (direct_pools.get("worker") or [{}])[0]
    _vf0 = (direct_pools.get("verifier") or [{}])[0]
    if _wk0.get("direct"):
        append_v3_event(
            order_id,
            0,
            "direct_lane",
            "内核直连择道",
            f"worker={_wk0.get('lane', '')}/{_wk0.get('model_id', '')} "
            f"verifier={_vf0.get('lane', '')}/{_vf0.get('model_id', '')} "
            f"pool={'→'.join(str(x.get('lane', '')) for x in direct_pools.get('worker') or [])} "
            f"ab={'skip' if _simple_order else _direct_ab_arm(order_id)} shift={_wk0.get('shift', '')}",
        )
    job_wk_tokens = 0
    job_gw_tokens = 0
    ledger = load_budget_ledger()

    def _merge_gw_delta(d1: dict, d2: dict) -> dict:
        out = {k: int(d1.get(k, 0) or 0) + int(d2.get(k, 0) or 0) for k in ("requests", "ok", "fail", "tokens_in", "tokens_out", "tokens_cached", "tokens_est_in", "tokens_est_out", "estimated_requests")}
        out["cost_cny"] = round(float(d1.get("cost_cny", 0.0) or 0.0) + float(d2.get("cost_cny", 0.0) or 0.0), 6)
        out["cost_est_cny"] = round(float(d1.get("cost_est_cny", 0.0) or 0.0) + float(d2.get("cost_est_cny", 0.0) or 0.0), 6)
        merged_lanes: dict = {}
        for src in (d1.get("lanes") or {}, d2.get("lanes") or {}):
            for lk, lv in src.items():
                cur = merged_lanes.setdefault(lk, {"ok": 0, "fail": 0, "tokens_in": 0, "tokens_out": 0, "cost_cny": 0.0})
                for fld in ("ok", "fail", "tokens_in", "tokens_out"):
                    cur[fld] += int(lv.get(fld, 0) or 0)
                cur["cost_cny"] = round(cur["cost_cny"] + float(lv.get("cost_cny", 0.0) or 0.0), 6)
        out["lanes"] = merged_lanes
        return out

    def _dispatch_with_lane_fallback(
        attempt_no: int,
        prompt: str,
        cwd: str,
        timeout_sec: int,
        role: str,
        log_path: str | None = None,
        task_title: str = "",
        disallowed_tools: str = "",
        prefer_desktop: bool = True,
        prompt_desktop: str = "",
    ) -> tuple[dict, str]:
        nonlocal active_lane, card_cost_cny, job_wk_tokens, job_gw_tokens, ledger, direct_idx
        refresh_lock()
        if v4.get("wallclock_live"):
            timeout_sec = cap_stage_timeout(timeout_sec, wallclock_remaining(e2e_t0, wall_cap))
        v4_trace("stage_start", order_id, attempt=attempt_no, role=role, title=task_title, timeout_sec=timeout_sec,
                 prompt_chars=len(prompt or ""), prompt_sha16=hashlib.sha256((prompt or "").encode("utf-8", "replace")).hexdigest()[:16],
                 desktop_prompt_chars=len(prompt_desktop or "") or None,
                 lane=(active_lane or {}).get("lane", "gateway"), disallowed_tools=disallowed_tools, log_path=log_path)
        _pool = direct_pools.get(role) or direct_pools.get("worker") or []
        _lane_i = int(direct_idx.get(role, 0))
        used_override = dict(_pool[_lane_i % len(_pool)]) if _pool else (dict(active_lane) if active_lane else None)
        is_wk_lane = bool(used_override and used_override.get("lane") == "weekend-glm")
        eff_timeout = min(timeout_sec, WEEKEND_GLM_BUILDER_TIMEOUT_SEC if role == "worker" else WEEKEND_GLM_VERIFIER_TIMEOUT_SEC) if is_wk_lane else timeout_sec
        eff_stall = WEEKEND_GLM_STALL_SEC if is_wk_lane else 0
        res = run_headless_zcode(
            prompt=prompt,
            cwd=cwd,
            timeout_sec=eff_timeout,
            role=role,
            attachments=None,
            log_path=log_path,
            task_title=task_title,
            disallowed_tools=disallowed_tools,
            prefer_desktop=prefer_desktop,
            lane_override=used_override,
            stall_timeout_sec=eff_stall,
            pulse_order=order_id,
            pulse_round=attempt_no,
            prompt_desktop=prompt_desktop,
        )
        if used_override and used_override.get("direct") and not is_wk_lane:
            _bad = (res.get("exit_code", 0) != 0 or res.get("timed_out") or res.get("stall_timeout")
                    or (role == "verifier" and not (res.get("stdout") or "").strip()))
            if _bad:
                _pool_now = direct_pools.get(role) or []
                if _pool_now:
                    direct_idx[role] = int(direct_idx.get(role, 0)) + 1
                    _nxt = _pool_now[direct_idx[role] % len(_pool_now)]
                    active_lane = dict(_nxt)
                    append_v3_event(order_id, attempt_no, "lane_fallback", "直连换道",
                                    f"{used_override.get('lane')} 运行异常 (rc={res.get('exit_code')} "
                                    f"timeout={res.get('timed_out')} stall={res.get('stall_timeout')})，"
                                    f"下一阶段切 {_nxt.get('lane')} ({_nxt.get('provider_id')}/{_nxt.get('model_id')})")
        direct_u = res.get("direct_usage")
        gw_tok_s = int((res.get("gateway_delta") or {}).get("tokens_in", 0) or 0) + int((res.get("gateway_delta") or {}).get("tokens_out", 0) or 0)
        wk_tok_s = int((direct_u or {}).get("tokens_in", 0) or 0) + int((direct_u or {}).get("tokens_out", 0) or 0)
        job_gw_tokens += gw_tok_s
        job_wk_tokens += wk_tok_s
        ledger, card_cost_cny, stage_fuse = record_stage_budget(
            order_id,
            res["gateway_delta"],
            card_cost_cny,
            direct_usage=direct_u,
            lane_quota_override={"weekend-glm": used_override["total_units"]} if is_wk_lane else None,
        )
        if is_wk_lane:
            empty_verifier = role == "verifier" and not (res.get("stdout") or "").strip()
            du_failed = bool(direct_u and int(direct_u.get("fail", 0) or 0) > 0 and int(direct_u.get("ok", 0) or 0) == 0)
            if res.get("exit_code", 0) != 0 or res.get("timed_out") or res.get("stall_timeout") or empty_verifier or du_failed:
                err_tag = f"rc={res.get('exit_code')} timed_out={res.get('timed_out')} stall={res.get('stall_timeout', False)} empty_v={empty_verifier} du_err={(direct_u or {}).get('error_codes')}"
                is_severe = bool(res.get("timed_out") or res.get("stall_timeout") or any(k in err_tag.lower() for k in ("429", "rate", "502", "503", "504", "timeout", "stall", "auth", "scope")))
                ledger = _record_weekend_glm_health(ok=False, reason=err_tag, severe=is_severe)
                active_lane = None
                _append_patrol(f"[V3_LANE_QUOTA {iso_now()}] {order_id} weekend-glm 运行异常 ({err_tag})，立即静默回落网关池（不停机/不重试免费票）")
                append_v3_event(order_id, attempt_no, "lane_fallback", "通道回落", f"weekend-glm 运行异常 ({err_tag})，静默回落网关池重跑本阶段")
                fb_timeout = cap_stage_timeout(timeout_sec, wallclock_remaining(e2e_t0, wall_cap)) if v4.get("wallclock_live") else timeout_sec
                fb_res = run_headless_zcode(
                    prompt=prompt,
                    cwd=cwd,
                    timeout_sec=fb_timeout,
                    role=role,
                    attachments=None,
                    log_path=log_path,
                    task_title=task_title,
                    disallowed_tools=disallowed_tools,
                    prefer_desktop=prefer_desktop,
                    lane_override=None,
                    stall_timeout_sec=0,
                    pulse_order=order_id,
                    pulse_round=attempt_no,
                    prompt_desktop=prompt_desktop,
                )
                gw_tok_2 = int((fb_res.get("gateway_delta") or {}).get("tokens_in", 0) or 0) + int((fb_res.get("gateway_delta") or {}).get("tokens_out", 0) or 0)
                job_gw_tokens += gw_tok_2
                ledger, card_cost_cny, stage_fuse = record_stage_budget(order_id, fb_res["gateway_delta"], card_cost_cny)
                fb_res["duration_sec"] = round(float(res.get("duration_sec", 0.0)) + float(fb_res.get("duration_sec", 0.0)), 2)
                fb_res["gateway_delta"] = _merge_gw_delta(res.get("gateway_delta") or {}, fb_res.get("gateway_delta") or {})
                fb_res["sessions"] = list(res.get("sessions") or []) + list(fb_res.get("sessions") or [])
                fb_res["fallback_from"] = "weekend-glm"
                res = fb_res
            else:
                if role != "verifier":
                    ledger = _record_weekend_glm_health(ok=True)
                mk = _month_key()
                wk_month_used = int(((ledger.get("month_lane_tokens") or {}).get(mk) or {}).get("weekend-glm", 0))
                wk_quota = int(used_override.get("total_units") or LANE_TOKEN_QUOTA_MONTH.get("weekend-glm") or 500_000_000)
                re_p = probe_weekend_glm()
                if wk_month_used >= wk_quota * LANE_FUSE_PCT or not re_p.get("available"):
                    active_lane = None
                    _append_patrol(
                        f"[V3_LANE_QUOTA {iso_now()}] {order_id} weekend-glm 触 90% 熔断线或不可用 "
                        f"(used={wk_month_used}/{wk_quota}, probe={re_p.get('reason','')})，后续阶段静默回落网关池（不停机）"
                    )
                    append_v3_event(order_id, attempt_no, "lane_fallback", "通道回落", f"weekend-glm 触线或不可用 ({re_p.get('reason','')})，后续阶段回落网关池（不停机）")
        refresh_lock()
        v4_trace("stage_end", order_id, attempt=attempt_no, role=role, result=_res_digest(res),
                 stdout_chars=len(res.get("stdout") or ""), stdout_tail=(res.get("stdout") or "")[-4000:],
                 card_cash_after=card_cost_cny, fuse=stage_fuse)
        return res, stage_fuse

    # V5.0：轮次表可以中途延长。专家救场轮不占施工的常规轮次；合入冲突多给一轮专门解冲突。
    _rounds = _Rounds(min(loop_max, 3))
    _rescue_used = False
    _rescue_pending = ""
    _conflict_extra_used = False

    def _call_expert_rescue(why: str, verdict: dict) -> bool:
        """本单将因「厂内解决不了」收场时请驻厂外聘专家出手：专家先在本单沙箱的声明目标里动手，
        之后多给一轮施工。每单只救一次；专家池关着就不救。返回是否排上了救场轮。"""
        nonlocal _rescue_used, _rescue_pending, wall_cap
        if _rescue_used:
            return False
        try:
            import expert_seat as _es
            if not _es.pool_enabled():
                return False
        except Exception:  # noqa: BLE001
            return False
        if not v4.get("wallclock_live") and now_ts() - e2e_t0 > _wall_lim - _wall_floor - EXPERT_RESCUE_MIN_SEC:
            append_v3_event(order_id, attempt, "expert_skip", "驻厂外聘专家", "想请专家救场，但整单墙钟剩得不够（%s）" % why[:120])
            return False
        _rescue_used = True
        _rescue_pending = _rescue_context(why, verdict, prev_feedback)
        if v4.get("wallclock_live"):
            wall_cap += EXPERT_RESCUE_EXT_SEC
        _rounds.extend(1)
        append_v3_event(order_id, attempt, "expert_rescue", "驻厂外聘专家", "%s；请专家在本单沙箱出手，之后追加一轮施工" % why[:200])
        v4_trace("expert_rescue_scheduled", order_id, attempt=attempt, why=why, wall_cap_sec=wall_cap)
        return True

    for attempt in _rounds:
        refresh_lock()
        _wall_lim = wall_cap if v4.get("wallclock_live") else ORDER_WALL_CLOCK_MAX_SEC
        _wall_floor = WALLCLOCK_MIN_STAGE_SEC if v4.get("wallclock_live") else 0
        if now_ts() - e2e_t0 > _wall_lim - _wall_floor:
            final_status = "needs_human"
            final_summary = f"整单耗时逼近/超过 {_wall_lim}s 墙钟硬上限（剩余不足 {_wall_floor}s 不再开新阶段），已自动暂停。转驻厂专家。由驻厂外聘专家诊断后重派 (needs_human)"
            v4_trace("wallclock_stop", order_id, attempt=attempt, stage="builder", elapsed_sec=round(now_ts() - e2e_t0, 2), cap_sec=_wall_lim)
            append_v3_event(order_id, attempt, "needs_human", "超时熔断", final_summary)
            break
        if attempt == 1 and reused_existing_draft:
            print(f"[factory_runner] {order_id} attempt 1/{loop_max}: reusing modified sandbox deliverable {wt_target_abs}, running Gate 1 + Gate 2 directly...", flush=True)
            w_res = {
                "role": "worker",
                "shift": "reuse",
                "model_order": [],
                "exit_code": 0,
                "timed_out": False,
                "duration_sec": 0.0,
                "start_ms": now_ms(),
                "end_ms": now_ms(),
                "gateway_delta": {"requests": 0, "ok": 0, "fail": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_cny": 0.0, "lanes": {}},
                "sessions": [],
                "task_title": f"【V4-复用既有沙箱草稿】{order_id}",
                "stdout": "Reused existing modified sandbox draft in worktree.",
            }
            fuse_reason = ""
        else:
            print(f"[factory_runner] {order_id} attempt {attempt}/{max(loop_max, attempt)} starting builder in worktree {wt_path}...", flush=True)
            brief_path = os.path.join(ev_dir, f"work_brief_attempt{attempt}.md")
            rework_block = f"\n\n## 上一轮机检/UI审美复核退回缺陷（本轮必须逐项消灭，可在当前沙箱文件基础上直接增量修改修复，不必整文件从零重写）\n{prev_feedback}" if prev_feedback else ""
            _sync_clause = _sync_clause_of(prev_feedback)
            delivery_doc = os.path.join(ev_dir, "FACTORY_W_DELIVERY.md")

            anti_hang_note = (
                "## ⛔ 执行纪律与防超时硬规（必读）\n"
                "1. **绝对禁止在 Bash 中启动任何常驻 HTTP 服务或阻塞进程**（如 `python srv.py` / `http.server` / `board_server.py` / `sleep`），否则会阻塞工具调用直达超时强杀！\n"
                "2. 如需静态检查语法，仅允许执行 1 秒内退出的纯内存检查（例如把 `<script>` 提取用 `node --check` 校验 JS 无语法错误，或用 `git diff --stat` 看改动）。\n"
                "3. 真机渲染、三视口布局校验、截图与审美审查均由工厂下一道自动流水线（Gate 1 CDP 探针 + Gate 2 专属 UI 审美质检专员）自动接管完成。\n"
                "4. 改完沙箱目标文件并把交件回执写入 `FACTORY_W_DELIVERY.md` 后，**立即结束本轮并简要总结**！\n"
            )
            aesthetic_note = ""
            if norm_kind == "ui" and v4.get("uispec_prompt"):
                _rules, _rules_path = load_uispec_rules()
                aesthetic_note = build_aesthetic_note(_rules, _rules_path)
                v4_trace("aesthetic_note", order_id, attempt=attempt, rules_path=_rules_path,
                         uispec_version=_rules.get("uispec_version", ""), note=aesthetic_note)
            elif norm_kind == "ui":
                aesthetic_note = (
                    "\n## 🎨 UI 审美质检前置红线（写完即过 Gate 1 + 专属 UI 审美质检官五维审验）\n"
                    "1. **字号与比例协调**：正文 13–14px，辅助说明 ≥12px（严禁 <11px），KPI 核心数字 24–30px，拒绝超大突兀文字或文字挤压。\n"
                    "2. **色彩严格克制 + 高对比度**：遵循 `全厂-UISPEC设计系统规范.md`（浅色清爽画布、全站仅 **1 个主强调色 + 红/黄/绿语义状态色**，拒绝五颜六色杂乱彩虹色；全部文本对比度 ≥ 4.5:1）。\n"
                    "3. **首屏清爽三区 + 呼吸感**：`1440×900`/`1200×800` 默认展开只留「Zone 1 告警条 + Zone 2 KPI 卡 + Zone 3 图形化漏斗柱」，Zone 4–12 全部包进默认关闭的 `<details>`（不加 `open`），每卡正文 ≤ 3 行，首屏可见总字数 ≤ 2,500 字（上限 4,500）。\n"
                    "4. **排版对齐 + 移动端适配**：短单号 `white-space: nowrap` 严禁折行；席位表同值通道列合并分组；`390×844` 手机端 `scrollWidth <= clientWidth`。\n"
                    "5. **零技术黑话 + 顶部 24h 打回按钮**：默认可见区 0 裸路径、0 SQL、0 下划线数据库表/字段名（对 `/data.json` 告警文做前端清洗脱敏）；顶部提供紧凑「24h 一键打回重做」按钮调用 `/api/v3/reject`。\n"
                )

            evidence_note = ""
            if norm_kind in ("code", "cad"):
                evidence_note = (
                    "\n## 🧾 交件回执证据硬规（Gate 2 按此判定，缺任一条按 P0 打回）\n"
                    "1. 规格书验收节的**每条命令**，回执里都要贴出：命令全文 + **原始 stdout 末段（至少 5 行）** + **退出码**（如 `echo EXIT=$?` 的实际数值）。\n"
                    "2. 若某条验收命令无法执行，必须写明「为什么不能执行」+ 一条等效可复跑的替代证明（命令 + 输出），不得省略不写。\n"
                    "3. 禁止只写「已完成/已通过/测试全绿」而无原始输出；禁止把多轮输出混成一句结论。\n"
                    "4. 若规格书自身互相矛盾（例如红线写「只改单文件」但验收要求新增测试文件），按红线执行，并在回执首段明确列出冲突条款、你的取舍与依据，不许静默取舍。\n"
                )
            # V5.0：专家段放在文件地图之前——专家可能已经在沙箱里动了目标文件，地图要按动完后的样子画。
            # 专家只在本单沙箱的声明目标里动手、活仓只读（旧实现让专家在活仓根目录干活，G52R3 的专家
            # 21:44 把没测过的内核改动直接写进了活仓）。出场两种：规格点名（开工先手）/ 厂内解决不了（救场）。
            expert_note = ""
            _expert_path = os.path.join(ev_dir, "expert_notes.md")
            try:
                import expert_seat as _es_stage
                _wants_expert = bool(_es_stage.wants_expert(spec_text)) and bool(_es_stage.pool_enabled())
            except Exception:  # noqa: BLE001
                _wants_expert = False
            try:
                if _rescue_pending:
                    _exp = _run_expert_stage(
                        order_id, attempt, spec_text=spec_text, wt_path=wt_path, wt_branch=wt_branch,
                        targets=list(targets) + [_p for _p in _last_regress_files if _p not in targets],
                        ev_dir=ev_dir, log_path=os.path.join(RUN_LOG_DIR, f"{run_id}_a{attempt}_expert.log"),
                        repos=[repo_root], dispatch=_dispatch_with_lane_fallback, context=_rescue_pending,
                        mode="rescue", title=str(job.get("title") or ""))
                    _rescue_pending = ""
                    expert_note = chr(10) + _expert_brief_block(_exp, rescue=True)
                    v4_trace("expert_rescue_done", order_id, attempt=attempt, ran=_exp.get("ran"),
                             notes_source=_exp.get("notes_source"), changed=_exp.get("changed"),
                             overreach_hit=bool(_exp.get("overreach_hit")))
                elif attempt == 1 and _wants_expert:
                    _exp = _run_expert_stage(
                        order_id, attempt, spec_text=spec_text, wt_path=wt_path, wt_branch=wt_branch, targets=targets,
                        ev_dir=ev_dir, log_path=os.path.join(RUN_LOG_DIR, f"{run_id}_a{attempt}_expert.log"),
                        repos=[repo_root], dispatch=_dispatch_with_lane_fallback, context="", mode="opening",
                        title=str(job.get("title") or ""))
                    expert_note = chr(10) + _expert_brief_block(_exp, rescue=False)
                elif attempt > 1 and os.path.isfile(_expert_path):
                    _saved = open(_expert_path, encoding="utf-8", errors="replace").read().strip()
                    if _saved:
                        expert_note = (chr(10) + "## 驻厂外聘专家的结论（前一轮已给出：继续照「交给施工」做，不要回退专家的改动）"
                                       + chr(10) + _saved + chr(10))
            except Exception as _expert_exc:  # noqa: BLE001
                expert_note = ""
                _rescue_pending = ""
                append_v3_event(order_id, attempt, "expert_skip", "驻厂外聘专家", f"专家段没跑完，施工照常进行：{_expert_exc}")
            map_path = os.path.join(ev_dir, f"map_attempt{attempt}.md")
            map_md = generate_file_map_md(wt_target_abs, map_path)
            kb_note = _kb_inject(str(job.get("title") or ""), spec_text)
            print(
                f"[factory_runner] KB-INJECT order={order_id} "
                f"lessons={_KB_INJECT_LAST.get('lessons', 0)} kb={_KB_INJECT_LAST.get('kb', 0)}",
                flush=True,
            )
            append_v3_event(order_id, attempt, "builder_start", "Builder施工", f"启动第 {attempt}/{max(loop_max, attempt)} 轮沙箱施工（已生成文件地图 map.md）")

            work_brief = (
                f"# {KERNEL_VERSION} 沙箱派工单 · {order_id}（第 {attempt}/{max(loop_max, attempt)} 轮）\n\n"
                f"- 隔离沙箱目录：`{wt_path}`（分支 `{wt_branch}`）\n"
                f"- **允许修改的文件范围 (`touch_scope`)**：仅限下列 {len(targets)} 个文件（严禁越界修改任何其他文件{_sync_clause}）：\n"
                + "".join(f"  - `{os.path.join(wt_path, t)}`\n" for t in targets)
                + f"- **目标文件行号地图 (`map.md`)**：`{map_path}`（请先按下方地图行号区间定向读取，用一次性集中编辑修改，避免全文通读耗时！）\n"
                f"- 交件回执写入（绝对路径）：`{delivery_doc}`\n"
                f"- **严格隔离红线**：严禁直接修改主仓 `{repo_root}` 下的任何文件！严禁触碰 `index.html`、六页静态正本、`Private1/`、`board_token.txt` 或重启 8900。\n\n"
                f"{map_md}\n"
                f"{BUILD_METHOD_NOTE}\n"
                f"{anti_hang_note}{aesthetic_note}{evidence_note}{expert_note}{seed_note}\n{assertions_brief_md(spec_asserts)}\n"
                f"{REWRITE_NOTE_BUILDER if rw_state == 'ok' else ''}"
                f"## 规格正本\n{spec_text}{rework_block}\n"
            )
            atomic_write_text(brief_path, work_brief)

            w_prompt = (
                f"你是 {KERNEL_VERSION} 施工生产同事（Builder）。你的隔离 Git Worktree 沙箱目录为 `{wt_path}`。"
                + (f"\n\n{kb_note}\n" if kb_note else "")
                + f"请用 Read 读取 `{brief_path}`（内含文件行号地图 `map.md` 与当前有效条款），按行号范围定向读取并只用绝对路径修改下列目标文件（仅此清单）："
                + "、".join(f"`{os.path.join(wt_path, t)}`" for t in targets)
                + ("；上一轮机检点名的回归用例文件按派工单「回归用例同步」一条处理" if _sync_clause else "")
                + f"，完成后将交件回执写入 `{delivery_doc}`。"
                f"严禁修改 `{repo_root}` 主仓文件、index.html、六页正本、Private1 或 board_token.txt；严禁在 Bash 中启动 HTTP 服务或常驻进程，写完静态检查通过后立即收尾交件。"
            )
            w_log = os.path.join(RUN_LOG_DIR, f"{run_id}_a{attempt}_worker.log")
            w_title = f"【V4-生产施工】{order_id} {str(job.get('title') or '')[:22]} (R{attempt})"
            w_res, fuse_reason = _dispatch_with_lane_fallback(
                attempt_no=attempt,
                prompt=w_prompt,
                cwd=wt_path,
                timeout_sec=BUILDER_TIMEOUT_SEC,
                role="worker",
                log_path=w_log,
                task_title=w_title,
            )
            tr_dir = os.path.join(TRANSCRIPTS_ROOT_DIR, order_id)
            os.makedirs(tr_dir, exist_ok=True)
            atomic_write_text(os.path.join(tr_dir, f"round_{attempt}_builder.md"), w_res.get("stdout") or "")
            append_v3_event(order_id, attempt, "builder_done", "Builder施工", f"第 {attempt} 轮施工结束 (通道={w_res.get('lane_selected','gateway')}, 耗时 {w_res['duration_sec']}s, rc={w_res['exit_code']})", files_changed=targets, cost_cny=w_res["gateway_delta"].get("cost_cny", 0.0))
        print(
            f"[factory_runner] worker done rc={w_res['exit_code']} dur={w_res['duration_sec']}s "
            f"gw_req={w_res['gateway_delta'].get('requests')} card_cost=¥{card_cost_cny:.4f}",
            flush=True,
        )
        if fuse_reason:
            fuse_tripped = True
            final_summary = fuse_reason
            attempts_log.append({"attempt": attempt, "worker": {k: v for k, v in w_res.items() if k != "stdout"}, "probe": {"all_passed": False}, "verifier": {"duration_sec": 0.0, "gateway_delta": {}}, "verifier_verdict": {"verdict": "FAIL", "defects": [fuse_reason]}})
            break

        # V4.0-P1 合入前闸（修 D-03）：Builder 未正常结束 → 本轮作废，不进 Gate1/Gate2、不合入
        _incomplete = builder_incomplete(w_res) if v4.get("merge_gate") else ""
        if _incomplete:
            prev_feedback = (
                f"- [V4 合入前闸] 上一轮 Builder 未正常结束（{_incomplete}，耗时 {w_res.get('duration_sec')}s），"
                "该轮改动不予验收合入。本轮请在当前沙箱文件基础上继续完成剩余改动，控制改动范围，按时收尾交件。"
            )
            append_v3_event(order_id, attempt, "builder_incomplete", "合入前闸", f"Builder 未正常结束（{_incomplete}），本轮不验收不合入")
            v4_trace("merge_gate_block", order_id, attempt=attempt, reason=_incomplete, worker=_res_digest(w_res))
            attempts_log.append({
                "attempt": attempt,
                "worker": {k: v for k, v in w_res.items() if k != "stdout"},
                "probe": {"all_passed": False, "skipped": True},
                "verifier": {"duration_sec": 0.0, "skipped": True, "gateway_delta": {"requests": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_cny": 0.0}, "sessions": []},
                "verifier_verdict": {"verdict": "SKIPPED_BUILDER_INCOMPLETE", "defects": [f"Builder 未正常结束: {_incomplete}"]},
            })
            final_summary = f"Builder 未正常结束（{_incomplete}），V4 合入前闸拦截"
            continue

        # Gate 1 Mechanical Probe
        handoff_t0 = now_ts()
        probe_out_dir = os.path.join(ev_dir, f"runner_a{attempt}")
        os.makedirs(probe_out_dir, exist_ok=True)
        probe_res = {}
        # V5.0-real.5：本单要过真引擎的看板页面——ui 单就是目标页；code 单取目标里第一个沙箱内存在的 dashboard/*.html
        ui_probe_rel = target_rel if norm_kind == "ui" else next(
            (t for t in targets if _norm_repo_path(t).startswith("dashboard/") and t.lower().endswith(".html")
             and os.path.isfile(os.path.join(wt_path, t.replace("/", os.sep)))), "")
        for _g1_try in range(MAX_GATE1_INFRA_RETRIES + 1):
            refresh_lock()
            if norm_kind == "ui":
                probe_res = probe_ui_dashboard(
                    wt_repo=wt_path,
                    main_repo=repo_root,
                    target_rel=target_rel,
                    out_dir=probe_out_dir,
                    six_baseline=six_baseline,
                    old_sha=main_file_sha_before,
                    main_file_sha_before=main_file_sha_before,
                    assertions=spec_asserts or None,
                )
            elif norm_kind == "cad":
                probe_res = probe_cad_project(wt_repo=wt_path, job=job, out_dir=probe_out_dir)
            else:
                probe_res = probe_code_delivery(wt_repo=wt_path, main_repo=repo_root, targets=targets,
                                                out_dir=probe_out_dir, builder_rc=w_res["exit_code"])
            if not probe_res.get("env_fail"):
                break
            if _g1_try < MAX_GATE1_INFRA_RETRIES:
                append_v3_event(order_id, attempt, "gate1_infra_retry", "Gate1基础设施重试", f"Gate1 Edge CDP 启动异常，5s 后第 {_g1_try + 1}/{MAX_GATE1_INFRA_RETRIES} 次重试（不消耗 Builder 轮次）")
                time.sleep(5)
        # V5.0-real.8：ui 单也做生产页基线——生产页本来就有的通检毛病不算本单的
        _hard_pre = [f for f in _rt_hard_failures(probe_res) if not f.startswith("[断言 FAIL")] if norm_kind == "ui" else []
        if _hard_pre and not probe_res.get("env_fail"):
            probe_res = _ui_gate1_with_baseline(
                probe_res, repo_root=repo_root, page=target_rel, out_dir=probe_out_dir + "_base",
                six_baseline=six_baseline, main_sha_before=main_file_sha_before,
            )
            _bl = (probe_res.get("checks") or {}).get("gate1_baseline") or {}
            _excluded = _bl.get("excluded") or []
            append_v3_event(order_id, attempt, "gate1_baseline", "Gate1生产页基线",
                            ("生产页基线探针环境故障，既有毛病按本单处理" if _bl.get("ran") is False else
                             f"生产页本来就有的通检毛病 {len(_excluded)} 条不计本单"),
                            files_changed=targets)
        refresh_lock()

        # V5.0-real.5：code 单动了看板页面、考卷又带断言 → 机检过了再上真引擎取证、逐条判断言
        _code_base = probe_res
        if (norm_kind == "code" and spec_asserts and ui_probe_rel and probe_res.get("all_passed")
                and not probe_res.get("env_fail")):
            probe_res = _merge_code_runtime(probe_res, _code_runtime_evidence(
                wt_repo=wt_path, repo_root=repo_root, page=ui_probe_rel, out_dir=probe_out_dir + "_ui",
                six_baseline=six_baseline, main_sha_before=main_shas_before.get(ui_probe_rel, ""), assertions=spec_asserts))
            _rt = (probe_res.get("checks") or {}).get("runtime_evidence") or {}
            _as_rt = _rt.get("assert_summary") or {}
            append_v3_event(order_id, attempt, "gate1_runtime", "Gate1真引擎取证",
                            ("真引擎取证环境故障，未能取证（不判 FAIL，如实交质检）" if _rt.get("env_fail") else
                             "看板真引擎取证 %s（页面 %s，断言 通过 %s / 失败 %s / 未验证 %s%s）"
                             % ("PASS" if probe_res.get("all_passed") else "FAIL", ui_probe_rel,
                                _as_rt.get("pass", 0), _as_rt.get("fail", 0), _as_rt.get("unverified", 0),
                                ("；生产页本来就有的通检毛病 %d 条不计本单" % len(_rt.get("preexisting_failures") or []))
                                if _rt.get("preexisting_failures") else "")))
            # V5.0-real.9：code 单的生产页基线结论也单独留一条事件（与 ui 单同名同文案）
            _pre_n = len(_rt.get("preexisting_failures") or [])
            if _rt.get("baseline_note"):
                append_v3_event(order_id, attempt, "gate1_baseline", "Gate1生产页基线", "生产页基线探针环境故障，既有毛病按本单处理",
                                files_changed=targets)
            elif _pre_n:
                append_v3_event(order_id, attempt, "gate1_baseline", "Gate1生产页基线",
                                f"生产页本来就有的通检毛病 {_pre_n} 条不计本单", files_changed=targets)
        refresh_lock()

        probe_dur = round(now_ts() - handoff_t0, 2)
        v4_trace("gate1_result", order_id, attempt=attempt, kind=norm_kind, duration_sec=probe_dur,
                 all_passed=probe_res.get("all_passed"), env_fail=probe_res.get("env_fail", False),
                 failures=probe_res.get("failures"), checks=probe_res.get("checks"),
                 screenshots=probe_res.get("screenshots"), telemetry_path=probe_res.get("telemetry_path"),
                 dom_dump_path=probe_res.get("dom_dump_path"), sha16=probe_res.get("sha16"), bytes=probe_res.get("bytes"),
                 assertions=probe_res.get("assertions"), assert_summary=probe_res.get("assert_summary"),
                 assert_shots=probe_res.get("assert_shots"))
        append_v3_event(
            order_id,
            attempt,
            "gate1_probe",
            "Gate1几何机检",
            f"Gate1 机检 {'ENV_FAIL' if probe_res.get('env_fail') else ('PASS' if probe_res.get('all_passed') else 'FAIL')} (耗时 {probe_dur}s, 失败项 {len(probe_res.get('failures') or [])})",
            files_changed=targets,
        )
        print(
            f"[factory_runner] Gate1 probe done all_passed={probe_res.get('all_passed')} env_fail={probe_res.get('env_fail', False)} "
            f"failures={len(probe_res.get('failures') or [])} dur={probe_dur}s",
            flush=True,
        )

        if probe_res.get("env_fail"):
            final_status = "infra_error"
            final_summary = ("内核回归闸环境故障（未能完成判定），非施工缺陷" if probe_res.get("env_fail_reason") == "kernel_regression"
                             else "Gate1 Edge CDP 探针环境故障（已重试2次），非工件缺陷")
            append_v3_event(order_id, attempt, "infra_error", "Gate1环境故障", final_summary)
            attempts_log.append({
                "attempt": attempt,
                "worker": {k: v for k, v in w_res.items() if k != "stdout"},
                "probe_dur_sec": probe_dur,
                "probe": probe_res,
                "handoff_plus_probe_sec": probe_dur,
                "verifier": {"duration_sec": 0.0, "skipped": True, "gateway_delta": {"requests": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_cny": 0.0}, "sessions": []},
                "verifier_verdict": {"verdict": "INFRA_ERROR", "defects": probe_res.get("failures") or [final_summary]},
            })
            break

        # V4.0-P2：必验断言未能验证 → 自动补证 1 次（不耗 Builder 轮次）；仍未验证 → 证据不足转人工，不合入
        if spec_asserts and probe_res.get("all_passed") and (probe_res.get("assert_summary") or {}).get("required_unverified"):
            _uv = (probe_res.get("assert_summary") or {}).get("required_unverified") or []
            append_v3_event(order_id, attempt, "assertion_reprobe", "断言补证", f"必验断言 {_uv} 未能验证，自动补证 1 次（不耗 Builder 轮次）")
            v4_trace("assertion_reprobe", order_id, attempt=attempt, unverified=_uv,
                     lines=assertion_result_lines(probe_res.get("assertions") or [], only=("UNVERIFIED",)))
            refresh_lock()
            _rp_t0 = now_ts()
            _rp_dir = probe_out_dir + "_reprobe"
            os.makedirs(_rp_dir, exist_ok=True)
            _rp = probe_ui_dashboard(wt_repo=wt_path, main_repo=repo_root, target_rel=ui_probe_rel, out_dir=_rp_dir,
                                     six_baseline=six_baseline, old_sha=main_file_sha_before if norm_kind == "ui" else "",
                                     main_file_sha_before=main_shas_before.get(ui_probe_rel, main_file_sha_before), assertions=spec_asserts)
            probe_dur = round(probe_dur + (now_ts() - _rp_t0), 2)
            if not _rp.get("env_fail"):
                # V5.0-real.5：code 单只换真引擎那部分，机检结论留着
                probe_res = _rp if norm_kind == "ui" else _merge_code_runtime(_code_base, _rp)
            _uv2 = (probe_res.get("assert_summary") or {}).get("required_unverified") or []
            if probe_res.get("all_passed") and _uv2:
                final_status = "needs_human"
                job["needs_reason"] = "needs_evidence"
                _why = assertion_result_lines(probe_res.get("assertions") or [], only=("UNVERIFIED",))
                final_summary = (f"[证据不足] 必验断言 {_uv2} 两次真机探测均未能验证，不合入；"
                                 f"请核对断言的选择器/状态或页面结构。{'；'.join(_why[:3])}")
                append_v3_event(order_id, attempt, "needs_human", "证据不足", final_summary[:400])
                v4_trace("needs_evidence", order_id, attempt=attempt, unverified=_uv2, lines=_why)
                attempts_log.append({
                    "attempt": attempt,
                    "worker": {k: v for k, v in w_res.items() if k != "stdout"},
                    "probe_dur_sec": probe_dur,
                    "probe": probe_res,
                    "verifier": {"duration_sec": 0.0, "skipped": True, "gateway_delta": {"requests": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_cny": 0.0}, "sessions": []},
                    "verifier_verdict": {"verdict": "NEEDS_EVIDENCE", "defects": _why},
                })
                break

        # Gate 1 Short-Circuit: Do NOT waste Verifier tokens if mechanical gate failed!
        if not probe_res.get("all_passed"):
            print(f"[factory_runner] Gate1 FAILED -> short-circuiting Verifier (0 verifier tokens wasted)!", flush=True)
            fb_lines = [f"- [Gate1 机检 FAIL] {fm}" for fm in (probe_res.get("failures") or [])]
            fb_lines.append("- [口径说明] Gate1 量的是沙箱相对主干的累计改动（已提交 + 暂存 + 未提交 + 未跟踪都算，"
                            "自分叉点以来）。目标文件没有任何改动才会判零改动；提交过也算，不必为了过检去撤销提交。")
            _rg_lines, _last_regress_files = _regress_rework_lines(probe_res)     # 硬闸拦下：断点明细 + 用例同步规矩
            fb_lines += _rg_lines
            fb_lines += [f"- {ln}" for ln in assertion_result_lines(probe_res.get("assertions") or [], only=("UNVERIFIED",))]
            prev_feedback = "\n".join(fb_lines)
            attempts_log.append({
                "attempt": attempt,
                "worker": {k: v for k, v in w_res.items() if k != "stdout"},
                "probe_dur_sec": probe_dur,
                "probe": probe_res,
                "handoff_plus_probe_sec": probe_dur,
                "verifier": {"duration_sec": 0.0, "skipped": True, "gateway_delta": {"requests": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_cny": 0.0}, "sessions": []},
                "verifier_verdict": {"verdict": "SKIPPED_GATE1_FAIL", "score": 0, "defects": probe_res.get("failures") or []},
            })
            if attempt >= _rounds.stop and _last_regress_files:
                _call_expert_rescue("常规轮次用尽仍没过内核回归（%s）" % "、".join(
                    ((probe_res.get("strict_checks") or {}).get("kernel_tests") or {}).get("new_failed") or []),
                    {"confirmed_p0": 1, "confirmed_p1": 0, "defects": list(probe_res.get("failures") or [])})
            continue

        if v4.get("wallclock_live") and wallclock_remaining(e2e_t0, wall_cap) < WALLCLOCK_MIN_STAGE_SEC:
            final_status = "needs_human"
            final_summary = f"Gate1 已过但整单墙钟剩余不足 {WALLCLOCK_MIN_STAGE_SEC}s（上限 {wall_cap}s），不再启动 Gate2。转驻厂专家。由驻厂外聘专家诊断后重派 (needs_human)；沙箱已保留"
            append_v3_event(order_id, attempt, "needs_human", "超时熔断", final_summary)
            v4_trace("wallclock_stop", order_id, attempt=attempt, stage="verifier", elapsed_sec=round(now_ts() - e2e_t0, 2), cap_sec=wall_cap)
            attempts_log.append({
                "attempt": attempt,
                "worker": {k: v for k, v in w_res.items() if k != "stdout"},
                "probe_dur_sec": probe_dur,
                "probe": probe_res,
                "verifier": {"duration_sec": 0.0, "skipped": True, "gateway_delta": {"requests": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_cny": 0.0}, "sessions": []},
                "verifier_verdict": {"verdict": "SKIPPED_WALLCLOCK", "defects": [final_summary]},
            })
            break

        # Gate 2: Independent Verifier with order-type-specific Verify Profile template (no new permanent seats)
        v_brief_path = os.path.join(ev_dir, f"verify_brief_attempt{attempt}.md")
        shots_list = [s["file"] for s in (probe_res.get("screenshots") or []) if s.get("ok")]
        probe_summary_obj = {k: v for k, v in probe_res.items() if k not in ("visible_text_preview", "cdp_telemetry_summary")}
        profile_ref = v_profile_path or f"verify_profiles/{v_profile_key}.md"
        profile_block = (
            f"## 专属岗位审查考卷模板（引用 `{profile_ref}`）\n{v_profile_text}\n\n"
            if v_profile_text else ""
        )

        # Include full 3-viewport CDP telemetry + static style/zone digest directly in brief so Verifier doesn't hang grepping/attaching
        extra_telemetry = ""
        tel_p = probe_res.get("telemetry_path") or ""
        if tel_p and os.path.exists(tel_p):
            try:
                td = json.load(open(tel_p, "r", encoding="utf-8"))
                vp_digest = {
                    vp: {k: v for k, v in m.items() if k != "visibleText"}
                    for vp, m in (td.get("viewports") or {}).items()
                }
                # 2026-11-23 supervisor fix: 遥测 JSON 无上限直塞简报, 面板增长后撞 Windows 命令行 32K 上限(winerror 206)
                _tel_s = json.dumps({'jsErrors': td.get('jsErrors', []), 'viewports': vp_digest}, ensure_ascii=False, indent=2)
                if len(_tel_s) > 6000:
                    _tel_s = _tel_s[:6000] + "\n... [遥测明细超长, 已截断 %d 字符; 以 Gate1 JSON 摘要与断言结果为准]" % (len(_tel_s) - 6000)
                extra_telemetry += f"### 三视口 CDP 视觉遥测明细\n```json\n{_tel_s}\n```\n\n"
            except Exception:
                pass
        if norm_kind == "ui" and os.path.exists(wt_target_abs):
            try:
                html_src = open(wt_target_abs, "r", encoding="utf-8", errors="replace").read()
                css_m = re.findall(r"<style[^>]*>(.*?)</style>", html_src, flags=re.S | re.I)
                css_head = ("\n".join(css_m))[-1800:]
                zone_sums = re.findall(r"<summary[^>]*>(.*?)</summary>", html_src, flags=re.S | re.I)
                dom_p = probe_res.get("dom_dump_path") or ""
                dom_body_excerpt = ""
                if dom_p and os.path.exists(dom_p):
                    d_raw = open(dom_p, "r", encoding="utf-8", errors="replace").read()
                    d_clean = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", d_raw, flags=re.S | re.I)
                    d_clean = re.sub(r"\s{2,}", " ", d_clean).strip()
                    dom_body_excerpt = f"- 真机渲染后 DOM 结构快照 (去 style/script, 前 2400 字):\n```html\n{d_clean[:2400]}\n```\n"
                extra_telemetry += (
                    f"### 源码样式与真机渲染 DOM 摘要（`{len(html_src)} B`）\n"
                    f"- 折叠栏 `<summary>` ({len(zone_sums)} 个): {[re.sub(r'<[^>]+>', '', z).strip()[:40] for z in zone_sums[:14]]}\n"
                    f"{dom_body_excerpt}"
                    f"- CSS 尾部关键片段:\n```css\n{css_head}\n```\n\n"
                )
            except Exception:
                pass
        tel_block = extra_telemetry
        # 2026-11-25 V5.0 真化（A3）：证据口径换成「自分叉点以来的累计改动」全量落盘 + 索引 + 按文件内嵌开头，
        # 施工回执 FACTORY_W_DELIVERY.md 同时交到质检手上。旧口径（未提交 diff 的前 8000 字符）有三宗冤：
        # 工人一 commit 质检就什么都看不见；多文件单只看得到第一个文件；截断不吭声。
        # 所有单型同一口径：UI 单的 DOM/遥测摘要回答「长什么样」，diff 回答「改了什么」。
        _ev: dict = {}
        try:
            _ev = write_diff_evidence(wt_path, probe_res.get("checks") or {}, ev_dir, attempt, main_repo=repo_root)
            diff_evidence_text = _ev["embedded"]
        except Exception as _ev_exc:
            diff_evidence_text = (
                "\n### 沙箱累计改动证据\n- 证据落盘失败（%s）。请直接用 Grep/Read 核对沙箱文件，"
                "或按 Gate1 汇总的 files_changed 清单逐个确认；**不得因为这里没有 diff 就判未实现**。\n" % _ev_exc
            )
        try:
            _w_start_ms = int(w_res.get("start_ms") or 0) if isinstance(w_res, dict) else 0
            receipt_text = embed_delivery_receipt(ev_dir, attempt, _w_start_ms)
        except Exception as _rc_exc:
            receipt_text = "\n### 施工交件回执\n- 回执读取失败（%s），不据此判定。\n" % _rc_exc

        speed_rule = (
            "## 质检纪律（先看证据，再下结论）\n"
            "- **只允许调用 Read 和 Grep 两个只读工具**，用来核对下面内嵌没覆盖到的证据；"
            "**严禁 Bash、Edit、Write、MultiEdit、TodoWrite、Task、Glob**，严禁改动任何文件、执行任何命令。\n"
            "- 内嵌的 diff / 遥测 / 回执都只是**开头**，完整内容已落盘（路径见第 3 项）。**没内嵌 ≠ 没做**："
            "要判「未实现 / 没落地」，必须先用 Grep 或 Read 在证据文件或沙箱里核对过，"
            "并在 evidence 里写出核对位置（`文件路径:行号`，或 `证据文件 + 搜索词`）且引用原文。\n"
            "- 反过来，**没看证据也不许放行**：每一条 dismissed 的依据同样要写核对位置。\n"
            "- 三端实拍 PNG 可以用 Read 直接查看（核对视觉项时看图，不要凭遥测数字想象画面）。\n"
            "- 工具核对做完之后，**最终回复的第一行必须直接是唯一一段完整标准 JSON 对象**（前面不要标题、寒暄或分析），"
            "之后可以附简短说明；不要只调用工具而不给最终 JSON。\n"
            "- 结论由内核按 `confirmed` 的 P0/P1 数量机械裁决 PASS/FAIL（无需自评总分）；有 confirmed P0/P1 时，"
            "`rework_prompt` 必须是完整可执行的《P0/P1/P2 修改提示词清单》。\n"
            "- 考卷模板里如有「禁止调用工具 / 只凭内嵌证据直出」的旧话，以本节为准。\n\n"
        )

        tests_sync_note = ""
        _ts = (probe_res.get("checks") or {}).get("tests_synced") or []
        if _ts:
            tests_sync_note = (
                "### 本轮同步了内核回归用例（请专门核对）\n"
                + "".join("- `%s`（+%s / −%s）\n" % (t.get("path"), t.get("added"), t.get("deleted")) for t in _ts[:6])
                + "核对点：用例改动是不是只因本单改变了被测口径（规格里哪一条要求改这个口径）；"
                  "有没有顺手放宽、跳过与本单无关的断言。没有依据地放宽断言 → P0；有依据且只动相关断言 → 通过。\n\n"
            )
        if "kernel_tests" in (probe_res.get("strict_checks") or {}) and any(
                str(f.get("path") or "").replace("\\", "/").lower() == "scripts/run_tests.py"
                for f in (probe_res.get("checks") or {}).get("files_changed") or []):
            tests_sync_note += (
                "### 本轮改了回归套件的跑手 `scripts/run_tests.py`（请专门核对）\n"
                "登记新用例属正常；删掉、注释掉、改宽已有用例的登记 = 拆硬闸 → P0（规格明确要求的除外）。\n\n"
            )
        # 本单的验收脚本本轮真被改了（test_cmd 点名的文件出现在本轮实际改动里）：它的通过不算独立验收，事前就交质检专门核对
        import admit_v5 as _admit_v5      # execute_job 里 admit_v5 是收尾才导入的局部名，这里另起一个名字，别踩 UnboundLocalError
        _own = _admit_v5.scripts_in_scope(job.get("test_cmd"), [f.get("path") for f in (probe_res.get("checks") or {}).get("files_changed") or []])
        if _own:
            tests_sync_note += (
                "### 本单的验收脚本本轮被改了（请专门核对）\n"
                + "".join("- `%s`\n" % p for p in _own[:6])
                + "核对点：验收脚本是不是只因本单改变了被测口径而改（规格里哪一条要求改它）；有没有放宽、跳过断言。"
                  "它的通过不算独立验收，要另找没被本轮改过的证据；没有依据地放宽断言 → P0。\n\n"
            )

        if norm_kind == "ui":
            v_title = f"【V4-{v_role_label}】{order_id} 视觉审美审计 (QC#{attempt})"
        else:
            v_title = f"【V4-{v_role_label}】{order_id} 专属质检 (QC#{attempt})"

        # 桌面通道的 prompt 没有长度上限，发全文；命令行回落通道单条 prompt 上限 24000 字符（run_headless_zcode）。
        # 回落通道发同一份考卷的缩减版：每段只留开头并写明缩了多少、全文在哪——不再悄悄掐掉中间。
        _VBRIEF_CLI_CAP = 20000
        _VBRIEF_LEVELS = (
            {"spec": 16000, "profile": 10 ** 9, "summary": 10 ** 9, "tel": 10 ** 9, "diff": 10 ** 9,
             "receipt": 10 ** 9, "assert": 10 ** 9, "visible": 10 ** 9},
            {"spec": 5000, "profile": 5000, "summary": 3500, "tel": 2000, "diff": 2500, "receipt": 2000,
             "assert": 4000, "visible": 500},
            {"spec": 2500, "profile": 4000, "summary": 1500, "tel": 600, "diff": 900, "receipt": 600,
             "assert": 1500, "visible": 0},
        )
        sandbox_targets = [t for t in (targets if isinstance(targets, (list, tuple)) else []) if t]
        if not sandbox_targets and target_rel:
            sandbox_targets = [target_rel]
        targets_md = "、".join(f"`{os.path.join(wt_path, t)}`" for t in sandbox_targets) or f"`{wt_target_abs}`"
        evidence_paths = [
            f"本考卷全文（所有内嵌证据的完整版）：`{v_brief_path}`",
            f"订单规格全文：`{job.get('spec_file')}`",
            f"施工回执：`{os.path.join(ev_dir, 'FACTORY_W_DELIVERY.md')}`",
        ]
        if _ev.get("index_path"):
            evidence_paths.append(f"累计 diff 索引：`{_ev['index_path']}`（按文件列出它在第几段分段文件里）")
        if probe_res.get("telemetry_path"):
            evidence_paths.append(f"三视口遥测全文：`{probe_res['telemetry_path']}`")
        if probe_res.get("dom_dump_path"):
            evidence_paths.append(f"渲染后 DOM 快照：`{probe_res['dom_dump_path']}`")

        def _cap(text: str, limit: int, what: str) -> str:
            text = str(text or "")
            if len(text) <= limit:
                return text
            return (text[:limit]
                    + f"\n… [【{what}】已缩减：此处只留前 {limit} / 共 {len(text)} 字符，完整内容在本考卷全文 `{v_brief_path}` 里] …\n")

        def _build_v_brief(level: int) -> str:
            c = _VBRIEF_LEVELS[level]
            spec_part = spec_text[:c["spec"]]
            if len(spec_text) > c["spec"]:
                spec_part += f"\n\n[… 规格共 {len(spec_text)} 字符，此处只嵌前 {c['spec']} 字符；全文：`{job.get('spec_file')}` …]"
            head_note = ""
            if level > 0:
                head_note = (
                    f"> **本考卷是缩减版（级别 {level}）**：标注「已缩减」的段落只留了开头，完整内容在本考卷全文 `{v_brief_path}`。"
                    "下结论之前，先按需 Read 它和第 3 项列出的证据文件。\n\n"
                )
            return (
                f"# {v_title} · 第 {attempt} 轮\n\n"
                + f"- 订单类型 (`kind`)：`{kind}` → 装配考卷：`{profile_ref}`（{v_role_label}）\n\n"
                + head_note
                + speed_rule
                + _cap(profile_block, c["profile"], "考卷模板")
                + "## 订单规格与验收要求\n"
                + spec_part + "\n\n"
                + "## 本轮实测证据与附卷材料\n"
                + "1. Gate 1 机械/CDP 探针汇总：\n```json\n"
                + _cap(json.dumps(probe_summary_obj, ensure_ascii=False, indent=2), c["summary"], "Gate1 探针汇总")
                + "\n```\n\n"
                + _cap(tests_sync_note, 1400, "回归用例同步")
                + _cap(tel_block, c["tel"], "遥测 / DOM 摘要")
                + _cap(diff_evidence_text, c["diff"], "沙箱改动证据")
                + _cap(receipt_text, c["receipt"], "施工回执")
                + (f"### 规格断言机检结果（Gate1 已在真机三视口/指定状态执行；表内量化项以机检为准，**你不再判定**，只审主观项 Q1/Q2/Q3/Q5/Q7）\n"
                   f"{_cap(_assert_table_for_brief(probe_res), c['assert'], '断言机检结果')}\n" if spec_asserts else "")
                + (closeout_brief_md(prior_items) if (v4.get("closeout_gate") and prior_items) else "")
                + (REWRITE_NOTE_VERIFIER if rw_state == "ok" else "")
                + (f"2. 首屏默认可见文本预览：\n```text\n"
                   f"{_cap(probe_res.get('visible_text_preview', ''), c['visible'], '首屏文本预览')}\n```\n\n"
                   if probe_res.get("visible_text_preview") else "")
                + "3. 沙箱与证据文件（全部只读，只许 Read / Grep）：\n"
                + f"   - 沙箱目标文件（共 {len(sandbox_targets)} 个）：{targets_md}\n"
                + "".join(f"   - {p}\n" for p in evidence_paths)
                + f"4. 三端实拍 PNG 存证路径：{shots_list}\n"
                + f"5. 请在**最终回复第一行**直接输出唯一一段完整标准 JSON 对象：`{{\"user_and_core_task\": \"一句话使用者与核心任务\", \"findings\": [{{\"id\": \"F1\", \"question\": \"Q1_信息去重\", \"severity\": \"P0\", \"status\": \"confirmed\", \"evidence\": [\"引用遥测或DOM证据\"], \"what\": \"具体缺陷\", \"fix\": \"精确到选择器/文案的修改指令\"}}], \"rework_prompt\": \"若有confirmed缺陷时的完整《P0/P1/P2修改提示词清单》，无缺陷时为''\", \"summary\": \"一句话结论\"}}`（若无 confirmed 缺陷则 `findings` 为 `[]`）。\n"
            )

        v_brief = _build_v_brief(0)
        atomic_write_text(v_brief_path, v_brief)
        v_prompt = v_brief
        _v_level = 0
        while len(v_prompt) > _VBRIEF_CLI_CAP and _v_level < len(_VBRIEF_LEVELS) - 1:
            _v_level += 1
            v_prompt = _build_v_brief(_v_level)
        if v_prompt is not v_brief:
            atomic_write_text(os.path.join(ev_dir, f"verify_brief_attempt{attempt}.sent.md"), v_prompt)
            print(f"[factory_runner] verifier brief {len(v_brief)} chars; CLI fallback copy = compact level {_v_level} "
                  f"({len(v_prompt)} chars); full brief on disk", flush=True)

        v_log = os.path.join(RUN_LOG_DIR, f"{run_id}_a{attempt}_verifier.log")
        v_t0 = now_ts()
        handoff_latency = round(v_t0 - handoff_t0, 2)
        append_v3_event(order_id, attempt, "verifier_start", "StageA/B质检", f"启动第 {attempt} 轮 Stage A 挑刺 + Stage B 验尸审计")
        v_res, fuse_reason = _dispatch_with_lane_fallback(
            attempt_no=attempt,
            prompt=v_prompt,
            cwd=wt_path,
            timeout_sec=VERIFIER_TIMEOUT_SEC,
            role="verifier",
            log_path=v_log,
            task_title=v_title,
            prompt_desktop=v_brief,
            # V5.0：质检可以 Read/Grep 核对证据（沙箱文件、分段 diff、回执、实拍 PNG）；其余工具一律禁用
            disallowed_tools="Bash Edit Write MultiEdit TodoWrite Task Glob",
            prefer_desktop=True,
        )
        tr_dir = os.path.join(TRANSCRIPTS_ROOT_DIR, order_id)
        os.makedirs(tr_dir, exist_ok=True)
        atomic_write_text(os.path.join(tr_dir, f"round_{attempt}_verifier.md"), v_res.get("stdout") or "")
        verifier_parsed = _parse_verifier_json(v_res.get("stdout") or "")
        if v_res.get("lane_selected") == "weekend-glm":
            if verifier_parsed.get("verdict") == "INFRA_ERROR":
                ledger = _record_weekend_glm_health(ok=False, reason="verifier_soft_degrade_infra_error", severe=False)
                active_lane = None
                append_v3_event(order_id, attempt, "lane_fallback", "通道回落", "weekend-glm 质检软降级（输出空/非规范JSON），切回网关池完成重试与挽救")
            else:
                ledger = _record_weekend_glm_health(ok=True)

        _v_empty_tries = 0
        while verifier_parsed.get("verdict") == "INFRA_ERROR" and verifier_parsed.get("empty_output") and _v_empty_tries < MAX_VERIFIER_INFRA_RETRIES and not fuse_reason:
            _v_empty_tries += 1
            refresh_lock()
            append_v3_event(order_id, attempt, "gate2_infra_retry", "Gate2空响应重试", f"Verifier 返回空响应，第 {_v_empty_tries}/{MAX_VERIFIER_INFRA_RETRIES} 次重试")
            v_retry_log = os.path.join(RUN_LOG_DIR, f"{run_id}_a{attempt}_verifier_r{_v_empty_tries}.log")
            v_retry_res, r_fuse = _dispatch_with_lane_fallback(
                attempt_no=attempt,
                prompt=v_prompt,
                cwd=wt_path,
                timeout_sec=VERIFIER_TIMEOUT_SEC,
                role="verifier",
                log_path=v_retry_log,
                task_title=f"{v_title} (Retry {_v_empty_tries})",
                prompt_desktop=v_brief,
                disallowed_tools="Bash Edit Write MultiEdit TodoWrite Task Glob",
                prefer_desktop=True,
            )
            if r_fuse:
                fuse_reason = r_fuse
            v_retry_res["duration_sec"] = round(float(v_res.get("duration_sec", 0.0)) + float(v_retry_res.get("duration_sec", 0.0)), 2)
            v_retry_res["gateway_delta"] = _merge_gw_delta(v_res.get("gateway_delta") or {}, v_retry_res.get("gateway_delta") or {})
            v_retry_res["sessions"] = list(v_res.get("sessions") or []) + list(v_retry_res.get("sessions") or [])
            v_res = v_retry_res
            atomic_write_text(os.path.join(tr_dir, f"round_{attempt}_verifier_r{_v_empty_tries}.md"), v_res.get("stdout") or "")
            verifier_parsed = _parse_verifier_json(v_res.get("stdout") or "")

        if verifier_parsed.get("verdict") == "INFRA_ERROR" and not verifier_parsed.get("empty_output") and len((v_res.get("stdout") or "").strip()) >= 200 and not fuse_reason:
            refresh_lock()
            append_v3_event(order_id, attempt, "gate2_json_rescue", "Gate2格式挽救", "Verifier 输出非规范 JSON，启动轻量 1-turn JSON 提取挽救")
            rescue_prompt = (
                "请把下方审查正文严格整理为唯一标准 JSON 对象，回复第一行必须直接输出 JSON（严禁任何前导文字或调用工具），格式：\n"
                '{"user_and_core_task": "...", "findings": [{"id": "F1", "question": "Q1", "severity": "P0|P1|P2", "status": "confirmed|dismissed", "evidence": ["..."], "what": "...", "fix": "..."}], "closeout": [{"tag": "R1-F1", "status": "closed|still_open|regressed", "evidence": "..."}], "rework_prompt": "...", "summary": "..."}\n\n'
                "## 审查原文\n" + (v_res.get("stdout") or "")[-6000:]
            )
            v_rescue_log = os.path.join(RUN_LOG_DIR, f"{run_id}_a{attempt}_verifier_rescue.log")
            r_res, r_fuse = _dispatch_with_lane_fallback(
                attempt_no=attempt,
                prompt=rescue_prompt,
                cwd=wt_path,
                timeout_sec=180,
                role="verifier",
                log_path=v_rescue_log,
                task_title=f"{v_title} (JSON Rescue)",
                disallowed_tools="Bash Edit Write MultiEdit TodoWrite Task Read Glob Grep",
                prefer_desktop=True,
            )
            if r_fuse:
                fuse_reason = r_fuse
            v_res["duration_sec"] = round(float(v_res.get("duration_sec", 0.0)) + float(r_res.get("duration_sec", 0.0)), 2)
            v_res["gateway_delta"] = _merge_gw_delta(v_res.get("gateway_delta") or {}, r_res.get("gateway_delta") or {})
            v_res["sessions"] = list(v_res.get("sessions") or []) + list(r_res.get("sessions") or [])
            rescued = _parse_verifier_json(r_res.get("stdout") or "")
            if rescued.get("verdict") != "INFRA_ERROR":
                verifier_parsed = rescued
                verifier_parsed["rescued_json"] = True

        if v4.get("closeout_gate") and prior_items and verifier_parsed.get("verdict") in ("PASS", "FAIL"):
            _co = apply_closeout(verifier_parsed, prior_items)
            v4_trace("closeout", order_id, attempt=attempt, prior=prior_items, rows=_co["rows"],
                     extra_p0=_co["extra_p0"], extra_p1=_co["extra_p1"], verdict_after=verifier_parsed.get("verdict"),
                     verdict_before=verifier_parsed.get("verdict_before_closeout", verifier_parsed.get("verdict")))
            if _co["extra_p0"] or _co["extra_p1"]:
                append_v3_event(order_id, attempt, "closeout", "销账核对",
                                f"上一轮缺陷未关 P0×{_co['extra_p0']} P1×{_co['extra_p1']}，判决 {verifier_parsed.get('verdict')}")
        v4_trace("gate2_verdict", order_id, attempt=attempt, verdict=verifier_parsed.get("verdict"),
                 confirmed_p0=verifier_parsed.get("confirmed_p0"), confirmed_p1=verifier_parsed.get("confirmed_p1"),
                 findings=verifier_parsed.get("findings") or verifier_parsed.get("stageA"), defects=verifier_parsed.get("defects"),
                 rework_prompt=verifier_parsed.get("rework_prompt"), summary=verifier_parsed.get("summary"),
                 rescued_json=verifier_parsed.get("rescued_json", False), truncated_recovered=verifier_parsed.get("truncated_recovered", False),
                 verifier=_res_digest(v_res), brief_chars=len(v_brief), brief_level=_v_level,
                 prompt_chars=len(v_prompt), evidence_files=len(_ev.get("files") or []),
                 evidence_chars=int(_ev.get("total_chars") or 0))
        append_v3_event(
            order_id,
            attempt,
            "gate2_verdict",
            "内核机械裁决",
            f"内核裁决 {verifier_parsed.get('verdict')} (confirmed P0={verifier_parsed.get('confirmed_p0', 0)}, P1={verifier_parsed.get('confirmed_p1', 0)}, 通道={v_res.get('lane_selected','gateway')})",
            cost_cny=v_res["gateway_delta"].get("cost_cny", 0.0),
        )
        print(
            f"[factory_runner] verifier done rc={v_res['exit_code']} verdict={verifier_parsed.get('verdict')} "
            f"score={verifier_parsed.get('score')} dur={v_res['duration_sec']}s card_cost=¥{card_cost_cny:.4f}",
            flush=True,
        )

        attempts_log.append({
            "attempt": attempt,
            "worker": {k: v for k, v in w_res.items() if k != "stdout"},
            "probe_dur_sec": probe_dur,
            "probe": probe_res,
            "handoff_plus_probe_sec": handoff_latency,
            "verifier": {k: v for k, v in v_res.items() if k != "stdout"},
            "verifier_verdict": verifier_parsed,
        })

        if fuse_reason:
            fuse_tripped = True
            final_summary = fuse_reason
            break

        if verifier_parsed.get("verdict") == "INFRA_ERROR":
            final_status = "infra_error"
            final_summary = f"Gate2 独立复核输出解析异常（{(verifier_parsed.get('defects') or [''])[0]}，已重试/挽救），非工件 P0 缺陷"
            append_v3_event(order_id, attempt, "infra_error", "复核格式/基础设施异常", final_summary)
            break

        if verifier_parsed.get("verdict") == "PASS":
            refresh_lock()
            m_t0 = now_ts()
            ok_merge, merge_result = _commit_and_merge_worktree(order_id, repo_root, wt_path, targets, main_shas_before=main_shas_before, kind=kind)
            merge_dur_sec = round(now_ts() - m_t0, 2)
            v4_trace("merge", order_id, attempt=attempt, ok=ok_merge, duration_sec=merge_dur_sec, result=merge_result)
            if ok_merge:
                final_status = "done"
                final_summary = verifier_parsed.get("summary") or "双门全绿并已合回主干"
                append_v3_event(order_id, attempt, "merged", "内核合入主干", f"双门全绿合入主干 ({'、'.join(targets)}, 耗时 {merge_dur_sec}s)", files_changed=targets)
                note_merge_not_live(order_id, attempt, repo_root, targets)  # V5.0-real.6：后端合入 ≠ 上线，只留痕不重启
                if v4.get("assertion_gate") and norm_kind == "ui":
                    # V4.0-P2：只把「断言 PASS」写进基线；其余契约条款标 done_unasserted（修 D-02）
                    try:
                        _bl = write_verified_baseline(order_id, probe_res.get("assertions") or [], spec_asserts)
                        v4_trace("baseline_write", order_id, **_bl)
                        append_v3_event(order_id, attempt, "baseline_write", "基线写入",
                                        f"已验证断言写入基线 +{_bl['baseline_added']}（累计 {_bl['baseline_total']}）；契约 done {_bl['contract'].get('done', 0)} / 未断言 {_bl['contract'].get('done_unasserted', 0)}")
                    except Exception as _bl_exc:
                        v4_trace("baseline_write_error", order_id, error=repr(_bl_exc))
                break
            else:
                # V5.0：合入失败先分清是谁的问题。旧实现一律回炉，工人拿着「合入失败」什么也做不了，
                # 而内核其实已经把他上一轮的活提交了——下一轮 Gate1 量不到改动，必死（2026-11-25 G44R3）。
                _inner = merge_result.get("merge") if isinstance(merge_result.get("merge"), dict) else {}
                _mreason = str(merge_result.get("reason") or _inner.get("reason") or "")
                _hint = str(_inner.get("hint") or merge_result.get("hint") or "")
                _conflicts = [str(x) for x in (_inner.get("conflicts") or merge_result.get("files") or [])][:20]
                _rounds_left = attempt < _rounds.stop
                append_v3_event(order_id, attempt, "merge_failed", "内核合入主干",
                                ("合入未成功（%s）：%s" % (_mreason or "未归类", _hint or merge_result.get("err") or ""))[:300])
                if _mreason in ("main-target-sha-unchanged", "already-up-to-date"):
                    final_status = "done"
                    final_summary = (verifier_parsed.get("summary") or "双门全绿") + "；主干已含相同内容，无新增量"
                    break
                if _mreason in MERGE_INFRA_REASONS:
                    final_status = "needs_human"
                    job["needs_reason"] = "merge_infra"
                    final_summary = (
                        f"[合入受阻·非施工缺陷] 独立复核已 PASS，但合入被工厂侧问题挡住（{_mreason}）："
                        f"{_hint or merge_result.get('err') or ''} 成果已完整保留在沙箱分支 {wt_branch}，"
                        f"排除后直接补合入即可，不必重派施工 (needs_human)")
                    break
                if _mreason == "merge-conflict":
                    if not _rounds_left and not _conflict_extra_used:
                        # V5.0：合入冲突不该因为「轮次用尽」直接判死——独立复核已经通过，只差把冲突解开。多给一轮专门解冲突。
                        _conflict_extra_used = True
                        _rounds.extend(1)
                        _rounds_left = True
                        append_v3_event(order_id, attempt, "merge_conflict_extra_round", "内核合入主干",
                                        "轮次本已用尽，为解决合入冲突追加一轮（质检已通过，不必重派）")
                    if _rounds_left:
                        prev_feedback = (
                            "- [合入冲突·不是质量缺陷] 独立复核已通过，但沙箱分支与主干在同一处都有改动，合不进去。\n"
                            "- 本轮不要重做功能：在沙箱内执行 `git merge main`，逐个解决下列文件的冲突并提交，"
                            "保证解决后功能不变；取舍写进回执。\n- 冲突文件：" + "、".join(_conflicts or ["（见 git status）"]))
                        final_summary = prev_feedback
                        continue
                    final_status = "needs_human"
                    job["needs_reason"] = "merge_conflict"
                    final_summary = (
                        f"[合入冲突] 独立复核已 PASS，但沙箱分支与主干冲突（{'、'.join(_conflicts[:6])}），轮次已用尽。"
                        f"成果保留在沙箱分支 {wt_branch}，在沙箱内 git merge main 解决后即可补合入 (needs_human)")
                    break
                if _mreason == "unexpected-files":
                    if not _rounds_left:
                        final_status = "needs_human"
                        job["needs_reason"] = "unexpected_files"
                        final_summary = "[目标外依赖] 目标文件引用到了声明范围之外的新文件：" + "、".join(_conflicts[:8]) + " (needs_human)"
                        break
                    prev_feedback = (
                        "- [目标外依赖] 目标文件引用到了声明范围之外的新文件：" + "、".join(_conflicts or [])
                        + "。请把依赖并回目标文件（内联），或在回执首段说明为何必须扩大声明范围。")
                    final_summary = prev_feedback
                    continue
                prev_feedback = f"- [Worktree 合入失败] {_hint or json.dumps(merge_result, ensure_ascii=False)[:1500]}"
                final_summary = prev_feedback
                continue

        cur_p0 = int(verifier_parsed.get("confirmed_p0", 1))
        _stuck = prev_p0_count is not None and cur_p0 >= prev_p0_count and cur_p0 > 0 and attempt >= 2
        if _stuck and not _call_expert_rescue(
                "连续两轮 confirmed P0 未下降 (上一轮 P0=%d -> 本轮 P0=%d)" % (prev_p0_count, cur_p0), verifier_parsed):
            final_status = "needs_human"
            final_summary = f"连续两轮 confirmed P0 未下降 (上一轮 P0={prev_p0_count} -> 本轮 P0={cur_p0})，触发防震荡止损。由驻厂外聘专家诊断后重派 (needs_human)"
            append_v3_event(order_id, attempt, "needs_human", "防震荡止损", final_summary)
            break
        prev_p0_count = cur_p0

        fb_lines = [f"- [Gate2 审美审计缺陷] {dm}" for dm in (verifier_parsed.get("defects") or [])]
        if verifier_parsed.get("rework_prompt"):
            fb_lines.append(f"\n### 📋 首席 UI 审美审计官开具的《P0/P1/P2 专业重构修改提示词》（本轮必须逐条执行）\n{verifier_parsed['rework_prompt']}\n")
        if verifier_parsed.get("summary"):
            fb_lines.append(f"- [审计总结] {verifier_parsed['summary']}")
        prev_feedback = "\n".join(fb_lines) or "- [返工原因] 独立复核未通过。"
        if v4.get("closeout_gate"):
            _carry = [it for it in prior_items if any(r.get("tag") == it["tag"] and r.get("status") != "closed" for r in (verifier_parsed.get("closeout_rows") or []))]
            prior_items = prior_confirmed_items(verifier_parsed, attempt) + _carry


        # V5.0：常规轮次用尽仍未过质检——这正是「厂内解决不了」的时刻。请驻厂外聘专家出手（每单一次）：
        # 下一轮专家先在本单沙箱的声明目标里动手，施工接着做。请不到就按原样收场。
        if attempt >= _rounds.stop and str(verifier_parsed.get("verdict") or "") == "FAIL":
            if _call_expert_rescue("常规轮次用尽仍未过质检 (P0=%s P1=%s)" % (verifier_parsed.get("confirmed_p0"), verifier_parsed.get("confirmed_p1")),
                                   verifier_parsed):
                # 这里不写 final_summary：救场后若仍没过，最终结论要如实取最后一轮的退回意见，不能留一句占位话
                v4_trace("expert_rescue_round", order_id, attempt=attempt, rounds_stop=_rounds.stop)

    e2e_total = round(now_ts() - e2e_t0, 2)
    run_end_ms = now_ms()
    gw_run_end = get_gateway_status()
    window_gw = gateway_delta(gw_run_start, gw_run_end)

    # Breakdown calculations
    builder_dur = round(sum(float(a["worker"].get("duration_sec", 0.0)) for a in attempts_log), 2)
    probe_dur_sum = round(sum(float(a.get("probe_dur_sec", 0.0)) for a in attempts_log), 2)
    verifier_dur = round(sum(float((a.get("verifier") or {}).get("duration_sec", 0.0)) for a in attempts_log), 2)

    builder_in = sum(int((a["worker"].get("gateway_delta") or {}).get("tokens_in", 0)) for a in attempts_log)
    builder_out = sum(int((a["worker"].get("gateway_delta") or {}).get("tokens_out", 0)) for a in attempts_log)
    builder_cached = sum(int((a["worker"].get("gateway_delta") or {}).get("tokens_cached", 0)) for a in attempts_log)
    builder_cost = round(sum(float((a["worker"].get("gateway_delta") or {}).get("cost_cny", 0.0)) for a in attempts_log), 6)

    verifier_in = sum(int(((a.get("verifier") or {}).get("gateway_delta") or {}).get("tokens_in", 0)) for a in attempts_log)
    verifier_out = sum(int(((a.get("verifier") or {}).get("gateway_delta") or {}).get("tokens_out", 0)) for a in attempts_log)
    verifier_cached = sum(int(((a.get("verifier") or {}).get("gateway_delta") or {}).get("tokens_cached", 0)) for a in attempts_log)
    verifier_cost = round(sum(float(((a.get("verifier") or {}).get("gateway_delta") or {}).get("cost_cny", 0.0)) for a in attempts_log), 6)

    def _sum_est(role_key: str, fld: str):
        return sum(((a.get(role_key) or {}).get("gateway_delta") or {}).get(fld, 0) or 0 for a in attempts_log)
    builder_est_cost = round(_sum_est("worker", "cost_est_cny"), 6)
    verifier_est_cost = round(_sum_est("verifier", "cost_est_cny"), 6)
    builder_est_tok = int(_sum_est("worker", "tokens_est_in") + _sum_est("worker", "tokens_est_out"))
    verifier_est_tok = int(_sum_est("verifier", "tokens_est_in") + _sum_est("verifier", "tokens_est_out"))
    window_book_cost = round(float(window_gw.get("cost_cny", 0.0) or 0.0) - float(window_gw.get("cost_est_cny", 0.0) or 0.0), 6)
    worker_tokens_io = builder_in + builder_out + verifier_in + verifier_out
    window_tokens_io = int(window_gw.get("tokens_in", 0)) + int(window_gw.get("tokens_out", 0))
    non_worker_tokens = max(0, window_tokens_io - worker_tokens_io)
    mgmt_tax_pct = round((non_worker_tokens / window_tokens_io) * 100.0, 2) if window_tokens_io > 0 else 0.0

    # Cross-check DB sessions spawned during window
    known_sids = set()
    for a in attempts_log:
        for rk in ("worker", "verifier"):
            for s in ((a.get(rk) or {}).get("sessions") or []):
                known_sids.add(s.get("session_id"))
    all_win_sids = detect_sessions_in_window(run_start_ms, run_end_ms)
    extra_non_v3_sids = [s for s in all_win_sids if s.get("session_id") not in known_sids]

    live_8900_verify = {}
    if final_status == "done" and norm_kind == "ui":
        tok = read_token_mem()
        page_nm = os.path.basename(target_rel)
        c403, _ = http_get_status(f"{BOARD_BASE_URL}/{page_nm}")
        c200, b200 = http_get_status(f"{BOARD_BASE_URL}/{page_nm}", {"X-Board-Token": tok} if tok else None)
        merged_disk_sha = sha256_file(main_target_abs) if os.path.exists(main_target_abs) else ""
        live_8900_sha = hashlib.sha256(b200).hexdigest() if b200 else ""
        live_8900_verify = {
            "unauth_code": c403,
            "auth_code": c200,
            "merged_disk_sha16": merged_disk_sha[:16],
            "live_8900_sha16": live_8900_sha[:16],
            "live_matches_merged": bool(merged_disk_sha and live_8900_sha == merged_disk_sha),
        }

    billing_breakdown = {
        "durations_sec": {
            "dispatch_latency": dispatch_latency,
            "builder": builder_dur,
            "gate1_probe": probe_dur_sum,
            "gate2_verifier": verifier_dur,
            "worktree_merge": merge_dur_sec,
            "e2e_total": e2e_total,
        },
        "tokens_and_cost": {
            "builder": {"in": builder_in, "out": builder_out, "cached": builder_cached, "cost_cny": builder_cost},
            "verifier": {"in": verifier_in, "out": verifier_out, "cached": verifier_cached, "cost_cny": verifier_cost},
            "gateway_window_total": window_gw,
            "worker_tokens_io": worker_tokens_io,
            "non_worker_tokens_io": non_worker_tokens,
            "management_tax_pct": mgmt_tax_pct,
            "card_total_cost_cny": round(card_cost_cny, 6),
            "cost_columns": {
                "cash_cny": round(card_cost_cny, 6),
                "book_cny": window_book_cost,
                "est_cny": round(float(window_gw.get("cost_est_cny", 0.0) or 0.0), 6),
                "builder_est_cny": builder_est_cost, "verifier_est_cny": verifier_est_cost,
                "builder_est_tokens": builder_est_tok, "verifier_est_tokens": verifier_est_tok,
                "usage_schema": 2 if "tokens_est_in" in window_gw else 1,
            },
            "daily_total_cost_cny": load_budget_ledger().get("daily_cost_cny", 0.0),
            "card_cap_cny": CARD_BUDGET_CAP_CNY,
            "daily_cap_cny": DAILY_BUDGET_CAP_CNY,
            "fuse_tripped": fuse_tripped,
        },
        "worktree": {
            "path": wt_path,
            "branch": wt_branch,
            "preserved_for_debug": (final_status != "done"),
            "merge_result": merge_result,
        },
        "extra_non_v3_sessions_in_window": extra_non_v3_sids,
    }

    daily_wk_tok = int(ledger.get("daily_weekend_glm_tokens") or 0)
    daily_gw_tok = int(ledger.get("daily_gateway_tokens") or 0)

    run_report = {
        "run_id": run_id,
        "kernel_version": KERNEL_VERSION,
        "kernel_build": KERNEL_BUILD,
        "order_id": order_id,
        "kind": kind,
        "verify_profile_used": f"{v_profile_key}.md ({v_role_label})",
        "title": job.get("title"),
        "target": main_target_abs,
        "final_status": final_status,
        "final_summary": final_summary or prev_feedback,
        "enqueued_at": job.get("enqueued_at"),
        "started_at": job.get("started_at"),
        "finished_at": iso_now(),
        "dispatch_latency_sec": dispatch_latency,
        "e2e_total_sec": e2e_total,
        "attempts_used": len(attempts_log),
        "human_approval_popups": 0,
        "lane_routing": {
            "initial_probe": wk_probe,
            "job_weekend_glm_tokens": job_wk_tokens,
            "job_gateway_tokens": job_gw_tokens,
            "daily_weekend_glm_tokens": daily_wk_tok,
            "daily_gateway_tokens": daily_gw_tok,
        },
        "billing_breakdown": billing_breakdown,
        "live_8900_verify": live_8900_verify,
        "assertions": {"source": assert_src, "count": len(spec_asserts),
                       "summary": ((attempts_log[-1].get("probe") or {}).get("assert_summary") if attempts_log else {}) or {},
                       "results": ((attempts_log[-1].get("probe") or {}).get("assertions") if attempts_log else []) or []},
        "needs_reason": job.get("needs_reason", ""),
        "gateway_totals": {
            "requests": int(window_gw.get("requests", 0)),
            "tokens_in": int(window_gw.get("tokens_in", 0)),
            "tokens_out": int(window_gw.get("tokens_out", 0)),
            "tokens_cached": int(window_gw.get("tokens_cached", 0)),
            "cost_cny": round(card_cost_cny, 6),
        },
        "attempts": attempts_log,
    }

    report_json_path = os.path.join(ev_dir, f"factory_run_{run_id}.json")
    report_md_path = os.path.join(ev_dir, f"factory_run_{run_id}.md")
    atomic_write_json(report_json_path, run_report)

    last_probe = (attempts_log[-1].get("probe") or {}) if attempts_log else {}
    md_lines = [
        f"# {KERNEL_VERSION} 工厂流水线运行账单 · {run_id}",
        "",
        f"- 订单：`{order_id}` (`{kind}`) — {job.get('title')}",
        f"- 专属质检模板：`{v_profile_key}.md`（{v_role_label}）",
        f"- 终态：**`{final_status}`**（{run_report['final_summary']}）",
        f"- 轮次：{len(attempts_log)} / {max_attempts} · Worktree：`{wt_path}`（{'已合入清理' if final_status=='done' else '已保留现场'}）",
        f"- 人工拍板弹窗：`0` 次 · 预算熔断：{'触发' if fuse_tripped else '未触发'}{'（观察期：只告警不停机）' if fuse_dry_run_active(v4) else ''}（单卡**现金** ¥{card_cost_cny:.4f}/¥{CARD_BUDGET_CAP_CNY:.2f} · 账面 ¥{window_book_cost:.4f} · 估算 ¥{float(window_gw.get('cost_est_cny', 0.0) or 0.0):.4f}；当日账面 ¥{billing_breakdown['tokens_and_cost']['daily_total_cost_cny']:.4f}，当日现金 ¥{float(load_budget_ledger().get('daily_cash_cny', 0.0)):.4f}/¥{DAILY_BUDGET_CAP_CNY:.2f}）",
        f"- 免费票(weekend-glm) vs 网关池用量：本单免费票 {job_wk_tokens:,} tok / 网关池 {job_gw_tokens:,} tok；今日累计 免费票 {daily_wk_tok:,} tok vs 网关池 {daily_gw_tok:,} tok",
        "",
        "## 端到端耗时分解",
        f"- 派单延迟：`{dispatch_latency} s`",
        f"- Builder 施工：`{builder_dur} s`",
        f"- Gate 1 机械探针：`{probe_dur_sum} s`",
        f"- Gate 2 独立 Verifier：`{verifier_dur} s`",
        f"- Worktree 合入清理：`{merge_dur_sec} s`",
        f"- **总端到端耗时**：**`{e2e_total} s`（{round(e2e_total/60.0, 2)} min）**",
        "",
        "## 真实成本与管理税核算",
        f"- Builder 实耗：`in={builder_in}, out={builder_out}, cached={builder_cached}, ¥{builder_cost:.6f}`（其中估算 {builder_est_tok} tok / ¥{builder_est_cost:.6f}）",
        f"- Verifier 实耗：`in={verifier_in}, out={verifier_out}, cached={verifier_cached}, ¥{verifier_cost:.6f}`（其中估算 {verifier_est_tok} tok / ¥{verifier_est_cost:.6f}）",
        f"- 窗口内网关总计：`in={window_gw.get('tokens_in',0)}, out={window_gw.get('tokens_out',0)}, ¥{window_gw.get('cost_cny',0.0):.6f}`",
        f"- **管理税占比**：**`{mgmt_tax_pct}%`**（非 Builder/Verifier 消耗 `{non_worker_tokens}` tokens）",
        f"- 终版产物：`{main_target_abs}` (`{last_probe.get('bytes', 0)} B`, `sha16={live_8900_verify.get('merged_disk_sha16') or last_probe.get('sha16', '')}`)",
    ]
    if spec_asserts:
        _as = (last_probe.get("assert_summary") or {})
        md_lines += ["", "## 规格断言机检（V4.0-P2）",
                     f"- 来源：{assert_src} · 共 {len(spec_asserts)} 条 · 通过 {_as.get('pass', 0)} / 失败 {_as.get('fail', 0)} / 未验证 {_as.get('unverified', 0)}", "",
                     assertion_table_md(last_probe.get("assertions") or [])]
    atomic_write_text(report_md_path, "\n".join(md_lines) + "\n")
    v4_trace("run_end", order_id, final_status=final_status, final_summary=run_report.get("final_summary"),
             needs_reason=job.get("needs_reason", ""), assertions_summary=run_report["assertions"]["summary"],
             attempts_used=len(attempts_log), e2e_total_sec=e2e_total, dispatch_latency_sec=dispatch_latency,
             billing=billing_breakdown, live_8900_verify=live_8900_verify, lane_routing=run_report.get("lane_routing"),
             report_json=report_json_path, report_md=report_md_path,
             attempt_verdicts=[(a.get("attempt"), (a.get("verifier_verdict") or {}).get("verdict")) for a in attempts_log])
    _V4_RUN_CTX.pop("run_id", None)
    _V4_RUN_CTX.pop("order_id", None)

    # V5.0：工厂自己的毛病（探针起不来 / 回归镜像没跑成 / 质检输出坏了）不该判死订单。
    # 旧做法直接终态 infra_error——泵不接、人工看不到，订单就这么无声无息地死了。
    # 现在：隔一会儿原单自动重跑（沙箱成果保留并复用）；重跑次数用完仍不行 → needs_human（泵和人工都看得见）。
    import admit_v5
    _infra_tries = int(job.get("infra_retries") or 0)
    if final_status == "infra_error" and not fuse_tripped and _infra_tries < MAX_JOB_INFRA_REQUEUES:
        job["infra_retries"] = _infra_tries + 1
        job["status"] = "queued"
        job["requeued_reason"] = "infra_error_requeue"
        job["requeued_at"] = iso_now()
        job["not_before_ts"] = now_ts() + JOB_INFRA_REQUEUE_DELAY_SEC
        job["infra_last_reason"] = admit_v5.one_line(final_summary or "")
        for _k in ("finished_at", "finished_ts", "e2e_total_sec", "disposition", "result_reason", "script_trust"):
            job.pop(_k, None)
        run_report["job_requeued"] = True
        run_report["infra_retry"] = _infra_tries + 1
        append_v3_event(order_id, 0, "infra_requeue", "工厂故障重排",
                        f"工厂侧故障（{job['infra_last_reason']}），{JOB_INFRA_REQUEUE_DELAY_SEC}s 后原单自动重跑"
                        f"（第 {_infra_tries + 1}/{MAX_JOB_INFRA_REQUEUES} 次），沙箱成果保留")
        _append_patrol(f"[V5_INFRA_REQUEUE {iso_now()}] {order_id}: {job['infra_last_reason']} "
                       f"(retry {_infra_tries + 1}/{MAX_JOB_INFRA_REQUEUES})")
        _lane_qcommit(job, q, fresh=True)     # G40b：收口落账（单道=原 reload 合并口径）
        return run_report
    if final_status == "infra_error":
        final_status = "needs_human"
        job["needs_reason"] = "infra_error"
        run_report["final_status"] = "needs_human"
        run_report["final_summary"] = f"[工厂侧故障·非施工缺陷] {run_report['final_summary']}（已自动重跑 {max(0, _infra_tries)} 次仍不行）"

    # V5.0：质检已 PASS、只是合入被工厂侧挡住（仓库锁没松 / 保护性提交没落下 / 索引被占）——成果是好的，不是施工的问题，
    # 不该一张 needs_human 就躺下等人。2026-11-25 下午 G44R3 / G47R2 就是这么「验过没进」的。
    # 现在：隔一会儿原单自动回来只补「双门 + 合入」（沙箱成果原样复用，不重派施工）；补满了仍不行才转 needs_human，
    # 此时 remerge 命令随时能再把它叫回来。
    _merge_tries = int(job.get("merge_requeues") or 0)
    if final_status == "needs_human" and job.get("needs_reason") == "merge_infra":
        if _merge_tries < MAX_MERGE_REQUEUES:
            job["merge_requeues"] = _merge_tries + 1
            job["status"] = "queued"
            job["resume_at"] = "gates"
            job["requeued_reason"] = "merge_infra_requeue"
            job["requeued_at"] = iso_now()
            job["not_before_ts"] = now_ts() + JOB_MERGE_REQUEUE_DELAY_SEC
            job["merge_last_reason"] = admit_v5.one_line(final_summary or "")
            for _k in ("finished_at", "finished_ts", "e2e_total_sec", "disposition", "result_reason", "script_trust", "needs_reason"):
                job.pop(_k, None)
            run_report["job_requeued"] = True
            run_report["merge_requeue"] = _merge_tries + 1
            append_v3_event(order_id, 0, "merge_requeue", "工厂合入重排",
                            f"质检已过、合入被工厂侧挡住（{job['merge_last_reason']}），{JOB_MERGE_REQUEUE_DELAY_SEC}s 后原单只补双门 + 合入"
                            f"（第 {_merge_tries + 1}/{MAX_MERGE_REQUEUES} 次），不重派施工")
            _append_patrol(f"[V5_MERGE_REQUEUE {iso_now()}] {order_id}: {job['merge_last_reason']} (requeue {_merge_tries + 1}/{MAX_MERGE_REQUEUES})")
            _lane_qcommit(job, q, fresh=True)
            return run_report
        run_report["final_summary"] = final_summary = (
            f"[合入受阻·已自动补合入 {_merge_tries} 次仍不行] {final_summary} 排除后运行 factory_runner.py remerge --order {order_id} 即可再补")

    job["status"] = final_status
    job["finished_at"] = run_report["finished_at"]
    job["finished_ts"] = now_ts()
    job["e2e_total_sec"] = e2e_total
    job["report_json"] = report_json_path
    _reason = admit_v5.one_line(run_report.get("final_summary") or "")
    _trust = admit_v5.script_in_scope(job.get("test_cmd"), admit_v5.job_paths(job))
    if _trust and _trust not in _reason:
        _reason = admit_v5.one_line((_reason + " " + _trust).strip())
        job["script_trust"] = _trust
    job["result_reason"] = _reason
    # 四去向要按整段结论判（「P1=2 / 部分实现 / 判返工」可能不在第一句里），旧实现只拿 80 字的首句去匹配
    job["disposition"] = admit_v5.disposition(final_status, run_report.get("final_summary") or "")
    try:
        _lesson_path = os.path.join(STEWARD_ROOT, "state", "lessons_auto.jsonl")
        os.makedirs(os.path.dirname(_lesson_path), exist_ok=True)
        with open(_lesson_path, "a", encoding="utf-8") as _lf:
            _lf.write(json.dumps({
                "ts": iso_now(),
                "order_id": order_id,
                "disposition": job.get("disposition") or "",
                "reason": job.get("result_reason") or "",
                "source": str(job.get("spec_file") or ""),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _lane_qcommit(job, q, fresh=True)     # G40b：收口落账（单道=原 reload 合并口径）

    _append_patrol(
        f"[V3_RUNNER {iso_now()}] {order_id} status={final_status} attempts={len(attempts_log)} "
        f"e2e={e2e_total}s mgmt_tax={mgmt_tax_pct}% cost=¥{round(card_cost_cny, 4)} merge={bool(merge_result.get('ok'))}"
    )
    return run_report


# ------------------------------------------------------------------ CLI


def cmd_smoke_test() -> int:
    ensure_dirs()
    res = run_headless_zcode(
        "只回复两个字：就绪。不要调用任何工具，不要解释。",
        cwd=MATE_ROOT,
        timeout_sec=120,
        role="verifier",
        log_path=os.path.join(RUN_LOG_DIR, f"smoke_{_dt.datetime.now().strftime('%H%M%S')}.log"),
    )
    out = {
        "exit_code": res["exit_code"],
        "timed_out": res["timed_out"],
        "duration_sec": res["duration_sec"],
        "shift": res["shift"],
        "model_order": res["model_order"],
        "gateway_delta": res["gateway_delta"],
        "sessions": res["sessions"],
        "stdout_preview": res["stdout"].strip()[:300],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if (res["exit_code"] == 0 and res["gateway_delta"].get("requests", 0) >= 1) else 2


def cmd_status() -> int:
    q = load_queue()
    print(json.dumps({"queue": q.get("jobs", []), "recent_history": (q.get("history") or [])[-5:], "budget_today": load_budget_ledger()}, ensure_ascii=False, indent=2))
    return 0


_REAP_STATUSES = ("cancelled", "done", "failed", "infra_error", "error", "skipped")


def reap_terminal_jobs(q: dict) -> list:
    """P4b：把 jobs 里已终态的单归档进 history（防幽灵单永久占位、永久冻结整个家族）。

    只归档明确终态；needs_human 留在 jobs（可能等人工签字后 promote，语义不动）。
    """
    jobs = q.get("jobs") or []
    known = {h.get("job_id") for h in (q.get("history") or [])}
    keep, moved = [], []
    for j in jobs:
        if str(j.get("status") or "") in _REAP_STATUSES:
            jj = dict(j)
            jj["archived_from_jobs"] = True
            jj["archived_at"] = iso_now()
            moved.append(jj)
        else:
            keep.append(j)
    if moved:
        q["jobs"] = keep
        q["history"] = (q.get("history") or []) + [m for m in moved if m.get("job_id") not in known]
    return moved


def cmd_tick(target_order_id: str = "") -> int:
    ensure_dirs()
    if os.path.exists(PAUSE_FLAG_PATH):
        print(json.dumps({"status": "PAUSED", "kernel_version": KERNEL_VERSION, "flag": PAUSE_FLAG_PATH}, ensure_ascii=False))
        return 0
    backfill_historical_v3_sessions()
    slots_mode = _slots_enabled()          # G40b：v4.slots 并行道（enabled!=false 且 max>1）
    if slots_mode:
        import slot_ledger
    if not slots_mode and not acquire_lock():
        print(json.dumps({"status": "BUSY"}))
        return 0
    _LANE["mode"] = slots_mode
    try:
        q = load_queue()
        live_orders = ({s.get("order_id") for s in (slot_ledger.slot_snapshot().get("slots") or []) if isinstance(s, dict)}
                       if slots_mode else None)
        with (slot_ledger.file_lock("factory_queue", 30) if slots_mode else contextlib.nullcontext()):
            changed = reconcile_queue_state(q, live_orders=live_orders)
            added = ingest_inbox(q)
            reaped = reap_terminal_jobs(q)     # P4b：终态单出 jobs 进 history，幽灵单不再冻家族
            if changed or added or reaped:
                atomic_write_json(QUEUE_PATH, q)
        if changed or added or reaped:
            v4_trace("queue_change", "", reconciled=changed, ingested=added,
                     reaped=[str(m.get("order_id") or "") for m in (reaped or [])],
                     jobs=[{k: j.get(k) for k in ("order_id", "status", "kind", "title", "enqueued_at", "requeued_reason", "requeued_at")} for j in q.get("jobs", [])])
        _now = now_ts()
        _delayed = [j for j in q.get("jobs", []) if j.get("status") == "queued" and float(j.get("not_before_ts") or 0) > _now]
        queued = [j for j in q.get("jobs", []) if j.get("status") == "queued" and float(j.get("not_before_ts") or 0) <= _now]
        if target_order_id:
            queued = [j for j in queued if j.get("order_id") == target_order_id] or queued
        if not queued:
            print(json.dumps({"status": "IDLE", "queued": 0, "delayed": len(_delayed)}))
            return 0
        job = queued[0]
        if not slots_mode:
            # V5.0：单道也过进场官——专家停单 / 前置未交付的单留在队列并写明原因，不再无脑取队首。
            import admit_v5
            _running = [j for j in q.get("jobs", []) if isinstance(j, dict) and j.get("status") == "running"]
            job, _extra, holds = admit_v5.plan_starts(queued, _running, set(), 1, _family_of, q.get("history") or [])
            _changed_hold = False
            for row in q.get("jobs", []):
                if not isinstance(row, dict):
                    continue
                _oid = str(row.get("order_id") or "")
                if _oid in holds:
                    if row.get("hold_reason") != holds[_oid]:
                        row["hold_reason"] = holds[_oid]
                        _changed_hold = True
                elif row.get("hold_reason"):
                    row.pop("hold_reason", None)
                    _changed_hold = True
            if _changed_hold:
                atomic_write_json(QUEUE_PATH, q)
            if job is None:
                print(json.dumps({"status": "HELD", "queued": len(queued), "holds": holds}, ensure_ascii=False))
                return 0
        if slots_mode:
            slot_ledger.slot_reap()        # G40b：派发前再回收死 pid/超时槽
            snap = slot_ledger.slot_snapshot()
            fam_busy = {s.get("family") for s in (snap.get("slots") or []) if isinstance(s, dict)}
            free_slots = max(0, int(snap.get("max") or 0) - len(snap.get("slots") or []))
            import admit_v5
            running_jobs = [j for j in q.get("jobs", []) if isinstance(j, dict) and j.get("status") == "running"]
            admit_slots = 1 if target_order_id else free_slots
            job, extras, holds = admit_v5.plan_starts(queued, running_jobs, fam_busy, admit_slots, _family_of, q.get("history") or [])
            started = {str(j.get("order_id") or "") for j in ([job] if job else []) + list(extras)}
            for _j in ([job] if job else []) + list(extras):
                _j.pop("hold_reason", None)
            if holds or started:
                with slot_ledger.file_lock("factory_queue", 30):
                    qc = load_queue()
                    changed_hold = False
                    for row in qc.get("jobs") or []:
                        if not isinstance(row, dict) or row.get("status") != "queued":
                            continue
                        oid = str(row.get("order_id") or "")
                        if oid in holds and row.get("hold_reason") != holds[oid]:
                            row["hold_reason"] = holds[oid]
                            changed_hold = True
                        elif oid in started and row.get("hold_reason"):
                            row.pop("hold_reason", None)
                            changed_hold = True
                    if changed_hold:
                        atomic_write_json(QUEUE_PATH, qc)
            if job and extras and not target_order_id:
                for extra in extras:
                    _spawn_lane_tick(str(extra.get("order_id") or ""))
            claim = (slot_ledger.slot_claim(str(job.get("order_id") or ""), _family_of(str(job.get("order_id") or ""))) if job
                     else {"ok": False, "reason": "slots_full_or_family_busy"})
            if not claim.get("ok"):              # 满槽/家族竞态：本轮退出（不报错、不丢单）
                print(json.dumps({"status": "BUSY", "reason": claim.get("reason")}))
                return 0
            _LANE["order"] = str(job.get("order_id") or "")
            with slot_ledger.file_lock("factory_queue", 30):   # claim 后复核仍是 queued（防竞态重复执行）
                row = next((j for j in (load_queue().get("jobs") or []) if j.get("job_id") == job.get("job_id")), None)
            if not (row and row.get("status") == "queued"):
                slot_ledger.slot_release(_LANE["order"])
                _LANE["order"] = ""
                print(json.dumps({"status": "BUSY", "reason": "order_state_changed"}))
                return 0
        try:
            rep = execute_job(q, job)
        except Exception as exc:
            import traceback as _tb
            v4_trace("kernel_crash", job.get("order_id", ""), error=repr(exc), traceback=_tb.format_exc())
            _append_patrol(f"[V4_KERNEL_CRASH {iso_now()}] {job.get('order_id', '')}: {exc!r}（详见 logs/v4_trace）")
            raise
        finally:
            if slots_mode:
                slot_ledger.slot_release(_LANE["order"])   # G40b：任何路径必释槽
                _LANE["order"] = ""
        print(json.dumps({k: rep[k] for k in ("run_id", "kernel_version", "kernel_build", "order_id", "final_status", "job_requeued", "e2e_total_sec", "dispatch_latency_sec", "attempts_used", "lane_routing", "gateway_totals", "final_summary", "billing_breakdown", "live_8900_verify") if k in rep}, ensure_ascii=False, indent=2))
        return 0 if rep["final_status"] == "done" else 1
    finally:
        if not slots_mode:
            release_lock()


def cmd_check_spec(spec_file: str) -> int:
    """V4.0-P2：离线校验规格书断言块（格式/模板/字段），不启动浏览器。退出码 0=合格 2=不合格。"""
    try:
        txt = open(spec_file, "r", encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(json.dumps({"ok": False, "errors": [f"读不了规格文件: {exc}"]}, ensure_ascii=False, indent=2))
        return 2
    a, e = extract_assertions(txt)
    ok = bool(a) and not e
    print(json.dumps({"ok": ok, "count": len(a), "errors": e if a or e else ["规格书缺 ```assertions``` 断言块"],
                      "assertions": a, "plan": build_assertion_plan(a) if a else {}}, ensure_ascii=False, indent=2))
    return 0 if ok else 2


def cmd_probe_spec(spec_file: str, target_rel: str = "dashboard/v2.html", repo: str = "") -> int:
    """V4.0-P2：在主仓当前生产版上真机试跑断言（派单前核对选择器/状态写得对不对）。不改任何文件、不进队列。
    注：此时「沙箱版 = 基线版 = 生产版」，baseline*k 类断言的结论只用于确认可测性，不代表本单改完后的判定。"""
    ensure_dirs()
    try:
        txt = open(spec_file, "r", encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"读不了规格文件: {exc}")
        return 2
    a, e = extract_assertions(txt)
    if e or not a:
        print(json.dumps({"ok": False, "errors": e or ["规格书缺 ```assertions``` 断言块"]}, ensure_ascii=False, indent=2))
        return 2
    repo_root = repo or STEWARD_ROOT
    out_dir = os.path.join(BP_ROOT, "reports", "spec_probe", _dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    tgt = os.path.join(repo_root, target_rel.replace("\\", "/"))
    sha_now = sha256_file(tgt) if os.path.exists(tgt) else ""
    res = probe_ui_dashboard(wt_repo=repo_root, main_repo=repo_root, target_rel=target_rel, out_dir=out_dir,
                             six_baseline=snapshot_six_pages(), old_sha="", main_file_sha_before=sha_now, assertions=a)
    rows = res.get("assertions") or []
    print(assertion_table_md(rows))
    for ln in assertion_result_lines(rows):
        print(ln)
    summ = res.get("assert_summary") or {}
    print(json.dumps({"summary": summ, "env_fail": res.get("env_fail", False), "out_dir": out_dir,
                      "shots": res.get("assert_shots")}, ensure_ascii=False, indent=2))
    v4_trace("spec_probe", "", spec_file=spec_file, summary=summ, results=rows, out_dir=out_dir)
    return 0 if (not res.get("env_fail") and not summ.get("unverified")) else 1


def freeze_all_assertions(main_target_path: str, max_pct: float = 0.2, threshold: int = 24) -> list[dict]:
    """V4.0-P3：为「视觉零变化」重构自动生成像素冻结断言：默认态 + 每个分区 + 降级态 × 三视口。"""
    try:
        txt = open(main_target_path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        txt = ""
    zones = sorted({int(z) for z in re.findall(r'<section[^>]*\bid="z(\d+)"', txt)} - {1})
    states = ["default"] + [f"zone:{z}" for z in zones] + ["degraded"]
    out = []
    for i, st in enumerate(states, 1):
        out.append({"id": f"V{i}", "clause": "视觉冻结", "desc": f"{st} 与生产版逐像素一致（≤{max_pct}%）",
                    "state": st, "metric": VISUAL_METRIC, "op": "<=", "value": max_pct, "threshold": threshold,
                    "mask": list(VISUAL_DEFAULT_MASK), "full_page": True, "required": st != "degraded",
                    "viewports": list(ASSERT_VIEWPORTS)})
    a, e = extract_assertions("```assertions\n" + json.dumps(out, ensure_ascii=False) + "\n```", {})
    if e:
        raise ValueError("; ".join(e))
    return a


def cmd_probe_candidate(candidate: str, spec_file: str = "", target_rel: str = "dashboard/v2.html", repo: str = "",
                        freeze_all: bool = False, max_pct: float = 0.2, apply: bool = False, message: str = "") -> int:
    """V4.0-P3：对一个现成的候选文件跑完整 Gate1（通用机检 + 规格断言 + 像素冻结 + 源码合规闸），不经 Builder。
    --apply：全部通过（且必验断言无未验证）才把候选写进主仓并 git commit。用于确定性重构（如 ui_lint slim 的产物）。
    自测噪声底：--candidate 直接指向生产版文件 + --freeze-all，看两次加载之间天然差多少。"""
    ensure_dirs()
    repo_root = repo or STEWARD_ROOT
    rel = target_rel.replace("\\", "/")
    main_t = os.path.join(repo_root, rel)
    if not os.path.exists(candidate):
        print(f"候选文件不存在: {candidate}")
        return 2
    assertions: list[dict] = []
    if spec_file:
        a, e = extract_assertions(open(spec_file, "r", encoding="utf-8", errors="replace").read())
        if e:
            print(json.dumps({"ok": False, "errors": e}, ensure_ascii=False, indent=2))
            return 2
        assertions += a
    if freeze_all:
        ids = {x["id"] for x in assertions}
        assertions += [x for x in freeze_all_assertions(main_t, max_pct) if x["id"] not in ids]
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    work = os.path.join(BP_ROOT, "reports", "candidate_probe", stamp)
    cand_repo = os.path.join(work, "repo")
    os.makedirs(os.path.dirname(os.path.join(cand_repo, rel)), exist_ok=True)
    shutil.copy(candidate, os.path.join(cand_repo, rel))
    main_sha = sha256_file(main_t) if os.path.exists(main_t) else ""
    noise_mode = sha256_file(candidate) == main_sha
    if noise_mode:
        print("[噪声底模式] 候选文件与生产版字节一致：本次结果 = 两次独立加载之间的天然像素差，用来确定遮罩与阈值；不会提交。")
        apply = False
    t0 = now_ts()
    res = probe_ui_dashboard(wt_repo=cand_repo, main_repo=repo_root, target_rel=rel, out_dir=os.path.join(work, "probe"),
                             six_baseline=snapshot_six_pages(), old_sha="" if noise_mode else main_sha, main_file_sha_before=main_sha,
                             assertions=assertions or None)
    summ = res.get("assert_summary") or {}
    rows = res.get("assertions") or []
    print(assertion_table_md(rows))
    for ln in res.get("failures") or []:
        print("FAIL ·", ln)
    for ln in assertion_result_lines(rows, only=("UNVERIFIED",)):
        print(ln)
    passed = bool(res.get("all_passed")) and not summ.get("required_unverified") and not res.get("env_fail")
    report = {"candidate": candidate, "candidate_sha16": sha256_file(candidate)[:16], "main_sha16": main_sha[:16], "noise_mode": noise_mode,
              "visual_max_pct": max([v.get("measured") or 0 for r_ in rows if r_.get("metric") == VISUAL_METRIC for v in r_["per_viewport"]
                                     if isinstance(v.get("measured"), (int, float))] or [0]),
              "passed": passed, "summary": summ, "failures": res.get("failures"), "ui_lint": (res.get("checks") or {}).get("ui_lint"),
              "duration_sec": round(now_ts() - t0, 1), "work_dir": work, "applied": False}
    if apply and passed:
        if (sha256_file(main_t) if os.path.exists(main_t) else "") != main_sha:
            report["apply_error"] = "比对期间主仓目标文件被改动，拒绝提交"
        else:
            shutil.copy(candidate, main_t)
            msg = message or f"v4-p3(candidate {stamp}): verified by probe-candidate (pixel-freeze + lint gate)"
            r1 = subprocess.run(["git", "-C", repo_root, "add", rel], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            r2 = subprocess.run(["git", "-C", repo_root, "commit", "-m", msg, "--", rel], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            report["applied"] = r2.returncode == 0
            report["git"] = (r1.stdout + r1.stderr + r2.stdout + r2.stderr)[-600:]
            report["new_main_sha16"] = sha256_file(main_t)[:16]
            append_v3_event("P3-CANDIDATE", 0, "merged", "候选直提", f"probe-candidate 全绿后提交 {rel}（{report['candidate_sha16']}）", files_changed=[rel])
    elif apply:
        report["apply_error"] = "未全部通过，不提交"
    atomic_write_json(os.path.join(work, "candidate_report.json"), report)
    v4_trace("candidate_probe", "", **{k: v for k, v in report.items()}, results=rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed and (not apply or report["applied"]) else 1


def _extra_meta(seed_from: str, depends_on: str, seed_gates: bool = False) -> dict | None:
    """enqueue 命令行上的附加字段（带种 / 前置）→ 订单上的 seed_from / seed_gates / depends_on。"""
    meta = {}
    if seed_from:
        meta["seed_from"] = seed_from
    if seed_from and seed_gates:
        meta["seed_gates"] = True
    deps = [x.strip() for x in depends_on.replace(";", ",").split(",") if x.strip()]
    if deps:
        meta["depends_on"] = deps
    return meta or None


def cmd_remerge(order_id: str, reason: str = "") -> int:
    """V5.0：把一张「质检已过、只是没合进去」的单重新排进队列——只补「双门 + 合入」，沙箱成果原样复用，不重派施工。
    沙箱分支必须还在且领先主干；这张单若还在队列里则不重复排。"""
    ensure_dirs()
    q = load_queue()
    jobs = [j for j in (q.get("jobs") or []) if isinstance(j, dict)]
    hist = [h for h in (q.get("history") or []) if isinstance(h, dict) and str(h.get("order_id") or "") == order_id]
    live = [j for j in jobs if str(j.get("order_id") or "") == order_id and str(j.get("status") or "") in ("queued", "running")]
    if live:
        print(json.dumps({"ok": False, "error": "这张单还在队列里（%s），不用补合入" % live[0].get("status"), "order_id": order_id},
                         ensure_ascii=False))
        return 1
    if not hist:
        print(json.dumps({"ok": False, "error": "历史里没有这张单", "order_id": order_id}, ensure_ascii=False))
        return 1
    src = max(hist, key=lambda h: str(h.get("job_id") or ""))
    repo = os.path.abspath(str(src.get("repo") or STEWARD_ROOT))
    branch = "wt/" + (worker_worktree.safe_name(order_id) if worker_worktree else order_id)
    if not worker_worktree or not worker_worktree.branch_exists(repo, branch):
        print(json.dumps({"ok": False, "error": "沙箱分支已不在，没有可补合入的成果", "branch": branch, "repo": repo},
                         ensure_ascii=False))
        return 1
    head_branch = (worker_worktree.head_branch(repo) if worker_worktree else "") or "main"
    r = subprocess.run(["git", "--no-optional-locks", "-C", repo, "rev-list", "--count", head_branch + ".." + branch],
                       capture_output=True, timeout=60)
    ahead = (r.stdout or b"").decode("utf-8", "replace").strip()
    drop = ("status", "finished_at", "finished_ts", "e2e_total_sec", "disposition", "result_reason", "script_trust", "needs_reason",
            "report_json", "requeued_reason", "requeued_at", "requeued_ts", "not_before_ts", "cancel_reason", "cancelled_at",
            "cancelled_by", "archived_from_jobs", "archived_at", "infra_last_reason", "merge_last_reason",
            "crash_requeues", "infra_retries", "merge_requeues")
    job = {k: v for k, v in src.items() if k not in drop}
    job.update({
        "job_id": f"{order_id}_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "status": "queued", "resume_at": "gates",
        "remerge_of": src.get("job_id"), "remerge_at": iso_now(),
        "remerge_reason": reason or "质检已过、只是没合进去：补双门 + 合入",
        "enqueued_ts": now_ts(), "enqueued_at": iso_now(),
    })
    if _slots_enabled():                 # 与 enqueue 同一口径：并行道锁内追加，防与在跑 tick 互覆
        import slot_ledger
        with slot_ledger.file_lock("factory_queue", 30):
            qc = load_queue()
            qc["jobs"] = (qc.get("jobs") or []) + [job]
            atomic_write_json(QUEUE_PATH, qc)
    else:
        q["jobs"] = jobs + [job]
        atomic_write_json(QUEUE_PATH, q)
    append_v3_event(order_id, 0, "remerge", "内核补合入",
                    "重新排队：只补双门 + 合入（来自 %s，分支领先主干 %s 个提交）" % (src.get("job_id"), ahead or "?"))
    print(json.dumps({"ok": True, "order_id": order_id, "job_id": job["job_id"], "resume_at": "gates", "branch": branch,
                      "commits_ahead": ahead, "repo": repo}, ensure_ascii=False, indent=2))
    return 0


# ------------------------------------------------------------------ 控制台事件护栏（隐藏窗口里的跑单进程）

_CONSOLE_GUARD: dict = {"installed": False, "handler": None, "events": []}
_CTRL_NAMES = {0: "CTRL_C", 1: "CTRL_BREAK", 2: "CTRL_CLOSE", 5: "CTRL_LOGOFF", 6: "CTRL_SHUTDOWN"}


def _console_peers() -> list:
    """此刻和本进程共用一个控制台的进程，一项 "pid:映像名"。没有控制台 / 查不了就返回空表，绝不抛错。"""
    out: list = []
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
        arr = (ctypes.c_uint * 256)()
        n = int(k32.GetConsoleProcessList(arr, 256))
        for i in range(min(n, 256)):
            pid = int(arr[i])
            name = "?"
            h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(260)
                    ln = ctypes.c_uint(260)
                    if k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(ln)):
                        name = os.path.basename(buf.value)
                finally:
                    k32.CloseHandle(ctypes.c_void_p(h))
            out.append("%d:%s" % (pid, name))
    except Exception:
        return out
    return out


def _note_console_ctrl(evt: int, via: str) -> None:
    """控制台事件来了记一笔：哪一种、几点、当时同控制台还有谁。只追加，永不抛错。"""
    evt = int(evt)
    try:
        _CONSOLE_GUARD["events"].append({"ts": iso_now(), "event": evt, "via": via})
        peers = _console_peers()
        oid = ""
        try:
            oid = str(_V4_RUN_CTX.get("order_id") or "")
        except Exception:
            oid = ""
        v4_trace("console_ctrl_event", oid, event=evt, name=_CTRL_NAMES.get(evt, str(evt)), via=via, peers=peers)
        if evt in (0, 1):
            _append_patrol("[CONSOLE_CTRL %s] 跑单进程(pid %d, 单 %s)收到控制台 %s 事件——已挡下，tick 接着跑；同控制台进程：%s"
                           % (iso_now(), os.getpid(), oid or "-", _CTRL_NAMES.get(evt, evt), " ".join(peers)[:600]))
    except Exception:
        pass


def _console_hidden() -> bool:
    """本进程的控制台窗口是隐藏的 / 根本没有控制台（计划任务 + wscript 起的 tick、并行道 tick 都是这样）。"""
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return True
        return not ctypes.windll.user32.IsWindowVisible(hwnd)
    except Exception:
        return False        # 判断不了就当它是有人在看的窗口：护栏不装，Ctrl+C 照常能用


def _install_console_guard() -> bool:
    """隐藏窗口里的跑单进程不许被一次控制台 Ctrl+C 悄悄弄死。

    2026-11-26 凌晨 G42R4 的跑单进程连着四次「没正常收口」，实为一个机制：工人跑内核 e2e 套件，套件里的
    探活调用 os.kill(pid, 0) 在 Windows 上是「给同控制台的其它进程发 Ctrl+C」。跑单进程没人按 Ctrl+C 也会中招
    （Python 默认把它变成 KeyboardInterrupt，主线程正卡在等 CLI 退出，等 CLI 一收场整只 tick 当场消失——
    不留轨迹、不写阶段日志），CLI 那头则当成「会话被用户打断」收场。这里装两层：
      · Win32 控制台事件处理器：事件一到就记一笔（种类 / 时刻 / 同控制台进程），然后回「已处理」，tick 接着跑；
      · Python 层 SIGINT 处理器兜底：万一有代码后来又注册了别的控制台处理器，也不会冒出 KeyboardInterrupt。
    只在窗口隐藏 / 没有控制台时装：人在终端里手动跑 tick，Ctrl+C 仍然照常打断。真要停一只 tick 用 taskkill /F。"""
    if os.name != "nt" or _CONSOLE_GUARD["installed"]:
        return _CONSOLE_GUARD["installed"]
    if not _console_hidden():
        return False
    try:
        import ctypes
        import signal
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k32.SetConsoleCtrlHandler.restype = ctypes.c_int
        handler_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def _py_sigint(signum, frame):      # noqa: ARG001 —— 兜底：只留痕，不抛 KeyboardInterrupt
            _note_console_ctrl(0, "python-sigint")

        signal.signal(signal.SIGINT, _py_sigint)

        def _on_ctrl(evt):
            _note_console_ctrl(int(evt), "win32")
            return 1 if int(evt) in (0, 1) else 0

        cb = handler_t(_on_ctrl)
        if not k32.SetConsoleCtrlHandler(ctypes.c_void_p(ctypes.cast(cb, ctypes.c_void_p).value), 1):
            return False
        _CONSOLE_GUARD.update(installed=True, handler=cb)
        return True
    except Exception:
        return False


def main() -> int:
    _install_console_guard()
    ap = argparse.ArgumentParser(description="AI Factory V3 Pipeline Kernel")
    ap.add_argument("--smoke-test", action="store_true", help="Run 1-turn gateway routing test")
    ap.add_argument("--once", action="store_true", help="Run one queued job (alias for tick)")
    ap.add_argument("--order", default="", help="Preferred order_id to execute with --once")
    ap.add_argument("--backfill-tasks", action="store_true", help="Backfill V3 CLI sessions into ZCode tasks-index.sqlite")
    sub = ap.add_subparsers(dest="cmd")

    enq = sub.add_parser("enqueue", help="Add job to factory queue")
    enq.add_argument("--order", required=True)
    enq.add_argument("--kind", default="ui_dashboard", choices=["ui", "ui_dashboard", "cad", "code"])
    enq.add_argument("--title", required=True)
    enq.add_argument("--spec-file", required=True)
    enq.add_argument("--target", default="")
    enq.add_argument("--targets", default="", help="逗号分隔的多个目标文件（code 单必填其一）")
    enq.add_argument("--repo", default="")
    enq.add_argument("--build-target", default="")
    enq.add_argument("--test-cmd", default="")
    enq.add_argument("--max-attempts", type=int, default=2)
    enq.add_argument("--depends-on", default="",
                     help="V5.0：前置订单号（逗号分隔）。前置没交付，本单留在队列不派发")
    enq.add_argument("--seed-gates", action="store_true",
                     help="V5.0：配合 --seed-from——上一代双门已过、只差合入时，本单直接过双门 + 合入，不再施工")
    enq.add_argument("--seed-from", default="",
                     help="V5.0 带种重下：把上一代订单（如 C-20261125-G44R3）沙箱里已提交的成果先并进本单沙箱，工人接着干")

    rej = sub.add_parser("reject", help="Initiator 24h penalty-free rejection backdoor")
    rej.add_argument("--order", required=True)
    rej.add_argument("--reason", default="发起人24h视检退回重做")
    rej.add_argument("--dry-run", action="store_true")

    t_p = sub.add_parser("tick", help="Ingest inbox and run one queued job if present")
    t_p.add_argument("--order", default="")
    ro_p = sub.add_parser("run-once", help="Alias of tick")
    ro_p.add_argument("--order", default="")
    sub.add_parser("status", help="Print queue, recent runs, and today budget ledger")
    sub.add_parser("backfill-tasks", help="Backfill V3 CLI sessions into ZCode tasks-index.sqlite")
    cs_p = sub.add_parser("check-spec", help="V4.0-P2: validate the ```assertions``` block of a spec (offline)")
    cs_p.add_argument("--spec-file", required=True)
    ps_p = sub.add_parser("probe-spec", help="V4.0-P2: dry-run spec assertions against current production page")
    ps_p.add_argument("--spec-file", required=True)
    ps_p.add_argument("--target", default="dashboard/v2.html")
    ps_p.add_argument("--repo", default="")
    pc_p = sub.add_parser("probe-candidate", help="V4.0-P3: full Gate1 on a ready-made candidate html (no Builder); --apply commits if all green")
    pc_p.add_argument("--candidate", required=True)
    pc_p.add_argument("--spec-file", default="")
    pc_p.add_argument("--target", default="dashboard/v2.html")
    pc_p.add_argument("--repo", default="")
    pc_p.add_argument("--freeze-all", action="store_true", help="auto pixel-freeze: default + every zone + degraded x 3 viewports")
    pc_p.add_argument("--max-pct", type=float, default=0.2)
    pc_p.add_argument("--apply", action="store_true")
    pc_p.add_argument("--message", default="")
    rm_p = sub.add_parser("remerge", help="V5.0: 把「质检已过、只是没合进去」的单重新排队，只补双门 + 合入（沙箱成果原样复用）")
    rm_p.add_argument("--order", required=True)
    rm_p.add_argument("--reason", default="")

    args = ap.parse_args()
    if args.cmd == "probe-candidate":
        return cmd_probe_candidate(args.candidate, args.spec_file, args.target, args.repo, args.freeze_all, args.max_pct, args.apply, args.message)
    if args.cmd == "check-spec":
        return cmd_check_spec(args.spec_file)
    if args.cmd == "probe-spec":
        return cmd_probe_spec(args.spec_file, args.target, args.repo)
    if args.smoke_test:
        return cmd_smoke_test()
    if args.backfill_tasks or args.cmd == "backfill-tasks":
        n = backfill_historical_v3_sessions()
        print(json.dumps({"backfilled": n}))
        return 0
    if args.once:
        return cmd_tick(target_order_id=args.order)
    if args.cmd == "enqueue":
        _v4e = load_v4_flags()
        if _v4e.get("assertion_gate") and _v4e.get("require_assertions") and normalize_order_kind(args.kind) == "ui":
            try:
                _a, _e = extract_assertions(open(args.spec_file, "r", encoding="utf-8", errors="replace").read())
            except OSError as exc:
                _a, _e = [], [f"读不了规格文件: {exc}"]
            if _e or not _a:
                print(json.dumps({"ok": False, "rejected": True, "errors": _e or ["规格书缺 ```assertions``` 断言块"],
                                  "hint": "先用 check-spec / probe-spec 自检"}, ensure_ascii=False, indent=2))
                return 2
        targets = [t.strip() for t in (getattr(args, "targets", "") or "").split(",") if t.strip()]
        if normalize_order_kind(args.kind) in ("code", "cad") and not targets and not args.target:
            print(json.dumps({"ok": False, "rejected": True,
                              "errors": ["code/cad 单必须用 --target 或 --targets 声明目标文件（不再默认 factory_runner.py / dashboard/v2.html）"]},
                             ensure_ascii=False, indent=2))
            return 2
        _seed = str(getattr(args, "seed_from", "") or "").strip()
        job = enqueue_job(args.order, args.kind, args.title, args.spec_file, args.target, args.max_attempts, args.repo, args.build_target, args.test_cmd,
                          targets=targets, extra_meta=_extra_meta(_seed, str(getattr(args, "depends_on", "") or ""),
                                                                  bool(getattr(args, "seed_gates", False))))
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "reject":
        res = reject_order(args.order, args.reason, dry_run=args.dry_run)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if args.cmd == "remerge":
        return cmd_remerge(args.order, args.reason)
    if args.cmd in ("tick", "run-once"):
        return cmd_tick(target_order_id=getattr(args, "order", ""))
    if args.cmd == "status":
        return cmd_status()
    ap.print_help()
    return 0


if __name__ == "__main__":
    _install_console_guard()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as _fatal:  # noqa: BLE001 —— 崩了也要留一笔：轨迹 + 巡检告警，别再「没正常收口」查无实据
        try:
            import traceback
            v4_trace("kernel_fatal", "", kind=type(_fatal).__name__, error=repr(_fatal), where=traceback.format_exc()[-2000:])
            _append_patrol("[KERNEL_FATAL %s] %s: %s" % (iso_now(), type(_fatal).__name__, repr(_fatal)[:300]))
        except Exception:
            pass
        raise
