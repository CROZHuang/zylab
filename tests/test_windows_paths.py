"""跨平台路径守卫的契约（2026-09-17 Windows 移植）。

`paths.path_under` 是所有前缀式路径守卫的唯一实现（受保护路径、状态目录）。
以前 `paths.is_protected` / `tools._guard` / `tools._under_protected` /
`tools._under_state_dir` 各写了一遍 `value.startswith(root + "/")`，
于是同一个平台 bug 要修四处 —— 而在 Windows 上那个写法**恒为 False**：
声明了受保护路径、守卫却一条都拦不住，界面上还看不出异常。

POSIX 断言在两个平台上都跑（`path_under` 的 POSIX 分支必须原样保留）；
Windows 专属语义用 skipUnless 圈起来。
"""
import errno
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

from core import paths  # noqa: E402

WINDOWS = sys.platform == "win32"


class PosixSemanticsUnchanged(unittest.TestCase):
    """POSIX 分支的表达式必须与重构前一字不差。"""

    def test_prefix_match_is_component_wise(self):
        roots = ("/protected/archive",)
        self.assertTrue(paths.path_under("/protected/archive", roots))
        self.assertTrue(paths.path_under("/protected/archive/deep/x", roots))
        # 同前缀但不同目录：不能误伤
        self.assertFalse(paths.path_under("/protected/archive-other", roots))
        self.assertFalse(paths.path_under("/protected", roots))
        self.assertFalse(paths.path_under("/elsewhere", roots))

    def test_empty_roots_never_match(self):
        self.assertFalse(paths.path_under("/anything", ()))
        self.assertFalse(paths.path_under("/anything", ("",)))


@unittest.skipUnless(WINDOWS, "Windows 专属路径语义")
class WindowsSemantics(unittest.TestCase):
    def test_backslash_paths_are_matched(self):
        roots = (r"D:\archive",)
        self.assertTrue(paths.path_under(r"D:\archive", roots))
        self.assertTrue(paths.path_under(r"D:\archive\deep\x.txt", roots))
        self.assertFalse(paths.path_under(r"D:\archive-other\x", roots))

    def test_drive_letter_and_separator_are_case_and_slash_insensitive(self):
        roots = (r"D:\archive",)
        self.assertTrue(paths.path_under(r"d:\ARCHIVE\deep\y", roots))
        self.assertTrue(paths.path_under("D:/archive/x.txt", roots))

    def test_drive_bearing_declaration_only_guards_that_drive(self):
        self.assertFalse(paths.path_under(r"C:\archive\x", (r"D:\archive",)))

    def test_drive_relative_declaration_guards_every_drive(self):
        """Windows 上 `/archive` 是盘符相对写法；守多了只是拒绝写入，守漏了才是事故。"""
        roots = ("/protected/archive",)
        self.assertTrue(paths.path_under(r"C:\protected\archive\x", roots))
        self.assertTrue(paths.path_under(r"D:\protected\archive\x", roots))
        self.assertFalse(paths.path_under(r"C:\protected\other\x", roots))


@unittest.skipUnless(WINDOWS, "Git Bash / MSYS 路径还原是 Windows 专属")
class MsysShellPaths(unittest.TestCase):
    """模型在 Windows 上跑的 shell 是 Git Bash，它写出来的绝对路径长这样。"""

    def setUp(self):
        from core import tools
        self.tools = tools

    def test_msys_drive_paths_resolve_to_windows_paths(self):
        here = os.getcwd()
        expected = self.tools._resolve_against(r"C:\protected\archive\x", here)
        for spelling in ("/c/protected/archive/x",
                         "/cygdrive/c/protected/archive/x",
                         "C:/protected/archive/x"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    self.tools._resolve_against(spelling, here), expected)

    def test_guard_denies_msys_spelling_of_a_protected_path(self):
        with mock.patch.object(self.tools, "PROTECTED", (r"C:\protected\archive",)):
            for cmd in ("echo hi > /c/protected/archive/x",
                        "tar -xf a.tar -C /c/protected/archive",
                        "rm -rf /cygdrive/c/protected/archive/x"):
                with self.subTest(cmd=cmd), self.assertRaises(self.tools.Denied):
                    self.tools._guard_bash(cmd, cwd=os.getcwd())

    def test_unrelated_msys_path_is_not_denied(self):
        with mock.patch.object(self.tools, "PROTECTED", (r"C:\protected\archive",)):
            self.tools._guard_bash("echo hi > /c/somewhere/else", cwd=os.getcwd())


