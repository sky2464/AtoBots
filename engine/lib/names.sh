# shellcheck shell=bash
# The names this engine uses on a server, in one place. Sourced by the
# installer, the updater and the migration scripts. Tests point CB_ETC_DIR at a
# temporary folder.
# shellcheck disable=SC2034
CB_PRODUCT="AtoBots"
CB_UNIT_PREFIX="atobots"
CB_HOME_VAR="ATOBOTS_HOME"
CB_ENV_FILE="${CB_ETC_DIR:-/etc}/atobots.env"
