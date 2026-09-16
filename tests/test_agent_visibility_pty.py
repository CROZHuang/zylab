"""J1–J5 端到端：子代理完成落永久行、正文不上屏、等待行原地更新、名册出现/消失、状态栏计数。

不真的起子模型：往 workspace 事件队列注入 lifecycle 事件，并让 list_agents 返回合成行。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

BODY = r'''
RUNNING = [
    {"id": "a-111111111111", "kind": "subagent", "state": "running", "name": "归纳病理线摘要",
     "model": "kimi-k3", "gateway": "deepinfer", "seat": "Kimi",
     "created_at": "2026-09-07T12:05:00+00:00", "started_at": "2026-09-07T12:05:00+00:00",
     "parent_session_id": "think-pty", "parent_turn_id": ""},
    {"id": "a-222222222222", "kind": "subagent", "state": "running", "name": "抓取 arXiv 元数据",
     "model": "glm-5.3", "gateway": "deepinfer", "seat": "GLM",
     "created_at": "2026-09-07T12:05:00+00:00", "started_at": "2026-09-07T12:05:00+00:00",
     "parent_session_id": "think-pty", "parent_turn_id": ""},
]
phase = {"rows": []}
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    if calls["n"] == 1:
        phase["rows"] = [dict(r) for r in RUNNING]
        yield {"t": "tool", "v": [{"id": "call-1", "type": "function",
                                   "function": {"name": "bash", "arguments": json.dumps({"command": "sleep 2.5; echo TOOL_DONE", "timeout": 20})}}]}
    else:
        yield {"t": "text", "v": "AFTER_TOOL"}
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
session = zylab.Session(agent)
session.cfg["sandbox"]["mode"] = "disabled"
session.cfg["sandbox"]["network_isolation"] = False
agent.hook_cfg = session.cfg
session.auto = True
session.list_agents = lambda limit=100: [dict(r) for r in phase["rows"]]
agent.confirm = session.confirm
def finish(index):
    row = dict(RUNNING[index]); row.update({"state": "completed", "ended_at": "2026-09-07T12:10:05+00:00",
                                            "result": f"REPORT_SENTINEL_{index} " + "内容" * 40})
    phase["rows"] = [r for r in phase["rows"] if r["id"] != row["id"]]
    session.agent_workspace._events.put_nowait({"kind": "agent_result_received", "payload": row})
def waiting_shown(n):
    return f"等待 {n} 个后台代理完成" in session.pump.snapshot().activity
announce_when(lambda: waiting_shown(2), "ZYLAB_TEST_WAITING_2")
threading.Timer(1.0, lambda: finish(0)).start()
announce_when(lambda: waiting_shown(1), "ZYLAB_TEST_WAITING_1")
threading.Timer(1.8, lambda: finish(1)).start()
samples = []
def sample():
    while True:
        try:
            label = session.pump.snapshot().activity
        except Exception:
            label = "?"
        if not samples or samples[-1] != label:
            samples.append(label)
        time.sleep(0.1)
threading.Thread(target=sample, daemon=True).start()
announce_when(lambda: (calls["n"] >= 2 and session.controller.current_turn_id is None
                       and not session.pump.snapshot().busy), "ZYLAB_TEST_TURN_DONE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"calls": calls["n"], "events": len(session._agent_event_log),
                              "samples": samples[-40:]}, ensure_ascii=False))
'''


class AgentVisibilityPTYTests(unittest.TestCase):
    def test_finished_lines_waiting_line_roster_and_status_count(self):
        out, result = run_pty_child(
            HEAD + BODY,
            [PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE", delay=0.4)],
            cwd=ROOT, timeout=40.0)
        plain = ANSI.sub("", out)
        self.assertEqual(result["events"], 2)
        # J1：永久完成行
        self.assertIn('● Agent "归纳病理线摘要" finished · 5m 5s', plain)
        self.assertIn('● Agent "抓取 arXiv 元数据" finished · 5m 5s', plain)
        self.assertIn("/agents peek a-1111111111", plain)
        # J2：正文不进父时间线
        self.assertNotIn("REPORT_SENTINEL", plain)
        # J3：等待行随子代理结束递减（原地更新的活动行）
        self.assertIn("ZYLAB_TEST_WAITING_2", out)
        self.assertIn("ZYLAB_TEST_WAITING_1", out)
        self.assertLess(out.find("ZYLAB_TEST_WAITING_2"), out.find("ZYLAB_TEST_WAITING_1"))
        # J4：名册行（席位 + label）出现过；J5：状态栏计数出现过
        self.assertIn("○ Kimi  归纳病理线摘要", plain)
        self.assertIn("○ GLM  抓取 arXiv 元数据", plain)
        self.assertIn("2 agents", plain)


if __name__ == "__main__":
    unittest.main()
