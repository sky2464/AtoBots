"""Engine-driven rotation: when the always-on Admin is moved to a fresh session."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import bots, chat, items, requests, rotation  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg = Config(self.root / "home",
                          claude_projects_dir=self.root / "projects")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="s",
                    persona="p")
        env = mock.patch.dict(os.environ, {
            chat.STATE_ENV: str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.transcript = self.root / "t.jsonl"

    def write_transcript(self, tokens, age_seconds):
        self.transcript.write_text(json.dumps({"type": "assistant", "message": {
            "role": "assistant",
            "usage": {"input_tokens": tokens, "output_tokens": 0}}}) + "\n")
        when = time.time() - age_seconds
        os.utime(self.transcript, (when, when))
        return self.transcript


class ShouldRotateTests(Base):
    def test_rotates_at_hard_threshold(self):
        self.assertTrue(rotation.should_rotate(100000, 0, self.cfg))
        self.assertTrue(rotation.should_rotate(150000, 5, self.cfg))
        self.assertFalse(rotation.should_rotate(99999, 0, self.cfg))

    def test_idle_rotation_needs_both_size_and_idle(self):
        # small and idle: no
        self.assertFalse(rotation.should_rotate(10000, 7200, self.cfg))
        # large and fresh: no (the cache is still warm)
        self.assertFalse(rotation.should_rotate(90000, 100, self.cfg))
        # large and idle past the cache TTL: yes
        self.assertTrue(rotation.should_rotate(90000, 3601, self.cfg))
        # exactly at the TTL is not yet past it
        self.assertFalse(rotation.should_rotate(90000, 3600, self.cfg))

    def test_defaults(self):
        self.assertEqual(self.cfg.idle_rotate_tokens, 80000)
        self.assertEqual(self.cfg.cache_ttl_seconds, 3600)

    def test_idle_seconds_reads_transcript_mtime(self):
        p = self.write_transcript(1, 120)
        self.assertGreaterEqual(rotation.idle_seconds(p), 119)
        self.assertEqual(rotation.idle_seconds(self.root / "missing.jsonl"), 0.0)


class SummaryTests(Base):
    def test_summary_lists_open_items_and_last_owner_messages(self):
        item = items.new(self.cfg, self.conn, "Ship the quarterly report",
                         "admin")
        for i in range(7):
            requests.capture(self.conn, f"msg {i}", "sess")
        self.conn.execute(
            "INSERT INTO decisions(id, title, quote, said_at, session,"
            " recorded_by, created_at) VALUES ('D-0001','Pay the invoice',"
            "'yes pay','2026-11-09T10:00:00+00:00','sess','admin',"
            "'2026-11-09T10:00:00+00:00')")
        text = rotation.build_summary(self.cfg, self.conn)
        self.assertIn("Ship the quarterly report", text)
        self.assertIn(item, text)
        self.assertIn("msg 6", text)
        self.assertIn("msg 2", text)
        self.assertIn("Pay the invoice", text)
        self.assertIn("## Open items", text)
        self.assertIn("## Owner's last messages", text)

    def test_summary_only_keeps_last_five_owner_messages(self):
        for i in range(7):
            requests.capture(self.conn, f"msg {i}", "sess")
        text = rotation.build_summary(self.cfg, self.conn)
        section = text.split("## Owner's last messages", 1)[1]
        section = section.split("\n## ", 1)[0]
        lines = [x for x in section.splitlines() if x.startswith("- ")]
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].endswith("msg 2"))
        self.assertTrue(lines[-1].endswith("msg 6"))


class CheckOnceTests(Base):
    def setUp(self):
        super().setUp()
        self.pid = self.root / "state" / "claude.pid"
        self.pid.parent.mkdir(parents=True, exist_ok=True)
        self.pid.write_text("4242\n")
        self.kills = []

    def kill(self, pid, sig):
        self.kills.append((pid, sig))

    def test_busy_turn_is_never_rotated(self):
        p = self.write_transcript(150000, 5)  # written 5 s ago: a turn runs
        self.assertFalse(rotation.check_once(self.cfg, self.conn, p,
                                             kill=self.kill))
        self.assertEqual(self.kills, [])
        self.assertFalse(chat.marker_path(self.cfg).exists())

    def test_idle_large_context_rotates_once(self):
        p = self.write_transcript(90000, 4000)  # idle past the cache TTL
        self.assertTrue(rotation.check_once(self.cfg, self.conn, p,
                                            kill=self.kill))
        self.assertEqual(len(self.kills), 1)
        self.assertTrue(chat.marker_path(self.cfg).exists())
        self.assertTrue(chat.rollover_path(self.cfg).is_file())

    def test_no_rotation_below_thresholds(self):
        p = self.write_transcript(20000, 4000)
        self.assertFalse(rotation.check_once(self.cfg, self.conn, p,
                                             kill=self.kill))
        self.assertEqual(self.kills, [])

    def test_owner_message_after_last_reply_blocks_rotation(self):
        p = self.write_transcript(150000, 120)
        requests.capture(self.conn, "are you there?", "sess")  # newer than file
        self.assertFalse(rotation.check_once(self.cfg, self.conn, p,
                                             kill=self.kill))
        self.assertEqual(self.kills, [])

    def test_missing_transcript_does_nothing(self):
        self.assertFalse(rotation.check_once(self.cfg, self.conn, None,
                                             kill=self.kill))

    def test_watch_stops_promptly(self):
        w = rotation.Watch(self.cfg, "sid", None, interval=3600)
        w.start()
        t0 = time.monotonic()
        w.stop()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertFalse(w.is_alive())


if __name__ == "__main__":
    unittest.main()
