# shellcheck shell=bash
# Render the engine's systemd units into /etc/systemd/system.
# Sourced by install.sh and by the update's second half. Not executed.
# shellcheck source=sysfiles.sh
source "$(dirname "${BASH_SOURCE[0]}")/sysfiles.sh"
# shellcheck source=names.sh
source "$(dirname "${BASH_SOURCE[0]}")/names.sh"

install_units() {
    local home=$1 engine=$2 user=$3 memory=$4
    local src=$engine/systemd dest=${CB_SYSTEMD_DIR:-/etc/systemd/system}
    local name tmp
    for name in \
            "$CB_UNIT_PREFIX-dispatcher.service" \
            "$CB_UNIT_PREFIX-backup.service" \
            "$CB_UNIT_PREFIX-backup.timer" \
            "$CB_UNIT_PREFIX-doctor.service" \
            "$CB_UNIT_PREFIX-doctor.timer" \
            "$CB_UNIT_PREFIX-admin.service"
    do
        if [ ! -f "$src/$name" ]; then
            echo "error: missing unit $src/$name" >&2
            return 1
        fi
        tmp=$(mktemp)
        sed -e "s|@HOME@|$home|g" \
            -e "s|@ENGINE@|$engine|g" \
            -e "s|@USER@|$user|g" \
            -e "s|@MEMORY_MAX@|$memory|g" \
            "$src/$name" > "$tmp"
        if ! install_root_file "$tmp" "$dest/$name" 644; then
            rm -f -- "$tmp"
            return 1
        fi
        rm -f -- "$tmp"
        echo "Installed $dest/$name"
    done
}

# Handover from an updater older than 0.0.3. Those scripts move the checkout
# to the new version and then source this file to install the units, with
# their own steps (and their own unit names) still to come. They know nothing
# about migrations, rollback or a renamed service, so the new version takes
# over here and the old script never continues. CHANGED and OLD_REF are that
# script's own variables; CB_UPD_KIND is set only by the current updater.
if [ "${CHANGED:-0}" = "1" ] && [ -n "${OLD_REF:-}" ] && [ -z "${CB_UPD_KIND:-}" ]; then
    trap - ERR
    echo "Handing the rest of this update to the new version ..."
    export CB_UPD_KIND=git
    export CB_UPD_LEGACY=1
    export CB_UPD_OLD_VERSION=${OLD_VERSION:-}
    export CB_UPD_OLD_REF=$OLD_REF
    export CB_UPD_BACKUP=${BACKUP:-}
    export CB_UPD_LABEL="tag:${TAG:-unknown}"
    export CB_UPD_FORCE=${FORCE:-0}
    export CB_UPD_OLD_PREFIX=claudebot
    exec "$(dirname "${BASH_SOURCE[0]}")/update_finish.sh"
fi
