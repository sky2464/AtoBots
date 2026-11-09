"""Append-only activity record. UPDATE and DELETE are blocked by triggers."""
import sqlite3

from .db import utcnow


def record(conn: sqlite3.Connection, actor: str, kind: str, detail: str,
           ref: str | None = None) -> None:
    conn.execute(
        "INSERT INTO activity(at, actor, kind, detail, ref) VALUES (?,?,?,?,?)",
        (utcnow(), actor, kind, detail, ref),
    )


def since(conn: sqlite3.Connection, iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM activity WHERE at >= ? ORDER BY id", (iso,)
    ).fetchall()
