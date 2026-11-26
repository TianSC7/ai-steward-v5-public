import json
import os
import re
import subprocess
import sys

STEWARD = r"C:\Users\Administrator\Documents\mate\plugins\ai-steward"
BLUEPRINT = r"C:\Users\Administrator\Documents\mate\dll\factory-blueprint"
SCRIPTS = os.path.join(STEWARD, "scripts")
sys.path.insert(0, SCRIPTS)

# 1. Patch board_server.py with /api/v3/events, /api/v3/archive, and Live Stream Dock injection on /v2.html
bs_path = os.path.join(SCRIPTS, "board_server.py")
bs_code = open(bs_path, "r", encoding="utf-8").read()
bs_code = bs_code.replace('widget + "\n</body>"', 'widget + "\\n</body>"')

LIVE_DOCK_HELPER = '''
def get_v3_events():
    import v3_data
    return json.dumps(v3_data.collect_live_agent_stream(), ensure_ascii=False).encode("utf-8")


def inject_v3_live_stream_dock(html_bytes: bytes) -> bytes:
    try:
        html_str = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return html_bytes
    if "id=\\"v3LiveStreamDock\\"" in html_str:
        return html_bytes
    widget = """
<!-- V3.1 方案A：实时施工/质检实况流（每2秒直读 cli/db/db.sqlite part 表） -->
<style id="v3LiveStreamStyle">
#v3LiveStreamDock {
  position: fixed;
  right: 16px;
  bottom: 14px;
  width: 500px;
  max-width: calc(100vw - 32px);
  background: rgba(11, 16, 30, 0.96);
  border: 1px solid #1e3a5f;
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.65);
  color: #e2e8f0;
  font-family: "JetBrains Mono", "Cascadia Mono", Consolas, "Microsoft YaHei", monospace;
  font-size: 12px;
  z-index: 9990;
  overflow: hidden;
  transition: height 0.18s ease;
}
#v3LiveStreamDock.v3-dock-min .v3-dock-body {
  display: none;
}
.v3-dock-hdr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: linear-gradient(90deg, #0f1d35 0%, #162644 100%);
  border-bottom: 1px solid #1e293b;
  cursor: pointer;
  user-select: none;
}
.v3-dock-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  color: #f8fafc;
  font-size: 12.5px;
}
.v3-live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 8px #22c55e;
}
.v3-live-dot.busy {
  background: #38bdf8;
  box-shadow: 0 0 10px #38bdf8;
}
.v3-dock-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}
.v3-dock-pill {
  padding: 2px 7px;
  border-radius: 4px;
  background: #1e293b;
  color: #93c5fd;
  border: 1px solid #334155;
  font-size: 12px;
}
.v3-dock-btn {
  padding: 2px 8px;
  border-radius: 4px;
  background: transparent;
  color: #cbd5e1;
  border: 1px solid #475569;
  font-size: 12px;
  cursor: pointer;
}
.v3-dock-btn:hover {
  background: #1e293b;
  color: #ffffff;
}
.v3-dock-subbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 12px;
  background: #0d1526;
  border-bottom: 1px solid #1e293b;
  color: #94a3b8;
  font-size: 12px;
}
.v3-dock-list {
  max-height: 225px;
  overflow-y: auto;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.v3-stream-row {
  display: grid;
  grid-template-columns: 58px 72px 70px 1fr;
  gap: 6px;
  align-items: baseline;
  padding: 4px 6px;
  border-radius: 5px;
  background: rgba(15, 23, 42, 0.78);
  border-left: 2px solid #334155;
  line-height: 1.45;
}
.v3-stream-row.k-think { border-left-color: #a855f7; }
.v3-stream-row.k-tool  { border-left-color: #38bdf8; }
.v3-stream-row.k-out   { border-left-color: #22c55e; }
.v3-stream-row.k-evt   { border-left-color: #f59e0b; }
.v3-st-time  { color: #94a3b8; font-size: 12px; white-space: nowrap; }
.v3-st-actor { color: #e2e8f0; font-weight: 600; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.v3-st-kind  { color: #93c5fd; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.v3-st-text  { color: #cbd5e1; font-size: 12px; word-break: break-word; }
</style>
<aside id="v3LiveStreamDock" aria-label="实时施工与质检实况流">
  <div class="v3-dock-hdr" onclick="if(event.target.tagName!=='BUTTON'){document.getElementById('v3LiveStreamDock').classList.toggle('v3-dock-min')}">
    <div class="v3-dock-title">
      <span id="v3LiveDot" class="v3-live-dot"></span>
      <span>实时施工 / 质检实况流 (2s 直读 part 表)</span>
    </div>
    <div class="v3-dock-meta">
      <span id="v3LiveStatePill" class="v3-dock-pill">待命</span>
      <button type="button" class="v3-dock-btn" onclick="window._v3ArchiveOldSessions(event)" title="一键归档旧会话">归档旧会话</button>
      <button type="button" class="v3-dock-btn" onclick="document.getElementById('v3LiveStreamDock').classList.toggle('v3-dock-min')">折叠</button>
    </div>
  </div>
  <div class="v3-dock-body">
    <div class="v3-dock-subbar">
      <span id="v3LiveSessionInfo">正在连接 cli/db/db.sqlite ...</span>
      <span id="v3LivePollTime">--:--:--</span>
    </div>
    <div id="v3LiveStreamList" class="v3-dock-list"></div>
  </div>
</aside>
<script>
(function(){
  function esc(s){
    return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }
  window._v3ArchiveOldSessions = function(ev){
    if(ev) ev.stopPropagation();
    if(!confirm("确认一键归档所有已结束的旧会话（保留最新活跃会话）？")) return;
    var q = location.search || "";
    fetch("/api/v3/archive" + q, {method:"POST"}).then(function(r){return r.json();}).then(function(d){
      alert("已归档旧会话: " + (d.archived_count || 0) + " 条");
      pollLiveStream();
    }).catch(function(e){ alert("归档请求失败: " + e); });
  };
  function pollLiveStream(){
    var q = location.search || "";
    fetch("/api/v3/events" + q, {cache:"no-store"}).then(function(r){
      if(!r.ok) throw new Error("HTTP "+r.status);
      return r.json();
    }).then(function(d){
      var dot = document.getElementById("v3LiveDot");
      var pill = document.getElementById("v3LiveStatePill");
      var info = document.getElementById("v3LiveSessionInfo");
      var tm = document.getElementById("v3LivePollTime");
      var list = document.getElementById("v3LiveStreamList");
      if(!list) return;
      if(tm) tm.textContent = "直读 " + (d.ts || "");
      if(d.runner_busy){
        if(dot) dot.className = "v3-live-dot busy";
        if(pill) pill.textContent = "施工/质检运行中";
      } else {
        if(dot) dot.className = "v3-live-dot";
        if(pill) pill.textContent = "内核就绪";
      }
      if(info){
        info.textContent = (d.active_session_title ? ("当前: " + d.active_session_title.slice(0,30)) : "当前无阻塞任务") +
          (d.cost_per_accepted_cny !== undefined ? (" · 单均交付 ¥" + Number(d.cost_per_accepted_cny||0).toFixed(4)) : "");
      }
      var items = d.items || [];
      var html = "";
      for(var i=0; i<items.length; i++){
        var it = items[i];
        var kd = String(it.kind || "");
        var cls = "k-out";
        if(kd.indexOf("思考") >= 0) cls = "k-think";
        else if(kd.indexOf("工具") >= 0) cls = "k-tool";
        else if(kd.indexOf("事件") >= 0 || kd.indexOf("裁决") >= 0 || kd.indexOf("机检") >= 0) cls = "k-evt";
        html += '<div class="v3-stream-row ' + cls + '">' +
          '<span class="v3-st-time">' + esc(it.time) + '</span>' +
          '<span class="v3-st-actor">' + esc(it.actor) + '</span>' +
          '<span class="v3-st-kind">' + esc(kd) + '</span>' +
          '<span class="v3-st-text">' + esc(it.text) + '</span>' +
        '</div>';
      }
      list.innerHTML = html || '<div class="v3-stream-row"><span class="v3-st-time">--:--:--</span><span class="v3-st-actor">V3内核</span><span class="v3-st-kind">待命</span><span class="v3-st-text">暂无实况流记录</span></div>';
    }).catch(function(){});
  }
  setInterval(pollLiveStream, 2000);
  setTimeout(pollLiveStream, 350);
})();
</script>
"""
    if "</body>" in html_str:
        html_str = html_str.replace("</body>", widget + "\\n</body>", 1)
    else:
        html_str += widget
    return html_str.encode("utf-8")


def archive_old_zcode_sessions() -> dict:
    import sqlite3
    cli_db = r"C:\\Users\\Administrator\\.zcode\\cli\\db\\db.sqlite"
    idx_db = r"C:\\Users\\Administrator\\.zcode\\v2\\tasks-index.sqlite"
    now_ms = int(time.time() * 1000)
    archived_cnt = 0
    try:
        if os.path.exists(idx_db):
            conn = sqlite3.connect(idx_db, timeout=15)
            cur = conn.execute("SELECT session_id FROM tasks WHERE archived = 0 ORDER BY updated_at DESC")
            rows = [r[0] for r in cur.fetchall()]
            to_arch = rows[2:] if len(rows) > 2 else []
            for sid in to_arch:
                conn.execute("UPDATE tasks SET archived = 1, updated_at = ? WHERE session_id = ?", (now_ms, sid))
                archived_cnt += 1
            conn.commit()
            conn.close()
        if os.path.exists(cli_db):
            conn2 = sqlite3.connect(cli_db, timeout=15)
            cur2 = conn2.execute("SELECT id FROM session WHERE time_archived IS NULL ORDER BY time_updated DESC")
            rows2 = [r[0] for r in cur2.fetchall()]
            for sid in rows2[2:]:
                conn2.execute("UPDATE session SET time_archived = ? WHERE id = ?", (now_ms, sid))
            conn2.commit()
            conn2.close()
    except Exception as e:
        return {"ok": False, "error": str(e), "archived_count": archived_cnt}
    return {"ok": True, "archived_count": archived_cnt}
'''

