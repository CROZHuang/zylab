"""测试包：进程内测试的状态目录隔离兜底。

自包含模式下 paths.state_home() 会优先取应用目录里的 .zylab-home/——那是用户的真实状态。
凡是 import 了 tests.* 的测试进程，这里先把 ZYLAB_HOME / ZYLAB_APP_ROOT 指到临时目录；
子进程测试各自显式传 env（见 pty_harness / test_cold_start）。
"""
import os
import sys
import tempfile

_ISOLATED = tempfile.mkdtemp(prefix="zylab-tests-")
os.environ.setdefault("ZYLAB_APP_ROOT", _ISOLATED)
os.environ.setdefault("ZYLAB_HOME", os.path.join(_ISOLATED, ".zylab"))
# 挂住的用例要**自己把栈打出来**。
#
# 2026-09-22：CI 的 macOS 那列单元测试步骤一直不结束，上一轮被判 cancelled——
# 一个挂住的 job 比一个红的 job 难查得多，因为它连结论都不给，而 job 日志又要
# 仓库 admin 权限才下得到。faulthandler 到点会把**所有线程**的栈打到 stderr 再
# 硬退出，于是它变成一次带栈的失败：退出码非零 → CI 的报告器照常把尾部写成注解。
#
# 只在显式要求时开（CI 会设 CI=true），本地跑测试不受影响。
# 时限要小于 workflow 的 timeout-minutes，否则 job 先被杀、栈就没了。
_WATCHDOG = os.environ.get("ZYLAB_TEST_WATCHDOG") or (
    "1200" if os.environ.get("CI") else "")
if _WATCHDOG:
    try:
        import faulthandler
        faulthandler.dump_traceback_later(float(_WATCHDOG), exit=True)
    except (ImportError, ValueError):       # 时限写坏了不该挡住测试
        pass
# Windows 上「删不掉还被人打开着的文件」是平台事实，不是被测对象的缺陷。
#
# 2026-09-22 公开仓库 CI 的 Windows 那列：注解里三条 traceback 形状完全一样——
#
#     AssertionError / （或什么都没有）
#     During handling of the above exception, another exception occurred:
#       tempfile.TemporaryDirectory.__exit__ → shutil._rmtree_unsafe → os.unlink
#       PermissionError: [WinError 32] The process cannot access the file
#                        because it is being used by another process
#
# 凡是起过子进程、或留着一个没关的 sqlite 连接的用例（尾部那句
# `ResourceWarning: unclosed database` 就是它），在清理临时目录时都会这样。
# 于是**通过的用例也被记成 ERROR**，整列 Windows 的读数变得无法解释：
# 注解里列出的 40 条全是 ERROR，没有一条 FAIL。
#
# 标准库自己给了答案（3.10 起的 `ignore_cleanup_errors`）。在这里统一打开，
# 只在 Windows，POSIX 一字不变——那边删打开着的文件本来就合法，
# 清理失败在那边是真信号，不该一起吞掉。
#
# **这不是把 Windows 的问题扫到地毯下**：没关的连接仍然由 ResourceWarning 报出来，
# 而用例真正的失败原因（断言）也终于能露出来了。
if sys.platform == "win32":
    _real_tmpdir = tempfile.TemporaryDirectory

    class _TolerantTemporaryDirectory(_real_tmpdir):
        """`TemporaryDirectory`，但在 Windows 上清理失败不抛。"""

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("ignore_cleanup_errors", True)
            super().__init__(*args, **kwargs)

    tempfile.TemporaryDirectory = _TolerantTemporaryDirectory
