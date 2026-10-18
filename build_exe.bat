@echo off
REM =====================================================================
REM  VHS-Viewer — сборка .exe (с аналитикой)
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

echo [*] Собираю (может занять 3-10 минут)...
.venv\Scripts\python.exe -m PyInstaller app.spec --noconfirm

if errorlevel 1 (
    echo.
    echo [!] Сборка упала. Смотрите ошибки выше.
    pause
    exit /b 1
)

set OUTDIR=dist\VHS-Viewer

echo [*] Копирую внешние файлы (БЕЗ секретов)...

REM --- ВАЖНО: НЕ копируем viewer_config.yaml (там пароли и токены) ---
REM Копируем только example — пользователь скопирует сам и заполнит.

if exist viewer_config.example.yaml (
    copy /Y viewer_config.example.yaml "%OUTDIR%\viewer_config.example.yaml" >nul
)

if exist LICENSE (
    copy /Y LICENSE "%OUTDIR%\LICENSE" >nul
)
if exist README.md (
    copy /Y README.md "%OUTDIR%\README.md" >nul
)

copy /Y start.bat "%OUTDIR%\start.bat" >nul 2>&1
copy /Y stop.bat  "%OUTDIR%\stop.bat"  >nul 2>&1

REM --- Опционально: положить модели рядом, если есть в папке models/ ---
if exist models (
    if not exist "%OUTDIR%\models" mkdir "%OUTDIR%\models"
    xcopy /Y /Q models\* "%OUTDIR%\models\" >nul 2>&1
)

echo.
echo ============================================
echo   Готово!
echo ============================================
echo.
echo Папка:  %OUTDIR%
echo Запуск: %OUTDIR%\VHS-Viewer.exe
echo.
echo НЕ ЗАБУДЬТЕ при первом запуске:
echo  1. Скопировать viewer_config.example.yaml -^> viewer_config.yaml
echo  2. Заполнить servers, пароли, токены
echo.
endlocal
pause