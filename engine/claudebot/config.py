"""Configuration: limits and paths. No company-specific text lives here."""
import json
import os
import pwd
import re
from dataclasses import dataclass, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Text settings config.json may override, with the shape each must have.
_TEXT_KEYS = {
    "run_user": re.compile(r"[a-z_][a-z0-9_-]{0,31}"),
    "run_home": re.compile(r"/[A-Za-z0-9._/-]+"),
    "claude_bin": re.compile(r"/[A-Za-z0-9._/-]+"),
    "run_memory_max": re.compile(r"[1-9][0-9]*[KMG]"),
    "default_model": re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}"),
    "timezone": re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}"),
    "transcribe_cmd": re.compile(r".+"),
}

ENV_FILE = Path("/etc/claudebot.env")  # written by install.sh: CLAUDEBOT_HOME=...


def _home_from_file() -> str | None:
    """Fallback for shells that never read ~/.bashrc (ssh host 'cb ...')."""
    try:
        for line in ENV_FILE.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "CLAUDEBOT_HOME" and value.strip():
                return value.strip()
    except OSError:
        pass
    return None


@dataclass
class Config:
    home: Path
    max_concurrent_runs: int = 2
    heavy_lane: int = 1
    memory_floor_mb: int = 500
    run_memory_max: str = "1200M"
    run_seconds: int = 1800
    lease_seconds: int = 2100
    retry_attempts: int = 3
    hop_limit: int = 8
    chain_window_s: int = 900
    wakes_per_bot_per_hour: int = 12
    # Soft hourly wake caps by model (cost control). Unknown models fall back
    # to wakes_per_bot_per_hour. Admin's urgent messages bypass the cap.
    # Overridable in config.json.
    wake_cap_opus: int = 3
    wake_cap_sonnet: int = 6
    wake_cap_haiku: int = 12
    batch_delay_seconds: int = 5
    # Workers wait longer so several task messages land in one run.
    worker_batch_delay_seconds: int = 60
    poll_seconds: int = 5
    usage_limit_retry_seconds: int = 900
    soul_cap_bytes: int = 4 * 1024
    persona_cap_bytes: int = 4 * 1024
    rules_cap_bytes: int = 4 * 1024
    memory_cap_bytes: int = 8 * 1024
    chat_rollover_tokens: int = 100000
    # Rotate a session that is this large once it has sat idle longer than
    # the prompt-cache lifetime (the next turn would rewrite all of it).
    idle_rotate_tokens: int = 80000
    cache_ttl_seconds: int = 300
    archive_offserver_pct: int = 70
    batch_max_messages: int = 20
    batch_max_bytes: int = 64 * 1024
    default_model: str = "sonnet"
    # The server account bot runs use, its home folder and the `claude`
    # program. Empty values are filled in from the account running `cb`.
    run_user: str = ""
    run_home: str = ""
    claude_bin: str = ""
    # Zone for routine schedules and for times shown to people.
    timezone: str = "UTC"
    transcribe_cmd: str = ""
    claude_projects_dir: Path | None = None

    def __post_init__(self):
        self.home = Path(self.home)
        self._fill_account()
        if self.claude_projects_dir is None:
            # Claude Code session transcripts for the account that runs the bots.
            self.claude_projects_dir = Path(self.run_home) / ".claude" / "projects"
        self.claude_projects_dir = Path(self.claude_projects_dir)
        self._validate_limits()

    def _validate_limits(self) -> None:
        if self.lease_seconds < self.run_seconds + 150:
            raise ValueError(
                f"lease_seconds ({self.lease_seconds}) must be at least "
                f"run_seconds + 150 ({self.run_seconds + 150})")

    def _fill_account(self) -> None:
        if not self.run_user:
            try:
                self.run_user = pwd.getpwuid(os.getuid()).pw_name
            except KeyError:
                self.run_user = os.environ.get("USER", "claude")
        if not self.run_home:
            try:
                self.run_home = pwd.getpwnam(self.run_user).pw_dir
            except KeyError:
                self.run_home = str(Path.home())
        if not self.claude_bin:
            self.claude_bin = f"{self.run_home}/.local/bin/claude"

    @classmethod
    def load(cls) -> "Config":
        home = os.environ.get("CLAUDEBOT_HOME") or _home_from_file()
        if not home:
            raise RuntimeError(f"CLAUDEBOT_HOME is not set (and {ENV_FILE} has none)")
        cfg = cls(Path(home))
        cfg._apply_overrides()
        from . import db  # late: db imports this module
        db.set_zone(cfg.timezone)
        return cfg

    def _apply_overrides(self) -> None:
        """Optional `<home>/config.json` overrides the numeric limits above
        and the text settings in `_TEXT_KEYS`. Unknown keys are ignored; a bad
        value stops with a clear message."""
        self.timezone_unset = False
        path = self.home / "config.json"
        data: dict = {}
        try:
            text = path.read_text()
        except FileNotFoundError:
            text = None
        except OSError as e:
            raise RuntimeError(f"cannot read {path}: {e}") from None
        if text is not None:
            try:
                data = json.loads(text)
            except ValueError as e:
                raise RuntimeError(f"{path} is not valid JSON: {e}") from None
            if not isinstance(data, dict):
                raise RuntimeError(f"{path} must hold a JSON object")
        account = any(k in data for k in ("run_user", "run_home"))
        numeric = {f.name for f in fields(self) if f.type in (int, "int")}
        for key, value in data.items():
            if key in _TEXT_KEYS:
                if not isinstance(value, str) or not _TEXT_KEYS[key].fullmatch(
                        value):
                    raise RuntimeError(f"{path}: {key} has a bad value: "
                                       f"{value!r}")
                if key == "timezone":
                    try:
                        ZoneInfo(value)
                    except (ZoneInfoNotFoundError, ValueError):
                        raise RuntimeError(
                            f"{path}: unknown timezone {value!r}") from None
                setattr(self, key, value)
                continue
            if key not in numeric:
                continue
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 1:
                raise RuntimeError(
                    f"{path}: {key} must be a whole number of 1 or more, "
                    f"not {value!r}")
            setattr(self, key, value)
        if account and "claude_bin" not in data:
            if "run_home" not in data:
                self.run_home = ""
            self.claude_bin = ""
            self._fill_account()
        self._validate_limits()
        if "timezone" not in data and self._legacy_company_clock():
            self.timezone = "America/Chicago"
            self.timezone_unset = True

    def _legacy_company_clock(self) -> bool:
        """True when this company already has schedules and no timezone key.

        Those schedules were written for America/Chicago. Keep that zone
        until config.json names one.
        """
        if not self.db_path.is_file():
            return False
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
        try:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "routines" in names and conn.execute(
                    "SELECT 1 FROM routines LIMIT 1").fetchone():
                return True
            if "decisions" in names and conn.execute(
                    "SELECT 1 FROM decisions LIMIT 1").fetchone():
                return True
            return False
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    @property
    def db_path(self) -> Path:
        return self.home / "company.db"

    @property
    def search_path(self) -> Path:
        return self.home / "search.db"

    @property
    def bots_dir(self) -> Path:
        return self.home / "bots"

    @property
    def work_dir(self) -> Path:
        return self.home / "work"

    @property
    def knowledge_dir(self) -> Path:
        return self.home / "knowledge"

    @property
    def archive_dir(self) -> Path:
        return self.home / "archive"

    @property
    def secrets_dir(self) -> Path:
        return self.home / ".secrets"
