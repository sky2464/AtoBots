"""The one wrapper every `cb` subcommand module runs its handlers through."""
import sys

from .config import Config
from .db import connect

# OSError already covers PermissionError.
ERRORS = (ValueError, KeyError, OSError)


def run(fn, *extra_errors):
    """Open the company database, call fn(cfg, conn), print a known failure
    as `error: ...` and return 1. `extra_errors` adds a module's own
    exception types (for example archive.ArchiveError)."""
    cfg = Config.load()
    conn = connect(cfg)
    try:
        return fn(cfg, conn)
    except ERRORS + tuple(extra_errors) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
