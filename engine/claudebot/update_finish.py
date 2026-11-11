"""The second half of an engine update, run by the NEW version's code.

The first half (engine/update.sh or engine/adopt.sh of the version that was
installed) has backed up the database, held the queue, stopped the dispatcher
and switched the code. It then runs `engine/lib/update_finish.sh`, which lands
here. This half applies schema changes and migration scripts, installs the
units, restarts the services and proves they came back. If any of that fails
it puts the old code, the old database and the old unit files back by itself,
restarts the services and tells the Owner what happened.

What the first half passes on (environment variables; unknown ones are
ignored, so the contract can grow):

  CB_UPD_KIND         git (the clone was moved to a new commit) or adopt (a
                      copied install was replaced by a new clone)
  CB_UPD_OLD_VERSION  version before the update
  CB_UPD_OLD_REF      tag or commit to go back to (git)
  CB_UPD_OLD_FOLDER   where the old engine folder was moved to (adopt)
  CB_UPD_BACKUP       database copy taken before anything changed
  CB_UPD_LABEL        where the new code came from: tag:v0.0.3, local:<sha>
  CB_UPD_OLD_PREFIX   unit-name prefix of the old version
  CB_UPD_STOPPED_AT   when the dispatcher was stopped (epoch seconds)
  CB_UPD_LEGACY       1 when an updater from before 0.0.3 handed over
  CB_UPDATE_LOG       the job's log file

Every module this file needs is imported at the top on purpose: a rollback
swaps the code on disk under the running process, and a late import would
load the old version's file.
"""
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import admin_live, chat, names, store, update
from . import full_version
from .config import Config
from .db import connect

VERIFY_SECONDS = 180    # how long the services get to come back
STEADY_SECONDS = 8      # they must then stay up this long
QUIESCE_SECONDS = 180   # how long a running backup or health check gets
MIGRATION_SECONDS = 900
KEEP_BACKUPS = 5
APPLIED_KEY = "migrations.applied"
# Exit codes the first half understands (engine/lib/update_common.sh). Any
# other non-zero code means this file never ran, and the first half puts the
# old version back itself.
ROLLED_BACK = 10
ROLLBACK_FAILED = 11
BUSY_STATES = ("active", "activating", "deactivating", "reloading")


class Failed(Exception):
    """A step failed; the update is rolled back."""


@dataclass
class State:
    kind: str = "git"
    old_version: str = ""
    old_ref: str = ""
    old_folder: Path | None = None
    backup: Path | None = None
    label: str = ""
    old_prefix: str = ""
    stopped_at: float | None = None
    legacy: bool = False
    log: str = ""

    @classmethod
    def from_env(cls, env=None) -> "State":
        env = os.environ if env is None else env

        def path(key):
            value = env.get(key) or ""
            return Path(value) if value.startswith("/") else None
        try:
            stopped = float(env.get("CB_UPD_STOPPED_AT") or "")
        except ValueError:
            stopped = None
        kind = env.get("CB_UPD_KIND") or "git"
        return cls(kind=kind if kind in ("git", "adopt") else "git",
                   old_version=env.get("CB_UPD_OLD_VERSION") or "",
                   old_ref=env.get("CB_UPD_OLD_REF") or "",
                   old_folder=path("CB_UPD_OLD_FOLDER"),
                   backup=path("CB_UPD_BACKUP"),
                   label=env.get("CB_UPD_LABEL") or "",
                   old_prefix=env.get("CB_UPD_OLD_PREFIX") or "",
                   stopped_at=stopped,
                   legacy=env.get("CB_UPD_LEGACY") == "1",
                   log=env.get("CB_UPDATE_LOG") or "")


