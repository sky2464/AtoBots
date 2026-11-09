"""Health checks: plain scripts, no AI. The backend role is woken only when a
check finds something wrong, and each finding is reported at most once a day.

The checks never depend on the dispatcher: this runs from its own timer, so it
still works (and says so) when the dispatcher is down.
"""
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import activity, bots, dispatcher, messages
from .config import Config
from .db import connect, utcnow

DISPATCHER_UNIT = "claudebot-dispatcher.service"
BACKEND = "backend"
ADMIN = "admin"
_NEVER_STOP = 10 ** 9


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
           disk_usage_fn=shutil.disk_usage, now: str | None = None
           ) -> list[Finding]:
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
            out.append(Finding("disk", f"The disk is {pct:.0f}% full "
                               f"(limit {cfg.archive_offserver_pct}%)."))
    except OSError as e:
        out.append(Finding("disk", f"Could not read disk usage: {e}"))
    mb = mem_fn()
    if mb is not None and mb < cfg.memory_floor_mb:
        out.append(Finding("memory", f"Available memory is {mb} MB, below the "
                           f"{cfg.memory_floor_mb} MB floor."))
    own = conn is None
    try:
        if own:
            conn = connect(cfg)
        out.extend(_db_checks(cfg, conn, now))
    except sqlite3.Error as e:
        out.append(Finding("database", f"The database could not be opened or "
                           f"read: {e}"))
    finally:
        if own and conn is not None:
            conn.close()
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


def run(cfg: Config, conn: sqlite3.Connection, **kw) -> list[Finding]:
    """Run the checks and message `backend` once per finding per day; a
    down dispatcher is also told to Admin. Returns all current findings."""
    now = kw.get("now") or utcnow()
    found = checks(cfg, conn, **kw)
    day = now[:10]
    for f in found:
        key = f"doctor:{f.key}:{day}"
        if conn.execute("SELECT 1 FROM messages WHERE dedupe_key=?",
                        (key,)).fetchone():
            continue
        try:
            messages.send(conn, "system", BACKEND, f"Health check: {f.key}",
                          f.text + " Look into it; fix only what is safe and "
                          "tell Admin the rest.", kind="alert",
                          level="normal", dedupe_key=key,
                          hop_limit=_NEVER_STOP, now=now)
            if f.key == "dispatcher":
                messages.send(conn, "system", ADMIN,
                              "The dispatcher is down", f.text + " The server "
                              "may need a restart of " + DISPATCHER_UNIT + ".",
                              kind="alert", level="normal",
                              dedupe_key=f"doctor:{f.key}:admin:{day}",
                              hop_limit=_NEVER_STOP, now=now)
            activity.record(conn, "system", "doctor.finding", f.text, f.key)
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
        found = checks(cfg)
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
