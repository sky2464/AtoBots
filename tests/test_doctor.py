"""Doctor: checks and out-of-band dispatcher-down alerts."""
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
import sys
sys.path.insert(0, str(ENGINE))

from claudebot import bots, doctor, messages  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="s",
                    persona="p")
        bots.create(self.cfg, self.conn, "backend", role="backend", soul="s",
                    persona="p")
        self.now = "2026-11-09T12:00:00+00:00"


class CheckTests(Base):
    def test_dispatcher_down_is_a_finding(self):
        found = doctor.checks(
            self.cfg, self.conn, active_fn=lambda: False,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: False,
            live_session_fn=lambda: False, token_fn=lambda c: True,
            track=False, now=self.now)
        keys = [f.key for f in found]
        self.assertIn("dispatcher", keys)


class OutOfBandTests(Base):
    def test_dispatcher_down_writes_local_alert_and_notifies(self):
        notes = []

        def notify(cfg, text, conn=None):
            notes.append(text)
            return True

        found = doctor.run(
            self.cfg, self.conn, active_fn=lambda: False,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: False,
            live_session_fn=lambda: False, token_fn=lambda c: True,
            track=False, now=self.now, notify_fn=notify)
        self.assertTrue(any(f.key == "dispatcher" for f in found))
        path = doctor.alerts_dir(self.cfg) / "dispatcher.txt"
        self.assertTrue(path.is_file())
        self.assertIn("dispatcher is not running", path.read_text())
        self.assertEqual(len(notes), 1)
        self.assertIn("dispatcher is not running", notes[0])
        # backoff: second run does not re-notify immediately
        doctor.run(
            self.cfg, self.conn, active_fn=lambda: False,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: False,
            live_session_fn=lambda: False, token_fn=lambda c: True,
            track=False, now=self.now, notify_fn=notify)
        self.assertEqual(len(notes), 1)

    def test_write_local_alert_mode_600(self):
        path = doctor.write_local_alert(
            self.cfg, "dispatcher", "down", now=self.now)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(path.read_text().startswith(self.now))

    def test_queue_alert_to_admin_still_attempted(self):
        """Queue path remains for when the dispatcher comes back."""
        doctor.run(
            self.cfg, self.conn, active_fn=lambda: False,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: False,
            live_session_fn=lambda: False, token_fn=lambda c: True,
            track=False, now=self.now, notify_fn=lambda *a, **k: True)
        queued = self.conn.execute(
            "SELECT * FROM messages WHERE to_bot='admin' AND from_bot='system'"
            " AND dedupe_key LIKE 'doctor:dispatcher:admin:%'").fetchall()
        self.assertEqual(len(queued), 1)


if __name__ == "__main__":
    unittest.main()
