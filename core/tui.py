"""raw-mode 终端交互层 —— 用标准库 termios/tty 手写。

为什么不用 prompt_toolkit：它装在 /usr/local/lib（临时层），pod 重建即丢；
zylab 的立身之本是开箱即用、零依赖。而我们只需要几件事，标准库足够：

  1. 输入 `/` 立刻弹出命令面板，上下键选，Tab/Enter 补全
  2. 已选中 `/cmd` 后输入空格，会显示带说明的二级指令列表；有参数的指令继续显示三级参数
  3. `/model` 这类长列表用上下键挑，带增量筛选
  4. 流式输出期间按 Esc 能中断

这些交互在 cooked mode（`input()`）下**物理上做不到** —— 那时按键停在内核行
缓冲里，程序要等回车才拿得到；方向键是转义序列，readline 自己消费掉了；而流式
期间程序根本没在读 stdin，所以 Esc 无人接收（Ctrl-C 能用是因为它是 tty 驱动发的
**信号**，不是按键）。raw mode 把这三条限制一次性解除。
"""
import select as _select
from . import paths
import base64
from dataclasses import dataclass, replace as dataclass_replace

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from . import goals
from . import wincompat

# ---- 转义序列 → 键名。终端把方向键等发成多字节序列，必须解码后才能用。
KEYS = {
    "\x1b[A": "up", "\x1bOA": "up",
    "\x1b[B": "down", "\x1bOB": "down",
    "\x1b[C": "right", "\x1bOC": "right",
    "\x1b[D": "left", "\x1bOD": "left",
    "\x1b[H": "home", "\x1bOH": "home", "\x1b[1~": "home",
    "\x1b[F": "end", "\x1bOF": "end", "\x1b[4~": "end",
    "\x1b[5~": "pageup", "\x1b[6~": "pagedown",
    "\x1b[3~": "delete",
    "\r": "enter", "\n": "enter",
    "\x7f": "backspace", "\x08": "backspace",
    "\t": "tab", "\x1b[Z": "shift-tab",
    "\x02": "ctrl-b", "\x03": "ctrl-c", "\x04": "ctrl-d",
    "\x01": "ctrl-a", "\x05": "end",      # Ctrl-A / Ctrl-E
    "\x14": "ctrl-t",
    "\x15": "ctrl-u", "\x17": "ctrl-w", "\x0b": "ctrl-k",
    "\x0f": "ctrl-o",                    # 展开最近一段折叠输出（SPEC-CC-parity C2）
    "\x12": "ctrl-r",                    # 反向增量搜索历史（SPEC-CC-parity A6）
    "\x1b": "esc",
    # DEC 1004 焦点上报：CSI I = 获得焦点，CSI O = 失去焦点。注意这是 **CSI** 的
    # I/O 终止字节，不是上面 SS3 那组 `ESC O x`——前缀不同，互不冲突。
    "\x1b[I": "focus-in", "\x1b[O": "focus-out",
}

CSI = "\x1b["

# DEC 私有模式 2004（bracketed paste）。终端不认识就静默忽略，没有副作用。
PASTE_ON = "\x1b[?2004h"
PASTE_OFF = "\x1b[?2004l"
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"

# DEC 私有模式 1004（焦点上报）。away recap 靠它判断「人离开了」：失焦后计时，
# 重新聚焦即取消（照搬 Claude Code 的 awaySummary）。终端不认识就静默忽略——
# 那样永远收不到失焦事件，自动 recap 也就永不触发，/recap 仍可用。
FOCUS_ON = "\x1b[?1004h"
FOCUS_OFF = "\x1b[?1004l"


def _set_focus_reporting(enable):
    """开/关 DEC 1004。失败不算错误 —— 终端不支持就当没这回事。"""
    try:
        sys.stdout.write(FOCUS_ON if enable else FOCUS_OFF)
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
# 上限存在的意义是防呆不是防坏：粘贴超过这个量几乎肯定是误操作（比如把整个
# 文件拖进终端），继续读只会让界面卡住。超限就截断并在 PasteEvent 上标记。
PASTE_MAX_CHARS = 262144


@dataclass(frozen=True)
class MouseEvent:
    """Decoded xterm SGR mouse event; coordinates are one-based cells."""

    kind: str
    x: int
    y: int
    button: int = 0
    modifiers: int = 0


@dataclass(frozen=True, order=True)
class SelectionPoint:
    """A zero-based display-cell position in the history viewport."""

    row: int
    column: int


@dataclass(frozen=True)
class ComposerMouseResult:
    """Result of routing one mouse event to the live composer."""

    handled: bool
    # A non-None cursor is returned only for a click (a zero-length drag).
    # Dragging to select text must not move the editor cursor underneath the
    # user's draft until the selection is explicitly cleared.
    cursor: int | None = None


# xterm mouse protocol modifier bit.  A terminal normally consumes
# Shift+drag for its own text selection even while an application has mouse
# reporting enabled; ignore it too when a terminal forwards the event.
_MOUSE_SHIFT = 4


@dataclass(frozen=True)
class CursorPosition:
    """xterm DSR cursor reply; coordinates are one-based cells."""

    x: int
    y: int


@dataclass(frozen=True)
class PasteEvent:
    """一次 bracketed paste 的载荷。

    没有它的话，粘贴和"手速极快地打字"在终端里是**同一件事**：每个换行都会
    被 KEYS 映射成 enter，于是粘 10 行 = 提交 10 条 prompt。开启 DEC 私有模式
    2004 后终端会把粘贴内容包在 ESC[200~ / ESC[201~ 里，这个类就是拆包结果。

    text 永远是**字面文本**：控制字符与 ESC 序列已在 _sanitize_paste 里剥掉，
    所以粘贴内容不可能再被解释成按键。
    """

    text: str
    truncated: bool = False


@dataclass(frozen=True)
class DockItem:
    """One composer-adjacent status row with an optional click action."""

    text: str
    event: str | None = None
    value: object = None


def _decode_cursor_position(sequence):
    match = re.fullmatch(r"\x1b\[(\d{1,5});(\d{1,5})R", str(sequence))
    if match is None:
        return None
    y, x = map(int, match.group(1, 2))
    if x < 1 or y < 1:
        return None
    return CursorPosition(x=x, y=y)


def _decode_sgr_mouse(sequence):
    match = re.fullmatch(
        r"\x1b\[<(\d{1,4});(\d{1,5});(\d{1,5})([Mm])",
        str(sequence))
    if match is None:
        return None
    code, x, y = map(int, match.group(1, 2, 3))
    if x < 1 or y < 1:
        return None
    button = code & 0b11
    modifiers = code & 0b11100
    if code & 0b1000000:
        if button == 0:
            kind = "scroll_up"
        elif button == 1:
            kind = "scroll_down"
        else:
            return None
    elif match.group(4) == "m":
        kind = "release"
    elif code & 0b100000:
        # DECSET 1002 (button-event tracking) adds the motion bit.  SGR keeps
        # the button number in the low bits; the renderer only needs to know
        # that this is a drag update.
        kind = "motion"
    else:
        kind = "press"
    return MouseEvent(
        kind=kind, x=x, y=y,
        button=button, modifiers=modifiers)


_CTRL_KEY_CODES = {
    97: "ctrl-a",
    98: "ctrl-b",
    99: "ctrl-c",
    100: "ctrl-d",
    101: "end",
    107: "ctrl-k",
    116: "ctrl-t",
    117: "ctrl-u",
    119: "ctrl-w",
}


def _modified_key_name(codepoint, modifiers=1, event_type=1):
    """Translate Kitty/CSI-u or xterm modifier fields into editor keys."""
    try:
        codepoint = int(codepoint)
        modifiers = int(modifiers or 1)
        event_type = int(event_type or 1)
    except (TypeError, ValueError):
        return None
    if event_type == 3:              # key release, when a terminal reports it
        return "ignore"
    flags = max(0, modifiers - 1)
    shift = bool(flags & 0b1)
    alt = bool(flags & 0b10)
    control = bool(flags & 0b100)
    if codepoint == 13:
        # ESC+Enter is the conventional VS Code/iTerm fallback for a
        # modified Enter, so Alt+Enter is intentionally a newline too.
        return "shift-enter" if shift or alt else "enter"
    if codepoint == 9:
        return "shift-tab" if shift else "tab"
    if codepoint in (8, 127):
        return "backspace"
    if codepoint == 27:
        return "esc"
    if control:
        return _CTRL_KEY_CODES.get(codepoint)
    return None


def _decode_modified_key(sequence):
    """Decode the modified keys emitted by common modern terminals.

    Supported forms cover Kitty/CSI-u, xterm ``modifyOtherKeys`` and the
    ESC+CR/LF sequence commonly configured for Shift+Enter in VS Code.
    """
    sequence = str(sequence)
    if sequence in {"\x1b\r", "\x1b\n"}:
        return "shift-enter"
    match = re.fullmatch(
        r"\x1b\[(\d+)(?::[0-9:]*)?"
        r"(?:;(\d+)(?::(\d+))?)?(?:;[0-9:]*)?u",
        sequence)
    if match is not None:
        return _modified_key_name(*match.groups())
    match = re.fullmatch(r"\x1b\[27;(\d+);(\d+)~", sequence)
    if match is not None:
        modifiers, codepoint = match.groups()
        return _modified_key_name(codepoint, modifiers)
    match = re.fullmatch(r"\x1b\[(\d+);(\d+)~", sequence)
    if match is not None:
        codepoint, modifiers = match.groups()
        return _modified_key_name(codepoint, modifiers)
    return None


def _cols(default=100):
    """终端宽度。拿不到就给个保守值。

    **0 也算「拿不到」。** `shutil.get_terminal_size()` 在 Python 3.11 之前不做
    这个判断：一个没设过 `TIOCSWINSZ` 的 pty 上它原样返回 0，于是原来的
    `max(20, 0)` 给出 **20 列** —— 界面被压成一条窄缝，每一行都截成「…」。
    3.11 起标准库自己回落到 (80, 24)，所以这个 bug 只在 3.10 上看得见；
    2026-09-21 由公开仓库 CI 的 py3.10 那一列抓到（一条折叠行断在
    `exit 0 · 200 lines · 0.…`，后面的 `/expand` 提示整个没了）。
    """
    try:
        columns = shutil.get_terminal_size().columns
    except OSError:
        return default
    return max(20, columns or default)


def _lines(default=24):
    """Terminal height used to keep modal frames inside the visible viewport.

    同 `_cols`：0 表示查不到，不是「高度为 0」。
    """
    try:
        rows = shutil.get_terminal_size().lines
    except OSError:
        return default
    return max(5, rows or default)


def supported():
    """只有真 tty 才能进 raw mode；管道/重定向下回退到 input()。

    Windows 还要多问一句「是不是 Win32 控制台」。Git Bash / MSYS mintty 的
    pty 会让 `isatty()` 为真，但它**不是控制台**：`GetConsoleMode` 失败，
    Python 也没有 termios 可用，raw 模式根本无从设起。只认 isatty 的话，
    zylab 会一头扎进全屏 TUI 然后在 raw_mode 里崩掉。
    判否之后走的是既有的 input() 回退路径（功能可用，只是没有整屏 UI）。
    """
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    if not wincompat.IS_WINDOWS:
        return True
    try:
        return wincompat.is_console(sys.stdin.fileno())
    except (AttributeError, ValueError, OSError):
        return False


class raw_mode:
    """进出 raw / cbreak 模式的上下文管理器。

    **必须**在 finally 里还原，否则终端会废掉。

    `cbreak=True` 是关键区别：`tty.setraw` 会一并关掉 **OPOST/ONLCR**，
    于是输出的 `\n` 不再自动补 `\r`，光标只下移不回行首 —— 模型每流式吐一行
    就右移一格，屏幕变成阶梯状（「每行很多空格」）。`setcbreak` 只关行缓冲和
    回显，保留输出后处理，所以**凡是期间还要正常打印的场合都必须用 cbreak**。
    只有自己完全掌控每一次写入（输入行、选择器）的地方才用 raw。
    """

    def __init__(self, stream=None, cbreak=False, capture_signals=False):
        self.fd = (stream or sys.stdin).fileno()
        self.saved = None
        self.cbreak = cbreak
        self.capture_signals = capture_signals

    def __enter__(self):
        self._ctx = wincompat.raw_mode(
            self.fd, cbreak=self.cbreak,
            capture_signals=self.capture_signals)
        self._ctx.__enter__()
        self._set_bracketed_paste(True)
        return self

    def __exit__(self, *a):
        # 先关粘贴模式再还原终端模式：顺序反了的话写入会经过已恢复的行规程，
        # 可能被回显出来。
        self._set_bracketed_paste(False)
        ctx = getattr(self, "_ctx", None)
        if ctx is not None:
            ctx.__exit__(*a)
            self._ctx = None

    # raw_mode 在本文件里有四个使用点（InputPump、Terminal、以及 read_line 内
    # 的两处），它们**会嵌套**。若按进出各发一次开关，内层退出就会把外层还需要
    # 的粘贴模式关掉 —— 症状是"用了选择器之后粘贴又坏了"，极难复现。
    # 用引用计数：只在 0→1 时开、1→0 时关。
    _paste_depth = 0

    @classmethod
    def _set_bracketed_paste(cls, enable):
        """开/关 DEC 2004。失败不算错误 —— 终端不支持就当没这回事。"""
        if enable:
            cls._paste_depth += 1
            if cls._paste_depth != 1:
                return
        else:
            if cls._paste_depth == 0:
                return
            cls._paste_depth -= 1
            if cls._paste_depth != 0:
                return
        try:
            sys.stdout.write(PASTE_ON if enable else PASTE_OFF)
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            pass


def _utf8_len(first_byte):
    """按 UTF-8 首字节推断这个字符总共几字节。"""
    b = first_byte
    if b < 0x80:
        return 1
    if b >> 5 == 0b110:
        return 2
    if b >> 4 == 0b1110:
        return 3
    if b >> 3 == 0b11110:
        return 4
    return 1                      # 非法首字节，当单字节处理


def _raw_read(n=1, timeout=None, stream=None):
    """直接读文件描述符，**不经过 sys.stdin 的用户态缓冲**。

    这是个必须踩过才知道的坑：`sys.stdin.read(1)` 底下是 TextIOWrapper，
    它可能一次把 `\x1b[B` 三个字节全读进用户态缓冲，只交出第一个。此时
    `select()` 查的是内核 fd，会报「没数据」—— 于是方向键被误判成裸 Esc。
    症状很迷惑：单字符键、Enter、筛选全正常，唯独方向键不灵。
    用 os.read 直取 fd 就没有这层缓冲。
    """
    source = stream or sys.stdin
    fd = source.fileno()
    if wincompat.IS_WINDOWS and wincompat.is_console(fd):
        # Windows **控制台**不能 os.read（见 wincompat.read_console 文档）。
        # 但只有真控制台才走这条路：Git Bash / MSYS 的 pty 和被重定向的管道
        # 仍然是普通 fd，必须走下面的 os.read，否则一个字节都读不到。
        raw = wincompat.read_console(n, timeout)
        if not raw:
            return ""
        return raw.decode("utf-8", "replace")
    if timeout is not None:
        if not wincompat.wait_fd(fd, timeout):
            return ""
    try:
        raw = os.read(fd, n)
    except OSError:
        return ""
    if not raw:
        return ""
    # 中文一个字 3 字节。只读 1 字节就 decode 会得到 U+FFFD（显示成 ? 或 �）——
    # 这是「新版不能输入中文」的根因。按首字节补齐整个序列再解码。
    need = _utf8_len(raw[0]) - len(raw)
    while need > 0:
        more = _blocking_read(need, stream=source)
        if not more:
            break
        raw += more
        need = _utf8_len(raw[0]) - len(raw)
    return raw.decode("utf-8", "replace")


def _blocking_read(n, timeout=0.05, stream=None):
    """补读多字节字符的后续字节。同一个按键的字节几乎总是一起到达。"""
    if wincompat.IS_WINDOWS:
        return wincompat.read_console(n, timeout)   # bytes
    fd = (stream or sys.stdin).fileno()
    if not wincompat.wait_fd(fd, timeout):
        return b""
    try:
        return os.read(fd, n)
    except OSError:
        return b""


def read_key(timeout=None, stream=None):
    """读一个按键，返回键名或字符。

    Esc 的歧义：它既是「Esc 键」，又是方向键等序列的引导字节。区分办法是读到
    \x1b 后**短暂等待**（这里 30ms）—— 有后续字节就是序列，没有就是裸 Esc 键。
    这是终端程序的通行做法。
    """
    ch = _raw_read(1, timeout, stream=stream)
    if not ch:
        return None
    if ch != "\x1b":
        return KEYS.get(ch, ch)
    seq = ch
    for _ in range(64):
        # 只能逐字节补 Esc 序列。一次 read(4) 可能把紧随方向键的 Enter
        # 一起取走，表现为快速“↓ Enter”时确认键被吞。
        nxt = _raw_read(1, 0.03, stream=stream)
        if not nxt:
            break
        seq += nxt
        if seq in KEYS:
            return KEYS[seq]
        if (seq.startswith("\x1b[") and len(seq) > 2
                and "@" <= seq[-1] <= "~"):
            break
        if (seq.startswith("\x1bO") and len(seq) > 2
                and seq[-1].isalpha()):
            break
        if not seq.startswith(("\x1b[", "\x1bO")) and len(seq) > 1:
            break
    if seq == PASTE_START:
        # 必须在下面几个解码器之前拦截：ESC[200~ 的结尾 `~` 落在 CSI 终止符
        # 区间里，会被当成一个普通的未知序列原样吐出去。
        return _read_paste(stream)
    mouse = _decode_sgr_mouse(seq)
    if mouse is not None:
        return mouse
    position = _decode_cursor_position(seq)
    if position is not None:
        return position
    modified = _decode_modified_key(seq)
    if modified == "ignore":
        return None
    if modified is not None:
        return modified
    return KEYS.get(seq, "esc" if seq == "\x1b" else seq)


