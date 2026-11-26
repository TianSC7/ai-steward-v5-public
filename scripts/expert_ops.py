# -*- coding: utf-8 -*-
"""驻厂专家的工厂权限。读全厂、停单、放行、改派、重启看板。

不杀正在施工的进程，不关 AutoCAD，不提交。
"""
import json
import os
import pathlib
import subprocess
import sys

import expert_seat


def factory_root() -> pathlib.Path:
    return expert_seat.factory_root()


def queue_path(root) -> pathlib.Path:
    return pathlib.Path(root) / "state" / "factory_queue.json"


def load_queue(root) -> dict:
    path = queue_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"jobs": [], "history": []}
    return data if isinstance(data, dict) else {"jobs": [], "history": []}


def save_queue(root, data: dict) -> None:
    """原子落盘：先写临时文件再换名，内核或看板不会读到写了一半的队列。"""
    path = queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".expert.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class _PoolLock:
    """专家席自己的锁文件（ai_store 不可用、或队列不在它管的状态目录下时用）。"""

    def __init__(self, folder):
        self.folder = folder
        self.handle = None

    def __enter__(self):
        self.handle = expert_seat._lock(self.folder)
        return self

    def __exit__(self, *_exc):
        expert_seat._unlock(self.handle)


def _queue_lock(base: pathlib.Path):
    """跟内核同一把队列锁（ai_store.file_lock("factory_queue")）：否则专家改队列时会盖掉内核刚写的账。
    队列不在 ai_store 的状态目录下（测试里的临时仓）或 ai_store 不可用时，退回专家席自己的锁。"""
    try:
        import ai_store
        if pathlib.Path(queue_path(base)).parent == pathlib.Path(ai_store.state_dir()):
            return ai_store.file_lock("factory_queue", 30)
    except (ImportError, OSError, ValueError):
        pass
    return _PoolLock(base / "state")


def _mutate(root, change):
    base = pathlib.Path(root)
    with _queue_lock(base):
        data = load_queue(base)
        result = change(data)
        save_queue(base, data)
        return result


def _find(data, order_id):
    for row in data.get("jobs") or []:
        if isinstance(row, dict) and str(row.get("order_id") or "") == order_id:
            return row
    return None


def status(root=None) -> dict:
    base = pathlib.Path(root) if root else factory_root()
    data = load_queue(base)
    rows = []
    for row in data.get("jobs") or []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "order_id": row.get("order_id"),
            "status": row.get("status"),
            "title": str(row.get("title") or "")[:80],
            "expert_hold": row.get("expert_hold") or "",
        })
    return {"jobs": rows, "expert_max": expert_seat.pool_max(base / "state")}


def report(order_id: str, root=None) -> dict:
    base = pathlib.Path(root) if root else factory_root()
    data = load_queue(base)
    row = _find(data, order_id)
    if row is None:
        return {"ok": False, "error": "没有这张单"}
    text = ""
    path = str(row.get("report_json") or "")
    if path and os.path.isfile(path):
        try:
            body = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
            text = str(body.get("final_summary") or body.get("summary") or "")[:2000]
        except (OSError, json.JSONDecodeError):
            text = ""
    return {"ok": True, "order_id": order_id, "status": row.get("status"), "summary": text}


def hold(order_id: str, reason: str, root=None) -> dict:
    base = pathlib.Path(root) if root else factory_root()
    reason = str(reason or "").strip() or "专家停单"

    def change(data):
        row = _find(data, order_id)
        if row is None:
            return {"ok": False, "error": "没有这张单"}
        if str(row.get("status") or "") == "running":
            return {"ok": False, "error": "施工中的单不杀，等它结束再停"}
        row["expert_hold"] = reason
        return {"ok": True, "order_id": order_id, "expert_hold": reason}

    return _mutate(base, change)


def release(order_id: str, root=None) -> dict:
    base = pathlib.Path(root) if root else factory_root()

    def change(data):
        row = _find(data, order_id)
        if row is None:
            return {"ok": False, "error": "没有这张单"}
        row.pop("expert_hold", None)
        return {"ok": True, "order_id": order_id}

    return _mutate(base, change)


def enqueue_command(args, root=None) -> list:
    base = pathlib.Path(root) if root else factory_root()
    py = r"C:\Program Files\Python313\python.exe"
    return [py, "-X", "utf8", str(base / "scripts" / "factory_runner.py"), "enqueue", *list(args)]


def enqueue(args, root=None) -> dict:
    command = enqueue_command(args, root)
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "code": proc.returncode, "out": (proc.stdout or "")[-2000:], "err": (proc.stderr or "")[-1000:]}


def board_restart(root=None) -> dict:
    base = pathlib.Path(root) if root else factory_root()
    py = r"C:\Program Files\Python313\python.exe"
    script = base / "scripts" / "board_server.py"
    flags = 0x01000000 | 0x00000200 | 0x00000008
    subprocess.Popen([py, "-X", "utf8", "-u", str(script)], cwd=str(script.parent), creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "started": str(script)}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("status | report 单号 | hold 单号 原因 | release 单号 | enqueue ... | board-restart")
        return 0
    command = args[0]
    if command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if command == "report" and len(args) >= 2:
        print(json.dumps(report(args[1]), ensure_ascii=False, indent=2))
        return 0
    if command == "hold" and len(args) >= 3:
        result = hold(args[1], " ".join(args[2:]))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 2
    if command == "release" and len(args) >= 2:
        result = release(args[1])
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 2
    if command == "enqueue":
        result = enqueue(args[1:])
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 2
    if command == "board-restart":
        print(json.dumps(board_restart(), ensure_ascii=False))
        return 0
    print(json.dumps({"ok": False, "error": "没有这个命令"}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
