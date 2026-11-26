# restart board_server (by command line match, NEVER by port - Caddy owns 8900)
# BoardGuard8900 scheduled task runs every minute; after kill the guard restarts board with new code.
# v2 2026-11-25: old version killed whatever listened on :8900 = caddy.exe. Fixed to match
# python processes whose command line contains board_server.py.
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*board_server.py*' }
if ($procs) {
    foreach ($p in $procs) {
        Write-Output ("killing pid " + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force
    }
} else {
    Write-Output "no board_server.py process found (guard will start it)"
}
