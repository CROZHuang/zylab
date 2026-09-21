"""发给模型的历史要守三条：模型自己说过的话一个字不改；刚拿到的结果原样可见；收起来的找得回来。

依据是 2026-09-20 在 7 个真实会话（2,925 次请求、2,651 个工具结果）上的重放与统计：

- 旧轮次的长**参数**被收成「头 200 字 + 占位符」只省 4.3% 的输入 token，却是 5 起模仿事故的全部
  来源——占位符出现在模型自己的口吻里，它就学着写。结果占位符（环境的口吻）525 处，零模仿。
- 「新鲜大结果」档在 12,000 字符就把刚读出来的内容切成头 4,000 + 尾 2,000：90 次 read_file 有 23
  次模型只看得到 6K；其中 10 次在捕获时就被从**文件中间**挖掉一段。
- 占位符说「原文仍保存在会话事件中」，而 expand_output 要的 id 占位符里没有：调用次数 0。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests  # noqa: F401,E402  —— 状态目录隔离

from core import context as C                         # noqa: E402
from core import tools                                # noqa: E402


def turn(index, *, tool="bash", args=None, result="ok"):
    call_id = f"call-{index}"
    return [
        {"role": "user", "content": f"任务 {index}"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": tool, "arguments": json.dumps(
                args or {"command": f"echo {index}"}, ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def history(*turns):
    messages = [{"role": "system", "content": "sys"}]
    for item in turns:
        messages.extend(item)
    return messages


def project(messages, **kwargs):
    params = {"age_after_turns": 3, "age_min_chars": 400, "age_keep_head": 200}
    params.update(kwargs)
    return C.materialize(messages, summary=None, tools_schema=[],
                         model_limit=1_000_000, **params)


def numbered(count):
    """每行都不一样、且认得出原始行号的正文——验「中间没被挖掉」要靠它。"""
    return "\n".join(f"line {index:05d} " + "x" * 30 for index in range(count))


class ArgumentsAreNeverRewrittenTests(unittest.TestCase):
    def setUp(self):
        self.body = "print('第一版')\n" * 600                      # 约 9,600 字符
        self.messages = history(
            turn(0, tool="write_file",
                 args={"path": "/repo/big.py", "content": self.body}),
            *[turn(index) for index in range(1, 10)])

    def test_old_large_arguments_are_replayed_byte_for_byte(self):
        raw = self.messages[2]["tool_calls"][0]["function"]["arguments"]
        projection = project(self.messages)
        sent = [m for m in projection.messages if m.get("tool_calls")
                and m["tool_calls"][0]["id"] == "call-0"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["tool_calls"][0]["function"]["arguments"], raw,
                         "九轮之前的 write_file 参数必须逐字回放")

    def test_no_placeholder_ever_appears_in_the_assistants_own_voice(self):
        projection = project(self.messages)
        for message in projection.messages:
            if message.get("role") != "assistant":
                continue
            blob = json.dumps(message, ensure_ascii=False)
            self.assertNotRegex(blob, C.PROJECTION_MARKER)

    def test_the_savings_report_no_longer_counts_arguments(self):
        """这段历史里唯一够大的东西就是那份参数；结果都是两个字的 ok。"""
        previews = project(self.messages).report["tool_previews"]
        self.assertEqual((previews["count"], previews["saved_chars"]), (0, 0))
        self.assertEqual(previews["largest_artifacts"], [])


class FreshResultsAreShownAsCapturedTests(unittest.TestCase):
    def test_a_fresh_result_at_the_capture_cap_is_verbatim(self):
        captured = tools._clip(numbered(5_000))                    # 工具层截过一次之后的样子
        self.assertGreater(len(captured), 29_000)
        messages = history(turn(0), turn(1, tool="read_file",
                                         args={"path": "/repo/a.py"}, result=captured))
        sent = [m for m in project(messages).messages
                if m.get("role") == "tool" and m.get("tool_call_id") == "call-1"]
        self.assertEqual(sent[0]["content"], captured)

    def test_the_backstop_is_never_stricter_than_the_capture_cap(self):
        """投影的「新鲜大结果」档只给忘了截的工具兜底；比捕获上限严 = 模型看不全自己刚要的东西。"""
        worst = tools._clip("x" * 500_000)
        self.assertLess(len(worst) + 500, C.RECENT_LARGE_TOOL_CHARS)
        self.assertGreaterEqual(C.RECENT_KEEP_HEAD + C.RECENT_KEEP_TAIL, tools.MAX_OUT)

    def test_a_tool_that_forgot_to_clip_is_still_bounded(self):
        messages = history(turn(0, result="y" * 200_000))
        sent = [m for m in project(messages).messages if m.get("role") == "tool"]
        self.assertLess(len(sent[0]["content"]), 32_000)
        self.assertIn("tier=recent-large", sent[0]["content"])


class AgedStubPointerTests(unittest.TestCase):
    def setUp(self):
        self.original = numbered(300)                             # 约 12,000 字符
        self.messages = history(
            turn(0, result=self.original), *[turn(index) for index in range(1, 8)])
        sent = [m for m in project(self.messages).messages
                if m.get("role") == "tool" and m.get("tool_call_id") == "call-0"]
        self.stub = sent[0]["content"]

    def session(self, view=None, error=None):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session.ag = mock.Mock()
        session.ag.messages = self.messages
        session.task_manager = mock.Mock()
        if error is not None:
            session.task_manager.artifact_page.side_effect = error
        else:
            session.task_manager.artifact_page.return_value = view
        return session

    def target(self):
        match = re.search(r'expand_output\(target="(sha256:[0-9a-f]{16})"\)', self.stub)
        self.assertIsNotNone(match, "占位符里必须有一个照抄就能用的 target：" + self.stub[-200:])
        return match.group(1)

    def test_the_stub_really_did_hide_the_middle(self):
        self.assertIn("tier=aged", self.stub)
        self.assertNotIn("line 00150", self.stub)

    def test_the_target_in_the_stub_finds_the_raw_message(self):
        found = C.find_tool_result(self.messages, self.target())
        self.assertIs(found, self.messages[3])
        self.assertIsNone(C.find_tool_result(self.messages, "call-0"))
        self.assertIsNone(C.find_tool_result(self.messages, "sha256:" + "0" * 16))

    def test_the_model_gets_the_original_back_end_to_end(self):
        session = self.session(view=None)
        with mock.patch.dict(tools.HOOK_CTX,
                             {"expand_output": session.expand_output_for_tool}):
            out = tools.t_expand_output(target=self.target())
        header, _, body = out.partition("\n")
        self.assertIn("已到末尾", header)
        self.assertEqual(body, self.original, "取回的必须是原文本身，不是转义过的 JSON")

    def test_long_originals_are_paged_and_the_header_says_where_to_continue(self):
        self.original = numbered(3_000)                           # 约 120,000 字符
        self.messages = history(
            turn(0, result=self.original), *[turn(index) for index in range(1, 8)])
        target = "sha256:" + C.tool_result_digest(self.original)
        session = self.session(view=None)
        pages = []
        with mock.patch.dict(tools.HOOK_CTX,
                             {"expand_output": session.expand_output_for_tool}):
            for page in range(1, 10):
                out = tools.t_expand_output(target=target, page=page)
                header, _, body = out.partition("\n")
                pages.append(body)
                if "已到末尾" in header:
                    break
                self.assertIn(f"page={page + 1}", header)
            beyond = tools.t_expand_output(target=target, page=page + 1)
        self.assertEqual("".join(pages), self.original)
        self.assertGreater(len(pages), 2)
        self.assertIn("error", json.loads(beyond))

    def test_output_saved_on_disk_wins_over_the_clipped_copy_in_the_transcript(self):
        """bash 的完整输出落了盘；会话记录里那份是捕获时截过的。认出是哪次调用后优先读盘。"""
        view = {"page": 1, "total_bytes": 9, "has_more": False, "missing": [],
                "sections": [{"stream": "stdout", "text": "full-disk"}]}
        session = self.session(view=view)
        result = session.expand_output_for_tool({"target": self.target(), "page": 1})
        session.task_manager.artifact_page.assert_called_once_with("call-0", page=1)
        self.assertEqual(result["text"], "full-disk")

    def test_a_broken_artifact_index_falls_back_to_the_transcript(self):
        from core import tasks                        # noqa: PLC0415

        session = self.session(error=tasks.TaskError("index 损坏"))
        result = session.expand_output_for_tool({"target": self.target(), "page": 1})
        self.assertEqual(result["text"], self.original)

    def test_an_unknown_target_says_what_a_valid_one_looks_like(self):
        session = self.session(view=None)
        result = session.expand_output_for_tool({"target": "nope"})
        for must in ("sha256:", "task_id", "tool_call_id"):
            self.assertIn(must, result["error"])


class ReadFilePaginationTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="zylab-read-")
        self.ctx = tools.ExecutionContext.capture(session="s", workspace_root=self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read(self, path, **kwargs):
        return tools.t_read_file(path, _execution_context=self.ctx, **kwargs)

    @staticmethod
    def line_numbers(out):
        return [int(row.split("\t", 1)[0]) for row in out.splitlines()
                if re.match(r"\s*\d+\t", row)]

    def test_a_big_file_is_cut_at_a_line_boundary_never_in_the_middle(self):
        path = self.write("big.py", numbered(3_000) + "\n")
        out = self.read(path)
        shown = self.line_numbers(out)
        self.assertNotIn("截断", out)
        self.assertEqual(shown, list(range(1, len(shown) + 1)), "行必须连续，中间不能有洞")
        self.assertLess(len(shown), 2_000)
        self.assertLessEqual(len(out), tools.MAX_OUT + 200)
        self.assertRegex(out, rf"后面还有 {3_000 - len(shown)} 行，接着读：offset={len(shown)}\]$")
        self.assertIn("上限", out)

    def test_following_the_offset_reads_the_whole_file_with_no_gap_or_overlap(self):
        path = self.write("big.py", numbered(3_000) + "\n")
        seen, offset = [], 0
        for _ in range(20):
            out = self.read(path, offset=offset)
            seen.extend(self.line_numbers(out))
            match = re.search(r"接着读：offset=(\d+)\]$", out)
            if not match:
                break
            offset = int(match.group(1))
        self.assertEqual(seen, list(range(1, 3_001)))

    def test_a_small_file_has_no_footer(self):
        out = self.read(self.write("small.py", "a\nb\nc\n"))
        self.assertEqual(self.line_numbers(out), [1, 2, 3])
        self.assertNotIn("[共", out)

    def test_an_explicit_window_says_where_to_continue_without_blaming_the_cap(self):
        out = self.read(self.write("mid.py", numbered(100) + "\n"), limit=10)
        self.assertTrue(out.endswith("[共 100 行，显示 1-10；后面还有 90 行，接着读：offset=10]"), out[-80:])

    def test_the_last_window_just_says_what_it_showed(self):
        out = self.read(self.write("mid.py", numbered(100) + "\n"), offset=90)
        self.assertTrue(out.endswith("[共 100 行，显示 91-100]"), out[-60:])

    def test_a_single_enormous_line_is_clipped_rather_than_dropped(self):
        out = self.read(self.write("min.js", "v" * 100_000 + "\n"))
        self.assertIn("截断", out)
        self.assertLess(len(out), tools.MAX_OUT + 200)


if __name__ == "__main__":
    unittest.main()
