$ErrorActionPreference = "Continue"
# 只杀无头 Edge（探针僵尸），不动正常 Edge 窗口
$procs = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'"
$headless = $procs | Where-Object { $_.CommandLine -match '--headless' }
foreach ($p in $headless) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
"total_edge=$($procs.Count) headless_killed=$($headless.Count)"
