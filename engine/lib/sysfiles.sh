# shellcheck shell=bash
# Helpers for the few things the engine does as root: writing a file under
# /etc or /usr/local and asking systemd. Sourced, never executed.
#
# Test switches (never set on a real server):
#   CB_NO_SUDO=1      run the command without sudo
#   CB_SYSTEMCTL=PATH use this program instead of systemctl
#   CB_SYSTEMD_DIR, CB_ETC_DIR, CB_USR_BIN_DIR  replace the system folders

cb_priv() {
    if [ -n "${CB_NO_SUDO:-}" ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Read-only questions to systemd (is-active, is-enabled).
cb_systemctl() {
    "${CB_SYSTEMCTL:-systemctl}" "$@"
}

# Changes (start, stop, enable, daemon-reload).
cb_systemctl_priv() {
    cb_priv "${CB_SYSTEMCTL:-systemctl}" "$@"
}

# install_root_file SOURCE TARGET [MODE]
# Put SOURCE at TARGET without ever writing through what is there now. An
# older install had /usr/local/bin/cb as a symlink into the engine clone, and
# `tee` through it overwrote the clone's own file. So the old path (a file or
# a symlink; the link itself, never what it points at) is removed first.
install_root_file() {
    local src=$1 target=$2 mode=${3:-644}
    if ! cb_priv rm -f -- "$target"; then
        echo "error: could not remove the old $target" >&2
        return 1
    fi
    if ! cb_priv install -m "$mode" "$src" "$target"; then
        echo "error: could not install $target" >&2
        return 1
    fi
}

# cb_timeout SECONDS COMMAND...  A step that hangs must end with a message,
# not wait forever. Without the `timeout` program the command runs as it is.
cb_timeout() {
    local seconds=$1
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
    else
        "$@"
    fi
}
