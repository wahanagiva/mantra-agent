@echo off
REM ============================================================================
REM  Mantra Creative Agent - BULLETPROOF Installer
REM
REM  Target: PC user FRESH install Windows 10/11. Tidak ada Python, tidak ada
REM  VC++, tidak ada apa-apa. Bat ini handle SEMUA dependency otomatis.
REM
REM  Yang di-install:
REM    1. Visual C++ Redistributable 2015-2022 (x64) via UAC
REM       + Bundled DLL fallback (kalau UAC ditolak, agent tetap jalan)
REM    2. uv (Astral Python toolchain, single binary)
REM    3. Python 3.12 (auto-download via uv)
REM    4. Source code dari GitHub (mantra-agent repo)
REM    5. PyTorch CPU + FastAPI + GFPGAN + rembg + 100+ deps via pip
REM    6. ffmpeg + ffprobe (untuk full-album audio merge)
REM    7. yt-dlp.exe (untuk YouTube download)
REM    8. Node.js 22 LTS portable (untuk bgutil-ytdlp-pot-provider)
REM    9. Smoke test: import torch, fail-fast kalau VC++ runtime broken
REM   10. Self-copy bat + VBS launcher + autostart registry
REM   11. Spawn agent tray (skip-if-running guard)
REM
REM  Total download: ~1.5 GB. Waktu: 5-10 menit.
REM  Subsequent runs: instant — langsung spawn tray.
REM ============================================================================

setlocal enabledelayedexpansion

set "ROOT=%LOCALAPPDATA%\MantraAgent"
set "VENV=%ROOT%\venv"
set "SRC=%ROOT%\src"
set "BIN=%ROOT%\bin"
set "TOOLS=%ROOT%\tools"
set "NODE_DIR=%ROOT%\node"
set "LOG=%ROOT%\setup.log"

if not exist "%ROOT%"  mkdir "%ROOT%"
if not exist "%TOOLS%" mkdir "%TOOLS%"
if not exist "%BIN%"   mkdir "%BIN%"

REM Source distribution. Always-latest pattern via GitHub main branch.
set "GITHUB_SRC_ZIP=https://github.com/wahanagiva/mantra-agent/archive/refs/heads/main.zip"

REM Detect "running from autostart" (silent mode, no pause/echo flourish needed)
set "IS_AUTOSTART=0"
if /i "%~f0"=="%ROOT%\MantraAgent.bat" (
    REM Triggered by VBS autostart launcher OR self-copy re-exec.
    REM If src already present + venv ready, this is autostart path.
    if exist "%SRC%\main.py" if exist "%VENV%\Scripts\python.exe" set "IS_AUTOSTART=1"
)

REM Fresh install banner (skip on autostart silent runs)
if "%IS_AUTOSTART%"=="0" (
    echo.
    echo ============================================================
    echo   Mantra Creative Agent - Installer
    echo ============================================================
    echo   Akan setup semua dependency otomatis ^(~1.5 GB^).
    echo   Estimasi waktu: 5-10 menit ^(tergantung internet^).
    echo.
    echo   JANGAN tutup window ini sampai selesai!
    echo ============================================================
    echo.
)

REM Reset log
echo. > "%LOG%"
echo Mantra Agent Bootstrap - Bulletproof Installer >> "%LOG%"
echo ROOT: %ROOT% >> "%LOG%"
echo Date: %DATE% %TIME% >> "%LOG%"
echo Mode: autostart=%IS_AUTOSTART% >> "%LOG%"
echo OS: >> "%LOG%"
ver >> "%LOG%" 2>&1
echo. >> "%LOG%"

REM Skip-if-running guard (top of file): kalau autostart trigger tapi tray
REM udah jalan dari sesi sebelumnya, langsung exit. Cegah double-spawn race.
if "%IS_AUTOSTART%"=="1" (
    powershell -NoProfile -Command "if (Get-Process pythonw -EA SilentlyContinue | Where-Object { try { $_.MainModule.FileName -like '*MantraAgent*src*' } catch { $false } }) { exit 0 } else { exit 1 }"
    if not errorlevel 1 (
        echo [autostart] Tray sudah jalan, skip. >> "%LOG%"
        exit /b 0
    )
)

