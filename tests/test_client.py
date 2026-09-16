"""SSE 流式解析测试。

这是最容易被网关行为变化打破、也最难手动回归的一处：
tool_calls 按 index 分片投递、name 只在首片出现、arguments 逐片拼接。
拼错了的症状是「模型调工具但参数残缺」，不看代码根本查不出来。
"""
import json
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import client, models




_metrics_patch = None
_models_cache_patch = None
_base_patches = []
_models_tmp = None
_key_patch = None
_gateway_previous = None


def setUpModule():
    # Parser/retry unit tests must never touch the developer's real HOME.
    global _metrics_patch, _models_cache_patch, _models_tmp, _key_patch
    global _gateway_previous
    # 虚构 key。这些用例都 mock 掉了 HTTP opener，却没人 mock api_key ——
    # 它们一直靠维护者机器上真实存在的 keys.env 才跑得起来，陌生 pod 上
    # clone 下来先跑测试就是 18 个 error（2026-08-28 bundle 实测）。
    # 顺带堵住更难看的一种可能：测试此前跑在**真** key 上，哪天某个 mock
    # 漏了就会打出真实请求、真实计费；虚构值让那种情况必然失败。
    _key_patch = mock.patch.dict(
        os.environ, {"DEEPINFER_API_KEY": "fictional-test-key",
                     "BOYUE_API_KEY": "fictional-test-key"})
    _key_patch.start()
    # The suite can be launched by zylab itself while the live parent uses
    # Boyue.  Parser tests that omit an explicit route require the secure,
    # synthetic DeepInfer default and must not inherit that live route.
    # 合成 endpoint。zylab 不预置网关地址，所以不声明的话这些用例会在
    # require_secure_transport 处全部撞 base_not_configured —— 那是正确行为，
    # 但这些用例要验的是 SSE 解析，不是配置缺失。https 以免撞明文传输闸。
    _gateway_previous = client.GATEWAY
    _base_patches.append(_gw_patch := mock.patch.dict(
        client.GATEWAYS["deepinfer"], {"base": "https://synthetic.invalid/v1"}))
    _gw_patch.start()
    client.set_gateway("deepinfer")
    _models_tmp = tempfile.TemporaryDirectory()
    _models_cache_patch = mock.patch.object(
        models, "CACHE", Path(_models_tmp.name) / "models.json")
    _models_cache_patch.start()
    _metrics_patch = mock.patch.object(
        client, "_default_metrics_facade", return_value=None)
    _metrics_patch.start()


def tearDownModule():
    while _base_patches:
        _base_patches.pop().stop()
    if _gateway_previous is not None:
        client.set_gateway(_gateway_previous)
    if _key_patch is not None:
        _key_patch.stop()
    if _metrics_patch is not None:
        _metrics_patch.stop()
    if _models_cache_patch is not None:
        _models_cache_patch.stop()
    if _models_tmp is not None:
        _models_tmp.cleanup()


class GatewayDefaults(unittest.TestCase):
    def test_builtin_profiles_ship_no_endpoint_address(self):
        """闸门：网关地址是部署事实，不能回到代码里。

        内置 profile 只提供名字和 key 变量名；谁想加地址，加进配置。
        """
        for name, cfg in client.BUILTIN_GATEWAYS.items():
            with self.subTest(gateway=name):
                self.assertNotIn("base", cfg)
                self.assertTrue(cfg["keys"])

    def test_base_resolves_from_env_then_settings(self):
        with mock.patch.dict(
                os.environ, {"ZYLAB_BASE_BOYUE": "https://env.invalid/v1"}):
            self.assertEqual(
                client.resolve_base("boyue"), "https://env.invalid/v1")
        with mock.patch.dict(client.GATEWAYS["boyue"],
                             {"base": "https://cfg.invalid/v1"}):
            self.assertEqual(
                client.resolve_base("boyue"), "https://cfg.invalid/v1")

    def test_unconfigured_gateway_reports_empty_base_with_a_hint(self):
        # 不能直接断言 resolve_base 为空：跑测试的机器上可能**已经**配了网关，
        # 那样这条会因为「配置正确」而变红。要验的是没配时的行为。
        with mock.patch.dict(client.GATEWAYS, {"boyue": {"keys": ()}}), \
                mock.patch.object(client, "_BASE_OVERRIDE", None):
            self.assertEqual(client.resolve_base("boyue"), "")
        hint = client.base_missing_hint("boyue")
        self.assertIn("ZYLAB_BASE_BOYUE", hint)
        self.assertIn("zylab init", hint)

    def test_client_tests_use_temporary_model_cache(self):
        self.assertEqual(models.CACHE.parent, Path(_models_tmp.name))


