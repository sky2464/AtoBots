"""Owner requests: stored word for word the moment they are typed, before
anything else happens. A request stays open until every item linked to it is
done or parked, so interrupted requests are not lost."""
import json
import os
import re
import sqlite3
import string
import sys
from pathlib import Path

from . import activity
from .config import Config
from .db import connect, ct_display, utcnow
from .items import _DONE_SQL, _run


def capture(conn: sqlite3.Connection, text: str, session_id: str,
            source: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO owner_requests(text, session_id, received_at, source)"
        " VALUES (?,?,?,?)", (text, session_id, utcnow(), source))
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
        via = f" (via {r['source']})" if r["source"] else ""
        out.append(f"\n[R-{r['id']}] {ct_display(r['received_at'])}{via}"
                   f"\n{r['text']}")
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


_CHANNEL = re.compile(r'<channel ([^>]*)>\n(.*?)\n</channel>', re.DOTALL)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def source_label(attrs: dict) -> str:
    """Where a channel message came from, e.g. `telegram user=123 chat=123`."""
    kind = (attrs.get("source") or "channel").split(":")[-1] or "channel"
    def clean(v):
        return re.sub(r"[^\w@.-]", "", v or "")[:40] or "?"
    user = clean(attrs.get("user_id") or attrs.get("user"))
    chat = clean(attrs.get("chat_id"))
    kind = clean(kind)
    return f"{kind} user={user} chat={chat}"


def channel_entries(prompt: str):
    """[(attrs, source label, text)] for a prompt made only of channel
    messages, else None."""
    found = _CHANNEL.findall(prompt)
    if not found or _CHANNEL.sub("", prompt).strip():
        return None
    out = []
    for a, t in found:
        attrs = dict(_ATTR.findall(a))
        out.append((attrs, source_label(attrs), t))
    return out


def channel_messages(prompt: str):
    """[(source label, text)] when the prompt is made only of channel messages
    (what Claude Code hands the hook for a Telegram message), else None. The
    text is the sender's words without the <channel> wrapper."""
    entries = channel_entries(prompt)
    if entries is None:
        return None
    return [(source, t) for _, source, t in entries]


def _telegram_source(attrs: dict) -> bool:
    return "telegram" in (attrs.get("source") or "").lower()


def _sender_allowed(attrs: dict, allowed: set[str]) -> bool:
    """Telegram senders must be in access.json allowFrom; other channels pass."""
    if not _telegram_source(attrs):
        return True
    if not allowed:
        return False
    uid = str(attrs.get("user_id") or attrs.get("user") or "").strip()
    if not uid:
        return False
    if uid in allowed:
        return True
    if uid.isdigit():
        return str(int(uid)) in allowed
    return False


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


_COMMAND = re.compile(r"/[A-Za-z][\w:-]*")
_SETUP_COMMANDS = ("/telegram:access", "/telegram:configure")


def _skip_command(conn, text: str) -> bool:
    """A bare slash command (`/clear`) is not a request, and neither is
    `/telegram:access ...` or `/telegram:configure ...` (channel setup, which
    can carry pairing codes). A path like /home/x, or `/skill do this`, is a
    real request. Only the command name is recorded, never its arguments."""
    parts = text.strip().split(None, 1)
    if not parts or not _COMMAND.fullmatch(parts[0]):
        return False
    name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    if rest and name not in _SETUP_COMMANDS:
        return False
    activity.record(conn, "hook", "request.skipped_command", name[:40])
    return True


# Bare acknowledgements: words and emoji that answer nothing on their own.
_ACK_WORDS = ("yes", "y", "ok", "okay", "k", "thanks", "thx",
              "\U0001F44D", "\U0001F44C", "\U0001F64F")  # thumbs up, ok hand, pray
_ACK_SEP = "[\\s" + re.escape(string.punctuation + "\u2026\u201c\u201d\u2018\u2019\u2013\u2014") + "]*"
_ACK = re.compile(
    "^" + _ACK_SEP + "(?:" + "|".join(
        re.escape(w) for w in sorted(_ACK_WORDS, key=len, reverse=True))
    + ")" + _ACK_SEP + "(?:(?:" + "|".join(
        re.escape(w) for w in sorted(_ACK_WORDS, key=len, reverse=True))
    + ")" + _ACK_SEP + ")*$", re.IGNORECASE)


