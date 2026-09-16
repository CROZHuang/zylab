"""C6（SPEC-CC-parity）：图标与文字一格间隔；结构化行（标题/围栏/列表）在首行不多缩两格。"""
import io
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SAMPLE = "# Result\n\n```python\nreturn 1\n```\n"


def plain_lines(text):
    return ANSI.sub("", text).replace("\r", "").split("\n")


class StructuralPrefixTests(unittest.TestCase):
    def test_hosted_first_line_drops_its_structural_indent(self):
        md = tui.StreamingMarkdown(styled=False, columns=60, icon_hosted=True)
        lines = plain_lines(md.feed(SAMPLE) + md.finish())
        self.assertEqual(lines[0], "◆ Result")
        self.assertIn("  ┌─ python", lines, lines)

    def test_styled_output_behaves_the_same(self):
        md = tui.StreamingMarkdown(styled=True, columns=60, icon_hosted=True)
        self.assertEqual(plain_lines(md.feed(SAMPLE) + md.finish())[0], "◆ Result")

    def test_unhosted_rendering_is_unchanged(self):
        md = tui.StreamingMarkdown(styled=False, columns=60)
        self.assertEqual(plain_lines(md.feed(SAMPLE) + md.finish())[0], "  ◆ Result")

    def test_next_message_on_the_same_instance_is_hosted_again(self):
        md = tui.StreamingMarkdown(styled=False, columns=60, icon_hosted=True)
        md.feed("plain first\n"); md.finish()
        self.assertEqual(plain_lines(md.feed("# Two\n") + md.finish())[0], "◆ Two")

    def test_hydrated_transcript_puts_one_space_between_icon_and_heading(self):
        stream = io.StringIO()
        r = tui.TerminalRenderer(stream)
        r.hydrate_transcript([
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": SAMPLE.rstrip("\n")},
        ])
        text = "\n".join(plain_lines(stream.getvalue()))
        self.assertIn("⏺ ◆ Result", text, text[-600:])
        self.assertNotIn("⏺   ◆", text)
        self.assertIn("\n  ┌─ python", text)


if __name__ == "__main__":
    unittest.main()
