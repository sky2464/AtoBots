"""Configuration: limits and paths. No company-specific text lives here."""
import os
from dataclasses import dataclass
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
    batch_delay_seconds: int = 5
    poll_seconds: int = 5
    usage_limit_retry_seconds: int = 900
    soul_cap_bytes: int = 4 * 1024
    persona_cap_bytes: int = 4 * 1024
    memory_cap_bytes: int = 8 * 1024
    chat_rollover_tokens: int = 100000
    archive_offserver_pct: int = 70
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
        return cls(Path(home))

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
