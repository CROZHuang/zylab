"""模型可以起草自动续轮目标，但 arm 永远是用户的动作。

2026-09-08 的实测事故：用户要求「持续跟踪另一个会话的进度并自动汇报」，模型回答
「必须等你 prompt」——**引擎其实就在旁边空转**。根因不是引擎缺失，而是没有 armed
goal 时 <goal-policy> 不会注入，模型的上下文里没有任何东西表明这功能存在。
修法两条：把能力写进基础 system prompt，以及给模型一个只起草不 arm 的工具。
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent as A                            # noqa: E402
from core import goals as GOALS                        # noqa: E402
from core import tools                                 # noqa: E402


class DiscoverabilityTests(unittest.TestCase):
    def test_base_system_prompt_says_the_capability_exists(self):
        # 关键：这段必须在**基础** prompt 里，不能只在 <goal-policy> 里 ——
        # 没有 armed goal 时那个块根本不注入，而那正是用户开口问的时刻。
        self.assertIn("goal_propose", A.SYSTEM)
        self.assertIn("不要回答「我做不到」", A.SYSTEM)

    def test_tool_schema_explains_that_the_user_decides(self):
        schema = {
            (item.get("function") or {}).get("name"): item
            for item in tools.SCHEMA}
        self.assertIn("goal_propose", schema)
        description = schema["goal_propose"]["function"]["description"]
        self.assertIn("必须由用户拍板", description)
        self.assertIn("弹一个采纳/不采纳的确认框", description)
        self.assertIn("/goal accept", description)   # 弹不成时的退路
        # 起草本身不改任何东西：不该要求写权限
        self.assertIn("goal_propose", tools.SAFE)


class ProposalToolTests(unittest.TestCase):
    def call(self, **kwargs):
        captured = {}

        def callback(payload, execution_context=None):
            captured.update(payload)
            return {"status": "staged"}

        with mock.patch.dict(tools.HOOK_CTX, {"goal_propose": callback},
                             clear=False):
            raw = tools.t_goal_propose(**kwargs)
        return captured, json.loads(raw)

    def test_defaults_and_bounds(self):
        payload, result = self.call(objective="把 252 个 marker 全部归一完")
        self.assertEqual(payload["max_rounds"], GOALS.DEFAULT_MAX_ROUNDS)
        self.assertEqual(result["status"], "staged")
        payload, _ = self.call(objective="x", max_rounds=5, rationale="理由")
        self.assertEqual(payload["max_rounds"], 5)
        self.assertEqual(payload["rationale"], "理由")

    def test_rejects_empty_or_out_of_range(self):
        for kwargs in ({"objective": "  "},
                       {"objective": "x", "max_rounds": 0},
                       {"objective": "x", "max_rounds": GOALS.MAX_ROUNDS + 1}):
            with self.assertRaises((ValueError, TypeError)):
                self.call(**kwargs)
        with self.assertRaises(TypeError):
            self.call(objective="x", max_rounds="20")

    def test_worker_path_says_it_is_not_armed_yet(self):
        events = []

        class Task:
            def runtime_event(self, event):
                events.append(event)
                return True

        text = tools.t_goal_propose(objective="x", _task=Task())
        self.assertEqual(events[0]["kind"], "goal_proposed")
        self.assertIn("等待用户采纳", text)
        self.assertIn("不要假设它已生效", text)


class SessionStagingTests(unittest.TestCase):
    def make_session(self):
        import zylab                                   # noqa: PLC0415
        session = zylab.Session.__new__(zylab.Session)
        session._goal_proposal = None
        return session, zylab

    def test_staging_never_arms_anything(self):
        session, _ = self.make_session()
        session.create_goal = mock.Mock()
        result = session._goal_proposal_from_tool({
            "objective": "跟踪医疗数据流水线到里程碑",
            "max_rounds": 30, "rationale": "跨天任务"})
        self.assertFalse(result["armed"])
        self.assertEqual(result["requires"], "/goal accept")
        session.create_goal.assert_not_called()       # 起草绝不等于 arm
        self.assertEqual(session._goal_proposal["max_rounds"], 30)

    def test_taking_the_proposal_consumes_it(self):
        session, _ = self.make_session()
        session._goal_proposal_from_tool({"objective": "x"})
        self.assertIsNotNone(session.take_goal_proposal())
        self.assertIsNone(session.take_goal_proposal())

    def test_invalid_drafts_are_refused(self):
        session, _ = self.make_session()
        for payload in ({"objective": ""},
                        {"objective": "x", "max_rounds": 0},
                        {"objective": "x", "max_rounds": "many"}):
            with self.assertRaises(GOALS.GoalError):
                session._goal_proposal_from_tool(payload)

    def test_terminal_escapes_in_a_draft_are_flattened(self):
        session, _ = self.make_session()
        session._goal_proposal_from_tool({
            "objective": "\x1b]0;title\x07跟踪\n进度"})
        objective = session._goal_proposal["objective"]
        self.assertNotIn("\x1b", objective)
        self.assertNotIn("\n", objective)


class AcceptRejectTests(unittest.TestCase):
    def make_session(self):
        import zylab                                   # noqa: PLC0415
        session = zylab.Session.__new__(zylab.Session)
        session._goal_proposal = None
        session.create_goal = mock.Mock(return_value={
            "rounds_started": 0, "max_rounds": 30})
        return session, zylab

    def test_accept_arms_with_the_drafted_rounds(self):
        import contextlib                              # noqa: PLC0415
        import io                                      # noqa: PLC0415

        session, zylab = self.make_session()
        session._goal_proposal_from_tool(
            {"objective": "跟踪流水线", "max_rounds": 30})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            zylab.cmd_goal(session, "accept")
        session.create_goal.assert_called_once_with("跟踪流水线", max_rounds=30)
        self.assertIn("已采纳模型提案并 armed", out.getvalue())
        self.assertIsNone(session._goal_proposal)

    def test_reject_drops_it_without_arming(self):
        import contextlib                              # noqa: PLC0415
        import io                                      # noqa: PLC0415

        session, zylab = self.make_session()
        session._goal_proposal_from_tool({"objective": "x"})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            zylab.cmd_goal(session, "reject")
        session.create_goal.assert_not_called()
        self.assertIn("已忽略", out.getvalue())

    def test_accept_without_a_proposal_says_so(self):
        import contextlib                              # noqa: PLC0415
        import io                                      # noqa: PLC0415

        session, zylab = self.make_session()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            zylab.cmd_goal(session, "accept")
        session.create_goal.assert_not_called()
        self.assertIn("没有待采纳", out.getvalue())


class HelpTests(unittest.TestCase):
    def test_help_explains_arming_and_its_process_boundary(self):
        import contextlib                              # noqa: PLC0415
        import io                                      # noqa: PLC0415
        import zylab                                   # noqa: PLC0415

        with contextlib.redirect_stdout(io.StringIO()) as out:
            zylab._goal_usage()
        text = out.getvalue()
        self.assertIn("会话空闲时自动开始下一轮", text)
        self.assertIn("只有你能 armed", text)
        self.assertIn("退出 zylab 或 pod 重启，自动续轮就停", text)
        self.assertIn("/goal accept", text)


if __name__ == "__main__":
    unittest.main()


class ModalGateTests(unittest.TestCase):
    """弹窗走 decision_gate 那条已验证的桥：有 owner 就弹，没答就退回草稿。"""

    def gate(self, raw, *, role="main"):
        captured = {}

        class Task:
            def request_interaction(self, kind, payload):
                captured["kind"] = kind
                captured["payload"] = payload
                return raw

            def runtime_event(self, event):
                captured.setdefault("events", []).append(event)
                return True

        class Context:
            interaction_role = role

        text = tools.t_goal_propose(
            objective="持续跟踪流水线并在里程碑汇报", max_rounds=30,
            rationale="跨多天", _task=Task(), _execution_context=Context())
        return captured, text

    def test_accept_arms_through_the_owner_thread(self):
        captured, text = self.gate({
            "status": "resolved", "attended": True,
            "choice": tools.GOAL_GATE_ACCEPT})
        # 复用 decision_gate 交互类型，不新造一种
        self.assertEqual(captured["kind"], "decision_gate")
        question = captured["payload"]["questions"][0]
        self.assertIn("最多 30 轮", question["question"])
        self.assertEqual(
            [option["label"] for option in question["options"]],
            [tools.GOAL_GATE_ACCEPT, tools.GOAL_GATE_DECLINE])
        self.assertTrue(question["options"][0]["recommended"])
        event = captured["events"][0]
        self.assertEqual(event["kind"], "goal_proposed")
        self.assertIs(event["payload"]["accepted"], True)
        self.assertIn("用户已在弹窗里采纳", text)

    def test_decline_stages_nothing_and_tells_the_model_to_drop_it(self):
        captured, text = self.gate({
            "status": "resolved", "attended": True,
            "choice": tools.GOAL_GATE_DECLINE})
        self.assertNotIn("events", captured)          # 连草稿都不留
        self.assertIn("不要再提", text)

    def test_unanswered_is_never_treated_as_consent(self):
        for raw in ({"status": "unattended", "attended": False},
                    {"status": "resolved", "attended": False,
                     "choice": tools.GOAL_GATE_ACCEPT},
                    {"status": "invalid", "attended": True, "choice": "x"},
                    "not-a-dict"):
            captured, text = self.gate(raw)
            event = captured["events"][0]
            self.assertNotIn("accepted", event["payload"])
            self.assertIn("等待用户采纳", text)

    def test_child_workers_cannot_open_the_modal(self):
        captured, text = self.gate(
            {"status": "resolved", "attended": True,
             "choice": tools.GOAL_GATE_ACCEPT}, role="blocked")
        self.assertNotIn("kind", captured)            # 没弹窗
        self.assertNotIn("accepted", captured["events"][0]["payload"])
        self.assertIn("等待用户采纳", text)


class AcceptedEventTests(unittest.TestCase):
    def test_owner_arms_immediately_when_the_user_accepted(self):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session._goal_proposal = None
        session.create_goal = mock.Mock(return_value={
            "rounds_started": 0, "max_rounds": 30})
        result = session._goal_proposal_from_tool({
            "objective": "跟踪流水线", "max_rounds": 30, "accepted": True})
        session.create_goal.assert_called_once_with("跟踪流水线", max_rounds=30)
        self.assertTrue(result["armed"])
        self.assertIsNone(session._goal_proposal)     # 不留待办草稿

    def test_a_forged_accepted_flag_still_goes_through_validation(self):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session._goal_proposal = None
        session.create_goal = mock.Mock()
        with self.assertRaises(GOALS.GoalError):
            session._goal_proposal_from_tool(
                {"objective": "", "accepted": True})
        session.create_goal.assert_not_called()
