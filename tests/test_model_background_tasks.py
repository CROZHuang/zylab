"""模型能自己把命令扔到后台并观察它 —— 引擎一直都在，缺的是工具入口。

用户 2026-09-08 的判断：「cc 的架构是为模型服务的，只要不是逻辑上的限制，模型
自己就能调用任何功能」。审计 zylab 后发现四处不是这样，最要命的是后台任务：
用户按 Ctrl+B 就能把命令扔到后台继续干活（/tasks /task /kill 都有），模型却只能
在前台干等一个二十分钟的命令。TaskManager.run() 的注释里甚至早就写好了
「主线程拿到 started 后转后台再关闭 generator」这条路径。
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tools                                # noqa: E402


class SchemaTests(unittest.TestCase):
    def schema(self, name):
        for item in tools.SCHEMA:
            function = item.get("function") or {}
            if function.get("name") == name:
                return function
        return None

    def test_bash_offers_background_and_says_not_to_wait(self):
        bash = self.schema("bash")
        properties = (bash.get("parameters") or {}).get("properties") or {}
        self.assertIn("background", properties)
        description = properties["background"]["description"]
        self.assertIn("task_status", description)
        self.assertIn("不要在前台干等", description)

    def test_task_status_is_safe_and_registered(self):
        self.assertIsNotNone(self.schema("task_status"))
        self.assertIn("task_status", tools.SAFE)     # 只读观察，不该要写权限
        self.assertIn("task_status", tools.IMPL)


class TaskStatusToolTests(unittest.TestCase):
    def call(self, **kwargs):
        seen = {}

        def callback(payload):
            seen.update(payload)
            return {"ok": True}

        with mock.patch.dict(tools.HOOK_CTX, {"task_status": callback},
                             clear=False):
            raw = tools.t_task_status(**kwargs)
        return seen, json.loads(raw)

    def test_defaults_to_listing_everything(self):
        seen, result = self.call()
        self.assertEqual(seen["action"], "status")
        self.assertEqual(seen["task_id"], "")
        self.assertTrue(result["ok"])

    def test_output_and_stop_require_a_task_id(self):
        for action in ("output", "stop"):
            with self.assertRaises(ValueError):
                self.call(action=action)

    def test_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            self.call(action="delete", task_id="bg1")

    def test_tail_is_clamped(self):
        seen, _ = self.call(task_id="bg1", action="output", tail=10**9)
        self.assertEqual(seen["tail"], tools.MAX_TASK_TAIL)
        seen, _ = self.call(task_id="bg1", action="output", tail=1)
        self.assertEqual(seen["tail"], 200)


class SessionBridgeTests(unittest.TestCase):
    def make_session(self, snapshots=(), tail=None):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        manager = mock.Mock()
        manager.list.return_value = list(snapshots)
        manager.get.side_effect = (
            lambda ident: next(
                (s for s in snapshots if s.as_dict()["id"] == ident), None))
        manager.tail.return_value = tail or {
            "stdout": {"text": "hello", "truncated": False},
            "stderr": {"text": "", "truncated": False}}
        session.task_manager = manager
        return session, manager

    def snapshot(self, task_id="bg000001", status="running"):
        item = {"id": task_id, "name": "bash", "status": status,
                "background": True, "stdout_bytes": 12, "stderr_bytes": 0,
                "returncode": None}
        return mock.Mock(id=task_id, as_dict=lambda item=item: dict(item))

    def test_listing_reports_every_task(self):
        session, _ = self.make_session([self.snapshot(), self.snapshot("bg2")])
        result = session.task_status_for_tool({"action": "status"})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["tasks"][0]["task_id"], "bg000001")

    def test_unknown_task_says_so_instead_of_raising(self):
        session, _ = self.make_session([])
        result = session.task_status_for_tool(
            {"action": "output", "task_id": "nope"})
        self.assertEqual(result["status"], "unknown")
        self.assertIn("没有这个 task", result["error"])

    def test_output_returns_both_streams(self):
        session, _ = self.make_session(
            [self.snapshot()],
            tail={"stdout": {"text": "out", "truncated": True},
                  "stderr": {"text": "err", "truncated": False}})
        result = session.task_status_for_tool(
            {"action": "output", "task_id": "bg000001", "tail": 500})
        self.assertEqual(result["stdout"], "out")
        self.assertTrue(result["stdout_truncated"])
        self.assertEqual(result["stderr"], "err")

    def test_stop_cancels_through_the_manager(self):
        session, manager = self.make_session([self.snapshot()])
        result = session.task_status_for_tool(
            {"action": "stop", "task_id": "bg000001"})
        manager.cancel.assert_called_once_with("bg000001")
        self.assertTrue(result["stopped"])

    def test_model_backgrounding_reuses_the_ctrl_b_path(self):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session.background_active_task = mock.Mock(return_value="snap")
        self.assertEqual(session.background_model_task("bg1"), "snap")
        session.background_active_task.assert_called_once_with()


class AgentBackgroundPathTests(unittest.TestCase):
    """started 事件后立刻转后台并关闭 generator —— worker 继续跑。"""

    def test_both_run_layers_accept_and_forward_the_callback(self):
        import inspect                                # noqa: PLC0415
        from core import agent as A                   # noqa: PLC0415

        for method in (A.Agent.run, A.Agent._run_managed):
            self.assertIn(
                "background_task", method.__code__.co_varnames,
                f"{method.__name__} 不接受 background_task")
        # run() 只是转发到 _run_managed —— 忘了转发的话工具循环永远拿不到它
        self.assertIn(
            "background_task=background_task",
            inspect.getsource(A.Agent.run))

    def test_the_result_tells_the_model_not_to_wait(self):
        import inspect                                # noqa: PLC0415
        from core import agent as A                   # noqa: PLC0415

        source = inspect.getsource(A.Agent._run_managed)
        self.assertIn("后台任务", source)
        self.assertIn("不要在这里等它", source)
        # 关掉 generator 是这条路径的关键：worker 继续跑，主线程不等
        self.assertIn("runner_events.close()", source)


if __name__ == "__main__":
    unittest.main()


class ModelReachabilityTests(unittest.TestCase):
    """审计后补齐的另外三处「用户有入口、模型没有」。

    判据：只要不是逻辑上的限制（同意边界、花钱、会话生命周期），模型自己就该
    够得着。graft_index 例外地不进 SAFE —— build 会写缓存、烧几十秒 CPU，
    值得逐次确认；关键是它有路可走，不是它能免确认。
    """

    def schema(self, name):
        for item in tools.SCHEMA:
            function = item.get("function") or {}
            if function.get("name") == name:
                return function
        return None

    def test_all_three_are_registered(self):
        for name in ("graft_index", "context_status", "expand_output"):
            self.assertIsNotNone(self.schema(name), name)
            self.assertIn(name, tools.IMPL, name)

    def test_only_index_building_needs_confirmation(self):
        self.assertIn("context_status", tools.SAFE)
        self.assertIn("expand_output", tools.SAFE)
        self.assertNotIn("graft_index", tools.SAFE)

    def test_graft_index_refuses_unknown_actions(self):
        with self.assertRaises(ValueError):
            tools.t_graft_index(action="destroy")

    def test_graft_index_passes_the_action_through(self):
        seen = {}

        def fake(action, **kwargs):
            seen["action"] = action
            seen.update(kwargs)
            return "ok"

        with mock.patch.object(tools, "_t_graft", side_effect=fake):
            self.assertEqual(tools.t_graft_index(action="build", path="src"),
                             "ok")
        self.assertEqual(seen["action"], "build")
        self.assertEqual(seen["path"], "src")

    def test_expand_output_validates_its_arguments(self):
        for kwargs in ({"target": " "}, {"target": "x", "page": 0},
                       {"target": "x", "page": "many"}):
            with self.assertRaises(ValueError):
                tools.t_expand_output(**kwargs)

    def test_schema_tells_the_model_not_to_rerun_expensive_commands(self):
        description = self.schema("expand_output")["description"]
        self.assertIn("不要重跑昂贵的命令", description)
        advice = self.schema("context_status")["description"]
        self.assertIn("在做大动作之前问一次", advice)


class ContextStatusBridgeTests(unittest.TestCase):
    def make_session(self, report):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session.ag = mock.Mock()
        session.ag.context_report.return_value = report
        session.ag.ctx_limit = 280_000
        return session

    def test_reports_remaining_budget_and_advice(self):
        session = self.make_session({
            "estimated_request_tokens": 40_000, "usable_budget": 238_000,
            "fits": True, "summary": {"status": "none"},
            "tool_previews": {"saved_chars": 1234}, "omitted_ranges": []})
        result = session.context_status_for_tool()
        self.assertEqual(result["remaining_tokens"], 198_000)
        self.assertEqual(result["percent_used"], 16.8)
        self.assertIn("充足", result["advice"])

    def test_tight_budget_advises_delegating(self):
        session = self.make_session({
            "estimated_request_tokens": 220_000, "usable_budget": 238_000,
            "fits": True, "summary": {"status": "valid"},
            "tool_previews": {"saved_chars": 0},
            "omitted_ranges": [{"start": 1, "end": 9}]})
        result = session.context_status_for_tool()
        self.assertIn("subagent", result["advice"])
        self.assertEqual(result["omitted_ranges"], 1)
        self.assertEqual(result["summary_status"], "valid")


class ExpandBridgeTests(unittest.TestCase):
    def make_session(self, view):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        manager = mock.Mock()
        manager.artifact_page.return_value = view
        session.task_manager = manager
        return session, manager

    def test_sections_are_joined_and_more_pages_are_announced(self):
        session, manager = self.make_session({
            "page": 1, "total_bytes": 900, "has_more": True, "missing": [],
            "sections": [{"stream": "stdout", "text": "out"},
                         {"stream": "stderr", "text": "err"}]})
        result = session.expand_output_for_tool(
            {"target": "call-1", "page": 1})
        manager.artifact_page.assert_called_once_with("call-1", page=1)
        self.assertIn("[stdout]", result["text"])
        self.assertIn("err", result["text"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_page"], 2)

    def test_a_single_stream_is_returned_raw(self):
        session, _ = self.make_session({
            "page": 2, "has_more": False, "missing": [],
            "sections": [{"stream": "stdout", "text": "only"}]})
        result = session.expand_output_for_tool(
            {"target": "call-1", "page": 2})
        self.assertEqual(result["text"], "only")
        self.assertIsNone(result["next_page"])

    def test_missing_artifact_explains_what_a_valid_target_is(self):
        session, _ = self.make_session(None)
        result = session.expand_output_for_tool({"target": "nope"})
        self.assertIn("tool_call_id", result["error"])


class SubagentControlTests(unittest.TestCase):
    """模型派得出去 child，也要能对话 —— 审计里唯一一处能力形态弱于 CC 的地方。

    2026-09-09 审计结论：`/agents` 只是「部分可达」。模型有 subagent 能派生，
    但不能给运行中的 child 追加指令、也不能读它的线程；Claude Code 有 SendMessage。
    """

    def schema(self, name):
        for item in tools.SCHEMA:
            function = item.get("function") or {}
            if function.get("name") == name:
                return function
        return None

    def test_registered_and_safe_but_never_offered_to_children(self):
        from core import agents                        # noqa: PLC0415

        self.assertIsNotNone(self.schema("subagent_control"))
        self.assertIn("subagent_control", tools.IMPL)
        self.assertIn("subagent_control", tools.SAFE)
        # child 只有四个只读文件工具：不可能递归控制别的 agent
        self.assertNotIn("subagent_control", agents.CHILD_TOOLS)

    def test_schema_warns_against_respawning(self):
        description = self.schema("subagent_control")["description"]
        self.assertIn("别为了「再查一点」重新派一个新 child", description)

    def call(self, **kwargs):
        seen = {}

        def callback(payload):
            seen.update(payload)
            return {"ok": True}

        with mock.patch.dict(tools.HOOK_CTX,
                             {"subagent_control": callback}, clear=False):
            raw = tools.t_subagent_control(**kwargs)
        return seen, json.loads(raw)

    def test_defaults_to_listing(self):
        seen, _ = self.call()
        self.assertEqual(seen["action"], "list")

    def test_peek_and_send_need_an_id_and_send_needs_a_body(self):
        with self.assertRaises(ValueError):
            self.call(action="peek")
        with self.assertRaises(ValueError):
            self.call(action="send", agent_id="a1")
        with self.assertRaises(ValueError):
            self.call(action="send", agent_id="a1", message="  ")
        with self.assertRaises(ValueError):
            self.call(action="send", agent_id="a1",
                      message="x" * (tools.MAX_AGENT_MESSAGE + 1))
        with self.assertRaises(ValueError):
            self.call(action="cancel", agent_id="a1")


class SubagentControlBridgeTests(unittest.TestCase):
    def make_session(self, record=None, rows=()):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session.list_agents = mock.Mock(return_value=list(rows))
        session.agent_record = mock.Mock(return_value=record)
        session._assert_agent_parent = mock.Mock()
        session.send_agent = mock.Mock()
        return session

    def record(self, **kwargs):
        base = {"id": "a-111111111111", "name": "抓取元数据",
                "state": "running", "seat": "Kimi", "task": "查 X",
                "result": "报告正文", "error": None}
        base.update(kwargs)
        return base

    def test_list_projects_the_roster(self):
        session = self.make_session(rows=[self.record(), self.record(
            id="a-2", state="completed")])
        result = session.subagent_control_for_tool({"action": "list"})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["agents"][0]["agent_id"], "a-111111111111")

    def test_peek_returns_the_bounded_report(self):
        session = self.make_session(record=self.record())
        result = session.subagent_control_for_tool(
            {"action": "peek", "agent_id": "a-111111111111"})
        self.assertEqual(result["report"], "报告正文")
        self.assertEqual(result["state"], "running")

    def test_send_goes_through_the_same_parent_check(self):
        session = self.make_session(record=self.record())
        result = session.subagent_control_for_tool(
            {"action": "send", "agent_id": "a-111111111111",
             "message": "再查一下 Y"})
        session._assert_agent_parent.assert_called_once()
        session.send_agent.assert_called_once_with(
            "a-111111111111", "再查一下 Y")
        self.assertTrue(result["delivered"])
        self.assertIn("不要在这里空转等它", result["note"])

    def test_someone_elses_child_is_refused_not_crashed(self):
        from core import agents                        # noqa: PLC0415

        session = self.make_session(record=self.record())
        session._assert_agent_parent.side_effect = agents.AgentRuntimeError(
            "不是本会话派出的 child")
        result = session.subagent_control_for_tool(
            {"action": "peek", "agent_id": "a-999"})
        self.assertIn("不是本会话", result["error"])
        session.send_agent.assert_not_called()

    def test_missing_child_reports_an_error(self):
        session = self.make_session(record=None)
        result = session.subagent_control_for_tool(
            {"action": "peek", "agent_id": "nope"})
        self.assertIn("没有这个 child agent", result["error"])
