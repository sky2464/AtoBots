import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from atobots import bots, items, messages, runner  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import connect, init_db  # noqa: E402

# A stand-in for `sudo systemd-run ...`: applies --setenv and
# --working-directory, skips the other options and runs the command.
FAKE_SUDO = f"""#!{sys.executable}
import os, sys
args = sys.argv[1:]
if args and args[0] == "systemctl":
    open(os.environ["FAKE_DIR"] + "/stopped", "w").write(" ".join(args))
    sys.exit(0)
assert args[0] == "systemd-run", args
env, cwd, i = dict(os.environ), None, 1
while i < len(args):
    a = args[i]
    if a == "-p":
        i += 2
        continue
    if a == "--unit" or a.startswith("--unit="):
        i += 1 if a.startswith("--unit=") else 2
        continue
    if a.startswith("--setenv="):
        k, v = a[len("--setenv="):].split("=", 1)
        env[k] = v
    elif a.startswith("--working-directory="):
        cwd = a[len("--working-directory="):]
    elif not a.startswith("-"):
        break
    i += 1
if cwd:
    os.chdir(cwd)
os.execve(args[i], args[i:], env)
"""

FAKE_CLAUDE = f"""#!{sys.executable}
import json, os, re, stat, sys
d = os.environ["FAKE_DIR"]
argv = sys.argv[1:]
rec = {{"argv": argv, "cb_bot": os.environ.get("CB_BOT"),
       "home": os.environ.get("ATOBOTS_HOME"), "cwd": os.getcwd()}}
for flag in ("--mcp-config", "--append-system-prompt-file"):
    if flag in argv:
        p = argv[argv.index(flag) + 1]
        rec[flag] = {{"path": p, "mode": stat.S_IMODE(os.stat(p).st_mode),
                     "text": open(p).read()}}
if "-p" in argv:
    prompt = argv[argv.index("-p") + 1]
    rec["-p"] = prompt
    m = re.search(r"Read the task file at (\\S+) and follow", prompt)
    if m:
        tp = m.group(1)
        rec["task_file"] = {{"path": tp,
                            "mode": stat.S_IMODE(os.stat(tp).st_mode),
                            "text": open(tp).read()}}
json.dump(rec, open(d + "/rec.json", "w"))
mode = open(d + "/mode").read().strip()
if mode == "ok":
    print(json.dumps({{"type": "result", "is_error": False, "result": "done",
        "session_id": "sess-from-claude", "terminal_reason": "completed",
        "usage": {{"input_tokens": 10, "cache_creation_input_tokens": 100,
                  "cache_read_input_tokens": 1000, "output_tokens": 7}},
        "modelUsage": {{"claude-sonnet-5-0": {{"inputTokens": 10}}}}}}))
elif mode == "usage":
    print(json.dumps({{"type": "result", "is_error": True,
        "result": "You've hit your session limit", "terminal_reason": "api_error",
        "api_error_status": 429, "usage": {{"input_tokens": 0, "output_tokens": 0}}}}))
    sys.exit(1)
elif mode == "garbage":
    print('{{"type": "res')
elif mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(1)
"""


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.fake = root / "fake"
        (self.fake / "bin").mkdir(parents=True)
        sudo = self.fake / "bin" / "sudo"
        sudo.write_text(FAKE_SUDO)
        sudo.chmod(0o755)
        claude = self.fake / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        self.set_mode("ok")
        patcher = mock.patch.dict(os.environ, {
            "PATH": f"{self.fake / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DIR": str(self.fake)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = Config(root / "home", claude_bin=str(claude))
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        for n in ("admin", "backend"):
            bots.create(self.cfg, self.conn, n, role=n, soul="I am " + n,
                        persona="p", model="sonnet", effort="medium")

    def set_mode(self, mode):
        (self.fake / "mode").write_text(mode)

    def rec(self):
        return json.loads((self.fake / "rec.json").read_text())

    def bot(self, name="backend"):
        return bots.get(self.conn, name)

    def msgs(self, *specs):
        """Each spec holds keyword arguments for messages.send (admin to backend)."""
        for s in specs:
            kw = dict(frm="admin", to="backend", subject="s", body="b")
            kw.update(s)
            messages.send(self.conn, **kw)
        return messages.pending_for(self.conn, "backend")


class CommandTests(RunnerBase):
    def test_command_has_limits(self):
        cmd = runner.build_command(self.cfg, self.bot(), "sid-1",
                                   "/tmp/ident.md",
                                   task_file="/tmp/task.md",
                                   unit="atobots-run-1.service")
        self.assertEqual(cmd[:3], ["sudo", "systemd-run", "--wait"])
        for flag in ("--collect", "--pipe", f"--uid={self.cfg.run_user}"):
            self.assertIn(flag, cmd)
        self.assertIn("--unit=atobots-run-1.service", cmd)
        self.assertIn("MemoryMax=1200M", cmd)
        self.assertIn("RuntimeMaxSec=1800", cmd)
        self.assertEqual(cmd[cmd.index("MemoryMax=1200M") - 1], "-p")
        self.assertEqual(cmd[cmd.index("RuntimeMaxSec=1800") - 1], "-p")
        self.assertEqual(cmd[cmd.index("MemorySwapMax=0") - 1], "-p")
        self.assertIn(f"--working-directory={self.cfg.bots_dir / 'backend'}", cmd)
        self.assertIn("--setenv=CB_BOT=backend", cmd)
        self.assertIn(f"--setenv=ATOBOTS_HOME={self.cfg.home}", cmd)
        self.assertIn(f"--setenv=HOME={self.cfg.run_home}", cmd)
        tail = cmd[cmd.index(self.cfg.claude_bin):]
        self.assertIn("/tmp/task.md", tail[tail.index("-p") + 1])
        self.assertIn("--dangerously-skip-permissions", tail)
        self.assertEqual(tail[tail.index("--output-format") + 1], "json")
        self.assertEqual(tail[tail.index("--setting-sources") + 1], "project,local")
        self.assertEqual(tail[tail.index("--session-id") + 1], "sid-1")
        self.assertEqual(tail[tail.index("--append-system-prompt-file") + 1],
                         "/tmp/ident.md")
        self.assertIn("--strict-mcp-config", tail)

    def test_task_file_keeps_body_off_argv(self):
        cmd = runner.build_command(
            self.cfg, self.bot(), "sid-1", "/tmp/ident.md",
            task_file="/tmp/secret-task.md")
        tail = cmd[cmd.index(self.cfg.claude_bin):]
        prompt = tail[tail.index("-p") + 1]
        self.assertIn("/tmp/secret-task.md", prompt)
        self.assertNotIn("SECRET-BODY", prompt)
        self.assertLess(len(prompt), 200)

    def test_limits_follow_config(self):
        self.cfg.run_memory_max = "900M"
        self.cfg.run_seconds = 60
        cmd = runner.build_command(self.cfg, self.bot(), "s", "/tmp/i",
                                   task_file="/tmp/t.task.md")
        self.assertIn("MemoryMax=900M", cmd)
        self.assertIn("RuntimeMaxSec=60", cmd)

    def test_connectors_bot_not_strict(self):
        bots.update(self.cfg, self.conn, "backend", "admin", connectors=True)
        cmd = runner.build_command(self.cfg, self.bot(), "s", "/tmp/i",
                                   task_file="/tmp/t.task.md")
        self.assertNotIn("--strict-mcp-config", cmd)

    def test_command_applies_bot_setup(self):
        (self.cfg.home / "plugins" / "pl").mkdir(parents=True)
        bots.update(self.cfg, self.conn, "backend", "admin", plugins=["pl"])
        cmd = runner.build_command(self.cfg, self.bot(), "s", "/tmp/i",
                                   task_file="/tmp/t.task.md",
                                   mcp_config="/tmp/m.json")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], "/tmp/m.json")
        self.assertEqual(cmd[cmd.index("--plugin-dir") + 1],
                         str(self.cfg.home / "plugins" / "pl"))
    def test_command_omits_unset_flags(self):
        bots.update(self.cfg, self.conn, "backend", "admin", effort=None)
        cmd = runner.build_command(self.cfg, self.bot(), "s", "/tmp/i",
                                   task_file="/tmp/t.task.md")
        for flag in ("--effort", "--mcp-config", "--plugin-dir"):
            self.assertNotIn(flag, cmd)

    def test_level_and_effort_override(self):
        b = self.bot()
        self.assertFalse(hasattr(runner, "LEVEL_ORDER"))
        self.assertEqual(runner.settings_for(b, []), ("sonnet", "medium"))
        for level, model in (("housekeeping", "haiku"), ("normal", "sonnet"),
                             ("deep", "sonnet"), ("heavy", "sonnet")):
            m = [{"level": level, "effort": None}]
            self.assertEqual(runner.settings_for(b, m), (model, "medium"), level)
        self.assertEqual(runner.settings_for(
            b, [{"level": "normal", "effort": "high"}])[1], "high")
        # a batch uses the highest level and effort among its messages
        batch = [{"level": "housekeeping", "effort": "low"},
                 {"level": "deep", "effort": "max"},
                 {"level": "normal", "effort": "high"}]
        self.assertEqual(runner.settings_for(b, batch), ("sonnet", "max"))
        # a bot pinned to a full model keeps it at the normal level
        bots.update(self.cfg, self.conn, "backend", "admin",
                    model="claude-sonnet-4-6")
        self.assertEqual(runner.settings_for(
            self.bot(), [{"level": "normal", "effort": None}])[0],
            "claude-sonnet-4-6")


