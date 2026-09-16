"""ES0a：触发评测 harness 自身的测试。

**harness 先要自己可信，才能拿去判别人。** 这里全部离线：不发 provider 请求，
不写真实 `~/.zylab/`。真机评测在 `scripts/run_skill_eval.py`，显式运行。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)          # 兼容 `python3 -m unittest tests.x` 与 discover
import skill_eval                                            # noqa: E402
from core import agent as A, client, controller as C, skills   # noqa: E402
from test_agent_loop import mk, scripted                     # noqa: E402


class PathToSkillName(unittest.TestCase):
    """判据必须和 zylab.py 的 ⚑ 标记一致，否则两套口径会打架。"""

    def test_recognizes_a_skill_file(self):
        self.assertEqual(
            skill_eval.skill_name_from_path("skills/test-ratchet/SKILL.md"),
            "test-ratchet")

    def test_absolute_path(self):
        self.assertEqual(
            skill_eval.skill_name_from_path("/a/b/skills/vendor-docs/SKILL.md"),
            "vendor-docs")

    def test_ordinary_file_is_not_a_skill(self):
        self.assertEqual(skill_eval.skill_name_from_path("core/agent.py"), "")

    def test_bare_skill_md_has_no_name(self):
        """没有父目录就说不出是哪条，宁可不认。"""
        self.assertEqual(skill_eval.skill_name_from_path("SKILL.md"), "")

    def test_empty_and_none(self):
        self.assertEqual(skill_eval.skill_name_from_path(""), "")
        self.assertEqual(skill_eval.skill_name_from_path(None), "")

    def test_matches_the_terminal_marker(self):
        import zylab
        path = "skills/quantify-first/SKILL.md"
        marked = zylab.preview("read_file", {"path": path})
        self.assertIn(skill_eval.skill_name_from_path(path), marked)


class JournalShapeNormalisation(unittest.TestCase):
    """run() 与 _run_managed 的 tool_started payload 形状不同，两种都要认。"""

    def _event(self, payload):
        return {"kind": "tool_started", "payload": payload}

    def test_run_loop_shape(self):
        events = [self._event({
            "tool_call_id": "1", "name": "read_file",
            "args": {"path": "skills/tdd/SKILL.md"}})]
        self.assertEqual(skill_eval.skills_read(events), ["tdd"])

    def test_managed_loop_shape(self):
        events = [self._event({"tool_call": {
            "id": "1", "type": "function",
            "function": {"name": "read_file",
                         "arguments": json.dumps(
                             {"path": "skills/tdd/SKILL.md"})}}})]
        self.assertEqual(skill_eval.skills_read(events), ["tdd"])

    def test_managed_shape_with_dict_arguments(self):
        events = [self._event({"tool_call": {
            "function": {"name": "read_file",
                         "arguments": {"path": "skills/tdd/SKILL.md"}}}})]
        self.assertEqual(skill_eval.skills_read(events), ["tdd"])

    def test_malformed_arguments_are_skipped_not_fatal(self):
        events = [self._event({"tool_call": {
            "function": {"name": "read_file", "arguments": "{not json"}}})]
        self.assertEqual(skill_eval.skills_read(events), [])

    def test_other_tools_ignored(self):
        events = [self._event({"name": "grep", "args": {"pattern": "SKILL.md"}}),
                  self._event({"name": "bash",
                               "args": {"command": "cat skills/x/SKILL.md"}})]
        self.assertEqual(skill_eval.skills_read(events), [],
                         "只有 read_file 算读取；grep/bash 命中不算")

    def test_other_event_kinds_ignored(self):
        self.assertEqual(skill_eval.skills_read(
            [{"kind": "tool_finished",
              "payload": {"name": "read_file",
                          "args": {"path": "skills/tdd/SKILL.md"}}}]), [])

    def test_order_preserved_and_deduped(self):
        events = [self._event({"name": "read_file",
                               "args": {"path": f"skills/{n}/SKILL.md"}})
                  for n in ("b", "a", "b")]
        self.assertEqual(skill_eval.skills_read(events), ["b", "a"])

    def test_empty_input(self):
        self.assertEqual(skill_eval.skills_read([]), [])
        self.assertEqual(skill_eval.skills_read(None), [])


class Grading(unittest.TestCase):
    """正向与负向必须分开报告，不合成单一分数。"""

    def test_positive_hit(self):
        got = skill_eval.grade(
            {"name": "c", "expect_any": ["tdd"], "expect_none": []}, ["tdd"])
        self.assertTrue(got["positive_ok"])
        self.assertEqual(got["hit"], ["tdd"])

    def test_positive_miss(self):
        got = skill_eval.grade(
            {"name": "c", "expect_any": ["tdd"], "expect_none": []}, [])
        self.assertFalse(got["positive_ok"])

    def test_false_positive_is_reported_separately(self):
        """该触发的触发了，不该触发的也触发了 —— 不能互相抵消。"""
        got = skill_eval.grade(
            {"name": "c", "expect_any": ["tdd"], "expect_none": ["code-review"]},
            ["tdd", "code-review"])
        self.assertTrue(got["positive_ok"])
        self.assertFalse(got["negative_ok"])
        self.assertEqual(got["false_positive"], ["code-review"])

    def test_pure_negative_case_has_no_positive_verdict(self):
        got = skill_eval.grade(
            {"name": "c", "expect_any": [], "expect_none": ["tdd"]}, [])
        self.assertIsNone(got["positive_ok"], "负向用例不该被算进正向分母")
        self.assertTrue(got["negative_ok"])

    def test_summary_counts_two_axes(self):
        results = [
            skill_eval.grade({"name": "a", "expect_any": ["x"]}, ["x"]),
            skill_eval.grade({"name": "b", "expect_any": ["y"]}, []),
            skill_eval.grade({"name": "c", "expect_none": ["z"]}, ["z"]),
        ]
        summary = skill_eval.summarize(results)
        self.assertEqual(summary["positive_ok"], 1)
        self.assertEqual(summary["positive_total"], 2)
        self.assertEqual(summary["negative_ok"], 2)
        self.assertEqual(summary["negative_total"], 3)


class CaseFixture(unittest.TestCase):
    """用例文件本身要跟着 catalog 走，改名/删除 skill 时立刻暴露。"""

    @classmethod
    def setUpClass(cls):
        cls.cases = skill_eval.load_cases()
        cls.catalog = set(skills.load(cwd=str(skill_eval.REPO)).skills)

    def test_has_both_axes(self):
        self.assertTrue([c for c in self.cases if c.get("expect_any")])
        self.assertTrue([c for c in self.cases if c.get("expect_none")],
                        "只测正向会漏掉过度触发")

    def test_names_unique(self):
        names = [c["name"] for c in self.cases]
        self.assertEqual(len(names), len(set(names)))

    def test_every_case_explains_itself(self):
        for case in self.cases:
            self.assertTrue(case.get("why"), f"{case['name']} 缺少 why")

    def test_expected_skills_exist_or_are_planned(self):
        """未来才加的 skill 允许出现，但要在 PLAN 里有名字。"""
        planned = {"tdd", "codebase-design", "diagnosing-bugs",
                   "domain-modeling", "trace-change-impact"}
        for case in self.cases:
            for name in list(case.get("expect_any") or ()) + \
                    list(case.get("expect_none") or ()):
                self.assertIn(name, self.catalog | planned,
                              f"{case['name']} 引用了不存在也未规划的 {name}")

    def test_no_prompt_forbids_reading_files(self):
        """任何用例都不许写「不要读文件」—— 负向用例尤其不许。

        同一天两次翻车：
        1. 正向用例写了它，SKILL.md 被一起禁掉，测出来的是提示词不是路由。
        2. 加了守卫，但守卫**只检查有 expect_any 的用例** —— 负向用例被豁免，
           而 negative-plain-knowledge 恰恰写了它。那条用例保证读不到任何
           skill，于是平凡通过，什么都证明不了。**守卫比被测对象窄。**

        负向用例的价值全在「模型有机会读、但正确地没读」。堵死这个机会，
        它就退化成一句同义反复。
        """
        for case in self.cases:
            for phrase in ("不要读文件", "不许读文件", "别读文件"):
                self.assertNotIn(phrase, case["prompt"],
                                 f"{case['name']} 会把 SKILL.md 一起禁掉")


class EndToEndOffline(unittest.TestCase):
    """用脚本化 provider 跑通两条循环，证明 harness 接得上真实事件流。"""

    def _read_call(self):
        return ("read_file", {"path": "skills/vendor-docs/SKILL.md"})

    def test_run_loop_end_to_end(self):
        ag = mk()
        captured = []
        with mock.patch.object(client, "stream_chat",
                               scripted(("", [self._read_call()]), ("done", []))), \
             mock.patch.object(ag, "log_usage"), \
             tempfile.TemporaryDirectory() as tmp:
            with mock.patch("core.tools.run", return_value="body"):
                list(ag.run("go", event_sink=lambda evs: captured.extend(evs) or evs))
        self.assertEqual(skill_eval.skills_read(captured), ["vendor-docs"])

    def test_managed_loop_end_to_end(self):
        ag = mk()
        journal = C.MemoryJournal()
        ctrl = C.SessionController("t", journal)
        ctrl.submit("go", C.QueueMode.NEXT_TURN)
        with mock.patch.object(client, "stream_chat",
                               scripted(("", [self._read_call()]), ("done", []))), \
             mock.patch.object(ag, "log_usage"), \
             mock.patch("core.tools.run", return_value="body"):
            list(ag.run("go", controller=ctrl))
        self.assertEqual(skill_eval.skills_read(journal.events), ["vendor-docs"],
                         "managed 路径的 payload 形状没被认出来")


class NoRealSideEffects(unittest.TestCase):
    """ES0 退出条件：不写真实 HOME，不发真实请求。"""

    def test_harness_module_makes_no_requests(self):
        source = (skill_eval.REPO / "tests" / "skill_eval.py").read_text(
            encoding="utf-8")
        for needle in ("urllib", "requests", "stream_chat", "api_key"):
            self.assertNotIn(needle, source,
                             f"harness 不该碰 {needle} —— 真机评测在 scripts/")

    def test_harness_does_not_touch_user_state(self):
        source = (skill_eval.REPO / "tests" / "skill_eval.py").read_text(
            encoding="utf-8")
        self.assertNotIn(".zylab", source)


if __name__ == "__main__":
    unittest.main()


class FailedRunIsNotAMiss(unittest.TestCase):
    """跑失败必须与「未触发」区分开。

    2026-08-31 第一次真机冒烟撞上网关 503：harness 如实报「读到 —」，输出成
    「正向 0/1」—— 与真正的路由失败完全无法区分。一次网关抖动就能伪造出一条
    「路由变差了」的结论。
    """

    def test_turn_failed_is_detected(self):
        self.assertTrue(skill_eval.turn_failed([
            {"kind": "request_started", "payload": {}},
            {"kind": "turn_failed", "payload": {"error": "HTTP 503"}}]))

    def test_healthy_turn_is_not_flagged(self):
        self.assertFalse(skill_eval.turn_failed([
            {"kind": "turn_started", "payload": {}},
            {"kind": "turn_completed", "payload": {}}]))

    def test_empty_events_are_not_flagged(self):
        self.assertFalse(skill_eval.turn_failed([]))
        self.assertFalse(skill_eval.turn_failed(None))

    def test_grade_defaults_to_no_error(self):
        self.assertIsNone(
            skill_eval.grade({"name": "a", "expect_any": ["x"]}, ["x"])["error"])


class ObservedVersusAbsent(unittest.TestCase):
    """docs/pipeline-lessons.md 的 F1「断言不存在」——本项目累计翻车 8 次以上。

    观察到的事件永远有效；「没观察到」只有这一轮跑完才有意义。
    """

    def test_hit_survives_a_failed_turn(self):
        """2026-08-31 实测形态：模型读了 vendor-docs，之后网关 503。

        那次读取是**发生过的事实**，不能因为后续失败被作废。
        """
        got = skill_eval.grade({"name": "c", "expect_any": ["vendor-docs"]},
                               ["vendor-docs"], completed=False)
        self.assertTrue(got["positive_ok"])
        self.assertTrue(got["positive_valid"], "已观察到的命中不该被作废")

    def test_miss_is_indeterminate_when_the_turn_died(self):
        got = skill_eval.grade({"name": "c", "expect_any": ["vendor-docs"]},
                               [], completed=False)
        self.assertFalse(got["positive_valid"],
                         "没跑完时「没读到」可能只是没轮到")

    def test_false_positive_survives_a_failed_turn(self):
        got = skill_eval.grade({"name": "c", "expect_none": ["code-review"]},
                               ["code-review"], completed=False)
        self.assertFalse(got["negative_ok"])
        self.assertTrue(got["negative_valid"], "过度触发是观察结果，永远算数")

    def test_clean_negative_is_indeterminate_when_the_turn_died(self):
        got = skill_eval.grade({"name": "c", "expect_none": ["code-review"]},
                               [], completed=False)
        self.assertTrue(got["negative_ok"])
        self.assertFalse(got["negative_valid"],
                         "turn 早死时「什么都没触发」是平凡真，不能算通过")

    def test_summary_excludes_only_the_invalid_axis(self):
        hit_then_died = skill_eval.grade(
            {"name": "a", "expect_any": ["x"], "expect_none": ["z"]},
            ["x"], completed=False)
        summary = skill_eval.summarize([hit_then_died])
        self.assertEqual(summary["positive_total"], 1, "命中轴仍可判定")
        self.assertEqual(summary["positive_ok"], 1)
        self.assertEqual(summary["negative_total"], 0, "负向轴不可判定")
        self.assertEqual(summary["indeterminate"], 1)

    def test_completed_run_keeps_both_axes(self):
        got = skill_eval.grade(
            {"name": "a", "expect_any": ["x"], "expect_none": ["z"]}, ["x"])
        self.assertTrue(got["positive_valid"])
        self.assertTrue(got["negative_valid"])
