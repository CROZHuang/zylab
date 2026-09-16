"""Instruction-loading flag and prompt-layer regression tests."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab as CLI
from core import agent


class InstructionLoadingTests(unittest.TestCase):
    def test_legacy_load_md_backfills_all_three_layers(self):
        self.assertEqual(
            agent.instruction_flags(False),
            {
                "load_user_md": False,
                "load_project_md": False,
                "load_skills": False,
            })
        self.assertEqual(
            agent.instruction_flags(True),
            {
                "load_user_md": True,
                "load_project_md": True,
                "load_skills": True,
            })

    def test_explicit_instruction_layers_override_legacy_independently(self):
        self.assertEqual(
            agent.instruction_flags(
                False, load_user_md=True,
                load_project_md=False, load_skills=True),
            {
                "load_user_md": True,
                "load_project_md": False,
                "load_skills": True,
            })

    def test_system_prompt_controls_user_and_project_md_independently(self):
        with (
                mock.patch.object(
                    agent, "env_context", return_value="<env-test>"),
                mock.patch.object(
                    agent, "user_instructions",
                    return_value="<user-md-sentinel>"),
                mock.patch.object(
                    agent, "project_instructions",
                    return_value="<project-md-sentinel>")):
            project_only = agent.system_prompt(
                load_md=False,
                load_user_md=False, load_project_md=True)
            user_only = agent.system_prompt(
                load_md=False,
                load_user_md=True, load_project_md=False)

        self.assertNotIn("<user-md-sentinel>", project_only)
        self.assertIn("<project-md-sentinel>", project_only)
        self.assertIn("<user-md-sentinel>", user_only)
        self.assertNotIn("<project-md-sentinel>", user_only)

    def test_restore_legacy_session_backfills_new_flags_from_load_md(self):
        runtime = SimpleNamespace(
            load_md=True, messages=[], session_id="current-session",
            gateway="deepinfer", tokens_in=0, tokens_out=0, last_total=0,
            load_context=lambda _value: None)
        sess = SimpleNamespace(
            ag=runtime, cfg=CLI.CFG.DEFAULTS,
            _pending_route=None, last_ctx=None, always=set())
        record = {
            "id": "legacy-session", "title": "legacy",
            "messages": [{"role": "system", "content": "old"}],
            "load_md": False,
        }

        with mock.patch.dict(CLI.tools.HOOK_CTX, {}, clear=True):
            CLI.restore_session_record(sess, record)

        self.assertFalse(runtime.load_md)
        self.assertFalse(runtime.load_user_md)
        self.assertFalse(runtime.load_project_md)
        self.assertFalse(runtime.load_skills)


if __name__ == "__main__":
    unittest.main(verbosity=2)
