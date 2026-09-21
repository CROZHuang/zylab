"""Pure probe-freshness and model-circuit policy.

This module deliberately has no storage or networking dependency. Callers pass
in a JSON-compatible record and receive a new record or immutable decision.
Persisted times are UTC ISO-8601 strings; an injected clock makes exact policy
boundaries deterministic in tests.

Probe freshness and route health are separate: capability probes may be stale
while a route is healthy, and an explicit user route may bypass an open circuit
with a warning. This module never performs fallback routing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time
from typing import Callable, Mapping


Clock = Callable[[], float | datetime]

PROBE_SUCCESS = "success"
PROBE_TRANSIENT_FAILURE = "transient_failure"
PROBE_CAPABILITY_REJECTION = "capability_rejection"
PROBE_OUTCOMES = frozenset({
    PROBE_SUCCESS,
    PROBE_TRANSIENT_FAILURE,
    PROBE_CAPABILITY_REJECTION,
})

REQUEST_SUCCESS = "success"
REQUEST_FAILURE = "failure"
REQUEST_INTERRUPTED = "interrupted"
REQUEST_OUTCOMES = frozenset({
    REQUEST_SUCCESS,
    REQUEST_FAILURE,
    REQUEST_INTERRUPTED,
})

# These outcomes do not establish transient route health. They neither advance
# nor reset the consecutive transient-failure counter; only success proves that
# a route recovered.
NON_CIRCUIT_ERROR_KINDS = frozenset({
    "authentication",
    "permission",
    "invalid_request",
    "invalid_parameter",
    "context_length",
    "capability_temperature",
    "interrupted",
    "user_interrupted",
    "cancelled",
})


@dataclass(frozen=True)
class ModelHealthPolicy:
    """Configurable M5 health-policy durations, expressed in seconds."""

    probe_success_ttl: float = 24 * 60 * 60
    probe_transient_base_ttl: float = 5 * 60
    probe_transient_max_ttl: float = 60 * 60
    circuit_failure_threshold: int = 3
    # 熔断时长是**指数退避的基数**，不是固定值：第一次开 base，其后每多一次
    # 连续失败翻倍，上限 circuit_open_max_ttl。理由见 09-20 实测——唯一计入
    # 熔断的 transient_http 按定义会自愈，网关 2 分钟就恢复了，固定 600s 等于
    # 对一条已经健康的路由继续自我封锁 8 分钟。
    circuit_open_ttl: float = 30
    circuit_open_max_ttl: float = 10 * 60

    def __post_init__(self):
        durations = {
            "probe_success_ttl": self.probe_success_ttl,
            "probe_transient_base_ttl": self.probe_transient_base_ttl,
            "probe_transient_max_ttl": self.probe_transient_max_ttl,
            "circuit_open_ttl": self.circuit_open_ttl,
            "circuit_open_max_ttl": self.circuit_open_max_ttl,
        }
        for name, value in durations.items():
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        threshold = self.circuit_failure_threshold
        if (isinstance(threshold, bool)
                or not isinstance(threshold, int)
                or threshold < 1):
            raise ValueError(
                "circuit_failure_threshold must be an integer >= 1")
        if self.probe_transient_max_ttl < self.probe_transient_base_ttl:
            raise ValueError(
                "probe_transient_max_ttl must be >= probe_transient_base_ttl")
        if self.circuit_open_max_ttl < self.circuit_open_ttl:
            raise ValueError(
                "circuit_open_max_ttl must be >= circuit_open_ttl")


DEFAULT_POLICY = ModelHealthPolicy()


@dataclass(frozen=True)
class ProbeDecision:
    """Whether an automatic capability probe is due."""

    due: bool
    reason: str
    next_probe_at: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """Whether a route is usable under the circuit policy."""

    allowed: bool
    circuit_open: bool
    bypassed: bool = False
    warning: str | None = None
    circuit_open_until: str | None = None


def _epoch(value) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "UTC persistence requires a timezone-aware datetime")
        result = value.timestamp()
    else:
        result = float(value)
    if not math.isfinite(result):
        raise ValueError("timestamp must be finite")
    return result


def _now_epoch(*, now=None, clock: Clock | None = None) -> float:
    if now is not None:
        return _epoch(now)
    return _epoch((clock or time.time)())


def format_utc(value) -> str:
    """Return a microsecond UTC ISO-8601 timestamp for JSON persistence."""

    instant = datetime.fromtimestamp(_epoch(value), timezone.utc)
    return instant.isoformat(timespec="microseconds")


def parse_utc(value: str) -> float:
    """Parse an aware ISO-8601 timestamp into Unix seconds.

    A trailing Z is accepted for interoperability. Naive timestamps are
    rejected so persisted records cannot acquire the host's local timezone.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        instant = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("persisted timestamp must include a UTC offset")
    return _epoch(instant.astimezone(timezone.utc))


