import errno
import gzip
import io
import json
import os
import statistics
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import (archive, bots, cli, dispatcher, items, messages,  # noqa: E402
                       requests, search)
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402
from claudebot.runner import RunResult, task_packet  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def iso(days_ago=0):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.cfg = Config(root / "home", claude_projects_dir=root / "projects")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        for n in ("admin", "backend"):
            bots.create(self.cfg, self.conn, n, role=n, soul="s", persona="p")

    def make_run(self, bot="backend", sid="11111111-2222-3333-4444-555555555555",
                 lines=None, project="-home-claude-DTbyNavid-bots-backend"):
        started = iso(0)
        cur = self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at, ended_at,"
            " exit_code, reason) VALUES (?,?,?,?,?,0,'ok')",
            (bot, sid, "normal", started, started))
        path = None
        if lines is not None:
            d = self.cfg.claude_projects_dir / project
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{sid}.jsonl"
            path.write_text("".join(json.dumps(x) + "\n" for x in lines))
        return cur.lastrowid, path

    def old_item(self, title, status="done", days=60, owner="backend"):
        id = items.new(self.cfg, self.conn, title, owner)
        self.conn.execute("UPDATE items SET status=?, updated_at=?, created_at=?"
                          " WHERE id=?", (status, iso(days), iso(days), id))
        return id

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def cb(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


CHAT = [
    {"type": "user", "message": {"role": "user", "content": "please check the zebrafish report"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Looking at quokka numbers now"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "echo hidden-tool-input"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "hidden-tool-output"}]}},
    {"type": "system", "subtype": "init", "note": "hidden-system"},
]


class TranscriptTests(Base):
    def test_transcript_moved_after_run(self):
        run_id, path = self.make_run(lines=CHAT)
        original = path.read_bytes()
        dest = archive.archive_transcript(self.cfg, self.conn, run_id)
        self.assertFalse(path.exists())
        self.assertEqual(dest, self.cfg.archive_dir / "2026-11" / f"backend-{run_id}.jsonl.gz")
        with gzip.open(dest, "rb") as f:
            self.assertEqual(f.read(), original)
        hits = search.search(self.cfg, "zebrafish", kind="transcript")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].bot, "backend")
        self.assertEqual(hits[0].ref, f"archive/2026-11/backend-{run_id}.jsonl.gz")
        self.assertTrue(search.search(self.cfg, "quokka", kind="transcript"))
        # only user and assistant text is indexed
        for word in ("hidden", "tool_result"):
            self.assertEqual(search.search(self.cfg, word), [])
        self.assertEqual(archive.fetch(self.cfg, hits[0].ref), dest)

    def test_index_text_capped_at_64kb(self):
        big = [{"type": "user", "message": {"content": "alpha " * 40000}},
               {"type": "assistant", "message": {"content": "omegaword"}}]
        run_id, _ = self.make_run(lines=big)
        archive.archive_transcript(self.cfg, self.conn, run_id)
        self.assertTrue(search.search(self.cfg, "alpha", kind="transcript"))
        self.assertEqual(search.search(self.cfg, "omegaword"), [])
        sc = search._open(self.cfg)
        try:
            n = sc.execute("SELECT length(body) FROM docs WHERE kind='transcript'"
                           ).fetchone()[0]
        finally:
            sc.close()
        self.assertLessEqual(n, 64 * 1024)

    def test_verify_before_delete(self):
        run_id, path = self.make_run(lines=CHAT)
        original = path.read_bytes()
        real = gzip.open

        def full_disk(*a, **k):
            mode = a[1] if len(a) > 1 else k.get("mode", "rb")
            if "w" in mode or "a" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(*a, **k)

        with mock.patch.object(gzip, "open", full_disk):
            with self.assertRaises(archive.ArchiveError):
                archive.archive_transcript(self.cfg, self.conn, run_id)
            with self.assertRaises(archive.ArchiveError):  # retried: still one note
                archive.archive_transcript(self.cfg, self.conn, run_id)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.cfg.archive_dir.rglob("*.gz")), [])
        self.assertEqual(list(self.cfg.archive_dir.rglob("*.tmp")), [])
        notes = self.conn.execute(
            "SELECT * FROM messages WHERE to_bot='admin' AND from_bot='system'"
        ).fetchall()
        self.assertEqual(len(notes), 1)
        self.assertIn(str(run_id), notes[0]["subject"])
        self.assertEqual(search.search(self.cfg, "zebrafish"), [])

    def test_corrupt_copy_keeps_original(self):
        run_id, path = self.make_run(lines=CHAT)
        with mock.patch.object(archive, "_digest_gz", return_value="bad"):
            with self.assertRaises(archive.ArchiveError):
                archive.archive_transcript(self.cfg, self.conn, run_id)
        self.assertTrue(path.exists())
        self.assertEqual(list(self.cfg.archive_dir.rglob("*.gz")), [])

    def test_missing_transcript_is_recorded_and_harmless(self):
        run_id, _ = self.make_run(lines=None)
        self.assertIsNone(archive.archive_transcript(self.cfg, self.conn, run_id))
        row = self.conn.execute(
            "SELECT * FROM activity WHERE kind='archive.transcript_missing'"
        ).fetchone()
        self.assertEqual(row["ref"], str(run_id))
        with self.assertRaises(KeyError):
            archive.archive_transcript(self.cfg, self.conn, 9999)

    def test_only_recorded_runs_are_touched(self):
        run_id, path = self.make_run(lines=CHAT)
        other = self.cfg.claude_projects_dir / "-home-claude-ClaudeBot" / "owner-session.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text("{}\n")
        archive.archive_transcript(self.cfg, self.conn, run_id)
        self.assertTrue(other.exists())

    def test_unsafe_session_id_is_not_followed(self):
        run_id, _ = self.make_run(sid="../../etc/passwd", lines=None)
        self.assertIsNone(archive.archive_transcript(self.cfg, self.conn, run_id))

    def test_dispatcher_archives_after_run_and_survives_failure(self):
        sid = "99999999-2222-3333-4444-555555555555"
        run_id, path = self.make_run(sid=sid, lines=CHAT)
        clock = lambda: iso(0)  # noqa: E731

        def fake_run(cfg, conn, bot, msgs, lane, on_start=None):
            return RunResult(run_id, sid, 0, "ok", 1, 1)

        d = dispatcher.Dispatcher(
            self.cfg, run_fn=fake_run, clock=clock, mem_fn=lambda: 4000,
            commit_fn=lambda *a: None, reindex_fn=lambda c, cn: 0,
            daily_fn=lambda *a: None)
        self.addCleanup(d.close)
        messages.send(self.conn, "admin", "backend", "s", "b", now=iso(1))
        d.tick()
        d.wait()
        self.assertFalse(path.exists())
        self.assertEqual(len(list(self.cfg.archive_dir.rglob("backend-*.gz"))), 1)

        def boom(cfg, conn, rid):
            raise RuntimeError("disk exploded")

        d2 = dispatcher.Dispatcher(
            self.cfg, run_fn=fake_run, clock=clock, mem_fn=lambda: 4000,
            commit_fn=lambda *a: None, reindex_fn=lambda c, cn: 0,
            archive_fn=boom, daily_fn=lambda *a: None)
        self.addCleanup(d2.close)
        mid = messages.send(self.conn, "admin", "backend", "s2", "b", now=iso(1))
        d2.tick()
        d2.wait()
        self.assertEqual(self.conn.execute(
            "SELECT status FROM messages WHERE id=?", (mid,)).fetchone()[0], "done")
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM activity WHERE kind='archive.failed'").fetchone())


