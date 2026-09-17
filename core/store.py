"""会话、兼容用量与 M5 运行指标的持久化层。

各类文件职责不同：

  sessions/<id>.json   每个会话一个文件，可并存多个（原来只有一个 last.json
                       槽位，每轮覆盖，等于没有多会话）；迁移期仍是会话权威源。
  usage.jsonl          成功调用兼容流水，供旧统计脚本继续使用。
  metrics.sqlite3      M5 provider attempt、tool run 和 model health 的权威源；
                       包含 retry/error/unknown measurement。
  stats-cache.json     从 usage.jsonl 算出来的汇总，形状对齐 Claude Code 的
                       ~/.claude/stats-cache.json，方便同一套工具消费。
                       派生物，删了能重算。
"""
from . import paths
from . import wincompat
import atexit
import json
import os
import re
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import goals, model_health, plans as task_plans, state as state_db

HOME = paths.state_home()

# 目录布局照 ~/.claude 和 ~/.codex 的形状来 —— 那两个是跑了很久的成熟产品，
# 它们分出来的每个目录都对应一类真实需求。这里只建**有实际用途**的，
# 不摆空架子。
SESSIONS = HOME / "sessions"            # 活跃会话，每个一个 json
ARCHIVE = HOME / "archived_sessions"    # 旧布局保留；M4 archive 只改 status
PLANS = HOME / "plans"                  # plan mode 产出的计划
BACKUPS = HOME / "backups"              # 改文件前的原文备份
LOGS = HOME / "logs"                    # 运行日志（报错、探测记录）
CACHE = HOME / "cache"                  # 可重建的中间产物
PROJECTS = HOME / "projects"            # 按工作目录分组的会话索引
ARTIFACTS = HOME / "artifacts"          # managed task 的完整 stdout/stderr
AGENT_RUNS = HOME / "agent-runs"        # child agent 独立 transcript/inbox/runtime
SESSION_LOCKS = HOME / "session-locks"  # session metadata/transcript 的统一互斥锁
SESSION_LEASES = HOME / "session-leases"  # 进程生命周期写租约（JSON 权威阶段）
SESSION_INDEX = HOME / "session-index.json"  # 可重建 metadata catalog
SESSION_INDEX_DIRTY = HOME / "session-index.dirty"  # canonical/index 中断恢复标记

USAGE_LOG = HOME / "usage.jsonl"
STATS_CACHE = HOME / "stats-cache.json"
HISTORY = HOME / "history.jsonl"        # 输入历史（带 cwd 和时间戳）
INSTALL_ID = HOME / "installation_id"
USER_MD = HOME / paths.user_md_name()          # 用户级全局指令，类比 ~/.claude/CLAUDE.md
STATS_VERSION = 1
STATE_DB_NAME = "state.sqlite3"
METRICS_DB_NAME = "metrics.sqlite3"
SESSION_INDEX_VERSION = 1
SESSION_LEASE_STALE_SECONDS = 30.0

_SHADOW = None
_SHADOW_PATH = None
_SHADOW_OVERRIDE = None
_SHADOW_BROKEN = None
_SHADOW_LOCK = threading.RLock()

# M5 metrics are independent of the opt-in session shadow.  They are always
# enabled but deliberately use a separate durable path: creating state.sqlite3
# early would violate the crash-safe JSON→SQLite migration's "new target"
# precondition and permanently block that later migration.
_METRICS = None
_METRICS_PATH = None
_METRICS_POLICY = model_health.DEFAULT_POLICY
_METRICS_LOCK = threading.RLock()

_REPO_CACHE = {}
_REPO_CACHE_LOCK = threading.RLock()
_REPO_CACHE_TTL = 2.0

_DIRS = (
    SESSIONS, ARCHIVE, PLANS, BACKUPS, LOGS, CACHE, PROJECTS, ARTIFACTS,
    AGENT_RUNS, SESSION_LOCKS, SESSION_LEASES)

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")
_SESSION_STATUSES = {"active", "archived"}


class SessionStoreError(RuntimeError):
    """Session authority or catalog state is unsafe to use."""


class SessionCorruptError(SessionStoreError):
    """A canonical session record is malformed; never overwrite it blindly."""


class SessionBusyError(SessionStoreError):
    """Another live process owns the session write lease."""

    def __init__(self, session_id, lease=None):
        self.session_id = str(session_id)
        self.lease = dict(lease or {})
        if self.lease.get("_corrupt"):
            path = self.lease.get("_path") or _lease_path(self.session_id)
            super().__init__(
                f"会话 {self.session_id} 的 lease 损坏，已 fail-closed：{path}；"
                f"确认没有其他 Zylab 在使用后运行 "
                f"/sessions repair-lease {self.session_id}")
            return
        owner = self.lease.get("owner_id") or "unknown"
        pid = self.lease.get("pid") or "?"
        host = self.lease.get("hostname") or "?"
        super().__init__(
            f"会话 {self.session_id} 正在被 {host} pid {pid} 使用 "
            f"(owner {str(owner)[:12]})")


def _private(path, mode=0o600):
    """尽力收紧本地状态权限；符号链接不跟随。"""
    try:
        target = Path(path)
        if not target.is_symlink():
            os.chmod(target, mode)
    except OSError:
        pass


@contextmanager
def _exclusive_file_lock(target):
    """Serialize cross-process updates associated with *target*."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = target.with_name(target.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        wincompat.fchmod(fd, 0o600)
        wincompat.flock(fd, wincompat.LOCK_EX)
        yield
    finally:
        try:
            wincompat.flock(fd, wincompat.LOCK_UN)
        finally:
            os.close(fd)


def _fsync_directory(path):
    """Persist directory-entry changes or raise when durability is unavailable."""
    if wincompat.IS_WINDOWS:
        # Windows 没有目录 fsync 的对应物；NTFS 元数据日志已保证 rename 的持久
        # 顺序足够强（FlushFileBuffers 语义由 os.replace 内部的
        # MoveFileEx(REPLACE_EXISTING) 近似）。接受弱化：崩溃后丢最后一次
        # rename 的概率与 POSIX 不同，但 marker/canonical 双写顺序仍成立。
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(Path(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_text(path, text):
    """Write a private text file via a unique same-directory temporary."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    tmp = Path(tmp_name)
    try:
        wincompat.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _private(path)
        # fsync(file) makes the bytes durable; fsync(parent) makes the rename
        # durable.  The session dirty marker relies on both guarantees to order
        # marker -> canonical -> index across a node/storage crash.
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _validate_session_id(value):
    session_id = str(value or "").strip()
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError(f"无效会话 id 或前缀：{session_id!r}")
    return session_id


def _session_sibling(path):
    """Keep session support files beside a patched/test SESSIONS directory."""
    # Production and normal fixtures use <home>/sessions.  A few older tests
    # patch SESSIONS directly to a temporary directory; putting siblings in
    # that directory's parent would leak shared /tmp/session-index.json state.
    root = (
        SESSIONS.parent
        if SESSIONS.name == "sessions"
        else SESSIONS / f"{paths.project_dirname()}-support"
    )
    return root / Path(path).name


def _session_lock_target(session_id):
    return _session_sibling(SESSION_LOCKS) / _validate_session_id(session_id)


def _lease_path(session_id):
    return (
        _session_sibling(SESSION_LEASES)
        / f"{_validate_session_id(session_id)}.json"
    )


def _index_path():
    return _session_sibling(SESSION_INDEX)


def _index_dirty_path():
    return _session_sibling(SESSION_INDEX_DIRTY)


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii").strip()
    except OSError:
        return ""


