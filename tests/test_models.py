"""模型能力 probe 的计时、参数兼容与结构化错误回归。"""
import contextlib
import io
import os
import sys
import urllib.error
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import client, model_health, models, store


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_patch = mock.patch.object(
            models, "CACHE", Path(self.tmp.name) / "models.json")
        self.cache_patch.start()
        store.configure_metrics(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        store.reset_metrics_configuration()
        self.cache_patch.stop()
        self.tmp.cleanup()

    def test_one_shot_reports_true_first_delta_and_full_response(self):
        class FixedPhases:
            def snapshot(self):
                return [{
                    "attempt": 1,
                    "queued_at": 9.9,
                    "started_at": 10.0,
                    "headers_at": 10.2,
                    "first_delta_at": 10.5,
                    "last_delta_at": 11.7,
                    "ended_at": 12.0,
                }]

        seen = {}

        def fake_stream(_model, _messages, **kwargs):
            seen.update(kwargs)
            yield {"t": "text", "v": "ok"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        with mock.patch.object(
                models.client, "RequestPhases", return_value=FixedPhases()), \
                mock.patch.object(
                    models.client, "stream_chat", side_effect=fake_stream):
            calls, text, timing = models._one_shot(
                "model", temperature=0.3, timeout=9, gateway="deepinfer")

        self.assertEqual(calls, [])
        self.assertEqual(text, "ok")
        self.assertEqual(timing["headers_s"], 0.2)
        self.assertEqual(timing["first_delta_s"], 0.5)
        self.assertEqual(timing["response_s"], 2.0)
        self.assertFalse(seen["temperature_fallback"])
        self.assertEqual(seen["temperature"], 0.3)

    def test_probe_observes_temperature_rejection_then_retries_without_it(self):
        temperatures = []

        def fake_stream(_model, _messages, **kwargs):
            phases = kwargs["phases"]
            phases.mark("queued", 1)
            phases.mark("started", 1)
            temperatures.append(kwargs.get("temperature"))
            if kwargs.get("temperature") is not None:
                phases.mark("ended", 1)
                raise client.APIError(
                    "HTTP 400: `temperature` is deprecated",
                    kind="http", status=400, code="invalid_parameter")
            phases.mark("headers", 1)
            phases.mark("first_delta", 1)
            phases.mark("last_delta", 1)
            phases.mark("ended", 1)
            yield {"t": "tool", "v": [{"id": "call-1"}]}
            yield {"t": "done", "reason": "tool_calls", "usage": {}}

        with mock.patch.object(
                models.client, "stream_chat", side_effect=fake_stream):
            record = models.probe("deepinfer", "model", timeout=9)

        self.assertEqual(temperatures, [0.3, None])
        self.assertEqual(record["status"], "ok")
        self.assertTrue(record["supports_tools"])
        self.assertFalse(record["supports_temperature"])
        self.assertTrue(record["temperature_retried"])
        self.assertEqual(record["probe_attempts"], 2)
        self.assertEqual(record["probe_timing_version"], 2)
        self.assertIsNotNone(record["first_token_s"])
        self.assertIsNotNone(record["response_s"])

    def test_probe_caches_structured_provider_error(self):
        def fake_stream(_model, _messages, **kwargs):
            phases = kwargs["phases"]
            phases.mark("queued", 1)
            phases.mark("started", 1)
            phases.mark("ended", 1)
            raise client.APIError(
                "model unavailable", kind="permission", status=403,
                code="model_not_available", request_id="req-123",
                gateway="boyue", model="missing", retryable=True)
            yield  # pragma: no cover - 让函数保持 generator 形状

        with mock.patch.object(
                models.client, "stream_chat", side_effect=fake_stream):
            record = models.probe("boyue", "missing", timeout=9)

        self.assertEqual(record["status"], "error")
        self.assertEqual(record["http_status"], 403)
        self.assertEqual(record["error_code"], "model_not_available")
        self.assertEqual(record["provider_request_id"], "req-123")
        self.assertEqual(record["error_kind"], "permission")
        self.assertTrue(record["retryable"])
        self.assertEqual(record["probe_attempts"], 1)
        self.assertIsNone(record["supports_tools"])

    def test_transient_probe_failure_preserves_prior_capability(self):
        models.update_record(
            "boyue", "flaky",
            lambda rec: rec.update(
                gateway="boyue", id="flaky", supports_tools=True,
                supports_temperature=False,
                catalog_fingerprint="catalog-a"))

        def unavailable(*_args, **_kwargs):
            raise client.APIError(
                "HTTP 503: temporarily unavailable",
                kind="transient_http", status=503, retryable=True)
            yield  # pragma: no cover

        with mock.patch.object(
                models.client, "stream_chat", side_effect=unavailable):
            record = models.probe("boyue", "flaky", timeout=9)

        self.assertEqual(record["status"], "error")
        self.assertTrue(record["supports_tools"])
        self.assertFalse(record["supports_temperature"])
        health = store.metrics_facade().get_health("boyue", "flaky")
        self.assertEqual(
            health["probe_status"], model_health.PROBE_TRANSIENT_FAILURE)

    def test_explicit_unsupported_tools_is_stable_capability_rejection(self):
        models.update_record(
            "boyue", "chat-only",
            lambda rec: rec.update(
                gateway="boyue", id="chat-only",
                catalog_fingerprint="catalog-tools-v1"))

        def unsupported(*_args, **_kwargs):
            raise client.APIError(
                "HTTP 400: tool_choice is not supported by this model",
                kind="invalid_request", status=400, retryable=False)
            yield  # pragma: no cover

        with mock.patch.object(
                models.client, "stream_chat", side_effect=unsupported):
            record = models.probe("boyue", "chat-only", timeout=9)

        self.assertFalse(record["supports_tools"])
        self.assertEqual(record["error_kind"], "capability_rejection")
        health = store.metrics_facade().get_health("boyue", "chat-only")
        self.assertEqual(
            health["probe_status"],
            model_health.PROBE_CAPABILITY_REJECTION)
        self.assertFalse(store.metrics_facade().probe_decision(
            "boyue", "chat-only",
            catalog_fingerprint="catalog-tools-v1").due)

    def test_unavailable_model_named_tools_is_not_capability_rejection(self):
        error = client.APIError(
            "model tools-plus is not available",
            kind="http_error", status=404, retryable=False)
        self.assertFalse(models._is_tool_capability_rejection(error))

    def test_fingerprint_includes_gateway_and_protocol_revision(self):
        record = {"id": "m", "max_model_len": 1000}
        deep = models._catalog_fingerprint(
            record, gateway="deepinfer")
        boyue = models._catalog_fingerprint(
            record, gateway="boyue")
        self.assertNotEqual(deep, boyue)
        with mock.patch.dict(
                models.GATEWAY_CATALOG_REVISION, {"deepinfer": 99}):
            revised = models._catalog_fingerprint(
                record, gateway="deepinfer")
        self.assertNotEqual(deep, revised)

    def test_context_input_uses_compact_ascii_and_records_provider_usage(self):
        seen = {}

        def fake_stream(_model, messages, **kwargs):
            seen["content"] = messages[0]["content"]
            seen.update(kwargs)
            yield {
                "t": "done", "reason": "stop",
                "usage": {"prompt_tokens": 30_123},
            }

        with mock.patch.object(
                models.client, "stream_chat", side_effect=fake_stream):
            result = models._ctx_fits(
                "deepinfer", "model", 32_000, timeout=9, retries=0)

        self.assertTrue(result)
        self.assertEqual(result.prompt_tokens, 30_123)
        self.assertEqual(result.attempts, 1)
        self.assertIsNone(seen["temperature"])
        self.assertEqual(seen["retries"], 1)
        self.assertLess(len(seen["content"].encode()), 64_000)
        self.assertTrue(seen["content"].endswith("回复 OK 即可"))

    def test_context_probe_keeps_expanding_past_one_million_until_rejected(self):
        targets = []
        events = []

        def fake_fit(_gateway, _model, target):
            targets.append(target)
            if target < 3_000_000:
                return models.ContextAttempt(
                    True, prompt_tokens=target - 100, attempts=1)
            return models.ContextAttempt(
                False, stated_limit=2_500_000, attempts=1,
                error="maximum context length is 2500000 tokens")

        with mock.patch.object(models, "_ctx_fits", side_effect=fake_fit):
            lower, upper = models.probe_context(
                "deepinfer", "large-model", on_progress=events.append)

        self.assertEqual(targets, [
            32_000, 64_000, 128_000, 200_000, 256_000,
            512_000, 1_000_000, 2_000_000, 3_000_000,
        ])
        self.assertEqual(lower, 1_999_900)
        self.assertEqual(upper, 2_500_000)
        record = models.get("deepinfer", "large-model")
        self.assertEqual(record["context_probe_state"], "bounded")
        self.assertEqual(record["context_probe_rejected_at"], 3_000_000)
        self.assertEqual(record["context_probe_steps"], 9)
        self.assertEqual(record["context_probe_requests"], 9)
        self.assertEqual(events[0]["state"], "start")
        self.assertEqual(events[-1]["outcome"], "bounded")
        self.assertEqual(events[-1]["total"], 9)

    def test_context_probe_ceiling_is_a_floor_not_a_claimed_model_limit(self):
        events = []

        def accepts(_gateway, _model, target):
            return models.ContextAttempt(
                True, prompt_tokens=target - 10, attempts=1)

        with mock.patch.object(models, "_ctx_fits", side_effect=accepts):
            lower, upper = models.probe_context(
                "boyue", "unbounded-model", ladder=(32_000, 64_000),
                max_probe_tokens=128_000, on_progress=events.append)

        self.assertEqual((lower, upper), (127_990, None))
        record = models.get("boyue", "unbounded-model")
        self.assertEqual(record["context_probe_state"], "ceiling")
        self.assertEqual(record["context_source"], "probed-floor")
        self.assertIsNone(record["context_upper_bound"])
        self.assertIn("真实上下文上限尚未确认", record["context_probe_error"])
        self.assertEqual(events[-1]["outcome"], "ceiling")

    def test_context_probe_transient_blocker_never_becomes_context_limit(self):
        blocker = models.ContextProbeBlocked(
            "gateway timeout", attempts=3, kind="transient")
        with mock.patch.object(models, "_ctx_fits", side_effect=blocker):
            lower, upper = models.probe_context(
                "boyue", "flaky-model", ladder=(32_000,),
                max_probe_tokens=32_000)

        self.assertEqual((lower, upper), (None, None))
        record = models.get("boyue", "flaky-model")
        self.assertEqual(record["context_probe_state"], "blocked")
        self.assertEqual(record["context_probe_requests"], 3)
        self.assertNotIn("context", record)

    def test_context_probe_freshness_never_skips_capability_probe(self):
        models.update_record(
            "boyue", "needs-capability",
            lambda rec: rec.update(
                gateway="boyue", id="needs-capability", status="listed",
                supports_tools=None, catalog_fingerprint="catalog-a"))

        with mock.patch.object(
                models, "_ctx_fits",
                return_value=models.ContextAttempt(
                    True, prompt_tokens=31_000, attempts=1)):
            models.probe_context(
                "boyue", "needs-capability", ladder=(32_000,),
                max_probe_tokens=32_000)

        completed = {
            "gateway": "boyue", "id": "needs-capability",
            "status": "ok", "supports_tools": True,
        }
        with mock.patch.object(
                models, "probe", return_value=completed) as capability:
            result = models.probe_many(
                "boyue", ids=["needs-capability"], skip_probed=True)
        self.assertEqual(result, (1, 0, 0))
        capability.assert_called_once_with("boyue", "needs-capability")

    def test_context_body_limit_is_not_misclassified_as_model_rejection(self):
        def fake_stream(*_args, **_kwargs):
            raise client.APIError(
                "HTTP 413: max bytes to request body exceeded",
                kind="http", status=413, retryable=False)
            yield  # pragma: no cover

        with mock.patch.object(
                models.client, "stream_chat", side_effect=fake_stream):
            with self.assertRaises(models.ContextProbeBlocked) as caught:
                models._ctx_fits(
                    "boyue", "model", 32_000, timeout=9, retries=0)
        self.assertEqual(caught.exception.kind, "request_body")


class PickerTests(unittest.TestCase):
    def test_current_provider_families_and_non_chat_filter(self):
        self.assertEqual(models.family_of("gpt-5.6-sol"), "OpenAI")
        self.assertEqual(models.family_of("claude-sonnet-5"), "Anthropic")
        self.assertEqual(models.family_of("minimax-m2.7"), "MiniMax")
        self.assertIsNone(models.family_of("gpt-image-2"))
        self.assertIsNone(models.family_of("openai/o4-mini-deep-research"))

    def test_default_picker_keeps_deepinfer_and_curates_boyue(self):
        def row(gateway, model, *, status="ok", tools=True):
            return {
                "gateway": gateway,
                "id": model,
                "status": status,
                "supports_tools": tools,
            }

        candidates = [
            row("deepinfer", "minimax-m2.7"),
            row("deepinfer", "future-model"),
            row("deepinfer", "intern-s1", tools=False),
            row("boyue", "gpt-5.6-sol"),
            row("boyue", "claude-sonnet-5"),
            row("boyue", "claude-sonnet-4-6"),
            row("boyue", "claude-opus-5", status="error"),
            row("boyue", "qwen3.8-max"),
            row("boyue", "gpt-5.6-terra", tools=False),
            row("boyue", "qwen3.7-plus"),
            row("other", "gpt-5.6-sol"),
        ]

        selected = {
            (item["gateway"], item["id"])
            for item in models.default_picker_rows(candidates)
        }
        self.assertEqual(selected, {
            ("deepinfer", "minimax-m2.7"),
            ("deepinfer", "future-model"),
            ("deepinfer", "intern-s1"),
            ("boyue", "gpt-5.6-sol"),
            ("boyue", "claude-sonnet-5"),
            ("boyue", "claude-sonnet-4-6"),
            ("boyue", "qwen3.8-max"),
        })

    def test_deepinfer_boyue_candidates_are_model_picker_defaults(self):
        expected = {
            "qwen3.6-35b-a3b",
            "deepseek-v4-flash", "deepseek-v4-flash-0731",
            "deepseek-v4-pro", "deepseek-v4-pro-0813",
            "glm-5.2", "glm-5.3",
            "kimi-k2.6", "kimi-k3",
            "qwen3.8-max",
        }
        self.assertEqual(
            set(models.BOYUE_DEEPINFER_CANDIDATE_MODELS), expected)
        self.assertEqual(
            len(models.BOYUE_DEEPINFER_CANDIDATE_MODELS), len(expected))

        rows = [{
            "gateway": "boyue",
            "id": model,
            "status": "ok",
            "supports_tools": True,
        } for model in expected]
        rows.append({
            "gateway": "boyue",
            "id": "qwen3.7-plus",
            "status": "ok",
            "supports_tools": True,
        })

        selected = {
            row["id"] for row in models.default_picker_rows(rows)
        }
        self.assertEqual(selected, expected)

    def test_workflow_lineup_uses_only_curated_flagships_and_leaves_gaps(self):
        def row(gateway, model, *, status="ok", tools=True):
            return {
                "gateway": gateway, "id": model,
                "status": status, "supports_tools": tools,
            }

        candidates = [
            row("boyue", "qwen3.8-max"),
            row("boyue", "qwen3.6-flash"),
            row("boyue", "glm-5.3"),
            row("deepinfer", "deepseek-v4-flash"),
            row("deepinfer", "deepseek-v4-pro-0813"),
            row("deepinfer", "minimax-m2.7", tools=False),
            row("deepinfer", "kimi-k3"),
            row("boyue", "claude-sonnet-5"),
            row("boyue", "gpt-5.6-terra"),
        ]

        selected = models.workflow_default_rows(candidates)

        self.assertEqual(
            [(item["seat"], item["id"]) for item in selected],
            [
                ("Kimi", "kimi-k3"),
                ("GLM", "glm-5.3"),
                ("DeepSeek", "deepseek-v4-pro-0813"),
                ("Qwen", "qwen3.8-max"),
            ])
        self.assertTrue(all(
            item["quality_tier"] == "flagship" for item in selected))
        self.assertNotIn(
            "deepseek-v4-flash", {item["id"] for item in selected})

    def test_workflow_lineup_skips_open_route_and_uses_curated_alias(self):
        rows = [
            {
                "gateway": "deepinfer", "id": "deepseek-v4-pro-0813",
                "status": "ok", "supports_tools": True,
            },
            {
                "gateway": "boyue", "id": "bailian/deepseek-v4-pro",
                "status": "ok", "supports_tools": True,
            },
        ]

        selected = models.workflow_default_rows(
            rows,
            route_allowed=lambda gateway, _model: gateway != "deepinfer")

        self.assertEqual(
            [(item["gateway"], item["id"]) for item in selected],
            [("boyue", "bailian/deepseek-v4-pro")])

    def test_workflow_glm_prefers_new_deepinfer_53_when_both_routes_are_healthy(self):
        rows = [
            {
                "gateway": "deepinfer", "id": "glm-5.3",
                "status": "ok", "supports_tools": True,
            },
            {
                "gateway": "boyue", "id": "glm-5.3",
                "status": "ok", "supports_tools": True,
            },
        ]

        selected = models.workflow_default_rows(rows)

        self.assertEqual(
            [(item["seat"], item["gateway"], item["id"])
             for item in selected],
            [("GLM", "deepinfer", "glm-5.3")])

    def test_workflow_seat_falls_back_to_next_verified_candidate(self):
        rows = [
            {
                "gateway": "deepinfer", "id": "deepseek-v4-pro-0813",
                "status": "error", "supports_tools": True,
            },
            {
                "gateway": "deepinfer", "id": "deepseek-v4-pro",
                "status": "ok", "supports_tools": True,
            },
        ]
        selected = models.workflow_default_rows(rows)
        self.assertEqual(
            [(item["seat"], item["id"]) for item in selected],
            [("DeepSeek", "deepseek-v4-pro")])
        routes = models.workflow_seat_routes("DeepSeek", rows)
        self.assertEqual([r["id"] for r in routes], ["deepseek-v4-pro"])
        self.assertEqual(models.workflow_seat_routes("Claude", rows), [],
                         "Claude 已移出席位池")



if __name__ == "__main__":
    unittest.main()


class ModelSeed(unittest.TestCase):
    """随仓库分发的能力表 seed。

    探测每档一次真实请求、厂商文档逐个翻，这批结果原本只存在用户态的
    ~/.zylab/models.json 里 —— 转交给别的研究者时会整个丢失，新 clone 的
    /model 看不到 context，压缩阈值退回保守默认。
    """

    def test_seed_ships_with_the_repo(self):
        self.assertTrue(models.SEED.is_file(), f"seed 必须随仓库分发：{models.SEED}")

    def test_seed_includes_deepinfer_glm53(self):
        record = models._seed_models().get("deepinfer/glm-5.3")
        self.assertIsNotNone(record)
        self.assertEqual(record["gateway"], "deepinfer")
        self.assertEqual(record["id"], "glm-5.3")
        self.assertEqual(record["status"], "ok")
        self.assertTrue(record["supports_tools"])

    def test_seed_carries_context_data(self):
        seeded = models._seed_models()
        self.assertGreater(len(seeded), 100)
        with_ctx = [r for r in seeded.values() if r.get("context")]
        self.assertGreater(len(with_ctx), 50, "seed 的价值就在 context")

    def test_seed_has_no_per_machine_fields(self):
        """别人机器的延迟、报错、request id 不是模型属性，不该分发。"""
        banned = {"error", "error_kind", "http_status", "error_code",
                  "provider_request_id", "first_token_s", "headers_s",
                  "response_s", "probe_total_s", "catalog_fingerprint"}
        for key, record in models._seed_models().items():
            leaked = banned & set(record)
            self.assertFalse(leaked, f"{key} 带了本机字段 {leaked}")

    def test_seed_excludes_test_pollution(self):
        """gateway=test 是当年 mk() fixture 事故的残留（AGENTS.md §3）。"""
        gateways = {r.get("gateway") for r in models._seed_models().values()}
        self.assertNotIn("test", gateways)

    def test_seed_carries_no_secrets(self):
        blob = models.SEED.read_text(encoding="utf-8")
        for needle in ("sk-", "Bearer", "Authorization"):
            self.assertNotIn(needle, blob)

    def test_local_measurement_beats_seed(self):
        """seed 是别人机器上的观测，本机测过就不该再被它影响。"""
        seed_key = next(iter(models._seed_models()))
        fake_user = {"version": models.CACHE_VERSION, "updated": None,
                     "models": {seed_key: {"id": "x", "context": 12345}}}
        with mock.patch.object(models, "_load_unlocked", lambda: fake_user):
            merged = models.load()["models"][seed_key]
        self.assertEqual(merged["context"], 12345)
        self.assertNotIn("seeded", merged, "本机记录不该被打上 seed 标记")

    def test_seed_never_written_into_user_cache(self):
        """transaction() 会把 _load_unlocked() 原样写回磁盘。

        seed 若混进那一层，182 条别人机器的观测会被永久烧进用户自己的
        models.json，之后再也分不清哪条是本机测的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models.json"
            with mock.patch.object(models, "CACHE", cache):
                self.assertEqual(models._load_unlocked()["models"], {},
                                 "写路径读到的必须是纯用户缓存")
                self.assertGreater(len(models.load()["models"]), 100,
                                   "读路径才合并 seed")

    def test_seeded_records_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(models, "CACHE", Path(tmp) / "models.json"):
                record = next(iter(models.load()["models"].values()))
        self.assertTrue(record.get("seeded"), "来自 seed 的记录必须可辨认")

    def test_missing_seed_is_not_an_error(self):
        with mock.patch.object(models, "SEED", Path("/nonexistent/seed.json")), \
             mock.patch.object(models, "_SEED_CACHE", None):
            self.assertEqual(models._seed_models(), {})


class ModelsCommandDegradation(unittest.TestCase):
    """`--models` 是新用户第一个会敲的命令，任何失败都不该是 traceback。

    2026-08-28 陌生 pod 实测：按 README 装好、key 写错一个字符，
    `zylab --models` 直接吐 urllib.error.HTTPError 的完整调用栈。
    """

    def _run(self, side_effect):
        import zylab
        buf = io.StringIO()
        argv = ["zylab.py", "--models"]
        with mock.patch.object(client, "list_models", side_effect=side_effect), \
             mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(buf):
            zylab.main()
        return buf.getvalue()

    def test_rejected_key_falls_back_to_local_table(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        out = self._run(err)
        self.assertIn("HTTP 401", out)
        self.assertIn("本地能力表", out, "被拒也要能浏览模型")
        self.assertNotIn("Traceback", out)

    def test_missing_key_falls_back_to_local_table(self):
        out = self._run(client.MissingKey("no key"))
        self.assertIn("本地能力表", out)
        self.assertIn("没有 key", out)

    def test_network_failure_falls_back_to_local_table(self):
        out = self._run(OSError("Network is unreachable"))
        self.assertIn("本地能力表", out)
        self.assertNotIn("Traceback", out)

    def test_fallback_names_its_source(self):
        """不能拿缓存冒充实时目录。"""
        out = self._run(client.MissingKey("no key"))
        self.assertIn("未查询网关", out)
