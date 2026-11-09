"""Archiving: plain scripts, no AI. Nothing is removed until its copy has been
written, flushed to disk, re-read and compared with the original.

* Transcripts: after each run, the session file Claude Code wrote moves to
  archive/YYYY-MM/<bot>-<run_id>.jsonl.gz and its user/assistant text goes
  into the search index (kind `transcript`, capped at 64 KB).
* Records: once a day, closed items, messages, runs and Owner requests older
  than 30 days move to archive/YYYY-MM/records.jsonl.gz. Decisions and the
  activity record stay in company.db forever.
* Archived rows stay searchable: search.db keeps their index entries.
* Nothing here deletes archive files; permanent deletion is the Owner's word
  and a manual step."""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import activity, bots, messages, requests, search
from .config import Config
from .db import connect, tx, utcnow

TRANSCRIPT_INDEX_CAP = 64 * 1024
_CHUNK = 1 << 20
_SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
_CLOSED = "('done','parked')"
_NEVER_STOP = 10 ** 9
DAILY_KEY = "archive.last_daily"
DAILY_TRY_KEY = "archive.last_daily_try"
_lock = threading.Lock()


class ArchiveError(Exception):
    """A copy could not be written or did not verify. The original is intact."""


# ---- safe copying ----

def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _digest_gz(path: Path) -> str:
    h = hashlib.sha256()
    with gzip.open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                return h.hexdigest()
            h.update(b)


def _write_verified(dest: Path, chunks, h) -> None:
    """Write the chunks gzipped to dest (via a temp file), fsync, re-read and
    compare with `h` (the running sha256 of the chunks). Then rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with gzip.open(tmp, "wb") as gz:
            for c in chunks:
                h.update(c)
                gz.write(c)
        _fsync_path(tmp)
        if _digest_gz(tmp) != h.hexdigest():
            raise ArchiveError(f"copy of {dest.name} did not match the original")
        os.replace(tmp, dest)
        _fsync_path(dest.parent)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_chunks(path: Path):
    with open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                return
            yield b


# ---- transcripts ----

def find_transcript(cfg: Config, session_id) -> Path | None:
    """The session file for a recorded run, or None. Looks only for the exact
    session id, so the Owner's own sessions are never touched."""
    if not isinstance(session_id, str) or not _SID_RE.fullmatch(session_id):
        return None
    root = cfg.claude_projects_dir
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir()):
        p = d / f"{session_id}.jsonl"
        if (d.is_dir() and not d.is_symlink() and p.is_file()
                and not p.is_symlink()):
            return p
    return None


def _session_files(cfg: Config) -> dict:
    """session id -> transcript path for every session file now on disk."""
    out = {}
    root = cfg.claude_projects_dir
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not d.is_dir():
            continue
        for p in d.glob("*.jsonl"):
            if p.is_file() and not p.is_symlink() and _SID_RE.fullmatch(p.stem):
                out.setdefault(p.stem, p)
    return out


def _note(conn, kind, detail, ref) -> None:
    try:
        with tx(conn):
            activity.record(conn, "system", kind, detail, ref)
    except sqlite3.Error:
        pass


def _message_text(msg) -> list:
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b["text"] for b in content if isinstance(b, dict)
                and b.get("type") == "text" and isinstance(b.get("text"), str)]
    return []


def extract_text(gz_path: Path, cap: int = TRANSCRIPT_INDEX_CAP) -> str:
    """Text of user and assistant messages only (no tool input or output)."""
    out, size = [], 0
    with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") not in ("user", "assistant"):
                continue
            for t in _message_text(d.get("message")):
                out.append(t)
                size += len(t.encode()) + 1
            if size >= cap:
                break
    return "\n".join(out).encode()[:cap].decode("utf-8", errors="ignore")


