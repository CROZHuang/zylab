"""M3c-1b TaskManager：顺序、artifact、有界输出与进程组取消。"""
import json
import os
import signal
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from unittest import mock as _mock
from core import tasks, tools


def fake_runner(name, args, *, on_note=None, context=None, task=None):
    del name, args, on_note, context, task
    return "OK"


class TaskLifecycleTests(unittest.TestCase):
    def manager(self, root, runner=fake_runner, **kwargs):
        return tasks.TaskManager(
            root, runner, session_getter=lambda: "session-one",
            poll_interval=0.005, **kwargs)

    def test_started_event_precedes_runner_side_effect(self):
        called = []

        def runner(*_args, **_kwargs):
            called.append(True)
            return "DONE"

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts", runner=runner)
            events = manager.run("fake", {}, task_key="call-1")
            started = next(events)
            self.assertEqual(started["t"], "started")
            self.assertEqual(called, [])
            remaining = list(events)

        self.assertEqual(called, [True])
        self.assertEqual(remaining[-1]["t"], "completed")
        self.assertEqual(remaining[-1]["v"], "DONE")

    def test_worker_start_failure_is_one_terminal_and_releases_foreground(self):
        called = []

        def runner(*_args, **_kwargs):
            called.append(True)
            return "MUST NOT RUN"

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts", runner=runner)
            events = manager.run(
                "fake", {}, task_key="call-thread-start-failure")
            self.assertEqual(next(events)["t"], "started")
            with mock.patch.object(
                    threading.Thread, "start",
                    side_effect=RuntimeError("thread start failed")):
                remaining = list(events)

            terminal = [event for event in remaining if event["t"] in {
                "completed", "failed", "cancelled"}]
            self.assertEqual(called, [])
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["t"], "failed")
            self.assertIn("thread start failed", terminal[0]["v"])
            self.assertIsNone(manager.foreground())
            self.assertEqual(
                manager.get(terminal[0]["task_id"]).status,
                tasks.TaskStatus.FAILED)

    def test_cancel_between_started_and_worker_prevents_side_effect(self):
        called = []

        def runner(*_args, **_kwargs):
            called.append(True)
            return "SHOULD_NOT_RUN"

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts", runner=runner)
            events = manager.run("fake", {}, task_key="call-pre")
            self.assertEqual(next(events)["t"], "started")
            manager.cancel("call-pre")
            remaining = list(events)

        self.assertEqual(called, [])
        self.assertEqual(remaining[-1]["t"], "cancelled")
        self.assertIn("尚未启动", remaining[-1]["v"])

    def test_nonprocess_result_is_private_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            terminal = list(
                manager.run("fake", {}, task_key="call-result"))[-1]
            task = terminal["task"]
            path = Path(task["stdout_path"])

            self.assertEqual(path.read_text(encoding="utf-8"), "OK")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(task["stdout_bytes"], 2)
            self.assertEqual(
                task["stdout_sha256"],
                "565339bc4d33d72817b583024112eb7f5cdf3e5e"
                "ef0252d6ec1b9c9a94e12bb3")

    def test_artifact_index_maps_call_and_pages_full_result(self):
        payload = "\n".join(
            f"line-{index:03d} " + ("x" * 220)
            for index in range(200))

        def runner(*_args, **_kwargs):
            return payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifacts"
            manager = self.manager(
                root, runner=runner, id_factory=lambda: "page0001")
            terminal = list(manager.run(
                "bash", {}, task_key="call-page-0001"))[-1]
            index_path = root / "session-one" / tasks.ARTIFACT_INDEX_NAME
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = manager.artifact_entry("call-page-0001")

            self.assertEqual(
                stat.S_IMODE(index_path.stat().st_mode), 0o600)
            self.assertEqual(index["version"], tasks.ARTIFACT_INDEX_VERSION)
            self.assertEqual(entry["task_id"], "page0001")
            self.assertEqual(
                manager.artifact_entry("page0001")["tool_call_id"],
                "call-page-0001")
            stdout = entry["streams"]["stdout"]
            self.assertFalse(Path(stdout["relative_path"]).is_absolute())
            self.assertEqual(stdout["line_count"], 200)

            first = manager.artifact_page("call-page-0001", page=1)
            second = manager.artifact_page("page0001", page=2)
            first_text = "".join(
                section["text"] for section in first["sections"])
            second_text = "".join(
                section["text"] for section in second["sections"])
            self.assertEqual(first["page_bytes"], 30_000)
            self.assertLessEqual(len(first_text.encode()), 30_000)
            self.assertTrue(first["has_more"])
            self.assertFalse(second["has_more"])
            self.assertEqual(first_text + second_text, payload)

            Path(terminal["task"]["stdout_path"]).unlink()
            missing = manager.artifact_page("call-page-0001")
            self.assertEqual(missing["sections"], [])
            self.assertEqual(missing["missing"], ["stdout"])

    def test_artifact_pages_preserve_utf8_split_at_byte_boundary(self):
        payload = ("x" * 4_095) + "你" + "tail"
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts",
                runner=lambda *_args, **_kwargs: payload,
                id_factory=lambda: "utf80001")
            list(manager.run(
                "fake", {}, task_key="call-utf8-page"))

            first = manager.artifact_page(
                "call-utf8-page", page=1, page_bytes=4_096)
            second = manager.artifact_page(
                "call-utf8-page", page=2, page_bytes=4_096)
            rendered = "".join(
                section["text"]
                for view in (first, second)
                for section in view["sections"])

        self.assertEqual(rendered, payload)
        self.assertNotIn("�", rendered)

    def test_legacy_metrics_mapping_expands_existing_artifact_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifacts"
            session_dir = root / "legacy-session"
            session_dir.mkdir(parents=True)
            artifact = session_dir / "legacy01.stdout.log"
            artifact.write_text(
                "legacy line one\nlegacy line two\n",
                encoding="utf-8")
            rows = [{
                "id": "tool-run-legacy",
                "tool_call_id": "call-legacy-1",
                "session_id": "legacy-session",
                "name": "bash",
                "status": "completed",
                "exit_code": 0,
                "started_at": "2026-08-26T00:00:00+00:00",
                "ended_at": "2026-08-26T00:00:01+00:00",
                "stdout_path": str(artifact),
                "stderr_path": None,
            }]
            manager = tasks.TaskManager(
                root, fake_runner,
                session_getter=lambda: "legacy-session",
                legacy_artifact_getter=lambda session: (
                    rows if session == "legacy-session" else []))

            entry = manager.artifact_entry("call-legacy-1")
            page = manager.artifact_page("legacy01")
            manager.id_factory = lambda: "current01"
            list(manager.run(
                "bash", {}, task_key="call-legacy-1"))
            current = manager.artifact_entry("call-legacy-1")

        self.assertEqual(entry["index_source"], "metrics-read-only")
        self.assertEqual(entry["task_id"], "legacy01")
        self.assertEqual(
            page["sections"][0]["text"],
            "legacy line one\nlegacy line two\n")
        self.assertEqual(current["task_id"], "current01")
        self.assertNotIn("index_source", current)

    def test_registry_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts", max_history=2)
            for index in range(4):
                list(manager.run(
                    "fake", {}, task_key=f"call-{index}"))
            snapshots = manager.list()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(
            [item.key for item in snapshots],
            ["call-3", "call-2"])

    def test_known_secret_is_redacted_before_result_and_artifact(self):
        secret = "api-secret-value-123"

        def runner(*_args, on_note=None, **_kwargs):
            on_note(f"note {secret}")
            return f"before {secret} after"

        def failing_runner(*_args, **_kwargs):
            raise RuntimeError(f"failure {secret}")

        with tempfile.TemporaryDirectory() as tmp:
            manager = tasks.TaskManager(
                Path(tmp) / "artifacts", runner,
                session_getter=lambda: "secret-session",
                secret_values=[secret])
            events = list(manager.run(
                "fake", {}, task_key="call-secret"))
            terminal = events[-1]
            artifact = Path(terminal["task"]["stdout_path"])
            stored = artifact.read_text(encoding="utf-8")

            failing_manager = tasks.TaskManager(
                Path(tmp) / "error-artifacts", failing_runner,
                session_getter=lambda: "secret-session",
                secret_values=[secret])
            failed = list(failing_manager.run(
                "fake", {}, task_key="call-error"))[-1]
            failed_artifact = Path(failed["task"]["stdout_path"])
            failed_stored = failed_artifact.read_text(encoding="utf-8")

        self.assertNotIn(
            secret, "\n".join(str(event.get("v", "")) for event in events))
        self.assertNotIn(secret, terminal["v"])
        self.assertNotIn(secret, stored)
        self.assertIn("[REDACTED_SECRET]", terminal["v"])
        self.assertEqual(
            terminal["task"]["redaction_count"], 2)
        self.assertEqual(failed["t"], "failed")
        self.assertNotIn(secret, failed["v"])
        self.assertNotIn(secret, failed["task"]["error"])
        self.assertNotIn(secret, failed_stored)
        self.assertIn("[REDACTED_SECRET]", failed["task"]["error"])

    def test_runtime_events_are_bounded_structured_and_redacted(self):
        secret = "agent-secret-value-123"

        def runner(*_args, task=None, **_kwargs):
            self.assertTrue(task.runtime_event({
                "kind": "agent_spawned",
                "payload": {"id": "a-one", "task": secret},
            }))
            self.assertTrue(task.runtime_event({
                "kind": "agent_state_changed",
                "payload": {"id": "a-one", "state": "running"},
            }))
            return "DONE"

        with tempfile.TemporaryDirectory() as tmp:
            manager = tasks.TaskManager(
                Path(tmp) / "artifacts", runner,
                session_getter=lambda: "runtime-session",
                poll_interval=0.005, secret_values=[secret])
            events = list(manager.run(
                "subagent", {}, task_key="call-agent"))

        runtime_events = [
            event for event in events if event["t"] == "runtime"]
        self.assertEqual(
            [event["event"]["kind"] for event in runtime_events],
            ["agent_spawned", "agent_state_changed"])
        self.assertNotIn(secret, str(runtime_events))
        self.assertIn(
            "[REDACTED_SECRET]",
            runtime_events[0]["event"]["payload"]["task"])
        self.assertLess(
            events.index(runtime_events[-1]), len(events) - 1)
        self.assertEqual(events[-1]["t"], "completed")
        self.assertEqual(
            events[-1]["task"]["runtime_event_dropped"], 0)

    def test_workspace_change_is_not_dropped_when_runtime_queue_is_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            task = manager._new_task("bash", "call-workspace-change")
            for index in range(64):
                self.assertTrue(task.runtime_event({
                    "kind": "probe",
                    "payload": {"index": index},
                }))
            self.assertTrue(task.runtime_event({
                "kind": "workspace_changed",
                "payload": {"tool": "bash"},
            }))
            events = task.drain_runtime_events()
            task.close_artifacts()
        self.assertEqual(len(events), 65)
        self.assertEqual(events[-1]["kind"], "workspace_changed")
        self.assertEqual(task.snapshot().runtime_event_dropped, 0)

    def test_ui_decoder_preserves_split_utf8_and_resets_after_gap(self):
        encoded = "你".encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts", ui_ring_bytes=2)
            task = manager._new_task("bash", "call-utf8")

            task._write_output("stdout", encoded[:1])
            first = task.drain_output()
            task._write_output("stdout", encoded[1:])
            second = task.drain_output()

            self.assertEqual(first, [])
            self.assertEqual(
                "".join(value for _stream, value in first + second),
                "你")
            self.assertNotIn("�", str(first + second))

            # decoder 正暂存一个 UTF-8 前缀；ring 丢掉中间的 X 后，不能把
            # gap 两侧字节误拼回一个看似合法的“你”。
            task._publish_output("stdout", encoded[:1])
            self.assertEqual(task.drain_output(), [])
            task._publish_output("stdout", b"X" + encoded[1:])
            after_gap = task.drain_output()
            self.assertNotIn(
                "你", "".join(value for _stream, value in after_gap))
            self.assertGreater(task.snapshot().ui_dropped_bytes, 0)

            # done 后 final=True 必须冲出尚未完成的尾字节，不能静默丢失。
            task._publish_output("stderr", encoded[:1])
            self.assertEqual(task.drain_output(), [])
            task.finish(tasks.TaskStatus.COMPLETED, "DONE")
            final = task.drain_output()
            self.assertEqual(final, [("stderr", "�")])
            manager._finish_task(task)

    def test_artifact_close_error_still_reaches_failed_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            events = manager.run(
                "fake", {}, task_key="call-close-error")
            started = next(events)
            task = manager._tasks[started["task_id"]]
            with mock.patch.object(
                    task, "close_artifacts",
                    side_effect=OSError("fsync failed")):
                remaining = list(events)

            terminal = remaining[-1]
            self.assertEqual(terminal["t"], "failed")
            self.assertTrue(task.is_done())
            self.assertIn("OSError: fsync failed", terminal["task"]["error"])
            self.assertIn("artifact 收尾失败", terminal["v"])
            task.close_artifacts()

    def test_artifact_write_error_hits_worker_terminal_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            events = manager.run(
                "fake", {}, task_key="call-write-error")
            started = next(events)
            task = manager._tasks[started["task_id"]]
            with mock.patch.object(
                    task._stdout, "write",
                    side_effect=OSError("disk full")):
                remaining = list(events)

            terminal = remaining[-1]
            self.assertEqual(terminal["t"], "failed")
            self.assertTrue(task.is_done())
            self.assertIn("OSError", terminal["task"]["error"])
            self.assertIn("disk full", terminal["v"])

    def test_background_drain_preserves_events_and_terminal_once(self):
        release = threading.Event()

        def runner(*_args, on_note=None, task=None, **_kwargs):
            release.wait(1.0)
            on_note("background note")
            task.runtime_event({
                "kind": "background_probe",
                "payload": {"state": "ready"},
            })
            return "BACKGROUND DONE"

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts", runner=runner)
            events = manager.run("bash", {}, task_key="call-bg-events")
            self.assertEqual(next(events)["t"], "started")
            self.assertEqual(next(events)["t"], "progress")

            snapshot = manager.background("call-bg-events")
            self.assertTrue(snapshot.background)
            self.assertIsNotNone(snapshot.backgrounded_at)
            self.assertIsNone(manager.foreground())
            detached = list(events)
            self.assertEqual([event["t"] for event in detached], [
                "backgrounded"])
            self.assertEqual(
                detached[0]["task_id"], snapshot.id)

            release.set()
            drained = []
            deadline = time.monotonic() + 1.0
            while (not any(event["t"] in {
                    "completed", "failed", "cancelled"}
                    for event in drained)
                    and time.monotonic() < deadline):
                drained.extend(manager.drain_background())
                time.sleep(0.005)

            terminal = [event for event in drained if event["t"] in {
                "completed", "failed", "cancelled"}]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["t"], "completed")
            self.assertEqual(terminal[0]["v"], "BACKGROUND DONE")
            self.assertEqual(
                [event["event"]["kind"] for event in drained
                 if event["t"] == "runtime"],
                ["background_probe"])
            self.assertEqual(
                [event["v"] for event in drained
                 if event["t"] == "note"],
                ["background note"])
            self.assertEqual(manager.drain_background(), [])
            self.assertEqual(
                manager.get(snapshot.id).status,
                tasks.TaskStatus.COMPLETED)

    def test_background_rejects_non_bash_and_non_foreground(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            events = manager.run("fake", {}, task_key="call-not-bash")
            self.assertEqual(next(events)["t"], "started")
            with self.assertRaisesRegex(
                    tasks.TaskError, "只有 foreground bash"):
                manager.background()
            list(events)

            bash_events = manager.run(
                "bash", {}, task_key="call-background-once")
            self.assertEqual(next(bash_events)["t"], "started")
            manager.background()
            with self.assertRaisesRegex(
                    tasks.TaskError, "foreground"):
                manager.background("call-background-once")
            self.assertEqual(
                list(bash_events)[-1]["t"], "backgrounded")

            cancelling = manager.run(
                "bash", {}, task_key="call-cancelling")
            self.assertEqual(next(cancelling)["t"], "started")
            manager.cancel("call-cancelling")
            with self.assertRaisesRegex(
                    tasks.TaskError, "后台|结束|取消"):
                manager.background("call-cancelling")
            self.assertEqual(list(cancelling)[-1]["t"], "cancelled")

    def test_history_prunes_only_after_all_background_terminals_are_drained(self):
        release = threading.Event()

        def runner(_name, args, **_kwargs):
            release.wait(1.0)
            return f"DONE {args['index']}"

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts", runner=runner, max_history=1)
            ids = []
            for index in range(2):
                events = manager.run(
                    "bash", {"index": index},
                    task_key=f"call-prune-{index}")
                started = next(events)
                self.assertEqual(next(events)["t"], "progress")
                manager.background(started["task_id"])
                self.assertEqual(list(events)[-1]["t"], "backgrounded")
                ids.append(started["task_id"])

            release.set()
            deadline = time.monotonic() + 1.0
            while (not all(manager.get(task_id).status in tasks.TERMINAL_STATES
                           for task_id in ids)
                    and time.monotonic() < deadline):
                time.sleep(0.005)

            drained = manager.drain_background()
            terminal = [event for event in drained if event["t"] in {
                "completed", "failed", "cancelled"}]

            self.assertEqual(
                {event["task_id"] for event in terminal}, set(ids))
            self.assertEqual(len(manager.list()), 1)

    def test_background_terminal_defer_keeps_live_events_and_history(self):
        release = threading.Event()

        def runner(_name, args, *, on_note=None, task=None, **_kwargs):
            if not args.get("background"):
                return "SEED"
            release.wait(1.0)
            task._write_output("stdout", b"LIVE OUTPUT\n")
            on_note("LIVE NOTE")
            task.runtime_event({
                "kind": "live_runtime",
                "payload": {"state": "done"},
            })
            return "BACKGROUND TERMINAL"

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts", runner=runner, max_history=1)
            seed = list(manager.run(
                "fake", {}, task_key="call-defer-seed"))[-1]
            self.assertEqual(seed["t"], "completed")

            foreground = manager.run(
                "bash", {"background": True},
                task_key="call-defer-terminal")
            started = next(foreground)
            self.assertEqual(next(foreground)["t"], "progress")
            manager.background(started["task_id"])
            self.assertEqual(
                list(foreground)[-1]["t"], "backgrounded")
            release.set()

            deadline = time.monotonic() + 1.0
            while (manager.get(started["task_id"]).status
                   not in tasks.TERMINAL_STATES
                   and time.monotonic() < deadline):
                time.sleep(0.005)

            deferred = manager.drain_background(
                defer_terminal_ids={started["task_id"]})
            self.assertIn(
                "LIVE OUTPUT", "".join(
                    event["v"] for event in deferred
                    if event["t"] == "output"))
            self.assertEqual(
                [event["v"] for event in deferred
                 if event["t"] == "note"],
                ["LIVE NOTE"])
            self.assertEqual(
                [event["event"]["kind"] for event in deferred
                 if event["t"] == "runtime"],
                ["live_runtime"])
            self.assertFalse(any(
                event["t"] in {"completed", "failed", "cancelled"}
                for event in deferred))
            self.assertEqual(len(manager.list()), 2)

            still_deferred = manager.drain_background(
                defer_terminal_ids=started["task_id"])
            self.assertFalse(any(
                event["t"] in {"completed", "failed", "cancelled"}
                for event in still_deferred))
            self.assertEqual(len(manager.list()), 2)

            released = manager.drain_background()
            terminal = [event for event in released if event["t"] in {
                "completed", "failed", "cancelled"}]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["task_id"], started["task_id"])
            self.assertEqual(terminal[0]["v"], "BACKGROUND TERMINAL")
            self.assertEqual(len(manager.list()), 1)
            self.assertFalse(any(
                event["t"] in {"completed", "failed", "cancelled"}
                for event in manager.drain_background()))

    def test_background_survives_consumer_close_after_started(self):
        called = threading.Event()

        def runner(*_args, **_kwargs):
            called.set()
            return "DETACHED DONE"

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts", runner=runner)
            events = manager.run(
                "bash", {}, task_key="call-close-handoff")
            started = next(events)
            manager.background(started["task_id"])
            events.close()
            self.assertTrue(called.wait(1.0))

            drained = []
            deadline = time.monotonic() + 1.0
            while (not any(event["t"] == "completed"
                           for event in drained)
                    and time.monotonic() < deadline):
                drained.extend(manager.drain_background())
                time.sleep(0.005)

            terminal = [event for event in drained
                        if event["t"] == "completed"]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["v"], "DETACHED DONE")

    def test_tail_is_repeatable_and_does_not_drain_ui_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            terminal = list(manager.run(
                "fake", {}, task_key="call-tail"))[-1]
            first = manager.tail(terminal["task_id"], limit=1)
            second = manager.tail("call-tail", limit=1)

        self.assertEqual(first, second)
        self.assertEqual(first["stdout"]["text"], "K")
        self.assertEqual(first["stdout"]["total_bytes"], 2)
        self.assertEqual(first["stdout"]["returned_bytes"], 1)
        self.assertTrue(first["stdout"]["truncated"])
        self.assertEqual(first["stderr"]["text"], "")
        with self.assertRaisesRegex(tasks.TaskError, "正整数"):
            manager.tail("call-tail", limit=0)

    def test_tail_preserves_utf8_character_split_by_byte_limit(self):
        cases = (
            ("A你", 2, "你", 3, 4),
            ("A😀", 1, "😀", 4, 5),
        )
        for payload, limit, expected, returned, total in cases:
            with self.subTest(payload=payload, limit=limit):
                with tempfile.TemporaryDirectory() as tmp:
                    manager = self.manager(
                        Path(tmp) / "artifacts",
                        runner=lambda *_args, value=payload, **_kwargs: value,
                        id_factory=lambda: "utf8tail")
                    list(manager.run(
                        "fake", {}, task_key="call-utf8-tail"))

                    value = manager.tail(
                        "call-utf8-tail", limit=limit)["stdout"]

                self.assertEqual(value["text"], expected)
                self.assertEqual(value["total_bytes"], total)
                self.assertEqual(value["returned_bytes"], returned)
                self.assertTrue(value["truncated"])
                self.assertNotIn("�", value["text"])


