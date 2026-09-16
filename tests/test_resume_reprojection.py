"""E2（SPEC-CC-parity）：resume 后时间线从规范消息重投影，每条一次、顺序不变，不回放旧屏幕。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

TAIL = r'''
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
# 先落一份有四条消息的会话，再以另一个会话身份启动
agent.session_id = "e2-saved"
agent.messages = [{"role": "system", "content": "s"},
                  {"role": "user", "content": "QUESTION-ONE"},
                  {"role": "assistant", "content": "ANSWER-ONE"},
                  {"role": "user", "content": "QUESTION-TWO"},
                  {"role": "assistant", "content": "ANSWER-TWO"}]
store.save_session(agent, "e2", cwd=os.getcwd())
agent.session_id = "e2-live"
agent.messages = [{"role": "system", "content": "s"}]
session = zylab.Session(agent)
agent.confirm = session.confirm
announce_when(lambda: "空闲" in session.pump.snapshot().activity, "ZYLAB_TEST_IDLE")
announce_when(lambda: str(agent.session_id) == "e2-saved" and "空闲" in session.pump.snapshot().activity,
              "ZYLAB_TEST_RESUMED")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"calls": calls["n"], "sid": str(agent.session_id), "n": len(agent.messages)}))
'''


class ResumeReprojectionTests(unittest.TestCase):
    def test_resume_paints_each_message_once_in_order(self):
        out, r = run_pty_child(
            HEAD + TAIL,
            [PTYSend(b"/resume e2-saved\r", after="ZYLAB_TEST_IDLE"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_RESUMED", delay=0.8)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=30.0)
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out)
        self.assertEqual(r["sid"], "e2-saved", r)
        self.assertEqual(r["n"], 5, "规范消息原样接上")
        self.assertEqual(r["calls"], 0, "resume 不该触发模型调用")
        positions = []
        for token in ("QUESTION-ONE", "ANSWER-ONE", "QUESTION-TWO", "ANSWER-TWO"):
            # 命令回显里不会出现消息正文，所以正文只应被画一次
            self.assertEqual(plain.count(token), 1, (token, plain[-1500:]))
            positions.append(plain.index(token))
        self.assertEqual(positions, sorted(positions), "顺序与退出前一致")
        # 画的是语义时间线（You 框 + ⏺ 行），不是旧屏幕字节
        self.assertIn("⏺ ANSWER-ONE", plain)


if __name__ == "__main__":
    unittest.main()
