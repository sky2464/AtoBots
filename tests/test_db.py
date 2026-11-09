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
