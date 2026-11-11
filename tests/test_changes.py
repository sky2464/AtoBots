import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from atobots import activity, changes, cli, doctor  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import connect, init_db, utcnow  # noqa: E402


class ChangesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.cfg = Config(self.home)
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)

    def test_normalize_id(self):
        self.assertEqual(changes.normalize_id("C-1"), "C-0001")
        self.assertEqual(changes.normalize_id("C-0001"), "C-0001")
        self.assertEqual(changes.normalize_id("c12"), "C-0012")
        self.assertEqual(changes.normalize_id("42"), "C-0042")
        with self.assertRaises(ValueError):
            changes.normalize_id("foo")

    def test_plan_creates_change_and_records_activity(self):
        cid = changes.plan(self.conn, "Upgrade system package", "1. apt update\n2. apt install pkg",
                           touches=["/etc/package.conf", "pkg"], actor="admin")
        self.assertEqual(cid, "C-0001")
        row = changes.get(self.conn, cid)
        self.assertEqual(row["title"], "Upgrade system package")
        self.assertEqual(row["status"], "planned")
        touches = json.loads(row["touches"])
        self.assertEqual(touches, ["/etc/package.conf", "pkg"])

        acts = activity.since(self.conn, "1970-01-01T00:00:00+00:00")
        self.assertTrue(any(a["kind"] == "change.plan" and a["ref"] == "C-0001" for a in acts))

    def test_plan_requires_touches(self):
        with self.assertRaises(ValueError):
            changes.plan(self.conn, "Title", "Steps", touches=[])

    def test_show_format(self):
        cid = changes.plan(self.conn, "Restart unit", "systemctl restart myunit",
                           touches=["myunit.service"])
        row = changes.get(self.conn, cid)
        text = changes.format_show(row)
        self.assertIn("Change C-0001: Restart unit", text)
        self.assertIn("Status: planned", text)
        self.assertIn("myunit.service", text)
        self.assertIn("systemctl restart myunit", text)

    def test_approve_requires_owner_session(self):
        cid = changes.plan(self.conn, "Restart unit", "steps", touches=["myunit"])
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.home)}):
            # Not an owner session (e.g. backend bot)
            with mock.patch("atobots.activity.caller",
                            return_value=activity.Caller("backend", "sess-1", "normal", False)):
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = cli.main(["change", "approve", cid, "--quote", "go ahead"])
                self.assertEqual(rc, 1)
                self.assertIn("only an Owner session", err.getvalue())

    def test_approve_records_quote_time_session(self):
        cid = changes.plan(self.conn, "Restart unit", "steps", touches=["myunit"])
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.home)}):
            # Admin in chat lane
            with mock.patch("atobots.activity.caller",
                            return_value=activity.Caller("admin", "owner-sess-1", "chat", True)):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = cli.main(["change", "approve", cid, "--quote", "Yes, please restart it"])
                self.assertEqual(rc, 0)
                self.assertIn("Approved C-0001", out.getvalue())

        row = changes.get(self.conn, cid)
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["approved_by_quote"], "Yes, please restart it")
        self.assertEqual(row["session_id"], "owner-sess-1")
        self.assertTrue(row["approved_at"])

    def test_done_refuses_when_verify_services_fails(self):
        cid = changes.plan(self.conn, "Restart unit", "steps", touches=["myunit"])
        changes.approve(self.conn, cid, "yes", "sess", "admin")

        def fail_verify(cfg):
            return False, ["atobots-dispatcher: FAIL"]

        with self.assertRaises(RuntimeError) as cm:
            changes.done(self.cfg, self.conn, cid, verify_fn=fail_verify)
        self.assertIn("services did not verify", str(cm.exception))
        row = changes.get(self.conn, cid)
        self.assertEqual(row["status"], "approved")

    def test_done_succeeds_when_verify_services_passes(self):
        cid = changes.plan(self.conn, "Restart unit", "steps", touches=["myunit"])
        changes.approve(self.conn, cid, "yes", "sess", "admin")

        def ok_verify(cfg):
            return True, ["atobots-dispatcher: PASS"]

        res = changes.done(self.cfg, self.conn, cid, verify_fn=ok_verify)
        self.assertEqual(res, "C-0001")
        row = changes.get(self.conn, cid)
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["completed_at"])

    def test_done_with_failed_flag_closes_as_failed(self):
        cid = changes.plan(self.conn, "Restart unit", "steps", touches=["myunit"])
        changes.approve(self.conn, cid, "yes", "sess", "admin")
        res = changes.done(self.cfg, self.conn, cid, failed_reason="Package install broke dependency")
        self.assertEqual(res, "C-0001")
        row = changes.get(self.conn, cid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["notes"], "Package install broke dependency")
        self.assertTrue(row["completed_at"])

    def test_log_sudo_with_approved_change_no_warning(self):
        cid = changes.plan(self.conn, "Stop service", "steps", touches=["myunit"])
        changes.approve(self.conn, cid, "yes", "sess", "admin")

        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.home)}), \
                redirect_stderr(err):
            rc = cli.main(["log", "sudo", "--change", cid, "systemctl stop myunit"])
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), "")

        acts = activity.since(self.conn, "1970-01-01T00:00:00+00:00")
        unplanned = [a for a in acts if a["kind"] == "change.unplanned"]
        self.assertEqual(len(unplanned), 0)

    def test_log_sudo_without_approved_change_warns_and_records_unplanned(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.home)}), \
                redirect_stderr(err):
            rc = cli.main(["log", "sudo", "rm -rf /tmp/data"])
        self.assertEqual(rc, 0)
        self.assertIn("warning: risky sudo command", err.getvalue())

        acts = activity.since(self.conn, "1970-01-01T00:00:00+00:00")
        unplanned = [a for a in acts if a["kind"] == "change.unplanned"]
        self.assertEqual(len(unplanned), 1)
        self.assertEqual(unplanned[0]["detail"], "rm -rf /tmp/data")

    def test_log_sudo_safe_command_does_not_warn(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"ATOBOTS_HOME": str(self.home)}), \
                redirect_stderr(err):
            rc = cli.main(["log", "sudo", "cat /var/log/syslog"])
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), "")

        acts = activity.since(self.conn, "1970-01-01T00:00:00+00:00")
        unplanned = [a for a in acts if a["kind"] == "change.unplanned"]
        self.assertEqual(len(unplanned), 0)

    def test_doctor_reports_stale_approved_change(self):
        cid = changes.plan(self.conn, "Old migration", "steps", touches=["db"])
        changes.approve(self.conn, cid, "yes", "sess", "admin")

        past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE changes SET approved_at=? WHERE id=?", (past, cid))

        findings = doctor.checks(self.cfg, conn=self.conn)
        change_findings = [f for f in findings if f.key.startswith("change:")]
        self.assertEqual(len(change_findings), 1)
        self.assertIn("more than 24 hours ago", change_findings[0].text)


if __name__ == "__main__":
    unittest.main()
