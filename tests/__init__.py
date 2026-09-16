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
