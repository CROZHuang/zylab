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

    def test_refresh_probes_only_what_is_new_or_failing(self):
        """刷新时的自动实测：本机测过能用的不重测；新出现的、seed 里别人测的要测。"""
        models.update_record("boyue", "known-ok", lambda rec: rec.update(
            gateway="boyue", id="known-ok", status="ok", supports_tools=True))
        models.update_record("boyue", "brand-new", lambda rec: rec.update(
            gateway="boyue", id="brand-new", status="listed"))
        seed = {"boyue/seeded-ok": {"gateway": "boyue", "id": "seeded-ok",
                                    "status": "ok", "supports_tools": True}}
        ids = ["known-ok", "brand-new", "seeded-ok"]
        with mock.patch.object(models, "_seed_models", return_value=seed):
            self.assertEqual(models.due_for_probe("boyue", ids, revalidate=False),
                             ["brand-new", "seeded-ok"])
            self.assertEqual(models.due_for_probe("boyue", ids), ids,
                             "--probe-all / probe_many 照旧按健康度重验")

    def test_a_successful_probe_clears_a_stale_transport_block(self):
        """2026-09-24 实跑：qwen3.8-max 实测成功，屏幕却说「未实测：明文 HTTP 未授权」——
        09-08 一次非交互探测留下的 probe_blocked 从来没被清掉。"""
        models.update_record("boyue", "qwen3.8-max", lambda rec: rec.update(
            gateway="boyue", id="qwen3.8-max", status="ok",
            probe_blocked="insecure_transport",
            probe_blocked_at="2026-09-08T00:00:00+00:00"))

        def fake_stream(*_args, **_kwargs):
            yield {"t": "tool", "v": [{"id": "c1", "name": "get_file",
                                       "args": "{}"}]}
            yield {"t": "done", "reason": "tool_calls", "usage": {}}

        with mock.patch.object(models.client, "stream_chat",
                               side_effect=fake_stream):
            record = models.probe("boyue", "qwen3.8-max")
        self.assertEqual(record["status"], "ok")
        self.assertIsNone(record.get("probe_blocked"))
        self.assertIsNone(record.get("probe_blocked_at"))

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
            for item in models.default_picker_rows(
                candidates, catalog_sizes={"boyue": 225})
        }
        # Boyue 每条产品线只留最新可用的一个：sonnet-5 在，sonnet-4-6 让位；
        # qwen 这一代有 max 与 plus 两条线，各留一个。
        self.assertEqual(selected, {
            ("deepinfer", "minimax-m2.7"),
            ("deepinfer", "future-model"),
            ("deepinfer", "intern-s1"),
            ("boyue", "gpt-5.6-sol"),
            ("boyue", "claude-sonnet-5"),
            ("boyue", "qwen3.8-max"),
            ("boyue", "qwen3.7-plus"),
        })

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


CATALOGS = Path(__file__).resolve().parent / "fixtures" / "model_catalogs_20260924.json"


