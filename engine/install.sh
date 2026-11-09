#!/usr/bin/env bash
# Install ClaudeBot on this server. Run by hand as the user `claude`:
#   engine/install.sh [--start] COMPANY_NAME /home/claude/COMPANY_HOME
# The dispatcher and timers are enabled (they start at boot) but are NOT started
# now unless you pass --start, so you can look around first.
# Safe to run again. It sets up the company (cb init), installs the systemd
# units, links `cb` into ~/.local/bin and records CLAUDEBOT_HOME in ~/.bashrc.
# It does not touch SSH, the firewall or sudo rules.
set -euo pipefail

usage() {
    echo "usage: $0 [--start] COMPANY_NAME /absolute/path/to/company/home" >&2
    exit 2
}

START=0
if [ "${1:-}" = "--start" ]; then
    START=1
    shift
fi
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
if ! [[ $ENGINE_DIR =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "error: the engine path may only use letters, digits, . _ - and /" >&2
    exit 2
fi
UNIT_SRC=$ENGINE_DIR/systemd
UNIT_DST=/etc/systemd/system
BIN_DIR=$HOME/.local/bin
BASHRC=$HOME/.bashrc

# Units to install and enable (started only with --start). Each timer needs a
# matching .service next to it.
SERVICES=(claudebot-dispatcher.service)
# The always-on Admin (Telegram) is installed but enabled only once the bot
# token is configured: enabling makes it start at every boot.
ADMIN_UNIT=claudebot-admin.service
TELEGRAM_ENV=$HOME/.claude/channels/telegram/.env
TIMERS=(claudebot-backup.timer claudebot-doctor.timer)

# 0. Check everything we need before changing anything, so this either does
# the whole job or stops here with a clear message.
CLAUDE_BIN=/home/claude/.local/bin/claude
[ -x "$ENGINE_DIR/bin/cb" ] || { echo "error: $ENGINE_DIR/bin/cb is missing" >&2; exit 1; }
if ! sudo -n true 2>/dev/null; then
    echo "error: this user needs passwordless sudo (sudo -n true failed)" >&2
    exit 1
fi
[ -x "$CLAUDE_BIN" ] || { echo "error: claude is not at $CLAUDE_BIN; install Claude Code first" >&2; exit 1; }
[ -f "$UNIT_SRC/$ADMIN_UNIT" ] || { echo "error: missing unit $UNIT_SRC/$ADMIN_UNIT" >&2; exit 1; }
for unit in "${SERVICES[@]}" "${TIMERS[@]}"; do
    [ -f "$UNIT_SRC/$unit" ] || { echo "error: missing unit $UNIT_SRC/$unit" >&2; exit 1; }
    case $unit in
        *.timer) [ -f "$UNIT_SRC/${unit%.timer}.service" ] || { echo "error: missing unit $UNIT_SRC/${unit%.timer}.service" >&2; exit 1; } ;;
    esac
done

# 1. Set up the company first. If this fails, nothing below runs.
echo "Setting up $COMPANY at $HOME_DIR ..."
CLAUDEBOT_HOME=$HOME_DIR "$ENGINE_DIR/bin/cb" init --company "$COMPANY"

# 2. The cb command.
mkdir -p "$BIN_DIR"
ln -sfn "$ENGINE_DIR/bin/cb" "$BIN_DIR/cb"
echo "Linked $BIN_DIR/cb"
# Also on the default PATH and in an env file, for `ssh host 'cb ...'`, which
# never reads ~/.bashrc.
sudo ln -sfn "$ENGINE_DIR/bin/cb" /usr/local/bin/cb
echo "CLAUDEBOT_HOME=$HOME_DIR" | sudo tee /etc/claudebot.env > /dev/null
echo "Linked /usr/local/bin/cb and wrote /etc/claudebot.env"

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

# 4. Systemd units, with @HOME@ and @ENGINE@ filled in.
render_unit() {
    local name=$1
    [ -f "$UNIT_SRC/$name" ] || { echo "error: missing unit $UNIT_SRC/$name" >&2; exit 1; }
    sed -e "s|@HOME@|$HOME_DIR|g" -e "s|@ENGINE@|$ENGINE_DIR|g" "$UNIT_SRC/$name" | sudo tee "$UNIT_DST/$name" > /dev/null
    echo "Installed $UNIT_DST/$name"
}

for unit in "${SERVICES[@]}" "$ADMIN_UNIT"; do
    render_unit "$unit"
done
for unit in "${TIMERS[@]}"; do
    render_unit "$unit"
    render_unit "${unit%.timer}.service"
done

sudo systemctl daemon-reload

for unit in "${SERVICES[@]}" "${TIMERS[@]}"; do
    sudo systemctl enable "$unit"
    if [ "$START" -eq 1 ]; then
        sudo systemctl restart "$unit"
        echo "Started $unit"
    else
        echo "Enabled $unit (not started)"
    fi
done

# The always-on Admin: only with a configured Telegram token (presence is
# checked; the value is never read or printed).
if grep -qs '^TELEGRAM_BOT_TOKEN=.' "$TELEGRAM_ENV"; then
    sudo systemctl enable "$ADMIN_UNIT"
    if [ "$START" -eq 1 ]; then
        sudo systemctl restart "$ADMIN_UNIT"
        echo "Started $ADMIN_UNIT (attach: tmux attach -t admin-live)"
    else
        echo "Enabled $ADMIN_UNIT (not started)"
    fi
else
    echo "Not enabled: $ADMIN_UNIT (always-on Admin with Telegram). Missing:"
    echo "  the bot token in $TELEGRAM_ENV. Save it from a plain shell (never"
    echo "  inside a claude session); the exact command is in CLAUDE.md."
    echo "  When ready: sudo systemctl enable --now $ADMIN_UNIT"
fi

echo "Done. Open a new shell (or run: source ~/.bashrc), then try: cb status"
if [ "$START" -eq 0 ]; then
    echo "Nothing is running yet. When you are ready, start it with:"
    echo "  sudo systemctl start ${SERVICES[*]} ${TIMERS[*]}"
    echo "(or run this installer again with --start)"
fi
