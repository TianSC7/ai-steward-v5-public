$ErrorActionPreference = "Continue"
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "C:\Program Files\Microsoft\Edge\Application\msedge.exe" }
"edge=$edge exists=$(Test-Path $edge)"
$udd = "C:\Users\REPLACE_WITH_USER\AppData\Local\Temp\v3_edge_test"
if (Test-Path $udd) { Remove-Item -Recurse -Force $udd -ErrorAction SilentlyContinue }
$p = Start-Process -FilePath $edge -ArgumentList "--user-data-dir=$udd","--remote-debugging-port=62299","--headless=new","--no-first-run","--no-default-browser-check","about:blank" -PassThru
Start-Sleep -Seconds 8
"pid=$($p.Id) has_exited=$($p.HasExited)"
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:62299/json/version" -UseBasicParsing -TimeoutSec 5
  "CDP_OK: " + $r.Content.Substring(0, [Math]::Min(200, $r.Content.Length))
} catch {
  "CDP_FAIL: " + $_.Exception.Message
  $errOut = & $edge --headless=new --dump-dom about:blank 2>&1 | Select-Object -First 5
  "direct_run: " + ($errOut -join " / ")
}
Get-Process msedge -ErrorAction SilentlyContinue | Measure-Object | ForEach-Object { "edge_procs_now=$($_.Count)" }
