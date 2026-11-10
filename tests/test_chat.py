import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import admin_live, bots, chat, cli, items, requests  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
CB = ENGINE / "bin" / "cb"

FAKE_CLAUDE = """#!{python}
import json, os, signal, subprocess, sys, time
d = os.environ["FAKE_DIR"]
os.makedirs(d + "/calls", exist_ok=True)
n = len(os.listdir(d + "/calls"))
a = sys.argv[1:]
def val(flag):
    return a[a.index(flag) + 1]
with open(val("--append-system-prompt-file")) as f:
    prompt = f.read()
with open(val("--settings")) as f:
    settings = f.read()
sid = val("--session-id")
proj = os.environ["FAKE_PROJECTS"] + "/proj"
os.makedirs(proj, exist_ok=True)
with open(proj + "/" + sid + ".jsonl", "w") as f:
    f.write(json.dumps({{"type": "user", "message": {{"role": "user",
        "content": "hello " + str(n)}}}}) + "\\n")
with open(d + "/calls/" + str(n) + ".json", "w") as f:
    json.dump({{"argv": a, "prompt": prompt, "settings": settings,
               "cwd": os.getcwd(), "cb_bot": os.environ.get("CB_BOT"),
               "home": os.environ.get("CLAUDEBOT_HOME")}}, f)
if n == 0 and os.environ.get("FAKE_ROLLOVER"):
    signal.signal(signal.SIGTERM, lambda *x: sys.exit(143))
    with open(d + "/summary.md", "w") as f:
        f.write("SUMMARY: the Owner wants the invoice report")
    subprocess.run([sys.executable, {cb!r}, "chat-rollover",
                    "--summary-file", d + "/summary.md"], check=True,
                   capture_output=True)
    time.sleep(30)
if n >= 1 and os.environ.get("FAKE_EXIT_AFTER"):
    sys.exit(int(os.environ["FAKE_EXIT_AFTER"]))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.root = root
        self.cfg = Config(root / "home", claude_projects_dir=root / "projects")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="s",
                    persona="p")

    def item(self, title="t", **kw):
        return items.new(self.cfg, self.conn, title, "admin", **kw)


class BriefTests(Base):
    def test_brief_has_changes_stuck_and_needs_owner(self):
        a = self.item("Approve the logo", owner_waiting=True)
        b = self.item("Ship the report")
        items.set_status(self.conn, b, "done", "admin")
        self.conn.execute(
            "INSERT INTO messages(from_bot, to_bot, subject, status, created_at)"
            " VALUES ('admin','admin','Broken thing','failed','x')")
        since = (NOW - timedelta(days=3650)).isoformat(timespec="seconds")
        text = chat.brief(self.cfg, self.conn, since=since)
        self.assertIn(a, text)
        self.assertIn("Approve the logo", text)
        self.assertIn("Ship the report", text)
        self.assertIn("Broken thing", text)
        self.assertRegex(text, r"CT")
        self.assertLess(text.index("Needs you"), text.index("Stuck"))
        self.assertLess(text.index("Stuck"), text.index("Done since"))

    def test_done_before_last_check_is_not_listed(self):
        b = self.item("Old news")
        items.set_status(self.conn, b, "done", "admin")
        text = chat.brief(self.cfg, self.conn,
                          since="2999-01-01T00:00:00+00:00")
        self.assertNotIn("Old news", text)

    def test_brief_is_capped(self):
        for i in range(500):
            self.item(f"Item number {i} " + "x" * 80, owner_waiting=True)
        for i in range(50):
            requests.capture(self.conn, f"request {i} " + "y" * 300, "s")
        text = chat.brief(self.cfg, self.conn)
        self.assertLessEqual(len(text.encode()), 2048)
        self.assertIn("500", text)
        self.assertIn("50", text)
        self.assertIn("cb open", text)
        self.assertIn("more", text)

    def test_brief_lists_unhandled_requests_first(self):
        self.item("Needs owner", owner_waiting=True)
        requests.capture(self.conn, "please do the thing\nsecond line", "s")
        text = chat.brief(self.cfg, self.conn)
        self.assertLess(text.index("Unhandled requests"), text.index("Needs you"))
        self.assertIn("please do the thing", text)
        self.assertNotIn("\nsecond line", text)

    def test_empty_brief(self):
        text = chat.brief(self.cfg, self.conn)
        self.assertIn("Nothing", text)

    def test_last_check_is_tracked_in_meta(self):
        self.assertIsNone(chat.last_check(self.conn))
        chat.mark_checked(self.conn, "2026-11-09T10:00:00+00:00")
        self.assertEqual(chat.last_check(self.conn),
                         "2026-11-09T10:00:00+00:00")


class TokenTests(unittest.TestCase):
    def write(self, lines):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        p = Path(d) / "t.jsonl"
        p.write_text("\n".join(json.dumps(x) if not isinstance(x, str) else x
                               for x in lines) + "\n")
        return p

    def assistant(self, inp, cc, cr, out):
        return {"type": "assistant", "message": {"role": "assistant", "usage": {
            "input_tokens": inp, "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr, "output_tokens": out}}}

    def test_context_tokens_reads_last_usage(self):
        p = self.write([self.assistant(1, 2, 3, 4), "not json",
                        {"type": "user", "message": {"content": "x"}},
                        self.assistant(10, 200, 3000, 40),
                        {"type": "user", "message": {"content": "y"}}])
        self.assertEqual(chat.context_tokens(p), 10 + 200 + 3000 + 40)

    def test_missing_usage_returns_zero(self):
        p = self.write([{"type": "assistant", "message": {"role": "assistant"}},
                        {"type": "user", "message": {"content": "x"}}])
        self.assertEqual(chat.context_tokens(p), 0)
        self.assertEqual(chat.context_tokens(p.with_name("nope.jsonl")), 0)
        self.assertEqual(chat.context_tokens(None), 0)

    def test_sidechain_is_ignored(self):
        side = self.assistant(999999, 0, 0, 0)
        side["isSidechain"] = True
        p = self.write([self.assistant(5, 0, 0, 5), side])
        self.assertEqual(chat.context_tokens(p), 10)


class StopHookTests(Base):
    def run_hook(self, payload, tokens):
        tp = self.root / "t.jsonl"
        tp.write_text(json.dumps({"type": "assistant", "message": {
            "usage": {"input_tokens": tokens, "output_tokens": 0}}}) + "\n")
        payload = dict({"transcript_path": str(tp), "session_id": "s"},
                       **payload)
        stdin = types.SimpleNamespace(
            buffer=io.BytesIO(json.dumps(payload).encode()))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                mock.patch("sys.stdin", stdin), redirect_stdout(out):
            rc = chat.cmd_hook_stop(None)
        return rc, out.getvalue()

    def test_stop_hook_below_threshold_passes(self):
        rc, out = self.run_hook({"stop_hook_active": False}, 99999)
        self.assertEqual((rc, out), (0, ""))

    def test_stop_hook_at_threshold_blocks_once(self):
        rc, out = self.run_hook({"stop_hook_active": False}, 100000)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertIn("chat-rollover", data["reason"])
        self.assertIn("summary", data["reason"])
        # the continuation it caused must not be blocked again
        rc, out = self.run_hook({"stop_hook_active": True}, 150000)
        self.assertEqual((rc, out), (0, ""))

    def test_stop_hook_does_not_nag_until_20k_more_tokens(self):
        rc, out = self.run_hook({"stop_hook_active": False}, 100000)
        self.assertEqual(json.loads(out)["decision"], "block")
        rc, out = self.run_hook({"stop_hook_active": False}, 119999)
        self.assertEqual((rc, out), (0, ""))
        rc, out = self.run_hook({"stop_hook_active": False}, 120000)
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_stop_hook_never_breaks_the_chat(self):
        out = io.StringIO()
        stdin = types.SimpleNamespace(buffer=io.BytesIO(b"{broken"))
        with mock.patch("sys.stdin", stdin), redirect_stdout(out):
            self.assertEqual(chat.cmd_hook_stop(None), 0)
        self.assertEqual(out.getvalue(), "")
        rc, out = self.run_hook({"stop_hook_active": False,
                                 "transcript_path": "/nonexistent"}, 0)
        self.assertEqual((rc, out), (0, ""))

    def test_no_second_block_while_rollover_is_pending(self):
        chat.chat_dir(self.cfg).mkdir(parents=True, exist_ok=True)
        chat.marker_path(self.cfg).write_text("x")
        rc, out = self.run_hook({"stop_hook_active": False}, 500000)
        self.assertEqual((rc, out), (0, ""))


class SettingsTests(Base):
    def test_settings_has_both_hooks_with_absolute_path(self):
        s = chat.settings_for_chat(self.cfg)
        cmds = {ev: s["hooks"][ev][0]["hooks"][0]["command"]
                for ev in ("UserPromptSubmit", "Stop")}
        self.assertTrue(cmds["UserPromptSubmit"].endswith("/bin/cb hook prompt"))
        self.assertTrue(cmds["Stop"].endswith("/bin/cb hook stop"))
        self.assertTrue(cmds["Stop"].startswith("/"))


class LockTests(Base):
    def test_owner_connected_tracks_lock(self):
        self.assertFalse(chat.owner_connected(self.cfg))
        lock = chat.acquire_lock(self.cfg)
        self.assertTrue(chat.owner_connected(self.cfg))
        with self.assertRaises(chat.ChatBusy) as cm:
            chat.acquire_lock(self.cfg)
        self.assertIn(str(os.getpid()), str(cm.exception))
        lock.close()
        self.assertFalse(chat.owner_connected(self.cfg))

    def test_acquire_rides_out_a_short_probe(self):
        import threading
        probe = chat.acquire_lock(self.cfg)
        threading.Timer(0.1, probe.close).start()
        lock = chat.acquire_lock(self.cfg, retries=3, delay=0.2)
        self.assertTrue(chat.owner_connected(self.cfg))
        lock.close()

    def test_lock_is_released_on_a_crash(self):
        code = ("import sys, os; sys.path.insert(0, %r);"
                "from claudebot import chat; from claudebot.config import Config;"
                "l = chat.acquire_lock(Config(%r)); print('held', flush=True);"
                "os.kill(os.getpid(), 9)" % (str(ENGINE), str(self.cfg.home)))
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        p.wait()
        p.stdout.close()
        self.assertFalse(chat.owner_connected(self.cfg))


class LoopTests(Base):
    def setUp(self):
        super().setUp()
        self.fake = self.root / "fakeclaude"
        self.fake.write_text(FAKE_CLAUDE.format(python=sys.executable,
                                                cb=str(CB)))
        self.fake.chmod(0o755)
        self.cfg.claude_bin = str(self.fake)
        self.fake_dir = self.root / "fake"
        env = {"FAKE_DIR": str(self.fake_dir),
               "FAKE_PROJECTS": str(self.cfg.claude_projects_dir)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def calls(self):
        d = self.fake_dir / "calls"
        return [json.loads((d / f).read_text())
                for f in sorted(os.listdir(d), key=lambda x: int(x[:-5]))]

    def run_loop(self):
        out = io.StringIO()
        with redirect_stdout(out):
            chat.chat_loop(self.cfg)
        return out.getvalue()

    def test_normal_exit_ends_the_loop_and_records_the_run(self):
        requests.capture(self.conn, "build me a report", "s")
        self.run_loop()
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(os.path.realpath(c["cwd"]),
                         os.path.realpath(self.cfg.bots_dir / "admin"))
        self.assertEqual(c["cb_bot"], "admin")
        self.assertEqual(c["home"], str(self.cfg.home))
        self.assertIn("build me a report", c["prompt"])
        self.assertIn("# Soul", c["prompt"])
        a = c["argv"]
        for flag in ("--dangerously-skip-permissions", "--strict-mcp-config"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--setting-sources") + 1], "project,local")
        self.assertEqual(a[a.index("--model") + 1], "sonnet")
        self.assertIn("hook stop", c["settings"])
        run = self.conn.execute("SELECT * FROM runs WHERE lane='chat'").fetchone()
        self.assertEqual(run["bot"], "admin")
        self.assertIsNotNone(run["ended_at"])
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["reason"], "ok")
        self.assertFalse(chat.owner_connected(self.cfg))
        self.assertFalse(chat.pid_path(self.cfg).exists())
        self.assertIsNotNone(chat.last_check(self.conn))

    def test_rollover_restarts_with_summary_and_archives(self):
        with mock.patch.dict(os.environ, {"FAKE_ROLLOVER": "1"}):
            out = self.run_loop()
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("SUMMARY: the Owner", calls[0]["prompt"])
        self.assertIn("SUMMARY: the Owner wants the invoice report",
                      calls[1]["prompt"])
        sid0 = calls[0]["argv"][calls[0]["argv"].index("--session-id") + 1]
        sid1 = calls[1]["argv"][calls[1]["argv"].index("--session-id") + 1]
        self.assertNotEqual(sid0, sid1)
        self.assertIn("ontinuing", out)
        self.assertIn("Brief as of", out)  # printed once, on the first start
        self.assertEqual(out.count("Brief as of"), 1)
        self.assertEqual(out.count("ontinuing"), 1)
        self.assertFalse(chat.marker_path(self.cfg).exists())
        self.assertTrue(chat.rollover_path(self.cfg).is_file())
        runs = self.conn.execute(
            "SELECT * FROM runs WHERE lane='chat' ORDER BY id").fetchall()
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r["ended_at"] for r in runs))
        self.assertEqual(runs[0]["reason"], "rollover")
        archived = list(self.cfg.archive_dir.rglob(
            f"admin-{runs[0]['id']}.jsonl.gz"))
        self.assertEqual(len(archived), 1)
        self.assertFalse(
            (self.cfg.claude_projects_dir / "proj" / f"{sid0}.jsonl").exists())

    def test_failed_restart_keeps_the_summary_for_the_next_chat(self):
        with mock.patch.dict(os.environ, {"FAKE_ROLLOVER": "1",
                                          "FAKE_EXIT_AFTER": "1"}):
            self.run_loop()
        self.assertEqual(len(self.calls()), 2)
        self.assertTrue(chat.marker_path(self.cfg).exists())
        self.run_loop()  # the next `cb chat`
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        self.assertIn("SUMMARY: the Owner wants the invoice report",
                      calls[2]["prompt"])
        self.assertFalse(chat.marker_path(self.cfg).exists())  # ended well

    def test_consumed_summary_does_not_silence_the_stop_hook_forever(self):
        # marker with the token this session was started with: hook may block
        chat.chat_dir(self.cfg).mkdir(parents=True, exist_ok=True)
        chat.marker_path(self.cfg).write_text("tok1\n")
        h = StopHookTests("test_stop_hook_below_threshold_passes")
        h.cfg, h.root = self.cfg, self.root
        with mock.patch.dict(os.environ, {chat.TOKEN_ENV: "tok1"}):
            rc, out = h.run_hook({"stop_hook_active": False}, 150000)
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_nonzero_exit_without_marker_ends_the_loop(self):
        with mock.patch.dict(os.environ, {"FAKE_EXIT": "3"}):
            self.run_loop()
        self.assertEqual(len(self.calls()), 1)
        run = self.conn.execute("SELECT * FROM runs WHERE lane='chat'").fetchone()
        self.assertEqual(run["exit_code"], 3)
        self.assertEqual(run["reason"], "error")

    def test_pending_summary_survives_a_crash_and_is_used_next_time(self):
        chat.chat_dir(self.cfg).mkdir(parents=True, exist_ok=True)
        chat.rollover_path(self.cfg).write_text("LEFTOVER SUMMARY")
        chat.marker_path(self.cfg).write_text("x")
        self.run_loop()
        self.assertIn("LEFTOVER SUMMARY", self.calls()[0]["prompt"])
        self.assertFalse(chat.marker_path(self.cfg).exists())
        self.assertTrue(chat.rollover_path(self.cfg).is_file())

    def test_loop_crash_keeps_the_summary_file(self):
        chat.chat_dir(self.cfg).mkdir(parents=True, exist_ok=True)
        chat.rollover_path(self.cfg).write_text("KEEP ME")
        chat.marker_path(self.cfg).write_text("x")
        with mock.patch.object(chat, "_launch", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
                chat.chat_loop(self.cfg)
        self.assertEqual(chat.rollover_path(self.cfg).read_text(), "KEEP ME")
        self.assertTrue(chat.marker_path(self.cfg).exists())
        self.assertFalse(chat.owner_connected(self.cfg))
        run = self.conn.execute("SELECT * FROM runs WHERE lane='chat'").fetchone()
        self.assertIsNotNone(run["ended_at"])


class RolloverCommandTests(Base):
    def test_rollover_needs_an_open_chat(self):
        with self.assertRaises(ValueError):
            chat.rollover(self.cfg, self.conn, "summary")

    def test_rollover_writes_files_and_signals_the_pid_file_process(self):
        lock = chat.acquire_lock(self.cfg)
        self.addCleanup(lock.close)
        chat.pid_path(self.cfg).write_text("4242")
        sent = []
        chat.rollover(self.cfg, self.conn, "the summary",
                      kill=lambda pid, sig: sent.append((pid, sig)))
        self.assertEqual(sent, [(4242, signal.SIGTERM)])
        self.assertEqual(chat.rollover_path(self.cfg).read_text(), "the summary")
        self.assertTrue(chat.marker_path(self.cfg).exists())

    def test_empty_summary_is_refused(self):
        lock = chat.acquire_lock(self.cfg)
        self.addCleanup(lock.close)
        chat.pid_path(self.cfg).write_text("4242")
        with self.assertRaises(ValueError):
            chat.rollover(self.cfg, self.conn, "  \n", kill=lambda *a: None)
        self.assertFalse(chat.marker_path(self.cfg).exists())

    def test_cli_chat_requires_a_terminal(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                redirect_stderr(err):
            rc = cli.main(["chat"])
        self.assertEqual(rc, 1)
        self.assertIn("terminal", err.getvalue())

    def test_cli_chat_busy_prints_holder(self):
        lock = chat.acquire_lock(self.cfg)
        self.addCleanup(lock.close)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                mock.patch("claudebot.chat._have_terminal", return_value=True), \
                redirect_stderr(err):
            rc = cli.main(["chat"])
        self.assertEqual(rc, 1)
        self.assertIn(str(os.getpid()), err.getvalue())

    def test_cli_chat_rollover_reads_the_file(self):
        f = self.root / "s.md"
        f.write_text("")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
                redirect_stderr(err):
            self.assertEqual(
                cli.main(["chat-rollover", "--summary-file", str(f)]), 1)
            self.assertEqual(
                cli.main(["chat-rollover", "--summary-file",
                          str(self.root / "missing")]), 1)


class ComposeMemoryTests(Base):
    """Memory left the identity prompt; both chat prompts must still carry it."""
    def test_chat_and_live_prompts_keep_memory(self):
        (self.cfg.bots_dir / "admin" / "MEMORY.md").write_text("ADMIN-MEM-XYZ")
        chat_p = chat._compose_prompt(self.cfg, self.conn, "BRIEF", None)
        live_p = admin_live.Live().compose(self.cfg, self.conn, "BRIEF", None)
        for p in (chat_p, live_p):
            self.assertIn("ADMIN-MEM-XYZ", p)
            self.assertLess(p.index("ADMIN-MEM-XYZ"), p.index("BRIEF"))


if __name__ == "__main__":
    unittest.main()
