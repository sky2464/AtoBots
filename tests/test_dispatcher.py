import io
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(ENGINE))

from claudebot import bots, cli, dispatcher, items, messages, requests  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402
from claudebot.runner import RunResult  # noqa: E402

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
            reindex_fn=kw.pop("reindex_fn", reindex), **kw)
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
        for k in range(12):
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
        notes = self.system_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("backend", notes[0]["subject"])
        # an hour later the old wakes have aged out
        self.clock.advance(3601)
        self.assertEqual(self.tick(d).__len__(), 2)  # backend + the admin note

    def test_owner_connected_defers_new_runs(self):
        self.send("backend")
        self.ready()
        self.owner = True
        d = self.make()
        self.assertEqual(self.tick(d), [])
        self.owner = False
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

    def test_startup_marks_open_runs_crashed_and_requeues_their_messages(self):
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
        self.assertEqual(self.msg(a)["status"], "queued")
        self.assertEqual(len(self.kinds("run.crashed")), 1)

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
        self.assertEqual(len(self.system_notes()), 2)

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
        self.conn.execute("UPDATE items SET waiting_on='owner' WHERE id=?", (k,))
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
        self.conn.execute("UPDATE items SET waiting_on='owner' WHERE id=?", (k,))
        d = self.make()
        d.tick()
        self.assertEqual(self.system_notes(), [])


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
        self.assertIn("Paused until: Mon Nov 9, 2026, 6:15 AM CT", text)
        self.assertIn("Stuck: 1", text)

    def test_status_idle(self):
        text = dispatcher.status_text(self.conn, self.clock())
        self.assertIn("Running: none", text)
        self.assertIn("Paused until: not paused", text)

    def test_cb_status_cli(self):
        import os
        os.environ["CLAUDEBOT_HOME"] = str(self.cfg.home)
        self.addCleanup(os.environ.pop, "CLAUDEBOT_HOME", None)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("Queued: 0", out.getvalue())


class ServiceFileTests(unittest.TestCase):
    def test_unit(self):
        text = (ENGINE / "systemd" / "claudebot-dispatcher.service").read_text()
        for line in ("User=claude", "Environment=CLAUDEBOT_HOME=@HOME@",
                     "Restart=always", "RestartSec=5",
                     "ExecStart=/home/claude/ClaudeBot/engine/bin/cb "
                     "dispatcher run"):
            self.assertIn(line, text.split("\n"))


class MemoryTests(unittest.TestCase):
    def test_available_mb_reads_meminfo(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
            f.write("MemTotal: 2000000 kB\nMemAvailable:    1536000 kB\n")
            f.flush()
            self.assertEqual(dispatcher.available_mb(f.name), 1500)
        self.assertEqual(dispatcher.available_mb("/nonexistent/meminfo"), 0)


if __name__ == "__main__":
    unittest.main()
