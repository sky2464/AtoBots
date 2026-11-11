import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from atobots import bots, cli, dispatcher, items, messages, requests, tasks  # noqa: E402
from atobots.config import Config  # noqa: E402
from atobots.db import connect, init_db  # noqa: E402
from atobots.runner import RunResult  # noqa: E402

T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t.isoformat(timespec="seconds")

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeRun:
    """Stands in for runner.run: records the run like the real one does."""

    def __init__(self, clock, reasons=None, gate=None):
        self.clock = clock
        self.reasons = list(reasons or [])  # consumed one per call; default ok
        self.gate = gate
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, cfg, conn, bot, msgs, lane, on_start=None):
        name = bot if isinstance(bot, str) else bot.name
        conn.execute("INSERT INTO runs(bot, session_id, lane, started_at)"
                     " VALUES (?,?,?,?)", (name, "s", lane, self.clock()))
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        with self.lock:
            self.calls.append((name, [m["id"] for m in msgs], lane, run_id))
            reason = self.reasons.pop(0) if self.reasons else "ok"
        if on_start:
            on_start(run_id)
        if self.gate is not None:
            self.gate.wait(10)
        conn.execute("UPDATE runs SET ended_at=?, reason=? WHERE id=?",
                     (self.clock(), reason, run_id))
        return RunResult(run_id, "s", 0 if reason == "ok" else 1, reason, 1, 1)

    @property
    def bots_run(self):
        return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    names = ("admin", "backend", "design")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = Clock()
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        for n in self.names:
            bots.create(self.cfg, self.conn, n, role=n, soul="s", persona="p")
        self.run_fn = FakeRun(self.clock)
        self.commits = []
        self.owner = False
        self.mem = 4000
        self.reindexed = 0

    def make(self, cfg=None, run_fn=None, **kw):
        def commit(c, bot, item_ids, run_id):
            self.commits.append((bot, list(item_ids), run_id))
            return "sha"

        def reindex(c, conn):
            self.reindexed += 1
            return 0

        d = dispatcher.Dispatcher(
            cfg or self.cfg, run_fn=run_fn or self.run_fn,
            mem_fn=lambda: self.mem, clock=self.clock,
            owner_fn=lambda: self.owner,
            commit_fn=kw.pop("commit_fn", commit),
            reindex_fn=kw.pop("reindex_fn", reindex),
            archive_fn=kw.pop("archive_fn", lambda *a: None),
            daily_fn=kw.pop("daily_fn", lambda *a: None), **kw)
        self.addCleanup(d.close)
        return d

    def send(self, to, subject="s", frm="admin", **kw):
        kw.setdefault("now", self.clock())
        return messages.send(self.conn, frm, to, subject, "body", **kw)

    def msg(self, id):
        return self.conn.execute("SELECT * FROM messages WHERE id=?",
                                 (id,)).fetchone()

    def ready(self):
        self.clock.advance(self.cfg.batch_delay_seconds)

    def tick(self, d):
        ids = d.tick()
        d.wait()
        return ids

    def kinds(self, kind):
        return self.conn.execute("SELECT * FROM activity WHERE kind=?",
                                 (kind,)).fetchall()

    def system_notes(self, to="admin"):
        return self.conn.execute(
            "SELECT * FROM messages WHERE from_bot='system' AND to_bot=?",
            (to,)).fetchall()


