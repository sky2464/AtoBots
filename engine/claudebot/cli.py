"""The `cb` command line. Each module adds its commands via register()."""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import (activity, admin_live, archive, backup, bots, chat, dispatcher,
               doctor, gitlog, items, messages, requests, routines, search,
               store, tasks, usage)
from . import __version__
from .config import Config
from .db import SCHEMA_VERSION, connect, init_db

MODULES = [store, bots, items, messages, requests, search, routines,
           dispatcher, archive, chat, doctor, backup, admin_live, usage,
           tasks, activity]


TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
ADMIN_SKILLS = ("cb-basics", "work-tracking", "create-bot")
ADMIN_MODEL = "sonnet"
BACKEND_SKILLS = ("cb-basics",)
BACKEND_MODEL = "sonnet"
RULES_SHA_KEY = "templates.rules_sha"
# Templates shipped in 0.0.1 and 0.0.2. A company file with one of these
# hashes was never edited, even though that install recorded no hash.
KNOWN_RULES_SHA = (
    "5a427531b1f2c4c2d119c7ba0554aea9b8fa5fad6d030a2603fbb64a734a0d56",
    "a526bf6e4a43f2816a9e685be0be90e48d4621ea870788a0f7188b193e080f6d",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _meta_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _remember_rules_sha(conn, digest: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                 (RULES_SHA_KEY, digest))


def init_company(cfg: Config, company: str) -> list[str]:
    """Create the home, database, Git repo and Admin. Safe to repeat: nothing
    that exists is overwritten. Returns what was done, for printing."""
    done = []
    existed = cfg.home.exists()
    cfg.home.mkdir(parents=True, exist_ok=True)
    for d in (cfg.work_dir, cfg.knowledge_dir, cfg.bots_dir):
        d.mkdir(exist_ok=True)
    cfg.secrets_dir.mkdir(mode=0o700, exist_ok=True)
    cfg.secrets_dir.chmod(0o700)
    init_db(cfg)
    gitlog.ensure_repo(cfg)
    rules = cfg.home / "rules.md"
    if not rules.exists():
        shutil.copyfile(TEMPLATES / "rules.md", rules)
        done.append("rules.md")
    conn = connect(cfg)
    try:
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('company', ?)",
                     (company,))
        if rules.is_file() and _meta_get(conn, RULES_SHA_KEY) is None:
            file_sha = _sha256(rules)
            template_sha = _sha256(TEMPLATES / "rules.md")
            if file_sha == template_sha:
                _remember_rules_sha(conn, template_sha)
        if not any(b.name == "admin" for st in bots.STATUSES
                   for b in bots.list_bots(conn, st)):
            admin = TEMPLATES / "admin"
            bots.create(cfg, conn, "admin", "Owner's contact and operator",
                        (admin / "SOUL.md").read_text(),
                        (admin / "PERSONA.md").read_text(), reports_to=None,
                        model=ADMIN_MODEL, skills=ADMIN_SKILLS, actor="system")
            shutil.copyfile(admin / "MEMORY.md",
                            cfg.bots_dir / "admin" / "MEMORY.md")
            done.append("admin")
        if not any(b.name == "backend" for st in bots.STATUSES
                   for b in bots.list_bots(conn, st, include_reserved=True)):
            be = TEMPLATES / "backend"
            bots.create(cfg, conn, "backend",
                        "Server health and backups (reserved, no company work)",
                        (be / "SOUL.md").read_text(),
                        (be / "PERSONA.md").read_text(),
                        org=bots.RESERVED_ORG, reports_to=None,
                        model=BACKEND_MODEL, skills=BACKEND_SKILLS,
                        actor="system")
            shutil.copyfile(be / "MEMORY.md",
                            cfg.bots_dir / "backend" / "MEMORY.md")
            done.append("backend")
        for st in bots.STATUSES:  # existing bots too
            for b in bots.list_bots(conn, st, include_reserved=True):
                bots.write_settings(cfg, b.name)
    finally:
        conn.close()
    gitlog.commit_sweep(cfg, f"Set up {company}" if not existed
                        else "Check setup")
    return done