def _copy_record(record: Mapping | None) -> dict:
    if record is None:
        return {}
    if not isinstance(record, Mapping):
        raise TypeError("health record must be a mapping or None")
    return dict(record)


def _stored_epoch(record: Mapping, field: str) -> float | None:
    value = record.get(field)
    return None if value in (None, "") else parse_utc(value)


def record_probe_outcome(
        record: Mapping | None, outcome: str, *,
        catalog_fingerprint: str | None = None,
        policy: ModelHealthPolicy = DEFAULT_POLICY,
        now=None, clock: Clock | None = None) -> dict:
    """Return a copy updated with one capability-probe outcome.

    A stable capability rejection requires an exact catalog fingerprint, which
    provides the invalidation key that prevents a permanent stale skip.
    """

    outcome = str(outcome or "")
    if outcome not in PROBE_OUTCOMES:
        raise ValueError(f"unknown probe outcome: {outcome!r}")
    fingerprint = (
        str(catalog_fingerprint).strip()
        if catalog_fingerprint is not None else None)
    if outcome == PROBE_CAPABILITY_REJECTION and not fingerprint:
        raise ValueError(
            "capability_rejection requires a catalog_fingerprint")

    current = _copy_record(record)
    previous_status = current.get("probe_status")
    previous_failures = int(current.get("probe_transient_failures") or 0)
    at = _now_epoch(now=now, clock=clock)
    current.update({
        "probe_status": outcome,
        "last_probe": format_utc(at),
        "probe_catalog_fingerprint": fingerprint,
    })

    if outcome == PROBE_SUCCESS:
        current["probe_transient_failures"] = 0
        current["expires_at"] = format_utc(
            at + policy.probe_success_ttl)
    elif outcome == PROBE_TRANSIENT_FAILURE:
        failures = (
            previous_failures + 1
            if previous_status == PROBE_TRANSIENT_FAILURE else 1)
        delay = min(
            policy.probe_transient_base_ttl * (2 ** (failures - 1)),
            policy.probe_transient_max_ttl,
        )
        current["probe_transient_failures"] = failures
        current["expires_at"] = format_utc(at + delay)
    else:
        current["probe_transient_failures"] = 0
        current["expires_at"] = None
    return current


def probe_decision(
        record: Mapping | None, *, force: bool = False,
        catalog_fingerprint: str | None = None,
        now=None, clock: Clock | None = None) -> ProbeDecision:
    """Evaluate probe freshness without mutating the record.

    Explicit force always wins. Corrupt or incomplete freshness metadata is
    treated as due instead of creating a permanent skip.
    """

    if force:
        return ProbeDecision(True, "explicit_force")
    if not record:
        return ProbeDecision(True, "never_probed")
    at = _now_epoch(now=now, clock=clock)

    status = record.get("probe_status")
    if status == PROBE_CAPABILITY_REJECTION:
        stored = record.get("probe_catalog_fingerprint")
        current = (
            str(catalog_fingerprint).strip()
            if catalog_fingerprint is not None else None)
        if not stored or not current:
            return ProbeDecision(True, "catalog_fingerprint_missing")
        if str(stored) != current:
            return ProbeDecision(True, "catalog_changed")
        return ProbeDecision(False, "stable_capability_rejection")

    if status not in {PROBE_SUCCESS, PROBE_TRANSIENT_FAILURE}:
        return ProbeDecision(True, "unknown_probe_status")
    try:
        expires = _stored_epoch(record, "expires_at")
    except (TypeError, ValueError):
        return ProbeDecision(True, "invalid_expiry")
    if expires is None:
        return ProbeDecision(True, "missing_expiry")
    next_at = format_utc(expires)
    if at >= expires:
        return ProbeDecision(True, "expired", next_at)
    return ProbeDecision(False, "fresh", next_at)


def _counts_for_circuit(error_kind: str | None, retryable: bool) -> bool:
    kind = str(error_kind or "unknown").strip().lower()
    return bool(retryable) and kind not in NON_CIRCUIT_ERROR_KINDS