class RetryTests(Base):
    def full_disk(self):
        real = gzip.open

        def f(*a, **k):
            mode = a[1] if len(a) > 1 else k.get("mode", "rb")
            if "w" in mode or "a" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(*a, **k)
        return mock.patch.object(gzip, "open", f)

    def admin_notes(self):
        return self.conn.execute(
            "SELECT * FROM messages WHERE to_bot='admin' AND from_bot='system'"
        ).fetchall()

    def test_failed_copy_is_retried_by_daily_step(self):
        run_id, path = self.make_run(lines=CHAT)
        with self.full_disk():
            with self.assertRaises(archive.ArchiveError):
                archive.archive_transcript(self.cfg, self.conn, run_id)
            self.assertEqual(archive.daily(self.cfg, self.conn, iso(0))
                             ["transcripts_retried"], 0)  # still failing
        self.assertTrue(path.exists())
        out = archive.daily(self.cfg, self.conn, iso(0))  # disk freed
        self.assertEqual(out["transcripts_retried"], 1)
        self.assertFalse(path.exists())
        self.assertTrue(search.search(self.cfg, "zebrafish", kind="transcript"))
        self.assertEqual(len(self.admin_notes()), 1)

    def test_unlink_failure_is_not_a_lost_archive(self):
        run_id, path = self.make_run(lines=CHAT)
        with mock.patch.object(archive.os, "unlink", side_effect=OSError("busy")):
            dest = archive.archive_transcript(self.cfg, self.conn, run_id)
        self.assertTrue(dest.exists())
        self.assertTrue(path.exists())
        self.assertEqual(self.admin_notes(), [])
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM activity WHERE kind='archive.unlink_failed'").fetchone())
        self.assertEqual(archive.sweep_transcripts(self.cfg, self.conn), 1)
        self.assertFalse(path.exists())

    def test_index_failure_keeps_original_and_is_retried(self):
        run_id, path = self.make_run(lines=CHAT)
        with mock.patch.object(search, "index_doc", side_effect=OSError("db busy")):
            with self.assertRaises(archive.ArchiveError):
                archive.archive_transcript(self.cfg, self.conn, run_id)
        self.assertTrue(path.exists())
        self.assertEqual(self.admin_notes(), [])
        self.assertEqual(search.search(self.cfg, "zebrafish"), [])
        self.assertEqual(archive.sweep_transcripts(self.cfg, self.conn), 1)
        self.assertFalse(path.exists())
        self.assertTrue(search.search(self.cfg, "zebrafish", kind="transcript"))

    def test_run_with_pending_copy_is_not_archived_as_record(self):
        run_id, path = self.make_run(lines=CHAT)
        self.conn.execute("UPDATE runs SET started_at=?, ended_at=? WHERE id=?",
                          (iso(60), iso(60), run_id))
        self.assertEqual(archive.archive_old_records(self.cfg, self.conn, iso(0))["runs"], 0)
        path.unlink()
        self.assertEqual(archive.archive_old_records(self.cfg, self.conn, iso(0))["runs"], 1)