class BashTaskTests(unittest.TestCase):
    @staticmethod
    def run_test_tool(name, args, *, on_note=None, context=None, task=None):
        """Exercise TaskManager without requiring a nested OS sandbox.

        Sandbox policy and adapter integration have their own test modules.
        These tests run real process groups and are also invoked from inside
        zylab's sandbox, where starting a second mount namespace is not a
        supported capability.
        """
        del context
        execution_context = tools.ExecutionContext.capture(
            session="bash-session",
            hook_config={
                "sandbox": {
                    "mode": "disabled",
                    "network_isolation": False,
                },
            })
        return tools.run(
            name, args, on_note=on_note,
            context=execution_context, task=task)

    def manager(self, root, **kwargs):
        return tasks.TaskManager(
            root, self.run_test_tool,
            session_getter=lambda: "bash-session",
            poll_interval=0.005, **kwargs)

    def await_ready(self, manager, events, task_key, timeout=5.0):
        """Wait through measured cold sandbox startup; clean up on failure."""
        ready = False
        pgid = None
        deadline = time.monotonic() + timeout
        while (pgid is None or not ready) and time.monotonic() < deadline:
            try:
                event = next(events)
            except StopIteration:
                break
            if event["t"] == "output" and "READY" in event.get("v", ""):
                ready = True
            pgid = manager.get(task_key).process_pid
        if pgid is None or not ready:
            events.close()
            manager.shutdown()
        self.assertIsNotNone(pgid)
        self.assertTrue(ready)
        return pgid

    @staticmethod
    def wait_group_gone(pgid, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.01)
        return False

    @staticmethod
    def wait_process_not_running(pid, timeout=1.0):
        deadline = time.monotonic() + timeout
        stat_path = Path(f"/proc/{pid}/stat")
        while time.monotonic() < deadline:
            try:
                state = stat_path.read_text(
                    encoding="utf-8").split()[2]
            except (FileNotFoundError, IndexError, OSError):
                return True
            if state == "Z":
                return True
            time.sleep(0.01)
        return False

    def test_large_output_is_fully_spooled_but_ui_ring_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts",
                ui_ring_bytes=4096)
            events = list(manager.run(
                "bash",
                {"command": "yes x | head -c 200000", "timeout": 10},
                task_key="call-large"))
            terminal = events[-1]
            task = terminal["task"]
            artifact = Path(task["stdout_path"])

            self.assertEqual(terminal["t"], "completed")
            self.assertEqual(task["stdout_bytes"], 200000)
            self.assertEqual(artifact.stat().st_size, 200000)
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            self.assertTrue(task["truncated"])
            self.assertGreater(task["ui_dropped_bytes"], 0)
            self.assertIn("完整输出 artifact", terminal["v"])
            ui_chunks = [
                len(event.get("v", "").encode("utf-8"))
                for event in events if event["t"] == "output"]
            self.assertTrue(ui_chunks)
            self.assertLessEqual(max(ui_chunks), 4096)

    def test_cancel_escalates_and_removes_ignoring_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts",
                interrupt_grace=0.04, terminate_grace=0.04)
            events = manager.run(
                "bash",
                {"command": (
                    "exec bash -c 'trap \"\" INT TERM; "
                    "echo READY; sleep 30 & wait'"),
                 "timeout": 10},
                task_key="call-cancel")
            self.assertEqual(next(events)["t"], "started")
            pgid = self.await_ready(
                manager, events, "call-cancel")

            started = time.monotonic()
            manager.cancel("call-cancel")
            remaining = list(events)
            elapsed = time.monotonic() - started
            terminal = remaining[-1]

            self.assertEqual(terminal["t"], "cancelled")
            self.assertLess(elapsed, 1.0)
            self.assertTrue(self.wait_group_gone(pgid))
            self.assertEqual(terminal["task"]["signal"], signal.SIGKILL)
            self.assertIn("用户中断", terminal["v"])

    def test_timeout_is_distinct_from_user_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts",
                interrupt_grace=0.02, terminate_grace=0.04)
            terminal = list(manager.run(
                "bash",
                {"command": (
                    "exec bash -c 'trap \"\" TERM; "
                    "sleep 30 & wait'"),
                 "timeout": 0.04},
                task_key="call-timeout"))[-1]

            self.assertEqual(terminal["t"], "failed")
            self.assertTrue(terminal["task"]["timed_out"])
            self.assertIsNone(
                terminal["task"]["cancel_requested_at"])
            self.assertIn("超时", terminal["v"])

    def test_invalid_timeout_fails_before_process_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            with mock.patch(
                    "core.tasks.subprocess.Popen",
                    wraps=tasks.subprocess.Popen) as popen:
                for index, value in enumerate((
                        "not-a-number", float("nan"),
                        float("inf"), -1)):
                    with self.subTest(timeout=value):
                        terminal = list(manager.run(
                            "bash",
                            {"command": "sleep 8", "timeout": value},
                            task_key=f"call-invalid-timeout-{index}"))[-1]
                        self.assertEqual(terminal["t"], "failed")
                        self.assertIn(
                            "timeout 必须是非负有限数字", terminal["v"])
                        self.assertIsNone(
                            terminal["task"]["process_pid"])
            popen.assert_not_called()

    def test_selector_register_failure_kills_reaps_and_unbinds(self):
        real_popen = tasks.subprocess.Popen
        real_selector = tasks.selectors.DefaultSelector()
        failing_selector = mock.Mock(wraps=real_selector)
        failing_selector.register.side_effect = OSError(
            "selector register failed")
        spawned = []

        def capture_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc

        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts",
                interrupt_grace=0.02, terminate_grace=0.04)
            with mock.patch(
                    "core.tasks.subprocess.Popen",
                    side_effect=capture_popen), mock.patch(
                    "core.tasks.selectors.DefaultSelector",
                    return_value=failing_selector):
                terminal = list(manager.run(
                    "bash",
                    {"command": "sleep 8", "timeout": 2},
                    task_key="call-selector-register"))[-1]

            self.assertEqual(len(spawned), 1)
            proc = spawned[0]
            task = manager._tasks[terminal["task_id"]]
            self.assertEqual(terminal["t"], "failed")
            self.assertIn("selector register failed", terminal["v"])
            self.assertIsNotNone(terminal["task"]["process_pid"])
            self.assertTrue(self.wait_process_not_running(proc.pid))
            self.assertTrue(self.wait_group_gone(proc.pid))
            self.assertIsNotNone(proc.poll())
            self.assertTrue(proc.stdout.closed)
            self.assertTrue(proc.stderr.closed)
            self.assertIsNone(task._process)
            failing_selector.close.assert_called_once_with()

    def test_real_bash_continues_after_generator_backgrounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            events = manager.run(
                "bash",
                {"command": "echo READY; sleep 0.3; echo DONE",
                 "timeout": 10},
                task_key="call-real-background")
            self.assertEqual(next(events)["t"], "started")
            self.await_ready(
                manager, events, "call-real-background")

            snapshot = manager.background()
            detached = list(events)
            self.assertEqual(detached[-1]["t"], "backgrounded")
            self.assertTrue(snapshot.background)
            self.assertIsNone(manager.foreground())

            drained = []
            deadline = time.monotonic() + 2.0
            while (not any(event["t"] == "completed"
                           for event in drained)
                    and time.monotonic() < deadline):
                drained.extend(manager.drain_background())
                time.sleep(0.005)

            self.assertIn(
                "DONE", "".join(
                    event["v"] for event in drained
                    if event["t"] == "output"))
            terminal = [event for event in drained
                        if event["t"] == "completed"]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(
                manager.get(snapshot.id).status,
                tasks.TaskStatus.COMPLETED)

    def test_tail_reads_each_process_artifact_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp) / "artifacts")
            terminal = list(manager.run(
                "bash",
                {"command": (
                    "printf 0123456789; printf abcdefghij >&2"),
                 "timeout": 10},
                task_key="call-bash-tail"))[-1]
            value = manager.tail(terminal["task_id"], limit=4)

        self.assertEqual(value["stdout"]["text"], "6789")
        self.assertEqual(value["stderr"]["text"], "ghij")
        self.assertEqual(value["stdout"]["total_bytes"], 10)
        self.assertEqual(value["stderr"]["total_bytes"], 10)
        self.assertTrue(value["stdout"]["truncated"])
        self.assertTrue(value["stderr"]["truncated"])

    def test_shutdown_kills_background_ignoring_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts",
                interrupt_grace=0.04, terminate_grace=0.04)
            events = manager.run(
                "bash",
                {"command": (
                    "exec bash -c 'trap \"\" INT TERM; "
                    "echo READY; sleep 30 & wait'"),
                 "timeout": 10},
                task_key="call-shutdown")
            self.assertEqual(next(events)["t"], "started")
            pgid = self.await_ready(
                manager, events, "call-shutdown")

            snapshot = manager.background()
            self.assertEqual(list(events)[-1]["t"], "backgrounded")
            closed = manager.shutdown()
            target = next(item for item in closed
                          if item.id == snapshot.id)

            self.assertEqual(target.status, tasks.TaskStatus.CANCELLED)
            self.assertEqual(target.signal, signal.SIGKILL)
            self.assertTrue(self.wait_group_gone(pgid))
            terminal = [event for event in manager.drain_background()
                        if event["t"] == "cancelled"]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(manager.drain_background(), [])

    def test_shell_leader_exit_cleans_same_group_background_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(
                Path(tmp) / "artifacts",
                interrupt_grace=0.02, terminate_grace=0.04)
            events = list(manager.run(
                "bash",
                {"command": (
                    "sleep 8 >/dev/null 2>&1 & echo $!"),
                 "timeout": 10},
                task_key="call-orphan-cleanup"))
            terminal = events[-1]
            process_output = "".join(
                event.get("v", "") for event in events
                if event["t"] == "output")
            pid_lines = [
                line.strip() for line in process_output.splitlines()
                if line.strip().isdigit()]
            self.assertTrue(pid_lines, process_output)
            child_pid = int(pid_lines[0])
            pgid = terminal["task"]["process_pid"]

            self.assertEqual(terminal["t"], "completed")
            self.assertTrue(
                terminal["v"].startswith("[UNSANDBOXED]"), terminal["v"])
            self.assertEqual(terminal["task"]["returncode"], 0)
            self.assertIsNone(terminal["task"]["signal"])
            self.assertTrue(any(
                event["t"] == "note"
                and "残留子进程" in event["v"]
                for event in events))
            self.assertTrue(self.wait_process_not_running(child_pid))
            self.assertTrue(self.wait_group_gone(pgid))


