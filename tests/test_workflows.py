"""异构 /workflow 编排、持久化与取消回归。"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from unittest import mock as _mock
from core import agents, workflows

# ignore_cleanup_errors：Windows 上打开着的文件删不掉（这是平台语义，
# 不是缺陷）。这些用例把 sqlite 库开在临时目录里、不显式关闭，
# POSIX 上照样能删，Windows 上会在 tearDown 抛 WinError 32，
# 把一个通过的断言变成 ERROR。


def lineup():
    return [
        {
            "seat": "Qwen", "family": "阿里",
            "id": "qwen-max", "gateway": "boyue",
            "status": "ok", "supports_tools": True,
            "quality_tier": "flagship",
        },
        {
            "seat": "GLM", "family": "智谱",
            "id": "glm-flagship", "gateway": "boyue",
            "status": "ok", "supports_tools": True,
            "quality_tier": "flagship",
        },
        {
            "seat": "DeepSeek", "family": "deepseek",
            "id": "deepseek-pro", "gateway": "deepinfer",
            "status": "ok", "supports_tools": True,
            "quality_tier": "flagship",
        },
        {
            "seat": "MiniMax", "family": "MiniMax",
            "id": "minimax-m", "gateway": "deepinfer",
            "status": "ok", "supports_tools": True,
            "quality_tier": "flagship",
        },
        {
            "seat": "Kimi", "family": "moonshot",
            "id": "kimi-k", "gateway": "deepinfer",
            "status": "ok", "supports_tools": True,
            "quality_tier": "flagship",
        },
        {
            "seat": "Claude", "family": "Anthropic",
            "id": "claude-sonnet-5", "gateway": "boyue",
            "status": "ok", "supports_tools": True,
            "quality_tier": "flagship",
        },
        {
            "seat": "GPT", "family": "OpenAI",
            "id": "gpt-5.6-terra", "gateway": "boyue",
            "status": "ok", "supports_tools": True,
            "quality_tier": "flagship",
        },
    ]


def plan(*seats):
    """按席位列出 n 个独立节点（DAG 是唯一拓扑；旧 preset 启动已删）。"""
    return [{"key": f"n{index}", "seat": seat, "task": f"task {index}"}
            for index, seat in enumerate(seats, 1)]


class WorkflowAgent:
    instances = []

    def __init__(self, model, gateway=None, load_md=True):
        self.model = model
        self.gateway = gateway
        self.load_md = load_md
        self.messages = []
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_total = 0
        self.allowed_tools_seen = None
        type(self).instances.append(self)

    def load_context(self, _value):
        return None

    def context_snapshot(self):
        return None

    def run(self, user_input, *, allowed_tools=None, event_sink=None,
            cancel=None, **_kwargs):
        self.allowed_tools_seen = frozenset(allowed_tools or ())
        if cancel is not None and cancel.is_set():
            yield {"t": "interrupted"}
            return
        guard = _kwargs.pop("before_provider_attempt", None)
        if guard is not None:
            guard({
                "attempt": 1, "purpose": "chat",
                "model": self.model, "gateway": self.gateway,
            })
        user = {"role": "user", "content": str(user_input)}
        assistant = {
            "role": "assistant",
            "content": (
                f"evidence from {self.model}@{self.gateway}: "
                "core/example.py:1 verified"),
        }
        self.messages.extend((user, assistant))
        event_sink([
            {"kind": "user_message", "payload": {"message": user}},
            {"kind": "assistant_message", "payload": {"message": assistant}},
        ])
        yield {"t": "text", "v": assistant["content"]}
        yield {"t": "end", "reason": "stop"}


class BlockingWorkflowAgent(WorkflowAgent):
    started = threading.Event()

    def run(self, _user_input, *, cancel=None, **_kwargs):
        guard = _kwargs.pop("before_provider_attempt", None)
        if guard is not None:
            guard({
                "attempt": 1, "purpose": "chat",
                "model": self.model, "gateway": self.gateway,
            })
        type(self).started.set()
        while cancel is None or not cancel.wait(0.005):
            pass
        yield {"t": "interrupted"}


class ConcurrencyWorkflowAgent(WorkflowAgent):
    lock = threading.Lock()
    active = 0
    active_by_gateway = {}
    max_active = 0
    max_by_gateway = {}
    three_started = threading.Event()
    release = threading.Event()

    def run(self, user_input, *, cancel=None, **kwargs):
        cls = type(self)
        guard = kwargs.pop("before_provider_attempt", None)
        if guard is not None:
            guard({
                "attempt": 1, "purpose": "chat",
                "model": self.model, "gateway": self.gateway,
            })
        with cls.lock:
            cls.active += 1
            cls.active_by_gateway[self.gateway] = (
                cls.active_by_gateway.get(self.gateway, 0) + 1)
            cls.max_active = max(cls.max_active, cls.active)
            cls.max_by_gateway[self.gateway] = max(
                cls.max_by_gateway.get(self.gateway, 0),
                cls.active_by_gateway[self.gateway])
            if cls.active >= 3:
                cls.three_started.set()
        try:
            while not cls.release.wait(0.005):
                if cancel is not None and cancel.is_set():
                    yield {"t": "interrupted"}
                    return
        finally:
            with cls.lock:
                cls.active -= 1
                cls.active_by_gateway[self.gateway] -= 1
        yield from super().run(
            user_input, cancel=cancel, **kwargs)


class MultiAttemptWorkflowAgent(WorkflowAgent):
    """模拟一次 child 内的两次真实 provider attempts（例如 tool loop）。"""

    def run(self, user_input, *, cancel=None, **kwargs):
        guard = kwargs.pop("before_provider_attempt", None)
        if guard is not None:
            for attempt in (1, 2):
                guard({
                    "attempt": attempt, "purpose": "chat",
                    "model": self.model, "gateway": self.gateway,
                })
        yield from super().run(user_input, cancel=cancel, **kwargs)


class GreedyWorkflowAgent(WorkflowAgent):
    """持续请求工具回合，验证单 child 不能抢光全局预算。"""

    def run(self, _user_input, *, cancel=None, **kwargs):
        guard = kwargs.pop("before_provider_attempt", None)
        if guard is not None:
            for attempt in range(1, 20):
                guard({
                    "attempt": attempt, "purpose": "chat",
                    "model": self.model, "gateway": self.gateway,
                })
        yield {"t": "end", "reason": "stop"}


class WorkflowManagerTests(unittest.TestCase):
    def setUp(self):
        WorkflowAgent.instances = []
        BlockingWorkflowAgent.instances = []
        BlockingWorkflowAgent.started = threading.Event()
        ConcurrencyWorkflowAgent.instances = []
        ConcurrencyWorkflowAgent.active = 0
        ConcurrencyWorkflowAgent.active_by_gateway = {}
        ConcurrencyWorkflowAgent.max_active = 0
        ConcurrencyWorkflowAgent.max_by_gateway = {}
        ConcurrencyWorkflowAgent.three_started = threading.Event()
        ConcurrencyWorkflowAgent.release = threading.Event()
        MultiAttemptWorkflowAgent.instances = []

    def make_manager(self, root, agent_factory=WorkflowAgent):
        workspace = agents.AgentWorkspace(
            root=root / "agents", agent_factory=agent_factory,
            poll_interval=0.005)
        manager = workflows.WorkflowManager(
            workspace, root=root / "workflows",
            lineup_resolver=lambda **_kwargs: lineup(),
            poll_interval=0.005)
        return manager, workspace

    def test_node_transitions_emit_workflow_node_events_with_timestamps(self):
        # J7：节点 running/completed 各发一个 workflow_node 事件，带 started_at/ended_at，
        # TUI 据此打完成行、刷名册，不重读文件
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(root)
            started = manager.start_plan(
                "fix the parser",
                [{"key": "code", "seat": "Qwen", "task": "梳理结构"},
                 {"key": "tests", "seat": "GLM", "task": "找反例"}],
                parent_session_id="parent-session")
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            events = [event for event in manager.drain()
                      if event.get("kind") == "workflow_node"]
            nodes = [dict(event["payload"].get("node") or {}) for event in events]
            running = [node for node in nodes if node.get("state") == "running"]
            completed = [node for node in nodes if node.get("state") == "completed"]
            self.assertTrue(running and completed, nodes)
            self.assertTrue(all(node.get("started_at") for node in running), running)
            self.assertTrue(all(node.get("ended_at") for node in completed), completed)
            self.assertTrue(all(node.get("key") in {"code", "tests"} for node in nodes))
            self.assertNotIn("node", manager.get(started["id"]))
            manager.close()
            workspace.close()

    def test_review_and_synthesis_become_dag_nodes_and_synthesis_is_the_result(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(root)
            started = manager.start_plan(
                "fix the parser",
                [
                    {"key": "code", "seat": "Qwen", "task": "梳理结构"},
                    {"key": "tests", "seat": "GLM", "task": "找反例"},
                    {"key": "root", "seat": "DeepSeek", "task": "找根因"},
                ],
                review={"enabled": True, "reviewers": ["Kimi"], "rounds": 1},
                synthesis={"seat": "Kimi"},
                parent_session_id="parent-session",
                parent_turn_id="turn-workflow", mode="implement",
                hook_config={"permissions": {}}, budget=8)
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            events = manager.drain()
            agent_records = [
                workspace.get(item["id"]) for item in final["agents"]]
            manager.close()
            workspace.close()
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["stage"], "completed")
        nodes = {node["key"]: node for node in final["plan"]["agents"]}
        self.assertEqual(
            set(nodes), {"code", "tests", "root", "review_r1_kimi", "final"})
        self.assertEqual(nodes["review_r1_kimi"]["role"], "review")
        self.assertEqual(
            set(nodes["review_r1_kimi"]["depends_on"]), {"code", "tests", "root"})
        self.assertEqual(nodes["final"]["role"], "synthesis")
        self.assertEqual(
            set(nodes["final"]["depends_on"]),
            {"code", "tests", "root", "review_r1_kimi"})
        self.assertTrue(all(
            node["state"] == "completed" for node in nodes.values()))
        self.assertEqual(final["completed_agents"], 5)
        self.assertTrue(
            final["result"].startswith("evidence from kimi-k@deepinfer"),
            final["result"][:80])
        self.assertTrue(all(
            item["permission_mode"] == "read_only"
            for item in agent_records))
        self.assertTrue(all(
            item["kind"].startswith("workflow-")
            for item in agent_records))
        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds[0], "workflow_started")
        self.assertIn("workflow_completed", kinds)
        self.assertTrue(all(
            instance.allowed_tools_seen == agents.CHILD_TOOLS
            for instance in WorkflowAgent.instances))

    def test_start_plan_defaults_and_empty_modes_fail_closed_to_review(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(root)
            observed = []
            for label, extra in (
                    ("omitted", {}),
                    ("none", {"mode": None}),
                    ("empty", {"mode": ""})):
                started = manager.start_plan(
                    f"safe default {label}",
                    [
                        {"key": "a", "seat": "Qwen", "task": "a"},
                        {"key": "b", "seat": "GLM", "task": "b"},
                    ],
                    parent_session_id="parent-session",
                    budget=2,
                    **extra,
                )
                self.assertTrue(manager.wait(started["id"], timeout=3.0))
                final = manager.get(started["id"])
                observed.append((final["mode"], final["auto_apply"]))
            manager.close()
            workspace.close()

        self.assertEqual(observed, [("review", False)] * 3)

    def test_legacy_node_reasoning_is_hidden_from_combined_report(self):
        combined = workflows.WorkflowManager._plan_reports({
            "plan": {"agents": [{
                "key": "legacy",
                "seat": "MiniMax",
                "model": "minimax-m2.7",
                "gateway": "deepinfer",
                "state": "completed",
                "report": (
                    "<think>private legacy reasoning</think>\n"
                    "VISIBLE_LEGACY_REPORT"),
            }]},
        })

        self.assertNotIn("private legacy reasoning", combined)
        self.assertIn(agents.REASONING_HIDDEN_NOTICE, combined)
        self.assertIn("VISIBLE_LEGACY_REPORT", combined)

    def test_cancel_stops_active_workflow_and_owned_agents(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, BlockingWorkflowAgent)

            started = manager.start_plan(
                "wait forever", plan("Qwen", "GLM"),
                parent_session_id="parent-session", budget=4)
            self.assertTrue(BlockingWorkflowAgent.started.wait(1.0))
            manager.cancel(started["id"])
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            manager.close()
            workspace.close()

        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(final["error"], "用户取消")

    def test_spawn_failure_cancels_already_started_agents(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, BlockingWorkflowAgent)
            original_spawn = workspace.spawn
            calls = 0

            def fail_second_spawn(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls >= 2:
                    raise agents.AgentRuntimeError(
                        "synthetic spawn failure")
                return original_spawn(*args, **kwargs)

            workspace.spawn = fail_second_spawn
            started = manager.start_plan(
                "fail during assembly", plan("Qwen", "GLM", "DeepSeek"),
                parent_session_id="parent-session", budget=6)
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            child_rows = workspace.list(
                parent_session_id="parent-session")
            active_total = manager._active_total
            manager.close()
            workspace.close()

        self.assertEqual(final["state"], "failed")
        self.assertIn("synthetic spawn failure", final["error"])
        self.assertEqual(len(child_rows), 1)
        self.assertEqual(child_rows[0]["state"], "cancelled")
        self.assertEqual(active_total, 0)

    def test_post_spawn_state_failure_cancels_child_and_releases_slot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, BlockingWorkflowAgent)

            def fail_projection(*_args, **_kwargs):
                raise workflows.WorkflowError("synthetic state failure")

            manager._plan_node_update = fail_projection
            started = manager.start_plan(
                "persist after spawn",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b"},
                ],
                parent_session_id="parent-session", budget=2)
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            children = workspace.list(parent_session_id="parent-session")
            active_total = manager._active_total
            manager.close()
            workspace.close()

        self.assertEqual(final["state"], "failed")
        self.assertIn("synthetic state failure", final["error"])
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["state"], "cancelled")
        self.assertEqual(active_total, 0)

    def test_foreign_live_owner_cannot_be_marked_cancelled(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, BlockingWorkflowAgent)
            other_workspace = agents.AgentWorkspace(
                root=root / "other-agents", agent_factory=WorkflowAgent)
            other = workflows.WorkflowManager(
                other_workspace, root=root / "workflows",
                lineup_resolver=lambda **_kwargs: lineup(),
                poll_interval=0.005)

            started = manager.start_plan(
                "owned elsewhere", plan("Qwen", "GLM"),
                parent_session_id="parent-session", budget=4)
            self.assertTrue(BlockingWorkflowAgent.started.wait(1.0))
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "原 Zylab 进程"):
                other.cancel(started["id"])
            self.assertEqual(manager.get(started["id"])["state"], "running")
            manager.cancel(started["id"])
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            other.close()
            other_workspace.close()
            manager.close()
            workspace.close()

    def test_owner_identity_rejects_reused_live_pid(self):
        record = {
            "owner_pid": os.getpid(),
            "owner_boot_id": workflows.store._boot_id(),
            "owner_proc_start": "not-the-current-start-tick",
        }
        self.assertFalse(workflows._owner_alive(record))
        record["owner_proc_start"] = workflows.store._proc_start(os.getpid())
        self.assertTrue(workflows._owner_alive(record))

    def test_scheduler_shares_gateway_limits_across_workflows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, ConcurrencyWorkflowAgent)

            first = manager.start_plan(
                "measure first", plan("Qwen", "GLM", "DeepSeek"),
                parent_session_id="parent-session", budget=6)
            second = manager.start_plan(
                "measure second", plan("Qwen", "GLM", "DeepSeek"),
                parent_session_id="parent-session", budget=6)
            self.assertTrue(
                ConcurrencyWorkflowAgent.three_started.wait(2.0))
            # 给第二个 scheduler 足够时间尝试抢占；配额若只在单 workflow
            # 内生效，这里会观察到 4 个并发请求。
            threading.Event().wait(0.5)
            with ConcurrencyWorkflowAgent.lock:
                max_active = ConcurrencyWorkflowAgent.max_active
                max_by_gateway = dict(
                    ConcurrencyWorkflowAgent.max_by_gateway)
            ConcurrencyWorkflowAgent.release.set()
            self.assertTrue(manager.wait(first["id"], timeout=4.0))
            self.assertTrue(manager.wait(second["id"], timeout=4.0))
            finals = (manager.get(first["id"]), manager.get(second["id"]))
            manager.close()
            workspace.close()

        self.assertTrue(all(
            row["state"] == "completed" for row in finals))
        self.assertEqual(max_active, 3)
        self.assertTrue(all(
            value <= workflows.MAX_PER_GATEWAY
            for value in max_by_gateway.values()))

    def test_requires_two_qualified_flagship_seats(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            workspace = agents.AgentWorkspace(
                root=root / "agents", agent_factory=WorkflowAgent)
            manager = workflows.WorkflowManager(
                workspace, root=root / "workflows",
                lineup_resolver=lambda **_kwargs: lineup()[:1])

            # 只有一个可用席位：三个未指定席位的节点都会轮到它，触发异构软门槛
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "至少要用 2 个不同席位"):
                manager.start_plan(
                    "insufficient",
                    [{"task": "a"}, {"task": "b"}, {"task": "c"}],
                    parent_session_id="parent-session", budget=6)

            manager.close()
            workspace.close()

    def test_free_dag_passes_completed_dependency_report_downstream(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(root)

            started = manager.start_plan(
                "trace dependency evidence",
                [
                    {
                        "key": "source",
                        "seat": "Qwen",
                        "task": "collect source evidence",
                    },
                    {
                        "key": "consumer",
                        "seat": "GLM",
                        "task": "check the source evidence",
                        "depends_on": ["source"],
                    },
                ],
                parent_session_id="parent-session",
                parent_turn_id="turn-dag",
                budget=2,
            )
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            consumer = next(
                node for node in final["plan"]["agents"]
                if node["key"] == "consumer")
            durable_child = workspace.runtime.store.get(
                consumer["agent_id"])
            manager.close()
            workspace.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["requests_started"], 2)
        self.assertEqual(
            [node["state"] for node in final["plan"]["agents"]],
            ["completed", "completed"])
        self.assertIn("## dependency source", durable_child["context"])
        self.assertIn("evidence from qwen-max@boyue", durable_child["context"])

    def test_request_budget_counts_each_child_provider_attempt(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, MultiAttemptWorkflowAgent)
            started = manager.start_plan(
                "bound every provider attempt",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b",
                     "depends_on": ["a"]},
                ],
                parent_session_id="parent-session", budget=3)
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            manager.close()
            workspace.close()

        nodes = final["plan"]["agents"]
        attempt_events = [
            event for event in final["events"]
            if event["kind"] == "provider_attempt_started"]
        self.assertEqual(final["requests_started"], 3)
        self.assertEqual(final["request_counter_mode"], "provider_attempt_v2")
        self.assertEqual(final["state"], "failed")
        self.assertEqual(len(attempt_events), 3)
        self.assertEqual([node["state"] for node in nodes],
                         ["completed", "failed"])
        self.assertIn("provider-attempt budget exhausted", nodes[1]["error"])

    def test_per_agent_budget_prevents_early_nodes_from_starving_peers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, GreedyWorkflowAgent)
            started = manager.start_plan(
                "share provider attempts fairly",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "DeepSeek", "task": "b"},
                ],
                parent_session_id="parent-session", budget=20,
                limits={
                    "max_requests": 20,
                    "max_requests_per_agent": 5,
                    "max_parallel": 2,
                })
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            manager.close()
            workspace.close()

        actor_counts = {}
        for event in final["events"]:
            if event["kind"] != "provider_attempt_started":
                continue
            actor = event["payload"]["actor"]
            actor_counts[actor] = actor_counts.get(actor, 0) + 1
        self.assertEqual(final["requests_started"], 10)
        self.assertEqual(
            actor_counts, {"node:a": 5, "node:b": 5})
        self.assertEqual(final["state"], "failed")
        self.assertTrue(all(
            "agent provider-attempt limit exhausted" in str(node["error"])
            for node in final["plan"]["agents"]))

    def test_dynamic_nodes_append_while_running_and_obey_dependencies(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, ConcurrencyWorkflowAgent)
            started = manager.start_plan(
                "expand from evidence",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b"},
                ],
                parent_session_id="parent-session", budget=5,
                limits={"max_nodes": 6, "max_requests": 5})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if manager.get(started["id"])["requests_started"] == 2:
                    break
                time.sleep(0.01)
            expanded = manager.add_nodes(started["id"], [{
                "key": "c", "seat": "DeepSeek", "task": "verify a",
                "depends_on": ["a"],
            }], requested_by="main-agent")
            ConcurrencyWorkflowAgent.release.set()
            self.assertTrue(manager.wait(started["id"], timeout=4.0))
            final = manager.get(started["id"])
            manager.close()
            workspace.close()

        self.assertEqual(expanded["budget"]["planned_requests"], 3)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["requests_started"], 3)
        self.assertEqual(final["expected_agents"], 3)
        self.assertEqual(
            [node["state"] for node in final["plan"]["agents"]],
            ["completed", "completed", "completed"])
        self.assertTrue(any(
            event["kind"] == "nodes_added" for event in final["events"]))

    def test_dynamic_nodes_fail_closed_on_request_budget_and_cycle(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, BlockingWorkflowAgent)
            started = manager.start_plan(
                "bounded expansion",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b"},
                ], parent_session_id="parent-session", budget=2,
                limits={"max_nodes": 3})
            self.assertTrue(BlockingWorkflowAgent.started.wait(1.0))
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "最多 2 个"):
                manager.add_nodes(started["id"], [
                    {"key": "c", "seat": "DeepSeek", "task": "c"},
                    {"key": "d", "seat": "MiniMax", "task": "d"},
                    {"key": "e", "seat": "Kimi", "task": "e"},
                ])
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "超过 max_requests=2"):
                manager.add_nodes(started["id"], [{
                    "key": "c", "seat": "DeepSeek", "task": "c"}])
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "超过 max_nodes=3"):
                manager.add_nodes(started["id"], [
                    {"key": "c", "seat": "DeepSeek", "task": "c"},
                    {"key": "d", "seat": "MiniMax", "task": "d"},
                ])
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "不存在的"):
                manager.add_nodes(started["id"], [{
                    "key": "c", "seat": "DeepSeek", "task": "c",
                    "depends_on": ["missing"],
                }])
            manager.cancel(started["id"])
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            manager.close()
            workspace.close()

    def test_dynamic_nodes_reject_exhausted_runtime_budget_before_mutation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, BlockingWorkflowAgent)
            started = manager.start_plan(
                "token-bounded expansion",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b"},
                ], parent_session_id="parent-session", budget=5,
                limits={"max_nodes": 4, "max_requests": 5,
                        "max_tokens": 16_000})
            self.assertTrue(BlockingWorkflowAgent.started.wait(1.0))

            before = manager.get(started["id"])

            def exhaust_requests(record):
                record["requests_started"] = record["budget"]["max_requests"]
                return record

            manager.store.update(started["id"], exhaust_requests)
            with self.assertRaisesRegex(
                    workflows.WorkflowError,
                    "provider-attempt budget exhausted"):
                manager.add_nodes(started["id"], [{
                    "key": "c", "seat": "DeepSeek", "task": "c"}])
            after_requests = manager.get(started["id"])

            def exhaust_tokens(record):
                record["requests_started"] = before["requests_started"]
                record["tokens_used"] = record["budget"]["max_tokens"]
                return record

            manager.store.update(started["id"], exhaust_tokens)
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "token budget exhausted"):
                manager.add_nodes(started["id"], [{
                    "key": "c", "seat": "DeepSeek", "task": "c"}])
            after = manager.get(started["id"])
            manager.cancel(started["id"])
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            manager.close()
            workspace.close()

        self.assertEqual(after_requests["plan"], before["plan"])
        self.assertEqual(after_requests["budget"], before["budget"])
        self.assertEqual(
            after_requests["expected_agents"], before["expected_agents"])
        self.assertEqual(after["plan"], before["plan"])
        self.assertEqual(after["budget"], before["budget"])
        self.assertEqual(after["expected_agents"], before["expected_agents"])

    def test_pause_resume_requeues_interrupted_nodes_without_losing_state(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, BlockingWorkflowAgent)
            started = manager.start_plan(
                "pause safely",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b"},
                ], parent_session_id="parent-session", budget=4)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if manager.get(started["id"])["requests_started"] == 2:
                    break
                time.sleep(0.01)
            paused = manager.pause_workflow(started["id"])
            workspace.runtime.agent_factory = WorkflowAgent
            resumed = manager.resume_workflow(started["id"])
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            manager.close()
            workspace.close()

        self.assertEqual(paused["state"], "queued")
        self.assertEqual(paused["stage"], "paused")
        self.assertTrue(all(
            node["state"] == "queued"
            for node in paused["plan"]["agents"]))
        self.assertEqual(resumed["state"], "queued")
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["requests_started"], 4)
        self.assertTrue(all(
            node["previous_agent_ids"]
            for node in final["plan"]["agents"]))

    def test_completed_node_restart_requires_and_cascades_downstream(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(root)
            started = manager.start_plan(
                "rerun evidence",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b",
                     "depends_on": ["a"]},
                ], parent_session_id="parent-session", budget=4)
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "显式 cascade"):
                manager.restart_node(started["id"], "a")
            reset = manager.restart_node(
                started["id"], "a", cascade=True)
            manager.resume_workflow(reset["id"])
            self.assertTrue(manager.wait(started["id"], timeout=3.0))
            final = manager.get(started["id"])
            manager.close()
            workspace.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["requests_started"], 4)
        self.assertEqual(
            [node["attempts"] for node in final["plan"]["agents"]],
            [2, 2])
        self.assertTrue(all(
            node["previous_agent_ids"]
            for node in final["plan"]["agents"]))

    def test_free_plan_rejects_cycles_and_under_budgeted_review(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(root)
            cyclic = [
                {"key": "a", "seat": "Qwen", "task": "a",
                 "depends_on": ["b"]},
                {"key": "b", "seat": "GLM", "task": "b",
                 "depends_on": ["a"]},
            ]
            with self.assertRaisesRegex(
                    workflows.WorkflowError, "存在循环"):
                manager.start_plan(
                    "cycle", cyclic,
                    parent_session_id="parent-session", budget=2)

            with self.assertRaisesRegex(
                    workflows.WorkflowError, "key 无效"):
                manager.start_plan(
                    "invalid key",
                    [
                        {"key": "../a", "seat": "Qwen", "task": "a"},
                        {"key": "b", "seat": "GLM", "task": "b"},
                    ], parent_session_id="parent-session", budget=2)

            with self.assertRaisesRegex(
                    workflows.WorkflowError, "超过 budget 2"):
                manager.start_plan(
                    "under budget",
                    [
                        {"seat": "Qwen", "task": "a"},
                        {"seat": "GLM", "task": "b"},
                    ],
                    parent_session_id="parent-session",
                    review={
                        "enabled": True,
                        "reviewers": ["DeepSeek"],
                        "rounds": 1,
                    },
                    budget=2,
                )
            manager.close()
            workspace.close()

    def test_paused_dag_recovers_only_interrupted_nodes_within_budget(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manager, workspace = self.make_manager(
                root, BlockingWorkflowAgent)
            started = manager.start_plan(
                "survive restart",
                [
                    {"key": "a", "seat": "Qwen", "task": "a"},
                    {"key": "b", "seat": "GLM", "task": "b"},
                ],
                parent_session_id="parent-session",
                parent_turn_id="turn-recovery",
                budget=4,
            )
            deadline = time.monotonic() + 2.0
            before = None
            while time.monotonic() < deadline:
                before = manager.get(started["id"])
                if before["requests_started"] == 2:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(before)
            self.assertEqual(before["requests_started"], 2)
            self.assertTrue(manager.close(preserve=True))
            workspace.close()
            paused = manager.get(started["id"])

            recovered_manager, recovered_workspace = self.make_manager(root)
            claimed = recovered_manager.recover("parent-session")
            self.assertEqual([row["id"] for row in claimed], [started["id"]])
            self.assertTrue(
                recovered_manager.wait(started["id"], timeout=3.0))
            final = recovered_manager.get(started["id"])
            recovered_manager.close()
            recovered_workspace.close()

        self.assertEqual(paused["state"], "queued")
        self.assertEqual(paused["stage"], "paused")
        self.assertIsNone(paused["owner_id"])
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["recovery_count"], 1)
        self.assertEqual(final["requests_started"], 4)
        self.assertEqual(
            [node["attempts"] for node in final["plan"]["agents"]],
            [2, 2])
        self.assertEqual(
            [node["state"] for node in final["plan"]["agents"]],
            ["completed", "completed"])

    def test_store_rejects_protected_root_without_write_probe(self):
        with _mock.patch.dict(
                os.environ,
                {"ZYLAB_PROTECTED_PATHS": "/protected/archive"}), \
                self.assertRaisesRegex(
                workflows.WorkflowError, "受保护路径"):
            workflows.WorkflowStore("/protected/archive/workflows")


if __name__ == "__main__":
    unittest.main()