def _transcript_failed(cfg, conn, run, err) -> None:
    run_id = run["id"]
    detail = f"run {run_id} ({run['bot']}): {type(err).__name__}: {err}"
    try:
        with tx(conn):
            activity.record(conn, "system", "archive.transcript_failed", detail,
                            str(run_id))
    except sqlite3.Error:
        pass
    try:
        messages.send(
            conn, "system", "admin", f"Archive failed for run {run_id}",
            f"The transcript of {run['bot']} run {run_id} could not be archived "
            f"({err}). The original was left in place and nothing was deleted. "
            "Check free disk space (df -h) and run `cb archive status`.",
            kind="note", dedupe_key=f"archive-fail:{run_id}",
            hop_limit=_NEVER_STOP)
    except (KeyError, ValueError, sqlite3.Error):
        pass  # no Admin to tell; the activity row above is the record


def archive_transcript(cfg: Config, conn: sqlite3.Connection,
                       run_id: int) -> Path | None:
    """Move a finished run's transcript into the archive. Returns the archive
    path, or None when the run left no transcript (recorded, not an error).

    Three separate steps: (1) copy, verified; a failure here tells Admin once
    and raises ArchiveError; (2) index; a failure raises ArchiveError but the
    copy is good and the daily sweep retries; (3) remove the original; a
    failure here is only logged (the copy is safe) and the sweep retries.
    The original is never removed before step 1 has verified."""
    with _lock:
        return _archive_transcript(cfg, conn, run_id)


def _archive_transcript(cfg, conn, run_id):
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"run {run_id}")
    src = find_transcript(cfg, run["session_id"])
    if src is None:
        _note(conn, "archive.transcript_missing",
              f"run {run_id} ({run['bot']}): no transcript found", str(run_id))
        return None
    month = (run["started_at"] or utcnow())[:7]
    dest = cfg.archive_dir / month / f"{run['bot']}-{run_id}.jsonl.gz"
    rel = dest.relative_to(cfg.home).as_posix()
    try:  # 1. copy, verified
        before = src.stat()
        have = False
        if dest.exists():  # left by an earlier attempt
            try:
                have = _digest_gz(dest) == _sha_file(src)
            except (OSError, EOFError):
                have = False
        if not have:
            _write_verified(dest, _read_chunks(src), hashlib.sha256())
            after = src.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size,
                                                        after.st_mtime_ns):
                dest.unlink()
                raise ArchiveError("transcript changed while it was being copied")
        sdir = src.parent / src.stem  # subagent files, if the session has any
        if sdir.is_dir() and not sdir.is_symlink():
            _archive_session_dir(sdir, dest.with_name(
                f"{run['bot']}-{run_id}.subagents.tar.gz"))
        else:
            sdir = None
    except (OSError, EOFError, tarfile.TarError, ArchiveError) as e:
        _transcript_failed(cfg, conn, run, e)
        raise ArchiveError(str(e)) from e
    try:  # 2. index
        search.index_doc(cfg, "transcript", rel, run["bot"],
                         f"{run['bot']} run {run_id}", extract_text(dest),
                         run["ended_at"] or run["started_at"])
    except (OSError, EOFError, ValueError, sqlite3.Error) as e:
        _note(conn, "archive.index_failed",
              f"run {run_id}: copy verified at {rel}, indexing failed: {e}",
              str(run_id))
        raise ArchiveError(f"copy verified, indexing failed (will retry): {e}"
                           ) from e
    try:  # 3. remove the originals (directory first: the session file is what
        # the retry sweep looks for)
        if sdir is not None:
            shutil.rmtree(sdir)
        os.unlink(src)
    except OSError as e:
        _note(conn, "archive.unlink_failed",
              f"run {run_id}: copy verified at {rel}; original not removed: {e}",
              str(run_id))
        return dest
    _note(conn, "archive.transcript", f"run {run_id} ({run['bot']}) -> {rel}",
          str(run_id))
    return dest


def _dir_manifest(root: Path) -> list:
    """(relative path, sha256 or None for a directory) for everything under
    root. Links and special files are refused, not followed."""
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink() or not (p.is_dir() or p.is_file()):
            raise ArchiveError(f"{rel}: not a plain file or directory")
        out.append((rel, None if p.is_dir() else _sha_file(p)))
    return sorted(out, key=lambda e: e[0])