class StartTests(Base):
    def test_idle_starts_nothing(self):
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.run_fn.calls, [])

    def test_batches_one_session_per_bot(self):
        ids = [self.send("backend", f"m{i}") for i in range(3)]
        d = self.make()
        self.assertEqual(d.tick(), [])  # still inside the batch delay
        self.clock.advance(4)
        self.assertEqual(d.tick(), [])
        self.clock.advance(1)
        started = self.tick(d)
        self.assertEqual(len(started), 1)
        self.assertEqual(len(self.run_fn.calls), 1)
        self.assertEqual(sorted(self.run_fn.calls[0][1]), ids)

    def test_urgent_skips_batch_delay(self):
        self.send("backend", priority="urgent")
        d = self.make()
        self.assertEqual(len(self.tick(d)), 1)

    def test_max_concurrent(self):
        gate = threading.Event()
        self.run_fn.gate = gate
        for n in ("admin", "backend", "design"):
            self.send(n, frm="system")
        self.ready()
        d = self.make()
        started = d.tick()
        self.assertEqual(len(started), 2)
        self.assertEqual(d.tick(), [])  # both slots busy
        gate.set()
        d.wait()
        self.assertEqual(len(d.tick()), 1)  # the third now goes
        d.wait()
        self.assertEqual(sorted(self.run_fn.bots_run),
                         ["admin", "backend", "design"])

    def test_memory_floor_blocks_start(self):
        self.send("backend")
        self.ready()
        self.mem = 499
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.msg(1)["status"], "queued")
        self.mem = 500
        self.assertEqual(len(self.tick(d)), 1)

    def test_heavy_lane_runs_one_at_a_time(self):
        cfg = Config(self.cfg.home, max_concurrent_runs=3)
        gate = threading.Event()
        self.run_fn.gate = gate
        self.send("admin", level="heavy", frm="system")
        self.send("backend", level="heavy", frm="system")
        self.send("design", frm="system")
        self.ready()
        d = self.make(cfg=cfg)
        started = d.tick()
        lanes = sorted(c[2] for c in self.run_fn.calls)
        self.assertEqual(len(started), 2)
        self.assertEqual(lanes, ["heavy", "normal"])  # 2nd heavy waits
        gate.set()
        d.wait()
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(sorted(c[2] for c in self.run_fn.calls),
                         ["heavy", "heavy", "normal"])

    def test_wake_cap(self):
        # Default backend is sonnet → soft cap wake_cap_sonnet (6).
        for k in range(self.cfg.wake_cap_sonnet):
            self.conn.execute(
                "INSERT INTO runs(bot, started_at, ended_at, reason)"
                " VALUES ('backend',?,?,'ok')",
                (self.clock(), self.clock()))
        self.send("backend")
        self.ready()
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.tick(d)
        self.assertEqual(self.run_fn.bots_run, [])
        self.assertEqual(self.system_notes(), [])
        capped = self.kinds("wake.capped")
        self.assertEqual(len(capped), 1)
        self.assertEqual(capped[0]["ref"], "backend")
        # an hour later the old wakes have aged out; no Admin message was sent
        self.clock.advance(3601)
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(self.run_fn.bots_run, ["backend"])

    def test_wake_cap_ignores_chat_and_live_lanes(self):
        for lane in ("chat", "live"):
            for _ in range(12):
                self.conn.execute(
                    "INSERT INTO runs(bot, lane, started_at, ended_at, reason)"
                    " VALUES ('admin',?,?,?,'ok')",
                    (lane, self.clock(), self.clock()))
        self.send("admin", frm="backend")
        self.ready()
        d = self.make()
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(self.run_fn.bots_run, ["admin"])

    def test_opus_bot_capped_at_three_runs_per_hour(self):
        bots.update(self.cfg, self.conn, "backend", "admin", model="opus")
        for _ in range(3):
            self.conn.execute(
                "INSERT INTO runs(bot, started_at, ended_at, reason)"
                " VALUES ('backend',?,?,'ok')",
                (self.clock(), self.clock()))
        self.send("backend")
        self.ready()
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.run_fn.bots_run, [])
        self.assertEqual(self.system_notes(), [])
        self.assertEqual(len(self.kinds("wake.capped")), 1)

    def test_owner_named_message_does_not_bypass_cap(self):
        bots.update(self.cfg, self.conn, "backend", "admin", model="opus")
        for _ in range(3):
            self.conn.execute(
                "INSERT INTO runs(bot, started_at, ended_at, reason)"
                " VALUES ('backend',?,?,'ok')",
                (self.clock(), self.clock()))
        # Owner is a reserved sender (not a bot row); insert the queue row
        # the way an Owner-channel path would land work.
        self.conn.execute(
            "INSERT INTO messages(from_bot, to_bot, kind, subject, body, "
            "priority, level, status, created_at) VALUES "
            "('owner','backend','wake','s','body','normal','normal',"
            "'queued',?)", (self.clock(),))
        self.ready()
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.run_fn.bots_run, [])
        self.assertEqual(len(self.kinds("wake.capped")), 1)

    def test_wake_cap_is_once_per_hour_and_doctor_flags_the_third(self):
        from atobots import doctor
        bots.update(self.cfg, self.conn, "backend", "admin", model="opus")
        d = self.make()
        for hour in range(3):
            if hour:
                self.clock.advance(3600)
            for _ in range(3):
                self.conn.execute(
                    "INSERT INTO runs(bot, started_at, ended_at, reason)"
                    " VALUES ('backend',?,?,'ok')",
                    (self.clock(), self.clock()))
            self.send("backend", subject=f"h{hour}")
            self.ready()
            self.assertEqual(self.tick(d), [])
            if hour == 0:
                self.assertEqual(self.tick(d), [])
                self.assertEqual(len(self.kinds("wake.capped")), 1)
        self.assertEqual(self.system_notes(), [])
        self.assertEqual(len(self.kinds("wake.capped")), 3)
        found = doctor.checks(
            self.cfg, self.conn, active_fn=lambda: True,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: False,
            live_session_fn=lambda: False, token_fn=lambda c: True,
            track=False, now=self.clock())
        self.assertIn("wakecap:backend", [f.key for f in found])
        self.assertTrue(doctor.valid_finding_key("wakecap:backend"))

    def test_admin_urgent_bypasses_cap(self):
        bots.update(self.cfg, self.conn, "backend", "admin", model="opus")
        for _ in range(3):
            self.conn.execute(
                "INSERT INTO runs(bot, started_at, ended_at, reason)"
                " VALUES ('backend',?,?,'ok')",
                (self.clock(), self.clock()))
        self.send("backend", priority="urgent")
        d = self.make()
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(self.run_fn.bots_run, ["backend"])

    def test_worker_waits_sixty_seconds_to_batch(self):
        bots.create(self.cfg, self.conn, "worker1", role="worker", soul="s",
                    persona="p", model="haiku", tier="worker")
        self.send("worker1", "first")
        d = self.make()
        # 10 s is enough for a thinker (batch_delay=5) but not a worker (60).
        self.clock.advance(10)
        self.assertEqual(self.tick(d), [])
        self.send("worker1", "second")
        self.clock.advance(55)  # first message now past 60 s
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(self.run_fn.bots_run, ["worker1"])
        self.assertEqual(len(self.run_fn.calls[0][1]), 2)

    def test_owner_connected_defers_new_runs(self):
        self.send("backend")
        self.ready()
        self.owner = True
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.owner = False
        self.assertEqual(len(self.tick(d)), 1)

    def test_spoofed_admin_urgent_blocked_under_owner_hold(self):
        """A bot bound to CB_BOT cannot mint from=admin+urgent for hold bypass."""
        self.owner = True
        d = self.make()
        with mock.patch.dict(os.environ, {"CB_BOT": "backend"}):
            with self.assertRaises(PermissionError):
                messages.send(self.conn, "admin", "backend", "wake", "now",
                              priority="urgent")
        self.assertEqual(self.tick(d), [])
        # Real Admin (CB_BOT=admin) urgent still goes through hold.
        with mock.patch.dict(os.environ, {"CB_BOT": "admin"}):
            messages.send(self.conn, "admin", "backend", "wake", "now",
                          priority="urgent")
        self.ready()
        self.assertEqual(len(self.tick(d)), 1)

    def test_paused_and_retired_bots_skip(self):
        self.send("backend")
        self.send("design")
        self.ready()
        bots.set_status(self.conn, "backend", "paused", "admin")
        bots.set_status(self.conn, "design", "retired", "admin")
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.msg(1)["status"], "queued")

    def test_same_bot_never_runs_twice_at_once(self):
        gate = threading.Event()
        self.run_fn.gate = gate
        self.send("backend")
        self.ready()
        d = self.make()
        self.assertEqual(len(d.tick()), 1)
        self.send("backend", "later", priority="urgent")
        self.assertEqual(d.tick(), [])
        gate.set()
        d.wait()


