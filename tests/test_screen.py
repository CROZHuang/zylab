"""core/screen：缓冲写入（裁剪、宽字符）、差分只发变化行、SGR 最小切换、同步输出包裹、无变化不发。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import screen  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x1b]*\x1b\\")


def plain(s):
    return ANSI.sub("", s)


class BufferTests(unittest.TestCase):
    def test_put_clips_at_right_edge_and_returns_next_col(self):
        b = screen.Buffer(2, 5)
        self.assertEqual(b.put(0, 0, "abcdefgh"), 5)
        self.assertEqual(b.text(0), "abcde")

    def test_wide_chars_take_two_cells_with_a_continuation(self):
        b = screen.Buffer(1, 6)
        b.put(0, 0, "中a")
        self.assertTrue(b.get(0, 0).wide)
        self.assertTrue(b.get(0, 1).continuation)
        self.assertEqual(b.get(0, 2).ch, "a")
        self.assertEqual(b.text(0), "中a")

    def test_wide_char_that_does_not_fit_is_replaced_by_a_space(self):
        b = screen.Buffer(1, 2)
        b.put(0, 0, "a中")
        self.assertEqual(b.text(0), "a")
        self.assertEqual(b.get(0, 1).ch, " ")

    def test_overwriting_half_a_wide_char_clears_the_whole_char(self):
        b = screen.Buffer(1, 4)
        b.put(0, 0, "中")
        b.put(0, 1, "x")
        self.assertEqual(b.get(0, 0), screen.BLANK)
        self.assertEqual(b.get(0, 1).ch, "x")

    def test_control_chars_never_reach_the_screen(self):
        b = screen.Buffer(1, 8)
        b.put(0, 0, "a\x1b[31mb\x07")
        self.assertNotIn("\x1b", b.text(0))
        self.assertNotIn("\x07", b.text(0))


class SgrTests(unittest.TestCase):
    def test_same_style_emits_nothing(self):
        self.assertEqual(screen.sgr_transition(screen.DEFAULT, screen.DEFAULT), "")

    def test_turning_an_attribute_off_resets_then_replays(self):
        prev = screen.Style(bold=True, fg=(255, 0, 0))
        nxt = screen.Style(fg=(255, 0, 0))
        out = screen.sgr_transition(prev, nxt)
        self.assertTrue(out.startswith("\x1b[0;"), out)
        self.assertIn("38;2;255;0;0", out)

    def test_truecolor_falls_back_to_256(self):
        out = screen.sgr_transition(screen.DEFAULT, screen.Style(fg=(255, 0, 0)), truecolor=False)
        self.assertIn("38;5;196", out)

    def test_links_open_and_close(self):
        on = screen.sgr_transition(screen.DEFAULT, screen.Style(link="https://x"))
        off = screen.sgr_transition(screen.Style(link="https://x"), screen.DEFAULT)
        self.assertIn("\x1b]8;;https://x\x1b\\", on)
        self.assertIn("\x1b]8;;\x1b\\", off)


class RendererTests(unittest.TestCase):
    def frame(self, lines, rows=4, cols=10):
        b = screen.Buffer(rows, cols)
        for i, line in enumerate(lines):
            b.put(i, 0, line)
        return b

    def test_first_frame_is_a_full_repaint_wrapped_in_sync(self):
        r = screen.Renderer()
        out = r.render(self.frame(["hello", "", "世界"]), cursor=(3, 0))
        self.assertTrue(out.startswith(screen.SYNC_ON) and out.endswith(screen.SYNC_OFF))
        self.assertIn("\x1b[2J", out)
        self.assertIn("hello", plain(out))
        self.assertIn("世界", plain(out))
        self.assertIn("\x1b[4;1H", out)                    # 光标停在 (3,0)

    def test_second_frame_sends_only_changed_rows(self):
        r = screen.Renderer(sync=False)
        r.render(self.frame(["hello", "keep", "世界"]))
        out = r.render(self.frame(["hallo", "keep", "世界"]))
        self.assertIn("\x1b[1;2H", out, "从第 1 行第 2 列开始改")
        self.assertNotIn("keep", plain(out))
        self.assertNotIn("世界", plain(out))
        self.assertEqual(plain(out).strip(), "a")

    def test_change_starting_inside_a_wide_char_backs_up_to_its_first_cell(self):
        r = screen.Renderer(sync=False)
        r.render(self.frame(["中b"]))
        out = r.render(self.frame(["国b"]))
        self.assertIn("\x1b[1;1H", out)
        self.assertIn("国", plain(out))

    def test_no_change_sends_nothing(self):
        r = screen.Renderer()
        f = self.frame(["same"])
        r.render(f, cursor=(0, 4))
        self.assertEqual(r.render(self.frame(["same"]), cursor=(0, 4)), "")

    def test_resize_forces_full_repaint(self):
        r = screen.Renderer(sync=False)
        r.render(self.frame(["x"], rows=2, cols=5))
        out = r.render(self.frame(["x"], rows=3, cols=5))
        self.assertIn("\x1b[2J", out)

    def test_styles_change_only_when_needed(self):
        r = screen.Renderer(sync=False)
        b = screen.Buffer(1, 6)
        b.put(0, 0, "ab", screen.Style(bold=True))
        b.put(0, 2, "cd", screen.Style(bold=True))
        b.put(0, 4, "e")
        out = r.render(b)
        self.assertEqual(out.count("\x1b[1m"), 1, out)
        self.assertLessEqual(out.count("\x1b[0m"), 2, out)


if __name__ == "__main__":
    unittest.main()
