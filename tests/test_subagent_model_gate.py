"""派子代理前的「选模型」决策门（用户 2026-09-24：「zylab 如果要启用 subagent，会弹出来
决策门让 user 选择模型，比如 agent1 任务 XXX，你想选择 XX model」）。

选项怎么来（同日第二版，用户：「改，之前最早的 workflow 机制非常死板」）：
- 和 /model 同一个来源（偏好家族的最新旗舰 + 小目录里实测能用的），只留实测能调工具的，
  一个模型只列一次（同一个模型挂在几个网关上时取当前网关那条）；
- 决策门里：当前模型（推荐）+ 偏好家族顺序里另两家各一个代表 + 「其他模型…」（打开完整列表）；
- 不再读 workflow 的席位池。第一版读的是那份手写的四席名单：GPT / Claude / 官方 DeepSeek
  永远不出现，同一个 glm-5.3 换个网关就占一格，Qwen 被截掉。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agents, subagent, tools


def row(gateway, model, *, tools_ok=True, status="ok", context=128000):
    return {"gateway": gateway, "id": model, "status": status,
            "supports_tools": tools_ok, "context": context}


# 形状取自 09-24 本机的真实能力表（/model 工作集），外加几条边界：同一个模型挂两个网关、
# 视觉线、不能调工具的、实测失败的、一家谁也没写进代码的公司。
ROWS = [
    row("boyue", "gpt-6-sol", context=400000),
    row("boyue", "gpt-6-astra", context=256000),
    row("boyue", "gpt-6-luna", status="error"),
    row("boyue", "claude-opus-5-5"),
    row("boyue", "claude-sonnet-5"),
    row("boyue", "claude-haiku-5", tools_ok=False),
    row("boyue", "deepseek-v4-pro"),
    row("boyue", "kimi-k3"),
    row("boyue", "glm-5.3"),
    row("boyue", "glm-5v-turbo"),
    row("boyue", "qwen3.8-max"),
    row("boyue", "qwen3.7-plus"),
    row("deepinfer", "glm-5.3"),
    row("deepinfer", "deepseek-v4-flash"),
    row("deepinfer", "kimi-k2.6"),
    row("deepinfer", "minimax-m2.7"),
    row("deepinfer", "acme-7"),
]
OTHER = tools.SUBAGENT_OTHER_LABEL


def execution(role="main", model="glm-5.3", gateway="boyue"):
    return tools.ExecutionContext.capture(
        session="parent-session", turn_id="parent-turn",
        model=model, gateway=gateway,
        permission_mode="default", interaction_role=role,
        hook_config={"safe": True})


class FakeTask:
    """按请求种类作答：decision_gate 与 pick（「其他模型…」的完整列表）各有各的答案。"""

    def __init__(self, gate, pick=None):
        self.answers = {"decision_gate": gate, "pick": pick}
        self.requests = []
        self.cancel_event = None
        self.key = "call-1"

    def request_interaction(self, kind, payload):
        self.requests.append((kind, payload))
        answer = self.answers.get(kind)
        return answer(payload) if callable(answer) else answer

    def runtime_event(self, event):
        pass

    def note(self, text):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.hook = mock.patch.dict(tools.HOOK_CTX, {
            "cfg": {}, "session_auto": lambda: False, "context_capsule": None})
        self.hook.start()
        self.addCleanup(self.hook.stop)
        source = mock.patch("core.models.default_picker_rows",
                            side_effect=lambda **_kw: [dict(r) for r in ROWS])
        source.start()
        self.addCleanup(source.stop)
        # 测试环境里 boyue / deepinfer 没配地址；网关能不能发出去另有用例专门测。
        self.ready = mock.patch("core.client.transport_ready", return_value=True)
        self.ready.start()
        self.addCleanup(self.ready.stop)
        self.batch = mock.patch.object(subagent, "run_batch", return_value="batch-report")
        self.single = mock.patch.object(subagent, "run", return_value="single-report")
        self.run_batch = self.batch.start()
        self.run_single = self.single.start()
        self.addCleanup(self.batch.stop)
        self.addCleanup(self.single.stop)

    TASKS = [{"name": "查资料", "task": "查北非网格的投影参数"},
             {"name": "读代码", "task": "读 TheaterMapBuilder 的投影模块"}]

    def spawn(self, gate, pick=None, *, tasks=TASKS, role="main",
              model="glm-5.3", gateway="boyue"):
        task = FakeTask(gate, pick)
        result = tools.t_subagent(tasks=[dict(t) for t in tasks], _task=task,
                                  _execution_context=execution(role, model, gateway))
        return task, result

    def gate_labels(self, **kwargs):
        task, _ = self.spawn({"status": "cancelled"}, **kwargs)
        return [option["label"]
                for option in task.requests[0][1]["questions"][0]["options"]]


class WhatTheGateOffers(Base):
    def test_the_current_model_comes_first_as_the_recommendation(self):
        task, _ = self.spawn({"status": "cancelled"})
        options = task.requests[0][1]["questions"][0]["options"]
        self.assertEqual(options[0]["label"], "glm-5.3（当前模型）")
        self.assertTrue(options[0].get("recommended"))
        self.assertLessEqual(len(options), 4, "决策门每题最多 4 个选项")

    def test_then_the_next_two_families_you_prefer_and_other_models(self):
        """偏好顺序 gpt、claude、deepseek、kimi、glm、qwen：当前是 glm，就给 gpt 和 claude。"""
        self.assertEqual(self.gate_labels(),
                         ["glm-5.3（当前模型）", "gpt-6-sol", "claude-opus-5-5", OTHER])

    def test_the_current_family_is_skipped(self):
        self.assertEqual(self.gate_labels(model="gpt-6-astra"),
                         ["gpt-6-astra（当前模型）", "claude-opus-5-5",
                          "deepseek-v4-pro", OTHER])

    def test_your_preferred_families_setting_decides_which_two(self):
        tools.HOOK_CTX["cfg"] = {"preferred_families": ["qwen", "kimi", "gpt"]}
        self.assertEqual(self.gate_labels(),
                         ["glm-5.3（当前模型）", "qwen3.8-max", "kimi-k3", OTHER])

    def test_it_no_longer_reads_the_workflow_seat_pool(self):
        with mock.patch("core.models.workflow_default_rows",
                        side_effect=AssertionError("不该再读席位池")):
            self.assertIn("gpt-6-sol", self.gate_labels())

    def test_with_nothing_more_to_offer_there_is_no_other_models_option(self):
        with mock.patch("core.models.default_picker_rows",
                        return_value=[row("boyue", "glm-5.3"), row("boyue", "kimi-k3")]):
            self.assertEqual(self.gate_labels(), ["glm-5.3（当前模型）", "kimi-k3"])

    def test_with_nothing_else_usable_it_does_not_ask(self):
        with mock.patch("core.models.default_picker_rows",
                        return_value=[row("boyue", "glm-5.3")]):
            task, _ = self.spawn({"status": "cancelled"})
        self.assertEqual(task.requests, [])
        self.run_batch.assert_called_once()


class TheFullList(Base):
    """「其他模型…」打开的完整列表：和 /model 同一份，只留能调工具、实测成功的。"""

    def full_list(self, **kwargs):
        task, _ = self.spawn({"status": "resolved", "attended": True,
                              "answers": {"agent1": OTHER, "agent2": OTHER}},
                             {"status": "cancelled"}, **kwargs)
        kind, payload = task.requests[1]
        self.assertEqual(kind, "pick")
        return payload

    def test_other_models_opens_the_full_list_for_that_agent(self):
        payload = self.full_list()
        self.assertIn("agent1", payload["title"])
        self.assertIn("查资料", payload["title"])
        labels = [option["label"] for option in payload["options"]]
        self.assertEqual(labels[0], "glm-5.3", "当前模型排第一")
        self.assertIn("当前模型", payload["options"][0]["detail"])

    def test_every_model_appears_once(self):
        labels = [option["label"] for option in self.full_list()["options"]]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(labels.count("glm-5.3"), 1)

    def test_only_models_that_can_use_tools_and_passed_their_test(self):
        labels = [option["label"] for option in self.full_list()["options"]]
        self.assertNotIn("claude-haiku-5", labels, "不能调工具的子代理用不了")
        self.assertNotIn("gpt-6-luna", labels, "实测失败的不冒充可用")

    def test_a_company_nobody_wrote_down_is_offered_too(self):
        labels = [option["label"] for option in self.full_list()["options"]]
        self.assertIn("acme-7", labels, "新公司不改代码也在列表里")
        self.assertIn("minimax-m2.7", labels)

    def test_your_preferred_families_come_first(self):
        labels = [option["label"] for option in self.full_list()["options"]]
        self.assertLess(labels.index("gpt-6-sol"), labels.index("minimax-m2.7"))
        self.assertLess(labels.index("claude-opus-5-5"), labels.index("acme-7"))


class OnlyGatewaysTheChildCanReach(Base):
    """子代理在后台线程里发请求，传输守卫不会替它弹确认框：发不出去的网关不列。"""

    def test_a_gateway_the_background_cannot_reach_is_not_offered(self):
        with mock.patch("core.client.transport_ready",
                        side_effect=lambda route: route.name != "deepinfer"):
            task, _ = self.spawn({"status": "resolved", "attended": True,
                                  "answers": {"agent1": OTHER, "agent2": OTHER}},
                                 {"status": "cancelled"})
        labels = [option["label"] for option in task.requests[1][1]["options"]]
        self.assertNotIn("minimax-m2.7", labels)
        self.assertNotIn("acme-7", labels)

    def test_the_parents_own_gateway_is_always_offered(self):
        """父会话的网关刚在主线程上预检过：哪怕检查说不行也照列（本会话临时放行的 http）。"""
        with mock.patch("core.client.transport_ready", return_value=False):
            labels = self.gate_labels()
        self.assertEqual(labels[:3], ["glm-5.3（当前模型）", "gpt-6-sol", "claude-opus-5-5"])


class WhereTheChildrenRun(Base):
    def test_each_subagent_gets_its_own_question(self):
        task, _ = self.spawn({"status": "resolved", "attended": True,
                              "answers": {"agent1": "gpt-6-sol",
                                          "agent2": "glm-5.3（当前模型）"}})
        kind, payload = task.requests[0]
        self.assertEqual(kind, "decision_gate")
        questions = payload["questions"]
        self.assertEqual([q["id"] for q in questions], ["agent1", "agent2"])
        self.assertIn("查北非网格的投影参数", questions[0]["question"])
        self.assertIn("用哪个模型", questions[0]["question"])

    def test_each_subagent_runs_on_the_model_chosen_for_it(self):
        self.spawn({"status": "resolved", "attended": True,
                    "answers": {"agent1": "claude-opus-5-5",
                                "agent2": "glm-5.3（当前模型）"}})
        sent = self.run_batch.call_args.args[0]
        self.assertEqual((sent[0]["_model"], sent[0]["_gateway"]),
                         ("claude-opus-5-5", "boyue"))
        self.assertEqual((sent[1]["_model"], sent[1]["_gateway"]), ("glm-5.3", "boyue"))

    def test_a_model_picked_from_the_full_list_is_the_one_that_runs(self):
        self.spawn({"status": "resolved", "attended": True,
                    "answers": {"agent1": OTHER, "agent2": "glm-5.3（当前模型）"}},
                   {"status": "resolved", "attended": True, "choice": "minimax-m2.7"})
        sent = self.run_batch.call_args.args[0]
        self.assertEqual((sent[0]["_model"], sent[0]["_gateway"]),
                         ("minimax-m2.7", "deepinfer"))

    def test_escape_in_the_full_list_cancels_the_dispatch(self):
        _, result = self.spawn({"status": "resolved", "attended": True,
                                "answers": {"agent1": OTHER, "agent2": OTHER}},
                               {"status": "cancelled"})
        self.run_batch.assert_not_called()
        self.assertIn("取消", str(result))

    def test_a_single_subagent_is_asked_too(self):
        task = FakeTask({"status": "resolved", "attended": True,
                         "answers": {"agent1": "gpt-6-sol"}})
        tools.t_subagent(task="查一个东西", _task=task, _execution_context=execution())
        kwargs = self.run_single.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["gateway"]), ("gpt-6-sol", "boyue"))

    def test_escape_cancels_the_dispatch_and_says_so(self):
        _, result = self.spawn({"status": "cancelled"})
        self.run_batch.assert_not_called()
        self.assertIn("取消", str(result))

    def test_no_answer_keeps_the_current_model(self):
        """非交互运行（-p）时桥会回「无人应答」：照旧用当前模型，不报错也不卡住。"""
        _, result = self.spawn({"status": "unattended"})
        sent = self.run_batch.call_args.args[0]
        self.assertNotIn("_model", sent[0])
        self.assertEqual(self.run_batch.call_args.kwargs["model"], "glm-5.3")
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
