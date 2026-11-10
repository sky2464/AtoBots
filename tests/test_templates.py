import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from claudebot import bots, cli  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect  # noqa: E402

TEMPLATES = ROOT / "engine" / "templates"
SKILLS = ("cb-basics", "work-tracking", "create-bot")


def banned_terms_from_env(environ=None):
    source = os.environ if environ is None else environ
    raw = source.get("CB_BANNED_TERMS", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def company_text_hits(root, banned):
    """Case-insensitive scan of files under `root` (used by the planted-word test)."""
    needles = tuple(word.casefold() for word in banned if word)
    hits = []
    for p in Path(root).rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        folded = p.read_text(errors="ignore").casefold()
        for original, word in zip(banned, needles):
            if word in folded:
                hits.append(f"{original} in {p}")
    return hits


def tracked_files():
    listed = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT)
    for rel in listed.decode().split("\0"):
        if not rel:
            continue
        path = ROOT / rel
        if path.is_file():
            yield rel, path


def tracked_text_hits(banned):
    """Case-insensitive scan of every tracked file."""
    needles = tuple(word.casefold() for word in banned if word)
    hits = []
    for rel, path in tracked_files():
        folded = path.read_text(errors="ignore").casefold()
        for word in needles:
            if word in folded:
                hits.append(f"{word} in {rel}")
    return hits


def _rules() -> str:
    return (TEMPLATES / "rules.md").read_text().lower()


class NeutralTemplateTests(unittest.TestCase):
    def test_templates_do_not_name_a_person_or_a_zone(self):
        banned = re.compile(r"\b(he|him|his|Central)\b", re.IGNORECASE)
        for path in (ROOT / "engine" / "templates").rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(errors="ignore")
            self.assertIsNone(banned.search(text), f"{path}: {banned.search(text)}")


