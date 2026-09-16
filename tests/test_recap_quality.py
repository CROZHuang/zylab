"""recap 的信息质量：时序、去重、锚点、否决、下一步、中文列宽。

来源 docs/FEEDBACK-recap-quality-20260908.md（zylab 自查）。六条全部核对属实。
最要命的是时序：`decisions[:2]` 取的是**最早**两条，而长会话里早期结论常被后来
推翻（实例：v14 作废、v17b 判不能发）。拿着过时结论干活比没有 recap 更糟。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab                                          # noqa: E402
from core import goals as GOALS                       # noqa: E402
from core import tui                                  # noqa: E402


def plain(text):
    return tui.strip_ansi(text) if hasattr(tui, "strip_ansi") else text


def record(*, summary=None, plan=None, goal=None, messages=None):
    value = {"messages": messages or [
        {"role": "user", "content": f"第 {i} 轮"} for i in range(40)]}
    if summary is not None:
        value["context"] = {"summary": {"status": "valid",
                                        "content": summary}}
    if plan is not None:
        value["task_plan"] = plan
    if goal is not None:
        value["goal"] = goal
    return value


class DecisionOrderingTests(unittest.TestCase):
    def test_anchored_decisions_render_newest_first(self):
        text = plain(zylab.format_recap(record(summary=(
            "## objective\n跑通流水线\n"
            "## decisions\n"
            "- [t3] 冻结 v4 疾病树\n"
            "- [t41] v14 作废，改用 v17b\n"
            "- [t22] 判读归一化用 ordinal_score\n")), replay_limit=5))
        first = text.index("v14 作废")
        self.assertLess(first, text.index("ordinal_score"))
        self.assertNotIn("冻结 v4", text)             # 最早的那条被挤掉了
        self.assertIn("·t41", text)                   # 锚点可回溯

    def test_without_anchors_the_template_order_is_trusted(self):
        text = plain(zylab.format_recap(record(summary=(
            "## decisions\n- 最新结论\n- 更早的结论\n")), replay_limit=5))
        self.assertLess(text.index("最新结论"), text.index("更早的结论"))

    def test_rejected_entries_get_their_own_line(self):
        text = plain(zylab.format_recap(record(summary=(
            "## decisions\n"
            "- [t9] 采用规则掩膜\n"
            "- [rejected] CAIX 盒状 201 行 —— 形态学不成立，已撤回\n")),
            replay_limit=5))
        self.assertIn("已否", text)
        self.assertIn("CAIX 盒状", text)
        # 否决不占「已定」的名额
        self.assertIn("采用规则掩膜", text)


class PendingDedupeTests(unittest.TestCase):
    def test_plan_wins_and_equivalent_pending_is_dropped(self):
        text = plain(zylab.format_recap(record(
            summary="## pending\n- 跑完三模型对抗审查\n- 写发布说明\n",
            plan={"items": [
                {"content": "跑完三模型对抗审查", "status": "in_progress"},
                {"content": "落盘冲突清单", "status": "pending"}]}),
            replay_limit=5))
        self.assertEqual(text.count("三模型对抗审查"), 1)  # 只出现在待办行
        self.assertIn("写发布说明", text)                  # 计划里没有的保留

    def test_no_open_plan_means_no_dedupe(self):
        text = plain(zylab.format_recap(record(
            summary="## pending\n- 跑完三模型对抗审查\n",
            plan={"items": [
                {"content": "跑完三模型对抗审查", "status": "completed"}]}),
            replay_limit=5))
        self.assertIn("三模型对抗审查", text)


class GoalNextStepTests(unittest.TestCase):
    def test_next_step_rides_the_goal_record_and_recap(self):
        goal = GOALS.new("完成扩盘发布跟踪", goal_id="goal-abc123")
        goal = GOALS.note(goal, evidence="审查 300/600",
                          next_step="~09:00 UTC 查审查完成与一致性结果")
        self.assertEqual(
            goal["next_step"], "~09:00 UTC 查审查完成与一致性结果")
        text = plain(zylab.format_recap(record(goal=goal), replay_limit=5))
        self.assertIn("Goal下一步", text)
        self.assertIn("09:00 UTC", text)

    def test_progress_without_next_step_keeps_the_previous_one(self):
        goal = GOALS.new("x", goal_id="goal-abc123")
        goal = GOALS.note(goal, evidence="a", next_step="查审查结果")
        goal = GOALS.note(goal, evidence="b")
        self.assertEqual(goal["next_step"], "查审查结果")

    def test_recap_stays_silent_when_there_is_no_next_step(self):
        goal = GOALS.new("x", goal_id="goal-abc123")
        text = plain(zylab.format_recap(record(goal=goal), replay_limit=5))
        self.assertNotIn("Goal下一步", text)


class ClipWidthTests(unittest.TestCase):
    def test_chinese_lines_are_clipped_by_display_columns(self):
        line = "跟" * 200
        clipped = zylab._recap_clip(line)
        self.assertLessEqual(
            tui.display_width(clipped), zylab._RECAP_LINE_CHARS)
        self.assertTrue(clipped.endswith("…"))
        # 按字符截会放进 95 个汉字 = 190 列，那正是要修的毛病
        self.assertLess(len(clipped), 95)

    def test_short_text_is_untouched(self):
        self.assertEqual(zylab._recap_clip("  短  行 "), "短 行")


class SummaryTemplateTests(unittest.TestCase):
    def test_prompt_numbers_turns_and_asks_for_anchors_and_rejections(self):
        from core import context as C                  # noqa: PLC0415

        messages = [{"role": "system", "content": "sys"}]
        for index in range(6):
            call_id = f"c{index}"
            messages.extend([
                {"role": "user", "content": f"任务 {index}"},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": "read_file",
                                 "arguments": '{"path": "/a"}'}}]},
                {"role": "tool", "tool_call_id": call_id, "content": "ok"},
            ])
        plan = C.plan_compaction(messages, None, keep_tail=1)
        prompt = C.summary_prompt(plan)
        self.assertIn("[t1 user]", prompt)
        self.assertIn("[t2 user]", prompt)
        self.assertIn("最新的写在最前面", prompt)
        self.assertIn("[rejected]", prompt)


if __name__ == "__main__":
    unittest.main()


class ObjectiveFallbackTests(unittest.TestCase):
    """回落到用户原话时不要把招呼语当目标。

    实测 297df6609726：它自己是从更早会话 resume 出来的，第一条 user 是 `hello`，
    recap 于是把「目标」写成了 hello。
    """

    def test_greetings_are_skipped(self):
        text = plain(zylab.format_recap(record(messages=(
            [{"role": "user", "content": "hello"},
             {"role": "user", "content": "把 252 个 marker 全部归一完"}]
            + [{"role": "assistant", "content": f"第 {i} 步"}
               for i in range(40)])), replay_limit=5))
        self.assertIn("252 个 marker", text)
        self.assertNotIn("hello", text)

    def test_an_existing_goal_owns_the_objective_line(self):
        goal = GOALS.new("跟踪扩盘发布", goal_id="goal-abc123")
        text = plain(zylab.format_recap(record(
            goal=goal,
            messages=([{"role": "user", "content": "hello"}]
                      + [{"role": "assistant", "content": f"第 {i} 步"}
                         for i in range(40)])), replay_limit=5))
        self.assertNotIn("hello", text)
        self.assertIn("跟踪扩盘发布", text)
        self.assertEqual(text.count("目标"), 0)      # 不再有冒充的目标行

    def test_all_greetings_means_no_objective_line(self):
        text = plain(zylab.format_recap(record(messages=(
            [{"role": "user", "content": "hi"}]
            + [{"role": "assistant", "content": f"第 {i} 步"}
               for i in range(40)])), replay_limit=5))
        self.assertEqual(text, "")


class ClipFillsTheBudgetTests(unittest.TestCase):
    def test_a_long_unbreakable_token_does_not_waste_the_line(self):
        # 按列换行取第一行会在 UUID 之前断开，白白浪费掉大半行
        text = ("持续跟踪 Claude Code 主会话（"
                "c88a42f8-58bb-4ef3-b969-c5d4f23cc91a.jsonl）的进度")
        clipped = zylab._recap_clip(text + "，" + "补" * 100)
        self.assertIn("c88a42f8", clipped)
        self.assertGreater(
            tui.display_width(clipped), zylab._RECAP_LINE_CHARS - 6)
