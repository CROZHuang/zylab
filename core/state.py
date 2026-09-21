"""Zylab v2 的本地状态库。

这一层只负责可验证的持久化语义：

- SQLite 中的 session/event/request 等权威记录；
- event 在 session 内严格递增且 append-only；
- legacy import 与 runtime shadow-write 的幂等键；
- 数据库完整性和外键检查。

当前 JSON session 仍是启动源。是否启用 shadow-write 由 core.store 决定，StateStore
本身不读取用户配置，也不碰 ~/.zylab 之外的文件。
"""
from __future__ import annotations
from . import paths

import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import model_health as health_policy
from . import wincompat


SCHEMA_VERSION = 1
MESSAGE_EVENTS = {"user_message", "assistant_message", "tool_result",
                  "tool_finished", "message_imported"}
JOURNAL_MODES = {"WAL", "DELETE"}
QUEUE_MODES = {"steer", "next_turn", "command", "shell"}
QUEUE_STATES = {"queued", "dispatched", "cancelled"}
AGENT_STATES = {
    "queued", "running", "waiting", "needs_input", "idle",
    "completed", "failed", "cancelled",
}
AGENT_EVENTS = {
    "agent_spawned", "agent_state_changed", "agent_result_received",
}
FORK_EXCLUDED_EVENT_PREFIXES = (
    "queued_input_", "task_", "agent_",
)
GRANT_DECISIONS = {"allow", "ask", "deny"}
REQUEST_STATUSES = {"running", "ok", "failed", "interrupted"}
REQUEST_PHASE_FIELDS = (
    "queued_at", "started_at", "headers_at", "first_delta_at",
    "last_delta_at", "ended_at",
)
TOOL_RUN_STATUSES = {
    "running", "completed", "denied", "failed", "cancelled", "unknown",
}
PROCESS_OWNER_ID = uuid.uuid4().hex
PROCESS_OWNER_HOST = socket.gethostname()


class StateError(RuntimeError):
    """StateStore 的可解释失败。"""


class SchemaMismatch(StateError):
    """数据库版本与当前代码不兼容。"""


class ImportConflict(StateError):
    """同一 legacy source 在已导入后发生了不可安全合并的变化。"""


def _is_sqlite_busy(exc):
    """Return whether an OperationalError is SQLite BUSY/LOCKED."""
    code = getattr(exc, "sqlite_errorcode", None)
    try:
        if int(code) & 0xFF in {
                getattr(sqlite3, "SQLITE_BUSY", 5),
                getattr(sqlite3, "SQLITE_LOCKED", 6)}:
            return True
    except (TypeError, ValueError):
        pass
    message = str(exc).lower()
    return "busy" in message or "locked" in message


def _journal_mode_value(row):
    return str(row[0]).upper() if row else ""


def _set_journal_mode_with_retry(connection, mode, timeout, *,
                                 clock=None, sleeper=None):
    """Set a database-wide journal mode without racing another first opener.

    ``PRAGMA journal_mode = ...`` can fail immediately with BUSY/LOCKED even
    when ``busy_timeout`` is configured.  Another opener only needs to win
    once, so after each failed conversion we read the current mode and accept
    the winner's result.  Persistent contention fails closed: it must not be
    mistaken for a filesystem that genuinely declined WAL.
    """
    target = str(mode).upper()
    if target not in JOURNAL_MODES:
        raise ValueError(f"不支持的 journal mode: {mode}")
    wait_clock = clock or time.monotonic
    wait_sleep = sleeper or time.sleep
    timeout = max(0.0, float(timeout))
    deadline = wait_clock() + timeout
    delay = 0.005
    last_error = None
    observed = ""

    while True:
        try:
            row = connection.execute(
                f"PRAGMA journal_mode = {target}").fetchone()
            return _journal_mode_value(row)
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy(exc):
                raise
            last_error = exc

        try:
            observed = _journal_mode_value(
                connection.execute("PRAGMA journal_mode").fetchone())
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy(exc):
                raise
            last_error = exc
            observed = ""
        if observed == target:
            return observed

        now = wait_clock()
        if now >= deadline:
            detail = observed or "unknown"
            raise StateError(
                f"journal_mode={target} 初始化在 {timeout:g}s 内持续 "
                f"locked/busy；当前 mode={detail}") from last_error
        pause = min(delay, max(0.0, deadline - now))
        wait_sleep(pause)
        delay = min(delay * 2, 0.05)


def _configure_journal_mode(connection, mode, timeout, *,
                            clock=None, sleeper=None):
    """Apply the requested mode, retaining the proven non-WAL fallback."""
    target = str(mode).upper()
    actual = _set_journal_mode_with_retry(
        connection, target, timeout, clock=clock, sleeper=sleeper)
    if target == "WAL" and actual != "WAL":
        # A successful PRAGMA that returns a non-WAL mode means the backing
        # filesystem declined WAL.  This is distinct from BUSY/LOCKED, which
        # the helper above retries and eventually reports instead of silently
        # changing the database-wide mode under concurrent openers.
        actual = _set_journal_mode_with_retry(
            connection, "DELETE", timeout, clock=clock, sleeper=sleeper)
    return actual


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _process_start_ticks(pid=None):
    """Return a process start token, which disambiguates PID reuse."""
    target = int(pid if pid is not None else os.getpid())
    if wincompat.IS_WINDOWS:
        # Windows 没有 /proc；等价物是 GetProcessTimes 的创建时间。
        return wincompat.proc_start(target) or None
    try:
        text = Path(f"/proc/{target}/stat").read_text(encoding="utf-8")
        tail = text[text.rfind(")") + 2:].split()
        return str(tail[19])
    except (OSError, IndexError, TypeError, ValueError):
        return None


def process_owner():
    return {
        "owner_id": PROCESS_OWNER_ID,
        "owner_pid": os.getpid(),
        "owner_host": PROCESS_OWNER_HOST,
        "owner_start_ticks": _process_start_ticks(),
    }


def _local_owner_alive(row):
    if str(row.get("owner_host") or "") != PROCESS_OWNER_HOST:
        return None
    try:
        pid = int(row.get("owner_pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if wincompat.IS_WINDOWS:
        # 存活走 OpenProcess，身份走创建时间。Windows 没有僵尸态
        # （最后一个句柄关掉进程对象就消失），所以没有 "Z" 对应分支。
        if not wincompat.pid_alive(pid):
            return False
        observed = wincompat.proc_start(pid)
        expected = row.get("owner_start_ticks")
        return expected in (None, "") or str(expected) == observed
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = text[text.rfind(")") + 2:].split()
        process_state = tail[0]
        observed = str(tail[19])
    except (OSError, IndexError, TypeError, ValueError):
        return False
    if process_state == "Z":
        return False
    expected = row.get("owner_start_ticks")
    return expected in (None, "") or str(expected) == observed


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def message_event_kind(message):
    role = message.get("role") if isinstance(message, dict) else None
    return {
        "system": "system_message",
        "user": "user_message",
        "assistant": "assistant_message",
        "tool": "tool_result",
    }.get(role, "message_imported")


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_timestamp(value=None):
    """Normalize an epoch/datetime/ISO value for durable UTC storage."""
    if value is None:
        return utcnow()
    if isinstance(value, str):
        # Validate and canonicalize instead of persisting local/naive time.
        return health_policy.format_utc(health_policy.parse_utc(value))
    return health_policy.format_utc(value)


def _elapsed_ms(start, end):
    if not start or not end:
        return None
    try:
        elapsed = (
            health_policy.parse_utc(end) - health_policy.parse_utc(start)
        ) * 1000.0
    except (TypeError, ValueError):
        return None
    return max(0.0, elapsed)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    version         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    migrated_from   TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'archived')),
    cwd                 TEXT NOT NULL DEFAULT '',
    repo_root           TEXT,
    git_branch          TEXT,
    model               TEXT NOT NULL DEFAULT '',
    gateway             TEXT NOT NULL DEFAULT '',
    permission_mode     TEXT NOT NULL DEFAULT 'default',
    parent_session_id   TEXT REFERENCES sessions(id),
    fork_seq            INTEGER,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_seq            INTEGER NOT NULL DEFAULT 0,
    message_count       INTEGER NOT NULL DEFAULT 0,
    last_context_tokens INTEGER,
    tokens_in           INTEGER NOT NULL DEFAULT 0,
    tokens_out          INTEGER NOT NULL DEFAULT 0,
    schema_version      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS sessions_status_updated
