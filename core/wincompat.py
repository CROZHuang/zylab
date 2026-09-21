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
import re
import signal
import stat
import subprocess
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"

# Windows 的 os.open 默认走**文本模式**（CRT 的 _O_TEXT）：读会把 CRLF 折成 LF，
# 写会把 LF 扩成 CRLF。凡是按字节算哈希、比长度、做原子替换的地方都必须显式
# 二进制，否则既会误判「文件变了」，也会真的改写用户文件的换行。
# POSIX 上没有这个标志，取 0 —— 所有 `| O_BINARY` 在那边都是恒等运算。
O_BINARY = getattr(os, "O_BINARY", 0)

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


def is_dir_box(f):
    """f 是否是 Windows 的「目录 fd」替身（见 _WinPathBox）。"""
    return isinstance(f, _WinPathBox)


def fstat(fd):
    # 目录盒子没有真 fd：Windows 打不开目录句柄，身份只能按路径取。
    # checkpoints 用 (st_dev, st_ino) 做防替换比对，os.stat 一样给得出。
    if isinstance(fd, _WinPathBox) and fd.fd < 0:
        return os.stat(fd.path)
    return os.fstat(_unwrap(fd))


def listdir(fd):
    """os.listdir(fd) 的跨平台版：目录盒子按路径列。"""
    if isinstance(fd, _WinPathBox):
        return os.listdir(fd.path)
    return os.listdir(fd)


def read(fd, n):
    return os.read(_unwrap(fd), n)


def write(fd, data):
    return os.write(_unwrap(fd), data)


def fsync(fd):
    if isinstance(fd, _WinPathBox) and fd.fd < 0:
        # 目录条目的 fsync 在 Windows 上没有对应 API（NTFS 元数据日志兜底，
        # 与 core/store.py、core/migrations.py 的同名处理一致）。
        # 仍然检查目录还在：静默成功会把「目录被换掉」也一起吞了。
        if not os.path.isdir(fd.path):
            raise NotADirectoryError(fd.path)
        return None
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
    how = fcntl.LOCK_UN if flags & LOCK_UN else (
        fcntl.LOCK_EX if flags & LOCK_EX else fcntl.LOCK_SH)
    if flags & LOCK_NB and not flags & LOCK_UN:
        how |= fcntl.LOCK_NB
    # 拿不到锁一律**抛 BlockingIOError**，不是返回 False。
    # Python 的 fcntl.flock 就是这个语义（返回 False 是 C 的约定）；zylab 的调用方
    # 全部靠 `except BlockingIOError` 判断「别人占着」——吞掉它等于每个调用方都以为
    # 自己拿到了锁。2026-09-21 在 Linux 上实测到后果：graft 的两个 builder 同时开建，
    # `test_two_terminals_share_one_locked_initial_build` 变红、worker 吐不出完整
    # envelope。Windows 上同样错，只是没有用例钉住。
    fcntl.flock(fd, how)
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
        # 与 _flock_posix 同一个契约：拿不到锁就抛 BlockingIOError，让调用方的
        # `except BlockingIOError` 真的能等到锁（见那边的说明）。
        if flags & LOCK_NB and exc.errno in (errno.EACCES, errno.EDEADLK, 13):
            raise BlockingIOError(
                errno.EWOULDBLOCK, "锁被别的进程占着") from exc
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

    __slots__ = ("path", "fd", "_keepalive")

    def __init__(self, path):
        self.path = str(path)
        self.fd = -1
        self._keepalive = None

    # checkpoints 到处是 `if fd >= 0: close(fd)` / `fd = -1` 这套「int fd 哨兵」
    # 写法。盒子代表一个**有效**的目录 fd，所以这些比较一律按非负数回答，
    # 否则 `_WinPathBox >= 0` 直接 TypeError。刻意不实现 __index__：
    # 那会让 os.close(盒子) 悄悄拿到一个假 fd。
    def _fd_number(self):
        return self.fd if self.fd >= 0 else 0

    def __ge__(self, other):
        return self._fd_number() >= other

    def __gt__(self, other):
        return self._fd_number() > other

    def __le__(self, other):
        return self._fd_number() <= other

    def __lt__(self, other):
        return self._fd_number() < other

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
    # 目标是目录时必须返回盒子：Windows 的 os.open 打不开目录（EACCES），
    # 而调用方要的 O_DIRECTORY 早在 getattr(os,"O_DIRECTORY",0) 那里被归零，
    # 这里看不到它。checkpoints 的 trash 路径就是一层层 openat 目录下去的。
    if os.path.isdir(full):
        return _WinPathBox(full)
    # **必须显式二进制。** Windows 的 os.open 默认是**文本模式**：os.read 会把
    # CRLF 悄悄折成 LF，读回来的字节数比 st_size 少，写出去的 \n 又会被扩成
    # \r\n。checkpoints 拿字节算 SHA、按 st_size 校验一致性，一旦经过这层翻译
    # 就是「文件在读取期间变了」和**内容被改写**。POSIX 上没有 O_BINARY，
    # getattr 取 0，行为不变。
    return os.open(full, int(flags) | O_BINARY, int(mode))


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


