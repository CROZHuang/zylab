"""Busy slash-command and turn-boundary route regression tests."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zylab as CLI
from core import controller
from tests.pty_harness import PTYSend, run_pty_child


class FakeAgent:
    def __init__(self):
        self.model = "old-model"
        self.gateway = "deepinfer"
        self.session_id = "live-command-session"
        self.last_total = 0
        self.compact_at = 70_000
        self.ctx_known = True
        self.ctx_limit = 100_000
        self.supports_tools = True
        self.tokens_in = 0
        self.tokens_out = 0
        self.set_calls = []
        self.explicit_marks = 0

    def set_model(self, model):
        self.model = str(model)
        self.ctx_known = True
        self.ctx_limit = 200_000
        self.supports_tools = True
        self.set_calls.append((self.gateway, self.model))
        return self.ctx_limit

    def mark_route_explicit(self):
        self.explicit_marks += 1


class LiveCommandTests(unittest.TestCase):
    def setUp(self):
        self.original_gateway = CLI.client.GATEWAY
        CLI.client.set_gateway("deepinfer")
        self.agent = FakeAgent()
        self.session = object.__new__(CLI.Session)
        self.session.ag = self.agent
        self.session._pending_route = None
        self.session._title = "route test"
        self.session.last_ctx = None
        self.session.plan_mode = False
        self.session.auto = False
        self.session._auto_override = None
        self.session.always = set()
        self.session.cfg = {"permissions": {}}
        self.session.renderer = None
        self.session._sandbox_status = {
            "marker": "SANDBOXED",
            "network_marker": "NET ISOLATED",
        }
        self.journal = controller.MemoryJournal()
        self.session.controller = controller.SessionController(
            self.agent.session_id, self.journal)

    def tearDown(self):
        CLI.client.set_gateway(self.original_gateway)

    def start_turn(self):
        action = self.session.controller.submit(
            "active", controller.QueueMode.NEXT_TURN)
        self.assertEqual(action.kind, controller.ActionKind.START_TURN)
        return action

    def test_live_command_categories_share_the_busy_dispatch_resolver(self):
        expected = {
            "/usage": "usage",
            "/cost": "cost",
            "/context": "context",
            "/model health": "model",
            "/model next-model": "model",
            "/model gateway boyue": "model",     # /gateway 已并入 /model（2026-09-04）
            "/auto": "auto",
            "/workflow status": "workflow",
            "/tasks": "tasks",
        }
        for line, canonical in expected.items():
            with self.subTest(line=line):
                resolved = CLI.resolve_live_command(line)
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved[0], canonical)
                self.assertIs(resolved[1], CLI.REGISTRY[canonical][0])

        for line in (
                "/compact", "/clear", "/resume", "/models",
                "plain prompt", "/mo",
                # /mouse 已随 1aaf879 从产品命令面删除；隐藏兼容入口不算 live
                "/mouse native",
                # /gateway 并入 /model gateway 后同理
                "/gateway boyue"):
            with self.subTest(line=line):
                self.assertIsNone(CLI.resolve_live_command(line))

    def test_active_turn_stages_route_until_predispatched_next_action(self):
        source = self.start_turn()
        self.session.controller.submit(
            "queued next", controller.QueueMode.NEXT_TURN)

        staged = self.session.request_route_change(
            "next-model", "boyue")

        self.assertTrue(staged["staged"])
        self.assertEqual(self.agent.model, "old-model")
        self.assertEqual(self.agent.gateway, "deepinfer")
        self.assertEqual(CLI.client.GATEWAY, "deepinfer")
        self.assertEqual(self.agent.set_calls, [])
        self.assertIn(
            "next next-model@boyue", CLI.status_line(self.session))

        # Controller pre-dispatches the queued turn before Session.run_actions
        # gets the boundary back. The route must still apply before that action
        # starts its first provider request.
        next_action = self.session.controller.fail_turn(
            "test boundary", details={"source": source.turn_id})
        self.assertEqual(
            next_action.kind, controller.ActionKind.START_TURN)
        self.assertIsNotNone(self.session.controller.current_turn_id)

        applied = self.session.apply_pending_route()

        self.assertFalse(applied["staged"])
        self.assertEqual(
            (self.agent.model, self.agent.gateway),
            ("next-model", "boyue"))
        self.assertEqual(CLI.client.GATEWAY, "boyue")
        self.assertEqual(self.agent.set_calls, [("boyue", "next-model")])
        self.assertEqual(self.agent.explicit_marks, 1)
        self.assertIsNone(self.session._pending_route)
        kinds = [event["kind"] for event in self.journal.events]
        self.assertIn("route_change_staged", kinds)
        self.assertIn("route_change_applied", kinds)

    def test_repeated_busy_route_commands_merge_last_selection(self):
        self.start_turn()

        first = self.session.request_route_change(
            "model-a", "boyue")
        second = self.session.request_route_change(
            "model-b", "deepinfer")

        self.assertTrue(first["staged"])
        self.assertTrue(second["staged"])
        self.assertEqual(
            self.session.route_selection(), ("model-b", "deepinfer"))
        self.assertEqual(self.agent.set_calls, [])

        self.session.controller.fail_turn("done")
        self.session.apply_pending_route()
        self.assertEqual(
            (self.agent.model, self.agent.gateway),
            ("model-b", "deepinfer"))

    def test_route_stage_journal_failure_leaves_runtime_unchanged(self):
        self.start_turn()
        with mock.patch.object(
                self.session.controller, "record_event",
                side_effect=RuntimeError("journal unavailable")):
            with self.assertRaisesRegex(RuntimeError, "journal unavailable"):
                self.session.request_route_change(
                    "never-staged", "boyue")

        self.assertIsNone(self.session._pending_route)
        self.assertEqual(
            (self.agent.model, self.agent.gateway),
            ("old-model", "deepinfer"))
        self.assertEqual(CLI.client.GATEWAY, "deepinfer")
        self.assertEqual(self.agent.set_calls, [])

    def test_cmd_model_and_gateway_update_one_pending_route(self):
        self.start_turn()
        record = {
            "supports_tools": True,
            "status": "ok",
            "context": 128_000,
        }
        with (
            mock.patch.object(CLI.M, "where", return_value=["boyue"]),
            mock.patch.object(
                CLI.M, "resolve", return_value=("boyue", "new-model")),
            mock.patch.object(CLI.M, "get", return_value=record),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_model(self.session, "new-model")
            CLI.cmd_gateway(self.session, "deepinfer")

        self.assertEqual(
            self.session.route_selection(), ("new-model", "deepinfer"))
        self.assertEqual(self.agent.set_calls, [])
        self.assertIn("下一轮切到 new-model@boyue", output.getvalue())
        self.assertIn("下一轮切到 deepinfer", output.getvalue())

    def test_model_refresh_uses_selected_gateway_without_route_change(self):
        with (
                mock.patch.object(
                    CLI.M, "refresh_catalog",
                    return_value={"count": 17, "gateway": "deepinfer",
                                  "added": [], "delisted": [], "restored": [],
                                  "changed": False}) as fetch,
                mock.patch.object(CLI.tui, "supported", return_value=False),
                contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_model(self.session, "refresh")

        fetch.assert_called_once_with("deepinfer")
        self.assertEqual(self.session.route_selection(), (
            "old-model", "deepinfer"))
        self.assertIn("deepinfer 已刷新，17 个模型", output.getvalue())
        self.assertNotIn("models", CLI.REGISTRY)
        self.assertNotIn("/models", CLI.COMMANDS)
        canonical, entry, exit_alias = CLI.resolve_command("models")
        self.assertEqual(canonical, "models")
        self.assertIsNone(entry)
        self.assertFalse(exit_alias)

    def test_auto_toggle_changes_only_future_permission_decisions(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            prior = self.session.permission_decision(
                "write_file", {"path": "future.txt"})
            with contextlib.redirect_stdout(io.StringIO()) as output:
                CLI.cmd_auto(self.session, "")
            future = self.session.permission_decision(
                "write_file", {"path": "future.txt"})

        self.assertFalse(prior["allowed"])
        self.assertEqual(prior["decision"], "noninteractive_deny")
        self.assertTrue(future["allowed"])
        self.assertEqual(future["decision"], "auto_approve")
        self.assertIn("仅影响尚未开始的后续审批", output.getvalue())


class BusyCommandPTYTests(unittest.TestCase):
    def test_auto_executes_during_stream_without_becoming_model_input(self):
        body = r"""
