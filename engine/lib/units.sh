# shellcheck shell=bash
# Render the engine's systemd units into /etc/systemd/system.
# Sourced by install.sh and update.sh. Not executed on its own.
install_units() {
    local home=$1 engine=$2 user=$3 memory=$4
    local src=$engine/systemd dest=/etc/systemd/system
    local name
    for name in \
            claudebot-dispatcher.service \
            claudebot-backup.service \
            claudebot-backup.timer \
            claudebot-doctor.service \
            claudebot-doctor.timer \
            claudebot-admin.service
    do
        if [ ! -f "$src/$name" ]; then
            echo "error: missing unit $src/$name" >&2
            return 1
        fi
        sed -e "s|@HOME@|$home|g" \
            -e "s|@ENGINE@|$engine|g" \
            -e "s|@USER@|$user|g" \
            -e "s|@MEMORY_MAX@|$memory|g" \
            "$src/$name" | sudo tee "$dest/$name" > /dev/null
        echo "Installed $dest/$name"
    done
}
