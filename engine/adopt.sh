#!/usr/bin/env bash
# Turn a copied install (an engine folder with no Git history) into a proper
# Git clone, once, with a backup and a way back. After this, `cb update`
# works like on any other install.
#
#   engine/adopt.sh --repo URL [--tag vX.Y.Z]   # clone the repository
#   engine/adopt.sh --source FOLDER             # an unpacked build, or a clone
#   engine/adopt.sh                             # the tree this script is in
#
# Normally started by `cb update --adopt ...` in its own systemd unit. An
# install too old to have that command is adopted by hand: get the new
# version onto the server (clone it, or unpack a zip) and run ITS
# engine/adopt.sh as the bots' account. Add --detach to run it as its own
# systemd job, which keeps going when Admin (the chat) is stopped.
#
# An install older than engine/MIN_UPDATE_FROM cannot go straight to this
# version. With --repo, this script does it in two steps by itself: it adopts
# the install into release v<MIN_UPDATE_FROM> first, then updates that to the
# release asked for. Both steps keep their own backup and rollback.
#
# What it does, as one job: back up the database, tell the Owner, hold the
# queue, wait for bot runs, stop the services, move the old engine folder to
# <folder>.old, put a Git clone in its place and let the new version finish
# (engine/lib/update_finish.sh): schema, migrations, links, units, restart,
# verification. If that fails, the old folder and the database are put back
# and the services are started again. The old folder is kept until the Owner
# removes it.
#
# It does not touch SSH, the firewall, sudo rules or secrets.
set -euo pipefail

usage() {
    echo "usage: $0 [--old-engine PATH] [--repo URL [--tag vX.Y.Z] | --source FOLDER] [--force] [--detach]" >&2
    exit 2
}

# version_lt A B   True when version A is older than B. Anything that is not
# x.y.z (an "unknown" version) is not older.
version_lt() {
    python3 -I -c '
import sys
def key(text):
    return tuple(int(n) for n in text.split("+")[0].split("."))
try:
    sys.exit(0 if key(sys.argv[1]) < key(sys.argv[2]) else 1)
except ValueError:
    sys.exit(1)
' "$1" "$2"
}

# detach ARGS...   Run this script again as its own systemd job and return.
# The guard (lib/update_guard.sh, copied next to the log) runs however the
# job ends and starts Admin again, so the Owner keeps Telegram.
detach() {
    local log=${CB_ADOPT_LOG:-$HOME/atobots-adopt-$(date -u +%Y%m%dT%H%M%SZ).log}
    local guard=${log%.log}.guard.sh
    install -m 700 "$(dirname "${BASH_SOURCE[0]}")/lib/update_guard.sh" "$guard"
    sudo systemd-run --unit=atobots-adopt --collect --no-block \
        --uid="$(id -un)" -p "StandardOutput=file:$log" \
        -p "StandardError=file:$log" -p "RuntimeMaxSec=3h" \
        -p "ExecStopPost=-/bin/bash \"$guard\" atobots adopt \"$log\"" \
        bash "${BASH_SOURCE[0]}" "$@"
    echo "Started as its own job. Log: $log"
    echo "Watch it with: tail -f $log"
    echo "However it ends, Admin is started again so Telegram keeps working."
}

