<#
.SYNOPSIS  Asks the running JARVIS to shut down gracefully (releases the microphone, stops services, closes connections).
.PARAMETER TimeoutSeconds  how long to wait for the process to exit (default 30). JARVIS is never force-killed by this script.
#>
param([int]$TimeoutSeconds = 30)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "JARVIS is not installed here." }

& $python -m desktop.launcher --stop
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    $running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match "desktop\.launcher" -and $_.CommandLine -notmatch "--stop" -and $_.CommandLine -notmatch "enable-startup|disable-startup|startup-status" }
    if (-not $running) { Write-Host "JARVIS has stopped."; exit 0 }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)
Write-Warning "JARVIS is still running after $TimeoutSeconds seconds. Use the tray menu (Exit) or check logs\jarvis.log. Nothing was force-killed."
exit 1