REM ============================================================================
REM  STEP 1/9 - Pre-flight: connectivity check
REM ============================================================================
echo [1/9] Pre-flight checks...

curl -s --max-time 10 -o nul -w "%%{http_code}" https://github.com > "%TOOLS%\_net.txt" 2>nul
set /p NET_CODE=<"%TOOLS%\_net.txt" 2>nul
del "%TOOLS%\_net.txt" 2>nul
if not "%NET_CODE%"=="200" if not "%NET_CODE%"=="301" if not "%NET_CODE%"=="302" (
    echo   ERROR: github.com tidak bisa diakses ^(HTTP %NET_CODE%^).
    echo          Cek koneksi WiFi, proxy, atau corporate firewall.
    goto fail
)
echo   - internet: OK

REM Architecture check (x64 only)
if /i not "%PROCESSOR_ARCHITECTURE%"=="AMD64" if /i not "%PROCESSOR_ARCHITEW6432%"=="AMD64" (
    echo   ERROR: PC bukan x64. Mantra Agent cuma support Windows x64.
    echo          Arsitektur terdeteksi: %PROCESSOR_ARCHITECTURE%
    goto fail
)
echo   - arsitektur: x64 OK

REM Windows version check (informational only)
for /f "tokens=4-5 delims=. " %%I in ('ver') do set "WIN_VER=%%I.%%J"
echo   - Windows version: %WIN_VER%
echo.

REM ============================================================================
REM  STEP 2/9 - Visual C++ Redistributable 2015-2022 (x64)
REM  Strategy: ALWAYS attempt system install via UAC. Bundled DLLs are
REM  copied later as fallback (works even if UAC denied).
REM ============================================================================
echo [2/9] Visual C++ Redistributable 2015-2022 ^(x64^)...

set "VC_INSTALLED=0"
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" /v Installed >nul 2>&1
if not errorlevel 1 set "VC_INSTALLED=1"

if "%VC_INSTALLED%"=="1" (
    echo   - sudah terpasang di system, skip install
) else (
    echo   - belum terpasang, akan download + install
    set "VCR_EXE=%TOOLS%\vc_redist.x64.exe"

    echo   - downloading vc_redist.x64.exe ^(~14 MB^)...
    curl -L --retry 3 -s -o "!VCR_EXE!" "https://aka.ms/vs/17/release/vc_redist.x64.exe" >> "%LOG%" 2>&1

    if not exist "!VCR_EXE!" (
        echo   - WARNING: download gagal. Akan pakai bundled DLL fallback.
    ) else (
        echo.
        echo   ============================================================
        echo     PERHATIAN: AKAN MUNCUL UAC PROMPT
        echo   ============================================================
        echo     Klik YES untuk install Visual C++ Redistributable.
        echo     Kalau klik NO, agent tetap bisa jalan via bundled DLL,
        echo     jadi gak masalah kalau ditolak.
        echo   ============================================================
        echo.
        timeout /t 4 >nul

        REM Run elevated, wait for completion. /quiet /norestart prevents reboot.
        powershell -NoProfile -Command "try { Start-Process -FilePath '!VCR_EXE!' -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -Wait -ErrorAction Stop; exit 0 } catch { exit 1 }" >> "%LOG%" 2>&1

        del "!VCR_EXE!" 2>nul

        REM Re-check registry
        reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" /v Installed >nul 2>&1
        if not errorlevel 1 (
            echo   - VC++ Redist install: OK
        ) else (
            echo   - VC++ Redist install: SKIPPED ^(UAC ditolak / blocked^)
            echo   - Akan pakai bundled DLL fallback ^(juga aman^)
        )
    )
)
echo.

REM ============================================================================
REM  STEP 3/9 - uv (Astral Python toolchain)
REM ============================================================================
echo [3/9] uv toolchain...
if not exist "%TOOLS%\uv.exe" (
    echo   - downloading uv ^(~15 MB^)...
    curl -L --retry 3 -o "%TOOLS%\uv.zip" "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo   ERROR: uv download failed & goto fail )
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%TOOLS%\uv.zip' -DestinationPath '%TOOLS%' -Force" >> "%LOG%" 2>&1
    del "%TOOLS%\uv.zip" 2>nul
    if not exist "%TOOLS%\uv.exe" ( echo   ERROR: uv.exe missing after extract & goto fail )
    echo   - installed
) else (
    echo   - already present
)
echo.

