"""Owner requests: stored word for word the moment they are typed, before
anything else happens. A request stays open until every item linked to it is
done or parked, so interrupted requests are not lost."""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from . import activity, admin_live
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


def close_request(conn: sqlite3.Connection, request_id: int, reason: str,
                  actor: str = "cli") -> None:
    request_id = _request_number(request_id)
    r = conn.execute("SELECT * FROM owner_requests WHERE id=?",
                     (request_id,)).fetchone()
    if r is None:
        raise KeyError(request_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required")
    conn.execute(
        "INSERT OR IGNORE INTO request_items(request_id, item_id) VALUES (?,?)",
        (request_id, "R-CLOSED"))
    activity.record(conn, actor, "request.closed",
                    f"R-{request_id}: {reason.strip()}", f"R-{request_id}")


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


_ACK_WORDS = {"yes", "y", "yep", "yeah", "ok", "okay", "k", "kk", "thanks",
              "thx", "ty", "great", "cool", "nice", "perfect", "good",
              "got", "it", "sure", "fine", "done", "thank", "you"}
_ACK_EMOJI = "\U0001F44D\U0001F44C\U0001F64F\u2705\u2764\U0001F60A\U0001F642"
_ACK_STRIP = re.compile(r"[\s.,!~\-\ufe0f\u200d\U0001F3FB-\U0001F3FF]+")
ACK_REASON = "Acknowledgement ignored (nothing was asked)."
DENY_SENDER_REASON = ("Message from a sender that is not on the allow list.")


def is_bare_ack(text) -> bool:
    """True for a message that only acknowledges: yes, ok, thanks, a thumbs
    up, and combinations of those. Anything with another word is a request."""
    if not isinstance(text, str):
        return False
    rest = "".join(c for c in text.lower() if c not in _ACK_EMOJI)
    had_emoji = len(rest) != len(text)
    words = [w for w in _ACK_STRIP.split(rest) if w]
    if not words:
        return had_emoji
    return len(words) <= 4 and all(w in _ACK_WORDS for w in words)


def last_reply_asked(transcript_path) -> bool:
    """True when Admin's last message in the session holds a question mark,
    or when that cannot be told. So an answer to a question Admin forgot to
    register with `cb question open` is never dropped."""
    try:
        with open(transcript_path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - 512 * 1024))
            lines = f.read().split(b"\n")
    except (OSError, TypeError):
        return True
    for line in reversed(lines):
        try:
            d = json.loads(line)
            if d.get("isSidechain") or d.get("type") != "assistant":
                continue
            content = d["message"]["content"]
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if isinstance(content, str):
            texts = [content]
        else:
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
        if any(t.strip() for t in texts):
            return any("?" in t for t in texts)
    return False  # Admin has said nothing yet, so nothing was asked


def open_question(conn: sqlite3.Connection, text: str) -> int:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the question is empty")
    return conn.execute(
        "INSERT INTO owner_questions(asked_at, text) VALUES (?,?)",
        (utcnow(), text.strip())).lastrowid


def has_open_question(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM owner_questions WHERE answered_at"
                        " IS NULL").fetchone() is not None


def close_questions(conn: sqlite3.Connection) -> int:
    return conn.execute("UPDATE owner_questions SET answered_at=? WHERE"
                        " answered_at IS NULL", (utcnow(),)).rowcount


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


def _telegram_sender_allowed(attrs: dict) -> bool:
    """True when the channel user id is on the Telegram allowFrom list."""
    allow = {str(x) for x in admin_live.access_allow_from()}
    if not allow:
        return False
    uid = attrs.get("user_id") or attrs.get("user") or ""
    return str(uid) in allow


def channel_messages(prompt: str):
    """[(source label, text)] when the prompt is made only of channel messages
    (what Claude Code hands the hook for a Telegram message), else None. The
    text is the sender's words without the <channel> wrapper."""
    found = _CHANNEL.findall(prompt)
    if not found or _CHANNEL.sub("", prompt).strip():
        return None
    return [(source_label(dict(_ATTR.findall(a))), t, dict(_ATTR.findall(a)))
            for a, t in found]


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


