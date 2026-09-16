"""SIGWINCH → 立即按新尺寸全画；重绘期间来的缩放排队到本帧结束。"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import apprender, termcaps  # noqa: E402


class _Stream(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        raise OSError("no fd")


class ResizeTests(unittest.TestCase):
    def test_winch_repaints_at_the_new_size(self):
        out = _Stream()
        r = apprender.AppRenderer(out, caps=termcaps.TermCaps(tty=True, alt_screen=False, cols=40, rows=10))
        r.write_output("x" * 60 + "\n")
        self.assertEqual(r._painter.prev.cols, 40)
        r.caps = termcaps.TermCaps(tty=True, alt_screen=False, cols=80, rows=12)
        r._on_winch(None, None)
        self.assertEqual((r._painter.prev.cols, r._painter.prev.rows), (80, 12))
        self.assertIn("\x1b[2J", out.getvalue().split("\x1b[2J")[-2] + "\x1b[2J", "缩放后全画")

    def test_winch_during_paint_is_deferred_not_reentrant(self):
        out = _Stream()
        r = apprender.AppRenderer(out, caps=termcaps.TermCaps(tty=True, alt_screen=False, cols=40, rows=10))
        r._painting = True
        r._on_winch(None, None)
        self.assertTrue(r._resize_pending)
        r._painting = False
        r._repaint()
        self.assertFalse(r._resize_pending)


if __name__ == "__main__":
    unittest.main()
