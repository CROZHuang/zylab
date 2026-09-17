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
import subprocess
import sys
import tempfile
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
        """同一路径的 LOCK_EX 必须跨进程互斥。"""
        fd_a = os.open(self.lockpath, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.assertTrue(wincompat.flock(fd_a, wincompat.LOCK_EX))
            code = (
                "import os, sys; sys.path.insert(0, {root!r}); "
                "from core import wincompat; "
                "fd = os.open({path!r}, os.O_RDWR | os.O_CREAT, 0o600); "
                "print(wincompat.flock(fd, wincompat.LOCK_EX | wincompat.LOCK_NB))"
            ).format(root=ROOT, path=str(self.lockpath))
            # 子进程对同一文件加非阻塞锁，必须失败（契约：竞争失败
            # 返回 False，两侧平台统一；拿到锁返回 True）
            probe = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                timeout=10)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertIn("False", probe.stdout)
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
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 30 & sleep 30"],
            start_new_session=True)
        time.sleep(0.4)
        try:
            self.assertTrue(
                wincompat.terminate_process_tree(proc.pid, grace=1.0))
            rc = proc.wait(timeout=5)
            self.assertLess(rc, 0)   # 被信号杀死
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

    # core.wincompat 本身按 sys.platform 分支 import fcntl —— 在 Linux 上跑
    # 必然走 POSIX 分支，不能纳入这个模拟（它的 Windows 分支要真机验证）。
    MODULES = (
        "core.tui", "core.termcaps", "core.apprender",
        "core.tasks", "core.tools", "core.graft", "core.graft_worker",
        "core.store", "core.settings", "core.models", "core.checkpoints",
    )

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
        for mod in self.MODULES:
            with self.subTest(module=mod):
                r = self._import_with_posix_blocked(mod)
                self.assertEqual(
                    r.returncode, 0,
                    f"{mod} 导入失败：{r.stdout[-400:]}{r.stderr[-400:]}")
                self.assertIn("OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