def _open_owner_session(conn, session: str) -> bool:
    """True when `session` is an open chat or live run."""
    if not session:
        return False
    return conn.execute(
        "SELECT 1 FROM runs WHERE session_id=? AND ended_at IS NULL"
        " AND lane IN ('chat','live')", (session,)).fetchone() is not None


def _peek_dir():
    """The always-on Admin's state folder, or None in any other session."""
    return os.environ.get("CB_STATE_DIR") or None


def _cmd_hook_stop(_args) -> int:
    """Stop hook of the always-on Admin: the turn ended, so the status peek
    in Telegram goes away. Silent, always exits 0, and does nothing in any
    other session."""
    try:
        sys.stdin.buffer.read()
        state = _peek_dir()
        if state:
            from . import peek
            peek.note_turn_ended(state)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _cmd_hook_prompt(args) -> int:
    """UserPromptSubmit hook. No AI cost, always exits 0. Silent, except that
    a bare acknowledgement ("ok", a thumbs up) while no question is open is
    blocked: it would cost a full turn and asks for nothing."""
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
        data = json.loads(raw.decode("utf-8"))
        text = data["prompt"]
        if not isinstance(text, str):
            raise TypeError("prompt is not text")
        session = str(data.get("session_id") or "")
        conn = connect(Config.load())
        try:
            if not _open_owner_session(conn, session):
                activity.record(conn, "hook", "request.rejected_session",
                                "not an open chat or live session")
                return 0
            wrapped = channel_messages(text)
            if wrapped is None and _skip_command(conn, text):
                return 0
            if wrapped is None and text.startswith("[system] "):
                return 0
            if wrapped:
                allowed, denied = [], []
                for message in wrapped:
                    if _telegram_sender_allowed(message[2]):
                        allowed.append(message)
                    else:
                        denied.append(message)
                if not allowed:
                    activity.record(conn, "hook", "request.sender_denied",
                                    "telegram sender not on allow list")
                    print(json.dumps({"decision": "block",
                                      "reason": DENY_SENDER_REASON}))
                    return 0
                for _message in denied:
                    activity.record(conn, "hook", "request.sender_denied",
                                    "telegram sender not on allow list")
                wrapped = allowed
            said = [t for _, t, _ in wrapped] if wrapped else [text]
            if all(is_bare_ack(t) for t in said) \
                    and not has_open_question(conn) \
                    and not last_reply_asked(data.get("transcript_path")):
                activity.record(conn, "hook", "request.ack_ignored",
                                "bare acknowledgement, nothing was asked")
                print(json.dumps({"decision": "block", "reason": ACK_REASON}))
                return 0
            if wrapped is None:
                capture(conn, text, str(session))
            for source, inner, _attrs in wrapped or ():
                rid = capture(conn, inner, str(session), source)
                activity.record(conn, "hook", "request.channel",
                                f"request arrived through {source}", str(rid))
            if wrapped and _peek_dir():
                # The Owner asked for something by Telegram: the status peek
                # starts. A failure here must not lose the request.
                try:
                    from . import peek
                    peek.note_turn_started(
                        _peek_dir(), [a.get("chat_id") or "" for _, _, a
                                      in wrapped])
                except Exception:  # noqa: BLE001
                    pass
            close_questions(conn)  # they spoke: whatever was asked is answered
        finally:
            conn.close()
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            return 0
        _hook_failed(raw, type(e).__name__)
    return 0


def _cmd_link(args):
    def go(cfg, conn):
        link(conn, args.request_id, args.items)
        return 0
    return _run(go)


