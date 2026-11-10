"""The `cb` command line. Each module adds its commands via register()."""
import argparse
import shutil
import sys
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


def refresh_engine_templates(cfg: Config) -> list[str]:
    """Copy engine-owned rules.md and bundled skills into the company home.
    Does not touch bot SOUL.md or PERSONA.md."""
    done = []
    shutil.copyfile(TEMPLATES / "rules.md", cfg.home / "rules.md")
    done.append("rules.md")
    src = TEMPLATES / "skills"
    dst = cfg.home / "skills"
    dst.mkdir(parents=True, exist_ok=True)
    refreshed = 0
    for skill in src.iterdir():
        if not skill.is_dir():
            continue
        target = dst / skill.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target)
        refreshed += 1
    if refreshed:
        done.append("skills")
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
