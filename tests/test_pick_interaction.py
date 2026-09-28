"""工具线程向主线程要「从列表里选一项」（kind="pick"）：派子代理时点了「其他模型…」用它。

以前主线程只认 decision_gate，别的种类一律答「不支持的 interaction」。协议与决策门
相同：先记账、再弹列表、结果规范化以后才叫醒工作线程；没问成绝不冒充用户选了什么。
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tools
import zylab as CLI

OPTIONS = [{"label": "glm-5.3", "detail": "当前模型 · 智谱 · boyue"},
           {"label": "minimax-m2.7", "detail": "MiniMax · deepinfer"}]


class Controller:
    state = CLI.C.ControllerState.TOOL_RUNNING_FOREGROUND
    current_tool_call_id = "call-pick"

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
        self.results = []

    def resolve_interaction(self, _task_id, _request_id, result):
        self.results.append(result)
        return True


def session_with(owner=None):
    session = CLI.Session.__new__(CLI.Session)
    session._ui_owner_thread_id = threading.get_ident()
    session.controller = Controller()
    session.task_manager = Manager()
    session._pending_interactions = {}
    if owner is not None:
        session._pick_owner = owner
    return session


def request(session, *, role="main", options=OPTIONS):
    return session.handle_interaction_request({
        "task_id": "task-pick", "tool_call_id": "call-pick",
        "request_id": "req-pick", "kind": "pick",
        "payload": {"_interaction_role": role, "title": "agent1 · 查资料 · 用哪个模型？",
                    "options": options},
    })


class OwnerAnswersAPick(unittest.TestCase):
    def test_the_choice_is_audited_then_handed_to_the_worker(self):
        seen = {}

        def owner(payload):
            seen["payload"] = payload
            return {"status": "resolved", "attended": True, "choice": "minimax-m2.7"}

        session = session_with(owner)
        result = request(session)
        self.assertEqual((result["status"], result["choice"]), ("resolved", "minimax-m2.7"))
        self.assertEqual([call[0] for call in session.controller.calls], ["begin", "resolve"])
        self.assertEqual(session.controller.calls[0][1][2], "pick")
        self.assertEqual(session.task_manager.results[-1]["choice"], "minimax-m2.7")
        self.assertIn("agent1", seen["payload"]["title"])

    def test_a_choice_that_was_not_offered_is_refused(self):
        session = session_with(lambda payload: {
            "status": "resolved", "attended": True, "choice": "rm -rf"})
        result = request(session)
        self.assertNotEqual(result["status"], "resolved")
        self.assertEqual(result["choice"], "")

    def test_only_the_main_agent_may_open_it(self):
        owner = mock.Mock()
        session = session_with(owner)
        result = request(session, role="subagent")
        self.assertEqual(result["status"], "unattended")
        owner.assert_not_called()

    def test_an_empty_list_is_invalid_not_a_modal(self):
        owner = mock.Mock()
        session = session_with(owner)
        result = request(session, options=[])
        self.assertEqual(result["status"], "invalid")
        owner.assert_not_called()


class PickOwnerUsesTheListWidget(unittest.TestCase):
    """主线程上真正弹的是 /model 那种列表（Session.pick）：Esc = 取消。"""

    def make(self, picked):
        session = session_with()
        session._ui_owner_thread_id = threading.get_ident()
        session.pump = mock.Mock()
        session.pump.stream.isatty.return_value = True
        session.renderer = mock.Mock()
        session.pick = mock.Mock(return_value=picked)
        return session

    def payload(self):
        return tools.normalize_pick_request({
            "_interaction_role": "main", "title": "agent1 · 用哪个模型？",
            "options": OPTIONS})

    def test_enter_on_a_row_is_the_choice(self):
        session = self.make(dict(OPTIONS[1]))
        result = session._pick_owner(self.payload())
        self.assertEqual((result["status"], result["choice"]), ("resolved", "minimax-m2.7"))
        self.assertIn("agent1", session.pick.call_args.kwargs["title"])

    def test_escape_is_a_cancel_not_a_choice(self):
        session = self.make(None)
        result = session._pick_owner(self.payload())
        self.assertEqual((result["status"], result["choice"]), ("cancelled", ""))

    def test_without_a_terminal_nobody_is_asked(self):
        session = self.make(dict(OPTIONS[0]))
        session.pump.stream.isatty.return_value = False
        result = session._pick_owner(self.payload())
        self.assertEqual(result["status"], "unattended")
        session.pick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
