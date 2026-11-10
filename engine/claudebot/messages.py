"""Messages between bots. A bot that asks another bot leaves a handoff note
and may exit while it waits; the answer wakes it. Claiming is the
dispatcher's job; this module creates, answers and completes."""
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
_SEND_KINDS = ("note", "question", "answer", "task", "result")


def _check_body(body) -> None:
    if not isinstance(body, str):
        raise ValueError("body must be text")
    if len(body.encode()) > MAX_BODY_BYTES:
        raise ValueError(
            f"message body is over {MAX_BODY_BYTES // 1024} KB: write it to a "
            "file and send the file's path instead")


def resolve_from(conn, requested: str | None = None, *,
                 command: str = "send") -> str:
    """From-field for a command: the bound caller, or the request when cli."""
    who = activity.caller(conn)
    if who.actor == "cli":
        return requested or "cli"
    if requested not in (None, who.actor):
        activity.refuse(conn, who, command, "cannot act as another bot")
    return who.actor


def _check_sender(conn, frm: str, command: str = "send") -> None:
    """Reject a from-field that is not the bound caller. Call outside a transaction."""
    who = activity.caller(conn)
    if who.actor == "cli":
        return
    if frm != who.actor:
        activity.refuse(
            conn, who, command,
            f"cannot send as {frm!r}: this process is bound to {who.actor!r}")


def _insert(conn, frm, to, subject, body, item_id, kind, priority, level,
            effort, in_reply_to, dedupe_key, hop_limit, now, window,
            *, _engine_system: bool = False) -> int:
    """Caller holds the transaction. Returns the message id, or 0 when the
    hop limit stopped the chain. `_engine_system` is only for in-process
    system notes (hop-stop) that may run while CB_BOT is set."""
    if _engine_system:
        if frm != SYSTEM:
            raise ValueError("_engine_system requires from=system")
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
    elif item_id is not None and frm != SYSTEM:
        items.get(conn, item_id)
        prev = conn.execute(
            "SELECT * FROM messages WHERE item_id=? AND chain_id IS NOT NULL"
            " AND from_bot != 'system' ORDER BY id DESC LIMIT 1",
            (item_id,)).fetchone()
    elif frm != SYSTEM and to != SYSTEM:
        # Bot↔bot traffic with no item and no reply link still chains when the
        # other side of the same pair spoke inside the window (ping-pong).
        # One-way floods (A→B, A→B, …) do not chain.
        prev = conn.execute(
            "SELECT * FROM messages WHERE item_id IS NULL AND chain_id IS NOT NULL"
            " AND from_bot=? AND to_bot=? ORDER BY id DESC LIMIT 1",
            (to, frm)).fetchone()
    if prev is not None and in_reply_to is None and not _within(
            prev["created_at"], now, window):
        prev = None  # pair/item went quiet: a new exchange starts a new chain
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
    # chain and reset the loop guard. May run inside a bot process (CB_BOT set).
    _insert(conn, SYSTEM, "admin", "Message chain stopped at hop limit",
            detail + ". The message was not delivered.", None, "note", "normal",
            "normal", None, None, key, 10**9, now, 0, _engine_system=True)


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
    _check_sender(conn, frm, "send")
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
    _check_sender(conn, frm, "ask")
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
          now: str | None = None, home=None) -> int:
    """Answer a message. Clears the item's waiting_on and wakes the asker; the
    reply carries the path of the asker's handoff note (absolute when `home`
    is given: the asker's run starts in its own bot folder)."""
    _check_sender(conn, frm, "reply")
    with tx(conn):
        m = conn.execute("SELECT * FROM messages WHERE id=?",
                         (message_id,)).fetchone()
        if m is None:
            raise KeyError(f"message {message_id}")
        if m["to_bot"] != frm:
            raise ValueError(f"message {message_id} was not sent to {frm}")
        _check_body(body)
        if m["from_bot"] == SYSTEM:
            # Nobody to answer: note it in activity, mark it answered, send nothing.
            conn.execute("UPDATE messages SET status='done', done_at=COALESCE("
                         "done_at, ?), lease_until=NULL WHERE id=?",
                         (utcnow(), message_id))
            activity.record(conn, frm, "message.reply_to_system",
                            f"{message_id}: {body[:200]}", str(message_id))
            return 0
        text = body
        if file:
            text += f"\n\nAnswer file: {file}"
        if m["item_id"]:
            note = handoff_rel_path(m["item_id"], m["from_bot"])
            text += f"\n\nYour handoff note: {os.path.join(home, note) if home else note}"
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


def mark_done(conn: sqlite3.Connection, ids, actor: str) -> None:
    """Close messages manually (inbox hygiene)."""
    rows = []
    for i in ids:
        r = conn.execute("SELECT * FROM messages WHERE id=?", (i,)).fetchone()
        if r is None:
            raise KeyError(f"message {i}")
        if r["status"] not in ("queued", "claimed", "failed"):
            raise ValueError(
                f"message {i} is {r['status']!r}, not queued, claimed or failed")
        if actor not in ("admin", "cli") and actor != r["to_bot"]:
            activity.record(conn, actor, "auth.refused", "message done")
            raise PermissionError(
                f"only {r['to_bot']}, admin or cli may close message {i}")
        rows.append(i)
    with tx(conn):
        for i in rows:
            conn.execute(
                "UPDATE messages SET status='done', done_at=?, lease_until=NULL"
                " WHERE id=?", (utcnow(), i))
            activity.record(conn, actor, "message.done", str(i), str(i))


