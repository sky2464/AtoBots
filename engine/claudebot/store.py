"""Secure store: gpg-encrypted secret items, never in argv, logs or stdout.

Each item is encrypted with `gpg --symmetric` (AES256) using a random key file
inside the 0700 `.secrets/` folder. This is a working rule plus file modes, not
a technical lock: every bot runs as the same server user.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from getpass import getpass

from . import activity
from .config import Config
from .db import connect

KINDS = ("token", "login", "contact")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MASK = b"***"


class Masker:
    """Streaming replacer: swaps secret values for *** and holds back
    max(len)-1 bytes so a value split across reads is still caught."""

    def __init__(self, values):
        vals = sorted({v for v in values if v}, key=len, reverse=True)
        self._values = vals
        self._hold = max((len(v) for v in vals), default=1) - 1
        self._buf = b""

    def _replace(self, data: bytes) -> bytes:
        for v in self._values:
            data = data.replace(v, _MASK)
        return data

    def feed(self, chunk: bytes) -> bytes:
        self._buf = self._replace(self._buf + chunk)
        if self._hold == 0:
            out, self._buf = self._buf, b""
            return out
        if len(self._buf) <= self._hold:
            return b""
        cut = len(self._buf) - self._hold
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return out

    def flush(self) -> bytes:
        out, self._buf = self._replace(self._buf), b""
        return out


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError("invalid secret name")


def _dir(cfg: Config):
    d = cfg.secrets_dir
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    gh = d / "gnupg"
    gh.mkdir(exist_ok=True)
    os.chmod(gh, 0o700)
    return d


def _write_private(path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _key_path(cfg: Config):
    d = _dir(cfg)
    kp = d / "store.key"
    if not kp.exists():
        # Write a private temp file, then link it into place: link() is atomic
        # and fails if another process won, so there is exactly one key.
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_hex(32).encode())
            try:
                os.link(tmp, kp)
            except FileExistsError:
                pass
        finally:
            os.unlink(tmp)
    return kp


@contextlib.contextmanager
def _lock(cfg: Config):
    """Exclusive lock for index and key changes (bots share one user)."""
    d = _dir(cfg)
    fd = os.open(d / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _gpg(cfg: Config, *args: str) -> list[str]:
    return ["gpg", "--homedir", str(cfg.secrets_dir / "gnupg"), "--batch",
            "--yes", "--quiet", "--no-tty", "--pinentry-mode", "loopback",
            "--passphrase-file", str(_key_path(cfg)), *args]


def _item_path(cfg: Config, name: str):
    return cfg.secrets_dir / (hashlib.sha256(name.encode()).hexdigest() + ".gpg")


def _read_index(cfg: Config) -> dict:
    p = cfg.secrets_dir / "index.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _write_index(cfg: Config, idx: dict) -> None:
    _write_private(cfg.secrets_dir / "index.json",
                   json.dumps(idx, sort_keys=True, indent=1).encode())


def _gpg_run(cmd, data: bytes) -> bytes:
    p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL)
    if p.returncode != 0:
        raise RuntimeError("gpg failed")
    return p.stdout


def add(cfg: Config, name: str, kind: str, value: str,
        owner_order_only: bool = False) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    _check_name(name)
    if not value:
        raise ValueError("empty value")
    if "\x00" in value:
        raise ValueError("value must not contain NUL")
    try:
        data = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("value must be valid UTF-8") from None
    with _lock(cfg):
        enc = _gpg_run(_gpg(cfg, "--symmetric", "--cipher-algo", "AES256"),
                       data)
        _write_private(_item_path(cfg, name), enc)
        idx = _read_index(cfg)
        idx[name] = {"kind": kind, "owner_order_only": bool(owner_order_only)}
        _write_index(cfg, idx)


def list_items(cfg: Config, kind: str | None = None,
               query: str | None = None) -> list[dict]:
    out = []
    for name, meta in sorted(_read_index(cfg).items()):
        if kind and meta["kind"] != kind:
            continue
        if query and query.lower() not in name.lower():
            continue
        out.append({"name": name, "kind": meta["kind"],
                    "owner_order_only": meta["owner_order_only"]})
    return out


def remove(cfg: Config, name: str) -> None:
    with _lock(cfg):
        idx = _read_index(cfg)
        if name not in idx:
            raise KeyError(name)
        del idx[name]
        _write_index(cfg, idx)
        try:
            _item_path(cfg, name).unlink()
        except FileNotFoundError:
            pass


def _reveal(cfg: Config, name: str) -> bytes:
    p = _item_path(cfg, name)
    return _gpg_run(_gpg(cfg, "--decrypt"), p.read_bytes())


def _pump(stream, sink, masker):
    fd = stream.fileno()
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data = masker.feed(chunk)
        if data:
            sink.write(data)
            sink.flush()
    data = masker.flush()
    if data:
        sink.write(data)
        sink.flush()


def run_with(cfg: Config, conn, actor: str, env_map: dict, argv: list,
             decision: str | None = None, out=None, err=None) -> int:
    """Run argv with secrets in the environment. Output is masked."""
    out = out if out is not None else sys.stdout.buffer
    err = err if err is not None else sys.stderr.buffer
    idx = _read_index(cfg)
    for var, name in env_map.items():
        if name not in idx:
            raise KeyError(name)
    for name in env_map.values():
        if not idx[name]["owner_order_only"]:
            continue
        ok = bool(decision) and conn.execute(
            "SELECT 1 FROM decisions WHERE id=?", (decision,)).fetchone()
        if not ok:
            activity.record(conn, actor, "secret.refused", name, decision)
            raise PermissionError(
                f"{name} is owner-order-only: a recorded decision is needed")
    env = dict(os.environ)
    values = []
    for var, name in env_map.items():
        v = _reveal(cfg, name)
        values.append(v)
        env[var] = v.decode()
    try:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, ValueError):
        for name in env_map.values():
            activity.record(conn, actor, "secret.use_failed", name, decision)
        return 127
    for name in env_map.values():
        activity.record(conn, actor, "secret.use", name, decision)
    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, out, Masker(values))),
        threading.Thread(target=_pump, args=(proc.stderr, err, Masker(values))),
    ]
    for t in threads:
        t.start()
    rc = proc.wait()
    for t in threads:
        t.join()
    proc.stdout.close()
    proc.stderr.close()
    return rc


def use_env(cfg: Config, conn, actor: str, env_map: dict,
            decision: str | None = None) -> dict:
    """Decrypt secrets into an env dict for a child process that must keep
    its terminal (the always-on Admin), so output cannot be masked. Same
    checks and the same `secret.use` record, by name, as run_with. Raises
    KeyError for an unknown name."""
    idx = _read_index(cfg)
    for var, name in env_map.items():
        if name not in idx:
            raise KeyError(name)
    for name in env_map.values():
        if idx[name]["owner_order_only"]:
            ok = bool(decision) and conn.execute(
                "SELECT 1 FROM decisions WHERE id=?", (decision,)).fetchone()
            if not ok:
                activity.record(conn, actor, "secret.refused", name, decision)
                raise PermissionError(
                    f"{name} is owner-order-only: a recorded decision is "
                    "needed")
    out = {var: _reveal(cfg, name).decode() for var, name in env_map.items()}
    for name in env_map.values():
        activity.record(conn, actor, "secret.use", name, decision)
    return out


# ---- CLI wiring -----------------------------------------------------------

def _actor() -> str:
    return os.environ.get("CB_BOT", "cli")


def _cmd_add(args) -> int:
    cfg = Config.load()
    value = getpass(f"Value for {args.name}: ")
    try:
        add(cfg, args.name, args.kind, value, args.owner_order_only)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    conn = connect(cfg)
    activity.record(conn, _actor(), "secret.add", args.name)
    conn.close()
    print(f"stored {args.name} ({args.kind})")
    return 0


def _cmd_list(args) -> int:
    for it in list_items(Config.load(), args.kind, args.search):
        flag = " owner-order-only" if it["owner_order_only"] else ""
        print(f"{it['name']}\t{it['kind']}{flag}")
    return 0


def _cmd_rm(args) -> int:
    cfg = Config.load()
    try:
        remove(cfg, args.name)
    except KeyError:
        print(f"error: no secret named {args.name}", file=sys.stderr)
        return 1
    conn = connect(cfg)
    activity.record(conn, _actor(), "secret.rm", args.name)
    conn.close()
    return 0


def _cmd_run(args) -> int:
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("error: no command given", file=sys.stderr)
        return 2
    env_map = {}
    for spec in args.env or []:
        var, sep, name = spec.partition("=")
        if not sep or not var or not name:
            print("error: --env needs VAR=NAME", file=sys.stderr)
            return 2
        env_map[var] = name
    cfg = Config.load()
    conn = connect(cfg)
    try:
        return run_with(cfg, conn, _actor(), env_map, cmd, args.decision)
    except (KeyError, PermissionError, RuntimeError, ValueError,
            UnicodeDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def register(subparsers) -> None:
    sp = subparsers.add_parser("secret", help="secure store")
    sub = sp.add_subparsers(dest="secret_cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--kind", required=True, choices=KINDS)
    a.add_argument("--owner-order-only", action="store_true")
    a.set_defaults(func=_cmd_add)
    ls = sub.add_parser("list")
    ls.add_argument("--kind", choices=KINDS)
    ls.add_argument("--search")
    ls.set_defaults(func=_cmd_list)
    r = sub.add_parser("rm")
    r.add_argument("name")
    r.set_defaults(func=_cmd_rm)
    ru = sub.add_parser("run")
    ru.add_argument("--env", action="append", metavar="VAR=NAME")
    ru.add_argument("--decision")
    ru.add_argument("cmd", nargs=argparse.REMAINDER)
    ru.set_defaults(func=_cmd_run)

