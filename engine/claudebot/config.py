"""Configuration: limits and paths. No company-specific text lives here."""
import json
import os
from dataclasses import dataclass, fields
from pathlib import Path


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
    # to wakes_per_bot_per_hour. Overridable in config.json.
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
    memory_cap_bytes: int = 8 * 1024
    chat_rollover_tokens: int = 100000
    archive_offserver_pct: int = 70
    batch_max_messages: int = 20
    batch_max_bytes: int = 64 * 1024
    default_model: str = "sonnet"
    claude_bin: str = "/home/claude/.local/bin/claude"
    claude_projects_dir: Path | None = None

    def __post_init__(self):
        self.home = Path(self.home)
        if self.claude_projects_dir is None:
            # where Claude Code keeps session transcripts for the claude user
            self.claude_projects_dir = Path.home() / ".claude" / "projects"
        self.claude_projects_dir = Path(self.claude_projects_dir)

    @classmethod
    def load(cls) -> "Config":
        home = os.environ.get("CLAUDEBOT_HOME") or _home_from_file()
        if not home:
            raise RuntimeError(f"CLAUDEBOT_HOME is not set (and {ENV_FILE} has none)")
        cfg = cls(Path(home))
        cfg._apply_overrides()
        return cfg

    def _apply_overrides(self) -> None:
        """Optional `<home>/config.json` overrides the numeric limits above.
        Unknown keys are ignored; a bad value stops with a clear message."""
        path = self.home / "config.json"
        try:
            text = path.read_text()
        except FileNotFoundError:
            return
        except OSError as e:
            raise RuntimeError(f"cannot read {path}: {e}") from None
        try:
            data = json.loads(text)
        except ValueError as e:
            raise RuntimeError(f"{path} is not valid JSON: {e}") from None
        if not isinstance(data, dict):
            raise RuntimeError(f"{path} must hold a JSON object")
        numeric = {f.name for f in fields(self) if f.type in (int, "int")}
        for key, value in data.items():
            if key not in numeric:
                continue
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 1:
                raise RuntimeError(
                    f"{path}: {key} must be a whole number of 1 or more, "
                    f"not {value!r}")
            setattr(self, key, value)

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
