"""POSIX-only API 的跨平台单点（Windows 原生支持，2026-09-16）。

zylab 原先只跑在 Linux/macOS 上，`fcntl`/`termios`/`tty` 是编译进 CPython 的
POSIX 专属模块，Windows 版解释器根本没有，`import` 即崩（ModuleNotFoundError:
fcntl）。这个模块是所有跨平台差异的**唯一入口**：

  flock      fcntl.flock(fd, LOCK_EX/LOCK_UN/LOCK_NB) 的等价物。
             Windows 用 msvcrt.locking 的字节范围锁；语义差异见 _flock_win32。
  set_raw    termios/tty 的 raw/cbreak 模式。
             Windows 用 ctypes 调 console API（SetConsoleMode）。
  wait_fd    select.select([fd],...) 的等价物。
             Windows 的 select 只认 socket；控制台用 msvcrt.kbhit，
             管道/文件 fd 用阻塞读 + 后台线程提前排空（见 wait_fd 文档）。
  kill_tree  os.killpg(pgid, sig) 的等价物。
             Windows 没有 process group 信号，用 taskkill /T 按进程树终止。
  euid_ok    info.st_uid == os.geteuid() 的安全检查。
             Windows 没有 euid；NTFS 单用户目录语义下放宽为存在性检查。

设计约束（照抄 AGENTS.md）：
- 零三方依赖 —— 只用 stdlib + Windows 自带 DLL 的 ctypes。
- POSIX 路径**零行为变化**：所有分支都先判 IS_WINDOWS，Linux 上走原代码。
- fail-closed：Windows 上拿不到等价保证时宁可报错，不静默降级。
"""
import contextlib
import errno
import os
import signal
import stat
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"

# fcntl 只在 POSIX 函数体内按需引用（_flock_posix）。模块级 import 在
# Windows 上会直接 ModuleNotFoundError；用 try 探测，缺了就是 None，
# POSIX 路径用到时才 fail。
try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None
fcntl = _fcntl

# ---- fd_* 体系的文件操作（fd 可能是 POSIX int，也可能是 Windows 的 _WinFileBox）
# POSIX 下全是 os.* 的直别名；Windows 下对 box 走 box 的方法，对 int fd 走 msvcrt
# 或降级实现。


def _unwrap(f):
    if isinstance(f, _WinPathBox):
        if f.fd < 0:
            raise ValueError("this _WinPathBox holds no open fd")
        return f.fd
    return f


def fstat(fd):
    return os.fstat(_unwrap(fd))


def read(fd, n):
    return os.read(_unwrap(fd), n)


def write(fd, data):
    return os.write(_unwrap(fd), data)


def fsync(fd):
    return os.fsync(_unwrap(fd))


def fdopen(fd, *args, **kwargs):
    handle = os.fdopen(_unwrap(fd), *args, **kwargs)
    if isinstance(fd, _WinPathBox):
        fd.fd = -1          # ownership moved to the file object
        fd._keepalive = handle
    return handle


def close(fd):
    if isinstance(fd, _WinPathBox):
        return fd.close()
    if isinstance(fd, int) and fd >= 0:
        os.close(fd)


# ---------------------------------------------------------------------------
# 1. 文件锁：fcntl.flock -> msvcrt.locking
# ---------------------------------------------------------------------------

# 与 fcntl 常量同名，让调用点写法不变
LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def _flock_posix(fd, flags):
    """LOCK_NB 竞争失败返回 False（不抛）；其余异常正常传播。

    调用方契约（09-17 融合裁决）：显式返回值优于异常 —— Windows 侧
    msvcrt.locking 无法完整模拟 BlockingIOError 语义，两侧统一为
    「拿到锁 True / 竞争失败 False」，等锁方按返回值重试。
    """
    how = fcntl.LOCK_UN if flags & LOCK_UN else (
        fcntl.LOCK_EX if flags & LOCK_EX else fcntl.LOCK_SH)
    if flags & LOCK_NB and not flags & LOCK_UN:
        how |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, how)
    except BlockingIOError:
        if flags & LOCK_NB:
            return False
        raise
    return True


