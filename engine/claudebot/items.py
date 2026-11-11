"""Work items and decisions. IDs are T-0001 / D-0001 and are never reused
(they come from AUTOINCREMENT row numbers, so archiving or deleting a row
cannot free a number)."""
import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

from . import activity, bots
from .config import Config
from .db import connect, ct_display, tx, utcnow

STATUSES = ("pending", "todo", "next", "doing", "paused", "feature", "slice",
            "finding", "done", "parked")
_CLOSED = ("done", "parked")
_ID_RE = re.compile(r"T-\d{4,}")
_DONE_SQL = "('done','parked')"


def _check_id(id) -> str:
    if not isinstance(id, str) or not _ID_RE.fullmatch(id):
        raise ValueError("item id must match ^T-\\d{4,}$")
    return id


def _check_status(status) -> str:
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    return status


def _get(conn: sqlite3.Connection, id: str) -> sqlite3.Row:
    _check_id(id)
    r = conn.execute("SELECT * FROM items WHERE id=?", (id,)).fetchone()
    if r is None:
        raise KeyError(id)
    return r


def get(conn: sqlite3.Connection, id: str) -> sqlite3.Row:
    return _get(conn, id)


def folder(cfg: Config, id: str) -> Path:
    return cfg.work_dir / _check_id(id)


def new(cfg: Config, conn: sqlite3.Connection, title: str, owner: str,
        status: str = "todo", parent: str | None = None, priority: int = 0,
        owner_waiting: bool = False, actor: str | None = None,
        finding: str | None = None) -> str:
    _check_status(status)
    if finding:
        from . import doctor  # late: it imports the dispatcher
        if not doctor.valid_finding_key(finding):
            raise ValueError(
                f"{finding!r} is not a key the health check raises "
                "(see `cb doctor`; keys such as disk, memory, backup-push, "
                "database, run:ID, waiting:T-0001, bots:NAME:FILE)")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required")
    bots.get(conn, owner)
    now = utcnow()
    with tx(conn):
        if parent is not None:
            _get(conn, parent)
        cur = conn.execute(
            "INSERT INTO items(status, owner, title, parent, priority,"
            " owner_waiting, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (status, owner, title, parent, int(priority), int(bool(owner_waiting)),
             now, now))
        id = f"T-{cur.lastrowid:04d}"
        conn.execute("UPDATE items SET id=? WHERE n=?", (id, cur.lastrowid))
        activity.record(conn, actor or owner, "item.new", f"{id}: {title}", id)
        if finding:
            # Kept in the activity record, not the note, so pausing or handing
            # off the item cannot erase it. The doctor closes the item when
            # this finding is gone (see doctor.close_cleared).
            activity.record(conn, actor or owner, "item.finding",
                            f"finding:{finding}", id)
    folder(cfg, id).mkdir(parents=True, exist_ok=True)
    return id


def set_status(conn: sqlite3.Connection, id: str, status: str, actor: str) -> None:
    _check_status(status)
    with tx(conn):
        old = _get(conn, id)["status"]
        conn.execute("UPDATE items SET status=?, updated_at=? WHERE id=?",
                     (status, utcnow(), id))
        activity.record(conn, actor, "item.status", f"{id}: {old} -> {status}", id)


def pause(conn: sqlite3.Connection, id: str, note: str, actor: str) -> None:
    with tx(conn):
        old = _get(conn, id)["status"]
        conn.execute("UPDATE items SET status='paused', note=?, updated_at=?"
                     " WHERE id=?", (note, utcnow(), id))
        activity.record(conn, actor, "item.pause", f"{id}: {old} -> paused", id)


def resume(conn: sqlite3.Connection, id: str, actor: str) -> str:
    with tx(conn):
        r = _get(conn, id)
        if r["status"] != "paused":
            raise ValueError(f"{id} is {r['status']}, not paused")
        conn.execute("UPDATE items SET status='doing', updated_at=? WHERE id=?",
                     (utcnow(), id))
        activity.record(conn, actor, "item.resume", id, id)
    return r["note"] or ""


