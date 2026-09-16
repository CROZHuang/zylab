"""C6 续行：icon_hosted 模式下整条 assistant 消息挂在 ⏺ 下面（像 Claude Code）。

之前纯段落原样吐给终端折行，续行顶回第 0 列，而标题/列表自带两格缩进——同一条
消息里两种对齐。现在纯段落按列宽在词边界折行、续行两格，第二段起也补两格；
结构行原样穿过。非托管模式（-p、测试）行为不变。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tui

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LONG_EN = ("The quick brown fox jumps over the lazy dog while the renderer keeps "
           "every continuation line aligned under the icon.")
LONG_ZH = "这是一段没有空格的中文散文用来验证宽字符会在列宽处折行并且续行对齐在圆点后面。"


def render(text, *, columns=40, styled=False, hosted=True, per_char=False):
    md = tui.StreamingMarkdown(columns=columns, styled=styled, icon_hosted=hosted)
    fed = "".join(md.feed(c) for c in text) if per_char else md.feed(text)
    return fed + md.finish()


def lines_of(text):
    return ANSI.sub("", text).split("\n")


class HangingIndentTests(unittest.TestCase):
    def test_long_paragraph_wraps_at_words_with_two_space_continuation(self):
        lines = lines_of(render(LONG_EN + "\n"))
        self.assertFalse(lines[0].startswith(" "), "首行由 ⏺ 托管，不补缩进")
        body = [l for l in lines[1:] if l]
        self.assertGreater(len(body), 1)
        for line in body:
            self.assertTrue(line.startswith("  ") and not line.startswith("   "), line)
        # 首行前面有 "⏺ " 两列，所以首行本身 ≤ 38
        self.assertLessEqual(tui.display_width(lines[0]), 38)
        for line in body:
            self.assertLessEqual(tui.display_width(line), 40)
        # 词边界折行：没有词被截断
        self.assertEqual(" ".join(l.strip() for l in lines if l.strip()), LONG_EN)

    def test_cjk_paragraph_wraps_by_width(self):
        lines = [l for l in lines_of(render(LONG_ZH + "\n")) if l]
        self.assertGreater(len(lines), 1)
        for line in lines[1:]:
            self.assertTrue(line.startswith("  "), line)
            self.assertLessEqual(tui.display_width(line), 40)
        self.assertEqual("".join(l.strip() for l in lines), LONG_ZH)

    def test_second_paragraph_is_indented_under_the_icon(self):
        lines = lines_of(render("first\n\nsecond paragraph\n"))
        self.assertEqual(lines[0], "first")
        self.assertIn("  second paragraph", lines)

    def test_structural_lines_pass_through_unchanged(self):
        text = "intro\n\n## 标题\n\n- 列表项一很长很长很长很长很长很长很长很长很长很长很长很长\n\n```py\nx = 1\n```\n"
        lines = lines_of(render(text))
        self.assertIn("  ◇ 标题", lines)
        self.assertIn("    很长很长很长很长很长", lines, "列表续行保持四格（在圆点后对齐）")
        self.assertIn("  ┌─ py", lines)
        self.assertTrue(any(l.startswith("  │ x = 1") for l in lines), lines)
        self.assertFalse(any(l.startswith("    ◇") or l.startswith("    ┌") for l in lines))

    def test_styles_are_zero_width_and_stay_with_their_word(self):
        text = "aaa bbb **ccc ddd** eee fff ggg hhh\n"
        styled = render(text, columns=20, styled=True)
        plain = render(text, columns=20, styled=False)
        self.assertEqual(ANSI.sub("", styled), plain)
        for line in lines_of(styled):
            self.assertLessEqual(tui.display_width(line), 20)
        bold = tui.markdown_theme().bold if hasattr(tui.markdown_theme(), "bold") else "\x1b[1m"
        self.assertIn(bold, styled)

    def test_output_is_invariant_to_chunking(self):
        text = LONG_ZH + " " + LONG_EN + "\n\n- item\n\n" + LONG_EN + "\n"
        self.assertEqual(render(text, per_char=True), render(text))

    def test_overlong_token_is_hard_wrapped_not_overflowed(self):
        url = "https://example.invalid/" + "a" * 70
        for line in lines_of(render("see " + url + " now\n", columns=40)):
            self.assertLessEqual(tui.display_width(line), 40, line)

    def test_unhosted_mode_is_unchanged(self):
        out = render(LONG_EN + "\n\nsecond\n", hosted=False)
        self.assertEqual(out, LONG_EN + "\n\nsecond\n")

    def test_width_is_read_live(self):
        width = {"v": 60}
        md = tui.StreamingMarkdown(columns=lambda: width["v"], styled=False, icon_hosted=True)
        first = md.feed(LONG_EN + "\n")
        width["v"] = 30
        second = md.feed(LONG_EN + "\n") + md.finish()
        self.assertTrue(all(tui.display_width(l) <= 60 for l in first.split("\n")))
        self.assertTrue(all(tui.display_width(l) <= 30 for l in second.split("\n")))


if __name__ == "__main__":
    unittest.main()
