"""Token usage ledger and `cb usage` (cost control).

Each worker run is recorded from its JSON result (runner.run). Admin and chat
turns have no result object, so ingest_transcripts reads their assistant lines
from the session transcripts. Sessions the runner already recorded are skipped
there, so no turn is counted twice.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import connect, tx, utcnow

GROUPS = ("bot", "model", "trigger")
TRANSCRIPT_LANE = "chat"
OFFSET_KEY = "usage.offset."  # meta key + transcript file name
LAST_MSG_KEY = "usage.msg."   # meta key + transcript file name
_WINDOW_RE = re.compile(r"(\d{1,4})([hd])")


def record(conn: sqlite3.Connection, *, bot, model, lane, trigger, ref,
           input_tokens, cache_write, cache_read, output_tokens,
           at=None) -> int:
    cur = conn.execute(
        "INSERT INTO usage(at, bot, model, lane, trigger, ref, input_tokens,"
        " cache_write, cache_read, output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (at or utcnow(), bot, model, lane, trigger,
         None if ref is None else str(ref), _int(input_tokens),
         _int(cache_write), _int(cache_read), _int(output_tokens)))
    return cur.lastrowid


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _model_total(stats) -> int:
    if not isinstance(stats, dict):
        return 0
    return sum(_int(stats.get(k)) for k in (
        "inputTokens", "outputTokens", "cacheReadInputTokens",
        "cacheCreationInputTokens"))


def from_result(data) -> dict:
    """Usage from the claude JSON result. When modelUsage names several models
    (a run may call a helper model too), the served model is the one with the
    most tokens. None when the result has no modelUsage."""
    out = {"model": None, "input_tokens": 0, "cache_write": 0,
           "cache_read": 0, "output_tokens": 0}
    if not isinstance(data, dict):
        return out
    usage = data.get("usage")
    if isinstance(usage, dict):
        out["input_tokens"] = _int(usage.get("input_tokens"))
        out["cache_write"] = _int(usage.get("cache_creation_input_tokens"))
        out["cache_read"] = _int(usage.get("cache_read_input_tokens"))
        out["output_tokens"] = _int(usage.get("output_tokens"))
    served = data.get("modelUsage")
    if isinstance(served, dict) and served:
        out["model"] = str(max(served, key=lambda m: _model_total(served[m])))
    return out


# ---- transcripts ----

def project_dir(cfg: Config, bot: str) -> Path:
    """Claude Code's folder for a bot's sessions: the working directory with
    every non-alphanumeric character turned into a dash."""
    folder = re.sub(r"[^A-Za-z0-9]", "-", str(cfg.bots_dir / bot))
    return cfg.claude_projects_dir / folder


def _iso(text) -> str | None:
    if not isinstance(text, str):
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _turn(line: bytes):
    """(message id, usage row) for an assistant line with usage, else None.
    Sidechain (subagent) lines are not the bot's own turns."""
    if not line.strip():
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if (not isinstance(d, dict) or d.get("type") != "assistant"
            or d.get("isSidechain")):
        return None
    msg = d.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
        return None
    u = msg["usage"]
    model = msg.get("model")
    return msg.get("id"), {
        "at": _iso(d.get("timestamp")),
        "model": model if isinstance(model, str) else None,
        "input_tokens": _int(u.get("input_tokens")),
        "cache_write": _int(u.get("cache_creation_input_tokens")),
        "cache_read": _int(u.get("cache_read_input_tokens")),
        "output_tokens": _int(u.get("output_tokens")),
    }


def _meta_get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _session_lane(conn, session_id: str) -> str:
    row = conn.execute(
        "SELECT lane FROM runs WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,)).fetchone()
    if row and row["lane"]:
        return row["lane"]
    return TRANSCRIPT_LANE


def _ingest_file(conn, path: Path, bot: str) -> int:
    """Caller holds the transaction: the stored offset is read and written
    under the same lock, so two concurrent calls cannot both insert."""
    name = path.name
    start_text = _meta_get(conn, OFFSET_KEY + name)
    start = int(start_text) if start_text and start_text.isdigit() else 0
    prev_id = _meta_get(conn, LAST_MSG_KEY + name)
    if path.stat().st_size < start:  # truncated or replaced: start over
        start, prev_id = 0, None
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b"\n") + 1  # only complete lines; a partial one waits
    if end == 0:
        return 0
    rows = []
    last_id = prev_id
    for line in data[:end].split(b"\n"):
        turn = _turn(line)
        if turn is None:
            continue
        msg_id, row = turn
        # One reply written as several lines repeats its usage on each line.
        # Count it once: skip a line whose message id is the last one counted,
        # including one counted in an earlier pass.
        if msg_id is not None and msg_id == last_id:
            continue
        last_id = msg_id if msg_id is not None else last_id
        rows.append(row)
    lane = _session_lane(conn, path.stem)
    for row in rows:
        record(conn, bot=bot, lane=lane, trigger="session",
               ref=path.stem, **row)
    conn.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key)"
                 " DO UPDATE SET value=excluded.value",
                 (OFFSET_KEY + name, str(start + end)))
    if last_id is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key)"
                     " DO UPDATE SET value=excluded.value",
                     (LAST_MSG_KEY + name, last_id))
    else:
        conn.execute("DELETE FROM meta WHERE key=?", (LAST_MSG_KEY + name,))
    return len(rows)


