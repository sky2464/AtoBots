"""Tasks for workers. A thinker sends a worker a `task` (goal, inputs, outputs,
check). The worker finishes it with `cb task finish`; the outcome goes back to
the sender as a `result`. Tasks are ordinary messages, so the hop limit and the
queue apply to them as to any other message.

Finishing is recorded in the same transaction as the item's failure count, so
a task counts as finished even when the result cannot be delivered. Failures
on an item count consecutively: a successful `done` resets them. At
ESCALATE_AT failures Admin is told once, and the dispatcher stops waking the
worker for that item (see dispatcher._wake_assigned).
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import activity, bots, items, messages
from .config import Config
from .db import connect, tx

ADMIN = "admin"
SYSTEM = "system"
ESCALATE_AT = 2
STATUSES = ("done", "failed")
_FAIL_PREFIX = "task.fail."
_FINISHED_PREFIX = "task.finished."


def _fail_key(item_id: str) -> str:
    return _FAIL_PREFIX + item_id


def _finished_key(message_id: int) -> str:
    return f"{_FINISHED_PREFIX}{message_id}"


def _result_key(message_id: int) -> str:
    return f"result:{message_id}"


def failures(conn: sqlite3.Connection, item_id: str) -> int:
    r = conn.execute("SELECT value FROM meta WHERE key=?",
                     (_fail_key(item_id),)).fetchone()
    return int(r["value"]) if r else 0


def finished(conn: sqlite3.Connection, message_id: int) -> bool:
    return conn.execute("SELECT 1 FROM meta WHERE key=?",
                        (_finished_key(message_id),)).fetchone() is not None


def _parse(body: str) -> dict:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _paths(value, label: str) -> list:
    """Paths relative to the company home, inside it: a list of non-empty text
    entries, no absolute path, no `..`, and not the home itself."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of paths")
    for p in value:
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"{label} entries must be non-empty text paths")
        path = Path(p)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"{label} path must be relative to the company "
                             f"home and inside it: {p!r}")
    return list(value)


