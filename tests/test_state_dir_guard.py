"""zylab 自己的状态目录不是工作对象：改写它要一次性、不可绕过的确认（2026-09-04 事故）。

事故链：老会话里 session_grants 已放行 bash/edit_file/write_file → graft 缓存被误拒 →
模型直接 edit_file 改了用户 settings.json，没有任何确认。这里钉住：bash 的 rm/sed -i/
重定向/带写调用的解释器脚本、以及 write_file/edit_file 落在状态目录都算高风险；
读不算；resume 时残留授权要说出来，/permissions revoke 能撤销。
"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from core import agent as A, store, tools

STATE = os.path.realpath(str(store.HOME))
# 嵌进 bash 命令串里的那份拼写。Windows 路径带反斜杠，而 bash（以及守卫用来
# 切词的 shlex posix 模式）把反斜杠当转义符 —— 不换成正斜杠的话，
# 命令在真 bash 里也会被吃掉分隔符，测的就不是守卫了。
# POSIX 上没有反斜杠，这行是恒等变换。
STATE_SH = STATE.replace(chr(92), "/")


def risk(command, cwd="/tmp"):
    return tools._assess_workspace_risk(command, cwd=cwd)


class BashStateDirTests(unittest.TestCase):
    def test_mutations_under_the_state_dir_are_high_risk(self):
        cases = [
            f"rm -v {STATE_SH}/sessions/a.json {STATE_SH}/sessions/b.json",
            f"sed -i 's/x/y/' {STATE_SH}/settings.json",
            f"echo '{{}}' > {STATE_SH}/settings.json",
            f"cd {STATE_SH}/sessions && rm -v a.json",
            f"python3 - <<'EOF'\nimport json\np='{STATE_SH}/settings.json'\nd=json.load(open(p))\njson.dump(d, open(p,'w'))\nEOF",
            f"cp /tmp/x.env {STATE_SH}/keys.env",
        ]
        for command in cases:
            with self.subTest(command=command[:60]):
                value = risk(command)
                self.assertIsNotNone(value, "应判为高风险")
                self.assertIn("state_dir_mutation", value.kinds)
                self.assertTrue(any("state dir" in line for line in value.evidence), value.evidence)

    def test_reads_and_unrelated_writes_are_not_flagged(self):
        cases = [
            f"cat {STATE_SH}/settings.json",
            f"ls -la {STATE_SH}/sessions | head",
            f"grep -rn workflow {STATE_SH}/sessions/",
            f"python3 -c \"import json; print(json.load(open('{STATE_SH}/settings.json')))\"",
            "rm -v /tmp/other.json",
            "sed -i 's/a/b/' /tmp/proj/app.py",
        ]
        for command in cases:
            with self.subTest(command=command[:60]):
                value = risk(command)
                self.assertTrue(
                    value is None or "state_dir_mutation" not in value.kinds,
                    f"不该判为状态目录改写：{value}")


class WriteToolStateDirTests(unittest.TestCase):
    def test_write_tools_targeting_the_state_dir_carry_risk(self):
        for name in ("write_file", "edit_file"):
            value = tools._assess_state_write_risk(
                name, {"path": f"{STATE_SH}/settings.json"}, cwd="/tmp")
            self.assertIsNotNone(value)
            self.assertEqual(value.kinds, ("state_dir_mutation",))
        self.assertIsNone(tools._assess_state_write_risk(
            "write_file", {"path": "/tmp/proj/app.py"}, cwd="/tmp"))
        self.assertIsNone(tools._assess_state_write_risk("bash", {"path": STATE}, cwd="/tmp"))

    def test_prepare_marks_state_dir_write_as_requiring_confirmation(self):
        os.makedirs(STATE, exist_ok=True)
        target = os.path.join(STATE, "settings.json")
        prepared = tools.prepare(
            "write_file", {"path": target, "content": "{}"},
            workspace_root=os.path.dirname(STATE))
        self.assertTrue(prepared.requires_workspace_confirmation)
        ordinary = tools.prepare(
            "write_file", {"path": os.path.join(os.path.dirname(STATE), "x.txt"), "content": "x"},
            workspace_root=os.path.dirname(STATE))
        self.assertFalse(ordinary.requires_workspace_confirmation)

    def test_authorization_accepts_write_tools_but_not_noninteractive(self):
        sess = object.__new__(zylab.Session)
        sess.renderer = None
        args = mock.Mock()
        args.requires_workspace_confirmation = True
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            decision = zylab.Session._authorize_workspace_risk(sess, "edit_file", args)
        self.assertEqual(decision["decision"], "noninteractive_workspace_risk_deny",
                         "写工具不再被当成无效工具拒绝，而是走同一条确认")
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            decision = zylab.Session._authorize_workspace_risk(sess, "read_file", args)
        self.assertEqual(decision["decision"], "workspace_risk_invalid_tool")


class GrantVisibilityTests(unittest.TestCase):
    def test_resume_announces_standing_grants(self):
        sess = mock.Mock()
        sess.always = {"bash", "edit_file"}
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            line = zylab._announce_session_grants(sess)
        self.assertIn("bash, edit_file", line)
        self.assertIn("/permissions revoke", out.getvalue())
        sess.always = set()
        self.assertEqual(zylab._announce_session_grants(sess), "")

    def test_permissions_revoke_clears_and_saves(self):
        sess = mock.Mock()
        sess.always = {"bash", "write_file"}
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            zylab.cmd_permissions(sess, "revoke")
        self.assertEqual(sess.always, set())
        sess.save.assert_called_once()
        self.assertIn("已撤销本会话授权", out.getvalue())

    def test_system_prompt_makes_research_requests_read_only(self):
        self.assertIn("只读的：不改任何文件", A.SYSTEM)
        self.assertIn(".zylab-home", A.SYSTEM)


if __name__ == "__main__":
    unittest.main()