def _tar_manifest(path: Path, top: str) -> list:
    out = []
    with tarfile.open(path, "r:gz") as t:
        for m in sorted(t.getmembers(), key=lambda m: m.name):
            if m.name == top:
                continue
            if not (m.isdir() or m.isfile()):
                raise ArchiveError(f"{m.name}: unexpected entry in archive")
            h = None
            if m.isfile():
                h = hashlib.sha256(t.extractfile(m).read()).hexdigest()
            out.append((m.name[len(top) + 1:], h))
    return sorted(out, key=lambda e: e[0])


def _archive_session_dir(src_dir: Path, dest: Path) -> None:
    """Tar+gzip the session's directory (subagent files) to dest and verify
    every entry against the source before dest is put in place."""
    top = src_dir.name
    before = _dir_manifest(src_dir)
    if dest.exists():
        # A tar from an earlier attempt is never replaced (the directory may
        # since have lost files it holds). Reuse it if it reads cleanly and
        # covers everything still in the directory.
        try:
            have = set(_tar_manifest(dest, top))
        except (OSError, EOFError, tarfile.TarError, ArchiveError) as e:
            raise ArchiveError(f"existing {dest.name} is unreadable: {e}")
        if not set(before) <= have:
            raise ArchiveError(f"existing {dest.name} lacks files now in "
                               f"{top}/; left alone")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with tarfile.open(tmp, "w:gz") as t:
            t.add(src_dir, arcname=top)
        _fsync_path(tmp)
        if _tar_manifest(tmp, top) != before or _dir_manifest(src_dir) != before:
            raise ArchiveError(f"copy of {dest.name} did not match the original")
        os.replace(tmp, dest)
        _fsync_path(dest.parent)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    for c in _read_chunks(path):
        h.update(c)
    return h.hexdigest()


def sweep_transcripts(cfg: Config, conn: sqlite3.Connection) -> int:
    """Retry every finished run of ours whose session file is still on disk
    (a failed copy, index or removal). Returns how many were archived."""
    n = 0
    for sid in sorted(_session_files(cfg)):
        run = conn.execute(
            "SELECT id FROM runs WHERE session_id=? AND ended_at IS NOT NULL"
            " ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if run is None:
            continue
        try:
            if archive_transcript(cfg, conn, run["id"]):
                n += 1
        except ArchiveError:
            pass  # already recorded; Admin was told once
    return n


# ---- records ----

def _is_autoinc(conn, table) -> bool:
    r = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                     (table,)).fetchone()
    return bool(r and "AUTOINCREMENT" in (r["sql"] or "").upper())


def _line(table, row) -> bytes:
    return (json.dumps({"table": table, "row": dict(row)}, sort_keys=True,
                       ensure_ascii=False) + "\n").encode()


class _Plan:
    def __init__(self):
        self.lines: dict = {}   # month -> [bytes]
        self.keys: dict = {}    # table -> [key tuples]

    def add(self, table, month, row, key):
        self.lines.setdefault(month, []).append(_line(table, row))
        self.keys.setdefault(table, []).append(key)

    def count(self, table):
        return len(self.keys.get(table, []))


def _plan_items(conn, cutoff, open_req_ids, plan) -> None:
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM items WHERE id IS NOT NULL AND archived_at IS NULL"
        f" AND status IN {_CLOSED} AND updated_at < ?", (cutoff,))}
    if open_req_ids:
        q = ",".join("?" * len(open_req_ids))
        for r in conn.execute(f"SELECT DISTINCT item_id FROM request_items"
                              f" WHERE request_id IN ({q})", tuple(open_req_ids)):
            rows.pop(r[0], None)
    children: dict = {}
    for r in conn.execute("SELECT id, parent FROM items WHERE parent IS NOT NULL"):
        children.setdefault(r["parent"], []).append(r["id"])
    changed = True
    while changed:  # a parent waits for every child still in the table
        changed = False
        for id in list(rows):
            if any(c not in rows for c in children.get(id, ())):
                del rows[id]
                changed = True
    for r in rows.values():
        plan.add("items", r["updated_at"][:7], r, (r["n"], r["updated_at"]))


