#!/usr/bin/env bash
# Run on the Pi to upgrade to a new commit safely: stops the bot, snapshots the current
# DB + records the current commit into backups/pi/, then pulls and restarts. If the new
# version has a problem, undo with scripts/revert_last_deploy.sh instead of the old
# manual PC<->Pi database-merge process.
#
# Usage: scripts/deploy_and_backup.sh [branch]
#   branch defaults to TestBranch.
set -euo pipefail

SERVICE_NAME="${UEX_BOT_SERVICE:-uex-trade-bot}"
BRANCH="${1:-TestBranch}"

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Same default bot/config.py uses; overridden by DATABASE_PATH in .env if set there.
DB_PATH="data/uexbot.sqlite3"
if [ -f .env ]; then
    env_db_path="$(grep -E '^DATABASE_PATH=' .env | tail -n1 | cut -d= -f2- || true)"
    [ -n "${env_db_path:-}" ] && DB_PATH="$env_db_path"
fi
[ -f "$DB_PATH" ] || { echo "Database not found at $DB_PATH - aborting, nothing backed up or deployed." >&2; exit 1; }

OLD_COMMIT="$(git rev-parse --short HEAD)"
OLD_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_DIR="$REPO_ROOT/backups/pi/${TIMESTAMP}_${OLD_COMMIT}"

# Without this, any failure between "stop" and "start" below (a bad fetch, a merge that
# isn't fast-forwardable, a broken pip install) leaves the bot stopped with no automatic
# recovery - set -e just exits mid-script. The DB is only ever copied FROM here, never
# overwritten, so rolling back never needs to touch it - restoring the old commit and
# restarting is enough to get back to the exact working state this run started from.
SERVICE_STOPPED=0
DEPLOY_SUCCEEDED=0
rollback_on_failure() {
    if [ "$DEPLOY_SUCCEEDED" -eq 1 ] || [ "$SERVICE_STOPPED" -eq 0 ]; then
        return
    fi
    echo "" >&2
    echo "Deploy failed - rolling back to $OLD_COMMIT and restarting $SERVICE_NAME..." >&2
    git checkout "$OLD_COMMIT" || echo "Could not check out $OLD_COMMIT - repo may be in a partial state, fix manually." >&2
    sudo systemctl start "$SERVICE_NAME" || echo "Could not restart $SERVICE_NAME - check it manually." >&2
}
trap rollback_on_failure ERR

echo "Stopping $SERVICE_NAME (so the DB backup is quiesced, not mid-write)..."
sudo systemctl stop "$SERVICE_NAME"
SERVICE_STOPPED=1

mkdir -p "$BACKUP_DIR"
cp "$DB_PATH" "$BACKUP_DIR/$(basename "$DB_PATH")"
for suffix in -wal -shm; do
    [ -f "${DB_PATH}${suffix}" ] && cp "${DB_PATH}${suffix}" "$BACKUP_DIR/$(basename "$DB_PATH")${suffix}"
done
cat > "$BACKUP_DIR/meta.txt" <<EOF
timestamp_utc=$TIMESTAMP
commit=$OLD_COMMIT
branch=$OLD_BRANCH
db_path=$DB_PATH
EOF
echo "Backed up DB + recorded commit $OLD_COMMIT ($OLD_BRANCH) to $BACKUP_DIR"

echo "Fetching and fast-forwarding to origin/$BRANCH..."
# Plain `git fetch origin "$BRANCH"` silently does nothing useful on a clone whose
# remote.origin.fetch refspec only auto-updates one branch (this Pi's is
# +refs/heads/main:refs/remotes/origin/main) - it fetches into FETCH_HEAD but never moves
# origin/$BRANCH, so the merge below would report "Already up to date" against a stale ref
# without erroring. The explicit destination refspec updates origin/$BRANCH regardless of
# what the remote's own default refspec covers.
git fetch origin "$BRANCH:refs/remotes/origin/$BRANCH"
git checkout "$BRANCH"
git merge --ff-only "origin/$BRANCH"

if ! git diff --quiet "$OLD_COMMIT" HEAD -- requirements.txt requirements-dev.txt; then
    echo "requirements*.txt changed - reinstalling dependencies..."
    .venv/bin/pip install -r requirements.txt
fi

echo "Starting $SERVICE_NAME..."
sudo systemctl start "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager

DEPLOY_SUCCEEDED=1
echo ""
echo "Deployed $(git rev-parse --short HEAD) (was $OLD_COMMIT)."
echo "If something's wrong: scripts/revert_last_deploy.sh"
