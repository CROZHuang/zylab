"""recap 接到会话上的那一层：/recap 命令、resume 入口、后台结果怎么交回主循环。

行为照搬 Claude Code：recap 是模型现写的**一行**，离开终端期间后台写好等人回来，
/recap 立刻写一条，结果只给人看、不进 messages。何时写/何时不打扰的判定在
test_away_recap.py，agent 层的请求形状在 test_session_recap.py。
"""
import contextlib
import io
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests  # noqa: F401,E402  —— 状态目录隔离

import zylab as CLI                                   # noqa: E402
from core import client                               # noqa: E402


def msgs(n_pairs, first_user="最初的目标是修好解析器"):
    out = [{"role": "system", "content": "s"},
           {"role": "user", "content": first_user},
           {"role": "assistant", "content": "ok"}]
    for i in range(n_pairs - 1):
        out.append({"role": "user", "content": f"追问 {i}"})
        out.append({"role": "assistant", "content": f"回答 {i}"})
    return out


def make_session(messages=None, cfg=None):
    session = CLI.Session.__new__(CLI.Session)
    session.cfg = dict(cfg or {})
    session.ag = mock.Mock()
    session.ag.messages = messages if messages is not None else msgs(4)
    session.ag.away_recap = None
    session.save = mock.Mock()
    session.pump = SimpleNamespace(editor=SimpleNamespace(text=""), on_focus=None)
    session.renderer = mock.Mock()
    session.away_recap_ctl = None
    session._away_recap_lock = threading.Lock()
    session._away_recap_ready = []
    session._turn_running = False
    session._resume_recap_pending = False
    session._deferred_pump_events = []
    session.task_manager = None
    session._count_active_agents = mock.Mock(return_value=0)
    session.allowed_tool_names = mock.Mock(return_value=frozenset({"read_file", "bash"}))
    return session


def run_command(sess, rest=""):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        CLI.cmd_recap(sess, rest)
    return out.getvalue()


class RenderTests(unittest.TestCase):
    def test_one_line_in_the_status_grammar(self):
        block = CLI.render_away_recap("在修 X，下一步跑测试。")
        self.assertIn("※ recap · 在修 X，下一步跑测试。", block)
        self.assertTrue(block.endswith("\r\n"))
        self.assertEqual(block.count("\n"), 1)


class CommandTests(unittest.TestCase):
    def make(self, result, messages=None):
        sess = SimpleNamespace(
            ag=mock.Mock(), save=mock.Mock(),
            generate_recap_now=mock.Mock(return_value=result))
        sess.ag.messages = messages if messages is not None else msgs(4)
        return sess

    def test_ok_prints_the_line_records_it_and_saves(self):
        sess = self.make({"kind": "ok", "text": "在修 X。", "seconds": 4.9})
        shown = run_command(sess)
        self.assertIn("※ recap · 在修 X。", shown)
        sess.ag.record_away_recap.assert_called_once_with("在修 X。")
        sess.save.assert_called_once()

    def test_nothing_to_recap_costs_no_model_call(self):
        sess = self.make({"kind": "ok", "text": "x"},
                         messages=[{"role": "system", "content": "s"}])
        self.assertIn("先发一条消息", run_command(sess))
        sess.generate_recap_now.assert_not_called()

    def test_cancelled(self):
        sess = self.make({"kind": "aborted", "text": ""})
        self.assertIn("已取消", run_command(sess))
        sess.ag.record_away_recap.assert_not_called()
        sess.save.assert_not_called()

    def test_failure_names_the_reason_on_one_line(self):
        sess = self.make({"kind": "api-error", "text": "HTTP 503\n  service_error"})
        shown = run_command(sess)
        self.assertIn("HTTP 503 service_error", shown)
        sess.ag.record_away_recap.assert_not_called()

    def test_empty_answer_is_reported_as_such(self):
        shown = run_command(self.make({"kind": "failed", "text": ""}))
        self.assertIn("空正文", shown)

    def test_a_slow_recap_never_suggests_another_model(self):
        """09-20 实测 glm-5.3@deepinfer 首字 128s。慢归慢，写 recap 的就是窗口当前的模型
        （用户定的），不劝人换。"""
        slow = {"kind": "ok", "text": "好。", "seconds": 128.0, "model": "glm-5.3"}
        shown = run_command(self.make(slow))
        self.assertEqual(shown.strip().count("\n"), 0)            # 只有那一行
        self.assertNotIn("recap_model", shown)

    def test_help_costs_no_model_call(self):
        sess = self.make({"kind": "ok", "text": "x"})
        shown = run_command(sess, "help")
        self.assertIn("/recap", shown)
        self.assertIn("这个窗口此刻在用的模型", shown)
        sess.generate_recap_now.assert_not_called()