class System:
    """systemd and the root-owned files, replaceable in tests (the same
    switches as engine/lib/sysfiles.sh)."""

    def __init__(self, env=None, run=subprocess.run):
        env = os.environ if env is None else env
        self.env = dict(env)
        self.program = env.get("CB_SYSTEMCTL") or "systemctl"
        self.sudo = [] if env.get("CB_NO_SUDO") else ["sudo"]
        self.unit_dir = Path(env.get("CB_SYSTEMD_DIR")
                             or "/etc/systemd/system")
        self._run = run

    def _call(self, cmd, timeout=120):
        try:
            return self._run(cmd, capture_output=True, text=True,
                             timeout=timeout, env=self.env)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def state(self, unit: str) -> str:
        r = self._call([self.program, "is-active", unit], timeout=20)
        return (r.stdout or "").strip() if r is not None else ""

    def is_active(self, unit: str) -> bool:
        return self.state(unit) == "active"

    def busy(self, unit: str) -> bool:
        return self.state(unit) in BUSY_STATES

    def is_enabled(self, unit: str) -> bool:
        r = self._call([self.program, "is-enabled", "--quiet", unit],
                       timeout=20)
        return r is not None and r.returncode == 0

    def ctl(self, *args, timeout=120) -> bool:
        r = self._call([*self.sudo, self.program, *args], timeout=timeout)
        return r is not None and r.returncode == 0

    def install(self, src, dest, mode: str = "644") -> bool:
        """Never writes through what is at `dest` (see install_root_file)."""
        self._call([*self.sudo, "rm", "-f", "--", str(dest)])
        r = self._call([*self.sudo, "install", "-m", mode, str(src),
                        str(dest)])
        return r is not None and r.returncode == 0

    def remove(self, path) -> None:
        self._call([*self.sudo, "rm", "-f", "--", str(path)])


