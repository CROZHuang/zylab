"""Runtime decision-candidate guard and authority-boundary regressions."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent as A, client, controller as C, tools  # noqa: E402
from tests.test_agent_loop import mk, scripted  # noqa: E402


class RuntimeCandidateGuardTests(unittest.TestCase):
    def test_managed_like_agent_blocks_workflow_before_runner(self):
        ag = mk()
        ag.supports_tools = True
        executed = []
        provider = scripted(
            ("", [("workflow", {"goal": "expensive", "agents": [{}, {}]})]),
            ("recovered", []),
        )

        def run_tool(name, _args, **_kwargs):
            executed.append(name)
            return "unexpected side effect"

        journal = []
        with mock.patch.object(client, "stream_chat", provider), \
                mock.patch.object(tools, "run", side_effect=run_tool), \
                mock.patch.object(ag, "log_usage"):
            events = list(ag.run(
                "start", event_sink=lambda rows: journal.extend(rows)))

        self.assertEqual(executed, [])
        self.assertTrue(any(
            row.get("kind") == "decision_candidate_detected"
            for row in journal))
        blocked = [row for row in ag.messages
                   if row.get("role") == "tool"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("需要先调用 decision_gate", blocked[0]["content"])
        self.assertEqual(events[-1]["t"], "end")

    def test_repeated_candidate_fails_closed_instead_of_burning_turns(self):
        ag = mk()
        ag.supports_tools = True
        call = ("workflow", {"goal": "same", "agents": [{}, {}]})
        provider = scripted(("", [call]), ("", [call]))
        with mock.patch.object(client, "stream_chat", provider), \
                mock.patch.object(tools, "run", side_effect=AssertionError), \
                mock.patch.object(ag, "log_usage"):
            events = list(ag.run("start", max_turns=4))
        error = next(item for item in events if item["t"] == "error")
        self.assertEqual(error["kind"], "decision_gate_required")

    def test_resolved_gate_authorizes_one_followup_fanout(self):
        ag = mk()
        ag.supports_tools = True
        workflow_args = {
            "goal": "expensive",
            "agents": [{"task": "a"}, {"task": "b"}],
        }
        gate_args = {
            "question": "允许并行 provider 请求？",
            "options": [
                {"label": "允许", "detail": "启动两个只读 agent",
                 "cost": "2 requests", "recommended": True},
                {"label": "拒绝", "detail": "不启动 workflow",
                 "cost": "无额外请求", "recommended": False},
            ],
        }
        provider = scripted(
            ("", [("workflow", workflow_args)]),
            ("", [("decision_gate", gate_args)]),
            ("", [("workflow", workflow_args)]),
            ("done", []),
        )
        executed = []

        def run_tool(name, args, **kwargs):
            executed.append(name)
            if name == "decision_gate":
                return tools.t_decision_gate(
                    **args, _execution_context=kwargs.get("context"))
            return "workflow started"

        saved = tools.HOOK_CTX.get("decision_gate")
        tools.HOOK_CTX["decision_gate"] = lambda payload: {
            "choice": payload["options"][0]["label"],
            "attended": True, "status": "resolved", "notes": "ok",
        }
        journal = []
        try:
            with mock.patch.object(client, "stream_chat", provider), \
                    mock.patch.object(tools, "run", side_effect=run_tool):
                events = list(ag.run(
                    "start", event_sink=lambda rows: (journal.extend(rows)
                                                       or rows)))
        finally:
            if saved is None:
                tools.HOOK_CTX.pop("decision_gate", None)
            else:
                tools.HOOK_CTX["decision_gate"] = saved

        self.assertEqual(executed, ["decision_gate", "workflow"])
        self.assertTrue(any(
            row.get("kind") == "decision_candidate_authorized"
            for row in journal))
        self.assertEqual(events[-1]["t"], "end")

    def test_managed_path_also_consumes_gate_authorization(self):
        ag = mk()
        ag.supports_tools = True
        ag.permission_decision = lambda _name, _prepared: {
            "allowed": True, "decision": "once", "source": "test",
        }
        workflow_args = {
            "goal": "expensive",
            "agents": [{"task": "a"}, {"task": "b"}],
        }
        gate_args = {
            "question": "允许并行 provider 请求？",
            "options": [
                {"label": "允许", "detail": "启动两个只读 agent",
                 "cost": "2 requests", "recommended": True},
                {"label": "拒绝", "detail": "不启动 workflow",
                 "cost": "无额外请求", "recommended": False},
            ],
        }
        provider = scripted(
            ("", [("workflow", workflow_args)]),
            ("", [("decision_gate", gate_args)]),
            ("", [("workflow", workflow_args)]),
            ("done", []),
        )
        executed = []

        def run_tool(name, args, **kwargs):
            executed.append(name)
            if name == "decision_gate":
                return tools.t_decision_gate(
                    **args, _execution_context=kwargs.get("context"))
            return "workflow started"

        saved = tools.HOOK_CTX.get("decision_gate")
        tools.HOOK_CTX["decision_gate"] = lambda payload: {
            "choice": payload["options"][0]["label"],
            "attended": True, "status": "resolved", "notes": "ok",
        }
        controller = C.SessionController("t", C.MemoryJournal())
        controller.submit("start")
        try:
            with mock.patch.object(client, "stream_chat", provider), \
                    mock.patch.object(tools, "run", side_effect=run_tool):
                events = list(ag.run(
                    "start", controller=controller,
                    stream_factory=provider))
        finally:
            if saved is None:
                tools.HOOK_CTX.pop("decision_gate", None)
            else:
                tools.HOOK_CTX["decision_gate"] = saved

        self.assertEqual(executed, ["decision_gate", "workflow"])
        self.assertEqual(controller.state, C.ControllerState.IDLE)
        self.assertEqual(events[-1]["t"], "end")

    def test_gate_first_authorizes_one_followup_fanout(self):
        """The model's preferred gate-before-tool order must not deadlock."""
        ag = mk()
        ag.supports_tools = True
        gate_args = {
            "question": "允许并行 provider 请求？",
            "options": [
                {"label": "允许", "detail": "启动两个只读 agent",
                 "cost": "2 requests", "recommended": True},
                {"label": "拒绝", "detail": "不启动 workflow",
                 "cost": "无额外请求", "recommended": False},
            ],
        }
        workflow_args = {
            "goal": "expensive",
            "agents": [{"task": "a"}, {"task": "b"}],
        }
        provider = scripted(
            ("", [("decision_gate", gate_args)]),
            ("", [("workflow", workflow_args)]),
            ("done", []),
        )
        executed = []

        def run_tool(name, args, **kwargs):
            executed.append(name)
            if name == "decision_gate":
                return tools.t_decision_gate(
                    **args, _execution_context=kwargs.get("context"))
            return "workflow started"

        saved = tools.HOOK_CTX.get("decision_gate")
        tools.HOOK_CTX["decision_gate"] = lambda payload: {
            "choice": payload["options"][0]["label"],
            "attended": True, "status": "resolved", "notes": "ok",
        }
        try:
            with mock.patch.object(client, "stream_chat", provider), \
                    mock.patch.object(tools, "run", side_effect=run_tool):
                events = list(ag.run("start"))
        finally:
            if saved is None:
                tools.HOOK_CTX.pop("decision_gate", None)
            else:
                tools.HOOK_CTX["decision_gate"] = saved

        self.assertEqual(executed, ["decision_gate", "workflow"])
        self.assertEqual(events[-1]["t"], "end")

    def test_batched_gate_is_not_consent_until_reissued_standalone(self):
        """A gate bundled with fan-out is held, then the standalone retry works."""
        ag = mk()
        ag.supports_tools = True
        gate_args = {
            "question": "允许并行 provider 请求？",
            "options": [
                {"label": "允许", "detail": "启动只读 agent",
                 "cost": "2 requests", "recommended": True},
                {"label": "拒绝", "detail": "保持当前 turn",
                 "cost": "无额外请求", "recommended": False},
            ],
        }
        workflow_args = {
            "goal": "expensive",
            "agents": [{"task": "a"}, {"task": "b"}],
        }
        provider = scripted(
            ("", [("workflow", workflow_args),
                  ("decision_gate", gate_args)]),
            ("", [("decision_gate", gate_args)]),
            ("", [("workflow", workflow_args)]),
            ("done", []),
        )
        executed = []

        def run_tool(name, args, **kwargs):
            executed.append(name)
            if name == "decision_gate":
                return tools.t_decision_gate(
                    **args, _execution_context=kwargs.get("context"))
            return "workflow started"

        saved = tools.HOOK_CTX.get("decision_gate")
        tools.HOOK_CTX["decision_gate"] = lambda payload: {
            "choice": payload["options"][0]["label"],
            "attended": True, "status": "resolved", "notes": "ok",
        }
        try:
            with mock.patch.object(client, "stream_chat", provider), \
                    mock.patch.object(tools, "run", side_effect=run_tool):
                events = list(ag.run("start"))
        finally:
            if saved is None:
                tools.HOOK_CTX.pop("decision_gate", None)
            else:
                tools.HOOK_CTX["decision_gate"] = saved

        self.assertEqual(executed, ["decision_gate", "workflow"])
        self.assertEqual(events[-1]["t"], "end")


