"""要看全的内容折行、不截断（用户 2026-09-04 截图：表格单元格与指令说明被截成"…"）。

分工：单行 UI（状态栏、输入框、选择器行、折叠摘要）仍按列截断；正文表格、二级指令
说明、审批明细、高风险预览、工具/技能/会话列表的说明列按列宽折行。
"""
import io
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from unittest import mock as _mock
from core import tui

ANSI = re.compile(r"\x1b\[[0-9;]*m")
EVIDENCE = ("kc.skills/commands/workflows/subagent/tasks/checkpoints/repomap/"
            "webfetch/graft/goals 均可导入")
TABLE = ("| 项目 | 结果 | 证据 |\n|---|---|---|\n"
         f"| 模块导入 | 21/21 全过 | {EVIDENCE} |\n"
         "| gate | normalize 正常 | normalize_decision_gate_request 返回 (dict, frozenset)；"
         "t_decision_gate 在无 runtime 时也能工作 |\n"
         "| F0 | BLOCKED | t_bash 写入 /protected/archive/test.txt 被拒绝 |\n\n")


def render(text, columns, per_char=False):
    md = tui.StreamingMarkdown(columns=columns, styled=False)
    fed = "".join(md.feed(c) for c in text) if per_char else md.feed(text)
    return ANSI.sub("", fed + md.finish())


def cells_of(rendered):
    """把物理行拆回列：返回 [[col0, col1, col2], ...]（含表头，去掉边框行）。"""
    rows = []
    for line in rendered.split("\n"):
        if line.strip().startswith("│"):
            rows.append([c.strip() for c in line.strip().strip("│").split("│")])
    return rows


class MarkdownTableTests(unittest.TestCase):
    def test_long_cells_wrap_inside_the_column_and_nothing_is_elided(self):
        out = render(TABLE, 70)
        self.assertNotIn("…", out)
        self.assertTrue(all(tui.display_width(l) <= 70 for l in out.split("\n")))
        joined = "".join(row[2] for row in cells_of(out))
        # 折行只在空格处丢空格；去掉空格后证据列内容一字不少
        self.assertIn(EVIDENCE.replace(" ", ""), joined.replace(" ", ""))

    def test_underscores_in_identifiers_survive(self):
        out = render(TABLE, 120)
        for token in ("t_bash", "normalize_decision_gate_request", "t_decision_gate"):
            self.assertIn(token, out.replace("\n", ""), token)

    def test_small_table_is_compact_not_full_width(self):
        out = render("| 名称 | 值 |\n|:---|---:|\n| 中文 | 42 |\n\n", 80)
        widths = {tui.display_width(l) for l in out.split("\n") if l.strip()}
        self.assertEqual(widths, {11}, out)
        self.assertIn("│42│", out, "右对齐列宽等于内容宽")

    def test_natural_widths_are_used_when_everything_fits(self):
        out = render(TABLE, 200)
        self.assertNotIn("…", out)
        rows = cells_of(out)
        self.assertEqual(len(rows), 4, "每行一条物理行，没有折行")
        self.assertLess(max(tui.display_width(l) for l in out.split("\n")), 200)

    def test_table_output_is_invariant_to_chunking(self):
        self.assertEqual(render(TABLE, 60, per_char=True), render(TABLE, 60))

    def test_header_row_wraps_too(self):
        out = render("| 一个非常非常非常长的表头名称 | b |\n|---|---|\n| x | y |\n\n", 20)
        self.assertNotIn("…", out)
        self.assertTrue(all(tui.display_width(l) <= 20 for l in out.split("\n")))
        self.assertIn("表头", out)


class WrapDisplayTests(unittest.TestCase):
    def test_breaks_at_spaces_and_hard_cuts_cjk(self):
        self.assertEqual(tui.wrap_display("aaa bbb ccc", 7), ["aaa bbb", "ccc"])
        self.assertEqual(tui.wrap_display("中文中文中文", 4), ["中文", "中文", "中文"])
        self.assertEqual(tui.wrap_display("a\nb", 10), ["a", "b"])
        self.assertEqual(tui.wrap_display("", 10), [""])
        long = "x" * 25
        self.assertEqual(tui.wrap_display(long, 10), ["x" * 10, "x" * 10, "x" * 5])