class Finisher:
    def __init__(self, cfg: Config, st: State, system: System | None = None,
                 engine: Path | None = None, sleep=time.sleep,
                 clock=time.time, notify=None, out=None, env=None):
        self.cfg, self.st = cfg, st
        self.sys = system or System()
        self.engine = Path(engine) if engine else update.engine_dir()
        self.repo = self.engine.parent
        self.sleep, self.clock = sleep, clock
        self._notify = notify
        self._out = out or (lambda s: print(s, flush=True))
        self.env = dict(os.environ if env is None else env)
        self.new_prefix = names.UNIT_PREFIX
        self.old_prefix = st.old_prefix or self._guess_prefix()
        self.new_version = full_version()
        self.started = clock()
        self.token = None
        self.snapshot_dir: Path | None = None
        self.enabled: dict = {}
        self.admin_enabled = False
        self.admin_silent = False
        self.admin_reason = ""
        self.marker = (cfg.home / admin_live.STATE_DIRNAME
                       / admin_live.UPDATE_PENDING_FILE)

    # ---- small helpers ----

    def say(self, text: str) -> None:
        self._out(text)

    def _guess_prefix(self) -> str:
        for prefix in names.KNOWN_PREFIXES:
            if (self.sys.unit_dir / names.unit("dispatcher.service",
                                               prefix)).exists():
                return prefix
        return names.UNIT_PREFIX

    def old(self, role: str) -> str:
        return names.unit(role, self.old_prefix)

    def new(self, role: str) -> str:
        return names.unit(role, self.new_prefix)

    def cb(self, *args, capture: bool = False, timeout: int = 600):
        """Run the engine's own `cb` (the code on disk right now)."""
        cmd = [str(self.engine / "bin" / "cb"), *args]
        try:
            return subprocess.run(cmd, env=self.env, timeout=timeout,
                                  text=True, capture_output=capture)
        except subprocess.TimeoutExpired:
            raise Failed(f"`cb {' '.join(args[:2])}` did not finish within "
                         f"{timeout} seconds") from None
        except OSError as e:
            raise Failed(f"could not run cb: {e}") from None

    def must(self, *args, timeout: int = 600) -> None:
        r = self.cb(*args, timeout=timeout)
        if r.returncode != 0:
            raise Failed(f"`cb {' '.join(args[:2])}` failed "
                         f"(exit {r.returncode})")

    def lib(self, script: str, *args, timeout: int = 300) -> None:
        """Run a function from engine/lib (the shell side of the install)."""
        body = ('set -euo pipefail; source "$1/lib/cb_wrapper.sh"; '
                'source "$1/lib/units.sh"; ' + script)
        try:
            r = subprocess.run(["bash", "-c", body, "cb-update",
                                str(self.engine), *args], env=self.env,
                               timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Failed(f"a shell step did not finish: {e}") from None
        if r.returncode != 0:
            raise Failed(f"a shell step failed: {script.split()[0]}")

    def notify(self, text: str) -> None:
        """Tell the Owner through the Bot API. Admin may be down, so this
        never goes through it. Best effort."""
        self.say("Notice to the Owner: " + text)
        try:
            if self._notify is not None:
                self._notify(text)
            elif self.token:
                admin_live.notify_telegram(self.cfg, text, token=self.token)
            else:
                subprocess.run([str(self.engine / "bin" / "cb"), "admin",
                                "notify", text], env=self.env, timeout=60,
                               capture_output=True)
        except Exception as e:  # noqa: BLE001
            self.say(f"note: the notice could not be sent "
                     f"({type(e).__name__})")

    def _company(self) -> str:
        try:
            conn = sqlite3.connect(f"file:{self.cfg.db_path}?mode=ro",
                                   uri=True)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key=?",
                                   ("company",)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise Failed(f"cannot read the company database: {e}") from None
        if not row or not row[0]:
            raise Failed("the database has no company name")
        return row[0]

    def _set_meta(self, **values) -> None:
        conn = connect(self.cfg)
        try:
            for key, value in values.items():
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                    (key, str(value)))
            conn.commit()
        finally:
            conn.close()

    # ---- the steps ----

    def snapshot(self) -> None:
        """Keep the unit files and what was enabled, so a rollback can put
        back exactly what was there, whatever the units are called now."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        d = self.cfg.home / "backups" / f"pre-update-units-{stamp}"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        for role in names.UNIT_ROLES:
            src = self.sys.unit_dir / self.old(role)
            if src.is_file():
                shutil.copyfile(src, d / self.old(role))
        self.enabled = {role: self.sys.is_enabled(self.old(role))
                        for role in names.ENABLE_ROLES}
        self.admin_enabled = self.enabled["admin.service"]
        (d / "state.json").write_text(json.dumps(
            {"prefix": self.old_prefix, "enabled": self.enabled}))
        self.snapshot_dir = d
        self.say(f"Saved the unit files to {d}")

    def quiesce(self, prefix: str | None = None) -> None:
        """Stop the timers, then give a backup or health check that is
        already running time to finish. Nothing may start a `cb` job while
        the code and the database are being switched."""
        prefix = prefix or self.old_prefix
        for role in ("backup.timer", "doctor.timer"):
            self.sys.ctl("stop", names.unit(role, prefix))
        jobs = [names.unit(r, prefix)
                for r in ("backup.service", "doctor.service")]
        deadline = self.clock() + QUIESCE_SECONDS
        while self.clock() < deadline:
            running = [u for u in jobs if self.sys.busy(u)]
            if not running:
                return
            self.say("Waiting for " + ", ".join(running) + " to finish ...")
            self.sleep(5)
        for unit in jobs:
            if self.sys.busy(unit):
                self.say(f"{unit} did not finish in {QUIESCE_SECONDS} "
                         "seconds; stopping it")
                self.sys.ctl("stop", unit)

    def apply(self) -> None:
        company = self._company()
        self.say(f'Applying the update to company "{company}" ...')
        self.must("init", "--company", company)
        self._fetch_token()
        if self.st.legacy:
            self.notify(update.start_notice(
                self.cfg, self.st.old_version or "the old version",
                self.new_version))
        self.run_migrations()
        self.say("Refreshing engine-owned templates and skill links ...")
        self.must("templates", "refresh")
        self.must("bot", "relink")
        self.lib('install_links "$1" "$2"; source "$1/lib/python_alias.sh"; '
                 'ensure_python_alias', str(self.cfg.home))

    def _fetch_token(self) -> None:
        """One read of the Telegram token, kept in memory, so the result can
        be sent even if the database is later rolled back."""
        try:
            if not admin_live.has_token(self.cfg):
                return
            conn = connect(self.cfg)
            try:
                got = store.use_env(self.cfg, conn, chat.BOT, {
                    admin_live.TOKEN_VAR: admin_live.TOKEN_SECRET})
            finally:
                conn.close()
            self.token = (got.get(admin_live.TOKEN_VAR) or "").strip() or None
        except Exception:  # noqa: BLE001
            self.token = None

    def run_migrations(self) -> None:
        folder = self.engine / "migrations"
        found = sorted(p.name for p in folder.glob("*.sh")) \
            if folder.is_dir() else []
        try:
            applied = json.loads(update._meta(self.cfg, APPLIED_KEY) or "[]")
        except ValueError:
            applied = []
        todo = update.migrations_between(found, self.st.old_version,
                                         self.new_version, applied)
        env = dict(self.env, CB_ENGINE_DIR=str(self.engine),
                   CB_HOME_DIR=str(self.cfg.home),
                   CB_OLD_VERSION=self.st.old_version,
                   CB_NEW_VERSION=self.new_version,
                   CB_OLD_PREFIX=self.old_prefix)
        for name in todo:
            self.say(f"Running migration {name} ...")
            try:
                r = subprocess.run(["bash", str(folder / name)], env=env,
                                   timeout=MIGRATION_SECONDS)
            except subprocess.TimeoutExpired:
                raise Failed(f"migration {name} did not finish within "
                             f"{MIGRATION_SECONDS} seconds") from None
            except OSError as e:
                raise Failed(f"migration {name} could not run: {e}") from None
            if r.returncode != 0:
                raise Failed(f"migration {name} failed (exit {r.returncode})")
            applied = sorted(set(applied) | {name})
            self._set_meta(**{APPLIED_KEY: json.dumps(applied)})

    def switch_units(self) -> None:
        r = self.cb("config", "get", "run_memory_max", capture=True)
        memory = (r.stdout or "").strip()
        if r.returncode != 0 or not memory:
            raise Failed("could not read run_memory_max")
        user = pwd.getpwuid(os.getuid()).pw_name
        self.lib('install_units "$2" "$1" "$3" "$4"', str(self.cfg.home),
                 user, memory)
        if self.old_prefix != self.new_prefix:
            # A renamed engine: the old units must be gone before the new
            # ones start, or two dispatchers would share one database.
            self.say(f"Replacing the {self.old_prefix}-* units with "
                     f"{self.new_prefix}-* ...")
            for role in ("admin.service", "dispatcher.service",
                         "backup.timer", "doctor.timer", "backup.service",
                         "doctor.service"):
                self.sys.ctl("stop", self.old(role))
            for role in names.ENABLE_ROLES:
                self.sys.ctl("disable", self.old(role))
            for role in names.UNIT_ROLES:
                self.sys.remove(self.sys.unit_dir / self.old(role))
        self.sys.ctl("daemon-reload")
        for role in names.ENABLE_ROLES:
            if self.enabled.get(role):
                self.sys.ctl("enable", self.new(role))

    def start(self) -> None:
        self.say("Starting services ...")
        if not self.sys.ctl("start", self.new("dispatcher.service")):
            raise Failed("the dispatcher could not be started")
        self.cb("dispatcher", "release")
        for role in ("backup.timer", "doctor.timer"):
            if self.enabled.get(role):
                self.sys.ctl("start", self.new(role))
        if self.admin_enabled:
            self.marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            admin_live._write_marker(self.marker, {
                "old_version": self.st.old_version or "unknown",
                "new_version": self.new_version,
                "time": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")})
            if not self.sys.ctl("restart", self.new("admin.service")):
                raise Failed("the always-on Admin could not be restarted")
            self.say("Restarted the always-on Admin. The conversation "
                     "starts fresh.")
        else:
            self.say("The always-on Admin is not enabled, so it was not "
                     "started.")

    def verify(self) -> None:
        """The services must come back and stay up. An Admin that is up but
        silent is reported, not rolled back: the new code is running, and
        going back would not make Admin answer."""
        limit = float(self.env.get("CB_UPD_VERIFY_SECONDS") or VERIFY_SECONDS)
        steady = float(self.env.get("CB_UPD_STEADY_SECONDS")
                       or STEADY_SECONDS)
        self.say("Verifying services ...")
        deadline = self.clock() + limit
        since = None
        disp = adm = False
        while True:
            disp = self.sys.is_active(self.new("dispatcher.service"))
            adm = (not self.admin_enabled
                   or self.sys.is_active(self.new("admin.service")))
            now = self.clock()
            since = (since if since is not None else now) \
                if disp and adm else None
            answered = not self.admin_enabled or not self.marker.exists()
            up = since is not None and now - since >= steady
            if (up and answered) or now >= deadline:
                break
            self.sleep(2)
        self.say(f"{self.new('dispatcher.service')}: "
                 + ("PASS" if disp else "FAIL"))
        if self.admin_enabled:
            self.say(f"{self.new('admin.service')}: "
                     + ("PASS" if adm else "FAIL"))
        if not up:
            down = [self.new(r) for r, ok in (("dispatcher.service", disp),
                                              ("admin.service", adm))
                    if not ok] or ["a service kept restarting"]
            raise Failed("the services did not come back: " + ", ".join(down))
        if self.admin_enabled and self.marker.exists():
            self.admin_silent = True
            self.admin_reason = ("trust" if admin_live.waiting_on_trust()
                                 else "")
            try:
                self.marker.unlink()  # this job reports it; no second notice
            except OSError:
                pass

    def report(self) -> int:
        offline = int(self.clock() - (self.st.stopped_at or self.started))
        try:
            self._set_meta(**{update.SOURCE_KEY: self.st.label or "unknown",
                              update.OFFLINE_KEY: max(1, offline)})
        except sqlite3.Error:
            pass
        r = self.cb("doctor", capture=True, timeout=300)
        findings = [line[2:].strip() for line in (r.stdout or "").splitlines()
                    if line.startswith("- ")]
        self.say("")
        head = (f"{names.PRODUCT} updated: "
                f"{self.st.old_version or 'unknown'} -> {self.new_version}"
                + (f" ({self.st.label})" if self.st.label else "")
                + ". The services are running.")
        self.say(head)
        if self.st.backup:
            self.say(f"Database backup from before the update: "
                     f"{self.st.backup}")
        if self.st.kind == "adopt" and self.st.old_folder:
            self.say(f"The old engine folder is kept at {self.st.old_folder}."
                     f" Remove it when you are happy: rm -rf "
                     f"{self.st.old_folder}")
        parts = []
        if self.admin_silent:
            if self.admin_reason == "trust":
                parts.append(
                    "Admin is up but waiting on Claude Code's folder-trust "
                    "question, so it cannot answer here yet. Fix (on the "
                    "server): tmux attach -t admin-live, then choose "
                    "\"Yes, I trust this folder\".")
            else:
                parts.append(
                    "Admin is up but has not answered. Look at it on the "
                    "server with: tmux attach -t admin-live")
        if findings:
            shown = [f"- {text[:200]}" for text in findings[:6]]
            if len(findings) > 6:
                shown.append(f"- and {len(findings) - 6} more (cb doctor)")
            parts.append(
                f"Not caused by the update: {len(findings)} existing "
                "issue(s) need a decision.\n" + "\n".join(shown))
        if self.admin_silent or not self.admin_enabled:
            self.notify("\n\n".join([head] + parts))
        elif parts:
            # Admin confirmed the update itself; this adds only the rest.
            self.notify("\n\n".join(parts))
        self._prune()
        return 0

    def _prune(self) -> None:
        folder = self.cfg.home / "backups"
        for pattern, is_dir in (("pre-update-*.db", False),
                                ("pre-update-units-*", True)):
            try:
                found = sorted((p for p in folder.glob(pattern)
                                if p.is_dir() == is_dir),
                               key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                continue
            for old in found[KEEP_BACKUPS:]:
                if is_dir:
                    shutil.rmtree(old, ignore_errors=True)
                else:
                    try:
                        old.unlink()
                    except OSError:
                        continue
                self.say(f"Deleted {old.name}")

    # ---- going back ----

    def rollback(self) -> None:
        """Put back the old code, database and unit files, then start the
        services. Raises when a step fails; the caller does not try again."""
        self.say("Rolling back ...")
        for role in ("admin.service", "dispatcher.service", "backup.timer",
                     "doctor.timer"):
            self.sys.ctl("stop", self.new(role))
            if self.old_prefix != self.new_prefix:
                self.sys.ctl("stop", self.old(role))
        try:
            self.marker.unlink()
        except OSError:
            pass
        # 1. the code
        if self.st.kind == "adopt":
            if not self.st.old_folder or not self.st.old_folder.is_dir():
                raise Failed("the old engine folder is not where it was "
                             "left")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            failed = self.repo.with_name(f"{self.repo.name}.failed-{stamp}")
            os.rename(self.repo, failed)
            os.rename(self.st.old_folder, self.repo)
            self.say(f"The old engine folder is back at {self.repo}; the "
                     f"new one is kept at {failed}")
        else:
            if not self.st.old_ref:
                raise Failed("the old commit is not known")
            r = subprocess.run(["git", "-C", str(self.repo), "checkout",
                                "--quiet", "--force", "--detach",
                                self.st.old_ref], capture_output=True,
                               text=True, timeout=120)
            if r.returncode != 0:
                raise Failed("git could not go back to "
                             f"{self.st.old_ref}: {r.stderr.strip()[:200]}")
            self.say(f"The code is back at {self.st.old_ref}")
        # 2. the database
        if self.st.backup and self.st.backup.is_file():
            db = self.cfg.db_path
            for suffix in ("-wal", "-shm"):
                try:
                    os.unlink(f"{db}{suffix}")
                except OSError:
                    pass
            tmp = db.with_name(db.name + ".restore")
            shutil.copyfile(self.st.backup, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, db)
            self.say(f"The database is restored from {self.st.backup}")
        else:
            self.say("No database backup was passed on, so the database was "
                     "left as it is")
        # 3. the unit files
        if self.snapshot_dir is not None:
            if self.old_prefix != self.new_prefix:
                for role in names.ENABLE_ROLES:
                    self.sys.ctl("disable", self.new(role))
                for role in names.UNIT_ROLES:
                    self.sys.remove(self.sys.unit_dir / self.new(role))
            for role in names.UNIT_ROLES:
                saved = self.snapshot_dir / self.old(role)
                if saved.is_file() and not self.sys.install(
                        saved, self.sys.unit_dir / self.old(role)):
                    raise Failed(f"could not put back {self.old(role)}")
            self.sys.ctl("daemon-reload")
            for role in names.ENABLE_ROLES:
                if self.enabled.get(role):
                    self.sys.ctl("enable", self.old(role))
        # 4. the services
        if not self.sys.ctl("start", self.old("dispatcher.service")):
            raise Failed("the dispatcher did not start after the rollback")
        try:
            self.cb("dispatcher", "release", capture=True, timeout=60)
        except Failed:
            pass
        for role in ("backup.timer", "doctor.timer"):
            if self.enabled.get(role):
                self.sys.ctl("start", self.old(role))
        if self.admin_enabled:
            self.sys.ctl("restart", self.old("admin.service"))
        deadline = self.clock() + 60
        while not self.sys.is_active(self.old("dispatcher.service")):
            if self.clock() >= deadline:
                raise Failed("the dispatcher is not running after the "
                             "rollback")
            self.sleep(2)
        self.say("The services are running on the old version again.")

    def manual_steps(self) -> str:
        db = self.cfg.db_path
        code = (f"mv {self.repo} {self.repo}.failed && mv "
                f"{self.st.old_folder} {self.repo}"
                if self.st.kind == "adopt"
                else f"git -C {self.repo} checkout --force --detach "
                     f"{self.st.old_ref}")
        return "\n".join([
            f"To go back to {self.st.old_version or 'the old version'} by "
            "hand:",
            "  1. Stop the services:",
            f"       sudo systemctl stop {self.new('dispatcher.service')} "
            f"{self.new('admin.service')}",
            "  2. Put the old code back:",
            f"       {code}",
            "  3. Restore the database:",
            f"       rm -f {db}-wal {db}-shm",
            f"       cp {self.st.backup} {db}",
            "  4. Put the old unit files back and reload:",
            f"       sudo cp {self.snapshot_dir}/*.service "
            f"{self.snapshot_dir}/*.timer {self.sys.unit_dir}/",
            "       sudo systemctl daemon-reload",
            "  5. Start the services again:",
            f"       sudo systemctl start {self.old('dispatcher.service')}",
            f"       sudo systemctl start {self.old('admin.service')}   "
            "(only if it is enabled)",
            "  6. Check the install: cb doctor",
        ])

    def fail(self, why: str) -> int:
        self.say("")
        self.say(f"The update FAILED: {why}")
        old = self.st.old_version or "the old version"
        log = f" Log: {self.st.log}" if self.st.log else ""
        try:
            self.rollback()
        except Exception as e:  # noqa: BLE001  # one try only, never a loop
            self.say(f"The automatic rollback FAILED too: {e}")
            self.say(self.manual_steps())
            self.notify(
                f"{names.PRODUCT} update to {self.new_version} FAILED "
                f"({why}), and the automatic rollback failed too ({e}). "
                "The services may be down. The steps to go back by hand are "
                f"at the end of the update log.{log}")
            return ROLLBACK_FAILED
        self.notify(
            f"{names.PRODUCT} update to {self.new_version} failed ({why}). "
            f"It was rolled back by itself: the code is {old} again, the "
            "database is the copy taken just before the update, and the "
            "services are running. Anything recorded during the update "
            f"itself is gone.{log}")
        return ROLLED_BACK

    def run(self) -> int:
        try:
            self.snapshot()
            self.quiesce()
            self.apply()
            self.switch_units()
            self.start()
            self.verify()
        except Failed as e:
            return self.fail(str(e))
        except Exception as e:  # noqa: BLE001
            return self.fail(f"{type(e).__name__}: {e}")
        try:
            return self.report()
        except Exception as e:  # noqa: BLE001  # the update itself is done
            self.say(f"note: the final report hit a problem "
                     f"({type(e).__name__}: {e}); the update is in place")
            return 0


def main() -> int:
    cfg = Config.load()
    st = State.from_env()
    return Finisher(cfg, st).run()


if __name__ == "__main__":
    sys.exit(main())
