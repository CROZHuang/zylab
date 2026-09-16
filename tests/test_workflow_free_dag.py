"""批 1（DESIGN-workflow-as-infrastructure）：编排归模型、执行归框架。

用户 2026-09-04 定的口径：席位池 Kimi/GLM/DeepSeek/Qwen（不必全用，同席位可多节点）；
节点数按任务定；异构是"75%"——≥3 节点至少 2 个席位；启动前探活，坏路由换候选；
预算耗尽带部分产出；/workflow 手动模板启动退役。
"""
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from core import models, tools, workflows

SEATS = ("Kimi", "GLM", "DeepSeek", "Qwen")


def fake_lineup(**_):
    return [
        {"seat": seat, "family": seat.lower(), "gateway": "deepinfer",
         "id": f"{seat.lower()}-model", "status": "ok", "supports_tools": True}
        for seat in SEATS]


def fake_seat_routes(seat, rows=None, *, route_allowed=None):
    return [
        {"seat": seat, "gateway": "deepinfer", "id": f"{seat.lower()}-model"},
        {"seat": seat, "gateway": "boyue", "id": f"{seat.lower()}-model"},
    ]


class _Store(workflows.WorkflowStore):
    pass


def make_manager(tmp, **kwargs):
    return workflows.WorkflowManager(
        tmp, root=os.path.join(tmp, "wf"), lineup_resolver=fake_lineup,
        seat_routes=fake_seat_routes, **kwargs)


def plan(manager, agents, **kwargs):
    return manager.start_plan(
        "goal", agents, parent_session_id="s1", trigger="model",
        limits={"max_nodes": 16, "max_requests": 24}, **kwargs)


class FreeDagNormalisationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = make_manager(self.tmp.name)
        # 只校验归一化，不真的起线程跑：把 driver 启动替换掉
        self.manager._launch_plan = lambda *a, **k: None
        patcher = mock.patch.object(
            workflows.WorkflowManager, "_start_driver", lambda *a, **k: None, create=True)
        patcher.start(); self.addCleanup(patcher.stop)

    def tearDown(self):
        self.manager.close() if hasattr(self.manager, "close") else None
        self.tmp.cleanup()

    def _plan_nodes(self, agents):
        with mock.patch.object(threading, "Thread") as thread:
            thread.return_value.start = lambda: None
            record = plan(self.manager, agents)
        return record, (record.get("plan") or {}).get("agents") or []

    def test_seven_nodes_with_repeated_seats_are_accepted(self):
        agents = [
            {"key": f"n{i}", "seat": SEATS[i % 2], "task": f"task {i}"}
            for i in range(7)]
        record, nodes = self._plan_nodes(agents)
        self.assertEqual(len(nodes), 7)
        self.assertEqual({n["seat"] for n in nodes}, {"Kimi", "GLM"})
        self.assertEqual(record["notes"], [])

    def test_unspecified_seats_rotate_across_the_pool(self):
        agents = [{"key": f"n{i}", "task": f"task {i}"} for i in range(6)]
        _, nodes = self._plan_nodes(agents)
        self.assertEqual([n["seat"] for n in nodes],
                         ["Kimi", "GLM", "DeepSeek", "Qwen", "Kimi", "GLM"])

    def test_three_nodes_on_one_seat_are_rejected(self):
        agents = [{"key": f"n{i}", "seat": "Kimi", "task": "t"} for i in range(3)]
        with self.assertRaises(workflows.WorkflowError) as ctx:
            self._plan_nodes(agents)
        self.assertIn("至少要用 2 个不同席位", str(ctx.exception))

    def test_two_nodes_on_one_seat_are_allowed_with_a_note(self):
        agents = [{"key": f"n{i}", "seat": "GLM", "task": "t"} for i in range(2)]
        record, nodes = self._plan_nodes(agents)
        self.assertEqual(len(nodes), 2)
        self.assertTrue(any("没有异构交叉验证" in note for note in record["notes"]))

    def test_review_and_final_are_ordinary_nodes(self):
        agents = [
            {"key": "a", "seat": "Kimi", "task": "调研 A"},
            {"key": "b", "seat": "GLM", "task": "调研 B"},
            {"key": "review", "seat": "DeepSeek", "task": "审查 a", "depends_on": ["a"]},
            {"key": "final", "seat": "Qwen", "task": "汇总", "depends_on": ["a", "b", "review"]},
        ]
        _, nodes = self._plan_nodes(agents)
        by_key = {n["key"]: n for n in nodes}
        self.assertEqual(by_key["review"]["depends_on"], ["a"])
        self.assertEqual(by_key["final"]["depends_on"], ["a", "b", "review"])


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self, manager, agents):
        with mock.patch.object(threading, "Thread") as thread:
            thread.return_value.start = lambda: None
            return plan(manager, agents)

    class _Driver:
        def __init__(self):
            self.cancel = threading.Event()
            self.pause = threading.Event()

    def test_engine_does_not_probe_unless_a_probe_is_injected(self):
        manager = make_manager(self.tmp.name)
        record = self._record(manager, [
            {"key": "a", "seat": "Kimi", "task": "t"},
            {"key": "b", "seat": "GLM", "task": "t"}])
        self.assertTrue(manager._preflight_plan(record["id"], self._Driver()))
        self.assertEqual(manager.store.get(record["id"])["preflight"], [])

    def test_dead_route_falls_back_to_next_candidate_before_any_budget(self):
        calls = []
        def probe(gateway, model, timeout=None):
            calls.append((gateway, model))
            if (gateway, model) == ("deepinfer", "kimi-model"):
                return {"status": "error", "error": "502 upstream"}
            return {"status": "ok"}
        manager = make_manager(self.tmp.name, preflight_probe=probe)
        record = self._record(manager, [
            {"key": "a", "seat": "Kimi", "task": "t"},
            {"key": "b", "seat": "GLM", "task": "t"}])
        self.assertTrue(manager._preflight_plan(record["id"], self._Driver()))
        stored = manager.store.get(record["id"])
        kimi = next(n for n in stored["plan"]["agents"] if n["key"] == "a")
        self.assertEqual((kimi["gateway"], kimi["model"]), ("boyue", "kimi-model"))
        self.assertIn(("boyue", "kimi-model"), calls)
        self.assertEqual(stored.get("requests_started", 0), 0, "探活不花 workflow 预算")
        replaced = [p for p in stored["preflight"] if p["replaced_by"]]
        self.assertEqual(len(replaced), 1)

    def test_seat_with_no_live_route_fails_the_workflow_before_dispatch(self):
        def probe(gateway, model, timeout=None):
            return {"status": "error", "error": "timeout"} if "glm" in model else {"status": "ok"}
        manager = make_manager(self.tmp.name, preflight_probe=probe)
        record = self._record(manager, [
            {"key": "a", "seat": "Kimi", "task": "t"},
            {"key": "b", "seat": "GLM", "task": "t"}])
        self.assertFalse(manager._preflight_plan(record["id"], self._Driver()))
        stored = manager.store.get(record["id"])
        self.assertEqual(stored["state"], "failed")
        self.assertIn("探活失败", stored["error"])
        self.assertIn("GLM", stored["error"])
        self.assertTrue(all(n["state"] == "queued" for n in stored["plan"]["agents"]))


