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
import urllib.error
import urllib.request
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
    # 这条闸门 2026-09-21 收窄了口径。原规矩是「内置 profile 一律不带地址」，
    # 初衷是别把维护者的内网端点写进代码。但它顺带挡住了公开厂商——陌生人
    # clone 下来 `zylab init --gateway openai` 会收到「未知网关」，而要新增网关
    # 又得先手写 settings.json。厂商官方 endpoint 是公开文档，不是部署事实。
    # 现在分两类钉：自建网关仍然一个字的地址都不带；公开厂商必须是 https 的
    # 厂商域名（本机推理例外，只允许 localhost）。
    SELF_HOSTED = {"deepinfer", "boyue"}
    LOCAL_ONLY = {"ollama", "lmstudio"}

    def test_self_hosted_profiles_still_ship_no_endpoint_address(self):
        for name in self.SELF_HOSTED:
            with self.subTest(gateway=name):
                self.assertIn(name, client.BUILTIN_GATEWAYS)
                self.assertNotIn("base", client.BUILTIN_GATEWAYS[name])

    def test_every_builtin_profile_names_its_key_variable(self):
        for name, cfg in client.BUILTIN_GATEWAYS.items():
            with self.subTest(gateway=name):
                self.assertTrue(cfg["keys"], "没有 key 变量名就没法收 key")

    def test_shipped_vendor_addresses_are_public_https_hosts(self):
        """防的是「有人把某个内网地址塞回代码」。"""
        import urllib.parse                               # noqa: PLC0415

        for name, cfg in client.BUILTIN_GATEWAYS.items():
            base = cfg.get("base")
            if not base:
                continue
            with self.subTest(gateway=name):
                parts = urllib.parse.urlsplit(base)
                host = parts.hostname or ""
                if name in self.LOCAL_ONLY:
                    self.assertIn(host, ("localhost", "127.0.0.1"),
                                  "本机推理只允许 localhost")
                    continue
                self.assertEqual(parts.scheme, "https", f"{name} 必须是 https")
                self.assertNotRegex(host, r"^\d+\.\d+\.\d+\.\d+$",
                                    "裸 IP 是部署事实，不该进代码")
                self.assertIn(".", host)
                self.assertFalse(
                    host.endswith((".local", ".internal", ".lan")),
                    "内网域名不该进代码")

    def test_a_public_vendor_resolves_without_any_user_config(self):
        self.assertEqual(client.route_for("openai").base,
                         "https://api.openai.com/v1")

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
            "cwd": "/no-such-root/repo-a/../repo-a",
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
        self.assertEqual(request["repo"], "/no-such-root/repo-a")
        self.assertEqual(request["session"], "session-a")
        self.assertEqual(request["gateway"], "boyue")
        opened.assert_called_once()

    def test_child_transport_scope_inherits_parent_session(self):
        route = client.GatewayRoute(
            "boyue", "http://example.invalid/v1", ("BOYUE_API_KEY",))
        request = client.transport_request(route, trace_context={
            "session_id": "child-session",
            "transport_session_id": "parent-session",
            "cwd": "/no-such-root/repo-a",
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


RESPONSES_TEXT_OK = [
    'data: {"type":"response.output_text.delta","delta":"你好","output_index":0}\n',
    'data: {"type":"response.completed","response":{"status":"completed",'
    '"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7},'
    '"reasoning":{"effort":"medium"}}}\n',
]


class NewerModelParameters(unittest.TestCase):
    """2026-09-24 对真实网关实测新旗舰，两类失败都是 **zylab 的请求格式**，不是模型坏了：

      gpt-6-sol / gpt-6-astra  `'max_tokens' is not supported with this model.
                               Use 'max_completion_tokens' instead.`
      kimi-k3                  `Parameter 'temperature'=0.3 is not supported`（**重发之后**）

    只 fake 最底层的 HTTP opener，stream_chat / _stream_once / probe 全是真的 ——
    第二个缺陷恰恰出在两层默认值的交界处，mock 掉任何一层都看不见它。
    """

    REJECT_MAX_TOKENS = (
        "HTTP 400: {\"error\":{\"message\":\"Unsupported parameter: 'max_tokens' is "
        "not supported with this model. Use 'max_completion_tokens' instead.\","
        "\"type\":\"invalid_request_error\",\"param\":\"max_tokens\"}}")

    REJECT_REASONING = (
        "HTTP 400: {\"error\":{\"message\":\"Function tools with reasoning_effort "
        "are not supported for gpt-6-sol in /v1/chat/completions. To use function "
        "tools, use /v1/responses or set reasoning_effort to 'none'.\","
        "\"param\":\"reasoning_effort\"}}")

    def setUp(self):
        client._ADAPTATIONS.clear()
        self.addCleanup(client._ADAPTATIONS.clear)

    def ok_response(self):
        return FakeResponse([sse(chunk(content="ok")), "data: [DONE]\n"])

    def test_max_tokens_rejection_switches_parameter_and_remembers_it(self):
        calls = []

        def fake_open(req, timeout=None):
            body = json.loads(req.data)
            calls.append(body)
            if "max_tokens" in body:
                raise client.APIError(self.REJECT_MAX_TOKENS)
            return self.ok_response()

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            events = list(client.stream_chat(
                "gpt-6-sol", [{"role": "user", "content": "x"}],
                max_tokens=120, retries=1))
            list(client.stream_chat(
                "gpt-6-sol", [{"role": "user", "content": "x"}],
                max_tokens=120, retries=1))
        self.assertEqual(
            [(("max_tokens" in c), c.get("max_completion_tokens")) for c in calls],
            [(True, None), (False, 120), (False, 120)],
            "被拒一次就换参数；同一进程里下一次直接用对的")
        self.assertEqual("".join(e["v"] for e in events if e["t"] == "text"), "ok")
        self.assertEqual(
            models.get("deepinfer", "gpt-6-sol").get("max_tokens_param"),
            "max_completion_tokens", "记进能力表，换个进程也不用再撞一次")

    def test_a_new_process_reads_the_remembered_parameter(self):
        models.update_record("deepinfer", "gpt-6-astra", lambda rec: rec.update(
            gateway="deepinfer", id="gpt-6-astra",
            max_tokens_param="max_completion_tokens"))
        calls = []

        def fake_open(req, timeout=None):
            calls.append(json.loads(req.data))
            return self.ok_response()

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            list(client.stream_chat(
                "gpt-6-astra", [{"role": "user", "content": "x"}], max_tokens=64))
        self.assertNotIn("max_tokens", calls[0])
        self.assertEqual(calls[0]["max_completion_tokens"], 64)

    def test_both_adaptations_chain_like_the_real_gateway_did(self):
        """真实顺序：先撞 max_tokens；换完参数名再撞「use /v1/responses or set
        reasoning_effort to 'none'」—— 两条路里**先换接口**：关推理能跑，但那是让模型
        关着脑子干活，Responses 上工具与推理可以同时开。"""
        calls = []

        def fake_open(req, timeout=None):
            body = json.loads(req.data)
            calls.append((req.full_url.rsplit("/", 1)[-1], body))
            if req.full_url.endswith("/responses"):
                return FakeResponse(RESPONSES_TEXT_OK)
            if "max_tokens" in body:
                raise client.APIError(self.REJECT_MAX_TOKENS)
            raise client.APIError(self.REJECT_REASONING)

        warnings_seen = []
        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            events = list(client.stream_chat(
                "gpt-6-chain", [{"role": "user", "content": "x"}],
                max_tokens=120, retries=1, route_warning=warnings_seen.append))
        self.assertEqual([url for url, _ in calls],
                         ["completions", "completions", "responses"])
        self.assertNotIn("reasoning_effort", calls[-1][1])
        self.assertEqual(calls[-1][1]["max_output_tokens"], 120)
        self.assertEqual("".join(e["v"] for e in events if e["t"] == "text"), "你好")
        self.assertTrue(any("/v1/responses" in w for w in warnings_seen))
        record = models.get("deepinfer", "gpt-6-chain")
        self.assertEqual(record.get("api"), "responses")
        self.assertIsNone(record.get("reasoning_effort"))

    def test_without_a_responses_endpoint_it_falls_back_to_reasoning_none(self):
        """网关根本没有 /v1/responses（404）：退回 chat，才轮到「关推理」。
        不跳过已试过的适配，这一步会在「切接口」上原地打转。"""
        calls = []

        def fake_open(req, timeout=None):
            body = json.loads(req.data)
            calls.append((req.full_url.rsplit("/", 1)[-1], body))
            if req.full_url.endswith("/responses"):
                raise client.APIError("HTTP 404: page not found", status=404)
            if body.get("reasoning_effort") == "none":
                return self.ok_response()
            raise client.APIError(self.REJECT_REASONING)

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            events = list(client.stream_chat(
                "gpt-6-no-responses", [{"role": "user", "content": "x"}],
                retries=1))
        self.assertEqual([url for url, _ in calls],
                         ["completions", "responses", "completions", "completions"])
        self.assertEqual(calls[-1][1].get("reasoning_effort"), "none")
        self.assertEqual("".join(e["v"] for e in events if e["t"] == "text"), "ok")
        self.assertEqual(
            models.get("deepinfer", "gpt-6-no-responses").get("api"), "chat")

    def test_an_unrelated_400_is_not_mistaken_for_an_adaptation(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(json.loads(req.data))
            raise client.APIError("HTTP 400: messages must alternate roles")

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            with self.assertRaises(client.APIError):
                list(client.stream_chat(
                    "gpt-6-plain", [{"role": "user", "content": "x"}], retries=1))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("reasoning_effort", calls[0])

    def test_the_capability_probe_gets_the_same_parameter_fallback(self):
        """probe 关掉的只是 temperature 的自动降级（它要亲眼看见那条拒绝）；
        参数名不是能力，照样自动换。"""
        calls = []

        def fake_open(req, timeout=None):
            body = json.loads(req.data)
            calls.append(body)
            if "max_tokens" in body:
                raise client.APIError(self.REJECT_MAX_TOKENS)
            return self.ok_response()

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            list(client.stream_chat(
                "gpt-6-luna", [{"role": "user", "content": "x"}],
                max_tokens=120, retries=1, temperature_fallback=False))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].get("max_completion_tokens"), 120)

    def test_the_probe_really_drops_temperature_on_its_retry(self):
        """`_one_shot(temperature=None)` 以前干脆不传这个参数，stream_chat 的默认值
        0.3 就又补了回去 —— 「去掉 temperature 重发」从来没去掉过。"""
        calls = []

        def fake_open(req, timeout=None):
            body = json.loads(req.data)
            calls.append(body)
            if "temperature" in body:
                raise client.APIError(
                    "HTTP 400: Parameter 'temperature'=0.3 is not supported "
                    "for kimi-k3 model.")
            return FakeResponse([
                sse(chunk(tool_calls=[{"index": 0, "id": "c1", "function": {
                    "name": "get_file", "arguments": "{\"path\": \"/tmp/a.txt\"}"}}])),
                "data: [DONE]\n"])

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open), \
                mock.patch.object(models, "_record_probe_health"):
            record = models.probe("deepinfer", "kimi-k3")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("temperature", calls[1])
        self.assertEqual(record["status"], "ok")
        self.assertIs(record["supports_temperature"], False)
        self.assertIs(record["supports_tools"], True)


