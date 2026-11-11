"""How the Owner talks to Admin: the opening brief, the interactive chat, and
rolling a long chat over into a fresh session.

`chat_loop` runs `claude` interactively as Admin, directly (not through
systemd-run: it needs the Owner's terminal). It holds an exclusive flock on
`.chat.lock` for as long as the chat is open; the dispatcher reads that lock
(`owner_connected`) and stays quiet while the Owner is talking.

Rollover: the engine watches the session (rotation.Watcher, from the wait
loop). When the context is too large, or large and idle past the cache
lifetime, it builds a summary from the records and calls `rollover`, which
stores the summary, drops a marker and sends SIGTERM to the claude process
named in the pid file. chat_loop sees the marker, archives the old transcript
and starts claude again with the summary in its prompt. Admin can still ask
for the same thing with `cb chat-rollover --summary-file F`.
"""
import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import (activity, archive, bots, items, names, requests, rotation,
               runner)
from .config import Config
from .db import connect, ct_display, tx, utcnow

BOT = "admin"
BRIEF_CAP = 2048
LINE_CAP = 150
MORE_RESERVE = 28
LAST_CHECK_KEY = "chat.last_check"
SUMMARY_PROMPT_CAP = 20000
TRANSCRIPT_SCAN_CAP = 16 * 1024 * 1024
KILL_GRACE_SECONDS = 20
STATE_ENV = "CB_STATE_DIR"  # set by `cb admin serve`: its own state directory


# ---- paths ----

def chat_dir(cfg: Config) -> Path:
    """State of the running chat. The always-on Admin session sets
    CB_STATE_DIR so the Stop hook and `cb chat-rollover` find its files, not
    those of a `cb chat`."""
    override = os.environ.get(STATE_ENV)
    return Path(override) if override else cfg.home / ".chat"


def lock_path(cfg: Config) -> Path:
    return cfg.home / ".chat.lock"


def pid_path(cfg: Config) -> Path:
    return chat_dir(cfg) / "claude.pid"


def rollover_path(cfg: Config) -> Path:
    return chat_dir(cfg) / "rollover.md"


def marker_path(cfg: Config) -> Path:
    return chat_dir(cfg) / "rollover.pending"


def _read_token(cfg: Config):
    try:
        return marker_path(cfg).read_text().strip() or None
    except OSError:
        return None


def _engine_cb() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "cb"


# ---- the lock ----

class ChatBusy(Exception):
    """The lock is held; the message says by whom."""


class ChatLock:
    def __init__(self, fd: int):
        self.fd = fd

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)  # closing releases the flock
            finally:
                self.fd = None


def _holder(cfg: Config) -> str:
    try:
        pid, started = lock_path(cfg).read_text().split(maxsplit=1)
        return f"pid {pid}, since {ct_display(started.strip())}"
    except (OSError, ValueError):
        return "another process"


def acquire_lock(cfg: Config, retries: int = 0, delay: float = 0.2) -> ChatLock:
    """Take the chat lock. `retries` extra tries, `delay` apart, ride out the
    dispatcher's brief probe of the same lock."""
    cfg.home.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(cfg), os.O_RDWR | os.O_CREAT, 0o600)
    for _ in range(retries):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(delay)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ChatBusy(f"A chat with Admin is already open ({_holder(cfg)}). "
                       f"Close it first.") from None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {utcnow()}\n".encode())
        chat_dir(cfg).mkdir(mode=0o700, exist_ok=True)
    except BaseException:
        os.close(fd)
        raise
    return ChatLock(fd)


def owner_connected(cfg: Config) -> bool:
    """True while a chat holds the lock. A crashed chat releases it."""
    cfg.home.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(cfg), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False
    finally:
        os.close(fd)  # drops the probe lock too


# ---- the brief ----

def last_check(conn, key: str = LAST_CHECK_KEY):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def mark_checked(conn, iso: str, key: str = LAST_CHECK_KEY) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                 (key, iso))


def _clip(text: str, cap: int = LINE_CAP) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= cap else text[:cap - 1] + "…"


