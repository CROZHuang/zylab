"""端到端：Ctrl+B 转后台的任务完成后，结果自动排成 prompt 交回模型、模型自动开新 turn。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BODY = r'''
seen = []
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    seen.append(str(messages[-1].get("content") or ""))
    if calls["n"] == 1:
        yield {"t": "tool", "v": [{
            "id": "call-bg", "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({
                "command": "sleep 1.2; echo BG_DONE_MARKER", "timeout": 20})},
        }]}
    elif calls["n"] == 2:
        yield {"t": "text", "v": "AFTER_BACKGROUND"}
    else:
        yield {"t": "text", "v": "GOT_HANDOFF"}
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
session = zylab.Session(agent)
session.cfg["sandbox"]["mode"] = "disabled"
session.cfg["sandbox"]["network_isolation"] = False
agent.hook_cfg = session.cfg
session.auto = True
task_ids = iter(["bg000001"])
session.task_manager.id_factory = lambda: next(task_ids)
agent.confirm = session.confirm
announce_when(lambda: session.task_manager.get("bg000001").status.value == "running",
              "ZYLAB_TEST_BG_RUNNING")
announce_when(lambda: (calls["n"] >= 3 and session.controller.current_turn_id is None
                       and not session.controller.queued
                       and not session.pump.snapshot().busy),
              "ZYLAB_TEST_HANDOFF_DONE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({
    "calls": calls["n"],
    "handoff_prompt": seen[2] if len(seen) > 2 else "",
    "task_status": session.task_manager.get("bg000001").status.value,
    "background": session.task_manager.get("bg000001").background,
}, ensure_ascii=False))
'''


class TaskHandoffPTYTests(unittest.TestCase):
    def test_finished_background_task_starts_a_new_turn_with_its_result(self):
        out, result = run_pty_child(
            HEAD + BODY,
            [PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(b"\x02", after="ZYLAB_TEST_BG_RUNNING", delay=0.2),   # Ctrl+B
             PTYSend(b"/exit\r", after="ZYLAB_TEST_HANDOFF_DONE", delay=0.3)],
            cwd=ROOT, timeout=40.0)
        self.assertTrue(result["background"], "Ctrl+B 应把任务转到后台")
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(result["calls"], 3, "转后台后一次、交接后一次")
        self.assertIn("[后台任务 bg000001 已完成", result["handoff_prompt"])
        self.assertIn("BG_DONE_MARKER", result["handoff_prompt"])
        self.assertIn("结果已排入主 agent", out)
        self.assertIn("GOT_HANDOFF", out)


if __name__ == "__main__":
    unittest.main()
