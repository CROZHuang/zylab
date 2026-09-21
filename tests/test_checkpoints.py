"""Focused M4c prepared-write/checkpoint tests (no real /protected/archive writes)."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock as _mock
from core import checkpoints
from tests.platform_support import assert_mode, requires_symlinks  # noqa: E402


class CheckpointCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = checkpoints.CheckpointStore(self.root / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, tool, args, **kwargs):
        return checkpoints.prepare_write(
            tool, args, cwd=self.workspace, workspace_root=self.workspace,
            **kwargs)

    def execute(self, prepared, checkpoint_id="cp1"):
        return checkpoints.execute_prepared(
            prepared, self.store, session_id="session1",
            checkpoint_id=checkpoint_id, source_tool_run_id="tool1")

    def crash_restore_after_first_mutation(self, exit_code):
        code = """
import os
import sys
from core.checkpoints import CheckpointStore
store = CheckpointStore(sys.argv[1])
store.restore(
    'session1', 'cp1', workspace_root=sys.argv[2],
    _after_mutation=lambda count, row: os._exit(int(sys.argv[3])))
"""
        return subprocess.run(
            [sys.executable, "-c", code,
             str(self.root / "state"), str(self.workspace), str(exit_code)],
            cwd=Path(__file__).resolve().parents[1], check=False)


class PreparedWriteTests(CheckpointCase):
    def test_existing_write_preview_is_numbered_unified_diff(self):
        target = self.workspace / "hello.txt"
        target.write_text("一行\nold\n末行\n", encoding="utf-8", newline="")
        prepared = self.prepare(
            "write_file", {"path": "hello.txt", "content": "一行\nnew\n末行\n"})

        self.assertTrue(prepared.executable)
        self.assertEqual(prepared.before_bytes, "一行\nold\n末行\n".encode())
        self.assertIn("--- ", prepared.preview_text)
        self.assertIn("+++ ", prepared.preview_text)
        self.assertRegex(prepared.preview_text, r"\s+2\s+\| -old")
        self.assertRegex(prepared.preview_text, r"\s+2\s+\| \+new")
        self.assertNotIn("\x1b", prepared.preview_text)

    def test_new_file_preview_has_content_line_numbers_and_total(self):
        prepared = self.prepare(
            "write_file", {"path": "新文件.txt", "content": "甲\n乙\n丙"})
        self.assertFalse(prepared.existed_before)
        self.assertIn("[新建文件]", prepared.preview_text)
        self.assertIn("3 行", prepared.preview_text)
        self.assertIn("     1 | +甲", prepared.preview_text)
        self.assertIn("     3 | +丙", prepared.preview_text)

    def test_edit_valid_preflight_and_execution(self):
        target = self.workspace / "edit.txt"
        target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        prepared = self.prepare(
            "edit_file", {"path": target, "old": "beta", "new": "BETA"})
        self.assertEqual(prepared.match_count, 1)
        self.assertEqual(prepared.match_locations[0].line, 2)
        self.assertIn("匹配 1 处", prepared.preview_text)
        self.execute(prepared)
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha\nBETA\ngamma\n")

    def test_edit_zero_matches_is_visible_before_approval_and_cannot_execute(self):
        target = self.workspace / "edit.txt"
        target.write_text("alpha\n", encoding="utf-8")
        prepared = self.prepare(
            "edit_file", {"path": target, "old": "missing", "new": "x"})
        self.assertFalse(prepared.executable)
        self.assertIn("匹配 0 处", prepared.preview_text)
        self.assertIn("拒绝执行", prepared.preview_text)
        with self.assertRaises(checkpoints.InvalidEditError):
            self.execute(prepared)
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha\n")

    def test_edit_three_matches_reports_positions_before_approval(self):
        target = self.workspace / "edit.txt"
        target.write_text("x\na x\nx\n", encoding="utf-8")
        prepared = self.prepare(
            "edit_file", {"path": target, "old": "x", "new": "y"})
        self.assertFalse(prepared.executable)
        self.assertEqual(prepared.match_count, 3)
        self.assertEqual([item.line for item in prepared.match_locations], [1, 2, 3])
        self.assertIn("匹配 3 处", prepared.preview_text)
        self.assertIn("第1行", prepared.preview_text)
        self.assertIn("第3行", prepared.preview_text)

    def test_replace_all_three_matches_is_executable(self):
        target = self.workspace / "edit.txt"
        target.write_text("x\nx\nx\n", encoding="utf-8")
        prepared = self.prepare("edit_file", {
            "path": target, "old": "x", "new": "y", "replace_all": True})
        self.assertTrue(prepared.executable)
        self.execute(prepared)
        self.assertEqual(target.read_text(), "y\ny\ny\n")

    def test_long_preview_keeps_head_tail_and_explicit_marker(self):
        target = self.workspace / "long.txt"
        target.write_text("\n".join(f"old-{i}" for i in range(100)) + "\n")
        prepared = self.prepare("write_file", {
            "path": target,
            "content": "\n".join(f"new-{i}" for i in range(100)) + "\n",
        }, preview_lines=12)
        lines = prepared.preview_text.splitlines()
        self.assertEqual(len(lines), 12)
        self.assertIn("预览截断", prepared.preview_text)
        self.assertIn("old-0", prepared.preview_text)
        self.assertIn("new-99", prepared.preview_text)

    def test_external_change_after_preview_fails_closed(self):
        target = self.workspace / "race.txt"
        target.write_text("before\n")
        prepared = self.prepare(
            "write_file", {"path": target, "content": "after\n"})
        target.write_text("external\n")
        with self.assertRaisesRegex(checkpoints.ExternalChangeError, "变化"):
            self.execute(prepared)
        self.assertEqual(target.read_text(), "external\n")

    @requires_symlinks
    def test_parent_swap_never_creates_temp_in_protected_directory(self):
        parent = self.workspace / "parent"
        parent.mkdir()
        target = parent / "target.txt"
        target.write_text("before\n")
        protected = self.root / "protected"
        protected.mkdir()
        prepared = checkpoints.prepare_write(
            "write_file", {"path": target, "content": "after\n"},
            cwd=self.workspace, workspace_root=self.workspace,
            protected_paths=(str(protected),))
        capture = self.store.capture_prepared(
            "session1", "swap", prepared)
        displaced = self.workspace / "parent-displaced"
        original_assert = checkpoints._assert_entry_at
        calls = {"count": 0}

        def swap_after_first_dirfd_check(*args, **kwargs):
            result = original_assert(*args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 1:
                parent.rename(displaced)
                parent.symlink_to(protected, target_is_directory=True)
            return result

        with mock.patch.object(
                checkpoints, "_assert_entry_at",
                side_effect=swap_after_first_dirfd_check):
            with self.assertRaises(checkpoints.ExternalChangeError):
                checkpoints.execute_prepared(
                    prepared, self.store, capture=capture)

        self.assertEqual(list(protected.iterdir()), [])
        self.assertFalse(any("zylab" in item.name
                             for item in displaced.iterdir()))

    def test_prepare_reads_existing_target_exactly_once(self):
        target = self.workspace / "once.txt"
        # newline=""：不让 Python 把 \n 翻成平台换行。prepare_write 现在按**字节**
        # 读原文（Windows 的 os.open 默认文本模式会折 CRLF，已改成二进制），
        # 不写死换行的话，Windows 上落盘的是 CRLF，与下面的断言对不上。
        target.write_text("before\n", newline="")
        real_read = checkpoints._read_regular_once
        with mock.patch(
                "core.checkpoints._read_regular_once", wraps=real_read) as read_mock:
            prepared = self.prepare(
                "write_file", {"path": target, "content": "after\n"})
        self.assertEqual(read_mock.call_count, 1)
        self.assertEqual(prepared.before_bytes, b"before\n")

    @requires_symlinks
    def test_symlink_target_and_parent_are_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("sentinel")
        (self.workspace / "link.txt").symlink_to(outside)
        with self.assertRaisesRegex(checkpoints.UnsafePathError, "符号链接"):
            self.prepare("write_file", {"path": "link.txt", "content": "bad"})

        outside_dir = self.root / "outside-dir"
        outside_dir.mkdir()
        (self.workspace / "linked-dir").symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaisesRegex(checkpoints.UnsafePathError, "符号链接"):
            self.prepare("write_file", {
                "path": "linked-dir/new.txt", "content": "bad"})
        self.assertEqual(outside.read_text(), "sentinel")

    def test_cross_workspace_and_lexical_protected_rejected_without_io(self):
        with self.assertRaisesRegex(checkpoints.UnsafePathError, "越出 workspace"):
            self.prepare("write_file", {
                "path": str(self.root / "outside.txt"), "content": "bad"})
        real_lstat = os.lstat
        with mock.patch("core.checkpoints.os.lstat", wraps=real_lstat) as lstat_mock:
            # 显式传 protected_paths：模块级 PROTECTED_PATHS 是 import 时从配置
            # 取的快照，默认为空。测试要验的是「受保护检查先于 workspace 检查」，
            # 所以必须自带受保护路径，否则只会撞上「越出 workspace」那条。
            with self.assertRaisesRegex(checkpoints.UnsafePathError, "受保护"):
                checkpoints.prepare_write(
                    "write_file", {"path": "/protected/archive/never-touch.txt", "content": "bad"},
                    cwd=self.workspace, workspace_root=self.workspace,
                    protected_paths=("/protected/archive",))
        # Workspace lstat occurs first; target /protected/archive is rejected before a
        # target lstat/read.  No write probe is ever attempted.
        checked = [os.fspath(call.args[0]) for call in lstat_mock.call_args_list]
        self.assertNotIn("/protected/archive/never-touch.txt", checked)


class CheckpointStoreTests(CheckpointCase):
    @requires_symlinks
    def test_store_rejects_symlink_component_before_creating_root(self):
        outside = self.root / "outside-state"
        outside.mkdir()
        link = self.root / "state-link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(checkpoints.ManifestError, "符号链接"):
            checkpoints.CheckpointStore(link / "nested")
        self.assertFalse((outside / "nested").exists())

    @requires_symlinks
    def test_checkpoint_lock_symlink_is_rejected_without_chmod_target(self):
        directory = self.store._session_dir("session1")
        victim = self.root / "victim.txt"
        victim.write_text("keep")
        victim.chmod(0o644)
        (directory / ".cp1.lock").symlink_to(victim)

        with self.assertRaises(checkpoints.ManifestError):
            with self.store._lock("session1", "cp1"):
                pass

        assert_mode(self, victim, 0o644)

    @requires_symlinks
    def test_replaced_lock_parents_never_create_in_protected_directory(self):
        protected = self.root / "protected-lock-target"
        protected.mkdir()
        authority = checkpoints.CheckpointStore(
            self.root / "anchored-state",
            protected_paths=(str(protected),))

        displaced_locks = self.root / "displaced-path-locks"
        authority.path_locks_dir.rename(displaced_locks)
        authority.path_locks_dir.symlink_to(
            protected, target_is_directory=True)
        with self.assertRaises(checkpoints.CheckpointError):
            with authority._path_locks((str(self.workspace / "x"),)):
                pass
        self.assertEqual(list(protected.iterdir()), [])

        # Rebuild a fresh authority for the independent session-parent case.
        authority = checkpoints.CheckpointStore(
            self.root / "anchored-state-2",
            protected_paths=(str(protected),))
        session_dir = authority._session_dir("session1")
        displaced_session = self.root / "displaced-session"
        session_dir.rename(displaced_session)
        session_dir.symlink_to(protected, target_is_directory=True)
        with self.assertRaises(checkpoints.CheckpointError):
            with authority._lock("session1", "cp1"):
                pass
        self.assertEqual(list(protected.iterdir()), [])

    def test_path_lock_serializes_two_sessions_and_prevents_lost_update(self):
        target = self.workspace / "shared.txt"
        target.write_text("base")
        prepared_a = self.prepare(
            "write_file", {"path": target, "content": "from-a"})
        prepared_b = self.prepare(
            "write_file", {"path": target, "content": "from-b"})
        store_b = checkpoints.CheckpointStore(self.root / "state")
        capture_a = self.store.capture_prepared(
            "session-a", "cp-a", prepared_a)
        capture_b = store_b.capture_prepared(
            "session-b", "cp-b", prepared_b)
        barrier = threading.Barrier(2)
        results = []

        def execute(prepared, authority, capture):
            barrier.wait(timeout=5)
            try:
                checkpoints.execute_prepared(
                    prepared, authority, capture=capture)
                results.append("success")
            except checkpoints.ExternalChangeError:
                results.append("conflict")

        threads = [
            threading.Thread(
                target=execute,
                args=(prepared_a, self.store, capture_a)),
            threading.Thread(
                target=execute,
                args=(prepared_b, store_b, capture_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ["success", "conflict"])
        self.assertIn(target.read_text(), {"from-a", "from-b"})

    def test_existing_and_new_files_execute_with_private_authority(self):
        old = self.workspace / "old.txt"
        old.write_text("before")
        p_old = self.prepare(
            "write_file", {"path": old, "content": "after"})
        self.execute(p_old)
        new = self.prepare(
            "write_file", {"path": "new.txt", "content": "created"})
        self.execute(new)

        manifest_path = self.store.manifest_path("session1", "cp1")
        manifest = json.loads(manifest_path.read_text())
        old_row = manifest["files"][str(old)]
        new_row = manifest["files"][str(self.workspace / "new.txt")]
        self.assertEqual(old_row["before_sha256"], p_old.before_sha256)
        self.assertEqual(old_row["expected_after_sha256"], p_old.after_sha256)
        self.assertIsNone(old_row["pending_after_sha256"])
        self.assertFalse(new_row["existed_before"])
        assert_mode(self, manifest_path, 0o600)
        blob = self.root / "state" / "checkpoint-files" / p_old.before_sha256[:2] / p_old.before_sha256
        self.assertEqual(blob.read_bytes(), b"before")
        assert_mode(self, blob, 0o600)
        for directory in (
                self.root / "state", self.root / "state" / "checkpoints",
                self.root / "state" / "checkpoint-files", self.root / "state" / "trash"):
            assert_mode(self, directory, 0o700)

    def test_240_byte_basenames_write_restore_and_trash(self):
        existing_name = "e" * 240
        created_name = "n" * 240
        existing = self.workspace / existing_name
        existing.write_text("before")
        self.execute(self.prepare("write_file", {
            "path": existing, "content": "after"}))
        self.execute(self.prepare("write_file", {
            "path": created_name, "content": "created"}))

        result = self.store.restore(
            "session1", "cp1", workspace_root=self.workspace)

        self.assertEqual(existing.read_text(), "before")
        self.assertFalse((self.workspace / created_name).exists())
        trash_path = Path(result.trashed[0][1])
        self.assertTrue(trash_path.exists())
        self.assertLessEqual(len(trash_path.name.encode()), 255)
        self.assertEqual(trash_path.read_text(), "created")

    def test_same_path_repeated_write_preserves_first_before_image(self):
        target = self.workspace / "repeat.txt"
        target.write_text("A")
        first = self.prepare(
            "write_file", {"path": target, "content": "B"})
        self.execute(first)
        second = self.prepare(
            "write_file", {"path": target, "content": "C"})
        self.execute(second)

        row = self.store.load_manifest("session1", "cp1")["files"][str(target)]
        self.assertEqual(row["before_sha256"], first.before_sha256)
        self.assertEqual(row["expected_after_sha256"], second.after_sha256)
        self.assertEqual(
            self.store._read_blob(row["before_sha256"]), b"A")
        self.assertEqual(target.read_bytes(), b"C")

    def test_list_checkpoints_returns_validated_metadata(self):
        target = self.workspace / "listed.txt"
        target.write_text("before\n", encoding="utf-8")
        first = self.prepare(
            "write_file", {"path": target, "content": "one\n"})
        second = self.prepare(
            "write_file", {"path": target, "content": "two\n"})
        self.store.capture_prepared("session1", "turn-a", first)
        self.store.capture_prepared("session1", "turn-b", second)

        rows = self.store.list_checkpoints("session1")

        self.assertEqual(
            {row["checkpoint_id"] for row in rows}, {"turn-a", "turn-b"})
        self.assertTrue(all(row["files"] == 1 for row in rows))
        self.assertTrue(all(str(target) in row["paths"] for row in rows))

    def test_readonly_checkpoint_ids_do_not_create_missing_store(self):
        missing = self.root / "missing-state"

        rows = checkpoints.list_checkpoint_ids_readonly(
            missing, "session1")

        self.assertEqual(rows, [])
        self.assertFalse(missing.exists())

    def test_readonly_checkpoint_ids_are_bounded_without_lock_files(self):
        target = self.workspace / "readonly-list.txt"
        target.write_text("before\n", encoding="utf-8")
        prepared = self.prepare(
            "write_file", {"path": target, "content": "after\n"})
        self.store.capture_prepared("session1", "turn-a", prepared)
        self.store.capture_prepared("session1", "turn-b", prepared)
        session_dir = self.root / "state" / "checkpoints" / "session1"
        before = sorted(path.name for path in session_dir.iterdir())

        rows = checkpoints.list_checkpoint_ids_readonly(
            self.root / "state", "session1", limit=1)
        after = sorted(path.name for path in session_dir.iterdir())

        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["checkpoint_id"], {"turn-a", "turn-b"})
        self.assertEqual(before, after)

    def test_capture_payload_can_precede_tool_started(self):
        target = self.workspace / "capture.txt"
        target.write_text("before")
        prepared = self.prepare(
            "write_file", {"path": target, "content": "after"})
        capture = self.store.capture_prepared(
            "session1", "cp1", prepared, source_tool_run_id="run7",
            conversation_message_count=17)
        payload = capture.event_payload()
        self.assertEqual(payload["checkpoint_id"], "cp1")
        self.assertEqual(payload["expected_after_sha256"], prepared.after_sha256)
        self.assertEqual(payload["conversation_message_count"], 17)
        self.assertEqual(
            self.store.list_checkpoints("session1")[0][
                "conversation_message_count"],
            17)
        self.assertEqual(target.read_text(), "before")
        checkpoints.execute_prepared(prepared, self.store, capture=capture)
        self.assertEqual(target.read_text(), "after")

    def test_blob_failure_prevents_existing_target_write(self):
        target = self.workspace / "blob-fail.txt"
        target.write_text("before")
        prepared = self.prepare(
            "write_file", {"path": target, "content": "after"})
        with mock.patch.object(
                self.store, "_write_blob", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.execute(prepared)
        self.assertEqual(target.read_text(), "before")
        self.assertFalse(self.store.manifest_path("session1", "cp1").exists())

    def test_manifest_failure_prevents_new_target_write(self):
        target = self.workspace / "manifest-fail.txt"
        prepared = self.prepare(
            "write_file", {"path": target, "content": "after"})
        with mock.patch.object(
                self.store, "_write_manifest", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.execute(prepared)
        self.assertFalse(target.exists())

    def test_restore_existing_bytes_mode_and_new_file_to_trash(self):
        existing = self.workspace / "existing.sh"
        existing.write_bytes(b"before\n")
        existing.chmod(0o750)
        p_existing = self.prepare(
            "write_file", {"path": existing, "content": "after\n"})
        self.execute(p_existing)
        created = self.prepare(
            "write_file", {"path": "created.txt", "content": "created\n"})
        self.execute(created)

        result = self.store.restore(
            "session1", "cp1", workspace_root=self.workspace)
        self.assertEqual(existing.read_bytes(), b"before\n")
        assert_mode(self, existing, 0o750)
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual(result.restored, (str(existing),))
        self.assertEqual(result.trashed[0][0], str(self.workspace / "created.txt"))
        self.assertEqual(Path(result.trashed[0][1]).read_bytes(), b"created\n")

    def test_restore_is_idempotent_when_already_before(self):
        target = self.workspace / "already.txt"
        target.write_text("before")
        prepared = self.prepare(
            "write_file", {"path": target, "content": "after"})
        self.execute(prepared)
        self.store.restore("session1", "cp1", workspace_root=self.workspace)
        again = self.store.restore(
            "session1", "cp1", workspace_root=self.workspace)
        self.assertEqual(again.already_restored, (str(target),))

    def test_restore_full_preflight_conflict_changes_nothing(self):
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("first-before")
        second.write_text("second-before")
        self.execute(self.prepare(
            "write_file", {"path": first, "content": "first-after"}))
        self.execute(self.prepare(
            "write_file", {"path": second, "content": "second-after"}))
        second.write_text("external")

        with self.assertRaisesRegex(checkpoints.RestoreConflict, "既不是"):
            self.store.restore("session1", "cp1", workspace_root=self.workspace)
        self.assertEqual(first.read_text(), "first-after")
        self.assertEqual(second.read_text(), "external")

    def test_restore_second_action_conflict_rolls_back_first_action(self):
        first = self.workspace / "a-first.txt"
        second = self.workspace / "b-second.txt"
        first.write_text("first-before")
        second.write_text("second-before")
        self.execute(self.prepare(
            "write_file", {"path": first, "content": "first-after"}))
        self.execute(self.prepare(
            "write_file", {"path": second, "content": "second-after"}))
        original = self.store._replace_for_restore
        calls = {"count": 0}

        def conflict_on_second(action):
            calls["count"] += 1
            if calls["count"] == 2:
                second.write_text("external")
            return original(action)

        with mock.patch.object(
                self.store, "_replace_for_restore",
                side_effect=conflict_on_second):
            with self.assertRaises(checkpoints.PartialRestoreError):
                self.store.restore(
                    "session1", "cp1", workspace_root=self.workspace)

        self.assertEqual(first.read_text(), "first-after")
        self.assertEqual(second.read_text(), "external")

    def test_restore_reports_partial_when_compensation_itself_fails(self):
        first = self.workspace / "a-first-partial.txt"
        second = self.workspace / "b-second-partial.txt"
        first.write_text("first-before")
        second.write_text("second-before")
        self.execute(self.prepare(
            "write_file", {"path": first, "content": "first-after"}))
        self.execute(self.prepare(
            "write_file", {"path": second, "content": "second-after"}))
        original = self.store._replace_for_restore
        calls = {"count": 0}

        def fail_second(action):
            calls["count"] += 1
            if calls["count"] == 2:
                raise checkpoints.RestoreConflict("injected second failure")
            return original(action)

        real_atomic = checkpoints._atomic_replace_snapshot

        def fail_recovery(*args, **kwargs):
            if kwargs.get("temp_tag") == "recovery-restore":
                raise OSError("rollback disk failure")
            return real_atomic(*args, **kwargs)

        with mock.patch.object(
                self.store, "_replace_for_restore",
                side_effect=fail_second), mock.patch.object(
                    checkpoints, "_atomic_replace_snapshot",
                    side_effect=fail_recovery):
            with self.assertRaises(checkpoints.PartialRestoreError) as caught:
                self.store.restore(
                    "session1", "cp1", workspace_root=self.workspace)

        self.assertEqual(first.read_text(), "first-before")
        self.assertEqual(second.read_text(), "second-after")
        self.assertEqual(
            caught.exception.details["rollback_failures"][0]["path"],
            str(first))

    def test_commit_marker_failure_compensates_all_mutations(self):
        target = self.workspace / "commit-marker.txt"
        target.write_text("before")
        self.execute(self.prepare("write_file", {
            "path": target, "content": "after"}))
        real_write = self.store._write_restore_transaction

        def fail_commit(value):
            if value.get("status") == "committed":
                raise OSError("injected commit fsync failure")
            return real_write(value)

        with mock.patch.object(
                self.store, "_write_restore_transaction",
                side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, "commit fsync"):
                self.store.restore(
                    "session1", "cp1", workspace_root=self.workspace)

        self.assertEqual(target.read_text(), "after")
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "rolled_back")

    def test_mode_zero_is_preserved_by_durable_compensation(self):
        target = self.workspace / "mode-zero.txt"
        target.write_text("before")
        target.chmod(0)
        try:
            target.read_bytes()
        except PermissionError:
            # The checkpoint code normally runs in the parent zylab
            # process.  When the entire suite is deliberately launched from
            # zylab's privilege-dropped bash sandbox, that process cannot
            # read a mode-zero before-image at all; exercising preservation is
            # impossible rather than a different checkpoint semantic.
            target.chmod(0o600)
            self.skipTest(
                "mode-zero before-image requires read privilege")
        self.execute(self.prepare("write_file", {
            "path": target, "content": "after"}))

        def abort_after_first(_count, _row):
            raise RuntimeError("injected after mutation")

        with self.assertRaisesRegex(RuntimeError, "after mutation"):
            self.store.restore(
                "session1", "cp1", workspace_root=self.workspace,
                _after_mutation=abort_after_first)
        self.assertEqual(target.read_text(), "after")
        assert_mode(self, target, 0)

    def test_recovery_rejects_same_content_with_external_mode(self):
        target = self.workspace / "mode-cas.txt"
        target.write_text("before")
        target.chmod(0o640)
        self.execute(self.prepare("write_file", {
            "path": target, "content": "after"}))
        work = checkpoints._workspace(
            self.workspace, self.store.protected_paths)
        plan = self.store.plan_restore(
            "session1", "cp1", workspace_root=self.workspace)
        transaction = self.store._prepare_restore_transaction(plan, work)
        self.store._set_restore_transaction_status(transaction, "mutating")
        self.store._replace_for_restore(plan.actions[0])
        target.chmod(0o777)

        with self.assertRaises(checkpoints.PartialRestoreError):
            self.store.recover_incomplete_transactions(
                workspace_root=self.workspace)
        self.assertEqual(target.read_text(), "before")
        assert_mode(self, target, 0o777)
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "recovery_conflict")

    def test_recovery_rejects_recreated_workspace_identity(self):
        target = self.workspace / "workspace-binding.txt"
        target.write_text("before")
        self.execute(self.prepare("write_file", {
            "path": target, "content": "after"}))
        work = checkpoints._workspace(
            self.workspace, self.store.protected_paths)
        plan = self.store.plan_restore(
            "session1", "cp1", workspace_root=self.workspace)
        transaction = self.store._prepare_restore_transaction(plan, work)
        self.store._set_restore_transaction_status(transaction, "mutating")
        displaced = self.root / "old-workspace"
        self.workspace.rename(displaced)
        self.workspace.mkdir()

        with self.assertRaisesRegex(
                checkpoints.RestoreConflict, "workspace 身份"):
            self.store.recover_incomplete_transactions(
                workspace_root=self.workspace)
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "recovery_conflict")

    def test_trash_recovery_never_resurrects_when_both_copies_missing(self):
        created = self.workspace / "trash-both-missing.txt"
        self.execute(self.prepare("write_file", {
            "path": created, "content": "created"}))
        work = checkpoints._workspace(
            self.workspace, self.store.protected_paths)
        plan = self.store.plan_restore(
            "session1", "cp1", workspace_root=self.workspace)
        transaction = self.store._prepare_restore_transaction(plan, work)
        self.store._set_restore_transaction_status(transaction, "mutating")
        row = transaction["actions"][0]
        self.store._move_to_trash(
            plan.actions[0], Path(row["destination"]))
        Path(row["destination"]).unlink()

        with self.assertRaises(checkpoints.PartialRestoreError):
            self.store.recover_incomplete_transactions(
                workspace_root=self.workspace)
        self.assertFalse(created.exists())
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "recovery_conflict")

    def test_cross_filesystem_trash_orphan_is_compensated(self):
        created = self.workspace / "cross-fs-created.txt"
        self.execute(self.prepare("write_file", {
            "path": created, "content": "created"}))
        real_replace = os.replace
        real_unlink = os.unlink
        state = {"exdev": False, "unlink_failed": False}

        def replace_once(source, destination, *args, **kwargs):
            if (not state["exdev"]
                    and source == created.name
                    and str(destination).startswith("file-")):
                state["exdev"] = True
                raise OSError(checkpoints.errno.EXDEV, "cross-device")
            return real_replace(source, destination, *args, **kwargs)

        def fail_source_unlink_once(path, *args, **kwargs):
            if not state["unlink_failed"] and path == created.name:
                state["unlink_failed"] = True
                raise OSError("injected source unlink failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
                checkpoints.os, "replace", side_effect=replace_once), \
                mock.patch.object(
                    checkpoints.os, "unlink",
                    side_effect=fail_source_unlink_once):
            with self.assertRaisesRegex(OSError, "source unlink"):
                self.store.restore(
                    "session1", "cp1", workspace_root=self.workspace)

        self.assertTrue(state["exdev"])
        self.assertTrue(state["unlink_failed"])
        self.assertEqual(created.read_text(), "created")
        transaction = self.store.list_restore_transactions()[0]
        self.assertEqual(transaction["status"], "rolled_back")
        destination = Path(transaction["actions"][0]["destination"])
        self.assertFalse(destination.exists())

    def test_recovery_removes_cross_filesystem_trash_copy_temp_after_exit(self):
        created = self.workspace / "cross-fs-copy-crash.txt"
        self.execute(self.prepare("write_file", {
            "path": created, "content": "secret-created"}))
        created.chmod(0o666)
        code = """
