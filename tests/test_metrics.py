"""M5 runtime request/tool metrics, health, and fail-closed boundaries."""
import json
import http.client
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import client, model_health, state, store

# ignore_cleanup_errors：Windows 上打开着的文件删不掉（这是平台语义，
# 不是缺陷）。这些用例把 sqlite 库开在临时目录里、不显式关闭，
# POSIX 上照样能删，Windows 上会在 tearDown 抛 WinError 32，
# 把一个通过的断言变成 ERROR。


class FakeResponse:
    def __init__(self, lines):
        self.lines = [
            line.encode() if isinstance(line, str) else line for line in lines
        ]

    def __iter__(self):
        return iter(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def sse(payload):
    return f"data: {json.dumps(payload)}\n"


def text_chunk(value):
    return {"choices": [{
        "delta": {"content": value}, "finish_reason": None,
    }]}


class SequenceClock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class MutableClock:
    def __init__(self, value=0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += max(0.0, float(seconds))


class JournalModeConnection:
    """Deterministic PRAGMA journal_mode stub for bootstrap retry tests."""

    def __init__(self, set_outcomes, *, read_modes=()):
        self.set_outcomes = list(set_outcomes)
        self.read_modes = list(read_modes)
        self.current = "DELETE"
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if statement == "PRAGMA journal_mode":
            if self.read_modes:
                value = self.read_modes.pop(0)
            else:
                value = self.current
            if isinstance(value, BaseException):
                raise value
            return SimpleNamespace(fetchone=lambda: (value,))
        prefix = "PRAGMA journal_mode = "
        if not statement.startswith(prefix):
            raise AssertionError(f"unexpected statement: {statement}")
        if not self.set_outcomes:
            raise AssertionError("journal mode outcome sequence exhausted")
        value = self.set_outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        self.current = str(value).upper()
        return SimpleNamespace(fetchone=lambda: (value,))


class RuntimeTraceTests(unittest.TestCase):
    def setUp(self):
        self.transport_policy = client.configure_transport_policy(
            allow_insecure_http=True)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "state.sqlite3"
        self.route = client.GatewayRoute(
            "test", "http://example.invalid/v1", ("TEST_KEY",))

    def tearDown(self):
        try:
            self.tmp.cleanup()
        finally:
            client.restore_transport_policy(self.transport_policy)

    def test_success_records_true_ttft_total_usage_and_health(self):
        # route check, queued, started, headers, first, last, ended
        metrics = state.MetricsFacade(
            self.db_path, clock=SequenceClock(range(7)))
        lines = [
            sse(text_chunk("ok")),
            sse({"choices": [], "usage": {
                "prompt_tokens": 11, "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 4},
            }}),
            "data: [DONE]\n",
        ]
        with mock.patch.object(client, "api_key", return_value="secret"), \
             mock.patch.object(
                 client._OPENER, "open", return_value=FakeResponse(lines)):
            events = list(client.stream_chat(
                "model-a", [{"role": "user", "content": "x"}],
                route=self.route, retries=1, metrics=metrics,
                trace_context={
                    "request_id": "logical-1", "session_id": "session-1",
                    "turn_id": "turn-1", "context_tokens_before": 99,
                    "context_limit": 1000,
                }))

        self.assertEqual([event["t"] for event in events], ["text", "done"])
        row = metrics.get("logical-1")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["attempt"], 1)
        self.assertEqual(row["prompt_tokens"], 11)
        self.assertEqual(row["completion_tokens"], 3)
        self.assertEqual(row["cache_read"], 4)
        self.assertEqual(row["usage_reported"], 1)
        self.assertEqual(row["true_ttft_ms"], 2000.0)
        self.assertEqual(row["total_latency_ms"], 4000.0)
        self.assertTrue(all(row[field] is not None
                            for field in state.REQUEST_PHASE_FIELDS))
        health = metrics.get_health("test", "model-a")
        self.assertEqual(health["success_count"], 1)
        self.assertEqual(health["failure_count"], 0)
        self.assertEqual(health["true_ttft_ms"], 2000.0)
        self.assertEqual(health["total_latency_ms"], 4000.0)

    def test_transient_retry_has_distinct_rows_and_failure_has_no_usage(self):
        metrics = state.MetricsFacade(self.db_path)
        with mock.patch.object(client, "api_key", return_value="secret"), \
             mock.patch.object(
                 client._OPENER, "open", side_effect=OSError("down")) as opened, \
             mock.patch.object(client, "_wait_retry"):
            with self.assertRaises(client.APIError):
                list(client.stream_chat(
                    "model-a", [], route=self.route, retries=2,
                    metrics=metrics,
                    trace_context={"request_id": "retry-root"}))

        rows = list(reversed(metrics.list_recent_traces(model="model-a")))
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(
            [row["id"] for row in rows],
            ["retry-root", "retry-root-attempt-2"])
        self.assertEqual([row["attempt"] for row in rows], [1, 2])
        self.assertEqual(
            [row["retry_reason"] for row in rows],
            ["transient_retry", None])
        for row in rows:
            self.assertEqual(row["status"], "failed")
            self.assertIsNone(row["prompt_tokens"])
            self.assertIsNone(row["completion_tokens"])
            self.assertEqual(row["usage_reported"], 0)
            self.assertIsNotNone(row["queued_at"])
            self.assertIsNotNone(row["started_at"])
            self.assertIsNotNone(row["ended_at"])
            self.assertIsNone(row["headers_at"])
            self.assertIsNone(row["first_delta_at"])
        health = metrics.get_health("test", "model-a")
        self.assertEqual(health["failure_count"], 2)
        self.assertEqual(health["consecutive_failures"], 2)

    def test_temperature_fallback_is_a_separate_capability_attempt(self):
        metrics = state.MetricsFacade(self.db_path)
        calls = []

        def open_request(request, timeout=None):
            payload = json.loads(request.data)
            calls.append(payload)
            if "temperature" in payload:
                raise client.APIError(
                    "HTTP 400: temperature is deprecated",
                    kind="capability_temperature", retryable=False)
            return FakeResponse([sse(text_chunk("ok")), "data: [DONE]\n"])

        with mock.patch.object(client, "api_key", return_value="secret"), \
             mock.patch.object(client._OPENER, "open", side_effect=open_request):
            list(client.stream_chat(
                "model-a", [], route=self.route, retries=1,
                temperature=0.3, metrics=metrics,
                trace_context={"request_id": "temperature-root"}))

        self.assertEqual(len(calls), 2)
        self.assertNotIn("temperature", calls[1])
        rows = list(reversed(metrics.list_recent_traces(model="model-a")))
        self.assertEqual([row["status"] for row in rows], ["failed", "ok"])
        self.assertEqual(rows[0]["retry_reason"], "temperature_fallback")
        self.assertEqual(rows[0]["error_kind"], "capability_temperature")
        self.assertEqual(rows[1]["id"], "temperature-root-attempt-2")
        health = metrics.get_health("test", "model-a")
        self.assertEqual(health["success_count"], 1)
        self.assertEqual(health["failure_count"], 1)
        self.assertEqual(health["consecutive_failures"], 0)

    def test_interrupted_attempt_is_not_counted_as_provider_failure(self):
        metrics = state.MetricsFacade(self.db_path)
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(client, "api_key", return_value="secret"), \
             mock.patch.object(client._OPENER, "open") as opened:
            with self.assertRaises(client.Interrupted):
                list(client.stream_chat(
                    "model-a", [], route=self.route, retries=1,
                    cancel=cancel, metrics=metrics,
                    trace_context={"request_id": "cancelled"}))
        opened.assert_not_called()
        row = metrics.get("cancelled")
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["error_kind"], "interrupted")
        self.assertIsNone(row["started_at"])
        self.assertIsNotNone(row["ended_at"])
        health = metrics.get_health("test", "model-a")
        self.assertEqual(health["failure_count"], 0)
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNotNone(health["last_interrupted"])

    def test_begin_failure_prevents_network(self):
        class BrokenMetrics:
            def route_decision(self, *args, **kwargs):
                return SimpleNamespace(allowed=True, warning=None)

            def begin_attempt(self, **kwargs):
                raise state.StateError("disk full before begin")

        with mock.patch.object(client._OPENER, "open") as opened:
            with self.assertRaisesRegex(state.StateError, "disk full"):
                list(client.stream_chat(
                    "model-a", [], route=self.route,
                    metrics=BrokenMetrics(), retries=3))
        opened.assert_not_called()

    def test_started_phase_failure_prevents_network(self):
        class BrokenTrace:
            def mark(self, phase, attempt):
                if phase == "started":
                    raise state.StateError("cannot persist started")
                return 0

            def finalize(self, *args, **kwargs):
                raise AssertionError("must not finalize after phase failure")

            def close(self):
                pass

        class Metrics:
            def route_decision(self, *args, **kwargs):
                return SimpleNamespace(allowed=True, warning=None)

            def begin_attempt(self, **kwargs):
                return BrokenTrace()

        with mock.patch.object(client, "api_key", return_value="secret"), \
             mock.patch.object(client._OPENER, "open") as opened:
            with self.assertRaisesRegex(state.StateError, "persist started"):
                list(client.stream_chat(
                    "model-a", [], route=self.route,
                    metrics=Metrics(), retries=3))
        opened.assert_not_called()

    def test_finalize_failure_stops_automatic_retry(self):
        class BrokenTrace:
            def mark(self, phase, attempt):
                return 0

            def finalize(self, *args, **kwargs):
                raise state.StateError("finalize failed")

            def close(self):
                pass

        class Metrics:
            def __init__(self):
                self.begins = 0

            def route_decision(self, *args, **kwargs):
                return SimpleNamespace(allowed=True, warning=None)

            def begin_attempt(self, **kwargs):
                self.begins += 1
                return BrokenTrace()

        metrics = Metrics()
        with mock.patch.object(client, "api_key", return_value="secret"), \
             mock.patch.object(
                 client._OPENER, "open", side_effect=OSError("down")) as opened, \
             mock.patch.object(client, "_wait_retry") as waited:
            with self.assertRaisesRegex(state.StateError, "finalize failed"):
                list(client.stream_chat(
                    "model-a", [], route=self.route,
                    metrics=metrics, retries=3))
        self.assertEqual(metrics.begins, 1)
        self.assertEqual(opened.call_count, 1)
        waited.assert_not_called()

    def test_success_finalize_failure_is_not_double_finalized(self):
        class BrokenTrace:
            def __init__(self):
                self.calls = []

            id = "broken-success"

            def mark(self, phase, attempt):
                return 0

            def finalize(self, status, **kwargs):
                self.calls.append(status)
                raise state.StateError("success commit failed")

            def close(self):
                pass

        trace = BrokenTrace()

        class Metrics:
            def route_decision(self, *args, **kwargs):
                return SimpleNamespace(allowed=True, warning=None)

            def begin_attempt(self, **kwargs):
                return trace

        with (
            mock.patch.object(client, "api_key", return_value="secret"),
            mock.patch.object(
                client._OPENER, "open", return_value=FakeResponse(
                    [sse(text_chunk("ok")), "data: [DONE]\n"])),
        ):
            with self.assertRaisesRegex(
                    state.StateError, "success commit failed"):
                list(client.stream_chat(
                    "model-a", [], route=self.route,
                    metrics=Metrics(), retries=1))
        self.assertEqual(trace.calls, ["ok"])

    def test_empty_and_partial_eof_are_failed_not_success(self):
        for label, lines in (
                ("empty", []),
                ("partial", [sse(text_chunk("half"))])):
            with self.subTest(case=label):
                path = Path(self.tmp.name) / f"{label}.sqlite3"
                metrics = state.MetricsFacade(path)
                with (
                    mock.patch.object(
                        client, "api_key", return_value="secret"),
                    mock.patch.object(
                        client._OPENER, "open",
                        return_value=FakeResponse(lines)),
                ):
                    with self.assertRaisesRegex(
                            client.APIError, "finish_reason"):
                        list(client.stream_chat(
                            "model-a", [], route=self.route, retries=1,
                            metrics=metrics,
                            trace_context={"request_id": f"eof-{label}"}))
                row = metrics.get(f"eof-{label}")
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["error_kind"], "stream_eof")
                health = metrics.get_health("test", "model-a")
                self.assertEqual(health["success_count"], 0)
                self.assertEqual(health["failure_count"], 1)

    def test_consumer_close_immediately_after_done_keeps_success(self):
        metrics = state.MetricsFacade(self.db_path)
        with (
            mock.patch.object(client, "api_key", return_value="secret"),
            mock.patch.object(
                client._OPENER, "open", return_value=FakeResponse(
                    [sse(text_chunk("ok")), "data: [DONE]\n"])),
        ):
            stream = client.stream_chat(
                "model-a", [], route=self.route, retries=1,
                metrics=metrics,
                trace_context={"request_id": "close-after-done"})
            self.assertEqual(next(stream)["t"], "text")
            self.assertEqual(next(stream)["t"], "done")
            stream.close()
        self.assertEqual(metrics.get("close-after-done")["status"], "ok")
        self.assertEqual(
            metrics.get_health("test", "model-a")["success_count"], 1)

    def test_incomplete_read_retries_only_before_visible_output(self):
        class BrokenResponse(FakeResponse):
            def __iter__(self):
                raise http.client.IncompleteRead(b"")

        metrics = state.MetricsFacade(self.db_path)
        responses = [
            BrokenResponse([]),
            FakeResponse([sse(text_chunk("ok")), "data: [DONE]\n"]),
        ]
        with (
            mock.patch.object(client, "api_key", return_value="secret"),
            mock.patch.object(
                client._OPENER, "open", side_effect=responses) as opened,
            mock.patch.object(client, "_wait_retry"),
        ):
            events = list(client.stream_chat(
                "model-a", [], route=self.route, retries=2,
                metrics=metrics,
                trace_context={"request_id": "incomplete"}))
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(events[-1]["t"], "done")
        rows = metrics.list_recent_traces(model="model-a")
        self.assertEqual(
            sorted(row["status"] for row in rows), ["failed", "ok"])
        failed = next(row for row in rows if row["status"] == "failed")
        self.assertTrue(failed["retryable"])

    def test_known_secret_in_http_error_is_absent_from_exception_and_db(self):
        secret = "sk-known-secret-m5-123456"
        body = {"error": {
            "code": secret,
            "message": f"gateway echoed {secret}",
        }}
        error = client.urllib.error.HTTPError(
            "http://example.invalid", 403, "forbidden",
            {"x-request-id": secret},
            io.BytesIO(json.dumps(body).encode()))
        metrics = state.MetricsFacade(self.db_path)
        with (
            mock.patch.dict(
                os.environ, {"DEEPINFER_API_KEY": secret}),
            mock.patch.object(client, "api_key", return_value=secret),
            mock.patch.object(
                client._OPENER, "open", side_effect=error),
        ):
            with self.assertRaises(client.APIError) as caught:
                list(client.stream_chat(
                    "model-a", [], route=self.route, retries=1,
                    metrics=metrics,
                    trace_context={"request_id": "secret-error"}))
        row = metrics.get("secret-error")
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, json.dumps(row, ensure_ascii=False))
        self.assertIn("[REDACTED_SECRET]", row["error_text"])


