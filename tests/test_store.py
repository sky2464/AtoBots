import io
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import cli, store  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db, utcnow  # noqa: E402

VALUE = "s3cr3t-value"
PRINT_X = ["python3", "-c", "import os; print(os.environ['X'])"]


def _add_worker(args):
    home, i = args
    store.add(Config(Path(home)), f"item{i}", "token", f"value-{i}",
              owner_order_only=(i % 2 == 0))


class MaskerTests(unittest.TestCase):
    def run_chunks(self, chunks, values=(VALUE.encode(),)):
        m = store.Masker(values)
        out = b"".join(m.feed(c) for c in chunks) + m.flush()
        return out

    def test_split_across_every_boundary(self):
        v = VALUE.encode()
        data = b"before " + v + b" after " + v
        for i in range(1, len(data)):
            out = self.run_chunks([data[:i], data[i:]])
            self.assertEqual(out, b"before *** after ***", i)

    def test_one_byte_chunks(self):
        data = b"a" + VALUE.encode() + b"b"
        out = self.run_chunks([data[i:i + 1] for i in range(len(data))])
        self.assertEqual(out, b"a***b")

    def test_plain_text_passes_and_flushes(self):
        self.assertEqual(self.run_chunks([b"hello ", b"world"]), b"hello world")


@unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(Path(self.tmp.name))
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        self.addCleanup(self.stop_agent)

    def stop_agent(self):
        gh = self.cfg.secrets_dir / "gnupg"
        if gh.exists():
            subprocess.run(["gpgconf", "--homedir", str(gh), "--kill", "all"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run_x(self, argv=PRINT_X, name="t", decision=None, actor="admin"):
        out, err = io.BytesIO(), io.BytesIO()
        rc = store.run_with(self.cfg, self.conn, actor, {"X": name}, argv,
                            decision, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_roundtrip_via_run(self):
        store.add(self.cfg, "t", "token", VALUE)
        rc, out, _ = self.run_x(["python3", "-c",
                                 "import os,sys; sys.exit(0 if os.environ['X']"
                                 f"=='{VALUE}' else 3)"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, b"")

    def test_value_masked_in_output(self):
        store.add(self.cfg, "t", "token", VALUE)
        code = ("import os,sys,time;v=os.environ['X'];h=len(v)//2;"
                "print(v);print(v,file=sys.stderr);"
                "sys.stdout.write(v[:h]);sys.stdout.flush();time.sleep(0.3);"
                "sys.stdout.write(v[h:]+'\\n');"
                "sys.stderr.write(v[:h]);sys.stderr.flush();time.sleep(0.3);"
                "sys.stderr.write(v[h:]+'\\n')")
        rc, out, err = self.run_x(["python3", "-c", code])
        self.assertEqual(rc, 0)
        self.assertEqual(out, b"***\n***\n")
        self.assertEqual(err, b"***\n***\n")
        self.assertNotIn(VALUE.encode(), out + err)

    def test_not_plaintext_on_disk(self):
        store.add(self.cfg, "t", "token", VALUE)
        self.run_x()
        for p in Path(self.tmp.name).rglob("*"):
            if p.is_file():
                self.assertNotIn(VALUE.encode(), p.read_bytes(), str(p))
        self.assertEqual(stat.S_IMODE(self.cfg.secrets_dir.stat().st_mode), 0o700)
        for p in self.cfg.secrets_dir.iterdir():
            if p.is_file():
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, p.name)
        self.assertTrue((self.cfg.secrets_dir / "store.key").exists())

    def test_list_has_no_values(self):
        store.add(self.cfg, "t", "token", VALUE)
        store.add(self.cfg, "mail", "login", "other-pass")
        items = store.list_items(self.cfg)
        self.assertEqual([i["name"] for i in items], ["mail", "t"])
        self.assertNotIn(VALUE, repr(items))
        self.assertNotIn("other-pass", repr(items))
        self.assertEqual([i["name"] for i in store.list_items(self.cfg, kind="login")],
                         ["mail"])
        self.assertEqual([i["name"] for i in store.list_items(self.cfg, query="MA")],
                         ["mail"])

    def test_payment_kind_rejected(self):
        with self.assertRaises(ValueError):
            store.add(self.cfg, "card", "payment", "4242")
        self.assertEqual(store.list_items(self.cfg), [])

    def test_remove(self):
        store.add(self.cfg, "t", "token", VALUE)
        store.remove(self.cfg, "t")
        self.assertEqual(store.list_items(self.cfg), [])
        with self.assertRaises(KeyError):
            store.remove(self.cfg, "t")
        self.assertEqual(list(self.cfg.secrets_dir.glob("*.gpg")), [])

    def test_use_is_recorded(self):
        store.add(self.cfg, "t", "token", VALUE)
        self.run_x(actor="admin")
        rows = self.conn.execute(
            "SELECT * FROM activity WHERE kind='secret.use'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["actor"], rows[0]["detail"]), ("admin", "t"))
        dump = repr([tuple(r) for r in self.conn.execute("SELECT * FROM activity")])
        self.assertNotIn(VALUE, dump)

    def _owner_decision(self, did="D-0001", session="owner-sess"):
        self.conn.execute(
            "INSERT OR IGNORE INTO runs(bot, session_id, lane, started_at)"
            " VALUES ('admin',?,'chat',?)", (session, utcnow()))
        self.conn.execute(
            "INSERT INTO decisions(id,title,quote,said_at,session,recorded_by,"
            "created_at,owner_session) VALUES(?,?,?,?,?,?,?,1)",
            (did, "use it", "go", utcnow(), session, "admin", utcnow()))

    def test_owner_order_only_needs_decision(self):
        store.add(self.cfg, "t", "token", VALUE, owner_order_only=True)
        for dec in (None, "D-0009"):
            with self.assertRaises(PermissionError):
                self.run_x(decision=dec)
        # Decision without Owner session provenance is refused.
        self.conn.execute(
            "INSERT INTO decisions(id,title,quote,said_at,session,recorded_by,"
            "created_at) VALUES('D-0098','t','q',?,?,?,?)",
            (utcnow(), "bot-sess", "admin", utcnow()))
        with self.assertRaises(PermissionError):
            self.run_x(decision="D-0098")
        refused = self.conn.execute(
            "SELECT * FROM activity WHERE kind='secret.refused'").fetchall()
        self.assertEqual(len(refused), 3)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM activity WHERE kind='secret.use'").fetchone()[0], 0)
        self._owner_decision()
        rc, out, _ = self.run_x(decision="D-0001")
        self.assertEqual((rc, out), (0, b"***\n"))

    def test_unknown_secret_and_bad_command(self):
        with self.assertRaises(KeyError):
            self.run_x(name="nope")
        store.add(self.cfg, "t", "token", VALUE)
        self.assertEqual(self.run_x(["/no/such/binary"])[0], 127)

    def test_overwrite_replaces_value(self):
        store.add(self.cfg, "t", "token", VALUE)
        store.add(self.cfg, "t", "token", "second-value")
        _, out, _ = self.run_x()
        self.assertEqual(out, b"***\n")
        rc, out, _ = self.run_x(["python3", "-c",
                                 "import os;print(os.environ['X'][:6])"])
        self.assertEqual(out, b"second\n")

    def test_concurrent_add_keeps_all_items_and_key(self):
        n = 6
        with multiprocessing.get_context("fork").Pool(n) as pool:
            pool.map(_add_worker, [(self.tmp.name, i) for i in range(n)])
        items = {i["name"]: i for i in store.list_items(self.cfg)}
        self.assertEqual(set(items), {f"item{i}" for i in range(n)})
        for i in range(n):
            self.assertEqual(items[f"item{i}"]["owner_order_only"], i % 2 == 0)
            out = io.BytesIO()
            self.conn.execute(
                "INSERT OR IGNORE INTO runs(bot, session_id, lane, started_at)"
                " VALUES ('admin','owner-sess','chat',?)", (utcnow(),))
            self.conn.execute(
                "INSERT OR IGNORE INTO decisions(id,title,quote,said_at,session,"
                "recorded_by,created_at,owner_session) VALUES('D-0001','t','q',"
                "?,?,?,?,1)",
                (utcnow(), "owner-sess", "admin", utcnow()))
            store.run_with(self.cfg, self.conn, "admin", {"X": f"item{i}"},
                           ["python3", "-c",
                            "import os;print(os.environ['X'][::-1])"],
                           "D-0001", out=out, err=io.BytesIO())
            self.assertEqual(out.getvalue(), f"{i}-eulav\n".encode())

    def test_nul_value_rejected(self):
        with self.assertRaises(ValueError):
            store.add(self.cfg, "t", "token", "ab\x00cd")
        with self.assertRaises(ValueError):
            store.add(self.cfg, "t", "token", "bad\udcff")
        self.assertEqual(store.list_items(self.cfg), [])

    def test_failed_start_is_recorded_as_failed(self):
        store.add(self.cfg, "t", "token", VALUE)
        self.assertEqual(self.run_x(["/no/such/binary"])[0], 127)
        self.assertEqual(self.run_x(["python3", "a\x00b"])[0], 127)
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM activity ORDER BY id")]
        self.assertEqual(kinds, ["secret.use_failed", "secret.use_failed"])

    def test_cli_run_with_env_and_separator(self):
        store.add(self.cfg, "t", "token", VALUE)
        script = ("import os,sys;print(os.environ['X'][:2]);"
                  "print(os.environ['X'],file=sys.stderr);sys.exit(4)")
        env = dict(os.environ, CLAUDEBOT_HOME=self.tmp.name,
                   CB_BOT="admin")
        cb = Path(__file__).resolve().parent.parent / "engine" / "bin" / "cb"
        p = subprocess.run([sys.executable, str(cb), "secret", "run",
                            "--env", "X=t", "--", "python3", "-c", script],
                           env=env, capture_output=True)
        self.assertEqual(p.returncode, 4)
        self.assertEqual(p.stdout, b"s3\n")
        self.assertEqual(p.stderr, b"***\n")
        bad = subprocess.run([sys.executable, str(cb), "secret", "run",
                              "--env", "NOEQUALS", "--", "true"],
                             env=env, capture_output=True)
        self.assertEqual(bad.returncode, 2)
        row = self.conn.execute(
            "SELECT actor, detail FROM activity WHERE kind='secret.use'").fetchone()
        self.assertEqual(tuple(row), ("admin", "t"))

    def test_cli_add_list_rm_run(self):
        env = {"CLAUDEBOT_HOME": self.tmp.name}
        with mock.patch.dict("os.environ", env), \
                mock.patch("claudebot.store.getpass", return_value=VALUE):
            quiet = io.StringIO()
            with mock.patch("sys.stdout", quiet):
                self.assertEqual(
                    cli.main(["secret", "add", "t", "--kind", "token"]), 0)
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(cli.main(["secret", "list", "--kind", "token"]), 0)
            self.assertEqual(buf.getvalue(), "t\ttoken\n")
            self.assertNotIn(VALUE, buf.getvalue())
            self.assertEqual(cli.main(["secret", "rm", "t"]), 0)
            with mock.patch("sys.stderr", quiet):
                self.assertEqual(cli.main(["secret", "rm", "t"]), 1)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM activity")]
        self.assertEqual(kinds, ["secret.add", "secret.rm"])


if __name__ == "__main__":
    unittest.main()
