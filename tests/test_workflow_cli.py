"""CLI /workflow、自动交接与启动动画回归。"""
import contextlib
import io
import os
import unittest
from unittest import mock

import zylab as CLI
from core import controller, tools


class MainAgent:
    model = "main-model"
    gateway = "deepinfer"
    session_id = "workflow-parent"
    compact_at = 100_000
    supports_tools = True
    tokens_in = 0
    tokens_out = 0
    last_total = 0


def selected_lineup():
    seats = ("Qwen", "GLM", "DeepSeek", "MiniMax", "Kimi")
    gateways = ("boyue", "boyue", "deepinfer", "deepinfer", "deepinfer")
    return [
        {
            "seat": seat,
            "family": seat,
            "id": f"{seat.lower()}-flagship",
            "gateway": gateway,
            "status": "ok",
            "supports_tools": True,
            "quality_tier": "flagship",
        }
        for seat, gateway in zip(seats, gateways)
    ]


def workflow_record(state="completed"):
    return {
        "version": 1,
        "id": "wf-12345678",
        "parent_session_id": "workflow-parent",
        "parent_turn_id": "workflow-turn",
        "goal": "fix parser",
        "preset": "standard",
        "mode": "implement",
        "state": state,
        "stage": state if state == "completed" else "scout",
        "lineup": [
            {
                "seat": row["seat"],
                "family": row["family"],
                "model": row["id"],
                "gateway": row["gateway"],
                "quality_tier": "flagship",
                "agent_id": None,
                "state": "queued",
            }
            for row in selected_lineup()
        ],
        "candidate_lineup": selected_lineup(),
        "missing_seats": [],
        "agents": [],
        "completed_agents": 8 if state == "completed" else 1,
        "expected_agents": 8,
        "auto_apply": True,
        "result": "verified synthesis with core/parser.py:12",
        "error": None,
    }


class FakeWorkflowManager:
    def __init__(self):
        self.record = workflow_record("completed")
        self.events = []
        self.starts = []
        self.plan_starts = []
        self.apply_marks = []
        self.cancelled = []

    def resolve_lineup(self):
        return selected_lineup()

    def start(self, goal, **kwargs):
        self.starts.append((goal, kwargs))
        self.record = workflow_record("running")
        self.record["goal"] = goal
        self.record["preset"] = kwargs["preset"]
        self.record["mode"] = kwargs["mode"]
        return dict(self.record)

    def start_plan(self, goal, agents_spec, **kwargs):
        self.plan_starts.append((goal, agents_spec, kwargs))
        self.record = workflow_record("running")
        self.record.update({
            "goal": goal,
            "parent_session_id": kwargs["parent_session_id"],
            "parent_turn_id": kwargs["parent_turn_id"],
            "preset": kwargs["preset"],
            "mode": kwargs["mode"],
            "trigger": kwargs["trigger"],
            "requests_started": 0,
            "budget": {
                "max_requests": (
                    kwargs.get("budget")
                    or kwargs["limits"]["max_requests"]),
            },
            "plan": {"agents": list(agents_spec)},
        })
        return dict(self.record)

    def get(self, _identifier):
        return dict(self.record)

    def current(self, _parent):
        return dict(self.record)

    def list(self, **_kwargs):
        return [dict(self.record)]

    def drain(self):
        events, self.events = self.events, []
        return events

    def mark_apply(self, _identifier, status):
        self.apply_marks.append(status)
        self.record["apply_status"] = status
        return dict(self.record)

    def cancel(self, identifier):
        self.cancelled.append(identifier)
        self.record["state"] = "cancelled"
        return dict(self.record)

    def status_projection(self, identifier):
        return {"workflow_id": identifier, "state": self.record["state"]}

    def add_nodes(self, identifier, specs, **kwargs):
        self.record["added"] = list(specs or [])
        self.record["added_by"] = kwargs.get("requested_by")
        return dict(self.record)

    def send_node(self, identifier, node, message, **kwargs):
        return {
            "workflow": dict(self.record),
            "message": {"id": "msg-1", "node": node, "text": message},
        }

    def close(self):
        return True


