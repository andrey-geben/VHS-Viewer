@echo off
cd /d "%~dp0"
echo Останавливаю VHS-Viewer...
taskkill /IM VHS-Viewer.exe /F >nul 2>&1
echo [OK] Остановлено.
timeout /t 2 /nobreak >nul