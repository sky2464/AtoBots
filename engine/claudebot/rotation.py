"""Engine-driven rotation: move a long Claude session to a fresh one before its
context gets expensive.

A long session costs more on every turn. Once it is idle past the prompt
cache TTL, the next turn pays to rebuild the whole cache, so a big idle
session is rotated: the engine writes a summary from the database (no model
call) and restarts claude with it, through the same rollover path as before
(chat.rollover / rollover.md / the marker file). The file format is unchanged.

It applies to the always-on Admin (live, 1 h cache) and to `cb chat` (5 min
cache, CHAT_CACHE_TTL).

Guards that keep a rotation from hurting the Owner:
* the transcript must end a finished turn (`turn_ended`): a long tool call
  writes nothing for minutes, so mtime alone cannot tell "busy" from "idle";
* the transcript must be quiet for BUSY_SECONDS;
* two consecutive checks CHECK_SECONDS apart must both qualify with the same
  transcript mtime before anything is rotated;
* an Owner message newer than Admin's last write blocks rotation (and when
  that cannot be checked, rotation is blocked too: fail closed).
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

from . import archive, chat, items, requests
from .config import Config
from .db import connect

BUSY_SECONDS = 20       # transcript written this recently: a turn is running
CHECK_SECONDS = 60      # how often a session is looked at
CHAT_CACHE_TTL = 300    # `cb chat` runs on the default 5 min prompt cache
OPEN_ITEMS_CAP = 40     # open items written into the summary
UNHANDLED_CAP = 10      # unhandled Owner requests written into the summary
OWNER_MESSAGES = 5      # last Owner messages written into the summary
DECISIONS = 5           # last decisions written into the summary
TEXT_CAP = 300          # one Owner message, cut to this many characters
TAIL_BYTES = 4 * 1024 * 1024  # how much of the transcript end is read


def should_rotate(tokens: int, idle_seconds: float, cfg: Config,
                  ttl: float | None = None) -> bool:
    """True when the session is too big to keep: at the hard limit, or big
    enough and idle past the cache TTL (the cache is cold anyway). `ttl`
    defaults to the live Admin's cache TTL."""
    if ttl is None:
        ttl = cfg.cache_ttl_seconds
    if tokens >= cfg.chat_rollover_tokens:
        return True
    return (tokens >= cfg.idle_rotate_tokens and idle_seconds > ttl)


def idle_seconds(transcript_path) -> float:
    """Seconds since the session transcript was last written. A missing file
    reads as 0, so it never looks idle."""
    try:
        return max(0.0, time.time() - os.stat(transcript_path).st_mtime)
    except (OSError, TypeError):
        return 0.0


def _last_turn_entry(transcript_path):
    """The last user or assistant entry (not a side chain) at the end of the
    transcript, or None. Garbage lines are skipped; nothing readable is None."""
    try:
        with open(transcript_path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read()
    except (OSError, ValueError, TypeError):
        return None
    lines = data.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]  # the first line may be cut off
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isSidechain"):
            continue
        if d.get("type") in ("assistant", "user"):
            return d
    return None


def turn_ended(transcript_path) -> bool:
    """True only when the last turn is finished: the last user or assistant
    entry is an assistant message with no tool_use block and stop_reason
    end_turn. Unreadable, empty or garbage reads as False."""
    d = _last_turn_entry(transcript_path)
    if d is None or d.get("type") != "assistant":
        return False
    msg = d.get("message")
    if not isinstance(msg, dict) or msg.get("stop_reason") != "end_turn":
        return False
    content = msg.get("content")
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in content):
        return False
    return True


def _line(text) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP - 1] + "…"


