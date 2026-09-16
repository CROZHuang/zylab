"""F4：worker 取消后 decision gate modal 必须被收掉，且不能伪造用户选择。

修复在 codex 的工作树里（zylab._decision_gate_owner 的 finally 调
pump.dismiss_decision_gate）；这里把它的契约钉死，免得回退。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


def _drain(pump):
    out = []
    while True:
        try:
            out.append(pump._events.get_nowait())
        except Exception:
            return out


class DismissContract(unittest.TestCase):
    def _pump_with_open_gate(self):
        pump = tui.InputPump()
        _drain(pump)
        pump.open_decision_gate(
            question="q", options=[{"label": "A", "recommended": True},
                                   {"label": "B", "recommended": False}])
        self.assertEqual(pump._mode, "decision_gate")
        _drain(pump)
        return pump

    def test_dismiss_returns_to_line_mode_without_a_result_event(self):
        pump = self._pump_with_open_gate()
        self.assertTrue(pump.dismiss_decision_gate())
        self.assertEqual(pump._mode, "line")
        self.assertIsNone(pump._decision_gate)
        kinds = [e.kind for e in _drain(pump)]
        self.assertNotIn("decision_gate_result", kinds, "不得伪造用户选择")
        self.assertIn("redraw", kinds)

    def test_dismiss_is_idempotent_and_harmless_when_no_gate(self):
        pump = tui.InputPump()
        _drain(pump)
        self.assertFalse(pump.dismiss_decision_gate())
        self.assertEqual(pump._mode, "line")
        self.assertEqual(_drain(pump), [])

    def test_next_ordinary_key_reaches_the_line_editor_after_dismiss(self):
        """残留 modal 的症状是：正常输入被 gate 吃掉，只能重启 REPL。"""
        pump = self._pump_with_open_gate()
        pump.dismiss_decision_gate()
        _drain(pump)
        events = pump.editor.handle("x")
        self.assertEqual(pump.editor.text, "x")
        self.assertTrue(events)


if __name__ == "__main__":
    unittest.main()
