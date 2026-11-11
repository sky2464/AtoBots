"""Full-text search over knowledge, items, messages, decisions and bot Memory.

The index lives in search.db, apart from company.db, so a growing archive
cannot slow routine commands. It is a rebuildable cache: delete it and the
next reindex fills it again. Rows indexed through index_doc stay in search.db
even after the source row leaves company.db."""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from . import activity, cliutil
from .config import Config
from .db import connect, ct_display, tx, utcnow

KINDS = ("knowledge", "item", "message", "memory", "transcript", "decision")
SNIPPET_CHARS = 300
_BOT_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
  title, body, kind UNINDEXED, ref UNINDEXED, bot UNINDEXED, when_at UNINDEXED);
CREATE TABLE IF NOT EXISTS docmap(
  kind TEXT, ref TEXT, rid INTEGER, sha TEXT, PRIMARY KEY(kind, ref));
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, kind TEXT);
CREATE TABLE IF NOT EXISTS smeta(key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Hit:
    kind: str
    ref: str
    bot: str | None
    title: str
    snippet: str
    when: str | None


def _open(cfg: Config) -> sqlite3.Connection:
    cfg.home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.search_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO smeta VALUES('token', ?)",
                 (uuid.uuid4().hex,))
    return conn


def _index(sc, kind, ref, bot, title, body, when) -> bool:
    """Insert or replace one document. Returns False if nothing changed."""
    sha = hashlib.sha256(
        "\0".join((bot or "", title, body, when or "")).encode()).hexdigest()
    old = sc.execute("SELECT rid, sha FROM docmap WHERE kind=? AND ref=?",
                     (kind, ref)).fetchone()
    if old and old["sha"] == sha:
        return False
    with tx(sc):
        if old:
            sc.execute("DELETE FROM docs WHERE rowid=?", (old["rid"],))
        cur = sc.execute(
            "INSERT INTO docs(title, body, kind, ref, bot, when_at)"
            " VALUES (?,?,?,?,?,?)", (title, body, kind, ref, bot or "", when))
        sc.execute("INSERT OR REPLACE INTO docmap VALUES (?,?,?,?)",
                   (kind, ref, cur.lastrowid, sha))
    return True


def _check_kind(kind) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")


def index_doc(cfg: Config, kind: str, ref: str, bot: str | None, title: str,
              body: str, when: str | None = None) -> None:
    """Add a document, replacing any earlier one with the same kind and ref."""
    _check_kind(kind)
    sc = _open(cfg)
    try:
        _index(sc, kind, str(ref), bot, title or "", body or "",
               when or utcnow())
    finally:
        sc.close()


def index_docs(cfg: Config, docs) -> int:
    """index_doc for many (kind, ref, bot, title, body, when) tuples, opening
    the index once. Returns how many documents changed."""
    sc = _open(cfg)
    n = 0
    try:
        for kind, ref, bot, title, body, when in docs:
            _check_kind(kind)
            n += _index(sc, kind, str(ref), bot, title or "", body or "",
                        when or utcnow())
    finally:
        sc.close()
    return n


# ---- files ----

def _iso(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc).isoformat(
        timespec="seconds")


def _candidates(cfg: Config):
    """(relative path, kind, bot) for each file that belongs in the index."""
    if cfg.knowledge_dir.is_dir():
        for p in sorted(cfg.knowledge_dir.rglob("*.md")):
            rel = p.relative_to(cfg.home)
            if any(part.startswith(".") for part in rel.parts):
                continue
            bot = rel.parts[1] if len(rel.parts) > 2 else None
            yield rel, "knowledge", bot
    if cfg.bots_dir.is_dir():
        for p in sorted(cfg.bots_dir.glob("*/MEMORY.md")):
            yield p.relative_to(cfg.home), "memory", p.parent.name


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
        if line.strip():
            break
    return fallback


def reindex_files(cfg: Config) -> int:
    """Re-read only files whose mtime or size changed. Returns how many were
    indexed. Files that vanished are dropped from the index."""
    sc = _open(cfg)
    n = 0
    try:
        seen = set()
        for rel, kind, bot in _candidates(cfg):
            p = cfg.home / rel
            if p.is_symlink() or not p.is_file():
                continue
            key = rel.as_posix()
            seen.add(key)
            st = p.stat()
            old = sc.execute("SELECT mtime_ns, size FROM files WHERE path=?",
                             (key,)).fetchone()
            if old and (old["mtime_ns"], old["size"]) == (st.st_mtime_ns, st.st_size):
                continue
            text = p.read_text(errors="replace")
            _index(sc, kind, key, bot, _title_of(text, p.stem), text,
                   _iso(st.st_mtime_ns))
            sc.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                       (key, st.st_mtime_ns, st.st_size, kind))
            n += 1
        for r in sc.execute("SELECT path, kind FROM files").fetchall():
            if r["path"] in seen:
                continue
            m = sc.execute("SELECT rid FROM docmap WHERE kind=? AND ref=?",
                           (r["kind"], r["path"])).fetchone()
            with tx(sc):
                if m:
                    sc.execute("DELETE FROM docs WHERE rowid=?", (m["rid"],))
                    sc.execute("DELETE FROM docmap WHERE kind=? AND ref=?",
                               (r["kind"], r["path"]))
                sc.execute("DELETE FROM files WHERE path=?", (r["path"],))
    finally:
        sc.close()
    return n


# ---- records ----

def _wm(conn, key, default):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def _set_wm(conn, key, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                 (key, str(value)))


