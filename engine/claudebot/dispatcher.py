"""The dispatcher: a small always-on service with no AI in it.

It decides who has work and whether there is room to run them. Each pass
(`tick`) recovers expired claims, fires due routines, queues wake-ups, and
starts one session per bot that has ready work, within the limits for
concurrency, the heavy lane, free memory and the soft hourly wake cap
(per model: opus/sonnet/haiku; Owner and Admin-urgent traffic bypass it).

Every run gets its own worker thread and its own database connection. All
time comes from the injected `clock` and all memory readings from `mem_fn`.
"""
import hashlib
import json
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timedelta

from . import (activity, archive, bots, chat, gitlog, items, messages, requests, routines,
               runner, search, tasks)
from .config import Config
from .db import connect, ct_display, tx, utcnow

PAUSED_KEY = "dispatcher.paused_until"
_NEVER_STOP = 10 ** 9  # system wake-ups are not part of any bot-to-bot loop
_URGENT = "urgent"
_HOUR = 3600
# Message kinds that need a reply or an explicit finish before an `ok` run
# may mark them done. Notes/answers/wakes may end without a reply.
_ACTION_KINDS = frozenset({"question", "task"})
# Activity kinds that count as explicit progress on an item during a run.
_PROGRESS_KINDS = frozenset({
    "item.status", "item.pause", "item.resume", "item.assign",
    "item.waiting", "item.unwaiting", "item.owner_waiting",
    "task.finish",
})


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
                 daily_fn=archive.daily, live_fn=None,
                 surface_fn=None):
        """`routines_fn(cfg, conn, now)` fires due routines by queueing
        messages. `owner_fn()` says whether the Owner is connected
        to a chat — that holds *all* new runs. `live_fn()` says
        whether always-on Admin is up; that only blocks a second Admin run
        (bots keep working). `surface_fn` delivers Admin-bound results to
        live/Telegram when the second Admin is skipped."""
        self.cfg = cfg
        self.run_fn, self.mem_fn, self.clock = run_fn, mem_fn, clock
        self.owner_fn = owner_fn
        self.live_fn = live_fn
        self.surface_fn = surface_fn
        self.routines_fn = routines_fn
        self.commit_fn, self.reindex_fn = commit_fn, reindex_fn
        self.archive_fn, self.daily_fn = archive_fn, daily_fn
        self._tl = threading.local()
        self._lock = threading.Lock()
        self._running: dict[str, _Worker] = {}
        self._dv = None
        self._dirty = False
        self._surfaced = None  # last surfaced admin-queue message ids
        self._step_errors: dict[str, str] = {}

    def _live_up(self) -> bool:
        """Always-on Admin is running (single-flight: no second Admin)."""
        if self.live_fn is not None:
            try:
                return bool(self.live_fn())
            except Exception:  # noqa: BLE001
                return False
        from . import admin_live
        try:
            return admin_live.is_running(self.cfg)
        except Exception:  # noqa: BLE001
            return False

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
        from . import admin_live  # late: it imports chat
        # Chat and always-on sessions close their own rows; one still open
        # while its process holds its lock is alive, not crashed.
        alive = {"chat": chat.owner_connected(self.cfg),
                 "live": admin_live.is_running(self.cfg)}
        rows = [r for r in conn.execute(
            "SELECT id, bot, lane FROM runs WHERE ended_at IS NULL"
        ).fetchall() if not alive.get(r["lane"])]
        for r in rows:
            with tx(conn):
                conn.execute("UPDATE runs SET ended_at=?, reason='crash'"
                             " WHERE id=? AND ended_at IS NULL", (now, r["id"]))
                activity.record(conn, "system", "run.crashed",
                                f"{r['bot']} run {r['id']} was open at startup",
                                str(r["id"]))
        # Models that disagree with the tier rule. Recorded, never changed:
        # the Owner sets the model.
        with tx(conn):
            for bot, model, problem in bots.model_mismatches(conn):
                activity.record(conn, "system", "model.mismatch",
                                f"{bot} is on {model}: {problem}", bot)

    # ---- claim / release / finish ----

    def claim(self, bot: str, hold: bool = False) -> list:
        """Move this bot's queued messages (and any whose lease ran out) to
        claimed in one transaction, with a lease and one more attempt. A run
        takes at most `batch_max_messages` messages and `batch_max_bytes` of
        text; the rest wait for the next run. While the Owner is in a chat
        (`hold`) only Admin's urgent
        messages to other bots are taken."""
        conn = self._db()
        now = self.clock()
        lease = _plus(now, self.cfg.lease_seconds)
        extra = (" AND priority=? AND from_bot='admin' AND to_bot!='admin'"
                 if hold else "")
        with tx(conn):
            ids, size = [], 0
            for r in conn.execute(
                    "SELECT id, LENGTH(CAST(COALESCE(subject,'')||"
                    "COALESCE(body,'') AS BLOB)) AS sz FROM messages WHERE"
                    " to_bot=? AND (status='queued' OR (status='claimed' AND"
                    f" lease_until<?)){extra} ORDER BY id",
                    (bot, now, *((_URGENT,) if hold else ()))):
                if ids and (len(ids) >= self.cfg.batch_max_messages or
                            size + r["sz"] > self.cfg.batch_max_bytes):
                    break
                ids.append(r["id"])
                size += r["sz"]
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
        """Settle a run's claimed messages. Done on `ok` only when the message
        needs no action, or a reply / task finish / item progress was recorded.
        A bare `ok` with empty progress requeues action messages (this uses one
        attempt; after `retry_attempts` the message fails) so they are not
        silently dropped."""
        conn = self._db()
        now = self.clock()
        ids = [m["id"] if hasattr(m, "keys") else m for m in msgs]
        reason = getattr(result, "reason", None) or "error"
        if reason == "ok":
            done, pending = [], []
            for i in ids:
                m = conn.execute(
                    "SELECT * FROM messages WHERE id=? AND status='claimed'",
                    (i,)).fetchone()
                if m is None:
                    continue
                if self._progress_ok(conn, m, run_id):
                    done.append(i)
                else:
                    pending.append(i)
            if done:
                messages.complete(conn, done)
            if pending:
                self._release(conn, pending, now, usage_limit=True)
                for i in pending:
                    _log(conn, "message.no_progress",
                         f"message {i} not done: ok run with no reply/progress",
                         str(i))
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

    def _progress_ok(self, conn, m, run_id) -> bool:
        """True when this claimed message may be marked done after an ok run.
        Replies are not stored as a foreign key; an answer/result later in the
        same chain, a finished task, or item activity during the run counts."""
        if m["kind"] not in _ACTION_KINDS:
            return True
        if m["chain_id"] and conn.execute(
                "SELECT 1 FROM messages WHERE chain_id=? AND id>? AND"
                " kind IN ('answer','result') LIMIT 1",
                (m["chain_id"], m["id"])).fetchone():
            return True
        if m["kind"] == "task" and tasks.finished(conn, m["id"]):
            return True
        if m["item_id"]:
            since = None
            if run_id:
                r = conn.execute("SELECT started_at FROM runs WHERE id=?",
                                 (run_id,)).fetchone()
                since = r["started_at"] if r else None
            if since and conn.execute(
                    f"SELECT 1 FROM activity WHERE actor=? AND ref=? AND at>=?"
                    f" AND kind IN ({','.join('?' * len(_PROGRESS_KINDS))})"
                    f" LIMIT 1",
                    (m["to_bot"], m["item_id"], since, *_PROGRESS_KINDS)
                    ).fetchone():
                return True
        return False

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
        """Archive old records, retry failed transcripts and check Memory caps
        once a day. After a failure it tries again at most once an hour and
        tells Admin once per day."""
        today = now[:10]
        get = lambda k: conn.execute(  # noqa: E731
            "SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        r = get(archive.DAILY_KEY)
        if r is not None and r["value"] == today:
            return
        t = get(archive.DAILY_TRY_KEY)
        if t is not None and not _before(_plus(t["value"], _HOUR), now):
            return
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                     (archive.DAILY_TRY_KEY, now))
        try:
            self.daily_fn(self.cfg, conn, now)
        except Exception as e:  # noqa: BLE001
            detail = f"{type(e).__name__}: {e}"
            _log(conn, "archive.daily_failed", detail)
            self._system_send(
                conn, "admin", "Daily archiving failed",
                f"The daily archive step failed: {detail}. Nothing was deleted "
                "without a verified copy. Check free disk space (df -h) and "
                "`cb archive status`. It is retried hourly.",
                f"archive-daily-fail:{today}", now=now)
            return
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                     (archive.DAILY_KEY, today))

    # ---- the pass ----

    def tick(self) -> list:
        """One pass. Returns the IDs of the runs it started."""
        conn = self._db()
        now = self.clock()
        for name, step in (("recover_expired", self.recover_expired),
                           ("stall_doing",
                            lambda: self._stall_doing(conn, now)),
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
                text = f"{type(e).__name__}: {e}"
                prev = self._step_errors.get(name)
                if prev != text:
                    self._step_errors[name] = text
                    _log(conn, "tick.step_failed", f"{name}: {text}")
            else:
                self._step_errors.pop(name, None)
        if self._paused(conn, now):
            return []
        hold = False
        try:
            hold = bool(self.owner_fn())
        except Exception as e:  # noqa: BLE001
            _log(conn, "owner_check.failed", str(e))
        return self._start_ready(conn, now, hold)

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

    def _stall_doing(self, conn, now) -> None:
        """A `doing` item with no live run past the lease age is stuck: revert
        it to `todo` so `_wake_assigned` can wake the owner again. Tell Admin
        once per stall window when the owner is missing or inactive."""
        limit = _plus(now, -self.cfg.lease_seconds)
        active = {b.name for b in bots.list_bots(conn, include_reserved=True)}
        rows = conn.execute(
            "SELECT id, owner, title, updated_at FROM items WHERE status='doing'"
            " AND archived_at IS NULL AND updated_at<? ORDER BY n",
            (limit,)).fetchall()
        for r in rows:
            if conn.execute(
                    "SELECT 1 FROM runs WHERE bot=? AND ended_at IS NULL",
                    (r["owner"],)).fetchone():
                continue
            with tx(conn):
                cur = conn.execute(
                    "UPDATE items SET status='todo', updated_at=? WHERE id=?"
                    " AND status='doing'", (now, r["id"]))
                if cur.rowcount == 0:
                    continue
                activity.record(conn, "system", "item.stall_revert",
                                f"{r['id']}: doing -> todo after lease age "
                                f"with no live run", r["id"])
            if r["owner"] not in active:
                self._system_send(
                    conn, "admin", f"Stalled item {r['id']}",
                    f"Item {r['id']} ({r['title']}) was stuck in doing past "
                    f"the {self.cfg.lease_seconds}s lease with no live run. "
                    f"It was reverted to todo; owner {r['owner']} is not "
                    "active.", f"stall:{r['id']}:{r['owner']}",
                    item_id=r["id"], now=now)

    def _wake_assigned(self, conn, now) -> None:
        """An item that is todo or next, waiting on nobody, wakes its owner
        once per assignment. Split children are created as todo so they wake."""
        active = {b.name for b in bots.list_bots(conn, include_reserved=True)}
        rows = conn.execute(
            "SELECT i.id, i.owner, i.title, (SELECT COUNT(*) FROM activity a"
            " WHERE a.kind='item.assign' AND a.ref=i.id) AS assigns FROM items i"
            " WHERE i.status IN ('todo','next') AND i.archived_at IS NULL"
            " AND (i.waiting_on IS NULL OR i.waiting_on='') ORDER BY i.n"
        ).fetchall()
        for r in rows:
            if r["owner"] not in active:
                continue
            if tasks.failures(conn, r["id"]) >= tasks.ESCALATE_AT:
                continue  # a worker failed it twice: Admin has been told
            stall_n = conn.execute(
                "SELECT COUNT(*) FROM activity WHERE kind='item.stall_revert'"
                " AND ref=?", (r["id"],)).fetchone()[0]
            key = f"assign:{r['id']}:{r['owner']}:{r['assigns']}"
            if stall_n > 0:
                key += f":s{stall_n}"
            if stall_n >= 3:
                self._system_send(
                    conn, "admin", f"Item {r['id']} keeps stalling",
                    f"Item {r['id']} ({r['title']}) has been reverted from "
                    "doing to todo at least three times. Check what is blocking "
                    "it.", f"stall-limit:{r['id']}", item_id=r["id"], now=now)
                continue
            if conn.execute("SELECT 1 FROM messages WHERE dedupe_key=?",
                            (key,)).fetchone():
                continue
            text = (f"Item {r['id']} is assigned to you. Read its folder and "
                    "handoff notes and carry on.")
            target = self._wake_target(conn, r["owner"], now)
            body = text if target == r["owner"] else tasks.wake_body(
                conn, r["id"], target, text)
            self._system_send(conn, target, f"{r['id']} is yours: "
                              f"{r['title']}", body, key,
                              item_id=r["id"], now=now)

    def _wake_target(self, conn, owner: str, now: str) -> str:
        """The owner's worker when it is an active worker-tier bot, else the
        owner. Without one, `worker.missing` is recorded once per bot per day."""
        worker = bots.get(conn, owner).worker
        if worker:
            try:
                w = bots.get(conn, worker)
            except KeyError:
                w = None
            if w is not None and w.status == "active" and w.tier == "worker":
                return worker
        day = now[:10]
        key = f"worker.missing.{owner}"
        r = conn.execute("SELECT value FROM meta WHERE key=?",
                         (key,)).fetchone()
        if r is None or r["value"] != day:
            with tx(conn):
                conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                             " VALUES (?,?)", (key, day))
                activity.record(conn, "system", "worker.missing",
                                f"{owner} has no active worker; its work is "
                                "done by its own session", owner)
        return owner

    def _wake_admin(self, conn, now) -> None:
        """Admin goes back to open work when nothing is queued for it and the
        Owner is not in a chat, at most once an hour, and only if the list of
        open work changed since the last wake or 24 hours have passed. Items
        waiting on the Owner are not Admin's to chase. Skipped while
        always-on Admin is already up (single-flight)."""
        bot = "admin"
        if bot not in {b.name for b in bots.list_bots(conn, include_reserved=True)}:
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
        dkey = key + ".digest"
        get = lambda k: conn.execute(  # noqa: E731
            "SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        last = get(key)
        if last and _before(now, _plus(last["value"], _HOUR)):
            return
        # A request the always-on or chat session is already handling (it came
        # from a live session, recently) is not a reason for a second Admin.
        recent = _plus(now, -30 * 60)
        unhandled = [r for r in requests.unhandled(conn)
                     if not (r["received_at"] > recent and conn.execute(
                         "SELECT 1 FROM runs WHERE session_id=? AND"
                         " ended_at IS NULL AND lane IN ('chat','live')",
                         (r["session_id"],)).fetchone())]
        paused = conn.execute(
            "SELECT id, title FROM items WHERE status='paused'"
            " AND archived_at IS NULL AND COALESCE(owner_waiting,0)=0"
            " ORDER BY n").fetchall()
        if not unhandled and not paused:
            return
        lines = []
        if unhandled:
            lines.append(f"{len(unhandled)} Owner request(s) not yet turned "
                         "into items (cb request unhandled).")
        lines += [f"  R-{r['id']} via {r['source']}: {r['text'][:200]}"
                  for r in unhandled if r["source"]]
        lines += [f"Paused: {p['id']} {p['title']}" for p in paused]
        # What this wake says, by identity of the things in it. The same list
        # is not sent again for 24 hours.
        digest = hashlib.sha256("\n".join(
            [f"R-{r['id']}" for r in unhandled]
            + [f"{p['id']}:{p['title']}" for p in paused]).encode()
        ).hexdigest()
        prev = get(dkey)
        if last and prev and prev["value"] == digest and \
                _before(now, _plus(last["value"], 24 * _HOUR)):
            return
        mid = self._system_send(
            conn, bot, "Open work", "\n".join(lines) + "\nPick up what you can.",
            f"openwork:{bot}:{now[:13]}", now=now)
        if mid:
            with tx(conn):
                for k, v in ((key, now), (dkey, digest)):
                    conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                                 " VALUES (?,?)", (k, v))

    def _wake_limit(self, bot) -> int:
        """Soft hourly cap for this bot's model. Opus/Sonnet/Haiku each have
        their own default; anything else uses wakes_per_bot_per_hour."""
        model = (getattr(bot, "model", None) or "").lower()
        if "opus" in model:
            return self.cfg.wake_cap_opus
        if "haiku" in model:
            return self.cfg.wake_cap_haiku
        if "sonnet" in model:
            return self.cfg.wake_cap_sonnet
        return self.cfg.wakes_per_bot_per_hour

    def _owner_bypasses_cap(self, msgs) -> bool:
        """Owner traffic and Admin urgent wakes are never soft-capped."""
        return any(
            m["from_bot"] == "owner" or
            (m["from_bot"] == "admin" and m["priority"] == _URGENT)
            for m in msgs)

    def _wake_capped(self, conn, bot, now: str, msgs) -> bool:
        if self._owner_bypasses_cap(msgs):
            return False
        limit = self._wake_limit(bot)
        name = bot if isinstance(bot, str) else bot.name
        # Only dispatcher lanes count: chat/live Admin sessions must not
        # burn the wake cap.
        n = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE bot=? AND started_at>?"
            " AND COALESCE(lane,'normal') IN ('normal','heavy')",
            (name, _plus(now, -_HOUR))).fetchone()[0]
        if n < limit:
            return False
        self._system_send(
            conn, "admin", f"Wake cap reached for {name}",
            f"{name} has been started {n} times in the last hour, which is the "
            f"limit ({limit}). Its queued work "
            "waits. Check whether it is looping.", f"wakecap:{name}:{now[:13]}",
            now=now)
        return True

    def _load_surfaced(self, conn) -> tuple | None:
        if self._surfaced is not None:
            return self._surfaced
        r = conn.execute("SELECT value FROM meta WHERE key=?",
                         ("admin.surfaced",)).fetchone()
        if r is None:
            self._surfaced = None
            return None
        self._surfaced = tuple(json.loads(r["value"]))
        return self._surfaced

    def _save_surfaced(self, conn, ids: tuple) -> None:
        self._surfaced = ids
        with tx(conn):
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                         ("admin.surfaced", json.dumps(list(ids))))

    def _surface_admin(self, conn, msgs, now: str) -> None:
        """When live Admin is up, do not start a second Admin: leave the
        messages queued and surface a digest to the live session / Telegram."""
        ids = tuple(m["id"] for m in msgs)
        if ids == self._load_surfaced(conn):
            return
        text = (f"{len(msgs)} message(s) are waiting for Admin. "
                "Run: cb inbox")
        try:
            if self.surface_fn is not None:
                self.surface_fn(self.cfg, text, conn=conn, now=now)
            else:
                from . import admin_live
                admin_live.surface(self.cfg, text, key="admin-queue",
                                   conn=conn, now=now)
            self._save_surfaced(conn, ids)
            _log(conn, "admin_live.surface",
                 f"surfaced {len(msgs)} queued Admin message(s)", "admin")
        except Exception as e:  # noqa: BLE001
            _log(conn, "admin_live.surface_failed",
                 f"{type(e).__name__}: {e}")

    def _batch_delay_for(self, bot) -> int:
        if getattr(bot, "tier", None) == "worker":
            return self.cfg.worker_batch_delay_seconds
        return self.cfg.batch_delay_seconds

    def _start_ready(self, conn, now, hold: bool = False) -> list:
        """`hold`: the Owner is in a chat, so only Admin's urgent messages to
        other bots (such as the test wake of a new bot) may start a run.
        Always-on Admin (`live_fn`) does not hold other bots; it only blocks
        a second Admin and surfaces its queue instead."""
        active = {b.name: b for b in bots.list_bots(conn, include_reserved=True)}
        live_up = self._live_up()
        groups: dict[str, list] = {}
        for m in conn.execute("SELECT * FROM messages WHERE status='queued'"
                              " ORDER BY id"):
            if hold and not (m["priority"] == _URGENT and
                             m["from_bot"] == "admin" and
                             m["to_bot"] != "admin"):
                continue
            groups.setdefault(m["to_bot"], []).append(m)
        cands = []
        for name, msgs in groups.items():
            if name not in active:
                continue
            if name == "admin" and live_up:
                self._surface_admin(conn, msgs, now)
                continue
            urgent = any(m["priority"] == _URGENT for m in msgs)
            oldest = msgs[0]["created_at"]
            delay = self._batch_delay_for(active[name])
            wait_over = not _before(now, _plus(oldest, delay))
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
            queued = groups[name]
            if self._wake_capped(conn, active[name], now, queued):
                continue
            msgs = self.claim(name, hold)
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
                           " ended_at IS NULL AND COALESCE(lane,'')!='live'"
                           " ORDER BY id").fetchall()
    live = conn.execute("SELECT 1 FROM runs WHERE ended_at IS NULL AND"
                        " lane='live'").fetchone()
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
            f"Paused until: {paused}\nStuck: {stuck}\n"
            + ("Always-on Admin: up\n" if live else ""))


