import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "tests"))

import fakes  # noqa: E402
from atobots import bots, changes, cli, names, update  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import connect, init_db  # noqa: E402


def make_zip(path: Path, entries: dict, links: dict | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100755 if name.endswith((".sh", "/cb"))
                                  else 0o100644) << 16
            zf.writestr(info, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return path


def zip_tree(src: Path, path: Path, prefix: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file() and ".git" not in p.parts \
                    and "__pycache__" not in p.parts:
                zf.write(p, prefix + p.relative_to(src).as_posix())
    return path


GOOD = {"engine/update.sh": "#!/bin/sh\n", "engine/bin/cb": "#!/bin/sh\n"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cfg = Config(self.home, run_user="testbot")


class VersionTests(unittest.TestCase):
    def test_local_suffix_is_ignored_when_ordering(self):
        self.assertEqual(update.version_key("0.1.0+local.abc1234"), (0, 1, 0))
        self.assertEqual(update.version_key("v0.1.0"), (0, 1, 0))
        self.assertLess(update.version_key("0.0.9+local.abc1234"),
                        update.version_key("0.0.10"))
        self.assertTrue(update.is_local("0.1.0+local.abc1234"))
        self.assertFalse(update.is_local("0.1.0"))
        for bad in ("", "1.2", "v1.2.3.4", "1.2.3+local.XYZ", "latest"):
            with self.assertRaises(update.UpdateError):
                update.version_key(bad)

    def test_migrations_run_in_version_order_between_old_and_new(self):
        files = ["0.1.0-rename.sh", "0.0.3-first.sh", "0.2.0-later.sh",
                 "README.md", "0.0.10-tenth.sh", "notes.sh"]
        self.assertEqual(
            update.migrations_between(files, "0.0.2", "0.1.0"),
            ["0.0.3-first.sh", "0.0.10-tenth.sh", "0.1.0-rename.sh"])
        self.assertEqual(
            update.migrations_between(files, "0.1.0", "0.1.0",
                                      ["0.1.0-rename.sh"]), [])
        # going back runs nothing
        self.assertEqual(update.migrations_between(files, "0.2.0", "0.0.3",
                                                   files), [])

    def test_a_second_local_build_can_still_add_a_migration(self):
        files = ["0.1.0-rename.sh", "0.1.0-second.sh"]
        self.assertEqual(
            update.migrations_between(files, "0.1.0+local.aaaaaaa",
                                      "0.1.0+local.bbbbbbb",
                                      ["0.1.0-rename.sh"]),
            ["0.1.0-second.sh"])

    def test_an_unknown_old_version_runs_everything_up_to_new(self):
        self.assertEqual(
            update.migrations_between(["0.0.3-a.sh", "0.1.0-b.sh"],
                                      "unknown", "0.0.3"), ["0.0.3-a.sh"])


class ZipTests(Base):
    def refuse(self, entries, links=None, needle=""):
        z = make_zip(self.tmp / "b.zip", entries, links)
        with self.assertRaises(update.UpdateError) as cm:
            update.validate_zip(z)
        self.assertIn(needle, str(cm.exception))

    def test_refuses_absolute_paths(self):
        self.refuse(dict(GOOD, **{"/etc/x": "x"}), needle="absolute")

    def test_refuses_dot_dot(self):
        self.refuse(dict(GOOD, **{"engine/../../x": "x"}), needle="'..'")

    def test_refuses_every_link(self):
        """Two links that each stay inside can lead out together, so a zip
        may hold none."""
        self.refuse(GOOD, {"engine/out": "../../etc/passwd"}, needle="link")
        self.refuse(GOOD, {"engine/abs": "/etc/passwd"}, needle="link")
        self.refuse(GOOD, {"engine/inside": "bin/cb"}, needle="link")
        self.refuse(GOOD, {"d/l1": "..", "d/l1/l2": ".."}, needle="link")

    def test_refuses_a_zip_without_the_updater(self):
        self.refuse({"engine/bin/cb": "x"}, needle="no engine/update.sh")
        self.refuse({"a/engine/update.sh": "x", "b/other": "x"},
                    needle="no engine/update.sh")

    def test_refuses_garbage(self):
        bad = self.tmp / "bad.zip"
        bad.write_text("not a zip")
        with self.assertRaises(update.UpdateError):
            update.validate_zip(bad)

    def test_accepts_a_tree_wrapped_in_one_folder(self):
        z = make_zip(self.tmp / "ok.zip",
                     {"repo-main/engine/update.sh": "#!/bin/sh\n",
                      "repo-main/engine/bin/cb": "#!/bin/sh\n",
                      "repo-main/README.md": "hi"})
        root = update.extract_zip(z, self.tmp / "out")
        self.assertEqual(root.name, "repo-main")
        self.assertTrue(os.access(root / "engine" / "update.sh", os.X_OK))
        update.validate_tree(root)

    def test_a_folder_build_may_not_link_outside(self):
        root = self.tmp / "tree"
        (root / "engine" / "bin").mkdir(parents=True)
        (root / "engine" / "bin" / "cb").write_text("x")
        (root / "engine" / "update.sh").write_text("x")
        update.validate_tree(root)
        (root / "engine" / "evil").symlink_to("/etc")
        with self.assertRaises(update.UpdateError):
            update.validate_tree(root)

    def test_unpacked_goes_under_the_company_home_and_is_removed(self):
        z = make_zip(self.tmp / "ok.zip", GOOD)
        with update.Unpacked(self.home, zip_path=z) as build:
            self.assertTrue(str(build.root).startswith(
                str(self.home / ".update")))
            self.assertEqual(len(build.sha), 64)
            kept = build.root
        self.assertFalse(kept.exists())


class LocalBuildTests(Base):
    def setUp(self):
        super().setUp()
        self.repo = fakes.make_engine_repo(self.tmp / "clone", tag="v0.0.2")

    def test_import_commits_on_local_builds_without_moving_the_checkout(self):
        head = fakes.git(self.repo, "rev-parse", "HEAD")
        src = fakes.copy_engine(self.tmp / "newer")
        (src / "engine" / "NEW_FILE").write_text("new\n")
        with update.Unpacked(self.home, dir_path=src) as build:
            commit = update.import_local(self.repo, build.root, build.sha,
                                         build.name)
            sha = build.sha
            again = update.import_local(self.repo, build.root, build.sha,
                                        build.name)
        self.assertEqual(again, commit)  # the same build is not committed twice
        self.assertEqual(fakes.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(fakes.git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(
            fakes.git(self.repo, "rev-parse", "refs/heads/local-builds"),
            commit)
        self.assertIn(f"local build {sha[:12]}",
                      fakes.git(self.repo, "log", "-1", "--format=%s", commit))
        marker = fakes.git(self.repo, "show", f"{commit}:engine/LOCAL_BUILD")
        self.assertEqual(marker, sha)
        self.assertEqual(fakes.git(self.repo, "show",
                                   f"{commit}:engine/NEW_FILE"), "new")
        tree = update.RefTree(self.repo, commit)
        self.assertTrue(update.tree_version(tree).endswith(
            f"+local.{sha[:7]}"))
        # the source folder itself is not modified
        self.assertFalse((src / "engine" / "LOCAL_BUILD").exists())

    def test_import_ignores_the_servers_ignore_lists_and_drops_caches(self):
        """A global gitignore on the server must not drop a file of the
        build, and Python caches inside it must not be committed."""
        src = fakes.copy_engine(self.tmp / "newer")
        (src / ".gitignore").unlink()
        (src / "engine" / "bin" / "notes.log").write_text("keep me\n")
        cache = src / "engine" / "atobots" / "__pycache__"
        cache.mkdir()
        (cache / "x.cpython-313.pyc").write_bytes(b"\0")
        ignore = self.tmp / "global-ignore"
        ignore.write_text("*.log\nbin\n")
        fakes.git(self.repo, "config", "core.excludesFile", str(ignore))
        z = zip_tree(src, self.tmp / "b.zip")
        with zipfile.ZipFile(z, "a") as zf:
            zf.writestr("engine/atobots/__pycache__/y.pyc", b"\0")
        with update.Unpacked(self.home, zip_path=z) as build:
            commit = update.import_local(self.repo, build.root, build.sha,
                                         build.name)
        files = fakes.git(self.repo, "ls-tree", "-r", "--name-only", commit)
        self.assertIn("engine/bin/cb", files)
        self.assertIn("engine/bin/notes.log", files)
        self.assertNotIn("__pycache__", files)
        self.assertNotIn(".pyc", files)
        exclude = (self.repo / ".git" / "info" / "exclude").read_text()
        self.assertIn("__pycache__/", exclude)

    def test_a_release_tree_never_carries_the_local_marker(self):
        tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT,
                                          text=True).split()
        self.assertNotIn("engine/LOCAL_BUILD", tracked)

    def test_cb_version_shows_the_local_suffix(self):
        marker = self.repo / "engine" / "LOCAL_BUILD"
        marker.write_text("ab" * 32 + "\n")
        env = dict(os.environ)
        r = subprocess.run([str(self.repo / "engine" / "bin" / "cb"),
                            "version"], capture_output=True, text=True,
                           env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^\d+\.\d+\.\d+\+local\.abababa$")


class CheckTests(Base):
    def setUp(self):
        super().setUp()
        init_db(self.cfg)

    def check(self, repo, **kw):
        return update.check(self.cfg, repo=repo, fetch=False,
                            admin_enabled_fn=lambda: True, **kw)

    def test_copied_install_must_be_adopted(self):
        copy = fakes.copy_engine(self.tmp / "copy")
        report = self.check(copy)
        self.assertTrue(report["needs_adopt"])
        self.assertFalse(report["ready"])
        self.assertEqual(report["current_source"], "copy")
        text = update.format_check(report)
        self.assertIn("copy with no Git history", text)
        self.assertIn("cb update --adopt", text)
        self.assertIn("Not ready", text)

    def test_copied_install_with_a_zip_newer_than_every_tag(self):
        """The situation that started this work: one paragraph answers it."""
        copy = fakes.copy_engine(self.tmp / "copy")
        newer = fakes.copy_engine(self.tmp / "newer")
        fakes.set_version(newer, "9.9.9")
        (newer / "engine" / "templates" / "rules.md").write_text("new rules\n")
        (newer / "engine" / "migrations" / "9.9.9-step.sh").write_text("x\n")
        z = zip_tree(newer, self.tmp / "build.zip", prefix="build/")
        report = self.check(copy, zip_path=z)
        self.assertEqual(report["relation"], "newer")
        self.assertEqual(report["target"]["kind"], "local")
        self.assertRegex(report["target"]["version"],
                         r"^9\.9\.9\+local\.[0-9a-f]{7}$")
        self.assertFalse(report["target"]["verified"])
        self.assertIn("bot templates and rules", report["includes"])
        self.assertIn("9.9.9-step.sh", report["migrations"])
        self.assertTrue(report["needs_adopt"])
        text = update.format_check(report)
        self.assertIn("unverified local build", text)
        self.assertIn("newer than what is running", text)
        self.assertIn("9.9.9-step.sh", text)
        self.assertIn("offline for about 2 minutes", text)
        self.assertLess(len(text.splitlines()), 12)
        json.dumps(report)  # the --json form
        # nothing was left behind, and the copy was not touched
        self.assertEqual([p.name for p in (self.home / ".update").iterdir()],
                         [".gitignore"])
        self.assertFalse((copy / ".git").exists())

    def test_clone_and_a_newer_tag(self):
        repo = fakes.make_engine_repo(self.tmp / "clone", tag="v0.0.2")
        fakes.set_version(repo, "99.0.9")
        (repo / "engine" / "systemd" / "extra.service").write_text("[Unit]\n")
        fakes.commit_all(repo, "next")
        fakes.git(repo, "tag", "v99.0.9")
        fakes.git(repo, "checkout", "-q", "--detach", "v0.0.2")
        report = self.check(repo)
        self.assertTrue(report["is_clone"])
        self.assertEqual(report["target"]["label"], "tag:v99.0.9")
        self.assertEqual(report["target"]["version"], "99.0.9")
        self.assertEqual(report["relation"], "newer")
        self.assertEqual(report["files_changed"], 2)
        self.assertIn("service unit files", report["includes"])
        self.assertTrue(report["ready"])
        text = update.format_check(report, update.start_command(report))
        self.assertIn("Ready. Start it with: cb update v99.0.9", text)
        self.assertIn("the dispatcher and the always-on Admin", text)
        # asking for an older tag says so
        fakes.git(repo, "checkout", "-q", "--detach", "v99.0.9")
        older = self.check(repo, tag="v0.0.2")
        self.assertEqual(older["relation"], "older")
        self.assertIn("OLDER", update.format_check(older))

    def test_dirty_tree_blocks(self):
        repo = fakes.make_engine_repo(self.tmp / "clone", tag="v0.0.2")
        (repo / "engine" / "update.sh").write_text("edited\n")
        report = self.check(repo)
        self.assertEqual(report["dirty"], ["engine/update.sh"])
        self.assertFalse(report["ready"])
        self.assertIn("local edits", update.format_check(report))

    def test_same_files_is_nothing_to_do_and_unknown_tag_blocks(self):
        repo = fakes.make_engine_repo(self.tmp / "clone", tag="v0.0.2")
        report = self.check(repo)
        self.assertEqual(report["files_changed"], 0)
        self.assertFalse(report["ready"])
        self.assertIn("Nothing to do", update.format_check(report))
        missing = self.check(repo, tag="v7.7.7")
        self.assertIn("there is no release v7.7.7", missing["blockers"])

    def test_active_runs_and_measured_offline_time_are_reported(self):
        repo = fakes.make_engine_repo(self.tmp / "clone", tag="v0.0.2")
        conn = connect(self.cfg)
        conn.execute("INSERT INTO runs(bot, session_id, lane, started_at)"
                     " VALUES ('x','s','work','2026-01-01T00:00:00+00:00')")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                     (update.OFFLINE_KEY, "290"))
        conn.commit()
        conn.close()
        report = self.check(repo)
        self.assertEqual(report["active_runs"], 1)
        self.assertEqual(report["offline_seconds"], 290)
        text = update.format_check(report)
        self.assertIn("1 bot run(s) are active", text)
        self.assertIn("about 5 minutes (measured at the last update)", text)

    def test_cli_check_is_read_only_and_exits_1_when_blocked(self):
        copy = fakes.copy_engine(self.tmp / "copy")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.home)}), \
                mock.patch.object(update, "repo_dir", return_value=copy), \
                redirect_stdout(out):
            rc = cli.main(["update", "--check"])
        self.assertEqual(rc, 1)
        self.assertIn("Running now:", out.getvalue())


class StartTests(Base):
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
        self.assertEqual(cmd[:5], ["sudo", "systemd-run",
                                   f"--unit={names.unit('update')}",
                                   "--collect", "--no-block"])
        self.assertIn(f"--setenv={names.HOME_VAR}={self.cfg.home}", cmd)
        self.assertIn(f"--uid={self.cfg.run_user}", cmd)
        self.assertIn(f"StandardOutput=file:{log_path}", cmd)
        self.assertIn(f"StandardError=file:{log_path}", cmd)
        self.assertTrue(cmd[-2].endswith("update.sh"))
        self.assertEqual(cmd[-1], "v1.0.0")
        forced = update.build_command(self.cfg, "v1.0.0", log_path, force=True)
        self.assertEqual(forced[-2:], ["v1.0.0", "--force"])

    def run_ok(self, fn, *args, **kw):
        calls = []

        def fake_runner(cmd, check=True):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = fn(self.cfg, *args, is_active_fn=lambda unit: False,
                    runner=fake_runner, **kw)
        return rc, calls, out.getvalue()

    def test_run_update_creates_log_dir_mode_700_and_runs(self):
        rc, calls, stdout = self.run_ok(update.run_update, "v0.0.2")
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        mode = stat.S_IMODE(self.cfg.logs_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)
        self.assertIn("the update runs on its own; admin will restart at the end",
                      stdout.lower())
        self.assertIn(str(self.cfg.logs_dir), stdout)
        self.assertIn("rolls itself back", stdout)

    def test_no_tag_means_the_newest_release(self):
        rc, calls, _ = self.run_ok(update.run_update, None)
        self.assertEqual(rc, 0)
        self.assertTrue(calls[0][-1].endswith("update.sh"))

    def test_cli_update_command(self):
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.home)}), \
                mock.patch("atobots.update.is_unit_active", return_value=False), \
                mock.patch("atobots.update.is_clone", return_value=True), \
                mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(["update", "v0.0.5", "--force"])
            self.assertEqual(rc, 0)
            cmd = mock_run.call_args[0][0]
            self.assertIn("--force", cmd)
            self.assertIn("v0.0.5", cmd)

    def test_cli_update_on_a_copy_points_at_adopt(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.home)}), \
                mock.patch("atobots.update.is_clone", return_value=False), \
                redirect_stderr(err):
            rc = cli.main(["update", "v0.0.5"])
        self.assertEqual(rc, 1)
        self.assertIn("--adopt", err.getvalue())


