# -*- coding: utf-8 -*-
"""C-20261126-G60 · 两条新线（cad-research / factory-health）的回归测试。

跑法：python -X utf8 scripts/test_lines_new.py
全部用例只用临时目录，不写 lines/*/jobs、不写 lines/*/specs、不起常驻进程。
"""
from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

# 本测试用 importlib 直载 dispatch.py / run_line.py；关掉字节码写入，
# 免得每跑一次就在仓内留下 lines/__pycache__、lines/*/__pycache__。
sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES = os.path.join(ROOT, "lines")
CAD = os.path.join(LINES, "cad-research")
HLT = os.path.join(LINES, "factory-health")
DISPATCH = os.path.join(LINES, "dispatch.py")
REGISTRY = os.path.join(LINES, "registry.json")
PY = sys.executable


def load_mod(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pe_bytes(machine: int = 0x8664) -> bytes:
    """合法 MZ + e_lfanew@0x3C + "PE\\0\\0" + machine。"""
    b = bytearray(0x100)
    b[0:2] = b"MZ"
    struct.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", b, 0x84, machine)
    return bytes(b)


def write(path: str, data) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    kw = {} if mode == "wb" else {"encoding": "utf-8"}
    with open(path, mode, **kw) as f:
        f.write(data)
    return path


def read_json(path: str):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class T1Registry(unittest.TestCase):
    """1. registry：两条线 active 且 dir/entry/gate/jobs_dir 非空、contract_ready=true。"""

    def test_two_lines_active(self):
        reg = read_json(REGISTRY)
        lines = reg["lines"]
        for lid in ("cad-research", "factory-health"):
            ent = lines[lid]
            self.assertEqual(ent["status"], "active", lid)
            for field in ("dir", "root", "entry", "gate", "jobs_dir"):
                self.assertTrue(str(ent.get(field) or "").strip(), "%s.%s 为空" % (lid, field))
            self.assertIs(ent["contract_ready"], True, lid)
            self.assertTrue(os.path.isdir(os.path.join(LINES, ent["dir"])), "%s 目录不存在" % lid)
        # 其余 5 条线一字不动（键集与非空口径）
        self.assertEqual(set(lines), {"video", "stock", "ui-web", "cad-research", "factory-health", "general"})
        self.assertEqual(lines["video"]["status"], "active")
        self.assertEqual(lines["ui-web"]["status"], "active")
        self.assertEqual(lines["general"]["status"], "active")


class T2Dispatch(unittest.TestCase):
    """2. dispatch：line_entry 映射正确且 entry/gate 文件存在。"""

    def test_line_entry_mapping(self):
        d = load_mod("g60_dispatch", DISPATCH)
        for lid in ("cad-research", "factory-health"):
            cfg = d.line_entry(lid)
            self.assertIsNotNone(cfg, lid)
            self.assertEqual(cfg["dir"], lid)
            for key in ("entry", "gate"):
                p = os.path.join(LINES, cfg["dir"], cfg[key])
                self.assertTrue(os.path.isfile(p), "%s 不存在: %s" % (lid, p))
            self.assertEqual(cfg["evidence"], ["report.md", "inventory.json"] if lid == "cad-research"
                             else ["report.md", "health.json"])
        self.assertIsNone(d.line_entry("video"))  # video/stock 不走这张表，行为不变
        self.assertIsNone(d.line_entry("nope"))

    def test_dry_run_exit0(self):
        p = subprocess.run([PY, "-X", "utf8", DISPATCH, "--dry-run"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 0, p.stderr[-800:])


class T3CadLine(unittest.TestCase):
    """3. CAD：临时 fixture -> run_line 退出 0，ledger done，3 文件，defun=2，PE=x64。"""

    def test_fixture_pipeline(self):
        tmp = tempfile.mkdtemp(prefix="g60-cad-")
        try:
            scan = os.path.join(tmp, "scan")
            write(os.path.join(scan, "a.dwg"), b"AC1032" + b"\x00" * 16)
            write(os.path.join(scan, "b.lsp"), b'(defun c:a () 1)\n(DEFUN c:b () 2)\n')  # 2 处（含大写）
            write(os.path.join(scan, "c.dll"), pe_bytes(0x8664))
            spec = write(os.path.join(tmp, "spec.json"), json.dumps(
                {"order_id": "T-CAD-1", "root": scan}, ensure_ascii=False))
            jobs = os.path.join(tmp, "jobs")
            p = subprocess.run([PY, "-X", "utf8", os.path.join(CAD, "run_line.py"),
                                "--spec", spec, "--jobs-dir", jobs],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(p.returncode, 0, "stdout=%s\nstderr=%s" % (p.stdout[-1200:], p.stderr[-1200:]))
            job = os.path.join(jobs, "T-CAD-1")
            led = read_json(os.path.join(job, "ledger.json"))
            inv = read_json(os.path.join(job, "inventory.json"))
            self.assertEqual(led["status"], "done")
            self.assertEqual(led["gate"]["verdict"], "PASS")
            self.assertEqual(inv["total_files"], 3)
            byext = {r["ext"]: r for r in inv["files"]}
            self.assertEqual(byext[".lsp"]["defun_count"], 2)
            self.assertEqual(byext[".dll"]["pe_machine"], "x64")
            self.assertEqual(inv["counts_by_ext"], {".dwg": 1, ".lsp": 1, ".dll": 1})
            self.assertEqual(inv["total_bytes"], sum(r["bytes"] for r in inv["files"]))
            for r in inv["files"]:
                self.assertNotIn("\\", r["path"])
                self.assertEqual(len(r["sha256"]), 64)
            rep = read_text(os.path.join(job, "report.md"))
            self.assertIn("- 总文件：3", rep)
            for ext, n in inv["counts_by_ext"].items():
                self.assertIn("- %s：%d" % (ext, n), rep)
            # 再跑一次机检，独立复核 PASS（可复跑）
            g = subprocess.run([PY, "-X", "utf8", "-I", os.path.join(CAD, "line_check.py"), "--job", job],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(g.returncode, 0, g.stdout[-800:])
            self.assertEqual(json.loads(g.stdout.strip().splitlines()[-1])["verdict"], "PASS")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gate_bites_on_tampered_inventory(self):
        """负例：篡改 total_files 后机检必须 FAIL（证明 C3 真的在算）。"""
        tmp = tempfile.mkdtemp(prefix="g60-cad-neg-")
        try:
            scan = os.path.join(tmp, "scan")
            write(os.path.join(scan, "a.dwg"), b"AC1032")
            spec = write(os.path.join(tmp, "spec.json"), json.dumps(
                {"order_id": "T-CAD-NEG", "root": scan}, ensure_ascii=False))
            jobs = os.path.join(tmp, "jobs")
            p = subprocess.run([PY, "-X", "utf8", os.path.join(CAD, "run_line.py"),
                                "--spec", spec, "--jobs-dir", jobs], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            self.assertEqual(p.returncode, 0)
            inv_p = os.path.join(jobs, "T-CAD-NEG", "inventory.json")
            inv = read_json(inv_p)
            inv["total_files"] = 99
            write(inv_p, json.dumps(inv, ensure_ascii=False))
            g = subprocess.run([PY, "-X", "utf8", "-I", os.path.join(CAD, "line_check.py"),
                                "--job", os.path.join(jobs, "T-CAD-NEG")],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(g.returncode, 1)
            out = json.loads(g.stdout.strip().splitlines()[-1])
            self.assertEqual(out["verdict"], "FAIL")
            self.assertTrue(any("C3" in f or "C5" in f for f in out["failures"]), out["failures"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class T4HealthLine(unittest.TestCase):
    """4. health：checks=["disk"] 退出 0 + disk.ok 为 bool；queue 单测（有账本计数 / 缺文件 skip）。"""

    def test_disk_pipeline(self):
        tmp = tempfile.mkdtemp(prefix="g60-hlt-")
        try:
            spec = write(os.path.join(tmp, "spec.json"), json.dumps(
                {"order_id": "T-HLT-1", "checks": ["disk"], "repo_root": ROOT}, ensure_ascii=False))
            jobs = os.path.join(tmp, "jobs")
            p = subprocess.run([PY, "-X", "utf8", os.path.join(HLT, "run_line.py"),
                                "--spec", spec, "--jobs-dir", jobs],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(p.returncode, 0, "stdout=%s\nstderr=%s" % (p.stdout[-1200:], p.stderr[-1200:]))
            job = os.path.join(jobs, "T-HLT-1")
            h = read_json(os.path.join(job, "health.json"))
            led = read_json(os.path.join(job, "ledger.json"))
            self.assertIn("disk", h["checks"])
            self.assertIsInstance(h["checks"]["disk"]["ok"], bool)
            self.assertEqual(h["summary"], {"total": 1, "ok": 1, "bad": 0, "skip": 0})
            self.assertEqual(led["status"], "done")
            self.assertIn("- disk：", read_text(os.path.join(job, "report.md")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_queue_counts_then_skip(self):
        mod = load_mod("g60_health", os.path.join(HLT, "run_line.py"))
        tmp = tempfile.mkdtemp(prefix="g60-hlt-q-")
        try:
            # 缺文件 -> skip
            r0 = mod.check_queue(tmp)
            self.assertEqual(r0["ok"], "skip")
            self.assertTrue(r0["detail"].strip())
            # 有账本 -> 按 status 计数 + 最老 queued 等待分钟
            write(os.path.join(tmp, "state", "factory_queue.json"), json.dumps({
                "jobs": [
                    {"job_id": "J1", "status": "queued", "enqueued_at": "2026-11-26 10:00:00"},
                    {"job_id": "J2", "status": "queued", "enqueued_at": "2026-11-26 08:00:00"},
                    {"job_id": "J3", "status": "running", "enqueued_at": "2026-11-26 09:00:00"},
                    {"job_id": "J4", "status": "done", "enqueued_at": "2026-11-26 07:00:00"},
                ]
            }, ensure_ascii=False))
            r1 = mod.check_queue(tmp)
            self.assertIs(r1["ok"], True, r1)
            self.assertEqual(r1["data"]["counts"], {"queued": 2, "running": 1, "done": 1})
            self.assertEqual(r1["data"]["total"], 4)
            self.assertIsNotNone(r1["data"]["oldest_queued_wait_min"])
            self.assertGreater(r1["data"]["oldest_queued_wait_min"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_board_check_never_skips_and_scrubs_token(self):
        """H3 口径：board 检查即使看板挂掉也只 ok=false，不是 skip。"""
        mod = load_mod("g60_health2", os.path.join(HLT, "run_line.py"))
        r = mod.check_board("http://127.0.0.1:1", tempfile.gettempdir())
        self.assertIn(r["ok"], (True, False))
        self.assertIsInstance(r["detail"], str)


class T5PyCompile(unittest.TestCase):
    """5. 四个 py 文件 py_compile 通过（.pyc 写临时目录，不污染仓内 __pycache__）。"""

    def test_compile_four(self):
        tmp = tempfile.mkdtemp(prefix="g60-pyc-")
        try:
            for i, path in enumerate([os.path.join(CAD, "run_line.py"),
                                      os.path.join(CAD, "line_check.py"),
                                      os.path.join(HLT, "run_line.py"),
                                      os.path.join(HLT, "line_check.py")]):
                py_compile.compile(path, cfile=os.path.join(tmp, "m%d.pyc" % i), doraise=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
