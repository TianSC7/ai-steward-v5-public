# dynamically find board_server.py processes and kill them (BoardGuard8900 relaunches with new code)
# v2 2026-11-25: old version matched netstat :8900 listener = caddy.exe (wrong target since the
# board moved to 127.0.0.1:8901 behind Caddy). Now matches process command line instead of port.
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*board_server.py*' }
if ($procs) {
    foreach ($p in $procs) {
        Write-Output ("killed " + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force
    }
} else {
    Write-Output "none running"
}
