"""Daily digest of company changes and health (cost control & closed loops)."""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import activity, doctor, items, requests, usage
from .config import Config
from .db import connect, ct_display, tx, utcnow

DIGEST_LAST_KEY = "digest.last_sent"
DIGEST_MAX_CHARS = 3500
DIGEST_CUT_SUFFIX = "\n... more: cb digest"


def build_digest(cfg: Config, conn: sqlite3.Connection,
                 since: str | None = None,
                 now: str | None = None) -> tuple[str | None, str]:
    """Build a human-readable digest of what changed since `since` (or last digest).
    Returns (digest_text, since_used). If nothing changed, digest_text is None.
    """
    now = now or utcnow()
    if since is None:
        r = conn.execute("SELECT value FROM meta WHERE key=?",
                         (DIGEST_LAST_KEY,)).fetchone()
        if r and r["value"]:
            since = r["value"]
        else:
            now_dt = datetime.fromisoformat(now)
            since = (now_dt - timedelta(hours=24)).isoformat(timespec="seconds")

    sections: list[tuple[str, list[str]]] = []
    has_changes = False

    # 1. Items closed since
    done_items = []
    for a in activity.since(conn, since):
        if a["kind"] == "item.status" and (a["detail"] or "").endswith("-> done"):
            ref = a["ref"]
            row = conn.execute("SELECT title, owner FROM items WHERE id=?", (ref,)).fetchone()
            title = row["title"] if row else ""
            done_items.append(f"{ref} {title}".strip())
    if done_items:
        has_changes = True
        sections.append(("Items closed", done_items))

    # 2. Items opened since
    new_items = []
    for a in activity.since(conn, since):
        if a["kind"] == "item.new":
            ref = a["ref"]
            row = conn.execute("SELECT title, owner, status FROM items WHERE id=?", (ref,)).fetchone()
            title = row["title"] if row else a["detail"]
            new_items.append(f"{ref} {title}".strip())
    if new_items:
        has_changes = True
        sections.append(("Items opened", new_items))

    # 3. Items waiting on Owner
    waiting_owner = [f"{i['id']} [{i['status']}] {i['title']}"
                     for i in items.open_items(conn) if i["owner_waiting"]]
    if waiting_owner:
        sections.append(("Waiting on Owner", waiting_owner))

    # 4. Requests still unhandled (especially >24h)
    unhandled_reqs = []
    cutoff_24h = (datetime.fromisoformat(now) - timedelta(hours=24)).isoformat(timespec="seconds")
    for r in requests.unhandled(conn):
        is_over_24h = (r["received_at"] < cutoff_24h)
        flag = " (>24h)" if is_over_24h else ""
        via = f" (via {r['source']})" if r["source"] else ""
        unhandled_reqs.append(f"R-{r['id']}{flag} {ct_display(r['received_at'])}{via}: {r['text'][:120]}")
    if unhandled_reqs:
        sections.append(("Unhandled requests", unhandled_reqs))

    # 5. Decisions recorded since
    dec_rows = conn.execute(
        "SELECT id, title, quote FROM decisions WHERE created_at >= ? ORDER BY id",
        (since,)).fetchall()
    if dec_rows:
        has_changes = True
        sections.append(("Decisions recorded",
                         [f"{d['id']} {d['title']}: {d['quote'][:100]}" for d in dec_rows]))

    # 6. Changes made (table changes if present - from Spec E3)
    table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "changes" in table_names:
        ch_rows = conn.execute(
            "SELECT id, title, status FROM changes"
            " WHERE COALESCE(completed_at, approved_at, created_at) >= ?"
            " ORDER BY id",
            (since,)).fetchall()
        if ch_rows:
            has_changes = True
            sections.append(("Changes made",
                             [f"{c['id']} [{c['status']}] {c['title']}" for c in ch_rows]))

    # 7. Health findings
    try:
        findings = doctor.checks(cfg, conn=conn, active_fn=lambda: True, track=False)
    except Exception as e:  # noqa: BLE001  # the digest still goes out
        has_changes = True  # a broken check is news even on a quiet day
        sections.append(("Health findings",
                         [f"[doctor] the health check itself failed "
                          f"({type(e).__name__}: {e}); run cb doctor"]))
        findings = []
    if findings:
        sections.append(("Health findings", [f"[{f.key}] {f.text}" for f in findings]))

    # 8. Failed runs since
    failed_runs = conn.execute(
        "SELECT id, bot, reason, ended_at FROM runs WHERE ended_at >= ? AND reason != 'ok'"
        " ORDER BY id", (since,)).fetchall()
    if failed_runs:
        has_changes = True
        sections.append(("Failed runs",
                         [f"run {r['id']} ({r['bot']}): {r['reason']}" for r in failed_runs]))

    # 9. Usage for the period
    u_row = conn.execute(
        "SELECT COUNT(*) AS calls, SUM(input_tokens) AS inp, SUM(cache_write) AS cw,"
        " SUM(cache_read) AS cr, SUM(output_tokens) AS outp FROM usage WHERE at >= ?",
        (since,)).fetchone()
    calls = u_row["calls"] or 0
    if calls > 0:
        has_changes = True
        inp = u_row["inp"] or 0
        cw = u_row["cw"] or 0
        cr = u_row["cr"] or 0
        outp = u_row["outp"] or 0
        tot = inp + cw + cr + outp
        denom = inp + cw + cr
        rate = f"{100 * cr / denom:.1f}%" if denom else "-"
        sections.append(("Usage", [f"{calls} call(s), {tot} tokens (cache hit {rate})"]))

    if not has_changes and not waiting_owner and not unhandled_reqs and not findings:
        return None, since

    lines = [f"# AtoBots Digest ({ct_display(since)} - {ct_display(now)})\n"]
    for title, items_list in sections:
        lines.append(f"## {title}")
        for it in items_list:
            lines.append(f"- {it}")
        lines.append("")

    full_text = "\n".join(lines).strip()
    return full_text, since


def format_for_telegram(text: str) -> str:
    """Cap at DIGEST_MAX_CHARS (3500) and append 'more: cb digest' if cut."""
    if len(text) <= DIGEST_MAX_CHARS:
        return text
    cut_limit = DIGEST_MAX_CHARS - len(DIGEST_CUT_SUFFIX)
    return text[:cut_limit] + DIGEST_CUT_SUFFIX


def _cmd_digest(args):
    try:
        cfg = Config.load()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    conn = connect(cfg)
    try:
        text, _ = build_digest(cfg, conn, since=args.since)
        if text is None:
            print("Nothing changed since last digest.")
        else:
            print(text)
        return 0
    finally:
        conn.close()


def register(subparsers) -> None:
    p = subparsers.add_parser("digest", help="what changed since last digest")
    p.add_argument("--since", help="ISO 8601 timestamp to compute digest from")
    p.set_defaults(func=_cmd_digest)
