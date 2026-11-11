"""Shared test helpers: a fake systemctl and a throwaway engine clone.

The fake keeps its state as files in one folder, so shell scripts and Python
code under test see the same "systemd":
  active/<unit>     the unit is running
  enabled/<unit>    the unit is enabled
  fail-start/<unit> starting it fails
  no-active/<unit>  starting it "works" but it never becomes active
  log               every call, one per line
"""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_SYSTEMCTL = r'''#!/bin/sh
S=$CB_FAKE_SYSTEMD
quiet=0
args=""
for a in "$@"; do
    case $a in
        --quiet) quiet=1 ;;
        --*) ;;
        *) args="$args $a" ;;
    esac
done
# shellcheck disable=SC2086
set -- $args
cmd=$1
shift
echo "$cmd $*" >> "$S/log"
case $cmd in
    is-active)
        if [ -e "$S/active/$1" ]; then
            [ "$quiet" = 1 ] || echo active
            exit 0
        fi
        [ "$quiet" = 1 ] || echo inactive
        exit 3
        ;;
    is-enabled) [ -e "$S/enabled/$1" ] ;;
    start|restart)
        for u in "$@"; do
            [ ! -e "$S/fail-start/$u" ] || exit 1
            [ -e "$S/no-active/$u" ] || : > "$S/active/$u"
        done
        ;;
    stop) for u in "$@"; do rm -f "$S/active/$u"; done ;;
    enable) for u in "$@"; do : > "$S/enabled/$u"; done ;;
    disable) for u in "$@"; do rm -f "$S/enabled/$u"; done ;;
    daemon-reload) ;;
    *) exit 1 ;;
esac
'''


class FakeSystemd:
    def __init__(self, base: Path):
        self.dir = Path(base) / "fake-systemd"
        for name in ("active", "enabled", "fail-start", "no-active"):
            (self.dir / name).mkdir(parents=True)
        (self.dir / "log").write_text("")
        self.program = self.dir / "systemctl"
        self.program.write_text(FAKE_SYSTEMCTL)
        self.program.chmod(0o755)
        self.units = Path(base) / "etc-systemd"
        self.etc = Path(base) / "etc"
        self.usr_bin = Path(base) / "usr-local-bin"
        self.user_bin = Path(base) / "user-bin"
        for d in (self.units, self.etc, self.usr_bin, self.user_bin):
            d.mkdir()

    def env(self, home: Path, var: str = "CLAUDEBOT_HOME") -> dict:
        env = dict(os.environ)
        env.update({
            var: str(home),
            "CB_FAKE_SYSTEMD": str(self.dir),
            "CB_SYSTEMCTL": str(self.program),
            "CB_NO_SUDO": "1",
            "CB_SYSTEMD_DIR": str(self.units),
            "CB_ETC_DIR": str(self.etc),
            "CB_USR_BIN_DIR": str(self.usr_bin),
            "CB_USER_BIN_DIR": str(self.user_bin),
            "CB_UPD_VERIFY_SECONDS": "6",
            "CB_UPD_STEADY_SECONDS": "0",
            "CB_UPD_IDLE_SECONDS": "2",
            "CB_UPD_IDLE_POLL": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        env.pop("CB_BOT", None)
        env.pop("CB_SESSION_ID", None)
        return env

    def mark(self, kind: str, unit: str, on: bool = True) -> None:
        path = self.dir / kind / unit
        if on:
            path.write_text("")
        elif path.exists():
            path.unlink()

    def has(self, kind: str, unit: str) -> bool:
        return (self.dir / kind / unit).exists()

    def calls(self) -> list:
        return (self.dir / "log").read_text().splitlines()


def git(repo, *args, env=None) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, env=env)
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def copy_engine(dest: Path) -> Path:
    """A copy of this repository's engine tree at dest/engine (no Git)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "engine", dest / "engine",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                  "LOCAL_BUILD"))
    (dest / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    return dest


def commit_all(repo: Path, message: str) -> str:
    env = dict(os.environ, **_GIT_ENV)
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD")


def make_engine_repo(dest: Path, tag: str | None = None) -> Path:
    """A Git repository holding a copy of the engine, with one commit."""
    repo = copy_engine(dest)
    git(repo, "init", "-q", "-b", "main")
    commit_all(repo, "engine")
    if tag:
        git(repo, "tag", tag)
    return repo


def package_dir(repo: Path) -> Path:
    """The engine's Python package folder, whatever it is called."""
    for p in sorted((Path(repo) / "engine").iterdir()):
        if (p / "__init__.py").is_file() and (p / "cli.py").is_file():
            return p
    raise AssertionError("no engine package found")


def set_version(repo: Path, version: str) -> None:
    init = package_dir(repo) / "__init__.py"
    lines = [f'__version__ = "{version}"' if line.startswith("__version__")
             else line for line in init.read_text().splitlines()]
    init.write_text("\n".join(lines) + "\n")