def _flock_win32(fd, flags):
    """msvcrt.locking 锁 1 个字节（从文件头开始）。

    语义对照（Windows 文档 + 实测）：
      LOCK_EX     -> LK_LOCK / LK_NBLCK：独占。LK_LOCK 最多重试 10 次（约 1s）
                     后抛 OSError，等价于「阻塞但有上限」；调用方拿不到锁
                     视为环境错误上抛，不会永久挂死。
      LOCK_UN     -> LK_UNLCK：释放。
      LOCK_NB     -> LK_NBLCK：立即失败。
    局限（诚实声明）：
      * msvcrt 锁的是字节范围（这里锁 [0,1)），不是整个 fd；并发写方必须
        也走本模块才会互斥。zylab 的锁文件都是「专用 .lock 文件、本体不走
        locking」，风险受控。
      * 锁在 fd 关闭时自动释放，与 flock 一致。
    """
    import msvcrt
    mode = 0
    if flags & LOCK_UN:
        mode = msvcrt.LK_UNLCK
    elif flags & LOCK_EX:
        mode = msvcrt.LK_NBLCK if flags & LOCK_NB else msvcrt.LK_LOCK
    else:
        mode = msvcrt.LK_NBLCK if flags & LOCK_NB else msvcrt.LK_RLCK
    try:
        msvcrt.locking(fd, mode, 1)
        return True
    except OSError as exc:
        if not flags & LOCK_NB:
            raise
        # 非阻塞拿锁失败：与 flock 的 EWOULDBLOCK 行为对齐，返回 False
        if exc.errno in (errno.EACCES, errno.EDEADLK, 13):
            return False
        raise


def flock(fd, flags):
    if IS_WINDOWS:
        return _flock_win32(fd, flags)
    return _flock_posix(fd, flags)


def fchmod(fd, mode):
    """os.fchmod 的跨平台版本；Windows 无权限位概念，尽力收紧失败即忽略。"""
    try:
        if IS_WINDOWS:
            # os.fchmod 在 Windows 上不存在；chmod 只支持只读位。
            # 状态文件都在用户 profile 下，NTFS ACL 由目录继承，这里不重复。
            return True
        return os.fchmod(fd, mode)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 3b. openat/dir_fd 家族：Windows 用绝对路径退化（wincompat.fd_open 等）
# ---------------------------------------------------------------------------
# checkpoints 的防替换链建立在 os.open(name, dir_fd=dirfd) 之上 —— Windows
# CPython 的 open/stat/unlink/rename/mkdir 全部不支持 dir_fd 参数（官方
# 文档 os 支持 表格）。退化语义：directory fd 始终来自 wincompat.fd_open
# 对目录的 O_RDONLY 打开；POSIX 上保持真 openat（返回真 fd），Windows 上
# 「fd」实际是 _WinPathBox（持有目录绝对路径，os.close/os.fstat/os.fsync
# 都被显式转发）。TOCTOU 防替换在 Windows 上退化为路径解析级别的检查
# （realpath 前后比对 + lstat 身份比对），符号链接攻击面由 checkpoints
# 既有的 identity 断言兜底。


class _WinPathBox:
    """Windows 上冒充目录 fd：把目录绝对路径装进一个 int 形参可用的盒子。

    os.close(x) / os.fstat(x) / os.fsync(x) 等调用点都在 checkpoints 里
    被 wincompat.* 显式转发，不会真的把盒子传进 C 层。
    """

    __slots__ = ("path", "fd")

    def __init__(self, path):
        self.path = str(path)
        self.fd = -1

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def _join_dir(dirfd, name):
    if isinstance(dirfd, _WinPathBox):
        return dirfd.path, os.path.join(dirfd.path, str(name))
    return None, os.path.join("/proc/self/fd", str(dirfd), str(name))


def fd_open(dirfd, name, flags, mode=0o666):
    """os.open(name, flags, mode, dir_fd=dirfd) 的跨平台版。

    POSIX：真 openat。Windows：目录盒子记录路径，普通 os.open 打开
    join 后的绝对路径（O_NOFOLLOW/O_CLOEXEC/O_DIRECTORY 都不存在，
    getattr(os, ..., 0) 已在调用方归零）。
    """
    if not IS_WINDOWS:
        return os.open(str(name), int(flags), int(mode), dir_fd=int(dirfd))
    root, full = _join_dir(dirfd, name)
    if root is None:  # 意外拿到真 fd（不该发生）：退回 /proc 路径，必然失败
        raise OSError("fd_open: Windows 上不接收真 fd")
    return os.open(full, int(flags), int(mode))