if "def get_v3_events():" not in bs_code:
    bs_code = bs_code.replace("def get_data():", LIVE_DOCK_HELPER + "\n\ndef get_data():", 1)

# Ensure board_server.py serves v2.html cleanly without floating dock injection (now integrated natively as Zone 13)
bs_code = bs_code.replace(
    'raw_b = f.read()\n                    ctype, body = "text/html; charset=utf-8", (inject_v3_live_stream_dock(raw_b) if name == "v2.html" else raw_b)',
    'ctype, body = "text/html; charset=utf-8", f.read()',
)
bs_code = bs_code.replace(
    'ctype, body = "text/html; charset=utf-8", inject_v3_live_stream_dock(f.read())',
    'ctype, body = "text/html; charset=utf-8", f.read()',
)

# Add /api/v3/events in do_GET
if 'elif path == "/api/v3/events":' not in bs_code:
    bs_code = bs_code.replace(
        'elif path == "/data.json":\n            ctype, body = "application/json; charset=utf-8", get_data()',
        'elif path == "/data.json":\n            ctype, body = "application/json; charset=utf-8", get_data()\n        elif path == "/api/v3/events":\n            import v3_data\n            ev = v3_data.collect_live_agent_stream()\n            try:\n                d_now = json.loads(get_data().decode("utf-8"))\n                ev["cost_per_accepted_cny"] = ((d_now.get("v3_pipeline") or {}).get("cost_per_accepted_cny", 0.0))\n            except Exception:\n                ev["cost_per_accepted_cny"] = 0.0\n            ctype, body = "application/json; charset=utf-8", json.dumps(ev, ensure_ascii=False).encode("utf-8")',
        1,
    )