class InsecureTransportGate(unittest.TestCase):
    def setUp(self):
        self.previous_policy = client.configure_transport_policy(
            allow_insecure_http=False, authorizer=None)

    def tearDown(self):
        client.restore_transport_policy(self.previous_policy)

    def test_plain_http_is_rejected_before_key_or_socket(self):
        route = client.GatewayRoute(
            "boyue", "http://example.invalid/v1", ("BOYUE_API_KEY",))
        with mock.patch.object(client, "api_key") as key, \
                mock.patch.object(client._OPENER, "open") as opened:
            with self.assertRaises(client.APIError) as raised:
                list(client.stream_chat(
                    "m", [{"role": "user", "content": "synthetic"}],
                    route=route, retries=1, metrics=None))
        self.assertEqual(raised.exception.kind, "insecure_transport")
        key.assert_not_called()
        opened.assert_not_called()

    def test_exact_configured_http_endpoint_is_persistently_allowed(self):
        authorize = mock.Mock(return_value=False)
        client.configure_transport_policy(
            authorizer=authorize,
            allowed_insecure_endpoints=["HTTP://TRUSTED.INVALID/v1/"])
        trusted = client.GatewayRoute(
            "boyue", "http://trusted.invalid/v1", ("BOYUE_API_KEY",))
        neighbor = client.GatewayRoute(
            "boyue", "http://trusted.invalid/v2", ("BOYUE_API_KEY",))

        request = client.require_secure_transport(trusted, model="m")
        self.assertEqual(request["base"], "http://trusted.invalid/v1")
        authorize.assert_not_called()
        with self.assertRaises(client.APIError) as raised:
            client.require_secure_transport(neighbor, model="m")
        self.assertEqual(raised.exception.kind, "insecure_transport")
        self.assertEqual(authorize.call_count, 1)

    def test_authorizer_receives_normalized_bound_scope_before_socket(self):
        route = client.GatewayRoute(
            "boyue", "HTTP://EXAMPLE.INVALID/v1/", ("BOYUE_API_KEY",))
        authorize = mock.Mock(return_value=True)
        client.configure_transport_policy(authorizer=authorize)
        trace = {
            "session_id": "session-a",
            "cwd": "/tmp/repo-a/../repo-a",
            "raw": {"purpose": "chat"},
        }
        with mock.patch.object(client, "api_key", return_value="synthetic"), \
                mock.patch.object(
                    client._OPENER, "open",
                    return_value=FakeResponse(["data: [DONE]\n"])) as opened:
            list(client.stream_chat(
                "m", [{"role": "user", "content": "synthetic"}],
                route=route, retries=1, metrics=None, trace_context=trace))
        request = authorize.call_args.args[0]
        self.assertEqual(request["base"], "http://example.invalid/v1")
        self.assertEqual(request["repo"], "/tmp/repo-a")
        self.assertEqual(request["session"], "session-a")
        self.assertEqual(request["gateway"], "boyue")
        opened.assert_called_once()

    def test_child_transport_scope_inherits_parent_session(self):
        route = client.GatewayRoute(
            "boyue", "http://example.invalid/v1", ("BOYUE_API_KEY",))
        request = client.transport_request(route, trace_context={
            "session_id": "child-session",
            "transport_session_id": "parent-session",
            "cwd": "/tmp/repo-a",
        })

        self.assertEqual(request["session"], "parent-session")

    def test_https_never_asks_insecure_transport_authorizer(self):
        route = client.GatewayRoute(
            "deepinfer", "https://example.invalid/v1", ("TEST_KEY",))
        authorize = mock.Mock(return_value=False)
        client.configure_transport_policy(authorizer=authorize)
        with mock.patch.object(client, "api_key", return_value="synthetic"), \
                mock.patch.object(
                    client._OPENER, "open",
                    return_value=FakeResponse(["data: [DONE]\n"])):
            list(client.stream_chat(
                "m", [{"role": "user", "content": "synthetic"}],
                route=route, retries=1, metrics=None))
        authorize.assert_not_called()