class ProbeEvidence(unittest.TestCase):
    """实测结论必须有证据。2026-09-24 实跑 claude-opus-5-5：第一次回了一个**空响应**
    （没有文本也没有工具调用），被记成「不支持工具」→ 列表里消失；第二次它好好地调了工具。
    一次都没说话，谈不上「不会调工具」。"""

    TOOL_CALL = [sse(chunk(tool_calls=[{"index": 0, "id": "c1", "function": {
        "name": "get_file", "arguments": "{\"path\": \"/tmp/a.txt\"}"}}])),
        "data: [DONE]\n"]

    def setUp(self):
        client._ADAPTATIONS.clear()
        self.addCleanup(client._ADAPTATIONS.clear)

    def probe(self, model, responses):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(json.loads(req.data))
            return FakeResponse(responses[min(len(calls), len(responses)) - 1])

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open), \
                mock.patch.object(models, "_record_probe_health"):
            record = models.probe("deepinfer", model)
        return record, calls

    def test_an_empty_reply_is_retried_not_read_as_no_tools(self):
        record, calls = self.probe("opus-empty-once", [["data: [DONE]\n"], self.TOOL_CALL])
        self.assertEqual(len(calls), 2)
        self.assertEqual(record["status"], "ok")
        self.assertIs(record["supports_tools"], True)
        self.assertGreater(calls[1]["max_tokens"], calls[0]["max_tokens"],
                           "重试给足 token：推理模型可能把 120 个全花在思考上")

    def test_two_empty_replies_are_an_error_not_a_capability(self):
        record, calls = self.probe("opus-empty-twice", [["data: [DONE]\n"]])
        self.assertEqual(len(calls), 2)
        self.assertEqual(record["status"], "error")
        self.assertIsNone(record["supports_tools"], "没有证据就不下能力结论")
        self.assertIn("空响应", record["error"])

    def test_a_content_filter_says_so(self):
        """真实原因要看得见：Bedrock 的内容过滤返回 finish_reason=content_filter、0 token。"""
        filtered = [sse({"choices": [{"delta": {}, "finish_reason": "content_filter"}]}),
                    "data: [DONE]\n"]
        record, calls = self.probe("opus-filtered", [filtered])
        self.assertEqual(record["status"], "error")
        self.assertIn("content_filter", record["error"])

    def test_a_text_reply_without_a_tool_call_is_still_chat_only(self):
        record, calls = self.probe("chatty", [[sse(chunk(content="我不能读文件。")),
                                               "data: [DONE]\n"]])
        self.assertEqual(len(calls), 1)
        self.assertIs(record["supports_tools"], False)

    def test_an_adaptation_the_gateway_rejects_is_forgotten(self):
        """gpt-6-astra：网关要它关推理才能带工具，而它不接受关推理 —— 那条 none
        不能一直记着，否则哪天网关支持了，记忆本身会把它挡在门外。"""
        models.update_record("deepinfer", "astra-like", lambda rec: rec.update(
            gateway="deepinfer", id="astra-like", reasoning_effort="none"))

        def fake_open(req, timeout=None):
            raise client.APIError(
                "HTTP 400: Unsupported value: 'reasoning_effort' does not support "
                "'none' with this model. Supported values are: 'low', 'medium'.")

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            with self.assertRaises(client.APIError):
                list(client.stream_chat(
                    "astra-like", [{"role": "user", "content": "x"}], retries=1))
        self.assertIsNone(models.get("deepinfer", "astra-like").get("reasoning_effort"))


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