def reindex_records(cfg: Config, conn: sqlite3.Connection) -> int:
    """Index items, messages and decisions added or changed since the last
    call (watermarks live in company.db `meta`). Returns how many documents
    changed. If search.db was deleted, the watermarks start over."""
    sc = _open(cfg)
    n = 0
    try:
        token = sc.execute("SELECT value FROM smeta WHERE key='token'"
                           ).fetchone()["value"]
        if _wm(conn, "search.token", None) != token:
            for k in ("items", "messages", "decisions"):
                conn.execute("DELETE FROM meta WHERE key=?", (f"search.{k}",))
            _set_wm(conn, "search.token", token)
            # a fresh index: bring back what has left company.db
            from . import archive
            archive.reindex_archive(cfg)
        wm = _wm(conn, "search.items", "")
        rows = conn.execute(
            "SELECT * FROM items WHERE id IS NOT NULL AND updated_at >= ?"
            " ORDER BY updated_at", (wm,)).fetchall()
        for r in rows:
            body = "\n".join(x for x in (r["note"], r["status"], r["waiting_on"])
                             if x)
            n += _index(sc, "item", r["id"], r["owner"], r["title"] or "", body,
                        r["updated_at"])
        if rows:
            _set_wm(conn, "search.items", rows[-1]["updated_at"])
        wm = int(_wm(conn, "search.messages", 0))
        rows = conn.execute("SELECT * FROM messages WHERE id > ? ORDER BY id",
                            (wm,)).fetchall()
        for r in rows:
            n += _index(sc, "message", str(r["id"]), r["from_bot"],
                        r["subject"] or "", r["body"] or "", r["created_at"])
        if rows:
            _set_wm(conn, "search.messages", rows[-1]["id"])
        wm = int(_wm(conn, "search.decisions", 0))
        rows = conn.execute(
            "SELECT * FROM decisions WHERE id IS NOT NULL AND n > ? ORDER BY n",
            (wm,)).fetchall()
        for r in rows:
            n += _index(sc, "decision", r["id"], r["recorded_by"],
                        r["title"] or "", r["quote"] or "", r["created_at"])
        if rows:
            _set_wm(conn, "search.decisions", rows[-1]["n"])
    finally:
        sc.close()
    return n


# ---- query ----

def _match_expr(query: str) -> str | None:
    terms = re.findall(r"\w+", query or "")
    if not terms:
        return None
    return " ".join('"' + t + '"' for t in terms)


def search(cfg: Config, query: str, kind: str | None = None,
           bot: str | None = None, limit: int = 10) -> list[Hit]:
    if kind is not None:
        _check_kind(kind)
    expr = _match_expr(query)
    if expr is None or not cfg.search_path.exists():
        return []
    sql = ("SELECT kind, ref, bot, title, when_at,"
           " snippet(docs, 1, '', '', '...', 40) AS snip FROM docs"
           " WHERE docs MATCH ?")
    args: list = [expr]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if bot:
        sql += " AND bot=?"
        args.append(bot)
    sql += " ORDER BY rank LIMIT ?"
    args.append(max(1, int(limit)))
    sc = _open(cfg)
    try:
        rows = sc.execute(sql, args).fetchall()
    finally:
        sc.close()
    return [Hit(r["kind"], r["ref"], r["bot"] or None, r["title"],
                " ".join(r["snip"].split())[:SNIPPET_CHARS], r["when_at"])
            for r in rows]


# ---- notes ----

def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-")
    if not slug:
        raise ValueError("title needs at least one letter or digit")
    return slug


def add_note(cfg: Config, conn: sqlite3.Connection, bot: str, title: str,
             body: str) -> str:
    """File a note at knowledge/<bot>/<slug>.md and index it. Returns the
    path relative to the company home. Never overwrites an existing note."""
    if not _BOT_RE.match(bot or ""):
        raise ValueError(f"bad bot name {bot!r}")
    if not title or not title.strip():
        raise ValueError("title is required")
    slug = slugify(title)
    d = cfg.knowledge_dir / bot
    d.mkdir(parents=True, exist_ok=True)
    text = f"# {title.strip()}\n\n{body.strip()}\n"
    n = 1
    while True:
        name = slug if n == 1 else f"{slug}-{n}"
        try:
            with open(d / f"{name}.md", "x") as f:
                f.write(text)
            break
        except FileExistsError:
            n += 1
    rel = f"knowledge/{bot}/{name}.md"
    with tx(conn):
        activity.record(conn, bot, "note.add", f"{rel}: {title.strip()}", rel)
    reindex_files(cfg)
    return rel


# ---- CLI ----

def _run(fn):
    return cliutil.run(fn)


def _cmd_search(args):
    def go(cfg, conn):
        reindex_files(cfg)
        reindex_records(cfg, conn)
        hits = search(cfg, args.query, kind=args.kind, bot=args.bot,
                      limit=args.limit)
        for h in hits:
            when = ct_display(h.when) if h.when else "-"
            print(f"{h.kind}\t{h.ref}\t{h.bot or '-'}\t{when}\t{h.title}")
            print(f"    {h.snippet}")
        return 0
    return _run(go)


def _cmd_note_add(args):
    def go(cfg, conn):
        with open(args.body_file) as f:
            body = f.read()
        print(add_note(cfg, conn, os.environ.get("CB_BOT", "cli"), args.title,
                       body))
        return 0
    return _run(go)


def register(subparsers) -> None:
    s = subparsers.add_parser("search", help="search notes, items, messages, "
                              "decisions and Memory")
    s.add_argument("query")
    s.add_argument("--kind", choices=KINDS)
    s.add_argument("--bot")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=_cmd_search)
    n = subparsers.add_parser("note", help="company knowledge notes")
    nsub = n.add_subparsers(dest="note_command", required=True)
    a = nsub.add_parser("add", help="file a note under knowledge/<you>/")
    a.add_argument("title")
    a.add_argument("--body-file", required=True)
    a.set_defaults(func=_cmd_note_add)
