# One-time setup of the NetRush server on a Windows laptop / PC at the arena.
# Run in PowerShell **as Administrator**, from the netrush folder:
#
#     powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1
#
# It: checks Python, installs the ZKTeco library, stops the machine sleeping, and registers a
# task that starts NetRush at boot (even before anyone logs in) and restarts it if it stops.

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Write-Host "NetRush folder: $root"

# --- must be Administrator
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Please run PowerShell as Administrator (right-click > Run as administrator)." }

# --- Python from python.org, installed for all users (the Microsoft Store one can't run at boot)
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py -or $py -like "*WindowsApps*") {
    throw ("Install Python 3.12+ from https://www.python.org/downloads/ and tick 'Install for all users' and " +
           "'Add python.exe to PATH'. Then open a new Administrator PowerShell and run this again.")
}
Write-Host "Python: $py"
& $py -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10 or newer needed'"

# --- the ZKTeco terminal library
& $py -m pip install --upgrade pyzk
if ($LASTEXITCODE -ne 0) { throw "Couldn't install pyzk (check the internet connection)." }

# --- settings file
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "Created .env from .env.example. Fill it in (see DEPLOY.md), then run this script again." -ForegroundColor Yellow
}

# --- never sleep or hibernate on mains power, and keep running with the lid closed
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
Write-Host "Power: sleep off on mains, lid-close does nothing."

# --- start at boot, restart on failure
New-Item -ItemType Directory -Force (Join-Path $root "data\logs") | Out-Null
$log = Join-Path $root "data\logs\server.log"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $root `
    -Argument "/c `"`"$py`" server.py >> `"$log`" 2>&1`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "NetRush" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "NetRush"
Start-Sleep -Seconds 4

try {
    $r = Invoke-WebRequest -UseBasicParsing http://localhost:8000/healthz -TimeoutSec 5
    Write-Host "NetRush is running: $($r.Content)" -ForegroundColor Green
} catch {
    Write-Host "NetRush didn't answer yet. Look at $log for the reason." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Next: install the Cloudflare Tunnel (DEPLOY.md, step 3)."
