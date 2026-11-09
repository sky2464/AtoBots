"""Backups: plain scripts, no AI.

* snapshot: the database is copied with SQLite's backup API, dumped as text to
  `backup/company.sql` and committed to the company Git repo (one hourly
  commit, only when something changed).
* push: `git push` to the remote named `backup`, never forced. No remote yet is
  a note, not an error. A failed push tells Admin once a day.
* restore: rebuilds a company home from a backup repo (or a bare company.sql)
  into an empty or missing folder, then runs PRAGMA integrity_check. It never
  writes into a home that already has anything in it.
* offsite_archive: when the disk is filling up, old archive months move to the
  remote named `archive`. Local copies go only after the push is verified by
  comparing the remote commit's files with the local ones.

Secrets never leave the server. `.secrets/` is git-ignored, and the key that
decrypts the store exists only there, so after a server loss the store's
secrets must be entered again.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import archive, gitlog, messages
from .config import Config
from .db import connect, utcnow

BACKUP_REMOTE = "backup"
ARCHIVE_REMOTE = "archive"
SQL_REL = "backup/company.sql"
MARKER = "OFFSITE"
_MONTH_RE = re.compile(r"\d{4}-\d{2}")
_NEVER_STOP = 10 ** 9
_GIT_TIMEOUT = 600
SECRETS_NOTE = ("Secrets are NOT backed up: .secrets/ never leaves this server "
                "and its key has no other copy. After losing the server, the "
                "store's secrets must be entered again.")


class BackupError(RuntimeError):
    pass


# ---- git helpers ----

def _env() -> dict:
    env = {**os.environ, **gitlog._ENV}
    env.setdefault("GIT_SSH_COMMAND",
                   "ssh -o BatchMode=yes -o ConnectTimeout=20")
    return env


def _git(cwd: Path, *args, stdin: str | None = None
         ) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, input=stdin, env=_env(),
                              timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BackupError(f"git {args[0]} timed out") from None


def _git_ok(cwd: Path, *args, stdin: str | None = None) -> str:
    r = _git(cwd, *args, stdin=stdin)
    if r.returncode:
        raise BackupError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def _remotes(cfg: Config) -> list:
    return _git_ok(cfg.home, "remote").split()


def _tmpdir(cfg: Config) -> str:
    """Scratch space inside .git, so a crash leaves nothing Git would commit."""
    d = cfg.home / ".git" / "cb-tmp"
    d.mkdir(exist_ok=True)
    return tempfile.mkdtemp(dir=d)


def _alert_admin(cfg: Config, conn, key: str, subject: str, body: str,
                 now: str | None = None) -> None:
    """One message to Admin per key per day. Never raises."""
    now = now or utcnow()
    own = conn is None
    try:
        if own:
            conn = connect(cfg)
        messages.send(conn, "system", "admin", subject, body, kind="alert",
                      dedupe_key=f"backup:{key}:{now[:10]}",
                      hop_limit=_NEVER_STOP, now=now)
    except Exception as e:  # noqa: BLE001 - an alert must not stop the backup
        print(f"backup: could not alert Admin ({subject}): {e}", file=sys.stderr)
    finally:
        if own and conn is not None:
            conn.close()


# ---- snapshot and push ----

def export_sql(cfg: Config, conn: sqlite3.Connection) -> Path:
    """Write backup/company.sql from a consistent copy of the live database."""
    out = cfg.home / SQL_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(_tmpdir(cfg))
    try:
        copy = sqlite3.connect(tmp / "copy.db")
        try:
            conn.backup(copy)
            part = out.with_name(out.name + ".part")
            with open(part, "w", encoding="utf-8") as f:
                for line in copy.iterdump():
                    f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        finally:
            copy.close()
        os.replace(part, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def snapshot(cfg: Config, conn: sqlite3.Connection) -> str | None:
    """Export the database as text and commit the home. Returns the commit ID,
    or None when nothing changed since the last snapshot."""
    gitlog.ensure_repo(cfg)
    export_sql(cfg, conn)
    return gitlog.commit_sweep(cfg, "Hourly snapshot")


def push(cfg: Config, conn: sqlite3.Connection | None = None) -> str:
    """Push main to the `backup` remote. Returns "pushed", "no-remote" or
    "failed". Never forced; a failure tells Admin once a day."""
    if BACKUP_REMOTE not in _remotes(cfg):
        print(f"note: no '{BACKUP_REMOTE}' remote yet, so nothing was pushed. "
              f"Add one with: git -C {cfg.home} remote add {BACKUP_REMOTE} "
              "<repo-url>")
        return "no-remote"
    try:
        _git_ok(cfg.home, "push", "-q", BACKUP_REMOTE, "main")
    except BackupError as e:
        _alert_admin(cfg, conn, "push-failed", "Backup push failed",
                     f"The hourly backup could not be pushed to the "
                     f"'{BACKUP_REMOTE}' remote: {e}. Nothing was lost; the "
                     "snapshot is committed on the server and the next hour "
                     "tries again.")
        print(f"error: {e}", file=sys.stderr)
        return "failed"
    return "pushed"


# ---- restore ----

def restore(cfg: Config, source: str, target) -> str:
    """Rebuild a company home at `target` from a backup repo (path or URL) or
    from a company.sql file. `target` must be missing or empty: a live home is
    never overwritten. Returns the integrity check result ("ok")."""
    target = Path(target).expanduser().absolute()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BackupError(f"{target} is not empty; restore only writes into an "
                          "empty or missing folder")
    created = not target.exists()
    src = Path(source).expanduser()
    try:
        if src.is_file():
            target.mkdir(parents=True, exist_ok=True)
            sql = src.read_text(encoding="utf-8")
        else:
            r = _git(Path.cwd(), "clone", "-q", source, str(target))
            if r.returncode:
                raise BackupError(f"git clone failed: {r.stderr.strip()[:300]}")
            sql_file = target / SQL_REL
            if not sql_file.is_file():
                raise BackupError(f"{SQL_REL} is not in that backup")
            sql = sql_file.read_text(encoding="utf-8")
            _git_ok(target, "remote", "rename", "origin", BACKUP_REMOTE)
            gitlog.ensure_repo(replace(cfg, home=target))
        db = sqlite3.connect(target / "company.db")
        try:
            db.executescript(sql)
            result = [r[0] for r in db.execute("PRAGMA integrity_check")]
        except sqlite3.Error as e:
            raise BackupError(f"the database could not be rebuilt: {e}") from e
        finally:
            db.close()
        if result != ["ok"]:
            raise BackupError("integrity check failed: " + "; ".join(result[:3]))
        for d in ("work", "knowledge", "bots"):
            (target / d).mkdir(exist_ok=True)
        (target / ".secrets").mkdir(mode=0o700, exist_ok=True)
        (target / ".secrets").chmod(0o700)
        return "ok"
    except BaseException:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        elif target.is_dir():  # it was empty, so everything in it is ours
            for child in target.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
        raise


# ---- off-server archive ----

def _months(cfg: Config) -> list:
    if not cfg.archive_dir.is_dir():
        return []
    return sorted(p.name for p in cfg.archive_dir.iterdir()
                  if p.is_dir() and not p.is_symlink()
                  and _MONTH_RE.fullmatch(p.name))


def _month_files(month_dir: Path) -> list:
    out = []
    for p in sorted(month_dir.rglob("*")):
        if p.is_symlink():
            raise BackupError(f"{p} is a symlink; not moving this month")
        if p.is_file() and p.relative_to(month_dir).as_posix() != MARKER:
            out.append(p.relative_to(month_dir).as_posix())
    return out


def _read_marker(month_dir: Path) -> dict:
    try:
        return json.loads((month_dir / MARKER).read_text())
    except (OSError, ValueError):
        return {}


def _hash_local(base: Path, paths: list) -> list:
    """Git blob IDs of base/<path>... (absolute paths: when base sits inside a
    repo, git would read relative ones from that repo's top)."""
    out = _git_ok(base, "hash-object", "--no-filters", "--stdin-paths",
                  stdin="".join(f"{base / p}\n" for p in paths)).split()
    if len(out) != len(paths):
        raise BackupError("could not hash the local files")
    return out


def _clone_remote(cfg: Config, dest: Path, branch: str | None = None) -> None:
    url = _git_ok(cfg.home, "remote", "get-url", ARCHIVE_REMOTE).strip()
    args = ["clone", "-q", "--depth", "1"]
    if branch:
        args += ["-b", branch]
    _git_ok(dest.parent, *args, url, str(dest))


def _push_month(cfg: Config, month: str) -> dict:
    """Push archive/<month> to the archive remote and verify it there.
    Returns the marker to leave behind. Raises BackupError if not verified."""
    month_dir = cfg.archive_dir / month
    files = _month_files(month_dir)
    tmp = Path(_tmpdir(cfg))
    try:
        repo = tmp / "repo"
        _clone_remote(cfg, repo)
        if _git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode:
            branch = "main"  # empty remote
            _git_ok(repo, "checkout", "-q", "-B", branch)
        else:
            branch = _git_ok(repo, "symbolic-ref", "--short", "HEAD").strip()
        for rel in files:
            dest = repo / month / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(month_dir / rel, dest)
        _git_ok(repo, "add", "-A", "--", month)
        if _git(repo, "diff", "--cached", "--quiet").returncode:
            _git_ok(repo, "-c", "user.name=claudebot", "-c",
                    "user.email=claudebot@claudebot.local", "-c",
                    "commit.gpgsign=false", "commit", "-q", "-m",
                    f"Archive {month}")
        _git_ok(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        # Verify against what the remote really holds now.
        _git_ok(repo, "fetch", "-q", "origin", branch)
        commit = _git_ok(repo, "rev-parse", "FETCH_HEAD").strip()
        remote = {}
        for line in _git_ok(repo, "ls-tree", "-r", "FETCH_HEAD", "--",
                            month).splitlines():
            meta, _, path = line.partition("\t")
            remote[path[len(month) + 1:]] = meta.split()[2]
        local = dict(zip(files, _hash_local(month_dir, files)))
        bad = [f for f in files if remote.get(f) != local[f]]
        if bad:
            raise BackupError(f"{month}: remote copy does not match for "
                              f"{len(bad)} file(s), e.g. {bad[0]}")
        old = _read_marker(month_dir).get("files", {})
        return {"remote": ARCHIVE_REMOTE, "branch": branch, "commit": commit,
                "moved_at": utcnow(), "files": {**old, **local}}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _remove_local(month_dir: Path, marker: dict) -> None:
    part = month_dir / (MARKER + ".part")
    part.write_text(json.dumps(marker, indent=1, sort_keys=True) + "\n")
    os.replace(part, month_dir / MARKER)
    for child in list(month_dir.iterdir()):
        if child.name == MARKER:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def offsite_archive(cfg: Config, conn: sqlite3.Connection | None = None,
                    disk_usage_fn=shutil.disk_usage,
                    now: str | None = None) -> list:
    """While the disk is over the limit, move the oldest archive months to the
    `archive` remote. The current and previous month always stay. Returns the
    months moved. Local files are removed only after a verified push."""
    now = now or utcnow()
    if not archive.offsite_needed(cfg, disk_usage_fn):
        return []
    if ARCHIVE_REMOTE not in _remotes(cfg):
        _alert_admin(cfg, conn, "no-archive-remote", "Disk is filling up",
                     f"The disk is over {cfg.archive_offserver_pct}% full but "
                     f"there is no '{ARCHIVE_REMOTE}' remote to move old "
                     "archive months to. The Owner needs to add one.", now)
        return []
    d = datetime.fromisoformat(now)
    this = f"{d.year}-{d.month:02d}"
    prev = f"{d.year - 1}-12" if d.month == 1 else f"{d.year}-{d.month - 1:02d}"
    moved = []
    for month in _months(cfg):
        if month in (this, prev):
            continue
        month_dir = cfg.archive_dir / month
        try:
            if not _month_files(month_dir):
                continue  # already off-server
            if not archive.offsite_needed(cfg, disk_usage_fn):
                break
            marker = _push_month(cfg, month)
        except BackupError as e:
            _alert_admin(cfg, conn, f"offsite-failed:{month}",
                         "Moving old archive off the server failed",
                         f"Could not move archive/{month} to the "
                         f"'{ARCHIVE_REMOTE}' remote: {e}. Local files were "
                         "kept.", now)
            print(f"error: {e}", file=sys.stderr)
            break  # oldest first: do not skip ahead past a failure
        _remove_local(month_dir, marker)
        moved.append(month)
    return moved


def fetch_offsite(cfg: Config, path: Path) -> Path | None:
    """Pull one archived file back from the `archive` remote into its place.
    `path` is archive/<month>/<file>. None when the OFFSITE marker does not
    list it. The copy is checked against the hash recorded when it left."""
    rel = path.relative_to(cfg.archive_dir)
    if len(rel.parts) < 2:
        return None
    month, inner = rel.parts[0], Path(*rel.parts[1:]).as_posix()
    marker = _read_marker(cfg.archive_dir / month)
    want = marker.get("files", {}).get(inner)
    if want is None:
        return None
    tmp = Path(_tmpdir(cfg))
    try:
        repo = tmp / "repo"
        _clone_remote(cfg, repo, marker.get("branch"))
        got = repo / month / inner
        if not got.is_file() or _hash_local(repo, [f"{month}/{inner}"]) != [want]:
            raise BackupError(f"{rel.as_posix()} on the remote does not match "
                              "what was moved")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(got, path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return path


# ---- CLI ----

def _cmd_now(args) -> int:
    cfg = Config.load()
    conn = connect(cfg)
    bad = False
    try:
        sha = snapshot(cfg, conn)
        print(f"snapshot: {'committed ' + sha[:8] if sha else 'no changes'}")
        result = push(cfg, conn)
        print(f"push: {result}")
        bad = result == "failed"
        try:
            moved = offsite_archive(cfg, conn)
        except BackupError as e:
            print(f"error: {e}", file=sys.stderr)
            moved, bad = [], True
        if moved:
            print(f"moved off-server: {', '.join(moved)}")
    except (BackupError, RuntimeError, OSError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        bad = True
    finally:
        conn.close()
    print(SECRETS_NOTE)
    return 1 if bad else 0


def _cmd_restore(args) -> int:
    try:
        cfg = Config.load()
    except RuntimeError:
        cfg = Config(Path(args.target))
    try:
        print(f"integrity_check: {restore(cfg, args.source, args.target)}")
    except (BackupError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Restored to {args.target}. Next: CLAUDEBOT_HOME={args.target} cb "
          "archive reindex (rebuild search), then point the dispatcher at it.")
    print(SECRETS_NOTE)
    return 0


def register(subparsers) -> None:
    b = subparsers.add_parser("backup", help="snapshots, push and restore")
    sub = b.add_subparsers(dest="backup_command", required=True)
    sub.add_parser("now", help="snapshot, push, and move old archive "
                   "off-server if the disk needs it").set_defaults(func=_cmd_now)
    r = sub.add_parser("restore", help="rebuild a company home from a backup "
                       "into an empty or missing folder")
    r.add_argument("source", help="backup repo (path or URL) or a company.sql")
    r.add_argument("target")
    r.set_defaults(func=_cmd_restore)
