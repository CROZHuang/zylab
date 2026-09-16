"""Persistent same-session goals and their small, auditable state machine.

``todo_write`` answers *which steps* are in a task.  A goal answers *what
must ultimately be true* and gives the interactive driver permission to keep
working after one provider turn.  The module deliberately contains no UI,
provider or filesystem code so the JSON session store and the controller can
share one validation boundary.

The durable record never contains process-local activation.  An active goal
is therefore safe to restore, but a newly started process must explicitly
``/goal resume`` before it may spend another provider request.  This mirrors
the useful part of Claude's goal lifecycle while keeping the cost boundary
visible in a local CLI.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone


PHASES = frozenset({"active", "paused", "blocked", "complete"})
ACTIVATIONS = frozenset({"armed", "disarmed"})
DEFAULT_MAX_ROUNDS = 20
MAX_ROUNDS = 256
MAX_OBJECTIVE = 4_000
MAX_EVIDENCE = 2_000
MAX_ERROR = 1_000
MAX_NEXT_STEP = 300
MAX_ID = 80
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
ROUND_OPEN = "<goal_round>"
ROUND_CLOSE = "</goal_round>"


class GoalError(ValueError):
    """Invalid goal input or lifecycle transition."""


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(value, field, maximum, *, required=False):
    text = str(value or "").strip()
    if required and not text:
        raise GoalError(f"goal {field} 不能为空")
    if len(text) > maximum:
        raise GoalError(f"goal {field} 最多 {maximum} 字符")
    return text


def _positive_int(value, field, *, maximum=None, default=None):
    if value in (None, "") and default is not None:
        value = default
    if isinstance(value, bool):
        raise GoalError(f"goal {field} 必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise GoalError(f"goal {field} 必须是正整数") from None
    # ``int(1.2)`` is a surprisingly easy way for a JSON caller to smuggle a
    # non-integral cap through; reject it rather than silently rounding.
    if isinstance(value, float) and value != number:
        raise GoalError(f"goal {field} 必须是正整数")
    if number < 1 or (maximum is not None and number > maximum):
        suffix = f"（1..{maximum}）" if maximum is not None else ""
        raise GoalError(f"goal {field} 必须是正整数{suffix}")
    return number


def _nonnegative_int(value, field, *, maximum=None, default=0):
    if value in (None, ""):
        value = default
    if isinstance(value, bool):
        raise GoalError(f"goal {field} 必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise GoalError(f"goal {field} 必须是非负整数") from None
    if isinstance(value, float) and value != number:
        raise GoalError(f"goal {field} 必须是非负整数")
    if number < 0 or (maximum is not None and number > maximum):
        suffix = f"（0..{maximum}）" if maximum is not None else ""
        raise GoalError(f"goal {field} 必须是非负整数{suffix}")
    return number


def _nonnegative_float(value, field, *, default=0.0):
    if value in (None, ""):
        value = default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise GoalError(f"goal {field} 必须是非负数字") from None
    if number < 0 or number != number or number == float("inf"):
        raise GoalError(f"goal {field} 必须是非负数字")
    return round(number, 3)


def _exact_positive_int(value, field):
    """Parse a CAS integer without accepting lossy JSON coercions."""
    if isinstance(value, bool):
        raise GoalError(f"goal {field} 必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise GoalError(f"goal {field} 必须是正整数") from None
    if isinstance(value, float) and value != number:
        raise GoalError(f"goal {field} 必须是正整数")
    if isinstance(value, str) and value.strip() != str(number):
        raise GoalError(f"goal {field} 必须是正整数")
    if number < 1:
        raise GoalError(f"goal {field} 必须是正整数")
    return number


def _timestamp(value, field, *, default=None):
    if value in (None, ""):
        value = default if default is not None else now()
    text = str(value).strip()
    if not text:
        raise GoalError(f"goal {field} 不能为空")
    # Timestamps are displayed and compared lexically only for diagnostics;
    # still reject control characters and absurdly long legacy values.
    if len(text) > 80 or any(ord(char) < 32 for char in text):
        raise GoalError(f"goal {field} 无效")
    return text


def _blocked_reason(value):
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise GoalError("goal blocked_reason 必须是 object")
    code = _text(value.get("code"), "blocked_reason.code", 80,
                 required=True)
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", code):
        raise GoalError("goal blocked_reason.code 必须是 lower-kebab-case")
    message = _text(value.get("message"), "blocked_reason.message",
                    MAX_ERROR, required=True)
    return {"code": code, "message": message}


def empty():
    """Return the absence value used by old session records."""
    return None


def is_round_prompt(value):
    """Identify the harness-owned continuation protocol in raw transcripts."""
    text = str(value or "").strip()
    return text.startswith(ROUND_OPEN + "\n") and text.endswith(ROUND_CLOSE)


def normalize_record(value):
    """Return a canonical durable record or ``None``.

    A small compatibility layer accepts the camel-case names used by the
    reference goal design (``maxGoalRounds``/``roundsStarted``) so an
    exported record can be inspected or restored without a migration script.
    Unknown fields are intentionally dropped: goal state is an allow-listed
    security/cost boundary, not an arbitrary model-controlled blob.
    """
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise GoalError("goal 必须是 object")
    raw_id = str(value.get("id") or "").strip()
    if not raw_id or len(raw_id) > MAX_ID or not _ID.fullmatch(raw_id):
        raise GoalError("goal id 无效")
    objective = _text(value.get("objective"), "objective", MAX_OBJECTIVE,
                      required=True)
    phase = str(value.get("phase") or "active").strip().lower()
    if phase not in PHASES:
        raise GoalError(f"goal phase 无效：{phase!r}")
    max_rounds = _positive_int(
        value.get("max_rounds", value.get(
            "max_goal_rounds", value.get("maxGoalRounds"))),
        "max_rounds", maximum=MAX_ROUNDS, default=DEFAULT_MAX_ROUNDS)
    rounds = _nonnegative_int(
        value.get("rounds_started", value.get("roundsStarted")),
        "rounds_started", maximum=max_rounds)
    revision = _positive_int(value.get("revision"), "revision")
    created = _timestamp(value.get("created_at", value.get("createdAt")),
                         "created_at")
    updated = _timestamp(value.get("updated_at", value.get("updatedAt")),
                         "updated_at", default=created)
    reason = _blocked_reason(
        value.get("blocked_reason", value.get("blockedReason")))
    if phase == "blocked" and reason is None:
        # Legacy blocked records occasionally only had a free-form error.
        legacy = _text(value.get("last_error"), "last_error", MAX_ERROR)
        reason = {"code": "legacy-blocked", "message": legacy or "goal 已阻塞"}
    if phase != "blocked":
        reason = None
    return {
        "id": raw_id,
        "revision": revision,
        "objective": objective,
        "phase": phase,
        "max_rounds": max_rounds,
        "rounds_started": rounds,
        "created_at": created,
        "updated_at": updated,
        "blocked_reason": reason,
        # 恢复会话的 agent 需要的不是「进度百分比」，是「下一步干什么」。
        # progress 报告顺带写下来，recap 直接渲染，零额外调用。
        "next_step": _text(value.get("next_step"), "next_step",
                           MAX_NEXT_STEP),
        "last_evidence": _text(value.get("last_evidence"),
                                 "last_evidence", MAX_EVIDENCE),
        "last_error": _text(value.get("last_error"), "last_error",
                             MAX_ERROR),
        "tokens_in": _nonnegative_int(value.get("tokens_in"), "tokens_in"),
        "tokens_out": _nonnegative_int(value.get("tokens_out"), "tokens_out"),
        "elapsed_seconds": _nonnegative_float(
            value.get("elapsed_seconds"), "elapsed_seconds"),
        "last_round_at": (
            _timestamp(value.get("last_round_at"), "last_round_at")
            if value.get("last_round_at") not in (None, "") else None),
    }


def new(objective, *, max_rounds=DEFAULT_MAX_ROUNDS, goal_id=None,
        timestamp=None):
    """Create a fresh active durable goal snapshot."""
    objective = _text(objective, "objective", MAX_OBJECTIVE, required=True)
    max_rounds = _positive_int(
        max_rounds, "max_rounds", maximum=MAX_ROUNDS,
        default=DEFAULT_MAX_ROUNDS)
    identifier = str(goal_id or f"goal-{uuid.uuid4().hex[:12]}").strip()
    if not _ID.fullmatch(identifier):
        raise GoalError("goal id 无效")
    stamp = _timestamp(timestamp, "created_at", default=now())
    return {
        "id": identifier,
        "revision": 1,
        "objective": objective,
        "phase": "active",
        "max_rounds": max_rounds,
        "rounds_started": 0,
        "created_at": stamp,
        "updated_at": stamp,
        "blocked_reason": None,
        "next_step": "",
        "last_evidence": "",
        "last_error": "",
        "tokens_in": 0,
        "tokens_out": 0,
        "elapsed_seconds": 0.0,
        "last_round_at": None,
    }


def copy(value):
    """Deep-copy a normalized record for callers that need isolation."""
    record = normalize_record(value)
    return _copy(record) if record is not None else None


def view(value, *, activation="disarmed"):
    """Return a UI/model-facing snapshot with process-local activation."""
    record = normalize_record(value)
    if record is None:
        return None
    activation = str(activation or "disarmed").lower()
    if activation not in ACTIVATIONS:
        raise GoalError(f"goal activation 无效：{activation!r}")
    return {**_copy(record), "activation": activation}


def assert_ref(value, goal_id=None, revision=None):
    """Require an exact compare-and-set reference for a mutation."""
    record = normalize_record(value)
    if record is None:
        raise GoalError("当前没有 goal")
    if str(goal_id or "") != record["id"]:
        raise GoalError("goal id 已过期，请先运行 /goal 查看当前状态")
    revision = _exact_positive_int(revision, "revision")
    if revision != record["revision"]:
        raise GoalError(
            f"goal revision 已过期（当前 {record['revision']}，收到 {revision}）")
    return record


def _next(record, *, phase=None, objective=None, max_rounds=None,
          evidence=None, last_error=None, blocked_reason=None, timestamp=None,
          next_step=None):
    current = normalize_record(record)
    if current is None:
        raise GoalError("当前没有 goal")
    next_record = _copy(current)
    if objective is not None:
        next_record["objective"] = _text(
            objective, "objective", MAX_OBJECTIVE, required=True)
    if max_rounds is not None:
        next_record["max_rounds"] = _positive_int(
            max_rounds, "max_rounds", maximum=MAX_ROUNDS)
        if next_record["max_rounds"] < next_record["rounds_started"]:
            raise GoalError("max_rounds 不能小于已开始的 round 数")
    if phase is not None:
        phase = str(phase).lower()
        if phase not in PHASES:
            raise GoalError(f"goal phase 无效：{phase!r}")
        next_record["phase"] = phase
    if evidence is not None:
        next_record["last_evidence"] = _text(
            evidence, "last_evidence", MAX_EVIDENCE)
    if next_step is not None:
        next_record["next_step"] = _text(
            next_step, "next_step", MAX_NEXT_STEP)
    if last_error is not None:
        next_record["last_error"] = _text(last_error, "last_error", MAX_ERROR)
    if next_record["phase"] == "blocked":
        reason = _blocked_reason(blocked_reason)
        if reason is None:
            raise GoalError("blocked goal 必须提供 blocked_reason")
        next_record["blocked_reason"] = reason
    else:
        next_record["blocked_reason"] = None
    next_record["revision"] = current["revision"] + 1
    next_record["updated_at"] = _timestamp(
        timestamp, "updated_at", default=now())
    return next_record


def edit(record, *, objective=None, max_rounds=None, timestamp=None):
    if objective is None and max_rounds is None:
        raise GoalError("goal edit 至少需要 objective 或 max_rounds")
    current = normalize_record(record)
    if current is None:
        raise GoalError("当前没有 goal")
    if current["phase"] == "complete":
        raise GoalError("已完成 goal 请直接用 /goal <新目标> 创建")
    return _next(current, objective=objective,
                 max_rounds=max_rounds, phase=current["phase"],
                 evidence=current["last_evidence"],
                 blocked_reason=current["blocked_reason"],
                 timestamp=timestamp)


def pause(record, *, timestamp=None):
    current = normalize_record(record)
    if current is None or current["phase"] != "active":
        raise GoalError("只有 active goal 可以 pause")
    return _next(current, phase="paused",
                 evidence=current["last_evidence"], timestamp=timestamp)


def resume(record, *, timestamp=None):
    current = normalize_record(record)
    if current is None:
        raise GoalError("当前没有 goal")
    if current["phase"] == "complete":
        raise GoalError("complete goal 不能 resume")
    if current["rounds_started"] >= current["max_rounds"]:
        raise GoalError(
            f"goal 已达到 round 上限 {current['max_rounds']}，请 edit 提高上限或创建新 goal")
    # ``active`` is the durable definition; arming is process-local.  Making
    # an already-active resume revise the record would invalidate a crash-safe
    # queued round and charge a second reservation for the same work.
    if current["phase"] == "active":
        return current
    return _next(current, phase="active",
                 evidence=current["last_evidence"], timestamp=timestamp)


def complete(record, *, evidence=None, timestamp=None):
    current = normalize_record(record)
    if current is None or current["phase"] == "complete":
        raise GoalError("goal 已经 complete 或不存在")
    return _next(current, phase="complete",
                 evidence=(current["last_evidence"]
                           if evidence is None else evidence),
                 timestamp=timestamp)


def block(record, *, code="model-reported", message=None, evidence=None,
          timestamp=None):
    current = normalize_record(record)
    if current is None or current["phase"] != "active":
        raise GoalError("只有 active goal 可以 block")
    reason = {"code": str(code or "model-reported"),
              "message": str(message or "goal 被阻塞")}
    return _next(current, phase="blocked",
                 blocked_reason=reason,
                 evidence=(current["last_evidence"]
                           if evidence is None else evidence),
                 timestamp=timestamp)


def note(record, *, evidence=None, last_error=None, timestamp=None,
         next_step=None):
    """Persist bounded progress evidence without changing the goal phase."""
    current = normalize_record(record)
    if current is None or current["phase"] != "active":
        raise GoalError("只有 active goal 可以记录 progress")
    return _next(current, phase=current["phase"],
                 next_step=(current["next_step"]
                            if next_step is None else next_step),
                 evidence=(current["last_evidence"]
                           if evidence is None else evidence),
                 last_error=(current["last_error"]
                             if last_error is None else last_error),
                 blocked_reason=current["blocked_reason"],
                 timestamp=timestamp)


def start_round(record, *, tokens_in=0, tokens_out=0, timestamp=None):
    """Reserve one bounded continuation round.

    Reservation is deliberately performed before the provider request.  A
    crash or provider failure therefore cannot silently restart forever after
    resume, and the hard cap remains a true cost boundary.
    """
    current = normalize_record(record)
    if current is None or current["phase"] != "active":
        raise GoalError("只有 active goal 可以开始 round")
    if current["rounds_started"] >= current["max_rounds"]:
        raise GoalError(
            f"goal 已达到 round 上限 {current['max_rounds']}")
    next_record = _copy(current)
    next_record["rounds_started"] += 1
    next_record["tokens_in"] += _nonnegative_int(tokens_in, "tokens_in")
    next_record["tokens_out"] += _nonnegative_int(tokens_out, "tokens_out")
    next_record["last_round_at"] = _timestamp(
        timestamp, "last_round_at", default=now())
    next_record["updated_at"] = next_record["last_round_at"]
    # Reservation is a progress fact, not a definition mutation: retaining
    # the revision keeps a goal-round tool call's CAS reference stable.
    return next_record


def add_usage(record, *, tokens_in=0, tokens_out=0, elapsed_seconds=0.0,
              timestamp=None):
    """Accumulate measured usage after a provider turn without revising id."""
    current = normalize_record(record)
    if current is None:
        return None
    next_record = _copy(current)
    next_record["tokens_in"] += _nonnegative_int(tokens_in, "tokens_in")
    next_record["tokens_out"] += _nonnegative_int(tokens_out, "tokens_out")
    next_record["elapsed_seconds"] = round(
        next_record["elapsed_seconds"]
        + _nonnegative_float(elapsed_seconds, "elapsed_seconds"), 3)
    next_record["updated_at"] = _timestamp(
        timestamp, "updated_at", default=now())
    return next_record


def prompt(record, round_number=None):
    """Render the bounded model-visible continuation instruction."""
    current = normalize_record(record)
    if current is None:
        raise GoalError("当前没有 goal")
    round_number = (current["rounds_started"]
                    if round_number is None else int(round_number))
    # JSON quoting protects whitespace and quotes, but an XML-like closing tag
    # inside a user objective would still terminate the harness-owned wrapper
    # for the model.  Keep the text semantically identical while preventing
    # that structural escape (the system goal projection uses the same rule).
    objective = json.dumps(
        current["objective"], ensure_ascii=False).replace("</", "<\\/")
    return (
        ROUND_OPEN + "\n"
        f"Objective: {objective}\n"
        f"Round: {round_number}/{current['max_rounds']}\n\n"
        "Continue working toward this objective in the same session. Treat the "
        "current workspace, tool results, task plan, and durable session state "
        "as authoritative; inspect them instead of trusting stale narration. "
        "Make concrete progress and verify it. When the whole objective is "
        "actually satisfied, call goal_update with the exact current id and "
        "revision, action=complete, and concise evidence. If work remains, "
        "leave the goal active and report the next concrete step. Use "
        "action=blocked only for a concrete blocker, not ordinary difficulty. "
        "Do not mark complete while managed/background tasks, workflows, or "
        "child agents still have unresolved work.\n"
        + ROUND_CLOSE
    )
