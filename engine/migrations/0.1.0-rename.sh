#!/usr/bin/env bash
# 0.1.0 renamed the project from ClaudeBot to AtoBots. This moves an existing
# install to the new names. It only adds: everything under the old names is
# left in place, so the old version can still run if the update is rolled
# back. Safe to run again.
#
# The update itself does the rest: it installs the atobots-* units, stops,
# disables and removes the claudebot-* ones, enables the same units as
# before, and writes /etc/atobots.env.
#
# What stays as it is, on purpose:
# - the `cb` command, the company home and everything in it, and secrets;
# - the engine folder's own name (rename it by hand if you want; see
#   UPDATING.md);
# - /etc/claudebot.env and the CLAUDEBOT_HOME line in ~/.bashrc, which are
#   still read for one release.
set -euo pipefail

ENGINE_DIR=${CB_ENGINE_DIR:?}
HOME_DIR=${CB_HOME_DIR:?}
# shellcheck source=../lib/names.sh
source "$ENGINE_DIR/lib/names.sh"

# 1. The shell of the bots' account: add the new variable beside the old one.
BASHRC=${CB_BASHRC:-$HOME/.bashrc}
if [ -f "$BASHRC" ] && ! grep -q "^export $CB_HOME_VAR=" "$BASHRC"; then
    echo "export $CB_HOME_VAR=$HOME_DIR" >> "$BASHRC"
    echo "Added $CB_HOME_VAR to $BASHRC (the $CB_OLD_HOME_VAR line was kept)"
else
    echo "$BASHRC already has $CB_HOME_VAR, or does not exist"
fi

# 2. The release-signing key setting of the engine clone, under its new name.
REPO_DIR=$(cd "$ENGINE_DIR/.." && pwd -P)
old_key=$(git -C "$REPO_DIR" config --get "$CB_OLD_UNIT_PREFIX.releaseKey" 2>/dev/null || true)
new_key=$(git -C "$REPO_DIR" config --get "$CB_UNIT_PREFIX.releaseKey" 2>/dev/null || true)
if [ -n "$old_key" ] && [ -z "$new_key" ]; then
    git -C "$REPO_DIR" config "$CB_UNIT_PREFIX.releaseKey" "$old_key"
    echo "Copied the release key setting to $CB_UNIT_PREFIX.releaseKey"
fi

echo "Rename migration done."
