"""`zylab --app` 薄切片端到端（DESIGN-TUI-app §5）：进备用屏 + kitty push + 鼠标；模型输出画在帧里；
退出时协议全部收回并把纯文本记录回放到主屏。子进程里把 termcaps.detect 打桩成全开（伪终端不会应答查询）。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

TAIL = r'''
from core import termcaps
termcaps.detect = lambda **kw: termcaps.TermCaps(
    tty=True, alt_screen=True, sync_output=True, truecolor=True, kitty_keyboard=True,
    sgr_mouse=True, osc52_write=True, cols=100, rows=30)
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "text", "v": "HELLO-FROM-MODEL"}
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
session = zylab.Session(agent)
agent.confirm = session.confirm
session.app_mode = True
announce_when(lambda: "空闲" in session.pump.snapshot().activity, "ZYLAB_TEST_IDLE")
announce_when(lambda: (calls["n"] >= 1 and session.controller.current_turn_id is None
                       and not session.pump.snapshot().busy and "空闲" in session.pump.snapshot().activity),
              "ZYLAB_TEST_TURN_DONE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"calls": calls["n"]}))
'''


class AppModePtyTests(unittest.TestCase):
    def test_app_mode_round_trip(self):
        out, r = run_pty_child(
            HEAD + TAIL,
            [PTYSend(b"hi\r", after="ZYLAB_TEST_IDLE"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE", delay=0.5)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=30.0)
        self.assertEqual(r["calls"], 1, r)
        self.assertIn("\x1b[?1049h", out, "进备用屏")
        self.assertIn("\x1b[>1u", out, "kitty 协议 push")
        self.assertIn("\x1b[?1006h", out, "SGR 鼠标")
        self.assertIn("\x1b[?2026h", out, "同步输出")
        enter = out.index("\x1b[?1049h")
        leave = out.index("\x1b[?1049l")
        self.assertLess(enter, leave)
        self.assertIn("HELLO-FROM-MODEL", out[enter:leave], "模型输出画在备用屏的帧里")
        self.assertIn("\x1b[<u", out[:leave + 20], "kitty 协议 pop")
        self.assertIn("\x1b[?1006l", out, "鼠标关")
        replay = out[leave:]
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", replay)
        self.assertIn("HELLO-FROM-MODEL", plain, "退出后纯文本回放到主屏")
        self.assertIn("› hi", plain)


if __name__ == "__main__":
    unittest.main()
