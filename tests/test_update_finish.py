"""The second half of an update (update_finish.Finisher) and whole scripted
runs of engine/update.sh and engine/adopt.sh, against a throwaway Git
repository, a fake company home and a fake systemctl."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "tests"))

import fakes  # noqa: E402
from atobots import admin_live, names, update, update_finish  # noqa: E402
from atobots.config import Config  # noqa: E402

PREFIX = names.UNIT_PREFIX
HOME_VAR = names.HOME_VAR
DISPATCHER = names.unit("dispatcher.service")
ADMIN = names.unit("admin.service")

MARK = '#!/usr/bin/env bash\nset -euo pipefail\n: > "$CB_HOME_DIR/migrated"\n'
FAIL = '#!/usr/bin/env bash\nset -euo pipefail\necho "migration says no"\nexit 3\n'


def backup_db(db: Path, dest: Path) -> None:
    src, dst = sqlite3.connect(db), sqlite3.connect(dest)
    src.backup(dst)
    dst.close()
    src.close()


def meta(db: Path, key: str):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.fake = fakes.FakeSystemd(self.tmp)
        self.env = self.fake.env(self.home, HOME_VAR)

    def cb(self, repo: Path, *args, check=True):
        r = subprocess.run([str(repo / "engine" / "bin" / "cb"), *args],
                           env=self.env, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise AssertionError(f"cb {args}: {r.stdout}{r.stderr}")
        return r

    def old_units(self, prefix: str = PREFIX, enabled=("dispatcher.service",
                                                       "backup.timer",
                                                       "doctor.timer")):
        for role in names.UNIT_ROLES:
            (self.fake.units / f"{prefix}-{role}").write_text(f"OLD {role}\n")
        for role in enabled:
            self.fake.mark("enabled", f"{prefix}-{role}")

    def run_script(self, script: Path, *args):
        return subprocess.run(["bash", str(script), *args], env=self.env,
                              capture_output=True, text=True, timeout=300)


class FinisherTests(Base):
    """The clone already sits on the new commit, as the first half left it."""

    def setUp(self):
        super().setUp()
        self.repo = fakes.make_engine_repo(self.tmp / "clone")
        self.old_ref = fakes.git(self.repo, "rev-parse", "HEAD")
        self.cb(self.repo, "init", "--company", "Test Co")
        self.db = self.home / "company.db"
        self.backup = self.home / "backups" / "pre-update-test.db"
        self.backup.parent.mkdir(exist_ok=True)
        backup_db(self.db, self.backup)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO meta(key, value) VALUES ('after','1')")
        conn.commit()
        conn.close()
        self.old_units()
        self.notices = []
        self.lines = []
        self.now = [1000.0]

    def new_commit(self, migration: str | None = None, body: str = MARK):
        if migration:
            path = self.repo / "engine" / "migrations" / migration
            path.write_text(body)
            path.chmod(0o755)
        (self.repo / "engine" / "CHANGED").write_text("new\n")
        return fakes.commit_all(self.repo, "new version")

    def finisher(self, **state) -> update_finish.Finisher:
        st = update_finish.State(
            kind="git", old_version="0.0.1", old_ref=self.old_ref,
            backup=self.backup, label="tag:v0.0.2", old_prefix=PREFIX,
            stopped_at=990.0)
        for key, value in state.items():
            setattr(st, key, value)

        def sleep(seconds):
            self.now[0] += seconds

        return update_finish.Finisher(
            Config(self.home), st, system=update_finish.System(self.env),
            engine=self.repo / "engine", sleep=sleep,
            clock=lambda: self.now[0], notify=self.notices.append,
            out=self.lines.append, env=self.env)

    def head(self) -> str:
        return fakes.git(self.repo, "rev-parse", "HEAD")

    def log(self) -> str:
        return "\n".join(self.lines)

    def test_success_applies_migrations_units_and_reports(self):
        new = self.new_commit(f"{update.base_version(update.running_version())}-mark.sh")
        rc = self.finisher().run()
        self.assertEqual(rc, 0, self.log())
        self.assertEqual(self.head(), new)
        self.assertTrue((self.home / "migrated").exists())
        self.assertTrue(self.fake.has("active", DISPATCHER))
        unit = (self.fake.units / DISPATCHER).read_text()
        self.assertIn("ExecStart=", unit)          # re-rendered, not "OLD"
        self.assertNotIn("@ENGINE@", unit)
        self.assertEqual(meta(self.db, update.SOURCE_KEY), "tag:v0.0.2")
        self.assertGreaterEqual(int(meta(self.db, update.OFFLINE_KEY)), 1)
        self.assertIn("-mark.sh", meta(self.db, "migrations.applied"))
        self.assertEqual(meta(self.db, "after"), "1")   # nothing rolled back
        self.assertIsNone(meta(self.db, "dispatcher.hold_until"))
        wrapper = self.fake.usr_bin / "cb"
        self.assertFalse(wrapper.is_symlink())
        self.assertIn("do not run as root", wrapper.read_text())
        self.assertEqual((self.fake.etc / "atobots.env").read_text()
                         if PREFIX == "atobots" else "", f"{HOME_VAR}={self.home}\n"
                         if PREFIX == "atobots" else "")
        # Admin is not enabled here, so the job itself reports the result.
        self.assertEqual(len(self.notices), 1)
        self.assertIn("0.0.1 ->", self.notices[0])
        self.assertIn("The services are running", self.notices[0])
        if "existing issue" in self.notices[0]:
            self.assertIn("Not caused by the update", self.notices[0])
        calls = self.fake.calls()
        stop_timers = calls.index(f"stop {PREFIX}-backup.timer")
        start = calls.index(f"start {DISPATCHER}")
        self.assertLess(stop_timers, start)
        self.assertIn(f"start {PREFIX}-backup.timer", calls)

    def test_a_running_backup_job_is_waited_for_then_stopped(self):
        self.new_commit()
        self.fake.mark("active", f"{PREFIX}-backup.service")
        rc = self.finisher().run()
        self.assertEqual(rc, 0, self.log())
        self.assertIn("Waiting for", self.log())
        self.assertIn(f"stop {PREFIX}-backup.service", self.fake.calls())

    def test_failed_migration_rolls_back_code_database_and_units(self):
        self.new_commit(f"{update.base_version(update.running_version())}-bad.sh", FAIL)
        rc = self.finisher().run()
        self.assertEqual(rc, update_finish.ROLLED_BACK, self.log())
        self.assertEqual(self.head(), self.old_ref)
        self.assertIsNone(meta(self.db, "after"))      # the backup is back
        self.assertEqual((self.fake.units / DISPATCHER).read_text(),
                         "OLD dispatcher.service\n")
        self.assertTrue(self.fake.has("active", DISPATCHER))
        self.assertTrue(self.fake.has("enabled", DISPATCHER))
        self.assertEqual(len(self.notices), 1)
        self.assertIn("rolled back by itself", self.notices[0])
        self.assertIn("-bad.sh failed", self.notices[0])
        self.assertEqual(self.log().count("Rolling back"), 1)

    def test_a_service_that_will_not_start_rolls_back(self):
        self.new_commit()
        self.fake.mark("enabled", ADMIN)
        self.fake.mark("fail-start", ADMIN)
        rc = self.finisher().run()
        self.assertEqual(rc, update_finish.ROLLED_BACK, self.log())
        self.assertEqual(self.head(), self.old_ref)
        self.assertFalse((self.home / ".admin-live"
                          / "update-pending.json").exists())
        self.assertIn("could not be restarted", self.notices[0])

    def test_a_dispatcher_that_never_comes_up_and_a_failed_rollback(self):
        """The rollback is tried once. When it fails too, the job stops and
        says so, with the manual steps; it does not loop."""
        self.new_commit()
        self.fake.mark("no-active", DISPATCHER)
        rc = self.finisher().run()
        self.assertEqual(rc, update_finish.ROLLBACK_FAILED, self.log())
        self.assertEqual(self.log().count("Rolling back"), 1)
        self.assertIn("by hand", self.log())
        self.assertEqual(len(self.notices), 1)
        self.assertIn("rollback failed too", self.notices[0])
        self.assertIn("may be down", self.notices[0])

    def test_admin_up_but_waiting_on_the_trust_question(self):
        self.new_commit()
        self.fake.mark("enabled", ADMIN)
        with mock.patch.object(admin_live, "waiting_on_trust",
                               return_value=True):
            rc = self.finisher().run()
        self.assertEqual(rc, 0, self.log())           # not a rollback
        self.assertEqual(self.head() != self.old_ref, True)
        text = self.notices[-1]
        self.assertIn("folder-trust", text)
        self.assertIn("tmux attach -t admin-live", text)
        self.assertIn("Yes, I trust this folder", text)
        self.assertFalse((self.home / ".admin-live"
                          / "update-pending.json").exists())
        self.assertIn(f"restart {ADMIN}", self.fake.calls())

    def test_admin_that_confirms_gets_no_duplicate_success_notice(self):
        self.new_commit()
        self.fake.mark("enabled", ADMIN)
        f = self.finisher()
        real_sleep = f.sleep

        def sleep(seconds):          # Admin acks while the job waits
            marker = self.home / ".admin-live" / "update-pending.json"
            if marker.exists():
                marker.unlink()
            real_sleep(seconds)

        f.sleep = sleep
        rc = f.run()
        self.assertEqual(rc, 0, self.log())
        for text in self.notices:
            self.assertNotIn("The services are running", text)
            self.assertIn("Not caused by the update", text)

    def test_renamed_units_replace_the_old_ones_and_come_back_on_rollback(self):
        for p in self.fake.units.iterdir():
            p.unlink()
        for p in (self.fake.dir / "enabled").iterdir():
            p.unlink()
        self.old_units(prefix="oldname")
        self.new_commit()
        rc = self.finisher(old_prefix="oldname").run()
        self.assertEqual(rc, 0, self.log())
        self.assertFalse((self.fake.units / "oldname-dispatcher.service").exists())
        self.assertFalse(self.fake.has("enabled", "oldname-dispatcher.service"))
        self.assertTrue(self.fake.has("enabled", DISPATCHER))
        self.assertTrue(self.fake.has("enabled", f"{PREFIX}-backup.timer"))
        self.assertFalse(self.fake.has("enabled", ADMIN))   # it was not before
        calls = self.fake.calls()
        self.assertLess(calls.index("stop oldname-dispatcher.service"),
                        calls.index(f"start {DISPATCHER}"))

    def test_renamed_units_rollback(self):
        for p in self.fake.units.iterdir():
            p.unlink()
        for p in (self.fake.dir / "enabled").iterdir():
            p.unlink()
        self.old_units(prefix="oldname")
        self.new_commit(f"{update.base_version(update.running_version())}-bad.sh", FAIL)
        f = self.finisher(old_prefix="oldname")
        # fail AFTER the units were switched, the hardest moment
        f.start = mock.Mock(side_effect=update_finish.Failed("late failure"))
        f.run_migrations = lambda: None
        rc = f.run()
        self.assertEqual(rc, update_finish.ROLLED_BACK, self.log())
        self.assertEqual((self.fake.units / "oldname-dispatcher.service"
                          ).read_text(), "OLD dispatcher.service\n")
        self.assertFalse((self.fake.units / DISPATCHER).exists())
        self.assertTrue(self.fake.has("enabled", "oldname-dispatcher.service"))
        self.assertFalse(self.fake.has("enabled", DISPATCHER))
        self.assertTrue(self.fake.has("active", "oldname-dispatcher.service"))

    def test_adopt_rollback_swaps_the_folders_back(self):
        kept = self.tmp / "clone.old"
        fakes.copy_engine(kept)
        (kept / "OLD_FOLDER").write_text("old\n")
        self.new_commit(f"{update.base_version(update.running_version())}-bad.sh", FAIL)
        rc = self.finisher(kind="adopt", old_ref="", old_folder=kept).run()
        self.assertEqual(rc, update_finish.ROLLED_BACK, self.log())
        self.assertTrue((self.repo / "OLD_FOLDER").exists())
        self.assertFalse(kept.exists())
        failed = [p for p in self.tmp.iterdir()
                  if p.name.startswith("clone.failed-")]
        self.assertEqual(len(failed), 1)
        self.assertTrue((failed[0] / ".git").is_dir())

    def test_old_backups_are_pruned(self):
        self.new_commit()
        folder = self.home / "backups"
        for i in range(8):
            p = folder / f"pre-update-0.0.{i}.db"
            p.write_text("x")
            os.utime(p, (i, i))
        self.finisher().run()
        left = sorted(p.name for p in folder.glob("pre-update-*.db"))
        self.assertEqual(len(left), update_finish.KEEP_BACKUPS)
        self.assertIn("pre-update-test.db", left)

    def test_state_from_env(self):
        st = update_finish.State.from_env({
            "CB_UPD_KIND": "adopt", "CB_UPD_OLD_VERSION": "0.0.2",
            "CB_UPD_BACKUP": "/x/b.db", "CB_UPD_OLD_FOLDER": "relative",
            "CB_UPD_LEGACY": "1", "CB_UPD_STOPPED_AT": "nonsense",
            "CB_UPD_SOMETHING_NEW": "ignored"})
        self.assertEqual(st.kind, "adopt")
        self.assertEqual(st.backup, Path("/x/b.db"))
        self.assertIsNone(st.old_folder)
        self.assertTrue(st.legacy)
        self.assertIsNone(st.stopped_at)
        self.assertEqual(update_finish.State.from_env({}).kind, "git")


class ScriptedRunTests(Base):
    """engine/update.sh and engine/adopt.sh from start to finish."""

    def setUp(self):
        super().setUp()
        self.upstream = fakes.make_engine_repo(self.tmp / "upstream",
                                               tag="v0.0.2")
        self.install = self.tmp / "install"
        fakes.clone(self.upstream, self.install)
        fakes.git(self.install, "checkout", "-q", "--detach", "v0.0.2")
        self.cb(self.install, "init", "--company", "Test Co")
        self.old_units()
        self.old_version = self.cb(self.install, "version").stdout.strip()
        self.old_head = fakes.git(self.install, "rev-parse", "HEAD")

    def release(self, version="99.0.0", migration=None, body=MARK,
                break_cb=False):
        fakes.set_version(self.upstream, version)
        if migration:
            path = self.upstream / "engine" / "migrations" / migration
            path.write_text(body)
            path.chmod(0o755)
        if break_cb:
            cb = self.upstream / "engine" / "bin" / "cb"
            cb.write_text("#!/bin/sh\nexit 7\n")
        fakes.commit_all(self.upstream, f"release {version}")
        fakes.git(self.upstream, "tag", f"v{version}")

    def version(self) -> str:
        return self.cb(self.install, "version").stdout.strip()

    def test_update_to_the_newest_tag(self):
        self.release(migration="99.0.0-mark.sh")
        r = self.run_script(self.install / "engine" / "update.sh")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.version(), "99.0.0")
        self.assertTrue((self.home / "migrated").exists())
        self.assertIn("Backed up the database", out)
        self.assertIn(f"{names.PRODUCT} updated: {self.old_version} -> 99.0.0 "
                      "(tag:v99.0.0)", out)
        self.assertTrue(self.fake.has("active", DISPATCHER))
        calls = self.fake.calls()
        self.assertLess(calls.index(f"stop {DISPATCHER}"),
                        calls.index(f"start {DISPATCHER}"))
        self.assertEqual(len(list((self.home / "backups"
                                   ).glob("pre-update-*.db"))), 1)
        self.assertEqual(meta(self.home / "company.db", update.SOURCE_KEY),
                         "tag:v99.0.0")
        # a second run has nothing to do
        again = self.run_script(self.install / "engine" / "update.sh")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Nothing to do", again.stdout)

    def test_failing_migration_rolls_everything_back(self):
        self.release(migration="99.0.0-bad.sh", body=FAIL)
        r = self.run_script(self.install / "engine" / "update.sh", "v99.0.0")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertEqual(fakes.git(self.install, "rev-parse", "HEAD"),
                         self.old_head)
        self.assertEqual(self.version(), self.old_version)
        self.assertTrue(self.fake.has("active", DISPATCHER))
        self.assertEqual((self.fake.units / DISPATCHER).read_text(),
                         "OLD dispatcher.service\n")
        self.assertIn("migration says no", out)
        self.assertIn("rolled back by itself", out)
        self.assertIsNone(meta(self.home / "company.db",
                               "dispatcher.hold_until"))

    def test_a_new_version_that_cannot_start_is_undone_by_the_old_one(self):
        self.release(break_cb=True)
        r = self.run_script(self.install / "engine" / "update.sh", "v99.0.0")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertEqual(fakes.git(self.install, "rev-parse", "HEAD"),
                         self.old_head)
        self.assertEqual(self.version(), self.old_version)
        self.assertTrue(self.fake.has("active", DISPATCHER))
        self.assertIn("could not finish the update", out)

    def test_refusals_change_nothing(self):
        self.release()
        (self.install / "engine" / "update.sh").write_text(
            (self.install / "engine" / "update.sh").read_text() + "\n# edit\n")
        r = self.run_script(self.install / "engine" / "update.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("local changes", r.stderr)
        fakes.git(self.install, "checkout", "-q", "--", ".")
        r = self.run_script(self.install / "engine" / "update.sh", "v1.2.3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("there is no tag v1.2.3", r.stderr)
        self.assertIn("Nothing was changed", r.stderr)
        self.assertEqual(self.fake.calls(), [])
        self.assertEqual(list((self.home / "backups").glob("*.db"))
                         if (self.home / "backups").exists() else [], [])

    def test_active_runs_stop_the_update_unless_forced(self):
        self.release()
        conn = sqlite3.connect(self.home / "company.db")
        conn.execute("INSERT INTO runs(bot, session_id, lane, started_at)"
                     " VALUES ('x','s','work','2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        r = self.run_script(self.install / "engine" / "update.sh", "v99.0.0")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("A bot run is still active", out)
        self.assertEqual(self.version(), self.old_version)
        self.assertNotIn(f"stop {DISPATCHER}", self.fake.calls())
        self.assertIsNone(meta(self.home / "company.db",
                               "dispatcher.hold_until"))
        r = self.run_script(self.install / "engine" / "update.sh", "v99.0.0",
                            "--force")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.version(), "99.0.0")

    def test_a_copied_install_is_told_to_adopt(self):
        copy = fakes.copy_engine(self.tmp / "copy")
        r = self.run_script(copy / "engine" / "update.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cb update --adopt", r.stderr)
        self.assertEqual(self.fake.calls(), [])

    def test_local_build_ref_must_come_from_the_import(self):
        self.release()
        fakes.git(self.install, "fetch", "-q", "--tags", "origin")
        stray = fakes.git(self.install, "rev-parse", "v99.0.0^{commit}")
        r = self.run_script(self.install / "engine" / "update.sh", "--ref",
                            stray, "--label", "local:" + "a" * 12)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a local build", r.stderr)

    def test_local_build_from_a_zip_end_to_end(self):
        newer = fakes.copy_engine(self.tmp / "newer")
        fakes.set_version(newer, "99.0.0")
        with update.Unpacked(self.home, dir_path=newer) as build:
            commit = update.import_local(self.install, build.root, build.sha,
                                         build.name)
            sha = build.sha
        r = self.run_script(self.install / "engine" / "update.sh", "--ref",
                            commit, "--label", f"local:{sha[:12]}")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.version(), f"99.0.0+local.{sha[:7]}")
        self.assertIn("unverified local build", out)
        self.assertEqual(meta(self.home / "company.db", update.SOURCE_KEY),
                         f"local:{sha[:12]}")

    def test_adopt_turns_a_copy_into_a_clone_and_keeps_the_old_folder(self):
        copy = fakes.copy_engine(self.tmp / "copied")
        (copy / "ONLY_IN_OLD").write_text("x")
        (self.fake.user_bin / "cb").unlink(missing_ok=True)
        (self.fake.user_bin / "cb").symlink_to(copy / "engine" / "bin" / "cb")
        # the old wrapper is a symlink into the engine, as old installs had
        (self.fake.usr_bin / "cb").symlink_to(copy / "engine" / "bin" / "cb")
        real_cb = (copy / "engine" / "bin" / "cb").read_text()
        newer = fakes.copy_engine(self.tmp / "newer")
        fakes.set_version(newer, "99.0.0")
        r = self.run_script(newer / "engine" / "adopt.sh", "--source",
                            str(newer))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertTrue((copy / ".git").is_dir())
        self.assertFalse((copy / "ONLY_IN_OLD").exists())
        kept = self.tmp / "copied.old"
        self.assertTrue((kept / "ONLY_IN_OLD").exists())
        self.assertEqual((kept / "engine" / "bin" / "cb").read_text(), real_cb)
        self.assertRegex(self.cb(copy, "version").stdout.strip(),
                         r"^99\.0\.0\+local\.[0-9a-f]{7}$")
        self.assertEqual(fakes.git(copy, "status", "--porcelain"), "")
        self.assertTrue(fakes.git(copy, "rev-parse", "refs/heads/local-builds"))
        self.assertFalse((self.fake.usr_bin / "cb").is_symlink())
        self.assertIn("is kept at", out)
        self.assertTrue(self.fake.has("active", DISPATCHER))
        self.assertEqual([p.name for p in (self.home / ".update").iterdir()],
                         [".gitignore"])
        # now it is a clone, adopting again is refused
        again = self.run_script(newer / "engine" / "adopt.sh", "--source",
                                str(newer), "--old-engine",
                                str(copy / "engine"))
        self.assertEqual(again.returncode, 1)
        self.assertIn("already a Git clone", again.stderr)

    def test_adopt_from_a_repository_tag(self):
        copy = fakes.copy_engine(self.tmp / "copied")
        self.release()
        r = self.run_script(copy / "engine" / "adopt.sh", "--old-engine",
                            str(copy / "engine"), "--repo", str(self.upstream))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.cb(copy, "version").stdout.strip(), "99.0.0")
        self.assertEqual(fakes.git(copy, "config", "--get",
                                   "remote.origin.url"), str(self.upstream))
        self.assertEqual(meta(self.home / "company.db", update.SOURCE_KEY),
                         "tag:v99.0.0")

    def test_adopt_failure_puts_the_old_folder_back(self):
        copy = fakes.copy_engine(self.tmp / "copied")
        (copy / "ONLY_IN_OLD").write_text("x")
        self.release(migration="99.0.0-bad.sh", body=FAIL)
        r = self.run_script(copy / "engine" / "adopt.sh", "--old-engine",
                            str(copy / "engine"), "--repo", str(self.upstream))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertTrue((copy / "ONLY_IN_OLD").exists())
        self.assertFalse((copy / ".git").exists())
        self.assertFalse((self.tmp / "copied.old").exists())
        self.assertTrue(self.fake.has("active", DISPATCHER))

    def test_an_updater_from_before_this_release_hands_over(self):
        """Old updaters source lib/units.sh after the checkout. The new
        version takes over there, and the old script never continues."""
        self.release(migration="99.0.0-mark.sh")
        fakes.git(self.install, "fetch", "-q", "--tags", "origin")
        backup = self.home / "backups" / "pre-update-legacy.db"
        backup.parent.mkdir(exist_ok=True)
        backup_db(self.home / "company.db", backup)
        legacy = self.tmp / "legacy-update.sh"
        legacy.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
ENGINE_DIR={self.install}/engine
REPO_DIR={self.install}
export {HOME_VAR}={self.home}
OLD_VERSION={self.old_version}
OLD_REF={self.old_head}
BACKUP={backup}
TAG=v99.0.0
FORCE=0
trap 'echo LEGACY-ERR-TRAP' ERR
CHANGED=1
git -C "$REPO_DIR" checkout --quiet --detach "refs/tags/$TAG"
source "$ENGINE_DIR/lib/units.sh"
echo LEGACY-CONTINUED
''')
        r = self.run_script(legacy)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("LEGACY-CONTINUED", out)
        self.assertNotIn("LEGACY-ERR-TRAP", out)
        self.assertIn("Handing the rest of this update to the new version", out)
        self.assertEqual(self.version(), "99.0.0")
        self.assertTrue((self.home / "migrated").exists())
        self.assertTrue(self.fake.has("active", DISPATCHER))

    def test_install_and_units_never_write_through_a_symlink(self):
        """An old install had root-owned symlinks into the clone. Installing
        over them must leave the clone clean, and a repeat run changes
        nothing."""
        engine = self.install / "engine"
        (self.fake.usr_bin / "cb").symlink_to(engine / "bin" / "cb")
        (self.fake.units / DISPATCHER).unlink()
        (self.fake.units / DISPATCHER).symlink_to(
            engine / "systemd" / DISPATCHER)
        env_file = self.fake.etc / Path(names.ENV_FILE).name
        env_file.symlink_to(engine / "install.sh")
        script = ('set -euo pipefail; source "$1/lib/cb_wrapper.sh"; '
                  'source "$1/lib/units.sh"; install_links "$1" "$2"; '
                  'install_units "$2" "$1" tester 1200M')
        for _ in range(2):
            r = subprocess.run(["bash", "-c", script, "t", str(engine),
                                str(self.home)], env=self.env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(fakes.git(self.install, "status", "--porcelain"),
                             "")
            for path in (self.fake.usr_bin / "cb", self.fake.units / DISPATCHER,
                         env_file):
                self.assertFalse(path.is_symlink(), path)
        self.assertEqual(env_file.read_text(), f"{HOME_VAR}={self.home}\n")
        self.assertIn("User=tester", (self.fake.units / DISPATCHER).read_text())


if __name__ == "__main__":
    unittest.main()
