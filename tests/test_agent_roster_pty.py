"""端到端：在真的 repl 里按 ↓ ↓ Enter 切到子代理、Esc Esc 回 main（用户 2026-09-24：
「cc 是从 main 的对话框键盘点击 下，就可以选择到了」）。

不起子模型：list_agents / agent_record / 子代理记录都是合成的，按键走真的输入泵、
真的事件循环、真的渲染器。断言看屏幕上出现了什么、会话最后停在哪。
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
DOWN, ENTER, ESC = b"\x1b[B", b"\r", b"\x1b"

BODY = r'''
from core import tui
ROWS = [
    {"id": "a-111111111111", "kind": "subagent", "state": "running", "name": "归纳调研线摘要",
     "task": "归纳调研线摘要：三条线索各写两句。", "model": "kimi-k3", "gateway": "deepinfer",
     "seat": "Kimi", "created_at": "2026-09-07T12:05:00+00:00",
     "started_at": "2026-09-07T12:05:00+00:00",
     "parent_session_id": "think-pty", "parent_turn_id": ""},
    {"id": "a-222222222222", "kind": "subagent", "state": "running", "name": "抓取 arXiv 元数据",
     "task": "抓取 arXiv 元数据。", "model": "glm-5.3", "gateway": "deepinfer",
     "seat": "GLM", "created_at": "2026-09-07T12:05:00+00:00",
     "started_at": "2026-09-07T12:05:00+00:00",
     "parent_session_id": "think-pty", "parent_turn_id": ""},
]
BY_ID = {row["id"]: row for row in ROWS}
TRANSCRIPT = {"messages": [
    {"role": "system", "content": "你是子代理"},
    {"role": "user", "content": ROWS[0]["task"]},
    {"role": "assistant", "content": "CHILD_STEP_SENTINEL 先把三条线索读一遍"}]}
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "done", "reason": "stop", "usage": {}}
client.stream_chat = fake_stream
session = zylab.Session(agent)
agent.confirm = session.confirm
session.list_agents = lambda limit=100: [dict(row) for row in ROWS]
session.agent_record = (
    lambda identifier=None, refresh=False:
    dict(BY_ID[str(identifier or session.attached_agent_id)]))
session.agent_workspace.transcript = lambda run_id: dict(TRANSCRIPT)
seen = [None]
def watch():
    while True:
        value = session.attached_agent_id
        if seen[-1] != value:
            seen.append(value)
        time.sleep(0.02)
threading.Thread(target=watch, daemon=True).start()
focus = lambda: session.pump.snapshot().dock_selected
viewing = lambda: bool(getattr(session.renderer, "agent_view_active", False))
announce_when(lambda: any(isinstance(item, tui.DockItem) and item.event == "agent_attach"
                          for item in session.pump.snapshot().dock), "ZYLAB_TEST_ROSTER")
announce_when(lambda: focus() == 0, "ZYLAB_TEST_ON_MAIN")
announce_when(lambda: focus() == 1, "ZYLAB_TEST_ON_AGENT")
announce_when(lambda: seen[-1] == "a-111111111111" and viewing(), "ZYLAB_TEST_VIEWING")
announce_when(lambda: seen[-1] == "a-111111111111" and focus() is None,
              "ZYLAB_TEST_LEFT_ROSTER")
announce_when(lambda: len(seen) >= 3 and seen[-1] is None and not viewing(),
              "ZYLAB_TEST_BACK_ON_MAIN")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"attached": seen, "model_calls": calls["n"]},
                             ensure_ascii=False))
'''


class RosterFromTheComposer(unittest.TestCase):
    def test_down_down_enter_opens_the_agent_and_escape_twice_comes_back(self):
        out, result = run_pty_child(
            HEAD + BODY,
            [PTYSend(DOWN, after="ZYLAB_TEST_ROSTER", delay=0.2),
             PTYSend(DOWN, after="ZYLAB_TEST_ON_MAIN", delay=0.1),
             PTYSend(ENTER, after="ZYLAB_TEST_ON_AGENT", delay=0.1),
             PTYSend(ESC, after="ZYLAB_TEST_VIEWING", delay=0.2),
             PTYSend(ESC, after="ZYLAB_TEST_LEFT_ROSTER", delay=0.2),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_BACK_ON_MAIN", delay=0.3)],
            cwd=ROOT, timeout=40.0)
        plain = ANSI.sub("", out)
        # 会话：切过去、再回来；一次都没去问模型
        self.assertEqual(result["attached"], [None, "a-111111111111", None])
        self.assertEqual(result["model_calls"], 0)
        # 名册在输入框下面：main 一行、子代理一行，进名册之前写着按 ↓
        self.assertIn("● main", plain)
        self.assertIn("◯ Kimi  归纳调研线摘要", plain)
        self.assertIn("↓ 选择", plain)
        # 键盘焦点：先落在 main，再到子代理
        self.assertIn("❯ ● main", plain)
        self.assertIn("❯ ◯ Kimi", plain)
        self.assertIn("Enter 查看", plain)
        # 切过去以后：整屏是它的记录，输入框写明发给谁
        self.assertIn("任务说明（主代理交给它的原话）", plain)
        self.assertIn("CHILD_STEP_SENTINEL", plain)
        self.assertIn("发给 @归纳调研线摘要", plain)
        # 以前 attach 会先往主会话里打一段预览；有整屏视图就不该再打
        self.assertNotIn("agent preview 结束", plain)
        # 切过去以后不许再冒出一帧「切换前的名册」（main 仍是 ●、它仍是 ◯）：
        # attach 发出的重画曾经带着旧名册，排在正确那帧之后又画一遍。
        switched = plain[plain.index("ZYLAB_TEST_ON_AGENT"):
                         plain.index("ZYLAB_TEST_LEFT_ROSTER")]
        self.assertIsNone(re.search(r"● main\s*\n\s*❯ ◯ Kimi", switched))


if __name__ == "__main__":
    unittest.main()