class ModelByTierTests(RunnerBase):
    def test_deep_level_keeps_sonnet_bot_on_sonnet(self):
        b = self.bot()
        self.assertEqual(runner.settings_for(
            b, [{"level": "deep", "effort": "high"}]), ("sonnet", "high"))

    def test_heavy_level_never_picks_opus(self):
        bots.update(self.cfg, self.conn, "backend", "admin", model="sonnet")
        for level in ("normal", "deep", "heavy"):
            model = runner.settings_for(
                self.bot(), [{"level": level, "effort": None}])[0]
            self.assertNotEqual(model, "opus", level)
        # an Opus bot keeps Opus at every level: a level never raises it
        bots.update(self.cfg, self.conn, "backend", "admin", model="opus")
        for level in ("housekeeping", "normal", "deep", "heavy"):
            model = runner.settings_for(
                self.bot(), [{"level": level, "effort": None}])[0]
            self.assertEqual(model, "haiku" if level == "housekeeping"
                             else "opus", level)

    def test_housekeeping_runs_on_haiku(self):
        self.assertEqual(runner.settings_for(
            self.bot(), [{"level": "housekeeping", "effort": None}])[0],
            "haiku")

    def test_housekeeping_leaves_worker_on_haiku(self):
        bots.create(self.cfg, self.conn, "gofer", role="g", soul="s",
                    persona="p", model="haiku")
        gofer = bots.get(self.conn, "gofer")
        self.assertEqual(runner.settings_for(
            gofer, [{"level": "housekeeping", "effort": None}])[0], "haiku")


