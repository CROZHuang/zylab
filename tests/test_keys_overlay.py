"""G2（SPEC-CC-parity）：? 打开一屏快捷键总览；G3/I1：稳定词汇有界。"""
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zylab
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

TAIL = r'''
calls = {"n": 0}
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
session = zylab.Session(agent)
agent.confirm = session.confirm
announce_when(lambda: "空闲" in session.pump.snapshot().activity, "ZYLAB_TEST_IDLE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"calls": calls["n"]}))
'''


class KeysOverlayTests(unittest.TestCase):
    def test_overlay_is_one_screen_and_lists_the_stable_vocabulary(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zylab.cmd_keys(None, "")
        out = buf.getvalue()
        self.assertLessEqual(out.count("\n"), 18, "一屏放得下")
        for key in ("Enter", "Esc", "Shift+Tab", "Ctrl+O", "Ctrl+R", "Ctrl+B"):
            self.assertIn(key, out)

    def test_stable_chord_vocabulary_is_at_most_ten(self):
        """G3：用户要记的修饰键组合不超过 10 个（Enter/Tab/方向键/`/ @ ?` 不计）。"""
        import re
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zylab.cmd_keys(None, "")
        chords = [l.split()[0] for l in buf.getvalue().splitlines()
                  if re.match(r"\s*(Ctrl|Shift|Alt|Esc)", l)]
        self.assertLessEqual(len(chords), 10, chords)
        self.assertEqual(len(chords), len(set(chords)), "不重复列")

    def test_question_mark_opens_overlay_without_calling_the_model(self):
        out, r = run_pty_child(
            HEAD + TAIL,
            [PTYSend(b"?\r", after="ZYLAB_TEST_IDLE"),
             PTYSend(b"/exit\r", delay=0.5)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=25.0)
        self.assertIn("快捷键", out)
        self.assertEqual(r["calls"], 0, "? 不能变成一条发给模型的 prompt")


if __name__ == "__main__":
    unittest.main()