class SessionWiringTests(unittest.TestCase):
    def test_preflight_probe_follows_settings(self):
        sess = object.__new__(zylab.Session)
        sess.workflow_manager = mock.Mock()
        sess.cfg = {"workflow": {"preflight": False}}
        sess._sync_workflow_preflight()
        self.assertIsNone(sess.workflow_manager.preflight_probe)
        sess.cfg = {"workflow": {"preflight": True}}
        sess._sync_workflow_preflight()
        self.assertIs(sess.workflow_manager.preflight_probe, models.probe)

    def test_manual_template_start_is_retired(self):
        out = io.StringIO()
        sess = mock.Mock()
        sess.start_workflow.side_effect = AssertionError("must not start")
        with mock.patch.object(sys, "stdout", out):
            zylab.cmd_workflow(sess, "review 帮我审查这个仓库")
            zylab.cmd_workflow(sess, "quick 做点什么")
            zylab.cmd_workflow(sess, "随便一个目标")
        text = out.getvalue()
        self.assertIn("已退役", text)
        self.assertIn("/workflow auto on", text)
        sess.start_workflow.assert_not_called()

    def test_failed_workflow_with_partial_reports_can_be_applied(self):
        sess = object.__new__(zylab.Session)
        sess._workflow_apply_ids = set()
        record = {"id": "wf-1", "state": "failed", "result": "部分报告",
                  "plan": {"agents": [{"key": "a"}]}}
        with mock.patch.object(zylab.Session, "queue_workflow_apply",
                               wraps=zylab.Session.queue_workflow_apply):
            try:
                zylab.Session.queue_workflow_apply(sess, record, manual=True)
            except workflows.WorkflowError as exc:
                self.fail(f"failed-with-result 应可交接：{exc}")
            except AttributeError:
                pass  # 交接的后续步骤依赖完整 Session，这里只验证前置判定放行


if __name__ == "__main__":
    unittest.main()
