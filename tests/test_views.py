"""core/views：ANSI 解析、按宽折行、transcript 追加/滚动/选区。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import screen, views  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_sgr_bold_256_truecolor_reset(self):
        segs = views.parse_ansi("a\x1b[1mb\x1b[38;5;214mc\x1b[38;2;1;2;3md\x1b[0me")
        self.assertEqual([t for t, _ in segs], ["a", "b", "c", "d", "e"])
        self.assertTrue(segs[1][1].bold)
        self.assertEqual(segs[2][1].fg, 214)
        self.assertEqual(segs[3][1].fg, (1, 2, 3))
        self.assertEqual(segs[4][1], screen.DEFAULT)

    def test_osc8_links_and_unknown_sequences_are_dropped(self):
        segs = views.parse_ansi("\x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\ \x1b[2Kplain")
        self.assertEqual(segs[0], ("link", screen.Style(link="https://x")))
        self.assertEqual(views.plain(segs), "link plain")

    def test_basic_colors_map_to_indices(self):
        segs = views.parse_ansi("\x1b[31mr\x1b[92mg\x1b[44mb")
        self.assertEqual([s.fg for _, s in segs][:2], [1, 10])
        self.assertEqual(segs[2][1].bg, 4)


class WrapTests(unittest.TestCase):
    def test_wraps_by_display_width_keeping_wide_chars_whole(self):
        rows = views.wrap_segments([("ab中文de", screen.DEFAULT)], 5)
        self.assertEqual([views.plain(r) for r in rows], ["ab中", "文de"])

    def test_style_survives_a_wrap(self):
        rows = views.wrap_segments([("abcdef", screen.Style(bold=True))], 4)
        self.assertTrue(all(s.bold for r in rows for _, s in r))
        self.assertEqual(len(rows), 2)


class TranscriptTests(unittest.TestCase):
    def test_streaming_chunks_join_into_lines_with_partial_tail(self):
        t = views.Transcript()
        t.append_text("hel"); t.append_text("lo\nwor")
        self.assertEqual(views.plain(t.lines[0]), "hello")
        self.assertEqual(t.partial, "wor")
        self.assertEqual(views.plain(t.visual_rows(80)[-1]), "wor")
        t.finish_line()
        self.assertEqual(len(t.lines), 2)
        self.assertEqual(t.plain_text(), "hello\nwor")

    def test_style_carries_across_chunk_boundaries(self):
        t = views.Transcript()
        t.append_text("\x1b[1mbo"); t.append_text("ld\x1b[0m x\n")
        self.assertTrue(all(s.bold for tx, s in t.lines[0] if tx.strip() != "x"))

    def test_visible_window_and_clamped_offset(self):
        rows = [[(str(i), screen.DEFAULT)] for i in range(10)]
        window, off = views.visible_window(rows, 4, 0)
        self.assertEqual([views.plain(r) for r in window], ["6", "7", "8", "9"])
        window, off = views.visible_window(rows, 4, 100)
        self.assertEqual(off, 6)
        self.assertEqual(views.plain(window[0]), "0")

    def test_selection_text_spans_rows_by_columns(self):
        rows = [[("hello world", screen.DEFAULT)], [("中文 second", screen.DEFAULT)]]
        self.assertEqual(views.selection_text(rows, (0, 6), (1, 1)), "world\n中")
        self.assertEqual(views.selection_text(rows, (1, 1), (0, 6)), "world\n中", "顺序无关")


if __name__ == "__main__":
    unittest.main()
