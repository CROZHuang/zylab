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
import contextlib
import os
import signal
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
# 「有没有 Linux 命名空间」**不等于**「不是 Windows」——macOS 也没有。
# 这条判据原来写成 `not IS_WINDOWS`，于是 2026-09-22 CI 的 macOS 那列 5 条
# test_sandbox 照样跑了起来，撞在 `sandbox.prepare()` 真的去读
# `/proc/self/ns/net` 上（`SandboxUnavailable: FileNotFoundError`）。
# 和之前那条 fork 守卫同一个毛病：只问「不是那个平台吗」，
# 而不是问「**我要的那个东西在不在**」。
NAMESPACE_SANDBOX_POSSIBLE = (
    sys.platform.startswith("linux")
    and os.path.exists("/proc/self/ns/net"))

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
    "本机没有 Linux 命名空间（Windows 与 macOS 都没有 unshare/setpriv，也没有 /proc/self/ns/net）："
    "UnshareSandboxAdapter 在那些平台上按设计恒为 unavailable")


def patience(seconds):
    """把在开发机上调出来的等待预算，按这台机器的实际速度放大。

    2026-09-22 公开仓库 CI 的 Windows 那列：`workspace.wait(run_id, timeout=1.0)`
    连着三处 `False is not true`。那个 1.0 秒是在一台 8 核、开着 50 天、空闲的
    开发机上调出来的；GitHub 的 Windows runner 是共享的慢核，起一个子进程加
    MSYS 初始化就比这里贵一个量级。

    **和「等信号而不是调睡眠」不冲突**：这里等的本来就是信号
    （`workspace.wait` 就是那个原语），只是预算给小了。能等信号的地方都该等信号；
    等不到才算失败——这个函数只决定「等多久才认输」。

    放大倍数按环境定，不按用例定：`ZYLAB_TEST_PATIENCE` 可显式覆盖。
    """
    factor = os.environ.get("ZYLAB_TEST_PATIENCE")
    if factor:
        try:
            return float(seconds) * max(1.0, float(factor))
        except ValueError:
            pass
    if IS_WINDOWS:
        return float(seconds) * 5.0
    if os.environ.get("CI"):
        return float(seconds) * 3.0
    return float(seconds)


def canonical_tempdir(prefix=None):
    """`TemporaryDirectory`，但目录路径是**规范路径**（没有符号链接组件）。

    macOS 的 `TMPDIR` 是 `/var/folders/…`，而 `/var` 是系统给的、指向
    `/private/var` 的符号链接（`/tmp`、`/etc` 同理）。于是任何「路径里不许有
    符号链接」的契约，在 macOS 上拿标准临时目录去测就**必然**失败——失败的是
    夹具，不是被测的规则。

    2026-09-22 公开仓库 CI 的 macOS 那列：71 个失败块里 **48 个**是同一条
    `checkpoint root 路径含符号链接: /var`。而 checkpoints 那条规则是刻意的安全
    立场（防止有人塞一条软链，把 checkpoint 里的用户文件内容重定向到别处），
    **不该为了让一列变绿去削弱它**——该改的是这里：把夹具的路径先解析好。

    返回 `(holder, path)`：holder 要保活（它析构时才删目录），path 已解析。
    """
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    return holder, Path(os.path.realpath(holder.name))


# 「进程被硬杀」在两个平台上的**退出码表示不一样**，而契约是同一个。
#
# POSIX 用负数编码「被信号杀死」（SIGKILL → -9）。Windows 没有信号：
# `os.kill(pid, 9)` 实际走 `TerminateProcess(handle, 9)`，于是退出码就是 **9**
# （CPython 文档原话：除 CTRL_C_EVENT/CTRL_BREAK_EVENT 之外，sig 被当成退出码）。
# 而 `signal.SIGKILL` 在 Windows 的 signal 模块里**根本不存在**。
#
# 2026-09-22 CI 两列 Windows 的 `test_sigkill_before_switch_leaves_valid_temp_
# and_no_target` 就是它：`AttributeError: module 'signal' has no attribute
# 'SIGKILL'`。改的只是「怎么杀」和「退出码怎么写」——**恢复逻辑一行没动**，
# 那是数据安全路径。
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)


def hard_kill_returncode():
    return HARD_KILL_SIGNAL if IS_WINDOWS else -HARD_KILL_SIGNAL


# 取消一个**忽略 SIGINT 的**任务（比如 bash）之后，快照里的 signal 该是什么。
#
# POSIX：`TaskState.record_returncode` 记的是 `-returncode`，也就是**真正杀死
# 进程的那个信号**。bash 忽略 SIGINT，所以升级阶梯一定走到 SIGKILL —— 可以钉死。
#
# Windows：没有「被信号杀死」的负退出码（终止走 Job Object / taskkill，退出码是
# 1），记的是**我们停到了哪一级**（`_stop_stage`：SIGINT → SIGTERM → SIGKILL）。
# Git Bash 在第一级 CTRL_C 就退掉是**更好**的结果，所以那边只能断言「在阶梯里」，
# 不能钉某个数字（2026-09-22 CI 两列 Windows：`<Signals.SIGINT: 2> != 9`）。
STOP_LADDER = (signal.SIGINT, signal.SIGTERM, HARD_KILL_SIGNAL)


def expected_stop_signals():
    return STOP_LADDER if IS_WINDOWS else (HARD_KILL_SIGNAL,)


@contextlib.contextmanager
def canonical_tmp():
    """`with canonical_tmp() as root:` —— 临时目录，且路径**已解析**。

    与 `canonical_tempdir()` 同一个理由（macOS 的 `/var`→`/private/var`），
    只是形态适合 `with` 块。这一条在需要 `chdir` 的用例上尤其要紧：
    `os.getcwd()` 返回的是**物理**路径，拿未解析的 root 拼出来的期望值与产品
    报出来的永远差一个 `/private`（2026-09-22 CI 的 macOS 那列，
    `test_session_picker.ResumeCwdTransactionTests` 两条）。
    """
    with tempfile.TemporaryDirectory() as root:
        yield os.path.realpath(root)
