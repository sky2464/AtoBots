import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from atobots import bots, cli, items, messages, search  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import connect, init_db  # noqa: E402


class SearchBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        for name in ("admin", "backend"):
            bots.create(self.cfg, self.conn, name, role=name, soul="s",
                        persona="p")

    def write(self, rel, text):
        p = self.cfg.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class SearchTests(SearchBase):
    def setUp(self):
        super().setUp()
        search.reindex_files(self.cfg)  # bots.create already wrote MEMORY.md
    def test_finds_another_bots_note_and_memory(self):
        self.write("knowledge/backend/deploy.md",
                   "# Deploy runbook\n\nThe zebrafish pipeline restarts nightly.")
        self.write("bots/backend/MEMORY.md", "Owner prefers quokka naming.")
        self.assertEqual(search.reindex_files(self.cfg), 2)
        hit, = search.search(self.cfg, "zebrafish")
        self.assertEqual((hit.kind, hit.bot, hit.title),
                         ("knowledge", "backend", "Deploy runbook"))
        self.assertEqual(hit.ref, "knowledge/backend/deploy.md")
        hit, = search.search(self.cfg, "quokka", bot="backend")
        self.assertEqual((hit.kind, hit.bot, hit.ref),
                         ("memory", "backend", "bots/backend/MEMORY.md"))

    def test_snippet_is_short(self):
        self.write("knowledge/a.md", "# Long\n\n" + "needle " + "word " * 500)
        search.reindex_files(self.cfg)
        hit, = search.search(self.cfg, "needle")
        self.assertLessEqual(len(hit.snippet), 300)
        self.assertIn("needle", hit.snippet)

    def test_filter_by_kind(self):
        self.write("knowledge/a.md", "# A\n\nshared term")
        self.write("bots/admin/MEMORY.md", "shared term")
        search.reindex_files(self.cfg)
        self.assertEqual(len(search.search(self.cfg, "shared")), 2)
        hits = search.search(self.cfg, "shared", kind="memory")
        self.assertEqual([h.kind for h in hits], ["memory"])
        with self.assertRaises(ValueError):
            search.search(self.cfg, "shared", kind="bogus")

    def test_reindex_skips_unchanged(self):
        p = self.write("knowledge/a.md", "# A\n\nfirst text")
        self.assertEqual(search.reindex_files(self.cfg), 1)
        self.assertEqual(search.reindex_files(self.cfg), 0)
        p.write_text("# A\n\nsecond words here")
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.assertEqual(search.reindex_files(self.cfg), 1)

    def test_edited_file_replaces_its_row(self):
        p = self.write("knowledge/a.md", "# A\n\nolduniquetoken")
        search.reindex_files(self.cfg)
        p.write_text("# A\n\nnewuniquetoken plus more")
        os.utime(p, (time.time() + 5, time.time() + 5))
        search.reindex_files(self.cfg)
        self.assertEqual(search.search(self.cfg, "olduniquetoken"), [])
        self.assertEqual(len(search.search(self.cfg, "newuniquetoken")), 1)

    def test_fts_syntax_chars_are_safe(self):
        self.write("knowledge/a.md", "# A\n\nfoo and bar")
        search.reindex_files(self.cfg)
        for q in ('"AND (foo', "foo*", "NEAR(", "a:b", "-", "", '""', "OR"):
            search.search(self.cfg, q)
        self.assertEqual(len(search.search(self.cfg, '"AND (foo')), 1)

    def test_rebuild_after_deleting_search_db(self):
        self.write("knowledge/a.md", "# A\n\nfiletoken")
        t = items.new(self.cfg, self.conn, "Fix the gizmo", "backend")
        search.reindex_files(self.cfg)
        search.reindex_records(self.cfg, self.conn)
        self.cfg.search_path.unlink()
        search.reindex_files(self.cfg)
        search.reindex_records(self.cfg, self.conn)
        self.assertEqual(len(search.search(self.cfg, "filetoken")), 1)
        self.assertEqual(search.search(self.cfg, "gizmo")[0].ref, t)

    def test_secrets_soul_and_persona_not_indexed(self):
        self.write(".secrets/k.md", "hiddentoken")
        self.write("bots/backend/SOUL.md", "hiddentoken")
        self.write("bots/backend/PERSONA.md", "hiddentoken")
        self.write("knowledge/.secrets/x.md", "hiddentoken")
        self.write("knowledge/ok.md", "# Ok\n\nvisibletoken")
        search.reindex_files(self.cfg)
        self.assertEqual(search.search(self.cfg, "hiddentoken"), [])
        self.assertEqual(len(search.search(self.cfg, "visibletoken")), 1)

    def test_index_doc_replaces_same_ref_and_survives_missing_source(self):
        search.index_doc(self.cfg, "transcript", "t1", "backend", "Chat", "alpha")
        search.index_doc(self.cfg, "transcript", "t1", "backend", "Chat", "beta")
        self.assertEqual(search.search(self.cfg, "alpha"), [])
        self.assertEqual(len(search.search(self.cfg, "beta")), 1)
        with self.assertRaises(ValueError):
            search.index_doc(self.cfg, "nope", "x", None, "t", "b")

    def test_limit_is_clamped_to_at_least_one(self):
        self.write("knowledge/a.md", "# A\n\nsame")
        self.write("knowledge/b.md", "# B\n\nsame")
        search.reindex_files(self.cfg)
        self.assertEqual(len(search.search(self.cfg, "same", limit=-1)), 1)
        self.assertEqual(len(search.search(self.cfg, "same", limit=0)), 1)

    def test_deleted_file_row_removed(self):
        p = self.write("knowledge/a.md", "# A\n\ngonetoken")
        search.reindex_files(self.cfg)
        p.unlink()
        search.reindex_files(self.cfg)
        self.assertEqual(search.search(self.cfg, "gonetoken"), [])


