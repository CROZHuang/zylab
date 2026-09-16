"""M4c command-level rewind tests."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab as CLI


class RewindCommandTests(unittest.TestCase):
    def make_session(self, authority):
        class Session:
            def __init__(self):
                self.ag = SimpleNamespace(
                    session_id="source-session",
                    messages=(
                        [{"role": "system", "content": "sys"}]
                        + [
                            {"role": role, "content": f"prior-{index}"}
                            for index in range(6)
                            for role in ("user", "assistant")
                        ]))
                self.lease_owner_id = "lease-owner"
                self.controller = None
                self.renderer = None

            def checkpoint_authority(self):
                return authority

            def pick(self, *args, **kwargs):
                raise AssertionError("explicit checkpoint/action must not open picker")

        return Session()

    def make_authority(self, *, cutoff=7):
        authority = mock.Mock()
        authority.list_checkpoints.return_value = [{
            "checkpoint_id": "cp-one",
            "created_at": "2026-08-25T00:00:00+00:00",
            "files": 1,
            "paths": ("/tmp/example.txt",),
            "conversation_message_count": cutoff,
        }]
        authority.plan_restore.return_value = SimpleNamespace(actions=(
            SimpleNamespace(action="restore", path="/tmp/example.txt"),
        ))
        authority.restore.return_value = SimpleNamespace(
            restored=("/tmp/example.txt",),
            already_restored=(),
            trashed=(),
        )
        return authority

    def test_conversation_rewind_uses_manifest_cutoff_and_keeps_code(self):
        authority = self.make_authority(cutoff=9)
        sess = self.make_session(authority)
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}), mock.patch.object(
                    CLI, "fork_session_record", return_value={"id": "child"}) as fork, redirect_stdout(io.StringIO()):
            CLI.cmd_rewind(sess, "cp-one conversation")

        authority.restore.assert_not_called()
        fork.assert_called_once_with(
            sess, {"id": "source-session"},
            message_count=9, replay=True, save_current=True)

    def test_code_rewind_calls_restore_once_and_records_completion(self):
        authority = self.make_authority()
        sess = self.make_session(authority)
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}) as audit, redirect_stdout(io.StringIO()):
            CLI.cmd_rewind(sess, "cp-one code")

        authority.restore.assert_called_once_with(
            "source-session", "cp-one", workspace_root=os.getcwd())
        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested", "checkpoint_restored", "rewind_completed"])

    def test_restore_success_is_not_reported_failed_when_completion_audit_fails(self):
        authority = self.make_authority()
        sess = self.make_session(authority)
        output = io.StringIO()

        def append(_session_id, kind, _payload, **_kwargs):
            if kind == "checkpoint_restored":
                raise OSError("audit disk full")
            return {}

        with mock.patch.object(
                CLI.store, "append_checkpoint_audit",
                side_effect=append) as audit, redirect_stdout(output):
            CLI.cmd_rewind(sess, "cp-one code")

        authority.restore.assert_called_once()
        rendered = output.getvalue()
        self.assertIn("代码恢复已完成", rendered)
        self.assertIn("完成审计失败", rendered)
        self.assertIn("不要直接重试", rendered)
        self.assertNotIn("[rewind] audit disk full", rendered)
        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested", "checkpoint_restored",
             "rewind_audit_failed", "rewind_completed"])

    def test_branch_success_is_not_reported_failed_when_branch_audit_fails(self):
        authority = self.make_authority(cutoff=7)
        sess = self.make_session(authority)
        output = io.StringIO()

        def append(_session_id, kind, _payload, **_kwargs):
            if kind == "rewind_branch_created":
                raise OSError("audit unavailable")
            return {}

        with mock.patch.object(
                CLI.store, "append_checkpoint_audit",
                side_effect=append) as audit, mock.patch.object(
                    CLI, "fork_session_record",
                    return_value={"id": "child"}), redirect_stdout(output):
            CLI.cmd_rewind(sess, "cp-one conversation")

        rendered = output.getvalue()
        self.assertIn("conversation branch已完成", rendered)
        self.assertIn("完成审计失败", rendered)
        self.assertIn("branch child", rendered)
        authority.restore.assert_not_called()
        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested", "rewind_branch_created",
             "rewind_audit_failed", "rewind_completed"])

    def test_both_creates_recoverable_branch_before_restoring_code(self):
        authority = self.make_authority(cutoff=11)
        sess = self.make_session(authority)
        calls = []

        def restore(*args, **kwargs):
            calls.append("restore")
            return SimpleNamespace(
                restored=(), already_restored=("/tmp/example.txt",),
                trashed=())

        def fork(*args, **kwargs):
            calls.append("fork")
            return {"id": "child"}

        authority.restore.side_effect = restore
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}), mock.patch.object(
                    CLI, "fork_session_record", side_effect=fork), redirect_stdout(io.StringIO()):
            CLI.cmd_rewind(sess, "cp-one both")

        self.assertEqual(calls, ["fork", "restore"])

    def test_code_conflict_is_visible_and_does_not_create_intent(self):
        authority = self.make_authority()
        authority.plan_restore.side_effect = CLI.CHECKPOINTS.RestoreConflict(
            "external change")
        sess = self.make_session(authority)
        output = io.StringIO()
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}) as audit, redirect_stdout(output):
            CLI.cmd_rewind(sess, "cp-one code")

        self.assertIn("external change", output.getvalue())
        authority.restore.assert_not_called()
        audit.assert_not_called()

    def test_clear_invalidates_old_cutoff_before_both_touches_code(self):
        authority = self.make_authority(cutoff=9)
        sess = self.make_session(authority)
        sess.ag.messages = sess.ag.messages[:1]
        output = io.StringIO()
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}) as audit, \
                mock.patch.object(CLI, "fork_session_record") as fork, \
                redirect_stdout(output):
            CLI.cmd_rewind(sess, "cp-one both")

        self.assertIn("/clear", output.getvalue())
        authority.restore.assert_not_called()
        fork.assert_not_called()
        audit.assert_not_called()

    def test_both_branch_failure_is_audited_before_any_code_restore(self):
        authority = self.make_authority(cutoff=7)
        sess = self.make_session(authority)
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}) as audit, \
                mock.patch.object(
                    CLI, "fork_session_record",
                    side_effect=OSError("session disk full")), \
                redirect_stdout(io.StringIO()):
            CLI.cmd_rewind(sess, "cp-one both")

        authority.restore.assert_not_called()
        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested", "rewind_branch_failed"])

    def test_both_code_failure_keeps_branch_and_records_partial_outcome(self):
        authority = self.make_authority(cutoff=7)
        authority.restore.side_effect = CLI.CHECKPOINTS.RestoreConflict(
            "late conflict")
        sess = self.make_session(authority)
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}) as audit, \
                mock.patch.object(
                    CLI, "fork_session_record",
                    return_value={"id": "child"}), \
                redirect_stdout(io.StringIO()):
            CLI.cmd_rewind(sess, "cp-one both")

        authority.restore.assert_called_once()
        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested", "rewind_branch_created",
             "checkpoint_restore_failed", "rewind_partial"])

    def test_partial_code_restore_writes_durable_partial_audit(self):
        authority = self.make_authority(cutoff=7)
        authority.restore.side_effect = CLI.CHECKPOINTS.PartialRestoreError(
            "partial", details={
                "schema_version": 1,
                "rollback_failures": [{
                    "path": "/tmp/example.txt",
                    "error_kind": "OSError",
                    "error": "disk failure",
                }],
            })
        sess = self.make_session(authority)
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}) as audit, \
                redirect_stdout(io.StringIO()):
            CLI.cmd_rewind(sess, "cp-one code")

        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested", "checkpoint_restore_partial"])
        partial_payload = audit.call_args_list[-1].args[2]
        self.assertEqual(
            partial_payload["rollback_failures"][0]["error"],
            "disk failure")

    def test_partial_restore_audit_failure_never_masks_primary_failure(self):
        authority = self.make_authority(cutoff=7)
        authority.restore.side_effect = CLI.CHECKPOINTS.PartialRestoreError(
            "durable compensation incomplete", details={
                "schema_version": 1,
                "transaction_id": "rt-partial-123",
                "rollback_failures": [{
                    "path": "/tmp/example.txt",
                    "kind": "OSError",
                    "message": "rollback disk failure",
                }],
            })
        sess = self.make_session(authority)
        output = io.StringIO()

        def append(_session_id, kind, _payload, **_kwargs):
            if kind == "checkpoint_restore_partial":
                raise OSError("audit disk full")
            return {}

        with mock.patch.object(
                CLI.store, "append_checkpoint_audit",
                side_effect=append) as audit, redirect_stdout(output):
            CLI.cmd_rewind(sess, "cp-one code")

        rendered = output.getvalue()
        self.assertIn("durable compensation incomplete", rendered)
        self.assertIn("transaction=rt-partial-123", rendered)
        self.assertIn("paths=/tmp/example.txt", rendered)
        self.assertIn("勿重试 /rewind", rendered)
        self.assertIn("checkpoint_restore_partial 审计失败", rendered)
        self.assertIn("audit disk full", rendered)
        self.assertIn("未替换", rendered)
        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested", "checkpoint_restore_partial",
             "rewind_audit_failed"])

    def test_empty_checkpoint_list_explains_scope(self):
        authority = self.make_authority()
        authority.list_checkpoints.return_value = []
        sess = self.make_session(authority)
        output = io.StringIO()
        with redirect_stdout(output):
            CLI.cmd_rewind(sess, "")

        self.assertIn("write_file/edit_file", output.getvalue())
        self.assertIn("Bash", output.getvalue())

    def test_summarize_uses_exact_cutoff_saves_projection_and_keeps_code(self):
        authority = self.make_authority(cutoff=13)
        sess = self.make_session(authority)
        sess.ag.context_summary = None
        sess.ag.last_total = 99
        sess.ag.summarize_to = mock.Mock(return_value="summary text")
        sess.save = mock.Mock()
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}) as audit, \
                mock.patch.object(CLI, "Spinner", mock.MagicMock()), \
                redirect_stdout(io.StringIO()):
            CLI.cmd_rewind(sess, "cp-one summarize")

        sess.ag.summarize_to.assert_called_once_with(13)
        sess.save.assert_called_once_with()
        authority.restore.assert_not_called()
        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested", "checkpoint_summarized"])

    def test_summarize_save_failure_rolls_back_projection_state(self):
        authority = self.make_authority(cutoff=3)
        sess = self.make_session(authority)
        original = {
            "context_summary": {"status": "valid", "content": "old"},
            "context_invalid_reason": "old-reason",
            "_compact_failed_key": "old-key",
            "compact_failed": "old-failure",
            "last_total": 77,
        }
        for name, value in original.items():
            setattr(sess.ag, name, value)

        def summarize(_cutoff):
            sess.ag.context_summary = {"status": "valid", "content": "new"}
            sess.ag.context_invalid_reason = None
            sess.ag._compact_failed_key = None
            sess.ag.compact_failed = None
            sess.ag.last_total = 0
            return "new summary"

        sess.ag.summarize_to = mock.Mock(side_effect=summarize)
        sess.save = mock.Mock(side_effect=OSError("disk full"))
        with mock.patch.object(
                CLI.store, "append_checkpoint_audit", return_value={}) as audit, \
                mock.patch.object(CLI, "Spinner", mock.MagicMock()), \
                redirect_stdout(io.StringIO()):
            CLI.cmd_rewind(sess, "cp-one summarize")

        for name, value in original.items():
            self.assertEqual(getattr(sess.ag, name), value)
        self.assertEqual(
            [call.args[1] for call in audit.call_args_list],
            ["rewind_requested"])


if __name__ == "__main__":
    unittest.main()
