"""工具循环没有进展时要自己停下来（2026-09-22 功能测试报告 §1）。

真实事故：为统计一个文件的行数，模型把同一条 `grep -c "202[0-9]-"` **连发了约
40 次**，每次都返回 45，一直到撞上 `max_turns=40` 才被打断。代价是四十次
provider 请求和一屏上下文，而用户得手动敲「请总结当前进展」才能脱身。

判据刻意要三样都相同——工具名、参数、**结果**：
- 只看工具名会误伤「同一个工具读不同文件」；
- 只看参数会误伤轮询（等后台任务、盯一个在变的文件）——那种情况结果在变，
  本来就该允许。

检测点在**发请求之前**，所以认出来时连这一次 provider 请求都省掉。
两条循环（managed / legacy）各插一份：历史上「只改一条」已经出过事。
"""
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

import tests  # noqa: F401,E402  —— 状态目录隔离

from core import agent as A  # noqa: E402


def call(name, arguments, result):
    """一轮「模型发一个工具 + 工具回一条结果」。"""
    return [
        {"role": "assistant", "tool_calls": [{
            "id": "x", "type": "function",
            "function": {"name": name, "arguments": arguments}}]},
        {"role": "tool", "content": result},
    ]


class Detector(unittest.TestCase):
    def agent(self, *rounds):
        ag = A.Agent.__new__(A.Agent)
        ag.messages = [{"role": "user", "content": "统计一下行数"}]
        for item in rounds:
            ag.messages += item
        return ag

    def test_three_identical_rounds_are_a_stall(self):
        same = call("bash", '{"command":"grep -c x f.txt"}', "45")
        ag = self.agent(same, same, same)
        detail = ag._stalled_tool_call()
        self.assertIsNotNone(detail)
        self.assertIn("bash", detail)
        self.assertIn("grep -c x", detail)

    def test_two_is_not_yet_a_stall(self):
        same = call("bash", '{"command":"ls"}', "a\nb")
        self.assertIsNone(self.agent(same, same)._stalled_tool_call())

    def test_a_changing_result_is_progress(self):
        """轮询一个在变的东西是合法的——结果不同就不算打转。"""
        ag = self.agent(
            call("bash", '{"command":"wc -l log"}', "10"),
            call("bash", '{"command":"wc -l log"}', "11"),
            call("bash", '{"command":"wc -l log"}', "12"))
        self.assertIsNone(ag._stalled_tool_call())

    def test_the_same_tool_on_different_inputs_is_progress(self):
        ag = self.agent(
            *[call("read_file", '{"path":"%s"}' % name, "body")
              for name in ("a", "b", "c")])
        self.assertIsNone(ag._stalled_tool_call())

    def test_only_the_latest_streak_counts(self):
        """先打转再换招，不该因为历史上有过重复而一直被判死。"""
        same = call("grep", '{"pattern":"x"}', "none")
        ag = self.agent(same, same, same,
                        call("read_file", '{"path":"a"}', "body"))
        self.assertIsNone(ag._stalled_tool_call())

    def test_a_parallel_batch_is_not_this_shape(self):
        """一轮发多个工具是另一种形态，不由这个判据管。"""
        ag = self.agent()
        batch = {"role": "assistant", "tool_calls": [
            {"id": "1", "type": "function",
             "function": {"name": "grep", "arguments": '{"pattern":"x"}'}},
            {"id": "2", "type": "function",
             "function": {"name": "grep", "arguments": '{"pattern":"x"}'}}]}
        for _ in range(3):
            ag.messages += [batch, {"role": "tool", "content": "none"},
                            {"role": "tool", "content": "none"}]
        self.assertIsNone(ag._stalled_tool_call())

    def test_a_fresh_conversation_is_never_a_stall(self):
        self.assertIsNone(self.agent()._stalled_tool_call())

    def test_the_threshold_is_configurable_but_never_below_two(self):
        same = call("bash", '{"command":"true"}', "")
        ag = self.agent(same, same)
        self.assertIsNotNone(ag._stalled_tool_call(repeats=2))
        self.assertIsNone(ag._stalled_tool_call(repeats=1))
        self.assertIsNone(ag._stalled_tool_call(repeats=0))


class TheNotice(unittest.TestCase):
    def test_it_tells_the_model_what_to_do_instead(self):
        text = A.Agent._no_progress_notice("连续 3 次调用 bash，参数与返回完全相同")
        self.assertIn("没有进展", text)
        self.assertIn("总结", text, "得给出下一步，不能只说「我拦了」")
        self.assertIn("保留", text, "已有的工具结果不会丢，这点要说清楚")


