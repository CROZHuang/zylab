"""Cross-session read-only snapshot consultation regression tests."""
import contextlib
import copy
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import zylab as CLI
from core import agents, consults, context as context_projection, controller, settings, tools


def caller(session="chat-a"):
    return tools.ExecutionContext.capture(
        session=session,
        turn_id="turn-a",
        model="main-model",
        gateway="deepinfer",
        permission_mode="default",
        hook_config={"permissions": {"consult_session": "ask"}},
    )


def source_record():
    return {
        "id": "chat-b",
        "title": "Parser investigation",
        "status": "active",
        "model": "target-model",
        "gateway": "boyue",
        "cwd": "/tmp/workspace/project-b",
        "updated": "2026-08-26T08:00:00.000+00:00",
        "messages": [
            {"role": "system", "content": "source system"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ],
        "context": {"summary": "saved projection"},
        "tokens_in": 900,
        "tokens_out": 120,
        "last_context_tokens": 700,
    }


@contextlib.contextmanager
def captured_source(record=None, *, live=True):
    value = record or source_record()
    with (
            mock.patch.object(
                consults.store, "resolve_session_id", return_value=value["id"]),
            mock.patch.object(
                consults.store, "load_session", return_value=value) as load,
            mock.patch.object(
                consults.store, "session_lease",
                return_value={"live": live, "pid": 123} if live else None)):
        yield load


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
        self.loaded_context = copy.deepcopy(value)

    def context_snapshot(self):
        return copy.deepcopy(self.loaded_context)

    def run(self, user_input, *, allowed_tools=None, event_sink=None,
            inbox_source=None, cancel=None, **_kwargs):
        self.inputs.append(user_input)
        self.allowed_tools_seen = frozenset(
            allowed_tools if allowed_tools is not None else {"ALL"})
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
                    "message": message,
                    "source": "agent_inbox",
                    "inbox_id": item["id"],
                },
            })
        if events:
            event_sink(events)
        if cancel is not None and cancel.is_set():
            yield {"t": "interrupted"}
            return
        latest = next(
            message["content"] for message in reversed(self.messages)
            if message.get("role") == "user")
        answer = f"consult answer: {latest}"
        message = {"role": "assistant", "content": answer}
        self.messages.append(message)
        self.tokens_in += 17
        self.tokens_out += 5
        self.last_total = 22
        event_sink([{
            "kind": "assistant_message", "payload": {"message": message},
        }])
        yield {"t": "text", "v": answer}
        yield {"t": "end", "reason": "stop"}


class BrokenAgent(FakeAgent):
    def run(self, *_args, **_kwargs):
        raise RuntimeError("consult exploded")
        yield  # pragma: no cover