ON sessions(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS sessions_repo_branch_updated
ON sessions(repo_root, git_branch, updated_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    turn_id         TEXT,
    kind            TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    payload_version INTEGER NOT NULL DEFAULT 1,
    source_key      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS events_session_seq
ON events(session_id, seq);

CREATE INDEX IF NOT EXISTS events_turn
ON events(turn_id);

CREATE UNIQUE INDEX IF NOT EXISTS events_session_source
ON events(session_id, source_key)
WHERE source_key IS NOT NULL;


CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    session_id      TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    event_id        INTEGER REFERENCES events(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    relative_path   TEXT NOT NULL,
    byte_count      INTEGER NOT NULL DEFAULT 0,
    line_count      INTEGER NOT NULL DEFAULT 0,
    head_preview    TEXT,
    tail_preview    TEXT,
    sha256          TEXT NOT NULL,
    truncated       INTEGER NOT NULL DEFAULT 0,
    missing         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    covered_from_seq    INTEGER NOT NULL,
    covered_to_seq      INTEGER NOT NULL,
    summary_json        TEXT,
    artifact_id         TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    model               TEXT,
    gateway             TEXT,
    source_prompt_hash  TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queued_inputs (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    mode            TEXT NOT NULL
                    CHECK (mode IN ('steer', 'next_turn', 'command', 'shell')),
    text            TEXT NOT NULL,
    state           TEXT NOT NULL
                    CHECK (state IN ('queued', 'dispatched', 'cancelled')),
    enqueued_at     TEXT NOT NULL,
    dispatched_at   TEXT,
    event_id        INTEGER REFERENCES events(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS queued_inputs_session_state
ON queued_inputs(session_id, state, enqueued_at);

CREATE TABLE IF NOT EXISTS requests (
    id                      TEXT PRIMARY KEY,
    legacy_key              TEXT UNIQUE,
    session_id              TEXT,
    turn_id                 TEXT,
    attempt                 INTEGER NOT NULL DEFAULT 1,
    model                   TEXT NOT NULL DEFAULT '',
    gateway                 TEXT NOT NULL DEFAULT '',
    purpose                 TEXT NOT NULL DEFAULT 'chat',
    status                  TEXT NOT NULL,
    queued_at               TEXT,
    started_at              TEXT,
    headers_at              TEXT,
    first_delta_at          TEXT,
    last_delta_at           TEXT,
    ended_at                TEXT,
    http_status             INTEGER,
    error_kind              TEXT,
    error_text              TEXT,
    retry_reason            TEXT,
    retryable               INTEGER,
    prompt_tokens           INTEGER,
    completion_tokens       INTEGER,
    cache_read              INTEGER,
    cache_write             INTEGER,
    usage_reported          INTEGER NOT NULL DEFAULT 0,
    context_tokens_before   INTEGER,
    context_limit           INTEGER,
    summary_id              TEXT REFERENCES summaries(id) ON DELETE SET NULL,
    total_ms                REAL,
    true_ttft_ms            REAL,
    total_latency_ms        REAL,
    error_code              TEXT,
    provider_request_id     TEXT,
    owner_id                TEXT,
    owner_pid               INTEGER,
    owner_host              TEXT,
    owner_start_ticks       TEXT,
    heartbeat_at            TEXT,
    cwd                     TEXT,
    raw_json                TEXT
);

CREATE INDEX IF NOT EXISTS requests_session_ended
ON requests(session_id, ended_at);

CREATE INDEX IF NOT EXISTS requests_model_gateway_ended
ON requests(model, gateway, ended_at);

CREATE TABLE IF NOT EXISTS tool_runs (
    id                  TEXT PRIMARY KEY,
    tool_call_id        TEXT,
    session_id          TEXT NOT NULL,
    turn_id             TEXT,
    request_id          TEXT REFERENCES requests(id) ON DELETE SET NULL,
    name                TEXT NOT NULL,
    args_redacted_json  TEXT,
    approval_decision   TEXT,
    approval_source     TEXT,
    started_at          TEXT,
    ended_at            TEXT,
    status              TEXT NOT NULL,
    exit_code           INTEGER,
    signal              INTEGER,
    stdout_artifact     TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    stderr_artifact     TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    stdout_path         TEXT,
    stderr_path         TEXT,
    owner_id            TEXT,
    owner_pid           INTEGER,
    owner_host          TEXT,
    owner_start_ticks   TEXT,
    heartbeat_at        TEXT
);

CREATE INDEX IF NOT EXISTS tool_runs_session_started
ON tool_runs(session_id, started_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    id                      TEXT PRIMARY KEY,
    parent_session_id       TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
    parent_turn_id          TEXT,
    parent_tool_call_id     TEXT,
    transcript_session_id   TEXT NOT NULL UNIQUE REFERENCES sessions(id)
                            ON DELETE CASCADE,
    name                    TEXT NOT NULL DEFAULT '',
    kind                    TEXT NOT NULL DEFAULT 'subagent',
    task                    TEXT NOT NULL DEFAULT '',
    state                   TEXT NOT NULL
                            CHECK (state IN (
                                'queued', 'running', 'waiting',
                                'needs_input', 'idle', 'completed',
                                'failed', 'cancelled')),
    model                   TEXT NOT NULL DEFAULT '',
    gateway                 TEXT NOT NULL DEFAULT '',
    created_at              TEXT NOT NULL,
    started_at              TEXT,
    updated_at              TEXT NOT NULL,
    ended_at                TEXT,
    result_preview          TEXT,
    result_truncated        INTEGER NOT NULL DEFAULT 0,
    error                   TEXT
);

CREATE INDEX IF NOT EXISTS agent_runs_parent_updated
ON agent_runs(parent_session_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS model_health (
    gateway                 TEXT NOT NULL,
    model                   TEXT NOT NULL,
    capability_json         TEXT,
    capability_source       TEXT,
    last_success            TEXT,
    last_failure            TEXT,
    last_probe              TEXT,
    true_ttft_ms            REAL,
    total_latency_ms        REAL,
    success_count           INTEGER NOT NULL DEFAULT 0,
    failure_count           INTEGER NOT NULL DEFAULT 0,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    ewma_latency_ms         REAL,
    circuit_open_until      TEXT,
    last_error_kind         TEXT,
    expires_at              TEXT,
    last_interrupted        TEXT,
    probe_status            TEXT,
    probe_transient_failures INTEGER NOT NULL DEFAULT 0,
    probe_catalog_fingerprint TEXT,
    last_outcome_at         TEXT,
    last_route_recovery_at  TEXT,
    last_context_probe      TEXT,
    context_probe_status    TEXT,
    PRIMARY KEY(gateway, model)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    prompt_seq          INTEGER NOT NULL,
    manifest_artifact   TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    status              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoint_files (
    checkpoint_id          TEXT NOT NULL REFERENCES checkpoints(id)
                           ON DELETE CASCADE,
    path                   TEXT NOT NULL,
    existed_before         INTEGER NOT NULL,
    before_sha256          TEXT,
    snapshot_artifact      TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    expected_after_sha256  TEXT,
    mode_before            INTEGER,
    source_tool_run_id     TEXT REFERENCES tool_runs(id) ON DELETE SET NULL,
    PRIMARY KEY(checkpoint_id, path)
);

CREATE TABLE IF NOT EXISTS session_grants (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    matcher         TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('allow', 'ask', 'deny')),
    source          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(session_id, matcher, decision, source)
);

CREATE TABLE IF NOT EXISTS session_leases (
    session_id      TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    owner_id        TEXT NOT NULL,
    pid             INTEGER NOT NULL,
    hostname        TEXT NOT NULL,
    acquired_at     TEXT NOT NULL,
    heartbeat_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_imports (
    id              INTEGER PRIMARY KEY,
    source_path     TEXT NOT NULL,
    source_sha256   TEXT NOT NULL,
    source_kind     TEXT NOT NULL CHECK (source_kind IN ('session', 'usage')),
    source_size     INTEGER NOT NULL,
    rows_seen       INTEGER NOT NULL,
    rows_imported   INTEGER NOT NULL,
    error_count     INTEGER NOT NULL,
    imported_at     TEXT NOT NULL,
    UNIQUE(source_path, source_sha256)
);

CREATE INDEX IF NOT EXISTS legacy_imports_path_kind
ON legacy_imports(source_path, source_kind, imported_at);

CREATE TABLE IF NOT EXISTS migration_errors (
    id              INTEGER PRIMARY KEY,
    source_path     TEXT NOT NULL,
    source_sha256   TEXT,
    line_number     INTEGER,
    error           TEXT NOT NULL,
    raw_excerpt     TEXT,
    created_at      TEXT NOT NULL
);
"""


class StateStore:
    """一个进程内的 SQLite state writer。

    设计约束是 controller 单写者。这个类仍用 BEGIN IMMEDIATE 防止误并发时两个
    writer 同时给同一 session 分配相同 seq。
    """

    def __init__(self, path, *, journal_mode="WAL", timeout=5.0,
                 initialize_schema=True):
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        resolved = self.path.resolve(strict=False)
        if paths.under_protected(resolved):
            raise StateError(f"state database 不能位于受保护路径：{resolved}")
        mode = str(journal_mode).upper()
        if mode not in JOURNAL_MODES:
            raise ValueError(f"不支持的 journal mode: {journal_mode}")
        created_parent = not self.path.parent.exists()
        if initialize_schema:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if created_parent:
                os.chmod(self.path.parent, 0o700)
        elif not self.path.parent.is_dir():
            raise StateError(
                f"state database parent 不存在：{self.path.parent}")
        if self.path.is_symlink():
            raise StateError(f"state database 不能是符号链接：{self.path}")
        if self.path.exists() and not self.path.is_file():
            raise StateError(f"state database 必须是普通文件：{self.path}")
        if not self.path.exists() and not initialize_schema:
            raise StateError(f"state database 尚未初始化：{self.path}")
        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(self.path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._conn = sqlite3.connect(
            str(self.path), timeout=self.timeout, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self.closed = False
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
            self._check_schema_compatibility(
                require_schema=not initialize_schema)
            if initialize_schema:
                actual = _configure_journal_mode(
                    self._conn, mode, self.timeout)
            else:
                row = self._conn.execute("PRAGMA journal_mode").fetchone()
                actual = _journal_mode_value(row) or mode
            self.journal_mode = actual
            self._conn.execute("PRAGMA synchronous = NORMAL")
            if initialize_schema:
                self._create_schema()
            # Avoid a PVC chmod syscall on every metric query while still
            # repairing permissions if an external actor loosened them.
            if self.path.stat().st_mode & 0o077:
                os.chmod(self.path, 0o600)
            stat_result = self.path.stat()
            self.file_identity = (
                int(stat_result.st_dev), int(stat_result.st_ino))
        except Exception:
            self._conn.close()
            self.closed = True
            raise

    def _check_schema_compatibility(self, *, require_schema=False):
        """在任何持久化 PRAGMA/DDL 之前拒绝未来或外来数据库。"""
        tables = {
            str(row[0]) for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'").fetchall()
        }
        if not tables and require_schema:
            raise SchemaMismatch("现有数据库没有 state schema，拒绝使用")
        if not tables:
            return
        if "schema_meta" not in tables:
            raise SchemaMismatch("现有数据库缺少 schema_meta，拒绝修改")
        try:
            deadline = time.monotonic() + self.timeout
            row = None
            while row is None:
                row = self._conn.execute(
                    "SELECT version FROM schema_meta"
                    " WHERE singleton = 1").fetchone()
                if row is not None or time.monotonic() >= deadline:
                    break
                # A concurrent first opener creates schema_meta before its
                # singleton row.  Wait only for that narrow initialization
                # window; a genuinely incomplete DB still fails closed.
                time.sleep(0.01)
            version = int(row["version"]) if row else 0
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise SchemaMismatch(f"无法读取 state schema version：{exc}") from None
        if version != SCHEMA_VERSION:
            raise SchemaMismatch(
                f"state schema={version}，代码只支持 {SCHEMA_VERSION}")

    def _create_schema(self):
        self._conn.executescript(SCHEMA)
        self._ensure_additive_columns()
        now = utcnow()
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta"
            " (singleton, version, created_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, now))
        row = self._conn.execute(
            "SELECT version FROM schema_meta WHERE singleton = 1").fetchone()
        version = int(row["version"]) if row else 0
        if version != SCHEMA_VERSION:
            raise SchemaMismatch(
                f"state schema={version}，代码只支持 {SCHEMA_VERSION}")

    def _ensure_additive_columns(self):
        """Extend schema-v1 databases without pretending this is v2.

        Schema v1 has intentionally grown by additive tables/columns during the
        shadow pilot.  Every statement is idempotent and runs before runtime
        writes, matching the existing additive ``agent_runs`` behaviour.
        """
        wanted = {
            "requests": {
                "true_ttft_ms": "REAL",
                "total_latency_ms": "REAL",
                "error_code": "TEXT",
                "provider_request_id": "TEXT",
                "retryable": "INTEGER",
                "purpose": "TEXT NOT NULL DEFAULT 'chat'",
                "owner_id": "TEXT",
                "owner_pid": "INTEGER",
                "owner_host": "TEXT",
                "owner_start_ticks": "TEXT",
                "heartbeat_at": "TEXT",
            },
            "model_health": {
                "last_interrupted": "TEXT",
                "probe_status": "TEXT",
                "probe_transient_failures":
                    "INTEGER NOT NULL DEFAULT 0",
                "probe_catalog_fingerprint": "TEXT",
                "last_outcome_at": "TEXT",
                "last_route_recovery_at": "TEXT",
                "last_context_probe": "TEXT",
                "context_probe_status": "TEXT",
            },
            "tool_runs": {
                "stdout_path": "TEXT",
                "stderr_path": "TEXT",
                "owner_id": "TEXT",
                "owner_pid": "INTEGER",
                "owner_host": "TEXT",
                "owner_start_ticks": "TEXT",
                "heartbeat_at": "TEXT",
            },
        }
        for table, columns in wanted.items():
            present = {
                str(row[1]) for row in self._conn.execute(
                    f"PRAGMA table_info({table})").fetchall()
            }
            for name, declaration in columns.items():
                if name not in present:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN"
                            f" {name} {declaration}")
                    except sqlite3.OperationalError as exc:
                        # Two fresh terminals may observe the same old schema
                        # before either ALTER commits.  Accept only a proven
                        # concurrent addition; every other DDL error is real.
                        current = {
                            str(row[1]) for row in self._conn.execute(
                                f"PRAGMA table_info({table})").fetchall()
                        }
                        if name not in current:
                            raise

    def close(self):
        if not self.closed:
            self._conn.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self):
        if self.closed:
            raise StateError("StateStore 已关闭")
        if self._conn.in_transaction:
            raise StateError("不支持嵌套 transaction")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _upsert_session_tx(self, con, rec):
        sid = str(rec.get("id") or "").strip()
        if not sid:
            raise StateError("session 缺少 id")
        now = utcnow()
        created = rec.get("created") or rec.get("created_at") or now
        updated = rec.get("updated") or rec.get("updated_at") or now
        con.execute(
            """
            INSERT INTO sessions (
                id, title, status, cwd, repo_root, git_branch, model, gateway,
                permission_mode, parent_session_id, fork_seq, created_at,
                updated_at, last_context_tokens, tokens_in, tokens_out,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = CASE WHEN excluded.title <> ''
                             THEN excluded.title ELSE sessions.title END,
                status = excluded.status,
                cwd = CASE WHEN excluded.cwd <> ''
                           THEN excluded.cwd ELSE sessions.cwd END,
                repo_root = COALESCE(excluded.repo_root, sessions.repo_root),
                git_branch = COALESCE(excluded.git_branch, sessions.git_branch),
                model = CASE WHEN excluded.model <> ''
                             THEN excluded.model ELSE sessions.model END,
                gateway = CASE WHEN excluded.gateway <> ''
                               THEN excluded.gateway ELSE sessions.gateway END,
                permission_mode = excluded.permission_mode,
                parent_session_id = COALESCE(
                    excluded.parent_session_id, sessions.parent_session_id),
                fork_seq = COALESCE(excluded.fork_seq, sessions.fork_seq),
                updated_at = CASE WHEN excluded.updated_at > sessions.updated_at
                                  THEN excluded.updated_at
                                  ELSE sessions.updated_at END,
                last_context_tokens = COALESCE(
                    excluded.last_context_tokens, sessions.last_context_tokens),
                tokens_in = MAX(sessions.tokens_in, excluded.tokens_in),
                tokens_out = MAX(sessions.tokens_out, excluded.tokens_out),
                schema_version = excluded.schema_version
            """,
            (
                sid,
                str(rec.get("title") or ""),
                rec.get("status") or "active",
                str(rec.get("cwd") or ""),
                rec.get("repo_root"),
                rec.get("git_branch"),
                str(rec.get("model") or ""),
                str(rec.get("gateway") or ""),
                rec.get("permission_mode") or "default",
                rec.get("parent_session_id"),
                rec.get("fork_seq"),
                str(created),
                str(updated),
                rec.get("last_context_tokens"),
                _as_int(rec.get("tokens_in")),
                _as_int(rec.get("tokens_out")),
                SCHEMA_VERSION,
            ))
        return sid

    def upsert_session(self, rec):
        with self.transaction() as con:
            sid = self._upsert_session_tx(con, rec)
        return self.get_session(sid)

    def get_session(self, session_id):
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _normalize_session_grant(grant):
        if not isinstance(grant, dict):
            raise StateError("session grant 必须是 dict")
        matcher = str(grant.get("matcher") or "").strip()
        raw_decision = grant["decision"] if "decision" in grant else "allow"
        raw_source = grant["source"] if "source" in grant else "user"
        decision = "" if raw_decision is None else str(raw_decision).strip()
        source = "" if raw_source is None else str(raw_source).strip()
        if not matcher:
            raise StateError("session grant 缺少 matcher")
        if decision not in GRANT_DECISIONS:
            raise StateError(f"session grant decision 无效: {decision}")
        if not source:
            raise StateError("session grant 缺少 source")
        return {
            "matcher": matcher,
            "decision": decision,
            "source": source,
        }

    @staticmethod
    def _require_session_tx(con, session_id):
        session_id = str(session_id or "").strip()
        if not session_id:
            raise StateError("session id 不能为空")
        row = con.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise StateError(f"session 不存在: {session_id}")
        return row

    def list_session_grants(self, session_id):
        """Return stable, metadata-only grants for one existing session."""
        self._require_session_tx(self._conn, session_id)
        rows = self._conn.execute(
            """
            SELECT id, session_id, matcher, decision, source, created_at
              FROM session_grants
             WHERE session_id = ?
             ORDER BY matcher, source, decision, id
            """,
            (str(session_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_session_grant(self, session_id, matcher, decision="allow", *,
                          source="user", created_at=None):
        """Create or replace one matcher/source decision transactionally."""
        grant = self._normalize_session_grant({
            "matcher": matcher,
            "decision": decision,
            "source": source,
        })
        ts = str(created_at or utcnow())
        with self.transaction() as con:
            self._require_session_tx(con, session_id)
            current = con.execute(
                """
                SELECT * FROM session_grants
                 WHERE session_id = ? AND matcher = ? AND source = ?
                 ORDER BY id
                """,
                (str(session_id), grant["matcher"], grant["source"]),
            ).fetchall()
            if (len(current) == 1
                    and current[0]["decision"] == grant["decision"]):
                return dict(current[0])
            con.execute(
                "DELETE FROM session_grants"
                " WHERE session_id = ? AND matcher = ? AND source = ?",
                (str(session_id), grant["matcher"], grant["source"]),
            )
            con.execute(
                """
                INSERT INTO session_grants (
                    session_id, matcher, decision, source, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(session_id), grant["matcher"], grant["decision"],
                 grant["source"], ts),
            )
        return next(
            row for row in self.list_session_grants(session_id)
            if (row["matcher"], row["source"])
            == (grant["matcher"], grant["source"])
        )

    def delete_session_grant(self, session_id, matcher, *, source=None):
        """Delete one matcher, optionally narrowed to its decision source."""
        matcher = str(matcher or "").strip()
        if not matcher:
            raise StateError("session grant 缺少 matcher")
        if source is not None:
            source = str(source or "").strip()
            if not source:
                raise StateError("session grant source 不能为空")
        with self.transaction() as con:
            self._require_session_tx(con, session_id)
            sql = ("DELETE FROM session_grants"
                   " WHERE session_id = ? AND matcher = ?")
            params = [str(session_id), matcher]
            if source is not None:
                sql += " AND source = ?"
                params.append(source)
            deleted = con.execute(sql, params).rowcount
        return int(deleted)

    def replace_session_grants(self, session_id, grants, *, created_at=None):
        """Replace the complete grant projection as one transaction.

        A matcher/source pair has one effective decision.  Rejecting duplicates
        before BEGIN prevents caller ordering from deciding which row wins.
        """
        if isinstance(grants, (str, bytes, dict)) or grants is None:
            raise StateError("session grants 必须是 grant dict 列表")
        try:
            items = list(grants)
        except TypeError:
            raise StateError("session grants 必须是 grant dict 列表") from None
        normalized = [self._normalize_session_grant(item) for item in items]
        keys = [(item["matcher"], item["source"]) for item in normalized]
        if len(keys) != len(set(keys)):
            raise StateError("session grants 含重复 matcher/source")
        ts = str(created_at or utcnow())
        with self.transaction() as con:
            self._require_session_tx(con, session_id)
            current = con.execute(
                """
                SELECT * FROM session_grants
                 WHERE session_id = ?
                 ORDER BY matcher, source, decision, id
                """,
                (str(session_id),),
            ).fetchall()
            current_values = sorted(
                (row["matcher"], row["decision"], row["source"])
                for row in current
            )
            requested_values = sorted(
                (item["matcher"], item["decision"], item["source"])
                for item in normalized
            )
            if current_values == requested_values:
                return [dict(row) for row in current]
            con.execute(
                "DELETE FROM session_grants WHERE session_id = ?",
                (str(session_id),),
            )
            con.executemany(
                """
                INSERT INTO session_grants (
                    session_id, matcher, decision, source, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (str(session_id), item["matcher"], item["decision"],
                     item["source"], ts)
                    for item in normalized
                ],
            )
        return self.list_session_grants(session_id)

    @staticmethod
    def _fork_event_source_key(source_session_id, source_seq):
        return f"fork:{source_session_id}:{int(source_seq)}"

    @staticmethod
    def _fork_event_is_excluded(kind):
        return str(kind).startswith(FORK_EXCLUDED_EVENT_PREFIXES)

    @staticmethod
    def _fork_target_matches(existing, source_session_id, fork_seq):
        return (
            existing["parent_session_id"] == source_session_id
            and existing["fork_seq"] == fork_seq
        )

    @classmethod
    def _fork_prefix_matches(cls, rows, source_session_id, source_rows,
                             fork_seq):
        if len(rows) < len(source_rows) + 1:
            return False
        for target, source in zip(rows, source_rows):
            if (target["kind"] != source["kind"]
                    or target["turn_id"] != source["turn_id"]
                    or target["payload_json"] != source["payload_json"]
                    or target["source_key"] != cls._fork_event_source_key(
                        source_session_id, source["seq"])):
                return False
        branch = rows[len(source_rows)]
        if (branch["kind"] != "branch_created"
                or branch["source_key"]
                != f"fork-created:{source_session_id}:{fork_seq}"):
            return False
        try:
            payload = json.loads(branch["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return False
        return payload == {
            "parent_session_id": source_session_id,
            "fork_seq": fork_seq,
        }

    def fork_session(self, source_session_id, fork_seq, target_record):
        """Create an immutable branch through a real source event sequence.

        The operation is one SQLite transaction.  It copies the durable event
        journal while intentionally leaving runtime projections (queue, task,
        agent and session grants) empty.  Retrying the same completed fork is a
        no-op; an unrelated or divergent target id is rejected.
        """
        source_session_id = str(source_session_id or "").strip()
        if not source_session_id:
            raise StateError("fork source session id 不能为空")
        if isinstance(fork_seq, bool) or not isinstance(fork_seq, int):
            raise StateError("fork_seq 必须是现有 event 的正整数 seq")
        if fork_seq <= 0:
            raise StateError("fork_seq 必须是现有 event 的正整数 seq")
        if not isinstance(target_record, dict):
            raise StateError("fork target session 必须是 dict")
        target_id = str(target_record.get("id") or "").strip()
        if not target_id:
            raise StateError("fork target session 缺少 id")
        if target_id == source_session_id:
            raise StateError("fork target id 不能等于 source id")
        supplied_parent = target_record.get("parent_session_id")
        if supplied_parent not in (None, "", source_session_id):
            raise StateError("fork target parent_session_id 与 source 不一致")
        supplied_seq = target_record.get("fork_seq")
        if (supplied_seq is not None
                and (isinstance(supplied_seq, bool)
                     or not isinstance(supplied_seq, int)
                     or supplied_seq != fork_seq)):
            raise StateError("fork target fork_seq 与 cutoff 不一致")
        if target_record.get("status", "active") != "active":
            raise StateError("fork target status 必须是 active")

        with self.transaction() as con:
            source = self._require_session_tx(con, source_session_id)
            cutoff = con.execute(
                "SELECT 1 FROM events WHERE session_id = ? AND seq = ?",
                (source_session_id, fork_seq),
            ).fetchone()
            if cutoff is None:
                raise StateError(
                    f"fork source event 不存在: {source_session_id}#{fork_seq}")
            raw_source_rows = con.execute(
                """
                SELECT * FROM events
                 WHERE session_id = ? AND seq <= ?
                 ORDER BY seq
                """,
                (source_session_id, fork_seq),
            ).fetchall()
            source_rows = [
                row for row in raw_source_rows
                if not self._fork_event_is_excluded(row["kind"])
            ]
            now = utcnow()

            def inherited(name):
                value = target_record.get(name)
                return source[name] if value is None else value

            desired = {
                "id": target_id,
                "title": str(inherited("title") or ""),
                "status": "active",
                "cwd": str(inherited("cwd") or ""),
                "repo_root": inherited("repo_root"),
                "git_branch": inherited("git_branch"),
                "model": str(inherited("model") or ""),
                "gateway": str(inherited("gateway") or ""),
                "permission_mode": str(
                    inherited("permission_mode") or "default"),
                "parent_session_id": source_session_id,
                "fork_seq": fork_seq,
                "created_at": str(
                    target_record.get("created")
                    or target_record.get("created_at") or now),
                "updated_at": str(
                    target_record.get("updated")
                    or target_record.get("updated_at") or now),
                "last_context_tokens": target_record.get(
                    "last_context_tokens"),
                "tokens_in": _as_int(target_record.get("tokens_in")),
                "tokens_out": _as_int(target_record.get("tokens_out")),
            }

            existing = con.execute(
                "SELECT * FROM sessions WHERE id = ?", (target_id,)).fetchone()
            if existing is not None:
                target_rows = con.execute(
                    "SELECT * FROM events WHERE session_id = ? ORDER BY seq",
                    (target_id,),
                ).fetchall()
                if (not self._fork_target_matches(
                            existing, source_session_id, fork_seq)
                        or not self._fork_prefix_matches(
                            target_rows, source_session_id, source_rows,
                            fork_seq)):
                    raise StateError(
                        f"fork target 已存在且来源/内容不一致: {target_id}")
                return dict(existing)

            self._upsert_session_tx(con, desired)
            for row in source_rows:
                self._append_event_tx(
                    con, target_id, row["kind"],
                    json.loads(row["payload_json"]),
                    turn_id=row["turn_id"],
                    source_key=self._fork_event_source_key(
                        source_session_id, row["seq"]),
                    created_at=row["created_at"],
                )
            self._append_event_tx(
                con, target_id, "branch_created",
                {
                    "parent_session_id": source_session_id,
                    "fork_seq": fork_seq,
                },
                source_key=f"fork-created:{source_session_id}:{fork_seq}",
                created_at=desired["created_at"],
            )
        return self.get_session(target_id)

    def _project_agent_event_tx(self, con, parent_session_id, kind,
                                payload, created_at):
        """把主 session lifecycle event 投影成 agent_runs 元数据。

        child raw transcript 的权威源在 AgentStore 的原子 state.json；这里不允许
        worker 直接写 SQLite，只在 controller 主线程提交 lifecycle event 时更新
        可重建 projection。
        """
        if kind not in AGENT_EVENTS or not isinstance(payload, dict):
            return
        run_id = str(payload.get("id") or "").strip()
        if not run_id:
            return
        old = con.execute(
            "SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        transcript_id = str(
            payload.get("transcript_session_id")
            or (old["transcript_session_id"] if old else "")).strip()
        if not transcript_id:
            return
        if (old is not None
                and old["transcript_session_id"] != transcript_id):
            raise StateError(
                f"agent {run_id} transcript id 不允许改写")
        event_parent = str(
            payload.get("parent_session_id") or parent_session_id)
        if event_parent != str(parent_session_id):
            raise StateError(
                f"agent {run_id} parent_session_id 与事件 session 不一致")
        if old is not None and old["parent_session_id"] != event_parent:
            raise StateError(
                f"agent {run_id} parent_session_id 不允许改写")
        old_child = con.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?",
            (transcript_id,)).fetchone()
        if (old_child is not None
                and old_child["parent_session_id"] not in (None, event_parent)):
            raise StateError(
                f"child transcript {transcript_id} 已属于其他 parent")

        created = str(
            payload.get("created_at")
            or (old["created_at"] if old else created_at))
        updated = str(payload.get("updated_at") or created_at)
        requested_state = str(payload.get("state") or "")
        state_value = (
            requested_state if requested_state in AGENT_STATES
            else (old["state"] if old else "queued"))

        self._upsert_session_tx(con, {
            "id": transcript_id,
            "title": str(payload.get("name") or payload.get("task") or run_id),
            "status": "active",
            "cwd": str(payload.get("cwd") or ""),
            "model": str(payload.get("model") or ""),
            "gateway": str(payload.get("gateway") or ""),
            "permission_mode": "read_only",
            "parent_session_id": event_parent,
            "created_at": created,
            "updated_at": updated,
        })
        result = (
            str(payload.get("result"))
            if payload.get("result") is not None else None)
        con.execute(
            """
            INSERT INTO agent_runs (
                id, parent_session_id, parent_turn_id,
                parent_tool_call_id, transcript_session_id, name, kind, task,
                state, model, gateway, created_at, started_at, updated_at,
                ended_at, result_preview, result_truncated, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                parent_turn_id = COALESCE(
                    excluded.parent_turn_id, agent_runs.parent_turn_id),
                parent_tool_call_id = COALESCE(
                    excluded.parent_tool_call_id,
                    agent_runs.parent_tool_call_id),
                name = CASE WHEN excluded.name <> ''
                            THEN excluded.name ELSE agent_runs.name END,
                kind = CASE WHEN excluded.kind <> ''
                            THEN excluded.kind ELSE agent_runs.kind END,
                task = CASE WHEN excluded.task <> ''
                            THEN excluded.task ELSE agent_runs.task END,
                state = excluded.state,
                model = CASE WHEN excluded.model <> ''
                             THEN excluded.model ELSE agent_runs.model END,
                gateway = CASE WHEN excluded.gateway <> ''
                               THEN excluded.gateway ELSE agent_runs.gateway END,
                started_at = COALESCE(
                    excluded.started_at, agent_runs.started_at),
                updated_at = excluded.updated_at,
                ended_at = excluded.ended_at,
                result_preview = COALESCE(
                    excluded.result_preview, agent_runs.result_preview),
                result_truncated = MAX(
                    agent_runs.result_truncated,
                    excluded.result_truncated),
                error = excluded.error
            """,
            (
                run_id, event_parent,
                payload.get("parent_turn_id"),
                payload.get("parent_tool_call_id"), transcript_id,
                str(payload.get("name") or ""),
                str(payload.get("kind") or "subagent"),
                str(payload.get("task") or ""), state_value,
                str(payload.get("model") or ""),
                str(payload.get("gateway") or ""), created,
                payload.get("started_at"), updated,
                payload.get("ended_at"), result,
                1 if payload.get("result_truncated") else 0,
                (str(payload.get("error"))
                 if payload.get("error") is not None else None),
            ))

    def get_agent_run(self, agent_id):
        row = self._conn.execute(
            "SELECT * FROM agent_runs WHERE id = ?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def list_agent_runs(self, parent_session_id, *, limit=100):
        rows = self._conn.execute(
            """
            SELECT * FROM agent_runs
             WHERE parent_session_id = ?
             ORDER BY updated_at DESC, id DESC
             LIMIT ?
            """,
            (str(parent_session_id), max(0, int(limit)))).fetchall()
        return [dict(row) for row in rows]

    def _append_event_tx(self, con, session_id, kind, payload, *,
                         turn_id=None, source_key=None, created_at=None):
        body = canonical_json(payload)
        if source_key:
            old = con.execute(
                "SELECT id, seq FROM events"
                " WHERE session_id = ? AND source_key = ?",
                (session_id, source_key)).fetchone()
            if old:
                return {"id": old["id"], "seq": old["seq"],
                        "inserted": False}
        sess = con.execute(
            "SELECT last_seq FROM sessions WHERE id = ?",
            (session_id,)).fetchone()
        if not sess:
            raise StateError(f"event 引用了不存在的 session: {session_id}")
        seq = int(sess["last_seq"]) + 1
        ts = created_at or utcnow()
        cur = con.execute(
            """
            INSERT INTO events (
                session_id, seq, turn_id, kind, payload_json,
                payload_version, source_key, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (session_id, seq, turn_id, kind, body, source_key, ts))
        delta = 1 if kind in MESSAGE_EVENTS else 0
        con.execute(
            """
            UPDATE sessions
               SET last_seq = ?,
                   message_count = message_count + ?,
                   updated_at = CASE WHEN updated_at < ? THEN ? ELSE updated_at END
             WHERE id = ?
            """,
            (seq, delta, ts, ts, session_id))
        self._project_agent_event_tx(
            con, session_id, kind, payload, ts)
        return {"id": cur.lastrowid, "seq": seq, "inserted": True}

    def append_event(self, session_id, kind, payload, *, turn_id=None,
                     source_key=None, created_at=None):
        with self.transaction() as con:
            return self._append_event_tx(
                con, session_id, kind, payload, turn_id=turn_id,
                source_key=source_key, created_at=created_at)

    def append_events(self, session_id, events):
        """原子追加一批 event。

        events 的元素是 dict，至少包含 kind；可选 payload、turn_id、
        source_key、created_at。
        """
        prepared = []
        for event in events:
            if not isinstance(event, dict) or not event.get("kind"):
                raise StateError("event 必须是带 kind 的 dict")
            # 在 BEGIN 前先序列化一次，避免不可序列化对象导致半批写入。
            canonical_json(event.get("payload") or {})
            prepared.append(event)
        out = []
        with self.transaction() as con:
            for event in prepared:
                out.append(self._append_event_tx(
                    con, session_id, event["kind"],
                    event.get("payload") or {},
                    turn_id=event.get("turn_id"),
                    source_key=event.get("source_key"),
                    created_at=event.get("created_at")))
        return out

    def enqueue_input(self, session_id, item_id, mode, text, *,
                      enqueued_at=None):
        """原子记录 queued_input_added 并更新 queued_inputs 投影。"""
        item_id = str(item_id or "").strip()
        mode = str(mode or "").strip()
        if not item_id:
            raise StateError("queued input 缺少 id")
        if mode not in QUEUE_MODES:
            raise StateError(f"queued input mode 无效: {mode}")
        if not isinstance(text, str):
            raise StateError("queued input text 必须是 str")
        ts = enqueued_at or utcnow()
        with self.transaction() as con:
            event = self._append_event_tx(
                con, session_id, "queued_input_added",
                {"id": item_id, "mode": mode, "text": text},
                created_at=ts)
            con.execute(
                """
                INSERT INTO queued_inputs (
                    id, session_id, mode, text, state, enqueued_at, event_id
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (item_id, session_id, mode, text, ts, event["id"]))
        return self.get_queued_input(session_id, item_id)

    @staticmethod
    def _prepare_event_batch(events):
        prepared = []
        for event in events or ():
            if not isinstance(event, dict) or not event.get("kind"):
                raise StateError("event 必须是带 kind 的 dict")
            canonical_json(event.get("payload") or {})
            prepared.append(event)
        return prepared

    def dispatch_queued_input(self, session_id, item_id, *, turn_id=None,
                              dispatched_at=None, events=()):
        """原子 dispatch 一条输入，并在其后追加关联语义事件。"""
        prepared = self._prepare_event_batch(events)
        ts = dispatched_at or utcnow()
        with self.transaction() as con:
            row = con.execute(
                "SELECT * FROM queued_inputs WHERE session_id = ? AND id = ?",
                (session_id, item_id)).fetchone()
            if not row:
                raise StateError(f"queued input 不存在: {item_id}")
            if row["state"] != "queued":
                raise StateError(
                    f"queued input {item_id} 已是 {row['state']}")
            out = [self._append_event_tx(
                con, session_id, "queued_input_dispatched",
                {"id": item_id, "mode": row["mode"]},
                turn_id=turn_id, created_at=ts)]
            con.execute(
                """
                UPDATE queued_inputs
                   SET state = 'dispatched', dispatched_at = ?
                 WHERE session_id = ? AND id = ? AND state = 'queued'
                """,
                (ts, session_id, item_id))
            for event in prepared:
                out.append(self._append_event_tx(
                    con, session_id, event["kind"],
                    event.get("payload") or {},
                    turn_id=event.get("turn_id", turn_id),
                    source_key=event.get("source_key"),
                    created_at=event.get("created_at")))
        return out

    def cancel_queued_input(self, session_id, item_id, *, cancelled_at=None):
        """原子记录取回/取消，并把 projection 标成 cancelled。"""
        ts = cancelled_at or utcnow()
        with self.transaction() as con:
            row = con.execute(
                "SELECT * FROM queued_inputs WHERE session_id = ? AND id = ?",
                (session_id, item_id)).fetchone()
            if not row:
                raise StateError(f"queued input 不存在: {item_id}")
            if row["state"] != "queued":
                raise StateError(
                    f"queued input {item_id} 已是 {row['state']}")
            event = self._append_event_tx(
                con, session_id, "queued_input_cancelled",
                {"id": item_id, "mode": row["mode"]}, created_at=ts)
            con.execute(
                """
                UPDATE queued_inputs
                   SET state = 'cancelled'
                 WHERE session_id = ? AND id = ? AND state = 'queued'
                """,
                (session_id, item_id))
        return event

    def get_queued_input(self, session_id, item_id):
        row = self._conn.execute(
            "SELECT * FROM queued_inputs WHERE session_id = ? AND id = ?",
            (session_id, item_id)).fetchone()
        return dict(row) if row else None

    def list_queued_inputs(self, session_id, *, state="queued"):
        if state is not None and state not in QUEUE_STATES:
            raise StateError(f"queued input state 无效: {state}")
        sql = "SELECT * FROM queued_inputs WHERE session_id = ?"
        params = [session_id]
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY enqueued_at, event_id, id"
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def event_count(self, session_id=None):
        if session_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE session_id = ?",
                (session_id,)).fetchone()
        return int(row["n"])

    def get_events(self, session_id, *, after_seq=0, limit=None):
        sql = ("SELECT * FROM events WHERE session_id = ? AND seq > ?"
               " ORDER BY seq")
        params = [session_id, int(after_seq)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            out.append(item)
        return out

    def fork_seq_for_messages(self, session_id, messages):
        """Return a journal cutoff whose message projection equals ``messages``.

        Canonical JSON and the opt-in SQLite journal are committed through
        different paths.  Reading ``sessions.last_seq`` is therefore not a
        proof that the two heads describe the same conversation.  This method
        walks one SQLite snapshot, matches every durable message in order, and
        returns the latest non-message event before a later message begins.
        A missing or divergent projection returns ``None`` so callers can
        bootstrap the branch from canonical JSON instead of copying the wrong
        audit prefix.
        """
        if not isinstance(messages, list) or not messages:
            return None
        expected = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise StateError(f"message[{index}] 不是 dict")
            # Round-trip through canonical JSON so values compare by durable
            # JSON semantics rather than caller-owned mutable object identity.
            expected.append(json.loads(canonical_json(message)))

        rows = self._conn.execute(
            "SELECT seq, kind, payload_json FROM events"
            " WHERE session_id = ? ORDER BY seq",
            (str(session_id),),
        ).fetchall()
        message_kinds = MESSAGE_EVENTS | {"system_message"}
        matched = 0
        cutoff = None
        for row in rows:
            if row["kind"] not in message_kinds:
                if matched == len(expected):
                    cutoff = int(row["seq"])
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                return None
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, dict):
                return None
            if matched >= len(expected):
                break
            if json.loads(canonical_json(message)) != expected[matched]:
                return None
            matched += 1
            cutoff = int(row["seq"])

        return cutoff if matched == len(expected) else None

    def list_sessions(self, *, limit=30, offset=0, status="active", cwd=None,
                      include_agent_transcripts=False):
        where, params = [], []
        if not include_agent_transcripts:
            # child transcript 由 /agents workspace 展示；普通 /resume 不应被
            # 内部会话淹没。正常 fork 不是 agent_run，仍会出现在这里。
            where.append(
                "id NOT IN (SELECT transcript_session_id FROM agent_runs)")
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if cwd is not None:
            where.append("cwd = ?")
            params.append(str(cwd))
        sql = "SELECT * FROM sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id LIMIT ? OFFSET ?"
        params.extend([max(0, int(limit)), max(0, int(offset))])
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def bootstrap_session(self, rec, messages, *, source="shadow-bootstrap"):
        """当 shadow 在已有 JSON session 中途启用时，先导入当前消息前缀。

        只有 event_count 为零才执行；不会覆盖已存在的 event journal。
        """
        if not isinstance(messages, list):
            raise StateError("messages 必须是 list")
        with self.transaction() as con:
            sid = self._upsert_session_tx(con, rec)
            n = con.execute(
                "SELECT COUNT(*) AS n FROM events WHERE session_id = ?",
                (sid,)).fetchone()["n"]
            if n:
                return 0
            for i, message in enumerate(messages):
                if not isinstance(message, dict):
                    raise StateError(f"message[{i}] 不是 dict")
                digest = sha256_bytes(canonical_json(message).encode("utf-8"))
                self._append_event_tx(
                    con, sid, message_event_kind(message),
                    {"message": message, "source": source, "source_index": i},
                    source_key=f"{source}:{i}:{digest}",
                    created_at=rec.get("updated") or rec.get("created"))
            return len(messages)

    def import_legacy_session(self, rec, messages, *, source_path,
                              source_sha256, source_size):
        """把一个 v1 session JSON 原子导入。

        同一路径相同 hash 是幂等 skip；同一路径内容改变则拒绝猜测合并。
        """
        if not isinstance(rec, dict):
            raise StateError("session JSON 顶层必须是 object")
        if not isinstance(messages, list):
            raise StateError("session messages 必须是 list")
        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                raise StateError(f"session message[{i}] 不是 object")
            canonical_json(message)
        source_path = str(source_path)
        with self.transaction() as con:
            exact = con.execute(
                "SELECT id FROM legacy_imports"
                " WHERE source_path = ? AND source_sha256 = ?",
                (source_path, source_sha256)).fetchone()
            if exact:
                return {"skipped": True, "messages": 0}
            changed = con.execute(
                "SELECT source_sha256 FROM legacy_imports"
                " WHERE source_path = ? AND source_kind = 'session' LIMIT 1",
                (source_path,)).fetchone()
            if changed:
                raise ImportConflict(
                    f"session source 已导入但内容改变：{source_path}")
            sid = str(rec.get("id") or Path(source_path).stem)
            existing = con.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone()
            if existing:
                raise ImportConflict(
                    f"session id 已由其他来源占用：{sid}")
            mapped = {
                **rec,
                "id": sid,
                "status": rec.get("status") or "active",
            }
            self._upsert_session_tx(con, mapped)
            for i, message in enumerate(messages):
                self._append_event_tx(
                    con, sid, message_event_kind(message),
                    {
                        "message": message,
                        "imported": True,
                        "source_path": source_path,
                        "source_index": i,
                    },
                    source_key=f"legacy:{source_sha256}:{i}",
                    created_at=rec.get("updated") or rec.get("created"))
            con.execute(
                """
                INSERT INTO legacy_imports (
                    source_path, source_sha256, source_kind, source_size,
                    rows_seen, rows_imported, error_count, imported_at
                ) VALUES (?, ?, 'session', ?, ?, ?, 0, ?)
                """,
                (source_path, source_sha256, int(source_size),
                 len(messages), len(messages), utcnow()))
            return {"skipped": False, "messages": len(messages),
                    "session_id": sid}

    # ---------------------------------------------------------- M5 metrics

    @staticmethod
    def _request_id(value=None):
        request_id = str(value or f"req-{uuid.uuid4().hex}").strip()
        if not request_id:
            raise StateError("request id 不能为空")
        return request_id

    def begin_request_trace(self, rec):
        """Durably create one provider-attempt row before network I/O."""
        if not isinstance(rec, dict):
            raise StateError("request trace 必须是 dict")
        request_id = self._request_id(rec.get("id"))
        attempt = rec.get("attempt", 1)
        if (isinstance(attempt, bool) or not isinstance(attempt, int)
                or attempt < 1):
            raise StateError("request attempt 必须是 >= 1 的整数")
        model = str(rec.get("model") or "").strip()
        gateway = str(rec.get("gateway") or "").strip()
        if not model or not gateway:
            raise StateError("request trace 缺少 model/gateway")
        queued_at = _utc_timestamp(rec.get("queued_at"))
        raw = rec.get("raw")
        raw_json = canonical_json(raw) if raw is not None else None
        purpose = (
            str(raw.get("purpose") or "chat")
            if isinstance(raw, dict) else "chat")
        owner = process_owner()
        for name in owner:
            if rec.get(name) not in (None, ""):
                owner[name] = rec[name]
        try:
            with self.transaction() as con:
                con.execute(
                    """
                    INSERT INTO requests (
                        id, session_id, turn_id, attempt, model, gateway,
                        purpose, status, queued_at, context_tokens_before,
                        context_limit, summary_id,
                        owner_id, owner_pid, owner_host, owner_start_ticks,
                        heartbeat_at, cwd, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id, rec.get("session_id"), rec.get("turn_id"),
                        attempt, model, gateway, purpose, queued_at,
                        rec.get("context_tokens_before"),
                        rec.get("context_limit"), rec.get("summary_id"),
                        owner["owner_id"], owner["owner_pid"],
                        owner["owner_host"], owner["owner_start_ticks"],
                        queued_at, rec.get("cwd"), raw_json,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError(
                f"request trace 无法创建 {request_id}: {exc}") from exc
        return self.get_request_trace(request_id)

    def update_request_phase(self, request_id, phase, at=None):
        """Persist a phase edge; ``started`` is called before opening socket."""
        field = phase if str(phase).endswith("_at") else f"{phase}_at"
        if field not in REQUEST_PHASE_FIELDS:
            raise StateError(f"未知 request phase: {phase}")
        stamp = _utc_timestamp(at)
        assignment = (
            f"{field} = ?" if field == "last_delta_at"
            else f"{field} = COALESCE({field}, ?)")
        with self.transaction() as con:
            row = con.execute(
                "SELECT status FROM requests WHERE id = ?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                raise StateError(f"request trace 不存在: {request_id}")
            if row["status"] != "running":
                raise StateError(
                    f"request trace 已结束: {request_id} ({row['status']})")
            con.execute(
                f"UPDATE requests SET {assignment}, heartbeat_at = ?"
                " WHERE id = ?",
                (stamp, stamp, str(request_id)),
            )
        return self.get_request_trace(request_id)

    @staticmethod
    def _decode_health_row(row):
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("capability_json", None)
        if raw:
            try:
                result["capability"] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                result["capability"] = None
                result["capability_corrupt"] = True
                result["capability_json_raw"] = raw
        else:
            result["capability"] = None
        return result

    @classmethod
    def _health_tx(cls, con, gateway, model):
        row = con.execute(
            "SELECT * FROM model_health WHERE gateway = ? AND model = ?",
            (str(gateway), str(model)),
        ).fetchone()
        return cls._decode_health_row(row) or {
            "gateway": str(gateway), "model": str(model),
            "success_count": 0, "failure_count": 0,
            "consecutive_failures": 0,
            "probe_transient_failures": 0,
            "capability": None,
        }

    @staticmethod
    def _write_health_tx(con, record):
        capability = record.get("capability")
        if capability is not None:
            capability_json = canonical_json(capability)
        elif record.get("capability_corrupt"):
            capability_json = record.get("capability_json_raw")
        else:
            capability_json = None
        con.execute(
            """
            INSERT INTO model_health (
                gateway, model, capability_json, capability_source,
                last_success, last_failure, last_probe,
                true_ttft_ms, total_latency_ms,
                success_count, failure_count, consecutive_failures,
                ewma_latency_ms, circuit_open_until, last_error_kind,
                expires_at, last_interrupted, probe_status,
                probe_transient_failures, probe_catalog_fingerprint,
                last_outcome_at, last_route_recovery_at,
                last_context_probe, context_probe_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gateway, model) DO UPDATE SET
                capability_json = excluded.capability_json,
                capability_source = excluded.capability_source,
                last_success = excluded.last_success,
                last_failure = excluded.last_failure,
                last_probe = excluded.last_probe,
                true_ttft_ms = excluded.true_ttft_ms,
                total_latency_ms = excluded.total_latency_ms,
                success_count = excluded.success_count,
                failure_count = excluded.failure_count,
                consecutive_failures = excluded.consecutive_failures,
                ewma_latency_ms = excluded.ewma_latency_ms,
                circuit_open_until = excluded.circuit_open_until,
                last_error_kind = excluded.last_error_kind,
                expires_at = excluded.expires_at,
                last_interrupted = excluded.last_interrupted,
                probe_status = excluded.probe_status,
                probe_transient_failures = excluded.probe_transient_failures,
                probe_catalog_fingerprint = excluded.probe_catalog_fingerprint,
                last_outcome_at = excluded.last_outcome_at,
                last_route_recovery_at = excluded.last_route_recovery_at,
                last_context_probe = excluded.last_context_probe,
                context_probe_status = excluded.context_probe_status
            """,
            (
                record["gateway"], record["model"], capability_json,
                record.get("capability_source"), record.get("last_success"),
                record.get("last_failure"), record.get("last_probe"),
                record.get("true_ttft_ms"),
                record.get("total_latency_ms"),
                _as_int(record.get("success_count")),
                _as_int(record.get("failure_count")),
                _as_int(record.get("consecutive_failures")),
                record.get("ewma_latency_ms"),
                record.get("circuit_open_until"),
                record.get("last_error_kind"), record.get("expires_at"),
                record.get("last_interrupted"), record.get("probe_status"),
                _as_int(record.get("probe_transient_failures")),
                record.get("probe_catalog_fingerprint"),
                record.get("last_outcome_at"),
                record.get("last_route_recovery_at"),
                record.get("last_context_probe"),
                record.get("context_probe_status"),
            ),
        )

    @classmethod
    def _rebuild_request_health_tx(cls, con, gateway, model, *, policy):
        """Rebuild request-derived health in true ended_at order.

        Multiple terminals can finish and acquire SQLite's write lock out of
        chronological order.  Replaying the indexed per-model request history
        makes circuit state independent of commit order while retaining probe
        capability/freshness fields already stored in model_health.
        """
        health = cls._health_tx(con, gateway, model)
        for key, value in {
            "last_success": None,
            "last_failure": None,
            "last_interrupted": None,
            "last_outcome_at": None,
            "true_ttft_ms": None,
            "total_latency_ms": None,
            "success_count": 0,
            "failure_count": 0,
            "consecutive_failures": 0,
            "ewma_latency_ms": None,
            "circuit_open_until": None,
            "last_error_kind": None,
        }.items():
            health[key] = value
        recovery_raw = health.get("last_route_recovery_at")
        try:
            recovery_epoch = (
                health_policy.parse_utc(recovery_raw)
                if recovery_raw else None)
        except (TypeError, ValueError):
            recovery_epoch = None
        rows = con.execute(
            """
            SELECT id, purpose, status, ended_at, error_kind, retryable,
                   true_ttft_ms, total_latency_ms
              FROM requests
             WHERE gateway = ? AND model = ?
               AND COALESCE(purpose, 'chat')
                   IN ('chat', 'context_compaction')
               AND status IN ('ok', 'failed', 'interrupted')
               AND ended_at IS NOT NULL
             ORDER BY ended_at, id
            """,
            (str(gateway), str(model)),
        ).fetchall()
        recovery_applied = False
        for row in rows:
            ended_epoch = health_policy.parse_utc(row["ended_at"])
            if (recovery_epoch is not None and not recovery_applied
                    and ended_epoch > recovery_epoch):
                cls._reset_route_tail(health)
                recovery_applied = True
            health = cls._apply_request_health_outcome(
                health, row, policy=policy)
        if recovery_epoch is not None and not recovery_applied:
            cls._reset_route_tail(health)
        health["gateway"], health["model"] = str(gateway), str(model)
        return health

    @staticmethod
    def _reset_route_tail(health):
        health["consecutive_failures"] = 0
        health["circuit_open_until"] = None
        health["last_error_kind"] = None

    @staticmethod
    def _apply_request_health_outcome(health, row, *, policy):
        """Apply one already-ordered terminal request to a health record."""
        purpose = (
            row["purpose"] if "purpose" in row.keys() else "chat")
        measured_chat = str(purpose or "chat") == "chat"
        preserved = {
            name: health.get(name)
            for name in (
                "success_count", "failure_count", "last_success",
                "last_failure", "last_interrupted")
        }
        outcome = {
            "ok": health_policy.REQUEST_SUCCESS,
            "failed": health_policy.REQUEST_FAILURE,
            "interrupted": health_policy.REQUEST_INTERRUPTED,
        }[row["status"]]
        ended_epoch = health_policy.parse_utc(row["ended_at"])
        health = health_policy.record_request_outcome(
            health, outcome, error_kind=row["error_kind"],
            retryable=bool(row["retryable"]),
            policy=policy, now=ended_epoch)
        if not measured_chat:
            # Compaction is real automatic route evidence, so it participates
            # in the circuit tail, but /model health success/latency remains
            # a user-chat measurement rather than hidden maintenance traffic.
            health.update(preserved)
        if measured_chat and row["true_ttft_ms"] is not None:
            health["true_ttft_ms"] = row["true_ttft_ms"]
        if measured_chat and row["total_latency_ms"] is not None:
            health["total_latency_ms"] = row["total_latency_ms"]
            if row["status"] == "ok":
                old = health.get("ewma_latency_ms")
                health["ewma_latency_ms"] = (
                    row["total_latency_ms"] if old is None
                    else 0.2 * float(row["total_latency_ms"])
                    + 0.8 * float(old))
        return health

    @classmethod
    def _update_request_health_tx(cls, con, row, *, policy):
        """O(1) normal update; replay only a rare out-of-order completion."""
        gateway, model = str(row["gateway"]), str(row["model"])
        health = cls._health_tx(con, gateway, model)
        last_raw = health.get("last_outcome_at")
        counts = (
            int(health.get("success_count") or 0)
            + int(health.get("failure_count") or 0))
        rebuild = bool(counts and not last_raw)
        try:
            ended_epoch = health_policy.parse_utc(row["ended_at"])
            last_epoch = (
                health_policy.parse_utc(last_raw) if last_raw else None)
            recovery_raw = health.get("last_route_recovery_at")
            recovery_epoch = (
                health_policy.parse_utc(recovery_raw)
                if recovery_raw else None)
        except (TypeError, ValueError):
            rebuild = True
            ended_epoch = last_epoch = recovery_epoch = None
        # Equal timestamps use request id as the replay-query tie-breaker.
        if (not rebuild and last_epoch is not None
                and ended_epoch is not None and ended_epoch <= last_epoch):
            rebuild = True
        # A request that logically ended before the last successful recovery
        # may commit later in another terminal. Replay so it contributes to
        # lifetime counts but cannot resurrect the pre-recovery circuit tail.
        if (not rebuild and recovery_epoch is not None
                and ended_epoch is not None
                and ended_epoch <= recovery_epoch):
            rebuild = True
        if rebuild:
            return cls._rebuild_request_health_tx(
                con, gateway, model, policy=policy)
        updated = cls._apply_request_health_outcome(
            health, row, policy=policy)
        updated["gateway"], updated["model"] = gateway, model
        return updated

    def finalize_request_trace(self, request_id, result, *,
                               policy=health_policy.DEFAULT_POLICY):
        """Finalize one attempt and update its route health atomically."""
        if not isinstance(result, dict):
            raise StateError("request result 必须是 dict")
        status = str(result.get("status") or "").strip()
        if status not in REQUEST_STATUSES - {"running"}:
            raise StateError(f"request status 无效: {status}")
        phases = dict(result.get("phases") or {})
        for key in phases:
            if key not in REQUEST_PHASE_FIELDS:
                raise StateError(f"未知 request phase: {key}")
        phases["ended_at"] = phases.get("ended_at") or result.get("ended_at")
        phases["ended_at"] = _utc_timestamp(phases["ended_at"])
        for key, value in tuple(phases.items()):
            if value is not None and key != "ended_at":
                phases[key] = _utc_timestamp(value)

        with self.transaction() as con:
            row = con.execute(
                "SELECT * FROM requests WHERE id = ?", (str(request_id),)
            ).fetchone()
            if row is None:
                raise StateError(f"request trace 不存在: {request_id}")
            if row["status"] != "running":
                raise StateError(
                    f"request trace 已结束: {request_id} ({row['status']})")
            current = dict(row)
            merged = {
                field: phases.get(field) or current.get(field)
                for field in REQUEST_PHASE_FIELDS
            }
            merged["ended_at"] = phases["ended_at"]
            true_ttft = _elapsed_ms(
                merged.get("started_at"), merged.get("first_delta_at"))
            total_latency = _elapsed_ms(
                merged.get("started_at"), merged.get("ended_at"))

            usage = result.get("usage") if status == "ok" else None
            usage = usage if isinstance(usage, dict) and usage else {}
            usage_reported = 1 if usage else 0
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            cache_read = usage.get("cache_read")
            cache_write = usage.get("cache_write")
            if not bool(usage.get("cache_reported")):
                cache_read = None
                cache_write = None
            error_kind = (
                None if status == "ok"
                else str(result.get("error_kind") or status))
            if status == "ok":
                error_text = None
            else:
                from .tasks import redact_known_secrets
                error_text = redact_known_secrets(
                    str(result.get("error_text") or ""))[:4000]
            from .tasks import redact_known_secrets
            error_code = (
                redact_known_secrets(str(result["error_code"]))[:200]
                if result.get("error_code") not in (None, "") else None)
            provider_request_id = (
                redact_known_secrets(
                    str(result["provider_request_id"]))[:300]
                if result.get("provider_request_id") not in (None, "")
                else None)
            retry_reason = result.get("retry_reason")
            raw = result.get("raw")
            raw_json = (
                canonical_json(raw) if raw is not None
                else current.get("raw_json"))

            con.execute(
                """
                UPDATE requests SET
                    status = ?, queued_at = ?, started_at = ?, headers_at = ?,
                    first_delta_at = ?, last_delta_at = ?, ended_at = ?,
                    http_status = ?, error_kind = ?, error_text = ?,
                    retry_reason = ?, retryable = ?, prompt_tokens = ?,
                    completion_tokens = ?, cache_read = ?, cache_write = ?,
                    usage_reported = ?, total_ms = ?, true_ttft_ms = ?,
                    total_latency_ms = ?, error_code = ?,
                    provider_request_id = ?, heartbeat_at = ?, raw_json = ?
                WHERE id = ?
                """,
                (
                    status, merged.get("queued_at"), merged.get("started_at"),
                    merged.get("headers_at"), merged.get("first_delta_at"),
                    merged.get("last_delta_at"), merged.get("ended_at"),
                    result.get("http_status"), error_kind, error_text,
                    retry_reason, 1 if result.get("retryable") else 0,
                    prompt_tokens, completion_tokens,
                    cache_read, cache_write, usage_reported, total_latency,
                    true_ttft, total_latency, error_code,
                    provider_request_id, merged.get("ended_at"), raw_json,
                    str(request_id),
                ),
            )

            # Probe traffic owns a separate capability/freshness transition;
            # it must not inflate chat counts or latency. Chat requests use
            # O(1) updates normally and indexed replay for a rare out-of-order
            # cross-terminal completion.
            if str(current.get("purpose") or "chat") in {
                    "chat", "context_compaction"}:
                terminal = dict(current)
                terminal.update({
                    "status": status,
                    "ended_at": merged.get("ended_at"),
                    "error_kind": error_kind,
                    "retryable": 1 if result.get("retryable") else 0,
                    "true_ttft_ms": true_ttft,
                    "total_latency_ms": total_latency,
                })
                health = self._update_request_health_tx(
                    con, terminal, policy=policy)
                self._write_health_tx(con, health)

        return self.get_request_trace(request_id)

    @staticmethod
    def _decode_request_row(row):
        if row is None:
            return None
        result = dict(row)
        raw = result.get("raw_json")
        if raw:
            try:
                result["raw"] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                result["raw_corrupt"] = True
        return result

    def get_request_trace(self, request_id):
        row = self._conn.execute(
            "SELECT * FROM requests WHERE id = ?", (str(request_id),)
        ).fetchone()
        return self._decode_request_row(row)

    def list_recent_traces(self, *, limit=100, session_id=None,
                           gateway=None, model=None, turn_id_prefix=None):
        limit = max(1, min(int(limit), 10_000))
        clauses, params = [], []
        for column, value in (
                ("session_id", session_id), ("gateway", gateway),
                ("model", model)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(str(value))
        if turn_id_prefix is not None:
            escaped = (str(turn_id_prefix).replace("\\", "\\\\")
                       .replace("%", "\\%").replace("_", "\\_"))
            clauses.append("turn_id LIKE ? ESCAPE '\\'")
            params.append(escaped + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._conn.execute(
            "SELECT * FROM requests" + where
            + " ORDER BY COALESCE(ended_at, started_at, queued_at) DESC,"
              " id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._decode_request_row(row) for row in rows]

    def get_model_health(self, gateway, model):
        row = self._conn.execute(
            "SELECT * FROM model_health WHERE gateway = ? AND model = ?",
            (str(gateway), str(model)),
        ).fetchone()
        return self._decode_health_row(row)

    def list_model_health(self, *, gateway=None, limit=1000):
        limit = max(1, min(int(limit), 10_000))
        if gateway is None:
            rows = self._conn.execute(
                "SELECT * FROM model_health"
                " ORDER BY gateway, model LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM model_health WHERE gateway = ?"
                " ORDER BY model LIMIT ?", (str(gateway), limit)
            ).fetchall()
        return [self._decode_health_row(row) for row in rows]

    def route_health_decision(self, gateway, model, *, explicit,
                              now=None, clock=None):
        return health_policy.route_decision(
            self.get_model_health(gateway, model), explicit=bool(explicit),
            now=now, clock=clock)

    def probe_health_decision(self, gateway, model, *, force=False,
                              catalog_fingerprint=None, now=None, clock=None):
        return health_policy.probe_decision(
            self.get_model_health(gateway, model), force=bool(force),
            catalog_fingerprint=catalog_fingerprint, now=now, clock=clock)

    def record_probe_outcome(self, gateway, model, outcome, *,
                             catalog_fingerprint=None, capability=None,
                             capability_source=None,
                             probe_kind="capability_probe",
                             policy=health_policy.DEFAULT_POLICY,
                             now=None, clock=None):
        gateway, model = str(gateway), str(model)
        with self.transaction() as con:
            current = self._health_tx(con, gateway, model)
            if probe_kind == "context_probe":
                if outcome not in {
                        health_policy.PROBE_SUCCESS,
                        health_policy.PROBE_TRANSIENT_FAILURE}:
                    raise ValueError(
                        "context probe 只接受 success/transient_failure")
                observation = health_policy.record_probe_outcome(
                    {}, outcome, policy=policy, now=now, clock=clock)
                updated = dict(current)
                updated["last_context_probe"] = observation["last_probe"]
                updated["context_probe_status"] = outcome
            elif probe_kind == "capability_probe":
                updated = health_policy.record_probe_outcome(
                    current, outcome,
                    catalog_fingerprint=catalog_fingerprint,
                    policy=policy, now=now, clock=clock)
            else:
                raise ValueError(f"未知 probe kind: {probe_kind}")
            updated["gateway"], updated["model"] = gateway, model
            if outcome == health_policy.PROBE_SUCCESS:
                # A successful explicit probe is direct recovery evidence but
                # does not inflate chat request counts/latency.
                self._reset_route_tail(updated)
                updated["last_route_recovery_at"] = (
                    updated["last_context_probe"]
                    if probe_kind == "context_probe"
                    else updated["last_probe"])
            if capability is not None:
                # canonical_json below validates serializability before SQL.
                canonical_json(capability)
                updated["capability"] = capability
            if capability_source is not None:
                updated["capability_source"] = str(capability_source)
            self._write_health_tx(con, updated)
        return self.get_model_health(gateway, model)

    def begin_tool_run(self, rec):
        """Create a durable tool-run intent before the tool side effect."""
        if not isinstance(rec, dict):
            raise StateError("tool run 必须是 dict")
        run_id = str(rec.get("id") or f"tool-{uuid.uuid4().hex}").strip()
        session_id = str(rec.get("session_id") or "").strip()
        name = str(rec.get("name") or "").strip()
        if not run_id or not session_id or not name:
            raise StateError("tool run 缺少 id/session_id/name")
        args = rec.get("args_redacted")
        owner = process_owner()
        for field in owner:
            if rec.get(field) not in (None, ""):
                owner[field] = rec[field]
        started_at = _utc_timestamp(rec.get("started_at"))
        with self.transaction() as con:
            con.execute(
                """
                INSERT INTO tool_runs (
                    id, tool_call_id, session_id, turn_id, request_id, name,
                    args_redacted_json, approval_decision, approval_source,
                    started_at, status, owner_id, owner_pid, owner_host,
                    owner_start_ticks, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    run_id, rec.get("tool_call_id"), session_id,
                    rec.get("turn_id"), rec.get("request_id"), name,
                    canonical_json(args) if args is not None else None,
                    rec.get("approval_decision"), rec.get("approval_source"),
                    started_at, owner["owner_id"], owner["owner_pid"],
                    owner["owner_host"], owner["owner_start_ticks"],
                    started_at,
                ),
            )
        return self.get_tool_run(run_id)

    def finalize_tool_run(self, run_id, *, status, ended_at=None,
                          exit_code=None, signal=None,
                          stdout_artifact=None, stderr_artifact=None,
                          stdout_path=None, stderr_path=None):
        status = str(status)
        if status not in TOOL_RUN_STATUSES - {"running"}:
            raise StateError(f"tool run status 无效: {status}")
        stamp = _utc_timestamp(ended_at)
        with self.transaction() as con:
            row = con.execute(
                "SELECT status FROM tool_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise StateError(f"tool run 不存在: {run_id}")
            if row["status"] != "running":
                raise StateError(f"tool run 已结束: {run_id}")
            con.execute(
                """
                UPDATE tool_runs SET status = ?, ended_at = ?, exit_code = ?,
                    signal = ?, stdout_artifact = ?, stderr_artifact = ?,
                    stdout_path = ?, stderr_path = ?, heartbeat_at = ?
                WHERE id = ?
                """,
                (status, stamp, exit_code, signal,
                 stdout_artifact, stderr_artifact,
                 str(stdout_path) if stdout_path else None,
                 str(stderr_path) if stderr_path else None,
                 stamp,
                 str(run_id)),
            )
        return self.get_tool_run(run_id)

    def get_tool_run(self, run_id):
        row = self._conn.execute(
            "SELECT * FROM tool_runs WHERE id = ?", (str(run_id),)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("args_redacted_json", None)
        try:
            result["args_redacted"] = json.loads(raw) if raw else None
        except (TypeError, json.JSONDecodeError):
            result["args_redacted"] = None
            result["args_redacted_corrupt"] = True
        return result

    def list_recent_tool_runs(self, *, limit=100, session_id=None,
                              turn_id_prefix=None):
        limit = max(1, min(int(limit), 10_000))
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if turn_id_prefix is not None:
            escaped = (str(turn_id_prefix).replace("\\", "\\\\")
                       .replace("%", "\\%").replace("_", "\\_"))
            clauses.append("turn_id LIKE ? ESCAPE '\\'")
            params.append(escaped + "%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._conn.execute(
            "SELECT id FROM tool_runs" + where
            + " ORDER BY started_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self.get_tool_run(row["id"]) for row in rows]

    def _insert_request_tx(self, con, rec, legacy_key):
        rid = "legacy-" + sha256_bytes(legacy_key.encode("utf-8"))[:24]
        secs = _as_float(rec.get("secs"), 0.0)
        cache_reported = bool(rec.get("cache_reported"))
        usage_reported = any(
            rec.get(name) is not None
            for name in ("prompt_tokens", "completion_tokens"))
        cur = con.execute(
            """
            INSERT OR IGNORE INTO requests (
                id, legacy_key, session_id, turn_id, attempt, model, gateway,
                status, ended_at, prompt_tokens, completion_tokens, cache_read,
                cache_write, usage_reported, total_ms, cwd, raw_json
            ) VALUES (?, ?, ?, ?, 1, ?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid,
                legacy_key,
                rec.get("session"),
                str(rec.get("turn") or ""),
                str(rec.get("model") or ""),
                str(rec.get("gateway") or ""),
                rec.get("ts"),
                _as_int(rec.get("prompt_tokens")),
                _as_int(rec.get("completion_tokens")),
                (_as_int(rec.get("cache_read")) if cache_reported else None),
                (_as_int(rec.get("cache_write")) if cache_reported else None),
                1 if usage_reported else 0,
                secs * 1000 if secs else None,
                rec.get("cwd"),
                canonical_json(rec),
            ))
        return bool(cur.rowcount)

    def record_usage(self, rec, *, source_key):
        with self.transaction() as con:
            return self._insert_request_tx(con, rec, source_key)

    def import_legacy_usage(self, rows, errors, *, source_path,
                            source_sha256, source_size, rows_seen):
        """原子导入一份 usage.jsonl 的有效行和错误清单。"""
        source_path = str(source_path)
        with self.transaction() as con:
            exact = con.execute(
                "SELECT id FROM legacy_imports"
                " WHERE source_path = ? AND source_sha256 = ?",
                (source_path, source_sha256)).fetchone()
            if exact:
                return {"skipped": True, "rows": 0, "errors": 0}
            changed = con.execute(
                "SELECT source_sha256 FROM legacy_imports"
                " WHERE source_path = ? AND source_kind = 'usage' LIMIT 1",
                (source_path,)).fetchone()
            if changed:
                raise ImportConflict(
                    f"usage source 已导入但内容改变：{source_path}")
            imported = 0
            for line_no, rec in rows:
                key = f"legacy-usage:{source_sha256}:{line_no}"
                imported += int(self._insert_request_tx(con, rec, key))
            for issue in errors:
                con.execute(
                    """
                    INSERT INTO migration_errors (
                        source_path, source_sha256, line_number, error,
                        raw_excerpt, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (source_path, source_sha256, issue.get("line"),
                     issue["error"], issue.get("raw_excerpt"), utcnow()))
            con.execute(
                """
                INSERT INTO legacy_imports (
                    source_path, source_sha256, source_kind, source_size,
                    rows_seen, rows_imported, error_count, imported_at
                ) VALUES (?, ?, 'usage', ?, ?, ?, ?, ?)
                """,
                (source_path, source_sha256, int(source_size), int(rows_seen),
                 imported, len(errors), utcnow()))
            return {"skipped": False, "rows": imported,
                    "errors": len(errors)}

    def integrity_check(self):
        return [str(r[0]) for r in
                self._conn.execute("PRAGMA integrity_check").fetchall()]

    def foreign_key_violations(self):
        return [tuple(r) for r in
                self._conn.execute("PRAGMA foreign_key_check").fetchall()]

    def metrics_counts(self):
        """Counts for the always-on M5 observability tables."""
        names = ("requests", "tool_runs", "model_health")
        return {
            name: int(self._conn.execute(
                f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in names
        }

    def reconcile_stale_metrics(self, *, remote_stale_after=7 * 24 * 60 * 60,
                                now=None):
        """Close crash-left running rows without touching live terminals."""
        now_epoch = float(time.time() if now is None else now)
        ended_at = health_policy.format_utc(now_epoch)

        def stale(row):
            value = dict(row)
            # Rows created before owner tracking cannot belong to a current
            # owner-aware process; recover them once during rollout.
            if not value.get("owner_id"):
                return True
            local_alive = _local_owner_alive(value)
            if local_alive is not None:
                return not local_alive
            heartbeat = (
                value.get("heartbeat_at") or value.get("started_at")
                or value.get("queued_at"))
            if not heartbeat:
                return False
            try:
                return (
                    now_epoch - health_policy.parse_utc(heartbeat)
                    >= float(remote_stale_after))
            except (TypeError, ValueError):
                return False

        requests = self._conn.execute(
            "SELECT * FROM requests WHERE status = 'running'").fetchall()
        tools = self._conn.execute(
            "SELECT * FROM tool_runs WHERE status = 'running'").fetchall()
        stale_requests = [row["id"] for row in requests if stale(row)]
        stale_tools = [row["id"] for row in tools if stale(row)]
        if stale_requests or stale_tools:
            with self.transaction() as con:
                con.executemany(
                    """
                    UPDATE requests
                       SET status = 'interrupted', ended_at = ?,
                           heartbeat_at = ?, error_kind = 'process_crash',
                           error_text = 'owner process exited before finalize',
                           retryable = 0
                     WHERE id = ? AND status = 'running'
                    """,
                    ((ended_at, ended_at, request_id)
                     for request_id in stale_requests),
                )
                con.executemany(
                    """
                    UPDATE tool_runs
                       SET status = 'unknown', ended_at = ?, heartbeat_at = ?
                     WHERE id = ? AND status = 'running'
                    """,
                    ((ended_at, ended_at, run_id)
                     for run_id in stale_tools),
                )
        return {
            "requests": len(stale_requests),
            "tools": len(stale_tools),
        }

    def request_usage_summary(self, *, session_id=None, since=None):
        """Aggregate M5 attempts while preserving unknown measurement state."""
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id LIKE ?")
            params.append(str(session_id) + "%")
        if since is not None:
            clauses.append(
                "COALESCE(ended_at, started_at, queued_at) >= ?")
            params.append(_utc_timestamp(since))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS attempts,
                COALESCE(SUM(CASE WHEN usage_reported = 1
                                  THEN 1 ELSE 0 END), 0) AS measured,
                COALESCE(SUM(CASE WHEN usage_reported = 0
                                  THEN 1 ELSE 0 END), 0) AS unknown,
                COALESCE(SUM(CASE WHEN usage_reported = 1
                                  THEN prompt_tokens ELSE 0 END), 0)
                    AS prompt_tokens,
                COALESCE(SUM(CASE WHEN usage_reported = 1
                                  THEN completion_tokens ELSE 0 END), 0)
                    AS completion_tokens,
                COALESCE(SUM(CASE WHEN usage_reported = 1
                                       AND cache_read IS NOT NULL
                                       AND cache_write IS NOT NULL
                                  THEN 1 ELSE 0 END), 0) AS cache_measured,
                COALESCE(SUM(CASE WHEN usage_reported = 1
                                  THEN cache_read ELSE 0 END), 0)
                    AS cache_read,
                COALESCE(SUM(CASE WHEN usage_reported = 1
                                  THEN cache_write ELSE 0 END), 0)
                    AS cache_write,
                COALESCE(SUM(CASE WHEN status = 'ok'
                                  THEN 1 ELSE 0 END), 0) AS ok,
                COALESCE(SUM(CASE WHEN status = 'failed'
                                  THEN 1 ELSE 0 END), 0) AS failed,
                COALESCE(SUM(CASE WHEN status = 'interrupted'
                                  THEN 1 ELSE 0 END), 0) AS interrupted,
                COALESCE(SUM(CASE WHEN status = 'running'
                                  THEN 1 ELSE 0 END), 0) AS running
              FROM requests
            """ + where,
            tuple(params),
        ).fetchone()
        result = {name: int(row[name] or 0) for name in row.keys()}
        result["cache_unknown"] = max(
            0, result["measured"] - result["cache_measured"])
        return result

    def counts(self):
        names = ("sessions", "events", "requests", "legacy_imports",
                 "migration_errors")
        return {
            name: int(self._conn.execute(
                f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in names
        }

    def latest_import_for(self, source_path, source_kind):
        row = self._conn.execute(
            """
            SELECT * FROM legacy_imports
             WHERE source_path = ? AND source_kind = ?
             ORDER BY imported_at DESC, id DESC LIMIT 1
            """,
            (str(source_path), source_kind)).fetchone()
        return dict(row) if row else None


class RequestTrace:
    """One open attempt backed by a StateStore connection.

    The row is inserted by :class:`MetricsFacade` before this object is
    returned.  ``started`` is synchronously persisted before the caller opens
    a socket; later high-frequency edges remain in memory and are committed
    together with the result and model-health transition. Facade-owned traces
    share that thread's cached connection; standalone callers may opt into
    owning and closing their connection.
    """

    def __init__(self, db, record, *, clock=None,
                 policy=health_policy.DEFAULT_POLICY, owns_db=True):
        self._db = db
        self._clock = clock or time.time
        self.id = record["id"]
        self.attempt = int(record["attempt"])
        self._policy = policy
        self._owns_db = bool(owns_db)
        self._phases = dict.fromkeys(REQUEST_PHASE_FIELDS)
        self._phases["queued_at"] = record["queued_at"]
        self._last_delta_persisted_at = None
        self._closed = False

    def mark(self, phase, attempt=None):
        if self._closed:
            raise StateError(f"request trace 已关闭: {self.id}")
        if attempt is not None and int(attempt) != self.attempt:
            raise StateError(
                f"request attempt 不匹配: {attempt} != {self.attempt}")
        field = phase if str(phase).endswith("_at") else f"{phase}_at"
        if field not in REQUEST_PHASE_FIELDS:
            raise StateError(f"未知 request phase: {phase}")
        if field == "queued_at":
            return self._phases[field]
        if field != "last_delta_at" and self._phases[field] is not None:
            return self._phases[field]
        at = float(self._clock())
        # last_delta is a token-frequency edge. Persisting it per chunk turns
        # one streamed answer into thousands of BEGIN IMMEDIATE locks and
        # makes other terminals visibly stutter. Keep the exact value in
        # memory for finalize, while refreshing the cross-terminal /status
        # view no more than twice a second. The first-delta edge immediately
        # preceding it is already durable, so its first write can wait.
        if field == "last_delta_at":
            previous = self._last_delta_persisted_at
            if previous is None:
                self._last_delta_persisted_at = at
            elif at - previous >= 0.5:
                self._db.update_request_phase(self.id, field, at)
                self._last_delta_persisted_at = at
            self._phases[field] = at
            return at

        # Persist each low-frequency semantic edge so a concurrent /status can
        # report the actual phase. started remains the side-effect gate.
        self._db.update_request_phase(self.id, field, at)
        self._phases[field] = at
        return at

    def snapshot(self):
        return {"attempt": self.attempt, **self._phases}

    def finalize(self, status, *, usage=None, error=None, error_kind=None,
                 retry_reason=None, retryable=False, raw=None, policy=None):
        if self._closed:
            raise StateError(f"request trace 已关闭: {self.id}")
        if self._phases["ended_at"] is None:
            self.mark("ended")
        details = {}
        if error is not None:
            details_method = getattr(error, "details", None)
            if callable(details_method):
                details = details_method() or {}
        result = {
            "status": status,
            "phases": dict(self._phases),
            "usage": usage,
            "error_kind": error_kind or details.get("kind") or (
                type(error).__name__ if error is not None else None),
            "error_text": str(error) if error is not None else None,
            "http_status": details.get("http_status"),
            "error_code": details.get("code"),
            "provider_request_id": details.get("provider_request_id"),
            "retry_reason": retry_reason,
            "retryable": (
                details.get("retryable")
                if details.get("retryable") is not None else retryable),
            "raw": raw,
        }
        try:
            row = self._db.finalize_request_trace(
                self.id, result, policy=policy or self._policy)
        finally:
            if self._owns_db:
                self._db.close()
            self._closed = True
        return row

    def close(self):
        """Close without claiming a result; client code should finalize first."""
        if not self._closed:
            if self._owns_db:
                self._db.close()
            self._closed = True


class MetricsFacade:
    """Thread/process-safe facade over the always-on metrics database.

    No SQLite connection is shared between threads or processes. Each thread
    reuses one checked connection for requests and queries, avoiding open and
    schema work on every streamed attempt. SQLite ``BEGIN IMMEDIATE``
    serializes health read-modify-write transitions across terminals.
    """

    def __init__(self, path, *, journal_mode="WAL", timeout=5.0, clock=None,
                 policy=health_policy.DEFAULT_POLICY):
        self.path = Path(path)
        self.journal_mode = journal_mode
        self.timeout = float(timeout)
        self.clock = clock
        self.policy = policy
        self._bootstrap_lock = threading.Lock()
        self._bootstrapped = False
        self._local = threading.local()

    def _open(self):
        if self._bootstrapped:
            return StateStore(
                self.path, journal_mode=self.journal_mode,
                timeout=self.timeout, initialize_schema=False)
        with self._bootstrap_lock:
            if self._bootstrapped:
                return StateStore(
                    self.path, journal_mode=self.journal_mode,
                    timeout=self.timeout, initialize_schema=False)
            db = StateStore(
                self.path, journal_mode=self.journal_mode,
                timeout=self.timeout, initialize_schema=True)
            try:
                db.reconcile_stale_metrics()
            except BaseException:
                db.close()
                raise
            self._bootstrapped = True
            return db

    def _shared(self):
        db = getattr(self._local, "db", None)
        if db is not None and not db.closed:
            try:
                stat_result = self.path.stat()
                identity = (
                    int(stat_result.st_dev), int(stat_result.st_ino))
            except OSError:
                identity = None
            if identity == getattr(db, "file_identity", None):
                return db
            db.close()
        db = self._open()
        self._local.db = db
        return db

    def close(self):
        """Close this thread's cached connection; safe for tests/reconfigure."""
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            del self._local.db

    def begin_attempt(self, *, gateway, model, attempt=1, session_id=None,
                      turn_id=None, request_id=None,
                      context_tokens_before=None, context_limit=None,
                      summary_id=None, cwd=None, raw=None):
        db = self._shared()
        try:
            queued_at = (self.clock or time.time)()
            record = db.begin_request_trace({
                "id": request_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "attempt": attempt,
                "model": model,
                "gateway": gateway,
                "queued_at": queued_at,
                "context_tokens_before": context_tokens_before,
                "context_limit": context_limit,
                "summary_id": summary_id,
                "cwd": cwd,
                "raw": raw,
            })
        except BaseException:
            raise
        return RequestTrace(
            db, record, clock=self.clock, policy=self.policy, owns_db=False)

    def get(self, request_id):
        return self._shared().get_request_trace(request_id)

    get_trace = get

    def list_recent_traces(self, **filters):
        return self._shared().list_recent_traces(**filters)

    def get_health(self, gateway, model):
        return self._shared().get_model_health(gateway, model)

    def list_health(self, **filters):
        return self._shared().list_model_health(**filters)

    def route_decision(self, gateway, model, *, explicit, now=None):
        return self._shared().route_health_decision(
            gateway, model, explicit=explicit, now=now,
            clock=self.clock if now is None else None)

    def probe_decision(self, gateway, model, *, force=False,
                       catalog_fingerprint=None, now=None):
        return self._shared().probe_health_decision(
            gateway, model, force=force,
            catalog_fingerprint=catalog_fingerprint, now=now,
            clock=self.clock if now is None else None)

    def record_probe_outcome(self, gateway, model, outcome, **kwargs):
        kwargs.setdefault("policy", self.policy)
        if "now" not in kwargs and "clock" not in kwargs:
            kwargs["clock"] = self.clock
        return self._shared().record_probe_outcome(
            gateway, model, outcome, **kwargs)

    def begin_tool_run(self, rec):
        return self._shared().begin_tool_run(rec)

    def finalize_tool_run(self, run_id, **result):
        return self._shared().finalize_tool_run(run_id, **result)

    def get_tool_run(self, run_id):
        return self._shared().get_tool_run(run_id)

    def list_recent_tool_runs(self, **filters):
        return self._shared().list_recent_tool_runs(**filters)

    def trace_timeline(self, *, session_id, limit=200,
                       turn_id_prefix=None):
        db = self._shared()
        filters = {
            "session_id": session_id,
            "limit": limit,
            "turn_id_prefix": turn_id_prefix,
        }
        return {
            "requests": db.list_recent_traces(**filters),
            "tools": db.list_recent_tool_runs(**filters),
        }

    def usage_summary(self, **filters):
        return self._shared().request_usage_summary(**filters)

    def diagnostics(self, *, read_only=False):
        if read_only:
            if not self.path.is_file():
                return {
                    "path": str(self.path), "initialized": False,
                    "integrity": [], "foreign_key_violations": [],
                    "counts": {
                        "requests": 0, "tool_runs": 0, "model_health": 0,
                    },
                }
            uri = self.path.resolve().as_uri() + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=self.timeout)
            try:
                tables = {
                    str(row[0]) for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")
                }
                counts = {
                    name: (
                        int(con.execute(
                            f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                        if name in tables else 0)
                    for name in ("requests", "tool_runs", "model_health")
                }
                return {
                    "path": str(self.path), "initialized": True,
                    "integrity": [str(row[0]) for row in con.execute(
                        "PRAGMA integrity_check").fetchall()],
                    "foreign_key_violations": [tuple(row) for row in
                        con.execute("PRAGMA foreign_key_check").fetchall()],
                    "counts": counts,
                }
            finally:
                con.close()
        db = self._shared()
        return {
            "path": str(self.path),
            "initialized": True,
            "integrity": db.integrity_check(),
            "foreign_key_violations": db.foreign_key_violations(),
            "counts": db.metrics_counts(),
        }
