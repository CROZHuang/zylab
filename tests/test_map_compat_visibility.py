"""The legacy /map path is hidden from discovery but remains callable."""
import contextlib
import io
import os
import tempfile
import types
import unittest

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import zylab as CLI
from core import tui


class MapCompatibilityVisibilityTests(unittest.TestCase):
    def test_map_is_hidden_from_top_level_discovery(self):
        self.assertIn("map", CLI.REGISTRY)
        self.assertNotIn("/map", CLI.COMMANDS)
        self.assertIn("/graft", CLI.COMMANDS)

        editor = tui.LineEditor(
            commands=CLI.COMMANDS, subcommands=CLI.COMMAND_SUBCOMMANDS)
        editor.replace("/")
        self.assertNotIn(
            "/map", {name for name, _ in editor._matches()})
        editor.replace("/ma")
        self.assertEqual(editor.snapshot().options, ())

        # Hiding discovery must not break the resolver's established
        # unique-prefix compatibility contract.
        canonical, entry, exit_alias = CLI.resolve_command("ma")
        self.assertEqual(canonical, "map")
        self.assertIs(entry[0], CLI.cmd_map)
        self.assertFalse(exit_alias)

    def test_help_hides_map_while_explicit_resolver_stays_compatible(self):
        session = types.SimpleNamespace(_custom_catalog=None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            CLI.cmd_help(session, "")
        help_text = output.getvalue()
        self.assertNotIn("/map", help_text)
        self.assertIn("/graft", help_text)

        canonical, entry, exit_alias = CLI.resolve_command("map")
        self.assertEqual(canonical, "map")
        self.assertIs(entry[0], CLI.cmd_map)
        self.assertFalse(exit_alias)

    def test_map_parameter_guidance_and_deprecation_notice_remain_available(self):
        editor = tui.LineEditor(
            commands=CLI.COMMANDS, subcommands=CLI.COMMAND_SUBCOMMANDS)
        editor.replace("/map ")
        self.assertIn(
            ("/map status", "显示 deterministic map 状态"),
            editor.snapshot().options)

        session = types.SimpleNamespace(
            _session_cwd=tempfile.gettempdir(), cfg=CLI.CFG.DEFAULTS)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            # Avoid touching the workspace while checking the compatibility
            # banner; the command's real resolver path is tested above.
            original = CLI._map_usage
            try:
                CLI._map_usage = lambda: None
                CLI.cmd_map(session, "help")
            finally:
                CLI._map_usage = original
        self.assertIn("/map 已从顶层入口隐藏", output.getvalue())


if __name__ == "__main__":
    unittest.main()
