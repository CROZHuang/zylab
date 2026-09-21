"""/doctor 的外部工具体检（2026-09-18）。

为什么有这一条：一次真实排查里，机器上 Git **是装了的**，但安装器默认只把
`<Git>\\cmd` 加进 PATH，`usr\\bin` 不在 —— 于是 bash/sed/awk/ssh 全找不到，
Bash 工具整个不可用。那次故障是在模型跑到一半时才以一句「找不到 bash」暴露的，
排查花了一轮来回；而它本该在 `/doctor` 里一眼看见。

这里钉两件事：**缺东西时说得出缺哪个**，以及**给的补救是可照做的**
（尤其要点名 `usr\\bin` —— 加成 `bin` 是这个坑最常见的第二次踩法）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

os.environ.setdefault("ZYLAB_HOME", tempfile.mkdtemp())
import zylab  # noqa: E402

WINDOWS = sys.platform == "win32"


class ExternalToolsRow(unittest.TestCase):
    def row(self, available):
        """available: 一个「哪些命令找得到」的集合。"""
        def fake_which(name, *args, **kwargs):
            return f"/fake/bin/{name}" if name in available else None

        with mock.patch("shutil.which", side_effect=fake_which):
            return zylab._external_tools_row()

    def test_everything_present_reports_ok_and_says_nothing_more(self):
        every = set(zylab._DOCTOR_REQUIRED_TOOLS) | set(zylab._DOCTOR_SHELL_TOOLS)
        value, hints = self.row(every)
        self.assertTrue(value.startswith("OK · "), value)
        self.assertEqual(hints, [], "一切正常时不该刷屏")

    def test_missing_tools_are_named(self):
        every = set(zylab._DOCTOR_REQUIRED_TOOLS) | set(zylab._DOCTOR_SHELL_TOOLS)
        value, _ = self.row(every - {"sed", "awk"})
        self.assertIn("sed", value)
        self.assertIn("awk", value)

    def test_missing_marks_the_row_red(self):
        """/doctor 的着色逻辑按 value.startswith("MISSING") 判红 —— 别改这个前缀。"""
        value, _ = self.row(set())
        self.assertTrue(value.startswith("MISSING"), value)

    def test_missing_bash_is_called_out_as_blocking_the_bash_tool(self):
        every = set(zylab._DOCTOR_REQUIRED_TOOLS) | set(zylab._DOCTOR_SHELL_TOOLS)
        _, hints = self.row(every - {"bash"})
        joined = "\n".join(hints)
        self.assertIn("bash", joined)
        self.assertIn("Bash 工具", joined)

    def test_grep_tool_is_not_blamed_on_a_missing_grep_binary(self):
        """grep 工具已是进程内实现；提示不该让人以为搜索也坏了。"""
        every = set(zylab._DOCTOR_REQUIRED_TOOLS) | set(zylab._DOCTOR_SHELL_TOOLS)
        _, hints = self.row(every - {"bash", "grep"})
        self.assertIn("不受影响", "\n".join(hints))

    @unittest.skipUnless(WINDOWS, "补救文案是平台相关的")
    def test_windows_hint_names_usr_bin_not_bin(self):
        """加成 <Git>\\bin 是这个坑最常见的第二次踩法，提示必须点名 usr\\bin。"""
        _, hints = self.row(set())
        joined = "\n".join(hints)
        self.assertIn("usr", joined)
        self.assertIn("Git for Windows", joined)
        self.assertIn("新进程", joined, "必须提醒 PATH 只对新进程生效")

    def test_row_is_cheap_enough_to_run_every_doctor(self):
        """只查十来个名字；真去跑子进程的话 /doctor 会变慢。"""
        calls = []

        def counting_which(name, *args, **kwargs):
            calls.append(name)
            return "/fake/bin/" + name

        with mock.patch("shutil.which", side_effect=counting_which):
            zylab._external_tools_row()
        self.assertLessEqual(len(calls), 16, f"查得太多：{calls}")


if __name__ == "__main__":
    unittest.main()