class LocalStartTests(Base):
    """A local build is unverified code: it needs the Owner's recorded yes."""

    def setUp(self):
        super().setUp()
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="s",
                    persona="p")
        self.repo = fakes.make_engine_repo(self.tmp / "clone", tag="v0.0.2")
        src = fakes.copy_engine(self.tmp / "newer")
        fakes.set_version(src, "0.0.9")
        self.zip = zip_tree(src, self.tmp / "build.zip")

    def as_bot(self):
        return mock.patch.dict(os.environ, {"CB_BOT": "admin"})

    def start(self, **kw):
        calls = []

        def runner(cmd, check=True):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = update.run_local(self.cfg, zip_path=self.zip, repo=self.repo,
                                  is_active_fn=lambda u: False, runner=runner,
                                  conn=self.conn, **kw)
        return rc, calls, out.getvalue()

    def test_a_bot_cannot_install_a_local_build_without_an_approved_change(self):
        with self.as_bot():
            with self.assertRaises(update.UpdateError) as cm:
                self.start()
            self.assertIn("needs the Owner's yes", str(cm.exception))
            cid = changes.plan(self.conn, "install build", "steps",
                               ["engine"], actor="admin")
            with self.assertRaises(update.UpdateError) as cm:
                self.start(change_id=cid)
            self.assertIn("not approved", str(cm.exception))
        self.assertFalse(fakes.git(self.repo, "branch", "--list",
                                   "local-builds"))

    def test_an_approved_change_imports_and_starts_the_job(self):
        cid = changes.plan(self.conn, "install build", "steps", ["engine"],
                           actor="admin")
        changes.approve(self.conn, cid, "yes, install it", "sess", "admin")
        with self.as_bot():
            rc, calls, stdout = self.start(change_id=cid)
        self.assertEqual(rc, 0)
        commit = fakes.git(self.repo, "rev-parse", "refs/heads/local-builds")
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("--ref") + 1], commit)
        self.assertRegex(cmd[cmd.index("--label") + 1],
                         r"^local:[0-9a-f]{12}$")
        self.assertIn("unverified local build", stdout)

    def test_a_copied_install_is_sent_to_adopt(self):
        copy = fakes.copy_engine(self.tmp / "copy")
        with self.assertRaises(update.UpdateError) as cm:
            update.run_local(self.cfg, zip_path=self.zip, repo=copy,
                             conn=self.conn)
        self.assertIn("--adopt", str(cm.exception))

    def test_adopt_starts_the_adopt_script_with_the_unpacked_build(self):
        copy = fakes.copy_engine(self.tmp / "copy")
        calls = []

        def runner(cmd, check=True):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with redirect_stdout(io.StringIO()):
            rc = update.run_adopt(self.cfg, zip_path=self.zip, repo=copy,
                                  is_active_fn=lambda u: False, runner=runner,
                                  conn=self.conn)
        self.assertEqual(rc, 0)
        cmd = calls[0]
        self.assertTrue(any(str(c).endswith("adopt.sh") for c in cmd))
        source = Path(cmd[cmd.index("--source") + 1])
        self.assertTrue((source / "engine" / "update.sh").is_file())
        self.assertEqual(cmd[cmd.index("--old-engine") + 1],
                         str(copy / "engine"))
        with self.assertRaises(update.UpdateError):
            update.run_adopt(self.cfg, repo=self.repo, repo_url="x")  # a clone
        with self.assertRaises(update.UpdateError):
            update.run_adopt(self.cfg, repo=copy, repo_url="bad url;rm")


if __name__ == "__main__":
    unittest.main()
