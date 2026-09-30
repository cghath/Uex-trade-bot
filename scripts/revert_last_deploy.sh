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

# Computed early (before anything destructive) so they're always defined by the time
# SERVICE_STOPPED could ever become 1 below - rollback_on_failure needs both to restore
# exactly the state this run is discarding.
CURRENT_COMMIT="$(git rev-parse --short HEAD)"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

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

echo "Reverting to commit $commit (snapshotted ${timestamp_utc:-unknown} from branch ${branch:-unknown})..."

# Without this, any failure between "stop" and "start" below (a failing cp, a git checkout
# that can't complete, a broken pip install) left the service stopped with no automatic
# recovery - set -e just exits mid-script. Unlike deploy_and_backup.sh (which never
# overwrites the DB, so its own rollback only needs to restore the commit), this script DOES
# overwrite db_path partway through - so rollback also restores it from PRE_REVERT_DIR
# (just snapshotted below) whenever the failure happens after that overwrite.
SERVICE_STOPPED=0
DB_OVERWRITTEN=0
REVERT_SUCCEEDED=0
rollback_on_failure() {
    if [ "$REVERT_SUCCEEDED" -eq 1 ] || [ "$SERVICE_STOPPED" -eq 0 ]; then
        return
    fi
    echo "" >&2
    echo "Revert failed - restoring the pre-revert state and restarting $SERVICE_NAME..." >&2

    # A restart is only safe once every piece of the pre-revert state is actually back in
    # place - a prior version started the service unconditionally here, so a failed
    # restoration cp (persistent disk/I/O problem) only printed a warning and fell straight
    # through to sidecar handling, git checkout, and systemctl start, potentially running
    # the bot against a partially-written or stale database. restore_ok tracks whether every
    # step below actually succeeded; the restart at the end is gated on it.
    restore_ok=1

    if [ "$DB_OVERWRITTEN" -eq 1 ]; then
        if [ -f "$PRE_REVERT_DIR/$(basename "$db_path")" ]; then
            if cp "$PRE_REVERT_DIR/$(basename "$db_path")" "$db_path"; then
                for suffix in -wal -shm; do
                    rm -f "${db_path}${suffix}"
                    if [ -f "$PRE_REVERT_DIR/$(basename "$db_path")${suffix}" ]; then
                        cp "$PRE_REVERT_DIR/$(basename "$db_path")${suffix}" "${db_path}${suffix}" \
                            || { echo "Could not restore sidecar ${db_path}${suffix} from $PRE_REVERT_DIR." >&2; restore_ok=0; }
                    fi
                done
            else
                echo "Could not restore the pre-revert DB from $PRE_REVERT_DIR." >&2
                restore_ok=0
            fi
        else
            echo "No pre-revert DB backup found at $PRE_REVERT_DIR." >&2
            restore_ok=0
        fi
    fi

    if ! git checkout "$CURRENT_COMMIT"; then
        echo "Could not check out $CURRENT_COMMIT - repo may be in a partial state." >&2
        restore_ok=0
    fi

    if [ "$restore_ok" -eq 1 ]; then
        sudo systemctl start "$SERVICE_NAME" || echo "Could not restart $SERVICE_NAME - check it manually." >&2
    else
        echo "Pre-revert state could not be fully restored - leaving $SERVICE_NAME stopped rather than" >&2
        echo "risk starting it against bad data. Fix $db_path (and/or the git checkout) manually using" >&2
        echo "the backup in $PRE_REVERT_DIR, then start $SERVICE_NAME yourself once it's verified." >&2
    fi
}
# EXIT, not ERR: bash's ERR trap does not fire for an explicit `exit N` (only for a command
# that itself fails under `set -e`) - see deploy_and_backup.sh's own A13 fix for why EXIT is
# the one that actually covers every termination path.
trap rollback_on_failure EXIT

echo "Stopping $SERVICE_NAME..."
sudo systemctl stop "$SERVICE_NAME"
SERVICE_STOPPED=1

# Snapshot the state being discarded too, in case the revert itself needs undoing.
PRE_REVERT_TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
PRE_REVERT_DIR="$BACKUP_ROOT/${PRE_REVERT_TIMESTAMP}_${CURRENT_COMMIT}_pre-revert"
mkdir -p "$PRE_REVERT_DIR"
if [ -f "$db_path" ]; then
    cp "$db_path" "$PRE_REVERT_DIR/$(basename "$db_path")"
    for suffix in -wal -shm; do
        [ -f "${db_path}${suffix}" ] && cp "${db_path}${suffix}" "$PRE_REVERT_DIR/$(basename "$db_path")${suffix}"
    done
    # Same complete field set deploy_and_backup.sh writes (timestamp_utc, commit, branch,
    # db_path) - a prior version of this file wrote only commit/db_path, which meant
    # reverting to THIS pre-revert snapshot later (undoing the revert) crashed with
    # "timestamp_utc: unbound variable" under this script's own `set -u`, since the loader
    # below unconditionally expands both fields.
    {
        echo "timestamp_utc=$PRE_REVERT_TIMESTAMP"
        echo "commit=$CURRENT_COMMIT"
        echo "branch=$CURRENT_BRANCH"
        echo "db_path=$db_path"
    } > "$PRE_REVERT_DIR/meta.txt"
    echo "Saved the state being discarded ($CURRENT_COMMIT) to $PRE_REVERT_DIR"
fi

# Set BEFORE the copy, not after it succeeds: cp itself can fail partway through (disk
# exhaustion, an I/O error) after already truncating/partially overwriting db_path - under
# set -e that failure exits immediately, and setting this flag only on success would have
# left rollback_on_failure thinking the DB was never touched, skipping restoration of a
# destination that may now hold a partially-written, corrupt file.
DB_OVERWRITTEN=1
cp "$BACKUP_DIR/$(basename "$db_path")" "$db_path"
for suffix in -wal -shm; do
    rm -f "${db_path}${suffix}"  # stale sidecars from the discarded run
    [ -f "$BACKUP_DIR/$(basename "$db_path")${suffix}" ] && cp "$BACKUP_DIR/$(basename "$db_path")${suffix}" "${db_path}${suffix}"
done

git checkout "$commit"

if ! git diff --quiet "$CURRENT_COMMIT" "$commit" -- requirements.txt requirements-dev.txt; then
    echo "requirements*.txt differs between the discarded and reverted-to commit - reinstalling dependencies..."
    # Local dev clones use .venv; the Pi's own clone uses venv (no dot) - same detection as
    # deploy_and_backup.sh, which this script previously didn't match (hardcoded .venv only,
    # so a requirements-changing revert on the Pi failed here after the code/DB were already
    # restored but before the service restarted).
    if [ -x ".venv/bin/pip" ]; then
        VENV_PIP=".venv/bin/pip"
    elif [ -x "venv/bin/pip" ]; then
        VENV_PIP="venv/bin/pip"
    else
        echo "Could not find a virtualenv's pip at .venv/bin/pip or venv/bin/pip - aborting." >&2
        exit 1
    fi
    "$VENV_PIP" install -r requirements.txt
fi

echo "Starting $SERVICE_NAME..."
sudo systemctl start "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager

REVERT_SUCCEEDED=1
echo ""
echo "Reverted to $commit."
echo "Note: this is a detached checkout, not a branch - run 'git checkout ${branch:-TestBranch}' (or your branch of choice) when you're ready to move forward again."
