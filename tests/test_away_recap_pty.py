"""away recap 端到端：真 PTY、真 REPL、真焦点序列，只把 provider 打桩。

单元测试钉的是各层的契约；这里钉的是它们**接起来之后**真的会发生：
终端失焦 → 定时器 → 后台生成 → 主循环空闲 tick 把那一行写上屏 → 落进会话记录，
以及「人回来了就别打扰」。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.pty_harness import PTYSend, run_pty_child  # noqa: E402
from tests.test_thinking_pty import HEAD              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BODY = r'''
from core import away_recap
away_recap.MIN_DELAY_SECONDS = 0.0          # 产品里是 30s 下限 / 2s 防抖；测试里压到毫秒级
away_recap.BLUR_DEBOUNCE_SECONDS = 0.05
for index in range(3):
    agent.messages.append({"role": "user", "content": f"第 {index} 个问题"})
    agent.messages.append({"role": "assistant", "content": f"第 {index} 个回答"})
agent.away_recap = None
seen = {"recap_requests": 0, "same_tools_as_main": None, "tool_count": 0}
main_tools = {}

def fake_stream(model, messages, **kwargs):
    if messages[-1].get("content") == away_recap.PROMPT:
        seen["recap_requests"] += 1
        seen["same_tools_as_main"] = kwargs.get("tools") == main_tools.get("v")
        seen["tool_count"] = len(kwargs.get("tools") or ())
        yield {"t": "text", "v": "RECAPLINE 在修解析器，下一步跑测试。"}
        yield {"t": "done", "reason": "stop", "usage": {}}
        return
    main_tools["v"] = kwargs.get("tools")
    calls["n"] += 1
    yield {"t": "text", "v": "VISIBLEANSWER"}
    yield {"t": "done", "reason": "stop", "usage": {}}

client.stream_chat = fake_stream
session = zylab.Session(agent)
session.cfg = dict(session.cfg, recap_away_seconds=AWAY_SECONDS)
agent.confirm = session.confirm
announce_when(lambda: (calls["n"] >= 1
                       and session.controller.current_turn_id is None
                       and not session.pump.snapshot().busy
                       and "空闲" in session.pump.snapshot().activity),
              "ZYLAB_TEST_TURN_DONE")
announce_when(lambda: agent.away_recap is not None, "ZYLAB_TEST_RECAP_SAVED")
zylab.repl(session, "m")
# RESULT 必须短：夹具读到 "RESULT:" 就不再读 PTY，长输出会把 child 堵死在 write() 上。
transcript = "\n".join(str(m.get("content")) for m in agent.messages)
print("RESULT:" + json.dumps({
    "recap": agent.away_recap, "seen": seen,
    "skip": session.away_recap_ctl.last_skip,
    "leaked": "RECAPLINE" in transcript or "stepped away" in transcript}))
'''


RESUME_BODY = r'''
from core import away_recap
seen = {"recap_requests": 0, "other_requests": 0}

def fake_stream(model, messages, **kwargs):
    if messages[-1].get("content") == away_recap.PROMPT:
        seen["recap_requests"] += 1
        yield {"t": "text", "v": "RESUMELINE 接着修解析器。"}
    else:
        seen["other_requests"] += 1
    yield {"t": "done", "reason": "stop", "usage": {}}

client.stream_chat = fake_stream
agent.session_id = "resume-recap"
msgs = [{"role": "system", "content": "s"}]
for index in range(4):
    msgs.append({"role": "user", "content": f"QUESTION-{index}"})
    msgs.append({"role": "assistant", "content": f"ANSWER-{index}"})
agent.messages = msgs
agent.away_recap = STORED
store.save_session(agent, "resume recap", cwd=os.getcwd())
agent.session_id = "live"
agent.messages = [{"role": "system", "content": "s"}]
agent.away_recap = None
session = zylab.Session(agent)
agent.confirm = session.confirm
zylab.resume_session_record(session, {"id": "resume-recap"}, replay=True, save_current=False)
shown = {"done": False}
_take = session.take_away_recap
def take():                       # 主循环取走那一行 = 它马上要上屏了
    text = _take()
    if text:
        shown["done"] = True
    return text
session.take_away_recap = take
announce_when(lambda: shown["done"], "ZYLAB_TEST_RECAP_SHOWN")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"seen": seen, "recap": agent.away_recap}))
'''


class ResumeRecapPTYTests(unittest.TestCase):
    def run_child(self, stored):
        body = HEAD + RESUME_BODY.replace("STORED", repr(stored))
        return run_pty_child(
            body, [PTYSend(b"/exit\r", after="ZYLAB_TEST_RECAP_SHOWN", delay=0.3)],
            cwd=ROOT, timeout=25.0)

    def test_startup_resume_writes_a_fresh_line_after_the_replay(self):
        output, result = self.run_child(None)
        self.assertEqual(result["seen"], {"recap_requests": 1, "other_requests": 0})
        self.assertIn("※ recap · RESUMELINE 接着修解析器。", output)
        self.assertLess(output.index("QUESTION-3"), output.index("※ recap"))
        self.assertEqual(result["recap"]["text"], "RESUMELINE 接着修解析器。")
        self.assertEqual(result["recap"]["users"], 4)

    def test_nothing_asked_since_the_last_recap_means_zero_calls(self):
        stored = {"text": "STOREDLINE 上次写好的。", "ts": "2026-09-20T00:00:00+00:00",
                  "users": 4}
        output, result = self.run_child(stored)
        self.assertEqual(result["seen"], {"recap_requests": 0, "other_requests": 0})
        self.assertIn("※ recap · STOREDLINE 上次写好的。", output)
        self.assertEqual(result["recap"]["text"], "STOREDLINE 上次写好的。")


class AwayRecapPTYTests(unittest.TestCase):
    def run_child(self, away_seconds, sends, timeout=25.0):
        body = HEAD + BODY.replace("AWAY_SECONDS", repr(away_seconds))
        return run_pty_child(body, sends, cwd=ROOT, timeout=timeout)

    def test_blur_writes_one_line_while_you_are_away_and_saves_it(self):
        output, result = self.run_child(0.05, [
            PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
            PTYSend(b"\x1b[O", after="ZYLAB_TEST_TURN_DONE", delay=0.1),
            PTYSend(b"\x1b[I", after="ZYLAB_TEST_RECAP_SAVED", delay=0.3),
            PTYSend(b"/exit\r", delay=0.2),
        ])
        self.assertEqual(result["seen"]["recap_requests"], 1)
        # 工具集与主请求一字不差（共用 prompt cache 前缀），且确实非空
        self.assertTrue(result["seen"]["same_tools_as_main"])
        self.assertGreater(result["seen"]["tool_count"], 5)
        self.assertIn("※ recap · RECAPLINE 在修解析器，下一步跑测试。", output)
        self.assertIn("recap_auto", output)                       # 前几次附一句怎么关
        # 落盘的是原文，不带提示；提问数 = 预置 3 条 + 这次的 go
        self.assertEqual(result["recap"]["text"], "RECAPLINE 在修解析器，下一步跑测试。")
        self.assertEqual(result["recap"]["users"], 4)
        # 只给人看：提示词和那一行都不进会话记录
        self.assertFalse(result["leaked"])

    def test_coming_back_before_the_timer_means_no_request_at_all(self):
        output, result = self.run_child(1.0, [
            PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
            PTYSend(b"\x1b[O", after="ZYLAB_TEST_TURN_DONE", delay=0.1),
            PTYSend(b"\x1b[I", delay=0.3),                         # 1s 内就回来了
            PTYSend(b"/exit\r", delay=1.4),                        # 等过原定的触发点
        ])
        self.assertEqual(result["seen"]["recap_requests"], 0)
        self.assertIsNone(result["recap"])
        self.assertNotIn("※ recap", output)
        self.assertIsNone(result["seen"]["same_tools_as_main"])

    def test_typing_a_draft_before_leaving_means_do_not_disturb(self):
        output, result = self.run_child(0.05, [
            PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
            PTYSend(b"half typed", after="ZYLAB_TEST_TURN_DONE", delay=0.1),
            PTYSend(b"\x1b[O", delay=0.2),
            PTYSend(b"\x1b[I", delay=0.6),
            PTYSend(b"\x15/exit\r", delay=0.2),                    # Ctrl+U 清掉草稿再退出
        ])
        self.assertEqual(result["seen"]["recap_requests"], 0)
        self.assertEqual(result["skip"], "draft input present")


if __name__ == "__main__":
    unittest.main()
