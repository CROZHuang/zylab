"""终端焦点上报（DEC 1004）—— away recap 靠它知道「人离开了」。

三件事必须同时成立，否则要么功能永不触发，要么把用户的终端弄脏：
  1. `CSI I` / `CSI O` 被解码成焦点事件，且不和 SS3 的 `ESC O x`（方向键）混淆；
  2. 焦点变化走订阅回调、**不进事件队列**——选择器/提示框那些消费循环会把不认识的
     事件延后重放，焦点信号一旦被延后就没有意义，更不能被当成输入；
  3. 1004 跟着 InputPump 的生命周期开关：退出后 shell 里切窗口不能冒出 `^[[I`。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tui                                  # noqa: E402
from tests.pty_harness import PTYSend, run_pty_child  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def feed(payload, *, reads=1, timeout=0.2):
    read_fd, write_fd = os.pipe()
    out = []
    try:
        os.write(write_fd, payload)
        with os.fdopen(read_fd, "rb", buffering=0) as stream:
            read_fd = None
            for _ in range(reads):
                out.append(tui.read_key(timeout=timeout, stream=stream))
    finally:
        os.close(write_fd)
        if read_fd is not None:
            os.close(read_fd)
    return out


class DecodingTests(unittest.TestCase):
    def test_csi_i_and_csi_o_are_focus_events(self):
        self.assertEqual(feed(b"\x1b[I\x1b[O", reads=2), ["focus-in", "focus-out"])

    def test_ss3_arrow_keys_are_not_mistaken_for_focus_out(self):
        """`ESC O A` 是应用光标模式下的 ↑；前缀是 SS3 不是 CSI。"""
        self.assertEqual(feed(b"\x1bOA\x1bOB", reads=2), ["up", "down"])

    def test_focus_between_keystrokes_does_not_eat_them(self):
        self.assertEqual(feed(b"a\x1b[Ob", reads=3), ["a", "focus-out", "b"])


class SubscriberTests(unittest.TestCase):
    def test_a_raising_subscriber_cannot_kill_the_input_thread(self):
        pump = tui.InputPump(on_focus=lambda focused: 1 / 0)
        pump._notify_focus(True)                      # 不抛即通过

    def test_no_subscriber_is_fine(self):
        tui.InputPump()._notify_focus(False)


class PTYTests(unittest.TestCase):
    def test_focus_goes_to_the_subscriber_and_never_into_the_event_queue(self):
        output, result = run_pty_child(
            r"""
import json
import time
from core import tui

seen, kinds, texts = [], [], []
with tui.InputPump(on_focus=seen.append) as pump:
    print("PUMP_READY", flush=True)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and "submit" not in kinds:
        event = pump.get(0.1)
        if event is None:
            continue
        kinds.append(event.kind)
        if event.kind == "submit":
            texts.append(event.text)
print("RESULT:" + json.dumps({"seen": seen, "kinds": sorted(set(kinds)), "texts": texts}))
""",
            [PTYSend(payload=b"\x1b[Oh\x1b[Ii\r", after="PUMP_READY")],
            cwd=ROOT, timeout=8.0)
        self.assertEqual(result["seen"], [False, True])
        self.assertEqual(result["texts"], ["hi"])     # 夹在中间的按键一个没丢
        self.assertNotIn("focus", result["kinds"])
        # 进出各一次，且关在开之后：退出后终端不再上报焦点
        self.assertEqual(output.count(tui.FOCUS_ON), 1)
        self.assertEqual(output.count(tui.FOCUS_OFF), 1)
        self.assertLess(output.index(tui.FOCUS_ON), output.index(tui.FOCUS_OFF))


if __name__ == "__main__":
    unittest.main()