class HomeHonoursTheEnvironment(unittest.TestCase):
    """`$HOME` 是测试用来重定向状态目录的杠杆；Windows 的 expanduser 忽略它。"""

    def test_home_dir_follows_HOME(self):
        with mock.patch.dict(os.environ, {"HOME": os.path.join("Z:", "fakehome")
                                          if WINDOWS else "/tmp/fakehome"}):
            self.assertEqual(str(paths.home_dir()),
                             os.environ["HOME"])

    def test_unresolvable_home_never_becomes_a_literal_tilde_directory(self):
        """`expanduser` 解析不出来时原样返回 `~`。

        照着它建目录，会在**当前工作目录**下造出一个名字就叫 `~` 的文件夹，
        把整棵状态树倒进去 —— 实测在仓库根目录里造出过 `./~/.zylab/`。
        （最小 env 的子进程测试很容易触发：HOME / USERPROFILE 都没有。）
        """
        bare = {k: v for k, v in os.environ.items()
                if k not in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")}
        with mock.patch.dict(os.environ, bare, clear=True):
            home = paths.home_dir()
            self.assertTrue(home.is_absolute(), f"必须是绝对路径，得到 {home}")
            self.assertNotIn("~", home.parts, f"不能出现字面量 ~：{home}")
            self.assertTrue(paths.default_home().is_absolute())


class ReservedWindowsNames(unittest.TestCase):
    """Win32 会吞掉的文件名。规则是纯函数，所以 Linux 门禁也测得到它。"""

    def setUp(self):
        from core import wincompat
        self.win = wincompat

    def test_device_names_are_flagged_with_or_without_an_extension(self):
        for name in ("NUL", "nul", "CON", "aux.txt", "com1", "LPT9.log",
                     "CONIN$", "CONOUT$"):
            with self.subTest(name=name):
                self.assertIsNotNone(self.win.reserved_name_problem(name))

    def test_names_that_merely_start_with_a_device_word_are_fine(self):
        """`conference.md` 不是 CON —— 整段相等才算，前缀不算。"""
        for name in ("conference.md", "nulla.py", "aux_helpers.py",
                     "com10.txt", "lpt.txt", "README.md"):
            with self.subTest(name=name):
                self.assertIsNone(self.win.reserved_name_problem(name))

    def test_trailing_dot_or_space_is_flagged(self):
        """Win32 落盘时静默去掉它，于是按原名再读就是「文件不存在」。"""
        for name in ("report.md ", "notes.", "x "):
            with self.subTest(name=name):
                self.assertIn("静默", self.win.reserved_name_problem(name))

    def test_characters_windows_forbids_are_flagged(self):
        for name in ('we:ird', "pipe|name", "quo\"te", "star*"):
            with self.subTest(name=name):
                self.assertIsNotNone(self.win.reserved_name_problem(name))

    def test_every_component_is_checked_not_just_the_basename(self):
        """`logs/aux/x.txt` 的 mkdir 会失败，而 Win32 只说「参数错误」。"""
        problem = self.win.reserved_path_problem(r"C:\proj\logs\aux\x.txt")
        self.assertIn("aux", problem)

    def test_drive_and_unc_prefixes_are_not_mistaken_for_names(self):
        for path in (r"C:\proj\ok.txt", r"\\srv\share\ok.txt",
                     "rel/dir/ok.txt"):
            with self.subTest(path=path):
                self.assertIsNone(self.win.reserved_path_problem(path))

    def test_write_file_refuses_to_create_one_but_only_on_windows(self):
        import tempfile
        from core import tools
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "NUL")
            with mock.patch.object(tools.wincompat, "IS_WINDOWS", True):
                out = tools.t_write_file(target, "x")
            self.assertIn("拒绝新建", out)
            self.assertFalse(os.path.exists(target))
            # POSIX 上 NUL 是个再普通不过的文件名，不许拦
            self.assertNotIn("拒绝新建", tools.t_write_file(target, "x"))
            self.assertTrue(os.path.exists(target))

    def test_an_existing_file_is_never_refused(self):
        """名字已经在盘上，说明本机能用它；再拦就是拦用户自己的历史文件。"""
        import tempfile
        from core import tools
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "NUL")
            Path(target).write_text("old", encoding="utf-8")
            with mock.patch.object(tools.wincompat, "IS_WINDOWS", True):
                self.assertNotIn("拒绝新建", tools.t_write_file(target, "new"))