class DSMLLeakFilterTests(unittest.TestCase):
    """DeepSeek 偶发把 tool-call 语法写进 content 通道（<｜DSML｜tool_calls>）。

    流式过滤必须做到：
    1. 标记被 chunk 边界劈在**任何位置**都能剥干净——包括劈在开标签中间；
    2. 剥掉的块里的调用要回收成真正的 tool 事件，两种参数写法都认；
    3. 模型在正文里**谈论 / 引用**这个标记时一个字都不能动。

    2026-09-20：第一版在真实网关上 7 次里漏了 3 次。当时的用例把分片切在完整开标签之后
    （leak[:20]），正好绕开了缺陷；真实的流是按 token 切的。所以下面每个用例都跑一组分片
    粒度，含逐字符。
    """

    SIZES = (10_000, 20, 7, 3, 1)
    JSON_FORM = ('<｜DSML｜tool_calls>\n'
                 '<｜DSML｜invoke name="bash">\n'
                 '<｜DSML｜parameter name="arguments" string="false">'
                 '{"command": "ls"}\n'
                 '</｜DSML｜parameter>\n'
                 '</｜DSML｜invoke>\n'
                 '</｜DSML｜tool_calls>')
    # DeepSeek 原生写法：一个参数一个元素，string= 说明值是裸字符串还是 JSON
    NAMED_FORM = ('<｜DSML｜tool_calls>\n'
                  '<｜DSML｜invoke name="expand_output">\n'
                  '<｜DSML｜parameter name="target" string="true">sha256:9faeb2cd578fadd2'
                  '</｜DSML｜parameter>\n'
                  '<｜DSML｜parameter name="page" string="false">2</｜DSML｜parameter>\n'
                  '</｜DSML｜invoke>\n'
                  '</｜DSML｜tool_calls>')

    def _run(self, content, size, finish="stop", structured=None):
        lines = [sse(chunk(content=content[i:i + size]))
                 for i in range(0, len(content), size)]
        if structured:
            lines.append(sse(chunk(tool_calls=structured)))
        lines += [sse({"choices": [{"delta": {}, "finish_reason": finish}]}),
                  "data: [DONE]\n"]
        with mock.patch.object(client._OPENER, "open",
                               return_value=FakeResponse(lines)):
            events = list(client.stream_chat(
                "deepseek-v4-pro", [{"role": "user", "content": "x"}]))
        text = "".join(e["v"] for e in events if e["t"] == "text")
        calls = [(call["function"]["name"], json.loads(call["function"]["arguments"]))
                 for e in events if e["t"] == "tool" for call in e["v"]]
        return text, calls, events

    def _each_size(self, content, want_text, want_calls, **kwargs):
        for size in self.SIZES:
            with self.subTest(chunk=size):
                text, calls, _ = self._run(content, size, **kwargs)
                self.assertEqual(text, want_text)
                self.assertEqual(calls, want_calls)

    def test_a_leak_is_stripped_and_recovered_however_the_stream_is_chunked(self):
        self._each_size("让我看看。\n" + self.JSON_FORM, "让我看看。\n",
                        [("bash", {"command": "ls"})])

    def test_the_native_one_element_per_parameter_form_is_recovered_too(self):
        """以前这种写法被剥掉后不回收：模型的调用凭空消失，这一轮什么都没干就结束。"""
        self._each_size("让我看看。\n" + self.NAMED_FORM + "\n", "让我看看。\n",
                        [("expand_output",
                          {"target": "sha256:9faeb2cd578fadd2", "page": 2})])

    def test_the_other_outer_tag_name_is_recognised(self):
        self._each_size(self.JSON_FORM.replace("tool_calls", "function_calls"), "",
                        [("bash", {"command": "ls"})])

    def test_prose_that_mentions_the_marker_is_left_alone(self):
        text = "流式分片可能把 `<｜DSML｜tool_calls>` 标记劈在两个 chunk 里；a < b 也没事。"
        self._each_size(text, text, [])

    def test_a_quoted_block_followed_by_prose_is_not_a_call(self):
        text = "格式如下：\n" + self.JSON_FORM + "\n以上就是 DeepSeek 的原生写法。"
        self._each_size(text, text, [])

    def test_a_block_inside_a_code_fence_is_not_a_call(self):
        text = "示例：\n```\n" + self.JSON_FORM + "\n```\n"
        self._each_size(text, text, [])

    def test_an_unclosed_block_still_yields_its_complete_invokes(self):
        cut = self.JSON_FORM[:self.JSON_FORM.index("</｜DSML｜tool_calls>")]
        self._each_size("查一下。" + cut, "查一下。", [("bash", {"command": "ls"})])

    def test_a_block_truncated_mid_argument_is_dropped_without_showing_markup(self):
        cut = self.JSON_FORM[:self.JSON_FORM.index('"ls"')]
        self._each_size("查一下。" + cut, "查一下。", [], finish="length")

    def test_consecutive_blocks_are_parallel_calls_not_a_quote(self):
        second = self.JSON_FORM.replace('"ls"', '"pwd"')
        self._each_size("同时查两样。\n" + self.JSON_FORM + "\n" + second + "\n", "同时查两样。\n",
                        [("bash", {"command": "ls"}), ("bash", {"command": "pwd"})])

    def test_a_real_call_after_a_quoted_block_is_still_recovered(self):
        quoted = "格式长这样：\n" + self.JSON_FORM + "\n好，现在真的查一下。\n"
        self._each_size(quoted + self.NAMED_FORM, quoted,
                        [("expand_output",
                          {"target": "sha256:9faeb2cd578fadd2", "page": 2})])

    # 2026-09-20 真实网关上抓到的原件（截短）：外层开标签被网关的解析器吃掉了，正文以裸 invoke
    # 开头，后面是几百个重复的闭标签；同一条响应的结构化字段里另有一个调用。
    DEGENERATE = ('<｜DSML｜invoke name="bash">\n'
                  '<｜DSML｜parameter name="command" string="true">sed -n \'9935,9940'
                  '</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜tool_calls>\n'
                  '</｜DSML｜invoke>\n</｜DSML｜tool_calls>\n</｜DSML｜>\n</invoke>\n'
                  '</｜DSML｜_command>\n' + '</invoke>\n' * 40 + 'invoke>\n</invoke>\n</invok')

    def test_a_bare_invoke_with_closing_tag_debris_never_reaches_the_user(self):
        structured = [{"index": 0, "id": "call_9", "function": {
            "name": "read_file", "arguments": "{\"path\": \"/repo/x\"}"}}]
        for size in self.SIZES:
            with self.subTest(chunk=size):
                text, calls, _ = self._run(self.DEGENERATE, size, structured=structured)
                self.assertEqual(text, "")
                self.assertEqual(calls, [("read_file", {"path": "/repo/x"})])

    def test_orphan_closing_tags_never_reach_the_user(self):
        """同一天抓到的另外两个原件：网关把调用解析走了，正文里只剩闭标签。"""
        structured = [{"index": 0, "id": "call_9", "function": {
            "name": "bash", "arguments": "{\"command\": \"ls\"}"}}]
        loop = ("</｜DSML｜invoke>\n</｜DSML｜tool_calls>\n</｜DSML｜invoke>\n</invoke>\n"
                + "</invoke>\n" * 60 + "invoke>\n</｜DSML｜parameter>\n</invok")
        for content in ("</｜DSML｜tool_calls>", loop):
            for size in self.SIZES:
                with self.subTest(chunk=size, content=content[:24]):
                    text, calls, _ = self._run(content, size, structured=structured)
                    self.assertEqual(text, "")
                    self.assertEqual(calls, [("bash", {"command": "ls"})])

    def test_a_quoted_partial_block_in_a_fence_is_left_alone(self):
        """真实会话里的原件：模型在代码块里引用了半个块（有开标签和 invoke，没有闭标签），
        后面接着讲解。逐字符分片时 ``` 本身会被劈开，代码块的判断不能依赖它整个到达。"""
        text = ("你贴的屏幕上是：\n\n```\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name=\"bash\">\n```\n\n"
                "这是 DSML 格式。但我刚才复现测试里模拟的是 `<tool_calls>`。")
        self._each_size(text, text, [])

    def test_the_same_partial_quote_outside_a_fence_is_left_alone_too(self):
        """invoke 里只能是 parameter 元素；跟了别的，就不是调用。"""
        text = ("格式的开头是 <｜DSML｜tool_calls>\n<｜DSML｜invoke name=\"bash\">\n然后才是参数，"
                "每个参数一个元素。")
        self._each_size(text, text, [])

    def test_parameter_values_may_contain_anything(self):
        body = "echo '```'; echo '</invoke>'; cat <<'EOF'\n<｜DSML｜invoke name=\"x\">\nEOF\n"
        leak = ('<｜DSML｜tool_calls>\n<｜DSML｜invoke name="bash">\n'
                '<｜DSML｜parameter name="command" string="true">' + body
                + '</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜tool_calls>')
        self._each_size("跑一下。\n" + leak, "跑一下。\n", [("bash", {"command": body})])

    def test_prose_about_the_closing_tag_is_left_alone(self):
        text = "闭标签 `</｜DSML｜tool_calls>` 之后如果还有正文，就是引用而不是调用。"
        self._each_size(text, text, [])

    def test_a_bare_invoke_is_recovered_when_nothing_structured_came_with_it(self):
        self._each_size("看一眼。\n" + self.DEGENERATE, "看一眼。\n",
                        [("bash", {"command": "sed -n '9935,9940"})])

    def test_structured_tool_calls_win_over_a_leaked_copy(self):
        """调用方对 tool 事件是整份替换：两边都有时只执行结构化的那份，不重复执行。"""
        structured = [{"index": 0, "id": "call_1", "function": {
            "name": "bash", "arguments": "{\"command\": \"pwd\"}"}}]
        for size in self.SIZES:
            with self.subTest(chunk=size):
                text, calls, events = self._run(
                    self.JSON_FORM, size, structured=structured)
                self.assertEqual(text, "")
                self.assertEqual(calls, [("bash", {"command": "pwd"})])
                self.assertEqual(sum(1 for e in events if e["t"] == "tool"), 1)

    def test_recovered_call_ids_do_not_repeat_across_requests(self):
        ids = set()
        for _ in range(20):
            _, _, events = self._run(self.JSON_FORM, 7)
            ids.update(call["id"] for e in events if e["t"] == "tool" for call in e["v"])
        self.assertEqual(len(ids), 20)

    def test_plain_text_and_other_models_are_untouched(self):
        text = "普通正文，没有泄漏 <b>html</b> 1 < 2"
        self._each_size(text, text, [])
        lines = [sse(chunk(content=self.JSON_FORM)),
                 sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
                 "data: [DONE]\n"]
        with mock.patch.object(client._OPENER, "open",
                               return_value=FakeResponse(lines)):
            events = list(client.stream_chat(
                "glm-5.3", [{"role": "user", "content": "x"}]))
        self.assertEqual("".join(e["v"] for e in events if e["t"] == "text"),
                         self.JSON_FORM, "只对已知会泄漏的模型族启用过滤")