def _setup_section(cfg: Config) -> list:
    """Things only the Owner can set up (no off-server backup). Shown here and
    in `cb doctor`; they never wake a bot."""
    from . import doctor  # late: doctor imports dispatcher, which imports chat
    try:
        found = doctor.setup_findings(cfg)
    except Exception:  # noqa: BLE001  # the brief matters more than this
        return []
    return [("Not set up", [_clip(f.text) for f in found])] if found else []


def _sections(conn, since: str, now: str, cfg: Config | None = None) -> list:
    out = []
    reqs = requests.unhandled(conn)
    if reqs:
        out.append(("Unhandled requests", [
            _clip(f"R-{r['id']} {ct_display(r['received_at'])}"
                  + (f" (via {r['source']})" if r["source"] else "")
                  + f": {r['text']}")
            for r in reqs]))
    need = [i for i in items.open_items(conn) if i["owner_waiting"]]
    if need:
        out.append(("Needs you", [
            _clip(f"{i['id']} [{i['status']}] {i['title']}") for i in need]))
    week = (datetime.fromisoformat(now) - timedelta(days=7)).isoformat(
        timespec="seconds")
    failed = conn.execute(
        "SELECT * FROM messages WHERE status='failed'"
        " AND COALESCE(done_at, created_at) >= ? ORDER BY id",
        (week,)).fetchall()
    if failed:
        stuck = [
            _clip(f"message {m['id']} {m['from_bot']}->{m['to_bot']}: "
                  f"{m['subject']}") for m in failed]
        stuck.append(_clip(
            "Clear one with `cb message done ID` or rerun it with "
            "`cb message retry ID`."))
        out.append(("Stuck", stuck))
    done, seen = [], set()
    for a in activity.since(conn, since):
        if (a["kind"] == "item.status" and (a["detail"] or "").endswith("-> done")
                and a["ref"] not in seen):
            seen.add(a["ref"])
            row = conn.execute("SELECT title FROM items WHERE id=?",
                               (a["ref"],)).fetchone()
            done.append(_clip(f"{a['ref']} {row['title'] if row else ''}"))
    if done:
        out.append((f"Done since {ct_display(since)}", done))
    if cfg is not None:
        out.extend(_setup_section(cfg))
    return out


def brief(cfg: Config, conn, since: str | None = None,
          now: str | None = None) -> str:
    """What the Owner should know when a chat opens, in at most 2 KB."""
    now = now or utcnow()
    since = since or last_check(conn) or (
        datetime.fromisoformat(now) - timedelta(hours=24)
    ).isoformat(timespec="seconds")
    head = f"Brief as of {ct_display(now)}"
    foot = "Full lists: cb open, cb request unhandled."
    sections = _sections(conn, since, now, cfg)
    if not sections:
        return (f"{head}\nNothing new: no open requests, nothing waiting on "
                f"the Owner, nothing stuck.\n{foot}")

    def size(s):
        return len(s.encode()) + 1

    titles = [f"{name} ({len(lines)}):" for name, lines in sections]
    remaining = (BRIEF_CAP - size(head) - size(foot)
                 - sum(size(t) for t in titles) - MORE_RESERVE * len(sections))
    out = [head]
    for (name, lines), title in zip(sections, titles):
        out.append(title)
        shown = 0
        for line in lines:
            if size(line) + 2 > remaining:
                break
            out.append("  " + line)
            remaining -= size(line) + 2
            shown += 1
        if shown < len(lines):
            out.append(f"  ... and {len(lines) - shown} more")
    out.append(foot)
    text = "\n".join(out)
    return text.encode()[:BRIEF_CAP].decode("utf-8", errors="ignore")


# ---- context size ----

