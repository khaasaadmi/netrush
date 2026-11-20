# Turns a spare Windows 10/11 laptop into the NetRush server, from nothing.
# Open PowerShell **as Administrator** (Start > type PowerShell > right-click > Run as administrator) and run:
#
#     powershell -ExecutionPolicy Bypass -File install.ps1
#
# (Download this one file from GitHub first, or copy it over on a USB stick.)
#
# It installs Python, Git and Cloudflare's tunnel program with winget, downloads NetRush into C:\netrush (or updates
# it if it's already there), creates the settings file and opens it for you. When you've filled the settings in,
# it hands over to setup.ps1, which makes NetRush start at boot.

$ErrorActionPreference = "Stop"
$dir = "C:\netrush"
$repo = "https://github.com/khaasaadmi/netrush.git"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Please run PowerShell as Administrator (right-click > Run as administrator)." }
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again."
}

function Install-App($id, $name) {
    Write-Host "Installing $name ..." -ForegroundColor Cyan
    winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements --scope machine
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {   # -1978335189 = already installed
        winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements
    }
}

Install-App "Python.Python.3.12" "Python 3.12"
Install-App "Git.Git" "Git"
Install-App "Cloudflare.cloudflared" "Cloudflare Tunnel (cloudflared)"

# pick up the new programs without opening a new window
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")

if (Test-Path (Join-Path $dir ".git")) {
    Write-Host "NetRush already in ${dir}, updating" -ForegroundColor Cyan
    git -C $dir pull --ff-only
} else {
    git clone $repo $dir
}
if ($LASTEXITCODE -ne 0) { throw "Couldn't download NetRush. If the GitHub repository is private, sign in when Git asks." }

$envFile = Join-Path $dir ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $dir ".env.example") $envFile
    Write-Host ""
    Write-Host "Opening the settings file. Fill in at least: ADMIN_PHONE, ADMIN_PASSWORD, RECEPTION_PHONE," -ForegroundColor Yellow
    Write-Host "RECEPTION_PASSWORD, ARENA_PHONE, ARENA_ADDRESS, ZK_IP, ZK_COMM_KEY and BACKUP_DIR. Save and close it." -ForegroundColor Yellow
    Start-Process notepad.exe $envFile -Wait
}

& powershell -ExecutionPolicy Bypass -File (Join-Path $dir "deploy\windows\setup.ps1")
