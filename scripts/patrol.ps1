# patrol.ps1 - AA factory one-shot read-only patrol (2026-11-24)
# Reads only. Writes a single report file under state\. No process changes, no restarts.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File patrol.ps1
$ErrorActionPreference = "Continue"
$ROOT = "C:\Users\Administrator\Documents\mate\plugins\ai-steward"
$STATE = Join-Path $ROOT "state"
$PY = "C:\Program Files\Python313\python.exe"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$out = Join-Path $STATE ("patrol_{0}.txt" -f $stamp)
$lines = @()
function Add-Line($t) { $script:lines += $t; Write-Output $t }

Add-Line ("=== PATROL {0} ===" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))

Add-Line "--- [1] ports (8900/8901/8000/8082) ---"
$ports = netstat -ano | Select-String "LISTENING" | Select-String ":8900 ",":8901 ",":8000 ",":8082 "
if ($ports) { $ports | ForEach-Object { Add-Line $_.Line.Trim() } } else { Add-Line "NONE LISTENING - CHECK!" }

Add-Line "--- [2] board healthz (local 8901) ---"
$h = & curl.exe -s -w " code=%{http_code} t=%{time_total}s" --max-time 5 http://127.0.0.1:8901/healthz
Add-Line $h

Add-Line "--- [3] gateway probe (8788) ---"
$gp = & $PY -X utf8 (Join-Path $ROOT "scripts\gateway_probe.py") --json 2>$null
try {
  $gj = $gp | ConvertFrom-Json
  Add-Line ("level={0} version={1} uptime_sec={2} lanes={3}/{4} cooldowns={5} degraded={6} reqs={7} ok={8} cost={9}" -f `
    $gj.level, $gj.version, $gj.uptime_sec, $gj.lanes_usable, $gj.lanes_total, $gj.cooldowns.Count, $gj.degraded.Count, $gj.totals.requests, $gj.totals.ok, $gj.totals.cost_cny)
} catch { Add-Line ("gateway_probe raw: " + ($gp -join " ")) }

Add-Line "--- [4] monitor5 snapshot ---"
Add-Line ((Get-Content (Join-Path $STATE "monitor_5min.json") -Raw -ErrorAction SilentlyContinue))

Add-Line "--- [5] anomalies tail (5) ---"
$al = Get-Content (Join-Path $STATE "monitor5_anomalies.log") -Tail 5 -ErrorAction SilentlyContinue
if ($al) { $al | ForEach-Object { Add-Line $_ } } else { Add-Line "(empty)" }

Add-Line "--- [6] gateway errors today ---"
$glog = Join-Path $ROOT ("logs\gateway-{0}.jsonl" -f (Get-Date -Format "yyyyMMdd"))
if (Test-Path $glog) {
  $ec = (Select-String -Path $glog -Pattern "level.{0,3}error" -ErrorAction SilentlyContinue | Measure-Object).Count
  Add-Line ("error_lines_today={0} file={1}" -f $ec, $glog)
} else { Add-Line ("no gateway log today: " + $glog) }

Add-Line "--- [7] guard tasks next run ---"
schtasks /Query /FO CSV /NH | Select-String -SimpleMatch "Steward","Factory","Monitor","Caddy","Board","Seafile" | ForEach-Object { Add-Line $_.Line }

Add-Line "--- [8] resources ---"
$os = Get-CimInstance Win32_OperatingSystem
Add-Line ("mem_free_gb={0:N1}/{1:N1} cpu_load={2}%" -f ($os.FreePhysicalMemory/1MB), ($os.TotalVisibleMemorySize/1MB), ((Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average))
Get-PSDrive C,D | ForEach-Object { Add-Line ("disk {0} free_gb={1:N0}" -f $_.Name, ($_.Free/1GB)) }

Add-Line "=== PATROL END ==="
$lines | Set-Content -Path $out -Encoding UTF8
Write-Output ("REPORT: " + $out)
