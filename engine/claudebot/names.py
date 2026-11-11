"""The names this engine uses on a server, in one place.

`engine/lib/names.sh` holds the same values for the shell scripts; a test
keeps the two in step.
"""

PRODUCT = "ClaudeBot"
UNIT_PREFIX = "claudebot"
HOME_VAR = "CLAUDEBOT_HOME"
ENV_FILE = "/etc/claudebot.env"
# Earlier names, still read so an install keeps working across a rename.
OLD_HOME_VARS: tuple = ()
OLD_ENV_FILES: tuple = ()
# Every unit prefix a server may still carry (newest first).
KNOWN_PREFIXES = ("claudebot",)

# The units an install has: the part after "<prefix>-".
UNIT_ROLES = ("dispatcher.service", "backup.service", "backup.timer",
              "doctor.service", "doctor.timer", "admin.service")
# The ones that are enabled (the two timers start their services).
ENABLE_ROLES = ("dispatcher.service", "backup.timer", "doctor.timer",
                "admin.service")


def unit(role: str, prefix: str | None = None) -> str:
    """`unit("dispatcher.service")` -> the installed unit's name."""
    return f"{prefix or UNIT_PREFIX}-{role}"