class RebuildTests(Base):
    def test_search_rebuilds_from_archive(self):
        item = self.old_item("Quokka migration", days=60)
        mid = messages.send(self.conn, "admin", "backend", "Wombat subject",
                            "narwhal body", now=iso(60))
        self.conn.execute("UPDATE messages SET status='done', done_at=? WHERE id=?",
                          (iso(60), mid))
        run_id, _ = self.make_run(lines=CHAT)
        archive.archive_transcript(self.cfg, self.conn, run_id)
        archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual(self.count("items"), 0)
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.cfg.search_path) + suffix).unlink(missing_ok=True)
        search.reindex_records(self.cfg, self.conn)
        self.assertEqual([h.ref for h in search.search(self.cfg, "Quokka", kind="item")], [item])
        self.assertEqual([h.ref for h in search.search(self.cfg, "narwhal", kind="message")], [str(mid)])
        self.assertEqual(len(search.search(self.cfg, "zebrafish", kind="transcript")), 1)

    def test_duplicates_in_archive_are_tolerated(self):
        self.old_item("dup", days=60)
        archive.archive_old_records(self.cfg, self.conn, iso(0))
        f = self.cfg.archive_dir / "2026-09" / "records.jsonl.gz"
        data = gzip.open(f, "rb").read()
        with gzip.open(f, "wb") as fh:
            fh.write(data + b"not json\n" + data)
        self.cfg.search_path.unlink()
        self.assertEqual(archive.reindex_archive(self.cfg), 1)
        self.assertEqual(len(search.search(self.cfg, "dup", kind="item")), 1)

    def test_cli_reindex_archive(self):
        self.old_item("Quokka", days=60)
        archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.cfg.search_path.unlink()
        rc, out, err = self.cb("search", "reindex", "--archive")
        self.assertEqual(rc, 0, err)
        self.assertTrue(search.search(self.cfg, "Quokka", kind="item"))