class FakeResponse:
    """假装成 urlopen 返回的对象：按行迭代 SSE。"""

    def __init__(self, lines):
        self._lines = [l.encode() if isinstance(l, str) else l for l in lines]

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sse(obj):
    return f"data: {json.dumps(obj)}\n"


def chunk(**delta):
    return {"choices": [{"delta": delta, "finish_reason": None}]}


def run_stream(lines):
    with mock.patch.object(client._OPENER, "open",
                           return_value=FakeResponse(lines)):
        return list(client.stream_chat("m", [{"role": "user", "content": "x"}]))


class TextStreaming(unittest.TestCase):
    def test_text_deltas_are_yielded_in_order(self):
        evs = run_stream([sse(chunk(content="他")), sse(chunk(content="好")),
                          "data: [DONE]\n"])
        text = "".join(e["v"] for e in evs if e["t"] == "text")
        self.assertEqual(text, "他好")

    def test_reasoning_content_is_separate_event(self):
        evs = run_stream([sse(chunk(reasoning_content="想")),
                          sse(chunk(content="答")), "data: [DONE]\n"])
        kinds = [e["t"] for e in evs if e["t"] in ("reasoning", "text")]
        self.assertEqual(kinds, ["reasoning", "text"])

    def test_malformed_json_line_is_skipped_not_fatal(self):
        evs = run_stream(["data: {not json\n", sse(chunk(content="ok")),
                          "data: [DONE]\n"])
        self.assertEqual("".join(e["v"] for e in evs if e["t"] == "text"), "ok")

    def test_non_data_lines_ignored(self):
        evs = run_stream([": keep-alive\n", "\n", sse(chunk(content="a")),
                          "data: [DONE]\n"])
        self.assertEqual("".join(e["v"] for e in evs if e["t"] == "text"), "a")


class ToolCallAssembly(unittest.TestCase):
    """OpenAI 按 index 分片投递 tool_calls，必须按 index 累加而非到达顺序。"""

    def test_arguments_are_concatenated_across_chunks(self):
        lines = [
            sse(chunk(tool_calls=[{"index": 0, "id": "c1",
                                   "function": {"name": "read_file",
                                                "arguments": '{"pa'}}])),
            sse(chunk(tool_calls=[{"index": 0,
                                   "function": {"arguments": 'th": "a.py"}'}}])),
            "data: [DONE]\n"]
        calls = next(e["v"] for e in run_stream(lines) if e["t"] == "tool")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {"path": "a.py"})

    def test_parallel_calls_kept_separate_by_index(self):
        lines = [
            sse(chunk(tool_calls=[{"index": 0, "id": "a",
                                   "function": {"name": "f0", "arguments": "{}"}}])),
            sse(chunk(tool_calls=[{"index": 1, "id": "b",
                                   "function": {"name": "f1", "arguments": "{}"}}])),
            "data: [DONE]\n"]
        calls = next(e["v"] for e in run_stream(lines) if e["t"] == "tool")
        self.assertEqual([c["function"]["name"] for c in calls], ["f0", "f1"])

    def test_out_of_order_chunks_still_assemble_by_index(self):
        """分片可能乱序到达 —— 按 index 归位，不能按到达顺序拼。"""
        lines = [
            sse(chunk(tool_calls=[{"index": 1, "id": "b",
                                   "function": {"name": "second", "arguments": '{"y'}}])),
            sse(chunk(tool_calls=[{"index": 0, "id": "a",
                                   "function": {"name": "first", "arguments": '{"x'}}])),
            sse(chunk(tool_calls=[{"index": 1, "function": {"arguments": '": 2}'}}])),
            sse(chunk(tool_calls=[{"index": 0, "function": {"arguments": '": 1}'}}])),
            "data: [DONE]\n"]
        calls = next(e["v"] for e in run_stream(lines) if e["t"] == "tool")
        self.assertEqual([c["function"]["name"] for c in calls], ["first", "second"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"x": 1})
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"y": 2})

    def test_missing_id_gets_synthesized(self):
        """有的网关不给 id。必须补一个，否则 tool_result 无法配对。"""
        lines = [sse(chunk(tool_calls=[{"index": 0,
                                        "function": {"name": "f", "arguments": "{}"}}])),
                 "data: [DONE]\n"]
        calls = next(e["v"] for e in run_stream(lines) if e["t"] == "tool")
        self.assertTrue(calls[0]["id"])

    def test_empty_arguments_defaults_to_object(self):
        lines = [sse(chunk(tool_calls=[{"index": 0, "id": "a",
                                        "function": {"name": "f"}}])),
                 "data: [DONE]\n"]
        calls = next(e["v"] for e in run_stream(lines) if e["t"] == "tool")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {})