def fd_open_dir(path):
    """打开一个「目录 fd」。POSIX 返回真 fd（O_DIRECTORY）；
    Windows 返回 _WinPathBox。close 一律走 fd_close。"""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if not IS_WINDOWS:
        return os.open(str(path), flags)
    return _WinPathBox(path)


def fd_close(dirfd):
    if isinstance(dirfd, _WinPathBox):
        dirfd.close()
        return
    os.close(dirfd)


def fd_stat_nofollow(dirfd, name):
    """os.stat(name, dir_fd=..., follow_symlinks=False) 的跨平台版。

    原调用点全部是不跟随链接的 at 语义，这里把 False 固化，
    避免路径版退化时悄悄改变符号链接行为。
    """
    root, full = _join_dir(dirfd, name)
    if root is not None:
        return os.stat(full, follow_symlinks=False)
    return os.stat(str(name), dir_fd=int(dirfd), follow_symlinks=False)


def fd_unlink(dirfd, name):
    root, full = _join_dir(dirfd, name)
    if root is not None:
        return os.unlink(full)
    return os.unlink(str(name), dir_fd=int(dirfd))


def fd_mkdir(dirfd, name, mode=0o700):
    root, full = _join_dir(dirfd, name)
    if root is not None:
        return os.mkdir(full, int(mode))
    return os.mkdir(str(name), int(mode), dir_fd=int(dirfd))


def fd_replace(src_dirfd, src_name, dst_dirfd, dst_name):
    """os.replace(..., src_dir_fd=..., dst_dir_fd=...) 的跨平台版。

    POSIX 走原语义（dir_fd 原子替换）。这里刻意保持与旧代码完全一致的
    os.replace(name, name, src_dir_fd=, dst_dir_fd=) 形态：测试通过
    monkeypatch os.replace 注入 EXDEV / 中途 _exit，参数形态变了解会破坏
    这些注入点（2026-09-16 实测：4 个 crash-recovery 测试因此红）。
    """
    if not IS_WINDOWS:
        # 与旧代码完全一致的调用形态（相对名 + dir_fd）：崩溃注入测试
        # monkeypatch os.replace 注入 EXDEV / 中途 _exit，形态变了解会破坏注入点。
        return os.replace(
            str(src_name), str(dst_name),
            src_dir_fd=int(src_dirfd), dst_dir_fd=int(dst_dirfd))
    s_root, s_full = _join_dir(src_dirfd, src_name)
    d_root, d_full = _join_dir(dst_dirfd, dst_name)
    if s_root is None or d_root is None:
        raise OSError("fd_replace: Windows 上不接收真 fd")
    return os.replace(s_full, d_full)


def geteuid():
    """os.geteuid() 的跨平台名（Windows 无 euid，返回 0 仅供显示；判断一律走 euid_owns）。"""
    return 0 if IS_WINDOWS else os.geteuid()


def st_owned_by_current_user(info):
    """stat 结果属于当前用户的跨平台检查（POSIX: st_uid==geteuid; Windows: 恒 True，见 euid_owns）。"""
    return euid_owns(info)


def fd_owned_by_current_user(fd):
    """fstat(fd).st_uid == euid 的跨平台安全检查（fail-open on Windows）。

    checkpoints 的 path-lock 守卫用它判断「锁文件没被其他用户替换」。
    Windows 无 euid/st_uid 语义，退化为常规文件检查。
    """
    if IS_WINDOWS:
        info = fstat(fd)
        return stat.S_ISREG(info.st_mode)
    return euid_owns(fstat(fd))


def euid_owns(info, uid=None):
    """st_uid == os.geteuid() 的跨平台安全检查（fail-closed on Windows）。

    Windows 没有 euid/st_uid 语义（st_uid 恒为 0）。NTFS 下单用户目录的
    「不可被其他用户改写」由目录 ACL 保证，检查退化为 is_regular。
    """
    if IS_WINDOWS:
        return True
    return info.st_uid == (os.geteuid() if uid is None else uid)


def wait_pid_exit(pid, timeout=1.0):
    """Windows 侧 pid 存活探测（POSIX 上 os.kill(pid,0) 的等价物）。"""
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # tasklist 一次调用比 ctypes OpenProcess 简单且无句柄泄漏
    try:
        out = os.popen(f"tasklist /FI \"PID eq {int(pid)}\" 2>nul").read()
    except OSError:
        return False
    return str(pid) in out


