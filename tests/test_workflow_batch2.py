"""批 2：DAG 是唯一拓扑；观测并入 /agents；配方给模型看；/gateway 并入 /model。"""
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from core import tools, workflows


class ExpandPlanTests(unittest.TestCase):
    BASE = [
        {"key": "a", "seat": "Kimi", "task": "调研 A"},
        {"key": "b", "seat": "GLM", "task": "调研 B"},
    ]

    def test_reviewers_and_synthesis_become_dependent_nodes(self):
        nodes = workflows.expand_plan_agents(
            self.BASE, review={"enabled": True, "reviewers": ["DeepSeek", "Qwen"], "rounds": 2},
            synthesis={"seat": "Kimi"}, goal="修 parser")
        by_key = {n["key"]: n for n in nodes}
        self.assertEqual(list(by_key)[:2], ["a", "b"])
        r1 = ["review_r1_deepseek", "review_r1_qwen"]
        r2 = ["review_r2_deepseek", "review_r2_qwen"]
        for key in r1:
            self.assertEqual(by_key[key]["role"], "review")
            self.assertEqual(by_key[key]["depends_on"], ["a", "b"])
        for key in r2:
            self.assertEqual(set(by_key[key]["depends_on"]), {"a", "b", *r1})
        self.assertEqual(by_key["final"]["role"], "synthesis")
        self.assertEqual(set(by_key["final"]["depends_on"]), {"a", "b", *r1, *r2})
        self.assertIn("修 parser", by_key["final"]["task"])
        self.assertTrue(all(workflows._NODE_KEY.fullmatch(k) for k in by_key))

    def test_custom_route_reviewer_and_key_collisions(self):
        base = self.BASE + [{"key": "final", "seat": "Qwen", "task": "已有 final"}]
        nodes = workflows.expand_plan_agents(
            base, review={"enabled": True, "reviewers": [{"model": "x-model", "gateway": "boyue"}]},
            synthesis={"seat": "GLM"})
        keys = [n["key"] for n in nodes]
        self.assertIn("review_r1_xmodel", keys)
        self.assertEqual(nodes[-1]["key"], "final2", "与用户节点撞名要让位")
        review = next(n for n in nodes if n["key"] == "review_r1_xmodel")
        self.assertEqual((review["model"], review["gateway"]), ("x-model", "boyue"))

    def test_no_review_no_synthesis_is_identity(self):
        self.assertEqual(workflows.expand_plan_agents(self.BASE), self.BASE)
        self.assertEqual(workflows.expand_plan_agents(self.BASE, review={"enabled": False}), self.BASE)

    def test_wave_path_is_gone(self):
        for name in ("PRESETS", "MODES", "SEAT_FOCUS", "SCOUT_ORDER"):
            self.assertFalse(hasattr(workflows, name), name)
        self.assertFalse(hasattr(workflows.WorkflowManager, "start"))
        self.assertFalse(hasattr(workflows.WorkflowManager, "_run_wave"))
        self.assertEqual(workflows.MODE_NAMES, ("implement", "review", "research", "debug"))


class RecipeToolTests(unittest.TestCase):
    def test_list_and_show_expand_recipes_for_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = SimpleNamespace(workspace_root=tmp)
            listed = json.loads(tools.t_workflow_recipe("list", _execution_context=ctx))
            names = {row["name"] for row in listed["recipes"]}
            self.assertIn("research-review", names)
            params = next(row for row in listed["recipes"]
                          if row["name"] == "research-review")["params"]
            shown = json.loads(tools.t_workflow_recipe(
                "show", name="research-review",
                values={name: "解析器" for name in params},
                _execution_context=ctx))
        keys = [agent["key"] for agent in shown["agents"]]
        self.assertTrue(any(k.startswith("review_r1_") for k in keys), keys)
        self.assertIn("final", keys)
        final = next(a for a in shown["agents"] if a["key"] == "final")
        self.assertEqual(final["role"], "synthesis")
        self.assertTrue(set(final["depends_on"]) >= {k for k in keys if k != "final"})
        self.assertIn("workflow_recipe", tools.READ_ONLY)
        self.assertIs(tools.IMPL["workflow_recipe"], tools.t_workflow_recipe)
        schema = next(row for row in tools.SCHEMA if row["function"]["name"] == "workflow_recipe")
        self.assertEqual(schema["function"]["parameters"]["properties"]["action"]["enum"], ["list", "show"])

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            tools.t_workflow_recipe("run")


