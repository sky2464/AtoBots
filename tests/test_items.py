import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import bots, cli, items  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


def seed_owner_session(conn, session="s1", lane="chat"):
    conn.execute(
        "INSERT INTO runs(bot, session_id, lane, started_at) VALUES (?,?,?,?)",
        ("admin", session, lane, "2026-11-09T00:00:00+00:00"))


class ItemsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        for name in ("admin", "backend"):
            bots.create(self.cfg, self.conn, name, role=name, soul="s",
                        persona="p")

    def new(self, title="t", owner="admin", **kw):
        return items.new(self.cfg, self.conn, title, owner, **kw)

    def row(self, id):
        return self.conn.execute("SELECT * FROM items WHERE id=?", (id,)).fetchone()


class ItemTests(ItemsBase):
    def test_ids_never_reused(self):
        a = self.new()
        self.assertEqual(a, "T-0001")
        self.conn.execute("UPDATE items SET archived_at='2026-01-01T00:00:00+00:00'"
                          " WHERE id=?", (a,))
        self.assertEqual(self.new(), "T-0002")

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            self.new(status="bogus")
        a = self.new()
        with self.assertRaises(ValueError):
            items.set_status(self.conn, a, "bogus", "admin")

    def test_item_folder_stays_inside_home(self):
        self.assertEqual(items.folder(self.cfg, "T-0001"),
                         self.cfg.work_dir / "T-0001")
        for bad in ("../x", "T-1", "T-0001/..", "t-0001", "T-0001\n", ""):
            with self.assertRaises(ValueError):
                items.folder(self.cfg, bad)

    def test_pause_resume_keeps_note(self):
        a = self.new(status="doing")
        items.pause(self.cfg, self.conn, a, "stopped at step 3", "admin")
        r = self.row(a)
        self.assertEqual((r["status"], r["note"]), ("paused", "stopped at step 3"))
        self.assertEqual(items.resume(self.conn, a, "admin"), "stopped at step 3")
        self.assertEqual(self.row(a)["status"], "doing")

    def test_assign_moves_owner_and_keeps_note(self):
        a = self.new(status="doing")
        items.pause(self.cfg, self.conn, a, "half done", "admin")
        items.assign(self.conn, a, "backend", "admin")
        r = self.row(a)
        self.assertEqual((r["owner"], r["status"]), ("backend", "todo"))
        self.assertIn("half done", r["note"])
        items.assign(self.conn, a, "admin", "admin", note="check tests")
        self.assertIn("half done", self.row(a)["note"])
        self.assertIn("check tests", self.row(a)["note"])
        with self.assertRaises(KeyError):
            items.assign(self.conn, a, "nobody", "admin")

    def test_next_order(self):
        old = self.new("old")
        prio = self.new("prio", priority=5)
        wait = self.new("wait", owner_waiting=True)
        paused = self.new("paused", status="paused")
        done = self.new("done", status="done")
        parked = self.new("parked", status="parked")
        got = [r["id"] for r in items.next_items(self.conn)]
        self.assertEqual(got, [wait, prio, old, paused])
        self.assertNotIn(done, got)
        self.assertNotIn(parked, got)
        other = self.new("b", owner="backend")
        self.assertEqual([r["id"] for r in items.next_items(self.conn, "backend")],
                         [other])
        self.assertEqual(len(items.next_items(self.conn, limit=2)), 2)

    def test_split_children_track_parent(self):
        p = self.new("big", status="doing")
        kids = items.split(self.cfg, self.conn, p, ["a", "b"], "backend")
        self.assertEqual(len(kids), 2)
        for k in kids:
            r = self.row(k)
            self.assertEqual((r["status"], r["parent"], r["owner"]),
                             ("todo", p, "backend"))
        items.set_status(self.conn, p, "done", "admin")
        ids = lambda: [r["id"] for r in items.open_items(self.conn)]  # noqa: E731
        self.assertIn(p, ids())
        items.set_status(self.conn, kids[0], "done", "admin")
        self.assertIn(p, ids())
        items.set_status(self.conn, kids[1], "parked", "admin")
        self.assertNotIn(p, ids())

    def test_split_children_are_wakeable_todo(self):
        p = self.new("big", status="doing")
        kids = items.split(self.cfg, self.conn, p, ["step"], "backend")
        self.assertEqual(self.row(kids[0])["status"], "todo")
        self.assertEqual(
            [r["id"] for r in items.next_items(self.conn, "backend")], kids)

    def test_decision_requires_exact_quote(self):
        seed_owner_session(self.conn, "s1")
        seed_owner_session(self.conn, "s")
        env = {"CB_SESSION_ID": "s1"}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(ValueError):
                items.decide(self.conn, "pay", "", "2026-11-09T00:00:00+00:00",
                             "s1", "admin")
            with self.assertRaises(ValueError):
                items.decide(self.conn, "pay", "  ", "2026-11-09T00:00:00+00:00",
                             "s1", "admin")
            d = items.decide(self.conn, "pay", "Yes, pay it\nnow",
                             "2026-11-09T00:00:00+00:00", "s1", "admin")
        self.assertEqual(d, "D-0001")
        r = self.conn.execute("SELECT * FROM decisions WHERE id=?", (d,)).fetchone()
        self.assertEqual(r["quote"], "Yes, pay it\nnow")
        self.assertEqual(r["owner_session"], 1)
        with mock.patch.dict(os.environ, {"CB_SESSION_ID": "s"}, clear=False):
            self.assertEqual(items.decide(self.conn, "x", "q", "t", "s", "admin"),
                             "D-0002")

    def test_decision_requires_matching_session(self):
        seed_owner_session(self.conn, "s1")
        with self.assertRaises(PermissionError):
            items.decide(self.conn, "pay", "go", "t", "s1", "admin")
        with mock.patch.dict(os.environ, {"CB_SESSION_ID": "s1"}, clear=False):
            items.decide(self.conn, "pay", "go", "t", "s1", "admin")

    def test_decision_refuses_bot_only_session(self):
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at) VALUES"
            " ('backend','bot-sess','normal','2026-11-09T00:00:00+00:00')")
        with self.assertRaises(PermissionError):
            items.decide(self.conn, "pay", "go", "t", "bot-sess", "admin")
        with self.assertRaises(PermissionError):
            items.decide(self.conn, "pay", "go", "t", "s1", "backend")
        with self.assertRaises(ValueError):
            items.decide(self.conn, "pay", "go", "t", "", "admin")


