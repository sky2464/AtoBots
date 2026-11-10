import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import bots, cli, items, messages  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


class MessagesBase(unittest.TestCase):
    names = ("admin", "backend")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        for name in self.names:
            bots.create(self.cfg, self.conn, name, role=name, soul="s",
                        persona="p")

    def msg(self, id):
        return self.conn.execute("SELECT * FROM messages WHERE id=?",
                                 (id,)).fetchone()

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


class SendTests(MessagesBase):
    def test_send_creates_queued_message_with_effort(self):
        i = messages.send(self.conn, "admin", "backend", "hi", "body",
                          effort="high", level="deep")
        m = self.msg(i)
        self.assertEqual((m["status"], m["hop"], m["effort"], m["level"]),
                         ("queued", 0, "high", "deep"))
        self.assertTrue(m["chain_id"])

    def test_send_unknown_bot_raises(self):
        with self.assertRaises(KeyError):
            messages.send(self.conn, "admin", "ghost", "s", "b")
        with self.assertRaises(KeyError):
            messages.send(self.conn, "ghost", "admin", "s", "b")

    def test_send_to_retired_bot_raises(self):
        bots.set_status(self.conn, "backend", "retired", "admin")
        with self.assertRaises(ValueError):
            messages.send(self.conn, "admin", "backend", "s", "b")

    def test_bad_level_rejected(self):
        with self.assertRaises(ValueError):
            messages.send(self.conn, "admin", "backend", "s", "b", level="huge")

    def test_duplicate_dedupe_key_ignored(self):
        a = messages.send(self.conn, "admin", "backend", "s", "b", dedupe_key="k")
        b = messages.send(self.conn, "admin", "backend", "s2", "b2", dedupe_key="k")
        self.assertEqual(a, b)
        self.assertEqual(self.count(), 1)

    def test_large_body_rejected(self):
        with self.assertRaisesRegex(ValueError, "file"):
            messages.send(self.conn, "admin", "backend", "s", "x" * 4097)
        self.assertEqual(self.count(), 0)
        messages.send(self.conn, "admin", "backend", "s", "x" * 4096)

    def test_message_not_done_until_completed(self):
        i = messages.send(self.conn, "admin", "backend", "s", "b")
        self.assertEqual([r["id"] for r in messages.pending_for(self.conn, "backend")], [i])
        self.conn.execute("UPDATE messages SET status='claimed' WHERE id=?", (i,))
        self.assertEqual(len(messages.pending_for(self.conn, "backend")), 1)
        self.assertEqual(self.msg(i)["done_at"], None)
        messages.complete(self.conn, [i])
        self.assertEqual(self.msg(i)["status"], "done")
        self.assertTrue(self.msg(i)["done_at"])
        self.assertEqual(messages.pending_for(self.conn, "backend"), [])


class ChainTests(MessagesBase):
    def bounce(self, n):
        last = messages.send(self.conn, "admin", "backend", "s", "b")
        for k in range(n):
            frm, to = ("backend", "admin") if k % 2 == 0 else ("admin", "backend")
            last = messages.send(self.conn, frm, to, "s", "b", in_reply_to=last)
        return last

    def test_hop_increments_along_a_chain(self):
        a = messages.send(self.conn, "admin", "backend", "s", "b")
        b = messages.send(self.conn, "backend", "admin", "s", "b", in_reply_to=a)
        c = messages.send(self.conn, "admin", "backend", "s", "b", in_reply_to=b)
        self.assertEqual([self.msg(x)["hop"] for x in (a, b, c)], [0, 1, 2])
        self.assertEqual(len({self.msg(x)["chain_id"] for x in (a, b, c)}), 1)

    def test_chain_inherited_from_items_latest_chain(self):
        t = items.new(self.cfg, self.conn, "t", "admin")
        a = messages.send(self.conn, "admin", "backend", "s", "b", item_id=t)
        b = messages.send(self.conn, "backend", "admin", "s", "b", item_id=t)
        self.assertEqual(self.msg(a)["chain_id"], self.msg(b)["chain_id"])
        self.assertEqual(self.msg(b)["hop"], 1)

    def test_hop_limit_stops_chain(self):
        last = self.bounce(7)  # hops 0..7 delivered
        self.assertEqual(self.msg(last)["hop"], 7)
        before = self.count()
        stopped = messages.send(self.conn, "admin", "backend", "s", "b",
                                in_reply_to=last)
        self.assertEqual(stopped, 0)
        notes = self.conn.execute(
            "SELECT * FROM messages WHERE from_bot='system'").fetchall()
        self.assertEqual(self.count(), before + 1)
        self.assertEqual((len(notes), notes[0]["to_bot"], notes[0]["hop"]),
                         (1, "admin", 0))
        self.assertNotEqual(notes[0]["chain_id"], self.msg(last)["chain_id"])
        # Trying again does not make a second note.
        self.assertEqual(messages.send(self.conn, "admin", "backend", "s", "b",
                                       in_reply_to=last), 0)
        self.assertEqual(self.count(), before + 1)

    def test_hop_limit_without_admin_records_activity(self):
        self.conn.execute("DELETE FROM bots WHERE name='admin'")
        bots.create(self.cfg, self.conn, "other", role="o", soul="s", persona="p",
                    reports_to="backend")
        a = messages.send(self.conn, "backend", "other", "s", "b", hop_limit=1)
        self.assertEqual(messages.send(self.conn, "other", "backend", "s", "b",
                                       in_reply_to=a, hop_limit=1), 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE from_bot='system'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM activity WHERE kind='message.hop_stop'"
        ).fetchone()[0], 1)


