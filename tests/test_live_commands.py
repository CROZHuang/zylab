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

    # 与 core.agent.Agent 同一对方法：推理强度按「网关 + 模型」记
    def effort_for(self, gateway=None, model=None):
        return getattr(self, "efforts", {}).get(
            (gateway or self.gateway, model or self.model))

    def set_effort(self, gateway, model, effort):
        efforts = self.__dict__.setdefault("efforts", {})
        if effort:
            efforts[(gateway, model)] = effort
        else:
            efforts.pop((gateway, model), None)


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

    @staticmethod
    def catalog(gateway, count, added=()):
        return {"count": count, "gateway": gateway, "added": list(added),
                "delisted": [], "restored": [], "changed": bool(added)}

    def test_model_refresh_covers_every_keyed_gateway_without_route_change(self):
        """以前只刷当前网关：用户加了官方 DeepSeek 的 key、当前网关是 boyue，
        DeepSeek 的目录就从来没被取过（2026-09-24）。"""
        catalogs = {"deepinfer": self.catalog("deepinfer", 17),
                    "deepseek": self.catalog("deepseek", 2)}
        with (
                mock.patch.object(CLI.M, "keyed_gateways",
                                  return_value=["deepinfer", "deepseek"]),
                mock.patch.object(CLI.M, "refresh_catalog",
                                  side_effect=catalogs.__getitem__) as fetch,
                mock.patch.object(CLI.M, "probe_targets", return_value=[]),
                mock.patch.object(CLI.M, "due_for_probe", return_value=[]),
                mock.patch.object(CLI.tui, "supported", return_value=False),
                contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_model(self.session, "refresh")

        self.assertEqual([c.args for c in fetch.call_args_list],
                         [("deepinfer",), ("deepseek",)])
        self.assertEqual(self.session.route_selection(), (
            "old-model", "deepinfer"))
        self.assertIn("deepinfer 已刷新，17 个模型", output.getvalue())
        self.assertIn("deepseek 已刷新，2 个模型", output.getvalue())
        self.assertNotIn("models", CLI.REGISTRY)
        self.assertNotIn("/models", CLI.COMMANDS)
        canonical, entry, exit_alias = CLI.resolve_command("models")
        self.assertEqual(canonical, "models")
        self.assertIsNone(entry)
        self.assertFalse(exit_alias)

    def test_model_refresh_probes_exactly_what_is_due(self):
        """用户定：刷新时自动实测一次。实测的是「该测的旗舰」里还没测过/到期的，
        不是整个目录 —— 已测过的不重复花钱。"""
        probed = []

        def probe(gateway, model):
            probed.append((gateway, model))
            return {"gateway": gateway, "id": model, "status": "ok",
                    "supports_tools": True, "first_token_s": 1.2}

        with (
                mock.patch.object(CLI.M, "keyed_gateways", return_value=["boyue"]),
                mock.patch.object(CLI.M, "refresh_catalog",
                                  return_value=self.catalog(
                                      "boyue", 240, added=["gpt-6-sol"])),
                mock.patch.object(CLI.M, "probe_targets",
                                  return_value=["gpt-6-sol", "gpt-5.6-sol"]) as targets,
                mock.patch.object(CLI.M, "due_for_probe",
                                  return_value=["gpt-6-sol"]) as due,
                mock.patch.object(CLI.M, "probe", side_effect=probe),
                mock.patch.object(CLI.tui, "supported", return_value=False),
                contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_model(self.session, "refresh")

        # 偏好家族来自会话的 settings（这里没配 → None = 出厂六家）
        targets.assert_called_once_with("boyue", preferred=None)
        due.assert_called_once_with(
            "boyue", ["gpt-6-sol", "gpt-5.6-sol"], revalidate=False)
        self.assertEqual(probed, [("boyue", "gpt-6-sol")])
        self.assertIn("gpt-6-sol", output.getvalue())
        self.assertIn("可用", output.getvalue())

    def test_an_unreachable_gateway_does_not_stop_the_others(self):
        """够不着的网关报一行、给出**只针对它**的代理出路，其余网关照常刷新与实测。"""
        def refresh(gateway):
            if gateway == "deepseek":
                raise CLI.M.CatalogError(
                    "deepseek 取模型列表失败: <urlopen error timed out>",
                    unreachable=True)
            return self.catalog(gateway, 240)

        with (
                mock.patch.object(CLI.M, "keyed_gateways",
                                  return_value=["deepseek", "boyue"]),
                mock.patch.object(CLI.M, "refresh_catalog", side_effect=refresh),
                mock.patch.object(CLI.M, "probe_targets", return_value=["glm-5.3"]),
                mock.patch.object(CLI.M, "due_for_probe", return_value=["glm-5.3"]),
                mock.patch.object(CLI.M, "probe", return_value={
                    "status": "ok", "supports_tools": True}) as probe,
                mock.patch.object(CLI.tui, "supported", return_value=False),
                contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_model(self.session, "refresh")

        text = output.getvalue()
        self.assertIn("deepseek 取模型列表失败", text)
        self.assertIn("ZYLAB_API_PROXY_DEEPSEEK", text)
        self.assertIn("boyue 已刷新，240 个模型", text)
        probe.assert_called_once_with("boyue", "glm-5.3")

    def test_a_rejected_key_gets_no_proxy_advice(self):
        """401 说明请求**到了**网关：该查的是 key，不是路由。"""
        with (
                mock.patch.object(CLI.M, "keyed_gateways", return_value=["deepseek"]),
                mock.patch.object(CLI.M, "refresh_catalog", side_effect=CLI.M.CatalogError(
                    "deepseek 取模型列表失败: HTTP Error 401: Unauthorized")),
                mock.patch.object(CLI.tui, "supported", return_value=False),
                contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_model(self.session, "refresh")

        self.assertIn("401", output.getvalue())
        self.assertNotIn("API_PROXY", output.getvalue())

    def run_temperature(self, rest, accepts=True):
        with (
                mock.patch.object(CLI.M, "supports_temperature",
                                  return_value=accepts),
                contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_temperature(self.session, rest)
        return output.getvalue()

    def test_temperature_is_settable_for_the_session(self):
        self.agent.temperature = 0.3
        text = self.run_temperature("0.7")
        self.assertEqual(self.agent.temperature, 0.7)
        self.assertIn("本会话生效", text)
        self.assertIn("0.7", self.run_temperature(""))

    def test_temperature_rejects_values_outside_0_to_2(self):
        self.agent.temperature = 0.3
        for bad in ("3", "-0.1", "warm", "nan"):
            with self.subTest(value=bad):
                text = self.run_temperature(bad)
                self.assertEqual(self.agent.temperature, 0.3)
                self.assertIn("0–2", text)

    def test_temperature_save_writes_the_user_default(self):
        self.agent.temperature = 0.3
        self.run_temperature("0.9")
        with mock.patch.object(CLI.CFG, "write_user",
                               return_value="settings.json") as write:
            text = self.run_temperature("save")
        write.assert_called_once_with({"temperature": 0.9})
        self.assertIn("已存为默认", text)
        self.assertEqual(self.session.cfg["temperature"], 0.9)

    def test_temperature_default_restores_the_setting(self):
        self.session.cfg["temperature"] = 0.5
        self.run_temperature("1.2")
        self.run_temperature("default")
        self.assertEqual(self.agent.temperature, 0.5)

    def test_temperature_says_when_the_model_will_not_receive_it(self):
        self.agent.temperature = 0.3
        text = self.run_temperature("", accepts=False)
        self.assertIn("不接受 temperature", text)

    def test_temperature_setting_parsing(self):
        self.assertEqual(CLI.CFG.temperature_policy({}), 0.3)
        self.assertEqual(CLI.CFG.temperature_policy({"temperature": 1}), 1.0)
        for bad in ("hot", 5, -1, True, None):
            with self.subTest(value=bad):
                self.assertEqual(
                    CLI.CFG.temperature_policy({"temperature": bad}), 0.3)

    # ---- /model 的第三级：推理强度（用户 2026-09-24：「effort 并到 /model 中」）----
    RESPONSES_RECORD = {"status": "ok", "supports_tools": True, "api": "responses",
                        "effort_options": ["low", "medium", "high"],
                        "effort_default": "medium"}
    CHAT_RECORD = {"status": "ok", "supports_tools": True}

    def open_picker(self, record, picks):
        rows = [{"gateway": "deepinfer", "id": "gpt-6-sol", "family": "OpenAI",
                 "status": "ok", "supports_tools": True}]
        with (
                mock.patch.object(CLI.tui, "supported", return_value=True),
                mock.patch.object(CLI.M, "default_picker_rows", return_value=rows),
                mock.patch.object(CLI.M, "get", return_value=dict(record)),
                mock.patch.object(CLI, "_gateway_key_label", return_value=""),
                mock.patch.object(CLI, "_picker_gateway_counts", return_value=""),
                mock.patch.object(self.session, "pick", create=True,
                                  side_effect=lambda rows_, **kw: picks.pop(0)(rows_))
                as pick,
                contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_model(self.session, "")
        return pick, output.getvalue()

    def test_picking_a_responses_model_opens_the_effort_menu(self):
        pick, text = self.open_picker(self.RESPONSES_RECORD, [
            lambda rows: rows[0],
            lambda rows: next(r for r in rows if r["effort"] == "high")])
        self.assertEqual(pick.call_count, 2)
        labels = [row["label"] for row in pick.call_args_list[1].args[0]]
        self.assertEqual(labels, ["模型默认（medium）   ← 当前", "low", "medium", "high"],
                         "选项就是网关给的那几个，外加「模型默认」；还没选过时当前就是默认")
        self.assertEqual(self.agent.effort_for("deepinfer", "gpt-6-sol"), "high")
        self.assertIn("推理 high", text)

    def test_a_chat_model_gets_no_effort_menu(self):
        pick, text = self.open_picker(self.CHAT_RECORD, [lambda rows: rows[0]])
        self.assertEqual(pick.call_count, 1)
        self.assertNotIn("推理", text)

    def test_escape_in_the_effort_menu_keeps_what_was_there(self):
        self.agent.set_effort("deepinfer", "gpt-6-sol", "low")
        pick, text = self.open_picker(self.RESPONSES_RECORD, [
            lambda rows: rows[0], lambda rows: None])
        self.assertEqual(self.agent.effort_for("deepinfer", "gpt-6-sol"), "low")
        self.assertIn("推理 low", text)
        labels = [row["label"] for row in pick.call_args_list[1].args[0]]
        self.assertIn("low   ← 当前", labels)

    def type_model(self, rest, record):
        with (
                mock.patch.object(CLI.M, "get", return_value=dict(record)),
                mock.patch.object(CLI.M, "where", return_value=["deepinfer"]),
                contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            CLI.cmd_model(self.session, rest)
        return output.getvalue()

    def test_a_typed_effort_is_checked_against_the_gateways_options(self):
        text = self.type_model("gpt-6-sol@deepinfer ultra", self.RESPONSES_RECORD)
        self.assertIn("low / medium / high", text)
        self.assertEqual(self.session.route_selection(), ("old-model", "deepinfer"),
                         "不合法就不切换")
        text = self.type_model("gpt-6-sol@deepinfer high", self.RESPONSES_RECORD)
        self.assertEqual(self.agent.effort_for("deepinfer", "gpt-6-sol"), "high")
        self.assertIn("推理 high", text)

    def test_a_typed_effort_on_a_chat_model_says_it_will_not_be_sent(self):
        text = self.type_model("glm-5.3@deepinfer high", self.CHAT_RECORD)
        self.assertIn("不会发出去", text)
        self.assertIsNone(self.agent.effort_for("deepinfer", "glm-5.3"))

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
                 ("/model", CLI.cmd_model),
                 ("/temperature", CLI.cmd_temperature))
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
                               ("/agents", CLI.cmd_agents),
                               ("/temperature", CLI.cmd_temperature)):
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
            "/recap": {"help"},           # 09-20 起 /recap 直接现写一行，没有 now 子动作了
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