REM ============================================================================
REM  STEP 4/9 - Python 3.12 venv (uv auto-downloads CPython if missing)
REM ============================================================================
echo [4/9] Python 3.12 venv...
if not exist "%VENV%\Scripts\python.exe" (
    echo   - creating venv ^(uv auto-downloads Python 3.12^)...
    "%TOOLS%\uv.exe" venv --python 3.12 "%VENV%" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo   ERROR: venv creation failed - check %LOG% & goto fail )
    echo   - created
) else (
    echo   - already exists
)
echo.

REM ============================================================================
REM  STEP 5/9 - Source code dari GitHub
REM ============================================================================
echo [5/9] Source code dari GitHub...
if not exist "%SRC%\main.py" (
    echo   - downloading source.zip...
    curl -L --retry 3 -o "%TOOLS%\src.zip" "%GITHUB_SRC_ZIP%" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo   ERROR: source download failed - check internet & goto fail )

    if exist "%TOOLS%\src_extract" rmdir /s /q "%TOOLS%\src_extract"
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%TOOLS%\src.zip' -DestinationPath '%TOOLS%\src_extract' -Force" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo   ERROR: source extract failed & goto fail )

    REM Move extracted folder (mantra-agent-main\*) into SRC
    for /f "delims=" %%D in ('dir /b /ad "%TOOLS%\src_extract"') do (
        move "%TOOLS%\src_extract\%%D" "%SRC%" >nul 2>&1
    )
    rmdir /s /q "%TOOLS%\src_extract" 2>nul
    del "%TOOLS%\src.zip" 2>nul

    if not exist "%SRC%\main.py" ( echo   ERROR: main.py missing after extract & goto fail )
    echo   - downloaded ^& extracted
) else (
    echo   - already present
)
echo.

REM ============================================================================
REM  STEP 6/9 - Python deps via pip (PyTorch CPU + FastAPI + GFPGAN + rembg)
REM  Re-runs only if requirements.txt hash changed or fastapi missing.
REM ============================================================================
echo [6/9] Python deps...
set "REQ_HASH_FILE=%VENV%\.req_hash"
set "REQ_HASH_NEW="
for /f "delims=" %%H in ('certutil -hashfile "%SRC%\requirements.txt" SHA256 ^| findstr /v ":"') do if not defined REQ_HASH_NEW set "REQ_HASH_NEW=%%H"
set "REQ_HASH_OLD="
if exist "%REQ_HASH_FILE%" set /p REQ_HASH_OLD=<"%REQ_HASH_FILE%"

set "NEEDS_PIP=0"
if not exist "%VENV%\Lib\site-packages\fastapi" set "NEEDS_PIP=1"
if not exist "%VENV%\Lib\site-packages\torch" set "NEEDS_PIP=1"
if not "%REQ_HASH_NEW%"=="%REQ_HASH_OLD%" set "NEEDS_PIP=1"

if "%NEEDS_PIP%"=="1" (
    echo   - installing/updating ^(~1 GB, lambat di first run, 5-10 min^)...
    "%TOOLS%\uv.exe" pip install --python "%VENV%\Scripts\python.exe" --index-strategy unsafe-best-match --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu -r "%SRC%\requirements.txt" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo   ERROR: pip install failed - check %LOG% & goto fail )
    echo !REQ_HASH_NEW!>"%REQ_HASH_FILE%"
    echo   - done
) else (
    echo   - up-to-date
)
echo.

REM ============================================================================
REM  STEP 6b/9 - Bundled VC++ DLL fallback (CRITICAL: must run AFTER pip install)
REM
REM  Python 3.8+ DLL loading rules: extension modules' dependencies are searched
REM  in (a) the .pyd/.dll's own directory, (b) System32, (c) os.add_dll_directory.
REM  NOT in venv\Scripts\ where python.exe lives.
REM
REM  PyTorch's c10.dll lives in venv\Lib\site-packages\torch\lib\. PyTorch calls
REM  os.add_dll_directory(torch_lib_path) before loading c10.dll, so c10's
REM  dependencies (vcruntime140.dll, msvcp140.dll, etc.) are searched in:
REM    1. torch\lib\          ← OUR copy lands here = guaranteed to be found
REM    2. System32             ← only if VC++ Redist installed system-wide
REM
REM  We copy to BOTH torch\lib\ (primary, for c10/torch_cpu) AND venv\Scripts\
REM  (secondary, for python.exe itself + ctypes loads from script dir).
REM ============================================================================
echo [6b/9] Bundled VC++ DLL fallback...
if not exist "%SRC%\runtime\vc\vcruntime140.dll" (
    echo   - WARNING: bundled DLLs tidak ada di source - relying on system VC++
    goto vcdll_skip
)