def _usage_total(line: bytes) -> int:
    try:
        d = json.loads(line)
        msg = d["message"]
        if d.get("isSidechain") or d.get("type") != "assistant":
            return 0
        u = msg["usage"]
        return sum(int(u.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens", "output_tokens"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return 0


def context_tokens(transcript_path) -> int:
    """Size of the conversation after the last assistant turn, from that
    turn's usage. Missing file, no usage or garbage reads as 0."""
    if not transcript_path:
        return 0
    try:
        with open(transcript_path, "rb") as f:
            pos = f.seek(0, os.SEEK_END)
            scanned, buf = 0, b""
            while pos > 0 and scanned < TRANSCRIPT_SCAN_CAP:
                step = min(1 << 20, pos)
                pos -= step
                scanned += step
                f.seek(pos)
                buf = f.read(step) + buf
                lines = buf.split(b"\n")
                if pos > 0:  # the first line may be cut off
                    buf = lines[0]
                    lines = lines[1:]
                else:
                    buf = b""
                for line in reversed(lines):
                    n = _usage_total(line) if line.strip() else 0
                    if n:
                        return n
        return 0
    except (OSError, ValueError):
        return 0


# ---- settings and hooks ----

def hook_entry(sub: str) -> list:
    cb = shlex.quote(str(_engine_cb()))
    return [{"hooks": [{"type": "command", "command": f"{cb} hook {sub}",
                        "timeout": 30}]}]


def settings_for_chat(cfg: Config) -> dict:
    return {"hooks": {"UserPromptSubmit": hook_entry("prompt")}}


# ---- rollover ----

def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _rollover_allowed(conn):
    """The open Owner chat, or a person at the shell."""
    who = activity.caller(conn)
    if who.actor == "cli":
        return who
    if who.session and conn.execute(
            "SELECT 1 FROM runs WHERE session_id=? AND ended_at IS NULL"
            " AND lane IN ('chat','live')", (who.session,)).fetchone():
        return who
    activity.refuse(conn, who, "chat-rollover",
                    "only the open Owner chat may roll over")


def rollover(cfg: Config, conn, summary: str, kill=os.kill) -> None:
    """Store the summary, mark the rollover and stop claude so chat_loop can
    restart it. Raises ValueError when nothing can be rolled over."""
    who = _rollover_allowed(conn)
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("the summary is empty")
    if not os.environ.get(STATE_ENV) and not owner_connected(cfg):
        raise ValueError("no chat is open")
    try:
        pid = int(pid_path(cfg).read_text().strip())
    except (OSError, ValueError):
        raise ValueError("the chat is not ready to roll over (no claude "
                         "process recorded)") from None
    chat_dir(cfg).mkdir(mode=0o700, exist_ok=True)
    _write_private(rollover_path(cfg), summary)
    _write_private(marker_path(cfg), uuid.uuid4().hex + "\n")
    activity.record(conn, who.actor, "chat.rollover",
                    "chat rollover requested")
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # already gone; the loop sees the marker and restarts anyway


def _pending(cfg: Config):
    """(summary, token) if a rollover is waiting, else (None, None)."""
    token = _read_token(cfg)
    if token is None:
        return None, None
    try:
        return rollover_path(cfg).read_text(errors="replace"), token
    except OSError:
        return None, None


# ---- the loop ----

def _have_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _compose_prompt(cfg, conn, brief_text: str, summary) -> str:
    parts = [bots.memory_block(cfg, BOT), bots.identity_prompt(cfg, conn, BOT),
             "# This chat\n\nYou are talking live with the Owner. The brief "
             "below was already printed on their screen before the chat opened, "
             "so do not repeat it. Wait for them to speak; answer from the "
             "brief when they ask about it.\n\n# Startup brief\n\n"
             + brief_text]
    if summary is not None:
        parts.append(
            "# Continuing an earlier chat\n\nThe earlier chat grew long and "
            "was restarted. Summary built by the engine from the record (it "
            "has no conversation text). Carry on from "
            "it; do not greet the Owner as if this were new.\n\n"
            + summary[:SUMMARY_PROMPT_CAP])
    return "\n\n".join(parts)


def _command(cfg, bot, session_id, prompt_file, settings_file, mcp_file,
             channels=None):
    cmd = [cfg.claude_bin,
           "--append-system-prompt-file", str(prompt_file),
           "--setting-sources", "project,local"]
    # A channel plugin's server is an MCP server: --strict-mcp-config would
    # drop it, so the always-on session runs without that flag.
    if not bot.connectors and not channels:
        cmd.append("--strict-mcp-config")
    if channels:
        cmd += ["--channels", *channels]
    model = bot.model or cfg.default_model
    cmd += ["--model", model]
    if bot.effort:
        cmd += ["--effort", bot.effort]
    cmd += ["--settings", str(settings_file)]
    if mcp_file:
        cmd += ["--mcp-config", str(mcp_file)]
    for d in runner._plugin_dirs(cfg, bot):
        cmd += ["--plugin-dir", str(d)]
    return cmd + ["--dangerously-skip-permissions", "--session-id", session_id]


def memory_wrap(cfg: Config, cmd: list) -> list:
    """Apply MemoryMax to interactive Admin (chat / live) via systemd-run
    --user --scope when a user manager is available. Scope mode keeps the
    caller's environment, working directory, terminal and pid. On hosts
    without a user manager the bare command is returned unchanged (no
    system-mode systemd-run, which needs a password prompt)."""
    sr = shutil.which("systemd-run")
    if not sr:
        return list(cmd)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    use_user = bool(runtime and Path(runtime, "systemd").is_dir())
    if not use_user:
        return list(cmd)
    return [sr, "--user", "--scope", "--collect",
            "-p", f"MemoryMax={cfg.run_memory_max}",
            "-p", "MemorySwapMax=0",
            "--", *cmd]


def _launch(cmd, cwd, env, cfg: Config | None = None):
    final = memory_wrap(cfg, cmd) if cfg is not None else list(cmd)
    try:
        return subprocess.Popen(final, cwd=cwd, env=env)
    except OSError:
        if final != list(cmd):
            return subprocess.Popen(cmd, cwd=cwd, env=env)
        raise


def _wait(proc, cfg, token=None, stop=None, watcher=None, peek=None) -> int:
    """Wait for claude. Ctrl-C belongs to claude, not to us. If a rollover
    was requested (or `stop` is set) and claude ignores SIGTERM, kill it after
    a grace period."""
    deadline, termed = None, False
    while True:
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if watcher is not None:
                watcher.tick()  # may request a rollover; never raises
            if peek is not None:
                peek.tick()  # Telegram status line; never raises
            if stop is not None and stop.is_set():
                if not termed:
                    termed = True
                    proc.terminate()
                deadline = deadline or time.monotonic() + KILL_GRACE_SECONDS
                if time.monotonic() > deadline:
                    proc.kill()
            elif _read_token(cfg) not in (None, token):
                deadline = deadline or time.monotonic() + KILL_GRACE_SECONDS
                if time.monotonic() > deadline:
                    proc.kill()
        except KeyboardInterrupt:
            continue


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _one_session(cfg, conn, summary, token, first: bool, live=None) -> str:
    """Run claude once. Returns 'rollover', 'ok', 'error' or (always-on
    session only) 'stopped'. `live` is the always-on Admin's settings (see
    admin_live.Live); None means an ordinary `cb chat`."""
    bot = bots.get(conn, BOT)
    lane = live.lane if live else "chat"
    prefix = "admin-live" if live else "chat"
    session_id = str(uuid.uuid4())
    with tx(conn):
        run_id = conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at)"
            " VALUES (?,?,?,?)", (BOT, session_id, lane, utcnow())).lastrowid
        activity.record(conn, BOT, f"{prefix}.start" if live else "chat.start",
                        f"{prefix} session {session_id}", str(run_id))
    temp, code, reason, status = [], None, "error", None
    try:
        try:
            since = last_check(conn, live.check_key if live
                               else LAST_CHECK_KEY)
            if live and since is None:  # never fall back to the chat's mark
                since = (datetime.now(timezone.utc) - timedelta(hours=24)
                         ).isoformat(timespec="seconds")
            text = brief(cfg, conn, since=since)
        except Exception as e:  # the chat matters more than the brief
            text = f"(The brief could not be built: {type(e).__name__}: {e})"
        if first or live:
            with tx(conn):
                mark_checked(conn, utcnow(),
                             live.check_key if live else LAST_CHECK_KEY)
        composed = (live.compose(cfg, conn, text, summary) if live
                    else _compose_prompt(cfg, conn, text, summary))
        prompt = runner._private_file(composed, ".md")
        temp.append(prompt)
        settings = runner._private_file(json.dumps(
            live.settings(cfg) if live else settings_for_chat(cfg)), ".json")
        temp.append(settings)
        mcp_text = runner._mcp_text(cfg, bot, conn)
        mcp = None
        if mcp_text:
            mcp = runner._private_file(mcp_text, ".json")
            temp.append(mcp)
        cmd = _command(cfg, bot, session_id, prompt, settings, mcp,
                       channels=live.channels if live else None)
        env = dict(os.environ, CB_BOT=BOT, CB_SESSION_ID=session_id,
                   **names.home_env(cfg.home))
        if live:
            # TokenError must refuse the start (no empty Telegram spam loop).
            from . import admin_live
            try:
                env.update(live.session_env(cfg, conn))
            except admin_live.TokenError:
                reason = "no-token"
                raise
        if summary is not None:
            print("Continuing this chat in a fresh session.", flush=True)
        elif live:
            print(f"Starting Admin (always-on) as of {ct_display(utcnow())}.",
                  flush=True)
        elif first:
            print(text, flush=True)  # on screen only; not sent as a prompt
        proc = _launch(cmd, str(cfg.bots_dir / BOT), env, cfg)
        # The marker stays until this session ends well; the summary file is
        # never deleted.
        _write_private(pid_path(cfg), f"{proc.pid}\n")
        if live:
            from . import peek as peek_mod
            peek_mod.note_turn_ended(chat_dir(cfg))  # none carries over
            status = live.peek(cfg, session_id)
        code = _wait(proc, cfg, token, live.stop if live else None,
                     rotation.Watcher(cfg, conn, session_id), status)
        now_token = _read_token(cfg)
        if live and live.stop.is_set():
            reason = "stopped"
        elif now_token is not None and now_token != token:
            reason = "rollover"  # a new rollover was requested meanwhile
        else:
            reason = "ok" if code == 0 else "error"
            if reason == "ok" and token is not None:
                _unlink(marker_path(cfg))  # the summary did its job
    finally:
        if status is not None:
            status.close()  # before the token file goes
        if live:
            live.cleanup()
        _unlink(pid_path(cfg))
        for p in temp:
            _unlink(p)
        with tx(conn):
            conn.execute(
                "UPDATE runs SET ended_at=?, exit_code=?, reason=? WHERE id=?",
                (utcnow(), code, reason, run_id))
            activity.record(conn, BOT, f"{prefix}.end" if live else "chat.end",
                            f"{prefix} session ended: {reason}", str(run_id))
    if reason == "rollover":
        try:
            archive.archive_transcript(cfg, conn, run_id)
        except Exception:
            pass  # recorded by the archive module; the sweep retries
    return reason