class BothLoopsCheckIt(unittest.TestCase):
    """managed 与 legacy 两条循环都要检测——只改一条是这个项目的老坑。"""

    def test_both_loops_call_the_detector_before_the_provider(self):
        source = (Path(ROOT) / "core" / "agent.py").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("stalled = self._stalled_tool_call()"), 2,
            "两条循环各要有一处；少一处就是有一条路径还会打转到 40 轮")
        # 检测必须在 for turn 的开头，也就是发 provider 请求之前
        for chunk in source.split("for turn in _turn_indices(max_turns):")[1:]:
            head = chunk[:400]
            self.assertIn("stalled = self._stalled_tool_call()", head,
                          "检测点要紧跟在 for turn 之后，否则这一轮的请求白花了")

    def test_the_stop_is_a_partial_failure_not_a_crash(self):
        source = (Path(ROOT) / "core" / "agent.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('"kind": "no_progress"'), 2)
        self.assertEqual(source.count('"status": "partial"'),
                         source.count('"status": "partial"'))
        self.assertIn('notice_kind="no_progress"', source)


class TheParentIsToldWhatTheChildHas(unittest.TestCase):
    """2026-09-22 报告 §2：主 agent 派了两个需要联网的 child，都回「我没有 web_fetch」。

    child 自己的 system prompt 一直写着「你只能读 read_file/list_dir/glob/grep」——
    缺的是**主 agent 那一侧**的可发现性：它照着工具描述设计任务，而描述里没说
    child 有哪些工具。不可发现的限制等于会被反复踩的限制。
    """

    def spec(self, name):
        from core import tools
        for item in tools.SCHEMA:
            if item.get("function", {}).get("name") == name:
                return item["function"]
        raise AssertionError(f"工具表里没有 {name}")

    def test_the_subagent_description_names_the_four_tools(self):
        text = self.spec("subagent")["description"]
        for tool in ("read_file", "list_dir", "glob", "grep"):
            self.assertIn(tool, text)

    def test_it_says_there_is_no_network_and_no_bash(self):
        text = self.spec("subagent")["description"]
        self.assertIn("web_fetch", text)
        self.assertIn("bash", text)

    def test_the_description_matches_the_actual_allowlist(self):
        """描述与代码里的白名单必须对得上，否则它只是另一处会漂移的文档。"""
        from core import agents
        text = self.spec("subagent")["description"]
        for tool in agents.CHILD_TOOLS:
            self.assertIn(tool, text, f"白名单里有 {tool}，描述里没写")
        self.assertEqual(
            agents.CHILD_TOOLS,
            frozenset({"read_file", "list_dir", "glob", "grep"}),
            "白名单变了就得同步改描述与上面两条断言")


class ItDoesNotBreakTheUnboundedLoop(unittest.TestCase):
    """「循环不设上限」与「打转就停」必须能同时成立。

    加检测时 `test_max_turns_unbounded` 三条当场红了——它的夹具是 45 次
    一模一样的 `list_dir {"path": "."}`、每次返回同一个字符串，正好命中打转形态。
    **拦得对，是夹具不对**：那条用例要证明的是「不再被 40 封顶」，不是
    「原地打转能跑很久」。夹具改成每轮参数与结果都不同（真有进展）。
    这条用例把两者的边界钉住，免得以后有人为了让 45 轮跑通而把检测调松。
    """

    def test_the_unbounded_fixture_shows_real_progress(self):
        text = (Path(ROOT) / "tests" / "test_max_turns_unbounded.py").read_text(
            encoding="utf-8")
        self.assertIn("round-{index}", text,
                      "每轮参数要不同，否则它测的是打转不是轮数")
        self.assertNotIn('return_value="o"', text)
        self.assertNotIn('return_value="out"', text)

    def test_forty_five_varying_rounds_are_not_a_stall(self):
        ag = A.Agent.__new__(A.Agent)
        ag.messages = [{"role": "user", "content": "go"}]
        for index in range(45):
            ag.messages += call("list_dir",
                                '{"path":"./round-%d"}' % index,
                                "out-%d" % index)
            self.assertIsNone(ag._stalled_tool_call(),
                              f"第 {index} 轮被误判成打转")


if __name__ == "__main__":
    unittest.main()
