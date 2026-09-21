"""M3c-2/3 Agent runtime 与交互 workspace。"""
import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from unittest import mock as _mock
from core import agent as agent_module
from core import agents, client, store, subagent, tools
from tests.platform_support import requires_symlinks  # noqa: E402


def execution():
    return tools.ExecutionContext.capture(
        session="parent-session", turn_id="parent-turn",
        model="model-one", gateway="boyue",
        permission_mode="default", hook_config={"safe": True})


class FakeAgent:
    instances = []

    def __init__(self, model, gateway=None, load_md=True):
        self.model = model
        self.gateway = gateway
        self.load_md = load_md
        self.messages = []
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_total = 0
        self.loaded_context = None
        self.allowed_tools_seen = None
        self.inputs = []
        type(self).instances.append(self)

    def load_context(self, value):
        self.loaded_context = value

    def context_snapshot(self):
        return {"resume": len(self.inputs)}

    def run(self, user_input, *, allowed_tools=None, event_sink=None,
            inbox_source=None, cancel=None, **_kwargs):
        self.inputs.append(user_input)
        self.allowed_tools_seen = frozenset(allowed_tools or ())
        events = []
        if user_input is not None:
            message = {"role": "user", "content": str(user_input)}
            self.messages.append(message)
            events.append({
                "kind": "user_message", "payload": {"message": message},
            })
        for item in list(inbox_source() if inbox_source else ()):
            message = {"role": "user", "content": item["text"]}
            self.messages.append(message)
            events.append({
                "kind": "user_message",
                "payload": {
                    "message": message, "source": "agent_inbox",
                    "inbox_id": item["id"],
                },
            })
        if events:
            event_sink(events)
        if cancel is not None and cancel.is_set():
            yield {"t": "interrupted"}
            return
        users = [
            item["content"] for item in self.messages
            if item.get("role") == "user"
        ]
        answer = f"report-{len(users)}: {users[-1]}"
        message = {"role": "assistant", "content": answer}
        self.messages.append(message)
        self.tokens_in += 11
        self.tokens_out += 3
        self.last_total += 14
        event_sink([{
            "kind": "assistant_message", "payload": {"message": message},
        }])
        yield {"t": "text", "v": answer}
        yield {"t": "end", "reason": "stop"}


class BrokenAgent(FakeAgent):
    def run(self, *_args, **_kwargs):
        raise RuntimeError("child exploded")
        yield  # pragma: no cover


class InlineReasoningAgent(FakeAgent):
    raw_answer = (
        "<think>private chain of thought must stay in the raw transcript"
        "\n\nVISIBLE_REPORT")

    def run(self, user_input, *, allowed_tools=None, event_sink=None,
            cancel=None, **_kwargs):
        self.inputs.append(user_input)
        self.allowed_tools_seen = frozenset(allowed_tools or ())
        user = {"role": "user", "content": str(user_input)}
        assistant = {
            "role": "assistant",
            "content": self.raw_answer,
        }
        self.messages.extend((user, assistant))
        event_sink([
            {"kind": "user_message", "payload": {"message": user}},
            {"kind": "assistant_message", "payload": {
                "message": assistant}},
        ])
        yield {"t": "text", "v": self.raw_answer}
        yield {"t": "end", "reason": "stop"}


class ConcurrentBatchAgent(FakeAgent):
    lock = threading.Lock()
    active = 0
    max_active = 0
    all_started = threading.Event()

    def run(self, user_input, **kwargs):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.max_active = max(cls.max_active, cls.active)
            if cls.active >= 3:
                cls.all_started.set()
        try:
            if not cls.all_started.wait(1.0):
                raise RuntimeError("batch children did not start concurrently")
            yield from super().run(user_input, **kwargs)
        finally:
            with cls.lock:
                cls.active -= 1


class GreedyBatchAgent(FakeAgent):
    accepted_attempts = 0
    lock = threading.Lock()

    def run(self, user_input, *, before_provider_attempt=None, **kwargs):
        for attempt in range(1, 20):
            before_provider_attempt({"attempt": attempt})
            with type(self).lock:
                type(self).accepted_attempts += 1
        yield from super().run(user_input, **kwargs)