def is_bare_ack(text: str) -> bool:
    """True for a message made only of acknowledgements (yes, ok, thanks,
    thumbs up, with or without punctuation), e.g. `Yes!` or `ok 👍`. False
    for anything with other words, e.g. `yes please publish it`."""
    if not isinstance(text, str):
        return False
    cleaned = text.replace("\ufe0f", "").strip()
    return bool(cleaned) and _ACK.fullmatch(cleaned) is not None


def _ack_ignored(conn, text: str, wrapped) -> bool:
    """True when the whole message is a bare acknowledgement and no Owner
    question is open, so it answers nothing and is refused. Any error gives
    False: a broken check must never block a real message."""
    try:
        texts = [t for _, t in wrapped] if wrapped is not None else [text]
        if not all(is_bare_ack(t) for t in texts):
            return False
        return conn.execute(
            "SELECT 1 FROM owner_questions WHERE answered_at IS NULL"
            " LIMIT 1").fetchone() is None
    except Exception:
        return False


def _record_ack(conn, text: str, wrapped) -> None:
    """Log one row for a refused acknowledgement. Any failure is swallowed:
    the block must happen even when the activity row cannot be written."""
    try:
        ack = " ".join(t for _, t in wrapped) if wrapped is not None else text
        activity.record(conn, "hook", "request.ack_ignored", ack[:40])
    except Exception:
        pass


def _close_questions(conn) -> None:
    """A saved message is the Owner's answer: close every open question."""
    try:
        conn.execute("UPDATE owner_questions SET answered_at=? WHERE"
                     " answered_at IS NULL", (utcnow(),))
    except Exception:
        pass


def _cmd_hook_prompt(args) -> int:
    """UserPromptSubmit hook. No AI cost. Prints a block decision only for a
    bare acknowledgement with no open question; otherwise always exits 0."""
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
            entries = channel_entries(text)
            wrapped = None if entries is None else [(s, t) for _, s, t in entries]
            if entries is None and _skip_command(conn, text):
                return 0
            if _ack_ignored(conn, text, wrapped):
                _record_ack(conn, text, wrapped)
                print(json.dumps({"decision": "block", "reason":
                                  "Acknowledgement ignored (nothing was asked)."}))
                return 0
            from . import admin_live
            allowed = {str(x) for x in admin_live.access_allow_from()}
            saved = False
            if entries is None:
                capture(conn, text, str(session))
                saved = True
            else:
                for attrs, source, inner in entries:
                    if _skip_command(conn, inner):
                        continue
                    if not _sender_allowed(attrs, allowed):
                        activity.record(conn, "hook", "request.telegram_denied",
                                        source[:60])
                        continue
                    rid = capture(conn, inner, str(session), source)
                    activity.record(conn, "hook", "request.channel",
                                    f"request arrived through {source}", str(rid))
                    saved = True
            if saved:
                _close_questions(conn)
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
            via = f"(via {r['source']}) " if r["source"] else ""
            print(f"R-{r['id']}\t{ct_display(r['received_at'])}\t{via}{r['text']}")
        return 0
    return _run(go)


def _cmd_question_open(args):
    def go(cfg, conn):
        text = args.text.strip()
        if not text:
            raise ValueError("the question is empty")
        cur = conn.execute("INSERT INTO owner_questions(asked_at, text)"
                           " VALUES (?,?)", (utcnow(), text))
        print(f"Q-{cur.lastrowid} open")
        return 0
    return _run(go)


def _cmd_question_close(args):
    def go(cfg, conn):
        cur = conn.execute("UPDATE owner_questions SET answered_at=? WHERE"
                           " answered_at IS NULL", (utcnow(),))
        print(f"closed {cur.rowcount} open question(s)")
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
    qn = subparsers.add_parser("question", help="Questions open to the Owner")
    qsub = qn.add_subparsers(dest="question_cmd", required=True)
    qo = qsub.add_parser("open", help="Register a question you asked the Owner")
    qo.add_argument("text")
    qo.set_defaults(func=_cmd_question_open)
    qc = qsub.add_parser("close", help="Mark every open question answered")
    qc.set_defaults(func=_cmd_question_close)
    h = subparsers.add_parser("hook", help="Claude Code hooks")
    hs = h.add_subparsers(dest="hook_cmd", required=True)
    hp = hs.add_parser("prompt")
    hp.set_defaults(func=_cmd_hook_prompt)
    hst = hs.add_parser("stop")
    hst.set_defaults(func=_cmd_hook_stop)