import errno
import os
import sys
from core import checkpoints
store = checkpoints.CheckpointStore(sys.argv[1])
real_replace = os.replace
state = {'exdev': False}
def crash_between_copy_and_rename(source, destination, *args, **kwargs):
    if (not state['exdev'] and source == sys.argv[3]
            and str(destination).startswith('file-')):
        state['exdev'] = True
        raise OSError(errno.EXDEV, 'forced cross-device')
    if (str(source).startswith('.kct-')
            and str(destination).startswith('file-')):
        os._exit(96)
    return real_replace(source, destination, *args, **kwargs)
checkpoints.os.replace = crash_between_copy_and_rename
store.restore('session1', 'cp1', workspace_root=sys.argv[2])
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(self.root / "state"),
             str(self.workspace), created.name],
            cwd=Path(__file__).resolve().parents[1], check=False)

        self.assertEqual(child.returncode, 96)
        transaction = self.store.list_restore_transactions()[0]
        self.assertEqual(transaction["status"], "mutating")
        destination = Path(transaction["actions"][0]["destination"])
        temps = list(destination.parent.glob(".kct-*.tmp"))
        self.assertEqual(len(temps), 1)
        self.assertEqual(temps[0].read_text(), "secret-created")
        assert_mode(self, temps[0], 0o666)
        self.assertTrue(created.exists())
        self.assertFalse(destination.exists())

        recovered = self.store.recover_incomplete_transactions(
            workspace_root=self.workspace)

        self.assertEqual(recovered, (transaction["transaction_id"],))
        self.assertEqual(created.read_text(), "secret-created")
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".kct-*.tmp")), [])
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "rolled_back")

    def test_recovery_removes_partial_trash_copy_temp_after_mid_copy_exit(self):
        created = self.workspace / "cross-fs-copy-partial.txt"
        self.execute(self.prepare("write_file", {
            "path": created, "content": "secret-created"}))
        code = """
import errno
import os
import sys
from core import checkpoints
store = checkpoints.CheckpointStore(sys.argv[1])
real_replace = os.replace
state = {'exdev': False}
def force_cross_device(source, destination, *args, **kwargs):
    if (not state['exdev'] and source == sys.argv[3]
            and str(destination).startswith('file-')):
        state['exdev'] = True
        raise OSError(errno.EXDEV, 'forced cross-device')
    return real_replace(source, destination, *args, **kwargs)
def crash_mid_copy(incoming, outgoing, length):
    outgoing.write(incoming.read(4))
    outgoing.flush()
    os.fsync(outgoing.fileno())
    os._exit(98)
checkpoints.os.replace = force_cross_device
checkpoints.shutil.copyfileobj = crash_mid_copy
store.restore('session1', 'cp1', workspace_root=sys.argv[2])
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(self.root / "state"),
             str(self.workspace), created.name],
            cwd=Path(__file__).resolve().parents[1], check=False)

        self.assertEqual(child.returncode, 98)
        transaction = self.store.list_restore_transactions()[0]
        destination = Path(transaction["actions"][0]["destination"])
        temps = list(destination.parent.glob(".kct-*.tmp"))
        self.assertEqual(len(temps), 1)
        self.assertEqual(temps[0].read_bytes(), b"secr")
        self.assertEqual(transaction["status"], "mutating")

        recovered = self.store.recover_incomplete_transactions(
            workspace_root=self.workspace)

        self.assertEqual(recovered, (transaction["transaction_id"],))
        self.assertEqual(created.read_text(), "secret-created")
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".kct-*.tmp")), [])
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "rolled_back")

    def test_recovery_removes_cross_filesystem_rollback_temp_after_exit(self):
        created = self.workspace / "cross-fs-rollback-crash.txt"
        self.execute(self.prepare("write_file", {
            "path": created, "content": "secret-created"}))
        work = checkpoints._workspace(
            self.workspace, self.store.protected_paths)
        plan = self.store.plan_restore(
            "session1", "cp1", workspace_root=self.workspace)
        transaction = self.store._prepare_restore_transaction(plan, work)
        self.store._set_restore_transaction_status(transaction, "mutating")
        row = transaction["actions"][0]
        destination = Path(row["destination"])
        self.store._move_to_trash(plan.actions[0], destination)
        self.assertFalse(created.exists())
        self.assertTrue(destination.exists())

        code = """
