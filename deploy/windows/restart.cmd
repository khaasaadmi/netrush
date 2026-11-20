@echo off
rem Restart NetRush after changing .env or updating the code (run as Administrator).
schtasks /End /TN "NetRush" >nul 2>&1
timeout /t 2 >nul
schtasks /Run /TN "NetRush"
echo NetRush restarted. Check http://localhost:8000/healthz
