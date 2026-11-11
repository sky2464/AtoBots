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
