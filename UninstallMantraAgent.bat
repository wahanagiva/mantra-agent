@echo off
REM ============================================================================
REM  Mantra Creative Agent - FULL UNINSTALLER
REM  Hapus TOTAL: agent + venv + Python + source + ffmpeg + yt-dlp + models
REM             + autostart + registry. Untuk re-install fresh.
REM  No admin required.
REM ============================================================================

setlocal enabledelayedexpansion

set "ROOT=%LOCALAPPDATA%\MantraAgent"

echo.
echo === Mantra Creative Agent - FULL UNINSTALLER ===
echo.
echo Akan menghapus SEMUA:
echo   - Folder: %ROOT% ^(termasuk venv, source, ffmpeg, yt-dlp, models cache^)
echo   - Autostart entry HKCU\...\Run\MantraAgent
echo   - Running agent processes
echo.

set "HAS_FOLDER=0"
set "HAS_REG=0"
if exist "%ROOT%" set "HAS_FOLDER=1"
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MantraAgent >nul 2>&1
if not errorlevel 1 set "HAS_REG=1"

if "%HAS_FOLDER%"=="0" if "%HAS_REG%"=="0" (
    echo [info] Mantra Agent belum di-install di PC ini.
    pause
    exit /b 0
)

set /p "CONFIRM=Lanjut FULL UNINSTALL? (y/N): "
if /i not "%CONFIRM%"=="y" (
    echo Dibatalkan.
    pause
    exit /b 0
)

echo.
echo [1/3] Stop semua agent processes...
powershell -NoProfile -Command "Get-Process pythonw,python -ErrorAction SilentlyContinue | Where-Object { try { $_.MainModule.FileName -like '*MantraAgent*' } catch { $false } } | ForEach-Object { Write-Host ('  killing ' + $_.Name + ' PID ' + $_.Id); Stop-Process -Id $_.Id -Force }"
timeout /t 3 >nul

echo.
echo [2/3] Hapus autostart registry...
if "%HAS_REG%"=="1" (
    reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MantraAgent /f >nul 2>&1
    echo   removed
) else (
    echo   ^(autostart entry tidak ada - skip^)
)

echo.
echo [3/3] Hapus folder %ROOT% ...
if "%HAS_FOLDER%"=="1" (
    rmdir /s /q "%ROOT%" 2>nul
    if exist "%ROOT%" (
        echo   ada file ke-lock, retry pake powershell...
        powershell -NoProfile -Command "Remove-Item -LiteralPath '%ROOT%' -Recurse -Force -ErrorAction SilentlyContinue"
        timeout /t 2 >nul
    )
    if exist "%ROOT%" (
        echo.
        echo   *** GAGAL hapus folder ***
        echo   File ke-lock. Coba:
        echo     1. Close browser yang lagi buka mantra.majutrah.co.id
        echo     2. Close File Explorer yang lagi browse %ROOT%
        echo     3. Restart PC, lalu run uninstaller lagi
        echo.
        pause
        exit /b 1
    )
    echo   done
) else (
    echo   ^(folder tidak ada - skip^)
)

echo.
echo === UNINSTALL SELESAI ===
echo.
echo Mantra Agent sudah TOTAL bersih dari PC ini.
echo Buat install ulang: download MantraAgent.bat lagi dari
echo   https://github.com/wahanagiva/mantra-agent/raw/main/MantraAgent.bat
echo atau dari website mantra.majutrah.co.id (tombol Download).
echo.
pause
exit /b 0
