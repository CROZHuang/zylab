"""全屏 TUI 的视图层零件（DESIGN-TUI-app §3 的 core/views.py）。

- parse_ansi：把带 SGR / OSC 8 的字符串拆成 (文本, Style) 段——StreamingMarkdown、_build_frame 的输出
  都是 ANSI 字符串，靠这个进单元格缓冲，两者一行不改。
- wrap_segments：按显示宽度硬折行（宽字符不拆半）。
- Transcript：逻辑行（段列表）+ 未换行的流式尾巴；按宽度缓存视觉行；滚动偏移（从底部数）；
  选区（视觉坐标）→ 纯文本。
"""
import re
from dataclasses import dataclass, field
from .screen import Style, DEFAULT, Buffer as _Buffer
from .tui import display_width

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_OSC8 = re.compile(r"\x1b\]8;[^;]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_OTHER = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]")
_BASIC = {30: 0, 31: 1, 32: 2, 33: 3, 34: 4, 35: 5, 36: 6, 37: 7,
          90: 8, 91: 9, 92: 10, 93: 11, 94: 12, 95: 13, 96: 14, 97: 15}


def _apply_sgr(style, params):
    if not params:
        return DEFAULT
    codes = [int(p) if p else 0 for p in params.split(";")]
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            style = Style(link=style.link)
        elif c == 1:
            style = style.merge(bold=True)
        elif c == 2:
            style = style.merge(dim=True)
        elif c == 3:
            style = style.merge(italic=True)
        elif c == 4:
            style = style.merge(underline=True)
        elif c == 7:
            style = style.merge(reverse=True)
        elif c == 22:
            style = style.merge(bold=False, dim=False)
        elif c == 23:
            style = style.merge(italic=False)
        elif c == 24:
            style = style.merge(underline=False)
        elif c == 27:
            style = style.merge(reverse=False)
        elif c in _BASIC:
            style = style.merge(fg=_BASIC[c])
        elif 40 <= c <= 47 or 100 <= c <= 107:
            style = style.merge(bg=_BASIC[c - 10])
        elif c == 39:
            style = style.merge(fg=None)
        elif c == 49:
            style = style.merge(bg=None)
        elif c in (38, 48) and i + 1 < len(codes):
            which = "fg" if c == 38 else "bg"
            if codes[i + 1] == 5 and i + 2 < len(codes):
                style = style.merge(**{which: codes[i + 2]}); i += 2
            elif codes[i + 1] == 2 and i + 4 < len(codes):
                style = style.merge(**{which: (codes[i + 2], codes[i + 3], codes[i + 4])}); i += 4
        i += 1
    return style


def parse_ansi(text, style=DEFAULT):
    """→ [(text, Style), …]，丢掉不认识的控制序列。段里不含换行；调用方先按行拆。"""
    out = []
    pos = 0
    text = str(text)
    pattern = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^;]*;[^\x07\x1b]*(?:\x07|\x1b\\)|" + _OTHER.pattern)
    for m in pattern.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], style))
        seq = m.group(0)
        sgr = _SGR.fullmatch(seq)
        osc = _OSC8.fullmatch(seq)
        if sgr:
            style = _apply_sgr(style, sgr.group(1))
        elif osc:
            style = style.merge(link=osc.group(1))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], style))
    return [(t, s) for t, s in out if t]


def plain(segments):
    return "".join(t for t, _ in segments)


def seg_width(segments):
    return sum(display_width(t) for t, _ in segments)


def wrap_segments(segments, width):
    """按显示宽度硬折行；返回视觉行列表（每行是段列表）。空输入 → 一个空行。"""
    width = max(1, int(width))
    rows, row, col = [], [], 0
    for text, style in segments:
        if text.isascii() and col + len(text) <= width:    # 整段放得下：不逐字
            row.append((text, style))
            col += len(text)
            continue
        buf = ""
        for ch in text:
            w = 1 if " " <= ch < "\x7f" else display_width(ch)
            if col + w > width and col > 0:
                if buf:
                    row.append((buf, style)); buf = ""
                rows.append(row); row, col = [], 0
            buf += ch
            col += w
        if buf:
            row.append((buf, style))
    rows.append(row)
    return rows


