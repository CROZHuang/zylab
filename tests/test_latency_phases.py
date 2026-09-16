"""D2（SPEC-CC-parity）：首字之前的等待要被解释 —— 阶段标签与 poll 事件的契约。"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zylab
from core import client


class PhaseLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(zylab._phase_activity(None), "思考中")
        self.assertEqual(zylab._phase_activity("queued"), "排队中")
        self.assertEqual(zylab._phase_activity("started"), "连接中")
        self.assertEqual(zylab._phase_activity("headers"), "等待首字")
        self.assertEqual(zylab._phase_activity("compaction"), "压缩上下文中")

    def test_slow_hint_names_the_stuck_segment(self):
        slow = zylab.SLOW_FIRST_TOKEN_SECONDS + 1
        self.assertIn("网关尚未应答", zylab._phase_activity("started", slow))
        self.assertIn("模型尚未开口", zylab._phase_activity("headers", slow))
        self.assertNotIn("慢", zylab._phase_activity("headers", 1.0))
        self.assertEqual(zylab._phase_activity("headers", "garbage"), "等待首字")

    def test_unresponsive_model_gets_a_way_out_not_just_a_spinner(self):
        stuck = zylab.UNRESPONSIVE_SECONDS + 1
        for phase in ("started", "headers"):
            label = zylab._phase_activity(phase, stuck)
            self.assertIn("可能无响应", label)
            self.assertIn("/model", label, "出路要写在状态栏里：换模型是用户的决定")
        self.assertNotIn("可能无响应", zylab._phase_activity("first_delta", stuck),
                         "已经开口就不是无响应")
        self.assertNotIn("可能无响应", zylab._phase_activity(
            "headers", zylab.SLOW_FIRST_TOKEN_SECONDS + 1))


class LivePhasesTests(unittest.TestCase):
    def test_snapshot_reports_latest_phase_and_age(self):
        lp = client.LivePhases()
        self.assertEqual(lp.snapshot(), (None, 0.0))
        lp.mark("headers", 1)
        phase, age = lp.snapshot()
        self.assertEqual(phase, "headers")
        self.assertGreaterEqual(age, 0.0)

    def test_chain_feeds_both_sinks(self):
        a, b = client.LivePhases(), client.LivePhases()
        client._PhaseChain(a, None, b).mark("started")
        self.assertEqual(a.snapshot()[0], "started")
        self.assertEqual(b.snapshot()[0], "started")


class BackgroundPollCarriesPhase(unittest.TestCase):
    def test_idle_polls_report_the_phase_marked_by_stream_chat(self):
        seen = []

        def fake_stream(*args, **kwargs):
            phases = kwargs["phases"]
            phases.mark("started", 1)
            time.sleep(0.08)
            phases.mark("headers", 1)
            time.sleep(0.08)
            yield {"t": "text", "v": "x"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        with mock.patch.object(client, "stream_chat", fake_stream):
            for ev in client.stream_chat_background("m", [], poll_interval=0.01):
                seen.append(ev)
        phases = [ev.get("phase") for ev in seen if ev["t"] == "poll"]
        self.assertIn("started", phases)
        self.assertIn("headers", phases)
        self.assertLess(phases.index("started"), phases.index("headers"))
        self.assertTrue(all("phase_age" in ev for ev in seen if ev["t"] == "poll"))

    def test_caller_provided_observer_still_receives_marks(self):
        mine = client.LivePhases()

        def fake_stream(*args, **kwargs):
            kwargs["phases"].mark("headers", 1)
            yield {"t": "done", "reason": "stop", "usage": {}}

        with mock.patch.object(client, "stream_chat", fake_stream):
            list(client.stream_chat_background("m", [], phases=mine, poll_interval=0.01))
        self.assertEqual(mine.snapshot()[0], "headers")


if __name__ == "__main__":
    unittest.main()
