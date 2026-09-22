"""工具循环默认不设轮数上限（用户 2026-09-04："稍微大一点的任务就很容易触顶（40 次）"）。

像 Claude Code 交互模式：模型自己停下或用户 Esc 才结束。settings 的 ``max_turns``
与 ``--max-turns N`` 是无人值守场景的可选兜底；主循环与 child 共用同一个旋钮。
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from core import agent as A, agents, client, controller as C, settings, tools
from tests.test_agent_loop import mk, scripted

ROUNDS = 45          # 明显超过旧上限 40


def _endless_then_done(n):
    """n 轮工具调用之后给最终总结。

    **每一轮的参数都不同**（`path` 带上轮号）。以前是 45 次一模一样的
    `list_dir {"path": "."}`，而 2026-09-22 加了无进展检测之后，那正好是
    「同一工具 + 同一参数 + 同一结果」的打转形态，会在第 3 轮被拦下——
    拦得对，是夹具不对：这条用例要证明的是「循环不再被 40 封顶」，
    不是「原地打转能跑很久」。
    """
    rounds = [("", [("list_dir", {"path": f"./round-{index}"})])
              for index in range(n)]
    return scripted(*(rounds + [("最终总结", [])]))


def _varying_tool_output():
    """每次返回不同内容的假工具。结果一样也会被判成打转。"""
    state = {"n": 0}

    def run(*_args, **_kwargs):
        state["n"] += 1
        return f"out-{state['n']}"

    return run, state


class UnboundedLoopTests(unittest.TestCase):
    def test_legacy_loop_runs_past_forty_rounds_by_default(self):
        ag = mk()
        fake_tool, calls = _varying_tool_output()
        with mock.patch.object(client, "stream_chat", _endless_then_done(ROUNDS)), \
             mock.patch.object(tools, "run", side_effect=fake_tool), \
             mock.patch.object(ag, "log_usage"):
            evs = list(ag.run("go"))
        end = [e for e in evs if e["t"] == "end"][-1]
        self.assertNotEqual(end.get("kind"), "max_turns", end)
        self.assertEqual(calls["n"], ROUNDS)
        self.assertEqual(ag.messages[-1]["role"], "assistant")
        self.assertIn("最终总结", ag.messages[-1]["content"])

    def test_managed_loop_runs_past_forty_rounds_by_default(self):
        ag = mk()
        journal = C.MemoryJournal()
        controller = C.SessionController("t", journal)
        action = controller.submit("go", C.QueueMode.NEXT_TURN)
        self.assertEqual(action.kind, C.ActionKind.START_TURN)
        fake_tool, _ = _varying_tool_output()
        with mock.patch.object(tools, "run", side_effect=fake_tool), \
             mock.patch.object(ag, "log_usage"):
            evs = list(ag.run(
                "go", controller=controller,
                stream_factory=_endless_then_done(ROUNDS)))
        end = evs[-1]
        self.assertNotEqual(end.get("kind"), "max_turns", end)
        kinds = [event["kind"] for event in journal.events]
        self.assertNotIn("turn_failed", kinds)
        self.assertEqual(kinds.count("tool_finished"), ROUNDS)
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_explicit_bound_still_stops_with_visible_partial(self):
        ag = mk()
        fake_tool, _ = _varying_tool_output()
        with mock.patch.object(client, "stream_chat", _endless_then_done(ROUNDS)), \
             mock.patch.object(tools, "run", side_effect=fake_tool), \
             mock.patch.object(ag, "log_usage"):
            evs = list(ag.run("go", max_turns=5))
        end = [e for e in evs if e["t"] == "end"][-1]
        self.assertEqual(end["kind"], "max_turns")
        self.assertIn("最大轮数 5", end["reason"])

    def test_turn_indices_semantics(self):
        self.assertEqual(list(A._turn_indices(3)), [0, 1, 2])
        for unbounded in (None, 0, -1, "x"):
            it = A._turn_indices(unbounded)
            self.assertEqual([next(it) for _ in range(3)], [0, 1, 2], unbounded)


class KnobTests(unittest.TestCase):
    def test_settings_policy_normalises_to_none_or_positive_int(self):
        for raw, want in ((None, None), (0, None), (-3, None), ("x", None),
                          (12, 12), ("12", 12), (7.9, 7)):
            self.assertEqual(settings.max_turns_policy({"max_turns": raw}), want, raw)
        self.assertIsNone(settings.max_turns_policy({}))
        self.assertIsNone(settings.max_turns_policy(settings.DEFAULTS), "默认不设上限")

    def test_settings_render_says_unbounded(self):
        rendered = settings.render(dict(settings.DEFAULTS), ["builtin"])
        text = rendered if isinstance(rendered, str) else "\n".join(rendered)
        self.assertIn("max_turns", text)
        self.assertIn("不设上限", text)

    def test_session_reads_the_knob_from_cfg(self):
        ag = mk()
        cfg = dict(settings.DEFAULTS); cfg["max_turns"] = 9
        with mock.patch.object(zylab.Session, "__init__", zylab.Session.__init__):
            sess = object.__new__(zylab.Session)
            # 只验证构造里的那一行：cfg → max_turns
            sess.cfg = cfg
            sess.max_turns = settings.max_turns_policy(sess.cfg)
        self.assertEqual(sess.max_turns, 9)

    def test_child_shares_the_parent_knob_through_hook_cfg(self):
        self.assertEqual(agents._child_max_turns(SimpleNamespace(hook_cfg={"max_turns": 7})), 7)
        self.assertIsNone(agents._child_max_turns(SimpleNamespace(hook_cfg={"max_turns": 0})))
        self.assertIsNone(agents._child_max_turns(SimpleNamespace(hook_cfg={})))
        self.assertIsNone(agents._child_max_turns(SimpleNamespace()))

    def test_cli_flag_zero_means_unbounded(self):
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--max-turns", type=int, default=None)
        for argv, want in (([], None), (["--max-turns", "0"], None), (["--max-turns", "25"], 25)):
            a = ap.parse_args(argv)
            value = None if a.max_turns is None else (a.max_turns if a.max_turns > 0 else None)
            self.assertEqual(value, want, argv)


if __name__ == "__main__":
    unittest.main()