def at(seconds):
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


DAY = 86400


class ChainWindowTests(MessagesBase):
    def setUp(self):
        super().setUp()
        self.t = items.new(self.cfg, self.conn, "t", "backend")

    def notes(self):
        return self.conn.execute(
            "SELECT * FROM messages WHERE from_bot='system'").fetchall()

    def test_four_ask_reply_cycles_over_days_all_deliver(self):
        for k in range(4):
            q = messages.ask(self.cfg, self.conn, "backend", "admin", self.t,
                             "q", "h", now=at(k * DAY))
            self.assertTrue(q)
            self.assertTrue(messages.reply(self.conn, "admin", q, "a",
                                           now=at(k * DAY + 60)))
        self.assertEqual(self.notes(), [])

    def test_nine_item_sends_over_days_all_deliver(self):
        for k in range(9):
            self.assertTrue(messages.send(self.conn, "admin", "backend", "s", "b",
                                          item_id=self.t, now=at(k * DAY)))

    def test_tight_reply_loop_stops_at_hop_8_with_one_note(self):
        last = messages.send(self.conn, "admin", "backend", "s", "b", now=at(0))
        for k in range(1, 8):
            frm, to = ("backend", "admin") if k % 2 else ("admin", "backend")
            last = messages.send(self.conn, frm, to, "s", "b", in_reply_to=last,
                                 now=at(k))
            self.assertTrue(last)
        for k in (8, 9):
            self.assertEqual(messages.send(self.conn, "admin", "backend", "s", "b",
                                           in_reply_to=last, now=at(k)), 0)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM activity WHERE kind='message.hop_stop'"
        ).fetchone()[0], 2)

    def test_tight_ask_loop_stops_at_hop_8_with_one_note(self):
        results = [messages.ask(self.cfg, self.conn, "backend", "admin", self.t,
                                "q", "h", now=at(k)) for k in range(10)]
        self.assertTrue(all(results[:8]))
        self.assertEqual(results[8:], [0, 0])
        self.assertEqual(len(self.notes()), 1)

    def test_no_item_frm_to_loop_stops_at_hop_limit(self):
        """Bot↔bot ping-pong without item_id or in_reply_to still chains."""
        last = messages.send(self.conn, "admin", "backend", "s", "b", now=at(0))
        for k in range(1, 8):
            frm, to = ("backend", "admin") if k % 2 else ("admin", "backend")
            last = messages.send(self.conn, frm, to, "s", "b", now=at(k))
            self.assertTrue(last, k)
            self.assertEqual(self.msg(last)["hop"], k)
            self.assertEqual(self.msg(last)["chain_id"], self.msg(1)["chain_id"])
        self.assertEqual(messages.send(self.conn, "admin", "backend", "s", "b",
                                       now=at(8)), 0)
        self.assertEqual(len(self.notes()), 1)
        # One-way floods do not chain (not a ping-pong): after the window the
        # reverse hop is forgotten, and same-direction sends stay at hop 0.
        one_way = [messages.send(self.conn, "admin", "backend", "note", "b",
                                 now=at(8 + 901 + k)) for k in range(5)]
        self.assertTrue(all(one_way))
        self.assertEqual([self.msg(i)["hop"] for i in one_way], [0] * 5)
        # A reverse send past that window starts a fresh chain at hop 0.
        nxt = messages.send(self.conn, "backend", "admin", "s", "b",
                            now=at(8 + 901 + 4 + 901))
        self.assertTrue(nxt)
        self.assertEqual(self.msg(nxt)["hop"], 0)

    def test_send_past_window_after_stop_delivers(self):
        for k in range(9):
            messages.send(self.conn, "admin", "backend", "s", "b",
                          item_id=self.t, now=at(k))
        self.assertEqual(messages.send(self.conn, "admin", "backend", "s", "b",
                                       item_id=self.t, now=at(10)), 0)
        i = messages.send(self.conn, "admin", "backend", "s", "b",
                          item_id=self.t, now=at(10 + 901))
        self.assertTrue(i)
        self.assertEqual(self.msg(i)["hop"], 0)

    def test_reply_honours_hop_limit(self):
        q = messages.ask(self.cfg, self.conn, "backend", "admin", self.t, "q", "h")
        self.assertEqual(messages.reply(self.conn, "admin", q, "a", hop_limit=1), 0)
        self.assertEqual(items.get(self.conn, self.t)["waiting_on"], "admin")

    def test_blocked_ask_writes_no_handoff(self):
        self.cfg.hop_limit = 0
        messages.ask(self.cfg, self.conn, "backend", "admin", self.t, "q", "h")
        self.assertFalse((self.cfg.work_dir / self.t / "handoff-backend.md").exists())