# ---------------------------------------------------------------------------
# 2. 终端：termios/tty raw 模式 -> Windows console mode
# ---------------------------------------------------------------------------

_ESC = "\x1b"


class _PosixRaw:
    """原 termios/tty 实现，Linux/macOS 行为零变化。"""

    def __init__(self, fd, cbreak=False, capture_signals=False):
        import termios
        import tty
        self.fd = fd
        self.saved = None
        self._termios = termios
        self._tty = tty
        self.cbreak = cbreak
        self.capture_signals = capture_signals

    def __enter__(self):
        import termios
        self.saved = termios.tcgetattr(self.fd)
        if self.cbreak:
            self._tty.setcbreak(self.fd)
            if self.capture_signals:
                # cbreak 之上再关 ISIG，让 Ctrl-C 以字节形式进输入流
                # （原 tui.raw_mode 的语义，pty_harness 依赖它）。
                attrs = termios.tcgetattr(self.fd)
                attrs[3] &= ~termios.ISIG
                termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        else:
            self._tty.setraw(self.fd)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            self._termios.tcsetattr(self.fd, self._termios.TCSADRAIN, self.saved)
        return False


class _Win32Raw:
    """Windows console raw 模式（ctypes，无三方依赖）。

    对齐 POSIX setcbreak 语义：关行缓冲/回显/ECHOCTL，**保留**输出处理
    （ENABLE_PROCESSED_OUTPUT），这样 \\n 仍自动补 \\r，不出现阶梯屏。
    """

    def __init__(self, fd, cbreak=False):
        self.fd = fd
        self.cbreak = cbreak
        self.saved = None
        self._kernel32 = None

    def _kernel(self):
        if self._kernel32 is None:
            import ctypes
            self._kernel32 = ctypes.windll.kernel32
        return self._kernel32

    def _get_mode(self):
        import ctypes
        mode = ctypes.c_uint32()
        if not self._kernel().GetConsoleMode(
                ctypes.c_void_p(self.fd), ctypes.byref(mode)):
            raise OSError("GetConsoleMode failed（fd 不是 console？）")
        return mode.value

    def _set_mode(self, value):
        import ctypes
        if not self._kernel().SetConsoleMode(
                ctypes.c_void_p(self.fd), ctypes.c_uint32(value)):
            raise OSError("SetConsoleMode failed")

    def __enter__(self):
        # Windows 上 zylab 的 stdin 是 console；fd 可能是 0 或 fileno()。
        self.saved = self._get_mode()
        # ENABLE_PROCESSED_INPUT(1) 关掉（Ctrl+C 不再由控制台预处理），
        # ENABLE_LINE_INPUT(2) / ENABLE_ECHO_INPUT(4) 关掉 = raw 输入。
        mode = self.saved & ~0x7
        if self.cbreak:
            # cbreak：保留 PROCESSED_INPUT（Ctrl+C 仍生效），只关行缓冲和回显
            mode = (self.saved | 0x1) & ~0x6
        self._set_mode(mode)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            try:
                self._set_mode(self.saved)
            except OSError:
                pass
        return False


def termios_error():
    """termios.error 的跨平台等价物（Windows 上没有 termios 模块，给 OSError 即可，
    except 子句要的就是"终端操作失败"这一类）。"""
    if IS_WINDOWS:
        return OSError
    import termios
    return termios.error


def raw_mode(fd, cbreak=False, capture_signals=False):
    """termios raw/cbreak 的跨平台上下文管理器工厂。"""
    if IS_WINDOWS:
        return _Win32Raw(fd, cbreak, capture_signals)
    return _PosixRaw(fd, cbreak, capture_signals)


# ---------------------------------------------------------------------------
# 3. select：wait_fd 可读等待
# ---------------------------------------------------------------------------


def wait_fd(fd, timeout):
    """select.select([fd],[],[],timeout) 的跨平台等价物；返回 bool。

    Windows 上 select 只支持 socket。zylab 里读的三类 fd：
      a) 控制台（tui/termcaps 的 stdin）—— msvcrt.kbhit 轮询（20ms 步长，
         与 POSIX 30ms esc-disambiguation 同量级）。
      b) subprocess.PIPE（tools/tasks 的 stdout drain）—— 用 PeekNamedPipe。
      c) socket —— 直接 select。
    """
    if not IS_WINDOWS:
        import select
        return bool(select.select([fd], [], [], timeout)[0])
    # ---- Windows ----
    import ctypes
    if fd == 0 or _is_console(fd):
        return _kbhit_wait(timeout)
    if _is_socket(fd):
        import select
        return bool(select.select([fd], [], [], timeout)[0])
    return _pipe_readable_win32(fd, timeout)