class GuidedOptionTests(unittest.TestCase):
    def test_long_descriptions_wrap_instead_of_eliding(self):
        renderer = tui.TerminalRenderer(io.StringIO())
        options = (
            ("/workflow research", "只读研究模式 · 后接一个很长很长的目标说明，用来验证说明会折行而不是被截断成省略号"),
            ("/workflow auto", "切换自适应 workflow"),
        )
        lines = renderer._guided_option_lines(options, 60, 0)
        plain = [ANSI.sub("", l) for l in lines]
        self.assertTrue(all(tui.display_width(l) <= 60 for l in plain), plain)
        self.assertNotIn("…", "".join(plain))
        self.assertGreater(len(plain), 2, "长说明占多行")
        self.assertIn("省略号", "".join(plain))
        self.assertTrue(plain[1].startswith(" " * 21), "续行对齐在说明列下")

    def test_descriptions_are_capped_at_three_lines(self):
        renderer = tui.TerminalRenderer(io.StringIO())
        options = (("/x", "很长 " * 200),)
        lines = renderer._guided_option_lines(options, 30, 0)
        self.assertLessEqual(len(lines), 3)
        self.assertIn("…", ANSI.sub("", lines[-1]))


class TableLayoutWrapTests(unittest.TestCase):
    def test_wrap_last_keeps_columns_aligned(self):
        layout = tui.TableLayout.fit((
            tui.TableColumn(6), tui.TableColumn(20, min_width=10)), 30)
        text = layout.row(("name", "a description that is longer than twenty cells"),
                          wrap_last=True)
        lines = text.split("\n")
        self.assertGreater(len(lines), 1)
        self.assertNotIn("…", text)
        self.assertTrue(all(tui.display_width(l) <= 30 for l in lines))
        self.assertTrue(all(l.startswith(" " * 8) for l in lines[1:]), lines)
        self.assertEqual(layout.row(("name", "short")).count("\n"), 0)


class ApprovalDetailTests(unittest.TestCase):
    def test_long_command_is_wrapped_not_elided(self):
        command = "python3 scripts/run.py --input /very/long/path/" + "x" * 150 + " --flag value"
        out = io.StringIO()
        with mock.patch.object(os, "get_terminal_size", return_value=os.terminal_size((80, 24))), \
             mock.patch.object(sys, "stdout", out):
            zylab._print_approval_details("bash", {"command": command})
        plain = ANSI.sub("", out.getvalue())
        self.assertNotIn("…", plain)
        self.assertTrue(all(tui.display_width(l) <= 80 for l in plain.split("\n")))
        self.assertIn("--flag value", plain)

    def test_help_no_longer_describes_hybrid_mouse(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            zylab.cmd_help(mock.Mock(_custom_catalog=None), "")
        text = out.getvalue()
        self.assertNotIn("hybrid", text)
        self.assertIn("终端原生", text)



# 受保护路径默认为空 —— 测试必须自己声明要守的东西，否则会在维护者机器上因为
# 读到用户配置而意外变绿、在别人 clone 下来的仓库里变红。
#
# 只打 tools.PROTECTED（词法守卫），**不设环境变量**：环境变量会同时喂给沙箱层，
# 而 sandbox 要求受保护路径真实存在，合成路径必然不存在，会把沙箱打成 fail-closed。
# PROTECTED 虽是 import 时赋值，但守卫是调用时查模块全局，所以 patch 与 import 顺序无关。
# tools 在函数内 import：不是每个用到守卫的测试模块都在顶层 import 它。
_protected_patches = []


def setUpModule():
    from core import tools as _guarded
    for name, value in (("PROTECTED", ("/protected/archive",)),
                        ("PROTECTED_REMOTES", ("archive:bucket",))):
        patch = _mock.patch.object(_guarded, name, value)
        patch.start()
        _protected_patches.append(patch)


def tearDownModule():
    while _protected_patches:
        _protected_patches.pop().stop()

if __name__ == "__main__":
    unittest.main()