def _plan_requests(conn, cutoff, open_req_ids, plan) -> None:
    rows = conn.execute(
        f"SELECT * FROM owner_requests r WHERE received_at < ?"
        f" AND EXISTS (SELECT 1 FROM request_items l WHERE l.request_id=r.id)"
        f" AND NOT EXISTS (SELECT 1 FROM request_items l JOIN items i"
        f"  ON i.id=l.item_id WHERE l.request_id=r.id"
        f"  AND (i.status NOT IN {_CLOSED} OR i.updated_at >= ?))",
        (cutoff, cutoff)).fetchall()
    protect = None if _is_autoinc(conn, "owner_requests") else conn.execute(
        "SELECT MAX(id) FROM owner_requests").fetchone()[0]
    for r in rows:
        if r["id"] in open_req_ids or r["id"] == protect:
            continue
        month = r["received_at"][:7]
        for l in conn.execute("SELECT * FROM request_items WHERE request_id=?",
                              (r["id"],)):
            plan.lines.setdefault(month, []).append(_line("request_items", l))
        plan.add("owner_requests", month, r, (r["id"],))


def _plan_simple(conn, plan, table, sql, args, month_col, skip=None) -> None:
    protect = None if _is_autoinc(conn, table) else conn.execute(
        f"SELECT MAX(id) FROM {table}").fetchone()[0]
    for r in conn.execute(sql, args):
        if r["id"] != protect and not (skip and skip(r)):
            plan.add(table, r[month_col][:7], r, (r["id"],))


def _append_month(cfg, month, new_lines) -> None:
    dest = cfg.archive_dir / month / "records.jsonl.gz"

    def chunks():
        if dest.exists():
            try:
                with gzip.open(dest, "rb") as f:
                    while True:
                        b = f.read(_CHUNK)
                        if not b:
                            break
                        yield b
            except (OSError, EOFError) as e:
                raise ArchiveError(f"existing {dest.name} is unreadable: {e}")
        yield b"".join(new_lines)

    _write_verified(dest, chunks(), hashlib.sha256())


def archive_old_records(cfg: Config, conn: sqlite3.Connection, now: str,
                        older_than_days: int = 30) -> dict:
    """Move closed records older than `older_than_days` out of company.db.
    Everything is indexed first, written and verified in the archive, and only
    then deleted. Returns how many rows of each kind moved."""
    search.reindex_records(cfg, conn)
    cutoff = (datetime.fromisoformat(now) - timedelta(days=older_than_days)
              ).isoformat(timespec="seconds")
    open_ids = {r["id"] for r in requests._open_requests(conn)}
    on_disk = _session_files(cfg)
    plan = _Plan()
    _plan_items(conn, cutoff, open_ids, plan)
    _plan_requests(conn, cutoff, open_ids, plan)
    _plan_simple(conn, plan, "messages",
                 "SELECT * FROM messages WHERE status='done'"
                 " AND COALESCE(done_at, created_at) < ?", (cutoff,),
                 "created_at")
    _plan_simple(conn, plan, "runs",
                 "SELECT * FROM runs WHERE ended_at IS NOT NULL AND ended_at < ?",
                 (cutoff,), "started_at",
                 skip=lambda r: r["session_id"] in on_disk)  # copy still pending
    result = {t: plan.count(t) for t in
              ("items", "messages", "runs", "owner_requests")}
    if not any(result.values()):
        return result
    try:
        for month, lines in sorted(plan.lines.items()):
            _append_month(cfg, month, lines)
    except OSError as e:
        raise ArchiveError(str(e)) from e
    moved = dict.fromkeys(result, 0)
    with tx(conn):
        for table, sql in (
                ("items", "DELETE FROM items WHERE n=? AND updated_at=?"
                          f" AND status IN {_CLOSED}"),
                ("messages", "DELETE FROM messages WHERE id=? AND status='done'"),
                ("runs", "DELETE FROM runs WHERE id=? AND ended_at IS NOT NULL")):
            moved[table] = conn.executemany(sql, plan.keys.get(table, [])
                                            ).rowcount
        for (rid,) in plan.keys.get("owner_requests", []):
            moved["owner_requests"] += conn.execute(
                f"DELETE FROM owner_requests WHERE id=? AND NOT EXISTS (SELECT 1"
                f" FROM request_items l JOIN items i ON i.id=l.item_id"
                f" WHERE l.request_id=? AND i.status NOT IN {_CLOSED})",
                (rid, rid)).rowcount
            conn.execute("DELETE FROM request_items WHERE request_id=? AND NOT"
                         " EXISTS (SELECT 1 FROM owner_requests WHERE id=?)",
                         (rid, rid))
        activity.record(conn, "system", "archive.records",
                        ", ".join(f"{k}: {v}" for k, v in moved.items()))
    return moved


