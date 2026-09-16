"""交接摘要：压缩摘要之外的第二条素材来源。

2026-09-08 实测：最近 5 个会话**没有一个有摘要**，4 个测得的里 3 个原文只有
58K–217K token，永远够不到 238K 的压缩阈值。所以 recap 的锚点/倒序/否决全是空转。
压缩摘要是为了「塞进上下文窗口」，交接摘要是为了「让 resume 的人知道到哪了」——
两个触发点必须分开，而且交接摘要**绝不能**进 provider 投影，否则就是提前丢历史。
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent as A                           # noqa: E402
from core import context as C                         # noqa: E402
from core import settings as CFG                      # noqa: E402
from core import tools                                # noqa: E402


def history(turns, *, result_chars=400):
    messages = [{"role": "system", "content": "sys"}]
    for index in range(turns):
        call_id = f"c{index}"
        messages.extend([
            {"role": "user", "content": f"任务 {index}"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": "read_file",
                             "arguments": '{"path": "/a"}'}}]},
            {"role": "tool", "tool_call_id": call_id,
             "content": "x" * result_chars},
        ])
    return messages


def make_agent(messages):
    agent = A.Agent.__new__(A.Agent)
    agent.messages = messages
    agent.model, agent.gateway = "m", "g"
    agent.session_id = "recap-test"
    agent.context_summary = None
    agent.session_recap = None
    agent.compact_failed = None
    agent._compact_failed_key = None
    agent.last_total = 0
    agent._trace_context = lambda *a, **k: None
    return agent


class PlanningTests(unittest.TestCase):
    def test_recap_covers_the_whole_session_unlike_compaction(self):
        messages = history(30)
        recap_plan = C.plan_compaction(messages, None, keep_tail=0)
        compact_plan = C.plan_compaction(messages, None, keep_tail=6)
        self.assertEqual(recap_plan["covered_to"], len(messages))
        self.assertLess(compact_plan["covered_to"], recap_plan["covered_to"])

    def test_recap_planning_respects_the_source_budget(self):
        messages = history(60, result_chars=4_000)
        plan = C.plan_compaction(
            messages, None, keep_tail=0, max_source_tokens=2_000)
        self.assertLessEqual(
            C.estimate_tokens(C.summary_prompt(plan)), 2_000)
        self.assertLess(plan["covered_to"], len(messages))


class StalenessTests(unittest.TestCase):
    def test_short_sessions_never_pay_for_a_recap(self):
        agent = make_agent(history(3))
        self.assertFalse(agent.recap_is_stale())

    def test_a_long_session_without_a_recap_is_stale(self):
        agent = make_agent(history(60, result_chars=4_000))
        self.assertTrue(agent.recap_is_stale())

    def test_a_fresh_recap_is_not_refreshed_until_enough_new_turns(self):
        messages = history(60, result_chars=4_000)
        agent = make_agent(messages)
        agent.session_recap = {"covered_to": len(messages) - 3 * 5,
                               "content": "## objective\nx"}
        self.assertFalse(agent.recap_is_stale(refresh_turns=15))
        self.assertTrue(agent.recap_is_stale(refresh_turns=2))


class CommitTests(unittest.TestCase):
    def test_the_recap_never_enters_the_provider_projection(self):
        messages = history(30)
        agent = make_agent(messages)
        with mock.patch.object(
                A.client, "stream_chat",
                return_value=iter([{"t": "text", "v": "## objective\n跟踪"}])):
            agent.refresh_session_recap()
        self.assertIsNotNone(agent.session_recap)
        self.assertIsNone(agent.context_summary)      # 关键：不替换上下文
        projection = C.materialize(
            messages, summary=agent.context_summary,
            tools_schema=tools.SCHEMA, model_limit=280_000,
            usable_budget=238_000)
        self.assertEqual(projection.report["summary"]["status"], "none")

    def test_snapshot_round_trips_and_a_stale_recap_survives(self):
        messages = history(30)
        agent = make_agent(messages)
        with mock.patch.object(
                A.client, "stream_chat",
                return_value=iter([{"t": "text", "v": "## objective\n跟踪"}])):
            agent.refresh_session_recap()
        snapshot = agent.context_snapshot()
        self.assertIn("recap", snapshot)

        restored = make_agent(messages + [
            {"role": "user", "content": "又一轮"}])
        restored.load_context(snapshot)
        # 哈希对不上也不作废：交接摘要不进投影，稍旧远好过没有
        self.assertIsNotNone(restored.session_recap)

    def test_a_failed_request_leaves_the_previous_recap_alone(self):
        messages = history(30)
        agent = make_agent(messages)
        agent.session_recap = {"content": "## objective\n旧的", "covered_to": 4}
        with mock.patch.object(
                A.client, "stream_chat", side_effect=RuntimeError("boom")):
            result = agent.refresh_session_recap()
        self.assertTrue(str(result).startswith("[摘要生成失败"))
        self.assertEqual(agent.session_recap["content"], "## objective\n旧的")


class RecapSourceTests(unittest.TestCase):
    def test_format_recap_falls_back_to_the_handoff_summary(self):
        import zylab                                  # noqa: PLC0415

        record = {
            "messages": [{"role": "user", "content": f"第 {i} 轮"}
                         for i in range(40)],
            "context": {"version": 1, "summary": None, "recap": {
                "content": "## objective\n跑通扩盘发布\n"
                           "## decisions\n- [t9] 冻结 v4 疾病树\n"}},
        }
        text = zylab.format_recap(record, replay_limit=5)
        self.assertIn("跑通扩盘发布", text)
        self.assertIn("冻结 v4", text)

    def test_a_valid_compaction_summary_still_wins(self):
        import zylab                                  # noqa: PLC0415

        record = {
            "messages": [{"role": "user", "content": f"第 {i} 轮"}
                         for i in range(40)],
            "context": {
                "summary": {"status": "valid",
                            "content": "## objective\n压缩摘要的目标\n"},
                "recap": {"content": "## objective\n交接摘要的目标\n"}},
        }
        text = zylab.format_recap(record, replay_limit=5)
        self.assertIn("压缩摘要的目标", text)
        self.assertNotIn("交接摘要的目标", text)


class PolicyTests(unittest.TestCase):
    def test_defaults_and_bad_values(self):
        policy = CFG.recap_policy({})
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["min_tokens"], 40_000)
        self.assertEqual(policy["refresh_turns"], 15)
        self.assertFalse(CFG.recap_policy({"recap_auto": False})["enabled"])
        for bad in ("x", None, 0, -1):
            self.assertEqual(
                CFG.recap_policy({"recap_refresh_turns": bad})[
                    "refresh_turns"], 15)


class SessionTriggerTests(unittest.TestCase):
    def make_session(self, stale):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session.cfg = {}
        session._recap_thread = None
        session.ag = mock.Mock()
        session.ag.recap_is_stale.return_value = stale
        session.ag.refresh_session_recap.return_value = "## objective\nx"
        session.save = mock.Mock()
        return session

    def test_fresh_sessions_do_not_spawn_a_thread(self):
        session = self.make_session(False)
        self.assertIsNone(session.maybe_refresh_session_recap())

    def test_disabled_by_settings(self):
        session = self.make_session(True)
        session.cfg = {"recap_auto": False}
        self.assertIsNone(session.maybe_refresh_session_recap())

    def test_stale_sessions_refresh_in_the_background_and_save(self):
        session = self.make_session(True)
        session.maybe_refresh_session_recap().join(timeout=5)
        session.ag.refresh_session_recap.assert_called_once()
        session.save.assert_called_once()

    def test_a_failed_refresh_does_not_save_or_raise(self):
        session = self.make_session(True)
        session.ag.refresh_session_recap.return_value = "[摘要生成失败：x]"
        session.maybe_refresh_session_recap().join(timeout=5)
        session.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
