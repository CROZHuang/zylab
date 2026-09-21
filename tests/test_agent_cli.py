"""M3c-3 Session 路由、agent panel 与真实 PTY 集成回归。"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zylab as CLI
from core import agents, client, controller, tools, tui
from tests.pty_harness import PTYSend, run_pty_child

# ignore_cleanup_errors：Windows 上打开着的文件删不掉（这是平台语义，
# 不是缺陷）。这些用例把 sqlite 库开在临时目录里、不显式关闭，
# POSIX 上照样能删，Windows 上会在 tearDown 抛 WinError 32，
# 把一个通过的断言变成 ERROR。


def execution(session="parent-session"):
    return tools.ExecutionContext.capture(
        session=session, turn_id="parent-turn",
        model="child-model", gateway="boyue",
        permission_mode="default", hook_config={})


class MainAgent:
    model = "main-model"
    gateway = "deepinfer"
    session_id = "parent-session"
    last_total = 4_000
    compact_at = 70_000
    ctx_known = True
    tokens_in = 0
    tokens_out = 0
    turns = 0
    started = 0


class TransportApprovalScopeTests(unittest.TestCase):
    @staticmethod
    def make_session(root):
        sess = object.__new__(CLI.Session)
        sess.ag = type("TransportAgent", (), {
            "session_id": "session-a",
            "model": "model-a",
            "gateway": "deepinfer",
        })()
        sess._session_cwd = str(root)
        sess._pending_route = None
        sess.controller = None
        sess._allow_insecure_http = False
        sess._insecure_transport_grants = set()
        sess._insecure_transport_lock = threading.Lock()
        sess._ui_owner_thread_id = threading.get_ident()
        sess._transport_policy_active = True
        return sess

    def test_session_installs_persistent_exact_endpoint_allowlist(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sess = self.make_session(Path(d))
            sess.cfg = json.loads(json.dumps(CLI.CFG.DEFAULTS))
            sess.cfg["transport"]["allowed_insecure_endpoints"] = [
                "HTTP://TRUSTED.INVALID/v1/",
            ]
            with mock.patch.object(
                    client, "configure_transport_policy") as configure:
                sess.install_transport_policy()

        self.assertEqual(
            configure.call_args.kwargs["allowed_insecure_endpoints"],
            ("http://trusted.invalid/v1",))
        self.assertIs(
            configure.call_args.kwargs["authorizer"],
            sess._transport_authorizer)

    def test_subagent_transport_is_preflighted_on_owner_thread(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sess = self.make_session(Path(d))
            sess.ag.gateway = "boyue"
            seen = {}

            def require(route, **kwargs):
                seen["thread"] = threading.get_ident()
                seen["route"] = route
                seen["kwargs"] = kwargs
                return {}

            with mock.patch.object(
                    client, "require_secure_transport",
                    side_effect=require) as preflight:
                decision = sess._authorize_unsandboxed(
                    "subagent", {}, {
                        "allowed": True,
                        "decision": "allow",
                        "source": "config",
                    })

        self.assertTrue(decision["allowed"])
        self.assertEqual(preflight.call_count, 1)
        self.assertEqual(seen["thread"], sess._ui_owner_thread_id)
        self.assertEqual(seen["route"].name, "boyue")
        self.assertEqual(
            seen["kwargs"]["trace_context"]["session_id"],
            "session-a")
        self.assertEqual(seen["kwargs"]["purpose"], "subagent_preflight")

    def test_background_transport_authorizer_fails_closed_without_ui(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sess = self.make_session(Path(d))
            sess._confirm_insecure_transport = mock.Mock(return_value=True)
            route = client.GatewayRoute(
                "boyue", "http://example.invalid/v1",
                ("BOYUE_API_KEY",))
            request = client.transport_request(route, trace_context={
                "session_id": "session-a", "cwd": d})
            observed = []
            worker = threading.Thread(
                target=lambda: observed.append(
                    sess.authorize_insecure_transport(request)))
            worker.start()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [False])
        sess._confirm_insecure_transport.assert_not_called()

    def test_workflow_preflight_deduplicates_shared_boyue_endpoint(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sess = self.make_session(Path(d))
            sess.workflow_manager = mock.Mock()
            sess.workflow_manager.resolve_lineup.return_value = [
                {"seat": "Qwen", "gateway": "boyue", "id": "qwen"},
                {"seat": "GLM", "gateway": "boyue", "id": "glm"},
            ]
            payload = {
                "agents": [
                    {"key": "a", "seat": "Qwen", "task": "inspect"},
                    {"key": "b", "seat": "GLM", "task": "verify"},
                ],
            }
            with mock.patch.object(
                    client, "require_secure_transport",
                    return_value={}) as preflight:
                decision = sess._authorize_provider_transport(
                    "workflow", payload, {
                        "allowed": True,
                        "decision": "once",
                        "source": "user",
                    })

        self.assertTrue(decision["allowed"])
        self.assertEqual(preflight.call_count, 1)
        self.assertEqual(preflight.call_args.args[0].name, "boyue")

    def test_workflow_control_preflights_add_but_not_status(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sess = self.make_session(Path(d))
            sess.workflow_manager = mock.Mock()
            sess.workflow_manager.resolve_lineup.return_value = [
                {"seat": "Qwen", "gateway": "boyue", "id": "qwen"},
                {"seat": "GLM", "gateway": "boyue", "id": "glm"},
            ]
            allowed = {
                "allowed": True,
                "decision": "allow",
                "source": "config",
            }
            with mock.patch.object(
                    client, "require_secure_transport",
                    return_value={}) as preflight:
                status = sess._authorize_provider_transport(
                    "workflow_control", {"action": "status"}, allowed)
                added = sess._authorize_provider_transport(
                    "workflow_control", {
                        "action": "add",
                        "nodes": [
                            {"key": "a", "seat": "Qwen",
                             "task": "inspect"},
                            {"key": "b", "seat": "GLM",
                             "task": "verify"},
                        ],
                    }, allowed)

        self.assertTrue(status["allowed"])
        self.assertTrue(added["allowed"])
        self.assertEqual(preflight.call_count, 1)
        self.assertEqual(
            sess.workflow_manager.resolve_lineup.call_count, 1)

    def test_parent_http_grant_covers_child_provider_attempt(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sess = self.make_session(Path(d))
            sess._confirm_insecure_transport = mock.Mock(return_value=True)
            route = client.GatewayRoute(
                "boyue", "http://example.invalid/v1",
                ("BOYUE_API_KEY",))
            previous = client.configure_transport_policy(
                authorizer=sess.authorize_insecure_transport)
            errors = []
            try:
                with mock.patch.object(
                        CLI.sys.stdin, "isatty", return_value=True):
                    client.require_secure_transport(route, trace_context={
                        "session_id": "session-a", "cwd": d})

                def child_attempt():
                    try:
                        client.require_secure_transport(
                            route, trace_context={
                                "session_id": "child-session",
                                "transport_session_id": "session-a",
                                "cwd": d,
                            })
                    except Exception as exc:
                        errors.append(exc)

                worker = threading.Thread(target=child_attempt)
                worker.start()
                worker.join(timeout=2)
            finally:
                client.restore_transport_policy(previous)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sess._confirm_insecure_transport.call_count, 1)

    def test_denied_provider_preflight_prevents_background_spawn(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sess = self.make_session(Path(d))
            sess.ag.gateway = "boyue"
            denied = client.APIError(
                "synthetic denial", kind="insecure_transport",
                gateway="boyue", model="model-a", retryable=False)
            with (
                    mock.patch.object(
                        client, "require_secure_transport",
                        side_effect=denied),
                    mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                decision = sess._authorize_provider_transport(
                    "subagent", {}, {
                        "allowed": True,
                        "decision": "allow",
                        "source": "config",
                    })

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["decision"],
                         "provider_transport_denied")
        self.assertEqual(decision["source"], "transport_guard")

    def test_grant_is_invalidated_by_base_repo_or_session_change(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            root = Path(d, "repo-a")
            other = Path(d, "repo-b")
            root.mkdir()
            other.mkdir()
            sess = self.make_session(root)
            prompt = mock.Mock(side_effect=[True, False, False, False])
            sess._confirm_insecure_transport = prompt
            route = client.GatewayRoute(
                "boyue", "http://example.invalid/v1", ("BOYUE_API_KEY",))
            request = client.transport_request(route, trace_context={
                "session_id": "session-a", "cwd": str(root)})
            with mock.patch.object(CLI.sys.stdin, "isatty", return_value=True):
                self.assertTrue(sess.authorize_insecure_transport(request))
                self.assertTrue(sess.authorize_insecure_transport(request))
                self.assertFalse(sess.authorize_insecure_transport({
                    **request, "base": "http://other.invalid/v1"}))
                self.assertFalse(sess.authorize_insecure_transport({
                    **request, "repo": str(other)}))
                self.assertFalse(sess.authorize_insecure_transport({
                    **request, "session": "session-b"}))
        self.assertEqual(prompt.call_count, 4)

    def test_declined_http_route_is_not_staged_or_applied(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            sess = self.make_session(Path(d))
            sess._confirm_insecure_transport = mock.Mock(return_value=False)
            old_gateway = client.GATEWAY
            previous = client.configure_transport_policy(
                authorizer=sess.authorize_insecure_transport)
            try:
                client.set_gateway("deepinfer")
                # 这条测的是**明文 HTTP 路由被拒**，所以必须自带一个 http://
                # endpoint：内置 profile 不再携带地址（地址是部署事实），不声明
                # 的话先撞上 base_not_configured，抛的就不是 ValueError 了。
                with mock.patch.dict(
                        client.GATEWAYS["boyue"],
                        {"base": "http://example.invalid/v1"}), \
                        mock.patch.object(
                            CLI.sys.stdin, "isatty", return_value=True):
                    with self.assertRaises(ValueError):
                        sess.request_route_change("model-a", "boyue")
                self.assertIsNone(sess._pending_route)
                self.assertEqual(sess.ag.gateway, "deepinfer")
            finally:
                client.set_gateway(old_gateway)
                client.restore_transport_policy(previous)

    def test_model_command_renders_transport_decline_without_raising(self):
        sess = mock.Mock()
        sess.route_selection.return_value = ("old-model", "boyue")
        sess.request_route_change.side_effect = ValueError(
            "synthetic HTTP transport denial")
        with (
                mock.patch.object(CLI.M, "where", return_value=["boyue"]),
                mock.patch.object(CLI.M, "get", return_value={
                    "supports_tools": True, "status": "ok"}),
                mock.patch.object(sys, "stdout", io.StringIO()) as output):
            CLI.cmd_model(sess, "new-model")
        self.assertIn("synthetic HTTP transport denial", output.getvalue())
        sess.request_route_change.assert_called_once_with(
            "new-model", "boyue")


class SessionAgentRoutingTests(unittest.TestCase):
    def test_corrupt_memory_is_visible_but_does_not_block_session_start(self):
        with mock.patch.object(
                CLI.MEMORY.MemoryStore, "render_index",
                side_effect=CLI.MEMORY.MemoryError("corrupt authority")):
            sess = CLI.Session(MainAgent())
        try:
            self.assertIn("corrupt authority", sess._memory_error)
            self.assertIn("memory error", CLI.status_line(sess))
        finally:
            sess.close()

    def make_session(self, root, record):
        runtime = agents.AgentRuntime(root)
        workspace = agents.AgentWorkspace(
            runtime, poll_interval=0.005)
        sess = CLI.Session(MainAgent())
        sess.agent_workspace.close()
        sess.agent_workspace = workspace
        sess.pump = tui.InputPump(
            stream=io.StringIO(), history=["main history"])
        sess.renderer = tui.TerminalRenderer(io.StringIO())
        journal = controller.MemoryJournal()
        sess.controller = controller.SessionController(
            sess.ag.session_id, journal)
        # 保持一个 active main turn，再排一条 main queue，验证 attach 后不串线。
        sess.controller.submit(
            "active main", controller.QueueMode.NEXT_TURN)
        sess.controller.submit(
            "main queued", controller.QueueMode.NEXT_TURN)
        self.addCleanup(workspace.close)
        return sess, runtime, journal

    def test_attach_routes_to_child_and_restores_main_queue_and_drafts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "runs"
            runtime = agents.AgentRuntime(root)
            child = runtime.spawn(
                "inspect", execution_context=execution())
            runtime.store.claim(child["id"], owner_id="external")
            sess, runtime, journal = self.make_session(root, child)
            sess.pump.replace_buffer(
                "main draft", active=True, publish=False)

            attached = sess.attach_agent(child["id"])
            attached_snapshot = sess.pump.snapshot()
            sess.pump.replace_buffer(
                "child draft", active=True, publish=False)
            sent = sess.send_agent(child["id"], "direct correction")
            child_snapshot = sess.refresh_composer(
                busy=True, prompt=sess.composer_prompt(busy=True),
                publish=False)
            notices = sess.drain_agent_events()
            detached = sess.detach_agent(publish=False)
            main_snapshot = sess.pump.snapshot()
            sess.attach_agent(child["id"])
            restored_child = sess.pump.snapshot()

            self.assertEqual(attached["id"], child["id"])
            self.assertEqual(attached_snapshot.target, child["id"])
            self.assertIn(f"agent {child['id'][:10]}›",
                          attached_snapshot.prompt)
            self.assertNotIn("main queued", attached_snapshot.queued)
            self.assertTrue(any(
                "direct correction" in line
                for line in child_snapshot.queued))
            self.assertIn("target", CLI.status_line(sess))
            self.assertEqual(detached, child["id"])
            self.assertIsNone(main_snapshot.target)
            self.assertEqual(main_snapshot.text, "main draft")
            self.assertTrue(any(
                "main queued" in line for line in main_snapshot.queued))
            self.assertEqual(restored_child.text, "child draft")
            self.assertEqual(
                runtime.get(child["id"])["inbox"][-1]["id"], sent["id"])
            self.assertEqual(notices, [])
            self.assertTrue(sess._agent_projection_changed)
            self.assertIn(
                "agent_message_queued",
                [event["kind"] for event in journal.events])

    def test_cross_parent_attach_and_send_are_rejected(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "runs"
            runtime = agents.AgentRuntime(root)
            foreign = runtime.spawn(
                "foreign", execution_context=execution("other-parent"))
            sess, _, _ = self.make_session(root, foreign)

            with self.assertRaisesRegex(
                    agents.AgentRuntimeError, "属于 session other-parent"):
                sess.attach_agent(foreign["id"])
            with self.assertRaisesRegex(
                    agents.AgentRuntimeError, "属于 session other-parent"):
                sess.send_agent(foreign["id"], "must not cross")

            self.assertEqual(
                runtime.get(foreign["id"])["inbox"], [])

    def test_lightweight_agent_summary_never_hides_queued_child_message(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "runs"
            runtime = agents.AgentRuntime(root)
            child = runtime.spawn(
                "inspect", execution_context=execution())
            runtime.store.queue_message(child["id"], "queued correction")
            sess, _, _ = self.make_session(root, child)
            sess.attached_agent_id = child["id"]

            # Dynamic guidance/listing uses the bounded summary projection;
            # the composer must still fetch the full record before rendering
            # an attached child inbox.
            sess.list_agents()
            self.assertTrue(any(
                "queued correction" in line
                for line in sess._composer_queue()))

    def test_preview_is_bounded_and_panel_row_shows_route_and_pending(self):
        huge = "x" * 10_000
        record = {
            "id": "a-1234567890",
            "state": "running",
            "name": "inspect repository",
            "model": "child-model",
            "gateway": "boyue",
            "updated_at": "2026-08-25T00:00:00+00:00",
            "inbox": [{"state": "queued", "text": "follow up"}],
            "pending": 1,
            "workspace_active": True,
            "transcript": {
                "messages": [
                    {"role": "user", "content": huge},
                    {"role": "assistant", "content": "answer"},
                ],
            },
        }

        preview = CLI.format_agent_preview(record)
        row = CLI._agent_panel_row(record)

        self.assertLess(len(preview), len(huge))
        self.assertIn("省略", preview)
        self.assertIn("child-model@boyue", row)
        self.assertIn("1 pending", row)
        self.assertIn("active", row)


    def test_normal_chat_children_appear_as_clickable_composer_rows(self):
        class View:
            _workflow_current = None
            controller = type(
                "Controller", (), {"current_turn_id": "turn-live"})()

            @staticmethod
            def list_agents(limit=100):
                del limit
                return [
                    {
                        "id": "a-live000001", "kind": "subagent",
                        "state": "running", "name": "Inspect parser",
                        "model": "main-model", "gateway": "boyue",
                        "parent_turn_id": "turn-live",
                        "parent_tool_call_id": "call-batch",
                    },
                    {
                        "id": "a-live000002", "kind": "subagent",
                        "state": "completed", "name": "Audit tests",
                        "model": "main-model", "gateway": "boyue",
                        "parent_turn_id": "turn-live",
                        "parent_tool_call_id": "call-batch",
                    },
                    {
                        "id": "a-stale00001", "kind": "subagent",
                        "state": "completed", "name": "Old task",
                        "parent_turn_id": "turn-old",
                        "parent_tool_call_id": "call-old",
                    },
                ]

        items = CLI._composer_dock_items(View())

        self.assertEqual(len(items), 3)
        self.assertIn("main", items[0].text)
        self.assertEqual(
            [(item.event, item.value) for item in items[1:]],
            [("agent_attach", "a-live000001"),
             ("agent_attach", "a-live000002")])
        self.assertNotIn("Old task", " ".join(item.text for item in items))

    def test_preview_hides_reasoning_without_mutating_raw_transcript(self):
        raw = (
            "<think>private preview reasoning</think>\n"
            "VISIBLE_PREVIEW_ANSWER")
        record = {
            "id": "a-preview-reasoning",
            "state": "completed",
            "name": "reasoning projection",
            "model": "child-model",
            "gateway": "deepinfer",
            "transcript": {
                "messages": [
                    {"role": "user", "content": "inspect"},
                    {"role": "assistant", "content": raw},
                ],
            },
        }

        preview = CLI.format_agent_preview(record)

        self.assertNotIn("private preview reasoning", preview)
        self.assertIn(agents.REASONING_HIDDEN_NOTICE, preview)
        self.assertIn("VISIBLE_PREVIEW_ANSWER", preview)
        self.assertEqual(
            record["transcript"]["messages"][1]["content"], raw)


class AgentPTYTests(unittest.TestCase):
    def run_child(self, body, sends, timeout=7.0):
        _, result = run_pty_child(
            body, sends,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=timeout,
        )
        return result

    def test_real_space_returns_peek_action(self):
        result = self.run_child(
            r"""
