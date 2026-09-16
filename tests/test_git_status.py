import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import zylab as CLI
from core import store


class GitStatusTests(unittest.TestCase):
    def setUp(self):
        store._REPO_CACHE.clear()

    def tearDown(self):
        store._REPO_CACHE.clear()

    def test_repo_context_parses_porcelain_and_caches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probes = [
                subprocess.CompletedProcess(
                    [], 0, stdout=f"{root}\nfeature/research\n", stderr=""),
                subprocess.CompletedProcess(
                    [], 0,
                    stdout=("M  staged.py\n M unstaged.py\n?? new.txt\n"
                            "MM both.py\n"), stderr=""),
            ]
            with mock.patch.object(
                    store.subprocess, "run", side_effect=probes) as run:
                first = store.repo_context(root)
                second = store.repo_context(root)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(first, second)
        self.assertEqual(first["git_branch"], "feature/research")
        self.assertTrue(first["git_dirty"])
        self.assertEqual(first["git_staged"], 2)
        self.assertEqual(first["git_unstaged"], 2)
        self.assertEqual(first["git_untracked"], 1)

    def test_status_line_marks_dirty_branch(self):
        agent = SimpleNamespace(
            session_id="session123", model="model", gateway="gateway",
            supports_tools=True, compact_at=256_000)
        session = SimpleNamespace(
            ag=agent, _title="Research", _pending_route=None,
            last_ctx=None, plan_mode=False, auto=False,
            workflow_auto=False, memory_use=False, task_plan=None,
            _sandbox_status={
                "marker": "SANDBOXED", "network_marker": "NET OPEN"},
            attached_agent_id=None, _consult_current=None,
            _workflow_current=None, attached_task_id=None,
            _session_cwd="/tmp/workspace/project")
        with mock.patch.object(CLI.store, "repo_context", return_value={
                "git_branch": "main", "git_dirty": True}):
            footer = CLI.status_line(session)
        self.assertIn("git main *", footer)
        self.assertIn("project", footer)


if __name__ == "__main__":
    unittest.main()