class RecordTests(Base):
    def test_done_items_leave_active_tables_after_30_days(self):
        old = self.old_item("Quokka migration", days=60)
        young = self.old_item("Fresh finish", days=5)
        parked = self.old_item("Shelved thing", status="parked", days=62)
        dec = items.decide(self.conn, "Pick quokka", "use the quokka plan", iso(60),
                           "s", "admin")
        res = archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual(res["items"], 2)
        ids = [r["id"] for r in items.open_items(self.conn)]
        self.assertNotIn(old, ids)
        rc, out, _ = self.cb("next")
        self.assertNotIn(old, out)
        left = {r["id"] for r in self.conn.execute("SELECT id FROM items")}
        self.assertEqual(left, {young})
        self.assertEqual(self.count("decisions"), 1)  # decisions stay
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM decisions WHERE id=?", (dec,)).fetchone())
        rc, out, _ = self.cb("search", "Quokka")
        self.assertIn(old, out)
        self.assertEqual(rc, 0)
        # archived rows are readable from the archive
        f = self.cfg.archive_dir / "2026-09" / "records.jsonl.gz"
        with gzip.open(f, "rt") as fh:
            rows = [json.loads(x) for x in fh]
        self.assertEqual({r["row"]["id"] for r in rows if r["table"] == "items"}, {old, parked})
        # IDs are never reused
        newest = items.new(self.cfg, self.conn, "After", "backend")
        self.assertEqual(int(newest[2:]), int(parked[2:]) + 1)
        self.assertEqual(self.count("activity") > 0, True)

    def test_ids_not_reused_even_if_newest_row_archived(self):
        a = self.old_item("only", days=60)
        archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual(self.count("items"), 0)
        b = items.new(self.cfg, self.conn, "next", "backend")
        self.assertEqual(int(b[2:]), int(a[2:]) + 1)
        # messages and runs also keep counting (search watermarks depend on it)
        mid = messages.send(self.conn, "admin", "backend", "s", "b", now=iso(60))
        self.conn.execute("UPDATE messages SET status='done', done_at=? WHERE id=?",
                          (iso(60), mid))
        archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual(self.count("messages"), 0)
        mid2 = messages.send(self.conn, "admin", "backend", "s2", "b")
        self.assertGreater(mid2, mid)

    def test_open_items_never_archived(self):
        keep = [self.old_item(f"open {s}", status=s, days=4000)
                for s in ("pending", "todo", "next", "doing", "paused",
                          "feature", "slice", "finding")]
        res = archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual(res["items"], 0)
        left = {r["id"] for r in self.conn.execute("SELECT id FROM items")}
        self.assertEqual(left, set(keep))

    def test_parent_with_open_child_stays(self):
        parent = self.old_item("parent", days=60)
        child = self.old_item("child", status="doing", days=60)
        self.conn.execute("UPDATE items SET parent=? WHERE id=?", (parent, child))
        res = archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual(res["items"], 0)

    def test_open_requests_unaffected(self):
        it1 = self.old_item("closed part", days=60)
        it2 = self.old_item("open part", status="doing", days=60)
        r1 = requests.capture(self.conn, "build the quokka app", "s")
        r2 = requests.capture(self.conn, "closed request", "s")
        r3 = requests.capture(self.conn, "never handled", "s")
        self.conn.execute("UPDATE owner_requests SET received_at=?", (iso(60),))
        requests.link(self.conn, r1, [it1, it2])
        requests.link(self.conn, r2, [it1])
        before_brief = requests.startup_brief(self.conn)
        before_unh = [r["id"] for r in requests.unhandled(self.conn)]
        res = archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual(res["owner_requests"], 1)  # only r2
        self.assertEqual(requests.startup_brief(self.conn), before_brief)
        self.assertEqual([r["id"] for r in requests.unhandled(self.conn)], before_unh)
        ids = {r["id"] for r in self.conn.execute("SELECT id FROM owner_requests")}
        self.assertEqual(ids, {r1, r3})
        self.assertEqual(self.count("items"), 2)  # it1 stays: r1 is still open

    def test_messages_and_runs(self):
        old = messages.send(self.conn, "admin", "backend", "old note", "b", now=iso(60))
        young = messages.send(self.conn, "admin", "backend", "young", "b", now=iso(2))
        queued = messages.send(self.conn, "admin", "backend", "queued", "b", now=iso(70))
        for i, d in ((old, 60), (young, 2)):
            self.conn.execute("UPDATE messages SET status='done', done_at=?"
                              " WHERE id=?", (iso(d), i))
        r_old, _ = self.make_run(sid="a" * 8 + "-0000-0000-0000-000000000000")
        self.conn.execute("UPDATE runs SET started_at=?, ended_at=? WHERE id=?",
                          (iso(60), iso(60), r_old))
        live = self.conn.execute(
            "INSERT INTO runs(bot, session_id, started_at) VALUES ('backend','x',?)",
            (iso(60),)).lastrowid
        res = archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual((res["messages"], res["runs"]), (1, 1))
        left = {r["id"] for r in self.conn.execute("SELECT id FROM messages")}
        self.assertEqual(left, {young, queued})
        self.assertEqual([r["id"] for r in self.conn.execute("SELECT id FROM runs")], [live])
        # old message still searchable
        self.assertTrue(search.search(self.cfg, "old note", kind="message"))

    def test_failed_write_deletes_nothing(self):
        self.old_item("keepme", days=60)
        with mock.patch.object(archive, "_digest_gz", return_value="bad"):
            with self.assertRaises(archive.ArchiveError):
                archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.assertEqual(self.count("items"), 1)

    def test_second_run_appends_to_month_file(self):
        self.old_item("one", days=60)
        archive.archive_old_records(self.cfg, self.conn, iso(0))
        self.old_item("two", days=60)
        archive.archive_old_records(self.cfg, self.conn, iso(0))
        f = self.cfg.archive_dir / "2026-09" / "records.jsonl.gz"
        with gzip.open(f, "rt") as fh:
            titles = [json.loads(x)["row"]["title"] for x in fh]
        self.assertEqual(titles, ["one", "two"])


