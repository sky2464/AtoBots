"""Tasks for workers. A thinker sends a worker a `task` (goal, inputs, outputs,
check). The worker finishes it with `cb task finish`; the outcome goes back to
the sender as a `result`. Tasks are ordinary messages, so the hop limit and the
queue apply to them as to any other message.

A worker can fail the same item twice. Then Admin is told once and the
dispatcher stops waking the worker for that item (see dispatcher._wake_assigned).
"""
import json
import os
import sqlite3
import sys

from . import activity, bots, items, messages
from .config import Config
from .db import connect, tx

ADMIN = "admin"
SYSTEM = "system"
ESCALATE_AT = 2
STATUSES = ("done", "failed")
_FAIL_PREFIX = "task.fail."


def _fail_key(item_id: str) -> str:
    return _FAIL_PREFIX + item_id


def failures(conn: sqlite3.Connection, item_id: str) -> int:
    r = conn.execute("SELECT value FROM meta WHERE key=?",
                     (_fail_key(item_id),)).fetchone()
    return int(r["value"]) if r else 0


def _parse(body: str) -> dict:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def send_task(conn: sqlite3.Connection, frm: str, to: str, item_id: str,
              goal: str, inputs, outputs, check: str) -> int:
    """Send a task to a worker-tier bot. Returns the message id (0 if the hop
    limit stopped it)."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal is required")
    if bots.get(conn, to).tier != "worker":
        raise ValueError(f"{to} is not a worker: tasks go to worker-tier bots")
    items.get(conn, item_id)
    body = json.dumps({"goal": goal, "inputs": list(inputs or []),
                       "outputs": list(outputs or []), "check": check or ""},
                      ensure_ascii=False)
    return messages.send(conn, frm, to, f"Task {item_id}: {goal[:80]}", body,
                         item_id=item_id, kind="task")


def render(body: str, message_id: int) -> str:
    """The task as a worker reads it: goal, inputs, outputs, check, and the
    command that finishes it. Bodies that are not task JSON are shown as text."""
    data = _parse(body)
    finish = (f"When finished, run: cb task finish {message_id} --status "
              "done|failed --note \"what you did\"")
    if not data:
        return f"Task (message {message_id}):\n{body}\n\n{finish}"
    lines = [f"Task (message {message_id}):",
             f"Goal: {data.get('goal', '')}", "Inputs:"]
    lines += [f"  - {p}" for p in data.get("inputs") or []] or ["  (none)"]
    lines.append("Outputs (each must exist when you finish):")
    lines += [f"  - {p}" for p in data.get("outputs") or []] or ["  (none)"]
    lines += [f"Check: {data.get('check', '')}", "", finish]
    return "\n".join(lines)


def wake_body(conn: sqlite3.Connection, item_id: str, to: str,
              fallback: str) -> str:
    """Body of the wake sent to a worker for an item: the item's latest task
    to that worker, rendered, when there is one; otherwise `fallback`."""
    t = conn.execute(
        "SELECT id, body FROM messages WHERE kind='task' AND item_id=? AND"
        " to_bot=? ORDER BY id DESC LIMIT 1", (item_id, to)).fetchone()
    if t is None:
        return fallback
    text = render(t["body"], t["id"])
    if len(text.encode()) > messages.MAX_BODY_BYTES:
        return (f"{fallback}\n\nThe task is message {t['id']}: read it with "
                "`cb inbox --full`.")
    return text


def _result_key(message_id: int) -> str:
    return f"result:{message_id}"


def _bump(conn: sqlite3.Connection, item_id: str) -> int:
    n = failures(conn, item_id) + 1
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                 (_fail_key(item_id), str(n)))
    return n


def finish_task(cfg: Config, conn: sqlite3.Connection, message_id: int,
                status: str, note: str) -> int:
    """Settle a task. `done` is checked: every output path must exist under
    cfg.home, or the task is failed. Sends the sender a `result` and returns its
    id (0 if the hop limit stopped it)."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    m = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if m is None:
        raise KeyError(f"message {message_id}")
    if m["kind"] != "task":
        raise ValueError(f"message {message_id} is not a task")
    if conn.execute("SELECT 1 FROM messages WHERE dedupe_key=?",
                    (_result_key(message_id),)).fetchone():
        raise ValueError(f"task {message_id} is already finished")
    item = m["item_id"]
    missing = []
    if status == "done":
        missing = [p for p in _parse(m["body"]).get("outputs") or []
                   if not (cfg.home / p).exists()]
        if missing:
            status = "failed"
    text = f"Task {message_id} on {item or '-'} finished: {status}."
    if missing:
        text += " Missing outputs: " + ", ".join(missing) + "."
    if note:
        text += f"\n\nWorker note: {note}"
    messages._check_body(text)
    with tx(conn):
        n = _bump(conn, item) if status == "failed" and item else 0
        activity.record(conn, m["to_bot"], "task.finish",
                        f"{message_id}: {status}", str(message_id))
    mid = messages.send(conn, m["to_bot"], m["from_bot"],
                        f"Result: {item or message_id} {status}", text,
                        kind="result", in_reply_to=message_id,
                        dedupe_key=_result_key(message_id))
    if n == ESCALATE_AT:
        _escalate(conn, m, item)
    return mid


def _escalate(conn, m, item) -> None:
    detail = (f"{item} failed {ESCALATE_AT} times (task {m['id']}, worker "
              f"{m['to_bot']}). Look at the activity record for the task.finish "
              "entries, then fix the cause, reassign the item or tell the Owner.")
    if m["from_bot"] == ADMIN:
        activity.record(conn, SYSTEM, "task.escalate", detail, item)
        return
    messages.send(conn, SYSTEM, ADMIN, f"Task failed twice on {item}", detail,
                  kind="escalation", dedupe_key=f"task.escalate:{item}")


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
        mid = send_task(conn, args.frm, args.to, args.item, args.goal,
                        args.inputs or [], args.outputs or [], args.check)
        if not mid:
            print("error: hop limit reached; not delivered, Admin was told",
                  file=sys.stderr)
            return 1
        print(mid)
        return 0
    return _run(go)


def _cmd_finish(args):
    def go(cfg, conn):
        m = conn.execute("SELECT to_bot FROM messages WHERE id=?",
                         (args.message_id,)).fetchone()
        actor = _actor()
        if m is not None and actor not in ("cli", m["to_bot"]):
            raise ValueError(f"task {args.message_id} was sent to {m['to_bot']}, "
                             f"not {actor}")
        mid = finish_task(cfg, conn, args.message_id, args.status, args.note or "")
        print(mid)
        return 0
    return _run(go)


def register(subparsers) -> None:
    me = _actor()
    t = subparsers.add_parser("task", help="tasks for workers")
    sub = t.add_subparsers(dest="task_command", required=True)
    s = sub.add_parser("send", help="send a task to a worker")
    s.add_argument("--to", required=True, help="a worker-tier bot")
    s.add_argument("--item", required=True)
    s.add_argument("--goal", required=True)
    s.add_argument("--input", dest="inputs", action="append",
                   help="a file the worker reads (repeat)")
    s.add_argument("--output", dest="outputs", action="append",
                   help="a file that must exist when done (repeat)")
    s.add_argument("--check", required=True, help="how to tell it is right")
    s.add_argument("--from", dest="frm", default=me)
    s.set_defaults(func=_cmd_send)
    f = sub.add_parser("finish", help="finish a task you were sent")
    f.add_argument("message_id", type=int)
    f.add_argument("--status", required=True, choices=STATUSES)
    f.add_argument("--note", default="")
    f.set_defaults(func=_cmd_finish)