def ingest_transcripts(cfg: Config, conn: sqlite3.Connection,
                       bot: str = "admin") -> int:
    """Record new assistant turns from the bot's session transcripts. Returns
    how many rows were added. Sessions the runner recorded are skipped. The
    whole pass runs in one immediate transaction."""
    folder = project_dir(cfg, bot)
    if not folder.is_dir() or folder.is_symlink():
        return 0
    count = 0
    with tx(conn):
        recorded = {r[0] for r in conn.execute(
            "SELECT session_id FROM runs WHERE session_id IS NOT NULL"
            " AND COALESCE(lane,'') NOT IN ('chat','live')")}
        for path in sorted(folder.glob("*.jsonl")):
            if (path.is_symlink() or not path.is_file()
                    or path.stem in recorded):
                continue
            count += _ingest_file(conn, path, bot)
    return count


# ---- report ----

def _window(text: str) -> timedelta:
    m = _WINDOW_RE.fullmatch(text or "")
    if not m:
        raise ValueError(f"--since must look like 24h or 7d, not {text!r}")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)


def window_arg(text: str) -> str:
    """argparse type for --since: returns the text when it is valid."""
    try:
        _window(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return text


def _rate(cache_read, inp, cache_write) -> str:
    denom = inp + cache_write + cache_read
    return f"{100 * cache_read / denom:.1f}%" if denom else "-"


def _table(conn, since_iso: str, by: str) -> list[str]:
    key = {"bot": "bot", "trigger": "trigger",
           "model": "COALESCE(model, '(unknown)')"}[by]
    rows = conn.execute(
        f"SELECT {key} AS k, COUNT(*) AS calls, SUM(input_tokens) AS inp,"
        " SUM(cache_write) AS cw, SUM(cache_read) AS cr,"
        " SUM(output_tokens) AS outp FROM usage WHERE at >= ?"
        " GROUP BY k ORDER BY k", (since_iso,)).fetchall()
    head = (f"{by:<24}{'calls':>7}{'input':>12}{'cache write':>14}"
            f"{'cache read':>14}{'output':>10}{'cache hit':>11}")
    lines = [head]
    total = [0, 0, 0, 0, 0]
    for r in rows:
        inp, cw, cr, outp = (r["inp"] or 0, r["cw"] or 0, r["cr"] or 0,
                             r["outp"] or 0)
        total = [t + v for t, v in zip(total, (r["calls"], inp, cw, cr, outp))]
        lines.append(f"{str(r['k']):<24}{r['calls']:>7}{inp:>12}{cw:>14}"
                     f"{cr:>14}{outp:>10}{_rate(cr, inp, cw):>11}")
    calls, inp, cw, cr, outp = total
    lines.append(f"{'TOTAL':<24}{calls:>7}{inp:>12}{cw:>14}{cr:>14}{outp:>10}"
                 f"{_rate(cr, inp, cw):>11}")
    return lines


def _mismatches(conn, since_iso: str) -> list[str]:
    """Calls whose served model is neither the bot's configured model (e.g.
    configured sonnet, served claude-opus-...) nor a deliberate lower model
    from runner.LEVEL_MODEL (housekeeping runs on haiku on purpose)."""
    from . import runner  # local: runner imports this module
    expected = set(runner.LEVEL_MODEL.values())
    rows = conn.execute(
        "SELECT u.bot AS bot, u.model AS model, b.model AS configured,"
        " COUNT(*) AS n FROM usage u LEFT JOIN bots b ON b.name = u.bot"
        " WHERE u.at >= ? AND u.model IS NOT NULL"
        " GROUP BY u.bot, u.model ORDER BY u.bot, u.model",
        (since_iso,)).fetchall()
    out = []
    for r in rows:
        configured = (r["configured"] or "").lower()
        served = r["model"].lower()
        if not configured or configured in served:
            continue
        if any(m.lower() in served for m in expected):
            continue
        out.append(f"  {r['bot']}: served {r['model']} for {r['n']} call(s), "
                   f"configured {r['configured']}")
    return out


def _worker_gaps(conn) -> list[str]:
    from . import bots  # local: avoid import cycle at module load
    return [f"  {b}: {why}" for b, why in bots.thinkers_missing_worker(conn)]


def report(conn: sqlite3.Connection, since: str = "24h",
           by: str = "bot") -> str:
    if by not in GROUPS:
        raise ValueError(f"--by must be one of {', '.join(GROUPS)}")
    since_iso = (datetime.now(timezone.utc) - _window(since)).isoformat(
        timespec="seconds")
    lines = [f"Token use in the last {since}, by {by}:", ""]
    lines += _table(conn, since_iso, by)
    mismatches = _mismatches(conn, since_iso)
    if mismatches:
        lines += ["", "Model mismatch (the served model is not the configured one;"
                  " a mismatch does not fail a run):"] + mismatches
    gaps = _worker_gaps(conn)
    if gaps:
        lines += ["", "Thinker bots with no active worker (routine work runs on"
                  " the thinker until fixed):"] + gaps
    return "\n".join(lines)


# ---- command ----

def _cmd_usage(args) -> int:
    try:
        cfg = Config.load()
        conn = connect(cfg)
        try:
            ingest_transcripts(cfg, conn)
            print(report(conn, since=args.since, by=args.by))
        finally:
            conn.close()
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "usage", help="token use: totals, cache hit rate, model check")
    p.add_argument("--since", default="24h", type=window_arg,
                   help="window, e.g. 24h or 7d (default 24h)")
    p.add_argument("--by", default="bot", choices=GROUPS)
    p.set_defaults(func=_cmd_usage)
