"""M3b 单 stdin owner 的纯状态与真实 PTY 回归测试。"""
import contextlib
import base64
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import checkpoints, tools, tui
import zylab as CLI
from tests.pty_harness import PTYSend, run_pty_child


class LineEditorTests(unittest.TestCase):
    def test_generic_tool_preview_accepts_immutable_prepared_arguments(self):
        prepared = tools.PreparedArguments(
            "subagent", '{"task":"inspect"}')

        rendered = CLI.preview("subagent", prepared)

        self.assertIn("inspect", rendered)

    def test_both_approval_uis_consume_the_prepared_diff_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "example.txt"
            target.write_text("old\n", encoding="utf-8")
            write = checkpoints.prepare_write(
                "write_file", {"path": target, "content": "new\n"},
                cwd=root, workspace_root=root)
            prepared = tools.PreparedArguments(
                "write_file",
                json.dumps({"path": str(target), "content": "new\n"}),
                prepared_write=write)

            lines = CLI._approval_detail_lines("write_file", prepared)

        self.assertEqual(lines, write.preview_text.splitlines())
        self.assertTrue(any("| -old" in line for line in lines))
        self.assertTrue(any("| +new" in line for line in lines))

    def test_idle_enter_and_busy_enter_have_distinct_modes(self):
        editor = tui.LineEditor()
        editor.handle("a")
        idle = editor.handle("enter")[0]
        self.assertEqual((idle.kind, idle.text, idle.mode),
                         ("submit", "a", "next_turn"))

        editor.set_busy(True)
        editor.handle("b")
        busy = editor.handle("enter")[0]
        self.assertEqual((busy.kind, busy.text, busy.mode),
                         ("submit", "b", "steer"))

    def test_shift_enter_inserts_newline_and_plain_enter_submits(self):
        editor = tui.LineEditor()
        editor.handle("a")
        editor.handle("c")
        editor.handle("left")

        redraw = editor.handle("shift-enter")[0]

        self.assertEqual(redraw.kind, "redraw")
        self.assertEqual((editor.text, editor.cursor), ("a\nc", 2))
        submitted = editor.handle("enter")[0]
        self.assertEqual(
            (submitted.kind, submitted.text, submitted.mode),
            ("submit", "a\nc", "next_turn"))

    def test_busy_tab_queues_next_turn_and_up_retrieves(self):
        editor = tui.LineEditor()
        editor.set_busy(True)
        self.assertEqual(editor.handle("up")[0].kind, "retrieve")
        editor.handle("x")
        event = editor.handle("tab")[0]
        self.assertEqual((event.kind, event.mode, event.text),
                         ("submit", "next_turn", "x"))

    def test_busy_empty_and_post_submit_composer_remain_active(self):
        editor = tui.LineEditor(prompt="steer› ")

        empty = editor.set_busy(True)
        self.assertTrue(empty.active)
        self.assertEqual(empty.text, "")

        editor.handle("x")
        editor.handle("enter")
        self.assertTrue(editor.snapshot().active)
    def test_escape_cancels_busy_without_mutating_draft(self):
        editor = tui.LineEditor()
        editor.set_busy(True)
        editor.handle("草")
        event = editor.handle("esc")[0]
        self.assertEqual(event.kind, "cancel")
        self.assertEqual(editor.text, "草")

    def test_command_menu_completion_remains_idle_only(self):
        editor = tui.LineEditor(
            commands={"/help": "帮助", "/hooks": "hooks"})
        editor.handle("/")
        editor.handle("h")
        editor.handle("e")
        event = editor.handle("tab")[0]
        self.assertEqual(event.kind, "redraw")
        self.assertEqual(editor.text, "/help ")

    def test_second_level_menu_guides_and_preserves_goal_freeform_input(self):
        editor = tui.LineEditor(
            commands={"/goal": "目标"},
            subcommands={
                "/goal": {
                    "hint": "也可直接输入用户目标",
                    "items": (
                        ("status", "查看状态"),
                        ("set", "创建目标"),
                    ),
                },
            })
        for char in "/goal ":
            editor.handle(char)

        menu = editor.snapshot()
        self.assertEqual(
            [row[0] for row in menu.options],
            ["/goal status", "/goal set"])
        self.assertIn("二级指令", menu.hint)
        self.assertIn("直接输入用户目标", menu.hint)

        editor.replace("/goal fix the parser")
        self.assertEqual(editor.snapshot().options, ())
        submitted = editor.handle("enter")[0]
        self.assertEqual(
            (submitted.kind, submitted.text, submitted.mode),
            ("submit", "/goal fix the parser", "next_turn"))

    def test_second_level_completion_closes_after_argument_separator(self):
        editor = tui.LineEditor(
            commands={"/goal": "目标"},
            subcommands={
                "/goal": [("status", "查看状态"), ("set", "创建目标")],
            })
        editor.replace("/goal st")
        self.assertEqual(
            [row[0] for row in editor.snapshot().options],
            ["/goal status"])

        editor.handle("tab")
        self.assertEqual(editor.text, "/goal status ")
        self.assertEqual(editor.snapshot().options, ())
        submitted = editor.handle("enter")[0]
        self.assertEqual(submitted.text, "/goal status")

    def test_third_level_parameter_menu_has_descriptions_and_preserves_args(self):
        editor = tui.LineEditor(
            commands={"/workflow": "workflow"},
            subcommands={
                "/workflow": {
                    "items": (("auto", "自适应 workflow"),),
                    "params": {
                        "auto": {
                            "hint": "不会因补全而启动请求",
                            "items": (
                                ("on", "开启"),
                                ("off", "关闭"),
                                ("status", "查看状态"),
                            ),
                        },
                    },
                },
            })
        editor.replace("/workflow auto ")
        menu = editor.snapshot()
        self.assertEqual(
            [row[0] for row in menu.options],
            ["/workflow auto on", "/workflow auto off",
             "/workflow auto status"])
        self.assertIn("三级参数", menu.hint)
        self.assertIn("不会因补全而启动请求", menu.hint)
        self.assertEqual(menu.options[0][1], "开启")

        editor.replace("/workflow auto o")
        self.assertEqual(
            [row[0] for row in editor.snapshot().options],
            ["/workflow auto on", "/workflow auto off"])
        editor.handle("tab")
        self.assertEqual(editor.text, "/workflow auto on ")
        self.assertEqual(editor.snapshot().options, ())

        editor.replace("/workflow auto on --reason queued")
        self.assertEqual(editor.snapshot().options, ())
        submitted = editor.handle("enter")[0]
        self.assertEqual(submitted.text, "/workflow auto on --reason queued")

    def test_parameter_catalog_normalizes_flags_and_rejects_nested_depth(self):
        editor = tui.LineEditor(
            subcommands={
                "/goal": {
                    "items": (("set", "创建"),),
                    "params": {
                        "set": {
                            "hint": "限制轮数",
                            "items": (("--rounds", "最大轮数"),),
                            "params": {
                                "--rounds": {
                                    "items": (("10", "非法的第三层"),),
                                },
                            },
                        },
                    },
                },
            })
        editor.replace("/goal set ")
        self.assertEqual(editor.snapshot().options,
                         (("/goal set --rounds", "最大轮数"),))
        editor.handle("tab")
        self.assertEqual(editor.text, "/goal set --rounds ")
        self.assertEqual(editor.snapshot().options, ())

    def test_builtin_catalog_exposes_guidance_for_command_families(self):
        import zylab as cli

        self.assertIn("/goal", cli.COMMAND_SUBCOMMANDS)
        self.assertNotIn("/mouse", cli.COMMANDS)
        self.assertNotIn("/mouse", cli.COMMAND_SUBCOMMANDS)
        self.assertEqual(cli.resolve_command("mouse")[0], "mouse")
        self.assertIn("/workflow", cli.COMMAND_SUBCOMMANDS)
        self.assertIn("/memory", cli.COMMAND_SUBCOMMANDS)
        for command, spec in cli.COMMAND_SUBCOMMANDS.items():
            with self.subTest(command=command):
                self.assertTrue(spec["items"])
                self.assertTrue(all(
                    isinstance(name, str) and isinstance(description, str)
                    for name, description in spec["items"]))
        self.assertIn("auto", cli.COMMAND_SUBCOMMANDS["/workflow"]["params"])
        self.assertEqual(
            [name for name, _ in cli.COMMAND_SUBCOMMANDS["/workflow"]
             .get("params", {}).get("auto", {}).get("items", ())],
            ["on", "off", "status", "enable", "disable"])
        self.assertEqual(
            [name for name, _ in cli.COMMAND_SUBCOMMANDS["/goal"]
             .get("params", {}).get("set", {}).get("items", ())],
            ["--rounds", "--max-rounds"])
        self.assertIn("use", cli.COMMAND_SUBCOMMANDS["/memory"]["params"])

    def test_exact_command_enter_submits_without_second_enter(self):
        editor = tui.LineEditor(commands={"/exit": "退出"})
        for char in "/exit":
            editor.handle(char)
        event = editor.handle("enter")[0]
        self.assertEqual(
            (event.kind, event.text, event.mode),
            ("submit", "/exit", "next_turn"))


    def test_history_up_browses_past_recalled_slash_command(self):
        editor = tui.LineEditor(
            commands={"/help": "帮助", "/hooks": "hooks"},
            history=["oldest prompt", "/help", "latest prompt"])

        snapshots = [editor.handle("up")[0].snapshot for _ in range(3)]

        self.assertEqual(
            [snapshot.text for snapshot in snapshots],
            ["latest prompt", "/help", "oldest prompt"])
        self.assertTrue(all(not snapshot.options for snapshot in snapshots))

    def test_editing_recalled_slash_command_reopens_completion(self):
        editor = tui.LineEditor(
            commands={"/help": "帮助"}, history=["/hel"])

        recalled = editor.handle("up")[0].snapshot
        edited = editor.handle("p")[0].snapshot

        self.assertEqual(recalled.options, ())
        self.assertEqual(
            [command for command, _ in edited.options], ["/help"])


