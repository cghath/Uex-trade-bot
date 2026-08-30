# Phase 2 of setting up a Windows dev machine for this repo — see docs/WINDOWS_DEV_SETUP.md.
# Run in a *new* PowerShell window (so Phase 1's installs are on PATH).
# Enables SSH, generates this machine's own key, clones the repo, and checks out TestBranch
# — this repo's active-development branch (see PROJECT_CONTEXT.md).

Set-Service -Name ssh-agent -StartupType Automatic
Start-Service ssh-agent

$keyPath = "$env:USERPROFILE\.ssh\id_ed25519"
if (-not (Test-Path $keyPath)) {
    ssh-keygen -t ed25519 -C $env:COMPUTERNAME -f $keyPath
} else {
    Write-Host "Key already exists at $keyPath - skipping." -ForegroundColor Yellow
}

Write-Host "`n=== Public key: send this to whoever/whatever manages SSH access to your deployment host, so it can be added to that host's authorized_keys ===" -ForegroundColor Cyan
Get-Content "$keyPath.pub"

Set-Location "$env:USERPROFILE\Documents"
git clone https://github.com/cghath/Uex-trade-bot.git
Set-Location "Uex-trade-bot"
git checkout TestBranch

Write-Host "`nCloned and on TestBranch. Remaining manual steps: Tailscale sign-in, copy CLAUDE.local.md in, then run 'claude'." -ForegroundColor Yellow