# Windows 上别的进程（杀毒实时扫描、Everything/搜索索引、编辑器）只要把文件
# 打开着且没带 FILE_SHARE_DELETE，rename/unlink 就直接失败——不是权限不够，是
# 这一瞬间不让动。表现为 winerror 32/33（sharing/lock violation）或 5，映射到
# errno 多半是 EACCES。POSIX 没有这个失败模式。
_SHARING_WINERRORS = frozenset({5, 32, 33})
_SHARING_ERRNOS = frozenset({errno.EACCES, errno.EBUSY, errno.EPERM})
SHARING_RETRIES = 6            # 10+20+40+80+160 ms ≈ 0.31 s 上限
SHARING_BACKOFF = 0.01


def _is_sharing_violation(exc):
    return (getattr(exc, "winerror", None) in _SHARING_WINERRORS
            or exc.errno in _SHARING_ERRNOS)


def retry_sharing(call, *, attempts=SHARING_RETRIES):
    """Windows 上对共享冲突做有界重试；POSIX 原样调用一次。

    刻意**有界**：真正的权限错误（目标在只读目录、被 ACL 拒）重试多少次都一样，
    拖长只会让模型盯着一个卡住的工具。次数用完抛原异常，调用方的错误处理不变。
    """
    if not IS_WINDOWS:
        return call()
    for attempt in range(attempts):
        try:
            return call()
        except OSError as exc:
            if attempt == attempts - 1 or not _is_sharing_violation(exc):
                raise
            time.sleep(SHARING_BACKOFF * (2 ** attempt))


def fd_unlink(dirfd, name):
    root, full = _join_dir(dirfd, name)
    if root is not None:
        return retry_sharing(lambda: os.unlink(full))
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
    return retry_sharing(lambda: os.replace(s_full, d_full))


# Win32 把这些名字当**设备**，不当文件名——`echo x > NUL` 写进黑洞，`CON` 读
# 控制台。带扩展名也一样（`aux.txt` 仍是 AUX）。尾随的点和空格则会被静默剥掉，
# 于是 `t_write_file("a.txt ")` 写出来的是 `a.txt`，下一轮按原名读就「不存在」。
# 两种都不会报错，只会让模型得到一个与事实不符的世界。
_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{d}" for d in range(1, 10)]
    + [f"LPT{d}" for d in range(1, 10)])
_BAD_NAME_CHARS = '<>:"|?*'


def reserved_name_problem(name):
    """这个**文件名**（单个路径分量）在 Windows 上会被吞掉吗？会就返回原因，否则 None。

    规则本身与当前平台无关（POSIX 上这些名字都合法），所以函数不判 IS_WINDOWS
    ——这样它在 Linux 上也测得了；要不要据此拒绝由调用方决定。
    """
    text = str(name)
    if not text:
        return None
    stem = text.split(".", 1)[0].upper()
    if stem in _DEVICE_NAMES:
        return (f"`{text}` 在 Windows 上是设备名（{stem}），不是文件："
                "写进去的内容会被丢弃，读出来的也不是文件内容。换个名字。")
    if text[-1] in ". ":
        return (f"`{text}` 以点或空格结尾，Windows 会在落盘时静默去掉它，"
                "于是按原名再读就成了「文件不存在」。去掉结尾的点/空格。")
    bad = sorted({c for c in text if c in _BAD_NAME_CHARS or ord(c) < 32})
    if bad:
        shown = " ".join(repr(c) for c in bad)
        return f"`{text}` 含 Windows 文件名不允许的字符：{shown}。"
    return None