class BlockingAgent(FakeAgent):
    started = threading.Event()

    def run(self, user_input, *, cancel=None, **_kwargs):
        self.inputs.append(user_input)
        type(self).started.set()
        while cancel is None or not cancel.wait(0.005):
            pass
        yield {"t": "interrupted"}


class GateResumeRuntime(agents.AgentRuntime):
    """把 resume 卡在 claim 前，确定性验证并发 send 复用单 worker。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resume_started = threading.Event()
        self.resume_gate = threading.Event()
        self.resume_calls = 0
        self.resume_lock = threading.Lock()

    def resume(self, identifier, **kwargs):
        with self.resume_lock:
            self.resume_calls += 1
        self.resume_started.set()
        if not self.resume_gate.wait(1.0):
            raise RuntimeError("test resume gate timeout")
        return super().resume(identifier, **kwargs)


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        FakeAgent.instances = []
        BrokenAgent.instances = []
        ConcurrentBatchAgent.active = 0
        ConcurrentBatchAgent.max_active = 0
        ConcurrentBatchAgent.all_started = threading.Event()
        GreedyBatchAgent.accepted_attempts = 0

    def test_run_has_stable_identity_private_raw_transcript_and_read_only_tools(self):
        lifecycle = []
        parent_messages = [{"role": "user", "content": "parent secret"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agent-runs"
            runtime = agents.AgentRuntime(root, agent_factory=FakeAgent)
            result = runtime.run_new(
                "inspect the repository", "only Python",
                execution_context=execution(),
                parent_tool_call_id="call-7",
                on_lifecycle=lifecycle.append)
            rows = runtime.list(parent_session_id="parent-session")
            record = runtime.get(rows[0]["id"])
            before_hash = record["transcript"]["raw_sha256"]
            parent_messages.append(
                {"role": "assistant", "content": "compacted parent"})
            after = runtime.get(record["id"])
            state_path = root / record["id"] / "state.json"

            self.assertRegex(record["id"], r"^a-[0-9a-f]{10}$")
            self.assertEqual(
                record["transcript_session_id"],
                "c-" + record["id"].split("-", 1)[1])
            self.assertIn(f"[agent {record['id']} · completed", result)
            self.assertEqual(record["parent_turn_id"], "parent-turn")
            self.assertEqual(record["parent_tool_call_id"], "call-7")
            self.assertEqual(record["model"], "model-one")
            self.assertEqual(record["gateway"], "boyue")
            self.assertEqual(record["state"], "completed")
            self.assertEqual(before_hash, after["transcript"]["raw_sha256"])
            self.assertNotIn(
                "parent secret", str(after["transcript"]["messages"]))
            self.assertEqual(
                stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(state_path.stat().st_mode), 0o600)

        self.assertEqual(
            FakeAgent.instances[0].allowed_tools_seen,
            agents.CHILD_TOOLS)
        self.assertNotIn("todo_write", agents.CHILD_TOOLS)
        self.assertNotIn("subagent", agents.CHILD_TOOLS)
        self.assertNotIn("write_file", agents.CHILD_TOOLS)
        self.assertEqual(
            [item["kind"] for item in lifecycle],
            ["agent_spawned", "agent_state_changed",
             "agent_state_changed", "agent_result_received"])
        seqs = [item["seq"] for item in record["events"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_reasoning_is_hidden_from_report_but_raw_transcript_is_unchanged(self):
        closed = agents.project_report(
            "header\n<think>closed private reasoning</think>\nanswer")
        self.assertNotIn("closed private reasoning", closed)
        self.assertIn(agents.REASONING_HIDDEN_NOTICE, closed)
        self.assertIn("answer", closed)

        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=InlineReasoningAgent)
            formatted = runtime.run_new(
                "smoke", execution_context=execution())
            record = runtime.get(runtime.list()[0]["id"])

        raw = next(
            item["content"] for item in record["transcript"]["messages"]
            if item.get("role") == "assistant")
        self.assertEqual(raw, InlineReasoningAgent.raw_answer)
        self.assertIn("private chain of thought", raw)
        self.assertNotIn("private chain of thought", record["result"])
        self.assertNotIn("private chain of thought", formatted)
        self.assertIn(agents.REASONING_HIDDEN_NOTICE, record["result"])
        self.assertIn("VISIBLE_REPORT", record["result"])

    def test_send_then_resume_keeps_route_and_delivers_separate_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            runtime.run_new("first task", execution_context=execution())
            run_id = runtime.list()[0]["id"]
            runtime.send(run_id, "first correction")
            runtime.send(run_id, "second correction")
            queued = runtime.get(run_id)
            result = runtime.resume(run_id)
            record = runtime.get(run_id)

        self.assertTrue(queued["resume_pending"])
        self.assertIn("report-3: second correction", result)
        users = [
            item["content"] for item in record["transcript"]["messages"]
            if item.get("role") == "user"
        ]
        self.assertEqual(
            users, ["first task", "first correction", "second correction"])
        inbox_events = [
            item for item in record["events"]
            if item["kind"] == "user_message"
            and item["payload"].get("source") == "agent_inbox"
        ]
        self.assertEqual(len(inbox_events), 2)
        self.assertNotEqual(
            inbox_events[0]["payload"]["inbox_id"],
            inbox_events[1]["payload"]["inbox_id"])
        self.assertEqual(
            [item["state"] for item in record["inbox"]],
            ["delivered", "delivered"])
        self.assertEqual(record["run_count"], 2)
        self.assertEqual(record["model"], "model-one")
        self.assertEqual(record["gateway"], "boyue")
        self.assertEqual(
            [(item.model, item.gateway) for item in FakeAgent.instances],
            [("model-one", "boyue"), ("model-one", "boyue")])

    def test_busy_claim_stale_recovery_and_completion_boundary_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_store = agents.AgentStore(Path(tmp) / "runs")
            record = agent_store.create(
                task="task", context=None, execution_context=execution())
            agent_store.claim(record["id"], owner_id="one")
            with self.assertRaises(agents.AgentBusy):
                agent_store.claim(
                    record["id"], owner_id="two", resume=True)

            def reused_pid(value):
                value["owner_pid"] = os.getpid()
                value["owner_boot_id"] = store._boot_id()
                value["owner_proc_start"] = "definitely-not-this-process"
                return value

            agent_store._update(record["id"], reused_pid)
            reused = agent_store.claim(
                record["id"], owner_id="two", resume=True)
            self.assertEqual(reused["owner_proc_start"],
                             store._proc_start(os.getpid()))

            def stale(value):
                value["owner_pid"] = 999_999_999
                return value

            agent_store._update(record["id"], stale)
            claimed = agent_store.claim(
                record["id"], owner_id="two", resume=True)
            agent_store.queue_message(record["id"], "late correction")
            final = agent_store.finish(
                record["id"], "completed", result="draft")

        self.assertEqual(claimed["state"], "running")
        self.assertTrue(any(
            item["payload"].get("reason") == "stale_owner_recovered"
            for item in claimed["events"]))
        self.assertEqual(final["state"], "waiting")
        self.assertTrue(final["resume_pending"])
        self.assertIsNone(final["ended_at"])

    def test_failure_and_cancel_are_terminal_and_observable(self):
        with tempfile.TemporaryDirectory() as tmp:
            failed_runtime = agents.AgentRuntime(
                Path(tmp) / "failed", agent_factory=BrokenAgent)
            failed_text = failed_runtime.run_new(
                "fail", execution_context=execution())
            failed = failed_runtime.list()[0]

            cancel = threading.Event()
            cancel.set()
            cancel_runtime = agents.AgentRuntime(
                Path(tmp) / "cancelled", agent_factory=FakeAgent)
            cancel_text = cancel_runtime.run_new(
                "cancel", execution_context=execution(), cancel=cancel)
            cancelled = cancel_runtime.list()[0]

        self.assertEqual(failed["state"], "failed")
        self.assertIn("RuntimeError: child exploded", failed["error"])
        self.assertIn("子代理失败", failed_text)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIn("子代理被中断", cancel_text)

    def test_failed_resume_does_not_relabel_old_report_as_new_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            runtime.run_new("first", execution_context=execution())
            run_id = runtime.list()[0]["id"]
            old_result = runtime.get(run_id)["result"]
            runtime.send(run_id, "continue")
            runtime.agent_factory = BrokenAgent
            failed_text = runtime.resume(run_id)
            record = runtime.get(run_id)

        self.assertEqual(record["state"], "failed")
        self.assertNotEqual(record["result"], old_result)
        self.assertIn("RuntimeError: child exploded", record["result"])
        self.assertIn("子代理失败", failed_text)

    @requires_symlinks
    def test_protected_and_symlink_roots_are_rejected(self):
        with _mock.patch.dict(
                os.environ,
                {"ZYLAB_PROTECTED_PATHS": "/protected/archive"}), \
                self.assertRaisesRegex(
                agents.AgentRuntimeError, "受保护路径"):
            agents.AgentStore("/protected/archive/.zylab-agent-runs")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(target, target_is_directory=True)
            agent_store = agents.AgentStore(link)
            with self.assertRaisesRegex(
                    agents.AgentRuntimeError, "符号链接"):
                agent_store.create(
                    task="task", context=None,
                    execution_context=execution())

    def test_lookup_rejects_absolute_and_traversal_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = agents.AgentStore(root / "outside")
            foreign = outside.create(
                task="foreign", context=None,
                execution_context=execution())
            self.assertEqual(
                outside.get(foreign["id"][:8])["id"], foreign["id"])
            before = (root / "outside" / foreign["id"] / "state.json").read_bytes()
            local = agents.AgentStore(root / "local")

            for identifier in (
                    str(root / "outside" / foreign["id"]),
                    "../outside/" + foreign["id"],
                    "..", "/tmp/agent"):
                with self.subTest(identifier=identifier):
                    with self.assertRaisesRegex(
                            agents.AgentRuntimeError, "无效 child agent"):
                        local.get(identifier)
                    with self.assertRaisesRegex(
                            agents.AgentRuntimeError, "无效 child agent"):
                        local.queue_message(identifier, "must not write")

            after = (root / "outside" / foreign["id"] / "state.json").read_bytes()

        self.assertEqual(after, before)

    def test_subagent_tool_forwards_lifecycle_through_task_handle_contract(self):
        class Task:
            key = "provider-call-9"

            def __init__(self):
                self.cancel_event = threading.Event()
                self.runtime_events = []
                self.notes = []

            def runtime_event(self, event):
                self.runtime_events.append(event)
                return True

            def note(self, value):
                self.notes.append(value)

        task = Task()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            with mock.patch(
                    "core.subagent.agents.AgentRuntime",
                    return_value=runtime):
                result = tools.t_subagent(
                    "inspect", _task=task,
                    _execution_context=execution())
            record = runtime.get(runtime.list()[0]["id"])

        self.assertIn(f"[agent {record['id']}", result)
        self.assertEqual(
            [item["kind"] for item in task.runtime_events],
            ["agent_spawned", "agent_state_changed",
             "agent_state_changed", "agent_result_received"])
        self.assertEqual(record["parent_tool_call_id"], "provider-call-9")
        self.assertEqual(record["parent_turn_id"], "parent-turn")

    def test_batch_subagents_start_concurrently_and_return_bounded_reports(self):
        tasks = [
            {"name": "one", "task": "inspect one"},
            {"name": "two", "task": "inspect two"},
            {"name": "three", "task": "inspect three"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = agents.AgentWorkspace(
                root=Path(tmp) / "runs",
                agent_factory=ConcurrentBatchAgent, poll_interval=0.005)
            try:
                result = subagent.run_batch(
                    tasks, workspace=workspace,
                    execution_context=execution(),
                    parent_tool_call_id="call-batch")
                rows = workspace.list(
                    parent_session_id="parent-session")
            finally:
                workspace.close()

        self.assertEqual(ConcurrentBatchAgent.max_active, 3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["state"] == "completed" for row in rows))
        self.assertTrue(all(
            row["parent_tool_call_id"] == "call-batch" for row in rows))
        self.assertLessEqual(len(result), subagent.MAX_COMBINED_REPORT + 500)

    def test_batch_subagents_enforce_per_child_provider_attempt_budget(self):
        tasks = [
            {"name": "one", "task": "inspect one"},
            {"name": "two", "task": "inspect two"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = agents.AgentWorkspace(
                root=Path(tmp) / "runs",
                agent_factory=GreedyBatchAgent, poll_interval=0.005)
            try:
                subagent.run_batch(
                    tasks, workspace=workspace,
                    execution_context=execution())
                rows = workspace.list(
                    parent_session_id="parent-session")
            finally:
                workspace.close()

        self.assertEqual(
            GreedyBatchAgent.accepted_attempts,
            2 * subagent.MAX_REQUESTS_PER_CHILD)
        self.assertTrue(all(row["state"] == "failed" for row in rows))
        self.assertTrue(all(
            "provider-attempt limit" in str(row["error"])
            for row in rows))



def real_agent_factory(model, gateway=None, load_md=True):
    """不触网构造真实 Agent.run 所需的最小状态。"""
    del load_md
    value = agent_module.Agent.__new__(agent_module.Agent)
    value.model = model
    value.gateway = gateway
    value.messages = []
    value.tokens_in = value.tokens_out = value.last_total = value.turns = 0
    value.cache_read = value.cache_write = 0
    value.cache_reported = False
    value.compact_failed = None
    value.context_summary = None
    value.context_invalid_reason = None
    value._compact_failed_key = None
    value._last_age_notice_key = None
    value._seen_ok_hi = 0
    value.compact_override = None
    value.ctx_limit = 100_000
    value.compact_at = 85_000
    value.ctx_known = True
    value.ctx_limit_source = "test"
    value.confirm = lambda *_args, **_kwargs: True
    value.started = time.time()
    value.log_usage = lambda *_args, **_kwargs: None
    return value


class SafeBoundaryTests(unittest.TestCase):
    def test_live_inbox_arrives_as_two_messages_before_next_request(self):
        calls = []
        lifecycle = []
        current_id = {"value": None}
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=real_agent_factory)

            def on_lifecycle(event):
                lifecycle.append(event)
                if event["kind"] == "agent_spawned":
                    current_id["value"] = event["payload"]["id"]

            def provider(model, messages, tools=None, gateway=None, **_kwargs):
                calls.append({
                    "model": model, "gateway": gateway,
                    "users": [
                        item["content"] for item in messages
                        if item.get("role") == "user"
                    ],
                    "tools": {
                        item["function"]["name"] for item in tools or []
                    },
                })
                if len(calls) == 1:
                    runtime.send(current_id["value"], "correction one")
                    runtime.send(current_id["value"], "correction two")
                    text = "draft"
                else:
                    text = "final report"
                yield {"t": "text", "v": text}
                yield {"t": "done", "reason": "stop", "usage": {}}

            with mock.patch.object(client, "stream_chat", provider):
                result = runtime.run_new(
                    "initial task", execution_context=execution(),
                    on_lifecycle=on_lifecycle)
            record = runtime.get(current_id["value"])

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["users"], ["initial task"])
        self.assertEqual(
            calls[1]["users"],
            ["initial task", "correction one", "correction two"])
        self.assertEqual(calls[0]["tools"], set(agents.CHILD_TOOLS))
        self.assertEqual(calls[1]["tools"], set(agents.CHILD_TOOLS))
        self.assertTrue(all(
            item["model"] == "model-one" and item["gateway"] == "boyue"
            for item in calls))
        self.assertIn("final report", result)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(
            [item["state"] for item in record["inbox"]],
            ["delivered", "delivered"])
        self.assertEqual(
            [item["payload"]["message"]["content"]
             for item in record["events"]
             if item["kind"] == "user_message"
             and item["payload"].get("source") == "agent_inbox"],
            ["correction one", "correction two"])


class AgentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        FakeAgent.instances = []
        BlockingAgent.instances = []
        BlockingAgent.started = threading.Event()

    def test_spawn_runs_initial_agent_async_with_explicit_route_and_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            workspace = agents.AgentWorkspace(
                runtime, poll_interval=0.005)
            context = tools.ExecutionContext.capture(
                session="parent-session", turn_id="workflow-turn",
                model="heterogeneous-model", gateway="deepinfer",
                permission_mode="workflow-read-only", hook_config={})

            record = workspace.spawn(
                "inspect independently", execution_context=context,
                name="Qwen scout", kind="workflow-scout")
            self.assertTrue(workspace.wait(record["id"], timeout=1.0))
            final = workspace.get(record["id"])
            events = workspace.drain()
            workspace.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["model"], "heterogeneous-model")
        self.assertEqual(final["gateway"], "deepinfer")
        self.assertEqual(final["kind"], "workflow-scout")
        self.assertEqual(final["permission_mode"], "read_only")
        self.assertEqual(
            [event["kind"] for event in events].count("agent_spawned"), 1)
        self.assertIn(
            "agent_result_received",
            [event["kind"] for event in events])

    def test_cancel_stops_workspace_owned_initial_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=BlockingAgent)
            workspace = agents.AgentWorkspace(
                runtime, poll_interval=0.005)
            record = workspace.spawn(
                "wait", execution_context=execution(),
                kind="workflow-scout")
            self.assertTrue(BlockingAgent.started.wait(1.0))

            workspace.cancel(record["id"])
            self.assertTrue(workspace.wait(record["id"], timeout=1.0))
            final = workspace.get(record["id"])
            workspace.close()

        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(final["error"], "用户中断")

    def test_completed_send_resumes_async_and_projects_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            runtime.run_new("first task", execution_context=execution())
            run_id = runtime.list()[0]["id"]
            workspace = agents.AgentWorkspace(
                runtime, poll_interval=0.005)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                item = workspace.send(run_id, "direct correction")
                self.assertTrue(workspace.wait(run_id, timeout=1.0))
            record = workspace.get(run_id)
            events = workspace.drain()

            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(item["state"], "queued")
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["run_count"], 2)
            self.assertEqual(record["inbox"][-1]["state"], "delivered")
            projected = workspace.list(parent_session_id="parent-session")[0]
            self.assertEqual(projected["id"], run_id)
            self.assertEqual(projected["pending"], 0)
            self.assertFalse(projected["resume_pending"])
            self.assertFalse(projected["workspace_active"])
            self.assertEqual(
                workspace.transcript(run_id)["id"],
                record["transcript_session_id"])
            self.assertFalse(workspace.active(run_id))
            self.assertTrue(workspace.close())

        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds[0], "agent_message_queued")
        self.assertIn("agent_state_changed", kinds)
        self.assertIn("agent_result_received", kinds)
        self.assertEqual(
            events[0]["payload"]["message_id"], item["id"])
        for event in events:
            self.assertEqual(event["payload"]["id"], run_id)
            self.assertIn("state", event["payload"])
            self.assertEqual(
                event["payload"]["parent_session_id"],
                "parent-session")

    def test_event_projection_queue_is_bounded_and_keeps_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = agents.AgentWorkspace(
                root=Path(tmp) / "runs", agent_factory=FakeAgent,
                event_limit=3)
            record = {
                "id": "a-1234567890", "state": "running",
                "parent_session_id": "parent-session",
            }
            for index in range(5):
                workspace._emit(
                    "agent_activity", record["id"],
                    {"index": index}, record=record)
            events = workspace.drain()
            workspace.close()

        self.assertEqual(
            [event["payload"]["index"] for event in events],
            [2, 3, 4])

    def test_tool_activity_projects_prepared_arguments_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = agents.AgentWorkspace(
                root=Path(tmp) / "runs", agent_factory=FakeAgent)
            prepared = tools.PreparedArguments(
                "read_file", '{"path":"/tmp/synthetic.txt"}')

            workspace._activity("a-1234567890", {
                "t": "tool_start",
                "name": "read_file",
                "args": prepared,
            })
            event = workspace.drain()[-1]
            workspace.close()

        self.assertEqual(event["kind"], "agent_activity")
        self.assertEqual(event["payload"]["tool"], "read_file")
        self.assertEqual(
            json.loads(event["payload"]["args"]),
            {"path": "/tmp/synthetic.txt"})
        self.assertFalse(event["payload"]["args_truncated"])

    def test_running_to_waiting_completion_boundary_is_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            record = runtime.spawn(
                "boundary task", execution_context=execution())
            runtime.store.claim(record["id"], owner_id="external-worker")
            workspace = agents.AgentWorkspace(
                runtime, poll_interval=0.005)
            workspace.send(record["id"], "late correction")
            boundary = runtime.store.finish(
                record["id"], "completed", result="draft")
            self.assertEqual(boundary["state"], "waiting")
            self.assertTrue(workspace.wait(record["id"], timeout=1.0))
            final = runtime.get(record["id"])
            workspace.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["run_count"], 2)
        self.assertEqual(final["inbox"][0]["state"], "delivered")
        users = [
            message["content"]
            for message in final["transcript"]["messages"]
            if message.get("role") == "user"
        ]
        self.assertEqual(users, ["boundary task", "late correction"])

    def test_multiple_sends_share_one_resume_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = GateResumeRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            runtime.run_new("first task", execution_context=execution())
            run_id = runtime.list()[0]["id"]
            workspace = agents.AgentWorkspace(
                runtime, poll_interval=0.005)
            workspace.send(run_id, "correction one")
            self.assertTrue(runtime.resume_started.wait(1.0))
            self.assertTrue(workspace.active(run_id))
            workspace.send(run_id, "correction two")
            runtime.resume_gate.set()
            self.assertTrue(workspace.wait(run_id, timeout=1.0))
            record = runtime.get(run_id)
            workspace.close()

        self.assertEqual(runtime.resume_calls, 1)
        self.assertEqual(
            [item["state"] for item in record["inbox"]],
            ["delivered", "delivered"])
        users = [
            message["content"]
            for message in record["transcript"]["messages"]
            if message.get("role") == "user"
        ]
        self.assertEqual(
            users, ["first task", "correction one", "correction two"])

    def test_new_send_at_workspace_resume_finish_boundary_runs_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            runtime.run_new("first task", execution_context=execution())
            run_id = runtime.list()[0]["id"]
            original_finish = runtime.store.finish
            finish_entered = threading.Event()
            release_finish = threading.Event()

            def gated_finish(*args, **kwargs):
                finish_entered.set()
                if not release_finish.wait(1.0):
                    raise RuntimeError("test finish gate timeout")
                return original_finish(*args, **kwargs)

            runtime.store.finish = gated_finish
            workspace = agents.AgentWorkspace(
                runtime, poll_interval=0.005)
            workspace.send(run_id, "correction one")
            self.assertTrue(finish_entered.wait(1.0))
            workspace.send(run_id, "correction two at finish")
            release_finish.set()
            self.assertTrue(workspace.wait(run_id, timeout=1.0))
            record = runtime.get(run_id)
            events = workspace.drain()
            workspace.close()

        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["run_count"], 3)
        self.assertEqual(
            [item["state"] for item in record["inbox"]],
            ["delivered", "delivered"])
        self.assertNotIn(
            "agent_workspace_error",
            [event["kind"] for event in events])

    def test_close_cancels_owned_resume_but_not_external_running_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "owned", agent_factory=FakeAgent)
            runtime.run_new("first task", execution_context=execution())
            run_id = runtime.list()[0]["id"]
            runtime.agent_factory = BlockingAgent
            workspace = agents.AgentWorkspace(
                runtime, poll_interval=0.005)
            workspace.send(run_id, "will be cancelled")
            self.assertTrue(BlockingAgent.started.wait(1.0))
            self.assertTrue(workspace.close(timeout=1.0))
            owned = runtime.get(run_id)
            with self.assertRaisesRegex(
                    agents.AgentRuntimeError, "已关闭"):
                workspace.send(run_id, "too late")

            external_runtime = agents.AgentRuntime(
                Path(tmp) / "external", agent_factory=FakeAgent)
            external = external_runtime.spawn(
                "external task", execution_context=execution())
            external_runtime.store.claim(
                external["id"], owner_id="external-worker")
            external_workspace = agents.AgentWorkspace(
                external_runtime, poll_interval=0.005)
            external_workspace.send(external["id"], "keep queued")
            self.assertTrue(external_workspace.close(timeout=1.0))
            still_running = external_runtime.get(external["id"])

        self.assertEqual(owned["state"], "cancelled")
        self.assertEqual(still_running["state"], "running")
        self.assertEqual(still_running["inbox"][0]["state"], "queued")


if __name__ == "__main__":
    unittest.main()