def _cmd_run(args) -> int:
    cfg = Config.load()
    Dispatcher(cfg, routines_fn=routines.due,
               owner_fn=lambda: chat.owner_connected(cfg)).serve()
    return 0


def _cmd_status(args) -> int:
    cfg = Config.load()
    conn = connect(cfg)
    try:
        print(status_text(conn), end="")
    finally:
        conn.close()
    return 0


def dispatcher_idle(conn: sqlite3.Connection) -> bool:
    """True when no non-chat/live run is still open."""
    return conn.execute(
        "SELECT 1 FROM runs WHERE ended_at IS NULL"
        " AND COALESCE(lane,'') NOT IN ('chat','live') LIMIT 1"
    ).fetchone() is None


def _cmd_idle(_args) -> int:
    cfg = Config.load()
    conn = connect(cfg)
    try:
        return 0 if dispatcher_idle(conn) else 1
    finally:
        conn.close()


def register(subparsers) -> None:
    d = subparsers.add_parser("dispatcher", help="the always-on dispatcher")
    dsub = d.add_subparsers(dest="dispatcher_command", required=True)
    r = dsub.add_parser("run", help="run the dispatcher loop (systemd does this)")
    r.set_defaults(func=_cmd_run)
    idle = dsub.add_parser("idle", help="exit 0 when no bot run is active")
    idle.set_defaults(func=_cmd_idle)
    s = subparsers.add_parser("status", help="running bots, queue, pauses")
    s.set_defaults(func=_cmd_status)
