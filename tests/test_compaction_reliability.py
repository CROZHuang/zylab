"""压缩可靠性：思考吃光额度、分段、工作集。（旧参数不再老化，见 test_history_fidelity。）

2026-09-07 的真实故障：摘要请求 max_tokens=2000，kimi-k3 默认开思考，2000 token
全花在 reasoning_content 上，正文为空 → 从建会话起一次都没压成功，会话在 227K
天花板上烧掉 84.9M 输入 token。这些测试钉住修复的每一环。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent as A          # noqa: E402
from core import context as C        # noqa: E402


# 桩摘要的后六节：验收要求结构完整（见 context.summary_defects）
FULL_SUMMARY_REST = ("\n## constraints\n- none\n## decisions\n- none\n## files_changed\n- none"
                     "\n## evidence\n- none\n## pending\n- none\n## risks\n- none")


def turn(index, *, tool="read_file", args=None, result="ok"):
    """一个完整轮次：user → assistant(tool_call) → tool。"""
    call_id = f"call-{index}"
    return [
        {"role": "user", "content": f"任务 {index}"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": tool, "arguments": json.dumps(
                args or {"path": f"/repo/file{index}.py"}, ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def history(count, **kwargs):
    messages = [{"role": "system", "content": "sys"}]
    for index in range(count):
        messages.extend(turn(index, **kwargs))
    return messages


class ThinkingBudgetTests(unittest.TestCase):
    def make_agent(self, model="kimi-k3-256k", gateway="deepinfer"):
        agent = A.Agent.__new__(A.Agent)
        agent.model, agent.gateway = model, gateway
        agent.compact_failed = None
        agent._compact_failed_key = None
        return agent

    def test_attempts_go_thinking_off_then_wide_budget_then_another_model(self):
        attempts = self.make_agent().compaction_attempts()
        self.assertEqual(attempts[0]["thinking"], {"type": "disabled"})
        self.assertEqual(attempts[0]["route"], None)
        self.assertLess(attempts[0]["max_tokens"], attempts[1]["max_tokens"])
        self.assertEqual(attempts[1]["thinking"], None)
        self.assertTrue(attempts[-1]["label"].startswith("fallback:"),
                        attempts[-1]["label"])
        route = attempts[-1]["route"]
        self.assertNotEqual(
            (route["gateway"], route["model"]), ("deepinfer", "kimi-k3-256k"))

    def test_wide_budget_runs_only_when_thinking_ate_the_budget(self):
        should = A.Agent.compaction_should_attempt
        wide = {"when": "thinking_starved"}
        swap = {"when": "any_failure"}
        # 只输出思考、没有正文 —— 加大额度就能修
        self.assertTrue(should(wide, None, "", 1200))
        # 真空：同一个模型再发一次没用，但换模型有意义
        self.assertFalse(should(wide, None, "", 0))
        self.assertTrue(should(swap, None, "", 0))
        # provider 报错：加额度无关，换模型才对
        self.assertFalse(should(wide, RuntimeError("boom"), "", 800))
        self.assertTrue(should(swap, RuntimeError("boom"), "", 800))
        # 拿到正文就不再往下走
        self.assertFalse(should(wide, None, "## objective", 900))
        self.assertFalse(should(swap, None, "## objective", 0))

    def test_fallback_route_skips_the_current_model(self):
        agent = self.make_agent()
        route = agent.compaction_fallback_route()
        self.assertIsNotNone(route)
        self.assertNotEqual(
            (route["gateway"], route["model"]), (agent.gateway, agent.model))

    def test_failure_message_names_the_thinking_case(self):
        agent = self.make_agent()
        plan = {"covered_sha256": "abc", "source_turns": 3}
        thinking = agent._finish_compaction(plan, "", None, reasoning_chars=4321)
        self.assertIn("只输出思考", thinking)
        self.assertIn("4,321", thinking)
        empty = agent._finish_compaction(plan, "", None, reasoning_chars=0)
        self.assertIn("provider 返回空摘要", empty)


class FallbackReachabilityTests(unittest.TestCase):
    """降级阶梯的三步各有前提；**某一步不适用时要跳过它，而不是收工**。

    2026-09-20 实测：循环里用的是 break，于是主模型 503 / 真空正文时只发 1 次请求就
    放弃，「换一家来写」从未被尝试——和它的设计目的（主模型不行时压缩照样做得成，
    否则上下文卡在天花板上一直烧 token）正好相反。谓词和选路各自都有测试且全绿，
    缺的是把它们**串起来**跑一遍。
    """

    def run_plan(self, *scripts, known_refuser=False):
        from unittest import mock                     # noqa: PLC0415
        agent = A.Agent.__new__(A.Agent)
        agent.messages = history(30)
        agent.model, agent.gateway = "kimi-k3-256k", "deepinfer"
        agent.session_id = "fallback-reach"
        agent.context_summary = None
        agent.compact_failed = None
        agent._compact_failed_key = None
        agent.last_total = 0
        agent.compact_at, agent.ctx_limit = 70_000, 100_000
        agent._trace_context = lambda *a, **k: None
        calls, queue = [], list(scripts)
        self.thinking = []

        def provider(model, messages, **kwargs):
            calls.append((kwargs.get("gateway"), model))
            self.thinking.append(kwargs.get("thinking"))
            script = queue.pop(0) if queue else [
                {"t": "text", "v": "## objective\n跟踪" + FULL_SUMMARY_REST}]
            if isinstance(script, Exception):
                raise script
            return iter(script)

        # 「不许关思考」是学到后持久化的能力：用例之间不能靠它串味。
        with mock.patch.object(A.client, "stream_chat", provider), \
             mock.patch.object(A.models_db, "thinking_off_rejected",
                               side_effect=lambda g, m: known_refuser
                               and (g, m) == ("deepinfer", "kimi-k3-256k")), \
             mock.patch.object(A.models_db,
                               "note_thinking_off_rejected") as self.learned:
            result = agent._execute_compaction_plan(
                agent._compaction_plan(force=True))
        return agent, calls, str(result or "")

    def assert_rescued_by_another_model(self, agent, calls, result):
        self.assertFalse(result.startswith("[摘要生成失败"), result)
        self.assertEqual(calls[0], ("deepinfer", "kimi-k3-256k"))
        self.assertNotEqual(calls[-1], ("deepinfer", "kimi-k3-256k"))
        self.assertEqual((agent.context_summary["gateway"],
                          agent.context_summary["model"]), calls[-1])

    def test_a_provider_error_on_the_main_model_reaches_the_other_model(self):
        down = A.client.APIError("HTTP 503", kind="transient_http", status=503)
        agent, calls, result = self.run_plan(down)
        self.assertEqual(len(calls), 2, calls)        # 加大额度那步被跳过，不是终点
        self.assert_rescued_by_another_model(agent, calls, result)

    def test_a_truly_empty_answer_reaches_the_other_model(self):
        """没思考、也没正文：同一个模型再发一次没用，换模型才有意义。"""
        agent, calls, result = self.run_plan([{"t": "text", "v": "  "}])
        self.assertEqual(len(calls), 2, calls)
        self.assert_rescued_by_another_model(agent, calls, result)

    def test_thinking_starved_twice_still_ends_at_the_other_model(self):
        starved = [{"t": "reasoning", "v": "想" * 500}]
        agent, calls, result = self.run_plan(starved, starved)
        self.assertEqual([c for c in calls[:2]],
                         [("deepinfer", "kimi-k3-256k")] * 2)
        self.assertEqual(len(calls), 3)
        self.assert_rescued_by_another_model(agent, calls, result)

    def test_a_model_that_refuses_thinking_off_is_retried_not_replaced(self):
        """2026-09-20 实测：glm-5.3@boyue 对 thinking=disabled 一律 400（code 1210），
        压缩在这条用户最常用的 route 上 33 次全部失败、从未成功。换个问法就能好的事，
        不该把会话内容交给另一家模型，更不该整个放弃。"""
        refused = A.client.APIError(
            'HTTP 400: {"error":{"message":"该模型始终思考，不支持关闭思考；请使用 low、high 或 '
            'max。","code":"1210"}}', kind="invalid_request", status=400)
        agent, calls, result = self.run_plan(refused)
        self.assertFalse(result.startswith("[摘要生成失败"), result)
        self.assertEqual(calls, [("deepinfer", "kimi-k3-256k")] * 2)
        self.assertEqual(self.thinking, [{"type": "disabled"}, None])
        self.assertEqual(agent.context_summary["model"], "kimi-k3-256k")
        self.learned.assert_called_once_with("deepinfer", "kimi-k3-256k")

    def test_a_known_refuser_is_asked_its_own_way_from_the_start(self):
        agent, calls, result = self.run_plan(known_refuser=True)
        self.assertEqual(calls, [("deepinfer", "kimi-k3-256k")])
        self.assertEqual(self.thinking, [None])
        labels = None
        with __import__("unittest").mock.patch.object(
                A.models_db, "thinking_off_rejected", return_value=True):
            labels = [step["label"] for step in agent.compaction_attempts()]
        self.assertEqual(labels[0], "default-thinking")
        self.assertNotIn("wide-budget", labels)        # 与第一步同形，不重复

    def test_a_too_long_summary_request_is_not_a_parameter_problem(self):
        too_long = A.client.APIError(
            "HTTP 400: This model's maximum context length is 131072 tokens",
            kind="invalid_request", status=400)
        agent, calls, result = self.run_plan(too_long)
        self.assertEqual(calls[0], ("deepinfer", "kimi-k3-256k"))
        self.assertNotEqual(calls[1], ("deepinfer", "kimi-k3-256k"))  # 直接换模型
        self.learned.assert_not_called()

    def test_success_on_the_first_try_asks_nobody_else(self):
        agent, calls, result = self.run_plan()
        self.assertEqual(calls, [("deepinfer", "kimi-k3-256k")])

    def test_everything_failing_is_still_a_visible_failure(self):
        down = A.client.APIError("HTTP 503", kind="transient_http", status=503)
        agent, calls, result = self.run_plan(down, down)
        self.assertTrue(result.startswith("[摘要生成失败"), result)
        self.assertIsNone(agent.context_summary)


class InterruptedToolCallTests(unittest.TestCase):
    """被打断的工具调用（未配对的 tool_call）埋在历史里，不该判整个会话永不压缩。

    2026-09-20 实查：一个 1,079 条消息、约 44.6 万 token 的真实会话，因为第 30 条和第 176 条
    各有一次被 Esc 打断的工具调用，`plan_compaction` 永远返回 None。规则的本意是「别在一个
    还没配对完的调用上落边界」——那只对**可能还在跑**的调用成立。
    """

    def broken_history(self, broken_at=2, turns=20):
        messages = [{"role": "system", "content": "sys"}]
        for index in range(turns):
            if index == broken_at:
                messages.extend([
                    {"role": "user", "content": f"任务 {index}"},
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "interrupted", "type": "function",
                        "function": {"name": "bash", "arguments": "{}"}}]},
                    # 用户按了 Esc：没有 tool 结果，直接进入下一轮
                ])
                continue
            messages.extend(turn(index))
        return messages

    def test_a_historical_interrupted_call_does_not_veto_compaction(self):
        messages = self.broken_history()
        plan = C.plan_compaction(messages, None, keep_tail=6)
        self.assertIsNotNone(plan)
        self.assertGreater(plan["source_turns"], 5)
        # 残缺的那一对不交给摘要器（投影层也是这么处理的），但覆盖范围跨过了它
        self.assertNotIn("interrupted", str(plan["source_messages"]))
        self.assertGreater(plan["covered_to"], 5)

    def test_the_resulting_summary_is_valid_and_the_request_is_protocol_clean(self):
        messages = self.broken_history()
        plan = C.plan_compaction(messages, None, keep_tail=6)
        summary = C.make_summary("## objective\n跟踪", plan, model="m", gateway="g")
        self.assertEqual(C.validate_summary(messages, summary), (True, None))
        projection = C.materialize(
            messages, summary=summary, tools_schema=[], model_limit=256_000,
            usable_budget=200_000)
        offered = {call["id"] for m in projection.messages
                   for call in (m.get("tool_calls") or ())}
        answered = {m.get("tool_call_id") for m in projection.messages
                    if m.get("role") == "tool"}
        self.assertEqual(offered - answered, set())
        self.assertNotIn("interrupted", str(projection.messages))

    def test_a_call_that_may_still_be_running_keeps_blocking(self):
        """最后一个单元是未配对的调用 = 工具也许还在跑：摘要边界不能落在它后面。"""
        messages = self.broken_history(broken_at=99)
        messages.extend([
            {"role": "user", "content": "再跑一个"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "in-flight", "type": "function",
                "function": {"name": "bash", "arguments": "{}"}}]},
        ])
        with self.assertRaisesRegex(ValueError, "不完整 tool"):
            C.plan_compaction_to(messages, len(messages))
        stale = C.make_summary("## objective\nx", {
            **C.plan_compaction(messages, None, keep_tail=6),
        }, model="m", gateway="g")
        stale["covered_to"] = len(messages)
        self.assertEqual(C.validate_summary(messages, stale)[0], False)


class RoundGranularityTests(unittest.TestCase):
    """压缩在任何时刻都得压得动——包括用户只说了一句话、模型自己跑了几百步的会话。

    以前的规则是「留最近 6 个用户轮次」。2026-09-20 在真实会话上重放：一个 284 条消息、
    约 9.8 万 token 的会话只有 6 个用户轮次，永远压不了；一个 410 条的会话一段只能覆盖
    前 4 条。现在尾部按 token 预算、以协议单元为粒度留；用户原话由投影逐字回放，不受影响。
    """

    def autonomous(self, steps=60, users=2, result="r" * 3000):
        messages = [{"role": "system", "content": "sys"}]
        per_user = steps // users
        for u in range(users):
            messages.append({"role": "user", "content": f"目标 {u}：自己干完"})
            for s in range(per_user):
                cid = f"c{u}-{s}"
                messages.extend([
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": cid, "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "make"}'}}]},
                    {"role": "tool", "tool_call_id": cid, "content": result},
                ])
        return messages

    def test_a_session_with_two_user_turns_is_compactable(self):
        messages = self.autonomous()
        self.assertIsNone(C.plan_compaction(messages, None, keep_tail=6))   # 旧规则：压不了
        plan = C.plan_compaction(messages, None, keep_tail=6, tail_tokens=4_000)
        self.assertIsNotNone(plan)
        self.assertGreater(plan["covered_to"], len(messages) // 2)
        self.assertNotEqual(messages[plan["covered_to"]]["role"], "user")  # 切进了轮次内部
        self.assertEqual(messages[plan["covered_to"]]["role"], "assistant")  # 但不切开协议单元

    def test_user_words_survive_verbatim_when_the_cut_is_inside_a_turn(self):
        messages = self.autonomous()
        plan = C.plan_compaction(messages, None, keep_tail=6, tail_tokens=4_000)
        summary = C.make_summary("## objective\n跟踪", plan, model="m", gateway="g")
        self.assertEqual(C.validate_summary(messages, summary), (True, None))
        projection = C.materialize(messages, summary=summary, tools_schema=[],
                                   model_limit=256_000, usable_budget=200_000)
        sent_users = [m["content"] for m in projection.messages if m["role"] == "user"]
        self.assertEqual(sent_users, ["目标 0：自己干完", "目标 1：自己干完"])
        offered = {call["id"] for m in projection.messages
                   for call in (m.get("tool_calls") or ())}
        answered = {m.get("tool_call_id") for m in projection.messages
                    if m.get("role") == "tool"}
        self.assertEqual(offered, answered)
        self.assertLess(projection.report["estimated_request_tokens"],
                        C.materialize(messages, summary=None, tools_schema=[],
                                      model_limit=256_000, usable_budget=200_000
                                      ).report["estimated_request_tokens"])

    def test_a_clean_turn_boundary_is_preferred_when_one_fits(self):
        messages = history(40)                          # 每轮都很小：尾部预算装得下好几轮
        plan = C.plan_compaction(messages, None, keep_tail=6, tail_tokens=400)
        self.assertEqual(messages[plan["covered_to"]]["role"], "user")

    def test_the_tail_stays_near_its_budget(self):
        messages = self.autonomous(steps=80, users=1)
        budget = 6_000
        plan = C.plan_compaction(messages, None, keep_tail=6, tail_tokens=budget)
        units, _ = C.build_units(messages)
        tail = [u for u in units if u.start >= plan["covered_to"]]
        kept = sum(C._tail_cost(u) for u in tail)
        self.assertLessEqual(kept, budget)
        self.assertGreater(kept, budget // 2)

    def test_at_least_two_units_stay_even_when_they_blow_the_budget(self):
        messages = self.autonomous(steps=10, users=1, result="r" * 50_000)
        plan = C.plan_compaction(messages, None, keep_tail=6, tail_tokens=10)
        units, _ = C.build_units(messages)
        self.assertEqual(len([u for u in units if u.start >= plan["covered_to"]]),
                         C.TAIL_MIN_UNITS)

    def test_a_boundary_right_after_an_interrupted_call_is_a_valid_boundary(self):
        """F5 留下的边角：残缺对不属于任何单元，它后面那个位置曾经不算合法边界，
        于是刚写好的摘要被判 coverage_splits_conversation_turn，压缩白做。"""
        messages = [{"role": "system", "content": "sys"}]
        for index in range(3):
            messages.extend(turn(index))
        messages.extend([
            {"role": "user", "content": "任务 3"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "interrupted", "type": "function",
                "function": {"name": "bash", "arguments": "{}"}}]},
        ])
        for index in range(4, 10):
            messages.extend(turn(index))
        plan = C.plan_compaction(messages, None, keep_tail=6)
        self.assertTrue(messages[plan["covered_to"] - 1].get("tool_calls"))   # 边界紧跟残缺调用
        summary = C.make_summary("## objective\nx", plan, model="m", gateway="g")
        self.assertEqual(C.validate_summary(messages, summary), (True, None))

    def test_chained_passes_walk_through_a_long_autonomous_session(self):
        messages = self.autonomous(steps=200, users=1, result="r" * 6_000)
        summary, passes = None, 0
        while passes < 20:
            plan = C.plan_compaction(messages, summary, keep_tail=6,
                                     max_source_tokens=3_000, tail_tokens=5_000)
            if plan is None:
                break
            summary = C.make_summary("## objective\nx", plan, model="m", gateway="g")
            self.assertEqual(C.validate_summary(messages, summary), (True, None))
            passes += 1
        self.assertGreater(passes, 1, "输入上限应当逼出多段压缩")
        self.assertGreater(summary["covered_to"], len(messages) * 0.8)

    def test_the_agent_sizes_the_tail_from_its_threshold(self):
        agent = A.Agent.__new__(A.Agent)
        for compact_at, expected in ((16_000, 4_000), (217_000, 27_125), (936_000, 40_000)):
            agent.compact_at = compact_at
            self.assertEqual(agent.compaction_tail_tokens(), expected)


GOOD_SUMMARY = ("## objective\n修 X\n## constraints\n- 不得改 Y\n## decisions\n- 用 A\n"
                "## files_changed\n- a.py\n## evidence\n- 测过\n## pending\n- 跑全量\n"
                "## risks\n- 无")
LOOPING_SUMMARY = ("## objective\nx\n## constraints\ny\n## decisions\nz\n"
                   "## files_changed\n" + "见 evidence 段。" * 300)


class SummaryAcceptanceTests(unittest.TestCase):
    """摘要装上去就永久替换了模型眼里的那段历史——残次品宁可不要。

    以前「只要非空就接受」。2026-09-20 在真实网关上测到一份写满 6000 token 后陷入重复循环的
    摘要（「见 evidence 段。见 evidence 段。…」，七个标题只有四个，漏掉 15 个锚定值）。
    """

    def test_a_complete_summary_is_accepted(self):
        self.assertEqual(C.summary_defects(GOOD_SUMMARY, "stop"), [])

    def test_truncation_repetition_and_missing_structure_are_named(self):
        self.assertEqual(C.summary_defects(GOOD_SUMMARY, "length"), ["truncated"])
        self.assertIn("degenerate", C.summary_defects(LOOPING_SUMMARY))
        self.assertEqual(C.summary_defects("一段没有标题的自由发挥。" * 5), ["structure"])

    def test_a_long_regular_bullet_list_is_not_mistaken_for_a_loop(self):
        listy = GOOD_SUMMARY + "\n" + "\n".join(
            f"- 文件 core/mod{i}.py：改了函数 f{i}，测试通过" for i in range(60))
        self.assertEqual(C.summary_defects(listy), [])

    def compact(self, *scripts, learned=None):
        from unittest import mock                     # noqa: PLC0415
        learned = {} if learned is None else learned
        agent = A.Agent.__new__(A.Agent)
        agent.messages = history(30)
        agent.model, agent.gateway = "kimi-k3-256k", "deepinfer"
        agent.session_id = "acceptance"
        agent.context_summary = None
        agent.compact_failed = None
        agent._compact_failed_key = None
        agent.last_total = 0
        agent.compact_at, agent.ctx_limit = 70_000, 100_000
        agent._trace_context = lambda *a, **k: None
        calls, queue = [], list(scripts)

        def provider(model, messages, **kwargs):
            calls.append({"route": (kwargs.get("gateway"), model),
                          "max_tokens": kwargs.get("max_tokens"),
                          "prompt": messages[0]["content"]})
            return iter(queue.pop(0))

        with mock.patch.object(A.client, "stream_chat", provider), \
             mock.patch.object(A.models_db, "thinking_off_rejected", return_value=False), \
             mock.patch.object(A.models_db, "note_thinking_off_rejected"), \
             mock.patch.object(A.models_db, "compact_needs_wide_budget",
                               side_effect=lambda g, m: learned.get((g, m), False)), \
             mock.patch.object(A.models_db, "note_compact_needs_wide_budget",
                               side_effect=lambda g, m: learned.__setitem__((g, m), True)):
            result = agent.force_compact(instructions="重点保留数据库迁移的决定")
        return agent, calls, str(result or "")

    @staticmethod
    def answer(text, reason="stop"):
        return [{"t": "text", "v": text}, {"t": "done", "reason": reason, "usage": {}}]

    def test_a_truncated_summary_is_retried_with_a_wider_budget_on_the_same_model(self):
        agent, calls, result = self.compact(
            self.answer(GOOD_SUMMARY[:60], "length"), self.answer(GOOD_SUMMARY))
        self.assertEqual([c["route"] for c in calls], [("deepinfer", "kimi-k3-256k")] * 2)
        self.assertGreater(calls[1]["max_tokens"], calls[0]["max_tokens"])
        self.assertEqual(agent.context_summary["content"], GOOD_SUMMARY)

    def test_a_route_that_needed_the_wide_budget_starts_there_next_time(self):
        """2026-09-24 实测 claude-opus-5-5@boyue：连续 4 段，每段 6000 额度那次必截断、
        16000 那次必通过（8.6K–9.9K），每段白跑 60 s。学到一次，下一段直接从大额度开始。"""
        learned = {}
        _, calls, _ = self.compact(
            self.answer(GOOD_SUMMARY[:60], "length"), self.answer(GOOD_SUMMARY),
            learned=learned)
        self.assertEqual([c["max_tokens"] for c in calls],
                         [A.COMPACT_MAX_TOKENS, A.COMPACT_RETRY_MAX_TOKENS])
        self.assertTrue(learned.get(("deepinfer", "kimi-k3-256k")))
        _, again, _ = self.compact(self.answer(GOOD_SUMMARY), learned=learned)
        self.assertEqual([c["max_tokens"] for c in again], [A.COMPACT_RETRY_MAX_TOKENS],
                         "一次就成，不再先撞 6000")

    def test_a_rescue_by_another_model_teaches_nothing_about_this_one(self):
        """小额度截断后是**别家**写成的：这不说明本 route 需要大额度，不记。"""
        learned = {}
        self.compact(self.answer(LOOPING_SUMMARY), self.answer(GOOD_SUMMARY),
                     learned=learned)
        self.assertEqual(learned, {})

    def test_the_learned_first_step_still_turns_thinking_off(self):
        agent = A.Agent.__new__(A.Agent)
        agent.model, agent.gateway = "claude-opus-5-5", "boyue"
        from unittest import mock                     # noqa: PLC0415
        with mock.patch.object(A.models_db, "thinking_off_rejected", return_value=False), \
                mock.patch.object(A.models_db, "compact_needs_wide_budget",
                                  return_value=True):
            first = agent.compaction_attempts()[0]
        self.assertEqual(first["max_tokens"], A.COMPACT_RETRY_MAX_TOKENS)
        self.assertEqual(first["thinking"], {"type": "disabled"})

    def test_a_looping_summary_is_never_installed(self):
        agent, calls, result = self.compact(
            self.answer(LOOPING_SUMMARY), self.answer(GOOD_SUMMARY))
        self.assertEqual(calls[0]["route"], ("deepinfer", "kimi-k3-256k"))
        self.assertNotEqual(calls[1]["route"], ("deepinfer", "kimi-k3-256k"))  # 换一家来写
        self.assertEqual(agent.context_summary["content"], GOOD_SUMMARY)

    def test_when_every_attempt_is_defective_history_stays_untouched(self):
        agent, calls, result = self.compact(
            self.answer(LOOPING_SUMMARY), self.answer("没有标题的东西。" * 9))
        self.assertIsNone(agent.context_summary)
        self.assertTrue(result.startswith("[摘要生成失败"), result)
        self.assertIn("被拒收", result)

    def test_compact_instructions_reach_the_prompt(self):
        agent, calls, _ = self.compact(self.answer(GOOD_SUMMARY))
        self.assertIn("重点保留数据库迁移的决定", calls[0]["prompt"])
        self.assertIn("此刻正在做的那一步", calls[0]["prompt"])


class RestoreAfterCompactionTests(unittest.TestCase):
    """压完立刻把工作现场带回来：最近动过的文件的**当前**内容 + 任务计划。

    以前只带回路径清单，模型得自己想起来去重读；Claude Code 压完会重读最近 5 个文件。
    内容在压缩那一刻读出、冻结进摘要记录，所以投影是确定的。
    """

    def setUp(self):
        import tempfile                               # noqa: PLC0415
        from unittest import mock                     # noqa: PLC0415
        self.mock = mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)

    def write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def agent(self, touched, **attrs):
        agent = A.Agent.__new__(A.Agent)
        messages = [{"role": "system", "content": "sys"}]
        for index, (tool, path) in enumerate(touched):
            args = {"path": path}
            if tool != "read_file":
                args["content" if tool == "write_file" else "new"] = "x"
            messages.extend(turn(index, tool=tool, args=args))
        for index in range(len(touched), len(touched) + 12):
            messages.extend(turn(index, tool="bash", args={"command": "make"}))
        agent.messages = messages
        agent.model, agent.gateway = "kimi-k3-256k", "deepinfer"
        agent.session_id = "restore"
        agent.context_summary = None
        agent.compact_failed = None
        agent._compact_failed_key = None
        agent.last_total = 0
        agent.compact_at, agent.ctx_limit = 70_000, 100_000
        agent.workspace_root = self.root
        agent._trace_context = lambda *a, **k: None
        for key, value in attrs.items():
            setattr(agent, key, value)
        return agent

    def compact(self, agent):
        reply = [{"t": "text", "v": GOOD_SUMMARY},
                 {"t": "done", "reason": "stop", "usage": {}}]
        with self.mock.patch.object(A.client, "stream_chat", lambda *a, **k: iter(reply)), \
             self.mock.patch.object(A.models_db, "thinking_off_rejected", return_value=False):
            agent.force_compact()
        projection = agent.project_context([])
        return agent.context_summary, projection.messages[1]["content"]

    def test_current_content_of_recently_edited_files_comes_back(self):
        path = self.write("rules.py", "THRESHOLD = 0.01  # 磁盘上的当前内容\n")
        summary, shown = self.compact(self.agent([("edit_file", path)]))
        self.assertEqual([f["path"] for f in summary["restored"]["files"]], [path])
        self.assertIn("THRESHOLD = 0.01  # 磁盘上的当前内容", shown)
        self.assertIn("<restored-files", shown)

    def test_edited_files_win_the_limited_slots_over_files_only_read(self):
        edited = self.write("edited.py", "edited\n")
        reads = [self.write(f"read{i}.py", f"read {i}\n") for i in range(8)]
        agent = self.agent([("edit_file", edited)] + [("read_file", p) for p in reads])
        summary, _ = self.compact(agent)
        paths = [f["path"] for f in summary["restored"]["files"]]
        self.assertEqual(paths[0], edited)
        # 只读过的文件多半是参考文档：名额和篇幅都收紧
        self.assertEqual(len(paths), 1 + C.RESTORE_READONLY_FILES)

    def test_read_only_files_get_a_smaller_share(self):
        doc = self.write("manual.md", "字" * 40_000)
        summary, _ = self.compact(self.agent([("read_file", doc)]))
        self.assertEqual(summary["restored"]["files"][0]["chars"], C.RESTORE_READONLY_CHARS)

    def test_the_read_guard_applies_nothing_outside_the_workspace_comes_back(self):
        inside = self.write("inside.py", "inside\n")
        agent = self.agent([("edit_file", inside), ("edit_file", "/etc/hostname"),
                            ("edit_file", os.path.join(self.root, ".env"))])
        self.write(".env", "API_KEY=sk-not-for-the-model\n")
        summary, shown = self.compact(agent)
        self.assertEqual([f["path"] for f in summary["restored"]["files"]], [inside])
        self.assertNotIn("sk-not-for-the-model", shown)

    def test_files_and_totals_are_bounded(self):
        paths = [self.write(f"big{i}.py", "字" * 40_000) for i in range(5)]
        summary, _ = self.compact(self.agent([("write_file", p) for p in paths]))
        files = summary["restored"]["files"]
        self.assertTrue(all(f["chars"] <= C.RESTORE_FILE_CHARS for f in files))
        self.assertLessEqual(sum(f["chars"] for f in files), C.RESTORE_TOTAL_CHARS)
        self.assertTrue(files[0]["truncated"])

    def test_missing_binary_and_permission_cases_are_skipped_quietly(self):
        gone = os.path.join(self.root, "deleted.py")
        binary = self.write("blob.bin", "\x00\x01\x02")
        ok = self.write("ok.py", "ok\n")
        summary, _ = self.compact(self.agent(
            [("edit_file", gone), ("edit_file", binary), ("edit_file", ok)]))
        self.assertEqual([f["path"] for f in summary["restored"]["files"]], [ok])
        locked = self.agent([("edit_file", ok)],
                            hook_cfg={"permissions": {"read_file": "ask"}})
        summary, _ = self.compact(locked)
        self.assertEqual(summary["restored"]["files"], [])      # 没人能点「同意」，就不读

    def test_the_unfinished_task_plan_comes_back(self):
        plan = {"items": [{"content": "修解析器", "status": "completed"},
                          {"content": "跑全量测试", "status": "in_progress"},
                          {"content": "写 DEVLOG", "status": "pending"}], "explanation": ""}
        agent = self.agent([], compaction_state=lambda: {"task_plan": plan})
        summary, shown = self.compact(agent)
        self.assertIn("跑全量测试", summary["restored"]["task_plan"])
        self.assertIn("<task-plan>", shown)
        done = {"items": [{"content": "都做完了", "status": "completed"}], "explanation": ""}
        agent = self.agent([], compaction_state=lambda: {"task_plan": done})
        summary, shown = self.compact(agent)
        self.assertEqual(summary["restored"]["task_plan"], "")  # 全部完成就不占地方
        self.assertNotIn("<task-plan>", shown)

    def test_a_failing_provider_never_fails_the_compaction(self):
        def boom():
            raise RuntimeError("session is gone")
        agent = self.agent([], compaction_state=boom)
        summary, _ = self.compact(agent)
        self.assertIsNotNone(summary)


class BreakerTests(unittest.TestCase):
    """自动压缩连续失败 3 次就歇一阵——但不是永久的。

    每次失败最多烧 3 个请求；不设闸就是每一轮都再烧一遍。以前的规则走了另一个极端：同一份
    计划失败过就**永不重试**，网关抖一次 503，这个会话就再也不会自动压缩了。
    """

    def agent(self):
        agent = A.Agent.__new__(A.Agent)
        agent.messages = history(40, result="x" * 9_000)
        agent.model, agent.gateway = "kimi-k3-256k", "deepinfer"
        agent.session_id = "breaker"
        agent.context_summary = None
        agent.compact_failed = None
        agent._compact_failed_key = None
        agent.last_total = 0
        agent.compact_at, agent.ctx_limit = 20_000, 100_000
        agent._trace_context = lambda *a, **k: None
        return agent

    def fail_once(self, agent):
        from unittest import mock                     # noqa: PLC0415
        down = A.client.APIError("HTTP 503", kind="transient_http", status=503)

        def provider(*a, **k):
            raise down

        with mock.patch.object(A.client, "stream_chat", provider), \
             mock.patch.object(agent, "compaction_fallback_route", return_value=None):
            return str(agent.maybe_compact() or "")

    def test_a_failed_plan_is_retried_once_the_short_pause_is_over(self):
        from unittest import mock                     # noqa: PLC0415
        agent = self.agent()
        self.assertTrue(self.fail_once(agent).startswith("[摘要生成失败"))
        self.assertIsNone(agent._compaction_plan())               # 刚失败：先不重试
        with mock.patch.object(A.time, "monotonic",
                               return_value=A.time.monotonic()
                               + A.COMPACT_SAME_PLAN_RETRY_SECONDS + 1):
            self.assertIsNotNone(agent._compaction_plan())        # 过一会儿：再给机会

    def test_three_consecutive_failures_pause_automatic_compaction(self):
        from unittest import mock                     # noqa: PLC0415
        agent = self.agent()
        clock = [A.time.monotonic()]
        with mock.patch.object(A.time, "monotonic", lambda: clock[0]):
            messages = []
            for _ in range(A.COMPACT_BREAKER_FAILURES):
                messages.append(self.fail_once(agent))
                clock[0] += A.COMPACT_SAME_PLAN_RETRY_SECONDS + 1
            self.assertNotIn("不再自动压缩", messages[0])
            self.assertIn("不再自动压缩", messages[-1])             # 第三次才说，只说一次
            self.assertTrue(agent.compaction_breaker_open())
            self.assertIsNone(agent._compaction_plan())
            self.assertIsNotNone(agent._compaction_plan(force=True))   # /compact 不受影响
            clock[0] += A.COMPACT_BREAKER_SECONDS
            self.assertFalse(agent.compaction_breaker_open())     # 冷却期过了
            self.assertIsNotNone(agent._compaction_plan())

    def test_changing_the_model_closes_the_breaker(self):
        agent = self.agent()
        agent._compact_failures = A.COMPACT_BREAKER_FAILURES
        agent._compact_failed_at = A.time.monotonic()
        self.assertTrue(agent.compaction_breaker_open())
        agent.set_model("glm-5.3")
        self.assertFalse(agent.compaction_breaker_open())

    def test_a_success_resets_the_count(self):
        from unittest import mock                     # noqa: PLC0415
        agent = self.agent()
        self.fail_once(agent)
        self.assertEqual(agent._compact_failures, 1)
        reply = [{"t": "text", "v": GOOD_SUMMARY},
                 {"t": "done", "reason": "stop", "usage": {}}]
        with mock.patch.object(A.client, "stream_chat", lambda *a, **k: iter(reply)):
            agent.force_compact()
        self.assertEqual(agent._compact_failures, 0)


class GenerationInheritanceTests(unittest.TestCase):
    """链式压缩不能让锚定的值和工作集随代数蒸发。"""

    def two_generations(self):
        messages = [{"role": "system", "content": "sys"}]
        messages.extend(turn(0, tool="edit_file", args={"path": "/repo/rules.py"},
                             result="规则：ER/PR <1% 必须判为阴性"))
        for index in range(1, 30):
            messages.extend(turn(index))
        first_plan = C.plan_compaction(messages, None, keep_tail=20)
        # 第一代摘要的正文**故意不复述**那个值：模型改写措辞是常态
        first = C.make_summary("## objective\n做判读规则", first_plan, model="m", gateway="g")
        second_plan = C.plan_compaction(messages, first, keep_tail=6)
        second = C.make_summary("## objective\n继续", second_plan, model="m", gateway="g")
        return first, second_plan, second

    def test_pinned_values_reach_the_second_generation(self):
        first, plan, second = self.two_generations()
        self.assertTrue(any("<1%" in line for line in first["pinned"]))
        self.assertTrue(any("<1%" in line for line in second["pinned"]),
                        "第一代锚定的阈值在第二代丢了")
        self.assertIn("<1%", C.summary_prompt(plan))     # 也要求模型在新摘要里复述它

    def test_working_set_reaches_the_second_generation(self):
        first, _, second = self.two_generations()
        self.assertIn({"path": "/repo/rules.py", "verb": "改"}, first["working_set"])
        paths = {item["path"]: item["verb"] for item in second["working_set"]}
        self.assertEqual(paths.get("/repo/rules.py"), "改")

    def test_budgets_still_hold_across_generations(self):
        _, _, second = self.two_generations()
        self.assertLessEqual(len(second["pinned"]), C.ANCHOR_MAX_LINES)
        self.assertLessEqual(len(second["working_set"]), C.WORKING_SET_LIMIT)

    def test_first_generation_is_unchanged(self):
        messages = history(20)
        plan = C.plan_compaction(messages, None, keep_tail=6)
        self.assertEqual(plan["inherited"], {})
        summary = C.make_summary("## objective\nx", plan, model="m", gateway="g")
        self.assertEqual(summary["working_set"], C.working_set(plan["source_messages"]))


class BoundedSourceTests(unittest.TestCase):
    def test_plan_is_cut_to_the_source_budget_at_a_turn_boundary(self):
        messages = history(40, result="x" * 4_000)
        whole = C.plan_compaction(messages, None, keep_tail=6)
        bounded = C.plan_compaction(
            messages, None, keep_tail=6, max_source_tokens=2_000)
        self.assertLess(bounded["source_turns"], whole["source_turns"])
        self.assertLessEqual(
            C.estimate_tokens(C.summary_prompt(bounded)), 2_000)
        # 边界必须落在完整轮次上：覆盖到的最后一条是 tool，下一条是 user
        self.assertEqual(
            messages[bounded["covered_to"]]["role"], "user")

    def test_chain_advances_until_only_the_tail_is_left(self):
        messages = history(40, result="x" * 4_000)
        summary, covered, rounds = None, 0, 0
        while rounds < 20:
            plan = C.plan_compaction(
                messages, summary, keep_tail=6, max_source_tokens=2_000)
            if plan is None:
                break
            self.assertGreater(plan["covered_to"], covered)
            covered = plan["covered_to"]
            summary = C.make_summary(
                "## objective\nx", plan, model="m", gateway="g")
            rounds += 1
        self.assertGreater(rounds, 1, "预算上限应当逼出多段压缩")
        self.assertIsNone(C.plan_compaction(
            messages, summary, keep_tail=6, max_source_tokens=2_000))

    def test_a_single_oversized_turn_still_gets_compacted(self):
        messages = history(8, result="x" * 40_000)
        plan = C.plan_compaction(
            messages, None, keep_tail=2, max_source_tokens=10)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["source_turns"], 1)


class WorkingSetTests(unittest.TestCase):
    def test_write_wins_over_read_and_the_list_rides_the_summary(self):
        messages = history(1)
        messages.extend(turn(1, args={"path": "/repo/a.py"}))
        messages.extend(turn(2, tool="edit_file", args={"path": "/repo/a.py"}))
        messages.extend(turn(3, tool="write_file",
                            args={"path": "/repo/b.py", "content": "x"}))
        items = C.working_set(messages)
        self.assertEqual(
            {item["path"]: item["verb"] for item in items},
            {"/repo/file0.py": "读", "/repo/a.py": "改", "/repo/b.py": "写"})
        plan = C.plan_compaction(messages, None, keep_tail=1)
        summary = C.make_summary("## objective\nx", plan,
                                 model="m", gateway="g")
        rendered = C._summary_messages(summary)[0]["content"]
        self.assertIn("<working-set>", rendered)
        self.assertIn("/repo/a.py", rendered)

    def test_no_files_means_no_working_set_block(self):
        messages = history(3, tool="bash", args={"command": "ls"})
        plan = C.plan_compaction(messages, None, keep_tail=1)
        summary = C.make_summary("## objective\nx", plan,
                                 model="m", gateway="g")
        self.assertEqual(summary["working_set"], [])
        self.assertNotIn(
            "<working-set>", C._summary_messages(summary)[0]["content"])


class PromptShapeTests(unittest.TestCase):
    def test_instruction_follows_the_transcript_and_names_the_objective(self):
        plan = C.plan_compaction(history(4), None, keep_tail=1)
        prompt = C.summary_prompt(plan)
        self.assertLess(prompt.index("<会话前缀>"), prompt.index("## objective"))
        self.assertIn("不是「整理摘要」这件事", prompt)
        for heading in ("objective", "constraints", "decisions", "files_changed",
                        "evidence", "pending", "risks"):
            self.assertIn(f"## {heading}", prompt)


class EffectLineTests(unittest.TestCase):
    def test_effect_line_reports_tokens_and_names_a_stand_in_model(self):
        import zylab                                   # noqa: PLC0415
        self.assertEqual(
            zylab._compaction_effect(215_315, 31_931, "kimi-k3", "kimi-k3"),
            "215,315 → 31,931 tokens")
        self.assertIn(
            "由 glm-5.3 代写",
            zylab._compaction_effect(200_000, 20_000, "glm-5.3", "kimi-k3"))
        # 拿不到数字时不吹牛
        self.assertEqual(
            zylab._compaction_effect(None, None), "已生成摘要")


class CatalogDelistingTests(unittest.TestCase):
    """目录成员变化必须让席位选择跟着变。

    2026-09-08 实测：DeepInfer 下架了 deepseek-v4-pro / -pro-0813（403
    model_not_available），但缓存里它们仍是 status=ok，席位解析照选不误。
    fetch_catalog 以前只做并集，从不标记「消失」。
    """

    def catalog(self, ids):
        return [{"id": mid, "max_model_len": 262_144} for mid in ids]

    def test_departures_are_marked_not_deleted_and_can_come_back(self):
        import tempfile                                # noqa: PLC0415
        from pathlib import Path                       # noqa: PLC0415
        from unittest import mock                      # noqa: PLC0415
        from core import models                        # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(models, "CACHE", Path(tmp) / "models.json"):
                with mock.patch.object(
                        models.client, "list_models",
                        return_value=self.catalog(["alive", "leaving"])):
                    models.fetch_catalog("deepinfer")
                models.update_record(
                    "deepinfer", "leaving",
                    lambda rec: rec.update(status="ok", supports_tools=True))

                # 下架：listed 落下、status 从 ok 变 delisted，记录本身留着
                with mock.patch.object(
                        models.client, "list_models",
                        return_value=self.catalog(["alive"])):
                    models.fetch_catalog("deepinfer")
                gone = models.get("deepinfer", "leaving")
                self.assertEqual(gone["listed"], False)
                self.assertEqual(gone["status"], "delisted")
                self.assertTrue(gone.get("delisted_at"))
                self.assertEqual(
                    models.get("deepinfer", "alive")["listed"], True)

                # 回来了：listed 恢复，但 status 只回到 listed —— 要重新 probe
                with mock.patch.object(
                        models.client, "list_models",
                        return_value=self.catalog(["alive", "leaving"])):
                    models.fetch_catalog("deepinfer")
                back = models.get("deepinfer", "leaving")
                self.assertEqual(back["listed"], True)
                self.assertEqual(back["status"], "listed")
                self.assertNotIn("delisted_at", back)

    def test_delisted_models_are_not_used_as_the_compaction_fallback(self):
        from unittest import mock                      # noqa: PLC0415
        from core import models                        # noqa: PLC0415

        agent = A.Agent.__new__(A.Agent)
        agent.gateway, agent.model = "deepinfer", "kimi-k3-256k"
        seats = (
            {"seat": "GLM", "family": "智谱",
             "candidates": (("deepinfer", "glm-5.3"),)},
            {"seat": "DeepSeek", "family": "deepseek",
             "candidates": (("deepinfer", "deepseek-v4-pro"),)},
        )
        records = {
            ("deepinfer", "glm-5.3"): {"status": "delisted"},
            ("deepinfer", "deepseek-v4-pro"): {"status": "ok"},
        }
        with (mock.patch.object(models, "WORKFLOW_DEFAULT_SEATS", seats),
              mock.patch.object(
                  models, "get",
                  side_effect=lambda g, m: records.get((g, m), {}))):
            route = agent.compaction_fallback_route()
        self.assertEqual(route["model"], "deepseek-v4-pro")

    def test_fallback_prefers_a_different_model_family(self):
        agent = A.Agent.__new__(A.Agent)
        agent.gateway, agent.model = "deepinfer", "kimi-k3-256k"
        route = agent.compaction_fallback_route()
        from core import models                        # noqa: PLC0415
        self.assertNotEqual(
            models.family_of(route["model"]), models.family_of(agent.model))


class PinnedFactsTests(unittest.TestCase):
    """高信号「值」必须活过压缩 —— 摘要改写措辞也不能把它们改掉。

    钉的是 2026-09-17 反馈书差距 1/2：`summary_prompt` 在模型看到之前就把工具
    结果截到 600 字符，位于中段的阈值根本进不了摘要请求。模型保不住它没见过的
    东西，所以值要单独摘出来、逐字带回。
    """

    def _messages_with(self, *contents):
        messages = [{"role": "system", "content": "sys"}]
        for index, text in enumerate(contents):
            block = turn(index)
            block[2]["content"] = text        # 工具结果就是值的来源
            messages.extend(block)
        messages.extend(turn(99))
        return messages

    def test_values_are_lifted_even_when_buried_past_the_truncation(self):
        # 值刻意放在第 900 字符之后——旧行为下工具结果被截到 600，它必然丢。
        buried = "填充。" * 300 + "\n阈值规则：误差 <1% 视为通过\n" + "尾部。" * 50
        messages = self._messages_with(buried)
        pinned = C.anchor_lines(messages)
        self.assertTrue(any("<1%" in line for line in pinned), pinned)
        plan = C.plan_compaction(messages, None, keep_tail=1)
        prompt = C.summary_prompt(plan)
        # 截断仍然发生（提示不能重新撑爆），但值另走一条路进了提示
        self.assertIn("<必须逐字保留的值>", prompt)
        self.assertIn("<1%", prompt)

    def test_only_values_are_lifted_not_narrative(self):
        messages = self._messages_with(
            "这里是一段普通叙述，没有任何阈值或硬约束。",
            "上限 95% 以上算超标",
            "这一步**必须**先做",
            "[rejected] 裸数字匹配 —— 脱离语境无意义")
        pinned = C.anchor_lines(messages)
        self.assertNotIn("这里是一段普通叙述，没有任何阈值或硬约束。", pinned)
        self.assertEqual(len(pinned), 3, pinned)

    def test_pinned_rides_the_summary_verbatim(self):
        messages = self._messages_with("阈值 <1% 视为通过")
        plan = C.plan_compaction(messages, None, keep_tail=1)
        summary = C.make_summary("## objective\nx", plan,
                                 model="m", gateway="g")
        self.assertTrue(summary["pinned"])
        rendered = C._summary_messages(summary)[0]["content"]
        self.assertIn("<pinned-facts>", rendered)
        self.assertIn("<1%", rendered)

    def test_old_summaries_without_pinned_still_render_and_validate(self):
        """加字段不能打翻旧会话：validate_summary 不枚举字段，读侧必须 .get()。"""
        messages = self._messages_with("阈值 <1% 视为通过")
        plan = C.plan_compaction(messages, None, keep_tail=1)
        summary = C.make_summary("## objective\nx", plan,
                                 model="m", gateway="g")
        del summary["pinned"]                       # 模拟改动前写下的摘要
        rendered = C._summary_messages(summary)[0]["content"]
        self.assertNotIn("<pinned-facts>", rendered)
        self.assertTrue(C.validate_summary(messages, summary)[0])

    def test_fidelity_check_is_deterministic_and_catches_paraphrase(self):
        """反馈书建议再调一次模型核对；这里用确定性判定替代。

        理由：每次压缩多一次 provider 往返，而让模型自查自己的摘要恰恰是最不
        可靠的一环。能用字符串包含精确判定的事，不花钱去猜。
        """
        pinned = ["阈值 <1% 视为通过", "这一步必须先做"]
        missing = C.missing_anchors("用约百分之一为界；这一步必须先做", pinned)
        self.assertEqual(missing, ["阈值 <1% 视为通过"])
        self.assertEqual(
            C.missing_anchors("constraints: <1% 视为通过；必须先做", pinned), [])

    def test_real_world_noise_is_not_anchored(self):
        """回归：真实会话上头 6 条锚定里 5 条是垃圾（2026-09-17 实测）。

        两类假阳性各有来头，合成测试永远碰不到，所以用真实形态钉住：
          `{x:>10,}`            f-string 对齐格式，`:>10` 长得像「大于 10」
          `...s1nv8%tmp%x.py`   URL 编码路径，`8%` 长得像百分数
        预算只有 24 行，放进垃圾等于把真正的阈值挤出去。
        """
        noise = [
            "20260821T115615_158857__tmp%tmpr62s1nv8%x.py",
            "-rw-r--r-- 1 root root    3 Aug 21 11:56 x.py",
            'print(DIM(f"  {g:12} in {c[\'inputTokens\']:>10,} / "',
            "0123456789abcdef0123456789abcdef",
        ]
        for line in noise:
            with self.subTest(line=line[:40]):
                self.assertFalse(C._anchor_worthy(line), line)
        for line in ("阈值规则：误差 <1% 视为通过", "阈值 95% 以上为超标",
                     "# 时间戳必须带亚秒：同一秒内连改两次会互相覆盖"):
            with self.subTest(line=line[:40]):
                self.assertTrue(C._anchor_worthy(line), line)

    def test_arrows_are_not_comparators(self):
        """`->` / `=>` / `<-` 里的符号不是比较符。

        2026-09-17 真实会话上 `internal.example -> 10.12.111.139` 被 `_CMP`
        命中了 `'> 1'`（箭头里的 `>`，负向回顾漏了 `-`）。一个以精度为职责的
        机制不能留下说不清为什么会命中的行。
        """
        for line in ("host.example -> 10.12.111.139", "a => 42", "x <- 7"):
            with self.subTest(line=line):
                self.assertFalse(C._anchor_worthy(line), line)
        # 版本约束里的 `>=` 是正当要求，不能因为修箭头而连坐
        self.assertTrue(
            C._anchor_worthy("harness 要求 >=24.0.0，否则装不上"))

    def test_same_value_in_different_phrasings_collapses(self):
        """同义复述吃预算：真实会话里「省 44%」出现三种写法，只该钉一条。"""
        content = "\n".join([
            "工具结果老化：长会话上下文省 44%",
            "- `5d5be68` 工具结果老化（省 44% 上下文）",
            "- **工具结果老化**：长会话上下文省 44%（8-21）",
            "缓存命中率 70%",
        ])
        pinned = C.anchor_lines([{"role": "tool", "content": content}])
        self.assertEqual(len(pinned), 2, pinned)
        self.assertTrue(any("44%" in line for line in pinned), pinned)
        self.assertTrue(any("70%" in line for line in pinned), pinned)

    def test_budget_goes_to_the_valuable_lines_first(self):
        """噪声在真实会话里出现得比判据早 —— 先到先得会把额度让给噪声。"""
        early_rule_only = ["这一步必须先做" for _ in range(30)]
        late_value = "阈值 <1% 视为通过"
        content = "\n".join(early_rule_only + [late_value])
        pinned = C.anchor_lines([{"role": "tool", "content": content}])
        self.assertIn(late_value, pinned)

    def test_anchors_are_bounded(self):
        """不能因为「保留值」把摘要请求重新撑爆 —— 那是 09-07 故障的成因。"""
        many = "\n".join(f"第 {i} 项上限 {i}% 以上" for i in range(200))
        pinned = C.anchor_lines([{"role": "tool", "content": many}])
        self.assertLessEqual(len(pinned), C.ANCHOR_MAX_LINES)
        self.assertLessEqual(sum(len(x) for x in pinned), C.ANCHOR_MAX_CHARS)


class AgingPolicyTests(unittest.TestCase):
    """老化力度随窗口放宽，但永不比基准更狠（反馈书差距 3）。"""

    def test_base_window_is_byte_for_byte_unchanged(self):
        self.assertEqual(
            A.aging_policy(A.AGE_SCALE_BASE),
            {"age_after_turns": A.AGE_AFTER_TURNS,
             "age_keep_head": A.AGE_KEEP_HEAD,
             "age_min_chars": A.AGE_MIN_CHARS})

    def test_small_window_never_gets_stricter_than_base(self):
        for limit in (0, None, 8_000, 128_000):
            with self.subTest(limit=limit):
                policy = A.aging_policy(limit)
                self.assertEqual(policy["age_keep_head"], A.AGE_KEEP_HEAD)

    def test_large_window_relaxes_and_stays_bounded(self):
        policy = A.aging_policy(1_049_000)
        self.assertGreater(policy["age_keep_head"], A.AGE_KEEP_HEAD)
        ceiling = A.aging_policy(100_000_000)
        self.assertEqual(
            ceiling["age_keep_head"],
            int(A.AGE_KEEP_HEAD * A.AGE_SCALE_MAX))


class CompactionDepthTests(unittest.TestCase):
    """压缩一旦开始，就压到阈值的一半以下再停。

    以前是「压到刚好装得下就停」：一段摘要的输入有上限，长会话一段压不完，压完仍占阈值的
    52–57%，几万 token 之后又得再压一次。2026-09-20 用真实压缩链在 7 个真实会话上逐请求回放
    （compact_at=200,000）：压到一半以下，累计输入 −11.8%、压缩事件 4 → 3，代价是摘要请求
    4 → 6 段。阈值更小时一段本来就能压到远低于一半，这个开关不起作用。
    """

    def agent(self, *, turns=60, chars=9_000, compact_at=20_000):
        agent = A.Agent.__new__(A.Agent)
        agent.messages = history(turns, result="x" * chars)
        agent.model, agent.gateway = "kimi-k3-256k", "deepinfer"
        agent.session_id = "depth"
        agent.context_summary = None
        agent.compact_failed = None
        agent._compact_failed_key = None
        agent.last_total = 0
        agent.compact_at, agent.ctx_limit = compact_at, compact_at + 30_000
        agent._trace_context = lambda *a, **k: None
        return agent

    def pressure(self, agent):
        return agent.project_context(A.tools.SCHEMA).report[
            "untrimmed_estimated_tokens"]

    def summarize(self, agent, **patches):
        """跑完整条压缩链，返回每段摘要请求的次数。"""
        from unittest import mock                     # noqa: PLC0415
        calls = []

        def provider(model, messages, **kwargs):
            calls.append(model)
            return iter([{"t": "text", "v": "## objective\n跟踪" + FULL_SUMMARY_REST}])

        with mock.patch.object(A.client, "stream_chat", provider), \
             mock.patch.multiple(A, **patches):
            agent.maybe_compact()
        return calls

    def test_the_target_is_half_the_threshold(self):
        """这个数是量出来的，不是随手定的：改它之前先重跑一遍真实会话的回放。"""
        self.assertEqual(self.agent(compact_at=200_000).compaction_deep_target(),
                         100_000)

    def test_the_trigger_is_still_the_threshold_not_the_deep_target(self):
        """压得更深，不等于压得更早——早压会白白丢掉还用得上的细节。"""
        agent = self.agent(turns=20)
        pressure = self.pressure(agent)  # 老化还在省，所以自动压缩这时还不该动手
        self.assertLess(pressure, agent.compact_at)
        self.assertGreater(pressure, agent.compaction_deep_target())
        self.assertIsNone(agent._compaction_plan())
        self.assertIsNotNone(agent._compaction_plan(deep=True),
                             "已经在压的这一轮里，同样的压力要接着压")

    def test_the_chain_stops_once_it_is_below_the_target(self):
        agent = self.agent(turns=12, chars=200)
        self.assertLess(self.pressure(agent), agent.compaction_deep_target())
        self.assertIsNone(agent._compaction_plan(deep=True))

    def test_a_stale_measured_total_does_not_drive_the_deep_pass(self):
        """last_total 是**压缩之前**那次请求的读数：续压时拿它当依据就永远嫌大。"""
        agent = self.agent(turns=12, chars=200)   # 结果太小不值得老化：aged 为 0
        agent.last_total = 10 * agent.compact_at
        self.assertIsNotNone(agent._compaction_plan(),
                             "非续压仍相信 provider 的读数（估算可能偏低）")
        self.assertIsNone(agent._compaction_plan(deep=True))

    def test_a_deep_pass_never_falls_back_to_the_tail_only_plan(self):
        """尾部已经在预算之内还接着压，只能啃尾巴，白烧一个请求。"""
        from unittest import mock                     # noqa: PLC0415
        agent = self.agent()
        real = A.context_projection.plan_compaction

        def only_legacy(*args, **kwargs):
            if kwargs.get("tail_tokens"):
                return None                           # 尾部预算装得下整段
            return real(*args, **kwargs)

        with mock.patch.object(A.context_projection, "plan_compaction",
                               side_effect=only_legacy):
            self.assertIsNotNone(agent._compaction_plan(force=True))
            self.assertIsNone(agent._compaction_plan(deep=True))

    def test_it_compacts_deeper_than_the_old_stop_at_the_threshold_rule(self):
        """一段摘要的输入有上限，长会话一段压不完——这时深浅才看得出差别。

        阈值这里**刻意比别的用例高**。压缩压不掉的那部分（system 提示 + 工具
        定义 ≈ 6.7K，加上必须留住的最近几轮）是一条地板；`compact_at=20_000`
        时目标 10,000 离地板只剩三十几个 token，于是 2026-09-21 grep 的工具
        描述加了两句「这是 Python 正则方言」，这条用例就红了——量的是描述的
        字数，不是压缩的深浅。24,000 留出约 2K 余量，测的才是它要测的东西。
        """
        bounded = {"COMPACT_SOURCE_TOKENS": 3_000, "COMPACT_MAX_PASSES": 8}
        old = self.agent(compact_at=24_000)
        old_passes = self.summarize(old, COMPACT_DEEP_FRACTION=1.0, **bounded)
        new = self.agent(compact_at=24_000)
        new_passes = self.summarize(new, **bounded)

        self.assertLess(self.pressure(new), new.compaction_deep_target())
        self.assertGreater(self.pressure(old), old.compaction_deep_target())
        self.assertLess(self.pressure(new), self.pressure(old))
        self.assertGreater(len(new_passes), len(old_passes),
                           "压得更深就是多发几段摘要请求换来的")

    def test_every_summary_in_the_chain_is_still_verified_against_raw(self):
        agent = self.agent()
        self.summarize(agent, COMPACT_SOURCE_TOKENS=3_000, COMPACT_MAX_PASSES=8)
        ok, reason = C.validate_summary(
            agent.messages, agent.context_summary,
            C.group_turns(C.build_units(
                agent.messages, C._system_prefix(agent.messages))[0]))
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