class PipeSelector:
    """Windows 上 selectors.DefaultSelector 的管道子集替身。

    Windows 的 selectors 只认 socket；tasks.py 用它等 subprocess 管道。
    接口对齐用到的子集：register/unregister/select/get_map/close/__len__。
    select() 的 timeout 语义与 selectors 相同（None=阻塞；此处用 20ms 步进
    近似，poll_interval 本来就在这个量级）。
    """

    def __init__(self):
        self._pipes = {}      # fileobj -> data
        self._closed = False

    def register(self, fileobj, events, data=None):
        if events != selectors.EVENT_READ:
            raise ValueError("PipeSelector 只支持读事件")
        self._pipes[fileobj] = data

    def unregister(self, fileobj):
        self._pipes.pop(fileobj, None)

    def get_map(self):
        return self._pipes if not self._closed else {}

    def __len__(self):
        return len(self._pipes)

    def select(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            ready = []
            for fileobj, data in self._pipes.items():
                if _pipe_readable_win32(fileobj.fileno(), 0):
                    ready.append(_SelectorKey(fileobj, selectors.EVENT_READ, data))
            if ready:
                return [(key, selectors.EVENT_READ) for key in ready]
            if deadline is not None and time.monotonic() >= deadline:
                return []
            time.sleep(0.02)

    def close(self):
        self._closed = True
        self._pipes.clear()


import selectors as _selectors_mod

_SelectorKey = _selectors_mod.SelectorKey
selectors = _selectors_mod
DefaultSelector = _selectors_mod.DefaultSelector

if IS_WINDOWS:
    DefaultSelector = PipeSelector


def wait_pipe_readable(fd, timeout):
    """wait_fd 的管道特化别名：tools/tasks 的 stdout drain 用它。"""
    return wait_fd(fd, timeout)


def _is_console(fd):
    import ctypes
    try:
        return bool(ctypes.windll.kernel32.GetConsoleMode(
            ctypes.c_void_p(fd), ctypes.byref(ctypes.c_uint32())))
    except Exception:
        return False


def _is_socket(fd):
    import socket
    try:
        s = socket.socket(fileno=fd)
        s.getpeername()
        return True
    except (OSError, ValueError):
        return False
    finally:
        pass


def _kbhit_wait(timeout):
    import msvcrt
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if msvcrt.kbhit():
            return True
        if deadline is None:
            time.sleep(0.02)
            continue
        if time.monotonic() >= deadline:
            return msvcrt.kbhit()
        time.sleep(0.02)


def read_console(n=1, timeout=None):
    """Windows 控制台按键读取（msvcrt.getwch 家族）。

    为什么不用 os.read(fd)：CPython 在 Windows 上对控制台 stdin 的 os.read
    行为不可靠（常见坑：阻塞不返回/返回空/字符集错乱），控制台必须走
    msvcrt 的 console API。这里把 getwch 的结果重新编码回 utf-8 字节串，
    让 tui._raw_read 的解码路径（按首字节补 UTF-8 序列）原样可用。

    特殊键（方向键等）getwch 先返回 '\x00' 或 '\xe0'，紧跟着真正的键码；
    为了让 read_key 的 Esc 序列解码器继续工作，把这两前缀改写成
    '\x1b[' + 键码对应的字母，与 xterm 序列对齐：
      \x00/\xe0 + 'H' -> up    'P' -> down   'K' -> left   'M' -> right
    其余特殊键（Home/End/PgUp/PgDn/Del/F1-F12）同样映射成 xterm 序列。
    Enter 在 Windows 控制台是 '\r'，与 POSIX 一致；getwch 不经过行缓冲，
    不存在需要剥的 \n。
    """
    import msvcrt
    if not _kbhit_wait(timeout):
        return b""
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        # 特殊键：读第二个码并翻成 xterm 序列
        if not _kbhit_wait(0.01):
            return b""
        code = msvcrt.getwch()
        xterm = {
            "H": "A", "P": "B", "K": "D", "M": "C",   # ↑ ↓ ← →
            "G": "H", "O": "F",                        # Home End
            "I": "5~", "Q": "6~",                      # PgUp PgDn
            "S": "3~", "R": "2~",                      # Del Ins
            "s": "15~", "t": "17~", "u": "18~", "v": "19~",   # F1-F4
            "w": "20~", "x": "21~", "y": "23~", "z": "24~",   # F5-F8
            "{": "25~", "|": "26~",                    # F9 F10
        }.get(code)
        if xterm is None:
            return b""
        return ("\x1b[" + xterm).encode("utf-8")
    return ch.encode("utf-8")


def _pipe_readable_win32(fd, timeout):
    """PeekNamedPipe：匿名管道是否有立即可读数据。"""
    import ctypes
    avail = ctypes.c_uint32(0)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        ok = ctypes.windll.kernel32.PeekNamedPipe(
            ctypes.c_void_p(fd), None, 0, None, ctypes.byref(avail), None)
        if not ok:
            return True  # pipe 断了：让上层 os.read 触发 EOF
        if avail.value > 0:
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# 4. 进程树终止：killpg -> taskkill /T
# ---------------------------------------------------------------------------


EVENT_READ = 1  # selectors.EVENT_READ 的跨平台常量（PipeSelector 只支持读事件）


def set_blocking(fd, blocking):
    """os.set_blocking 的跨平台版。Windows 管道由 PipeSelector 轮询，无需（也无从）设置。"""
    if not IS_WINDOWS:
        os.set_blocking(_unwrap(fd), bool(blocking))
    # Windows: 非阻塞语义由 PeekNamedPipe 轮询承担；socket 由 selectors 的事件循环承担。


def popen_group_kwargs(new_group=True):
    """Popen 的进程组 kwargs：POSIX=start_new_session；Windows=CREATE_NEW_PROCESS_GROUP。

    new_group=False 时返回空 dict（原 start_new_session=False 语义：同进程组）。
    """
    if not new_group:
        return {}
    """subprocess.Popen 的「新进程组」跨平台 kwargs。

    POSIX: start_new_session=True（原语义）。
    Windows: CREATE_NEW_PROCESS_GROUP（让 taskkill /T 有组边界语义可依据；
    实际树终止靠 taskkill，这里主要保证 ctrl 事件不外泄到父控制台）。
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def signal_process_group(pid, sig):
    """os.killpg(pid, sig) 的跨平台版（Windows: taskkill /T）。

    组已消失时静默返回 False（对齐原 tasks._send_group / tools 终止链里
    对 ProcessLookupError 的吞并语义），调用方用返回值判断是否送达过。
    """
    if not IS_WINDOWS:
        try:
            os.killpg(pid, sig)
            return True
        except ProcessLookupError:
            return False
    import subprocess
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(int(pid))],
            capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def terminate_process_tree(pid, sig=None, *, grace=0.35):
    """按进程树终止（POSIX: killpg TERM + 宽限后 KILL；Windows: taskkill /T）。

    返回 True 表示至少发出过一次终止信号。语义对齐 tools._terminate_process_group。
    """
    if not IS_WINDOWS:
        # POSIX 侧：完整复刻原 _terminate_process_group 的 TERM -> 宽限 -> KILL 序列。
        try:
            os.killpg(pid, signal.SIGTERM)
            sent = True
        except (ProcessLookupError, PermissionError):
            return False
        deadline = time.monotonic() + max(0.0, float(grace))
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):
                return sent
            time.sleep(0.01)
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return sent
    import subprocess
    # /T 连子进程一起；/F 强杀（Windows 没有 TERM 的概念，
    # Ctrl+Break 送达依赖每个进程的 handler，不可靠 —— 直接强杀并诚实标注）
    try:
        result = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(int(pid))],
            capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def group_exists(pid):
    """POSIX killpg(pid,0) 存活探测；Windows 走 tasklist。"""
    if not IS_WINDOWS:
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # 同 uid 的 managed group 不应发生；保守视为仍存在。
            return True
    return wait_pid_exit(pid)


# ---------------------------------------------------------------------------
# 5. SIGWINCH：Windows 没有；调用方拿 None 表示「用轮询」
# ---------------------------------------------------------------------------

WINCH_SIGNAL = None if IS_WINDOWS else getattr(
    __import__("signal"), "SIGWINCH", None)
