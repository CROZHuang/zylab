"""单元格缓冲与差分渲染（DESIGN-TUI-app §3 的 core/screen.py）。

借鉴 ratatui 的 Buffer::diff：view 每帧产出一个完整的 Buffer，渲染器只把与上一帧不同的行段发给终端，
包在同步输出（DECSET 2026）里，重绘期间隐藏光标。宽字符（CJK/emoji）占两格：第二格是续格
（ch == ""），改写从续格开始时回退到宽字符的首格，避免把半个字打在屏上。

这里不认识任何 widget，也不读 stdin；纯函数 + 纯数据，方便测试。
"""
from typing import NamedTuple
from .tui import display_width

ESC = "\x1b"
CSI = ESC + "["
SYNC_ON, SYNC_OFF = CSI + "?2026h", CSI + "?2026l"
CURSOR_HIDE, CURSOR_SHOW = CSI + "?25l", CSI + "?25h"


class Style(NamedTuple):
    """NamedTuple 而不是 dataclass：差分要比较上万个格子，tuple 的相等在 C 里跑。"""
    fg: object = None          # None | 0..255 | (r, g, b)
    bg: object = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    link: str = ""             # OSC 8 超链接

    def merge(self, **kw):
        return self._replace(**kw)


DEFAULT = Style()


def _color_code(value, *, background, truecolor):
    base = 48 if background else 38
    if value is None:
        return str(base + 1)                       # 39 / 49：默认色
    if isinstance(value, tuple):
        r, g, b = value
        if truecolor:
            return f"{base};2;{r};{g};{b}"
        return f"{base};5;{_nearest_256(r, g, b)}"
    return f"{base};5;{int(value)}"


def _nearest_256(r, g, b):
    """真彩 → 256 色立方体（灰阶单独处理），供不支持真彩的终端回退。"""
    if abs(r - g) < 12 and abs(g - b) < 12:
        gray = round((r + g + b) / 3)
        if gray < 8:
            return 16
        if gray > 238:
            return 231
        return 232 + round((gray - 8) / 247 * 23)
    def level(v):
        return 0 if v < 48 else 1 + round((v - 48) / 41.6) if v < 255 else 5
    return 16 + 36 * min(5, level(r)) + 6 * min(5, level(g)) + min(5, level(b))


def sgr_transition(prev, nxt, *, truecolor=True):
    """从样式 prev 切到 nxt 需要发的最短序列（含 OSC 8 链接的开关）。"""
    if prev == nxt:
        return ""
    out = []
    # 属性只能减不能"关一个"（除了各自的关闭码），所以任何属性被关掉时整体 reset 再重放
    attrs_off = (prev.bold and not nxt.bold) or (prev.dim and not nxt.dim) or (prev.italic and not nxt.italic) \
        or (prev.underline and not nxt.underline) or (prev.reverse and not nxt.reverse)
    codes = []
    base = DEFAULT if attrs_off else prev
    if attrs_off:
        codes.append("0")
    if nxt.bold and not base.bold:
        codes.append("1")
    if nxt.dim and not base.dim:
        codes.append("2")
    if nxt.italic and not base.italic:
        codes.append("3")
    if nxt.underline and not base.underline:
        codes.append("4")
    if nxt.reverse and not base.reverse:
        codes.append("7")
    if nxt.fg != base.fg or (attrs_off and nxt.fg is not None):
        codes.append(_color_code(nxt.fg, background=False, truecolor=truecolor))
    if nxt.bg != base.bg or (attrs_off and nxt.bg is not None):
        codes.append(_color_code(nxt.bg, background=True, truecolor=truecolor))
    if codes:
        out.append(CSI + ";".join(codes) + "m")
    if prev.link != nxt.link:
        out.append(f"{ESC}]8;;{nxt.link}{ESC}\\")
    return "".join(out)


class Cell(NamedTuple):
    ch: str = " "
    style: Style = DEFAULT
    wide: bool = False         # 首格且占两列

    @property
    def continuation(self):
        return self.ch == ""


BLANK = Cell()
CONT = Cell(ch="")


def _clean(ch):
    """控制字符不上屏（防终端注入），用空格顶位。"""
    return " " if (ord(ch) < 0x20 or 0x7f <= ord(ch) < 0xa0) else ch


