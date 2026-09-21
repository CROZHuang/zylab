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
