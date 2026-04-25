@echo off
REM Mantra Creative Agent — Windows dev launcher
REM Run from agent/ folder. Creates venv if missing, installs deps, runs server.

setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtualenv at .venv
    python -m venv .venv
    if errorlevel 1 (
        echo [error] Failed to create venv. Is Python 3.11+ installed and in PATH?
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

echo [setup] Installing/updating dependencies
".venv\Scripts\pip.exe" install -q --upgrade pip
".venv\Scripts\pip.exe" install -q -r requirements.txt
if errorlevel 1 (
    echo [error] pip install failed
    exit /b 1
)

REM Allow extra CORS origins for dev (e.g. http://localhost:8080)
if "%MANTRA_AGENT_CORS_EXTRA%"=="" (
    set MANTRA_AGENT_CORS_EXTRA=http://localhost:3000,http://127.0.0.1:3000,http://localhost:8080
)

echo [run] Starting agent on http://127.0.0.1:5555
echo       (CORS extras: %MANTRA_AGENT_CORS_EXTRA%)
echo.
".venv\Scripts\python.exe" main.py

endlocal
