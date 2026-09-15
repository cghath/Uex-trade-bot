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
# snapshot until every snapshot is confirmed present AND COMPLETE in the local archive
# first - see is_complete_backup below for what "complete" means and the real bug this
# closes.
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

# Audit-confirmed defect: validated before it reaches arithmetic/tail below - a malformed
# or zero value there would silently prune the newest snapshot(s) too, not just old ones
# (tail -n +1 with keep_count=0 keeps nothing at all).
case "$KEEP_COUNT" in
    ''|*[!0-9]*)
        echo "keep_count must be a non-negative integer, got: '$KEEP_COUNT'" >&2
        exit 1
        ;;
esac
if [ "$KEEP_COUNT" -lt 1 ]; then
    echo "keep_count must be at least 1 - 0 would prune every snapshot on the Pi, including the newest." >&2
    exit 1
fi

# Windows OpenSSH's own client, not whatever `ssh`/`scp` resolve to on PATH - git-bash's
# bundled (Cygwin) ssh doesn't talk to the Windows OpenSSH Authentication Agent service
# the way ssh.exe does, so it can't reach the key that actually authenticates here even
# though the same key file is on disk either way. Matches the exact binary this project's
# deploy/revert steps already invoke from PowerShell. Overridable (matching PI_HOST/
# PI_REPO's own pattern above) so a test can substitute fixture binaries.
SSH="${SSH_BIN:-/c/Windows/System32/OpenSSH/ssh.exe}"
SCP="${SCP_BIN:-/c/Windows/System32/OpenSSH/scp.exe}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
ARCHIVE_ROOT="$REPO_ROOT/backups/pi-archive"
# scp lands here first, never directly in ARCHIVE_ROOT - see is_complete_backup and the
# promotion step below for why.
STAGING_ROOT="$REPO_ROOT/backups/.pi-archive-staging"
mkdir -p "$ARCHIVE_ROOT" "$STAGING_ROOT"

# Audit-confirmed defect (P1): the previous version of this script only checked whether
# ARCHIVE_ROOT/$dir EXISTED before treating a snapshot as "already archived" - an scp -r
# that fails partway (a dropped connection) still leaves a partial directory behind, and a
# later run would see that directory, assume it was a complete prior success, and go on to
# authorize pruning the Pi's own (real, complete) copy - permanently losing the good data
# and keeping only the broken half-copy. Confirmed via a real reproduction (a fixture-based
# test harness with SSH/SCP replaced by fakes: an interrupted first run left a partial
# folder, a second run accepted it and requested pruning).
#
# Fixed two ways together: (1) a backup dir is only ever trusted once it has both a
# meta.txt and the DB file meta.txt itself names - the same completeness check
# revert_last_deploy.sh already applies before trusting a backup, reused here rather than
# invented fresh, so a half-copied directory (old bug, or a future interrupted transfer)
# can never be mistaken for a real one; (2) scp lands in a STAGING directory, not directly
# in the final archive location, and is only atomically promoted (via `mv` - a rename on
# the same filesystem, never a partial-state window at the final path) once verified
# complete - so the final ARCHIVE_ROOT/$dir path itself is never observably partial to a
# later run, even if THIS run is interrupted immediately after.
is_complete_backup() {
    local dir="$1"
    local meta="$dir/meta.txt"
    [ -f "$meta" ] || return 1
    local db_path
    db_path="$(grep -E '^db_path=' "$meta" | tail -n1 | cut -d= -f2-)"
    [ -n "$db_path" ] || return 1
    [ -f "$dir/$(basename "$db_path")" ]
}

echo "Listing backups on the Pi..."
mapfile -t REMOTE_DIRS < <("$SSH" -o BatchMode=yes "$PI_HOST" "ls -1 '$PI_REPO/backups/pi' 2>/dev/null || true")

if [ "${#REMOTE_DIRS[@]}" -eq 0 ]; then
    echo "No backups found on the Pi - nothing to sync."
    exit 0
fi

copied=0
for dir in "${REMOTE_DIRS[@]}"; do
    [ -n "$dir" ] || continue
    if is_complete_backup "$ARCHIVE_ROOT/$dir"; then
        continue  # already archived and verified complete from a previous run
    fi
    # Clears out any stale partial copy at either path - a leftover from an interrupted
    # PREVIOUS attempt (this run's own retry, or one left by the old, unfixed script)
    # must never be silently reused as if it were already complete.
    rm -rf "${ARCHIVE_ROOT:?}/$dir" "${STAGING_ROOT:?}/$dir"
    echo "Archiving $dir..."
    "$SCP" -rq "$PI_HOST:$PI_REPO/backups/pi/$dir" "$STAGING_ROOT/$dir"
    if ! is_complete_backup "$STAGING_ROOT/$dir"; then
        echo "Copy of $dir finished but looks incomplete (no meta.txt/DB file) - refusing to promote it." >&2
        rm -rf "${STAGING_ROOT:?}/$dir"
        exit 1
    fi
    mv "$STAGING_ROOT/$dir" "$ARCHIVE_ROOT/$dir"
    copied=$((copied + 1))
done
echo "Archived $copied new snapshot(s) to $ARCHIVE_ROOT (${#REMOTE_DIRS[@]} total on the Pi)."

# Only prune once every remote snapshot is confirmed present AND COMPLETE locally - never
# delete the Pi's only copy of something whose scp above silently failed to complete.
missing=0
for dir in "${REMOTE_DIRS[@]}"; do
    [ -n "$dir" ] || continue
    if ! is_complete_backup "$ARCHIVE_ROOT/$dir"; then
        echo "Missing or incomplete archive copy of $dir - refusing to prune anything this run." >&2
        missing=1
    fi
done
if [ "$missing" -eq 1 ]; then
    exit 1
fi

echo "Pruning the Pi to its newest $KEEP_COUNT snapshot(s)..."
"$SSH" -o BatchMode=yes "$PI_HOST" \
    "cd '$PI_REPO/backups/pi' && ls -1dt */ 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) | xargs -r rm -rf --"

rmdir "$STAGING_ROOT" 2>/dev/null || true
echo "Done. Pi now keeps its newest $KEEP_COUNT backup(s); full history lives in $ARCHIVE_ROOT."
