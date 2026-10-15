@echo off
REM =====================================================================
REM  VHS-Viewer — сборка .exe
REM =====================================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ============================================
echo   VHS-Viewer — сборка .exe
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [!] Не найдено .venv. Создайте:  python -m venv .venv
    pause
    exit /b 1
)

.venv\Scripts\python.exe -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [*] PyInstaller не установлен — устанавливаю...
    .venv\Scripts\pip.exe install pyinstaller
)

echo [*] Очищаю предыдущую сборку...
if exist build rmdir /S /Q build
if exist dist rmdir /S /Q dist

echo [*] Собираю...
.venv\Scripts\python.exe -m PyInstaller app.spec --noconfirm

if errorlevel 1 (
    echo.
    echo [!] Сборка упала. Смотрите ошибки выше.
    pause
    exit /b 1
)

set OUTDIR=dist\VHS-Viewer

echo [*] Копирую внешние файлы...

if exist viewer_config.example.yaml (
    copy /Y viewer_config.example.yaml "%OUTDIR%\viewer_config.example.yaml" >nul
)
if exist viewer_config.yaml (
    copy /Y viewer_config.yaml "%OUTDIR%\viewer_config.yaml" >nul
) else (
    echo [i] viewer_config.yaml не найден — копирую из example
    copy /Y viewer_config.example.yaml "%OUTDIR%\viewer_config.yaml" >nul
)
if exist LICENSE (
    copy /Y LICENSE "%OUTDIR%\LICENSE" >nul
)

copy /Y start.bat "%OUTDIR%\start.bat" >nul 2>&1
copy /Y stop.bat  "%OUTDIR%\stop.bat"  >nul 2>&1

echo.
echo ============================================
echo   Готово!
echo ============================================
echo.
echo Папка:  %OUTDIR%
echo Запуск: %OUTDIR%\VHS-Viewer.exe
echo.
endlocal
pause