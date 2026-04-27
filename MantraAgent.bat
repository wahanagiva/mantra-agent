@echo off
REM ============================================================================
REM  Mantra Creative Agent - bootstrap + run
REM  First-run: setup Python + venv + deps + ffmpeg + yt-dlp (~5-10 min)
REM  Subsequent runs: instant - just spawn agent
REM  Auto-start: registered to HKCU Run on first successful run
REM  No admin / UAC required.
REM ============================================================================

setlocal enabledelayedexpansion

set "ROOT=%LOCALAPPDATA%\MantraAgent"
set "VENV=%ROOT%\venv"
set "SRC=%ROOT%\src"
set "BIN=%ROOT%\bin"
set "TOOLS=%ROOT%\tools"
set "LOG=%ROOT%\setup.log"

if not exist "%ROOT%" mkdir "%ROOT%"
if not exist "%TOOLS%" mkdir "%TOOLS%"
if not exist "%BIN%" mkdir "%BIN%"

REM Source code distribution.
REM PROD: download zip from GitHub (always-latest pattern).
REM Override for local dev/testing: set MANTRA_LOCAL_SRC env var to a local path.
set "GITHUB_SRC_ZIP=https://github.com/wahanagiva/mantra-agent/archive/refs/heads/main.zip"
set "GITHUB_VERSION_URL=https://api.github.com/repos/wahanagiva/mantra-agent/commits/main"

echo. > "%LOG%"
echo Mantra Agent Bootstrap >> "%LOG%"
echo ROOT: %ROOT% >> "%LOG%"

REM --- 1. uv (15 MB single binary - handles Python + venv + pip) -------------
if not exist "%TOOLS%\uv.exe" (
    echo [1/5] Downloading uv 15 MB...
    curl -L --retry 3 -o "%TOOLS%\uv.zip" "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: uv download failed & goto fail )
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%TOOLS%\uv.zip' -DestinationPath '%TOOLS%' -Force" >> "%LOG%" 2>&1
    del "%TOOLS%\uv.zip"
    if not exist "%TOOLS%\uv.exe" ( echo ERROR: uv.exe missing after extract & goto fail )
)

REM --- 2. Python 3.12 venv (uv auto-downloads Python if needed) -------------
if not exist "%VENV%\Scripts\python.exe" (
    echo [2/5] Setting up Python 3.12 venv...
    "%TOOLS%\uv.exe" venv --python 3.12 "%VENV%" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: uv venv failed & goto fail )
)

REM --- 3. Source code (download from GitHub once, on first install) ---
if not exist "%SRC%\main.py" (
    echo [3/5] Downloading source from GitHub...
    curl -L --retry 3 -o "%TOOLS%\src.zip" "%GITHUB_SRC_ZIP%" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: source download failed - check internet & goto fail )
    if exist "%TOOLS%\src_extract" rmdir /s /q "%TOOLS%\src_extract"
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%TOOLS%\src.zip' -DestinationPath '%TOOLS%\src_extract' -Force" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: source extract failed & goto fail )
    REM Move extracted folder (mantra-agent-main\*) into SRC
    for /f "delims=" %%D in ('dir /b /ad "%TOOLS%\src_extract"') do (
        move "%TOOLS%\src_extract\%%D" "%SRC%" >nul 2>&1
    )
    rmdir /s /q "%TOOLS%\src_extract" 2>nul
    del "%TOOLS%\src.zip" 2>nul
)

REM --- 4. pip install deps (also re-runs if requirements.txt changed) -----
set "REQ_HASH_FILE=%VENV%\.req_hash"
set "REQ_HASH_NEW="
for /f "delims=" %%H in ('certutil -hashfile "%SRC%\requirements.txt" SHA256 ^| findstr /v ":"') do if not defined REQ_HASH_NEW set "REQ_HASH_NEW=%%H"
set "REQ_HASH_OLD="
if exist "%REQ_HASH_FILE%" set /p REQ_HASH_OLD=<"%REQ_HASH_FILE%"

