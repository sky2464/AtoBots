# shellcheck shell=bash
# Make `python` resolve on this server, as an alias for the python3 already
# installed. Hooks and routines that say bare `python` then work everywhere.
# It only ever points at the newest python3 the system has: it never installs,
# downgrades or replaces anything, and it leaves an existing `python` alone.
# shellcheck source=sysfiles.sh
source "$(dirname "${BASH_SOURCE[0]}")/sysfiles.sh"

ensure_python_alias() {
    if command -v python >/dev/null 2>&1; then
        echo "python already resolves to $(command -v python)"
        return 0
    fi
    local target dest=${CB_USR_BIN_DIR:-/usr/local/bin}/python
    target=$(command -v python3 2>/dev/null || true)
    if [ -z "$target" ]; then
        echo "warning: python3 was not found, so no python alias was made" >&2
        return 0
    fi
    target=$(readlink -f "$target" 2>/dev/null || echo "$target")
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        echo "$dest already exists and was left alone"
        return 0
    fi
    if cb_priv ln -s "$target" "$dest"; then
        echo "Linked $dest -> $target"
    else
        echo "warning: could not create $dest" >&2
    fi
}
