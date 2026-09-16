"""真实 PTY 下的粘贴回归：粘 3 行必须是 0 次提交，不是 3 次。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui
from tests.pty_harness import PTYSend, run_pty_child


BODY = r'''
import json
import os
import threading
import time

# HOME 由 run_pty_child 在创建 child 之前统一隔离，body 不再自己包 tempdir。
os.environ["TERM"] = "xterm-256color"
import zylab
from core import agent as agent_mod, client, store
from tests.pty_harness import announce_when, install_ready_input_pump

install_ready_input_pump()
store.ensure_home()
agent = agent_mod.Agent.__new__(agent_mod.Agent)
agent.model = "m"
agent.gateway = "deepinfer"
agent.session_id = "paste-pty"
agent.messages = [{"role": "system", "content": "s"}]
agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
agent.cache_read = agent.cache_write = 0
agent.cache_reported = False
agent.compact_failed = None
agent.ctx_limit = 100000
agent.compact_at = 70000
agent.ctx_known = True
agent.ctx_limit_source = "test"
agent.context_summary = None
agent.context_invalid_reason = None
agent._compact_failed_key = None
agent._last_age_notice_key = None
agent._seen_ok_hi = 0
agent.started = time.time()

calls = {"n": 0}
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "text", "v": "done"}
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream

session = zylab.Session(agent)
agent.confirm = session.confirm

seen = {}
announce_when(
    lambda: "\n" in session.pump.snapshot().text,
    "ZYLAB_TEST_PASTE_LANDED",
    on_ready=lambda: seen.setdefault(
        "draft", session.pump.snapshot().text))
announce_when(
    lambda: (calls["n"] == 1
             and session.controller.current_turn_id is None
             and not session.pump.snapshot().busy),
    "ZYLAB_TEST_SUBMITTED")

zylab.repl(session, "m")

users = [m["content"] for m in agent.messages if m.get("role") == "user"]
print("RESULT:" + json.dumps(
    {"draft": seen.get("draft"), "users": users, "calls": calls["n"]},
    ensure_ascii=False))
'''

THREE = "alpha\nbeta\ngamma"


class PastePTYTests(unittest.TestCase):
    def test_three_line_paste_becomes_one_prompt_not_three(self):
        """用户报的 bug 本身：粘 N 行曾变成 N 条 prompt。

        粘 3 行 → 按一次回车 → 必须只有 **1** 条 user message，内容是完整的
        3 行。修复前这里会是 3 条。
        """
        payload = (tui.PASTE_START + THREE + tui.PASTE_END).encode()
        output, result = run_pty_child(
            BODY,
            [PTYSend(payload, after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(b"\r", after="ZYLAB_TEST_PASTE_LANDED"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_SUBMITTED")],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=20.0,
        )
        self.assertEqual(result["draft"], THREE, result)
        self.assertEqual(result["users"], [THREE], result)
        self.assertEqual(result["calls"], 1, result)

    def test_real_tui_actually_enables_dec_2004(self):
        """解码器写对了但终端侧没开启，等于没修 —— 这里验的是真实输出。"""
        output, _ = run_pty_child(
            BODY,
            [PTYSend(b"/exit\r", after="ZYLAB_TEST_PUMP_READY")],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=15.0,
        )
        # run_pty_child 返回的是解码后的 str，不是 bytes。
        self.assertIn(tui.PASTE_ON, output)


if __name__ == "__main__":
    unittest.main()
