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

    def ensure_admin(self):
        try:
            bots.get(self.conn, "admin")
        except KeyError:
            bots.create(self.cfg, self.conn, "admin", role="Chief",
                        soul="I am a soul.", persona="Calm and brief.",
                        reports_to=None)

    def new(self, name="backend", **kw):
        kw.setdefault("role", "Backend engineer")
        if name != "admin" and kw.get("reports_to", "admin") not in (None, ""):
            # Default manager is admin; create it when tests never did.
            try:
                bots.get(self.conn, kw.get("reports_to", "admin"))
            except KeyError:
                if kw.get("reports_to", "admin") == "admin":
                    self.ensure_admin()
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
        # ensure_admin may have created admin while trying a create that then
        # failed the name check; no other bot dirs should exist.
        names = ({p.name for p in self.cfg.bots_dir.iterdir()}
                 if self.cfg.bots_dir.exists() else set())
        self.assertTrue(names <= {"admin"})
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
        names = [r["name"] for r in self.conn.execute("SELECT name FROM bots")]
        self.assertTrue(set(names) <= {"admin"})

    def test_created_bot_survives_reload(self):
        self.new()
        c2 = connect(self.cfg)
        try:
            self.assertEqual(
                sorted(b.name for b in bots.list_bots(c2)),
                ["admin", "backend"])
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
        self.assertEqual([b.name for b in bots.list_bots(self.conn)], ["admin"])
        self.assertEqual(len(bots.list_bots(self.conn, status="retired")), 1)

    def test_bad_status_rejected(self):
        self.new()
        with self.assertRaises(ValueError):
            bots.set_status(self.conn, "backend", "gone", "admin")

    def test_admin_pause_retire_need_owner_decision(self):
        self.ensure_admin()
        with self.assertRaises(ValueError):
            bots.set_status(self.conn, "admin", "paused", "cli")
        with self.assertRaises(ValueError):
            bots.set_status(self.conn, "admin", "retired", "cli")
        self.assertEqual(bots.get(self.conn, "admin").status, "active")
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at) VALUES"
            " ('admin','owner-sess','chat','2026-11-09T00:00:00+00:00')")
        self.conn.execute(
            "INSERT INTO decisions(id,title,quote,said_at,session,recorded_by,"
            "created_at) VALUES('D-0001','pause admin','pause it','t',"
            "'owner-sess','admin','x')")
        bots.set_status(self.conn, "admin", "paused", "cli", decision="D-0001")
        self.assertEqual(bots.get(self.conn, "admin").status, "paused")

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

    def test_update_persona_atomic_temp_then_rename(self):
        self.new()
        path = self.cfg.bots_dir / "backend" / "PERSONA.md"
        bots.update(self.cfg, self.conn, "backend", "admin", persona="Atomic.")
        self.assertEqual(path.read_text(), "Atomic.")
        self.assertFalse((path.parent / "PERSONA.md.tmp").exists())

    def test_reports_to_must_be_existing_bot(self):
        self.ensure_admin()
        self.new()
        with self.assertRaises(ValueError):
            bots.update(self.cfg, self.conn, "backend", "admin",
                        reports_to="nobody")
        with self.assertRaises(ValueError):
            bots.update(self.cfg, self.conn, "backend", "admin",
                        reports_to="backend")
        bots.update(self.cfg, self.conn, "backend", "admin", reports_to="")
        self.assertIsNone(bots.get(self.conn, "backend").reports_to)

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
        # Bare decision row without Owner session provenance is refused.
        self.conn.execute(
            "INSERT INTO decisions(id,title,quote,said_at,session,recorded_by,"
            "created_at) VALUES('D-0001','t','q','t','bot-only','admin','x')")
        with self.assertRaises(ValueError):
            bots.update(self.cfg, self.conn, "backend", "admin", soul="new",
                        decision="D-0001")
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at) VALUES"
            " ('admin','owner-sess','chat','2026-11-09T00:00:00+00:00')")
        self.conn.execute(
            "INSERT INTO decisions(id,title,quote,said_at,session,recorded_by,"
            "created_at) VALUES('D-0002','t','q','t','owner-sess','admin','x')")
        bots.update(self.cfg, self.conn, "backend", "admin", soul="new soul",
                    decision="D-0002")
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
                                      "RULES-TEXT")]
        self.assertEqual(order, sorted(order))
        # Memory is not part of the identity: it moved to the task packet
        self.assertNotIn("MEM-TEXT", p)

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

    def test_no_agents_json_stub(self):
        # Cost control: colleagues travel in the task packet; Claude --agents
        # is not used (its payload would bust the cached prefix).
        self.assertFalse(hasattr(bots, "agents_json"))
        self.new("admin", role="Chief")
        self.new("backend")
        block = bots.colleagues_block(self.conn, exclude="admin")
        self.assertIn("backend", block)
        self.assertIn("cb send", block)


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
        self.ensure_admin()
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



