#!/usr/bin/env python3
"""终端能力探测（只读，不碰 zylab 状态）：给"自建全屏 TUI"路线画边界用。

在真实终端里跑：  python3 scripts/term_probe.py          （交互探测约 40 秒）
                  python3 scripts/term_probe.py --auto   （跳过需要你动手的段落）
把最后的「摘要」整段贴回来即可。所有查询都带超时，退出时恢复终端模式；Ctrl+C 也会恢复。

探什么、为什么：
- DA1/DA2/XTVERSION       终端是谁（Cursor 的集成终端是 xterm.js，报告的版本决定后面能指望什么）
- DECRQM 2026/2004/1006/1049/1004/1007   同步输出 / 括号粘贴 / SGR 鼠标 / 备用屏 / 焦点事件 / 备用屏滚轮
- kitty 键盘协议 `CSI ? u`  能否消除 Esc 歧义、拿到 Shift+Enter
- CPR `CSI 6 n`            终端会不会回答查询；顺便量 CJK / emoji / 希腊字母的实际占位宽度
- OSC 52 写 + 括号粘贴回读  复制这条唯一的通道在 Cursor 里通不通（pod 上没有显示服务器，xclip 不存在）
- 修饰键 Enter 原始字节     Shift/Ctrl/Alt+Enter 到底发什么
- 备用屏里的滚轮            全屏应用不抓鼠标时，滚轮会不会被翻译成方向键（DECSET 1007）
"""
import argparse
import base64
import os
import select
import sys
import termios
import time
import tty

ESC = "\x1b"
LOG = []


def log(line=""):
    LOG.append(line)
    sys.stdout.write(line + "\r\n")
    sys.stdout.flush()


