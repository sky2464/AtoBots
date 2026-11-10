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
from .db import connect, tx, utcnow

DISPATCHER_UNIT = "claudebot-dispatcher.service"
ADMIN_UNIT = "claudebot-admin.service"
BACKEND = "backend"
ADMIN = "admin"
_NEVER_STOP = 10 ** 9
STALE_HOURS = 24
GIT_MAX_BYTES = 5 * 1024 ** 3
SQL_MAX_BYTES = 200 * 1024 ** 2
MEM_READINGS = 3        # low readings in a row before it is a finding
REPEAT_DAYS = 7         # an unchanged finding is repeated after this long
WAKES_PER_DAY = 3       # most messages the doctor sends backend in 24 hours
WAITING_HOURS = 48      # an item waiting on a bot this long is a finding
# Findings that are the Owner's to act on: shown by `cb doctor` and in the chat
# brief, never sent to a bot (a bot cannot add a remote).
NO_WAKE = ("backup-remote", "archive-remote")


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


def admin_live_enabled(unit: str = ADMIN_UNIT) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-enabled", "--quiet", unit],
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _admin_live_session() -> bool:
    from . import admin_live  # late: it imports chat
    return admin_live.has_session()


def _memory_mb() -> int | None:
    return dispatcher.available_mb() if Path("/proc/meminfo").exists() else None


def checks(cfg: Config, conn: sqlite3.Connection | None = None,
           active_fn=dispatcher_active, mem_fn=_memory_mb,
           disk_usage_fn=shutil.disk_usage, now: str | None = None,
           track: bool = True, live_enabled_fn=admin_live_enabled,
           live_session_fn=_admin_live_session) -> list[Finding]:
    """Everything wrong right now. Opens its own connection if none is given,
    so a database that cannot be opened is a finding, not a crash."""
    now = now or utcnow()
    out: list[Finding] = []
    if active_fn() is False:
        out.append(Finding("dispatcher", "The dispatcher is not running "
                           f"({DISPATCHER_UNIT}). No bot can be woken until "
                           "it is started."))
    if live_enabled_fn() and not live_session_fn():
        out.append(Finding("admin-live", "The always-on Admin is enabled "
                           f"({ADMIN_UNIT}) but tmux session admin-live is "
                           "missing, so Telegram messages get no answer."))
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
    except sqlite3.Error as e:
        out.append(Finding("database", f"The database could not be opened or "
                           f"read: {e}"))
    finally:
        if own and conn is not None:
            conn.close()
    if mb is not None:
        out.extend(_memory_check(cfg, conn, mb, track, own))
    return out


def _memory_check(cfg, conn, mb: int, track: bool, own: bool
                  ) -> list[Finding]:
    """Low memory is a finding only after MEM_READINGS low readings in a row.
    The count lives in the database; if that is unusable, one low reading
    counts at once, so a broken database cannot hide the problem."""
    low = mb < cfg.memory_floor_mb
    try:
        if own:
            conn = connect(cfg)
        try:
            n = int(backup.meta_get(conn, "doctor.mem_low") or 0)
            n = n + 1 if low else 0
            if track:
                backup.meta_set(conn, "doctor.mem_low", str(n))
        finally:
            if own:
                conn.close()
    except (sqlite3.Error, ValueError):
        n = MEM_READINGS if low else 0
    if n >= MEM_READINGS:
        return [Finding("memory", "Available memory has been below the "
                        f"{cfg.memory_floor_mb} MB floor for "
                        f"{MEM_READINGS} checks in a row.")]
    return []


def _age_hours(iso: str, now: str) -> float:
    return (datetime.fromisoformat(now) - datetime.fromisoformat(iso)
            ).total_seconds() / 3600


def setup_findings(cfg: Config) -> list[Finding]:
    """Missing off-server remotes. Only the Owner can fix these, so they are
    never sent to a bot."""
    if not (cfg.home / ".git").exists():
        return []
    have = backup.remotes(cfg)
    out = []
    if backup.BACKUP_REMOTE not in have:
        out.append(Finding("backup-remote", "There is no 'backup' remote, so "
                           "nothing is backed up off this server. The Owner "
                           "needs to add a repository."))
    if backup.ARCHIVE_REMOTE not in have:
        out.append(Finding("archive-remote", "There is no 'archive' remote, so "
                           "old archive months are not copied off this server."))
    return out