# bridge_through_stepping_stone
# An install older than the floor goes to release v<floor> first, using that
# release's own adopt script (it still knows the older layout), then to the
# release asked for.
bridge_through_stepping_stone() {
    local step=v$FLOOR bridge final
    if [ -z "$REPO_URL" ]; then
        echo "error: this install is $OLD_VERSION, older than $FLOOR, which this version cannot adopt directly." >&2
        echo "Run this script again with --repo URL so the steps in between can be fetched." >&2
        exit 2
    fi
    bridge=$(mktemp -d "${TMPDIR:-/tmp}/atobots-bridge.XXXXXX")
    # shellcheck disable=SC2064
    trap "rm -rf -- '$bridge'" EXIT
    echo "This install is $OLD_VERSION. This release needs $FLOOR or later, so it goes in two steps: $step first, then the release asked for."
    git clone --quiet "$REPO_URL" "$bridge"
    if ! git -C "$bridge" rev-parse -q --verify "refs/tags/$step^{commit}" >/dev/null; then
        echo "error: the repository has no release $step to go through" >&2
        exit 1
    fi
    final=$TAG
    if [ -z "$final" ]; then
        final=$(git -C "$bridge" tag --list 'v*' --sort=-v:refname | sed -n '1p')
    fi
    git -C "$bridge" checkout --quiet --detach "$step"
    echo "Step 1: adopting into $step ..."
    local -a step_args=(--old-engine "$OLD_ENGINE" --source "$bridge")
    [ "$FORCE" -eq 1 ] && step_args+=(--force)
    bash "$bridge/engine/adopt.sh" "${step_args[@]}"
    if [ "$final" = "$step" ]; then
        return 0
    fi
    echo "Step 2: updating to $final ..."
    local -a up_args=("$final")
    [ "$FORCE" -eq 1 ] && up_args+=(--force)
    "$OLD_REPO/engine/update.sh" "${up_args[@]}"
}