class FlagshipsFromNameShape(unittest.TestCase):
    """/model 在大目录上列**偏好家族的最新旗舰** —— 由通用的名字解析决定，不按公司写死。

    历史：这里原来是一份手写的 id 名单（`BOYUE_DEFAULT_MODELS`），每个月都在过期 ——
    gpt-6-sol、claude-opus-5-5 进了 Boyue 目录，列表还停在 gpt-5.6、一个 Claude 都没有；
    同一份名单还决定实测谁，新旗舰连实测都轮不到。第二版改成按公司写的六条正则，
    用户一句话否掉：「万一哪一天我给了你别的公司的 api 呢？难道要我返回 cc 再修一遍？」

    所以这里的夹具有两类：**真实目录**（2026-09-24 三个网关的 /models 原样，
    不是从规则反推的例子），以及**一家谁也没写过规则的虚构公司**。
    """

    BOYUE_LISTED = 408

    @classmethod
    def setUpClass(cls):
        import json
        cls.catalogs = json.loads(CATALOGS.read_text(encoding="utf-8"))

    @staticmethod
    def row(gateway, model, *, status="ok", tools=True):
        return {"gateway": gateway, "id": model,
                "status": status, "supports_tools": tools}

    def shown(self, rows, gateway="boyue", size=BOYUE_LISTED, preferred=None):
        return {r["id"] for r in models.default_picker_rows(
                    rows, catalog_sizes={gateway: size}, preferred=preferred)
                if r["gateway"] == gateway}

    # ---- 真实目录 -----------------------------------------------------------
    def test_the_real_catalog_yields_what_a_person_would_pick(self):
        """408 条真实 Boyue 目录（假设全部实测可用）→ 偏好六家各自的最新旗舰 + 视觉线。"""
        rows = [self.row("boyue", m) for m in self.catalogs["boyue"]]
        self.assertEqual(self.shown(rows), {
            "gpt-6-sol", "gpt-6-luna", "gpt-6-astra",
            "claude-opus-5-5", "claude-sonnet-5",
            "deepseek-v4-pro", "deepseek-v4-flash-vision-exp",
            "kimi-k3",
            "glm-5.3", "glm-5v-turbo",
            "qwen3.8-max", "qwen3.7-plus", "qwen3-vl-plus"})

    def test_every_real_id_parses(self):
        for gateway, ids in self.catalogs.items():
            if gateway.startswith("_"):
                continue
            for model_id in ids:
                with self.subTest(gateway=gateway, model=model_id):
                    self.assertIsNotNone(models.parse_model_name(model_id))

    # ---- 别的公司：没人给它写过规则 ------------------------------------------
    def test_a_company_nobody_wrote_a_rule_for_is_handled_the_same_way(self):
        """用户：「万一哪一天我给了你别的公司的 api 呢？」—— 一家虚构的公司，
        只要按行业通行的写法起名，就和六家走同一套规则。"""
        rows = [self.row("boyue", m) for m in (
            "zeta-2-ultra", "zeta-3-pro", "zeta-3.5-pro", "zeta-3.5-mini",
            "zeta-3.5-pro-thinking", "zeta-3.5-pro-20260801", "zeta-3.5-vl")]
        self.assertEqual(self.shown(rows, preferred=["zeta"]),
                         {"zeta-3.5-pro", "zeta-3.5-vl"})

    def test_a_gateway_without_any_preferred_family_lists_every_family(self):
        """只接了别家的聚合网关：偏好一个都不在，就列所有家族，而不是给一张空表。"""
        rows = [self.row("openrouter", m) for m in (
            "mistralai/mistral-large-3", "mistralai/mistral-large-2",
            "x-ai/grok-4.7", "x-ai/grok-4.6", "meta-llama/llama-4-maverick")]
        self.assertEqual(self.shown(rows, "openrouter", size=300), {
            "mistralai/mistral-large-3", "x-ai/grok-4.7",
            "meta-llama/llama-4-maverick"})

    def test_preferences_are_data_not_code(self):
        """偏好来自 settings：写词干或公司名都行。"""
        rows = [self.row("boyue", m) for m in self.catalogs["boyue"]]
        self.assertEqual(self.shown(rows, preferred=["Anthropic"]),
                         {"claude-opus-5-5", "claude-sonnet-5"})
        self.assertEqual(self.shown(rows, preferred=["grok"]),
                         {"grok-4.7", "x-ai/grok-2-vision-1212"})

    def test_refresh_keeps_models_of_a_company_nobody_listed(self):
        """目录层以前按手写的家族白名单丢模型：接上 Mistral 的 key，一条都进不来。"""
        listing = [{"id": "mistral-large-3"}, {"id": "codestral-2508"},
                   {"id": "text-embedding-3-large"}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(models, "CACHE", Path(tmp) / "models.json"), \
                mock.patch.object(models.client, "list_models",
                                  return_value=listing):
            result = models.refresh_catalog("mistral")
        self.assertEqual(sorted(result["added"]),
                         ["codestral-2508", "mistral-large-3"],
                         "非对话模型（embedding）照旧不进目录")

    # ---- 新旧与产品线 --------------------------------------------------------
    def test_each_line_moves_to_its_newest_generation(self):
        """gpt-6 出来之后，这一家只看第 6 代：还没有 6 代的 terra 让位。"""
        rows = [self.row("boyue", m) for m in (
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-sol", "gpt-6-luna")]
        self.assertEqual(self.shown(rows), {"gpt-6-sol", "gpt-6-luna"})

    def test_families_release_independently(self):
        """claude-opus 与 claude-sonnet 各自发版：sonnet 先到 6，opus-5-5 仍在。"""
        rows = [self.row("boyue", m) for m in (
            "claude-opus-5", "claude-opus-5-5",
            "claude-sonnet-4-6", "claude-sonnet-6")]
        self.assertEqual(self.shown(rows), {"claude-opus-5-5", "claude-sonnet-6"})

    def test_a_lower_tier_never_hides_the_flagship_of_its_generation(self):
        rows = [self.row("boyue", m) for m in (
            "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4.1-flash",
            "glm-5.3", "glm-5.3-flash", "glm-5-turbo")]
        self.assertEqual(self.shown(rows), {"deepseek-v4-pro", "glm-5.3"})

    def test_the_newest_generation_wins_even_when_it_is_only_a_lower_tier(self):
        """钉住一个取舍：「最新一代」优先于「档位」。glm-5v-turbo 是 GLM 第 5 代唯一的
        视觉模型；先看档位就会列出更旧的 glm-4.6v，那正是用户抱怨的「不是最新」。"""
        rows = [self.row("boyue", m) for m in (
            "glm-4.5v", "glm-4.6v", "glm-5v-turbo", "glm-5.3")]
        self.assertEqual(self.shown(rows), {"glm-5.3", "glm-5v-turbo"})

    def test_vision_models_are_a_line_of_their_own(self):
        """用户：「glm 有个视觉模型的，那个要加上，deepseek 的也是」。视觉线与文本线
        各自取最新，互不压制：文本已到第 5 代，第 4 代的 glm-4.6v 仍是 GLM 最新的视觉版。"""
        rows = [self.row("boyue", m) for m in (
            "deepseek-v4-pro", "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp", "deepseek-v4-flash-vision-exp-responses",
            "glm-5.3", "glm-4.6v", "glm-4.5v")]
        self.assertEqual(self.shown(rows), {
            "deepseek-v4-pro", "deepseek-v4-flash-vision-exp",
            "glm-5.3", "glm-4.6v"})

    def test_every_preferred_company_gets_its_newest_flagship(self):
        rows = [self.row("boyue", m) for m in (
            "deepseek-v4-pro", "deepseek-v4-pro-0813",
            "deepseek-v4-flash", "deepseek-v4.1-flash",
            "glm-5.2", "glm-5.3", "kimi-k2.6", "kimi-k3", "kimi/kimi-k3",
            "qwen3.7-max", "qwen3.8-max", "qwen3.8-flash")]
        self.assertEqual(self.shown(rows), {
            "deepseek-v4-pro", "glm-5.3", "kimi-k3", "qwen3.8-max"})

    def test_a_non_preferred_company_is_not_listed(self):
        rows = [self.row("boyue", "MiniMax-M3"), self.row("boyue", "glm-5.3")]
        self.assertEqual(self.shown(rows), {"glm-5.3"})

    # ---- 可用性 --------------------------------------------------------------
    def test_a_broken_newest_falls_back_to_the_newest_that_works(self):
        """最新的那个实测失败时，列表给出上一个能用的 —— 不能让这家整个消失。

        现场就是这样：名单里的 claude-opus-5 / claude-sonnet-5 在 boyue 上实测 error，
        于是列表里**一个 Claude 都没有**。
        """
        rows = [self.row("boyue", "gpt-6-sol", status="error"),
                self.row("boyue", "gpt-5.6-sol")]
        self.assertEqual(self.shown(rows), {"gpt-5.6-sol"})

    def test_an_unprobed_newest_is_not_shown(self):
        """只列**实测过能用**的：刷新刚加入、还没实测的新旗舰要先测（见 auto-probe）。"""
        rows = [self.row("boyue", "gpt-6-sol", status="listed", tools=None),
                self.row("boyue", "gpt-5.6-sol")]
        self.assertEqual(self.shown(rows), {"gpt-5.6-sol"})

    # ---- 变体、快照、日期 ----------------------------------------------------
    def test_another_way_of_calling_a_model_is_never_a_flagship(self):
        rows = [self.row("boyue", m) for m in (
            "claude-opus-5-5-thinking", "kimi-k2.7-code", "gpt-oss-120b",
            "deepseek-v4-flash-responses", "glm-5.2-1m", "qwen3.8-27b-fp8",
            "nex-agi/nex-n2.5-pro:free", "gpt-5.5-high")]
        self.assertEqual(self.shown(rows, preferred=[]), set())

    def test_a_plain_name_beats_its_snapshot_and_its_preview(self):
        rows = [self.row("boyue", m) for m in (
            "gpt-6-sol", "gpt-6-sol-2026-09-01",
            "deepseek-v4-pro", "deepseek-v4-pro-beta",
            "qwen3.8-max", "qwen3.8-max-0902")]
        self.assertEqual(self.shown(rows),
                         {"gpt-6-sol", "deepseek-v4-pro", "qwen3.8-max"})

    def test_a_snapshot_stands_in_when_it_is_all_there_is(self):
        """豆包的型号全带日期（doubao-seed-2-1-pro-260628）：没有不带日期的别名时，
        快照就代表这个版本 —— 否则整家都会被筛空。"""
        rows = [self.row("volc", m) for m in (
            "doubao-seed-2-0-pro-260215", "doubao-seed-2-1-pro-260628",
            "doubao-seed-2-1-turbo-260628")]
        with mock.patch.dict(models.client.GATEWAYS, {"volc": {}}):
            self.assertEqual(self.shown(rows, "volc", size=80),
                             {"doubao-seed-2-1-pro-260628"})

    def test_a_date_is_never_read_as_a_version(self):
        """`claude-opus-20250929` 不能被当成「第 20250929 版」压过所有真版本；
        `qwen-plus-2025-01-25` 的 `01-25` 也不能被读成 1.25 版。"""
        rows = [self.row("boyue", m) for m in (
            "claude-opus-20250929", "claude-opus-5-5",
            "qwen-plus-2025-01-25", "qwen-plus-1220")]
        self.assertEqual(self.shown(rows), {"claude-opus-5-5"})

    # ---- 其它网关 ----------------------------------------------------------
    def test_an_official_vendor_gateway_is_listed(self):
        """用户往 keys.env 里加了官方 DeepSeek key，列表里却一条 DeepSeek 都没有：
        原来的 `default_picker_rows` 对 deepinfer / boyue 之外的网关一律 `continue`。

        夹具取自 2026-09-24 官方 /models 的**真实返回**（就这两条）。
        """
        rows = [self.row("deepseek", m) for m in self.catalogs["deepseek"]]
        self.assertEqual(self.shown(rows, "deepseek", size=2),
                         {"deepseek-flash", "deepseek-v4-pro"})

    def test_an_unknown_gateway_is_still_dropped(self):
        self.assertEqual(
            models.default_picker_rows([self.row("no-such-gw", "gpt-6-sol")]), [])

    def test_a_small_catalog_lists_everything_that_works(self):
        """小目录（DeepInfer 16 条）照旧全列：非偏好公司的、明确仅聊天的都在；
        没测过的、测挂的不冒充可用。偏好家族的筛选只用在「全列不现实」的大目录上。"""
        rows = [self.row("deepinfer", "minimax-m2.7"),
                self.row("deepinfer", "future-model"),
                self.row("deepinfer", "intern-s1", tools=False),
                self.row("deepinfer", "glm-5.3-flash", status="listed", tools=None),
                self.row("deepinfer", "kimi-k3", status="error")]
        self.assertEqual(self.shown(rows, "deepinfer", size=16),
                         {"minimax-m2.7", "future-model", "intern-s1"})

    def test_the_same_rows_are_curated_once_the_catalog_is_large(self):
        rows = [self.row("deepinfer", "minimax-m2.7"),
                self.row("deepinfer", "glm-5.3")]
        self.assertEqual(self.shown(rows, "deepinfer", size=16),
                         {"minimax-m2.7", "glm-5.3"})
        self.assertEqual(self.shown(rows, "deepinfer", size=300), {"glm-5.3"})

    # ---- 实测候选：刷新后自动实测谁 ----------------------------------------
    def test_probe_candidates_are_the_newest_two_per_line(self):
        """每条产品线实测最新两个：最新的坏了，列表才有退路可退。"""
        ids = ["gpt-5.5-sol", "gpt-5.6-sol", "gpt-6-sol",
               "claude-opus-5", "claude-opus-5-5", "claude-opus-4-8",
               "glm-5.1", "glm-5.2", "glm-5.3",
               "MiniMax-M3", "qwen3.7-plus"]
        self.assertEqual(set(models.flagship_candidates(ids)), {
            "gpt-6-sol", "gpt-5.6-sol",
            "claude-opus-5-5", "claude-opus-5",
            "glm-5.3", "glm-5.2", "qwen3.7-plus"})

    def test_probe_targets_skip_delisted_flagships_only_on_large_catalogs(self):
        """大目录：已下架的旗舰不再实测（它不会是「最新」）；小目录照旧全测 ——
        DeepInfer 上 kimi-k3-256k 不在目录里却一直能调。"""
        # seed（随仓库分发的别人机器上的观测）会在 load() 时并进来，这里只看本机记录。
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(models, "CACHE", Path(tmp) / "models.json"), \
                mock.patch.object(models, "SEED", Path(tmp) / "no-seed.json"), \
                mock.patch.object(models, "_SEED_CACHE", None):
            def put(gateway, model, listed):
                models.update_record(gateway, model, lambda rec: rec.update(
                    gateway=gateway, id=model, status="listed", listed=listed))
            put("boyue", "claude-fable-5-1", False)
            put("boyue", "claude-opus-5-5", True)
            for index in range(45):
                put("boyue", f"filler-{index}", True)
            put("deepinfer", "kimi-k3-256k", False)
            put("deepinfer", "glm-5.3", True)
            self.assertEqual(models.probe_targets("boyue"), ["claude-opus-5-5"])
            self.assertEqual(set(models.probe_targets("deepinfer")),
                             {"kimi-k3-256k", "glm-5.3"})

    def test_probe_candidates_on_the_real_catalog_stay_small(self):
        """首刷一次要实测的量：真实 408 条目录上是几十个，不是几百个。"""
        candidates = models.flagship_candidates(self.catalogs["boyue"])
        self.assertLessEqual(len(candidates), 30)
        self.assertIn("gpt-6-sol", candidates)
        self.assertIn("claude-opus-5-5", candidates)


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
