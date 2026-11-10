"""Append-only activity record. UPDATE and DELETE are blocked by triggers."""
import os
import re
import sqlite3
import sys
from dataclasses import dataclass

from .config import Config
from .db import connect, utcnow

_OWNER_LANES = ("chat", "live")
_ANCESTOR_LIMIT = 20


@dataclass(frozen=True)
class Caller:
    """Who is invoking `cb`, bound to an open run when the session id names one."""
    actor: str          # bot name, or "cli"
    session: str | None  # CB_SESSION_ID when it names an open run
    lane: str | None     # that run's lane: normal | heavy | chat | live
    owner_session: bool  # lane in ("chat", "live") and actor == "admin"


def record(conn: sqlite3.Connection, actor: str, kind: str, detail: str,
           ref: str | None = None) -> None:
    conn.execute(
        "INSERT INTO activity(at, actor, kind, detail, ref) VALUES (?,?,?,?,?)",
        (utcnow(), actor, kind, detail, ref),
    )


def _present(env, key: str) -> str | None:
    value = env.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _resolve(conn, env) -> Caller:
    """Steps 2–4: an open run wins, else CB_BOT, else a person at the shell."""
    bot = _present(env, "CB_BOT")
    session = _present(env, "CB_SESSION_ID")
    if session:
        row = conn.execute(
            "SELECT bot, lane FROM runs WHERE session_id=? AND ended_at IS NULL",
            (session,)).fetchone()
        if row is not None:
            actor = row["bot"]
            lane = row["lane"]
            return Caller(actor, session, lane,
                          lane in _OWNER_LANES and actor == "admin")
    if bot:
        return Caller(bot, None, None, False)
    return Caller("cli", None, None, False)


def _proc_environ(pid: int):
    """Environment of `pid` from /proc, or None on any read error."""
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read()
    except OSError:
        return None
    out = {}
    for part in raw.split(b"\0"):
        if b"=" not in part:
            continue
        key, _, val = part.partition(b"=")
        try:
            out[key.decode()] = val.decode()
        except UnicodeDecodeError:
            continue
    return out


def _proc_parent(pid: int):
    """Parent pid from /proc, or None on any read error."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None


def _ancestor_env(environ_of, parent_of, pid: int):
    """First ancestor whose environment holds CB_BOT or CB_SESSION_ID.

    Stops at pid 1, on a read error, or after 20 steps. Returns None when
    no such ancestor is found.
    """
    cur = pid
    seen = set()
    for _ in range(_ANCESTOR_LIMIT):
        if cur in seen:
            return None
        seen.add(cur)
        try:
            parent = parent_of(cur)
        except Exception:
            return None
        if not parent or parent <= 1:
            return None
        try:
            env = environ_of(parent)
        except Exception:
            return None
        if env is None:
            return None
        if _present(env, "CB_BOT") or _present(env, "CB_SESSION_ID"):
            return env
        cur = parent
    return None


def caller(conn, *, environ=None, environ_of=None, parent_of=None,
           pid=None) -> Caller:
    """Who is running this command.

    An open run named by CB_SESSION_ID wins over CB_BOT. When the process
    looks like a person at the shell, ancestors are checked (Linux /proc)
    so `env -u CB_BOT` cannot drop the binding. `environ_of(pid)` and
    `parent_of(pid)` are injectable so a test can supply an ancestor
    without /proc. On a system with no /proc the walk is skipped unless
    those functions are passed in.
    """
    env = os.environ if environ is None else environ
    found = _resolve(conn, env)
    if found.actor != "cli":
        return found
    if environ_of is None and not os.path.isdir("/proc"):
        return found
    inherited = _ancestor_env(
        environ_of or _proc_environ,
        parent_of or _proc_parent,
        os.getpid() if pid is None else pid)
    if not inherited:
        return found
    return _resolve(conn, inherited)


def allows_owner(who: Caller) -> bool:
    """Owner session: Admin in chat or live, or a person at the shell."""
    return who.owner_session or who.actor == "cli"


def allows_admin(who: Caller) -> bool:
    """Admin in any lane, or a person at the shell."""
    return who.actor in ("admin", "cli")


def refuse(conn, who: Caller, command: str, reason: str) -> None:
    """Record the refusal (command name only, never its arguments) and raise."""
    record(conn, who.actor, "auth.refused", command)
    raise PermissionError(reason)


def actor() -> str:
    """Label only, never for permission checks.

    The bot named by CB_BOT, or `cli` when that variable is unset. Permission
    checks use `caller`, which binds the name to an open run.
    """
    return os.environ.get("CB_BOT", "cli")


def since(conn: sqlite3.Connection, iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM activity WHERE at >= ? ORDER BY id", (iso,)
    ).fetchall()


RISKY_WORDS = re.compile(
    r"\b(systemctl\s+(?:stop|restart|disable|mask)|rm|mv|apt|pip|npm|install)\b",
    re.IGNORECASE,
)


def _is_change_approved(conn: sqlite3.Connection, change_id: str | None) -> bool:
    if not change_id:
        return False
    from . import changes
    try:
        cid = changes.normalize_id(change_id)
        row = conn.execute("SELECT status FROM changes WHERE id=?", (cid,)).fetchone()
        return row is not None and row["status"] == "approved"
    except (ValueError, sqlite3.OperationalError):
        return False


def _cmd_sudo(args) -> int:
    cfg = Config.load()
    conn = connect(cfg)
    try:
        change_ref = None
        if args.change:
            from . import changes
            try:
                change_ref = changes.normalize_id(args.change)
            except ValueError:
                change_ref = args.change

        if RISKY_WORDS.search(args.text) and not _is_change_approved(conn, args.change):
            record(conn, actor(), "change.unplanned", args.text, ref=change_ref)
            print("warning: risky sudo command recorded without an approved change plan",
                  file=sys.stderr)
        record(conn, actor(), "sudo", args.text, ref=change_ref)
        return 0
    finally:
        conn.close()


def register(subparsers) -> None:
    log = subparsers.add_parser("log", help="record notable actions")
    lsub = log.add_subparsers(dest="log_cmd", required=True)
    sudo = lsub.add_parser("sudo", help='record a sudo command ("what and why")')
    sudo.add_argument("--change", help="approved change id (e.g. C-0001)")
    sudo.add_argument("text")
    sudo.set_defaults(func=_cmd_sudo)
