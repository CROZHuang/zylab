"""wincompat 跨平台兼容层的回归测试（2026-09-16 Windows 支持改造）。

两层验证：
1. POSIX 契约：Linux 上这些包装必须与原语完全同义（flock 互斥、killpg 语义、
   fd_* 的 at 语义、raw_mode 进出对称、wait_fd 超时行为）。
2. Windows 可导入性：模拟 Windows 解释器环境（fcntl/termios/tty 不存在），
   core 的全部改造模块必须仍能 import —— 这是「Windows 上第一行就 ModuleNotFoundError」
   的直接回归测试。
"""
import contextlib
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

from core import wincompat  # noqa: E402


class FlockContract(unittest.TestCase):
    """跨进程互斥契约：msvcrt 退化版也不能弱于 fcntl.flock。"""

    def setUp(self):
        fd, self.lockpath = tempfile.mkstemp(prefix="zylab-flock-")
        os.close(fd)

    def tearDown(self):
        with contextlib.suppress(OSError):
            os.unlink(self.lockpath)

    def test_exclusive_lock_blocks_second_process(self):
        """同一路径的 LOCK_EX 必须跨进程互斥，**且拿不到锁要抛 BlockingIOError**。

        抛而不是返回 False：Python 的 fcntl.flock 就是这个语义（返回 False 是 C 的
        约定），而 zylab 的调用方全部靠 `except BlockingIOError` 判断「别人占着」。
        这条用例原本钉的是返回 False —— 与实现一起错。2026-09-21 在 Linux 上实测到
        后果：graft 的两个 builder 同时开建、worker 吐不出完整 envelope。
        """
        fd_a = os.open(self.lockpath, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.assertTrue(wincompat.flock(fd_a, wincompat.LOCK_EX))
            code = (
                "import os, sys; sys.path.insert(0, {root!r}); "
                "from core import wincompat; "
                "fd = os.open({path!r}, os.O_RDWR | os.O_CREAT, 0o600); "
                "\ntry:\n"
                "    wincompat.flock(fd, wincompat.LOCK_EX | wincompat.LOCK_NB)\n"
                "    print('ACQUIRED')\n"
                "except BlockingIOError:\n"
                "    print('BLOCKED')\n"
            ).format(root=ROOT, path=str(self.lockpath))
            # 子进程对同一文件加非阻塞锁，必须拿不到
            probe = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                timeout=10)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertIn("BLOCKED", probe.stdout)
            self.assertNotIn("ACQUIRED", probe.stdout)
        finally:
            wincompat.flock(fd_a, wincompat.LOCK_UN)
            os.close(fd_a)

    def test_unlock_then_second_process_locks(self):
        """LOCK_UN 之后其他进程必须能立刻拿锁。"""
        fd_a = os.open(self.lockpath, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.assertTrue(wincompat.flock(fd_a, wincompat.LOCK_EX))
            wincompat.flock(fd_a, wincompat.LOCK_UN)
            code = (
                "import os, sys; sys.path.insert(0, {root!r}); "
                "from core import wincompat; "
                "fd = os.open({path!r}, os.O_RDWR | os.O_CREAT, 0o600); "
                "ok = wincompat.flock(fd, wincompat.LOCK_EX | wincompat.LOCK_NB); "
                "wincompat.flock(fd, wincompat.LOCK_UN); os.close(fd); print(ok)"
            ).format(root=ROOT, path=str(self.lockpath))
            probe = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                timeout=10)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertIn("True", probe.stdout)
        finally:
            os.close(fd_a)


class ProcessGroupContract(unittest.TestCase):
    """进程树终止契约。"""

    def test_terminate_process_tree_kills_children(self):
        """契约是「**整棵树**都没了」，不是退出码长什么样。

        原来断言 `rc < 0`（POSIX 用负数编码「被信号杀死」）。Windows 上
        taskkill /F 给的是 1 —— 于是这条在 Windows 上必红，而它真正要证明的
        「孙子进程也被收掉」两个平台**都没验**。改成直接查两个 pid 的存活。
        用 python 起子进程而不是 `bash -c 'sleep &'`：Git Bash 的 `$!` 是 MSYS
        pid，和 Windows pid 不是一回事，拿来查存活没有意义。
        """
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            code = (
                "import subprocess, sys, time;"
                "child = subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(30)']);"
                "open(sys.argv[1], 'w').write(str(child.pid));"
                "time.sleep(30)")
            proc = subprocess.Popen(
                [sys.executable, "-c", code, str(pidfile)],
                **wincompat.popen_group_kwargs())
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not pidfile.exists():
                    time.sleep(0.05)
                grandchild = int(pidfile.read_text().strip())
                self.assertTrue(
                    wincompat.pid_alive(grandchild), "孙子进程应已启动")

                self.assertTrue(
                    wincompat.terminate_process_tree(proc.pid, grace=1.0))
                proc.wait(timeout=10)

                deadline = time.monotonic() + 5
                while (time.monotonic() < deadline
                       and wincompat.pid_alive(grandchild)):
                    time.sleep(0.05)
                self.assertFalse(
                    wincompat.pid_alive(proc.pid), "leader 应已退出")
                self.assertFalse(
                    wincompat.pid_alive(grandchild), "孙子进程也必须被收掉")
            finally:
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=5)


