"""A9（SPEC-CC-parity）：Ctrl+C 清空草稿；空草稿 1.5s 内连按两次退出；Esc 永不退出。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


def kinds(events):
    return [e.kind for e in events]


class CtrlCTests(unittest.TestCase):
    def test_ctrl_c_clears_a_draft_and_does_not_exit(self):
        ed = tui.LineEditor(); [ed.handle(c) for c in "draft"]
        ev = kinds(ed.handle("ctrl-c"))
        self.assertEqual(ed.text, ""); self.assertNotIn("eof", ev)

    def test_double_ctrl_c_on_empty_draft_exits(self):
        ed = tui.LineEditor()
        first = ed.handle("ctrl-c")
        self.assertNotIn("eof", kinds(first))
        self.assertIn("再按一次 Ctrl+C", ed.snapshot().hint)
        self.assertIn("eof", kinds(ed.handle("ctrl-c")))

    def test_slow_second_press_does_not_exit(self):
        ed = tui.LineEditor()
        with mock.patch.object(tui.time, "monotonic", side_effect=[100.0, 100.0, 103.0, 103.0, 103.0, 103.0]):
            ed.handle("ctrl-c")
            self.assertNotIn("eof", kinds(ed.handle("ctrl-c")))

    def test_esc_never_exits(self):
        ed = tui.LineEditor()
        self.assertNotIn("eof", kinds(ed.handle("esc")))
        self.assertNotIn("eof", kinds(ed.handle("esc")))

    def test_typing_after_first_press_cancels_the_exit_arm(self):
        ed = tui.LineEditor()
        ed.handle("ctrl-c"); ed.handle("x"); ed.handle("backspace")
        self.assertEqual(ed.text, "")
        # 期间有过草稿，再按一次只算"第一次"
        self.assertNotIn("eof", kinds(ed.handle("ctrl-c")))

    def test_busy_ctrl_c_cancels_the_turn(self):
        ed = tui.LineEditor(); ed.busy = True
        self.assertEqual(kinds(ed.handle("ctrl-c")), ["cancel"])


if __name__ == "__main__":
    unittest.main()