class ClaimTests(Base):
    def test_atomic_claim(self):
        ids = [self.send("backend", f"m{i}") for i in range(40)]
        d1, d2 = self.make(), self.make()
        got = {}
        barrier = threading.Barrier(2)

        def go(name, d):
            barrier.wait()
            try:
                got[name] = [r["id"] for r in d.claim("backend")]
            finally:
                d._drop_conn()

        ts = [threading.Thread(target=go, args=(n, d))
              for n, d in (("a", d1), ("b", d2))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(got["a"] + got["b"]), ids)
        self.assertEqual(set(got["a"]) & set(got["b"]), set())
        m = self.msg(ids[0])
        self.assertEqual((m["status"], m["attempts"]), ("claimed", 1))
        self.assertEqual(m["lease_until"],
                         (T0 + timedelta(seconds=2100)).isoformat(
                             timespec="seconds"))

    def test_message_done_only_after_success(self):
        a = self.send("backend")
        self.ready()
        gate = threading.Event()
        self.run_fn.gate = gate
        d = self.make()
        d.tick()
        self.assertEqual(self.msg(a)["status"], "claimed")  # running, not done
        gate.set()
        d.wait()
        self.assertEqual(self.msg(a)["status"], "done")
        b = self.send("backend")
        self.run_fn.reasons = ["error"]
        self.ready()
        self.tick(d)
        self.assertEqual(self.msg(b)["status"], "queued")

    def test_bare_ok_without_progress_does_not_complete_question(self):
        t = items.new(self.cfg, self.conn, "q", "backend")
        q = messages.ask(self.cfg, self.conn, "admin", "backend", t,
                         "which?", "handoff", now=self.clock())
        self.ready()
        d = self.make()
        self.tick(d)
        m = self.msg(q)
        self.assertEqual((m["status"], m["attempts"]), ("queued", 1))
        self.assertTrue(self.kinds("message.no_progress"))

    def test_ok_without_progress_fails_after_retry_attempts(self):
        t = items.new(self.cfg, self.conn, "q", "backend")
        q = messages.ask(self.cfg, self.conn, "admin", "backend", t,
                         "which?", "handoff", now=self.clock())
        d = self.make()
        for _ in range(self.cfg.retry_attempts):
            self.ready()
            self.tick(d)
        m = self.msg(q)
        self.assertEqual((m["status"], m["attempts"]),
                         ("failed", self.cfg.retry_attempts))
        stuck = [n for n in self.system_notes() if n["kind"] == "stuck"]
        self.assertEqual(len(stuck), 1)
        self.assertIn(
            "The bot's runs ended normally but it never replied, "
            "finished the task or updated the item.", stuck[0]["body"])

    def test_crash_requeues(self):
        a = self.send("backend")
        d = self.make()
        d.claim("backend")
        self.assertEqual(self.msg(a)["status"], "claimed")
        self.clock.advance(2099)
        self.assertEqual(d.recover_expired(), 0)
        self.clock.advance(2)
        self.assertEqual(d.recover_expired(), 1)
        m = self.msg(a)
        self.assertEqual((m["status"], m["attempts"], m["lease_until"]),
                         ("queued", 1, None))
        self.assertEqual(len(self.tick(d)), 1)  # and it runs again

    def test_expired_lease_is_claimable_again(self):
        a = self.send("backend")
        d = self.make()
        d.claim("backend")
        self.assertEqual(d.claim("backend"), [])  # lease still holds
        self.clock.advance(2200)
        got = d.claim("backend")
        self.assertEqual([r["id"] for r in got], [a])
        self.assertEqual(self.msg(a)["attempts"], 2)

    def test_three_failures_mark_stuck_and_tell_admin(self):
        a = self.send("backend", "do it")
        self.run_fn.reasons = ["error", "timeout", "oom"]
        d = self.make()
        for k in range(3):
            self.ready()
            self.assertEqual(len(self.tick(d)), 1)
        m = self.msg(a)
        self.assertEqual((m["status"], m["attempts"]), ("failed", 3))
        notes = self.system_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn(str(a), notes[0]["subject"] + notes[0]["body"])
        self.ready()
        self.assertEqual(self.tick(d).__len__(), 1)  # only the admin note runs
        self.assertEqual(self.run_fn.bots_run.count("backend"), 3)  # no retry

    def test_stuck_note_that_fails_does_not_breed_notes(self):
        a = self.send("admin", "x", frm="system", kind="stuck")
        self.run_fn.reasons = ["error"] * 3
        d = self.make()
        for k in range(3):
            self.ready()
            self.tick(d)
        self.assertEqual(self.msg(a)["status"], "failed")
        self.assertEqual(len(self.system_notes()), 1)  # just the original

    def test_hold_stops_new_runs_until_release(self):
        self.send("backend")
        self.ready()
        until = dispatcher.hold_queue(self.conn, 45, "update", now=self.clock())
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.assertEqual(self.msg(1)["status"], "queued")
        text = dispatcher.status_text(self.conn, self.clock())
        self.assertIn("Held until:", text)
        self.assertIn("(update)", text)
        self.assertTrue(until)
        dispatcher.release_queue(self.conn)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM meta WHERE key=?",
            (dispatcher.HOLD_UNTIL_KEY,)).fetchone())
        self.assertEqual(len(self.tick(d)), 1)

    def test_usage_limit_pauses_queue_and_resumes(self):
        a = self.send("backend")
        b = self.send("design")
        self.ready()
        self.run_fn.reasons = ["usage_limit"]
        d = self.make(cfg=Config(self.cfg.home, max_concurrent_runs=1))
        self.assertEqual(len(self.tick(d)), 1)
        first = self.run_fn.bots_run[0]
        self.assertEqual(self.msg(a if first == "backend" else b)["attempts"], 0)
        self.assertEqual(self.msg(a if first == "backend" else b)["status"],
                         "queued")
        self.clock.advance(899)
        self.assertEqual(self.tick(d), [])
        self.assertEqual(len(self.run_fn.calls), 1)
        self.clock.advance(1)
        self.assertEqual(len(self.tick(d)), 1)  # one attempt after 900 s
        self.assertEqual(len(self.run_fn.calls), 2)

    def test_pause_survives_restart(self):
        self.send("backend")
        self.ready()
        self.run_fn.reasons = ["usage_limit"]
        d = self.make()
        self.tick(d)
        d2 = self.make()
        self.clock.advance(10)
        self.assertEqual(self.tick(d2), [])
        self.assertEqual(len(self.run_fn.calls), 1)

    def test_run_fn_raising_requeues_and_closes_run(self):
        a = self.send("backend")
        self.ready()

        def boom(cfg, conn, bot, msgs, lane, on_start=None):
            conn.execute("INSERT INTO runs(bot, started_at) VALUES ('backend',?)",
                         (self.clock(),))
            on_start(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            raise RuntimeError("kaboom")

        d = self.make(run_fn=boom)
        self.tick(d)
        self.assertEqual(self.msg(a)["status"], "queued")
        r = self.conn.execute("SELECT * FROM runs").fetchone()
        self.assertIsNotNone(r["ended_at"])
        self.assertEqual(r["reason"], "error")


class CommitAndRecoveryTests(Base):
    def test_commit_called_after_each_run(self):
        self.send("backend", item_id=items.new(self.cfg, self.conn, "t",
                                               "backend", status="doing"))
        self.send("design")
        self.ready()
        d = self.make()
        self.tick(d)
        self.assertEqual(sorted(c[0] for c in self.commits),
                         ["backend", "design"])
        by_bot = {c[0]: c for c in self.commits}
        self.assertEqual(by_bot["backend"][1], ["T-0001"])
        run_ids = {c[3]: c[0] for c in self.run_fn.calls}
        self.assertEqual({c[2] for c in self.commits}, set(run_ids))
        sha = self.conn.execute("SELECT commit_sha FROM runs WHERE bot='design'"
                                ).fetchone()[0]
        self.assertEqual(sha, "sha")

    def test_failed_commit_is_logged_and_result_kept(self):
        a = self.send("backend")
        self.ready()

        def bad(*a):
            raise RuntimeError("git exploded")

        d = self.make(commit_fn=bad)
        self.tick(d)
        self.assertEqual(self.msg(a)["status"], "done")
        self.assertEqual(len(self.kinds("commit.failed")), 1)
        self.send("backend")
        self.ready()
        self.assertEqual(len(self.tick(d)), 1)  # service carries on

    def test_reindex_only_when_data_changed_and_never_breaks_tick(self):
        d = self.make()
        d.tick()
        d.tick()
        self.assertEqual(self.reindexed, 1)
        self.send("backend")  # another connection commits
        d.tick()
        self.assertEqual(self.reindexed, 2)

        def bad(c, conn):
            raise RuntimeError("fts broke")

        d2 = self.make(reindex_fn=bad)
        self.send("backend")
        self.ready()
        self.assertEqual(len(self.tick(d2)), 1)
        self.assertEqual(len(self.kinds("search.reindex_failed")), 1)

    def test_startup_marks_open_runs_crashed_and_keeps_messages_claimed(self):
        a = self.send("backend")
        self.conn.execute("INSERT INTO runs(bot, started_at) VALUES"
                          " ('backend', ?)", (self.clock(),))
        self.conn.execute("UPDATE messages SET status='claimed', attempts=1,"
                          " run_id=1, lease_until=? WHERE id=?",
                          ("2099-01-01T00:00:00+00:00", a))
        d = self.make()
        d.startup()
        r = self.conn.execute("SELECT * FROM runs WHERE id=1").fetchone()
        self.assertEqual(r["reason"], "crash")
        self.assertIsNotNone(r["ended_at"])
        self.assertEqual(self.msg(a)["status"], "claimed")
        self.assertEqual(len(self.kinds("run.crashed")), 1)

    def test_restart_does_not_rerun_live_work(self):
        a = self.send("backend")
        self.ready()
        gate = threading.Event()
        self.run_fn.gate = gate
        d1 = self.make()
        d1.tick()  # old dispatcher claims; session is "still alive"
        # simulate the old process dying: its run row stays open
        self.conn.execute("UPDATE runs SET ended_at=NULL, reason=NULL")
        d2 = self.make()
        d2.startup()
        self.clock.advance(1800)
        self.run_fn.gate = None
        calls = len(self.run_fn.calls)
        self.assertEqual(d2.tick(), [])
        self.assertEqual(len(self.run_fn.calls), calls)
        self.assertEqual(self.msg(a)["status"], "claimed")
        self.clock.advance(301)  # lease (2100 s) is over
        self.assertEqual(len(self.tick(d2)), 1)
        self.assertEqual(len(self.run_fn.calls), calls + 1)
        self.assertEqual(self.msg(a)["status"], "done")
        gate.set()
        d1.wait()

    def test_long_run_outliving_lease_is_not_duplicated(self):
        a = self.send("backend")
        self.ready()
        gate = threading.Event()
        self.run_fn.gate = gate
        d = self.make()
        d.tick()
        self.clock.advance(5000)
        self.assertEqual(d.tick(), [])
        self.assertEqual(len(self.run_fn.calls), 1)
        self.assertEqual(self.msg(a)["status"], "claimed")
        gate.set()
        d.wait()
        self.assertEqual(self.msg(a)["status"], "done")

    def test_failing_optional_steps_do_not_starve_dispatch(self):
        a = self.send("backend")
        self.ready()
        d = self.make()

        def boom(*args):
            raise RuntimeError("step broke")

        d._wake_assigned = boom
        d._wake_admin = boom
        d.recover_expired = boom
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(self.msg(a)["status"], "done")
        self.assertEqual(len(self.kinds("tick.step_failed")), 3)

    def test_failed_connect_does_not_wedge_bot(self):
        a = self.send("backend")
        self.ready()
        d = self.make()
        real = d._db
        main = threading.current_thread()

        def db():
            if threading.current_thread() is not main:
                raise RuntimeError("cannot connect")
            return real()

        d._db = db
        err = io.StringIO()
        with redirect_stderr(err):
            d.tick()
            d.wait()
        self.assertIn("cannot connect", err.getvalue())
        self.assertEqual(d._running, {})

    def test_serve_runs_startup_then_stops(self):
        self.conn.execute("INSERT INTO runs(bot, started_at) VALUES"
                          " ('backend', ?)", (self.clock(),))
        stop = threading.Event()
        stop.set()
        self.make().serve(stop)
        self.assertEqual(self.conn.execute(
            "SELECT reason FROM runs").fetchone()[0], "crash")


class WakeTests(Base):
    def test_assigned_item_wakes_new_owner(self):
        i = items.new(self.cfg, self.conn, "build it", "backend", status="todo")
        j = items.new(self.cfg, self.conn, "later", "design", status="next")
        k = items.new(self.cfg, self.conn, "doing", "admin", status="doing")
        w = items.new(self.cfg, self.conn, "blocked", "admin", status="todo")
        self.conn.execute("UPDATE items SET waiting_on='design' WHERE id=?", (w,))
        d = self.make()
        d.tick()
        wake = self.conn.execute(
            "SELECT * FROM messages WHERE from_bot='system'"
            " ORDER BY id").fetchall()
        self.assertEqual([(m["to_bot"], m["item_id"]) for m in wake],
                         [("backend", i), ("design", j)])
        d.tick()
        d.tick()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0], 2)  # no extras
        # reassigned: wakes again
        self.ready()
        self.tick(d)
        items.assign(self.conn, i, "design", "admin")
        d.tick()
        self.assertEqual(len(self.conn.execute(
            "SELECT * FROM messages WHERE to_bot='design' AND item_id=?",
            (i,)).fetchall()), 1)

    def test_split_children_wake_owner(self):
        p = items.new(self.cfg, self.conn, "big", "admin", status="doing")
        kids = items.split(self.cfg, self.conn, p, ["a", "b"], "backend")
        d = self.make()
        d.tick()
        woke = [m["item_id"] for m in self.conn.execute(
            "SELECT item_id FROM messages WHERE from_bot='system'"
            " AND to_bot='backend' ORDER BY id").fetchall()]
        self.assertEqual(woke, kids)

    def test_stalled_doing_reverts_to_todo_and_wakes(self):
        i = items.new(self.cfg, self.conn, "stuck", "backend", status="doing")
        # Age the item past the lease with no live run.
        old = (T0 - timedelta(seconds=self.cfg.lease_seconds + 1)
               ).isoformat(timespec="seconds")
        self.conn.execute("UPDATE items SET updated_at=? WHERE id=?", (old, i))
        d = self.make()
        d.tick()
        row = self.conn.execute("SELECT status FROM items WHERE id=?",
                                (i,)).fetchone()
        self.assertEqual(row["status"], "todo")
        self.assertTrue(self.kinds("item.stall_revert"))
        wakes = self.conn.execute(
            "SELECT * FROM messages WHERE from_bot='system' AND item_id=?",
            (i,)).fetchall()
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["to_bot"], "backend")

    def test_waiting_item_is_not_treated_as_stalled(self):
        old = (T0 - timedelta(seconds=self.cfg.lease_seconds + 1)
               ).isoformat(timespec="seconds")
        waiting = items.new(self.cfg, self.conn, "on admin", "backend",
                            status="doing")
        owned = items.new(self.cfg, self.conn, "on owner", "backend",
                          status="doing")
        self.conn.execute(
            "UPDATE items SET waiting_on='admin', updated_at=? WHERE id=?",
            (old, waiting))
        self.conn.execute(
            "UPDATE items SET owner_waiting=1, updated_at=? WHERE id=?",
            (old, owned))
        d = self.make()
        d.tick()
        for i in (waiting, owned):
            status = self.conn.execute(
                "SELECT status FROM items WHERE id=?", (i,)).fetchone()["status"]
            self.assertEqual(status, "doing")
        self.assertFalse(self.kinds("item.stall_revert"))

    def test_idle_admin_returns_to_open_work(self):
        d = self.make()
        d.tick()
        self.assertEqual(self.system_notes(), [])  # nothing open
        requests.capture(self.conn, "please do X", "sess")
        d.tick()
        notes = self.system_notes()
        self.assertEqual(len(notes), 1)
        self.ready()
        self.tick(d)  # admin runs and finishes the note
        self.clock.advance(1800)
        d.tick()
        self.assertEqual(len(self.system_notes()), 1)  # at most once an hour
        self.clock.advance(1801)
        d.tick()
        self.assertEqual(len(self.system_notes()), 1)  # same list: not again
        requests.capture(self.conn, "and also Y", "sess")
        d.tick()
        self.assertEqual(len(self.system_notes()), 2)  # list changed
        self.ready()
        self.tick(d)
        self.clock.advance(24 * 3600 + 1)
        d.tick()
        self.assertEqual(len(self.system_notes()), 3)  # unchanged for 24 h

    def test_open_work_wake_survives_restart_without_extras(self):
        requests.capture(self.conn, "please do X", "sess")
        self.make().tick()
        self.make().tick()
        self.make().tick()
        self.assertEqual(len(self.system_notes()), 1)

    def test_admin_not_woken_while_owner_connected_or_busy(self):
        requests.capture(self.conn, "please do X", "sess")
        self.owner = True
        d = self.make()
        d.tick()
        self.assertEqual(self.system_notes(), [])
        self.owner = False
        self.send("admin", frm="backend")  # something already queued
        d.tick()
        self.assertEqual(self.system_notes(), [])

    def test_paused_items_wake_admin_but_items_waiting_on_owner_do_not(self):
        i = items.new(self.cfg, self.conn, "p", "backend", status="paused")
        j = items.new(self.cfg, self.conn, "q", "backend", status="pending")
        k = items.new(self.cfg, self.conn, "r", "backend", status="paused")
        self.conn.execute("UPDATE items SET owner_waiting=1 WHERE id=?", (k,))
        self.conn.execute("DELETE FROM messages")
        d = self.make()
        d.tick()
        notes = [n for n in self.system_notes() if n["to_bot"] == "admin"]
        self.assertEqual(len(notes), 1)
        self.assertIn(i, notes[0]["body"])
        self.assertNotIn(j, notes[0]["body"])
        self.assertNotIn(k, notes[0]["body"])

    def test_only_owner_waiting_items_do_not_wake_admin(self):
        k = items.new(self.cfg, self.conn, "r", "backend", status="paused")
        j = items.new(self.cfg, self.conn, "q", "backend", status="pending")
        self.conn.execute("UPDATE items SET owner_waiting=1 WHERE id=?", (k,))
        d = self.make()
        d.tick()
        self.assertEqual(self.system_notes(), [])


