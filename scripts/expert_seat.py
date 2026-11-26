# -*- coding: utf-8 -*-
"""驻厂外聘专家。厂内施工解决不了的事请专家出手：在本单沙箱的声明目标里动手，活工厂只读，验收不归专家改。
专家席数量由 state/expert_pool.json 配置。"""
import json
import os
import pathlib
import time

MARKER = "驻厂外聘专家"
LEGACY = "架构专家升级"
DEFAULT_MAX = 2
MAX_CAP = 6
RUN_TIMEOUT_SEC = 900                 # 专家段一次会话的上限
LEASE_SEC = RUN_TIMEOUT_SEC + 600     # 租约要比会话活得久，否则长诊断做到一半席位被回收
NOTES_NAME = "expert_notes.md"
LIVE_NAME = "expert_live.json"
SECTIONS = ("## 卡在哪", "## 根因", "## 已动手", "## 交给施工", "## 验收异议")


def wants_expert(spec: str) -> bool:
    text = str(spec or "")
    return MARKER in text or LEGACY in text


def notes_template(stuck: str = "", cause: str = "", done: str = "", handoff: str = "", objection: str = "无") -> str:
    """专家结论的统一格式：五节齐全，施工照「交给施工」做，验收异议留给厂里裁决。"""
    return (
        "## 卡在哪\n" + (stuck or "（未写）") + "\n\n"
        "## 根因\n" + (cause or "（未写）") + "\n\n"
        "## 已动手\n" + (done or "（本次未动手，只给判断）") + "\n\n"
        "## 交给施工\n" + (handoff or "（未写）") + "\n\n"
        "## 验收异议\n" + (objection or "无") + "\n"
    )


def notes_complete(text: str) -> bool:
    """五节都在才算一份完整结论。"""
    body = str(text or "")
    return all(section in body for section in SECTIONS)


def diagnose(spec: str) -> str:
    """专家没出场（席满 / 超时）时交给施工的底稿。底稿无权改验收：验收做不到，写成异议交厂里裁决。"""
    text = str(spec or "")
    if "extra_meta" in text or ("禁止" in text and "factory_runner" in text):
        decision = "把验收与红线的冲突写成「验收异议」交厂里裁决；只在点名的文件里做最小改动，不碰内核"
        stuck = "验收要求与红线相撞，施工照哪条做都会被另一条拦下。"
        objection = "规格里的验收与红线互相矛盾，施工无法同时满足，请厂里裁决以哪条为准。"
    elif any(word in text for word in ("截断", "无法核实", "不可见", "零代码")):
        decision = "缩小改动并补上可复跑证据"
        stuck = "质检看不到改动正文或证据。"
        objection = "无"
    else:
        decision = "换一条更小的实现"
        stuck = "上一代没有把严重问题降下去。"
        objection = "无"
    return notes_template(
        stuck=stuck,
        cause="同一条路已经走完，严重问题数量没有下降；再派一次原样施工没有新信息。",
        handoff="只改本单点名的文件，按下面的决定做最小改动：\n- " + decision,
        objection=objection,
    )


