"""SQLite state journal、shadow facade 与 agent 事件接线测试。"""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from unittest import mock as _mock
from tests.platform_support import assert_mode  # noqa: E402
from core import agent as agent_mod
from core import client, state, store
from tests.platform_support import requires_symlinks  # noqa: E402


def session_rec(session_id="s1", **overrides):
    rec = {
        "id": session_id,
        "title": "测试会话",
        "model": "kimi-test",
        "gateway": "test",
        "cwd": "/tmp/workspace/project",
        "created": "2026-08-24T00:00:00+00:00",
        "updated": "2026-08-24T00:00:01+00:00",
        "tokens_in": 3,
        "tokens_out": 2,
    }
    rec.update(overrides)
    return rec


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_permissions_and_integrity(self):
        with state.StateStore(self.db_path) as db:
            self.assertEqual(db.journal_mode, "WAL")
            self.assertEqual(db.integrity_check(), ["ok"])
            self.assertEqual(db.foreign_key_violations(), [])
            self.assertEqual(
                db.counts(),
                {"sessions": 0, "events": 0, "requests": 0,
                 "legacy_imports": 0, "migration_errors": 0})
        assert_mode(self, self.db_path, 0o600)

    @requires_symlinks
    def test_rejects_symlink_database_without_touching_target(self):
        target = Path(self.tmp.name) / "target.bin"
        target.write_bytes(b"sentinel")
        link = Path(self.tmp.name) / "linked.sqlite3"
        link.symlink_to(target)
        with self.assertRaisesRegex(state.StateError, "符号链接"):
            state.StateStore(link)
        self.assertEqual(target.read_bytes(), b"sentinel")

    def test_rejects_protected_path_before_creating_anything(self):
        with _mock.patch.dict(
                os.environ,
                {"ZYLAB_PROTECTED_PATHS": "/protected/archive"}), \
                self.assertRaisesRegex(
                state.StateError, "受保护路径"):
            state.StateStore(
                "/protected/archive/zylab-forbidden/state.sqlite3")

    def test_future_schema_is_rejected_before_any_schema_or_journal_write(self):
        future = Path(self.tmp.name) / "future.sqlite3"
        con = sqlite3.connect(future)
        con.executescript(
            """
            CREATE TABLE schema_meta (
                singleton INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                migrated_from TEXT
            );
            INSERT INTO schema_meta VALUES (1, 999, 'future', NULL);
            CREATE TABLE future_only (sentinel TEXT);
            """)
        con.commit()
        con.close()
        os.chmod(future, 0o640)
        before = future.read_bytes()

        with self.assertRaisesRegex(state.SchemaMismatch, "schema=999"):
            state.StateStore(future)

        self.assertEqual(future.read_bytes(), before)
        assert_mode(self, future, 0o640)
        con = sqlite3.connect(future)
        try:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            journal = con.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(tables, {"schema_meta", "future_only"})
        self.assertEqual(journal, "delete")

    def test_existing_v1_database_gets_additive_agent_runs_table(self):
        with state.StateStore(self.db_path) as db:
            db._conn.execute("DROP TABLE agent_runs")
            version = db._conn.execute(
                "SELECT version FROM schema_meta WHERE singleton = 1"
            ).fetchone()["version"]
        with state.StateStore(self.db_path) as reopened:
            tables = {
                row[0] for row in reopened._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            integrity = reopened.integrity_check()

        self.assertEqual(version, 1)
        self.assertIn("agent_runs", tables)
        self.assertEqual(integrity, ["ok"])

    def test_sequence_message_count_and_pagination(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec())
            first = db.append_event(
                "s1", "user_message", {"text": "hello"}, turn_id="t1")
            second = db.append_event(
                "s1", "assistant_message", {"text": "hi"}, turn_id="t1")
            third = db.append_event(
                "s1", "tool_finished", {"content": "ok"}, turn_id="t1")
            self.assertEqual(
                [first["seq"], second["seq"], third["seq"]], [1, 2, 3])
            self.assertEqual(db.get_session("s1")["message_count"], 3)
            page = db.get_events("s1", after_seq=1, limit=1)
            self.assertEqual([row["seq"] for row in page], [2])
            self.assertEqual(page[0]["payload"], {"text": "hi"})

    def test_resume_archive_and_fork_metadata(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec("parent", title="Parent"))
            db.upsert_session(session_rec(
                "child", title="Child", parent_session_id="parent",
                fork_seq=0))
            db.upsert_session(session_rec(
                "parent", title="Parent", status="archived",
                updated="2026-08-24T00:00:03+00:00"))
            self.assertEqual(db.get_session("child")["parent_session_id"],
                             "parent")
            self.assertEqual(db.get_session("child")["fork_seq"], 0)
            self.assertEqual(
                [row["id"] for row in db.list_sessions(status="active")],
                ["child"])
            self.assertEqual(
                [row["id"] for row in db.list_sessions(status="archived")],
                ["parent"])

    def test_session_grant_crud_and_replace_are_transactional(self):
        stamp = "2026-08-25T01:00:00.000+00:00"
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec())
            first = db.set_session_grant(
                "s1", "write_file", "allow",
                source="user", created_at=stamp)
            replaced = db.set_session_grant(
                "s1", "write_file", "deny",
                source="user", created_at=stamp)
            repeated = db.set_session_grant(
                "s1", "write_file", "deny",
                source="user",
                created_at="2026-08-25T02:00:00.000+00:00")
            db.set_session_grant(
                "s1", "write_file", "ask",
                source="project", created_at=stamp)

            self.assertEqual(first["decision"], "allow")
            self.assertEqual(replaced["decision"], "deny")
            self.assertEqual(repeated, replaced)
            self.assertEqual([
                (row["matcher"], row["decision"], row["source"])
                for row in db.list_session_grants("s1")
            ], [
                ("write_file", "ask", "project"),
                ("write_file", "deny", "user"),
            ])
            self.assertEqual(db.delete_session_grant(
                "s1", "write_file", source="user"), 1)

            wanted = [
                {"matcher": "bash", "decision": "ask", "source": "user"},
                {"matcher": "edit_file", "decision": "allow",
                 "source": "user"},
            ]
            db.replace_session_grants("s1", wanted, created_at=stamp)
            before = db.list_session_grants("s1")
            db.replace_session_grants("s1", wanted, created_at=stamp)
            after = db.list_session_grants("s1")
            self.assertEqual(after, before)
            self.assertEqual([
                (row["matcher"], row["decision"], row["source"])
                for row in after
            ], [
                ("bash", "ask", "user"),
                ("edit_file", "allow", "user"),
            ])
            self.assertEqual(
                [row["created_at"] for row in after], [stamp, stamp])

            with self.assertRaisesRegex(state.StateError, "重复"):
                db.replace_session_grants("s1", [
                    {"matcher": "bash", "decision": "allow",
                     "source": "user"},
                    {"matcher": "bash", "decision": "deny",
                     "source": "user"},
                ])
            self.assertEqual(db.list_session_grants("s1"), after)
            for invalid in (
                    42,
                    [{"matcher": "bash", "decision": None,
                      "source": "user"}],
                    [{"matcher": "bash", "decision": "allow",
                      "source": None}]):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(state.StateError):
                        db.replace_session_grants("s1", invalid)
            self.assertEqual(db.list_session_grants("s1"), after)

            order_sensitive = [
                {"matcher": "bash", "decision": "deny",
                 "source": "project"},
                {"matcher": "bash", "decision": "allow",
                 "source": "user"},
            ]
            ordered_once = db.replace_session_grants(
                "s1", order_sensitive, created_at=stamp)
            ordered_twice = db.replace_session_grants(
                "s1", reversed(order_sensitive), created_at=stamp)
            self.assertEqual(ordered_twice, ordered_once)

            with self.assertRaisesRegex(state.StateError, "session 不存在"):
                db.set_session_grant("missing", "bash")

    def test_fork_copies_cutoff_audit_without_runtime_projections(self):
        agent_payload = {
            "id": "agent-source",
            "parent_session_id": "source",
            "transcript_session_id": "agent-transcript",
            "name": "reader",
            "task": "inspect",
            "state": "queued",
            "model": "m",
            "gateway": "g",
            "created_at": "2026-08-25T00:00:00.000+00:00",
            "updated_at": "2026-08-25T00:00:00.000+00:00",
        }
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec("source", title="Source"))
            db.append_event(
                "source", "user_message", {"text": "question"},
                turn_id="turn-1")
            db.enqueue_input("source", "queued-1", "next_turn", "later")
            db.append_event(
                "source", "assistant_message", {"text": "working"},
                turn_id="turn-1")
            db.append_event(
                "source", "task_started", {"task_id": "task-1"},
                turn_id="turn-1")
            db.append_event(
                "source", "permission_decided", {"decision": "once"},
                turn_id="turn-1")
            db.append_event(
                "source", "agent_spawned", agent_payload,
                turn_id="turn-1")
            db.append_event(
                "source", "tool_started", {"tool_call_id": "call-1"},
                turn_id="turn-1")
            db.append_event(
                "source", "tool_finished", {"tool_call_id": "call-1"},
                turn_id="turn-1")
            cutoff = db.get_session("source")["last_seq"]
            db.append_event(
                "source", "user_message", {"text": "after cutoff"},
                turn_id="turn-2")
            db.set_session_grant(
                "source", "write_file", "allow", source="user")

            source_before = db.get_session("source")
            events_before = db.get_events("source")
            grants_before = db.list_session_grants("source")
            queue_before = db.list_queued_inputs("source", state=None)
            agents_before = db.list_agent_runs("source")

            child = db.fork_session(
                "source", cutoff,
                session_rec("forked", title="Forked", tokens_in=0,
                            tokens_out=0))
            child_events = db.get_events("forked")

            self.assertEqual(child["parent_session_id"], "source")
            self.assertEqual(child["fork_seq"], cutoff)
            self.assertEqual(
                [event["seq"] for event in child_events],
                list(range(1, len(child_events) + 1)))
            self.assertEqual(
                [event["kind"] for event in child_events], [
                    "user_message",
                    "assistant_message",
                    "permission_decided",
                    "tool_started",
                    "tool_finished",
                    "branch_created",
                ])
            self.assertEqual(
                [event["source_key"] for event in child_events], [
                    "fork:source:1",
                    "fork:source:3",
                    "fork:source:5",
                    "fork:source:7",
                    "fork:source:8",
                    f"fork-created:source:{cutoff}",
                ])
            self.assertEqual(child["message_count"], 3)
            self.assertEqual(db.list_session_grants("forked"), [])
            self.assertEqual(
                db.list_queued_inputs("forked", state=None), [])
            self.assertEqual(db.list_agent_runs("forked"), [])

            self.assertEqual(db.get_session("source"), source_before)
            self.assertEqual(db.get_events("source"), events_before)
            self.assertEqual(
                db.list_session_grants("source"), grants_before)
            self.assertEqual(
                db.list_queued_inputs("source", state=None), queue_before)
            self.assertEqual(db.list_agent_runs("source"), agents_before)

    def test_fork_cutoff_matches_canonical_projection_not_later_journal_head(self):
        canonical = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        later = {"role": "user", "content": "second terminal"}
        with state.StateStore(self.db_path) as db:
            db.bootstrap_session(session_rec("source"), canonical)
            expected = db.append_event(
                "source", "turn_completed", {"reason": "stop"})["seq"]
            db.append_event(
                "source", "user_message", {"message": later})
            db.append_event(
                "source", "turn_started", {"queue_id": "later"})

            cutoff = db.fork_seq_for_messages("source", canonical)

            self.assertEqual(cutoff, expected)
            self.assertLess(cutoff, db.get_session("source")["last_seq"])
            self.assertIsNone(db.fork_seq_for_messages(
                "source",
                canonical[:-1]
                + [{"role": "assistant", "content": "diverged"}],
            ))

    def test_fork_retry_is_idempotent_and_preserves_continued_child(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec("source"))
            db.append_event("source", "user_message", {"text": "one"})
            cutoff = db.append_event(
                "source", "assistant_message", {"text": "two"})["seq"]
            target = session_rec(
                "forked", title="Forked", tokens_in=0, tokens_out=0)

            db.fork_session("source", cutoff, target)
            db.append_event("forked", "user_message", {"text": "continued"})
            before = db.get_events("forked")
            retried = db.fork_session("source", cutoff, target)

            self.assertEqual(retried["id"], "forked")
            self.assertEqual(db.get_events("forked"), before)

    def test_fork_rejects_invalid_or_divergent_target_without_mutation(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec("source"))
            cutoff = db.append_event(
                "source", "user_message", {"text": "one"})["seq"]
            source_before = db.get_session("source")
            events_before = db.get_events("source")

            for bad_seq in (True, 0, cutoff + 1):
                with self.subTest(fork_seq=bad_seq):
                    with self.assertRaises(state.StateError):
                        db.fork_session(
                            "source", bad_seq, session_rec("bad-target"))
            with self.assertRaisesRegex(state.StateError, "parent_session_id"):
                db.fork_session(
                    "source", cutoff,
                    session_rec("bad-parent", parent_session_id="elsewhere"))
            with self.assertRaisesRegex(state.StateError, "fork_seq"):
                db.fork_session(
                    "source", cutoff,
                    session_rec("bad-lineage", fork_seq=float(cutoff)))

            db.upsert_session(session_rec("occupied", title="Unrelated"))
            occupied_before = db.get_session("occupied")
            with self.assertRaisesRegex(state.StateError, "已存在"):
                db.fork_session(
                    "source", cutoff,
                    session_rec("occupied", title="Forked"))

            self.assertIsNone(db.get_session("bad-target"))
            self.assertIsNone(db.get_session("bad-parent"))
            self.assertIsNone(db.get_session("bad-lineage"))
            self.assertEqual(db.get_session("occupied"), occupied_before)
            self.assertEqual(db.get_session("source"), source_before)
            self.assertEqual(db.get_events("source"), events_before)

    def test_fork_rolls_back_target_when_final_audit_event_fails(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec("source"))
            cutoff = db.append_event(
                "source", "user_message", {"text": "one"})["seq"]
            db._conn.executescript(
                """
                CREATE TRIGGER reject_fork_audit
                BEFORE INSERT ON events
                WHEN NEW.session_id = 'fork-fail'
                 AND NEW.kind = 'branch_created'
                BEGIN
                    SELECT RAISE(ABORT, 'reject fork audit');
                END;
                """)

            with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "reject fork audit"):
                db.fork_session(
                    "source", cutoff, session_rec("fork-fail"))

            self.assertIsNone(db.get_session("fork-fail"))
            self.assertEqual(db.event_count("fork-fail"), 0)

    def test_source_key_is_idempotent(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec())
            one = db.append_event(
                "s1", "user_message", {"text": "a"}, source_key="same")
            two = db.append_event(
                "s1", "user_message", {"text": "different"},
                source_key="same")
            self.assertTrue(one["inserted"])
            self.assertFalse(two["inserted"])
            self.assertEqual(one["seq"], two["seq"])
            self.assertEqual(db.event_count("s1"), 1)

    def test_event_batch_rolls_back_as_one_transaction(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec())
            db._conn.executescript(
                """
                CREATE TRIGGER reject_explode
                BEFORE INSERT ON events
                WHEN NEW.kind = 'explode'
                BEGIN
                    SELECT RAISE(ABORT, 'boom');
                END;
                """)
            with self.assertRaises(sqlite3.IntegrityError):
                db.append_events("s1", [
                    {"kind": "user_message", "payload": {"text": "kept?"}},
                    {"kind": "explode", "payload": {}},
                ])
            self.assertEqual(db.event_count("s1"), 0)
            self.assertEqual(db.get_session("s1")["last_seq"], 0)

    def test_events_are_append_only_at_database_layer(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec())
            db.append_event("s1", "user_message", {"text": "immutable"})
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db._conn.execute(
                    "UPDATE events SET kind = 'changed' WHERE session_id = 's1'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db._conn.execute(
                    "DELETE FROM events WHERE session_id = 's1'")
            self.assertEqual(db.event_count("s1"), 1)

    def test_bootstrap_is_exact_and_runs_only_once(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "问题"},
            {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        ]
        with state.StateStore(self.db_path) as db:
            self.assertEqual(db.bootstrap_session(
                session_rec(), messages), len(messages))
            self.assertEqual(db.bootstrap_session(
                session_rec(updated="2026-08-24T00:00:02+00:00"),
                messages), 0)
            rows = db.get_events("s1")
            restored = [row["payload"]["message"] for row in rows]
            self.assertEqual(restored, messages)
            self.assertEqual(db.get_session("s1")["message_count"], 2)

    def test_session_list_reads_metadata_only(self):
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec())
            db.append_event("s1", "user_message", {"large": "x" * 1000})
            statements = []
            db._conn.set_trace_callback(statements.append)
            try:
                rows = db.list_sessions()
            finally:
                db._conn.set_trace_callback(None)
            self.assertEqual([row["id"] for row in rows], ["s1"])
            selects = [sql.lower() for sql in statements
                       if sql.lstrip().lower().startswith("select")]
            self.assertTrue(selects)
            self.assertTrue(all("events" not in sql for sql in selects))

    def test_agent_lifecycle_projects_child_session_and_agent_run(self):
        base = {
            "id": "a-child01",
            "parent_session_id": "s1",
            "parent_turn_id": "turn-1",
            "parent_tool_call_id": "call-1",
            "transcript_session_id": "c-child01",
            "name": "inspect auth", "kind": "subagent",
            "task": "inspect auth", "model": "child-model",
            "gateway": "boyue", "cwd": "/tmp/workspace/project",
            "created_at": "2026-08-25T00:00:00.000+00:00",
        }
        with state.StateStore(self.db_path) as db:
            db.upsert_session(session_rec())
            db.append_event("s1", "agent_spawned", {
                **base, "state": "queued",
                "updated_at": "2026-08-25T00:00:00.000+00:00",
            }, turn_id="turn-1")
            db.append_event("s1", "agent_state_changed", {
                **base, "state": "running",
                "started_at": "2026-08-25T00:00:01.000+00:00",
                "updated_at": "2026-08-25T00:00:01.000+00:00",
            }, turn_id="turn-1")
            db.append_event("s1", "agent_result_received", {
                **base, "state": "completed", "result": "bounded report",
                "result_truncated": False,
                "started_at": "2026-08-25T00:00:01.000+00:00",
                "updated_at": "2026-08-25T00:00:02.000+00:00",
                "ended_at": "2026-08-25T00:00:02.000+00:00",
            }, turn_id="turn-1")
            child = db.get_session("c-child01")
            run = db.get_agent_run("a-child01")
            listed = db.list_agent_runs("s1")
            resume_rows = db.list_sessions()
            all_rows = db.list_sessions(include_agent_transcripts=True)
            violations = db.foreign_key_violations()

        self.assertEqual(child["parent_session_id"], "s1")
        self.assertEqual(child["permission_mode"], "read_only")
        self.assertEqual(child["model"], "child-model")
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["result_preview"], "bounded report")
        self.assertEqual(run["parent_tool_call_id"], "call-1")
        self.assertEqual([item["id"] for item in listed], ["a-child01"])
        self.assertEqual([item["id"] for item in resume_rows], ["s1"])
        self.assertEqual(
            {item["id"] for item in all_rows}, {"s1", "c-child01"})
        self.assertEqual(violations, [])


    def test_usage_source_key_is_idempotent(self):
        usage = {
            "session": "s1", "turn": 1, "model": "m", "gateway": "g",
            "ts": "2026-08-24T00:00:00+00:00",
            "prompt_tokens": 10, "completion_tokens": 2,
        }
        with state.StateStore(self.db_path) as db:
            self.assertTrue(db.record_usage(usage, source_key="usage-1"))
            self.assertFalse(db.record_usage(usage, source_key="usage-1"))
            self.assertEqual(db.counts()["requests"], 1)


