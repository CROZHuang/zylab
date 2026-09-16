"""Exact boundary tests for the pure M5 model-health policy."""
import json
import os
import sys
import unittest
from datetime import datetime, timezone


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import model_health as health


class FakeClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class UtcPersistenceTests(unittest.TestCase):
    def test_utc_round_trip_preserves_microsecond_boundary(self):
        value = 1_750_000_000.123456
        encoded = health.format_utc(value)
        self.assertTrue(encoded.endswith("+00:00"))
        self.assertAlmostEqual(health.parse_utc(encoded), value, places=6)
        self.assertEqual(
            health.parse_utc("2026-08-25T00:00:00Z"),
            datetime(2026, 8, 25, tzinfo=timezone.utc).timestamp())

    def test_naive_or_nonfinite_times_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            health.parse_utc("2026-08-25T00:00:00")
        with self.assertRaisesRegex(ValueError, "finite"):
            health.format_utc(float("inf"))

    def test_records_are_json_serializable(self):
        record = health.record_probe_outcome(
            None, health.PROBE_SUCCESS, now=100)
        record = health.record_request_outcome(
            record, health.REQUEST_FAILURE,
            error_kind="network", retryable=True, now=101)
        self.assertEqual(json.loads(json.dumps(record)), record)
        self.assertTrue(record["last_probe"].endswith("+00:00"))


class ProbeFreshnessTests(unittest.TestCase):
    def test_success_ttl_exact_24h_boundary(self):
        record = health.record_probe_outcome(
            None, health.PROBE_SUCCESS, now=1_000)
        before = health.probe_decision(
            record, now=1_000 + 86_400 - 0.000001)
        boundary = health.probe_decision(record, now=1_000 + 86_400)
        self.assertFalse(before.due)
        self.assertEqual(before.reason, "fresh")
        self.assertTrue(boundary.due)
        self.assertEqual(boundary.reason, "expired")

    def test_transient_backoff_doubles_and_caps_at_one_hour(self):
        record = None
        delays = []
        for index in range(1, 7):
            record = health.record_probe_outcome(
                record, health.PROBE_TRANSIENT_FAILURE, now=10_000)
            delays.append(
                health.parse_utc(record["expires_at"]) - 10_000)
            self.assertEqual(record["probe_transient_failures"], index)
        self.assertEqual(delays, [300, 600, 1200, 2400, 3600, 3600])
        self.assertFalse(health.probe_decision(
            record, now=13_599.999999).due)
        self.assertTrue(health.probe_decision(record, now=13_600).due)

    def test_success_resets_transient_backoff(self):
        record = health.record_probe_outcome(
            None, health.PROBE_TRANSIENT_FAILURE, now=0)
        record = health.record_probe_outcome(
            record, health.PROBE_TRANSIENT_FAILURE, now=1)
        record = health.record_probe_outcome(
            record, health.PROBE_SUCCESS, now=2)
        record = health.record_probe_outcome(
            record, health.PROBE_TRANSIENT_FAILURE, now=3)
        self.assertEqual(record["probe_transient_failures"], 1)
        self.assertEqual(health.parse_utc(record["expires_at"]), 303)

    def test_capability_rejection_follows_catalog_fingerprint(self):
        record = health.record_probe_outcome(
            None, health.PROBE_CAPABILITY_REJECTION,
            catalog_fingerprint="catalog-a", now=50)
        same = health.probe_decision(
            record, catalog_fingerprint="catalog-a", now=10_000_000)
        changed = health.probe_decision(
            record, catalog_fingerprint="catalog-b", now=51)
        missing = health.probe_decision(record, now=51)
        forced = health.probe_decision(
            record, force=True, catalog_fingerprint="catalog-a", now=51)
        self.assertFalse(same.due)
        self.assertEqual(same.reason, "stable_capability_rejection")
        self.assertTrue(changed.due)
        self.assertEqual(changed.reason, "catalog_changed")
        self.assertTrue(missing.due)
        self.assertEqual(missing.reason, "catalog_fingerprint_missing")
        self.assertTrue(forced.due)
        self.assertEqual(forced.reason, "explicit_force")

    def test_stable_rejection_requires_fingerprint(self):
        with self.assertRaisesRegex(ValueError, "catalog_fingerprint"):
            health.record_probe_outcome(
                None, health.PROBE_CAPABILITY_REJECTION, now=0)

    def test_injected_clock_controls_probe_boundary(self):
        clock = FakeClock(200)
        record = health.record_probe_outcome(
            None, health.PROBE_SUCCESS, clock=clock)
        clock.value += 86_400
        self.assertTrue(health.probe_decision(record, clock=clock).due)

    def test_explicit_force_does_not_consult_clock(self):
        def broken_clock():
            raise AssertionError("force should bypass freshness clock")

        decision = health.probe_decision(
            {"probe_status": health.PROBE_SUCCESS},
            force=True, clock=broken_clock)
        self.assertTrue(decision.due)
        self.assertEqual(decision.reason, "explicit_force")

    def test_custom_probe_policy_is_honoured(self):
        policy = health.ModelHealthPolicy(
            probe_success_ttl=10,
            probe_transient_base_ttl=2,
            probe_transient_max_ttl=4,
            circuit_failure_threshold=2,
            circuit_open_ttl=7)
        record = health.record_probe_outcome(
            None, health.PROBE_SUCCESS, policy=policy, now=0)
        self.assertFalse(health.probe_decision(record, now=9.999999).due)
        self.assertTrue(health.probe_decision(record, now=10).due)