def reserved_path_problem(path):
    """整条路径上有没有会被 Windows 吞掉的名字；有就返回原因，否则 None。

    逐段查，不只查文件名——`logs/aux/x.txt` 里的 `aux` 一样会让 mkdir 失败，
    而 Win32 给的错是「参数错误」，看不出是名字的问题。

    用 ntpath 而不是 os.path：这样规则在 Linux 上也跑得起来，门禁才测得到它。
    """
    import ntpath
    _, rest = ntpath.splitdrive(str(path))
    for part in re.split(r"[\\/]+", rest):
        problem = reserved_name_problem(part)
        if problem:
            return problem
    return None


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
    """st_uid == os.geteuid() 的跨平台安全检查。**Windows 上恒 True（fail-open）。**

    Windows 没有 euid/st_uid 语义（st_uid 恒为 0），这里拿不到任何等价保证，
    所以它在那边**不是一道检查**——「不可被其他用户改写」完全依赖状态目录
    所在的 NTFS ACL。别把它读成 fail-closed：真正还剩一点判别力的是调用方
    fd_owned_by_current_user，它在 Windows 上至少还查 is_regular。
    """
    if IS_WINDOWS:
        return True
    return info.st_uid == (os.geteuid() if uid is None else uid)


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
STILL_ACTIVE = 259


def _open_process_win32(pid):
    """OpenProcess(QUERY_LIMITED_INFORMATION)；返回 (handle, winerror)。

    QUERY_LIMITED_INFORMATION 是「只问不碰」的最小权限，同用户的普通进程
    一律可开；拿不到句柄时 winerror 区分「不存在」与「存在但不让看」。
    """
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.OpenProcess.restype = ctypes.c_void_p
    handle = k32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    return (k32, handle, 0 if handle else ctypes.get_last_error())


def pid_alive(pid, *, denied_is_alive=False):
    """pid 是否仍是一个活着的进程。

    `denied_is_alive` 对齐各调用点原本的 POSIX 语义：拿不到权限时，
    store/agents/workflows 判「死」（原代码把 PermissionError 一起吞进
    `except OSError: return False`），repomap/group_exists 判「活」。
    这个差异是既有行为，不在这次移植里统一 —— 改它要单独论证。

    **Windows 上绝不能用 `os.kill(pid, 0)`。** POSIX 的 signal 0 是「只查不发」，
    而 Windows 的 0 就是 `CTRL_C_EVENT`：CPython 会把它翻成
    `GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)` —— 对多数 pid 抛 WinError 87
    （于是活着的进程被判成已死），对共享控制台的进程组则**真的送出一次 Ctrl+C**
    （于是调用方自己被 SIGINT 打断）。2026-09-17 实测：会话 lease 的存活判定
    因此在启动时抛 SessionBusyError，zylab 画完界面就退出。

    Windows 走 OpenProcess + GetExitCodeProcess；不发送任何东西。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            # 同机异用户：进程确实存在，只是不许发信号。
            return bool(denied_is_alive)
        except OSError:
            return False
    import ctypes
    k32, handle, err = _open_process_win32(pid)
    if not handle:
        # 存在但受保护（系统/其他用户/提权进程）：与 POSIX 的
        # PermissionError 分支同一策略。
        return bool(denied_is_alive) and err == ERROR_ACCESS_DENIED
    try:
        k32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        code = ctypes.c_uint32()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def proc_start(pid):
    """进程启动标识：同号 pid 被复用时必然不同；拿不到返回 ""。

    POSIX 用 `/proc/<pid>/stat` 的第 22 个字段（starttime ticks），
    Windows 用 GetProcessTimes 的创建时间（FILETIME，100ns 精度）。
    两边都只是**不透明字符串**，调用方只做相等比较。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if not IS_WINDOWS:
        try:
            raw = open(f"/proc/{pid}/stat", encoding="ascii").read()
            return raw[raw.rfind(")") + 2:].split()[19]
        except (OSError, IndexError):
            return ""
    import ctypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    k32, handle, _err = _open_process_win32(pid)
    if not handle:
        return ""
    try:
        k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [
            ctypes.POINTER(_FILETIME)] * 4
        created, exited = _FILETIME(), _FILETIME()
        kernel, user = _FILETIME(), _FILETIME()
        if not k32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            return ""
        return f"{created.high:08x}{created.low:08x}"
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def wait_pid_exit(pid, timeout=1.0):
    """pid 存活探测（历史名；新代码用 pid_alive）。

    group_exists 的 POSIX 分支把 PermissionError 当「仍存在」，这里对齐。
    """
    return pid_alive(pid, denied_is_alive=True)


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


ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200