class InputNavigationTests(unittest.TestCase):
    def test_modified_enter_decoder_covers_modern_terminal_forms(self):
        sequences = (
            "\x1b[13;2u",          # Kitty / CSI-u
            "\x1b[13;2:1u",        # Kitty explicit press event
            "\x1b[27;2;13~",       # xterm modifyOtherKeys
            "\x1b[13;2~",          # CSI modified-key variant
            "\x1b\n",              # VS Code sendSequence fallback
            "\x1b\r",
        )

        self.assertEqual(
            [tui._decode_modified_key(value) for value in sequences],
            ["shift-enter"] * len(sequences))
        self.assertEqual(tui._decode_modified_key("\x1b[13u"), "enter")
        self.assertEqual(
            tui._decode_modified_key("\x1b[13;2:3u"), "ignore")

    def test_sgr_mouse_decoder_accepts_wheel_click_and_rejects_malformed(self):
        wheel = tui._decode_sgr_mouse("\x1b[<64;12;7M")
        click = tui._decode_sgr_mouse("\x1b[<0;4;1M")
        motion = tui._decode_sgr_mouse("\x1b[<32;9;4M")
        release = tui._decode_sgr_mouse("\x1b[<0;9;4m")

        self.assertEqual(
            (wheel.kind, wheel.x, wheel.y), ("scroll_up", 12, 7))
        self.assertEqual(
            (click.kind, click.button, click.x, click.y),
            ("press", 0, 4, 1))
        self.assertEqual(
            (motion.kind, motion.button, motion.x, motion.y),
            ("motion", 0, 9, 4))
        self.assertEqual(
            (release.kind, release.button, release.x, release.y),
            ("release", 0, 9, 4))
        self.assertIsNone(tui._decode_sgr_mouse("\x1b[<64;0;7M"))
        self.assertIsNone(tui._decode_sgr_mouse("\x1b[<badM"))

    def test_cursor_position_decoder_and_main_dock_click_routing(self):
        position = tui._decode_cursor_position("\x1b[20;8R")
        self.assertEqual((position.x, position.y), (8, 20))
        self.assertIsNone(tui._decode_cursor_position("\x1b[0;8R"))

        pump = tui.InputPump()
        pump.configure(dock=(
            tui.DockItem("● main"),
            tui.DockItem(
                "○ Explore inspect parser",
                event="agent_attach", value="a-child0001"),
        ), publish=False)
        pump._handle_line(tui.CursorPosition(x=4, y=20))
        with mock.patch.object(tui, "_cols", return_value=80):
            events = pump._handle_line(tui.MouseEvent(
                "press", x=12, y=18, button=0))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "agent_attach")
        self.assertEqual(events[0].value, "a-child0001")

    def test_cursor_position_wait_closes_dsr_scheduling_gap(self):
        pump = tui.InputPump()

        def publish_position():
            time.sleep(0.01)
            pump._handle_line(tui.CursorPosition(x=1, y=17))

        thread = threading.Thread(target=publish_position)
        thread.start()
        self.assertEqual(pump.wait_cursor_screen_row(0.2), 17)
        thread.join()

        pump.configure(prompt="steer> ", publish=False)
        self.assertIsNone(pump.cursor_screen_row)


    def test_history_navigation_preserves_draft_and_returns_to_editing(self):
        pump = tui.InputPump()
        pump.editor.replace("草稿")

        opened = pump._handle_line("pageup")
        closed = pump._handle_line("esc")
        pump._handle_line("pageup")
        resumed = pump._handle_line("x")

        self.assertEqual(opened[0].kind, "history_scroll")
        self.assertEqual(closed[0].kind, "history_close")
        self.assertEqual(
            [event.kind for event in resumed], ["history_close", "redraw"])
        self.assertEqual(pump.editor.text, "草稿x")

    def test_mouse_wheel_enters_history_and_click_is_routed(self):
        pump = tui.InputPump()

        scrolled = pump._handle_line(tui.MouseEvent(
            "scroll_up", x=8, y=5, button=0))
        clicked = pump._handle_line(tui.MouseEvent(
            "press", x=8, y=1, button=0))

        self.assertEqual(scrolled[0].kind, "history_scroll")
        self.assertEqual(clicked[0].kind, "history_click")

        self.assertEqual(
            pump._handle_line(tui.MouseEvent(
                "motion", x=8, y=2, button=0))[0].kind,
            "history_mouse")
        self.assertEqual(
            pump._handle_line(tui.MouseEvent(
                "release", x=8, y=2, button=0))[0].kind,
            "history_mouse")
        self.assertEqual(
            pump._handle_line("ctrl-c")[0].kind, "history_copy")

    def test_live_composer_mouse_and_copy_shortcuts_are_routed(self):
        pump = tui.InputPump()

        press = pump._handle_line(tui.MouseEvent(
            "press", x=8, y=5, button=0))
        motion = pump._handle_line(tui.MouseEvent(
            "motion", x=9, y=5, button=0))
        release = pump._handle_line(tui.MouseEvent(
            "release", x=9, y=5, button=0))

        self.assertEqual(press[0].kind, "composer_mouse")
        self.assertEqual(motion[0].kind, "composer_mouse")
        self.assertEqual(release[0].kind, "composer_mouse")

        pump.set_composer_selection_active(True)
        self.assertEqual(
            pump._handle_line("ctrl-c")[0].kind, "composer_copy")
        pump.set_composer_selection_active(True)
        self.assertEqual(
            pump._handle_line("esc")[0].kind, "composer_clear_selection")

    def test_shift_drag_in_live_composer_is_left_to_terminal(self):
        pump = tui.InputPump()
        events = (
            tui.MouseEvent("press", x=3, y=2, button=0, modifiers=4),
            tui.MouseEvent("motion", x=7, y=2, button=0, modifiers=4),
            tui.MouseEvent("release", x=7, y=2, button=0, modifiers=4),
        )

        for event in events:
            with self.subTest(kind=event.kind):
                self.assertEqual(pump._handle_line(event), [])

    def test_session_composer_event_handler_updates_cursor_and_selection_flag(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        pump = tui.InputPump()
        pump.editor.replace("hello")
        pump._handle_line(tui.CursorPosition(x=1, y=2))
        session = CLI.Session.__new__(CLI.Session)
        session.pump = pump
        session.renderer = renderer

        with mock.patch.object(tui, "_cols", return_value=52):
            session.handle_composer_selection(tui.PumpEvent(
                "composer_mouse",
                value=tui.MouseEvent("press", x=3, y=2, button=0)))
            session.handle_composer_selection(tui.PumpEvent(
                "composer_mouse",
                value=tui.MouseEvent("motion", x=6, y=2, button=0)))
            session.handle_composer_selection(tui.PumpEvent(
                "composer_mouse",
                value=tui.MouseEvent("release", x=6, y=2, button=0)))

        self.assertEqual(renderer._composer_selection_text(), "he")
        self.assertTrue(pump._composer_selection_active)
        self.assertEqual(pump._handle_line("ctrl-c")[0].kind,
                         "composer_copy")

    def test_session_composer_handler_waits_for_missing_dsr_row(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        pump = tui.InputPump()
        pump.editor.replace("hello")
        session = CLI.Session.__new__(CLI.Session)
        session.pump = pump
        session.renderer = renderer

        with (mock.patch.object(tui, "_cols", return_value=52),
              mock.patch.object(
                  pump, "wait_cursor_screen_row", return_value=2) as wait):
            result = session.handle_composer_selection(tui.PumpEvent(
                "composer_mouse",
                value=tui.MouseEvent("press", x=3, y=2, button=0)))

        self.assertTrue(result)
        wait.assert_called_once_with()
        self.assertTrue(renderer._composer_selection_dragging)

    def test_shift_mouse_events_are_left_to_terminal_native_selection(self):
        pump = tui.InputPump()
        pump.open_picker(["row"], fullscreen=True)

        press = tui.MouseEvent(
            "press", x=8, y=5, button=0, modifiers=4)
        self.assertEqual(pump._handle_line(press), [])
        self.assertEqual(pump._handle_picker(press), [])

        # Shift is only a selection modifier for button gestures; wheel
        # navigation remains available even if the key is held.
        scroll = tui.MouseEvent(
            "scroll_up", x=8, y=5, button=0, modifiers=4)
        self.assertEqual(
            pump._handle_line(scroll)[0].kind, "history_scroll")
        self.assertEqual(
            pump._handle_picker(scroll)[0].kind, "redraw")

    def test_fullscreen_picker_click_selects_visible_row_and_action(self):
        pump = tui.InputPump()
        with (
                mock.patch.object(tui, "_cols", return_value=80),
                mock.patch.object(tui, "_lines", return_value=24)):
            pump.open_picker(
                ["first", "second", "third"],
                title="Agent workspace", allow_filter=False,
                fullscreen=True, mouse_action="attach")
            events = pump._handle_picker(tui.MouseEvent(
                "press", x=5, y=3, button=0))

        result = next(
            event for event in events if event.kind == "picker_result")
        self.assertEqual((result.value, result.mode), ("second", "attach"))

    def test_composer_dock_survives_unrelated_state_updates(self):
        pump = tui.InputPump()
        rows = ("workflow header", "DAG agents → review → synthesis")

        initial = pump.configure(dock=rows, publish=False)
        busy = pump.configure(busy=True, activity="工作中", publish=False)

        self.assertEqual(initial.dock, rows)
        self.assertEqual(busy.dock, rows)
        self.assertEqual(busy.activity, "工作中")


class StreamingMarkdownTests(unittest.TestCase):
    SAMPLE = (
        "正文 **粗体** 与 `inline`\n"
        "- 中文列表\n"
        "2. second\n"
        "```python\n"
        "print('你好')\n"
        "```\n"
    )
    RICH_SAMPLE = (
        "# 阅读体验\n"
        "> 引用 **重点** 与 *斜体*\n"
        "- [x] 已完成\n"
        "  - 嵌套列表包含一段会在窄终端自动换行的中文说明\n"
        "---\n"
        "| 名称 | 值 |\n"
        "|:---|---:|\n"
        "| 中文 | 42 |\n"
        "```python\n"
        "def hello(name: str):\n"
        "    # comment\n"
        "    return f\"hi {name}\"\n"
        "```\n"
        "访问 [OpenAI](https://openai.com) 或看 ~~旧说明~~。\n"
    )

    @staticmethod
    def render(chunks, *, columns=40, styled=True):
        renderer = tui.StreamingMarkdown(
            columns=columns, styled=styled, hyperlinks=False)
        return "".join(renderer.feed(chunk) for chunk in chunks) \
            + renderer.finish()

    def test_plain_text_streams_without_waiting_for_newline(self):
        renderer = tui.StreamingMarkdown(styled=False)

        self.assertEqual(renderer.feed("普通正文"), "普通正文")
        self.assertEqual(renderer.finish(), "")

    def test_fence_split_across_chunks_matches_single_feed(self):
        split = [
            "正文 **粗", "体** 与 `inline`\n- 中文", "列表\n2.",
            " second\n``", "`py", "thon\nprint('", "你好')\n`", "``\n",
        ]

        whole = self.render([self.SAMPLE])
        fragmented = self.render(split)
        characterwise = self.render(list(self.SAMPLE))

        visible = lambda value: tui._ANSI.sub("", value)
        self.assertEqual(visible(fragmented), visible(whole))
        self.assertEqual(visible(characterwise), visible(whole))

    def test_unlabelled_fences_accept_empty_or_whitespace_info(self):
        samples = (
            "```\nplain\n```\n",
            "```   \nplain\n```\n",
            "~~~\nplain\n~~~\n",
        )

        for sample in samples:
            with self.subTest(opening=sample.splitlines()[0]):
                whole = self.render([sample], styled=False)
                characterwise = self.render(list(sample), styled=False)

                self.assertEqual(whole, "  ┌─\n  │ plain\n  └─\n")
                self.assertEqual(characterwise, whole)

    def test_active_style_is_reset_and_resumed_between_flushes(self):
        bold = tui.StreamingMarkdown(styled=True)
        first = bold.feed("**粗")
        second = bold.feed("体**")

        self.assertTrue(first.endswith(tui.StreamingMarkdown.RESET))
        self.assertTrue(second.startswith(tui.StreamingMarkdown.BOLD))

        code = tui.StreamingMarkdown(styled=True)
        opening = code.feed("```py\npri")
        continuation = code.feed("nt('x')\n```\n")

        # Code waits for one physical line so lexical tokens are not colored
        # differently merely because a provider split the chunk mid-token.
        self.assertTrue(
            opening.endswith(tui.StreamingMarkdown.RESET + "\n"))
        self.assertIn(tui.StreamingMarkdown.CODE, continuation)
        self.assertIn(tui.StreamingMarkdown.FENCE, continuation)

    def test_minimal_markdown_is_visually_distinct(self):
        rendered = self.render([self.SAMPLE])
        plain = tui._ANSI.sub("", rendered)

        self.assertNotIn("```", plain)
        self.assertIn("正文 粗体 与 inline", plain)
        self.assertIn("  • 中文列表", plain)
        self.assertIn("  2. second", plain)
        self.assertIn("  ┌─ python", plain)
        self.assertIn("  │ print('你好')", plain)
        self.assertIn("  └─", plain)
        self.assertIn(tui.StreamingMarkdown.BOLD, rendered)
        self.assertIn(tui.StreamingMarkdown.INLINE_CODE, rendered)

    def test_fence_header_uses_terminal_display_columns(self):
        rendered = self.render(
            ["```超长中文语言标签abcdef\nx\n```\n"], columns=12)
        header = rendered.splitlines()[0]

        self.assertLessEqual(tui.display_width(header), 12)
        self.assertIn("…", tui._ANSI.sub("", header))

    def test_closing_fence_may_be_longer_but_not_shorter(self):
        longer = self.render(["```py\nx\n````\nafter\n"], styled=False)
        shorter = self.render(["````py\nx\n```\nafter\n"], styled=False)

        self.assertIn("  └─\nafter", longer)
        self.assertNotIn("  └─\nafter", shorter)
        self.assertIn("  │ ```", shorter)

    def test_non_tty_turn_preserves_raw_markdown(self):
        class Agent:
            compact_at = 70_000
            tokens_in = 0
            tokens_out = 0

            @staticmethod
            def run(*_args, **_kwargs):
                yield {"t": "text", "v": "```py\nx\n```\n"}
                yield {"t": "end"}

        session = CLI.Session.__new__(CLI.Session)
        session.ag = Agent()
        session.plan_mode = False
        session.heartbeat_lease = lambda: None
        stream = io.StringIO()

        with mock.patch.object(CLI.sys, "stdout", stream), \
             mock.patch.object(CLI, "_TTY", False), \
             mock.patch.object(CLI, "status_line", return_value="status"):
            session.turn("prompt")

        self.assertIn("```py\nx\n```", stream.getvalue())
        self.assertNotIn("  ┌─ py", stream.getvalue())

    def test_rich_blocks_are_distinct_width_safe_and_highlighted(self):
        rendered = self.render(
            [self.RICH_SAMPLE], columns=48, styled=True)
        plain = tui._ANSI.sub("", rendered)
        theme = tui.markdown_theme("dark")

        self.assertIn("  ◆ 阅读体验", plain)
        self.assertIn("  │ 引用 重点 与 斜体", plain)
        self.assertIn("  ☑ 已完成", plain)
        self.assertIn("    ◦ 嵌套列表", plain)
        self.assertIn("  ┌", plain)
        self.assertIn("│名称", plain)
        self.assertIn("│中文", plain)
        self.assertIn("  ┌─ python", plain)
        self.assertIn("  │ def hello", plain)
        self.assertIn("OpenAI (https://openai.com)", plain)
        self.assertIn("旧说明", plain)
        self.assertNotIn("```python", plain)
        self.assertNotIn("|:---|", plain)
        self.assertTrue(all(
            tui.display_width(line) <= 48
            for line in rendered.splitlines()))
        self.assertIn(theme.keyword, rendered)
        self.assertIn(theme.string, rendered)
        self.assertIn(theme.comment, rendered)
        self.assertIn(tui.StreamingMarkdown.ITALIC, rendered)
        self.assertIn(tui.StreamingMarkdown.STRIKE, rendered)

    def test_rich_markdown_is_invariant_to_every_character_chunking(self):
        whole = self.render([self.RICH_SAMPLE], columns=52)
        fragmented = self.render(list(self.RICH_SAMPLE), columns=52)

        self.assertEqual(
            tui._ANSI.sub("", fragmented),
            tui._ANSI.sub("", whole))

    def test_split_terminal_control_sequences_cannot_reprogram_tty(self):
        renderer = tui.StreamingMarkdown(styled=False)
        chunks = [
            "safe\x1b[", "2Jbad\x1b]0;TI", "TLE\x1b",
            "\\tail\r", "rewritten\u202e\n",
            "\x1bPsecret\x1b", "\\done",
        ]
        rendered = "".join(renderer.feed(chunk) for chunk in chunks)
        rendered += renderer.finish()

        self.assertEqual(rendered, "safebadtail\nrewritten\ndone")
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("TITLE", rendered)
        self.assertNotIn("secret", rendered)

    def test_safe_links_use_osc8_and_unsafe_links_remain_visible(self):
        safe = tui.StreamingMarkdown(
            styled=True, hyperlinks=True, columns=80)
        safe_output = safe.feed(
            "[OpenAI](https://openai.com)\n") + safe.finish()

        self.assertIn("\x1b]8;;https://openai.com\x1b\\", safe_output)
        self.assertEqual(tui._ANSI.sub("", safe_output), "OpenAI\n")
        self.assertEqual(tui.display_width(safe_output.splitlines()[0]), 6)

        unsafe = tui.StreamingMarkdown(
            styled=True, hyperlinks=True, columns=80)
        unsafe_output = unsafe.feed(
            "[bad](javascript:alert(1)) "
            "[broken](http://[)\n") + unsafe.finish()
        unsafe_plain = tui._ANSI.sub("", unsafe_output)

        self.assertNotIn("\x1b]8;;javascript:", unsafe_output)
        self.assertIn("javascript:alert(1)", unsafe_plain)
        self.assertIn("http://[", unsafe_plain)

    def test_no_color_dumb_terminal_and_theme_selection(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        stream = TTY()
        self.assertFalse(tui.terminal_style_enabled(
            stream, {"TERM": "xterm-256color", "NO_COLOR": "1"}))
        self.assertFalse(tui.terminal_style_enabled(
            stream, {"TERM": "dumb"}))
        self.assertTrue(tui.terminal_style_enabled(
            stream, {"TERM": "xterm-256color"}))

        dark = tui.StreamingMarkdown(styled=True, theme="dark")
        light = tui.StreamingMarkdown(styled=True, theme="light")
        dark_output = dark.feed("# title\n") + dark.finish()
        light_output = light.feed("# title\n") + light.finish()
        self.assertNotEqual(dark_output, light_output)
        self.assertEqual(
            tui._ANSI.sub("", dark_output),
            tui._ANSI.sub("", light_output))

    def test_unicode_tabs_ansi_and_emoji_have_stable_display_width(self):
        self.assertEqual(tui.display_width("中文"), 4)
        self.assertEqual(tui.display_width("A\tB"), 9)
        self.assertEqual(tui.display_width("👩‍💻"), 2)
        self.assertEqual(tui.display_width("🇨🇳"), 2)

        value = "\x1b[31m中文abc\x1b[0m"
        truncated = tui.truncate_display(value, 4)
        self.assertLessEqual(tui.display_width(truncated), 4)
        self.assertIn("…", tui._ANSI.sub("", truncated))
        self.assertTrue(truncated.endswith("\x1b[0m"))

    def test_display_cell_padding_aligns_cjk_and_ansi(self):
        values = (
            tui.pad_display("智谱", 8),
            tui.pad_display("\x1b[31m智谱\x1b[0m", 8),
            tui.pad_display("42", 6, align="right"),
        )

        self.assertEqual([tui.display_width(value) for value in values],
                         [8, 8, 6])
        self.assertEqual(tui._ANSI.sub("", values[0]), "智谱    ")
        self.assertEqual(tui._ANSI.sub("", values[2]), "    42")

    def test_table_layout_hides_optional_column_before_shrinking_keys(self):
        columns = (
            tui.TableColumn(8, min_width=4),
            tui.TableColumn(
                8, min_width=4, priority=2, optional=True),
            tui.TableColumn(6, min_width=6, align="right"),
        )

        wide = tui.TableLayout.fit(columns, 26)
        narrow = tui.TableLayout.fit(columns, 16)
        row = narrow.row(("智谱", "latency", "42"))

        self.assertEqual(wide.widths, (8, 8, 6))
        self.assertEqual(narrow.widths, (8, 0, 6))
        self.assertEqual(tui.display_width(row), 16)
        self.assertNotIn("latency", row)

    def test_many_column_table_and_structural_wrap_stay_in_viewport(self):
        table = (
            "| a | b | c | d | e | f | g | h | i | j |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |\n"
        )
        wrapped = (
            "> 这是一段很长的中文引用，需要在非常窄的终端中继续显示引用边栏\n"
            "- 这是一段很长的列表说明，需要让续行与正文保持清楚的层次\n"
        )
        rendered = self.render([table + wrapped], columns=20)
        plain = tui._ANSI.sub("", rendered)

        self.assertIn("…", plain)
        self.assertGreaterEqual(plain.count("  │ "), 2)
        self.assertTrue(all(
            tui.display_width(line) <= 20
            for line in rendered.splitlines()))

    def test_finish_closes_unterminated_fence_and_table_frames(self):
        fence = self.render(["```python\nprint(1)"], styled=False)
        table = self.render([
            "| key | value |\n|---|---:|\n| x | 1 |"
        ], styled=False)

        self.assertIn("  │ print(1)\n  └─", fence)
        self.assertIn("└", table)
        self.assertTrue(table.rstrip().endswith("┘"))

    def test_structural_prefix_buffer_is_bounded(self):
        renderer = tui.StreamingMarkdown(styled=False)
        output = renderer.feed("# " + "x" * 70_000)

        self.assertGreater(len(output), 69_000)
        self.assertEqual(renderer._line_buffer, "")

        code = tui.StreamingMarkdown(styled=False)
        code_output = code.feed("```text\n" + "x" * 20_000)
        self.assertGreater(len(code_output), 16_000)
        self.assertLess(len(code._line_buffer), code.CODE_BUFFER_LIMIT)

    def test_heading_keeps_hash_that_is_part_of_text(self):
        rendered = self.render(["# C#\n# closed ###\n"], styled=False)

        self.assertIn("  ◆ C#", rendered)
        self.assertIn("  ◆ closed", rendered)
        self.assertNotIn("closed ###", rendered)

    def test_unstyled_rich_output_contains_no_escape_sequences(self):
        rendered = self.render(
            [self.RICH_SAMPLE], columns=48, styled=False)

        self.assertNotIn("\x1b", rendered)
        self.assertIn("  ◆ 阅读体验", rendered)
        self.assertIn("  ┌─ python", rendered)


class InputPumpFrameTests(unittest.TestCase):
    def test_subcommand_options_render_with_guidance_and_frame_metadata(self):
        pump = tui.InputPump(
            stream=io.StringIO(),
            commands={"/goal": "目标"},
            subcommands={
                "/goal": {
                    "hint": "也可直接输入用户目标",
                    "items": (("status", "查看 phase 与 progress"),),
                },
            })
        pump.editor.replace("/goal ")
        snapshot = pump._decorate_snapshot(pump.editor.snapshot())
        lines, _, _ = tui.TerminalRenderer(io.StringIO())._build_frame(
            snapshot, 96)
        rendered = "\n".join(lines)
        self.assertIn("/goal status", rendered)
        self.assertIn("查看 phase 与 progress", rendered)
        self.assertIn("二级指令", rendered)
        self.assertIn("直接输入用户目标", rendered)

    def test_third_level_options_render_with_parameter_descriptions(self):
        pump = tui.InputPump(
            stream=io.StringIO(),
            subcommands={
                "/workflow": {
                    "items": (("auto", "自适应"),),
                    "params": {
                        "auto": {
                            "hint": "当前 chat",
                            "items": (("on", "开启"), ("off", "关闭")),
                        },
                    },
                },
            })
        pump.editor.replace("/workflow auto ")
        snapshot = pump._decorate_snapshot(pump.editor.snapshot())
        lines, _, _ = tui.TerminalRenderer(io.StringIO())._build_frame(
            snapshot, 96)
        rendered = "\n".join(lines)
        self.assertIn("/workflow auto on", rendered)
        self.assertIn("开启", rendered)
        self.assertIn("三级参数", rendered)
        self.assertIn("当前 chat", rendered)

    def test_guided_option_rows_fit_cjk_and_long_paths_at_narrow_widths(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        snapshot = tui.InputSnapshot(
            mode="line", prompt="› ", text="/workflow ", cursor=10,
            options=(
                ("/workflow research", "只读研究模式 · 后接一个很长的目标说明"),
                ("/workflow standard", "标准协作 preset"),
                ("/workflow auto", "切换自适应 workflow"),
            ), selected=1, hint="二级指令 · ↑↓ 选择 · Enter/Tab 补全")
        for styled in (False, True):
            stream = TTY() if styled else io.StringIO()
            with mock.patch.dict(
                    os.environ,
                    {"TERM": "xterm-256color"} if styled else {"NO_COLOR": "1"},
                    clear=True):
                renderer = tui.TerminalRenderer(stream)
                if styled:
                    self.assertTrue(renderer.styled)
                lines, _, _ = renderer._build_frame(snapshot, 20)
            self.assertTrue(all(
                tui.display_width(line) <= 20 for line in lines))
            if not styled:
                self.assertTrue(all("\x1b" not in line for line in lines))

    def test_guidance_refresh_can_preserve_selected_row_without_reset(self):
        pump = tui.InputPump(stream=io.StringIO())
        pump.editor.replace("/workflow auto ")
        pump.editor.menu_index = 1
        pump.set_subcommands({
            "/workflow": {
                "items": (("auto", "auto"),),
                "params": {"auto": {
                    "items": (("on", "on"), ("off", "off"), ("status", "status")),
                }},
            },
        }, publish=False, reset_menu=False)
        self.assertEqual(pump.editor.menu_index, 1)
        self.assertEqual(
            pump.editor.snapshot().options[1][0], "/workflow auto off")

    def test_frame_metadata_survives_editor_redraw(self):
        pump = tui.InputPump(stream=io.StringIO())

        configured = pump.configure(
            busy=True,
            queued=("↪ steer  first", "＋ next  second"),
            status="chat demo · model@gateway · ctx 4K/70K",
            activity="工作中 2s", publish=False)
        self.assertIsNone(pump.get(timeout=0.001))
        raw_event = pump.editor.handle("x")[0]
        event = pump._decorate_event(raw_event)

        self.assertTrue(configured.active)
        self.assertEqual(event.snapshot.text, "x")
        self.assertEqual(event.snapshot.queued, configured.queued)
        self.assertEqual(event.snapshot.status, configured.status)
        self.assertEqual(event.snapshot.activity, configured.activity)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self._previous_hook_cfg = CLI.tools.HOOK_CTX.get("cfg")

    def tearDown(self):
        CLI.tools.HOOK_CTX["cfg"] = self._previous_hook_cfg

    @staticmethod
    def session(permission="ask"):
        class Agent:
            last_total = 0

        cfg = json.loads(json.dumps(CLI.CFG.DEFAULTS))
        cfg["permissions"]["bash"] = permission
        return CLI.Session(Agent(), cfg)

    def test_noninteractive_ask_is_fail_closed_but_explicit_modes_work(self):
        sess = self.session("ask")
        with mock.patch.object(CLI.sys.stdin, "isatty", return_value=False):
            decision = sess.permission_decision("bash", {"command": "true"})
            self.assertFalse(sess.confirm("bash", {"command": "true"}))
            self.assertFalse(decision["allowed"])
            self.assertEqual(decision["decision"], "noninteractive_deny")
            self.assertEqual(decision["source"], "safety_default")

            sess.auto = True
            self.assertTrue(sess.confirm("bash", {"command": "true"}))
            sess.auto = False
            sess.cfg["permissions"]["bash"] = "allow"
            self.assertTrue(sess.confirm("bash", {"command": "true"}))
            sess.cfg["permissions"]["bash"] = "deny"
            sess.auto = True
            self.assertFalse(sess.confirm("bash", {"command": "true"}))

    @staticmethod
    def workspace_risk_prepared():
        risk = tools.WorkspaceRisk(
            ("git_reset_hard",),
            ("git reset --hard 会丢弃未提交修改",),
            (
                "git status --porcelain=v1 --untracked-files=all:",
                " M tracked.py",
            ))
        return tools.PreparedArguments(
            "bash", '{"command":"git reset --hard"}',
            workspace_risk=risk)

    def test_workspace_risk_auto_still_prompts_and_binds_once(self):
        sess = self.session("ask")
        sess.auto = True
        sess.pump = object()
        seen = []

        def pick(options, **_kwargs):
            seen.append(list(options))
            return options[1]

        sess.pick = pick
        out = io.StringIO()
        with mock.patch.object(
                CLI.sys.stdin, "isatty", return_value=True), \
             contextlib.redirect_stdout(out):
            decision = sess.permission_decision(
                "bash", self.workspace_risk_prepared())

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["decision"], "workspace_risk_once")
        self.assertTrue(
            decision["prepared"].workspace_risk_approved)
        self.assertEqual(seen[0][0][0], "deny")
        self.assertNotIn("always", [row[0] for row in seen[0]])
        self.assertIn("tracked.py", out.getvalue())
        self.assertIn("不会被 /auto", out.getvalue())

    def test_workspace_risk_picker_escape_defaults_to_deny(self):
        sess = self.session("allow")
        sess.auto = True
        sess.pump = object()
        seen = []

        def escape(options, **_kwargs):
            seen.append(list(options))
            return None

        sess.pick = escape
        with mock.patch.object(
                CLI.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.print"):
            decision = sess.permission_decision(
                "bash", self.workspace_risk_prepared())

        self.assertFalse(decision["allowed"])
        self.assertEqual(
            decision["decision"], "workspace_risk_denied")
        self.assertEqual(seen[0][0][0], "deny")

    def test_workspace_risk_noninteractive_auto_is_fail_closed(self):
        sess = self.session("allow")
        sess.auto = True
        with mock.patch.object(
                CLI.sys.stdin, "isatty", return_value=False):
            decision = sess.permission_decision(
                "bash", self.workspace_risk_prepared())

        self.assertFalse(decision["allowed"])
        self.assertEqual(
            decision["decision"],
            "noninteractive_workspace_risk_deny")

    def test_normal_git_prepared_call_does_not_open_risk_picker(self):
        sess = self.session("allow")
        sess.auto = True
        sess.pump = object()
        sess.pick = mock.Mock(
            side_effect=AssertionError("normal git must not prompt"))
        prepared = tools.PreparedArguments(
            "bash", '{"command":"git status"}')

        decision = sess.permission_decision("bash", prepared)

        self.assertTrue(decision["allowed"])
        sess.pick.assert_not_called()

    @staticmethod
    def memory_risk_prepared(name):
        if name == "memory_write":
            args = {
                "content": "cross-project fact",
                "scope": "global",
            }
            risk = tools.MemoryRisk(
                "global_write", "memory_write_global",
                "global memory affects future sessions",
                ("scope: global", "content chars: 18"))
        elif name == "memory_forget":
            args = {"identifier": "m-deadbeef"}
            risk = tools.MemoryRisk(
                "forget", "memory_forget",
                "memory deletion is persistent",
                ("identifier chars: 10",))
        else:
            raise ValueError(name)
        return tools.PreparedArguments(
            name,
            json.dumps(
                args, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")),
            memory_risk=risk)

    def test_memory_hard_guard_auto_allow_and_grant_still_prompt_once(self):
        cases = (
            ("memory_write", "memory_write_global"),
            ("memory_forget", "memory_forget"),
        )
        for name, policy_name in cases:
            with self.subTest(name=name):
                sess = self.session("allow")
                sess.cfg["permissions"][policy_name] = "allow"
                sess.auto = True
                sess.always.add(policy_name)
                sess.pump = object()
                seen = []

                def pick(options, **_kwargs):
                    seen.append(list(options))
                    return options[1]

                sess.pick = pick
                out = io.StringIO()
                with mock.patch.object(
                        CLI.sys.stdin, "isatty", return_value=True), \
                     contextlib.redirect_stdout(out):
                    decision = sess.permission_decision(
                        name, self.memory_risk_prepared(name))

                self.assertTrue(decision["allowed"])
                self.assertEqual(
                    decision["decision"], "memory_risk_once")
                self.assertEqual(decision["source"], "user")
                self.assertTrue(
                    decision["prepared"].memory_risk_approved)
                self.assertEqual(
                    [option[0] for option in seen[0]],
                    ["deny", "once"])
                self.assertIn("不会被 /auto", out.getvalue())
                self.assertIn("risk:", out.getvalue())

    def test_memory_hard_guard_escape_defaults_to_deny(self):
        sess = self.session("allow")
        sess.cfg["permissions"]["memory_forget"] = "allow"
        sess.auto = True
        sess.pump = object()
        sess.pick = mock.Mock(return_value=None)

        with mock.patch.object(
                CLI.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.print"):
            decision = sess.permission_decision(
                "memory_forget",
                self.memory_risk_prepared("memory_forget"))

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["decision"], "memory_risk_denied")
        self.assertEqual(decision["source"], "user")

    def test_memory_hard_config_deny_precedes_prompt(self):
        sess = self.session("allow")
        sess.cfg["permissions"]["memory_write_global"] = "deny"
        sess.auto = True
        sess.always.add("memory_write_global")
        sess.pump = object()
        sess.pick = mock.Mock(
            side_effect=AssertionError("deny must not prompt"))

        decision = sess.permission_decision(
            "memory_write",
            self.memory_risk_prepared("memory_write"))

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["decision"], "deny")
        self.assertEqual(decision["source"], "config")
        sess.pick.assert_not_called()

    def test_project_memory_write_does_not_open_hard_picker(self):
        sess = self.session("allow")
        sess.auto = True
        sess.pump = object()
        sess.pick = mock.Mock(
            side_effect=AssertionError("project write must not prompt"))
        prepared = tools.PreparedArguments(
            "memory_write",
            '{"content":"project fact","scope":"project"}')

        decision = sess.permission_decision(
            "memory_write", prepared)

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["decision"], "allow")
        self.assertEqual(decision["source"], "config")
        sess.pick.assert_not_called()

    def test_memory_hard_permission_rows_cannot_be_downgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sess = self.session("allow")
            sess.cfg["permissions"]["memory_write_global"] = "allow"
            sess.cfg["permissions"]["memory_forget"] = "ask"
            with mock.patch.object(
                    CLI.CFG, "USER_FILE", root / "user.json"), \
                 mock.patch.object(
                    CLI.CFG, "project_file",
                    return_value=root / "project.json"):
                rows = {
                    row["tool"]: row
                    for row in CLI._permission_rows(sess)
                }
                self.assertEqual(
                    rows["memory_write_global"]["effective"], "hard")
                self.assertEqual(
                    rows["memory_forget"]["source"], "hard_guard")

                offered = []
                sess.pick = lambda choices, **_kwargs: (
                    offered.extend(choices) or choices[1])
                selected = CLI._pick_permission_value(
                    sess, rows["memory_forget"])

                with mock.patch.object(
                        CLI.CFG, "update_user_permission") as update, \
                     mock.patch("builtins.print"):
                    changed = CLI._set_permission(
                        sess, "memory_forget", "allow")

                sess.cfg["permissions"]["memory_forget"] = "deny"
                denied_rows = {
                    row["tool"]: row
                    for row in CLI._permission_rows(sess)
                }

        self.assertEqual(selected, "reset")
        self.assertEqual(
            [choice[0] for choice in offered],
            ["deny", "reset"])
        self.assertFalse(changed)
        update.assert_not_called()
        self.assertEqual(
            denied_rows["memory_forget"]["effective"], "deny")
        self.assertEqual(
            denied_rows["memory_forget"]["source"],
            "effective_config")

    def test_parameter_command_persists_and_refreshes_current_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_file = root / ".zylab" / "settings.json"
            project_file = root / "project-settings.json"
            sess = self.session("ask")
            sess.always.add("bash")
            persisted_grants = []
            sess.save = lambda: persisted_grants.append(sorted(sess.always))
            with mock.patch.object(CLI.CFG, "USER_FILE", user_file), \
                 mock.patch.object(
                    CLI.CFG, "project_file", return_value=project_file), \
                 mock.patch("builtins.print"):
                CLI.cmd_permissions(sess, "bash deny")
                stored = json.loads(user_file.read_text(encoding="utf-8"))
                self.assertEqual(stored["permissions"]["bash"], "deny")
                self.assertEqual(sess.cfg["permissions"]["bash"], "deny")
                self.assertNotIn("bash", sess.always)
                self.assertEqual(persisted_grants, [[]])

                CLI.cmd_permissions(sess, "bash reset")

            reset = json.loads(user_file.read_text(encoding="utf-8"))
            self.assertNotIn("bash", reset.get("permissions", {}))
            self.assertEqual(sess.cfg["permissions"]["bash"], "ask")
            self.assertIs(sess.ag.hook_cfg, sess.cfg)
            self.assertIs(CLI.tools.HOOK_CTX["cfg"], sess.cfg)

    def test_permission_change_aborts_if_grant_revocation_cannot_persist(self):
        sess = self.session("ask")
        sess.always.add("bash")
        sess.save = mock.Mock(side_effect=OSError("disk full"))
        with mock.patch.object(
                CLI.CFG, "update_user_permission") as update, \
                mock.patch("builtins.print"):
            changed = CLI._set_permission(sess, "bash", "deny")

        self.assertFalse(changed)
        self.assertIn("bash", sess.always)
        update.assert_not_called()

    def test_interactive_panel_can_edit_multiple_then_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_file = root / ".zylab" / "settings.json"
            project_file = root / "project-settings.json"
            sess = self.session("ask")
            sess.pump = object()
            picks = 0

            def pick(rows, *, title="", **_kwargs):
                nonlocal picks
                picks += 1
                if picks == 1:
                    return next(row for row in rows if row["tool"] == "bash")
                if picks == 2:
                    return next(choice for choice in rows if choice[0] == "deny")
                return None

            sess.pick = pick
            with mock.patch.object(CLI.CFG, "USER_FILE", user_file), \
                 mock.patch.object(
                    CLI.CFG, "project_file", return_value=project_file), \
                 mock.patch("builtins.print"):
                CLI.cmd_permissions(sess, "")

            self.assertEqual(picks, 3)
            self.assertEqual(sess.cfg["permissions"]["bash"], "deny")

    def test_permission_alias_and_unique_prefix_resolve_to_plural_command(self):
        for command in ("permission", "permissions", "permiss"):
            canonical, entry, exit_alias = CLI.resolve_command(command)
            self.assertEqual(canonical, "permissions")
            self.assertIs(entry[0], CLI.cmd_permissions)
            self.assertFalse(exit_alias)
        self.assertIn("/permission", CLI.COMMANDS)
        self.assertIn("permissions", CLI.REGISTRY)


class ProbeCommandTests(unittest.TestCase):
    class Agent:
        model = "current-model"
        gateway = "boyue"

        def __init__(self):
            self.set_models = []

        def set_model(self, model):
            self.set_models.append(model)

    class Session:
        def __init__(self):
            self.ag = ProbeCommandTests.Agent()

    def test_empty_probe_targets_current_session_route(self):
        # 非交互调用没有 picker，仍保留“当前目标”默认值，便于脚本/单测复用。
        sess = self.Session()
        record = {
            "gateway": "boyue", "id": "current-model", "status": "ok",
            "supports_tools": True, "supports_temperature": True,
            "first_token_s": 0.4, "response_s": 1.2,
            "probe_total_s": 1.3, "probe_attempts": 1,
            "probe_timing_version": 2,
        }
        with mock.patch.object(
                CLI.M, "probe", return_value=record) as probe, \
                mock.patch("builtins.print") as printer:
            CLI.cmd_probe(sess, "")

        probe.assert_called_once_with("boyue", "current-model")
        output = "\n".join(
            " ".join(str(arg) for arg in call.args)
            for call in printer.call_args_list)
        self.assertIn("current-model@boyue", output)
        self.assertIn("首响应 0.400s", output)
        self.assertIn("完整响应 1.200s", output)

    def test_interactive_bare_probe_opens_cost_menu_and_can_choose_status(self):
        sess = self.Session()
        sess.pump = object()
        titles = []

        def pick(rows, *, title="", **_kwargs):
            titles.append(title)
            self.assertTrue(any("0 请求" in row["cost"] for row in rows))
            self.assertTrue(any("7.2M" in row["cost"] for row in rows))
            return next(row for row in rows if row["mode"] == "status")

        sess.pick = pick
        cached = {
            "gateway": "boyue", "id": "current-model",
            "status": "unprobed", "supports_tools": None,
            "supports_temperature": None,
        }
        with mock.patch.object(CLI.M, "get", return_value=cached) as get, \
                mock.patch.object(CLI.M, "probe") as probe, \
                mock.patch("builtins.print"):
            CLI.cmd_probe(sess, "")

        self.assertEqual(titles, ["模型探测 · Enter 选择 · Esc 取消"])
        get.assert_called_once_with("boyue", "current-model")
        probe.assert_not_called()

    def test_interactive_probe_cancel_sends_no_request(self):
        sess = self.Session()
        sess.pump = object()
        sess.pick = mock.Mock(return_value=None)
        with mock.patch.object(CLI.M, "probe") as probe, \
                mock.patch.object(CLI.M, "probe_context") as context, \
                mock.patch("builtins.print") as printer:
            CLI.cmd_probe(sess, "")

        probe.assert_not_called()
        context.assert_not_called()
        output = "\n".join(
            " ".join(str(arg) for arg in call.args)
            for call in printer.call_args_list)
        self.assertIn("已取消 probe · 0 请求", output)

    def test_interactive_probe_can_pick_alternate_cached_target(self):
        sess = self.Session()
        sess.pump = object()
        selected = {
            "gateway": "deepinfer", "id": "other-model", "status": "listed",
            "supports_tools": None, "supports_temperature": None,
        }
        picks = 0

        def pick(rows, *, title="", **_kwargs):
            nonlocal picks
            picks += 1
            if picks == 1:
                return next(row for row in rows if row["mode"] == "select")
            self.assertIn("选择要探测", title)
            return selected

        sess.pick = pick
        result = {**selected, "status": "ok", "supports_tools": True,
                  "supports_temperature": True, "probe_timing_version": 2}
        with mock.patch.object(CLI.M, "probed_rows", return_value=[selected]), \
                mock.patch.object(CLI.M, "probe", return_value=result) as probe, \
                mock.patch("builtins.print"):
            CLI.cmd_probe(sess, "")

        self.assertEqual(picks, 2)
        probe.assert_called_once_with("deepinfer", "other-model")

    def test_explicit_probe_subcommand_bypasses_interactive_menu(self):
        sess = self.Session()
        sess.pump = object()
        sess.pick = mock.Mock()
        cached = {
            "gateway": "boyue", "id": "current-model",
            "status": "unprobed", "supports_tools": None,
            "supports_temperature": None,
        }
        with mock.patch.object(CLI.M, "get", return_value=cached), \
                mock.patch("builtins.print"):
            CLI.cmd_probe(sess, "status")
        sess.pick.assert_not_called()

    def test_target_parser_supports_route_forms_without_breaking_model_slash(self):
        sess = self.Session()
        self.assertEqual(
            CLI._parse_probe_request(sess, "Qwen/Qwen3-Coder"),
            ("capability", "boyue", "Qwen/Qwen3-Coder"))
        self.assertEqual(
            CLI._parse_probe_request(sess, "model@deepinfer"),
            ("capability", "deepinfer", "model"))
        self.assertEqual(
            CLI._parse_probe_request(sess, "model@DeepInfer"),
            ("capability", "deepinfer", "model"))
        self.assertEqual(
            CLI._parse_probe_request(sess, "deepinfer/model"),
            ("capability", "deepinfer", "model"))
        self.assertEqual(
            CLI._parse_probe_request(sess, "deepinfer"),
            ("capability", "deepinfer", "current-model"))
        self.assertEqual(
            CLI._parse_probe_request(sess, "status deepinfer model"),
            ("status", "deepinfer", "model"))

    def test_status_reads_cache_without_network_probe(self):
        sess = self.Session()
        cached = {
            "gateway": "boyue", "id": "current-model",
            "status": "unprobed", "supports_tools": None,
            "supports_temperature": None,
        }
        with mock.patch.object(CLI.M, "get", return_value=cached) as get, \
                mock.patch.object(CLI.M, "probe") as probe, \
                mock.patch("builtins.print") as printer:
            CLI.cmd_probe(sess, "status")

        get.assert_called_once_with("boyue", "current-model")
        probe.assert_not_called()
        output = "\n".join(
            " ".join(str(arg) for arg in call.args)
            for call in printer.call_args_list)
        self.assertIn("没有发送 API 请求", output)

    def test_legacy_cached_latency_is_not_claimed_as_true_first_response(self):
        record = {
            "gateway": "boyue", "id": "legacy", "status": "ok",
            "supports_tools": True, "supports_temperature": True,
            "first_token_s": 20.0,
        }
        with mock.patch("builtins.print") as printer:
            CLI._render_probe_record(record)
        output = "\n".join(
            " ".join(str(arg) for arg in call.args)
            for call in printer.call_args_list)
        self.assertIn("旧版整段耗时 20.000s", output)
        self.assertNotIn("首响应 20.000s", output)

    def test_all_is_rejected_before_any_expensive_call(self):
        sess = self.Session()
        with mock.patch.object(CLI.M, "probe") as probe, \
                mock.patch.object(CLI.M, "probe_context") as context, \
                mock.patch("builtins.print") as printer:
            CLI.cmd_probe(sess, "all")

        probe.assert_not_called()
        context.assert_not_called()
        output = "\n".join(
            " ".join(str(arg) for arg in call.args)
            for call in printer.call_args_list)
        self.assertIn("--probe-all", output)

    def test_context_probe_is_explicit_and_refreshes_current_agent_limit(self):
        sess = self.Session()
        record = {
            "gateway": "boyue", "id": "current-model", "status": "ok",
            "supports_tools": True, "supports_temperature": True,
            "context": 128_000, "context_source": "probed",
            "context_upper_bound": 256_000,
        }
        with mock.patch.object(
                CLI.M, "probe_context", return_value=(128_000, 256_000)) as probe, \
                mock.patch.object(CLI.M, "get", return_value=record), \
                mock.patch("builtins.print"):
            CLI.cmd_probe(sess, "context")

        probe.assert_called_once()
        self.assertEqual(probe.call_args.args, ("boyue", "current-model"))
        self.assertTrue(callable(probe.call_args.kwargs["on_progress"]))
        self.assertEqual(sess.ag.set_models, ["current-model"])

    def test_context_probe_progress_bar_reports_steps_requests_and_boundary(self):
        sess = self.Session()
        record = {
            "gateway": "boyue", "id": "current-model", "status": "ok",
            "supports_tools": True, "supports_temperature": True,
            "context": 63_900, "context_source": "probed",
            "context_upper_bound": 128_000,
            "context_probe_state": "bounded",
            "context_probe_rejected_at": 128_000,
        }

        def fake_probe(_gateway, _model, *, on_progress):
            on_progress({
                "state": "request", "step": 1, "completed": 0,
                "total": 2, "target": 64_000, "requests": 0,
            })
            on_progress({
                "state": "result", "result": "ok", "completed": 1,
                "total": 2, "target": 64_000, "observed": 63_900,
                "requests": 1,
            })
            on_progress({
                "state": "result", "result": "rejected", "completed": 2,
                "total": 2, "target": 128_000, "requests": 2,
            })
            on_progress({
                "state": "done", "outcome": "bounded", "completed": 2,
                "total": 2, "target": 128_000, "requests": 2,
            })
            return 63_900, 128_000

        with mock.patch.object(
                CLI.M, "probe_context", side_effect=fake_probe), \
                mock.patch.object(CLI.M, "get", return_value=record), \
                mock.patch("builtins.print") as printer:
            CLI.cmd_probe(sess, "context")

        output = "\n".join(
            " ".join(str(arg) for arg in call.args)
            for call in printer.call_args_list)
        self.assertIn("[█████████░░░░░░░░░]", output)
        self.assertIn("1/2 档 · 1 req", output)
        self.assertIn("usage 63,900", output)
        self.assertIn("已触顶", output)

    def test_context_progress_bar_marks_ceiling_as_unknown_not_bounded(self):
        line = CLI._context_progress_line({
            "state": "done", "outcome": "ceiling", "completed": 9,
            "total": 9, "target": 3_000_000, "requests": 9,
        })
        self.assertIn("[██████████████████]", line)
        self.assertIn("模型顶仍未知", line)
        self.assertNotIn("拒绝边界", line)


class SessionTaskCancellationTests(unittest.TestCase):
    def test_controller_intent_is_recorded_before_task_cancel(self):
        class Agent:
            session_id = "session-task"
            last_total = 0

        session = CLI.Session(Agent(), json.loads(
            json.dumps(CLI.CFG.DEFAULTS)))
        journal = CLI.C.MemoryJournal()
        controller = CLI.C.SessionController(
            "session-task", journal,
            id_factory=lambda kind: f"{kind}-1")
        controller.submit("go")
        controller.begin_request(request_id="request-1")
        call = {
            "id": "tool-call-1",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": '{"command":"sleep 30"}',
            },
        }
        controller.complete_response(
            {"role": "assistant", "content": "",
             "tool_calls": [call]})
        controller.decide_tool(
            "tool-call-1", allowed=True,
            decision="once", source="user")
        controller.start_tool("tool-call-1")
        session.controller = controller

        class ProbeManager:
            def __init__(self):
                self.key = None
                self.kind_at_cancel = None

            def cancel(probe, key):
                probe.key = key
                probe.kind_at_cancel = journal.events[-1]["kind"]
                return None

        probe = ProbeManager()
        session.task_manager = probe
        action, task = session.cancel_active_work()

        self.assertIsNone(task)
        self.assertEqual(
            action.kind, CLI.C.ActionKind.CANCEL_TOOL)
        self.assertEqual(probe.key, "tool-call-1")
        self.assertEqual(
            probe.kind_at_cancel, "tool_cancel_requested")


class ResumeReplayTests(unittest.TestCase):
    def test_preview_shows_recent_user_and_assistant_text_only(self):
        messages = [
            {"role": "system", "content": "hidden system"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "tool", "content": "hidden raw tool output"},
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
        ]

        replay = CLI.format_resume_replay(messages, limit=2)

        self.assertNotIn("hidden system", replay)
        self.assertNotIn("hidden raw tool output", replay)
        self.assertNotIn("old question", replay)
        self.assertLess(replay.index("recent question"),
                        replay.index("recent answer"))
        self.assertIn("会话预览", replay)
        self.assertIn("预览结束", replay)

    def test_replay_preserves_leading_code_indentation(self):
        replay = CLI.format_resume_replay([
            {"role": "user", "content": "\n  indented code\n"}])

        self.assertIn("›   indented code", replay)

    def test_replay_truncation_preserves_message_head_and_tail(self):
        content = "HEAD-" + ("x" * 500) + "-TAIL"

        replay = CLI.format_resume_replay(
            [{"role": "assistant", "content": content}],
            message_chars=200, total_chars=1_000)

        self.assertIn("HEAD-", replay)
        self.assertIn("-TAIL", replay)
        self.assertIn("省略", replay)

    def test_show_preview_uses_configured_limit_and_renderer(self):
        class Capture:
            def __init__(self):
                self.output = ""

            def write_output(self, text):
                self.output += text

        sess = CLI.Session.__new__(CLI.Session)
        sess.cfg = {"resume_replay": 1}
        sess.renderer = Capture()
        messages = [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "newest"},
        ]

        self.assertTrue(CLI.show_session_preview(sess, messages))
        self.assertNotIn("older", sess.renderer.output)
        self.assertIn("newest", sess.renderer.output)

    def test_resume_command_hydrates_loaded_session(self):
        class Agent:
            def __init__(self):
                self.messages = [{"role": "system", "content": "current"}]
                self.session_id = "current"
                self.model = "old-model"

            def load_context(self, context):
                self.context = context

            def set_model(self, model):
                self.model = model

        class Session:
            def __init__(self):
                self.ag = Agent()
                self.cfg = {"resume_replay": 2}
                self.pump = object()
                self.saved = False
                self.reset = False

            def save(self):
                self.saved = True

            def reset_controller(self):
                self.reset = True

        messages = [
            {"role": "system", "content": "saved system"},
            {"role": "user", "content": "saved question"},
            {"role": "assistant", "content": "saved answer"},
        ]
        loaded = {
            "id": "saved",
            "title": "saved title",
            "model": "saved-model",
            "context": {"summary": None},
            "messages": messages,
        }
        sess = Session()

        with mock.patch.object(
                CLI.store, "list_sessions",
                return_value=[{"id": "saved"}]),                 mock.patch.object(
                    CLI.store, "load_session", return_value=loaded),                 mock.patch.object(CLI, "show_resumed_transcript", return_value=True) as hydrate,                 mock.patch("builtins.print"):
            CLI.cmd_resume(sess, "saved")

        self.assertTrue(sess.saved)
        self.assertTrue(sess.reset)
        self.assertIs(sess.ag.messages, messages)
        self.assertEqual(sess.ag.session_id, "saved")
        self.assertEqual(sess.ag.model, "saved-model")
        hydrate.assert_called_once_with(sess, messages)

    def test_resume_tail_has_no_replay_component(self):
        tail = CLI._format_resume_tail([
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ])

        self.assertIn("› question", tail)
        self.assertIn("⏺ answer", tail)
        self.assertNotIn("恢复上下文", tail)
        self.assertNotIn("回放结束", tail)


    def test_restore_record_recovers_footer_metadata(self):
        class Agent:
            def __init__(self):
                self.messages = []
                self.session_id = "current"
                self.model = "old-model"
                self.gateway = CLI.client.GATEWAY
                self.tokens_in = 0
                self.tokens_out = 0
                self.last_total = 0
                self.compact_at = 70_000

            def load_context(self, context):
                self.context = context

            def set_model(self, model):
                self.model = model

        class Session:
            def __init__(self):
                self.ag = Agent()
                self.last_ctx = None
                self._title = None
                self.plan_mode = False
                self.auto = False

        original_gateway = CLI.client.GATEWAY
        restored_gateway = (
            "boyue" if original_gateway != "boyue" else "deepinfer")
        messages = [{"role": "user", "content": "saved question"}]
        record = {
            "id": "saved-session",
            "title": "restored title",
            "gateway": restored_gateway,
            "model": "saved-model",
            "tokens_in": 123,
            "tokens_out": 45,
            "last_context_tokens": 4_321,
            "context": {"summary": "saved summary"},
            "messages": messages,
        }
        sess = Session()

        try:
            CLI.restore_session_record(sess, record)
            footer = CLI.status_line(sess)
        finally:
            CLI.client.set_gateway(original_gateway)

        self.assertIs(sess.ag.messages, messages)
        self.assertEqual(sess.ag.session_id, "saved-session")
        self.assertEqual(sess.ag.gateway, restored_gateway)
        self.assertEqual(sess.ag.model, "saved-model")
        self.assertEqual((sess.ag.tokens_in, sess.ag.tokens_out), (123, 45))
        self.assertEqual((sess.ag.last_total, sess.last_ctx), (4_321, 4_321))
        self.assertEqual(sess._title, "restored title")
        self.assertIn("restored title", footer)
        self.assertIn(f"saved-model@{restored_gateway}", footer)
        self.assertIn("ctx 4K/70K", footer)


class RendererTests(unittest.TestCase):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    def test_renderer_rejects_background_stdout_write(self):
        renderer = tui.TerminalRenderer(io.StringIO())
        errors = []

        def background():
            try:
                renderer.write_output("bad")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=background)
        thread.start()
        thread.join()
        self.assertIsInstance(errors[0], RuntimeError)

    def test_output_erases_visible_spinner(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        renderer.spinner("thinking", 0)
        renderer.write_output("done")
        self.assertIn("\r\x1b[2Kdone", stream.getvalue())
        self.assertFalse(renderer._spinner_visible)

    def test_partial_stream_survives_composer_redraw_and_continues(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        snapshot = tui.InputSnapshot(
            mode="line", prompt="steer> ", status="chat")

        renderer.render(snapshot)
        renderer.write_output("ANSWER")
        renderer.render(snapshot)
        first_frame = stream.getvalue()

        self.assertIn("ANSWER\r\n\r\x1b[J", first_frame)
        self.assertIn("╭", tui._ANSI.sub("", first_frame))
        self.assertIn("│ steer> ", tui._ANSI.sub("", first_frame))
        self.assertNotIn("ANSWER\r\x1b[J", first_frame)
        self.assertEqual(renderer._output_partial_col, 6)

        before_continue = len(stream.getvalue())
        renderer.write_output(" CONT")
        renderer.render(snapshot)
        continuation = stream.getvalue()[before_continue:]
        self.assertIn("\x1b[1A\r\x1b[6C CONT\r\n", continuation)
        self.assertEqual(renderer._output_partial_col, 11)

        renderer.finish_output_line()
        self.assertIsNone(renderer._output_partial_col)

    def test_ansi_only_markdown_delta_preserves_partial_output(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        snapshot = tui.InputSnapshot(
            mode="line", prompt="steer> ", status="chat")

        renderer.render(snapshot)
        renderer.write_output("prefix", source="model")
        renderer.render(snapshot)
        renderer.write_output(
            tui.StreamingMarkdown.BOLD + tui.StreamingMarkdown.RESET,
            source="model")

        # A delimiter-only provider delta has no visible cells, but it must
        # not make render() forget the unfinished model line above composer.
        self.assertEqual(renderer._output_partial_col, 6)
        self.assertEqual(renderer._output_source, "model")
        renderer.render(snapshot)

        before_continue = len(stream.getvalue())
        renderer.write_output(" suffix", source="model")
        continuation = stream.getvalue()[before_continue:]
        self.assertIn("\x1b[1A\r\x1b[6C suffix\r\n", continuation)
        self.assertEqual(renderer._output_partial_col, 13)


    def test_full_width_partial_line_is_not_mistaken_for_newline(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        snapshot = tui.InputSnapshot(mode="line", prompt="> ")

        with mock.patch.object(tui, "_cols", return_value=6):
            renderer.write_output("ABCDEF")
            renderer.render(snapshot)
            self.assertEqual(renderer._output_partial_col, 0)
            renderer.write_output(
                tui.StreamingMarkdown.INLINE_CODE
                + tui.StreamingMarkdown.RESET)
            self.assertEqual(renderer._output_partial_col, 0)
            renderer.render(snapshot)
            before_continue = len(stream.getvalue())
            renderer.write_output("Z")

        continuation = stream.getvalue()[before_continue:]
        self.assertIn("\x1b[1A\rZ\r\n", continuation)


    def test_queue_prompt_and_footer_are_one_clearable_frame(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        snapshot = tui.InputSnapshot(
            mode="line",
            prompt="steer› ",
            queued=("↪ steer  first", "＋ next  second"),
            status="chat demo · model@gateway · ctx 4K/70K",
            activity="工作中 2s")

        renderer.render(snapshot)
        rendered = tui._ANSI.sub("", stream.getvalue())

        self.assertLess(rendered.index("first"), rendered.index("steer› "))
        self.assertLess(rendered.index("steer› "), rendered.index("chat demo"))
        self.assertIn("工作中 2s", rendered)
        self.assertEqual(renderer._rows_before_cursor, 3)

        before_clear = len(stream.getvalue())
        renderer.clear_input()
        clear_sequence = stream.getvalue()[before_clear:]
        self.assertEqual(clear_sequence, "\r\x1b[3A\x1b[J")

    def test_workflow_dock_is_inside_the_clearable_composer_frame(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        snapshot = tui.InputSnapshot(
            mode="line", prompt="steer› ",
            queued=("＋ next  queued prompt",),
            dock=(
                "◆ wf-dock · running/review · agents 5/8 · req 6/24",
                "DAG  ✓ agents → ● review → ○ synthesis",
                "Seats  ✓ Qwen@boyue · ● GLM@boyue",
            ),
            status="chat demo · model@gateway")

        renderer.render(snapshot)
        rendered = tui._ANSI.sub("", stream.getvalue())

        self.assertLess(rendered.index("queued prompt"), rendered.index("wf-dock"))
        self.assertLess(rendered.index("wf-dock"), rendered.index("DAG"))
        self.assertLess(rendered.index("DAG"), rendered.index("steer› "))
        self.assertLess(rendered.index("steer› "), rendered.index("chat demo"))
        self.assertEqual(renderer._rows_before_cursor, 5)

        before_clear = len(stream.getvalue())
        renderer.clear_input()
        self.assertEqual(
            stream.getvalue()[before_clear:], "\r\x1b[5A\x1b[J")

    def test_fullscreen_picker_redraw_enters_once_and_exit_restores_screen(self):
        stream = self.TTY()
        renderer = tui.TerminalRenderer(stream)
        snapshot = tui.InputSnapshot(
            mode="picker", title="Agent workspace",
            options=("first", "second"), selected=0,
            controls="Click/Enter 打开", fullscreen=True)

        renderer.render(snapshot)
        renderer.render(tui.dataclass_replace(snapshot, selected=1))
        renderer.clear_input()

        output = stream.getvalue()
        self.assertEqual(output.count("\x1b[?1049h"), 1)
        self.assertEqual(output.count("\x1b[?1049l"), 1)
        self.assertEqual(output.count("\x1b[2J\x1b[H"), 2)
        self.assertFalse(renderer._picker_fullscreen)

    def test_boxed_composer_wraps_unicode_and_keeps_cursor_inside(self):
        renderer = tui.TerminalRenderer(self.TTY())
        snapshot = tui.InputSnapshot(
            mode="line", prompt="› ",
            text="中文👩‍💻-" * 8,
            cursor=len("中文👩‍💻-" * 5),
            status="chat · model@gateway")

        lines, cursor_row, cursor_col = renderer._build_frame(snapshot, 32)
        plain_lines = [tui._ANSI.sub("", line) for line in lines]

        self.assertTrue(plain_lines[0].startswith("╭"))
        self.assertTrue(any(line.startswith("│ › ") for line in plain_lines))
        self.assertTrue(any(line.startswith("╰") for line in plain_lines))
        self.assertTrue(all(tui.display_width(line) <= 32 for line in lines))
        self.assertGreater(cursor_row, 1)
        self.assertLess(cursor_col, 31)

    def test_boxed_composer_renders_explicit_newlines_as_rows(self):
        renderer = tui.TerminalRenderer(self.TTY())
        text = "first\n第二行"
        snapshot = tui.InputSnapshot(
            mode="line", prompt="› ", text=text,
            cursor=len("first\n第"))

        lines, cursor_row, cursor_col = renderer._build_frame(snapshot, 32)
        plain_lines = [tui._ANSI.sub("", line) for line in lines]

        self.assertTrue(any("› first" in line for line in plain_lines))
        self.assertTrue(any("第二行" in line for line in plain_lines))
        self.assertTrue(all("\n" not in line for line in lines))
        self.assertEqual(cursor_row, 2)
        self.assertEqual(cursor_col, 6)

    def test_boxed_composer_width_invariants_cover_unicode_edges(self):
        renderer = tui.TerminalRenderer(self.TTY())
        samples = (
            "", "plain", "中文组合", "e\u0301 + combining",
            "🇨🇳🇺🇳 flags", "👩\u200d💻" * 12,
            "safe\x1b[2Jbad\x1b]0;TITLE\x1b\\tail",
        )
        for width in (20, 21, 32, 48):
            for value in samples:
                for cursor in (0, len(value) // 2, len(value)):
                    with self.subTest(
                            width=width, value=value, cursor=cursor):
                        lines, row, column = renderer._build_frame(
                            tui.InputSnapshot(
                                mode="line", prompt="steer› ",
                                text=value, cursor=cursor),
                            width)
                        self.assertTrue(all(
                            tui.display_width(line) <= width
                            for line in lines))
                        self.assertLess(row, len(lines))
                        self.assertGreaterEqual(column, 0)
                        self.assertLess(column, width)

    def test_footer_collapses_multiline_and_repeated_whitespace(self):
        renderer = tui.TerminalRenderer(self.TTY())
        lines, _, _ = renderer._build_frame(
            tui.InputSnapshot(
                mode="line", prompt="› ",
                activity="  空闲 \n ",
                status=" chat   demo \r\n model@test "),
            60)
        footer = tui._ANSI.sub("", lines[-1])
        self.assertEqual(footer, "  空闲 · chat demo model@test")

    def test_committed_user_input_is_one_dim_line(self):
        stream = self.TTY()
        renderer = tui.TerminalRenderer(stream)

        renderer.commit_input("› ", "inspect this")

        plain = tui._ANSI.sub("", stream.getvalue())
        self.assertIn("› inspect this", plain)
        self.assertNotIn("╭─ You", plain)       # 像 Claude Code：用户消息一行，不画框
        self.assertIn("\x1b[48;5;236;38;5;255m", stream.getvalue(), "整行铺底色，一眼认出用户输入")

    def test_resume_hydration_uses_normal_cards_markdown_and_lazy_history(self):
        stream = self.TTY()
        renderer = tui.TerminalRenderer(stream)
        messages = [{"role": "system", "content": "hidden"}]
        for index in range(50):
            messages.extend((
                {"role": "user", "content": f"prompt {index}"},
                {"role": "assistant", "content": f"answer {index}"},
            ))
        messages[-1]["content"] = "## Latest answer\n\n**rendered**"
        original = json.loads(json.dumps(messages))

        with (mock.patch.object(tui, "_cols", return_value=52),
              mock.patch.object(tui, "_lines", return_value=10)):
            shown = renderer.hydrate_transcript(messages)

        plain = tui._ANSI.sub("", stream.getvalue())
        self.assertGreater(shown, 0)
        self.assertIn("› prompt 49", plain)
        self.assertIn("Latest answer", plain)
        self.assertIn("rendered", plain)
        self.assertNotIn("恢复上下文", plain)
        self.assertNotIn("回放结束", plain)
        self.assertNotIn("prompt 0", plain)
        self.assertGreater(renderer._transcript_source_cursor, 0)
        self.assertEqual(messages, original)

    def test_transcript_history_compacts_tool_details_without_mutating_raw(self):
        renderer = tui.TerminalRenderer(self.TTY())
        payload = "\n".join(f"line {index}" for index in range(200))
        messages = [
            {"role": "user", "content": "inspect"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "bash", "arguments": "{\"cmd\":\"pwd\"}"},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": payload},
        ]
        original = json.loads(json.dumps(messages))

        renderer.set_transcript(messages)
        entries = [entry.text for entry in renderer._transcript]

        self.assertTrue(any(value.startswith("● bash") for value in entries))
        result = next(value for value in entries if value.startswith("⎿  bash"))
        self.assertIn("200 lines", result)
        self.assertIn("/expand call-1", result)
        self.assertEqual(messages, original)

    def test_transcript_history_keeps_failed_tool_result_visible(self):
        renderer = tui.TerminalRenderer(self.TTY())
        payload = "[SANDBOXED]\ncommand failed\n[exit 7]"
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-failed", "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": "{\"command\":\"false\"}",
                },
            }]},
            {
                "role": "tool",
                "tool_call_id": "call-failed",
                "content": payload,
            },
        ]
        original = json.loads(json.dumps(messages))

        renderer.set_transcript(messages)
        result = next(
            entry.text for entry in renderer._transcript
            if entry.text.startswith("⎿  bash"))

        self.assertIn("command failed", result)
        self.assertIn("[exit 7]", result)
        self.assertNotIn("/expand", result)
        self.assertEqual(messages, original)

    def test_live_composer_drag_selection_highlights_unicode_and_copies(self):
        stream = self.TTY()
        renderer = tui.TerminalRenderer(stream)
        snapshot = tui.InputSnapshot(
            mode="line", prompt="› ", text="hello 世界", cursor=8)

        with (mock.patch.object(tui, "_cols", return_value=52),
              mock.patch.object(tui, "_lines", return_value=16)):
            _, cursor_row, _ = renderer._build_frame(snapshot, 51)
            cursor_screen_row = 6
            content_row = cursor_screen_row - cursor_row + 1
            self.assertTrue(renderer.composer_mouse(
                tui.MouseEvent("press", x=3, y=content_row, button=0),
                snapshot, cursor_screen_row=cursor_screen_row).handled)
            renderer.composer_mouse(
                tui.MouseEvent("motion", x=9, y=content_row, button=0),
                snapshot, cursor_screen_row=cursor_screen_row)
            result = renderer.composer_mouse(
                tui.MouseEvent("release", x=9, y=content_row, button=0),
                snapshot, cursor_screen_row=cursor_screen_row)

            self.assertTrue(result.handled)
            self.assertEqual(renderer._composer_selection_text(), "hello")
            self.assertTrue(renderer.composer_selection_active)
            selected_frame = renderer._build_frame(snapshot, 51)[0]
            self.assertIn("\x1b[7mhello\x1b[27m", "\n".join(selected_frame))
            self.assertTrue(renderer.copy_composer_selection(snapshot))

        encoded = base64.b64encode(b"hello").decode("ascii")
        self.assertIn("\x1b]52;c;" + encoded + "\x07", stream.getvalue())
        self.assertIn("Ctrl-C 复制选区", tui._ANSI.sub("", stream.getvalue()))

    def test_live_composer_click_returns_cursor_without_selection(self):
        renderer = tui.TerminalRenderer(self.TTY())
        snapshot = tui.InputSnapshot(
            mode="line", prompt="› ", text="hello", cursor=5)

        with mock.patch.object(tui, "_cols", return_value=52):
            result = renderer.composer_mouse(
                tui.MouseEvent("press", x=6, y=2, button=0),
                snapshot, cursor_screen_row=2)
            result = renderer.composer_mouse(
                tui.MouseEvent("release", x=6, y=2, button=0),
                snapshot, cursor_screen_row=2)

        self.assertTrue(result.handled)
        self.assertEqual(result.cursor, 2)
        self.assertFalse(renderer.composer_selection_active)
        self.assertIsNone(renderer._composer_selection_anchor)
        self.assertIsNone(renderer._composer_selection_focus)

    def test_live_composer_drag_selection_spans_wrapped_unicode_rows(self):
        renderer = tui.TerminalRenderer(self.TTY())
        snapshot = tui.InputSnapshot(
            mode="line", prompt="› ", text="abc中文\n第二行xyz",
            cursor=len("abc中文\n第二行xyz"))
        width = 24
        cursor_screen_row = 10

        with (mock.patch.object(tui, "_cols", return_value=width + 1),
              mock.patch.object(tui, "_lines", return_value=16)):
            _, cursor_row, _ = renderer._build_frame(snapshot, width)
            frame_top = cursor_screen_row - cursor_row
            start_y = frame_top + 1
            end_y = frame_top + 2

            self.assertTrue(renderer.composer_mouse(
                tui.MouseEvent("press", x=5, y=start_y, button=0),
                snapshot, cursor_screen_row=cursor_screen_row).handled)
            renderer.composer_mouse(
                tui.MouseEvent("motion", x=8, y=end_y, button=0),
                snapshot, cursor_screen_row=cursor_screen_row)
            result = renderer.composer_mouse(
                tui.MouseEvent("release", x=8, y=end_y, button=0),
                snapshot, cursor_screen_row=cursor_screen_row)

            self.assertTrue(result.handled)
            self.assertEqual(
                renderer._composer_selection_text(), "bc中文\n第二")
            rows = renderer._build_frame(snapshot, width)[0]
            self.assertIn("\x1b[7mbc中文\x1b[27m", rows[1])
            self.assertIn("\x1b[7m第二\x1b[27m", rows[2])

    def test_clipboard_external_backend_is_bounded_and_explicit(self):
        renderer = tui.TerminalRenderer(self.TTY())
        completed = mock.Mock(returncode=0)
        with (mock.patch.dict(
                    os.environ, {"ZYLAB_CLIPBOARD": "external"},
                    clear=False),
              mock.patch.object(
                  tui.shutil, "which", return_value="/usr/bin/wl-copy") as which,
              mock.patch.object(
                  tui.subprocess, "run", return_value=completed) as run):
            backend = renderer._copy_text_to_clipboard("你好")

        self.assertEqual(backend, "wl-copy")
        which.assert_called_once_with("wl-copy")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/wl-copy"])
        self.assertEqual(run.call_args.kwargs["input"], "你好".encode())
        self.assertEqual(run.call_args.kwargs["timeout"], 1.0)

    def test_live_composer_selection_is_invalidated_by_draft_or_layout_change(self):
        renderer = tui.TerminalRenderer(self.TTY())
        snapshot = tui.InputSnapshot(
            mode="line", prompt="› ", text="hello", cursor=5)

        with mock.patch.object(tui, "_cols", return_value=52):
            renderer.composer_mouse(
                tui.MouseEvent("press", x=3, y=2, button=0),
                snapshot, cursor_screen_row=2)
            renderer.composer_mouse(
                tui.MouseEvent("motion", x=8, y=2, button=0),
                snapshot, cursor_screen_row=2)
            renderer.composer_mouse(
                tui.MouseEvent("release", x=8, y=2, button=0),
                snapshot, cursor_screen_row=2)
            self.assertTrue(renderer.composer_selection_active)

            renderer.render(tui.InputSnapshot(
                mode="line", prompt="› ", text="changed", cursor=7))
            self.assertFalse(renderer.composer_selection_active)

            fresh = tui.InputSnapshot(
                mode="line", prompt="› ", text="hello", cursor=5)
            renderer.composer_mouse(
                tui.MouseEvent("press", x=3, y=2, button=0),
                fresh, cursor_screen_row=2)
            renderer.composer_mouse(
                tui.MouseEvent("motion", x=8, y=2, button=0),
                fresh, cursor_screen_row=2)
            renderer.composer_mouse(
                tui.MouseEvent("release", x=8, y=2, button=0),
                fresh, cursor_screen_row=2)
            renderer.render(tui.InputSnapshot(
                mode="line", prompt="› ", text="hello", cursor=5,
                queued=("queued",)))
            self.assertFalse(renderer.composer_selection_active)

    def test_clipboard_off_does_not_emit_osc52(self):
        stream = self.TTY()
        renderer = tui.TerminalRenderer(stream)
        with mock.patch.dict(
                os.environ, {"ZYLAB_CLIPBOARD": "off"}, clear=False):
            self.assertIsNone(renderer._copy_text_to_clipboard("secret"))

        self.assertNotIn("\x1b]52;", stream.getvalue())

    def test_ascii_history_chunking_preserves_exact_text(self):
        value = "x" * 205
        chunks = tui.TerminalRenderer._history_chunks(value, 80)
        self.assertEqual([len(chunk) for chunk in chunks], [80, 80, 45])
        self.assertEqual("".join(chunks), value)

    def test_prefixed_ascii_history_chunking_preserves_marker(self):
        value = "⏺ " + "x" * 203
        chunks = tui.TerminalRenderer._history_chunks(value, 80)
        self.assertEqual(
            [tui.display_width(chunk) for chunk in chunks],
            [80, 80, 45])
        self.assertEqual("".join(chunks), value)

    def test_set_transcript_indexes_a_visual_tail_lazily(self):
        class CountingRenderer(tui.TerminalRenderer):
            def __init__(self):
                super().__init__(io.StringIO())
                self.parsed = 0

            def _message_text(self, message):
                self.parsed += 1
                return super()._message_text(message)

        renderer = CountingRenderer()
        messages = [
            {"role": "user", "content": f"prompt {index}"}
            for index in range(100)
        ]
        with (mock.patch.object(tui, "_cols", return_value=52),
              mock.patch.object(tui, "_lines", return_value=10)):
            renderer.set_transcript(messages)
            initially_parsed = renderer.parsed
            self.assertGreater(initially_parsed, 0)
            self.assertLess(initially_parsed, len(messages))
            self.assertEqual(renderer._transcript[-1].text, "› prompt 99")

    def test_transcript_index_and_deferred_output_are_bounded(self):
        renderer = tui.TerminalRenderer(io.StringIO())
        renderer.TRANSCRIPT_CHAR_LIMIT = 80
        renderer.TRANSCRIPT_ENTRY_LIMIT = 3

        for index in range(10):
            renderer._record_transcript(
                "status", f"{index}:" + "x" * 24)

        self.assertLessEqual(len(renderer._transcript), 3)
        self.assertLessEqual(renderer._transcript_chars, 80)

    def test_source_backed_transcript_still_bounds_live_operations(self):
        renderer = tui.TerminalRenderer(io.StringIO())
        renderer.TRANSCRIPT_CHAR_LIMIT = 80
        renderer.TRANSCRIPT_ENTRY_LIMIT = 3
        renderer.set_transcript([
            {"role": "user", "content": "canonical prompt"},
        ])

        for index in range(10):
            renderer._record_transcript(
                "status", f"{index}:" + ("x" * 24),
                source=f"status:{index}")

        source_entries = [
            entry for entry in renderer._transcript
            if entry.message_index is not None
        ]
        live_entries = [
            entry for entry in renderer._transcript
            if entry.message_index is None
        ]
        self.assertEqual(
            [entry.text for entry in source_entries],
            ["› canonical prompt"])
        self.assertLessEqual(len(live_entries), 3)
        self.assertLessEqual(
            sum(entry.char_count for entry in live_entries), 80)

    def test_model_to_tool_transition_has_only_one_spacer_row(self):
        stream = io.StringIO()
        renderer = tui.TerminalRenderer(stream)
        renderer.write_output("ANSWER", source="model")
        marker = len(stream.getvalue())

        renderer.write_output(
            CLI._tool_transition_prefix(True) + "  TOOL\r\n",
            source="tool")

        transition = stream.getvalue()[marker:]
        self.assertIn("\r\n\r\n  TOOL\r\n", transition)
        self.assertNotIn("\r\n\r\n\r\n  TOOL", transition)

    def test_no_color_keeps_structure_without_decorative_escape_codes(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            renderer = tui.TerminalRenderer(self.TTY())
            lines, _, _ = renderer._build_frame(
                tui.InputSnapshot(
                    mode="line", prompt="\x1b[31m›\x1b[0m ",
                    text="plain", cursor=5,
                    status="chat", options=(("/help", "help"),)),
                40)

        self.assertFalse(renderer.styled)
        self.assertTrue(any("╭" in line for line in lines))
        self.assertTrue(all("\x1b" not in line for line in lines))

    def test_composer_and_prompt_card_strip_terminal_control_injection(self):
        stream = self.TTY()
        renderer = tui.TerminalRenderer(stream)
        malicious = "safe\x1b[2Jbad\x1b]0;TITLE\x1b\\tail\u202e"

        lines, _, _ = renderer._build_frame(
            tui.InputSnapshot(
                mode="line", prompt="› ", text=malicious,
                cursor=len(malicious)),
            60)
        renderer.commit_input("› ", malicious)

        plain = tui._ANSI.sub("", "\n".join(lines))
        self.assertIn("safebadtail", plain)
        self.assertNotIn("TITLE", plain)
        self.assertNotIn("\u202e", plain)
        self.assertNotIn("\x1b]", stream.getvalue())
        self.assertNotIn("TITLE", stream.getvalue())


class ModalEventTests(unittest.TestCase):
    def test_picker_defers_preexisting_line_events(self):
        submitted = tui.PumpEvent("submit", text="keep-me")
        retrieved = tui.PumpEvent("retrieve")

        class Pump:
            def __init__(self):
                self.events = [
                    submitted,
                    tui.PumpEvent("picker_result", value="second"),
                ]

            def open_picker(self, *args, **kwargs):
                pass

            def get(self, timeout=None):
                return self.events.pop(0)

            def drain(self):
                return [retrieved]

        class Renderer:
            def clear_input(self):
                pass

            def render(self, snapshot):
                pass

        session = CLI.Session.__new__(CLI.Session)
        session.pump = Pump()
        session.renderer = Renderer()
        session._deferred_pump_events = []

        self.assertEqual(session.pick(["first", "second"]), "second")
        self.assertIs(session._get_pump_event(), submitted)
        self.assertEqual(
            [event.kind for event in session._drain_pump_events()],
            ["retrieve"])

    def test_fullscreen_picker_error_restores_base_screen(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        failure = RuntimeError("input reader failed")

        class Pump:
            def __init__(self):
                self.events = [
                    tui.PumpEvent(
                        "redraw", snapshot=tui.InputSnapshot(
                            mode="picker", title="Agent workspace",
                            options=("agent",), fullscreen=True)),
                    tui.PumpEvent("error", value=failure),
                ]

            def open_picker(self, *args, **kwargs):
                pass

            def get(self, timeout=None):
                return self.events.pop(0)

        stream = TTY()
        session = CLI.Session.__new__(CLI.Session)
        session.pump = Pump()
        session.renderer = tui.TerminalRenderer(stream)
        session._deferred_pump_events = []
        session.heartbeat_lease = lambda: None

        with self.assertRaisesRegex(RuntimeError, "input reader failed"):
            session.pick(["agent"], fullscreen=True)

        output = stream.getvalue()
        self.assertEqual(output.count("\x1b[?1049h"), 1)
        self.assertEqual(output.count("\x1b[?1049l"), 1)
        self.assertFalse(session.renderer._picker_fullscreen)



class PTYTests(unittest.TestCase):
    CHILD_PREFIX = r"""
import json
import time
from core import tui
"""

    def run_child(self, body, sends, timeout=4.0, *, return_output=False):
        output, result = run_pty_child(
            body, sends,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=timeout,
            prefix=self.CHILD_PREFIX,
        )
        return (output, result) if return_output else result

    def test_queue_picker_actions_are_decoded_over_real_pty(self):
        result = self.run_child(
            r"""
rows = [{"id": "input-1", "text": "queued"}]
results = []
with tui.InputPump() as pump:
    for index, key in enumerate(("e", "s", "d"), 1):
        pump.open_picker(
            rows,
            title="Queue manager",
            render=lambda row: row["text"],
            actions={"enter": "edit", "e": "edit", "s": "send", "d": "delete"},
            controls="e edit · s send · d delete",
            fullscreen=True,
        )
        print(f"QUEUE_PICKER_READY_{index}", flush=True)
        while True:
            event = pump.get(0.2)
            if event is None:
                continue
            if event.kind == "picker_result":
                results.append([event.mode, event.value["id"] if event.value else None])
                break
print("RESULT:" + json.dumps(results, ensure_ascii=False))
""",
            [
                PTYSend(b"e", after="QUEUE_PICKER_READY_1"),
                PTYSend(b"s", after="QUEUE_PICKER_READY_2"),
                PTYSend(b"d", after="QUEUE_PICKER_READY_3"),
            ],
        )
        self.assertEqual(result, [
            ["edit", "input-1"],
            ["send", "input-1"],
            ["delete", "input-1"],
        ])

    def test_arrow_escape_and_text_are_not_conflated(self):
        result = self.run_child(
            r"""
events = []
with tui.InputPump() as pump:
    pump.set_busy(True)
    deadline = time.monotonic() + 3
    while len(events) < 3 and time.monotonic() < deadline:
        event = pump.get(0.2)
        if event and event.kind in ("retrieve", "submit", "cancel"):
            events.append([event.kind, event.text, event.mode])
print("RESULT:" + json.dumps(events, ensure_ascii=False))
""",
            [(0.12, b"\x1b[A"),
             (0.06, b"x\r"),
             (0.08, b"\x1b")])
        self.assertEqual(result, [
            ["retrieve", "", None],
            ["submit", "x", "steer"],
            ["cancel", "", None],
        ])

    def test_real_shift_enter_sequences_insert_newlines_without_submit(self):
        result = self.run_child(
            r"""
events = []
with tui.InputPump() as pump:
    deadline = time.monotonic() + 3
    while len(events) < 3 and time.monotonic() < deadline:
        event = pump.get(0.2)
        if event and event.kind == "submit":
            events.append([event.text, event.mode])
print("RESULT:" + json.dumps(events, ensure_ascii=False))
""",
            [(0.12, b"a\x1b[13;2ub\r"),
             (0.08, b"c\x1b\nd\r"),
             (0.08, b"e\x1b[27;2;13~f\r")])

        self.assertEqual(result, [
            ["a\nb", "next_turn"],
            ["c\nd", "next_turn"],
            ["e\nf", "next_turn"],
        ])

    def test_real_second_level_command_menu_and_freeform_goal(self):
        result = self.run_child(
            r"""
menu_seen = False
submitted = None
with tui.InputPump(
        commands={"/goal": "目标"},
        subcommands={
            "/goal": {
                "hint": "也可直接输入用户目标",
                "items": (("status", "查看状态"), ("set", "创建目标")),
            },
        }) as pump:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and submitted is None:
        event = pump.get(0.2)
        if event is None:
            continue
        if event.kind == "redraw":
            options = tuple(event.snapshot.options or ())
            if options and options[0][0] == "/goal status":
                menu_seen = True
        elif event.kind == "submit":
            submitted = [event.text, event.mode]
print("RESULT:" + json.dumps({"menu": menu_seen, "submitted": submitted}, ensure_ascii=False))
""",
            [(0.12, b"/goal s\t\r")])
        self.assertEqual(result, {
            "menu": True,
            "submitted": ["/goal status", "next_turn"],
        })

    def test_real_third_level_parameter_menu_and_description(self):
        result = self.run_child(
            r"""
menu_seen = False
description_seen = False
hint_seen = False
submitted = None
with tui.InputPump(
        subcommands={
            "/workflow": {
                "items": (("auto", "自适应"),),
                "params": {
                    "auto": {
                        "hint": "不会自动启动",
                        "items": (("on", "开启"), ("off", "关闭")),
                    },
                },
            },
        }) as pump:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and submitted is None:
        event = pump.get(0.2)
        if event is None:
            continue
        if event.kind == "redraw":
            snapshot = event.snapshot
            options = tuple(snapshot.options or ())
            if options and options[0][0] == "/workflow auto on":
                menu_seen = True
                description_seen = options[0][1] == "开启"
            if "三级参数" in (snapshot.hint or ""):
                hint_seen = True
        elif event.kind == "submit":
            submitted = [event.text, event.mode]
print("RESULT:" + json.dumps({
    "menu": menu_seen,
    "description": description_seen,
    "hint": hint_seen,
    "submitted": submitted,
}, ensure_ascii=False))
""",
            [(0.12, b"/workflow auto o\t\r")])
        self.assertEqual(result, {
            "menu": True,
            "description": True,
            "hint": True,
            "submitted": ["/workflow auto on", "next_turn"],
        })

    def test_real_pty_narrow_guidance_frame_is_width_bounded(self):
        output, result = run_pty_child(
            r"""
import json
import time
from core import tui

seen = False
widths = []
with tui.InputPump(
        commands={"/workflow": "workflow"},
        subcommands={
            "/workflow": {
                "items": (("research", "只读研究模式"),
                          ("standard", "标准协作模式")),
            },
        }) as pump:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not seen:
        event = pump.get(0.2)
        if event is None or event.kind != "redraw":
            continue
        if event.snapshot.options:
            lines, _, _ = tui.TerminalRenderer()._build_frame(
                event.snapshot, 20)
            widths = [tui.display_width(line) for line in lines]
            seen = True
print("RESULT:" + json.dumps({
    "seen": seen,
    "max_width": max(widths) if widths else 0,
    "bounded": bool(widths) and max(widths) <= 20,
}))
""",
            [(0.12, b"/workflow ")],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=6,
        )
        self.assertTrue(result["seen"], output)
        self.assertTrue(result["bounded"], output)
        self.assertLessEqual(result["max_width"], 20)

    def test_workflow_animation_list_detail_and_confirmation_in_real_pty(self):
        output, result = self.run_child(
            r"""
import os
import zylab

# 内置网关 profile 不再自带地址（地址是部署事实，不随代码发布），所以这个
# fixture 必须自己声明一个 HTTPS endpoint。它依赖 HTTPS 来避开明文传输那道
# **独立**的确认框：否则这里发出的一次 Enter 会被第二个 modal 吃掉，
# workflow 确认永远等不到答复，表现为 allowed=False 而不是超时。
zylab.client.GATEWAYS.setdefault("deepinfer", {})["base"] = (
    "https://synthetic.invalid/v1")

os.environ["TERM"] = "xterm-256color"
os.environ["ZYLAB_MOTION"] = "1"
os.environ.pop("NO_COLOR", None)
record = {
    "id": "wf-pty-smoke",
    "state": "completed",
    "stage": "completed",
    "goal": "PTY workflow smoke",
    "mode": "review",
    "preset": "quick",
    "completed_agents": 2,
    "expected_agents": 2,
    "lineup": [
        {"seat": "Qwen", "model": "qwen3.8-max",
         "gateway": "boyue", "state": "completed"},
        {"seat": "DeepSeek", "model": "deepseek-v4-pro-0813",
         "gateway": "deepinfer", "state": "completed"},
    ],
    "missing_seats": [],
    "result": (
        "<think>PTY_PRIVATE_REASONING</think>\n"
        "PTY_VISIBLE_SYNTHESIS"),
    "error": None,
}

class Manager:
    def list(self, **_kwargs):
        return [record]

    def resolve_lineup(self):
        # This PTY case isolates the workflow permission picker.  Use HTTPS
        # routes so the independent insecure-transport gate does not require
        # a second modal confirmation in the same fixture.
        return [
            {"seat": "Qwen", "id": "qwen-test", "gateway": "deepinfer"},
            {"seat": "DeepSeek", "id": "deepseek-test",
             "gateway": "deepinfer"},
        ]

class View:
    renderer = None
    workflow_manager = Manager()

    def list_workflows(self):
        return self.workflow_manager.list()

print("ANIMATION_START", flush=True)
zylab._play_workflow_launch(View(), record)
print("LIST_START", flush=True)
zylab.cmd_workflow(View(), "list")
print("DETAIL_START", flush=True)
zylab._print_workflow(record)

class Agent:
    last_total = 0

cfg = json.loads(json.dumps(zylab.CFG.DEFAULTS))
session = zylab.Session(Agent(), cfg)
session.workflow_manager = Manager()
allowed = session.confirm("workflow", {
    "goal": "PTY confirmation smoke",
    "agents": [{"task": "a"}, {"task": "b"}],
})
print("RESULT:" + json.dumps({
    "allowed": allowed,
    "permission": cfg["permissions"]["workflow"],
}, ensure_ascii=False))
""",
            [PTYSend(payload=b"\r", after="Enter 确认", timeout=3.0)],
            timeout=6.0,
            return_output=True,
        )

        plain = tui._ANSI.sub("", output).replace("\r", "")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["permission"], "ask")
        self.assertIn("\x1b[2K", output)
        self.assertIn("wf-pty-smoke", plain)
        self.assertIn("PTY workflow smoke", plain)
        self.assertIn("PTY_VISIBLE_SYNTHESIS", plain)
        self.assertIn("reasoning 已隐藏", plain)
        self.assertNotIn("PTY_PRIVATE_REASONING", plain)

    def test_tool_result_folds_to_two_lines_and_expand_restores_200_lines(self):
        output, result = self.run_child(
            r"""
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import zylab
from core import tasks

payload = "\n".join(f"line-{index:03d}" for index in range(200))
with tempfile.TemporaryDirectory() as tmp:
    manager = tasks.TaskManager(
        Path(tmp) / "artifacts",
        lambda *_args, **_kwargs: payload,
        session_getter=lambda: "pty-fold-session",
        id_factory=lambda: "task200")
    terminal = list(manager.run(
        "bash", {}, task_key="call-200"))[-1]
    messages = [{
        "role": "tool",
        "tool_call_id": "call-200",
        "content": terminal["v"],
    }]
    before = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    session = SimpleNamespace(
        task_manager=manager,
        ag=SimpleNamespace(messages=messages))

    print("FOLD_START")
    print("● bash  generate 200 lines")
    zylab.show_result(
        "bash", terminal["v"], secs=0.2,
        status=terminal["t"], task=terminal["task"],
        task_id=terminal["task_id"], tool_call_id="call-200")
    print("FOLD_END")
    print("EXPAND_START")
    zylab.cmd_expand(session, "call-200")
    print("EXPAND_END")
    after = json.dumps(messages, ensure_ascii=False, sort_keys=True)
print("RESULT:" + json.dumps({"messages_unchanged": before == after}))
""",
            [], return_output=True)

        plain = tui._ANSI.sub("", output).replace("\r", "")
        folded = plain.split("FOLD_START\n", 1)[1].split(
            "FOLD_END", 1)[0]
        folded_lines = [
            line for line in folded.splitlines() if line.strip()]
        expanded = plain.split("EXPAND_START\n", 1)[1].split(
            "EXPAND_END", 1)[0]

        self.assertEqual(len(folded_lines), 2, folded)
        self.assertIn("exit 0 · 200 lines", folded)
        self.assertIn("/expand", folded)
        self.assertIn("line-000", expanded)
        self.assertIn("line-199", expanded)
        self.assertTrue(result["messages_unchanged"])

    def test_real_mouse_wheel_click_pageup_and_escape_navigation(self):
        result = self.run_child(
            r"""
events = []
with tui.InputPump() as pump:
    print("ZYLAB_MOUSE_PUMP_READY", flush=True)
    deadline = time.monotonic() + 3
    while len(events) < 4 and time.monotonic() < deadline:
        event = pump.get(0.2)
        if event and event.kind.startswith("history_"):
            value = (
                event.value.kind
                if isinstance(event.value, tui.MouseEvent)
                else event.value)
            events.append([event.kind, value])
print("RESULT:" + json.dumps(events, ensure_ascii=False))
""",
            [PTYSend(
                 b"\x1b[<64;12;7M",
                 after="ZYLAB_MOUSE_PUMP_READY"),
             (0.06, b"\x1b[<0;4;1M"),
             (0.06, b"\x1b[5~"),
             (0.06, b"\x1b")])

        self.assertEqual(result, [
            ["history_scroll", 3],
            ["history_click", "press"],
            ["history_scroll", "page_up"],
            ["history_close", None],
        ])

    def test_real_pty_live_composer_drag_and_copy(self):
        result = self.run_child(
            r"""
import os

os.environ["TERM"] = "xterm-256color"
renderer = tui.TerminalRenderer()
snapshot = tui.InputSnapshot(
    mode="line", prompt="› ", text="hello 世界", cursor=8)
events = []
copied = False
with tui.InputPump() as pump:
    renderer.enable_mouse()
    renderer.render(snapshot)
    print("COMPOSER_DRAG_READY", flush=True)
    deadline = time.monotonic() + 3
    while not copied and time.monotonic() < deadline:
        event = pump.get(0.2)
        if event is None:
            continue
        if event.kind == "composer_mouse":
            events.append(event.value.kind)
            result = renderer.composer_mouse(
                event.value, snapshot,
                cursor_screen_row=pump.cursor_screen_row)
            pump.set_composer_selection_active(
                renderer.composer_selection_active)
        elif event.kind == "composer_copy":
            copied = renderer.copy_composer_selection(snapshot)
            pump.set_composer_selection_active(
                renderer.composer_selection_active)
            events.append("copy")
    selected = renderer._composer_selection_text()
    renderer.disable_mouse()
print("RESULT:" + json.dumps({
    "events": events,
    "selected": selected,
    "copied": copied,
}, ensure_ascii=False))
""",
            [
                PTYSend(
                    b"\x1b[2;1R",
                    after="COMPOSER_DRAG_READY"),
                PTYSend(b"\x1b[<0;3;2M", delay=0.05),
                PTYSend(b"\x1b[<32;9;2M", delay=0.05),
                PTYSend(b"\x1b[<0;9;2m", delay=0.05),
                PTYSend(b"\x03", delay=0.05),
            ],
        )
        self.assertEqual(result, {
            "events": ["press", "motion", "release", "copy"],
            "selected": "hello",
            "copied": True,
        })

    def test_real_up_browses_past_slash_command_history(self):
        result = self.run_child(
            r"""
states = []
with tui.InputPump(
        commands={"/help": "帮助", "/hooks": "hooks"},
        history=["oldest prompt", "/help", "latest prompt"]) as pump:
    deadline = time.monotonic() + 3
    while len(states) < 3 and time.monotonic() < deadline:
        event = pump.get(0.2)
        if (event and event.kind == "redraw" and event.snapshot.text):
            states.append([
                event.snapshot.text, len(event.snapshot.options)])
print("RESULT:" + json.dumps(states, ensure_ascii=False))
""",
            [(0.12, b"\x1b[A\x1b[A\x1b[A")])
        self.assertEqual(result, [
            ["latest prompt", 0],
            ["/help", 0],
            ["oldest prompt", 0],
        ])

    def test_utf8_tab_and_ctrl_c_share_one_reader(self):
        result = self.run_child(
            r"""
events = []
with tui.InputPump() as pump:
    pump.set_busy(True)
    deadline = time.monotonic() + 3
    while len(events) < 2 and time.monotonic() < deadline:
        event = pump.get(0.2)
        if event and event.kind in ("submit", "cancel"):
            events.append([event.kind, event.text, event.mode])
print("RESULT:" + json.dumps(events, ensure_ascii=False))
""",
            [(0.12, "你好".encode("utf-8") + b"\t"),
             (0.08, b"\x03")])
        self.assertEqual(result, [
            ["submit", "你好", "next_turn"],
            ["cancel", "", None],
        ])

    def test_repl_delivers_enter_steer_then_tab_next_turn(self):
        output, result = self.run_child(
            r"""
import os
import tempfile
import threading

with tempfile.TemporaryDirectory() as home:
    os.environ["HOME"] = home
    os.environ["TERM"] = "xterm-256color"
    os.environ["ZYLAB_MOUSE"] = "1"
    import zylab
    from core import agent as agent_mod, client, store
    from tests.pty_harness import announce_when, install_ready_input_pump

    install_ready_input_pump()

    store.ensure_home()
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.model = "m"
    agent.gateway = "test"
    agent.session_id = "pty-session"
    agent.messages = [{"role": "system", "content": "s"}]
    agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
    agent.cache_read = agent.cache_write = 0
    agent.cache_reported = False
    agent.compact_failed = None
    agent.ctx_limit = 100000
    agent.compact_at = 70000
    agent.ctx_known = True
    agent.ctx_limit_source = "test"
    agent.context_summary = None
    agent.context_invalid_reason = None
    agent._compact_failed_key = None
    agent._last_age_notice_key = None
    agent._seen_ok_hi = 0
    agent.started = time.time()

    calls = {"n": 0}
    release_first = threading.Event()
    def fake_stream(model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            print("ZYLAB_TEST_CALL_1_STARTED", flush=True)
            release_first.wait()
        if calls["n"] == 3:
            yield {
                "t": "text",
                "v": (
                    "# PTY 标题\n> 引用\n"
                    "| 名称 | 值 |\n|---|---:|\n| 中文 | 42 |\n"
                    "```py"),
            }
            yield {"t": "text", "v": "thon\nprint('你好')\n```\n"}
        else:
            yield {"t": "text", "v": "R" + str(calls["n"])}
        yield {"t": "done", "reason": "stop", "usage": {}}

    client.stream_chat = fake_stream
    session = zylab.Session(agent)
    agent.confirm = session.confirm
    announce_when(
        lambda: len(session.controller.queued) >= 2,
        "ZYLAB_TEST_INPUTS_QUEUED",
        on_ready=release_first.set)
    announce_when(
        lambda: (calls["n"] >= 3
                 and session.controller.current_turn_id is None
                 and not session.controller.queued
                 and not session.pump.snapshot().busy),
        "ZYLAB_TEST_IDLE_AFTER_3")
    zylab.repl(session, "m")
    users = [
        message["content"] for message in agent.messages
        if message.get("role") == "user"
    ]
    modes = [
        event["payload"].get("mode")
        for event in session.controller.journal.events
        if event["kind"] == "user_message"
    ]
    result = {"calls": calls["n"], "users": users, "modes": modes}
print("RESULT:" + json.dumps(result, ensure_ascii=False))
""",
            [PTYSend(
                 b"first\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(
                 b"steer\rqueued\t",
                 after="ZYLAB_TEST_CALL_1_STARTED"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_IDLE_AFTER_3", delay=0.08)],
            timeout=6.0,
            return_output=True)
        self.assertEqual(result["calls"], 3, result)
        self.assertEqual(result["users"], ["first", "steer", "queued"])
        self.assertEqual(
            result["modes"], ["next_turn", "steer", "next_turn"])
        plain = tui._ANSI.sub("", output)
        primary_plain = tui._ANSI.sub(
            "", output.split("\x1b[?1049h", 1)[0])
        # Busy inputs first live in the composer; after durable dispatch each
        # one must move into scrollback exactly once as a normal prompt.
        # The initial prompt also remains in raw PTY capture before its composer
        # frame is erased. Busy composer lines use a different prompt, so these
        # patterns uniquely identify their later scrollback commits.
        scrollback_prompts = {}
        for prompt_text in ("steer", "queued"):
            matches = list(re.finditer(
                rf"(?<!\w)› {re.escape(prompt_text)} *\r+\n",   # 用户行整行铺底，行尾补空格
                primary_plain))
            self.assertEqual(len(matches), 1, (prompt_text, primary_plain))
            scrollback_prompts[prompt_text] = matches[0]
        self.assertLess(
            primary_plain.index("R1"), scrollback_prompts["steer"].start())
        self.assertLess(
            scrollback_prompts["steer"].end(), primary_plain.index("R2"))
        self.assertIn("⏺ R2", primary_plain)
        self.assertLess(
            primary_plain.index("R2"), scrollback_prompts["queued"].start())
        self.assertLess(
            scrollback_prompts["queued"].end(),
            primary_plain.index("PTY 标题"))
        self.assertIn("  ┌─ python", plain)
        self.assertIn("  │ print('你好')", plain)
        self.assertIn("  └─", plain)
        self.assertIn("⏺ ◆ PTY 标题", plain)   # C6：首行标题由 ⏺ 托管，一格间隔
        self.assertIn("  │ 引用", plain)
        self.assertIn("│中文", plain)
        self.assertNotIn("```python", plain)
        self.assertIn("↪ steer", primary_plain)
        self.assertNotIn("↳ ↪ steer", primary_plain)
        # 内联模式回到 I4：不进备用屏、不接管鼠标（历史浏览器已下线；全屏模式见 test_app_pty）
        self.assertNotIn("\x1b[?1049h", output)
        self.assertNotIn("\x1b[?1006h", output)

    def test_queued_exit_does_not_discard_following_turn(self):
        result = self.run_child(
            r"""
import os
import tempfile
import threading

with tempfile.TemporaryDirectory() as home:
    os.environ["HOME"] = home
    import zylab
    from core import agent as agent_mod, client, store
    from tests.pty_harness import announce_when, install_ready_input_pump

    install_ready_input_pump()

    store.ensure_home()
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.model = "m"
    agent.gateway = "test"
    agent.session_id = "pty-exit-queue"
    agent.messages = [{"role": "system", "content": "s"}]
    agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
    agent.cache_read = agent.cache_write = 0
    agent.cache_reported = False
    agent.compact_failed = None
    agent.ctx_limit = 100000
    agent.compact_at = 70000
    agent.ctx_known = True
    agent.ctx_limit_source = "test"
    agent.context_summary = None
    agent.context_invalid_reason = None
    agent._compact_failed_key = None
    agent._last_age_notice_key = None
    agent._seen_ok_hi = 0
    agent.started = time.time()

    calls = {"n": 0}
    release_first = threading.Event()
    def fake_stream(model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            print("ZYLAB_TEST_CALL_1_STARTED", flush=True)
            release_first.wait()
        yield {"t": "text", "v": "R" + str(calls["n"])}
        yield {"t": "done", "reason": "stop", "usage": {}}

    client.stream_chat = fake_stream
    session = zylab.Session(agent)
    agent.confirm = session.confirm
    announce_when(
        lambda: len(session.controller.queued) >= 2,
        "ZYLAB_TEST_EXIT_AND_NEXT_QUEUED",
        on_ready=release_first.set)
    announce_when(
        lambda: (calls["n"] >= 2
                 and session.controller.current_turn_id is None
                 and not session.controller.queued
                 and not session.pump.snapshot().busy),
        "ZYLAB_TEST_IDLE_AFTER_2")
    zylab.repl(session, "m")
    result = {
        "calls": calls["n"],
        "users": [
            message["content"] for message in agent.messages
            if message.get("role") == "user"
        ],
        "modes": [
            event["payload"].get("mode")
            for event in session.controller.journal.events
            if event["kind"] == "user_message"
        ],
    }
print("RESULT:" + json.dumps(result, ensure_ascii=False))
""",
            [PTYSend(
                 b"first\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(
                 b"/exit\rqueued\t",
                 after="ZYLAB_TEST_CALL_1_STARTED"),
             PTYSend(
                 b"/exit\r", after="ZYLAB_TEST_IDLE_AFTER_2")],
            timeout=6.0)
        self.assertEqual(result["calls"], 2, result)
        self.assertEqual(result["users"], ["first", "queued"])
        self.assertEqual(result["modes"], ["next_turn", "next_turn"])

    def test_modal_uses_same_input_pump_for_arrows(self):
        result = self.run_child(
            r"""
picked = "timeout"
with tui.InputPump() as pump:
    pump.open_picker(["first", "second"], allow_filter=False)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        event = pump.get(0.2)
        if event and event.kind == "picker_result":
            picked = event.value
            break
print("RESULT:" + json.dumps(picked, ensure_ascii=False))
""",
            [(0.12, b"\x1b[B\r")])
        self.assertEqual(result, "second")


if __name__ == "__main__":
    unittest.main(verbosity=2)
