"""M3a Controller：状态、事件顺序、queue durability 与取消语义。"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import controller
from core import state


class IdFactory:
    def __init__(self):
        self.value = 0

    def __call__(self, kind):
        self.value += 1
        return f"{kind}-{self.value}"


class GateJournal:
    """可注入失败的 StateStore adapter，用来验证 fail-closed。"""

    def __init__(self, db):
        self.db = db
        self.fail_enqueue = False
        self.fail_dispatch = False
        self.fail_append = False

    def list_queued_inputs(self, *args, **kwargs):
        return self.db.list_queued_inputs(*args, **kwargs)

    def enqueue_input(self, *args, **kwargs):
        if self.fail_enqueue:
            raise RuntimeError("enqueue unavailable")
        return self.db.enqueue_input(*args, **kwargs)

    def dispatch_queued_input(self, *args, **kwargs):
        if self.fail_dispatch:
            raise RuntimeError("dispatch unavailable")
        return self.db.dispatch_queued_input(*args, **kwargs)

    def cancel_queued_input(self, *args, **kwargs):
        return self.db.cancel_queued_input(*args, **kwargs)

    def append_events(self, *args, **kwargs):
        if self.fail_append:
            raise RuntimeError("journal unavailable")
        return self.db.append_events(*args, **kwargs)


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = state.StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.db.upsert_session({
            "id": "s1",
            "title": "controller",
            "cwd": "/tmp/workspace/zylab",
            "model": "m",
            "gateway": "g",
            "created": "2026-08-24T00:00:00+00:00",
            "updated": "2026-08-24T00:00:00+00:00",
        })
        self.ids = IdFactory()
        self.ctrl = controller.SessionController(
            "s1", self.db, id_factory=self.ids,
            clock=lambda: "2026-08-24T00:00:01+00:00")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def kinds(self):
        return [row["kind"] for row in self.db.get_events("s1")]

    def test_steer_and_next_turn_have_distinct_safe_boundaries(self):
        first = self.ctrl.submit("first")
        self.assertEqual(first.kind, controller.ActionKind.START_TURN)
        self.assertEqual(
            [item.text for item in self.ctrl.drain_dispatched_inputs()],
            ["first"])
        self.assertEqual(self.ctrl.drain_dispatched_inputs(), ())
        first_turn = first.turn_id
        self.ctrl.begin_request(request_id="r1", model="m", gateway="g")

        self.assertIsNone(self.ctrl.submit("adjust"))
        self.assertIsNone(
            self.ctrl.submit("later", controller.QueueMode.NEXT_TURN))
        steer = self.ctrl.complete_response("answer", reason="stop")
        self.assertEqual(steer.kind, controller.ActionKind.DELIVER_STEER)
        self.assertEqual(steer.turn_id, first_turn)
        self.assertEqual(steer.item.text, "adjust")
        self.assertEqual(
            [item.text for item in self.ctrl.drain_dispatched_inputs()],
            ["adjust"])

        self.ctrl.begin_request(request_id="r2", model="m", gateway="g")
        next_turn = self.ctrl.complete_response("adjusted", reason="stop")
        self.assertEqual(next_turn.kind, controller.ActionKind.START_TURN)
        self.assertNotEqual(next_turn.turn_id, first_turn)
        self.assertEqual(next_turn.item.text, "later")
        self.assertEqual(
            [item.text for item in self.ctrl.drain_dispatched_inputs()],
            ["later"])
        self.assertEqual(self.kinds(), [
            "queued_input_added",
            "queued_input_dispatched",
            "user_message",
            "turn_started",
            "request_started",
            "queued_input_added",
            "queued_input_added",
            "assistant_message",
            "queued_input_dispatched",
            "user_message",
            "request_started",
            "assistant_message",
            "turn_completed",
            "queued_input_dispatched",
            "user_message",
            "turn_started",
        ])
        user_texts = [
            row["payload"]["message"]["content"]
            for row in self.db.get_events("s1")
            if row["kind"] == "user_message"
        ]
        self.assertEqual(user_texts, ["first", "adjust", "later"])

    def test_retrieve_cancels_latest_undispatched_item(self):
        self.ctrl.submit("first")
        self.ctrl.begin_request(request_id="r1")
        self.ctrl.submit("steer")
        self.ctrl.submit("later", controller.QueueMode.NEXT_TURN)

        item = self.ctrl.retrieve_latest()
        self.assertEqual(item.text, "later")
        self.assertEqual([queued.text for queued in self.ctrl.queued], ["steer"])
        row = self.db.get_queued_input("s1", item.id)
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(self.kinds()[-1], "queued_input_cancelled")

    def test_queued_local_command_emits_one_dispatch_notification(self):
        self.ctrl.submit("first")
        self.ctrl.drain_dispatched_inputs()
        self.ctrl.begin_request(request_id="r1")
        self.assertIsNone(self.ctrl.submit(
            "/status", controller.QueueMode.COMMAND))

        action = self.ctrl.complete_response("done", reason="stop")

        self.assertEqual(action.kind, controller.ActionKind.RUN_COMMAND)
        self.assertEqual(
            [item.text for item in self.ctrl.drain_dispatched_inputs()],
            ["/status"])
        self.assertEqual(self.ctrl.drain_dispatched_inputs(), ())

    def test_cancel_intent_is_durable_before_response_close(self):
        self.ctrl.submit("first")
        start = self.ctrl.begin_request(request_id="r1")

        class Probe:
            def __init__(probe_self):
                probe_self.kind_seen_at_close = None

            def close(probe_self):
                probe_self.kind_seen_at_close = self.kinds()[-1]

        response = Probe()
        start.cancel.bind(response)
        action = self.ctrl.request_cancel()
        self.assertEqual(action.kind, controller.ActionKind.CANCEL_REQUEST)
        self.assertEqual(
            response.kind_seen_at_close, "request_cancel_requested")
        self.assertTrue(self.ctrl.cancelling)

    def test_interrupt_delivers_steer_in_same_turn_and_keeps_next(self):
        first = self.ctrl.submit("first")
        self.ctrl.begin_request(request_id="r1")
        self.ctrl.submit("adjust")
        self.ctrl.submit("later", controller.QueueMode.NEXT_TURN)
        self.ctrl.request_cancel()

        action = self.ctrl.acknowledge_interrupt("partial")
        self.assertEqual(action.kind, controller.ActionKind.DELIVER_STEER)
        self.assertEqual(action.turn_id, first.turn_id)
        self.assertEqual([item.text for item in self.ctrl.queued], ["later"])
        self.assertNotIn("turn_failed", self.kinds())
        self.assertEqual(self.kinds()[-3:], [
            "request_interrupted",
            "queued_input_dispatched",
            "user_message",
        ])

    def test_interrupt_without_steer_closes_turn_then_dispatches_next(self):
        first = self.ctrl.submit("first")
        self.ctrl.begin_request(request_id="r1")
        self.ctrl.submit("later", controller.QueueMode.NEXT_TURN)
        self.ctrl.request_cancel()

        action = self.ctrl.acknowledge_interrupt("partial")
        self.assertEqual(action.kind, controller.ActionKind.START_TURN)
        self.assertNotEqual(action.turn_id, first.turn_id)
        self.assertEqual(self.kinds()[-5:], [
            "request_interrupted",
            "turn_failed",
            "queued_input_dispatched",
            "user_message",
            "turn_started",
        ])

    def test_tool_intent_is_persisted_before_start_action(self):
        self.ctrl.submit("first")
        self.ctrl.begin_request(request_id="r1")
        call = {
            "id": "tc1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
        review = self.ctrl.complete_response(
            {"role": "assistant", "content": "", "tool_calls": [call]})
        self.assertEqual(review.kind, controller.ActionKind.REVIEW_TOOLS)
        self.assertEqual(
            self.ctrl.state, controller.ControllerState.TOOL_WAITING_APPROVAL)

        start = self.ctrl.decide_tool(
            "tc1", allowed=True, decision="once", source="user")
        self.assertEqual(start.kind, controller.ActionKind.START_TOOL)
        self.assertEqual(self.kinds()[-1:], ["permission_decided"])
        self.assertEqual(
            self.ctrl.state, controller.ControllerState.TOOL_WAITING_APPROVAL)
        self.ctrl.record_event(
            "checkpoint_created", {"checkpoint_id": "checkpoint-1"})
        self.ctrl.start_tool("tc1")
        self.assertEqual(self.kinds()[-3:], [
            "permission_decided", "checkpoint_created", "tool_started"])
        follow = self.ctrl.finish_tool("file contents")
        self.assertEqual(follow.kind, controller.ActionKind.CONTINUE_TURN)
        self.assertEqual(self.kinds()[-1], "tool_finished")
        self.assertEqual(
            self.ctrl.state, controller.ControllerState.MODEL_STREAMING)

    def test_tool_cancel_intent_can_precede_task_creation(self):
        self.ctrl.submit("first")
        self.ctrl.begin_request(request_id="r1")
        call = {
            "id": "tc-cancel",
            "type": "function",
            "function": {"name": "bash", "arguments": "{}"},
        }
        self.ctrl.complete_response(
            {"role": "assistant", "content": "", "tool_calls": [call]})
        self.ctrl.decide_tool(
            "tc-cancel", allowed=True, decision="once", source="user")
        self.ctrl.start_tool("tc-cancel")

        action = self.ctrl.request_cancel()
        self.assertEqual(
            action.kind, controller.ActionKind.CANCEL_TOOL)
        self.assertEqual(
            self.kinds()[-1], "tool_cancel_requested")
        self.ctrl.register_tool_task("tc-cancel", "task-1")

        self.assertEqual(self.kinds()[-2:], [
            "tool_cancel_requested", "task_started"])
        last = self.db.get_events("s1")[-1]
        self.assertTrue(
            last["payload"]["cancel_requested"])

    def test_cancelled_tool_finish_clears_task_state(self):
        self.ctrl.submit("first")
        self.ctrl.begin_request(request_id="r1")
        call = {
            "id": "tc-cancel",
            "type": "function",
            "function": {"name": "bash", "arguments": "{}"},
        }
        self.ctrl.complete_response(
            {"role": "assistant", "content": "", "tool_calls": [call]})
        self.ctrl.decide_tool(
            "tc-cancel", allowed=True, decision="once", source="user")
        self.ctrl.start_tool("tc-cancel")
        self.ctrl.register_tool_task("tc-cancel", "task-1")
        self.ctrl.request_cancel()

        action = self.ctrl.finish_tool(
            "[用户中断]", status="cancelled",
            details={"task_id": "task-1"})

        self.assertEqual(
            action.kind, controller.ActionKind.CONTINUE_TURN)
        self.assertFalse(self.ctrl.tool_cancelling)
        self.assertIsNone(self.ctrl.current_task_id)
        self.assertIsNone(self.ctrl.current_tool_call_id)
        self.assertEqual(
            self.ctrl.state,
            controller.ControllerState.MODEL_STREAMING)

    def test_invalid_tool_calls_do_not_mutate_active_request(self):
        self.ctrl.submit("first")
        self.ctrl.begin_request(request_id="r1")
        call = {
            "id": "same",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [call, dict(call)],
        }
        with self.assertRaisesRegex(
                controller.ControllerError, "重复 tool_call_id"):
            self.ctrl.complete_response(message)
        self.assertEqual(self.ctrl.current_request_id, "r1")
        self.assertEqual(self.kinds()[-1], "request_started")


    def test_dispatch_transaction_rolls_back_user_and_turn_on_failure(self):
        self.db._conn.executescript(
            """
            CREATE TRIGGER reject_user
            BEFORE INSERT ON events
            WHEN NEW.kind = 'user_message'
            BEGIN
                SELECT RAISE(ABORT, 'reject user');
            END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ctrl.submit("first")

        self.assertEqual(self.ctrl.state, controller.ControllerState.IDLE)
        self.assertEqual(len(self.ctrl.queued), 1)
        self.assertEqual(self.kinds(), ["queued_input_added"])
        queued = self.db.list_queued_inputs("s1")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["state"], "queued")

        self.db._conn.execute("DROP TRIGGER reject_user")
        action = self.ctrl.dispatch_ready()
        self.assertEqual(action.kind, controller.ActionKind.START_TURN)

    def test_enqueue_failure_does_not_mutate_memory(self):
        journal = GateJournal(self.db)
        journal.fail_enqueue = True
        ctrl = controller.SessionController(
            "s1", journal, id_factory=IdFactory())
        with self.assertRaisesRegex(RuntimeError, "enqueue unavailable"):
            ctrl.submit("first")
        self.assertEqual(ctrl.state, controller.ControllerState.IDLE)
        self.assertEqual(ctrl.queued, ())
        self.assertEqual(self.kinds(), [])

    def test_dispatch_failure_leaves_durable_queue_and_no_turn(self):
        journal = GateJournal(self.db)
        journal.fail_dispatch = True
        ctrl = controller.SessionController(
            "s1", journal, id_factory=IdFactory())
        with self.assertRaisesRegex(RuntimeError, "dispatch unavailable"):
            ctrl.submit("first")
        self.assertEqual(ctrl.state, controller.ControllerState.IDLE)
        self.assertEqual(len(ctrl.queued), 1)
        self.assertEqual(ctrl.drain_dispatched_inputs(), ())
        self.assertEqual(self.kinds(), ["queued_input_added"])
        self.assertEqual(
            self.db.list_queued_inputs("s1")[0]["state"], "queued")

        journal.fail_dispatch = False
        restored = controller.SessionController(
            "s1", journal, id_factory=IdFactory())
        self.assertEqual([item.text for item in restored.queued], ["first"])
        action = restored.dispatch_ready()
        self.assertEqual(action.kind, controller.ActionKind.START_TURN)
        self.assertEqual(
            [item.text for item in restored.drain_dispatched_inputs()],
            ["first"])
        self.assertEqual(
            self.db.list_queued_inputs("s1", state=None)[0]["state"], "dispatched")


    def test_restored_orphan_steer_is_not_silently_retyped(self):
        self.ctrl.submit("first")
        self.ctrl.begin_request(request_id="r1")
        self.ctrl.submit("adjust")

        restored = controller.SessionController(
            "s1", self.db, id_factory=IdFactory())
        self.assertIsNone(restored.dispatch_ready())
        self.assertEqual([item.text for item in restored.queued], ["adjust"])
        recalled = restored.retrieve_latest(controller.QueueMode.STEER)
        self.assertEqual(recalled.text, "adjust")
        self.assertEqual(restored.queued, ())


    def test_request_journal_failure_returns_no_side_effect_handle(self):
        journal = GateJournal(self.db)
        ctrl = controller.SessionController(
            "s1", journal, id_factory=IdFactory())
        ctrl.submit("first")
        journal.fail_append = True
        with self.assertRaisesRegex(RuntimeError, "journal unavailable"):
            ctrl.begin_request(request_id="r1")
        self.assertIsNone(ctrl.current_request_id)
        self.assertIsNone(ctrl.request_handle)

    def test_modal_api_rejects_non_modal_and_restores_idle(self):
        with self.assertRaises(controller.ControllerError):
            self.ctrl.open_modal(
                controller.ControllerState.TOOL_WAITING_APPROVAL)
        self.assertEqual(self.ctrl.state, controller.ControllerState.IDLE)
        self.ctrl.open_modal(controller.ControllerState.PICKER)
        self.assertEqual(self.ctrl.state, controller.ControllerState.PICKER)
        self.ctrl.close_modal()
        self.assertEqual(self.ctrl.state, controller.ControllerState.IDLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
