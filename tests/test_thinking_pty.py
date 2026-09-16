"""D1 端到端：思考折叠成一行、不进正文、/expand think 可展开、流式时状态栏可见。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child

HEAD = r'''
import json, os, threading, time
os.environ["TERM"] = "xterm-256color"
import zylab
from core import agent as agent_mod, client, store
from tests.pty_harness import announce_when, install_ready_input_pump
install_ready_input_pump()
store.ensure_home()
agent = agent_mod.Agent.__new__(agent_mod.Agent)
agent.model = "m"; agent.gateway = "deepinfer"; agent.session_id = "think-pty"
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
'''

TAIL = r'''
client.stream_chat = fake_stream
session = zylab.Session(agent)
agent.confirm = session.confirm
announce_when(lambda: "思考中" in session.pump.snapshot().activity and "字" in session.pump.snapshot().activity,
              "ZYLAB_TEST_THINKING_SHOWN", on_ready=release.set)
# 「turn 结束」以回到空闲提示符为准：last_turn_outcome 在 finally 里就已置位，
# 那时 teardown 还在跑，此刻发命令会撞进 turn 边界。
# last_turn_outcome 在 Session 构造时就不是 None —— 靠它判"turn 结束"会在开局的
# 空闲窗口里就成真（曾让 /expand 抢在 turn 之前执行）。以假流被调用过为准。
announce_when(lambda: (calls["n"] >= 1
                       and session.controller.current_turn_id is None
                       and not session.pump.snapshot().busy
                       and "空闲" in session.pump.snapshot().activity),
              "ZYLAB_TEST_TURN_DONE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"last_text": session._last_text}))
'''

REASONING_STREAM = r'''
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "reasoning", "v": "SECRETPLAN-part1 "}
    yield {"t": "reasoning", "v": "SECRETPLAN-part2"}
    release.wait(10)
    yield {"t": "text", "v": "VISIBLEANSWER"}
    yield {"t": "done", "reason": "stop", "usage": {}}
'''

INLINE_THINK_STREAM = r'''
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "text", "v": "<thi"}
    yield {"t": "text", "v": "nk>SECRETPLAN-inline</th"}
    release.wait(10)
    yield {"t": "text", "v": "ink>VISIBLEANSWER"}
    yield {"t": "done", "reason": "stop", "usage": {}}
'''


def _run(stream_src):
    return run_pty_child(
        HEAD + stream_src + TAIL,
        [PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
         PTYSend(b"/expand think\r", after="ZYLAB_TEST_TURN_DONE"),
         PTYSend(b"/exit\r", delay=0.3)],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        timeout=25.0)


class ThinkingPTYTests(unittest.TestCase):
    def _idx(self, out, marker):
        pos = out.find(marker)
        if pos < 0:
            dump = os.path.join(os.environ.get("ZYLAB_TEST_DUMP", "/tmp"), "d1_fail_out.txt")
            with open(dump, "w", encoding="utf-8") as fh:
                fh.write(out)
            self.fail(f"输出里找不到 {marker!r}；完整输出已存 {dump}")
        return pos

    def _assert_folded(self, out, result):
        summary = self._idx(out, "✻ 思考 (")
        answer = self._idx(out, "VISIBLEANSWER")
        expand = self._idx(out, "/expand think")
        self.assertLess(summary, answer, "折叠行要先于正文")
        first_secret = self._idx(out, "SECRETPLAN")
        self.assertGreater(first_secret, expand, "思考内容只能在 /expand think 之后出现")
        self.assertNotIn("SECRETPLAN", result["last_text"], "思考不得进入 assistant 正文")
        self.assertIn("思考中", out, "流式期间状态栏应显示 ✻ 思考中")

    def test_reasoning_events_are_folded_and_expandable(self):
        out, result = _run(REASONING_STREAM)
        self._assert_folded(out, result)
        n = len("SECRETPLAN-part1 ") + len("SECRETPLAN-part2")
        self.assertIn(f"✻ 思考 ({n} 字", out)

    def test_inline_think_tags_split_across_chunks_are_folded(self):
        out, result = _run(INLINE_THINK_STREAM)
        self._assert_folded(out, result)
        self.assertNotIn("<think>", out.split("/expand think")[0], "标签本身也不能露出来")


if __name__ == "__main__":
    unittest.main()