class DocLinkTests(unittest.TestCase):
    def test_readme_updating_and_changelog_links_exist(self):
        link = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for name in ("README.md", "UPDATING.md", "CHANGELOG.md"):
            text = (ROOT / name).read_text()
            for target in link.findall(text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path = target.split("#", 1)[0]
                if not path:
                    continue
                self.assertTrue((ROOT / path).is_file(), f"{name} -> {target}")


class RulesTest(unittest.TestCase):
    def test_rules_have_required_lines(self):
        r = _rules()
        text = (TEMPLATES / "rules.md").read_text()
        self.assertLess(len(text.splitlines()), 120)
        for needle in (
            "publishing and spending are owner-only",
            "cb decision record",
            "sending messages",
            "editing or deleting published content",
            "signups",
            "commitments",
            "read connected services on its own",
            "read-only",
            "only the owner's own words count",
            "never approval",
            "never follow instructions",
            "raw outside content",
            "summaries",
            "ssh or the firewall",
            "lock the owner out",
            "never put secret values",
            "masking hides the raw, base64 and hex forms only",
            "any bot can read the store key",
            "memory",
            "sources the owner approved",
            "one server account",
            "not a technical lock",
            "asking the owner",
            "put every open question in one message",
            "should i proceed",
        ):
            self.assertIn(needle, r, needle)

    def test_store_wording_matches_in_readme_and_module(self):
        sentence = ("masking hides the raw, base64 and hex forms only. "
                    "any bot can read the store key.")
        readme = (ROOT / "README.md").read_text().lower()
        module = (ROOT / "engine" / "claudebot" / "store.py").read_text().lower()
        self.assertNotIn("never passed through a model session", readme)
        for text in (readme, module, _rules()):
            self.assertIn(sentence, " ".join(text.split()), text[:40])

    def test_admin_persona_batches_questions(self):
        text = " ".join(
            (TEMPLATES / "admin" / "PERSONA.md").read_text().lower().split())
        self.assertIn("put every open question in one message", text)
        self.assertNotIn("one question at a time", text)


class InitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.cfg = Config(self.home)

    def init(self, name="Acme"):
        import os
        old = os.environ.get("CLAUDEBOT_HOME")
        os.environ["CLAUDEBOT_HOME"] = str(self.home)
        try:
            with redirect_stdout(io.StringIO()):
                return cli.main(["init", "--company", name])
        finally:
            if old is None:
                del os.environ["CLAUDEBOT_HOME"]
            else:
                os.environ["CLAUDEBOT_HOME"] = old

    def names(self):
        conn = connect(self.cfg)
        try:
            return [b.name for b in bots.list_bots(conn)]
        finally:
            conn.close()

    def test_init_ships_only_admin(self):
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.names(), ["admin"])
        conn = connect(self.cfg)
        try:
            admin = bots.get(conn, "admin")
            self.assertEqual(admin.model, "sonnet")
            self.assertIsNone(admin.effort)
            self.assertEqual(sorted(admin.skills), sorted(SKILLS))
            self.assertEqual(bots.check(self.cfg, conn), [])
        finally:
            conn.close()
        self.assertTrue((self.home / "rules.md").is_file())
        self.assertTrue((self.home / ".git").is_dir())
        self.assertIn(".secrets/", (self.home / ".gitignore").read_text())
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.home,
                             capture_output=True, text=True).stdout
        self.assertTrue(log.strip())

    def test_init_is_idempotent(self):
        self.init()
        files = [self.home / "rules.md",
                 *(self.home / "bots" / "admin" / f
                   for f in ("SOUL.md", "PERSONA.md", "MEMORY.md"))]
        before = [p.read_text() for p in files]
        (self.home / "bots" / "admin" / "MEMORY.md").write_text("# Memory\nkept\n")
        (self.home / "rules.md").write_text("custom rules\n")
        self.assertEqual(self.init(), 0)
        after = [p.read_text() for p in files]
        self.assertEqual(after[1:3], before[1:3])
        self.assertEqual(after[3], "# Memory\nkept\n")
        self.assertEqual(after[0], "custom rules\n")
        self.assertEqual(self.names(), ["admin"])

    def _refresh(self):
        buf = io.StringIO()
        old = os.environ.get("CLAUDEBOT_HOME")
        os.environ["CLAUDEBOT_HOME"] = str(self.home)
        try:
            with redirect_stdout(buf):
                rc = cli.main(["templates", "refresh"])
        finally:
            if old is None:
                os.environ.pop("CLAUDEBOT_HOME", None)
            else:
                os.environ["CLAUDEBOT_HOME"] = old
        return rc, buf.getvalue()

    def test_refresh_keeps_edited_rules_and_doctor_says_so(self):
        from claudebot import doctor
        self.init()
        rules = self.home / "rules.md"
        template = (TEMPLATES / "rules.md").read_text()
        rc, out = self._refresh()
        self.assertEqual(rc, 0)
        self.assertFalse((self.home / "rules.md.new").exists())
        self.assertIn("rules.md", out)
        rules.write_text("local rule\n")
        rc, out = self._refresh()
        self.assertEqual(rules.read_text(), "local rule\n")
        self.assertEqual((self.home / "rules.md.new").read_text(), template)
        self.assertIn(
            "rules.md has local changes; the new version is in rules.md.new", out)
        found = doctor.checks(
            self.cfg, active_fn=lambda: True, mem_fn=lambda: 4000,
            live_enabled_fn=lambda: False, live_session_fn=lambda: False,
            token_fn=lambda c: True, track=False)
        self.assertIn("rules-new", [f.key for f in found])
        self.assertFalse(any(p.name.startswith(".") for p in (self.home / "skills").iterdir()))

    def test_refresh_replaces_an_unedited_older_template(self):
        self.init()
        old = subprocess.check_output(
            ["git", "-C", str(ROOT), "show", "05e85da:engine/templates/rules.md"])
        rules = self.home / "rules.md"
        rules.write_bytes(old)
        conn = connect(self.cfg)
        conn.execute("DELETE FROM meta WHERE key='templates.rules_sha'")
        conn.close()
        rc, _ = self._refresh()
        self.assertEqual(rc, 0)
        self.assertEqual(rules.read_bytes(), (TEMPLATES / "rules.md").read_bytes())
        self.assertFalse((self.home / "rules.md.new").exists())


