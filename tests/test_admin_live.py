"""Always-on Admin: token refuse/scrub, surface, single-flight helpers."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
import sys
sys.path.insert(0, str(ENGINE))

from claudebot import admin_live, bots, chat  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.cfg = Config(root / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="s",
                    persona="p")
        self.tg = root / "telegram"
        self.tg.mkdir()
        self.env_path = self.tg / ".env"
        p = mock.patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(self.tg)})
        p.start()
        self.addCleanup(p.stop)


class TokenFileTests(Base):
    def test_scrub_removes_env_even_without_store_item(self):
        self.env_path.write_text("TELEGRAM_BOT_TOKEN=x\n")
        self.assertTrue(admin_live.scrub_token(self.cfg))
        self.assertFalse(self.env_path.exists())
        self.assertFalse(admin_live.scrub_token(self.cfg))

    def test_session_env_refuses_unexpected_existing_file(self):
        self.env_path.write_text("TELEGRAM_BOT_TOKEN=foreign\n")
        live = admin_live.Live()
        with self.assertRaises(admin_live.TokenError) as cm:
            live.session_env(self.cfg, self.conn)
        self.assertIn("already exists", str(cm.exception))
        self.assertEqual(self.env_path.read_text(), "TELEGRAM_BOT_TOKEN=foreign\n")

    def test_session_env_raises_when_secret_missing(self):
        live = admin_live.Live()
        with self.assertRaises(admin_live.TokenError):
            live.session_env(self.cfg, self.conn)

    def test_session_env_writes_and_cleanup_scrubs(self):
        live = admin_live.Live()
        with mock.patch("claudebot.admin_live.store.use_env",
                        return_value={admin_live.TOKEN_VAR: "tok-1"}):
            env = live.session_env(self.cfg, self.conn)
        self.assertIsInstance(env, dict)
        self.assertTrue(self.env_path.is_file())
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)
        self.assertIn("tok-1", self.env_path.read_text())
        live.cleanup()
        self.assertFalse(self.env_path.exists())

    def test_session_env_refuses_empty_decrypted_token(self):
        live = admin_live.Live()
        with mock.patch("claudebot.admin_live.store.use_env",
                        return_value={admin_live.TOKEN_VAR: "  "}):
            with self.assertRaises(admin_live.TokenError) as cm:
                live.session_env(self.cfg, self.conn)
        self.assertIn("empty", str(cm.exception).lower())
        self.assertFalse(self.env_path.exists())


class ServeLoopTokenTests(Base):
    def test_serve_loop_does_not_start_without_token(self):
        live = admin_live.Live()
        live.stop = threading.Event()
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            live.stop.set()

        started = []

        def boom(*a, **k):
            started.append(1)
            raise AssertionError("must not start a session")

        with mock.patch("claudebot.admin_live.has_token", return_value=False), \
             mock.patch("claudebot.chat._one_session", side_effect=boom), \
             mock.patch("claudebot.admin_live.activity.record"):
            admin_live.serve_loop(self.cfg, live, self.conn, sleep=sleep)
        self.assertEqual(started, [])
        self.assertEqual(len(sleeps), 1)

    def test_serve_loop_backs_off_on_token_error(self):
        live = admin_live.Live()
        live.stop = threading.Event()
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            live.stop.set()

        def fail(*a, **k):
            raise admin_live.TokenError("decrypt failed")

        with mock.patch("claudebot.admin_live.has_token", return_value=True), \
             mock.patch("claudebot.chat._one_session", side_effect=fail), \
             mock.patch("claudebot.admin_live.activity.record"):
            admin_live.serve_loop(self.cfg, live, self.conn, sleep=sleep)
        self.assertEqual(len(sleeps), 1)
        self.assertFalse(self.env_path.exists())


class SurfaceTests(Base):
    def test_surface_rejects_text_that_is_not_a_plain_notice(self):
        with self.assertRaises(ValueError):
            admin_live.surface(self.cfg, "hello\nOwner")
        with self.assertRaises(ValueError):
            admin_live.surface(self.cfg, "wait: see `cb inbox`")

    def test_surface_types_into_tmux_or_falls_back(self):
        posted = []
        tmux_cmds = []

        def post(token, chat_id, text):
            posted.append((token, chat_id, text))

        def fake_run(cmd, **kwargs):
            tmux_cmds.append(cmd)
            if cmd[0] == "tmux":
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0)

        (self.tg / "access.json").write_text(json.dumps(
            {"allowFrom": [42, "99"]}))
        with mock.patch("claudebot.admin_live.subprocess.run", side_effect=fake_run):
            admin_live.surface(
                self.cfg, "two replies waiting", key="admin-queue",
                conn=self.conn, now="2026-11-09T12:00:00+00:00",
                post=post)
        self.assertTrue(any(c[:3] == ["tmux", "send-keys", "-t"] for c in tmux_cmds))
        self.assertEqual(posted, [])

        with mock.patch("claudebot.admin_live.has_token", return_value=True), \
             mock.patch("claudebot.admin_live.store.use_env",
                        return_value={admin_live.TOKEN_VAR: "T"}):
            admin_live.notify_telegram(
                self.cfg, "hello", conn=self.conn, post=post)
        self.assertEqual({c for _, c, _ in posted}, {42, "99"})
        self.assertTrue(all(t == "T" for t, _, _ in posted))

    def test_is_running_tracks_serve_lock(self):
        self.assertFalse(admin_live.is_running(self.cfg))
        sd = admin_live.state_dir(self.cfg)
        sd.mkdir(parents=True)
        fd = os.open(sd / "serve.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: os.close(fd) if fd is not None else None)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(admin_live.is_running(self.cfg))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertFalse(admin_live.is_running(self.cfg))


class MemoryWrapTests(unittest.TestCase):
    def test_memory_wrap_passthrough_without_systemd_run(self):
        cfg = Config(Path("/tmp/unused-home"))
        cmd = ["/bin/claude", "--model", "sonnet"]
        with mock.patch("claudebot.chat.shutil.which", return_value=None):
            self.assertEqual(chat.memory_wrap(cfg, cmd), cmd)

    def test_memory_wrap_adds_memorymax_with_systemd_run(self):
        cfg = Config(Path("/tmp/unused-home"), run_memory_max="900M")
        cmd = ["/bin/claude", "--model", "sonnet"]
        with mock.patch("claudebot.chat.shutil.which",
                        return_value="/bin/systemd-run"), \
             mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}, clear=False):
            wrapped = chat.memory_wrap(cfg, cmd)
        self.assertEqual(wrapped, cmd)

        with mock.patch("claudebot.chat.shutil.which",
                        return_value="/bin/systemd-run"), \
             mock.patch("claudebot.chat.Path") as path_cls, \
             mock.patch.dict(os.environ,
                             {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=False):
            path_cls.return_value.__truediv__.return_value.is_dir.return_value = True
            wrapped = chat.memory_wrap(cfg, cmd)
        self.assertEqual(wrapped[0], "/bin/systemd-run")
        self.assertIn("--scope", wrapped)
        self.assertIn("MemoryMax=900M", wrapped)
        self.assertIn("MemorySwapMax=0", wrapped)
        self.assertEqual(wrapped[wrapped.index("--") + 1:], cmd)


class UpdatePendingTests(Base):
    def test_check_pending_update_no_marker(self):
        called = []
        self.assertFalse(admin_live.check_pending_update(
            self.cfg, notify_fn=lambda *a, **k: called.append(a)))
        self.assertEqual(called, [])

    def test_check_pending_update_sends_notification_and_deletes_marker(self):
        sd = admin_live.state_dir(self.cfg)
        sd.mkdir(parents=True, exist_ok=True)
        marker = sd / admin_live.UPDATE_PENDING_FILE
        marker.write_text(json.dumps({
            "old_version": "v0.0.1",
            "new_version": "v0.0.2",
            "time": "2026-11-10T12:00:00Z",
        }))
        messages = []

        def fake_notify(cfg, text, conn=None):
            messages.append(text)
            return True

        self.assertTrue(admin_live.check_pending_update(self.cfg, notify_fn=fake_notify))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0], "ClaudeBot updated v0.0.1 to v0.0.2. Admin is back.")
        self.assertFalse(marker.exists())

    def test_supervise_triggers_check_pending_update(self):
        sd = admin_live.state_dir(self.cfg)
        sd.mkdir(parents=True, exist_ok=True)
        marker = sd / admin_live.UPDATE_PENDING_FILE
        marker.write_text(json.dumps({
            "old_version": "v0.0.1",
            "new_version": "v0.0.2",
        }))
        stop = threading.Event()
        checked = []

        def fake_check(cfg):
            checked.append(1)
            stop.set()

        def has_sess():
            return not stop.is_set()

        with mock.patch("claudebot.admin_live.has_session", side_effect=has_sess), \
             mock.patch("claudebot.admin_live.kill_session"):
            admin_live.supervise(self.cfg, stop, check_update_fn=fake_check)
        self.assertTrue(len(checked) >= 1)


class VerifyServicesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")

    def test_verify_services_all_pass(self):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0)

        ok, lines = admin_live.verify_services(self.cfg, runner=fake_run)
        self.assertTrue(ok)
        self.assertEqual(lines, ["claudebot-dispatcher: PASS", "claudebot-admin: PASS"])

    def test_verify_services_dispatcher_down(self):
        def fake_run(cmd, **kw):
            if "claudebot-dispatcher.service" in cmd:
                return subprocess.CompletedProcess(cmd, 3)
            return subprocess.CompletedProcess(cmd, 0)

        ok, lines = admin_live.verify_services(self.cfg, runner=fake_run)
        self.assertFalse(ok)
        self.assertEqual(lines, ["claudebot-dispatcher: FAIL", "claudebot-admin: PASS"])

    def test_verify_services_admin_not_enabled(self):
        def fake_run(cmd, **kw):
            if "is-enabled" in cmd:
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        ok, lines = admin_live.verify_services(self.cfg, runner=fake_run)
        self.assertTrue(ok)
        self.assertEqual(lines, ["claudebot-dispatcher: PASS", "claudebot-admin: PASS (not enabled)"])

    def test_cli_verify_services(self):
        from claudebot import cli
        import io
        from contextlib import redirect_stdout
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.cfg.home)}), \
             mock.patch("claudebot.admin_live.verify_services",
                        return_value=(True, ["claudebot-dispatcher: PASS", "claudebot-admin: PASS"])):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(["verify", "services"])
            self.assertEqual(rc, 0)
            self.assertIn("claudebot-dispatcher: PASS", out.getvalue())


if __name__ == "__main__":
    unittest.main()
