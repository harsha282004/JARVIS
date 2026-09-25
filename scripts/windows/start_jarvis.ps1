<#
.SYNOPSIS  Starts JARVIS in the background (system tray) with no console window.
#>
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "JARVIS is not installed here. Run scripts\windows\install_jarvis.ps1 first." }

Start-Process -FilePath $pythonw -ArgumentList "-m", "desktop.launcher" -WorkingDirectory $root -WindowStyle Hidden
Write-Host "JARVIS is starting. Look for the tray icon (it turns green when JARVIS is online). Log: $root\logs\jarvis.log"
