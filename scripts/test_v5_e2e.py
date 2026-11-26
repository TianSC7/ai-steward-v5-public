# -*- coding: utf-8 -*-
"""V5.0 端到端干跑：真的 execute_job，真的沙箱 / Gate1 / 质检简报 / 合入 / 收口，只有「调模型」换成剧本。

为什么要有它：内核每个单号一个新进程，execute_job 是两千多行的内联流程，单测够不着的地方
（收口落账、回炉循环、合入失败分类、工厂故障重排）一个笔误就是整单崩溃，线上一轮 30-60 分钟。
这里不调模型、不联网、不碰真实台账：factory_runner 的全部路径常量改指临时目录，
worker_worktree 的配置 / 台账改指用例自己的。每个用例各建一个临时 git 仓当「活仓」。

  Z1 工人自提交 + 主干同文件别处有手改 → 保护性提交 → 合入 → done
  Z2 质检第一轮退 P1 → 回炉一轮（带修改提示词 + 销账）→ 第二轮过 → done
  Z3 质检过了但合入被工厂侧问题挡住 → merge_infra，不回炉、不冤枉工人
  Z4 主干同一处也有改动 → 合入冲突 → 回炉一轮在沙箱里解决 → 合入成功
  Z5 质检一直空响应 → 工厂故障原单重排（沙箱成果保留）→ 重跑时不再施工，直接过双门合入
  Z6 连续两轮 P0 不降 → 防震荡止损 needs_human
  Z7 工人什么都没改 → Gate1 判零改动 → 轮次用尽 → failed，原因如实
  Z14 工人自己提交过、沙箱只剩没跟踪的运行态文件 → 照常合入（2026-11-26，G44R4 / G44R5 卡在合入）
  Z15 本单的验收脚本被工人改了 → 质检考卷事前单列；没改不添（2026-11-26，V5.0 最后一条名义规则做实）
  Z16 code 单的目标带看板页面、考卷带 JSON 断言块 → Gate1 上真引擎逐条判；不过回炉、取证环境故障不冤枉工人；
      考卷没断言块或断言块是散文清单 → 照旧不拦单、不跑（2026-11-26，此前只有 ui 单过真引擎，code 单零运行时证据也机械放行）
  Z18 code 单的沙箱页只有生产页自带的正文墙（计数不同）→ 不判 FAIL、不回炉；做得比生产页更差 → 回炉（2026-11-26，G57 第 3 轮实况）
  Z19 直连择道：白天施工隔单交替 STEP 先 / R4 先，臂记在登记文件、择道结果进事件流水；质检、夜班、小单不参加
      （2026-11-26，换线 A/B）
  Z0  全程不许 os.kill(pid, 0) 探活（Windows 上那是给同控制台的其它进程发 Ctrl+C，2026-11-26，G42R4）

用法：python -X utf8 test_v5_e2e.py [--json]
退出码 0 = 全部符合预期；2 = 有偏差。
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
import traceback

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import expert_seat  # noqa: E402
import factory_runner as fr  # noqa: E402
import worker_worktree as ww  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []

# Windows 上 os.kill(pid, 0) 不是探活：signal.CTRL_C_EVENT 恰好等于 0，这一下是 GenerateConsoleCtrlEvent(CTRL_C, pid)。
# 这套 e2e 会被工人在工厂自己的控制台里跑——旧版 expert_seat._alive 这么「探活」一次，同控制台的跑单内核和它的 CLI
# 就各挨一次 Ctrl+C（2026-11-26 凌晨 G42R4 的跑单进程连着四次「没正常收口」）。探活一律在这里拦下并记名。
_KILL_CALLS: list = []
_REAL_KILL = os.kill


def _no_probe_kill(pid, sig):
    if os.name == "nt" and int(sig) == 0:
        _KILL_CALLS.append("%s <- %s" % (pid, "".join(traceback.format_stack(limit=6)[:-1]).replace("\n", " | ")))
        raise OSError("Windows 上 os.kill(pid, 0) 是给同控制台的其它进程发 Ctrl+C，不是探活")
    return _REAL_KILL(pid, sig)


os.kill = _no_probe_kill


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def now_iso():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 与线上 config/steward.json 的 v4 段一致（2026-11-25 读自 AA），干跑走的就是生产的开关组合
LIVE_V4 = {
    "merge_gate": True, "wallclock_live": True, "char_icon_hard_fail": True, "uispec_prompt": True,
    "drop_details_gate": True, "fuse_dry_run_until": "", "order_wallclock_sec": 5400, "assertion_gate": True,
    "require_assertions": True, "closeout_gate": True, "probe_regex_fix": "observe", "lint_gate": True,
    "rewrite_authorized_orders": [],
}

APP0 = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"
NOTES_PATH_RE = re.compile(r"把结论写到 `([^`]+)`")
PASS_JSON = json.dumps({"user_and_core_task": "改配置", "findings": [], "rework_prompt": "", "summary": "全部验收项通过"}, ensure_ascii=False)
V1 = "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n"          # 第一版交付
V2 = "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n# logged\n"  # 回炉后的第二版
FAKE_SUITE = r'''# -*- coding: utf-8 -*-
"""干跑用的假回归套件：输出格式与工厂 scripts/run_tests.py --json 一致（最后一行是带 cases 的 JSON）。"""
import json
import os

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cases = [{"case": "k_%s" % n, "status": "PASS"} for n in (1, 2, 3)]
src = open(os.path.join(here, "scripts", "factory_runner.py"), encoding="utf-8").read()
if "BROKEN" in src:
    cases[1]["status"] = "FAIL"
if os.path.exists(os.path.join(here, "state", "PRE_EXISTING")):
    cases[2]["status"] = "FAIL"
print(json.dumps({"cases": cases}))
'''


def findings_json(sev: str, what: str, fix: str, summary="有不通过项") -> str:
    return json.dumps({"user_and_core_task": "改配置", "findings": [
        {"id": "F1", "question": "Q1", "severity": sev, "status": "confirmed", "evidence": ["回执里没有退出码"], "what": what, "fix": fix},
    ], "rework_prompt": "1. " + fix, "summary": summary}, ensure_ascii=False)


class Stage:
    """一次假调度：role=worker 时 do(world, cwd) 负责改沙箱；role=verifier 时 text 是回复正文。"""

    def __init__(self, role, do=None, text="", **over):
        self.role, self.do, self.text, self.over = role, do, text, over


class World:
    def __init__(self, case, v4_over=None, kernel_repo=False):
        self.home = os.path.join(case, "home")
        self.st = os.path.join(self.home, "steward")          # 活仓 = STEWARD_ROOT
        self.bp = os.path.join(self.home, "blueprint")
        for d in ("state", os.path.join("logs", "factory_runner"), os.path.join("logs", "v4_trace"), "config",
                  "verify_profiles", os.path.join("state", "factory_inbox"), "dashboard", "scripts", "docs"):
            os.makedirs(os.path.join(self.st, d), exist_ok=True)
        for d in ("golden", os.path.join("reports", "transcripts"), "verify_profiles"):
            os.makedirs(os.path.join(self.bp, d), exist_ok=True)
        v4 = dict(LIVE_V4)
        v4.update(v4_over or {})
        write(os.path.join(self.st, "config", "steward.json"), json.dumps({"v4": v4}, ensure_ascii=False))
        write(os.path.join(self.st, "config", "board_token.txt"), "FAKE-TOKEN-FOR-DRY-RUN\n")
        for k in ("code", "ui", "cad"):
            write(os.path.join(self.st, "verify_profiles", k + ".md"), "# 考卷模板 %s\n- Q1 逐条核对验收项\n" % k)
        write(os.path.join(self.st, "scripts", "app.py"), APP0)
        write(os.path.join(self.st, "scripts", "other.py"), "o = 1\n")
        write(os.path.join(self.st, "docs", "readme.md"), "readme\n")
        write(os.path.join(self.st, "state", "queue_seed.json"), "{}\n")
        self.kernel_repo = bool(kernel_repo)
        if kernel_repo:
            # 假装这是个工厂内核仓：有内核文件、有一套自己的回归套件（工厂文件里带 BROKEN 或仓里有 PRE_EXISTING 就红）
            write(os.path.join(self.st, "scripts", "factory_runner.py"), "X = 1\n")
            write(os.path.join(self.st, "scripts", "worker_worktree.py"), "Y = 1\n")
            write(os.path.join(self.st, "scripts", "run_tests.py"), FAKE_SUITE)
        self.git(["init", "-q", self.st])
        self.git(["config", "user.email", "e2e@example.com"])
        self.git(["config", "user.name", "e2e"])
        self.git(["config", "core.autocrlf", "false"])
        self.git(["add", "-A"])
        self.git(["commit", "-qm", "base"])
        # worker_worktree：工作区根 / 配置 / 台账全在临时目录
        self.cfg = {"root": os.path.join(self.home, "wt"), "links": [], "branch_prefix": "wt/"}
        self.ledger = os.path.join(self.home, "ledger.json")
        self.script: list[Stage] = []
        self.calls: list[dict] = []
        self.hooks: dict[str, list] = {"worker": [], "verifier": []}
        self.expert = expert_notes_only            # 用例可换成会动手 / 会越界的假专家
        self.expert_seen: list = []
        self._orig: dict = {}
        self._install()

    # -- 环境 ------------------------------------------------------------
    def git(self, args, cwd=None, must=True):
        r = subprocess.run(["git", "-c", "core.quotepath=false"] + args, cwd=cwd or self.st, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if must:
            assert r.returncode == 0, "%s failed:\n%s\n%s" % (args, r.stdout, r.stderr)
        return r

    def _install(self):
        st, bp, home = self.st, self.bp, self.home
        paths = {
            "STEWARD_ROOT": st, "BP_ROOT": bp, "MATE_ROOT": home,
            "QUEUE_PATH": os.path.join(st, "state", "factory_queue.json"),
            "BUDGET_LEDGER_PATH": os.path.join(st, "state", "v3_budget_ledger.json"),
            "EVENTS_JSONL_PATH": os.path.join(st, "state", "events.jsonl"),
            "RUN_LOG_DIR": os.path.join(st, "logs", "factory_runner"),
            "V4_TRACE_DIR": os.path.join(st, "logs", "v4_trace"),
            "LOCK_PATH": os.path.join(st, "state", "factory_runner.lock"),
            "PAUSE_FLAG_PATH": os.path.join(st, "state", "factory_paused.flag"),
            "PATROL_ALERT": os.path.join(st, "state", "PATROL_ALERT.txt"),
            "INBOX_DIR": os.path.join(st, "state", "factory_inbox"),
            "GOLDEN_ROOT_DIR": os.path.join(bp, "golden"),
            "TRANSCRIPTS_ROOT_DIR": os.path.join(bp, "reports", "transcripts"),
            "DB_SQLITE": os.path.join(home, "nodb", "db.sqlite"),
            "TASKS_INDEX_SQLITE": os.path.join(home, "nodb", "tasks-index.sqlite"),
            "ZCODE_V2_LOG_DIR": os.path.join(home, "nodb", "v2logs"),
            "TOKEN_FILE": os.path.join(st, "config", "board_token.txt"),
            "PROVIDER_CFG_PATH": os.path.join(st, "state", "factory_provider_config.json"),
            "DIRECT_AB_PATH": os.path.join(st, "state", "direct_ab.json"),
            "VERIFIER_CFG_PATH": os.path.join(st, "state", "factory_provider_config_verifier.json"),
            "STEWARD_CONFIG_PATH": os.path.join(st, "config", "steward.json"),
            "VERIFY_PROFILES_DIR": os.path.join(st, "verify_profiles"),
            "BP_VERIFY_PROFILES_DIR": os.path.join(bp, "verify_profiles"),
            "DASHBOARD_DIR": os.path.join(st, "dashboard"),
            "SCRIPTS_DIR": os.path.join(st, "scripts"),
            "RUNTIME_PROVIDER_ROOT": os.path.join(home, "nodb", "provider"),
            "ZCODE_CJS": os.path.join(home, "nodb", "zcode.cjs"),
        }
        for k, v in paths.items():
            self._orig[k] = getattr(fr, k)
            setattr(fr, k, v)
        # 任何残留的绝对路径常量都不许指到临时目录之外（防止哪个漏网的常量写到真机器上）
        leaked = sorted(k for k, v in vars(fr).items()
                        if not k.startswith("__") and isinstance(v, str) and len(v) > 3 and v[1:3] in (":\\", ":/") and "nodb" not in v
                        and not os.path.normcase(v).startswith(os.path.normcase(home)) and not k.startswith("NATIVE_")
                        and k not in ("DOTNET_EXE", "NODE_EXE"))
        assert not leaked, "factory_runner 还有指向临时目录之外的路径常量：%s" % leaked
        self._orig_funcs = {
            "run_headless_zcode": fr.run_headless_zcode, "probe_weekend_glm": fr.probe_weekend_glm,
            "get_gateway_status": fr.get_gateway_status, "detect_sessions_in_window": fr.detect_sessions_in_window,
        }
        fr.run_headless_zcode = self.dispatch
        fr.probe_weekend_glm = lambda *a, **k: {"available": False, "reason": "dry-run: no free lane"}
        fr.get_gateway_status = lambda *a, **k: {"totals": {"requests": 0, "ok": 0, "fail": 0, "tokens_in": 0, "tokens_out": 0,
                                                           "tokens_cached": 0, "cost_cny": 0.0, "lanes": {}}}
        fr.detect_sessions_in_window = lambda *a, **k: []
        # V5.0-real.6：看板服务进程表默认按「没查到」出演——干跑期间不真去查本机进程（慢，也不是干跑世界的事）；用例可换剧本
        self._orig_funcs["board_server_procs"] = fr._board_server_procs
        fr._board_server_procs = lambda *a, **k: []
        self._orig_expert = expert_seat.wants_expert
        expert_seat.wants_expert = lambda *a, **k: False
        # 专家席的租约 / 出场记录 / 锁落到临时目录，别落在代码树的 state/ 里
        self._orig_expert_root = expert_seat._root
        _home = self.home
        expert_seat._root = lambda root=None: (expert_seat.pathlib.Path(root) if root
                                               else expert_seat.pathlib.Path(_home) / "expert_state")
        # 共享文件锁 / 槽位账本日志落在环境变量 STEWARD_STATE_DIR 指的目录（官方测试注入点）：干跑期间改指临时目录
        self._orig_env = os.environ.get("STEWARD_STATE_DIR")
        os.environ["STEWARD_STATE_DIR"] = os.path.join(self.home, "ai_state")
        self._orig_ww = (ww.status, ww.remove, ww.ensure, ww.finish)
        st0, rm0, en0, fin0 = self._orig_ww
        cfg, ledger = self.cfg, self.ledger

        def _w(fn, inject_cfg=False):      # 不管内核按位置还是按关键字传台账，一律换成用例自己的
            def call(tid, repo="", *a, **kw):
                kw.pop("ledger_path", None)
                if inject_cfg:
                    kw["cfg"] = cfg
                return fn(tid, repo=repo, *a, ledger_path=ledger, **kw)
            return call
        ww.status, ww.remove, ww.ensure, ww.finish = _w(st0), _w(rm0), _w(en0, True), _w(fin0)

    def close(self):
        for k, v in self._orig.items():
            setattr(fr, k, v)
        for k, v in self._orig_funcs.items():
            setattr(fr, k, v)
        expert_seat.wants_expert = self._orig_expert
        expert_seat._root = self._orig_expert_root
        if self._orig_env is None:
            os.environ.pop("STEWARD_STATE_DIR", None)
        else:
            os.environ["STEWARD_STATE_DIR"] = self._orig_env
        ww.status, ww.remove, ww.ensure, ww.finish = self._orig_ww

    # -- 假调度 ----------------------------------------------------------
    def dispatch(self, prompt, cwd, timeout_sec, role, **kw):
        title = str(kw.get("task_title") or "")
        if title.startswith("【V5-驻厂专家】"):
            # 专家段不吃剧本：按 w.expert 演，单独记一笔
            self.calls.append({"role": "expert", "prompt": prompt or "", "cwd": cwd, "title": title})
            out = self.expert(self, cwd, prompt) or ""
            t = time.time() * 1000
            return {"role": role, "shift": "dry-run", "model_order": [], "exit_code": 0, "timed_out": False,
                    "stall_timeout": False, "duration_sec": 1.0, "start_ms": t - 1000, "end_ms": t,
                    "gateway_delta": {"requests": 1, "ok": 1, "fail": 0, "tokens_in": 1000, "tokens_out": 200,
                                      "tokens_cached": 900, "cost_cny": 0.0, "cost_est_cny": 0.0, "lanes": {}},
                    "sessions": [{"session_id": "dry-expert"}], "task_title": title,
                    "stdout": out, "lane_selected": "gateway"}
        stage = self.script.pop(0) if self.script else Stage(role)
        assert stage.role == role, "剧本第 %d 步是 %s，实际调度的是 %s" % (len(self.calls) + 1, stage.role, role)
        n = sum(1 for c in self.calls if c["role"] == role)
        self.calls.append({"role": role, "prompt": prompt or "", "prompt_desktop": kw.get("prompt_desktop") or "",
                           "cwd": cwd, "disallowed": kw.get("disallowed_tools") or "", "title": kw.get("task_title") or ""})
        for hook in self.hooks[role]:              # 用例在「这一刻」往主干 / 沙箱里塞东西
            hook(self, cwd, n)
        out = stage.do(self, cwd, n) or "" if stage.do else stage.text
        t = time.time() * 1000
        res = {"role": role, "shift": "dry-run", "model_order": [], "exit_code": 0, "timed_out": False,
               "stall_timeout": False, "duration_sec": 1.0, "start_ms": t - 1000, "end_ms": t,
               "gateway_delta": {"requests": 1, "ok": 1, "fail": 0, "tokens_in": 1000, "tokens_out": 200,
                                 "tokens_cached": 900, "cost_cny": 0.0, "cost_est_cny": 0.0, "lanes": {}},
               "sessions": [{"session_id": "dry-%s-%d" % (role, n)}], "task_title": kw.get("task_title") or "",
               "stdout": out, "lane_selected": "gateway"}
        res.update(stage.over)
        return res

    def ev_dir(self, oid):
        return os.path.join(self.bp, "reports", "evidence", oid)

    def order(self, oid, targets=("scripts/app.py", "scripts/other.py"), kind="code", spec_text=None, **over):
        spec = os.path.join(self.home, "spec_%s.md" % oid)
        write(spec, spec_text or ("# 规格\n把 `scripts/app.py` 的 b 改成 20，并让 `python scripts/app.py` 退出码为 0。\n\n"
                                  "## 验收\n- `python scripts/app.py` 退出码 0\n"))
        job = {"job_id": "J-" + oid, "order_id": oid, "status": "queued", "kind": kind, "title": "干跑 " + oid,
               "targets": list(targets), "repo": self.st, "spec_file": spec,
               "enqueued_ts": time.time(), "enqueued_at": now_iso(), "max_attempts": 2}
        job.update(over)
        q = {"jobs": [job], "history": []}
        fr.atomic_write_json(fr.QUEUE_PATH, q)
        return q, job

    def run(self, q, job):
        rep = fr.execute_job(q, job)
        qd = json.load(open(fr.QUEUE_PATH, encoding="utf-8"))
        return rep, qd

    def main_text(self, rel="scripts/app.py"):
        return read(os.path.join(self.st, rel.replace("/", os.sep)))

    def events(self, oid):
        p = fr.EVENTS_JSONL_PATH
        if not os.path.isfile(p):
            return []
        return [json.loads(ln) for ln in read(p).splitlines() if ln.strip() and json.loads(ln).get("order") == oid]


def deliver(world, cwd, n, text=None):
    """假工人：改沙箱里的 app.py，自己 commit，写交件回执。"""
    text = text or V1
    wt = cwd
    write(os.path.join(wt, "scripts", "app.py"), text)
    write(os.path.join(wt, "scripts", "app_test.py"), "def test_b():\n    assert 20 == 20\n")
    world.git(["add", "-A"], cwd=wt)
    world.git(["commit", "-qm", "builder: b=20"], cwd=wt)
    write(os.path.join(world.ev_dir(os.path.basename(wt)), "FACTORY_W_DELIVERY.md"), "# 回执\n$ python scripts/app.py\n(无输出)\nEXIT=0\n")
    return "已把 b 改成 20，测试通过。"


def deliver_dirty(world, cwd, n, text=None):
    """假工人：活干完自己 commit 了，但沙箱里留了一堆没跟踪的运行态文件（state/tmp/…）。"""
    out = deliver(world, cwd, n, text)
    write(os.path.join(cwd, "state", "tmp", "probe.log"), "probe output\n")
    write(os.path.join(cwd, "state", "tmp", "nested", "cdp.png"), "x")
    return out


CLOSED_R2_JSON = json.dumps({"user_and_core_task": "改配置", "findings": [],
                             "closeout": [{"tag": "R1-F1", "status": "closed", "evidence": "专家补上了缺失的日志"},
                                          {"tag": "R2-F1", "status": "closed", "evidence": "专家补上了缺失的日志"}],
                             "rework_prompt": "", "summary": "上一轮缺陷已关，全部验收项通过"}, ensure_ascii=False)


def expert_notes_only(world, cwd, prompt, done=None, handoff="接着把验收项逐条补齐，并在回执里贴出原始输出与退出码"):
    """假专家：只给判断、不动文件（五节齐全的结论写到派工单指定的路径）。记下出场那一刻的席位状态。"""
    m = NOTES_PATH_RE.search(prompt or "")
    path = m.group(1) if m else os.path.join(world.ev_dir(os.path.basename(cwd)), "expert_notes.md")
    write(path, expert_seat.notes_template(stuck="厂内同一条路走了几轮，严重问题没降下去。",
                                           cause="（专家判断）缺一处日志导致页面白屏。",
                                           done=done or "（本次未动手，只给判断）", handoff=handoff))
    world.expert_seen.append({"cwd": cwd, "live": expert_seat.active_stages(expert_seat._root()), "notes": path})
    return "专家结论已写入。"


def worker_texts(world, cwd, n, texts):
    write(os.path.join(cwd, "scripts", "app.py"), texts[min(n, len(texts) - 1)])
    return "done"


# ------------------------------------------------------------------ 用例
def z1(case):
    w = World(case)
    try:
        q, job = w.order("Z1")

        def hand_edit(world, cwd, n):
            if n == 0:      # 质检期间，主干同文件别处被人手改了（看板热修那类）
                write(os.path.join(world.st, "scripts", "app.py"), "a = 1\nb = 2\nc = 3\nd = 4\ne = 500\n")
        w.hooks["verifier"].append(hand_edit)
        w.script = [Stage("worker", do=deliver), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z1 工人自提交 + 主干手改 → 交付 done", rep["final_status"] == "done" and qd["history"][-1]["status"] == "done",
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z1 主干同时含交付与手改", w.main_text() == "a = 1\nb = 20\nc = 3\nd = 4\ne = 500\n", repr(w.main_text()))
        check("Z1 工人的新测试文件进了主干", os.path.isfile(os.path.join(w.st, "scripts", "app_test.py")))
        mr = (rep["billing_breakdown"].get("worktree") or {}).get("merge_result") or {}
        inner = mr.get("merge") or {}
        check("Z1 合入前自动落了保护性提交", (inner.get("protective_commit") or {}).get("committed") is True
              and "protective commit" in w.git(["log", "-3", "--format=%s"]).stdout, json.dumps(inner, ensure_ascii=False)[:300])
        check("Z1 运行账单带内核构建号", rep.get("kernel_build") == fr.KERNEL_BUILD)
        check("Z1 事件流水记了合入", any(e["phase"] == "merged" for e in w.events("Z1")))
        check("Z1 收口后订单出队列进历史、去向=完全实现", not qd["jobs"] and qd["history"][-1]["job_id"] == "J-Z1"
              and qd["history"][-1].get("disposition") == "完全实现", json.dumps(qd["history"][-1].get("disposition"), ensure_ascii=False))
        wk = next(c for c in w.calls if c["role"] == "worker")
        check("Z1 施工派工单列出全部声明目标", all(os.path.join(cwd_name, t) in wk["prompt"] for t in job["targets"] for cwd_name in [wk["cwd"]]),
              wk["prompt"][:300])
        vf = next(c for c in w.calls if c["role"] == "verifier")
        check("Z1 质检拿到的全文简报含已提交改动的 diff、回执、纪律",
              "+b = 20" in vf["prompt_desktop"] and "EXIT=0" in vf["prompt_desktop"] and "只允许调用 Read 和 Grep" in vf["prompt_desktop"],
              "len=%s" % len(vf["prompt_desktop"]))
        check("Z1 质检通道只许 Read/Grep（其余工具禁用）",
              vf["disallowed"] == "Bash Edit Write MultiEdit TodoWrite Task Glob", vf["disallowed"])
        check("Z1 质检提示词没超命令行上限", len(vf["prompt"]) <= 24000 and len(vf["prompt"]) <= len(vf["prompt_desktop"]),
              "%s / %s" % (len(vf["prompt"]), len(vf["prompt_desktop"])))
    finally:
        w.close()


CLOSED_JSON = json.dumps({"user_and_core_task": "改配置", "findings": [],
                          "closeout": [{"tag": "R1-F1", "status": "closed", "evidence": "本轮补了日志，回执里有"}],
                          "rework_prompt": "", "summary": "上一轮缺陷已关，全部验收项通过"}, ensure_ascii=False)


def z2(case):
    w = World(case)
    try:
        q, job = w.order("Z2")
        w.script = [Stage("worker", do=lambda world, cwd, n: worker_texts(world, cwd, n, [V1, V2])),
                    Stage("verifier", text=findings_json("P0", "页面白屏", "补日志并修白屏")),
                    Stage("worker", do=lambda world, cwd, n: worker_texts(world, cwd, n, [V1, V2])),
                    Stage("verifier", text=CLOSED_JSON)]
        rep, qd = w.run(q, job)
        check("Z2 第一轮退 P0、第二轮修好并销账 → done，用了两轮", rep["final_status"] == "done" and rep["attempts_used"] == 2,
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z2 第二版进了主干", w.main_text() == V2, repr(w.main_text()))
        wk = [c for c in w.calls if c["role"] == "worker"]
        brief2 = read(os.path.join(w.ev_dir("Z2"), "work_brief_attempt2.md"))
        check("Z2 回炉派工单带上质检开的修改提示词与缺陷清单",
              len(wk) == 2 and "补日志并修白屏" in brief2 and "[Gate2 审美审计缺陷]" in brief2 and "页面白屏" in brief2, brief2[-400:])
        check("Z2 回炉派工单保留上一轮成果（在沙箱里接着改，不是从零重来）", "增量修改" in brief2 and "不必整文件从零重写" in brief2,
              [ln for ln in brief2.splitlines() if "上一轮" in ln][:2].__str__()[:300])
        vf = [c for c in w.calls if c["role"] == "verifier"]
        check("Z2 第二轮质检简报带上一轮缺陷销账清单", len(vf) == 2 and "R1-F1" in vf[1]["prompt_desktop"] and "销账" in vf[1]["prompt_desktop"],
              "verifier calls=%s" % len(vf))
        ev = [e["phase"] for e in w.events("Z2")]
        check("Z2 事件流水：两轮施工、两轮机检、两轮质检、最后合入", ev.count("builder_start") == 2 and ev.count("gate1_probe") == 2
              and ev.count("verifier_start") == 2 and ev.count("gate2_verdict") == 2 and ev[-1] == "merged", str(ev))
    finally:
        w.close()

    # 只有 P1/P2 意见、没有 P0：照常交付（意见随单留档），不因为口味之争回炉
    w = World(case)
    try:
        q, job = w.order("Z2b")
        w.script = [Stage("worker", do=deliver), Stage("verifier", text=findings_json("P1", "命名可以更好", "改个名"))]
        rep, qd = w.run(q, job)
        check("Z2b 只有 P1 意见 → 一轮交付 done", rep["final_status"] == "done" and rep["attempts_used"] == 1
              and qd["history"][-1]["status"] == "done" and "P1=1" in rep["final_summary"],
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z2b P1 意见随单留档（不丢）", "命名可以更好" in json.dumps(rep, ensure_ascii=False))
    finally:
        w.close()

    # 第二轮说「都过了」却没逐条销账：上一轮的 P0 按未关算，不能就这么合入。
    # 轮次用尽时内核会请专家救一轮（「质检没销账」这种事专家也修不了），救完仍不过才收场。
    w = World(case)
    try:
        q, job = w.order("Z2c")
        w.script = [Stage("worker", do=lambda world, cwd, n: worker_texts(world, cwd, n, [V1, V2])),
                    Stage("verifier", text=findings_json("P0", "页面白屏", "修白屏")),
                    Stage("worker", do=lambda world, cwd, n: worker_texts(world, cwd, n, [V1, V2])),
                    Stage("verifier", text=PASS_JSON),
                    Stage("worker", do=lambda world, cwd, n: "照专家说的又查了一遍"),
                    Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z2c 没销账就说过了 → 不合入", rep["final_status"] in ("failed", "needs_human") and w.main_text() == APP0
              and "销账未关" in rep["final_summary"], "%s | %s" % (rep["final_status"], rep["final_summary"][:300]))
        check("Z2c 轮次用尽时请专家救过一轮（只救一次）", [e["phase"] for e in w.events("Z2c")].count("expert_rescue") == 1
              and rep["attempts_used"] == 3, "attempts=%s" % rep["attempts_used"])
    finally:
        w.close()


def z3(case):
    """质检已过、只是合入被工厂侧挡住：成果是好的。不判死——原单延时重排，只补「双门 + 合入」，不重派施工。"""
    def busy_merge(*a, **k):
        return {"ok": False, "reason": "repo-busy", "id": "Z3", "error": "index.lock 一直被占用", "conflicts": []}

    w = World(case)
    orig_merge = ww.merge
    try:
        q, job = w.order("Z3")
        w.script = [Stage("worker", do=deliver), Stage("verifier", text=PASS_JSON)]
        ww.merge = busy_merge
        rep, qd = w.run(q, job)
        jq = qd["jobs"][0] if qd["jobs"] else {}
        check("Z3 合入被工厂侧挡住 → 原单延时重排（不判死）", rep.get("job_requeued") is True and jq.get("status") == "queued"
              and jq.get("requeued_reason") == "merge_infra_requeue" and jq.get("resume_at") == "gates"
              and int(jq.get("merge_requeues") or 0) == 1 and not qd["history"],
              json.dumps({k: jq.get(k) for k in ("status", "requeued_reason", "resume_at", "merge_requeues")}, ensure_ascii=False))
        check("Z3 不回炉冤枉工人（只派了一次工人）", rep["attempts_used"] == 1 and len([c for c in w.calls if c["role"] == "worker"]) == 1,
              "attempts=%s" % rep["attempts_used"])
        check("Z3 重排有延时（等仓库锁自己松开）", float(jq.get("not_before_ts") or 0) > time.time() + 30, str(jq.get("not_before_ts")))
        check("Z3 主干没被改动", w.main_text() == APP0)
        check("Z3 沙箱成果还在", read(os.path.join(w.home, "wt", "Z3", "scripts", "app.py")) == V1)
        check("Z3 事件流水记了合入重排", any(e["phase"] == "merge_requeue" for e in w.events("Z3")))
        # 障碍排除后重跑：不再施工，直接过双门合入
        ww.merge = orig_merge
        w.script = [Stage("verifier", text=PASS_JSON)]
        rep2, qd2 = w.run(q, jq)
        check("Z3 障碍排除后原单只补双门 + 合入 → done（没再派工人）", rep2["final_status"] == "done" and w.main_text() == V1
              and len([c for c in w.calls if c["role"] == "worker"]) == 1,
              "%s | %s" % (rep2["final_status"], rep2["final_summary"][:200]))
        check("Z3 还是同一张单收口、历史里只出现一次", qd2["history"][-1]["job_id"] == "J-Z3" and qd2["history"][-1]["status"] == "done"
              and [h["job_id"] for h in qd2["history"]].count("J-Z3") == 1 and not qd2["jobs"], str([h["job_id"] for h in qd2["history"]]))
    finally:
        ww.merge = orig_merge
        w.close()

    # 障碍一直在：补合入次数用完 → needs_human / merge_infra，结论里写明怎么再补
    w = World(case + "-b")
    try:
        q, job = w.order("Z3b")
        w.script = [Stage("worker", do=deliver), Stage("verifier", text=PASS_JSON)]
        ww.merge = busy_merge
        rep, qd = w.run(q, job)
        jq = qd["jobs"][0] if qd["jobs"] else {}
        for i in range(fr.MAX_MERGE_REQUEUES - 1):
            w.script = [Stage("verifier", text=PASS_JSON)]
            rep, qd = w.run(q, jq)
            jq = qd["jobs"][0] if qd["jobs"] else {}
            check("Z3b 第 %d 次补合入仍是原单重排、没多派工人" % (i + 2), rep.get("job_requeued") is True
                  and len([c for c in w.calls if c["role"] == "worker"]) == 1 and int(jq.get("merge_requeues") or 0) == i + 2,
                  "%s | %s" % (rep.get("job_requeued"), jq.get("merge_requeues")))
        w.script = [Stage("verifier", text=PASS_JSON)]
        repN, qdN = w.run(q, jq)
        hist = qdN["history"][-1] if qdN["history"] else {}
        check("Z3b 补合入次数用完 → needs_human / merge_infra（写明 remerge 可再补）", repN["final_status"] == "needs_human"
              and hist.get("needs_reason") == "merge_infra" and "remerge" in repN["final_summary"] and not qdN["jobs"],
              "%s | %s | %s" % (repN["final_status"], hist.get("needs_reason"), repN["final_summary"][:240]))
        check("Z3b 泵拿到的去向是转专家（补合入），不是打回重派", hist.get("disposition") == "转驻厂专家", str(hist.get("disposition")))
        check("Z3b 主干没被改动、沙箱成果还在", w.main_text() == APP0 and read(os.path.join(w.home, "wt", "Z3b", "scripts", "app.py")) == V1)
    finally:
        ww.merge = orig_merge
        w.close()


def z4(case):
    w = World(case)
    try:
        q, job = w.order("Z4")

        def clash(world, cwd, n):
            if n == 0:      # 质检期间，主干同一行被别人改了
                write(os.path.join(world.st, "scripts", "app.py"), "a = 1\nb = 77\nc = 3\nd = 4\ne = 5\n")

        def resolve(world, cwd, n):
            # 回炉一轮：在沙箱里 git merge main，冲突按「保留主干那行 + 自己的其余改动」解决
            world.git(["merge", "main", "-q"], cwd=cwd, must=False)
            write(os.path.join(cwd, "scripts", "app.py"), "a = 1\nb = 77\nc = 3\nd = 4\ne = 5\n# resolved\n")
            world.git(["add", "-A"], cwd=cwd)
            world.git(["commit", "-qm", "resolve conflict with main"], cwd=cwd)
            return "已在沙箱内解决冲突"
        w.hooks["verifier"].append(clash)
        w.script = [Stage("worker", do=deliver), Stage("verifier", text=PASS_JSON), Stage("worker", do=resolve), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z4 合入冲突 → 回炉一轮在沙箱解决 → done", rep["final_status"] == "done" and rep["attempts_used"] == 2,
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z4 主干结果是解决后的版本", w.main_text() == "a = 1\nb = 77\nc = 3\nd = 4\ne = 5\n# resolved\n", repr(w.main_text()))
        wk = [c for c in w.calls if c["role"] == "worker"]
        brief2 = read(os.path.join(w.ev_dir("Z4"), "work_brief_attempt2.md"))
        check("Z4 回炉派工单点明是合入冲突、点名冲突文件、不要重做功能",
              "[合入冲突·不是质量缺陷]" in brief2 and "scripts/app.py" in brief2 and "不要重做功能" in brief2, brief2[-500:])
        check("Z4 只回炉了一轮（工人总共被派两次）", len(wk) == 2, "worker calls=%s" % len(wk))
        check("Z4 事件流水记了合入失败与最终合入", [e["phase"] for e in w.events("Z4") if e["phase"] in ("merge_failed", "merged")] == ["merge_failed", "merged"])
    finally:
        w.close()


def z4b(case):
    """轮次已经用尽才撞上合入冲突：再加时一轮专门解冲突，不因为「轮次用尽」把过了质检的单判死。"""
    w = World(case)
    try:
        q, job = w.order("Z4b", max_attempts=1)

        def clash(world, cwd, n):
            if n == 0:      # 质检期间，主干同一行被别人改了
                write(os.path.join(world.st, "scripts", "app.py"), "a = 1\nb = 77\nc = 3\nd = 4\ne = 5\n")

        def resolve(world, cwd, n):
            world.git(["merge", "main", "-q"], cwd=cwd, must=False)
            write(os.path.join(cwd, "scripts", "app.py"), "a = 1\nb = 77\nc = 3\nd = 4\ne = 5\n# resolved\n")
            world.git(["add", "-A"], cwd=cwd)
            world.git(["commit", "-qm", "resolve conflict with main"], cwd=cwd)
            return "已在沙箱内解决冲突"
        w.hooks["verifier"].append(clash)
        w.script = [Stage("worker", do=deliver), Stage("verifier", text=PASS_JSON), Stage("worker", do=resolve), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z4b 轮次用尽才撞上合入冲突 → 加时一轮解决 → done", rep["final_status"] == "done" and rep["attempts_used"] == 2,
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z4b 主干结果是解决后的版本", w.main_text() == "a = 1\nb = 77\nc = 3\nd = 4\ne = 5\n# resolved\n", repr(w.main_text()))
        ev = [e["phase"] for e in w.events("Z4b") if e["phase"] in ("merge_conflict_extra_round", "merge_failed", "merged")]
        check("Z4b 事件流水：合入失败 → 加时一轮 → 合入", ev == ["merge_failed", "merge_conflict_extra_round", "merged"], str(ev))
        wk = [c for c in w.calls if c["role"] == "worker"]
        check("Z4b 只加时了一轮（工人总共被派两次）", len(wk) == 2, "worker calls=%s" % len(wk))
    finally:
        w.close()


def z5(case):
    w = World(case)
    try:
        q, job = w.order("Z5")
        w.script = [Stage("worker", do=deliver)] + [Stage("verifier", text="")] * (fr.MAX_VERIFIER_INFRA_RETRIES + 1)
        rep, qd = w.run(q, job)
        jq = qd["jobs"][0] if qd["jobs"] else {}
        check("Z5 质检一直空响应 → 工厂故障原单重排（不判死）", rep.get("job_requeued") is True and jq.get("status") == "queued"
              and jq.get("requeued_reason") == "infra_error_requeue" and int(jq.get("infra_retries") or 0) == 1,
              json.dumps({k: jq.get(k) for k in ("status", "requeued_reason", "infra_retries")}, ensure_ascii=False))
        check("Z5 重排有延时（等工厂自己缓过来）", float(jq.get("not_before_ts") or 0) > time.time() + 30, str(jq.get("not_before_ts")))
        check("Z5 事件流水记了工厂故障重排", any(e["phase"] == "infra_requeue" for e in w.events("Z5")))
        check("Z5 沙箱成果保留", read(os.path.join(w.home, "wt", "Z5", "scripts", "app.py")) == "a = 1\nb = 20\nc = 3\nd = 4\ne = 5\n")
        # 重跑：质检恢复正常 → 不再施工，直接过双门合入
        n_worker = len([c for c in w.calls if c["role"] == "worker"])
        w.script = [Stage("verifier", text=PASS_JSON)]
        rep2, qd2 = w.run(q, jq)
        check("Z5 重跑时复用沙箱成果、不再施工", len([c for c in w.calls if c["role"] == "worker"]) == n_worker,
              "worker calls=%s (重跑前 %s)" % (len([c for c in w.calls if c["role"] == "worker"]), n_worker))
        check("Z5 重跑后交付 done", rep2["final_status"] == "done" and w.main_text() == V1,
              "%s | %s" % (rep2["final_status"], rep2["final_summary"][:200]))
        check("Z5 重跑用的还是同一个单", qd2["history"][-1]["job_id"] == "J-Z5" and qd2["history"][-1]["status"] == "done")
        check("Z5 重跑成功后没留下重复历史", [h["job_id"] for h in qd2["history"]].count("J-Z5") == 1, str([h["job_id"] for h in qd2["history"]]))
    finally:
        w.close()

    # 重跑次数用完：转 needs_human，泵和人都看得见（另起一个活仓，别沾上面已交付的内容）
    w = World(case + "-b")
    try:
        q3, job3 = w.order("Z5b")
        w.script = [Stage("worker", do=deliver)] + [Stage("verifier", text="")] * (fr.MAX_VERIFIER_INFRA_RETRIES + 1)
        rep3, qd3 = w.run(q3, job3)
        jq3 = qd3["jobs"][0] if qd3["jobs"] else {}
        for i in range(fr.MAX_JOB_INFRA_REQUEUES - 1):
            w.script = [Stage("verifier", text="")] * (fr.MAX_VERIFIER_INFRA_RETRIES + 1)
            rep3, qd3 = w.run(q3, jq3)
            jq3 = qd3["jobs"][0] if qd3["jobs"] else {}
            check("Z5b 第 %d 次重跑仍是原单重排、没多派工人" % (i + 2), rep3.get("job_requeued") is True
                  and len([c for c in w.calls if c["role"] == "worker"]) == 1 and int(jq3.get("infra_retries") or 0) == i + 2,
                  "%s | %s" % (rep3.get("job_requeued"), jq3.get("infra_retries")))
        w.script = [Stage("verifier", text="")] * (fr.MAX_VERIFIER_INFRA_RETRIES + 1)
        rep4, qd4 = w.run(q3, jq3)
        check("Z5 重跑次数用完仍不行 → needs_human（泵和人工都看得见）", rep4["final_status"] == "needs_human"
              and qd4["history"][-1].get("needs_reason") == "infra_error" and "[工厂侧故障" in rep4["final_summary"]
              and not qd4["jobs"],
              "%s | %s | %s" % (rep4["final_status"], qd4["history"][-1].get("needs_reason") if qd4["history"] else None, rep4["final_summary"][:200]))
        check("Z5 次数用完后单子只在历史里出现一次（队列里不留影子）", [h["job_id"] for h in qd4["history"]].count("J-Z5b") == 1,
              str([h["job_id"] for h in qd4["history"]]))
    finally:
        w.close()


def z6(case):
    """连续两轮 P0 不降：内核先请专家救一轮（专家在沙箱里出手）；专家也救不回来才防震荡止损。"""
    w = World(case)
    try:
        q, job = w.order("Z6")
        w.script = [Stage("worker", do=deliver), Stage("verifier", text=findings_json("P0", "页面白屏", "修白屏", "P0 未解决")),
                    Stage("worker", do=lambda world, cwd, n: "又改了一遍"), Stage("verifier", text=findings_json("P0", "页面白屏", "修白屏", "P0 仍未解决")),
                    Stage("worker", do=lambda world, cwd, n: "照专家说的又改了一遍"),
                    Stage("verifier", text=findings_json("P0", "页面白屏", "修白屏", "P0 还在"))]
        rep, qd = w.run(q, job)
        ev = [e["phase"] for e in w.events("Z6")]
        check("Z6 连续两轮 P0 不降 → 先请专家救一轮（在本单沙箱里出手）", ev.count("expert_rescue") == 1 and ev.count("expert_start") == 1
              and len(w.expert_seen) == 1 and os.path.normcase(w.expert_seen[0]["cwd"]) == os.path.normcase(os.path.join(w.home, "wt", "Z6")),
              str(ev))
        check("Z6 专家出场那一刻席位灯是真的亮、指向这张单", [a.get("order_id") for a in w.expert_seen[0]["live"]] == ["Z6"],
              json.dumps(w.expert_seen[0]["live"], ensure_ascii=False)[:200])
        check("Z6 救场后 P0 仍不降 → 防震荡止损 needs_human（专家只救一次）", rep["final_status"] == "needs_human"
              and "防震荡止损" in rep["final_summary"] and qd["history"][-1]["status"] == "needs_human"
              and rep["attempts_used"] == 3 and ev.count("expert_rescue") == 1,
              "%s | %s | attempts=%s" % (rep["final_status"], rep["final_summary"][:200], rep["attempts_used"]))
        check("Z6 主干没被合入", w.main_text() == APP0)
        check("Z6 收场后专家席空出来（没有残留出场记录）", expert_seat.active_stages(expert_seat._root()) == [])
    finally:
        w.close()


def z10(case):
    """救场成功：厂内两轮降不下去，专家在沙箱里动手修好，施工接着收尾，第三轮交付。"""

    def expert_fix(world, cwd, prompt):
        write(os.path.join(cwd, "scripts", "app.py"), V2)          # 专家只动本单沙箱里的目标文件
        return expert_notes_only(world, cwd, prompt, done="已改 scripts/app.py：补上缺失的日志（沙箱内，未提交）",
                                 handoff="别动 app.py，补交件回执并核对验收命令")

    w = World(case)
    w.expert = expert_fix
    try:
        q, job = w.order("Z10")
        w.script = [Stage("worker", do=lambda world, cwd, n: worker_texts(world, cwd, n, [V1, V1])),
                    Stage("verifier", text=findings_json("P0", "页面白屏", "补日志并修白屏")),
                    Stage("worker", do=lambda world, cwd, n: worker_texts(world, cwd, n, [V1, V1])),
                    Stage("verifier", text=findings_json("P0", "页面白屏", "补日志并修白屏")),
                    Stage("worker", do=lambda world, cwd, n: "按专家的交付补了回件"),
                    Stage("verifier", text=CLOSED_R2_JSON)]
        rep, qd = w.run(q, job)
        ev = [e["phase"] for e in w.events("Z10")]
        check("Z10 专家救场后第三轮交付 done", rep["final_status"] == "done" and rep["attempts_used"] == 3
              and qd["history"][-1]["disposition"] == "完全实现", "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z10 进主干的是专家修好的那一版", w.main_text() == V2, repr(w.main_text()))
        check("Z10 事件流水：救场 → 专家出场 → 专家结束 → 合入", [p for p in ev if p in ("expert_rescue", "expert_start", "expert_done", "merged")]
              == ["expert_rescue", "expert_start", "expert_done", "merged"], str(ev))
        check("Z10 专家结论落了盘（救场那份，五节齐全）", os.path.isfile(os.path.join(w.ev_dir("Z10"), "expert_notes_rescue.md"))
              and expert_seat.notes_complete(read(os.path.join(w.ev_dir("Z10"), "expert_notes_rescue.md"))))
        ex = next(c for c in w.calls if c["role"] == "expert")
        check("Z10 给专家的派工单：在沙箱、点名目标、活仓只读、不许改验收",
              os.path.normcase(os.path.join(w.home, "wt", "Z10")) in os.path.normcase(ex["prompt"])
              and "`scripts/app.py`" in ex["prompt"] and "只读" in ex["prompt"]
              and "验收条件你无权修改" in ex["prompt"] and "卡住的原因：连续两轮 confirmed P0 未下降" in ex["prompt"]
              and "expert_ops" not in ex["prompt"], ex["prompt"][:400])
        brief3 = read(os.path.join(w.ev_dir("Z10"), "work_brief_attempt3.md"))
        check("Z10 救场轮的施工派工单带上了专家结论、写明不要回退", "驻厂外聘专家已救场出手" in brief3 and "不要回退" in brief3
              and "别动 app.py" in brief3 and "第 3/3 轮" in brief3, brief3[-500:])
        check("Z10 活仓除合入外没被碰（源码目录干净）", w.git(["status", "--porcelain", "--", "scripts"]).stdout == "",
              w.git(["status", "--porcelain", "--", "scripts"]).stdout[:200])
        check("Z10 没有越界记录", not any(e["phase"] == "expert_overreach" for e in w.events("Z10"))
              and not os.path.isdir(os.path.join(w.ev_dir("Z10"), "expert_overreach")))
    finally:
        w.close()


def z11(case):
    """规格点名请专家（开工先手）：专家在本单沙箱动手；专家若把手伸进活仓，改动被回滚并留证。"""
    def expert_overreach(world, cwd, prompt):
        write(os.path.join(world.st, "scripts", "other.py"), "o = 999\n")             # 跟踪的源码文件被改
        write(os.path.join(world.st, "scripts", "sneaky.py"), "SNEAK = 1\n")          # 活仓里新建文件
        write(os.path.join(world.st, "state", "runtime_note.json"), "{\"x\": 1}\n")    # 运行态目录：不算越界
        write(os.path.join(cwd, "scripts", "app.py"), V1)                              # 沙箱里的目标文件：分内事
        return expert_notes_only(world, cwd, prompt, done="已改 scripts/app.py：b = 20",
                                 handoff="核对验收命令，写交件回执")

    w = World(case)
    w.expert = expert_overreach
    try:
        expert_seat.wants_expert = w._orig_expert                      # 这张单的规格真的点了专家
        q, job = w.order("Z11", spec_text="# 规格\n## 驻厂外聘专家\n把 `scripts/app.py` 的 b 改成 20，"
                                          "并让 `python scripts/app.py` 退出码为 0。\n\n## 验收\n- `python scripts/app.py` 退出码 0\n")
        w.script = [Stage("worker", do=lambda world, cwd, n: "专家已改好，我补了回执"), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        roles = [c["role"] for c in w.calls]
        check("Z11 开工先手：专家先于施工出场，在沙箱里干活", roles == ["expert", "worker", "verifier"]
              and os.path.normcase(w.calls[0]["cwd"]) == os.path.normcase(os.path.join(w.home, "wt", "Z11")), str(roles))
        check("Z11 交付 done，进主干的是沙箱里的版本", rep["final_status"] == "done" and w.main_text() == V1,
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z11 专家改了活仓的跟踪源码 → 已回滚", read(os.path.join(w.st, "scripts", "other.py")) == "o = 1\n",
              repr(w.main_text("scripts/other.py")))
        check("Z11 专家在活仓新建的文件 → 已挪进证据目录", not os.path.exists(os.path.join(w.st, "scripts", "sneaky.py"))
              and read(os.path.join(w.ev_dir("Z11"), "expert_overreach", "scripts", "sneaky.py")) == "SNEAK = 1\n")
        check("Z11 活仓源码目录收场时干净", w.git(["status", "--porcelain", "--", "scripts"]).stdout == "",
              w.git(["status", "--porcelain", "--", "scripts"]).stdout[:200])
        check("Z11 运行态目录的写入不算越界（原样留着）", os.path.isfile(os.path.join(w.st, "state", "runtime_note.json")))
        ev = w.events("Z11")
        check("Z11 事件流水记了越界（给泵和人工看）", any(e["phase"] == "expert_overreach" and "sneaky.py" in e["summary"] for e in ev),
              json.dumps([e for e in ev if e["phase"].startswith("expert")], ensure_ascii=False)[:500])
        brief1 = read(os.path.join(w.ev_dir("Z11"), "work_brief_attempt1.md"))
        check("Z11 施工派工单写明专家已在沙箱先动手、不要回退", "驻厂外聘专家已在本单沙箱先动手" in brief1 and "b = 20" in brief1, brief1[-500:])
        check("Z11 收场后专家席空出来", expert_seat.active_stages(expert_seat._root()) == [])
    finally:
        w.close()


def z12(case):
    """带种重下：新一代订单的沙箱先把上一代已提交的成果并进来。
    上一代双门已过（seed_gates）→ 只补双门；没过双门 → 先施工再过双门；并不拢 → 交给工人先并再施工。"""
    def prev_branch(world, text):
        res = ww.ensure("S0", world.st)
        s0 = res["path"]
        write(os.path.join(s0, "scripts", "app.py"), text)
        world.git(["add", "-A"], cwd=s0)
        world.git(["commit", "-qm", "prev: verified work"], cwd=s0)
        return s0

    w = World(case)
    try:
        prev_branch(w, V1)
        q, job = w.order("S1", seed_from="S0", seed_gates=True)
        w.script = [Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z12 带种（上一代双门已过）：成果并得拢 → 不派施工，直接过双门 → done", rep["final_status"] == "done" and w.main_text() == V1
              and [c["role"] for c in w.calls] == ["verifier"], "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z12 事件流水记了带种", any(e["phase"] == "seed" for e in w.events("S1")),
              json.dumps([e for e in w.events("S1") if e["phase"] == "seed"], ensure_ascii=False)[:300])
        log = w.git(["log", "--format=%s", "-5"]).stdout            # 沙箱交付后已清理，带种提交随合入进了主干历史
        check("Z12 有一笔带种并入提交（随交付进了主干历史）", any(ln.startswith("seed:") for ln in log.splitlines()), log[:300])
    finally:
        w.close()

    w = World(case + "-g")
    try:
        prev_branch(w, V1)
        q, job = w.order("S1", seed_from="S0")            # 没给 seed_gates = 上一代没过双门
        w.script = [Stage("worker", do=lambda world, cwd, n: "照上代质检结论，在带进来的成果上补完了"),
                    Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z12b 带种（上一代没过双门）→ 先施工、再过双门 → done（不白跑一轮双门把同样的缺陷再确认一遍）",
              rep["final_status"] == "done" and [c["role"] for c in w.calls] == ["worker", "verifier"] and w.main_text() == V1,
              "%s | %s | %s" % (rep["final_status"], rep["final_summary"][:200], [c["role"] for c in w.calls]))
        brief1 = read(os.path.join(w.ev_dir("S1"), "work_brief_attempt1.md"))
        check("Z12b 施工派工单写明上一代成果已在沙箱、不要推翻重做",
              "上一代成果已带进沙箱" in brief1 and "wt/S0" in brief1 and "不要推翻重做" in brief1, brief1[-700:])
    finally:
        w.close()

    w = World(case + "-c")
    try:
        prev_branch(w, V1)
        write(os.path.join(w.st, "scripts", "app.py"), "a = 1\nb = 33\nc = 3\nd = 4\ne = 5\n")   # 主干在同一行上改过了
        w.git(["add", "-A"])
        w.git(["commit", "-qm", "main moved on"])

        def resolve_seed(world, cwd, n):
            world.git(["merge", "wt/S0", "-q"], cwd=cwd, must=False)       # 冲突
            write(os.path.join(cwd, "scripts", "app.py"), "a = 1\nb = 33\nc = 3\nd = 4\ne = 5\n# resolved\n")
            world.git(["add", "-A"], cwd=cwd)
            world.git(["commit", "-qm", "resolve seed conflict"], cwd=cwd)
            return "已把上一代成果并进来，冲突按主干为准解决"

        q, job = w.order("S1", seed_from="S0")
        w.script = [Stage("worker", do=resolve_seed), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z12c 带种有冲突 → 沙箱保持干净主干，交给工人解决 → done", rep["final_status"] == "done"
              and w.main_text() == "a = 1\nb = 33\nc = 3\nd = 4\ne = 5\n# resolved\n",
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        brief1 = read(os.path.join(w.ev_dir("S1"), "work_brief_attempt1.md"))
        check("Z12c 派工单点明上一代成果在哪、冲突文件是什么、先并再施工",
              "上一代成果需要你先并进沙箱" in brief1 and "wt/S0" in brief1 and "scripts/app.py" in brief1
              and "git merge wt/S0" in brief1, brief1[-600:])
        seed_ev = [e for e in w.events("S1") if e["phase"] == "seed"]
        check("Z12c 事件流水如实记了冲突", seed_ev and "冲突" in seed_ev[0]["summary"], json.dumps(seed_ev, ensure_ascii=False)[:300])
        left = w.git(["worktree", "list"]).stdout
        check("Z12c 交付后沙箱按常规清理（工作区列表里没有残留）", "wt/S1" not in left and "wt/S0" in left, left[:300])
    finally:
        w.close()


def z7(case):
    w = World(case)
    try:
        q, job = w.order("Z7")
        w.script = [Stage("worker", do=lambda world, cwd, n: "我看过了，不需要改"), Stage("worker", do=lambda world, cwd, n: "还是不需要改")]
        rep, qd = w.run(q, job)
        check("Z7 工人零改动 → 轮次用尽 failed", rep["final_status"] == "failed" and rep["attempts_used"] == 2
              and qd["history"][-1]["status"] == "failed", "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z7 原因如实写明是 Gate1 判零改动", "[Gate1 机检 FAIL] 声明目标零改动" in rep["final_summary"], rep["final_summary"][:300])
        check("Z7 零改动没浪费质检（只调了工人）", not [c for c in w.calls if c["role"] == "verifier"])
        check("Z7 主干没被改动", w.main_text() == APP0)
        check("Z7 泵拿到的去向是打回重派", qd["history"][-1].get("disposition") == "未实现打回"
              and "零改动" in str(qd["history"][-1].get("result_reason")), str(qd["history"][-1].get("disposition")))
    finally:
        w.close()


def z8(case):
    """工人没正常收尾（超时 / 卡死）：这一轮不验收不合入，下一轮在沙箱里接着做。"""
    w = World(case)
    try:
        q, job = w.order("Z8")
        w.script = [Stage("worker", do=lambda world, cwd, n: "（做到一半超时，没交件）", timed_out=True, stall_timeout=True),
                    Stage("worker", do=deliver), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        ev = [e["phase"] for e in w.events("Z8")]
        check("Z8 第一轮超时 → 第二轮接着做完 → done", rep["final_status"] == "done" and rep["attempts_used"] == 2
              and qd["history"][-1]["disposition"] == "完全实现", "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z8 超时那一轮没进机检也没进质检", "builder_incomplete" in ev and ev.count("gate1_probe") == 1
              and len([c for c in w.calls if c["role"] == "verifier"]) == 1, str(ev))
        brief2 = read(os.path.join(w.ev_dir("Z8"), "work_brief_attempt2.md"))
        check("Z8 第二轮派工单说明上一轮没正常收尾、要接着做", "未正常结束" in brief2 and "继续完成" in brief2, brief2[-400:])
    finally:
        w.close()
    w = World(case + "-b")
    try:
        q, job = w.order("Z8b")
        w.script = [Stage("worker", do=lambda world, cwd, n: "超时", timed_out=True)] * 2
        rep, qd = w.run(q, job)
        check("Z8b 轮轮超时 → failed，原因写明是没正常收尾", rep["final_status"] == "failed" and "未正常结束" in rep["final_summary"]
              and qd["history"][-1].get("disposition") == "未实现打回", "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z8b 没有质检白跑", not [c for c in w.calls if c["role"] == "verifier"])
    finally:
        w.close()


def _kernel_fix(world, cwd, n, text="X = 2\n"):
    write(os.path.join(cwd, "scripts", "factory_runner.py"), text)
    world.git(["add", "-A"], cwd=cwd)
    world.git(["commit", "-qm", "kernel tweak"], cwd=cwd)
    return "已改内核"


def _mirrors_left(world):
    return [n for n in os.listdir(world.home) if n.startswith(fr._REGRESS_MIRROR_PREFIX)]


def z9(case):
    """改到工厂内核文件：合入前在沙箱镜像里跑工厂自己的回归套件，不许带进新失败。"""
    w = World(case, kernel_repo=True)
    try:
        q, job = w.order("Z9", targets=("scripts/factory_runner.py",))
        w.script = [Stage("worker", do=_kernel_fix), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        _cp = os.path.join(w.ev_dir("Z9"), "runner_a1", "code_probe.json")
        kt = (json.load(open(_cp, encoding="utf-8")).get("strict_checks") or {}).get("kernel_tests") if os.path.isfile(_cp) else None
        check("Z9 改内核、回归全绿 → 交付 done", rep["final_status"] == "done"
              and read(os.path.join(w.st, "scripts", "factory_runner.py")) == "X = 2\n", "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z9 回归镜像用完即删（活仓旁边不留目录）", not _mirrors_left(w), str(_mirrors_left(w)))
        check("Z9 机检记录里留了回归套件的结果（过了几个、红没红）", isinstance(kt, dict) and kt.get("ran") is True
              and kt.get("ok") is True and kt.get("total") == 3, json.dumps(kt, ensure_ascii=False)[:300])
    finally:
        w.close()

    w = World(case + "-b", kernel_repo=True)
    try:
        q, job = w.order("Z9b", targets=("scripts/factory_runner.py",))

        def broke_then_fix(world, cwd, n):
            return _kernel_fix(world, cwd, n, "X = 2  # BROKEN\n" if n == 0 else "X = 2\n")
        w.script = [Stage("worker", do=broke_then_fix), Stage("worker", do=broke_then_fix), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        ev = [e["phase"] for e in w.events("Z9b")]
        check("Z9b 改内核带进新失败 → 第一轮机检拦下、不进质检；改好再过 → done", rep["final_status"] == "done"
              and rep["attempts_used"] == 2 and len([c for c in w.calls if c["role"] == "verifier"]) == 1,
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        brief2 = read(os.path.join(w.ev_dir("Z9b"), "work_brief_attempt2.md"))
        check("Z9b 回炉派工单点名是新带进来的失败用例", "内核回归未全绿" in brief2 and "k_2" in brief2, brief2[-400:])
        check("Z9b 事件流水：两轮机检、一轮质检", ev.count("gate1_probe") == 2 and ev.count("verifier_start") == 1, str(ev))
        check("Z9b 回归镜像用完即删", not _mirrors_left(w), str(_mirrors_left(w)))
    finally:
        w.close()

    w = World(case + "-c", kernel_repo=True)
    try:
        write(os.path.join(w.st, "state", "PRE_EXISTING"), "x\n")        # 主干上本来就有一个红用例
        w.git(["add", "-A"])
        w.git(["commit", "-qm", "pre-existing red"])
        q, job = w.order("Z9c", targets=("scripts/factory_runner.py",))
        w.script = [Stage("worker", do=_kernel_fix), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z9c 主干上本来就红的用例不算在工人头上 → 照常交付", rep["final_status"] == "done"
              and qd["history"][-1]["disposition"] == "完全实现", "%s | %s" % (rep["final_status"], rep["final_summary"][:300]))
    finally:
        w.close()

    w = World(case + "-d", kernel_repo=True)
    try:
        os.remove(os.path.join(w.st, "scripts", "run_tests.py"))          # 仓里没有回归套件
        w.git(["add", "-A"])
        w.git(["commit", "-qm", "no suite"])
        q, job = w.order("Z9d", targets=("scripts/factory_runner.py",))
        w.script = [Stage("worker", do=_kernel_fix), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z9d 仓里没有回归套件就不硬套内核闸（不把好单判成工厂故障）", rep["final_status"] == "done"
              and qd["history"][-1]["disposition"] == "完全实现", "%s | %s" % (rep["final_status"], rep["final_summary"][:300]))
    finally:
        w.close()


# 干跑用的第三版假回归套件：新口径（factory_runner.py 里带 BROKEN）下，还按旧口径写的
# test_lane_status（含 STRICT）会红，直到用例同步。输出格式与工厂 scripts/run_tests.py --json 一致，失败用例带断点行。
SUITE_V3 = r"""# -*- coding: utf-8 -*-
import json
import os

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(here, "scripts", "factory_runner.py"), encoding="utf-8").read()
tst = open(os.path.join(here, "scripts", "test_lane_status.py"), encoding="utf-8").read()
row = {"case": "test_lane_status", "status": "PASS"}
if "BROKEN" in src and "STRICT" in tst:
    row.update(status="FAIL", fails=["[FAIL] L6 未归属单独列出且计入会话总数 ({'used': 0, 'pairs': []}, 9)"])