class UsageAndFinish(unittest.TestCase):
    def test_usage_is_carried_to_done_event(self):
        lines = [sse(chunk(content="x")),
                 sse({"choices": [], "usage": {"prompt_tokens": 7,
                                               "completion_tokens": 2}}),
                 "data: [DONE]\n"]
        done = next(e for e in run_stream(lines) if e["t"] == "done")
        self.assertEqual(done["usage"]["prompt_tokens"], 7)

    def test_finish_reason_captured(self):
        lines = [sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
                 "data: [DONE]\n"]
        done = next(e for e in run_stream(lines) if e["t"] == "done")
        self.assertEqual(done["reason"], "tool_calls")


class Cancellation(unittest.TestCase):
    def test_cancel_event_aborts_stream(self):
        import threading
        ev = threading.Event(); ev.set()
        with mock.patch.object(client._OPENER, "open",
                               return_value=FakeResponse([sse(chunk(content="x"))])):
            with self.assertRaises(client.Interrupted):
                list(client.stream_chat("m", [{"role": "user", "content": "x"}],
                                        cancel=ev))

    def test_handle_closes_bound_response_once(self):
        class Probe:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        handle = client.CancellationHandle()
        response = Probe()
        handle.bind(response)
        self.assertTrue(handle.cancel())
        self.assertFalse(handle.cancel())
        self.assertTrue(handle.is_set())
        self.assertEqual(response.close_calls, 1)
        self.assertIsNotNone(handle.requested_at)

    def test_cancel_before_bind_closes_late_response(self):
        class Probe:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        handle = client.CancellationHandle()
        handle.cancel()
        response = Probe()
        handle.bind(response)
        self.assertEqual(response.close_calls, 1)

    def test_pre_cancelled_handle_avoids_network_request(self):
        handle = client.CancellationHandle()
        handle.cancel()
        reservations = []
        with mock.patch.object(client._OPENER, "open") as opened:
            with self.assertRaises(client.Interrupted):
                list(client.stream_chat(
                    "m", [{"role": "user", "content": "x"}],
                    cancel=handle, retries=1,
                    before_attempt=reservations.append))
        opened.assert_not_called()
        self.assertEqual(reservations, [])

    def test_active_close_error_is_reported_as_interrupted(self):
        handle = client.CancellationHandle()
        phases = client.RequestPhases()

        class ClosingResponse:
            def __init__(self):
                self.close_calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                handle.cancel()
                raise OSError("socket closed")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def close(self):
                self.close_calls += 1

        response = ClosingResponse()
        with mock.patch.object(client._OPENER, "open", return_value=response):
            with self.assertRaises(client.Interrupted):
                list(client.stream_chat(
                    "m", [{"role": "user", "content": "x"}],
                    cancel=handle, phases=phases, retries=1))
        self.assertEqual(response.close_calls, 1)
        self.assertIsNotNone(phases.attempt(1)["ended_at"])