class ItemCliTests(ItemsBase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def test_cli_flow(self):
        seed_owner_session(self.conn, "s1")
        rc, out = self.run_cli("item", "new", "Write docs", "--owner", "admin")
        self.assertEqual((rc, out.strip()), (0, "T-0001"))
        self.assertEqual(self.run_cli("item", "pause", "T-0001", "midway")[0], 0)
        rc, out = self.run_cli("item", "resume", "T-0001")
        self.assertEqual((rc, out.strip()), (0, "midway"))
        rc, out = self.run_cli("item", "split", "T-0001", "a", "b")
        self.assertEqual(out.split(), ["T-0002", "T-0003"])
        self.assertEqual(self.run_cli("item", "assign", "T-0002", "backend")[0], 0)
        self.assertIn("backend", self.run_cli("item", "show", "T-0002")[1])
        self.assertIn("T-0001", self.run_cli("next")[1])
        self.assertIn("T-0003", self.run_cli("open")[1])
        rc, out = self.run_cli("decision", "record", "Pay", "--quote", "go ahead",
                               "--session", "s1")
        self.assertEqual(out.strip(), "D-0001")
        self.assertIn("go ahead", self.run_cli("decision", "show", "D-0001")[1])
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.run_cli("item", "status", "T-0001", "bogus")[0], 1)


if __name__ == "__main__":
    unittest.main()
