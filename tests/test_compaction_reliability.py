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


if __name__ == "__main__":
    unittest.main()


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
