import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from atobots import activity, bots, cli, items  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import connect, init_db  # noqa: E402


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
        items.pause(self.conn, a, "stopped at step 3", "admin")
        r = self.row(a)
        self.assertEqual((r["status"], r["note"]), ("paused", "stopped at step 3"))
        self.assertEqual(items.resume(self.conn, a, "admin"), "stopped at step 3")
        self.assertEqual(self.row(a)["status"], "doing")

    def test_assign_moves_owner_and_keeps_note(self):
        a = self.new(status="doing")
        items.pause(self.conn, a, "half done", "admin")
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

    def test_decision_refuses_non_owner_actor(self):
        with self.assertRaises(PermissionError):
            items.decide(self.conn, "pay", "go", "t", "s1", "backend")
        with self.assertRaises(ValueError):
            items.decide(self.conn, "pay", "go", "t", "", "admin")


class ItemCliTests(ItemsBase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        env = {"ATOBOTS_HOME": str(self.cfg.home), "CB_BOT": "",
               "CB_SESSION_ID": ""}
        with mock.patch.dict(os.environ, env), redirect_stdout(buf):
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
        rc, out = self.run_cli("decision", "record", "Pay", "--quote", "go ahead")
        self.assertEqual(out.strip(), "D-0001")
        recorded = self.conn.execute(
            "SELECT session, recorded_by FROM decisions WHERE id='D-0001'"
            ).fetchone()
        self.assertEqual(recorded["recorded_by"], "cli")
        self.assertTrue(recorded["session"].startswith("cli:"))
        self.assertIn("go ahead", self.run_cli("decision", "show", "D-0001")[1])
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.run_cli("item", "status", "T-0001", "bogus")[0], 1)


class DecisionGateTests(ItemsBase):
    def cli_cmd(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        e = {"ATOBOTS_HOME": str(self.cfg.home)}
        e.update(env or {})
        with mock.patch.dict(os.environ, e), redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_owner_session_records_and_a_normal_lane_bot_is_refused(self):
        seed_owner_session(self.conn, "live-1", "live")
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at) VALUES"
            " ('backend','norm-1','normal','2026-11-10T00:00:00+00:00')")
        rc, _, err = self.cli_cmd(
            "decision", "record", "Pay", "--quote", "go",
            env={"CB_BOT": "admin", "CB_SESSION_ID": "norm-1"})
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM decisions").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT detail FROM activity WHERE kind='auth.refused'"
            ).fetchone()["detail"], "decision record")

        rc, out, err = self.cli_cmd(
            "decision", "record", "Pay", "--quote", "go ahead",
            env={"CB_BOT": "backend", "CB_SESSION_ID": "live-1"})
        self.assertEqual((rc, err, out.strip()), (0, "", "D-0001"))
        row = self.conn.execute("SELECT * FROM decisions WHERE id='D-0001'").fetchone()
        self.assertEqual((row["recorded_by"], row["session"], row["owner_session"]),
                         ("admin", "live-1", 1))


