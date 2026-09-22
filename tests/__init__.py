"""测试包：进程内测试的状态目录隔离兜底。

自包含模式下 paths.state_home() 会优先取应用目录里的 .zylab-home/——那是用户的真实状态。
凡是 import 了 tests.* 的测试进程，这里先把 ZYLAB_HOME / ZYLAB_APP_ROOT 指到临时目录；
子进程测试各自显式传 env（见 pty_harness / test_cold_start）。
"""
import os
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
