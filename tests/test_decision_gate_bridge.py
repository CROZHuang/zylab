"""Owner-thread decision-gate bridge regression tests.

The important invariant is architectural: a managed worker may block waiting
for a decision, but it must never call TerminalRenderer or a UI callback
itself.  The tests use only synthetic options and a temporary artifact root.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.pty_harness import PTYSend, run_pty_child

from core import tasks, tools, tui
import zylab as CLI


class TaskInteractionBridgeTests(unittest.TestCase):
    def _next_until(self, generator, predicate, limit=200):
        for _ in range(limit):
            event = next(generator)
            if predicate(event):
                return event
        self.fail("没有收到预期的 task event")

    def test_worker_request_is_lossless_and_owner_resolves_it(self):
        worker_ident = []

        def runner(name, args, *, task, **kwargs):
            worker_ident.append(threading.get_ident())
            raw = tools.t_decision_gate(
                question="选择方向",
                options=[
                    {"label": "A", "detail": "保留方案 A", "cost": "10 min",
                     "recommended": True},
                    {"label": "B", "detail": "切换方案 B", "cost": "30 min",
                     "recommended": False},
                ],
                _task=task)
            return raw

        with tempfile.TemporaryDirectory() as tmp:
            manager = tasks.TaskManager(
                Path(tmp), runner, poll_interval=0.001, max_history=4)
            stream = manager.run("decision_gate", {}, task_key="call-1")
            self.assertEqual(next(stream)["t"], "started")
            request = self._next_until(
                stream, lambda event: event.get("t") == "interaction_request")
            self.assertTrue(worker_ident)
            self.assertNotEqual(worker_ident[0], threading.get_ident())
            self.assertEqual(request["kind"], "decision_gate")
            self.assertEqual(request["payload"]["question"], "选择方向")

            manager.resolve_interaction(
                request["task_id"], request["request_id"], {
                    "choice": "B", "notes": "用户指定", "attended": True,
                    "status": "resolved",
                })
            terminal = self._next_until(
                stream, lambda event: event.get("t") == "completed")
            value = json.loads(terminal["v"])
            self.assertEqual(value["choice"], "B")
            self.assertEqual(value["status"], "resolved")
            stream.close()

    def test_cancellation_wakes_waiting_worker_without_a_choice(self):
        def runner(name, args, *, task, **kwargs):
            return tools.t_decision_gate(
                question="q",
                options=[
                    {"label": "A", "recommended": True},
                    {"label": "B", "recommended": False},
                ], _task=task)

        with tempfile.TemporaryDirectory() as tmp:
            manager = tasks.TaskManager(Path(tmp), runner, poll_interval=0.001)
            stream = manager.run("decision_gate", {}, task_key="call-2")
            started = next(stream)
            request = self._next_until(
                stream, lambda event: event.get("t") == "interaction_request")
            manager.cancel(started["task_id"])
            terminal = self._next_until(
                stream, lambda event: event.get("t") in {
                    "cancelled", "failed", "completed"})
            # The task itself is cancelled; importantly, the worker did not
            # receive an attended choice or hang forever.
            self.assertEqual(terminal["t"], "cancelled")
            self.assertIsNotNone(request["request_id"])
            stream.close()


class DecisionGateToolContractTests(unittest.TestCase):
    def test_rejects_duplicate_or_missing_recommendation(self):
        with self.assertRaises(ValueError):
            tools.t_decision_gate(
                question="q",
                options=[
                    {"label": "same", "recommended": True},
                    {"label": "same", "recommended": False},
                ],
                _task=object())
        with self.assertRaises(ValueError):
            tools.t_decision_gate(
                question="q",
                options=[
                    {"label": "a", "recommended": False},
                    {"label": "b", "recommended": False},
                ],
                _task=object())

    def test_invalid_owner_choice_is_fail_closed(self):
        saved = tools.HOOK_CTX.get("decision_gate")
        tools.HOOK_CTX["decision_gate"] = lambda payload: {
            "choice": "not-an-option", "attended": True}
        try:
            raw = tools.t_decision_gate(
                question="q",
                options=[
                    {"label": "a", "recommended": True},
                    {"label": "b", "recommended": False},
                ])
        finally:
            if saved is None:
                tools.HOOK_CTX.pop("decision_gate", None)
            else:
                tools.HOOK_CTX["decision_gate"] = saved
        value = json.loads(raw)
        self.assertEqual(value["status"], "invalid")
        self.assertFalse(value["attended"])
        self.assertEqual(value["choice"], "")

    def test_explicit_failure_cannot_be_upgraded_by_a_stale_choice(self):
        options = [
            {"label": "a", "recommended": True},
            {"label": "b", "recommended": False},
        ]
        value = tools.normalize_decision_gate_result({
            "choice": "a", "attended": True, "status": "cancelled",
        }, options)
        self.assertEqual(value["status"], "cancelled")
        self.assertFalse(value["attended"])
        self.assertEqual(value["choice"], "")
        self.assertFalse(tools.decision_gate_is_resolved(
            json.dumps(value, ensure_ascii=False)))

    def test_resolved_requires_attended_and_choice(self):
        value = tools.normalize_decision_gate_result({
            "choice": "a", "attended": False, "status": "resolved",
        }, [
            {"label": "a", "recommended": True},
            {"label": "b", "recommended": False},
        ])
        self.assertEqual(value["status"], "invalid")
        self.assertFalse(tools.decision_gate_is_resolved(value))


class DecisionGateTuiTests(unittest.TestCase):
    def test_dedicated_modal_keeps_selected_consequence_visible(self):
        pump = tui.InputPump(stream=mock.Mock(isatty=lambda: True))
        pump.configure(
            status="model@gateway · ctx 12K/100K",
            activity="需要拍板",
            publish=False,
        )
        pump.open_decision_gate(
            question="是否改变交付物形状？",
            context="这会影响下游消费者",
            options=[
                {"label": "保守", "detail": "不改变形状", "cost": "少一次重跑",
                 "recommended": True},
                {"label": "激进", "detail": "迁移新形状", "cost": "+40 min",
                 "recommended": False},
            ])
        first = pump.get(0.1)
        self.assertEqual(first.kind, "redraw")
        self.assertEqual(first.snapshot.mode, "decision_gate")
        self.assertIn("不改变形状", first.snapshot.option_details[0]["detail"])
        pump._handle_decision_gate("down")
        snap = pump.snapshot()
        self.assertEqual(snap.selected, 1)
        self.assertEqual(snap.option_details[1]["cost"], "+40 min")
        renderer = tui.TerminalRenderer(stream=mock.Mock(
            isatty=lambda: False, write=lambda value: None,
            flush=lambda: None))
        lines, _row, _col = renderer._build_frame(snap, 80)
        plain = "\n".join(renderer._transcript_plain(line) for line in lines)
        self.assertIn("是否改变交付物形状？", plain)
        self.assertIn("迁移新形状", plain)
        self.assertIn("代价：+40 min", plain)
        self.assertIn("model@gateway", plain)
        self.assertIn("ctx 12K/100K", plain)

    def test_recommendation_is_selected_without_being_submitted(self):
        pump = tui.InputPump(stream=mock.Mock(isatty=lambda: True))
        pump.open_decision_gate(
            question="q",
            options=[
                {"label": "a", "recommended": False},
                {"label": "b", "recommended": True},
            ])
        first = pump.get(0.1)
        self.assertEqual(first.snapshot.selected, 1)
        pump._handle_decision_gate("1")
        self.assertEqual(pump.snapshot().selected, 0)

    def test_multi_question_space_and_enter_return_all_answers(self):
        pump = tui.InputPump(stream=mock.Mock(isatty=lambda: True))
        pump.open_decision_gate(questions=[
            {
                "id": "shape", "question": "形状？",
                "options": [
                    {"label": "兼容", "recommended": True},
                    {"label": "迁移", "recommended": False},
                ],
            },
            {
                "id": "checks", "question": "检查？", "multi_select": True,
                "options": [
                    {"label": "单测", "recommended": True},
                    {"label": "PTY", "recommended": False},
                ],
            },
        ])
        pump.get(0.1)  # initial redraw
        # Enter explicitly commits the first question and advances; it does
        # not silently submit the recommendation.
        events = pump._handle_decision_gate("enter")
        self.assertEqual(events[0].snapshot.modal_question_index, 1)
        # The recommended multi-select option is a visible default.  Space
        # toggles it off, then the numeric shortcut selects the second item.
        self.assertEqual(events[0].snapshot.modal_selected, ("单测",))
        pump._handle_decision_gate("space")
        events = pump._handle_decision_gate("2")
        self.assertEqual(events[0].snapshot.modal_selected, ("PTY",))
        events = pump._handle_decision_gate("enter")
        self.assertEqual(events[0].kind, "decision_gate_result")
        value = events[0].value
        self.assertEqual(value["answers"], {
            "shape": "兼容", "checks": ["PTY"]})

    def test_dismiss_decision_gate_does_not_emit_choice(self):
        pump = tui.InputPump(stream=mock.Mock(isatty=lambda: True))
        pump.open_decision_gate(
            question="q",
            options=[
                {"label": "a", "recommended": True},
                {"label": "b", "recommended": False},
            ])
        pump.drain()
        self.assertTrue(pump.dismiss_decision_gate())
        self.assertEqual(pump.snapshot().mode, "line")
        self.assertFalse(any(
            event.kind == "decision_gate_result" for event in pump.drain()))


class SessionOwnerBridgeTests(unittest.TestCase):
    def test_owner_bridge_preserves_multi_question_payload_and_answers(self):
        class Controller:
            state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND
            current_tool_call_id = "call-multi"

            def __init__(self):
                self.calls = []

            def begin_interaction(self, *args):
                self.calls.append(("begin", args))
                self.state = CLI.C.ControllerState.TOOL_WAITING_DECISION

            def resolve_interaction(self, *args):
                self.calls.append(("resolve", args))
                self.state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND

        class Manager:
            def resolve_interaction(self, *_args):
                return True

        questions = [
            {
                "id": "shape", "question": "形状？",
                "options": [
                    {"label": "兼容", "recommended": True},
                    {"label": "迁移", "recommended": False},
                ],
            },
            {
                "id": "checks", "question": "检查？", "multi_select": True,
                "options": [
                    {"label": "单测", "recommended": True},
                    {"label": "PTY", "recommended": False},
                ],
            },
        ]
        session = CLI.Session.__new__(CLI.Session)
        session._ui_owner_thread_id = threading.get_ident()
        session.controller = Controller()
        session.task_manager = Manager()
        session._pending_interactions = {}
        seen = {}

        def owner(payload):
            seen["questions"] = payload.get("questions")
            return {
                "choice": "兼容", "answers": {
                    "shape": "兼容", "checks": ["PTY"]},
                "attended": True, "status": "resolved",
            }

        session._decision_gate_owner = owner
        result = session.handle_interaction_request({
            "task_id": "task-multi", "tool_call_id": "call-multi",
            "request_id": "req-multi", "kind": "decision_gate",
            "payload": {"_interaction_role": "main", "questions": questions},
        })
        self.assertEqual(result["status"], "resolved")
        self.assertEqual([item["id"] for item in seen["questions"]],
                         ["shape", "checks"])
        self.assertEqual(result["answers"]["checks"], ["PTY"])

    def test_owner_audits_before_waking_worker(self):
        class Controller:
            state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND
            current_tool_call_id = "call-1"

            def __init__(self):
                self.calls = []

            def begin_interaction(self, *args):
                self.calls.append(("begin", args))
                self.state = CLI.C.ControllerState.TOOL_WAITING_DECISION

            def resolve_interaction(self, *args):
                self.calls.append(("resolve", args))
                self.state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND

        class Manager:
            def __init__(self):
                self.calls = []

            def resolve_interaction(self, *args):
                self.calls.append(args)
                return True

        session = CLI.Session.__new__(CLI.Session)
        session._ui_owner_thread_id = threading.get_ident()
        session.controller = Controller()
        session.task_manager = Manager()
        session._decision_gate_owner = lambda payload: {
            "choice": "A", "notes": "", "attended": True,
            "status": "resolved",
        }
        session._pending_interactions = {}

        result = session.handle_interaction_request({
            "task_id": "task-1",
            "tool_call_id": "call-1",
            "request_id": "req-1",
            "kind": "decision_gate",
            "payload": {
                "_interaction_role": "main",
                "question": "q",
                "options": [
                    {"label": "A", "recommended": True},
                    {"label": "B", "recommended": False},
                ],
            },
        })
        self.assertEqual(result["status"], "resolved")
        self.assertEqual([item[0] for item in session.controller.calls],
                         ["begin", "resolve"])
        self.assertEqual(session.task_manager.calls[0][0], "task-1")
        self.assertEqual(session.task_manager.calls[0][1], "req-1")

    def test_owner_result_is_normalized_before_controller_audit(self):
        class Controller:
            state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND
            current_tool_call_id = "call-1"

            def __init__(self):
                self.calls = []

            def begin_interaction(self, *args):
                self.calls.append(("begin", args))
                self.state = CLI.C.ControllerState.TOOL_WAITING_DECISION

            def resolve_interaction(self, *args):
                self.calls.append(("resolve", args))
                self.state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND

        class Manager:
            def resolve_interaction(self, *args):
                return True

        session = CLI.Session.__new__(CLI.Session)
        session._ui_owner_thread_id = threading.get_ident()
        session.controller = Controller()
        session.task_manager = Manager()
        # A stale UI callback supplies a label while explicitly cancelling.
        session._decision_gate_owner = lambda payload: {
            "choice": "A", "notes": "stale", "attended": True,
            "status": "cancelled",
        }
        session._pending_interactions = {}

        result = session.handle_interaction_request({
            "task_id": "task-1",
            "tool_call_id": "call-1",
            "request_id": "req-1",
            "kind": "decision_gate",
            "payload": {
                "_interaction_role": "main",
                "question": "q",
                "options": [
                    {"label": "A", "recommended": True},
                    {"label": "B", "recommended": False},
                ],
            },
        })
        self.assertEqual(result["status"], "cancelled")
        resolved = session.controller.calls[-1][1][-1]
        self.assertEqual(resolved["status"], "cancelled")
        self.assertEqual(resolved["choice"], "")

    def test_owner_uses_pump_stream_for_tty_detection(self):
        class Pump:
            stream = mock.Mock(isatty=lambda: True)

            def __init__(self):
                self.events = [
                    tui.PumpEvent("redraw", snapshot=None),
                    tui.PumpEvent("decision_gate_result", value={
                        "choice": "A", "attended": True,
                        "status": "resolved",
                    }),
                ]

            def open_decision_gate(self, **kwargs):
                return None

            def get(self, timeout=None):
                return self.events.pop(0) if self.events else None

        class Renderer:
            def clear_input(self):
                return None

            def render(self, snapshot):
                return None

        session = CLI.Session.__new__(CLI.Session)
        session._ui_owner_thread_id = threading.get_ident()
        session.pump = Pump()
        session.renderer = Renderer()
        session.watcher = None
        session._deferred_pump_events = []
        # Make the process-level stdin irrelevant; the bound pump is the
        # authoritative stream for embedded/PTY sessions.
        with mock.patch.object(sys, "stdin", mock.Mock(
                isatty=lambda: False)):
            result = session._decision_gate_owner({
                "_interaction_role": "main",
                "question": "q",
                "options": [
                    {"label": "A", "recommended": True},
                    {"label": "B", "recommended": False},
                ],
            })
        self.assertEqual(result["status"], "resolved")

    def test_failed_worker_wake_cancels_exact_task(self):
        """A resolver failure must not leave the worker parked forever."""
        class Controller:
            state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND
            current_tool_call_id = "call-1"

            def __init__(self):
                self.calls = []

            def record_event(self, kind, payload=None, **kwargs):
                self.calls.append((kind, payload, kwargs))
                return payload

            def begin_interaction(self, *args):
                self.calls.append(("begin", args))
                self.state = CLI.C.ControllerState.TOOL_WAITING_DECISION

            def resolve_interaction(self, *args):
                self.calls.append(("resolve", args))
                self.state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND

        class Manager:
            def __init__(self):
                self.cancelled = []

            def resolve_interaction(self, *_args):
                raise RuntimeError("worker disappeared")

            def cancel(self, task_id):
                self.cancelled.append(task_id)
                return True

        session = CLI.Session.__new__(CLI.Session)
        session._ui_owner_thread_id = threading.get_ident()
        session.controller = Controller()
        session.task_manager = Manager()
        session._decision_gate_owner = lambda _payload: {
            "choice": "A", "notes": "", "attended": True,
            "status": "resolved",
        }
        session._pending_interactions = {}

        result = session.handle_interaction_request({
            "task_id": "task-exact",
            "tool_call_id": "call-1",
            "request_id": "req-1",
            "kind": "decision_gate",
            "payload": {
                "_interaction_role": "main",
                "question": "q",
                "options": [
                    {"label": "A", "recommended": True},
                    {"label": "B", "recommended": False},
                ],
            },
        })
        self.assertEqual(result["status"], "persistence_error")
        self.assertIn("bridge_error", result)
        self.assertEqual(session.task_manager.cancelled, ["task-exact"])
        self.assertTrue(any(
            item[0] == "interaction_bridge_failed"
            for item in session.controller.calls))

    def test_malformed_payload_is_fail_closed_without_owner_loop_crash(self):
        class Controller:
            state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND
            current_tool_call_id = "call-1"

            def begin_interaction(self, *args):
                self.state = CLI.C.ControllerState.TOOL_WAITING_DECISION

            def resolve_interaction(self, *args):
                self.state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND

        class Manager:
            def __init__(self):
                self.result = None

            def resolve_interaction(self, _task, _request, result):
                self.result = result
                return True

        session = CLI.Session.__new__(CLI.Session)
        session._ui_owner_thread_id = threading.get_ident()
        session.controller = Controller()
        session.task_manager = Manager()
        session._pending_interactions = {}

        result = session.handle_interaction_request({
            "task_id": "task-1", "tool_call_id": "call-1",
            "request_id": "req-malformed", "kind": "decision_gate",
            "payload": "not an object",
        })
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(session.task_manager.result["status"], "invalid")

    def test_foreign_task_event_cannot_open_current_gate(self):
        class Controller:
            state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND
            current_tool_call_id = "call-current"
            current_task_id = "task-current"

            def __init__(self):
                self.begun = 0

            def begin_interaction(self, *args):
                self.begun += 1

        class Manager:
            def __init__(self):
                self.result = None

            def resolve_interaction(self, _task, _request, result):
                self.result = result
                return True

        session = CLI.Session.__new__(CLI.Session)
        session._ui_owner_thread_id = threading.get_ident()
        session.controller = Controller()
        session.task_manager = Manager()
        session._pending_interactions = {}

        result = session.handle_interaction_request({
            "task_id": "task-foreign", "tool_call_id": "call-current",
            "request_id": "req-foreign", "kind": "decision_gate",
            "payload": {
                "_interaction_role": "main",
                "question": "q",
                "options": [
                    {"label": "A", "recommended": True},
                    {"label": "B", "recommended": False},
                ],
            },
        })
        self.assertEqual(result["status"], "invalid")
        self.assertIn("task_id", result["reason"])
        self.assertEqual(session.controller.begun, 0)
        self.assertEqual(session.task_manager.result["status"], "invalid")

    def test_non_main_event_cannot_open_owner_modal(self):
        class Controller:
            state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND
            current_tool_call_id = "call-child"

            def __init__(self):
                self.begun = 0

            def begin_interaction(self, *args):
                self.begun += 1

        class Manager:
            def __init__(self):
                self.result = None

            def resolve_interaction(self, _task, _request, result):
                self.result = result
                return True

        session = CLI.Session.__new__(CLI.Session)
        session._ui_owner_thread_id = threading.get_ident()
        session.controller = Controller()
        session.task_manager = Manager()
        session._pending_interactions = {}
        session._decision_gate_owner = mock.Mock(
            side_effect=AssertionError("child must not reach owner UI"))

        result = session.handle_interaction_request({
            "task_id": "task-child", "tool_call_id": "call-child",
            "request_id": "req-child", "kind": "decision_gate",
            "payload": {
                "_interaction_role": "workflow",
                "question": "q",
                "options": [
                    {"label": "A", "recommended": True},
                    {"label": "B", "recommended": False},
                ],
            },
        })
        self.assertEqual(result["status"], "unattended")
        self.assertEqual(session.controller.begun, 0)
        self.assertEqual(session.task_manager.result["status"], "unattended")
        session._decision_gate_owner.assert_not_called()


DECISION_GATE_PTY_BODY = r'''
import json
import os
import tempfile
import time

with tempfile.TemporaryDirectory() as home:
    os.environ["HOME"] = home
    os.environ["TERM"] = "xterm-256color"
    import zylab
    from core import agent as agent_mod, client, store
    from tests.pty_harness import announce_when, install_ready_input_pump

    install_ready_input_pump()
    store.ensure_home()
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.model = "m"
    agent.gateway = "deepinfer"
    agent.session_id = "decision-gate-pty"
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
    agent.supports_tools = True
    agent.hook_cfg = {}

    phase = {"value": 0}
    def fake_stream(model, messages, **kwargs):
        if phase["value"] == 0:
            phase["value"] = 1
            yield {"t": "tool", "v": [{
                "id": "gate-call",
                "function": {"name": "decision_gate", "arguments": json.dumps({
                    "question": "选择交付形状",
                    "context": "会影响下游消费者",
                    "options": [
                        {"label": "保守", "detail": "保持兼容", "cost": "10 min",
                         "recommended": True},
                        {"label": "迁移", "detail": "采用新形状", "cost": "40 min",
                         "recommended": False},
                    ],
                })},
            }]}
            yield {"t": "done", "reason": "tool_calls", "usage": {}}
        else:
            yield {"t": "text", "v": "gate resolved"}
            yield {"t": "done", "reason": "stop", "usage": {}}
    client.stream_chat_background = fake_stream

    session = zylab.Session(agent)
    agent.confirm = session.confirm
    announce_when(
        lambda: session.pump.snapshot().mode == "decision_gate",
        "ZYLAB_TEST_GATE_OPEN")
    announce_when(
        lambda: session.pump.snapshot().mode == "prompt",
        "ZYLAB_TEST_GATE_NOTES")
    announce_when(
        lambda: (session.controller.current_turn_id is None
                 and not session.pump.snapshot().busy),
        "ZYLAB_TEST_TURN_DONE")
    zylab.repl(session, "m")
    print("RESULT:" + json.dumps({
        "events": [row["kind"] for row in session.controller.journal.events],
        "text": agent.messages[-1].get("content"),
    }, ensure_ascii=False))
'''


class DecisionGatePtyTests(unittest.TestCase):
    def test_real_pty_resolves_gate_without_owner_thread_error(self):
        output, result = run_pty_child(
            DECISION_GATE_PTY_BODY,
            [
                PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
                PTYSend(b"\x1b[B", after="ZYLAB_TEST_GATE_OPEN"),
                PTYSend(b"\r", after="ZYLAB_TEST_GATE_OPEN"),
                PTYSend(b"\r", after="ZYLAB_TEST_GATE_NOTES"),
                PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE"),
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=30.0,
        )
        self.assertIn("gate resolved", output)
        self.assertNotIn("TerminalRenderer 只能由 owner 线程", output)
        self.assertIn("interaction_requested", result["events"])
        self.assertIn("interaction_resolved", result["events"])


if __name__ == "__main__":
    unittest.main()
