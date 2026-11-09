#!/usr/bin/env bash
# Install ClaudeBot on this server. Run by hand as the user `claude`:
#   engine/install.sh COMPANY_NAME /home/claude/COMPANY_HOME
# Safe to run again. It sets up the company (cb init), installs the systemd
# units, links `cb` into ~/.local/bin and records CLAUDEBOT_HOME in ~/.bashrc.
# It does not touch SSH, the firewall or sudo rules.
set -euo pipefail

usage() {
    echo "usage: $0 COMPANY_NAME /absolute/path/to/company/home" >&2
    exit 2
}

[ "$#" -eq 2 ] || usage
COMPANY=$1
HOME_DIR=$2
[ -n "$COMPANY" ] || usage
case $HOME_DIR in
    /*) ;;
    *) echo "error: the company home must be an absolute path" >&2; exit 2 ;;
esac
if ! [[ $HOME_DIR =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "error: the company home may only use letters, digits, . _ - and /" >&2
    exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
    echo "error: run this as the user claude, not root" >&2
    exit 2
fi

ENGINE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
UNIT_SRC=$ENGINE_DIR/systemd
UNIT_DST=/etc/systemd/system
BIN_DIR=$HOME/.local/bin
BASHRC=$HOME/.bashrc

# Units to start now. Task 14 adds the backup and doctor timers: list them in
# TIMERS and the loop below installs, enables and starts them.
SERVICES=(claudebot-dispatcher.service)
TIMERS=()

[ -x "$ENGINE_DIR/bin/cb" ] || { echo "error: $ENGINE_DIR/bin/cb is missing" >&2; exit 1; }

# 1. Set up the company first. If this fails, nothing below runs.
echo "Setting up $COMPANY at $HOME_DIR ..."
CLAUDEBOT_HOME=$HOME_DIR "$ENGINE_DIR/bin/cb" init --company "$COMPANY"

# 2. The cb command.
mkdir -p "$BIN_DIR"
ln -sfn "$ENGINE_DIR/bin/cb" "$BIN_DIR/cb"
echo "Linked $BIN_DIR/cb"

# 3. Shell settings, added only if absent.
touch "$BASHRC"
if ! grep -q '^export CLAUDEBOT_HOME=' "$BASHRC"; then
    echo "export CLAUDEBOT_HOME=$HOME_DIR" >> "$BASHRC"
    echo "Added CLAUDEBOT_HOME to $BASHRC"
fi
if ! grep -q 'claudebot-path' "$BASHRC"; then
    # shellcheck disable=SC2016  # written literally; expands when bashrc runs
    echo 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac  # claudebot-path' >> "$BASHRC"
    echo "Added ~/.local/bin to PATH in $BASHRC"
fi

# 4. Systemd units, with @HOME@ filled in.
render_unit() {
    local name=$1
    [ -f "$UNIT_SRC/$name" ] || { echo "error: missing unit $UNIT_SRC/$name" >&2; exit 1; }
    sed "s|@HOME@|$HOME_DIR|g" "$UNIT_SRC/$name" | sudo tee "$UNIT_DST/$name" > /dev/null
    echo "Installed $UNIT_DST/$name"
}

for unit in "${SERVICES[@]}"; do
    render_unit "$unit"
done
for unit in "${TIMERS[@]}"; do
    render_unit "$unit"
    render_unit "${unit%.timer}.service"
done

sudo systemctl daemon-reload

for unit in "${SERVICES[@]}" "${TIMERS[@]}"; do
    sudo systemctl enable "$unit"
    sudo systemctl restart "$unit"
    echo "Started $unit"
done

echo "Done. Open a new shell (or run: source ~/.bashrc), then try: cb status"
