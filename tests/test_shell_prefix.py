"""A8（SPEC-CC-parity）：`!` 前缀直跑 shell，不经模型 —— controller 层契约。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import controller as C


class ShellPrefixTests(unittest.TestCase):
    def test_shell_mode_dispatches_run_shell_not_a_model_turn(self):
        ctl = C.SessionController("s", C.MemoryJournal())
        action = ctl.submit("!ls -la", C.QueueMode.SHELL)
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, C.ActionKind.RUN_SHELL)
        self.assertNotEqual(action.kind, C.ActionKind.START_TURN)

    def test_next_turn_mode_is_a_model_turn(self):
        ctl = C.SessionController("s", C.MemoryJournal())
        action = ctl.submit("hello", C.QueueMode.NEXT_TURN)
        self.assertEqual(action.kind, C.ActionKind.START_TURN)


if __name__ == "__main__":
    unittest.main()