def _replace_tree(src: Path, dst: Path) -> None:
    """Copy `src` onto `dst` via a temp directory and os.replace.

    A crash during the copy leaves the previous tree in place.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=dst.parent, prefix=f".{dst.name}-"))
    try:
        built = staging / dst.name
        shutil.copytree(src, built)
        backup = None
        if dst.exists():
            backup = Path(tempfile.mkdtemp(dir=dst.parent, prefix=f".{dst.name}-old-"))
            os.replace(dst, backup / dst.name)
        os.replace(built, dst)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def refresh_engine_templates(cfg: Config, conn=None) -> list[str]:
    """Copy engine-owned rules and bundled skills into the company home.

    rules.md is replaced only when the company has not edited it. An edited
    copy is left alone and the new template is written beside it. Does not
    touch bot SOUL.md or PERSONA.md.
    """
    own = conn is None
    if own:
        conn = connect(cfg)
    done = []
    try:
        src = TEMPLATES / "rules.md"
        dest = cfg.home / "rules.md"
        digest = _sha256(src)
        current = _sha256(dest) if dest.is_file() else None
        recorded = _meta_get(conn, RULES_SHA_KEY)
        unedited = (current is None or current == recorded
                    or (recorded is None and current in KNOWN_RULES_SHA))
        if current is not None and not unedited:
            shutil.copyfile(src, cfg.home / "rules.md.new")
            print("rules.md has local changes; the new version is in rules.md.new")
            done.append("rules.md.new")
        else:
            shutil.copyfile(src, dest)
            _remember_rules_sha(conn, digest)
            done.append("rules.md")
        src_skills = TEMPLATES / "skills"
        dst = cfg.home / "skills"
        refreshed = 0
        for skill in src_skills.iterdir():
            if not skill.is_dir():
                continue
            _replace_tree(skill, dst / skill.name)
            refreshed += 1
        if refreshed:
            done.append("skills")
    finally:
        if own:
            conn.close()
    return done


def _cmd_init(args) -> int:
    try:
        cfg = Config.load()
        done = init_company(cfg, args.company)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{args.company}: ready at {cfg.home}"
          + (f" (created: {', '.join(done)})" if done else " (already set up)"))
    return 0


def _cmd_version(args) -> int:
    if not args.schema:
        print(__version__)
        return 0
    print(SCHEMA_VERSION)
    try:
        cfg = Config.load()
    except RuntimeError:
        return 0
    if not cfg.db_path.exists():
        return 0
    conn = connect(cfg)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    finally:
        conn.close()
    if row is not None:
        print(row["value"])
    return 0


def _cmd_templates_refresh(_args) -> int:
    try:
        cfg = Config.load()
        done = refresh_engine_templates(cfg)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("refreshed " + (", ".join(done) if done else "nothing"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cb")
    sub = p.add_subparsers(dest="command", required=True)
    for m in MODULES:
        m.register(sub)
    ip = sub.add_parser("init", help="set up the company home and Admin")
    ip.add_argument("--company", required=True)
    ip.set_defaults(func=_cmd_init)
    vp = sub.add_parser("version", help="print the ClaudeBot version")
    vp.add_argument("--schema", action="store_true",
                    help="print the engine schema version and, when a company "
                         "home is configured, the database's")
    vp.set_defaults(func=_cmd_version)
    tp = sub.add_parser("templates", help="engine-owned company templates")
    tsub = tp.add_subparsers(dest="templates_cmd", required=True)
    tr = tsub.add_parser("refresh", help="refresh rules.md and bundled skills")
    tr.set_defaults(func=_cmd_templates_refresh)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:  # e.g. a bad config.json
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
