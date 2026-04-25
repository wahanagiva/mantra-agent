@echo off
REM ============================================================================
REM  Mantra Creative Agent - UNINSTALLER
REM  Stops agent + removes install folder + clears autostart + (optional) models
REM  No admin required.
REM ============================================================================

setlocal enabledelayedexpansion

set "ROOT=%LOCALAPPDATA%\MantraAgent"

echo.
echo === Mantra Creative Agent Uninstaller ===
echo.
echo Akan menghapus:
echo   - Folder: %ROOT%
echo   - Autostart entry (HKCU\...\Run\MantraAgent)
echo   - Running agent processes (pythonw)
echo.

if not exist "%ROOT%" (
    echo [info] Folder %ROOT% tidak ada - Mantra Agent belum di-install.
    pause
    exit /b 0
)

REM Show size of what will be deleted
for /f "tokens=3" %%S in ('dir /s "%ROOT%" ^| findstr /C:"File(s)"') do set "SIZE=%%S"
echo Total size: %SIZE% bytes
echo.

set /p "CONFIRM=Lanjut uninstall? (y/N): "
if /i not "%CONFIRM%"=="y" (
    echo Dibatalkan.
    pause
    exit /b 0
)

echo.
echo [1/4] Stopping running agent processes...
REM Use PowerShell to filter pythonw/python processes by path (reliable).
powershell -NoProfile -Command "Get-Process pythonw,python -ErrorAction SilentlyContinue | Where-Object { try { $_.MainModule.FileName -like '*MantraAgent*' } catch { $false } } | ForEach-Object { Write-Host ('  killing ' + $_.Name + ' PID ' + $_.Id); Stop-Process -Id $_.Id -Force }"
timeout /t 3 >nul

echo.
echo [2/4] Removing autostart registry entry...
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MantraAgent >nul 2>&1
if errorlevel 1 (
    echo   ^(autostart entry tidak ada - skip^)
) else (
    reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MantraAgent /f >nul 2>&1
    echo   removed
)

echo.
echo [3/4] Models cache (%ROOT%\models)
echo   Model AI (~333 MB) di folder ini bakal di-redownload kalau install lagi.
set /p "KEEPMODELS=Keep models? (y/N): "
if /i "%KEEPMODELS%"=="y" (
    if exist "%ROOT%\models" (
        if not exist "%TEMP%\MantraAgent_models_backup" (
            move "%ROOT%\models" "%TEMP%\MantraAgent_models_backup" >nul 2>&1
            echo   models backed up to %%TEMP%%\MantraAgent_models_backup
        )
    )
)

echo.
echo [4/4] Removing %ROOT% ...
rmdir /s /q "%ROOT%" 2>nul
if exist "%ROOT%" (
    echo   WARNING: folder masih ada - mungkin file ke-lock. Coba close browser/explorer + run lagi.
    pause
    exit /b 1
)
echo   done

REM Restore models if user chose to keep
if /i "%KEEPMODELS%"=="y" (
    if exist "%TEMP%\MantraAgent_models_backup" (
        mkdir "%ROOT%" >nul 2>&1
        move "%TEMP%\MantraAgent_models_backup" "%ROOT%\models" >nul 2>&1
        echo   models restored to %ROOT%\models
    )
)

echo.
echo === Uninstall complete ===
echo.
echo Untuk install ulang: jalankan MantraAgent.bat
echo.
pause
exit /b 0