class WslIsNotGitBash(unittest.TestCase):
    """装了 WSL 的 Windows 上，System32\\bash.exe 通常排在 Git Bash 前面。

    用它跑命令的后果不是「慢一点」而是换了一台机器：cwd 传的是 `C:\\Users\\x`，
    WSL 里根本不认；守卫比的是 Windows 路径，命令碰的是 `/mnt/c/...`，
    `_guard` 与状态目录守卫全部静默落空。
    """

    def setUp(self):
        from core import tools
        self.tools = tools

    # 假的 bash 在 **Windows 上必须带 .exe**：`shutil.which` 那边按 PATHEXT
    # 匹配，一个没有扩展名的 `bash` 找不到，用例会在真 Windows 上假红。
    # （这条是 2026-09-21 写完之后自己 review 出来的，不是 CI 报的——
    # 用 POSIX 去模拟 Windows 时，最容易漏的就是 PATHEXT 这类只在那边存在的规则。）
    EXE_NAME = "bash.exe" if WINDOWS else "bash"

    def _fake_tree(self, tmp):
        system32 = os.path.join(tmp, "Windows", "System32")
        gitbin = os.path.join(tmp, "Git", "usr", "bin")
        for d in (system32, gitbin):
            os.makedirs(d)
            exe = os.path.join(d, self.EXE_NAME)
            Path(exe).write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(exe, 0o755)
        return system32, gitbin

    def test_the_system32_shim_is_skipped_for_the_real_bash(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            system32, gitbin = self._fake_tree(tmp)
            env = {"PATH": os.pathsep.join([system32, gitbin]),
                   "SystemRoot": os.path.join(tmp, "Windows")}
            with mock.patch.object(self.tools.wincompat, "IS_WINDOWS", True), \
                    mock.patch.dict(os.environ, env):
                picked = self.tools.bash_executable()
            self.assertEqual(os.path.dirname(picked), gitbin,
                             f"挑中的是 {picked}，应该在 Git 的 bin 里")

    def test_only_the_shim_present_is_refused_with_an_actionable_message(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            system32, _ = self._fake_tree(tmp)
            env = {"PATH": system32, "SystemRoot": os.path.join(tmp, "Windows")}
            with mock.patch.object(self.tools.wincompat, "IS_WINDOWS", True), \
                    mock.patch.dict(os.environ, env):
                with self.assertRaises(self.tools.Denied) as caught:
                    self.tools.bash_executable()
            self.assertIn("Git for Windows", str(caught.exception))
            self.assertIn("WSL", str(caught.exception))

    def test_posix_keeps_taking_the_first_bash_on_path(self):
        """POSIX 上这条路径必须与改动前一字不差。"""
        import shutil
        self.assertEqual(self.tools.bash_executable(), shutil.which("bash"))
        self.assertFalse(self.tools._is_wsl_launcher("/usr/bin/bash"))

    def test_wsl_style_paths_are_translated_so_the_guard_still_sees_them(self):
        """模型把 WSL 惯例写进 Git Bash 是常事；认不出来守卫就静默落空。"""
        with mock.patch.object(self.tools.wincompat, "IS_WINDOWS", True):
            self.assertEqual(self.tools._from_shell_path("/mnt/c/proj/x"),
                             "C:\\proj/x")
            # `/mnt/data` 不是盘符写法，别乱动
            self.assertEqual(self.tools._from_shell_path("/mnt/data/real"),
                             "/mnt/data/real")
        # POSIX 上整条函数是恒等
        self.assertEqual(self.tools._from_shell_path("/mnt/c/proj/x"),
                         "/mnt/c/proj/x")


class SharingViolationRetry(unittest.TestCase):
    """杀毒/索引器把文件打开着时，Windows 的 rename/unlink 会当场失败。"""

    def setUp(self):
        from core import wincompat
        self.win = wincompat

    def _sharing_error(self):
        exc = PermissionError(13, "sharing violation")
        exc.winerror = 32
        return exc

    def test_posix_calls_exactly_once_and_never_sleeps(self):
        calls = []
        with mock.patch.object(self.win.time, "sleep",
                               side_effect=AssertionError("POSIX 不该退避")):
            self.assertEqual(
                self.win.retry_sharing(lambda: calls.append(1) or "ok"), "ok")
        self.assertEqual(len(calls), 1)

    def test_windows_retries_a_sharing_violation_until_it_clears(self):
        attempts = []
        error = self._sharing_error()

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise error
            return "ok"

        slept = []
        with mock.patch.object(self.win, "IS_WINDOWS", True), \
                mock.patch.object(self.win.time, "sleep", slept.append):
            self.assertEqual(self.win.retry_sharing(flaky), "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(slept, [0.01, 0.02])      # 指数退避

    def test_a_real_permission_error_is_raised_at_once(self):
        """目标在只读目录里，重试多少次都一样；拖长只会让模型盯着卡住的工具。"""
        attempts = []

        def denied():
            attempts.append(1)
            raise OSError(errno.EROFS, "read-only file system")

        with mock.patch.object(self.win, "IS_WINDOWS", True), \
                mock.patch.object(self.win.time, "sleep",
                                  side_effect=AssertionError("不该退避")):
            with self.assertRaises(OSError):
                self.win.retry_sharing(denied)
        self.assertEqual(len(attempts), 1)

    def test_retries_are_bounded_and_the_original_error_survives(self):
        error = self._sharing_error()
        attempts = []

        def always():
            attempts.append(1)
            raise error

        with mock.patch.object(self.win, "IS_WINDOWS", True), \
                mock.patch.object(self.win.time, "sleep", lambda _s: None):
            with self.assertRaises(PermissionError) as caught:
                self.win.retry_sharing(always)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(attempts), self.win.SHARING_RETRIES)


class ShellFactsInThePrompt(unittest.TestCase):
    """Windows 上得告诉模型它在跟哪个 shell 说话——否则错得不报错。"""

    def test_posix_says_nothing_and_costs_no_tokens(self):
        from core import agent
        self.assertEqual(agent.shell_facts(), [])

    def test_windows_names_the_shell_and_the_two_silent_traps(self):
        from core import agent
        with mock.patch.object(agent.wincompat, "IS_WINDOWS", True), \
                mock.patch.object(agent.tools, "bash_executable",
                                  return_value=r"C:\Git\usr\bin\bash.exe"):
            lines = agent.shell_facts()
        blob = "\n".join(lines)
        self.assertIn("bash.exe", blob)
        self.assertIn("/mnt/c/", blob)
        self.assertIn("NUL", blob)

    def test_a_missing_bash_does_not_break_the_whole_request(self):
        from core import agent
        with mock.patch.object(agent.wincompat, "IS_WINDOWS", True), \
                mock.patch.object(agent.tools, "bash_executable",
                                  side_effect=RuntimeError("boom")):
            self.assertEqual(len(agent.shell_facts()), 3)


if __name__ == "__main__":
    unittest.main()
