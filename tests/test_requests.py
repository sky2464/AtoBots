import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import bots, cli, items, requests  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


class RequestsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="a", soul="s", persona="p")

    def item(self, **kw):
        return items.new(self.cfg, self.conn, "t", "admin", **kw)


class RequestTests(RequestsBase):
    def test_capture_is_verbatim(self):
        text = "  Zeile eins\n\n  café ☃ 你好 \U0001f600\t \n"
        rid = requests.capture(self.conn, text, "sess-1")
        r = self.conn.execute("SELECT * FROM owner_requests WHERE id=?",
                              (rid,)).fetchone()
        self.assertEqual(r["text"].encode("utf-8"), text.encode("utf-8"))
        self.assertEqual(r["session_id"], "sess-1")

    def test_unhandled_until_linked(self):
        rid = requests.capture(self.conn, "do x", "s")
        self.assertEqual([r["id"] for r in requests.unhandled(self.conn)], [rid])
        a = self.item()
        requests.link(self.conn, rid, [a])
        requests.link(self.conn, rid, [a])  # idempotent
        self.assertEqual(requests.unhandled(self.conn), [])
        with self.assertRaises(KeyError):
            requests.link(self.conn, rid, ["T-9999"])
        with self.assertRaises(KeyError):
            requests.link(self.conn, 999, [a])

    def test_request_closes_when_all_items_done_or_parked(self):
        rid = requests.capture(self.conn, "do y", "s")
        a, b = self.item(), self.item()
        requests.link(self.conn, rid, [a, b])
        items.set_status(self.conn, a, "done", "admin")
        self.assertIn("do y", requests.startup_brief(self.conn))
        items.set_status(self.conn, b, "parked", "admin")
        self.assertNotIn("do y", requests.startup_brief(self.conn))

    def test_ten_requests_survive_kill(self):
        ids = [requests.capture(self.conn, f"request number {i}\nsecond line", "s")
               for i in range(10)]
        its = [self.item(status="doing") for _ in range(4)]
        for rid, it in zip(ids[:4], its):
            requests.link(self.conn, rid, [it])
        items.pause(self.conn, its[0], "note alpha", "admin")
        items.pause(self.conn, its[1], "note beta", "admin")
        self.conn.close()
        conn2 = connect(self.cfg)
        self.addCleanup(conn2.close)
        brief = requests.startup_brief(conn2)
        for i in range(10):
            self.assertIn(f"request number {i}\nsecond line", brief)
        self.assertIn("note alpha", brief)
        self.assertIn("note beta", brief)
        self.assertEqual(len(requests.unhandled(conn2)), 6)

    def test_brief_limit_says_how_many_more(self):
        for i in range(5):
            requests.capture(self.conn, f"r{i}", "s")
        brief = requests.startup_brief(self.conn, limit=3)
        self.assertIn("r0", brief)
        self.assertNotIn("r4", brief)
        self.assertIn("2 more", brief)