def _backup_checks(cfg, conn, now: str) -> list[Finding]:
    """The silent failure: backups that are not happening."""
    if not (cfg.home / ".git").exists():
        return []
    out = []
    have = backup.remotes(cfg)
    first = conn.execute("SELECT MIN(at) FROM activity").fetchone()[0]
    young = first is None or _age_hours(first, now) < STALE_HOURS
    out.extend(setup_findings(cfg))
    if backup.BACKUP_REMOTE in have:
        last = backup.meta_get(conn, "backup.last_push_ok")
        if (last is None and not young) or (
                last and _age_hours(last, now) > STALE_HOURS):
            out.append(Finding("backup-push", "No backup push has succeeded "
                               f"in over {STALE_HOURS} hours."))
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
                          " ended_at IS NULL AND started_at < ?"
                          " AND COALESCE(lane,'') NOT IN ('chat','live') ORDER BY id",
                          (limit,)):
        out.append(Finding(f"run:{r['id']}",
                           f"Run {r['id']} of {r['bot']} has been going since "
                           f"{r['started_at']} UTC, longer than the "
                           f"{cfg.lease_seconds} s lease."))
    out.extend(_waiting_checks(conn, now))
    for msg in bots.check(cfg, conn):
        out.append(Finding("bots:" + msg, f"Bot files: {msg}"))
    return out


def _waiting_checks(conn, now: str) -> list[Finding]:
    """Open items that have waited on someone for more than WAITING_HOURS, so
    a question nobody answered does not sit forever."""
    out = []
    for r in conn.execute(
            "SELECT id, waiting_on, updated_at, (SELECT MAX(a.at) FROM"
            " activity a WHERE a.kind='item.waiting' AND a.ref=items.id) AS"
            " since FROM items WHERE COALESCE(waiting_on,'')!='' AND"
            " archived_at IS NULL AND status NOT IN ('done','parked')"
            " ORDER BY n"):
        since = r["since"] or r["updated_at"]
        if since and _age_hours(since, now) > WAITING_HOURS:
            out.append(Finding(
                f"waiting:{r['id']}",
                f"{r['id']} has been waiting on {r['waiting_on']} for over "
                f"{WAITING_HOURS} hours (since {since} UTC) with no answer."))
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


def close_cleared(conn: sqlite3.Connection, current: set, now: str) -> list:
    """Close open items opened for a finding (`cb item new --finding KEY`)
    when the doctor no longer sees that finding. Returns the closed ids."""
    closed = []
    rows = conn.execute(
        "SELECT i.id, a.detail FROM items i JOIN activity a ON a.ref=i.id"
        " AND a.kind='item.finding' WHERE i.status NOT IN ('done','parked')"
        " AND i.archived_at IS NULL ORDER BY a.id").fetchall()
    keys = {}
    for r in rows:  # the latest tag on an item wins
        keys[r["id"]] = r["detail"][len("finding:"):]
    for id, key in keys.items():
        if key in current:
            continue
        with tx(conn):
            conn.execute("UPDATE items SET status='done', updated_at=?"
                         " WHERE id=?", (now, id))
            activity.record(conn, "system", "item.status",
                            f"{id}: closed, the health check no longer "
                            f"reports finding {key}", id)
        closed.append(id)
    return closed


def run(cfg: Config, conn: sqlite3.Connection, **kw) -> list[Finding]:
    """Run the checks and tell `backend` about each finding on the first
    sighting, when its text changes, or after 7 days; never more than 3 times
    in 24 hours overall. A down dispatcher is also told to Admin, with the
    same backoff. Returns all current findings."""
    now = kw.get("now") or utcnow()
    found = checks(cfg, conn, **kw)
    try:
        close_cleared(conn, {f.key for f in found}, now)
    except sqlite3.Error as e:
        print(f"doctor: could not close cleared items: {e}", file=sys.stderr)
    for f in found:
        if f.key in NO_WAKE:
            continue
        try:
            if f.key.startswith("waiting:") or f.key == "admin-live":
                # Admin's to chase, not backend's
                if _due(conn, f.key, f.text, now):
                    tag = hashlib.sha256(f"{f.key}{now}".encode()).hexdigest()[:12]
                    subject, hint = (
                        ("The always-on Admin is not running", " Tell the "
                         "Owner; he restarts it with sudo systemctl restart "
                         + ADMIN_UNIT + ".") if f.key == "admin-live" else
                        ("An item is waiting too long", " Nudge whoever it "
                         "waits on or ask the Owner."))
                    messages.send(conn, "system", ADMIN,
                                  subject, f.text + hint,
                                  kind="alert", level="normal",
                                  dedupe_key=f"doctor:{f.key}:{tag}",
                                  hop_limit=_NEVER_STOP, now=now)
                    _mark(conn, f.key, f.text, now)
                    activity.record(conn, "system", "doctor.finding", f.text,
                                    f.key)
                continue
            tag = hashlib.sha256(f"{f.key}{f.text}{now}".encode()).hexdigest()[:12]
            if _due(conn, f.key, f.text, now) and \
                    _wakes_today(conn, now) < WAKES_PER_DAY:
                messages.send(conn, "system", BACKEND, f"Health check: {f.key}",
                              f.text + " Look into it; fix only what is safe "
                              "and tell Admin the rest. If you open an item "
                              f"for it, use cb item new --finding {f.key} so "
                              "it closes when the problem clears.", kind="alert",
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
