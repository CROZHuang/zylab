"""attach 即整屏切到子代理的记录（用户 2026-09-24 看了 Claude Code 2.1.270 的截图要的）。

CC 的做法：选中一个子代理，整个屏幕换成它自己的记录——开头是主代理交给它的原话，之后是
它每一步的工具调用；输入框改发给它；标题栏换成它的任务名并变色；回 main 时主会话原样还在。

zylab 以前 attach 只改了输入框的收件人，屏幕上还是主会话。

断言都落在**屏幕上看得见什么**（terminal_screen 把输出还原成主屏 / 备用屏的最终画面），
不落在渲染器的内部字段上。
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

COLS, ROWS = 90, 22
BRIEF = "调研北非网格的投影参数。只读，不改仓库。"
TITLE = "○ general-purpose  调研北非网格  0m 05s · ↓ 1.2k"


def step(index):
    call = f"c{index}"
    return [
        {"role": "assistant", "content": f"第 {index} 步：查资料", "tool_calls": [{
            "id": call, "type": "function",
            "function": {"name": "web_search", "arguments": f'{{"q": "查询{index}"}}'}}]},
        {"role": "tool", "tool_call_id": call, "content": f"结果{index}"},
    ]


def child(steps):
    messages = [{"role": "system", "content": "你是子代理"},
                {"role": "user", "content": BRIEF}]
    for index in range(1, steps + 1):
        messages.extend(step(index))
    return messages


class AgentViewOnScreen(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.renderer = tui.TerminalRenderer(self.stream)
        self.snap = tui.InputSnapshot(mode="line", prompt="agent› ", text="",
                                      cursor=0, target="run-1")

    @contextlib.contextmanager
    def sized(self):
        with mock.patch.object(tui, "_cols", return_value=COLS), \
                mock.patch.object(tui, "_lines", return_value=ROWS):
            yield

    def screen(self):
        result = parse_final_screen(self.stream.getvalue(), COLS, ROWS)
        for name in ("primary", "alternate"):
            result[name] = ["".join(cell.text for cell in row).rstrip()
                            for row in result[name]]
        return result

    @staticmethod
    def text(lines):
        return "\n".join(lines)

    def open(self, messages):
        with self.sized():
            self.renderer.write_output("主会话的最后一行\r\n")
            self.renderer.render(self.snap)
            self.renderer.open_agent_view(title=TITLE, brief=BRIEF,
                                          messages=messages, snapshot=self.snap)

    def test_attaching_switches_the_whole_screen_to_the_agent(self):
        self.open(child(1))
        screen = self.screen()
        self.assertEqual(screen["active_buffer"], "alternate")
        shown = self.text(screen["alternate"])
        self.assertIn("任务说明（主代理交给它的原话）", shown)
        self.assertIn(BRIEF, shown)
        self.assertIn("web_search", shown)
        self.assertIn("结果1", shown)
        header = screen["alternate"][0]
        self.assertIn("调研北非网格", header, "标题行说清你在看谁（不许被按键说明挤掉）")
        self.assertIn("Esc 回 main", header, "也说清怎么回去")
        self.assertNotIn("主会话的最后一行", shown)

    def test_the_brief_is_shown_once(self):
        self.open(child(1))
        shown = self.text(self.screen()["alternate"])
        self.assertEqual(shown.count(BRIEF), 1,
                         "它的第一条用户消息就是那段原话，别列两遍")

    def test_main_output_waits_and_comes_back_on_return(self):
        """看子代理的时候，主会话的输出不画在它上面；回 main 时一行不少地补上。"""
        self.open(child(1))
        with self.sized():
            self.renderer.write_output("主会话后来说的话\r\n")
            self.renderer.render(self.snap)
        shown = self.text(self.screen()["alternate"])
        self.assertNotIn("主会话后来说的话", shown)
        self.assertIn("main +1 条新输出", shown)
        with self.sized():
            self.assertTrue(self.renderer.close_agent_view(self.snap))
        screen = self.screen()
        self.assertEqual(screen["active_buffer"], "primary")
        main = self.text(screen["primary"])
        self.assertIn("主会话的最后一行", main)
        self.assertIn("主会话后来说的话", main)
        self.assertFalse(self.renderer.agent_view_active)

    def test_new_steps_show_up_while_you_watch(self):
        self.open(child(1))
        with self.sized():
            self.renderer.update_agent_view(messages=child(2), snapshot=self.snap)
        self.assertIn("结果2", self.text(self.screen()["alternate"]))

    def test_a_view_scrolled_up_stays_put_when_the_agent_moves_on(self):
        """往上翻着看的时候它又走了一步：画面不能跳到底部。"""
        self.open(child(20))
        with self.sized():
            self.renderer.scroll_history("page_up", self.snap)
        before = [row for row in self.screen()["alternate"][1:] if row.strip()][0]
        with self.sized():
            self.renderer.update_agent_view(messages=child(21), snapshot=self.snap)
        after = [row for row in self.screen()["alternate"][1:] if row.strip()][0]
        self.assertEqual(before, after)

    def test_the_agent_view_does_not_put_the_input_pump_in_history_mode(self):
        """历史浏览模式下打字会关掉视图；而在子代理视图里打的字是发给它的。"""
        self.open(child(1))
        self.assertFalse(self.renderer.history_active)
        self.assertTrue(self.renderer.agent_view_active)


class SessionOpensAndFollowsTheView(unittest.TestCase):
    """attach 就打开视图、detach 就关；视图开着时每秒看一眼子代理的记录，有新东西才重画。"""

    def make_session(self, messages):
        import zylab
        sess = object.__new__(zylab.Session)
        sess.pump = tui.InputPump(stream=io.StringIO())
        sess.renderer = mock.Mock()
        sess.renderer.agent_view_active = True
        sess.attached_agent_id = "run-1"
        sess._agent_activity = {}
        sess._agent_tokens = lambda record: 1200
        record = {"id": "run-1", "task": BRIEF, "label": "调研北非网格",
                  "state": "running", "kind": "subagent"}
        sess.agent_record = lambda identifier=None, refresh=False: dict(record)
        store = {"messages": list(messages)}
        sess.agent_workspace = mock.Mock()
        sess.agent_workspace.transcript = lambda run_id: {"messages": list(store["messages"])}
        return sess, store, record

    def test_opening_hands_the_brief_and_the_transcript_to_the_renderer(self):
        sess, _, record = self.make_session(child(1))
        self.assertTrue(sess.open_agent_view(record))
        kwargs = sess.renderer.open_agent_view.call_args.kwargs
        self.assertEqual(kwargs["brief"], BRIEF)
        self.assertEqual(len(kwargs["messages"]), len(child(1)))
        self.assertIn("调研北非网格", kwargs["title"])
        self.assertTrue(sess.pump._agent_view_mode)

    def test_refresh_redraws_only_when_something_changed(self):
        sess, store, record = self.make_session(child(1))
        with mock.patch("zylab._agent_dock_row", return_value="○ 同一行"):
            sess.open_agent_view(record)
            self.assertFalse(sess.refresh_agent_view(force=True), "没变化就不重画")
            store["messages"] = child(2)
            self.assertTrue(sess.refresh_agent_view(force=True))
        messages = sess.renderer.update_agent_view.call_args.kwargs["messages"]
        self.assertEqual(len(messages), len(child(2)))

    def test_refresh_is_throttled(self):
        sess, store, record = self.make_session(child(1))
        with mock.patch("zylab._agent_dock_row", return_value="○ 同一行"):
            sess.open_agent_view(record)
            store["messages"] = child(2)
            self.assertFalse(sess.refresh_agent_view(), "一秒之内不去读记录")

    def test_a_view_closed_behind_our_back_releases_the_keys(self):
        """主会话输出攒太多时渲染器会自己回主屏：输入泵也得回到正常模式。"""
        sess, _, record = self.make_session(child(1))
        sess.open_agent_view(record)
        sess.renderer.agent_view_active = False
        sess.refresh_agent_view(force=True)
        self.assertFalse(sess.pump._agent_view_mode)


class KeysInTheAgentView(unittest.TestCase):
    def setUp(self):
        self.pump = tui.InputPump(stream=io.StringIO())
        self.pump.configure(target="run-1", publish=False)
        self.pump.set_agent_view_mode(True)

    def kinds(self, key):
        return [event.kind for event in self.pump._handle_line(key)]

    def test_paging_scrolls_the_agent(self):
        self.assertEqual(self.kinds("pageup"), ["history_scroll"])
        self.assertEqual(self.kinds("pagedown"), ["history_scroll"])

    def test_typing_goes_to_the_composer_not_to_history(self):
        kinds = self.kinds("x")
        self.assertNotIn("history_close", kinds)
        self.assertEqual(self.pump.editor.text, "x")

    def test_escape_goes_back_to_main(self):
        self.assertIn("detach", self.kinds("esc"))

    def test_without_the_view_paging_is_unchanged(self):
        self.pump.set_agent_view_mode(False)
        self.pump.set_history_mode(False)
        self.assertEqual(self.kinds("pagedown"), [], "不在视图里时 PgDn 仍然什么都不做")


if __name__ == "__main__":
    unittest.main()
