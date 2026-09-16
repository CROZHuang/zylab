"""turn 边界竞态：controller 已结束 turn、_turn_managed 还在 drain 输入时到达的提交
不能让 REPL 崩溃 —— 要交回 run_actions 顺序执行。

历史：09-01 claude 复现（4/5 命中），交给 codex；codex 的修复与测试只存在于它
未提交的工作树，master 从未修过。09-03 D1 的 PTY 测试再次撞上，这次修在 master 线。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child


def body(draft):
    return r'''
import json, os, threading, time
os.environ["TERM"] = "xterm-256color"
import zylab
from core import agent as agent_mod, client, store
from tests.pty_harness import announce_when, install_ready_input_pump
install_ready_input_pump()
store.ensure_home()
agent = agent_mod.Agent.__new__(agent_mod.Agent)
agent.model = "m"; agent.gateway = "deepinfer"; agent.session_id = "boundary"
agent.messages = [{"role": "system", "content": "s"}]
agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
agent.cache_read = agent.cache_write = 0
agent.cache_reported = False; agent.compact_failed = None
agent.ctx_limit = 100000; agent.compact_at = 70000
agent.ctx_known = True; agent.ctx_limit_source = "test"
agent.context_summary = None; agent.context_invalid_reason = None
agent._compact_failed_key = None; agent._last_age_notice_key = None
agent._seen_ok_hi = 0; agent.started = time.time()
release = threading.Event()
calls = {"n": 0}
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    print("ZYLAB_TEST_STREAM_STARTED", flush=True)
    yield {"t": "text", "v": "X"}
    release.wait(10)
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
session = zylab.Session(agent)
agent.confirm = session.confirm
# 草稿一写好就放行 turn：Enter 恰好落在 turn 结束的瞬间。
announce_when(lambda: session.pump.snapshot().text == ''' + repr(draft) + r''',
              "ZYLAB_TEST_DRAFT_READY", on_ready=release.set)
crashed = None
try:
    zylab.repl(session, "m")
except BaseException as exc:
    crashed = f"{type(exc).__name__}: {exc}"
print("RESULT:" + json.dumps({"crashed": crashed, "calls": calls["n"]}))
'''


def _run(draft, extra=()):
    sends = [PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(draft.encode(), after="ZYLAB_TEST_STREAM_STARTED"),
             PTYSend(b"\r", after="ZYLAB_TEST_DRAFT_READY")]
    sends += list(extra)
    _, r = run_pty_child(
        body(draft), sends,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        timeout=25.0)
    return r


class TurnBoundaryTests(unittest.TestCase):
    def test_non_live_command_at_boundary_does_not_crash(self):
        r = _run("/exit")
        self.assertIsNone(r["crashed"], r)
        self.assertEqual(r["calls"], 1)

    def test_plain_text_at_boundary_becomes_the_next_turn(self):
        r = _run("plaintext", extra=[PTYSend(b"/exit\r", delay=1.5)])
        self.assertIsNone(r["crashed"], r)
        self.assertEqual(r["calls"], 2, "迟到的文本应成为下一个 turn，而不是丢掉或崩溃")

    def test_live_then_non_live_right_after_done(self):
        r = _run("/usage", extra=[PTYSend(b"/exit\r", delay=0.3)])
        self.assertIsNone(r["crashed"], r)


if __name__ == "__main__":
    unittest.main()