import errno
import os
import sys
from core import checkpoints
store = checkpoints.CheckpointStore(sys.argv[1])
real_replace = os.replace
def crash_between_copy_and_rename(source, destination, *args, **kwargs):
    if source == sys.argv[3] and destination == sys.argv[4]:
        raise OSError(errno.EXDEV, 'forced cross-device')
    if str(source).startswith('.kcr-') and destination == sys.argv[4]:
        os._exit(97)
    return real_replace(source, destination, *args, **kwargs)
checkpoints.os.replace = crash_between_copy_and_rename
store.recover_incomplete_transactions(workspace_root=sys.argv[2])
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(self.root / "state"),
             str(self.workspace), destination.name, created.name],
            cwd=Path(__file__).resolve().parents[1], check=False)

        self.assertEqual(child.returncode, 97)
        temps = list(created.parent.glob(".kcr-*.tmp"))
        self.assertEqual(len(temps), 1)
        self.assertEqual(temps[0].read_text(), "secret-created")
        self.assertFalse(created.exists())
        self.assertTrue(destination.exists())
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "recovering")

        recovered = self.store.recover_incomplete_transactions(
            workspace_root=self.workspace)

        self.assertEqual(recovered, (transaction["transaction_id"],))
        self.assertEqual(created.read_text(), "secret-created")
        self.assertFalse(destination.exists())
        self.assertEqual(list(created.parent.glob(".kcr-*.tmp")), [])
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "rolled_back")

    def test_recovery_removes_partial_rollback_temp_after_mid_copy_exit(self):
        created = self.workspace / "cross-fs-rollback-partial.txt"
        self.execute(self.prepare("write_file", {
            "path": created, "content": "secret-created"}))
        work = checkpoints._workspace(
            self.workspace, self.store.protected_paths)
        plan = self.store.plan_restore(
            "session1", "cp1", workspace_root=self.workspace)
        transaction = self.store._prepare_restore_transaction(plan, work)
        self.store._set_restore_transaction_status(transaction, "mutating")
        destination = Path(transaction["actions"][0]["destination"])
        self.store._move_to_trash(plan.actions[0], destination)

        code = """
import errno
import os
import sys
from core import checkpoints
store = checkpoints.CheckpointStore(sys.argv[1])
real_replace = os.replace
def force_cross_device(source, destination, *args, **kwargs):
    if source == sys.argv[3] and destination == sys.argv[4]:
        raise OSError(errno.EXDEV, 'forced cross-device')
    return real_replace(source, destination, *args, **kwargs)
def crash_mid_copy(incoming, outgoing, length):
    outgoing.write(incoming.read(4))
    outgoing.flush()
    os.fsync(outgoing.fileno())
    os._exit(99)
checkpoints.os.replace = force_cross_device
checkpoints.shutil.copyfileobj = crash_mid_copy
store.recover_incomplete_transactions(workspace_root=sys.argv[2])
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(self.root / "state"),
             str(self.workspace), destination.name, created.name],
            cwd=Path(__file__).resolve().parents[1], check=False)

        self.assertEqual(child.returncode, 99)
        temps = list(created.parent.glob(".kcr-*.tmp"))
        self.assertEqual(len(temps), 1)
        self.assertEqual(temps[0].read_bytes(), b"secr")
        self.assertFalse(created.exists())
        self.assertTrue(destination.exists())
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "recovering")

        recovered = self.store.recover_incomplete_transactions(
            workspace_root=self.workspace)

        self.assertEqual(recovered, (transaction["transaction_id"],))
        self.assertEqual(created.read_text(), "secret-created")
        self.assertFalse(destination.exists())
        self.assertEqual(list(created.parent.glob(".kcr-*.tmp")), [])
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "rolled_back")

    def test_os_exit_mid_restore_is_recovered_across_processes(self):
        first = self.workspace / "a-crash.txt"
        second = self.workspace / "b-crash.txt"
        first.write_text("first-before")
        second.write_text("second-before")
        self.execute(self.prepare("write_file", {
            "path": first, "content": "first-after"}))
        self.execute(self.prepare("write_file", {
            "path": second, "content": "second-after"}))
        code = """
