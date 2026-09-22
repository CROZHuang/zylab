"""M4a session catalog, metadata mutation, and write-lease tests."""
import json
import multiprocessing
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import store
from tests.platform_support import requires_symlinks  # noqa: E402


def _store_paths(root):
    root = Path(root)
    return {
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


def _bind_store_root(root):
    """Bind the store module inside a child process to an isolated test root."""
    paths = _store_paths(root)
    for name, path in paths.items():
        setattr(store, name, path)
    store._DIRS = (
        paths["SESSIONS"], paths["ARCHIVE"], paths["SESSION_LOCKS"],
        paths["SESSION_LEASES"],
    )
    store._SHADOW_OVERRIDE = False


def _agent(session_id, marker="initial", *, messages=None):
    transcript = messages or [
        {"role": "system", "content": "system"},
        {"role": "user", "content": marker},
        {"role": "assistant", "content": f"answer:{marker}"},
    ]
    return SimpleNamespace(
        session_id=session_id,
        model="kimi-test",
        gateway="test-gateway",
        tokens_in=11,
        tokens_out=7,
        last_total=18,
        messages=transcript,
        context_snapshot=lambda: {"marker": marker},
    )


def _first_process_writer(root, session_id, ready, release, results):
    """Hold a lease until the competing writer has attempted its write."""
    _bind_store_root(root)
    owner = "writer-owner-a"
    acquired = False
    try:
        store.ensure_home()
        store.acquire_session_lease(session_id, owner)
        acquired = True
        ready.set()
        if not release.wait(10):
            raise TimeoutError("writer A did not receive release signal")
        store.save_session(
            _agent(session_id, "writer-a"), owner_id=owner)
        results.put(("a", "saved"))
    except Exception as exc:  # pragma: no cover - asserted through child result
        results.put(("a", "error", type(exc).__name__, str(exc)))
    finally:
        if acquired:
            try:
                store.release_session_lease(session_id, owner)
            except Exception:
                pass


def _second_process_writer(root, session_id, ready, results):
    """Attempt both lease acquisition and a guarded save as writer B."""
    _bind_store_root(root)
    owner = "writer-owner-b"
    try:
        store.ensure_home()
        if not ready.wait(10):
            raise TimeoutError("writer B did not observe writer A")
        acquire_busy = False
        save_busy = False
        try:
            store.acquire_session_lease(session_id, owner)
        except store.SessionBusyError:
            acquire_busy = True
        else:
            store.release_session_lease(session_id, owner)
        try:
            store.save_session(
                _agent(session_id, "writer-b"), owner_id=owner)
        except store.SessionBusyError:
            save_busy = True
        results.put(("b", acquire_busy, save_busy))
    except Exception as exc:  # pragma: no cover - asserted through child result
        results.put(("b", "error", type(exc).__name__, str(exc)))


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "zylab-state"
        self.paths = _store_paths(self.root)
        self.stack = ExitStack()
        for name, path in self.paths.items():
            self.stack.enter_context(mock.patch.object(store, name, path))
        self.stack.enter_context(mock.patch.object(
            store,
            "_DIRS",
            (
                self.paths["SESSIONS"], self.paths["ARCHIVE"],
                self.paths["SESSION_LOCKS"], self.paths["SESSION_LEASES"],
            ),
        ))
        self.stack.enter_context(mock.patch.object(
            store, "_SHADOW_OVERRIDE", False))
        store.ensure_home()

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def save(self, session_id="session001", marker="initial", **kwargs):
        return store.save_session(_agent(session_id, marker), **kwargs)

    def test_instruction_loading_flags_round_trip_in_session_record(self):
        runtime = _agent("instruction01", "instruction flags")
        runtime.load_md = True
        runtime.load_user_md = False
        runtime.load_project_md = True
        runtime.load_skills = False

        store.save_session(runtime)
        loaded = store.load_session(runtime.session_id)

        self.assertTrue(loaded["load_md"])
        self.assertFalse(loaded["load_user_md"])
        self.assertTrue(loaded["load_project_md"])
        self.assertFalse(loaded["load_skills"])

    def test_catalog_list_uses_ready_index_without_reading_transcript(self):
        self.save("catalog001", "searchable title")
        transcript_dir = self.paths["SESSIONS"]
        original_read_text = Path.read_text

        def guarded_read_text(path, *args, **kwargs):
            candidate = Path(path)
            if candidate.parent == transcript_dir:
                raise AssertionError(f"catalog opened transcript: {candidate}")
            return original_read_text(candidate, *args, **kwargs)

        with mock.patch.object(Path, "read_text", new=guarded_read_text):
            rows = store.list_session_summaries(
                status="active", query="searchable", cwd=self.root)

        self.assertEqual([row["id"] for row in rows], ["catalog001"])
        self.assertEqual(rows[0]["count"], 2)

    def test_catalog_recovers_when_index_write_fails_after_canonical(self):
        self.save("seed0001", "seed")
        path = self.paths["SESSIONS"] / "orphan001.json"

        with mock.patch.object(
                store, "_write_session_index_unlocked",
                side_effect=OSError("injected catalog failure")):
            with self.assertRaisesRegex(OSError, "catalog failure"):
                self.save("orphan001", "canonical survived")

        self.assertTrue(path.is_file())
        self.assertTrue(self.paths["SESSION_INDEX_DIRTY"].is_file())
        loaded = store.load_session("orphan001")
        self.assertEqual(loaded["messages"][1]["content"],
                         "canonical survived")
        self.assertFalse(self.paths["SESSION_INDEX_DIRTY"].exists())

    def test_catalog_rebuild_degrades_bad_context_tokens_per_session(self):
        damaged = {
            "badlist001": [1],
            "baddict001": {"unexpected": 1},
            "badnan001": float("nan"),
            "badinf001": float("inf"),
            "badbool001": True,
        }
        damaged_paths = set()
        for session_id, value in damaged.items():
            path = self.save(session_id, "damaged metadata")
            record = json.loads(path.read_text(encoding="utf-8"))
            record["last_context_tokens"] = value
            path.write_text(json.dumps(record), encoding="utf-8")
            damaged_paths.add(str(path))

        negative_path = self.save("negative001", "negative metadata")
        negative_record = json.loads(
            negative_path.read_text(encoding="utf-8"))
        negative_record["last_context_tokens"] = -9
        negative_path.write_text(
            json.dumps(negative_record), encoding="utf-8")
        self.save("goodtokens01", "valid metadata")
        self.paths["SESSION_INDEX"].unlink()

        rows = store.list_session_summaries(
            limit=None, status="active", scope="all", cwd=self.root)
        by_id = {row["id"]: row for row in rows}

        for session_id in damaged:
            self.assertEqual(by_id[session_id]["last_context_tokens"], 0)
        self.assertEqual(by_id["negative001"]["last_context_tokens"], 0)
        self.assertEqual(by_id["goodtokens01"]["last_context_tokens"], 18)
        index = json.loads(
            self.paths["SESSION_INDEX"].read_text(encoding="utf-8"))
        normalization_errors = {
            entry["path"] for entry in index["errors"]
            if entry.get("kind") == "summary_normalization"
        }
        self.assertEqual(normalization_errors, damaged_paths)
        self.assertTrue(all(
            "last_context_tokens" in entry["error"]
            for entry in index["errors"]
            if entry.get("kind") == "summary_normalization"
        ))

    def test_catalog_normalizes_legacy_fork_scalars_once(self):
        bad_path = self.save("badforkmeta1", "bad fork metadata")
        bad = json.loads(bad_path.read_text(encoding="utf-8"))
        bad["fork_seq"] = 1.5
        bad["fork_message_count"] = {"bad": True}
        bad_path.write_text(json.dumps(bad), encoding="utf-8")
        legacy_path = self.save("legacyfork01", "legacy fork metadata")
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        legacy["fork_seq"] = "7"
        legacy["fork_message_count"] = "2"
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        self.paths["SESSION_INDEX"].unlink()

        original = store._rebuild_session_index_unlocked
        with mock.patch.object(
                store, "_rebuild_session_index_unlocked",
                wraps=original) as rebuild:
            first = store.list_session_summaries(
                limit=None, status="active", scope="all", cwd=self.root)
            second = store.list_session_summaries(
                limit=None, status="active", scope="all", cwd=self.root)

        by_id = {row["id"]: row for row in first}
        self.assertIsNone(by_id["badforkmeta1"]["fork_seq"])
        self.assertIsNone(by_id["badforkmeta1"]["fork_message_count"])
        self.assertEqual(by_id["legacyfork01"]["fork_seq"], 7)
        self.assertEqual(by_id["legacyfork01"]["fork_message_count"], 2)
        self.assertEqual(second, first)
        self.assertEqual(rebuild.call_count, 1)

    def test_atomic_replace_and_dirty_unlink_fsync_parent_directory(self):
        target = self.root / "durable.txt"
        with mock.patch.object(
                store, "_fsync_directory",
                wraps=store._fsync_directory) as fsync_directory:
            store._atomic_write_text(target, "durable")
        fsync_directory.assert_called_once_with(target.parent)
        self.assertEqual(target.read_text(encoding="utf-8"), "durable")

        dirty = self.paths["SESSION_INDEX_DIRTY"]
        store._atomic_write_text(dirty, "dirty")
        with mock.patch.object(
                store, "_fsync_directory",
                wraps=store._fsync_directory) as fsync_directory:
            store._clear_session_index_dirty_unlocked()
        fsync_directory.assert_called_once_with(dirty.parent)
        self.assertFalse(dirty.exists())

    def test_invalid_catalog_key_cannot_escape_sessions_via_prefix(self):
        escape_dir = self.paths["SESSIONS"] / "escape"
        escape_dir.mkdir()
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({
            "id": "outside",
            "status": "active",
            "messages": [{"role": "system", "content": "outside"}],
        }), encoding="utf-8")
        malicious_id = "escape/../../outside"
        row = {
            "id": malicious_id,
            "title": "outside",
            "status": "active",
            "model": "",
            "gateway": "",
            "cwd": str(self.root),
            "repo_root": "",
            "git_branch": "",
            "created": "",
            "updated": "",
            "count": 0,
            "last_context_tokens": 0,
            "parent_session_id": "",
            "fork_seq": None,
        }
        self.paths["SESSION_INDEX"].write_text(json.dumps({
            "version": store.SESSION_INDEX_VERSION,
            "updated": "",
            "sessions": {malicious_id: row},
            "errors": [],
        }), encoding="utf-8")

        with self.assertRaises(FileNotFoundError):
            store.load_session("escape")

    def test_save_preserves_unknown_metadata_and_archived_status(self):
        path = self.save("preserve001", "before")
        store.archive_session("preserve001")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["future_metadata"] = {
            "schema": 7,
            "nested": ["keep", {"also": "keep"}],
        }
        path.write_text(json.dumps(record), encoding="utf-8")

        self.save("preserve001", "after")
        saved = store.load_session_preview("preserve001")

        self.assertEqual(saved["status"], "archived")
        self.assertEqual(saved["future_metadata"], record["future_metadata"])
        self.assertEqual(saved["messages"][1]["content"], "after")

    def test_checkpoint_audit_is_canonical_and_preserves_unknown_fields(self):
        path = self.save("audit0001", "before")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["future_metadata"] = {"schema": 8, "keep": True}
        path.write_text(json.dumps(record), encoding="utf-8")

        entry = store.append_checkpoint_audit(
            "audit0001", "rewind_requested",
            {"checkpoint_id": "cp-one", "paths": ["/tmp/a"]})
        saved = store.load_session_preview("audit0001")

        self.assertEqual(saved["future_metadata"], record["future_metadata"])
        self.assertEqual(saved["checkpoint_audit"], [entry])
        self.assertEqual(entry["schema_version"], 1)
        self.assertEqual(entry["kind"], "rewind_requested")

    def test_checkpoint_audit_respects_live_lease_owner(self):
        self.save("auditlease1", "before")
        store.acquire_session_lease("auditlease1", "owner-a")
        try:
            with self.assertRaises(store.SessionBusyError):
                store.append_checkpoint_audit(
                    "auditlease1", "rewind_requested", {},
                    owner_id="owner-b")
            store.append_checkpoint_audit(
                "auditlease1", "rewind_requested", {},
                owner_id="owner-a")
        finally:
            store.release_session_lease("auditlease1", "owner-a")

        self.assertEqual(
            len(store.load_session_preview("auditlease1")[
                "checkpoint_audit"]),
            1)

    def test_checkpoint_audit_rejects_corrupt_history_without_replacing_it(self):
        path = self.save("badaudit01", "before")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["checkpoint_audit"] = {"bad": True}
        path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaises(store.SessionCorruptError):
            store.append_checkpoint_audit(
                "badaudit01", "rewind_requested", {})

        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["checkpoint_audit"], {"bad": True})

    def test_first_legacy_save_persists_missing_repo_metadata(self):
        path = self.save("legacyrepo01", "before")
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("repo_root", None)
        record.pop("git_branch", None)
        path.write_text(json.dumps(record), encoding="utf-8")

        with mock.patch.object(store, "repo_context", return_value={
                "cwd": str(self.root),
                "repo_root": str(self.root / "repo"),
                "git_branch": "feature",
        }):
            store.save_session(_agent("legacyrepo01", "after"))

        saved = store.load_session("legacyrepo01")
        self.assertEqual(saved["repo_root"], str(self.root / "repo"))
        self.assertEqual(saved["git_branch"], "feature")

    def test_corrupt_record_fails_closed_without_overwrite(self):
        path = self.save("corrupt001")
        corrupt = b'{"id":"corrupt001","messages":'
        path.write_bytes(corrupt)

        with self.assertRaises(store.SessionCorruptError):
            self.save("corrupt001", "replacement")
        self.assertEqual(path.read_bytes(), corrupt)
        with self.assertRaises(store.SessionCorruptError):
            store.load_session("corrupt001")

    @requires_symlinks
    def test_symlink_record_is_rejected_without_writing_target(self):
        session_id = "symlink001"
        target = self.root / "outside.json"
        target.write_text("sentinel", encoding="utf-8")
        session_path = self.paths["SESSIONS"] / f"{session_id}.json"
        session_path.symlink_to(target)

        with self.assertRaises(store.SessionCorruptError):
            self.save(session_id, "replacement")
        self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")

    def test_legacy_migration_is_idempotent_and_catalog_aware(self):
        legacy = {
            "model": "legacy-model",
            "cwd": str(self.root),
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "legacy question"},
            ],
        }
        source = self.paths["SESSIONS"] / "last.json"
        encoded = json.dumps(legacy, ensure_ascii=False)
        source.write_text(encoded, encoding="utf-8")

        first = store.migrate_legacy()

        self.assertFalse(source.exists())
        self.assertEqual(
            [row["id"] for row in store.list_session_summaries(
                status="active", scope="all", cwd=self.root)],
            [first],
        )
        source.write_text(encoded, encoding="utf-8")
        second = store.migrate_legacy()
        self.assertEqual(second, first)
        self.assertEqual(
            [row["id"] for row in store.list_session_summaries(
                status="active", scope="all", cwd=self.root)],
            [first],
        )

    def test_reappearing_legacy_file_never_overwrites_continued_live_chat(self):
        legacy = {
            "model": "legacy-model",
            "cwd": str(self.root),
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "legacy"},
            ],
        }
        source = self.paths["SESSIONS"] / "last.json"
        encoded = json.dumps(legacy, ensure_ascii=False)
        source.write_text(encoded, encoding="utf-8")
        session_id = store.migrate_legacy()
        owner = "continued-owner"
        store.acquire_session_lease(session_id, owner)
        try:
            store.save_session(
                _agent(session_id, "continued"), owner_id=owner)
            source.write_text(encoded, encoding="utf-8")

            repeated = store.migrate_legacy()

            self.assertEqual(repeated, session_id)
            self.assertEqual(
                store.load_session(session_id)["messages"][1]["content"],
                "continued",
            )
            self.assertTrue(store.session_lease(session_id)["live"])
        finally:
            store.release_session_lease(session_id, owner)

    def test_safe_identifier_and_unique_prefix_resolution(self):
        self.save("alpha1111", "first")
        self.save("alpha2222", "second")
        self.save("unique333", "only")

        self.assertEqual(store.load_session("unique")["id"], "unique333")
        with self.assertRaisesRegex(ValueError, "匹配到 2 个"):
            store.load_session("alpha")
        for unsafe in ("ab", "../escape", "a/b", "alpha!bad", "/etc/passwd"):
            with self.subTest(identifier=unsafe):
                with self.assertRaises(ValueError):
                    store.load_session(unsafe)

    def test_rename_archive_and_unarchive_update_catalog(self):
        self.save("lifecycle01", "original")

        renamed = store.rename_session("lifec", "  renamed   session  ")
        self.assertEqual(renamed["title"], "renamed session")
        archived = store.archive_session("lifecycle01")
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(
            store.list_session_summaries(status="active", cwd=self.root), [])
        self.assertEqual(
            [row["id"] for row in store.list_session_summaries(
                status="archived", cwd=self.root)],
            ["lifecycle01"],
        )
        with self.assertRaises(FileNotFoundError):
            store.load_session("lifecycle01")

        restored = store.unarchive_session("lifecycle01")
        self.assertEqual(restored["status"], "active")
        self.assertEqual(store.load_session("lifecycle01")["title"],
                         "renamed session")

    def test_archive_preserves_messages_exactly(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": [{"type": "text", "text": "问"}]},
            {"role": "assistant", "content": "答", "tool_calls": [
                {"id": "call-1", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ]
        store.save_session(_agent("archive001", messages=messages))
        before = store.load_session("archive001")["messages"]

        store.archive_session("archive001")
        after = store.load_session_preview("archive001")["messages"]

        self.assertEqual(after, before)

    def test_fork_is_immutable_and_drops_session_grants(self):
        parent_path = store.save_session(
            _agent("forkparent01", "branch source"),
            session_grants={"bash", "edit_file"},
        )
        parent_before = parent_path.read_bytes()

        child = store.fork_session(
            "forkparent01", target_id="forkchild001",
            message_count=2, cwd=self.root / "branch-worktree")

        self.assertEqual(parent_path.read_bytes(), parent_before)
        self.assertEqual(child["parent_session_id"], "forkparent01")
        self.assertEqual(child["fork_message_count"], 2)
        self.assertIsNone(child["fork_seq"])
        self.assertEqual(child["messages"], _agent(
            "unused", "branch source").messages[:2])
        self.assertEqual(child["session_grants"], [])
        self.assertEqual(child["tokens_in"], 0)
        self.assertEqual(child["tokens_out"], 0)
        self.assertIsNone(child["context"])
        self.assertEqual(
            store.load_session("forkparent01")["session_grants"],
            ["bash", "edit_file"],
        )

    def test_full_fork_preserves_context_and_real_seq_metadata(self):
        task_plan = {
            "revision": 3,
            "explanation": "continue after restart",
            "items": [
                {"content": "inspect", "status": "completed"},
                {"content": "verify", "status": "in_progress"},
            ],
        }
        store.save_session(
            _agent("fullparent01", "full branch"),
            task_plan=task_plan, workflow_auto=True)

        child = store.fork_session(
            "fullparent01", target_id="fullchild001", fork_seq=17)

        self.assertEqual(child["fork_seq"], 17)
        self.assertEqual(child["fork_message_count"], 3)
        self.assertEqual(child["context"], {"marker": "full branch"})
        self.assertEqual(child["last_context_tokens"], 18)
        self.assertEqual(child["task_plan"], task_plan)
        self.assertTrue(child["workflow_auto"])

        historical = store.fork_session(
            "fullparent01", target_id="historychild1", message_count=2)
        self.assertEqual(historical["task_plan"], {
            "revision": 0, "explanation": "", "items": []})

    def test_fork_rejects_non_integral_cutoffs_without_creating_child(self):
        store.save_session(_agent("strictparent1", "strict cutoff"))
        for field, value in (
                ("message_count", 1.9),
                ("message_count", "2"),
                ("message_count", True),
                ("fork_seq", 2.0),
                ("fork_seq", "2"),
                ("fork_seq", True)):
            with self.subTest(field=field, value=value):
                kwargs = {field: value}
                with self.assertRaisesRegex(ValueError, field):
                    store.fork_session(
                        "strictparent1", target_id="strictchild1", **kwargs)
                self.assertFalse(
                    (self.paths["SESSIONS"] / "strictchild1.json").exists())

    def test_fork_requires_matching_target_lease_owner(self):
        store.save_session(_agent("leaseparent1", "lease branch"))
        store.acquire_session_lease("leasechild01", "owner-a")
        try:
            with self.assertRaises(store.SessionBusyError):
                store.fork_session(
                    "leaseparent1", target_id="leasechild01",
                    owner_id="owner-b")
            child = store.fork_session(
                "leaseparent1", target_id="leasechild01",
                owner_id="owner-a")
        finally:
            store.release_session_lease("leasechild01", "owner-a")

        self.assertEqual(child["parent_session_id"], "leaseparent1")

    def test_catalog_groups_nested_branches_by_root(self):
        self.save("rootbranch01", "root")
        store.fork_session(
            "rootbranch01", target_id="childbranch1")
        store.fork_session(
            "childbranch1", target_id="grandbranch1")
        self.save("unrelated01", "other")

        rows = store.list_session_summaries(
            limit=None, scope="all", status="active", cwd=self.root)
        by_id = {row["id"]: row for row in rows}
        branch_positions = [
            index for index, row in enumerate(rows)
            if row["id"] in {"rootbranch01", "childbranch1", "grandbranch1"}
        ]

        self.assertEqual(
            branch_positions,
            list(range(min(branch_positions), max(branch_positions) + 1)),
        )
        self.assertEqual(by_id["rootbranch01"]["branch_depth"], 0)
        self.assertEqual(by_id["childbranch1"]["branch_depth"], 1)
        self.assertEqual(by_id["grandbranch1"]["branch_depth"], 2)
        self.assertTrue(all(
            by_id[item]["branch_root_id"] == "rootbranch01"
            for item in ("rootbranch01", "childbranch1", "grandbranch1")
        ))

    def test_live_lease_blocks_second_owner_save_and_archive(self):
        session_id = "leased001"
        self.save(session_id)
        owner = "owner-primary"
        store.acquire_session_lease(session_id, owner)
        try:
            with self.assertRaises(store.SessionBusyError):
                store.acquire_session_lease(session_id, "owner-secondary")
            with self.assertRaises(store.SessionBusyError):
                store.save_session(
                    _agent(session_id, "secondary"), owner_id="owner-secondary")
            with self.assertRaises(store.SessionBusyError):
                store.archive_session(session_id)

            store.save_session(_agent(session_id, "primary"), owner_id=owner)
            self.assertEqual(
                store.load_session(session_id)["messages"][1]["content"],
                "primary",
            )
        finally:
            store.release_session_lease(session_id, owner)

    def test_release_allows_a_different_owner_to_reacquire(self):
        session_id = "reacquire01"
        first = store.acquire_session_lease(session_id, "owner-one")
        self.assertEqual(first["owner_id"], "owner-one")
        self.assertTrue(store.release_session_lease(session_id, "owner-one"))
        self.assertIsNone(store.session_lease(session_id))

        second = store.acquire_session_lease(session_id, "owner-two")
        self.assertEqual(second["owner_id"], "owner-two")
        self.assertTrue(store.release_session_lease(session_id, "owner-two"))

    def test_stale_local_lease_can_be_taken_over(self):
        session_id = "stalelease01"
        stale = store.acquire_session_lease(session_id, "dead-owner")
        stale.update({
            "hostname": socket.gethostname(),
            "pid": 2_147_483_647,
            "proc_start": "definitely-not-a-live-process",
            "heartbeat_at": "2000-01-01T00:00:00+00:00",
        })
        lease_path = self.paths["SESSION_LEASES"] / f"{session_id}.json"
        lease_path.write_text(json.dumps(stale), encoding="utf-8")

        replacement = store.acquire_session_lease(session_id, "new-owner")

        self.assertEqual(replacement["owner_id"], "new-owner")
        self.assertTrue(replacement["proc_start"])
        store.release_session_lease(session_id, "new-owner")

    def test_semantically_corrupt_lease_is_fail_closed_then_quarantinable(self):
        session_id = "badlease001"
        path = self.paths["SESSION_LEASES"] / f"{session_id}.json"
        path.write_text("{}", encoding="utf-8")

        lease = store.session_lease(session_id)
        self.assertTrue(lease["live"])
        self.assertTrue(lease["_corrupt"])
        with self.assertRaisesRegex(
                store.SessionBusyError, "repair-lease"):
            store.acquire_session_lease(session_id, "new-owner")

        quarantined = store.quarantine_corrupt_session_lease(session_id)
        self.assertFalse(path.exists())
        self.assertTrue(quarantined.is_file())
        replacement = store.acquire_session_lease(
            session_id, "new-owner")
        self.assertEqual(replacement["owner_id"], "new-owner")
        store.release_session_lease(session_id, "new-owner")

    def test_different_sessions_can_hold_leases_concurrently(self):
        rendezvous = threading.Barrier(2)
        held = []
        failures = []

        def worker(session_id, owner):
            try:
                store.acquire_session_lease(session_id, owner)
                rendezvous.wait(timeout=5)
                held.append((session_id, owner))
                store.release_session_lease(session_id, owner)
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [
            threading.Thread(target=worker, args=("parallel01", "owner-a")),
            threading.Thread(target=worker, args=("parallel02", "owner-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertCountEqual(held, [
            ("parallel01", "owner-a"),
            ("parallel02", "owner-b"),
        ])

    @unittest.skipUnless(
        sys.platform.startswith("linux")
        and "fork" in multiprocessing.get_all_start_methods(),
        # 守卫原来只问「有没有 fork」，而 macOS **有** fork（只是不是默认）——
        # 于是这条用例在那边照跑，而 macOS 上 fork 一个带线程的进程是
        # 官方标注不安全的（3.12 起还会告警）。理由行里本来就写着
        # 「Linux process identity」，守卫跟上它（2026-09-22）。
        "lease liveness uses Linux process identity",
    )
    def test_multiprocess_second_writer_is_rejected_by_live_lease(self):
        session_id = "process001"
        self.save(session_id, "before")
        ctx = multiprocessing.get_context("fork")
        ready = ctx.Event()
        release = ctx.Event()
        results = ctx.Queue()
        first = ctx.Process(
            target=_first_process_writer,
            args=(self.root, session_id, ready, release, results),
        )
        second = ctx.Process(
            target=_second_process_writer,
            args=(self.root, session_id, ready, results),
        )

        first.start()
        self.assertTrue(ready.wait(10), "writer A did not acquire its lease")
        second.start()
        second.join(10)
        release.set()
        first.join(10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        outcomes = {item[0]: item[1:] for item in (
            results.get(timeout=2), results.get(timeout=2))}
        results.close()
        # join_thread() 没有超时参数：feeder 线程卡住就是永久挂着。
        # 放到后台线程里等，主线程有界收场——挂住也是失败，不是 cancelled。
        joiner = threading.Thread(target=results.join_thread, daemon=True)
        joiner.start()
        joiner.join(10)
        self.assertFalse(joiner.is_alive(), "results 队列的 feeder 线程没收干净")
        self.assertEqual(outcomes["a"], ("saved",))
        self.assertEqual(outcomes["b"], (True, True))
        saved = store.load_session(session_id)
        self.assertEqual(saved["messages"][1]["content"], "writer-a")


if __name__ == "__main__":
    unittest.main()
