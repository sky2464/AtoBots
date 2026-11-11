# shellcheck shell=bash
# Install the root-owned `cb` wrapper that refuses to run as root, and the
# other links an install needs.
#
#   install_cb_wrapper ENGINE_DIR [TARGET]
#
# TARGET defaults to /usr/local/bin/cb. It replaces any old file or symlink at
# that path and never writes through one (see install_root_file).
# shellcheck source=sysfiles.sh
source "$(dirname "${BASH_SOURCE[0]}")/sysfiles.sh"
# shellcheck source=names.sh
source "$(dirname "${BASH_SOURCE[0]}")/names.sh"

install_cb_wrapper() {
    local engine_dir=$1
    local target=${2:-${CB_USR_BIN_DIR:-/usr/local/bin}/cb}
    local tmp status=0
    tmp=$(mktemp)
    cat > "$tmp" <<WRAPPER
#!/bin/sh
[ "\$(id -u)" -ne 0 ] || { echo "cb: do not run as root" >&2; exit 1; }
exec $engine_dir/bin/cb "\$@"
WRAPPER
    install_root_file "$tmp" "$target" 755 || status=1
    rm -f -- "$tmp"
    return "$status"
}

# write_env_file HOME_DIR   Record the company home for shells that never
# read ~/.bashrc (ssh host 'cb ...').
write_env_file() {
    local home=$1 tmp status=0
    tmp=$(mktemp)
    echo "$CB_HOME_VAR=$home" > "$tmp"
    install_root_file "$tmp" "$CB_ENV_FILE" 644 || status=1
    rm -f -- "$tmp"
    return "$status"
}

# install_links ENGINE_DIR HOME_DIR   The `cb` command for the bots' account
# and for every other shell, plus the env file. Safe to repeat.
install_links() {
    local engine_dir=$1 home=$2
    local bin_dir=${CB_USER_BIN_DIR:-$HOME/.local/bin}
    mkdir -p "$bin_dir"
    ln -sfn "$engine_dir/bin/cb" "$bin_dir/cb"
    echo "Linked $bin_dir/cb"
    install_cb_wrapper "$engine_dir"
    write_env_file "$home"
    echo "Installed ${CB_USR_BIN_DIR:-/usr/local/bin}/cb and wrote $CB_ENV_FILE"
}
