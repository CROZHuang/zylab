"""上下文管理：工具结果老化 + 压缩的行为约束。

这两处坏了用户不会立刻发现（表现为「模型忘事」或「莫名报错」），
所以约束要用测试钉死。
"""
import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import agent as A, client


def mk_agent(model="test-model"):
    ag = A.Agent.__new__(A.Agent)
    ag.model = model
    ag.messages = [{"role": "system", "content": "sys"}]
    ag.tokens_in = ag.tokens_out = ag.last_total = ag.turns = 0
    ag.cache_read = ag.cache_write = 0
    ag.cache_reported = False
    ag.compact_failed = None
    ag.session_id = "test"
    ag.gateway = "test"
    ag.ctx_limit, ag.compact_at, ag.ctx_known = 100_000, 70_000, True
    return ag


def add_turn(ag, tool_out, call_id):
    """加一轮：assistant 发起工具调用 + tool 返回结果。"""
    ag.messages.append({"role": "assistant", "content": "",
                        "tool_calls": [{"id": call_id, "type": "function",
                                        "function": {"name": "bash",
                                                     "arguments": "{}"}}]})
    ag.messages.append({"role": "tool", "tool_call_id": call_id,
                        "content": tool_out})


class ToolResultAging(unittest.TestCase):
    def test_old_results_are_shortened_recent_kept(self):
        ag = mk_agent()
        for i in range(8):
            add_turn(ag, f"OUTPUT-{i}" + "x" * 2000, f"call{i}")
        raw_before = copy.deepcopy(ag.messages)
        projection = ag.project_context([])
        projected_tools = [m for m in projection.messages if m["role"] == "tool"]
        # 最近 AGE_AFTER_TURNS 轮保持原文
        for m in projected_tools[-A.AGE_AFTER_TURNS:]:
            self.assertNotIn("工具结果投影", m["content"])
            self.assertGreater(len(m["content"]), 2000)
        # 更早的只在 provider 投影中缩短；raw 一字不改
        self.assertTrue(any("tier=aged" in m["content"]
                            for m in projected_tools[:3]))
        self.assertEqual(ag.messages, raw_before)

    def test_message_count_unchanged_no_orphans(self):
        """必须替换内容而不是删消息 —— 删了会留下孤儿 tool_call_id，服务端拒收。"""
        ag = mk_agent()
        for i in range(8):
            add_turn(ag, "y" * 3000, f"call{i}")
        n_before = len(ag.messages)
        ag.age_tool_results()
        self.assertEqual(len(ag.messages), n_before)
        called = [t["id"] for m in ag.messages if m.get("tool_calls")
                  for t in m["tool_calls"]]
        answered = {m.get("tool_call_id") for m in ag.messages
                    if m.get("role") == "tool"}
        self.assertEqual([c for c in called if c not in answered], [])

    def test_head_is_preserved_so_model_can_recognize_it(self):
        ag = mk_agent()
        for i in range(8):
            add_turn(ag, f"MARKER-{i} " + "z" * 3000, f"call{i}")
        projection = ag.project_context([])
        aged = [m for m in projection.messages
                if m["role"] == "tool" and "tier=aged" in m["content"]]
        self.assertTrue(aged)
        self.assertIn("MARKER-0", aged[0]["content"])

    def test_short_results_are_left_alone(self):
        ag = mk_agent()
        for i in range(8):
            add_turn(ag, "ok", f"call{i}")
        self.assertEqual(ag.age_tool_results(), 0)

    def test_aging_is_idempotent(self):
        """反复投影必须确定且 raw 不变。"""
        ag = mk_agent()
        for i in range(8):
            add_turn(ag, "w" * 3000, f"call{i}")
        raw_before = copy.deepcopy(ag.messages)
        first = ag.project_context([])
        second = ag.project_context([])
        self.assertGreater(ag.age_tool_results(), 0)
        self.assertEqual(first.messages, second.messages)
        self.assertEqual(first.report["projected_sha256"],
                         second.report["projected_sha256"])
        self.assertEqual(ag.messages, raw_before)

    def test_no_aging_when_conversation_is_short(self):
        ag = mk_agent()
        for i in range(2):
            add_turn(ag, "v" * 3000, f"call{i}")
        self.assertEqual(ag.age_tool_results(), 0)


class Compaction(unittest.TestCase):
    def test_below_threshold_does_nothing(self):
        ag = mk_agent()
        ag.last_total = 100
        self.assertIsNone(ag.maybe_compact())

    def test_summary_failure_degrades_instead_of_crashing(self):
        """自动触发的功能，其失败必须可降级 —— 网络抖动不该崩掉会话。"""
        ag = mk_agent()
        for i in range(10):
            add_turn(ag, "q" * 500, f"call{i}")
        ag.last_total = 99_000
        with mock.patch.object(client, "stream_chat",
                               side_effect=client.APIError("HTTP 503")):
            s = ag.maybe_compact()
        self.assertIn("摘要生成失败", s)
        self.assertEqual(ag.messages[0]["role"], "system")
        self.assertGreater(len(ag.messages), 1)

    def test_tail_never_starts_with_orphan_tool_message(self):
        """压缩后 tail 若以 tool 消息开头，其 tool_call 已被摘要吃掉 → 孤儿。"""
        ag = mk_agent()
        for i in range(10):
            add_turn(ag, "r" * 500, f"call{i}")
        ag.last_total = 99_000
        with mock.patch.object(client, "stream_chat",
                               side_effect=client.APIError("boom")):
            ag.maybe_compact()
        called = [t["id"] for m in ag.messages if m.get("tool_calls")
                  for t in m["tool_calls"]]
        answered = {m.get("tool_call_id") for m in ag.messages
                    if m.get("role") == "tool"}
        self.assertEqual([c for c in called if c not in answered], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