class PacketTests(RunnerBase):
    def test_packet_uses_pointers(self):
        t = items.new(self.cfg, self.conn, "Build thing", "backend")
        (items.folder(self.cfg, t) / "spec.md").write_text("FILE-CONTENT-XYZ")
        (items.folder(self.cfg, t) / "handoff-backend.md").write_text(
            "HANDOFF-CONTENT-XYZ")
        ms = self.msgs({"subject": "Please build", "body": "see spec",
                        "item_id": t})
        p = runner.task_packet(self.cfg, self.conn, self.bot(), ms)
        self.assertIn("Please build", p)
        self.assertIn("see spec", p)
        self.assertIn(t, p)
        self.assertIn(f"work/{t}/", p)
        self.assertIn(messages.handoff_rel_path(t, "backend"), p)
        self.assertNotIn("FILE-CONTENT-XYZ", p)
        self.assertNotIn("HANDOFF-CONTENT-XYZ", p)

    def test_task_packet_carries_memory_and_colleagues(self):
        bots.update(self.cfg, self.conn, "backend", "admin", role="Backend engineer")
        (self.cfg.bots_dir / "backend" / "MEMORY.md").write_text("MEM-NOTE-XYZ")
        ms = self.msgs({"subject": "Job", "body": "do it"})
        p = runner.task_packet(self.cfg, self.conn, self.bot(), ms)
        self.assertIn("## Memory", p)
        self.assertIn("MEM-NOTE-XYZ", p)
        self.assertIn("## Colleagues", p)
        self.assertIn("- admin: ", p)
        self.assertLess(p.index("## Memory"), p.index("## Message"))
        self.assertLess(p.index("## Colleagues"), p.index("## Message"))

    def test_packet_has_no_history(self):
        ms = self.msgs({"subject": "first job", "body": "FIRST-BODY"})
        runner.run(self.cfg, self.conn, self.bot(), ms, "normal")
        messages.complete(self.conn, [m["id"] for m in ms])
        ms2 = self.msgs({"subject": "second job", "body": "SECOND-BODY"})
        p = runner.task_packet(self.cfg, self.conn, self.bot(), ms2)
        self.assertIn("SECOND-BODY", p)
        for old in ("FIRST-BODY", "first job", "sess-from-claude"):
            self.assertNotIn(old, p)


