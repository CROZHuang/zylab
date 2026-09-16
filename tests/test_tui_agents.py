"""M3c-3 agent workspace 所需的 TUI 纯状态回归测试。"""
import io
import os
import sys
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tui


class AgentLineEditorTests(unittest.TestCase):
    def test_ctrl_t_opens_agents_without_mutating_draft(self):
        editor = tui.LineEditor()
        editor.handle("草")

        event = editor.handle("ctrl-t")[0]

        self.assertEqual(event.kind, "agents")
        self.assertEqual(editor.text, "草")
        self.assertEqual(tui.KEYS["\x14"], "ctrl-t")

    def test_target_routes_enter_and_tab_and_disables_slash_menu(self):
        editor = tui.LineEditor(commands={"/help": "帮助"})
        editor.set_busy(True)
        editor.set_target("agent-1", history=["child old"])
        for char in "/help":
            editor.handle(char)

        snapshot = editor.snapshot()
        enter = editor.handle("enter")[0]
        for char in "follow up":
            editor.handle(char)
        tab = editor.handle("tab")[0]

        self.assertEqual(snapshot.target, "agent-1")
        self.assertEqual(snapshot.options, ())
        self.assertEqual(
            (enter.kind, enter.text, enter.mode),
            ("submit", "/help", "agent"))
        self.assertEqual(
            (tab.kind, tab.text, tab.mode),
            ("submit", "follow up", "agent"))

    def test_target_slash_command_is_kept_in_main_history_only(self):
        editor = tui.LineEditor(history=["main old"])
        editor.set_target("agent-1", history=["child old"])
        for char in "/agents detach":
            editor.handle(char)
        submitted = editor.handle("enter")[0]

        editor.set_target("agent-1")
        child_recalled = editor.handle("up")[0].snapshot.text
        editor.replace("")
        editor.set_target(None)
        main_recalled = editor.handle("up")[0].snapshot.text

        self.assertEqual(submitted.mode, "agent")
        self.assertEqual(child_recalled, "child old")
        self.assertEqual(main_recalled, "/agents detach")

    def test_each_target_has_independent_history_even_while_main_busy(self):
        editor = tui.LineEditor(history=["main old"])
        editor.set_busy(True)
        editor.set_target("agent-a", history=["a old"])
        for char in "a new":
            editor.handle(char)
        editor.handle("enter")
        editor.set_target("agent-b", history=["b old"])

        b_history = editor.handle("up")[0].snapshot.text
        editor.replace("")
        editor.set_target("agent-a")
        a_history = editor.handle("up")[0].snapshot.text
        editor.replace("")
        editor.set_target(None)
        editor.set_busy(False)
        main_history = editor.handle("up")[0]

        self.assertEqual(b_history, "b old")
        self.assertEqual(a_history, "a new")
        self.assertEqual(main_history.snapshot.text, "main old")

    def test_target_escape_ctrl_c_and_left_edge_detach_without_data_loss(self):
        for key in ("esc", "ctrl-c"):
            with self.subTest(key=key):
                editor = tui.LineEditor()
                editor.set_target("agent-1")
                for char in "draft":
                    editor.handle(char)
                event = editor.handle(key)[0]
                self.assertEqual(
                    (event.kind, event.text, event.mode),
                    ("detach", "draft", "agent"))
                self.assertEqual(editor.text, "draft")

        editor = tui.LineEditor()
        editor.set_target("agent-1")
        editor.handle("x")
        moved = editor.handle("left")[0]
        detached = editor.handle("left")[0]
        self.assertEqual(moved.kind, "redraw")
        self.assertEqual(detached.kind, "detach")
        self.assertEqual(detached.text, "x")


class AgentInputPumpTests(unittest.TestCase):
    def test_configure_atomically_sets_target_history_and_frame(self):
        pump = tui.InputPump(stream=io.StringIO(), history=["main old"])

        snapshot = pump.configure(
            busy=True, prompt="agent agent-1› ", target="agent-1",
            target_history=["child old"], status="child running",
            queued=("child queued",), publish=False)
        recalled = pump.editor.handle("up")[0]

        self.assertIsNone(pump.get(timeout=0.001))
        self.assertEqual(snapshot.target, "agent-1")
        self.assertEqual(snapshot.prompt, "agent agent-1› ")
        self.assertEqual(snapshot.status, "child running")
        self.assertEqual(snapshot.queued, ("child queued",))
        self.assertEqual(recalled.kind, "redraw")
        self.assertEqual(recalled.snapshot.text, "child old")

    def test_picker_actions_return_modes_and_custom_controls(self):
        pump = tui.InputPump(stream=io.StringIO())
        controls = "↑↓ 选择 · Enter attach · Space peek · Esc 取消"
        pump.open_picker(
            ["first", "second"], allow_filter=False,
            actions={"enter": "attach", "space": "peek"},
            controls=controls)

        snapshot = pump.snapshot()
        peek = pump._handle_picker(" ")[0]

        self.assertEqual(snapshot.controls, controls)
        self.assertEqual((peek.kind, peek.value, peek.mode),
                         ("picker_result", "first", "peek"))

        pump.open_picker(
            ["first", "second"], allow_filter=False,
            actions={"enter": "attach"})
        pump._handle_picker("down")
        attach = pump._handle_picker("enter")[0]
        self.assertEqual((attach.value, attach.mode), ("second", "attach"))

    def test_picker_default_result_remains_backward_compatible(self):
        pump = tui.InputPump(stream=io.StringIO())
        pump.open_picker(["only"], allow_filter=False)

        selected = pump._handle_picker("enter")[0]

        self.assertEqual(
            (selected.kind, selected.value, selected.mode),
            ("picker_result", "only", None))

    def test_renderer_uses_custom_picker_controls(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        renderer.render(tui.InputSnapshot(
            mode="picker", options=("agent",), selected=0,
            controls="Enter attach · Space peek", hint="1 项"))

        rendered = tui._ANSI.sub("", stream.getvalue())
        self.assertIn("\r\n  1 项\r\n", rendered)
        self.assertIn("Enter attach · Space peek", rendered)
        self.assertNotIn("Enter 确认", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