class AskReplyTests(MessagesBase):
    def setUp(self):
        super().setUp()
        self.t = items.new(self.cfg, self.conn, "t", "backend")

    def ask(self):
        return messages.ask(self.cfg, self.conn, "backend", "admin", self.t,
                            "which db?", "I was at step 3; then migrate.")

    def test_ask_writes_handoff_and_marks_waiting(self):
        mid = self.ask()
        p = self.cfg.work_dir / self.t / "handoff-backend.md"
        self.assertEqual(p.read_text(), "I was at step 3; then migrate.")
        self.assertEqual(items.get(self.conn, self.t)["waiting_on"], "admin")
        m = self.msg(mid)
        self.assertEqual((m["from_bot"], m["to_bot"], m["item_id"], m["kind"]),
                         ("backend", "admin", self.t, "question"))

    def test_reply_wakes_asker_with_handoff_path(self):
        mid = self.ask()
        rid = messages.reply(self.conn, "admin", mid, "use sqlite")
        r = self.msg(rid)
        self.assertEqual((r["to_bot"], r["from_bot"], r["kind"], r["item_id"]),
                         ("backend", "admin", "answer", self.t))
        self.assertIn("use sqlite", r["body"])
        self.assertIn(f"work/{self.t}/handoff-backend.md", r["body"])
        self.assertIsNone(items.get(self.conn, self.t)["waiting_on"])
        self.assertEqual(r["status"], "queued")
        self.assertEqual(r["hop"], 1)

    def test_reply_from_wrong_bot_rejected(self):
        mid = self.ask()
        with self.assertRaises(ValueError):
            messages.reply(self.conn, "backend", mid, "x")
        self.assertEqual(items.get(self.conn, self.t)["waiting_on"], "admin")

    def test_ask_at_hop_limit_does_not_mark_waiting(self):
        self.cfg.hop_limit = 0
        self.assertEqual(self.ask(), 0)
        self.assertIsNone(items.get(self.conn, self.t)["waiting_on"])


