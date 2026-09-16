"""B1 端到端：Shift+Tab 三次，提示符 › → ⏵⏵› → plan› → ›，模式可见。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

TAIL = r'''
calls = {"n": 0}
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
session = zylab.Session(agent)
agent.confirm = session.confirm
modes = []
_orig = session.cycle_permission_mode
def _tracked():
    modes.append(_orig()); print("ZYLAB_TEST_MODE_" + str(len(modes)), flush=True); return modes[-1]
session.cycle_permission_mode = _tracked
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"modes": modes, "plan": session.plan_mode, "accept": session.accept_edits}))
'''

SHIFT_TAB = b"\x1b[Z"


class ModeCyclePTYTests(unittest.TestCase):
    def test_shift_tab_cycles_modes_and_prompt_shows_them(self):
        out, r = run_pty_child(
            HEAD + TAIL,
            [PTYSend(SHIFT_TAB, after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(SHIFT_TAB, after="ZYLAB_TEST_MODE_1"),
             PTYSend(SHIFT_TAB, after="ZYLAB_TEST_MODE_2"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_MODE_3")],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=25.0)
        self.assertEqual(r["modes"], ["accept-edits", "plan", "default"])
        self.assertFalse(r["plan"]); self.assertFalse(r["accept"])
        i_accept = out.find("⏵⏵›"); i_plan = out.find("plan›")
        self.assertGreater(i_accept, -1, "接受编辑模式要在提示符里可见")
        self.assertGreater(i_plan, i_accept, "plan 提示符在其后")
        self.assertIn("接受编辑", out)


if __name__ == "__main__":
    unittest.main()
