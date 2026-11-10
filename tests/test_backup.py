import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import backup, gitlog  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=check,
                          env={**os.environ, **gitlog._ENV})


class BackupBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.cfg = Config(root / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        gitlog.ensure_repo(self.cfg)
        self.archive_bare = root / "archive.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.archive_bare))
        # Seed an empty commit so clone has a branch (or leave empty — _push_month
        # handles empty remotes). Add the remote on the company home.
        git(self.cfg.home, "remote", "add", backup.ARCHIVE_REMOTE,
            str(self.archive_bare))

    def write_month(self, month: str, rel: str, text: str):
        p = self.cfg.archive_dir / month / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class ForcePushTests(unittest.TestCase):
    def test_assert_no_force_rejects_flags(self):
        for bad in (("--force",), ("-f",), ("-q", "--force", "origin"),
                    ("--force-with-lease",), ("--force-if-includes",),
                    ("-qf", "origin", "main")):
            with self.assertRaises(backup.BackupError):
                backup._assert_no_force(bad)

    def test_assert_no_force_allows_normal_push_args(self):
        backup._assert_no_force(("-q", "origin", "HEAD:refs/heads/main"))
        backup._assert_no_force(("-q", "backup", "main"))

    def test_git_push_refuses_force_before_running(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(backup.BackupError) as cm:
                backup._git_push(Path(d), "--force", "origin", "main")
            self.assertIn("force", str(cm.exception).lower())


class SecretScanTests(BackupBase):
    def test_scan_paths_finds_secret_value(self):
        secret = "super-secret-token-value"
        path = self.write_month("2026-10", "leak.txt", f"prefix {secret} suffix")
        with mock.patch.object(backup, "_secret_values",
                               return_value={"tok": {secret.encode()}}):
            hits = backup.scan_paths_for_secrets(self.cfg, [path])
        self.assertEqual(hits, ["tok"])

    def test_scan_paths_clean_when_no_match(self):
        path = self.write_month("2026-10", "ok.txt", "nothing sensitive here")
        with mock.patch.object(backup, "_secret_values",
                               return_value={"tok": {b"super-secret-token-value"}}):
            self.assertEqual(backup.scan_paths_for_secrets(self.cfg, [path]), [])

    def test_push_month_blocked_by_secret_scan(self):
        secret = "archive-must-not-leak-this"
        self.write_month("2026-09", "note.txt", f"embed {secret}")
        with mock.patch.object(backup, "_secret_values",
                               return_value={"tok": {secret.encode()}}):
            with self.assertRaises(backup.BackupError) as cm:
                backup._push_month(self.cfg, "2026-09")
        self.assertIn("secret scan", str(cm.exception).lower())
        # Nothing should have been pushed to the bare remote.
        show = git(self.archive_bare, "show-ref", check=False)
        self.assertNotEqual(show.returncode, 0)


class ArchivePushTests(BackupBase):
    def test_push_month_uses_non_force_and_verifies(self):
        self.write_month("2026-09", "a.txt", "hello archive")
        with mock.patch.object(backup, "_secret_values", return_value={}):
            with mock.patch.object(backup, "_git_push",
                                   wraps=backup._git_push) as push:
                marker = backup._push_month(self.cfg, "2026-09")
        self.assertTrue(push.called)
        for call in push.call_args_list:
            args = call.args[1:]  # skip cwd
            self.assertNotIn("--force", args)
            self.assertTrue(all(not str(a).startswith("--force") for a in args))
        self.assertEqual(marker["remote"], backup.ARCHIVE_REMOTE)
        self.assertIn("a.txt", marker["verified"])
        # Local files still present (push does not delete).
        self.assertTrue((self.cfg.archive_dir / "2026-09" / "a.txt").is_file())

    def test_mirror_current_month_pushes_without_deleting(self):
        self.write_month("2026-11", "hot.txt", "still hot on disk")
        with mock.patch.object(backup, "_secret_values", return_value={}):
            month = backup.mirror_current_month(
                self.cfg, self.conn, now="2026-11-09T12:00:00+00:00")
        self.assertEqual(month, "2026-11")
        self.assertTrue((self.cfg.archive_dir / "2026-11" / "hot.txt").is_file())
        # Idempotent when signature unchanged.
        with mock.patch.object(backup, "_secret_values", return_value={}):
            again = backup.mirror_current_month(
                self.cfg, self.conn, now="2026-11-09T13:00:00+00:00")
        self.assertIsNone(again)

    def test_mirror_skips_without_archive_remote(self):
        git(self.cfg.home, "remote", "remove", backup.ARCHIVE_REMOTE)
        self.write_month("2026-11", "hot.txt", "x")
        self.assertIsNone(backup.mirror_current_month(
            self.cfg, self.conn, now="2026-11-09T12:00:00+00:00"))

    def test_push_argv_for_backup_remote_never_forced(self):
        git(self.cfg.home, "remote", "add", backup.BACKUP_REMOTE,
            str(self.archive_bare))
        with mock.patch.object(backup, "scan_for_secrets", return_value=[]):
            with mock.patch.object(backup, "_git_push",
                                   return_value="") as push:
                self.assertEqual(backup.push(self.cfg, self.conn), "pushed")
        args = push.call_args.args[1:]
        self.assertNotIn("--force", args)
        self.assertEqual(args[-2:], (backup.BACKUP_REMOTE, "main"))


if __name__ == "__main__":
    unittest.main()