@dataclass
class Transcript:
    """逻辑行 + 未换行尾巴；视觉行按宽度缓存。"""
    lines: list = field(default_factory=list)      # 每项：段列表
    partial: str = ""                              # 未换行的原始 ANSI 文本
    partial_style: Style = DEFAULT
    _cache_width: int = 0
    _cache: list = field(default_factory=list)     # 视觉行列表
    _cache_upto: int = 0                           # 已缓存的逻辑行数

    def append_text(self, text):
        """流式追加（可含 \\r\\n / \\n）；未换行部分留在 partial。"""
        text = str(text).replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in text:
            head, text = text.split("\n", 1)
            self._push_line(self.partial + head)
            self.partial = ""
        self.partial += text
        self._invalidate_tail()

    def finish_line(self):
        if self.partial:
            self._push_line(self.partial)
            self.partial = ""
            self._invalidate_tail()

    def add_line(self, segments):
        self.finish_line()
        self.lines.append(list(segments))
        self._invalidate_tail()

    def _push_line(self, raw):
        segs = parse_ansi(raw, self.partial_style)
        self.partial_style = segs[-1][1] if segs else self.partial_style
        self.lines.append(segs)

    def _invalidate_tail(self):
        self._cache_upto = min(self._cache_upto, len(self.lines))

    def _ensure_cache(self, width):
        if width != self._cache_width:
            self._cache, self._cache_upto, self._cache_width = [], 0, width
        for line in self.lines[self._cache_upto:]:
            self._cache.extend(wrap_segments(line, width))
        self._cache_upto = len(self.lines)
        tail = wrap_segments(parse_ansi(self.partial, self.partial_style), width) if self.partial else []
        return tail

    def visual_rows(self, width):
        """全部视觉行（含 partial 那行）。会拷贝一次；热路径用 window()。"""
        tail = self._ensure_cache(width)
        return self._cache + tail

    def row_count(self, width):
        tail = self._ensure_cache(width)          # 先刷新，再数（原来先数后刷新，改宽后会把可滚范围算成 0）
        return len(self._cache) + len(tail)

    def window(self, width, height, offset):
        """不拷贝整个缓存：直接切出从底部数 offset 行起的 height 行。返回 (行, 实际偏移, 窗口起点)。"""
        tail = self._ensure_cache(width)
        total = len(self._cache) + len(tail)
        offset = max(0, min(int(offset), max(0, total - height)))
        end = total - offset
        start = max(0, end - height)
        if start >= len(self._cache):
            rows = tail[start - len(self._cache):end - len(self._cache)]
        elif end <= len(self._cache):
            rows = self._cache[start:end]
        else:
            rows = self._cache[start:] + tail[:end - len(self._cache)]
        return rows, offset, start

    def plain_text(self):
        out = [plain(l) for l in self.lines]
        if self.partial:
            out.append(plain(parse_ansi(self.partial)))
        return "\n".join(out)


def visible_window(rows, height, offset):
    """从底部数 offset 行开始，取 height 行；返回 (行列表, 实际偏移)。"""
    total = len(rows)
    offset = max(0, min(int(offset), max(0, total - height)))
    end = total - offset
    start = max(0, end - height)
    return rows[start:end], offset


def selection_text(rows, start, end):
    """视觉坐标 (row, col) 的选区 → 纯文本（行间用换行）。start/end 任意顺序。"""
    (r1, c1), (r2, c2) = sorted((tuple(start), tuple(end)))
    out = []
    for r in range(r1, min(r2, len(rows) - 1) + 1):
        text = plain(rows[r])
        lo = c1 if r == r1 else 0
        hi = c2 + 1 if r == r2 else len(text)
        out.append(_slice_by_columns(text, lo, hi))
    return "\n".join(out)


def _slice_by_columns(text, lo, hi):
    col, out = 0, []
    for ch in text:
        w = display_width(ch)
        if col >= hi:
            break
        if col >= lo:
            out.append(ch)
        col += w
    return "".join(out)


def row_cells(segments, cols):
    """一条视觉行 → 定长 cols 的格子列表（宽字符带续格）。结果可被多帧共享，调用方别原地改。"""
    b = _Buffer(1, cols)
    col = 0
    for text, style in segments:
        col = b.put(0, col, text, style)
    return b.cells[0]
