"""Run one bot's short Claude session under memory and time limits.

The session is started with `sudo systemd-run`, which enforces MemoryMax and
RuntimeMaxSec. Every run is a fresh session: the task packet holds pointers
(message text, item IDs, file paths), never earlier output. Claiming messages
and retrying is the dispatcher's job; this module runs once and records it.
"""
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import activity, bots, items, messages
from .config import Config
from .db import tx, utcnow

CLAUDE_HOME = "/home/claude"
LEVEL_ORDER = ("housekeeping", "normal", "deep", "heavy")
LEVEL_MODEL = {"housekeeping": "haiku", "deep": "opus", "heavy": "opus"}
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")

# Text that marks a plan usage limit. Only the "hit your ... limit" family is
# documented (preflight notes); the exact text in a `-p` run is unverified, so
# keep every pattern here, one place to change.
USAGE_LIMIT_PATTERNS = (
    r"hit your [\w' -]{0,40}limit",
    r"usage limit (reached|exceeded)",
)
# systemd-run's own wording when it stops a unit (stderr of the wrapper).
OOM_PATTERNS = (r"result: oom-kill", r"result 'oom-kill'")
TIMEOUT_PATTERNS = (r"result: timeout", r"result 'timeout'")
# Exit codes: 137 = SIGKILL (the OOM killer), 143 = SIGTERM (RuntimeMaxSec).
OOM_EXIT_CODES = (137,)
TIMEOUT_EXIT_CODES = (143,)


@dataclass
class RunResult:
    run_id: int
    session_id: str
    exit_code: int
    reason: str  # ok | usage_limit | timeout | oom | error
    tokens_in: int | None
    tokens_out: int | None


def _bot(conn, bot) -> bots.Bot:
    return bots.get(conn, bot) if isinstance(bot, str) else bot


def settings_for(bot: bots.Bot, msgs) -> tuple:
    """(model, effort) for this run. A batch takes the highest level and the
    highest effort among its messages; unset effort falls back to the bot's."""
    levels = [m["level"] for m in msgs if m["level"] in LEVEL_ORDER]
    level = max(levels, key=LEVEL_ORDER.index, default="normal")
    model = LEVEL_MODEL.get(level, bot.model)
    efforts = [m["effort"] for m in msgs if m["effort"] in EFFORT_ORDER]
    effort = max(efforts, key=EFFORT_ORDER.index, default=bot.effort)
    return model, effort


def _resolve(cfg: Config, kind: str, name: str, suffix: str = "") -> Path:
    p = Path(name) if os.path.isabs(name) else cfg.home / kind / (name + suffix)
    if not p.exists():
        raise FileNotFoundError(f"{kind} {name!r} not found at {p}")
    return p


def _plugin_dirs(cfg: Config, bot: bots.Bot) -> list:
    return [_resolve(cfg, "plugins", n) for n in bot.plugins]


def task_packet(cfg: Config, conn: sqlite3.Connection, bot, msgs) -> str:
    """What to do now, as pointers. File contents are not included."""
    bot = _bot(conn, bot)
    lines = [f"You are {bot.name}. New work for this run:", ""]
    item_ids = []
    for m in msgs:
        lines.append(f"## Message {m['id']} from {m['from_bot']}: {m['subject']}")
        if m["item_id"]:
            lines.append(f"Item: {m['item_id']}")
            if m["item_id"] not in item_ids:
                item_ids.append(m["item_id"])
        lines += [m["body"], ""]
    for iid in item_ids:
        row = items.get(conn, iid)
        lines.append(f"## {iid}: {row['title']} (status {row['status']})")
        # Absolute: the run starts in bots/<name>, not in the company home.
        lines.append(f"Item folder: {cfg.home / 'work' / iid}/")
        note = cfg.home / messages.handoff_rel_path(iid, bot.name)
        if note.is_file():
            lines.append(f"Your handoff note from earlier: {note} (read it first)")
        lines.append("")
    lines.append("Read the files named above yourself. When you finish, "
                 "update the item and answer messages with the cb commands.")
    return "\n".join(lines)


def build_command(cfg: Config, bot: bots.Bot, session_id: str, prompt_file,
                  lane: str, *, task: str = "", model: str | None = None,
                  effort: str | None = None, mcp_config=None,
                  agents: str | None = None) -> list:
    """The full command line. `prompt_file` holds the identity text and goes in
    with --append-system-prompt-file; `task` is the prompt. `lane` only labels
    the run (limits are the same in every lane)."""
    cmd = ["sudo", "systemd-run", "--wait", "--collect", "--pipe",
           "--uid=claude",
           "-p", f"MemoryMax={cfg.run_memory_max}",
           # No swap: with swap, a run over MemoryMax is slowed, not killed.
           "-p", "MemorySwapMax=0",
           "-p", f"RuntimeMaxSec={cfg.run_seconds}",
           f"--working-directory={cfg.bots_dir / bot.name}",
           f"--setenv=HOME={CLAUDE_HOME}",
           f"--setenv=PATH={CLAUDE_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
           f"--setenv=CB_BOT={bot.name}",
           f"--setenv=CB_SESSION_ID={session_id}",
           f"--setenv=CLAUDEBOT_HOME={cfg.home}",
           cfg.claude_bin, "-p", task,
           "--output-format", "json",
           "--dangerously-skip-permissions",
           "--setting-sources", "project,local",
           "--session-id", session_id,
           "--append-system-prompt-file", str(prompt_file)]
    if not bot.connectors:
        cmd.append("--strict-mcp-config")
    model = model or bot.model
    effort = effort if effort is not None else bot.effort
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if mcp_config:
        cmd += ["--mcp-config", str(mcp_config)]
    for d in _plugin_dirs(cfg, bot):
        cmd += ["--plugin-dir", str(d)]
    if agents:
        cmd += ["--agents", agents]
    return cmd