class Term:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        self.enabled = []

    def __enter__(self):
        tty.setcbreak(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        return self

    def __exit__(self, *exc):
        for seq in reversed(self.enabled):
            self.write(seq)
        termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        return False

    def write(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def enable(self, on, off):
        self.write(on)
        self.enabled.append(off)

    def disable(self, off):
        if off in self.enabled:
            self.enabled.remove(off)
            self.write(off)

    def read(self, timeout=0.6, until=None, max_bytes=4096):
        """读到 until 出现或超时为止；没有 until 就收 timeout 内的全部字节。"""
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                break
            chunk = os.read(self.fd, 1024)
            if not chunk:
                break
            buf += chunk
            if until is not None and until in buf:
                break
            if len(buf) >= max_bytes:
                break
        return buf

    def query(self, seq, until, timeout=0.6):
        self.read(0.05)                      # 先清掉残留
        self.write(seq)
        return self.read(timeout, until=until)


def show(b):
    return repr(b.decode("utf-8", "replace")).strip("'").replace("\\x1b", "ESC")


def decrqm(term, mode):
    reply = term.query(f"{ESC}[?{mode}$p", until=b"$y")
    text = reply.decode("latin-1")
    if "$y" not in text:
        return "无应答（DECRQM 不支持）", None
    try:
        value = int(text.split(";")[1].split("$")[0])
    except (IndexError, ValueError):
        return f"应答无法解析 {show(reply)}", None
    names = {0: "不认识此模式", 1: "支持（当前开）", 2: "支持（当前关）", 3: "永久开", 4: "永久关（不支持）"}
    return names.get(value, f"值 {value}"), value


def cpr(term):
    reply = term.query(f"{ESC}[6n", until=b"R")
    text = reply.decode("latin-1")
    if not text.endswith("R") or "[" not in text:
        return None
    try:
        row, col = text[text.rindex("[") + 1:-1].split(";")
        return int(row), int(col)
    except ValueError:
        return None


def width_of(term, sample):
    """把样本打在行首，问光标列，得到终端实际给它的列宽。"""
    term.write("\r" + f"{ESC}[K" + sample)
    pos = cpr(term)
    term.write("\r" + f"{ESC}[K")
    return None if pos is None else pos[1] - 1


KEY_LABELS = {
    b"\r": "普通 Enter", b"\n": "Ctrl+J/换行", b"\x1b": "裸 ESC", b"\x1b\r": "Alt+Enter（旧编码）",
    b"\x1b[27;2;13~": "Shift+Enter（modifyOtherKeys）", b"\x1b[27;5;13~": "Ctrl+Enter（modifyOtherKeys）",
    b"\x1b[13;2u": "Shift+Enter（kitty）", b"\x1b[13;5u": "Ctrl+Enter（kitty）", b"\x1b[13;3u": "Alt+Enter（kitty）",
    b"\x1b[27u": "Esc（kitty，无歧义）", b"\x1b[13u": "Enter（kitty）",
}


def label_key(chunk):
    return KEY_LABELS.get(chunk, "未知序列")


def interactive(term, title, max_wait, on, off, decoder, *, expect_events=None, settle=0.8):
    """expect_events 给定：等满这么多个事件（每个当场翻译）或 max_wait 秒；
    否则：等到第一个事件为止（最长 max_wait 秒），收到后再多收 settle 秒。"""
    log(f"── {title}（最长等 {max_wait} 秒，现在动手）")
    if on:
        term.enable(on, off)
    deadline = time.monotonic() + max_wait
    seen = []
    while time.monotonic() < deadline:
        chunk = term.read(min(0.5, max(0.05, deadline - time.monotonic())))
        if chunk:
            seen.append(chunk)
            log(f"   收到 {show(chunk)}" + (f"  = {label_key(chunk)}" if expect_events else ""))
            if expect_events is None:
                deadline = min(deadline, time.monotonic() + settle)
            elif len(seen) >= expect_events:
                break
        if len(seen) >= 12:
            break
    if off:
        term.disable(off)
    log("   " + decoder(b"".join(seen)))
    return b"".join(seen)


def main():
    ap = argparse.ArgumentParser(description="终端能力探测（只读）")
    ap.add_argument("--auto", action="store_true", help="跳过需要你动手的段落")
    ap.add_argument("--only", default="", metavar="段落",
                    help="只跑这些交互段（逗号分隔）：keys,kitty,mouse,wheel,paste；默认全部")
    a = ap.parse_args()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("需要在真实终端里运行（stdin/stdout 不是 TTY）。", file=sys.stderr)
        return 2
    summary = {}
    env_keys = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LANG", "LC_ALL",
                "VSCODE_PID", "VSCODE_INJECTION", "CURSOR_TRACE_ID", "TMUX", "STY", "SSH_TTY", "NO_COLOR")
    with Term() as term:
        log("=== zylab 终端能力探测 ===")
        for k in env_keys:
            v = os.environ.get(k)
            if v:
                log(f"  {k}={v[:60]}")
        cols, rows = os.get_terminal_size()
        log(f"  尺寸 {cols}x{rows}")
        summary["env"] = {k: os.environ.get(k, "") for k in ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")}

        log("── 身份")
        da1 = term.query(f"{ESC}[c", until=b"c")
        da2 = term.query(f"{ESC}[>c", until=b"c")
        xtv = term.query(f"{ESC}[>0q", until=b"\\")
        log(f"  DA1: {show(da1) or '无应答'}")
        log(f"  DA2: {show(da2) or '无应答'}")
        log(f"  XTVERSION: {show(xtv) or '无应答'}")
        summary["DA1"], summary["DA2"], summary["XTVERSION"] = show(da1), show(da2), show(xtv)

        log("── 模式（DECRQM）")
        for mode, name in ((2026, "同步输出 2026"), (2004, "括号粘贴 2004"), (1006, "SGR 鼠标 1006"),
                           (1049, "备用屏 1049"), (1004, "焦点事件 1004"), (1007, "备用屏滚轮→方向键 1007"),
                           (2031, "配色通知 2031")):
            text, value = decrqm(term, mode)
            log(f"  {name}: {text}")
            summary[f"mode{mode}"] = value

        log("── 键盘协议")
        kitty = term.query(f"{ESC}[?u", until=b"u")
        log(f"  kitty `CSI ? u`: {show(kitty) or '无应答（不支持）'}")
        summary["kitty"] = show(kitty)

        log("── 查询与宽度")
        pos = cpr(term)
        log(f"  CPR 光标位置报告: {'可用 ' + str(pos) if pos else '无应答'}")
        summary["cpr"] = bool(pos)
        if pos:
            widths = {}
            for label, sample in (("中", "中"), ("😀", "😀"), ("α", "α"), ("→", "→"), ("│", "│"), ("⏺", "⏺"), ("✓", "✓")):
                w = width_of(term, sample)
                widths[label] = w
            log("  实际列宽: " + "  ".join(f"{k}={v}" for k, v in widths.items()))
            summary["widths"] = widths

        log("── 颜色与链接（肉眼看）")
        term.write("  真彩: ")
        for i in range(0, 256, 16):
            term.write(f"{ESC}[48;2;{i};{255 - i};{(i * 3) % 256}m ")
        term.write(f"{ESC}[0m   256 色: ")
        for i in (196, 202, 208, 214, 220, 226, 46, 51, 21, 93):
            term.write(f"{ESC}[48;5;{i}m ")
        term.write(f"{ESC}[0m\r\n")
        term.write(f"  OSC 8 链接: {ESC}]8;;https://example.com{ESC}\\这里应是可点击的 example.com{ESC}]8;;{ESC}\\\r\n")
        log("  （真彩条应该是平滑渐变，不是几段色块；链接应可悬停/点击）")

        marker = "ZYLAB-OSC52-OK-" + time.strftime("%H%M%S")
        term.write(f"{ESC}]52;c;{base64.b64encode(marker.encode()).decode()}\x07")
        reply = term.query(f"{ESC}]52;c;?\x07", until=b"\x07", timeout=0.8)
        readback = ""
        if b"]52;" in reply:
            try:
                readback = base64.b64decode(reply.split(b";")[-1].rstrip(b"\x07\x1b\\")).decode()
            except Exception:                              # noqa: BLE001
                readback = "(无法解析)"
        log("── 剪贴板 OSC 52")
        log(f"  已写入标记 {marker}；回读: {readback or '无应答（多数终端禁止读，正常）'}")
        summary["osc52_read"] = readback == marker

        bg = term.query(f"{ESC}]11;?\x07", until=b"\x07", timeout=0.8)
        bg_text = show(bg)
        log(f"── 背景色 OSC 11: {bg_text or '无应答'}")
        summary["osc11_bg"] = bg_text

        only = {x.strip() for x in a.only.split(",") if x.strip()}
        want = lambda name: not a.auto and (not only or name in only)   # noqa: E731
        if want("keys"):
            keys = interactive(term, "按键：请依次按 Shift+Enter、Ctrl+Enter、Alt+Enter、Esc（每个一次，共 4 个）", 25,
                               None, None,
                               lambda b: "解读：" + ("有 CSI 27;2;13~ 或 CSI 13;2u → Shift+Enter 可区分" if (b"27;2;13" in b or b"13;2u" in b)
                                                  else "Shift+Enter 与普通回车不可区分（只有 \\r）" if b"\r" in b else "没收到回车"),
                               expect_events=4)
            summary["shift_enter_distinct"] = (b"27;2;13" in keys or b"13;2u" in keys)
        if want("kitty"):
            # 开启 kitty 键盘协议（flags=1：消除转义歧义）再按一次；结束时 pop
            kk = interactive(term, "kitty 协议已开启：请依次按 Shift+Enter、Esc、Ctrl+Enter、Alt+Enter（共 4 个）", 25,
                             f"{ESC}[>1u", f"{ESC}[<u",
                             lambda b: "解读：" + ("Shift+Enter → CSI 13;2u，可区分" if b"13;2u" in b else "Shift+Enter 仍不可区分")
                             + ("；Esc → CSI 27u，无歧义" if b"27u" in b else "；Esc 仍是裸 ESC"),
                             expect_events=4)
            summary["kitty_shift_enter"] = b"13;2u" in kk
            summary["kitty_esc_unambiguous"] = b"27u" in kk
        if want("mouse"):
            mouse = interactive(term, "鼠标：请在这个窗口里点一下、再滚一下滚轮", 20,
                                f"{ESC}[?1000h{ESC}[?1006h", f"{ESC}[?1006l{ESC}[?1000l",
                                lambda b: "解读：" + ("收到 SGR 鼠标事件（CSI < …M/m）" if b"[<" in b else "没有 SGR 鼠标事件"))
            summary["sgr_mouse"] = b"[<" in mouse
        if want("wheel"):
            term.enable(f"{ESC}[?1049h", f"{ESC}[?1049l")
            term.write(f"{ESC}[H{ESC}[2J")
            wheel = interactive(term, "备用屏：现在在这个空屏里滚一下滚轮（不抓鼠标）", 20, None, None,
                                lambda b: "解读：" + ("滚轮被翻译成方向键（可用于全屏滚动）" if (b"[A" in b or b"[B" in b)
                                                   else "备用屏里滚轮什么都不发（全屏应用必须自己抓鼠标才能滚）"))
            term.disable(f"{ESC}[?1049l")
            summary["altscreen_wheel_arrows"] = (b"[A" in wheel or b"[B" in wheel)
        if want("paste"):
            paste = interactive(term, "粘贴：Cursor/VS Code 终端里用 Ctrl+Shift+V（macOS 用 Cmd+V）粘贴，剪贴板里应是刚写入的标记", 25,
                                f"{ESC}[?2004h", f"{ESC}[?2004l",
                                lambda b: "解读：" + (("括号粘贴可用；" + ("贴出的正是标记 → OSC 52 写剪贴板可用" if marker.encode() in b else "贴出内容不是标记 → OSC 52 写入未生效"))
                                                   if b"[200~" in b else ("没有括号粘贴序列" + ("，但收到了标记（粘贴不带括号）" if marker.encode() in b else ""))))
            summary["bracketed_paste"] = b"[200~" in paste
            summary["osc52_write"] = marker.encode() in paste

    log("")
    log("=== 摘要（把这一段贴回来）===")
    for k, v in summary.items():
        log(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断，终端模式已恢复。")
        raise SystemExit(130)
