"""派子代理前的「选模型」决策门（用户 2026-09-24：「zylab 如果要启用 subagent，会弹出来
决策门让 user 选择模型，比如 agent1 任务 XXX，你想选择 XX model」）。

以前所有子代理一律沿用主会话当前的模型，中间没有任何人能插手的地方。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agents, subagent, tools

SEATS = [
    {"gateway": "boyue", "id": "kimi-k3", "seat": "Kimi"},
    {"gateway": "boyue", "id": "glm-5.3", "seat": "GLM"},
    {"gateway": "boyue", "id": "deepseek-v4-pro", "seat": "DeepSeek"},
    {"gateway": "boyue", "id": "qwen3.8-max", "seat": "Qwen"},
]


def execution(role="main"):
    return tools.ExecutionContext.capture(
        session="parent-session", turn_id="parent-turn",
        model="gpt-6-sol", gateway="boyue",
        permission_mode="default", interaction_role=role,
        hook_config={"safe": True})


class FakeTask:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.cancel_event = None
        self.key = "call-1"

    def request_interaction(self, kind, payload):
        self.requests.append((kind, payload))
        return self.answer(payload) if callable(self.answer) else self.answer

    def runtime_event(self, event):
        pass

    def note(self, text):
        pass


class SubagentModelGate(unittest.TestCase):
    def setUp(self):
        self.hook = mock.patch.dict(tools.HOOK_CTX, {
            "cfg": {}, "session_auto": lambda: False, "context_capsule": None})
        self.hook.start()
        self.addCleanup(self.hook.stop)
        seats = mock.patch("core.models.workflow_default_rows", return_value=list(SEATS))
        seats.start()
        self.addCleanup(seats.stop)
        self.batch = mock.patch.object(subagent, "run_batch", return_value="batch-report")
        self.single = mock.patch.object(subagent, "run", return_value="single-report")
        self.run_batch = self.batch.start()
        self.run_single = self.single.start()
        self.addCleanup(self.batch.stop)
        self.addCleanup(self.single.stop)

    TASKS = [{"name": "查资料", "task": "查北非网格的投影参数"},
             {"name": "读代码", "task": "读 TheaterMapBuilder 的投影模块"}]

    def spawn(self, answer, *, tasks=TASKS, role="main"):
        task = FakeTask(answer)
        result = tools.t_subagent(tasks=[dict(t) for t in tasks], _task=task,
                                  _execution_context=execution(role))
        return task, result

    def test_each_subagent_gets_its_own_question(self):
        task, _ = self.spawn({"status": "resolved", "attended": True,
                              "answers": {"agent1": "kimi-k3",
                                          "agent2": "gpt-6-sol（当前模型）"}})
        kind, payload = task.requests[0]
        self.assertEqual(kind, "decision_gate")
        questions = payload["questions"]
        self.assertEqual([q["id"] for q in questions], ["agent1", "agent2"])
        self.assertIn("查北非网格的投影参数", questions[0]["question"])
        self.assertIn("用哪个模型", questions[0]["question"])

    def test_the_current_model_comes_first_as_the_recommendation(self):
        task, _ = self.spawn({"status": "cancelled"})
        options = task.requests[0][1]["questions"][0]["options"]
        self.assertEqual(options[0]["label"], "gpt-6-sol（当前模型）")
        self.assertTrue(options[0].get("recommended"))
        self.assertLessEqual(len(options), 4, "决策门每题最多 4 个选项")
        self.assertEqual([o["label"] for o in options[1:]],
                         ["kimi-k3", "glm-5.3", "deepseek-v4-pro"])

    def test_each_subagent_runs_on_the_model_chosen_for_it(self):
        self.spawn({"status": "resolved", "attended": True,
                    "answers": {"agent1": "kimi-k3", "agent2": "gpt-6-sol（当前模型）"}})
        sent = self.run_batch.call_args.args[0]
        self.assertEqual((sent[0]["_model"], sent[0]["_gateway"]), ("kimi-k3", "boyue"))
        self.assertEqual((sent[1]["_model"], sent[1]["_gateway"]), ("gpt-6-sol", "boyue"))

    def test_a_single_subagent_is_asked_too(self):
        task = FakeTask({"status": "resolved", "attended": True,
                         "answers": {"agent1": "glm-5.3"}})
        tools.t_subagent(task="查一个东西", _task=task, _execution_context=execution())
        kwargs = self.run_single.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["gateway"]), ("glm-5.3", "boyue"))

    def test_escape_cancels_the_dispatch_and_says_so(self):
        _, result = self.spawn({"status": "cancelled"})
        self.run_batch.assert_not_called()
        self.assertIn("取消", str(result))

    def test_no_answer_keeps_the_current_model(self):
        """非交互运行（-p）时桥会回「无人应答」：照旧用当前模型，不报错也不卡住。"""
        _, result = self.spawn({"status": "unattended"})
        sent = self.run_batch.call_args.args[0]
        self.assertNotIn("_model", sent[0])
        self.assertEqual(self.run_batch.call_args.kwargs["model"], "gpt-6-sol")
        self.assertEqual(result, "batch-report")

    def test_auto_mode_does_not_ask(self):
        tools.HOOK_CTX["session_auto"] = lambda: True
        task, _ = self.spawn({"status": "cancelled"})
        self.assertEqual(task.requests, [])
        self.run_batch.assert_called_once()

    def test_the_setting_turns_it_off(self):
        tools.HOOK_CTX["cfg"] = {"subagent_model_gate": False}
        task, _ = self.spawn({"status": "cancelled"})
        self.assertEqual(task.requests, [])

    def test_a_child_spawning_its_own_helpers_does_not_ask(self):
        task, _ = self.spawn({"status": "cancelled"}, role="subagent")
        self.assertEqual(task.requests, [])
        self.run_batch.assert_called_once()


class ChosenModelsReachTheChildren(unittest.TestCase):
    """不只是参数传对了：真正跑起来的子代理用的就是选中的模型。"""

    def test_run_batch_spawns_each_child_on_its_own_route(self):
        from tests.test_agents import FakeAgent, execution as parent_execution
        FakeAgent.instances = []
        tasks = [{"task": "one", "_model": "kimi-k3", "_gateway": "boyue"},
                 {"task": "two"}]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = agents.AgentWorkspace(
                root=Path(tmp) / "runs", agent_factory=FakeAgent, poll_interval=0.005)
            try:
                subagent.run_batch(tasks, model="gpt-6-sol", gateway="boyue",
                                   workspace=workspace,
                                   execution_context=parent_execution())
            finally:
                workspace.close()
        models = sorted(agent.model for agent in FakeAgent.instances)
        self.assertEqual(models, ["gpt-6-sol", "kimi-k3"])


if __name__ == "__main__":
    unittest.main()
