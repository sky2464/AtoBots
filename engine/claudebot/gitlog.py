"""Git history of the company home: one commit per finished run.

Git settings are local to the repo and the calls ignore global and system
config, so the result does not depend on who runs them.
"""
import fcntl
import os
import subprocess
from contextlib import contextmanager

from .config import Config

GITIGNORE = (".secrets/", "*.db", "*.db-wal", "*.db-shm", "archive/", "media/",
             ".claude/projects/")
_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0"}


def _git(cfg: Config, *args, env=None, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cfg.home, capture_output=True,
                       text=True, env={**os.environ, **_ENV, **(env or {})})
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()}")
    return r


def ensure_repo(cfg: Config) -> None:
    """Create the repo, local identity and .gitignore. Safe to repeat."""
    cfg.home.mkdir(parents=True, exist_ok=True)
    if not (cfg.home / ".git").exists():
        _git(cfg, "init", "-q", "-b", "main")
    with _locked(cfg):
        for key, value in (("user.name", "claudebot"),
                           ("user.email", "claudebot@claudebot.local"),
                           ("commit.gpgsign", "false")):
            cur = _git(cfg, "config", "--local", key, check=False).stdout.strip()
            if cur != value:
                _git(cfg, "config", "--local", key, value)
        path = cfg.home / ".gitignore"
        have = path.read_text().split("\n") if path.exists() else []
        missing = [p for p in GITIGNORE if p not in have]
        if missing:
            text = "".join(line + "\n" for line in have if line)
            path.write_text(text + "\n".join(missing) + "\n")


@contextmanager
def _locked(cfg: Config):
    with open(cfg.home / ".git" / "cb.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def commit_run(cfg: Config, bot: str, item_ids, run_id: int) -> str | None:
    """Commit all changes as `bot`. Returns the commit ID, or None if nothing
    changed. Commits are serialised by a lock, so parallel finishes queue up."""
    ensure_repo(cfg)
    items = ", ".join(item_ids) if item_ids else "no item"
    who = f"{bot} <{bot}@claudebot.local>"
    with _locked(cfg):
        _git(cfg, "add", "-A")
        # Belt and braces in case .gitignore was edited: never keep .secrets.
        _git(cfg, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
             ".secrets")
        if _git(cfg, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        _git(cfg, "commit", "-q", "--author", who,
             "-m", f"{bot}: {items} (run {run_id})",
             env={"GIT_COMMITTER_NAME": bot,
                  "GIT_COMMITTER_EMAIL": f"{bot}@claudebot.local"})
        return _git(cfg, "rev-parse", "HEAD").stdout.strip()
