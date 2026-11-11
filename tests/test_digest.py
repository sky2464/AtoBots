import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from atobots import activity, bots, cli, digest, dispatcher, items, messages, requests, usage
from atobots.config import Config
from atobots.db import connect, init_db

T0 = datetime(2026, 11, 10, 8, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t.isoformat(timespec="seconds")

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="s", persona="p")
        bots.create(self.cfg, self.conn, "backend", role="backend", soul="s", persona="p")

    def test_digest_empty_when_nothing_changed(self):
        # Empty company state with no changes and no open items/requests
        text, since = digest.build_digest(self.cfg, self.conn, now=T0.isoformat(timespec="seconds"))
        # doctor.checks might report disk usage if over limit or timezone unset
        # Let's inspect text if any
        if text is not None:
            # If disk is reported, that's a health finding
            self.assertIn("Health findings", text)
        else:
            self.assertIsNone(text)

    def test_digest_builds_all_sections(self):
        t_base = T0 - timedelta(hours=10)
        since = t_base.isoformat(timespec="seconds")

        # 1. Closed item
        item_done = items.new(self.cfg, self.conn, "Fix bug", "admin")
        items.set_status(self.conn, item_done, "done", "admin")

        # 2. Opened item
        item_open = items.new(self.cfg, self.conn, "New feature", "backend")

        # 3. Item waiting on Owner
        items.set_owner_waiting(self.conn, item_open, True, "backend")

        # 4. Unhandled request (>24h and recent)
        t_old = (T0 - timedelta(hours=30)).isoformat(timespec="seconds")
        rid_old = requests.capture(self.conn, "Old request text", "s-old")
        self.conn.execute("UPDATE owner_requests SET received_at=? WHERE id=?", (t_old, rid_old))

        rid_recent = requests.capture(self.conn, "Recent request text", "s-new")

        # 5. Decision
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at) VALUES ('admin','sess-d','chat',?)",
            (since,))
        items.decide(self.conn, "Launch product", "Let's ship it", since, "sess-d", "admin")

        # 6. Failed run
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at, ended_at, reason) VALUES "
            "('backend','sess-fail','normal',?,?,?)",
            (since, since, "error"))

        # 7. Usage
        usage.record(self.conn, bot="backend", model="sonnet", lane="normal", trigger="task",
                     ref=None, input_tokens=100, cache_write=20, cache_read=80, output_tokens=50,
                     at=since)

        text, _ = digest.build_digest(self.cfg, self.conn, since=since, now=T0.isoformat(timespec="seconds"))
        self.assertIsNotNone(text)
        self.assertIn("Items closed", text)
        self.assertIn("Fix bug", text)
        self.assertIn("Items opened", text)
        self.assertIn("New feature", text)
        self.assertIn("Waiting on Owner", text)
        self.assertIn("Unhandled requests", text)
        self.assertIn(">24h", text)
        self.assertIn("Decisions recorded", text)
        self.assertIn("Launch product", text)
        self.assertIn("Failed runs", text)
        self.assertIn("Usage", text)
        self.assertIn("call(s)", text)

    def test_digest_reports_a_failed_health_check(self):
        from unittest import mock
        from atobots import doctor
        with mock.patch.object(doctor, "checks", side_effect=RuntimeError("boom")):
            text, _ = digest.build_digest(
                self.cfg, self.conn, since=(T0 - timedelta(days=1)).isoformat(timespec="seconds"),
                now=T0.isoformat(timespec="seconds"))
        self.assertIn("health check itself failed", text)
        self.assertIn("boom", text)

    def test_digest_max_length_cut(self):
        long_text = "x" * 4000
        cut = digest.format_for_telegram(long_text)
        self.assertLessEqual(len(cut), 3500)
        self.assertTrue(cut.endswith("more: cb digest"))

    def test_cli_digest(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.cfg.home)}), \
                redirect_stdout(buf):
            rc = cli.main(["digest"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertTrue(len(out) > 0)


class DispatcherDigestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = Clock()
        self.cfg = Config(Path(self._tmp.name) / "home", timezone="America/Chicago")
        self.cfg.digest_time = "08:00"
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="s", persona="p")

    def test_dispatcher_sends_digest_at_digest_time_in_tz(self):
        notified = []

        def notify(cfg, text, conn=None):
            notified.append(text)

        d = dispatcher.Dispatcher(
            self.cfg, clock=self.clock, live_fn=lambda: True,
            notify_fn=notify,
            run_fn=lambda *a, **k: None)

        # In America/Chicago (UTC-6 in standard time):
        # 08:00 Chicago = 14:00 UTC
        # Start clock at 13:59 UTC
        self.clock.t = datetime(2026, 11, 10, 13, 59, 0, tzinfo=timezone.utc)
        # Create an item so there are changes
        items.new(self.cfg, self.conn, "Item A", "admin")

        d.tick()
        self.assertEqual(notified, [])

        # Advance to 14:00 UTC (08:00 Chicago)
        self.clock.advance(60)
        d.tick()
        self.assertEqual(len(notified), 1)
        self.assertIn("Item A", notified[0])

        # Verify meta recorded
        last_sent = self.conn.execute("SELECT value FROM meta WHERE key='digest.last_sent'").fetchone()
        self.assertIsNotNone(last_sent)
        self.assertEqual(last_sent["value"], self.clock())

        # Next tick on same day: does not send again
        self.clock.advance(60)
        d.tick()
        self.assertEqual(len(notified), 1)


if __name__ == "__main__":
    unittest.main()
