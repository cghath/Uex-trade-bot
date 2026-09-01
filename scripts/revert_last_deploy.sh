#!/usr/bin/env bash
# Undo a scripts/deploy_and_backup.sh upgrade: restores that snapshot's DB and checks out
# the commit it recorded, then restarts the bot. The state you're reverting away from is
# itself snapshotted first, so this is undoable too.
#
# Usage:
#   scripts/revert_last_deploy.sh                  # revert to the most recent backup
#   scripts/revert_last_deploy.sh backups/pi/<dir>  # revert to a specific one
set -euo pipefail

SERVICE_NAME="${UEX_BOT_SERVICE:-uex-trade-bot}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

BACKUP_ROOT="$REPO_ROOT/backups/pi"

if [ $# -ge 1 ]; then
    BACKUP_DIR="${1%/}"
else
    BACKUP_DIR="$(ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | head -n1)"
    BACKUP_DIR="${BACKUP_DIR%/}"
fi
[ -n "${BACKUP_DIR:-}" ] && [ -d "$BACKUP_DIR" ] || {
    echo "No backup found under $BACKUP_ROOT. Pass one explicitly, e.g.:" >&2
    echo "  scripts/revert_last_deploy.sh backups/pi/20260830-120000_abc1234" >&2
    exit 1
}

META="$BACKUP_DIR/meta.txt"
[ -f "$META" ] || { echo "No meta.txt in $BACKUP_DIR - can't tell which commit to revert to." >&2; exit 1; }
# shellcheck disable=SC1090
source "$META"
[ -n "${commit:-}" ] || { echo "meta.txt in $BACKUP_DIR has no commit recorded." >&2; exit 1; }
[ -n "${db_path:-}" ] || { echo "meta.txt in $BACKUP_DIR has no db_path recorded." >&2; exit 1; }

# Validate the backup is actually usable BEFORE stopping anything - meta.txt existing and
# parsing isn't enough on its own; a half-written backup or a commit that's since been
# pruned/rebased away would otherwise leave the service stopped with no way to complete
# the revert (git checkout / cp failing after the stop, with nothing to fall back to).
BACKUP_DB="$BACKUP_DIR/$(basename "$db_path")"
[ -f "$BACKUP_DB" ] || { echo "Backup DB file $BACKUP_DB is missing - refusing to revert from an incomplete backup." >&2; exit 1; }
git cat-file -e "${commit}^{commit}" 2>/dev/null || { echo "Recorded commit $commit does not exist in this repo - refusing to revert to an unknown commit." >&2; exit 1; }

echo "Reverting to commit $commit (snapshotted $timestamp_utc from branch $branch)..."

echo "Stopping $SERVICE_NAME..."
sudo systemctl stop "$SERVICE_NAME"

# Snapshot the state being discarded too, in case the revert itself needs undoing.
CURRENT_COMMIT="$(git rev-parse --short HEAD)"
PRE_REVERT_DIR="$BACKUP_ROOT/$(date -u +%Y%m%d-%H%M%S)_${CURRENT_COMMIT}_pre-revert"
mkdir -p "$PRE_REVERT_DIR"
if [ -f "$db_path" ]; then
    cp "$db_path" "$PRE_REVERT_DIR/$(basename "$db_path")"
    {
        echo "commit=$CURRENT_COMMIT"
        echo "db_path=$db_path"
    } > "$PRE_REVERT_DIR/meta.txt"
    echo "Saved the state being discarded ($CURRENT_COMMIT) to $PRE_REVERT_DIR"
fi

cp "$BACKUP_DIR/$(basename "$db_path")" "$db_path"
for suffix in -wal -shm; do
    rm -f "${db_path}${suffix}"  # stale sidecars from the discarded run
    [ -f "$BACKUP_DIR/$(basename "$db_path")${suffix}" ] && cp "$BACKUP_DIR/$(basename "$db_path")${suffix}" "${db_path}${suffix}"
done

git checkout "$commit"

if ! git diff --quiet "$CURRENT_COMMIT" "$commit" -- requirements.txt requirements-dev.txt; then
    echo "requirements*.txt differs between the discarded and reverted-to commit - reinstalling dependencies..."
    .venv/bin/pip install -r requirements.txt
fi

echo "Starting $SERVICE_NAME..."
sudo systemctl start "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "Reverted to $commit."
echo "Note: this is a detached checkout, not a branch - run 'git checkout $branch' (or your branch of choice) when you're ready to move forward again."
