"""粘贴一大段文字必须变成一条多行 prompt，而不是 N 条 prompt。

用户报告的形态：复制 10 行贴进输入框，模型收到 10 条分别的 prompt。
根因是终端里"粘贴"和"手速极快地打字"本是同一件事 —— 除非开启 DEC 私有模式
2004（bracketed paste），否则每个换行都会被 KEYS 映射成 enter。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


def feed(payload, *, timeout=0.2, reads=1):
    """把字节喂进 read_key，返回读出的前 reads 个事件。"""
    read_fd, write_fd = os.pipe()
    out = []
    try:
        os.write(write_fd, payload)
        with os.fdopen(read_fd, "rb", buffering=0) as stream:
            read_fd = None
            for _ in range(reads):
                out.append(tui.read_key(timeout=timeout, stream=stream))
    finally:
        os.close(write_fd)
        if read_fd is not None:
            os.close(read_fd)
    return out


TEN_LINES = "\n".join(f"line {i}" for i in range(1, 11))


class RegressionTests(unittest.TestCase):
    """用户报的那个 bug 本身。"""

    def test_ten_line_paste_is_one_event(self):
        payload = (tui.PASTE_START + TEN_LINES + tui.PASTE_END).encode()
        events = feed(payload, reads=1)
        self.assertIsInstance(events[0], tui.PasteEvent)
        self.assertEqual(events[0].text, TEN_LINES)
        self.assertFalse(events[0].truncated)
        self.assertEqual(events[0].text.count("\n"), 9)

    def test_paste_consumes_exactly_its_own_bytes(self):
        """粘贴之后紧跟的按键不能被吞掉，也不能被算进粘贴内容。"""
        payload = (tui.PASTE_START + "abc" + tui.PASTE_END + "\r").encode()
        events = feed(payload, reads=2)
        self.assertEqual(events[0].text, "abc")
        self.assertEqual(events[1], "enter")

    def test_without_bracketing_newlines_are_still_enter(self):
        """对照组：没有包裹标记时，换行仍是 enter —— 这正是必须在终端侧
        开启 2004 的原因，光改解码器不够。"""
        events = feed(b"a\nb\n", reads=4)
        self.assertEqual(events, ["a", "enter", "b", "enter"])


class SanitizeTests(unittest.TestCase):
    def test_crlf_is_normalized(self):
        self.assertEqual(tui._sanitize_paste("a\r\nb\rc"), "a\nb\nc")

    def test_escape_sequences_are_stripped(self):
        """安全边界：剪贴板内容可能来自任意网页，绝不能被当成终端指令。"""
        self.assertEqual(
            tui._sanitize_paste("red\x1b[31mtext\x1b[0m"), "redtext")
        self.assertEqual(tui._sanitize_paste("a\x1b]0;title\x07b"), "ab")

    def test_control_characters_are_dropped_but_tab_survives(self):
        self.assertEqual(tui._sanitize_paste("a\x00\x07b\tc"), "ab\tc")

    def test_emoji_joiners_survive(self):
        """ZWJ 与变体选择符是 emoji 组合的一部分，_cell_width 专门处理过。"""
        family = "\U0001f468‍\U0001f469‍\U0001f467"
        self.assertEqual(tui._sanitize_paste(family), family)
        self.assertEqual(tui._sanitize_paste("❤️"), "❤️")

    def test_trailing_newlines_are_trimmed_only_at_the_end(self):
        self.assertEqual(tui._sanitize_paste("a\n\nb\n\n"), "a\n\nb")


class RobustnessTests(unittest.TestCase):
    def test_missing_terminator_does_not_hang(self):
        events = feed((tui.PASTE_START + "orphan").encode(), timeout=0.2)
        self.assertIsInstance(events[0], tui.PasteEvent)
        self.assertEqual(events[0].text, "orphan")
        self.assertTrue(events[0].truncated)

    def test_oversized_paste_is_truncated_not_unbounded(self):
        original = tui.PASTE_MAX_CHARS
        tui.PASTE_MAX_CHARS = 16
        try:
            payload = (tui.PASTE_START + "x" * 200 + tui.PASTE_END).encode()
            events = feed(payload)
        finally:
            tui.PASTE_MAX_CHARS = original
        self.assertTrue(events[0].truncated)
        self.assertLess(len(events[0].text), 200)


class EditorTests(unittest.TestCase):
    def test_paste_inserts_without_submitting(self):
        editor = tui.LineEditor()
        events = editor.handle(tui.PasteEvent(text=TEN_LINES))
        # A3：长粘贴在草稿里折叠成占位符，提交时原样展开
        self.assertEqual(editor.text, "[Pasted text #1 +10 lines]")
        self.assertEqual(editor.cursor, len(editor.text))
        self.assertFalse([e for e in events if e.kind == "submit"])
        submit = [e for e in editor.handle("enter") if e.kind == "submit"]
        self.assertEqual(submit[0].text, TEN_LINES)

    def test_short_paste_stays_verbatim(self):
        editor = tui.LineEditor()
        editor.handle(tui.PasteEvent(text="a\nb\nc"))
        self.assertEqual(editor.text, "a\nb\nc")

    def test_two_folds_are_numbered_and_both_expand(self):
        editor = tui.LineEditor()
        editor.handle(tui.PasteEvent(text=TEN_LINES))
        for ch in " and ":
            editor.handle(ch)
        editor.handle(tui.PasteEvent(text="x" * 700))
        self.assertIn("[Pasted text #2 +1 lines]", editor.text)
        submit = [e for e in editor.handle("enter") if e.kind == "submit"]
        self.assertEqual(submit[0].text, TEN_LINES + " and " + "x" * 700)

    def test_ctrl_c_drops_folded_pastes(self):
        editor = tui.LineEditor()
        editor.handle(tui.PasteEvent(text=TEN_LINES))
        editor.handle("ctrl-c")
        editor.handle("z")
        submit = [e for e in editor.handle("enter") if e.kind == "submit"]
        self.assertEqual(submit[0].text, "z")

    def test_paste_lands_at_the_cursor(self):
        editor = tui.LineEditor()
        for char in "ad":
            editor.handle(char)
        editor.cursor = 1
        editor.handle(tui.PasteEvent(text="bc"))
        self.assertEqual(editor.text, "abcd")
        self.assertEqual(editor.cursor, 3)

    def test_empty_paste_is_a_noop(self):
        editor = tui.LineEditor()
        editor.handle("x")
        self.assertEqual(editor.handle(tui.PasteEvent(text="")), [])
        self.assertEqual(editor.text, "x")

    def test_pasted_slash_command_does_not_run(self):
        """粘进来的 /command 是文本，不是命令。"""
        editor = tui.LineEditor()
        events = editor.handle(tui.PasteEvent(text="/quit\nsecond line"))
        self.assertEqual(editor.text, "/quit\nsecond line")
        self.assertFalse([e for e in events if e.kind == "submit"])


class TerminalModeTests(unittest.TestCase):
    def setUp(self):
        tui.raw_mode._paste_depth = 0

    tearDown = setUp

    @staticmethod
    def capture(fn):
        buf = io.StringIO()
        saved = sys.stdout
        sys.stdout = buf
        try:
            fn()
        finally:
            sys.stdout = saved
        return buf.getvalue()

    def test_nesting_keeps_paste_on_until_the_outermost_exit(self):
        """raw_mode 有四个使用点（InputPump / EscWatcher / read_line 两处）
        且会嵌套。内层退出若关掉粘贴模式，症状是"用过选择器之后粘贴又坏了"。
        """
        toggle = tui.raw_mode._set_bracketed_paste
        out = self.capture(lambda: [
            toggle(True),    # 外层进入 -> 开
            toggle(True),    # 内层进入 -> 不重复开
            toggle(False),   # 内层退出 -> **不能关**
        ])
        self.assertEqual(out, tui.PASTE_ON)
        self.assertEqual(
            self.capture(lambda: toggle(False)), tui.PASTE_OFF)

    def test_unbalanced_close_does_not_go_negative(self):
        toggle = tui.raw_mode._set_bracketed_paste
        self.assertEqual(self.capture(lambda: toggle(False)), "")
        self.assertEqual(tui.raw_mode._paste_depth, 0)

    def test_raw_mode_toggles_dec_2004(self):
        buf = io.StringIO()
        saved = sys.stdout
        sys.stdout = buf
        try:
            tui.raw_mode._set_bracketed_paste(True)
            tui.raw_mode._set_bracketed_paste(False)
        finally:
            sys.stdout = saved
        self.assertEqual(buf.getvalue(), tui.PASTE_ON + tui.PASTE_OFF)

    def test_toggle_survives_a_dead_stdout(self):
        """终端已经关掉时不能因为写不进去就崩掉整个退出流程。"""
        saved = sys.stdout
        sys.stdout = None
        try:
            tui.raw_mode._set_bracketed_paste(False)
        finally:
            sys.stdout = saved


if __name__ == "__main__":
    unittest.main()
