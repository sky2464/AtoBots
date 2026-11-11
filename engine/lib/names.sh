# shellcheck shell=bash
# The names this engine uses on a server, in one place. Sourced by the
# installer, the updater and the migration scripts. Tests point CB_ETC_DIR at a
# temporary folder.
# shellcheck disable=SC2034
CB_PRODUCT="ClaudeBot"
CB_UNIT_PREFIX="claudebot"
CB_HOME_VAR="CLAUDEBOT_HOME"
CB_ENV_FILE="${CB_ETC_DIR:-/etc}/claudebot.env"
