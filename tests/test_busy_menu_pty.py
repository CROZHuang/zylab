"""真实 PTY：模型流式输出期间打 `/`，菜单要真的画出来且不弄乱输出流。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.pty_harness import PTYSend, run_pty_child


BODY = r'''
import json
import os
import sys
import tempfile
import threading
import time

# HOME 必须在 import core.store **之前**设好 —— store 在 import 时就把 18 个路径
# 常量绑死（core/store.py:33 起）。漏掉这层隔离，测试会话会写进用户真实的
# ~/.zylab/sessions/，正是 skills/pty-tui-testing/SKILL.md 明令禁止的事。
with tempfile.TemporaryDirectory() as home:
    os.environ["HOME"] = home
    os.environ["TERM"] = "xterm-256color"
    import zylab
    from core import agent as agent_mod, client, store
    from tests.pty_harness import announce_when, install_ready_input_pump

    install_ready_input_pump()
    store.ensure_home()
    agent = agent_mod.Agent.__new__(agent_mod.Agent)
    agent.model = "m"
    agent.gateway = "deepinfer"
    agent.session_id = "busy-menu-pty"
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

    release = threading.Event()
    calls = {"n": 0}
    def fake_stream(model, messages, **kwargs):
        calls["n"] += 1
        print("ZYLAB_TEST_STREAM_STARTED", flush=True)
        yield {"t": "text", "v": "MODELTEXT"}
        release.wait(10)
        yield {"t": "done", "reason": "stop", "usage": {}}
    client.stream_chat = fake_stream

    session = zylab.Session(agent)
    agent.confirm = session.confirm

    seen = {}
    announce_when(
        lambda: bool(session.pump.snapshot().options),
        "ZYLAB_TEST_MENU_OPEN",
        on_ready=lambda: (
            seen.setdefault(
                "options",
                [row[0] for row in session.pump.snapshot().options]),
            seen.setdefault("busy", session.pump.snapshot().busy),
            release.set()))
    # 必须等 turn 真正结束再发 /exit。turn 边界上提交非 live 命令会撞上
    # handle_input_events 的 "active turn 意外 dispatch" 守卫（zylab.py
    # 4244 附近）—— 那是另一条路径的问题，不该混进这个用例。
    # 谓词必须要求假流被调用过：否则开局的空闲窗口就"完成"了，/exit 会被发进
    # turn 边界 —— 这条测试此前约 1/10 的 flake 正是这个。
    announce_when(
        lambda: (calls["n"] >= 1
                 and session.controller.current_turn_id is None
                 and not session.pump.snapshot().busy
                 and "空闲" in session.pump.snapshot().activity),
        "ZYLAB_TEST_TURN_DONE")

    zylab.repl(session, "m")

    print("RESULT:" + json.dumps(
        {"options": seen.get("options"), "busy": seen.get("busy")},
        ensure_ascii=False))
'''


class BusyMenuPTYTests(unittest.TestCase):
    def test_slash_opens_the_live_menu_during_streaming(self):
        """菜单在流式输出期间打开，且模型已吐的文本不被冲掉。"""
        output, result = run_pty_child(
            BODY,
            [PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
             PTYSend(b"/u", after="ZYLAB_TEST_STREAM_STARTED"),
             PTYSend(b"\x15", after="ZYLAB_TEST_MENU_OPEN"),
             PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE")],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=25.0,
        )
        self.assertTrue(result["busy"], result)
        # `/u` 在 live 名单里只匹配 /usage。注意 snapshot.options 是**可见的
        # 那一页**（LineEditor.PAGE = 8），不是完整匹配列表 —— 所以这里用
        # 前缀过滤到唯一一条来断言，而不是去翻页。
        self.assertEqual(result["options"], ["/usage"], result)
        # 模型已经吐出的文本不能被菜单冲掉。
        self.assertIn("MODELTEXT", output)


if __name__ == "__main__":
    unittest.main()
