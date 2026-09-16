"""真实 PTY 测试的状态同步驱动。"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable


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
        predicate: Callable[[], bool], marker: str, *, timeout=10.0,
        on_ready: Callable[[], None] | None = None):
    """后台等待 child 内部状态，满足后发标记并可执行回调。"""

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
