"""H1/H2（SPEC-CC-parity）：完成响铃与终端标题的渲染器层契约。"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


class _Tty(io.StringIO):
    def isatty(self):
        return True


class BellTests(unittest.TestCase):
    def test_bell_writes_bel_on_tty(self):
        out = _Tty(); r = tui.TerminalRenderer(stream=out)
        self.assertTrue(r.bell())
        self.assertEqual(out.getvalue(), "\x07")

    def test_bell_is_silent_off_tty(self):
        """管道 / -p / 测试里的 StringIO：绝不往被重定向的输出里塞 \\x07。"""
        out = io.StringIO(); r = tui.TerminalRenderer(stream=out)
        self.assertFalse(r.bell())
        self.assertEqual(out.getvalue(), "")


class TitleTests(unittest.TestCase):
    def test_title_uses_osc0_and_bel_terminator(self):
        out = _Tty(); r = tui.TerminalRenderer(stream=out)
        self.assertTrue(r.set_title("✳ zylab · repo"))
        self.assertEqual(out.getvalue(), "\x1b]0;✳ zylab · repo\x07")

    def test_empty_title_clears(self):
        out = _Tty(); r = tui.TerminalRenderer(stream=out)
        r.set_title("")
        self.assertEqual(out.getvalue(), "\x1b]0;\x07")

    def test_control_characters_cannot_inject_sequences(self):
        """session 名来自用户；BEL/ESC/ST 必须剥掉，否则能从标题里逃逸出新序列。"""
        out = _Tty(); r = tui.TerminalRenderer(stream=out)
        r.set_title("evil\x07\x1b]52;c;SECRET\x07\x9cname")
        payload = out.getvalue()
        self.assertEqual(payload.count("\x07"), 1, "只允许结尾那一个 BEL")
        self.assertNotIn("\x1b]52", payload)
        self.assertTrue(payload.startswith("\x1b]0;evil"))

    def test_title_is_clamped(self):
        out = _Tty(); r = tui.TerminalRenderer(stream=out)
        r.set_title("x" * 500)
        self.assertLessEqual(len(out.getvalue()), len("\x1b]0;") + 120 + 1)

    def test_title_is_silent_off_tty(self):
        out = io.StringIO(); r = tui.TerminalRenderer(stream=out)
        self.assertFalse(r.set_title("anything"))
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()


from core import settings as _settings


class NotifySettingsTests(unittest.TestCase):
    def _cfg(self):
        return {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in _settings.DEFAULTS.items()}

    def test_defaults_are_on(self):
        self.assertEqual(_settings.DEFAULTS["notify"], {"bell": True, "title": True})

    def test_partial_override_keeps_other_keys(self):
        cfg = self._cfg()
        _settings._merge(cfg, {"notify": {"bell": False}}, False)
        self.assertEqual(cfg["notify"], {"bell": False, "title": True})

    def test_non_bool_and_unknown_keys_are_ignored(self):
        cfg = self._cfg()
        _settings._merge(cfg, {"notify": {"title": "yes", "bogus": 1}}, False)
        self.assertEqual(cfg["notify"], {"bell": True, "title": True})
