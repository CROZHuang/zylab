"""一个坏席位不该把整个 workflow 的产出清零（2026-09-22 功能测试报告 §3）。

实测现场（workflow `wf-b4ba468297`）：
- `deepseek-scout`：403 `model_not_available`，直接失败；
- `kimi-scout`：`provider-attempt limit exhausted (5/5)`，把**共享的**请求预算耗光；
- `glm-scout`：一直 running，而 workflow 整体已被标 failed，报告未返回；
- 合计 8 个 provider 请求、44,523 token，**零有效产出**。

核实之后两件事要改（探活本身没坏——它默认开着、会换候选；但供应商的可用性会
**跑到一半才抖掉**，deepinfer 的成员资格双向抖动是已知事实）：

1. **路线级失败要换席位候选**，而不是让一个坏席位把共享预算耗光；
2. **预算耗尽时先把在飞的 child 收完**，再停——它们的 token 已经花了。
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

import tests  # noqa: F401,E402  —— 状态目录隔离

from core import workflows as W  # noqa: E402


class TheRouteLevelPredicate(unittest.TestCase):
    """判据要认出「换条路线可能就好了」，又不能把真失败也算进去。"""

    # 下面这些串**照抄真实网关的输出**，不是编的。判据第一版只认了 DeepInfer
    # 那一家的说法，在用户的默认网关（Boyue）上恒为 False —— 而单元测试全绿，
    # 因为判据和夹具当时是我从同一个来源抄下来的。
    BOYUE_503 = (
        'HTTP 503: {"error":{"code":"model_not_found","message":"No available '
        'channel for model glm-5.3 under group auto (distributor) '
        '(request id: 202609220719544346612298268d9d66IM3aw2B)"}}')

    def test_provider_side_unavailability_counts(self):
        for error in ("403 model_not_available",
                      "HTTP 403 model is not available",
                      "kind=model_unavailable gateway=deepinfer",
                      self.BOYUE_503,
                      "provider-attempt limit exhausted (5/5)",
                      "Provider Attempt Limit reached"):
            with self.subTest(error=error):
                self.assertTrue(W._is_route_level_failure(error))

    def test_the_default_gateway_is_covered(self):
        """单独钉一条：用户默认网关的原话必须命中。

        2026-09-22 实测发现它**不**命中——那一刻「workflow 已修」这句话是假的。
        """
        self.assertTrue(W._is_route_level_failure(self.BOYUE_503),
                        "Boyue 说 model_not_found / No available channel")

    def test_a_real_task_failure_does_not_count(self):
        """模型答不出来、断言失败——换路线没用，不该白花第二次。"""
        for error in ("模型答不出来", "AssertionError: 断言失败",
                      "context_length exceeded", "", None,
                      "worker crashed: KeyError"):
            with self.subTest(error=error):
                self.assertFalse(W._is_route_level_failure(error))


class SeatFailover(unittest.TestCase):
    """换的是**该席位的下一候选**，用的是探活那套候选顺序。"""

    def engine(self, candidates):
        engine = object.__new__(W.WorkflowManager)
        engine.seat_routes = lambda seat, route_allowed=None: candidates
        engine._route_allowed = lambda *a, **k: True
        engine.updates = []

        def update(workflow_id, key, **changes):
            engine.updates.append((key, changes))

        engine._plan_node_update = update
        return engine

    def node(self, **extra):
        base = {"key": "scout-1", "seat": "kimi",
                "gateway": "deepinfer", "model": "kimi-k3"}
        base.update(extra)
        return base

    def test_it_requeues_on_the_next_candidate(self):
        engine = self.engine([
            {"gateway": "deepinfer", "id": "kimi-k3"},      # 就是坏的那条
            {"gateway": "boyue", "id": "kimi-k3-256k"},     # 下一候选
        ])
        self.assertTrue(engine._seat_failover("wf", self.node()))
        key, changes = engine.updates[-1]
        self.assertEqual(key, "scout-1")
        self.assertEqual(changes["state"], "queued")
        self.assertEqual(changes["gateway"], "boyue")
        self.assertEqual(changes["model"], "kimi-k3-256k")
        self.assertEqual(changes["agent_id"], "")
        self.assertIn("换路线", changes["error"])

    def test_it_never_reuses_a_route_it_already_tried(self):
        engine = self.engine([
            {"gateway": "deepinfer", "id": "kimi-k3"},
            {"gateway": "boyue", "id": "kimi-k3-256k"},
        ])
        node = self.node(failover_from=[["boyue", "kimi-k3-256k"]])
        self.assertFalse(engine._seat_failover("wf", node),
                         "两条都试过了，没有可换的")
        self.assertEqual(engine.updates, [])

    def test_a_seatless_node_is_left_alone(self):
        engine = self.engine([{"gateway": "boyue", "id": "x"}])
        self.assertFalse(engine._seat_failover("wf", self.node(seat="")))

    def test_a_broken_seat_lookup_is_not_fatal(self):
        engine = self.engine([])

        def boom(seat, route_allowed=None):
            raise RuntimeError("目录读坏了")

        engine.seat_routes = boom
        self.assertFalse(engine._seat_failover("wf", self.node()))

    def test_it_records_where_it_came_from(self):
        """报告里要看得见「这个席位换过」，否则结果无法解释。"""
        engine = self.engine([
            {"gateway": "deepinfer", "id": "kimi-k3"},
            {"gateway": "boyue", "id": "kimi-k3-256k"},
        ])
        engine._seat_failover("wf", self.node())
        _, changes = engine.updates[-1]
        self.assertIn(["deepinfer", "kimi-k3"],
                      [list(x) for x in changes["failover_from"]])


class DrainBeforeStopping(unittest.TestCase):
    """预算耗尽时，已经花过 token 的 child 要先把报告交完。"""

    def engine(self, children):
        engine = object.__new__(W.WorkflowManager)
        engine.workspace = mock.Mock()
        engine.workspace.get = lambda agent_id: children[agent_id]
        engine._release_slot = mock.Mock()
        engine._refresh_agent = mock.Mock()
        engine.updates = []
        engine._plan_node_update = (
            lambda wf, key, **changes: engine.updates.append((key, changes)))
        return engine

    def driver(self):
        holder = mock.Mock()
        holder.cancel = mock.Mock()
        holder.cancel.is_set.return_value = False
        holder.cancel.wait = lambda _timeout: False
        return holder

    def test_a_finished_child_report_is_persisted(self):
        children = {"a-1": {"id": "a-1", "state": "completed",
                            "result": "查到了三条", "error": ""}}
        engine = self.engine(children)
        active = {"a-1": {"key": "scout-1", "seat": "glm",
                          "gateway": "boyue", "name": "glm-scout"}}
        engine._drain_active("wf", self.driver(), active)
        self.assertEqual(active, {}, "收完就该从 active 里摘掉")
        key, changes = engine.updates[-1]
        self.assertEqual(key, "scout-1")
        self.assertEqual(changes["state"], "completed")
        self.assertIn("查到了三条", changes["report"])

    def test_it_is_bounded_and_gives_up(self):
        """workflow 已经该停了——排空不能变成无界等待。"""
        children = {"a-1": {"id": "a-1", "state": "running"}}
        engine = self.engine(children)
        engine.DRAIN_SECONDS = 0.2
        active = {"a-1": {"key": "scout-1", "seat": "glm",
                          "gateway": "boyue", "name": "glm-scout"}}
        started = time.monotonic()
        engine._drain_active("wf", self.driver(), active)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(list(active), ["a-1"], "没交完的原样留着，按失败处理")

    def test_cancel_stops_the_drain_at_once(self):
        children = {"a-1": {"id": "a-1", "state": "running"}}
        engine = self.engine(children)
        holder = self.driver()
        holder.cancel.is_set.return_value = True
        started = time.monotonic()
        engine._drain_active("wf", holder, {"a-1": {
            "key": "scout-1", "seat": "glm", "gateway": "boyue",
            "name": "glm-scout"}})
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_budget_stop_drains_first(self):
        """源码层面钉住顺序：先排空、再 raise。"""
        source = (Path(ROOT) / "core" / "workflows.py").read_text(
            encoding="utf-8")
        drain = source.index("self._drain_active(workflow_id, driver, active)")
        raise_at = source.index("raise WorkflowError(stopped)", drain - 400)
        self.assertLess(drain, raise_at,
                        "排空必须在 raise 之前，否则在飞的产出还是丢")


if __name__ == "__main__":
    unittest.main()
