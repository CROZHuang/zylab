"""端到端：真 repl 里模型派一个子代理 → 决策门 → 「其他模型…」→ 完整列表里打字筛选、
Enter → 子代理真的跑在选中的模型上。

单元测试各自假定了一截：决策门的回答、列表的回答、主线程怎么弹。只有串起来跑才看得见
「工具线程在等主线程时，主线程能不能在一轮进行中再弹一个列表」这类问题。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.pty_harness import PTYSend, run_pty_child

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
DOWN = b"\x1b[B"

BODY = r'''
import json
import os
import tempfile
import time

with tempfile.TemporaryDirectory() as home:
    os.environ["HOME"] = home
    os.environ["TERM"] = "xterm-256color"
    import zylab
    from core import agent as agent_mod, client, models, store, subagent
    from tests.pty_harness import announce_when, install_ready_input_pump

    install_ready_input_pump()
    store.ensure_home()
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.model = "deepseek-flash"
    # 全用自带 https 地址的公共网关：临时 HOME 里没有 Boyue / DeepInfer 的地址，
    # 传输守卫会在权限这一步就拒掉（真实环境里用户配过）。
    agent.gateway = "deepseek"
    agent.session_id = "subagent-gate-pty"
    agent.messages = [{"role": "system", "content": "s"}]
    agent.tokens_in = agent.tokens_out = agent.last_total = agent.turns = 0
    agent.cache_read = agent.cache_write = 0
    agent.cache_reported = False
    agent.compact_failed = None
    agent.ctx_limit = 100000
    agent.compact_at = 70000
    agent.ctx_known = True
    agent.ctx_limit_source = "test"
    agent.context_summary = None
    agent.context_invalid_reason = None
    agent._compact_failed_key = None
    agent._last_age_notice_key = None
    agent._seen_ok_hi = 0
    agent.started = time.time()
    agent.supports_tools = True
    agent.hook_cfg = {}

    def row(gateway, model, tools_ok=True):
        return {"gateway": gateway, "id": model, "status": "ok",
                "supports_tools": tools_ok, "context": 128000}
    ROWS = [row("deepseek", "deepseek-flash"), row("openai", "gpt-6-sol"),
            row("anthropic", "claude-opus-5-5"), row("moonshot", "kimi-k3"),
            row("siliconflow", "minimax-m2.7")]
    models.default_picker_rows = lambda **_kw: [dict(r) for r in ROWS]

    launched = []
    def fake_run(task, context=None, **kwargs):
        launched.append([kwargs.get("model"), kwargs.get("gateway")])
        return "CHILD_REPORT"
    subagent.run = fake_run

    phase = {"value": 0}
    def fake_stream(model, messages, **kwargs):
        if phase["value"] == 0:
            phase["value"] = 1
            yield {"t": "tool", "v": [{
                "id": "spawn-call",
                "function": {"name": "subagent", "arguments": json.dumps({
                    "task": "查北非网格的投影参数"})},
            }]}
            yield {"t": "done", "reason": "tool_calls", "usage": {}}
        else:
            yield {"t": "text", "v": "PARENT_DONE"}
            yield {"t": "done", "reason": "stop", "usage": {}}
    client.stream_chat_background = fake_stream

    session = zylab.Session(agent)
    agent.confirm = session.confirm
    mode = lambda: session.pump.snapshot().mode
    announce_when(lambda: mode() == "decision_gate", "ZYLAB_TEST_GATE_OPEN")
    announce_when(lambda: mode() == "prompt", "ZYLAB_TEST_GATE_NOTES")
    announce_when(lambda: mode() == "picker", "ZYLAB_TEST_LIST_OPEN")
    announce_when(
        lambda: (launched and session.controller.current_turn_id is None
                 and not session.pump.snapshot().busy),
        "ZYLAB_TEST_TURN_DONE")
    zylab.repl(session, "deepseek-flash")
    print("RESULT:" + json.dumps({
        "launched": launched,
        "events": [r["kind"] for r in session.controller.journal.events],
        "interactions": [
            (r.get("payload") or {}).get("kind")
            for r in session.controller.journal.events
            if r["kind"] == "interaction_requested"],
    }, ensure_ascii=False))
'''


class SubagentModelGateEndToEnd(unittest.TestCase):
    def test_other_models_then_filter_then_enter_runs_the_child_on_it(self):
        output, result = run_pty_child(
            BODY,
            [
                PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
                PTYSend(DOWN, after="ZYLAB_TEST_GATE_OPEN", delay=0.2),
                PTYSend(DOWN, delay=0.1),
                PTYSend(DOWN, delay=0.1),
                PTYSend(b"\r", delay=0.2),
                PTYSend(b"\r", after="ZYLAB_TEST_GATE_NOTES", delay=0.2),
                PTYSend(b"minimax", after="ZYLAB_TEST_LIST_OPEN", delay=0.2),
                PTYSend(b"\r", delay=0.3),
                PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE", delay=0.3),
            ],
            cwd=ROOT, timeout=40.0)
        plain = ANSI.sub("", output)
        # 子代理真的跑在完整列表里选中的那个模型上
        self.assertEqual(result["launched"], [["minimax-m2.7", "siliconflow"]])
        # 先决策门、后列表，两次都记了账
        self.assertEqual(result["interactions"], ["decision_gate", "pick"])
        self.assertEqual(result["events"].count("interaction_resolved"), 2)
        # 决策门里：当前模型 + 两家旗舰 + 「其他模型…」
        self.assertIn("deepseek-flash（当前模型）", plain)
        self.assertIn("gpt-6-sol", plain)
        self.assertIn("其他模型…", plain)
        # 列表标题说清是给谁选
        self.assertIn("agent1", plain)
        self.assertIn("PARENT_DONE", plain)
        self.assertNotIn("TerminalRenderer 只能由 owner 线程", plain)


if __name__ == "__main__":
    unittest.main()