class _Win32Raw:
    """Windows console raw 模式（ctypes，无三方依赖）。

    对齐 POSIX setcbreak 语义：关行缓冲/回显/ECHOCTL，**保留**输出处理
    （ENABLE_PROCESSED_OUTPUT），这样 \\n 仍自动补 \\r，不出现阶梯屏。
    """

    def __init__(self, fd, cbreak=False, capture_signals=False):
        self.fd = fd
        self.cbreak = cbreak
        self.capture_signals = capture_signals
        self.saved = None
        self._kernel32 = None

    def _kernel(self):
        if self._kernel32 is None:
            import ctypes
            self._kernel32 = ctypes.windll.kernel32
        return self._kernel32

    def _handle(self):
        """fd -> 真 console HANDLE（见模块级 _fd_handle 的说明）。"""
        return _fd_handle(self.fd)

    def _get_mode(self):
        import ctypes
        mode = ctypes.c_uint32()
        if not self._kernel().GetConsoleMode(
                self._handle(), ctypes.byref(mode)):
            raise OSError("GetConsoleMode failed（fd 不是 console？）")
        return mode.value

    def _set_mode(self, value):
        import ctypes
        if not self._kernel().SetConsoleMode(
                self._handle(), ctypes.c_uint32(value)):
            raise OSError("SetConsoleMode failed")

    def __enter__(self):
        # 不是真控制台就**大声失败**，与 POSIX termios 对齐（管道上 tcgetattr 会抛）。
        # 不静默降级：降级出来的是行缓冲输入 —— 方向键、Ctrl+R、Shift+Tab 全失灵，
        # 界面却装作正常，比直接报错难查得多。
        # 调用方（core/tui.supported()）负责在进来之前就判掉这种终端。
        self.saved = self._get_mode()
        # ENABLE_PROCESSED_INPUT(1) 关掉（Ctrl+C 不再由控制台预处理），
        # ENABLE_LINE_INPUT(2) / ENABLE_ECHO_INPUT(4) 关掉 = raw 输入。
        mode = self.saved & ~0x7
        if self.cbreak:
            # cbreak：保留 PROCESSED_INPUT（Ctrl+C 仍生效），只关行缓冲和回显
            mode = (self.saved | 0x1) & ~0x6
        # **ENABLE_VIRTUAL_TERMINAL_INPUT**：让控制台把按键交成 VT 序列，
        # 于是 Windows 的输入字节流与 POSIX **完全一致**（`\x1b[A`、`\x1b[Z`、
        # `\x1b[200~`…），core/tui.py 的 KEYS 表和 Esc 序列解码器原样可用，
        # 连括号粘贴、SGR 鼠标、焦点事件都跟着一起对。
        #
        # 不开的话最要命的一条：ConPTY（Windows Terminal 走的就是它）把
        # `\x1b[Z` 翻成 INPUT_RECORD 时**丢掉 Shift**，getwch() 拿到的是普通
        # `\t` —— Shift+Tab 与 Tab 不可区分，权限模式循环（SPEC-CC-parity B1）
        # 在 Windows Terminal 里根本没有入口。2026-09-17 实测确认。
        #
        # 设不上就退回 `\x00`/`\xe0` 扫描码路径（read_console 两条都认）。
        try:
            self._set_mode(mode | ENABLE_VIRTUAL_TERMINAL_INPUT)
        except OSError:
            self._set_mode(mode)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            try:
                self._set_mode(self.saved)
            except OSError:
                pass
        return False


ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def enable_vt_output(stream=None):
    """让 Windows 控制台按 ANSI/VT 解释转义序列。返回是否已启用。

    Windows Terminal / VS Code 的终端本来就开着（它们经 ConPTY 托管），
    但**经典 conhost 窗口默认是关的** —— 在那里 zylab 的整屏 UI 会变成满屏
    可见的 `←[0m`、`←[2J`。这是 per-console 的开关，设一次即可，不必还原。
    POSIX 上恒为 True（终端本来就认 VT），不做任何事。
    """
    if not IS_WINDOWS:
        return True
    import ctypes
    import msvcrt
    stream = sys.stdout if stream is None else stream
    try:
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
    except (OSError, ValueError, AttributeError):
        return False
    k32 = ctypes.windll.kernel32
    mode = ctypes.c_uint32()
    if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False        # 不是控制台（重定向到文件/管道）：无所谓 VT
    if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
        return True
    return bool(k32.SetConsoleMode(
        handle,
        ctypes.c_uint32(mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)))