main() {
    ORIG_ARGS=("$@")
    DETACH=0
    OLD_ENGINE=""
    SOURCE=""
    REPO_URL=""
    TAG=""
    CLEANUP=""
    FORCE=0
    while [ "$#" -gt 0 ]; do
        case $1 in
            --old-engine) [ "$#" -ge 2 ] || usage; OLD_ENGINE=$2; shift 2 ;;
            --source) [ "$#" -ge 2 ] || usage; SOURCE=$2; shift 2 ;;
            --repo) [ "$#" -ge 2 ] || usage; REPO_URL=$2; shift 2 ;;
            --tag) [ "$#" -ge 2 ] || usage; TAG=$2; shift 2 ;;
            --cleanup) [ "$#" -ge 2 ] || usage; CLEANUP=$2; shift 2 ;;
            --force) FORCE=1; shift ;;
            --detach) DETACH=1; shift ;;
            *) usage ;;
        esac
    done
    if [ -n "$TAG" ] && ! [[ $TAG =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "error: --tag must look like v0.0.1" >&2
        exit 2
    fi
    if [ -n "$REPO_URL" ] && [ -n "$SOURCE" ]; then
        usage
    fi
    if [ -n "$REPO_URL" ] && ! [[ $REPO_URL =~ ^[A-Za-z0-9@:/._~+-]+$ ]]; then
        echo "error: the repository address has characters that are not allowed" >&2
        exit 2
    fi
    if [ "$(id -u)" -eq 0 ]; then
        echo "error: run this as the bots' account, not root" >&2
        exit 2
    fi

    if [ "$DETACH" -eq 1 ]; then
        local -a again=()
        local arg
        for arg in "${ORIG_ARGS[@]}"; do
            [ "$arg" = "--detach" ] || again+=("$arg")
        done
        detach "${again[@]}"
        exit 0
    fi

    ENGINE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
    # shellcheck source=lib/update_common.sh
    source "$ENGINE_DIR/lib/update_common.sh"
    TOOL_CB=$ENGINE_DIR/bin/cb   # this script's own version; used to import

    # The install being adopted.
    if [ -z "$OLD_ENGINE" ]; then
        link=$(readlink "${CB_USER_BIN_DIR:-$HOME/.local/bin}/cb" 2>/dev/null || true)
        case $link in
            */engine/bin/cb) OLD_ENGINE=${link%/bin/cb} ;;
            *) echo "error: cannot tell where the installed engine is; pass --old-engine PATH" >&2; exit 2 ;;
        esac
    fi
    if [ ! -x "$OLD_ENGINE/bin/cb" ]; then
        echo "error: $OLD_ENGINE/bin/cb is missing" >&2
        exit 1
    fi
    OLD_ENGINE=$(cd "$OLD_ENGINE" && pwd -P)
    OLD_REPO=$(cd "$OLD_ENGINE/.." && pwd -P)
    if ! [[ $OLD_REPO =~ ^/[A-Za-z0-9._/-]+$ ]]; then
        echo "error: the engine path may only use letters, digits, . _ - and /" >&2
        exit 2
    fi
    if [ "$(git -C "$OLD_REPO" rev-parse --show-toplevel 2>/dev/null)" = "$OLD_REPO" ]; then
        echo "error: $OLD_REPO is already a Git clone. Use cb update." >&2
        exit 1
    fi
    KEEP=$OLD_REPO.old
    if [ -e "$KEEP" ]; then
        echo "error: $KEEP already exists (from an earlier adoption). Remove it or move it away first." >&2
        exit 1
    fi
    OLD_CB=$OLD_ENGINE/bin/cb

    OLD_VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$OLD_ENGINE"/*/__init__.py 2>/dev/null | sed -n '1p')
    OLD_VERSION=${OLD_VERSION:-unknown}
    FLOOR=$(tr -d '[:space:]' < "$ENGINE_DIR/MIN_UPDATE_FROM" 2>/dev/null || true)
    if [ -n "$FLOOR" ] && version_lt "$OLD_VERSION" "$FLOOR"; then
        bridge_through_stepping_stone
        exit 0
    fi

    HOME_DIR=$(cb_find_home)
    export "$CB_HOME_VAR=$HOME_DIR"
    DB=$HOME_DIR/company.db
    [ -f "$DB" ] || { echo "error: no database at $DB; run engine/install.sh first" >&2; exit 1; }
    if [ -z "${CB_NO_SUDO:-}" ] && ! sudo -n true 2>/dev/null; then
        echo "error: this user needs passwordless sudo (sudo -n true failed)" >&2
        exit 1
    fi
    COMPANY=$(cb_company_name "$DB")
    if [ -z "$COMPANY" ]; then
        echo "error: the database has no company name in its meta table" >&2
        exit 1
    fi

    OLD_PREFIX=$CB_UNIT_PREFIX
    DISPATCHER=$OLD_PREFIX-dispatcher.service
    ADMIN=$OLD_PREFIX-admin.service

    # One handler runs when this script ends for any reason and cleans up
    # according to these.
    CHANGED=0
    HELD=0
    STOPPED=0
    FINISHED=0
    ADMIN_WAS_ENABLED=0
    BACKUP=""
    WORK=$HOME_DIR/.update
    umask 077
    mkdir -p "$WORK"
    # The company home is a Git repository; none of this belongs in it.
    [ -e "$WORK/.gitignore" ] || echo '*' > "$WORK/.gitignore"
    STAGING=""
    trap 'on_exit $?' EXIT

    # 1. Get the new version as a Git repository (nothing live is touched).
    if [ -n "$REPO_URL" ]; then
        STAGING=$WORK/adopt-clone-$$
        echo "Cloning $REPO_URL ..."
        cb_timeout 600 git clone --quiet "$REPO_URL" "$STAGING"
        if [ -z "$TAG" ]; then
            TAG=$(git -C "$STAGING" tag --list 'v*' --sort=-v:refname | sed -n '1p')
        fi
        if [ -z "$TAG" ] || ! git -C "$STAGING" rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null; then
            echo "error: the repository has no release tag ${TAG:-(v*)}" >&2
            exit 1
        fi
        TARGET_COMMIT=$(git -C "$STAGING" rev-parse "refs/tags/$TAG^{commit}")
        LABEL=tag:$TAG
        ORIGIN=$REPO_URL
        FROM=$STAGING
    else
        [ -n "$SOURCE" ] || SOURCE=$(cd "$ENGINE_DIR/.." && pwd -P)
        SOURCE=$(cd "$SOURCE" && pwd -P)
        if [ "$SOURCE" = "$OLD_REPO" ]; then
            echo "error: the source is the installed engine itself. Pass --repo URL or --source FOLDER." >&2
            exit 2
        fi
        if [ "$(git -C "$SOURCE" rev-parse --show-toplevel 2>/dev/null)" = "$SOURCE" ]; then
            if [ -n "$(git -C "$SOURCE" status --porcelain)" ]; then
                echo "error: $SOURCE has uncommitted changes" >&2
                exit 1
            fi
            TARGET_COMMIT=$(git -C "$SOURCE" rev-parse HEAD)
            exact=$(git -C "$SOURCE" describe --tags --exact-match 2>/dev/null || true)
            LABEL=${exact:+tag:$exact}
            LABEL=${LABEL:-commit:$(git -C "$SOURCE" rev-parse --short HEAD)}
            ORIGIN=$(git -C "$SOURCE" config --get remote.origin.url || true)
            FROM=$SOURCE
        else
            # An unpacked build: it becomes a local build in a new repository.
            STAGING=$WORK/adopt-repo-$$
            git init --quiet "$STAGING"
            imported=$("$TOOL_CB" update-import --repo "$STAGING" --dir "$SOURCE")
            TARGET_COMMIT=${imported%% *}
            LABEL=${imported##* }
            ORIGIN=""
            FROM=$STAGING
            echo "note: this is an unverified local build ($LABEL): nothing proves who made it"
        fi
    fi
    if ! git -C "$FROM" cat-file -e "$TARGET_COMMIT:engine/lib/update_finish.sh" 2>/dev/null; then
        echo "error: the new version is from before 0.0.3 and cannot finish an adoption" >&2
        exit 1
    fi
    echo "Adopting $OLD_REPO ($CB_PRODUCT $OLD_VERSION) as a clone at $LABEL ..."

    # 2. Back up the database.
    BACKUP=$HOME_DIR/backups/pre-update-$OLD_VERSION-$(date -u +%Y-%m-%d-%H%M%S).db
    cb_backup_db "$DB" "$BACKUP"
    echo "Backed up the database to $BACKUP"

    # 3. Tell the Owner, hold the queue, wait, stop. The old engine may be
    # too old for some of these commands; none of them is allowed to stop
    # the job.
    cb_timeout 90 "$TOOL_CB" update-notice --old "$OLD_VERSION" --new "$LABEL" 2>/dev/null \
        || cb_timeout 90 "$OLD_CB" admin notify "Updating $CB_PRODUCT now. The chat will be offline for a few minutes." >/dev/null 2>&1 \
        || true
    HELD=1
    cb_timeout 60 "$OLD_CB" dispatcher hold --minutes 45 --reason update \
        || echo "this version cannot hold the queue; continuing"
    cb_wait_idle "$OLD_CB" "$FORCE" || exit 1
    cb_systemctl_priv stop "$DISPATCHER"
    STOPPED=1
    STOPPED_AT=$(date +%s)
    # The hooks of a running Admin call `cb` by the engine path, which is
    # about to move, so Admin stops too. It is started again at the end.
    if cb_systemctl is-enabled --quiet "$ADMIN" 2>/dev/null; then
        ADMIN_WAS_ENABLED=1
        cb_systemctl_priv stop "$ADMIN"
    fi

    # 4. Swap the folders. The old one is kept.
    CHANGED=1
    mv -- "$OLD_REPO" "$KEEP"
    OLD_CB=$KEEP/engine/bin/cb
    git clone --quiet --no-hardlinks "$FROM" "$OLD_REPO"
    if [ -n "$ORIGIN" ]; then
        git -C "$OLD_REPO" remote set-url origin "$ORIGIN"
    else
        git -C "$OLD_REPO" remote remove origin
        echo "note: the new clone has no 'origin' remote. Add one before updating to a release tag: git -C $OLD_REPO remote add origin URL"
    fi
    if git -C "$FROM" rev-parse -q --verify refs/heads/local-builds >/dev/null; then
        git -C "$OLD_REPO" fetch --quiet "$FROM" "refs/heads/local-builds:refs/heads/local-builds"
    fi
    git -C "$OLD_REPO" checkout --quiet --detach "$TARGET_COMMIT"

    # 5. The new version finishes.
    export CB_UPD_KIND=adopt
    export CB_UPD_OLD_VERSION=$OLD_VERSION
    export CB_UPD_OLD_FOLDER=$KEEP
    export CB_UPD_BACKUP=$BACKUP
    export CB_UPD_LABEL=$LABEL
    export CB_UPD_OLD_PREFIX=$OLD_PREFIX
    export CB_UPD_STOPPED_AT=$STOPPED_AT
    status=0
    "$OLD_REPO/engine/lib/update_finish.sh" || status=$?
    result=0
    cb_finish_status "$status" || result=$?
    case $result in
        0) FINISHED=1; cleanup; exit 0 ;;
        10) FINISHED=1; cleanup; exit 1 ;;
        11) FINISHED=1; cleanup; exit 2 ;;
        *) exit "$status" ;;   # on_exit puts the old folder back
    esac
}

