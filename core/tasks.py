"""M3c foreground/background TaskManager。

TaskManager 只负责阻塞工具的运行生命周期，不碰 transcript、Controller、SQLite
或 terminal。主线程先持久化 tool/task intent，再让这里启动 worker；worker 只通过
有界事件、UI ring 和私有 artifact 文件交回结果。

一个 manager 同时只有一个 foreground task，但可继续管理多个 background bash。
background 只改变 UI/事件所有权，不改变 worker、artifact 或取消语义。
"""
from __future__ import annotations
from . import paths

import collections
import codecs
import hashlib
import json
import math
import os
import queue
import re
import selectors
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class TaskError(RuntimeError):
    """TaskManager 无法安全创建或运行任务。"""


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}
ARTIFACT_INDEX_VERSION = 1
ARTIFACT_INDEX_NAME = "index.json"
ARTIFACT_PAGE_BYTES = 30_000
_REDACTED = b"[REDACTED_SECRET]"
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY|"
    r"ACCESS_KEY|CREDENTIAL)$")


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _safe_component(value, fallback):
    raw = str(value or "")
    cleaned = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    cleaned = cleaned.strip("._")[:80]
    return cleaned or fallback


def _read_utf8_window(path, start, count):
    """Read one nominal byte window without corrupting split UTF-8 chars."""
    with Path(path).open("rb") as stream:
        stream.seek(start)
        data = stream.read(count + 3)
    begin = 0
    if start:
        while (begin < min(3, len(data))
               and data[begin] & 0xC0 == 0x80):
            begin += 1
    end = min(len(data), count)
    while end < len(data) and data[end] & 0xC0 == 0x80:
        end += 1
    return (
        data[begin:end].decode("utf-8", "replace"),
        start + begin,
        start + end,
    )


def _under(path, root):
    value = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    base = os.path.realpath(os.path.abspath(os.path.expanduser(str(root))))
    return value == base or value.startswith(base + os.sep)


def _validate_artifact_root(path):
    root = Path(path).expanduser().absolute()
    if paths.under_protected(root):
        raise TaskError("artifact root 不能位于受保护路径")
    if root.is_symlink():
        raise TaskError(f"artifact root 不能是符号链接：{root}")
    return root


def _environment_secrets():
    values = []
    for name, value in os.environ.items():
        if (_SECRET_NAME.search(name.upper())
                and isinstance(value, str)
                and len(value.encode("utf-8", "ignore")) >= 8):
            values.append(value)
    return values


def redact_known_secrets(value):
    """Return text with exact known environment-secret values redacted.

    This is intentionally the same policy as managed task artifacts.  It is
    not a general secret detector: unknown, encoded and generated values are
    outside this narrow output-sanitization contract.
    """
    if value is None:
        raw = b""
    elif isinstance(value, bytes):
        raw = value
    else:
        raw = str(value).encode("utf-8", "replace")
    redactor = _SecretRedactor(_environment_secrets())
    return redactor.feed(raw, final=True).decode("utf-8", "replace")


class _SecretRedactor:
    """跨 chunk 精确替换；只暂存“仍可能成为 secret”的尾前缀。"""

    def __init__(self, values):
        encoded = {
            str(value).encode("utf-8")
            for value in values
            if value is not None and len(str(value).encode("utf-8")) >= 8
        }
        self.secrets = sorted(encoded, key=len, reverse=True)
        self.pending = b""
        self.count = 0

    def feed(self, data=b"", *, final=False):
        if not self.secrets:
            return bytes(data)
        self.pending += bytes(data)
        out = bytearray()
        index = 0
        while index < len(self.pending):
            matched = next(
                (secret for secret in self.secrets
                 if self.pending.startswith(secret, index)),
                None)
            if matched is not None:
                out.extend(_REDACTED)
                index += len(matched)
                self.count += 1
            elif (not final and any(
                    secret.startswith(self.pending[index:])
                    for secret in self.secrets)):
                # 这个尾巴可能是跨下一 chunk 的 secret 开头；只保留它。
                # 普通 READY/进度文本不会再因最长 token 很长而延迟。
                break
            else:
                out.append(self.pending[index])
                index += 1
        self.pending = self.pending[index:]
        return bytes(out)


class KnownSecretRedactor:
    """Streaming redactor shared by managed and direct process runners."""

    def __init__(self):
        self._impl = _SecretRedactor(_environment_secrets())

    @property
    def count(self):
        return self._impl.count

    def feed(self, data=b"", *, final=False):
        return self._impl.feed(data, final=final)


@dataclass(frozen=True)
class TaskSnapshot:
    id: str
    key: str
    session: str
    name: str
    background: bool
    backgrounded_at: str | None
    status: TaskStatus
    created_at: str
    started_at: str | None
    ended_at: str | None
    cancel_requested_at: str | None
    process_pid: int | None
    returncode: int | None
    signal: int | None
    timed_out: bool
    stdout_path: str | None
    stderr_path: str | None
    stdout_bytes: int
    stderr_bytes: int
    stdout_lines: int
    stderr_lines: int
    stdout_sha256: str | None
    stderr_sha256: str | None
    truncated: bool
    ui_dropped_bytes: int
    note_dropped: int
    runtime_event_dropped: int
    redaction_count: int
    error: str | None

    def as_dict(self):
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass
class _InteractionRequest:
    """A worker-to-owner request which never performs terminal I/O itself.

    The request object deliberately lives on the task handle rather than in a
    global queue: task cancellation, ownership and audit correlation then stay
    bound to the tool call that asked the question.  ``done`` is the only
    synchronisation primitive the worker waits on; the owner resolves it via
    :meth:`TaskHandle.resolve_interaction`.
    """

    id: str
    kind: str
    payload: dict
    created_at: str
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    announced: bool = False


class _Artifact:
    """单个 stdout/stderr append-only spool。"""

    def __init__(self, path):
        self.path = Path(path)
        self._stream = None
        self._opened = False
        self._sha = hashlib.sha256()
        self.byte_count = 0
        self._newlines = 0
        self._last_byte = None

    def open(self):
        if paths.under_protected(self.path):
            raise TaskError(
                f"拒绝把 task artifact 写入受保护路径：{self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise TaskError(
                f"artifact session 目录不能是符号链接：{self.path.parent}")
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            self._stream = os.fdopen(fd, "wb", buffering=0)
            self._opened = True
        except BaseException:
            os.close(fd)
            raise
        return self

    def write(self, data):
        if not data:
            return
        if self._stream is None:
            self.open()
        data = bytes(data)
        self._stream.write(data)
        self._sha.update(data)
        self.byte_count += len(data)
        self._newlines += data.count(b"\n")
        self._last_byte = data[-1]

    @property
    def line_count(self):
        return self._newlines + (
            1 if self.byte_count and self._last_byte != 10 else 0)

    @property
    def sha256(self):
        return self._sha.hexdigest() if self._opened else None

    def close(self):
        if self._stream is not None:
            stream = self._stream
            try:
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                try:
                    stream.close()
                finally:
                    self._stream = None

    def preview(self, limit):
        if not self.path.is_file() or self.byte_count == 0:
            return "", False
        limit = max(1, int(limit))
        with self.path.open("rb") as stream:
            if self.byte_count <= limit:
                data = stream.read()
                return data.decode("utf-8", "replace"), False
            head_size = limit * 2 // 3
            tail_size = limit - head_size
            head = stream.read(head_size)
            stream.seek(max(0, self.byte_count - tail_size))
            tail = stream.read(tail_size)
        omitted = self.byte_count - len(head) - len(tail)
        marker = (
            f"\n\n… [artifact 中省略 {omitted:,} bytes] …\n\n"
        ).encode("utf-8")
        return (head + marker + tail).decode("utf-8", "replace"), True