class DockAndAgentsTests(unittest.TestCase):
    def test_dock_counts_nodes_instead_of_phases(self):
        record = {
            "id": "wf-abc", "state": "running", "stage": "agents",
            "completed_agents": 1, "expected_agents": 4,
            "budget": {"max_requests": 24}, "requests_started": 3,
            "plan": {"agents": [
                {"key": "a", "seat": "Kimi", "state": "completed"},
                {"key": "b", "seat": "GLM", "state": "running"},
                {"key": "c", "seat": "DeepSeek", "state": "queued"},
                {"key": "d", "seat": "Qwen", "state": "failed"},
            ]},
            "lineup": [{"seat": "Kimi", "gateway": "deepinfer"}],
        }
        rows = zylab._workflow_dock_rows(record)
        # J7：running 节点在 Seats 行下各占一条名册行
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[3].startswith("○ GLM  b"), rows[3])
        self.assertIn("节点 1/4", rows[1])
        for mark in ("✓1", "●1", "○1", "!1"):
            self.assertIn(mark, rows[1])
        self.assertNotIn("synthesis", rows[1])
        record["stage"] = "preflight"
        self.assertIn("探活中", zylab._workflow_dock_rows(record)[1])

    def test_agents_lists_workflow_summary_and_routes_workflow_subcommands(self):
        sess = mock.Mock()
        sess.ag.session_id = "s1"
        sess.workflow_manager.current.return_value = {
            "id": "wf-1", "state": "running", "requests_started": 2,
            "budget": {"max_requests": 24},
            "plan": {"agents": [{"state": "completed"}, {"state": "running"}]}}
        sess.list_agents.return_value = []
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            zylab.print_agents(sess)
        text = out.getvalue()
        self.assertIn("◆ workflow wf-1", text)
        self.assertIn("节点 1/2", text)
        with mock.patch.object(zylab, "cmd_workflow") as cmd:
            zylab.cmd_agents(sess, "workflow status wf-1")
            cmd.assert_called_once_with(sess, "status wf-1")
            cmd.reset_mock()
            zylab.cmd_agents(sess, "workflow")
            cmd.assert_called_once_with(sess, "status")
            cmd.reset_mock()
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                zylab.cmd_agents(sess, "workflow add [1]")
            cmd.assert_not_called()
            self.assertIn("用法", out.getvalue())


class ModelGatewayMergeTests(unittest.TestCase):
    def make_session(self):
        sess = mock.Mock()
        sess.route_selection.return_value = ("kimi-k3", "deepinfer")
        sess.request_route_change.return_value = {"staged": False, "context": None}
        return sess

    def test_model_at_gateway_selects_the_route_explicitly(self):
        sess = self.make_session()
        out = io.StringIO()
        with mock.patch.object(zylab.M, "where", return_value=["deepinfer", "boyue"]), \
             mock.patch.object(zylab.M, "get", return_value={"supports_tools": True}), \
             mock.patch.object(sys, "stdout", out):
            zylab.cmd_model(sess, "glm-5.3@boyue")
        sess.request_route_change.assert_called_once_with("glm-5.3", "boyue")
        self.assertIn("glm-5.3@boyue", out.getvalue())

    def test_unknown_gateway_is_rejected_without_switching(self):
        sess = self.make_session()
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            zylab.cmd_model(sess, "glm-5.3@nowhere")
        sess.request_route_change.assert_not_called()
        self.assertIn("未知网关", out.getvalue())

    def test_model_gateway_subcommand_delegates_and_alias_is_hidden(self):
        sess = self.make_session()
        with mock.patch.object(zylab, "cmd_gateway") as gateway:
            zylab.cmd_model(sess, "gateway boyue")
            gateway.assert_called_once_with(sess, "boyue", quiet=True)
        self.assertIn("gateway", zylab.HIDDEN_COMPAT_COMMANDS)
        self.assertNotIn("/gateway", zylab.COMMANDS)
        self.assertIn("/model", zylab.COMMANDS)


if __name__ == "__main__":
    unittest.main()
