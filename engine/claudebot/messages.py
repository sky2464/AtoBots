"""Messages between bots. A bot that asks another bot leaves a handoff note
and may exit while it waits; the answer wakes it. Claiming is the
dispatcher's job (Task 9); this module creates, answers and completes."""
import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime

from . import activity, bots, items
from .config import Config
from .db import connect, ct_display, tx, utcnow

LEVELS = ("housekeeping", "normal", "deep", "heavy")
MAX_BODY_BYTES = 4 * 1024
SYSTEM = "system"
_PENDING = ("queued", "claimed")


def _check_body(body) -> None:
    if not isinstance(body, str):
        raise ValueError("body must be text")
    if len(body.encode()) > MAX_BODY_BYTES:
        raise ValueError(
            f"message body is over {MAX_BODY_BYTES // 1024} KB: write it to a "
            "file and send the file's path instead")


def _insert(conn, frm, to, subject, body, item_id, kind, priority, level,
            effort, in_reply_to, dedupe_key, hop_limit, now, window) -> int:
    """Caller holds the transaction. Returns the message id, or 0 when the
    hop limit stopped the chain."""
    if frm != SYSTEM:
        bots.get(conn, frm)
    if bots.get(conn, to).status == "retired":
        raise ValueError(f"bot {to} is retired")
    if level not in LEVELS:
        raise ValueError(f"level must be one of: {', '.join(LEVELS)}")
    _check_body(body)
    if dedupe_key is not None:
        r = conn.execute("SELECT id FROM messages WHERE dedupe_key=?",
                         (dedupe_key,)).fetchone()
        if r:
            return r["id"]
    prev = None
    if in_reply_to is not None:
        prev = conn.execute("SELECT * FROM messages WHERE id=?",
                            (in_reply_to,)).fetchone()
        if prev is None:
            raise KeyError(f"message {in_reply_to}")
        if item_id is None:
            item_id = prev["item_id"]
    elif item_id is not None:
        items.get(conn, item_id)
        prev = conn.execute(
            "SELECT * FROM messages WHERE item_id=? AND chain_id IS NOT NULL"
            " ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    if prev is not None and in_reply_to is None and not _within(
            prev["created_at"], now, window):
        prev = None  # item went quiet: a new exchange starts a new chain
    if prev is not None and prev["chain_id"]:
        chain, hop = prev["chain_id"], (prev["hop"] or 0) + 1
    else:
        chain, hop = "C-" + uuid.uuid4().hex[:12], 0
    if hop >= hop_limit:
        _hop_stop(conn, frm, to, subject, item_id, chain, hop, now)
        return 0
    cur = conn.execute(
        "INSERT INTO messages(from_bot, to_bot, item_id, kind, subject, body,"
        " priority, level, effort, hop, chain_id, dedupe_key, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',?)",
        (frm, to, item_id, kind, subject, body, priority, level, effort, hop,
         chain, dedupe_key, now))
    activity.record(conn, frm, "message.send",
                    f"{frm} -> {to}: {subject}", str(cur.lastrowid))
    return cur.lastrowid


def _within(then: str, now: str, window: int) -> bool:
    return (datetime.fromisoformat(now) - datetime.fromisoformat(then)
            ).total_seconds() <= window


def _hop_stop(conn, frm, to, subject, item_id, chain, hop, now) -> None:
    detail = (f"chain {chain} stopped at hop {hop}: {frm} -> {to}: {subject}"
              + (f" (item {item_id})" if item_id else ""))
    key = f"hopstop:{chain}"
    activity.record(conn, SYSTEM, "message.hop_stop", detail, chain)
    admin = conn.execute("SELECT status FROM bots WHERE name='admin'").fetchone()
    if admin is None or admin["status"] == "retired":
        return
    # New chain, no item: a note on the item would make it the item's latest
    # chain and reset the loop guard.
    _insert(conn, SYSTEM, "admin", "Message chain stopped at hop limit",
            detail + ". The message was not delivered.", None, "note", "normal",
            "normal", None, None, key, 10**9, now, 0)