class ProviderWorkerTests(unittest.TestCase):
    def test_background_stream_yields_poll_then_events_in_order(self):
        import time

        def fake(*args, **kwargs):
            time.sleep(0.03)
            yield {"t": "text", "v": "a"}
            yield {"t": "done", "usage": {}, "reason": "stop"}

        with mock.patch.object(client, "stream_chat", side_effect=fake):
            events = list(client.stream_chat_background(
                "m", [], poll_interval=0.005, queue_size=2))
        self.assertIn("poll", [event["t"] for event in events])
        self.assertEqual(
            [event["t"] for event in events if event["t"] != "poll"],
            ["text", "done"])

    def test_background_stream_reraises_worker_error(self):
        def fake(*args, **kwargs):
            raise client.APIError("boom")
            yield  # pragma: no cover

        with mock.patch.object(client, "stream_chat", side_effect=fake):
            with self.assertRaisesRegex(client.APIError, "boom"):
                list(client.stream_chat_background(
                    "m", [], poll_interval=0.005))

    def test_closing_background_iterator_cancels_active_request(self):
        import time

        handle = client.CancellationHandle()

        def fake(*args, cancel=None, **kwargs):
            while not cancel.is_set():
                time.sleep(0.005)
            raise client.Interrupted("closed")
            yield  # pragma: no cover

        with mock.patch.object(client, "stream_chat", side_effect=fake):
            stream = client.stream_chat_background(
                "m", [], cancel=handle, poll_interval=0.005)
            self.assertEqual(next(stream)["t"], "poll")
            stream.close()
        self.assertTrue(handle.is_set())

    def test_worker_queue_is_bounded_and_close_unblocks_producer(self):
        import time

        worker = client.ProviderWorker(
            lambda: iter(range(1000)), queue_size=2).start()
        time.sleep(0.03)
        self.assertLessEqual(worker._queue.qsize(), 2)
        worker.close(timeout=0.5)
        self.assertFalse(worker.alive)


class RequestPhaseTracking(unittest.TestCase):
    def test_success_exposes_ordered_phase_boundaries(self):
        phases = client.RequestPhases()
        with mock.patch.object(
                client._OPENER, "open",
                return_value=FakeResponse([
                    sse(chunk(content="x")), "data: [DONE]\n"])):
            list(client.stream_chat(
                "m", [{"role": "user", "content": "x"}],
                phases=phases, retries=1))
        row = phases.attempt(1)
        fields = client.RequestPhases.FIELDS
        self.assertTrue(all(row[field] is not None for field in fields))
        observed = [row[field] for field in fields]
        self.assertEqual(observed, sorted(observed))

    def test_failed_connect_has_no_fake_headers_or_delta_time(self):
        phases = client.RequestPhases()
        with mock.patch.object(
                client._OPENER, "open", side_effect=OSError("down")):
            with self.assertRaises(client.APIError):
                list(client.stream_chat(
                    "m", [{"role": "user", "content": "x"}],
                    phases=phases, retries=1))
        row = phases.attempt(1)
        self.assertIsNotNone(row["queued_at"])
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["ended_at"])
        self.assertIsNone(row["headers_at"])
        self.assertIsNone(row["first_delta_at"])
        self.assertIsNone(row["last_delta_at"])


