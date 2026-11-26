param(
  [int]$DelaySeconds = 0,
  [string]$ExePath = 'C:\Users\Administrator\AppData\Local\Programs\ZCode\ZCode.exe',
  [string]$ResultPath = 'C:\Users\Administrator\Documents\mate\plugins\ai-steward\state\SELFKILL_DRILL_RESULT.txt',
  [string]$TracePath  = 'C:\Users\Administrator\Documents\mate\plugins\ai-steward\state\selfkill_drill.log',
  [switch]$DryRun
)

$ErrorActionPreference = 'SilentlyContinue'

function Trace([string]$m) {
  $dir = Split-Path -Parent $TracePath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  Add-Content -Path $TracePath -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8
}

function Get-Target {
  Get-Process -Name 'ZCode' -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -eq $ExePath } catch { $false }
  }
}

$procs = @(Get-Target)
Trace ("start delay=$DelaySeconds dry=$($DryRun.IsPresent) pid_count=$($procs.Count)")

if ($DryRun) {
  "DRYRUN ok target_count=$($procs.Count) exe=$ExePath"
  $procs | Select-Object -First 5 Id, ProcessName, Path | Format-Table -AutoSize | Out-String -Width 200
  exit 0
}

if ($DelaySeconds -gt 0) { Start-Sleep -Seconds $DelaySeconds }

$before = @(Get-Target)
if ($before.Count -eq 0) {
  Trace 'abort: no target process'
  "ABORT no ZCode process matched $ExePath"
  exit 2
}

$t0 = Get-Date
$pids = ($before | Sort-Object Id | Select-Object -ExpandProperty Id) -join ','
Trace ("kill t0=$($t0.ToString('HH:mm:ss')) count=$($before.Count) pids=$pids")
$before | Stop-Process -Force

$restarted = $null
$deadline = $t0.AddSeconds(240)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 2
  $now = @(Get-Target)
  if ($now.Count -gt 0) { $restarted = $now; break }
}

$t1 = Get-Date
$source = 'watchdog'
if (-not $restarted) {
  Trace 'restart-timeout 240s -> drill starts exe itself'
  Start-Process -FilePath $ExePath -WindowStyle Normal
  $source = 'drill-fallback'
  $deadline2 = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $deadline2) {
    Start-Sleep -Seconds 2
    $now = @(Get-Target)
    if ($now.Count -gt 0) { $restarted = $now; break }
  }
  $t1 = Get-Date
}

$downtime = [math]::Round(($t1 - $t0).TotalSeconds, 1)
$ok = [bool]$restarted
if ($ok) {
  Trace ("restarted t1=$($t1.ToString('HH:mm:ss')) downtime=${downtime}s count=$($restarted.Count) source=$source")
} else {
  Trace 'RESTART-FAILED'
}

$logTail = ''
$wl = 'C:\Users\Administrator\Documents\mate\plugins\ai-steward\state\watchdog.log'
if (Test-Path $wl) { $logTail = (Get-Content $wl -Tail 5 | Out-String) }

$block = @()
$block += "=== SELFKILL DRILL $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
$block += "target=$ExePath"
$block += "kill_epoch=$($t0.ToString('yyyy-MM-dd HH:mm:ss')) killed_pids_count=$($before.Count)"
$block += "restart_epoch=$($t1.ToString('yyyy-MM-dd HH:mm:ss')) downtime_sec=$downtime restarted=$ok source=$source"
$block += "watchdog_log_tail:"
$block += $logTail
$block += "session_recovery=UNKNOWN (device proof: this session replying after restart)"
$block += "EXIT=$(if($ok){0}else{3})"
$block | Add-Content -Path $ResultPath -Encoding UTF8

if ($ok) { Set-Content -Path 'C:\Users\Administrator\Documents\mate\plugins\ai-steward\state\.drill_done' -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') -Encoding UTF8 }
$block | ForEach-Object { $_ }
if ($ok) { exit 0 } else { exit 3 }
