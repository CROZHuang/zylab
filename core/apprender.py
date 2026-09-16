"""AppRenderer：与 TerminalRenderer 公开接口相同的全屏渲染器（DESIGN-TUI-app §3 的适配器）。

REPL 与 42 个斜杠命令只认这 23 个成员；底下换成 screen.Buffer + views + 差分渲染。
输入框/菜单/决策模态/状态栏的排版直接复用 TerminalRenderer._build_frame（纯函数，给它一个
StringIO 做傀儡实例），transcript 是一个虚拟滚动的视图——实时与历史是同一个视图的不同滚动位置。

鼠标：应用模式默认抓（滚轮滚 transcript；拖选 = 应用内选区，松手即经 OSC 52 复制）；
Shift+拖拽仍是终端原生选择；`/mouse off` 可交还终端。粘贴永远走终端自己的快捷键。
"""
import base64
import collections
import io
import os
import signal
import sys
import threading
import time

import dataclasses
import re as _re

from . import screen, termcaps, views
from .tui import StreamingMarkdown, TerminalRenderer, display_width, user_line_band

_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*m")
PLACEHOLDER = "输入消息 · / 命令 · @ 文件 · ? 快捷键"

CSI = "\x1b["


class AppRenderer:
    SPINNER = "·✢✳✶✻✽✻✶✳✢"      # Claude Code 的脉冲，不用盲文转轮

    def __init__(self, stream=None, *, caps=None, mouse=True):
        self.stream = stream or sys.stdout
        self.caps = caps or termcaps.detect()
        self.owner_ident = threading.get_ident()
        self.styled = True
        self._helper = TerminalRenderer(stream=io.StringIO())     # 只借它的 _build_frame / _message_entries
        self._painter = screen.Renderer(truecolor=self.caps.truecolor, sync=self.caps.sync_output)
        self.transcript = views.Transcript()
        self._snapshot = None
        self._spinner = ""
        self._spinner_key = None
        self._scroll = 0                 # 视觉行，从底部数；0 = 跟随
        self._selection = None           # (anchor, end) 视觉坐标（相对当前窗口起点为绝对行号）
        self._flash = ("", 0.0)          # 状态栏一闪而过的提示
        self._mouse_pref = mouse
        self._mouse_enabled = False
        self._entered = False
        self._last_rows = []             # 上一帧 transcript 窗口的视觉行（供选区取文）
        self._last_window_start = 0
        self._painting = False
        self._resize_pending = False
        self._prev_winch = None
        self._frame_cache = (None, 0, None)   # (snapshot, width, (lines, crow, ccol, parsed))
        self._cells_cache = {}                # (id(视觉行), cols) → (视觉行, 格子列表)
        self._posted = collections.deque()    # 非主线程投来的文本（后台任务的 print），主线程排干
        self._posted_lock = threading.Lock()
        self.mouse_events = 0                 # 收到的鼠标事件数（/mouse 显示，排查"滚轮有没有送到"）

    # ---------- 生命周期 ----------
    def enter(self):
        if self._entered:
            return
        seq = ""
        if self.caps.alt_screen:
            seq += CSI + "?1049h" + CSI + "H" + CSI + "2J"
        if self.caps.kitty_keyboard:
            seq += CSI + ">1u"
        self._write(seq)
        if self._mouse_pref and self.caps.sgr_mouse:
            self.enable_mouse(force=True)
        self._entered = True
        self._painter.reset()
        # 窗口缩放即时重排：信号处理器在主线程的字节码之间运行，pump.get() 的等待会被打断后重试。
        if threading.get_ident() == threading.main_thread().ident:
            try:
                self._prev_winch = signal.signal(signal.SIGWINCH, self._on_winch)
            except (ValueError, OSError):
                self._prev_winch = None

    def exit(self, *, replay=True):
        if not self._entered:
            return
        if self._prev_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, self._prev_winch)
            except (ValueError, OSError):
                pass
            self._prev_winch = None
        self.disable_mouse()
        seq = ""
        if self.caps.kitty_keyboard:
            seq += CSI + "<u"
        seq += CSI + "0m" + CSI + "?25h"
        if self.caps.alt_screen:
            seq += CSI + "?1049l"
        self._write(seq)
        self._entered = False
        if replay and self.caps.alt_screen:
            text = self.transcript.plain_text().rstrip()
            if text:
                self._write(text.replace("\n", "\r\n") + "\r\n")
        self.flush()

    # ---------- 输出侧 ----------
    def post_output(self, text):
        """任何线程都可调：先排队，主线程下一次重绘/输出时按顺序进 transcript。"""
        with self._posted_lock:
            self._posted.append(str(text))

    def _take_posted(self):
        with self._posted_lock:
            if not self._posted:
                return []
            items = list(self._posted)
            self._posted.clear()
        return items

    def drain_posted(self):
        """主线程：把后台线程投来的文本进 transcript 并重绘。返回是否有东西。"""
        self._check_owner()
        items = self._take_posted()
        if not items:
            return False
        for text in items:
            self.transcript.append_text(text)
        self._repaint()
        return True

    def write_output(self, text, *, flush=True, source=None):
        self._check_owner()
        for posted in self._take_posted():
            self.transcript.append_text(posted)
        self.transcript.append_text(text)
        self._repaint()

    def finish_output_line(self):
        self._check_owner()
        self.transcript.finish_line()
        self._repaint()
        return True

    def commit_input(self, prompt, text, *, label=None):
        """用户消息：像 Claude Code 一样一行 `› 文本`（淡色），不画框。"""
        self._check_owner()
        mark = "›" if label is None else str(label)
        band, reset = user_line_band(True)
        cols, _ = self._size()
        self.transcript.finish_line()
        self.transcript.add_line([])
        lines = str(text).split("\n")
        for index, line in enumerate(lines):
            shown = f"{mark} {line}" if index == 0 else f"  {line}"
            pad = " " * max(0, cols - display_width(shown))
            self.transcript.add_line(views.parse_ansi(f"{band}{shown}{pad}{reset}"))
        self._repaint()

    def spinner(self, label, elapsed):
        self._check_owner()
        frame = self.SPINNER[int(elapsed / 0.25) % len(self.SPINNER)]
        key = (str(label), frame)
        if key == self._spinner_key:
            return
        self._spinner_key = key
        self._spinner = f"{frame} {label} · esc 中断"
        self._repaint()

    def clear_spinner(self):
        self._check_owner()
        if self._spinner:
            self._spinner = ""
            self._spinner_key = None
            self._repaint()

    def clear_input(self):
        self._check_owner()          # 输入框是帧的一部分，没有"清掉再画"的概念

    def render(self, snapshot):
        self._check_owner()
        self._snapshot = snapshot
        self._repaint()

    def flush(self):
        try:
            self.stream.flush()
        except (OSError, ValueError, AttributeError):
            pass

    def bell(self):
        try:
            if self.stream.isatty():
                self._write("\a")
                return True
        except (AttributeError, ValueError):
            pass
        return False

    def set_title(self, text):
        clean = "".join(ch for ch in str(text) if ch >= " " and ch != "\x7f")[:120]
        self._write(f"\x1b]0;{clean}\x07")

    # ---------- transcript / 历史 ----------
    def set_transcript(self, messages):
        """把规范消息重投影成 transcript 行（与 hydrate_transcript 同一种格式）。"""
        self._check_owner()
        self.transcript = views.Transcript()
        entries = []
        for index, message in enumerate(messages or ()):
            try:
                entries.extend(self._helper._message_entries(message, index))
            except Exception:                              # noqa: BLE001
                continue
        for entry in entries[-400:]:
            value = entry.text
            if entry.kind == "user":
                self.commit_input("› ", value[2:] if value.startswith("› ") else value)
            elif entry.kind == "assistant":
                body = value[2:] if value.startswith("⏺ ") else value
                md = StreamingMarkdown(styled=True, icon_hosted=True)
                self.transcript.append_text("\x1b[38;5;214m⏺ \x1b[0m" + md.feed(body) + md.finish() + "\n")
            else:
                self.transcript.append_text("\x1b[2;38;5;110m" + value + "\x1b[0m\n")
        self._scroll = 0
        self._repaint()

    @property
    def history_active(self):
        return self._scroll > 0

    def scroll_history(self, amount, snapshot=None):
        self._check_owner()
        cols, _ = self._size()
        height = self._transcript_height()
        maxoff = max(0, self.transcript.row_count(cols) - height)
        if amount == "page_up":
            self._scroll += max(1, height - 2)
        elif amount == "page_down":
            self._scroll -= max(1, height - 2)
        elif amount == "home":
            self._scroll = maxoff
        elif amount == "end":
            self._scroll = 0
        else:
            self._scroll += int(amount)
        self._scroll = max(0, min(self._scroll, maxoff))
        self._repaint()
        return self.history_active

    def close_history(self, snapshot=None):
        self._check_owner()
        self._scroll = 0
        self._selection = None
        self._repaint()

    def history_click(self, event, snapshot=None):
        return self.history_mouse(event, snapshot)

    def history_mouse(self, event, snapshot=None):
        """应用内拖选：按下定锚，拖动扩展，松手复制。坐标 = 终端行列（1 起）→ 视觉行绝对号。"""
        self._check_owner()
        self.mouse_events += 1
        kind = getattr(event, "kind", "")
        if kind in ("scroll_up", "scroll_down"):
            return self.scroll_history(3 if kind == "scroll_up" else -3, snapshot)
        if int(getattr(event, "y", 1)) - 1 >= len(self._last_rows):
            return False                          # 落在输入框/状态栏区域：不是 transcript 选区
        row = self._last_window_start + (int(getattr(event, "y", 1)) - 1)
        col = int(getattr(event, "x", 1)) - 1
        if kind == "press":
            self._selection = ((row, col), (row, col))
        elif kind == "motion" and self._selection:
            self._selection = (self._selection[0], (row, col))
        elif kind == "release" and self._selection:
            self._selection = (self._selection[0], (row, col))
            copied = self.copy_history_selection()
            if not copied:
                self._selection = None
        self._repaint()
        return True

    def copy_history_selection(self, snapshot=None):
        if not self._selection:
            return False
        rows = self._transcript_rows()
        text = views.selection_text(rows, *self._selection)
        if not text.strip():
            return False
        self._osc52(text)
        self._flash = (f"已复制 {len(text)} 字", time.monotonic() + 2.0)
        return True

    def composer_mouse(self, event, snapshot=None, *, cursor_screen_row=None):
        """底部（跟随模式）时编辑器把按下/拖动/松开当作输入框事件送来；落在 transcript 区域的
        一律按选区处理——否则"先拖拽再说"什么都不会发生。"""
        return self.history_mouse(event, snapshot)

    def clear_composer_selection(self, snapshot=None):
        return False

    def copy_composer_selection(self, snapshot=None):
        return False

    # ---------- 鼠标 ----------
    @property
    def mouse_enabled(self):
        return self._mouse_enabled

    def enable_mouse(self, env=None, *, force=False):
        if not self.caps.sgr_mouse or (not self._mouse_pref and not force):
            return False
        if not self._mouse_enabled:
            self._write(CSI + "?1000h" + CSI + "?1002h" + CSI + "?1006h")
            self._mouse_enabled = True
        return True

    def disable_mouse(self):
        if self._mouse_enabled:
            self._write(CSI + "?1006l" + CSI + "?1002l" + CSI + "?1000l")
            self._mouse_enabled = False

    def set_actionable_dock(self, active):
        return self._mouse_enabled

    def _on_winch(self, signum, frame):
        if self._painting:
            self._resize_pending = True
            return
        self._painter.reset()            # 尺寸变了，下一帧全画
        self._repaint()

    # ---------- 帧 ----------
    def _size(self):
        try:
            cols, rows = os.get_terminal_size(self.stream.fileno())
        except (OSError, ValueError, AttributeError):
            cols, rows = self.caps.cols, self.caps.rows
        return max(20, cols), max(5, rows)

    @staticmethod
    def _compact_status(snapshot):
        """状态栏对齐 Claude Code：左边是提示/模式，右边只留模型与上下文；chat id、memory、git、
        沙箱标记这些去 /doctor 看。忙碌时 activity 由 spinner 行承担，不重复。"""
        if snapshot is None or snapshot.mode != "line":
            return snapshot
        keep = []
        for part in str(snapshot.status or "").split(" · "):
            plain = _ANSI_RE.sub("", part).strip()
            if not plain:
                continue
            if ("@" in plain or plain.startswith("ctx ") or plain in ("plan", "auto", "仅聊天")
                    or plain.startswith(("next ", "workflow", "需要拍板", "gate recovery"))):
                keep.append(part)
        left = "" if snapshot.busy else "? 快捷键 · Shift+Tab 权限模式"
        return dataclasses.replace(snapshot, activity=left, status=" · ".join(keep))

    def _frame_lines(self, width):
        snap, w, cached = self._frame_cache
        if cached is not None and snap is self._snapshot and w == width:
            return cached
        try:
            lines, crow, ccol = self._helper._build_frame(self._snapshot, width)
        except Exception:                                  # noqa: BLE001
            lines, crow, ccol = [""], 0, 0
        parsed = [views.parse_ansi(line) for line in lines]
        cached = (lines, crow, ccol, parsed)
        self._frame_cache = (self._snapshot, width, cached)
        return cached

    def _transcript_height(self):
        cols, rows = self._size()
        lines = self._frame_lines(cols - 1)[0]
        return max(1, rows - len(lines) - (1 if self._spinner else 0))

    def _transcript_rows(self):
        cols, _ = self._size()
        return self.transcript.visual_rows(cols)

    def _repaint(self):
        if self._painting:
            return
        self._painting = True
        try:
            self._repaint_once()
        finally:
            self._painting = False
        if self._resize_pending:
            self._resize_pending = False
            self._painter.reset()
            self._repaint()

    def _repaint_once(self):
        cols, rows = self._size()
        buf = screen.Buffer(rows, cols)
        frame_lines, crow, ccol, frame_segs = self._frame_lines(cols - 1)
        spinner_rows = 1 if self._spinner else 0
        height = max(1, rows - len(frame_lines) - spinner_rows)
        window, self._scroll, self._last_window_start = self.transcript.window(cols, height, self._scroll)
        self._last_rows = window
        sel = self._selection
        cache = self._cells_cache
        if len(cache) > 8000:
            cache.clear()
        for i, segs in enumerate(window):
            key = (id(segs), cols)
            hit = cache.get(key)
            if hit is None or hit[0] is not segs:
                hit = (segs, views.row_cells(segs, cols))
                cache[key] = hit
            buf.cells[i] = list(hit[1])
            if sel:
                self._paint_selection(buf, i, self._last_window_start + i, cols)
        if self._scroll:
            tag = f" ↑ {self._scroll} 行 · End 回到底部 "
            buf.put(0, max(0, cols - display_width(tag) - 1), tag, screen.Style(reverse=True))
        elif self.caps.sgr_mouse and not self._mouse_enabled and self._entered:
            tag = " 鼠标已交还终端 · /mouse on 收回 "
            buf.put(0, max(0, cols - display_width(tag) - 1), tag, screen.Style(dim=True))
        y = height
        if self._spinner:
            buf.put(y, 0, self._spinner, screen.Style(fg=214)); y += 1
        for j, segs in enumerate(frame_segs):
            col = 0
            for text, style in segs:
                col = buf.put(y + j, col, text, style)
        flash, until = self._flash
        if flash and time.monotonic() < until:
            buf.put(rows - 1, max(0, cols - display_width(flash) - 1), flash, screen.Style(reverse=True))
        cursor = None
        if self._snapshot is not None and getattr(self._snapshot, "active", True) and not self._scroll:
            cursor = (y + crow, ccol)
        self._write(self._painter.render(buf, cursor=cursor))
        self.flush()

    def _paint_selection(self, buf, screen_row, abs_row, cols):
        (r1, c1), (r2, c2) = sorted(self._selection)
        if not (r1 <= abs_row <= r2):
            return
        lo = c1 if abs_row == r1 else 0
        hi = c2 if abs_row == r2 else cols - 1
        for c in range(max(0, lo), min(cols - 1, hi) + 1):
            cell = buf.get(screen_row, c)
            if not cell.continuation:
                buf.cells[screen_row][c] = screen.Cell(cell.ch, cell.style.merge(reverse=True), wide=cell.wide)

    # ---------- 杂 ----------
    def _osc52(self, text):
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self._write(f"\x1b]52;c;{payload}\x07")

    def _write(self, s):
        if s:
            try:
                self.stream.write(s)
            except (OSError, ValueError):
                pass

    def _check_owner(self):
        if threading.get_ident() != self.owner_ident:
            raise RuntimeError("AppRenderer 只能在创建它的线程（主线程）上调用")
