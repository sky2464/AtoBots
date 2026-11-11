"""Written, approved plans for system changes."""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from . import activity, admin_live
from .config import Config
from .db import connect, utcnow

STATUS_PLANNED = "planned"
STATUS_APPROVED = "approved"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def normalize_id(raw: str) -> str:
    """Normalize C-1, C-0001, c1, 1 to C-0001."""
    if not isinstance(raw, str):
        raise ValueError(f"change id must be a string, not {type(raw)}")
    raw = raw.strip()
    m = re.match(r"^(?:[Cc]-?)?([0-9]+)$", raw)
    if not m:
        raise ValueError(f"invalid change id: {raw!r}")
    return f"C-{int(m.group(1)):04d}"


def plan(conn: sqlite3.Connection, title: str, steps: str,
         touches: list[str], actor: str = "cli") -> str:
    title = (title or "").strip()
    if not title:
        raise ValueError("change title is required")
    steps = (steps or "").strip()
    if not steps:
        raise ValueError("change steps are required")
    if not touches:
        raise ValueError("--touches is required and must list at least one path, unit or package")
    clean_touches = [t.strip() for t in touches if t and t.strip()]
    if not clean_touches:
        raise ValueError("--touches cannot be empty")

    now = utcnow()
    cur = conn.execute(
        "INSERT INTO changes(title, steps, touches, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, steps, json.dumps(clean_touches), STATUS_PLANNED, now),
    )
    n = cur.lastrowid
    cid = f"C-{n:04d}"
    conn.execute("UPDATE changes SET id=? WHERE n=?", (cid, n))
    activity.record(conn, actor, "change.plan", f"planned {cid}: {title}", ref=cid)
    return cid


def get(conn: sqlite3.Connection, change_id: str) -> sqlite3.Row:
    cid = normalize_id(change_id)
    row = conn.execute("SELECT * FROM changes WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise KeyError(f"unknown change {change_id}")
    return row


def format_show(row: sqlite3.Row) -> str:
    touches = []
    if row["touches"]:
        try:
            touches = json.loads(row["touches"])
        except (ValueError, TypeError):
            touches = [row["touches"]]

    lines = [
        f"Change {row['id']}: {row['title']}",
        f"Status: {row['status']}",
        f"Created: {row['created_at']}",
    ]
    if row["approved_at"]:
        lines.append(f"Approved: {row['approved_at']}")
        if row["approved_by_quote"]:
            lines.append(f"Quote: \"{row['approved_by_quote']}\"")
        if row["session_id"]:
            lines.append(f"Session: {row['session_id']}")
    if row["completed_at"]:
        lines.append(f"Completed: {row['completed_at']}")
    if row["notes"]:
        lines.append(f"Notes: {row['notes']}")

    lines.append("Touches:")
    for t in touches:
        lines.append(f"  - {t}")

    lines.append("Steps:")
    for s_line in (row["steps"] or "").splitlines():
        lines.append(f"  {s_line}")

    return "\n".join(lines)


def approve(conn: sqlite3.Connection, change_id: str, quote: str,
            session: str, actor: str) -> str:
    quote = (quote or "").strip()
    if not quote:
        raise ValueError("approval requires the Owner's exact words (--quote)")
    row = get(conn, change_id)
    if row["status"] != STATUS_PLANNED:
        raise ValueError(
            f"change {row['id']} cannot be approved (current status is {row['status']})"
        )

    now = utcnow()
    conn.execute(
        "UPDATE changes SET status=?, approved_at=?, approved_by_quote=?, "
        "session_id=? WHERE id=?",
        (STATUS_APPROVED, now, quote, session, row["id"]),
    )
    activity.record(conn, actor, "change.approve", f"approved {row['id']}: {quote}", ref=row["id"])
    return row["id"]


def done(cfg: Config, conn: sqlite3.Connection, change_id: str,
         failed_reason: str | None = None, actor: str = "cli",
         verify_fn=None) -> str:
    row = get(conn, change_id)
    now = utcnow()

    if failed_reason:
        reason = failed_reason.strip() or "failed"
        conn.execute(
            "UPDATE changes SET status=?, completed_at=?, notes=? WHERE id=?",
            (STATUS_FAILED, now, reason, row["id"]),
        )
        activity.record(conn, actor, "change.failed", f"failed {row['id']}: {reason}", ref=row["id"])
        return row["id"]

    verify_fn = verify_fn or admin_live.verify_services
    ok, lines = verify_fn(cfg)
    if not ok:
        raise RuntimeError(f"services did not verify: {'; '.join(lines)}")

    conn.execute(
        "UPDATE changes SET status=?, completed_at=?, notes=NULL WHERE id=?",
        (STATUS_DONE, now, row["id"]),
    )
    activity.record(conn, actor, "change.done", f"completed {row['id']}", ref=row["id"])
    return row["id"]


def _cmd_plan(args) -> int:
    cfg = Config.load()
    steps_path = Path(args.steps_file)
    if not steps_path.is_file():
        print(f"error: steps file not found: {steps_path}", file=sys.stderr)
        return 1
    try:
        steps_text = steps_path.read_text()
    except OSError as e:
        print(f"error: cannot read steps file: {e}", file=sys.stderr)
        return 1

    conn = connect(cfg)
    try:
        cid = plan(conn, args.title, steps_text, args.touches, actor=activity.actor())
        print(cid)
        return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def _cmd_show(args) -> int:
    cfg = Config.load()
    conn = connect(cfg)
    try:
        row = get(conn, args.id)
        print(format_show(row))
        return 0
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_approve(args) -> int:
    cfg = Config.load()
    conn = connect(cfg)
    try:
        who = activity.caller(conn)
        if not activity.allows_owner(who):
            activity.refuse(conn, who, "change approve",
                            "only an Owner session may approve a change")
        session = who.session if who.actor != "cli" else f"cli:{utcnow()}"
        cid = approve(conn, args.id, args.quote, session, who.actor)
        print(f"Approved {cid}.")
        return 0
    except PermissionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_done(args) -> int:
    cfg = Config.load()
    conn = connect(cfg)
    try:
        cid = done(cfg, conn, args.id, failed_reason=args.failed,
                   actor=activity.actor())
        status = "failed" if args.failed else "done"
        print(f"Closed {cid} ({status}).")
        return 0
    except (RuntimeError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def register(subparsers) -> None:
    cp = subparsers.add_parser("change", help="plan, approve, and track system changes")
    sub = cp.add_subparsers(dest="change_cmd", required=True)

    plan_p = sub.add_parser("plan", help="create a new change plan")
    plan_p.add_argument("title", help="title of the change")
    plan_p.add_argument("--steps-file", required=True, help="file containing change steps")
    plan_p.add_argument("--touches", nargs="+", required=True,
                        help="folders, files, units or packages affected")
    plan_p.set_defaults(func=_cmd_plan)

    show_p = sub.add_parser("show", help="show a change plan")
    show_p.add_argument("id", help="change id (e.g. C-1 or C-0001)")
    show_p.set_defaults(func=_cmd_show)

    app_p = sub.add_parser("approve", help="approve a change plan (Owner session required)")
    app_p.add_argument("id", help="change id")
    app_p.add_argument("--quote", required=True, help="the Owner's exact words approving the plan")
    app_p.set_defaults(func=_cmd_approve)

    done_p = sub.add_parser("done", help="mark a change done or failed")
    done_p.add_argument("id", help="change id")
    done_p.add_argument("--failed", help="reason if the change failed")
    done_p.set_defaults(func=_cmd_done)
