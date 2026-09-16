import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import commands
from core import tui


class CustomCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.user = self.root / "user-commands"
        self.project = self.root / "project"
        self.project_commands = self.project / ".zylab" / "commands"
        self.user.mkdir()
        self.project_commands.mkdir(parents=True)
        self.user_patch = mock.patch.object(commands, "USER_ROOT", self.user)
        self.user_patch.start()

    def tearDown(self):
        self.user_patch.stop()
        self.temp.cleanup()

    def load(self, **kwargs):
        return commands.load(cwd=self.project, **kwargs)

    def test_frontmatter_and_literal_argument_expansion(self):
        (self.user / "review.md").write_text(
            "---\ndescription: Review evidence\nargument-hint: FILE\n---\n"
            "Review this carefully: $ARGUMENTS\n",
            encoding="utf-8")
        catalog = self.load()
        command, expanded = catalog.resolve("/review notes/a.md; echo no")
        self.assertEqual(command.scope, "user")
        self.assertEqual(command.argument_hint, "FILE")
        self.assertEqual(
            expanded, "Review this carefully: notes/a.md; echo no")
        # It is literal prompt data; no shell interpolation or execution layer.
        self.assertIn("; echo no", expanded)

    def test_arguments_append_when_template_has_no_placeholder(self):
        (self.user / "explain.md").write_text(
            "Explain the selected design.", encoding="utf-8")
        _, expanded = self.load().resolve("/explain memory")
        self.assertEqual(
            expanded,
            "Explain the selected design.\n\nArguments:\nmemory")

    def test_builtin_and_user_scope_win_with_visible_conflicts(self):
        (self.user / "status.md").write_text("fake status", encoding="utf-8")
        (self.user / "same.md").write_text("user", encoding="utf-8")
        (self.project_commands / "same.md").write_text(
            "project", encoding="utf-8")
        catalog = self.load(builtins={"status"})
        self.assertNotIn("status", catalog.commands)
        self.assertEqual(catalog.commands["same"].scope, "user")
        winners = {(row["name"], row["winner"])
                   for row in catalog.conflicts}
        self.assertIn(("status", "builtin"), winners)
        self.assertIn(("same", "user"), winners)

    def test_permission_metadata_is_rejected_fail_closed(self):
        (self.project_commands / "unsafe.md").write_text(
            "---\nallowed-tools: bash\n---\ndo it", encoding="utf-8")
        catalog = self.load()
        self.assertNotIn("unsafe", catalog.commands)
        self.assertTrue(any("不支持的 frontmatter" in row
                            for row in catalog.errors))

    def test_symlink_command_is_rejected(self):
        target = self.root / "outside.md"
        target.write_text("outside", encoding="utf-8")
        (self.project_commands / "linked.md").symlink_to(target)
        catalog = self.load()
        self.assertNotIn("linked", catalog.commands)
        self.assertTrue(any("符号链接" in row for row in catalog.errors))

    def test_directory_and_template_reads_are_bounded(self):
        for name in ("a.md", "b.md", "c.md"):
            (self.user / name).write_text(name, encoding="utf-8")
        with mock.patch.object(commands, "MAX_COMMAND_FILES", 2):
            catalog = self.load()
        self.assertEqual(sorted(catalog.commands), ["a", "b"])
        self.assertTrue(any("files 超过上限 2" in row
                            for row in catalog.errors))

        (self.user / "huge.md").write_text("12345", encoding="utf-8")
        with mock.patch.object(commands, "MAX_TEMPLATE_BYTES", 4):
            catalog = self.load()
        self.assertNotIn("huge", catalog.commands)
        self.assertTrue(any("超过 4 bytes" in row
                            for row in catalog.errors))

    def test_line_editor_completion_can_refresh_without_restart(self):
        editor = tui.LineEditor(commands={"/help": "built in"})
        editor.replace("/r")
        self.assertEqual(editor.snapshot().options, ())
        editor.set_commands({"/help": "built in", "/review": "custom"})
        snapshot = editor.snapshot()
        self.assertEqual(snapshot.options, (("/review", "custom"),))


if __name__ == "__main__":
    unittest.main()
