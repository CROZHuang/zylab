"""忙时打开 / 列表，spinner 每秒的 configure(busy=True) 不能把光标打回第一行（2026-09-04 反馈）。"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tui

COMMANDS = {
    "/usage": "用量", "/cost": "花费", "/context": "上下文", "/model": "模型",
    "/auto": "自动", "/workflow": "workflow", "/tasks": "任务", "/compact": "压缩",
}
LIVE = frozenset({"/usage", "/cost", "/context", "/model", "/auto", "/workflow", "/tasks"})


def busy_pump():
    pump = tui.InputPump(stream=io.StringIO(), commands=COMMANDS, live_commands=LIVE)
    pump.configure(busy=True, activity="思考中 1s", status="s", publish=False)
    pump.editor.replace("/")
    return pump


class MenuCursorTests(unittest.TestCase):
    def test_activity_ticks_keep_the_selected_row(self):
        pump = busy_pump()
        options = pump.editor.snapshot().options
        self.assertGreaterEqual(len(options), 3, options)
        pump.editor.menu_index = 2
        for second in range(2, 6):
            pump.configure(busy=True, activity=f"思考中 {second}s", status="s", publish=False)
            self.assertEqual(pump.editor.menu_index, 2, f"第 {second} 秒刷新把光标打回去了")
            self.assertEqual(pump.editor.snapshot().selected, 2)
        # 状态栏/队列/dock 变化同样不能动光标
        pump.configure(busy=True, queued=("↪ steer x",), dock=(), status="changed", publish=False)
        self.assertEqual(pump.editor.menu_index, 2)

    def test_busy_transition_still_resets_because_the_command_set_changes(self):
        pump = busy_pump()
        pump.editor.menu_index = 2
        pump.configure(busy=False, publish=False)
        self.assertEqual(pump.editor.menu_index, 0)
        pump.editor.menu_index = 1
        pump.configure(busy=True, publish=False)
        self.assertEqual(pump.editor.menu_index, 0)

    def test_unchanged_command_and_skill_tables_keep_the_cursor(self):
        pump = busy_pump()
        pump.editor.menu_index = 2
        pump.set_commands(dict(COMMANDS), publish=False)
        self.assertEqual(pump.editor.menu_index, 2)
        pump.set_skills({}, publish=False)
        pump.set_cwd(pump.editor.cwd, publish=False)
        self.assertEqual(pump.editor.menu_index, 2)
        pump.set_commands({**COMMANDS, "/new": "新命令"}, publish=False)
        self.assertEqual(pump.editor.menu_index, 0, "命令表真变了才归零")

    def test_cursor_is_clamped_when_the_menu_shrinks(self):
        pump = busy_pump()
        pump.editor.menu_index = 6
        pump.editor.replace("/co")
        pump.editor.menu_index = 5
        snapshot = pump.editor.snapshot()
        self.assertEqual(snapshot.selected, len(snapshot.options) - 1)


if __name__ == "__main__":
    unittest.main()
