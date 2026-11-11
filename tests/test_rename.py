"""The rename to AtoBots (0.1.0): old names keep working for one release,
an existing install is moved by the updater, and an updater too old to do
that is refused without leaving the server half renamed."""
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "tests"))

import fakes  # noqa: E402
from atobots import cli, config, doctor, names, update  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import init_db  # noqa: E402

OLD = "claudebot"
OLD_VAR = "CLAUDEBOT_HOME"
# Files that may still say the old name: the compatibility layer, the
# migration, the records of what happened, and the tests of all three.
ALLOWED = (
    "CHANGELOG.md", "UPDATING.md", "README.md", "docs/",
    "engine/atobots/names.py",
    "engine/atobots/__init__.py", "engine/lib/names.sh",
    "engine/migrations/0.1.0-rename.sh", "engine/systemd/",
    "tests/test_rename.py", "tests/test_update_finish.py",
)


class NameTests(unittest.TestCase):
    def test_the_old_name_is_only_where_it_is_meant_to_be(self):
        listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        hits = []
        for rel in listed.decode().split("\0"):
            path = ROOT / rel
            if not rel or not path.is_file() or rel.startswith(ALLOWED):
                continue
            if OLD in path.read_text(errors="ignore").casefold() \
                    or OLD in rel.casefold():
                hits.append(rel)
        self.assertEqual(hits, [])
        # The README says what the project used to be called, and no more.
        readme = (ROOT / "README.md").read_text()
        self.assertLessEqual(readme.casefold().count(OLD), 3)
        self.assertTrue(readme.startswith("# AtoBots"))

    def test_unit_files_keep_the_old_variable_for_company_scripts(self):
        for role in names.UNIT_ROLES:
            if role.endswith(".timer"):
                continue
            text = (ROOT / "engine" / "systemd" / names.unit(role)).read_text()
            self.assertIn(f"Environment={names.HOME_VAR}=@HOME@", text)
            self.assertIn(f"Environment={OLD_VAR}=@HOME@", text)
            self.assertNotIn("ClaudeBot", text)

    def test_child_processes_get_both_variables(self):
        self.assertEqual(names.home_env("/x"), {"ATOBOTS_HOME": "/x",
                                                OLD_VAR: "/x"})
        runner = (ROOT / "engine" / "atobots" / "runner.py").read_text()
        self.assertIn("names.home_env(cfg.home)", runner)

    def test_the_cb_command_is_unchanged(self):
        self.assertTrue((ROOT / "engine" / "bin" / "cb").is_file())
        self.assertEqual(cli.build_parser().prog, "cb")


class ConfigCompatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        patch = mock.patch.object(config, "ENV_FILE", self.tmp / "none.env")
        patch.start()
        self.addCleanup(patch.stop)

    def load(self, env):
        with mock.patch.dict(os.environ, env, clear=False):
            for var in ("ATOBOTS_HOME", OLD_VAR):
                if var not in env:
                    os.environ.pop(var, None)
            return Config.load()

    def test_new_name_wins_and_is_not_flagged(self):
        cfg = self.load({"ATOBOTS_HOME": str(self.home), OLD_VAR: "/elsewhere"})
        self.assertEqual(cfg.home, self.home)
        self.assertIsNone(cfg.old_name)
        self.assertEqual(doctor.old_name_findings(cfg, self.tmp), [])

    def test_old_variable_still_works_and_doctor_says_so(self):
        cfg = self.load({OLD_VAR: str(self.home)})
        self.assertEqual(cfg.home, self.home)
        self.assertEqual(cfg.old_name, OLD_VAR)
        found = doctor.old_name_findings(cfg, self.tmp)
        self.assertEqual([f.key for f in found], ["old-name"])
        self.assertIn(f"export ATOBOTS_HOME={self.home}", found[0].text)
        self.assertTrue(doctor.valid_finding_key("old-name"))
        self.assertIn("old-name", doctor.NO_WAKE)

    def test_old_env_file_still_works(self):
        old_file = self.tmp / "claudebot.env"
        old_file.write_text(f"{OLD_VAR}={self.home}\n")
        with mock.patch.object(names, "OLD_ENV_FILES", (str(old_file),)):
            cfg = self.load({})
        self.assertEqual(cfg.home, self.home)
        self.assertEqual(cfg.old_name, str(old_file))

    def test_nothing_set_is_still_an_error(self):
        with mock.patch.object(names, "OLD_ENV_FILES", ()):
            with self.assertRaises(RuntimeError) as cm:
                self.load({})
        self.assertIn("ATOBOTS_HOME is not set", str(cm.exception))

    def test_half_moved_units_are_a_finding(self):
        cfg = Config(self.home)
        units = self.tmp / "units"
        units.mkdir()
        (units / "claudebot-dispatcher.service").write_text("x")
        self.assertEqual(doctor.old_name_findings(cfg, units), [])  # not moved yet
        (units / "atobots-dispatcher.service").write_text("x")
        found = doctor.old_name_findings(cfg, units)
        self.assertEqual(len(found), 1)
        self.assertIn("Two dispatchers must never run", found[0].text)


