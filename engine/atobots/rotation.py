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
import sys
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


def _transcript_tail(transcript_path, last_n: int = 5) -> list[tuple[str, str]]:
    """Return the last `last_n` user and assistant messages from transcript."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    entries = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("isSidechain") \
                        or d.get("type") not in ("user", "assistant"):
                    continue
                msg = d.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict)
                                     and b.get("type") == "text" and isinstance(b.get("text"), str))
                if text:
                    role = "The Owner" if d["type"] == "user" else "Admin"
                    entries.append((role, _clip(text, _TEXT_CAP)))
    except (OSError, TypeError):
        return []
    return entries[-(last_n * 2):] if entries else []


def build_summary(cfg: Config, conn, last_owner_messages: int = 5, transcript_path=None) -> str:
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

    if transcript_path:
        tail = _transcript_tail(transcript_path, last_n=5)
        if tail:
            out.append("\n## Recent conversation tail")
            for speaker, text in tail:
                out.append(f"- **{speaker}**: {text}")
    return "\n".join(out)


class Watcher:
    """Called from the session wait loop about once a second. Looks at the
    transcript every CHECK_SECONDS and asks for a rollover when it is time.
    Any failure is swallowed: it must never break the Owner's session."""

    def __init__(self, cfg: Config, conn, session_id: str, clock=time.monotonic,
                 rollover=None, find=None, surface=None):
        self.cfg, self.conn, self.session_id = cfg, conn, session_id
        self.clock = clock
        self._rollover, self._find = rollover, find
        self._surface = surface
        self._next = clock() + CHECK_SECONDS
        self._handoff_prompted = False
        self._handoff_deadline = None
        self.done = False

    def tick(self) -> bool:
        if self.done:
            return False
        # If handoff was prompted, check if timeout expired or summary arrived
        if self._handoff_prompted:
            from . import chat
            summary, _ = chat._pending(self.cfg)
            if summary is not None:
                # Admin wrote their handoff!
                self.done = True
                return True
            if self.clock() >= self._handoff_deadline:
                # Timed out waiting for Admin handoff; perform rollover with fallback summary
                return self._do_fallback_rollover()
            return False

        if self.clock() < self._next:
            return False
        self._next = self.clock() + CHECK_SECONDS
        try:
            return self.check()
        except Exception as e:  # noqa: BLE001  # never stop the live session
            print(f"rotation: check failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return False

    def _do_fallback_rollover(self) -> bool:
        from . import activity, archive, chat
        find = self._find or archive.find_transcript
        path = find(self.cfg, self.session_id)
        summary = build_summary(self.cfg, self.conn, transcript_path=path)
        (self._rollover or chat.rollover)(self.cfg, self.conn, summary)
        activity.record(self.conn, "system", "chat.rotated",
                        "rollover handoff timed out; rotated with transcript tail")
        self.done = True
        return True

    def check(self) -> bool:
        from . import activity, admin_live, archive, chat  # late: chat imports this module
        find = self._find or archive.find_transcript
        path = find(self.cfg, self.session_id)
        if path is None:
            return False
        tokens = chat.context_tokens(path)
        idle = idle_seconds(path)
        if not should_rotate(tokens, idle, self.cfg) \
                or not safe_to_restart(path):
            return False

        # When session passes rollover threshold and is safe to restart:
        # Prompt Admin to write handoff and wait up to rollover_handoff_seconds.
        surface_fn = self._surface or admin_live.surface
        try:
            surface_fn(self.cfg, "This session is long. Write your handoff now with cb chat-rollover.",
                       conn=self.conn)
        except Exception:
            pass
        self._handoff_prompted = True
        self._handoff_deadline = self.clock() + getattr(self.cfg, "rollover_handoff_seconds", 180)
        return False