class FdAtContract(unittest.TestCase):
    """fd_* 家族（openat/dir_fd 退化层）在 POSIX 上必须保持 at 语义。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_open_stat_replace_roundtrip(self):
        box = wincompat.fd_open_dir(str(self.root))
        try:
            fd = wincompat.fd_open(box, "a.txt", os.O_WRONLY | os.O_CREAT, 0o600)
            with wincompat.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(b"one")
                handle.flush()
            os.close(fd)
            info = wincompat.fd_stat_nofollow(box, "a.txt")
            self.assertEqual(info.st_size, 3)
            fd = wincompat.fd_open(box, "b.txt", os.O_WRONLY | os.O_CREAT, 0o600)
            with wincompat.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(b"two-two")
                handle.flush()
            os.close(fd)
            # 同目录 replace：at 语义
            wincompat.fd_replace(box, "b.txt", box, "a.txt")
            self.assertEqual(
                (self.root / "a.txt").read_bytes(), b"two-two")
            wincompat.fd_unlink(box, "a.txt")
            self.assertFalse((self.root / "a.txt").exists())
        finally:
            wincompat.close(box)


class ModeAndRedirectRules(unittest.TestCase):
    """两条「为 Windows 写、但在 Linux 上测得到」的规则（AGENTS.md §平台不变量）。

    2026-09-22 公开仓库 CI 的两列 Windows 主因就在这里，而且**两列不一样**：
    - py3.10：`os.chmod(..., follow_symlinks=False)` 在 Windows 上 3.13 才实现，
      之前对任何路径都抛 `NotImplementedError`（还不是 OSError），
      `CheckpointStore.__init__` 当场炸 → test_checkpoints/test_agent_loop 成片；
    - py3.13：只读属性挡住 `os.replace`，`WinError 5`，而它**不是**共享冲突，
      重试无用（`test_mode_zero_is_preserved_by_durable_compensation`）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    # ---- is_link_like：重定向点判据 -------------------------------------
    def test_a_plain_file_or_dir_is_not_a_redirect(self):
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        self.assertFalse(wincompat.is_link_like(target))
        self.assertFalse(wincompat.is_link_like(self.root))

    def test_a_symlink_is_a_redirect(self):
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        link = self.root / "l"
        link.symlink_to(target)
        self.assertTrue(wincompat.is_link_like(link))

    def test_an_unreadable_path_is_treated_as_one(self):
        """调用方都在做安全判断——不确定时收紧，不是放过。"""
        self.assertTrue(wincompat.is_link_like(self.root / "nope"))

    # ---- chmod_nofollow：POSIX 语义一字不变 ----------------------------
    def test_it_applies_the_mode_to_a_real_path(self):
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        wincompat.chmod_nofollow(target, 0o600)
        self.assertEqual(stat.S_IMODE(target.lstat().st_mode), 0o600)
        wincompat.chmod_nofollow(self.root, 0o700)
        self.assertEqual(stat.S_IMODE(self.root.lstat().st_mode), 0o700)

    def test_it_refuses_to_chmod_through_a_symlink(self):
        """checkpoints 的符号链接拒绝就靠这个异常——不能被包装吞掉。

        Linux 上 `os.chmod not in os.supports_follow_symlinks`，但对**非**符号
        链接照样成功、只在真是符号链接时抛 `NotImplementedError`（glibc 的
        `fchmodat(AT_SYMLINK_NOFOLLOW)` 报 ENOTSUP）。所以这个异常在 POSIX 上
        的含义是「这是符号链接，拒绝」，不是「本平台不支持」。
        """
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o644)
        link = self.root / "l"
        link.symlink_to(target)
        with self.assertRaises(NotImplementedError):
            wincompat.chmod_nofollow(link, 0o777)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644,
                         "目标的权限一位都不该动")

    # ---- 只读属性：规则与摘除 ------------------------------------------
    def test_the_readonly_rule_reads_the_write_bit(self):
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o444)
        self.assertTrue(wincompat.readonly_blocks_write(target.lstat()))
        os.chmod(target, 0o000)
        self.assertTrue(wincompat.readonly_blocks_write(target.lstat()))
        os.chmod(target, 0o644)
        self.assertFalse(wincompat.readonly_blocks_write(target.lstat()))

    def test_clear_readonly_only_touches_what_it_must(self):
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o400)
        self.assertEqual(wincompat.clear_readonly(target), 0o400,
                         "要把原 mode 交回去，调用方才放得回")
        self.assertTrue(stat.S_IMODE(target.lstat().st_mode) & stat.S_IWRITE)
        self.assertIsNone(wincompat.clear_readonly(target),
                          "已经可写就不该再动它")
        self.assertIsNone(wincompat.clear_readonly(self.root / "nope"))

    def test_clear_readonly_never_goes_through_a_symlink(self):
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o400)
        link = self.root / "l"
        link.symlink_to(target)
        self.assertIsNone(wincompat.clear_readonly(link))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o400)

    def test_the_fallback_unlocks_then_retries_once(self):
        """顺序：先有界重试（瞬态的共享冲突），再摘只读位（持久的属性）。"""
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o400)
        calls = []

        def call():
            calls.append(stat.S_IMODE(target.lstat().st_mode))
            if len(calls) == 1:
                raise PermissionError(5, "Access is denied")
            return "done"

        self.assertEqual(
            wincompat._retry_then_unlock(call, str(target)), "done")
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0] & stat.S_IWRITE, "第一次是只读时调的")
        self.assertTrue(calls[1] & stat.S_IWRITE, "第二次之前已经摘掉只读位")

    def test_the_fallback_re_raises_when_there_is_nothing_to_unlock(self):
        """不是只读属性挡的，就该把原异常原样抛出去——不许无限试。"""
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        calls = []

        def call():
            calls.append(1)
            raise PermissionError(13, "真的没权限")

        with self.assertRaises(PermissionError):
            wincompat._retry_then_unlock(call, str(target))
        self.assertEqual(len(calls), 1, "POSIX 上只该调一次")

    def test_a_failed_second_try_puts_the_readonly_bit_back(self):
        """目标既只读、又被别的进程占着时：不该在用户文件上留下没成事的改动。"""
        target = self.root / "f"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o400)

        def always_denied():
            raise PermissionError(5, "Access is denied")

        with self.assertRaises(PermissionError):
            wincompat._retry_then_unlock(always_denied, str(target))
        self.assertEqual(stat.S_IMODE(target.lstat().st_mode), 0o400,
                         "第二次也失败了，只读位要放回去")