def record_request_outcome(
        record: Mapping | None, outcome: str, *,
        error_kind: str | None = None, retryable: bool = False,
        policy: ModelHealthPolicy = DEFAULT_POLICY,
        now=None, clock: Clock | None = None) -> dict:
    """Return a copy updated by one provider attempt.

    Failed attempts increment failure_count for observability. Only a retryable
    transient failure advances the circuit counter. Interruptions are user
    intent, not provider failures.
    """

    outcome = str(outcome or "")
    if outcome not in REQUEST_OUTCOMES:
        raise ValueError(f"unknown request outcome: {outcome!r}")
    current = _copy_record(record)
    at = _now_epoch(now=now, clock=clock)
    stamp = format_utc(at)
    current.setdefault("success_count", 0)
    current.setdefault("failure_count", 0)
    current.setdefault("consecutive_failures", 0)
    try:
        previous_outcome = _stored_epoch(current, "last_outcome_at")
    except (TypeError, ValueError):
        # Corrupt ordering evidence must not let an older completion mutate
        # the circuit tail.  Treat the existing row as newer/unknown.
        previous_outcome = math.inf
    latest = previous_outcome is None or at >= previous_outcome

    def update_latest_timestamp(field):
        try:
            previous = _stored_epoch(current, field)
        except (TypeError, ValueError):
            previous = None
        if previous is None or at >= previous:
            current[field] = stamp

    if outcome == REQUEST_SUCCESS:
        current["success_count"] = int(current["success_count"] or 0) + 1
        update_latest_timestamp("last_success")
        if not latest:
            return current
        current["consecutive_failures"] = 0
        current["last_error_kind"] = None
        current["circuit_open_until"] = None
        current["last_outcome_at"] = stamp
        return current

    if outcome == REQUEST_INTERRUPTED:
        update_latest_timestamp("last_interrupted")
        return current

    current["failure_count"] = int(current["failure_count"] or 0) + 1
    update_latest_timestamp("last_failure")
    if not latest:
        return current
    current["last_error_kind"] = str(error_kind or "unknown")
    current["last_outcome_at"] = stamp
    if not _counts_for_circuit(error_kind, retryable):
        return current

    failures = int(current["consecutive_failures"] or 0) + 1
    current["consecutive_failures"] = failures
    if failures >= policy.circuit_failure_threshold:
        # 指数退避：刚到阈值开 base，之后每多一次连续失败翻倍，封顶 max。
        # 瞬时抖动（503/网络）通常一两次就过去，短窗口让路由迅速回到可用；
        # 真正持续坏掉的路由才会被逐步拉长到上限。
        steps = failures - int(policy.circuit_failure_threshold)
        ttl = min(
            float(policy.circuit_open_ttl) * (2 ** max(0, steps)),
            float(policy.circuit_open_max_ttl))
        proposed = at + ttl
        try:
            existing = _stored_epoch(current, "circuit_open_until")
        except (TypeError, ValueError):
            existing = None
        current["circuit_open_until"] = format_utc(max(
            proposed, existing if existing is not None else proposed))
    return current


def route_decision(
        record: Mapping | None, *, explicit: bool,
        now=None, clock: Clock | None = None) -> RouteDecision:
    """Evaluate an existing circuit for explicit or automatic route use."""

    at = _now_epoch(now=now, clock=clock)
    if not record or not record.get("circuit_open_until"):
        return RouteDecision(True, False)
    try:
        until_epoch = _stored_epoch(record, "circuit_open_until")
    except (TypeError, ValueError):
        warning = "invalid circuit_open_until; automatic route blocked"
        if explicit:
            return RouteDecision(
                True, True, bypassed=True, warning=warning)
        return RouteDecision(False, True, warning=warning)
    if until_epoch is None or at >= until_epoch:
        return RouteDecision(True, False)

    until = format_utc(until_epoch)
    kind = str(record.get("last_error_kind") or "transient failure")
    # 子串 "circuit open until" 与错误类别是对外契约（测试与 UI 都依赖），
    # 倒计时只做追加：原始 UTC 时间戳对人没有意义，"还有 N 秒"才有。
    remaining = max(0, int(round(float(until_epoch) - float(at))))
    warning = (
        f"circuit open until {until} (还有 {remaining}s); last error: {kind}")
    if explicit:
        return RouteDecision(
            True, True, bypassed=True, warning=warning,
            circuit_open_until=until)
    return RouteDecision(
        False, True, warning=warning, circuit_open_until=until)


__all__ = [
    "DEFAULT_POLICY",
    "ModelHealthPolicy",
    "ProbeDecision",
    "RouteDecision",
    "PROBE_SUCCESS",
    "PROBE_TRANSIENT_FAILURE",
    "PROBE_CAPABILITY_REJECTION",
    "REQUEST_SUCCESS",
    "REQUEST_FAILURE",
    "REQUEST_INTERRUPTED",
    "format_utc",
    "parse_utc",
    "probe_decision",
    "record_probe_outcome",
    "record_request_outcome",
    "route_decision",
]
