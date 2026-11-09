"""Routines: a prompt sent to a bot on a 5-field cron schedule (America/Chicago).

A routine may carry a check command. It exits 0 when there is work and 1 when
there is none; anything else is a failed check, which is recorded and skipped.
A routine with nothing to do sends no message, so no session starts and no
usage is spent.

Checks run in short-lived threads, so a slow check never holds up the
dispatcher's tick. `due` waits a little for them, then leaves stragglers to
finish on their own (each has a hard time limit).
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import activity, bots, messages
from .config import Config
from .db import connect, ct_display, tx, utcnow

CHECK_TIMEOUT = 60  # seconds, per check
CT = "America/Chicago"
_NEVER_STOP = 10 ** 9  # routines are not part of any bot-to-bot loop
_UTC = timezone.utc

_lock = threading.Lock()
_inflight: set[int] = set()
_threads: list[threading.Thread] = []


# ---- cron -----------------------------------------------------------------

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31),
           ("month", 1, 12), ("day of week", 0, 7))


def _parse_field(text: str, name: str, lo: int, hi: int) -> tuple[set, bool]:
    """Returns (allowed values, starts with `*`)."""
    values: set[int] = set()
    for part in text.split(","):
        rng, _, step_s = part.partition("/")
        try:
            step = int(step_s) if step_s else 1
            if step < 1 or (step_s and not step_s.isdigit()):
                raise ValueError
            if rng == "*":
                a, b = lo, hi
            elif "-" in rng:
                x, y = rng.split("-")
                a, b = int(x), int(y)
            else:
                a = int(rng)
                b = hi if step_s else a
            if not (lo <= a <= hi and lo <= b <= hi and a <= b) or not all(
                    s.isdigit() for s in rng.replace("-", " ").split()
                    if rng != "*"):
                raise ValueError
        except ValueError:
            raise ValueError(f"bad cron {name} field: {text!r}") from None
        values.update(range(a, b + 1, step))
    return values, text.startswith("*")


def parse_cron(cron: str):
    if not isinstance(cron, str):
        raise ValueError("cron must be text")
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError("cron needs 5 fields: minute hour day-of-month "
                         f"month day-of-week (got {cron!r})")
    f = [_parse_field(p, n, lo, hi) for p, (n, lo, hi) in zip(parts, _FIELDS)]
    dow = {0 if v == 7 else v for v in f[4][0]}
    return f[0][0], f[1][0], f[2][0], f[3][0], dow, f[2][1], f[4][1]


def _day_ok(d, dom, month, dow, dom_star, dow_star) -> bool:
    if d.month not in month:
        return False
    d_ok = d.day in dom
    w_ok = (d.weekday() + 1) % 7 in dow
    if dom_star or dow_star:
        return d_ok and w_ok
    return d_ok or w_ok  # both restricted: either one matches (as cron does)


def _naive_matches(parsed, start):
    """Yield naive local datetimes matching the cron, from `start` on."""
    minute, hour, dom, month, dow, dom_star, dow_star = parsed
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(366 * 9):  # reaches the next Feb 29
        if _day_ok(day, dom, month, dow, dom_star, dow_star):
            for h in sorted(hour):
                for m in sorted(minute):
                    t = day.replace(hour=h, minute=m)
                    if t >= start:
                        yield t
        day += timedelta(days=1)


def _to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """A wall-clock time that does not exist (spring forward) becomes the
    first valid minute after the gap; a repeated one (fall back) takes its
    first occurrence."""
    for _ in range(24 * 60):
        aware = naive.replace(tzinfo=tz)
        utc = aware.astimezone(_UTC)
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            return utc
        naive += timedelta(minutes=1)
    raise ValueError("no valid time")  # unreachable for real zones


def next_time(cron: str, after_iso: str, tz: str = CT) -> str:
    """The first run strictly after `after_iso`, as UTC ISO 8601."""
    parsed = parse_cron(cron)
    zone = ZoneInfo(tz)
    after = datetime.fromisoformat(after_iso)
    if after.tzinfo is None:
        after = after.replace(tzinfo=_UTC)
    after = after.astimezone(_UTC)
    start = (after.astimezone(zone).replace(tzinfo=None, second=0,
                                            microsecond=0)
             + timedelta(minutes=1))
    for naive in _naive_matches(parsed, start):
        utc = _to_utc(naive, zone)
        if utc > after:
            return utc.isoformat(timespec="seconds")
    raise ValueError(f"cron {cron!r} never fires")


# ---- storage --------------------------------------------------------------

def add(conn, bot: str, cron: str, prompt: str, check_cmd: str | None = None,
        level: str = "normal", actor: str = "admin") -> int:
    bots.get(conn, bot)
    if level not in messages.LEVELS:
        raise ValueError(f"level must be one of: {', '.join(messages.LEVELS)}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("a routine needs a prompt")
    messages._check_body(prompt)
    nxt = next_time(cron, utcnow())  # also validates the cron
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO routines(bot, cron, check_cmd, prompt, level, enabled,"
            " next_due, created_by) VALUES (?,?,?,?,?,1,?,?)",
            (bot, cron, check_cmd or None, prompt, level, nxt, actor))
        activity.record(conn, actor, "routine.add",
                        f"{bot}: {cron}: {prompt[:80]}", str(cur.lastrowid))
    return cur.lastrowid


def _get(conn, rid: int):
    r = conn.execute("SELECT * FROM routines WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise KeyError(f"routine {rid}")
    return r


def set_enabled(conn, rid: int, enabled: bool, actor: str = "admin") -> None:
    r = _get(conn, rid)
    with tx(conn):
        if enabled:
            # Start fresh: periods missed while it was off are not caught up.
            conn.execute("UPDATE routines SET enabled=1, next_due=? WHERE id=?",
                         (next_time(r["cron"], utcnow()), rid))
        else:
            conn.execute("UPDATE routines SET enabled=0 WHERE id=?", (rid,))
        activity.record(conn, actor, "routine.on" if enabled else "routine.off",
                        f"{r['bot']}: {r['cron']}", str(rid))


def remove(conn, rid: int, actor: str = "admin") -> None:
    r = _get(conn, rid)
    with tx(conn):
        conn.execute("DELETE FROM routines WHERE id=?", (rid,))
        activity.record(conn, actor, "routine.rm",
                        f"{r['bot']}: {r['cron']}: {r['prompt'][:80]}", str(rid))


# ---- firing ---------------------------------------------------------------

def _run_check(cmd: str) -> tuple[bool, str]:
    """Returns (work to do, problem). Exit 0 is work, 1 is nothing, anything
    else (or a timeout) is a problem."""
    try:
        p = subprocess.Popen(["/bin/sh", "-c", cmd], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        return False, f"could not start: {e}"
    try:
        _, err = p.communicate(timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return False, f"timed out after {CHECK_TIMEOUT:g}s"
    if p.returncode == 0:
        return True, ""
    if p.returncode == 1:
        return False, ""
    tail = err.decode("utf-8", "replace").strip()[-200:]
    return False, f"exit {p.returncode}" + (f": {tail}" if tail else "")


def _fire(cfg: Config, rid: int, bot: str, check_cmd, prompt: str, level: str,
          due_iso: str, now: str, sent: list) -> None:
    """Runs in its own thread with its own connection."""
    conn = connect(cfg)
    try:
        if check_cmd:
            work, problem = _run_check(check_cmd)
            if problem:
                activity.record(conn, "system", "routine.check_failed",
                                f"routine {rid} ({bot}): {problem}", str(rid))
            if not work:
                return
        try:
            messages.send(conn, "system", bot, f"Routine {rid}", prompt,
                          kind="wake", level=level,
                          dedupe_key=f"routine:{rid}:{due_iso}",
                          hop_limit=_NEVER_STOP, now=now)
        except (KeyError, ValueError) as e:
            activity.record(conn, "system", "routine.failed",
                            f"routine {rid} ({bot}): {e}", str(rid))
            return
        sent.append(rid)
    except Exception as e:  # noqa: BLE001
        try:
            activity.record(conn, "system", "routine.failed",
                            f"routine {rid}: {type(e).__name__}: {e}", str(rid))
        except Exception:  # noqa: BLE001
            print(f"routines: routine {rid}: {e}", file=sys.stderr)
    finally:
        conn.close()
        with _lock:
            _inflight.discard(rid)


def due(cfg: Config, conn, now: str, wait: float = 2.0) -> list[int]:
    """Fire every enabled routine whose time has come, once however many
    periods were missed. Each routine's next time is stored before its check
    starts. Returns the routines whose message was queued within `wait`
    seconds; a slower check finishes in the background."""
    now_dt = datetime.fromisoformat(now)
    rows = [r for r in conn.execute("SELECT * FROM routines WHERE enabled=1"
                                    " ORDER BY id")
            if r["next_due"] and datetime.fromisoformat(r["next_due"]) <= now_dt]
    sent: list[int] = []
    started: list[threading.Thread] = []
    for r in rows:
        with _lock:
            if r["id"] in _inflight:
                continue
        try:
            nxt = next_time(r["cron"], now)
        except ValueError as e:
            with tx(conn):
                conn.execute("UPDATE routines SET enabled=0 WHERE id=?",
                             (r["id"],))
                activity.record(conn, "system", "routine.failed",
                                f"routine {r['id']} switched off: {e}",
                                str(r["id"]))
            continue
        with tx(conn):
            conn.execute("UPDATE routines SET next_due=? WHERE id=?",
                         (nxt, r["id"]))
        t = threading.Thread(
            target=_fire, name=f"routine-{r['id']}", daemon=True,
            args=(cfg, r["id"], r["bot"], r["check_cmd"], r["prompt"],
                  r["level"], r["next_due"], now, sent))
        with _lock:
            _inflight.add(r["id"])
            _threads.append(t)
        t.start()
        started.append(t)
    deadline = time.monotonic() + wait
    for t in started:
        t.join(max(0.0, deadline - time.monotonic()))
    with _lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
    return sorted(sent)


def wait_idle() -> None:
    """Join every check still running (tests and shutdown)."""
    with _lock:
        ts = list(_threads)
    for t in ts:
        t.join()


# ---- CLI ------------------------------------------------------------------

def _actor() -> str:
    return os.environ.get("CB_BOT", "cli")


def _run(fn):
    conn = connect(Config.load())
    try:
        return fn(conn)
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_add(args) -> int:
    def go(conn):
        rid = add(conn, args.bot, args.cron, args.prompt, args.check,
                  args.level, _actor())
        print(f"routine {rid} added; first run "
              f"{ct_display(_get(conn, rid)['next_due'])}")
        return 0
    return _run(go)


def _cmd_list(args) -> int:
    def go(conn):
        for r in conn.execute("SELECT * FROM routines ORDER BY id"):
            print(f"{r['id']}\t{r['bot']}\t{r['cron']}\t"
                  f"{'on' if r['enabled'] else 'off'}\t{r['level']}\t"
                  f"next {ct_display(r['next_due'])}\t{r['prompt']}"
                  + (f"\tcheck: {r['check_cmd']}" if r["check_cmd"] else ""))
        return 0
    return _run(go)


def _toggle(enabled: bool):
    def cmd(args) -> int:
        return _run(lambda conn: set_enabled(conn, args.id, enabled, _actor())
                    or 0)
    return cmd


def _cmd_rm(args) -> int:
    return _run(lambda conn: remove(conn, args.id, _actor()) or 0)


def register(subparsers) -> None:
    sp = subparsers.add_parser("routine", help="scheduled prompts")
    sub = sp.add_subparsers(dest="routine_cmd", required=True)
    a = sub.add_parser("add", help="add a routine (cron is in Central Time)")
    a.add_argument("bot")
    a.add_argument("cron", help='5 fields, e.g. "0 7 * * *"')
    a.add_argument("prompt")
    a.add_argument("--check", help="shell command: exit 0 means work to do, "
                   "anything else means nothing")
    a.add_argument("--level", default="normal", choices=messages.LEVELS)
    a.set_defaults(func=_cmd_add)
    sub.add_parser("list").set_defaults(func=_cmd_list)
    for name, flag in (("on", True), ("off", False)):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        p.set_defaults(func=_toggle(flag))
    r = sub.add_parser("rm")
    r.add_argument("id", type=int)
    r.set_defaults(func=_cmd_rm)
