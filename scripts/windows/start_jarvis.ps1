<#
.SYNOPSIS  Starts JARVIS in the background (system tray) with no console window.

.DESCRIPTION
  Uses the project's own interpreter (.venv\Scripts\pythonw.exe) and the launcher script, so it works from any directory, from a plain PowerShell window or
  from Task Scheduler: it does not need VS Code or an activated virtual environment. If JARVIS is already running (for example Windows already started it) no
  second copy is started. It waits until the runtime is really up and reports the result.

.PARAMETER Source       recorded in the startup log as STARTUP_SOURCE (default: manual)
.PARAMETER WaitSeconds  how long to wait for JARVIS to come up (default 40)
#>
param([string]$Source = "manual", [int]$WaitSeconds = 40)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$launcher = Join-Path $root "scripts\windows\jarvis_launcher.pyw"
if (-not (Test-Path $pythonw)) { throw "JARVIS is not installed here. Run scripts\windows\install_jarvis.ps1 first." }

& $python $launcher --status *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "JARVIS is already running. Nothing was started (Windows auto-start and a manual start never create two copies)."
    exit 0
}

Start-Process -FilePath $pythonw -ArgumentList "`"$launcher`"", "--startup-source", $Source -WorkingDirectory $root -WindowStyle Hidden
$deadline = (Get-Date).AddSeconds($WaitSeconds)
do {
    Start-Sleep -Milliseconds 700
    & $python $launcher --status *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "JARVIS is running. Look for the tray icon (it turns green when JARVIS is online). Log: $root\logs\jarvis.log"
        Write-Host "Check everything with:  $python scripts\jarvis_status.py"
        exit 0
    }
} while ((Get-Date) -lt $deadline)
Write-Warning "JARVIS did not come up within $WaitSeconds seconds. See $root\logs\jarvis.log and $root\logs\jarvis-crash.log."
exit 1
