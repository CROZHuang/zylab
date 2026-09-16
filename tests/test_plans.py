"""Session-scoped Updated Plan validation, tool events and projection."""
import unittest

from core import plans, tools


class RuntimeTask:
    def __init__(self):
        self.events = []

    def runtime_event(self, event):
        self.events.append(event)
        return True


class PlanTests(unittest.TestCase):
    def test_update_is_revisioned_and_semantic_noop_keeps_revision(self):
        items = [
            {"content": "inspect", "status": "completed"},
            {"content": "implement", "status": "in_progress"},
            {"content": "verify", "status": "pending"},
        ]
        first, changed = plans.updated(
            plans.empty(), items, explanation="scope established")
        repeated, repeated_changed = plans.updated(
            first, items, explanation="scope established")

        self.assertTrue(changed)
        self.assertEqual(first["revision"], 1)
        self.assertFalse(repeated_changed)
        self.assertEqual(repeated, first)
        self.assertEqual(plans.progress(first), (1, 3, "implement"))
        rendered = plans.plain_text(first)
        self.assertIn("Updated Plan", rendered)
        self.assertIn("✓ inspect", rendered)
        self.assertIn("◩ implement", rendered)
        self.assertIn("□ verify", rendered)

    def test_plan_rejects_multiple_active_items(self):
        with self.assertRaisesRegex(
                plans.PlanError, "最多只能有一个 in_progress"):
            plans.normalize_items([
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ])

    def test_todo_tool_emits_structured_runtime_event(self):
        task = RuntimeTask()
        result = tools.t_todo_write(
            [
                {"content": "inspect", "status": "completed"},
                {"content": "verify", "status": "in_progress"},
            ],
            explanation="tests are running",
            _task=task,
        )

        self.assertEqual(result, "[计划已更新：1/2 completed]")
        self.assertEqual(len(task.events), 1)
        self.assertEqual(task.events[0]["kind"], "plan_updated")
        self.assertEqual(
            task.events[0]["payload"]["explanation"],
            "tests are running")
        self.assertEqual(
            [item["status"] for item in task.events[0]["payload"]["items"]],
            ["completed", "in_progress"])


if __name__ == "__main__":
    unittest.main()