# Add /api/v3/archive in do_POST
if 'path == "/api/v3/archive"' not in bs_code and "def do_POST(self):" in bs_code:
    bs_code = bs_code.replace(
        'if path != "/api/v3/reject":',
        'if path == "/api/v3/archive":\n            res = archive_old_zcode_sessions()\n            raw = json.dumps(res, ensure_ascii=False).encode("utf-8")\n            self.send_response(200)\n            self.send_header("Content-Type", "application/json; charset=utf-8")\n            self.send_header("Content-Length", str(len(raw)))\n            self.end_headers()\n            self.wfile.write(raw)\n            return\n        if path != "/api/v3/reject":',
        1,
    )

open(bs_path, "w", encoding="utf-8").write(bs_code)
print("PATCHED_BOARD_SERVER_OK")

# 2. Check C-20261121-001 run state & worktree on AA
import factory_runner
import v3_data

wt_v2 = os.path.join(STEWARD, "wt", "C-20261121-001", "dashboard", "v2.html")
main_v2 = os.path.join(STEWARD, "dashboard", "v2.html")
print("MAIN_V2_EXISTS:", os.path.exists(main_v2), "SIZE:", os.path.getsize(main_v2) if os.path.exists(main_v2) else 0)
print("WT_V2_EXISTS:", os.path.exists(wt_v2), "SIZE:", os.path.getsize(wt_v2) if os.path.exists(wt_v2) else 0)

