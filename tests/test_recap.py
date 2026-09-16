"""Resume 自动 recap：只在回放之外有增量信号时出现，否则一个字不打。

维护者要的是 Claude 式「接上会话时自动给一段进展提要」。数据全部来自
已落盘的会话记录（第一条 user 原话、task_plan、结构化摘要），零 API 调用。
最重要的断言是**静默条件**：短会话、无计划、无摘要时回放本身已是完整
画面，recap 不得渲染 —— 否则每次 resume 都多一坨噪音，功能会被用户关掉。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab as CLI


def msgs(n_pairs, first_user="最初的目标是修好解析器"):
    out = [{"role": "system", "content": "s"},
           {"role": "user", "content": first_user},
           {"role": "assistant", "content": "ok"}]
    for i in range(n_pairs - 1):
        out.append({"role": "user", "content": f"追问 {i}"})
        out.append({"role": "assistant", "content": f"回答 {i}"})
    return out


class Silence(unittest.TestCase):
    def test_short_session_without_signals_is_silent(self):
        record = {"messages": msgs(3), "task_plan": None, "context": None}
        self.assertEqual(CLI.format_recap(record, replay_limit=20), "")

    def test_all_completed_plan_alone_is_silent(self):
        record = {
            "messages": msgs(2),
            "task_plan": {"revision": 1, "explanation": "", "items": [
                {"content": "都做完了", "status": "completed"}]},
        }
        self.assertEqual(CLI.format_recap(record, replay_limit=20), "")

    def test_garbage_record_is_silent_not_crashing(self):
        self.assertEqual(CLI.format_recap(None), "")
        self.assertEqual(CLI.format_recap({"task_plan": ["坏形状"]}), "")


class HeadCutObjective(unittest.TestCase):
    def test_long_session_recovers_first_user_ask(self):
        """回放只剩尾部时，recap 必须补上最初目标 —— 这是它存在的首要理由。"""
        record = {"messages": msgs(30)}
        block = CLI.format_recap(record, replay_limit=20)
        self.assertIn("目标", block)
        self.assertIn("最初的目标是修好解析器", block)

    def test_short_session_does_not_repeat_visible_head(self):
        record = {"messages": msgs(4)}
        self.assertEqual(CLI.format_recap(record, replay_limit=20), "")


class PlanProgress(unittest.TestCase):
    def test_in_progress_plan_renders_counts_and_current(self):
        record = {
            "messages": msgs(2),
            "task_plan": {"revision": 3, "explanation": "", "items": [
                {"content": "摸清现状", "status": "completed"},
                {"content": "实现并跑测试", "status": "in_progress"},
                {"content": "补回归", "status": "pending"}]},
        }
        block = CLI.format_recap(record, replay_limit=20)
        self.assertIn("待办", block)
        self.assertIn("1/3", block)
        self.assertIn("◩", block)
        self.assertIn("实现并跑测试", block)


class SummarySections(unittest.TestCase):
    CONTENT = ("## objective\n修好 SSE 解析\n"
               "## constraints\nunknown\n"
               "## decisions\n- 保持零依赖\n- 放弃探测\n- 第三条不该出现\n"
               "## pending\n- 补 managed 测试\n")

    def record(self):
        return {"messages": msgs(2),
                "context": {"summary": {"status": "valid",
                                        "content": self.CONTENT}}}

    def test_sections_render_and_unknown_is_filtered(self):
        block = CLI.format_recap(self.record(), replay_limit=20)
        self.assertIn("修好 SSE 解析", block)
        self.assertIn("保持零依赖", block)
        self.assertIn("补 managed 测试", block)
        self.assertNotIn("unknown", block)
        self.assertNotIn("第三条不该出现", block, "decisions 应只取前 2 条")

    def test_summary_objective_beats_first_message(self):
        record = self.record()
        record["messages"] = msgs(30, first_user="旧的第一句")
        block = CLI.format_recap(record, replay_limit=20)
        self.assertIn("修好 SSE 解析", block)
        self.assertNotIn("旧的第一句", block)

    def test_invalid_summary_is_ignored(self):
        record = self.record()
        record["context"]["summary"]["status"] = "failed"
        self.assertEqual(CLI.format_recap(record, replay_limit=20), "")


class ShowRecapWiring(unittest.TestCase):
    class FakeRenderer:
        def __init__(self):
            self.blocks = []

        def write_output(self, block):
            self.blocks.append(block)

    class FakeSess:
        def __init__(self, renderer):
            self.renderer = renderer
            self.cfg = {"resume_replay": 20}

    def test_renders_through_renderer_when_signal_exists(self):
        r = self.FakeRenderer()
        sess = self.FakeSess(r)
        ok = CLI.show_recap(sess, {"messages": msgs(30)})
        self.assertTrue(ok)
        self.assertEqual(len(r.blocks), 1)
        self.assertIn("Recap", r.blocks[0])

    def test_silent_when_no_signal(self):
        r = self.FakeRenderer()
        sess = self.FakeSess(r)
        self.assertFalse(CLI.show_recap(sess, {"messages": msgs(2)}))
        self.assertEqual(r.blocks, [])


if __name__ == "__main__":
    unittest.main()
