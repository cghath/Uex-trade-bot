# Setting up a second Windows dev machine

A repeatable path for bringing another Windows machine onto this repo, with SSH access
back to wherever you manage/deploy the bot (see "Deploying to a Raspberry Pi 5" in
`README.md` if that's a Pi). Two scripts handle the automatable parts; a short list of
steps has to be done by hand — a sign-in, a file that's deliberately not in git, or a
step only you can complete on that machine.

## Phase 1 — install the tools

Run once in PowerShell:

```powershell
.\scripts\windows-setup-phase1.ps1
```

Installs Git, the Claude Code CLI, and Tailscale via `winget`. **Close the PowerShell
window and open a new one afterward** — Git won't be on `PATH` in the same window until
you do.

## Phase 2 — configure and clone

Run in the *new* PowerShell window:

```powershell
.\scripts\windows-setup-phase2.ps1
```

Enables the `ssh-agent` service, generates this machine's own `ed25519` key (skipped if
one already exists), prints the public key, clones the repo into
`Documents\Uex-trade-bot`, and checks out `TestBranch` — this repo's active-development
branch (see `PROJECT_CONTEXT.md`; `main` is production-only).

## Then — finish by hand

1. **Sign into Tailscale** from its tray icon, using the same account as your other
   machine(s).
2. **Add this machine's new public key** (printed at the end of Phase 2) to
   `~/.ssh/authorized_keys` on whatever host you SSH into for this project. Run this from
   a machine that already has access there, pasting the whole public-key line:
   ```
   ssh <user>@<host> "echo '<paste the public key>' >> ~/.ssh/authorized_keys"
   ```
3. **Get `CLAUDE.local.md` onto this machine.** It's gitignored on purpose (see
   `.gitignore`) — personal/local setup notes (real hostnames, machine-specific paths,
   anything not meant for git history) belong there, not in a committed file. Copy it in
   by hand, next to `CLAUDE.md`.
4. **Launch Claude Code**: `cd` into the cloned folder and run `claude` — first launch
   opens a browser to sign in with your Anthropic account.
5. **Verify SSH connectivity** to your deployment host now that the key's been added.
