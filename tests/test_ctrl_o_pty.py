"""C2 端到端：Ctrl+O 在真实 PTY 里展开最近一段折叠输出（这里是思考）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD, TAIL, REASONING_STREAM


class CtrlOPTYTests(unittest.TestCase):
    def test_ctrl_o_expands_folded_thinking_when_no_tool_output(self):
        out, result = run_pty_child(
            HEAD + REASONING_STREAM + TAIL,
            [PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(b"\x0f", after="ZYLAB_TEST_TURN_DONE"),        # Ctrl+O
             PTYSend(b"/exit\r", delay=0.5)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=25.0)
        fold = out.find("✻ 思考 (")
        secret = out.find("SECRETPLAN")
        self.assertGreater(fold, -1, "折叠行应先出现")
        self.assertGreater(secret, fold, "Ctrl+O 之后思考内容才出现")
        self.assertNotIn("SECRETPLAN", result["last_text"])


if __name__ == "__main__":
    unittest.main()
