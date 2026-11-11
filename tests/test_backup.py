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

from atobots import backup, bots, gitlog  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import connect, init_db  # noqa: E402


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
        for bad in (("--force",), ("-q", "--force", "origin"),
                    ("--force-with-lease",), ("--force-if-includes",)):
            with self.assertRaises(backup.BackupError):
                backup._assert_no_force(bad)

    def test_assert_no_force_allows_normal_push_args(self):
        backup._assert_no_force(("-q", "origin", "HEAD:refs/heads/main"))
        backup._assert_no_force(("-q", "backup", "main"))

    def test_assert_no_force_rejects_mirror_and_plus_refspec(self):
        for bad in (("--mirror",), ("+main:main",)):
            with self.assertRaises(backup.BackupError):
                backup._assert_no_force(bad)

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



class RestoreArchiveTests(BackupBase):
    def test_archive_months_come_back_and_existing_files_are_kept(self):
        root = Path(self._tmp.name)
        src = root / "arch-src"
        (src / "2026-09").mkdir(parents=True)
        (src / "2026-09" / "admin-1.jsonl.gz").write_bytes(b"OLD-TRANSCRIPT")
        (src / "2026-09" / "records.jsonl.gz").write_bytes(b"REMOTE")
        (src / "notes").mkdir()
        (src / "notes" / "x.txt").write_text("not a month")
        git(root, "init", "-q", "-b", "main", str(src))
        git(src, "add", "-A")
        git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "-m", "a")
        target = root / "restored"
        (target / "archive" / "2026-09").mkdir(parents=True)
        (target / "archive" / "2026-09" / "records.jsonl.gz").write_bytes(
            b"LOCAL")
        self.assertEqual(backup.restore_archive(str(src), target), 1)
        self.assertEqual((target / "archive" / "2026-09" / "admin-1.jsonl.gz"
                          ).read_bytes(), b"OLD-TRANSCRIPT")
        self.assertEqual((target / "archive" / "2026-09" / "records.jsonl.gz"
                          ).read_bytes(), b"LOCAL")
        self.assertFalse((target / "archive" / "notes").exists())

    def test_bad_source_is_a_clear_error(self):
        with self.assertRaises(backup.BackupError):
            backup.restore_archive(str(Path(self._tmp.name) / "nope"),
                                   Path(self._tmp.name) / "t")


class DumpRedactTests(BackupBase):
    def test_prefix_mention_is_not_a_hit(self):
        self.conn.execute(
            "INSERT INTO activity(at, actor, kind, detail) VALUES (?,?,?,?)",
            ("2026-11-10T00:00:00+00:00", "cli", "note",
             "keys start with sk-ant-"))
        path, hits = backup.export_sql(self.cfg, self.conn)
        self.assertEqual(hits, [])
        self.assertIn("keys start with sk-ant-", path.read_text())

    def test_full_token_is_redacted_and_the_snapshot_continues(self):
        bots.create(self.cfg, self.conn, "admin", role="a", soul="s",
                    persona="p")
        token = "sk-ant-" + ("a" * 32)
        self.conn.execute(
            "INSERT INTO activity(at, actor, kind, detail) VALUES (?,?,?,?)",
            ("2026-11-10T00:00:00+00:00", "cli", "note", f"leak {token} end"))
        sha = backup.snapshot(self.cfg, self.conn)
        self.assertTrue(sha)
        dump = (self.cfg.home / backup.SQL_REL).read_text()
        self.assertNotIn(token, dump)
        self.assertIn("[redacted:Anthropic API key]", dump)
        stored = self.conn.execute(
            "SELECT detail FROM activity WHERE kind='note'").fetchone()["detail"]
        self.assertIn(token, stored)
        notes = self.conn.execute(
            "SELECT subject, body FROM messages WHERE kind='alert'").fetchall()
        self.assertEqual(len(notes), 1)
        self.assertNotIn(token, notes[0]["body"])
        self.assertIn("activity row", notes[0]["body"])


