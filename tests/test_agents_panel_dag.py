"""/agents 面板的 DAG 视图（用户 2026-09-04 点头）：头行 + 按依赖层级的节点行 + 动作 + 刷新。"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from core import tui

RECORD = {
    "id": "wf-1", "state": "running", "stage": "agents", "requests_started": 3,
    "budget": {"max_requests": 24}, "started_at": "2026-09-04T00:00:00+00:00",
    "preflight": [{"seat": "Kimi", "gateway": "deepinfer", "model": "kimi-k3",
                   "ok": True, "replaced_by": None}],
    "plan": {"agents": [
        {"key": "a", "role": "scout", "seat": "Kimi", "state": "completed",
         "agent_id": "ag-a", "depends_on": []},
        {"key": "b", "role": "scout", "seat": "GLM", "state": "running",
         "agent_id": "ag-b", "depends_on": []},
        {"key": "review_r1_deepseek", "role": "review", "seat": "DeepSeek",
         "state": "running", "agent_id": "ag-r", "depends_on": ["a", "b"]},
        {"key": "final", "role": "synthesis", "seat": "Qwen", "state": "queued",
         "agent_id": "", "depends_on": ["a", "b", "review_r1_deepseek"],
         "task": "汇总全部报告", "model": "qwen3.8-max", "gateway": "boyue"},
    ]},
}
CHILDREN = [
    {"id": "ag-r", "state": "running", "name": "DeepSeek reviewer", "model": "deepseek-v4-pro", "gateway": "deepinfer", "updated_at": ""},
    {"id": "ag-b", "state": "running", "name": "GLM scout", "model": "glm-5.3", "gateway": "deepinfer", "updated_at": ""},
    {"id": "ag-x", "state": "completed", "name": "unrelated child", "model": "kimi-k3", "gateway": "deepinfer", "updated_at": ""},
    {"id": "ag-a", "state": "completed", "name": "Kimi scout", "model": "kimi-k3", "gateway": "deepinfer", "updated_at": ""},
]


def make_sess(record=RECORD, children=CHILDREN):
    sess = mock.Mock()
    sess.ag.session_id = "s1"
    sess.workflow_manager.current.return_value = record
    sess.list_agents.return_value = [dict(row) for row in children]
    sess.renderer = None
    return sess


class PanelRowsTests(unittest.TestCase):
    def test_rows_are_header_then_nodes_by_dependency_level_then_others(self):
        rows = zylab._agent_panel_rows(make_sess())
        self.assertEqual(rows[0]["kind"], "workflow")
        ids = [row["id"] for row in rows[1:]]
        self.assertEqual(ids, ["ag-a", "ag-b", "ag-r", "node:wf-1:final", "ag-x"])
        levels = [row["node"]["level"] for row in rows[1:4]]
        self.assertEqual(levels, [0, 0, 1])
        self.assertEqual(rows[4]["node"]["level"], 2)
        self.assertEqual(rows[4]["kind"], "workflow-node")

    def test_rendered_rows_show_dag_facts(self):
        rows = zylab._agent_panel_rows(make_sess())
        header = zylab._agent_panel_row(rows[0])
        self.assertIn("wf-1", header)
        self.assertIn("节点 1/4", header)
        self.assertIn("req 3/24", header)
        self.assertIn("探活 ✓", header)
        review = zylab._agent_panel_row(rows[3])
        self.assertIn("⚖ review_r1_deepseek", review)
        self.assertIn("← a,b", review)
        final = zylab._agent_panel_row(rows[4])
        self.assertIn("◆ final", final)
        self.assertIn("—", final, "未启动节点没有 agent id")
        plain = zylab._agent_panel_row(rows[5])
        self.assertIn("unrelated child", plain)
        self.assertNotIn("←", plain)

    def test_preflight_reroute_is_visible_in_header(self):
        record = dict(RECORD)
        record["preflight"] = [{"seat": "GLM", "gateway": "deepinfer", "model": "glm-5.3",
                                "ok": False, "replaced_by": "glm-5.3@boyue"}]
        rows = zylab._agent_panel_rows(make_sess(record))
        self.assertIn("探活换路 GLM→glm-5.3@boyue", zylab._agent_panel_row(rows[0]))

    def test_no_workflow_keeps_plain_rows(self):
        sess = make_sess(record=None)
        rows = zylab._agent_panel_rows(sess)
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in CHILDREN])


class PanelActionsTests(unittest.TestCase):
    def test_header_actions_route_to_workflow_commands(self):
        sess = make_sess()
        rows = zylab._agent_panel_rows(sess)
        picks = iter([("pause", rows[0], {}), ("attach", rows[0], {}), ("attach", rows[4], {}), (None, None, {})])
        sess.pick = mock.Mock(side_effect=lambda *a, **k: next(picks))
        out = io.StringIO()
        with mock.patch.object(zylab, "cmd_workflow") as cmd, \
             mock.patch.object(sys, "stdout", out):
            result = zylab.open_agents_panel(sess)
        self.assertIsNone(result)
        self.assertEqual([c.args[1] for c in cmd.call_args_list], ["pause wf-1", "console wf-1"])
        self.assertIn("尚未启动", out.getvalue())
        kwargs = sess.pick.call_args_list[0].kwargs
        self.assertEqual(kwargs["actions"]["p"], "pause")
        self.assertEqual(kwargs["actions"]["x"], "cancel")
        self.assertTrue(kwargs["explicit_filter"], "有快捷键时筛选要按 / 显式进入")
        self.assertIsNotNone(kwargs["refresh"])
        self.assertEqual(kwargs["row_key"](rows[0]), "workflow:wf-1")

    def test_plain_children_keep_the_old_panel_contract(self):
        sess = make_sess(record=None)
        rows = zylab._agent_panel_rows(sess)
        sess.pick = mock.Mock(return_value=("attach", rows[0], {}))
        sess.attach_agent.return_value = {"id": rows[0]["id"]}
        with mock.patch.object(zylab, "show_agent_preview"):
            record = zylab.open_agents_panel(sess)
        self.assertEqual(record["id"], rows[0]["id"])
        kwargs = sess.pick.call_args.kwargs
        self.assertEqual(set(kwargs["actions"]), {"enter", "space"})
        self.assertFalse(kwargs["explicit_filter"])
        self.assertIsNone(kwargs["refresh"])


class PickerRefreshTests(unittest.TestCase):
    def test_replace_picker_rows_keeps_cursor_by_key_and_filter(self):
        pump = tui.InputPump(stream=io.StringIO())
        rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        pump.open_picker(rows, render=lambda r: r["id"], row_key=lambda r: r["id"])
        pump._picker["index"] = 1                       # 光标在 b
        pump._picker["filter"] = ""
        self.assertTrue(pump.replace_picker_rows([{"id": "c"}, {"id": "b"}, {"id": "a"}, {"id": "d"}]))
        self.assertEqual(pump._picker["rows"][pump._picker["index"]]["id"], "b")
        pump._picker["filter"] = "d"
        self.assertTrue(pump.replace_picker_rows([{"id": "d"}, {"id": "b"}]))
        self.assertEqual(pump._picker["filter"], "d")
        self.assertEqual(pump._picker_view(), [{"id": "d"}])
        pump._close_picker(None)
        self.assertFalse(pump.replace_picker_rows(rows), "没有 picker 时返回 False")

    def test_session_pick_refreshes_rows_while_open(self):
        sess = object.__new__(zylab.Session)
        sess.renderer = mock.Mock()
        sess.renderer.mouse_enabled = True
        sess.heartbeat_lease = mock.Mock()
        sess._deferred_pump_events = []
        events = iter([None, None, tui.PumpEvent("picker_result", value={"id": "b"}, mode=None, state={})])
        pump = mock.Mock()
        pump.get = mock.Mock(side_effect=lambda _t: next(events))
        sess.pump = pump
        versions = iter([[{"id": "a"}, {"id": "b"}], [{"id": "b"}]])
        picked = sess.pick([{"id": "a"}], refresh=lambda: next(versions), refresh_interval=0.0)
        self.assertEqual(picked, {"id": "b"})
        replaced = [c.args[0] for c in pump.replace_picker_rows.call_args_list]
        self.assertEqual(replaced, [[{"id": "a"}, {"id": "b"}], [{"id": "b"}]])


if __name__ == "__main__":
    unittest.main()
