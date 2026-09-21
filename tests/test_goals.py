"""Durable /goal state, queue gating and model-facing update boundaries."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab as CLI
from core import controller, goals, memory, store, tools
from tests.pty_harness import (
    PTYSend, install_ready_input_pump, run_pty_child)


class GoalStateTests(unittest.TestCase):
    def make_goal(self, **kwargs):
        return goals.new(
            "完成一个可验证的研究任务",
            goal_id="goal-test-1",
            timestamp="2026-08-31T00:00:00+00:00",
            **kwargs,
        )

    def test_lifecycle_is_revisioned_and_cas_is_exact(self):
        first = self.make_goal(max_rounds=3)
        reserved = goals.start_round(
            first, timestamp="2026-08-31T00:00:01+00:00")
        self.assertEqual(reserved["revision"], first["revision"])
        self.assertEqual(reserved["rounds_started"], 1)

        progress = goals.note(reserved, evidence="测试已通过")
        self.assertEqual(progress["revision"], 2)
        self.assertEqual(progress["last_evidence"], "测试已通过")
        with self.assertRaisesRegex(goals.GoalError, "revision"):
            goals.assert_ref(progress, first["id"], first["revision"])

        paused = goals.pause(progress)
        self.assertEqual(paused["phase"], "paused")
        resumed = goals.resume(paused)
        self.assertEqual(resumed["phase"], "active")
        complete = goals.complete(resumed, evidence="验收完成")
        self.assertEqual(complete["phase"], "complete")
        self.assertEqual(complete["last_evidence"], "验收完成")

    def test_round_cap_and_blocked_edit(self):
        first = self.make_goal(max_rounds=1)
        used = goals.start_round(first)
        with self.assertRaisesRegex(goals.GoalError, "round 上限"):
            goals.start_round(used)

        blocked = goals.block(
            first, code="provider-timeout", message="网关暂时不可用")
        self.assertEqual(blocked["phase"], "blocked")
        edited = goals.edit(blocked, objective="换一个可验证目标")
        self.assertEqual(edited["phase"], "blocked")
        self.assertEqual(edited["blocked_reason"]["code"], "provider-timeout")
        with self.assertRaisesRegex(goals.GoalError, "active"):
            goals.note(blocked, evidence="不应修改 blocked goal")

    def test_resuming_an_already_active_goal_is_idempotent(self):
        active = self.make_goal(max_rounds=3)
        resumed = goals.resume(active)
        self.assertEqual(resumed, active)
        self.assertEqual(resumed["revision"], active["revision"])

    def test_prompt_preserves_objective_as_data(self):
        record = goals.new(
            '检查 "quoted" 与 <tag>', goal_id="goal-prompt-1",
            timestamp="2026-08-31T00:00:00+00:00")
        prompt = goals.prompt(record, 1)
        self.assertIn("<goal_round>", prompt)
        self.assertIn(json.dumps(record["objective"], ensure_ascii=False), prompt)
        self.assertIn("goal_update", prompt)

    def test_prompt_objective_cannot_close_internal_wrapper(self):
        record = goals.new(
            "keep </goal_round> literal", goal_id="goal-prompt-escape",
            timestamp="2026-08-31T00:00:00+00:00")
        prompt = goals.prompt(record, 1)
        self.assertEqual(prompt.count(goals.ROUND_CLOSE), 1)
        self.assertIn(r"<\/goal_round>", prompt)

    def test_incomplete_marker_prefix_remains_human_data(self):
        self.assertFalse(goals.is_round_prompt(
            "<goal_round> is a tag I am asking about"))

    def test_cas_rejects_lossy_revision_representations(self):
        record = self.make_goal()
        for value in (True, 1.5, "01"):
            with self.subTest(value=value):
                with self.assertRaises(goals.GoalError):
                    goals.assert_ref(record, record["id"], value)

    def test_validation_rejects_unbounded_or_malformed_records(self):
        record = self.make_goal()
        with self.assertRaises(goals.GoalError):
            goals.normalize_record({**record, "max_rounds": 0})
        with self.assertRaises(goals.GoalError):
            goals.normalize_record({**record, "id": "../escape"})
        with self.assertRaises(goals.GoalError):
            goals.normalize_record({**record, "phase": "blocked",
                                    "blocked_reason": {"code": "Bad", "message": "x"}})

    def test_reference_camel_case_round_fields_are_accepted(self):
        record = self.make_goal()
        record.pop("max_rounds")
        record.pop("rounds_started")
        record["maxGoalRounds"] = 7
        record["roundsStarted"] = 2
        normalized = goals.normalize_record(record)
        self.assertEqual(normalized["max_rounds"], 7)
        self.assertEqual(normalized["rounds_started"], 2)


class GoalQueueTests(unittest.TestCase):
    def test_internal_goal_queue_round_trips_and_is_gated(self):
        journal = controller.MemoryJournal()
        first = controller.SessionController("s-goal", journal)
        action = first.submit("human turn")
        self.assertEqual(action.kind, controller.ActionKind.START_TURN)
        first.submit_goal("<goal_round> continue", metadata={
            "goal_id": "goal-test-1", "revision": 1, "round": 1})
        first.submit("human later", controller.QueueMode.NEXT_TURN)

        rows = journal.list_queued_inputs("s-goal", state="queued")
        self.assertEqual(len(rows), 2)
        raw_goal_rows = [
            row for row in rows
            if row["text"].startswith("\x00zylab-internal-v1:")
        ]
        self.assertEqual(len(raw_goal_rows), 1)

        recovered = controller.SessionController("s-goal", journal)
        recovered.internal_dispatch_enabled = False
        human = recovered.dispatch_ready()
        self.assertEqual(human.item.text, "human later")
        self.assertEqual(human.item.origin, "user")
        recovered.begin_request(request_id="r-human")
        self.assertIsNone(recovered.complete_response("done"))

        # The held goal row becomes dispatchable only after the explicit gate.
        self.assertIsNone(recovered.dispatch_ready())
        recovered.internal_dispatch_enabled = True
        goal_action = recovered.dispatch_ready()
        self.assertEqual(goal_action.item.origin, "goal")
        self.assertEqual(goal_action.item.metadata["round"], 1)

    def test_malformed_internal_envelope_fails_closed(self):
        with self.assertRaises(controller.ControllerError):
            controller.QueuedInput.from_row({
                "id": "i1", "mode": "next_turn", "enqueued_at": "now",
                "text": "\x00zylab-internal-v1:{not-json}",
            })

    def test_internal_metadata_must_be_json_serializable(self):
        journal = controller.MemoryJournal()
        session = controller.SessionController("s-goal-meta", journal)
        with self.assertRaises(controller.ControllerError):
            session.submit_goal("<goal_round>", metadata={"bad": {1, 2}})

    def test_session_driver_reserves_rounds_and_stops_at_cap(self):
        class Agent:
            session_id = "goal-session"
            tokens_in = 0
            tokens_out = 0

            def set_goal_context(self, value):
                self.goal_context = value

        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = None
        sess._goal_activation = "disarmed"
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        sess._queue_display = {}
        sess._skip_queue_display = set()
        sess._session_cwd = os.getcwd()
        sess.plan_mode = False
        sess._workflow_current = None
        sess.controller = controller.SessionController(
            sess.ag.session_id, controller.MemoryJournal())
        sess.save = lambda: None
        sess.cwd_switch_blocker = lambda: None

        created = CLI.Session.create_goal(sess, "验证两轮后停止", max_rounds=2)
        self.assertEqual(created["activation"], "armed")
        first = CLI.Session.dispatch_goal_ready(sess)
        self.assertEqual(first.item.origin, "goal")
        self.assertEqual(sess.goal["rounds_started"], 1)
        sess.controller.begin_request(request_id="r-goal-1")
        self.assertIsNone(sess.controller.complete_response("progress"))
        self.assertIsNone(CLI.Session.goal_turn_finished(
            sess, first.item, SimpleNamespace(status="ok")))

        second = CLI.Session.dispatch_goal_ready(sess)
        self.assertEqual(second.item.metadata["round"], 2)
        sess.controller.begin_request(request_id="r-goal-2")
        self.assertIsNone(sess.controller.complete_response("done"))
        notice = CLI.Session.goal_turn_finished(
            sess, second.item, SimpleNamespace(status="ok"))
        self.assertIn("round 上限", notice)
        self.assertEqual(sess.goal["phase"], "blocked")
        self.assertIsNone(CLI.Session.dispatch_goal_ready(sess))

    def test_old_round_cannot_mutate_a_replaced_goal(self):
        class Agent:
            session_id = "goal-replace"
            tokens_in = 10
            tokens_out = 20

            def set_goal_context(self, value):
                self.goal_context = value

        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = goals.new("new objective", goal_id="new-goal")
        sess._goal_activation = "armed"
        sess._goal_round_items = {
            "old-item": {"goal_id": "old-goal", "revision": 1, "round": 1}
        }
        sess._goal_round_started = {
            "old-item": {"tokens_in": 0, "tokens_out": 0,
                          "started_at": 0.0}
        }
        sess.save = lambda: None
        sess._goal_audit = lambda *args, **kwargs: None
        item = SimpleNamespace(id="old-item", origin="goal")
        result = CLI.Session.goal_turn_finished(
            sess, item, SimpleNamespace(status="error", message="old failed"))
        self.assertIsNone(result)
        self.assertEqual(sess.goal["id"], "new-goal")
        self.assertEqual(sess.goal["rounds_started"], 0)

    def test_resume_cannot_bypass_session_owned_goal_dispatch(self):
        class Agent:
            session_id = "goal-recover"
            tokens_in = 0
            tokens_out = 0

            def set_goal_context(self, value):
                self.goal_context = value

        record = goals.start_round(goals.new(
            "resume the reserved round", goal_id="goal-recover-id",
            max_rounds=2))
        journal = controller.MemoryJournal()
        ctl = controller.SessionController("goal-recover", journal)
        ctl.internal_dispatch_enabled = False
        self.assertIsNone(ctl.submit_goal(
            goals.prompt(record, 1), metadata={
                "goal_id": record["id"], "revision": record["revision"],
                "round": 1,
            }))
        command = ctl.submit("/goal resume", controller.QueueMode.COMMAND)
        self.assertEqual(command.kind, controller.ActionKind.RUN_COMMAND)

        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = record
        sess._goal_activation = "disarmed"
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        sess._queue_display = {}
        sess._skip_queue_display = set()
        sess.plan_mode = False
        sess._workflow_current = None
        sess.controller = ctl
        sess.save = lambda: None
        sess.cwd_switch_blocker = lambda: None
        sess._goal_audit = lambda *args, **kwargs: None

        resumed = CLI.Session.resume_goal(sess)
        self.assertEqual(resumed["revision"], record["revision"])
        # Generic command completion must not spend/dispatch an internal turn.
        self.assertIsNone(ctl.finish_local_action())
        action = CLI.Session.dispatch_goal_ready(sess)
        self.assertEqual(action.item.origin, "goal")
        self.assertEqual(action.item.metadata["round"], 1)
        self.assertEqual(sess.goal["rounds_started"], 1)
        self.assertIn(action.item.id, sess._goal_round_items)

    def test_user_edit_keeps_an_old_round_from_mutating_new_definition(self):
        class Agent:
            session_id = "goal-edited"
            tokens_in = 12
            tokens_out = 7

            def set_goal_context(self, value):
                self.goal_context = value

        original = goals.start_round(goals.new(
            "old objective", goal_id="goal-edit-id", max_rounds=3))
        edited = goals.edit(original, objective="new objective")
        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = edited
        sess._goal_activation = "armed"
        sess._goal_round_items = {
            "old-item": {
                "goal_id": original["id"],
                "revision": original["revision"],
                "round": 1,
            },
        }
        sess._goal_round_started = {
            "old-item": (0, 0, 0.0),
        }
        sess.save = lambda: None
        sess._goal_audit = lambda *args, **kwargs: None
        item = SimpleNamespace(id="old-item", origin="goal")

        notice = CLI.Session.goal_turn_finished(
            sess, item, SimpleNamespace(status="error", message="old failed"))
        self.assertIsNone(notice)
        self.assertEqual(sess.goal["objective"], "new objective")
        self.assertEqual(sess.goal["last_error"], "")
        self.assertEqual(sess._goal_activation, "armed")

    def test_model_progress_advances_the_inflight_round_reference(self):
        class Agent:
            session_id = "goal-progress"
            tokens_in = 0
            tokens_out = 0

            def set_goal_context(self, value):
                self.goal_context = value

        record = goals.start_round(goals.new(
            "finish one round", goal_id="goal-progress-id", max_rounds=1))
        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = record
        sess._goal_activation = "armed"
        sess._goal_round_items = {
            "progress-item": {
                "goal_id": record["id"], "revision": record["revision"],
                "round": 1,
            },
        }
        sess._goal_round_started = {
            "progress-item": (0, 0, 0.0),
        }
        sess.save = lambda: None
        sess._goal_audit = lambda *args, **kwargs: None
        sess.controller = None

        CLI.Session._goal_update_from_tool(sess, {
            "action": "progress", "goal_id": record["id"],
            "revision": record["revision"], "evidence": "one check passed",
        })
        self.assertEqual(
            sess._goal_round_items["progress-item"]["revision"],
            sess.goal["revision"])

    def test_armed_goal_at_cap_blocks_instead_of_crashing_idle_loop(self):
        class Agent:
            session_id = "goal-cap-edge"
            tokens_in = 0
            tokens_out = 0

            def set_goal_context(self, value):
                self.goal_context = value

        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = goals.start_round(goals.new(
            "already at cap", goal_id="goal-cap-edge-id", max_rounds=1))
        sess._goal_activation = "armed"
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        sess._queue_display = {}
        sess._skip_queue_display = set()
        sess.plan_mode = False
        sess._workflow_current = None
        sess.controller = controller.SessionController(
            sess.ag.session_id, controller.MemoryJournal())
        sess.save = lambda: None
        sess.cwd_switch_blocker = lambda: None

        self.assertIsNone(CLI.Session.dispatch_goal_ready(sess))
        self.assertEqual(sess.goal["phase"], "blocked")
        self.assertEqual(
            sess.goal["blocked_reason"]["code"], "round-limit")

    def test_reservation_persistence_failure_disarms_without_crashing(self):
        class Agent:
            session_id = "goal-reservation-failure"
            tokens_in = 0
            tokens_out = 0
            supports_tools = True

            def set_goal_context(self, value):
                self.goal_context = value

        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = goals.new(
            "do not retry blindly", goal_id="goal-reservation-id",
            max_rounds=2)
        sess._goal_activation = "armed"
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        sess._queue_display = {}
        sess._skip_queue_display = set()
        sess.plan_mode = False
        sess._workflow_current = None
        sess.controller = controller.SessionController(
            sess.ag.session_id, controller.MemoryJournal())
        sess.save = mock.Mock(side_effect=OSError("state disk unavailable"))
        sess.cwd_switch_blocker = lambda: None
        sess.renderer = None

        with contextlib.redirect_stdout(io.StringIO()) as out:
            action = CLI.Session.dispatch_goal_ready(sess)
        self.assertIsNone(action)
        self.assertEqual(sess.goal["rounds_started"], 0)
        self.assertEqual(sess._goal_activation, "disarmed")
        self.assertEqual(sess.controller.queued, ())
        self.assertIn("已停用", out.getvalue())

    def test_chat_only_model_cannot_start_an_autonomous_goal(self):
        sess = object.__new__(CLI.Session)
        sess.ag = SimpleNamespace(supports_tools=False)
        sess.goal = None
        with self.assertRaisesRegex(goals.GoalError, "仅聊天"):
            CLI.Session.create_goal(sess, "cannot finish with no tools")

    def test_manual_completion_without_new_text_preserves_last_evidence(self):
        class Agent:
            session_id = "goal-manual-complete"

            def set_goal_context(self, value):
                self.goal_context = value

        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = goals.note(
            goals.new("preserve evidence", goal_id="goal-evidence"),
            evidence="focused tests passed")
        sess._goal_activation = "armed"
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        sess._queue_display = {}
        sess._skip_queue_display = set()
        sess.controller = None
        sess.save = lambda: None
        sess._goal_audit = lambda *args, **kwargs: None

        view = CLI.Session.complete_goal(sess)
        self.assertEqual(view["phase"], "complete")
        self.assertEqual(view["last_evidence"], "focused tests passed")
        self.assertEqual(CLI.Session._goal_context_text(sess), "")

    def test_terminal_goal_state_cancels_a_held_internal_round(self):
        class Agent:
            session_id = "goal-terminal"

            def set_goal_context(self, value):
                self.goal_context = value

        record = goals.start_round(goals.new(
            "finish manually", goal_id="goal-terminal-id", max_rounds=2))
        ctl = controller.SessionController(
            "goal-terminal", controller.MemoryJournal())
        self.assertIsNone(ctl.submit_goal(
            goals.prompt(record, 1), metadata={
                "goal_id": record["id"], "revision": record["revision"],
                "round": 1,
            }))
        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = record
        sess._goal_activation = "disarmed"
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        sess._queue_display = {}
        sess._skip_queue_display = set()
        sess.controller = ctl
        sess.save = lambda: None
        sess._goal_audit = lambda *args, **kwargs: None

        CLI.Session.complete_goal(sess, evidence="user verified")
        self.assertEqual(sess.goal["phase"], "complete")
        self.assertEqual(ctl.queued, ())

    def test_pause_save_failure_cancels_queue_and_keeps_gate_disarmed(self):
        class Agent:
            session_id = "goal-pause-failure"

            def set_goal_context(self, value):
                self.goal_context = value

        record = goals.start_round(goals.new(
            "pause safely", goal_id="goal-pause-id", max_rounds=2))
        ctl = controller.SessionController(
            "goal-pause-failure", controller.MemoryJournal())
        self.assertIsNone(ctl.submit_goal(
            goals.prompt(record, 1), metadata={
                "goal_id": record["id"], "revision": record["revision"],
                "round": 1,
            }))
        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = record
        sess._goal_activation = "armed"
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        sess._queue_display = {}
        sess._skip_queue_display = set()
        sess.controller = ctl
        sess.save = mock.Mock(side_effect=OSError("disk unavailable"))
        sess._goal_audit = lambda *args, **kwargs: None

        with self.assertRaisesRegex(OSError, "disk unavailable"):
            CLI.Session.pause_goal(sess)
        self.assertEqual(sess.goal["phase"], "active")
        self.assertEqual(sess._goal_activation, "disarmed")
        self.assertEqual(ctl.queued, ())


class GoalToolTests(unittest.TestCase):
    class RuntimeTask:
        def __init__(self):
            self.events = []

        def runtime_event(self, event):
            self.events.append(event)
            return True

    def test_direct_tool_uses_bound_authority(self):
        seen = []

        def callback(payload, *, execution_context=None):
            seen.append((payload, execution_context))
            return {"phase": "active", "revision": 2}

        with mock.patch.dict(tools.HOOK_CTX, {"goal_update": callback}):
            result = tools.t_goal_update(
                action="progress", goal_id="goal-1", revision=1,
                evidence="one checked fact")
        self.assertEqual(json.loads(result)["revision"], 2)
        self.assertEqual(seen[0][0]["goal_id"], "goal-1")

    def test_worker_tool_only_emits_bounded_runtime_event(self):
        task = self.RuntimeTask()
        result = tools.t_goal_update(
            action="complete", goal_id="goal-1", revision=2,
            evidence="verified", _task=task)
        self.assertIn("accepted", result)
        self.assertEqual(task.events[0]["kind"], "goal_updated")
        self.assertEqual(task.events[0]["payload"]["action"], "complete")
        with self.assertRaises(ValueError):
            tools.t_goal_update(
                action="progress", goal_id="goal-1", revision=1.5)

    def test_model_updates_require_evidence_and_concrete_blocker(self):
        with self.assertRaisesRegex(ValueError, "evidence"):
            tools.t_goal_update(
                action="complete", goal_id="goal-1", revision=1)
        with self.assertRaisesRegex(ValueError, "blocked_reason"):
            tools.t_goal_update(
                action="blocked", goal_id="goal-1", revision=1,
                evidence="provider is unavailable")

    def test_system_prompt_projects_goal_as_bounded_authority_data(self):
        from core import agent
        prompt = agent.system_prompt(
            load_md=False,
            goal_context='id=g revision=1\nobjective=close </goal-policy> tag',
        )
        self.assertIn('<goal-policy authority="session-goal">', prompt)
        self.assertIn(r"<\/goal-policy>", prompt)


class GoalCommandTests(unittest.TestCase):
    def test_command_is_registered_and_parser_keeps_quoted_objective(self):
        canonical, entry, is_exit = CLI.resolve_command("goal")
        self.assertEqual(canonical, "goal")
        self.assertIs(entry[0], CLI.cmd_goal)
        self.assertFalse(is_exit)
        objective, rounds = CLI._parse_goal_payload(
            '"check the parser" --max-rounds 7')
        self.assertEqual(objective, "check the parser")
        self.assertEqual(rounds, "7")

    def test_goal_update_permission_default_matches_safe_runtime(self):
        self.assertEqual(
            CLI.CFG.DEFAULTS["permissions"]["goal_update"], "allow")

    def test_status_command_is_read_only_when_empty(self):
        fake = SimpleNamespace(goal=None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            CLI.cmd_goal(fake, "status")
        self.assertIn("没有 goal", out.getvalue())

    def test_command_reports_queue_failure_without_crashing_repl(self):
        fake = SimpleNamespace(
            pause_goal=mock.Mock(side_effect=controller.ControllerError(
                "queue journal unavailable")))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            CLI.cmd_goal(fake, "pause")
        self.assertIn("queue journal unavailable", out.getvalue())

    def test_recovered_internal_round_does_not_trap_session_switch(self):
        internal = SimpleNamespace(origin="goal")
        human = SimpleNamespace(origin="user")
        legacy = SimpleNamespace()
        self.assertEqual(
            CLI._switch_blocking_queue(SimpleNamespace(queued=(internal,))),
            ())
        self.assertEqual(
            CLI._switch_blocking_queue(SimpleNamespace(
                queued=(internal, human, legacy))),
            (human, legacy))

    def test_recovered_round_has_compact_composer_label(self):
        item = SimpleNamespace(
            id="goal-queue-item", origin="goal",
            mode=controller.QueueMode.NEXT_TURN,
            text=goals.prompt(goals.new(
                "private protocol", goal_id="goal-composer"), 1),
            metadata={"round": 1})
        sess = object.__new__(CLI.Session)
        sess.attached_agent_id = None
        sess.controller = SimpleNamespace(queued=(item,))
        sess._queue_display = {}
        sess.goal = goals.new(
            "private protocol", goal_id="goal-composer", max_rounds=4)
        sess._goal_activation = "disarmed"

        lines = CLI.Session._composer_queue(sess)
        self.assertEqual(lines, ("↻ goal  round 1/4 · resume required",))
        self.assertNotIn(goals.ROUND_OPEN, lines[0])

    def test_clear_disarms_goal_before_erasing_its_context(self):
        class Agent:
            messages = [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "evidence-bearing prompt"},
            ]
            last_total = 12
            context_summary = {"content": "old"}
            context_invalid_reason = "old"
            _compact_failed_key = "old"
            _last_age_notice_key = "old"

            def set_goal_context(self, value):
                self.goal_context = value

        sess = object.__new__(CLI.Session)
        sess.ag = Agent()
        sess.goal = goals.new("keep objective", goal_id="goal-clear")
        sess._goal_activation = "armed"
        sess.controller = None
        sess.sync_transcript = mock.Mock()

        with contextlib.redirect_stdout(io.StringIO()) as out:
            CLI.cmd_clear(sess, "")
        self.assertEqual(sess.ag.messages, [
            {"role": "system", "content": "system"}])
        self.assertEqual(sess._goal_activation, "disarmed")
        self.assertIn("/goal resume", out.getvalue())

    def test_resume_replay_and_recap_hide_internal_round_prompt(self):
        goal = goals.new("human objective", goal_id="goal-replay")
        record = {
            "messages": [
                {"role": "user", "content": "human objective"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": goals.prompt(goal, 1)},
                {"role": "assistant", "content": "second answer"},
            ],
            "goal": goal,
            "task_plan": None,
        }
        replay = CLI.format_resume_replay(record["messages"], limit=20)
        self.assertNotIn("<goal_round>", replay)
        self.assertIn("human objective", replay)
        # recap 现在由模型现写；对内部续轮提示的保证变成：它不算人的提问，
        # 不能拿来凑「够 3 条才 recap」的数。
        self.assertEqual(
            CLI.AWAY_RECAP.real_user_messages(record["messages"]), 1)

    def test_allowed_goal_tool_is_hidden_without_active_goal(self):
        fake = SimpleNamespace(
            plan_mode=False, cfg=None, workflow_auto=False, goal=None)
        self.assertNotIn("goal_update", CLI.Session.allowed_tool_names(fake))

    def test_plan_mode_never_exposes_goal_mutation_tool(self):
        fake = SimpleNamespace(
            plan_mode=True, cfg=None, workflow_auto=False,
            goal=goals.new("inspect only", goal_id="goal-plan"))
        self.assertNotIn(
            "goal_update", CLI.Session.allowed_tool_names(fake))

    def test_goal_status_sanitizes_persisted_terminal_controls(self):
        fake = SimpleNamespace(
            goal=goals.new("safe\x1b[2J title", goal_id="goal-ui"),
            _goal_activation="disarmed")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            CLI.cmd_goal(fake, "status")
        self.assertNotIn("\x1b", out.getvalue())

    def test_resume_restores_definition_but_disarms_provider_spending(self):
        class Agent:
            load_md = False
            messages = []

            def load_context(self, value):
                self.context = value

        record = {
            "id": "goal-resume-session",
            "messages": [{"role": "system", "content": "system"}],
            "goal": goals.new(
                "resume safely", goal_id="goal-resume-definition"),
            "task_plan": None,
        }
        sess = SimpleNamespace(
            ag=Agent(), cfg=CLI.CFG.DEFAULTS,
            controller=SimpleNamespace(internal_dispatch_enabled=True),
            goal=None, _goal_activation="armed", _pending_route={"model": "x"},
        )
        with mock.patch.dict(tools.HOOK_CTX, {}, clear=False):
            CLI.restore_session_record(sess, record)

        self.assertEqual(sess.goal["id"], "goal-resume-definition")
        self.assertEqual(sess.goal["phase"], "active")
        self.assertEqual(sess._goal_activation, "disarmed")
        self.assertFalse(sess.controller.internal_dispatch_enabled)
        self.assertIsNone(sess._pending_route)


class GoalMemoryTests(unittest.TestCase):
    def test_internal_round_is_not_a_recent_user_message(self):
        agent = SimpleNamespace(messages=[
            {"role": "user", "content": "real user objective"},
            {"role": "assistant", "content": "first result"},
            {"role": "user", "content": goals.prompt(
                goals.new("real user objective", goal_id="goal-memory"), 1)},
            {"role": "assistant", "content": "second result"},
        ])
        self.assertEqual(
            memory._recent_user_messages(agent), "real user objective")


class GoalTerminalProjectionTests(unittest.TestCase):
    def test_real_pty_hydration_hides_internal_round_but_keeps_chat(self):
        body = r'''
import json
import sys
from core import goals, tui

record = goals.new("internal objective", goal_id="goal-pty")
messages = [
    {"role": "user", "content": "VISIBLE-HUMAN"},
    {"role": "assistant", "content": "VISIBLE-BEFORE"},
    {"role": "user", "content": goals.prompt(record, 1)},
    {"role": "assistant", "content": "VISIBLE-AFTER"},
]
renderer = tui.TerminalRenderer(sys.stdout)
renderer.hydrate_transcript(messages)
print("RESULT:" + json.dumps({
    "entries": [entry.text for entry in renderer._transcript],
}))
'''
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output, result = run_pty_child(
            body, [], cwd=root, timeout=4.0)

        self.assertIn("VISIBLE-HUMAN", output)
        self.assertIn("VISIBLE-AFTER", output)
        self.assertNotIn(goals.ROUND_OPEN, output)
        self.assertFalse(any(
            goals.ROUND_OPEN in text for text in result["entries"]))

    def test_real_repl_goal_command_auto_runs_and_stops_at_cap(self):
        body = r'''
import json
import os
import sys
import tempfile

repo = os.getcwd()
with tempfile.TemporaryDirectory(prefix="zylab-goal-repl-") as root:
    home = os.path.join(root, "home")
    workspace = os.path.join(root, "workspace")
    os.makedirs(home)
    os.makedirs(workspace)
    os.environ["HOME"] = home
    os.chdir(workspace)
    sys.path.insert(0, repo)

    import zylab
    from core import agent as A, client, goals
    from tests.pty_harness import install_ready_input_pump

    install_ready_input_pump()

    def fake_stream(model, messages, tools=None, cancel=None, **kwargs):
        assert goals.is_round_prompt(messages[-1]["content"]), messages[-1]
        yield {"t": "text", "v": "GOAL-ROUND-REPLY"}
        yield {"t": "done", "reason": "stop", "usage": {
            "prompt_tokens": 5, "completion_tokens": 2,
            "total_tokens": 7}}

    client.stream_chat = fake_stream
    client.model_limit = lambda *args, **kwargs: 100_000
    ag = A.Agent(model="goal-pty-model", load_md=False)
    session = zylab.Session(ag)
    ag.confirm = session.confirm
    ag.permission_decision = session.permission_decision
    zylab.repl(session, ag.model)
    internal = [message for message in ag.messages
                if message.get("role") == "user"
                and goals.is_round_prompt(message.get("content"))]
    print("RESULT:" + json.dumps({
        "phase": session.goal["phase"],
        "rounds": session.goal["rounds_started"],
        "internal": len(internal),
    }))
'''
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output, result = run_pty_child(
            body,
            [
                PTYSend(
                    b"/goal verify-the-idle-driver --rounds 1\r",
                    after="ZYLAB_TEST_PUMP_READY"),
                PTYSend(
                    b"/exit\r", after="goal 已暂停：达到 round 上限",
                    timeout=12.0),
            ],
            cwd=root, timeout=15.0)

        self.assertIn("GOAL-ROUND-REPLY", output)
        self.assertNotIn(goals.ROUND_OPEN, output)
        self.assertEqual(result, {
            "phase": "blocked", "rounds": 1, "internal": 1})


class GoalStoreTests(unittest.TestCase):
    def test_goal_is_persisted_before_first_chat_message_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {
                "HOME": root,
                "SESSIONS": root / "sessions",
                "ARCHIVE": root / "archived_sessions",
                "SESSION_LOCKS": root / "session-locks",
                "SESSION_LEASES": root / "session-leases",
                "SESSION_INDEX": root / "session-index.json",
                "SESSION_INDEX_DIRTY": root / "session-index.dirty",
                "INSTALL_ID": root / "installation_id",
                "USER_MD": root / "ZYLAB.md",
            }
            agent = SimpleNamespace(
                session_id="goal-store-1", model="m", gateway="g",
                tokens_in=0, tokens_out=0, last_total=0,
                messages=[{"role": "system", "content": "system"}],
                context_snapshot=lambda: None,
                load_md=True, load_user_md=True,
                load_project_md=True, load_skills=True,
            )
            with ExitStack() as stack:
                for name, path in paths.items():
                    stack.enter_context(mock.patch.object(store, name, path))
                stack.enter_context(mock.patch.object(
                    store, "_DIRS", (paths["SESSIONS"], paths["ARCHIVE"],
                                      paths["SESSION_LOCKS"], paths["SESSION_LEASES"])))
                stack.enter_context(mock.patch.object(
                    store, "_SHADOW_OVERRIDE", False))
                goal = goals.new(
                    "persist this objective", goal_id="goal-store-id",
                    timestamp="2026-08-31T00:00:00+00:00")
                saved = store.save_session(agent, goal_state=goal)
                self.assertIsNotNone(saved)
                self.assertEqual(
                    store.load_session("goal-store-1")["goal"]["id"],
                    "goal-store-id")
                self.assertEqual(
                    store.load_session("goal-store-1")["title"],
                    "persist this objective")

                full = store.fork_session(
                    "goal-store-1", target_id="goalstorefull")
                agent.messages.extend([
                    {"role": "user", "content": "later user turn"},
                    {"role": "assistant", "content": "later answer"},
                ])
                store.save_session(agent, goal_state=goal)
                historical = store.fork_session(
                    "goal-store-1", target_id="goalstorehistory",
                    message_count=1)
                self.assertEqual(full["goal"]["id"], "goal-store-id")
                self.assertIsNone(historical["goal"])

                # Explicit None means clear, even though the transcript still
                # has no user message; a brand-new empty session remains omitted.
                agent.messages = [{"role": "system", "content": "system"}]
                store.save_session(agent, goal_state=None)
                self.assertIsNone(store.load_session("goal-store-1")["goal"])


if __name__ == "__main__":
    unittest.main()
