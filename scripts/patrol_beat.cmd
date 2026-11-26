@echo off
chcp 65001 >nul
rem 24h 零 token 巡检拍（本地脚本，不调 LLM）。红灯写 state\PATROL_ALERT.txt，供下一单/下一拍处理。
setlocal
set PY=C:\Program Files\Python313\python.exe
set ROOT=C:\Users\Administrator\Documents\mate\plugins\ai-steward
cd /d "%ROOT%"
if not exist logs mkdir logs
if not exist state mkdir state
echo.>>logs\patrol.log
echo ==== %date% %time% ====>>logs\patrol.log
"%PY%" -X utf8 scripts\factory_seat.py patrol >>logs\patrol.log 2>&1
set RC=%ERRORLEVEL%
for /f %%c in ('curl.exe -s -o NUL -w "%%{http_code}" http://127.0.0.1:8900/') do echo 8900_root=%%c>>logs\patrol.log
for /f %%c in ('curl.exe -s -o NUL -w "%%{http_code}" http://127.0.0.1:8900/healthz') do echo 8900_healthz=%%c>>logs\patrol.log
if %RC% GEQ 3 (
  echo [ALERT] %date% %time% patrol_rc=%RC%>>state\PATROL_ALERT.txt
) else (
  echo [OK] %date% %time% patrol_rc=%RC%>>state\PATROL_OK.txt
)
endlocal