import os
import sys
from core.checkpoints import CheckpointStore
store = CheckpointStore(sys.argv[1])
def crash(count, row):
    if count == 1:
        os._exit(91)
store.restore('session1', 'cp1', workspace_root=sys.argv[2],
              _after_mutation=crash)
"""
        child = subprocess.run(
            [sys.executable, "-c", code,
             str(self.root / "state"), str(self.workspace)],
            cwd=Path(__file__).resolve().parents[1], check=False)
        self.assertEqual(child.returncode, 91)
        self.assertEqual(first.read_text(), "first-before")
        self.assertEqual(second.read_text(), "second-after")
        pending = self.store.list_restore_transactions()[0]
        self.assertEqual(pending["status"], "mutating")
        self.assertTrue(all(
            row["current_snapshot_sha256"] is not None
            for row in pending["actions"]))

        recovered = self.store.recover_incomplete_transactions(
            workspace_root=self.workspace)
        self.assertEqual(recovered, (pending["transaction_id"],))
        self.assertEqual(first.read_text(), "first-after")
        self.assertEqual(second.read_text(), "second-after")
        rolled_back = self.store.list_restore_transactions()[0]
        self.assertEqual(rolled_back["status"], "rolled_back")
        self.assertTrue(all(
            row["progress"] == "rolled_back"
            for row in rolled_back["actions"]))

        self.store.restore(
            "session1", "cp1", workspace_root=self.workspace)
        self.assertEqual(first.read_text(), "first-before")
        self.assertEqual(second.read_text(), "second-before")
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "committed")

    def test_new_write_recovers_crash_partial_restore_before_capture(self):
        target = self.workspace / "write-after-crash.txt"
        target.write_text("before")
        self.execute(self.prepare("write_file", {
            "path": target, "content": "after"}))
        code = """
