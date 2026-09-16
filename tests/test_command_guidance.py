"""Dynamic slash-parameter guidance and command/schema boundary tests."""
import copy
import io
import os
import tempfile
import types
import unittest
from unittest import mock

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import zylab as CLI
from core import tui


class _Memory:
    def list(self, **_kwargs):
        return [
            {"id": "mem-abc123", "scope": "project", "kind": "explicit"},
            {"id": "mem-def456", "scope": "global", "kind": "session_handoff"},
        ]


class _Task:
    def __init__(self, identifier, status):
        self.id = identifier
        self.status = status


class _Tasks:
    def list(self):
        return [_Task("task-abc123", "running")]


class _GuidanceSession:
    def __init__(self):
        self.ag = types.SimpleNamespace(session_id="s-current")
        self._session_cwd = tempfile.gettempdir()
        self._workflow_current = None
        self._command_guidance_catalog = None
        self._command_guidance_signature = None
        self._command_guidance_at = 0.0
        self.memory_store = _Memory()
        self.task_manager = _Tasks()
        self.workflow = {
            "id": "wf-abc123",
            "state": "running",
            "stage": "agents",
            "plan": {"agents": [
                {"key": "evidence", "state": "queued", "seat": "Qwen"},
                {"key": "unsafe key", "state": "queued", "seat": "GLM"},
            ]},
        }

    def list_workflows(self, limit=100):
        return [self.workflow][:limit]

    def workflow_record(self):
        return self.workflow

    def list_agents(self, limit=100):
        return [{
            "id": "a-abc123", "state": "running",
            "model": "flagship", "gateway": "boyue",
            "name": "DO-NOT-LEAK-TASK-TEXT",
        }][:limit]

    def list_consults(self, limit=100):
        return [{
            "id": "cs-abc123", "state": "queued",
            "source_snapshot": {"session_id": "s-other"},
        }][:limit]

    def refresh_command_guidance(self, **kwargs):
        return CLI.Session.refresh_command_guidance(self, **kwargs)


class DynamicGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.session = _GuidanceSession()
        self.recipe_rows = [
            {"name": "same-recipe", "scope": "user", "params": {}},
            {"name": "same-recipe", "scope": "project", "params": {"goal": {}}},
            {"name": "research-review", "scope": "builtin", "params": {"goal": {}}},
            {"name": "bad recipe", "scope": "user", "params": {}},
        ]
        self.sessions = [
            {"id": "s-other", "status": "active", "model": "m", "gateway": "g",
             "title": "DO-NOT-LEAK-TITLE"},
            {"id": "s-current", "status": "active", "model": "m", "gateway": "g"},
            {"id": "bad id/with-space", "status": "active", "model": "m", "gateway": "g"},
        ]

    def patch_sources(self):
        return mock.patch.multiple(
            CLI.RECIPES,
            list_recipes=mock.Mock(return_value=self.recipe_rows),
        ), mock.patch.object(
            CLI.store, "list_session_summaries",
            return_value=self.sessions,
        ), mock.patch.object(
            CLI.store, "session_lease",
            side_effect=AssertionError(
                "guidance must not create per-session lease locks"),
        ), mock.patch.object(
            CLI.store, "active_session_leases",
            return_value={},
        ), mock.patch.object(
            CLI.CHECKPOINTS, "list_checkpoint_ids_readonly",
            return_value=[{"checkpoint_id": "cp-abc123", "files": 2}],
        )

    def test_dynamic_rows_are_safe_detached_and_handler_specific(self):
        static_before = copy.deepcopy(CLI.COMMAND_SUBCOMMANDS)
        patches = self.patch_sources()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            catalog = CLI._session_command_guidance(self.session)

        self.assertEqual(CLI.COMMAND_SUBCOMMANDS, static_before)
        self.assertIsNot(catalog, CLI.COMMAND_SUBCOMMANDS)
        self.assertIn(
            ("wf-abc123", "running · agents"),
            catalog["/workflow"]["params"]["status"]["items"],
        )
        restart = catalog["/workflow"]["params"]["restart"]["items"]
        self.assertEqual(restart, (("evidence", "queued · Qwen"),))
        recipes = catalog["/workflow"]["params"]["run"]["items"]
        self.assertIn(("research-review", "builtin · 1 params"), recipes)
        self.assertIn(("user:same-recipe", "user · 0 params"), recipes)
        self.assertIn(("project:same-recipe", "project · 1 params"), recipes)
        self.assertIn(
            ("a-abc123", "running · flagship@boyue"),
            catalog["/agents"]["params"]["attach"]["items"],
        )
        self.assertIn(
            ("cs-abc123", "queued · source s-other"),
            catalog["/consult"]["params"]["open"]["items"],
        )
        self.assertIn(
            ("s-other", "active · m@g"),
            catalog["/consult"]["params"]["start"]["items"],
        )
        self.assertIn(
            ("mem-abc123", "project · explicit"),
            catalog["/memory"]["params"]["show"]["items"],
        )
        self.assertIn(("task-abc123", "running"), catalog["/task"]["items"])
        self.assertIn(("cp-abc123", "2 files"), catalog["/rewind"]["items"])
        rendered_text = repr(catalog)
        self.assertNotIn("DO-NOT-LEAK", rendered_text)
        self.assertNotIn("unsafe key", rendered_text)
        self.assertNotIn("bad id/with-space", rendered_text)

    def test_namespaced_recipe_completion_is_accepted_and_stays_one_level(self):
        editor = tui.LineEditor(
            subcommands={
                "/workflow": {
                    "items": (("run", "run"),),
                    "params": {"run": {
                        "items": (("project:same-recipe", "project"),),
                    }},
                },
            })
        editor.replace("/workflow run project:s")
        self.assertEqual(
            editor.snapshot().options,
            (("/workflow run project:same-recipe", "project"),),
        )
        editor.handle("tab")
        self.assertEqual(editor.text, "/workflow run project:same-recipe ")
        self.assertEqual(editor.snapshot().options, ())

    def test_guidance_readers_are_bounded_and_reject_oversized_tokens(self):
        rows = CLI._guidance_records(
            lambda **_kwargs: (
                {"id": f"row-{index}"} for index in range(10_000)),
            limit=3)
        self.assertEqual(len(rows), 3)
        self.assertIsNone(CLI._guidance_token("x" * 10_000))

    def test_refresh_is_throttled_and_does_not_jump_selection(self):
        pump = tui.InputPump(
            stream=io.StringIO(),
            subcommands=CLI.COMMAND_SUBCOMMANDS,
        )
        self.session.pump = pump
        calls = {"workflows": 0}

        original = self.session.list_workflows

        def counted(limit=100):
            calls["workflows"] += 1
            return original(limit)

        self.session.list_workflows = counted
        patches = self.patch_sources()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            self.session.refresh_command_guidance(force=True)
            first_calls = calls["workflows"]
            self.session.refresh_command_guidance()
            self.assertEqual(calls["workflows"], first_calls)

            pump.editor.replace("/workflow status ")
            pump.editor.menu_index = 1
            self.session.workflow = {
                **self.session.workflow,
                "id": "wf-new456",
            }
            self.session._workflow_extra = {
                "id": "wf-second7", "state": "queued", "stage": "agents",
                "plan": {"agents": []},
            }
            self.session.list_workflows = lambda limit=100: [
                self.session.workflow, self.session._workflow_extra,
            ][:limit]
            self.session.refresh_command_guidance(force=True)

        self.assertEqual(pump.editor.menu_index, 1)
        self.assertTrue(any(
            "wf-new456" in name
            for name, _ in pump.editor.snapshot().options))


class StaticGuidanceConsistencyTests(unittest.TestCase):
    def test_every_static_parameter_parent_is_a_visible_second_level_item(self):
        normalized = tui.LineEditor._normalize_subcommands(
            CLI.COMMAND_SUBCOMMANDS)
        self.assertEqual(set(normalized), set(CLI.COMMAND_SUBCOMMANDS))
        for command, spec in normalized.items():
            names = {name.casefold() for name, _ in spec["items"]}
            for parent, child in (spec.get("params") or {}).items():
                with self.subTest(command=command, parent=parent):
                    self.assertIn(parent.casefold(), names)
                    self.assertTrue(child["items"])
                    self.assertTrue(all(
                        name and description
                        for name, description in child["items"]))


if __name__ == "__main__":
    unittest.main()