class BackupFilesTests(BackupBase):
    def setUp(self):
        super().setUp()
        self.files_bare = Path(self._tmp.name) / "files.git"
        git(Path(self._tmp.name), "init", "-q", "--bare", "-b", "main", str(self.files_bare))
        git(self.cfg.home, "remote", "add", backup.FILES_REMOTE, str(self.files_bare))
        self.private_dir = Path(self._tmp.name) / "my_private_docs"
        self.private_dir.mkdir(parents=True)
        (self.private_dir / "doc1.txt").write_text("very private text 1")
        (self.private_dir / "doc2.txt").write_text("very private text 2")

    def test_files_add_and_list_and_warning(self):
        self.assertEqual(backup.files_list(self.cfg), [])
        added, is_first = backup.files_add(self.cfg, self.private_dir)
        self.assertTrue(added)
        self.assertTrue(is_first)
        self.assertEqual(backup.files_list(self.cfg), [str(self.private_dir.resolve())])

        # Second add of another folder is not first
        other_dir = Path(self._tmp.name) / "other_docs"
        other_dir.mkdir()
        added, is_first = backup.files_add(self.cfg, other_dir)
        self.assertTrue(added)
        self.assertFalse(is_first)
        self.assertEqual(backup.files_list(self.cfg),
                         sorted([str(self.private_dir.resolve()), str(other_dir.resolve())]))

        # Re-adding existing is False
        added, is_first = backup.files_add(self.cfg, self.private_dir)
        self.assertFalse(added)
        self.assertFalse(is_first)

    def test_files_now_missing_passphrase(self):
        backup.files_add(self.cfg, self.private_dir)
        with mock.patch("atobots.store._reveal", side_effect=Exception("no key")):
            with self.assertRaises(backup.BackupError):
                backup.files_now(self.cfg, self.conn)

    def test_files_now_verify_and_restore(self):
        backup.files_add(self.cfg, self.private_dir)
        passphrase = b"super-secure-passphrase\n"
        real_subp_run = subprocess.run

        # Mock gpg and tar execution so tests don't require external gpg binary
        def mock_subp_run(cmd, *args, **kwargs):
            if cmd[0] == "tar":
                if "-cf" in cmd:
                    tar_path = cmd[2]
                    Path(tar_path).write_bytes(b"mock-tar-contents")
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                elif "-xf" in cmd:
                    target_dir = Path(cmd[4])
                    # Untar mock: recreate structure
                    sub = target_dir / self.private_dir.name
                    sub.mkdir(parents=True, exist_ok=True)
                    (sub / "restored.txt").write_text("restored text")
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            elif cmd[0] == "gpg":
                if "--symmetric" in cmd:
                    out_path = cmd[cmd.index("-o") + 1]
                    Path(out_path).write_bytes(b"mock-gpg-encrypted")
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                elif "--decrypt" in cmd:
                    out_path = cmd[cmd.index("-o") + 1]
                    if out_path != os.devnull:
                        Path(out_path).write_bytes(b"mock-tar-contents")
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_subp_run(cmd, *args, **kwargs)

        with mock.patch("atobots.store._reveal", return_value=passphrase):
            with mock.patch("subprocess.run", side_effect=mock_subp_run):
                res = backup.files_now(self.cfg, self.conn)
                self.assertEqual(res, "pushed")

                ver = backup.files_verify(self.cfg)
                self.assertEqual(ver, "ok")

                restore_dir = Path(self._tmp.name) / "restored_files"
                count = backup.restore_files(self.cfg, str(self.files_bare), restore_dir)
                self.assertEqual(count, 1)
                self.assertTrue((restore_dir / self.private_dir.name / "restored.txt").is_file())

    def test_cmd_now_runs_files_when_configured(self):
        backup.files_add(self.cfg, self.private_dir)
        with mock.patch.object(backup, "snapshot", return_value="12345678"):
            with mock.patch.object(backup, "push", return_value="pushed"):
                with mock.patch.object(backup, "files_now", return_value="pushed") as fn:
                    with mock.patch.object(backup, "files_verify", return_value="ok") as fv:
                        with mock.patch("atobots.backup.Config.load", return_value=self.cfg):
                            rc = backup._cmd_now(None)
                            self.assertEqual(rc, 0)
                            self.assertTrue(fn.called)
                            self.assertTrue(fv.called)


if __name__ == "__main__":
    unittest.main()
