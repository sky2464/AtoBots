import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import activity, bots, runner, usage  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402

from test_runner import RunnerBase  # noqa: E402


def _line(message_id, model="claude-sonnet-5-0", at="2026-11-09T10:00:00.000Z",
          **extra):
    d = {"type": "assistant", "timestamp": at,
         "message": {"id": message_id, "model": model,
                     "usage": {"input_tokens": 5,
                               "cache_creation_input_tokens": 50,
                               "cache_read_input_tokens": 500,
                               "output_tokens": 9}}}
    d.update(extra)
    return (json.dumps(d) + "\n").encode()


class FromResultTests(unittest.TestCase):
    def test_from_result_reads_cache_fields_and_model(self):
        data = {"usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                          "cache_read_input_tokens": 1000, "output_tokens": 7},
                "modelUsage": {"claude-sonnet-5-0": {}, "claude-haiku-5-5": {}}}
        self.assertEqual(usage.from_result(data), {
            "model": "claude-sonnet-5-0", "input_tokens": 10,
            "cache_write": 100, "cache_read": 1000, "output_tokens": 7})

    def test_from_result_without_data_is_zero_and_no_model(self):
        empty = {"model": None, "input_tokens": 0, "cache_write": 0,
                 "cache_read": 0, "output_tokens": 0}
        self.assertEqual(usage.from_result(None), empty)
        self.assertEqual(usage.from_result({"usage": "garbage"}), empty)


class UsageBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.cfg = Config(root / "home", claude_bin="/nonexistent",
                          claude_projects_dir=root / "projects")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="a",
                    persona="p", model="sonnet", effort="medium")
        bots.create(self.cfg, self.conn, "backend", role="backend", soul="b",
                    persona="p", model="sonnet", effort="medium")

    def project_folder(self, bot="admin") -> Path:
        d = self.cfg.claude_projects_dir / usage.project_dir(self.cfg, bot).name
        d.mkdir(parents=True, exist_ok=True)
        return d


class IngestTests(UsageBase):
    def test_ingest_is_incremental(self):
        f = self.project_folder() / "sess-1.jsonl"
        f.write_bytes(_line("m1"))
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 1)
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 0)
        with open(f, "ab") as h:
            h.write(_line("m2"))
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 1)
        row = self.conn.execute("SELECT * FROM usage ORDER BY id DESC").fetchone()
        self.assertEqual((row["trigger"], row["ref"], row["bot"]),
                         ("session", "sess-1", "admin"))
        self.assertEqual((row["input_tokens"], row["cache_write"],
                          row["cache_read"], row["output_tokens"]),
                         (5, 50, 500, 9))
        self.assertEqual(row["at"], "2026-11-09T10:00:00+00:00")

    def test_ingest_skips_sidechains_users_and_garbage(self):
        f = self.project_folder() / "sess-2.jsonl"
        f.write_bytes(_line("m1", isSidechain=True) + b'{"type": "user"}\n'
                      + b"not json\n" + _line("m2"))
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 1)

    def test_ingest_counts_a_message_once_when_split_into_lines(self):
        # One API reply written as two content blocks: same message id, same usage.
        f = self.project_folder() / "sess-3.jsonl"
        f.write_bytes(_line("m1") + _line("m1"))
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 1)
        with open(f, "ab") as h:
            h.write(_line("m1"))  # the second block arrives in a later pass
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 0)

    def test_ingest_skips_partial_last_line(self):
        f = self.project_folder() / "sess-4.jsonl"
        f.write_bytes(_line("m1") + b'{"type": "assistant", "mess')
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 1)
        with open(f, "ab") as h:
            h.write(b'age": {"id": "m2", "usage": {}}}\n')
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 1)

    def test_ingest_skips_sessions_the_runner_already_recorded(self):
        f = self.project_folder() / "sess-run.jsonl"
        f.write_bytes(_line("m1"))
        self.conn.execute("INSERT INTO runs(bot, session_id, lane, started_at) "
                          "VALUES ('admin', 'sess-run', 'normal', 'x')")
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 0)

    def test_missing_folder_is_zero(self):
        self.assertEqual(usage.ingest_transcripts(self.cfg, self.conn), 0)


class ReportTests(UsageBase):
    def test_report_hit_rate(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        usage.record(self.conn, bot="admin", model="claude-sonnet-5-0",
                     lane="chat", trigger="session", ref="s", input_tokens=100,
                     cache_write=100, cache_read=800, output_tokens=10, at=now)
        text = usage.report(self.conn, since="24h", by="bot")
        self.assertIn("80.0%", text)
        self.assertIn("admin", text)

    def test_report_by_model_and_trigger(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        usage.record(self.conn, bot="admin", model="claude-sonnet-5-0", lane="n",
                     trigger="owner", ref="1", input_tokens=1, cache_write=0,
                     cache_read=0, output_tokens=1, at=now)
        by_model = usage.report(self.conn, since="24h", by="model")
        by_trigger = usage.report(self.conn, since="24h", by="trigger")
        self.assertIn("claude-sonnet-5-0", by_model)
        self.assertIn("owner", by_trigger)

    def test_report_respects_window(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
            timespec="seconds")
        usage.record(self.conn, bot="admin", model="claude-sonnet-5-0", lane="n",
                     trigger="owner", ref="1", input_tokens=7, cache_write=0,
                     cache_read=0, output_tokens=0, at=old)
        self.assertNotIn("admin", usage.report(self.conn, since="7d", by="bot"))
        self.assertIn("admin", usage.report(self.conn, since="30d", by="bot"))

    def test_report_flags_model_mismatch(self):
        usage.record(self.conn, bot="backend", model="claude-opus-5-0", lane="n",
                     trigger="wake", ref="1", input_tokens=1, cache_write=0,
                     cache_read=0, output_tokens=1)
        text = usage.report(self.conn, since="24h", by="bot")
        self.assertIn("mismatch", text.lower())
        self.assertIn("claude-opus-5-0", text)

    def test_matching_model_is_not_flagged(self):
        usage.record(self.conn, bot="backend", model="claude-sonnet-5-0", lane="n",
                     trigger="wake", ref="1", input_tokens=1, cache_write=0,
                     cache_read=0, output_tokens=1)
        self.assertNotIn("mismatch", usage.report(self.conn, since="24h").lower())

    def test_bad_since_is_rejected(self):
        with self.assertRaises(ValueError):
            usage.report(self.conn, since="soon")
        with self.assertRaises(argparse.ArgumentTypeError):
            usage.window_arg("soon")
        self.assertEqual(usage.window_arg("7d"), "7d")


class RunUsageTests(RunnerBase):
    def test_run_records_usage(self):
        ms = self.msgs({"kind": "wake"})
        r = runner.run(self.cfg, self.conn, self.bot(), ms, "normal")
        rows = self.conn.execute("SELECT * FROM usage").fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row["bot"], row["model"], row["lane"], row["trigger"], row["ref"],
             row["input_tokens"], row["cache_write"], row["cache_read"],
             row["output_tokens"]),
            ("backend", "claude-sonnet-5-0", "normal", "wake", str(r.run_id),
             10, 100, 1000, 7))

    def test_usage_failure_never_fails_the_run(self):
        ms = self.msgs({})
        with mock.patch.object(usage, "record", side_effect=RuntimeError("disk")):
            r = runner.run(self.cfg, self.conn, self.bot(), ms, "normal")
        self.assertEqual((r.reason, r.exit_code), ("ok", 0))
        events = self.conn.execute(
            "SELECT detail FROM activity WHERE kind='usage.record_failed'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn("disk", events[0][0])


if __name__ == "__main__":
    unittest.main()
