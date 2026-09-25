<#
.SYNOPSIS  Updates JARVIS: stop, pull the latest code, update dependencies, migrate the database, start again.
.PARAMETER NoStart   do not start JARVIS afterwards
.PARAMETER NoPull    skip "git pull" (for example after copying new files in by hand)
#>
param([switch]$NoStart, [switch]$NoPull)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

& (Join-Path $PSScriptRoot "stop_jarvis.ps1") | Out-Host
if ($LASTEXITCODE -ne 0) { throw "JARVIS did not stop; not updating while it is running." }

if (-not $NoPull -and (Test-Path (Join-Path $root ".git"))) {
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull failed (local changes or a diverged branch). Resolve that and re-run." }
}
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
& $python -m alembic -c database\alembic.ini upgrade head
if ($LASTEXITCODE -ne 0) { throw "Database migration failed. JARVIS was not restarted; your data is unchanged by the failed step." }

if (-not $NoStart) { & (Join-Path $PSScriptRoot "start_jarvis.ps1") }
Write-Host "Update finished."
