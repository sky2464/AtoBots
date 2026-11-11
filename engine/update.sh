#!/usr/bin/env bash
# Update the engine on this server: to a release tag, or to a local build
# that `cb update --zip` has already imported into the clone.
#
#   engine/update.sh                  # the newest v* tag
#   engine/update.sh --tag v0.0.3     # a specific tag
#   engine/update.sh --ref COMMIT --label local:SHA   # used by `cb update --zip`
#
# Normally started by `cb update`, in its own systemd unit, so it survives the
# restart of whoever asked for it. Run as the bots' account (not root), from
# the clone that install.sh used.
#
# This is the FIRST half of an update, done by the version installed now: it
# checks, backs up the database, tells the Owner, holds the queue, waits for
# bot runs, stops the dispatcher and moves the checkout. Then it runs
# engine/lib/update_finish.sh from the NEW version, which applies the schema
# and migrations, installs the units, restarts and verifies the services, and
# rolls everything back by itself if that fails.
#
# It does not touch SSH, the firewall, sudo rules, secrets or company files,
# except for writing the backup file.
set -euo pipefail

usage() {
    echo "usage: $0 [--tag vX.Y.Z | --ref COMMIT --label LABEL] [--force]" >&2
    exit 2
}

# The whole script is one function, called on the last line: bash has then
# read all of it before the checkout replaces this file on disk.
main() {
    TAG=""
    REF=""
    LABEL=""
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
            --ref)
                [ "$#" -ge 2 ] || usage
                REF=$2
                shift 2
                ;;
            --label)
                [ "$#" -ge 2 ] || usage
                LABEL=$2
                shift 2
                ;;
            --force)
                FORCE=1
                shift
                ;;
            -*)
                usage
                ;;
            *)
                if [ -z "$TAG" ]; then
                    TAG=$1
                    EXPLICIT_TAG=1
                    shift
                else
                    usage
                fi
                ;;
        esac
    done
    if [ -n "$TAG" ] && ! [[ $TAG =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "error: --tag must look like v0.0.1" >&2
        exit 2
    fi
    if [ -n "$REF" ]; then
        if [ -n "$TAG" ] || ! [[ $REF =~ ^[0-9a-f]{7,64}$ ]] || ! [[ $LABEL =~ ^local:[0-9a-f]{7,64}$ ]]; then
            usage
        fi
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
    # shellcheck source=lib/update_common.sh
    source "$ENGINE_DIR/lib/update_common.sh"
    DISPATCHER=$CB_UNIT_PREFIX-dispatcher.service
    ADMIN=$CB_UNIT_PREFIX-admin.service

    if [ -f /proc/self/cgroup ] && grep -F -q "$ADMIN" /proc/self/cgroup; then
        echo "run cb update ${TAG:-TAG} instead" >&2
        exit 1
    fi

    REPO_DIR=$(cd "$ENGINE_DIR/.." && pwd -P)
    CB=$ENGINE_DIR/bin/cb
    [ -x "$CB" ] || { echo "error: $CB is missing" >&2; exit 1; }
    if [ "$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null)" != "$REPO_DIR" ]; then
        echo "error: this install is a copy with no Git history, so it cannot be updated in place." >&2
        echo "Turn it into a clone once with: cb update --adopt --repo URL   (or --zip FILE)" >&2
        echo "See UPDATING.md, \"A copied install\"." >&2
        exit 1
    fi

    HOME_DIR=$(cb_find_home)
    export "$CB_HOME_VAR=$HOME_DIR"
    DB=$HOME_DIR/company.db
    [ -f "$DB" ] || { echo "error: no database at $DB; run engine/install.sh first" >&2; exit 1; }

    if [ -z "${CB_NO_SUDO:-}" ] && ! sudo -n true 2>/dev/null; then
        echo "error: this user needs passwordless sudo (sudo -n true failed)" >&2
        exit 1
    fi

    # Set once the first change is made. Until then a failure changes nothing.
    # One handler runs when this script ends for any reason (a failed command
    # or an explicit exit) and cleans up according to these.
    CHANGED=0
    HELD=0
    STOPPED=0
    FINISHED=0
    OLD_VERSION=""
    OLD_REF=""
    BACKUP=""
    TARGET_NAME=""
    trap 'on_exit $?' EXIT

    # 1. Refuse on a dirty tree: local edits would be lost or mixed in.
    if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
        echo "error: the engine checkout has local changes. Commit or discard them, then run this again:" >&2
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
    echo "Current $CB_PRODUCT version: $OLD_VERSION (checkout: $OLD_REF)"

    # 3. The target. Nothing is changed yet.
    if [ -n "$REF" ]; then
        if ! git -C "$REPO_DIR" rev-parse -q --verify "$REF^{commit}" >/dev/null; then
            echo "error: there is no commit $REF in this clone" >&2
            exit 1
        fi
        TARGET_COMMIT=$(git -C "$REPO_DIR" rev-parse "$REF^{commit}")
        if ! git -C "$REPO_DIR" merge-base --is-ancestor "$TARGET_COMMIT" refs/heads/local-builds 2>/dev/null; then
            echo "error: $REF is not a local build imported by cb update" >&2
            exit 1
        fi
        TARGET_NAME="a local build ($LABEL, unverified)"
        echo "note: this is an unverified local build: nothing proves who made it"
    else
        echo "Fetching release tags ..."
        cb_timeout 300 git -C "$REPO_DIR" fetch --tags origin
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
        RELEASE_KEY=$(git -C "$REPO_DIR" config --get "$CB_UNIT_PREFIX.releaseKey" || true)
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
        LABEL=tag:$TAG
        TARGET_NAME=$TAG
        # Downgrade guard: the checkout is newer than the target tag. Refuse
        # unless the tag was named (an explicit rollback).
        if [ "$EXPLICIT_TAG" -eq 0 ] && [ "$(git -C "$REPO_DIR" rev-parse HEAD)" != "$TARGET_COMMIT" ] \
                && git -C "$REPO_DIR" merge-base --is-ancestor "$TARGET_COMMIT" HEAD; then
            echo "error: this checkout ($OLD_VERSION) is newer than the newest release, $TAG." >&2
            echo "Refusing to go back. To roll back on purpose, run: cb update $TAG" >&2
            exit 1
        fi
    fi
    if [ "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$TARGET_COMMIT" ]; then
        echo "Already on $TARGET_NAME ($CB_PRODUCT $OLD_VERSION). Nothing to do."
        exit 0
    fi
    if ! git -C "$REPO_DIR" cat-file -e "$TARGET_COMMIT:engine/lib/update_finish.sh" 2>/dev/null; then
        echo "error: $TARGET_NAME is from before 0.0.3 and cannot finish its own update." >&2
        echo "See UPDATING.md, \"Roll back on purpose\"." >&2
        exit 1
    fi
    echo "Updating to $TARGET_NAME ..."

    # 4. The company name is needed after the checkout; fail early without it.
    COMPANY=$(cb_company_name "$DB")
    if [ -z "$COMPANY" ]; then
        echo "error: the database has no company name in its meta table" >&2
        exit 1
    fi

    # 5. Back up the database before anything changes.
    umask 077
    BACKUP_DIR=$HOME_DIR/backups
    BACKUP=$BACKUP_DIR/pre-update-$OLD_VERSION-$(date -u +%Y-%m-%d-%H%M%S).db
    cb_backup_db "$DB" "$BACKUP"
    echo "Backed up the database to $BACKUP"

    # 6. Tell the Owner before the chat goes offline. Best effort.
    cb_timeout 90 "$CB" update-notice --old "$OLD_VERSION" --new "$TARGET_NAME" || true

    # 7. Hold the queue, wait for the current runs, stop the dispatcher.
    HELD=1
    cb_timeout 60 "$CB" dispatcher hold --minutes 45 --reason update
    cb_wait_idle "$CB" "$FORCE" || exit 1
    cb_systemctl_priv stop "$DISPATCHER"
    STOPPED=1
    STOPPED_AT=$(date +%s)

    # 8. Move the checkout. From here on a failure means going back.
    CHANGED=1
    git -C "$REPO_DIR" checkout --quiet --detach "$TARGET_COMMIT"

    # 9. The new version finishes its own update.
    export CB_UPD_KIND=git
    export CB_UPD_OLD_VERSION=$OLD_VERSION
    export CB_UPD_OLD_REF=$OLD_REF
    export CB_UPD_BACKUP=$BACKUP
    export CB_UPD_LABEL=$LABEL
    export CB_UPD_OLD_PREFIX=$CB_UNIT_PREFIX
    export CB_UPD_STOPPED_AT=$STOPPED_AT
    status=0
    "$ENGINE_DIR/lib/update_finish.sh" || status=$?
    result=0
    cb_finish_status "$status" || result=$?
    case $result in
        0) FINISHED=1; exit 0 ;;
        10) FINISHED=1; exit 1 ;;
        11) FINISHED=1; exit 2 ;;
        *) exit "$status" ;;   # on_exit puts the old version back
    esac
}

