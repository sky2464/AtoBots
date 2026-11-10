#!/usr/bin/env bash
# Update ClaudeBot on this server to a release tag. Requires a full Git clone
# (do not deploy with rsync --delete into this tree). Run by hand as the bots'
# account, from the clone that engine/install.sh was run from:
#   engine/update.sh                 # the newest v* tag
#   engine/update.sh --tag v0.0.1    # a specific tag
# Run as the bots' account (not root), from the clone that install.sh used.
# It backs up the company database, moves the checkout to the tag, applies
# schema changes with `cb init` (safe to repeat), restarts the services and runs
# `cb doctor`. If something fails after the first change, it prints how to go
# back and exits non-zero. It does not touch SSH, the firewall, sudo rules,
# secrets or company files, except for writing the backup file.
set -euo pipefail

usage() {
    echo "usage: $0 [--tag vX.Y.Z] [--force]" >&2
    exit 2
}

TAG=""
EXPLICIT_TAG=0
FORCE=0
while [ "$#" -gt 0 ]; do
    case $1 in
        --tag)
            [ "$#" -ge 2 ] || usage
            TAG=$2
            EXPLICIT_TAG=1
            shift 2
            ;;
        --force)
            FORCE=1
            shift
            ;;
        *) usage ;;
    esac
done
if [ -n "$TAG" ] && ! [[ $TAG =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "error: --tag must look like v0.0.1" >&2
    exit 2
fi

if [ "$(id -u)" -eq 0 ]; then
    echo "error: run this as the bots' account, not root" >&2
    exit 2
fi

ENGINE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
if ! [[ $ENGINE_DIR =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "error: the engine path may only use letters, digits, . _ - and /" >&2
    exit 2
fi
REPO_DIR=$(cd "$ENGINE_DIR/.." && pwd -P)
CB=$ENGINE_DIR/bin/cb
[ -x "$CB" ] || { echo "error: $CB is missing" >&2; exit 1; }
if [ "$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null)" != "$REPO_DIR" ]; then
    echo "error: $REPO_DIR is not a Git clone of ClaudeBot" >&2
    exit 1
fi

HOME_DIR=${CLAUDEBOT_HOME:-}
if [ -z "$HOME_DIR" ] && [ -r /etc/claudebot.env ]; then
    HOME_DIR=$(sed -n 's/^CLAUDEBOT_HOME=//p' /etc/claudebot.env | sed -n '1p')
fi
case $HOME_DIR in
    /*) ;;
    *) echo "error: CLAUDEBOT_HOME is not set to an absolute path" >&2; exit 1 ;;
esac
if ! [[ $HOME_DIR =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "error: the company home may only use letters, digits, . _ - and /" >&2
    exit 1
fi
export CLAUDEBOT_HOME=$HOME_DIR
DB=$HOME_DIR/company.db
[ -f "$DB" ] || { echo "error: no database at $DB; run engine/install.sh first" >&2; exit 1; }

if ! sudo -n true 2>/dev/null; then
    echo "error: this user needs passwordless sudo (sudo -n true failed)" >&2
    exit 1
fi

# Set once the first change is made. Until then a failure changes nothing.
CHANGED=0
OLD_VERSION=""
OLD_REF=""
BACKUP=""

on_error() {
    local status=$1
    "$CB" dispatcher release >/dev/null 2>&1 || true
    echo "" >&2
    if [ "$CHANGED" -eq 0 ]; then
        echo "The update stopped (exit $status). Nothing was changed." >&2
        return
    fi
    cat >&2 <<EOF
The update FAILED (exit $status). To go back to ClaudeBot $OLD_VERSION:
  1. Stop the services:
       sudo systemctl stop claudebot-dispatcher
       sudo systemctl stop claudebot-admin   (only if it is enabled)
  2. Put the old code back:
       git -C "$REPO_DIR" checkout $OLD_REF
  3. Restore the database from the backup taken before the update:
       rm -f "$DB-wal" "$DB-shm"
       cp "$BACKUP" "$DB"
     Backup file: $BACKUP
  4. Start the services again:
       sudo systemctl start claudebot-dispatcher
       sudo systemctl start claudebot-admin   (only if it is enabled)
  5. Check the install: "$CB" doctor
EOF
}
trap 'on_error $?' ERR

# 1. Refuse on a dirty tree: local edits would be lost or mixed in.
if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
    echo "error: the ClaudeBot checkout has local changes. Commit or discard them, then run this again:" >&2
    git -C "$REPO_DIR" status --short >&2
    exit 1
fi

# 2. Where we are now.
OLD_VERSION=$("$CB" version)
if ! [[ $OLD_VERSION =~ ^[0-9A-Za-z._+-]+$ ]]; then
    echo "error: cb version printed something unexpected: $OLD_VERSION" >&2
    exit 1
fi
OLD_REF=$(git -C "$REPO_DIR" describe --tags --exact-match 2>/dev/null || git -C "$REPO_DIR" rev-parse HEAD)
echo "Current ClaudeBot version: $OLD_VERSION (checkout: $OLD_REF)"

# 3. Fetch release tags. This only adds tags and objects; the checkout is not
# touched yet.
echo "Fetching release tags ..."
git -C "$REPO_DIR" fetch --tags origin

if [ -z "$TAG" ]; then
    TAG=$(git -C "$REPO_DIR" tag --list 'v*' --sort=-v:refname | sed -n '1p')
    if [ -z "$TAG" ]; then
        echo "error: no v* release tags were found on origin" >&2
        exit 1
    fi
fi
if ! git -C "$REPO_DIR" rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null; then
    echo "error: there is no tag $TAG. List the releases with: git -C $REPO_DIR tag --list 'v*'" >&2
    exit 1
fi
RELEASE_KEY=$(git -C "$REPO_DIR" config --get claudebot.releaseKey || true)
if [ -n "$RELEASE_KEY" ]; then
    verify_out=$(git -C "$REPO_DIR" verify-tag "$TAG" 2>&1 || true)
    if ! printf '%s\n' "$verify_out" | grep -F -q -- "$RELEASE_KEY"; then
        echo "error: tag $TAG is not signed by the configured release key" >&2
        exit 1
    fi
else
    echo 'note: release tags are not verified (see UPDATING.md, "Verifying releases")'
fi
TARGET_COMMIT=$(git -C "$REPO_DIR" rev-parse "refs/tags/$TAG^{commit}")
if [ "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$TARGET_COMMIT" ]; then
    echo "Already on $TAG (ClaudeBot $OLD_VERSION). Nothing to do."
    exit 0
fi
# Downgrade guard: the checkout is newer than the target tag. Refuse unless the
# user named the tag with --tag (an explicit rollback).
if [ "$EXPLICIT_TAG" -eq 0 ] && git -C "$REPO_DIR" merge-base --is-ancestor "$TARGET_COMMIT" HEAD; then
    echo "error: this checkout ($OLD_VERSION) is newer than the newest release, $TAG." >&2
    echo "Refusing to go back. To roll back on purpose, run: engine/update.sh --tag $TAG" >&2
    exit 1
fi
echo "Updating to $TAG ..."

# 4. The company name is needed to run `cb init` after the checkout.
COMPANY=$(python3 -I -c '
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
row = conn.execute("SELECT value FROM meta WHERE key = ?", ("company",)).fetchone()
print(row[0] if row else "")
' "$DB")
if [ -z "$COMPANY" ]; then
    echo "error: the database has no company name in its meta table" >&2
    exit 1
fi

# 5. Back up the database before anything changes. The database runs in WAL
# mode, so a plain copy could miss recent writes; SQLite's own backup is used.
BACKUP_DIR=$HOME_DIR/backups
DATE=$(date -u +%Y-%m-%d)
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
BACKUP=$BACKUP_DIR/pre-update-$OLD_VERSION-$DATE.db
if [ -e "$BACKUP" ]; then
    BACKUP=$BACKUP_DIR/pre-update-$OLD_VERSION-$DATE-$(date -u +%H%M%S).db
fi
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$BACKUP'"
else
    python3 -I -c '
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
' "$DB" "$BACKUP"
fi
chmod 600 "$BACKUP"
[ -s "$BACKUP" ] || { echo "error: the backup at $BACKUP is empty" >&2; exit 1; }
echo "Backed up the database to $BACKUP"

# 6. Hold the queue with the code that is running now, then wait until the
# current runs finish. An engine older than this release has no hold command.
set +e
"$CB" dispatcher hold --minutes 45 --reason update
hold_status=$?
set -e
if [ "$hold_status" -eq 2 ]; then
    echo "this version cannot hold the queue; continuing as before"
elif [ "$hold_status" -ne 0 ]; then
    exit "$hold_status"
fi
echo "Waiting for bot runs to finish (up to 30 minutes) ..."
IDLE_DEADLINE=$((SECONDS + 1800))
while [ "$SECONDS" -lt "$IDLE_DEADLINE" ]; do
    if "$CB" dispatcher idle; then
        break
    fi
    sleep 10
done
if ! "$CB" dispatcher idle; then
    echo "A bot run is still active:" >&2
    "$CB" status >&2 || true
    if [ "$FORCE" -eq 0 ]; then
        "$CB" dispatcher release >/dev/null 2>&1 || true
        exit 1
    fi
    echo "warning: continuing because --force was given" >&2
fi
sudo systemctl stop claudebot-dispatcher.service

# 7. Move the checkout to the tag. From here on, a failure needs the rollback
# steps printed by on_error.
CHANGED=1
git -C "$REPO_DIR" checkout --quiet --detach "refs/tags/$TAG"

NEW_VERSION=$("$CB" version)
if [ "v$NEW_VERSION" != "$TAG" ]; then
    echo "warning: tag $TAG reports ClaudeBot version $NEW_VERSION" >&2
fi

# 7. Apply schema changes. Safe to repeat: it creates nothing that exists.
echo "Applying the update to company \"$COMPANY\" ..."
"$CB" init --company "$COMPANY"
echo "Refreshing engine-owned templates ..."
"$CB" templates refresh

# 8. Re-render every unit, then start the services on the new code.
# shellcheck source=lib/units.sh
source "$ENGINE_DIR/lib/units.sh"
MEMORY_MAX=$("$CB" config get run_memory_max)
ADMIN_UNIT=/etc/systemd/system/claudebot-admin.service
ADMIN_RENDERED=$(sed -e "s|@HOME@|$HOME_DIR|g" -e "s|@ENGINE@|$ENGINE_DIR|g" \
    -e "s|@USER@|$(id -un)|g" -e "s|@MEMORY_MAX@|$MEMORY_MAX|g" \
    "$ENGINE_DIR/systemd/claudebot-admin.service")
ADMIN_INSTALLED=""
if [ -f "$ADMIN_UNIT" ]; then
    ADMIN_INSTALLED=$(cat "$ADMIN_UNIT")
fi
install_units "$HOME_DIR" "$ENGINE_DIR" "$(id -un)" "$MEMORY_MAX"
sudo systemctl daemon-reload
echo "Starting services ..."
sudo systemctl start claudebot-dispatcher.service
"$CB" dispatcher release
if systemctl is-enabled --quiet claudebot-admin.service 2>/dev/null; then
    if [ "$ADMIN_RENDERED" != "$ADMIN_INSTALLED" ] || [ "$OLD_VERSION" != "$NEW_VERSION" ]; then
        sudo systemctl restart claudebot-admin.service
        echo "Restarted claudebot-admin. The conversation will restart."
    else
        echo "claudebot-admin is unchanged, so the conversation was left running."
    fi
else
    echo "claudebot-admin is not enabled, so it was not restarted."
fi

# 9. Health check. Findings are reported, not treated as a failed update: a
# finding (for example, no backup remote yet) is not caused by the update.
echo ""
echo "Running cb doctor ..."
DOCTOR_STATUS=0
"$CB" doctor || DOCTOR_STATUS=$?

echo ""
echo "ClaudeBot updated: $OLD_VERSION -> $NEW_VERSION ($TAG)."
echo "Database backup from before the update: $BACKUP"
if [ "$DOCTOR_STATUS" -ne 0 ]; then
    echo "cb doctor reported problems (listed above). The update itself finished and"
    echo "nothing was rolled back. See UPDATING.md, \"If cb doctor reports problems\"."
fi

# Keep the five newest pre-update database copies and delete the older ones.
kept=0
while IFS= read -r old_copy; do
    [ -n "$old_copy" ] || continue
    kept=$((kept + 1))
    if [ "$kept" -gt 5 ]; then
        rm -f -- "$old_copy"
        echo "Deleted $(basename "$old_copy")"
    fi
done < <(ls -1t "$BACKUP_DIR"/pre-update-*.db 2>/dev/null || true)