set "NEEDS_PIP=0"
if not exist "%VENV%\Lib\site-packages\fastapi" set "NEEDS_PIP=1"
if not "%REQ_HASH_NEW%"=="%REQ_HASH_OLD%" set "NEEDS_PIP=1"

if "%NEEDS_PIP%"=="1" (
    echo [4/5] Installing/updating Python deps ^(slow on first run, ~5-10 min^)...
    "%TOOLS%\uv.exe" pip install --python "%VENV%\Scripts\python.exe" --index-strategy unsafe-best-match --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu -r "%SRC%\requirements.txt" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: pip install failed - check %LOG% & goto fail )
    echo !REQ_HASH_NEW!>"%REQ_HASH_FILE%"
) else (
    echo [4/5] Deps up-to-date
)

REM --- 5a. ffmpeg ----------------------------------------------------------
if not exist "%BIN%\ffmpeg.exe" (
    echo [5/5a] Downloading ffmpeg 32 MB...
    curl -L --retry 3 -o "%TOOLS%\ffmpeg.zip" "https://github.com/GyanD/codexffmpeg/releases/download/8.1/ffmpeg-8.1-essentials_build.zip" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: ffmpeg download failed & goto fail )
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%TOOLS%\ffmpeg.zip' -DestinationPath '%TOOLS%\ffmpeg_extract' -Force" >> "%LOG%" 2>&1
    for /f "delims=" %%D in ('dir /b /ad "%TOOLS%\ffmpeg_extract"') do (
        copy /y "%TOOLS%\ffmpeg_extract\%%D\bin\ffmpeg.exe" "%BIN%\" >nul
        copy /y "%TOOLS%\ffmpeg_extract\%%D\bin\ffprobe.exe" "%BIN%\" >nul
    )
    rmdir /s /q "%TOOLS%\ffmpeg_extract"
    del "%TOOLS%\ffmpeg.zip"
)

REM --- 5b. yt-dlp standalone exe -------------------------------------------
if not exist "%BIN%\yt-dlp.exe" (
    echo [5/5b] Downloading yt-dlp 17 MB...
    curl -L --retry 3 -o "%BIN%\yt-dlp.exe" "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: yt-dlp download failed & goto fail )
)

REM --- 6. Self-install bat to %ROOT% + register autostart from there ------
REM CRITICAL: autostart must point to a STABLE path. Pointing to %~f0 (where
REM the user double-clicked from) breaks if user moves/deletes that file.
REM Always copy ourselves to %ROOT%\MantraAgent.bat (lives next to runtime).
set "INSTALLED_BAT=%ROOT%\MantraAgent.bat"
if /i not "%~f0"=="%INSTALLED_BAT%" (
    copy /y "%~f0" "%INSTALLED_BAT%" >nul 2>&1
)
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MantraAgent /t REG_SZ /d "\"%INSTALLED_BAT%\"" /f >> "%LOG%" 2>&1
echo [autostart] registered ^(points to %INSTALLED_BAT%^)

REM --- 7. Spawn tray (which spawns agent + shows tray icon) ---------------
echo [run] Starting Mantra Agent tray ...
set "MANTRA_AGENT_FFMPEG=%BIN%\ffmpeg.exe"
set "MANTRA_AGENT_YTDLP=%BIN%\yt-dlp.exe"
set "PYTHONPATH=%SRC%"
start "" /B "%VENV%\Scripts\pythonw.exe" "%SRC%\tray.py"

echo.
echo OK - Mantra Agent jalan di background. Buka https://mantra.majutrah.co.id
echo Logs: %LOG%
echo Tray icon di system tray (kanan bawah).
timeout /t 5 >nul
exit /b 0

:fail
echo.
echo === FAIL - check %LOG% for details ===
echo.
pause
exit /b 1