cleanup() {
    if [ -n "$STAGING" ] && [ -d "$STAGING" ]; then
        rm -rf -- "$STAGING"
    fi
    case $CLEANUP in
        "$WORK"/build-*) rm -rf -- "$CLEANUP" ;;
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
            cb_timeout 60 "$OLD_CB" dispatcher release >/dev/null 2>&1 || true
        fi
        if [ "$STOPPED" -eq 1 ]; then
            cb_systemctl_priv start "$DISPATCHER" 2>/dev/null || true
            if [ "$ADMIN_WAS_ENABLED" -eq 1 ]; then
                cb_systemctl_priv start "$ADMIN" 2>/dev/null || true
            fi
        fi
        cleanup
        echo "The adoption stopped (exit $status). Nothing was changed." >&2
        exit "$status"
    fi
    put_old_folder_back "$status"
}

# shellcheck disable=SC2329  # reached from on_exit
put_old_folder_back() {
    local status=$1 ok=1
    set +e
    echo "The new version could not finish (exit $status). Putting the old engine folder back ..." >&2
    if [ -d "$KEEP" ]; then
        if [ -e "$OLD_REPO" ]; then
            mv -- "$OLD_REPO" "$OLD_REPO.failed-$(date -u +%Y%m%dT%H%M%SZ)" || ok=0
        fi
        mv -- "$KEEP" "$OLD_REPO" || ok=0
    else
        ok=0
    fi
    OLD_CB=$OLD_REPO/engine/bin/cb
    cb_restore_db "$BACKUP" "$DB" || ok=0
    cb_systemctl_priv start "$DISPATCHER" || ok=0
    cb_timeout 60 "$OLD_CB" dispatcher release >/dev/null 2>&1
    if [ "$ADMIN_WAS_ENABLED" -eq 1 ]; then
        cb_systemctl_priv start "$ADMIN"
    fi
    cleanup
    if [ "$ok" -eq 1 ]; then
        cb_timeout 90 "$OLD_CB" admin notify "The $CB_PRODUCT update failed before the new version could start (exit $status). It was undone: the old engine is running again with the database from just before. Log: ${CB_UPDATE_LOG:-unknown}" >/dev/null 2>&1
        echo "The old engine is running again." >&2
        exit 1
    fi
    cat >&2 <<STEPS
The adoption FAILED and could not be undone automatically. To go back by hand:
  1. sudo systemctl stop $DISPATCHER
  2. mv "$OLD_REPO" "$OLD_REPO.failed" ; mv "$KEEP" "$OLD_REPO"
  3. rm -f "$DB-wal" "$DB-shm" && cp "$BACKUP" "$DB"
  4. sudo systemctl start $DISPATCHER   (and $ADMIN if it is enabled)
  5. "$OLD_REPO/engine/bin/cb" doctor
STEPS
    exit 2
}

main "$@"
