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

echo "Stopping $SERVICE_NAME (so the DB backup is quiesced, not mid-write)..."
sudo systemctl stop "$SERVICE_NAME"

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
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git merge --ff-only "origin/$BRANCH"

if ! git diff --quiet "$OLD_COMMIT" HEAD -- requirements.txt requirements-dev.txt; then
    echo "requirements*.txt changed - reinstalling dependencies..."
    .venv/bin/pip install -r requirements.txt
fi

echo "Starting $SERVICE_NAME..."
sudo systemctl start "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "Deployed $(git rev-parse --short HEAD) (was $OLD_COMMIT)."
echo "If something's wrong: scripts/revert_last_deploy.sh"