class MessagesCliTests(MessagesBase):
    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        e = {"CLAUDEBOT_HOME": str(self.cfg.home)}
        e.update(env or {})
        with mock.patch.dict(os.environ, e), redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_send_inbox_reply_flow_uses_cb_bot(self):
        rc, out, _ = self.run_cli("send", "backend", "hello", "body",
                                  env={"CB_BOT": "admin"})
        self.assertEqual((rc, out.strip()), (0, "1"))
        rc, out, _ = self.run_cli("inbox", "--from", "backend", "--full")
        self.assertEqual(rc, 0)
        self.assertIn("hello", out)
        self.assertIn("body", out)
        rc, out, _ = self.run_cli("reply", "1", "thanks", env={"CB_BOT": "backend"})
        self.assertEqual(rc, 0)
        self.assertEqual(self.msg(int(out))["to_bot"], "admin")

    def test_ask_cli(self):
        t = items.new(self.cfg, self.conn, "t", "backend")
        f = Path(self._tmp.name) / "h.md"
        f.write_text("handoff text")
        rc, _, _ = self.run_cli("ask", "admin", t, "q?", "--handoff-file", str(f),
                                env={"CB_BOT": "backend"})
        self.assertEqual(rc, 0)
        self.assertEqual(items.get(self.conn, t)["waiting_on"], "admin")

    def test_send_rejects_an_unknown_effort(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(
                ["send", "backend", "s", "b", "--effort", "huge"])

    def test_large_body_error_exit_1(self):
        rc, _, err = self.run_cli("send", "backend", "s", "x" * 5000,
                                  env={"CB_BOT": "admin"})
        self.assertEqual(rc, 1)
        self.assertIn("file", err)

    def _open(self, bot, session, lane="normal"):
        self.conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at) VALUES (?,?,?,?)",
            (bot, session, lane, "2026-11-10T00:00:00+00:00"))

    def test_normal_lane_cannot_spoof_from_and_own_send_works(self):
        self._open("backend", "norm-1")
        env = {"CB_BOT": "admin", "CB_SESSION_ID": "norm-1"}
        rc, _, err = self.run_cli(
            "send", "admin", "hi", "body", "--from", "admin", env=env)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.conn.execute(
            "SELECT detail FROM activity WHERE kind='auth.refused'"
            ).fetchone()["detail"], "send")
        rc, out, err = self.run_cli("send", "admin", "hi", "body", env=env)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.msg(int(out.strip()))["from_bot"], "backend")

    def test_cli_may_choose_from(self):
        rc, out, err = self.run_cli(
            "send", "backend", "hi", "body", "--from", "admin")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.msg(int(out.strip()))["from_bot"], "admin")

    def test_inbox_done_and_retry_follow_the_caller(self):
        to_backend = messages.send(self.conn, "admin", "backend", "hello", "body")
        to_admin = messages.send(self.conn, "backend", "admin", "ping", "body")
        self._open("admin", "norm-a")
        self._open("backend", "norm-b")
        worker = {"CB_BOT": "admin", "CB_SESSION_ID": "norm-b"}
        rc, _, err = self.run_cli("inbox", "--bot", "admin", env=worker)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertEqual(self.conn.execute(
            "SELECT detail FROM activity WHERE kind='auth.refused'"
            " ORDER BY id DESC").fetchone()["detail"], "inbox")
        rc, out, _ = self.run_cli("inbox", "--bot", "backend", env=worker)
        self.assertEqual(rc, 0)
        self.assertIn("hello", out)
        rc, _, _ = self.run_cli("message", "done", str(to_admin), env=worker)
        self.assertEqual(rc, 1)
        self.assertEqual(self.msg(to_admin)["status"], "queued")
        rc, _, err = self.run_cli("message", "done", str(to_backend), env=worker)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.msg(to_backend)["status"], "done")
        self.conn.execute(
            "UPDATE messages SET status='failed' WHERE id=?", (to_backend,))
        rc, _, _ = self.run_cli("message", "retry", str(to_backend), env=worker)
        self.assertEqual(rc, 1)
        self.assertEqual(self.msg(to_backend)["status"], "failed")
        rc, _, err = self.run_cli(
            "message", "retry", str(to_backend),
            env={"CB_SESSION_ID": "norm-a"})
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.msg(to_backend)["status"], "queued")

    def test_library_rejects_reserved_spoof_when_bound(self):
        with mock.patch.dict(os.environ, {"CB_BOT": "backend"}):
            with self.assertRaises(PermissionError):
                messages.send(self.conn, "admin", "backend", "s", "b",
                              priority="urgent")
            with self.assertRaises(PermissionError):
                messages.send(self.conn, "system", "admin", "s", "b")
            mid = messages.send(self.conn, "backend", "admin", "ok", "body")
        self.assertEqual(self.msg(mid)["from_bot"], "backend")


class OwnerHoldSpoofTests(MessagesBase):
    def test_spoofed_admin_urgent_cannot_be_minted_by_bot(self):
        """Owner-chat hold trusts from=admin+urgent; bots must not mint that."""
        with mock.patch.dict(os.environ, {"CB_BOT": "backend"}):
            with self.assertRaises(PermissionError):
                messages.send(self.conn, "admin", "backend", "wake", "now",
                              priority="urgent")
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()
