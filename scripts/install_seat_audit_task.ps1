# 五经理制席位普查任务安装（可回滚：Unregister-ScheduledTask -TaskName MES-Seat-Audit -Confirm:$false）
$ErrorActionPreference = "Stop"
$action = New-ScheduledTaskAction -Execute "C:\Program Files\Python313\pythonw.exe" `
  -Argument '-X utf8 "C:\Users\Administrator\Documents\mate\plugins\ai-steward\scripts\factory_seat.py" verify --strict'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "MES-Seat-Audit" -Action $action -Trigger $trigger `
  -Description "五经理制席位普查：经理<=5、W=0 P0" -Force | Out-Null
Get-ScheduledTask -TaskName "MES-Seat-Audit" | Select-Object TaskName,State | Format-List
