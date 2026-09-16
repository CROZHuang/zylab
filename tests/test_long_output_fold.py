"""C4（SPEC-CC-parity）：长输出永不刷屏 —— 工具结果默认折叠成一行，失败结果保留原文。"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


class LongOutputFoldTests(unittest.TestCase):
    def setUp(self):
        self.r = tui.TerminalRenderer(io.StringIO())

    def test_two_thousand_lines_fold_to_one_line_with_expand_hint(self):
        out = self.r._fold_transcript_result("\n".join(f"line {i}" for i in range(2000)), "c1", "bash")
        self.assertEqual(out.count("\n"), 0)
        self.assertIn("2,000 lines", out)
        self.assertIn("/expand c1", out)

    def test_one_very_long_line_is_truncated_not_wrapped_forever(self):
        out = self.r._fold_transcript_result("x" * 5000, "c2", "read_file")
        self.assertLess(len(out), 140)
        self.assertIn("/expand c2", out)

    def test_failures_are_not_folded_away(self):
        body = "Traceback\n  File x\nValueError: boom\n[exit 1]"
        self.assertEqual(self.r._fold_transcript_result(body, "c3", "bash"), body)


if __name__ == "__main__":
    unittest.main()
