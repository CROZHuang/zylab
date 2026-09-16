"""Session-scoped task-plan validation and projection.

The model-facing ``todo_write`` tool, canonical session JSON and TUI all use
this module so a plan has one shape and one set of invariants.  The module is
deliberately free of runtime imports; store/tools/UI can depend on it without
creating a cycle.
"""
from __future__ import annotations

import json


STATUSES = frozenset({"pending", "in_progress", "completed"})
MAX_ITEMS = 20
MAX_CONTENT = 500
MAX_EXPLANATION = 1000


class PlanError(ValueError):
    pass


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def normalize_items(value):
    """Return a bounded canonical list with at most one active item."""
    if not isinstance(value, list):
        raise PlanError("todos 必须是 array")
    if len(value) > MAX_ITEMS:
        raise PlanError(f"todos 最多 {MAX_ITEMS} 项")
    items = []
    active = 0
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise PlanError(f"todos[{index}] 必须是 object")
        content = str(raw.get("content") or "").strip()
        if not content:
            raise PlanError(f"todos[{index}].content 不能为空")
        if len(content) > MAX_CONTENT:
            raise PlanError(
                f"todos[{index}].content 最多 {MAX_CONTENT} 字符")
        status = str(raw.get("status") or "").strip()
        if status not in STATUSES:
            raise PlanError(
                f"todos[{index}].status 无效：{status!r}")
        active += status == "in_progress"
        items.append({"content": content, "status": status})
    if active > 1:
        raise PlanError("todo 计划最多只能有一个 in_progress 项")
    return items


def normalize_explanation(value):
    explanation = str(value or "").strip()
    if len(explanation) > MAX_EXPLANATION:
        raise PlanError(
            f"explanation 最多 {MAX_EXPLANATION} 字符")
    return explanation


def empty():
    return {"revision": 0, "explanation": "", "items": []}


def normalize_record(value):
    """Validate a canonical/session record; legacy item arrays are accepted."""
    if value in (None, ""):
        return empty()
    if isinstance(value, list):
        value = {"items": value}
    if not isinstance(value, dict):
        raise PlanError("task_plan 必须是 object")
    revision = value.get("revision", 0)
    if isinstance(revision, bool):
        raise PlanError("task_plan.revision 必须是非负整数")
    try:
        revision = int(revision)
    except (TypeError, ValueError, OverflowError):
        raise PlanError("task_plan.revision 必须是非负整数") from None
    if revision < 0:
        raise PlanError("task_plan.revision 必须是非负整数")
    return {
        "revision": revision,
        "explanation": normalize_explanation(value.get("explanation")),
        "items": normalize_items(value.get("items") or []),
    }


def updated(current, items, *, explanation=""):
    """Build the next revision, retaining identity for a semantic no-op."""
    current = normalize_record(current)
    next_items = normalize_items(items)
    next_explanation = normalize_explanation(explanation)
    if (current["items"] == next_items
            and current["explanation"] == next_explanation):
        return _copy(current), False
    return {
        "revision": current["revision"] + 1,
        "explanation": next_explanation,
        "items": next_items,
    }, True


def progress(value):
    record = normalize_record(value)
    total = len(record["items"])
    completed = sum(
        item["status"] == "completed" for item in record["items"])
    active = next((
        item["content"] for item in record["items"]
        if item["status"] == "in_progress"), "")
    return completed, total, active


def plain_text(value, *, title="Updated Plan"):
    record = normalize_record(value)
    marks = {"pending": "□", "in_progress": "◩", "completed": "✓"}
    rows = [title]
    if record["explanation"]:
        rows.append("  " + record["explanation"])
    if not record["items"]:
        rows.append("  └ (空)")
    for index, item in enumerate(record["items"]):
        branch = "└" if index == 0 else " "
        rows.append(
            f"  {branch} {marks[item['status']]} {item['content']}")
    return "\n".join(rows)
