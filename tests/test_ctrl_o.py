"""C2（SPEC-CC-parity）：Ctrl+O 展开最近一段折叠输出 —— 键位解码与编辑器事件。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


class CtrlOTests(unittest.TestCase):
    def test_read_key_decodes_ctrl_o(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"\x0f")
            with os.fdopen(read_fd, "rb", buffering=0) as stream:
                read_fd = None
                self.assertEqual(tui.read_key(timeout=0.1, stream=stream), "ctrl-o")
        finally:
            os.close(write_fd)
            if read_fd is not None:
                os.close(read_fd)

    def test_editor_emits_expand_last_and_keeps_draft(self):
        editor = tui.LineEditor()
        editor.handle("d"); editor.handle("r")
        events = editor.handle("ctrl-o")
        self.assertEqual([e.kind for e in events], ["expand_last"])
        self.assertEqual(editor.text, "dr", "展开不该动草稿")

    def test_ctrl_o_works_while_busy_too(self):
        editor = tui.LineEditor()
        editor.busy = True
        self.assertEqual([e.kind for e in editor.handle("ctrl-o")], ["expand_last"])


if __name__ == "__main__":
    unittest.main()
