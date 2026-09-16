"""错误行的唯一形状（⏺ 错误 · 一句话 / ⎿  下一步）与启动横幅方框。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from core import tui

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Agent:
    gateway = "deepinfer"


class _Session:
    agent = _Agent()


class ErrorLineTests(unittest.TestCase):
    def test_known_kind_gets_its_next_step(self):
        head, step = (ANSI.sub("", s) for s in zylab._error_lines("HTTP 502", "transient_http"))
        self.assertEqual(head, "⏺ 错误 · HTTP 502")
        self.assertTrue(step.startswith("  ⎿  "), step)
        self.assertIn("/gateway", step)

    def test_unknown_kind_falls_back_to_doctor(self):
        _, step = zylab._error_lines("weird", "never_seen_kind")
        self.assertIn("/doctor", step)

    def test_attachment_message_without_kind_is_recognised(self):
        _, step = zylab._error_lines("附件输入失败：太大", None)
        self.assertIn("附件", step)

    def test_message_is_one_line_and_bounded(self):
        head, _ = zylab._error_lines("first line\n<html>stack</html>\n" + "x" * 500, "network")
        plain = ANSI.sub("", head)
        self.assertNotIn("\n", plain)
        self.assertNotIn("<html>", plain)
        self.assertLess(len(plain), 220)

    def test_every_next_step_only_names_real_commands(self):
        for step in zylab._ERROR_NEXT_STEPS.values():
            for cmd in re.findall(r"/([a-z]+)", step):
                self.assertTrue(hasattr(zylab, f"cmd_{cmd}"),
                                f"{step!r} 引用了不存在的命令 /{cmd}")


class BannerTests(unittest.TestCase):
    def render(self, width, cwd="/tmp/workspace/zylab", build="abc1234"):
        lines = zylab._startup_banner(_Session(), "kimi-k3-256k", width=width, cwd=cwd, build=build)
        return lines, [ANSI.sub("", l) for l in lines]

    def test_box_is_rectangular_and_within_width(self):
        for width in (40, 60, 80, 120):
            lines, plain = self.render(width)
            widths = {tui.display_width(l) for l in plain}
            self.assertEqual(len(widths), 1, (width, plain))
            self.assertLessEqual(widths.pop(), width - 1, "最后一列留空，避免 pending-wrap")
            self.assertTrue(plain[0].startswith("╭") and plain[-1].startswith("╰"))
            for row in plain[1:-1]:
                self.assertTrue(row.startswith("│") and row.endswith("│"), row)

    def test_banner_carries_identity_model_cwd_and_help_anchor(self):
        _, plain = self.render(80)
        text = "\n".join(plain)
        self.assertIn("◆ zylab", text)
        self.assertIn("build abc1234", text)
        self.assertIn("kimi-k3-256k @ deepinfer", text)
        self.assertIn("/tmp/workspace/zylab", text)
        self.assertIn("/help 看命令", text, "会话选择器 PTY 测试的同步锚点")
        self.assertIn("Esc=中断", text)
        self.assertNotIn("空闲", text, "PTY 测试拿「空闲」同步第一帧状态栏，横幅撞词会提前触发")

    def test_long_cwd_keeps_its_tail(self):
        cwd = "/tmp/workspace/" + "deep/" * 30 + "zylab"
        _, plain = self.render(60, cwd=cwd)
        row = next(r for r in plain if "目录" in r)
        self.assertIn("…", row)
        self.assertIn("zylab", row)

    def test_no_build_id_means_no_build_field(self):
        _, plain = self.render(80, build="")
        self.assertNotIn("build", "\n".join(plain))


if __name__ == "__main__":
    unittest.main()
