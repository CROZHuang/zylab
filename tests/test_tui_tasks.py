"""M3c-4 task-control TUI primitives."""
import copy
import io
import os
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


class CtrlBKeyTests(unittest.TestCase):
    def test_read_key_decodes_ctrl_b(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"\x02")
            with os.fdopen(read_fd, "rb", buffering=0) as stream:
                read_fd = None
                self.assertEqual(
                    tui.read_key(timeout=0.1, stream=stream), "ctrl-b")
        finally:
            os.close(write_fd)
            if read_fd is not None:
                os.close(read_fd)


class LineEditorBackgroundTests(unittest.TestCase):
    def assert_background_is_state_preserving(self, editor):
        before = copy.deepcopy(editor.__dict__)

        events = editor.handle("ctrl-b")

        self.assertEqual(events, [tui.PumpEvent("background")])
        self.assertEqual(editor.__dict__, before)

    def test_ctrl_b_preserves_idle_busy_and_target_composers(self):
        idle = tui.LineEditor(history=["old prompt"])
        idle.replace("idle draft")
        idle.cursor = 4

        busy = tui.LineEditor(history=["old prompt"])
        busy.set_busy(True)
        busy.replace("queued draft")
        busy.cursor = 6

        target = tui.LineEditor(history=["main prompt"])
        target.set_target("agent-1", history=["child prompt"])
        target.replace("child draft")
        target.cursor = 5

        for editor in (idle, busy, target):
            with self.subTest(
                    busy=editor.busy, target=editor.target,
                    text=editor.text):
                self.assert_background_is_state_preserving(editor)

    def test_ctrl_b_preserves_history_navigation_and_command_menu_state(self):
        recalled = tui.LineEditor(
            commands={"/help": "帮助"}, history=["/help"])
        recalled.handle("up")

        command = tui.LineEditor(
            commands={"/help": "帮助", "/hooks": "hooks"})
        command.replace("/h")
        command.menu_index = 1

        for editor in (recalled, command):
            with self.subTest(text=editor.text):
                self.assert_background_is_state_preserving(editor)


class InputPumpPickerTests(unittest.TestCase):
    def test_ctrl_b_does_not_escape_or_emit_background_from_picker(self):
        pump = tui.InputPump()
        pump.open_picker(
            ["first", "second"], allow_filter=False,
            actions={"enter": "attach"})
        pump.drain()
        before = copy.deepcopy(pump._picker)

        events = pump._handle_picker("ctrl-b")

        self.assertEqual(pump._mode, "picker")
        self.assertEqual(pump._picker, before)
        self.assertEqual([event.kind for event in events], ["redraw"])
        self.assertEqual(events[0].snapshot.mode, "picker")


class RendererSourceTests(unittest.TestCase):
    def test_source_switch_finishes_partial_line_before_next_stream(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)

        renderer.write_output("MODEL", source="model")
        renderer.write_output("TASK", source="task:bg1")
        renderer.write_output("STATUS", source="status")

        value = stream.getvalue()
        model_to_task = value[value.index("MODEL"):value.index("TASK")]
        task_to_status = value[value.index("TASK"):value.index("STATUS")]
        self.assertEqual(model_to_task.count("\r\n"), 2)
        self.assertEqual(task_to_status.count("\r\n"), 2)
        self.assertEqual(renderer._output_source, "status")

    def test_same_source_keeps_partial_chunk_continuation(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)

        renderer.write_output("RAW_", source="task:bg1")
        renderer.write_output("DONE", source="task:bg1")

        value = stream.getvalue()
        between = value[value.index("RAW_"):value.index("DONE")]
        self.assertEqual(between.count("\r\n"), 1)
        self.assertEqual(renderer._output_source, "task:bg1")

        renderer.finish_output_line()
        self.assertIsNone(renderer._output_source)


if __name__ == "__main__":
    unittest.main()
