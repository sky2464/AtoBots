"""Owner requests: stored word for word the moment they are typed, before
anything else happens. A request stays open until every item linked to it is
done or parked, so interrupted requests are not lost."""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from . import activity
from .config import Config
from .db import connect, ct_display, utcnow
from .items import _DONE_SQL, _run


def capture(conn: sqlite3.Connection, text: str, session_id: str) -> int:
    cur = conn.execute(
        "INSERT INTO owner_requests(text, session_id, received_at) VALUES (?,?,?)",
        (text, session_id, utcnow()))
    return cur.lastrowid


def _request_number(value) -> int:
    """2, "2" or "R-2" (any case) all mean request 2."""
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*(?:R-?)?(\d+)\s*", str(value), re.IGNORECASE)
    if not m:
        raise ValueError(f"not a request number: {value!r}")
    return int(m.group(1))


def link(conn: sqlite3.Connection, request_id: int, item_ids: list) -> None:
    request_id = _request_number(request_id)
    if conn.execute("SELECT 1 FROM owner_requests WHERE id=?",
                    (request_id,)).fetchone() is None:
        raise KeyError(request_id)
    for i in item_ids:
        if conn.execute("SELECT 1 FROM items WHERE id=?", (i,)).fetchone() is None:
            raise KeyError(i)
    conn.executemany(
        "INSERT OR IGNORE INTO request_items(request_id, item_id) VALUES (?,?)",
        [(request_id, i) for i in item_ids])


def unhandled(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT * FROM owner_requests r WHERE NOT EXISTS (SELECT 1 FROM"
        " request_items l WHERE l.request_id=r.id) ORDER BY id").fetchall()


def _open_requests(conn) -> list:
    return conn.execute(
        "SELECT * FROM owner_requests r WHERE NOT EXISTS (SELECT 1 FROM"
        " request_items l WHERE l.request_id=r.id) OR EXISTS (SELECT 1 FROM"
        " request_items l JOIN items i ON i.id=l.item_id WHERE l.request_id=r.id"
        f" AND i.status NOT IN {_DONE_SQL}) ORDER BY r.id").fetchall()


def startup_brief(conn: sqlite3.Connection, limit: int = 20) -> str:
    """Open requests, oldest first, verbatim, with their items and paused notes."""
    rows = _open_requests(conn)
    if not rows:
        return "No open Owner requests."
    out = [f"Open Owner requests ({len(rows)}):"]
    for r in rows[:limit]:
        out.append(f"\n[R-{r['id']}] {ct_display(r['received_at'])}\n{r['text']}")
        its = conn.execute(
            "SELECT i.* FROM request_items l JOIN items i ON i.id=l.item_id"
            " WHERE l.request_id=? ORDER BY i.n", (r["id"],)).fetchall()
        if not its:
            out.append("  -> not handled yet (no item)")
        for i in its:
            out.append(f"  -> {i['id']} [{i['status']}] {i['owner']}: {i['title']}")
            if i["status"] == "paused" and i["note"]:
                out.append(f"     paused note: {i['note']}")
    if len(rows) > limit:
        out.append(f"\n... and {len(rows) - limit} more open requests "
                   f"(cb request unhandled, cb open)")
    return "\n".join(out)


_CHANNEL = re.compile(r'<channel source="([^"]*)"[^>]*>\n(.*?)\n</channel>',
                      re.DOTALL)


def channel_messages(prompt: str):
    """[(source, text)] when the prompt is made only of channel messages (what
    Claude Code hands the hook for a Telegram message), else None. The text is
    the sender's words without the <channel> wrapper."""
    found = _CHANNEL.findall(prompt)
    if not found or _CHANNEL.sub("", prompt).strip():
        return None
    return found


def _caller() -> str:
    """Who ran the hook, so an input that Claude Code could not have sent
    (like `{}`) can be traced. Linux only; empty when unknown."""
    try:
        raw = Path(f"/proc/{os.getppid()}/cmdline").read_bytes()
        return " [parent: " + raw.replace(b"\0", b" ").decode(
            "utf-8", "replace").strip()[:200] + "]"
    except OSError:
        return ""


def _hook_failed(raw: bytes, reason: str) -> None:
    """Record the raw hook input so nothing the Owner typed is lost."""
    text = raw.decode("utf-8", errors="replace")
    try:
        cfg = Config.load()
        conn = connect(cfg)
        try:
            activity.record(conn, "hook", "capture.failed",
                            f"{reason}: {text}{_caller()}")
        finally:
            conn.close()
        return
    except Exception:
        pass
    try:
        home = os.environ.get("CLAUDEBOT_HOME")
        if home:
            with open(Path(home) / "capture-failed.log", "ab") as f:
                f.write(raw + b"\n")
    except Exception:
        pass


def _skip_command(conn, text: str) -> bool:
    """A prompt that starts with `/` is a slash command, not a request. Only
    the command name is recorded: its arguments can hold secrets."""
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return False
    name = stripped.split(None, 1)[0]
    activity.record(conn, "hook", "request.skipped_command", name)
    return True


def _cmd_hook_prompt(args) -> int:
    """UserPromptSubmit hook. No output, no AI cost, always exits 0."""
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
        data = json.loads(raw.decode("utf-8"))
        text = data["prompt"]
        if not isinstance(text, str):
            raise TypeError("prompt is not text")
        session = data.get("session_id") or ""
        conn = connect(Config.load())
        try:
            wrapped = channel_messages(text)
            if wrapped is None and _skip_command(conn, text):
                return 0
            if wrapped is None:
                capture(conn, text, str(session))
            for source, inner in wrapped or ():
                rid = capture(conn, inner, str(session))
                activity.record(conn, "hook", "request.channel",
                                f"request arrived through {source}", str(rid))
        finally:
            conn.close()
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            return 0
        _hook_failed(raw, type(e).__name__)
    return 0


def _cmd_hook_stop(args) -> int:
    from . import chat  # imported late: chat itself imports this module
    return chat.cmd_hook_stop(args)


def _cmd_link(args):
    def go(cfg, conn):
        link(conn, args.request_id, args.items)
        return 0
    return _run(go)


def _cmd_unhandled(args):
    def go(cfg, conn):
        for r in unhandled(conn):
            print(f"R-{r['id']}\t{ct_display(r['received_at'])}\t{r['text']}")
        return 0
    return _run(go)


def _cmd_brief(args):
    def go(cfg, conn):
        print(startup_brief(conn, args.limit))
        return 0
    return _run(go)


def register(subparsers) -> None:
    sp = subparsers.add_parser("request", help="Owner requests")
    sub = sp.add_subparsers(dest="request_cmd", required=True)
    ln = sub.add_parser("link")
    ln.add_argument("request_id", help="2 or R-2")
    ln.add_argument("items", nargs="+")
    ln.set_defaults(func=_cmd_link)
    un = sub.add_parser("unhandled")
    un.set_defaults(func=_cmd_unhandled)
    br = sub.add_parser("brief")
    br.add_argument("--limit", type=int, default=20)
    br.set_defaults(func=_cmd_brief)
    h = subparsers.add_parser("hook", help="Claude Code hooks")
    hs = h.add_subparsers(dest="hook_cmd", required=True)
    hp = hs.add_parser("prompt")
    hp.set_defaults(func=_cmd_hook_prompt)
    hst = hs.add_parser("stop")
    hst.set_defaults(func=_cmd_hook_stop)