class WiringTests(unittest.TestCase):
    def test_controller_subscribes_to_terminal_focus(self):
        sess = make_session()
        controller = sess.start_away_recap()
        self.assertIs(sess.away_recap_ctl, controller)
        self.assertEqual(sess.pump.on_focus, controller.focus_changed)
        controller.dispose()

    def test_recap_auto_false_disables_the_automatic_triggers(self):
        sess = make_session(cfg={"recap_auto": False})
        controller = sess.start_away_recap()
        controller.turn_ended()
        controller.focus_changed(False)
        self.assertIsNone(controller.maybe_generate())
        self.assertEqual(controller.last_skip, "disabled")
        sess.ag.generate_away_recap.assert_not_called()

    def test_state_reports_draft_loading_and_the_last_recap(self):
        sess = make_session()
        self.assertFalse(sess._away_recap_state()["draft"])
        sess.pump.editor.text = "  写到一半的提问"
        sess._turn_running = True
        sess.ag.away_recap = {"text": "旧的", "users": 3}
        state = sess._away_recap_state()
        self.assertTrue(state["draft"])
        self.assertTrue(state["loading"])
        self.assertEqual(state["last_recap_users"], 3)
        self.assertEqual(state["messages"], sess.ag.messages)

    def test_running_background_tasks_count_as_pending_work(self):
        sess = make_session()
        sess._count_active_agents.return_value = 1
        sess.task_manager = SimpleNamespace(list=lambda: [
            SimpleNamespace(status="running"),
            SimpleNamespace(status=next(iter(CLI.TASKS.TERMINAL_STATES)))])
        self.assertEqual(sess._away_recap_background(), 2)

    def test_generation_is_left_to_this_windows_own_agent(self):
        """会话层不挑模型：连旧配置里残留的 recap_model 也传不到 agent 那里。"""
        sess = make_session(cfg={"recap_model": "deepinfer/deepseek-v4-flash"})
        cancel = threading.Event()
        sess._generate_away_recap(cancel)
        # 只传取消句柄和与主循环同源的工具名单（为了共用缓存前缀），不传模型
        sess.ag.generate_away_recap.assert_called_once_with(
            cancel=cancel, allowed_tools=frozenset({"read_file", "bash"}))


class HandBackTests(unittest.TestCase):
    """后台线程只入队；写屏和写会话文件都留给主循环。"""

    def test_delivery_only_queues(self):
        sess = make_session()
        sess._deliver_away_recap("在修 X。（提示）", "在修 X。")
        sess.ag.record_away_recap.assert_not_called()
        sess.save.assert_not_called()
        sess.renderer.write_output.assert_not_called()

    def test_taking_it_records_the_raw_text_and_saves(self):
        sess = make_session()
        sess._deliver_away_recap("在修 X。（提示）", "在修 X。")
        self.assertEqual(sess.take_away_recap(), "在修 X。（提示）")
        sess.ag.record_away_recap.assert_called_once_with("在修 X。")
        sess.save.assert_called_once()
        self.assertIsNone(sess.take_away_recap())

    def test_only_the_latest_one_is_shown(self):
        sess = make_session()
        sess._deliver_away_recap("旧", "旧")
        sess._deliver_away_recap("新", "新")
        self.assertEqual(sess.take_away_recap(), "新")
        sess.ag.record_away_recap.assert_called_once_with("新")

    def test_a_new_turn_discards_what_was_not_shown_yet(self):
        sess = make_session()
        sess.away_recap_ctl = mock.Mock()
        sess._deliver_away_recap("过时了", "过时了")
        sess.away_recap_turn(True)
        self.assertTrue(sess._turn_running)
        sess.away_recap_ctl.turn_started.assert_called_once()
        self.assertIsNone(sess.take_away_recap())
        sess.away_recap_turn(False)
        self.assertFalse(sess._turn_running)
        sess.away_recap_ctl.turn_ended.assert_called_once()

    def test_turn_bookkeeping_works_without_a_controller(self):
        """非交互模式（-p）没有控制器：起止记账不能因此崩。"""
        sess = make_session()
        sess.away_recap_turn(True)
        sess.away_recap_turn(False)

    def test_a_failing_save_does_not_lose_the_line(self):
        sess = make_session()
        sess.save.side_effect = OSError("disk full")
        sess._deliver_away_recap("好。", "好。")
        self.assertEqual(sess.take_away_recap(), "好。")