class ModelMismatchStartTests(Base):
    def test_startup_logs_model_mismatch(self):
        # a thinker forced onto haiku by hand: the start records it once
        self.conn.execute("UPDATE bots SET model='haiku' WHERE name='backend'")
        self.conn.commit()
        d = self.make()
        d.startup()
        rows = self.kinds("model.mismatch")
        self.assertEqual(len(rows), 1)
        self.assertIn("backend", rows[0]["detail"])
        self.assertEqual(rows[0]["ref"], "backend")

    def test_startup_skips_retired_bots(self):
        self.conn.execute("UPDATE bots SET model='haiku', status='retired'"
                          " WHERE name='backend'")
        self.conn.commit()
        self.make().startup()
        self.assertEqual(self.kinds("model.mismatch"), [])


class DoctorModelsTests(Base):
    def test_doctor_lists_mismatch_and_is_quiet_when_ok(self):
        from atobots import doctor
        self.assertNotIn("models", [f.key for f in doctor.checks(
            self.cfg, self.conn, active_fn=lambda: True,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: False,
            disk_usage_fn=lambda p: (100, 10, 90), track=False)])
        self.conn.execute("UPDATE bots SET model='haiku' WHERE name='backend'")
        self.conn.commit()
        found = [f for f in doctor.checks(
            self.cfg, self.conn, active_fn=lambda: True,
            mem_fn=lambda: 4000, live_enabled_fn=lambda: False,
            disk_usage_fn=lambda p: (100, 10, 90), track=False)
            if f.key == "models"]
        self.assertEqual(len(found), 1)
        self.assertIn("backend", found[0].text)


