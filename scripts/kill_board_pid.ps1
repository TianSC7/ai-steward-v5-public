# 按 PID 精确重启看板进程（BoardGuard8900 会拉起新进程加载新代码）
Stop-Process -Id 10932 -Force -ErrorAction SilentlyContinue
Write-Output "killed 10932"