def _cmd_unhandled(args):
    def go(cfg, conn):
        from . import items, search
        try:
            search.reindex_records(cfg, conn)
        except Exception:
            pass
        open_items = {row["id"]: row["title"] for row in items.open_items(conn)}
        for r in unhandled(conn):
            via = f"(via {r['source']}) " if r["source"] else ""
            print(f"R-{r['id']}\t{ct_display(r['received_at'])}\t{via}{r['text']}")
            hits = []
            try:
                hits = [h for h in search.search(cfg, r["text"], kind="item", limit=10)
                        if h.ref in open_items][:3]
                if not hits:
                    terms = search.re.findall(r"\w+", r["text"])
                    if terms:
                        expr = " OR ".join('"' + t + '"' for t in terms)
                        sc = search._open(cfg)
                        try:
                            rows = sc.execute(
                                "SELECT ref, title FROM docs WHERE docs MATCH ? AND kind='item'"
                                " ORDER BY rank LIMIT 10", (expr,)).fetchall()
                            hits = [r_row for r_row in rows if r_row["ref"] in open_items][:3]
                        finally:
                            sc.close()
            except Exception:
                hits = []
            for h in hits:
                ref = h.ref if hasattr(h, "ref") else h["ref"]
                title = open_items.get(ref) or (h.title if hasattr(h, "title") else h["title"])
                print(f"  maybe: cb request link R-{r['id']} {ref}  ({title})")
        return 0
    return _run(go)


def _cmd_brief(args):
    def go(cfg, conn):
        print(startup_brief(conn, args.limit))
        return 0
    return _run(go)


def _cmd_question_open(args):
    def go(cfg, conn):
        print(open_question(conn, args.text))
        return 0
    return _run(go)


def _cmd_question_close(args):
    def go(cfg, conn):
        print(close_questions(conn))
        return 0
    return _run(go)


def _cmd_question_list(args):
    def go(cfg, conn):
        for r in conn.execute("SELECT * FROM owner_questions WHERE"
                              " answered_at IS NULL ORDER BY id"):
            print(f"{r['id']}\t{ct_display(r['asked_at'])}\t{r['text']}")
        return 0
    return _run(go)


def _cmd_close(args):
    def go(cfg, conn):
        close_request(conn, args.request_id, args.reason)
        return 0
    return _run(go)


def register(subparsers) -> None:
    sp = subparsers.add_parser("request", help="Owner requests")
    sub = sp.add_subparsers(dest="request_cmd", required=True)
    ln = sub.add_parser("link")
    ln.add_argument("request_id", help="2 or R-2")
    ln.add_argument("items", nargs="+")
    ln.set_defaults(func=_cmd_link)
    cl = sub.add_parser("close", help="close a request that needs no item")
    cl.add_argument("request_id", help="2 or R-2")
    cl.add_argument("--reason", required=True, help="why this needs no item")
    cl.set_defaults(func=_cmd_close)
    un = sub.add_parser("unhandled")
    un.set_defaults(func=_cmd_unhandled)
    br = sub.add_parser("brief")
    br.add_argument("--limit", type=int, default=20)
    br.set_defaults(func=_cmd_brief)
    q = subparsers.add_parser(
        "question", help="questions waiting for the Owner's answer")
    qs = q.add_subparsers(dest="question_cmd", required=True)
    qo = qs.add_parser("open", help="register a question you just asked them")
    qo.add_argument("text")
    qo.set_defaults(func=_cmd_question_open)
    qs.add_parser("close", help="mark every open question answered"
                  ).set_defaults(func=_cmd_question_close)
    qs.add_parser("list").set_defaults(func=_cmd_question_list)
    h = subparsers.add_parser("hook", help="Claude Code hooks")
    hs = h.add_subparsers(dest="hook_cmd", required=True)
    hp = hs.add_parser("prompt")
    hp.set_defaults(func=_cmd_hook_prompt)
    hs.add_parser("stop", help="kept for sessions started by an older engine; "
                  "does nothing").set_defaults(func=_cmd_hook_stop)
