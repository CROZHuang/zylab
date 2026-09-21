"""测试用的平台能力探针（2026-09-17 Windows 移植）。

有些契约在 Windows 上**没有可被断言的载体**，不是实现错了：

* 符号链接：Windows 要管理员权限或开发者模式才能创建（`WinError 1314`）。
  被守卫拒绝的行为仍然正确，只是造不出用来试探的链接。
* POSIX 权限位：`os.chmod(p, 0o600)` 在 Windows 上只改只读标志，
  回读仍是 `0o666`；「私有 0600」在那里由 NTFS ACL 继承承担。
* Linux 命名空间沙箱：`unshare`/`setpriv` 在 Windows 上不存在，
  `UnshareSandboxAdapter` 按设计恒为 unavailable。

**skip 不等于通过。** 每个 skip 都写清楚为什么无法断言，免得下次有人
把「跳过」读成「没问题」。
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def _probe_symlinks():
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_text("x", encoding="utf-8")
            (Path(tmp) / "link").symlink_to(target)
        return True
    except (OSError, NotImplementedError):
        return False


def _probe_posix_modes():
    try:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe"
            probe.write_text("x", encoding="utf-8")
            os.chmod(probe, 0o600)
            return stat.S_IMODE(probe.stat().st_mode) == 0o600
    except OSError:
        return False


SYMLINKS_AVAILABLE = _probe_symlinks()
POSIX_MODES_AVAILABLE = _probe_posix_modes()
NAMESPACE_SANDBOX_POSSIBLE = not IS_WINDOWS

requires_symlinks = unittest.skipUnless(
    SYMLINKS_AVAILABLE,
    "本机不能创建符号链接（Windows 需要管理员或开发者模式）："
    "守卫行为本身仍然成立，只是造不出用来试探的链接")

requires_posix_modes = unittest.skipUnless(
    POSIX_MODES_AVAILABLE,
    "本机没有 POSIX 权限位（Windows 的 chmod 只改只读标志）："
    "私有目录语义由 NTFS ACL 继承承担")

def assert_mode(case, path, expected, msg=None):
    """断言 POSIX 权限位；没有权限位的平台上只断言「文件还在」。

    比 skip 掉整个测试好：那些用例里除了权限位还断言了内容、原子性、
    拒绝行为 —— 那些在 Windows 上**照样成立**，不该一起丢掉。
    """
    target = Path(path)
    if not POSIX_MODES_AVAILABLE:
        case.assertTrue(
            target.exists(),
            msg or f"{target} 应存在（本平台无 POSIX 权限位，只验存在性）")
        return
    case.assertEqual(stat.S_IMODE(target.stat().st_mode), expected, msg)


requires_namespace_sandbox = unittest.skipUnless(
    NAMESPACE_SANDBOX_POSSIBLE,
    "Linux 命名空间沙箱在 Windows 上不存在（unshare/setpriv）："
    "UnshareSandboxAdapter 按设计恒为 unavailable")
