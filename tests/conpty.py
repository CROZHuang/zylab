"""Windows 伪控制台（ConPTY）驱动：tests/pty_harness 在 win32 上的 PTY 后端。

POSIX 用 pty.openpty() 给 child 一个真终端；Windows 没有 pty 模块，等价物是
kernel32 的 CreatePseudoConsole（Windows 10 1809+，Windows Terminal 用的就是它）。
child 看到的是一个真控制台：isatty() 为真、GetConsoleMode 成功、按键以
INPUT_RECORD 形式到达 —— 与用户在 Windows Terminal 里启动 zylab 是同一条路径。

只用 stdlib + ctypes，零三方依赖。

与 POSIX pty 的差异（写断言时要知道）：
  * ConPTY 在 child 与我们之间**重新渲染**：输出流是它的屏幕缓冲区的 VT 重绘，
    不是 child 写出的原始字节。文本内容会出现，但中间可能夹着光标定位序列，
    超过列宽的行会被折行。短标记（READY/RESULT 前缀）照常可匹配。
  * 写入的字节被 ConPTY 当作「终端收到的按键」翻译成 INPUT_RECORD：
    ``\\x1b[A`` 变成 ↑ 键事件，``\\r`` 变成 Enter，``\\x03`` 变成 Ctrl+C。
"""
from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import threading
import time
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HPCON = wintypes.HANDLE
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
STILL_ACTIVE = 259
INFINITE = 0xFFFFFFFF


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW),
                ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


_kernel32.CreatePseudoConsole.argtypes = [
    _COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
    ctypes.POINTER(HPCON)]
_kernel32.CreatePseudoConsole.restype = ctypes.c_long
_kernel32.ResizePseudoConsole.argtypes = [HPCON, _COORD]
_kernel32.ResizePseudoConsole.restype = ctypes.c_long
_kernel32.ClosePseudoConsole.argtypes = [HPCON]
_kernel32.ClosePseudoConsole.restype = None
_kernel32.CreatePipe.argtypes = [
    ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
    ctypes.c_void_p, wintypes.DWORD]
_kernel32.CreatePipe.restype = wintypes.BOOL
_kernel32.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(ctypes.c_size_t)]
_kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
_kernel32.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
_kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
_kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
_kernel32.DeleteProcThreadAttributeList.restype = None
_kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.c_void_p, ctypes.POINTER(_PROCESS_INFORMATION)]
_kernel32.CreateProcessW.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _check(ok, what):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), what)


def _env_block(env):
    # CreateProcessW 的 Unicode 环境块：按名字（不分大小写）排序的
    # "K=V\0" 序列，末尾再多一个 \0。
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    text = "".join(f"{k}={v}\0" for k, v in items) + "\0"
    return ctypes.create_unicode_buffer(text, len(text))