class RecordTests(SearchBase):
    def test_records_indexed_incrementally_and_kept_after_source_leaves(self):
        t = items.new(self.cfg, self.conn, "Calibrate widget", "backend")
        mid = messages.send(self.conn, "admin", "backend", "Heads up",
                            "the flamingo cache is stale")
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at) VALUES"
            " ('admin','s1','chat','2026-11-08T10:00:00+00:00')")
        with mock.patch.dict(os.environ, {"CB_SESSION_ID": "s1"}):
            d = items.decide(self.conn, "Use sqlite", "yes use sqlite please",
                             "2026-11-08T10:00:00+00:00", "s1", "admin")
        self.assertEqual(search.reindex_records(self.cfg, self.conn), 3)
        self.assertEqual(search.reindex_records(self.cfg, self.conn), 0)
        self.assertEqual(search.search(self.cfg, "widget", kind="item")[0].ref, t)
        self.assertEqual(search.search(self.cfg, "flamingo",
                                       kind="message")[0].ref, str(mid))
        self.assertEqual(search.search(self.cfg, "sqlite",
                                       kind="decision")[0].ref, d)
        # an edited item is re-indexed, not duplicated
        self.conn.execute("UPDATE items SET title='Calibrate gadget',"
                          " updated_at=? WHERE id=?",
                          ("2099-01-01T00:00:00+00:00", t))
        self.assertGreaterEqual(search.reindex_records(self.cfg, self.conn), 1)
        self.assertEqual(search.search(self.cfg, "widget"), [])
        self.assertEqual(len(search.search(self.cfg, "gadget")), 1)
        # source row gone: search row stays
        self.conn.execute("DELETE FROM messages")
        search.reindex_records(self.cfg, self.conn)
        self.assertEqual(len(search.search(self.cfg, "flamingo")), 1)


class SearchCliTests(SearchBase):
    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        e = {"ATOBOTS_HOME": str(self.cfg.home)}
        e.update(env or {})
        with mock.patch.dict(os.environ, e), redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_note_add_files_under_bot_name(self):
        f = self.write("body.txt", "The ocelot protocol is simple.")
        rc, out, _ = self.run_cli("note", "add", "Ocelot Protocol!",
                                  "--body-file", str(f), env={"CB_BOT": "backend"})
        self.assertEqual(rc, 0)
        p = self.cfg.home / "knowledge" / "backend" / "ocelot-protocol.md"
        self.assertTrue(p.exists())
        self.assertIn("ocelot protocol", p.read_text().lower())
        rc, out, _ = self.run_cli("search", "ocelot", "--kind", "knowledge")
        self.assertEqual(rc, 0)
        self.assertIn("knowledge/backend/ocelot-protocol.md", out)

    def test_note_title_cannot_escape_folder(self):
        f = self.write("body.txt", "x")
        rc, _, _ = self.run_cli("note", "add", "../../etc/passwd",
                                "--body-file", str(f), env={"CB_BOT": "backend"})
        self.assertEqual(rc, 0)
        self.assertEqual([x.name for x in
                          (self.cfg.home / "knowledge" / "backend").iterdir()],
                         ["etc-passwd.md"])
        rc, _, err = self.run_cli("note", "add", "!!!", "--body-file", str(f),
                                  env={"CB_BOT": "backend"})
        self.assertEqual(rc, 1)

    def test_note_same_title_does_not_overwrite(self):
        f = self.write("body.txt", "x")
        for _ in range(2):
            self.run_cli("note", "add", "Dup", "--body-file", str(f),
                         env={"CB_BOT": "backend"})
        self.assertEqual(sorted(x.name for x in
                                (self.cfg.home / "knowledge" / "backend").iterdir()),
                         ["dup-2.md", "dup.md"])

    def test_cli_search_reindexes_records_first(self):
        items.new(self.cfg, self.conn, "Polish the narwhal", "backend")
        rc, out, _ = self.run_cli("search", "narwhal")
        self.assertEqual(rc, 0)
        self.assertIn("T-0001", out)


if __name__ == "__main__":
    unittest.main()
