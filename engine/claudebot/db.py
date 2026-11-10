"""SQLite access: connection, schema, transactions, time helpers."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Config

# Token use per run, one row per model call (cost control).
USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY, at TEXT, bot TEXT, model TEXT, lane TEXT,
  trigger TEXT, ref TEXT, input_tokens INT, cache_write INT, cache_read INT,
  output_tokens INT
);
"""

# Questions the Admin put to the Owner that he has not answered yet (cost
# control: a bare acknowledgement is refused when none is open).
OWNER_QUESTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS owner_questions(
  id INTEGER PRIMARY KEY, asked_at TEXT, text TEXT, answered_at TEXT
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots(
  name TEXT PRIMARY KEY, role TEXT, org TEXT, reports_to TEXT,
  model TEXT, effort TEXT, skills TEXT, mcp TEXT, plugins TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
  soul_sha TEXT, created_at TEXT, connectors INT DEFAULT 0,
  tier TEXT NOT NULL DEFAULT 'thinker' CHECK(tier IN ('thinker','worker')),
  worker TEXT
);
CREATE TABLE IF NOT EXISTS items(
  n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, status TEXT, owner TEXT,
  title TEXT, parent TEXT, waiting_on TEXT, priority INT DEFAULT 0,
  owner_waiting INT DEFAULT 0, note TEXT, created_at TEXT, updated_at TEXT,
  archived_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, from_bot TEXT, to_bot TEXT, item_id TEXT, kind TEXT,
  subject TEXT, body TEXT, priority TEXT, level TEXT, effort TEXT, hop INT,
  chain_id TEXT, dedupe_key TEXT UNIQUE, status TEXT, attempts INT DEFAULT 0,
  lease_until TEXT, run_id INT, created_at TEXT, done_at TEXT
);
CREATE TABLE IF NOT EXISTS owner_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, session_id TEXT, received_at TEXT,
  source TEXT
);
CREATE TABLE IF NOT EXISTS request_items(
  request_id INT, item_id TEXT, PRIMARY KEY(request_id, item_id)
);
CREATE TABLE IF NOT EXISTS decisions(
  n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, title TEXT, quote TEXT,
  said_at TEXT, session TEXT, recorded_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, session_id TEXT, lane TEXT, started_at TEXT,
  ended_at TEXT, exit_code INT, reason TEXT, input_tokens INT, output_tokens INT,
  commit_sha TEXT
);
CREATE TABLE IF NOT EXISTS routines(
  id INTEGER PRIMARY KEY, bot TEXT, cron TEXT, check_cmd TEXT, prompt TEXT,
  level TEXT, enabled INT DEFAULT 1, next_due TEXT, created_by TEXT
);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
  kind TEXT NOT NULL, detail TEXT, ref TEXT
);
CREATE INDEX IF NOT EXISTS activity_at ON activity(at);
CREATE INDEX IF NOT EXISTS activity_kind_ref ON activity(kind, ref);
CREATE TRIGGER IF NOT EXISTS activity_no_update BEFORE UPDATE ON activity
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS activity_no_delete BEFORE DELETE ON activity
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
""" + USAGE_TABLE + OWNER_QUESTIONS_TABLE

_CT = ZoneInfo("America/Chicago")


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Columns added after the first release. Cheap, and safe to repeat."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(owner_requests)")]
    if cols and "source" not in cols:
        try:
            conn.execute("ALTER TABLE owner_requests ADD COLUMN source TEXT")
        except sqlite3.OperationalError:  # another process added it first
            pass
    bcols = [r[1] for r in conn.execute("PRAGMA table_info(bots)")]
    if bcols and "tier" not in bcols:
        try:
            conn.execute("ALTER TABLE bots ADD COLUMN tier TEXT NOT NULL "
                         "DEFAULT 'thinker' CHECK(tier IN ('thinker','worker'))")
        except sqlite3.OperationalError:  # another process added it first
            pass
        else:
            conn.execute("UPDATE bots SET tier='worker' WHERE lower(model) "
                        "LIKE '%haiku%'")
    if bcols and "worker" not in bcols:
        try:
            conn.execute("ALTER TABLE bots ADD COLUMN worker TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute(USAGE_TABLE)
    conn.execute(OWNER_QUESTIONS_TABLE)


def init_db(cfg: Config) -> None:
    conn = connect(cfg)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ct_display(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(_CT)
    hour = d.hour % 12 or 12
    return (f"{d:%a %b} {d.day}, {d.year}, {hour}:{d:%M} "
            f"{'AM' if d.hour < 12 else 'PM'} CT")