def _parse(output: str):
    """The JSON result object from claude's stdout, or None. Tries the whole
    text, then each line from the last."""
    text = (output or "").strip()
    candidates = [text] + [l for l in reversed(text.split("\n")) if l.strip()]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _usage_limited(data, text: str) -> bool:
    """A usage limit by pattern, by HTTP 429, or by an error result whose text
    mentions both a limit and a reset time."""
    if any(re.search(p, text, re.I) for p in USAGE_LIMIT_PATTERNS):
        return True
    if not data:
        return False
    if data.get("api_error_status") == 429:
        return True
    low = str(data.get("result", "")).lower()
    return bool(data.get("is_error")) and "limit" in low and "reset" in low


def classify(exit_code: int, output: str) -> str:
    """ok | usage_limit | timeout | oom | error. Unknown failures are error."""
    output = output or ""
    if exit_code == 0:
        data = _parse(output)
        if data is None:
            return "error"
        if not data.get("is_error"):
            return "ok"
        return "usage_limit" if _usage_limited(
            data, str(data.get("result", ""))) else "error"
    if exit_code in OOM_EXIT_CODES or any(
            re.search(p, output, re.I) for p in OOM_PATTERNS):
        return "oom"
    if exit_code in TIMEOUT_EXIT_CODES or any(
            re.search(p, output, re.I) for p in TIMEOUT_PATTERNS):
        return "timeout"
    data = _parse(output)
    text = str(data.get("result", "")) if data else output
    return "usage_limit" if _usage_limited(data, text) else "error"


def _tokens(data) -> tuple:
    """(input, output) tokens; input counts cached input too."""
    usage = data.get("usage") if data else None
    if not isinstance(usage, dict):
        return None, None
    try:
        tin = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens"))
        return tin, int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None, None


def _private_file(text: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="cb-run-", suffix=suffix)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except BaseException:
        os.unlink(path)
        raise
    return path


def _mcp_text(cfg: Config, bot: bots.Bot) -> str | None:
    """Merge the bot's MCP definitions. They are copied as written, so tokens
    must appear as ${VARIABLE} references, never as values."""
    if not bot.mcp:
        return None
    servers = {}
    for name in bot.mcp:
        data = json.loads(_resolve(cfg, "mcp", name, ".json").read_text())
        servers.update(data.get("mcpServers", {}))
    return json.dumps({"mcpServers": servers})


def run(cfg: Config, conn: sqlite3.Connection, bot, msgs, lane: str,
        on_start=None) -> RunResult:
    """`on_start(run_id)`, if given, is called once the run row is recorded and
    before the session starts, so the dispatcher can link messages to the run."""
    bot = _bot(conn, bot)
    session_id = str(uuid.uuid4())
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO runs(bot, session_id, lane, started_at) VALUES (?,?,?,?)",
            (bot.name, session_id, lane, utcnow()))
        run_id = cur.lastrowid
        activity.record(conn, bot.name, "run.start",
                        f"{bot.name} run {run_id} ({lane})", str(run_id))
    if on_start is not None:
        on_start(run_id)
    temp, exit_code, output = [], 1, ""
    try:
        model, effort = settings_for(bot, msgs)
        ident = _private_file(bots.identity_prompt(cfg, conn, bot.name), ".md")
        temp.append(ident)
        mcp_text = _mcp_text(cfg, bot)
        mcp = None
        if mcp_text:
            mcp = _private_file(mcp_text, ".json")
            temp.append(mcp)
        cmd = build_command(
            cfg, bot, session_id, ident, lane,
            task=task_packet(cfg, conn, bot, msgs), model=model, effort=effort,
            mcp_config=mcp, agents=bots.agents_json(conn, bot.name))
        try:
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, errors="replace",
                               timeout=cfg.run_seconds + 120)
            exit_code, output = p.returncode, p.stdout + "\n" + p.stderr
            stdout = p.stdout
        except subprocess.TimeoutExpired:
            exit_code, output, stdout = 143, "Finished with result: timeout", ""
        reason = classify(exit_code, stdout if exit_code == 0 else output)
    except Exception as e:  # setup failed: nothing ran
        exit_code, output, stdout, reason = 1, f"{type(e).__name__}: {e}", "", "error"
    finally:
        for path in temp:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    data = _parse(stdout)
    tin, tout = _tokens(data)
    sid = data.get("session_id") if data else None
    sid = sid if isinstance(sid, str) and sid else session_id
    with tx(conn):
        conn.execute(
            "UPDATE runs SET session_id=?, ended_at=?, exit_code=?, reason=?,"
            " input_tokens=?, output_tokens=? WHERE id=?",
            (sid, utcnow(), exit_code, reason, tin, tout, run_id))
        activity.record(conn, bot.name, "run.end",
                        f"{bot.name} run {run_id}: {reason}", str(run_id))
    return RunResult(run_id, sid, exit_code, reason, tin, tout)
