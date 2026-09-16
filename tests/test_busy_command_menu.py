"""模型工作期间打 `/` 应列出当场可执行的命令。

此前 `_matches()` 里 `self.busy` 直接让菜单返回空 —— 而工作中恰恰是最需要它的
时候：哪些当场执行、哪些排到本轮结束，全靠用户自己记。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zylab
from core import tui


CHOICES = {
    "/usage": "用量", "/tasks": "任务", "/model": "模型",
    "/help": "帮助", "/compact": "压缩", "/exit": "退出",
}
LIVE = frozenset({"/usage", "/tasks", "/model"})


def editor(*, busy, live=LIVE, target=None):
    ed = tui.LineEditor(commands=CHOICES, live_commands=live)
    ed.busy = busy
    ed.target = target
    return ed


class BusyMenuTests(unittest.TestCase):
    def test_busy_slash_lists_only_live_commands(self):
        ed = editor(busy=True)
        ed.handle("/")
        self.assertEqual(
            [name for name, _ in ed._matches()],
            ["/model", "/tasks", "/usage"])

    def test_busy_menu_still_filters_by_prefix(self):
        ed = editor(busy=True)
        for char in "/ta":
            ed.handle(char)
        self.assertEqual([name for name, _ in ed._matches()], ["/tasks"])

    def test_deferred_commands_are_absent_while_busy(self):
        ed = editor(busy=True)
        for char in "/co":
            ed.handle(char)
        # /compact 会排到本轮结束，不该出现在"当场执行"的菜单里。
        self.assertEqual(ed._matches(), [])

    def test_idle_menu_is_unchanged(self):
        ed = editor(busy=False)
        ed.handle("/")
        self.assertEqual(len(ed._matches()), len(CHOICES))

    def test_attached_composer_stays_closed(self):
        """attach 到 child agent 时输入是发给它的，与 live 命令无关。"""
        ed = editor(busy=True, target="child-1")
        ed.handle("/")
        self.assertEqual(ed._matches(), [])

    def test_without_a_live_set_busy_behaves_as_before(self):
        ed = editor(busy=True, live=None)
        ed.handle("/")
        self.assertEqual(ed._matches(), [])


class MenuMatchesDispatcherTests(unittest.TestCase):
    """菜单和分发器必须永远一致 —— 两份清单迟早会漂。"""

    def test_every_offered_command_actually_runs_live(self):
        offered = [
            name for name in zylab.COMMANDS
            if zylab.resolve_live_command(name) is not None]
        self.assertTrue(offered)
        for name in offered:
            self.assertIsNotNone(
                zylab.resolve_live_command(name), name)

    def test_offered_set_equals_the_whitelist(self):
        offered = {
            zylab.resolve_live_command(name)[0]
            for name in zylab.COMMANDS
            if zylab.resolve_live_command(name) is not None}
        self.assertEqual(offered, set(zylab.LIVE_COMMANDS))

    def test_exit_is_never_offered_while_busy(self):
        self.assertIsNone(zylab.resolve_live_command("/exit"))
        self.assertIsNone(zylab.resolve_live_command("/quit"))


if __name__ == "__main__":
    unittest.main()