class StructuredErrorsAndRouting(unittest.TestCase):
    @staticmethod
    def http_error(status, payload):
        return client.urllib.error.HTTPError(
            "http://example.invalid", status, "error", {},
            io.BytesIO(json.dumps(payload).encode()))

    def test_model_not_available_403_is_retryable_not_key_or_route_guess(self):
        error = client._http_error(
            self.http_error(403, {"error": {
                "code": "model_not_available",
                "message": "Model is not available.",
                "request_id": "req-upstream",
                "type": "permission_error",
            }}),
            client.route_for("deepinfer"), "kimi-k3-256k")

        self.assertEqual(error.kind, "model_unavailable")
        self.assertTrue(error.retryable)
        self.assertEqual(error.status, 403)
        self.assertEqual(error.code, "model_not_available")
        self.assertEqual(error.request_id, "req-upstream")
        self.assertNotIn("key 错", str(error))
        self.assertNotIn("路由错", str(error))

    def test_generic_403_is_nonretryable_permission_error(self):
        error = client._http_error(
            self.http_error(403, {"error": {
                "code": "permission_denied", "message": "denied"}}),
            client.route_for("deepinfer"), "m")
        self.assertEqual(error.kind, "permission")
        self.assertFalse(error.retryable)

    def test_retry_keeps_original_route_when_global_gateway_changes(self):
        routes = []
        original = client.GATEWAY

        def flaky(*_args, route=None, **_kwargs):
            routes.append(route)
            if len(routes) == 1:
                client.set_gateway("boyue")
                raise client.APIError(
                    "temporarily unavailable", kind="model_unavailable",
                    gateway=route.name, retryable=True)
            yield {"t": "text", "v": "ok"}
            yield {"t": "done", "usage": {}, "reason": "stop"}

        try:
            with mock.patch.object(client, "_stream_once", side_effect=flaky), \
                 mock.patch.object(client, "_wait_retry"):
                events = list(client.stream_chat(
                    "m", [], gateway="deepinfer", retries=2))
        finally:
            client.set_gateway(original)

        self.assertEqual([route.name for route in routes],
                         ["deepinfer", "deepinfer"])
        self.assertIs(routes[0], routes[1])
        self.assertEqual(
            "".join(event.get("v", "") for event in events
                    if event["t"] == "text"), "ok")

    def test_before_attempt_counts_retries_and_can_block_before_wire(self):
        wire_calls = []
        reservations = []

        def flaky(*_args, route=None, **_kwargs):
            wire_calls.append(route.name)
            if len(wire_calls) == 1:
                raise client.APIError(
                    "temporarily unavailable", kind="model_unavailable",
                    gateway=route.name, retryable=True)
            yield {"t": "done", "usage": {}, "reason": "stop"}

        def guard(details):
            reservations.append(details)
            if len(reservations) == 2:
                raise RuntimeError("provider attempt budget exhausted")

        with mock.patch.object(client, "_stream_once", side_effect=flaky), \
             mock.patch.object(client, "_wait_retry"):
            with self.assertRaisesRegex(
                    RuntimeError, "attempt budget exhausted"):
                list(client.stream_chat(
                    "m", [], gateway="deepinfer", retries=2,
                    before_attempt=guard))

        self.assertEqual(wire_calls, ["deepinfer"])
        self.assertEqual(
            [item["attempt"] for item in reservations], [1, 2])
        self.assertTrue(all(
            item["gateway"] == "deepinfer" for item in reservations))


