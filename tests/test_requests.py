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
        items.pause(self.cfg, self.conn, its[0], "note alpha", "admin")
        items.pause(self.cfg, self.conn, its[1], "note beta", "admin")
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


if __name__ == "__main__":
    unittest.main()