def build_summary(cfg: Config, conn, last_owner_messages: int = OWNER_MESSAGES
                  ) -> str:
    """The summary for the next session, from the database only."""
    out = ["# Summary of the earlier session (from the engine's records)"]

    rows = items.open_items(conn)
    out.append("\n## Open items")
    if not rows:
        out.append("- none")
    for r in rows[:OPEN_ITEMS_CAP]:
        out.append(_line(f"- {r['id']} [{r['status']}] {r['title']}"
                         f" (owner: {r['owner']})"))
    if len(rows) > OPEN_ITEMS_CAP:
        out.append(f"- ... and {len(rows) - OPEN_ITEMS_CAP} more (cb open)")

    recent = conn.execute(
        "SELECT * FROM owner_requests ORDER BY id DESC LIMIT ?",
        (last_owner_messages,)).fetchall()[::-1]
    out.append("\n## Owner's last messages")
    if not recent:
        out.append("- none")
    for r in recent:
        out.append(_line(f"- R-{r['id']} {r['received_at']}: {r['text']}"))

    pending = requests.unhandled(conn)
    out.append("\n## Requests not yet handled")
    if not pending:
        out.append("- none")
    for r in pending[:UNHANDLED_CAP]:
        out.append(_line(f"- R-{r['id']}: {r['text']}"))
    if len(pending) > UNHANDLED_CAP:
        out.append(f"- ... and {len(pending) - UNHANDLED_CAP} more "
                   "(cb request unhandled)")

    decided = conn.execute(
        "SELECT id, title, quote FROM decisions ORDER BY n DESC LIMIT ?",
        (DECISIONS,)).fetchall()[::-1]
    out.append("\n## Last decisions")
    if not decided:
        out.append("- none")
    for r in decided:
        out.append(_line(f"- {r['id']} {r['title']}"
                         + (f" (said: {r['quote']})" if r["quote"] else "")))
    return "\n".join(out) + "\n"


def _newer_owner_message(conn, transcript_path) -> bool:
    """True when an Owner message arrived after the transcript was last
    written. Fails closed: if this cannot be checked, a newer message is
    assumed (so nothing is rotated away)."""
    try:
        row = conn.execute("SELECT MAX(received_at) AS at FROM owner_requests"
                           ).fetchone()
        if not row or not row["at"]:
            return False
        at = datetime.fromisoformat(row["at"])
        written = datetime.fromtimestamp(
            os.stat(transcript_path).st_mtime, timezone.utc)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at > written
    except Exception:  # noqa: BLE001  # fail closed on any error
        return True


def _qualifying_mtime(cfg, conn, transcript_path, token, ttl):
    """The transcript mtime when this check qualifies for rotation, else None."""
    if transcript_path is None:
        return None
    try:
        mtime = os.stat(transcript_path).st_mtime
    except OSError:
        return None
    idle = time.time() - mtime
    if idle <= BUSY_SECONDS:
        return None  # a turn is in progress (or just ended)
    if not turn_ended(transcript_path):
        return None  # mid-turn: a tool call may be running
    tokens = chat.context_tokens(transcript_path)
    if not should_rotate(tokens, idle, cfg, ttl):
        return None
    now_token = chat._read_token(cfg)
    if now_token is not None and now_token != token:
        return None  # a rollover is already on its way
    if _newer_owner_message(conn, transcript_path):
        return None  # Owner wrote after the last write: not yet answered
    return mtime


def check_once(cfg: Config, conn, transcript_path, token=None,
               kill=os.kill, ttl: float | None = None,
               state: dict | None = None) -> bool:
    """One look at a session. A rotation needs two consecutive qualifying
    checks with the same transcript mtime; `state` (kept by the caller
    between checks, e.g. by Watch) remembers the first one. Returns True
    when a rollover was requested."""
    if state is None:
        state = {}
    mtime = _qualifying_mtime(cfg, conn, transcript_path, token, ttl)
    if mtime is None:
        state.pop("mtime", None)
        return False
    if state.get("mtime") != mtime:
        state["mtime"] = mtime  # first qualifying look: confirm next time
        return False
    state.pop("mtime", None)
    try:
        chat.rollover(cfg, conn, build_summary(cfg, conn), kill=kill)
    except ValueError:
        return False  # no claude process recorded yet: try again later
    return True


class Watch:
    """Looks at one session every CHECK_SECONDS until stopped. It has its own
    database connection and never raises into the session. `ttl` is the cache
    TTL for this kind of session (None: the live Admin's)."""

    def __init__(self, cfg: Config, session_id, token,
                 interval: float = CHECK_SECONDS, kill=os.kill,
                 ttl: float | None = None):
        self.cfg = cfg
        self.session_id = session_id
        self.token = token
        self.interval = interval
        self.kill = kill
        self.ttl = ttl
        self._state = {}
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="session-rotation")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        if self._thread.is_alive() and \
                self._thread is not threading.current_thread():
            self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        try:
            conn = connect(self.cfg)
        except Exception:  # noqa: BLE001  # rotation is optional
            return
        try:
            while not self._done.wait(self.interval):
                try:
                    path = archive.find_transcript(self.cfg, self.session_id)
                    if check_once(self.cfg, conn, path, self.token,
                                  kill=self.kill, ttl=self.ttl,
                                  state=self._state):
                        return  # rolled over; the session loop restarts
                except Exception:  # noqa: BLE001  # never break the session
                    self._state.pop("mtime", None)
        finally:
            conn.close()