class OldUpdaterGuardTests(unittest.TestCase):
    """`cb init` under an updater from before 0.0.3."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.fake = fakes.FakeSystemd(self.tmp)
        self.env = self.fake.env(self.home, "ATOBOTS_HOME")
        self.repo = fakes.make_engine_repo(self.tmp / "clone")
        self.before = fakes.git(self.repo, "rev-parse", "HEAD")
        (self.repo / "NEW").write_text("x")
        fakes.commit_all(self.repo, "new")
        fakes.git(self.repo, "checkout", "-q", "--detach", self.before)
        fakes.git(self.repo, "checkout", "-q", "--detach", "main")
        init_db(Config(self.home))
        conn = sqlite3.connect(self.home / "company.db")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES "
                     "('dispatcher.hold_until','2099-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()

    def guard(self, parent, **env):
        from atobots import update_finish
        full = dict(self.env, **env)
        full.pop("CB_UPD_KIND", None)
        full.update(env)
        with mock.patch.dict(os.environ, full, clear=True):
            return update.refuse_old_updater(
                self.home, repo=self.repo, parent_cmd=parent,
                system=update_finish.System(full))

    def test_no_old_units_means_no_refusal(self):
        self.assertIsNone(self.guard("bash engine/update.sh"))
        (self.fake.units / "atobots-dispatcher.service").write_text("x")
        (self.fake.units / "claudebot-dispatcher.service").write_text("x")
        self.assertIsNone(self.guard("bash engine/update.sh"))

    def test_the_second_half_of_a_real_update_is_let_through(self):
        (self.fake.units / "claudebot-dispatcher.service").write_text("x")
        self.assertIsNone(self.guard("bash update_finish.sh",
                                     CB_UPD_KIND="git"))

    def test_an_old_updater_is_refused_and_undone(self):
        (self.fake.units / "claudebot-dispatcher.service").write_text("x")
        message = self.guard("/bin/bash /srv/engine/update.sh --tag v9.9.9")
        self.assertIn("Update to 0.0.3 first", message)
        self.assertIn("Nothing was changed", message)
        self.assertEqual(fakes.git(self.repo, "rev-parse", "HEAD"), self.before)
        self.assertTrue(self.fake.has("active", "claudebot-dispatcher.service"))
        conn = sqlite3.connect(self.home / "company.db")
        held = conn.execute("SELECT 1 FROM meta WHERE key="
                            "'dispatcher.hold_until'").fetchone()
        conn.close()
        self.assertIsNone(held)

    def test_a_person_running_init_is_refused_but_nothing_is_touched(self):
        (self.fake.units / "claudebot-dispatcher.service").write_text("x")
        head = fakes.git(self.repo, "rev-parse", "HEAD")
        message = self.guard("-zsh")
        self.assertIn("Update to 0.0.3 first", message)
        self.assertNotIn("Nothing was changed", message)
        self.assertEqual(fakes.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.fake.calls(), [])

    def test_cb_init_prints_the_refusal(self):
        (self.fake.units / "claudebot-dispatcher.service").write_text("x")
        env = dict(self.env)
        env.pop("CB_UPD_KIND", None)
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(update, "_parent_command",
                                  return_value="-bash"), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cli.main(["init", "--company", "Test Co"])
        self.assertEqual(rc, 1)
        self.assertIn("claudebot-* services", err.getvalue())


def have_release(tag: str) -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify",
                        f"refs/tags/{tag}^{{commit}}"], capture_output=True)
    return r.returncode == 0


@unittest.skipUnless(have_release("v0.0.3"),
                     "needs the v0.0.3 tag (a full clone with tags)")
class RealUpdateAcrossTheRenameTests(unittest.TestCase):
    """The real 0.0.3 tree, installed under the old name, updated to this
    tree by its own updater: the path every live install takes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.fake = fakes.FakeSystemd(self.tmp)
        self.env = self.fake.env(self.home, OLD_VAR)
        self.bashrc = self.tmp / "bashrc"
        self.bashrc.write_text(f"export {OLD_VAR}={self.home}\n")
        self.env["CB_BASHRC"] = str(self.bashrc)
        self.upstream = self.tmp / "upstream"
        self.upstream.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", "v0.0.3",
                                  "engine"], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(self.upstream)],
                       input=archive.stdout, check=True)
        (self.upstream / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        fakes.init_repo(self.upstream)
        fakes.commit_all(self.upstream, "0.0.3")
        fakes.git(self.upstream, "tag", "v0.0.3")
        self.install = self.tmp / "install"
        fakes.clone(self.upstream, self.install)
        fakes.git(self.install, "checkout", "-q", "--detach", "v0.0.3")
        self.old_head = fakes.git(self.install, "rev-parse", "HEAD")
        self.cb("init", "--company", "Test Co")
        for role in names.UNIT_ROLES:
            (self.fake.units / f"{OLD}-{role}").write_text(f"OLD {role}\n")
        for role in ("dispatcher.service", "backup.timer", "doctor.timer"):
            self.fake.mark("enabled", f"{OLD}-{role}")
        self.fake.mark("active", f"{OLD}-dispatcher.service")

    def cb(self, *args):
        r = subprocess.run([str(self.install / "engine" / "bin" / "cb"), *args],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.strip()

    def release(self, bad_migration: bool = False):
        import shutil
        shutil.rmtree(self.upstream / "engine")
        fakes.copy_engine(self.upstream)
        if bad_migration:
            path = self.upstream / "engine" / "migrations" / "0.1.0-zz-bad.sh"
            path.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 4\n")
            path.chmod(0o755)
        fakes.commit_all(self.upstream, "0.1.0")
        fakes.git(self.upstream, "tag", "v0.1.0")

    def update(self):
        return subprocess.run(
            ["bash", str(self.install / "engine" / "update.sh"), "v0.1.0"],
            env=self.env, capture_output=True, text=True, timeout=300)

    def test_an_old_named_install_is_moved_to_the_new_name(self):
        self.assertEqual(self.cb("version"), "0.0.3")
        self.release()
        r = self.update()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.cb("version"), update.running_version())
        self.assertIn("Running migration 0.1.0-rename.sh", out)
        for role in names.UNIT_ROLES:
            self.assertFalse((self.fake.units / f"{OLD}-{role}").exists(), role)
            self.assertTrue((self.fake.units / names.unit(role)).is_file(), role)
        for role in ("dispatcher.service", "backup.timer", "doctor.timer"):
            self.assertFalse(self.fake.has("enabled", f"{OLD}-{role}"))
            self.assertTrue(self.fake.has("enabled", names.unit(role)))
        self.assertFalse(self.fake.has("enabled", names.unit("admin.service")))
        self.assertFalse(self.fake.has("active", f"{OLD}-dispatcher.service"))
        self.assertTrue(self.fake.has("active",
                                      names.unit("dispatcher.service")))
        self.assertEqual((self.fake.etc / "atobots.env").read_text(),
                         f"ATOBOTS_HOME={self.home}\n")
        text = self.bashrc.read_text()
        self.assertIn(f"export {OLD_VAR}={self.home}", text)   # kept
        self.assertIn(f"export ATOBOTS_HOME={self.home}", text)
        self.assertIn(f"{names.PRODUCT} updated: 0.0.3 ->", out)
        calls = self.fake.calls()
        self.assertLess(calls.index(f"stop {OLD}-dispatcher.service"),
                        calls.index(f"start {names.unit('dispatcher.service')}"))
        # the new name alone is now enough
        env = {k: v for k, v in self.env.items() if k != OLD_VAR}
        env["ATOBOTS_HOME"] = str(self.home)
        r = subprocess.run([str(self.install / "engine" / "bin" / "cb"),
                            "update", "--check"], env=env, capture_output=True,
                           text=True)
        self.assertIn("Running now: AtoBots", r.stdout)

    def test_a_failed_rename_leaves_the_old_install_running(self):
        self.release(bad_migration=True)
        r = self.update()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertEqual(fakes.git(self.install, "rev-parse", "HEAD"),
                         self.old_head)
        self.assertEqual(self.cb("version"), "0.0.3")
        for role in names.UNIT_ROLES:
            self.assertEqual((self.fake.units / f"{OLD}-{role}").read_text(),
                             f"OLD {role}\n")
            self.assertFalse((self.fake.units / names.unit(role)).exists())
        self.assertTrue(self.fake.has("enabled", f"{OLD}-dispatcher.service"))
        self.assertTrue(self.fake.has("active", f"{OLD}-dispatcher.service"))
        self.assertFalse(self.fake.has("enabled",
                                       names.unit("dispatcher.service")))
        self.assertIn("rolled back by itself", out)


if __name__ == "__main__":
    unittest.main()
