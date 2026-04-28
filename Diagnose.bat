@echo off
REM ============================================================================
REM  Mantra Agent Diagnostic Tool
REM  Collects system state + logs into 1 file - kirim file ini ke admin biar
REM  bisa diagnose masalah cepat.
REM ============================================================================

setlocal enabledelayedexpansion
set "ROOT=%LOCALAPPDATA%\MantraAgent"
set "REPORT=%USERPROFILE%\Desktop\MantraDiagnose_%COMPUTERNAME%_%RANDOM%.txt"

echo Collecting diagnostic info...
echo Output: %REPORT%
echo.

(
echo === MANTRA AGENT DIAGNOSTIC REPORT ===
echo Generated: %DATE% %TIME%
echo Computer: %COMPUTERNAME%
echo User: %USERNAME%
echo.

echo === OS INFO ===
ver
systeminfo 2^>nul ^| findstr /C:"OS Name" /C:"OS Version" /C:"System Type" /C:"Total Physical Memory"
echo.

echo === DISK SPACE on C: ===
fsutil volume diskfree C: 2^>nul
echo.

echo === FOLDER STATE %ROOT% ===
if exist "%ROOT%" (
    dir "%ROOT%" 2^>nul
    echo.
    echo === SUBFOLDER SIZES ===
    for /d %%D in ^("%ROOT%\*"^) do echo   %%~nD
) else (
    echo MantraAgent folder TIDAK ADA - bat belum berhasil install
)
echo.

echo === KEY FILES CHECK ===
for %%F in ^(
    "%ROOT%\MantraAgent.bat"
    "%ROOT%\MantraAgent.vbs"
    "%ROOT%\setup.log"
    "%ROOT%\venv\Scripts\python.exe"
    "%ROOT%\venv\Scripts\pythonw.exe"
    "%ROOT%\src\main.py"
    "%ROOT%\src\tray.py"
    "%ROOT%\bin\ffmpeg.exe"
    "%ROOT%\bin\yt-dlp.exe"
    "%ROOT%\node\node.exe"
    "%ROOT%\tools\uv.exe"
^) do (
    if exist %%F ^(echo   EXIST  %%~F^) else ^(echo   MISSING %%~F^)
)
echo.

echo === RUNNING PROCESSES ^(MantraAgent-related^) ===
tasklist /FI "IMAGENAME eq pythonw.exe" /V 2^>nul
echo ---
tasklist /FI "IMAGENAME eq python.exe" /V 2^>nul
echo ---
tasklist /FI "IMAGENAME eq wscript.exe" /V 2^>nul
echo.

echo === PORT 5555 ===
netstat -ano ^| findstr ":5555"
echo.

echo === REGISTRY AUTOSTART ===
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v MantraAgent 2^>nul
if errorlevel 1 echo   ^(autostart NOT registered^)
echo.

echo === VC++ REDIST STATUS ===
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" /v Installed 2^>nul
if errorlevel 1 ^(
    reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" /v Installed 2^>nul
    if errorlevel 1 echo   VC++ Redist x64: NOT INSTALLED
^)
echo.

echo === HEALTH CHECK ===
curl -s --max-time 5 http://127.0.0.1:5555/api/health 2^>nul
echo.

echo === NETWORK CHECK ===
echo Testing github.com...
curl -s --max-time 5 -o nul -w "  HTTP: %%{http_code}, Time: %%{time_total}s" https://github.com 2^>nul
echo.
echo Testing pypi.org...
curl -s --max-time 5 -o nul -w "  HTTP: %%{http_code}" https://pypi.org 2^>nul
echo.
echo Testing nodejs.org...
curl -s --max-time 5 -o nul -w "  HTTP: %%{http_code}" https://nodejs.org 2^>nul
echo.
echo.

echo === DEFENDER STATUS ===
powershell -NoProfile -Command "Get-MpComputerStatus | Select-Object AntivirusEnabled, RealTimeProtectionEnabled, AntivirusSignatureLastUpdated, ComputerState | Format-List" 2^>nul
echo.
echo === DEFENDER QUARANTINE ^(MantraAgent-related^) ===
powershell -NoProfile -Command "Get-MpThreatDetection | Where-Object { $_.Resources -like '*MantraAgent*' } | Select-Object DomainUser, ThreatID, ProcessName, Resources, ActionSuccess | Format-List" 2^>nul
echo.

echo === SETUP LOG ^(last 50 lines^) ===
if exist "%ROOT%\setup.log" ^(
    powershell -NoProfile -Command "Get-Content '%ROOT%\setup.log' -Tail 50"
^) else ^(
    echo   ^(setup.log tidak ada^)
^)
echo.

echo === TRAY LOG ===
if exist "%ROOT%\logs\tray.log" ^(
    powershell -NoProfile -Command "Get-Content '%ROOT%\logs\tray.log' -Tail 30"
^) else ^(
    echo   ^(tray.log tidak ada^)
^)
echo.

echo === TRAY AGENT LOG ^(stderr dari spawn agent^) ===
if exist "%ROOT%\logs\tray_agent.log" ^(
    powershell -NoProfile -Command "Get-Content '%ROOT%\logs\tray_agent.log' -Tail 30"
^) else ^(
    echo   ^(tray_agent.log tidak ada^)
^)
echo.

echo === AGENT LOG ===
if exist "%ROOT%\logs\agent.log" ^(
    powershell -NoProfile -Command "Get-Content '%ROOT%\logs\agent.log' -Tail 50"
^) else ^(
    echo   ^(agent.log tidak ada^)
^)
echo.

echo === END OF REPORT ===
) > "%REPORT%" 2>&1

echo.
echo Report saved to: %REPORT%
echo.
echo Kirim file ini ke admin buat diagnose masalah cepat.
echo File ini di Desktop kamu, nama: MantraDiagnose_%COMPUTERNAME%_%RANDOM%.txt
echo.
pause
exit /b 0