class DailyAndCapTests(Base):
    def write_memory(self, bot, n):
        (self.cfg.bots_dir / bot / "MEMORY.md").write_text("x" * n)

    def test_memory_over_cap_sends_one_housekeeping_message(self):
        self.write_memory("backend", self.cfg.memory_cap_bytes + 1)
        out = archive.check_memory_caps(self.cfg, self.conn)
        self.assertEqual(len(out), 1)
        self.assertIn("backend", out[0])
        self.assertEqual(archive.check_memory_caps(self.cfg, self.conn), [])
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE to_bot='backend'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "housekeeping")
        # once the bot has dealt with it, a new overage is reported again
        self.conn.execute("UPDATE messages SET status='done'")
        self.assertEqual(len(archive.check_memory_caps(self.cfg, self.conn)), 1)

    def test_under_cap_sends_nothing(self):
        self.write_memory("backend", self.cfg.memory_cap_bytes)
        self.assertEqual(archive.check_memory_caps(self.cfg, self.conn), [])
        self.assertEqual(self.count("messages"), 0)

    def test_offsite_needed(self):
        d = self.cfg.archive_dir / "2026-01"
        d.mkdir(parents=True)
        (d / "a.gz").write_bytes(b"x" * 100)
        mk = lambda used: (lambda p: (100, used, 100 - used))  # noqa: E731
        self.assertTrue(archive.offsite_needed(self.cfg, disk_usage_fn=mk(70)))
        self.assertFalse(archive.offsite_needed(self.cfg, disk_usage_fn=mk(69)))

    def test_fetch(self):
        d = self.cfg.archive_dir / "2026-01"
        d.mkdir(parents=True)
        (d / "a.gz").write_bytes(b"x")
        self.assertEqual(archive.fetch(self.cfg, "archive/2026-01/a.gz"), d / "a.gz")
        self.assertEqual(archive.fetch(self.cfg, "2026-01/a.gz"), d / "a.gz")
        with self.assertRaisesRegex(FileNotFoundError, "not on this server"):
            archive.fetch(self.cfg, "archive/2025-01/gone.gz")
        with self.assertRaises(ValueError):
            archive.fetch(self.cfg, "../company.db")

    def test_cli_archive(self):
        self.old_item("x", days=60)
        rc, out, err = self.cb("archive", "run")
        self.assertEqual(rc, 0, err)
        self.assertIn("items: 1", out)
        rc, out, _ = self.cb("archive", "status")
        self.assertEqual(rc, 0)
        self.assertIn("files: 1", out)
        rc, out, _ = self.cb("archive", "fetch", "2026-09/records.jsonl.gz")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().endswith("records.jsonl.gz"))
        rc, _, err = self.cb("archive", "fetch", "2020-01/nope.gz")
        self.assertEqual(rc, 1)
        self.assertIn("not on this server", err)

    def test_dispatcher_runs_daily_once_a_day(self):
        calls = []
        t = [NOW]
        d = dispatcher.Dispatcher(
            self.cfg, clock=lambda: t[0].isoformat(timespec="seconds"),
            mem_fn=lambda: 4000, reindex_fn=lambda c, cn: 0,
            daily_fn=lambda cfg, conn, now: calls.append(now))
        self.addCleanup(d.close)
        d.tick()
        t[0] += timedelta(hours=2)
        d.tick()
        self.assertEqual(len(calls), 1)
        t[0] += timedelta(days=1)
        d.tick()
        self.assertEqual(len(calls), 2)

    def test_dispatcher_daily_failure_never_breaks_tick(self):
        def bad(*a):
            raise RuntimeError("nope")

        d = dispatcher.Dispatcher(
            self.cfg, clock=lambda: iso(0), mem_fn=lambda: 4000,
            reindex_fn=lambda c, cn: 0, daily_fn=bad)
        self.addCleanup(d.close)
        self.assertEqual(d.tick(), [])
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM activity WHERE kind='archive.daily_failed'").fetchone())
        notes = lambda: self.conn.execute(  # noqa: E731
            "SELECT * FROM messages WHERE to_bot='admin' AND"
            " subject='Daily archiving failed'").fetchall()
        self.assertEqual(len(notes()), 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM meta WHERE key=?", (archive.DAILY_KEY,)).fetchone())
        d.tick()  # within the hour: not retried
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM activity WHERE kind='archive.daily_failed'"
        ).fetchone()[0], 1)


