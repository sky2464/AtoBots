"""The `cb` command line. Each module adds its commands via register()."""
import argparse
import shutil
import sys
from pathlib import Path

from . import (archive, bots, chat, dispatcher, gitlog, items, messages, requests,
               routines, search, store)
from .config import Config
from .db import connect, init_db

MODULES = [store, bots, items, messages, requests, search, routines,
           dispatcher, archive, chat]


TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
ADMIN_SKILLS = ("cb-basics", "work-tracking", "create-bot")
ADMIN_MODEL = "opus"


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
    finally:
        conn.close()
    gitlog.commit_sweep(cfg, f"Set up {company}" if not existed
                        else "Check setup")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cb")
    sub = p.add_subparsers(dest="command", required=True)
    for m in MODULES:
        m.register(sub)
    ip = sub.add_parser("init", help="set up the company home and Admin")
    ip.add_argument("--company", required=True)
    ip.set_defaults(func=_cmd_init)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