class Buffer:
    """rows × cols 的单元格网格。坐标从 0 起。"""

    def __init__(self, rows, cols, fill=BLANK):
        self.rows, self.cols = max(1, int(rows)), max(1, int(cols))
        self.cells = [[fill] * self.cols for _ in range(self.rows)]

    def get(self, row, col):
        return self.cells[row][col]

    def put(self, row, col, text, style=DEFAULT):
        """从 (row, col) 起写文本，超出右边裁掉；返回写完后的列。宽字符放不下时用空格顶位。"""
        if not (0 <= row < self.rows):
            return col
        line = self.cells[row]
        cols = self.cols
        for ch in str(text):
            if col >= cols:
                break
            if col < 0:
                col += 1
                continue
            if " " <= ch < "\x7f":                 # ASCII 可见：宽 1，不进正则
                if line[col].ch == "" or line[col].wide:
                    self._clear_wide_overlap(line, col)
                line[col] = Cell(ch, style)
                col += 1
                continue
            ch = _clean(ch)
            w = display_width(ch)
            if w <= 0:
                continue                           # 零宽/组合字符：不占格（简化：丢弃）
            if w == 2:
                if col + 1 >= self.cols:
                    line[col] = Cell(" ", style)
                    col += 1
                    break
                self._clear_wide_overlap(line, col)
                self._clear_wide_overlap(line, col + 1)
                line[col] = Cell(ch, style, wide=True)
                line[col + 1] = Cell("", style)
                col += 2
            else:
                self._clear_wide_overlap(line, col)
                line[col] = Cell(ch, style)
                col += 1
        return col

    @staticmethod
    def _clear_wide_overlap(line, col):
        """覆盖一个宽字符的任一格时，把它整个抹掉，别留半个。"""
        cell = line[col]
        if cell.wide and col + 1 < len(line):
            line[col + 1] = BLANK
        elif cell.continuation and col - 1 >= 0:
            line[col - 1] = BLANK

    def fill_row(self, row, style=DEFAULT, ch=" "):
        if 0 <= row < self.rows:
            self.cells[row] = [Cell(ch, style)] * self.cols

    def text(self, row):
        """一行的纯文本（测试与退出时的转录用）。"""
        return "".join(c.ch for c in self.cells[row] if not c.continuation).rstrip()

    def dump(self):
        return "\n".join(self.text(r) for r in range(self.rows))


def _row_diff_span(old, new):
    """一行里第一个和最后一个不同格；没有差异返回 None。起点落在续格时回退到宽字符首格。"""
    first = last = None
    for i, (a, b) in enumerate(zip(old, new)):
        if a != b:
            if first is None:
                first = i
            last = i
    if first is None:
        return None
    if new[first].continuation and first > 0:
        first -= 1
    if last + 1 < len(new) and new[last].wide:
        last += 1
    return first, last


class Renderer:
    """把 Buffer 差异变成终端字节。状态：上一帧、上一次的样式、光标。"""

    def __init__(self, *, truecolor=True, sync=True):
        self.truecolor = truecolor
        self.sync = sync
        self.prev = None
        self._style = DEFAULT

    def reset(self):
        self.prev = None
        self._style = DEFAULT

    def render(self, frame, cursor=None):
        """返回要写给终端的字符串。cursor=(row, col) 是重绘后光标停的位置（None 则隐藏）。"""
        out = []
        full = self.prev is None or self.prev.rows != frame.rows or self.prev.cols != frame.cols
        if self.sync:
            out.append(SYNC_ON)
        out.append(CURSOR_HIDE)
        if full:
            out.append(CSI + "2J")
            self._style = DEFAULT
            out.append(sgr_transition(Style(bold=True), DEFAULT, truecolor=self.truecolor))   # 强制 reset
        for r in range(frame.rows):
            new = frame.cells[r]
            if full:
                span = (0, frame.cols - 1)
                # 全画时右侧全是空白格的行可以提前截断
                last = frame.cols - 1
                while last >= 0 and new[last] == BLANK:
                    last -= 1
                if last < 0:
                    continue
                span = (0, last)
            else:
                old = self.prev.cells[r]
                if old == new:                     # 列表相等在 C 里跑：流式时大多数行原样
                    continue
                span = _row_diff_span(old, new)
                if span is None:
                    continue
            first, last = span
            out.append(f"{CSI}{r + 1};{first + 1}H")
            c = first
            while c <= last:
                cell = new[c]
                if cell.continuation:
                    c += 1
                    continue
                out.append(sgr_transition(self._style, cell.style, truecolor=self.truecolor))
                self._style = cell.style
                out.append(cell.ch)
                c += 2 if cell.wide else 1
        # 收尾：样式归零、光标
        out.append(sgr_transition(self._style, DEFAULT, truecolor=self.truecolor))
        self._style = DEFAULT
        if cursor is not None:
            row, col = cursor
            out.append(f"{CSI}{max(0, row) + 1};{max(0, col) + 1}H")
            out.append(CURSOR_SHOW)
        if self.sync:
            out.append(SYNC_OFF)
        self.prev = frame
        # 无差异且不换光标：什么都不发
        payload = "".join(out)
        if not full and payload == (SYNC_ON if self.sync else "") + CURSOR_HIDE + (
                (f"{CSI}{cursor[0] + 1};{cursor[1] + 1}H{CURSOR_SHOW}") if cursor is not None else "") + (SYNC_OFF if self.sync else ""):
            return payload if cursor is not None and cursor != getattr(self, "_cursor", None) else ""
        self._cursor = cursor
        return payload
