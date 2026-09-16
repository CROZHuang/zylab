"""AppRenderer：同一接口、缓冲里的画面、滚动、选区复制（OSC 52）、退出回放。"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import apprender, termcaps, tui  # noqa: E402


def caps(**kw):
    base = dict(tty=True, alt_screen=True, sync_output=False, truecolor=True, kitty_keyboard=True,
                sgr_mouse=True, osc52_write=True, cols=40, rows=10)
    base.update(kw)
    return termcaps.TermCaps(**base)


class _Stream(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        raise OSError("no fd")           # 让 _size() 回落到 caps 的尺寸


def snapshot(text="", **kw):
    return tui.InputSnapshot(mode="line", prompt="› ", text=text, cursor=len(text), active=True,
                             status="chat x · m@g", activity="空闲", **kw)


class AppRendererTests(unittest.TestCase):
    def setUp(self):
        self.out = _Stream()
        self.r = apprender.AppRenderer(self.out, caps=caps())

    def test_surface_matches_terminal_renderer(self):
        need = "render write_output clear_input clear_spinner finish_output_line history_active close_history bell commit_input enable_mouse disable_mouse mouse_enabled spinner clear_composer_selection set_transcript set_title set_actionable_dock scroll_history history_mouse history_click copy_history_selection copy_composer_selection composer_mouse flush".split()
        missing = [n for n in need if not hasattr(self.r, n)]
        self.assertEqual(missing, [])

    def test_output_lands_above_the_composer(self):
        self.r.render(snapshot("draft"))
        self.r.write_output("hello\n")
        dump = self.r._painter.prev.dump()
        lines = dump.split("\n")
        self.assertEqual(lines[0], "hello")
        self.assertTrue(any("draft" in l for l in lines[-6:]), dump)
        self.assertTrue(any("空闲" in l for l in lines[-3:]), dump)

    def test_scroll_shows_older_rows_and_history_active(self):
        for i in range(30):
            self.r.write_output(f"line{i}\n")
        self.r.render(snapshot())
        self.assertFalse(self.r.history_active)
        self.assertIn("line29", self.r._painter.prev.dump())
        self.r.scroll_history("page_up")
        self.assertTrue(self.r.history_active)
        dump = self.r._painter.prev.dump()
        self.assertNotIn("line29", dump.split("\n")[0:3])
        self.assertIn("↑", dump)
        self.r.close_history()
        self.assertFalse(self.r.history_active)

    def test_drag_selection_copies_via_osc52(self):
        self.r.write_output("copy me please\n")
        self.r.render(snapshot())
        press = tui.MouseEvent(kind="press", x=1, y=1, button=0, modifiers=0)
        release = tui.MouseEvent(kind="release", x=7, y=1, button=0, modifiers=0)
        self.r.history_mouse(press)
        self.r.history_mouse(release)
        out = self.out.getvalue()
        self.assertIn("\x1b]52;c;", out)
        import base64
        payload = out.split("\x1b]52;c;")[-1].split("\x07")[0]
        self.assertEqual(base64.b64decode(payload).decode(), "copy me")

    def test_drag_at_the_bottom_selects_without_scrolling_first(self):
        self.r.write_output("bottom line text\n")
        self.r.render(snapshot())
        press = tui.MouseEvent(kind="press", x=1, y=1, button=0, modifiers=0)
        motion = tui.MouseEvent(kind="motion", x=6, y=1, button=0, modifiers=0)
        release = tui.MouseEvent(kind="release", x=6, y=1, button=0, modifiers=0)
        self.r.composer_mouse(press); self.r.composer_mouse(motion); self.r.composer_mouse(release)
        import base64
        payload = self.out.getvalue().split("\x1b]52;c;")[-1].split("\x07")[0]
        self.assertEqual(base64.b64decode(payload).decode(), "bottom")
        self.assertEqual(self.r.mouse_events, 3)

    def test_wheel_after_a_width_change_still_scrolls(self):
        for i in range(40):
            self.r.write_output(f"row{i}\n")
        self.r.render(snapshot())
        self.r.caps = caps(cols=60, rows=10)          # 宽度变了，缓存要重折
        self.assertTrue(self.r.scroll_history(3))
        self.assertTrue(self.r.history_active)

    def test_enter_and_exit_replay_plain_transcript(self):
        self.r.enter()
        self.assertIn("\x1b[?1049h", self.out.getvalue())
        self.assertIn("\x1b[>1u", self.out.getvalue())
        self.assertIn("\x1b[?1006h", self.out.getvalue())
        self.r.write_output("\x1b[1mkept\x1b[0m text\n")
        self.r.exit()
        tail = self.out.getvalue().split("\x1b[?1049l")[-1]
        self.assertIn("kept text", tail)
        self.assertNotIn("\x1b[1m", tail)

    def test_set_transcript_reprojects_messages(self):
        self.r.set_transcript([{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"}])
        text = self.r.transcript.plain_text()
        self.assertIn("Q1", text)
        self.assertIn("⏺ A1", text)


class CcLookTests(unittest.TestCase):
    def setUp(self):
        self.out = _Stream()
        self.r = apprender.AppRenderer(self.out, caps=caps(cols=80, rows=12))

    def test_user_line_is_one_banded_line_not_a_box(self):
        self.r.commit_input("› ", "hello there")
        text = self.r.transcript.plain_text()
        self.assertIn("› hello there", text)
        self.assertNotIn("╭─", text)
        segs = self.r.transcript.lines[-1]
        self.assertTrue(all(style.bg == 236 for _, style in segs), segs)
        self.assertGreaterEqual(sum(len(t) for t, _ in segs), 80, "底色铺满整行")

    def test_placeholder_shows_only_when_draft_is_empty_and_idle(self):
        self.r.render(snapshot(""))
        self.assertIn("输入消息", self.r._painter.prev.dump())
        self.r.render(snapshot("x"))
        self.assertNotIn("输入消息", self.r._painter.prev.dump())

    def test_status_is_compact_and_spinner_carries_activity(self):
        self.r.render(tui.InputSnapshot(mode="line", prompt="› ", text="", cursor=0, active=True,
                                        status="chat abc · m@g · ctx 11K/936K · memory 0 · git main · SANDBOXED:x",
                                        activity="空闲"))
        dump = self.r._painter.prev.dump()
        self.assertIn("m@g", dump); self.assertIn("ctx 11K/936K", dump)
        self.assertNotIn("memory 0", dump); self.assertNotIn("SANDBOXED", dump); self.assertIn("chat abc", dump)
        self.r.spinner("思考中 3s", 3.0)
        dump = self.r._painter.prev.dump()
        self.assertIn("esc 中断", dump)
        self.assertNotIn("⠋", dump)


if __name__ == "__main__":
    unittest.main()
