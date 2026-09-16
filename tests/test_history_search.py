"""A6（SPEC-CC-parity）：Ctrl+R 反向增量搜索历史 —— 编辑器层契约。

行为对齐 bash / Claude Code 的 reverse-i-search：Ctrl+R 进入搜索；打字即过滤（从最近
往旧找包含子串的记录）；再按 Ctrl+R 跳到更旧的匹配；Enter/Tab 把匹配填进草稿（不提交）；
Esc 取消并还原原草稿；Backspace 收窄。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


HIST = ["git status", "python3 -m unittest tests", "git diff --stat", "ls -la", "git log --oneline"]


def editor(text=""):
    ed = tui.LineEditor(history=list(HIST))
    for ch in text:
        ed.handle(ch)
    return ed


class ReverseSearchTests(unittest.TestCase):
    def test_ctrl_r_enters_search_and_typing_filters_newest_first(self):
        ed = editor()
        ed.handle("ctrl-r")
        self.assertTrue(ed.searching)
        for ch in "git":
            ed.handle(ch)
        self.assertEqual(ed.text, "git log --oneline")
        self.assertIn("git", ed.snapshot().hint)
        self.assertIn("reverse", ed.snapshot().hint.lower())

    def test_ctrl_r_again_steps_to_older_match(self):
        ed = editor(); ed.handle("ctrl-r")
        for ch in "git": ed.handle(ch)
        ed.handle("ctrl-r"); self.assertEqual(ed.text, "git diff --stat")
        ed.handle("ctrl-r"); self.assertEqual(ed.text, "git status")
        ed.handle("ctrl-r"); self.assertEqual(ed.text, "git status", "到底后停住")

    def test_enter_accepts_into_draft_without_submitting(self):
        ed = editor(); ed.handle("ctrl-r")
        for ch in "unittest": ed.handle(ch)
        events = ed.handle("enter")
        self.assertFalse([e for e in events if e.kind == "submit"])
        self.assertFalse(ed.searching)
        self.assertEqual(ed.text, "python3 -m unittest tests")
        self.assertEqual(ed.cursor, len(ed.text))

    def test_escape_restores_the_original_draft(self):
        ed = editor("half typed"); ed.handle("ctrl-r")
        for ch in "git": ed.handle(ch)
        ed.handle("esc")
        self.assertFalse(ed.searching)
        self.assertEqual(ed.text, "half typed")

    def test_backspace_narrows_query_and_no_match_keeps_last_text(self):
        ed = editor(); ed.handle("ctrl-r")
        for ch in "gitzzz": ed.handle(ch)
        self.assertIn("无匹配", ed.snapshot().hint)
        for _ in range(3): ed.handle("backspace")
        self.assertEqual(ed.text, "git log --oneline")

    def test_search_does_not_emit_menu_or_submit_events(self):
        ed = editor(); ed.handle("ctrl-r")
        kinds = {e.kind for e in ed.handle("g")}
        self.assertTrue(kinds <= {"redraw"})


if __name__ == "__main__":
    unittest.main()