def chat_loop(cfg: Config) -> None:
    lock = acquire_lock(cfg, retries=3)
    try:
        conn = connect(cfg)
        try:
            summary, token = _pending(cfg)  # left by an earlier chat
            first = True
            while True:
                reason = _one_session(cfg, conn, summary, token, first)
                first = False
                if reason != "rollover":
                    return
                summary, token = _pending(cfg)
                if summary is None:
                    return
        finally:
            conn.close()
    finally:
        lock.close()


# ---- CLI ----

def _cmd_chat(args) -> int:
    if not _have_terminal():
        print("error: cb chat needs a terminal", file=sys.stderr)
        return 1
    cfg = Config.load()
    try:
        chat_loop(cfg)
    except ChatBusy as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_rollover(args) -> int:
    cfg = Config.load()
    try:
        summary = Path(args.summary_file).read_text()
        conn = connect(cfg)
        try:
            rollover(cfg, conn, summary)
        finally:
            conn.close()
    except (ValueError, OSError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("Rolling over: the chat restarts in a moment with your summary.")
    return 0


def register(subparsers) -> None:
    subparsers.add_parser("chat", help="talk to Admin (needs a terminal)"
                          ).set_defaults(func=_cmd_chat)
    r = subparsers.add_parser(
        "chat-rollover", help="restart this chat with a summary (Admin runs it)")
    r.add_argument("--summary-file", required=True)
    r.set_defaults(func=_cmd_rollover)