class CircuitPolicyTests(unittest.TestCase):
    def test_third_transient_failure_opens_for_ten_minutes(self):
        record = None
        for at in (100, 101):
            record = health.record_request_outcome(
                record, health.REQUEST_FAILURE,
                error_kind="network", retryable=True, now=at)
            self.assertIsNone(record.get("circuit_open_until"))
        record = health.record_request_outcome(
            record, health.REQUEST_FAILURE,
            error_kind="network", retryable=True, now=102)

        self.assertEqual(record["consecutive_failures"], 3)
        self.assertEqual(
            health.parse_utc(record["circuit_open_until"]), 702)
        before = health.route_decision(
            record, explicit=False, now=701.999999)
        boundary = health.route_decision(
            record, explicit=False, now=702)
        self.assertFalse(before.allowed)
        self.assertTrue(before.circuit_open)
        self.assertTrue(boundary.allowed)
        self.assertFalse(boundary.circuit_open)

    def test_explicit_route_bypasses_with_warning(self):
        record = {
            "circuit_open_until": health.format_utc(700),
            "last_error_kind": "rate_limit",
        }
        decision = health.route_decision(record, explicit=True, now=100)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.circuit_open)
        self.assertTrue(decision.bypassed)
        self.assertIn("circuit open until", decision.warning)
        self.assertIn("rate_limit", decision.warning)

    def test_excluded_errors_and_interruptions_do_not_advance_circuit(self):
        cases = [
            (health.REQUEST_FAILURE, "authentication", True),
            (health.REQUEST_FAILURE, "permission", True),
            (health.REQUEST_FAILURE, "invalid_request", True),
            (health.REQUEST_FAILURE, "context_length", True),
            (health.REQUEST_FAILURE, "network", False),
            (health.REQUEST_INTERRUPTED, "interrupted", True),
        ]
        record = None
        for index, (outcome, kind, retryable) in enumerate(cases):
            record = health.record_request_outcome(
                record, outcome, error_kind=kind,
                retryable=retryable, now=index)
        self.assertEqual(record["consecutive_failures"], 0)
        self.assertIsNone(record.get("circuit_open_until"))
        self.assertEqual(record["failure_count"], 5)
        self.assertIn("last_interrupted", record)

    def test_irrelevant_failure_does_not_break_transient_sequence(self):
        record = health.record_request_outcome(
            None, health.REQUEST_FAILURE,
            error_kind="network", retryable=True, now=0)
        record = health.record_request_outcome(
            record, health.REQUEST_FAILURE,
            error_kind="context_length", retryable=False, now=1)
        record = health.record_request_outcome(
            record, health.REQUEST_FAILURE,
            error_kind="rate_limit", retryable=True, now=2)
        self.assertEqual(record["consecutive_failures"], 2)

    def test_success_resets_counter_error_and_open_circuit(self):
        record = {
            "success_count": 2,
            "failure_count": 3,
            "consecutive_failures": 3,
            "last_error_kind": "network",
            "circuit_open_until": health.format_utc(1_000),
        }
        updated = health.record_request_outcome(
            record, health.REQUEST_SUCCESS, now=200)
        self.assertEqual(updated["success_count"], 3)
        self.assertEqual(updated["consecutive_failures"], 0)
        self.assertIsNone(updated["last_error_kind"])
        self.assertIsNone(updated["circuit_open_until"])
        self.assertTrue(health.route_decision(
            updated, explicit=False, now=200).allowed)

    def test_clock_and_custom_policy_control_exact_open_boundary(self):
        policy = health.ModelHealthPolicy(
            probe_success_ttl=10,
            probe_transient_base_ttl=2,
            probe_transient_max_ttl=4,
            circuit_failure_threshold=1,
            circuit_open_ttl=7)
        clock = FakeClock(10)
        record = health.record_request_outcome(
            None, health.REQUEST_FAILURE,
            error_kind="transient_http", retryable=True,
            policy=policy, clock=clock)
        clock.value = 16.999999
        self.assertFalse(health.route_decision(
            record, explicit=False, clock=clock).allowed)
        clock.value = 17
        self.assertTrue(health.route_decision(
            record, explicit=False, clock=clock).allowed)

    def test_invalid_persisted_circuit_fails_closed_but_explicit_can_bypass(self):
        automatic = health.route_decision(
            {"circuit_open_until": "broken"}, explicit=False, now=0)
        explicit = health.route_decision(
            {"circuit_open_until": "broken"}, explicit=True, now=0)
        self.assertFalse(automatic.allowed)
        self.assertTrue(automatic.circuit_open)
        self.assertTrue(explicit.allowed)
        self.assertTrue(explicit.bypassed)
        self.assertIn("invalid circuit_open_until", automatic.warning)


class ValidationTests(unittest.TestCase):
    def test_invalid_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            health.ModelHealthPolicy(probe_success_ttl=0)
        with self.assertRaises(ValueError):
            health.ModelHealthPolicy(
                probe_transient_base_ttl=10,
                probe_transient_max_ttl=5)
        with self.assertRaises(ValueError):
            health.ModelHealthPolicy(circuit_failure_threshold=0)
        with self.assertRaises(ValueError):
            health.ModelHealthPolicy(circuit_failure_threshold=1.5)

    def test_input_record_is_not_mutated(self):
        original = {
            "probe_status": health.PROBE_TRANSIENT_FAILURE,
            "probe_transient_failures": 1,
        }
        updated = health.record_probe_outcome(
            original, health.PROBE_TRANSIENT_FAILURE, now=0)
        self.assertEqual(original["probe_transient_failures"], 1)
        self.assertEqual(updated["probe_transient_failures"], 2)


if __name__ == "__main__":
    unittest.main()
