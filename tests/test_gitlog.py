import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import gitlog  # noqa: E402
from claudebot.config import Config  # noqa: E402


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
                    "media/", ".claude/projects/"):
            self.assertIn(pat, ig)
        self.assertEqual(git(self.cfg, "config", "--local", "user.name").strip(),
                         "claudebot")

    def test_commit_names_bot_and_item(self):
        (self.cfg.home / "work" / "T-0001").mkdir(parents=True)
        (self.cfg.home / "work" / "T-0001" / "a.md").write_text("x")
        sha = gitlog.commit_run(self.cfg, "backend", ["T-0001"], 7)
        self.assertEqual(sha, git(self.cfg, "rev-parse", "HEAD").strip())
        log = git(self.cfg, "log", "-1", "--format=%an <%ae>%n%cn%n%B")
        self.assertIn("backend <backend@claudebot.local>", log)
        self.assertIn("T-0001", log)
        self.assertIn("7", log)

    def test_no_changes_no_commit(self):
        (self.cfg.home / "a.md").write_text("x")
        self.assertIsNotNone(gitlog.commit_run(self.cfg, "admin", [], 1))
        self.assertIsNone(gitlog.commit_run(self.cfg, "admin", [], 2))
        self.assertEqual(len(git(self.cfg, "log", "--oneline").split("\n")) - 1, 1)

    def test_secrets_never_staged(self):
        (self.cfg.home / ".secrets").mkdir()
        (self.cfg.home / ".secrets" / "token").write_text("s3cret")
        (self.cfg.home / "company.db").write_text("db")
        (self.cfg.home / "archive").mkdir()
        (self.cfg.home / "archive" / "t.jsonl").write_text("x")
        (self.cfg.home / "keep.md").write_text("k")
        gitlog.commit_run(self.cfg, "admin", [], 1)
        files = git(self.cfg, "ls-files").split()
        self.assertIn("keep.md", files)
        for f in files:
            self.assertFalse(f.startswith((".secrets", "archive")), f)
            self.assertNotEqual(f, "company.db")

    def test_secrets_not_staged_even_if_gitignore_edited(self):
        (self.cfg.home / ".gitignore").write_text("")
        (self.cfg.home / ".secrets").mkdir()
        (self.cfg.home / ".secrets" / "token").write_text("s3cret")
        (self.cfg.home / "keep.md").write_text("k")
        gitlog.commit_run(self.cfg, "admin", [], 1)
        self.assertNotIn(".secrets/token", git(self.cfg, "ls-files"))

    def test_parallel_commits_no_conflict(self):
        # Both files exist before either process commits, so "stage all
        # changes" lets the first committer take both and the second find
        # nothing. What must hold: no git error, linear history, no change lost.
        code = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(ENGINE)!r})\n"
            "from pathlib import Path\n"
            "from claudebot import gitlog\n"
            "from claudebot.config import Config\n"
            f"cfg = Config(Path({str(self.cfg.home)!r}))\n"
            "bot = sys.argv[1]\n"
            "(cfg.home / (bot + '.md')).write_text(bot)\n"
            "while not ((cfg.home / 'a.md').exists() and (cfg.home / 'b.md').exists()):\n"
            "    time.sleep(0.01)\n"
            "print(gitlog.commit_run(cfg, bot, ['T-0001'], 1))\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, b],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for b in ("a", "b")]
        outs = [p.communicate(timeout=60) for p in procs]
        for p, (_, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, err)
        shas = [o.strip() for o, _ in outs if o.strip() != "None"]
        self.assertGreaterEqual(len(shas), 1)
        self.assertEqual(git(self.cfg, "log", "--merges", "--oneline"), "")
        self.assertEqual(git(self.cfg, "status", "--porcelain"), "")
        files = git(self.cfg, "ls-files").split()
        self.assertIn("a.md", files)
        self.assertIn("b.md", files)
        self.assertEqual(len(git(self.cfg, "rev-list", "HEAD").split()),
                         len(shas))


if __name__ == "__main__":
    unittest.main()
