"""D2 端到端：首字之前状态栏把等待解释出来（连接中 → 等待首字），而不是只数秒。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD, TAIL

PHASED_STREAM = r'''
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    phases = kwargs.get("phases")          # 真正的 stream_chat_background 会注入它
    phases.mark("started", 1); time.sleep(0.35)
    phases.mark("headers", 1); time.sleep(0.35)
    yield {"t": "text", "v": "VISIBLEANSWER"}
    yield {"t": "done", "reason": "stop", "usage": {}}
'''


class LatencyPTYTests(unittest.TestCase):
    def test_status_line_explains_the_wait_before_first_token(self):
        out, _ = run_pty_child(
            HEAD + PHASED_STREAM + TAIL,
            [PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE")],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=25.0)
        i_conn, i_wait, i_ans = out.find("连接中"), out.find("等待首字"), out.find("VISIBLEANSWER")
        self.assertGreater(i_conn, -1, "应出现 连接中")
        self.assertGreater(i_wait, -1, "应出现 等待首字")
        self.assertLess(i_conn, i_wait, "先连接中再等待首字")
        self.assertLess(i_wait, i_ans, "都在首字之前")


if __name__ == "__main__":
    unittest.main()