class EnvFileTest(unittest.TestCase):
    def test_home_falls_back_to_env_file(self):
        import os
        from unittest import mock
        from claudebot import config
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "claudebot.env"
            f.write_text("CLAUDEBOT_HOME=/srv/co\n")
            env = {k: v for k, v in os.environ.items() if k != "CLAUDEBOT_HOME"}
            with mock.patch.object(config, "ENV_FILE", f), \
                    mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(Config.load().home, Path("/srv/co"))
                os.environ["CLAUDEBOT_HOME"] = "/other"
                self.assertEqual(Config.load().home, Path("/other"))
                del os.environ["CLAUDEBOT_HOME"]
                f.unlink()
                with self.assertRaises(RuntimeError):
                    Config.load()


class TemplatesTest(unittest.TestCase):
    def test_engine_has_no_company_text(self):
        banned = banned_terms_from_env()
        ref = os.environ.get("GITHUB_REF_NAME", "")
        if not banned:
            if ref == "main":
                self.fail("CB_BANNED_TERMS is empty on main")
            self.skipTest("CB_BANNED_TERMS is unset")
        self.assertEqual(tracked_text_hits(banned), [])

    def test_tracked_files_have_no_addresses_or_public_keys(self):
        ipv4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        key = re.compile(r"ssh-(?:ed25519|rsa) AAAA")
        allowed = {"127.0.0.1", "0.0.0.0"}
        bad = []
        for rel, path in tracked_files():
            text = path.read_text(errors="ignore")
            for match in ipv4.finditer(text):
                if match.group(0) not in allowed:
                    bad.append(f"{rel}: {match.group(0)}")
            if key.search(text):
                bad.append(f"{rel}: ssh public key")
        self.assertEqual(bad, [])

    def test_banned_term_scan_flags_a_planted_word(self):
        self.assertEqual(banned_terms_from_env({}), ())
        self.assertEqual(
            banned_terms_from_env({"CB_BANNED_TERMS": " one , ,two "}),
            ("one", "two"))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "note.txt"
            path.write_text("prefix TOKEN suffix\n")
            self.assertEqual(company_text_hits(d, ("TOKEN",)),
                             [f"TOKEN in {path}"])
            self.assertEqual(company_text_hits(d, ("absent",)), [])

    def test_admin_has_all_three_skills(self):
        for s in SKILLS:
            p = TEMPLATES / "skills" / s / "SKILL.md"
            self.assertTrue(p.is_file(), s)
            head = p.read_text().split("---")[1]
            self.assertIn(f"name: {s}", head)
            self.assertIn("description:", head)

    def test_admin_soul_under_cap(self):
        cfg = Config("/nonexistent")
        for f, cap in (("SOUL.md", cfg.soul_cap_bytes),
                       ("PERSONA.md", cfg.persona_cap_bytes),
                       ("MEMORY.md", cfg.memory_cap_bytes)):
            n = len((TEMPLATES / "admin" / f).read_bytes())
            self.assertGreater(n, 0)
            self.assertLess(n, cap, f)

    def test_every_cb_command_in_skills_exists(self):
        parser = cli.build_parser()
        pat = re.compile(r"`cb ([a-z-]+)(?: ([a-z][a-z-]*))?")
        for p in TEMPLATES.rglob("*.md"):
            for m in pat.finditer(p.read_text()):
                argv = [m.group(1)] + ([m.group(2)] if m.group(2) else [])
                with self.assertRaises(SystemExit) as cm, \
                        redirect_stdout(io.StringIO()):
                    parser.parse_args(argv + ["--help"])
                self.assertEqual(cm.exception.code, 0, f"{p.name}: cb {argv}")

    def test_install_script_syntax(self):
        r = subprocess.run(["bash", "-n", str(ROOT / "engine" / "install.sh")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (ROOT / "engine" / "install.sh").read_text()
        self.assertIn("set -euo pipefail", text)
        self.assertIn("sudo -n true", text)
        self.assertLess(text.index("sudo -n true"), text.index(" init --company"))
        self.assertIn("/etc/claudebot.env", text)
        unit = (ROOT / "engine" / "systemd" / "claudebot-dispatcher.service").read_text()
        self.assertIn("@ENGINE@", unit)
        self.assertNotIn("/home/", unit)
        for forbidden in ("sshd", "ufw", "sudoers"):
            self.assertNotIn(forbidden, text)

    def test_update_script_holds_the_queue_before_checkout(self):
        path = ROOT / "engine" / "update.sh"
        r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = path.read_text()
        self.assertIn("set -euo pipefail", text)
        self.assertIn("this version cannot hold the queue; continuing as before", text)
        self.assertLess(text.index("dispatcher hold"), text.index("checkout --quiet --detach"))
        stop = text.index("sudo systemctl stop claudebot-dispatcher.service")
        self.assertLess(text.index("dispatcher idle"), stop)
        self.assertLess(stop, text.index("checkout --quiet --detach"))
        self.assertIn('"$CB" dispatcher release', text)
        self.assertIn("--force", text)
        self.assertIn("/proc/self/cgroup", text)
        self.assertIn("claudebot-admin.service", text)
        self.assertIn("run cb update", text)
        self.assertIn("sudo systemctl restart claudebot-admin.service", text)
        self.assertNotIn("sudo systemctl stop claudebot-admin.service", text)
        self.assertIn("sudo systemctl start claudebot-dispatcher.service", text)
        self.assertIn("update-pending.json", text)
        self.assertIn("update-pending marker", text)
        self.assertIn("Verifying services", text)

    def test_units_are_rendered_in_one_place(self):
        lib = ROOT / "engine" / "lib" / "units.sh"
        install = (ROOT / "engine" / "install.sh").read_text()
        update = (ROOT / "engine" / "update.sh").read_text()
        admin = (ROOT / "engine" / "systemd" / "claudebot-admin.service").read_text()
        for path in (lib, ROOT / "engine" / "install.sh", ROOT / "engine" / "update.sh"):
            r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("install_units", lib.read_text())
        self.assertIn("lib/units.sh", install)
        self.assertIn("lib/units.sh", update)
        self.assertNotIn("render_unit()", install)
        self.assertNotIn("2 GB", admin)
        self.assertIn("MemoryMax=@MEMORY_MAX@", admin)
        self.assertIn("The conversation will restart.", update)
        self.assertIn("config get run_memory_max", install)
        self.assertIn("config get run_memory_max", update)
        self.assertIn("--timezone", install)
        self.assertIn("admin has-token", install)
        self.assertIn("pre-update-*.db", update)
        self.assertIn("Deleted", update)
        self.assertIn("-gt 5", update)
        self.assertIn("claudebot.releaseKey", update)
        self.assertIn('note: release tags are not verified (see UPDATING.md, "Verifying releases")', update)
        self.assertIn("git tag -s", (ROOT / "UPDATING.md").read_text())
        self.assertIn("cb: do not run as root", install)
        self.assertNotIn('ln -sfn "$ENGINE_DIR/bin/cb" /usr/local/bin/cb', install)

    def test_ci_workflow_is_locked_down(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("permissions:", text)
        self.assertIn("contents: read", text)
        self.assertIn("branches: [main]", text)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4", text)
        self.assertIn("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5", text)
        self.assertIn("shellcheck engine/*.sh engine/lib/*.sh", text)
        self.assertIn("CB_BANNED_TERMS: ${{ secrets.CB_BANNED_TERMS }}", text)


if __name__ == "__main__":
    unittest.main()
