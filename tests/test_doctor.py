"""Doctor: checks and out-of-band dispatcher-down alerts."""
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
import sys
sys.path.insert(0, str(ENGINE))

from atobots import bots, doctor, messages  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import connect, init_db  # noqa: E402


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

    def test_health_check_message_to_backend_advises_safe_repairs(self):
        # Trigger a finding that goes to backend (e.g. disk or model finding)
        found = doctor.run(
            self.cfg, self.conn, active_fn=lambda: True,
            mem_fn=lambda: 4000, disk_usage_fn=lambda p: (100, 95, 5),
            live_enabled_fn=lambda: False, live_session_fn=lambda: False,
            token_fn=lambda c: True, track=False, now=self.now)
        self.assertTrue(any(f.key == "disk" for f in found))
        msg = self.conn.execute(
            "SELECT body FROM messages WHERE to_bot='backend' AND from_bot='system'"
            " AND dedupe_key LIKE 'doctor:disk:%'").fetchone()
        self.assertIsNotNone(msg)
        body = msg["body"]
        self.assertIn("Do not stop, restart, install, move or delete anything", body)
        self.assertIn("Report what you see to Admin", body)
        self.assertIn("Safe repairs are: retry a backup push, clear a stuck run", body)

    def test_down_services_contain_exact_restart_command_for_owner(self):
        found = doctor.checks(
            self.cfg, self.conn, active_fn=lambda: False,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: True,
            live_session_fn=lambda: False, token_fn=lambda c: True,
            track=False, now=self.now)
        findings = {f.key: f.text for f in found}
        self.assertIn("sudo systemctl restart atobots-dispatcher.service", findings["dispatcher"])
        self.assertIn("sudo systemctl restart atobots-admin.service", findings["admin-live"])


    def test_commands_check_detects_python_and_missing_commands(self):
        # Create a settings.json under home/.claude with python hook
        home_claude = self.cfg.home / ".claude"
        home_claude.mkdir(parents=True, exist_ok=True)
        settings_file = home_claude / "settings.json"
        settings_file.write_text(
            '{"hooks": {"pre-commit": [{"command": "python -m unittest"}]}}',
            encoding="utf-8"
        )
        # Create a settings.local.json under a bot folder with nonexistent command
        bot_claude = self.cfg.bots_dir / "backend" / ".claude"
        bot_claude.mkdir(parents=True, exist_ok=True)
        bot_settings = bot_claude / "settings.local.json"
        bot_settings.write_text(
            '{"hooks": {"post-task": "nonexistent_executable_12345 --foo"}}',
            encoding="utf-8"
        )
        # Also create a routine with missing command
        self.conn.execute(
            "INSERT INTO routines (id, bot, cron, check_cmd) VALUES (42, 'backend', '* * * * *', 'another_bad_cmd_6789')"
        )
        self.conn.commit()

        found = doctor.checks(
            self.cfg, self.conn, active_fn=lambda: True,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: False,
            live_session_fn=lambda: False, token_fn=lambda c: True,
            track=False, now=self.now)

        keys = [f.key for f in found]
        self.assertIn("commands:settings.json:python", keys)
        self.assertIn("commands:settings.local.json:nonexistent_executable_12345", keys)
        self.assertIn("commands:routine:42:another_bad_cmd_6789", keys)

        python_finding = next(f for f in found if f.key == "commands:settings.json:python")
        self.assertIn("this server has python3, not python", python_finding.text)
        self.assertIn("hook command 'python' was not found on PATH", python_finding.text)


if __name__ == "__main__":
    unittest.main()