def configure_stdio():
    """把重定向出去的 stdout/stderr 改成 UTF-8，避免 UnicodeEncodeError 崩溃。

    Windows 上写**管道/文件**时，Python 用的是本地代码页（这台机器是 cp936）。
    zylab 的输出里满是 `✓ ✗ ⏺ │ ─`，GBK 编不出来 —— `zylab -p ... | tee`
    或在 Git Bash 里跑就会直接抛 UnicodeEncodeError 而不是把话说完。
    写**控制台**时 Python 走 WriteConsoleW（Unicode 正确），不碰。
    POSIX 不做任何事。
    """
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or stream.isatty():
                continue
            if (stream.encoding or "").lower().replace("-", "") == "utf8":
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


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
    # 判据只能是「真的是 Win32 控制台」。早先这里写的是 `fd == 0 or _is_console(fd)`，
    # 于是在 Git Bash / MSYS mintty 下（stdin 是 fd 0，但那是 MSYS 的 pty 管道、
    # 不是控制台）会去轮询一个根本不存在的控制台键盘缓冲，输入永远不到达。
    if _is_console(fd):
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
        # fileobj -> SelectorKey。**必须存 key 而不是 data**：调用方
        # （core/tasks.py）会遍历 `get_map().values()` 取 `key.fileobj`，
        # 存 data 的话那里拿到的是一个裸字符串。
        self._pipes = {}
        self._closed = False

    def register(self, fileobj, events, data=None):
        if events != selectors.EVENT_READ:
            raise ValueError("PipeSelector 只支持读事件")
        # SelectorKey 是 4 元 namedtuple(fileobj, fd, events, data)；
        # 少给一个 fd 会在「管道刚可读」的那一刻抛 TypeError —— 也就是
        # 后台 bash 第一次吐字节的时候（2026-09-17 实测）。
        key = _SelectorKey(fileobj, fileobj.fileno(), events, data)
        self._pipes[fileobj] = key
        return key

    def unregister(self, fileobj):
        return self._pipes.pop(fileobj, None)

    def get_map(self):
        return {} if self._closed else dict(self._pipes)

    def __len__(self):
        return len(self._pipes)

    def select(self, timeout=None):
        deadline = (None if timeout is None
                    else time.monotonic() + max(0.0, float(timeout)))
        while True:
            ready = []
            for key in list(self._pipes.values()):
                try:
                    readable = _pipe_readable_win32(key.fileobj.fileno(), 0)
                except (OSError, ValueError):
                    # fd 已经被关掉：当作可读，让上层 os.read 去收 EOF，
                    # 与 select() 对已关闭端的行为一致。
                    readable = True
                if readable:
                    ready.append((key, selectors.EVENT_READ))
            if ready:
                return ready
            if deadline is not None and time.monotonic() >= deadline:
                return []
            time.sleep(0.02)

    def close(self):
        self._closed = True
        self._pipes.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


import selectors as _selectors_mod

_SelectorKey = _selectors_mod.SelectorKey
selectors = _selectors_mod
DefaultSelector = _selectors_mod.DefaultSelector

if IS_WINDOWS:
    DefaultSelector = PipeSelector


def wait_pipe_readable(fd, timeout):
    """wait_fd 的管道特化别名：tools/tasks 的 stdout drain 用它。"""
    return wait_fd(fd, timeout)


def _fd_handle(fd):
    """int fd -> kernel HANDLE（msvcrt.get_osfhandle）。Windows console/pipe
    API 全部收内核句柄；直接传 CRT fd 必失败。"""
    import ctypes
    import msvcrt
    return ctypes.c_void_p(msvcrt.get_osfhandle(fd))


