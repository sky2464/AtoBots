import io
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import cli, update  # noqa: E402
from claudebot.config import Config  # noqa: E402


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.cfg = Config(self.home, run_user="testbot")

    def test_tag_validation(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = update.run_update(self.cfg, "bad-tag")
        self.assertEqual(rc, 2)
        self.assertIn("v0.0.1", err.getvalue())

    def test_refuses_when_unit_is_already_active(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = update.run_update(self.cfg, "v1.2.3",
                                   is_active_fn=lambda unit: True)
        self.assertEqual(rc, 1)
        self.assertIn("already active", err.getvalue())

    def test_build_command_structure(self):
        log_path = self.cfg.logs_dir / "update-test.log"
        cmd = update.build_command(self.cfg, "v1.0.0", log_path, force=False)
        self.assertEqual(cmd[:5], ["sudo", "systemd-run", "--unit=claudebot-update",
                                   "--collect", "--no-block"])
        self.assertIn(f"--setenv=CLAUDEBOT_HOME={self.cfg.home}", cmd)
        self.assertIn(f"--uid={self.cfg.run_user}", cmd)
        self.assertIn(f"StandardOutput=file:{log_path}", cmd)
        self.assertIn(f"StandardError=file:{log_path}", cmd)
        self.assertTrue(cmd[-2].endswith("update.sh"))
        self.assertEqual(cmd[-1], "v1.0.0")

    def test_build_command_force_flag(self):
        log_path = self.cfg.logs_dir / "update-test.log"
        cmd = update.build_command(self.cfg, "v1.0.0", log_path, force=True)
        self.assertEqual(cmd[-1], "--force")
        self.assertEqual(cmd[-2], "v1.0.0")

    def test_run_update_creates_log_dir_mode_700_and_runs(self):
        calls = []

        def fake_runner(cmd, check=True):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = update.run_update(self.cfg, "v0.0.2",
                                   is_active_fn=lambda unit: False,
                                   runner=fake_runner)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.cfg.logs_dir.exists())
        mode = stat.S_IMODE(self.cfg.logs_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)
        stdout = out.getvalue()
        self.assertIn("the update runs on its own; admin will restart at the end",
                      stdout.lower())
        self.assertIn(str(self.cfg.logs_dir), stdout)

    def test_cli_update_command(self):
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.home)}), \
                mock.patch("claudebot.update.is_unit_active", return_value=False), \
                mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(["update", "v0.0.5", "--force"])
            self.assertEqual(rc, 0)
            cmd = mock_run.call_args[0][0]
            self.assertIn("--force", cmd)
            self.assertIn("v0.0.5", cmd)
