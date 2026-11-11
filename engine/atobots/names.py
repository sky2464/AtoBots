"""The names this engine uses on a server, in one place.

`engine/lib/names.sh` holds the same values for the shell scripts; a test
keeps the two in step.
"""

PRODUCT = "AtoBots"
UNIT_PREFIX = "atobots"
HOME_VAR = "ATOBOTS_HOME"
ENV_FILE = "/etc/atobots.env"

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
    """The company home for a child process."""
    return {HOME_VAR: str(home)}