def is_console(fd):
    """fd 是不是一个 Win32 控制台。

    与 `isatty()` **不是一回事**：Git Bash / MSYS mintty 的 pty 让 isatty 为真，
    但它不是控制台 —— 控制台 API（GetConsoleMode / msvcrt 读键）在那里全不适用。
    POSIX 上恒为 False：那里没有「控制台」这个概念，调用点都在 IS_WINDOWS 分支内。
    """
    if not IS_WINDOWS:
        return False
    return _is_console(fd)


def _is_console(fd):
    import ctypes
    try:
        return bool(ctypes.windll.kernel32.GetConsoleMode(
            _fd_handle(fd), ctypes.byref(ctypes.c_uint32())))
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


STD_INPUT_HANDLE = 0xFFFFFFF6          # (DWORD)-10
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF
INFINITE_WAIT = 0xFFFFFFFF


KEY_EVENT = 0x0001


def _drain_one_non_key_record(k32, handle):
    """吞掉队首那条**非按键**输入记录（鼠标移动 / 焦点 / 窗口尺寸）。

    这些记录同样会把控制台输入句柄置信号，而 `msvcrt.kbhit()` 对它们返回
    False —— 不取走的话 WaitForSingleObject 会立刻再醒，变成 100% CPU 空转。

    **必须先 Peek 再 Read。** 直接 Read 会撞上一个竞态：`kbhit()` 说「现在没有
    按键」之后、Read 之前，用户刚好按下一个键 —— 那一下就被吞掉了。
    实测踩到过：打 20 个字符丢掉第一个，还伴随一次 3 秒卡顿。
    队列是 FIFO 且只有我们在消费，所以「队首是非按键」这个判断到 Read 时仍然成立。
    """
    import ctypes
    buffer = (ctypes.c_byte * 32)()
    count = ctypes.c_uint32()
    for name in ("PeekConsoleInputW", "ReadConsoleInputW"):
        getattr(k32, name).argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32)]
    if not k32.PeekConsoleInputW(
            ctypes.c_void_p(handle), buffer, 1, ctypes.byref(count)):
        return
    if not count.value:
        return
    if ctypes.c_uint16.from_buffer(buffer).value == KEY_EVENT:
        return          # 是按键：留给 getwch，绝不吞
    k32.ReadConsoleInputW(
        ctypes.c_void_p(handle), buffer, 1, ctypes.byref(count))


def _stdin_console_handle():
    """控制台**输入**句柄。它是可等待对象：有输入记录时被置信号。"""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.restype = ctypes.c_void_p
    return k32, k32.GetStdHandle(STD_INPUT_HANDLE)


def _kbhit_wait(timeout):
    """等到有按键可读。

    **不要轮询。** 原实现是 `while: kbhit(); sleep(0.02)` —— 每个按键平均要等
    半个轮询周期才被发现，实测「按键 → 回显」中位数 **31ms**，快速打字时
    明显发涩（POSIX 那边 select() 是立刻醒的，亚毫秒级）。

    控制台输入句柄本身就是可等待对象，WaitForSingleObject 一有输入就醒。
    唯一要注意的是：鼠标移动、焦点变化、窗口缩放也会把它置信号，所以醒来后
    仍要用 kbhit() 确认是不是**按键**，不是就接着等剩余时间。
    """
    import ctypes
    import msvcrt
    if msvcrt.kbhit():
        return True
    try:
        k32, handle = _stdin_console_handle()
    except OSError:
        handle = None
    if not handle or handle == INVALID_HANDLE_VALUE:
        # 拿不到可等待句柄时退回轮询，行为与旧实现一致。
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return msvcrt.kbhit()
            time.sleep(0.02)
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if deadline is None:
            wait_ms = INFINITE_WAIT
        else:
            left = deadline - time.monotonic()
            if left <= 0:
                return msvcrt.kbhit()
            wait_ms = max(0, int(left * 1000))
        k32.WaitForSingleObject(ctypes.c_void_p(handle), wait_ms)
        if msvcrt.kbhit():
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return msvcrt.kbhit()
        # 醒来但不是按键（鼠标/焦点/尺寸事件）：msvcrt 不消费它们，
        # 直接再等会立刻空转。丢弃一条非按键记录再继续。
        _drain_one_non_key_record(k32, handle)