set "TORCH_LIB=%VENV%\Lib\site-packages\torch\lib"
if exist "%TORCH_LIB%" (
    copy /y "%SRC%\runtime\vc\*.dll" "%TORCH_LIB%\" >nul 2>&1
    echo   - copied to torch\lib\ ^(primary - next to c10.dll^)
) else (
    echo   - WARNING: torch\lib not found, skip primary copy
)

copy /y "%SRC%\runtime\vc\*.dll" "%VENV%\Scripts\" >nul 2>&1
echo   - copied to venv\Scripts ^(secondary fallback^)

:vcdll_skip
echo.

REM ============================================================================
REM  STEP 6c/9 - Smoke test: import torch
REM  Fail-fast kalau VC++ runtime broken (c10.dll WinError 1114).
REM  Both system VC++ install AND bundled DLLs already attempted by now.
REM ============================================================================
echo [6c/9] Smoke test ^(import torch^)...
"%VENV%\Scripts\python.exe" -c "import torch; print('torch', torch.__version__, 'OK')" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo.
    echo   ============================================================
    echo     ERROR: import torch GAGAL
    echo   ============================================================
    echo     Kemungkinan VC++ runtime broken / DLL load failed.
    echo     Detail di log: %LOG%
    echo.
    echo     MANUAL FIX:
    echo       1. Download dari https://aka.ms/vs/17/release/vc_redist.x64.exe
    echo       2. Double-click, klik YES pas UAC, install
    echo       3. Re-run MantraAgent.bat
    echo   ============================================================
    goto fail
)
echo   - torch import OK
echo.

REM ============================================================================
REM  STEP 7/9 - ffmpeg + yt-dlp + Node.js
REM ============================================================================
echo [7/9] Media tools...

REM 7a. ffmpeg + ffprobe
if not exist "%BIN%\ffmpeg.exe" (
    echo   - downloading ffmpeg ^(~32 MB^)...
    curl -L --retry 3 -o "%TOOLS%\ffmpeg.zip" "https://github.com/GyanD/codexffmpeg/releases/download/8.1/ffmpeg-8.1-essentials_build.zip" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo   ERROR: ffmpeg download failed & goto fail )
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%TOOLS%\ffmpeg.zip' -DestinationPath '%TOOLS%\ffmpeg_extract' -Force" >> "%LOG%" 2>&1
    for /f "delims=" %%D in ('dir /b /ad "%TOOLS%\ffmpeg_extract"') do (
        copy /y "%TOOLS%\ffmpeg_extract\%%D\bin\ffmpeg.exe" "%BIN%\" >nul
        copy /y "%TOOLS%\ffmpeg_extract\%%D\bin\ffprobe.exe" "%BIN%\" >nul
    )
    rmdir /s /q "%TOOLS%\ffmpeg_extract" 2>nul
    del "%TOOLS%\ffmpeg.zip" 2>nul
    if not exist "%BIN%\ffmpeg.exe" ( echo   ERROR: ffmpeg.exe missing after extract & goto fail )
    echo   - ffmpeg installed
) else (
    echo   - ffmpeg already present
)

REM 7b. yt-dlp standalone
if not exist "%BIN%\yt-dlp.exe" (
    echo   - downloading yt-dlp ^(~17 MB^)...
    curl -L --retry 3 -o "%BIN%\yt-dlp.exe" "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo   ERROR: yt-dlp download failed & goto fail )
    echo   - yt-dlp installed
) else (
    echo   - yt-dlp already present
)

