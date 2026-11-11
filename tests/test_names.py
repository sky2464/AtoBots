"""The product name lives in one place, and nothing else spells an old one."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from atobots import config, names  # noqa: E402

# Built from pieces so this file does not contain the word it forbids.
OLD = "claude" + "bot"


class NameTests(unittest.TestCase):
    def test_no_tracked_file_uses_an_old_name(self):
        listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        hits = []
        for rel in listed.decode().split("\0"):
            path = ROOT / rel
            if not rel or not path.is_file():
                continue
            flat = path.read_text(errors="ignore").casefold() + rel.casefold()
            flat = flat.replace("_", "").replace("-", "").replace(" ", "")
            if OLD in flat:
                hits.append(rel)
        self.assertEqual(hits, [])

    def test_children_get_only_the_current_variable(self):
        self.assertEqual(names.home_env("/x"), {names.HOME_VAR: "/x"})

    def test_home_comes_from_the_environment_first(self):
        self.assertEqual(config.find_home({names.HOME_VAR: "/h"}), "/h")

    def test_the_cb_command_is_unchanged(self):
        from atobots import cli
        self.assertTrue((ROOT / "engine" / "bin" / "cb").is_file())
        self.assertEqual(cli.build_parser().prog, "cb")


if __name__ == "__main__":
    unittest.main()