class ScaleTests(Base):
    def test_context_constant_over_years(self):
        admin_dir = self.cfg.bots_dir / "admin"
        (admin_dir / "MEMORY.md").write_text("memory v1")
        (self.cfg.home / "rules.md").write_text("rules")
        day_one = (bots.identity_prompt(self.cfg, self.conn, "admin"),
                   task_packet(self.cfg, self.conn, "admin", []),
                   requests.startup_brief(self.conn))
        for i in range(5):  # a few real archived transcripts
            rid, _ = self.make_run(sid=f"0000000{i}-2222-3333-4444-555555555555",
                                   lines=CHAT)
            archive.archive_transcript(self.cfg, self.conn, rid)
        n_msg, n_run, n_item = 200_000, 5_000, 20_000
        old = iso(400)
        c = self.conn
        keep = [self.old_item(f"live {i}", status="doing", days=900) for i in range(30)]
        c.execute("BEGIN")
        c.executemany(
            "INSERT INTO messages(from_bot,to_bot,subject,body,level,status,"
            "created_at,done_at,hop) VALUES ('admin','backend',?,?,'normal','done',?,?,0)",
            ((f"s{i}", f"body {i}", old, old) for i in range(n_msg)))
        c.executemany(
            "INSERT INTO runs(bot,session_id,lane,started_at,ended_at,exit_code,reason)"
            " VALUES ('backend',?,'normal',?,?,0,'ok')",
            ((f"sess{i}", old, old) for i in range(n_run)))
        c.executemany(
            "INSERT INTO items(id,status,owner,title,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ((f"T-{100000 + i}", "done", "backend", f"t{i}", old, old)
             for i in range(n_item)))
        c.execute("COMMIT")
        # these rows stand in for rows search already indexed day by day
        c.execute("INSERT OR REPLACE INTO meta VALUES('search.messages', ?)",
                  (str(c.execute("SELECT MAX(id) FROM messages").fetchone()[0]),))
        c.execute("INSERT OR REPLACE INTO meta VALUES('search.items', ?)", (iso(0),))
        start = time.monotonic()
        res = archive.archive_old_records(self.cfg, c, iso(0))
        self.assertEqual((res["messages"], res["runs"], res["items"]),
                         (n_msg, n_run, n_item))
        self.assertLess(time.monotonic() - start, 25)
        self.assertEqual(self.count("messages"), 0)
        after = (bots.identity_prompt(self.cfg, self.conn, "admin"),
                 task_packet(self.cfg, self.conn, "admin", []),
                 requests.startup_brief(self.conn))
        self.assertEqual(after, day_one)
        times = []
        for _ in range(7):
            t0 = time.perf_counter()
            items.next_items(c, None, 20)
            times.append(time.perf_counter() - t0)
        self.assertLess(statistics.median(times), 0.2)
        self.assertEqual({r["id"] for r in items.next_items(c, None, 100)}, set(keep))


if __name__ == "__main__":
    unittest.main()
