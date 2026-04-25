@echo off
REM Mantra Creative Agent — Tray launcher
REM Double-click to start the agent + tray icon. No console window.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [error] Venv not set up. Run run_dev.bat first to install deps.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "tray.py"
endlocal
