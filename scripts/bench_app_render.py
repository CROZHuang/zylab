#!/usr/bin/env python3
"""全屏渲染每帧开销基线：254×38、2000 行 transcript、流式追加 300 个片段。
数字进 DEVLOG；改 screen/views/apprender 后重跑，别凭感觉说"不卡"。"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import apprender, termcaps, tui  # noqa: E402


class _Stream(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        raise OSError("no fd")


def main():
    cols, rows = 254, 38
    out = _Stream()
    r = apprender.AppRenderer(out, caps=termcaps.TermCaps(tty=True, alt_screen=False, sync_output=True,
                                                          truecolor=True, cols=cols, rows=rows))
    snap = tui.InputSnapshot(mode="line", prompt="› ", text="draft", cursor=5, active=True,
                             status="chat x · m@g · ctx 10K/900K · git main", activity="空闲")
    r.render(snap)
    t0 = time.perf_counter()
    for i in range(2000):
        r.write_output(f"\x1b[38;5;214m⏺\x1b[0m line {i} 中文内容 " + "x" * (i % 120) + "\n")
    fill = time.perf_counter() - t0
    t0 = time.perf_counter()
    for i in range(300):
        r.write_output("token " if i % 9 else "token\n")
    stream = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(50):
        r.scroll_history("page_up")
    for _ in range(50):
        r.scroll_history("page_down")
    scroll = time.perf_counter() - t0
    bytes_out = len(out.getvalue())
    print(f"尺寸 {cols}x{rows}")
    print(f"灌入 2000 行：{fill*1000:.0f} ms（{fill/2000*1000:.2f} ms/帧）")
    print(f"流式 300 片段：{stream*1000:.0f} ms（{stream/300*1000:.2f} ms/帧）")
    print(f"翻页 100 次：{scroll*1000:.0f} ms（{scroll/100*1000:.2f} ms/帧）")
    print(f"总输出 {bytes_out/1024:.0f} KB")


if __name__ == "__main__":
    main()
