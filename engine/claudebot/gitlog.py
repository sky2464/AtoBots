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
             ".claude/projects/", ".chat/", ".chat.lock", "capture-failed.log",
             "*.pid")
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
                           ("commit.gpgsign", "false"),
                           ("gc.auto", "1000")):
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


def _commit(cfg: Config, who: str, message: str, add: list) -> str | None:
    """Caller holds the lock. Stage `add` (pathspecs), commit only the changed
    files under them, so nothing else that is staged or modified goes in."""
    _git(cfg, "add", "-A", "--", *add)
    # Belt and braces in case .gitignore was edited: never keep .secrets.
    _git(cfg, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", ".secrets")
    files = _git(cfg, "diff", "--cached", "--name-only", "-z", "--",
                 *add).stdout.split("\0")
    files = [f for f in files if f]
    if not files:
        return None
    _git(cfg, "commit", "-q", "--author", f"{who} <{who}@claudebot.local>",
         "-m", message, "--", *files,
         env={"GIT_COMMITTER_NAME": who,
              "GIT_COMMITTER_EMAIL": f"{who}@claudebot.local"})
    return _git(cfg, "rev-parse", "HEAD").stdout.strip()


def _known(cfg: Config, paths) -> list:
    """Paths that exist on disk or are tracked (so deletions are staged)."""
    out = []
    for p in paths:
        if (cfg.home / p).exists() or _git(
                cfg, "ls-files", "--", p).stdout.strip():
            out.append(p)
    return out


def commit_run(cfg: Config, bot: str, item_ids, run_id: int) -> str | None:
    """Commit this run's own files as `bot`: bots/<bot>/, knowledge/<bot>/ and
    work/<item>/ for its items. Other bots' work in progress is left alone.
    Returns the commit ID, or None if nothing changed."""
    ensure_repo(cfg)
    items = ", ".join(item_ids) if item_ids else "no item"
    paths = [f"bots/{bot}", f"knowledge/{bot}",
             *[f"work/{i}" for i in item_ids]]
    with _locked(cfg):
        add = _known(cfg, paths)
        if not add:
            return None
        return _commit(cfg, bot, f"{bot}: {items} (run {run_id})", add)


def commit_sweep(cfg: Config, message: str) -> str | None:
    """Commit everything else (shared files, leftovers) as `system`."""
    ensure_repo(cfg)
    with _locked(cfg):
        return _commit(cfg, "system", message, ["."])
