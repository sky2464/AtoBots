# shellcheck shell=bash
# Make `python` resolve on this server, as an alias for the python3 already
# installed. Hooks and routines that say bare `python` then work everywhere.
# It only ever points at the newest python3 the system has: it never installs,
# downgrades or replaces anything, and it leaves an existing `python` alone.
ensure_python_alias() {
    if command -v python >/dev/null 2>&1; then
        echo "python already resolves to $(command -v python)"
        return 0
    fi
    local target
    target=$(command -v python3 2>/dev/null || true)
    if [ -z "$target" ]; then
        echo "warning: python3 was not found, so no python alias was made" >&2
        return 0
    fi
    target=$(readlink -f "$target" 2>/dev/null || echo "$target")
    if sudo ln -s "$target" /usr/local/bin/python; then
        echo "Linked /usr/local/bin/python -> $target"
    else
        echo "warning: could not create /usr/local/bin/python" >&2
    fi
}