# ---- Memory caps ----

def check_memory_caps(cfg: Config, conn: sqlite3.Connection) -> list[str]:
    """Ask each bot whose MEMORY.md is over the cap to condense it, with one
    low-cost housekeeping message. A bot that already has such a message
    waiting is not asked again."""
    sent = []
    for b in bots.list_bots(conn, "active"):
        try:
            size = (bots._bot_dir(cfg, b.name) / "MEMORY.md").stat().st_size
        except FileNotFoundError:
            continue
        if size <= cfg.memory_cap_bytes:
            continue
        if conn.execute(
                "SELECT 1 FROM messages WHERE to_bot=? AND kind='housekeeping'"
                " AND status IN ('queued','claimed')", (b.name,)).fetchone():
            continue
        messages.send(
            conn, "system", b.name, "Condense your Memory",
            f"Your MEMORY.md is {size} bytes; the cap is {cfg.memory_cap_bytes}. "
            "Rewrite it shorter: keep what you need at the start of every run, "
            "move detail into a knowledge note with `cb note add`, and drop "
            "what is no longer true. Do not delete anything else.",
            kind="housekeeping", level="housekeeping",
            dedupe_key=f"memcap:{b.name}:{utcnow()}:{size}",
            hop_limit=_NEVER_STOP)
        sent.append(f"{b.name}: Memory is {size} bytes (cap {cfg.memory_cap_bytes})")
    return sent


def daily(cfg: Config, conn: sqlite3.Connection, now: str) -> dict:
    """What the dispatcher runs once a day. Every step runs even if an earlier
    one fails; the first failure is raised at the end."""
    out, errors = {}, []
    for name, fn in (
            ("transcripts_retried", lambda: sweep_transcripts(cfg, conn)),
            ("records", lambda: archive_old_records(cfg, conn, now)),
            ("memory_notes", lambda: len(check_memory_caps(cfg, conn)))):
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if isinstance(r, dict):
            out.update(r)
        else:
            out[name] = r
    if errors:
        raise ArchiveError("; ".join(errors))
    return out


# ---- rebuilding the search index from the archive ----

def _archive_docs(cfg: Config):
    """Index documents for everything in archive/, tolerating duplicates (a
    later record of the same row wins)."""
    if not cfg.archive_dir.is_dir():
        return
    for f in sorted(cfg.archive_dir.glob("*/records.jsonl.gz")):
        try:
            with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                        t, r = d["table"], d["row"]
                        if t == "items" and r.get("id"):
                            body = "\n".join(x for x in (
                                r.get("note"), r.get("status"),
                                r.get("waiting_on")) if x)
                            yield ("item", r["id"], r.get("owner"),
                                   r.get("title") or "", body, r.get("updated_at"))
                        elif t == "messages":
                            yield ("message", str(r["id"]), r.get("from_bot"),
                                   r.get("subject") or "", r.get("body") or "",
                                   r.get("created_at"))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
        except (OSError, EOFError):
            continue
    for f in sorted(cfg.archive_dir.glob("*/*.jsonl.gz")):
        m = re.fullmatch(r"(.+)-(\d+)\.jsonl\.gz", f.name)
        if f.is_symlink() or not m:
            continue
        try:
            when = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc
                                          ).isoformat(timespec="seconds")
            yield ("transcript", f.relative_to(cfg.home).as_posix(), m.group(1),
                   f"{m.group(1)} run {m.group(2)}", extract_text(f), when)
        except (OSError, EOFError):
            continue


