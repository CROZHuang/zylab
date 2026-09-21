"""Windows 控制台按键 → tui 键名的映射契约（2026-09-17）。

这些用例在**所有平台**上跑：它们不碰真控制台，只把 `msvcrt.getwch` 的已知
输出喂进 `wincompat.read_console`，再让 `core/tui.py` 的解码器去认。
Windows 的键盘路径没有 PTY 可测，这层契约就是它唯一的回归网。

记住教训：早先的实现把 Ctrl+← 的扫描码当成 F5，把 Shift+Tab 整个吞掉
（SPEC-CC-parity B1 的权限模式循环键因此在 Windows 上完全不工作），
而这两件事没有任何测试会发现。
"""
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

from core import tui, wincompat  # noqa: E402


class FakeMsvcrt:
    """按 getwch 的真实约定回放一串码元。"""

    def __init__(self, units):
        self.units = list(units)

    def kbhit(self):
        return bool(self.units)

    def getwch(self):
        return self.units.pop(0)


class ConsoleKeyDecoding(unittest.TestCase):
    def decode(self, units):
        """把码元序列喂给 read_console，返回它产出的字节。"""
        fake = FakeMsvcrt(units)
        real = sys.modules.get("msvcrt")
        sys.modules["msvcrt"] = fake
        try:
            return wincompat.read_console(1, 0.05)
        finally:
            if real is None:
                sys.modules.pop("msvcrt", None)
            else:
                sys.modules["msvcrt"] = real

    def key_name(self, units):
        """再往前走一步：这串码元最终在 tui 里叫什么键。"""
        raw = self.decode(units).decode("utf-8")
        return tui.KEYS.get(raw, raw)

    def test_shift_tab_reaches_the_mode_cycle(self):
        """B1：Shift+Tab 必须解成 shift-tab，否则权限模式循环在 Windows 上没有入口。"""
        self.assertEqual(self.key_name(["\x00", "\x0f"]), "shift-tab")

    def test_arrows_and_navigation(self):
        for units, expected in (
                (["\xe0", "H"], "up"), (["\xe0", "P"], "down"),
                (["\xe0", "K"], "left"), (["\xe0", "M"], "right"),
                (["\xe0", "G"], "home"), (["\xe0", "O"], "end"),
                (["\xe0", "I"], "pageup"), (["\xe0", "Q"], "pagedown"),
                (["\xe0", "S"], "delete")):
            with self.subTest(units=units):
                self.assertEqual(self.key_name(units), expected)

    def test_ctrl_arrows_are_not_impersonating_function_keys(self):
        """Ctrl+← 曾被映射成 F5（\\x1b[15~）—— 按一次就会触发别的动作。"""
        self.assertEqual(self.decode(["\xe0", "s"]), b"\x1b[1;5D")
        self.assertEqual(self.decode(["\xe0", "t"]), b"\x1b[1;5C")
        for units in (["\xe0", "s"], ["\xe0", "t"], ["\xe0", "w"], ["\xe0", "u"]):
            with self.subTest(units=units):
                self.assertNotIn(b"15~", self.decode(units))

    def test_two_prefixes_are_two_tables(self):
        """\\x86 在功能键组是 F12，在导航键组不是 —— 合成一张表必然撞车。"""
        self.assertEqual(self.decode(["\x00", "\x86"]), b"\x1b[24~")
        self.assertNotEqual(self.decode(["\xe0", "H"]), self.decode(["\x00", ";"]))

    def test_function_keys_use_their_real_scan_codes(self):
        self.assertEqual(self.decode(["\x00", ";"]), b"\x1bOP")     # F1
        self.assertEqual(self.decode(["\x00", "D"]), b"\x1b[21~")   # F10

    def test_plain_and_control_characters_pass_through(self):
        self.assertEqual(self.key_name(["\r"]), "enter")
        self.assertEqual(self.key_name(["\x03"]), "ctrl-c")
        self.assertEqual(self.key_name(["\x0f"]), "ctrl-o")   # 裸 \x0f 不是 Shift+Tab
        self.assertEqual(self.decode(["中"]), "中".encode("utf-8"))

    def test_astral_characters_do_not_kill_the_input_thread(self):
        """emoji 以 UTF-16 代理对分两次到达；半个代理对 encode 会抛异常。"""
        code = ord("\U0001f600") - 0x10000
        units = [chr(0xD800 + (code >> 10)), chr(0xDC00 + (code & 0x3FF))]
        self.assertEqual(self.decode(units), "\U0001f600".encode("utf-8"))

    def test_unknown_special_key_is_swallowed_not_misread(self):
        self.assertEqual(self.decode(["\xe0", "\x99"]), b"")


if __name__ == "__main__":
    unittest.main()
