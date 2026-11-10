#!/usr/bin/env bash
# Update ClaudeBot on this server to a release tag. Run by hand as the user
# claude, from the clone that engine/install.sh was run from:
#   engine/update.sh                 # the newest v* tag
#   engine/update.sh --tag v0.0.1    # a specific tag
# It backs up the company database, moves the checkout to the tag, applies
# schema changes with `cb init` (safe to repeat), restarts the services and runs
# `cb doctor`. If something fails after the first change, it prints how to go
# back and exits non-zero. It does not touch SSH, the firewall, sudo rules,
# secrets or company files, except for writing the backup file.
set -euo pipefail

usage() {
    echo "usage: $0 [--tag vX.Y.Z]" >&2
    exit 2
}

TAG=""
while [ "$#" -gt 0 ]; do
    case $1 in
        --tag)
            [ "$#" -ge 2 ] || usage
            TAG=$2
            shift 2
            ;;
        *) usage ;;
    esac
done
if [ -n "$TAG" ] && ! [[ $TAG =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "error: --tag must look like v0.0.1" >&2
    exit 2
fi

if [ "$(id -u)" -eq 0 ]; then
    echo "error: run this as the user claude, not root" >&2
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
TARGET_COMMIT=$(git -C "$REPO_DIR" rev-parse "refs/tags/$TAG^{commit}")
if [ "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$TARGET_COMMIT" ]; then
    echo "Already on $TAG (ClaudeBot $OLD_VERSION). Nothing to do."
    exit 0
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

# 6. Move the checkout to the tag. From here on, a failure needs the rollback
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

# 8. Restart the services so they run the new code.
echo "Restarting services ..."
sudo systemctl restart claudebot-dispatcher.service
if systemctl is-enabled --quiet claudebot-admin.service 2>/dev/null; then
    sudo systemctl restart claudebot-admin.service
    echo "Restarted claudebot-admin (the current Admin conversation ends)."
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
