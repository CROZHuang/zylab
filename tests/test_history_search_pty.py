"""A6 端到端：真实 PTY 里 Ctrl+R 搜到历史、Enter 填进草稿而不提交。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

TAIL = r'''
calls = {"n": 0}
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
for line in ("ls -la", "git status", "python3 -m unittest"):
    store.append_history(line)
session = zylab.Session(agent)
agent.confirm = session.confirm
announce_when(lambda: session.pump.snapshot().text == "git status" and not session.pump.editor.searching,
              "ZYLAB_TEST_DRAFT_FILLED")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"calls": calls["n"]}))
'''


class HistorySearchPTYTests(unittest.TestCase):
    def test_ctrl_r_fills_draft_from_history(self):
        out, r = run_pty_child(
            HEAD + TAIL,
            [PTYSend(b"\x12", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(b"git st", delay=0.2),
             PTYSend(b"\r", delay=0.3),                       # 采用匹配，不提交
             PTYSend(b"\x15/exit\r", after="ZYLAB_TEST_DRAFT_FILLED")],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=25.0)
        self.assertIn("reverse-i-search", out)
        self.assertEqual(r["calls"], 0, "Enter 只是填入草稿，不能触发模型调用")


if __name__ == "__main__":
    unittest.main()