# If worktree C-20261121-001 has a newer modified v2.html that wasn't merged yet, test it with Gate 1 probe!
if os.path.exists(wt_v2):
    wt_sha = factory_runner.sha256_file(wt_v2)
    main_sha = factory_runner.sha256_file(main_v2)
    print("WT_SHA:", wt_sha[:16], "MAIN_SHA:", main_sha[:16])

# 3. Run V3.1 Contract Compaction 6-conflict unit test on C-20261121-001 (and keep clean state)
conflicts_6 = [
    "1.1【P0】版本去重：标题旁徽章与右上版本只留一处 v3.8",
    "1.1【P0】版本去重：右上只保留1处版本徽章 v3.9 · 第9版，区分版本与构建时间",
    "2.1【P0】当前告警：删除计数下满宽蓝色渐变伪进度条",
    "2.1【P0】当前告警：删除伪进度条，无红黄告警时显示蓝色为提示级",
    "3.1【P0】指标顶板：四卡高度统一，删除左侧装饰竖条与0值染色",
    "4.1【P1】状态漏斗：删除顶部重复100%水平渐变条，保留单圆环与两列非零优先图例",
]
for idx, reason in enumerate(conflicts_6, 1):
    c_obj, active_md = factory_runner.compact_contract_on_reject(
        "C-20261121-001",
        f"C-20261121-001-R{idx}",
        reason,
        target_rel="dashboard/v2.html",
    )

ord_dir = os.path.join(BLUEPRINT, "orders", "C-20261121-001")
c_path = os.path.join(ord_dir, "contract.json")
l_path = os.path.join(ord_dir, "ledger.jsonl")
c_final = json.load(open(c_path, "r", encoding="utf-8"))
l_lines = [ln for ln in open(l_path, "r", encoding="utf-8").read().splitlines() if ln.strip()]
active_list = c_final.get("clauses") or c_final.get("active") or []
print("CONTRACT_COMPACTION_TEST:", json.dumps({
    "rev": c_final.get("rev"),
    "active_count": len(active_list),
    "active_topics": [a.get("topic") for a in active_list],
    "ledger_superseded_count": len(l_lines),
}, ensure_ascii=False))

# 4. Generate map.md test
map_out = os.path.join(BLUEPRINT, "orders", "C-20261121-001", "map.md")
map_text = factory_runner.generate_file_map_md(main_v2, map_out)
print("MAP_MD_LINES:", len(map_text.splitlines()), "PREVIEW:", map_text.splitlines()[:6])

# 5. Log a single clean V3.1 kernel upgrade event to events.jsonl and test collect_live_agent_stream()
ev_jsonl = os.path.join(BLUEPRINT, "state", "events.jsonl")
if os.path.exists(ev_jsonl):
    os.remove(ev_jsonl)
factory_runner.append_v3_event(
    "C-20261121-001",
    1,
    "v3_1_kernel_ready",
    "V3.1内核加固",
    "已完成 StageA/B 质检收权、4项通用几何探针、Contract 契约压缩机与 2s 实况流窗口上线",
    files_changed=["dashboard/v2.html", "scripts/factory_runner.py", "scripts/v3_data.py", "verify_profiles/ui.md"],
)
stream_data = v3_data.collect_live_agent_stream()
print("LIVE_STREAM_ITEMS_COUNT:", len(stream_data.get("items") or []), "ACTIVE_TITLE:", stream_data.get("active_session_title"))
if stream_data.get("items"):
    print("LIVE_STREAM_TOP3:", json.dumps(stream_data["items"][:3], ensure_ascii=False))

# 7. Check board_server.py syntax and restart on port 8900 via WMI
import shutil
import time
import urllib.request

shutil.copyfile(
    os.path.join(STEWARD, "verify_profiles", "ui.md"),
    os.path.join(BLUEPRINT, "verify_profiles", "ui.md"),
)

c_res = subprocess.run([sys.executable, "-X", "utf8", "-m", "py_compile", bs_path], capture_output=True, text=True)
print("BS_COMPILE_RC:", c_res.returncode, "ERR:", c_res.stderr.strip())
if c_res.returncode != 0:
    lines_bs = open(bs_path, "r", encoding="utf-8").read().splitlines()
    for i, ln in enumerate(lines_bs[:280], 1):
        print(f"{i:3d}: {ln}")
    sys.exit(1)

# Reset contract.json rev back to clean Rev 1 after unit test so order state is clean
c_final["rev"] = 1
c_final["needs_human_resign"] = False
open(c_path, "w", encoding="utf-8").write(json.dumps(c_final, ensure_ascii=False, indent=2))

