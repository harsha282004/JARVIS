<#
.SYNOPSIS
  Installs JARVIS on this Windows account so it starts with Windows, without VS Code.

.DESCRIPTION
  1. checks Python (3.11 or newer)     2. creates .venv and installs requirements.txt
  3. creates .env from .env.example (never overwrites an existing .env)
  4. creates the logs and state folders
  5. applies database migrations (alembic upgrade head) and checks the database connection
  6. registers start-with-Windows: a Startup-folder shortcut (default) or a Scheduled Task that also restarts JARVIS if the
     whole process ever dies (-UseScheduledTask)

  Nothing here needs administrator rights. It never touches your database contents, .env values or models.

.PARAMETER NoStartup         install only; do not register start-with-Windows
.PARAMETER SkipMigrations    do not run alembic (for example when PostgreSQL is not set up yet)
.PARAMETER UseScheduledTask  register a per-user logon Scheduled Task (with restart on failure) instead of the Startup shortcut
.PARAMETER Python            python launcher to use for the venv (default: python)
#>
[CmdletBinding()]
param(
    [switch]$NoStartup,
    [switch]$SkipMigrations,
    [switch]$UseScheduledTask,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
Write-Host "JARVIS install folder: $root"

# 1. Python
try { $version = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])" } catch { throw "Python was not found. Install Python 3.11+ from python.org and re-run." }
if ([version]$version -lt [version]"3.11") { throw "Python $version found, but JARVIS needs 3.11 or newer." }
Write-Host "Python $version OK"

# 2. Virtual environment and dependencies
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment (.venv)..."
    & $Python -m venv (Join-Path $root ".venv")
}
Write-Host "Installing dependencies (this can take a few minutes the first time)..."
& $venvPython -m pip install --upgrade pip | Out-Null
& $venvPython -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed. See the messages above." }

# 3. Configuration
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "Created .env from .env.example. Open it and set DATABASE_URL and the model paths before first use."
} else {
    Write-Host ".env already exists; left unchanged."
}

# 4. Folders
New-Item -ItemType Directory -Force (Join-Path $root "logs"), (Join-Path $root ".jarvis") | Out-Null

# 5. Database
if (-not $SkipMigrations) {
    & $venvPython -m alembic -c (Join-Path $root "database\alembic.ini") upgrade head
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Migrations failed. Is PostgreSQL running and DATABASE_URL correct? JARVIS will start, but tasks, reminders and memory need the database. Re-run this script afterwards."
    } else {
        & $venvPython (Join-Path $root "scripts\check_db.py")
    }
}

# 6. Start with Windows
if (-not $NoStartup) {
    if ($UseScheduledTask) {
        $pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m desktop.launcher" -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
        Register-ScheduledTask -TaskName "JARVIS" -Action $action -Trigger $trigger -Settings $settings -Description "JARVIS personal assistant (starts at logon, restarts on failure)" -Force | Out-Null
        & $venvPython -m desktop.launcher --disable-startup | Out-Null   # avoid starting twice
        Write-Host "Scheduled Task 'JARVIS' registered (starts at logon, restarts up to 3 times if the process dies)."
    } else {
        & $venvPython -m desktop.launcher --enable-startup
    }
}

Write-Host ""
Write-Host "Done. Start JARVIS now with:  .\scripts\windows\start_jarvis.ps1"
Write-Host "It runs in the system tray. Dashboard: http://127.0.0.1:8000/dashboard (only while JARVIS is running)."
