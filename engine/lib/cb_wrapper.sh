# shellcheck shell=bash
# Install the root-owned `cb` wrapper that refuses to run as root.
#
# Older installs had /usr/local/bin/cb as a symlink into the engine clone.
# Writing the wrapper with `tee` through that symlink overwrote the clone's own
# engine/bin/cb with the wrapper, which then ran itself. So this never writes
# through an existing path: it removes whatever is at the target (a file or a
# symlink; the link itself, never what it points at), then installs the new
# file from a temp file.
#
#   install_cb_wrapper ENGINE_DIR [TARGET]
#
# TARGET defaults to /usr/local/bin/cb. CB_NO_SUDO=1 skips sudo (used by tests
# that install into a temporary folder).
install_cb_wrapper() {
    local engine_dir=$1
    local target=${2:-/usr/local/bin/cb}
    local -a priv=(sudo)
    if [ -n "${CB_NO_SUDO:-}" ]; then
        priv=()
    fi
    local tmp
    tmp=$(mktemp)
    cat > "$tmp" <<EOF
#!/bin/sh
[ "\$(id -u)" -ne 0 ] || { echo "cb: do not run as root" >&2; exit 1; }
exec $engine_dir/bin/cb "\$@"
EOF
    if ! "${priv[@]}" rm -f -- "$target"; then
        rm -f -- "$tmp"
        echo "error: could not remove the old $target" >&2
        return 1
    fi
    if ! "${priv[@]}" install -m 755 "$tmp" "$target"; then
        rm -f -- "$tmp"
        echo "error: could not install $target" >&2
        return 1
    fi
    rm -f -- "$tmp"
}
