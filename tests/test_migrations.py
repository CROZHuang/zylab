"""Legacy JSON/JSONL -> SQLite migration 的 dry-run、幂等与切换安全测试。"""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock as _mock
from tests.platform_support import (  # noqa: E402
    HARD_KILL_SIGNAL, hard_kill_returncode)
from core import migrations, state


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.usage = self.root / "usage.jsonl"
        self.target = self.root / "new-home" / "state.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def write_session(self, name="s1.json", *, session_id="s1", messages=None,
                      title="会话"):
        if messages is None:
            messages = [
                {"role": "system", "content": "系统"},
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "完成"},
            ]
        rec = {
            "id": session_id,
            "title": title,
            "model": "kimi-test",
            "gateway": "test",
            "cwd": "/tmp/workspace/project",
            "created": "2026-08-24T00:00:00+00:00",
            "updated": "2026-08-24T00:00:01+00:00",
            "tokens_in": 12,
            "tokens_out": 4,
            "messages": messages,
        }
        path = self.sessions / name
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return path, rec

    def write_usage(self, rows):
        encoded = []
        for row in rows:
            encoded.append(
                row if isinstance(row, str)
                else json.dumps(row, ensure_ascii=False))
        self.usage.write_text("\n".join(encoded) + "\n", encoding="utf-8")
        return self.usage

    @staticmethod
    def digest(path):
        return state.sha256_bytes(Path(path).read_bytes())

    @staticmethod
    def usage_row(session_id="s1", turn=1):
        return {
            "ts": "2026-08-24T00:00:02+00:00",
            "session": session_id,
            "turn": turn,
            "model": "kimi-test",
            "gateway": "test",
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
            "cache_read": 0,
            "cache_write": 0,
            "cache_reported": False,
            "secs": 0.25,
            "cwd": "/tmp/workspace/project",
            "tools": [],
        }

    def test_dry_run_is_read_only_and_reports_exact_counts(self):
        first, _ = self.write_session("a.json", session_id="a")
        second, _ = self.write_session(
            "b.json", session_id="b",
            messages=[{"role": "user", "content": "第二个"}])
        self.write_usage([self.usage_row("a", 1), self.usage_row("b", 1)])
        before = {path: self.digest(path)
                  for path in (first, second, self.usage)}

        report = migrations.migrate_v1_to_new_db(
            self.sessions, self.usage, self.target, dry_run=True)

        self.assertTrue(report.ok)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.session_files_seen, 2)
        self.assertEqual(report.sessions_valid, 2)
        self.assertEqual(report.messages_valid, 4)
        self.assertEqual(report.usage_rows_seen, 2)
        self.assertEqual(report.usage_rows_valid, 2)
        self.assertFalse(self.target.exists())
        self.assertFalse(Path(str(self.target) + ".migrate.lock").exists())
        self.assertEqual(
            before,
            {path: self.digest(path) for path in (first, second, self.usage)})

    def test_write_migration_round_trips_messages_and_usage(self):
        messages = [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": [{"type": "text", "text": "复杂内容"}]},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "list_dir",
                                          "arguments": "{\"path\":\".\"}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a.py\nb.py"},
        ]
        source, _ = self.write_session(messages=messages)
        self.write_usage([self.usage_row()])
        before_session = self.digest(source)
        before_usage = self.digest(self.usage)

        report = migrations.migrate_v1_to_new_db(
            self.sessions, self.usage, self.target, dry_run=False)

        self.assertTrue(report.ok)
        self.assertTrue(report.target_created)
        self.assertEqual(report.target_path, str(self.target))
        self.assertEqual(report.sessions_imported, 1)
        self.assertEqual(report.messages_imported, len(messages))
        self.assertEqual(report.usage_rows_imported, 1)
        self.assertEqual(report.integrity, ["ok"])
        self.assertEqual(report.foreign_key_violations, [])
        self.assertEqual(report.journal_mode, "WAL")
        self.assertTrue(self.target.is_file())
        self.assertFalse(Path(str(self.target) + ".tmp").exists())
        self.assertFalse(Path(str(self.target) + ".migrate.lock").exists())
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.target.parent.stat().st_mode), 0o700)
        self.assertEqual(self.digest(source), before_session)
        self.assertEqual(self.digest(self.usage), before_usage)

        with state.StateStore(self.target) as db:
            restored = [
                event["payload"]["message"]
                for event in db.get_events("s1")
            ]
            self.assertEqual(restored, messages)
            self.assertEqual(
                db.counts(),
                {"sessions": 1, "events": len(messages), "requests": 1,
                 "legacy_imports": 2, "migration_errors": 0})
            self.assertEqual(db.integrity_check(), ["ok"])

    def test_import_into_same_database_is_idempotent(self):
        self.write_session()
        self.write_usage([self.usage_row()])
        direct = self.root / "direct.sqlite3"
        with state.StateStore(direct) as db:
            first = migrations.import_v1(db, self.sessions, self.usage)
            counts = db.counts()
            second = migrations.import_v1(db, self.sessions, self.usage)
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            self.assertEqual(second.sessions_skipped, 1)
            self.assertEqual(second.usage_rows_skipped, 1)
            self.assertEqual(second.sessions_imported, 0)
            self.assertEqual(second.usage_rows_imported, 0)
            self.assertEqual(db.counts(), counts)

    def test_changed_source_is_a_conflict_not_a_guess_merge(self):
        source, rec = self.write_session()
        direct = self.root / "direct.sqlite3"
        with state.StateStore(direct) as db:
            first = migrations.import_v1(db, self.sessions, self.usage)
            self.assertTrue(first.ok)
            before = db.counts()
            rec["messages"].append({"role": "user", "content": "后来改了"})
            source.write_text(
                json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            second = migrations.import_v1(db, self.sessions, self.usage)
            self.assertFalse(second.ok)
            self.assertTrue(any(
                "内容改变" in issue["error"] for issue in second.errors))
            self.assertEqual(db.counts(), before)

    def test_malformed_usage_keeps_target_unswitched_and_writes_diagnostics(self):
        source, _ = self.write_session()
        self.write_usage([self.usage_row(), "{broken json"])
        before = {source: self.digest(source), self.usage: self.digest(self.usage)}

        with self.assertRaises(migrations.MigrationFailed) as caught:
            migrations.migrate_v1_to_new_db(
                self.sessions, self.usage, self.target, dry_run=False)

        self.assertIsNotNone(caught.exception.report)
        self.assertFalse(self.target.exists())
        temp = Path(str(self.target) + ".tmp")
        errors = Path(str(temp) + ".errors.json")
        self.assertTrue(temp.is_file())
        self.assertTrue(errors.is_file())
        self.assertFalse(Path(str(self.target) + ".migrate.lock").exists())
        diagnosis = json.loads(errors.read_text(encoding="utf-8"))
        self.assertFalse(diagnosis["ok"])
        self.assertEqual(diagnosis["usage_rows_seen"], 2)
        self.assertEqual(diagnosis["usage_rows_valid"], 1)
        self.assertEqual(
            before,
            {source: self.digest(source), self.usage: self.digest(self.usage)})
        with state.StateStore(temp) as db:
            self.assertEqual(db.counts()["migration_errors"], 1)
            self.assertEqual(db.integrity_check(), ["ok"])

    def test_existing_target_is_never_overwritten(self):
        self.write_session()
        self.target.parent.mkdir()
        self.target.write_bytes(b"sentinel")
        with self.assertRaisesRegex(migrations.MigrationFailed, "已存在"):
            migrations.migrate_v1_to_new_db(
                self.sessions, self.usage, self.target, dry_run=False)
        self.assertEqual(self.target.read_bytes(), b"sentinel")
        self.assertFalse(Path(str(self.target) + ".tmp").exists())

    def test_existing_lock_blocks_migration_without_creating_database(self):
        self.write_session()
        self.target.parent.mkdir()
        lock = Path(str(self.target) + ".migrate.lock")
        lock.write_text("owner=someone-else\n", encoding="utf-8")
        with self.assertRaisesRegex(migrations.MigrationFailed, "lock"):
            migrations.migrate_v1_to_new_db(
                self.sessions, self.usage, self.target, dry_run=False)
        self.assertFalse(self.target.exists())
        self.assertFalse(Path(str(self.target) + ".tmp").exists())
        self.assertEqual(lock.read_text(encoding="utf-8"),
                         "owner=someone-else\n")

    def test_atomic_link_refuses_a_racing_target(self):
        self.write_session()
        with mock.patch.object(
                migrations.os, "link", side_effect=FileExistsError):
            with self.assertRaisesRegex(
                    migrations.MigrationFailed, "迁移期间出现"):
                migrations.migrate_v1_to_new_db(
                    self.sessions, self.usage, self.target, dry_run=False)
        self.assertFalse(self.target.exists())
        self.assertTrue(Path(str(self.target) + ".tmp").is_file())
        self.assertFalse(Path(str(self.target) + ".migrate.lock").exists())

    def test_sigkill_before_switch_leaves_valid_temp_and_no_target(self):
        self.write_session()
        repo = Path(__file__).resolve().parents[1]
        # 硬杀用数字信号号，不写 signal.SIGKILL —— Windows 的 signal 模块
        # 没有它；os.kill(pid, 9) 在那边走 TerminateProcess(handle, 9)，
        # 「进程没跑完就消失」这个契约两边一致，只是退出码编码不同
        # （见 tests/platform_support.hard_kill_returncode）。
        code = f"""
import os
from core import migrations

def kill_after_import(stage, temp, target, report):
    if stage == "after_import":
        os.kill(os.getpid(), {HARD_KILL_SIGNAL})

migrations.migrate_v1_to_new_db(
    {str(self.sessions)!r}, {str(self.usage)!r}, {str(self.target)!r},
    dry_run=False, _stage_hook=kill_after_import)
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo, capture_output=True, encoding="utf-8", errors="replace", text=True, timeout=20)
        self.assertEqual(proc.returncode, hard_kill_returncode(),
                         proc.stderr)
        self.assertFalse(self.target.exists())
        temp = Path(str(self.target) + ".tmp")
        lock = Path(str(self.target) + ".migrate.lock")
        self.assertTrue(temp.is_file())
        self.assertTrue(lock.is_file())
        connection = sqlite3.connect(
            "file:" + str(temp) + "?mode=ro", uri=True)
        try:
            integrity = connection.execute(
                "PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
        self.assertEqual(integrity, [("ok",)])


    def test_protected_target_is_rejected_before_any_write(self):
        """这条以前是**假绿**，而且真的往文件系统根上写过东西。

        setUpModule 打的是 `tools.PROTECTED`（词法守卫），而
        `migrations._reject_protected_target` 查的是 `paths.protected_paths()`
        ——两者没有关系，守卫从来没被触发过。于是代码一路往下，在维护者的
        root 环境里**真的建出了 `/protected/archive/zylab-forbidden/state.sqlite3`**
        （2026-09-16，196 KB 的 SQLite 库，2026-09-21 才发现并清掉）。
        此后每次再跑，拦住它的是「target 已存在，拒绝覆盖」——那条消息里也带着
        `/protected/archive`，正好满足旧断言。CI 的 runner 是非 root、又没有遗留
        文件，当场 ERROR。

        现在按 migrations 真正读的那个来源声明，并且断言**具体是哪条守卫**
        以及**什么都没被创建**。
        """
        target = Path(self.root) / "forbidden-archive" / "state.sqlite3"
        with _mock.patch.object(
                migrations.paths, "protected_paths",
                return_value=[str(target.parent)]):
            with self.assertRaisesRegex(
                    migrations.MigrationFailed, "受保护路径"):
                migrations.migrate_v1_to_new_db(
                    self.sessions, self.usage, target, dry_run=False)
        self.assertFalse(target.parent.exists(),
                         "名字里写着 before any write，就一个目录都不该留下")



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
