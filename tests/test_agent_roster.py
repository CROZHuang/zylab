"""主输入框按 ↓ 进子代理名册（用户 2026-09-24：「cc 是从 main 的对话框键盘点击 下，
就可以选择到了」，并要求再对照那三张截图）。

照本机 Claude Code 2.1.281 二进制里的键位表与处理函数（Footer 上下文）：
- 输入框里光标在最后一行、不在翻历史、没有补全菜单时，↓ 把焦点移进名册，落在 main 那行；
- 名册里 ↑↓ 移动，最上面再 ↑ 回输入框；Enter 切到选中的那个（main 那行 = 回 main）；
  Esc 回输入框；切过去以后焦点仍留在名册里；
- 名册画在输入框和状态栏**下面**：● 是正在看的那个、◯ 是其余，❯ 是键盘焦点，
  耗时与 token 靠右；在看子代理时输入框上沿挂它的任务名，占位提示写「发给 @它」。
zylab 以前名册画在输入框**上面**，只能 Ctrl+T 开面板或 /agents attach 才进得去。
（CC 在名册最底一行按 ↓ 不动；这里绕回 main——用户要列表首尾相接。）
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tui
from tests.terminal_screen import parse_final_screen

import zylab as CLI


def roster(attached=None):
    return (
        tui.DockItem("◯ main" if attached else "● main", event="agent_detach",
                     viewed=attached is None, action="回 main" if attached else ""),
        tui.DockItem("◯ Kimi  调研北非网格  web_search", event="agent_attach",
                     value="a-1", viewed=attached == "a-1",
                     right="0m 05s · ↓ 1.2k", action="查看"),
        tui.DockItem("◯ GLM  读投影模块", event="agent_attach", value="a-2",
                     viewed=attached == "a-2", right="0m 03s", action="查看"),
    )


class RosterKeys(unittest.TestCase):
    def setUp(self):
        self.pump = tui.InputPump(stream=io.StringIO())
        self.pump.configure(dock=roster(), publish=False)

    def press(self, key):
        return self.pump._handle_line(key)

    def focus(self):
        return self.pump.snapshot().dock_selected

    def test_down_from_the_composer_lands_on_main(self):
        self.press("down")
        self.assertEqual(self.focus(), 0)

    def test_down_again_reaches_the_agent_and_enter_opens_it(self):
        self.press("down")
        self.press("down")
        self.assertEqual(self.focus(), 1)
        events = self.press("enter")
        self.assertEqual([(e.kind, e.value) for e in events], [("agent_attach", "a-1")])
        self.assertEqual(self.focus(), 1, "切过去以后焦点留在名册里（CC 同）")

    def test_up_from_main_goes_back_to_the_composer(self):
        self.press("down")
        self.press("up")
        self.assertIsNone(self.focus())

    def test_down_past_the_last_row_wraps_to_main(self):
        for _ in range(4):
            self.press("down")
        self.assertEqual(self.focus(), 0)

    def test_escape_only_leaves_the_roster(self):
        self.pump.configure(target="a-1", dock=roster("a-1"), publish=False)
        self.pump.set_agent_view_mode(True)
        self.press("down")
        events = self.press("esc")
        self.assertIsNone(self.focus())
        self.assertNotIn("detach", [e.kind for e in events],
                         "Esc 先退出名册，不是直接回 main")

    def test_typing_goes_back_to_the_draft(self):
        self.pump.editor.replace("草稿")
        self.press("down")
        self.press("x")
        self.assertIsNone(self.focus())
        self.assertEqual(self.pump.editor.text, "草稿x")

    def test_enter_on_the_row_you_are_viewing_changes_nothing(self):
        self.press("down")                      # main，正在看的就是它
        kinds = [e.kind for e in self.press("enter")]
        self.assertNotIn("agent_detach", kinds)
        self.assertNotIn("agent_attach", kinds)

    def test_enter_on_main_while_viewing_an_agent_goes_back(self):
        self.pump.configure(target="a-1", dock=roster("a-1"), publish=False)
        self.press("down")
        self.assertEqual([e.kind for e in self.press("enter")], ["agent_detach"])

    def test_history_comes_first(self):
        self.pump.editor.add_history("上一条")
        self.press("up")
        self.assertEqual(self.pump.editor.text, "上一条")
        self.press("down")                      # 先回到草稿
        self.assertIsNone(self.focus())
        self.assertEqual(self.pump.editor.text, "")
        self.press("down")                      # 草稿再往下才进名册
        self.assertEqual(self.focus(), 0)

    def test_while_main_is_working_down_goes_straight_to_the_roster(self):
        """主会话忙着时 ↑ 是取回排队的消息、↓ 不翻历史——↓ 直接进名册。"""
        self.pump.editor.add_history("上一条")
        self.pump.configure(busy=True, publish=False)
        self.press("down")
        self.assertEqual(self.focus(), 0)

    def test_the_slash_menu_keeps_its_arrows(self):
        pump = tui.InputPump(stream=io.StringIO(),
                             commands={"/help": "帮助", "/model": "模型"})
        pump.configure(dock=roster(), publish=False)
        pump.editor.replace("/")
        pump._handle_line("down")
        self.assertIsNone(pump.snapshot().dock_selected)
        self.assertEqual(pump.editor.menu_index, 1)

    def test_a_cursor_above_the_last_line_stays_in_the_draft(self):
        self.pump.editor.replace("第一行\n第二行")
        self.pump.editor.set_cursor(2)
        self.press("down")
        self.assertIsNone(self.focus())

    def test_nothing_to_select_nothing_happens(self):
        pump = tui.InputPump(stream=io.StringIO())
        pump._handle_line("down")
        self.assertIsNone(pump.snapshot().dock_selected)

    def test_focus_stays_on_the_same_agent_when_the_roster_changes(self):
        for _ in range(3):
            self.press("down")                  # a-2
        rows = roster()
        grown = (rows[0],
                 tui.DockItem("◯ Qwen  新来的", event="agent_attach", value="a-3",
                              viewed=False),
                 rows[1], rows[2])
        self.pump.configure(dock=grown, publish=False)
        snapshot = self.pump.snapshot()
        self.assertEqual(snapshot.dock[snapshot.dock_selected].value, "a-2")

    def test_focus_is_dropped_when_the_roster_goes_away(self):
        self.press("down")
        self.pump.configure(dock=(), publish=False)
        self.assertIsNone(self.focus())


class RosterOnScreen(unittest.TestCase):
    COLS, ROWS = 80, 16

    def render(self, **fields):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        values = {"mode": "line", "prompt": "› ",
                  "status": "chat · gpt-6-sol@boyue", "dock": roster()}
        values.update(fields)
        with mock.patch.object(tui, "_cols", return_value=self.COLS), \
                mock.patch.object(tui, "_lines", return_value=self.ROWS):
            renderer.render(tui.InputSnapshot(**values))
        screen = parse_final_screen(stream.getvalue(), self.COLS, self.ROWS)
        return ["".join(cell.text for cell in row).rstrip()
                for row in screen["primary"]]

    @staticmethod
    def row_of(lines, needle):
        return next(index for index, line in enumerate(lines) if needle in line)

    def test_the_roster_sits_below_the_composer_and_the_status_line(self):
        lines = self.render()
        box_bottom = self.row_of(lines, "╰")
        status = self.row_of(lines, "gpt-6-sol@boyue")
        main = self.row_of(lines, "main")
        self.assertLess(box_bottom, status)
        self.assertLess(status, main)
        self.assertLess(main, self.row_of(lines, "调研北非网格"))

    def test_the_focused_row_carries_the_pointer(self):
        lines = self.render(dock_selected=1)
        self.assertTrue(lines[self.row_of(lines, "调研北非网格")].startswith("❯"))
        self.assertFalse(lines[self.row_of(lines, "main")].startswith("❯"))

    def test_it_says_how_to_get_in_and_how_to_get_out(self):
        idle = lines = self.render()
        self.assertIn("↓", lines[self.row_of(idle, "main")], "名册上写着按 ↓ 进来")
        focused = "\n".join(self.render(dock_selected=1))
        self.assertIn("Enter 查看", focused)
        self.assertIn("Esc", focused)

    def test_elapsed_time_and_tokens_sit_at_the_right_edge(self):
        lines = self.render()
        row = lines[self.row_of(lines, "调研北非网格")]
        self.assertTrue(row.endswith("0m 05s · ↓ 1.2k"))
        self.assertGreaterEqual(tui.display_width(row), self.COLS - 2)

    def test_while_viewing_an_agent_the_composer_names_it(self):
        lines = self.render(target="a-1", target_label="调研北非网格",
                            dock=roster("a-1"))
        self.assertIn("调研北非网格", lines[self.row_of(lines, "╭")],
                      "任务名挂在输入框上沿")
        self.assertIn("@调研北非网格", "\n".join(lines), "占位提示写明发给谁")


class RosterRows(unittest.TestCase):
    """会话层给名册的行：main 是一行、正在看的那个标出来、老的也不丢。"""

    RECORDS = [
        {"id": "a-1", "kind": "subagent", "state": "running", "name": "调研北非网格",
         "model": "kimi-k3", "gateway": "boyue", "parent_turn_id": "turn-live"},
        {"id": "a-2", "kind": "subagent", "state": "completed", "name": "读投影模块",
         "model": "glm-5.3", "gateway": "boyue", "parent_turn_id": "turn-live"},
        {"id": "a-old", "kind": "subagent", "state": "completed", "name": "上一轮的",
         "model": "glm-5.3", "gateway": "boyue", "parent_turn_id": "turn-old"},
    ]

    def items(self, attached=None):
        records = {row["id"]: dict(row) for row in self.RECORDS}

        class View:
            _workflow_current = None
            _agent_activity = {}
            attached_agent_id = attached
            controller = type("Controller", (), {"current_turn_id": "turn-live"})()

            @staticmethod
            def list_agents(limit=100):
                del limit
                return [dict(row) for row in records.values()]

            @staticmethod
            def agent_record(identifier=None, refresh=False):
                del refresh
                return dict(records[identifier])

        return CLI._composer_dock_items(View())

    def test_main_is_a_row_you_can_select(self):
        main = self.items()[0]
        self.assertEqual(main.event, "agent_detach")
        self.assertTrue(main.viewed)
        self.assertIn("● main", main.text)

    def test_the_agent_you_are_viewing_is_marked_and_main_is_not(self):
        items = self.items(attached="a-1")
        self.assertFalse(items[0].viewed)
        self.assertIn("◯ main", items[0].text)
        self.assertEqual([item.value for item in items if item.viewed], ["a-1"])

    def test_the_agent_you_are_viewing_stays_listed_even_when_it_is_old(self):
        self.assertIn("a-old", [item.value for item in self.items(attached="a-old")])

    def test_rows_put_time_on_the_right_and_say_how_it_ended(self):
        done = next(item for item in self.items() if item.value == "a-2")
        self.assertIn("完成", done.text)
        self.assertRegex(done.right, r"\d+m \d\ds")


class SwitchingAgents(unittest.TestCase):
    """名册里 Enter：切到那个子代理；已经在看它就不动；有整屏视图时不往主会话里塞预览。"""

    def make_session(self, attached=None):
        sess = object.__new__(CLI.Session)
        sess.attached_agent_id = attached
        sess.renderer = mock.Mock()
        sess.attach_agent = mock.Mock(return_value={"id": "a-2"})
        return sess

    def test_switching_goes_straight_to_the_other_agent(self):
        sess = self.make_session(attached="a-1")
        with mock.patch.object(CLI, "show_agent_preview") as preview:
            sess.switch_agent("a-2")
        sess.attach_agent.assert_called_once_with("a-2")
        preview.assert_not_called()

    def test_the_agent_you_are_already_viewing_is_left_alone(self):
        sess = self.make_session(attached="a-1")
        sess.switch_agent("a-1")
        sess.attach_agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