class SafetyTests(unittest.TestCase):
    def test_artifact_fsync_failure_still_closes_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = tasks._Artifact(Path(tmp) / "artifact.log")
            artifact.write(b"data")
            with mock.patch(
                    "core.tasks.os.fsync",
                    side_effect=OSError("fsync failed")):
                with self.assertRaisesRegex(OSError, "fsync failed"):
                    artifact.close()

            self.assertIsNone(artifact._stream)
            self.assertEqual(
                artifact.path.read_bytes(), b"data")

    def test_secret_redactor_handles_chunk_boundary(self):
        secret = "split-secret-value"
        redactor = tasks._SecretRedactor([secret])
        output = (
            redactor.feed(b"before split-sec")
            + redactor.feed(b"ret-value after", final=True))
        self.assertNotIn(secret.encode(), output)
        self.assertEqual(
            output, b"before [REDACTED_SECRET] after")
        self.assertEqual(redactor.count, 1)

    def test_protected_artifact_root_is_rejected_without_write(self):
        with _mock.patch.dict(
                os.environ,
                {"ZYLAB_PROTECTED_PATHS": "/protected/archive"}), \
                self.assertRaisesRegex(
                tasks.TaskError, "受保护路径"):
            tasks.TaskManager(
                "/protected/archive/.zylab-artifacts", fake_runner)


if __name__ == "__main__":
    unittest.main()