class HealthAndQueryTests(unittest.TestCase):
    def setUp(self):
        self.transport_policy = client.configure_transport_policy(
            allow_insecure_http=True)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "state.sqlite3"
        self.metrics = state.MetricsFacade(self.db_path)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        finally:
            client.restore_transport_policy(self.transport_policy)

    def failure(self, index, gateway="g", model="m"):
        trace = self.metrics.begin_attempt(
            gateway=gateway, model=model, attempt=index,
            request_id=f"failure-{gateway}-{model}-{index}")
        return trace.finalize(
            "failed", error=RuntimeError("temporary"),
            error_kind="network", retryable=True)

    def test_circuit_blocks_automatic_and_explicit_bypasses_with_warning(self):
        for index in range(1, 4):
            self.failure(index)
        automatic = self.metrics.route_decision("g", "m", explicit=False)
        explicit = self.metrics.route_decision("g", "m", explicit=True)
        self.assertFalse(automatic.allowed)
        self.assertTrue(explicit.allowed)
        self.assertTrue(explicit.bypassed)
        self.assertIn("circuit open until", explicit.warning)

    def test_client_route_api_blocks_auto_and_warns_on_explicit_bypass(self):
        for index in range(1, 4):
            self.failure(index)
        route = client.GatewayRoute(
            "g", "http://example.invalid/v1", ("TEST_KEY",))
        with mock.patch.object(client._OPENER, "open") as opened:
            with self.assertRaisesRegex(client.APIError, "circuit open"):
                list(client.stream_chat(
                    "m", [], route=route, metrics=self.metrics, retries=1))
        opened.assert_not_called()

        warnings = []
        with mock.patch.object(client, "api_key", return_value="secret"), \
             mock.patch.object(client._OPENER, "open", return_value=FakeResponse(
                 [sse(text_chunk("ok")), "data: [DONE]\n"])):
            list(client.stream_chat(
                "m", [], route=route, metrics=self.metrics, retries=1,
                route_explicit=True, route_warning=warnings.append))
        self.assertEqual(len(warnings), 1)
        self.assertIn("circuit open until", warnings[0])
        self.assertTrue(self.metrics.route_decision(
            "g", "m", explicit=False).allowed)

    def test_probe_ttl_and_catalog_invalidation_persist(self):
        first = self.metrics.record_probe_outcome(
            "g", "m", model_health.PROBE_SUCCESS, now=100,
            capability={"tools": True}, capability_source="probe")
        self.assertEqual(first["capability"], {"tools": True})
        self.assertFalse(self.metrics.probe_decision(
            "g", "m", now=100 + 86_399).due)
        self.assertTrue(self.metrics.probe_decision(
            "g", "m", now=100 + 86_400).due)

        rejected = self.metrics.record_probe_outcome(
            "g", "m", model_health.PROBE_CAPABILITY_REJECTION,
            catalog_fingerprint="catalog-a", now=200)
        self.assertEqual(
            rejected["probe_catalog_fingerprint"], "catalog-a")
        self.assertFalse(self.metrics.probe_decision(
            "g", "m", catalog_fingerprint="catalog-a",
            now=999_999).due)
        self.assertTrue(self.metrics.probe_decision(
            "g", "m", catalog_fingerprint="catalog-b", now=201).due)

    def test_context_probe_does_not_suppress_capability_probe_freshness(self):
        self.metrics.record_probe_outcome(
            "g", "m", model_health.PROBE_SUCCESS, now=100,
            capability={"tools": True},
            capability_source="capability_probe")
        after = self.metrics.record_probe_outcome(
            "g", "m", model_health.PROBE_TRANSIENT_FAILURE, now=200,
            capability={"context_probe_state": "blocked"},
            capability_source="context_probe",
            probe_kind="context_probe")

        self.assertEqual(after["probe_status"], model_health.PROBE_SUCCESS)
        self.assertEqual(
            after["context_probe_status"],
            model_health.PROBE_TRANSIENT_FAILURE)
        self.assertEqual(
            model_health.parse_utc(after["last_probe"]), 100)
        self.assertEqual(
            model_health.parse_utc(after["last_context_probe"]), 200)
        self.assertFalse(
            self.metrics.probe_decision("g", "m", now=101).due)

    def test_probe_recovery_survives_late_pre_recovery_completion(self):
        clock = MutableClock()
        metrics = state.MetricsFacade(self.db_path, clock=clock)

        def fail(request_id, at):
            clock.value = at
            trace = metrics.begin_attempt(
                gateway="recover", model="m", request_id=request_id)
            return trace.finalize(
                "failed", error=RuntimeError("temporary"),
                error_kind="network", retryable=True)

        for index, at in enumerate((10, 20, 30), 1):
            fail(f"before-{index}", at)
        self.assertFalse(metrics.route_decision(
            "recover", "m", explicit=False, now=31).allowed)
        metrics.record_probe_outcome(
            "recover", "m", model_health.PROBE_SUCCESS, now=40,
            probe_kind="capability_probe")
        self.assertTrue(metrics.route_decision(
            "recover", "m", explicit=False, now=41).allowed)

        # This terminal commits after the probe but its logical end was before
        # recovery; it contributes to lifetime failures, not the circuit tail.
        fail("late-old", 25)
        health = metrics.get_health("recover", "m")
        self.assertEqual(health["failure_count"], 4)
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertTrue(metrics.route_decision(
            "recover", "m", explicit=False, now=41).allowed)

        fail("after-1", 50)
        fail("after-2", 60)
        self.assertTrue(metrics.route_decision(
            "recover", "m", explicit=False, now=61).allowed)
        fail("after-3", 70)
        self.assertFalse(metrics.route_decision(
            "recover", "m", explicit=False, now=71).allowed)

    def test_compaction_failures_affect_circuit_not_chat_measurements(self):
        for index in range(1, 4):
            trace = self.metrics.begin_attempt(
                gateway="g", model="compact", attempt=index,
                request_id=f"compact-{index}",
                raw={"purpose": "context_compaction"})
            trace.finalize(
                "failed", error=RuntimeError("down"),
                error_kind="network", retryable=True)
        health = self.metrics.get_health("g", "compact")
        self.assertEqual(health["success_count"], 0)
        self.assertEqual(health["failure_count"], 0)
        self.assertEqual(health["consecutive_failures"], 3)
        self.assertFalse(self.metrics.route_decision(
            "g", "compact", explicit=False).allowed)

    def test_usage_summary_distinguishes_usage_and_cache_unknown(self):
        measured = self.metrics.begin_attempt(
            gateway="g", model="usage", request_id="usage-measured")
        measured.finalize(
            "ok", usage={"prompt_tokens": 12, "completion_tokens": 3})
        unknown = self.metrics.begin_attempt(
            gateway="g", model="usage", request_id="usage-unknown")
        unknown.finalize(
            "failed", error=RuntimeError("down"),
            error_kind="network", retryable=True)

        summary = self.metrics.usage_summary()
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["measured"], 1)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["prompt_tokens"], 12)
        self.assertEqual(summary["completion_tokens"], 3)
        self.assertEqual(summary["cache_measured"], 0)
        self.assertEqual(summary["cache_unknown"], 1)

    def test_trace_and_health_query_filters(self):
        for gateway, model, index in (("a", "m1", 1), ("a", "m2", 2),
                                      ("b", "m1", 3)):
            self.failure(index, gateway=gateway, model=model)
        self.assertEqual(len(self.metrics.list_recent_traces(gateway="a")), 2)
        self.assertEqual(len(self.metrics.list_recent_traces(model="m1")), 2)
        self.assertEqual(len(self.metrics.list_health(gateway="a")), 2)
        self.assertEqual(self.metrics.get("failure-a-m1-1")["model"], "m1")

    def test_concurrent_terminals_do_not_lose_health_updates(self):
        # Pre-create schema so this test isolates transactional update races.
        self.metrics.list_health()
        workers = 6
        per_worker = 8
        barrier = threading.Barrier(workers)
        failures = []

        def run(worker):
            try:
                barrier.wait()
                facade = state.MetricsFacade(self.db_path, timeout=10)
                for index in range(per_worker):
                    trace = facade.begin_attempt(
                        gateway="shared", model="model", attempt=index + 1,
                        request_id=f"worker-{worker}-{index}")
                    trace.finalize(
                        "failed", error=RuntimeError("temporary"),
                        error_kind="network", retryable=True)
            except BaseException as exc:  # make thread failures observable
                failures.append(exc)

        threads = [threading.Thread(target=run, args=(i,))
                   for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        rows = self.metrics.list_recent_traces(
            gateway="shared", model="model", limit=1000)
        health = self.metrics.get_health("shared", "model")
        expected = workers * per_worker
        self.assertEqual(len(rows), expected)
        self.assertEqual(health["failure_count"], expected)
        self.assertEqual(health["consecutive_failures"], expected)
        self.assertFalse(self.metrics.route_decision(
            "shared", "model", explicit=False).allowed)

    def test_concurrent_first_open_does_not_observe_schema_version_zero(self):
        fresh = Path(self.tmp.name) / "fresh.sqlite3"
        workers = 8
        barrier = threading.Barrier(workers)
        failures = []
        observed_modes = []

        def run(worker):
            facade = None
            try:
                barrier.wait()
                facade = state.MetricsFacade(fresh, timeout=10)
                trace = facade.begin_attempt(
                    gateway="fresh", model="model",
                    request_id=f"fresh-{worker}")
                trace.finalize("ok", usage={"prompt_tokens": 1})
                observed_modes.append(facade._shared().journal_mode)
            except BaseException as exc:
                failures.append(exc)
            finally:
                if facade is not None:
                    facade.close()

        threads = [threading.Thread(target=run, args=(i,))
                   for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(observed_modes, ["WAL"] * workers)
        facade = state.MetricsFacade(fresh)
        self.assertEqual(len(facade.list_recent_traces(limit=100)), workers)
        self.assertEqual(
            facade.get_health("fresh", "model")["success_count"], workers)

    def test_journal_mode_retries_locked_then_succeeds_without_downgrade(self):
        connection = JournalModeConnection([
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("database is busy"),
            "wal",
        ], read_modes=["delete", "delete"])
        clock = MutableClock()

        actual = state._set_journal_mode_with_retry(
            connection, "WAL", 1, clock=clock, sleeper=clock.sleep)

        self.assertEqual(actual, "WAL")
        self.assertEqual(
            connection.statements.count("PRAGMA journal_mode = WAL"), 3)
        self.assertNotIn("PRAGMA journal_mode = DELETE",
                         connection.statements)
        self.assertGreater(clock.value, 0)

    def test_journal_mode_retry_accepts_concurrent_winner(self):
        connection = JournalModeConnection([
            sqlite3.OperationalError("database is locked"),
        ], read_modes=["wal"])
        clock = MutableClock()

        actual = state._set_journal_mode_with_retry(
            connection, "WAL", 1, clock=clock, sleeper=clock.sleep)

        self.assertEqual(actual, "WAL")
        self.assertEqual(clock.value, 0)
        self.assertEqual(connection.statements, [
            "PRAGMA journal_mode = WAL",
            "PRAGMA journal_mode",
        ])

    def test_journal_mode_busy_timeout_fails_closed(self):
        connection = JournalModeConnection(
            [sqlite3.OperationalError("database is locked")] * 8,
            read_modes=["delete"] * 8)
        clock = MutableClock()

        with self.assertRaisesRegex(state.StateError, "locked/busy"):
            state._set_journal_mode_with_retry(
                connection, "WAL", 0.02,
                clock=clock, sleeper=clock.sleep)

        self.assertNotIn("PRAGMA journal_mode = DELETE",
                         connection.statements)
        self.assertGreaterEqual(clock.value, 0.02)

    def test_journal_mode_non_busy_error_propagates_without_retry(self):
        connection = JournalModeConnection([
            sqlite3.OperationalError("disk I/O error"),
        ])
        clock = MutableClock()

        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            state._set_journal_mode_with_retry(
                connection, "WAL", 1,
                clock=clock, sleeper=clock.sleep)

        self.assertEqual(connection.statements,
                         ["PRAGMA journal_mode = WAL"])
        self.assertEqual(clock.value, 0)

    def test_journal_mode_non_wal_result_keeps_delete_fallback(self):
        connection = JournalModeConnection(["delete", "delete"])
        clock = MutableClock()

        actual = state._configure_journal_mode(
            connection, "WAL", 1, clock=clock, sleeper=clock.sleep)

        self.assertEqual(actual, "DELETE")
        self.assertEqual(connection.statements, [
            "PRAGMA journal_mode = WAL",
            "PRAGMA journal_mode = DELETE",
        ])

    def test_tool_run_lifecycle_is_queryable(self):
        started = self.metrics.begin_tool_run({
            "id": "tool-1", "session_id": "s1", "turn_id": "t1",
            "name": "bash", "args_redacted": {"command": "<redacted>"},
            "approval_decision": "once", "approval_source": "user",
        })
        self.assertEqual(started["status"], "running")
        ended = self.metrics.finalize_tool_run(
            "tool-1", status="completed", exit_code=0)
        self.assertEqual(ended["status"], "completed")
        self.assertEqual(ended["exit_code"], 0)
        self.assertEqual(
            self.metrics.list_recent_tool_runs(session_id="s1")[0]["id"],
            "tool-1")

    def test_facade_runs_schema_bootstrap_once(self):
        fresh = Path(self.tmp.name) / "bootstrap-once.sqlite3"
        original = state.StateStore._create_schema
        calls = []

        def counted(instance):
            calls.append(instance.path)
            return original(instance)

        with mock.patch.object(
                state.StateStore, "_create_schema", new=counted):
            facade = state.MetricsFacade(fresh)
            facade.list_health()
            facade.list_recent_traces()
            facade.usage_summary()
            facade.route_decision("g", "m", explicit=False)
        self.assertEqual(calls, [fresh])

    def test_last_delta_phase_writes_are_throttled(self):
        class FakeDB:
            def __init__(self):
                self.updates = []
                self.closed = False

            def update_request_phase(self, request_id, field, at):
                self.updates.append((request_id, field, at))

            def close(self):
                self.closed = True

        values = iter([0.0] + [0.1 + index / 1000 for index in range(2000)])
        db = FakeDB()
        trace = state.RequestTrace(
            db, {"id": "rapid", "attempt": 1, "queued_at": "queued"},
            clock=lambda: next(values))
        trace.mark("first_delta", 1)
        for _ in range(2000):
            trace.mark("last_delta", 1)

        writes = [field for _, field, _ in db.updates]
        self.assertEqual(writes.count("first_delta_at"), 1)
        self.assertLess(writes.count("last_delta_at"), 6)
        self.assertEqual(trace.snapshot()["last_delta_at"], 2.099)
        trace.close()

    def test_killed_owner_rows_reconcile_without_touching_live_rows(self):
        crash_db = Path(self.tmp.name) / "crash.sqlite3"
        code = (
            "import sys,time\n"
            "from core.state import MetricsFacade\n"
            "f=MetricsFacade(sys.argv[1])\n"
            "t=f.begin_attempt(gateway='g',model='m',"
            "request_id='crashed-request')\n"
            "f.begin_tool_run({'id':'crashed-tool','session_id':'s',"
            "'name':'bash'})\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(crash_db)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

        recovered = state.MetricsFacade(crash_db)
        self.addCleanup(recovered.close)
        request = recovered.get("crashed-request")
        tool = recovered.get_tool_run("crashed-tool")
        self.assertEqual(request["status"], "interrupted")
        self.assertEqual(request["error_kind"], "process_crash")
        self.assertEqual(tool["status"], "unknown")
        summary = recovered.usage_summary()
        self.assertEqual(summary["running"], 0)
        self.assertEqual(summary["unknown"], 1)

        # A live owner in this process must survive a second facade bootstrap.
        live = recovered.begin_attempt(
            gateway="g", model="m", request_id="live-request")
        observer = state.MetricsFacade(crash_db)
        self.addCleanup(observer.close)
        self.assertEqual(observer.get("live-request")["status"], "running")
        live.finalize("interrupted", error_kind="interrupted")


class StoreFacadeTests(unittest.TestCase):
    def tearDown(self):
        store.reset_shadow_configuration()
        store.reset_metrics_configuration()

    def test_metrics_is_always_on_and_separate_from_shadow_migration_target(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
             mock.patch.object(store, "HOME", Path(tmp)):
            store.reset_shadow_configuration()
            store.reset_metrics_configuration()
            self.assertFalse(store.shadow_enabled())

            metrics = store.metrics_facade()
            trace = metrics.begin_attempt(
                gateway="g", model="m", request_id="before-shadow")
            trace.finalize("ok", usage={"prompt_tokens": 1})
            metrics_target = Path(tmp) / store.METRICS_DB_NAME
            state_target = Path(tmp) / store.STATE_DB_NAME
            self.assertTrue(metrics_target.is_file())
            # M5 must not create the future JSON-to-SQLite migration target.
            self.assertFalse(state_target.exists())
            self.assertFalse(store.shadow_enabled())

            store.configure_shadow(state_target, enabled=True)
            agent = SimpleNamespace(
                session_id="session-one", model="m", gateway="g",
                messages=[{"role": "system", "content": "sys"}],
                tokens_in=0, tokens_out=0, last_total=0, started=0,
            )
            self.assertEqual(store.shadow_ensure_agent(agent), 1)
            second = metrics.begin_attempt(
                gateway="g", model="m", session_id="session-one",
                request_id="with-shadow")
            second.finalize("ok", usage={"prompt_tokens": 2})

            with state.StateStore(state_target) as db:
                self.assertIsNotNone(db.get_session("session-one"))
                self.assertEqual(len(db.list_recent_traces()), 0)
            with state.StateStore(metrics_target) as db:
                self.assertEqual(len(db.list_recent_traces()), 2)
                self.assertEqual(
                    db.get_model_health("g", "m")["success_count"], 2)

    def test_legacy_tool_artifact_lookup_is_read_only_and_session_scoped(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
             mock.patch.object(store, "HOME", Path(tmp)):
            store.reset_metrics_configuration()
            metrics = store.metrics_facade()
            metrics.begin_tool_run({
                "id": "tool-legacy",
                "tool_call_id": "call-legacy",
                "session_id": "session-legacy",
                "name": "bash",
            })
            artifact = (
                Path(tmp) / ".zylab" / "artifacts"
                / "session-legacy" / "task01.stdout.log")
            metrics.finalize_tool_run(
                "tool-legacy", status="completed",
                stdout_path=artifact)
            target = Path(tmp) / store.METRICS_DB_NAME
            before = target.stat()

            matched = store.legacy_tool_artifact_rows(
                "session-legacy")
            missing = store.legacy_tool_artifact_rows(
                "another-session")
            after = target.stat()

        self.assertEqual(len(matched), 1)
        self.assertEqual(
            matched[0]["tool_call_id"], "call-legacy")
        self.assertEqual(matched[0]["stdout_path"], str(artifact))
        self.assertEqual(missing, [])
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