def assign(conn: sqlite3.Connection, id: str, to_bot: str, actor: str,
           note: str | None = None) -> None:
    bots.get(conn, to_bot)
    with tx(conn):
        r = _get(conn, id)
        keep = r["note"]
        if note:
            keep = f"{keep}\n{note}" if keep else note
        conn.execute("UPDATE items SET owner=?, status='todo', note=?,"
                     " updated_at=? WHERE id=?", (to_bot, keep, utcnow(), id))
        conn.execute("DELETE FROM meta WHERE key=?",
                     ("task.fail." + id,))
        activity.record(conn, actor, "item.assign",
                        f"{id}: {r['owner']} -> {to_bot}", id)


def split(cfg: Config, conn: sqlite3.Connection, parent: str, titles: list,
          owner: str) -> list:
    """Child items start as `todo` so the dispatcher can wake their owner."""
    _get(conn, parent)
    return [new(cfg, conn, t, owner, status="todo", parent=parent)
            for t in titles]


_OPEN_ORDER = "ORDER BY owner_waiting DESC, priority DESC, n ASC"
# Items owned by infrastructure roles (the backend) are not company work.
_NOT_RESERVED = (" AND owner NOT IN (SELECT name FROM bots WHERE org='"
                 + bots.RESERVED_ORG + "')")


def set_owner_waiting(conn: sqlite3.Connection, id: str, on: bool,
                      actor: str) -> None:
    """Mark that the next move is the Owner's (or clear it again)."""
    with tx(conn):
        _get(conn, id)
        conn.execute("UPDATE items SET owner_waiting=?, updated_at=?"
                     " WHERE id=?", (int(bool(on)), utcnow(), id))
        activity.record(conn, actor, "item.owner_waiting",
                        f"{id}: waiting on the Owner" if on
                        else f"{id}: no longer waiting on the Owner", id)


def next_items(conn: sqlite3.Connection, owner: str | None = None,
               limit: int = 20, include_reserved: bool = False) -> list:
    sql = (f"SELECT * FROM items WHERE archived_at IS NULL"
           f" AND status NOT IN {_DONE_SQL}")
    if not include_reserved:
        sql += _NOT_RESERVED
    args: list = []
    if owner is not None:
        sql += " AND owner=?"
        args.append(owner)
    return conn.execute(f"{sql} {_OPEN_ORDER} LIMIT ?", (*args, limit)).fetchall()


def open_items(conn: sqlite3.Connection,
               include_reserved: bool = False) -> list:
    """Every item not done or parked, plus any parent that still has an
    unfinished child, whatever its own status says."""
    extra = "" if include_reserved else _NOT_RESERVED
    return conn.execute(
        f"SELECT * FROM items i WHERE archived_at IS NULL AND (status NOT IN"
        f" {_DONE_SQL} OR EXISTS (SELECT 1 FROM items c WHERE c.parent=i.id"
        f" AND c.archived_at IS NULL AND c.status NOT IN {_DONE_SQL}))"
        f"{extra} {_OPEN_ORDER}").fetchall()


# Decisions are recorded only from an Owner session (see the CLI). Spending one
# still requires that provenance on the row.
OWNER_DECISION_ACTORS = frozenset({"admin", "cli", "owner"})


def require_owner_decision(conn: sqlite3.Connection, decision_id: str):
    """Return the decision row, or raise PermissionError if it is missing or
    not bound to Owner session provenance."""
    if not decision_id:
        raise PermissionError("a recorded Owner decision is needed")
    r = conn.execute("SELECT * FROM decisions WHERE id=?",
                     (decision_id,)).fetchone()
    if r is None:
        raise PermissionError(f"unknown decision {decision_id}")
    if r["recorded_by"] not in OWNER_DECISION_ACTORS:
        raise PermissionError(
            f"{decision_id} was not recorded by Admin or the Owner")
    if not r["owner_session"]:
        raise PermissionError(
            f"{decision_id} is not bound to an Owner chat/live session")
    return r


