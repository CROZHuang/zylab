"""agent 主循环测试：工具分发、中断、轮次上限、plan 模式约束。

这里的 bug 表现为「模型行为怪异」而非报错，最难手动定位。
本轮开发中真实发生过的两个（Ctrl-C 留孤儿 tool_call、plan 模式漏发写工具）
都在下面钉死。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import (agent as A, checkpoints, client, controller as C, memory,
                state, tasks as task_runtime, tools)


def scripted(*rounds):
    """把若干轮回复脚本化成 stream_chat 的替身。

    每轮是 (text, [(工具名, 参数dict), ...])；工具列表为空表示这轮收尾。
    """
    state = {"i": 0}

    def fake(model, messages, tools=None, **kw):
        i = state["i"]; state["i"] += 1
        guard = kw.get("before_attempt")
        if guard is not None:
            guard({
                "attempt": 1, "purpose": "chat",
                "model": model, "gateway": kw.get("gateway"),
            })
        text, calls = rounds[i] if i < len(rounds) else ("done", [])
        if text:
            yield {"t": "text", "v": text}
        if calls:
            yield {"t": "tool", "v": [
                {"id": f"c{i}_{j}", "type": "function",
                 "function": {"name": n, "arguments": json.dumps(a)}}
                for j, (n, a) in enumerate(calls)]}
        yield {"t": "done", "reason": "stop",
               "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                         "total_tokens": 15}}
    return fake


def traced_scripted(agent, *rounds, cancel_after_tools=False):
    """Script provider attempts through the real metrics facade."""
    cursor = {"i": 0}

    def fake(model, messages, tools=None, cancel=None, gateway=None,
             trace_context=None, **kw):
        index = cursor["i"]
        cursor["i"] += 1
        text, calls = rounds[index] if index < len(rounds) else ("done", [])
        context = dict(trace_context or {})
        trace_id = f"provider-attempt-{index}"
        trace = agent.metrics.begin_attempt(
            gateway=gateway or agent.gateway,
            model=model,
            attempt=index + 1,
            request_id=trace_id,
            session_id=context.get("session_id"),
            turn_id=context.get("turn_id"),
            context_tokens_before=context.get("context_tokens_before"),
            context_limit=context.get("context_limit"),
            cwd=context.get("cwd"),
            raw=context.get("raw"),
        )
        trace.mark("started")
        trace.mark("headers")
        if text:
            trace.mark("first_delta")
            trace.mark("last_delta")
            yield {"t": "text", "v": text, "trace_id": trace_id}
        if calls:
            yield {"t": "tool", "v": calls, "trace_id": trace_id}
            if cancel_after_tools and cancel is not None:
                cancel.set()
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
        trace.finalize("ok", usage=usage)
        yield {
            "t": "done",
            "reason": "stop",
            "usage": usage,
            "trace_id": trace_id,
        }

    return fake


def mk(model="m"):
    ag = A.Agent.__new__(A.Agent)
    ag.model, ag.gateway, ag.session_id = model, "test", "t"
    ag.messages = [{"role": "system", "content": "s"}]
    ag.tokens_in = ag.tokens_out = ag.last_total = ag.turns = 0
    ag.cache_read = ag.cache_write = 0
    ag.cache_reported = False
    ag.compact_failed = None
    ag.ctx_limit, ag.compact_at, ag.ctx_known = 100_000, 70_000, True
    ag.confirm = lambda *a, **k: True
    ag.started = 0
    # 用量流水是**真副作用**：Agent.log_usage 会写进真实的
    # ~/.zylab/usage.jsonl，并污染由它重算的 stats-cache.json。
    # 实测这个 fixture 每跑一次套件写 4 条假记录（gateway="test"、
    # model="m"），累积到 212 条时已占真实统计的 39%。
    # 在 fixture 层一次断掉，而不是让每个用例各自 mock —— 后者漏一个
    # 就又开始污染，而且新增用例的人不会知道有这个坑。
    # 同 tests/test_agents.py:390 的做法。
    ag.log_usage = lambda *a, **k: None
    return ag


class ToolDispatch(unittest.TestCase):
    def test_provider_attempt_guard_reaches_each_tool_loop_request(self):
        ag = mk()
        reservations = []
        with mock.patch.object(
                client, "stream_chat",
                scripted(("", [("list_dir", {"path": "."})]),
                         ("done", []))), \
             mock.patch.object(tools, "run", return_value="OUT"):
            list(ag.run(
                "go", before_provider_attempt=reservations.append))

        self.assertEqual(len(reservations), 2)
        self.assertEqual(
            [item["model"] for item in reservations], ["m", "m"])
        self.assertEqual(
            [item["gateway"] for item in reservations], ["test", "test"])

    def test_tool_result_appended_with_matching_id(self):
        ag = mk()
        with mock.patch.object(client, "stream_chat",
                               scripted(("", [("list_dir", {"path": "."})]),
                                        ("好了", []))), \
             mock.patch.object(tools, "run", return_value="FAKE_OUT"), \
             mock.patch.object(ag, "log_usage"):
            evs = [e["t"] for e in ag.run("go")]
        self.assertIn("tool_start", evs)
        self.assertIn("tool_end", evs)
        tool_msgs = [m for m in ag.messages if m["role"] == "tool"]
        self.assertEqual(tool_msgs[0]["content"], "FAKE_OUT")
        called = [t["id"] for m in ag.messages if m.get("tool_calls")
                  for t in m["tool_calls"]]
        self.assertEqual({m["tool_call_id"] for m in tool_msgs}, set(called))

    def test_global_memory_requires_bound_approval_through_agent_dispatch(self):
        cases = (
            ("unbound", False),
            ("bound", True),
        )
        for label, bind_approval in cases:
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "memory"
                ag = mk()
                if bind_approval:
                    ag.confirm = lambda _name, prepared: {
                        "allowed": True,
                        "decision": "memory_risk_once",
                        "source": "user",
                        "prepared": prepared.approve_memory_risk(),
                    }
                old = os.getcwd()
                os.chdir(tmp)
                try:
                    with mock.patch.object(memory, "ROOT", root), \
                         mock.patch.object(
                             client, "stream_chat",
                             scripted(
                                 ("", [("memory_write", {
                                     "content": "cross-project fact",
                                     "scope": "global",
                                 })]),
                                 ("done", []))):
                        list(ag.run("remember globally"))
                    rows = memory.MemoryStore(root).list(cwd=tmp)
                finally:
                    os.chdir(old)

                tool_result = next(
                    message["content"] for message in ag.messages
                    if message.get("role") == "tool")
                if bind_approval:
                    self.assertTrue(
                        tool_result.startswith("[memory saved m-"))
                    self.assertEqual(
                        [row["scope"] for row in rows], ["global"])
                else:
                    self.assertTrue(
                        tool_result.startswith("[已拒绝]"))
                    self.assertIn(
                        "最终参数匹配", tool_result)
                    self.assertEqual(rows, [])

    def test_memory_write_failure_reaches_the_next_model_request(self):
        ag = mk()
        observed = []

        def provider(model, messages, tools=None, **kwargs):
            tool_messages = [
                message for message in messages
                if message.get("role") == "tool"
            ]
            if not tool_messages:
                yield {"t": "tool", "v": [{
                    "id": "memory-failure-call",
                    "type": "function",
                    "function": {
                        "name": "memory_write",
                        "arguments": json.dumps({
                            "content": "x" * (
                                memory.MAX_ENTRY_CHARS + 1),
                        }),
                    },
                }]}
            else:
                observed.append(tool_messages[-1]["content"])
                yield {"t": "text", "v": "已看到 memory 写入失败"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(memory, "ROOT", Path(tmp) / "memory"), \
                mock.patch.object(client, "stream_chat", provider), \
                mock.patch.object(ag, "log_usage"):
            events = list(ag.run("remember this"))

        self.assertEqual(len(observed), 1)
        self.assertIn("[执行失败: MemoryError:", observed[0])
        self.assertIn("最多 12,000 字符", observed[0])
        self.assertIn("已看到 memory 写入失败", [
            event.get("v") for event in events if event["t"] == "text"])

    def test_parallel_calls_all_execute(self):
        ag = mk()
        with mock.patch.object(client, "stream_chat",
                               scripted(("", [("list_dir", {"path": "."}),
                                              ("glob", {"pattern": "*.py"})]),
                                        ("ok", []))), \
             mock.patch.object(tools, "run", return_value="OUT") as run, \
             mock.patch.object(ag, "log_usage"):
            list(ag.run("go"))
        self.assertEqual(run.call_count, 2)

    def test_chat_only_model_offers_no_tools(self):
        ag = mk("chat-only")
        ag.supports_tools = False
        seen = {}

        def fake(_model, _messages, tools=None, **_kwargs):
            seen["tools"] = tools
            yield {"t": "text", "v": "纯文本回答"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        with mock.patch.object(client, "stream_chat", fake), \
                mock.patch.object(ag, "log_usage"):
            events = list(ag.run("只聊天"))

        self.assertEqual(seen["tools"], [])
        self.assertEqual(ag._offered_tools(tools.READ_ONLY), [])
        self.assertIn("纯文本回答", [
            event.get("v") for event in events if event["t"] == "text"])

    def test_chat_only_model_blocks_fabricated_tool_call(self):
        ag = mk("chat-only")
        ag.supports_tools = False
        with mock.patch.object(
                client, "stream_chat",
                scripted(("", [("list_dir", {"path": "."})]),
                         ("纯文本恢复", []))), \
                mock.patch.object(tools, "prepare") as prepare, \
                mock.patch.object(tools, "run") as run:
            list(ag.run("只聊天"))

        prepare.assert_not_called()
        run.assert_not_called()
        tool_message = next(
            item for item in ag.messages if item["role"] == "tool")
        self.assertIn("仅聊天模型", tool_message["content"])

    def test_invalid_json_arguments_do_not_abort_turn(self):
        ag = mk()

        def bad(model, messages, tools=None, **kw):
            if len([m for m in messages if m["role"] == "assistant"]) == 0:
                yield {"t": "tool", "v": [{"id": "c1", "type": "function",
                                           "function": {"name": "list_dir",
                                                        "arguments": "{not json"}}]}
            else:
                yield {"t": "text", "v": "recovered"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        with mock.patch.object(client, "stream_chat", bad), \
             mock.patch.object(ag, "log_usage"):
            texts = "".join(e["v"] for e in ag.run("go") if e["t"] == "text")
        self.assertEqual(texts, "recovered")
        self.assertTrue(any("不是合法 JSON" in str(m.get("content", ""))
                            for m in ag.messages if m["role"] == "tool"))

    def test_denied_tool_tells_model_not_to_retry(self):
        ag = mk()
        ag.confirm = lambda *a, **k: False
        with mock.patch.object(client, "stream_chat",
                               scripted(("", [("bash", {"command": "x"})]),
                                        ("ok", []))), \
             mock.patch.object(ag, "log_usage"):
            list(ag.run("go"))
        msg = [m for m in ag.messages if m["role"] == "tool"][0]["content"]
        self.assertIn("拒绝", msg)
        self.assertIn("不要重试", msg)

    def test_real_metrics_links_and_redacts_tool_lifecycle(self):
        calls = [
            {
                "id": "safe-call",
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "arguments": json.dumps({"path": "secret-content"}),
                },
            },
            {
                "id": "denied-call",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps(
                        {"command": "printf secret-content"}),
                },
            },
            {
                "id": "invalid-call",
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "arguments": "{not json",
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ag = mk()
            ag.metrics = state.MetricsFacade(Path(tmp) / "metrics.sqlite3")
            ag.confirm = lambda *args, **kwargs: False
            try:
                provider = traced_scripted(
                    ag, ("", calls), ("finished", []))
                with (
                    mock.patch.object(client, "stream_chat", provider),
                    mock.patch.object(tools, "run", return_value="SAFE")
                    as run,
                    mock.patch.object(ag, "log_usage"),
                ):
                    events = list(ag.run("go"))

                rows = ag.metrics.list_recent_tool_runs(
                    session_id=ag.session_id, limit=10)
                by_call = {row["tool_call_id"]: row for row in rows}
                self.assertEqual(
                    {name: by_call[name]["status"] for name in by_call},
                    {
                        "safe-call": "completed",
                        "denied-call": "denied",
                        "invalid-call": "denied",
                    },
                )
                self.assertTrue(all(
                    row["request_id"] == "provider-attempt-0"
                    for row in rows))
                persisted = json.dumps(
                    [row["args_redacted"] for row in rows],
                    ensure_ascii=False)
                self.assertNotIn("secret-content", persisted)
                self.assertIn("path_sha256", persisted)
                self.assertIn("command_sha256", persisted)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(events[-1]["t"], "end")
                timeline = ag.metrics.trace_timeline(
                    session_id=ag.session_id)
                self.assertEqual(len(timeline["requests"]), 2)
                self.assertEqual(len(timeline["tools"]), 3)
            finally:
                ag.metrics.close()

    def test_cancelled_before_start_has_durable_tool_trace(self):
        call = {
            "id": "cancelled-call",
            "type": "function",
            "function": {
                "name": "list_dir",
                "arguments": json.dumps({"path": "secret-content"}),
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            ag = mk()
            ag.metrics = state.MetricsFacade(Path(tmp) / "metrics.sqlite3")
            cancel = threading.Event()
            try:
                provider = traced_scripted(
                    ag, ("", [call]), ("finished", []),
                    cancel_after_tools=True)
                with (
                    mock.patch.object(client, "stream_chat", provider),
                    mock.patch.object(tools, "run") as run,
                    mock.patch.object(ag, "log_usage"),
                ):
                    list(ag.run("go", cancel=cancel))

                rows = ag.metrics.list_recent_tool_runs(
                    session_id=ag.session_id, limit=10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tool_call_id"], "cancelled-call")
                self.assertEqual(rows[0]["status"], "cancelled")
                self.assertEqual(
                    rows[0]["approval_decision"], "user_interrupted")
                self.assertEqual(rows[0]["approval_source"], "tool_cancel")
                self.assertEqual(
                    rows[0]["request_id"], "provider-attempt-0")
                self.assertNotIn(
                    "secret-content",
                    json.dumps(rows[0]["args_redacted"],
                               ensure_ascii=False))
                run.assert_not_called()
            finally:
                ag.metrics.close()


    def test_background_runner_polls_until_result(self):
        release = threading.Event()

        def slow_tool(_execution_context=None, **_):
            release.wait(timeout=1.0)
            return tools.run(
                "_inner_test", {}, context=_execution_context)

        def inner_tool(**_):
            return "SLOW_OUT"

        def hook(_event, tool_name, args, _cfg, **kwargs):
            if tool_name == "_inner_test":
                self.assertEqual(kwargs["session"], "parent-session")
                kwargs["on_note"]("inner note")
            return args

        previous = tools.IMPL.get("_slow_test")
        previous_inner = tools.IMPL.get("_inner_test")
        previous_session = tools.HOOK_CTX["session"]
        previous_current = dict(tools.CURRENT)
        previous_note = tools.HOOK_CTX["note"]
        tools.IMPL["_slow_test"] = slow_tool
        tools.IMPL["_inner_test"] = inner_tool
        tools.HOOK_CTX["session"] = "parent-session"
        tools.CURRENT.update(model="parent-model", gateway="parent-gateway")
        tools.HOOK_CTX["note"] = lambda _message: self.fail(
            "background hook note escaped to the global terminal callback")
        try:
            with mock.patch("core.hooks.run_hooks", side_effect=hook):
                execution = tools.ExecutionContext.capture(
                    session="parent-session", model="parent-model",
                    gateway="parent-gateway")
                runner = tools.run_background(
                    "_slow_test", {}, poll_interval=0.005,
                    context=execution)
                first = next(runner)
                # 模拟另一个 agent/主线程在 worker 运行中改变兼容性全局；
                # worker 必须继续使用自己的 snapshot，也不能完成后写回旧值。
                tools.HOOK_CTX["session"] = "other-session"
                tools.CURRENT.update(
                    model="other-model", gateway="other-gateway")
                release.set()
                remaining = list(runner)
                observed_session = tools.HOOK_CTX["session"]
                observed_current = dict(tools.CURRENT)
        finally:
            if previous is None:
                tools.IMPL.pop("_slow_test", None)
            else:
                tools.IMPL["_slow_test"] = previous
            if previous_inner is None:
                tools.IMPL.pop("_inner_test", None)
            else:
                tools.IMPL["_inner_test"] = previous_inner
            tools.HOOK_CTX["session"] = previous_session
            tools.HOOK_CTX["note"] = previous_note
            tools.CURRENT.clear()
            tools.CURRENT.update(previous_current)

        self.assertEqual(first["t"], "poll")
        self.assertEqual(remaining[-1], {"t": "result", "v": "SLOW_OUT"})
        self.assertIn({"t": "note", "v": "inner note"}, remaining)
        self.assertEqual(observed_session, "other-session")
        self.assertEqual(observed_current, {
            "model": "other-model", "gateway": "other-gateway"})


class Interruption(unittest.TestCase):
    def test_interrupt_leaves_no_orphan_tool_calls(self):
        """本轮开发的真实 bug：Ctrl-C 后残留孤儿 tool_call_id，下轮请求被服务端拒。"""
        ag = mk()
        cancel = threading.Event()

        def fake(model, messages, tools=None, cancel=None, **kw):
            yield {"t": "tool", "v": [
                {"id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}},
                {"id": "c2", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]}
            yield {"t": "done", "reason": "stop", "usage": {}}

        def slow_run(name, args, context=None):
            self.assertEqual(context.session, ag.session_id)
            cancel.set()            # 第一个工具跑完时用户按了 Esc
            return "partial"

        with mock.patch.object(client, "stream_chat", fake), \
             mock.patch.object(tools, "run", side_effect=slow_run), \
             mock.patch.object(ag, "log_usage"):
            list(ag.run("go", cancel=cancel))
        called = [t["id"] for m in ag.messages if m.get("tool_calls")
                  for t in m["tool_calls"]]
        answered = {m.get("tool_call_id") for m in ag.messages
                    if m["role"] == "tool"}
        self.assertEqual([c for c in called if c not in answered], [],
                         "中断后不能留下没有 tool_result 的 tool_call")

    def test_stream_interrupt_yields_event_and_stops(self):
        ag = mk()

        def fake(*a, **kw):
            raise client.Interrupted("用户中断")
            yield  # pragma: no cover

        with mock.patch.object(client, "stream_chat", fake):
            evs = [e["t"] for e in ag.run("go")]
        self.assertIn("interrupted", evs)


class ManagedController(unittest.TestCase):
    def start(self, text="go"):
        journal = C.MemoryJournal()
        controller = C.SessionController("t", journal)
        action = controller.submit(text, C.QueueMode.NEXT_TURN)
        self.assertEqual(action.kind, C.ActionKind.START_TURN)
        return journal, controller

    def test_request_is_journaled_before_provider_and_user_not_duplicated(self):
        ag = mk()
        journal, controller = self.start()
        seen = {}

        def provider(*args, **kwargs):
            seen["kinds_at_start"] = [
                event["kind"] for event in journal.events]
            yield {"t": "text", "v": "ok"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        events = list(ag.run(
            "go", controller=controller, stream_factory=provider))
        kinds = [event["kind"] for event in journal.events]
        self.assertIn("request_started", seen["kinds_at_start"])
        self.assertEqual(kinds.count("user_message"), 1)
        self.assertEqual(kinds.count("turn_started"), 1)
        self.assertEqual(kinds.count("assistant_message"), 1)
        self.assertEqual(kinds.count("turn_completed"), 1)
        self.assertEqual(events[-1]["t"], "end")
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_child_trace_keeps_child_metrics_and_parent_transport_scope(self):
        ag = mk()
        ag.session_id = "child-session"
        ag.parent_session_id = "parent-session"

        trace = ag._trace_context("request-1")

        self.assertEqual(trace["session_id"], "child-session")
        self.assertEqual(trace["transport_session_id"], "parent-session")

    def test_safe_subagent_still_runs_session_transport_decision(self):
        ag = mk()
        _, controller = self.start("delegate")
        permission = mock.Mock(return_value={
            "allowed": False,
            "decision": "provider_transport_denied",
            "source": "transport_guard",
        })
        ag.permission_decision = permission

        events = list(ag.run(
            "delegate", controller=controller,
            stream_factory=scripted(
                ("", [("subagent", {"task": "inspect"})]),
                ("done", []))))

        self.assertEqual(permission.call_count, 1)
        self.assertEqual(permission.call_args.args[0], "subagent")
        denied = [event for event in events
                  if event.get("t") == "tool_end"]
        self.assertEqual(len(denied), 1)
        self.assertTrue(denied[0]["denied"])

    def test_rejected_attachment_preserves_raw_prompt_and_local_error(self):
        ag = mk()
        journal, controller = self.start("review @.env")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text("API_KEY=secret", encoding="utf-8")
            with mock.patch.object(A.os, "getcwd", return_value=tmp):
                events = list(ag.run(
                    "review @.env", controller=controller,
                    stream_factory=mock.Mock()))

        self.assertEqual([event["t"] for event in events], ["error"])
        self.assertEqual(ag.messages[-2]["content"], "review @.env")
        self.assertIn("本地拒绝附件输入", ag.messages[-1]["content"])
        kinds = [event["kind"] for event in journal.events]
        self.assertEqual(kinds.count("user_message"), 1)
        self.assertIn("assistant_message", kinds)
        self.assertIn("turn_failed", kinds)

    def test_unmanaged_rejection_is_a_complete_recoverable_turn(self):
        ag = mk()
        journal = []

        def sink(events):
            journal.extend(events)
            return events

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text("API_KEY=secret", encoding="utf-8")
            with mock.patch.object(A.os, "getcwd", return_value=tmp):
                events = list(ag.run(
                    "review @.env", event_sink=sink,
                    stream_factory=mock.Mock()))

        self.assertEqual([event["t"] for event in events], ["error"])
        self.assertEqual(ag.messages[-2]["content"], "review @.env")
        self.assertIn("本地拒绝附件输入", ag.messages[-1]["content"])
        self.assertEqual(
            [event["kind"] for event in journal],
            ["user_message", "turn_started", "assistant_message",
             "turn_failed"])

    def test_write_uses_final_hook_args_and_checkpoint_before_tool_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.txt"
            backup_dir = root / "backups"
            target.write_text("before\n", encoding="utf-8")
            authority = checkpoints.CheckpointStore(root / "state")
            ag = mk()
            journal, controller = self.start()
            hook_events = []
            previews = []

            def hook(event, _name, args, _cfg, **_kwargs):
                hook_events.append(event)
                updated = dict(args)
                if event == "PreToolUse":
                    updated["content"] = "after from hook\n"
                return updated

            def permission(_name, prepared):
                previews.append(prepared.prepared_write.preview_text)
                return {
                    "allowed": True, "decision": "once", "source": "user"}

            def capture(prepared, *, turn_id, tool_call_id, message_count):
                item = authority.capture_prepared(
                    ag.session_id, turn_id, prepared.prepared_write,
                    source_tool_run_id=tool_call_id,
                    conversation_message_count=message_count)
                return prepared.bind_checkpoint(authority, item), item.event_payload()

            ag.permission_decision = permission
            ag.checkpoint_prepared = capture
            with mock.patch.object(
                    tools.os, "getcwd", return_value=str(root)), \
                    mock.patch("core.hooks.run_hooks", side_effect=hook), \
                    mock.patch("core.store.BACKUPS", backup_dir), \
                    mock.patch.object(ag, "log_usage"):
                list(ag.run(
                    "go", controller=controller,
                    stream_factory=scripted(
                        ("", [("write_file", {
                            "path": str(target), "content": "model draft\n"})]),
                        ("done", []))))

            kinds = [event["kind"] for event in journal.events]
            self.assertEqual(target.read_text(encoding="utf-8"),
                             "after from hook\n")
            backups = list(backup_dir.glob("*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"before\n")
            self.assertEqual(hook_events.count("PreToolUse"), 1)
            self.assertEqual(hook_events.count("PostToolUse"), 1)
            self.assertIn("after from hook", previews[0])
            self.assertNotIn("model draft", previews[0])
            self.assertLess(kinds.index("permission_decided"),
                            kinds.index("checkpoint_created"))
            self.assertLess(kinds.index("checkpoint_created"),
                            kinds.index("tool_started"))
            self.assertLess(kinds.index("tool_started"),
                            kinds.index("tool_finished"))

    def test_external_change_after_preview_aborts_before_tool_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.txt"
            target.write_text("before\n", encoding="utf-8")
            authority = checkpoints.CheckpointStore(root / "state")
            ag = mk()
            journal, controller = self.start()
            ag.permission_decision = lambda *_args: {
                "allowed": True, "decision": "once", "source": "user"}

            def conflict(prepared, *, turn_id, tool_call_id, message_count):
                target.write_text("external\n", encoding="utf-8")
                item = authority.capture_prepared(
                    ag.session_id, turn_id, prepared.prepared_write,
                    source_tool_run_id=tool_call_id,
                    conversation_message_count=message_count)
                return prepared.bind_checkpoint(authority, item), item.event_payload()

            ag.checkpoint_prepared = conflict
            with mock.patch.object(
                    tools.os, "getcwd", return_value=str(root)), \
                    mock.patch.object(ag, "log_usage"):
                list(ag.run(
                    "go", controller=controller,
                    stream_factory=scripted(
                        ("", [("write_file", {
                            "path": str(target), "content": "model\n"})]),
                        ("done", []))))

            kinds = [event["kind"] for event in journal.events]
            self.assertEqual(target.read_text(encoding="utf-8"), "external\n")
            self.assertIn("permission_decided", kinds)
            self.assertNotIn("checkpoint_created", kinds)
            self.assertNotIn("tool_started", kinds)
            failed = [
                event for event in journal.events
                if event["kind"] == "tool_finished"][-1]
            self.assertEqual(failed["payload"]["status"], "checkpoint_failed")

    def test_plan_control_does_not_rewrite_managed_raw_user(self):
        ag = mk()
        journal, controller = self.start("go")
        seen = {}

        def provider(model, messages, **kwargs):
            seen["messages"] = json.loads(json.dumps(messages))
            yield {"t": "text", "v": "plan"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        list(ag.run("go", controller=controller, stream_factory=provider,
                    plan_mode=True, allowed_tools=tools.READ_ONLY))
        self.assertEqual([m["content"] for m in ag.messages
                          if m["role"] == "user"], ["go"])
        journal_users = [event["payload"]["message"]["content"]
                         for event in journal.events
                         if event["kind"] == "user_message"]
        self.assertEqual(journal_users, ["go"])
        self.assertEqual([m["content"] for m in seen["messages"]
                          if m["role"] == "user"], ["go"])
        self.assertTrue(any(m["role"] == "system"
                            and A.PLAN_PREAMBLE in m["content"]
                            for m in seen["messages"]))

    def test_managed_chat_only_model_blocks_fabricated_tool_call(self):
        ag = mk("chat-only")
        ag.supports_tools = False
        journal, controller = self.start("只聊天")
        with mock.patch.object(tools, "prepare") as prepare:
            list(ag.run(
                "只聊天", controller=controller,
                stream_factory=scripted(
                    ("", [("list_dir", {"path": "."})]),
                    ("纯文本恢复", []))))

        prepare.assert_not_called()
        decisions = [
            event["payload"] for event in journal.events
            if event["kind"] == "permission_decided"
        ]
        self.assertTrue(any(
            item.get("decision") == "chat_only_denied"
            and item.get("source") == "model_capability"
            for item in decisions))
        tool_message = next(
            item for item in ag.messages if item["role"] == "tool")
        self.assertIn("仅聊天模型", tool_message["content"])

    def test_managed_compaction_polls_and_can_be_cancelled(self):
        ag = mk()
        for index in range(8):
            ag.messages.extend([
                {"role": "user", "content": f"old-{index}"},
                {"role": "assistant", "content": f"answer-{index}"},
            ])
        ag.last_total = 80_000
        journal, controller = self.start("go")
        calls = {"n": 0}

        def provider(model, messages, cancel=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                yield {"t": "poll"}
                if cancel.is_set():
                    raise client.Interrupted("cancelled")
                yield {"t": "text", "v": "partial summary"}
            else:
                self.assertEqual(
                    [m["content"] for m in messages if m["role"] == "user"][-2:],
                    ["go", "adjust"])
                yield {"t": "text", "v": "done"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        events = []
        cancelled = False
        for event in ag.run(
                "go", controller=controller, stream_factory=provider):
            events.append(event)
            if (event["t"] == "poll"
                    and event.get("phase") == "compaction"
                    and not cancelled):
                cancelled = True
                controller.submit("adjust", C.QueueMode.STEER)
                controller.request_cancel()

        starts = [
            event for event in journal.events
            if event["kind"] == "request_started"
        ]
        self.assertEqual(calls["n"], 2)
        self.assertEqual(
            starts[0]["payload"]["context"]["purpose"],
            "context_compaction")
        self.assertIn("request_interrupted",
                      [event["kind"] for event in journal.events])
        self.assertEqual(events[-1]["t"], "end")
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_managed_compaction_completes_before_main_request(self):
        ag = mk()
        for index in range(8):
            ag.messages.extend([
                {"role": "user", "content": f"old-{index}"},
                {"role": "assistant", "content": f"answer-{index}"},
            ])
        ag.last_total = 80_000
        journal, controller = self.start("go")
        calls = {"n": 0}

        def provider(model, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                yield {"t": "text", "v": "## objective\ncontinue"}
            else:
                yield {"t": "text", "v": "done"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        events = list(ag.run(
            "go", controller=controller, stream_factory=provider))

        kinds = [event["kind"] for event in journal.events]
        first_start = kinds.index("request_started")
        compact_done = kinds.index("request_completed")
        compact_event = kinds.index("context_compacted")
        second_start = kinds.index("request_started", first_start + 1)
        self.assertLess(first_start, compact_done)
        self.assertLess(compact_done, compact_event)
        self.assertLess(compact_event, second_start)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(ag.context_summary["status"], "valid")
        self.assertEqual(events[-1]["t"], "end")
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_steer_submitted_on_poll_is_delivered_same_turn(self):
        ag = mk()
        journal, controller = self.start()
        calls = {"n": 0}

        def provider(model, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                yield {"t": "poll"}
                yield {"t": "text", "v": "first"}
            else:
                self.assertEqual(messages[-1]["content"], "adjust")
                yield {"t": "text", "v": "second"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        events = []
        submitted = False
        for event in ag.run(
                "go", controller=controller, stream_factory=provider):
            events.append(event)
            if event["t"] == "poll" and not submitted:
                submitted = True
                controller.submit("adjust", C.QueueMode.STEER)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(
            [message["content"] for message in ag.messages
             if message["role"] == "user"],
            ["go", "adjust"])
        self.assertEqual(
            [event["kind"] for event in journal.events].count(
                "turn_completed"), 1)
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_cancel_intent_precedes_worker_ack(self):
        ag = mk()
        journal, controller = self.start()

        def provider(*args, cancel=None, **kwargs):
            yield {"t": "poll"}
            if cancel.is_set():
                raise client.Interrupted("cancelled")
            yield {"t": "done", "reason": "stop", "usage": {}}

        events = []
        for event in ag.run(
                "go", controller=controller, stream_factory=provider):
            events.append(event)
            if event["t"] == "poll":
                controller.request_cancel()

        kinds = [event["kind"] for event in journal.events]
        self.assertLess(
            kinds.index("request_cancel_requested"),
            kinds.index("request_interrupted"))
        self.assertEqual(events[-1]["t"], "interrupted")
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_cancel_after_worker_done_before_commit_is_acknowledged(self):
        ag = mk()
        journal, controller = self.start()

        def provider(*args, **kwargs):
            yield {"t": "text", "v": "complete"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        events = []
        for event in ag.run(
                "go", controller=controller, stream_factory=provider):
            events.append(event)
            if event["t"] == "usage":
                controller.request_cancel()

        kinds = [event["kind"] for event in journal.events]
        self.assertIn("request_cancel_requested", kinds)
        self.assertIn("request_interrupted", kinds)
        self.assertNotIn("assistant_message", kinds)
        self.assertEqual(events[-1]["t"], "interrupted")
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_tool_poll_accepts_steer_and_preserves_same_turn(self):
        ag = mk()
        journal, controller = self.start()
        provider = scripted(
            ("", [("list_dir", {"path": "."})]),
            ("done", []))

        def tool_runner(name, args, context=None):
            self.assertEqual(name, "list_dir")
            self.assertEqual(context.session, ag.session_id)
            self.assertEqual(context.model, ag.model)
            self.assertEqual(context.gateway, ag.gateway)
            yield {"t": "poll"}
            yield {"t": "note", "v": "still running"}
            yield {"t": "result", "v": "TOOL_OUT"}

        events = []
        submitted = False
        with mock.patch.object(ag, "log_usage"):
            for event in ag.run(
                    "go", controller=controller,
                    stream_factory=provider, tool_runner=tool_runner):
                events.append(event)
                if (event["t"] == "poll"
                        and event.get("source") == "tool"
                        and not submitted):
                    submitted = True
                    controller.submit("adjust", C.QueueMode.STEER)

        event_types = [event["t"] for event in events]
        self.assertLess(
            event_types.index("tool_start"), event_types.index("poll"))
        self.assertLess(
            event_types.index("poll"), event_types.index("tool_end"))
        self.assertIn("tool_note", event_types)
        self.assertEqual(
            [message["content"] for message in ag.messages
             if message["role"] == "user"],
            ["go", "adjust"])
        self.assertEqual(controller.state, C.ControllerState.IDLE)
        self.assertEqual(
            [event["kind"] for event in journal.events].count(
                "tool_finished"), 1)

    def test_workspace_change_yields_before_followup_provider_request(self):
        ag = mk()
        _journal, controller = self.start()
        state = {"provider_calls": 0, "refreshed": False}

        def provider(_model, _messages, **_kwargs):
            state["provider_calls"] += 1
            if state["provider_calls"] == 1:
                yield {"t": "tool", "v": [{
                    "id": "edit-1", "type": "function",
                    "function": {
                        "name": "list_dir",
                        "arguments": json.dumps({"path": "."}),
                    },
                }]}
            else:
                self.assertTrue(
                    state["refreshed"],
                    "main thread must refresh before provider follow-up")
                yield {"t": "text", "v": "done"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        def tool_runner(_name, _args, context=None):
            yield {"t": "runtime", "event": {
                "kind": "workspace_changed",
                "payload": {"tool": "bash"},
            }}
            yield {"t": "result", "v": "[ok]"}

        events = []
        for event in ag.run(
                "go", controller=controller,
                stream_factory=provider, tool_runner=tool_runner):
            events.append(event["t"])
            if event["t"] == "workspace_changed":
                state["refreshed"] = True

        self.assertEqual(state["provider_calls"], 2)
        self.assertLess(
            events.index("workspace_changed"),
            events.index("tool_end"))

    def test_graft_runtime_state_yields_before_tool_end(self):
        ag = mk()
        _journal, controller = self.start()

        def provider(_model, _messages, **_kwargs):
            yield {"t": "tool", "v": [{
                "id": "graft-1", "type": "function",
                "function": {
                    "name": "list_dir",
                    "arguments": json.dumps({"path": "."}),
                },
            }]}
            yield {"t": "done", "reason": "stop", "usage": {}}

        def tool_runner(_name, _args, context=None):
            yield {"t": "runtime", "event": {
                "kind": "graft_status",
                "payload": {
                    "state": "ready", "action": "find_code",
                    "root": "/tmp/repo", "files": 3,
                },
            }}
            yield {"t": "result", "v": "[graft ok]"}

        events = list(ag.run(
            "go", controller=controller,
            stream_factory=provider, tool_runner=tool_runner))
        event_types = [event["t"] for event in events]
        graft_event = next(
            event for event in events if event["t"] == "graft_event")
        self.assertEqual(graft_event["payload"]["files"], 3)
        self.assertLess(
            event_types.index("graft_event"), event_types.index("tool_end"))

    def test_backgrounded_tool_closes_protocol_and_provider_continues(self):
        ag = mk()
        journal, controller = self.start("run it")
        ack = (
            "[task task-bg01 已转后台；使用 /tasks 查看，"
            "/task task-bg01 跟随，/kill task-bg01 终止]")
        provider_calls = []

        def provider(model, messages, **kwargs):
            del model, kwargs
            provider_calls.append(json.loads(json.dumps(messages)))
            if len(provider_calls) == 1:
                yield {"t": "tool", "v": [{
                    "id": "call-bg01", "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"command": "sleep 30"}),
                    },
                }]}
            else:
                self.assertEqual(messages[-1], {
                    "role": "tool",
                    "tool_call_id": "call-bg01",
                    "content": ack,
                })
                yield {"t": "text", "v": "continued"}
            yield {
                "t": "done", "reason": "stop",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }

        def tool_runner(name, args, context=None, task_key=None):
            del args, context
            task = {
                "id": "task-bg01", "key": task_key,
                "name": name, "status": "running",
                "background": True,
            }
            yield {
                "t": "started", "task_id": "task-bg01",
                "task": {**task, "status": "queued",
                         "background": False},
            }
            yield {
                "t": "backgrounded", "task_id": "task-bg01",
                "v": ack, "task": task,
            }

        with mock.patch.object(ag, "log_usage"):
            events = list(ag.run(
                "run it", controller=controller,
                stream_factory=provider, tool_runner=tool_runner))

        event_types = [event["t"] for event in events]
        self.assertLess(
            event_types.index("tool_task_started"),
            event_types.index("tool_task_backgrounded"))
        self.assertLess(
            event_types.index("tool_task_backgrounded"),
            event_types.index("tool_end"))
        self.assertNotIn("tool_task_end", event_types)
        background_event = next(
            event for event in events
            if event["t"] == "tool_task_backgrounded")
        self.assertEqual(background_event["status"], "backgrounded")
        self.assertEqual(background_event["task_id"], "task-bg01")
        self.assertEqual(background_event["task"]["background"], True)

        assistant_calls = [
            call for message in ag.messages
            for call in message.get("tool_calls", [])]
        tool_results = [
            message for message in ag.messages
            if message.get("role") == "tool"]
        self.assertEqual(len(provider_calls), 2)
        self.assertEqual(len(assistant_calls), 1)
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(
            tool_results[0]["tool_call_id"], assistant_calls[0]["id"])
        self.assertEqual(tool_results[0]["content"], ack)

        finished = [
            event for event in journal.events
            if event["kind"] == "tool_finished"]
        kinds = [event["kind"] for event in journal.events]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["payload"]["status"], "backgrounded")
        self.assertEqual(finished[0]["payload"]["message"]["content"], ack)
        self.assertLess(
            kinds.index("tool_finished"),
            kinds.index("request_started", kinds.index("tool_finished")))
        self.assertNotIn("tool_cancel_requested", kinds)
        self.assertEqual(events[-1]["t"], "end")
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_tool_runner_failure_before_first_event_closes_protocol(self):
        ag = mk()
        journal, controller = self.start("run it")
        provider = scripted(
            ("", [("bash", {"command": "ignored"})]),
            ("provider continued", []))

        def broken_runner(name, args, context=None, task_key=None):
            del name, args, context, task_key
            raise RuntimeError("runner create boom")

        events = list(ag.run(
            "run it", controller=controller,
            stream_factory=provider, tool_runner=broken_runner))

        event_types = [event["t"] for event in events]
        tool_results = [
            message for message in ag.messages
            if message.get("role") == "tool"]
        finished = [
            event for event in journal.events
            if event["kind"] == "tool_finished"]
        self.assertIn("tool_end", event_types)
        self.assertNotIn("tool_task_started", event_types)
        self.assertEqual(len(tool_results), 1)
        self.assertIn("RuntimeError: runner create boom",
                      tool_results[0]["content"])
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["payload"]["status"], "failed")
        self.assertIn("provider continued", "".join(
            event.get("v", "") for event in events
            if event["t"] == "text"))
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_tool_runner_failure_after_started_closes_task_and_protocol(self):
        ag = mk()
        journal, controller = self.start("run it")
        provider = scripted(
            ("", [("bash", {"command": "ignored"})]),
            ("provider continued", []))

        def broken_runner(name, args, context=None, task_key=None):
            del args, context
            yield {
                "t": "started", "task_id": "task-broken",
                "task": {
                    "id": "task-broken", "key": task_key,
                    "name": name, "status": "queued",
                },
            }
            raise RuntimeError("runner iterate boom")

        events = list(ag.run(
            "run it", controller=controller,
            stream_factory=provider, tool_runner=broken_runner))

        event_types = [event["t"] for event in events]
        task_end = next(
            event for event in events
            if event["t"] == "tool_task_end")
        tool_results = [
            message for message in ag.messages
            if message.get("role") == "tool"]
        finished = [
            event for event in journal.events
            if event["kind"] == "tool_finished"]
        self.assertLess(event_types.index("tool_task_started"),
                        event_types.index("tool_task_end"))
        self.assertLess(event_types.index("tool_task_end"),
                        event_types.index("tool_end"))
        self.assertEqual(task_end["status"], "failed")
        self.assertEqual(task_end["task"]["status"], "failed")
        self.assertIn("runner iterate boom", task_end["task"]["error"])
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["payload"]["status"], "failed")
        self.assertIn("provider continued", "".join(
            event.get("v", "") for event in events
            if event["t"] == "text"))
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_tool_cancel_closes_batch_without_orphan_calls(self):
        ag = mk()
        journal, controller = self.start("go")
        provider = scripted(("", [
            ("list_dir", {"path": "."}),
            ("glob", {"pattern": "*.py"}),
        ]))

        def tool_runner(name, args, context=None, task_key=None):
            del args, context
            yield {
                "t": "started",
                "task_id": "task-cancel",
                "task": {
                    "id": "task-cancel",
                    "key": task_key,
                    "name": name,
                    "status": "queued",
                },
            }
            yield {
                "t": "progress",
                "task_id": "task-cancel",
                "stdout_bytes": 0,
                "stderr_bytes": 0,
            }
            yield {
                "t": "cancelled",
                "task_id": "task-cancel",
                "v": "[用户中断]",
                "task": {
                    "id": "task-cancel",
                    "key": task_key,
                    "name": name,
                    "status": "cancelled",
                },
            }

        events = []
        requested = False
        with mock.patch.object(ag, "log_usage"):
            for event in ag.run(
                    "go", controller=controller,
                    stream_factory=provider, tool_runner=tool_runner):
                events.append(event)
                if (event["t"] == "poll"
                        and event.get("source") == "tool"
                        and not requested):
                    requested = True
                    controller.request_cancel()

        calls = [
            call["id"] for message in ag.messages
            for call in message.get("tool_calls", [])
        ]
        results = {
            message.get("tool_call_id")
            for message in ag.messages if message["role"] == "tool"
        }
        kinds = [event["kind"] for event in journal.events]
        self.assertEqual(set(calls), results)
        self.assertLess(
            kinds.index("tool_cancel_requested"),
            kinds.index("tool_finished"))
        self.assertEqual(kinds.count("tool_finished"), 2)
        self.assertIn("task_started", kinds)
        self.assertEqual(events[-1]["t"], "interrupted")
        self.assertEqual(
            controller.state, C.ControllerState.IDLE)

    def test_cancel_after_task_completed_before_commit_is_honest(self):
        ag = mk()
        journal, controller = self.start("go")
        provider = scripted(("", [("list_dir", {"path": "."})]))

        def tool_runner(name, args, context=None, task_key=None):
            del args, context
            task = {
                "id": "task-late", "key": task_key,
                "name": name, "status": "completed",
            }
            yield {
                "t": "started", "task_id": "task-late",
                "task": {**task, "status": "queued"},
            }
            yield {
                "t": "completed", "task_id": "task-late",
                "v": "SIDE_EFFECT_DONE", "task": task,
            }

        events = []
        with mock.patch.object(ag, "log_usage"):
            for event in ag.run(
                    "go", controller=controller,
                    stream_factory=provider, tool_runner=tool_runner):
                events.append(event)
                if event["t"] == "tool_task_end":
                    controller.request_cancel()

        tool_result = next(
            message["content"] for message in ag.messages
            if message["role"] == "tool")
        finished = next(
            event for event in journal.events
            if event["kind"] == "tool_finished")
        self.assertIn("副作用可能已经发生", tool_result)
        self.assertEqual(
            finished["payload"]["status"],
            "completed_after_cancel")
        self.assertEqual(events[-1]["t"], "interrupted")

    def test_child_agent_lifecycle_is_committed_only_on_main_loop(self):
        ag = mk()
        journal, controller = self.start("delegate")
        provider = scripted(
            ("", [("subagent", {"task": "inspect"})]),
            ("done", []))

        def tool_runner(name, args, context=None, task_key=None):
            del args, context
            task = {
                "id": "task-agent", "key": task_key,
                "name": name, "status": "completed",
            }
            base = {
                "id": "a-child01",
                "parent_session_id": ag.session_id,
                "parent_turn_id": controller.current_turn_id,
                "parent_tool_call_id": task_key,
                "transcript_session_id": "c-child01",
                "name": "inspect", "kind": "subagent",
                "task": "inspect", "model": ag.model,
                "gateway": ag.gateway,
                "created_at": "2026-08-25T00:00:00.000+00:00",
                "updated_at": "2026-08-25T00:00:00.000+00:00",
            }
            yield {
                "t": "started", "task_id": "task-agent",
                "task": {**task, "status": "queued"},
            }
            for kind, payload in (
                    ("agent_spawned", {**base, "state": "queued"}),
                    ("agent_state_changed", {
                        **base, "state": "running"}),
                    ("agent_state_changed", {
                        **base, "state": "completed"}),
                    ("agent_result_received", {
                        **base, "state": "completed", "result": "REPORT"})):
                yield {
                    "t": "runtime", "task_id": "task-agent",
                    "event": {"kind": kind, "payload": payload},
                }
            yield {
                "t": "completed", "task_id": "task-agent",
                "v": "[agent a-child01 · completed]\nREPORT",
                "task": task,
            }

        with mock.patch.object(ag, "log_usage"):
            events = list(ag.run(
                "delegate", controller=controller,
                stream_factory=provider, tool_runner=tool_runner))

        kinds = [event["kind"] for event in journal.events]
        lifecycle = [
            event for event in events if event["t"] == "agent_event"]
        self.assertEqual(
            [event["kind"] for event in lifecycle],
            ["agent_spawned", "agent_state_changed",
             "agent_state_changed", "agent_result_received"])
        self.assertLess(kinds.index("task_started"),
                        kinds.index("agent_spawned"))
        self.assertLess(kinds.index("agent_result_received"),
                        kinds.index("tool_finished"))
        self.assertEqual(
            next(message["content"] for message in ag.messages
                 if message.get("role") == "tool"),
            "[agent a-child01 · completed]\nREPORT")

    def test_real_task_manager_cancels_bash_and_closes_protocol(self):
        ag = mk()
        # This test owns TaskManager/controller cancellation semantics, not
        # the sandbox adapter.  It must also run when the suite itself is
        # already inside zylab's mount namespace sandbox.
        ag.hook_cfg = {
            "sandbox": {
                "mode": "disabled",
                "network_isolation": False,
            },
        }
        journal, controller = self.start("go")
        provider = scripted(("", [("bash", {
            "command": (
                "exec bash -c 'trap \"\" INT TERM; "
                "echo READY; sleep 30 & wait'"),
            "timeout": 10,
        })]))

        with tempfile.TemporaryDirectory() as tmp:
            manager = task_runtime.TaskManager(
                Path(tmp) / "artifacts", tools.run,
                session_getter=lambda: ag.session_id,
                poll_interval=0.005,
                interrupt_grace=0.04,
                terminate_grace=0.04)
            events = []
            cancelled = False
            with mock.patch.object(ag, "log_usage"):
                for event in ag.run(
                        "go", controller=controller,
                        stream_factory=provider,
                        tool_runner=manager.run):
                    events.append(event)
                    if (event["t"] == "tool_output"
                            and "READY" in event.get("v", "")
                            and not cancelled):
                        cancelled = True
                        action = controller.request_cancel()
                        manager.cancel(
                            action.payload["tool_call_id"])
            snapshot = manager.get("c0_0")

        call_ids = [
            call["id"] for message in ag.messages
            for call in message.get("tool_calls", [])]
        result_ids = [
            message["tool_call_id"] for message in ag.messages
            if message["role"] == "tool"]
        kinds = [event["kind"] for event in journal.events]
        self.assertTrue(cancelled)
        self.assertEqual(call_ids, result_ids)
        self.assertEqual(snapshot.status.value, "cancelled")
        self.assertFalse(snapshot.timed_out)
        self.assertIsNotNone(snapshot.cancel_requested_at)
        self.assertIsNotNone(snapshot.ended_at)
        self.assertEqual(snapshot.signal, 9)
        self.assertLess(
            kinds.index("tool_cancel_requested"),
            kinds.index("tool_finished"))
        self.assertEqual(
            controller.state, C.ControllerState.IDLE)

    def test_worker_start_failure_releases_manager_and_agent_protocol(self):
        ag = mk()
        journal, controller = self.start("go")
        provider = scripted(
            ("", [("bash", {"command": "echo MUST_NOT_RUN"})]),
            ("provider continued", []))

        with tempfile.TemporaryDirectory() as tmp:
            manager = task_runtime.TaskManager(
                Path(tmp) / "artifacts", tools.run,
                session_getter=lambda: ag.session_id,
                poll_interval=0.005)
            with mock.patch.object(
                    threading.Thread, "start",
                    side_effect=RuntimeError("thread start failed")), \
                 mock.patch.object(ag, "log_usage"):
                events = list(ag.run(
                    "go", controller=controller,
                    stream_factory=provider,
                    tool_runner=manager.run))
            snapshot = manager.get("c0_0")

        event_types = [event["t"] for event in events]
        tool_results = [
            message for message in ag.messages
            if message.get("role") == "tool"]
        finished = [
            event for event in journal.events
            if event["kind"] == "tool_finished"]
        self.assertLess(event_types.index("tool_task_started"),
                        event_types.index("tool_task_end"))
        self.assertEqual(snapshot.status, task_runtime.TaskStatus.FAILED)
        self.assertIsNone(manager.foreground())
        self.assertEqual(len(tool_results), 1)
        self.assertIn("thread start failed", tool_results[0]["content"])
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["payload"]["status"], "failed")
        self.assertIn("provider continued", "".join(
            event.get("v", "") for event in events
            if event["t"] == "text"))
        self.assertEqual(controller.state, C.ControllerState.IDLE)


    def test_permission_decision_precedes_tool_execution(self):
        ag = mk()
        journal, controller = self.start()
        observed = {}
        calls = [
            {
                "id": "managed-safe",
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "arguments": json.dumps({"path": "secret-content"}),
                },
            },
            {
                "id": "managed-denied",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps(
                        {"command": "printf secret-content"}),
                },
            },
        ]

        def tool_run(name, args, context=None):
            self.assertEqual(context.gateway, ag.gateway)
            observed["kinds"] = [
                event["kind"] for event in journal.events]
            return "OUT"

        with tempfile.TemporaryDirectory() as tmp:
            ag.metrics = state.MetricsFacade(
                Path(tmp) / "metrics.sqlite3")
            ag.confirm = lambda *args, **kwargs: False
            provider = traced_scripted(
                ag, ("", calls), ("done", []))
            try:
                with (
                    mock.patch.object(
                        tools, "run", side_effect=tool_run) as run,
                    mock.patch.object(ag, "log_usage"),
                ):
                    list(ag.run(
                        "go", controller=controller,
                        stream_factory=provider))
                rows = ag.metrics.list_recent_tool_runs(
                    session_id=ag.session_id, limit=10)
                by_call = {row["tool_call_id"]: row for row in rows}
                self.assertEqual(
                    by_call["managed-safe"]["status"], "completed")
                self.assertEqual(
                    by_call["managed-denied"]["status"], "denied")
                self.assertTrue(all(
                    row["request_id"] == "provider-attempt-0"
                    for row in rows))
                persisted = json.dumps(
                    [row["args_redacted"] for row in rows],
                    ensure_ascii=False)
                self.assertNotIn("secret-content", persisted)
                self.assertIn("path_sha256", persisted)
                self.assertIn("command_sha256", persisted)
                self.assertEqual(run.call_count, 1)
            finally:
                ag.metrics.close()

        kinds = observed["kinds"]
        self.assertLess(
            kinds.index("permission_decided"),
            kinds.index("tool_started"))
        self.assertIn("tool_started", kinds)
        all_kinds = [event["kind"] for event in journal.events]
        self.assertEqual(all_kinds.count("tool_finished"), 2)
        self.assertEqual(controller.state, C.ControllerState.IDLE)


class Limits(unittest.TestCase):
    def test_max_turns_terminates_with_reason(self):
        ag = mk()
        endless = scripted(*[("", [("list_dir", {"path": "."})])] * 10)
        with mock.patch.object(client, "stream_chat", endless), \
             mock.patch.object(tools, "run", return_value="o"), \
             mock.patch.object(ag, "log_usage"):
            evs = list(ag.run("go", max_turns=3))
        end = [e for e in evs if e["t"] == "end"][-1]
        self.assertIn("最大轮数", end["reason"])
        self.assertEqual(end["kind"], "max_turns")
        self.assertEqual(end["status"], "partial")
        self.assertIn("尚未生成最终总结", end["message"])
        self.assertEqual(ag.messages[-1]["role"], "assistant")
        self.assertIn("最大轮数", ag.messages[-1]["content"])

    def test_managed_max_turns_persists_notice_and_failure(self):
        ag = mk()
        journal = C.MemoryJournal()
        controller = C.SessionController("t", journal)
        action = controller.submit("go", C.QueueMode.NEXT_TURN)
        self.assertEqual(action.kind, C.ActionKind.START_TURN)
        endless = scripted(
            ("", [("list_dir", {"path": "."})]),
            ("", [("list_dir", {"path": "."})]),
        )
        with mock.patch.object(tools, "run", return_value="out"):
            evs = list(ag.run(
                "go", max_turns=2, controller=controller,
                stream_factory=endless))

        end = evs[-1]
        self.assertEqual(end["kind"], "max_turns")
        self.assertEqual(end["status"], "partial")
        self.assertIn("尚未生成最终总结", end["message"])
        kinds = [event["kind"] for event in journal.events]
        self.assertEqual(kinds[-2:], ["assistant_message", "turn_failed"])
        notice_event = journal.events[-2]["payload"]
        self.assertTrue(notice_event["synthetic"])
        self.assertEqual(notice_event["notice_kind"], "max_turns")
        self.assertIn("最大轮数", notice_event["message"]["content"])
        self.assertEqual(controller.state, C.ControllerState.IDLE)

    def test_empty_provider_response_is_visible_partial_failure(self):
        ag = mk()

        def empty(*_args, **_kwargs):
            yield {"t": "done", "reason": "stop", "usage": {}}

        with mock.patch.object(client, "stream_chat", empty):
            evs = list(ag.run("go"))

        end = evs[-1]
        self.assertEqual(end["kind"], "empty_response")
        self.assertEqual(end["status"], "partial")
        self.assertIn("空响应", end["message"])
        self.assertIn("空响应", ag.messages[-1]["content"])

    def test_api_error_surfaces_and_stops(self):
        ag = mk()

        def fake(*a, **kw):
            raise client.APIError("HTTP 500")
            yield  # pragma: no cover

        with mock.patch.object(client, "stream_chat", fake):
            evs = list(ag.run("go"))
        self.assertEqual(evs[-1]["t"], "error")


class PlanMode(unittest.TestCase):
    def test_only_readonly_tools_are_offered(self):
        ag = mk()
        seen = {}

        def fake(model, messages, tools=None, **kw):
            seen["tools"] = tools
            yield {"t": "text", "v": "plan"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        with mock.patch.object(client, "stream_chat", fake), \
             mock.patch.object(ag, "log_usage"):
            list(ag.run("go", plan_mode=True, allowed_tools=tools.READ_ONLY))
        names = {t["function"]["name"] for t in seen["tools"]}
        self.assertEqual(names, set(tools.READ_ONLY))
        for w in ("bash", "write_file", "edit_file"):
            self.assertNotIn(w, names)

    def test_offlist_tool_blocked_before_execution(self):
        """双保险：即便模型凭记忆调了没给它的工具，也必须在执行前拦下。"""
        ag = mk()
        with mock.patch.object(client, "stream_chat",
                               scripted(("", [("bash", {"command": "rm x"})]),
                                        ("ok", []))), \
             mock.patch.object(tools, "run") as run, \
             mock.patch.object(ag, "log_usage"):
            list(ag.run("go", allowed_tools=tools.READ_ONLY))
        run.assert_not_called()
        msg = [m for m in ag.messages if m["role"] == "tool"][0]["content"]
        self.assertIn("不可用", msg)

    def test_plan_preamble_is_prepended(self):
        ag = mk()
        with mock.patch.object(client, "stream_chat", scripted(("p", []))), \
             mock.patch.object(ag, "log_usage"):
            list(ag.run("改个文件", plan_mode=True))
        first_user = [m for m in ag.messages if m["role"] == "user"][0]
        self.assertIn("计划模式", first_user["content"])
        self.assertIn("改个文件", first_user["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DeniedCallVisibility(unittest.TestCase):
    """被拒的调用必须先打调用头，否则用户不知道被拒的是什么。

    2026-08-31 在 `-p`（非 TTY，ask 档 fail-closed 自动拒绝）下复现：终端上
    只有一句「[用户拒绝了这次调用]」，没有任何信息说明模型想跑什么命令。
    tool_start 原本只在权限通过后才发出。

    交互路径（_run_managed，有 controller）与脚本路径（run() 内联循环）是两条
    独立主循环 —— 同一个修复必须做两遍，所以这里两条都测。
    """

    def _events(self, *, managed):
        ag = mk()
        ag.confirm = lambda *a, **k: False
        kwargs = {}
        if managed:
            controller = C.SessionController("t", C.MemoryJournal())
            controller.submit("go", C.QueueMode.NEXT_TURN)
            kwargs["controller"] = controller
        with mock.patch.object(
                client, "stream_chat",
                scripted(("", [("bash", {"command": "wc -l x.py"})]),
                         ("ok", []))), \
             mock.patch.object(ag, "log_usage"):
            return list(ag.run("go", **kwargs))

    def _assert_header_precedes_denial(self, events):
        starts = [e for e in events if e["t"] == "tool_start"]
        denials = [e for e in events if e["t"] == "tool_end" and e.get("denied")]
        self.assertTrue(denials, "这一轮应该产生一次拒绝")
        self.assertTrue(starts, "拒绝之前必须有 tool_start，否则终端只剩一句拒绝")
        first_start = events.index(starts[0])
        first_denial = events.index(denials[0])
        self.assertLess(first_start, first_denial, "调用头要排在拒绝之前")
        return starts[0]

    def test_script_path_shows_what_was_denied(self):
        """run() 内联循环 —— `-p` 与管道用的就是这条。"""
        header = self._assert_header_precedes_denial(self._events(managed=False))
        self.assertEqual(header["name"], "bash")
        rendered = str(header["args"])
        self.assertIn("wc -l x.py", rendered,
                      "光有工具名不够，要能看出是哪条命令")

    def test_managed_path_shows_what_was_denied(self):
        """_run_managed —— 交互会话用的那条。"""
        header = self._assert_header_precedes_denial(self._events(managed=True))
        self.assertEqual(header["name"], "bash")
        self.assertIn("wc -l x.py", str(header["args"]))
