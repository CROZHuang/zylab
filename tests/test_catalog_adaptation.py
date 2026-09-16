"""平台模型名单会变，zylab 必须自己跟上。

2026-09-08 的事故形状：缓存里 deepseek-v4-pro 还是 status=ok，网关早已 403；
而缓存上次刷新是 18 天前，因为**只有人手动跑 /model refresh 才会更新**。
三条证据链各自的责任：目录管「在不在册」，真实请求管「调不调得通」，
probe 管「支不支持工具」。这些测试钉住它们的交接。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import models                              # noqa: E402
from core import settings as CFG                     # noqa: E402


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            models, "CACHE", Path(self.tmp.name) / "models.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def catalog(self, ids):
        return [{"id": mid, "max_model_len": 262_144} for mid in ids]

    def test_never_fetched_counts_as_stale(self):
        self.assertIsNone(models.catalog_age_seconds("deepinfer"))
        self.assertTrue(models.catalog_is_stale("deepinfer"))

    def test_refresh_stamps_the_time_and_clears_staleness(self):
        with mock.patch.object(models.client, "list_models",
                               return_value=self.catalog(["a", "b"])):
            result = models.refresh_catalog("deepinfer")
        self.assertEqual(result["count"], 2)
        self.assertEqual(sorted(result["added"]), ["a", "b"])
        self.assertTrue(result["changed"])
        self.assertLess(models.catalog_age_seconds("deepinfer"), 60)
        self.assertFalse(models.catalog_is_stale("deepinfer"))
        # TTL 到期后又算过期
        self.assertTrue(models.catalog_is_stale("deepinfer", ttl=0))

    def test_second_refresh_without_changes_is_silent(self):
        with mock.patch.object(models.client, "list_models",
                               return_value=self.catalog(["a"])):
            models.refresh_catalog("deepinfer")
            again = models.refresh_catalog("deepinfer")
        self.assertFalse(again["changed"])
        self.assertEqual(again["added"], [])

    def test_departure_and_return_are_both_reported(self):
        with mock.patch.object(models.client, "list_models",
                               return_value=self.catalog(["a", "b"])):
            models.refresh_catalog("deepinfer")
        with mock.patch.object(models.client, "list_models",
                               return_value=self.catalog(["a"])):
            gone = models.refresh_catalog("deepinfer")
        self.assertEqual(gone["delisted"], ["b"])
        with mock.patch.object(models.client, "list_models",
                               return_value=self.catalog(["a", "b"])):
            back = models.refresh_catalog("deepinfer")
        self.assertEqual(back["restored"], ["b"])
        self.assertEqual(back["added"], [])          # 记录没被删过


class AvailabilityLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            models, "CACHE", Path(self.tmp.name) / "models.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        models.update_record(
            "deepinfer", "m", lambda rec: rec.update(status="ok"))

    def test_one_failure_is_not_enough_to_demote(self):
        # DeepInfer 的 403/404 可能只是后端实例没挂载 —— client 也把它判为
        # retryable。单次失败就下架会把一个健康模型误杀。
        record = models.note_unavailable("deepinfer", "m", "403")
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["unavailable_streak"], 1)

    def test_two_failures_demote_and_remember_the_old_status(self):
        models.note_unavailable("deepinfer", "m", "403")
        record = models.note_unavailable("deepinfer", "m", "403 again")
        self.assertEqual(record["status"], "unavailable")
        self.assertEqual(record["status_before_unavailable"], "ok")
        self.assertTrue(record["unavailable_at"])

    def test_a_real_success_restores_it(self):
        models.note_unavailable("deepinfer", "m", "403")
        models.note_unavailable("deepinfer", "m", "403")
        record = models.note_available("deepinfer", "m")
        self.assertEqual(record["status"], "ok")
        self.assertNotIn("unavailable_streak", record)
        self.assertNotIn("unavailable_at", record)

    def test_success_path_only_writes_when_something_is_marked(self):
        with mock.patch.object(models, "note_available") as note:
            models.clear_unavailable_if_marked("deepinfer", "m")
            note.assert_not_called()
        models.note_unavailable("deepinfer", "m", "403")
        with mock.patch.object(models, "note_available") as note:
            models.clear_unavailable_if_marked("deepinfer", "m")
            note.assert_called_once_with("deepinfer", "m")

    def test_agent_learns_from_both_outcomes(self):
        from core import agent as A                   # noqa: PLC0415
        from core import client                       # noqa: PLC0415

        agent = A.Agent.__new__(A.Agent)
        agent.gateway, agent.model = "deepinfer", "m"
        # 无关的错误不该影响可用性判断
        agent._note_route_outcome(
            client.APIError("boom", kind="rate_limit"))
        self.assertEqual(models.get("deepinfer", "m")["status"], "ok")
        error = client.APIError("gone", kind="model_unavailable")
        self.assertIsNone(agent._note_route_outcome(error))
        self.assertEqual(
            agent._note_route_outcome(error), "deepinfer/m")
        self.assertEqual(models.get("deepinfer", "m")["status"], "unavailable")
        agent._note_route_outcome()                   # 一次成功就翻回来
        self.assertEqual(models.get("deepinfer", "m")["status"], "ok")


class PolicyTests(unittest.TestCase):
    def test_defaults_and_bad_values(self):
        self.assertEqual(
            CFG.catalog_policy({}),
            {"enabled": True, "ttl_seconds": 24 * 3600})
        self.assertFalse(CFG.catalog_policy(
            {"catalog_refresh": False})["enabled"])
        self.assertEqual(
            CFG.catalog_policy({"catalog_ttl_hours": 6})["ttl_seconds"],
            6 * 3600)
        for bad in ("x", None, 0, -3):
            self.assertEqual(
                CFG.catalog_policy(
                    {"catalog_ttl_hours": bad})["ttl_seconds"], 24 * 3600)


class SessionRefreshTests(unittest.TestCase):
    """启动时的后台刷新：该跑才跑，跑完只在有变化时吭声。"""

    def make_session(self):
        import zylab                                  # noqa: PLC0415
        session = zylab.Session.__new__(zylab.Session)
        session.cfg = {}
        session._catalog_notices = []
        session._catalog_thread = None
        session._catalog_checked_at = 0.0
        session.route_selection = lambda: ("m", "deepinfer")
        return session, zylab

    def test_skipped_when_fresh_or_disabled(self):
        session, _ = self.make_session()
        with mock.patch.object(models, "catalog_is_stale", return_value=False):
            self.assertIsNone(session.start_catalog_refresh())
        session.cfg = {"catalog_refresh": False}
        with mock.patch.object(models, "catalog_is_stale", return_value=True):
            self.assertIsNone(session.start_catalog_refresh())

    def test_stale_catalog_refreshes_and_queues_only_real_changes(self):
        session, zylab = self.make_session()
        quiet = {"count": 3, "gateway": "deepinfer", "added": [],
                 "delisted": [], "restored": [], "changed": False}
        loud = dict(quiet, changed=True, added=["agents-a1"])
        for result, expected in ((quiet, 0), (loud, 1)):
            session._catalog_notices = []
            session._catalog_thread = None
            with (mock.patch.object(models, "catalog_is_stale",
                                    return_value=True),
                  mock.patch.object(zylab.M, "refresh_catalog",
                                    return_value=result)):
                thread = session.start_catalog_refresh()
                thread.join(timeout=5)
            self.assertEqual(len(session.drain_catalog_events()), expected)

    def test_turn_boundary_rechecks_but_is_throttled(self):
        """会话可能开好几天：只在启动时刷一次等于没刷。turn 边界顺带判断，
        但两次判断之间有内存节流，免得每个 turn 都去读 90 KB 的 models.json。"""
        session, zylab = self.make_session()
        with mock.patch.object(session, "start_catalog_refresh") as start:
            session.maybe_refresh_catalog()
            start.assert_called_once()
            session.maybe_refresh_catalog()           # 立刻再来一次：被节流挡住
            start.assert_called_once()
            # 节流窗口过去之后放行
            session._catalog_checked_at -= zylab.CATALOG_CHECK_SECONDS + 1
            session.maybe_refresh_catalog()
            self.assertEqual(start.call_count, 2)

    def test_gateway_switch_clears_the_throttle(self):
        session, zylab = self.make_session()
        session._catalog_checked_at = 12345.0
        with mock.patch.object(zylab, "_warn_if_no_key"):
            zylab._after_gateway_switch(session, "boyue")
        self.assertEqual(session._catalog_checked_at, 0.0)

    def test_gateway_failure_never_reaches_the_session(self):
        session, zylab = self.make_session()
        with (mock.patch.object(models, "catalog_is_stale", return_value=True),
              mock.patch.object(zylab.M, "refresh_catalog",
                                side_effect=RuntimeError("网关不通"))):
            session.start_catalog_refresh().join(timeout=5)
        self.assertEqual(session.drain_catalog_events(), [])


class NoticeTests(unittest.TestCase):
    def test_notice_stays_silent_without_changes(self):
        import zylab                                  # noqa: PLC0415
        self.assertEqual(zylab._catalog_change_notice(
            {"gateway": "deepinfer", "changed": False}), "")
        text = zylab._catalog_change_notice({
            "gateway": "deepinfer", "changed": True,
            "added": ["agents-a1"], "delisted": ["deepseek-v4-pro"],
            "restored": []})
        self.assertIn("新增 agents-a1", text)
        self.assertIn("下架 deepseek-v4-pro", text)
        self.assertIn("/model check", text)


if __name__ == "__main__":
    unittest.main()


class TransportRefusalTests(unittest.TestCase):
    """本地传输策略拒发 ≠ 模型不可用：请求根本没出这台机器。

    2026-09-08 实测：非交互脚本里 probe boyue（明文 HTTP 未授权），两个健康
    席位被一起打成 status=error，席位解析随即把它们排除 —— 一条本地策略把
    远端能力表改坏了。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            models, "CACHE", Path(self.tmp.name) / "models.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refusal_keeps_the_previous_status_and_marks_the_block(self):
        from core import client                       # noqa: PLC0415

        models.update_record("boyue", "m", lambda rec: rec.update(
            status="ok", supports_tools=True))
        refusal = client.APIError(
            "已拒绝明文 HTTP provider 传输", kind="insecure_transport")
        with mock.patch.object(models, "_one_shot", side_effect=refusal):
            record = models.probe("boyue", "m")
        self.assertEqual(record["status"], "ok")      # 能力结论不动
        self.assertTrue(record["supports_tools"])
        self.assertEqual(record["probe_blocked"], "insecure_transport")
        self.assertIsNone(record.get("error"))

    def test_a_real_api_error_still_demotes(self):
        from core import client                       # noqa: PLC0415

        models.update_record("boyue", "m", lambda rec: rec.update(status="ok"))
        with mock.patch.object(
                models, "_one_shot",
                side_effect=client.APIError("gone", kind="model_unavailable")):
            record = models.probe("boyue", "m")
        self.assertEqual(record["status"], "error")
