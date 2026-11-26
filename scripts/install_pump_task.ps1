# 注册 Factory-Autopilot-Pump：每 5 分钟、24h 全天候（MES/12 绝对无人值守法执行体）
$py = "python"
$script = "C:\Users\Administrator\Documents\mate\plugins\ai-steward\scripts\autopilot_pump.py"
schtasks /Create /F /TN "Factory-Autopilot-Pump" /TR "$py -X utf8 $script" /SC MINUTE /MO 5
schtasks /Query /TN "Factory-Autopilot-Pump" /FO LIST
