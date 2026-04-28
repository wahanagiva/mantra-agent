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
echo Date: %DATE% %TIME% >> "%LOG%"
echo OS: >> "%LOG%"
ver >> "%LOG%" 2>&1
echo. >> "%LOG%"

REM --- 0. Pre-check: connectivity + Visual C++ Redist ----------------------
echo [0/6] Checking system requirements...

REM Check internet to github.com
curl -s --max-time 5 -o nul -w "%%{http_code}" https://github.com > "%TOOLS%\_net.txt" 2>nul
set /p NET_CODE=<"%TOOLS%\_net.txt" 2>nul
del "%TOOLS%\_net.txt" 2>nul
if not "%NET_CODE%"=="200" if not "%NET_CODE%"=="301" if not "%NET_CODE%"=="302" (
    echo ERROR: Cannot reach github.com ^(network blocked or no internet^).
    echo        Check WiFi/ethernet, proxy, atau corporate firewall yang block github.com.
    echo        HTTP code received: %NET_CODE%
    goto fail
)

REM Check Visual C++ Redist 2015-2022 (x64) installed (registry check)
set "VC_OK=0"
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" /v Installed >nul 2>&1
if not errorlevel 1 set "VC_OK=1"
if "%VC_OK%"=="0" reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" /v Installed >nul 2>&1 && set "VC_OK=1"

if "%VC_OK%"=="0" (
    echo.
    echo ============================================================
    echo   Visual C++ Redistributable 2015-2022 ^(x64^) BELUM TERPASANG.
    echo   Tanpa ini, PyTorch CRASH ^(error c10.dll^).
    echo ============================================================
    echo.
    echo Mendownload installer ^(~14 MB^)...
    curl -L --retry 3 -o "%TOOLS%\vc_redist.x64.exe" "https://aka.ms/vs/17/release/vc_redist.x64.exe" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: VC++ Redist download failed & goto fail )

    echo.
    echo Akan muncul UAC prompt - WAJIB klik YES.
    echo Kalo gak diklik YES, agent gak akan jalan.
    timeout /t 3 >nul

    REM Trigger UAC via PowerShell + WAIT for completion
    powershell -NoProfile -Command "Start-Process -FilePath '%TOOLS%\vc_redist.x64.exe' -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -Wait" >> "%LOG%" 2>&1
    del "%TOOLS%\vc_redist.x64.exe" 2>nul

    REM Re-check after install
    set "VC_OK=0"
    reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" /v Installed >nul 2>&1
    if not errorlevel 1 set "VC_OK=1"
    if "!VC_OK!"=="0" reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" /v Installed >nul 2>&1 && set "VC_OK=1"

    if "!VC_OK!"=="0" (
        echo.
        echo ============================================================
        echo   ERROR: Visual C++ Redistributable GAGAL DI-INSTALL
        echo ============================================================
        echo.
        echo Kemungkinan UAC prompt di-cancel atau policy block elevation.
        echo.
        echo MANUAL FIX:
        echo   1. Download: https://aka.ms/vs/17/release/vc_redist.x64.exe
        echo   2. Double-click, klik YES pas UAC, install
        echo   3. Setelah selesai, re-run MantraAgent.bat
        echo.
        goto fail
    )
    echo   VC++ Redist install OK.
)

echo   System requirements OK.
echo.

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

REM --- 5c. Node.js portable (kepake buat bgutil-ytdlp-pot-provider /
REM        bypass YouTube bot detection; full-album fail tanpa ini) ----------
set "NODE_DIR=%ROOT%\node"
if not exist "%NODE_DIR%\node.exe" (
    echo [5/5c] Downloading Node.js 22 LTS portable ^(~30 MB^)...
    curl -L --retry 3 -o "%TOOLS%\node.zip" "https://nodejs.org/dist/v22.11.0/node-v22.11.0-win-x64.zip" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo ERROR: Node.js download failed & goto fail )
    if exist "%TOOLS%\node_extract" rmdir /s /q "%TOOLS%\node_extract"
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%TOOLS%\node.zip' -DestinationPath '%TOOLS%\node_extract' -Force" >> "%LOG%" 2>&1
    if exist "%NODE_DIR%" rmdir /s /q "%NODE_DIR%"
    for /f "delims=" %%D in ('dir /b /ad "%TOOLS%\node_extract"') do (
        move "%TOOLS%\node_extract\%%D" "%NODE_DIR%" >nul 2>&1
    )
    rmdir /s /q "%TOOLS%\node_extract" 2>nul
    del "%TOOLS%\node.zip" 2>nul
)

REM --- 6. Self-install bat + write VBS launcher + register autostart -------
REM Autostart runs the VBS launcher (silent, no console) which calls the bat
REM with hidden window. First-run keeps console visible (user sees install
REM progress), subsequent boots = silent like Spotify / Discord.
set "INSTALLED_BAT=%ROOT%\MantraAgent.bat"
set "VBS_LAUNCHER=%ROOT%\MantraAgent.vbs"
if /i not "%~f0"=="%INSTALLED_BAT%" (
    copy /y "%~f0" "%INSTALLED_BAT%" >nul 2>&1
)
REM Write VBS launcher (one-liner that runs bat hidden, no wait)
> "%VBS_LAUNCHER%" echo CreateObject("Wscript.Shell").Run """%INSTALLED_BAT%""", 0, False
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MantraAgent /t REG_SZ /d "wscript.exe \"%VBS_LAUNCHER%\"" /f >> "%LOG%" 2>&1
echo [autostart] registered silent launcher ^(VBS hidden window^)

REM --- 7. Spawn tray (skip if already running to avoid double-spawn race) -
REM Race scenario: user double-clicks bat, lalu Windows autostart fire bat
REM kedua kalinya = 2 tray + 2 agent = port conflict. Cek dulu.
powershell -NoProfile -Command "if (Get-Process pythonw -EA SilentlyContinue | Where-Object { try { $_.MainModule.FileName -like '*MantraAgent*src*' } catch { $false } }) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo [run] Mantra Agent tray sudah jalan - skip spawn
    goto :end
)

echo [run] Starting Mantra Agent tray ...
set "MANTRA_AGENT_FFMPEG=%BIN%\ffmpeg.exe"
set "MANTRA_AGENT_YTDLP=%BIN%\yt-dlp.exe"
REM Prepend our portable Node to PATH so bgutil-ytdlp-pot-provider finds it
set "PATH=%NODE_DIR%;%PATH%"
set "PYTHONPATH=%SRC%"
start "" /B "%VENV%\Scripts\pythonw.exe" "%SRC%\tray.py"

:end

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