def send(conn: sqlite3.Connection, frm: str, to: str, subject: str, body: str,
         item_id: str | None = None, kind: str = "note",
         priority: str = "normal", level: str = "normal",
         effort: str | None = None, in_reply_to: int | None = None,
         dedupe_key: str | None = None,
         hop_limit: int = Config.hop_limit,
         chain_window_s: int = Config.chain_window_s,
         now: str | None = None) -> int:
    """Queue a message and return its id (the original id for a repeated
    dedupe_key). Returns 0 if the hop limit stopped the chain; Admin is told
    once per chain."""
    with tx(conn):
        return _insert(conn, frm, to, subject, body, item_id, kind, priority,
                       level, effort, in_reply_to, dedupe_key, hop_limit,
                       now or utcnow(), chain_window_s)


def handoff_rel_path(item_id: str, frm: str) -> str:
    return f"work/{item_id}/handoff-{frm}.md"


def ask(cfg: Config, conn: sqlite3.Connection, frm: str, to: str,
        item_id: str, question: str, handoff: str,
        now: str | None = None) -> int:
    """Leave a handoff note in the item's folder, mark the item as waiting on
    `to`, and send the question. The asker can then exit."""
    items.get(conn, item_id)
    bots.get(conn, frm)
    path = items.folder(cfg, item_id) / f"handoff-{frm}.md"
    now = now or utcnow()
    with tx(conn):
        mid = _insert(conn, frm, to, f"Question on {item_id}", question,
                      item_id, "question", "normal", "normal", None, None,
                      None, cfg.hop_limit,
                      now, cfg.chain_window_s)
        if mid:
            conn.execute("UPDATE items SET waiting_on=?, updated_at=?"
                         " WHERE id=?", (to, utcnow(), item_id))
            activity.record(conn, frm, "item.waiting", f"{item_id}: waiting on {to}",
                            item_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(handoff)
    return mid


def reply(conn: sqlite3.Connection, frm: str, message_id: int, body: str,
          file: str | None = None, hop_limit: int = Config.hop_limit,
          chain_window_s: int = Config.chain_window_s,
          now: str | None = None) -> int:
    """Answer a message. Clears the item's waiting_on and wakes the asker; the
    reply carries the path of the asker's handoff note."""
    with tx(conn):
        m = conn.execute("SELECT * FROM messages WHERE id=?",
                         (message_id,)).fetchone()
        if m is None:
            raise KeyError(f"message {message_id}")
        if m["to_bot"] != frm:
            raise ValueError(f"message {message_id} was not sent to {frm}")
        _check_body(body)
        text = body
        if file:
            text += f"\n\nAnswer file: {file}"
        if m["item_id"]:
            text += f"\n\nYour handoff note: {handoff_rel_path(m['item_id'], m['from_bot'])}"
        mid = _insert(conn, frm, m["from_bot"], f"Re: {m['subject']}", text,
                      m["item_id"], "answer", m["priority"], m["level"],
                      m["effort"], message_id, None, hop_limit,
                      now or utcnow(), chain_window_s)
        if m["item_id"] and mid:
            conn.execute("UPDATE items SET waiting_on=NULL, updated_at=?"
                         " WHERE id=? AND waiting_on=?",
                         (utcnow(), m["item_id"], frm))
            activity.record(conn, frm, "item.unwaiting",
                            f"{m['item_id']}: answered", m["item_id"])
    return mid


def pending_for(conn: sqlite3.Connection, bot: str) -> list:
    return conn.execute(
        "SELECT * FROM messages WHERE to_bot=? AND status IN ('queued','claimed')"
        " ORDER BY id", (bot,)).fetchall()


def complete(conn: sqlite3.Connection, ids) -> None:
    with tx(conn):
        for i in ids:
            r = conn.execute("SELECT to_bot FROM messages WHERE id=?",
                             (i,)).fetchone()
            if r is None:
                raise KeyError(f"message {i}")
            conn.execute("UPDATE messages SET status='done', done_at=?,"
                         " lease_until=NULL WHERE id=?", (utcnow(), i))
            activity.record(conn, r["to_bot"], "message.done", str(i), str(i))


# ---- CLI ----

def _actor() -> str:
    return os.environ.get("CB_BOT", "cli")


def _run(fn):
    cfg = Config.load()
    conn = connect(cfg)
    try:
        return fn(cfg, conn)
    except (ValueError, KeyError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_send(args):
    def go(cfg, conn):
        mid = send(conn, args.frm, args.to, args.subject, args.body,
                   item_id=args.item, kind=args.kind, priority=args.priority,
                   level=args.level, effort=args.effort,
                   in_reply_to=args.in_reply_to, dedupe_key=args.dedupe_key,
                   hop_limit=cfg.hop_limit,
                   chain_window_s=cfg.chain_window_s)
        if not mid:
            print("error: hop limit reached; not delivered, Admin was told",
                  file=sys.stderr)
            return 1
        print(mid)
        return 0
    return _run(go)


def _cmd_ask(args):
    def go(cfg, conn):
        with open(args.handoff_file) as f:
            handoff = f.read()
        mid = ask(cfg, conn, args.frm, args.to, args.item, args.question, handoff)
        if not mid:
            print("error: hop limit reached; not delivered, Admin was told",
                  file=sys.stderr)
            return 1
        print(mid)
        return 0
    return _run(go)


def _cmd_reply(args):
    def go(cfg, conn):
        print(reply(conn, args.frm, args.message_id, args.body, file=args.file,
                    hop_limit=cfg.hop_limit, chain_window_s=cfg.chain_window_s))
        return 0
    return _run(go)


def _cmd_inbox(args):
    def go(cfg, conn):
        for r in pending_for(conn, args.bot or args.frm):
            print(f"{r['id']}\t{r['status']}\t{r['from_bot']}\t{r['item_id'] or '-'}"
                  f"\t{ct_display(r['created_at'])}\t{r['subject']}")
            if args.full:
                print(r["body"])
        return 0
    return _run(go)


def register(subparsers) -> None:
    me = _actor()
    s = subparsers.add_parser("send", help="send a message to a bot")
    s.add_argument("to")
    s.add_argument("subject")
    s.add_argument("body")
    s.add_argument("--from", dest="frm", default=me)
    s.add_argument("--item")
    s.add_argument("--kind", default="note")
    s.add_argument("--priority", default="normal")
    s.add_argument("--level", default="normal", choices=LEVELS)
    s.add_argument("--effort")
    s.add_argument("--in-reply-to", type=int)
    s.add_argument("--dedupe-key")
    s.set_defaults(func=_cmd_send)
    a = subparsers.add_parser("ask", help="ask a bot and leave a handoff note")
    a.add_argument("to")
    a.add_argument("item")
    a.add_argument("question")
    a.add_argument("--handoff-file", required=True,
                   help="file holding what you were doing and what to do on the answer")
    a.add_argument("--from", dest="frm", default=me)
    a.set_defaults(func=_cmd_ask)
    r = subparsers.add_parser("reply", help="answer a message")
    r.add_argument("message_id", type=int)
    r.add_argument("body")
    r.add_argument("--file", help="path of a longer answer")
    r.add_argument("--from", dest="frm", default=me)
    r.set_defaults(func=_cmd_reply)
    i = subparsers.add_parser("inbox", help="unfinished messages for a bot")
    i.add_argument("--bot")
    i.add_argument("--from", dest="frm", default=me)
    i.add_argument("--full", action="store_true")
    i.set_defaults(func=_cmd_inbox)
