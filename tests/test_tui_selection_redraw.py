"""Gate 0 regression tests for history continuity and app-owned selection.

These tests deliberately inspect the final terminal cell model instead of
counting redraw escape sequences.  The production renderer remains free to
choose an efficient redraw strategy as long as the visible contract holds.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from core import tui
from tests.terminal_screen import parse_final_screen
from tests.pty_harness import run_pty_child


class Gate0RendererTests(unittest.TestCase):
    def tty(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        return TTY()

    def messages(self, count=24):
        result = []
        for index in range(count):
            result.extend((
                {"role": "user", "content": f"prompt {index}"},
                {"role": "assistant", "content": f"answer {index}"},
            ))
        return result

    def snapshot(self, *, text="draft", cursor=None, queued=()):
        if cursor is None:
            cursor = len(text)
        return tui.InputSnapshot(
            mode="line", prompt="› ", text=text, cursor=cursor,
            queued=tuple(queued),
        )

    @contextlib.contextmanager
    def sized(self, cols=52, rows=16):
        with mock.patch.object(tui, "_cols", return_value=cols), mock.patch.object(
                tui, "_lines", return_value=rows):
            yield

    def render_screen(self, renderer, snapshot, *, cols=52, rows=16):
        with self.sized(cols, rows):
            renderer.render(snapshot)
        return parse_final_screen(renderer.stream.getvalue(), cols, rows)

    def enter(self, renderer, snapshot=None, *, cols=52, rows=16, delta=0):
        snapshot = snapshot or self.snapshot()
        with self.sized(cols, rows):
            self.assertTrue(renderer.scroll_history(delta, snapshot))
        return parse_final_screen(renderer.stream.getvalue(), cols, rows)












    def select_history(self, renderer, snapshot=None):
        snapshot = snapshot or self.snapshot()
        if not renderer.history_active:
            renderer.scroll_history(0, snapshot)
        renderer.history_mouse(tui.MouseEvent("press", 3, 2, 0), snapshot)
        renderer.history_mouse(tui.MouseEvent("motion", 10, 2, 0), snapshot)
        renderer.history_mouse(tui.MouseEvent("release", 10, 2, 0), snapshot)
        self.assertTrue(renderer.history_selection_active)
        return snapshot

    def select_composer(self, renderer, snapshot=None):
        snapshot = snapshot or self.snapshot(text="hello world")
        renderer.composer_mouse(
            tui.MouseEvent("press", 3, 2, 0), snapshot, cursor_screen_row=2)
        renderer.composer_mouse(
            tui.MouseEvent("motion", 8, 2, 0), snapshot, cursor_screen_row=2)
        renderer.composer_mouse(
            tui.MouseEvent("release", 8, 2, 0), snapshot, cursor_screen_row=2)
        self.assertTrue(renderer.composer_selection_active)
        return snapshot


    def test_external_backend_exit_zero_is_confirmed(self):
        renderer = tui.TerminalRenderer(self.tty())
        completed = mock.Mock(returncode=0)
        with (mock.patch.dict(os.environ, {"ZYLAB_CLIPBOARD": "external"}, clear=False),
              mock.patch.object(tui.shutil, "which", return_value="/usr/bin/wl-copy"),
              mock.patch.object(tui.subprocess, "run", return_value=completed)):
            result = renderer._copy_result("hello")
        self.assertEqual(result.status, "confirmed_external")
        self.assertEqual(result.backend, "wl-copy")

    def test_osc52_is_requested_not_reported_confirmed(self):
        renderer = tui.TerminalRenderer(self.tty())
        with mock.patch.dict(os.environ, {"ZYLAB_CLIPBOARD": "osc52"}, clear=False):
            result = renderer._copy_result("hello")
        self.assertEqual(result.status, "requested_osc52")
        self.assertNotEqual(result.status, "confirmed_external")
        self.assertIn("已发送复制请求", renderer._last_copy_notice)

    def test_clipboard_off_reports_not_executed(self):
        renderer = tui.TerminalRenderer(self.tty())
        with mock.patch.dict(os.environ, {"ZYLAB_CLIPBOARD": "off"}, clear=False):
            result = renderer._copy_result("secret")
        self.assertEqual(result.status, "disabled")
        self.assertIn("复制未执行", renderer._last_copy_notice)

    def test_all_external_backends_fail_reports_or_falls_back_explicitly(self):
        renderer = tui.TerminalRenderer(self.tty())
        failed = mock.Mock(returncode=1)
        with (mock.patch.dict(os.environ, {"ZYLAB_CLIPBOARD": "external"}, clear=False),
              mock.patch.object(tui.shutil, "which", return_value="/bin/helper"),
              mock.patch.object(tui.subprocess, "run", return_value=failed)):
            result = renderer._copy_result("hello")
        self.assertEqual(result.status, "failed")
        self.assertIn("复制失败", renderer._last_copy_notice)







    def test_shift_drag_never_enters_app_selection(self):
        pump = tui.InputPump()
        for kind in ("press", "motion", "release"):
            self.assertEqual(pump._handle_line(
                tui.MouseEvent(kind, 3, 2, 0, modifiers=tui._MOUSE_SHIFT)), [])




class ScreenModelTests(unittest.TestCase):
    def test_screen_model_handles_alt_buffer_unicode_and_osc(self):
        data = (
            "primary\r\n"
            "\x1b[?1049h\x1b[2J\x1b[H"
            "A中文e\u0301\t👩\u200d💻\x1b]52;c;aGk=\x07"
            "\x1b[?1002h\x1b[?1006h"
            "\x1b[?1049l")
        result = parse_final_screen(data, 24, 12)
        self.assertEqual(result["active_buffer"], "primary")
        self.assertIn("A中文", result["alternate"].row_text(0))
        self.assertEqual(result["mouse_modes"], (1002, 1006))
