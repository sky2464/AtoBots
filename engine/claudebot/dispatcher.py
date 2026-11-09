"""The dispatcher: a small always-on service with no AI in it.

It decides who has work and whether there is room to run them. Each pass
(`tick`) recovers expired claims, fires due routines, queues wake-ups, and
starts one session per bot that has ready work, within the limits for
concurrency, the heavy lane, free memory and the per-bot hourly wake cap.

Every run gets its own worker thread and its own database connection. All
time comes from the injected `clock` and all memory readings from `mem_fn`.
"""
import signal
import sys
import threading
from datetime import datetime, timedelta

from . import (activity, archive, bots, gitlog, items, messages, requests, routines,
               runner, search)
from .config import Config
from .db import connect, ct_display, tx, utcnow

PAUSED_KEY = "dispatcher.paused_until"
_NEVER_STOP = 10 ** 9  # system wake-ups are not part of any bot-to-bot loop
_URGENT = "urgent"
_HOUR = 3600


def available_mb(path: str = "/proc/meminfo") -> int:
    """MemAvailable in MB. An unreadable file counts as 0, so nothing starts."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _plus(iso: str, seconds: float) -> str:
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)
            ).isoformat(timespec="seconds")


def _before(a: str, b: str) -> bool:
    return datetime.fromisoformat(a) < datetime.fromisoformat(b)


def _log(conn, kind: str, detail: str, ref: str | None = None) -> None:
    """Record in the activity table; logging must never raise."""
    try:
        activity.record(conn, "system", kind, detail, ref)
    except Exception as e:  # noqa: BLE001
        print(f"dispatcher: could not log {kind}: {e}", file=sys.stderr)


class _Worker:
    def __init__(self, bot, msgs, lane):
        self.bot, self.msgs, self.lane = bot, msgs, lane
        self.run_id = None
        self.started = threading.Event()
        self.thread = None


class Dispatcher:
    def __init__(self, cfg: Config, run_fn=runner.run, mem_fn=available_mb,
                 clock=utcnow, owner_fn=lambda: False, routines_fn=None,
                 commit_fn=gitlog.commit_run,
                 reindex_fn=search.reindex_records,
                 archive_fn=archive.archive_transcript,
                 daily_fn=archive.daily):
        """`routines_fn(cfg, conn, now)` fires due routines by queueing
        messages (Task 10). `owner_fn()` says whether the Owner is connected
        to a chat (Task 12)."""
        self.cfg = cfg
        self.run_fn, self.mem_fn, self.clock = run_fn, mem_fn, clock
        self.owner_fn = owner_fn
        self.routines_fn = routines_fn
        self.commit_fn, self.reindex_fn = commit_fn, reindex_fn
        self.archive_fn, self.daily_fn = archive_fn, daily_fn
        self._tl = threading.local()
        self._lock = threading.Lock()
        self._running: dict[str, _Worker] = {}
        self._dv = None
        self._dirty = False

    # ---- connections: one per thread ----

    def _db(self):
        c = getattr(self._tl, "conn", None)
        if c is None:
            c = connect(self.cfg)
            self._tl.conn = c
        return c

    def _drop_conn(self) -> None:
        c = getattr(self._tl, "conn", None)
        if c is not None:
            self._tl.conn = None
            c.close()

    def close(self) -> None:
        self.wait()
        self._drop_conn()

    def wait(self) -> None:
        """Join every running worker (used by tests and on shutdown)."""
        while True:
            with self._lock:
                ts = [w.thread for w in self._running.values() if w.thread]
            if not ts:
                return
            for t in ts:
                t.join()

    # ---- startup ----

    def startup(self) -> None:
        """Runs recorded while the dispatcher was alive can be left open by a
        crash. Close them as `crash`. Their messages stay claimed: the old
        session may still be alive in its systemd unit (up to RuntimeMaxSec),
        so only the lease (longer than that) returns the work to the queue."""
        conn = self._db()
        now = self.clock()
        rows = conn.execute(
            "SELECT id, bot FROM runs WHERE ended_at IS NULL").fetchall()
        for r in rows:
            with tx(conn):
                conn.execute("UPDATE runs SET ended_at=?, reason='crash'"
                             " WHERE id=? AND ended_at IS NULL", (now, r["id"]))
                activity.record(conn, "system", "run.crashed",
                                f"{r['bot']} run {r['id']} was open at startup",
                                str(r["id"]))

    # ---- claim / release / finish ----

    def claim(self, bot: str) -> list:
        """Move this bot's queued messages (and any whose lease ran out) to
        claimed in one transaction, with a lease and one more attempt."""
        conn = self._db()
        now = self.clock()
        lease = _plus(now, self.cfg.lease_seconds)
        with tx(conn):
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM messages WHERE to_bot=? AND (status='queued'"
                " OR (status='claimed' AND lease_until<?)) ORDER BY id",
                (bot, now))]
            for i in ids:
                conn.execute("UPDATE messages SET status='claimed',"
                             " lease_until=?, attempts=attempts+1, run_id=NULL"
                             " WHERE id=?", (lease, i))
            if not ids:
                return []
            rows = conn.execute(
                f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(ids))})"
                " ORDER BY id", ids).fetchall()
        return rows

    def _held_ids(self) -> set:
        with self._lock:
            return {m["id"] for w in self._running.values() for m in w.msgs}

    def recover_expired(self) -> int:
        """Return work to the queue when its lease ran out (a crashed or hung
        run). Counts as a failed attempt. Returns how many messages."""
        conn = self._db()
        now = self.clock()
        held = self._held_ids()
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM messages WHERE status='claimed' AND lease_until<?",
            (now,)) if r["id"] not in held]
        return self._release(conn, ids, now)

    def _release(self, conn, ids, now, usage_limit: bool = False) -> int:
        """Claimed messages go back to the queue. After the last allowed attempt
        they fail instead and Admin is told once. A usage limit is not the
        message's fault: its attempt is given back."""
        failed, n = [], 0
        with tx(conn):
            for i in ids:
                r = conn.execute("SELECT * FROM messages WHERE id=? AND"
                                 " status='claimed'", (i,)).fetchone()
                if r is None:
                    continue
                n += 1
                if usage_limit:
                    conn.execute("UPDATE messages SET status='queued',"
                                 " lease_until=NULL, run_id=NULL,"
                                 " attempts=MAX(attempts-1,0) WHERE id=?", (i,))
                elif r["attempts"] >= self.cfg.retry_attempts:
                    conn.execute("UPDATE messages SET status='failed',"
                                 " lease_until=NULL, done_at=? WHERE id=?",
                                 (now, i))
                    activity.record(conn, "system", "message.failed",
                                    f"message {i} to {r['to_bot']} failed "
                                    f"after {r['attempts']} attempts", str(i))
                    failed.append(r)
                else:
                    conn.execute("UPDATE messages SET status='queued',"
                                 " lease_until=NULL, run_id=NULL WHERE id=?",
                                 (i,))
                    activity.record(conn, "system", "message.requeue",
                                    f"message {i} back to the queue", str(i))
        for r in failed:
            self._tell_stuck(conn, r, now)
        return n

    def _tell_stuck(self, conn, r, now) -> None:
        if r["kind"] == "stuck":
            return  # a failed note about a failure must not breed notes
        item = f" (item {r['item_id']})" if r["item_id"] else ""
        try:
            messages.send(
                conn, "system", "admin",
                f"Stuck: message {r['id']} to {r['to_bot']}{item}",
                f"Message {r['id']} to {r['to_bot']}{item}, \"{r['subject']}\", "
                f"failed {r['attempts']} times and will not be retried. Look "
                "at the activity record, then fix the cause, resend it, or "
                "tell the Owner.", kind="stuck", dedupe_key=f"stuck:{r['id']}",
                hop_limit=_NEVER_STOP, now=now)
        except (KeyError, ValueError) as e:
            _log(conn, "message.stuck_unreported", f"message {r['id']}: {e}",
                 str(r["id"]))

    def finish(self, run_id, result, msgs) -> None:
        """Settle a run's claimed messages. Done only on `ok`."""
        conn = self._db()
        now = self.clock()
        ids = [m["id"] if hasattr(m, "keys") else m for m in msgs]
        reason = getattr(result, "reason", None) or "error"
        if reason == "ok":
            live = [i for i in ids if conn.execute(
                "SELECT 1 FROM messages WHERE id=? AND status='claimed'",
                (i,)).fetchone()]
            messages.complete(conn, live)
        elif reason == "usage_limit":
            self._release(conn, ids, now, usage_limit=True)
            until = _plus(now, self.cfg.usage_limit_retry_seconds)
            with tx(conn):
                conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                             " VALUES (?,?)", (PAUSED_KEY, until))
                activity.record(conn, "system", "queue.paused",
                                f"usage limit hit; paused until {until}",
                                str(run_id))
        else:
            self._release(conn, ids, now)

    def _commit(self, conn, bot: str, msgs, run_id) -> None:
        item_ids = sorted({m["item_id"] for m in msgs if m["item_id"]})
        try:
            sha = self.commit_fn(self.cfg, bot, item_ids, run_id)
            if sha and run_id:
                conn.execute("UPDATE runs SET commit_sha=? WHERE id=?",
                             (sha, run_id))
        except Exception as e:  # noqa: BLE001
            _log(conn, "commit.failed", f"{bot} run {run_id}: {e}",
                 str(run_id))

    # ---- workers ----

    def _work(self, w: _Worker) -> None:
        try:
            self._work_body(w, self._db())
        except Exception as e:  # noqa: BLE001
            # e.g. no connection; the lease brings the messages back.
            print(f"dispatcher: worker for {w.bot.name} failed: {e}",
                  file=sys.stderr)
        finally:
            w.started.set()
            with self._lock:
                self._running.pop(w.bot.name, None)
            self._drop_conn()

    def _work_body(self, w: _Worker, conn) -> None:
        ids = [m["id"] for m in w.msgs]

        def on_start(run_id):
            w.run_id = run_id
            conn.execute(f"UPDATE messages SET run_id=? WHERE status='claimed'"
                         f" AND id IN ({','.join('?' * len(ids))})",
                         (run_id, *ids))
            w.started.set()

        result = None
        try:
            result = self.run_fn(self.cfg, conn, w.bot, w.msgs, w.lane,
                                 on_start=on_start)
        except Exception as e:  # noqa: BLE001
            _log(conn, "run.exception", f"{w.bot.name}: {type(e).__name__}"
                 f": {e}", str(w.run_id) if w.run_id else None)
            if w.run_id:
                conn.execute("UPDATE runs SET ended_at=?, reason='error'"
                             " WHERE id=? AND ended_at IS NULL",
                             (self.clock(), w.run_id))
        run_id = result.run_id if result else w.run_id
        try:
            self.finish(run_id, result, w.msgs)
        except Exception as e:  # noqa: BLE001
            # Messages stay claimed; the lease brings them back.
            _log(conn, "finish.failed", f"run {run_id}: {e}",
                 str(run_id) if run_id else None)
        self._commit(conn, w.bot.name, w.msgs, run_id)
        self._archive(conn, run_id)

    def _archive(self, conn, run_id) -> None:
        """Move the run's transcript to the archive; a failure is logged, never
        fails the run."""
        if not run_id:
            return
        try:
            self.archive_fn(self.cfg, conn, run_id)
        except Exception as e:  # noqa: BLE001
            _log(conn, "archive.failed", f"run {run_id}: {type(e).__name__}: {e}",
                 str(run_id))

    def _archive_daily(self, conn, now) -> None:
        """Archive old records and check Memory caps once a day."""
        today = now[:10]
        r = conn.execute("SELECT value FROM meta WHERE key=?",
                         (archive.DAILY_KEY,)).fetchone()
        if r is not None and r["value"] == today:
            return
        # Marked first, so a failure is retried tomorrow, not every pass.
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                     (archive.DAILY_KEY, today))
        try:
            self.daily_fn(self.cfg, conn, now)
        except Exception as e:  # noqa: BLE001
            _log(conn, "archive.daily_failed", f"{type(e).__name__}: {e}")

    # ---- the pass ----

    def tick(self) -> list:
        """One pass. Returns the IDs of the runs it started."""
        conn = self._db()
        now = self.clock()
        for name, step in (("recover_expired", self.recover_expired),
                           ("routines", lambda: self._call_routines(conn, now)),
                           ("wake_assigned",
                            lambda: self._wake_assigned(conn, now)),
                           ("wake_admin", lambda: self._wake_admin(conn, now)),
                           ("archive_daily",
                            lambda: self._archive_daily(conn, now)),
                           ("reindex", lambda: self._reindex(conn))):
            try:
                step()
            except Exception as e:  # noqa: BLE001
                _log(conn, "tick.step_failed",
                     f"{name}: {type(e).__name__}: {e}")
        if self._paused(conn, now):
            return []
        try:
            if self.owner_fn():
                return []
        except Exception as e:  # noqa: BLE001
            _log(conn, "owner_check.failed", str(e))
        return self._start_ready(conn, now)

    def _call_routines(self, conn, now) -> None:
        if self.routines_fn is None:
            return
        try:
            self.routines_fn(self.cfg, conn, now)
            self._dirty = True
        except Exception as e:  # noqa: BLE001
            _log(conn, "routines.failed", f"{type(e).__name__}: {e}")

    def _reindex(self, conn) -> None:
        """Only when the database changed since the last look."""
        dv = conn.execute("PRAGMA data_version").fetchone()[0]
        if dv == self._dv and not self._dirty:
            return
        self._dirty = False
        try:
            self.reindex_fn(self.cfg, conn)
        except Exception as e:  # noqa: BLE001
            _log(conn, "search.reindex_failed", f"{type(e).__name__}: {e}")
        self._dv = conn.execute("PRAGMA data_version").fetchone()[0]

    def _paused(self, conn, now) -> bool:
        r = conn.execute("SELECT value FROM meta WHERE key=?",
                         (PAUSED_KEY,)).fetchone()
        if r is None:
            return False
        if _before(now, r["value"]):
            return True
        conn.execute("DELETE FROM meta WHERE key=?", (PAUSED_KEY,))
        _log(conn, "queue.resumed", "usage-limit pause is over")
        return False

    def _system_send(self, conn, to, subject, body, key, item_id=None,
                     level="normal", now=None) -> int:
        try:
            mid = messages.send(conn, "system", to, subject, body,
                                item_id=item_id, kind="wake", level=level,
                                dedupe_key=key, hop_limit=_NEVER_STOP, now=now)
        except (KeyError, ValueError) as e:
            _log(conn, "wake.failed", f"{key}: {e}")
            return 0
        self._dirty = True
        return mid

    def _wake_assigned(self, conn, now) -> None:
        """An item that is todo or next, waiting on nobody, wakes its owner
        once per assignment."""
        active = {b.name for b in bots.list_bots(conn)}
        rows = conn.execute(
            "SELECT i.id, i.owner, i.title, (SELECT COUNT(*) FROM activity a"
            " WHERE a.kind='item.assign' AND a.ref=i.id) AS assigns FROM items i"
            " WHERE i.status IN ('todo','next') AND i.archived_at IS NULL"
            " AND (i.waiting_on IS NULL OR i.waiting_on='') ORDER BY i.n"
        ).fetchall()
        for r in rows:
            if r["owner"] not in active:
                continue
            key = f"assign:{r['id']}:{r['owner']}:{r['assigns']}"
            if conn.execute("SELECT 1 FROM messages WHERE dedupe_key=?",
                            (key,)).fetchone():
                continue
            self._system_send(conn, r["owner"], f"{r['id']} is yours: "
                              f"{r['title']}",
                              f"Item {r['id']} is assigned to you. Read its "
                              "folder and handoff notes and carry on.", key,
                              item_id=r["id"], now=now)

    def _wake_admin(self, conn, now) -> None:
        """Admin goes back to open work at most once an hour, when nothing
        is queued for it and the Owner is not in a chat."""
        bot = "admin"
        if bot not in {b.name for b in bots.list_bots(conn)}:
            return
        with self._lock:
            if bot in self._running:
                return
        if conn.execute("SELECT 1 FROM messages WHERE to_bot=? AND status IN"
                        " ('queued','claimed')", (bot,)).fetchone():
            return
        try:
            if self.owner_fn():
                return
        except Exception:  # noqa: BLE001
            pass
        key = f"wake.openwork.{bot}"
        last = conn.execute("SELECT value FROM meta WHERE key=?",
                            (key,)).fetchone()
        if last and _before(now, _plus(last["value"], _HOUR)):
            return
        unhandled = requests.unhandled(conn)
        paused = conn.execute(
            "SELECT id, title FROM items WHERE status='paused'"
            " AND archived_at IS NULL AND COALESCE(waiting_on,'')!='owner'"
            " ORDER BY n").fetchall()
        if not unhandled and not paused:
            return
        lines = []
        if unhandled:
            lines.append(f"{len(unhandled)} Owner request(s) not yet turned "
                         "into items (cb unhandled).")
        lines += [f"Paused: {p['id']} {p['title']}" for p in paused]
        mid = self._system_send(
            conn, bot, "Open work", "\n".join(lines) + "\nPick up what you can.",
            f"openwork:{bot}:{now[:13]}", now=now)
        if mid:
            with tx(conn):
                conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                             " VALUES (?,?)", (key, now))

    def _wake_capped(self, conn, bot: str, now: str) -> bool:
        n = conn.execute("SELECT COUNT(*) FROM runs WHERE bot=? AND"
                         " started_at>?", (bot, _plus(now, -_HOUR))
                         ).fetchone()[0]
        if n < self.cfg.wakes_per_bot_per_hour:
            return False
        self._system_send(
            conn, "admin", f"Wake cap reached for {bot}",
            f"{bot} has been started {n} times in the last hour, which is the "
            f"limit ({self.cfg.wakes_per_bot_per_hour}). Its queued work "
            "waits. Check whether it is looping.", f"wakecap:{bot}:{now[:13]}",
            now=now)
        return True

    def _start_ready(self, conn, now) -> list:
        active = {b.name: b for b in bots.list_bots(conn)}
        groups: dict[str, list] = {}
        for m in conn.execute("SELECT * FROM messages WHERE status='queued'"
                              " ORDER BY id"):
            groups.setdefault(m["to_bot"], []).append(m)
        cands = []
        for name, msgs in groups.items():
            if name not in active:
                continue
            urgent = any(m["priority"] == _URGENT for m in msgs)
            oldest = msgs[0]["created_at"]
            wait_over = not _before(now, _plus(oldest,
                                               self.cfg.batch_delay_seconds))
            if urgent or wait_over:
                cands.append((not urgent, msgs[0]["id"], name,
                              any(m["level"] == "heavy" for m in msgs)))
        cands.sort()
        started: list[_Worker] = []
        for _, _, name, heavy in cands:
            with self._lock:
                if name in self._running:
                    continue
                if len(self._running) >= self.cfg.max_concurrent_runs:
                    break
                heavy_now = sum(1 for w in self._running.values()
                                if w.lane == "heavy")
            if heavy and heavy_now >= self.cfg.heavy_lane:
                continue
            if self.mem_fn() < self.cfg.memory_floor_mb:
                break
            if self._wake_capped(conn, name, now):
                continue
            msgs = self.claim(name)
            if not msgs:
                continue
            lane = "heavy" if any(m["level"] == "heavy" for m in msgs) \
                else "normal"
            w = _Worker(active[name], msgs, lane)
            w.thread = threading.Thread(target=self._work, args=(w,),
                                        name=f"run-{name}", daemon=True)
            with self._lock:
                self._running[name] = w
            started.append(w)
            w.thread.start()
        ids = []
        for w in started:
            w.started.wait(60)
            if w.run_id:
                ids.append(w.run_id)
        return ids

    # ---- the loop ----

    def serve(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
        self.startup()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                print(f"dispatcher: tick failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
                _log(self._db(), "dispatcher.tick_failed",
                     f"{type(e).__name__}: {e}")
            stop.wait(self.cfg.poll_seconds)


# ---- status and CLI ----

def status_text(conn, now: str | None = None) -> str:
    now = now or utcnow()
    running = conn.execute("SELECT bot, id, started_at FROM runs WHERE"
                           " ended_at IS NULL ORDER BY id").fetchall()
    queued = conn.execute("SELECT COUNT(*) FROM messages WHERE status='queued'"
                          ).fetchone()[0]
    stuck = conn.execute("SELECT COUNT(*) FROM messages WHERE status='failed'"
                         ).fetchone()[0]
    r = conn.execute("SELECT value FROM meta WHERE key=?",
                     (PAUSED_KEY,)).fetchone()
    paused = ct_display(r["value"]) if r and _before(now, r["value"]) \
        else "not paused"
    run_text = ", ".join(f"{x['bot']} (run {x['id']}, since "
                         f"{ct_display(x['started_at'])})" for x in running
                         ) or "none"
    return (f"Running: {run_text}\nQueued: {queued}\n"
            f"Paused until: {paused}\nStuck: {stuck}\n")


def _cmd_run(args) -> int:
    Dispatcher(Config.load(), routines_fn=routines.due).serve()
    return 0


def _cmd_status(args) -> int:
    cfg = Config.load()
    conn = connect(cfg)
    try:
        print(status_text(conn), end="")
    finally:
        conn.close()
    return 0


def register(subparsers) -> None:
    d = subparsers.add_parser("dispatcher", help="the always-on dispatcher")
    dsub = d.add_subparsers(dest="dispatcher_command", required=True)
    r = dsub.add_parser("run", help="run the dispatcher loop (systemd does this)")
    r.set_defaults(func=_cmd_run)
    s = subparsers.add_parser("status", help="running bots, queue, pauses")
    s.set_defaults(func=_cmd_status)