# '\xe0' 前缀：导航键组。值是 core/tui.py KEYS 表认得的 xterm 序列。
_WIN_NAV_KEYS = {
    "H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C",   # ↑ ↓ ← →
    "G": "\x1b[H", "O": "\x1b[F",                                  # Home End
    "I": "\x1b[5~", "Q": "\x1b[6~",                                # PgUp PgDn
    "R": "\x1b[2~", "S": "\x1b[3~",                                # Ins Del
    # Ctrl+方向/Home/End：xterm 的 modifier=5 形态。tui 目前不绑定它们，
    # 但必须发出**正确**的序列，而不是冒充 F 键去触发别的动作。
    "s": "\x1b[1;5D", "t": "\x1b[1;5C",                            # Ctrl+← →
    "\x8d": "\x1b[1;5A", "\x91": "\x1b[1;5B",                      # Ctrl+↑ ↓
    "w": "\x1b[1;5H", "u": "\x1b[1;5F",                            # Ctrl+Home End
}

# '\x00' 前缀：功能键组。
_WIN_FN_KEYS = {
    "\x0f": "\x1b[Z",                                              # Shift+Tab
    ";": "\x1bOP", "<": "\x1bOQ", "=": "\x1bOR", ">": "\x1bOS",    # F1–F4
    "?": "\x1b[15~", "@": "\x1b[17~", "A": "\x1b[18~",              # F5–F7
    "B": "\x1b[19~", "C": "\x1b[20~", "D": "\x1b[21~",              # F8–F10
    "\x85": "\x1b[23~", "\x86": "\x1b[24~",                        # F11 F12
}