class ApiProxyIsOptInOnly(unittest.TestCase):
    """模型请求的代理入口：**只认显式声明**。

    2026-09-22 实跑陌生人流程时撞到的死路：`ProxyHandler({})` 是刻意的
    （企业网络里默认导出的代理常常只放行一部分域名，打到自家网关上返回 403，
    看起来像 key 失效），但它此前**没有任何出路** —— 端点只能经代理到达的用户
    完全没法用 zylab，而 init 的提示只说「我不读你的环境变量」。

    刻意**不**复用 `web.proxy`：那条给网页抓取，信任面不同 —— 这条会把 API key、
    prompt、代码与工具结果都送过去，必须由用户为这条路单独表态。
    """

    def setUp(self):
        client._PROXY_OPENERS.clear()
        self.addCleanup(client._PROXY_OPENERS.clear)

    def env(self, value):
        return mock.patch.dict(os.environ, {"ZYLAB_API_PROXY": value})

    def test_undeclared_means_direct(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZYLAB_API_PROXY", None)
            self.assertIsNone(client.api_proxy())

    def test_a_declared_url_is_used(self):
        with self.env("http://u:p@proxy.invalid:3128/"):
            self.assertEqual(client.api_proxy(),
                             "http://u:p@proxy.invalid:3128/")

    def test_a_value_that_is_not_a_url_is_treated_as_undeclared(self):
        """09-20 的事故形态：一条 shell 命令被当成代理交给 ProxyHandler，
        urllib 抛 InvalidURL，代理这条腿当场失效。形态不对就当没声明，
        **不是报错** —— 直连仍然可用。"""
        for value in ("unset http_proxy https_proxy", "yes", "1",
                      "proxy.invalid:3128", ""):
            with self.subTest(value=value), self.env(value):
                self.assertIsNone(client.api_proxy())

    def test_the_direct_opener_is_the_module_level_one(self):
        """无代理时必须返回 `_OPENER` 本身 —— 整套测试都 patch 它的 open。"""
        self.assertIs(client._opener_for(None), client._OPENER)
        self.assertIs(client._opener_for(""), client._OPENER)

    def test_a_proxy_gets_its_own_cached_opener(self):
        first = client._opener_for("http://proxy.invalid:3128/")
        self.assertIsNot(first, client._OPENER)
        self.assertIs(first, client._opener_for("http://proxy.invalid:3128/"),
                      "同一个代理不该每次重建 opener")
        self.assertIsNot(first, client._opener_for("http://other.invalid:1/"))
        handlers = [h for h in first.handlers
                    if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(handlers)
        self.assertEqual(handlers[0].proxies.get("https"),
                         "http://proxy.invalid:3128/")

    def test_both_request_paths_go_through_it(self):
        """两条发请求的路都要用 `_opener_for(api_proxy())`，漏一条就是
        「聊天走代理、列模型不走」这种更难查的半通状态。"""
        source = (Path(__file__).resolve().parents[1]
                  / "core" / "client.py").read_text(encoding="utf-8")
        # 三条：chat/completions、/v1/responses（09-24 起）、/models
        self.assertEqual(source.count("_opener_for(api_proxy(route)).open("), 3)
        self.assertNotIn("_OPENER.open(", source)
        self.assertNotIn("api_proxy())", source,
                         "不带 route 就读不到按网关的声明")

    def test_a_per_gateway_declaration_applies_to_that_gateway_only(self):
        """2026-09-24 实测：DeepSeek 官方口只有经代理才通、DeepInfer 只有直连才通。
        全局一个开关两者不可兼得，所以要能只给一个网关声明代理。"""
        deepseek = client.GatewayRoute("deepseek", "https://api.deepseek.com/v1", ())
        deepinfer = client.GatewayRoute("deepinfer", "https://gw.invalid/v1", ())
        env = {"ZYLAB_API_PROXY_DEEPSEEK": "http://proxy.invalid:3128/"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("ZYLAB_API_PROXY", None)
            self.assertEqual(client.api_proxy(deepseek), "http://proxy.invalid:3128/")
            self.assertIsNone(client.api_proxy(deepinfer))
            self.assertIsNone(client.api_proxy(), "没有 route 时只认全局那一条")

    def test_the_per_gateway_declaration_wins_over_the_global_one(self):
        route = client.GatewayRoute("deepseek", "https://api.deepseek.com/v1", ())
        other = client.GatewayRoute("boyue", "https://gw.invalid/v1", ())
        env = {"ZYLAB_API_PROXY": "http://global.invalid:1/",
               "ZYLAB_API_PROXY_DEEPSEEK": "http://proxy.invalid:3128/"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(client.api_proxy(route), "http://proxy.invalid:3128/")
            self.assertEqual(client.api_proxy(other), "http://global.invalid:1/")

    def test_a_malformed_per_gateway_value_falls_back_like_the_global_one(self):
        route = client.GatewayRoute("deepseek", "https://api.deepseek.com/v1", ())
        with mock.patch.dict(os.environ, {"ZYLAB_API_PROXY_DEEPSEEK": "yes"}):
            os.environ.pop("ZYLAB_API_PROXY", None)
            self.assertIsNone(client.api_proxy(route))

    def test_a_proxy_saved_in_settings_applies_to_that_gateway(self):
        """同事的机器上没有这台机器的 shell 文件：代理要能存进 zylab 自己的配置
        （settings 的 gateways.<名字>.proxy），从哪个终端、IDE、cron 启动都生效。"""
        deepseek = client.GatewayRoute("deepseek", "https://api.deepseek.com/v1", ())
        deepinfer = client.GatewayRoute("deepinfer", "https://gw.invalid/v1", ())
        with mock.patch.dict(client.GATEWAYS["deepseek"],
                             {"proxy": "http://proxy.invalid:3128/"}), \
                mock.patch.dict(os.environ, {}):
            for name in ("ZYLAB_API_PROXY", "ZYLAB_API_PROXY_DEEPSEEK"):
                os.environ.pop(name, None)
            self.assertEqual(client.api_proxy(deepseek), "http://proxy.invalid:3128/")
            self.assertIsNone(client.api_proxy(deepinfer))

    def test_the_environment_overrides_the_saved_proxy(self):
        route = client.GatewayRoute("deepseek", "https://api.deepseek.com/v1", ())
        with mock.patch.dict(client.GATEWAYS["deepseek"],
                             {"proxy": "http://saved.invalid:1/"}), \
                mock.patch.dict(os.environ,
                                {"ZYLAB_API_PROXY_DEEPSEEK": "http://env.invalid:2/"}):
            self.assertEqual(client.api_proxy(route), "http://env.invalid:2/")

    def test_a_malformed_saved_proxy_is_ignored(self):
        route = client.GatewayRoute("deepseek", "https://api.deepseek.com/v1", ())
        with mock.patch.dict(client.GATEWAYS["deepseek"], {"proxy": "proxy_on"}), \
                mock.patch.dict(os.environ, {}):
            for name in ("ZYLAB_API_PROXY", "ZYLAB_API_PROXY_DEEPSEEK"):
                os.environ.pop(name, None)
            self.assertIsNone(client.api_proxy(route))

    def test_reachability_is_probed_without_the_key(self):
        """试连只看连不连得上：任何 HTTP 回答（401 也算）= 连上；不带 Authorization ——
        试一条陌生代理时，key 不能先交出去。"""
        seen = []

        class Opener:
            def __init__(self, outcome):
                self.outcome = outcome

            def open(self, req, timeout=None):
                seen.append(dict(req.header_items()))
                raise self.outcome

        answered = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch.object(client, "_opener_for", return_value=Opener(answered)):
            self.assertTrue(client.probe_reachable("https://gw.invalid/v1")[0])
        with mock.patch.object(client, "_opener_for",
                               return_value=Opener(urllib.error.URLError("timed out"))):
            self.assertFalse(client.probe_reachable("https://gw.invalid/v1")[0])
        self.assertTrue(seen)
        self.assertFalse(any("Authorization" in headers for headers in seen))

    def test_environment_proxy_candidates(self):
        env = {"https_proxy": "http://a.invalid:1/", "HTTPS_PROXY": "http://a.invalid:1/",
               "http_proxy": "http://b.invalid:2/", "all_proxy": "unset x"}
        self.assertEqual(client.environment_proxy_candidates(env), [
            ("https_proxy", "http://a.invalid:1/"), ("http_proxy", "http://b.invalid:2/")])

    def test_list_models_sends_through_the_gateways_own_proxy(self):
        """不只是 api_proxy 算对了 —— 取目录那一跳真的交给了这个网关的 opener。"""
        route = client.GatewayRoute("deepseek", "https://api.deepseek.com/v1",
                                    ("DEEPSEEK_API_KEY",))
        seen = []

        class Opener:
            def open(self, req, timeout=None):
                seen.append((req.full_url, timeout))
                return io.BytesIO(b'{"data": [{"id": "deepseek-chat"}]}')

        env = {"ZYLAB_API_PROXY_DEEPSEEK": "http://proxy.invalid:3128/",
               "DEEPSEEK_API_KEY": "sk-test"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(client, "_opener_for",
                                  side_effect=lambda proxy: (
                                      seen.append(proxy) or Opener())):
            listing = client.list_models(route=route, timeout=7)
        self.assertEqual([m["id"] for m in listing], ["deepseek-chat"])
        self.assertEqual(seen[0], "http://proxy.invalid:3128/")
        self.assertEqual(seen[1], ("https://api.deepseek.com/v1/models", 7))


class InitDiagnosticNeverPrintsProxyCredentials(unittest.TestCase):
    """`zylab init` 探测失败时会打印本机代理变量 —— 值必须脱敏。

    代理 URL 常内嵌 `user:pass@`，而那行诊断会进终端、进日志、进用户粘给同事的
    那段输出。`webfetch.redact()` 的注释原话就是「代理凭据绝不能进任何输出」，
    这里之前没用上。2026-09-22 实跑陌生人流程时撞到：那台机器的代理正好带
    用户名密码。
    """

    def test_the_values_go_through_redact(self):
        source = (Path(__file__).resolve().parents[1]
                  / "zylab.py").read_text(encoding="utf-8")
        self.assertIn("proxies = {k: webfetch.redact(v)", source)
        self.assertNotIn(
            "proxies = {k: v for k, v in os.environ.items()", source)

    def test_redact_actually_removes_the_credentials(self):
        from core import webfetch
        out = webfetch.redact("http://someone:s3cr3t@10.0.0.1:23128/")
        self.assertNotIn("s3cr3t", out)
        self.assertNotIn("someone", out)
        self.assertIn("10.0.0.1:23128", out)

