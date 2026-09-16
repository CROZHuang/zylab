"""/graft build 前先说规模与预期时长：首次索引只有一个 spinner，像卡住（2026-09-04 反馈）。"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab


class GraftBuildHintTests(unittest.TestCase):
    def run_build(self, indexed, action="build"):
        sess = mock.Mock()
        sess.cfg = {}
        sess._session_cwd = "/tmp/proj"
        out = io.StringIO()
        with mock.patch.object(zylab.GRAFT, "local_status", return_value={
                    "available": True, "indexed": indexed, "root": "/tmp/proj"}), \
             mock.patch.object(zylab.GRAFT, "inventory", return_value={"files": 164, "bytes": 3_870_687}), \
             mock.patch.object(zylab.GRAFT, "execute", return_value={"root": "/tmp/proj", "inventory": {"files": 164}}), \
             mock.patch.object(zylab.GRAFT, "render", return_value="[graft ok]"), \
             mock.patch.object(zylab.CFG, "graft_policy", return_value={}), \
             mock.patch.object(zylab, "Spinner") as spinner, \
             mock.patch.object(sys, "stdout", out):
            spinner.return_value.__enter__ = lambda *a: None
            spinner.return_value.__exit__ = lambda *a: False
            zylab.cmd_graft(sess, action)
        return out.getvalue()

    def test_first_build_announces_size_and_expected_wait(self):
        text = self.run_build(indexed=False)
        self.assertIn("首次索引 164 个源码文件（3.9 MB）", text)
        self.assertIn("几十秒", text)
        self.assertIn("[graft ok]", text)

    def test_incremental_build_stays_quiet(self):
        text = self.run_build(indexed=True)
        self.assertNotIn("首次索引", text)
        self.assertIn("[graft ok]", text)

    def test_rebuild_always_announces(self):
        self.assertIn("重建索引 164", self.run_build(indexed=True, action="rebuild"))


if __name__ == "__main__":
    unittest.main()