class RawModeDoesNotWaitForOutput(unittest.TestCase):
    """退出 raw 模式不能等输出排空——那在 macOS/BSD 上是**无界等待**。

    2026-09-22 公开仓库 CI 的 macOS 那列：单元测试步骤跑满 20 分钟看门狗，
    栈顶是 `wincompat` 的 `_PosixRaw.__exit__` 那行 `tcsetattr(..., TCSADRAIN)`
    （经 `termcaps.probe` → `_Query.__exit__` 进来）。Linux 的 `set_termios`
    走带超时的 `tty_wait_until_sent`，所以同一行代码在这边从来没挂过——
    **这就是它藏了这么久的原因**，也是为什么这条用例要在两个平台上都跑。
    """

    def test_leaving_raw_mode_does_not_wait_for_unread_output(self):
        try:
            import pty
        except ImportError:                      # Windows 没有 pty
            self.skipTest("需要 POSIX pty")
        master, slave = pty.openpty()
        done = threading.Event()

        def cycle():
            # 进入时队列是空的（与 termcaps.probe 的顺序一致）；写完这几百字节
            # 再退出——没有任何人读 master，它们永远排不空。
            with wincompat.raw_mode(slave, cbreak=True):
                os.write(slave, b"x" * 512)
            done.set()

        # 放进线程里跑：挂住时这条用例**快速红**，而不是把整个套件拖到看门狗
        # 开火（那一轮 macOS 连结论都没给出来，只有 cancelled）。
        worker = threading.Thread(target=cycle, daemon=True)
        worker.start()
        worker.join(10.0)
        try:
            self.assertTrue(
                done.is_set(),
                "退出 raw 模式卡住了：它在等输出队列排空（TCSADRAIN 语义），"
                "而对端没人读时那是无界等待")
        finally:
            if done.is_set():
                os.close(slave)
            os.close(master)

    def test_the_source_never_drains_on_restore(self):
        """规则钉在源码层：两个平台都执行得到，不依赖手上有没有那台机器。

        按 AST 找 `*.TCSADRAIN` 这个**属性访问**，不是按字符串找——正文里
        正好有一段注释在解释「为什么不能用 TCSADRAIN」，纯子串断言会被自己
        的注释绊倒（第一版就是）。
        """
        import ast
        tree = ast.parse(
            (Path(ROOT) / "core" / "wincompat.py").read_text(encoding="utf-8"))
        lines = sorted(node.lineno for node in ast.walk(tree)
                       if isinstance(node, ast.Attribute)
                       and node.attr == "TCSADRAIN")
        self.assertEqual(
            lines, [],
            f"wincompat.py:{lines} 用了 TCSADRAIN。恢复终端属性只能用 TCSANOW："
            "TCSADRAIN 要等输出排空，macOS/BSD 上对端不读就永远不返回，"
            "而 termcaps.probe 在启动路径上")