import json
import os
import tempfile
import threading
import time

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
    agent.session_id = "busy-command-pty"
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
        release.wait()
        yield {"t": "text", "v": "done"}
        yield {"t": "done", "reason": "stop", "usage": {}}

    client.stream_chat = fake_stream
    session = zylab.Session(agent)
    agent.confirm = session.confirm
    announce_when(
        lambda: session.auto,
        "ZYLAB_TEST_AUTO_ON",
        on_ready=release.set)
    announce_when(
        lambda: (calls["n"] == 1
                 and session.controller.current_turn_id is None
                 and not session.controller.queued
                 and not session.pump.snapshot().busy),
        "ZYLAB_TEST_IDLE")
    zylab.repl(session, "m")
    users = [
        message["content"] for message in agent.messages
        if message.get("role") == "user"
    ]
    result = {
        "auto": session.auto,
        "calls": calls["n"],
        "users": users,
    }
print("RESULT:" + json.dumps(result, ensure_ascii=False))
"""
        output, result = run_pty_child(
            body,
            [
                PTYSend(b"first\r", after="ZYLAB_TEST_PUMP_READY"),
                PTYSend(b"/auto\r", after="ZYLAB_TEST_STREAM_STARTED"),
                PTYSend(b"/exit\r", after="ZYLAB_TEST_IDLE"),
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=8.0,
        )

        self.assertTrue(result["auto"])
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["users"], ["first"])
        self.assertIn("仅影响尚未开始的后续审批", output)


if __name__ == "__main__":
    unittest.main()


class SubcommandMenuCoverageTests(unittest.TestCase):
    """新加的二级指令必须同时进 `/` 菜单，否则用户根本发现不了。

    2026-09-08 用户当场抓到：`/recap now` 做了功能却没进菜单，`/model check`
    也只写进了 help 文本。命令实现和菜单是两份需要手动同步的清单，这类遗漏
    不会有任何测试或运行时报错——所以在这里钉住。
    """

    def actions_in_source(self, function):
        import inspect                                # noqa: PLC0415
        import re                                     # noqa: PLC0415

        source = inspect.getsource(function)
        literals = set()
        # 两种写法都要认：`action == "x"` / `action in {...}`，以及
        # `/goal status` 用的 `raw.casefold() in {"status", "show"}`
        for pattern in (r'action\s*(?:==|in)\s*(\{[^}]*\}|"[a-z-]+")',
                        r'casefold\(\)\s*in\s*(\{[^}]*\})'):
            for match in re.finditer(pattern, source):
                literals.update(re.findall(r'"([a-z-]+)"', match.group(1)))
        return literals

    def test_menu_items_are_all_really_accepted(self):
        pairs = (("/recap", CLI.cmd_recap), ("/goal", CLI.cmd_goal),
                 ("/model", CLI.cmd_model))
        for name, function in pairs:
            accepted = self.actions_in_source(function)
            entry = CLI.COMMAND_SUBCOMMANDS.get(name) or {}
            listed = {item[0] for item in entry.get("items", ())}
            self.assertTrue(listed, f"{name} 没有二级菜单条目")
            dead = listed - accepted
            self.assertEqual(dead, set(), f"{name} 菜单里有实现不认的条目：{dead}")

    # 命令函数里出现、但**不是**二级子命令的字面量：别名、以及被比较的状态值。
    # 显式列出来，好让下面那条「实现有、菜单没有」的检查不被噪音淹没。
    NOT_SUBCOMMANDS = {
        "/goal": {"new", "create", "delete", "cancel", "done", "show",
                  "blocked"},
        "/recap": {"refresh", "rebuild"},
        "/model": {"status"},
        "/agents": set(),
        "/consult": set(),
        "/graft": set(),
        "/memory": set(),
        "/workflow": set(),
        "/architecture": set(),
        "/map": set(),
    }

    def test_actions_the_command_accepts_are_all_discoverable(self):
        """反向检查：实现认的动作必须能在 `/` 里被看见。

        这条才是真正的守卫 —— `/recap now`（09-08）和 `/agents workflow`（09-09）
        都是「功能做了、菜单没登记」，用户发现不了，也不会有任何测试失败。
        """
        for name, function in (("/recap", CLI.cmd_recap),
                               ("/goal", CLI.cmd_goal),
                               ("/model", CLI.cmd_model),
                               ("/agents", CLI.cmd_agents)):
            accepted = self.actions_in_source(function)
            accepted -= {"help", "?"} | self.NOT_SUBCOMMANDS.get(name, set())
            listed = {
                item[0] for item in
                (CLI.COMMAND_SUBCOMMANDS.get(name) or {}).get("items", ())}
            missing = accepted - listed
            self.assertEqual(
                missing, set(),
                f"{name} 实现认这些动作但菜单里没有：{sorted(missing)}")

    def test_the_commands_added_on_09_08_are_discoverable(self):
        expected = {
            "/recap": {"now"},
            "/model": {"check", "gateway", "refresh"},
            "/goal": {"accept", "reject"},
            "/agents": {"workflow"},
        }
        for name, required in expected.items():
            listed = {
                item[0] for item in
                (CLI.COMMAND_SUBCOMMANDS.get(name) or {}).get("items", ())}
            self.assertTrue(
                required <= listed,
                f"{name} 菜单缺少 {required - listed}")

    def test_every_registered_command_with_a_menu_exists(self):
        for name in CLI.COMMAND_SUBCOMMANDS:
            bare = name.lstrip("/")
            # REGISTRY 是分发的事实源；COMMANDS 是可见清单，隐藏兼容命令
            # （/map 等）刻意不在里面，但仍可显式输入，所以仍有二级菜单。
            self.assertIn(
                bare, CLI.REGISTRY, f"{name} 有二级菜单但命令本身没注册")
            if bare not in CLI.HIDDEN_COMPAT_COMMANDS:
                self.assertIn(name, CLI.COMMANDS)
