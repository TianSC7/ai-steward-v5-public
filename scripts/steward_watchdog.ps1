param(
  [string]$ProcessName = 'ZCode',
  [string]$ExePath = 'C:\Users\Administrator\AppData\Local\Programs\ZCode\ZCode.exe',
  [string]$LogPath = 'C:\Users\Administrator\Documents\mate\plugins\ai-steward\state\watchdog.log',
  [int]$MaxLogBytes = 5MB
)

$ErrorActionPreference = 'SilentlyContinue'
$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

function Write-Log([string]$msg) {
  $dir = Split-Path -Parent $LogPath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt $MaxLogBytes)) {
    Move-Item -Path $LogPath -Destination ($LogPath + '.1') -Force
  }
  Add-Content -Path $LogPath -Value "$ts $msg" -Encoding UTF8
}

# --- zcode-site plugin-host leak patrol (D-044, 2026-11-19) ---
# Platform leaks one `__zcode-plugin-host ...zcode-site...server.mjs` process per tick.
# Rule: age > 12 min -> taskkill /F. Only processes whose command line has BOTH
# __zcode-plugin-host and zcode-site are candidates; main/renderer/session hosts never are.
# NOTE: keep this block ASCII-only. Non-ASCII comments in a no-BOM .ps1 get mis-decoded by
# PowerShell 5.1 and silently swallow the following statement (observed 14:01 misfire).
$SiteMaxAgeMin = 12
$siteTotal = 0
$siteHosts = @()
foreach ($p in (Get-CimInstance Win32_Process)) {
  if (-not $p.CommandLine) { continue }
  if ($p.Name -ne 'ZCode.exe') { continue }
  if ($p.CommandLine -notlike '*__zcode-plugin-host*zcode-site*') { continue }
  if ($p.ProcessId -eq $PID) { continue }
  $siteTotal = $siteTotal + 1
  $ageMin = [math]::Round(((Get-Date) - $p.CreationDate).TotalMinutes, 1)
  if ($ageMin -le [double]$SiteMaxAgeMin) { continue }
  $siteHosts += [pscustomobject]@{ Id = [int]$p.ProcessId; Age = $ageMin }
}
if ($siteTotal -gt 0) { Write-Log "ZCODESITE-SCAN hosts=$siteTotal kill=$($siteHosts.Count) threshold=${SiteMaxAgeMin}m" }
foreach ($h in $siteHosts) {
  try {
    Stop-Process -Id $h.Id -Force -ErrorAction Stop
    Write-Log "ZCODESITE-KILL pid=$($h.Id) age=$($h.Age)m"
  } catch {
    Write-Log "ZCODESITE-KILL-FAILED pid=$($h.Id) age=$($h.Age)m err=$($_.Exception.Message)"
  }
}

$procs = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
if ($procs) {
  # heartbeat once per hour at minute 0 to prove watchdog alive
  if ((Get-Date).Minute -eq 0) { Write-Log "OK $ProcessName pid=$($procs[0].Id) count=$($procs.Count)" }
  exit 0
}

if (-not (Test-Path $ExePath)) {
  Write-Log "MISSING exe=$ExePath"
  exit 2
}

Write-Log "DOWN $ProcessName not found -> starting $ExePath"
Start-Process -FilePath $ExePath -WindowStyle Normal
Start-Sleep -Seconds 3
$after = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
if ($after) { Write-Log "RESTARTED pid=$($after[0].Id)" } else { Write-Log 'RESTART-FAILED' ; exit 3 }

# --- MES board refresh + http serve guard ---
$mesPy = 'C:\Program Files\Python313\python.exe'
& $mesPy -X utf8 'C:\Users\Administrator\Documents\mate\plugins\ai-steward\scripts\mes_board.py' | Out-Null
$mesL = netstat -ano | Select-String ':8900 .*LISTENING'
if (-not $mesL) { Start-Process -WindowStyle Hidden -FilePath $mesPy -ArgumentList '-X','utf8','-m','http.server','8900','--bind','0.0.0.0','--directory','C:\Users\Administrator\Documents\mate\plugins\ai-steward\dashboard' }