class TemperatureFallback(unittest.TestCase):
    def test_temperature_none_omits_the_field(self):
        seen = {}

        def fake_open(req, timeout=None):
            seen.update(json.loads(req.data))
            return FakeResponse(["data: [DONE]\n"])

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            list(client.stream_chat("m", [{"role": "user", "content": "x"}],
                                    temperature=None))
        self.assertNotIn("temperature", seen)

    def test_temperature_rejection_retries_without_it(self):
        """当前世代 Claude 废弃了 temperature —— 撞到就该自动去掉重发。"""
        calls = []

        def fake_open(req, timeout=None):
            body = json.loads(req.data)
            calls.append(body)
            if "temperature" in body:
                raise client.APIError("HTTP 400: `temperature` is deprecated")
            return FakeResponse([sse(chunk(content="ok")), "data: [DONE]\n"])

        with mock.patch.object(client, "_stream_once",
                               side_effect=client._stream_once):
            with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
                evs = list(client.stream_chat("m", [{"role": "user", "content": "x"}],
                                              temperature=0.3, retries=1))
        self.assertEqual(len(calls), 2, "应重发一次")
        self.assertNotIn("temperature", calls[1])
        self.assertEqual("".join(e["v"] for e in evs if e["t"] == "text"), "ok")

    def test_temperature_fallback_can_be_disabled_for_capability_probe(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(json.loads(req.data))
            raise client.APIError("HTTP 400: `temperature` is deprecated")

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            with self.assertRaisesRegex(client.APIError, "temperature"):
                list(client.stream_chat(
                    "m", [{"role": "user", "content": "x"}],
                    temperature=0.3, retries=1,
                    temperature_fallback=False))

        self.assertEqual(len(calls), 1)
        self.assertIn("temperature", calls[0])


class ProviderToolSchemaCompatibility(unittest.TestCase):
    def test_boyue_claude_omits_top_level_schema_combinators(self):
        schema = {
            "type": "function",
            "function": {
                "name": "subagent",
                "description": "synthetic",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "tasks": {"type": "array"},
                    },
                    "required": [],
                    "oneOf": [
                        {"required": ["task"]},
                        {"required": ["tasks"]},
                    ],
                    "allOf": [{"type": "object"}],
                    "anyOf": [{"required": ["task"]}],
                },
            },
        }
        seen = {}

        def fake_open(request, timeout=None):
            del timeout
            seen.update(json.loads(request.data))
            return FakeResponse(["data: [DONE]\n"])

        route = client.GatewayRoute(
            "boyue", "http://example.invalid/v1", ("BOYUE_API_KEY",))
        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            list(client._stream_once(
                "claude-sonnet-5",
                [{"role": "user", "content": "synthetic"}],
                tools=[schema], route=route))

        sent = seen["tools"][0]["function"]["parameters"]
        for keyword in ("oneOf", "allOf", "anyOf"):
            self.assertNotIn(keyword, sent)
            self.assertIn(keyword, schema["function"]["parameters"])

    def test_non_boyue_claude_routes_keep_canonical_schema(self):
        schema = {"type": "function", "function": {
            "name": "synthetic",
            "parameters": {"type": "object", "oneOf": [
                {"required": ["value"]}]},
        }}
        route = client.GatewayRoute(
            "deepinfer", "https://example.invalid/v1", ("TEST_KEY",))

        compatible = client._provider_tool_schemas(
            [schema], route, "claude-sonnet-5")

        self.assertIs(compatible[0], schema)
        self.assertIn(
            "oneOf", compatible[0]["function"]["parameters"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class KeyFileResolution(unittest.TestCase):
    """key 的查找链必须可移植，且**永远不进仓库**。

    转交内测时的实际形态：研究者的 pod 上没有维护者的
    维护者机器上那条绝对路径，旧代码写死了它，报错还让他往一个
    不存在的目录里写。
    """

    def test_no_key_material_in_repo(self):
        """路径可以硬编码，key 本体不行。"""
        src = Path(client.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sk-", src, "源码里不得出现任何 key 形态的字面量")

    def test_candidates_are_home_relative(self):
        with mock.patch.dict(os.environ, {"HOME": "/tmp/somepod"}, clear=False):
            os.environ.pop("ZYLAB_KEYS_FILE", None)
            os.environ.pop("ZYLAB_HOME", None)          # 显式改道优先于 HOME；这里验证的是 HOME 相对
            os.environ["ZYLAB_APP_ROOT"] = "/tmp/somepod"   # 没有便携目录
            found = client._key_files()
        self.assertTrue(found[0].startswith("/tmp/somepod"),
                        f"首选落点要跟着 HOME 走，实际 {found[0]}")

    def test_legacy_path_still_searched(self):
        """老安装的落点由 ZYLAB_LEGACY_KEYS_FILE 声明，不写死某台机器。"""
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "legacy-keys.env")
            open(legacy, "w").close()
            with mock.patch.dict(os.environ, {
                    "HOME": "/tmp/somepod",
                    "ZYLAB_LEGACY_KEYS_FILE": legacy}, clear=False):
                os.environ.pop("ZYLAB_KEYS_FILE", None)
                self.assertIn(legacy, client._key_files(),
                              "声明过的老路径要继续兜底")

    def test_explicit_override_wins(self):
        with mock.patch.dict(os.environ, {"ZYLAB_KEYS_FILE": "~/k.env"}):
            found = client._key_files()
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].startswith("~"), "必须展开 ~")

    def test_dedupes_when_home_collides(self):
        """某些 HOME 布局下后两条候选会重合，不该重复搜索。"""
        with mock.patch.dict(os.environ, {"HOME": "/tmp/collide"}, clear=False):
            os.environ.pop("ZYLAB_KEYS_FILE", None)
            found = client._key_files()
        self.assertEqual(len(found), len(set(found)))

    def test_reads_from_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = os.path.join(tmp, "keys.env")
            with open(key_file, "w", encoding="utf-8") as handle:
                handle.write("DEEPINFER_API_KEY=fictional-test-value\n")
            with mock.patch.dict(os.environ, {"ZYLAB_KEYS_FILE": key_file}):
                for name in ("DEEPINFER_API_KEY",):
                    os.environ.pop(name, None)
                self.assertEqual(client.api_key(client.route_for()),
                                 "fictional-test-value")

    def test_env_var_beats_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = os.path.join(tmp, "keys.env")
            with open(key_file, "w", encoding="utf-8") as handle:
                handle.write("DEEPINFER_API_KEY=from-file\n")
            with mock.patch.dict(os.environ, {"ZYLAB_KEYS_FILE": key_file,
                                              "DEEPINFER_API_KEY": "from-env"}):
                self.assertEqual(client.api_key(client.route_for()), "from-env")

    def test_missing_key_error_offers_both_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"ZYLAB_KEYS_FILE": os.path.join(tmp, "nope.env")}):
                for name in ("DEEPINFER_API_KEY",):
                    os.environ.pop(name, None)
                with self.assertRaises(client.MissingKey) as caught:
                    client.api_key(client.route_for())
        message = str(caught.exception)
        self.assertIn("export DEEPINFER_API_KEY", message)
        self.assertIn("chmod 600", message, "写文件的补救必须自带权限收紧")

    def test_missing_key_is_catchable_not_systemexit(self):
        """这条是整改的核心，不能退回去。

        原本 api_key() 直接 sys.exit，抛的 SystemExit 继承 BaseException，
        于是 model_limit() 里那句 `except Exception: pass` 接不住 ——
        一个**可选的**模型目录查询把整个进程在 Agent 构造阶段带走，
        /doctor、/model、/resume 全都进不去。
        """
        self.assertTrue(issubclass(client.MissingKey, Exception))
        self.assertFalse(issubclass(client.MissingKey, SystemExit))

    def test_optional_lookup_degrades_instead_of_exiting(self):
        """model_limit 是可选查询：没 key 要退回默认值，不是终止进程。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"ZYLAB_KEYS_FILE": os.path.join(tmp, "nope.env")}):
                for name in ("DEEPINFER_API_KEY",):
                    os.environ.pop(name, None)
                client._LIMITS.clear()
                self.assertEqual(
                    client.model_limit("whatever", default=4096), 4096)


class KeyStatus(unittest.TestCase):
    """启动检查与 /doctor 的数据源：只报来源，永不返回 key 本体。"""

    def test_never_returns_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = os.path.join(tmp, "keys.env")
            with open(key_file, "w", encoding="utf-8") as handle:
                handle.write("DEEPINFER_API_KEY=sk-fictional-abcdef\n")
            with mock.patch.dict(os.environ, {"ZYLAB_KEYS_FILE": key_file}):
                for name in ("DEEPINFER_API_KEY",):
                    os.environ.pop(name, None)
                status = client.key_status(client.route_for("deepinfer"))
        self.assertTrue(status["found"])
        self.assertNotIn("sk-fictional-abcdef", str(status),
                         "状态对象绝不能携带 key 本体 —— 它会进 /doctor 输出")
        self.assertIn(key_file, status["source"])

    def test_env_source_named(self):
        with mock.patch.dict(os.environ, {"DEEPINFER_API_KEY": "fictional"}):
            status = client.key_status(client.route_for("deepinfer"))
        self.assertEqual(status["source"], "环境变量 DEEPINFER_API_KEY")

    def test_missing_lists_where_it_looked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"ZYLAB_KEYS_FILE": os.path.join(tmp, "no.env")}):
                for name in ("DEEPINFER_API_KEY",):
                    os.environ.pop(name, None)
                status = client.key_status(client.route_for("deepinfer"))
        self.assertFalse(status["found"])
        self.assertIn("DEEPINFER_API_KEY", status["names"])
        self.assertTrue(status["candidates"], "缺 key 时必须说明找过哪里")


class TestIsolationFromRealCredentials(unittest.TestCase):
    """这套测试绝不能碰维护者的真 key。

    背景：整改时我把一份 setUpModule 插在文件前部，而第 47 行早有一份 ——
    Python 只保留后定义的那个，我那份从未执行。本机之所以全绿，纯粹因为
    真 keys.env 存在；陌生 pod 上同一份代码是 18 个 error。
    （AGENTS.md §4：脆的地方永远在两位作者假设的交界处。）
    """

    def test_module_fixture_supplies_a_fictional_key(self):
        self.assertEqual(client.api_key(client.route_for("deepinfer")),
                         "fictional-test-key",
                         "setUpModule 没生效 —— 多半是又被同名定义遮蔽了")

    def test_no_real_key_file_is_consulted(self):
        """环境变量优先于文件，所以本机的 keys.env 根本轮不到被读。"""
        self.assertEqual(client.api_key(client.route_for("boyue")),
                         "fictional-test-key")