def decide(conn: sqlite3.Connection, title: str, quote: str, said_at: str,
           session: str, actor: str) -> str:
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("a decision needs the Owner's exact words (quote)")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title is required")
    if actor not in OWNER_DECISION_ACTORS:
        raise PermissionError(
            "only Admin or the Owner may record decisions")
    if not isinstance(session, str) or not session.strip():
        raise ValueError("a decision needs the Owner session id")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO decisions(title, quote, said_at, session, recorded_by,"
            " created_at, owner_session) VALUES (?,?,?,?,?,?,1)",
            (title, quote, said_at, session, actor, utcnow()))
        id = f"D-{cur.lastrowid:04d}"
        conn.execute("UPDATE decisions SET id=? WHERE n=?", (id, cur.lastrowid))
        activity.record(conn, actor, "decision.record", f"{id}: {title}", id)
    return id


# ---- CLI ----

_actor = activity.actor


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


def _line(r) -> str:
    flag = "!" if r["owner_waiting"] else " "
    return f"{r['id']}\t{flag}\t{r['status']}\t{r['owner']}\t{r['title']}"


def _cmd_new(args):
    def go(cfg, conn):
        item_id = new(cfg, conn, args.title, args.owner, status=args.status,
                      parent=args.parent, priority=args.priority,
                      owner_waiting=args.owner_waiting, actor=_actor(),
                      finding=args.finding)
        print(item_id)
        if not args.no_request:
            from . import requests as req_mod
            if args.request:
                rid = req_mod._request_number(args.request)
                req_mod.link(conn, rid, [item_id])
                print(f"Linked to R-{rid}.")
            else:
                who = activity.caller(conn)
                session = who.session or os.environ.get("CB_SESSION_ID")
                if session:
                    rows = conn.execute(
                        "SELECT r.id FROM owner_requests r WHERE r.session_id=?"
                        " AND NOT EXISTS (SELECT 1 FROM request_items l WHERE l.request_id=r.id)"
                        " ORDER BY r.id", (session,)).fetchall()
                    for row in rows:
                        rid = row["id"]
                        req_mod.link(conn, rid, [item_id])
                        print(f"Linked to R-{rid}.")
        return 0
    return _run(go)


def _cmd_status(args):
    def go(cfg, conn):
        set_status(conn, args.id, args.status, _actor())
        return 0
    return _run(go)


def _cmd_pause(args):
    def go(cfg, conn):
        pause(conn, args.id, args.note, _actor())
        return 0
    return _run(go)


def _cmd_resume(args):
    def go(cfg, conn):
        print(resume(conn, args.id, _actor()))
        return 0
    return _run(go)


def _cmd_assign(args):
    def go(cfg, conn):
        assign(conn, args.id, args.to, _actor(), note=args.note)
        return 0
    return _run(go)


def _cmd_split(args):
    def go(cfg, conn):
        owner = args.owner or _get(conn, args.parent)["owner"]
        for i in split(cfg, conn, args.parent, args.titles, owner):
            print(i)
        return 0
    return _run(go)


def _cmd_show(args):
    def go(cfg, conn):
        r = _get(conn, args.id)
        for k in ("id", "title", "status", "owner", "parent", "priority",
                  "owner_waiting", "note"):
            print(f"{k}: {r[k]}")
        print(f"created: {ct_display(r['created_at'])}")
        print(f"updated: {ct_display(r['updated_at'])}")
        kids = conn.execute("SELECT id, status FROM items WHERE parent=? ORDER BY n",
                            (args.id,)).fetchall()
        if kids:
            print("children: " + ", ".join(f"{k['id']} ({k['status']})" for k in kids))
        return 0
    return _run(go)


def _cmd_next(args):
    def go(cfg, conn):
        for r in next_items(conn, args.owner, args.limit):
            print(_line(r))
        return 0
    return _run(go)


def _cmd_open(args):
    def go(cfg, conn):
        for r in open_items(conn):
            print(_line(r))
        return 0
    return _run(go)


def _cmd_waiting_on_owner(args):
    def go(cfg, conn):
        set_owner_waiting(conn, args.id, not args.off, _actor())
        return 0
    return _run(go)