def _proc_start(pid):
    """Linux process start ticks; distinguishes a reused pid in this boot."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        return tail[19]
    except (OSError, TypeError, ValueError, IndexError):
        return ""


def _timestamp_age(value, now=None):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(
            0.0,
            (current - stamp.astimezone(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError):
        return float("inf")


def _corrupt_lease_record(session_id, path):
    return {
        "session_id": session_id,
        "owner_id": "corrupt",
        "pid": "?",
        "hostname": "?",
        "heartbeat_at": "",
        "_corrupt": True,
        "_path": str(path),
    }


def _normalize_lease_record(session_id, value, path):
    """Validate ownership data; shape corruption is fail-closed too."""
    if not isinstance(value, dict):
        return _corrupt_lease_record(session_id, path)
    try:
        pid = int(value.get("pid"))
    except (TypeError, ValueError):
        return _corrupt_lease_record(session_id, path)
    required_strings = (
        "owner_id", "hostname", "acquired_at", "heartbeat_at")
    if (value.get("version") != 1
            or value.get("session_id") != session_id
            or pid <= 0
            or any(not isinstance(value.get(name), str)
                   or not value.get(name) for name in required_strings)
            or _timestamp_age(value.get("heartbeat_at")) == float("inf")):
        return _corrupt_lease_record(session_id, path)
    record = dict(value)
    record["pid"] = pid
    return record


def _read_lease_unlocked(session_id):
    path = _lease_path(session_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        # A malformed ownership record is not safe to take over implicitly.
        return _corrupt_lease_record(session_id, path)
    return _normalize_lease_record(session_id, value, path)


def _lease_is_live(record, *, stale_after=SESSION_LEASE_STALE_SECONDS):
    if not record or record.get("_corrupt"):
        return bool(record)
    host = str(record.get("hostname") or "")
    if host == socket.gethostname():
        saved_boot = str(record.get("boot_id") or "")
        current_boot = _boot_id()
        if saved_boot and current_boot and saved_boot != current_boot:
            return False
        pid = record.get("pid")
        saved_start = str(record.get("proc_start") or "")
        current_start = _proc_start(pid)
        if saved_start:
            return bool(current_start and current_start == saved_start)
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, TypeError, ValueError):
            return False
    return _timestamp_age(record.get("heartbeat_at")) <= float(stale_after)


def acquire_session_lease(session_id, owner_id=None, *,
                          stale_after=SESSION_LEASE_STALE_SECONDS):
    """Acquire the canonical process-lifetime write lease for a session."""
    session_id = _validate_session_id(session_id)
    owner_id = str(owner_id or uuid.uuid4().hex)
    target = _session_lock_target(session_id)
    path = _lease_path(session_id)
    now = _now()
    with _exclusive_file_lock(target):
        current = _read_lease_unlocked(session_id)
        if (current and current.get("owner_id") != owner_id
                and _lease_is_live(current, stale_after=stale_after)):
            raise SessionBusyError(session_id, current)
        acquired = (
            current.get("acquired_at")
            if current and current.get("owner_id") == owner_id
            else now
        )
        record = {
            "version": 1,
            "session_id": session_id,
            "owner_id": owner_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "boot_id": _boot_id(),
            "proc_start": _proc_start(os.getpid()),
            "acquired_at": acquired or now,
            "heartbeat_at": now,
        }
        _atomic_write_text(
            path,
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        )
    return record


def heartbeat_session_lease(session_id, owner_id):
    session_id = _validate_session_id(session_id)
    owner_id = str(owner_id or "")
    with _exclusive_file_lock(_session_lock_target(session_id)):
        current = _read_lease_unlocked(session_id)
        if (not current or current.get("owner_id") != owner_id
                or not _lease_is_live(current)):
            raise SessionBusyError(session_id, current)
        current["heartbeat_at"] = _now()
        _atomic_write_text(
            _lease_path(session_id),
            json.dumps(current, ensure_ascii=False, separators=(",", ":")),
        )
    return current


def release_session_lease(session_id, owner_id):
    session_id = _validate_session_id(session_id)
    owner_id = str(owner_id or "")
    with _exclusive_file_lock(_session_lock_target(session_id)):
        current = _read_lease_unlocked(session_id)
        if current is None:
            return False
        if current.get("owner_id") != owner_id:
            raise SessionBusyError(session_id, current)
        try:
            _lease_path(session_id).unlink()
        except FileNotFoundError:
            return False
    return True


def session_lease(session_id):
    """Return ownership metadata plus a computed live flag."""
    session_id = _validate_session_id(session_id)
    with _exclusive_file_lock(_session_lock_target(session_id)):
        current = _read_lease_unlocked(session_id)
    if not current:
        return None
    value = dict(current)
    value["live"] = _lease_is_live(current)
    return value


def active_session_leases():
    """Read only the usually-small lease directory, never session transcripts."""
    root = _session_sibling(SESSION_LEASES)
    if not root.is_dir():
        return {}
    active = {}
    for path in root.glob("*.json"):
        try:
            session_id = _validate_session_id(path.stem)
        except ValueError:
            continue
        record = _read_lease_unlocked(session_id)
        if record is not None and _lease_is_live(record):
            active[session_id] = record
    return active


def quarantine_corrupt_session_lease(session_id):
    """Explicitly move a corrupt lease aside; never remove a valid lease."""
    session_id = _validate_session_id(session_id)
    target = _session_lock_target(session_id)
    path = _lease_path(session_id)
    with _exclusive_file_lock(target):
        current = _read_lease_unlocked(session_id)
        if current is None:
            raise FileNotFoundError(f"会话 {session_id} 没有 lease")
        if not current.get("_corrupt"):
            if _lease_is_live(current):
                raise SessionBusyError(session_id, current)
            raise SessionStoreError(
                f"会话 {session_id} 的 lease 结构有效；拒绝按损坏记录修复")
        quarantine = path.parent / "quarantine"
        quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(quarantine, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        destination = quarantine / f"{session_id}.{stamp}.json"
        os.replace(path, destination)
        _private(destination)
    return destination


def _assert_session_write_owner_unlocked(session_id, owner_id=None):
    current = _read_lease_unlocked(session_id)
    if not current or not _lease_is_live(current):
        if owner_id:
            raise SessionBusyError(session_id, current)
        return
    if current.get("owner_id") != str(owner_id or ""):
        raise SessionBusyError(session_id, current)


def ensure_home():
    """首次运行时把家目录建起来，并放一份可编辑的用户级指令模板。"""
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(HOME, 0o700)
    for d in _DIRS:
        d.mkdir(exist_ok=True, mode=0o700)
        _private(d, 0o700)
    with _exclusive_file_lock(INSTALL_ID):
        if not INSTALL_ID.is_file():
            _atomic_write_text(INSTALL_ID, uuid.uuid4().hex)
        _private(INSTALL_ID)
    with _exclusive_file_lock(USER_MD):
        if not USER_MD.is_file():
            _atomic_write_text(
                USER_MD,
                f"# {paths.user_md_name()}\n\n"
                "这里写**跨项目**都适用的指令，每次会话都会加载。\n"
                "项目专属的规则请写在项目里的 CLAUDE.md / AGENTS.md。\n\n"
                "例如：\n"
                "- 改完代码一定要跑测试再说完成。\n"
                "- 回答用中文，代码和报错保持原文。\n")
        _private(USER_MD)
    return HOME


def install_id():
    try:
        return INSTALL_ID.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def log(event, **fields):
    """写一行运行日志。失败静默 —— 日志不该影响主流程。"""
    try:
        LOGS.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(LOGS, 0o700)
        rec = {"ts": _now(), "event": event, **fields}
        path = LOGS / f"{_now()[:10]}.jsonl"
        with _exclusive_file_lock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            _private(path)
    except OSError:
        pass


_TRUE_VALUES = {"1", "true", "yes", "on"}


def shadow_enabled():
    """SQLite 影子写入默认关闭；只接受显式配置或环境变量。"""
    if _SHADOW_OVERRIDE is not None:
        return _SHADOW_OVERRIDE
    return paths.env_get("STATE_SHADOW", "").strip().lower() in _TRUE_VALUES


def _shadow_target():
    if _SHADOW_PATH is not None:
        return _SHADOW_PATH
    configured = paths.env_get("STATE_DB", "").strip()
    return Path(configured) if configured else HOME / STATE_DB_NAME


def _metrics_target():
    if _METRICS_PATH is not None:
        return _METRICS_PATH
    configured = paths.env_get("METRICS_DB", "").strip()
    return Path(configured) if configured else HOME / METRICS_DB_NAME


def legacy_tool_artifact_rows(session_id):
    """Read historical tool_call_id→artifact paths without opening a writer."""
    target = _metrics_target()
    if not target.is_file():
        return []
    uri = target.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(
            uri, uri=True, timeout=0.25)
        connection.row_factory = sqlite3.Row
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'tool_runs'"
            ).fetchone()
            if table is None:
                return []
            rows = connection.execute(
                """
                SELECT id, tool_call_id, session_id, name, status,
                       exit_code, started_at, ended_at,
                       stdout_path, stderr_path
                FROM tool_runs
                WHERE session_id = ?
                  AND tool_call_id IS NOT NULL
                  AND (stdout_path IS NOT NULL OR stderr_path IS NOT NULL)
                ORDER BY started_at, id
                LIMIT 10000
                """,
                (str(session_id or ""),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SessionStoreError(
            f"legacy artifact metrics 只读查询失败：{exc}") from exc


def metrics_facade():
    """Return the fail-closed, always-on request/tool metrics facade.

    Unlike the session shadow, failures are intentionally not swallowed: a
    provider/tool side effect must not start when its trace intent cannot be
    persisted. The facade keeps one checked connection per calling thread;
    connections are never shared across threads or terminals.
    """
    global _METRICS
    target = _metrics_target()
    with _METRICS_LOCK:
        if (_METRICS is None or _METRICS.path != target
                or _METRICS.policy != _METRICS_POLICY):
            _METRICS = state_db.MetricsFacade(
                target, policy=_METRICS_POLICY)
        return _METRICS


def configure_metrics_policy(policy):
    """Install the effective per-process policy without opening the database."""
    if not isinstance(policy, model_health.ModelHealthPolicy):
        raise TypeError("metrics policy 必须是 ModelHealthPolicy")
    global _METRICS, _METRICS_POLICY
    with _METRICS_LOCK:
        if policy != _METRICS_POLICY:
            if _METRICS is not None:
                _METRICS.close()
            _METRICS_POLICY = policy
            _METRICS = None


def configure_metrics(path=None):
    """Override the always-on metrics target (primarily tests/embedding)."""
    global _METRICS, _METRICS_PATH
    with _METRICS_LOCK:
        if _METRICS is not None:
            _METRICS.close()
        _METRICS = None
        _METRICS_PATH = Path(path) if path is not None else None
    return metrics_facade()


def reset_metrics_configuration():
    global _METRICS, _METRICS_PATH, _METRICS_POLICY
    with _METRICS_LOCK:
        if _METRICS is not None:
            _METRICS.close()
        _METRICS = None
        _METRICS_PATH = None
        _METRICS_POLICY = model_health.DEFAULT_POLICY


def close_shadow():
    """关闭当前进程的影子 writer；JSON 主存储不受影响。"""
    global _SHADOW
    with _SHADOW_LOCK:
        if _SHADOW is not None:
            _SHADOW.close()
            _SHADOW = None


def configure_shadow(path=None, *, enabled=True):
    """显式配置影子 writer，主要供 CLI opt-in 和测试使用。"""
    global _SHADOW_PATH, _SHADOW_OVERRIDE, _SHADOW_BROKEN
    with _SHADOW_LOCK:
        close_shadow()
        _SHADOW_PATH = Path(path) if path is not None else None
        _SHADOW_OVERRIDE = bool(enabled)
        _SHADOW_BROKEN = None


def reset_shadow_configuration():
    """清除进程内 override，恢复环境变量驱动的默认行为。"""
    global _SHADOW_PATH, _SHADOW_OVERRIDE, _SHADOW_BROKEN
    with _SHADOW_LOCK:
        close_shadow()
        _SHADOW_PATH = None
        _SHADOW_OVERRIDE = None
        _SHADOW_BROKEN = None


def _get_shadow():
    global _SHADOW
    if not shadow_enabled():
        return None
    target = _shadow_target()
    if _SHADOW is not None and _SHADOW.path != target:
        close_shadow()
    if _SHADOW is None:
        _SHADOW = state_db.StateStore(target)
    return _SHADOW


def _shadow_try(operation, callback):
    """影子写入 fail-open：任何错误只禁用本进程 shadow，不碰 JSON 主流程。"""
    global _SHADOW, _SHADOW_BROKEN
    if not shadow_enabled() or _SHADOW_BROKEN is not None:
        return None
    try:
        with _SHADOW_LOCK:
            db = _get_shadow()
            return callback(db) if db is not None else None
    except Exception as exc:
        with _SHADOW_LOCK:
            _SHADOW_BROKEN = f"{type(exc).__name__}: {exc}"
            if _SHADOW is not None:
                try:
                    _SHADOW.close()
                except Exception:
                    pass
                _SHADOW = None
        log("state_shadow_error", operation=operation, error=_SHADOW_BROKEN)
        return None


def _agent_record(agent, title=None):
    started = getattr(agent, "started", None)
    if started:
        created = datetime.fromtimestamp(started, timezone.utc).isoformat(
            timespec="seconds")
    else:
        created = _now()
    messages = getattr(agent, "messages", None) or []
    return {
        "id": agent.session_id,
        "title": title if title is not None else _auto_title(messages),
        "model": getattr(agent, "model", ""),
        "gateway": getattr(agent, "gateway", ""),
        "cwd": os.getcwd(),
        "created": created,
        "updated": _now(),
        "last_context_tokens": getattr(agent, "last_total", 0),
        "tokens_in": getattr(agent, "tokens_in", 0),
        "tokens_out": getattr(agent, "tokens_out", 0),
    }


def shadow_ensure_agent(agent):
    """首次启用时把当前 JSON 消息前缀引入 journal，之后只补 metadata。"""
    def write(db):
        rec = _agent_record(agent)
        return db.bootstrap_session(rec, list(getattr(agent, "messages", [])))

    return _shadow_try("bootstrap_session", write)


def controller_journal(agent):
    """给主线程 SessionController 返回 opt-in shadow writer。

    默认关闭时返回 None；调用方应使用 MemoryJournal。返回的 SQLite connection
    只能继续由当前 controller 主线程访问。
    """
    if not shadow_enabled():
        return None
    shadow_ensure_agent(agent)
    with _SHADOW_LOCK:
        if _SHADOW_BROKEN is not None:
            return None
        return _SHADOW


def shadow_events(agent, events):
    """原子追加一批 runtime event。"""
    def write(db):
        db.upsert_session(_agent_record(agent))
        return db.append_events(agent.session_id, events)

    return _shadow_try("append_events", write)


def shadow_event(agent, kind, payload=None, **metadata):
    event = {"kind": kind, "payload": payload or {}, **metadata}
    result = shadow_events(agent, [event])
    return result[0] if result else None


def _shadow_sync_session(rec):
    def write(db):
        result = db.bootstrap_session(rec, rec.get("messages") or [])
        db.replace_session_grants(
            rec["id"],
            [
                {"matcher": matcher, "decision": "allow",
                 "source": "user"}
                for matcher in rec.get("session_grants") or []
            ],
        )
        return result

    return _shadow_try("save_session", write)


def _shadow_fork_head(source_session_id):
    def read(db):
        row = db.get_session(source_session_id)
        return int(row["last_seq"]) if row and row["last_seq"] else None

    return _shadow_try("fork_head", read)


def _shadow_fork_seq_for_messages(source_session_id, messages):
    """Return only a SQLite cutoff proven to match canonical messages."""
    return _shadow_try(
        "fork_head",
        lambda db: db.fork_seq_for_messages(source_session_id, messages),
    )


def _shadow_fork_session(source_session_id, rec):
    def write(db):
        fork_seq = rec.get("fork_seq")
        if fork_seq:
            result = db.fork_session(source_session_id, int(fork_seq), rec)
        else:
            result = db.bootstrap_session(rec, rec.get("messages") or [])
        db.replace_session_grants(rec["id"], [])
        return result

    return _shadow_try("fork_session", write)


def _shadow_record_usage(rec):
    digest = state_db.sha256_bytes(
        state_db.canonical_json(rec).encode("utf-8"))
    source_key = "runtime-usage:" + digest
    return _shadow_try(
        "append_usage", lambda db: db.record_usage(rec, source_key=source_key))


def shadow_status():
    """返回状态但不为查询而创建数据库。"""
    with _SHADOW_LOCK:
        return {
            "enabled": shadow_enabled(),
            "path": str(_shadow_target()),
            "active": _SHADOW is not None,
            "journal_mode": getattr(_SHADOW, "journal_mode", None),
            "broken": _SHADOW_BROKEN,
        }


atexit.register(close_shadow)


def backup_file(path, *, content=None):
    """改文件前存一份原文。返回备份路径；文件不存在则返回 None。

    Claude Code 有 file-history/，改错了能找回来。zylab 原来的
    write_file 是直接覆盖、无备份 —— 模型改错一个大文件就没了。

    ``content`` 供 prepared-write 复用已经批准的 before-image，避免为了
    legacy backup 再读一次目标并制造另一份竞态快照。
    """
    src = Path(path)
    if content is None and not src.is_file():
        return None
    try:
        data = src.read_bytes() if content is None else bytes(content)
        BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(BACKUPS, 0o700)
        # 时间戳必须带亚秒：同一秒内连改两次会互相覆盖，中间那版就没了。
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        flat = str(src).lstrip("/").replace("/", "%")[-120:]
        dst = BACKUPS / f"{stamp}__{flat}"
        dst.write_bytes(data)
        _private(dst)
        return dst
    except (OSError, TypeError, ValueError):
        return None


def prune_backups(keep_days=14, max_files=2000):
    """备份会无限增长，定期清。按时间和数量双重上限。"""
    try:
        files = sorted(BACKUPS.glob("*"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return 0
    import time as _t
    cutoff = _t.time() - keep_days * 86400
    removed = 0
    for f in files:
        try:
            if f.stat().st_mtime < cutoff or len(files) - removed > max_files:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(obj):
    """json.dumps 的 default 回调：把不可序列化对象降级成 dict。

    主要针对 tools.PreparedArguments（Mapping 子类，json 不认），
    以及任何实现了 to_jsonable() 的对象。用鸭子类型，避免 import tools
    造成循环依赖。
    """
    to_jsonable = getattr(obj, "to_jsonable", None)
    if callable(to_jsonable):
        return to_jsonable()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def append_history(line, cwd=None):
    """追加一条输入历史到 history.jsonl（带 cwd 和时间戳）。

    readline 的 ~/.zylab/history 只在进程正常退出时由 atexit 落盘，
    进程被 kill 就丢整段会话的输入。这里每条输入立即 append，
    崩溃也最多丢当前这一行。写历史不该弄崩会话，异常一律吞掉。
    """
    try:
        HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(HOME, 0o700)
        rec = {"ts": _now(), "cwd": cwd or os.getcwd(), "input": line}
        with _exclusive_file_lock(HISTORY):
            with open(HISTORY, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            _private(HISTORY)
    except OSError:
        pass


def read_history(limit=200):
    """读取最近的 canonical 输入历史；损坏半行不影响其他记录。"""
    if not HISTORY.is_file():
        return []
    with _exclusive_file_lock(HISTORY):
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-max(0, int(limit)):]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("input"), str):
            out.append(record)
    return out


def new_id():
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------------ 会话

def repo_context(cwd=None, *, refresh=False):
    """Return cwd/repo/branch/dirty metadata with a short process-local cache."""
    raw = Path(cwd or os.getcwd()).expanduser()
    absolute = os.path.abspath(str(raw))
    with _REPO_CACHE_LOCK:
        cached = _REPO_CACHE.get(absolute)
        if (not refresh and cached is not None
                and time.monotonic() - cached[0] < _REPO_CACHE_TTL):
            return dict(cached[1])
    result = {
        "cwd": absolute,
        "repo_root": "",
        "git_branch": "",
        "git_dirty": None,
        "git_staged": 0,
        "git_unstaged": 0,
        "git_untracked": 0,
    }
    if not os.path.isdir(absolute):
        return result
    try:
        probe = subprocess.run(
            [
                "git", "-C", absolute, "rev-parse",
                "--show-toplevel", "--abbrev-ref", "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return result
    lines = probe.stdout.splitlines()
    if probe.returncode == 0 and len(lines) >= 2:
        result["repo_root"] = os.path.abspath(lines[0].strip())
        result["git_branch"] = lines[1].strip()
        try:
            status = subprocess.run(
                [
                    "git", "-C", result["repo_root"], "status",
                    "--porcelain=v1", "--untracked-files=normal",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            status = None
        if status is not None and status.returncode == 0:
            rows = [line for line in status.stdout.splitlines() if line]
            result["git_dirty"] = bool(rows)
            result["git_untracked"] = sum(
                line.startswith("??") for line in rows)
            result["git_staged"] = sum(
                not line.startswith("??") and line[:1] not in ("", " ")
                for line in rows)
            result["git_unstaged"] = sum(
                not line.startswith("??") and len(line) > 1
                and line[1] != " " for line in rows)
    with _REPO_CACHE_LOCK:
        _REPO_CACHE[absolute] = (time.monotonic(), dict(result))
    return result


def _normalize_session_record(path, raw):
    """Validate canonical JSON without discarding unknown future metadata."""
    path = Path(path)
    if path.is_symlink():
        raise SessionCorruptError(f"会话文件不能是符号链接：{path}")
    if not isinstance(raw, dict):
        raise SessionCorruptError(f"会话记录不是 object：{path}")
    value = dict(raw)
    session_id = value.get("id") or path.stem
    try:
        session_id = _validate_session_id(session_id)
    except ValueError as exc:
        raise SessionCorruptError(f"{path}: {exc}") from None
    if session_id != path.stem:
        raise SessionCorruptError(
            f"会话文件名与记录 id 不一致：{path.stem!r} != {session_id!r}")
    messages = value.get("messages")
    if not isinstance(messages, list):
        raise SessionCorruptError(f"会话 messages 不是 array：{path}")
    status = str(value.get("status") or "active")
    if status not in _SESSION_STATUSES:
        raise SessionCorruptError(f"会话 status 无效：{status!r} ({path})")
    value["id"] = session_id
    value["status"] = status
    value.setdefault("title", "(无标题)")
    value.setdefault("cwd", "")
    value.setdefault("created", "")
    value.setdefault("updated", "")
    grants = value.setdefault("session_grants", [])
    if (not isinstance(grants, list)
            or any(not isinstance(item, str) or not item.strip()
                   for item in grants)):
        raise SessionCorruptError(
            f"会话 session_grants 不是非空字符串数组：{path}")
    value["session_grants"] = sorted(set(grants))
    try:
        value["task_plan"] = task_plans.normalize_record(
            value.get("task_plan"))
    except task_plans.PlanError as exc:
        raise SessionCorruptError(
            f"会话 task_plan 无效：{exc} ({path})") from None
    try:
        value["goal"] = goals.normalize_record(value.get("goal"))
    except goals.GoalError as exc:
        raise SessionCorruptError(
            f"会话 goal 无效：{exc} ({path})") from None
    return value


def _read_session_path(path):
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionCorruptError(
            f"无法读取会话 {path}: {type(exc).__name__}: {exc}") from None
    return _normalize_session_record(path, raw)


def _safe_nonnegative_int(value, field):
    """Normalize legacy JSON scalars without letting one row break a rebuild."""
    if value is None or value == "":
        return 0, None
    if isinstance(value, bool):
        return 0, f"{field} 不是非负整数；catalog 已降级为 0 (type=bool)"
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0, (
            f"{field} 不是非负整数；catalog 已降级为 0 "
            f"(type={type(value).__name__})"
        )
    return max(0, number), None


def _safe_optional_fork_int(value, field, *, minimum=0):
    """Normalize legacy fork metadata without emitting an invalid catalog."""
    if value is None or value == "":
        return None, None
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None, (
            f"{field} 不是整数；catalog 已降级为 null "
            f"(type={type(value).__name__})"
        )
    if number < minimum:
        return None, (
            f"{field} 小于 {minimum}；catalog 已降级为 null"
        )
    return number, None


def _summary_from_record(record, *, environment=None, issues=None):
    messages = record.get("messages") or []
    env = dict(environment or {})
    cwd = str(record.get("cwd") or env.get("cwd") or "")
    repo_root = str(record.get("repo_root") or env.get("repo_root") or "")
    git_branch = str(record.get("git_branch") or env.get("git_branch") or "")
    last_context_tokens, issue = _safe_nonnegative_int(
        record.get("last_context_tokens"), "last_context_tokens")
    if issue is not None and issues is not None:
        issues.append(issue)
    fork_seq, issue = _safe_optional_fork_int(
        record.get("fork_seq"), "fork_seq")
    if issue is not None and issues is not None:
        issues.append(issue)
    fork_message_count, issue = _safe_optional_fork_int(
        record.get("fork_message_count"), "fork_message_count", minimum=1)
    if issue is not None and issues is not None:
        issues.append(issue)
    return {
        "id": record["id"],
        "title": str(record.get("title") or "(无标题)"),
        "status": str(record.get("status") or "active"),
        "model": str(record.get("model") or ""),
        "gateway": str(record.get("gateway") or ""),
        "cwd": cwd,
        "repo_root": repo_root,
        "git_branch": git_branch,
        "created": str(record.get("created") or ""),
        "updated": str(record.get("updated") or ""),
        "count": max(0, len(messages) - 1),
        "last_context_tokens": last_context_tokens,
        "parent_session_id": str(
            record.get("parent_session_id") or ""),
        "fork_seq": fork_seq,
        "fork_message_count": fork_message_count,
    }


def _empty_session_index():
    return {
        "version": SESSION_INDEX_VERSION,
        "updated": _now(),
        "sessions": {},
        "errors": [],
    }


def _read_session_index_unlocked():
    path = _index_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionStoreError(
            f"session catalog 损坏：{type(exc).__name__}: {exc}") from None
    if (not isinstance(value, dict)
            or value.get("version") != SESSION_INDEX_VERSION
            or not isinstance(value.get("sessions"), dict)
            or not isinstance(value.get("errors", []), list)):
        raise SessionStoreError("session catalog version/shape 无效")
    string_fields = (
        "id", "title", "status", "model", "gateway", "cwd",
        "repo_root", "git_branch", "created", "updated",
        "parent_session_id",
    )
    for key, row in value["sessions"].items():
        try:
            valid_key = _validate_session_id(key)
        except ValueError as exc:
            raise SessionStoreError(
                f"session catalog key 无效：{exc}") from None
        if (not isinstance(row, dict)
                or row.get("id") != valid_key
                or row.get("status") not in _SESSION_STATUSES
                or any(not isinstance(row.get(name, ""), str)
                       for name in string_fields)
                or isinstance(row.get("count", 0), bool)
                or not isinstance(row.get("count", 0), int)
                or row.get("count", 0) < 0
                or isinstance(row.get("last_context_tokens", 0), bool)
                or not isinstance(row.get("last_context_tokens", 0), int)
                or row.get("last_context_tokens", 0) < 0
                or (row.get("fork_seq") is not None
                    and (isinstance(row.get("fork_seq"), bool)
                         or not isinstance(row.get("fork_seq"), int)
                         or row.get("fork_seq") < 0))
                or (row.get("fork_message_count") is not None
                    and (isinstance(row.get("fork_message_count"), bool)
                         or not isinstance(row.get("fork_message_count"), int)
                         or row.get("fork_message_count") < 1))):
            raise SessionStoreError(
                f"session catalog row 无效：{valid_key}")
    return value


def _write_session_index_unlocked(index):
    index = dict(index)
    index["version"] = SESSION_INDEX_VERSION
    index["updated"] = _now()
    _atomic_write_text(
        _index_path(),
        json.dumps(index, ensure_ascii=False, separators=(",", ":")),
    )


def _rebuild_session_index_unlocked():
    index = _empty_session_index()
    environments = {}
    if SESSIONS.is_dir():
        for path in SESSIONS.glob("*.json"):
            if path.name == "last.json" or path.is_symlink():
                continue
            try:
                record = _read_session_path(path)
                cwd = str(record.get("cwd") or "")
                environment = environments.get(cwd)
                if environment is None:
                    environment = repo_context(cwd) if cwd else {}
                    environments[cwd] = environment
                issues = []
                summary = _summary_from_record(
                    record, environment=environment, issues=issues)
                index["sessions"][record["id"]] = summary
                for issue in issues:
                    index["errors"].append({
                        "path": str(path),
                        "error": issue,
                        "kind": "summary_normalization",
                    })
            except (OSError, SessionStoreError, ValueError) as exc:
                index["errors"].append({
                    "path": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                })
    _write_session_index_unlocked(index)
    _clear_session_index_dirty_unlocked()
    return index


def rebuild_session_index():
    """Explicitly rebuild the derived catalog from canonical transcripts."""
    with _exclusive_file_lock(_index_path()):
        return _rebuild_session_index_unlocked()


def _session_index_unlocked():
    if _index_dirty_path().exists():
        return _rebuild_session_index_unlocked()
    try:
        index = _read_session_index_unlocked()
    except SessionStoreError:
        index = None
    return index if index is not None else _rebuild_session_index_unlocked()


def _mark_session_index_dirty_unlocked(session_id):
    _atomic_write_text(
        _index_dirty_path(),
        json.dumps({
            "version": 1,
            "session_id": str(session_id),
            "marked_at": _now(),
        }, ensure_ascii=False, separators=(",", ":")),
    )


def _clear_session_index_dirty_unlocked():
    path = _index_dirty_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return
    # The index replace is directory-synced before this unlink.  Syncing the
    # unlink makes the clean commit durable instead of rebuilding forever
    # after a restart that resurrects the marker directory entry.
    _fsync_directory(path.parent)


def _index_upsert_unlocked(index, record, *, environment=None):
    issues = []
    summary = _summary_from_record(
        record, environment=environment, issues=issues)
    index["sessions"][record["id"]] = summary
    canonical_path = str(SESSIONS / f"{record['id']}.json")
    index["errors"] = [
        entry for entry in index.get("errors", [])
        if not (isinstance(entry, dict)
                and entry.get("path") == canonical_path)
    ]
    for issue in issues:
        index["errors"].append({
            "path": canonical_path,
            "error": issue,
            "kind": "summary_normalization",
        })
    return summary


def _upsert_session_summary(record, *, environment=None):
    """Update only the derived catalog, with crash-rebuild signaling."""
    with _exclusive_file_lock(_index_path()):
        index = _session_index_unlocked()
        _mark_session_index_dirty_unlocked(record["id"])
        summary = _index_upsert_unlocked(
            index, record, environment=environment)
        _write_session_index_unlocked(index)
        _clear_session_index_dirty_unlocked()
    return summary


def _write_session_and_index_unlocked(path, record, *, environment=None):
    """Commit canonical JSON and its derived row with crash self-healing.

    The caller holds the per-session lock.  A dirty marker is durable before
    canonical replacement; if the process stops before catalog replacement,
    the next catalog read rebuilds from canonical transcripts.
    """
    with _exclusive_file_lock(_index_path()):
        index = _session_index_unlocked()
        _mark_session_index_dirty_unlocked(record["id"])
        _atomic_write_text(
            path,
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        )
        summary = _index_upsert_unlocked(
            index, record, environment=environment)
        _write_session_index_unlocked(index)
        _clear_session_index_dirty_unlocked()
    return summary


def _resolve_session_id(identifier, *, include_archived=False):
    identifier = _validate_session_id(identifier)
    with _exclusive_file_lock(_index_path()):
        index = _session_index_unlocked()
        rows = index["sessions"]
        exact = rows.get(identifier)
        if exact is not None:
            if include_archived or exact.get("status") == "active":
                return identifier
            raise FileNotFoundError(f"会话 {identifier} 已归档，请先 unarchive")
        hits = [
            session_id for session_id, row in rows.items()
            if session_id.startswith(identifier)
            and (include_archived or row.get("status") == "active")
        ]
    if len(hits) == 1:
        return _validate_session_id(hits[0])
    if len(hits) > 1:
        raise ValueError(
            f"前缀 {identifier} 匹配到 {len(hits)} 个会话，请给更长的前缀")
    raise FileNotFoundError(f"没有会话 {identifier}")


def resolve_session_id(identifier, *, include_archived=False):
    """Public metadata-only exact/unique-prefix resolver."""
    return _resolve_session_id(
        identifier, include_archived=include_archived)


_UNSET = object()


def save_session(agent, title=None, *, owner_id=None, cwd=None,
                 plan_mode=None, session_grants=None, task_plan=None,
                 workflow_auto=None, memory_use=None,
                 memory_generate=None, goal_state=_UNSET):
    """Atomically merge runtime fields into a canonical session record.

    没有任何用户输入时不落盘：进程一启动就创建空会话文件，
    「打开看一眼就退出」会攒下一堆 0 条消息的 (空会话)。
    """
    normalized_goal = (
        _UNSET if goal_state is _UNSET
        else goals.normalize_record(goal_state))
    has_user_message = any(
        m.get("role") == "user" for m in getattr(agent, "messages", []))
    # An explicitly created goal is itself meaningful user state.  Persist it
    # even before the first model turn so a crash/restart cannot erase the
    # user's armed completion condition.  Ordinary empty sessions still stay
    # out of the inventory.
    # Keep the no-empty-chat invariant, but allow an explicit ``goal=None``
    # to clear a previously persisted goal before the first user turn.  The
    # latter matters for ``/goal clear`` after a user created a goal and then
    # changed their mind without sending a provider request.  A brand-new
    # empty session still remains out of the catalog.
    session_id_hint = str(getattr(agent, "session_id", "") or "")
    existing_path = (
        SESSIONS / f"{session_id_hint}.json"
        if session_id_hint else None)
    if (not has_user_message and normalized_goal is _UNSET):
        return None
    if (not has_user_message and normalized_goal is None
            and (existing_path is None or not existing_path.is_file())):
        return None
    ensure_home()
    session_id = _validate_session_id(agent.session_id)
    path = SESSIONS / f"{session_id}.json"
    with _exclusive_file_lock(_session_lock_target(session_id)):
        _assert_session_write_owner_unlocked(session_id, owner_id)
        prev = _read_session_path(path) if path.is_file() else {}
        selected_cwd = os.path.abspath(str(
            cwd or prev.get("cwd") or os.getcwd()))
        # Branch can change without cwd changing.  Refresh this small metadata
        # probe on save so Ctrl+B never filters on a permanently stale branch.
        environment = repo_context(selected_cwd)
        rec = dict(prev)
        title_goal = (
            goals.normalize_record(prev.get("goal"))
            if normalized_goal is _UNSET else normalized_goal)
        automatic_title = _auto_title(agent.messages)
        if automatic_title == "(空会话)" and title_goal is not None:
            objective = " ".join(title_goal["objective"].split())
            automatic_title = objective[:48] + (
                "…" if len(objective) > 48 else "")
        previous_title = str(prev.get("title") or "").strip()
        rec.update({
            "id": session_id,
            "title": (
                title if title is not None
                else (previous_title
                      if previous_title and previous_title != "(空会话)"
                      else automatic_title)
            ),
            "status": str(prev.get("status") or "active"),
            "model": agent.model,
            "gateway": getattr(agent, "gateway", ""),
            # Keep the legacy aggregate while persisting independent layers.
            # Old records can backfill all three from load_md.
            "load_md": bool(getattr(agent, "load_md", True)),
            "load_user_md": bool(getattr(
                agent, "load_user_md",
                getattr(agent, "load_md", True))),
            "load_project_md": bool(getattr(
                agent, "load_project_md",
                getattr(agent, "load_md", True))),
            "load_skills": bool(getattr(
                agent, "load_skills",
                getattr(agent, "load_md", True))),
            "cwd": selected_cwd,
            "repo_root": environment["repo_root"],
            "git_branch": environment["git_branch"],
            "created": prev.get("created") or _now(),
            "updated": _now(),
            "tokens_in": agent.tokens_in,
            "tokens_out": agent.tokens_out,
            "last_context_tokens": getattr(agent, "last_total", 0),
            "messages": agent.messages,
            "context": (
                agent.context_snapshot()
                if hasattr(agent, "context_snapshot") else None
            ),
        })
        if plan_mode is not None:
            rec["plan_mode"] = bool(plan_mode)
        if session_grants is not None:
            grants = [str(item).strip() for item in session_grants]
            if any(not item for item in grants):
                raise ValueError("session grant 不能为空")
            rec["session_grants"] = sorted(set(grants))
        if task_plan is not None:
            rec["task_plan"] = task_plans.normalize_record(task_plan)
        if workflow_auto is not None:
            rec["workflow_auto"] = bool(workflow_auto)
        if memory_use is not None:
            rec["memory_use"] = bool(memory_use)
        if memory_generate is not None:
            rec["memory_generate"] = bool(memory_generate)
        if normalized_goal is not _UNSET:
            rec["goal"] = normalized_goal
        _write_session_and_index_unlocked(
            path, rec, environment=environment)
    _shadow_sync_session(rec)
    return path


def _auto_title(messages, limit=48):
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            t = " ".join(m["content"].split())
            if t and not goals.is_round_prompt(m["content"]):
                return t[:limit] + ("…" if len(t) > limit else "")
    return "(空会话)"


def load_session(sid, *, include_archived=False):
    """Load one canonical transcript by safe id or unique prefix."""
    session_id = _resolve_session_id(
        sid, include_archived=include_archived)
    return _read_session_path(SESSIONS / f"{session_id}.json")


def load_session_preview(sid):
    """Read one selected transcript for preview without taking a write lease."""
    return load_session(sid, include_archived=True)


def fork_session(sid, *, target_id=None, message_count=None, fork_seq=None,
                 cwd=None, title=None, owner_id=None):
    """Create an immutable conversational branch from one canonical snapshot.

    ``message_count`` counts the stored message array including the system
    prefix.  It is deliberately distinct from ``fork_seq``: the JSON backend
    cannot invent a SQLite event sequence.  Callers may pass a real journal
    sequence when they have one; otherwise ``fork_seq`` stays ``None``.

    Grants, queued input and runtime/task state are not part of canonical
    messages and are intentionally not copied.
    """
    ensure_home()
    source_id = _resolve_session_id(sid, include_archived=True)
    with _exclusive_file_lock(_session_lock_target(source_id)):
        source = _read_session_path(SESSIONS / f"{source_id}.json")
        messages = source.get("messages") or []
        if not messages:
            raise SessionCorruptError(f"会话 {source_id} 没有 messages")
        if message_count is None:
            cutoff = len(messages)
        else:
            if type(message_count) is not int:
                raise ValueError("message_count 必须是整数")
            cutoff = message_count
        if cutoff < 1 or cutoff > len(messages):
            raise ValueError(
                f"message_count 超出范围：{cutoff}（有效 1..{len(messages)}）")
        if fork_seq is None and cutoff == len(messages):
            # Never pair a canonical snapshot with a merely contemporaneous
            # SQLite last_seq.  The journal may be ahead or behind JSON.
            fork_seq = _shadow_fork_seq_for_messages(source_id, messages)
    if fork_seq is not None:
        if type(fork_seq) is not int or fork_seq < 0:
            raise ValueError("fork_seq 必须是非负整数或 None")

    child_id = _validate_session_id(target_id or new_id())
    if child_id == source_id:
        raise ValueError("fork target 不能与 source 相同")
    selected_cwd = os.path.abspath(os.path.expanduser(str(
        cwd if cwd is not None else source.get("cwd") or os.getcwd())))
    environment = repo_context(selected_cwd)
    now = _now()
    source_title = str(source.get("title") or "(无标题)")
    child_title = " ".join(str(
        title if title is not None else f"{source_title} · branch").split())
    if not child_title:
        child_title = "(无标题) · branch"
    full_head = cutoff == len(messages)
    copied_messages = json.loads(json.dumps(
        messages[:cutoff], ensure_ascii=False, default=_jsonable))
    record = {
        "id": child_id,
        "title": child_title[:160],
        "status": "active",
        "model": str(source.get("model") or ""),
        "gateway": str(source.get("gateway") or ""),
        "cwd": selected_cwd,
        "repo_root": environment["repo_root"],
        "git_branch": environment["git_branch"],
        "created": now,
        "updated": now,
        "tokens_in": 0,
        "tokens_out": 0,
        "last_context_tokens": (
            source.get("last_context_tokens", 0) if full_head else 0),
        "messages": copied_messages,
        "context": source.get("context") if full_head else None,
        "plan_mode": bool(source.get("plan_mode", False)),
        "task_plan": (
            task_plans.normalize_record(source.get("task_plan"))
            if full_head else task_plans.empty()),
        "workflow_auto": bool(source.get("workflow_auto", False)),
        "goal": (
            goals.normalize_record(source.get("goal"))
            if full_head else goals.empty()),
        "parent_session_id": source_id,
        "fork_seq": fork_seq,
        "fork_message_count": cutoff,
        "session_grants": [],
    }
    target = SESSIONS / f"{child_id}.json"
    with _exclusive_file_lock(_session_lock_target(child_id)):
        _assert_session_write_owner_unlocked(child_id, owner_id)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"fork target 已存在：{child_id}")
        _write_session_and_index_unlocked(
            target, record, environment=environment)
    _shadow_fork_session(source_id, record)
    return dict(record)


def list_session_summaries(*, limit=30, offset=0, status="active",
                           query="", scope="all", branch_only=False,
                           cwd=None):
    """Query the metadata catalog without opening any transcript."""
    cwd = os.path.abspath(str(cwd or os.getcwd()))
    current = repo_context(cwd)
    with _exclusive_file_lock(_index_path()):
        index = _session_index_unlocked()
        all_rows = [dict(row) for row in index["sessions"].values()]

    by_id = {row["id"]: row for row in all_rows}
    lineage = {}

    def resolve_lineage(start):
        if start in lineage:
            return lineage[start]
        path = []
        local = {}
        current_id = start
        while (current_id in by_id and current_id not in lineage
               and current_id not in local):
            local[current_id] = len(path)
            path.append(current_id)
            parent_id = str(
                by_id[current_id].get("parent_session_id") or "")
            if not parent_id or parent_id not in by_id:
                lineage[current_id] = (current_id, 0, False)
                path.pop()
                break
            current_id = parent_id

        if current_id in lineage:
            root_id, depth, cycle = lineage[current_id]
        elif current_id in local:
            cycle_start = local[current_id]
            cycle_nodes = path[cycle_start:]
            root_id = min(cycle_nodes)
            depth, cycle = 0, True
            for node in cycle_nodes:
                lineage[node] = (root_id, 0, True)
            path = path[:cycle_start]
        else:
            # The terminal root was installed just above and removed from
            # ``path``; this branch is defensive for future catalog shapes.
            root_id, depth, cycle = start, 0, False

        for node in reversed(path):
            depth += 1
            lineage[node] = (root_id, depth, cycle)
        return lineage[start]

    for row in all_rows:
        root_id, depth, cycle = resolve_lineage(row["id"])
        row["branch_root_id"] = root_id
        row["branch_depth"] = depth
        row["branch_cycle"] = cycle
    rows = all_rows

    if status is not None:
        statuses = {status} if isinstance(status, str) else set(status)
        unknown = statuses - _SESSION_STATUSES
        if unknown:
            raise ValueError(f"未知 session status：{sorted(unknown)}")
        rows = [row for row in rows if row.get("status") in statuses]

    if scope not in {"all", "current"}:
        raise ValueError(f"未知 session scope：{scope!r}")
    if scope == "current":
        if current["repo_root"]:
            rows = [
                row for row in rows
                if row.get("repo_root") == current["repo_root"]
            ]
        else:
            rows = [row for row in rows if row.get("cwd") == cwd]
    if branch_only:
        if not current["repo_root"] or not current["git_branch"]:
            rows = []
        else:
            rows = [
                row for row in rows
                if row.get("repo_root") == current["repo_root"]
                and row.get("git_branch") == current["git_branch"]
            ]

    needle = str(query or "").casefold().strip()
    if needle:
        rows = [
            row for row in rows
            if needle in " ".join(str(row.get(name) or "") for name in (
                "title", "id", "cwd", "repo_root", "git_branch",
                "model", "gateway",
            )).casefold()
        ]
    groups = defaultdict(list)
    for row in rows:
        groups[row["branch_root_id"]].append(row)
    group_order = sorted(
        groups,
        key=lambda root: max(
            item.get("updated") or "" for item in groups[root]),
        reverse=True,
    )
    grouped = []
    for root in group_order:
        members = groups[root]
        members.sort(key=lambda row: row.get("updated") or "", reverse=True)
        members.sort(key=lambda row: int(row.get("branch_depth") or 0))
        grouped.extend(members)
    rows = grouped
    offset = max(0, int(offset))
    if limit is None:
        rows = rows[offset:]
    else:
        rows = rows[offset:offset + max(0, int(limit))]
    leases = active_session_leases()
    for row in rows:
        lease = leases.get(row["id"])
        row["live"] = lease is not None
        row["lease"] = dict(lease) if lease is not None else None
    return rows


def list_sessions(limit=30, cwd_only=False):
    """Backward-compatible metadata-only list facade."""
    return list_session_summaries(
        limit=limit,
        scope="all",
        cwd=os.getcwd(),
        query="",
        status="active",
    ) if not cwd_only else [
        row for row in list_session_summaries(
            limit=None, scope="all", cwd=os.getcwd(), status="active")
        if row.get("cwd") == os.path.abspath(os.getcwd())
    ][:max(0, int(limit))]


def update_session_metadata(sid, *, owner_id=None, **changes):
    """Merge selected metadata under the same lock used by transcript saves."""
    allowed = {"title", "status", "cwd", "repo_root", "git_branch"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"不允许修改 session metadata：{sorted(unknown)}")
    session_id = _resolve_session_id(sid, include_archived=True)
    path = SESSIONS / f"{session_id}.json"
    with _exclusive_file_lock(_session_lock_target(session_id)):
        _assert_session_write_owner_unlocked(session_id, owner_id)
        record = _read_session_path(path)
        if "status" in changes:
            status = str(changes["status"])
            if status not in _SESSION_STATUSES:
                raise ValueError(f"未知 session status：{status!r}")
            if status == "archived":
                lease = _read_lease_unlocked(session_id)
                if lease and _lease_is_live(lease):
                    raise SessionBusyError(session_id, lease)
            changes["status"] = status
        if "title" in changes:
            title = " ".join(str(changes["title"] or "").split())
            if not title:
                raise ValueError("会话标题不能为空")
            changes["title"] = title[:160]
        environment = None
        if "cwd" in changes:
            changes["cwd"] = os.path.abspath(
                os.path.expanduser(str(changes["cwd"])))
            environment = repo_context(changes["cwd"])
            changes.setdefault("repo_root", environment["repo_root"])
            changes.setdefault("git_branch", environment["git_branch"])
        record.update(changes)
        record["updated"] = _now()
        _write_session_and_index_unlocked(
            path, record, environment=environment)
    _shadow_sync_session(record)
    return _summary_from_record(record, environment=environment)


def append_checkpoint_audit(sid, kind, payload=None, *, owner_id=None):
    """Append a durable rewind/checkpoint audit record to canonical JSON."""
    session_id = _resolve_session_id(sid, include_archived=True)
    kind = str(kind or "").strip()
    if not kind:
        raise ValueError("checkpoint audit kind 不能为空")
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ValueError("checkpoint audit payload 必须是 dict")
    try:
        payload = json.loads(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=_jsonable))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"checkpoint audit payload 无法序列化: {exc}") from exc
    path = SESSIONS / f"{session_id}.json"
    with _exclusive_file_lock(_session_lock_target(session_id)):
        _assert_session_write_owner_unlocked(session_id, owner_id)
        record = _read_session_path(path)
        history = record.get("checkpoint_audit", [])
        if not isinstance(history, list):
            raise SessionCorruptError(
                f"会话 checkpoint_audit 不是 array：{path}")
        entry = {
            "schema_version": 1,
            "kind": kind,
            "payload": payload,
            "created_at": _now(),
        }
        record["checkpoint_audit"] = [*history, entry]
        record["updated"] = _now()
        _write_session_and_index_unlocked(path, record)
    _shadow_sync_session(record)
    return dict(entry)


def rename_session(sid, title, *, owner_id=None):
    return update_session_metadata(sid, owner_id=owner_id, title=title)


def archive_session(sid):
    return update_session_metadata(sid, status="archived")


def unarchive_session(sid):
    return update_session_metadata(sid, status="active")


def most_recent_id(cwd_only=False):
    s = list_sessions(limit=1, cwd_only=cwd_only)
    return s[0]["id"] if s else None


def migrate_legacy():
    """把旧的单槽位 last.json 迁成一个正常会话文件。只做一次。"""
    old = SESSIONS / "last.json"
    if not old.is_file():
        return None
    with _exclusive_file_lock(old):
        if not old.is_file():
            return None
        try:
            d = json.loads(old.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        messages = d.get("messages") if isinstance(d, dict) else None
        if not isinstance(messages, list):
            return None
        source = json.dumps(
            d, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=_jsonable)
        source_sha256 = state_db.sha256_bytes(source.encode("utf-8"))
        # Deterministic id makes a crash after writing but before renaming
        # idempotent instead of producing duplicate migrated chats.
        sid = uuid.uuid5(uuid.NAMESPACE_URL, source).hex[:12]
        environment = repo_context(d.get("cwd") or os.getcwd())
        def legacy_int(name):
            try:
                return max(0, int(d.get(name) or 0))
            except (TypeError, ValueError):
                return 0
        rec = dict(d)
        rec.update({
            "id": sid,
            "title": (
                str(d.get("title") or _auto_title(messages))
                + "（迁移自 last.json）"
            ),
            "status": "active",
            "model": str(d.get("model") or ""),
            "gateway": str(d.get("gateway") or ""),
            "cwd": environment["cwd"],
            "repo_root": environment["repo_root"],
            "git_branch": environment["git_branch"],
            "created": str(d.get("created") or _now()),
            "updated": _now(),
            "tokens_in": legacy_int("tokens_in"),
            "tokens_out": legacy_int("tokens_out"),
            "last_context_tokens": legacy_int("last_context_tokens"),
            "messages": messages,
            "legacy_source_sha256": source_sha256,
        })
        new_path = SESSIONS / f"{sid}.json"
        with _exclusive_file_lock(_session_lock_target(sid)):
            if new_path.exists():
                # A crash can leave canonical written before last.json is
                # renamed; a copied-back legacy file can also reappear later.
                # In both cases the canonical chat may already have continued.
                # Re-index it, but never roll it back to the legacy snapshot.
                existing = _read_session_path(new_path)
                existing_environment = repo_context(
                    existing.get("cwd") or os.getcwd())
                _upsert_session_summary(
                    existing, environment=existing_environment)
            else:
                _assert_session_write_owner_unlocked(sid)
                _write_session_and_index_unlocked(
                    new_path, rec, environment=environment)
        migrated = SESSIONS / f"last.{sid}.json.migrated"
        old.rename(migrated)
        _private(migrated)
        return sid


# ------------------------------------------------------------------ 用量

def append_usage(rec):
    """追加一行流水。写日志不该弄崩会话，异常一律吞掉。"""
    try:
        HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(HOME, 0o700)
        with _exclusive_file_lock(USAGE_LOG):
            with open(USAGE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            _private(USAGE_LOG)
        _shadow_record_usage(rec)
    except OSError:
        pass


def _read_usage_unlocked():
    if not USAGE_LOG.is_file():
        return []
    out = []
    for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # 半行/损坏行跳过，不让一行毁掉整份统计
    return out


def read_usage():
    if not USAGE_LOG.is_file():
        return []
    with _exclusive_file_lock(USAGE_LOG):
        return _read_usage_unlocked()


def rebuild_stats():
    """从 usage.jsonl 重算 stats-cache.json。

    形状刻意对齐 Claude Code 的 ~/.claude/stats-cache.json：同样的
    dailyActivity / dailyModelTokens / modelUsage / hourCounts 字段名，
    这样已有的统计脚本能直接复用。额外多一个 gatewayUsage —— 那是本项目
    特有的维度（Claude Code 只有一个后端，不需要）。
    """
    with _exclusive_file_lock(STATS_CACHE):
        return _rebuild_stats_locked()


def _rebuild_stats_locked():
    """Recompute stats while the caller holds the stats-cache lock."""
    recs = read_usage()
    daily = defaultdict(lambda: {"messageCount": 0, "sessions": set(), "toolCallCount": 0})
    daily_model = defaultdict(Counter)
    model_usage = defaultdict(Counter)
    gateway_usage = defaultdict(Counter)
    hours = Counter()
    sessions = set()

    for r in recs:
        ts = r.get("ts", "")
        day = ts[:10]
        model = r.get("model", "?")
        gw = r.get("gateway", "?")
        sid = r.get("session", "")
        tin = r.get("prompt_tokens", 0)
        tout = r.get("completion_tokens", 0)
        cr = r.get("cache_read", 0)
        reasoning = r.get("reasoning_tokens", 0)

        sessions.add(sid)
        daily[day]["messageCount"] += 1
        daily[day]["sessions"].add(sid)
        daily[day]["toolCallCount"] += len(r.get("tools") or [])
        daily_model[day][model] += tin + tout
        hours[str(int(ts[11:13]))] += 1 if len(ts) > 12 else 0

        for agg in (model_usage[model], gateway_usage[gw]):
            agg["inputTokens"] += tin
            agg["outputTokens"] += tout
            agg["cacheReadInputTokens"] += cr
            agg["cacheCreationInputTokens"] += r.get("cache_write", 0)
            # 缓存命中部分已在 prompt_tokens 里计费，freshInput 是实际新处理的量。
            # 两者拆开才能回答「缓存到底省了多少」。
            agg["freshInputTokens"] += max(0, tin - cr)
            # reasoning 模型的思考 token；0 表示网关没上报或模型不思考。
            agg["reasoningTokens"] += reasoning
            agg["calls"] += 1

    stats = {
        "version": STATS_VERSION,
        "lastComputedDate": _now()[:10],
        "dailyActivity": [
            {"date": d, "messageCount": v["messageCount"],
             "sessionCount": len(v["sessions"]), "toolCallCount": v["toolCallCount"]}
            for d, v in sorted(daily.items())],
        "dailyModelTokens": [
            {"date": d, "tokensByModel": dict(c)} for d, c in sorted(daily_model.items())],
        "modelUsage": {m: dict(c) for m, c in model_usage.items()},
        # 本项目特有：同一个模型可能同时挂在两个网关下，成本与配额不同。
        "gatewayUsage": {g: dict(c) for g, c in gateway_usage.items()},
        "totalSessions": len(sessions),
        "totalCalls": len(recs),
        "totalTokens": sum(r.get("total_tokens", 0) for r in recs),
        "firstSessionDate": min((r.get("ts", "") for r in recs), default=""),
        # Counter 按首次出现顺序排列，小时在 JSON 里会乱序（9,10,...,13,3,4）。
        # 排序后人类直接读文件也能看出日内分布。
        "hourCounts": {h: hours[h] for h in sorted(hours, key=int)},
    }
    try:
        HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private(HOME, 0o700)
        _atomic_write_text(
            STATS_CACHE, json.dumps(stats, ensure_ascii=False, indent=2))
    except OSError:
        pass
    return stats
