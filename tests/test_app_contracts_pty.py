"""全屏模式下的 CC 契约对照（SPEC-CC-parity 里最依赖渲染的几条）：帧里能看到该看到的。
子进程里把 termcaps.detect 打桩成全开；断言对象是画进备用屏的帧文本。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui  # noqa: E402
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

CAPS = r'''
from core import termcaps
termcaps.detect = lambda **kw: termcaps.TermCaps(
    tty=True, alt_screen=True, sync_output=False, truecolor=True, kitty_keyboard=True,
    sgr_mouse=True, osc52_write=True, cols=100, rows=30)
'''
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def plain(s):
    return ANSI.sub("", s)


def child(stream_body, extra=""):
    return HEAD + CAPS + r'''
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
''' + stream_body + r'''
client.stream_chat = fake_stream
session = zylab.Session(agent)
agent.confirm = session.confirm
session.app_mode = True
''' + extra + r'''
announce_when(lambda: "空闲" in session.pump.snapshot().activity, "ZYLAB_TEST_IDLE")
announce_when(lambda: (calls["n"] >= 1 and session.controller.current_turn_id is None
                       and not session.pump.snapshot().busy and "空闲" in session.pump.snapshot().activity),
              "ZYLAB_TEST_TURN_DONE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"calls": calls["n"], "draft": session.pump.snapshot().text}))
'''

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEN = "\n".join(f"line {i}" for i in range(1, 11))


class AppContractTests(unittest.TestCase):
    def run_child(self, body, sends, extra=""):
        return run_pty_child(child(body, extra), sends, cwd=ROOT, timeout=30.0)

    def test_a3_long_paste_folds_in_the_composer(self):
        payload = (tui.PASTE_START + TEN + tui.PASTE_END).encode()
        out, r = self.run_child('''    yield {"t": "done", "reason": "stop", "usage": {}}''',
                                [PTYSend(payload, after="ZYLAB_TEST_IDLE"),
                                 PTYSend(b"\x03", delay=0.6),          # A9：Ctrl+C 清掉带占位符的草稿
                                 PTYSend(b"/exit\r", delay=0.6)])
        text = plain(out)                      # harness 的伪终端只有 20 列，占位符会折行
        self.assertIn("[Pasted text", text)
        self.assertIn("#1 +10 lines]", text)
        self.assertEqual(r["calls"], 0)

    def test_d1_thinking_is_folded_not_in_body(self):
        out, r = self.run_child('''    yield {"t": "reasoning", "v": "SECRETPLAN"}
    yield {"t": "text", "v": "VISIBLEANSWER"}
    yield {"t": "done", "reason": "stop", "usage": {}}''',
                                [PTYSend(b"go\r", after="ZYLAB_TEST_IDLE"),
                                 PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE", delay=0.6)])
        text = plain(out)
        self.assertIn("VISIBLEANSWER", text)
        self.assertIn("✻ 思考", text)
        self.assertNotIn("SECRETPLAN", text)

    def test_b1_shift_tab_cycles_permission_mode_in_the_prompt(self):
        out, r = self.run_child('''    yield {"t": "done", "reason": "stop", "usage": {}}''',
                                [PTYSend(b"\x1b[Z", after="ZYLAB_TEST_IDLE"),
                                 PTYSend(b"/exit\r", delay=0.8)])
        self.assertIn("⏵⏵", plain(out))

    def test_g2_question_mark_shows_the_keys_card_in_the_transcript(self):
        out, r = self.run_child('''    yield {"t": "done", "reason": "stop", "usage": {}}''',
                                [PTYSend(b"?\r", after="ZYLAB_TEST_IDLE"),
                                 PTYSend(b"/exit\r", delay=0.8)])
        text = plain(out)
        self.assertIn("快捷键", text)
        self.assertIn("Shift+Tab", text)
        self.assertEqual(r["calls"], 0)

    def test_c2_ctrl_o_expands_the_last_folded_output(self):
        out, r = self.run_child('''    yield {"t": "reasoning", "v": "SECRETPLAN-XYZ"}
    yield {"t": "text", "v": "VISIBLEANSWER"}
    yield {"t": "done", "reason": "stop", "usage": {}}''',
                                [PTYSend(b"go\r", after="ZYLAB_TEST_IDLE"),
                                 PTYSend(b"\x0f", after="ZYLAB_TEST_TURN_DONE", delay=0.4),
                                 PTYSend(b"/exit\r", delay=0.8)])
        self.assertIn("SECRETPLAN-XYZ", plain(out), "Ctrl+O 之后思考内容展开进帧")


if __name__ == "__main__":
    unittest.main()
