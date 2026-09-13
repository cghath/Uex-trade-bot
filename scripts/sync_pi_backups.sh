#!/usr/bin/env bash
# Run this from the PC (git-bash), NOT on the Pi - unlike every other script in this
# directory, it pulls files ONTO the machine it runs on, so it only makes sense run from
# wherever you actually want the archive to live.
#
# Copies every backups/pi/<snapshot> directory scripts/deploy_and_backup.sh has left on
# the Pi down to a local backups/pi-archive/ (skipping any already copied down from a
# previous run), then prunes the Pi's own copies to the newest keep_count - matching this
# project's standing preference to keep the Pi's own storage footprint minimal and
# accumulate history on the PC instead (see CLAUDE.local.md). Never deletes a Pi-side
# snapshot until every snapshot is confirmed present in the local archive first.
#
# Usage: scripts/sync_pi_backups.sh [keep_count]
#   keep_count defaults to 2 - revert_last_deploy.sh only ever needs the single newest
#   snapshot by default, so 2 leaves one extra fallback if that newest one is ever bad or
#   incomplete, without letting the Pi's copy grow unbounded like it did before this script
#   existed (52 snapshots / 1.7GB, found 2026-09-13).
set -euo pipefail

PI_HOST="${PI_HOST:-cheeno@arkwatcher}"
PI_REPO="${PI_REPO:-/home/cheeno/uex-trade-bot}"
KEEP_COUNT="${1:-2}"

# Windows OpenSSH's own client, not whatever `ssh`/`scp` resolve to on PATH - git-bash's
# bundled (Cygwin) ssh doesn't talk to the Windows OpenSSH Authentication Agent service
# the way ssh.exe does, so it can't reach the key that actually authenticates here even
# though the same key file is on disk either way. Matches the exact binary this project's
# deploy/revert steps already invoke from PowerShell.
SSH="/c/Windows/System32/OpenSSH/ssh.exe"
SCP="/c/Windows/System32/OpenSSH/scp.exe"

REPO_ROOT="$(git rev-parse --show-toplevel)"
ARCHIVE_ROOT="$REPO_ROOT/backups/pi-archive"
mkdir -p "$ARCHIVE_ROOT"

echo "Listing backups on the Pi..."
mapfile -t REMOTE_DIRS < <("$SSH" -o BatchMode=yes "$PI_HOST" "ls -1 '$PI_REPO/backups/pi' 2>/dev/null || true")

if [ "${#REMOTE_DIRS[@]}" -eq 0 ]; then
    echo "No backups found on the Pi - nothing to sync."
    exit 0
fi

copied=0
for dir in "${REMOTE_DIRS[@]}"; do
    [ -n "$dir" ] || continue
    if [ -d "$ARCHIVE_ROOT/$dir" ]; then
        continue  # already archived from a previous run
    fi
    echo "Archiving $dir..."
    "$SCP" -rq "$PI_HOST:$PI_REPO/backups/pi/$dir" "$ARCHIVE_ROOT/$dir"
    copied=$((copied + 1))
done
echo "Archived $copied new snapshot(s) to $ARCHIVE_ROOT (${#REMOTE_DIRS[@]} total on the Pi)."

# Only prune once every remote snapshot is confirmed present locally - never delete the
# Pi's only copy of something whose scp above silently failed to complete.
missing=0
for dir in "${REMOTE_DIRS[@]}"; do
    [ -n "$dir" ] || continue
    if [ ! -d "$ARCHIVE_ROOT/$dir" ]; then
        echo "Missing archive copy of $dir - refusing to prune anything this run." >&2
        missing=1
    fi
done
if [ "$missing" -eq 1 ]; then
    exit 1
fi

echo "Pruning the Pi to its newest $KEEP_COUNT snapshot(s)..."
"$SSH" -o BatchMode=yes "$PI_HOST" \
    "cd '$PI_REPO/backups/pi' && ls -1dt */ 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) | xargs -r rm -rf --"

echo "Done. Pi now keeps its newest $KEEP_COUNT backup(s); full history lives in $ARCHIVE_ROOT."
