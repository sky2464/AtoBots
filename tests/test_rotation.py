import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import bots, items, requests, rotation  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


def _assistant(tokens, stop="end_turn"):
    return json.dumps({"type": "assistant", "message": {
        "stop_reason": stop,
        "usage": {"input_tokens": tokens, "output_tokens": 0}}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(self.root / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        bots.create(self.cfg, self.conn, "admin", "Admin", "soul", "persona",
                    reports_to=None, actor="system")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def transcript(self, lines, age=0):
        p = self.root / "t.jsonl"
        p.write_text("\n".join(lines) + "\n")
        t = time.time() - age
        os.utime(p, (t, t))
        return p


class ShouldRotateTests(Base):
    def test_rotates_at_hard_threshold(self):
        self.assertTrue(rotation.should_rotate(
            self.cfg.chat_rollover_tokens, 0, self.cfg))
        self.assertFalse(rotation.should_rotate(
            self.cfg.chat_rollover_tokens - 1, 0, self.cfg))

    def test_idle_rotation_needs_both_size_and_idle(self):
        big, ttl = self.cfg.idle_rotate_tokens, self.cfg.cache_ttl_seconds
        self.assertFalse(rotation.should_rotate(big - 1, ttl + 1, self.cfg))
        self.assertFalse(rotation.should_rotate(big, ttl, self.cfg))
        self.assertTrue(rotation.should_rotate(big, ttl + 1, self.cfg))


class TurnTests(Base):
    def test_finished_turn_is_not_in_progress(self):
        p = self.transcript([_assistant(5), json.dumps({"type": "system"})])
        self.assertFalse(rotation.turn_in_progress(p))

    def test_tool_use_or_pending_user_message_is_in_progress(self):
        self.assertTrue(rotation.turn_in_progress(
            self.transcript([_assistant(5, "tool_use")])))
        self.assertTrue(rotation.turn_in_progress(self.transcript(
            [_assistant(5), json.dumps({"type": "user", "message": {}})])))
        self.assertTrue(rotation.turn_in_progress("/nonexistent"))

    def test_safe_to_restart_needs_quiet_and_a_finished_turn(self):
        self.assertFalse(rotation.safe_to_restart(
            self.transcript([_assistant(5)], age=5)))
        self.assertTrue(rotation.safe_to_restart(
            self.transcript([_assistant(5)], age=60)))
        self.assertFalse(rotation.safe_to_restart(
            self.transcript([_assistant(5, "tool_use")], age=60)))
        # nothing written for ten minutes: no turn can still be running
        self.assertTrue(rotation.safe_to_restart(
            self.transcript([_assistant(5, "tool_use")], age=700)))


class SummaryTests(Base):
    def test_summary_lists_open_items_and_last_owner_messages(self):
        a = items.new(self.cfg, self.conn, "Draft the invoice report", "admin")
        items.pause(self.conn, a, "Next: add totals", "admin")
        done = items.new(self.cfg, self.conn, "Old thing", "admin")
        items.set_status(self.conn, done, "done", "admin")
        for i in range(7):
            requests.capture(self.conn, f"owner message {i}", "s")
        text = rotation.build_summary(self.cfg, self.conn)
        self.assertIn(a, text)
        self.assertIn("Next: add totals", text)
        self.assertNotIn("Old thing", text)
        self.assertIn("owner message 6", text)
        self.assertNotIn("owner message 1\n", text.split("## Requests")[0])
        self.assertIn("not yet linked to an item (7)", text)


class WatcherTests(Base):
    def make(self, path, clock):
        self.calls = []
        return rotation.Watcher(
            self.cfg, self.conn, "sid", clock=clock,
            rollover=lambda cfg, conn, summary: self.calls.append(summary),
            find=lambda cfg, sid: path)

    def test_rotates_once_when_large_and_quiet(self):
        now = [0.0]
        p = self.transcript([_assistant(self.cfg.chat_rollover_tokens)],
                            age=60)
        w = self.make(p, lambda: now[0])
        self.assertFalse(w.tick())  # not yet time to look
        now[0] += rotation.CHECK_SECONDS
        self.assertTrue(w.tick())
        now[0] += rotation.CHECK_SECONDS
        self.assertFalse(w.tick())
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Open items", self.calls[0])
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM activity WHERE kind='chat.rotated'").fetchone())

    def test_never_interrupts_a_turn_or_a_small_session(self):
        now = [float(rotation.CHECK_SECONDS)]
        busy = self.transcript(
            [_assistant(self.cfg.chat_rollover_tokens, "tool_use")], age=60)
        w = self.make(busy, lambda: now[0] * 2)
        self.assertFalse(w.check())
        small = self.transcript([_assistant(10)], age=9999)
        self.assertFalse(self.make(small, lambda: 0.0).check())
        self.assertEqual(self.calls, [])

    def test_a_failing_check_never_raises(self):
        w = rotation.Watcher(self.cfg, self.conn, "sid", clock=lambda: 1e9,
                             find=lambda cfg, sid: 1 / 0)
        w._next = 0
        self.assertFalse(w.tick())


if __name__ == "__main__":
    unittest.main()
