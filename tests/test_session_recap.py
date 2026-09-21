"""away recap 的生成：模型现写一行，照搬 Claude Code awaySummary 的生成函数。

旧的「交接摘要」（turn 结束时后台写七节结构化摘要、resume 时零调用拼多行块）已下线：
它应付不了开放式的会话。这里钉住新机制在 agent 层的契约——
上下文是主请求的那份投影 + 末尾追加 CC 的提示词；前缀与主循环一致（工具照发、不许用）；
**绝不碰 self.messages**；
写 recap 的永远是这个窗口此刻在用的模型（用户 09-20 定），失败也不换别家来写。
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tests  # noqa: F401,E402  —— 状态目录隔离

from core import agent as A                           # noqa: E402
from core import away_recap as R                      # noqa: E402
from core import client                               # noqa: E402
from core import context as C                         # noqa: E402
from core import goals                                # noqa: E402
from core import settings as CFG                      # noqa: E402
from core import tools                                # noqa: E402
from test_agent_loop import mk                        # noqa: E402


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
            {"role": "assistant", "content": f"做完了 {index}"},
        ])
    return messages


def make_agent(messages, model="kimi-k3-256k", gateway="deepinfer"):
    agent = A.Agent.__new__(A.Agent)
    agent.messages = messages
    agent.model, agent.gateway = model, gateway
    agent.session_id = "recap-test"
    agent.context_summary = None
    agent.away_recap = None
    agent.compact_failed = None
    agent._compact_failed_key = None
    agent.last_total = 0
    agent._trace_context = lambda *a, **k: None
    return agent


class Provider:
    """按顺序回放每次 stream_chat 的事件；记下每次调用的参数。"""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls = []

    def __call__(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        return iter(script)


def text(value):
    return [{"t": "text", "v": value}]


class RequestShapeTests(unittest.TestCase):
    def generate(self, agent, *scripts, **kwargs):
        provider = Provider(*scripts)
        with mock.patch.object(A.client, "stream_chat", provider):
            result = agent.generate_away_recap(**kwargs)
        return result, provider

    def test_prompt_is_appended_last(self):
        agent = make_agent(history(4))
        result, provider = self.generate(agent, text("在修 X，下一步跑测试。"))
        self.assertEqual(result["kind"], "ok")
        self.assertEqual(result["text"], "在修 X，下一步跑测试。")
        call = provider.calls[0]
        self.assertEqual(call["messages"][-1],
                         {"role": "user", "content": R.PROMPT})
        self.assertEqual(call["thinking"], {"type": "disabled"})
        self.assertIn("do not call any tools", R.PROMPT)

    def test_the_session_transcript_is_never_touched(self):
        """recap 只给人看：提示词和回答都不进 messages，否则下一轮模型会看到
        一条自己没见过的「用户提问」。"""
        messages = history(4)
        before = [dict(m) for m in messages]
        agent = make_agent(messages)
        self.generate(agent, text("好。"))
        self.assertEqual(agent.messages, before)
        self.assertIsNone(agent.context_summary)

    def test_an_interrupted_turn_does_not_make_the_request_invalid(self):
        """轮次被中断时尾部是一条没应答的 tool_calls；在它后面直接接 user 会被 400。
        投影层负责剔掉它——这里钉住 recap 确实走的是投影而不是 raw messages。"""
        messages = history(3)
        dangling = {"role": "assistant", "content": "", "tool_calls": [{
            "id": "never-answered", "type": "function",
            "function": {"name": "bash", "arguments": "{}"}}]}
        messages.append(dangling)
        agent = make_agent(messages)
        _, provider = self.generate(agent, text("好。"))
        sent = provider.calls[0]["messages"]
        answered = {m.get("tool_call_id") for m in sent if m.get("role") == "tool"}
        offered = {call["id"] for m in sent for call in (m.get("tool_calls") or ())}
        self.assertEqual(offered - answered, set())
        self.assertEqual(sent[-1]["content"], R.PROMPT)
        self.assertIs(agent.messages[-1], dangling)       # raw 记录原样保留

    def test_nothing_to_recap_without_a_real_question(self):
        agent = make_agent([{"role": "system", "content": "sys"},
                            {"role": "user", "content": "[已中断] 运行时通知"}])
        result, provider = self.generate(agent)
        self.assertEqual(result["kind"], "no-turn")
        self.assertEqual(provider.calls, [])

    def test_thinking_is_stripped_and_the_line_is_capped(self):
        agent = make_agent(history(4))
        result, _ = self.generate(
            agent, text("<think>想了很多</think>" + "字" * 900))
        self.assertTrue(result["capped"])
        self.assertEqual(len(result["text"]), R.CHAR_CAP)
        self.assertNotIn("think", result["text"])


class CacheSafeTests(unittest.TestCase):
    """旁路请求必须和主循环共用前缀，否则整个上下文的 prompt cache 全废。

    2026-09-20 实测（deepseek-v4-flash@deepinfer，6.5 万 token）：同前缀命中
    65,024/65,279 token、首字 0.4–0.5s，不命中 4.6s；「带工具」与「不带工具」是两份
    互不相干的缓存（工具定义排在提示词最前面）。第一版 recap 发的是 tools=None，
    等于每次都冷启动——而主循环在 Boyue 上 91.8% 的输入 token 来自缓存。
    """

    def test_the_request_is_the_main_loops_request_plus_one_message(self):
        agent = mk()
        for index in range(3):
            agent.messages += [{"role": "user", "content": f"问题 {index}"},
                               {"role": "assistant", "content": f"回答 {index}"}]
        main = Provider([{"t": "text", "v": "主循环的回答"},
                         {"t": "done", "reason": "stop", "usage": {}}])
        with mock.patch.object(A.client, "stream_chat", main):
            list(agent.run("第四个问题"))
        side = Provider(text("在修 X。"))
        with mock.patch.object(A.client, "stream_chat", side):
            result = agent.generate_away_recap()
        self.assertEqual(result["kind"], "ok")
        sent_main, sent_side = main.calls[0], side.calls[0]
        self.assertTrue(sent_main["tools"])                       # 主循环确实带了工具
        self.assertEqual(sent_side["tools"], sent_main["tools"])  # 旁路：同一套，一字不差
        prefix = sent_main["messages"]
        self.assertEqual(sent_side["messages"][:len(prefix)], prefix)
        self.assertEqual(sent_side["messages"][-1]["content"], R.PROMPT)

    def test_a_tool_call_in_the_reply_is_a_failure_and_nothing_runs(self):
        """工具是为了缓存才带上的，不是给它用的。"""
        agent = make_agent(history(4))
        wants_tool = [{"t": "tool", "v": [{"id": "c1", "type": "function", "function": {
            "name": "bash", "arguments": '{"command": "rm -rf /"}'}}]},
                      {"t": "done", "reason": "tool_calls", "usage": {}}]
        provider = Provider(wants_tool)
        with mock.patch.object(A.client, "stream_chat", provider), \
             mock.patch.object(tools, "run") as runner:
            result = agent.generate_away_recap()
        runner.assert_not_called()
        self.assertEqual(result["kind"], "failed")
        self.assertIn("工具", result["text"])
        self.assertEqual(len(provider.calls), 1)                  # 也不为此重问

    def test_before_any_request_the_allowlist_shapes_the_tools(self):
        """刚 resume、本进程还没发过主请求：按与 run() 同义的名单拼工具集。"""
        agent = make_agent(history(4))
        provider = Provider(text("好。"), text("好。"))
        with mock.patch.object(A.client, "stream_chat", provider):
            agent.generate_away_recap(allowed_tools={"read_file"})
            agent.generate_away_recap()
        names = [[t["function"]["name"] for t in call["tools"]] for call in provider.calls]
        self.assertEqual(names[0], ["read_file"])
        self.assertGreater(len(names[1]), 5)


class WhichModelTests(unittest.TestCase):
    """写 recap 的就是这个窗口此刻在用的模型——没有第二个选项，失败时也没有。"""

    def setUp(self):
        # 「这条 route 不许关思考」是学到后持久化的；用例之间不能靠它串味。
        self.known = mock.patch.object(
            A.models_db, "thinking_off_rejected", return_value=False)
        self.learn = mock.patch.object(A.models_db, "note_thinking_off_rejected")
        self.known_mock, self.learn_mock = self.known.start(), self.learn.start()
        self.addCleanup(self.known.stop)
        self.addCleanup(self.learn.stop)

    def generate(self, agent, *scripts, **kwargs):
        provider = Provider(*scripts)
        with mock.patch.object(A.client, "stream_chat", provider):
            return agent.generate_away_recap(**kwargs), provider

    def test_it_is_the_model_this_window_is_using_right_now(self):
        agent = make_agent(history(4), model="glm-5.3", gateway="boyue")
        result, provider = self.generate(agent, text("好。"))
        self.assertEqual((provider.calls[0]["gateway"], provider.calls[0]["model"]),
                         ("boyue", "glm-5.3"))
        self.assertEqual((result["gateway"], result["model"]), ("boyue", "glm-5.3"))
        # /model 切走之后，下一条 recap 跟着走
        agent.model, agent.gateway = "deepseek-v4-flash", "deepinfer"
        _, provider = self.generate(agent, text("好。"))
        self.assertEqual((provider.calls[0]["gateway"], provider.calls[0]["model"]),
                         ("deepinfer", "deepseek-v4-flash"))

    def test_a_failing_model_is_never_replaced_by_another_one(self):
        """压缩在主模型 503 时会换席位池里另一家来写；recap 不会——那等于在没人要求的
        情况下把会话内容发给用户没给这个窗口选的 provider。写不出来就安静地失败。"""
        agent = make_agent(history(4))
        self.assertIsNotNone(agent.compaction_fallback_route())   # 兜底确实存在，只是不用
        down = client.APIError("HTTP 503 service_error", kind="transient_http", status=503)
        result, provider = self.generate(agent, down)
        self.assertEqual(result["kind"], "api-error")
        self.assertIn("503", result["text"])
        self.assertEqual([(c["gateway"], c["model"]) for c in provider.calls],
                         [("deepinfer", "kimi-k3-256k")])

    def test_thinking_that_ate_the_budget_gets_a_wider_one_on_the_same_model(self):
        agent = make_agent(history(4))
        starved = [{"t": "reasoning", "v": "想" * 500}]
        result, provider = self.generate(agent, starved, text("好了。"))
        self.assertEqual(result["kind"], "ok")
        self.assertEqual(provider.calls[0]["thinking"], {"type": "disabled"})
        self.assertIsNone(provider.calls[1]["thinking"])
        self.assertGreater(provider.calls[1]["max_tokens"],
                           provider.calls[0]["max_tokens"])
        self.assertEqual({(c["gateway"], c["model"]) for c in provider.calls},
                         {("deepinfer", "kimi-k3-256k")})

    def test_a_model_that_refuses_thinking_off_is_asked_again_its_own_way(self):
        """有的模型始终思考，关思考会被 400；那就按它默认的方式再问一次，仍是同一个模型。"""
        agent = make_agent(history(4))
        refused = client.APIError("该模型始终思考，不支持关闭思考", kind="invalid_request",
                                  status=400)
        result, provider = self.generate(agent, refused, text("好了。"))
        self.assertEqual(result["kind"], "ok")
        self.assertIsNone(provider.calls[1]["thinking"])
        self.assertEqual({(c["gateway"], c["model"]) for c in provider.calls},
                         {("deepinfer", "kimi-k3-256k")})
        # 证据是一对真实请求（关思考 400、不关就成功）→ 学下来，下次不再白撞
        self.learn_mock.assert_called_once_with("deepinfer", "kimi-k3-256k")

    def test_a_known_refuser_is_not_sent_the_doomed_request_again(self):
        self.known_mock.return_value = True
        agent = make_agent(history(4))
        result, provider = self.generate(agent, text("好了。"))
        self.assertEqual(result["kind"], "ok")
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(provider.calls[0]["thinking"])
        self.assertEqual(provider.calls[0]["max_tokens"], A.COMPACT_RETRY_MAX_TOKENS)

    def test_a_too_long_rejection_is_not_mistaken_for_a_parameter_problem(self):
        """同样是 400，但「输入太长」换参数没用：不重问，也不学成「不许关思考」。"""
        agent = make_agent(history(4))
        too_long = client.APIError(
            "HTTP 400: This model's maximum context length is 131072 tokens",
            kind="invalid_request", status=400)
        result, provider = self.generate(agent, too_long)
        self.assertEqual(result["kind"], "api-error")
        self.assertEqual(len(provider.calls), 1)
        self.learn_mock.assert_not_called()

    def test_the_route_is_fixed_for_the_whole_call(self):
        """两次尝试之间有人 /model 切走，也不能让第二次落到另一个模型上。"""
        agent = make_agent(history(4))

        def starved_then_switch():
            yield {"t": "reasoning", "v": "想" * 500}
            agent.model, agent.gateway = "glm-5.3", "boyue"

        _, provider = self.generate(agent, starved_then_switch(), text("好了。"))
        self.assertEqual({(c["gateway"], c["model"]) for c in provider.calls},
                         {("deepinfer", "kimi-k3-256k")})

    def test_an_empty_answer_is_a_failure_and_is_not_retried(self):
        agent = make_agent(history(4))
        result, provider = self.generate(agent, text("   "))
        self.assertEqual(result["kind"], "failed")
        self.assertEqual(len(provider.calls), 1)

    def test_abort_before_the_request_sends_nothing(self):
        agent = make_agent(history(4))
        cancel = threading.Event()
        cancel.set()
        result, provider = self.generate(agent, cancel=cancel)
        self.assertEqual(result["kind"], "aborted")
        self.assertEqual(provider.calls, [])

    def test_abort_during_the_request_wins_over_the_answer(self):
        agent = make_agent(history(4))
        cancel = threading.Event()

        def events():
            yield {"t": "text", "v": "写到一半"}
            cancel.set()

        result, _ = self.generate(agent, events(), cancel=cancel)
        self.assertEqual(result["kind"], "aborted")


class PersistenceTests(unittest.TestCase):
    def test_recorded_recap_round_trips_with_the_question_count(self):
        agent = make_agent(history(5))
        record = agent.record_away_recap("在修 X。")
        self.assertEqual(record["users"], 5)
        snapshot = agent.context_snapshot()
        restored = make_agent(history(5))
        restored.load_context(snapshot)
        self.assertEqual(restored.away_recap["text"], "在修 X。")
        self.assertEqual(restored.away_recap["users"], 5)

    def test_rebuilding_the_system_prompt_does_not_drop_it(self):
        """refresh_environment 会重验压缩摘要的边界；它曾经只把 summary 传给
        load_context，于是每次 resume（必然重建 system prompt）都把 recap 清空——
        单元测试全绿，是端到端 PTY 测试抓到的。"""
        agent = make_agent(history(5))
        agent.record_away_recap("在修 X。")
        with mock.patch.object(A, "system_prompt", return_value="sys"):
            agent.refresh_environment()
        self.assertIsNotNone(agent.away_recap)
        self.assertEqual(agent.away_recap["text"], "在修 X。")

    def test_it_never_enters_the_provider_projection(self):
        agent = make_agent(history(5))
        agent.record_away_recap("只给人看的一行")
        projection = agent.project_context(offered_tools=[])
        self.assertNotIn("只给人看的一行", str(projection.messages))

    def test_legacy_handoff_summaries_in_old_records_are_ignored(self):
        """旧记录的 context["recap"] 是已下线的交接摘要：读到不报错，也不再写回。"""
        agent = make_agent(history(3))
        agent.load_context({"version": 1, "summary": None,
                            "recap": {"content": "## objective\n旧的",
                                      "covered_to": 4}})
        self.assertIsNone(agent.away_recap)
        self.assertNotIn("recap", agent.context_snapshot())

    def test_malformed_away_recap_is_dropped(self):
        agent = make_agent(history(3))
        agent.load_context({"away_recap": {"text": ""}})
        self.assertIsNone(agent.away_recap)
        agent.load_context({"away_recap": "不是 dict"})
        self.assertIsNone(agent.away_recap)


class CountingTests(unittest.TestCase):
    def test_goal_continuation_prompts_are_not_questions(self):
        """goal 自动续轮的 <goal_round> 是 harness 写的，不能拿来凑「够 3 条」。"""
        goal = goals.new("human objective", goal_id="goal-recap")
        messages = [{"role": "user", "content": "human objective"}]
        for index in range(1, 6):
            messages.append({"role": "assistant", "content": f"第 {index} 轮"})
            messages.append({"role": "user", "content": goals.prompt(goal, index)})
        self.assertEqual(R.real_user_messages(messages), 1)
        self.assertFalse(R.eligible(messages))

    def test_questions_with_attachments_count(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
        self.assertEqual(R.real_user_messages(messages), 1)
        self.assertEqual(R.real_user_messages(
            [{"role": "user", "content": [{"type": "image_url"}]}]), 0)


class PolicyTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(CFG.recap_policy({}), {
            "enabled": True, "away_seconds": 300.0})

    def test_switch_and_delay(self):
        policy = CFG.recap_policy({"recap_auto": False, "recap_away_seconds": 90})
        self.assertEqual(policy, {"enabled": False, "away_seconds": 90.0})

    def test_there_is_no_model_switch(self):
        """写 recap 的永远是窗口当前的模型；留在旧配置里的 recap_model 被无视。"""
        policy = CFG.recap_policy({"recap_model": "deepinfer/deepseek-v4-flash"})
        self.assertEqual(sorted(policy), ["away_seconds", "enabled"])
        self.assertNotIn("recap_model", CFG.DEFAULTS)

    def test_bad_values_fall_back_instead_of_crashing_the_session(self):
        for bad in ("x", None, -5, 0, float("nan"), float("inf"), []):
            self.assertEqual(
                CFG.recap_policy({"recap_away_seconds": bad})["away_seconds"],
                300.0, bad)


class PlanningTests(unittest.TestCase):
    """keep_tail=0 是 plan_compaction 的通用能力：不留尾部原文、覆盖到最后一个完整轮次。"""

    def test_keep_tail_zero_covers_the_whole_session(self):
        messages = history(30)
        whole = C.plan_compaction(messages, None, keep_tail=0)
        compact = C.plan_compaction(messages, None, keep_tail=6)
        self.assertEqual(whole["covered_to"], len(messages))
        self.assertLess(compact["covered_to"], whole["covered_to"])

    def test_keep_tail_zero_still_respects_the_source_budget(self):
        messages = history(60, result_chars=4_000)
        plan = C.plan_compaction(
            messages, None, keep_tail=0, max_source_tokens=2_000)
        self.assertLessEqual(
            C.estimate_tokens(C.summary_prompt(plan)), 2_000)
        self.assertLess(plan["covered_to"], len(messages))


if __name__ == "__main__":
    unittest.main()
