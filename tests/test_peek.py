"""Telegram status peek: fixed labels, rate limits, tolerant parsing, and
the rule that a failure in the status path never touches the work."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from claudebot import admin_live, chat, cli, peek  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


def tool(name, **tool_input):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "private words"},
        {"type": "tool_use", "name": name, "input": tool_input}]}})


class LabelTests(unittest.TestCase):
    def test_fixed_labels(self):
        self.assertEqual(peek.label_for("Read"), "Reading files")
        self.assertEqual(peek.label_for("Grep"), "Searching")
        self.assertEqual(peek.label_for("Edit"), "Editing files")
        self.assertEqual(peek.label_for("Bash", {"command": "ls /secret"}),
                         "Running a command")
        self.assertEqual(peek.label_for("WebSearch"), "Looking something up")
        self.assertEqual(peek.label_for("Task"), "Asking a helper")
        self.assertEqual(peek.label_for("SomethingNew"), "Working")
        self.assertEqual(peek.label_for(None), "Working")
        self.assertEqual(peek.label_for("mcp__plugin_telegram_telegram__reply"),
                         "Writing the reply")
        self.assertEqual(peek.label_for("mcp__other__thing"),
                         "Using a connected service")

    def test_tests_are_recognised_from_a_fixed_list_only(self):
        self.assertEqual(peek.label_for(
            "Bash", {"command": "python3 -m unittest discover -s tests"}),
            "Running tests")
        self.assertEqual(peek.label_for("Bash", {"command": "  pytest -q"}),
                         "Running tests")
        self.assertEqual(peek.label_for(
            "Bash", {"command": "cb send tl \"subject\" \"body\""}),
            "Handing work to a bot")
        self.assertEqual(peek.label_for("Bash", {"command": "rm pytest.ini"}),
                         "Running a command")
        self.assertEqual(peek.label_for("Bash", {"command": 7}),
                         "Running a command")

    def test_no_argument_ever_reaches_the_text(self):
        secret = "/home/x/payroll-2026.csv"
        lines = [tool("Read", file_path=secret),
                 tool("Bash", command=f"cat {secret}"),
                 tool("Grep", pattern="salary")]
        label, waiting = peek.scan(lines)
        text = peek.status_text(label, 125, waiting)
        self.assertEqual(text, "Searching · 2 min")
        for word in (secret, "payroll", "salary", "private words", "cat"):
            self.assertNotIn(word, text)
        every = set(peek.LABELS.values()) | {peek.WORKING, peek.REPLYING}
        for label in every:
            self.assertRegex(label, r"^[A-Za-z ]+$")

    def test_status_text(self):
        self.assertEqual(peek.status_text("Working", 5),
                         "Working · under a minute")
        self.assertEqual(peek.status_text("Working", 61, 2),
                         "Working · 1 min · 2 more waiting")


class ScanTests(unittest.TestCase):
    def test_malformed_lines_are_skipped(self):
        lines = ["", "not json", "[1, 2]", "{\"type\": \"assistant\"}",
                 json.dumps({"type": "assistant", "message": "text"}),
                 json.dumps({"type": "assistant",
                             "message": {"content": [7, None, {"type": "x"}]}}),
                 json.dumps({"type": "assistant", "isSidechain": True,
                             "message": {"content": [
                                 {"type": "tool_use", "name": "Edit"}]}}),
                 tool("Read"), "{\"broken\": "]
        self.assertEqual(peek.scan(lines), ("Reading files", 0))

    def test_queue_count(self):
        q = lambda op: json.dumps({"type": "queue-operation", "operation": op})  # noqa: E731
        self.assertEqual(peek.scan([q("enqueue"), q("enqueue")])[1], 2)
        self.assertEqual(peek.scan([q("enqueue"), q("dequeue"),
                                    q("dequeue")])[1], 0)
        self.assertEqual(peek.scan([q("enqueue"), q("enqueue"),
                                    q("popAll")])[1], 0)
        self.assertEqual(peek.scan([q("mystery")], "Working", 3)[1], 3)


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.transcript = self.dir / "session.jsonl"
        self.transcript.write_text(tool("Edit") + "\n")   # an earlier turn
        self.now = [1000.0]
        self.calls = []
        self.down = False
        self.token = "TOKEN"

    def api(self, token, method, params):
        if self.down:
            raise OSError("down")
        self.calls.append((method, dict(params)))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 77}}
        return {"ok": True}

    def make(self, **kw) -> peek.Peek:
        return peek.Peek(self.dir, token_fn=lambda: self.token,
                         transcript_fn=lambda: self.transcript,
                         chats_fn=lambda: [42], api_fn=self.api,
                         clock=lambda: self.now[0], **kw)

    def step(self, p, seconds=1, times=1):
        for _ in range(times):
            self.now[0] += seconds
            p.tick()

    def add(self, line):
        with open(self.transcript, "a") as f:
            f.write(line + "\n")

    def methods(self):
        return [m for m, _ in self.calls]


class PeekTests(Harness):
    def test_nothing_happens_without_a_turn(self):
        p = self.make()
        self.step(p, times=30)
        self.assertEqual(self.calls, [])

    def test_quick_work_only_shows_typing(self):
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p, times=8)
        self.assertEqual(set(self.methods()), {"sendChatAction"})
        self.assertLessEqual(len(self.calls), 3)      # every few seconds
        peek.note_turn_ended(self.dir)
        self.step(p)
        self.assertNotIn("sendMessage", self.methods())
        self.assertNotIn("deleteMessage", self.methods())

    def test_one_silent_message_edited_then_removed(self):
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p, times=10)
        self.add(tool("Read", file_path="/x"))
        self.step(p, times=2)
        sent = [c for c in self.calls if c[0] == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["disable_notification"], "true")
        self.assertEqual(sent[0][1]["chat_id"], "42")
        # the earlier turn's "Editing files" is not this turn's status
        self.assertTrue(sent[0][1]["text"].startswith(("Working", "Reading")))
        self.add(tool("Bash", command="pytest"))
        self.step(p, times=6)
        edits = [c for c in self.calls if c[0] == "editMessageText"]
        self.assertEqual(edits[-1][1]["text"], "Running tests · under a minute")
        self.assertEqual(edits[-1][1]["message_id"], 77)
        peek.note_turn_ended(self.dir)
        self.step(p)
        self.assertEqual(self.calls[-1], ("deleteMessage", {
            "chat_id": "42", "message_id": 77}))
        self.assertEqual(len([c for c in self.calls
                              if c[0] == "sendMessage"]), 1)

    def test_edits_are_rate_limited_and_only_on_change(self):
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p, times=11)
        before = len(self.calls)
        for name in ("Read", "Grep", "Edit", "Read", "Grep", "Edit", "Read",
                     "Grep", "Edit", "Read"):
            self.add(tool(name))
            self.step(p)                       # ten changes in ten seconds
        edits = len(self.calls) - before
        self.assertLessEqual(edits, 2)
        self.assertGreaterEqual(edits, 1)
        # nothing changes for a while: no calls at all inside the minute
        self.now[0] = 1000.0 + 61              # just past the first minute
        p.tick()
        quiet = len(self.calls)
        self.step(p, times=40)
        self.assertEqual(len(self.calls), quiet)
        # the minute changing is worth one edit
        self.step(p, times=25)
        self.assertEqual(len(self.calls), quiet + 1)
        self.assertIn("2 min", self.calls[-1][1]["text"])

    def test_queue_count_is_shown(self):
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p)
        self.add(json.dumps({"type": "queue-operation",
                             "operation": "enqueue"}))
        self.step(p, times=11)
        self.assertIn("1 more waiting", self.calls[-1][1]["text"])

    def test_a_second_message_keeps_the_clock_and_the_one_status(self):
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p, times=70)
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p, times=6)
        self.assertEqual(self.methods().count("sendMessage"), 1)
        self.assertIn("1 min", self.calls[-1][1]["text"])

    def test_switched_off_does_nothing(self):
        p = self.make(enabled=False)
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p, times=30)
        self.assertEqual(self.calls, [])

    def test_no_token_means_no_calls(self):
        self.token = None
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p, times=30)
        self.assertEqual(self.calls, [])

    def test_a_failed_telegram_call_is_silent_and_pauses_the_peek(self):
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.down = True
        self.step(p, times=20)                 # must not raise
        self.down = False
        self.step(p, times=20)                 # still inside the pause
        self.assertEqual(self.calls, [])
        self.step(p, times=peek.PAUSE_AFTER_ERROR)
        self.assertIn("sendMessage", self.methods())   # it recovers

    def test_a_broken_transcript_or_turn_file_never_raises(self):
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.transcript.write_bytes(b"\xff\xfe{{{{\n\x00\x00\nnot json\n")
        self.step(p, times=15)
        self.assertIn("sendMessage", self.methods())
        self.transcript.unlink()
        self.step(p, times=10)
        peek.turn_path(self.dir).write_text("{not json")
        self.step(p, times=3)                  # reads as "no turn": removed
        self.assertEqual(self.methods()[-1], "deleteMessage")
        p2 = peek.Peek(self.dir, token_fn=mock.Mock(side_effect=RuntimeError),
                       clock=lambda: self.now[0])
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        p2.tick()                              # an unexpected error: silent
        p2.close()

    def test_a_stale_turn_file_is_ignored(self):
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.assertIsNotNone(peek.read_turn(self.dir, self.now[0] + 10))
        self.assertIsNone(peek.read_turn(self.dir,
                                         self.now[0] + peek.MAX_TURN + 1))

    def test_close_removes_the_status_message(self):
        p = self.make()
        peek.note_turn_started(self.dir, ["42"], now=self.now[0])
        self.step(p, times=12)
        p.close()
        self.assertEqual(self.methods()[-1], "deleteMessage")

    def test_only_numeric_chat_ids_are_kept(self):
        peek.note_turn_started(self.dir, ["42", "?", "", "-100123", "a;b"],
                               now=self.now[0])
        self.assertEqual(peek.read_turn(self.dir, self.now[0])["chats"],
                         ["42", "-100123"])
        mode = os.stat(peek.turn_path(self.dir)).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_token_is_read_from_the_plugin_file(self):
        env = self.dir / ".env"
        self.assertIsNone(peek.read_token(env))
        env.write_text("OTHER=1\nTELEGRAM_BOT_TOKEN=abc:def\n")
        self.assertEqual(peek.read_token(env), "abc:def")


class WiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.cfg = Config(self.home)
        init_db(self.cfg)
        self.state = self.home / ".admin-live"
        self.state.mkdir()
        self.tg = Path(self._tmp.name) / "tg"
        self.tg.mkdir()
        (self.tg / "access.json").write_text(json.dumps({"allowFrom": [42]}))
        conn = connect(self.cfg)
        conn.execute("INSERT INTO runs(bot, session_id, lane, started_at)"
                     " VALUES ('admin','S1','live','2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        self.env = {"CLAUDEBOT_HOME": str(self.home),
                    "TELEGRAM_STATE_DIR": str(self.tg)}

    def hook(self, sub, payload, state=True):
        env = dict(self.env)
        if state:
            env["CB_STATE_DIR"] = str(self.state)
        stdin = mock.Mock()
        stdin.buffer.read.return_value = json.dumps(payload).encode()
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "stdin", stdin), redirect_stdout(out):
            os.environ.pop("CB_BOT", None)
            rc = cli.main(["hook", sub])
        self.assertEqual(rc, 0)
        return out.getvalue()

    def telegram(self, user="42", text="please check the report"):
        return {"session_id": "S1", "prompt":
                f'<channel source="plugin:telegram:x" user_id="{user}" '
                f'chat_id="{user}">\n{text}\n</channel>'}

    def test_owner_message_starts_the_turn_and_stop_ends_it(self):
        self.hook("prompt", self.telegram())
        turn = peek.read_turn(self.state)
        self.assertEqual(turn["chats"], ["42"])
        self.hook("stop", {"session_id": "S1"})
        self.assertIsNone(peek.read_turn(self.state))

    def test_a_stranger_or_a_system_line_starts_nothing(self):
        self.hook("prompt", self.telegram(user="666"))
        self.assertIsNone(peek.read_turn(self.state))
        self.hook("prompt", {"session_id": "S1",
                             "prompt": "[system] two replies waiting"})
        self.assertIsNone(peek.read_turn(self.state))
        # typed at the terminal: the Owner is looking at the screen already
        self.hook("prompt", {"session_id": "S1", "prompt": "do the thing"})
        self.assertIsNone(peek.read_turn(self.state))

    def test_an_ordinary_chat_never_writes_a_turn_file(self):
        self.hook("prompt", self.telegram(), state=False)
        self.hook("stop", {"session_id": "S1"}, state=False)
        self.assertFalse((self.home / ".chat" / peek.TURN_FILE).exists())
        self.assertIsNone(peek.read_turn(self.state))

    def test_the_setting_turns_it_off_and_only_live_sessions_get_the_hook(self):
        live = admin_live.Live()
        self.assertIsInstance(live.peek(self.cfg, "S1"), peek.Peek)
        self.cfg.status_peek = "off"
        self.assertIsNone(live.peek(self.cfg, "S1"))
        self.assertIn("Stop", live.settings(self.cfg)["hooks"])
        self.assertNotIn("Stop", chat.settings_for_chat(self.cfg)["hooks"])
        (self.home / "config.json").write_text('{"status_peek": "off"}')
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(Config.load().status_peek, "off")
        (self.home / "config.json").write_text('{"status_peek": "maybe"}')
        with mock.patch.dict(os.environ, self.env), \
                self.assertRaises(RuntimeError):
            Config.load()

    def test_wait_loop_ticks_the_peek_and_survives_it(self):
        class Proc:
            def __init__(self):
                self.n = 0

            def wait(self, timeout=None):
                self.n += 1
                if self.n < 4:
                    raise chat.subprocess.TimeoutExpired("claude", timeout)
                return 0

        ticks = []
        status = mock.Mock()
        status.tick.side_effect = lambda: ticks.append(1)
        self.assertEqual(chat._wait(Proc(), self.cfg, peek=status), 0)
        self.assertEqual(len(ticks), 3)

    def test_the_module_makes_no_model_call(self):
        text = (ROOT / "engine" / "claudebot" / "peek.py").read_text()
        for word in ("claude_bin", "subprocess", "anthropic", "import runner"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