class McpSecretTests(RunnerBase):
    def define(self, defn):
        (self.cfg.home / "mcp").mkdir(exist_ok=True)
        (self.cfg.home / "mcp" / "notes.json").write_text(json.dumps(defn))
        bots.update(self.cfg, self.conn, "backend", "admin", mcp=["notes"])
        return bots.get(self.conn, "backend")

    def test_secret_reference_is_filled_from_the_store_once_per_name(self):
        from unittest import mock
        b = self.define({"mcpServers": {"notes": {
            "url": "http://x", "env": {"A": "${secret:notes-token}",
                                       "B": "pre-${secret:notes-token}"},
            "headers": {"X": "${OTHER}"}}}})
        calls = []

        def use_env(cfg, conn, actor, env_map, decision=None):
            calls.append((actor, dict(env_map)))
            return {"V": "S3CR3T"}
        with mock.patch("atobots.store.use_env", use_env):
            out = json.loads(runner._mcp_text(self.cfg, b, self.conn))
        srv = out["mcpServers"]["notes"]
        self.assertEqual(srv["env"], {"A": "S3CR3T", "B": "pre-S3CR3T"})
        self.assertEqual(srv["headers"], {"X": "${OTHER}"})  # left as written
        self.assertEqual(calls, [("backend", {"V": "notes-token"})])

    def test_refused_or_unknown_secret_stops_the_run_setup(self):
        from unittest import mock
        b = self.define({"mcpServers": {"n": {"env": {"A": "${secret:nope}"}}}})
        with mock.patch("atobots.store.use_env",
                        side_effect=PermissionError("owner-order-only")):
            with self.assertRaises(PermissionError):
                runner._mcp_text(self.cfg, b, self.conn)
        with self.assertRaises(ValueError):
            runner._mcp_text(self.cfg, b)  # no connection to record the use