# shellcheck disable=SC2329  # called by the EXIT trap
on_exit() {
    local status=$1
    trap - EXIT
    if [ "$status" -eq 0 ] || [ "$FINISHED" -eq 1 ]; then
        exit "$status"
    fi
    echo "" >&2
    if [ "$CHANGED" -eq 0 ]; then
        if [ "$HELD" -eq 1 ]; then
            cb_timeout 60 "$CB" dispatcher release >/dev/null 2>&1 || true
        fi
        if [ "$STOPPED" -eq 1 ]; then
            cb_systemctl_priv start "$DISPATCHER" 2>/dev/null || true
        fi
        echo "The update stopped (exit $status). Nothing was changed." >&2
        exit "$status"
    fi
    put_old_version_back "$status"
}

# The new version could not run its own second half at all (a broken build).
# Nothing but the checkout should have changed; the database is restored
# anyway so the two always match.
# shellcheck disable=SC2329  # reached from on_exit
put_old_version_back() {
    local status=$1 ok=1
    set +e
    echo "The new version could not finish the update (exit $status). Going back to $OLD_VERSION ..." >&2
    git -C "$REPO_DIR" checkout --quiet --force --detach "$OLD_REF" || ok=0
    cb_restore_db "$BACKUP" "$DB" || ok=0
    cb_systemctl_priv start "$DISPATCHER" || ok=0
    cb_timeout 60 "$CB" dispatcher release >/dev/null 2>&1
    if cb_systemctl is-enabled --quiet "$ADMIN" 2>/dev/null; then
        cb_systemctl_priv restart "$ADMIN"
    fi
    if [ "$ok" -eq 1 ]; then
        cb_timeout 90 "$CB" admin notify "$CB_PRODUCT update to $TARGET_NAME failed before the new version could start (exit $status). It was undone: $OLD_VERSION is running again with the database from just before the update. Log: ${CB_UPDATE_LOG:-unknown}" >/dev/null 2>&1
        echo "The old version is running again. Nothing else was changed." >&2
        exit 1
    fi
    cb_timeout 90 "$CB" admin notify "$CB_PRODUCT update to $TARGET_NAME FAILED and could not be undone automatically. The services may be down. Log: ${CB_UPDATE_LOG:-unknown}" >/dev/null 2>&1
    cat >&2 <<STEPS
The update FAILED and could not be undone automatically. To go back to $OLD_VERSION by hand:
  1. sudo systemctl stop $DISPATCHER
  2. git -C "$REPO_DIR" checkout --force --detach $OLD_REF
  3. rm -f "$DB-wal" "$DB-shm" && cp "$BACKUP" "$DB"
  4. sudo systemctl start $DISPATCHER   (and $ADMIN if it is enabled)
  5. "$CB" doctor
STEPS
    exit 2
}

main "$@"