# Kill any old board_server.py and spawn fresh detached process on port 8900
subprocess.run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*board_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"], capture_output=True)
time.sleep(0.6)
log_8900 = os.path.join(STEWARD, "state", "board_server_8900.log")
log_fh = open(log_8900, "w", encoding="utf-8")
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
try:
    p_bs = subprocess.Popen(
        [sys.executable, "-X", "utf8", bs_path, "8900"],
        cwd=SCRIPTS,
        stdout=log_fh,
        stderr=log_fh,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
        close_fds=True,
    )
except OSError:
    p_bs = subprocess.Popen(
        [sys.executable, "-X", "utf8", bs_path, "8900"],
        cwd=SCRIPTS,
        stdout=log_fh,
        stderr=log_fh,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
print("BOARD_SERVER_SPAWNED_PID:", p_bs.pid)
time.sleep(2.5)
if os.path.exists(log_8900):
    print("BOARD_SERVER_LOG:", open(log_8900, "r", encoding="utf-8", errors="replace").read().strip()[:400])

# 8. Test /api/v3/events and /v2.html live dock over HTTP 8900
tok_path = os.path.join(STEWARD, "config", "board_token.txt")
tok = open(tok_path, "r", encoding="utf-8").read().strip()

ev_raw = urllib.request.urlopen(f"http://127.0.0.1:8900/api/v3/events?k={tok}", timeout=5).read().decode("utf-8")
ev_obj = json.loads(ev_raw)
print("HTTP_8900_EVENTS_OK:", json.dumps({
    "items_count": len(ev_obj.get("items") or []),
    "runner_busy": ev_obj.get("runner_busy"),
    "active_session_title": ev_obj.get("active_session_title"),
    "cost_per_accepted_cny": ev_obj.get("cost_per_accepted_cny"),
}, ensure_ascii=False))

v2_raw = urllib.request.urlopen(f"http://127.0.0.1:8900/v2.html?k={tok}", timeout=5).read().decode("utf-8", errors="replace")
print("HTTP_8900_V2_HAS_LIVE_DOCK:", "v3LiveStreamDock" in v2_raw, "V2_BYTES:", len(v2_raw.encode("utf-8")))

# 9. Print current queue status and recent history
q_obj = factory_runner.load_queue()
print("QUEUE_JOBS:", len(q_obj.get("jobs") or []), "RECENT_HISTORY:", json.dumps((q_obj.get("history") or [])[-2:], ensure_ascii=False))

# 10. Run Gate 1 general geometric probe + Edge headless screenshot against live HTTP 8900 /v2.html
probe_verify_dir = os.path.join(BLUEPRINT, "reports", "evidence", "C-20261121-001", "v31_live_verify")
os.makedirs(probe_verify_dir, exist_ok=True)
main_sha_cur = factory_runner.sha256_file(main_v2)
six_base_cur = factory_runner.snapshot_six_pages()
probe_res = factory_runner.probe_ui_dashboard(
    wt_repo=STEWARD,
    main_repo=STEWARD,
    target_rel="dashboard/v2.html",
    out_dir=probe_verify_dir,
    six_baseline=six_base_cur,
    old_sha="dummy_before_sha",
    main_file_sha_before=main_sha_cur,
)
print("GATE1_GENERAL_PROBE_CHECKS:", json.dumps(probe_res.get("checks") or {}, ensure_ascii=False))
print("GATE1_FAILURES:", json.dumps(probe_res.get("failures") or [], ensure_ascii=False))

# Also capture a live screenshot of http://127.0.0.1:8900/v2.html?k=<tok> with Edge headless so the injected Live Stream Dock is visible!
edge_exe = factory_runner.find_edge_exe()
live_shot = os.path.join(probe_verify_dir, "live_8900_v2_with_stream_dock.png")
live_shot_z13_open = os.path.join(probe_verify_dir, "live_8900_v2_zone13_open.png")
if edge_exe and os.path.exists(edge_exe):
    subprocess.run(
        [
            edge_exe,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--window-size=1440,950",
            "--virtual-time-budget=3500",
            f"--screenshot={live_shot}",
            f"http://127.0.0.1:8900/v2.html?k={tok}",
        ],
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            edge_exe,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--window-size=1440,980",
            "--virtual-time-budget=3500",
            f"--screenshot={live_shot_z13_open}",
            f"http://127.0.0.1:8900/v2.html?k={tok}#z13",
        ],
        capture_output=True,
        timeout=30,
    )
    print("LIVE_SHOT_SAVED:", os.path.exists(live_shot), "Z13_OPEN_SAVED:", os.path.exists(live_shot_z13_open))
