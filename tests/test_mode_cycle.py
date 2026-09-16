"""B1（SPEC-CC-parity）：Shift+Tab 循环权限模式 —— 编辑器层。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest import mock as _mock
from core import tui


class ShiftTabEditorTests(unittest.TestCase):
    def test_shift_tab_emits_cycle_mode_in_line_mode(self):
        editor = tui.LineEditor()
        editor.handle("h")
        events = editor.handle("shift-tab")
        self.assertEqual([e.kind for e in events], ["cycle_mode"])
        self.assertEqual(editor.text, "h", "草稿不受影响")

    def test_shift_tab_with_open_command_menu_is_left_to_the_menu(self):
        editor = tui.LineEditor(commands={"/help": "帮助", "/hooks": "hooks"})
        editor.handle("/"); editor.handle("h")
        self.assertTrue(editor._matches())
        events = editor.handle("shift-tab")
        self.assertNotIn("cycle_mode", [e.kind for e in events])

    def test_shift_tab_works_while_busy(self):
        editor = tui.LineEditor(); editor.busy = True
        self.assertEqual([e.kind for e in editor.handle("shift-tab")], ["cycle_mode"])



# 受保护路径默认为空 —— 测试必须自己声明要守的东西，否则会在维护者机器上因为
# 读到用户配置而意外变绿、在别人 clone 下来的仓库里变红。
#
# 只打 tools.PROTECTED（词法守卫），**不设环境变量**：环境变量会同时喂给沙箱层，
# 而 sandbox 要求受保护路径真实存在，合成路径必然不存在，会把沙箱打成 fail-closed。
# PROTECTED 虽是 import 时赋值，但守卫是调用时查模块全局，所以 patch 与 import 顺序无关。
# tools 在函数内 import：不是每个用到守卫的测试模块都在顶层 import 它。
_protected_patches = []


def setUpModule():
    from core import tools as _guarded
    for name, value in (("PROTECTED", ("/protected/archive",)),
                        ("PROTECTED_REMOTES", ("archive:bucket",))):
        patch = _mock.patch.object(_guarded, name, value)
        patch.start()
        _protected_patches.append(patch)


def tearDownModule():
    while _protected_patches:
        _protected_patches.pop().stop()

if __name__ == "__main__":
    unittest.main()


import json
import tempfile
import time
from unittest import mock


def _session(tmp):
    import zylab
    from core import agent as agent_mod
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.model = "m"; agent.gateway = "deepinfer"; agent.session_id = "mode-unit"
    agent.messages = [{"role": "system", "content": "s"}]
    agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
    agent.cache_read = agent.cache_write = 0
    agent.cache_reported = False; agent.compact_failed = None
    agent.ctx_limit = 100000; agent.compact_at = 70000
    agent.ctx_known = True; agent.ctx_limit_source = "test"
    agent.context_summary = None; agent.context_invalid_reason = None
    agent._compact_failed_key = None; agent._last_age_notice_key = None
    agent._seen_ok_hi = 0; agent.started = time.time()
    return zylab.Session(agent)


class AcceptEditsDecisionTests(unittest.TestCase):
    """accept-edits 只放编辑工具；bash 照旧；plan 优先；硬守卫之外。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev = os.getcwd(); os.chdir(self.tmp.name)
        with open("f.txt", "w") as fh:
            fh.write("alpha\n")
        os.environ["HOME"] = self.tmp.name
        self.sess = _session(self.tmp.name)

    def tearDown(self):
        os.chdir(self.prev); self.tmp.cleanup()

    def _decide(self, name, args):
        from core import tools
        prepared = tools.prepare(name, args)
        return self.sess.permission_decision(name, prepared)

    def test_default_mode_does_not_auto_allow_edits(self):
        d = self._decide("edit_file", {"path": "f.txt", "old": "alpha", "new": "beta"})
        self.assertNotEqual(d.get("decision"), "accept_edits")

    def test_accept_edits_allows_edit_file_but_not_bash(self):
        self.assertEqual(self.sess.cycle_permission_mode(), "accept-edits")
        d = self._decide("edit_file", {"path": "f.txt", "old": "alpha", "new": "beta"})
        self.assertTrue(d["allowed"]); self.assertEqual(d["decision"], "accept_edits")
        b = self._decide("bash", {"command": "echo hi"})
        self.assertNotEqual(b.get("decision"), "accept_edits")

    def test_plan_mode_overrides_accept_edits(self):
        self.sess.accept_edits = True
        self.sess.plan_mode = True
        self.assertEqual(self.sess.permission_mode, "plan")
        self.assertFalse(self.sess._accept_edits_applies("edit_file"))

    def test_cycle_order_and_flags(self):
        s = self.sess
        with mock.patch("builtins.print"):
            self.assertEqual(s.cycle_permission_mode(), "accept-edits")
            self.assertEqual(s.cycle_permission_mode(), "plan")
            self.assertTrue(s.plan_mode); self.assertFalse(s.accept_edits)
            self.assertEqual(s.cycle_permission_mode(), "default")
            self.assertFalse(s.plan_mode); self.assertFalse(s.accept_edits)

    def test_hard_guard_is_outside_every_mode(self):
        from core import checkpoints, tools
        self.sess.accept_edits = True
        # 硬守卫在 prepare 阶段就拦（checkpoints.UnsafePathError），根本到不了任何模式的审批。
        with self.assertRaises((tools.Denied, checkpoints.UnsafePathError)):
            tools.prepare("write_file", {"path": "/protected/archive/probe.txt", "content": "x"})
