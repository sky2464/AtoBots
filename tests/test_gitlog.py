import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from atobots import gitlog  # noqa: E402
from atobots.config import Config  # noqa: E402


def git(cfg, *args):
    return subprocess.run(["git", *args], cwd=cfg.home, capture_output=True,
                          text=True, check=True).stdout


class GitlogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        gitlog.ensure_repo(self.cfg)

    def test_ensure_repo_is_idempotent_and_ignores(self):
        gitlog.ensure_repo(self.cfg)
        ig = (self.cfg.home / ".gitignore").read_text().split()
        for pat in (".secrets/", "*.db", "*.db-wal", "*.db-shm", "archive/",
                    "media/", ".claude/projects/", "backups/", ".alerts/"):
            self.assertIn(pat, ig)
        self.assertEqual(git(self.cfg, "config", "--local", "user.name").strip(),
                         "atobots")

    def test_backups_and_alerts_not_committed(self):
        self.write("backups/old.sql", "dump")
        self.write(".alerts/note.txt", "local")
        self.write("work/T-0001/keep.md")
        gitlog.commit_run(self.cfg, "admin", ["T-0001"], 1)
        gitlog.commit_sweep(self.cfg, "s")
        files = git(self.cfg, "ls-files").split()
        self.assertIn("work/T-0001/keep.md", files)
        self.assertNotIn("backups/old.sql", files)
        self.assertNotIn(".alerts/note.txt", files)
        for f in files:
            self.assertFalse(f.startswith("backups/"), f)
            self.assertFalse(f.startswith(".alerts/"), f)

    def write(self, rel, text="x"):
        p = self.cfg.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_commit_names_bot_and_item(self):
        self.write("work/T-0001/a.md")
        sha = gitlog.commit_run(self.cfg, "backend", ["T-0001"], 7)
        self.assertEqual(sha, git(self.cfg, "rev-parse", "HEAD").strip())
        log = git(self.cfg, "log", "-1", "--format=%an <%ae>%n%cn%n%B")
        self.assertIn("backend <backend@atobots.local>", log)
        self.assertIn("T-0001", log)
        self.assertIn("run 7", log)

    def test_commits_own_folders(self):
        self.write("bots/admin/MEMORY.md")
        self.write("knowledge/admin/n.md")
        self.write("work/T-0002/x.md")
        self.write("work/T-0003/y.md")
        self.write("shared.md")
        gitlog.commit_run(self.cfg, "admin", ["T-0002"], 1)
        files = git(self.cfg, "ls-files").split()
        for f in ("bots/admin/MEMORY.md", "knowledge/admin/n.md",
                  "work/T-0002/x.md"):
            self.assertIn(f, files)
        self.assertNotIn("work/T-0003/y.md", files)
        self.assertNotIn("shared.md", files)

    def test_deletion_is_committed(self):
        self.write("work/T-0001/a.md")
        gitlog.commit_run(self.cfg, "admin", ["T-0001"], 1)
        (self.cfg.home / "work/T-0001/a.md").unlink()
        self.assertIsNotNone(gitlog.commit_run(self.cfg, "admin", ["T-0001"], 2))
        self.assertNotIn("work/T-0001/a.md", git(self.cfg, "ls-files"))

    def test_no_changes_no_commit(self):
        self.write("bots/admin/a.md")
        self.assertIsNotNone(gitlog.commit_run(self.cfg, "admin", [], 1))
        self.assertIsNone(gitlog.commit_run(self.cfg, "admin", [], 2))
        self.assertIsNone(gitlog.commit_run(self.cfg, "nobody", ["T-9999"], 3))
        self.assertEqual(len(git(self.cfg, "log", "--oneline").splitlines()), 1)

    def test_in_progress_work_of_other_bot_not_committed(self):
        self.write("bots/a/mine.md")
        self.write("bots/b/wip.md")
        gitlog.commit_run(self.cfg, "a", [], 1)
        self.assertEqual(git(self.cfg, "ls-files").split(), ["bots/a/mine.md"])
        self.assertIn("?? bots/b/", git(self.cfg, "status", "--porcelain"))
        self.assertIsNotNone(gitlog.commit_run(self.cfg, "b", [], 2))
        self.assertEqual(git(self.cfg, "log", "-1", "--format=%an",
                             "--", "bots/b/wip.md").strip(), "b")

    def test_sweep_commits_remaining_as_system(self):
        self.write("bots/a/mine.md")
        self.write("shared.md")
        self.write(".secrets/token", "s3cret")
        gitlog.commit_run(self.cfg, "a", [], 1)
        sha = gitlog.commit_sweep(self.cfg, "snapshot")
        self.assertEqual(sha, git(self.cfg, "rev-parse", "HEAD").strip())
        log = git(self.cfg, "log", "-1", "--format=%an <%ae>|%s")
        self.assertEqual(log.strip(), "system <system@atobots.local>|snapshot")
        files = git(self.cfg, "ls-files").split()
        self.assertIn("shared.md", files)
        self.assertNotIn(".secrets/token", files)
        self.assertIsNone(gitlog.commit_sweep(self.cfg, "again"))

    def test_secrets_never_staged(self):
        self.write(".secrets/token", "s3cret")
        self.write("company.db", "db")
        self.write("archive/t.jsonl")
        self.write("work/T-0001/keep.md")
        gitlog.commit_run(self.cfg, "admin", ["T-0001"], 1)
        gitlog.commit_sweep(self.cfg, "s")
        files = git(self.cfg, "ls-files").split()
        self.assertIn("work/T-0001/keep.md", files)
        for f in files:
            self.assertFalse(f.startswith((".secrets", "archive")), f)
            self.assertNotEqual(f, "company.db")

    def test_secrets_not_staged_even_if_gitignore_edited(self):
        self.write(".gitignore", "")
        self.write(".secrets/token", "s3cret")
        self.write("work/T-0001/keep.md")
        gitlog.commit_run(self.cfg, "admin", ["T-0001"], 1)
        gitlog.commit_sweep(self.cfg, "s")
        self.assertNotIn(".secrets/token", git(self.cfg, "ls-files"))

    def test_parallel_commits_no_conflict(self):
        code = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(ENGINE)!r})\n"
            "from pathlib import Path\n"
            "from atobots import gitlog\n"
            "from atobots.config import Config\n"
            f"cfg = Config(Path({str(self.cfg.home)!r}))\n"
            "bot, item = sys.argv[1], sys.argv[2]\n"
            "d = cfg.home / 'work' / item\n"
            "d.mkdir(parents=True)\n"
            "(d / 'f.md').write_text(bot)\n"
            "while not all((cfg.home / 'work' / i / 'f.md').exists()\n"
            "              for i in ('T-0001', 'T-0002')):\n"
            "    time.sleep(0.01)\n"
            "print(gitlog.commit_run(cfg, bot, [item], int(item[-1])))\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, b, i],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
                 for b, i in (("a", "T-0001"), ("b", "T-0002"))]
        outs = [p.communicate(timeout=60) for p in procs]
        for p, (_, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(git(self.cfg, "log", "--merges", "--oneline"), "")
        self.assertEqual(git(self.cfg, "status", "--porcelain"), "?? .gitignore\n")
        self.assertEqual(len(git(self.cfg, "rev-list", "HEAD").split()), 2)
        for bot, item, run in (("a", "T-0001", "1"), ("b", "T-0002", "2")):
            out = git(self.cfg, "log", "--format=%an|%s", "--",
                      f"work/{item}/f.md").strip()
            self.assertEqual(out, f"{bot}|{bot}: {item} (run {run})")


if __name__ == "__main__":
    unittest.main()