class ClassifyTests(unittest.TestCase):
    OK = json.dumps({"is_error": False, "result": "x"})

    def test_ok(self):
        self.assertEqual(runner.classify(0, self.OK), "ok")

    def test_usage_limit(self):
        for txt in ("You've hit your session limit", "You've hit your weekly limit",
                    "You've hit your Opus limit", "You've hit your Sonnet limit",
                    "You've hit your monthly spend limit"):
            out = json.dumps({"is_error": True, "result": txt,
                              "api_error_status": 429})
            self.assertEqual(runner.classify(1, out), "usage_limit", txt)

    def test_usage_text_in_a_successful_run_is_not_a_limit(self):
        out = json.dumps({"is_error": False,
                          "result": "he said: You've hit your session limit"})
        self.assertEqual(runner.classify(0, out), "ok")

    def test_oom(self):
        self.assertEqual(runner.classify(
            1, "Failed with result 'oom-kill'.\nFinished with result: oom-kill"),
            "oom")
        self.assertEqual(runner.classify(137, ""), "oom")

    def test_timeout(self):
        self.assertEqual(runner.classify(
            1, "Finished with result: timeout\nService runtime: 30min"), "timeout")
        self.assertEqual(runner.classify(143, ""), "timeout")

    def test_error(self):
        self.assertEqual(runner.classify(1, "Not logged in · Please run /login"),
                         "error")
        out = json.dumps({"is_error": True, "terminal_reason": "api_error",
                          "api_error_status": 404,
                          "result": "There's an issue with the selected model"})
        self.assertEqual(runner.classify(1, out), "error")
        self.assertEqual(runner.classify(1, "API Error: Repeated 529 Overloaded"),
                         "error")
        self.assertEqual(runner.classify(2, ""), "error")

    def test_bad_output_is_error(self):
        for out in ("", "not json", '{"type": "res', "[1, 2]", "null"):
            self.assertEqual(runner.classify(0, out), "error", out)
        self.assertEqual(runner.classify(0, json.dumps({"is_error": True})),
                         "error")