def _cmd_decision_record(args):
    def go(cfg, conn):
        who = activity.caller(conn)
        if not activity.allows_owner(who):
            activity.refuse(conn, who, "decision record",
                            "only an Owner session may record a decision")
        session = who.session if who.actor != "cli" else f"cli:{utcnow()}"
        print(decide(conn, args.title, args.quote, args.said_at or utcnow(),
                     session, who.actor))
        return 0
    return _run(go)


def _cmd_decision_show(args):
    def go(cfg, conn):
        r = conn.execute("SELECT * FROM decisions WHERE id=?", (args.id,)).fetchone()
        if r is None:
            raise KeyError(args.id)
        print(f"id: {r['id']}\ntitle: {r['title']}")
        print(f"said: {ct_display(r['said_at'])}" if _is_iso(r["said_at"])
              else f"said: {r['said_at']}")
        print(f"session: {r['session']}\nrecorded by: {r['recorded_by']}")
        print(f"quote: {r['quote']}")
        return 0
    return _run(go)


def _is_iso(s) -> bool:
    from datetime import datetime
    try:
        datetime.fromisoformat(s)
        return True
    except (TypeError, ValueError):
        return False


def register(subparsers) -> None:
    sp = subparsers.add_parser("item", help="work items")
    sub = sp.add_subparsers(dest="item_cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("title")
    n.add_argument("--owner", default=_actor())
    n.add_argument("--status", default="todo", choices=STATUSES)
    n.add_argument("--parent")
    n.add_argument("--priority", type=int, default=0)
    n.add_argument("--owner-waiting", action="store_true")
    n.add_argument("--finding", help="health-check finding key this item is "
                   "for; the item closes itself when the problem clears")
    n.add_argument("--request", help="link this item to a specific request (e.g. R-12)")
    n.add_argument("--no-request", action="store_true",
                   help="do not link this item to unhandled session requests")
    n.set_defaults(func=_cmd_new)
    s = sub.add_parser("status")
    s.add_argument("id")
    s.add_argument("status")
    s.set_defaults(func=_cmd_status)
    p = sub.add_parser("pause")
    p.add_argument("id")
    p.add_argument("note")
    p.set_defaults(func=_cmd_pause)
    r = sub.add_parser("resume")
    r.add_argument("id")
    r.set_defaults(func=_cmd_resume)
    a = sub.add_parser("assign")
    a.add_argument("id")
    a.add_argument("to")
    a.add_argument("--note")
    a.set_defaults(func=_cmd_assign)
    sl = sub.add_parser("split")
    sl.add_argument("parent")
    sl.add_argument("titles", nargs="+")
    sl.add_argument("--owner")
    sl.set_defaults(func=_cmd_split)
    sh = sub.add_parser("show")
    sh.add_argument("id")
    sh.set_defaults(func=_cmd_show)
    wo = sub.add_parser("waiting-on-owner",
                        help="mark that the next move is the Owner's")
    wo.add_argument("id")
    wo.add_argument("--off", action="store_true",
                    help="clear the mark (the Owner answered)")
    wo.set_defaults(func=_cmd_waiting_on_owner)
    nx = subparsers.add_parser("next", help="what to work on next")
    nx.add_argument("--owner")
    nx.add_argument("--limit", type=int, default=20)
    nx.set_defaults(func=_cmd_next)
    op = subparsers.add_parser("open", help="all open items")
    op.set_defaults(func=_cmd_open)
    d = subparsers.add_parser("decision", help="Owner decisions")
    dsub = d.add_subparsers(dest="decision_cmd", required=True)
    rec = dsub.add_parser("record")
    rec.add_argument("title")
    rec.add_argument("--quote", required=True, help="the Owner's exact words")
    rec.add_argument("--said-at", help="ISO 8601; default now")
    rec.set_defaults(func=_cmd_decision_record)
    shw = dsub.add_parser("show")
    shw.add_argument("id")
    shw.set_defaults(func=_cmd_decision_show)
