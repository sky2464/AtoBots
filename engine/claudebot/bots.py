"""Bot identity (Soul, Persona, Memory), the bot list and the run prompt.

Files live in `<home>/bots/<name>/`; the bot list row holds the settings.
Skills are symlinked into `<bot>/.claude/skills/`, so a run started in the bot
folder only loads that bot's skills.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from . import activity
from .config import Config
from .db import connect, ct_display, tx, utcnow

_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,30}")
STATUSES = ("active", "paused", "retired")
# Thinker bots plan and decide on the big model; worker bots do routine work on
# haiku and are handed to by a thinker through `worker`.
TIERS = ("thinker", "worker")
# Names the record uses for non-bot actors; no bot may take one.
RESERVED_NAMES = ("system", "cli", "hook", "owner")
_FILES = ("SOUL.md", "PERSONA.md", "MEMORY.md")
_TEMPLATE_SKILLS = Path(__file__).resolve().parent.parent / "templates" / "skills"
_LIST_FIELDS = ("skills", "mcp", "plugins")
_PLAIN_FIELDS = ("role", "org", "reports_to", "model", "effort", "tier", "worker")
# Reserved infrastructure roles (the backend) carry this `org`. They are not
# company bots: hidden from lists unless asked for, never created by hand.
RESERVED_ORG = "reserved"


@dataclass
class Bot:
    name: str
    role: str
    org: str | None
    reports_to: str | None
    model: str
    effort: str | None
    skills: tuple
    mcp: tuple
    plugins: tuple
    status: str
    soul_sha: str | None
    created_at: str
    connectors: bool
    tier: str = "thinker"
    worker: str | None = None


def _check_name(name) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError("bot name must match ^[a-z][a-z0-9-]{0,30}$")
    if name in RESERVED_NAMES:
        raise ValueError(f"{name!r} is reserved (used for actors in the "
                         "record); pick another name")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row_to_bot(r: sqlite3.Row) -> Bot:
    def tup(v):
        return tuple(json.loads(v)) if v else ()
    return Bot(r["name"], r["role"], r["org"], r["reports_to"], r["model"],
               r["effort"], tup(r["skills"]), tup(r["mcp"]), tup(r["plugins"]),
               r["status"], r["soul_sha"], r["created_at"],
               bool(r["connectors"]), r["tier"], r["worker"])


def _bot_dir(cfg: Config, name: str) -> Path:
    _check_name(name)
    return cfg.bots_dir / name


def _skill_source(cfg: Config, skill: str) -> Path:
    _check_name(skill)
    for base in (cfg.home / "skills", _TEMPLATE_SKILLS):
        p = base / skill
        if (p / "SKILL.md").is_file():
            return p
    raise ValueError(f"unknown skill: {skill}")


def _link_skills(cfg: Config, name: str, skills) -> None:
    sources = {s: _skill_source(cfg, s) for s in skills}
    d = _bot_dir(cfg, name) / ".claude" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.iterdir():
        if old.is_symlink():
            old.unlink()
    for s, src in sources.items():
        (d / s).symlink_to(src)


def write_settings(cfg: Config, name: str) -> None:
    """Keep ancestor CLAUDE.md files (for example ~/CLAUDE.md, meant for the
    Owner's own server Claude) out of this bot's runs and chats, by listing
    them in `claudeMdExcludes` in the bot's project settings. Other keys in
    the file are kept. Safe to repeat."""
    d = _bot_dir(cfg, name)
    path = d / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    ancestors = [str(p / f) for p in d.resolve().parents
                 for f in ("CLAUDE.md", "CLAUDE.local.md")]
    if data.get("claudeMdExcludes") == ancestors:
        return
    data["claudeMdExcludes"] = ancestors
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _check_tier(tier) -> str:
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    return tier


def _check_worker(conn: sqlite3.Connection, name: str, worker) -> str | None:
    """A worker must name another existing bot whose tier is worker."""
    if not worker:
        return None
    if worker == name:
        raise ValueError("a bot cannot be its own worker")
    r = conn.execute("SELECT tier FROM bots WHERE name=?", (worker,)).fetchone()
    if r is None or r["tier"] != "worker":
        raise ValueError(f"worker must be an existing worker-tier bot: {worker}")
    return worker


def get(conn: sqlite3.Connection, name: str) -> Bot:
    _check_name(name)
    r = conn.execute("SELECT * FROM bots WHERE name=?", (name,)).fetchone()
    if r is None:
        raise KeyError(name)
    return _row_to_bot(r)


def list_bots(conn: sqlite3.Connection, status: str = "active",
              include_reserved: bool = False) -> list[Bot]:
    """Bots with this status. Reserved infrastructure roles (the backend) are
    left out unless `include_reserved` is true."""
    rows = conn.execute("SELECT * FROM bots WHERE status=? ORDER BY name",
                        (status,)).fetchall()
    return [b for b in map(_row_to_bot, rows)
            if include_reserved or b.org != RESERVED_ORG]


def create(cfg: Config, conn: sqlite3.Connection, name: str, role: str,
           soul: str, persona: str, org=None, reports_to="admin",
           model="sonnet", effort=None, skills=(), mcp=(), plugins=(),
           actor="admin", connectors: bool = False, tier: str | None = None,
           worker: str | None = None) -> Bot:
    _check_name(name)
    if org == RESERVED_ORG and actor != "system":
        raise ValueError(f"org {RESERVED_ORG!r} is for infrastructure roles")
    skills, mcp, plugins = tuple(skills), tuple(mcp), tuple(plugins)
    if tier is None:
        tier = "worker" if model == "haiku" else "thinker"
    _check_tier(tier)
    worker = _check_worker(conn, name, worker)
    for s in skills:
        _skill_source(cfg, s)
    d = cfg.bots_dir / name
    if d.exists() or conn.execute("SELECT 1 FROM bots WHERE name=?",
                                  (name,)).fetchone():
        raise ValueError(f"bot {name} already exists")
    d.mkdir(parents=True)
    try:
        (d / "SOUL.md").write_text(soul)
        (d / "PERSONA.md").write_text(persona)
        (d / "MEMORY.md").write_text("# Memory\n")
        _link_skills(cfg, name, skills)
        write_settings(cfg, name)
        with tx(conn):
            conn.execute(
                "INSERT INTO bots(name, role, org, reports_to, model, effort, "
                "skills, mcp, plugins, status, soul_sha, created_at, "
                "connectors, tier, worker) "
                "VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?)",
                (name, role, org, reports_to, model, effort,
                 json.dumps(list(skills)), json.dumps(list(mcp)),
                 json.dumps(list(plugins)), _sha(d / "SOUL.md"), utcnow(),
                 int(bool(connectors)), tier, worker))
            activity.record(conn, actor, "bot.create", f"{name}: {role}", name)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return get(conn, name)


def update(cfg: Config, conn: sqlite3.Connection, name: str, actor: str,
           **fields) -> None:
    allowed = set(_PLAIN_FIELDS) | set(_LIST_FIELDS) | {
        "connectors", "soul", "persona", "decision"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update: {', '.join(sorted(unknown))}")
    get(conn, name)
    if "tier" in fields:
        _check_tier(fields["tier"])
    if "worker" in fields:
        fields["worker"] = _check_worker(conn, name, fields["worker"])
    d = _bot_dir(cfg, name)
    decision = fields.pop("decision", None)
    soul = fields.pop("soul", None)
    persona = fields.pop("persona", None)
    if soul is not None:
        ok = decision and conn.execute(
            "SELECT 1 FROM decisions WHERE id=?", (decision,)).fetchone()
        if not ok:
            raise ValueError("changing a Soul needs a recorded decision ID")
    sets, vals = [], []
    for k in _PLAIN_FIELDS:
        if k in fields:
            sets.append(f"{k}=?")
            vals.append(fields[k])
    for k in _LIST_FIELDS:
        if k in fields:
            sets.append(f"{k}=?")
            vals.append(json.dumps(list(fields[k])))
    if "connectors" in fields:
        sets.append("connectors=?")
        vals.append(int(bool(fields["connectors"])))
    if "skills" in fields:
        _link_skills(cfg, name, tuple(fields["skills"]))
    if persona is not None:
        (d / "PERSONA.md").write_text(persona)
    if soul is not None:
        (d / "SOUL.md").write_text(soul)
        sets.append("soul_sha=?")
        vals.append(_sha(d / "SOUL.md"))
    with tx(conn):
        if sets:
            conn.execute(f"UPDATE bots SET {', '.join(sets)} WHERE name=?",
                         (*vals, name))
        changed = sorted(list(fields) + [k for k, v in
                         (("soul", soul), ("persona", persona)) if v is not None])
        detail = f"{name}: " + ", ".join(changed)
        if soul is not None:
            detail += f" (decision {decision})"
        activity.record(conn, actor, "bot.update", detail, name)


def set_status(conn: sqlite3.Connection, name: str, status: str,
               actor: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    get(conn, name)
    with tx(conn):
        conn.execute("UPDATE bots SET status=? WHERE name=?", (status, name))
        activity.record(conn, actor, f"bot.{status}", name, name)


def _read_capped(path: Path, cap: int) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    return data[:cap].decode("utf-8", errors="ignore")


def identity_prompt(cfg: Config, conn: sqlite3.Connection, name: str) -> str:
    """Soul, Persona, Memory (each capped) and shared rules. Never skills."""
    get(conn, name)
    d = _bot_dir(cfg, name)
    parts = [
        ("Soul", _read_capped(d / "SOUL.md", cfg.soul_cap_bytes)),
        ("Persona", _read_capped(d / "PERSONA.md", cfg.persona_cap_bytes)),
        ("Memory", _read_capped(d / "MEMORY.md", cfg.memory_cap_bytes)),
    ]
    rules = cfg.home / "rules.md"
    if rules.is_file():
        parts.append(("Shared rules", _read_capped(rules, 4 * 1024)))
    return "\n\n".join(f"# {title}\n\n{text.strip()}" for title, text in parts)


def model_mismatches(conn: sqlite3.Connection, include_retired: bool = False
                     ) -> list[tuple[str, str, str]]:
    """Bots whose tier and model disagree: a thinker on haiku, or a worker
    on any other model. Returns (bot, model, problem). Retired bots are
    skipped unless `include_retired` is True."""
    out = []
    where = "" if include_retired else " WHERE status != 'retired'"
    for r in conn.execute("SELECT name, model, tier FROM bots" + where +
                          " ORDER BY name"):
        if r["tier"] == "thinker" and r["model"] == "haiku":
            out.append((r["name"], r["model"], "thinker on haiku"))
        elif r["tier"] == "worker" and r["model"] != "haiku":
            out.append((r["name"], r["model"], "worker not on haiku"))
    return out


def agents_json(conn: sqlite3.Connection, exclude: str) -> str:
    """JSON for `claude --agents`: the other active bots a bot may hand to."""
    out = {}
    for b in list_bots(conn, "active"):
        if b.name == exclude:
            continue
        out[b.name] = {
            "description": b.role or b.name,
            "prompt": f"You are {b.name}, role: {b.role}. Hand work to this "
                      f"bot with `cb send`; do not do its work yourself.",
        }
    return json.dumps(out, sort_keys=True)


def check(cfg: Config, conn: sqlite3.Connection) -> list[str]:
    msgs = []
    caps = {"SOUL.md": cfg.soul_cap_bytes, "PERSONA.md": cfg.persona_cap_bytes,
            "MEMORY.md": cfg.memory_cap_bytes}
    for status in STATUSES:
        for b in list_bots(conn, status, include_reserved=True):
            d = _bot_dir(cfg, b.name)
            for f in _FILES:
                p = d / f
                if not p.is_file():
                    msgs.append(f"{b.name}: {f} is missing")
                    continue
                size = p.stat().st_size
                if size > caps[f]:
                    msgs.append(f"{b.name}: {f} is {size} bytes, over the "
                                f"{caps[f]} byte cap")
            soul = d / "SOUL.md"
            if soul.is_file() and b.soul_sha and _sha(soul) != b.soul_sha:
                msgs.append(f"{b.name}: SOUL.md changed without a recorded "
                            f"decision")
            for s in b.skills:
                if not (d / ".claude" / "skills" / s / "SKILL.md").exists():
                    msgs.append(f"{b.name}: skill {s} is not linked")
    return msgs


# ---- CLI wiring -----------------------------------------------------------

def _actor() -> str:
    return os.environ.get("CB_BOT", "cli")


def _csv(v):
    return tuple(x for x in (v or "").split(",") if x)


def _read(path):
    return Path(path).read_text()


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


def _cmd_new(args) -> int:
    def go(cfg, conn):
        create(cfg, conn, args.name, args.role, _read(args.soul_file),
               _read(args.persona_file), org=args.org,
               reports_to=args.reports_to, model=args.model or cfg.default_model,
               effort=args.effort, skills=_csv(args.skills), mcp=_csv(args.mcp),
               plugins=_csv(args.plugins), actor=_actor(),
               connectors=args.connectors, tier=args.tier,
               worker=args.worker)
        print(f"created {args.name}")
        return 0
    return _run(go)


def _cmd_list(args) -> int:
    def go(cfg, conn):
        for b in list_bots(conn, args.status):
            print(f"{b.name}\t{b.role}\t{b.model}\t{b.status}")
        return 0
    return _run(go)


def _cmd_show(args) -> int:
    def go(cfg, conn):
        b = get(conn, args.name)
        for k in ("name", "role", "org", "reports_to", "model", "effort",
                  "tier", "worker", "status", "connectors"):
            print(f"{k}: {getattr(b, k)}")
        for k in _LIST_FIELDS:
            print(f"{k}: {', '.join(getattr(b, k))}")
        print(f"created: {ct_display(b.created_at)}")
        return 0
    return _run(go)


def _cmd_set(args) -> int:
    def go(cfg, conn):
        f = {}
        for k in _PLAIN_FIELDS:
            v = getattr(args, k)
            if v is not None:
                f[k] = v
        for k in _LIST_FIELDS:
            v = getattr(args, k)
            if v is not None:
                f[k] = _csv(v)
        if args.connectors is not None:
            f["connectors"] = args.connectors
        if args.soul_file:
            f["soul"] = _read(args.soul_file)
        if args.persona_file:
            f["persona"] = _read(args.persona_file)
        if args.decision:
            f["decision"] = args.decision
        update(cfg, conn, args.name, _actor(), **f)
        return 0
    return _run(go)


def _status_cmd(status):
    def cmd(args) -> int:
        def go(cfg, conn):
            set_status(conn, args.name, status, _actor())
            return 0
        return _run(go)
    return cmd


def _cmd_check(args) -> int:
    def go(cfg, conn):
        msgs = check(cfg, conn)
        for m in msgs:
            print(m)
        if not msgs:
            print("ok")
        return 1 if msgs else 0
    return _run(go)


def register(subparsers) -> None:
    sp = subparsers.add_parser("bot", help="bots")
    sub = sp.add_subparsers(dest="bot_cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("name")
    n.add_argument("--role", required=True)
    n.add_argument("--soul-file", required=True)
    n.add_argument("--persona-file", required=True)
    n.add_argument("--org")
    n.add_argument("--reports-to", default="admin")
    n.add_argument("--model")
    n.add_argument("--effort")
    n.add_argument("--skills")
    n.add_argument("--mcp")
    n.add_argument("--plugins")
    n.add_argument("--connectors", action="store_true")
    n.add_argument("--tier", help="thinker or worker (default from model)")
    n.add_argument("--worker", help="name of a worker-tier bot to hand routine work to")
    n.set_defaults(func=_cmd_new)
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=STATUSES, default="active")
    ls.set_defaults(func=_cmd_list)
    sh = sub.add_parser("show")
    sh.add_argument("name")
    sh.set_defaults(func=_cmd_show)
    st = sub.add_parser("set")
    st.add_argument("name")
    for k in _PLAIN_FIELDS:
        st.add_argument(f"--{k.replace('_', '-')}")
    for k in _LIST_FIELDS:
        st.add_argument(f"--{k}", help="comma-separated")
    st.add_argument("--connectors", action=argparse.BooleanOptionalAction, default=None)
    st.add_argument("--soul-file")
    st.add_argument("--persona-file")
    st.add_argument("--decision")
    st.set_defaults(func=_cmd_set)
    for word, status in (("pause", "paused"), ("resume", "active"),
                         ("retire", "retired")):
        p = sub.add_parser(word)
        p.add_argument("name")
        p.set_defaults(func=_status_cmd(status))
    c = subparsers.add_parser("check", help="check bot files")
    c.set_defaults(func=_cmd_check)

