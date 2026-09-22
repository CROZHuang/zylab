"""F0：/protected/archive 的 bash 守卫此前只认字面量，cd / 相对路径 / tar -C / 解释器全都穿过。

契约分两层（README「两条硬规矩」已同步）：
  沙箱内   —— 词法守卫 + 只读挂载；读和拷出放行。
  UNSANDBOXED —— 守卫无法解析任意 shell，对受保护路径的任何引用一律拒绝。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest import mock as _mock
from core import tools

SAFE_CWD = tempfile.mkdtemp(prefix="core-guard-")


def lexical(cmd, cwd=SAFE_CWD):
    return tools._guard_bash(cmd, cwd=cwd)


def strict(cmd, cwd=SAFE_CWD):
    return tools._guard_bash_unsandboxed(cmd, cwd=cwd)


class LexicalGuardClosesTheProbes(unittest.TestCase):
    """09-02 静态探针里放行的三种形态，现在必须拒。"""

    def test_cd_then_relative_redirect(self):
        with self.assertRaises(tools.Denied) as c:
            lexical("cd /protected/archive && echo x > probe.txt")
        self.assertIn("cd/pushd", str(c.exception))

    def test_pushd_is_treated_like_cd(self):
        with self.assertRaises(tools.Denied):
            lexical("pushd /protected/archive/sub; touch a")

    def test_tar_extract_into_protected_dir(self):
        for cmd in ("tar -xf a.tar -C /protected/archive",
                    "tar xzf a.tgz --directory /protected/archive/x",
                    "tar --extract -f a.tar --directory=/protected/archive",
                    "unzip a.zip -d /protected/archive/out"):
            with self.subTest(cmd=cmd), self.assertRaises(tools.Denied):
                lexical(cmd)

    def test_tar_create_from_protected_dir_is_a_read(self):
        lexical("tar -cf /tmp/workspace/out.tar /protected/archive/x")

    def test_cwd_inside_protected_dir_is_refused_outright(self):
        with self.assertRaises(tools.Denied) as c:
            lexical("echo x > probe.txt", cwd="/protected/archive")
        self.assertIn("cwd=", str(c.exception))

    def test_relative_traversal_resolves_before_check(self):
        # cwd 刻意用**不存在的**路径：realpath 对不存在的路径是恒等的，于是
        # `..` 的算术在三个平台上都一样。
        #
        # 原来这里写 `cwd="/tmp/workspace"`，注释说
        # `/tmp/workspace/../../protected/archive = /protected/archive`。
        # **这个算术在 macOS 上是错的**：那边 `/tmp` 是指向 `/private/tmp` 的系统
        # 软链，内核解析 `..` 走的是物理路径，于是 `/tmp/workspace/../..` 是
        # `/private`，整条路径落在 `/private/protected/archive`。守卫不拦**是对的**
        # ——那次写入确实没落进受保护路径。2026-09-22 CI 的 macOS 那列
        # `AssertionError: Denied not raised` 就是这条夹具的算术，不是守卫漏了。
        with self.assertRaises(tools.Denied):
            lexical("echo x > ../../protected/archive/probe.txt",
                    cwd="/no-such-root/workspace")
        with self.assertRaises(tools.Denied):
            lexical("touch ../protected/archive/a", cwd="/no-such-root")

    def test_interpreter_writes_are_out_of_lexical_scope(self):
        """沙箱内由只读挂载兜底；词法守卫**不假装**能看见这个。"""
        lexical("python3 -c \"open('/protected/archive/probe.txt','w')\"")

    def test_copy_out_and_reads_stay_allowed_in_sandbox_mode(self):
        lexical("cp /protected/archive/x /tmp/workspace/")
        lexical("ls /protected/archive")
        lexical("rclone copy archive:bucket/src /tmp/workspace/dst")

    def test_direct_write_and_rclone_still_denied(self):
        with self.assertRaises(tools.Denied):
            lexical("echo x > /protected/archive/probe.txt")
        with self.assertRaises(tools.Denied):
            lexical("rclone copy ./x archive:bucket/")


class StrictUnsandboxedGuard(unittest.TestCase):
    def test_any_mention_is_refused_including_reads(self):
        for cmd in ("ls /protected/archive", "cp /protected/archive/x ./",
                    "python3 -c \"open('/protected/archive/p','w')\"",
                    "tar -xf a.tar -C /protected/archive", "cd /protected/archive && ls",
                    "P=/protected/archive; ls $P"):
            with self.subTest(cmd=cmd), self.assertRaises(tools.Denied) as c:
                strict(cmd)
            self.assertIn("UNSANDBOXED", str(c.exception))

    def test_relative_path_resolving_into_protected_is_refused(self):
        with self.assertRaises(tools.Denied):
            strict("cat ../protected/archive/readme", cwd="/root")

    def test_protected_cwd_is_refused(self):
        with self.assertRaises(tools.Denied):
            strict("ls", cwd="/protected/archive")

    def test_unrelated_commands_pass(self):
        strict("ls -la && git status")
        strict("python3 -c 'print(1)'")
        strict("rclone copy other:bucket/src ./dst")


class NoSideEffects(unittest.TestCase):
    def test_guard_never_touches_the_filesystem(self):
        """守卫只做路径运算；受保护目录一个 stat 都不该多。

        这里**必须**用真实存在的目录：合成路径下 `os.path.isdir` 为假，
        整条断言会被静默跳过，测试看着是绿的其实什么都没验。
        """
        with tempfile.TemporaryDirectory() as real:
            with mock.patch.object(tools, "PROTECTED", (real,)):
                before = os.stat(real).st_mtime
                for fn in (lexical, strict):
                    for cmd in (f"echo x > {real}/a", f"cd {real} && touch b"):
                        try:
                            fn(cmd)
                        except tools.Denied:
                            pass
                self.assertEqual(os.stat(real).st_mtime, before)



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
from unittest import mock
from core import sandbox


class UnsandboxedBranchIsWiredIn(unittest.TestCase):
    """守卫函数对了还不够 —— 必须证明 t_bash 的 UNSANDBOXED 分支真的调用了它，
    且拒绝发生在任何子进程启动之前。"""

    def _prepared(self, command):
        return tools.PreparedArguments(
            tool_name="bash",
            arguments_json=json.dumps({"command": command}),
            sandbox_decision=sandbox.SandboxDecision(
                mode="ask-unsandboxed", allowed=True, sandboxed=False,
                requires_confirmation=False,
                decision="unsandboxed-approved", marker="UNSANDBOXED"))

    def test_read_of_protected_path_is_refused_before_any_process(self):
        boom = mock.Mock(side_effect=AssertionError("不该启动子进程"))
        with mock.patch.object(tools.subprocess, "Popen", boom), \
             mock.patch.object(tools.subprocess, "run", boom):
            with self.assertRaises(tools.Denied) as c:
                tools.t_bash("ls /protected/archive", _prepared=self._prepared("ls /protected/archive"))
        self.assertIn("UNSANDBOXED", str(c.exception))
        boom.assert_not_called()

    def test_interpreter_write_is_refused_on_the_unsandboxed_branch(self):
        cmd = "python3 -c \"open('/protected/archive/p','w')\""
        with mock.patch.object(tools.subprocess, "Popen",
                               side_effect=AssertionError("不该启动子进程")):
            with self.assertRaises(tools.Denied):
                tools.t_bash(cmd, _prepared=self._prepared(cmd))
