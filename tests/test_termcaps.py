"""termcaps：非 TTY 全关；缓存按终端身份命中；DECRQM/OSC 11 解析；探测有预算。"""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import termcaps  # noqa: E402


class TermCapsTests(unittest.TestCase):
    def test_non_tty_is_all_off(self):
        caps = termcaps.probe(stream_in=io.StringIO(), stream_out=io.StringIO(), environ={})
        self.assertFalse(caps.tty)
        self.assertFalse(caps.alt_screen or caps.sync_output or caps.kitty_keyboard)

    def test_dark_detection_from_osc11(self):
        self.assertTrue(termcaps._is_dark("rgb:1e1e/1e1e/1e1e"))
        self.assertFalse(termcaps._is_dark("rgb:ffff/ffff/ffff"))
        self.assertTrue(termcaps._is_dark("garbage"))

    def test_cache_round_trip_keyed_by_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(termcaps, "_cache_path", return_value=termcaps.Path(tmp) / "termcaps.json") \
                    if hasattr(termcaps, "Path") else mock.patch.object(
                        termcaps, "_cache_path", return_value=__import__("pathlib").Path(tmp) / "termcaps.json"):
                caps = termcaps.TermCaps(tty=True, sync_output=True, kitty_keyboard=True, terminal="xterm.js(6.1)")
                termcaps.save_cached("vscode|3.14.7|xterm-256color", caps)
                self.assertEqual(termcaps.load_cached("vscode|3.14.7|xterm-256color"), caps)
                self.assertIsNone(termcaps.load_cached("other|1|xterm"))

    def test_probe_on_a_silent_pty_finishes_within_budget(self):
        try:
            import pty
        except ImportError:                 # Windows：pty 依赖 termios，不存在
            # **skip 不等于通过**（AGENTS.md §7）：这条量的是「没人应答时探测会
            # 在预算内返回」，而 Windows 上造不出「静默 pty」这个载体——ConPTY
            # 由控制台驱动，没有「挂在那儿不读」的对端。同样的契约在 Windows 上
            # 由 tests/conpty.py 的真 ConPTY 冒烟覆盖。
            self.skipTest("需要 POSIX pty（Windows 上没有 termios）")
        master, slave = pty.openpty()
        try:
            fin = os.fdopen(slave, "rb", buffering=0)
            fout = os.fdopen(os.dup(slave), "w")
            start = time.monotonic()
            caps = termcaps.probe(stream_in=fin, stream_out=fout, environ={"COLORTERM": "truecolor"}, budget=0.15)
            elapsed = time.monotonic() - start
        finally:
            os.close(master)
        self.assertTrue(caps.tty)
        self.assertTrue(caps.truecolor)
        self.assertFalse(caps.sync_output)
        self.assertLess(elapsed, 1.0, f"探测超预算：{elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
