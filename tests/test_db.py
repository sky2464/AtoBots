import multiprocessing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import activity  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, ct_display, init_db, tx  # noqa: E402

TABLES = {"bots", "items", "messages", "owner_requests", "request_items",
          "decisions", "runs", "routines", "activity", "meta"}


def _writer(args):
    home, n = args
    cfg = Config(Path(home))
    conn = connect(cfg)
    for i in range(50):
        with tx(conn):
            activity.record(conn, f"w{n}", "test", f"row {i}")
    conn.close()


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(Path(self.tmp.name))
        init_db(self.cfg)

    def tables(self, conn):
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {r["name"] for r in rows}

    def test_init_is_idempotent(self):
        init_db(self.cfg)
        conn = connect(self.cfg)
        self.assertTrue(TABLES <= self.tables(conn))
        conn.close()

    def test_ct_display(self):
        self.assertEqual(ct_display("2026-11-03T13:45:00+00:00"),
                         "Tue Nov 3, 2026, 7:45 AM CT")

    def test_activity_is_append_only(self):
        conn = connect(self.cfg)
        with tx(conn):
            activity.record(conn, "admin", "note", "hello", ref="T-0001")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE activity SET detail='x'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM activity")
        self.assertEqual(len(activity.since(conn, "1970-01-01T00:00:00+00:00")), 1)
        conn.close()

    def test_connection_pragmas(self):
        conn = connect(self.cfg)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        conn.close()

    def test_concurrent_writers(self):
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(8) as pool:
            pool.map(_writer, [(str(self.cfg.home), n) for n in range(8)])
        conn = connect(self.cfg)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0], 400)
        conn.close()


if __name__ == "__main__":
    unittest.main()


class TierMigrationTests(unittest.TestCase):
    OLD_BOTS = """
    CREATE TABLE bots(
      name TEXT PRIMARY KEY, role TEXT, org TEXT, reports_to TEXT,
      model TEXT, effort TEXT, skills TEXT, mcp TEXT, plugins TEXT,
      status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
      soul_sha TEXT, created_at TEXT, connectors INT DEFAULT 0
    );
    """

    def test_migration_adds_tier_and_worker(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(Path(tmp.name))
        raw = sqlite3.connect(cfg.db_path)
        raw.executescript(self.OLD_BOTS)
        raw.execute("INSERT INTO bots(name, model, status) VALUES('runner', 'haiku', 'active')")
        raw.execute("INSERT INTO bots(name, model, status) VALUES('boss', 'opus', 'active')")
        raw.commit()
        raw.close()

        conn = connect(cfg)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bots)")}
        self.assertTrue({"tier", "worker"} <= cols)
        tiers = dict(conn.execute("SELECT name, tier FROM bots").fetchall())
        self.assertEqual(tiers, {"runner": "worker", "boss": "thinker"})
        self.assertTrue(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage'").fetchone())
        conn.close()

        conn = connect(cfg)  # a second connect must be a no-op
        self.assertEqual(dict(conn.execute("SELECT name, tier FROM bots").fetchall()), tiers)
        conn.close()
