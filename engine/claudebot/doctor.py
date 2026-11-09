"""Health checks: plain scripts, no AI. The backend role is woken only when a
check finds something wrong, and each finding is reported at most once a day.

The checks never depend on the dispatcher: this runs from its own timer, so it
still works (and says so) when the dispatcher is down.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import activity, backup, bots, dispatcher, messages
from .config import Config
from .db import connect, utcnow

DISPATCHER_UNIT = "claudebot-dispatcher.service"
BACKEND = "backend"
ADMIN = "admin"
_NEVER_STOP = 10 ** 9
STALE_HOURS = 24
GIT_MAX_BYTES = 5 * 1024 ** 3
SQL_MAX_BYTES = 200 * 1024 ** 2
MEM_READINGS = 3        # low readings in a row before it is a finding
REPEAT_DAYS = 7         # an unchanged finding is repeated after this long
WAKES_PER_DAY = 3       # most messages the doctor sends backend in 24 hours


@dataclass(frozen=True)
class Finding:
    key: str    # stable per problem, used to report it once a day
    text: str   # what the reader sees


def dispatcher_active(unit: str = DISPATCHER_UNIT) -> bool | None:
    """True/False from systemd; None when systemd is not there to ask."""
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


def _memory_mb() -> int | None:
    return dispatcher.available_mb() if Path("/proc/meminfo").exists() else None


def checks(cfg: Config, conn: sqlite3.Connection | None = None,
           active_fn=dispatcher_active, mem_fn=_memory_mb,
           disk_usage_fn=shutil.disk_usage, now: str | None = None,
           track: bool = True) -> list[Finding]:
    """Everything wrong right now. Opens its own connection if none is given,
    so a database that cannot be opened is a finding, not a crash."""
    now = now or utcnow()
    out: list[Finding] = []
    if active_fn() is False:
        out.append(Finding("dispatcher", "The dispatcher is not running "
                           f"({DISPATCHER_UNIT}). No bot can be woken until "
                           "it is started."))
    try:
        total, used = disk_usage_fn(cfg.home if cfg.home.exists() else "/")[:2]
        pct = used * 100 / total if total else 0
        if pct >= cfg.archive_offserver_pct:
            out.append(Finding("disk", f"The disk is over {int(pct) // 5 * 5}% full "
                               f"(limit {cfg.archive_offserver_pct}%)."))
    except OSError as e:
        out.append(Finding("disk", f"Could not read disk usage: {e}"))
    out.extend(_backup_size_checks(cfg))
    mb = mem_fn()
    own = conn is None
    try:
        if own:
            conn = connect(cfg)
        out.extend(_db_checks(cfg, conn, now))
        out.extend(_backup_checks(cfg, conn, now))
        if mb is not None:
            out.extend(_memory_check(cfg, conn, mb, track))
    except sqlite3.Error as e:
        out.append(Finding("database", f"The database could not be opened or "
                           f"read: {e}"))
    finally:
        if own and conn is not None:
            conn.close()
    return out


def _memory_check(cfg, conn, mb: int, track: bool) -> list[Finding]:
    """Low memory is a finding only after MEM_READINGS low readings in a row."""
    try:
        n = int(backup.meta_get(conn, "doctor.mem_low") or 0)
    except ValueError:
        n = 0
    n = n + 1 if mb < cfg.memory_floor_mb else 0
    if track:
        backup.meta_set(conn, "doctor.mem_low", str(n))
    if n >= MEM_READINGS:
        return [Finding("memory", "Available memory has been below the "
                        f"{cfg.memory_floor_mb} MB floor for "
                        f"{MEM_READINGS} checks in a row.")]
    return []


def _age_hours(iso: str, now: str) -> float:
    return (datetime.fromisoformat(now) - datetime.fromisoformat(iso)
            ).total_seconds() / 3600


def _backup_checks(cfg, conn, now: str) -> list[Finding]:
    """The silent failure: backups that are not happening."""
    if not (cfg.home / ".git").exists():
        return []
    out = []
    have = backup.remotes(cfg)
    first = conn.execute("SELECT MIN(at) FROM activity").fetchone()[0]
    young = first is None or _age_hours(first, now) < STALE_HOURS
    if backup.BACKUP_REMOTE not in have:
        out.append(Finding("backup-remote", "There is no 'backup' remote, so "
                           "nothing is backed up off this server. The Owner "
                           "needs to add a repository."))
    else:
        last = backup.meta_get(conn, "backup.last_push_ok")
        if (last is None and not young) or (
                last and _age_hours(last, now) > STALE_HOURS):
            out.append(Finding("backup-push", "No backup push has succeeded "
                               f"in over {STALE_HOURS} hours."))
    if backup.ARCHIVE_REMOTE not in have:
        out.append(Finding("archive-remote", "There is no 'archive' remote, so "
                           "old archive months are not copied off this server."))
    last = backup.meta_get(conn, "backup.last_snapshot_ok")
    if (last is None and not young) or (
            last and _age_hours(last, now) > STALE_HOURS):
        out.append(Finding("backup-snapshot", "No backup snapshot has "
                           f"succeeded in over {STALE_HOURS} hours."))
    return out


def _tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _backup_size_checks(cfg: Config) -> list[Finding]:
    out = []
    if (cfg.home / ".git").is_dir() and _tree_bytes(cfg.home / ".git") > GIT_MAX_BYTES:
        out.append(Finding("git-size", "The Git history (.git) is over 5 GB."))
    sql = cfg.home / backup.SQL_REL
    if sql.is_file() and sql.stat().st_size > SQL_MAX_BYTES:
        out.append(Finding("sql-size", "backup/company.sql is over 200 MB."))
    return out


def _db_checks(cfg: Config, conn: sqlite3.Connection, now: str
               ) -> list[Finding]:
    out = []
    result = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        out.append(Finding("database", "The database integrity check failed: "
                           + "; ".join(result[:3])))
        return out  # the other queries are not trustworthy on a damaged file
    limit = (datetime.fromisoformat(now)
             - timedelta(seconds=cfg.lease_seconds)).isoformat(timespec="seconds")
    for r in conn.execute("SELECT id, bot, started_at FROM runs WHERE"
                          " ended_at IS NULL AND started_at < ? ORDER BY id",
                          (limit,)):
        out.append(Finding(f"run:{r['id']}",
                           f"Run {r['id']} of {r['bot']} has been going since "
                           f"{r['started_at']} UTC, longer than the "
                           f"{cfg.lease_seconds} s lease."))
    for msg in bots.check(cfg, conn):
        out.append(Finding("bots:" + msg, f"Bot files: {msg}"))
    return out


def _due(conn, key: str, text: str, now: str) -> bool:
    """First time, changed text, or REPEAT_DAYS since the last notice."""
    raw = backup.meta_get(conn, f"doctor.seen:{key}")
    if raw:
        try:
            seen = json.loads(raw)
            if seen["text"] == text and _age_hours(seen["at"], now) < \
                    REPEAT_DAYS * 24:
                return False
        except (ValueError, KeyError, TypeError):
            pass
    return True


def _mark(conn, key: str, text: str, now: str) -> None:
    backup.meta_set(conn, f"doctor.seen:{key}",
                    json.dumps({"text": text, "at": now}))


def _wakes_today(conn, now: str) -> int:
    since = (datetime.fromisoformat(now) - timedelta(hours=24)
             ).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE to_bot=? AND from_bot='system'"
        " AND dedupe_key LIKE 'doctor:%' AND created_at>=?",
        (BACKEND, since)).fetchone()[0]


def run(cfg: Config, conn: sqlite3.Connection, **kw) -> list[Finding]:
    """Run the checks and tell `backend` about each finding on the first
    sighting, when its text changes, or after 7 days; never more than 3 times
    in 24 hours overall. A down dispatcher is also told to Admin, with the
    same backoff. Returns all current findings."""
    now = kw.get("now") or utcnow()
    found = checks(cfg, conn, **kw)
    for f in found:
        try:
            tag = hashlib.sha256(f"{f.key}{f.text}{now}".encode()).hexdigest()[:12]
            if _due(conn, f.key, f.text, now) and \
                    _wakes_today(conn, now) < WAKES_PER_DAY:
                messages.send(conn, "system", BACKEND, f"Health check: {f.key}",
                              f.text + " Look into it; fix only what is safe "
                              "and tell Admin the rest.", kind="alert",
                              level="normal",
                              dedupe_key=f"doctor:{f.key}:{tag}",
                              hop_limit=_NEVER_STOP, now=now)
                _mark(conn, f.key, f.text, now)
                activity.record(conn, "system", "doctor.finding", f.text,
                                f.key)
            if f.key == "dispatcher" and _due(conn, "dispatcher:admin",
                                              f.text, now):
                messages.send(conn, "system", ADMIN,
                              "The dispatcher is down", f.text + " The server "
                              "may need a restart of " + DISPATCHER_UNIT + ".",
                              kind="alert", level="normal",
                              dedupe_key=f"doctor:{f.key}:admin:{tag}",
                              hop_limit=_NEVER_STOP, now=now)
                _mark(conn, "dispatcher:admin", f.text, now)
        except (sqlite3.Error, ValueError, KeyError) as e:
            # e.g. the database is damaged: the printed finding still shows.
            print(f"doctor: could not report {f.key}: {e}", file=sys.stderr)
    return found


# ---- CLI ----

def _cmd_doctor(args) -> int:
    cfg = Config.load()
    if args.notify:
        conn = None
        try:
            conn = connect(cfg)
            found = run(cfg, conn)
        except sqlite3.Error:
            found = checks(cfg)  # database unusable: report what we can
        finally:
            if conn is not None:
                conn.close()
    else:
        found = checks(cfg, track=False)
    for f in found:
        print(f"- {f.text}")
    if not found:
        print("All good.")
    return 1 if found else 0


def register(subparsers) -> None:
    d = subparsers.add_parser("doctor", help="health checks (exit 1 if any "
                              "problem is found)")
    d.add_argument("--notify", action="store_true",
                   help="also message backend once per finding per day "
                        "(what the timer does)")
    d.set_defaults(func=_cmd_doctor)