def read_console(n=1, timeout=None):
    """Windows 控制台按键读取（msvcrt.getwch 家族）。

    为什么不用 os.read(fd)：CPython 在 Windows 上对控制台 stdin 的 os.read
    行为不可靠（常见坑：阻塞不返回/返回空/字符集错乱），控制台必须走
    msvcrt 的 console API。这里把 getwch 的结果重新编码回 utf-8 字节串，
    让 tui._raw_read 的解码路径（按首字节补 UTF-8 序列）原样可用。

    正常情况下 raw_mode 已经开了 ENABLE_VIRTUAL_TERMINAL_INPUT，按键直接以
    VT 序列到达（`\x1b[A`、`\x1b[Z`…），与 POSIX 字节流一模一样，下面的扫描码
    翻译根本不会被用到。**它是退路**：开不了 VT 输入的控制台上，特殊键仍然走
    '\x00' / '\xe0' + 扫描码。两条路并存不冲突 —— 开了 VT 就见不到这两个前缀。

    特殊键（方向键等）getwch 先返回 '\x00' 或 '\xe0'，紧跟着真正的键码；
    为了让 read_key 的 Esc 序列解码器继续工作，这里把它们翻成等价的 xterm
    序列（core/tui.py 的 KEYS 表认的就是这些）。

    **两个前缀是两张表，不能合并。** '\xe0' 是导航键组，'\x00' 是功能键组，
    同一个码在两组里含义不同（例如 \x86：\x00 下是 F12，\xe0 下是 Ctrl+PgUp）。
    早先的实现只有一张表，还把 's'/'t'/'u'/'v'/'w' 当成 F1–F5 —— 它们其实是
    Ctrl+← / Ctrl+→ / Ctrl+End / Ctrl+PgDn / Ctrl+Home，于是按一次 Ctrl+←
    会当成 F5 发出去。同一张表里 Shift+Tab（'\x00' + '\x0f'）根本没有条目，
    被整个吞掉 —— 那是 SPEC-CC-parity B1 的权限模式循环键。

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
        sequence = (_WIN_NAV_KEYS if ch == "\xe0" else _WIN_FN_KEYS).get(code)
        if sequence is None:
            return b""
        return sequence.encode("utf-8")
    if "\ud800" <= ch <= "\udbff":
        # getwch 交的是 UTF-16 码元：BMP 外的字符（emoji）分两次到达。
        # 只拿到半个代理对就 encode("utf-8") 会直接 UnicodeEncodeError，
        # 于是粘一个 emoji 就能把输入线程打死。
        if _kbhit_wait(0.05):
            low = msvcrt.getwch()
            if "\udc00" <= low <= "\udfff":
                return (ch + low).encode("utf-16", "surrogatepass").decode(
                    "utf-16").encode("utf-8")
            return low.encode("utf-8", "replace")
        return b""
    return ch.encode("utf-8", "replace")


def _pipe_readable_win32(fd, timeout):
    """PeekNamedPipe：匿名管道是否有立即可读数据。"""
    import ctypes
    avail = ctypes.c_uint32(0)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        ok = ctypes.windll.kernel32.PeekNamedPipe(
            _fd_handle(fd), None, 0, None, ctypes.byref(avail), None)
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

# 终止信号的跨平台名。Windows 的 signal 模块**没有 SIGKILL**（只有 SIGTERM /
# SIGBREAK / SIGABRT…），而 tasks.py 既拿它当「真要送的信号」，也拿它当
# TERM→KILL 升级的**阶段标记**（self._stop_stage）。所以 Windows 上给一个
# 与 SIGTERM 不同的哨兵值：数值取 POSIX 的 9，语义由 signal_process_group /
# terminate_process_tree 解释（那里一律 taskkill /T /F）。
SIGTERM = signal.SIGTERM
SIGKILL = getattr(signal, "SIGKILL", 9)


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


# ---- Job Object：Windows 上「进程组」的真正等价物 -------------------------
# POSIX 靠 start_new_session + killpg 一次收掉整棵树。Windows 没有进程组信号，
# 而 `taskkill /T` 是按**调用那一刻**的父子关系枚举的：中间进程一死，孙子就
# 成了孤儿、枚举不到。实测（2026-09-17）：Git Bash 里
# `bash -c 'trap "" TERM; sleep 30 & wait'`，杀掉 leader 之后那个 sleep 仍然
# 活着并**握着 stdout 管道 28 秒** —— 于是 /cancel 要等 30 秒才返回。
# Job Object 没有这个洞：进程一旦入 job，它此后派生的所有后代都自动在 job 里，
# TerminateJobObject 一次性全收。
_JOBS = {}                      # pid -> job handle
_JOBS_LOCK = threading.Lock()

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9


def _job_structs():
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return EXTENDED_LIMIT


def attach_to_job(proc):
    """把刚起的进程放进一个 Job Object，之后 terminate_process_tree 一次收干净。

    在 Popen 之后立刻调用。POSIX 上是 no-op（那里 start_new_session 已经给了
    进程组）。失败不抛：拿不到 job 就退回 taskkill，功能降级但不影响主流程。
    """
    if not IS_WINDOWS or proc is None:
        return False
    handle = getattr(proc, "_handle", None)
    if handle is None:
        return False
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return False
    try:
        extended = _job_structs()()
        extended.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        if not k32.SetInformationJobObject(
                ctypes.c_void_p(job), JobObjectExtendedLimitInformation,
                ctypes.byref(extended), ctypes.sizeof(extended)):
            raise OSError("SetInformationJobObject failed")
        k32.AssignProcessToJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p]
        if not k32.AssignProcessToJobObject(
                ctypes.c_void_p(job), ctypes.c_void_p(int(handle))):
            raise OSError("AssignProcessToJobObject failed")
    except OSError:
        k32.CloseHandle(ctypes.c_void_p(job))
        return False
    with _JOBS_LOCK:
        _JOBS[int(proc.pid)] = job
    return True


def _terminate_job(pid):
    """有 job 就整组终止；返回是否处理过。"""
    with _JOBS_LOCK:
        job = _JOBS.pop(int(pid), None)
    if job is None:
        return False
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.TerminateJobObject(ctypes.c_void_p(job), 1)
    k32.CloseHandle(ctypes.c_void_p(job))
    return True


def release_job(pid):
    """进程正常结束后回收 job 句柄（KILL_ON_JOB_CLOSE：关掉即收尾）。"""
    if not IS_WINDOWS:
        return
    with _JOBS_LOCK:
        job = _JOBS.pop(int(pid), None)
    if job is not None:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(job))


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
    if _terminate_job(pid):
        return True
    try:
        result = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(int(pid))],
            capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # taskkill: 0=已送达；128（或 ERROR_NOT_FOUND 文案）=进程已不在，
    # 等价于 POSIX 的 ProcessLookupError → False。调用方靠这个返回值判断
    # 「是否真的送达过」，漏掉 return 会让整条终止链读到 None。
    return result.returncode == 0


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
    # 有 job 就整组收（能连孤儿孙子一起收，taskkill /T 做不到）。
    if _terminate_job(pid):
        return True
    # 退路：/T 连子进程一起；/F 强杀（Windows 没有 TERM 的概念，
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