class RawModeContract(unittest.TestCase):
    def test_raw_mode_enter_exit_symmetric_on_pipe(self):
        # pipe 不是 tty：POSIX termios 会抛 —— wincompat 必须同样 fail loud，
        # 不能静默成功（TUI 有 supported() 前置判断）。
        r, w = os.pipe()
        try:
            with self.assertRaises(Exception):
                with wincompat.raw_mode(r):
                    pass
        finally:
            os.close(r)
            os.close(w)


class WindowsImportSimulation(unittest.TestCase):
    """模拟 Windows：fcntl/termios/tty 缺失时 core 全模块必须可导入。

    这是用户实际撞到的第一个错误（ModuleNotFoundError: fcntl）的直接回归。
    注意不能 block 'posix'/'pwd' 等模块名：Linux 的 shutil 等标准库在
    import 链上会拉它们，而 Windows 的对应标准库有自己的实现，不受影响。
    """

    # 名单是**枚举出来的**，不是手写的。手写名单只能防住已经犯过的错：
    # 2026-09-21 合并时 git 自动合并把 `import fcntl` 还给了四个模块，其中
    # core.store / core.models 恰好在名单里才被抓到；同一次合并里 zylab.py
    # 这个真正的入口一直不在名单上。新加一个模块不该需要有人记得来改这里。
    #
    # 唯一的例外是 core.wincompat 自己：它按 sys.platform 分支 import fcntl，
    # 在 Linux 上必然走 POSIX 分支，纳进来只会恒红（它的 Windows 分支要真机验证）。
    EXCLUDED = frozenset({"core.wincompat"})

    @classmethod
    def modules(cls):
        root = pathlib.Path(ROOT)
        found = ["zylab"]          # 入口本身，10k 行，最不该漏
        found += sorted(
            f"core.{path.stem}" for path in (root / "core").glob("*.py")
            if path.stem != "__init__")
        return [m for m in found if m not in cls.EXCLUDED]

    def _import_with_posix_blocked(self, modname):
        # 说明：无法在 Linux 上完整伪造 Windows（shutil 在 win32 下会拉真
        # _winapi，Linux 二进制里没有）。这里验证的是「核心模块不再无条件
        # import fcntl/termios/tty」——即用户在 Windows 上报的那个崩溃根因。
        # wincompat 的 Windows 分支（msvcrt/ctypes 路径）只能真机验证。
        body = (
            "import sys; sys.path.insert(0, {root!r})\n"
            "import builtins\n"
            "real_import = builtins.__import__\n"
            "BLOCKED = {{'fcntl', 'termios', 'tty'}}\n"
            "def guarded(name, *a, **k):\n"
            "    root = name.split('.')[0]\n"
            "    if root in BLOCKED:\n"
            "        raise ImportError('blocked: ' + name)\n"
            "    return real_import(name, *a, **k)\n"
            "builtins.__import__ = guarded\n"
            "try:\n"
            "    __import__({mod!r})\n"
            "    print('OK')\n"
            "except ImportError as exc:\n"
            "    if 'blocked:' in str(exc):\n"
            "        raise SystemExit('POSIX-ONLY-IMPORT: ' + str(exc))\n"
            "    raise SystemExit('IMPORT-FAILED: ' + str(exc))\n"
        ).format(root=ROOT, mod=modname)
        return subprocess.run(
            [sys.executable, "-c", body], capture_output=True, text=True,
            timeout=30)

    def test_core_modules_import_without_posix_stdlib(self):
        names = self.modules()
        self.assertGreater(len(names), 30, f"枚举只找到 {names}，路径不对？")
        for mod in names:
            with self.subTest(module=mod):
                r = self._import_with_posix_blocked(mod)
                self.assertEqual(
                    r.returncode, 0,
                    f"{mod} 导入失败：{r.stdout[-400:]}{r.stderr[-400:]}")
                self.assertIn("OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
