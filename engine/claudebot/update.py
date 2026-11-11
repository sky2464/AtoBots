"""Engine updates: look before anything changes (`cb update --check`), bring
in a local build, and start the update job in a standalone systemd unit.

The job has two halves. `engine/update.sh` (or `engine/adopt.sh`) of the
installed version backs up, holds the queue, stops the dispatcher and switches
the code. `update_finish.py` of the NEW version then finishes: schema,
migrations, units, restart, verification and, on failure, the rollback.

Nothing in this module changes the running install except `import_local`,
which adds a commit on the `local-builds` branch of the engine clone (the
checkout is not moved), and `run_update`, which starts the job.
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import names
from .config import Config

UNIT_NAME = names.unit("update")
TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
LOCAL_BRANCH = "local-builds"
LOCAL_MARKER = "engine/LOCAL_BUILD"
SOURCE_KEY = "engine_source"            # meta: tag:v0.0.3 or local:<sha>
OFFLINE_KEY = "update.last_offline_seconds"
DEFAULT_OFFLINE_SECONDS = 120
MAX_ZIP_FILES = 20000
MAX_ZIP_BYTES = 300 * 1024 * 1024
SKIP_DIRS = frozenset({".git", "__pycache__", ".remember", ".venv",
                       ".superpowers", "backups", "node_modules"})
SKIP_FILES = frozenset({".DS_Store"})
# Files that must be runnable whatever the zip tool did with permissions.
_EXECUTABLE = re.compile(
    r"^engine/(bin/cb|[^/]+\.sh|lib/update_finish\.sh|migrations/[^/]+\.sh)$")
_VERSION = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:\+local\.([0-9a-f]{7,64}))?$")
_VERSION_LINE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
_SCHEMA_LINE = re.compile(r"^SCHEMA_VERSION\s*=\s*(\d+)", re.M)
_MIGRATION = re.compile(r"^(\d+\.\d+\.\d+)-[a-z0-9][a-z0-9-]*\.sh$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class UpdateError(Exception):
    """A refusal or failure with a message for the person reading it."""


# ---- versions ----

def version_key(text: str) -> tuple:
    """(major, minor, patch). The `+local.<sha>` suffix is ignored, so a
    local build orders like the release it was built from."""
    m = _VERSION.match((text or "").strip())
    if not m:
        raise UpdateError(f"not a version: {text!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_local(text: str) -> bool:
    m = _VERSION.match((text or "").strip())
    return bool(m and m.group(4))


def base_version(text: str) -> str:
    return ".".join(str(n) for n in version_key(text))


def migrations_between(file_names, old: str | None, new: str,
                       applied=()) -> list:
    """The migration scripts an update from `old` to `new` runs, in order.

    A script named `<version>-<name>.sh` runs when old < version <= new. One
    for exactly the new version also runs when it is not recorded as applied
    yet, so a second local build of the same version can still add one. An
    unknown old version runs everything up to the new one (the scripts are
    safe to repeat)."""
    new_key = version_key(new)
    try:
        old_key = version_key(old) if old else (0, 0, 0)
    except UpdateError:
        old_key = (0, 0, 0)
    done = set(applied)
    picked = []
    for name in file_names:
        m = _MIGRATION.match(name)
        if not m:
            continue
        key = version_key(m.group(1))
        if key > new_key:
            continue
        if key > old_key or (key == new_key and name not in done):
            picked.append((key, name))
    return [name for _, name in sorted(picked)]


# ---- git and trees ----

def engine_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def repo_dir() -> Path:
    return engine_dir().parent


def git(repo, *args, check: bool = True, timeout: int = 120, env=None,
        cwd=None, strip: bool = True) -> str:
    try:
        r = subprocess.run(["git", "-C", str(cwd or repo), *args],
                           capture_output=True, text=True, timeout=timeout,
                           env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        if check:
            raise UpdateError(f"git {args[0]} did not finish: {e}") from None
        return ""
    if check and r.returncode != 0:
        raise UpdateError(
            f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    if r.returncode != 0:
        return ""
    return r.stdout.strip() if strip else r.stdout


def is_clone(repo) -> bool:
    """True when `repo` is the top folder of a Git clone (not a copy, and not
    a folder that merely sits inside some other repository)."""
    top = git(repo, "rev-parse", "--show-toplevel", check=False)
    if not top:
        return False
    try:
        return Path(top).resolve() == Path(repo).resolve()
    except OSError:
        return False


def blob_sha(data: bytes) -> str:
    """The id Git gives this content, so a folder can be compared with a
    commit without writing anything into the clone."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class DirTree:
    """An engine tree in a folder (a copied install, or an unpacked build)."""

    def __init__(self, root):
        self.root = Path(root)

    def files(self) -> dict:
        out = {}
        for base, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in files:
                if name in SKIP_FILES or name.endswith(".pyc"):
                    continue
                path = Path(base) / name
                rel = path.relative_to(self.root).as_posix()
                try:
                    if path.is_symlink():
                        data = os.readlink(path).encode()
                    else:
                        data = path.read_bytes()
                except OSError:
                    continue
                out[rel] = blob_sha(data)
        return out

    def read(self, rel: str) -> bytes | None:
        try:
            return (self.root / rel).read_bytes()
        except OSError:
            return None


