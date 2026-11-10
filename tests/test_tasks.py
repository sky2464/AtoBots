import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from claudebot import bots, cli, items, messages, tasks  # noqa: E402
from claudebot.config import Config  # noqa: E402
from claudebot.db import connect, init_db  # noqa: E402


class TasksBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Config(Path(self._tmp.name) / "home")
        init_db(self.cfg)
        self.conn = connect(self.cfg)
        self.addCleanup(self.conn.close)
        bots.create(self.cfg, self.conn, "admin", role="admin", soul="s", persona="p")
        bots.create(self.cfg, self.conn, "backend", role="b", soul="s", persona="p")
        bots.create(self.cfg, self.conn, "scout", role="w", soul="s", persona="p",
                    model="haiku")
        self.item = items.new(self.cfg, self.conn, "build it", "backend",
                              status="todo")

    def task(self, sender="backend", outputs=("work/T-0001/out.md",), item=None):
        return tasks.send_task(self.conn, sender, "scout", item or self.item,
                               "list the files", ["work/T-0001/in.md"],
                               list(outputs), "the file exists")

    def msg(self, id):
        return self.conn.execute("SELECT * FROM messages WHERE id=?",
                                 (id,)).fetchone()

    def results_to(self, bot):
        return self.conn.execute(
            "SELECT * FROM messages WHERE kind='result' AND to_bot=? ORDER BY id",
            (bot,)).fetchall()

    def to(self, bot, kind=None):
        sql = "SELECT * FROM messages WHERE to_bot=?"
        args = [bot]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()


class SendTaskTests(TasksBase):
    def test_send_task_rejects_thinker_recipient(self):
        with self.assertRaises(ValueError):
            tasks.send_task(self.conn, "scout", "backend", self.item, "g",
                            [], [], "c")
        self.assertEqual(self.to("backend", "task"), [])

    def test_send_task_writes_task_message_for_worker(self):
        mid = self.task()
        m = self.msg(mid)
        self.assertEqual((m["kind"], m["to_bot"], m["item_id"]),
                         ("task", "scout", self.item))
        body = json.loads(m["body"])
        self.assertEqual(body["goal"], "list the files")
        self.assertEqual(body["outputs"], ["work/T-0001/out.md"])
        self.assertEqual(body["check"], "the file exists")


class FinishTaskTests(TasksBase):
    def write(self, rel):
        p = self.cfg.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")

    def test_finish_done_requires_output_files(self):
        mid = self.task()
        rid = tasks.finish_task(self.cfg, self.conn, mid, "done", "wrote it")
        r = self.msg(rid)
        self.assertEqual((r["kind"], r["to_bot"]), ("result", "backend"))
        self.assertIn("failed", r["body"])
        self.assertIn("work/T-0001/out.md", r["body"])
        self.assertEqual(self.conn.execute(
            "SELECT value FROM meta WHERE key='task.fail.T-0001'").fetchone()[0],
            "1")
        self.write("work/T-0001/out.md")
        mid2 = self.task()
        rid2 = tasks.finish_task(self.cfg, self.conn, mid2, "done", "")
        self.assertIn("finished: done", self.msg(rid2)["body"])

    def test_finish_twice_is_refused(self):
        mid = self.task()
        tasks.finish_task(self.cfg, self.conn, mid, "failed", "no")
        with self.assertRaises(ValueError):
            tasks.finish_task(self.cfg, self.conn, mid, "failed", "again")

    def test_second_failure_escalates_once(self):
        for _ in range(3):
            mid = self.task()
            tasks.finish_task(self.cfg, self.conn, mid, "failed", "broken")
        self.assertEqual(len(self.to("admin")), 1)
        self.assertEqual(self.to("admin")[0]["kind"], "escalation")
        self.assertEqual(len(self.results_to("backend")), 3)

    def test_failure_escalation_from_admin_is_only_recorded(self):
        for _ in range(2):
            mid = self.task(sender="admin")
            tasks.finish_task(self.cfg, self.conn, mid, "failed", "broken")
        self.assertEqual(
            [m for m in self.to("admin") if m["kind"] == "escalation"], [])
        self.assertEqual(len(self.conn.execute(
            "SELECT * FROM activity WHERE kind='task.escalate'").fetchall()), 1)


class CliTests(TasksBase):
    def test_cli_registers_task_commands(self):
        p = cli.build_parser()
        a = p.parse_args(["task", "finish", "7", "--status", "done",
                          "--note", "ok"])
        self.assertEqual((a.message_id, a.status, a.note), (7, "done", "ok"))
        b = p.parse_args(["task", "send", "--to", "scout", "--item", "T-0001",
                          "--goal", "g", "--input", "a", "--output", "b",
                          "--check", "c"])
        self.assertEqual((b.to, b.item, b.inputs, b.outputs), (
            "scout", "T-0001", ["a"], ["b"]))

    def test_render_names_the_finish_command(self):
        text = tasks.render(self.task_body(), 12)
        self.assertIn("Goal: list the files", text)
        self.assertIn("cb task finish 12 --status done|failed", text)

    def task_body(self):
        return json.dumps({"goal": "list the files", "inputs": [],
                           "outputs": ["x"], "check": "c"})


if __name__ == "__main__":
    unittest.main()
