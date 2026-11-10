"""Tests for voice and audio transcription (Task G1)."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import admin_live, cli, doctor, transcribe  # noqa: E402
from claudebot.config import Config  # noqa: E402


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.cfg = Config(self.home)

    def test_transcribe_without_config_raises_clear_error(self):
        audio_file = self.home / "voice.ogg"
        audio_file.write_text("audio content")
        self.cfg.transcribe_cmd = ""
        with self.assertRaises(RuntimeError) as cm:
            transcribe.transcribe(self.cfg, audio_file)
        self.assertIn("no speech-to-text tool is set up; ask the Owner", str(cm.exception))

    def test_transcribe_cli_without_config_exits_1(self):
        audio_file = self.home / "voice.ogg"
        audio_file.write_text("audio content")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.home)}), \
                redirect_stderr(err):
            rc = cli.main(["transcribe", str(audio_file)])
        self.assertEqual(rc, 1)
        self.assertIn("no speech-to-text tool is set up; ask the Owner", err.getvalue())

    def test_transcribe_runs_command_with_file_as_last_arg(self):
        audio_file = self.home / "voice.ogg"
        audio_file.write_text("audio content")
        self.cfg.transcribe_cmd = "whisper --model base"

        recorded_cmd = []

        def fake_run(cmd, capture_output=True, text=True, timeout=None, check=True):
            recorded_cmd.extend(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="Transcribed text\n", stderr="")

        def fake_which(cmd):
            if cmd == "whisper":
                return "/usr/bin/whisper"
            return None

        with mock.patch("shutil.which", side_effect=fake_which):
            result = transcribe.transcribe(self.cfg, audio_file, runner=fake_run)

        self.assertEqual(result, "Transcribed text")
        self.assertEqual(recorded_cmd[-1], str(audio_file))
        self.assertEqual(recorded_cmd[:2], ["whisper", "--model"])

    def test_transcribe_enforces_timeout_and_memory_wrap(self):
        audio_file = self.home / "voice.ogg"
        audio_file.write_text("audio content")
        self.cfg.transcribe_cmd = "/bin/tool"

        calls = []

        def fake_run(cmd, capture_output=True, text=True, timeout=None, check=True):
            calls.append({"cmd": cmd, "timeout": timeout})
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

        def fake_which(cmd):
            if cmd in ("/bin/tool", "systemd-run"):
                return f"/bin/{cmd}"
            return None

        with mock.patch("shutil.which", side_effect=fake_which), \
                mock.patch("claudebot.chat.Path.is_dir", return_value=True), \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}):
            result = transcribe.transcribe(self.cfg, audio_file, runner=fake_run)

        self.assertEqual(result, "done")
        self.assertEqual(calls[0]["timeout"], 300)
        self.assertIn(f"MemoryMax={self.cfg.run_memory_max}", calls[0]["cmd"])

    def test_transcribe_timeout_expired_raises_runtime_error(self):
        audio_file = self.home / "voice.ogg"
        audio_file.write_text("audio content")
        self.cfg.transcribe_cmd = "whisper"

        def fake_run(cmd, capture_output=True, text=True, timeout=None, check=True):
            raise subprocess.TimeoutExpired(cmd, timeout)

        with mock.patch("shutil.which", return_value="/usr/bin/whisper"), \
                self.assertRaises(RuntimeError) as cm:
            transcribe.transcribe(self.cfg, audio_file, runner=fake_run)

        self.assertIn("timed out after 300s", str(cm.exception))

    def test_transcribe_cli_prints_text(self):
        audio_file = self.home / "voice.ogg"
        audio_file.write_text("audio content")
        (self.home / "config.json").write_text('{"transcribe_cmd": "whisper"}')

        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDEBOT_HOME": str(self.home)}), \
                mock.patch("shutil.which", return_value="/usr/bin/whisper"), \
                mock.patch("subprocess.run") as mock_run, \
                redirect_stdout(out):
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="voice to text", stderr="")
            rc = cli.main(["transcribe", str(audio_file)])

        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "voice to text")

    def test_doctor_reports_missing_transcribe_cmd_executable(self):
        self.cfg.transcribe_cmd = "missing_stt_tool --arg"
        with mock.patch("shutil.which", return_value=None):
            found = doctor.checks(self.cfg, track=False)
        keys = [f.key for f in found]
        self.assertIn("transcribe-cmd", keys)
        finding = [f for f in found if f.key == "transcribe-cmd"][0]
        self.assertIn("missing_stt_tool", finding.text)
        self.assertIn("PATH", finding.text)

    def test_doctor_clean_when_transcribe_cmd_executable_exists(self):
        self.cfg.transcribe_cmd = "existing_stt_tool --arg"
        with mock.patch("shutil.which", return_value="/usr/bin/existing_stt_tool"):
            found = doctor.checks(self.cfg, track=False)
        keys = [f.key for f in found]
        self.assertNotIn("transcribe-cmd", keys)

    def test_live_prompt_mentions_transcribe_and_allowlist(self):
        self.assertIn("cb transcribe", admin_live.LIVE_PROMPT)
        self.assertIn("quote the text back", admin_live.LIVE_PROMPT)
        self.assertIn("allow list", admin_live.LIVE_PROMPT)


if __name__ == "__main__":
    unittest.main()