def retry_failed(conn: sqlite3.Connection, message_id: int, actor: str) -> None:
    if actor not in ("admin", "cli"):
        activity.record(conn, actor, "auth.refused", "message retry")
        raise PermissionError("only admin or cli may retry a failed message")
    with tx(conn):
        r = conn.execute("SELECT * FROM messages WHERE id=?",
                         (message_id,)).fetchone()
        if r is None:
            raise KeyError(f"message {message_id}")
        if r["status"] != "failed":
            raise ValueError(f"message {message_id} is not failed")
        conn.execute(
            "UPDATE messages SET status='queued', attempts=0, lease_until=NULL,"
            " done_at=NULL WHERE id=?", (message_id,))
        activity.record(conn, actor, "message.retry", str(message_id),
                        str(message_id))


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

def _who(conn):
    return activity.caller(conn)


def _run(fn):
    cfg = Config.load()
    conn = connect(cfg)
    try:
        return fn(cfg, conn)
    except (ValueError, KeyError, OSError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


class SendActionParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write('Not sent. Example: cb send tl "subject" "body"\n')
        super().error(message)


def _cmd_send(args):
    def go(cfg, conn):
        frm = resolve_from(conn, args.frm, command="send")
        mid = send(conn, frm, args.to, args.subject, args.body,
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
        sys.stderr.write(f"Sent to {args.to} as message {mid}.\n")
        return 0
    return _run(go)


def _cmd_ask(args):
    def go(cfg, conn):
        with open(args.handoff_file) as f:
            handoff = f.read()
        frm = resolve_from(conn, args.frm, command="ask")
        mid = ask(cfg, conn, frm, args.to, args.item, args.question, handoff)
        if not mid:
            print("error: hop limit reached; not delivered, Admin was told",
                  file=sys.stderr)
            return 1
        print(mid)
        sys.stderr.write(f"Sent to {args.to} as message {mid}.\n")
        return 0
    return _run(go)


def _cmd_reply(args):
    def go(cfg, conn):
        frm = resolve_from(conn, args.frm, command="reply")
        row = conn.execute("SELECT from_bot FROM messages WHERE id=?",
                           (args.message_id,)).fetchone()
        to_name = row["from_bot"] if row else None
        mid = reply(conn, frm, args.message_id, args.body, file=args.file,
                    hop_limit=cfg.hop_limit, chain_window_s=cfg.chain_window_s,
                    home=cfg.home)
        print(mid)
        if mid and to_name:
            sys.stderr.write(f"Sent to {to_name} as message {mid}.\n")
        return 0
    return _run(go)


def _cmd_inbox(args):
    def go(cfg, conn):
        who = _who(conn)
        bot = args.bot or args.frm or (who.actor if who.actor != "cli" else "cli")
        if who.actor not in ("admin", "cli") and bot != who.actor:
            activity.refuse(conn, who, "inbox",
                            "cannot read another bot's inbox")
        for r in pending_for(conn, bot):
            print(f"{r['id']}\t{r['status']}\t{r['from_bot']}\t{r['item_id'] or '-'}"
                  f"\t{ct_display(r['created_at'])}\t{r['subject']}")
            if args.full:
                print(r["body"])
        return 0
    return _run(go)


def _cmd_done(args):
    def go(cfg, conn):
        mark_done(conn, args.ids, _who(conn).actor)
        return 0
    return _run(go)


def _cmd_retry(args):
    def go(cfg, conn):
        retry_failed(conn, args.message_id, _who(conn).actor)
        return 0
    return _run(go)


def register(subparsers) -> None:
    from .runner import EFFORT_ORDER
    s = subparsers.add_parser("send", help="send a message to a bot")
    s.__class__ = SendActionParser
    s.add_argument("to")
    s.add_argument("subject")
    s.add_argument("body")
    s.add_argument("--from", dest="frm",
                   help="sender when run from the shell; a bot session sends as itself")
    s.add_argument("--item")
    s.add_argument("--kind", default="note", choices=_SEND_KINDS)
    s.add_argument("--priority", default="normal", choices=("normal", "urgent"))
    s.add_argument("--level", default="normal", choices=LEVELS)
    s.add_argument("--effort", choices=EFFORT_ORDER)
    s.add_argument("--in-reply-to", type=int)
    s.add_argument("--dedupe-key")
    s.set_defaults(func=_cmd_send)
    a = subparsers.add_parser("ask", help="ask a bot and leave a handoff note")
    a.__class__ = SendActionParser
    a.add_argument("to")
    a.add_argument("item")
    a.add_argument("question")
    a.add_argument("--handoff-file", required=True,
                   help="file holding what you were doing and what to do on the answer")
    a.add_argument("--from", dest="frm",
                   help="sender when run from the shell; a bot session sends as itself")
    a.set_defaults(func=_cmd_ask)
    r = subparsers.add_parser("reply", help="answer a message")
    r.__class__ = SendActionParser
    r.add_argument("message_id", type=int)
    r.add_argument("body")
    r.add_argument("--file", help="path of a longer answer")
    r.add_argument("--from", dest="frm",
                   help="sender when run from the shell; a bot session sends as itself")
    r.set_defaults(func=_cmd_reply)
    i = subparsers.add_parser("inbox", help="unfinished messages for a bot")
    i.add_argument("--bot")
    i.add_argument("--from", dest="frm",
                   help="bot whose inbox to show when run from the shell")
    i.add_argument("--full", action="store_true")
    i.set_defaults(func=_cmd_inbox)
    mp = subparsers.add_parser("message", help="finish or retry messages")
    ms = mp.add_subparsers(dest="message_cmd", required=True)
    done = ms.add_parser("done", help="mark messages done")
    done.add_argument("ids", nargs="+", type=int)
    done.set_defaults(func=_cmd_done)
    retry = ms.add_parser("retry", help="re-queue a failed message")
    retry.add_argument("message_id", type=int)
    retry.set_defaults(func=_cmd_retry)
