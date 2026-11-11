"""The update job's guard (engine/lib/update_guard.sh): however a job ends,
Admin and the timers come back so the Owner keeps Telegram."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakes  # noqa: E402

GUARD = fakes.ROOT / "engine" / "lib" / "update_guard.sh"
P = "atobots"


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sd = fakes.FakeSystemd(self.tmp)
        self.notes = self.tmp / "notes"
        cb = self.tmp / "cb"
        cb.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.notes}"\n')
        cb.chmod(0o755)
        self.env = self.sd.env(self.tmp / "home")
        self.env["CB_GUARD_CB"] = str(cb)
        for role in ("dispatcher.service", "admin.service", "backup.timer",
                     "doctor.timer"):
            self.sd.mark("enabled", f"{P}-{role}")
            self.sd.mark("active", f"{P}-{role}")

    def run_guard(self, result="success"):
        env = dict(self.env, SERVICE_RESULT=result)
        r = subprocess.run(["bash", str(GUARD), P, "update", "/x/update.log"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def sent(self):
        return self.notes.read_text() if self.notes.exists() else ""

    def test_all_running_does_nothing(self):
        out = self.run_guard()
        self.assertIn("every service is running", out)
        self.assertFalse(any(c.startswith("start") for c in self.sd.calls()))
        self.assertEqual(self.sent(), "")

    def test_killed_job_starts_admin_and_timers_not_the_dispatcher(self):
        for role in ("dispatcher.service", "admin.service", "backup.timer",
                     "doctor.timer"):
            self.sd.mark("active", f"{P}-{role}", on=False)
        self.run_guard("signal")
        self.assertTrue(self.sd.has("active", f"{P}-admin.service"))
        self.assertTrue(self.sd.has("active", f"{P}-backup.timer"))
        self.assertTrue(self.sd.has("active", f"{P}-doctor.timer"))
        self.assertFalse(self.sd.has("active", f"{P}-dispatcher.service"))
        note = self.sent()
        self.assertIn("admin notify", note)
        self.assertIn("(signal)", note)
        self.assertIn(f"Started again: {P}-admin.service", note)
        self.assertIn("dispatcher is still stopped", note)
        self.assertIn("/x/update.log", note)

    def test_disabled_admin_is_left_alone(self):
        self.sd.mark("enabled", f"{P}-admin.service", on=False)
        self.sd.mark("active", f"{P}-admin.service", on=False)
        self.run_guard("timeout")
        self.assertFalse(self.sd.has("active", f"{P}-admin.service"))
        self.assertEqual(self.sent(), "")

    def test_a_start_that_fails_is_reported(self):
        self.sd.mark("active", f"{P}-admin.service", on=False)
        self.sd.mark("fail-start", f"{P}-admin.service")
        self.run_guard("exit-code")
        self.assertIn(f"Could not start: {P}-admin.service", self.sent())

    def test_missing_cb_still_starts_admin(self):
        self.env["CB_GUARD_CB"] = str(self.tmp / "nope")
        self.sd.mark("active", f"{P}-admin.service", on=False)
        out = self.run_guard("signal")
        self.assertTrue(self.sd.has("active", f"{P}-admin.service"))
        self.assertIn("notice was not sent", out)


if __name__ == "__main__":
    unittest.main()
