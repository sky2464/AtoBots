"""Engine-driven rotation: move the always-on Admin to a fresh session before
its context gets expensive.

A long session costs more on every turn. Once it is idle past the prompt
cache TTL, the next turn pays to rebuild the whole cache, so a big idle
session is rotated: the engine writes a summary from the database (no model
call) and restarts claude with it, through the same rollover path as before
(chat.rollover / rollover.md / the marker file). The file format is unchanged.

Two guards keep a rotation from hurting the Owner:
* a turn in progress is never interrupted (the transcript changed within
  BUSY_SECONDS means busy);
* a rotation waits while an Owner message is newer than Admin's last reply,
  so nothing that was just said is cut off (the summary also lists every
  request that is not yet handled).
"""
import os
import threading
import time
from datetime import datetime, timezone

from . import archive, chat, items, requests
from .config import Config
from .db import connect

BUSY_SECONDS = 20       # transcript written this recently: a turn is running
CHECK_SECONDS = 60      # how often a live session is looked at
OPEN_ITEMS_CAP = 40     # open items written into the summary
OWNER_MESSAGES = 5      # last Owner messages written into the summary
DECISIONS = 5           # last decisions written into the summary
TEXT_CAP = 300          # one Owner message, cut to this many characters


def should_rotate(tokens: int, idle_seconds: float, cfg: Config) -> bool:
    """True when the session is too big to keep: at the hard limit, or big
    enough and idle past the cache TTL (the cache is cold anyway)."""
    if tokens >= cfg.chat_rollover_tokens:
        return True
    return (tokens >= cfg.idle_rotate_tokens
            and idle_seconds > cfg.cache_ttl_seconds)


def idle_seconds(transcript_path) -> float:
    """Seconds since the session transcript was last written. A missing file
    reads as 0, so it never looks idle."""
    try:
        return max(0.0, time.time() - os.stat(transcript_path).st_mtime)
    except (OSError, TypeError):
        return 0.0


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
    for r in pending:
        out.append(_line(f"- R-{r['id']}: {r['text']}"))

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
    """True when an Owner message arrived after Admin last wrote anything."""
    row = conn.execute("SELECT MAX(received_at) AS at FROM owner_requests"
                       ).fetchone()
    if not row or not row["at"]:
        return False
    try:
        at = datetime.fromisoformat(row["at"])
        written = datetime.fromtimestamp(
            os.stat(transcript_path).st_mtime, timezone.utc)
    except (OSError, TypeError, ValueError):
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at > written


def check_once(cfg: Config, conn, transcript_path, token=None,
               kill=os.kill) -> bool:
    """One look at a live session. Requests a rollover and returns True when
    the session should be rotated and it is safe to do so now."""
    if transcript_path is None:
        return False
    idle = idle_seconds(transcript_path)
    if idle <= BUSY_SECONDS:
        return False  # a turn is in progress
    tokens = chat.context_tokens(transcript_path)
    if not should_rotate(tokens, idle, cfg):
        return False
    now_token = chat._read_token(cfg)
    if now_token is not None and now_token != token:
        return False  # a rollover is already on its way
    if _newer_owner_message(conn, transcript_path):
        return False  # Admin has not answered the Owner yet
    try:
        chat.rollover(cfg, conn, build_summary(cfg, conn), kill=kill)
    except ValueError:
        return False  # no claude process recorded yet: try again later
    return True


class Watch:
    """Looks at one live session every CHECK_SECONDS until stopped. It has
    its own database connection and never raises into the session."""

    def __init__(self, cfg: Config, session_id, token,
                 interval: float = CHECK_SECONDS, kill=os.kill):
        self.cfg = cfg
        self.session_id = session_id
        self.token = token
        self.interval = interval
        self.kill = kill
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="admin-rotation")

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
                                  kill=self.kill):
                        return  # rolled over; the session loop restarts
                except Exception:  # noqa: BLE001  # never break the session
                    pass
        finally:
            conn.close()