import os
import sys
from core.checkpoints import CheckpointStore
store = CheckpointStore(sys.argv[1])
store.restore(
    'session1', 'cp1', workspace_root=sys.argv[2],
    _after_mutation=lambda count, row: os._exit(92))
"""
        child = subprocess.run(
            [sys.executable, "-c", code,
             str(self.root / "state"), str(self.workspace)],
            cwd=Path(__file__).resolve().parents[1], check=False)
        self.assertEqual(child.returncode, 92)
        self.assertEqual(target.read_text(), "before")

        stale = self.prepare("write_file", {
            "path": target, "content": "new-write"})
        with self.assertRaises(checkpoints.ExternalChangeError):
            self.store.capture_prepared("session2", "cp2", stale)

        # Recovery runs before capture, returns the old transaction to its
        # exact pre-restore state, then stale prepared SHA blocks the new write.
        self.assertEqual(target.read_text(), "after")
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "rolled_back")
        self.assertFalse(
            self.store.manifest_path("session2", "cp2").exists())

    def test_execute_rescans_after_path_lock_before_partial_restore_race(self):
        first = self.workspace / "a-execute-race.txt"
        second = self.workspace / "b-execute-race.txt"
        first.write_text("first-before")
        second.write_text("second-before")
        self.execute(self.prepare("write_file", {
            "path": first, "content": "first-after"}))
        self.execute(self.prepare("write_file", {
            "path": second, "content": "second-after"}))

        writer_store = checkpoints.CheckpointStore(self.root / "state")
        writer = self.prepare("write_file", {
            "path": second, "content": "second-writer"})
        capture = writer_store.capture_prepared(
            "session2", "cp2", writer)
        first_scan_done = threading.Event()
        release_writer = threading.Event()
        real_recover = writer_store.recover_incomplete_transactions
        recover_calls = {"count": 0}

        def pause_after_first_scan(**kwargs):
            result = real_recover(**kwargs)
            recover_calls["count"] += 1
            if recover_calls["count"] == 1:
                first_scan_done.set()
                if not release_writer.wait(timeout=10):
                    raise TimeoutError("writer recovery pause timed out")
            return result

        results = []
        failures = []

        def run_writer():
            try:
                results.append(checkpoints.execute_prepared(
                    writer, writer_store, capture=capture))
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(
                writer_store, "recover_incomplete_transactions",
                side_effect=pause_after_first_scan):
            thread = threading.Thread(target=run_writer)
            thread.start()
            try:
                self.assertTrue(first_scan_done.wait(timeout=10))
                child = self.crash_restore_after_first_mutation(93)
                self.assertEqual(child.returncode, 93)
                self.assertEqual(first.read_text(), "first-before")
                self.assertEqual(second.read_text(), "second-after")
            finally:
                release_writer.set()
                thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(first.read_text(), "first-after")
        self.assertEqual(second.read_text(), "second-writer")
        transactions = self.store.list_restore_transactions()
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["status"], "rolled_back")
        self.assertNotEqual(transactions[0]["status"], "recovery_conflict")
        self.assertEqual(
            self.store.recover_incomplete_transactions(
                workspace_root=self.workspace), ())

    def test_capture_rescans_after_path_lock_before_partial_restore_race(self):
        first = self.workspace / "a-capture-race.txt"
        second = self.workspace / "b-capture-race.txt"
        first.write_text("first-before")
        second.write_text("second-before")
        self.execute(self.prepare("write_file", {
            "path": first, "content": "first-after"}))
        self.execute(self.prepare("write_file", {
            "path": second, "content": "second-after"}))

        capture_store = checkpoints.CheckpointStore(self.root / "state")
        prepared = self.prepare("write_file", {
            "path": second, "content": "second-next"})
        first_scan_done = threading.Event()
        release_capture = threading.Event()
        real_recover = capture_store.recover_incomplete_transactions
        recover_calls = {"count": 0}

        def pause_after_first_scan(**kwargs):
            result = real_recover(**kwargs)
            recover_calls["count"] += 1
            if recover_calls["count"] == 1:
                first_scan_done.set()
                if not release_capture.wait(timeout=10):
                    raise TimeoutError("capture recovery pause timed out")
            return result

        captures = []
        failures = []

        def run_capture():
            try:
                captures.append(capture_store.capture_prepared(
                    "session2", "cp2", prepared))
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(
                capture_store, "recover_incomplete_transactions",
                side_effect=pause_after_first_scan):
            thread = threading.Thread(target=run_capture)
            thread.start()
            try:
                self.assertTrue(first_scan_done.wait(timeout=10))
                child = self.crash_restore_after_first_mutation(94)
                self.assertEqual(child.returncode, 94)
                self.assertEqual(first.read_text(), "first-before")
                self.assertEqual(second.read_text(), "second-after")
            finally:
                release_capture.set()
                thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(captures), 1)
        self.assertEqual(first.read_text(), "first-after")
        self.assertEqual(second.read_text(), "second-after")
        self.assertEqual(
            self.store.list_restore_transactions()[0]["status"],
            "rolled_back")
        row = capture_store.load_manifest("session2", "cp2")["files"][
            str(second)]
        self.assertEqual(row["pending_after_sha256"], prepared.after_sha256)

    def test_restore_rescans_after_path_lock_before_partial_restore_race(self):
        first = self.workspace / "a-restore-race.txt"
        second = self.workspace / "b-restore-race.txt"
        first.write_text("first-before")
        second.write_text("second-before")
        self.execute(self.prepare("write_file", {
            "path": first, "content": "first-after"}))
        self.execute(self.prepare("write_file", {
            "path": second, "content": "second-after"}))

        second_store = checkpoints.CheckpointStore(self.root / "state")
        first_scan_done = threading.Event()
        release_restore = threading.Event()
        real_recover = second_store.recover_incomplete_transactions
        recover_calls = {"count": 0}

        def pause_after_first_scan(**kwargs):
            result = real_recover(**kwargs)
            recover_calls["count"] += 1
            if recover_calls["count"] == 1:
                first_scan_done.set()
                if not release_restore.wait(timeout=10):
                    raise TimeoutError("restore recovery pause timed out")
            return result

        results = []
        failures = []

        def run_restore():
            try:
                results.append(second_store.restore(
                    "session1", "cp1", workspace_root=self.workspace))
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(
                second_store, "recover_incomplete_transactions",
                side_effect=pause_after_first_scan):
            thread = threading.Thread(target=run_restore)
            thread.start()
            try:
                self.assertTrue(first_scan_done.wait(timeout=10))
                child = self.crash_restore_after_first_mutation(95)
                self.assertEqual(child.returncode, 95)
                self.assertEqual(first.read_text(), "first-before")
                self.assertEqual(second.read_text(), "second-after")
            finally:
                release_restore.set()
                thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(first.read_text(), "first-before")
        self.assertEqual(second.read_text(), "second-before")
        statuses = [
            transaction["status"]
            for transaction in self.store.list_restore_transactions()]
        self.assertCountEqual(statuses, ["rolled_back", "committed"])
        self.assertNotIn("recovery_conflict", statuses)

    @requires_symlinks
    def test_restore_rejects_symlink_without_touching_target(self):
        target = self.workspace / "safe.txt"
        target.write_text("before")
        prepared = self.prepare(
            "write_file", {"path": target, "content": "after"})
        self.execute(prepared)
        outside = self.root / "outside.txt"
        outside.write_text("outside")
        target.unlink()
        target.symlink_to(outside)

        with self.assertRaisesRegex(checkpoints.RestoreConflict, "符号链接"):
            self.store.restore("session1", "cp1", workspace_root=self.workspace)
        self.assertEqual(outside.read_text(), "outside")



# 受保护路径默认为空 —— 测试必须自己声明要守的东西，否则会在维护者机器上因为
# 读到用户配置而意外变绿、在别人 clone 下来的仓库里变红。
#
# 只打 tools.PROTECTED（词法守卫），**不设环境变量**：环境变量会同时喂给沙箱层，
# 而 sandbox 要求受保护路径真实存在，合成路径必然不存在，会把沙箱打成 fail-closed。
# PROTECTED 虽是 import 时赋值，但守卫是调用时查模块全局，所以 patch 与 import 顺序无关。
# tools 在函数内 import：不是每个用到守卫的测试模块都在顶层 import 它。
_protected_patches = []


def setUpModule():
    from core import tools as _guarded
    for name, value in (("PROTECTED", ("/protected/archive",)),
                        ("PROTECTED_REMOTES", ("archive:bucket",))):
        patch = _mock.patch.object(_guarded, name, value)
        patch.start()
        _protected_patches.append(patch)


def tearDownModule():
    while _protected_patches:
        _protected_patches.pop().stop()

if __name__ == "__main__":
    unittest.main(verbosity=2)