class ShadowFacadeTests(unittest.TestCase):
    def tearDown(self):
        store.reset_shadow_configuration()

    @staticmethod
    def agent(messages=None):
        return SimpleNamespace(
            session_id="shadow1", model="m", gateway="g",
            messages=messages or [{"role": "system", "content": "sys"}],
            tokens_in=0, tokens_out=0, last_total=0, started=0,
        )

    def test_default_is_off_and_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(store, "HOME", Path(tmp)):
            os.environ.pop("ZYLAB_STATE_SHADOW", None)
            os.environ.pop("ZYLAB_STATE_DB", None)
            store.reset_shadow_configuration()
            self.assertFalse(store.shadow_enabled())
            self.assertIsNone(store.shadow_ensure_agent(self.agent()))
            self.assertIsNone(store.controller_journal(self.agent()))
            self.assertFalse((Path(tmp) / store.STATE_DB_NAME).exists())

    def test_opt_in_controller_journal_bootstraps_on_main_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite3"
            store.configure_shadow(db_path, enabled=True)
            journal = store.controller_journal(self.agent())
            self.assertIsInstance(journal, state.StateStore)
            journal.append_events("shadow1", [{
                "kind": "turn_started", "payload": {},
                "turn_id": "turn-1",
            }])
            store.close_shadow()
            with state.StateStore(db_path) as reopened:
                self.assertEqual(
                    [event["kind"] for event in reopened.get_events("shadow1")],
                    ["system_message", "turn_started"])

    def test_shadow_failure_does_not_block_json_session_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            broken_target = Path(tmp) / "is-a-directory"
            broken_target.mkdir()
            os.chmod(broken_target, 0o711)
            paths = {
                "HOME": root,
                "SESSIONS": root / "sessions",
                "ARCHIVE": root / "archived_sessions",
                "PLANS": root / "plans",
                "BACKUPS": root / "backups",
                "LOGS": root / "logs",
                "CACHE": root / "cache",
                "PROJECTS": root / "projects",
                "INSTALL_ID": root / "installation_id",
                "USER_MD": root / "ZYLAB.md",
            }
            dirs = tuple(paths[name] for name in (
                "SESSIONS", "ARCHIVE", "PLANS", "BACKUPS",
                "LOGS", "CACHE", "PROJECTS"))
            store.configure_shadow(broken_target, enabled=True)
            ag = self.agent([
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "仍需保存"},
            ])
            with mock.patch.multiple(store, **paths, _DIRS=dirs):
                saved = store.save_session(ag)
                self.assertTrue(saved.is_file())
                payload = json.loads(saved.read_text(encoding="utf-8"))
                self.assertEqual(
                    stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertTrue(all(
                    stat.S_IMODE(path.stat().st_mode) == 0o700
                    for path in dirs))
                for private_file in (
                        saved, paths["INSTALL_ID"], paths["USER_MD"]):
                    self.assertEqual(
                        stat.S_IMODE(private_file.stat().st_mode), 0o600)
            self.assertEqual(payload["messages"], ag.messages)
            self.assertIsNotNone(store.shadow_status()["broken"])
            self.assertEqual(
                stat.S_IMODE(broken_target.stat().st_mode), 0o711)

    def test_agent_run_emits_ordered_runtime_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite3"
            store.configure_shadow(db_path, enabled=True)
            ag = agent_mod.Agent.__new__(agent_mod.Agent)
            ag.model, ag.gateway, ag.session_id = "m", "test", "run1"
            ag.messages = [{"role": "system", "content": "sys"}]
            ag.tokens_in = ag.tokens_out = ag.last_total = ag.turns = 0
            ag.cache_read = ag.cache_write = 0
            ag.cache_reported = False
            ag.compact_failed = None
            ag.ctx_limit, ag.compact_at, ag.ctx_known = 100_000, 70_000, True
            ag.confirm = lambda *args, **kwargs: True
            ag.started = 0
            ag._seen_ok_hi = 0

            def stream(*args, **kwargs):
                yield {"t": "text", "v": "完成"}
                yield {"t": "done", "reason": "stop", "usage": {}}

            with mock.patch.object(client, "stream_chat", stream), \
                 mock.patch.object(ag, "log_usage"):
                list(ag.run("开始"))
            store.close_shadow()

            with state.StateStore(db_path) as db:
                kinds = [row["kind"] for row in db.get_events("run1")]
                seqs = [row["seq"] for row in db.get_events("run1")]
            self.assertEqual(kinds, [
                "system_message", "user_message", "turn_started",
                "request_started", "assistant_message", "turn_completed",
            ])
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


class ShadowClosesOnExitTests(unittest.TestCase):
    """影子 writer 是模块级单例，进程退出时必须关掉。

    `close_shadow()` 一直都在，只是没人在退出路径上调 —— 于是解释器收尾时
    sqlite 连接被 GC，打出 `ResourceWarning: unclosed database`。

    **在 POSIX 上那只是噪音，Windows 上是真后果**：未关闭的句柄会把库文件占到
    进程退出，父进程/测试夹具删不掉那个目录。2026-09-22 公开仓库 CI 的 Windows
    那列，`TemporaryDirectory` 清理抛 `WinError 32` 一度把 40 条用例全染成
    ERROR，而尾部那句 `<sys>:0: ResourceWarning: unclosed database` 就是指纹。

    所以这条用例**用子进程 + `-W error::ResourceWarning` 复现原现象**：
    在真实的解释器收尾路径上跑，而不是在进程内断言一个 mock。
    """

    @staticmethod
    def _warns_about_unclosed_connections(tmp):
        """本解释器会不会为「没关的 sqlite 连接」发 ResourceWarning？**探，不问版本。**

        这条警告是解释器收尾期发出的（CI 日志里带 `<sys>:0:` 前缀），
        而且并非所有版本都有：实测本机 3.12 与 3.10 **一条都不发**，连裸
        `sqlite3.connect` 也不发；CI 的 3.13 那几列才有。

        所以直接断言「stderr 里没有 unclosed」在 3.12/3.10 上**不修也是绿的**
        —— 那是假保证。先探一次：探不到就明确 skip，探到了才当真断言。
        （AGENTS.md §7：问「我要的那个东西在不在」，不要问版本号。）
        """
        probe = subprocess.run(
            [sys.executable, "-W", "always::ResourceWarning", "-c",
             "import sqlite3; sqlite3.connect(%r)"
             % os.path.join(tmp, "probe.db")],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        return "unclosed" in probe.stderr

    def test_a_process_that_opened_the_shadow_exits_without_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            if not self._warns_about_unclosed_connections(tmp):
                self.skipTest(
                    "本解释器不为未关闭的 sqlite 连接发 ResourceWarning，"
                    "这条断言在这里无法反驳实现（机制本身由 "
                    "test_it_registers_once_not_per_call 钉住）")
            code = (
                "import os, sys;"
                "sys.path.insert(0, %r);"
                "from core import store;"
                "store.configure_shadow(%r, enabled=True);"
                "store._get_shadow();"
                "print('opened')"
            ) % (ROOT, os.path.join(tmp, "shadow.db"))
            done = subprocess.run(
                [sys.executable, "-W", "always::ResourceWarning", "-c", code],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120)
            self.assertIn("opened", done.stdout, done.stderr)
            self.assertEqual(done.returncode, 0, done.stderr[-1200:])
            self.assertNotIn("unclosed database", done.stderr)

    def test_it_registers_once_not_per_call(self):
        """懒注册：开过多次也只挂一个 handler，没开过的进程一个都不挂。"""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(store, "_SHADOW_ATEXIT", False), \
                mock.patch.object(store.atexit, "register") as register:
            store.configure_shadow(
                os.path.join(tmp, "shadow.db"), enabled=True)
            store._get_shadow()
            store._get_shadow()
            store.reset_shadow_configuration()
            self.assertEqual(register.call_count, 1)
            self.assertIs(register.call_args.args[0], store.close_shadow)


class FileCacheConcurrencyTests(unittest.TestCase):
    def test_concurrent_jsonl_appends_keep_usage_and_history_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage = root / "usage.jsonl"
            history = root / "history.jsonl"

            def append_batch(worker):
                for index in range(20):
                    store.append_usage({
                        "ts": "2026-08-25T02:00:00+00:00",
                        "session": f"s{worker}",
                        "model": "m1", "gateway": "g1",
                        "prompt_tokens": 1, "completion_tokens": 1,
                        "total_tokens": 2,
                        "record": f"{worker}:{index}",
                    })
                    store.append_history(
                        f"prompt-{worker}:{index}", cwd=f"/work/{worker}")

            with mock.patch.multiple(
                    store, HOME=root, USAGE_LOG=usage, HISTORY=history):
                threads = [threading.Thread(target=append_batch, args=(i,))
                           for i in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                records = store.read_usage()
                prompts = store.read_history(limit=1_000)

            self.assertEqual(len(records), 160)
            self.assertEqual(
                len({record["record"] for record in records}), 160)
            self.assertEqual(len(prompts), 160)
            self.assertEqual(
                len({record["input"] for record in prompts}), 160)

    def test_concurrent_stats_rebuilds_are_serialized_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage = root / "usage.jsonl"
            cache = root / "stats-cache.json"
            usage.write_text(json.dumps({
                "ts": "2026-08-25T02:00:00+00:00",
                "session": "s1", "model": "m1", "gateway": "g1",
                "prompt_tokens": 7, "completion_tokens": 3,
                "total_tokens": 10, "tools": [],
            }) + "\n", encoding="utf-8")
            active = 0
            peak = 0
            guard = threading.Lock()
            original = store._rebuild_stats_locked

            def observed_rebuild():
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.005)
                    return original()
                finally:
                    with guard:
                        active -= 1

            with mock.patch.multiple(
                    store, HOME=root, USAGE_LOG=usage, STATS_CACHE=cache), \
                 mock.patch.object(
                    store, "_rebuild_stats_locked", observed_rebuild):
                threads = [threading.Thread(target=store.rebuild_stats)
                           for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(peak, 1)
            self.assertEqual(payload["totalCalls"], 1)
            self.assertEqual(payload["totalTokens"], 10)
            assert_mode(self, cache, 0o600)
            self.assertEqual(
                list(root.glob(".stats-cache.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
