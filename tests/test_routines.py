import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import bots, cli, routines  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


def _iso(*a):
    return datetime(*a, tzinfo=timezone.utc).isoformat(timespec="seconds")


class RoutinesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="a", soul="s",
                    persona="p")

    def msgs(self):
        return self.conn.execute("SELECT * FROM messages").fetchall()

    def make_due(self, rid, due="2026-11-08T12:00:00+00:00"):
        self.conn.execute("UPDATE routines SET next_due=? WHERE id=?",
                          (due, rid))


class RoutineTests(RoutinesBase):
    def test_check_says_nothing_no_message(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "look",
                           check_cmd="exit 1")
        self.make_due(rid)
        sent = routines.due(self.cfg, self.conn, _iso(2026, 11, 8, 13))
        self.assertEqual(sent, [])
        self.assertEqual(self.msgs(), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs"
                                           ).fetchone()[0], 0)
        # not a failure either
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM activity WHERE kind='routine.check_failed'"
        ).fetchone())
        # and it waits for the next period
        nd = self.conn.execute("SELECT next_due FROM routines").fetchone()[0]
        self.assertGreater(datetime.fromisoformat(nd),
                           datetime.fromisoformat(_iso(2026, 11, 8, 13)))

    def test_check_says_work_sends_message(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "do the thing",
                           check_cmd="exit 0", level="deep")
        self.make_due(rid)
        sent = routines.due(self.cfg, self.conn, _iso(2026, 11, 8, 13))
        self.assertEqual(sent, [rid])
        (m,) = self.msgs()
        self.assertEqual((m["from_bot"], m["to_bot"], m["body"], m["level"]),
                         ("system", "admin", "do the thing", "deep"))
        self.assertEqual(m["dedupe_key"],
                         f"routine:{rid}:2026-11-08T12:00:00+00:00")

    def test_no_check_always_sends(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "x")
        self.make_due(rid)
        self.assertEqual(routines.due(self.cfg, self.conn,
                                      _iso(2026, 11, 8, 13)), [rid])

    def test_failing_check_recorded_and_skipped(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "x",
                           check_cmd="exit 127")
        self.make_due(rid)
        self.assertEqual(routines.due(self.cfg, self.conn,
                                      _iso(2026, 11, 8, 13)), [])
        self.assertEqual(self.msgs(), [])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM activity WHERE kind='routine.check_failed'"
        ).fetchone())

    def test_check_timeout_recorded_and_skipped(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "x",
                           check_cmd="sleep 30")
        self.make_due(rid)
        with mock.patch.object(routines, "CHECK_TIMEOUT", 0.3):
            self.assertEqual(routines.due(self.cfg, self.conn,
                                          _iso(2026, 11, 8, 13), wait=5), [])
        self.assertEqual(self.msgs(), [])
        self.assertIn("timed out", self.conn.execute(
            "SELECT detail FROM activity WHERE kind='routine.check_failed'"
        ).fetchone()[0])

    def test_slow_check_does_not_block_the_tick(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "x",
                           check_cmd="sleep 1; exit 0")
        self.make_due(rid)
        t = time.monotonic()
        sent = routines.due(self.cfg, self.conn, _iso(2026, 11, 8, 13),
                            wait=0.1)
        self.assertLess(time.monotonic() - t, 0.8)
        self.assertEqual(sent, [])
        # not started twice while in flight
        self.assertEqual(routines.due(self.cfg, self.conn,
                                      _iso(2026, 11, 8, 13), wait=0.0), [])
        routines.wait_idle()
        self.assertEqual(len(self.msgs()), 1)

    def test_cron_ct_across_dst(self):
        # DST ends Sun Nov 1, 2026 at 2 AM: CDT (UTC-5) -> CST (UTC-6)
        self.assertEqual(routines.next_time("0 7 * * *", "2026-10-31T00:00:00+00:00"),
                         "2026-10-31T12:00:00+00:00")
        self.assertEqual(routines.next_time("0 7 * * *", "2026-11-01T00:00:00+00:00"),
                         "2026-11-01T13:00:00+00:00")
        self.assertEqual(routines.next_time("0 7 * * *", "2026-11-02T00:00:00+00:00"),
                         "2026-11-02T13:00:00+00:00")

    def test_nonexistent_time_fires_at_next_valid_minute(self):
        # Mar 8, 2026: 2:00 -> 3:00 CT; 2:30 does not exist
        self.assertEqual(routines.next_time("30 2 * * *", "2026-03-08T07:00:00+00:00"),
                         "2026-03-08T08:00:00+00:00")  # 3:00 CDT
        again = routines.next_time("30 2 * * *", "2026-03-08T08:00:00+00:00")
        self.assertEqual(again, "2026-03-09T07:30:00+00:00")  # fired once

    def test_repeated_time_fires_once(self):
        # Nov 1, 2026: 1:30 happens twice (06:30Z CDT, 07:30Z CST)
        first = routines.next_time("30 1 * * *", "2026-11-01T00:00:00+00:00")
        self.assertEqual(first, "2026-11-01T06:30:00+00:00")
        self.assertEqual(routines.next_time("30 1 * * *", first),
                         "2026-11-02T07:30:00+00:00")
        # even starting from the second 1:15, nothing goes backwards
        n = routines.next_time("30 1 * * *", "2026-11-01T07:15:00+00:00")
        self.assertEqual(n, "2026-11-02T07:30:00+00:00")

    def test_cron_syntax(self):
        nt = routines.next_time
        a = "2026-11-08T00:00:00+00:00"  # Sat Nov 7, 6 PM CST
        self.assertEqual(nt("*/15 * * * *", a), "2026-11-08T00:15:00+00:00")
        self.assertEqual(nt("0 8,17 * * *", a), "2026-11-08T14:00:00+00:00")
        # Mon-Fri 9:00 CT; Nov 7 is Saturday -> Monday Nov 9
        self.assertEqual(nt("0 9 * * 1-5", a), "2026-11-09T15:00:00+00:00")
        self.assertEqual(nt("0 9 * * 7", a), "2026-11-08T15:00:00+00:00")  # Sun
        self.assertEqual(nt("10-20/5 12 * * *", a), "2026-11-08T18:10:00+00:00")
        # day-of-month OR day-of-week when both are set
        self.assertEqual(nt("0 0 9 * 0", a), "2026-11-08T06:00:00+00:00")
        self.assertEqual(nt("0 0 29 2 *", a), "2028-02-29T06:00:00+00:00")

    def test_missed_routine_runs_once(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "daily")
        self.make_due(rid, "2026-11-04T13:00:00+00:00")  # 4 days behind
        now = _iso(2026, 11, 8, 3)
        self.assertEqual(routines.due(self.cfg, self.conn, now), [rid])
        self.assertEqual(routines.due(self.cfg, self.conn, now), [])
        self.assertEqual(len(self.msgs()), 1)
        nd = self.conn.execute("SELECT next_due FROM routines").fetchone()[0]
        self.assertEqual(nd, "2026-11-08T13:00:00+00:00")

    def test_same_due_time_never_sends_twice(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "daily")
        self.make_due(rid)
        routines.due(self.cfg, self.conn, _iso(2026, 11, 8, 13))
        self.make_due(rid)  # e.g. restored from a backup
        routines.due(self.cfg, self.conn, _iso(2026, 11, 8, 13))
        self.assertEqual(len(self.msgs()), 1)

    def test_disabled_routine_does_not_fire(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "daily")
        routines.set_enabled(self.conn, rid, False)
        self.make_due(rid)
        self.assertEqual(routines.due(self.cfg, self.conn,
                                      _iso(2026, 11, 8, 13)), [])
        routines.set_enabled(self.conn, rid, True)
        nd = self.conn.execute("SELECT next_due FROM routines").fetchone()[0]
        self.assertGreater(datetime.fromisoformat(nd),
                           datetime.now(timezone.utc))

    def test_changes_recorded_in_activity(self):
        rid = routines.add(self.conn, "admin", "0 7 * * *", "daily", actor="ceo")
        routines.set_enabled(self.conn, rid, False, actor="ceo")
        routines.set_enabled(self.conn, rid, True, actor="ceo")
        routines.remove(self.conn, rid, actor="ceo")
        rows = self.conn.execute("SELECT actor, kind, ref FROM activity WHERE"
                                 " kind LIKE 'routine.%' ORDER BY id").fetchall()
        self.assertEqual([(r["actor"], r["kind"], r["ref"]) for r in rows],
                         [("ceo", "routine.add", str(rid)),
                          ("ceo", "routine.off", str(rid)),
                          ("ceo", "routine.on", str(rid)),
                          ("ceo", "routine.rm", str(rid))])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routines"
                                           ).fetchone()[0], 0)

    def test_invalid_cron_rejected(self):
        for bad in ("", "* * * *", "* * * * * *", "61 * * * *", "* 24 * * *",
                    "* * 0 * *", "* * * 13 *", "* * * * 8", "5-1 * * * *",
                    "*/0 * * * *", "a * * * *", "1,,2 * * * *", "*/x * * * *"):
            with self.assertRaises(ValueError, msg=bad):
                routines.add(self.conn, "admin", bad, "p")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM routines"
                                           ).fetchone()[0], 0)

    def test_bad_arguments_rejected(self):
        with self.assertRaises(ValueError):
            routines.add(self.conn, "admin", "0 7 * * *", "p", level="loud")
        with self.assertRaises(ValueError):
            routines.add(self.conn, "admin", "0 7 * * *", "  ")
        with self.assertRaises(KeyError):
            routines.add(self.conn, "nobody", "0 7 * * *", "p")

    def test_cli_add_list_off_on_rm(self):
        env = {"CLAUDEBOT_HOME": str(self.cfg.home), "CB_BOT": "ceo"}

        def run(*argv):
            out = io.StringIO()
            with mock.patch.dict("os.environ", env), redirect_stdout(out):
                rc = cli.main(list(argv))
            return rc, out.getvalue()

        rc, out = run("routine", "add", "admin", "0 7 * * *", "morning",
                      "--check", "exit 0", "--level", "deep")
        self.assertEqual(rc, 0)
        rc, out = run("routine", "list")
        self.assertIn("0 7 * * *", out)
        self.assertIn("morning", out)
        self.assertIn("CT", out)
        self.assertEqual(run("routine", "off", "1")[0], 0)
        self.assertIn("off", run("routine", "list")[1])
        self.assertEqual(run("routine", "on", "1")[0], 0)
        self.assertEqual(run("routine", "rm", "1")[0], 0)
        self.assertEqual(run("routine", "list")[1].strip(), "")


if __name__ == "__main__":
    unittest.main()