def _sanitize_paste(text):
    """把粘贴载荷压成纯文本。

    三件事，每件都有具体理由：
    1. `\r\n` / `\r` 统一成 `\n` —— 从 Windows 或网页复制来的换行否则会在
       编辑器里变成多余的空行。
    2. 剥掉所有 ESC 序列。**这是安全边界不是洁癖**：粘贴内容来自剪贴板，可能
       是从任意网页复制的；若原样保留，一段精心构造的文本就能在渲染时移动光标、
       改颜色，甚至（在别的终端程序里）触发命令。粘贴永远只是文本。
    3. 去掉其余 C0/C1 控制字符，但**保留** `\t`、ZWJ 与变体选择符 ——
       后两者是 emoji 组合的一部分，_cell_width 专门处理过它们。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI.sub("", text)
    out = []
    for char in text:
        code = ord(char)
        if char in "\n\t":
            out.append(char)
            continue
        if char == "\u200d" or 0xFE00 <= code <= 0xFE0F:
            out.append(char)
            continue
        if code < 32 or 0x7F <= code < 0xA0:
            continue
        out.append(char)
    # 末尾的换行几乎总是复制时捎带的，不是用户想要的空行；留着会让输入框凭空
    # 高一行。只裁末尾，其余一字不动。
    return "".join(out).rstrip("\n")


def _read_paste(stream=None):
    """读到 ESC[201~ 为止，返回一个 PasteEvent。

    逐字符读而不是整块读：整块读会顺手把粘贴结束符之后的按键一起吞掉，那就得
    再引入一层回推缓冲。粘贴通常只有几 KB，逐字符完全够快。

    终止符缺失（终端半途断了、或对端根本没发结束符）时靠 PASTE_MAX_CHARS 和
    读超时兜底 —— 绝不能在这里挂住整个输入线程。
    """
    buf = []
    tail = ""
    truncated = False
    while True:
        char = _raw_read(1, 0.5, stream=stream)
        if not char:
            truncated = True
            break
        buf.append(char)
        tail = (tail + char)[-len(PASTE_END):]
        if tail == PASTE_END:
            del buf[-len(PASTE_END):]
            break
        if len(buf) > PASTE_MAX_CHARS:
            truncated = True
            break
    return PasteEvent(text=_sanitize_paste("".join(buf)), truncated=truncated)


_ANSI = re.compile(
    r"(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\))"
    r"|(?:\x1b[P^_X][^\x1b]*(?:\x1b\\))"
    r"|(?:\x1b\[[0-?]*[ -/]*[@-~])"
    r"|(?:\x1b[@-_])"
)
_SAFE_TRANSCRIPT_ASCII = re.compile(r"[\t\n -~]*\Z")
# Option labels are copied into the editable draft.  Keep the grammar narrow
# enough that a catalog (including one discovered from the workspace) cannot
# inject whitespace, shell syntax, or terminal control sequences.  The
# namespaced forms are used only for recipe disambiguation, e.g.
# ``project:review``.
_OPTION_NAME = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63}|"
    r"(?:builtin|user|project):[A-Za-z0-9][A-Za-z0-9_-]{1,47})")


def _cell_width(char, *, joined=False):
    """Width of one Unicode scalar for the terminal model used by zylab."""
    code = ord(char)
    if char == "\t":
        return None                    # caller resolves the next tab stop
    if (code < 32 or 0x7F <= code < 0xA0
            or unicodedata.category(char) in {"Cc", "Cf"}
            or char == "\u200d"
            or 0xFE00 <= code <= 0xFE0F
            or 0x1F3FB <= code <= 0x1F3FF
            or unicodedata.combining(char)):
        return 0
    if joined:
        # Emoji ZWJ sequences occupy one glyph cell cluster. The first emoji
        # already contributed its width; joined components add no columns.
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text):
    """文本在终端上占多少**列**（不是多少个字符）。

    两件事会让「字符数」和「列数」不一致，光标定位必须按后者：
      - 中日韩字符是**双宽**：`我` 是 1 个字符但占 2 列。
      - ANSI 转义序列（颜色）**不占列**：`\x1b[36m` 是 5 个字符、0 列。
    只按 len() 算，中文一多光标就飘到文字右边好几格。
    """
    plain = _ANSI.sub("", str(text))
    if plain.isascii() and plain.isprintable():
        return len(plain)
    w = 0
    joined = False
    regional_pending = False
    for ch in plain:
        if ch == "\u200d":
            joined = True
            continue
        # A pair of regional-indicator symbols is one two-column flag.
        regional = 0x1F1E6 <= ord(ch) <= 0x1F1FF
        if regional:
            if regional_pending:
                regional_pending = False
                continue
            regional_pending = True
            width = 2
        else:
            regional_pending = False
            width = _cell_width(ch, joined=joined)
        joined = False
        if width is None:
            w += 8 - (w % 8)
        else:
            w += width
    return w


def _display_cell_spans(text):
    """Return ``(text-index, cell-start, cell-end)`` glyph spans.

    History rows are sanitized before they reach this helper, but stripping
    ANSI here keeps the primitive safe for callers that use it with styled
    text.  Combining marks, variation selectors, ZWJ components and regional
    indicators stay attached to the preceding glyph so a selection never
    splits a visible cluster in the middle.
    """
    value = _ANSI.sub("", str(text))
    spans = []
    column = 0
    joined = False
    regional_pending = False
    for index, char in enumerate(value):
        if char == "\u200d":
            if spans:
                spans[-1][1] = index + 1
            joined = True
            continue
        regional = 0x1F1E6 <= ord(char) <= 0x1F1FF
        if regional:
            if regional_pending:
                cell = 0
                regional_pending = False
            else:
                cell = 2
                regional_pending = True
        else:
            regional_pending = False
            cell = _cell_width(char, joined=joined)
        joined = False
        if cell is None:
            cell = 8 - (column % 8)
        if cell == 0:
            if spans:
                spans[-1][1] = index + 1
            else:
                spans.append([index, index + 1, column, column])
            continue
        spans.append([index, index + 1, column, column + cell])
        column += cell
    return tuple(
        (int(start), int(end), int(cell_start), int(cell_end))
        for start, end, cell_start, cell_end in spans
    ), column


def _slice_display_cells(text, start, end):
    """Slice a display-cell interval without splitting wide glyphs."""
    value = _ANSI.sub("", str(text))
    spans, total = _display_cell_spans(value)
    start = min(max(0, int(start)), total)
    end = min(max(0, int(end)), total)
    if start >= end or not spans:
        return ""
    selected = [
        span for span in spans
        if span[3] > start and (span[2] < end or span[2] == span[3] == 0)
    ]
    if not selected:
        return ""
    return value[selected[0][0]:selected[-1][1]]


def _highlight_display_cells(text, start, end):
    """Apply inverse video to a display-cell interval in plain text."""
    value = _ANSI.sub("", str(text))
    spans, total = _display_cell_spans(value)
    start = min(max(0, int(start)), total)
    end = min(max(0, int(end)), total)
    if start >= end or not spans:
        return value
    selected = [
        span for span in spans
        if span[3] > start and span[2] < end
    ]
    if not selected:
        return value
    return (
        value[:selected[0][0]]
        + "\x1b[7m" + value[selected[0][0]:selected[-1][1]]
        + "\x1b[27m" + value[selected[-1][1]:]
    )


def wrap_display(text, columns):
    """按终端显示列做词级折行，返回物理行列表；用于「要看全、不能截」的内容。

    空格处优先断，CJK/长 token 在列宽处硬断；显式换行保留。ANSI 在这里按普通字符
    计宽，所以调用方应传纯文本、样式在外层加。与 truncate_display 的分工：单行 UI
    （状态栏、输入框、选择器行）截断，正文/说明/预览折行。
    """
    value = str(text)
    columns = max(1, int(columns))
    lines = []
    for raw in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        remaining = raw
        while display_width(remaining) > columns:
            width = 0
            cut = 0
            for index, char in enumerate(remaining):
                cell = display_width(char)
                if width + cell > columns:
                    break
                width += cell
                cut = index + 1
            if cut <= 0:
                cut = 1
            space = remaining.rfind(" ", 0, cut + 1)
            if space > 0:
                cut = space
            lines.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        lines.append(remaining)
    return lines or [""]


def truncate_display(text, columns):
    """按**显示列数**截断，而不是字符数。

    中文按字符截到 56 会占 112 列，在窄终端上直接折行、把界面撑乱。
    """
    text = str(text)
    columns = max(0, int(columns))
    if display_width(text) <= columns:
        return text
    if columns == 0:
        return ""
    out, width, position = [], 0, 0
    joined = False
    regional_pending = False
    saw_escape = False
    osc8_open = False

    def safety_suffix():
        return (
            ("\x1b]8;;\x1b\\" if osc8_open else "")
            + ("\x1b[0m" if saw_escape else "")
        )

    for match in _ANSI.finditer(text):
        segment = text[position:match.start()]
        for ch in segment:
            if ch == "\u200d":
                out.append(ch)
                joined = True
                continue
            regional = 0x1F1E6 <= ord(ch) <= 0x1F1FF
            if regional:
                if regional_pending:
                    cell = 0
                    regional_pending = False
                else:
                    cell = 2
                    regional_pending = True
            else:
                regional_pending = False
                cell = _cell_width(ch, joined=joined)
            joined = False
            if cell is None:
                cell = 8 - (width % 8)
            if width + cell > columns - 1:
                return "".join(out) + "…" + safety_suffix()
            out.append(ch)
            width += cell
        escape = match.group(0)
        out.append(escape)
        saw_escape = True
        if escape.startswith("\x1b]8;;"):
            target = escape[5:-2] if escape.endswith("\x1b\\") else ""
            osc8_open = bool(target)
        position = match.end()
    truncated = False
    for ch in text[position:]:
        if ch == "\u200d":
            out.append(ch)
            joined = True
            continue
        regional = 0x1F1E6 <= ord(ch) <= 0x1F1FF
        if regional:
            if regional_pending:
                cell = 0
                regional_pending = False
            else:
                cell = 2
                regional_pending = True
        else:
            regional_pending = False
            cell = _cell_width(ch, joined=joined)
        joined = False
        if cell is None:
            cell = 8 - (width % 8)
        if width + cell > columns - 1:
            out.append("…")
            truncated = True
            break
        out.append(ch)
        width += cell
    return "".join(out) + (safety_suffix() if truncated else "")


def pad_display(text, columns, *, align="left"):
    """按终端显示列截断并补齐，ANSI/CJK 不会破坏相邻列对齐。"""
    columns = max(0, int(columns))
    if align not in {"left", "right"}:
        raise ValueError("align 必须是 left 或 right")
    shown = truncate_display(str(text), columns)
    padding = " " * max(0, columns - display_width(shown))
    return padding + shown if align == "right" else shown + padding


@dataclass(frozen=True)
class TableColumn:
    """一个响应式终端表格列；priority 越大越先收缩/隐藏。"""

    width: int
    min_width: int = 1
    align: str = "left"
    priority: int = 0
    optional: bool = False

    def __post_init__(self):
        if int(self.width) < 1:
            raise ValueError("TableColumn.width 必须 >= 1")
        if not 1 <= int(self.min_width) <= int(self.width):
            raise ValueError("TableColumn.min_width 必须在 1..width 内")
        if self.align not in {"left", "right"}:
            raise ValueError("TableColumn.align 必须是 left 或 right")


@dataclass(frozen=True)
class TableLayout:
    """固定一次布局后复用于 header/rows，保证每一行列边界一致。"""

    columns: tuple
    widths: tuple
    gap: str = "  "
    total_width: int | None = None

    @classmethod
    def fit(cls, columns, total_width=None, *, gap="  "):
        columns = tuple(columns)
        if not columns:
            return cls((), (), str(gap), total_width)
        if not all(isinstance(column, TableColumn) for column in columns):
            raise TypeError("columns 必须全部是 TableColumn")
        widths = [int(column.width) for column in columns]
        if total_width is None:
            return cls(columns, tuple(widths), str(gap), None)

        total_width = max(0, int(total_width))
        gap_width = display_width(gap)

        def occupied():
            active = [width for width in widths if width > 0]
            return sum(active) + gap_width * max(0, len(active) - 1)

        # 先隐藏低价值的可选列，再收缩仍显示的列。相同 priority 时右侧
        # 先消失，窄终端仍保留左侧主键，读起来更稳定。
        optional = sorted(
            (index for index, column in enumerate(columns)
             if column.optional),
            key=lambda index: (columns[index].priority, index), reverse=True)
        for index in optional:
            if occupied() <= total_width:
                break
            widths[index] = 0

        shrinkable = sorted(
            range(len(columns)),
            key=lambda index: (columns[index].priority, index), reverse=True)
        for index in shrinkable:
            if occupied() <= total_width or widths[index] == 0:
                continue
            floor = int(columns[index].min_width)
            reduction = min(widths[index] - floor, occupied() - total_width)
            widths[index] -= max(0, reduction)

        return cls(columns, tuple(widths), str(gap), total_width)

    def row(self, values, *, wrap_last=False):
        """一行。``wrap_last=True`` 时最后一个可见列（说明/标题）按列宽折行、续行对齐
        在该列起点，返回多行字符串；其余列仍按列宽截断补齐。"""
        values = tuple(values)
        if len(values) != len(self.columns):
            raise ValueError(
                f"表格值数量 {len(values)} != 列数量 {len(self.columns)}")
        visible = [
            (value, width, column)
            for value, width, column in zip(values, self.widths, self.columns)
            if width > 0
        ]
        if not wrap_last or not visible:
            rendered = self.gap.join(
                pad_display(value, width, align=column.align)
                for value, width, column in visible)
            if self.total_width is not None:
                rendered = truncate_display(rendered, self.total_width)
            return rendered
        head, (last_value, last_width, last_column) = visible[:-1], visible[-1]
        lead = self.gap.join(
            pad_display(value, width, align=column.align)
            for value, width, column in head)
        if head:
            lead += self.gap
        indent = " " * display_width(lead)
        lines = []
        for index, part in enumerate(
                wrap_display(str(last_value), last_width)):
            cell = pad_display(part, last_width, align=last_column.align)
            rendered = (lead if index == 0 else indent) + cell
            if self.total_width is not None:
                rendered = truncate_display(rendered, self.total_width)
            lines.append(rendered)
        return "\n".join(lines)


@dataclass(frozen=True)
class MarkdownTheme:
    """Semantic terminal palette; callers choose capability, not raw colors."""

    heading1: str
    heading2: str
    heading3: str
    quote: str
    bullet: str
    rule: str
    inline_code: str
    link: str
    link_url: str
    fence: str
    code: str
    keyword: str
    string: str
    number: str
    comment: str
    operator: str
    type_name: str
    table_border: str
    table_header: str
    success: str
    danger: str


_DARK_MARKDOWN_THEME = MarkdownTheme(
    heading1="\x1b[38;5;214m", heading2="\x1b[38;5;117m",
    heading3="\x1b[38;5;150m", quote="\x1b[38;5;103m",
    bullet="\x1b[38;5;110m", rule="\x1b[38;5;238m",
    inline_code="\x1b[38;5;222m", link="\x1b[38;5;117m",
    link_url="\x1b[2;38;5;103m", fence="\x1b[38;5;110m",
    code="\x1b[38;5;252m", keyword="\x1b[38;5;177m",
    string="\x1b[38;5;150m", number="\x1b[38;5;215m",
    comment="\x1b[2;38;5;102m", operator="\x1b[38;5;110m",
    type_name="\x1b[38;5;117m", table_border="\x1b[38;5;240m",
    table_header="\x1b[1;38;5;117m", success="\x1b[38;5;114m",
    danger="\x1b[38;5;203m",
)

_LIGHT_MARKDOWN_THEME = MarkdownTheme(
    heading1="\x1b[38;5;130m", heading2="\x1b[38;5;25m",
    heading3="\x1b[38;5;28m", quote="\x1b[38;5;60m",
    bullet="\x1b[38;5;25m", rule="\x1b[38;5;250m",
    inline_code="\x1b[38;5;94m", link="\x1b[38;5;25m",
    link_url="\x1b[2;38;5;60m", fence="\x1b[38;5;25m",
    code="\x1b[38;5;235m", keyword="\x1b[38;5;90m",
    string="\x1b[38;5;28m", number="\x1b[38;5;130m",
    comment="\x1b[2;38;5;244m", operator="\x1b[38;5;25m",
    type_name="\x1b[38;5;24m", table_border="\x1b[38;5;248m",
    table_header="\x1b[1;38;5;24m", success="\x1b[38;5;28m",
    danger="\x1b[38;5;160m",
)


def markdown_theme(name=None, env=None):
    """Resolve dark/light palette without making color a hard dependency."""
    env = os.environ if env is None else env
    requested = str(
        name or paths.env_get("MARKDOWN_THEME", environ=env) or "dark").lower()
    if requested == "light":
        return _LIGHT_MARKDOWN_THEME
    return _DARK_MARKDOWN_THEME


def user_line_band(styled=True):
    """用户消息的整行底色（像 Claude Code 的 prompt 条）：深色主题灰底亮字，浅色主题浅灰底深字。
    返回 (开, 关)。"""
    if not styled:
        return "", ""
    if markdown_theme() is _LIGHT_MARKDOWN_THEME:
        return "\x1b[48;5;254;38;5;235m", "\x1b[0m"
    return "\x1b[48;5;236;38;5;255m", "\x1b[0m"


def terminal_style_enabled(stream=None, env=None):
    """Honor NO_COLOR/TERM=dumb while retaining explicit TTY detection."""
    env = os.environ if env is None else env
    if "NO_COLOR" in env or str(env.get("TERM", "")).lower() == "dumb":
        return False
    target = stream or sys.stdout
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


def terminal_hyperlinks_enabled(env=None):
    """Conservative OSC-8 capability check with an explicit override."""
    env = os.environ if env is None else env
    override = str(paths.env_get("HYPERLINKS", "", environ=env)).lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return bool(
        env.get("WT_SESSION")
        or env.get("VTE_VERSION")
        or env.get("KONSOLE_VERSION")
        or str(env.get("TERM_PROGRAM", "")).lower() in {
            "iterm.app", "wezterm", "vscode", "ghostty",
        }
    )


def terminal_mouse_enabled(stream=None, env=None):
    """Return whether the fixed hybrid mouse UI can run on this TTY.

    The interactive UI always wants wheel/click events so it can navigate the
    transcript.  Terminal emulators that support mouse capture conventionally
    reserve ``Shift+drag`` for their own text selection.  The old
    ``ZYLAB_MOUSE=native`` values remain as a startup-only escape hatch for
    terminals where that convention is unavailable; the ``/mouse`` command no
    longer toggles this state at runtime.
    """
    # I4（SPEC-CC-parity）：内联模式不抓鼠标、不画伪选区——滚动与选择都是终端原生的。
    # 全屏应用模式的鼠标由 core/apprender 自己管。
    return False

class _TerminalTextSanitizer:
    """Incrementally remove terminal control sequences from model text."""

    def __init__(self):
        self.state = "text"
        self.pending_cr = False

    def _text_char(self, char, out):
        code = ord(char)
        if char == "\x1b":
            self.state = "esc"
        elif char == "\r":
            self.pending_cr = True
        elif char in ("\n", "\t") or (
                code >= 32
                and not (0x7F <= code < 0xA0)
                and (char == "\u200d"
                     or unicodedata.category(char) != "Cf")):
            out.append(char)

    def feed(self, value):
        out = []
        for char in str(value):
            if self.state == "text" and self.pending_cr:
                out.append("\n")
                self.pending_cr = False
                if char == "\n":
                    continue
            if self.state == "text":
                self._text_char(char, out)
            elif self.state == "esc":
                if char == "[":
                    self.state = "csi"
                elif char in "]P^_X":
                    self.state = "string"
                else:
                    self.state = "text"
            elif self.state == "csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif self.state == "string":
                if char == "\x07":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_esc"
            elif self.state == "string_esc":
                if char == "\\":
                    self.state = "text"
                elif char != "\x1b":
                    self.state = "string"
        return "".join(out)

    def finish(self):
        tail = "\n" if self.pending_cr and self.state == "text" else ""
        self.state = "text"
        self.pending_cr = False
        return tail


def sanitize_terminal_text(value):
    """Remove ANSI/OSC/C0 controls from persisted or non-streamed text."""
    value = str(value)
    if _SAFE_TRANSCRIPT_ASCII.fullmatch(value) or value.isprintable():
        return value
    sanitizer = _TerminalTextSanitizer()
    return sanitizer.feed(value) + sanitizer.finish()


class _SyntaxHighlighter:
    """Small stateful lexer for common coding-agent output languages.

    This is intentionally lexical, not a parser. It keeps zylab zero-
    dependency while distinguishing comments, strings, numbers, keywords and
    types. Unknown languages retain the readable code-block theme unchanged.
    """

    RESET = "\x1b[0m"
    ALIASES = {
        "py": "python", "python3": "python",
        "js": "javascript", "jsx": "javascript", "mjs": "javascript",
        "ts": "typescript", "tsx": "typescript",
        "sh": "shell", "bash": "shell", "zsh": "shell", "console": "shell",
        "yml": "yaml", "rs": "rust", "golang": "go",
        "c++": "cpp", "cc": "cpp", "h": "c", "hpp": "cpp",
        "md": "markdown", "text": "plaintext", "txt": "plaintext",
        "diff": "diff", "patch": "diff",
    }
    KEYWORDS = {
        "python": frozenset("""
            and as assert async await break case class continue def del elif
            else except False finally for from global if import in is lambda
            match None nonlocal not or pass raise return True try while with
            yield
        """.split()),
        "javascript": frozenset("""
            async await break case catch class const continue debugger default
            delete do else export extends false finally for from function get
            if import in instanceof let new null of return set static super
            switch this throw true try typeof undefined var void while with
            yield
        """.split()),
        "typescript": frozenset("""
            abstract any as asserts async await boolean break case catch class
            const constructor continue declare default delete do else enum
            export extends false finally for from function get if implements
            import in infer instanceof interface is keyof let module namespace
            never new null number object of override private protected public
            readonly return satisfies set static string super switch symbol
            this throw true try type typeof undefined unique unknown var void
            while with yield
        """.split()),
        "c": frozenset("""
            auto break case char const continue default do double else enum
            extern float for goto if inline int long register restrict return
            short signed sizeof static struct switch typedef union unsigned
            void volatile while
        """.split()),
        "cpp": frozenset("""
            alignas alignof auto bool break case catch char class concept const
            constexpr continue co_await co_return co_yield decltype default
            delete do double else enum explicit export extern false float for
            friend if inline int long namespace new noexcept nullptr operator
            override private protected public requires return short signed
            sizeof static struct switch template this throw true try typedef
            typename union unsigned using virtual void volatile while
        """.split()),
        "rust": frozenset("""
            as async await break const continue crate dyn else enum extern
            false fn for if impl in let loop match mod move mut pub ref return
            self Self static struct super trait true type unsafe use where while
        """.split()),
        "go": frozenset("""
            break case chan const continue default defer else fallthrough for
            func go goto if import interface map package range return select
            struct switch type var
        """.split()),
        "java": frozenset("""
            abstract assert boolean break byte case catch char class const
            continue default do double else enum extends final finally float
            for if implements import instanceof int interface long native new
            package private protected public return short static strictfp super
            switch synchronized this throw throws transient try void volatile
            while true false null
        """.split()),
        "shell": frozenset("""
            case do done elif else esac export fi for function if in local
            readonly return select set shift then time trap until while
        """.split()),
        "sql": frozenset("""
            all alter and as asc begin between by case check column commit
            constraint create database default delete desc distinct drop else
            end exists foreign from full grant group having in index inner
            insert into is join key left like limit not null on or order outer
            primary references right rollback row select set table then union
            unique update values view when where with
        """.split()),
        "json": frozenset("true false null".split()),
        "yaml": frozenset("true false null yes no on off".split()),
    }
    TYPE_NAMES = frozenset("""
        bool byte bytes char dict double error f32 f64 float i8 i16 i32 i64
        int integer list long map object result set str string tuple u8 u16
        u32 u64 uint usize vec void
    """.split())
    HASH_COMMENT = frozenset(
        {"python", "shell", "ruby", "yaml", "toml", "perl", "r"})
    SLASH_COMMENT = frozenset(
        {"javascript", "typescript", "c", "cpp", "rust", "go", "java",
         "swift", "kotlin", "scala"})
    BLOCK_COMMENT = frozenset(
        {"javascript", "typescript", "c", "cpp", "rust", "go", "java",
         "swift", "kotlin", "scala", "css"})

    def __init__(self, language, theme, *, styled=True):
        parts = str(language or "").strip().split(maxsplit=1)
        raw = parts[0].lower() if parts else ""
        raw = raw.strip("{}.")
        self.language = self.ALIASES.get(raw, raw)
        self.theme = theme
        self.styled = bool(styled)
        self.block_end = None
        self.string_end = None

    def _paint(self, style, value):
        if not self.styled or not value:
            return value
        return style + value + self.RESET + self.theme.code

    @staticmethod
    def _quoted_end(line, start, delimiter):
        index = start + len(delimiter)
        while index < len(line):
            if line.startswith(delimiter, index):
                backslashes = 0
                cursor = index - 1
                while cursor >= 0 and line[cursor] == "\\":
                    backslashes += 1
                    cursor -= 1
                if backslashes % 2 == 0:
                    return index + len(delimiter)
            index += 1
        return None

    def highlight(self, line):
        line = str(line)
        if not self.styled or self.language in {
                "", "plaintext", "text", "markdown"}:
            return line
        if self.language == "diff":
            if line.startswith("@@"):
                return self._paint(self.theme.keyword, line)
            if line.startswith("+") and not line.startswith("+++"):
                return self._paint(self.theme.success, line)
            if line.startswith("-") and not line.startswith("---"):
                return self._paint(self.theme.danger, line)
            if line.startswith(("+++", "---")):
                return self._paint(self.theme.type_name, line)
            return line

        out = []
        index = 0
        while index < len(line):
            if self.block_end:
                end = line.find(self.block_end, index)
                if end < 0:
                    out.append(self._paint(self.theme.comment, line[index:]))
                    return "".join(out)
                end += len(self.block_end)
                out.append(self._paint(self.theme.comment, line[index:end]))
                self.block_end = None
                index = end
                continue
            if self.string_end:
                end = self._quoted_end(line, index - len(self.string_end),
                                       self.string_end)
                if end is None:
                    out.append(self._paint(self.theme.string, line[index:]))
                    return "".join(out)
                out.append(self._paint(self.theme.string, line[index:end]))
                self.string_end = None
                index = end
                continue

            if (self.language in self.BLOCK_COMMENT
                    and line.startswith("/*", index)):
                end = line.find("*/", index + 2)
                if end < 0:
                    self.block_end = "*/"
                    out.append(self._paint(self.theme.comment, line[index:]))
                    return "".join(out)
                end += 2
                out.append(self._paint(self.theme.comment, line[index:end]))
                index = end
                continue
            if (self.language in self.SLASH_COMMENT
                    and line.startswith("//", index)):
                out.append(self._paint(self.theme.comment, line[index:]))
                break
            if (self.language == "sql"
                    and line.startswith("--", index)):
                out.append(self._paint(self.theme.comment, line[index:]))
                break
            if (self.language in self.HASH_COMMENT and line[index] == "#"
                    and (index == 0 or line[index - 1].isspace())):
                out.append(self._paint(self.theme.comment, line[index:]))
                break

            delimiter = None
            if (self.language == "python"
                    and line.startswith(("'''", '"""'), index)):
                delimiter = line[index:index + 3]
            elif line[index] in "'\"":
                delimiter = line[index]
            elif (line[index] == "`"
                    and self.language in {"javascript", "typescript", "shell"}):
                delimiter = "`"
            if delimiter is not None:
                end = self._quoted_end(line, index, delimiter)
                if end is None:
                    if len(delimiter) == 3:
                        self.string_end = delimiter
                    out.append(self._paint(self.theme.string, line[index:]))
                    break
                token = line[index:end]
                following = line[end:].lstrip()
                style = (
                    self.theme.type_name
                    if self.language in {"json", "yaml"}
                    and following.startswith(":")
                    else self.theme.string)
                out.append(self._paint(style, token))
                index = end
                continue

            number = re.match(
                r"(?:0[xX][0-9A-Fa-f]+|0[bB][01]+|"
                r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)",
                line[index:])
            if number:
                token = number.group(0)
                out.append(self._paint(self.theme.number, token))
                index += len(token)
                continue
            identifier = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", line[index:])
            if identifier:
                token = identifier.group(0)
                lowered = token.lower()
                keywords = self.KEYWORDS.get(self.language, frozenset())
                if token in keywords or lowered in keywords:
                    style = self.theme.keyword
                elif lowered in self.TYPE_NAMES or (
                        token[:1].isupper() and self.language not in {
                            "shell", "sql", "json", "yaml"}):
                    style = self.theme.type_name
                else:
                    style = ""
                out.append(self._paint(style, token) if style else token)
                index += len(token)
                continue
            if line[index] in "{}[]()=+-*/%<>!&|^~:;,.@":
                out.append(self._paint(self.theme.operator, line[index]))
            else:
                out.append(line[index])
            index += 1
        return "".join(out)


_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_LEADING_STRUCTURAL_INDENT = re.compile(r"^((?:\x1b\[[0-9;]*m)*)  (?=\S)")
_WRAP_TOKEN = re.compile(rf"({_ANSI.pattern})|(\n)|([^\x1b\n]+)")


class _HangingWrap:
    """SPEC-CC-parity C6 的续行部分：icon_hosted 模式下把纯段落挂在 ⏺ 下面。

    结构行（标题/列表/引用/围栏/表格）自带两格缩进且已按列宽预折行——它们以空格
    开头，整行原样穿过。顶在第 0 列的纯文本行补两格；纯文本超过列宽时在词边界折行、
    续行同样两格。于是整条 assistant 消息都对齐在 ⏺ 之后，而不是续行顶回第 0 列。

    流式：攒当前这个词（连续的 ASCII 非空白），到空白/换行/finish 才决定它留在本行
    还是换行，延迟只有一个词；宽字符（CJK）单字成词，可在任意处折。转义序列零宽，
    跟着词一起进出，顺序不变。列宽每次现取，窗口变了下一行就按新宽度折。
    """
    INDENT = "  "

    def __init__(self, width):
        self._width = width
        self._cache = {}
        self.reset()

    def reset(self):
        self._col = len(self.INDENT)   # 首行前面是 "⏺ "，已占两列
        self._line_start = False       # 首行由图标托管，不补缩进
        self._passthrough = False      # 结构行：原样穿过直到行尾
        self._word = []                # 当前词的片段（含零宽转义）
        self._word_width = 0
        self._space = ""               # 词前的空白；折行时被吃掉

    def _limit(self):
        try:
            return max(8, int(self._width()))
        except (TypeError, ValueError):
            return 80

    def _cw(self, ch):
        w = self._cache.get(ch)
        if w is None:
            w = display_width(ch)
            self._cache[ch] = w
        return w

    def push(self, text):
        if not text:
            return text
        out = []
        for m in _WRAP_TOKEN.finditer(text):
            esc, newline, run = m.group(1), m.group(2), m.group(3)
            if esc is not None:
                (out if self._passthrough else self._word).append(esc)
            elif newline is not None:
                self._flush(out)
                out.append("\n")
                self._col = 0
                self._line_start = True
                self._passthrough = False
            else:
                self._push_run(run, out)
        return "".join(out)

    def flush(self):
        out = []
        self._flush(out)
        return "".join(out)

    def _push_run(self, run, out):
        if self._passthrough:
            out.append(run)
            return
        for ch in run:
            if self._passthrough:
                out.append(ch)
                continue
            if self._line_start:
                self._line_start = False
                if ch in " \t":
                    # 结构行自带缩进：连同已攒的零宽转义原样放出，本行不再管
                    self._passthrough = True
                    out.extend(self._word)
                    self._word, self._word_width = [], 0
                    out.append(ch)
                    continue
                out.append(self.INDENT)
                self._col = len(self.INDENT)
            if ch in " \t":
                self._flush(out)
                self._space += ch
                continue
            w = self._cw(ch)
            if w >= 2:                  # 宽字符单字成词
                self._flush(out)
                self._word.append(ch)
                self._word_width = w
                self._flush(out)
                continue
            self._word.append(ch)
            self._word_width += w

    def _flush(self, out):
        if not self._word:
            if self._space:
                out.append(self._space)
                self._col += len(self._space)
                self._space = ""
            return
        limit = self._limit()
        indent = len(self.INDENT)
        if (self._col + len(self._space) + self._word_width > limit
                and self._col > indent):
            out.append("\n" + self.INDENT)     # 折行吃掉词前空白
            self._col = indent
        else:
            out.append(self._space)
            self._col += len(self._space)
        self._space = ""
        if self._word_width > limit - self._col:
            # 比剩余整行还长的词（URL/长标识符）：按字符硬折
            for piece in self._word:
                if piece.startswith("\x1b"):
                    out.append(piece)
                    continue
                for ch in piece:
                    w = self._cw(ch)
                    if self._col + w > limit and self._col > indent:
                        out.append("\n" + self.INDENT)
                        self._col = indent
                    out.append(ch)
                    self._col += w
        else:
            out.extend(self._word)
            self._col += self._word_width
        self._word, self._word_width = [], 0

COMPOSER_PLACEHOLDER = "输入消息 · / 命令 · @ 文件 · ? 快捷键"


def compact_status(status):
    """状态栏对齐 Claude Code：只留模型、上下文与模式标记；chat id、memory、git、沙箱标记、cwd
    这些去 /doctor 看。输入是 status_line() 拼好的 " · " 串（可带颜色码）。"""
    keep = []
    for index, part in enumerate(str(status or "").split(" · ")):
        plain = _ANSI.sub("", part).strip()
        if not plain:
            continue
        if index == 0:                      # 第一段是会话身份：chat <id> 或改名后的标题，总保留
            keep.append(part)
            continue
        if ("@" in plain or plain.startswith("ctx ") or plain in ("plan", "auto", "仅聊天")
                or plain.startswith(("next ", "workflow", "需要拍板", "gate recovery"))
                or re.fullmatch(r"\d+ agents?", plain)):   # J5：活跃子代理计数（← 1 agent）
            keep.append(part)
    return " · ".join(keep)


