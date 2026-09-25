<#
.SYNOPSIS  Stops JARVIS and removes its start-with-Windows registration.

.DESCRIPTION
  Removes the Startup shortcut and the "JARVIS" Scheduled Task (if present) and stops the running instance.
  It does NOT delete your database, .env, models, documents or the project folder.

.PARAMETER RemoveLocalState  also delete JARVIS's local state folder (.jarvis: privacy mode, preferences, notification history,
                             audit log, timeline and OAuth tokens). Asks for confirmation unless -Force is given.
.PARAMETER Force             do not ask for confirmation when removing local state
#>
param([switch]$RemoveLocalState, [switch]$Force)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (Test-Path $python) {
    & (Join-Path $PSScriptRoot "stop_jarvis.ps1") | Out-Host
    & $python -m desktop.launcher --disable-startup
}
if (Get-ScheduledTask -TaskName "JARVIS" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "JARVIS" -Confirm:$false
    Write-Host "Scheduled Task 'JARVIS' removed."
}

if ($RemoveLocalState) {
    $state = Join-Path $root ".jarvis"
    if (Test-Path $state) {
        $go = $Force
        if (-not $go) { $go = (Read-Host "Delete $state (including saved sign-in tokens)? Type yes to confirm") -eq "yes" }
        if ($go) { Remove-Item -Recurse -Force $state; Write-Host "Local state removed." } else { Write-Host "Local state kept." }
    }
}
Write-Host "JARVIS will no longer start with Windows. Your database, .env and models were not touched."