class StatusAndRoutineTests(Base):
    def test_routines_fn_called_each_tick_and_errors_do_not_stop_tick(self):
        seen = []

        def routines(cfg, conn, now):
            seen.append(now)
            raise RuntimeError("cron broke")

        self.send("backend")
        self.ready()
        d = self.make(routines_fn=routines)
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(self.kinds("routines.failed")), 1)

    def test_status_text(self):
        self.send("backend")
        self.send("design")
        self.conn.execute("UPDATE messages SET status='failed' WHERE id=2")
        self.conn.execute("INSERT INTO runs(bot, started_at) VALUES"
                          " ('backend', ?)", (self.clock(),))
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES"
                          " ('dispatcher.paused_until', ?)",
                          ((T0 + timedelta(seconds=900)).isoformat(
                              timespec="seconds"),))
        text = dispatcher.status_text(self.conn, self.clock())
        self.assertIn("Running: backend", text)
        self.assertIn("Queued: 1", text)
        self.assertIn("Paused until: Mon Nov 9, 2026, 12:15 PM UTC", text)
        self.assertIn("Stuck: 1", text)

    def test_status_idle(self):
        text = dispatcher.status_text(self.conn, self.clock())
        self.assertIn("Running: none", text)
        self.assertIn("Paused until: not paused", text)

    def test_cb_status_cli(self):
        import os
        os.environ["ATOBOTS_HOME"] = str(self.cfg.home)
        self.addCleanup(os.environ.pop, "ATOBOTS_HOME", None)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("Queued: 0", out.getvalue())