class StreamingMarkdown:
    """Streaming, terminal-safe Markdown renderer for conversational output.

    Plain prose still appears immediately. Only structurally ambiguous prefixes
    (fences, headings, lists, quotes and leading-pipe tables) wait for a line
    boundary. Parser state survives arbitrary provider chunking and every
    returned chunk resets ANSI before the persistent composer is redrawn.
    """

    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    ITALIC = "\x1b[3m"
    UNDERLINE = "\x1b[4m"
    STRIKE = "\x1b[9m"
    INLINE_CODE = "\x1b[38;5;222m"
    FENCE = "\x1b[38;5;110m"
    CODE = "\x1b[38;5;252m"
    CODE_BUFFER_LIMIT = 16_384

    def __init__(self, *, columns=None, styled=True, theme=None,
                 icon_hosted=False,
                 hyperlinks=None, syntax_highlight=True, sanitize=True):
        self.columns = columns or _cols
        self.styled = bool(styled)
        self._icon_hosted = bool(icon_hosted)
        self._first_line_pending = bool(icon_hosted)
        # 托管模式下整条消息挂在 ⏺ 下面：纯段落补缩进并按列宽折行（C6 续行）
        self._hanging = _HangingWrap(self._width) if icon_hosted else None
        self.theme = (
            theme if isinstance(theme, MarkdownTheme)
            else markdown_theme(theme))
        # Preserve the public constants used by existing integrations while
        # letting an instance select a light palette.
        self.INLINE_CODE = self.theme.inline_code
        self.FENCE = self.theme.fence
        self.CODE = self.theme.code
        self.hyperlinks = (
            terminal_hyperlinks_enabled()
            if hyperlinks is None else bool(hyperlinks))
        self.syntax_highlight = bool(syntax_highlight)
        self.sanitize = bool(sanitize)
        self._sanitizer = _TerminalTextSanitizer()
        self._reset_state()

    def _reset_state(self):
        self._in_fence = False
        self._fence_char = None
        self._fence_len = 0
        self._fence_label = ""
        self._highlighter = None
        self._at_line_start = True
        self._line_buffer = ""
        self._code_line_open = False
        self._bold = False
        self._italic = False
        self._strike = False
        self._inline_code = False
        self._pending_marker = None
        self._pending_marker_count = 0
        self._escape_pending = False
        self._last_visible_char = "\n"
        self._block_style = ""
        self._link_buffer = None
        self._link_phase = None
        self._pending_table_header = None
        self._in_table = False
        self._table_alignments = ()
        self._table_widths = ()
        self._table_rows = []
        self._table_count = 0

    def _width(self):
        value = self.columns() if callable(self.columns) else self.columns
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 80

    def _style(self, value):
        return value if self.styled else ""

    def _active_style(self):
        if not self.styled:
            return ""
        styles = []
        if self._block_style:
            styles.append(self._block_style)
        if self._bold:
            styles.append(self.BOLD)
        if self._italic:
            styles.append(self.ITALIC)
        if self._strike:
            styles.append(self.STRIKE)
        if self._inline_code:
            styles.append(self.INLINE_CODE)
        return "".join(styles)

    def _inline_transition(self):
        if not self.styled:
            return ""
        return self.RESET + self._active_style()

    def _resume_style(self):
        """返回跨 renderer flush 后应恢复的当前样式。"""
        if self.styled and self._in_fence and self._code_line_open:
            return self.CODE
        return self._active_style()

    def _emit_literal(self, value, out):
        if not value:
            return
        out.append(value)
        for char in reversed(value):
            if not char.isspace():
                self._last_visible_char = char
                break

    def _resolve_marker(self, out, next_char=""):
        marker = self._pending_marker
        count = self._pending_marker_count
        self._pending_marker = None
        self._pending_marker_count = 0
        if not marker or not count:
            return
        previous = self._last_visible_char
        can_open = bool(next_char and not next_char.isspace())
        can_close = bool(previous and not previous.isspace())

        if marker == "*":
            pairs, single = divmod(count, 2)
            if pairs:
                if ((self._bold and can_close)
                        or (not self._bold and can_open)):
                    if pairs % 2:
                        self._bold = not self._bold
                        out.append(self._inline_transition())
                    if pairs > 1:
                        self._emit_literal("**" * (pairs - 1), out)
                else:
                    self._emit_literal("**" * pairs, out)
            if single:
                if ((self._italic and can_close)
                        or (not self._italic and can_open)):
                    self._italic = not self._italic
                    out.append(self._inline_transition())
                else:
                    self._emit_literal("*", out)
            return
        pairs, single = divmod(count, 2)
        if pairs:
            if ((self._strike and can_close)
                    or (not self._strike and can_open)):
                if pairs % 2:
                    self._strike = not self._strike
                    out.append(self._inline_transition())
                if pairs > 1:
                    self._emit_literal("~~" * (pairs - 1), out)
            else:
                self._emit_literal("~~" * pairs, out)
        if single:
            self._emit_literal("~", out)

    @staticmethod
    def _plain_link_label(value):
        value = re.sub(r"\\([\\\[\]()`*_~])", r"\1", value)
        return re.sub(r"(?<!\\)[`*_~]", "", value)

    def _render_link(self, candidate, out):
        match = re.fullmatch(r"\[([^\]\n]+)\]\(([^\n]+)\)", candidate)
        if match is None:
            self._emit_literal(candidate, out)
            return
        label = self._plain_link_label(match.group(1)).strip()
        target = match.group(2).strip()
        # Strip an optional Markdown title without accepting control-bearing
        # or whitespace-obfuscated OSC targets.
        title = re.fullmatch(
            r"(?:<([^<>]+)>|([^\s]+))(?:\s+[\"'][^\n]*[\"'])?",
            target)
        url = (title.group(1) or title.group(2)) if title else target
        try:
            parsed = urlsplit(url)
        except ValueError:
            parsed = None
        safe_osc = (
            parsed is not None
            and parsed.scheme.lower() in {"http", "https", "mailto"}
            and not any(char.isspace() for char in url)
            and not any(ord(char) < 32 or ord(char) == 127 for char in url))
        label = label or url
        active = self._active_style()
        if self.styled and self.hyperlinks and safe_osc:
            out.append(f"\x1b]8;;{url}\x1b\\")
            out.append(self.theme.link + self.UNDERLINE + label)
            out.append(self.RESET + "\x1b]8;;\x1b\\" + active)
        else:
            if self.styled:
                out.append(self.theme.link + self.UNDERLINE + label)
                out.append(self.RESET + active)
            else:
                out.append(label)
            if url != label:
                visible_url = truncate_display(
                    url, max(12, min(60, self._width() // 2)))
                if self.styled:
                    out.append(
                        self.theme.link_url + f" ({visible_url})"
                        + self.RESET + active)
                else:
                    out.append(f" ({visible_url})")
        self._last_visible_char = label[-1] if label else self._last_visible_char

    def _feed_link_candidate(self, char, out):
        self._link_buffer += char
        if len(self._link_buffer) > 4096:
            value = self._link_buffer
            self._link_buffer = self._link_phase = None
            self._emit_literal(value, out)
            return True
        if self._link_phase == "label":
            if char == "]":
                self._link_phase = "after_label"
            return True
        if self._link_phase == "after_label":
            if char == "(":
                self._link_phase = "target"
                return True
            value = self._link_buffer[:-1]
            self._link_buffer = self._link_phase = None
            self._emit_literal(value, out)
            return False
        if self._link_phase == "target" and char == ")":
            value = self._link_buffer
            self._link_buffer = self._link_phase = None
            self._render_link(value, out)
        return True

    def _close_inline(self):
        out = []
        if self._link_buffer:
            self._emit_literal(self._link_buffer, out)
            self._link_buffer = self._link_phase = None
        self._resolve_marker(out, "\n")
        if self._escape_pending:
            self._emit_literal("\\", out)
            self._escape_pending = False
        if (self._bold or self._italic or self._strike
                or self._inline_code or self._block_style):
            self._bold = False
            self._italic = False
            self._strike = False
            self._inline_code = False
            self._block_style = ""
            out.append(self._style(self.RESET))
        return "".join(out)

    def _feed_inline_char(self, char, out):
        if self._link_buffer is not None:
            if self._feed_link_candidate(char, out):
                return
        if self._escape_pending:
            self._escape_pending = False
            self._emit_literal(char, out)
            return
        if char == "\\" and not self._inline_code:
            self._escape_pending = True
            return
        if (self._pending_marker is not None
                and char != self._pending_marker):
            self._resolve_marker(out, char)
        if char == "`":
            self._resolve_marker(out, char)
            self._inline_code = not self._inline_code
            out.append(self._inline_transition())
            return
        if not self._inline_code and char in "*~":
            if self._pending_marker == char:
                self._pending_marker_count += 1
            else:
                self._resolve_marker(out, char)
                self._pending_marker = char
                self._pending_marker_count = 1
            return
        if not self._inline_code and char == "[":
            self._link_buffer = "["
            self._link_phase = "label"
            return
        self._emit_literal(char, out)

    @staticmethod
    def _leading_spaces(value):
        return len(value) - len(value.lstrip(" "))

    @staticmethod
    def _fence(value):
        match = re.match(r"^( {0,3})(`{3,}|~{3,})(.*)$", value)
        if match is None:
            return None
        marker = match.group(2)
        if any(char != marker[0] for char in marker):
            return None
        return marker, match.group(3).strip()

    @staticmethod
    def _wrap_block_text(value, columns):
        """Word-wrap one buffered structural line by terminal display cells."""
        return wrap_display(str(value).strip(), columns)

    def _render_prefixed_block(self, prefix, continuation, body, out, *,
                               prefix_style="", body_style=""):
        prefix_limit = max(1, self._width() - 1)
        prefix = truncate_display(prefix, prefix_limit)
        continuation = truncate_display(continuation, prefix_limit)
        available = max(1, self._width() - display_width(prefix))
        parts = self._wrap_block_text(body, available)
        self._block_style = self._style(body_style)
        for index, part in enumerate(parts):
            current_prefix = prefix if index == 0 else continuation
            if index:
                out.append(self._style(self.RESET) + "\n")
            out.append(self._style(prefix_style) + current_prefix)
            out.append(self._style(self.RESET) + self._active_style())
            for char in part:
                self._feed_inline_char(char, out)
        out.append(self._close_inline())
        out.append("\n")
        self._last_visible_char = "\n"

    @staticmethod
    def _split_table_row(value):
        value = str(value).strip()
        if not value.startswith("|"):
            return None
        if value.endswith("|"):
            value = value[1:-1]
        else:
            value = value[1:]
        cells, cell = [], []
        escaped = False
        in_code = False
        for char in value:
            if escaped:
                cell.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "`":
                in_code = not in_code
                cell.append(char)
            elif char == "|" and not in_code:
                cells.append("".join(cell).strip())
                cell = []
            else:
                cell.append(char)
        if escaped:
            cell.append("\\")
        cells.append("".join(cell).strip())
        return cells

    @classmethod
    def _table_separator(cls, value):
        cells = cls._split_table_row(value)
        if not cells:
            return None
        alignments = []
        for cell in cells:
            marker = cell.replace(" ", "")
            if re.fullmatch(r":?-{3,}:?", marker) is None:
                return None
            if marker.startswith(":") and marker.endswith(":"):
                alignments.append("center")
            elif marker.endswith(":"):
                alignments.append("right")
            else:
                alignments.append("left")
        return tuple(alignments)

    @staticmethod
    def _plain_table_cell(value):
        # 只去掉行内渲染真正当作标记的字符（` * ~）。下划线不是标记——t_bash、
        # normalize_decision_gate_request 这种标识符里的 _ 曾被一并剥掉。
        value = re.sub(
            r"\[([^\]\n]+)\]\(([^\n)]+)\)", r"\1", str(value))
        value = re.sub(r"\\([\\|`*_~])", r"\1", value)
        return re.sub(r"(?<!\\)[`*~]", "", value).strip()

    def _table_column_widths(self, rows, count):
        """按内容定列宽：每列先取自然宽度（最长单元格），整表放不下时从最宽的列开始
        一格一格让，直到进得了 viewport；让出去的内容在单元格内折行，不截断。"""
        usable = max(count, self._width() - 2 - (count + 1))
        natural = [1] * count
        for cells in rows:
            for index in range(count):
                text = self._plain_table_cell(
                    cells[index] if index < len(cells) else "")
                natural[index] = max(natural[index], display_width(text))
        widths = list(natural)
        total = sum(widths)
        while total > usable:
            widest = max(range(count), key=widths.__getitem__)
            if widths[widest] <= 1:
                break
            widths[widest] -= 1
            total -= 1
        return tuple(widths)

    def _table_border(self, left, middle, right):
        line = "  " + left + middle.join(
            "─" * width for width in self._table_widths) + right
        return (
            self._style(self.theme.table_border)
            + truncate_display(line, self._width())
            + self._style(self.RESET)
        )

    def _table_row(self, cells, *, header=False):
        """一行单元格排成若干物理行：每格按列宽词级折行，行高取最高的格。"""
        widths = self._table_widths
        columns = []
        for index, width in enumerate(widths):
            text = self._plain_table_cell(
                cells[index] if index < len(cells) else "")
            columns.append(wrap_display(text, width) if text else [""])
        height = max((len(column) for column in columns), default=1)
        lines = []
        for line_index in range(height):
            padded = []
            for index, width in enumerate(widths):
                column = columns[index]
                value = column[line_index] if line_index < len(column) else ""
                value = truncate_display(value, width)   # 安全网；折行已保证不超宽
                padding = max(0, width - display_width(value))
                alignment = (
                    self._table_alignments[index]
                    if index < len(self._table_alignments) else "left")
                if alignment == "right":
                    left, right = padding, 0
                elif alignment == "center":
                    left = padding // 2
                    right = padding - left
                else:
                    left, right = 0, padding
                padded.append(" " * left + value + " " * right)
            lines.append(self._table_row_line(padded, header=header))
        return "\n".join(lines)

    def _table_row_line(self, padded, *, header=False):
        border = self._style(self.theme.table_border)
        cell_style = self._style(
            self.theme.table_header if header else "")
        reset = self._style(self.RESET)
        return (
            border + "  │" + reset + cell_style
            + (reset + border + "│" + reset + cell_style).join(padded)
            + reset + border + "│" + reset
        )

    def _start_table(self, header, alignments, out):
        requested = max(1, len(alignments))
        max_columns = max(1, (self._width() - 3) // 2)
        count = min(requested, max_columns)
        header = list(header[:count])
        if requested > count and header:
            header[-1] = "…"
        self._table_alignments = tuple(alignments[:count])
        # 整表缓冲：列宽要看完全部行才定得下来。以前按表头等分整屏宽度，
        # 后面的长单元格只能截成省略号（用户 2026-09-04 截图）。
        self._table_count = count
        self._table_rows = [header]
        self._table_widths = ()
        self._in_table = True

    def _close_table(self, out):
        if not self._in_table:
            return
        rows = self._table_rows
        self._table_widths = self._table_column_widths(rows, self._table_count)
        out.append(self._table_border("┌", "┬", "┐") + "\n")
        for index, cells in enumerate(rows):
            out.append(self._table_row(cells, header=index == 0) + "\n")
            if index == 0:
                out.append(self._table_border("├", "┼", "┤") + "\n")
        out.append(self._table_border("└", "┴", "┘") + "\n")
        self._in_table = False
        self._table_alignments = ()
        self._table_widths = ()
        self._table_rows = []
        self._table_count = 0

    def _render_plain_line(self, value, out):
        for char in value:
            self._feed_inline_char(char, out)
        out.append(self._close_inline())
        out.append("\n")
        self._last_visible_char = "\n"

    def _complete_normal_line(self, value, out, *, allow_table=True):
        value = value.rstrip("\r")
        if self._pending_table_header is not None:
            header = self._pending_table_header
            self._pending_table_header = None
            alignments = self._table_separator(value)
            if alignments is not None:
                cells = self._split_table_row(header) or []
                self._start_table(cells, alignments, out)
                return
            self._render_plain_line(header, out)
            self._complete_normal_line(value, out, allow_table=allow_table)
            return

        if self._in_table:
            cells = self._split_table_row(value)
            if cells is not None and value.strip() != "|":
                self._table_rows.append(cells)
                return
            self._close_table(out)
            self._complete_normal_line(value, out, allow_table=allow_table)
            return

        fence = self._fence(value)
        if fence is not None:
            marker, label = fence
            out.append(self._close_inline())
            self._in_fence = True
            self._fence_char = marker[0]
            self._fence_len = len(marker)
            self._fence_label = label
            self._highlighter = _SyntaxHighlighter(
                label, self.theme,
                styled=self.styled and self.syntax_highlight)
            out.append(self._fence_line("┌─", label) + "\n")
            return

        if allow_table and value.lstrip().startswith("|"):
            self._pending_table_header = value
            return

        indent = self._leading_spaces(value)
        body = value[indent:]
        if re.fullmatch(r"(?:\s*[-*_]){3,}\s*", body):
            width = max(3, min(72, self._width() - 2))
            out.append(
                self._style(self.theme.rule)
                + "  " + "─" * width + self._style(self.RESET) + "\n")
            return

        heading = re.fullmatch(
            r"(#{1,6})[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?", body)
        if heading:
            level = len(heading.group(1))
            markers = {1: "◆", 2: "◇", 3: "▸"}
            marker = markers.get(level, "·")
            style = (
                self.theme.heading1 if level == 1
                else self.theme.heading2 if level == 2
                else self.theme.heading3)
            prefix = " " * indent + "  " + marker + " "
            self._render_prefixed_block(
                prefix, " " * display_width(prefix), heading.group(2), out,
                prefix_style=style, body_style=style + self.BOLD)
            return

        quote = re.fullmatch(r"((?:>[ \t]*)+)(.*)", body)
        if quote:
            depth = quote.group(1).count(">")
            prefix = " " * indent + "  " + "│ " * depth
            self._render_prefixed_block(
                prefix, prefix, quote.group(2), out,
                prefix_style=self.theme.quote,
                body_style=self.theme.quote)
            return

        task = re.fullmatch(r"([-+*])[ \t]+\[([ xX])\][ \t]*(.*)", body)
        bullet = re.fullmatch(r"([-+*])[ \t]+(.*)", body)
        ordered = re.fullmatch(r"(\d{1,9})[.)][ \t]+(.*)", body)
        if task or bullet or ordered:
            depth = min(8, indent // 2)
            if task:
                marker = "☑" if task.group(2).lower() == "x" else "☐"
                content = task.group(3)
                marker_style = (
                    self.theme.success if marker == "☑"
                    else self.theme.bullet)
            elif ordered:
                marker = ordered.group(1) + "."
                content = ordered.group(2)
                marker_style = self.theme.bullet
            else:
                marker = ("•", "◦", "▪")[depth % 3]
                content = bullet.group(2)
                marker_style = self.theme.bullet
            prefix = self._list_prefix(indent, marker)
            continuation = " " * display_width(prefix)
            self._render_prefixed_block(
                prefix, continuation, content, out,
                prefix_style=marker_style)
            return

        self._render_plain_line(value, out)

    def _fence_line(self, edge, label=""):
        line = f"  {edge}"
        if label:
            line += f" {label}"
        # ANSI 不占列，先按纯文本截断，再施加样式。
        line = truncate_display(line, self._width())
        return (
            self._style(self.FENCE)
            + line
            + self._style(self.RESET)
        )

    def _list_prefix(self, indent, marker):
        prefix = " " * indent + "  " + marker + " "
        if display_width(prefix) >= self._width():
            prefix = truncate_display(prefix, self._width())
        return prefix

    def _code_prefix(self):
        prefix = "  │ "
        if display_width(prefix) >= self._width():
            prefix = truncate_display(prefix, self._width())
        return prefix

    def _flush_normal_start(self, out):
        value = self._line_buffer
        self._line_buffer = ""
        self._at_line_start = False
        for char in value:
            self._feed_inline_char(char, out)

    def _normal_start(self, char, out):
        self._line_buffer += char
        if char == "\n":
            value = self._line_buffer[:-1]
            self._line_buffer = ""
            self._complete_normal_line(value, out)
            self._at_line_start = True
            return

        value = self._line_buffer
        if len(value) > 65_536:
            if self._pending_table_header is not None:
                header = self._pending_table_header
                self._pending_table_header = None
                self._render_plain_line(header, out)
            self._close_table(out)
            self._flush_normal_start(out)
            return
        if self._pending_table_header is not None or self._in_table:
            return
        indent = self._leading_spaces(value)
        if indent == len(value):
            if indent <= 32:
                return
            self._flush_normal_start(out)
            return
        if indent > 32:
            self._flush_normal_start(out)
            return
        body = value[indent:]
        first = body[0]
        if first in "`~":
            if all(item == first for item in body) and len(body) < 3:
                return
            if body.startswith(first * 3):
                return                         # fence 要等到行尾确认 label
            self._flush_normal_start(out)
            return
        if first in ">|":
            return
        if first == "#":
            if (re.fullmatch(r"#{1,6}", body)
                    or re.match(r"^#{1,6}[ \t]", body)):
                return
            self._flush_normal_start(out)
            return
        if first in "-*+":
            list_prefix = re.match(r"^[-*+][ \t]", body)
            possible_rule = (
                first in "-*"
                and all(item in (first, " ", "\t") for item in body))
            if len(body) == 1 or list_prefix or possible_rule:
                return
            self._flush_normal_start(out)
            return
        if first == "_":
            if all(item in ("_", " ", "\t") for item in body):
                return
            self._flush_normal_start(out)
            return
        if first.isdigit():
            if re.fullmatch(r"\d{1,9}", body):
                return
            if re.fullmatch(r"\d{1,9}[.)]", body):
                return
            if re.match(r"^\d{1,9}[.)][ \t]", body):
                return
        self._flush_normal_start(out)

    def _open_code_line(self, out):
        value = self._line_buffer
        self._line_buffer = ""
        prefix = (
            "" if self._code_line_open
            else self._style(self.FENCE) + self._code_prefix())
        code = (
            self._style(self.CODE)
            if self._code_line_open
            else self._style(self.RESET + self.CODE))
        highlighted = (
            self._highlighter.highlight(value)
            if self._highlighter is not None else value)
        out.append(prefix + code + highlighted + self._style(self.RESET))
        self._at_line_start = True
        self._code_line_open = True

    def _is_closing_fence(self, value):
        value = value.rstrip("\r")
        indent = self._leading_spaces(value)
        if indent > 3:
            return False
        body = value[indent:]
        run = len(body) - len(body.lstrip(self._fence_char))
        return (
            run >= self._fence_len
            and not body[run:].strip()
        )

    def _code_start(self, char, out):
        self._line_buffer += char
        if char != "\n":
            if len(self._line_buffer) >= self.CODE_BUFFER_LIMIT:
                self._open_code_line(out)
            return
        value = self._line_buffer[:-1]
        self._line_buffer = ""
        if not self._code_line_open and self._is_closing_fence(value):
            out.append(self._fence_line("└─") + "\n")
            self._in_fence = False
            self._fence_char = None
            self._fence_len = 0
            self._fence_label = ""
            self._highlighter = None
        else:
            self._line_buffer = value
            if value:
                self._open_code_line(out)
            out.append(self._style(self.RESET) + "\n")
        self._at_line_start = True
        self._code_line_open = False

    def _consume(self, text, out):
        for char in text:
            if self._in_fence:
                self._code_start(char, out)
            elif self._at_line_start:
                self._normal_start(char, out)
            elif char == "\n":
                out.append(self._close_inline())
                out.append("\n")
                self._last_visible_char = "\n"
                self._at_line_start = True
            else:
                self._feed_inline_char(char, out)

    def feed(self, text):
        out = self._host_first_line(self._feed_inner(text))
        if self._hanging is not None:
            out = self._hanging.push(out)
        return out

    def finish(self):
        out = self._host_first_line(self._finish_inner())
        if self._hanging is not None:
            out = self._hanging.push(out) + self._hanging.flush()
            self._hanging.reset()
        self._first_line_pending = self._icon_hosted   # 下一条 assistant 消息又有图标
        return out

    def _host_first_line(self, rendered):
        """SPEC-CC-parity C6：首行前面是 "⏺ "（两列），结构化行自带的两格缩进要去掉，
        否则标题/围栏/列表在第一行会比后续行多缩两格。"""
        if not self._first_line_pending or not rendered:
            return rendered
        if not _SGR_RE.sub("", rendered):
            return rendered                      # 只有样式码：第一行还没开始
        self._first_line_pending = False
        m = _LEADING_STRUCTURAL_INDENT.match(rendered)
        if m:
            rendered = m.group(1) + rendered[m.end():]
        return rendered

    def _feed_inner(self, text):
        text = (
            self._sanitizer.feed(text)
            if self.sanitize else str(text))
        if not text:
            return ""
        out = []
        resumed = self._resume_style()
        if resumed:
            out.append(resumed)
        self._consume(text, out)
        if self._resume_style() and out:
            # TerminalRenderer 会在 chunk 之间重绘 composer；显式 reset，
            # 下一次 feed() 再按 parser state 恢复，避免样式污染输入框。
            out.append(self._style(self.RESET))
        return "".join(out)

    def _finish_inner(self):
        """冲出歧义后缀并复位；同一实例可继续渲染下一条 assistant 消息。"""
        out = []
        sanitizer_tail = self._sanitizer.finish() if self.sanitize else ""
        resumed = self._resume_style()
        if resumed:
            out.append(resumed)
        if sanitizer_tail:
            self._consume(sanitizer_tail, out)
        body = []
        if self._line_buffer:
            if self._in_fence:
                if (not self._code_line_open
                        and self._is_closing_fence(self._line_buffer)):
                    body.append(self._fence_line("└─") + "\n")
                    self._in_fence = False
                else:
                    self._open_code_line(body)
                    body.append(self._style(self.RESET) + "\n")
            else:
                value = self._line_buffer
                self._line_buffer = ""
                self._complete_normal_line(
                    value, body, allow_table=False)
        if self._pending_table_header is not None:
            value = self._pending_table_header
            self._pending_table_header = None
            self._render_plain_line(value, body)
        self._close_table(body)
        if self._in_fence:
            if self._code_line_open and not body:
                body.append(self._style(self.RESET) + "\n")
            body.append(self._fence_line("└─") + "\n")
            self._in_fence = False
        body.append(self._close_inline())
        rendered_body = "".join(body)
        if rendered_body.endswith("\n"):
            rendered_body = rendered_body[:-1]
        out.append(rendered_body)
        self._reset_state()
        return "".join(out)


def _wrap_control_lines(text, columns, *, max_lines=2):
    """Wrap a picker key legend at semantic separators, with a hard row cap."""
    columns = max(1, int(columns))
    max_lines = max(1, int(max_lines))
    chunks = [
        chunk.strip()
        for chunk in " ".join(str(text).splitlines()).split(" · ")
        if chunk.strip()
    ]
    lines = []
    while chunks and len(lines) < max_lines:
        if len(lines) == max_lines - 1:
            lines.append(truncate_display(" · ".join(chunks), columns))
            break
        current = chunks.pop(0)
        while chunks:
            candidate = f"{current} · {chunks[0]}"
            if display_width(candidate) > columns:
                break
            current = candidate
            chunks.pop(0)
        lines.append(truncate_display(current, columns))
    return tuple(lines)


def _picker_control_line_limit(terminal_lines=None):
    """Keep title, state, one option and metadata visible in very short TTYs."""
    height = _lines() if terminal_lines is None else int(terminal_lines)
    return max(1, min(2, height - 4))


def _w(s):
    sys.stdout.write(s)


def _flush():
    sys.stdout.flush()


# ------------------------------------------------------------------ 列表选择器

def select(rows, title="", render=str, page=12, allow_filter=True):
    """上下键选择器，带增量筛选。返回选中项，取消返回 None。

    rows 可以很长（模型有 171 个），所以只渲染一个窗口，跟着光标滚动。
    """
    if not supported() or not rows:
        return None
    items = list(rows)
    filt, idx, top = "", 0, 0
    drawn = 0

    def view():
        if not filt:
            return items
        f = filt.lower()
        return [r for r in items if f in render(r).lower()]

    with raw_mode():
        try:
            while True:
                cur = view()
                if not cur:
                    idx = top = 0
                elif idx >= len(cur):
                    idx = len(cur) - 1
                if idx < top:
                    top = idx
                elif idx >= top + page:
                    top = idx - page + 1

                # 擦掉上一次画的内容
                if drawn:
                    _w(f"{CSI}{drawn}A")
                _w(f"\r{CSI}J")

                head = f"  {title}" if title else "  选择"
                if allow_filter:
                    head += f"  {'筛选: ' + filt if filt else '（直接打字筛选）'}"
                head += f"  [{len(cur)}]"
                _w(head + "\r\n")
                window = cur[top:top + page]
                for i, r in enumerate(window):
                    sel = (top + i) == idx
                    mark = "\x1b[7m>" if sel else " "
                    end = "\x1b[0m" if sel else ""
                    _w(f"{mark} {truncate_display(render(r), _cols() - 4)}{end}\r\n")
                foot = "  ↑↓ 选择 · Enter 确认 · Esc 取消"
                if len(cur) > page:
                    foot += f" · {top+1}-{min(top+page,len(cur))}/{len(cur)}"
                _w(foot + "\r\n")
                drawn = len(window) + 2
                _flush()

                k = read_key()
                if k in ("esc", "ctrl-c"):
                    return None
                if k == "enter":
                    return cur[idx] if cur else None
                if k == "up":
                    idx = max(0, idx - 1)
                elif k == "down":
                    idx = min(len(cur) - 1, idx + 1) if cur else 0
                elif k == "backspace":
                    filt = filt[:-1]
                    idx = 0
                elif k == "ctrl-u":
                    filt, idx = "", 0
                elif allow_filter and isinstance(k, str) and len(k) == 1 and k.isprintable():
                    filt += k
                    idx = 0
        finally:
            if drawn:
                _w(f"{CSI}{drawn}A\r{CSI}J")
                _flush()


# ------------------------------------------------------------------ 带面板的输入行

def read_line(prompt, commands=None, history=None):
    """行输入。打 `/` 时在下方弹出命令面板，上下键选、Tab/Enter 补全。

    commands: {命令: 说明}。history: 可上下翻的历史列表（最新在末尾）。
    返回输入的字符串；Ctrl-D 抛 EOFError；Ctrl-C 抛 KeyboardInterrupt。
    """
    if not supported():
        return input(prompt)
    commands = commands or {}
    hist = list(history or [])
    buf, cur = "", 0
    hidx = len(hist)
    menu_i, menu_n, menu_top = 0, 0, 0
    PAGE = 8                       # 面板一次显示几行
    reserved = 0                   # 已在输入行下方预留出的行数

    def matches():
        if not buf.startswith("/") or " " in buf:
            return []
        f = buf[1:].lower()
        return [(c, d) for c, d in sorted(commands.items()) if c[1:].lower().startswith(f)]

    with raw_mode():
        try:
            while True:
                ms = matches()
                if menu_i >= len(ms):
                    menu_i = 0
                # 窗口跟着光标滚动 —— 原来硬切 ms[:8]，方向键只能在前 8 个里
                # 移动，第 9 个往后的命令除非打字筛选否则永远选不到。
                if menu_i < menu_top:
                    menu_top = menu_i
                elif menu_i >= menu_top + PAGE:
                    menu_top = menu_i - PAGE + 1
                if menu_top > max(0, len(ms) - PAGE):
                    menu_top = max(0, len(ms) - PAGE)
                shown = ms[menu_top:menu_top + PAGE]
                need = len(shown) + (1 if len(ms) > PAGE else 0)

                # **先把空间预留出来**。菜单画到屏幕底部时终端会向上滚，
                # 而重绘依赖「向上 N 行」回到输入行 —— 滚动一次这个位移就错位，
                # 于是每次重绘的 prompt+buf 落在下一行，屏幕上留下一串重复的
                # 「/」。做法：主动打 N 个换行逼终端滚完，再原路移回来；
                # 此后输入行下方保证有 N 行可用，位移才是可靠的。
                if need > reserved:
                    extra = need - reserved
                    _w("\n" * extra)
                    _w(f"{CSI}{extra}A")
                    reserved = need

                _w(f"\r{CSI}J")
                _w(prompt + buf)
                if shown:
                    _w("\r\n")
                    for i, (c, d) in enumerate(shown):
                        sel = (menu_top + i) == menu_i
                        pre = "\x1b[7m>" if sel else " "
                        end = "\x1b[0m" if sel else ""
                        desc = truncate_display(d, max(20, _cols() - 24))
                        _w(f"{pre} {c:<12} \x1b[2m{desc}\x1b[0m{end}\r\n")
                    if len(ms) > PAGE:
                        _w(f"  \x1b[2m{menu_top+1}-{menu_top+len(shown)}/{len(ms)}"
                           f"  ↑↓ 滚动\x1b[0m\r\n")
                    # 光标回到输入行的正确列：提示符宽度 + 光标左侧文本的宽度。
                    # **上移的行数必须等于刚才下移的行数**：菜单写了
                    # 1（换行到菜单区）+ len(shown) 行 + 位置指示条（若有），
                    # 即 need + 1 行。此前写死 len(shown)+1，漏掉了位置条那行，
                    # 于是每次重绘下移一格，旧输入行留在上面 —— 症状就是
                    # 一上下滚就出现一串重复的「› /」。位置条只在命令数超过
                    # 一页时才有，所以只有滚动场景会触发，很容易看漏。
                    col = display_width(prompt) + display_width(buf[:cur])
                    _w(f"{CSI}{need + 1}A\r" + (f"{CSI}{col}C" if col else ""))
                else:
                    col = display_width(prompt) + display_width(buf[:cur])
                    _w("\r" + (f"{CSI}{col}C" if col else ""))
                menu_n = len(shown) + (1 if len(ms) > PAGE else 0)
                _flush()

                k = read_key()
                if k == "ctrl-c":
                    _w(f"\r{CSI}J")
                    _flush()
                    raise KeyboardInterrupt
                if k == "ctrl-d":
                    if not buf:
                        _w(f"\r{CSI}J")
                        _flush()
                        raise EOFError
                    continue
                if k == "enter":
                    if shown and menu_n:
                        # 面板开着时 Enter 先补全命令，不直接提交
                        buf = ms[menu_i][0] + " "
                        cur = len(buf)
                        continue
                    _w(f"\r{CSI}J" + prompt + buf + "\r\n")
                    _flush()
                    reserved = 0
                    return buf
                if k == "tab" and shown:
                    buf = ms[menu_i][0] + " "
                    cur = len(buf)
                elif k == "up":
                    if shown:
                        menu_i = max(0, menu_i - 1)
                    elif hidx > 0:
                        hidx -= 1
                        buf = hist[hidx]
                        cur = len(buf)
                elif k == "down":
                    if shown:
                        menu_i = min(len(ms) - 1, menu_i + 1)
                    elif hidx < len(hist):
                        hidx += 1
                        buf = hist[hidx] if hidx < len(hist) else ""
                        cur = len(buf)
                elif k == "left":
                    cur = max(0, cur - 1)
                elif k == "right":
                    cur = min(len(buf), cur + 1)
                elif k in ("home", "ctrl-a"):
                    cur = 0
                elif k == "end":
                    cur = len(buf)
                elif k == "backspace":
                    if cur:
                        buf = buf[:cur-1] + buf[cur:]
                        cur -= 1
                elif k == "delete":
                    buf = buf[:cur] + buf[cur+1:]
                elif k == "ctrl-u":
                    buf, cur = buf[cur:], 0
                elif k == "ctrl-k":
                    buf = buf[:cur]
                elif k == "ctrl-w":
                    left = buf[:cur].rstrip()
                    p = left.rfind(" ") + 1
                    buf = buf[:p] + buf[cur:]
                    cur = p
                elif k == "esc":
                    if shown:
                        buf, cur = "", 0     # 关掉面板
                elif isinstance(k, PasteEvent):
                    # read_line 目前没有调用方（LineEditor 取代了它），但留着
                    # 同样的处理，免得日后被复活时带着同一个 bug 回来。
                    buf = buf[:cur] + k.text + buf[cur:]
                    cur += len(k.text)
                elif isinstance(k, str) and len(k) == 1 and k.isprintable():
                    buf = buf[:cur] + k + buf[cur:]
                    cur += 1
        finally:
            # 清掉输入行连同下方预留的空行，别把菜单残影留在屏幕上
            _w(f"\r{CSI}J")
            _flush()


# ------------------------------------------------------------------ 单一输入 owner

_FRAME_UNSET = object()


@dataclass(frozen=True)
class InputSnapshot:
    mode: str
    prompt: str = ""
    text: str = ""
    cursor: int = 0
    busy: bool = False
    active: bool = True
    options: tuple = ()
    queued: tuple = ()
    dock: tuple = ()
    status: str = ""
    activity: str = ""
    selected: int = 0
    title: str = ""
    hint: str = ""
    target: str | None = None
    controls: str = ""
    fullscreen: bool = False
    # Structured decision-gate metadata.  Ordinary pickers leave these empty;
    # keeping them on the snapshot lets the owner renderer show the selected
    # option's consequence/cost without making worker threads format output.
    modal_context: str = ""
    option_details: tuple = ()
    # v2 gate projection: the owner renderer can show question progress and
    # selected values without exposing mutable modal state to worker threads.
    modal_questions: tuple = ()
    modal_question_index: int = 0
    modal_selected: tuple = ()
    modal_error: str = ""


@dataclass(frozen=True)
class PumpEvent:
    kind: str
    text: str = ""
    mode: str | None = None
    value: object = None
    snapshot: InputSnapshot | None = None
    state: object = None


class LineEditor:
    """纯状态行编辑器；不读 fd，也不写终端。"""

    PAGE = 8
    EXIT_DOUBLE_PRESS_SECONDS = 1.5
    # SPEC-CC-parity A3：长粘贴在草稿里折叠成占位符，提交时原样展开
    PASTE_FOLD_MIN_LINES = 4
    PASTE_FOLD_MIN_CHARS = 600

    def __init__(self, *, prompt="› ", commands=None, subcommands=None,
                 history=None, cwd=None, live_commands=None):
        self.prompt = prompt
        self.commands = dict(commands or {})
        # 模型工作期间仍可当场执行的命令（显示名，如 "/usage"）。名单由主 CLI
        # 拥有（zylab.LIVE_COMMANDS），这里只当 prompt 数据用 —— tui 不该
        # 反向 import 主程序，也不该自己判断什么命令"安全"。
        # 空集合 = 忙时不弹菜单，即本参数出现之前的行为。
        self.live_commands = frozenset(live_commands or ())
        # Optional second-level command catalog.  The main CLI owns the
        # catalog and passes it in as prompt data; keeping it out of the
        # command parser means custom commands and non-interactive callers
        # remain fully backwards compatible.
        self.subcommands = self._normalize_subcommands(subcommands)
        # $skill-name 点名补全。skill 的调用是模型侧行为（读 SKILL.md），
        # 这里只负责让用户能把名字准确打出来 —— 打错名字等于没点名。
        self.skills = {}
        self.cwd = os.path.abspath(str(cwd or os.getcwd()))
        initial_history = [item for item in (history or []) if item]
        self.target = None
        self._histories = {None: initial_history}
        self.history = self._histories[None]
        self.text = ""
        self.cursor = 0
        self.history_index = len(self.history)
        # SPEC-CC-parity A6：Ctrl+R 反向增量搜索。None = 不在搜索；否则是
        # {"query", "index", "saved_text", "saved_cursor"}。
        self._search = None
        self._ctrl_c_at = 0.0            # A9：上一次空草稿 Ctrl+C 的时刻
        self._pastes = []                # A3：折叠起来的粘贴原文，按 #n 编号
        # History recall is navigation rather than fresh slash-command input.
        # Keep the menu closed until the user edits the recalled text or
        # explicitly asks for completion with Tab; otherwise the next Up/Down
        # is captured by the menu and history browsing gets stuck on entries
        # such as `/help`.
        self._history_browsing = False
        self.menu_index = 0
        self.busy = False
        self.active = True

    @staticmethod
    def _normalize_option_rows(value):
        """Normalize one level of ``(name, description)`` prompt data.

        A small amount of validation here is intentional: descriptions are
        user-visible and command names are later inserted into the draft.  A
        malformed/custom catalog must never inject whitespace or terminal
        control sequences into the editor state.
        """
        if isinstance(value, dict):
            # A mapping is a convenient shorthand: ``{"on": "启用"}``.
            # Structured specs use ``items`` (or ``options``) explicitly.
            if "items" in value or "options" in value:
                value = value.get("items") or value.get("options") or ()
            else:
                value = value.items()
        try:
            iterator = iter(value)
        except TypeError:
            return ()
        rows = []
        for raw_row in iterator:
            if isinstance(raw_row, dict):
                name = raw_row.get("name") or raw_row.get("token")
                description = raw_row.get("description")
            else:
                try:
                    name, description = raw_row
                except (TypeError, ValueError):
                    continue
            name = str(name or "").strip().lstrip("/")
            # Parameter names may be flags (for example ``--rounds``), but
            # never contain whitespace/control characters or shell syntax.
            valid_name = (
                _OPTION_NAME.fullmatch(name)
                or re.fullmatch(r"--[A-Za-z0-9][A-Za-z0-9_-]{0,61}", name))
            if valid_name is None:
                continue
            description = str(description or "").strip()
            rows.append((name, description[:240]))
            if len(rows) >= 64:
                break
        return tuple(rows)

    @staticmethod
    def _normalize_subcommands(value):
        """Normalize a bounded command catalog with one nested parameter level.

        The public shape stays compatible with the original second-level
        catalog::

            {"/command": {"items": (("sub", "..."), ...),
                           "params": {"sub": {"items": (("arg", "..."), ...),
                                               "hint": "..."}}}}

        ``params`` is intentionally only one level deep.  That is enough for
        a command's third-level enum/flag values while keeping arbitrary
        free-form arguments out of the completion parser.
        """
        if not isinstance(value, dict):
            return {}
        normalized = {}
        for raw_command, raw_spec in value.items():
            command = str(raw_command or "").strip().lower()
            if not command.startswith("/") or re.fullmatch(
                    r"/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", command) is None:
                continue

            hint = ""
            rows_value = raw_spec
            params_value = {}
            if isinstance(raw_spec, dict):
                hint = str(raw_spec.get("hint") or "")[:160]
                rows_value = raw_spec.get("items")
                if rows_value is None:
                    rows_value = raw_spec.get("options")
                if rows_value is None and not any(
                        key in raw_spec for key in ("hint", "params", "children")):
                    # Accept the shorthand ``{"status": "查看状态"}``.
                    rows_value = raw_spec
                params_value = (
                    raw_spec.get("params")
                    or raw_spec.get("children")
                    or {})
            rows = LineEditor._normalize_option_rows(rows_value)
            if not rows:
                continue

            params = {}
            if isinstance(params_value, dict):
                known_names = {name.casefold() for name, _ in rows}
                for raw_parent, raw_child_spec in params_value.items():
                    parent = str(raw_parent or "").strip().lstrip("/")
                    if (parent.casefold() not in known_names
                            or not parent):
                        continue
                    child_hint = ""
                    child_rows_value = raw_child_spec
                    if isinstance(raw_child_spec, dict):
                        child_hint = str(
                            raw_child_spec.get("hint") or "")[:160]
                        child_rows_value = raw_child_spec.get("items")
                        if child_rows_value is None:
                            child_rows_value = raw_child_spec.get("options")
                        if child_rows_value is None and not any(
                                key in raw_child_spec
                                for key in ("hint", "params", "children")):
                            child_rows_value = raw_child_spec
                    child_rows = LineEditor._normalize_option_rows(
                        child_rows_value)
                    if child_rows:
                        params[parent.casefold()] = {
                            "items": child_rows,
                            "hint": child_hint,
                        }
            normalized[command] = {
                "items": rows,
                "hint": hint,
                "params": params,
            }
        return normalized

    def set_busy(self, busy):
        """忙/闲切换时命令集会变（忙时只剩 live 白名单），光标回顶；**同状态刷新不动**。
        模型工作时 spinner 每秒都会 configure(busy=True)，以前每次都把菜单打回第一行，
        用户在 steer 里打开 / 列表根本选不到下面的项（2026-09-04 反馈）。"""
        busy = bool(busy)
        if busy != self.busy:
            self.menu_index = 0
        self.busy = busy
        self.active = True
        return self.snapshot()

    def set_prompt(self, prompt):
        self.prompt = str(prompt)
        return self.snapshot()

    def set_commands(self, commands):
        commands = dict(commands or {})
        if commands != self.commands:
            self.menu_index = 0
        self.commands = commands
        return self.snapshot()

    def set_subcommands(self, subcommands, *, reset_menu=True):
        self.subcommands = self._normalize_subcommands(subcommands)
        if reset_menu:
            self.menu_index = 0
        return self.snapshot()

    def set_skills(self, skills):
        """{name: description}，用于 $ 点名补全。"""
        skills = dict(skills or {})
        if skills != self.skills:
            self.menu_index = 0
        self.skills = skills
        return self.snapshot()

    def set_cwd(self, cwd):
        cwd = os.path.abspath(str(cwd or os.getcwd()))
        if cwd != self.cwd:
            self.menu_index = 0
        self.cwd = cwd
        return self.snapshot()

    def set_target(self, target, *, history=_FRAME_UNSET):
        """切换输入目标，并为每个目标保留独立的历史游标。"""
        target = None if target is None else str(target)
        if history is not _FRAME_UNSET:
            self._histories[target] = [
                item for item in (history or []) if item]
        elif target not in self._histories:
            self._histories[target] = []
        self.target = target
        self.history = self._histories[target]
        self.history_index = len(self.history)
        self._history_browsing = False
        self.menu_index = 0
        return self.snapshot()

    def add_history(self, text):
        text = str(text)
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.history_index = len(self.history)
        self._history_browsing = False

    def _leave_history_browsing(self):
        self._history_browsing = False
        self.history_index = len(self.history)

    def replace(self, text, *, active=True):
        self.text = str(text)
        self.cursor = len(self.text)
        self.active = bool(active)
        self._leave_history_browsing()
        self.menu_index = 0
        return self.snapshot()

    def set_cursor(self, cursor):
        """Move the insertion point without changing the draft contents."""
        self.cursor = min(max(0, int(cursor)), len(self.text))
        self._leave_history_browsing()
        self.menu_index = 0
        return self.snapshot()

    def _dollar_matches(self):
        """`$` 起头的 token 补 skill 名。

        与 `@`（文件）、`/`（命令）并列的第三种前缀。`$` 选它是因为
        codex 用的就是这个，且它在 shell 里的含义（变量展开）不会出现在
        自然语言 prompt 的词首。
        """
        if self._history_browsing or self.cursor != len(self.text):
            return []
        match = re.search(r"(?<!\S)\$([A-Za-z0-9_-]*)$", self.text[:self.cursor])
        if match is None:
            return []
        needle = match.group(1).lower()
        token_start = match.start()
        rows = []
        for name, description in sorted(self.skills.items()):
            if not name.lower().startswith(needle):
                continue
            rows.append((self.text[:token_start] + "$" + name,
                         str(description or "")[:72]))
        return rows

    def _at_matches(self):
        if self._history_browsing or self.cursor != len(self.text):
            return []
        match = re.search(
            r"(?<!\S)@(?:\"([^\"\n]*)|'([^'\n]*)|([^\s]*))$",
            self.text[:self.cursor])
        if match is None:
            return []
        typed = next(
            group for group in match.groups() if group is not None)
        expanded = os.path.expanduser(typed)
        path = expanded if os.path.isabs(expanded) else os.path.join(
            self.cwd, expanded)
        parent, prefix = (
            (path, "") if typed.endswith(("/", os.sep))
            else (os.path.dirname(path) or self.cwd, os.path.basename(path)))
        try:
            resolved_parent = os.path.realpath(parent)
            if paths.is_protected(resolved_parent):
                return []
            entries = []
            with os.scandir(parent) as iterator:
                for scanned, entry in enumerate(iterator):
                    if scanned >= 2_000:
                        break
                    if entry.name.startswith(prefix):
                        entries.append(entry)
            entries.sort(key=lambda row: row.name.casefold())
        except OSError:
            return []
        rows = []
        token_start = match.start()
        for entry in entries:
            if len(rows) >= 100:
                break
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if not (is_dir or is_file):
                continue
            resolved_entry = os.path.realpath(entry.path)
            if paths.is_protected(resolved_entry):
                continue
            if typed.endswith(("/", os.sep)):
                shown = typed + entry.name
            elif "/" in typed:
                shown = typed.rsplit("/", 1)[0] + "/" + entry.name
            else:
                shown = entry.name
            if is_dir:
                shown += "/"
            if " " in shown:
                # 目录保持开放引号，下一次 Tab 才能继续扫描其子项；
                # 最终文件补全再闭合，引号内空格不会被误当成 prompt 分隔。
                token = f'@"{shown}' if is_dir else f'@"{shown}"'
            else:
                token = "@" + shown
            replacement = self.text[:token_start] + token
            description = "directory" if is_dir else "file"
            rows.append((replacement, description))
        return rows

    def _subcommand_matches(self):
        """Return guided completion rows after an exact slash command.

        Suggestions begin after a separator (``/goal ``), so a bare command
        retains its established action (for example, bare ``/model`` still
        opens the model picker and bare ``/goal`` still shows status).  Once a
        known subcommand is followed by a separator, a ``params`` catalog can
        provide a third-level enum/flag menu (``/workflow auto on``).  If no
        such catalog exists, or after a completed parameter, the command's
        free-form argument can be typed normally.  Unknown first words
        likewise remain ordinary command arguments, which is important for
        ``/goal <user objective>``.
        """
        if (self.target is not None or self.busy or self._history_browsing
                or self.cursor != len(self.text)
                or not self.text.startswith("/")
                or "\n" in self.text):
            return []
        match = re.match(r"^(\S+)([ \t]+(.*))?$", self.text, re.S)
        if match is None or match.group(2) is None:
            return []
        command_token = match.group(1)
        spec = self.subcommands.get(command_token.casefold())
        if not spec:
            return []
        tail = match.group(3) or ""
        stripped = tail.lstrip(" \t")
        tokens = re.findall(r"\S+", stripped)
        trailing_separator = bool(tail and tail[-1] in " \t")
        items = tuple(spec.get("items") or ())

        def exact_row(rows, token):
            token = str(token or "").casefold()
            return next(
                (row for row in rows if row[0].casefold() == token), None)

        def guided_rows(rows, prefix, needle, *, level, hint):
            needle = str(needle or "").casefold()
            result = [
                (f"{prefix} {name}", description)
                for name, description in rows
                if name.casefold().startswith(needle)
            ]
            if result:
                self._match_kind = "subcommand"
                self._match_level = level
                self._match_hint = hint or ""
            return result

        # No token after the command: show the second-level command list.
        if not tokens:
            return guided_rows(
                items, command_token, "", level=2,
                hint=spec.get("hint") or "")

        parent_token = tokens[0]
        parent_row = exact_row(items, parent_token)
        params = spec.get("params") or {}

        # A single partial/exact token is still the second-level menu unless
        # the user typed a separator after an exact parent that has parameter
        # metadata.  In that case immediately descend to level three.
        if len(tokens) == 1:
            if trailing_separator and parent_row is not None:
                child = params.get(parent_row[0].casefold())
                if child:
                    return guided_rows(
                        child.get("items") or (),
                        f"{command_token} {parent_row[0]}", "", level=3,
                        hint=child.get("hint") or spec.get("hint") or "")
                # An exact second-level command with no parameter catalog is
                # complete; its following separator belongs to free-form
                # arguments and must not reopen the same menu.
                return []
            return guided_rows(
                items, command_token, parent_token, level=2,
                hint=spec.get("hint") or "")

        # More than one token means a third-level parameter candidate.  Only
        # an exact parent may descend; unknown/free-form words close the menu
        # rather than hijacking a user's argument text.
        if parent_row is None or len(tokens) > 2:
            return []
        child = params.get(parent_row[0].casefold())
        if not child:
            return []
        parameter_token = tokens[1]
        parameter_row = exact_row(child.get("items") or (), parameter_token)
        # A completed parameter followed by a separator is now ready for its
        # value (or any remaining free-form argument), so stop suggesting.
        if trailing_separator and parameter_row is not None:
            return []
        return guided_rows(
            child.get("items") or (),
            f"{command_token} {parent_row[0]}", parameter_token, level=3,
            hint=child.get("hint") or spec.get("hint") or "")

    def _matches(self):
        self._match_kind = None
        self._match_level = 0
        self._match_hint = ""
        skill_matches = self._dollar_matches()
        if skill_matches:
            return skill_matches
        attachment_matches = self._at_matches()
        if attachment_matches:
            return attachment_matches
        subcommand_matches = self._subcommand_matches()
        if subcommand_matches:
            return subcommand_matches
        if (self.target is not None or self._history_browsing
                or not self.text.startswith("/") or " " in self.text):
            return []
        if self.busy and not self.live_commands:
            return []
        needle = self.text[1:].lower()
        # 工作中只列当场执行的那些。其余命令并非不能用（它们会排到本轮结束），
        # 但此刻把它们混在一起会让"按下去会发生什么"变得不可预测 —— 菜单的
        # 用处正是回答这个问题。
        pool = (
            {name: text for name, text in self.commands.items()
             if name in self.live_commands}
            if self.busy else self.commands)
        return [
            (command, description)
            for command, description in sorted(pool.items())
            if command[1:].lower().startswith(needle)
        ]

    def _apply_match(self, value):
        self.text = str(value)
        if not self.text.endswith("/"):
            self.text += " "
        self.cursor = len(self.text)

    def snapshot(self):
        if self._search is not None:
            query = self._search["query"]
            found = self._search_find(len(self.history)) is not None or not query
            hint = (f"(reverse-i-search) '{query}'"
                    + (" · Enter 采用 · Esc 取消 · Ctrl+R 更旧" if found else " 无匹配"))
            return InputSnapshot(
                mode="line", prompt=self.prompt, text=self.text,
                cursor=self.cursor, busy=self.busy, active=self.active,
                options=(), selected=0, hint=hint, target=self.target)
        if (not self.text and self._ctrl_c_at
                and time.monotonic() - self._ctrl_c_at <= self.EXIT_DOUBLE_PRESS_SECONDS):
            return InputSnapshot(
                mode="line", prompt=self.prompt, text=self.text,
                cursor=self.cursor, busy=self.busy, active=self.active,
                options=(), selected=0,
                hint="再按一次 Ctrl+C 退出（Ctrl+D 也可）", target=self.target)
        matches = self._matches()
        if matches:
            self.menu_index = min(self.menu_index, len(matches) - 1)
            top = max(
                0, min(
                    self.menu_index,
                    max(0, len(matches) - self.PAGE)))
            if self.menu_index >= top + self.PAGE:
                top = self.menu_index - self.PAGE + 1
            shown = tuple(matches[top:top + self.PAGE])
            selected = self.menu_index - top
            hint = (
                f"{top + 1}-{top + len(shown)}/{len(matches)}  ↑↓ 滚动"
                if len(matches) > self.PAGE else "")
            if self._match_kind == "subcommand":
                label = (
                    "三级参数" if self._match_level >= 3 else "二级指令")
                suffix = f"{label} · ↑↓ 选择 · Enter/Tab 补全"
                if self._match_hint:
                    suffix += " · " + self._match_hint
                hint = suffix + ((" · " + hint) if hint else "")
        else:
            shown, selected, hint = (), 0, ""
        return InputSnapshot(
            mode="line", prompt=self.prompt, text=self.text,
            cursor=self.cursor, busy=self.busy, active=self.active,
            options=shown, selected=selected, hint=hint,
            target=self.target)

    def _redraw(self):
        return [PumpEvent("redraw", snapshot=self.snapshot())]

    @staticmethod
    def _paste_placeholder(index, text):
        return f"[Pasted text #{index} +{text.count(chr(10)) + 1} lines]"

    def expand_pastes(self, text):
        """把草稿里的 [Pasted text #n +N lines] 占位符换回原文。"""
        for index, original in enumerate(self._pastes, 1):
            text = text.replace(
                self._paste_placeholder(index, original), original)
        return text

    def _submit(self, mode):
        text = self.expand_pastes(self.text).strip()
        if not text:
            return self._redraw()
        self._pastes = []
        prompt = self.prompt
        if self.target is not None and text.startswith("/"):
            # attached composer 的 slash command 仍属于主 CLI；不能污染
            # child prompt history。持久 history 由 Session 在主线程追加。
            main_history = self._histories[None]
            if not main_history or main_history[-1] != text:
                main_history.append(text)
            self.history_index = len(self.history)
            self._history_browsing = False
        else:
            self.add_history(text)
        self.text, self.cursor = "", 0
        self.menu_index = 0
        self.active = True
        return [PumpEvent(
            "submit", text=text, mode=mode,
            value={"prompt": prompt})]

    @property
    def searching(self):
        return self._search is not None

    # ------------------------------------------------------ reverse-i-search
    def _search_start(self):
        self._search = {"query": "", "index": len(self.history),
                        "saved_text": self.text, "saved_cursor": self.cursor}
        if self._history_browsing:
            self._leave_history_browsing()
        return self._redraw()

    def _search_find(self, from_index):
        """从 from_index 往旧找包含 query 的记录；返回下标或 None。"""
        query = self._search["query"]
        if not query:
            return None
        for idx in range(min(from_index, len(self.history)) - 1, -1, -1):
            if query in self.history[idx]:
                return idx
        return None

    def _search_apply(self, from_index):
        found = self._search_find(from_index)
        if found is not None:
            self._search["index"] = found
            self.text = self.history[found]
            self.cursor = len(self.text)
        elif not self._search["query"]:
            self._search["index"] = len(self.history)
        return self._redraw()

    def _search_end(self, *, restore):
        state, self._search = self._search, None
        if restore:
            self.text, self.cursor = state["saved_text"], state["saved_cursor"]
        else:
            self.cursor = len(self.text)
        self.active = True
        self.menu_index = 0
        return self._redraw()

    def _handle_search(self, key):
        state = self._search
        if key == "ctrl-r":
            # 再按：跳到更旧的匹配；到底就停在原地
            return self._search_apply(state["index"])
        if key == "backspace":
            state["query"] = state["query"][:-1]
            return self._search_apply(len(self.history))
        if key in ("esc", "ctrl-c", "ctrl-g"):
            return self._search_end(restore=True)
        if key in ("enter", "tab"):
            return self._search_end(restore=False)
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            state["query"] += key
            return self._search_apply(len(self.history))
        # 其它键（方向、粘贴…）：采用当前匹配，再按普通键处理
        events = self._search_end(restore=False)
        return events + self.handle(key)

    def handle(self, key):
        if key != "ctrl-c":
            self._ctrl_c_at = 0.0    # A9：任何别的键都解除"再按一次退出"
        if self._search is not None:
            return self._handle_search(key)
        if key == "ctrl-r":
            return self._search_start()
        if key == "ctrl-b":
            # Ctrl+B 只声明“把当前工作转后台 / 退出任务视图”的意图。
            # LineEditor 不判断前台任务类型，也不触碰当前 composer；Session
            # 会根据运行态决定 background、detach 或响铃。
            return [PumpEvent("background")]
        if isinstance(key, PasteEvent):
            # 粘贴就是"一次插入很多字符"，**绝不提交**。终端把整块内容包在
            # ESC[200~/201~ 里交过来，其中的换行是文本的一部分，不是回车。
            if not key.text:
                return []
            if self._history_browsing:
                self._leave_history_browsing()
            text = key.text
            n_lines = text.count("\n") + 1
            if (n_lines >= self.PASTE_FOLD_MIN_LINES
                    or len(text) >= self.PASTE_FOLD_MIN_CHARS):
                self._pastes.append(text)
                text = self._paste_placeholder(len(self._pastes), text)
            self.text = (
                self.text[:self.cursor] + text + self.text[self.cursor:])
            self.cursor += len(text)
            self.menu_index = 0
            self.active = True
            return self._redraw()
        if key == "shift-enter":
            # A logical newline belongs to the draft. Ordinary Enter remains
            # the only submit action in idle, busy and attached composers.
            if self._history_browsing:
                self._leave_history_browsing()
            self.text = (
                self.text[:self.cursor] + "\n" + self.text[self.cursor:])
            self.cursor += 1
            self.menu_index = 0
            self.active = True
            return self._redraw()
        matches = self._matches()
        if key == "ctrl-t":
            return [PumpEvent("agents")]
        if key == "ctrl-o":
            # Claude Code 的 Ctrl+O：展开最近一段折叠的工具输出（没有工具输出时展开
            # 上一 turn 的思考）。空闲与工作中都可用，等价于 /expand。
            return [PumpEvent("expand_last")]
        if key == "shift-tab" and not self._matches():
            # Claude Code 的 Shift+Tab：循环权限模式（default → accept-edits → plan）。
            # 只在行模式且没有补全菜单时生效；picker / prompt / decision gate 各自
            # 先于本方法消费这个键，不受影响。模式改的是**未来**的审批，忙时也可按。
            return [PumpEvent("cycle_mode")]
        if key == "enter":
            if self.target is not None:
                return self._submit("agent")
            if (matches
                    and self.text != matches[self.menu_index][0]):
                self._apply_match(matches[self.menu_index][0])
                return self._redraw()
            return self._submit("steer" if self.busy else "next_turn")
        if key == "tab":
            if self.target is not None:
                return self._submit("agent")
            if self._history_browsing:
                # Tab is an explicit completion request, so it intentionally
                # leaves history navigation and re-enables slash matching.
                self._leave_history_browsing()
                matches = self._matches()
            if matches:
                self._apply_match(matches[self.menu_index][0])
            elif self.busy:
                return self._submit("next_turn")
            return self._redraw()
        if key in ("esc", "ctrl-c"):
            if self.target is not None:
                return [PumpEvent(
                    "detach", text=self.expand_pastes(self.text),
                    mode="agent")]
            if self.busy:
                return [PumpEvent("cancel")]
            # SPEC-CC-parity A9：Ctrl+C 清空草稿；空草稿时在 1.5s 内连按两次退出
            # （Esc 永远不退出）。第一次给一行提示，免得用户以为没反应。
            if key == "ctrl-c" and not self.text:
                now = time.monotonic()
                if now - self._ctrl_c_at <= self.EXIT_DOUBLE_PRESS_SECONDS:
                    self._ctrl_c_at = 0.0
                    return [PumpEvent("eof")]
                self._ctrl_c_at = now
                return [PumpEvent("redraw", snapshot=self.snapshot())]
            self._ctrl_c_at = 0.0
            self.text, self.cursor = "", 0
            self._pastes = []
            self._leave_history_browsing()
            self.menu_index = 0
            return [
                PumpEvent("interrupt"),
                PumpEvent("redraw", snapshot=self.snapshot()),
            ]
        if key == "ctrl-d":
            if not self.busy and not self.text:
                return [PumpEvent("eof")]
            return self._redraw()
        if key == "up":
            if matches:
                self.menu_index = max(0, self.menu_index - 1)
            elif self.target is None and self.busy:
                return [PumpEvent("retrieve")]
            elif self.history_index > 0:
                self.history_index -= 1
                self.text = self.history[self.history_index]
                self.cursor = len(self.text)
                self._history_browsing = True
            return self._redraw()
        if key == "down":
            if matches:
                self.menu_index = min(
                    len(matches) - 1, self.menu_index + 1)
            elif ((self.target is not None or not self.busy)
                  and self.history_index < len(self.history)):
                self.history_index += 1
                self.text = (
                    self.history[self.history_index]
                    if self.history_index < len(self.history) else "")
                self.cursor = len(self.text)
                self._history_browsing = (
                    self.history_index < len(self.history))
            return self._redraw()
        editing = (
            key in ("backspace", "delete", "ctrl-u", "ctrl-k", "ctrl-w")
            or (isinstance(key, str) and len(key) == 1 and key.isprintable()))
        if editing and self._history_browsing:
            self._leave_history_browsing()
        if key == "left":
            if self.target is not None and self.cursor == 0:
                return [PumpEvent(
                    "detach", text=self.expand_pastes(self.text),
                    mode="agent")]
            self.cursor = max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key in ("home", "ctrl-a"):
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.text)
        elif key == "backspace" and self.cursor:
            self.text = (
                self.text[:self.cursor - 1] + self.text[self.cursor:])
            self.cursor -= 1
        elif key == "delete":
            self.text = (
                self.text[:self.cursor] + self.text[self.cursor + 1:])
        elif key == "ctrl-u":
            self.text = self.text[self.cursor:]
            self.cursor = 0
        elif key == "ctrl-k":
            self.text = self.text[:self.cursor]
        elif key == "ctrl-w":
            left = self.text[:self.cursor].rstrip()
            start = left.rfind(" ") + 1
            self.text = self.text[:start] + self.text[self.cursor:]
            self.cursor = start
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.text = (
                self.text[:self.cursor] + key + self.text[self.cursor:])
            self.cursor += 1
            self.active = True
        return self._redraw()

class InputPump:
    """REPL 生命周期内唯一读取 stdin 的线程。"""

    def __init__(self, *, stream=None, prompt="› ", commands=None,
                 subcommands=None, history=None, key_reader=None, cwd=None,
                 live_commands=None, on_focus=None):
        self.stream = stream or sys.stdin
        # 焦点变化的订阅者（away recap）。在 pump 线程里直接回调、**不进事件队列**：
        # 选择器/提示框那几个消费循环会把不认识的事件延后重放，焦点信号一旦被
        # 延后就失去意义；走回调则没有任何消费循环需要认识它。
        self.on_focus = on_focus
        self.editor = LineEditor(
            prompt=prompt, commands=commands, subcommands=subcommands,
            live_commands=live_commands,
            history=history, cwd=cwd)
        self._key_reader = key_reader or read_key
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._cursor_condition = threading.Condition(self._lock)
        self._thread = None
        self._raw = None
        self._mode = "line"
        self._picker = None
        self._decision_gate = None
        self._prompt_editor = None
        self._queued = ()
        self._dock = ()
        self._status = ""
        self._activity = ""
        self._history_mode = False
        self._cursor_screen_row = None
        self._composer_selection_active = False

    def __enter__(self):
        if not self.stream.isatty():
            raise RuntimeError("InputPump 需要 TTY stdin")
        self._raw = raw_mode(
            self.stream, cbreak=True, capture_signals=True)
        self._raw.__enter__()
        # 只有 pump 消费焦点事件，所以 1004 跟着 pump 的生命周期走，不进
        # raw_mode 的引用计数（选择器、read_line 那些旧循环不需要它）。
        _set_focus_reporting(True)
        self._thread = threading.Thread(
            target=self._loop, name="zylab-input", daemon=True)
        with self._lock:
            initial = self._decorate_snapshot(self.editor.snapshot())
        # 首帧必须先于任何按键事件，避免快速输入被旧 snapshot 覆盖。
        self._events.put(PumpEvent("redraw", snapshot=initial))
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        # 退出时必须复原：否则 shell 里切窗口会冒出 ^[[I / ^[[O。
        _set_focus_reporting(False)
        if self._raw:
            self._raw.__exit__(*exc)

    @property
    def reader_ident(self):
        return self._thread.ident if self._thread else None

    @property
    def cursor_screen_row(self):
        """Last cursor row reported by the terminal's DSR response."""
        with self._lock:
            return self._cursor_screen_row

    def wait_cursor_screen_row(self, timeout=0.15):
        """Wait briefly for the response to the renderer's ``CSI 6n`` query."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cursor_condition:
            while self._cursor_screen_row is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cursor_condition.wait(remaining)
            return self._cursor_screen_row

    def _decorate_snapshot(self, snapshot):
        return dataclass_replace(
            snapshot, queued=self._queued, dock=self._dock,
            status=self._status, activity=self._activity)

    def _decorate_event(self, event):
        if event.snapshot is None:
            return event
        return dataclass_replace(
            event, snapshot=self._decorate_snapshot(event.snapshot))

    def _notify_focus(self, focused):
        callback = self.on_focus
        if callback is None:
            return
        try:
            callback(bool(focused))
        except Exception:                                   # noqa: BLE001
            pass        # 订阅者出错不能弄死 REPL 里唯一的输入线程

    def _loop(self):
        try:
            while not self._stop.is_set():
                key = self._key_reader(
                    timeout=0.05, stream=self.stream)
                if key is None:
                    continue
                if key in ("focus-in", "focus-out"):
                    # 焦点变化不是按键：先于一切模态分发拦截，选择器/决策门/
                    # 提示框都不该把它当输入。
                    self._notify_focus(key == "focus-in")
                    continue
                with self._lock:
                    if self._mode == "picker":
                        events = self._handle_picker(key)
                    elif self._mode == "decision_gate":
                        events = self._handle_decision_gate(key)
                    elif self._mode == "prompt":
                        events = self._handle_prompt(key)
                    else:
                        events = self._handle_line(key)
                    events = [self._decorate_event(event) for event in events]
                for event in events:
                    self._events.put(event)
        except BaseException as exc:
            self._events.put(PumpEvent("error", value=exc))

    def get(self, timeout=None):
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self):
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def set_history_mode(self, active):
        with self._lock:
            active = bool(active)
            if active != self._history_mode:
                self._cursor_screen_row = None
            self._history_mode = active

    def set_composer_selection_active(self, active):
        with self._lock:
            self._composer_selection_active = bool(active)

    def set_cursor(self, cursor, *, publish=True):
        with self._lock:
            self._composer_selection_active = False
            self._cursor_screen_row = None
            snapshot = self._decorate_snapshot(self.editor.set_cursor(cursor))
        if publish:
            self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return snapshot

    def _dock_click_event(self, mouse):
        if (self._cursor_screen_row is None
                or mouse.kind != "press" or mouse.button != 0):
            return None
        if mouse.x < 1 or mouse.x > _cols():
            return None
        dock = self._dock[:5]
        if not dock:
            return None
        snapshot = self.editor.snapshot()
        raw_text = str(snapshot.text)
        safe_text = sanitize_terminal_text(raw_text)
        safe_cursor = len(sanitize_terminal_text(
            raw_text[:snapshot.cursor]))
        _, cursor = TerminalRenderer._editor_rows(
            str(snapshot.prompt), safe_text, safe_cursor,
            max(4, max(20, _cols() - 1) - 4))
        cursor_frame_row = (
            len(self._queued) + len(dock) + 1 + cursor[0])
        frame_top = self._cursor_screen_row - cursor_frame_row
        index = mouse.y - (frame_top + len(self._queued))
        if not 0 <= index < len(dock):
            return None
        item = dock[index]
        if not isinstance(item, DockItem) or not item.event:
            return None
        return PumpEvent(item.event, value=item.value)

    def _handle_line(self, key):
        """Route viewport navigation and dock clicks before line editing."""
        if isinstance(key, CursorPosition):
            with self._cursor_condition:
                self._cursor_screen_row = key.y
                self._cursor_condition.notify_all()
            return []
        if not isinstance(key, MouseEvent):
            # The next redraw will issue a fresh DSR query.  Keeping the old
            # absolute row could map an immediate click into the wrong frame.
            self._cursor_screen_row = None
        if isinstance(key, MouseEvent):
            if (key.modifiers & _MOUSE_SHIFT
                    and key.kind not in {"scroll_up", "scroll_down"}):
                # Preserve the terminal's native selection gesture.  Some
                # emulators consume it before the application sees it; those
                # that report it to us must not turn it into a dock/history
                # action as a fallback.
                return []
            if key.kind == "scroll_up":
                self._history_mode = True
                self._composer_selection_active = False
                return [PumpEvent("history_scroll", value=3)]
            if key.kind == "scroll_down" and self._history_mode:
                return [PumpEvent("history_scroll", value=-3)]
            if (key.kind == "press" and key.button == 0
                    and self._history_mode):
                return [PumpEvent("history_click", value=key)]
            if (self._history_mode
                    and key.kind in {"motion", "release"}):
                return [PumpEvent("history_mouse", value=key)]
            clicked = self._dock_click_event(key)
            if clicked is not None:
                return [clicked]
            if (key.kind in {"motion", "release"}
                    or (key.kind == "press" and key.button == 0)):
                return [PumpEvent("composer_mouse", value=key)]
            return []
        if key == "pageup":
            self._history_mode = True
            return [PumpEvent("history_scroll", value="page_up")]
        if key == "pagedown":
            if self._history_mode:
                return [PumpEvent("history_scroll", value="page_down")]
            return []
        if self._history_mode:
            if key == "home":
                return [PumpEvent("history_scroll", value="home")]
            if key == "end":
                self._history_mode = False
                return [PumpEvent("history_close")]
            if key == "ctrl-c":
                # In the app-owned history viewport Ctrl-C is copy; Esc keeps
                # its unambiguous meaning of returning to the composer.
                return [PumpEvent("history_copy")]
            if key == "esc":
                self._history_mode = False
                return [PumpEvent("history_close")]
            # Typing/editing is an intentional return to the live composer.
            # Close the alternate history view before applying the same key,
            # so the user's draft and normal shortcuts remain untouched.
            self._history_mode = False
            return [PumpEvent("history_close"), *self.editor.handle(key)]
        if key == "ctrl-c" and self._composer_selection_active:
            return [PumpEvent("composer_copy")]
        if key == "esc" and self._composer_selection_active:
            return [PumpEvent("composer_clear_selection")]
        had_composer_selection = self._composer_selection_active
        if key not in ("ctrl-c", "esc"):
            self._composer_selection_active = False
        events = self.editor.handle(key)
        if had_composer_selection:
            return [PumpEvent("composer_clear_selection"), *events]
        return events

    def configure(self, *, busy=None, prompt=None, queued=_FRAME_UNSET,
                  dock=_FRAME_UNSET, status=_FRAME_UNSET,
                  activity=_FRAME_UNSET,
                  target=_FRAME_UNSET, target_history=_FRAME_UNSET,
                  publish=True):
        """原子更新 composer 状态，只产生一帧完整 redraw。"""
        with self._lock:
            old_busy = self.editor.busy
            if target is not _FRAME_UNSET or target_history is not _FRAME_UNSET:
                selected_target = (
                    self.editor.target
                    if target is _FRAME_UNSET else target)
                if selected_target != self.editor.target:
                    self._cursor_screen_row = None
                self.editor.set_target(
                    selected_target, history=target_history)
            if prompt is not None:
                if str(prompt) != self.editor.prompt:
                    self._composer_selection_active = False
                    self._cursor_screen_row = None
                self.editor.set_prompt(prompt)
            if queued is not _FRAME_UNSET:
                next_queued = tuple(queued or ())
                if next_queued != self._queued:
                    self._composer_selection_active = False
                self._queued = next_queued
                self._cursor_screen_row = None
            if dock is not _FRAME_UNSET:
                next_dock = tuple(dock or ())
                if next_dock != self._dock:
                    self._composer_selection_active = False
                self._dock = next_dock
                self._cursor_screen_row = None
            if status is not _FRAME_UNSET:
                self._status = str(status or "")
            if activity is not _FRAME_UNSET:
                self._activity = str(activity or "")
            editor_snapshot = (
                self.editor.set_busy(busy)
                if busy is not None else self.editor.snapshot())
            if busy is not None and bool(busy) != bool(old_busy):
                self._composer_selection_active = False
                self._cursor_screen_row = None
            snapshot = self._decorate_snapshot(editor_snapshot)
        if publish:
            self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return snapshot

    def set_busy(self, busy):
        return self.configure(busy=busy)

    def set_prompt(self, prompt):
        return self.configure(prompt=prompt)

    def set_commands(self, commands, *, publish=True):
        with self._lock:
            self._cursor_screen_row = None
            snapshot = self._decorate_snapshot(
                self.editor.set_commands(commands))
        if publish:
            self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return snapshot

    def set_subcommands(self, subcommands, *, publish=True, reset_menu=True):
        with self._lock:
            self._cursor_screen_row = None
            snapshot = self._decorate_snapshot(
                self.editor.set_subcommands(
                    subcommands, reset_menu=reset_menu))
        if publish:
            self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return snapshot

    def set_skills(self, skills, *, publish=True):
        with self._lock:
            self._cursor_screen_row = None
            snapshot = self._decorate_snapshot(self.editor.set_skills(skills))
        if publish:
            self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return snapshot

    def set_cwd(self, cwd, *, publish=True):
        with self._lock:
            self._cursor_screen_row = None
            snapshot = self._decorate_snapshot(self.editor.set_cwd(cwd))
        if publish:
            self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return snapshot

    def add_history(self, text):
        with self._lock:
            self.editor.add_history(text)

    def replace_buffer(self, text, *, active=True, publish=True):
        with self._lock:
            self._composer_selection_active = False
            self._cursor_screen_row = None
            snapshot = self._decorate_snapshot(
                self.editor.replace(text, active=active))
        if publish:
            self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return snapshot

    def snapshot(self):
        with self._lock:
            if self._mode == "picker":
                return self._decorate_snapshot(self._picker_snapshot())
            if self._mode == "decision_gate":
                return self._decorate_snapshot(
                    self._decision_gate_snapshot())
            if self._mode == "prompt":
                return self._decorate_snapshot(self._prompt_snapshot())
            return self._decorate_snapshot(self.editor.snapshot())

    def open_picker(self, rows, *, title="", render=str, page=12,
                    allow_filter=True, actions=None, controls="",
                    fullscreen=False, mouse_action=None,
                    allow_empty=False, explicit_filter=False,
                    initial_state=None, row_key=None, filter_text=None):
        """打开 modal picker。

        ``actions`` 把按键映射为返回事件的 ``mode``，例如
        ``{"enter": "attach", "space": "peek"}``。未配置时保持旧行为：
        Enter 选择并返回 ``mode=None``。``controls`` 可替换默认操作提示。
        """
        rows = list(rows)
        if not rows and not allow_empty:
            self._events.put(PumpEvent("picker_result", value=None))
            return
        picker_actions = {"enter": None}
        for key, action in dict(actions or {}).items():
            key = " " if key == "space" else str(key)
            picker_actions[key] = (
                None if action is None else str(action))
        with self._lock:
            if self._mode != "line":
                raise RuntimeError("已有 modal 打开")
            self._history_mode = False
            self._composer_selection_active = False
            self._cursor_screen_row = None
            self._mode = "picker"
            self._picker = {
                "rows": rows,
                "title": str(title),
                "render": render,
                "page": max(1, int(page)),
                "allow_filter": bool(allow_filter),
                "actions": picker_actions,
                "fullscreen": bool(fullscreen),
                "mouse_action": (
                    picker_actions.get("enter")
                    if mouse_action is None else str(mouse_action)),
                "controls": str(controls or ""),
                "filter": str((initial_state or {}).get("filter") or ""),
                "filter_mode": bool(
                    (initial_state or {}).get("filter_mode", False)),
                "explicit_filter": bool(explicit_filter),
                "row_key": row_key,
                "filter_text": filter_text,
                "index": 0,
                "visible_top": 0,
                "visible_count": 0,
            }
            requested_key = (initial_state or {}).get("selected_key")
            requested_index = max(
                0, int((initial_state or {}).get("index") or 0))
            view = self._picker_view()
            selected_index = None
            if requested_key is not None and callable(row_key):
                for index, row in enumerate(view):
                    if row_key(row) == requested_key:
                        selected_index = index
                        break
            self._picker["index"] = (
                requested_index
                if selected_index is None else selected_index)
            snapshot = self._decorate_snapshot(self._picker_snapshot())
        self._events.put(PumpEvent("redraw", snapshot=snapshot))

    def replace_picker_rows(self, rows):
        """原地替换 picker 的行（定时刷新用）：保住筛选词，光标按 row_key 跟着原来那行走。"""
        with self._lock:
            picker = self._picker
            if self._mode != "picker" or picker is None:
                return False
            state = self._picker_state()
            picker["rows"] = list(rows)
            view = self._picker_view()
            row_key = picker.get("row_key")
            index = None
            if state.get("selected_key") is not None and callable(row_key):
                for position, row in enumerate(view):
                    if row_key(row) == state["selected_key"]:
                        index = position
                        break
            picker["index"] = (
                index if index is not None
                else min(picker["index"], max(0, len(view) - 1)))
            snapshot = self._decorate_snapshot(self._picker_snapshot())
        self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return True

    def _picker_view(self):
        picker = self._picker
        rows = picker["rows"]
        needle = picker["filter"].lower()
        if not needle:
            return rows
        filter_text = picker.get("filter_text")
        return [
            row for row in rows
            if needle in str(
                filter_text(row) if callable(filter_text)
                else picker["render"](row)
            ).casefold()
        ]

    def _picker_snapshot(self):
        picker = self._picker
        view = self._picker_view()
        if view:
            picker["index"] = min(picker["index"], len(view) - 1)
        else:
            picker["index"] = 0
        # title + state hint + wrapped controls + composer metadata; never
        # force the terminal to scroll merely because the configured page is
        # taller.  Renderer and pagination share the same two-row cap.
        terminal_lines = _lines()
        frame_width = max(20, _cols() - 1)
        controls = (
            picker["controls"]
            or "↑↓ 选择 · Enter 确认 · Esc 取消")
        control_rows = _wrap_control_lines(
            controls, max(1, frame_width - 2),
            max_lines=_picker_control_line_limit(terminal_lines))
        fixed_rows = 3 + len(control_rows)
        page = min(
            picker["page"], max(1, terminal_lines - fixed_rows))
        top = max(
            0, min(picker["index"], max(0, len(view) - page)))
        if picker["index"] >= top + page:
            top = picker["index"] - page + 1
        window = view[top:top + page]
        picker["visible_top"] = top
        picker["visible_count"] = len(window)
        options = tuple(str(picker["render"](row)) for row in window)
        hint = (
            f"{top + 1}-{top + len(window)}/{len(view)}"
            if len(view) > page else f"{len(view)} 项")
        if picker["allow_filter"]:
            if picker["explicit_filter"]:
                if picker["filter_mode"]:
                    hint += f" · 筛选: {picker['filter']}▏"
                elif picker["filter"]:
                    hint += f" · 筛选: {picker['filter']} · / 编辑"
                else:
                    hint += " · / 筛选"
            else:
                hint += (
                    f" · 筛选: {picker['filter']}"
                    if picker["filter"] else " · 直接打字筛选")
        return InputSnapshot(
            mode="picker", options=options,
            selected=max(0, picker["index"] - top),
            title=picker["title"], hint=hint,
            target=self.editor.target, controls=picker["controls"],
            fullscreen=picker["fullscreen"])

    def _picker_state(self):
        picker = self._picker
        view = self._picker_view()
        selected = (
            view[picker["index"]]
            if view and picker["index"] < len(view) else None)
        row_key = picker.get("row_key")
        return {
            "filter": picker["filter"],
            "filter_mode": picker["filter_mode"],
            "index": picker["index"],
            "selected_key": (
                row_key(selected)
                if selected is not None and callable(row_key) else None),
        }

    def _close_picker(self, value, action=None):
        state = self._picker_state()
        self._mode = "line"
        self._picker = None
        self._cursor_screen_row = None
        return [
            PumpEvent(
                "picker_result", value=value, mode=action, state=state),
            PumpEvent("redraw", snapshot=self.editor.snapshot()),
        ]

    def _handle_picker(self, key):
        picker = self._picker
        view = self._picker_view()
        if isinstance(key, MouseEvent):
            if (key.modifiers & _MOUSE_SHIFT
                    and key.kind not in {"scroll_up", "scroll_down"}):
                return []
            if not picker["fullscreen"]:
                return []
            if key.kind == "scroll_up":
                picker["index"] = max(0, picker["index"] - 1)
                return [PumpEvent(
                    "redraw", snapshot=self._picker_snapshot())]
            if key.kind == "scroll_down":
                picker["index"] = (
                    min(len(view) - 1, picker["index"] + 1)
                    if view else 0)
                return [PumpEvent(
                    "redraw", snapshot=self._picker_snapshot())]
            if key.kind == "press" and key.button == 0:
                # Fullscreen picker is anchored at terminal row 1: title on
                # row 1, then one row per visible option. SGR mouse rows are
                # 1-based, so y=2 selects the first visible option.
                visible_index = int(key.y) - 2
                top = int(picker.get("visible_top") or 0)
                count = int(picker.get("visible_count") or 0)
                if 0 <= visible_index < count:
                    picker["index"] = top + visible_index
                    selected = view[picker["index"]]
                    return self._close_picker(
                        selected, picker.get("mouse_action"))
            return []
        if key in ("esc", "ctrl-c"):
            if picker["explicit_filter"] and picker["filter_mode"]:
                picker["filter_mode"] = False
                return [PumpEvent(
                    "redraw", snapshot=self._picker_snapshot())]
            return self._close_picker(None)
        if (picker["explicit_filter"] and not picker["filter_mode"]
                and key == "/" and picker["allow_filter"]):
            picker["filter_mode"] = True
            return [PumpEvent(
                "redraw", snapshot=self._picker_snapshot())]
        action_allowed = (
            not picker["explicit_filter"]
            or not picker["filter_mode"]
            or key == "enter"
        )
        if key in picker["actions"] and action_allowed:
            selected = view[picker["index"]] if view else None
            return self._close_picker(
                selected, picker["actions"][key])
        if key == "up":
            picker["index"] = max(0, picker["index"] - 1)
        elif key == "down":
            picker["index"] = (
                min(len(view) - 1, picker["index"] + 1)
                if view else 0)
        elif (key == "backspace" and picker["allow_filter"]
              and (not picker["explicit_filter"]
                   or picker["filter_mode"])):
            picker["filter"] = picker["filter"][:-1]
            picker["index"] = 0
        elif (key == "ctrl-u" and picker["allow_filter"]
              and (not picker["explicit_filter"]
                   or picker["filter_mode"])):
            picker["filter"] = ""
            picker["index"] = 0
        elif (picker["allow_filter"] and isinstance(key, str)
              and len(key) == 1 and key.isprintable()
              and (not picker["explicit_filter"]
                   or picker["filter_mode"])):
            picker["filter"] += key
            picker["index"] = 0
        return [PumpEvent(
            "redraw", snapshot=self._picker_snapshot())]

    # ------------------------------------------------------ decision gate modal
    def open_decision_gate(self, *, question="", options=(), context="",
                           questions=None,
                           controls=("↑↓ 选择 · Tab 切题 · Space 多选 · "
                                     "Enter 下一步/确认 · Esc 取消")):
        """Open a dedicated, owner-rendered decision gate modal.

        ``options`` are detached dictionaries.  The input reader only changes
        the selected index and emits a result; it never invokes application
        callbacks or writes output.
        """
        raw_questions = questions
        if not isinstance(raw_questions, (list, tuple)) or not raw_questions:
            raw_questions = [{
                "id": "decision", "question": question,
                "options": options, "multi_select": False,
            }]
        clean_questions = []
        for index, raw in enumerate(raw_questions):
            if not isinstance(raw, dict):
                continue
            clean_options = []
            for option in raw.get("options") or ():
                if not isinstance(option, dict):
                    continue
                clean_options.append({
                    "label": str(option.get("label") or ""),
                    "detail": str(option.get("detail") or ""),
                    "cost": str(option.get("cost") or ""),
                    "recommended": bool(option.get("recommended")),
                })
            if not clean_options:
                continue
            clean_questions.append({
                "id": str(raw.get("id") or f"question-{index + 1}"),
                "question": str(raw.get("question") or question or ""),
                "options": clean_options,
                "multi_select": bool(raw.get("multi_select")),
            })
        if not clean_questions:
            self._events.put(PumpEvent("decision_gate_result", value=None))
            return
        with self._lock:
            if self._mode != "line":
                raise RuntimeError("已有 modal 打开")
            self._history_mode = False
            self._composer_selection_active = False
            self._cursor_screen_row = None
            self._mode = "decision_gate"
            first_options = clean_questions[0]["options"]
            recommended_index = next(
                (index for index, item in enumerate(first_options)
                 if item.get("recommended")), 0)
            # Multi-select questions start with the single recommended item
            # checked.  This is a visible default, not consent: only an
            # explicit Enter on the final question emits a resolved result.
            # It also matches the ordinary picker, whose cursor starts on the
            # recommendation, while allowing Space/number to remove it.
            selected = {}
            for item in clean_questions:
                if not item.get("multi_select"):
                    continue
                recommended = next(
                    (str(option.get("label") or "")
                     for option in item.get("options") or ()
                     if option.get("recommended")), "")
                if recommended:
                    selected[item["id"]] = [recommended]
            self._decision_gate = {
                "question": str(question or ""),
                "questions": clean_questions,
                "context": str(context or ""),
                "controls": str(controls or ""),
                # Put the model's recommendation under the cursor.  For a
                # multi-select question it is also visibly checked, but never
                # submitted implicitly: Enter is still an explicit user key.
                "index": recommended_index,
                "question_index": 0,
                "selected": selected,
                "error": "",
            }
            snapshot = self._decorate_snapshot(
                self._decision_gate_snapshot())
        self._events.put(PumpEvent("redraw", snapshot=snapshot))

    def _decision_gate_snapshot(self):
        gate = self._decision_gate or {}
        questions = gate.get("questions") or []
        question_index = int(gate.get("question_index") or 0)
        if questions:
            question_index = max(0, min(question_index, len(questions) - 1))
            gate["question_index"] = question_index
        current = questions[question_index] if questions else {}
        current_options = current.get("options") or []
        options = tuple(str(item.get("label") or "")
                        for item in current_options)
        index = int(gate.get("index") or 0)
        if options:
            index = max(0, min(index, len(options) - 1))
            gate["index"] = index
        return InputSnapshot(
            mode="decision_gate",
            options=options,
            selected=index,
            title=current.get("question") or gate.get("question", ""),
            hint=gate.get("controls", ""),
            controls=gate.get("controls", ""),
            modal_context=gate.get("context", ""),
            option_details=tuple(dict(item) for item in current_options),
            modal_questions=tuple(dict(item) for item in questions),
            modal_question_index=question_index,
            modal_selected=tuple(
                sorted(gate.get("selected", {}).get(
                    str(current.get("id") or ""), ()))),
            modal_error=str(gate.get("error") or ""),
            target=self.editor.target,
        )

    def _close_decision_gate(self, value):
        self._mode = "line"
        self._decision_gate = None
        self._cursor_screen_row = None
        return [
            PumpEvent("decision_gate_result", value=value),
            PumpEvent("redraw", snapshot=self.editor.snapshot()),
        ]

    def dismiss_decision_gate(self):
        """Close a stale gate without fabricating a user result event."""
        with self._lock:
            if self._mode != "decision_gate":
                return False
            self._mode = "line"
            self._decision_gate = None
            self._cursor_screen_row = None
            snapshot = self._decorate_snapshot(self.editor.snapshot())
        self._events.put(PumpEvent("redraw", snapshot=snapshot))
        return True

    def _handle_decision_gate(self, key):
        gate = self._decision_gate
        if gate is None:
            return []
        questions = gate.get("questions") or []
        if not questions:
            return self._close_decision_gate(None)
        question_index = int(gate.get("question_index") or 0)
        question_index = max(0, min(question_index, len(questions) - 1))
        gate["question_index"] = question_index
        current = questions[question_index]
        options = current.get("options") or []
        question_id = str(current.get("id") or f"question-{question_index + 1}")
        selected_map = gate.setdefault("selected", {})
        chosen = set(selected_map.get(question_id) or ())
        multi = bool(current.get("multi_select"))
        if key in ("esc", "ctrl-c"):
            return self._close_decision_gate(None)
        if key in ("left", "shift-tab", "prev-question"):
            gate["question_index"] = max(0, question_index - 1)
            current = questions[gate["question_index"]]
            current_options = current.get("options") or []
            gate["index"] = next(
                (i for i, item in enumerate(current_options)
                 if item.get("recommended")), 0)
            gate["error"] = ""
            return [PumpEvent("redraw", snapshot=self._decision_gate_snapshot())]
        if key in ("right", "tab", "next-question"):
            gate["question_index"] = min(len(questions) - 1, question_index + 1)
            current = questions[gate["question_index"]]
            current_options = current.get("options") or []
            gate["index"] = next(
                (i for i, item in enumerate(current_options)
                 if item.get("recommended")), 0)
            gate["error"] = ""
            return [PumpEvent("redraw", snapshot=self._decision_gate_snapshot())]
        if key == "up":
            gate["index"] = max(0, int(gate.get("index") or 0) - 1)
        elif key == "down":
            gate["index"] = min(
                max(0, len(options) - 1), int(gate.get("index") or 0) + 1)
        elif key == "pageup":
            gate["index"] = max(0, int(gate.get("index") or 0) - 5)
        elif key == "pagedown":
            gate["index"] = min(
                max(0, len(options) - 1), int(gate.get("index") or 0) + 5)
        elif isinstance(key, str) and len(key) == 1 and key.isdigit():
            # Numeric shortcuts make a 2–4 item gate usable without repeated
            # arrow presses, while values outside the visible option range
            # remain harmless no-ops.
            option_index = int(key) - 1
            if 0 <= option_index < len(options):
                gate["index"] = option_index
                if multi:
                    label = str(options[option_index].get("label") or "")
                    if label in chosen:
                        chosen.remove(label)
                    else:
                        chosen.add(label)
                    selected_map[question_id] = sorted(chosen)
        elif key in ("space", " ") and options:
            label = str(options[int(gate.get("index") or 0)].get("label") or "")
            if multi:
                if label in chosen:
                    chosen.remove(label)
                else:
                    chosen.add(label)
                selected_map = gate.setdefault("selected", {})
                selected_map[question_id] = sorted(chosen)
            else:
                selected_map[question_id] = [label]
        elif key == "enter":
            index = max(0, min(
                int(gate.get("index") or 0), len(options) - 1))
            if options and not multi:
                selected_map[question_id] = [str(
                    options[index].get("label") or "")]
            if question_index < len(questions) - 1 and selected_map.get(question_id):
                gate["question_index"] = question_index + 1
                next_options = questions[question_index + 1].get("options") or []
                gate["index"] = next(
                    (i for i, item in enumerate(next_options)
                     if item.get("recommended")), 0)
                gate["error"] = ""
                return [PumpEvent(
                    "redraw", snapshot=self._decision_gate_snapshot())]
            missing = []
            for item in questions:
                item_id = str(item.get("id") or "")
                if not selected_map.get(item_id):
                    missing.append(item_id)
            if missing:
                gate["error"] = "请先回答：" + ", ".join(missing)
            else:
                answers = {}
                for item in questions:
                    item_id = str(item.get("id") or "")
                    values = list(selected_map[item_id])
                    answers[item_id] = values if item.get("multi_select") else values[0]
                first_values = answers.get(str(questions[0].get("id") or ""))
                first_choice = (first_values[0] if isinstance(first_values, list)
                                else first_values)
                return self._close_decision_gate({
                    "choice": str(first_choice or ""),
                    "answers": answers,
                    "notes": "",
                    "attended": True,
                    "status": "resolved",
                })
        return [PumpEvent(
            "redraw", snapshot=self._decision_gate_snapshot())]

    def open_prompt(self, *, prompt="› ", initial="", title="", hint=""):
        """Open an isolated one-line modal without touching the main draft."""
        with self._lock:
            if self._mode != "line":
                raise RuntimeError("已有 modal 打开")
            self._history_mode = False
            self._composer_selection_active = False
            self._cursor_screen_row = None
            self._mode = "prompt"
            self._prompt_editor = LineEditor(prompt=str(prompt))
            self._prompt_editor.replace(str(initial), active=True)
            self._prompt_title = str(title or "")
            self._prompt_hint = str(hint or "")
            snapshot = self._decorate_snapshot(self._prompt_snapshot())
        self._events.put(PumpEvent("redraw", snapshot=snapshot))

    def _prompt_snapshot(self):
        snapshot = self._prompt_editor.snapshot()
        return dataclass_replace(
            snapshot,
            mode="prompt",
            title=self._prompt_title,
            hint=self._prompt_hint,
            options=(),
            selected=0,
        )

    def _close_prompt(self, value):
        self._mode = "line"
        self._prompt_editor = None
        self._cursor_screen_row = None
        return [
            PumpEvent("prompt_result", value=value),
            PumpEvent("redraw", snapshot=self.editor.snapshot()),
        ]

    def _handle_prompt(self, key):
        if key in ("esc", "ctrl-c"):
            return self._close_prompt(None)
        if key == "enter":
            return self._close_prompt(self._prompt_editor.text)
        if key in ("ctrl-b", "ctrl-t", "tab", "shift-tab"):
            return [PumpEvent(
                "redraw", snapshot=self._prompt_snapshot())]
        events = self._prompt_editor.handle(key)
        converted = []
        for event in events:
            if event.kind == "redraw":
                converted.append(PumpEvent(
                    "redraw", snapshot=self._prompt_snapshot()))
            elif event.kind == "eof":
                converted.extend(self._close_prompt(None))
            else:
                converted.append(event)
        return converted


@dataclass
class _TranscriptEntry:
    id: object
    kind: str
    parts: list
    source: str | None = None
    message_index: int | None = None

    @property
    def text(self):
        return "".join(self.parts)

    @property
    def char_count(self):
        return sum(len(part) for part in self.parts)

    def append(self, value):
        self.parts.append(str(value))

    def replace(self, value):
        self.parts = [str(value)]


class ClipboardResult(str):
    """带显式语义的复制结果；仍是 str 子类，旧调用点照样能用。

    为什么需要：`_copy_text_to_clipboard` 只返回后端名或 None，于是三种完全不同
    的结局被压成同一句「已复制 N 字符」——
      · 外部 helper exit 0 → 真的复制成功了
      · OSC 52            → **只是发出了请求**，协议没有任何回执
      · clipboard=off     → 一个字都没复制
    把它们说成同一件事，撞的是 AGENTS.md 第 2 条（用户可见文案必须覆盖全部状态）。

    继承 str 是刻意的：真值语义与原来的 bool API 兼容，想区分「确认」与「已请求」
    的调用方再去看 `status`。（接口沿用 2026-09-02 那份未合并实现，因为那批测试
    是照它写的。）
    """

    SUCCESS_STATUSES = frozenset({"confirmed_external", "requested_osc52"})

    def __new__(cls, status, *, backend=None, char_count=0,
                truncated=False, reason=""):
        obj = super().__new__(cls, str(status))
        obj.status = str(status)
        obj.backend = None if backend is None else str(backend)
        obj.char_count = int(char_count or 0)
        obj.truncated = bool(truncated)
        obj.reason = str(reason or "")
        return obj

    @property
    def result(self):
        return self.status

    def __bool__(self):
        return self.status in self.SUCCESS_STATUSES


class TerminalRenderer:
    """主线程唯一 stdout writer；输入组件只提交 snapshot。"""

    TRANSCRIPT_CHAR_LIMIT = 2_000_000
    TRANSCRIPT_ENTRY_LIMIT = 2_000
    TRANSCRIPT_INITIAL_SCREENS = 3
    TRANSCRIPT_PAGE_ENTRIES = 96
    TRANSCRIPT_TOOL_PREVIEW = 1_200
    HISTORY_DEFER_LIMIT = 4_000_000
    HISTORY_COPY_CHAR_LIMIT = 1_000_000

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.owner_ident = threading.get_ident()
        try:
            self.is_tty = self.stream.isatty()
        except (AttributeError, ValueError):
            self.is_tty = False
        self.styled = terminal_style_enabled(self.stream)
        self._drawn = False
        self._rows_before_cursor = 0
        # 非换行结尾的模型 chunk 暂存在 composer 上一行。下一 chunk 先回到
        # 该列续写，避免 render() 的 ESC[J 把刚输出的正文擦掉。
        self._output_partial_col = None
        self._output_source = None
        self._spinner_visible = False
        self._spinner_key = None
        # 上一帧画出去的行，用来做增量重绘（见 _render_incremental）。
        # 任何「帧被抹掉/位置不再可信」的地方都要把它清成 None。
        self._last_lines = None
        self._last_snapshot = None
        self._transcript = []
        self._transcript_chars = 0
        self._next_transcript_id = 1
        # Saved messages remain the canonical full transcript. The UI index
        # starts with a viewport-sized tail and pages older entries on demand,
        # avoiding both a fixed "last N messages" resume and a 41 MB redraw.
        self._transcript_source = None
        self._transcript_source_cursor = 0
        self._transcript_source_length = 0
        self._transcript_tool_names = {}
        self._history_active = False
        self._history_offset = 0
        self._history_anchor_id = None
        self._history_header_row = None
        self._history_deferred = []
        self._history_deferred_chars = 0
        self._history_unseen = 0
        self._history_rows_snapshot = None
        self._history_snapshot_width = None
        self._history_window_start = 0
        self._history_window_end = 0
        self._history_selection_anchor = None
        self._history_selection_focus = None
        self._history_selection_dragging = False
        self._history_notice = ""
        self._composer_selection_anchor = None
        self._composer_selection_focus = None
        self._composer_selection_dragging = False
        self._composer_selection_value = None
        self._composer_selection_cursor = None
        self._composer_selection_layout_key = None
        self._composer_notice = ""
        self._last_clipboard_backend = None
        self._last_clipboard_status = None
        self._last_clipboard_reason = ""
        self._last_clipboard_result = None
        self._last_copy_notice = ""
        self._last_cursor_screen_row = None
        self._last_mouse_query_signature = None
        self._last_mouse_query_at = 0.0
        self._mouse_enabled = False
        self._mouse_tracking = "press"
        mouse_setting = "native"          # 内联模式固定：不接管鼠标（I4）；ZYLAB_MOUSE 不再生效
        if mouse_setting in {"0", "false", "no", "off", "native"}:
            # Keep an explicit startup escape hatch for terminals that do not
            # implement Shift+drag selection while mouse reporting is active.
            self._mouse_preference = "native"
        else:
            # The normal product mode is fixed: wheel/click navigation plus
            # terminal-native Shift+drag selection.
            self._mouse_preference = "hybrid"
        self._dock_mouse_auto = False
        self._picker_fullscreen = False

    def _check_owner(self):
        if threading.get_ident() != self.owner_ident:
            raise RuntimeError("TerminalRenderer 只能由 owner 线程写 stdout")

    def _write(self, text):
        self.stream.write(text)

    def flush(self):
        self.stream.flush()

    # ------------------------------------------------------------ 通知与标题
    # Claude Code 契约 H1/H2（SPEC-CC-parity）：turn 完成响一声、终端标题随
    # 状态变。两者都只在 tty 上发 —— 管道、-p 模式、测试里的 StringIO 一律静默，
    # 否则 \x07 和 OSC 会污染被重定向的输出。

    _TITLE_UNSAFE = re.compile(r"[\x00-\x1f\x7f\x9c]")

    def set_title(self, text):
        """OSC 0 设置终端标题；空串即清空。

        标题内容可能来自用户输入（session 标题、cwd 名），所以先剥掉 C0 控制字符
        与 ST（0x9c）—— 否则一个含 BEL/ESC 的 session 名就能从标题里注入序列。
        """
        if not self.is_tty:
            return False
        safe = self._TITLE_UNSAFE.sub("", str(text or ""))[:120]
        self._write(f"\x1b]0;{safe}\x07")
        self.flush()
        return True

    @property
    def history_active(self):
        return self._history_active

    @property
    def mouse_enabled(self):
        return self._mouse_enabled

    @property
    def history_selection_active(self):
        return (
            self._history_selection_anchor is not None
            and self._history_selection_focus is not None
            and self._history_selection_anchor
            != self._history_selection_focus
        )

    @property
    def composer_selection_active(self):
        return (
            self._composer_selection_anchor is not None
            and self._composer_selection_focus is not None
            and self._composer_selection_anchor
            != self._composer_selection_focus
        )

    @property
    def mouse_tracking(self):
        """Current DEC mouse tracking mode, useful to embedding TUI tests."""
        return self._mouse_tracking

    @property
    def clipboard_backend(self):
        return self._last_clipboard_backend

    @staticmethod
    def _clipboard_notice(result):
        """四种结局四句话 —— 绝不把「已请求」说成「已复制」。"""
        if result.status == "confirmed_external":
            text = (f"已复制 {result.char_count:,} 字符 · "
                    f"{result.backend or 'external'}")
        elif result.status == "requested_osc52":
            text = f"已发送复制请求 {result.char_count:,} 字符 · osc52"
            if result.reason == "external_failed":
                text += "（本地 helper 全部失败）"
        elif result.status == "disabled":
            text = "复制未执行 · clipboard=off"
        else:
            text = "复制失败 · 无可用 backend"
        if result.truncated:
            text += "（已截断）"
        return text

    def _copy_result(self, value, *, truncated=False):
        """复制一段文本并返回带语义的结果。"""
        value = str(value)
        self._last_clipboard_status = None
        self._last_clipboard_reason = ""
        backend = self._copy_text_to_clipboard(value)
        status = getattr(self, "_last_clipboard_status", None)
        if status not in {"confirmed_external", "requested_osc52",
                          "disabled", "failed"}:
            status = "failed" if backend is None else "requested_osc52"
        result = ClipboardResult(
            status, backend=backend, char_count=len(value),
            truncated=truncated,
            reason=getattr(self, "_last_clipboard_reason", ""))
        self._last_clipboard_result = result
        self._last_copy_notice = self._clipboard_notice(result)
        return result

    def _copy_text_to_clipboard(self, value):
        """Use an explicit local backend when requested, then OSC 52."""
        value = str(value)
        environment = os.environ
        setting = str(paths.env_get("CLIPBOARD", "osc52", environ=environment)).lower()
        if setting in {"0", "false", "no", "off", "none"}:
            self._last_clipboard_backend = None
            self._last_clipboard_status = "disabled"
            return None

        candidates = []
        # ``auto`` respects the session's advertised display protocol.  The
        # explicit external modes are useful in SSH wrappers and tests where
        # DISPLAY/WAYLAND_DISPLAY is intentionally not exported, so they probe
        # the requested helper directly.
        probe_all = setting in {"external", "system"}
        if setting in {"auto", "external", "system", "wl-copy"}:
            if probe_all or setting == "wl-copy" or environment.get("WAYLAND_DISPLAY"):
                candidates.append(("wl-copy", []))
        if setting in {"auto", "external", "system", "xclip"}:
            if probe_all or setting == "xclip" or environment.get("DISPLAY"):
                candidates.append(("xclip", ["-selection", "clipboard"]))
        if setting in {"auto", "external", "system", "xsel"}:
            if probe_all or setting == "xsel" or environment.get("DISPLAY"):
                candidates.append(("xsel", ["--clipboard", "--input"]))
        if candidates:
            for command, args in candidates:
                executable = shutil.which(command)
                if executable is None:
                    continue
                try:
                    completed = subprocess.run(
                        [executable, *args], input=value.encode("utf-8"),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=1.0, check=False)
                except (OSError, subprocess.SubprocessError):
                    continue
                if completed.returncode == 0:
                    self._last_clipboard_backend = command
                    self._last_clipboard_status = "confirmed_external"
                    return command

        # 显式指定了本地 helper 却全部失败时**不回落** —— 用户要的是 external，
        # 悄悄改走 OSC 52 会把选中文本写进终端流，那是他没要求的数据路径，
        # 带隐私含义。只有 `auto` 是刻意允许回落的那一档。
        if probe_all and candidates:
            self._last_clipboard_backend = None
            self._last_clipboard_status = "failed"
            self._last_clipboard_reason = "external_failed"
            return None

        # OSC 52 is the portable path for SSH/tmux-style TUI sessions.  It is
        # intentionally explicit in the output rather than using a shell.
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        self._write("\x1b]52;c;" + encoded + "\x07")
        self.flush()
        self._last_clipboard_backend = "osc52"
        # 走到这里只有两种：本来就只有 OSC 52 可走，或 `auto` 下 helper 失败后
        # 刻意回落。后者用户该知道 —— 它意味着本地 helper 有问题。
        self._last_clipboard_status = "requested_osc52"
        self._last_clipboard_reason = "external_failed" if candidates else ""
        return "osc52"

    def _set_mouse_tracking(self, mode):
        """Switch press-only tracking to drag tracking for app-owned views."""
        self._check_owner()
        mode = "drag" if str(mode).lower() == "drag" else "press"
        if not self._mouse_enabled or mode == self._mouse_tracking:
            return False
        if self._mouse_tracking == "drag":
            self._write(CSI + "?1002l")
        else:
            self._write(CSI + "?1000l")
        self._write(CSI + ("?1002h" if mode == "drag" else "?1000h"))
        self._mouse_tracking = mode
        self.flush()
        return True

    def enable_mouse(self, env=None, *, force=False):
        self._check_owner()
        return False                     # 内联模式永不接管鼠标（I4）；全屏模式见 core/apprender
        if self._mouse_enabled:
            return False
        if force:
            try:
                capable = bool(self.stream.isatty())
            except (AttributeError, ValueError):
                capable = False
            environment = os.environ if env is None else env
            capable = (
                capable
                and str(environment.get("TERM", "")).lower() != "dumb")
        else:
            capable = terminal_mouse_enabled(self.stream, env=env)
        if not capable:
            return False
        # Button-event tracking (1002) is required for app-owned drag
        # selection.  SGR (1006) keeps coordinates unambiguous for wide and
        # long terminal cells.  Enable both in one write so no press-only
        # window exists during startup.
        self._write(CSI + "?1002h" + CSI + "?1006h")
        self.flush()
        self._mouse_enabled = True
        self._mouse_tracking = "drag"
        return True

    def disable_mouse(self):
        self._check_owner()
        if not self._mouse_enabled:
            return False
        if self._history_active:
            self.close_history(self._last_snapshot)
        # Disable both tracking variants defensively.  A previous process or
        # an embedding caller may have left 1000 enabled even if our local
        # state says otherwise.
        self._write(CSI + "?1002l" + CSI + "?1000l" + CSI + "?1006l")
        self.flush()
        self._mouse_enabled = False
        self._mouse_tracking = "press"
        self._last_mouse_query_signature = None
        self._last_mouse_query_at = 0.0
        return True

    def set_actionable_dock(self, active):
        """Ensure dock clicks work without changing the fixed mouse policy."""
        self._check_owner()
        return False                     # 内联模式不抓鼠标，dock 只能用键盘
        active = bool(active)
        if active:
            if self._mouse_preference == "native":
                return False
            if not self._mouse_enabled:
                self.enable_mouse(force=True)
            return self._mouse_enabled
        # Mouse reporting is session-scoped now.  Do not turn it off merely
        # because the currently visible dock has no actionable row.
        return self._mouse_enabled

    def set_mouse_mode(self, mode):
        """Deprecated compatibility shim for the former ``/mouse`` toggle."""
        self._check_owner()
        value = str(mode or "").strip().lower()
        if value not in {
                "", "status", "claude", "app", "on", "1",
                "native", "off", "0"}:
            raise ValueError("mouse 已固定为 hybrid；/mouse 已废弃")
        self._mouse_preference = "hybrid"
        self._dock_mouse_auto = False
        if not self._mouse_enabled:
            return "hybrid" if self.enable_mouse(force=True) else "unsupported"
        return "hybrid"

    @staticmethod
    def _message_text(message):
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        values = []
        for item in content:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                values.append(str(item.get("text") or ""))
        return "\n".join(value for value in values if value)

    @staticmethod
    def _transcript_plain(value):
        value = str(value)
        # These C-level checks cover ordinary ASCII and printable Unicode.
        # Anything containing ESC, CR, C0/C1 or format controls still goes
        # through the complete Unicode/control-state sanitizer below.
        if (_SAFE_TRANSCRIPT_ASCII.fullmatch(value)
                or value.isprintable()):
            return value
        sanitizer = _TerminalTextSanitizer()
        return sanitizer.feed(value) + sanitizer.finish()

    def _clip_transcript_tool(self, value):
        value = self._transcript_plain(value).strip()
        limit = max(200, int(self.TRANSCRIPT_TOOL_PREVIEW))
        if len(value) <= limit:
            return value
        head = max(1, limit * 2 // 3)
        tail = max(1, limit - head - 1)
        return value[:head] + "\n… tool output collapsed …\n" + value[-tail:]

    def _fold_transcript_result(self, value, call_id, name):
        value = self._transcript_plain(value).strip()
        failure = (
            value.startswith((
                "[已拒绝]", "[执行失败", "[读取失败", "[不存在",
                "[用户拒绝", "[用户中断", "[超时", "[搜索超时",
                "[未执行：", "[取消请求", "[参数错误",
                "[参数不是合法 JSON", "[无权限", "[不是目录",
                "[未找到待替换内容", "[匹配到", "[glob 已取消",
                "[grep 已取消", "[checkpoint 创建失败",
                "[tool trace 创建失败"))
            or "[artifact index 写入失败:" in value
            or "[artifact 收尾失败:" in value
            or (name == "bash"
                and re.search(r"\[exit -?[1-9]\d*\]", value)))
        if failure:
            return value
        lines = [line for line in value.splitlines() if line.strip()]
        if len(lines) <= 1:
            summary = " ".join((lines[0] if lines else "无输出").split())
            summary = truncate_display(summary, 96)
        else:
            summary = f"{len(lines):,} lines"
        hint = f" · /expand {call_id}" if call_id else ""
        return summary + hint

    def _message_entries(self, message, message_index):
        """Build compact UI entries without changing canonical messages."""
        if not isinstance(message, dict):
            return []
        role = str(message.get("role") or "")
        entries = []

        def add(kind, value, *, source=None, suffix=0):
            plain = self._transcript_plain(value)
            if plain:
                entries.append(_TranscriptEntry(
                    id=("message", int(message_index), int(suffix)),
                    kind=kind,
                    parts=[plain],
                    source=source,
                    message_index=int(message_index),
                ))

        text = self._message_text(message)
        if role == "user" and text:
            # Autonomous goal continuations are durable model context, not
            # human prompts.  Keep them in the canonical transcript while
            # omitting their protocol wrapper from the user-facing scrollback;
            # the footer/goal status still exposes that a round is running.
            if goals.is_round_prompt(text):
                return entries
            add("user", "› " + text)
        elif role == "assistant":
            if text:
                add("assistant", "⏺ " + text)
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for index, call in enumerate(calls, 1):
                    function = (
                        call.get("function")
                        if isinstance(call, dict) else None)
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name") or "tool")
                    arguments = self._clip_transcript_tool(
                        function.get("arguments") or "")
                    suffix = f"  {arguments}" if arguments else ""
                    add(
                        "tool", f"● {name}{suffix}",
                        source=name, suffix=index)
        elif role == "tool" and text:
            call_id = str(message.get("tool_call_id") or "")
            name = str(message.get("name")
                       or self._transcript_tool_names.get(call_id)
                       or call_id or "tool")
            # 先算再插值：f-string 的替换字段里换行要 Python 3.12（PEP 701），
            # 而 README 承诺的是 3.10+ —— 在 3.10/3.11 上这是 SyntaxError，
            # 整个模块 import 不了。
            folded = self._fold_transcript_result(text, call_id, name)
            add("tool", f"⎿  {name} · {folded}", source=name)
        return entries

    def _entry_row_count(self, entry, width):
        rows = 0
        values = entry.text.replace("\r\n", "\n").replace(
            "\r", "\n").split("\n")
        if values and values[-1] == "":
            values = values[:-1] or [""]
        for value in values:
            rows += len(self._history_chunks(value, width))
        return max(1, rows)

    def _prepend_source_entries(
            self, *, entry_target=None, row_target=None, width=None,
            all_remaining=False):
        source = self._transcript_source
        if source is None or self._transcript_source_cursor <= 0:
            return 0
        entry_target = max(1, int(
            entry_target or self.TRANSCRIPT_PAGE_ENTRIES))
        row_target = None if row_target is None else max(1, int(row_target))
        width = max(20, int(width or (_cols() - 1)))
        cursor = self._transcript_source_cursor
        groups = []
        entries_loaded = 0
        rows_loaded = 0
        while cursor > 0:
            cursor -= 1
            group = self._message_entries(source[cursor], cursor)
            if group:
                groups.append(group)
                entries_loaded += len(group)
                if row_target is not None:
                    rows_loaded += sum(
                        self._entry_row_count(entry, width)
                        for entry in group)
            if not all_remaining:
                enough_entries = entries_loaded >= entry_target
                enough_rows = (
                    row_target is None or rows_loaded >= row_target)
                if enough_entries and enough_rows:
                    break
        if not groups:
            self._transcript_source_cursor = cursor
            return 0
        loaded = [
            entry
            for group in reversed(groups)
            for entry in group
        ]
        self._transcript[0:0] = loaded
        self._transcript_chars += sum(
            entry.char_count for entry in loaded)
        self._transcript_source_cursor = cursor
        self._history_rows_snapshot = None
        self._history_snapshot_width = None
        return len(loaded)

    def _trim_transcript(self):
        # A restored/live session keeps the canonical message list as a lazy
        # backing store. Keep source-backed entries that the user explicitly
        # paged in, while still bounding auxiliary/live renderer operations.
        if self._transcript_source is not None:
            while True:
                live = [
                    entry for entry in self._transcript
                    if entry.message_index is None
                ]
                live_chars = sum(entry.char_count for entry in live)
                if (len(live) <= self.TRANSCRIPT_ENTRY_LIMIT
                        and live_chars <= self.TRANSCRIPT_CHAR_LIMIT):
                    return
                if len(live) <= 1:
                    if live and live[0].char_count > self.TRANSCRIPT_CHAR_LIMIT:
                        entry = live[0]
                        clipped = entry.text[-self.TRANSCRIPT_CHAR_LIMIT:]
                        self._transcript_chars += (
                            len(clipped) - entry.char_count)
                        entry.replace(clipped)
                    return
                removed = live[0]
                self._transcript.remove(removed)
                self._transcript_chars -= removed.char_count
            return
        while (len(self._transcript) > self.TRANSCRIPT_ENTRY_LIMIT
               or self._transcript_chars > self.TRANSCRIPT_CHAR_LIMIT):
            if len(self._transcript) <= 1:
                entry = self._transcript[0]
                clipped = entry.text[-self.TRANSCRIPT_CHAR_LIMIT:]
                self._transcript_chars = len(clipped)
                entry.replace(clipped)
                break
            removed = self._transcript.pop(0)
            self._transcript_chars -= removed.char_count

    def _record_transcript(self, kind, value, *, source=None, merge=False):
        plain = self._transcript_plain(value)
        if not plain:
            return None
        if len(plain) > self.TRANSCRIPT_CHAR_LIMIT:
            plain = "…\n" + plain[-self.TRANSCRIPT_CHAR_LIMIT + 2:]
        if (merge and self._transcript
                and self._transcript[-1].kind == kind
                and self._transcript[-1].source == source):
            entry = self._transcript[-1]
            entry.append(plain)
            self._transcript_chars += len(plain)
        else:
            entry = _TranscriptEntry(
                id=self._next_transcript_id, kind=str(kind),
                parts=[plain],
                source=None if source is None else str(source))
            self._next_transcript_id += 1
            self._transcript.append(entry)
            self._transcript_chars += len(plain)
        self._trim_transcript()
        if not self._history_active:
            self._history_rows_snapshot = None
            self._history_snapshot_width = None
        return entry.id

    def set_transcript(self, messages):
        """Attach a full canonical transcript and index a visual tail lazily."""
        self._check_owner()
        if self._history_active:
            self.close_history(self._last_snapshot)
        self._transcript = []
        self._transcript_chars = 0
        self._next_transcript_id = 1
        self._clear_history_selection()
        self._history_notice = ""
        self._transcript_source = (
            messages if isinstance(messages, (list, tuple))
            else list(messages or ()))
        self._transcript_tool_names = {}
        for message in self._transcript_source:
            if (not isinstance(message, dict)
                    or message.get("role") != "assistant"):
                continue
            for call in message.get("tool_calls") or ():
                function = call.get("function") if isinstance(
                    call, dict) else None
                call_id = str(call.get("id") or "") if isinstance(
                    call, dict) else ""
                if call_id and isinstance(function, dict):
                    self._transcript_tool_names[call_id] = str(
                        function.get("name") or "tool")
        self._transcript_source_length = len(self._transcript_source)
        self._transcript_source_cursor = self._transcript_source_length
        width = max(20, _cols() - 1)
        row_target = max(
            12, _lines() * self.TRANSCRIPT_INITIAL_SCREENS)
        self._prepend_source_entries(
            entry_target=1, row_target=row_target, width=width)
        self._history_rows_snapshot = None
        self._history_snapshot_width = None

    def _hydration_entries(self, height, width):
        """Return a viewport tail beginning at a user-turn boundary."""
        height = max(4, int(height))
        width = max(20, int(width))
        while (self._transcript_source_cursor > 0
               and not any(
                   entry.kind == "user" for entry in self._transcript)):
            if not self._prepend_source_entries(
                    entry_target=self.TRANSCRIPT_PAGE_ENTRIES,
                    width=width):
                break
        selected = []
        rows = 0
        for entry in reversed(self._transcript):
            selected.append(entry)
            rows += self._entry_row_count(entry, width)
            if rows >= height and entry.kind == "user":
                break
        return list(reversed(selected))

    def hydrate_transcript(self, messages):
        """Paint a natural viewport tail while retaining lazy full history."""
        self._check_owner()
        self.set_transcript(messages)
        entries = self._hydration_entries(
            max(4, _lines() - 6), max(20, _cols() - 1))
        if not entries:
            return 0
        self.clear_input()
        self.clear_spinner()
        for entry in entries:
            value = entry.text
            if entry.kind == "user":
                self._commit_input_live("› ", value[2:] if value.startswith(
                    "› ") else value)
            elif entry.kind == "assistant":
                body = value[2:] if value.startswith("⏺ ") else value
                markdown = StreamingMarkdown(
                    styled=self.styled, icon_hosted=True)
                rendered = markdown.feed(body) + markdown.finish()
                prefix = (
                    "\x1b[38;5;214m⏺ \x1b[0m"
                    if self.styled else "⏺ ")
                self._write_output_live(
                    prefix + rendered + "\r\n", source="model")
            else:
                style = "\x1b[2;38;5;110m" if self.styled else ""
                reset = "\x1b[0m" if self.styled else ""
                self._write_output_live(
                    style + value + reset + "\r\n", source="tool")
        self._finish_output_line_live()
        return len(entries)

    @staticmethod
    def _history_chunks(value, columns):
        columns = max(1, int(columns))
        value = str(value).expandtabs(4)
        if not value:
            return [""]
        # Navigation text is already sanitized. ASCII therefore has
        # a one-byte/one-cell mapping and can avoid a Python-level
        # Unicode scan for every row at the 2M-character cap.
        if value.isascii():
            return [
                value[index:index + columns]
                for index in range(0, len(value), columns)
            ]
        if (value.startswith(("› ", "⏺ "))
                and value[2:].isascii()):
            prefix, tail = value[:2], value[2:]
            prefix_width = display_width(prefix)
            if prefix_width < columns:
                first_size = columns - prefix_width
                chunks = [prefix + tail[:first_size]]
                tail = tail[first_size:]
                chunks.extend(
                    tail[index:index + columns]
                    for index in range(0, len(tail), columns))
                return chunks
        if display_width(value) <= columns:
            return [value]
        if value.lstrip().startswith(
                ("┌", "├", "└", "│", "╭", "╰")):
            return [truncate_display(value, columns)]
        chunks, current, width = [], [], 0
        joined = False
        regional_pending = False
        for char in value:
            if char == "\u200d":
                current.append(char)
                joined = True
                continue
            regional = 0x1F1E6 <= ord(char) <= 0x1F1FF
            if regional:
                if regional_pending:
                    cell = 0
                    regional_pending = False
                else:
                    cell = 2
                    regional_pending = True
            else:
                regional_pending = False
                cell = _cell_width(char, joined=joined)
            joined = False
            cell = 0 if cell is None else cell
            if current and width + cell > columns:
                chunks.append("".join(current))
                current, width = [], 0
            current.append(char)
            width += cell
        chunks.append("".join(current))
        return chunks

    def _history_rows(self, width):
        rows = []
        for entry in self._transcript:
            values = entry.text.replace("\r\n", "\n").replace(
                "\r", "\n").split("\n")
            if values and values[-1] == "":
                values = values[:-1] or [""]
            for value in values:
                for chunk in self._history_chunks(value, width):
                    rows.append((entry.id, entry.kind, chunk))
        return rows

    def _clear_history_selection(self):
        self._history_selection_anchor = None
        self._history_selection_focus = None
        self._history_selection_dragging = False

    def _history_selection_bounds(self):
        anchor = self._history_selection_anchor
        focus = self._history_selection_focus
        if anchor is None or focus is None:
            return None
        return (anchor, focus) if anchor <= focus else (focus, anchor)

    def _history_point(self, event, snapshot=None, *, clamp=False):
        """Map a screen-cell mouse coordinate to a history row point."""
        if not self._history_active or not isinstance(event, MouseEvent):
            return None
        snapshot = snapshot or self._last_snapshot
        width = max(20, _cols() - 1)
        frame_lines, _, _ = self._build_frame(snapshot, width)
        terminal_rows = max(5, _lines())
        if len(frame_lines) + 2 > terminal_rows:
            return None
        height = max(1, terminal_rows - len(frame_lines) - 1)
        self._history_window(height, width)
        rows = self._history_rows_snapshot or ()
        visible_count = max(0, self._history_window_end
                            - self._history_window_start)
        if not rows or not visible_count:
            return None
        screen_offset = int(event.y) - 2
        if clamp:
            screen_offset = min(max(0, screen_offset), visible_count - 1)
        elif not 0 <= screen_offset < visible_count:
            return None
        row_index = self._history_window_start + screen_offset
        row_text = rows[row_index][2]
        column = min(
            max(0, int(event.x) - 1), display_width(row_text))
        return SelectionPoint(row=row_index, column=column)

    def _history_selection_text(self):
        bounds = self._history_selection_bounds()
        rows = self._history_rows_snapshot or ()
        if bounds is None or not rows:
            return ""
        start, end = bounds
        start_row = min(max(0, start.row), len(rows) - 1)
        end_row = min(max(0, end.row), len(rows) - 1)
        if start_row > end_row:
            start_row, end_row = end_row, start_row
        parts = []
        for row_index in range(start_row, end_row + 1):
            text = rows[row_index][2]
            left = start.column if row_index == start.row else 0
            right = end.column if row_index == end.row else display_width(text)
            if row_index == start.row == end.row:
                left, right = start.column, end.column
            parts.append(_slice_display_cells(text, left, right))
        return "\n".join(parts)

    def _highlight_history_row(self, text, row_index, width):
        shown = truncate_display(text, width)
        bounds = self._history_selection_bounds()
        if bounds is None or not self.styled:
            return shown
        start, end = bounds
        if row_index < start.row or row_index > end.row:
            return shown
        left = start.column if row_index == start.row else 0
        right = end.column if row_index == end.row else display_width(shown)
        if row_index == start.row == end.row:
            left, right = start.column, end.column
        selected = _slice_display_cells(shown, left, right)
        if not selected:
            return shown
        spans, _ = _display_cell_spans(shown)
        selected_start = None
        selected_end = None
        for span in spans:
            if span[3] <= left:
                continue
            if span[2] >= right:
                break
            selected_start = span[0] if selected_start is None else selected_start
            selected_end = span[1]
        if selected_start is None or selected_end is None:
            return shown
        return (
            shown[:selected_start]
            + "\x1b[7m" + shown[selected_start:selected_end]
            + "\x1b[27m" + shown[selected_end:]
        )

    def history_mouse(self, event, snapshot=None):
        """Handle app-owned drag selection inside the history alternate screen."""
        self._check_owner()
        if (not self._history_active or not isinstance(event, MouseEvent)
                or event.modifiers & _MOUSE_SHIFT):
            return False
        snapshot = snapshot or self._last_snapshot
        if event.kind == "press" and event.button == 0:
            # The sticky header remains a navigation affordance, not part of
            # the selectable transcript.
            if event.y == self._history_header_row:
                self._clear_history_selection()
                return self._history_click_header(snapshot)
            point = self._history_point(event, snapshot)
            if point is None:
                return False
            self._history_selection_anchor = point
            self._history_selection_focus = point
            self._history_selection_dragging = True
            self._history_notice = ""
            self._render_history(snapshot)
            return True
        if event.kind == "motion" and self._history_selection_dragging:
            point = self._history_point(event, snapshot, clamp=True)
            if point is None:
                return False
            self._history_selection_focus = point
            self._render_history(snapshot)
            return True
        if event.kind == "release" and self._history_selection_dragging:
            point = self._history_point(event, snapshot, clamp=True)
            if point is not None:
                self._history_selection_focus = point
            self._history_selection_dragging = False
            self._render_history(snapshot)
            return True
        return False

    def _history_click_header(self, snapshot=None):
        """Jump the viewport to the sticky prompt header."""
        if self._history_anchor_id is None:
            return False
        width = max(20, _cols() - 1)
        rows = self._history_rows_snapshot or self._history_rows(width)
        target = next((
            index for index, row in enumerate(rows)
            if row[0] == self._history_anchor_id), None)
        if target is None:
            return False
        frame_lines, _, _ = self._build_frame(
            snapshot or self._last_snapshot, width)
        height = max(1, _lines() - len(frame_lines) - 1)
        end = min(len(rows), target + height)
        self._history_offset = max(0, len(rows) - end)
        self._render_history(snapshot or self._last_snapshot)
        return self._history_active

    def copy_history_selection(self, snapshot=None):
        """Copy the current app-owned selection through terminal OSC 52."""
        self._check_owner()
        value = self._history_selection_text()
        if not value:
            return False
        truncated = False
        if len(value) > self.HISTORY_COPY_CHAR_LIMIT:
            value = value[:self.HISTORY_COPY_CHAR_LIMIT]
            truncated = True
        # 返回值语义这一轮刻意不动：falsey 会让调用点关掉历史视图，那是回归。
        # 先把说谎的文案修对；富结果放在 _last_clipboard_result 里。
        self._history_notice = self._clipboard_notice(
            self._copy_result(value, truncated=truncated))
        if self._history_active:
            self._render_history(snapshot or self._last_snapshot)
        return True

    def _clear_composer_selection(self):
        self._composer_selection_anchor = None
        self._composer_selection_focus = None
        self._composer_selection_dragging = False
        self._composer_selection_value = None
        self._composer_selection_cursor = None
        self._composer_selection_layout_key = None

    def _composer_layout_key(self, snapshot, width, safe_text=None):
        """Return the geometry inputs that make a composer point valid."""
        if safe_text is None:
            safe_text = self._transcript_plain(str(snapshot.text))
        prompt = _ANSI.sub("", str(snapshot.prompt))
        queued = tuple(
            self._transcript_plain(str(value)) for value in snapshot.queued)
        dock = tuple(
            self._transcript_plain(
                str(value.text if isinstance(value, DockItem) else value))
            for value in tuple(snapshot.dock)[:5])
        return (int(width), prompt, str(safe_text), queued, dock)

    def _composer_selection_bounds(self, value=None):
        if (self._composer_selection_anchor is None
                or self._composer_selection_focus is None):
            return None
        if (value is not None
                and self._composer_selection_value != str(value)):
            return None
        anchor = int(self._composer_selection_anchor)
        focus = int(self._composer_selection_focus)
        return (anchor, focus) if anchor <= focus else (focus, anchor)

    def _composer_selection_text(self, value=None):
        value = (self._composer_selection_value
                 if value is None else str(value))
        bounds = self._composer_selection_bounds(value)
        if bounds is None:
            return ""
        start, end = bounds
        return value[max(0, start):max(0, end)]

    def _composer_point(
            self, event, snapshot=None, cursor_screen_row=None, *,
            clamp=False):
        """Map a screen coordinate to a composer buffer index."""
        if not isinstance(event, MouseEvent):
            return None
        snapshot = snapshot or self._last_snapshot
        if (snapshot is None or snapshot.mode != "line"
                or not snapshot.active):
            return None
        if cursor_screen_row is None:
            cursor_screen_row = getattr(
                self, "_last_cursor_screen_row", None)
        if cursor_screen_row is None:
            return None
        width = max(20, _cols() - 1)
        _, cursor_row, _ = self._build_frame(snapshot, width)
        frame_top = int(cursor_screen_row) - int(cursor_row)
        line_index = int(event.y) - frame_top
        editor_start = (
            len(tuple(snapshot.queued))
            + len(tuple(snapshot.dock)[:5]))
        row = line_index - (editor_start + 1)
        raw_text = str(snapshot.text)
        safe_text = self._transcript_plain(raw_text)
        # Selection highlighting is deliberately based on plain display text;
        # ANSI styling cannot be used as a buffer index or cell coordinate.
        editor_prompt = _ANSI.sub("", str(snapshot.prompt))
        inner_width = max(4, width - 4)
        safe_cursor = len(self._transcript_plain(
            raw_text[:max(0, min(len(raw_text), int(snapshot.cursor)))]))
        editor_rows, _ = self._editor_rows(
            editor_prompt, safe_text, safe_cursor, inner_width)
        if not editor_rows:
            return None
        if clamp:
            row = min(max(0, row), len(editor_rows) - 1)
        elif not 0 <= row < len(editor_rows):
            return None
        # ``_editor_index_at`` addresses the complete content row (prompt
        # included).  The row itself starts after the left border and one
        # padding cell, so screen column 1 maps to content column -1.
        value_column = max(0, int(event.x) - 2)
        index = self._editor_index_at(
            editor_prompt, safe_text, row, value_column, inner_width)
        return index, safe_text

    def composer_mouse(
            self, event, snapshot=None, *, cursor_screen_row=None):
        """Handle ordinary drag selection and click positioning in composer."""
        self._check_owner()
        if (not isinstance(event, MouseEvent)
                or event.modifiers & _MOUSE_SHIFT):
            return ComposerMouseResult(False)
        snapshot = snapshot or self._last_snapshot
        if (snapshot is None or snapshot.mode != "line"
                or not snapshot.active):
            return ComposerMouseResult(False)
        if cursor_screen_row is not None:
            self._last_cursor_screen_row = int(cursor_screen_row)
        if event.kind == "press" and event.button == 0:
            point = self._composer_point(
                event, snapshot, cursor_screen_row)
            if point is None:
                # A click outside the box dismisses an existing selection,
                # matching normal terminal selection ergonomics.
                if self._composer_selection_anchor is not None:
                    self._clear_composer_selection()
                    self.render(snapshot)
                    return ComposerMouseResult(True)
                return ComposerMouseResult(False)
            index, value = point
            self._composer_selection_anchor = index
            self._composer_selection_focus = index
            self._composer_selection_dragging = True
            self._composer_selection_value = value
            self._composer_selection_cursor = len(self._transcript_plain(
                str(snapshot.text)[:max(
                    0, min(len(str(snapshot.text)), int(snapshot.cursor)))]))
            self._composer_selection_layout_key = self._composer_layout_key(
                snapshot, max(20, _cols() - 1), value)
            self._composer_notice = ""
            self.render(snapshot)
            return ComposerMouseResult(True)
        if event.kind == "motion" and self._composer_selection_dragging:
            point = self._composer_point(
                event, snapshot, cursor_screen_row, clamp=True)
            if point is None:
                return ComposerMouseResult(False)
            index, value = point
            if value != self._composer_selection_value:
                self._clear_composer_selection()
                self.render(snapshot)
                return ComposerMouseResult(True)
            self._composer_selection_focus = index
            self.render(snapshot)
            return ComposerMouseResult(True)
        if event.kind == "release" and self._composer_selection_dragging:
            point = self._composer_point(
                event, snapshot, cursor_screen_row, clamp=True)
            click_cursor = None
            if point is not None:
                index, value = point
                if value == self._composer_selection_value:
                    self._composer_selection_focus = index
                    if (self._composer_selection_anchor == index
                            and self._composer_selection_focus == index):
                        click_cursor = index
                        # A click is cursor placement, not an empty selection.
                        # Drop the anchor before rendering so the status hint
                        # and the next Ctrl-C/Esc path cannot retain stale state.
                        self._clear_composer_selection()
            self._composer_selection_dragging = False
            self.render(snapshot)
            return ComposerMouseResult(True, cursor=click_cursor)
        return ComposerMouseResult(False)

    def clear_composer_selection(self, snapshot=None):
        self._check_owner()
        had_selection = (
            self._composer_selection_anchor is not None
            or self._composer_selection_focus is not None)
        self._clear_composer_selection()
        self._composer_notice = ""
        if had_selection and snapshot is not None:
            self.render(snapshot)
        return had_selection

    def copy_composer_selection(self, snapshot=None):
        self._check_owner()
        value = self._composer_selection_text()
        if not value:
            return False
        if len(value) > self.HISTORY_COPY_CHAR_LIMIT:
            value = value[:self.HISTORY_COPY_CHAR_LIMIT]
            suffix = "（已截断）"
        else:
            suffix = ""
        self._composer_notice = self._clipboard_notice(
            self._copy_result(value, truncated=bool(suffix)))
        if snapshot is not None:
            self.render(snapshot)
        return True

    def _history_window(self, height, width):
        if (self._history_rows_snapshot is None
                or self._history_snapshot_width != width):
            if self._history_selection_anchor is not None:
                self._clear_history_selection()
            self._history_rows_snapshot = self._history_rows(width)
            self._history_snapshot_width = width
        rows = self._history_rows_snapshot
        if not rows:
            self._history_anchor_id = None
            self._history_window_start = 0
            self._history_window_end = 0
            return "  暂无可浏览的对话", []
        max_offset = max(0, len(rows) - max(1, height))
        self._history_offset = min(
            max(0, self._history_offset), max_offset)
        end = max(1, len(rows) - self._history_offset)
        start = max(0, end - max(1, height))
        visible = rows[start:end]
        self._history_window_start = start
        self._history_window_end = end

        anchors = []
        seen_user_entries = set()
        for index, (entry_id, kind, text) in enumerate(rows):
            if kind != "user" or entry_id in seen_user_entries:
                continue
            seen_user_entries.add(entry_id)
            if index < end:
                anchors.append((index, entry_id, text))
        if anchors:
            _, anchor_id, label = anchors[-1]
            self._history_anchor_id = anchor_id
            label = " ".join(label.split()) or "(empty prompt)"
        else:
            self._history_anchor_id = None
            label = "对话开始"
        unseen = (
            f" · +{self._history_unseen} live"
            if self._history_unseen else "")
        older = (
            " · 更早历史 ↑"
            if self._transcript_source_cursor > 0 else "")
        position = f"{start + 1}-{end}/{len(rows)}"
        controls = (
            "click 跳到输入 · PgUp/PgDn · Esc 返回"
            if self._mouse_enabled
            else "PgUp/PgDn · Esc 返回")
        if self.history_selection_active:
            controls += " · Ctrl-C 复制选区"
        if self._history_notice:
            controls += " · " + self._history_notice
        room = max(
            8,
            width - display_width(
                position + controls + unseen + older) - 10)
        label = truncate_display(label, room)
        header = (
            f"  ↑ {label} · {position}{older}{unseen} · {controls}")
        return truncate_display(header, width), visible

    def _history_row_style(self, kind):
        if not self.styled:
            return ""
        return {
            "user": "\x1b[1;38;5;214m",
            "assistant": "\x1b[38;5;252m",
            "tool": "\x1b[38;5;110m",
            "status": "\x1b[2;38;5;103m",
        }.get(kind, "\x1b[38;5;250m")

    def _render_history(self, snapshot):
        if not self._history_active:
            return
        snapshot = snapshot or self._last_snapshot
        width = max(20, _cols() - 1)
        frame_lines, frame_cursor_row, frame_cursor_col = (
            self._build_frame(snapshot, width))
        terminal_rows = max(5, _lines())
        if len(frame_lines) + 2 > terminal_rows:
            # A queue burst or resize can make a composer that fit
            # when browsing began consume the viewport later. Return
            # to live mode instead of placing the cursor off-screen.
            self.close_history(snapshot)
            return
        history_height = max(1, terminal_rows - len(frame_lines) - 1)
        header, visible = self._history_window(history_height, width)
        history_lines = []
        for offset, (_, kind, text) in enumerate(visible):
            row_index = self._history_window_start + offset
            history_lines.append(
                self._history_row_style(kind)
                + self._highlight_history_row(text, row_index, width)
                + ("\x1b[0m" if self.styled else ""))
        padding = max(0, history_height - len(history_lines))
        header_style = "\x1b[48;5;236;38;5;250m" if self.styled else ""
        reset = "\x1b[0m" if self.styled else ""
        lines = [header_style + header
                 + " " * max(0, width - display_width(header))
                 + reset]
        lines.extend(history_lines)
        lines.extend([""] * padding)
        frame_start = len(lines)
        lines.extend(frame_lines)
        cursor_row = frame_start + frame_cursor_row + 1
        cursor_col = frame_cursor_col + 1
        self._write(CSI + "2J" + CSI + "H")
        self._write("\r\n".join(lines[:terminal_rows]))
        self._write(f"{CSI}{cursor_row};{cursor_col}H")
        self.flush()
        self._history_header_row = 1

    def _ensure_history_offset(self, desired, height, width, *, home=False):
        """Page older canonical messages until ``desired`` can be displayed."""
        if home:
            self._prepend_source_entries(all_remaining=True, width=width)
        while True:
            if (self._history_rows_snapshot is None
                    or self._history_snapshot_width != width):
                self._history_rows_snapshot = self._history_rows(width)
                self._history_snapshot_width = width
            rows = self._history_rows_snapshot
            max_offset = max(0, len(rows) - max(1, height))
            if home or desired <= max_offset:
                return max_offset if home else desired
            if self._transcript_source_cursor <= 0:
                return desired
            missing = max(1, desired - max_offset)
            loaded = self._prepend_source_entries(
                entry_target=self.TRANSCRIPT_PAGE_ENTRIES,
                row_target=max(missing, height),
                width=width,
            )
            if not loaded:
                return desired

    def scroll_history(self, amount, snapshot=None):
        self._check_owner()
        return False                     # 内联模式没有历史浏览器：向上滚就是终端自己的 scrollback
        if not self._transcript:
            return False
        snapshot = snapshot or self._last_snapshot
        width = max(20, _cols() - 1)
        frame_lines, _, _ = self._build_frame(snapshot, width)
        if len(frame_lines) + 2 > _lines():
            return False
        page = max(3, _lines() // 2)
        if amount == "page_up":
            delta = page
        elif amount == "page_down":
            delta = -page
        elif amount == "home":
            delta = 0
        else:
            try:
                delta = int(amount)
            except (TypeError, ValueError):
                return self._history_active
        if not self._history_active:
            self.clear_spinner()
            self._history_active = True
            # The alternate transcript screen owns the pointer until it is
            # closed; never leave a live-composer selection armed behind it.
            self._clear_composer_selection()
            self._history_deferred = []
            self._history_deferred_chars = 0
            self._history_unseen = 0
            self._clear_history_selection()
            self._history_notice = ""
            self._history_rows_snapshot = self._history_rows(width)
            self._history_snapshot_width = width
            self._write(CSI + "?1049h")
            self._set_mouse_tracking("drag")
        history_height = max(1, _lines() - len(frame_lines) - 1)
        desired = max(0, self._history_offset + delta)
        if self._history_active and (delta != 0 or amount == "home"):
            self._clear_history_selection()
            self._history_notice = ""
        self._history_offset = self._ensure_history_offset(
            desired, history_height, width, home=(amount == "home"))
        if delta < 0 and self._history_offset == 0:
            self.close_history(snapshot)
            return False
        self._render_history(snapshot)
        return self._history_active

    def history_click(self, event, snapshot=None):
        """Backward-compatible name for history mouse handling."""
        return self.history_mouse(event, snapshot)

    def _defer_history(self, operation, chars=0):
        self._history_deferred.append(operation)
        self._history_deferred_chars += max(0, int(chars))
        if operation[0] != "finish":
            self._history_unseen += 1
        if (self._history_deferred_chars >= self.HISTORY_DEFER_LIMIT
                or len(self._history_deferred) >= 10_000):
            self.close_history(self._last_snapshot)
            return False
        return True

    def close_history(self, snapshot=None):
        self._check_owner()
        if not self._history_active:
            return False
        self._set_mouse_tracking("drag")
        deferred = self._history_deferred
        self._history_active = False
        self._history_offset = 0
        self._history_anchor_id = None
        self._history_header_row = None
        self._history_deferred = []
        self._history_deferred_chars = 0
        self._history_unseen = 0
        self._history_rows_snapshot = None
        self._history_snapshot_width = None
        self._history_window_start = 0
        self._history_window_end = 0
        self._clear_history_selection()
        self._history_notice = ""
        self._write(CSI + "?1049l")
        self.flush()
        self.clear_input()
        for operation in deferred:
            kind = operation[0]
            if kind == "write":
                _, value, flush, source = operation
                self._write_output_live(
                    value, flush=flush, source=source)
            elif kind == "finish":
                self._finish_output_line_live()
            elif kind == "commit":
                _, prompt, text, label = operation
                self._commit_input_live(prompt, text, label=label)
        target = snapshot or self._last_snapshot
        if target is not None:
            self.render(target)
        return True

    def clear_input(self):
        self._check_owner()
        # 帧被抹掉了，缓存的行不再对应屏幕上的东西。
        self._last_lines = None
        if self._picker_fullscreen:
            self._write(CSI + "?1049l")
            self.flush()
            self._picker_fullscreen = False
            self._drawn = False
            self._rows_before_cursor = 0
            return
        if self._history_active:
            return
        if self._drawn:
            self._write("\r")
            if self._rows_before_cursor:
                self._write(
                    f"{CSI}{self._rows_before_cursor}A")
            self._write(CSI + "J")
            self.flush()
            self._drawn = False
            self._rows_before_cursor = 0

    @staticmethod
    def _editor_layout(prompt, text, inner_width):
        """Return wrapped composer rows and every buffer-index position."""
        inner_width = max(4, int(inner_width))
        prompt = truncate_display(str(prompt), max(1, inner_width - 2))
        prompt_width = display_width(prompt)
        continuation = " " * prompt_width
        text = str(text)
        limit = max(1, inner_width - 1)  # retain one visible insertion cell
        rows = [[prompt, [], prompt_width]]
        positions = {0: (0, prompt_width)}
        joined = False
        regional_pending = False
        for index, char in enumerate(text):
            if char == "\n":
                rows.append([continuation, [], prompt_width])
                positions[index + 1] = (
                    len(rows) - 1, prompt_width)
                joined = False
                regional_pending = False
                continue
            current_width = rows[-1][2]
            if char == "\u200d":
                cell = 0
                joined = True
            else:
                regional = 0x1F1E6 <= ord(char) <= 0x1F1FF
                if regional:
                    if regional_pending:
                        cell = 0
                        regional_pending = False
                    else:
                        cell = 2
                        regional_pending = True
                else:
                    regional_pending = False
                    cell = _cell_width(char, joined=joined)
                joined = False
                if cell is None:
                    cell = 8 - (current_width % 8)
            if rows[-1][1] and current_width + cell > limit:
                rows.append([continuation, [], prompt_width])
                current_width = prompt_width
                positions[index] = (len(rows) - 1, current_width)
            rows[-1][1].append(char)
            rows[-1][2] += cell
            positions[index + 1] = (len(rows) - 1, rows[-1][2])
        rendered = [prefix + "".join(chars) for prefix, chars, _ in rows]
        return rendered, positions, prompt_width

    @staticmethod
    def _editor_rows(prompt, text, cursor, inner_width):
        """Wrap one composer draft and return rows plus cursor cell."""
        rendered, positions, prompt_width = TerminalRenderer._editor_layout(
            prompt, text, inner_width)
        cursor = min(max(0, int(cursor)), len(str(text)))
        return rendered, positions.get(cursor, (0, prompt_width))

    @staticmethod
    def _editor_index_at(prompt, text, row, column, inner_width):
        """Map a display-cell coordinate in the composer to a buffer index."""
        rendered, positions, prompt_width = TerminalRenderer._editor_layout(
            prompt, text, inner_width)
        del rendered
        text = str(text)
        row = int(row)
        column = max(0, int(column))
        candidates = sorted(
            (index, position[1])
            for index, position in positions.items()
            if position[0] == row)
        if not candidates:
            return 0 if row < 0 else len(text)
        if column <= candidates[0][1]:
            return candidates[0][0]
        for (index, left), (next_index, right) in zip(
                candidates, candidates[1:]):
            if column <= right:
                return index if column - left < right - column else next_index
        return candidates[-1][0]

    @staticmethod
    def _highlight_editor_rows(prompt, text, start, end, inner_width):
        """Highlight a buffer interval across wrapped composer rows."""
        rendered, positions, prompt_width = TerminalRenderer._editor_layout(
            prompt, text, inner_width)
        text = str(text)
        start = min(max(0, int(start)), len(text))
        end = min(max(0, int(end)), len(text))
        if start >= end:
            return rendered
        start_position = positions.get(start)
        end_position = positions.get(end)
        if start_position is None or end_position is None:
            return rendered
        first_row, start_column = start_position
        last_row, end_column = end_position
        if last_row < first_row:
            first_row, last_row = last_row, first_row
            start_column, end_column = end_column, start_column
        highlighted = list(rendered)
        for row in range(first_row, last_row + 1):
            if not 0 <= row < len(rendered):
                continue
            left = start_column if row == first_row else prompt_width
            right = end_column if row == last_row else display_width(
                rendered[row])
            if right > left:
                highlighted[row] = _highlight_display_cells(
                    rendered[row], left, right)
        return highlighted

    def _guided_option_lines(self, options, width, selected_index=0):
        """Render inline command options in display-cell bounded columns.

        The old formatter used ``command:<12`` and then reserved a fixed 24
        columns for the description.  That is only correct for short ASCII
        commands; a full ``/workflow ...`` label or CJK text could overrun a
        narrow terminal.  Compute the command column once per frame and use
        the same display-cell arithmetic as the rest of the renderer.
        """
        if not options:
            return []
        safe = [
            (self._transcript_plain(command),
             self._transcript_plain(description))
            for command, description in options
        ]
        # Keep a readable command column on normal terminals, while retaining
        # at least one cell for a description (or an ellipsis) on tiny ones.
        command_col = min(
            max(display_width(command) for command, _ in safe),
            max(1, width - 4),
            32,
        )
        fixed = command_col + 3       # marker, spaces around command column
        description_col = max(0, width - fixed)
        lines = []
        for index, (command, description) in enumerate(safe):
            selected = index == int(selected_index)
            prefix = "\x1b[7m>" if selected and self.styled else (
                ">" if selected else " ")
            suffix = "\x1b[0m" if selected and self.styled else ""
            command_cell = pad_display(command, command_col)
            line = f"{prefix} {command_cell}"
            if not description_col:
                lines.append(line + suffix)
                continue
            # 说明按列宽折行而不是截成省略号；最多三行，弹窗不能把屏幕撑满。
            # 说明列窄到放不下一个 CJK 字（<4 列）时折行没有意义，退回截断。
            parts = (
                wrap_display(description, description_col)
                if description_col >= 4
                else [truncate_display(description, description_col)])
            if len(parts) > 3:
                parts = parts[:2] + [
                    truncate_display(" ".join(parts[2:]), description_col)]
            dim_on = "\x1b[2m" if self.styled else ""
            dim_off = "\x1b[0m" if self.styled else ""
            lines.append(line + " " + dim_on + parts[0] + dim_off + suffix)
            for part in parts[1:]:
                lines.append(" " * fixed + dim_on + part + dim_off)
        return lines

    def _build_frame(self, snapshot, width):
        if snapshot is None:
            return [""], 0, 0
        width = max(20, int(width))
        metadata = " · ".join(
            " ".join(self._transcript_plain(value).split())
            for value in (snapshot.activity, compact_status(snapshot.status))
            if value)
        metadata_in_frame = False
        if snapshot.mode == "decision_gate":
            # A gate is deliberately a different visual state from a generic
            # picker: the question, context and selected consequence remain on
            # screen together, so the user can make an informed choice without
            # opening a second view.  All values are treated as untrusted text.
            border = "\x1b[38;5;240m" if self.styled else ""
            accent = "\x1b[38;5;214m" if self.styled else border
            dim = "\x1b[2m" if self.styled else ""
            reset = "\x1b[0m" if self.styled else ""
            inner = max(4, width - 4)

            def safe(value, limit=inner):
                # Gate cards are single-row records.  Collapse model-provided
                # newlines/control spacing before width arithmetic so one
                # hostile or verbose field cannot escape the box geometry.
                plain = self._transcript_plain(str(value or ""))
                return truncate_display(" ".join(plain.split()), limit)

            body = [
                ("⚑ 需要拍板", False),
            ]
            question_total = len(snapshot.modal_questions or ())
            if question_total > 1:
                body.append((safe(
                    f"问题 {int(snapshot.modal_question_index) + 1}/{question_total}"
                    f" · {snapshot.title}"), False))
            else:
                body.append((safe(snapshot.title), False))
            context = " ".join(
                self._transcript_plain(snapshot.modal_context).split())
            if context:
                body.append((safe("背景：" + context), True))
            body.append(("选项", True))
            details = tuple(snapshot.option_details or ())
            selected_labels = set(snapshot.modal_selected or ())
            current_multi = False
            if 0 <= int(snapshot.modal_question_index) < question_total:
                current_multi = bool(
                    (snapshot.modal_questions[int(snapshot.modal_question_index)]
                     or {}).get("multi_select"))
            for index, label in enumerate(snapshot.options):
                selected = index == int(snapshot.selected)
                mark = " ★" if (index < len(details)
                                and details[index].get("recommended")) else ""
                checked = (
                    "☑ " if label in selected_labels else "☐ ")
                prefix = "> " if selected else "  "
                if not current_multi:
                    checked = ""
                body.append((safe(
                    f"{prefix}{checked}{index + 1}. {label}{mark}"),
                             False))
                # Keep consequence/cost beside every choice.  This avoids the
                # Claude-like modal forcing the user to move the cursor just
                # to compare two otherwise identical labels.
                if index < len(details):
                    detail = details[index]
                    if detail.get("detail"):
                        body.append((safe("   ↳ " + str(detail["detail"])), True))
                    if detail.get("cost"):
                        body.append((safe("   · 代价：" + str(detail["cost"])), True))
            if snapshot.modal_error:
                body.append((safe("⚠ " + snapshot.modal_error), False))
            if metadata:
                body.append((safe("会话：" + metadata), True))
                metadata_in_frame = True
            if snapshot.queued:
                queued_hint = " · ".join(
                    " ".join(self._transcript_plain(str(item)).split())
                    for item in tuple(snapshot.queued)[:2])
                suffix = (
                    f" · 另有 {len(snapshot.queued) - 2} 条"
                    if len(snapshot.queued) > 2 else "")
                body.append((safe("排队：" + queued_hint + suffix), True))
            if snapshot.dock:
                body.append((safe(f"活动面板：{len(snapshot.dock)} 项"), True))
            controls = snapshot.controls or "↑↓ 选择 · Enter 确认 · Esc 取消"
            body.append((safe(controls), True))
            top = border + "╭" + "─" * (width - 2) + "╮" + reset
            bottom = border + "╰" + "─" * (width - 2) + "╯" + reset
            lines = [top]
            for index, (value, is_dim) in enumerate(body):
                style = dim if is_dim else ("\x1b[1m" if self.styled
                                             and index == 0 else "")
                value = style + value + (reset if style else "")
                padding = max(0, inner - display_width(value))
                lines.append(
                    accent + "│" + reset + " " + value
                    + " " * padding + " " + border + "│" + reset)
            lines.append(bottom)
            cursor_row, cursor_col = 0, 0
        elif snapshot.mode == "picker":
            safe_title = self._transcript_plain(snapshot.title)
            heading = f"  {safe_title}" if safe_title else "  选择"
            lines = [truncate_display(heading, width)]
            for index, option in enumerate(snapshot.options):
                option = self._transcript_plain(option)
                selected = index == snapshot.selected
                prefix = "\x1b[7m>" if selected and self.styled else (
                    ">" if selected else " ")
                suffix = "\x1b[0m" if selected and self.styled else ""
                lines.append(
                    f"{prefix} {truncate_display(option, width - 3)}{suffix}")
            controls = (
                snapshot.controls
                or "↑↓ 选择 · Enter 确认 · Esc 取消")
            if snapshot.hint:
                lines.append(truncate_display(
                    f"  {self._transcript_plain(snapshot.hint)}", width))
            for control_line in _wrap_control_lines(
                    controls, max(1, width - 2),
                    max_lines=_picker_control_line_limit()):
                lines.append(truncate_display(
                    f"  {control_line}", width))
            cursor_row, cursor_col = 0, 0
        else:
            queued = []
            for item in snapshot.queued:
                plain = self._transcript_plain(item).replace(
                    "\r", " ").replace("\n", " ")
                queued.append(
                    ("\x1b[2;38;5;103m" if self.styled else "")
                    + truncate_display("  " + plain, width)
                    + ("\x1b[0m" if self.styled else ""))
            dock = []
            for index, item in enumerate(snapshot.dock[:5]):
                value = item.text if isinstance(item, DockItem) else item
                plain = self._transcript_plain(value).replace(
                    "\r", " ").replace("\n", " ")
                style = (
                    "\x1b[38;5;75m" if self.styled and index == 0
                    else ("\x1b[2;38;5;103m" if self.styled else ""))
                dock.append(
                    style + truncate_display("  " + plain, width)
                    + ("\x1b[0m" if self.styled else ""))
            title = []
            if snapshot.mode == "prompt" and snapshot.title:
                safe_title = self._transcript_plain(snapshot.title)
                title.append(
                    ("\x1b[1;38;5;250m" if self.styled else "")
                    + truncate_display("  " + safe_title, width)
                    + ("\x1b[0m" if self.styled else ""))
            border = "\x1b[38;5;240m" if self.styled else ""
            accent = (
                "\x1b[38;5;214m" if self.styled and snapshot.active
                else border)
            reset = "\x1b[0m" if self.styled else ""
            inner_width = max(4, width - 4)
            editor_prompt = str(snapshot.prompt)
            if not self.styled:
                editor_prompt = _ANSI.sub("", editor_prompt)
            raw_text = str(snapshot.text)
            safe_text = self._transcript_plain(raw_text)
            safe_cursor = len(self._transcript_plain(
                raw_text[:snapshot.cursor]))
            editor_rows, cursor = self._editor_rows(
                editor_prompt, safe_text,
                safe_cursor, inner_width)
            if (not safe_text and not snapshot.busy and snapshot.mode == "line"
                    and editor_rows and snapshot.target is None):
                # 像 Claude Code：草稿为空时给一行淡色占位，光标仍停在提示符后
                hint = COMPOSER_PLACEHOLDER
                room = inner_width - display_width(editor_prompt) - 1
                if room > 8:
                    if display_width(hint) > room:
                        hint = truncate_display(hint, room)
                    dim = "\x1b[2m" if self.styled else ""
                    reset = "\x1b[0m" if self.styled else ""
                    editor_rows[0] = editor_prompt + dim + hint + reset
            selection_bounds = self._composer_selection_bounds(safe_text)
            if selection_bounds is not None:
                # Use a plain prompt for the selected frame.  This keeps the
                # inverse-video span independent of ANSI byte offsets while
                # preserving exact display-cell geometry.
                editor_rows = self._highlight_editor_rows(
                    _ANSI.sub("", editor_prompt), safe_text,
                    selection_bounds[0], selection_bounds[1], inner_width)
            top = border + "╭" + "─" * (width - 2) + "╮" + reset
            bottom = border + "╰" + "─" * (width - 2) + "╯" + reset
            boxed = [top]
            for value in editor_rows:
                padding = max(0, inner_width - display_width(value))
                boxed.append(
                    accent + "│" + reset + " " + value
                    + " " * padding + " " + border + "│" + reset)
            boxed.append(bottom)
            lines = queued + dock + title + boxed
            cursor_row = (
                len(queued) + len(dock) + len(title) + 1 + cursor[0])
            cursor_col = 2 + cursor[1]
            lines.extend(self._guided_option_lines(
                snapshot.options, width, snapshot.selected))
            if snapshot.hint:
                lines.append(
                    ("  \x1b[2;38;5;103m" if self.styled else "  ")
                    + truncate_display(
                        self._transcript_plain(snapshot.hint), width - 2)
                    + ("\x1b[0m" if self.styled else ""))
            if (self._composer_selection_anchor is not None
                    or self._composer_notice):
                selection_hint = "  选区 · Ctrl-C 复制选区 · Esc 清除"
                if self._composer_notice:
                    selection_hint += " · " + self._composer_notice
                lines.append(
                    ("  \x1b[2;38;5;103m" if self.styled else "  ")
                    + truncate_display(selection_hint, width - 2)
                    + ("\x1b[0m" if self.styled else ""))
        if metadata and not metadata_in_frame:
            lines.append(
                ("\x1b[2;38;5;103m" if self.styled else "")
                + truncate_display("  " + metadata, width)
                + ("\x1b[0m" if self.styled else ""))
        return lines, cursor_row, cursor_col

    def _validate_composer_selection(self, snapshot, width):
        """Drop a selection when the frame it refers to is no longer stable."""
        if self._composer_selection_anchor is None:
            return
        if (snapshot is None or snapshot.mode != "line"
                or not snapshot.active):
            self._clear_composer_selection()
            return
        safe_text = self._transcript_plain(str(snapshot.text))
        current_key = self._composer_layout_key(
            snapshot, width, safe_text)
        if self._composer_selection_layout_key != current_key:
            self._clear_composer_selection()
            return
        if (not self._composer_selection_dragging
                and self._composer_selection_cursor is not None):
            safe_cursor = len(self._transcript_plain(
                str(snapshot.text)[:max(
                    0, min(len(str(snapshot.text)), int(snapshot.cursor)))]))
            if safe_cursor != self._composer_selection_cursor:
                self._clear_composer_selection()

    def _maybe_query_cursor_position(self, snapshot, width, cursor_row,
                                     cursor_col, lines, *, fullscreen=False):
        """Ask the terminal for the absolute composer cursor row sparingly."""
        if (not self._mouse_enabled or fullscreen
                or snapshot is None or snapshot.mode != "line"):
            return
        signature = (
            int(width), int(cursor_row), int(cursor_col), len(lines),
            str(snapshot.prompt), str(snapshot.text), int(snapshot.cursor),
            tuple(snapshot.queued), tuple(snapshot.dock[:5]),
        )
        now = time.monotonic()
        if (signature == self._last_mouse_query_signature
                and now - self._last_mouse_query_at < 0.25):
            return
        self._write(CSI + "6n")
        self._last_mouse_query_signature = signature
        self._last_mouse_query_at = now

    # 输入框闪烁的由来（2026-09-18 实测）：每敲一个字符，这里原本都要
    # 「整帧擦掉再重画」—— clear_input() 发 ESC[J 抹掉整个输入区，再重写边框、
    # 提示符和输入行。进程内直接量 TerminalRenderer 写出的字节（FakeTTY + 同一帧
    # 只改一个字符）：整帧重画 **323 字节 / 13 条转义序列，含全区擦除 ESC[J**；
    # 增量重绘 **119 字节 / 6 条，无 ESC[J**。终端会把中间那些
    # 「已擦掉、还没画上」的状态显示出来，看起来就是输入框在闪。
    #
    # 试过标准解法「把整帧包进 DECSET 2026（同步输出）」，**在内联路径上不可用**：
    # ConPTY（Windows Terminal 走的就是它）收到 2026h 之后会把输出一直扣住到
    # 2026l，test_mode_cycle_pty / test_tui_pty 的标记等待集体超时（8 个用例）——
    # 等于拿闪烁换「界面更新被推迟」。结论记在这里，别再试第二遍。
    #
    # 采用的办法是**别整帧重画**：帧结构没变（行数一致）时，只重写与上一帧
    # 不同的那几行。打字时变的只有输入行，边框一个字节都不用动。
    def _render_incremental(self, lines, cursor_row, cursor_col):
        """只重写变化的行；做不到就返回 False，让调用方走整帧重画。

        前提条件都是为了「不和别的写入者抢光标」：必须已经画过一帧、行数一致、
        没有 spinner、没有半行模型输出挂在上面。任何一条不满足就老实重画。
        """
        previous = self._last_lines
        if (not self._drawn or previous is None
                or len(previous) != len(lines)
                or self._spinner_visible
                or self._output_partial_col is not None):
            return False
        changed = [i for i, line in enumerate(lines) if line != previous[i]]
        if not changed:
            # 只是光标动了（左右移动、点选）：连一行都不用重写。
            self._move_within_frame(self._rows_before_cursor, cursor_row)
            self._write("\r" + (f"{CSI}{cursor_col}C" if cursor_col else ""))
            self.flush()
            self._rows_before_cursor = cursor_row
            self._last_lines = list(lines)
            return True
        row = self._rows_before_cursor
        for index in changed:
            self._move_within_frame(row, index)
            row = index
            # ESC[K 只擦这一行的尾巴：比 ESC[J 抹掉整片安静得多。
            self._write("\r" + lines[index] + CSI + "K")
        self._move_within_frame(row, cursor_row)
        self._write("\r" + (f"{CSI}{cursor_col}C" if cursor_col else ""))
        self.flush()
        self._rows_before_cursor = cursor_row
        self._last_lines = list(lines)
        return True

    def _move_within_frame(self, from_row, to_row):
        if to_row > from_row:
            self._write(f"{CSI}{to_row - from_row}B")
        elif to_row < from_row:
            self._write(f"{CSI}{from_row - to_row}A")

    def render(self, snapshot):
        self._check_owner()
        self._last_snapshot = snapshot
        if self._history_active:
            self._render_history(snapshot)
            return
        fullscreen = bool(
            snapshot is not None
            and snapshot.mode == "picker"
            and snapshot.fullscreen)
        incremental = (
            not fullscreen and not self._picker_fullscreen
            and snapshot is not None
            and not (snapshot.mode == "line" and not snapshot.active))
        if incremental:
            width = max(20, _cols() - 1)
            self._validate_composer_selection(snapshot, width)
            lines, cursor_row, cursor_col = self._build_frame(snapshot, width)
            if self._render_incremental(lines, cursor_row, cursor_col):
                return
        if fullscreen and self._picker_fullscreen:
            # Redraw in place inside the alternate screen. Exiting/re-entering
            # it for every arrow or wheel event causes a visible flash and can
            # restore stale base-screen cursor state.
            self._drawn = False
            self._rows_before_cursor = 0
        else:
            self.clear_input()
        self.clear_spinner()
        if snapshot is None or (
                snapshot.mode == "line" and not snapshot.active):
            self._clear_composer_selection()
            self._last_lines = None
            return
        width = max(20, _cols() - 1)
        self._validate_composer_selection(snapshot, width)
        lines, cursor_row, cursor_col = self._build_frame(snapshot, width)
        self._last_lines = list(lines)
        if fullscreen:
            if not self._picker_fullscreen:
                self._write(CSI + "?1049h")
                self._picker_fullscreen = True
            self._write(CSI + "2J" + CSI + "H")
        else:
            self._write("\r" + CSI + "J")
        self._write("\r\n".join(lines))
        rows_after_cursor = len(lines) - 1 - cursor_row
        if rows_after_cursor:
            self._write(f"{CSI}{rows_after_cursor}A")
        self._write("\r" + (f"{CSI}{cursor_col}C" if cursor_col else ""))
        self._maybe_query_cursor_position(
            snapshot, width, cursor_row, cursor_col, lines,
            fullscreen=fullscreen)
        self.flush()
        self._drawn = True
        self._rows_before_cursor = cursor_row

    @staticmethod
    def _output_kind(source):
        source = str(source or "")
        if source == "model":
            return "assistant"
        if source == "tool" or source.startswith("task:"):
            return "tool"
        if source == "status":
            return "status"
        return "output"

    def _commit_input_live(self, prompt, text, *, label=None):
        self._finish_output_line_live()
        self.clear_input()
        self.clear_spinner()
        safe_text = self._transcript_plain(text)
        safe_label = self._transcript_plain(label) if label is not None else None
        shown = (
            f"{prompt}{safe_text}"
            if safe_label is None else f"{safe_label} {safe_text}")
        if not self.styled:
            shown = _ANSI.sub("", shown)
        # 像 Claude Code：用户消息是一条整行铺底色的横条 `› 文本`，不画框；多行时每行都铺底。
        band, reset = user_line_band(self.styled)
        width = max(20, _cols() - 1)
        body = str(shown).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines = []
        for index, value in enumerate(body):
            text = value if index == 0 else "  " + value
            pad = " " * max(0, width - display_width(text))
            lines.append(band + text + pad + reset)
        self._write("\r\n" + "\r\n".join(lines) + "\r\n")
        self.flush()

    def commit_input(self, prompt, text, *, label=None):
        self._check_owner()
        transcript_text = f"› {text}" if label is None else f"{label} {text}"
        self._record_transcript("user", transcript_text)
        if self._history_active:
            self._defer_history(
                ("commit", prompt, text, label), len(str(text)))
            return
        self._commit_input_live(prompt, text, label=label)

    def _resume_output_cursor(self):
        """从 composer 顶部回到上一行未结束的流式输出。"""
        column = self._output_partial_col
        if column is None:
            return 0
        self._write(f"{CSI}1A\r")
        if column:
            self._write(f"{CSI}{column}C")
        self._output_partial_col = None
        return column

    def _write_output_live(self, text, *, flush=True, source=None):
        self.clear_input()
        self.clear_spinner()
        if (self._output_partial_col is not None
                and source != self._output_source):
            # 非换行 chunk 只可由同一逻辑 stream 续写。model、foreground
            # task、attached background task 与 status 之间切换时先收束上一行，
            # 否则会出现 assistant文本TASK_OUTPUT 或状态块覆盖上一行。
            self._resume_output_cursor()
            self._write("\r\n")
            self._output_source = None
        # A Markdown delimiter may arrive in its own provider delta.  In that
        # case StreamingMarkdown emits only ANSI style transitions (for
        # example BOLD + RESET) and no visible cells.  We still move back to
        # the unfinished output line before writing it, so remember that this
        # was a real continuation independently of its column: a full-width
        # partial line legitimately has column 0.
        had_partial = self._output_partial_col is not None
        previous_column = self._resume_output_cursor()
        value = str(text)
        self._write(value)

        plain = _ANSI.sub("", value)
        if not plain:
            if had_partial:
                # Keep the composer one row below the unfinished model line.
                # Dropping this state lets the next render() clear that model
                # line with CSI J, which visibly swallows everything before
                # an inline **bold** or `code` delimiter split across deltas.
                self._output_partial_col = previous_column
                self._output_source = source
                self._write("\r\n")
            if flush:
                self.flush()
            return
        if "\n" in plain or "\r" in plain:
            tail = re.split(r"[\r\n]", plain)[-1]
            column = display_width(tail)
        else:
            column = previous_column + display_width(plain)
        width = max(1, _cols())
        column %= width
        if plain and not plain.endswith("\n"):
            # Composer 必须从下一行开始；后续 output 会先上移一行续写。
            # 恰好写满一整行时 column=0，但终端仍处于待换行边界，不能把 0
            # 当成“已经换行”；None 才表示没有 continuation。
            self._output_partial_col = column
            self._output_source = source
            self._write("\r\n")
        else:
            self._output_source = None
        if flush:
            self.flush()

    def write_output(self, text, *, flush=True, source=None):
        self._check_owner()
        value = str(text)
        self._record_transcript(
            self._output_kind(source), value,
            source=source, merge=True)
        if self._history_active:
            self._defer_history(
                ("write", value, bool(flush), source), len(value))
            return
        self._write_output_live(value, flush=flush, source=source)

    def _finish_output_line_live(self):
        if self._output_partial_col is None:
            return False
        self.clear_input()
        self.clear_spinner()
        self._resume_output_cursor()
        self._write("\r\n")
        self._output_source = None
        self.flush()
        return True

    def finish_output_line(self):
        """结束仍未换行的流式输出，并清除 continuation 状态。"""
        self._check_owner()
        if self._history_active:
            self._defer_history(("finish",))
            return True
        return self._finish_output_line_live()

    def spinner(self, label, elapsed):
        self._check_owner()
        if self._drawn or self._history_active:
            return
        frames = "·✢✳✶✻✽✻✶✳✢"          # Claude Code 的脉冲，不用盲文转轮
        frame_index = int(elapsed / 0.25) % len(frames)
        key = (str(label), frame_index, int(elapsed))
        if key == self._spinner_key:
            return
        frame = frames[frame_index]
        self._write(
            f"\r{CSI}2K{frame} {label} {elapsed:.0f}s · esc 中断")
        self.flush()
        self._spinner_visible = True
        self._spinner_key = key

    def bell(self):
        """响一声提醒用户（后台任务、失败、turn 完成 —— SPEC-CC-parity H1）。

        只在 tty 上发：管道、-p、测试里的 StringIO 一律静默，否则 \\a 会污染被
        重定向的输出。返回是否真的发了。
        """
        self._check_owner()
        if not self.is_tty:
            return False
        self._write("\a")
        self.flush()
        return True

    def clear_spinner(self):
        self._check_owner()
        if self._spinner_visible:
            self._write(f"\r{CSI}2K")
            self.flush()
            self._spinner_visible = False
        self._spinner_key = None


# ------------------------------------------------------------------ Esc 中断（旧同步路径兼容）

class EscWatcher:
    """流式输出期间在后台盯着 stdin，按 Esc（或 Ctrl-C）就置位。

    这是 Esc 能中断的关键：主线程忙于收 HTTP 流，没人读键盘。开一个线程在
    raw mode 下轮询 stdin，把「用户想停」变成一个可检查的 Event。
    """

    def __init__(self):
        self.event = threading.Event()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._t = None
        self._raw = None

    def __enter__(self):
        if not supported():
            return self
        # 流式输出期间终端还要正常打印模型的文本，所以必须 cbreak 而非 raw。
        self._raw = raw_mode(cbreak=True)
        self._raw.__enter__()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            if self._paused.is_set():
                # 暂停期间**绝不能读 stdin** —— 否则会和权限菜单、选择器
                # 抢同一个输入源，把用户的方向键吞掉。
                self._stop.wait(0.05)
                continue
            try:
                ch = _raw_read(1, 0.1)
                if ch:
                    if ch[0] in ("\x1b", "\x03"):
                        self.event.set()
                        return
            except (OSError, ValueError):
                return

    def __exit__(self, *a):
        self._stop.set()
        if self._t:
            self._t.join(timeout=0.3)
        if self._raw:
            self._raw.__exit__()

    def pause(self):
        """让出 stdin，并把终端还原成正常模式，供交互组件使用。"""
        self._paused.set()
        time.sleep(0.06)              # 等监听线程走完当前一轮 select
        if self._raw:
            self._raw.__exit__()

    def resume(self):
        if self._raw:
            self._raw.__enter__()
        self._paused.clear()

    @property
    def interrupted(self):
        return self.event.is_set()
