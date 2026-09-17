"""压缩可靠性：思考吃光额度、分段、参数老化、工作集。

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


class ArgumentAgingTests(unittest.TestCase):
    def test_old_write_arguments_are_projected_and_stay_valid_json(self):
        body = "print('x')\n" * 400
        messages = history(1)
        messages.extend(turn(1, tool="write_file",
                             args={"path": "/repo/big.py", "content": body}))
        for index in range(2, 8):
            messages.extend(turn(index))
        units, _ = C.build_units(messages, C._system_prefix(messages))
        projected = C._project_units(units, 3, 400, 200)
        aged = [p for item in projected for p in item["previews"]
                if p["tier"] == "aged-arguments"]
        self.assertEqual(len(aged), 1, "旧轮次的大参数应当被收起")
        self.assertGreater(aged[0]["saved_chars"], 1_000)
        for item in projected:
            for message in item["messages"]:
                for call in message.get("tool_calls") or []:
                    args = json.loads(call["function"]["arguments"])
                    self.assertIn("path", args)      # 结构不动，只换字符串值

    def test_recent_arguments_are_untouched(self):
        body = "print('x')\n" * 400
        messages = history(1)
        messages.extend(turn(1, tool="write_file",
                             args={"path": "/repo/big.py", "content": body}))
        units, _ = C.build_units(messages, C._system_prefix(messages))
        projected = C._project_units(units, 3, 400, 200)
        self.assertEqual(
            [p for item in projected for p in item["previews"]
             if p["tier"] == "aged-arguments"], [])


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


if __name__ == "__main__":
    unittest.main()