def reindex_archive(cfg: Config) -> int:
    """Re-read archive/ into search.db. Returns how many documents changed."""
    return search.index_docs(cfg, _archive_docs(cfg))


def reindex_all(cfg: Config, conn: sqlite3.Connection) -> int:
    """Rebuild search.db completely: notes, live records and the archive."""
    return (search.reindex_files(cfg) + search.reindex_records(cfg, conn)
            + reindex_archive(cfg))


# ---- disk and retrieval ----

def archive_bytes(cfg: Config) -> tuple:
    n = size = 0
    if cfg.archive_dir.is_dir():
        for p in cfg.archive_dir.rglob("*"):
            if p.is_file() and not p.is_symlink():
                n += 1
                size += p.stat().st_size
    return n, size


def offsite_needed(cfg: Config, disk_usage_fn=shutil.disk_usage) -> bool:
    """True when the disk holding the company home is `archive_offserver_pct`
    percent full or more. Only reports; Task 14 does the moving."""
    cfg.home.mkdir(parents=True, exist_ok=True)
    total, used = disk_usage_fn(cfg.home)[:2]
    return bool(total) and used * 100 >= total * cfg.archive_offserver_pct


def fetch(cfg: Config, ref: str) -> Path:
    """Local path of an archive file, named like `archive/2026-09/records.jsonl.gz`
    or `2026-09/records.jsonl.gz`. Off-server retrieval arrives with Task 14."""
    rel = Path((ref or "").strip())
    parts = rel.parts
    if parts[:1] == ("archive",):
        parts = parts[1:]
    if not parts or rel.is_absolute() or ".." in parts:
        raise ValueError(f"not an archive reference: {ref!r}")
    p = cfg.archive_dir.joinpath(*parts)
    root = os.path.realpath(cfg.archive_dir)
    if os.path.commonpath([root, os.path.realpath(p)]) != root or p.is_symlink():
        raise ValueError(f"not an archive reference: {ref!r}")
    if p.is_file():
        return p
    raise FileNotFoundError(
        f"{ref} is not on this server. If it was moved off-server, retrieving "
        "it is not available yet.")


# ---- CLI ----

def _run(fn):
    cfg = Config.load()
    conn = connect(cfg)
    try:
        return fn(cfg, conn)
    except (ValueError, KeyError, OSError, ArchiveError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_run(args):
    def go(cfg, conn):
        for k, v in daily(cfg, conn, utcnow()).items():
            print(f"{k}: {v}")
        return 0
    return _run(go)


def _cmd_status(args):
    def go(cfg, conn):
        n, size = archive_bytes(cfg)
        r = conn.execute("SELECT value FROM meta WHERE key=?", (DAILY_KEY,)
                         ).fetchone()
        print(f"files: {n}\nbytes: {size}\n"
              f"offsite needed: {'yes' if offsite_needed(cfg) else 'no'}\n"
              f"last daily run: {r['value'] if r else 'never'}")
        return 0
    return _run(go)


def _cmd_reindex(args):
    def go(cfg, conn):
        print(f"reindexed: {reindex_all(cfg, conn)}")
        return 0
    return _run(go)


def _cmd_fetch(args):
    def go(cfg, conn):
        print(fetch(cfg, args.ref))
        return 0
    return _run(go)


def register(subparsers) -> None:
    a = subparsers.add_parser("archive", help="archive status and retrieval")
    sub = a.add_subparsers(dest="archive_command", required=True)
    sub.add_parser("run", help="archive old records now"
                   ).set_defaults(func=_cmd_run)
    sub.add_parser("status", help="archive size and off-server need"
                   ).set_defaults(func=_cmd_status)
    sub.add_parser("reindex", help="rebuild the search index, archive included"
                   ).set_defaults(func=_cmd_reindex)
    f = sub.add_parser("fetch", help="local path of an archived file")
    f.add_argument("ref")
    f.set_defaults(func=_cmd_fetch)
