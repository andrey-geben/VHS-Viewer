@echo off
cd /d "%~dp0"
if exist run.pid (
    set /p OLD_PID=<run.pid
    tasklist /FI "PID eq %OLD_PID%" 2>nul | find /I "%OLD_PID%" >nul
    if not errorlevel 1 (
        echo [!] Уже запущено ^(PID=%OLD_PID%^).
        pause
        exit /b 1
    )
    del run.pid >nul 2>&1
)
netstat -ano | find ":8200 " | find "LISTENING" >nul
if not errorlevel 1 (
    echo [!] Порт 8200 занят другим процессом.
    pause
    exit /b 1
)
echo [*] Запускаю VHS-Viewer...
start "VHS-Viewer" cmd /k VHS-Viewer.exe
echo [OK] Через несколько секунд откроется браузер на http://127.0.0.1:8200
timeout /t 3 /nobreak >nul