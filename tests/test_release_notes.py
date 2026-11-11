import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "release_notes.py"

_spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

SAMPLE = """# Changelog

Intro text.

## 0.2.0 - 2026-11-12

Second.

### What changed
- thing

## 0.1.0 - 2026-11-10

First.

## 0.0.10 - 2026-11-09

Ten.

## 0.0.1 - 2026-11-08
"""


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_one_section_without_its_heading(self):
        notes = release_notes.extract(SAMPLE, "0.2.0")
        self.assertEqual(notes, "Second.\n\n### What changed\n- thing\n")

    def test_version_match_is_exact(self):
        # 0.0.1 must not match the 0.0.10 heading, and vice versa.
        self.assertEqual(release_notes.extract(SAMPLE, "0.0.10"), "Ten.\n")
        with self.assertRaises(ValueError):
            release_notes.extract(SAMPLE, "0.0.1")  # heading exists but is empty

    def test_missing_version_is_an_error(self):
        with self.assertRaises(ValueError):
            release_notes.extract(SAMPLE, "9.9.9")

    def test_bad_version_is_an_error(self):
        for bad in ("v0.1.0", "0.1", "0.1.0; rm", ""):
            with self.assertRaises(ValueError):
                release_notes.extract(SAMPLE, bad)

    def test_command_line_prints_notes_or_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "CHANGELOG.md"
            path.write_text(SAMPLE)
            ok = subprocess.run([sys.executable, "-I", str(SCRIPT), "0.1.0", str(path)],
                                capture_output=True, text=True)
            self.assertEqual((ok.returncode, ok.stdout), (0, "First.\n"))
            bad = subprocess.run([sys.executable, "-I", str(SCRIPT), "3.0.0", str(path)],
                                 capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertEqual(bad.stdout, "")
            self.assertIn("no section", bad.stderr)

    def test_the_repository_changelog_has_a_section_for_the_current_version(self):
        sys.path.insert(0, str(ROOT / "engine"))
        from atobots import __version__
        notes = release_notes.extract((ROOT / "CHANGELOG.md").read_text(), __version__)
        self.assertTrue(notes.strip())


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    def test_triggers_on_version_tags_and_by_hand(self):
        self.assertIn('"v[0-9]+.[0-9]+.[0-9]+"', self.text)
        self.assertIn("workflow_dispatch:", self.text)

    def test_only_this_workflow_can_write(self):
        self.assertIn("contents: write", self.text)
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertNotIn("write", ci)

    def test_actions_are_pinned_to_full_commit_shas(self):
        for line in self.text.splitlines():
            if "uses:" in line:
                ref = line.split("@", 1)[1].split()[0]
                self.assertRegex(ref, r"^[0-9a-f]{40}$", line)

    def test_checks_run_before_anything_is_created(self):
        order = [self.text.index(s) for s in
                 ("Check the tag", "release_notes.py", "gh release create")]
        self.assertEqual(order, sorted(order))
        self.assertIn("merge-base --is-ancestor", self.text)
        self.assertIn("--verify-tag", self.text)

    def test_tag_input_reaches_the_shell_only_through_the_environment(self):
        # An input such as `v1; curl ...` must never be pasted into a script.
        for line in self.text.splitlines():
            if "github.event.inputs" in line:
                self.assertTrue(line.strip().startswith("TAG:"), line)


if __name__ == "__main__":
    unittest.main()