class MainOnlyAuthorityTests(unittest.TestCase):
    def test_non_main_runtime_does_not_advertise_decision_gate(self):
        for role in ("workflow", "subagent"):
            with self.subTest(role=role):
                ag = mk()
                ag.interaction_role = role
                self.assertNotIn(
                    "decision_gate",
                    ag._effective_tool_allowlist(None))
                self.assertNotIn(
                    "decision_gate",
                    ag._effective_tool_allowlist({"decision_gate", "read_file"}))

    def test_execution_context_serializes_role(self):
        context = tools.ExecutionContext.capture(
            session="s", interaction_role="workflow")
        self.assertEqual(context.interaction_role, "workflow")
        self.assertFalse(context.can_request_decision)

    def test_child_role_is_not_inherited_from_parent_spawn(self):
        # This checks the public boundary without touching a real HOME: the
        # runtime's spawn path must rewrite a parent's ``main`` role.
        from core import agents
        with mock.patch.object(agents.AgentStore, "create", return_value={
                "id": "a-test", "kind": "subagent"}) as create:
            runtime = agents.AgentRuntime(root=Path(os.environ.get(
                "TMPDIR", "/tmp")) / "zylab-role-test")
            parent = tools.ExecutionContext.capture(
                session="s", turn_id="t", model="m", gateway="test",
                interaction_role="main")
            runtime.spawn("read", execution_context=parent, kind="subagent")
            context = create.call_args.kwargs["execution_context"]
            self.assertEqual(context.interaction_role, "subagent")

    def test_workflow_kind_also_gets_non_main_role(self):
        from core import agents
        with mock.patch.object(agents.AgentStore, "create", return_value={
                "id": "a-workflow", "kind": "workflow"}) as create:
            runtime = agents.AgentRuntime(root=Path("/tmp") / "zylab-role-test")
            parent = tools.ExecutionContext.capture(
                session="s", turn_id="t", model="m", gateway="test",
                interaction_role="main")
            runtime.spawn("read", execution_context=parent, kind="workflow")
            context = create.call_args.kwargs["execution_context"]
            self.assertEqual(context.interaction_role, "workflow")


if __name__ == "__main__":
    unittest.main()
