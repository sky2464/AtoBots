"""The names this engine uses on a server, in one place.

`engine/lib/names.sh` holds the same values for the shell scripts; a test
keeps the two in step.
"""

PRODUCT = "AtoBots"
UNIT_PREFIX = "atobots"
HOME_VAR = "ATOBOTS_HOME"
ENV_FILE = "/etc/atobots.env"
# The project's name before 0.1.0. An install made under it keeps working:
# the old variable and env file are still read (cb doctor says when they are
# what is being used), and bot runs are given both variables, so a company
# script that says $CLAUDEBOT_HOME does not break. To be removed one release
# after 0.1.0.
OLD_PRODUCT = "ClaudeBot"
OLD_HOME_VARS: tuple = ("CLAUDEBOT_HOME",)
OLD_ENV_FILES: tuple = ("/etc/claudebot.env",)
OLD_PREFIXES: tuple = ("claudebot",)
# Every unit prefix a server may still carry (newest first).
KNOWN_PREFIXES = ("atobots",) + OLD_PREFIXES

# The units an install has: the part after "<prefix>-".
UNIT_ROLES = ("dispatcher.service", "backup.service", "backup.timer",
              "doctor.service", "doctor.timer", "admin.service")
# The ones that are enabled (the two timers start their services).
ENABLE_ROLES = ("dispatcher.service", "backup.timer", "doctor.timer",
                "admin.service")


def unit(role: str, prefix: str | None = None) -> str:
    """`unit("dispatcher.service")` -> the installed unit's name."""
    return f"{prefix or UNIT_PREFIX}-{role}"


def home_env(home) -> dict:
    """The company home for a child process, under the current name and the
    earlier ones."""
    return {var: str(home) for var in (HOME_VAR,) + OLD_HOME_VARS}
