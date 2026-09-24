"""/ 列表是一个环，不是一条队列（用户 2026-09-24：「我第一个往上会跳到最后一个，最后一个
接着往下会跳到第一个，它是一个 loop 而不是 queue」）。

断言落在用户看得见的东西上：快照里**高亮的是哪一行**、滚动窗口停在哪一页 ——
只改 menu_index 而窗口没跟上，用户看到的高亮仍会停在原处。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tui

# 比一页（8 行）多，滚动窗口才有意义。
COMMANDS = {f"/cmd{index:02d}": f"命令 {index}" for index in range(12)}


def open_menu():
    pump = tui.InputPump(stream=io.StringIO(), commands=COMMANDS)
    pump.editor.replace("/")
    return pump.editor


def highlighted(editor):
    snap = editor.snapshot()
    return snap.options[snap.selected][0], snap.hint


class SlashMenuWrapsAround(unittest.TestCase):
    def test_up_from_the_first_row_lands_on_the_last(self):
        editor = open_menu()
        self.assertEqual(highlighted(editor)[0], "/cmd00")
        editor.handle("up")
        name, hint = highlighted(editor)
        self.assertEqual(name, "/cmd11")
        self.assertIn("/12", hint)
        self.assertTrue(hint.startswith("5-12/12"), hint)

    def test_down_from_the_last_row_lands_on_the_first(self):
        editor = open_menu()
        for _ in range(11):
            editor.handle("down")
        self.assertEqual(highlighted(editor)[0], "/cmd11")
        editor.handle("down")
        name, hint = highlighted(editor)
        self.assertEqual(name, "/cmd00")
        self.assertTrue(hint.startswith("1-8/12"), hint)

    def test_ordinary_moves_are_unchanged(self):
        editor = open_menu()
        editor.handle("down")
        editor.handle("down")
        self.assertEqual(highlighted(editor)[0], "/cmd02")
        editor.handle("up")
        self.assertEqual(highlighted(editor)[0], "/cmd01")

    def test_a_filtered_menu_wraps_within_what_is_shown(self):
        editor = open_menu()
        editor.replace("/cmd1")                  # 只剩 /cmd10、/cmd11
        self.assertEqual(highlighted(editor)[0], "/cmd10")
        editor.handle("up")
        self.assertEqual(highlighted(editor)[0], "/cmd11")
        editor.handle("down")
        self.assertEqual(highlighted(editor)[0], "/cmd10")


class EveryListSharesTheSameGrammar(unittest.TestCase):
    """SPEC-CC-parity I1：所有面板共用一套导航语法。"""

    def test_wrap_step(self):
        self.assertEqual(tui.wrap_step(0, -1, 5), 4)
        self.assertEqual(tui.wrap_step(4, 1, 5), 0)
        self.assertEqual(tui.wrap_step(2, 1, 5), 3)
        self.assertEqual(tui.wrap_step(0, 1, 0), 0, "空列表不除零")

    def test_the_overlay_picker_wraps_and_its_window_follows(self):
        """/model 这类选择器：同一套语法。窗口跟着跳 —— 否则高亮会跑出可见范围。"""
        pump = tui.InputPump(stream=io.StringIO())
        rows = [f"m{index:02d}" for index in range(20)]
        pump.open_picker(rows, page=5)
        pump._handle_picker("up")
        self.assertEqual(pump._picker["index"], 19)
        pump._picker_snapshot()
        self.assertEqual(pump._picker["visible_top"], 15)
        pump._handle_picker("down")
        self.assertEqual(pump._picker["index"], 0)
        pump._picker_snapshot()
        self.assertEqual(pump._picker["visible_top"], 0)
        pump._close_picker(None)

    def test_the_mouse_wheel_still_stops_at_the_ends(self):
        """滚轮不是「选下一个」而是「滚动」：滚过头跳回顶部会让人找不到自己在哪。"""
        pump = tui.InputPump(stream=io.StringIO())
        pump.open_picker([f"m{index}" for index in range(5)], fullscreen=True)
        pump._handle_picker(tui.MouseEvent("scroll_up", 1, 1))
        self.assertEqual(pump._picker["index"], 0)
        pump._close_picker(None)

    def test_the_decision_gate_wraps(self):
        pump = tui.InputPump(stream=io.StringIO())
        pump.open_decision_gate(question="选哪个？", options=[
            {"label": "甲"}, {"label": "乙"}, {"label": "丙"}])
        pump._handle_decision_gate("up")
        self.assertEqual(pump._decision_gate["index"], 2)
        pump._handle_decision_gate("down")
        self.assertEqual(pump._decision_gate["index"], 0)


if __name__ == "__main__":
    unittest.main()
