"""F3：工具执行跟随逻辑 workspace（ExecutionContext.workspace_root），而不是进程 cwd。

历史：父/子 agent、consult、workflow 记录的 cwd 只进了 prompt，工具一律回落到 os.getcwd()。
本文件原是 tests/repro_workspace_cwd.py（故意不入套件的红色复现），修好后改名入套件。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import tools


class WorkspaceIsNotProcessCwd(unittest.TestCase):
    def setUp(self):
        self.A = tempfile.mkdtemp(prefix="core-process-")
        self.B = tempfile.mkdtemp(prefix="core-logical-")
        for root, body in ((self.A, "OTHER\n"), (self.B, "LOGICAL\n")):
            with open(os.path.join(root, "f.txt"), "w", encoding="utf-8") as f:
                f.write(body)
        self.prev = os.getcwd()
        os.chdir(self.A)
        self.ctx = tools.ExecutionContext.capture(session="s", workspace_root=self.B)

    def tearDown(self):
        os.chdir(self.prev)
        shutil.rmtree(self.A, ignore_errors=True)
        shutil.rmtree(self.B, ignore_errors=True)

    def test_capture_defaults_to_process_cwd_and_canonicalizes(self):
        self.assertEqual(tools.ExecutionContext.capture(session="s").workspace_root,
                         os.path.realpath(self.A))
        self.assertEqual(self.ctx.workspace_root, os.path.realpath(self.B))

    def test_read_follows_logical_workspace(self):
        out = tools.t_read_file("f.txt", _execution_context=self.ctx)
        self.assertIn("LOGICAL", out, "读到了进程 cwd 的文件，而不是逻辑 workspace 的")

    def test_prepare_takes_its_root_from_the_context(self):
        prepared = tools.prepare("read_file", {"path": "f.txt"}, context=self.ctx)
        self.assertEqual(prepared.workspace_root, os.path.realpath(self.B))

    def test_reading_across_workspaces_is_refused(self):
        try:
            out = tools.t_read_file(os.path.join(self.A, "f.txt"), _execution_context=self.ctx)
        except tools.Denied as exc:
            out = f"[denied] {exc}"
        self.assertNotIn("OTHER", out, "越过逻辑 workspace 读到了进程 cwd 的文件")

    def test_unsandboxed_bash_runs_in_the_logical_workspace(self):
        try:
            out = tools.t_bash("pwd", _execution_context=self.ctx)
        except tools.Denied as exc:      # 沙箱策略不允许时也不能悄悄跑在错的目录
            self.skipTest(f"bash 在本环境被拒绝：{exc}")
        self.assertIn(os.path.realpath(self.B), out, out[-400:])
        self.assertNotIn(os.path.realpath(self.A) + "\n", out)


if __name__ == "__main__":
    unittest.main()