class HookTests(RequestsBase):
    def setUp(self):
        super().setUp()
        for sid in ("abc", "s"):
            self.conn.execute(
                "INSERT INTO runs(bot, session_id, lane, started_at)"
                " VALUES ('admin', ?, 'chat', ?)",
                (sid, "2026-11-10T00:00:00+00:00"))

    def run_hook(self, raw: bytes):
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(raw)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", err):
            rc = cli.main(["hook", "prompt"])
        return rc, out.getvalue(), err.getvalue()

    def test_hook_captures_prompt_silently(self):
        raw = json.dumps({"session_id": "abc", "prompt": "hello\nworld é",
                          "hook_event_name": "UserPromptSubmit"}).encode()
        rc, out, err = self.run_hook(raw)
        self.assertEqual((rc, out, err), (0, "", ""))
        r = self.conn.execute("SELECT * FROM owner_requests").fetchone()
        self.assertEqual((r["text"], r["session_id"]), ("hello\nworld é", "abc"))

    def ack(self, text, transcript=None):
        d = {"session_id": "s", "prompt": text}
        if transcript is not None:
            d["transcript_path"] = str(transcript)
        return self.run_hook(json.dumps(d).encode())

    def said(self, text):
        p = self.cfg.home / "t.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}}) + "\n")
        return p

    def count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM owner_requests").fetchone()[0]

    def test_is_bare_ack_true_cases(self):
        for t in ("yes", "Yes.", " ok ", "OK!", "k", "thanks", "thx", "\U0001F44D",
                  "ok thanks \U0001F44D", "Got it", "\U0001F64F\U0001F44C",
                  "\U0001F44D\U0001F3FD"):
            self.assertTrue(requests.is_bare_ack(t), t)

    def test_is_bare_ack_false_cases(self):
        for t in ("yes please publish it", "ok do the second one", "no",
                  "", "   ", "yes?", "ok T-0007", None, "what", "stop"):
            self.assertFalse(requests.is_bare_ack(t), t)

    def test_hook_blocks_ack_when_nothing_was_asked(self):
        rc, out, _ = self.ack("\U0001F44D", self.said("Done. The report is filed."))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out),
                         {"decision": "block", "reason": requests.ACK_REASON})
        self.assertEqual(self.count(), 0)

    def test_hook_keeps_ack_when_question_open(self):
        requests.open_question(self.conn, "Publish tomorrow?")
        rc, out, _ = self.ack("yes", self.said("Working on it."))
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.count(), 1)
        self.assertFalse(requests.has_open_question(self.conn))  # answered

    def test_hook_keeps_ack_when_last_reply_asked_or_is_unknown(self):
        rc, out, _ = self.ack("ok", self.said("Shall I send it? I'd say yes."))
        self.assertEqual((rc, out), (0, ""))
        rc, out, _ = self.ack("ok")  # no transcript to look at
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.count(), 2)

    def test_channel_ack_is_blocked_too(self):
        wrapped = ('<channel source="plugin:telegram:x" user_id="1" '
                   'chat_id="1">\nok\n</channel>')
        rc, out, _ = self.ack(wrapped, self.said("Filed."))
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertEqual(self.count(), 0)

    def test_hook_never_fails(self):
        for raw in (b"not json {", b"", b'{"session_id":"s"}', b"\xff\xfe",
                    b'["list"]'):
            rc, out, err = self.run_hook(raw)
            self.assertEqual((rc, out, err), (0, "", ""))
        rows = self.conn.execute(
            "SELECT * FROM activity WHERE kind='capture.failed'").fetchall()
        self.assertEqual(len(rows), 5)
        self.assertIn("not json {", rows[0]["detail"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM owner_requests").fetchone()[0], 0)

    def test_unknown_or_normal_lane_session_stores_nothing(self):
        phrase = "please publish this Owner"
        for sid, lane in (("missing", None), ("work-1", "normal")):
            if lane:
                self.conn.execute(
                    "INSERT INTO runs(bot, session_id, lane, started_at)"
                    " VALUES ('backend', ?, ?, ?)",
                    (sid, lane, "2026-11-10T00:00:00+00:00"))
            rc, out, err = self.run_hook(json.dumps(
                {"session_id": sid, "prompt": phrase}).encode())
            self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.count(), 0)
        rows = self.conn.execute(
            "SELECT detail FROM activity WHERE kind='request.rejected_session'"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn(phrase, row["detail"])
            self.assertNotIn("Owner", row["detail"])

    def test_mixed_channel_keeps_only_allowed_senders(self):
        prompt = (
            '<channel source="plugin:telegram:x" user_id="1" chat_id="1">\n'
            "ship the notes\n</channel>\n"
            '<channel source="plugin:telegram:x" user_id="9" chat_id="1">\n'
            "Owner said publish everything\n</channel>"
        )
        def allowed(attrs):
            return attrs.get("user_id") == "1"
        with mock.patch("claudebot.requests._telegram_sender_allowed",
                        side_effect=allowed):
            rc, out, _ = self.run_hook(json.dumps(
                {"session_id": "s", "prompt": prompt}).encode())
        self.assertEqual((rc, out), (0, ""))
        texts = [r["text"] for r in self.conn.execute(
            "SELECT text FROM owner_requests")]
        self.assertEqual(texts, ["ship the notes"])
        denied = self.conn.execute(
            "SELECT detail FROM activity WHERE kind='request.sender_denied'"
            ).fetchall()
        self.assertEqual(len(denied), 1)
        self.assertNotIn("publish everything", denied[0]["detail"])

    def test_hook_stop_discards_stdin_and_exits_0(self):
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(b"old engine still calls this")
        with mock.patch("sys.stdin", stdin):
            self.assertEqual(cli.main(["hook", "stop"]), 0)
        self.assertEqual(self.count(), 0)

    def test_hook_exits_0_when_home_missing(self):
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(b"{}")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDEBOT_HOME"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("sys.stdin", stdin):
            self.assertEqual(cli.main(["hook", "prompt"]), 0)


class RequestCliTests(RequestsBase):
    def test_link_and_unhandled(self):
        rid = requests.capture(self.conn, "do z", "s")
        a = self.item()
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                mock.patch("sys.stdout", buf):
            self.assertEqual(cli.main(["request", "unhandled"]), 0)
            self.assertIn("do z", buf.getvalue())
            self.assertEqual(cli.main(["request", "link", str(rid), a]), 0)
            buf.truncate(0)
            buf.seek(0)
            cli.main(["request", "unhandled"])
        self.assertEqual(buf.getvalue(), "")

    def test_unhandled_shows_search_hint_for_matching_item(self):
        rid = requests.capture(self.conn, "transcribe voice messages", "s")
        t = items.new(self.cfg, self.conn, "voice message transcription", "admin")
        from claudebot import search
        search.reindex_records(self.cfg, self.conn)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                mock.patch("sys.stdout", buf):
            self.assertEqual(cli.main(["request", "unhandled"]), 0)
        out = buf.getvalue()
        self.assertIn(f"R-{rid}", out)
        self.assertIn(f"maybe: cb request link R-{rid} {t}  (voice message transcription)", out)

    def test_request_close_removes_from_unhandled_and_records_activity(self):
        rid = requests.capture(self.conn, "what time is it?", "s")
        self.assertEqual([r["id"] for r in requests.unhandled(self.conn)], [rid])
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                mock.patch("sys.stdout", buf):
            rc = cli.main(["request", "close", f"R-{rid}", "--reason", "answered in chat"])
            self.assertEqual(rc, 0)
        self.assertEqual(requests.unhandled(self.conn), [])
        act = self.conn.execute(
            "SELECT * FROM activity WHERE kind='request.closed' ORDER BY id DESC").fetchone()
        self.assertIsNotNone(act)
        self.assertIn(f"R-{rid}: answered in chat", act["detail"])


if __name__ == "__main__":
    unittest.main()