class RefTree:
    """An engine tree at a commit or tag of the clone."""

    def __init__(self, repo, ref: str):
        self.repo, self.ref = Path(repo), ref

    def files(self) -> dict:
        out = {}
        listing = git(self.repo, "ls-tree", "-r", "-z", self.ref)
        for entry in listing.split("\0"):
            if not entry.strip():
                continue
            meta, _, rel = entry.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                out[rel] = parts[2]
        return out

    def read(self, rel: str) -> bytes | None:
        try:
            r = subprocess.run(
                ["git", "-C", str(self.repo), "show", f"{self.ref}:{rel}"],
                capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None


def _package_file(tree, files: dict, leaf: str) -> bytes | None:
    """`engine/<package>/<leaf>`, whatever the package folder is called."""
    for rel in sorted(files):
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "engine" and parts[2] == leaf:
            data = tree.read(rel)
            if data is not None and (leaf != "__init__.py"
                                     or b"__version__" in data):
                return data
    return None


def tree_version(tree, files: dict | None = None) -> str | None:
    """The version a tree reports, with the local-build suffix if it has
    the marker file."""
    files = tree.files() if files is None else files
    data = _package_file(tree, files, "__init__.py")
    if data is None:
        return None
    m = _VERSION_LINE.search(data.decode("utf-8", "replace"))
    if not m:
        return None
    version = m.group(1)
    marker = tree.read(LOCAL_MARKER) if LOCAL_MARKER in files else None
    if marker:
        sha = marker.decode("ascii", "replace").strip()
        if re.fullmatch(r"[0-9a-f]{7,64}", sha):
            version = f"{version}+local.{sha[:7]}"
    return version


def tree_schema(tree, files: dict | None = None) -> int | None:
    files = tree.files() if files is None else files
    data = _package_file(tree, files, "db.py")
    if data is None:
        return None
    m = _SCHEMA_LINE.search(data.decode("utf-8", "replace"))
    return int(m.group(1)) if m else None


# ---- local builds ----

def validate_zip(path) -> None:
    """Refuse a zip that could write outside its own folder, or that is not
    an engine tree. Raises UpdateError with the reason."""
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise UpdateError(f"cannot read {path}: {e}") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise UpdateError("the zip holds too many files")
        if sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise UpdateError("the zip is too large when unpacked")
        names_seen = set()
        for info in infos:
            name = info.filename
            if (name.startswith(("/", "\\")) or "\\" in name
                    or re.match(r"^[A-Za-z]:", name)):
                raise UpdateError(f"the zip has an absolute path: {name!r}")
            parts = PurePosixPath(name).parts
            if ".." in parts:
                raise UpdateError(f"the zip has a '..' path: {name!r}")
            if stat.S_ISLNK(info.external_attr >> 16):
                target = zf.read(info).decode("utf-8", "replace")
                joined = posixpath.normpath(
                    posixpath.join(posixpath.dirname(name), target))
                if (target.startswith("/") or joined == ".."
                        or joined.startswith("../")):
                    raise UpdateError(
                        f"the zip has a link that points outside: {name!r}")
            names_seen.add(name.rstrip("/"))
        if _zip_prefix(names_seen) is None:
            raise UpdateError("the zip is not an engine build: it has no "
                              "engine/update.sh")


def _zip_prefix(names_seen: set) -> str | None:
    """'' when engine/ is at the top, 'folder/' when the zip wraps the tree
    in one folder (as a download from a code host does), else None."""
    if "engine/update.sh" in names_seen:
        return ""
    tops = {n.split("/", 1)[0] for n in names_seen if n}
    if len(tops) == 1:
        top = next(iter(tops))
        if f"{top}/engine/update.sh" in names_seen:
            return top + "/"
    return None


def extract_zip(path, dest) -> Path:
    """Unpack a validated zip into `dest` (a fresh folder). Returns the
    folder that holds `engine/`."""
    validate_zip(path)
    dest = Path(dest)
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename
            target = dest / name
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                os.symlink(zf.read(info).decode("utf-8", "replace"), target)
                continue
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            target.chmod(0o755 if mode & 0o111 else 0o644)
    return find_tree_root(dest)


def find_tree_root(folder) -> Path:
    folder = Path(folder)
    if (folder / "engine" / "update.sh").is_file():
        return folder
    inner = [p for p in folder.iterdir() if p.name not in SKIP_FILES]
    if len(inner) == 1 and (inner[0] / "engine" / "update.sh").is_file():
        return inner[0]
    raise UpdateError(f"{folder} is not an engine build: it has no "
                      "engine/update.sh")


def validate_tree(root) -> None:
    """A folder used as a local build: no link may point outside it."""
    root = Path(root).resolve()
    if not (root / "engine" / "bin" / "cb").is_file():
        raise UpdateError(f"{root} is not an engine build: engine/bin/cb is "
                          "missing")
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in list(dirs) + files:
            p = Path(base) / name
            if p.is_symlink():
                try:
                    p.resolve().relative_to(root)
                except (OSError, ValueError):
                    raise UpdateError(
                        f"{p} is a link that points outside the build"
                    ) from None


def fix_modes(root) -> None:
    root = Path(root)
    for base, dirs, files in os.walk(root / "engine"):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            p = Path(base) / name
            if _EXECUTABLE.match(p.relative_to(root).as_posix()) \
                    and not p.is_symlink():
                p.chmod(0o755)


def source_sha(source, root=None) -> str:
    """What identifies a local build: the zip's own hash, or a hash of every
    file of a folder."""
    source = Path(source)
    h = hashlib.sha256()
    if source.is_file():
        with open(source, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    files = DirTree(root or source).files()
    files.pop(LOCAL_MARKER, None)
    for rel in sorted(files):
        h.update(f"{rel} {files[rel]}\n".encode())
    return h.hexdigest()


def work_dir(home) -> Path:
    """Scratch space for unpacking, under the company home and never inside
    the engine clone."""
    d = Path(home) / ".update"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    ignore = d / ".gitignore"
    if not ignore.exists():
        # The company home is a Git repository; none of this belongs in it.
        ignore.write_text("*\n")
    return d


class Unpacked:
    """A local build unpacked into a temp folder: `with Unpacked(...) as u`
    gives `u.root` (holds engine/) and `u.sha`."""

    def __init__(self, home, zip_path=None, dir_path=None):
        self.home, self.zip_path, self.dir_path = home, zip_path, dir_path
        self._tmp = None
        self.root = None
        self.sha = None
        self.name = ""

    def __enter__(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="build-",
                                          dir=work_dir(self.home)))
        try:
            if self.zip_path:
                src = Path(self.zip_path)
                if not src.is_file():
                    raise UpdateError(f"there is no file {src}")
                self.root = extract_zip(src, self._tmp)
                self.sha = source_sha(src)
            else:
                src = Path(self.dir_path)
                if not src.is_dir():
                    raise UpdateError(f"there is no folder {src}")
                found = find_tree_root(src)
                validate_tree(found)
                self.root = self._tmp / "tree"
                shutil.copytree(
                    found, self.root, symlinks=True,
                    ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"))
                self.sha = source_sha(src, self.root)
            validate_tree(self.root)
            fix_modes(self.root)
            self.name = _SAFE_NAME.sub("_", src.name)[:60] or "build"
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
        return False


def import_local(repo, root, sha: str, name: str) -> str:
    """Commit the unpacked build at `root` to the `local-builds` branch of
    the clone and return the commit. The clone's checkout and index are not
    touched: a private index and Git's plumbing commands do the work.

    The commit carries engine/LOCAL_BUILD with the build's hash. That file is
    what makes `cb version` say `<version>+local.<sha>`; a release never has
    it, so a local build cannot pass for the release of the same number."""
    repo, root = Path(repo), Path(root)
    if not is_clone(repo):
        raise UpdateError(f"{repo} is not a Git clone; a copied install is "
                          "adopted first (cb update --adopt)")
    (root / LOCAL_MARKER).write_text(sha + "\n")
    git_dir = git(repo, "rev-parse", "--absolute-git-dir")
    fd, index = tempfile.mkstemp(prefix="cb-index-")
    os.close(fd)
    os.unlink(index)
    env = dict(os.environ, GIT_INDEX_FILE=index, GIT_DIR=git_dir,
               GIT_WORK_TREE=str(root),
               GIT_AUTHOR_NAME="local build",
               GIT_AUTHOR_EMAIL="local-build@localhost",
               GIT_COMMITTER_NAME="local build",
               GIT_COMMITTER_EMAIL="local-build@localhost")
    try:
        git(repo, "add", "-A", env=env, cwd=root)
        git(repo, "add", "-f", LOCAL_MARKER, env=env, cwd=root)
        tree = git(repo, "write-tree", env=env, cwd=root)
        ref = f"refs/heads/{LOCAL_BRANCH}"
        tip = git(repo, "rev-parse", "-q", "--verify", ref, check=False)
        if tip and git(repo, "rev-parse", f"{tip}^{{tree}}",
                       check=False) == tree:
            return tip  # the same build, imported before
        # A brand-new repository (adopting from a zip) has no commit yet.
        parent = tip or git(repo, "rev-parse", "-q", "--verify", "HEAD",
                            check=False)
        link = ["-p", parent] if parent else []
        commit = git(repo, "commit-tree", tree, *link, "-m",
                     f"local build {sha[:12]} from {name}", env=env, cwd=root)
        git(repo, "update-ref", ref, commit)
        return commit
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass


# ---- what is running ----

def running_version() -> str:
    from . import full_version
    return full_version()


def _meta(cfg: Config, key: str) -> str | None:
    """One meta value, read without changing or creating the database."""
    if not cfg.db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
        return None if row is None else row[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def active_runs(cfg: Config) -> int:
    if not cfg.db_path.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM runs WHERE ended_at IS NULL"
            " AND COALESCE(lane,'') NOT IN ('chat','live')").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def running_source(cfg: Config, repo) -> str:
    """Where the running engine came from, in one word plus a name."""
    version = running_version()
    if is_local(version):
        return "local:" + version.split("+local.", 1)[1]
    recorded = _meta(cfg, SOURCE_KEY)
    if recorded and not recorded.startswith("local:"):
        return recorded
    if not is_clone(repo):
        return "copy"
    tag = git(repo, "describe", "--tags", "--exact-match", check=False)
    if tag:
        return f"tag:{tag}"
    return "commit:" + git(repo, "rev-parse", "--short", "HEAD", check=False)


def offline_estimate(cfg: Config) -> tuple:
    """(seconds, measured). The last update's own number when there is one."""
    raw = _meta(cfg, OFFLINE_KEY)
    try:
        seconds = int(raw)
        if 0 < seconds < 6 * 3600:
            return seconds, True
    except (TypeError, ValueError):
        pass
    return DEFAULT_OFFLINE_SECONDS, False


def minutes_text(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"about {minutes} minute" + ("" if minutes == 1 else "s")


def _admin_enabled() -> bool | None:
    program = os.environ.get("CB_SYSTEMCTL") or "systemctl"
    try:
        r = subprocess.run([program, "is-enabled", "--quiet",
                            names.unit("admin.service")], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


# ---- the check ----

def newest_tag(repo) -> str | None:
    listed = git(repo, "tag", "--list", "v*", "--sort=-v:refname",
                 check=False)
    for line in listed.splitlines():
        if TAG_PATTERN.match(line.strip()):
            return line.strip()
    return None


def check(cfg: Config, tag: str | None = None, zip_path=None, dir_path=None,
          repo=None, fetch: bool = True, admin_enabled_fn=None) -> dict:
    """Everything an update would do, without doing any of it."""
    repo = Path(repo) if repo else repo_dir()
    clone = is_clone(repo)
    current = running_version()
    report = {
        "current_version": current,
        "current_source": running_source(cfg, repo),
        "is_clone": clone,
        "needs_adopt": not clone,
        "dirty": [],
        "target": None,
        "relation": "unknown",
        "files_changed": None,
        "includes": [],
        "schema_change": None,
        "migrations": [],
        "active_runs": active_runs(cfg),
        "restarts": [],
        "blockers": [],
        "notes": [],
    }
    if clone:
        # Each line is "XY path"; X may be a space, so it is cut, not stripped.
        status = git(repo, "status", "--porcelain", check=False, strip=False)
        report["dirty"] = [line[3:] for line in status.splitlines()
                           if len(line) > 3][:20]
        if report["dirty"]:
            report["blockers"].append(
                f"the engine folder has local edits ({len(report['dirty'])} "
                "file(s)); commit or discard them first")
    else:
        report["blockers"].append(
            "this install is a copy with no Git history; it must be adopted "
            "first (cb update --adopt)")
    here = RefTree(repo, "HEAD") if clone else DirTree(repo)
    here_files = here.files()
    # What is on disk is what an update replaces, so the version comes from
    # there; the code in memory is only the fallback.
    current = tree_version(here, here_files) or current
    report["current_version"] = current
    here_files.pop(LOCAL_MARKER, None)

    def compare(tree, kind: str, label: str, verified: bool) -> None:
        files = tree.files()
        version = tree_version(tree, files)
        files.pop(LOCAL_MARKER, None)
        if kind == "local" and version and not is_local(version):
            version = f"{version}+local.{label.split(':', 1)[1][:7]}"
        report["target"] = {"kind": kind, "label": label, "version": version,
                            "verified": verified}
        changed = sorted(p for p in set(files) | set(here_files)
                         if files.get(p) != here_files.get(p))
        report["files_changed"] = len(changed)
        if any(p.startswith("engine/templates/") for p in changed):
            report["includes"].append("bot templates and rules")
        if any(p.startswith("engine/systemd/") for p in changed):
            report["includes"].append("service unit files")
        old_schema, new_schema = tree_schema(here, here_files), \
            tree_schema(tree, files)
        if old_schema and new_schema and old_schema != new_schema:
            report["schema_change"] = f"{old_schema} -> {new_schema}"
        if version:
            try:
                a, b = version_key(current), version_key(version)
                report["relation"] = ("newer" if b > a else "older" if b < a
                                      else "same")
                scripts = [p.rsplit("/", 1)[1] for p in files
                           if p.startswith("engine/migrations/")]
                applied = json.loads(_meta(cfg, "migrations.applied") or "[]")
                report["migrations"] = migrations_between(
                    scripts, current, version, applied)
            except (UpdateError, ValueError):
                pass
        if report["relation"] == "older":
            report["notes"].append(
                "the target is OLDER than what is running; going back is "
                "only done on purpose")
        if "engine/lib/update_finish.sh" not in files:
            report["blockers"].append(
                "the target is from before 0.0.3 and cannot finish its own "
                "update; see UPDATING.md, \"Roll back on purpose\"")
        if kind == "tag" and is_local(current) and changed:
            report["notes"].append(
                "a local build is running; moving to the release replaces "
                f"it ({len(changed)} file(s) differ)")
        if not changed:
            report["notes"].append("the files are identical to what is "
                                   "running; there is nothing to install")

    if zip_path or dir_path:
        try:
            with Unpacked(cfg.home, zip_path, dir_path) as build:
                compare(DirTree(build.root), "local",
                        f"local:{build.sha[:12]}", False)
            report["target"]["file"] = Path(zip_path or dir_path).name
            report["notes"].append(
                "unverified local build: nothing proves who made this file. "
                "It runs with the bots' account. Install it only on the "
                "Owner's direct yes")
        except UpdateError as e:
            report["blockers"].append(str(e))
    elif clone:
        if fetch:
            try:
                subprocess.run(["git", "-C", str(repo), "fetch", "--tags",
                                "--quiet", "origin"], capture_output=True,
                               timeout=60, check=True)
            except (OSError, subprocess.SubprocessError):
                report["notes"].append(
                    "the repository could not be reached; this uses the "
                    "releases already known here")
        want = tag or newest_tag(repo)
        if want is None:
            report["blockers"].append("no release tag (v*) is known here")
        elif not TAG_PATTERN.match(want):
            report["blockers"].append("a release tag looks like v0.0.1")
        elif not git(repo, "rev-parse", "-q", "--verify",
                     f"refs/tags/{want}^{{commit}}", check=False):
            report["blockers"].append(f"there is no release {want}")
        else:
            key = git(repo, "config", "--get",
                      f"{names.UNIT_PREFIX}.releaseKey", check=False)
            compare(RefTree(repo, f"refs/tags/{want}"), "tag", f"tag:{want}",
                    bool(key))
            if not key:
                report["notes"].append(
                    "release tags are not verified on this server (see "
                    "UPDATING.md, \"Verifying releases\")")
    enabled = (admin_enabled_fn or _admin_enabled)()
    report["admin_enabled"] = enabled
    report["restarts"] = ["the dispatcher"]
    if enabled:
        report["restarts"].append("the always-on Admin (the chat restarts)")
    seconds, measured = offline_estimate(cfg)
    report["offline_seconds"] = seconds
    report["offline_measured"] = measured
    report["ready"] = (not report["blockers"] and report["target"] is not None
                       and report["files_changed"] != 0)
    return report


def start_command(report: dict, tag=None, zip_path=None, dir_path=None) -> str:
    if zip_path:
        return f"cb update --zip {zip_path} --change C-N"
    if dir_path:
        return f"cb update --dir {dir_path} --change C-N"
    target = report.get("target") or {}
    label = str(target.get("label") or "")
    return "cb update " + (tag or label.removeprefix("tag:") or "TAG")


def format_check(report: dict, command: str = "") -> str:
    """The report as a few plain sentences, for Admin to pass on."""
    out = [f"Running now: {names.PRODUCT} {report['current_version']} "
           f"({report['current_source']})."]
    target = report.get("target")
    if target:
        what = {"newer": "newer than what is running",
                "older": "OLDER than what is running",
                "same": "the same version number",
                "unknown": "version unknown"}[report["relation"]]
        if target["kind"] == "local":
            source = (f"an unverified local build "
                      f"({target.get('file', 'folder')}, "
                      f"{target['label']})")
        else:
            source = "release " + target["label"].removeprefix("tag:")
        out.append(f"Target: {target['version'] or 'unknown version'} from "
                   f"{source}; {what}.")
        parts = [f"{report['files_changed']} file(s) differ"]
        parts += report["includes"]
        if report["schema_change"]:
            parts.append("a database change (schema "
                         f"{report['schema_change']})")
        if report["migrations"]:
            parts.append("migration step(s): "
                         + ", ".join(report["migrations"]))
        out.append("What changes: " + "; ".join(parts) + ".")
    if report["active_runs"]:
        out.append(f"{report['active_runs']} bot run(s) are active now; the "
                   "update waits for them to finish (up to 30 minutes).")
    how = ("measured at the last update" if report["offline_measured"]
           else "an estimate")
    out.append("It restarts " + " and ".join(report["restarts"]) + ". The "
               f"chat is offline for {minutes_text(report['offline_seconds'])}"
               f" ({how}). The database is backed up first, and a failed "
               "update rolls itself back.")
    for note in report["notes"]:
        out.append("Note: " + note + ".")
    if report["blockers"]:
        out.append("Not ready: " + "; ".join(report["blockers"]) + ".")
    elif report["ready"]:
        out.append("Ready." + (f" Start it with: {command}" if command else ""))
    else:
        out.append("Nothing to do.")
    return "\n".join(out)


# ---- starting the job ----

def is_unit_active(unit: str = UNIT_NAME) -> bool:
    program = os.environ.get("CB_SYSTEMCTL") or "systemctl"
    try:
        r = subprocess.run([program, "is-active", "--quiet", unit],
                           timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_command(cfg: Config, args, log_path: Path, force: bool = False,
                  script_path: Path | None = None) -> list[str]:
    """The systemd-run line. The job runs in its own unit so it survives the
    restart of whoever asked for it."""
    if script_path is None:
        script_path = engine_dir() / "update.sh"
    if isinstance(args, str):
        args = [args]
    cmd = [
        "sudo", "systemd-run",
        f"--unit={UNIT_NAME}",
        "--collect",
        "--no-block",
        f"--setenv={names.HOME_VAR}={cfg.home}",
        f"--setenv=CB_UPDATE_LOG={log_path}",
        f"--uid={cfg.run_user}",
        "-p", f"StandardOutput=file:{log_path}",
        "-p", f"StandardError=file:{log_path}",
        str(script_path),
        *args,
    ]
    if force:
        cmd.append("--force")
    return cmd


def _start_job(cfg: Config, args: list, force: bool, script_path=None,
               is_active_fn=None, runner=None) -> int:
    is_active_fn = is_active_fn or is_unit_active
    runner = runner or subprocess.run
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
    cmd = build_command(cfg, args, log_path, force=force,
                        script_path=script_path)
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
    print("The Owner gets a Telegram notice before the chat goes offline, "
          "and a result afterwards. A failed update rolls itself back.")
    return 0


def run_update(cfg: Config, tag: str | None = None, force: bool = False,
               is_active_fn=None, runner=None, repo=None) -> int:
    """Update to a release tag (the newest when none is named)."""
    if tag is not None and not TAG_PATTERN.match(tag):
        print("error: TAG must look like v0.0.1", file=sys.stderr)
        return 2
    return _start_job(cfg, [tag] if tag else [], force,
                      is_active_fn=is_active_fn, runner=runner)


def owner_yes(conn, change_id: str | None) -> None:
    """A local build is unverified code, so it needs the Owner's recorded
    yes: an approved change plan, unless a person is at the server."""
    from . import activity, changes
    if activity.caller(conn).actor == "cli":
        return
    if not change_id:
        raise UpdateError(
            "a local build needs the Owner's yes. Plan it with `cb change "
            "plan`, record their exact words with `cb change approve`, then "
            "pass --change C-N")
    try:
        row = changes.get(conn, change_id)
    except (ValueError, KeyError) as e:
        raise UpdateError(str(e)) from None
    if row["status"] != changes.STATUS_APPROVED:
        raise UpdateError(f"change {row['id']} is {row['status']}, not "
                          "approved by the Owner")


def run_local(cfg: Config, zip_path=None, dir_path=None, force: bool = False,
              change_id: str | None = None, repo=None, is_active_fn=None,
              runner=None, conn=None) -> int:
    """Import a zip or folder as a local build, then update to it."""
    from .db import connect
    repo = Path(repo) if repo else repo_dir()
    own = conn is None
    if own:
        conn = connect(cfg)
    try:
        owner_yes(conn, change_id)
    finally:
        if own:
            conn.close()
    if not is_clone(repo):
        raise UpdateError("this install is a copy with no Git history; run "
                          "cb update --adopt with the same --zip or --dir")
    with Unpacked(cfg.home, zip_path, dir_path) as build:
        commit = import_local(repo, build.root, build.sha, build.name)
        sha = build.sha
    print(f"Imported the local build as {commit[:12]} on {LOCAL_BRANCH} "
          f"(unverified local build {sha[:12]}).")
    return _start_job(cfg, ["--ref", commit, "--label", f"local:{sha[:12]}"],
                      force, is_active_fn=is_active_fn, runner=runner)


def run_adopt(cfg: Config, zip_path=None, dir_path=None, repo_url=None,
              tag=None, force: bool = False, change_id=None, repo=None,
              is_active_fn=None, runner=None, conn=None) -> int:
    """Turn a copied install into a clone, in one detached job."""
    from .db import connect
    repo = Path(repo) if repo else repo_dir()
    if is_clone(repo):
        raise UpdateError("this install is already a Git clone; use cb "
                          "update")
    args = ["--old-engine", str(repo / "engine")]
    if zip_path or dir_path:
        own = conn is None
        if own:
            conn = connect(cfg)
        try:
            owner_yes(conn, change_id)
        finally:
            if own:
                conn.close()
        # Unpacked now and kept: the job reads it after this command ends.
        build = Unpacked(cfg.home, zip_path, dir_path).__enter__()
        args += ["--source", str(build.root), "--cleanup", str(build._tmp)]
    elif repo_url:
        if not re.fullmatch(r"[A-Za-z0-9@:/._~+-]+", repo_url):
            raise UpdateError("the repository address has characters that "
                              "are not allowed")
        args += ["--repo", repo_url]
        if tag:
            if not TAG_PATTERN.match(tag):
                raise UpdateError("TAG must look like v0.0.1")
            args += ["--tag", tag]
    else:
        raise UpdateError("--adopt needs a source: --repo URL [TAG], --zip "
                          "FILE or --dir FOLDER")
    return _start_job(cfg, args, force, script_path=engine_dir() / "adopt.sh",
                      is_active_fn=is_active_fn, runner=runner)


def start_notice(cfg: Config, old: str, new: str) -> str:
    seconds, _ = offline_estimate(cfg)
    return (f"Updating {names.PRODUCT} from {old} to {new} now. The chat "
            f"will be offline for {minutes_text(seconds)}. You will get a "
            "message when it is back, or if it had to be rolled back.")


# ---- CLI ----

def _cmd_update(args: argparse.Namespace) -> int:
    cfg = Config.load()
    local = bool(args.zip or args.dir)
    if args.tag and local:
        print("error: give a TAG, or --zip/--dir, not both", file=sys.stderr)
        return 2
    if args.zip and args.dir:
        print("error: give --zip or --dir, not both", file=sys.stderr)
        return 2
    try:
        if args.check:
            report = check(cfg, tag=args.tag, zip_path=args.zip,
                           dir_path=args.dir)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(format_check(report, start_command(
                    report, args.tag, args.zip, args.dir)))
            return 0 if not report["blockers"] else 1
        if args.adopt:
            return run_adopt(cfg, zip_path=args.zip, dir_path=args.dir,
                             repo_url=args.repo, tag=args.tag,
                             force=bool(args.force), change_id=args.change)
        if local:
            return run_local(cfg, zip_path=args.zip, dir_path=args.dir,
                             force=bool(args.force), change_id=args.change)
        if not is_clone(repo_dir()):
            raise UpdateError(
                "this install is a copy with no Git history; run cb update "
                "--adopt --repo URL (or --zip FILE) once to turn it into a "
                "clone")
        return run_update(cfg, args.tag, force=bool(args.force))
    except (UpdateError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _cmd_notice(args: argparse.Namespace) -> int:
    """Used by the update job: tell the Owner before the chat goes offline.
    Best effort; never fails the job."""
    try:
        from . import admin_live
        from .db import connect
        cfg = Config.load()
        text = start_notice(cfg, args.old, args.new)
        print(text)
        conn = connect(cfg)
        try:
            admin_live.notify_telegram(cfg, text, conn=conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"note: the start notice was not sent ({type(e).__name__})")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    """Used by engine/adopt.sh: commit an unpacked build into a new clone."""
    try:
        root = find_tree_root(args.dir)
        validate_tree(root)
        fix_modes(root)
        sha = source_sha(root)
        commit = import_local(args.repo, root, sha,
                              _SAFE_NAME.sub("_", Path(args.dir).name)[:60])
    except UpdateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{commit} local:{sha[:12]}")
    return 0


def _cmd_finish(_args: argparse.Namespace) -> int:
    from . import update_finish
    return update_finish.main()


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "update", help=f"update {names.PRODUCT}, or see what an update "
                       "would do (--check)")
    p.add_argument("tag", nargs="?",
                   help="release tag (e.g. v0.0.1); the newest when omitted")
    p.add_argument("--check", action="store_true",
                   help="read-only: say what the update would do")
    p.add_argument("--json", action="store_true",
                   help="with --check: print the report as JSON")
    p.add_argument("--zip", help="a local build: a zip of the engine tree")
    p.add_argument("--dir", help="a local build: a folder with the engine "
                                 "tree")
    p.add_argument("--change", help="the approved change plan (C-N) that "
                                    "holds the Owner's yes to a local build")
    p.add_argument("--adopt", action="store_true",
                   help="turn a copied install (no .git) into a clone")
    p.add_argument("--repo", help="with --adopt: the repository address")
    p.add_argument("--force", action="store_true",
                   help="continue even if bot runs are still active")
    p.set_defaults(func=_cmd_update)
    f = subparsers.add_parser(
        "update-finish", help="second half of an update (the update job "
                              "runs it; not for people)")
    f.set_defaults(func=_cmd_finish)
    n = subparsers.add_parser(
        "update-notice", help="tell the Owner an update starts (the update "
                              "job runs it)")
    n.add_argument("--old", required=True)
    n.add_argument("--new", required=True)
    n.set_defaults(func=_cmd_notice)
    i = subparsers.add_parser(
        "update-import", help="commit an unpacked build as a local build "
                              "(engine/adopt.sh runs it)")
    i.add_argument("--repo", required=True)
    i.add_argument("--dir", required=True)
    i.set_defaults(func=_cmd_import)
