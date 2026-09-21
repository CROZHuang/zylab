"""M3c-4 task commands、Session journaling 与 shutdown 集成。"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zylab as CLI
from core import controller, tasks
from tests.pty_harness import PTYSend, run_pty_child
from tests.terminal_screen import parse_final_screen


class MainAgent:
    model = "main-model"
    gateway = "test"
    session_id = "task-session"
    last_total = 4_000
    compact_at = 70_000
    ctx_known = True
    tokens_in = 0
    tokens_out = 0
    turns = 0
    started = 0


def snapshot(task_id="task001", *, status="running", background=True,
             stdout_bytes=2048, stderr_bytes=0, name="bash",
             returncode=None, signal=None, timed_out=False, error=None):
    return SimpleNamespace(
        id=task_id, key="call-" + task_id, session="task-session",
        name=name, status=tasks.TaskStatus(status), background=background,
        created_at="2026-08-25T00:00:00+00:00",
        started_at="2026-08-25T00:00:01+00:00",
        ended_at=("2026-08-25T00:00:03+00:00"
                  if status in {"completed", "failed", "cancelled"}
                  else None),
        stdout_bytes=stdout_bytes, stderr_bytes=stderr_bytes,
        returncode=returncode, signal=signal,
        timed_out=timed_out, error=error,
    )


class FakeWorkspace:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True
        return True


class FakeManager:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.events = []
        self.cancelled = []
        self.shutdown_called = False
        self.on_background = None

    def list(self):
        return list(self.rows)

    def get(self, identifier):
        return next((row for row in self.rows
                     if identifier in {row.id, row.key}), None)

    def foreground(self):
        return next((row for row in self.rows
                     if not row.background
                     and CLI._task_status(row) not in {
                         "completed", "failed", "cancelled"}), None)

    def background(self, identifier=None):
        if self.on_background:
            self.on_background()
        row = self.get(identifier) if identifier else self.foreground()
        if row is None or row.name != "bash":
            raise tasks.TaskError("cannot background")
        row.background = True
        return row

    def drain_background(self, *, defer_terminal_ids=None):
        deferred = {str(value) for value in (defer_terminal_ids or ())}
        ready, pending = [], []
        for event in self.events:
            if (event.get("t") in {"completed", "failed", "cancelled"}
                    and str(event.get("task_id") or "") in deferred):
                pending.append(event)
            else:
                ready.append(event)
        self.events = pending
        return ready

    def tail(self, identifier, limit=8192):
        row = self.get(identifier)
        if row is None:
            raise tasks.TaskError("unknown")
        return {
            "task": vars(row),
            "stdout": {
                "text": "READY\n", "path": "/tmp/stdout",
                "total_bytes": 6, "returned_bytes": 6,
                "truncated": False,
            },
            "stderr": {
                "text": "", "path": None, "total_bytes": 0,
                "returned_bytes": 0, "truncated": False,
            },
        }

    def cancel(self, identifier):
        row = self.get(identifier)
        if row:
            self.cancelled.append(row.id)
        return row

    def shutdown(self):
        self.shutdown_called = True
        return list(self.rows)


class TaskCommandTests(unittest.TestCase):
    def make_session(self, manager):
        sess = CLI.Session(MainAgent())
        sess.agent_workspace.close()
        sess.agent_workspace = FakeWorkspace()
        sess.task_manager = manager
        journal = controller.MemoryJournal()
        sess.controller = controller.SessionController(
            sess.ag.session_id, journal)
        return sess, journal

    def test_commands_are_registered(self):
        self.assertTrue(
            {"tasks", "task", "expand", "kill"} <= set(CLI.REGISTRY))

    def test_expand_missing_artifact_falls_back_without_mutating_messages(self):
        sess, _ = self.make_session(FakeManager())
        sess.ag.messages = [
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-expand-1",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": "{\"command\":\"printf test\"}",
                },
            }]},
            {
                "role": "tool",
                "tool_call_id": "call-expand-1",
                "content": "saved line one\nsaved line two",
            },
        ]
        original = json.loads(json.dumps(sess.ag.messages))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            CLI.cmd_expand(sess, "call-expand-1")

        text = out.getvalue()
        self.assertIn("完整输出已不可用", text)
        self.assertIn("saved line one", text)
        self.assertIn("saved line two", text)
        self.assertEqual(sess.ag.messages, original)

    def test_tasks_lists_mode_status_elapsed_and_output(self):
        foreground = snapshot("fore0001", background=False)
        background = snapshot("back0001", background=True,
                              stdout_bytes=4096)
        completed = snapshot(
            "done0001", status="completed", background=False,
            stdout_bytes=12, returncode=7)
        sess, _ = self.make_session(FakeManager(
            [foreground, background, completed]))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            CLI.cmd_tasks(sess, "")

        text = out.getvalue()
        self.assertIn("fore0001  fg", text)
        self.assertIn("back0001  bg", text)
        self.assertIn("completed", text)
        self.assertIn("4.0KiB", text)
        self.assertIn("exit 7", text)

    def test_task_tails_and_attaches_running_background(self):
        running = snapshot("back0002", background=True)
        sess, _ = self.make_session(FakeManager([running]))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            CLI.cmd_task(sess, running.id)
            CLI.cmd_task(sess, "detach")

        self.assertIn("READY", out.getvalue())
        self.assertIn("已 attach", out.getvalue())
        self.assertIn("已 detach", out.getvalue())
        self.assertIsNone(sess.attached_task_id)

    def test_task_tail_shows_completed_process_outcome(self):
        completed = snapshot(
            "done0002", status="completed", background=True,
            returncode=7)
        sess, _ = self.make_session(FakeManager([completed]))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            CLI.cmd_task(sess, completed.id)

        self.assertIn("exit 7", out.getvalue())
        self.assertIsNone(sess.attached_task_id)

    def test_background_intent_is_journaled_before_manager_mutation(self):
        foreground = snapshot("fore0002", background=False)
        manager = FakeManager([foreground])
        sess, journal = self.make_session(manager)
        manager.on_background = lambda: self.assertEqual(
            journal.events[-1]["kind"], "task_background_requested")

        result = sess.background_active_task()

        self.assertTrue(result.background)
        self.assertIn(result.id, sess._background_task_context)

    def test_kill_background_uses_original_controller_context(self):
        running = snapshot("back0003", background=True)
        manager = FakeManager([running])
        sess, journal = self.make_session(manager)
        sess._background_task_context[running.id] = (
            sess.controller, "original-turn")

        with contextlib.redirect_stdout(io.StringIO()):
            CLI.cmd_kill(sess, running.id)

        self.assertEqual(manager.cancelled, [running.id])
        event = journal.events[-1]
        self.assertEqual(event["kind"], "task_kill_requested")
        self.assertEqual(event["turn_id"], "original-turn")

    def test_background_output_and_terminal_are_consumed_on_main_thread(self):
        running = snapshot("back0004", background=True)
        manager = FakeManager([running])
        sess, journal = self.make_session(manager)
        sess.attached_task_id = running.id
        sess._background_task_context[running.id] = (
            sess.controller, "original-turn")
        task_dict = {
            "id": running.id, "key": running.key, "name": "bash",
            "stdout_bytes": 6, "stderr_bytes": 0,
            "stdout_path": "/tmp/stdout", "stderr_path": None,
            "returncode": 7, "signal": None,
            "timed_out": False, "error": None,
        }
        manager.events = [
            {"t": "output", "task_id": running.id, "v": "LIVE\n"},
            {"t": "completed", "task_id": running.id, "v": "LIVE",
             "task": task_dict},
        ]

        notices = sess.drain_task_events()

        self.assertIn(
            {"text": "LIVE\n", "source": f"task:{running.id}"},
            notices)
        self.assertTrue(any(
            "completed" in item["text"]
            and "exit 7" in item["text"]
            and item["source"] == "status"
            for item in notices))
        self.assertIsNone(sess.attached_task_id)
        event = journal.events[-1]
        self.assertEqual(event["kind"], "task_finished")
        self.assertEqual(event["turn_id"], "original-turn")

    def test_fast_background_terminal_waits_for_protocol_handoff(self):
        running = snapshot("fast0001", background=True)
        manager = FakeManager([running])
        sess, journal = self.make_session(manager)
        sess._task_background_handoff_id = running.id
        sess._background_task_context[running.id] = (
            sess.controller, "original-turn")
        manager.events = [{
            "t": "completed", "task_id": running.id, "v": "done",
            "task": {
                "id": running.id, "key": running.key, "name": "bash",
                "stdout_bytes": 0, "stderr_bytes": 0,
                "stdout_path": None, "stderr_path": None,
                "returncode": 0, "signal": None,
                "timed_out": False, "error": None,
            },
        }]

        self.assertEqual(sess.drain_task_events(), [])
        self.assertEqual(
            [event["kind"] for event in journal.events].count(
                "task_finished"), 0)
        self.assertEqual(len(manager.events), 1)

        # Agent.finish_tool 已在 tool_end yield 前发生；UI 此时才释放 gate。
        sess.controller.record_event(
            "tool_finished_probe", {"task_id": running.id})
        sess._task_background_handoff_id = None
        notices = sess.drain_task_events()

        kinds = [event["kind"] for event in journal.events]
        self.assertEqual(len(notices), 1)
        self.assertEqual(kinds.count("task_finished"), 1)
        self.assertLess(kinds.index("tool_finished_probe"),
                        kinds.index("task_finished"))
        self.assertEqual(sess.drain_task_events(), [])

    def test_task_outcome_formats_timeout_signal_and_bounded_error(self):
        text = CLI._task_outcome({
            "timed_out": True, "signal": 9, "returncode": -9,
            "error": "line one\n" + "x" * 300,
        }, error_limit=40)

        self.assertIn("timeout", text)
        self.assertIn("signal 9", text)
        self.assertIn("error line one ", text)
        self.assertLessEqual(len(text.split("error ", 1)[1]), 40)

    def test_close_shuts_down_tasks_and_workspace(self):
        manager = FakeManager([snapshot()])
        sess, _ = self.make_session(manager)
        workspace = sess.agent_workspace

        self.assertTrue(sess.close())

        self.assertTrue(manager.shutdown_called)
        self.assertTrue(workspace.closed)


class TaskPTYTests(unittest.TestCase):
    def assertRowOrder(self, output, upto, above, below):
        """在**算出来的屏幕**上，`below` 必须自成一行，落在 `above` 下面。

        以前这里断的是「这段字节里出现过 \\r\\n」。那是**整帧重画**的副产品，
        不是契约：增量重绘只重写变化的行，行间靠 `CSI nB` / `CSI nA` 走位，
        一个 \\r\\n 都不发，而屏幕逐格相同（2026-09-21 用 terminal_screen
        逐行比过主线与移植后的两份输出，除工作目录那行的路径外完全一致）。
        旧写法量的是重绘策略，不是用户看到的东西。
        tests/terminal_screen.py 开篇就写着：原始字节流里混着已被覆盖的旧帧，
        不是可靠的断言对象。
        """
        head = output[:upto + len(below)]
        rows = parse_final_screen(head, 80, 24)["primary"].text_rows(trim=True)
        above_rows = [i for i, line in enumerate(rows) if above in line]
        below_rows = [i for i, line in enumerate(rows) if below in line]
        screen = "\n".join(f"{i:>3}| {line}" for i, line in enumerate(rows) if line)
        self.assertTrue(above_rows, f"屏幕上找不到 {above!r}：\n{screen}")
        self.assertTrue(below_rows, f"屏幕上找不到 {below!r}：\n{screen}")
        self.assertGreater(
            min(below_rows), min(above_rows),
            f"{below!r} 没有画在 {above!r} 下面：\n{screen}")

    def run_child(self, body, sends, timeout=9.0):
        return run_pty_child(
            body, sends,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=timeout,
        )

    def test_real_bash_chunk_background_observe_and_protocol_continue(self):
        output, result = self.run_child(
            r'''
import json
import os
import tempfile
import time

with tempfile.TemporaryDirectory() as home:
    os.environ["HOME"] = home
    import zylab
    from core import agent as agent_mod, client, store
    from tests.pty_harness import announce_when, install_ready_input_pump

    install_ready_input_pump()

    store.ensure_home()
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.model = "m"
    agent.gateway = "test"
    agent.session_id = "pty-task-session"
    agent.messages = [{"role": "system", "content": "s"}]
    agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
    agent.cache_read = agent.cache_write = 0
    agent.cache_reported = False
    agent.compact_failed = None
    agent.ctx_limit = 100000
    agent.compact_at = 70000
    agent.ctx_known = True
    agent.ctx_limit_source = "test"
    agent.context_summary = None
    agent.context_invalid_reason = None
    agent._compact_failed_key = None
    agent._last_age_notice_key = None
    agent._seen_ok_hi = 0
    agent.started = time.time()

    calls = {"n": 0}
    def fake_stream(model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            command = (
                "printf '%b' '\\122\\101\\127\\137\\122\\105\\101\\104\\131'; "
                "sleep 1.6; "
                "printf '%b' '\\n\\122\\101\\127\\137\\104\\117\\116\\105'")
            yield {"t": "tool", "v": [{
                "id": "call-bg", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({
                    "command": command, "timeout": 5})},
            }]}
        elif calls["n"] == 3:
            yield {"t": "tool", "v": [{
                "id": "call-fg", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({
                    "command": (
                        "printf '%b' "
                        "'\\106\\117\\122\\105\\107\\122\\117\\125\\116\\104\\137\\122\\101\\127'; "
                        "exit 7"),
                    "timeout": 5})},
            }]}
        elif calls["n"] == 4:
            yield {"t": "text", "v": "AFTER_FOREGROUND"}
        elif calls["n"] == 5:
            yield {"t": "tool", "v": [{
                "id": "call-fail", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({
                    "command": "sleep 5", "timeout": "not-a-number"})},
            }]}
        elif calls["n"] == 6:
            yield {"t": "text", "v": "AFTER_FAILURE"}
        else:
            yield {"t": "text", "v": "AFTER_BACKGROUND"}
        yield {"t": "done", "reason": "stop", "usage": {}}

    client.stream_chat = fake_stream
    session = zylab.Session(agent)
    # The PTY case exercises task/background/controller protocol.  The test
    # process may itself run inside zylab's sandbox, where nesting another
    # mount namespace is unavailable; sandbox behavior is covered separately.
    session.cfg["sandbox"]["mode"] = "disabled"
    session.cfg["sandbox"]["network_isolation"] = False
    # 这条用例钉的是 Ctrl+B 协议本身；后台任务完成后的自动交接会另起一个 turn、
    # 打乱这里数好的假流脚本，它有自己的测试（test_task_handoff_pty）。
    session.cfg["background_task_handoff"] = False
    agent.hook_cfg = session.cfg
    session.auto = True
    task_ids = iter(["bg000001", "fg000002", "fail0003"])
    session.task_manager.id_factory = lambda: next(task_ids)
    agent.confirm = session.confirm
    announce_when(
        lambda: (session.task_manager.get("bg000001").status.value
                 == "completed"),
        "ZYLAB_TEST_BACKGROUND_COMPLETED")
    announce_when(
        lambda: (calls["n"] >= 4
                 and session.controller.current_turn_id is None
                 and not session.controller.queued
                 and not session.pump.snapshot().busy),
        "ZYLAB_TEST_NORMAL_IDLE")
    announce_when(
        lambda: (calls["n"] >= 6
                 and session.controller.current_turn_id is None
                 and not session.controller.queued
                 and not session.pump.snapshot().busy),
        "ZYLAB_TEST_FAILURE_IDLE")
    zylab.repl(session, "m")
    background = session.task_manager.get("bg000001")
    foreground = session.task_manager.get("fg000002")
    failure = session.task_manager.get("fail0003")
    events = session.controller.journal.events
    result = {
        "calls": calls["n"],
        "task_status": background.status.value,
        "task_background": background.background,
        "foreground_status": foreground.status.value,
        "failure_status": failure.status.value,
        "tool_results": [m["content"] for m in agent.messages
                         if m.get("role") == "tool"],
        "journal_kinds": [e["kind"] for e in events],
        "tool_statuses": [e["payload"].get("status") for e in events
                          if e["kind"] == "tool_finished"],
    }
print("RESULT:" + json.dumps(result, ensure_ascii=False))
''',
            [PTYSend(
                 b"go\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(b"\x02", after="RAW_READY"),
             PTYSend(b"/tasks\r", after="· background ·"),
             PTYSend(
                 b"/task bg000001\r", after="id        mode  status"),
             PTYSend(
                 b"/tasks\r", after="ZYLAB_TEST_BACKGROUND_COMPLETED"),
             PTYSend(b"normal\r", after="RAW_DONE"),
             PTYSend(b"failure\r", after="ZYLAB_TEST_NORMAL_IDLE"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_FAILURE_IDLE")])

        raw_index = output.find("RAW_READY")
        background_index = output.find("· background ·")
        self.assertGreaterEqual(raw_index, 0, output)
        self.assertGreater(background_index, raw_index, output)
        self.assertIn("/task bg000001 attach", output)
        self.assertRowOrder(output, background_index,
                            "⏺ Bash(", "· background ·")
        self.assertIn("RAW_DONE", output)
        done_index = output.rfind("RAW_DONE")
        completed_index = output.find("· completed ·", done_index)
        self.assertGreater(completed_index, done_index, output)
        # 原始输出的最后一行（RAW_DONE）与"已完成"提示必须各占一行——
        # 这正是旧的 \r\n 断言想说的事。
        self.assertRowOrder(output, completed_index,
                            "RAW_DONE", "· completed ·")
        self.assertIn("AFTER_BACKGROUND", output)
        self.assertIn("AFTER_FOREGROUND", output)
        foreground_index = output.find("FOREGROUND_RAW")
        self.assertGreater(foreground_index, completed_index, output)
        foreground_exit = output.find("exit 7", foreground_index)
        after_foreground = output.find("AFTER_FOREGROUND", foreground_exit)
        self.assertGreater(foreground_exit, foreground_index, output)
        self.assertGreater(after_foreground, foreground_exit, output)
        # 原文会先短暂出现在 footer activity preview，随后失败结果完整写入
        # scrollback；PTY 捕获的是重绘流，不是最终屏幕，不能据出现次数判重。
        self.assertGreaterEqual(
            output[:output.rfind("RESULT:")].count("FOREGROUND_RAW"), 1)
        self.assertIn("AFTER_FAILURE", output)
        self.assertIn("timeout 必须是非负有限数字", output)
        self.assertEqual(result["calls"], 6)
        self.assertEqual(result["task_status"], "completed")
        self.assertTrue(result["task_background"])
        self.assertEqual(result["foreground_status"], "completed")
        self.assertEqual(result["failure_status"], "failed")
        self.assertEqual(
            result["tool_statuses"],
            ["backgrounded", "completed", "failed"])
        self.assertEqual(len(result["tool_results"]), 3)
        self.assertIn("已转后台", result["tool_results"][0])
        self.assertIn(
            "timeout 必须是非负有限数字",
            result["tool_results"][2])
        kinds = result["journal_kinds"]
        self.assertLess(kinds.index("task_background_requested"),
                        kinds.index("tool_finished"))
        self.assertIn("task_finished", kinds)
        self.assertNotIn("tool_cancel_requested", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
