"""away recap 的判定与控制器 —— 照搬 Claude Code awaySummary 的行为契约。

依据是 CC 2.1.272 的明文实现：失焦后计时、重新聚焦即取消并中止在途生成、
一串「不打扰」的闸、前 3 次附关闭提示、结果截到 400 字符。
"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import away_recap as recap  # noqa: E402


def users(*texts):
    return [{"role": "user", "content": text} for text in texts]


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeTimer:
    def __init__(self, wait, fn):
        self.wait, self.fn = wait, fn
        self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class Harness:
    """把控制器接到假时钟、假定时器和可调的会话状态上。"""

    def __init__(self, **kwargs):
        self.clock = FakeClock()
        self.timers = []
        self.delivered = []
        self.calls = 0
        self.result = {"kind": "ok", "text": "在修 X，下一步跑测试。"}
        self.on_generate = None
        self.state = {"loading": False, "draft": False, "background": 0,
                      "messages": users("a", "b", "c"), "last_recap_users": None}
        self.ctl = recap.AwayRecapController(
            generate=self._generate, state=lambda: dict(self.state),
            deliver=lambda text, raw: self.delivered.append((text, raw)),
            clock=self.clock, timer=self._timer, hint="（可关闭）", **kwargs)

    def _timer(self, wait, fn):
        timer = FakeTimer(wait, fn)
        self.timers.append(timer)
        return timer

    def _generate(self, cancel):
        self.calls += 1
        if self.on_generate:
            self.on_generate(cancel)
        return dict(self.result)

    def live_timer(self):
        live = [t for t in self.timers if t.started and not t.cancelled]
        return live[-1] if live else None

    def fire(self):
        timer = self.live_timer()
        assert timer is not None, "没有待触发的定时器"
        timer.fn()


class EligibilityTests(unittest.TestCase):
    def test_needs_three_real_questions(self):
        self.assertFalse(recap.eligible(users("a", "b")))
        self.assertTrue(recap.eligible(users("a", "b", "c")))

    def test_runtime_notices_and_other_roles_do_not_count(self):
        messages = users("a", "b", "[已中断] 运行时通知", "   ")
        messages.append({"role": "assistant", "content": "答"})
        messages.append({"role": "tool", "content": "结果"})
        self.assertEqual(recap.real_user_messages(messages), 2)

    def test_needs_two_new_questions_since_the_last_recap(self):
        """防止反复总结同一段对话。"""
        messages = users("a", "b", "c", "d")
        self.assertFalse(recap.eligible(messages, last_recap_users=3))
        self.assertTrue(recap.eligible(messages, last_recap_users=2))


    def test_a_rewound_history_does_not_wait_on_a_count_from_the_future(self):
        """rewind 把历史截短后，上一条 recap 记下的提问数比现在还大——那个计数已经
        没有意义，不能让人再等好几轮才有下一条。"""
        self.assertTrue(recap.eligible(users("a", "b", "c"), last_recap_users=9))


class CleanTests(unittest.TestCase):
    def test_thinking_is_never_part_of_the_recap(self):
        text, capped = recap.clean("<think>先想想\n很多行</think>在修 X。\n下一步跑测试。")
        self.assertEqual(text, "在修 X。 下一步跑测试。")
        self.assertFalse(capped)

    def test_dangling_think_block_is_dropped(self):
        """思考吃光额度时 <think> 没闭合：那不是 recap，是残段。"""
        self.assertEqual(recap.clean("<think>还没想完就被截断")[0], "")

    def test_capped_at_400_chars(self):
        text, capped = recap.clean("字" * 900)
        self.assertTrue(capped)
        self.assertEqual(len(text), recap.CHAR_CAP)
        self.assertTrue(text.endswith("…"))


class TriggerTests(unittest.TestCase):
    def test_without_focus_events_it_never_fires_on_its_own(self):
        """终端不支持 1004 → 从没见过失焦 → 永不自动触发（与 CC 一致）。"""
        h = Harness()
        h.ctl.turn_ended()
        h.fire()
        self.assertEqual(h.calls, 0)

    def test_blur_right_after_a_turn_waits_out_the_delay(self):
        h = Harness(delay=300)
        h.ctl.turn_ended()
        h.clock.now += 10
        h.ctl.focus_changed(False)
        self.assertAlmostEqual(h.live_timer().wait, 290)
        h.fire()
        self.assertEqual(h.calls, 1)
        self.assertEqual(h.delivered, [("在修 X，下一步跑测试。（可关闭）", "在修 X，下一步跑测试。")])

    def test_blur_long_after_a_turn_only_waits_out_the_debounce(self):
        """切一下窗口马上回来不算离开：2 秒防抖。"""
        h = Harness(delay=300)
        h.ctl.turn_ended()
        h.clock.now += 3600
        h.ctl.focus_changed(False)
        self.assertAlmostEqual(h.live_timer().wait, recap.BLUR_DEBOUNCE_SECONDS)

    def test_refocus_cancels_the_pending_timer(self):
        h = Harness()
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        timer = h.live_timer()
        h.ctl.focus_changed(True)
        self.assertTrue(timer.cancelled)
        self.assertIsNone(h.live_timer())

    def test_refocus_during_generation_discards_the_result(self):
        """人回来了就别让 recap 事后冒出来。"""
        h = Harness()
        h.on_generate = lambda cancel: h.ctl.focus_changed(True)
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        h.fire()
        self.assertEqual(h.calls, 1)
        self.assertEqual(h.delivered, [])
        self.assertEqual(h.ctl.last_skip, "aborted")

    def test_a_new_turn_cancels_timer_and_inflight_generation(self):
        h = Harness()
        seen = []
        h.on_generate = lambda cancel: (h.ctl.turn_started(), seen.append(cancel.is_set()))
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        h.fire()
        self.assertEqual(seen, [True])
        self.assertEqual(h.delivered, [])

    def test_delay_has_a_floor(self):
        h = Harness(delay=1)
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        self.assertAlmostEqual(h.live_timer().wait, recap.MIN_DELAY_SECONDS)


class DoNotDisturbTests(unittest.TestCase):
    def _away(self, **state):
        h = Harness()
        h.state.update(state)
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        h.fire()
        return h

    def test_draft_input_present(self):
        h = self._away(draft=True)
        self.assertEqual((h.calls, h.ctl.last_skip), (0, "draft input present"))

    def test_background_work_pending(self):
        h = self._away(background=2)
        self.assertEqual((h.calls, h.ctl.last_skip), (0, "background work pending"))

    def test_not_enough_new_conversation(self):
        h = self._away(last_recap_users=3)
        self.assertEqual((h.calls, h.ctl.last_skip), (0, "not enough new conversation"))

    def test_result_is_dropped_when_a_turn_started_meanwhile(self):
        h = Harness()
        h.on_generate = lambda cancel: h.state.update(loading=True)
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        h.fire()
        self.assertEqual(h.delivered, [])
        self.assertEqual(h.ctl.last_skip, "dropped: new turn already running")

    def test_gives_up_after_three_failures_until_the_next_turn(self):
        h = Harness()
        h.result = {"kind": "failed", "text": ""}
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        for _ in range(5):
            h.ctl.maybe_generate()
        self.assertEqual(h.calls, recap.MAX_FAILURES_PER_TURN)
        self.assertEqual(h.ctl.last_skip, "failing repeatedly this turn")
        h.result = {"kind": "ok", "text": "好了。"}
        h.ctl.turn_ended()                         # 新一轮复位失败计数
        h.ctl.maybe_generate()
        self.assertEqual(len(h.delivered), 1)

    def test_a_crashing_generator_counts_as_a_failure_not_a_crash(self):
        h = Harness()

        def boom(cancel):
            raise RuntimeError("gateway down")
        h.on_generate = boom
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        h.fire()
        self.assertEqual(h.delivered, [])
        self.assertTrue(h.ctl.last_skip.startswith("generate failed"))

    def test_disable_hint_only_on_the_first_few_recaps(self):
        h = Harness()
        h.ctl.turn_ended()
        for _ in range(recap.HINT_FIRST_N + 2):
            h.ctl.maybe_generate()
        hinted = [text.endswith("（可关闭）") for text, _ in h.delivered]
        self.assertEqual(hinted, [True] * recap.HINT_FIRST_N + [False, False])

    def test_disabled_controller_does_nothing(self):
        h = Harness(enabled=False)
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        self.assertIsNone(h.live_timer())
        h.ctl.maybe_generate()
        self.assertEqual(h.calls, 0)


class ResumeRequestTests(unittest.TestCase):
    def test_request_does_not_need_a_completed_turn_in_this_process(self):
        """刚 resume 的会话在本进程里还没跑过一轮，但它有的是可 recap 的历史。"""
        h = Harness()
        self.assertIsNone(h.ctl.maybe_generate())
        self.assertEqual(h.ctl.last_skip, "no completed turn")
        h.ctl.request().join(timeout=5)
        self.assertEqual(h.calls, 1)
        self.assertEqual(len(h.delivered), 1)

    def test_request_still_respects_do_not_disturb(self):
        h = Harness()
        h.state["draft"] = True
        h.ctl.request().join(timeout=5)
        self.assertEqual((h.calls, h.ctl.last_skip), (0, "draft input present"))

    def test_request_is_a_no_op_when_disabled(self):
        h = Harness(enabled=False)
        self.assertIsNone(h.ctl.request())
        self.assertEqual(h.calls, 0)


class CancelHandleTests(unittest.TestCase):
    def test_abort_goes_through_the_injected_handle(self):
        """真实会话注入 client.CancellationHandle：它的 set() 会 shutdown socket，
        才叫得醒阻塞在「等首字节」上的读线程；光置位一个 Event 不行。"""
        made = []

        class Handle:
            def __init__(self):
                self.fired = False
                made.append(self)

            def set(self):
                self.fired = True

            def is_set(self):
                return self.fired

        h = Harness(cancel_factory=Handle)
        h.on_generate = lambda cancel: h.ctl.focus_changed(True)
        h.ctl.turn_ended()
        h.ctl.focus_changed(False)
        h.fire()
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].fired)
        self.assertEqual(h.delivered, [])


class ThreadingSmokeTest(unittest.TestCase):
    def test_real_timer_fires_in_the_background(self):
        done = threading.Event()
        ctl = recap.AwayRecapController(
            generate=lambda cancel: {"kind": "ok", "text": "好。"},
            state=lambda: {"messages": users("a", "b", "c")},
            deliver=lambda text, raw: done.set(), delay=0)
        ctl._delay = 0.0                           # 绕过 30s 下限，只测真实定时器
        ctl.turn_ended()
        ctl.focus_changed(False)
        ctl._first_blur -= recap.BLUR_DEBOUNCE_SECONDS
        ctl._arm()
        self.assertTrue(done.wait(3), "真实 threading.Timer 没有触发生成")
        ctl.dispose()


if __name__ == "__main__":
    unittest.main()