print(json.dumps({"cases": [row, {"case": "k_1", "status": "PASS"}]}))
"""


def _v3_world(case):
    """内核仓 + 一套会因口径变化而红的回归用例（主干上：旧口径、用例是旧的 STRICT）。"""
    w = World(case, kernel_repo=True)
    write(os.path.join(w.st, "scripts", "run_tests.py"), SUITE_V3)
    write(os.path.join(w.st, "scripts", "test_lane_status.py"), "STRICT = 1\n")
    w.git(["add", "-A"])
    w.git(["commit", "-qm", "suite v3"])
    return w


def _new_rule(world, cwd, n):
    return _kernel_fix(world, cwd, n, "X = 2  # BROKEN\n")          # 工人把口径改了（声明目标：内核文件）


def _new_rule_again(world, cwd, n):
    """第二轮还是只改口径、没同步用例（换个数字，保证有新提交）。"""
    return _kernel_fix(world, cwd, n, "X = 3  # BROKEN\n")


def _sync_test(world, cwd, n):
    """工人同步用例（声明目标之外、硬闸点名的那个文件）。"""
    write(os.path.join(cwd, "scripts", "test_lane_status.py"), "TOLERANT = 1  # 同步到新口径\n")
    world.git(["add", "-A"], cwd=cwd)
    world.git(["commit", "-qm", "sync test to new rule"], cwd=cwd)
    return "已同步用例"


def z13(case):
    """硬闸把话说清楚 + 回归用例随口径同步（2026-11-26 G47R4：立卡的单被旧口径用例拦住，派工单又禁止它碰用例）。"""
    w = _v3_world(case)
    try:
        q, job = w.order("Z13", targets=("scripts/factory_runner.py",), max_attempts=2)
        w.script = [Stage("worker", do=_new_rule), Stage("worker", do=_sync_test), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        ev = [e["phase"] for e in w.events("Z13")]
        check("Z13 新口径被旧用例拦下 → 同步用例后过门 → done", rep["final_status"] == "done" and rep["attempts_used"] == 2
              and qd["history"][-1]["disposition"] == "完全实现", "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        p1 = json.load(open(os.path.join(w.ev_dir("Z13"), "runner_a1", "code_probe.json"), encoding="utf-8"))
        kt = (p1.get("strict_checks") or {}).get("kernel_tests") or {}
        check("Z13 机检记录里带着断点明细",
              kt.get("new_failed") == ["test_lane_status"]
              and "L6 未归属单独列出且计入会话总数" in str((kt.get("detail") or {}).get("test_lane_status")),
              json.dumps(kt, ensure_ascii=False)[:300])
        brief1 = read(os.path.join(w.ev_dir("Z13"), "work_brief_attempt1.md"))
        check("Z13 第一轮派工单没有例外（那时硬闸还没点名）", "例外：上一轮机检点名" not in brief1, "")
        brief2 = read(os.path.join(w.ev_dir("Z13"), "work_brief_attempt2.md"))
        check("Z13 回炉派工单：断点明细 + 点名允许同步的用例文件",
              "断点 `test_lane_status`：[FAIL] L6 未归属单独列出且计入会话总数" in brief2
              and "[回归用例同步]" in brief2 and "`scripts/test_lane_status.py`" in brief2
              and "仅此豁免越界" in brief2, brief2[brief2.find("回归用例同步") - 260:][:700])
        check("Z13 回炉派工单：「严禁越界」留了出口", "例外：上一轮机检点名的回归用例文件可以同步修改" in brief2, "")
        check("Z13 派给工人的话也点明例外",
              "上一轮机检点名的回归用例文件按派工单「回归用例同步」一条处理" in w.calls[1]["prompt"], w.calls[1]["prompt"][-220:])
        p2 = json.load(open(os.path.join(w.ev_dir("Z13"), "runner_a2", "code_probe.json"), encoding="utf-8"))
        ch = p2.get("checks") or {}
        check("Z13 同步的用例不算越界、单列给质检",
              [t["path"] for t in ch.get("tests_synced") or []] == ["scripts/test_lane_status.py"]
              and ch.get("files_outside_declared") == [], json.dumps(ch, ensure_ascii=False)[:300])
        vb = read(os.path.join(w.ev_dir("Z13"), "verify_brief_attempt2.md"))
        check("Z13 质检考卷单列一段请它专门核对用例改动",
              "本轮同步了内核回归用例（请专门核对）" in vb and "没有依据地放宽断言 → P0" in vb, "")
        check("Z13 合入后主干：新口径 + 同步后的用例",
              read(os.path.join(w.st, "scripts", "factory_runner.py")) == "X = 2  # BROKEN\n"
              and read(os.path.join(w.st, "scripts", "test_lane_status.py")).startswith("TOLERANT"), "")
        check("Z13 事件流水：两轮机检、一轮质检、合入", ev.count("gate1_probe") == 2 and ev.count("verifier_start") == 1
              and "merged" in ev, str(ev))
        check("Z13 没请专家（厂内两轮就解决了）", not [c for c in w.calls if c["role"] == "expert"], str([c["role"] for c in w.calls]))
    finally:
        w.close()

    # 轮次耗光仍没过内核回归 → 请专家救一轮，专家也拿到点名的用例文件
    w = _v3_world(case + "-b")
    try:
        q, job = w.order("Z13b", targets=("scripts/factory_runner.py",), max_attempts=2)
        w.script = [Stage("worker", do=_new_rule), Stage("worker", do=_new_rule_again), Stage("worker", do=_sync_test),
                    Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        ev = [e["phase"] for e in w.events("Z13b")]
        ex = [c for c in w.calls if c["role"] == "expert"]
        check("Z13b 轮次用尽仍被硬闸拦着 → 请专家救一轮 → 同步用例后 done",
              rep["final_status"] == "done" and len(ex) == 1 and "expert_rescue" in ev,
              "%s | %s | %s" % (rep["final_status"], rep["final_summary"][:200], ev))
        check("Z13b 出场顺序：两轮施工 → 专家 → 施工 → 质检", [c["role"] for c in w.calls] == ["worker", "worker", "expert", "worker", "verifier"],
              str([c["role"] for c in w.calls]))
        check("Z13b 专家也拿到点名的用例文件（声明目标之外仅此一个）",
              bool(ex) and "- `scripts/test_lane_status.py`" in ex[0]["prompt"] and "- `scripts/factory_runner.py`" in ex[0]["prompt"]
              and ex[0]["prompt"].count("\n- `scripts/") == 2, ex[0]["prompt"][:420] if ex else "(没有专家出场)")
        check("Z13b 专家拿到的「现在卡在哪」带断点明细和用例同步规矩",
              bool(ex) and "断点 `test_lane_status`" in ex[0]["prompt"] and "[回归用例同步]" in ex[0]["prompt"]
              and "仍没过内核回归" in ex[0]["prompt"], "")
        check("Z13b 合入后主干：新口径 + 同步后的用例",
              read(os.path.join(w.st, "scripts", "factory_runner.py")) == "X = 3  # BROKEN\n"
              and read(os.path.join(w.st, "scripts", "test_lane_status.py")).startswith("TOLERANT"), "")
    finally:
        w.close()

    # 对照：别种机检失败（零改动）耗光轮次，不请专家——那是工人没干活，不是厂内解决不了
    w = World(case + "-c", kernel_repo=True)
    try:
        q, job = w.order("Z13c", targets=("scripts/factory_runner.py",), max_attempts=2)
        w.script = [Stage("worker", do=lambda world, cwd, n: "看过了，不用改")] * 2
        rep, qd = w.run(q, job)
        check("Z13c 零改动耗光轮次 → 照旧 failed、不请专家", rep["final_status"] == "failed"
              and not [c for c in w.calls if c["role"] == "expert"], "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
    finally:
        w.close()


def z14(case):
    """工人自己把活提交了、沙箱里只留下没跟踪的运行态文件：合入照常。
    2026-11-26 凌晨 G44R4 / G44R5：质检过了却卡在合入——git 说的是「nothing added to commit but untracked files present」。"""
    w = World(case)
    try:
        q, job = w.order("Z14")
        w.script = [Stage("worker", do=deliver_dirty), Stage("verifier", text=PASS_JSON)]
        rep, qd = w.run(q, job)
        check("Z14 工人自己提交过、沙箱只剩没跟踪的运行态文件 → 照常合入 → done", rep["final_status"] == "done",
              "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        check("Z14 主干是工人提交的那一版", w.main_text() == V1, repr(w.main_text()))
        ev = [e["phase"] for e in w.events("Z14")]
        check("Z14 没走「合入基础设施返工」", "merged" in ev and not any(p in ("merge_failed", "merge_requeue") for p in ev), str(ev))
        wk = [c for c in w.calls if c["role"] == "worker"]
        check("Z14 工人只被派了一次", len(wk) == 1, "worker calls=%s" % len(wk))
        check("Z14 运行态文件没进主干", not os.path.exists(os.path.join(w.st, "state", "tmp")))
    finally:
        w.close()


def z15(case):
    """本单的验收脚本被工人顺手改了：质检考卷事前单列、收尾照旧记标注；没改验收脚本的单不添这一节。
    2026-11-26 审计：V5.0 的「验收脚本落在改动范围内不当作验收」此前只在收尾贴一句标注，质检事前并不知道。"""
    head = "### 本单的验收脚本本轮被改了（请专门核对）"
    for tag, touched in (("z15a", True), ("z15b", False)):
        w = World(os.path.join(case, tag))
        try:
            write(os.path.join(w.st, "scripts", "check.py"), "print('base')\n")
            w.git(["add", "-A"])
            w.git(["commit", "-qm", "check"])
            targets = ["scripts/app.py", "scripts/check.py"] if touched else ["scripts/app.py"]
            q, job = w.order("Z15", targets=targets, test_cmd="python -X utf8 scripts/check.py")

            def worker(world, cwd, n, _touched=touched):
                out = deliver(world, cwd, n)
                if _touched:
                    write(os.path.join(cwd, "scripts", "check.py"), "print('patched')\n")
                    world.git(["add", "-A"], cwd=cwd)
                    world.git(["commit", "-qm", "builder: check"], cwd=cwd)
                return out

            w.script = [Stage("worker", do=worker), Stage("verifier", text=PASS_JSON)]
            rep, qd = w.run(q, job)
            vs = [c for c in w.calls if c["role"] == "verifier"]
            vp = (vs[-1]["prompt_desktop"] or vs[-1]["prompt"]) if vs else ""
            if touched:
                check("Z15 验收脚本被工人改了 → 质检考卷事前单列并点名文件", head in vp and "`scripts/check.py`" in vp,
                      "%s | %s" % (rep["final_status"], vp[max(0, vp.find(head) - 5):vp.find(head) + 220] if head in vp else vp[-200:]))
                row = next((r for r in (qd.get("history") or []) + (qd.get("jobs") or []) if r.get("order_id") == "Z15"), {})
                check("Z15 收尾照旧记 script_trust 标注", "验收脚本" in str(row.get("script_trust") or ""),
                      str(row.get("script_trust"))[:120])
            check("Z15 %s → 单子照常走完" % ("验收脚本被改" if touched else "验收脚本没被改，质检考卷不添这一节"),
                  rep["final_status"] == "done" and (head in vp) is touched,
                  "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
        finally:
            w.close()


Z16_SPEC = ("# 规格\n把 `scripts/app.py` 的 b 改成 20。\n\n## 验收\n- `python scripts/app.py` 退出码 0\n\n"
            "```assertions\n[{\"id\": \"Z16-1\", \"clause\": \"F1\", \"desc\": \"标题在\", \"metric\": \"text\", \"selector\": \"#t\", "
            "\"op\": \"contains\", \"value\": \"demo\", \"viewports\": [390]}]\n```\n")
Z16_PROSE = ("# 规格\n把 `scripts/app.py` 的 b 改成 20。\n\n## 验收\n- `python scripts/app.py` 退出码 0\n\n"
             "```assertions\n- 页面标题在\n- 控制台零报错\n```\n")      # 历史上的 code 考卷多半这么写：断言块是散文清单
Z16_FAIL_LINE = "[断言 FAIL · Z16-1/F1] 标题在 @ 390 状态=default：实测 —，期望 contains demo"
Z16_PAGE_FAIL = "存在 WCAG 对比度不足元素 (实测最低 4.43 < 4.5:1): [{'sel': '.x'}]"      # 页面通检的毛病（生产页本来就有）
Z16_ENV = {"all_passed": False, "failures": ["[ENV] Edge CDP 3次启动均未就绪（环境故障，非工件缺陷）stderr=x"],
           "screenshots": [], "env_fail": True}


def _z16_ui(ok):
    """一条断言的真引擎判定结果（结构同 evaluate_assertions 的产出）。"""
    summ = {"total": 1, "pass": int(ok), "fail": int(not ok), "unverified": 0, "required_unverified": []}
    return {"all_passed": ok, "checks": {"http_200_auth": True}, "failures": [] if ok else [Z16_FAIL_LINE],
            "screenshots": [{"file": os.path.join(tempfile.gettempdir(), "z16_390.png"), "ok": True}],
            "assertions": [{"id": "Z16-1", "clause": "F1", "desc": "标题在", "required": True, "state": "default", "selector": "#t",
                            "metric": "text", "op": "contains", "value": "demo", "aggregate": "first", "status": "PASS" if ok else "FAIL",
                            "per_viewport": [{"viewport": 390, "status": "PASS" if ok else "FAIL", "measured": "demo" if ok else "—",
                                              "expected": "demo"}]}],
            "assert_summary": summ}


def z16(case):
    """code 单的目标里带看板页面、考卷带断言 → 机检过了 Gate1 再上真引擎逐条判：断言不过回炉，过了才交质检；
    取证环境故障不冤枉工人；考卷没断言就不跑。2026-11-26 审计：此前只有 ui 单过真引擎，code 单零运行时证据也机械放行
    （G44R5 / G44R6）。真引擎探针本身（probe_ui_dashboard）没改，这里换成按剧本出演。"""
    real_probe, real_sleep = fr.probe_ui_dashboard, fr.time.sleep
    # (代号, 考卷断言块: json / 散文清单 / 没有, 真引擎剧本, 页面是否排在目标第一个)
    for tag, blk, steps, page_first in (("z16a", "json", ["PASS"], False), ("z16b", "json", ["FAIL", "PASS"], False),
                                        ("z16c", "json", ["ENV"], False), ("z16d", "none", ["PASS"], False),
                                        ("z16e", "json", ["PASS"], True), ("z16f", "prose", ["PASS"], False),
                                        ("z16g", "json", ["PRE"], False)):
        asserts = blk == "json"
        w = World(os.path.join(case, tag))
        try:
            write(os.path.join(w.st, "dashboard", "index.html"), "<!doctype html><title>t</title><div id='t'>demo</div>\n")
            w.git(["add", "-A"])
            w.git(["commit", "-qm", "page"])
            calls: list = []
            fr.time.sleep = lambda s: None

            def fake_probe(**kw):
                calls.append(kw)
                step = steps[min(len(calls), len(steps)) - 1]
                if step == "PRE":          # 页面通检的毛病：沙箱页和生产页（基线）都有
                    return dict(_z16_ui(True), all_passed=False, failures=[Z16_PAGE_FAIL])
                return dict(Z16_ENV) if step == "ENV" else _z16_ui(step == "PASS")
            fr.probe_ui_dashboard = fake_probe
            page, app = "dashboard/index.html", "scripts/app.py"
            q, job = w.order("Z16", targets=[page, app] if page_first else [app, page],
                             spec_text=Z16_SPEC if asserts else (Z16_PROSE if blk == "prose" else None))
            w.script = [Stage("worker", do=lambda world, cwd, n: deliver(world, cwd, n, text=V1 if n == 0 else V2))
                        for _ in range(max(1, len(steps)))] + [Stage("verifier", text=PASS_JSON)]
            rep, qd = w.run(q, job)
            workers = [c for c in w.calls if c["role"] == "worker"]
            verifiers = [c for c in w.calls if c["role"] == "verifier"]
            vp = (verifiers[-1]["prompt_desktop"] or verifiers[-1]["prompt"]) if verifiers else ""
            rt = [e.get("summary") or "" for e in w.events("Z16") if e["phase"] == "gate1_runtime"]
            brief1 = os.path.join(w.ev_dir("Z16"), "work_brief_attempt1.md")
            brief1 = read(brief1) if os.path.isfile(brief1) else ""
            n_expect = 0 if not asserts else (3 if steps == ["ENV"] else len(steps) * (2 if steps == ["PRE"] else 1))
            check("Z16 %s → 单子走完；真引擎取证 %d 次、质检 1 次" % (tag, n_expect),
                  rep["final_status"] == "done" and len(calls) == n_expect and len(verifiers) == 1,
                  "%s | calls=%s | %s" % (rep["final_status"], len(calls), rep["final_summary"][:200]))
            if asserts:
                k = calls[0]
                env_only = steps == ["ENV"]        # 取证环境故障：没有断言结果，考卷里直说「没取到」，不给空表
                check("Z16 %s 探的是沙箱里的看板页面（页面没改不算问题）；工人派工单、质检考卷都看到断言" % tag,
                      k["target_rel"] == page and k["old_sha"] == "" and set(k) == set(real_probe.__code__.co_varnames[:real_probe.__code__.co_argcount])
                      and "规格断言机检结果" in vp and ("| Z16-1 |" in vp) is not env_only and "本单机检断言" in brief1
                      and ("本轮真引擎取证没取到" in vp) is env_only
                      and rt and ("看板真引擎取证" in rt[0] or "取证环境故障" in rt[0]),
                      "%s | %s" % (str({x: k.get(x) for x in ("target_rel", "old_sha")}), vp[max(0, vp.find("规格断言机检结果")):][:200]))
                check("Z16 %s 进主干的是工人最后一版；机检结论（改了哪些文件）也交了质检" % tag,
                      w.main_text() == (V1 if len(steps) == 1 or steps == ["ENV"] else V2) and "files_changed" in vp,
                      "main=%r" % w.main_text()[:24])
            if steps == ["FAIL", "PASS"]:
                brief2 = read(os.path.join(w.ev_dir("Z16"), "work_brief_attempt2.md"))
                check("Z16 断言不过 → 不请质检，回炉时把真引擎实测 / 期望告诉工人；第二轮过了才交质检",
                      len(workers) == 2 and "[看板真引擎]" in brief2 and "Z16-1" in brief2
                      and "实测 —，期望 contains demo" in brief2 and len(rt) == 2 and "FAIL" in rt[0] and "PASS" in rt[1],
                      brief2[max(0, brief2.find("[看板真引擎]") - 40):][:320])
            if steps == ["ENV"]:
                check("Z16 取证环境故障 → 不判 FAIL、不回炉（只派了一次工人），如实交质检",
                      len(workers) == 1 and len(rt) == 1 and "取证环境故障" in rt[0] and '"env_fail": true' in vp,
                      "%s | %s" % ((rt or ["-"])[0][:160], vp[max(0, vp.find("runtime_evidence") - 5):][:220]))
            if steps == ["PRE"]:
                check("Z16 沙箱页的通检毛病生产页本来就有 → 再探一遍生产页，不算本单的：不回炉、不判 FAIL，记档交质检",
                      len(workers) == 1 and len(calls) == 2 and calls[1]["wt_repo"] == w.st and calls[1]["assertions"] is None
                      and len(rt) == 1 and "生产页本来就有" in rt[0] and '"preexisting_failures"' in vp,
                      "%s | calls=%s" % ((rt or ["-"])[0][:200], len(calls)))
            if not asserts:
                check("Z16 %s 考卷%s → 照旧不拦单、不上真引擎，工人派工单和质检考卷都没有机检断言" % (tag, "没有断言块" if blk == "none" else "的断言块是散文清单"),
                      "规格断言机检结果" not in vp and "本单机检断言" not in brief1 and not rt,
                      "rt=%s | brief=%s" % (rt, brief1[brief1.find("## 本单机检断言"):][:80]))
        finally:
            fr.probe_ui_dashboard, fr.time.sleep = real_probe, real_sleep
            w.close()


def z17(case):
    """合入了看板服务加载的模块、看板服务进程比盘上数据层还旧 → 事件「合入未生效提醒」+ 巡检 [MERGE_NOT_LIVE]；
    服务比数据层新 / 合入的东西看板服务不加载 → 不提醒。进程表按剧本出演，内核只留痕、不动服务。
    2026-11-26 实况：board_server 把 v3_data 缓存在进程里，11-25 18:49 起没重启，之后合入的抽屉修复访客看不到。"""
    for tag, tgt, started, queried in (("z17a", "scripts/mes_board.py", "2000-01-01 00:00:00", True),
                                       ("z17b", "scripts/mes_board.py", "2999-01-01 00:00:00", True),
                                       ("z17c", "scripts/other.py", "2000-01-01 00:00:00", False)):
        w = World(os.path.join(case, tag))
        try:
            write(os.path.join(w.st, "scripts", "mes_board.py"), "X = 1\n")
            w.git(["add", "-A"])
            w.git(["commit", "-qm", "data layer"])
            asked: list = []
            fr._board_server_procs = lambda root, _s=started: asked.append(root) or [dict(pid=4242, started=_s, in_repo=True)]

            def do(world, cwd, n, _tgt=tgt):
                write(os.path.join(cwd, _tgt), "X = %d\n" % (20 + n))
                world.git(["add", "-A"], cwd=cwd)
                world.git(["commit", "-qm", "builder"], cwd=cwd)
                write(os.path.join(world.ev_dir("Z17"), "FACTORY_W_DELIVERY.md"), "# 回执\nEXIT=0\n")
                return "done"
            q, job = w.order("Z17", targets=[tgt])
            w.script = [Stage("worker", do=do), Stage("verifier", text=PASS_JSON)]
            rep, qd = w.run(q, job)
            ev = [e for e in w.events("Z17") if e["phase"] == "merge_not_live"]
            patrol = os.path.join(w.st, "state", "PATROL_ALERT.txt")
            lines = [ln for ln in (read(patrol).splitlines() if os.path.isfile(patrol) else []) if "[MERGE_NOT_LIVE" in ln]
            done = rep["final_status"] == "done" and read(os.path.join(w.st, tgt)).strip() == "X = 20"
            if tag == "z17a":
                check("Z17 %s 合入了看板服务加载的模块、服务进程比盘上数据层还旧 → 合入照常成功，另记事件「合入未生效提醒」+ 巡检 [MERGE_NOT_LIVE]",
                      done and len(ev) == 1 and ev[0]["actor"] == "合入未生效提醒" and "scripts/mes_board.py" in ev[0]["summary"]
                      and started in ev[0]["summary"] and len(lines) == 1 and "Z17" in lines[0] and "内核不自己动" in lines[0]
                      and asked == [w.st],
                      "%s | ev=%s | patrol=%s | asked=%s" % (rep["final_status"], [e["summary"][:90] for e in ev], lines[:1], asked))
            else:
                why = "服务进程比数据层新" if queried else "合入的东西看板服务不加载"
                check("Z17 %s → 不提醒（%s）；进程表%s" % (tag, why, "查了 1 次" if queried else "不去查"),
                      done and not ev and not lines and asked == ([w.st] if queried else []),
                      "%s | ev=%s | patrol=%s | asked=%s" % (rep["final_status"], [e["summary"][:90] for e in ev], lines[:1], asked))
        finally:
            w.close()


Z18_WALL_S = "首屏默认可见正文 5841 字符过多 (上限 4500，文字墙未压密)"      # 沙箱页的正文墙：生产页也这么长（计数略有出入）
Z18_WALL_B = "首屏默认可见正文 5928 字符过多 (上限 4500，文字墙未压密)"      # 生产页（基线）自带的正文墙
Z18_WALL_BAD = "首屏默认可见正文 7000 字符过多 (上限 4500，文字墙未压密)"    # 本单把正文墙做得比生产页更差


def z18(case):
    """code 单的沙箱页只有生产页自带的正文墙（计数与生产页不同）→ 不判 FAIL、不回炉。2026-11-26 G57 第 3 轮实况：
    code 单那条路逐字比对「生产页本来就有」，消息里带字数永远对不上，生产页自带的正文墙全算到本单头上，白开一轮去砍首页文字。
    把正文墙做得比生产页更差 → 照判 FAIL 回炉。生产页基线结论单独记一条 gate1_baseline 事件。真引擎探针按剧本出演。"""
    real_probe, real_sleep = fr.probe_ui_dashboard, fr.time.sleep
    for tag, steps in (("z18a", ["WALL"]), ("z18b", ["WALL_BAD", "PASS"])):
        w = World(os.path.join(case, tag))
        try:
            write(os.path.join(w.st, "dashboard", "index.html"), "<!doctype html><title>t</title><div id='t'>demo</div>\n")
            w.git(["add", "-A"])
            w.git(["commit", "-qm", "page"])
            calls: list = []
            n_sbx = [0]
            fr.time.sleep = lambda s: None

            def fake_probe(**kw):
                calls.append(kw)
                if kw["wt_repo"] == w.st:          # 生产页（基线）：自带正文墙
                    return {"all_passed": False, "checks": {"http_200_auth": True}, "failures": [Z18_WALL_B], "screenshots": []}
                n_sbx[0] += 1
                step = steps[min(n_sbx[0], len(steps)) - 1]
                if step == "PASS":
                    return _z16_ui(True)
                return dict(_z16_ui(True), all_passed=False, failures=[Z18_WALL_S if step == "WALL" else Z18_WALL_BAD])
            fr.probe_ui_dashboard = fake_probe
            q, job = w.order("Z18", targets=["scripts/app.py", "dashboard/index.html"], spec_text=Z16_SPEC)
            w.script = [Stage("worker", do=lambda world, cwd, n: deliver(world, cwd, n, text=V1 if n == 0 else V2))
                        for _ in range(max(1, len(steps)))] + [Stage("verifier", text=PASS_JSON)]
            rep, qd = w.run(q, job)
            workers = [c for c in w.calls if c["role"] == "worker"]
            verifiers = [c for c in w.calls if c["role"] == "verifier"]
            order = [e["phase"] for e in w.events("Z18")]
            rt = [e.get("summary") or "" for e in w.events("Z18") if e["phase"] == "gate1_runtime"]
            bl = [e.get("summary") or "" for e in w.events("Z18") if e["phase"] == "gate1_baseline"]
            brief1 = os.path.join(w.ev_dir("Z18"), "work_brief_attempt1.md")
            brief1 = read(brief1) if os.path.isfile(brief1) else ""
            brief2 = os.path.join(w.ev_dir("Z18"), "work_brief_attempt2.md")
            brief2 = read(brief2) if os.path.isfile(brief2) else ""
            if tag == "z18a":
                check("Z18 沙箱页只有生产页自带的正文墙（沙箱 5841 / 生产页 5928）→ 不判 FAIL、不回炉：施工 1 次、真引擎 2 次（沙箱 + 生产页基线）、质检 1 次、合入",
                      rep["final_status"] == "done" and len(workers) == 1 and len(calls) == 2 and len(verifiers) == 1
                      and calls[1]["wt_repo"] == w.st and calls[1]["assertions"] is None and w.main_text() == V1,
                      "%s | workers=%s calls=%s verifiers=%s | %s" % (rep["final_status"], len(workers), len(calls), len(verifiers), rep["final_summary"][:200]))
                check("Z18 事件：真引擎取证 PASS（断言通过 1 / 失败 0）并写明生产页既有的 1 条不计本单；紧随其后单独记一条「Gate1生产页基线」",
                      len(rt) == 1 and rt[0].startswith("看板真引擎取证 PASS") and "通过 1 / 失败 0 / 未验证 0" in rt[0]
                      and "生产页本来就有的通检毛病 1 条不计本单" in rt[0]
                      and len(bl) == 1 and bl[0] == "生产页本来就有的通检毛病 1 条不计本单"
                      and order.count("gate1_runtime") == 1 and order.index("gate1_runtime") + 1 == order.index("gate1_baseline")
                      and order.index("gate1_baseline") < order.index("gate1_probe"),
                      "%s | %s | %s" % (rt, bl, order[:14]))
                check("Z18 没有回炉：不存在第 2 轮派工单，第 1 轮派工单里也没把正文墙当缺陷派给工人",
                      not brief2 and "首屏默认可见正文" not in brief1, brief1[brief1.find("退回缺陷"):][:220])
            else:
                check("Z18 本单把正文墙做得比生产页更差（7000 > 5928 + 500）→ 判 FAIL 回炉并在派工单点名；第二轮过了才交质检、合入",
                      rep["final_status"] == "done" and len(workers) == 2 and len(verifiers) == 1 and len(calls) == 3
                      and len(rt) == 2 and rt[0].startswith("看板真引擎取证 FAIL") and rt[1].startswith("看板真引擎取证 PASS")
                      and "[看板真引擎] 首屏默认可见正文 7000 字符过多" in brief2 and not bl and w.main_text() == V2,
                      "%s | workers=%s calls=%s rt=%s bl=%s | %s" % (rep["final_status"], len(workers), len(calls), [r[:60] for r in rt], bl, brief2[:200]))
        finally:
            fr.probe_ui_dashboard, fr.time.sleep = real_probe, real_sleep
            w.close()


def z19(case):
    """直连择道（2026-11-26 换线 A/B）：白天施工隔单交替「STEP 先 / R4 先」，臂记在登记文件，择道结果进事件流水。"""
    w = World(case)
    real_dt = fr._dt
    try:
        arms, firsts = [], []
        for i, oid in enumerate(("Z19a", "Z19b")):
            q, job = w.order(oid, targets=("scripts/app.py", "scripts/other.py", "docs/readme.md"))     # 三个目标：不算「小单」
            w.script = [Stage("worker", do=(deliver if i == 0 else (lambda world, cwd, n: deliver(world, cwd, n, text=V2)))),
                        Stage("verifier", text=PASS_JSON)]
            rep, qd = w.run(q, job)
            check("Z19 第 %d 张单照常交付 done" % (i + 1), rep["final_status"] == "done",
                  "%s | %s" % (rep["final_status"], rep["final_summary"][:200]))
            dl = [e for e in w.events(oid) if e["phase"] == "direct_lane"]
            s = dl[0]["summary"] if dl else ""
            m = re.search(r"\bab=(\S+)", s)
            m2 = re.search(r"\bworker=([a-z0-9-]+)/", s)
            arms.append(m.group(1) if m else "")
            firsts.append(m2.group(1) if m2 else "")
            check("Z19 %s 事件流水恰有一条直连择道事件，写明臂 / 施工序 / 模型 / 质检道 / 备选序 / 班次" % oid,
                  len(dl) == 1 and bool(m) and bool(m2) and "verifier=" in s and "pool=" in s and "shift=" in s, s[:300])
        check("Z19 两张单的臂交替：一张 STEP 先、一张 R4 先", sorted(arms) == ["r4-first", "step-first"], arms)
        reg = {}
        if os.path.isfile(fr.DIRECT_AB_PATH):
            reg = json.load(open(fr.DIRECT_AB_PATH, encoding="utf-8")).get("orders") or {}
        check("Z19 臂记在临时世界的登记文件里（不写真机的 state/）",
              reg.get("Z19a") == arms[0] and reg.get("Z19b") == arms[1]
              and os.path.normcase(fr.DIRECT_AB_PATH).startswith(os.path.normcase(case)),
              "%s | %s" % (fr.DIRECT_AB_PATH, json.dumps(reg, ensure_ascii=False)[:200]))
        fr._DIRECT_AB_ARMS.clear()
        check("Z19 同一张单换进程再问也不翻臂", fr._direct_ab_arm("Z19a") == arms[0] and fr._direct_ab_arm("Z19b") == arms[1],
              "%s / %s" % (arms[0], arms[1]))
        hour = real_dt.datetime.now().hour
        night = hour >= 22 or hour < 9
        check("Z19 施工首道与臂一致（夜班一律 STEP 先）",
              firsts == [("step" if (night or a == "step-first") else "r4") for a in arms],
              "%s | arms=%s night=%s" % (firsts, arms, night))

        def pool_at(h, role, simple=False, ab=""):
            class _Fixed(real_dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return real_dt.datetime(2026, 11, 26, h, 0, 0)

            class _Clock:
                datetime = _Fixed

                def __getattr__(self, name):
                    return getattr(real_dt, name)

            fr._dt = _Clock()
            try:
                return fr._direct_lane_pool(role, simple=simple, ab=ab)
            finally:
                fr._dt = real_dt

        step_first = [x["lane"] for x in pool_at(15, "worker", ab="step-first")]
        r4_first = [x["lane"] for x in pool_at(15, "worker", ab="r4-first")]
        verifier = [x["lane"] for x in pool_at(15, "verifier", ab="step-first")]
        simple = [x["lane"] for x in pool_at(15, "worker", simple=True, ab="step-first")]
        night = [x["lane"] for x in pool_at(23, "worker", ab="r4-first")]
        check("Z19 白天：step 臂 STEP→R4→R4-2→NEX；r4 臂 R4→R4-2→STEP→NEX（其余顺序不动）",
              step_first == ["step", "r4", "r42", "nex"] and r4_first == ["r4", "r42", "step", "nex"], "%s | %s" % (step_first, r4_first))
        check("Z19 夜班一律 STEP 先（不看臂）；质检 R4-2 先；小单 NEX 先——都不参加 A/B",
              night[:1] == ["step"] and verifier == ["r42", "r4", "step", "nex"] and simple == ["nex", "r4", "r42", "step"],
              "%s | %s | %s" % (night, verifier, simple))
        check("Z19 STEP 道用的是 step-5-preview（不是模型表里排在前面的 step-3.7-flash）",
              pool_at(15, "worker", ab="step-first")[0]["model_id"] == "step-5-preview"
              and pool_at(23, "worker")[0]["model_id"] == "step-5-preview",
              pool_at(15, "worker", ab="step-first")[0]["model_id"])
    finally:
        fr._dt = real_dt
        w.close()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    base = tempfile.mkdtemp(prefix="v5e2e_")
    cases = [("Z1", z1), ("Z2", z2), ("Z3", z3), ("Z4", z4), ("Z5", z5), ("Z6", z6), ("Z7", z7), ("Z8", z8), ("Z9", z9),
             ("Z10", z10), ("Z11", z11), ("Z12", z12), ("Z4b", z4b), ("Z13", z13), ("Z14", z14), ("Z15", z15), ("Z16", z16), ("Z17", z17), ("Z18", z18), ("Z19", z19)]
    if args.only:
        cases = [c for c in cases if c[0] in args.only.upper().split(",")]
    try:
        for name, fn in cases:
            case = os.path.join(base, name.lower())
            os.makedirs(case, exist_ok=True)
            try:
                fn(case)
            except Exception as exc:  # noqa: BLE001 - 用例崩了也算失败，别打断后面的
                import traceback
                RESULTS.append(("FAIL", "%s 用例自身崩溃" % name, "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc()[-900:])))
        check("Z0 全程没有 os.kill(pid, 0)「探活」（Windows 上那是给同控制台的其它进程发 Ctrl+C）", not _KILL_CALLS,
              "被拦下 %d 次；第一处：%s" % (len(_KILL_CALLS), (_KILL_CALLS[0] if _KILL_CALLS else "")[:700]))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    n_pass = sum(1 for r in RESULTS if r[0] == "PASS")
    n_fail = len(RESULTS) - n_pass
    if args.json:
        print(json.dumps({"total": len(RESULTS), "passed": n_pass, "failed": n_fail,
                          "cases": [{"case": r[1], "status": r[0], "detail": r[2][:300]} for r in RESULTS]}, ensure_ascii=False))
        return 0 if n_fail == 0 else 1
    print("PASS %d/%d" % (n_pass, len(RESULTS)))
    for st, name, detail in RESULTS:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail[:600]) if (st == "FAIL" and detail) else ""))
    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
