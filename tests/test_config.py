import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import db, routines  # noqa: E402
from claudebot.config import Config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.addCleanup(db.set_zone, "UTC")

    def load(self, data=None):
        if data is not None:
            (self.home / "config.json").write_text(json.dumps(data))
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.home)}):
            return Config.load()

    def test_account_defaults_to_whoever_runs_cb(self):
        cfg = self.load()
        self.assertTrue(cfg.run_user)
        self.assertTrue(cfg.run_home.startswith("/"))
        self.assertEqual(cfg.claude_bin, f"{cfg.run_home}/.local/bin/claude")
        self.assertEqual(cfg.timezone, "UTC")
        self.assertEqual(
            cfg.claude_projects_dir,
            Path(cfg.run_home) / ".claude" / "projects")
        self.assertEqual(cfg.rules_cap_bytes, 4 * 1024)

    def test_config_get_prints_one_value(self):
        from claudebot import cli
        buf = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.home)}), \
                redirect_stdout(buf):
            self.assertEqual(cli.main(["config", "get", "run_memory_max"]), 0)
        self.assertEqual(buf.getvalue().strip(), "1200M")
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.home)}), \
                redirect_stderr(err):
            self.assertEqual(cli.main(["config", "get", "no_such_key"]), 1)
        self.assertIn("unknown setting", err.getvalue())

    def test_text_settings_can_be_overridden(self):
        cfg = self.load({"run_user": "bots", "run_home": "/srv/bots",
                         "run_memory_max": "800M", "default_model": "opus",
                         "timezone": "Europe/Berlin", "hop_limit": 5})
        self.assertEqual((cfg.run_user, cfg.run_home), ("bots", "/srv/bots"))
        self.assertEqual(cfg.claude_bin, "/srv/bots/.local/bin/claude")
        self.assertEqual((cfg.run_memory_max, cfg.default_model, cfg.hop_limit),
                         ("800M", "opus", 5))
        self.assertEqual(db.zone_name(), "Europe/Berlin")

    def test_lease_must_cover_run_time(self):
        with self.assertRaises(ValueError):
            self.load({"lease_seconds": 100, "run_seconds": 1800})

    def test_bad_text_values_stop_with_a_clear_message(self):
        for bad in ({"run_user": "a; rm -rf /"}, {"run_home": "relative"},
                    {"run_memory_max": "lots"}, {"timezone": "Mars/Phobos"},
                    {"claude_bin": 5}):
            with self.assertRaises(RuntimeError, msg=bad):
                self.load(bad)

    def test_existing_schedules_keep_chicago_until_timezone_is_set(self):
        from claudebot.config import Config
        from claudebot.db import init_db
        cfg = Config(self.home)
        init_db(cfg)
        conn = db.connect(cfg)
        conn.execute(
            "INSERT INTO routines(bot, cron, prompt, level, enabled, next_due)"
            " VALUES ('admin','0 7 * * *','look','normal',1,"
            " '2026-11-10T12:00:00+00:00')")
        conn.close()
        cfg = self.load()
        self.assertEqual(cfg.timezone, "America/Chicago")
        self.assertTrue(cfg.timezone_unset)
        cfg = self.load({"timezone": "UTC"})
        self.assertEqual(cfg.timezone, "UTC")
        self.assertFalse(cfg.timezone_unset)

    def test_zone_drives_display_and_routine_times(self):
        self.load({"timezone": "Europe/Berlin"})
        shown = db.ct_display("2026-07-01T10:00:00+00:00")
        self.assertIn("12:00 PM", shown)
        self.assertTrue(shown.endswith("CEST"))
        # 07:00 in Berlin (summer) is 05:00 UTC
        self.assertEqual(
            routines.next_time("0 7 * * *", "2026-07-01T00:00:00+00:00"),
            "2026-07-01T05:00:00+00:00")
        db.set_zone("America/Chicago")
        self.assertTrue(db.ct_display("2026-07-01T10:00:00+00:00"
                                      ).endswith("5:00 AM CDT"))


if __name__ == "__main__":
    unittest.main()