class CallerTests(ItemsBase):
    def _run_row(self, bot, session, lane, ended=None):
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at, ended_at)"
            " VALUES (?,?,?,?,?)",
            (bot, session, lane, "2026-11-10T00:00:00+00:00", ended))

    def test_open_run_row_wins_over_the_environment(self):
        self._run_row("admin", "sess-live", "live")
        who = activity.caller(self.conn, environ={
            "CB_BOT": "backend", "CB_SESSION_ID": "sess-live"})
        self.assertEqual(who, activity.Caller("admin", "sess-live", "live", True))
        self._run_row("backend", "sess-work", "normal")
        worker = activity.caller(self.conn, environ={
            "CB_BOT": "admin", "CB_SESSION_ID": "sess-work"})
        self.assertEqual(
            worker, activity.Caller("backend", "sess-work", "normal", False))

    def test_closed_run_falls_back_to_cb_bot(self):
        self._run_row("admin", "sess-old", "chat",
                      ended="2026-11-10T01:00:00+00:00")
        who = activity.caller(self.conn, environ={
            "CB_BOT": "backend", "CB_SESSION_ID": "sess-old"})
        self.assertEqual(who, activity.Caller("backend", None, None, False))

    def test_no_variables_is_cli(self):
        who = activity.caller(
            self.conn, environ={},
            environ_of=lambda pid: {},
            parent_of=lambda pid: 1,
            pid=100)
        self.assertEqual(who, activity.Caller("cli", None, None, False))

    def test_ancestor_environment_is_used_when_the_child_looks_like_cli(self):
        self._run_row("backend", "parent-sess", "normal")

        def parent_of(pid):
            return {100: 50, 50: 1}[pid]

        def environ_of(pid):
            if pid == 50:
                return {"CB_BOT": "admin", "CB_SESSION_ID": "parent-sess"}
            return {}

        who = activity.caller(
            self.conn, environ={}, environ_of=environ_of, parent_of=parent_of,
            pid=100)
        self.assertEqual(
            who, activity.Caller("backend", "parent-sess", "normal", False))

        def bot_only(pid):
            if pid == 40:
                return {"CB_BOT": "backend"}
            return {}

        def parents(pid):
            return {80: 40, 40: 1}[pid]

        named = activity.caller(
            self.conn, environ={}, environ_of=bot_only, parent_of=parents,
            pid=80)
        self.assertEqual(named, activity.Caller("backend", None, None, False))


class ItemAutoLinkTests(ItemsBase):
    def run_cli(self, *argv, env=None):
        buf = io.StringIO()
        e = {"ATOBOTS_HOME": str(self.cfg.home), "CB_BOT": "admin"}
        if env:
            e.update(env)
        with mock.patch.dict(os.environ, e), redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def test_item_new_auto_links_unhandled_requests_in_same_session(self):
        seed_owner_session(self.conn, "sess-1")
        from atobots import requests as req_mod
        rid1 = req_mod.capture(self.conn, "fix login", "sess-1")
        rid2 = req_mod.capture(self.conn, "fix logout", "sess-1")
        rid_other = req_mod.capture(self.conn, "unrelated", "sess-other")

        rc, out = self.run_cli("item", "new", "Fix auth", env={"CB_SESSION_ID": "sess-1"})
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "T-0001")
        self.assertIn(f"Linked to R-{rid1}.", lines)
        self.assertIn(f"Linked to R-{rid2}.", lines)
        self.assertNotIn(f"Linked to R-{rid_other}.", out)

        # Confirm requests 1 and 2 are now linked to T-0001
        self.assertEqual(
            [r["id"] for r in req_mod.unhandled(self.conn)],
            [rid_other])

    def test_item_new_with_explicit_request_flag(self):
        seed_owner_session(self.conn, "sess-1")
        from atobots import requests as req_mod
        rid1 = req_mod.capture(self.conn, "first thing", "sess-1")
        rid2 = req_mod.capture(self.conn, "second thing", "sess-1")

        rc, out = self.run_cli("item", "new", "First item",
                               "--request", f"R-{rid1}",
                               env={"CB_SESSION_ID": "sess-1"})
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "T-0001")
        self.assertIn(f"Linked to R-{rid1}.", lines)
        self.assertNotIn(f"Linked to R-{rid2}.", out)

        # rid1 is handled, rid2 remains unhandled
        self.assertEqual([r["id"] for r in req_mod.unhandled(self.conn)], [rid2])

    def test_item_new_with_no_request_flag(self):
        seed_owner_session(self.conn, "sess-1")
        from atobots import requests as req_mod
        rid = req_mod.capture(self.conn, "request", "sess-1")

        rc, out = self.run_cli("item", "new", "Unlinked item",
                               "--no-request",
                               env={"CB_SESSION_ID": "sess-1"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "T-0001")
        self.assertEqual([r["id"] for r in req_mod.unhandled(self.conn)], [rid])


if __name__ == "__main__":
    unittest.main()