class ResumeTests(unittest.TestCase):
    def test_before_the_repl_is_up_it_is_only_remembered(self):
        sess = make_session()
        sess.renderer = None
        self.assertEqual(sess.request_resume_recap(), "pending")
        self.assertTrue(sess._resume_recap_pending)

    def test_stored_recap_is_reused_when_nothing_was_asked_since(self):
        sess = make_session(messages=msgs(4))
        sess.away_recap_ctl = mock.Mock()
        sess.ag.away_recap = {"text": "在修 X。", "users": 4}
        self.assertEqual(sess.request_resume_recap(), "stored")
        sess.away_recap_ctl.request.assert_not_called()
        self.assertEqual(sess.take_away_recap(), "在修 X。")
        sess.ag.record_away_recap.assert_not_called()      # 本来就是落盘的那条
        sess.save.assert_not_called()

    def test_new_questions_since_then_mean_a_fresh_one(self):
        sess = make_session(messages=msgs(6))
        sess.away_recap_ctl = mock.Mock()
        sess.ag.away_recap = {"text": "旧的", "users": 4}
        self.assertEqual(sess.request_resume_recap(), "generating")
        sess.away_recap_ctl.request.assert_called_once()
        self.assertFalse(sess._resume_recap_pending)

    def test_recap_auto_false_means_no_call_on_resume(self):
        sess = make_session(cfg={"recap_auto": False})
        sess.away_recap_ctl = mock.Mock()
        self.assertIsNone(sess.request_resume_recap())
        sess.away_recap_ctl.request.assert_not_called()

    def test_show_recap_tolerates_embedders_without_the_feature(self):
        self.assertIsNone(CLI.show_recap(SimpleNamespace(), {"messages": msgs(3)}))
        sess = make_session()
        sess.renderer = None
        self.assertEqual(CLI.show_recap(sess, {}), "pending")


class ClearTests(unittest.TestCase):
    def test_clear_drops_the_recap_of_the_conversation_it_cleared(self):
        sess = make_session()
        sess.goal = None
        sess.ag.away_recap = {"text": "说的是旧对话", "users": 4}
        sess.sync_transcript = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            CLI.cmd_clear(sess, "")
        self.assertIsNone(sess.ag.away_recap)


class FakePump:
    def __init__(self, events):
        self.events = list(events)
        self.editor = SimpleNamespace(text="")

    def get(self, timeout=None):
        return self.events.pop(0) if self.events else None


class RecapNowTests(unittest.TestCase):
    def interactive(self, events, generate):
        sess = make_session()
        sess.pump = FakePump(events)
        sess.refresh_composer = mock.Mock(return_value="snapshot")
        sess.composer_prompt = mock.Mock(return_value="› ")
        sess.heartbeat_lease = mock.Mock()
        sess.ag.generate_away_recap.side_effect = generate
        return sess

    def test_without_a_pump_it_is_a_plain_blocking_call(self):
        sess = make_session()
        sess.pump = sess.renderer = None
        sess.ag.generate_away_recap.return_value = {"kind": "ok", "text": "好。"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sess.generate_recap_now()["text"], "好。")

    def test_result_comes_back_and_the_wait_is_visible(self):
        sess = self.interactive(
            [], lambda cancel, allowed_tools=None: {"kind": "ok", "text": "好。"})
        self.assertEqual(sess.generate_recap_now(), {"kind": "ok", "text": "好。"})
        activity = sess.refresh_composer.call_args.kwargs["activity"]
        self.assertIn("recap", activity)
        self.assertIn("Esc", activity)
        sess.renderer.clear_input.assert_called()

    def test_esc_cancels_a_request_that_is_still_waiting_for_its_first_byte(self):
        """慢网关上首字要一两分钟：Esc 必须立刻把人放回输入框。"""
        seen = {}

        def slow(cancel, allowed_tools=None):
            seen["handle"] = cancel
            cancel.wait(10)
            return {"kind": "aborted", "text": ""}

        sess = self.interactive([CLI.tui.PumpEvent("interrupt")], slow)
        self.assertEqual(sess.generate_recap_now()["kind"], "aborted")
        self.assertIsInstance(seen["handle"], client.CancellationHandle)
        self.assertTrue(seen["handle"].is_set())

    def test_unrelated_input_is_kept_for_the_main_loop(self):
        gate = threading.Event()
        typed = CLI.tui.PumpEvent("submit", text="下一个问题")

        def generate(cancel, allowed_tools=None):
            gate.wait(5)
            return {"kind": "ok", "text": "好。"}

        sess = self.interactive([typed], generate)
        original_get = sess.pump.get

        def get(timeout=None):
            event = original_get(timeout)
            if event is None:
                gate.set()
            return event

        sess.pump.get = get
        self.assertEqual(sess.generate_recap_now()["kind"], "ok")
        self.assertEqual(sess._deferred_pump_events, [typed])

    def test_a_crashing_generator_is_a_failed_recap_not_a_dead_repl(self):
        def boom(cancel, allowed_tools=None):
            raise RuntimeError("gateway down")

        result = self.interactive([], boom).generate_recap_now()
        self.assertEqual(result["kind"], "failed")
        self.assertIn("gateway down", result["text"])


if __name__ == "__main__":
    unittest.main()
