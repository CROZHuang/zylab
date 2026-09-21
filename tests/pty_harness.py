"""真实 PTY 测试的状态同步驱动。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import pty
    import select
else:
    # Windows 没有 pty 模块（它 import tty → termios，都是 POSIX 专属）。
    # 等价物是 ConPTY，见 tests/conpty.py。没有这层，所有 PTY 测试在
    # Windows 上连 import 都过不去 —— 也就是说 SPEC-CC-parity 记分板里
    # 大半条契约（Shift+Tab、↑↓ 历史、Ctrl+R、粘贴、Ctrl+O…）
    # 在 Windows 上从来没有被验证过。
    from tests.conpty import ConPTY


@dataclass(frozen=True)
class PTYSend:
    """在 ``after`` 已出现在 PTY 输出后发送一段输入。"""

    payload: bytes
    after: bytes | str | None = None
    delay: float = 0.0
    timeout: float | None = None


def _marker_bytes(marker):
    if marker is None or isinstance(marker, bytes):
        return marker
    return str(marker).encode("utf-8")


def _read_until(master, proc, output, marker, timeout):
    marker = _marker_bytes(marker)
    deadline = time.monotonic() + timeout
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"等待 PTY 标记超时: {marker!r}")
        ready, _, _ = select.select(
            [master], [], [], min(0.1, remaining))
        if ready:
            try:
                chunk = os.read(master, 8192)
            except OSError as exc:
                if marker in output:
                    return
                raise EOFError(
                    f"PTY 在标记 {marker!r} 前关闭") from exc
            if not chunk:
                raise EOFError(
                    f"PTY 在标记 {marker!r} 前结束")
            output.extend(chunk)
            continue
        if proc.poll() is not None:
            raise EOFError(
                f"子进程在标记 {marker!r} 前退出: {proc.returncode}")


class RawPTY:
    """跨平台的「自带 PTY 的 child」。

    给那些不走 run_pty_child、要自己驱动 REPL 的测试用（会话选择器、历史浏览）。
    它们原本直接 `pty.openpty()` + `fcntl.ioctl(TIOCSWINSZ)` —— 三个 POSIX 专属
    模块，Windows 上连 import 都过不去。这里把「开一个带尺寸的 PTY、读、写、
    等退出」收成一个接口，两个平台各自实现。
    """

    def __init__(self, argv, *, cwd=None, env=None, cols=140, rows=40):
        self.cols, self.rows = cols, rows
        if IS_WINDOWS:
            self._conpty = ConPTY(argv, cwd=cwd, env=env, cols=cols, rows=rows)
            self._proc = None
            return
        self._conpty = None
        import fcntl
        import struct
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        try:
            self._proc = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave,
                cwd=cwd, env=env, close_fds=True)
        finally:
            os.close(slave)
        self._master = master

    def read(self, timeout=0.1):
        """尽力读一段；没有数据就返回 b""（不抛）。"""
        if self._conpty is not None:
            return self._conpty.read(timeout)
        ready, _, _ = select.select([self._master], [], [], timeout)
        if not ready:
            return b""
        try:
            return os.read(self._master, 65536)
        except OSError:
            return b""

    def write(self, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if self._conpty is not None:
            self._conpty.write(payload)
        else:
            os.write(self._master, payload)

    def poll(self):
        return (self._conpty.poll() if self._conpty is not None
                else self._proc.poll())

    def wait(self, timeout=None):
        return (self._conpty.wait(timeout) if self._conpty is not None
                else self._proc.wait(timeout=timeout))

    def kill(self):
        if self._conpty is not None:
            self._conpty.kill()
        else:
            self._proc.kill()

    def close(self):
        if self._conpty is not None:
            self._conpty.close()
        else:
            try:
                os.close(self._master)
            except OSError:
                pass


def _read_until_conpty(child, output, marker, timeout):
    marker = _marker_bytes(marker)
    deadline = time.monotonic() + timeout
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"等待 PTY 标记超时: {marker!r}")
        chunk = child.read(min(0.1, remaining))
        if chunk:
            output.extend(chunk)
            continue
        if child.eof:
            if marker in output:
                return
            raise EOFError(f"PTY 在标记 {marker!r} 前结束")
        if child.poll() is not None:
            if marker in output:
                return
            raise EOFError(
                f"子进程在标记 {marker!r} 前退出: {child.returncode}")


def _run_conpty_child(body, sends, *, cwd, timeout, prefix, child_env):
    """Windows 版 run_pty_child：伪控制台里跑同一段 body。

    与 POSIX 版的差异只有一处会影响断言：ConPTY 会把 child 的输出**重新渲染**
    成自己屏幕缓冲区的 VT 重绘，所以行宽要开大，避免长行被折进标记中间。
    """
    child = ConPTY(
        [sys.executable, "-c", prefix + body],
        # 行宽开得很大是**必须**的，不是保险起见：ConPTY 在屏幕缓冲区的列边界上
        # 硬折行，而 child 的 `RESULT:{...}` 是一条很长的逻辑行 —— 折了之后
        # json.loads 会报 "Unterminated string"（实测在第 141 个字符处断掉）。
        # 只要宽到不折，标记与 RESULT 的提取就回到和 POSIX 一样简单。
        cwd=cwd, env=child_env, cols=1000, rows=50)
    output = bytearray()
    try:
        for item in sends:
            action = item if isinstance(item, PTYSend) else PTYSend(
                payload=item[1], delay=item[0])
            if action.after is not None:
                _read_until_conpty(
                    child, output, action.after,
                    timeout if action.timeout is None else action.timeout)
            if action.delay:
                time.sleep(action.delay)
            child.write(action.payload)
        _read_until_conpty(child, output, b"RESULT:", timeout)
        child.wait(timeout)
    except Exception as exc:
        child.kill()
        raise AssertionError(
            "PTY child failed before RESULT:\n"
            + output.decode("utf-8", "replace")) from exc
    finally:
        child.close()
    decoded = output.decode("utf-8", "replace")
    payload = decoded.rsplit("RESULT:", 1)[1].strip().splitlines()[0]
    return decoded, json.loads(payload)


def run_pty_child(body, sends, *, cwd, timeout, prefix=""):
    """运行 PTY child，并返回完整输出与 ``RESULT:`` JSON。

    旧式 ``(delay, payload)`` 仍可用于纯键盘解析测试；涉及 REPL/worker
    状态转换的流程应使用 :class:`PTYSend`，等待可观察标记后再发键。

    HOME 在创建 child 之前统一隔离。很多 core 模块会在 import 时绑定状态
    路径，不能依赖每个测试 body 都记得先设置临时 HOME。
    """

    child_home = tempfile.TemporaryDirectory(prefix="zylab-pty-home-")
    child_env = os.environ.copy()
    child_env["HOME"] = child_home.name
    # 自包含模式下 state_home() 优先取应用目录里的 .zylab-home/——只隔离 HOME 就会碰到真实状态。
    child_env["ZYLAB_HOME"] = os.path.join(child_home.name, ".zylab")
    child_env.pop("ZYLAB_APP_ROOT", None)
    if IS_WINDOWS:
        try:
            return _run_conpty_child(
                body, sends, cwd=cwd, timeout=timeout, prefix=prefix,
                child_env=child_env)
        finally:
            child_home.cleanup()
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", prefix + body],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            close_fds=True,
            env=child_env,
        )
    except BaseException:
        os.close(master)
        child_home.cleanup()
        raise
    finally:
        os.close(slave)
    output = bytearray()
    try:
        for item in sends:
            if isinstance(item, PTYSend):
                action = item
            else:
                delay, payload = item
                action = PTYSend(payload=payload, delay=delay)
            if action.after is not None:
                _read_until(
                    master, proc, output, action.after,
                    timeout if action.timeout is None else action.timeout,
                )
            if action.delay:
                time.sleep(action.delay)
            os.write(master, action.payload)
        _read_until(master, proc, output, b"RESULT:", timeout)
        proc.wait(timeout=timeout)
    except Exception as exc:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1.0)
        decoded = output.decode("utf-8", "replace")
        raise AssertionError(
            "PTY child failed before RESULT:\n" + decoded) from exc
    finally:
        os.close(master)
        child_home.cleanup()

    decoded = output.decode("utf-8", "replace")
    payload = decoded.rsplit("RESULT:", 1)[1].strip().splitlines()[0]
    return decoded, json.loads(payload)


def install_ready_input_pump(marker="ZYLAB_TEST_PUMP_READY"):
    """让 child 在 InputPump 已进入 raw mode 且 reader 启动后发标记。"""

    from core import tui

    base = tui.InputPump

    class ReadyInputPump(base):
        def __enter__(self):
            entered = super().__enter__()
            print(marker, flush=True)
            return entered

    tui.InputPump = ReadyInputPump


def announce_when(
        predicate: Callable[[], bool], marker: str, *, timeout=None,
        on_ready: Callable[[], None] | None = None):
    """后台等待 child 内部状态，满足后发标记并可执行回调。

    默认预算在 Windows 上放宽：这些用例要等真的子代理起来，而每个子代理都会
    拉起一次 Git Bash —— Windows 的进程创建（加上 MSYS 初始化）比 Linux 贵一个
    量级。

    **别把这条当成 J1–J5 的解药**：实测放宽到 30 秒后，
    `test_agent_visibility_pty` 仍然 5 次里只过 1 次，失败点是
    `ZYLAB_TEST_WAITING_1` 没出现而 `_2` 出现了 —— 两个子代理在 Windows 上
    结束得太近，「还剩 1 个在等」这一帧压根没被渲染出来。那是用例在断言一个
    **瞬态**，不是超时不够。
    """
    if timeout is None:
        timeout = 30.0 if IS_WINDOWS else 10.0

    def watch():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ready = bool(predicate())
            except (AttributeError, KeyError, TypeError):
                ready = False
            if ready:
                if on_ready is not None:
                    on_ready()
                print(marker, flush=True)
                return
            time.sleep(0.005)
        print("ZYLAB_TEST_WATCH_TIMEOUT", flush=True)

    thread = threading.Thread(
        target=watch, name=f"pty-watch-{marker}", daemon=True)
    thread.start()
    return thread
