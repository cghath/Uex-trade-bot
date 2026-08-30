# Phase 1 of setting up a Windows dev machine for this repo — see docs/WINDOWS_DEV_SETUP.md.
# Run once in PowerShell. Installs Git, the Claude Code CLI, and Tailscale.

winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements

powershell -ExecutionPolicy Bypass -Command "irm https://claude.claude.com/install.ps1 | iex"

winget install --id Tailscale.Tailscale -e --source winget --accept-package-agreements --accept-source-agreements

Write-Host "`nPhase 1 done. Close this window, open a new PowerShell, then run windows-setup-phase2.ps1." -ForegroundColor Yellow
