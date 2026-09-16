"""H1/H2 端到端：真实 PTY 下 turn 完成响铃、标题随状态切换、退出时清空。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child


def body(notify):
    return r'''
import copy, json, os, threading, time
os.environ["TERM"] = "xterm-256color"
import zylab
from core import agent as agent_mod, client, store, settings as CFG
from tests.pty_harness import announce_when, install_ready_input_pump

install_ready_input_pump()
store.ensure_home()
agent = agent_mod.Agent.__new__(agent_mod.Agent)
agent.model = "m"; agent.gateway = "deepinfer"; agent.session_id = "notify-pty"
agent.messages = [{"role": "system", "content": "s"}]
agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
agent.cache_read = agent.cache_write = 0
agent.cache_reported = False; agent.compact_failed = None
agent.ctx_limit = 100000; agent.compact_at = 70000
agent.ctx_known = True; agent.ctx_limit_source = "test"
agent.context_summary = None; agent.context_invalid_reason = None
agent._compact_failed_key = None; agent._last_age_notice_key = None
agent._seen_ok_hi = 0; agent.started = time.time()

calls = {"n": 0}
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "text", "v": "MODELTEXT"}
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream

cfg = copy.deepcopy(CFG.DEFAULTS)
cfg["notify"] = ''' + repr(notify) + r'''
session = zylab.Session(agent, cfg=cfg)
agent.confirm = session.confirm
announce_when(
    lambda: (calls["n"] >= 1
             and session.controller.current_turn_id is None
             and not session.pump.snapshot().busy
             and "空闲" in session.pump.snapshot().activity),
    "ZYLAB_TEST_TURN_DONE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"ok": True}))
'''


def _run(notify):
    output, _ = run_pty_child(
        body(notify),
        [PTYSend(b"hi\r", after="ZYLAB_TEST_PUMP_READY"),
         PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE")],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        timeout=20.0)
    return output


class NotifyPTYTests(unittest.TestCase):
    def test_bell_rings_once_after_the_turn(self):
        """标题关掉，线上唯一的 \\x07 就是响铃本身。"""
        out = _run({"bell": True, "title": False})
        self.assertEqual(out.count("\x07"), 1, out[-300:])
        self.assertLess(out.index("MODELTEXT"), out.index("\x07"), "铃要在模型文本之后")

    def test_bell_can_be_disabled(self):
        out = _run({"bell": False, "title": False})
        self.assertEqual(out.count("\x07"), 0)

    def test_title_tracks_state_and_is_cleared_on_exit(self):
        out = _run({"bell": False, "title": True})
        i_idle0 = out.index("\x1b]0;✳ zylab · ")
        i_busy = out.index("\x1b]0;● zylab · ")
        i_idle1 = out.index("\x1b]0;✳ zylab · ", i_busy)
        self.assertLess(i_idle0, i_busy)
        self.assertLess(i_busy, out.index("MODELTEXT"))
        self.assertLess(out.index("MODELTEXT"), i_idle1)
        self.assertTrue(out.rstrip().endswith("\x1b]0;\x07") or "\x1b]0;\x07" in out[i_idle1:],
                        "退出时标题应被清空")


if __name__ == "__main__":
    unittest.main()