class RunTests(RunnerBase):
    def run_bot(self, *specs, lane="normal"):
        ms = self.msgs(*(specs or ({},)))
        return runner.run(self.cfg, self.conn, self.bot(), ms, lane)

    def row(self, run_id):
        return self.conn.execute("SELECT * FROM runs WHERE id=?",
                                 (run_id,)).fetchone()

    def test_run_command_has_no_agents_flag(self):
        self.run_bot()
        argv = self.rec()["argv"]
        self.assertNotIn("--agents", argv)

    def test_run_ok_records_row_and_tokens(self):
        r = self.run_bot()
        self.assertEqual((r.reason, r.exit_code), ("ok", 0))
        self.assertEqual(r.session_id, "sess-from-claude")
        self.assertEqual((r.tokens_in, r.tokens_out), (1110, 7))
        row = self.row(r.run_id)
        self.assertEqual((row["bot"], row["lane"], row["reason"], row["exit_code"],
                          row["session_id"]),
                         ("backend", "normal", "ok", 0, "sess-from-claude"))
        self.assertTrue(row["started_at"] and row["ended_at"])
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (1110, 7))
        rec = self.rec()
        self.assertEqual(rec["cb_bot"], "backend")
        self.assertEqual(rec["home"], str(self.cfg.home))
        self.assertEqual(Path(rec["cwd"]).resolve(),
                         (self.cfg.bots_dir / "backend").resolve())
        self.assertIn("I am backend", rec["--append-system-prompt-file"]["text"])

    def test_run_does_not_claim_messages(self):
        ms = self.msgs({})
        runner.run(self.cfg, self.conn, self.bot(), ms, "normal")
        self.assertEqual(len(messages.pending_for(self.conn, "backend")), 1)

    def test_temp_files_are_private_and_removed(self):
        (self.cfg.home / "mcp").mkdir(parents=True)
        defn = {"mcpServers": {"notes": {
            "type": "http", "url": "https://x/mcp",
            "headers": {"Authorization": "Bearer ${NOTES_TOKEN}"}}}}
        (self.cfg.home / "mcp" / "notes.json").write_text(json.dumps(defn))
        bots.update(self.cfg, self.conn, "backend", "admin", mcp=["notes"])
        self.run_bot({"subject": "SECRET-SUBJ", "body": "SECRET-BODY-XYZ"})
        rec = self.rec()
        for flag in ("--mcp-config", "--append-system-prompt-file"):
            self.assertEqual(rec[flag]["mode"], 0o600, flag)
            self.assertFalse(Path(rec[flag]["path"]).exists(), flag)
        self.assertEqual(json.loads(rec["--mcp-config"]["text"]), defn)
        self.assertIn("task_file", rec)
        self.assertEqual(rec["task_file"]["mode"], 0o600)
        self.assertFalse(Path(rec["task_file"]["path"]).exists())
        self.assertIn("SECRET-BODY-XYZ", rec["task_file"]["text"])
        self.assertNotIn("SECRET-BODY-XYZ", rec["-p"])
        self.assertNotIn("SECRET-BODY-XYZ", " ".join(rec["argv"]))

    def test_no_mcp_config_when_bot_has_none(self):
        self.run_bot()
        self.assertNotIn("--mcp-config", self.rec()["argv"])

    def test_temp_files_removed_on_failure(self):
        self.set_mode("crash")
        self.run_bot()
        path = self.rec()["--append-system-prompt-file"]["path"]
        self.assertFalse(Path(path).exists())

    def test_usage_limit(self):
        self.set_mode("usage")
        r = self.run_bot()
        self.assertEqual((r.reason, r.exit_code), ("usage_limit", 1))
        self.assertEqual(self.row(r.run_id)["reason"], "usage_limit")

    def test_garbage_output_is_error_not_crash(self):
        self.set_mode("garbage")
        r = self.run_bot()
        self.assertEqual(r.reason, "error")
        self.assertEqual((r.tokens_in, r.tokens_out), (None, None))
        self.assertTrue(r.session_id)  # the id we passed with --session-id

    def test_crash_is_error(self):
        self.set_mode("crash")
        r = self.run_bot()
        self.assertEqual((r.reason, r.exit_code), ("error", 1))

    def test_missing_plugin_is_error_row(self):
        bots.update(self.cfg, self.conn, "backend", "admin", plugins=["nope"])
        r = self.run_bot()
        self.assertEqual(r.reason, "error")
        self.assertIsNotNone(self.row(r.run_id)["ended_at"])

    def test_batch_level_reaches_command(self):
        self.run_bot({"level": "housekeeping"},
                     {"level": "deep", "effort": "high"})
        argv = self.rec()["argv"]
        # deep no longer picks opus: the bot's own model (sonnet) stands
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_activity_recorded(self):
        self.run_bot()
        kinds = [a["kind"] for a in self.conn.execute("SELECT kind FROM activity")]
        self.assertIn("run.start", kinds)
        self.assertIn("run.end", kinds)

    def test_python_timeout_stops_unit_and_keeps_partial(self):
        import subprocess as sp

        class FakeTimeout(sp.TimeoutExpired):
            def __init__(self):
                super().__init__(cmd=["x"], timeout=1,
                                 output="partial-out", stderr="partial-err")

        stops = []

        def fake_run(*a, **kw):
            raise FakeTimeout()

        with mock.patch("atobots.runner.subprocess.run", side_effect=fake_run), \
                mock.patch("atobots.runner._stop_unit",
                           side_effect=lambda u: stops.append(u)):
            r = self.run_bot()
        self.assertEqual(r.reason, "timeout")
        self.assertEqual(r.exit_code, 143)
        self.assertEqual(stops, [runner.unit_name_for(r.run_id)])
        self.assertEqual(self.row(r.run_id)["reason"], "timeout")


if __name__ == "__main__":
    unittest.main()