def factory_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def research_prompt(spec: str, fallback: str, *, wt_path: str = "", wt_branch: str = "", targets=(), notes_path: str = "",
                    live_roots=(), context: str = "", mode: str = "opening") -> str:
    """给专家的派工单：在沙箱的声明目标里动手，活仓只读，验收不许改，五节结论写到 notes_path。"""
    root = factory_root()
    live = "、".join(str(p) for p in (live_roots or (root,)) if p) or str(root)
    tgt = "\n".join("- `%s`" % t for t in (targets or [])) or "- （本单没有声明目标——那你什么都不能改，只写判断）"
    when = "本单已经连续几轮过不了质检，厂内施工解决不了，现在请你出手。" if mode == "rescue" else "本单点名请你先出手。"
    return (
        "你是这座工厂的驻厂外聘专家。" + when + "\n\n"
        "## 你在哪、能改什么\n"
        f"- 当前目录是本单的施工沙箱：{wt_path or '（未给出）'}" + (f"（分支 {wt_branch}）" if wt_branch else "") + "\n"
        "- 你只允许修改下面这些文件（相对沙箱根目录）：\n" + tgt + "\n"
        f"- 活工厂对你**只读**：{live}。可以读它的源码、规格、状态、日志来诊断，但一个字节都不许写——"
        "写了会立刻被回滚，并记为越界。\n"
        "- 沙箱里可以读代码、跑测试、改目标文件。不要 git commit（内核会替你提交），不要 git push。\n\n"
        "## 你要做什么\n"
        "1. 找到真正的根因：读代码、复现，别猜。\n"
        "2. 能在目标文件范围内解决的，直接在沙箱里动手解决，并验证（跑相关测试/检查，贴原始输出与退出码）。\n"
        "3. 目标文件之外的问题（工厂内核、别的模块、流程本身）不要动：写清楚该由谁改、改哪里、为什么。\n"
        "4. 验收条件你无权修改。认为某条验收本身做不到或不合理，写进「验收异议」并说明理由，由厂里裁决；"
        "不许为了让验收通过去改验收、删断言或绕过检查。\n\n"
        "## 交付\n"
        f"把结论写到 `{notes_path or NOTES_NAME}`（UTF-8），五个小节都要有：\n"
        + "".join(f"- {section}\n" for section in SECTIONS)
        + "「已动手」要列出改了哪些文件、怎么验证的、原始输出。写完后用一两句话总结并结束。\n\n"
        "## 纪律\n"
        "- 不要启动常驻进程（HTTP 服务、监听器），不要阻塞等待；每条命令都要能自己退出。\n"
        "- 不要杀进程，不要动 AutoCAD，不要停单/放行/改派——那些不归你。\n"
        "- 不要把问题原样退回：能在目标里修的就修；修不了的，把「交给施工」写到施工同事照着做就能过。\n\n"
        + (f"## 现在卡在哪\n{context}\n\n" if context else "")
        + "## 订单规格\n"
        + str(spec or "")[:6000] + "\n\n"
        + (f"## 底稿（拿不出更好的判断时至少要达到它）\n{fallback}\n" if fallback else "")
    )


def _root(root=None) -> pathlib.Path:
    if root:
        return pathlib.Path(root)
    return pathlib.Path(__file__).resolve().parents[1] / "state"


