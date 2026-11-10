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


class ReviewRoundOneTests(TasksBase):
    """Fix round 1: path escape, marker-based finish, reset, open-task wake."""

    def write(self, rel, text="x"):
        p = self.cfg.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def raw_task(self, outputs, item=None):
        """A task message whose body was not checked by send_task."""
        body = json.dumps({"goal": "g", "inputs": [], "outputs": outputs,
                           "check": "c"})
        return messages.send(self.conn, "backend", "scout", "t", body,
                             item_id=item or self.item, kind="task")

    def test_send_task_rejects_bad_output_paths(self):
        bad = [["/etc/passwd"], ["../x"], ["work/../../x"], [""], ["."],
               [5], "work/out.md", [None]]
        for outputs in bad:
            with self.subTest(outputs=outputs), self.assertRaises(ValueError):
                tasks.send_task(self.conn, "backend", "scout", self.item,
                                "g", [], outputs, "c")

    def test_send_task_rejects_bad_input_paths(self):
        for inputs in (["/etc/passwd"], ["../x"], "work/in.md", [""], [3]):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                tasks.send_task(self.conn, "backend", "scout", self.item,
                                "g", inputs, [], "c")
        self.assertEqual(self.to("scout", "task"), [])

    def test_finish_treats_escaping_or_bad_outputs_as_missing(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("x")
        cases = [["/etc/hostname"], ["../outside.txt"], [5], "work/x.md",
                 [None]]
        for outputs in cases:
            with self.subTest(outputs=outputs):
                item = items.new(self.cfg, self.conn, "per case", "backend",
                                 status="todo")
                mid = self.raw_task(outputs, item=item)
                sent = tasks.finish_task(self.cfg, self.conn, mid, "done", "")
                self.assertIn("finished: failed", self.msg(sent)["body"])

    def test_finish_done_with_symlink_out_of_home_is_missing(self):
        outside = Path(self._tmp.name) / "secret.txt"
        outside.write_text("x")
        (self.cfg.home / "work").mkdir(parents=True, exist_ok=True)
        (self.cfg.home / "work" / "link.txt").symlink_to(outside)
        mid = self.raw_task(["work/link.txt"])
        sent = tasks.finish_task(self.cfg, self.conn, mid, "done", "")
        self.assertIn("finished: failed", self.msg(sent)["body"])

    def test_failed_result_send_still_counts_and_refuses_refinish(self):
        bots.set_status(self.conn, "backend", "retired", "admin")
        mid = self.task()
        sent = tasks.finish_task(self.cfg, self.conn, mid, "failed", "no")
        self.assertEqual(sent, 0)  # result cannot reach the retired sender
        self.assertEqual(tasks.failures(self.conn, self.item), 1)
        self.assertEqual(len(self.conn.execute(
            "SELECT * FROM activity WHERE kind='task.result_unsent'").fetchall()), 1)
        with self.assertRaises(ValueError):
            tasks.finish_task(self.cfg, self.conn, mid, "failed", "again")
        self.assertEqual(tasks.failures(self.conn, self.item), 1)

    def test_escalates_at_two_even_when_results_cannot_be_sent(self):
        bots.set_status(self.conn, "backend", "retired", "admin")
        for _ in range(2):
            mid = self.task()
            tasks.finish_task(self.cfg, self.conn, mid, "failed", "no")
        esc = [m for m in self.to("admin") if m["kind"] == "escalation"]
        self.assertEqual(len(esc), 1)
        self.assertEqual(tasks.failures(self.conn, self.item), 2)

    def test_hop_limit_zero_result_counts_once_and_refuses_refinish(self):
        mid = self.task()
        self.cfg.hop_limit = 0
        sent = tasks.finish_task(self.cfg, self.conn, mid, "failed", "no")
        self.assertEqual(sent, 0)
        self.assertEqual(tasks.failures(self.conn, self.item), 1)
        with self.assertRaises(ValueError):
            tasks.finish_task(self.cfg, self.conn, mid, "failed", "again")
        self.assertEqual(tasks.failures(self.conn, self.item), 1)
        self.assertEqual(len(self.conn.execute(
            "SELECT * FROM activity WHERE kind='task.result_unsent'").fetchall()), 1)

    def test_send_task_uses_cfg_hop_limit(self):
        self.cfg.hop_limit = 0
        self.assertEqual(tasks.send_task(self.conn, "backend", "scout", self.item,
                                         "g", [], [], "c", cfg=self.cfg), 0)

    def test_success_resets_failures(self):
        self.write("work/T-0001/out.md")
        mid = self.task()
        tasks.finish_task(self.cfg, self.conn, mid, "failed", "no")
        self.assertEqual(tasks.failures(self.conn, self.item), 1)
        mid2 = self.task()
        tasks.finish_task(self.cfg, self.conn, mid2, "done", "")
        self.assertEqual(tasks.failures(self.conn, self.item), 0)
        mid3 = self.task()
        tasks.finish_task(self.cfg, self.conn, mid3, "failed", "no")
        self.assertEqual(tasks.failures(self.conn, self.item), 1)
        self.assertEqual(self.to("admin"), [])  # counted from zero: no escalation

    def test_wake_body_points_at_open_task_without_embedding(self):
        mid = self.task()
        body = tasks.wake_body(self.conn, self.item, "scout", "fallback")
        self.assertIn(f"message {mid}", body)
        self.assertIn(f"cb task finish {mid} --status done|failed", body)
        self.assertNotIn("Goal:", body)
        self.assertNotIn("list the files", body)

    def test_wake_body_ignores_finished_task(self):
        self.write("work/T-0001/out.md")
        mid = self.task()
        tasks.finish_task(self.cfg, self.conn, mid, "done", "")
        self.assertEqual(tasks.wake_body(self.conn, self.item, "scout", "fallback"),
                         "fallback")


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
