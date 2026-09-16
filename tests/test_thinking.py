"""D1：<think> 分离器与思考累计器的纯逻辑契约。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import thinking


def run(chunks):
    f = thinking.ThinkFilter()
    vis, th = "", ""
    for c in chunks:
        v, t = f.feed(c); vis += v; th += t
    v, t = f.finish(); vis += v; th += t
    return vis, th


class ThinkFilterTests(unittest.TestCase):
    def test_leading_think_block_is_separated(self):
        self.assertEqual(run(["<think>abc</think>hello"]), ("hello", "abc"))

    def test_tags_split_across_chunks(self):
        self.assertEqual(run(["<th", "ink>a", "bc</th", "ink>hel", "lo"]), ("hello", "abc"))

    def test_close_tag_split_at_every_position(self):
        full = "<think>xyz</think>ok"
        for i in range(1, len(full)):
            with self.subTest(i=i):
                self.assertEqual(run([full[:i], full[i:]]), ("ok", "xyz"))

    def test_plain_text_is_visible_immediately(self):
        f = thinking.ThinkFilter()
        self.assertEqual(f.feed("hello"), ("hello", ""))

    def test_leading_whitespace_before_think_is_tolerated(self):
        self.assertEqual(run(["\n  <think>t</think>v"]), ("v", "t"))

    def test_literal_think_mid_text_is_not_thinking(self):
        self.assertEqual(run(["Note: the <think> tag"]), ("Note: the <think> tag", ""))

    def test_prefix_that_diverges_is_visible(self):
        self.assertEqual(run(["<thi", "s is text"]), ("<this is text", ""))

    def test_unclosed_think_stays_thinking(self):
        self.assertEqual(run(["<think>cut off"]), ("", "cut off"))

    def test_only_one_leading_block_is_recognized(self):
        self.assertEqual(run(["<think>a</think>b<think>c</think>"]), ("b<think>c</think>", "a"))

    def test_empty_chunks_are_harmless(self):
        self.assertEqual(run(["", "<think>", "", "x", "</think>", "", "y"]), ("y", "x"))


class TrackerTests(unittest.TestCase):
    def test_counts_and_summary(self):
        t = thinking.ThinkingTracker()
        self.assertFalse(t)
        t.add("a" * 1500)
        self.assertTrue(t)
        self.assertEqual(t.chars, 1500)
        self.assertIn("1.5k", t.summary())
        self.assertIn("/expand think", t.summary())

    def test_keeps_text_up_to_cap_but_keeps_counting(self):
        t = thinking.ThinkingTracker()
        t.add("x" * (thinking.MAX_KEEP + 10))
        self.assertEqual(len(t.text), thinking.MAX_KEEP)
        self.assertEqual(t.chars, thinking.MAX_KEEP + 10)


if __name__ == "__main__":
    unittest.main()