class ConPTY:
    """一个跑在伪控制台里的 child 进程。

    read() 从后台线程填充的缓冲区取数据；ConPTY 的输出管道必须持续排空，
    否则 child 写控制台会阻塞，ClosePseudoConsole 也会卡住。
    """

    def __init__(self, argv, *, cwd=None, env=None, cols=80, rows=24):
        self.cols, self.rows = int(cols), int(rows)
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._eof = False
        self._closed = False
        self.returncode = None

        in_read, in_write = wintypes.HANDLE(), wintypes.HANDLE()
        out_read, out_write = wintypes.HANDLE(), wintypes.HANDLE()
        _check(_kernel32.CreatePipe(
            ctypes.byref(in_read), ctypes.byref(in_write), None, 0),
            "CreatePipe(in)")
        _check(_kernel32.CreatePipe(
            ctypes.byref(out_read), ctypes.byref(out_write), None, 0),
            "CreatePipe(out)")
        self._hpc = HPCON()
        hr = _kernel32.CreatePseudoConsole(
            _COORD(self.cols, self.rows), in_read, out_write, 0,
            ctypes.byref(self._hpc))
        # ConPTY 自己复制了这两端；我们这边必须关掉，EOF 才能传过来。
        _kernel32.CloseHandle(in_read)
        _kernel32.CloseHandle(out_write)
        if hr != 0:
            _kernel32.CloseHandle(in_write)
            _kernel32.CloseHandle(out_read)
            raise OSError(f"CreatePseudoConsole failed: HRESULT {hr & 0xFFFFFFFF:#x}")

        size = ctypes.c_size_t(0)
        _kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        self._attrs = (ctypes.c_byte * size.value)()
        _check(_kernel32.InitializeProcThreadAttributeList(
            self._attrs, 1, 0, ctypes.byref(size)),
            "InitializeProcThreadAttributeList")
        # lpValue 传的是 HPCON 本身（不是指向它的指针），见 MS 文档示例。
        _check(_kernel32.UpdateProcThreadAttribute(
            self._attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            self._hpc, ctypes.sizeof(HPCON), None, None),
            "UpdateProcThreadAttribute")

        siex = _STARTUPINFOEXW()
        siex.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        # 父进程（测试 runner）的 std 句柄常被重定向成管道；不显式清空的话
        # child 会继承它们，输出绕过伪控制台（node-pty 同款处理）。
        siex.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        siex.StartupInfo.hStdInput = None
        siex.StartupInfo.hStdOutput = None
        siex.StartupInfo.hStdError = None
        siex.lpAttributeList = ctypes.cast(self._attrs, ctypes.c_void_p)

        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        env_block = _env_block(os.environ if env is None else env)
        pi = _PROCESS_INFORMATION()
        _check(_kernel32.CreateProcessW(
            None, cmdline, None, None, False,
            EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
            env_block, None if cwd is None else str(cwd),
            ctypes.byref(siex), ctypes.byref(pi)),
            "CreateProcessW")
        _kernel32.CloseHandle(pi.hThread)
        self._hproc = pi.hProcess
        self.pid = int(pi.dwProcessId)

        self._in_fd = msvcrt.open_osfhandle(in_write.value, os.O_WRONLY)
        self._out_fd = msvcrt.open_osfhandle(out_read.value, os.O_RDONLY)
        self._reader = threading.Thread(
            target=self._pump, name=f"conpty-reader-{self.pid}", daemon=True)
        self._reader.start()

    # ---- 输出 --------------------------------------------------------------
    def _pump(self):
        while True:
            try:
                chunk = os.read(self._out_fd, 65536)
            except OSError:
                chunk = b""
            with self._cond:
                if not chunk:
                    self._eof = True
                    self._cond.notify_all()
                    return
                self._buf.extend(chunk)
                self._cond.notify_all()

    def read(self, timeout=None):
        """取走目前缓冲的全部输出；没有数据时最多等 timeout 秒。EOF 且空返回 b""。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._buf and not self._eof:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    break
                self._cond.wait(left)
            data = bytes(self._buf)
            self._buf.clear()
            return data

    def readable(self, timeout):
        with self._cond:
            if self._buf or self._eof:
                return True
            self._cond.wait(timeout)
            return bool(self._buf or self._eof)

    @property
    def eof(self):
        with self._cond:
            return self._eof and not self._buf

    # ---- 输入 / 尺寸 ---------------------------------------------------------
    def write(self, data):
        view = memoryview(bytes(data))
        while view:
            n = os.write(self._in_fd, view)
            view = view[n:]

    def resize(self, cols, rows):
        self.cols, self.rows = int(cols), int(rows)
        hr = _kernel32.ResizePseudoConsole(self._hpc, _COORD(self.cols, self.rows))
        if hr != 0:
            raise OSError(f"ResizePseudoConsole failed: HRESULT {hr & 0xFFFFFFFF:#x}")

    # ---- 进程 ----------------------------------------------------------------
    def poll(self):
        if self.returncode is None:
            if _kernel32.WaitForSingleObject(self._hproc, 0) == WAIT_OBJECT_0:
                code = wintypes.DWORD()
                _kernel32.GetExitCodeProcess(self._hproc, ctypes.byref(code))
                self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout=None):
        ms = INFINITE if timeout is None else max(0, int(timeout * 1000))
        if _kernel32.WaitForSingleObject(self._hproc, ms) == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired("conpty-child", timeout)
        return self.poll()

    def kill(self):
        if self.poll() is None:
            _kernel32.TerminateProcess(self._hproc, 1)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._in_fd)
        except OSError:
            pass
        # ClosePseudoConsole 可能等输出排空（旧版 Windows 同步阻塞），
        # 放到线程里做，reader 继续排空直到 EOF。
        closer = threading.Thread(
            target=_kernel32.ClosePseudoConsole, args=(self._hpc,), daemon=True)
        closer.start()
        closer.join(5.0)
        self._reader.join(5.0)
        try:
            os.close(self._out_fd)
        except OSError:
            pass
        _kernel32.DeleteProcThreadAttributeList(self._attrs)
        self.poll()
        _kernel32.CloseHandle(self._hproc)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.kill()
        self.close()
        return False