class TaskHandle:
    """一个 managed task 的线程安全 cancellation/output handle。"""

    def __init__(self, manager, *, task_id, key, session, name,
                 stdout_path, stderr_path):
        self.manager = manager
        self.id = task_id
        self.key = key
        self.session = session
        self.name = name
        self.background = False
        self.backgrounded_at = None
        self.created_at = _utcnow()
        self.started_at = None
        self.ended_at = None
        self.cancel_requested_at = None
        self.status = TaskStatus.QUEUED
        self.returncode = None
        self.signal = None
        self.timed_out = False
        self.error = None
        self.result = None
        self.process_pid = None

        self._stdout = _Artifact(stdout_path)
        self._stderr = _Artifact(stderr_path)
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._worker = None
        self._process = None
        self._process_used = False
        self._process_group_cleanup_noted = False
        self._stop_stage = None
        self._signal_at = None
        self._ui = collections.deque()
        self._ui_bytes = 0
        self._ui_dropped = 0
        self._ui_decoders = {
            stream: codecs.getincrementaldecoder("utf-8")(
                errors="replace")
            for stream in ("stdout", "stderr")
        }
        self._ui_decoder_gaps = set()
        self._ui_final = False
        self._ui_decoders_finalized = False
        self._notes = queue.Queue(maxsize=64)
        self._note_dropped = 0
        self._runtime_events = queue.Queue(maxsize=64)
        self._runtime_event_dropped = 0
        self._workspace_changed_pending = None
        # Interactive tools (currently decision_gate) use this private,
        # lossless bridge.  A worker may enqueue one request and wait, while
        # the Session owner drains it from the normal TaskManager event loop.
        # It is intentionally separate from the bounded runtime event queue:
        # dropping a consent request would leave a worker blocked forever.
        self._interaction_lock = threading.RLock()
        self._interactions = collections.OrderedDict()
        self._stdout_redactor = _SecretRedactor(
            manager.secret_values)
        self._stderr_redactor = _SecretRedactor(
            manager.secret_values)
        self._redaction_count = 0
        self._background_announced = threading.Event()
        self._background_terminal_emitted = False

    @property
    def cancel_event(self):
        return self._cancel

    def is_cancelled(self):
        return self._cancel.is_set()

    def is_done(self):
        return self._done.is_set()

    def is_background(self):
        with self._lock:
            return bool(self.background)

    def mark_background(self):
        with self._lock:
            if self.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                return False
            if self.background:
                return False
            self.background = True
            self.backgrounded_at = _utcnow()
            return True

    def announce_background(self):
        self._background_announced.set()

    def background_announced(self):
        return self._background_announced.is_set()

    def claim_background_terminal(self):
        with self._lock:
            if (not self.background or not self._done.is_set()
                    or self._background_terminal_emitted):
                return False
            self._background_terminal_emitted = True
            return True

    def background_terminal_emitted(self):
        with self._lock:
            return bool(self._background_terminal_emitted)

    def note(self, message):
        message = self.redact_result(message)
        try:
            self._notes.put_nowait(message)
        except queue.Full:
            with self._lock:
                self._note_dropped += 1

    def drain_notes(self):
        out = []
        while True:
            try:
                out.append(self._notes.get_nowait())
            except queue.Empty:
                return out

    def runtime_event(self, event):
        """worker → main 的有界结构化通道；不能触碰 Controller/SQLite。"""
        if not isinstance(event, dict) or not event.get("kind"):
            return False

        def clean(value):
            if isinstance(value, str):
                return self.redact_result(value)
            if isinstance(value, dict):
                return {str(key): clean(item)
                        for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            if value is None or isinstance(value, (bool, int, float)):
                return value
            return self.redact_result(str(value))

        prepared = clean(event)
        if prepared.get("kind") == "workspace_changed":
            # Projection invalidation is correctness-critical but naturally
            # coalescible. Keep it outside the lossy general event queue so a
            # burst of agent/workflow events cannot make the next provider
            # request consume stale repo context.
            with self._lock:
                self._workspace_changed_pending = prepared
            return True
        try:
            self._runtime_events.put_nowait(prepared)
            return True
        except queue.Full:
            with self._lock:
                self._runtime_event_dropped += 1
            return False

    def drain_runtime_events(self):
        out = []
        while True:
            try:
                out.append(self._runtime_events.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            pending = self._workspace_changed_pending
            self._workspace_changed_pending = None
        if pending is not None:
            out.append(pending)
        return out

    # ---------------------------- owner-thread interaction bridge
    @staticmethod
    def _interaction_result(status, *, reason=None, suggested_choice=""):
        value = {
            "choice": "",
            "notes": "",
            "attended": False,
            "status": str(status),
        }
        if reason:
            value["reason"] = str(reason)[:512]
        if suggested_choice:
            value["suggested_choice"] = str(suggested_choice)[:256]
        return value

    def request_interaction(self, kind, payload, *, timeout=None):
        """Synchronously ask the owner to resolve a structured request.

        This method is safe to call from a managed worker.  It never invokes a
        callback, reads stdin, or writes stdout.  The returned object is a
        small JSON-compatible result; cancellation and timeout are explicit
        non-success states so callers cannot mistake them for consent.
        """
        kind = str(kind or "").strip()
        if not kind:
            raise TaskError("interaction kind 不能为空")
        try:
            # A deep JSON round-trip gives the owner a detached snapshot and
            # rejects unserialisable/model-specific objects before they enter
            # the cross-thread channel.
            snapshot = json.loads(json.dumps(
                payload or {}, ensure_ascii=False, sort_keys=True,
                allow_nan=False))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskError(
                f"interaction payload 不可序列化：{type(exc).__name__}") from None
        if not isinstance(snapshot, dict):
            raise TaskError("interaction payload 必须是 object")
        request_id = f"{self.id}:interaction-{uuid.uuid4().hex[:12]}"
        request = _InteractionRequest(
            id=request_id, kind=kind, payload=snapshot, created_at=_utcnow())
        with self._interaction_lock:
            if self.is_cancelled() or self.is_done():
                return self._interaction_result(
                    "cancelled", reason="task 已取消或结束")
            self._interactions[request_id] = request
            # Cancellation can arrive between the check above and insertion.
            # Close that race while still holding the interaction lock so a
            # late owner drain cannot display a request whose worker is gone.
            if self.is_cancelled() or self.is_done():
                request.result = self._interaction_result(
                    "cancelled", reason="task 在 interaction 入队时已结束")
                request.done.set()

        # ``Event.wait`` in short bounded slices makes cancellation responsive
        # without asking the owner thread to poll a second stdin channel.
        try:
            deadline = (
                None if timeout is None
                else time.monotonic() + max(0.0, float(timeout)))
            while True:
                if request.done.wait(0.05):
                    break
                if self.is_cancelled():
                    with self._interaction_lock:
                        if not request.done.is_set():
                            request.result = self._interaction_result(
                                "cancelled", reason="task 被取消")
                            request.done.set()
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    with self._interaction_lock:
                        if not request.done.is_set():
                            request.result = self._interaction_result(
                                "expired", reason="interaction 超时")
                            request.done.set()
                    break
            with self._interaction_lock:
                return dict(request.result or self._interaction_result(
                    "cancelled", reason="interaction 没有返回结果"))
        finally:
            with self._interaction_lock:
                self._interactions.pop(request_id, None)

    def drain_interactions(self):
        """Return each pending request once for the owner event loop."""
        with self._interaction_lock:
            out = []
            for request in self._interactions.values():
                if request.done.is_set() or request.announced:
                    continue
                request.announced = True
                out.append({
                    "request_id": request.id,
                    "kind": request.kind,
                    "payload": json.loads(json.dumps(
                        request.payload, ensure_ascii=False)),
                    "created_at": request.created_at,
                })
            return out

    def resolve_interaction(self, request_id, result):
        """Resolve one request from the owner thread; return whether accepted."""
        request_id = str(request_id or "").strip()
        if not request_id or not isinstance(result, dict):
            return False
        try:
            clean = json.loads(json.dumps(
                result, ensure_ascii=False, sort_keys=True,
                allow_nan=False))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        with self._interaction_lock:
            request = self._interactions.get(request_id)
            if request is None or request.done.is_set():
                return False
            request.result = clean
            request.done.set()
            return True

    def pending_interactions(self):
        """Read-only owner diagnostics for tests/status surfaces."""
        with self._interaction_lock:
            return tuple({
                "request_id": item.id, "kind": item.kind,
                "payload": json.loads(json.dumps(
                    item.payload, ensure_ascii=False)),
                "created_at": item.created_at,
            } for item in self._interactions.values()
              if not item.done.is_set())

    def _cancel_interactions(self, reason="task 被取消"):
        with self._interaction_lock:
            for request in self._interactions.values():
                if not request.done.is_set():
                    request.result = self._interaction_result(
                        "cancelled", reason=reason)
                    request.done.set()

    def _publish_output(self, stream, data):
        data = bytes(data)
        stream = str(stream)
        limit = self.manager.ui_ring_bytes
        if limit <= 0 or not data:
            return
        with self._lock:
            if len(data) > limit:
                self._ui_dropped += len(data) - limit
                self._ui_decoder_gaps.add(stream)
                data = data[-limit:]
            while self._ui and self._ui_bytes + len(data) > limit:
                old_stream, old = self._ui.popleft()
                self._ui_bytes -= len(old)
                self._ui_dropped += len(old)
                self._ui_decoder_gaps.add(old_stream)
            self._ui.append((stream, data))
            self._ui_bytes += len(data)

    def drain_output(self):
        with self._lock:
            rows = list(self._ui)
            self._ui.clear()
            self._ui_bytes = 0
            for stream in self._ui_decoder_gaps:
                self._ui_decoders[stream].reset()
            self._ui_decoder_gaps.clear()

            decoded = []
            for stream, data in rows:
                value = self._ui_decoders[stream].decode(
                    data, final=False)
                if value:
                    decoded.append((stream, value))
            if self._ui_final and not self._ui_decoders_finalized:
                for stream, decoder in self._ui_decoders.items():
                    value = decoder.decode(b"", final=True)
                    if value:
                        decoded.append((stream, value))
                self._ui_decoders_finalized = True
            return decoded

    def _write_output(self, stream, data, *, final=False):
        artifact = self._stdout if stream == "stdout" else self._stderr
        redactor = (
            self._stdout_redactor
            if stream == "stdout" else self._stderr_redactor)
        with self._lock:
            before = redactor.count
            clean = redactor.feed(data, final=final)
            self._redaction_count += redactor.count - before
            artifact.write(clean)
            self._publish_output(stream, clean)

    def _flush_redactors(self):
        self._write_output("stdout", b"", final=True)
        self._write_output("stderr", b"", final=True)

    def redact_result(self, result):
        redactor = _SecretRedactor(self.manager.secret_values)
        clean = redactor.feed(
            str(result).encode("utf-8", "replace"), final=True)
        with self._lock:
            self._redaction_count += redactor.count
        return clean.decode("utf-8", "replace")

    def _group_exists(self):
        with self._lock:
            pid = self.process_pid
        if pid is None:
            return False
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # 同 uid 的 managed group 不应发生；保守视为仍存在。
            return True

    def _send_group(self, sig, *, allow_exited_leader=False):
        with self._lock:
            proc = self._process
            pid = self.process_pid
        if pid is None:
            return False
        if (not allow_exited_leader
                and (proc is None or proc.poll() is not None)):
            return False
        try:
            os.killpg(pid, sig)
            return True
        except ProcessLookupError:
            return False

    def _cleanup_process_group(self):
        """TERM→KILL 清理 leader 退出后仍留在同 PGID 的子进程。"""
        if not self._group_exists():
            return False
        with self._lock:
            first_notice = not self._process_group_cleanup_noted
            self._process_group_cleanup_noted = True
        if first_notice:
            self.note(
                "shell leader 已结束或 task 正在收尾；"
                "正在清理同进程组残留子进程")

        self._send_group(
            signal.SIGTERM, allow_exited_leader=True)
        deadline = time.monotonic() + self.manager.terminate_grace
        while self._group_exists() and time.monotonic() < deadline:
            with self._lock:
                proc = self._process
            if proc is not None:
                proc.poll()             # 及时 reap managed leader zombie
            time.sleep(min(0.01, self.manager.poll_interval))
        if self._group_exists():
            self._send_group(
                signal.SIGKILL, allow_exited_leader=True)
            deadline = time.monotonic() + 1.0
            while self._group_exists() and time.monotonic() < deadline:
                with self._lock:
                    proc = self._process
                if proc is not None:
                    proc.poll()
                time.sleep(min(0.01, self.manager.poll_interval))
        if self._group_exists():
            error = TaskError(
                f"无法清理 managed process group {self.process_pid}")
            self.record_error(error)
            self.note(str(error))
        return True

    def _start_stop(self, sig):
        now = time.monotonic()
        with self._lock:
            if self._stop_stage is not None:
                return
            self._stop_stage = sig
            self._signal_at = now
        self._send_group(sig)

    def _advance_stop(self):
        with self._lock:
            stage = self._stop_stage
            sent = self._signal_at
        if stage is None or sent is None:
            return
        now = time.monotonic()
        if (stage == signal.SIGINT
                and now - sent >= self.manager.interrupt_grace):
            with self._lock:
                if self._stop_stage != signal.SIGINT:
                    return
                self._stop_stage = signal.SIGTERM
                self._signal_at = now
            self._send_group(signal.SIGTERM)
        elif (stage == signal.SIGTERM
              and now - sent >= self.manager.terminate_grace):
            with self._lock:
                if self._stop_stage != signal.SIGTERM:
                    return
                self._stop_stage = signal.SIGKILL
                self._signal_at = now
            self._send_group(signal.SIGKILL)

    def cancel(self):
        with self._lock:
            if self.status in TERMINAL_STATES:
                return False
            first = not self._cancel.is_set()
            self._cancel.set()
            if self.cancel_requested_at is None:
                self.cancel_requested_at = _utcnow()
            self.status = TaskStatus.CANCELLING
        # Wake a worker blocked in an interactive request before attempting
        # any process-group signal escalation.
        self._cancel_interactions()
        if first:
            self._start_stop(signal.SIGINT)
        return first

    def request_timeout(self):
        with self._lock:
            if self.timed_out:
                return False
            self.timed_out = True
        self._start_stop(signal.SIGTERM)
        return True

    def bind_process(self, proc, *, managed_output=True):
        with self._lock:
            self._process = proc
            self._process_used = self._process_used or bool(managed_output)
            self.process_pid = int(proc.pid)
            cancelled = self._cancel.is_set()
            timed_out = self.timed_out
        if cancelled:
            self._start_stop(signal.SIGINT)
            # _start_stop 在 pre-cancel 时已经设置 stage，但当时没有 proc；
            # bind 后需补发第一枚信号。
            self._send_group(signal.SIGINT)
        elif timed_out:
            self._start_stop(signal.SIGTERM)
            self._send_group(signal.SIGTERM)

    def unbind_process(self, proc):
        with self._lock:
            if self._process is proc:
                self._process = None

    def record_returncode(self, returncode):
        with self._lock:
            self.returncode = int(returncode)
            self.signal = -self.returncode if self.returncode < 0 else None

    def record_error(self, error):
        value = self.redact_result(
            f"{type(error).__name__}: {error}")
        with self._lock:
            self.error = value

    def ensure_process_stopped(self, proc):
        """等待已请求的进程组停止，并按 INT→TERM→KILL 升级。"""
        deadline = (
            time.monotonic() + self.manager.interrupt_grace
            + self.manager.terminate_grace + 1.0)
        while proc.poll() is None and time.monotonic() < deadline:
            self._advance_stop()
            time.sleep(min(0.01, self.manager.poll_interval))
        if proc.poll() is None:
            self._send_group(signal.SIGKILL)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

    def run_process(self, argv, *, cwd=None, timeout=None, env=None):
        """运行一个可取消的进程组，同时完整 spool stdout/stderr。"""
        if timeout is None:
            timeout_seconds = None
        else:
            try:
                timeout_seconds = float(timeout)
            except (TypeError, ValueError) as exc:
                raise TaskError("bash timeout 必须是非负有限数字") from exc
            if (not math.isfinite(timeout_seconds)
                    or timeout_seconds < 0):
                raise TaskError("bash timeout 必须是非负有限数字")

        selector = None
        proc = subprocess.Popen(
            list(argv), cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, bufsize=0)
        try:
            # Popen 之后的 bind、selector 构造和每一次 register 必须都在
            # 同一个 cleanup boundary 内；任何 setup 错误都不能泄漏 PGID。
            self.bind_process(proc)
            selector = selectors.DefaultSelector()
            pipes = {
                proc.stdout: "stdout",
                proc.stderr: "stderr",
            }
            for pipe, stream_name in pipes.items():
                os.set_blocking(pipe.fileno(), False)
                selector.register(
                    pipe, selectors.EVENT_READ, stream_name)
            deadline = (
                time.monotonic() + timeout_seconds
                if timeout_seconds is not None else None)
            while selector.get_map() or proc.poll() is None:
                leader_returncode = proc.poll()
                if leader_returncode is not None:
                    if self.returncode is None:
                        self.record_returncode(leader_returncode)
                    # shell 可在后台 child 仍存活时先 exit 0。此时不能等到
                    # CLI shutdown，更不能把 proc.poll()!=None 当成 group 消失。
                    self._cleanup_process_group()
                now = time.monotonic()
                if (deadline is not None and now >= deadline
                        and not self.timed_out):
                    self.request_timeout()
                self._advance_stop()
                for key, _mask in selector.select(self.manager.poll_interval):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 1 << 16)
                    except BlockingIOError:
                        continue
                    if chunk:
                        self._write_output(key.data, chunk)
                    else:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
            if self.returncode is None:
                self.record_returncode(proc.wait())
        finally:
            cleanup_error = None
            try:
                # 正常 leader exit 的 orphan，以及 output/artifact 异常时仍活着
                # 的 leader，都必须在解除 handle 绑定前清理。
                self._cleanup_process_group()
                leader_returncode = proc.poll()
                if leader_returncode is None:
                    try:
                        leader_returncode = proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        leader_returncode = None
                if (leader_returncode is not None
                        and self.returncode is None):
                    self.record_returncode(leader_returncode)
            except BaseException as exc:
                cleanup_error = exc
            try:
                self._flush_redactors()
            finally:
                if selector is not None:
                    try:
                        registered = list(
                            selector.get_map().values())
                    except Exception:
                        registered = []
                    for key in registered:
                        try:
                            selector.unregister(key.fileobj)
                        except Exception:
                            pass
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is None:
                        continue
                    try:
                        pipe.close()
                    except Exception:
                        pass
                try:
                    if selector is not None:
                        selector.close()
                finally:
                    self.unbind_process(proc)
            if cleanup_error is not None:
                raise cleanup_error
        return self.process_result(timeout=timeout_seconds)

    def process_result(self, *, timeout=None):
        stdout, stdout_cut = self._stdout.preview(20_000)
        stderr, stderr_cut = self._stderr.preview(10_000)
        pieces = []
        if self.is_cancelled():
            pieces.append("[用户中断，进程组已终止]")
        elif self.timed_out:
            pieces.append(
                f"[超时 {timeout}s，进程组已终止]")
        if stdout.strip():
            pieces.append(stdout.strip())
        if stderr.strip():
            pieces.append("[stderr]\n" + stderr.strip())
        if self.returncode not in (None, 0):
            if self.signal:
                try:
                    label = signal.Signals(self.signal).name
                except ValueError:
                    label = str(self.signal)
                pieces.append(f"[signal {label}]")
            else:
                pieces.append(f"[exit {self.returncode}]")
        if not pieces:
            pieces.append(f"[无输出，exit {self.returncode or 0}]")
        truncated = stdout_cut or stderr_cut
        if truncated:
            paths = []
            if self._stdout.byte_count:
                paths.append(
                    f"stdout={self._stdout.path} ({self._stdout.byte_count:,} bytes)")
            if self._stderr.byte_count:
                paths.append(
                    f"stderr={self._stderr.path} ({self._stderr.byte_count:,} bytes)")
            pieces.append("[完整输出 artifact: " + " · ".join(paths) + "]")
        return "\n".join(pieces)

    def write_result_artifact(self, result):
        if self._process_used:
            return
        result = self.redact_result(result)
        with self._lock:
            self._stdout.write(
                str(result).encode("utf-8", "replace"))
        return result

    def close_artifacts(self):
        with self._lock:
            errors = []
            for artifact in (self._stdout, self._stderr):
                try:
                    artifact.close()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise errors[0]

    def mark_running(self):
        with self._lock:
            if self.status in TERMINAL_STATES:
                return
            self.started_at = _utcnow()
            if self._cancel.is_set():
                self.status = TaskStatus.CANCELLING
            else:
                self.status = TaskStatus.RUNNING

    def finish(self, status, result, error=None):
        status = TaskStatus(status)
        # A normal terminal result should never leave an owner request parked;
        # this also covers a runner that failed while a gate was visible.
        self._cancel_interactions(reason="task 已结束")
        clean_errors = []
        if error:
            raw_error = (
                f"{type(error).__name__}: {error}"
                if isinstance(error, BaseException) else str(error))
            clean_errors.append(self.redact_result(raw_error))
        close_error = None
        try:
            self.close_artifacts()
        except BaseException as exc:
            close_error = exc
            clean_errors.append(self.redact_result(
                f"{type(exc).__name__}: {exc}"))
            if status != TaskStatus.CANCELLED:
                status = TaskStatus.FAILED
        result = str(result)
        if close_error is not None:
            result += (
                "\n[artifact 收尾失败: "
                f"{clean_errors[-1]}]")
        with self._lock:
            self.status = status
            self.result = result
            if clean_errors:
                self.error = " · ".join(clean_errors)
            self.ended_at = _utcnow()
            try:
                self.manager._record_artifact_index(self.snapshot())
            except BaseException as exc:
                clean = self.redact_result(
                    f"{type(exc).__name__}: {exc}")
                self.result += f"\n[artifact index 写入失败: {clean}]"
                self.error = " · ".join(
                    value for value in (self.error, clean) if value)
            self._ui_final = True
            self._done.set()

    def snapshot(self):
        with self._lock:
            status = self.status
            return TaskSnapshot(
                id=self.id,
                key=self.key,
                session=self.session,
                name=self.name,
                background=bool(self.background),
                backgrounded_at=self.backgrounded_at,
                status=status,
                created_at=self.created_at,
                started_at=self.started_at,
                ended_at=self.ended_at,
                cancel_requested_at=self.cancel_requested_at,
                process_pid=self.process_pid,
                returncode=self.returncode,
                signal=self.signal,
                timed_out=bool(self.timed_out),
                stdout_path=(
                    str(self._stdout.path)
                    if self._stdout.path.is_file() else None),
                stderr_path=(
                    str(self._stderr.path)
                    if self._stderr.path.is_file() else None),
                stdout_bytes=self._stdout.byte_count,
                stderr_bytes=self._stderr.byte_count,
                stdout_lines=self._stdout.line_count,
                stderr_lines=self._stderr.line_count,
                stdout_sha256=self._stdout.sha256,
                stderr_sha256=self._stderr.sha256,
                truncated=(
                    self._stdout.byte_count > 20_000
                    or self._stderr.byte_count > 10_000),
                ui_dropped_bytes=self._ui_dropped,
                note_dropped=self._note_dropped,
                runtime_event_dropped=self._runtime_event_dropped,
                redaction_count=self._redaction_count,
                error=self.error,
            )


class TaskManager:
    """单 foreground、多 background registry；观察接口返回不可变 snapshot。"""

    def __init__(self, artifact_root, runner, *, session_getter=None,
                 id_factory=None, poll_interval=0.025,
                 ui_ring_bytes=64 * 1024, max_history=128,
                 interrupt_grace=0.75, terminate_grace=2.0,
                 secret_values=None, legacy_artifact_getter=None):
        self.artifact_root = _validate_artifact_root(artifact_root)
        self.runner = runner
        self.session_getter = session_getter or (lambda: "")
        self.id_factory = id_factory or (
            lambda: uuid.uuid4().hex[:8])
        self.poll_interval = max(0.001, float(poll_interval))
        self.ui_ring_bytes = max(0, int(ui_ring_bytes))
        self.max_history = max(1, int(max_history))
        self.interrupt_grace = max(0.0, float(interrupt_grace))
        self.terminate_grace = max(0.0, float(terminate_grace))
        self.secret_values = tuple(
            _environment_secrets()
            if secret_values is None else secret_values)
        self.legacy_artifact_getter = legacy_artifact_getter
        self._lock = threading.RLock()
        self._tasks = collections.OrderedDict()
        self._by_key = {}
        self._foreground_id = None
        self._pending_cancel = set()
        self._background_drain_lock = threading.Lock()
        self._artifact_index_lock = threading.RLock()

    def _artifact_session_dir(self, session):
        path = self.artifact_root / _safe_component(
            session, "unknown-session")
        if path.is_symlink():
            raise TaskError(
                f"artifact session 目录不能是符号链接：{path}")
        return path

    def _artifact_index_path(self, session):
        return self._artifact_session_dir(session) / ARTIFACT_INDEX_NAME

    def _read_artifact_index(self, session):
        session = str(session or "")
        path = self._artifact_index_path(session)
        if path.is_symlink():
            raise TaskError(f"artifact index 不能是符号链接：{path}")
        if not path.exists():
            return {
                "version": ARTIFACT_INDEX_VERSION,
                "session": session,
                "updated_at": None,
                "entries": [],
            }
        if not path.is_file():
            raise TaskError(f"artifact index 不是普通文件：{path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskError(f"artifact index 损坏：{path}：{exc}") from exc
        if (not isinstance(value, dict)
                or value.get("version") != ARTIFACT_INDEX_VERSION
                or str(value.get("session") or "") != session
                or not isinstance(value.get("entries"), list)):
            raise TaskError(f"artifact index schema 不兼容：{path}")
        return value

    def _write_artifact_index(self, session, value):
        directory = self._artifact_session_dir(session)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise TaskError(
                f"artifact session 目录不能是符号链接：{directory}")
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        path = directory / ARTIFACT_INDEX_NAME
        if path.is_symlink():
            raise TaskError(f"artifact index 不能是符号链接：{path}")
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        temporary = directory / (
            f".{ARTIFACT_INDEX_NAME}.{uuid.uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _artifact_relative_path(self, value):
        path = Path(value).absolute()
        if not _under(path, self.artifact_root):
            raise TaskError(
                f"artifact path 越出 root：{path}")
        try:
            return path.relative_to(self.artifact_root).as_posix()
        except ValueError as exc:
            raise TaskError(
                f"artifact path 无法相对化：{path}") from exc

    def _record_artifact_index(self, snapshot):
        streams = {}
        for stream in ("stdout", "stderr"):
            path = getattr(snapshot, f"{stream}_path")
            if not path:
                continue
            streams[stream] = {
                "relative_path": self._artifact_relative_path(path),
                "byte_count": int(
                    getattr(snapshot, f"{stream}_bytes") or 0),
                "line_count": int(
                    getattr(snapshot, f"{stream}_lines") or 0),
                "sha256": getattr(snapshot, f"{stream}_sha256"),
            }
        entry = {
            "tool_call_id": str(snapshot.key),
            "task_id": str(snapshot.id),
            "tool": str(snapshot.name),
            "status": str(snapshot.status.value),
            "returncode": snapshot.returncode,
            "timed_out": bool(snapshot.timed_out),
            "created_at": snapshot.created_at,
            "ended_at": snapshot.ended_at,
            "streams": streams,
        }
        with self._artifact_index_lock:
            index = self._read_artifact_index(snapshot.session)
            entries = [
                row for row in index["entries"]
                if not (isinstance(row, dict)
                        and (str(row.get("task_id") or "") == snapshot.id
                             or str(row.get("tool_call_id") or "")
                             == snapshot.key))
            ]
            entries.append(entry)
            index.update({
                "updated_at": _utcnow(),
                "entries": entries,
            })
            self._write_artifact_index(snapshot.session, index)
        return entry

    def artifact_entries(self, session=None):
        selected = str(session or self.session_getter() or "")
        with self._artifact_index_lock:
            value = self._read_artifact_index(selected)
        current = [dict(row) for row in value["entries"]
                   if isinstance(row, dict)]
        legacy = self._legacy_artifact_entries(selected)
        merged = collections.OrderedDict()
        for row in legacy + current:
            call_id = str(row.get("tool_call_id") or "")
            task_id = str(row.get("task_id") or "")
            key = ("call", call_id) if call_id else ("task", task_id)
            if call_id or task_id:
                merged[key] = row
        return list(merged.values())

    @staticmethod
    def _legacy_task_id(paths, fallback):
        found = []
        for value in paths:
            name = Path(str(value or "")).name
            for suffix in (".stdout.log", ".stderr.log"):
                if name.endswith(suffix):
                    found.append(name[:-len(suffix)])
                    break
        unique = {value for value in found if value}
        return unique.pop() if len(unique) == 1 else str(fallback or "")

    def _legacy_artifact_entries(self, session):
        getter = self.legacy_artifact_getter
        if not callable(getter):
            return []
        try:
            rows = getter(session) or []
        except Exception as exc:
            raise TaskError(
                f"legacy artifact index 读取失败：{exc}") from exc
        entries = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            call_id = str(row.get("tool_call_id") or "").strip()
            paths = [row.get("stdout_path"), row.get("stderr_path")]
            task_id = self._legacy_task_id(paths, row.get("id"))
            if not call_id or not task_id:
                continue
            streams = {}
            for stream in ("stdout", "stderr"):
                raw_path = row.get(f"{stream}_path")
                if not raw_path:
                    continue
                relative = self._artifact_relative_path(raw_path)
                path = self.artifact_root / relative
                size = (
                    path.stat().st_size
                    if path.is_file() and not path.is_symlink() else 0)
                streams[stream] = {
                    "relative_path": relative,
                    "byte_count": size,
                    "line_count": None,
                    "sha256": None,
                }
            if not streams:
                continue
            entries.append({
                "tool_call_id": call_id,
                "task_id": task_id,
                "tool": str(row.get("name") or "tool"),
                "status": str(row.get("status") or "unknown"),
                "returncode": row.get("exit_code"),
                "timed_out": False,
                "created_at": row.get("started_at"),
                "ended_at": row.get("ended_at"),
                "streams": streams,
                "index_source": "metrics-read-only",
            })
        return entries

    def artifact_entry(self, identifier, session=None):
        hint = str(identifier or "").strip()
        if not hint:
            return None
        rows = self.artifact_entries(session=session)
        exact = [row for row in rows if hint in {
            str(row.get("tool_call_id") or ""),
            str(row.get("task_id") or ""),
        }]
        if len(exact) == 1:
            return exact[0]
        matches = [row for row in rows if any(
            str(row.get(key) or "").startswith(hint)
            for key in ("tool_call_id", "task_id"))]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise TaskError(
                f"artifact id 前缀 {hint!r} 匹配 {len(matches)} 项")
        return None

    def _indexed_artifact_path(self, relative_path):
        raw = Path(str(relative_path or ""))
        if (not relative_path or raw.is_absolute()
                or ".." in raw.parts):
            raise TaskError("artifact index 含不安全 relative_path")
        path = self.artifact_root / raw
        if not _under(path, self.artifact_root) or path.is_symlink():
            raise TaskError(
                f"artifact index path 不安全：{relative_path}")
        return path

    def artifact_page(self, identifier, *, page=1,
                      page_bytes=ARTIFACT_PAGE_BYTES, session=None):
        entry = self.artifact_entry(identifier, session=session)
        if entry is None:
            return None
        try:
            page = int(page)
        except (TypeError, ValueError, OverflowError):
            raise TaskError("artifact page 必须是正整数") from None
        if page < 1:
            raise TaskError("artifact page 必须是正整数")
        limit = max(4_096, min(100_000, int(page_bytes)))
        available = []
        missing = []
        streams = entry.get("streams") or {}
        for name in ("stdout", "stderr"):
            meta = streams.get(name)
            if not isinstance(meta, dict):
                continue
            path = self._indexed_artifact_path(meta.get("relative_path"))
            if not path.is_file():
                missing.append(name)
                continue
            available.append((name, path, path.stat().st_size))
        total = sum(size for _name, _path, size in available)
        offset = (page - 1) * limit
        if total and offset >= total:
            raise TaskError(
                f"artifact 只有 {(total + limit - 1) // limit} 页")
        remaining = limit
        cursor = offset
        sections = []
        for name, path, size in available:
            if cursor >= size:
                cursor -= size
                continue
            start = cursor
            count = min(remaining, size - start)
            text, actual_start, actual_end = _read_utf8_window(
                path, start, count)
            sections.append({
                "stream": name,
                "text": text,
                "start": actual_start,
                "end": actual_end,
                "total_bytes": size,
            })
            remaining -= count
            cursor = 0
            if remaining <= 0:
                break
        end = min(total, offset + (limit - remaining))
        return {
            "entry": entry,
            "page": page,
            "page_bytes": limit,
            "start": min(offset, total),
            "end": end,
            "total_bytes": total,
            "has_more": end < total,
            "sections": sections,
            "missing": missing,
        }

    def _paths(self, session, task_id):
        session_dir = self.artifact_root / _safe_component(
            session, "unknown-session")
        return (
            session_dir / f"{task_id}.stdout.log",
            session_dir / f"{task_id}.stderr.log",
        )

    def _new_task(self, name, key):
        session = str(self.session_getter() or "")
        task_id = _safe_component(self.id_factory(), "task")
        key = str(key or task_id)
        with self._lock:
            if self._foreground_id is not None:
                current = self._tasks.get(self._foreground_id)
                if current is not None and not current.is_done():
                    raise TaskError(
                        f"已有 foreground task：{current.id}")
            if task_id in self._tasks:
                raise TaskError(f"重复 task id：{task_id}")
            stdout_path, stderr_path = self._paths(session, task_id)
            task = TaskHandle(
                self, task_id=task_id, key=key, session=session,
                name=str(name), stdout_path=stdout_path,
                stderr_path=stderr_path)
            self._tasks[task_id] = task
            self._by_key[key] = task_id
            self._foreground_id = task_id
            pending = key in self._pending_cancel
            self._pending_cancel.discard(key)
        if pending:
            task.cancel()
        return task

    def _finish_task(self, task):
        with self._lock:
            if self._foreground_id == task.id:
                self._foreground_id = None
            # background terminal 必须先经主线程 drain/journal 才能淘汰；否则
            # 多个短任务在一次 UI poll 前结束时，max_history 会静默丢结果。
            terminal = [
                task_id for task_id, item in self._tasks.items()
                if (item.is_done()
                    and (not item.is_background()
                         or item.background_terminal_emitted()))
            ]
            while len(terminal) > self.max_history:
                task_id = terminal.pop(0)
                old = self._tasks.pop(task_id, None)
                if old is not None and self._by_key.get(old.key) == task_id:
                    self._by_key.pop(old.key, None)

    def _worker(self, task, name, args, context):
        try:
            self._worker_body(task, name, args, context)
        except BaseException as exc:
            # error handling 本身也可能撞到 artifact write/close 错误；worker
            # 最外层必须保证主线程最终看到 terminal，而不是永久 progress。
            status = (
                TaskStatus.CANCELLED
                if task.is_cancelled() else TaskStatus.FAILED)
            try:
                failure = task.redact_result(
                    f"[执行失败: {type(exc).__name__}: {exc}]")
            except BaseException:
                failure = f"[执行失败: {type(exc).__name__}]"
            try:
                task.finish(status, failure, error=exc)
            except BaseException as finish_exc:
                # finish() 已设计为不因 artifact close 失败而抛出；这里仍保留
                # 最后一道纯内存兜底，抵抗未来实现或 monkeypatch 回归。
                try:
                    clean_error = task.redact_result(
                        f"{type(exc).__name__}: {exc} · "
                        f"terminal {type(finish_exc).__name__}: {finish_exc}")
                except BaseException:
                    clean_error = (
                        f"{type(exc).__name__} · "
                        f"terminal {type(finish_exc).__name__}")
                with task._lock:
                    task.status = status
                    task.result = failure
                    task.error = clean_error
                    task.ended_at = _utcnow()
                    task._ui_final = True
                    task._done.set()
        finally:
            # background generator 已经退出时，worker 仍是 lifecycle owner。
            self._finish_task(task)

    def _worker_body(self, task, name, args, context):
        task.mark_running()
        if task.is_cancelled():
            task.finish(
                TaskStatus.CANCELLED, "[用户中断，工具尚未启动]")
            return
        try:
            result = self.runner(
                name, args, on_note=task.note,
                context=context, task=task)
            redacted = task.write_result_artifact(result)
            if redacted is not None:
                result = redacted
        except BaseException as exc:                 # worker 边界必须可观察
            result = f"[执行失败: {type(exc).__name__}: {exc}]"
            redacted = task.write_result_artifact(result)
            if redacted is not None:
                result = redacted
            status = TaskStatus.CANCELLED if task.is_cancelled() else TaskStatus.FAILED
            task.finish(status, result, error=exc)
            return
        if task.is_cancelled():
            status = TaskStatus.CANCELLED
        elif task.timed_out or task.error:
            status = TaskStatus.FAILED
        else:
            status = TaskStatus.COMPLETED
        task.finish(status, result)

    @staticmethod
    def _output_event(task, stream, value):
        snap = task.snapshot()
        return {
            "t": "output", "stream": stream, "v": value,
            "task_id": task.id,
            "stdout_bytes": snap.stdout_bytes,
            "stderr_bytes": snap.stderr_bytes,
        }

    def _drain_events(self, task):
        events = []
        # Interactive requests are lossless and must be delivered before the
        # ordinary (bounded) runtime/output streams.  The owner can therefore
        # render a modal while the worker remains blocked without touching
        # TerminalRenderer from that worker.
        for request in task.drain_interactions():
            events.append({
                "t": "interaction_request",
                "task_id": task.id,
                **request,
            })
        for runtime_event in task.drain_runtime_events():
            events.append({
                "t": "runtime", "event": runtime_event,
                "task_id": task.id,
            })
        for note in task.drain_notes():
            events.append({
                "t": "note", "v": note,
                "task_id": task.id,
            })
        for stream, value in task.drain_output():
            events.append(self._output_event(task, stream, value))
        return events

    @staticmethod
    def _terminal_event(task):
        snapshot = task.snapshot()
        return {
            "t": snapshot.status.value,
            "v": task.result,
            "task_id": task.id,
            "task": snapshot.as_dict(),
        }

    def _launch_worker(self, task, name, args, context):
        try:
            # core.tools.PreparedArguments is an immutable Mapping carrying the
            # exact post-hook before-image/capture.  Converting it to dict here
            # would silently discard that durability contract.
            worker_args = (
                args if getattr(args, "tool_name", None) == name
                and hasattr(args, "arguments_json")
                else dict(args)
            )
            worker = threading.Thread(
                target=self._worker,
                args=(task, name, worker_args, context),
                name=f"zylab-task-{task.id}", daemon=True)
            task._worker = worker
            worker.start()
            return worker
        except BaseException as exc:
            task._worker = None
            try:
                failure = task.redact_result(
                    f"[worker 启动失败: {type(exc).__name__}: {exc}]")
            except BaseException:
                failure = f"[worker 启动失败: {type(exc).__name__}]"
            try:
                task.finish(TaskStatus.FAILED, failure, error=exc)
            except BaseException as finish_exc:
                try:
                    clean_error = task.redact_result(
                        f"{type(exc).__name__}: {exc} · "
                        f"terminal {type(finish_exc).__name__}: {finish_exc}")
                except BaseException:
                    clean_error = (
                        f"{type(exc).__name__} · "
                        f"terminal {type(finish_exc).__name__}")
                with task._lock:
                    task.status = TaskStatus.FAILED
                    task.result = failure
                    task.error = clean_error
                    task.ended_at = _utcnow()
                    task._ui_final = True
                    task._done.set()
            self._finish_task(task)
            return None

    def run(self, name, args, *, context=None, task_key=None,
            poll_interval=None):
        """返回 started/output/progress/terminal 事件；started 后才启动 worker。"""
        task = self._new_task(name, task_key)
        interval = (
            self.poll_interval if poll_interval is None
            else max(0.001, float(poll_interval)))
        advanced = False
        try:
            yield {
                "t": "started",
                "task_id": task.id,
                "task_key": task.key,
                "task": task.snapshot().as_dict(),
            }
            advanced = True
        finally:
            if not advanced:
                # started 已交给主线程，若它随后明确 background 后关闭
                # generator，background 的 worker 仍必须启动。普通 close
                # 则保持 foreground generator 的取消语义。
                if task.is_background() and not task.is_done():
                    if task.is_cancelled():
                        task.mark_running()
                        task.finish(
                            TaskStatus.CANCELLED,
                            "[用户中断，工具尚未启动]")
                        self._finish_task(task)
                    else:
                        self._launch_worker(
                            task, name, args, context)
                elif not task.is_done():
                    task.cancel()
                    task.mark_running()
                    task.finish(
                        TaskStatus.CANCELLED,
                        "[consumer 已关闭，工具尚未启动]")
                    self._finish_task(task)
        if task.is_cancelled():
            task.mark_running()
            task.finish(
                TaskStatus.CANCELLED, "[用户中断，工具尚未启动]")
        else:
            self._launch_worker(task, name, args, context)
        try:
            while not task.is_done():
                if task.is_background():
                    task.announce_background()
                    snapshot = task.snapshot()
                    yield {
                        "t": "backgrounded",
                        "v": (
                            f"[任务 {task.id} 已转后台；结束后结果会自动作为一条消息交回你，不必轮询；用 /tasks 查看，"
                            f"/task {task.id} 查看输出，/kill {task.id} 终止]"),
                        "task_id": task.id,
                        "task": snapshot.as_dict(),
                    }
                    return
                emitted = False
                for event in self._drain_events(task):
                    emitted = True
                    yield event
                if not emitted:
                    task._done.wait(interval)
                    if not task.is_done():
                        snap = task.snapshot()
                        yield {
                            "t": "progress",
                            "task_id": task.id,
                            "status": snap.status.value,
                            "stdout_bytes": snap.stdout_bytes,
                            "stderr_bytes": snap.stderr_bytes,
                        }
            # background 与 worker terminal 可能在相邻线程 tick 同时发生；
            # background() 成功后必须由后台通道报告 terminal。
            if task.is_background():
                task.announce_background()
                snapshot = task.snapshot()
                yield {
                    "t": "backgrounded",
                    "v": (
                        f"[任务 {task.id} 已转后台；结束后结果会自动作为一条消息交回你，不必轮询；用 /tasks 查看，"
                        f"/task {task.id} 查看输出，/kill {task.id} 终止]"),
                    "task_id": task.id,
                    "task": snapshot.as_dict(),
                }
                return
            for event in self._drain_events(task):
                yield event
            yield self._terminal_event(task)
        finally:
            if task.is_background():
                # 即使 consumer 在 backgrounded yield 前 close，后台 drain
                # 仍可接管，不会把 worker 留在不可观察状态。
                task.announce_background()
            elif not task.is_done():
                task.cancel()
                worker = task._worker
                if worker is not None:
                    worker.join(
                        timeout=self.interrupt_grace
                        + self.terminate_grace + 1.0)
            self._finish_task(task)

    def background(self, identifier=None):
        """把 active foreground bash 转后台；worker 与 artifact 持续运行。"""
        with self._lock:
            task_id = self._foreground_id
            if identifier is not None:
                value = str(identifier or "")
                resolved = (
                    value if value in self._tasks
                    else self._by_key.get(value))
                if resolved != task_id:
                    raise TaskError("只能把当前 foreground task 转后台")
            task = self._tasks.get(task_id) if task_id else None
            if task is None or task.is_done():
                raise TaskError("当前没有 active foreground task")
            if task.name != "bash":
                raise TaskError("只有 foreground bash 可以转后台")
            if not task.mark_background():
                raise TaskError(
                    f"task {task.id} 已转后台、正在取消或已经结束")
            self._foreground_id = None
            # ownership 在 background() 返回时即转移；调用方即使随后关闭
            # run generator，drain_background() 也能马上接管。
            task.announce_background()
        return task.snapshot()

    def drain_background(self, *, defer_terminal_ids=None):
        """非阻塞取走后台事件；可暂缓指定 task 的 terminal claim。

        defer 只 gate terminal/prune，不阻止 output、note 或 runtime。调用方
        解除 defer 后，原 terminal 仍会恰好返回一次。默认行为与旧接口相同。
        """
        if isinstance(defer_terminal_ids, (str, bytes)):
            deferred = {str(defer_terminal_ids)}
        else:
            deferred = {
                str(task_id) for task_id in (defer_terminal_ids or ())
            }
        with self._background_drain_lock:
            with self._lock:
                managed = [
                    task for task in self._tasks.values()
                    if task.is_background()
                    and task.background_announced()
                ]
            events = []
            for task in managed:
                events.extend(self._drain_events(task))
                if (task.id not in deferred
                        and task.claim_background_terminal()):
                    events.append(self._terminal_event(task))
                    # terminal 已进入主线程事件批次，现在才允许 history prune。
                    self._finish_task(task)
            return events

    @staticmethod
    def _artifact_tail(artifact, limit):
        with artifact.path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            total = stream.tell()
            nominal_start = max(0, total - limit)
            # UTF-8 code points are at most four bytes.  Read up to three
            # bytes before the nominal byte tail so a boundary that lands on
            # a continuation byte can be moved back to its leading byte.
            # Invalid bytes still render as replacement characters; valid
            # UTF-8 must never be corrupted merely by the requested limit.
            probe_start = max(0, nominal_start - 3)
            stream.seek(probe_start)
            probe = stream.read(limit + (nominal_start - probe_start))
        boundary = nominal_start - probe_start
        while (boundary > 0 and boundary < len(probe)
               and probe[boundary] & 0xC0 == 0x80):
            boundary -= 1
        data = probe[boundary:]
        start = probe_start + boundary
        return {
            "text": data.decode("utf-8", "replace"),
            "path": str(artifact.path),
            "total_bytes": total,
            "returned_bytes": len(data),
            "truncated": start > 0,
        }

    def tail(self, identifier, limit=8192):
        """非破坏读取 task 的 stdout/stderr artifact 尾部。"""
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise TaskError("tail limit 必须是正整数") from exc
        if limit <= 0:
            raise TaskError("tail limit 必须是正整数")
        identifier = str(identifier or "")
        with self._lock:
            task_id = (
                identifier if identifier in self._tasks
                else self._by_key.get(identifier))
            task = self._tasks.get(task_id) if task_id else None
        if task is None:
            raise TaskError(f"未知 task：{identifier}")

        streams = {}
        for stream, artifact in (
                ("stdout", task._stdout), ("stderr", task._stderr)):
            try:
                streams[stream] = self._artifact_tail(artifact, limit)
            except FileNotFoundError:
                streams[stream] = {
                    "text": "", "path": None, "total_bytes": 0,
                    "returned_bytes": 0, "truncated": False,
                }
        return {"task": task.snapshot().as_dict(), **streams}

    def shutdown(self):
        """取消并等待所有 managed task；用于 CLI lifetime 收尾。"""
        with self._lock:
            managed = list(self._tasks.values())
        active = [task for task in managed if not task.is_done()]
        for task in active:
            task.cancel()
        for task in active:
            worker = task._worker
            if worker is None:
                task.finish(
                    TaskStatus.CANCELLED,
                    "[Zylab 退出，工具尚未启动]")
                self._finish_task(task)
                continue
            worker.join(timeout=(
                self.interrupt_grace + self.terminate_grace + 1.5))
            if worker.is_alive():
                task._send_group(signal.SIGKILL)
                worker.join(timeout=1.0)
            self._finish_task(task)
        return [task.snapshot() for task in managed]

    close = shutdown

    def cancel(self, identifier):
        """按 task id 或稳定 task key 取消；尚未 create 时记录 pre-cancel。"""
        identifier = str(identifier or "")
        with self._lock:
            task_id = (
                identifier if identifier in self._tasks
                else self._by_key.get(identifier))
            task = self._tasks.get(task_id) if task_id else None
            if task is None:
                self._pending_cancel.add(identifier)
                return None
        task.cancel()
        return task.snapshot()

    def get(self, identifier):
        identifier = str(identifier or "")
        with self._lock:
            task_id = (
                identifier if identifier in self._tasks
                else self._by_key.get(identifier))
            task = self._tasks.get(task_id) if task_id else None
        return task.snapshot() if task is not None else None

    def resolve_interaction(self, task_identifier, request_id, result):
        """Resolve a worker interaction without exposing TaskHandle publicly."""
        identifier = str(task_identifier or "")
        with self._lock:
            task_id = (
                identifier if identifier in self._tasks
                else self._by_key.get(identifier))
            task = self._tasks.get(task_id) if task_id else None
        if task is None:
            raise TaskError(f"interaction task 不存在: {task_identifier}")
        if not task.resolve_interaction(request_id, result):
            raise TaskError(
                f"interaction 已结束或不存在：{request_id}")
        return True

    def pending_interactions(self, identifier=None):
        """Owner-side diagnostics; never returns mutable task internals."""
        if identifier is None:
            with self._lock:
                tasks = list(self._tasks.values())
        else:
            value = str(identifier or "")
            with self._lock:
                task_id = (
                    value if value in self._tasks else self._by_key.get(value))
                task = self._tasks.get(task_id) if task_id else None
                tasks = [task] if task is not None else []
        out = []
        for task in tasks:
            for request in task.pending_interactions():
                out.append({"task_id": task.id, **request})
        return tuple(out)

    def foreground(self):
        with self._lock:
            task = self._tasks.get(self._foreground_id)
        return task.snapshot() if task is not None else None

    def list(self):
        with self._lock:
            tasks = list(self._tasks.values())
        return [task.snapshot() for task in reversed(tasks)]