REM 7c. Node.js portable (untuk bgutil-ytdlp-pot-provider YouTube bot bypass)
if not exist "%NODE_DIR%\node.exe" (
    echo   - downloading Node.js 22 LTS portable ^(~30 MB^)...
    curl -L --retry 3 -o "%TOOLS%\node.zip" "https://nodejs.org/dist/v22.11.0/node-v22.11.0-win-x64.zip" >> "%LOG%" 2>&1
    if errorlevel 1 ( echo   ERROR: Node.js download failed & goto fail )
    if exist "%TOOLS%\node_extract" rmdir /s /q "%TOOLS%\node_extract"
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '%TOOLS%\node.zip' -DestinationPath '%TOOLS%\node_extract' -Force" >> "%LOG%" 2>&1
    if exist "%NODE_DIR%" rmdir /s /q "%NODE_DIR%"
    for /f "delims=" %%D in ('dir /b /ad "%TOOLS%\node_extract"') do (
        move "%TOOLS%\node_extract\%%D" "%NODE_DIR%" >nul 2>&1
    )
    rmdir /s /q "%TOOLS%\node_extract" 2>nul
    del "%TOOLS%\node.zip" 2>nul
    if not exist "%NODE_DIR%\node.exe" ( echo   ERROR: node.exe missing after extract & goto fail )
    echo   - Node.js installed
) else (
    echo   - Node.js already present
)
echo.

REM ============================================================================
REM  STEP 8/9 - Self-install bat + VBS launcher + autostart registry
REM ============================================================================
echo [8/9] Setup autostart...

set "INSTALLED_BAT=%ROOT%\MantraAgent.bat"
set "VBS_LAUNCHER=%ROOT%\MantraAgent.vbs"

REM Self-copy bat to %ROOT% so autostart can find it (kalau user run dari
REM Downloads folder, file di sana bisa dipindah/hapus user kapan saja).
if /i not "%~f0"=="%INSTALLED_BAT%" (
    copy /y "%~f0" "%INSTALLED_BAT%" >nul 2>&1
    echo   - bat di-copy ke stable location
)

REM Write VBS launcher (silent autostart, no console flash on boot)
> "%VBS_LAUNCHER%" echo CreateObject("Wscript.Shell").Run """%INSTALLED_BAT%""", 0, False

REM Register HKCU\Run autostart (no admin needed)
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MantraAgent /t REG_SZ /d "wscript.exe \"%VBS_LAUNCHER%\"" /f >> "%LOG%" 2>&1
echo   - autostart registered ^(silent VBS launcher^)
echo.

REM ============================================================================
REM  STEP 9/9 - Spawn agent tray (skip if already running)
REM ============================================================================
echo [9/9] Starting agent tray...

REM Skip-if-running guard (race protection: user double-click + autostart fire)
powershell -NoProfile -Command "if (Get-Process pythonw -EA SilentlyContinue | Where-Object { try { $_.MainModule.FileName -like '*MantraAgent*src*' } catch { $false } }) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo   - tray sudah jalan, skip spawn
    goto :end
)

REM Spawn tray detached. Pass tool paths via env so handlers find them.
set "MANTRA_AGENT_FFMPEG=%BIN%\ffmpeg.exe"
set "MANTRA_AGENT_YTDLP=%BIN%\yt-dlp.exe"
set "PATH=%NODE_DIR%;%PATH%"
set "PYTHONPATH=%SRC%"
start "" /B "%VENV%\Scripts\pythonw.exe" "%SRC%\tray.py"
echo   - spawned

:end
echo.
echo ============================================================
echo   INSTALL SELESAI
echo ============================================================
echo   Agent jalan di background ^(tray icon kanan bawah^).
echo   Buka https://mantra.majutrah.co.id untuk pakai.
echo   Logs: %LOG%
echo ============================================================
echo.

REM Pause hanya kalau bukan autostart (jangan block silent boot run)
if "%IS_AUTOSTART%"=="0" (
    echo Window akan tertutup otomatis dalam 10 detik...
    timeout /t 10 >nul
)
exit /b 0

:fail
echo.
echo ============================================================
echo   INSTALL GAGAL
echo ============================================================
echo   Lihat log lengkap: %LOG%
echo   Kontak admin dengan log file di atas.
echo ============================================================
echo.
if "%IS_AUTOSTART%"=="0" pause
exit /b 1