class ConsultRuntimeTests(unittest.TestCase):
    def setUp(self):
        FakeAgent.instances = []
        BrokenAgent.instances = []

    def test_snapshot_copy_pins_target_route_has_cs_identity_and_no_tools(self):
        source = source_record()
        before = json.dumps(source, ensure_ascii=False, sort_keys=True)
        lifecycle = []
        with tempfile.TemporaryDirectory() as tmp, captured_source(source):
            root = Path(tmp) / "runs"
            runtime = agents.AgentRuntime(root, agent_factory=FakeAgent)
            with self.assertRaisesRegex(
                    agents.AgentRuntimeError, "seed 必须是 object"):
                runtime.spawn(
                    "falsey seed", execution_context=caller(),
                    kind=consults.KIND, transcript=[])
            with self.assertRaisesRegex(
                    agents.AgentRuntimeError, "snapshot metadata 必须是 object"):
                runtime.spawn(
                    "falsey metadata", execution_context=caller(),
                    kind=consults.KIND, source_snapshot=[])
            self.assertFalse(root.exists())
            result = consults.run_new(
                runtime, "chat-b", "What did we decide?",
                execution_context=caller(),
                parent_tool_call_id="call-consult",
                on_lifecycle=lifecycle.append)
            record = runtime.get(runtime.list()[0]["id"])
            state_path = root / record["id"] / "state.json"
            state_mode = stat.S_IMODE(state_path.stat().st_mode)

        self.assertRegex(record["id"], r"^cs-[0-9a-f]{10}$")
        self.assertEqual(
            record["transcript_session_id"], "c-" + record["id"])
        self.assertEqual(record["parent_session_id"], "chat-a")
        self.assertEqual(record["parent_tool_call_id"], "call-consult")
        self.assertEqual(record["kind"], consults.KIND)
        self.assertEqual(record["model"], "target-model")
        self.assertEqual(record["gateway"], "boyue")
        self.assertEqual(record["cwd"], "/tmp/workspace/project-b")
        self.assertTrue(record["source_snapshot"]["source_live"])
        self.assertEqual(
            record["source_snapshot"]["source_lease_state"], "live")
        self.assertEqual(record["source_snapshot"]["message_count"], 3)
        self.assertEqual(len(record["source_snapshot"]["snapshot_sha256"]), 64)
        self.assertIn("[consult cs-", result)
        self.assertIn("consult answer: What did we decide?", result)
        self.assertEqual(
            FakeAgent.instances[0].allowed_tools_seen,
            agents.CONSULT_TOOLS)
        self.assertEqual(FakeAgent.instances[0].loaded_context, source["context"])
        transcript = record["transcript"]["messages"]
        self.assertTrue(any(
            message.get("role") == "system"
            and "cross-session consultation" in str(message.get("content"))
            for message in transcript))
        self.assertEqual(
            [message["content"] for message in transcript
             if message.get("role") == "user"],
            ["old question", "What did we decide?"])
        self.assertIn(
            "consult_snapshot_captured",
            [event["kind"] for event in record["events"]])
        self.assertEqual(state_mode, 0o600)
        self.assertEqual(
            json.dumps(source, ensure_ascii=False, sort_keys=True), before)
        self.assertEqual(
            [event["kind"] for event in lifecycle],
            ["agent_spawned", "agent_state_changed",
             "agent_state_changed", "agent_result_received"])

    def test_followup_to_live_owner_is_reported_as_queued(self):
        with tempfile.TemporaryDirectory() as tmp, captured_source():
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            consults.run_new(
                runtime, "chat-b", "first",
                execution_context=caller())
            consult_id = runtime.list()[0]["id"]
            runtime.store.claim(
                consult_id, owner_id="external-worker", resume=True)
            result = consults.follow_up(
                runtime, consult_id, "while running",
                execution_context=caller())
            record = runtime.get(consult_id)

        pending = [
            item for item in record["inbox"]
            if item.get("state") == "queued"]
        self.assertIn("follow-up", result)
        self.assertIn("queued", result)
        self.assertEqual(record["state"], "running")
        self.assertEqual(
            [item["text"] for item in pending], ["while running"])

    def test_guard_keeps_a_valid_source_summary_valid(self):
        source = source_record()
        plan = context_projection.plan_compaction_to(
            source["messages"], len(source["messages"]))
        summary = context_projection.make_summary(
            "saved summary", plan,
            model=source["model"], gateway=source["gateway"])
        source["context"] = {"version": 1, "summary": summary}

        with captured_source(source):
            snapshot = consults.capture(
                "chat-b", current_session_id="chat-a")
        seeded = consults._seed_transcript(snapshot)
        shifted = seeded["context"]["summary"]
        valid, reason = context_projection.validate_summary(
            seeded["messages"], shifted)

        self.assertTrue(valid, reason)
        self.assertEqual(
            shifted["covered_from"], summary["covered_from"] + 1)
        self.assertEqual(
            shifted["covered_to"], summary["covered_to"] + 1)
        self.assertEqual(
            shifted["covered_sha256"], summary["covered_sha256"])

    def test_consult_transcript_id_is_namespaced_from_regular_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent,
                id_factory=lambda prefix: f"{prefix}-deadbeef00")
            regular = runtime.spawn(
                "regular", execution_context=caller(), kind="subagent")
            consult = runtime.spawn(
                "consult", execution_context=caller(),
                kind=consults.KIND,
                transcript={"messages": [
                    {"role": "system", "content": "snapshot"}]})

        self.assertEqual(
            regular["transcript_session_id"], "c-deadbeef00")
        self.assertEqual(
            consult["transcript_session_id"], "c-cs-deadbeef00")
        self.assertNotEqual(
            regular["transcript_session_id"],
            consult["transcript_session_id"])

    def test_followup_reuses_private_copy_and_rejects_cross_parent(self):
        with tempfile.TemporaryDirectory() as tmp, captured_source() as load:
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            consults.run_new(
                runtime, "chat-b", "first",
                execution_context=caller())
            consult_id = runtime.list()[0]["id"]
            result = consults.follow_up(
                runtime, consult_id, "second",
                execution_context=caller())
            before_inbox = copy.deepcopy(runtime.get(consult_id)["inbox"])
            with self.assertRaisesRegex(
                    consults.ConsultError, "属于 session chat-a"):
                consults.follow_up(
                    runtime, consult_id, "must not cross",
                    execution_context=caller("other-chat"))
            record = runtime.get(consult_id)

        self.assertIn("consult answer: second", result)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(
            [message["content"] for message in record["transcript"]["messages"]
             if message.get("role") == "user"],
            ["old question", "first", "second"])
        self.assertEqual(record["inbox"], before_inbox)
        self.assertEqual(record["run_count"], 2)
        self.assertEqual(
            [agent.allowed_tools_seen for agent in FakeAgent.instances],
            [agents.CONSULT_TOOLS, agents.CONSULT_TOOLS])

    def test_parallel_consults_are_independent_and_failure_is_durable(self):
        source = source_record()
        before = copy.deepcopy(source)
        with tempfile.TemporaryDirectory() as tmp, captured_source(source):
            root = Path(tmp) / "runs"

            def run_one(index):
                runtime = agents.AgentRuntime(root, agent_factory=FakeAgent)
                return consults.run_new(
                    runtime, "chat-b", f"parallel {index}",
                    execution_context=caller(f"chat-a-{index}"))

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run_one, (1, 2)))
            rows = agents.AgentRuntime(root).list()

        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertEqual(
            {row["parent_session_id"] for row in rows},
            {"chat-a-1", "chat-a-2"})
        self.assertTrue(all("consult answer" in result for result in results))
        self.assertEqual(source, before)

        with tempfile.TemporaryDirectory() as tmp, captured_source(source):
            runtime = agents.AgentRuntime(
                Path(tmp) / "failed", agent_factory=BrokenAgent)
            text = consults.run_new(
                runtime, "chat-b", "fail safely",
                execution_context=caller())
            failed = runtime.get(runtime.list()[0]["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertIn("RuntimeError: consult exploded", failed["error"])
        self.assertIn("咨询失败", text)
        self.assertEqual(source, before)

    def test_title_resolution_is_exact_unique_and_current_is_rejected(self):
        rows = [
            {"id": "one", "title": "Exact title"},
            {"id": "two", "title": "Other"},
        ]
        with (
                mock.patch.object(
                    consults.store, "resolve_session_id",
                    side_effect=FileNotFoundError("missing")),
                mock.patch.object(
                    consults.store, "list_session_summaries",
                    return_value=rows)):
            self.assertEqual(
                consults.resolve_target("exact TITLE"), "one")
        with mock.patch.object(
                consults.store, "resolve_session_id", return_value="chat-a"):
            with self.assertRaisesRegex(
                    consults.ConsultError, "当前前台会话"):
                consults.resolve_target(
                    "chat-a", current_session_id="chat-a")


    def test_lease_failure_is_unknown_and_invalid_seed_leaves_no_run(self):
        source = source_record()
        with (
                mock.patch.object(
                    consults.store, "resolve_session_id",
                    return_value="chat-b"),
                mock.patch.object(
                    consults.store, "load_session", return_value=source),
                mock.patch.object(
                    consults.store, "session_lease",
                    side_effect=OSError("lease unreadable"))):
            snapshot = consults.capture(
                "chat-b", current_session_id="chat-a")

        identity = snapshot["identity"]
        self.assertEqual(identity["source_lease_state"], "unknown")
        self.assertFalse(identity["source_live"])
        guard = next(
            message for message in consults._seed_transcript(snapshot)["messages"]
            if message.get("role") == "system"
            and "cross-session consultation" in message.get("content", ""))
        self.assertIn("freshness is unknown", guard["content"])

        malformed = source_record()
        malformed["messages"] = [
            {"role": "system", "content": "ok"}, "bad-item"]
        with captured_source(malformed):
            with self.assertRaisesRegex(
                    consults.ConsultError, "messages 含非 object"):
                consults.capture(
                    "chat-b", current_session_id="chat-a")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runs"
            runtime = agents.AgentRuntime(root, agent_factory=FakeAgent)
            with self.assertRaisesRegex(
                    agents.AgentRuntimeError, "messages 必须是 array"):
                runtime.spawn(
                    "bad seed", execution_context=caller(),
                    kind=consults.KIND,
                    transcript={"messages": "not-an-array"})
            self.assertFalse(root.exists())


class ToolAndCLITests(unittest.TestCase):
    def test_tool_forwards_lifecycle_and_permission_defaults_to_ask(self):
        class Task:
            key = "call-9"

            def __init__(self):
                self.cancel_event = threading.Event()
                self.events = []
                self.notes = []

            def runtime_event(self, event):
                self.events.append(event)

            def note(self, value):
                self.notes.append(value)

        task = Task()
        with tempfile.TemporaryDirectory() as tmp, captured_source():
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            with mock.patch.object(
                    agents, "AgentRuntime", return_value=runtime):
                result = tools.t_consult_session(
                    target_session="chat-b", question="tool question",
                    _task=task, _execution_context=caller())
            record = runtime.get(runtime.list()[0]["id"])

        self.assertIn(f"[consult {record['id']}", result)
        self.assertEqual(record["parent_tool_call_id"], "call-9")
        self.assertEqual(
            [event["kind"] for event in task.events],
            ["agent_spawned", "agent_state_changed",
             "agent_state_changed", "agent_result_received"])
        self.assertTrue(any("captured chat-b@" in note for note in task.notes))
        self.assertEqual(
            settings.DEFAULTS["permissions"]["consult_session"], "ask")
        self.assertNotIn("consult_session", tools.SAFE)
        self.assertIn("consult_session", tools.READ_ONLY)

    def test_slash_command_keeps_consults_out_of_agents_and_renders_result(self):
        class MainAgent:
            model = "main-model"
            gateway = "deepinfer"
            session_id = "chat-a"
            compact_at = 100_000
            ctx_known = True
            tokens_in = 0
            tokens_out = 0
            last_total = 0

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, captured_source():
            runtime = agents.AgentRuntime(
                Path(tmp) / "runs", agent_factory=FakeAgent)
            workspace = agents.AgentWorkspace(
                runtime, poll_interval=0.005)
            sess = CLI.Session(MainAgent())
            sess.agent_workspace.close()
            sess.agent_workspace = workspace
            sess.controller = controller.SessionController(
                sess.ag.session_id, controller.MemoryJournal())
            try:
                with contextlib.redirect_stdout(output):
                    CLI.cmd_consult(sess, "chat-b summarize")
                self.assertTrue(workspace.wait(timeout=2.0))
                notices = sess.drain_agent_events()
                consult_rows = sess.list_consults()
                agent_rows = sess.list_agents()
                preview = CLI.format_agent_preview(
                    sess.consult_record(consult_rows[0]["id"]))
            finally:
                sess.close()

        self.assertIn("consult", CLI.REGISTRY)
        self.assertIn("snapshot", output.getvalue())
        self.assertEqual(len(consult_rows), 1)
        self.assertEqual(agent_rows, [])
        self.assertIn("── consult", preview)
        self.assertIn("source chat-b", preview)
        spawn_notice = CLI._agent_spawn_notice(consult_rows[0])
        self.assertIn("↳ consult", spawn_notice)
        self.assertIn("/consult status", spawn_notice)
        self.assertNotIn("Ctrl+T", spawn_notice)
        self.assertTrue(any(
            "[consult" in notice and "consult answer: summarize" in notice
            for notice in notices))


if __name__ == "__main__":
    unittest.main(verbosity=2)