import json
import time
from core import tui

picked = None
with tui.InputPump() as pump:
    pump.open_picker(
        ["agent-one"], allow_filter=False,
        actions={"enter": "attach", "space": "peek"})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        event = pump.get(0.2)
        if event and event.kind == "picker_result":
            picked = [event.mode, event.value]
            break
print("RESULT:" + json.dumps(picked))
""",
            [(0.12, b" ")])
        self.assertEqual(result, ["peek", "agent-one"])

    def test_ctrl_t_attach_direct_message_does_not_enter_main_queue(self):
        result = self.run_child(
            r"""
import json
import os
import tempfile
import threading
import time

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
    os.environ["HOME"] = home
    import zylab
    from core import agent as agent_mod
    from core import agents, client, store, tools
    from tests.pty_harness import announce_when, install_ready_input_pump

    install_ready_input_pump()

    store.ensure_home()
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.model = "main-model"
    agent.gateway = "test"
    agent.session_id = "pty-agent-session"
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

    runtime = agents.AgentRuntime(store.AGENT_RUNS)
    context = tools.ExecutionContext.capture(
        session=agent.session_id, turn_id="turn-parent",
        model="child-model", gateway="boyue",
        permission_mode="default", hook_config={})
    child = runtime.spawn("inspect", execution_context=context)
    runtime.store.claim(child["id"], owner_id="external")

    calls = {"n": 0}
    release_main = threading.Event()
    def fake_stream(model, messages, **kwargs):
        calls["n"] += 1
        print("ZYLAB_TEST_MAIN_STREAM_STARTED", flush=True)
        release_main.wait()
        yield {"t": "text", "v": "main answer"}
        yield {"t": "done", "reason": "stop", "usage": {}}

    client.stream_chat = fake_stream
    session = zylab.Session(agent)
    agent.confirm = session.confirm
    announce_when(
        lambda: session.pump.snapshot().mode == "picker",
        "ZYLAB_TEST_AGENT_PICKER_OPEN")
    announce_when(
        lambda: session.attached_agent_id == child["id"],
        "ZYLAB_TEST_AGENT_ATTACHED")
    announce_when(
        lambda: bool(runtime.get(child["id"])["inbox"]),
        "ZYLAB_TEST_DIRECT_QUEUED")
    announce_when(
        lambda: (bool(runtime.get(child["id"])["inbox"])
                 and session.attached_agent_id is None),
        "ZYLAB_TEST_AGENT_DETACHED",
        on_ready=release_main.set)
    announce_when(
        lambda: (calls["n"] >= 1
                 and session.controller.current_turn_id is None
                 and not session.controller.queued
                 and not session.pump.snapshot().busy),
        "ZYLAB_TEST_MAIN_IDLE")
    zylab.repl(session, agent.model)
    child_after = runtime.get(child["id"])
    result = {
        "calls": calls["n"],
        "main_users": [
            message["content"] for message in agent.messages
            if message.get("role") == "user"
        ],
        "journal_users": [
            event["payload"]["message"]["content"]
            for event in session.controller.journal.events
            if event["kind"] == "user_message"
        ],
        "child_inbox": [
            item["text"] for item in child_after["inbox"]
        ],
        "target": session.attached_agent_id,
    }
print("RESULT:" + json.dumps(result, ensure_ascii=False))
""",
            [PTYSend(
                 b"first\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(
                 b"\x14", after="ZYLAB_TEST_MAIN_STREAM_STARTED"),
             PTYSend(
                 b"\r", after="ZYLAB_TEST_AGENT_PICKER_OPEN"),
             PTYSend(
                 b"direct correction\r",
                 after="ZYLAB_TEST_AGENT_ATTACHED"),
             PTYSend(
                 b"\x1b", after="ZYLAB_TEST_DIRECT_QUEUED"),
             PTYSend(
                 b"/exit\r", after="ZYLAB_TEST_MAIN_IDLE")])

        self.assertEqual(result["calls"], 1, result)
        self.assertEqual(result["main_users"], ["first"])
        self.assertEqual(result["journal_users"], ["first"])
        self.assertEqual(result["child_inbox"], ["direct correction"])
        self.assertIsNone(result["target"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
