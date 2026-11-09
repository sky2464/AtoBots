import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import bots, cli  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


class BotsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)

    def make_skill(self, name):
        d = self.cfg.home / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n")

    def new(self, name="backend", **kw):
        kw.setdefault("role", "Backend engineer")
        return bots.create(self.cfg, self.conn, name, soul="I am a soul.",
                           persona="Calm and brief.", **kw)


class CreateTests(BotsBase):
    def test_create_makes_all_parts(self):
        self.make_skill("a")
        self.make_skill("b")
        b = self.new(model="opus", effort="high", skills=("a", "b"),
                     mcp=("gh",), connectors=True)
        d = self.cfg.bots_dir / "backend"
        for f in ("SOUL.md", "PERSONA.md", "MEMORY.md"):
            self.assertTrue((d / f).is_file(), f)
        self.assertTrue((d / ".claude" / "skills").is_dir())
        self.assertTrue((d / ".claude" / "skills" / "a").is_symlink())
        self.assertTrue((d / ".claude" / "skills" / "b" / "SKILL.md").exists())
        row = self.conn.execute("SELECT * FROM bots WHERE name='backend'").fetchone()
        self.assertEqual(row["model"], "opus")
        self.assertEqual(row["effort"], "high")
        self.assertEqual(json.loads(row["skills"]), ["a", "b"])
        self.assertEqual(json.loads(row["mcp"]), ["gh"])
        self.assertEqual(row["connectors"], 1)
        self.assertEqual(row["status"], "active")
        self.assertEqual(b.skills, ("a", "b"))
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM activity WHERE kind='bot.create'").fetchone())

    def test_defaults(self):
        b = self.new()
        self.assertEqual((b.model, b.reports_to, b.connectors), ("sonnet", "admin", False))

    def test_bad_name_rejected(self):
        for bad in ("../admin", "Admin", "a b", "", "a" * 32, "admin\n", "1x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.new(bad)
        self.assertFalse(self.cfg.bots_dir.exists()
                         and any(self.cfg.bots_dir.iterdir()))
        self.assertFalse((self.cfg.home / "admin").exists())
        with self.assertRaises(ValueError):
            bots.get(self.conn, "../admin")

    def test_duplicate_rejected(self):
        self.new()
        with self.assertRaises(ValueError):
            self.new()

    def test_unknown_skill_rejected_without_files(self):
        with self.assertRaises(ValueError):
            self.new(skills=("nope",))
        self.assertFalse((self.cfg.bots_dir / "backend").exists())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM bots").fetchone())

    def test_created_bot_survives_reload(self):
        self.new()
        c2 = connect(self.cfg)
        try:
            self.assertEqual([b.name for b in bots.list_bots(c2)], ["backend"])
            self.assertEqual(bots.get(c2, "backend").role, "Backend engineer")
        finally:
            c2.close()


class LifecycleTests(BotsBase):
    def test_retire_keeps_identity(self):
        self.new()
        bots.set_status(self.conn, "backend", "retired", "admin")
        d = self.cfg.bots_dir / "backend"
        for f in ("SOUL.md", "PERSONA.md", "MEMORY.md"):
            self.assertTrue((d / f).exists())
        self.assertEqual(bots.get(self.conn, "backend").status, "retired")
        self.assertEqual(bots.list_bots(self.conn), [])
        self.assertEqual(len(bots.list_bots(self.conn, status="retired")), 1)

    def test_bad_status_rejected(self):
        self.new()
        with self.assertRaises(ValueError):
            bots.set_status(self.conn, "backend", "gone", "admin")

    def test_update_fields_and_skills(self):
        self.make_skill("a")
        self.new()
        bots.update(self.cfg, self.conn, "backend", "admin", model="opus",
                    skills=("a",), connectors=True, persona="New persona.")
        b = bots.get(self.conn, "backend")
        self.assertEqual((b.model, b.skills, b.connectors), ("opus", ("a",), True))
        self.assertEqual((self.cfg.bots_dir / "backend" / "PERSONA.md").read_text(),
                         "New persona.")
        self.assertTrue((self.cfg.bots_dir / "backend" / ".claude" / "skills" / "a").is_symlink())
        bots.update(self.cfg, self.conn, "backend", "admin", skills=())
        self.assertEqual(list((self.cfg.bots_dir / "backend" / ".claude" / "skills").iterdir()), [])

    def test_update_unknown_field(self):
        self.new()
        with self.assertRaises(ValueError):
            bots.update(self.cfg, self.conn, "backend", "admin", status="retired")


class CheckTests(BotsBase):
    def test_clean_check_is_empty(self):
        self.new()
        self.assertEqual(bots.check(self.cfg, self.conn), [])

    def test_check_flags_oversize(self):
        self.new()
        d = self.cfg.bots_dir / "backend"
        (d / "MEMORY.md").write_text("m" * (8 * 1024 + 1))
        (d / "PERSONA.md").write_text("p" * (4 * 1024 + 1))
        msgs = bots.check(self.cfg, self.conn)
        self.assertTrue(any("MEMORY.md" in m for m in msgs), msgs)
        self.assertTrue(any("PERSONA.md" in m for m in msgs), msgs)
        (d / "SOUL.md").write_text("s" * (4 * 1024 + 1))
        self.assertTrue(any("SOUL.md" in m and "over the" in m
                            for m in bots.check(self.cfg, self.conn)))

    def test_soul_change_detected(self):
        self.new()
        (self.cfg.bots_dir / "backend" / "SOUL.md").write_text("tampered")
        msgs = bots.check(self.cfg, self.conn)
        self.assertTrue(any("SOUL.md changed" in m for m in msgs), msgs)

    def test_soul_update_requires_decision(self):
        self.new()
        with self.assertRaises(ValueError):
            bots.update(self.cfg, self.conn, "backend", "admin", soul="new")
        with self.assertRaises(ValueError):
            bots.update(self.cfg, self.conn, "backend", "admin", soul="new",
                        decision="D-0099")
        self.assertEqual((self.cfg.bots_dir / "backend" / "SOUL.md").read_text(),
                         "I am a soul.")
        self.conn.execute(
            "INSERT INTO decisions(id,title,created_at) VALUES('D-0001','t','x')")
        bots.update(self.cfg, self.conn, "backend", "admin", soul="new soul",
                    decision="D-0001")
        self.assertEqual(bots.check(self.cfg, self.conn), [])
        self.assertEqual((self.cfg.bots_dir / "backend" / "SOUL.md").read_text(),
                         "new soul")

    def test_check_flags_missing_file(self):
        self.new()
        (self.cfg.bots_dir / "backend" / "MEMORY.md").unlink()
        self.assertTrue(any("MEMORY.md" in m for m in bots.check(self.cfg, self.conn)))


class PromptTests(BotsBase):
    def test_identity_order_and_rules(self):
        self.new()
        d = self.cfg.bots_dir / "backend"
        (d / "MEMORY.md").write_text("MEM-TEXT")
        (self.cfg.home / "rules.md").write_text("RULES-TEXT")
        p = bots.identity_prompt(self.cfg, self.conn, "backend")
        order = [p.index(x) for x in ("I am a soul.", "Calm and brief.",
                                      "MEM-TEXT", "RULES-TEXT")]
        self.assertEqual(order, sorted(order))

    def test_rules_optional(self):
        self.new()
        self.assertIn("I am a soul.", bots.identity_prompt(self.cfg, self.conn, "backend"))

    def test_identity_prompt_stays_small(self):
        names = [f"s{i}" for i in range(10)]
        for n in names:
            self.make_skill(n)
        self.new(skills=tuple(names))
        p = bots.identity_prompt(self.cfg, self.conn, "backend")
        for n in names:
            self.assertNotIn(n, p)
        d = self.cfg.bots_dir / "backend"
        (d / "SOUL.md").write_text("S" * 20000)
        (d / "PERSONA.md").write_text("P" * 20000)
        (d / "MEMORY.md").write_text("M" * 20000)
        p = bots.identity_prompt(self.cfg, self.conn, "backend")
        self.assertLessEqual(len(p.encode()), 4096 + 4096 + 8192 + 1024)

    def test_agents_json_omits_self_and_retired(self):
        self.new("admin", role="Chief")
        self.new("backend")
        self.new("old")
        self.new("pausy")
        bots.set_status(self.conn, "old", "retired", "admin")
        bots.set_status(self.conn, "pausy", "paused", "admin")
        data = json.loads(bots.agents_json(self.conn, exclude="admin"))
        self.assertEqual(sorted(data), ["backend"])
        self.assertIn("Backend engineer", data["backend"]["description"])
        self.assertIn("prompt", data["backend"])


class CliTests(BotsBase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def test_missing_soul_file_is_clean_error(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            rc, _ = self.run_cli("bot", "new", "x", "--role", "r",
                                 "--soul-file", "/nonexistent/s",
                                 "--persona-file", "/nonexistent/p")
        self.assertEqual(rc, 1)
        self.assertIn("error:", err.getvalue())

    def test_new_list_show_set_pause_retire_check(self):
        home = Path(self._tmp.name)
        (home / "soul.md").write_text("soul text")
        (home / "persona.md").write_text("persona text")
        rc, _ = self.run_cli("bot", "new", "backend", "--role", "Dev",
                             "--soul-file", str(home / "soul.md"),
                             "--persona-file", str(home / "persona.md"))
        self.assertEqual(rc, 0)
        rc, out = self.run_cli("bot", "list")
        self.assertIn("backend", out)
        rc, out = self.run_cli("bot", "show", "backend")
        self.assertIn("Dev", out)
        rc, _ = self.run_cli("bot", "set", "backend", "--model", "opus")
        self.assertEqual(bots.get(self.conn, "backend").model, "opus")
        self.run_cli("bot", "pause", "backend")
        self.assertEqual(bots.get(self.conn, "backend").status, "paused")
        self.run_cli("bot", "resume", "backend")
        self.assertEqual(bots.get(self.conn, "backend").status, "active")
        self.run_cli("bot", "retire", "backend")
        self.assertEqual(bots.get(self.conn, "backend").status, "retired")
        rc, out = self.run_cli("check")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
