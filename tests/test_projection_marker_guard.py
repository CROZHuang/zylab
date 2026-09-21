"""模型会模仿上下文投影的占位符——工具层必须拦住。

占位符出现在模型**自己过去的 tool_call 参数**里（`head…[参数投影 field=… omitted_chars=…]`），
等于用它自己的口吻示范「长参数可以这样收尾」。2026-09-20 在真实会话里查到 5 起（735 次长参数
调用的 0.68%，全部是 glm-5.3）：4 条 bash 命令写到一半吐出**走样的**假占位符，引号/heredoc
不闭合，全部 exit 2；1 次 write_file 把一份反馈书的后 2/3 写成了假占位符，模型事后还汇报
「五条差距齐了」，磁盘上只有三条——Memory 那一半就这样丢了三天。下面的样本取自那 5 起原件。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests  # noqa: F401,E402  —— 状态目录隔离

from core import context as C                         # noqa: E402
from core import tools                                # noqa: E402

REAL_INCIDENTS = {
    "write_file 毁掉半份文档": (
        "write_file", "content",
        "### 差距 3：老化阈值是全局常量，不自适应\n…\n[参数投影 field=content "
        "artifact=sha256:6a4c8e5e0b0e6c1e original_chars=3,838 omitted_chars=3,638；"
        "原始参数仍在会话事件里，落盘内容重新读文件即可]"),
    "heredoc 写到一半": (
        "bash", "command",
        "cd /repo && python3 - <<'PYEOF'\npath = 'tests/test_x.py'\nold = '''  \"try:\n    …[参数投影 "
        "field=command artifact=sha256:da8927680b07458b original_chars=1,056 omitted_chars=856；"
        "原始参数仍在会话事件里，落盘内容重新读文件即可]"),
    "走样：artifact 不是哈希": (
        "bash", "command",
        'grep -n "balance" main.tex | head; echo "== confirm 0 unverified count\n'
        "…[参数投影 field=command artifact=figure_object_left_out_of_context]"),
    "走样：结尾成了乱码": (
        "bash", "command",
        'curl -sL --max-time 20 "https://example.edu/f?x=q\n…[参数投影 field=command '
        "artifact=sha256:55659d0f5b8e3b0c original_chars=627 omitted_chars=427；原始参数仍在会6: 3件如何，"
        "落盘内容重新读文件即可]"),
}


class RefusalTests(unittest.TestCase):
    def test_every_real_incident_would_have_been_refused(self):
        for label, (tool, field, value) in REAL_INCIDENTS.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                args = {field: value}
                if tool == "write_file":
                    args["path"] = os.path.join(tmp, "doc.md")
                with self.assertRaises(tools.Denied) as caught:
                    tools.prepare(tool, args, workspace_root=tmp)
                self.assertEqual(caught.exception.decision,
                                 "projection_marker_in_arguments")
                if tool == "write_file":
                    self.assertFalse(os.path.exists(args["path"]), "拦晚了：文件已经落盘")

    def test_the_refusal_tells_the_model_what_the_marker_is_and_what_to_do(self):
        with self.assertRaises(tools.Denied) as caught:
            tools._refuse_projection_markers(
                "write_file", {"path": "/x", "content": "头部\n…[参数投影 field=content x]"})
        message = str(caught.exception)
        for must in ("不是内容", "完整的真实内容", "重新读文件"):
            self.assertIn(must, message)

    def test_result_stubs_and_omission_blocks_are_refused_too(self):
        for text in ("[工具结果投影 tier=aged tool=bash status=ok artifact=sha256:ab]",
                     "<context-projection-omissions>\n- raw #13"):
            with self.subTest(text[:12]), self.assertRaises(tools.Denied):
                tools._refuse_projection_markers("edit_file", {"new_string": text})

    def test_nested_arguments_are_searched(self):
        with self.assertRaises(tools.Denied):
            tools._refuse_projection_markers("todo_write", {"todos": [
                {"content": "修 X …[参数投影 field=content y]", "status": "pending"}]})


class NoFalseAlarmTests(unittest.TestCase):
    def test_prose_that_merely_mentions_the_mechanism_is_fine(self):
        tools._refuse_projection_markers("write_file", {
            "path": "/x", "content": "DEVLOG：参数投影会把旧参数收起来；工具结果投影同理。"})

    def test_searching_for_the_marker_is_legitimate(self):
        """开发 zylab 自己时要能搜这个标记：只读搜索类工具不拦。"""
        tools._refuse_projection_markers("grep", {"pattern": r"\[参数投影 field="})
        tools._refuse_projection_markers("read_file", {"path": "[参数投影 field=x].md"})
        tools._refuse_projection_markers("bash", {"command": "grep -rn '参数投影' core/"})

    def test_the_guard_and_the_generators_share_one_pattern(self):
        """生成器改了写法而闸没跟上 = 闸悄悄失效。用真生成器的输出来验闸。"""
        projected, _ = C._preview_tool(
            "x" * 5000, {"name": "bash", "arguments": "{}"}, False, 400, 200)
        self.assertRegex(projected, C.PROJECTION_MARKER)
        omitted = C._omission_messages([{"start": 3, "end": 9, "reason": "budget"}])
        self.assertRegex(omitted[0]["content"], C.PROJECTION_MARKER)

    def test_the_retired_argument_stub_is_still_recognised(self):
        """「参数投影」的生成器 2026-09-20 已删（模型自己的参数不再改写），但闸必须继续认它：
        旧会话的原始记录里留着模型当年照抄的假占位符，恢复会话后模型看得见，就可能再抄。
        下面这条是删除前真生成器的原样输出。"""
        frozen = ('{"path": "/a", "content": "' + "字" * 200 + "\\n…[参数投影 field=content "
                  "artifact=sha256:0f3c9d2e1a4b5c6d original_chars=900 omitted_chars=700；"
                  '原始参数仍在会话事件里，落盘内容重新读文件即可]"}')
        self.assertRegex(frozen, C.PROJECTION_MARKER)
        with self.assertRaises(tools.Denied):
            tools._refuse_projection_markers("write_file", {"path": "/a", "content": frozen})


if __name__ == "__main__":
    unittest.main()