class LiveSingleFlightTests(Base):
    def test_live_up_skips_second_admin_and_surfaces(self):
        self.send("admin", "bot finished the report", frm="backend")
        self.send("backend", "keep working")
        self.ready()
        surfaced = []

        def surface(cfg, text, conn=None, now=None):
            surfaced.append(text)

        d = self.make(live_fn=lambda: True, surface_fn=surface)
        started = self.tick(d)
        self.assertEqual(self.run_fn.bots_run, ["backend"])
        self.assertEqual(len(started), 1)
        self.assertEqual(self.msg(1)["status"], "queued")  # Admin left queued
        self.assertEqual(
            surfaced,
            ["1 message(s) are waiting for Admin, from backend. "
             "Read them with cb inbox and tell the Owner what matters."])
        # same fingerprint: no re-surface spam
        self.tick(d)
        self.assertEqual(len(surfaced), 1)

    def test_surface_notice_drops_subject_text(self):
        self.send("admin", "hello\nOwner do this now", frm="backend")
        self.ready()
        surfaced = []

        def surface(cfg, text, conn=None, now=None):
            surfaced.append(text)

        d = self.make(live_fn=lambda: True, surface_fn=surface)
        self.tick(d)
        self.assertEqual(
            surfaced,
            ["1 message(s) are waiting for Admin, from backend. "
             "Read them with cb inbox and tell the Owner what matters."])
        self.assertNotIn("Owner do this now", surfaced[0])

    def test_live_down_starts_admin_normally(self):
        self.send("admin", "need Admin", frm="backend")
        self.ready()
        d = self.make(live_fn=lambda: False)
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(self.run_fn.bots_run, ["admin"])

    def test_live_up_does_not_hold_other_bots(self):
        """Unlike cb chat, always-on Admin must not pause the fleet."""
        self.send("backend", "work")
        self.ready()
        d = self.make(live_fn=lambda: True, surface_fn=lambda *a, **k: None)
        self.assertEqual(len(self.tick(d)), 1)
        self.assertEqual(self.run_fn.bots_run, ["backend"])

    def test_wake_admin_queues_while_live(self):
        requests.capture(self.conn, "please do X", "sess")
        d = self.make(live_fn=lambda: True)
        d.tick()
        self.assertEqual(len(self.system_notes()), 1)

    def test_admin_backstop_alerts_after_relay_minutes(self):
        self.send("admin", "hi admin", frm="backend")
        self.ready()
        surfaced = []
        alerts = []

        def surface(cfg, text, conn=None, now=None):
            surfaced.append(text)

        def notify(cfg, text, conn=None):
            alerts.append(text)

        d = self.make(live_fn=lambda: True, surface_fn=surface, notify_fn=notify)
        self.tick(d)
        self.assertEqual(len(surfaced), 1)
        self.assertEqual(alerts, [])

        # Advance 14 minutes (less than default 15): no alert
        self.clock.advance(14 * 60)
        self.tick(d)
        self.assertEqual(alerts, [])

        # Advance 1 minute to reach 15 minutes: backstop alert triggers
        self.clock.advance(60)
        self.tick(d)
        self.assertEqual(
            alerts,
            ["backend answered Admin 15 minutes ago and Admin has not read it yet."])

        # Another tick: alerted only once
        self.clock.advance(60)
        self.tick(d)
        self.assertEqual(len(alerts), 1)


