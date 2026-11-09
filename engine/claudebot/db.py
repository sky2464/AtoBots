"""SQLite access: connection, schema, transactions, time helpers."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots(
  name TEXT PRIMARY KEY, role TEXT, org TEXT, reports_to TEXT,
  model TEXT, effort TEXT, skills TEXT, mcp TEXT, plugins TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
  soul_sha TEXT, created_at TEXT, connectors INT DEFAULT 0
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
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, session_id TEXT, received_at TEXT
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
CREATE TRIGGER IF NOT EXISTS activity_no_update BEFORE UPDATE ON activity
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS activity_no_delete BEFORE DELETE ON activity
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

_CT = ZoneInfo("America/Chicago")


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
