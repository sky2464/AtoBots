"""Start an engine update in a standalone transient systemd unit."""
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

UNIT_NAME = "claudebot-update"
TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


def is_unit_active(unit: str = UNIT_NAME) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                           timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_command(cfg: Config, tag: str, log_path: Path,
                  force: bool = False,
                  script_path: Path | None = None) -> list[str]:
    if script_path is None:
        script_path = Path(__file__).resolve().parent.parent / "update.sh"
    cmd = [
        "sudo", "systemd-run",
        f"--unit={UNIT_NAME}",
        "--collect",
        "--no-block",
        f"--setenv=CLAUDEBOT_HOME={cfg.home}",
        f"--uid={cfg.run_user}",
        "-p", f"StandardOutput=file:{log_path}",
        "-p", f"StandardError=file:{log_path}",
        str(script_path),
        tag,
    ]
    if force:
        cmd.append("--force")
    return cmd


def run_update(cfg: Config, tag: str, force: bool = False,
               is_active_fn=None,
               runner=None) -> int:
    if is_active_fn is None:
        is_active_fn = is_unit_active
    if runner is None:
        runner = subprocess.run
    if not TAG_PATTERN.match(tag):
        print("error: TAG must look like v0.0.1", file=sys.stderr)
        return 2

    if is_active_fn(UNIT_NAME):
        print(f"error: {UNIT_NAME} unit is already active", file=sys.stderr)
        return 1

    logs_dir = cfg.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(logs_dir, 0o700)
    except OSError:
        pass

    now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs_dir / f"update-{now_str}.log"

    cmd = build_command(cfg, tag, log_path, force=force)
    try:
        runner(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"error: failed to start update unit (exit {e.returncode})",
              file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: failed to run systemd-run: {e}", file=sys.stderr)
        return 1

    print(f"Log: {log_path}")
    print("The update runs on its own; Admin will restart at the end.")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    cfg = Config.load()
    return run_update(cfg, args.tag, force=bool(args.force))


def register(subparsers) -> None:
    p = subparsers.add_parser("update",
                              help="update ClaudeBot in a standalone transient unit")
    p.add_argument("tag", help="release tag (e.g. v0.0.1)")
    p.add_argument("--force", action="store_true",
                   help="continue even if bot runs are still active")
    p.set_defaults(func=_cmd_update)