def pool_settings(root=None) -> dict:
    base = _root(root)
    data = {"enabled": True, "max": DEFAULT_MAX}
    try:
        raw = json.loads((base / "expert_pool.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data.update({k: raw[k] for k in ("enabled", "max") if k in raw})
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        count = int(data.get("max"))
    except (TypeError, ValueError):
        count = DEFAULT_MAX
    data["max"] = min(MAX_CAP, max(1, count))
    data["enabled"] = bool(data.get("enabled", True))
    return data


def pool_max(root=None) -> int:
    return int(pool_settings(root)["max"])


def pool_enabled(root=None) -> bool:
    return bool(pool_settings(root)["enabled"])


def pool_note(root=None) -> str:
    count = pool_max(root)
    if not pool_enabled(root):
        return "驻厂外聘专家已关闭，疑难改用底稿，不设人工席"
    return f"驻厂外聘专家并发 {count}，只在沙箱的声明目标里动手，活仓只读。改 state/expert_pool.json 的 max。不设人工席"


def _spec_text(job: dict) -> str:
    for key in ("spec_file", "spec_path", "spec"):
        path = str(job.get(key) or "")
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    return handle.read(4000)
            except OSError:
                return ""
    return ""


def is_expert_job(job: dict) -> bool:
    title = str((job or {}).get("title") or "")
    if "架构升级" in title or "驻厂" in title:
        return True
    return wants_expert(_spec_text(job or {}))


def _alive(pid: int) -> bool:
    """进程还活着吗——只问系统，不许给对方发任何信号。

    旧写法 os.kill(pid, 0) 是 POSIX 的探活惯用语；Windows 上 signal.CTRL_C_EVENT 恰好等于 0，这一下成了
    GenerateConsoleCtrlEvent(CTRL_C, pid)：同控制台的其它进程（跑单内核、它的 CLI）各挨一次 Ctrl+C。
    2026-11-26 凌晨 G42R4 跑单进程连着四次「没正常收口」，就是工人跑内核 e2e 套件时套件探活捅出来的。"""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            handle = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                # 没权限打听（别的用户的 / 受保护的进程）：探不了就当它活着，宁可多占一会儿席，也不误回收
                return ctypes.get_last_error() == 5
            try:
                code = ctypes.c_ulong()
                return bool(k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
            finally:
                k32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _live_rows(base: pathlib.Path) -> list:
    """expert_live.json 里仍然有效的出场记录（进程还活着才算）。"""
    try:
        raw = json.loads((base / LIVE_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []
    rows = raw.get("active") if isinstance(raw, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict) and _alive(int(row.get("pid") or 0))]


def _write_live(base: pathlib.Path, rows: list) -> None:
    path = base / LIVE_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"active": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def stage_begin(order_id: str, attempt: int = 0, mode: str = "opening", title: str = "", root=None) -> None:
    """内核在专家段开始时落一笔：谁、第几轮、为什么出场、哪个进程。看板席位灯读它。"""
    base = _root(root)
    handle = _lock(base)
    try:
        mine = str(order_id or "")
        rows = [row for row in _live_rows(base) if str(row.get("order_id") or "") != mine]
        rows.append({"order_id": mine, "attempt": attempt, "mode": mode, "title": title,
                     "pid": os.getpid(), "started_at": time.time()})
        _write_live(base, rows)
    finally:
        _unlock(handle)


def stage_end(order_id: str, root=None) -> None:
    base = _root(root)
    handle = _lock(base)
    try:
        mine = str(order_id or "")
        _write_live(base, [row for row in _live_rows(base) if str(row.get("order_id") or "") != mine])
    finally:
        _unlock(handle)


def active_stages(root=None) -> list:
    """此刻真在出场的专家段。每项 {order_id, attempt, mode, title, pid, started_at}。
    主证 = expert_live.json（内核起止落笔）；旁证 = 还新鲜的租约且进程活着（内核没落 live 文件时兜底）。"""
    base = _root(root)
    out = _live_rows(base)
    seen = {str(row.get("order_id") or "") for row in out}
    for row in _fresh(_leases(base), time.time()):
        mine = str(row.get("order_id") or "")
        if mine and mine not in seen:
            seen.add(mine)
            out.append({"order_id": mine, "mode": "lease", "pid": row.get("pid"), "started_at": row.get("started_at")})
    return out


def seat_items(running, queued, now_str, root=None) -> list:
    """看板席位。真在出场 = active_stages 有记录；「单子在跑且带专家标记」只说明这张单想要专家，不算在场。"""
    count = pool_max(root)
    enabled = pool_enabled(root)
    active = active_stages(root) if enabled else []
    titles = {str(job.get("order_id") or ""): str(job.get("title") or "")
              for job in list(running or []) + list(queued or [])}
    items = []
    for index in range(count):
        row = active[index] if index < len(active) else None
        started = float((row or {}).get("started_at") or 0)
        age = max(0, int((time.time() - started) / 60)) if started else 0
        order = str(row.get("order_id") or "") if row else ""
        items.append({
            "seat": f"驻厂外聘专家 {index + 1}" if count > 1 else "驻厂外聘专家",
            "status": "active" if row else "idle",
            "order": order,
            "title": titles.get(order, "") if row else "",
            "last_beat": now_str,
            "beat_age_min": age,
            "channel": {
                "status": "ok" if enabled else "off",
                "provider": "疑难出手",
                "model": f"并发 {count}",
            },
        })
    return items


def _leases(root: pathlib.Path) -> list:
    path = root / "expert_leases.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []
    rows = raw.get("leases") if isinstance(raw, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _write_leases(root: pathlib.Path, rows: list) -> None:
    path = root / "expert_leases.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"leases": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _lock(root: pathlib.Path):
    import msvcrt
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / "expert_pool.lock", "a+b")
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b" ")
        handle.flush()
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    return handle


def _unlock(handle) -> None:
    import msvcrt
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _fresh(rows: list, now: float) -> list:
    kept = []
    for row in rows:
        started = float(row.get("started_at") or 0)
        if now - started > LEASE_SEC:
            continue
        if not _alive(int(row.get("pid") or 0)):
            continue
        kept.append(row)
    return kept


def try_acquire(order_id: str, root=None) -> bool:
    base = _root(root)
    if not pool_enabled(base):
        return False
    handle = _lock(base)
    try:
        now = time.time()
        rows = _fresh(_leases(base), now)
        mine = str(order_id or "")
        for row in rows:
            if str(row.get("order_id") or "") == mine:
                row["started_at"] = now
                row["pid"] = os.getpid()
                _write_leases(base, rows)
                return True
        if len(rows) >= pool_max(base):
            _write_leases(base, rows)
            return False
        rows.append({"order_id": mine, "pid": os.getpid(), "started_at": now})
        _write_leases(base, rows)
        return True
    finally:
        _unlock(handle)


def release(order_id: str, root=None) -> None:
    base = _root(root)
    handle = _lock(base)
    try:
        mine = str(order_id or "")
        rows = [row for row in _leases(base) if str(row.get("order_id") or "") != mine]
        _write_leases(base, rows)
    finally:
        _unlock(handle)