class ServiceFileTests(unittest.TestCase):
    def test_unit(self):
        text = (ENGINE / "systemd" / "atobots-dispatcher.service").read_text()
        for line in ("User=@USER@", "Environment=ATOBOTS_HOME=@HOME@",
                     "Restart=always", "RestartSec=5",
                     "ExecStart=@ENGINE@/bin/cb "
                     "dispatcher run"):
            self.assertIn(line, text.split("\n"))

    def test_admin_unit_has_memorymax(self):
        text = (ENGINE / "systemd" / "atobots-admin.service").read_text()
        self.assertIn("MemoryMax=@MEMORY_MAX@", text)
        self.assertIn("MemorySwapMax=0", text)
        self.assertIn("admin scrub-token", text)


class MemoryTests(unittest.TestCase):
    def test_available_mb_reads_meminfo(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
            f.write("MemTotal: 2000000 kB\nMemAvailable:    1536000 kB\n")
            f.flush()
            self.assertEqual(dispatcher.available_mb(f.name), 1500)
        self.assertEqual(dispatcher.available_mb("/nonexistent/meminfo"), 0)


class WorkerWakeTests(Base):
    names = ("admin", "backend", "design", "scout")

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE bots SET model='haiku', tier='worker'"
                          " WHERE name='scout'")
        self.conn.execute("UPDATE bots SET worker='scout' WHERE name='backend'")

    def wakes(self, bot, item_id):
        return self.conn.execute(
            "SELECT * FROM messages WHERE from_bot='system' AND to_bot=?"
            " AND item_id=?", (bot, item_id)).fetchall()

    def test_wake_assigned_goes_to_worker(self):
        i = items.new(self.cfg, self.conn, "build it", "backend", status="todo")
        d = self.make()
        d.tick()
        self.assertEqual(self.wakes("backend", i), [])
        self.assertEqual(len(self.wakes("scout", i)), 1)
        self.assertIn("Item " + i, self.wakes("scout", i)[0]["body"])

    def test_wake_assigned_uses_existing_task_as_body(self):
        i = items.new(self.cfg, self.conn, "build it", "backend", status="todo")
        tid = tasks.send_task(self.conn, "backend", "scout", i, "list files",
                              [], ["work/x.md"], "exists")
        d = self.make()
        d.tick()
        body = self.wakes("scout", i)[0]["body"]
        self.assertIn(f"message {tid}", body)
        self.assertIn(f"cb task finish {tid} --status done|failed", body)
        self.assertNotIn("Goal:", body)  # the task is queued itself: not embedded

    def test_wake_assigned_ignores_finished_task(self):
        i = items.new(self.cfg, self.conn, "build it", "backend", status="todo")
        tid = tasks.send_task(self.conn, "backend", "scout", i, "list files",
                              [], [], "exists")
        tasks.finish_task(self.cfg, self.conn, tid, "failed", "no")
        self.make().tick()
        body = self.wakes("scout", i)[0]["body"]
        self.assertIn("is assigned to you", body)
        self.assertNotIn("message", body)

    def test_wake_assigned_falls_back_to_owner_without_worker_and_flags(self):
        self.conn.execute("UPDATE bots SET worker=NULL WHERE name='backend'")
        i = items.new(self.cfg, self.conn, "one", "backend", status="todo")
        j = items.new(self.cfg, self.conn, "two", "backend", status="todo")
        d = self.make()
        d.tick()
        self.assertEqual(len(self.wakes("backend", i)), 1)
        self.assertEqual(len(self.wakes("backend", j)), 1)
        self.assertEqual(len(self.kinds("worker.missing")), 1)  # once a day
        self.clock.advance(24 * 3600)
        k = items.new(self.cfg, self.conn, "three", "backend", status="todo")
        self.tick(d)
        self.assertEqual(len(self.wakes("backend", k)), 1)
        self.assertEqual(len(self.kinds("worker.missing")), 2)

    def test_paused_worker_falls_back_to_owner(self):
        self.conn.execute("UPDATE bots SET status='paused' WHERE name='scout'")
        i = items.new(self.cfg, self.conn, "one", "backend", status="todo")
        self.make().tick()
        self.assertEqual(len(self.wakes("backend", i)), 1)
        self.assertEqual(self.wakes("scout", i), [])
        self.assertEqual(len(self.kinds("worker.missing")), 1)

    def test_items_with_two_failures_are_not_woken(self):
        i = items.new(self.cfg, self.conn, "build it", "backend", status="todo")
        self.conn.execute("INSERT INTO meta(key, value) VALUES ('task.fail.' || ?,"
                          " '2')", (i,))
        self.make().tick()
        self.assertEqual(self.wakes("scout", i), [])


if __name__ == "__main__":
    unittest.main()