def send_task(conn: sqlite3.Connection, frm: str, to: str, item_id: str,
              goal: str, inputs, outputs, check: str, *,
              cfg: Config | None = None) -> int:
    """Send a task to a worker-tier bot. Returns the message id (0 if the hop
    limit stopped it). `cfg` gives the hop limit and chain window (defaults
    when not given)."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal is required")
    inputs = _paths(inputs, "inputs")
    outputs = _paths(outputs, "outputs")
    if bots.get(conn, to).tier != "worker":
        raise ValueError(f"{to} is not a worker: tasks go to worker-tier bots")
    items.get(conn, item_id)
    body = json.dumps({"goal": goal, "inputs": inputs, "outputs": outputs,
                       "check": check or ""}, ensure_ascii=False)
    return messages.send(
        conn, frm, to, f"Task {item_id}: {goal[:80]}", body, item_id=item_id,
        kind="task",
        hop_limit=cfg.hop_limit if cfg else Config.hop_limit,
        chain_window_s=cfg.chain_window_s if cfg else Config.chain_window_s)


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
    """Body of the wake sent to a worker for an item. When an open task for
    that worker is waiting (not yet finished), the wake only points at it: the
    task itself is already queued to the worker and is shown in its packet.
    Otherwise `fallback`."""
    rows = conn.execute(
        "SELECT id FROM messages WHERE kind='task' AND item_id=? AND to_bot=?"
        " AND status IN ('queued','claimed') ORDER BY id DESC",
        (item_id, to)).fetchall()
    open_ids = [r["id"] for r in rows if not finished(conn, r["id"])]
    if not open_ids:
        return fallback
    mid = open_ids[0]
    return (f"A task is waiting for you on {item_id}: message {mid}. Work it, "
            f"then run cb task finish {mid} --status done|failed --note "
            "\"what you did\".")


def _missing(cfg: Config, body: str) -> list:
    """Outputs that do not exist inside the company home. An entry that is not
    text, is absolute, escapes the home (also through a symlink) or is not
    there counts as missing."""
    outs = _parse(body).get("outputs") or []
    if not isinstance(outs, list):
        return ["outputs"]
    home = cfg.home.resolve()
    missing = []
    for p in outs:
        if not isinstance(p, str) or not p.strip():
            missing.append(str(p))
            continue
        try:
            target = (home / p).resolve()
            ok = (target != home and target.is_relative_to(home)
                  and target.exists())
        except (OSError, ValueError, RuntimeError):
            ok = False
        if not ok:
            missing.append(p)
    return missing


def _bump(conn: sqlite3.Connection, item_id: str) -> int:
    n = failures(conn, item_id) + 1
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                 (_fail_key(item_id), str(n)))
    return n


def finish_task(cfg: Config, conn: sqlite3.Connection, message_id: int,
                status: str, note: str) -> int:
    """Settle a task once. `done` is checked: every output must exist inside
    cfg.home, or the task is failed. The finish, its failure count (or reset)
    and the activity row are one transaction, so the task counts as finished
    even if the result cannot be delivered. Returns the result's message id,
    or 0 when it was not delivered (then `task.result_unsent` is recorded)."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    m = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if m is None:
        raise KeyError(f"message {message_id}")
    if m["kind"] != "task":
        raise ValueError(f"message {message_id} is not a task")
    item = m["item_id"]
    missing = _missing(cfg, m["body"]) if status == "done" else []
    if missing:
        status = "failed"
    text = f"Task {message_id} on {item or '-'} finished: {status}."
    if missing:
        text += " Missing outputs: " + ", ".join(missing) + "."
    if note:
        text += f"\n\nWorker note: {note}"
    messages._check_body(text)
    with tx(conn):
        if finished(conn, message_id):
            raise ValueError(f"task {message_id} is already finished")
        conn.execute("INSERT INTO meta(key, value) VALUES (?,?)",
                     (_finished_key(message_id), status))
        if item and status == "failed":
            _bump(conn, item)
        elif item:
            conn.execute("DELETE FROM meta WHERE key=?", (_fail_key(item),))
        activity.record(conn, m["to_bot"], "task.finish",
                        f"{message_id}: {status}", str(message_id))
    sent, reason = 0, None
    try:
        sent = messages.send(
            conn, m["to_bot"], m["from_bot"],
            f"Result: {item or message_id} {status}", text, kind="result",
            in_reply_to=message_id, dedupe_key=_result_key(message_id),
            hop_limit=cfg.hop_limit, chain_window_s=cfg.chain_window_s)
        if not sent:
            reason = "hop limit reached"
    except (ValueError, KeyError) as e:
        reason = str(e)
    if reason:
        activity.record(conn, SYSTEM, "task.result_unsent",
                        f"task {message_id}: {reason}", str(message_id))
    if item and status == "failed" and failures(conn, item) >= ESCALATE_AT:
        _escalate(conn, m, item)
    return sent


def _escalate(conn, m, item) -> None:
    """Tell Admin once per item. A sender that is Admin gets the activity
    record instead of a message."""
    detail = (f"{item} failed {ESCALATE_AT} times (task {m['id']}, worker "
              f"{m['to_bot']}). Look at the activity record for the task.finish "
              "entries, then fix the cause, reassign the item or tell the Owner.")
    if m["from_bot"] == ADMIN:
        if not conn.execute("SELECT 1 FROM activity WHERE kind='task.escalate'"
                            " AND ref=?", (item,)).fetchone():
            activity.record(conn, SYSTEM, "task.escalate", detail, item)
        return
    try:
        messages.send(conn, SYSTEM, ADMIN, f"Task failed twice on {item}",
                      detail, kind="escalation",
                      dedupe_key=f"task.escalate:{item}")
    except (ValueError, KeyError) as e:
        activity.record(conn, SYSTEM, "task.escalate_unsent", f"{item}: {e}",
                        item)


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
                        args.inputs or [], args.outputs or [], args.check,
                        cfg=cfg)
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
        sent = finish_task(cfg, conn, args.message_id, args.status,
                           args.note or "")
        print(sent)
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
                   help="a file the worker reads, relative to the company home (repeat)")
    s.add_argument("--output", dest="outputs", action="append",
                   help="a file that must exist when done, relative to the company home (repeat)")
    s.add_argument("--check", required=True, help="how to tell it is right")
    s.add_argument("--from", dest="frm", default=me)
    s.set_defaults(func=_cmd_send)
    f = sub.add_parser("finish", help="finish a task you were sent")
    f.add_argument("message_id", type=int)
    f.add_argument("--status", required=True, choices=STATUSES)
    f.add_argument("--note", default="")
    f.set_defaults(func=_cmd_finish)
