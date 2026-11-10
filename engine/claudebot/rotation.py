"""Engine-driven session rotation for the Owner's chat and the always-on Admin.

A long session is expensive in two ways: every turn re-reads the whole
context, and after the prompt cache expires the next turn rewrites all of it.
So the engine restarts the session with a short summary when the context
passes `chat_rollover_tokens`, and also when it is merely large
(`idle_rotate_tokens`) and has been idle longer than the cache lifetime.

The summary is built from the database, with no model call. A turn in
progress is never interrupted.
"""
import json
import os
import time

from .config import Config
from .db import ct_display

CHECK_SECONDS = 60      # how often the wait loop looks
QUIET_SECONDS = 20      # the transcript must be this old before a restart
STALE_SECONDS = 600     # after this long with no write, no turn is running
_TAIL_BYTES = 256 * 1024
_TEXT_CAP = 600


def should_rotate(tokens: int, idle_seconds: float, cfg: Config) -> bool:
    if tokens >= cfg.chat_rollover_tokens:
        return True
    return (tokens >= cfg.idle_rotate_tokens
            and idle_seconds > cfg.cache_ttl_seconds)


def idle_seconds(transcript_path, now: float | None = None) -> float:
    """Seconds since the transcript was last written; 0 if it cannot be read
    (so an unreadable file never looks idle)."""
    try:
        mtime = os.stat(transcript_path).st_mtime
    except (OSError, TypeError):
        return 0.0
    return max(0.0, (time.time() if now is None else now) - mtime)


def turn_in_progress(transcript_path) -> bool:
    """True unless the last user or assistant entry is an assistant message
    that ended its turn. Unreadable or empty reads as in progress."""
    try:
        with open(transcript_path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - _TAIL_BYTES))
            lines = f.read().split(b"\n")
    except (OSError, TypeError):
        return True
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isSidechain") \
                or d.get("type") not in ("user", "assistant"):
            continue
        if d["type"] == "user":
            return True
        msg = d.get("message")
        stop = msg.get("stop_reason") if isinstance(msg, dict) else None
        return stop not in ("end_turn", "stop_sequence")
    return True


def safe_to_restart(transcript_path, now: float | None = None) -> bool:
    idle = idle_seconds(transcript_path, now)
    if idle < QUIET_SECONDS:
        return False
    return idle >= STALE_SECONDS or not turn_in_progress(transcript_path)


def _clip(text, cap: int = _TEXT_CAP) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[:cap - 1] + "…"


def build_summary(cfg: Config, conn, last_owner_messages: int = 5) -> str:
    """What a fresh session needs, from the records only: open items, the
    Owner's last messages, requests not yet handled and recent decisions."""
    from . import items, requests  # late: they import the CLI helpers
    out = ["This summary was built by the engine from the tracked records, "
           "not written by you. Anything said in the earlier session that is "
           "not in an item, a request or a decision is in the archive "
           "(`cb search`)."]
    rows = items.open_items(conn)
    out.append(f"\n## Open items ({len(rows)})")
    for r in rows[:40]:
        flag = " (waiting on the Owner)" if r["owner_waiting"] else ""
        out.append(f"- {r['id']} [{r['status']}] {r['owner']}: "
                   f"{_clip(r['title'], 160)}{flag}")
        if r["status"] == "paused" and r["note"]:
            out.append(f"  paused note: {_clip(r['note'], 300)}")
    if len(rows) > 40:
        out.append(f"- ... and {len(rows) - 40} more (cb open)")
    last = conn.execute(
        "SELECT * FROM owner_requests ORDER BY id DESC LIMIT ?",
        (max(1, int(last_owner_messages)),)).fetchall()
    out.append("\n## The Owner's last messages (oldest first)")
    for r in reversed(last):
        via = f" via {r['source']}" if r["source"] else ""
        out.append(f"- R-{r['id']} {ct_display(r['received_at'])}"
                   f"{via}: {_clip(r['text'])}")
    unhandled = requests.unhandled(conn)
    out.append(f"\n## Requests not yet linked to an item ({len(unhandled)})")
    for r in unhandled[:20]:
        out.append(f"- R-{r['id']}: {_clip(r['text'], 300)}")
    try:
        asked = conn.execute(
            "SELECT text FROM owner_questions WHERE answered_at IS NULL"
            " ORDER BY id").fetchall()
    except Exception:  # noqa: BLE001  # an older database without the table
        asked = []
    if asked:
        out.append("\n## Questions you asked that they have not answered")
        out += [f"- {_clip(r['text'], 300)}" for r in asked]
    dec = conn.execute(
        "SELECT id, title, quote FROM decisions ORDER BY n DESC LIMIT 5"
    ).fetchall()
    if dec:
        out.append("\n## Recent decisions")
        out += [f"- {d['id']} {_clip(d['title'], 160)}: "
                f"\"{_clip(d['quote'], 200)}\"" for d in reversed(dec)]
    return "\n".join(out)


class Watcher:
    """Called from the session wait loop about once a second. Looks at the
    transcript every CHECK_SECONDS and asks for a rollover when it is time.
    Any failure is swallowed: it must never break the Owner's session."""

    def __init__(self, cfg: Config, conn, session_id: str, clock=time.monotonic,
                 rollover=None, find=None):
        self.cfg, self.conn, self.session_id = cfg, conn, session_id
        self.clock = clock
        self._rollover, self._find = rollover, find
        self._next = clock() + CHECK_SECONDS
        self.done = False

    def tick(self) -> bool:
        if self.done or self.clock() < self._next:
            return False
        self._next = self.clock() + CHECK_SECONDS
        try:
            return self.check()
        except Exception:  # noqa: BLE001
            return False

    def check(self) -> bool:
        from . import activity, archive, chat  # late: chat imports this module
        find = self._find or archive.find_transcript
        path = find(self.cfg, self.session_id)
        if path is None:
            return False
        tokens = chat.context_tokens(path)
        idle = idle_seconds(path)
        if not should_rotate(tokens, idle, self.cfg) \
                or not safe_to_restart(path):
            return False
        why = ("context size" if tokens >= self.cfg.chat_rollover_tokens
               else "large and idle past the cache lifetime")
        (self._rollover or chat.rollover)(
            self.cfg, self.conn, build_summary(self.cfg, self.conn))
        activity.record(self.conn, "system", "chat.rotated",
                        f"{tokens} tokens, idle {int(idle)} s: {why}")
        self.done = True
        return True
