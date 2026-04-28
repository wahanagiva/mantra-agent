# ============================================================================
#  Mantra Agent - QUICK CLEAN UNINSTALL (PowerShell)
#  All-in-one wipe: kill processes + delete folder + clear registry
#  No admin required. ~5 seconds total.
#
#  Usage:
#    1. Right-click Cleanup.ps1 -> "Run with PowerShell"
#    2. Confirm with Y
#    3. Done
#
#  Or run from PowerShell:  .\Cleanup.ps1
# ============================================================================

$root = "$env:LOCALAPPDATA\MantraAgent"

Write-Host ""
Write-Host "=== Mantra Creative Agent - Quick Cleanup ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Akan menghapus:"
Write-Host "  1. Folder $root"
Write-Host "  2. Autostart entry HKCU\...\Run\MantraAgent"
Write-Host "  3. Running pythonw / python / wscript Mantra processes"
Write-Host ""

$confirm = Read-Host "Lanjut full cleanup? (y/N)"
if ($confirm -ne "y" -and $confirm -ne "Y") {
    Write-Host "Dibatalkan." -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 0
}

Write-Host ""
Write-Host "[1/3] Killing Mantra processes..." -ForegroundColor Cyan
$killed = 0
foreach ($name in @("pythonw", "python", "wscript")) {
    Get-Process $name -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmd -like "*MantraAgent*") {
                Write-Host "  killing $name PID $($_.Id)"
                Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
                $killed++
            }
        } catch {}
    }
}
if ($killed -eq 0) { Write-Host "  (no Mantra processes running)" }
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "[2/3] Removing autostart..." -ForegroundColor Cyan
$run = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
if ($run.MantraAgent) {
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'MantraAgent' -ErrorAction SilentlyContinue
    Write-Host "  removed: $($run.MantraAgent)"
} else {
    Write-Host "  (autostart entry tidak ada)"
}

Write-Host ""
Write-Host "[3/3] Removing folder $root ..." -ForegroundColor Cyan
if (Test-Path -LiteralPath $root) {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $root) {
        Write-Host "  WARNING: ada file ke-lock, retry..." -ForegroundColor Yellow
        Start-Sleep -Seconds 2
        Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $root) {
        Write-Host ""
        Write-Host "  *** GAGAL hapus folder ***" -ForegroundColor Red
        Write-Host "  File ke-lock. Coba:"
        Write-Host "    1. Close browser yang lagi buka mantra.majutrah.co.id"
        Write-Host "    2. Restart PC, lalu run Cleanup.ps1 lagi"
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "  done" -ForegroundColor Green
} else {
    Write-Host "  (folder tidak ada)"
}

Write-Host ""
Write-Host "=== CLEANUP SELESAI ===" -ForegroundColor Green
Write-Host ""
Write-Host "Mantra Agent sudah TOTAL bersih dari PC ini."
Write-Host "Buat install ulang: download MantraAgent.bat dari"
Write-Host "  https://github.com/wahanagiva/mantra-agent/releases/download/v1.4.0/MantraAgent.bat"
Write-Host ""
Read-Host "Press Enter to exit"
exit 0
