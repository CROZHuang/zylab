"""恢复会话像从没退出过：启动时 --resume 一个 30 条消息（含工具调用与结果）的会话，
整段都以实时输出的形状放回终端——用户行 `› …`、助手 `⏺ …`、工具 `⏺ Bash(...)` + `⎿  …`——
不是"最后一屏"，也没有单独的预览格式。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

TAIL = r'''
from core import away_recap
def fake_stream(model, messages, **kwargs):
    # resume 之后会后台现写一行 recap（09-20 起）：那是另一回事，单独计数。
    # 这条测试钉的是**回放本身**不花任何调用。
    is_recap = messages[-1].get("content") == away_recap.PROMPT
    calls["recap" if is_recap else "n"] = calls.get("recap" if is_recap else "n", 0) + 1
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
agent.session_id = "full-replay"
msgs = [{"role": "system", "content": "s"}]
for i in range(12):
    msgs.append({"role": "user", "content": f"QUESTION-{i:02d}"})
    msgs.append({"role": "assistant", "content": f"ANSWER-{i:02d}"})
msgs.append({"role": "user", "content": "run something"})
msgs.append({"role": "assistant", "content": "", "tool_calls": [
    {"id": "call-1", "type": "function", "function": {"name": "bash", "arguments": "{\"command\": \"echo TOOL-CMD\"}"}}]})
msgs.append({"role": "tool", "tool_call_id": "call-1", "name": "bash", "content": "TOOL-CMD\n[exit 0]"})
msgs.append({"role": "assistant", "content": "FINAL-ANSWER"})
agent.messages = msgs
store.save_session(agent, "full", cwd=os.getcwd())
agent.session_id = "live"
agent.messages = [{"role": "system", "content": "s"}]
session = zylab.Session(agent)
agent.confirm = session.confirm
# 启动时 --resume 的路径：renderer 还没建，先记下，repl 起来后补放
zylab.resume_session_record(session, {"id": "full-replay"}, replay=True, save_current=False)
announce_when(lambda: "空闲" in session.pump.snapshot().activity, "ZYLAB_TEST_IDLE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"calls": calls["n"], "n": len(agent.messages)}))
'''


class FullReplayTests(unittest.TestCase):
    def test_startup_resume_replays_the_whole_session_in_live_shapes(self):
        out, r = run_pty_child(
            HEAD + TAIL,
            [PTYSend(b"/exit\r", after="ZYLAB_TEST_IDLE", delay=0.8)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=40.0)
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", out)
        self.assertEqual(r["calls"], 0)
        self.assertEqual(r["n"], 29)
        for i in (0, 5, 11):
            self.assertIn(f"› QUESTION-{i:02d}", plain, "整段都放，不是最后一屏")
            self.assertIn(f"⏺ ANSWER-{i:02d}", plain)
        self.assertIn("⏺ Bash(echo TOOL-CMD)", plain, "工具调用与实时一样的形状")
        self.assertIn("⎿", plain, "工具结果折叠行")
        self.assertIn("⏺ FINAL-ANSWER", plain)
        self.assertNotIn("会话预览", plain)
        self.assertNotIn("╭─ You", plain)
        self.assertLess(plain.index("› QUESTION-00"), plain.index("› QUESTION-11"))


if __name__ == "__main__":
    unittest.main()