class StablePrefixTests(BotsBase):
    def test_prefix_bytes_identical_when_memory_changes(self):
        self.new()
        d = self.cfg.bots_dir / "backend"
        (d / "MEMORY.md").write_text("first memory")
        before = bots.identity_prompt(self.cfg, self.conn, "backend").encode()
        (d / "MEMORY.md").write_text("second memory, edited between runs")
        after = bots.identity_prompt(self.cfg, self.conn, "backend").encode()
        self.assertEqual(before, after)

    def test_persona_notes_for_people_are_not_sent(self):
        try:
            bots.get(self.conn, "backend")
        except KeyError:
            bots.create(self.cfg, self.conn, "backend", "Backend", "soul",
                        "persona", reports_to=None, actor="system")
        d = self.cfg.bots_dir / "backend"
        (d / "PERSONA.md").write_text(
            "<!-- a note\nfor people -->\n# Persona: Backend\nVOICE-TEXT")
        text = bots.identity_prompt(self.cfg, self.conn, "backend")
        self.assertNotIn("a note", text)
        self.assertNotIn("<!--", text)
        self.assertIn("VOICE-TEXT", text)

    def test_memory_block_reads_capped_memory(self):
        self.new()
        (self.cfg.bots_dir / "backend" / "MEMORY.md").write_text("MEM-TEXT")
        block = bots.memory_block(self.cfg, "backend")
        self.assertTrue(block.startswith("## Memory\n\n"))
        self.assertIn("MEM-TEXT", block)
        (self.cfg.bots_dir / "backend" / "MEMORY.md").write_text(
            "M" * (self.cfg.memory_cap_bytes + 500))
        self.assertLessEqual(len(bots.memory_block(self.cfg, "backend").encode()),
                             self.cfg.memory_cap_bytes + 64)

    def test_read_capped_keeps_newest_suffix(self):
        self.new()
        cap = self.cfg.memory_cap_bytes
        body = ("OLD-" * 3000) + ("NEW-" * 3000)
        path = self.cfg.bots_dir / "backend" / "MEMORY.md"
        path.write_bytes(body.encode())
        self.assertGreater(path.stat().st_size, cap)
        text = bots._read_capped(path, cap)
        self.assertLessEqual(len(text.encode()), cap)
        self.assertIn("NEW-", text[-40:])
        self.assertNotIn("OLD-", text[:40])

    def test_colleagues_block_lists_other_active_bots(self):
        self.new("admin", role="Chief")
        self.new("backend", role="Backend engineer")
        self.new("old", role="Retired one")
        self.new("pausy", role="Paused one")
        bots.set_status(self.conn, "old", "retired", "admin")
        bots.set_status(self.conn, "pausy", "paused", "admin")
        block = bots.colleagues_block(self.conn, exclude="admin")
        self.assertTrue(block.startswith("## Colleagues\n"))
        self.assertIn("- backend: Backend engineer", block)
        self.assertNotIn("admin", block)
        self.assertNotIn("old", block)
        self.assertNotIn("pausy", block)
        self.assertEqual(block, bots.colleagues_block(self.conn, exclude="admin"))


if __name__ == "__main__":
    unittest.main()


class TierTests(BotsBase):
    def test_create_defaults_tier_from_model(self):
        self.assertEqual(self.new("runner", model="haiku").tier, "worker")
        self.assertEqual(self.new("planner", model="opus").tier, "thinker")
        self.assertEqual(self.new("explicit", model="haiku", tier="thinker").tier, "thinker")
        self.assertEqual(
            self.new("dated", model="claude-haiku-4-5-20251001").tier, "worker")

    def test_create_rejects_unknown_tier(self):
        with self.assertRaises(ValueError):
            self.new("odd", tier="boss")

    def test_model_change_updates_tier(self):
        self.new("switcher", model="sonnet")
        self.assertEqual(bots.get(self.conn, "switcher").tier, "thinker")
        bots.update(self.cfg, self.conn, "switcher", "admin",
                    model="claude-haiku-4-5")
        b = bots.get(self.conn, "switcher")
        self.assertEqual((b.model, b.tier), ("claude-haiku-4-5", "worker"))
        bots.update(self.cfg, self.conn, "switcher", "admin", model="opus")
        self.assertEqual(bots.get(self.conn, "switcher").tier, "thinker")

    def test_model_mismatches_flags_thinker_on_haiku_and_worker_on_sonnet(self):
        self.new("planner", model="opus")
        self.new("gofer", model="haiku")
        self.new("badthinker", model="haiku", tier="thinker")
        self.new("badworker", model="sonnet", tier="worker")
        self.new("idhaiku", model="claude-haiku-4-5", tier="thinker")
        self.assertEqual(sorted(bots.model_mismatches(self.conn)), [
            ("badthinker", "haiku", "thinker on haiku"),
            ("badworker", "sonnet", "worker not on haiku"),
            ("idhaiku", "claude-haiku-4-5", "thinker on haiku"),
        ])

    def test_model_mismatches_skips_retired_unless_asked(self):
        self.new("badthinker", model="haiku", tier="thinker")
        self.conn.execute("UPDATE bots SET status='retired' WHERE name=?",
                          ("badthinker",))
        self.assertEqual(bots.model_mismatches(self.conn), [])
        self.assertEqual(bots.model_mismatches(self.conn,
                                               include_retired=True),
                         [("badthinker", "haiku", "thinker on haiku")])

    def test_set_worker_must_be_existing_worker_bot(self):
        self.new("planner", model="opus")
        self.new("gofer", model="haiku")
        self.new("boss", model="opus")
        bots.update(self.cfg, self.conn, "planner", "admin", worker="gofer")
        self.assertEqual(bots.get(self.conn, "planner").worker, "gofer")
        for bad in ("planner", "boss", "nobody"):
            with self.assertRaises(ValueError, msg=bad):
                bots.update(self.cfg, self.conn, "planner", "admin", worker=bad)
        bots.update(self.cfg, self.conn, "planner", "admin", worker="")
        self.assertIsNone(bots.get(self.conn, "planner").worker)

    def test_create_takes_worker(self):
        self.new("gofer", model="haiku")
        b = self.new("planner", model="opus", worker="gofer")
        self.assertEqual(b.worker, "gofer")
        with self.assertRaises(ValueError):
            self.new("planner2", model="opus", worker="planner")
        self.assertFalse((self.cfg.bots_dir / "planner2").exists())

    def test_usage_table_exists(self):
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage'").fetchone())