class WorkflowCLITests(unittest.TestCase):
    def make_session(self):
        sess = CLI.Session(MainAgent())
        sess.workflow_manager.close()
        fake = FakeWorkflowManager()
        sess.workflow_manager = fake
        sess.controller = controller.SessionController(
            sess.ag.session_id, controller.MemoryJournal())
        return sess, fake

    def test_command_is_registered_and_manual_template_start_is_retired(self):
        # 2026-09-04：编排归模型。/workflow <goal>、quick|deep、review|... 不再启动，
        # 只指路；配方 run 与运行中 DAG 的管理子命令保留。
        sess, manager = self.make_session()
        output = io.StringIO()

        with (
                mock.patch.object(CLI, "_TTY", False),
                contextlib.redirect_stdout(output)):
            CLI.cmd_workflow(sess, "fix parser")
            CLI.cmd_workflow(sess, "review fix parser")
            CLI.cmd_workflow(sess, "quick fix parser")

        self.assertIn("workflow", CLI.REGISTRY)
        self.assertEqual(manager.starts, [])
        self.assertIn("已退役", output.getvalue())
        self.assertIn("/workflow auto on", output.getvalue())
        sess.close()

    def test_workflow_node_events_share_the_agent_lines_and_roster(self):
        # J7：节点完成行与子代理同一句法，正文不进时间线，running 节点进名册
        sess, manager = self.make_session()
        manager.record = workflow_record("running")
        sess._workflow_current = dict(manager.record)
        base = {
            "id": manager.record["id"], "parent_session_id": sess.ag.session_id,
            "state": "running", "stage": "agents",
            "completed_agents": 1, "expected_agents": 2,
        }
        manager.events = [
            {"kind": "workflow_node", "payload": dict(base, node={
                "key": "code", "task": "梳理结构", "seat": "Qwen", "state": "running",
                "agent_id": "agent-node-1", "started_at": "2026-09-07T10:00:00"})},
            {"kind": "workflow_node", "payload": dict(base, node={
                "key": "tests", "task": "找反例", "seat": "GLM", "state": "completed",
                "agent_id": "agent-node-2", "started_at": "2026-09-07T10:00:00",
                "ended_at": "2026-09-07T10:05:05", "report": "r" * 120})},
        ]

        text = "".join(sess.drain_workflow_events())

        self.assertIn('● Node "tests · 找反例" finished · 5m 5s', text)
        self.assertIn("/agents peek agent-node-2", text)
        self.assertNotIn("rrrr", text)
        self.assertNotIn('"code · 梳理结构" finished', text)
        self.assertNotIn("node", sess._workflow_current)
        states = {node["key"]: node["state"]
                  for node in sess._workflow_current["plan"]["agents"]}
        self.assertEqual(states, {"code": "running", "tests": "completed"})
        rows = CLI._workflow_dock_rows(
            sess._workflow_current, activity_map={"agent-node-1": "读 parser.py"})
        self.assertTrue(any(
            row.startswith("○ Qwen  code · 梳理结构  读 parser.py") for row in rows), rows)
        self.assertEqual(len(sess._agent_event_log), 1)
        sess.close()

    def test_completed_implement_is_queued_and_dispatched_as_main_prompt(self):
        sess, manager = self.make_session()
        manager.record = workflow_record("completed")
        manager.record["result"] = (
            "\x1b]0;unsafe-title\x07"
            "verified synthesis with core/parser.py:12")
        manager.events = [{
            "kind": "workflow_completed",
            "payload": {
                "id": manager.record["id"],
                "parent_session_id": sess.ag.session_id,
                "state": "completed",
                "stage": "completed",
                "completed_agents": 8,
                "expected_agents": 8,
                "result": manager.record["result"],
            },
        }]

        notices = sess.drain_workflow_events()
        action = sess.dispatch_workflow_ready()

        self.assertTrue(any("已排入 main agent" in row for row in notices))
        self.assertEqual(
            action.kind, controller.ActionKind.START_TURN)
        self.assertIn("异构 workflow wf-12345678", action.item.text)
        self.assertIn("唯一具有写权限", action.item.text)
        self.assertIn("verified synthesis", action.item.text)
        self.assertNotIn("\x1b", action.item.text)
        self.assertNotIn("unsafe-title", action.item.text)
        self.assertEqual(manager.apply_marks, ["queued", "dispatched"])
        sess.close()

    def test_workflow_report_ui_and_apply_hide_reasoning(self):
        sess, manager = self.make_session()
        record = workflow_record("completed")
        record["result"] = (
            "<think>private UI reasoning</think>\n"
            "VISIBLE_SYNTHESIS")
        manager.record = dict(record)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            CLI._print_workflow(record)
        self.assertTrue(sess.queue_workflow_apply(record, manual=True))
        prompt = sess._workflow_apply_queue[-1]["prompt"]

        for projected in (output.getvalue(), prompt):
            self.assertNotIn("private UI reasoning", projected)
            self.assertIn(CLI.AGENTS.REASONING_HIDDEN_NOTICE, projected)
            self.assertIn("VISIBLE_SYNTHESIS", projected)
        sess.close()

    def test_adaptive_handoff_describes_actual_shape_without_overclaiming(self):
        sess, manager = self.make_session()
        manager.record = workflow_record("completed")
        manager.record.update({
            "trigger": "model",
            "plan": {
                "agents": [
                    {"key": "code", "seat": "Qwen"},
                    {"key": "tests", "seat": "GLM"},
                ],
            },
            "review_rounds_completed": 0,
            "synthesis": None,
        })

        self.assertTrue(sess.queue_workflow_apply(manager.record))
        prompt = sess._workflow_apply_queue[0]["prompt"]

        self.assertIn(
            "2 个异构只读 DAG 节点所形成的有界汇总报告", prompt)
        self.assertNotIn("五家旗舰模型", prompt)
        self.assertNotIn("经交叉审查", prompt)
        sess.close()

    def test_launch_animation_uses_one_scrollback_line_and_reduced_motion(self):
        record = workflow_record("running")
        animated = io.StringIO()
        static = io.StringIO()
        sess = mock.Mock(renderer=None)

        with (
                mock.patch.object(CLI, "_TTY", True),
                mock.patch.object(CLI.time, "sleep") as sleep,
                mock.patch.object(CLI.sys, "stdout", animated),
                mock.patch.dict(
                    os.environ,
                    {"TERM": "xterm-256color", "ZYLAB_MOTION": "1"},
                    clear=False)):
            os.environ.pop("NO_COLOR", None)
            CLI._play_workflow_launch(sess, record)

        with (
                mock.patch.object(CLI, "_TTY", True),
                mock.patch.object(CLI.sys, "stdout", static),
                mock.patch.dict(
                    os.environ,
                    {"TERM": "xterm-256color", "NO_COLOR": "1"},
                    clear=False)):
            CLI._play_workflow_launch(sess, record)

        self.assertTrue(sleep.called)
        self.assertIn("\x1b[2K", animated.getvalue())
        self.assertEqual(animated.getvalue().count("\n"), 1)
        self.assertIn("5 flagship models", animated.getvalue())
        final_frame = animated.getvalue().rsplit("\x1b[2K", 1)[-1]
        self.assertIn("Qwen → GLM → DeepSeek → MiniMax → Kimi", final_frame)
        self.assertNotIn("\x1b[2K", static.getvalue())
        self.assertIn("wf-12345678", static.getvalue())
        self.assertIn(
            "Qwen → GLM → DeepSeek → MiniMax → Kimi",
            static.getvalue())

    def test_active_workflow_dock_projects_progress_dag_and_routes(self):
        record = workflow_record("running")
        record.update({
            "stage": "review-1",
            "requests_started": 6,
            "budget": {"max_requests": 24},
            "plan": {
                "agents": [
                    {"key": "code", "seat": "Qwen",
                     "state": "completed"},
                    {"key": "tests", "seat": "GLM",
                     "state": "completed"},
                ],
                "review": {"enabled": True},
                "synthesis": {"seat": "Kimi"},
            },
            "agents": [
                {"id": "agent-review", "seat": "DeepSeek",
                 "state": "running", "phase": "review"},
            ],
        })

        rows = CLI._workflow_dock_rows(record)

        self.assertEqual(len(rows), 3)
        self.assertIn("wf-12345678", rows[0])
        self.assertIn("agents 1/8", rows[0])
        self.assertIn("req 6/24", rows[0])
        # DAG 是唯一拓扑（批 2）：进度是节点计数，不再是 agents→review→synthesis 三段
        self.assertIn("节点 2/2", rows[1])
        self.assertIn("✓2", rows[1])
        self.assertIn("● DeepSeek@deepinfer", rows[2])
        self.assertEqual(
            CLI._workflow_dock_rows(workflow_record("completed")), ())

    def test_workflow_agent_workspace_is_clickable_and_previews_on_open(self):
        row = {
            "id": "agent-1", "parent_session_id": "workflow-parent",
            "state": "running", "model": "model", "gateway": "boyue",
        }
        calls = []

        class Session:
            def agent_record(self, identifier, refresh=False):
                self.last_refresh = (identifier, refresh)
                return dict(row)

            def pick(self, rows, **kwargs):
                self.picker_rows = rows
                self.picker_kwargs = kwargs
                return "attach", rows[0]

            def attach_agent(self, identifier):
                calls.append(("attach", identifier))
                return {**row, "attached": True}

        sess = Session()
        record = workflow_record("running")
        record["agents"] = [{"id": "agent-1"}]

        with mock.patch.object(
                CLI, "show_agent_preview",
                side_effect=lambda _sess, identifier: calls.append(
                    ("preview", identifier))):
            opened = CLI._open_workflow_agents(sess, record)

        self.assertTrue(opened["attached"])
        self.assertTrue(sess.picker_kwargs["fullscreen"])
        self.assertTrue(sess.picker_kwargs["mouse"])
        self.assertEqual(sess.picker_kwargs["mouse_action"], "attach")
        self.assertEqual(calls, [
            ("preview", "agent-1"), ("attach", "agent-1")])

    def test_status_line_keeps_active_workflow_next_to_composer(self):
        sess, _manager = self.make_session()
        sess._workflow_current = workflow_record("running")

        line = CLI.status_line(sess)

        self.assertIn("wf-12345678", line)
        self.assertIn("scout 1/8", line)
        sess.close()

    def test_auto_command_controls_model_tool_visibility_per_chat(self):
        sess, _manager = self.make_session()
        output = io.StringIO()

        self.assertEqual(
            CLI.CFG.DEFAULTS["permissions"]["workflow"], "ask")
        self.assertEqual(
            CLI.CFG.DEFAULTS["permissions"]["workflow_control"], "allow")
        self.assertNotIn("workflow", sess.allowed_tool_names())
        with (
                mock.patch.object(CLI, "_TTY", False),
                contextlib.redirect_stdout(output)):
            CLI.cmd_workflow(sess, "auto on")

        self.assertTrue(sess.workflow_auto)
        self.assertIn("workflow", sess.allowed_tool_names())
        self.assertIn("Adaptive workflow  ON", output.getvalue())
        self.assertIn("parallel≤3", output.getvalue())
        self.assertIn("per-agent≤5", output.getvalue())
        self.assertIn("attempts≤24", output.getvalue())

        with contextlib.redirect_stdout(io.StringIO()):
            CLI.cmd_workflow(sess, "auto off")
        self.assertFalse(sess.workflow_auto)
        self.assertNotIn("workflow", sess.allowed_tool_names())
        sess.close()

    def test_model_workflow_tool_is_async_and_idempotent_within_turn(self):
        sess, manager = self.make_session()
        sess.workflow_auto = True
        sess._workflow_auto_override = True
        execution = tools.ExecutionContext.capture(
            session=sess.ag.session_id,
            turn_id="turn-adaptive",
            model=sess.ag.model,
            gateway=sess.ag.gateway,
            hook_config=sess.cfg,
        )
        agents_spec = [
            {"key": "code", "seat": "Qwen", "task": "inspect code"},
            {"key": "tests", "seat": "GLM", "task": "inspect tests"},
        ]

        with self.assertRaisesRegex(
                CLI.WORKFLOWS.WorkflowError, "只能使用旗舰 seat"):
            sess._start_workflow_from_tool({
                "goal": "bypass lineup",
                "agents": [
                    {"task": "a", "model": "cheap-model",
                     "gateway": "boyue"},
                    {"task": "b", "seat": "GLM"},
                ],
            }, execution_context=execution)
        self.assertEqual(manager.plan_starts, [])

        task = mock.Mock()
        task.runtime_event.return_value = True
        result = tools.t_workflow(
            "audit parser",
            agents_spec,
            budget=3,
            _task=task,
            _execution_context=execution,
        )
        repeated = sess._start_workflow_from_tool({
            "goal": "audit parser",
            "agents": agents_spec,
            "budget": 3,
        }, execution_context=execution)

        self.assertEqual(len(manager.plan_starts), 1)
        self.assertEqual(repeated["id"], manager.record["id"])
        self.assertIn("started asynchronously", result)
        # 2026-09-04：不再把模型触发的 DAG 夹到 3 个节点；节点数只受 max_nodes 与预算约束
        self.assertEqual(
            manager.plan_starts[0][2]["limits"]["max_nodes"], 16)
        self.assertEqual(
            manager.plan_starts[0][2]["limits"]["max_agents"], 5)
        self.assertEqual(
            manager.plan_starts[0][2]["limits"]["max_requests"], 24)
        self.assertEqual(
            manager.plan_starts[0][2]["limits"]["max_requests_per_agent"], 5)
        runtime_event = task.runtime_event.call_args.args[0]
        self.assertEqual(runtime_event["kind"], "workflow_started")
        self.assertEqual(runtime_event["payload"]["trigger"], "model")
        sess.close()

    def test_model_workflow_control_is_session_bound_and_non_destructive(self):
        sess, manager = self.make_session()
        sess.workflow_auto = True
        manager.record = workflow_record("running")
        sess._workflow_current = dict(manager.record)
        execution = tools.ExecutionContext.capture(
            session=sess.ag.session_id, turn_id="turn-control",
            model=sess.ag.model, gateway=sess.ag.gateway,
            hook_config=sess.cfg)

        status = sess._control_workflow_from_tool(
            {"action": "status"}, execution_context=execution)
        self.assertEqual(status["state"], "running")
        added = sess._control_workflow_from_tool({
            "action": "add",
            "nodes": [{"key": "security", "seat": "Qwen",
                       "task": "inspect boundary"}],
        }, execution_context=execution)
        self.assertEqual(added["workflow_id"], manager.record["id"])
        self.assertEqual(manager.record["added_by"], "main-agent")
        sent = sess._control_workflow_from_tool({
            "action": "send", "node": "security", "message": "new clue",
        }, execution_context=execution)
        self.assertEqual(sent["message_id"], "msg-1")

        with self.assertRaisesRegex(
                CLI.WORKFLOWS.WorkflowError, "只能是 status/add/send"):
            sess._control_workflow_from_tool(
                {"action": "cancel"}, execution_context=execution)
        with self.assertRaisesRegex(
                CLI.WORKFLOWS.WorkflowError, "只能选择旗舰 seat"):
            sess._control_workflow_from_tool({
                "action": "add",
                "nodes": [{"key": "bad", "task": "bad",
                           "model": "cheap", "gateway": "other"}],
            }, execution_context=execution)
        with self.assertRaisesRegex(
                CLI.WORKFLOWS.WorkflowError, "每次最多追加 2 个"):
            sess._control_workflow_from_tool({
                "action": "add",
                "nodes": [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b"},
                    {"key": "c", "seat": "GPT", "task": "c"},
                ],
            }, execution_context=execution)
        foreign = tools.ExecutionContext.capture(
            session="another-session", turn_id="turn-control",
            model=sess.ag.model, gateway=sess.ag.gateway,
            hook_config=sess.cfg)
        with self.assertRaisesRegex(
                CLI.WORKFLOWS.WorkflowError, "另一个 session"):
            sess._control_workflow_from_tool(
                {"action": "status"}, execution_context=foreign)
        sess.close()

    def test_workflow_tool_schema_is_a_free_dag_over_four_seats(self):
        schema = next(row for row in tools.SCHEMA
                      if row["function"]["name"] == "workflow")
        properties = schema["function"]["parameters"]["properties"]
        budget = properties["budget"]
        self.assertEqual(budget["maximum"], 48)
        self.assertEqual(properties["agents"]["minItems"], 2)
        self.assertEqual(properties["agents"]["maxItems"], 16)
        seats = properties["agents"]["items"]["properties"]["seat"]["enum"]
        self.assertEqual(set(seats), {"Kimi", "GLM", "DeepSeek", "Qwen"})
        self.assertNotIn("review", properties)
        self.assertNotIn("synthesis", properties)
        control = next(
            row for row in tools.SCHEMA
            if row["function"]["name"] == "workflow_control")
        control_nodes = control["function"]["parameters"]["properties"]["nodes"]
        self.assertEqual(control_nodes["maxItems"], 2)

    def test_updated_plan_renders_once_and_stays_in_footer(self):
        sess, _manager = self.make_session()
        record, changed = sess.update_task_plan({
            "explanation": "implementation started",
            "items": [
                {"content": "audit", "status": "completed"},
                {"content": "implement recovery", "status": "in_progress"},
            ],
        })

        rendered = CLI._render_updated_plan(record)
        footer = CLI.status_line(sess)
        self.assertTrue(changed)
        self.assertIn("Updated Plan", rendered)
        self.assertIn("✓ audit", rendered)
        self.assertIn("◩ implement recovery", rendered)
        self.assertIn("plan 1/2 implement recovery", footer)
        sess.close()


if __name__ == "__main__":
    unittest.main()
