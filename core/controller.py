"""M3a 的串行会话控制器。

Controller 不读 stdin、不写 stdout，也不自行启动 provider/tool。它先把用户意图
和生命周期事件交给 StateStore，再返回可执行 action；调用方只有拿到 action 后
才能做网络或工具副作用。这个顺序让持久化失败保持 fail-closed。
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum

from . import client
from .state import utcnow


# Internal continuation prompts use the same durable queue as ordinary user
# input, but carry a small authenticated-by-construction envelope so a
# restarted controller can distinguish them from human text.  The envelope
# is decoded before it reaches Agent/provider context; the NUL prefix cannot
# be produced by the terminal line editor and is rejected on malformed input.
_INTERNAL_PREFIX = "\x00zylab-internal-v1:"
_INTERNAL_ORIGINS = frozenset({"goal"})
_INTERNAL_ENVELOPE_MAX = 16 * 1024
_DECISION_GATE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def _encode_internal(text, origin, metadata):
    payload = {
        "origin": str(origin),
        "metadata": dict(metadata or {}),
        "text": str(text),
    }
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ControllerError(
            f"内部 queue metadata 不可序列化：{type(exc).__name__}") from None
    if (len(encoded.encode("utf-8", "surrogatepass"))
            > _INTERNAL_ENVELOPE_MAX or "\x00" in encoded):
        raise ControllerError("内部 queue metadata 超出有界范围")
    return _INTERNAL_PREFIX + encoded


def _decode_input(value):
    raw = str(value)
    if not raw.startswith(_INTERNAL_PREFIX):
        return raw, "user", {}
    try:
        payload_bytes = raw[len(_INTERNAL_PREFIX):].encode(
            "utf-8", "surrogatepass")
    except UnicodeError:
        raise ControllerError("内部 queue envelope 编码无效") from None
    if len(payload_bytes) > _INTERNAL_ENVELOPE_MAX:
        raise ControllerError("内部 queue envelope 超出有界范围")
    try:
        payload = json.loads(raw[len(_INTERNAL_PREFIX):])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ControllerError(f"内部 queue envelope 无效：{exc}") from None
    if not isinstance(payload, dict):
        raise ControllerError("内部 queue envelope 必须是 object")
    origin = str(payload.get("origin") or "")
    metadata = payload.get("metadata") or {}
    text = payload.get("text")
    try:
        metadata_json = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True,
            allow_nan=False)
    except (TypeError, ValueError):
        raise ControllerError("内部 queue envelope metadata 无效") from None
    if (origin not in _INTERNAL_ORIGINS or not isinstance(metadata, dict)
            or "\x00" in metadata_json):
        raise ControllerError("内部 queue envelope origin/metadata 无效")
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise ControllerError("内部 queue envelope text 无效")
    return text, origin, dict(metadata)


class ControllerError(RuntimeError):
    """Controller 收到与当前状态不一致的操作。"""


class InvalidTransition(ControllerError):
    """非法状态转换。"""


class ControllerState(str, Enum):
    IDLE = "IDLE"
    MODEL_STREAMING = "MODEL_STREAMING"
    TOOL_WAITING_APPROVAL = "TOOL_WAITING_APPROVAL"
    TOOL_RUNNING_FOREGROUND = "TOOL_RUNNING_FOREGROUND"
    TOOL_WAITING_DECISION = "TOOL_WAITING_DECISION"
    TOOL_RUNNING_BACKGROUND = "TOOL_RUNNING_BACKGROUND"
    PICKER = "PICKER"
    REWIND = "REWIND"
    EXITING = "EXITING"


class QueueMode(str, Enum):
    STEER = "steer"
    NEXT_TURN = "next_turn"
    COMMAND = "command"
    SHELL = "shell"


class ActionKind(str, Enum):
    START_TURN = "start_turn"
    DELIVER_STEER = "deliver_steer"
    START_REQUEST = "start_request"
    CANCEL_REQUEST = "cancel_request"
    CANCEL_TOOL = "cancel_tool"
    REVIEW_TOOLS = "review_tools"
    START_TOOL = "start_tool"
    CONTINUE_TURN = "continue_turn"
    RUN_COMMAND = "run_command"
    RUN_SHELL = "run_shell"


@dataclass(frozen=True)
class QueuedInput:
    id: str
    mode: QueueMode
    text: str
    enqueued_at: str
    origin: str = "user"
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row):
        text, origin, metadata = _decode_input(row["text"])
        return cls(
            id=str(row["id"]),
            mode=QueueMode(row["mode"]),
            text=text,
            enqueued_at=str(row["enqueued_at"]),
            origin=origin,
            metadata=metadata,
        )


@dataclass(frozen=True)
class ControllerAction:
    kind: ActionKind
    turn_id: str | None = None
    request_id: str | None = None
    item: QueuedInput | None = None
    cancel: client.CancellationHandle | None = None
    payload: dict = field(default_factory=dict)


class MemoryJournal:
    """未启用 SQLite shadow 时的进程内 queue/event 适配器。

    它保持与 StateStore 相同的事务可见顺序，但不宣称跨进程恢复；启用 shadow
    时 SessionController 会直接使用 StateStore。
    """

    def __init__(self):
        self.events = []
        self._items = {}
        self._seq = 0

    def append_events(self, session_id, events):
        """原子追加一批 event —— StateStore.append_events 的进程内镜像。

        那边靠事务回滚保证「要么全进要么全不进」；这里没有事务，靠**先算后写**：
        整批先校验、先深拷贝，任何一条不合法都在改动 events/_seq 之前抛出。
        深拷贝是必须的 —— 浅拷贝下调用方追加后再改嵌套 payload，审计记录就
        跟着变了。"""
        prepared = []
        for event in events:
            if not isinstance(event, dict) or not event.get("kind"):
                raise ControllerError("event 必须是带 kind 的 dict")
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                raise ControllerError("event.payload 必须是 dict")
            try:
                payload = copy.deepcopy(payload)
            except Exception as exc:  # noqa: BLE001 - deepcopy 的失败类型不统一
                raise ControllerError(
                    f"event.payload 无法复制（{type(exc).__name__}），"
                    "拒绝整批追加") from exc
            prepared.append(
                (str(event["kind"]), payload, event.get("turn_id")))
        base = self._seq
        rows = [
            {
                "seq": base + index,
                "session_id": str(session_id),
                "kind": kind,
                "payload": payload,
                "turn_id": turn_id,
            }
            for index, (kind, payload, turn_id) in enumerate(prepared, 1)
        ]
        # 到这里之前没有任何副作用；下面两行之间不会抛异常。
        self.events.extend(rows)
        self._seq = base + len(rows)
        return [copy.deepcopy(row) for row in rows]

    def enqueue_input(self, session_id, item_id, mode, text, *,
                      enqueued_at=None):
        key = (str(session_id), str(item_id))
        if key in self._items:
            raise ControllerError(f"queued input 已存在: {item_id}")
        at = enqueued_at or utcnow()
        self.append_events(session_id, [{
            "kind": "queued_input_added",
            "payload": {"id": str(item_id), "mode": str(mode), "text": text},
        }])
        row = {
            "id": str(item_id), "session_id": str(session_id),
            "mode": str(mode), "text": text, "state": "queued",
            "enqueued_at": at, "dispatched_at": None,
        }
        self._items[key] = row
        return dict(row)

    def dispatch_queued_input(self, session_id, item_id, *, turn_id=None,
                              dispatched_at=None, events=()):
        key = (str(session_id), str(item_id))
        row = self._items.get(key)
        if row is None:
            raise ControllerError(f"queued input 不存在: {item_id}")
        if row["state"] != "queued":
            raise ControllerError(
                f"queued input {item_id} 已是 {row['state']}")
        row["state"] = "dispatched"
        row["dispatched_at"] = dispatched_at or utcnow()
        batch = [{
            "kind": "queued_input_dispatched",
            "payload": {"id": row["id"], "mode": row["mode"]},
            "turn_id": turn_id,
        }, *events]
        return self.append_events(session_id, batch)

    def cancel_queued_input(self, session_id, item_id, *, cancelled_at=None):
        key = (str(session_id), str(item_id))
        row = self._items.get(key)
        if row is None:
            raise ControllerError(f"queued input 不存在: {item_id}")
        if row["state"] != "queued":
            raise ControllerError(
                f"queued input {item_id} 已是 {row['state']}")
        row["state"] = "cancelled"
        return self.append_events(session_id, [{
            "kind": "queued_input_cancelled",
            "payload": {"id": row["id"], "mode": row["mode"]},
        }])[0]

    def list_queued_inputs(self, session_id, *, state="queued"):
        rows = [
            dict(row) for (sid, _), row in self._items.items()
            if sid == str(session_id)
            and (state is None or row["state"] == state)
        ]
        return sorted(
            rows, key=lambda row: (row["enqueued_at"], row["id"]))


_ALLOWED_TRANSITIONS = {
    ControllerState.IDLE: {
        ControllerState.MODEL_STREAMING,
        ControllerState.TOOL_RUNNING_FOREGROUND,
        ControllerState.PICKER,
        ControllerState.REWIND,
        ControllerState.EXITING,
    },
    ControllerState.MODEL_STREAMING: {
        ControllerState.IDLE,
        ControllerState.TOOL_WAITING_APPROVAL,
        ControllerState.TOOL_RUNNING_FOREGROUND,
        ControllerState.EXITING,
    },
    ControllerState.TOOL_WAITING_APPROVAL: {
        ControllerState.IDLE,
        ControllerState.MODEL_STREAMING,
        ControllerState.TOOL_RUNNING_FOREGROUND,
        ControllerState.EXITING,
    },
    ControllerState.TOOL_RUNNING_FOREGROUND: {
        ControllerState.IDLE,
        ControllerState.MODEL_STREAMING,
        ControllerState.TOOL_WAITING_APPROVAL,
        ControllerState.TOOL_WAITING_DECISION,
        ControllerState.TOOL_RUNNING_BACKGROUND,
        ControllerState.EXITING,
    },
    ControllerState.TOOL_WAITING_DECISION: {
        ControllerState.TOOL_RUNNING_FOREGROUND,
        ControllerState.TOOL_WAITING_APPROVAL,
        ControllerState.MODEL_STREAMING,
        ControllerState.IDLE,
        ControllerState.EXITING,
    },
    ControllerState.TOOL_RUNNING_BACKGROUND: {
        ControllerState.IDLE,
        ControllerState.MODEL_STREAMING,
        ControllerState.PICKER,
        ControllerState.REWIND,
        ControllerState.EXITING,
    },
    ControllerState.PICKER: {
        ControllerState.IDLE,
        ControllerState.TOOL_RUNNING_BACKGROUND,
        ControllerState.EXITING,
    },
    ControllerState.REWIND: {
        ControllerState.IDLE,
        ControllerState.TOOL_RUNNING_BACKGROUND,
        ControllerState.EXITING,
    },
    ControllerState.EXITING: set(),
}


class SessionController:
    """单线程 reducer；journal 通常是 core.state.StateStore。"""

    def __init__(self, session_id, journal, *, id_factory=None, clock=None):
        self.session_id = str(session_id)
        self.journal = journal
        self._id_factory = id_factory or (
            lambda kind: f"{kind}-{uuid.uuid4().hex[:12]}")
        self._clock = clock or utcnow
        self.state = ControllerState.IDLE
        self.current_turn_id = None
        self.current_request_id = None
        self.request_handle = None
        self.cancelling = False
        self._local_action = None
        self._pending_tools = []
        self._approved_tool = None
        self._running_tool = None
        self._running_task_id = None
        self._tool_cancelling = False
        # Exactly one owner interaction may be parked for the foreground tool.
        # Keeping its correlation id in the reducer prevents a late/foreign
        # result from resolving whichever gate happens to be visible now.
        self._interaction_request_id = None
        self._interaction_kind = None
        # Detached option labels for the active decision gate.  The owner
        # bridge validates the same payload, but the reducer keeps its own
        # copy so a foreign/buggy embedding cannot persist an arbitrary label
        # as a human decision.
        self._interaction_options = frozenset()
        self._interaction_questions = {}
        self._modal_return_state = None
        # Dispatch is the durable point at which queued text becomes a user
        # message (or local action). Interactive callers drain this transient
        # notification stream to move the prompt from the composer into
        # scrollback exactly once. It is deliberately not persisted: the
        # journal owns recovery, while terminal paint is process-local.
        self._dispatched_inputs = []
        # Internal continuations are fail-closed by default.  Session briefly
        # opens this gate only after validating the durable goal id/revision;
        # generic command/turn completion must never dispatch one by accident.
        self.internal_dispatch_enabled = False
        self._queue = [
            QueuedInput.from_row(row)
            for row in journal.list_queued_inputs(self.session_id, state="queued")
        ]

    @property
    def queued(self):
        return tuple(self._queue)

    def drain_dispatched_inputs(self):
        """Return newly dispatched inputs once, in durable dispatch order."""
        items = tuple(self._dispatched_inputs)
        self._dispatched_inputs.clear()
        return items

    @property
    def pending_tools(self):
        return tuple(self._pending_tools)

    @property
    def current_tool_call_id(self):
        return (
            self._tool_id(self._running_tool)
            if self._running_tool is not None else None)

    @property
    def current_task_id(self):
        return self._running_task_id

    @property
    def current_interaction_id(self):
        """Opaque id of the one interaction currently awaiting the owner."""
        return self._interaction_request_id

    @property
    def tool_cancelling(self):
        return self._tool_cancelling

    def _transition(self, new_state):
        """内部状态边；所有公开入口先验证自己的领域不变量。"""
        new_state = ControllerState(new_state)
        if new_state == self.state:
            return self.state
        if new_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state.value} -> {new_state.value}")
        self.state = new_state
        return self.state

    def open_modal(self, modal):
        modal = ControllerState(modal)
        if modal not in {ControllerState.PICKER, ControllerState.REWIND}:
            raise ControllerError(f"不是 modal state: {modal.value}")
        if self.state not in {
                ControllerState.IDLE,
                ControllerState.TOOL_RUNNING_BACKGROUND}:
            raise ControllerError(f"{self.state.value} 不能打开 modal")
        self._modal_return_state = self.state
        return self._transition(modal)

    def close_modal(self):
        if self.state not in {
                ControllerState.PICKER, ControllerState.REWIND}:
            raise ControllerError("当前没有打开 modal")
        target = self._modal_return_state or ControllerState.IDLE
        self._modal_return_state = None
        return self._transition(target)

    def begin_exit(self):
        if self.state == ControllerState.EXITING:
            return self.state
        self._modal_return_state = None
        return self._transition(ControllerState.EXITING)


    def _new_id(self, kind):
        value = str(self._id_factory(kind) or "").strip()
        if not value:
            raise ControllerError(f"{kind} id 不能为空")
        return value

    def _append(self, events):
        return self.journal.append_events(self.session_id, events)

    def record_event(self, kind, payload=None, *, turn_id=None):
        """记录不改变 reducer 状态的语义事件（context/diagnostic 等）。"""
        active_turn = self.current_turn_id if turn_id is None else turn_id
        return self._append([{
            "kind": str(kind),
            "payload": payload or {},
            "turn_id": active_turn,
        }])[0]

    def _remove_item(self, item):
        self._queue.remove(item)

    def _find_queued(self, *, mode=None, newest=False):
        candidates = [
            item for item in self._queue
            if mode is None or item.mode == QueueMode(mode)
        ]
        if not candidates:
            return None
        return candidates[-1] if newest else candidates[0]

    def submit(self, text, mode=None, *, origin="user", metadata=None):
        """保存输入；空闲时立即 dispatch，工作中则保持 queued。"""
        if not isinstance(text, str) or not text.strip():
            raise ControllerError("不能提交空输入")
        if "\x00" in text:
            raise ControllerError("输入不能包含 NUL")
        origin = str(origin or "user").strip().lower()
        if metadata in (None, ""):
            metadata = {}
        elif not isinstance(metadata, dict):
            raise ControllerError("queue metadata 必须是 object")
        else:
            metadata = dict(metadata)
        if origin == "user":
            if metadata:
                raise ControllerError("user queue 不接受内部 metadata")
            stored_text = text
        elif origin in _INTERNAL_ORIGINS:
            if mode is not None and QueueMode(mode) != QueueMode.NEXT_TURN:
                raise ControllerError("goal continuation 只能是 next_turn")
            stored_text = _encode_internal(text, origin, metadata)
        else:
            raise ControllerError(f"未知 queue origin: {origin}")
        if self.state in {
                ControllerState.PICKER, ControllerState.REWIND,
                ControllerState.TOOL_WAITING_APPROVAL,
                ControllerState.EXITING}:
            raise ControllerError(f"{self.state.value} 不接受文本提交")

        if mode is None:
            active_turn = self.current_turn_id is not None
            if (active_turn and self.state in {
                    ControllerState.MODEL_STREAMING,
                    ControllerState.TOOL_RUNNING_FOREGROUND}):
                mode = QueueMode.STEER
            else:
                mode = QueueMode.NEXT_TURN
        else:
            try:
                mode = QueueMode(mode)
            except ValueError as exc:
                raise ControllerError(f"未知 queue mode: {mode}") from exc
        if mode == QueueMode.STEER and self.current_turn_id is None:
            raise ControllerError("没有 active turn，不能提交 steer")

        item = QueuedInput(
            id=self._new_id("input"),
            mode=mode,
            text=text,
            enqueued_at=self._clock(),
            origin=origin,
            metadata=metadata,
        )
        # 先落盘；失败时内存 queue 不变化。
        self.journal.enqueue_input(
            self.session_id, item.id, item.mode.value, stored_text,
            enqueued_at=item.enqueued_at)
        self._queue.append(item)

        if (self.current_turn_id is None
                and self.state in {
                    ControllerState.IDLE,
                    ControllerState.TOOL_RUNNING_BACKGROUND}):
            return self.dispatch_ready()
        return None

    def submit_goal(self, text, *, metadata=None):
        """Queue one internal goal continuation through normal durability gates."""
        return self.submit(
            text, QueueMode.NEXT_TURN, origin="goal", metadata=metadata)

    def retrieve_latest(self, mode=None):
        """Up 的语义：取回最后一条未 dispatch 输入并记录 cancel event。"""
        item = self._find_queued(mode=mode, newest=True)
        if item is None:
            return None
        return self.cancel_queued_item(item.id)

    def cancel_queued_item(self, item_id, *, origin=None):
        """Durably cancel one queued item and then remove its local view."""
        identifier = str(item_id or "")
        item = next((candidate for candidate in self._queue
                     if candidate.id == identifier), None)
        if item is None:
            raise ControllerError(f"queued input 不存在: {identifier}")
        if origin is not None and item.origin != str(origin).lower():
            raise ControllerError(
                f"queued input {identifier} origin 不匹配")
        self.journal.cancel_queued_input(self.session_id, item.id)
        self._remove_item(item)
        return item

    def cancel_internal_inputs(self, origin="goal"):
        """Cancel queued non-human inputs without disturbing user queue order."""
        origin = str(origin or "").strip().lower()
        removed = []
        for item in tuple(self._queue):
            if item.origin != origin:
                continue
            removed.append(self.cancel_queued_item(item.id, origin=origin))
        return tuple(removed)

    def dispatch_ready(self):
        """空闲时按入队顺序 dispatch 下一条可执行输入。"""
        if self.current_turn_id is not None or self._local_action is not None:
            return None
        if self.state not in {
                ControllerState.IDLE,
                ControllerState.TOOL_RUNNING_BACKGROUND}:
            return None
        # crash 后残留的 steer 不能静默改成新 turn；保留给用户取回。
        item = next(
            (candidate for candidate in self._queue
             if candidate.mode != QueueMode.STEER
             and (candidate.origin != "goal"
                  or self.internal_dispatch_enabled)),
            None)
        if item is None:
            return None
        if item.mode == QueueMode.NEXT_TURN:
            return self._dispatch_new_turn(item)
        return self._dispatch_local(item)

    def _dispatch_new_turn(self, item):
        turn_id = self._new_id("turn")
        message = {"role": "user", "content": item.text}
        # Keep the legacy payload byte-for-byte compatible for ordinary human
        # input.  Internal goal metadata is an audit/UI affordance, not a
        # reason to make every existing consumer learn a new schema.
        input_details = {"queue_id": item.id, "mode": item.mode.value}
        if item.origin != "user":
            input_details["origin"] = item.origin
            if item.metadata:
                input_details["metadata"] = dict(item.metadata)
        events = [
            {
                "kind": "user_message",
                "payload": {
                    "message": message,
                    **input_details,
                },
                "turn_id": turn_id,
            },
            {
                "kind": "turn_started",
                "payload": dict(input_details),
                "turn_id": turn_id,
            },
        ]
        self.journal.dispatch_queued_input(
            self.session_id, item.id, turn_id=turn_id,
            dispatched_at=self._clock(), events=events)
        self._remove_item(item)
        self.current_turn_id = turn_id
        self._transition(ControllerState.MODEL_STREAMING)
        self._dispatched_inputs.append(item)
        return ControllerAction(
            ActionKind.START_TURN, turn_id=turn_id, item=item,
            payload={"message": message})

    def _dispatch_steer(self, item):
        if self.current_turn_id is None:
            raise ControllerError("steer dispatch 缺少 active turn")
        message = {"role": "user", "content": item.text}
        self.journal.dispatch_queued_input(
            self.session_id, item.id, turn_id=self.current_turn_id,
            dispatched_at=self._clock(), events=[{
                "kind": "user_message",
                "payload": {
                    "message": message,
                    "queue_id": item.id,
                    "mode": item.mode.value,
                },
                "turn_id": self.current_turn_id,
            }])
        self._remove_item(item)
        self._transition(ControllerState.MODEL_STREAMING)
        self._dispatched_inputs.append(item)
        return ControllerAction(
            ActionKind.DELIVER_STEER, turn_id=self.current_turn_id,
            item=item, payload={"message": message})

    def _dispatch_local(self, item):
        self.journal.dispatch_queued_input(
            self.session_id, item.id, dispatched_at=self._clock())
        self._remove_item(item)
        self._local_action = item
        self._transition(ControllerState.TOOL_RUNNING_FOREGROUND)
        self._dispatched_inputs.append(item)
        kind = (ActionKind.RUN_COMMAND if item.mode == QueueMode.COMMAND
                else ActionKind.RUN_SHELL)
        return ControllerAction(kind, item=item)

    def finish_local_action(self):
        if self._local_action is None:
            raise ControllerError("当前没有 local action")
        self._local_action = None
        self._transition(ControllerState.IDLE)
        return self.dispatch_ready()

    def dispatch_at_boundary(self):
        """纯文本结束或工具批次结束后的 steer 交付点。"""
        if self.current_turn_id is None:
            return None
        if self.current_request_id is not None:
            raise ControllerError("provider 仍在运行，不是安全交付点")
        item = self._find_queued(mode=QueueMode.STEER)
        return self._dispatch_steer(item) if item else None

    def begin_request(self, *, request_id=None, attempt=1, model="",
                      gateway="", context=None):
        """先提交 request_started，再把 START_REQUEST action 交给 worker。"""
        if self.state != ControllerState.MODEL_STREAMING:
            raise ControllerError(f"{self.state.value} 不能启动 provider")
        if self.current_turn_id is None:
            raise ControllerError("request 缺少 active turn")
        if self.current_request_id is not None:
            raise ControllerError("已有 provider request 在运行")
        request_id = request_id or self._new_id("request")
        payload = {
            "request_id": request_id,
            "attempt": int(attempt),
            "model": str(model),
            "gateway": str(gateway),
        }
        if context is not None:
            payload["context"] = context
        self._append([{
            "kind": "request_started",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        handle = client.CancellationHandle()
        self.current_request_id = request_id
        self.request_handle = handle
        self.cancelling = False
        return ControllerAction(
            ActionKind.START_REQUEST,
            turn_id=self.current_turn_id,
            request_id=request_id,
            cancel=handle,
            payload=payload)

    def request_cancel(self):
        """先记录 cancel intent，再 close response；ack 前保持 cancelling。"""
        if self.current_request_id is None or self.request_handle is None:
            return self.request_tool_cancel()
        if self.cancelling:
            return ControllerAction(
                ActionKind.CANCEL_REQUEST,
                turn_id=self.current_turn_id,
                request_id=self.current_request_id,
                cancel=self.request_handle,
                payload={"already_requested": True})
        self._append([{
            "kind": "request_cancel_requested",
            "payload": {"request_id": self.current_request_id},
            "turn_id": self.current_turn_id,
        }])
        self.cancelling = True
        self.request_handle.cancel()
        return ControllerAction(
            ActionKind.CANCEL_REQUEST,
            turn_id=self.current_turn_id,
            request_id=self.current_request_id,
            cancel=self.request_handle)

    def request_tool_cancel(self):
        """先持久化 foreground tool cancel intent，再交还 task key。"""
        if (self.state not in {
                ControllerState.TOOL_RUNNING_FOREGROUND,
                ControllerState.TOOL_WAITING_DECISION}
                or self._running_tool is None):
            return None
        tool_call_id = self._tool_id(self._running_tool)
        payload = {
            "tool_call_id": tool_call_id,
            "task_id": self._running_task_id,
        }
        if self._tool_cancelling:
            payload["already_requested"] = True
            return ControllerAction(
                ActionKind.CANCEL_TOOL,
                turn_id=self.current_turn_id,
                payload=payload)
        self._append([{
            "kind": "tool_cancel_requested",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self._tool_cancelling = True
        return ControllerAction(
            ActionKind.CANCEL_TOOL,
            turn_id=self.current_turn_id,
            payload=payload)

    def acknowledge_interrupt(self, partial_text=""):
        """worker 确认退出；steer 留在同一 turn，next-turn queue 保持独立。"""
        if self.current_request_id is None:
            raise ControllerError("没有可确认的 provider request")
        request_id = self.current_request_id
        steer = self._find_queued(mode=QueueMode.STEER)
        events = [{
            "kind": "request_interrupted",
            "payload": {
                "request_id": request_id,
                "partial_text": str(partial_text),
            },
            "turn_id": self.current_turn_id,
        }]
        if steer is None:
            events.append({
                "kind": "turn_failed",
                "payload": {
                    "request_id": request_id,
                    "reason": "user_interrupted",
                },
                "turn_id": self.current_turn_id,
            })
        self._append(events)
        self.request_handle.release()
        self.current_request_id = None
        self.request_handle = None
        self.cancelling = False
        if steer is not None:
            return self.dispatch_at_boundary()

        self.current_turn_id = None
        self._transition(ControllerState.IDLE)
        return self.dispatch_ready()

    def complete_response(self, message, *, tool_calls=(), usage=None,
                          reason=None):
        """提交 assistant/tool intent；返回下一项可执行 action。"""
        if self.current_request_id is None:
            raise ControllerError("没有 active provider request")
        if self.cancelling:
            raise ControllerError("request 正在 cancelling，忽略迟到结果")
        if isinstance(message, str):
            message = {"role": "assistant", "content": message}
        elif not isinstance(message, dict) or message.get("role") != "assistant":
            raise ControllerError("assistant message 格式无效")
        else:
            message = dict(message)
        raw_calls = tool_calls or message.get("tool_calls") or ()
        calls = self._normalize_tool_calls(raw_calls)
        if calls:
            # Keep the durable assistant message independent from the
            # approval work queue.  ``decide_tool`` removes items from
            # ``_pending_tools`` one by one; sharing this list would mutate an
            # already-appended MemoryJournal event into ``tool_calls: []``
            # during replay (StateStore serializes sooner and masked it).
            message["tool_calls"] = list(calls)

        request_id = self.current_request_id
        events = [{
            "kind": "assistant_message",
            "payload": {
                "message": message,
                "request_id": request_id,
                "usage": usage or {},
            },
            "turn_id": self.current_turn_id,
        }]
        events.extend({
            "kind": "tool_requested",
            "payload": {"request_id": request_id, "tool_call": call},
            "turn_id": self.current_turn_id,
        } for call in calls)

        steer = self._find_queued(mode=QueueMode.STEER)
        if not calls and steer is None:
            events.append({
                "kind": "turn_completed",
                "payload": {"request_id": request_id, "reason": reason},
                "turn_id": self.current_turn_id,
            })
        self._append(events)
        self.request_handle.release()
        self.current_request_id = None
        self.request_handle = None

        if calls:
            self._pending_tools = list(calls)
            self._transition(ControllerState.TOOL_WAITING_APPROVAL)
            return ControllerAction(
                ActionKind.REVIEW_TOOLS,
                turn_id=self.current_turn_id,
                payload={"tool_calls": list(calls)})
        if steer is not None:
            return self.dispatch_at_boundary()

        self.current_turn_id = None
        self._transition(ControllerState.IDLE)
        return self.dispatch_ready()

    def fail_request(self, error, *, error_kind=None, details=None):
        if self.current_request_id is None:
            raise ControllerError("没有 active provider request")
        request_id = self.current_request_id
        payload = {
            "request_id": request_id,
            "error_kind": error_kind or type(error).__name__,
            "error": str(error),
        }
        if details:
            payload.update(details)
        self._append([{
            "kind": "turn_failed",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self.request_handle.release()
        self.current_request_id = None
        self.request_handle = None
        self.cancelling = False
        self._interaction_request_id = None
        self._interaction_kind = None
        self._interaction_options = frozenset()
        self._interaction_questions = {}
        self.current_turn_id = None
        self._transition(ControllerState.IDLE)
        return self.dispatch_ready()

    def retry_request(self, error, *, error_kind=None, details=None):
        """结束一次可重试 request，但保持当前 turn 在 MODEL_STREAMING。"""
        if self.current_request_id is None:
            raise ControllerError("没有 active provider request")
        request_id = self.current_request_id
        payload = {
            "request_id": request_id,
            "error_kind": error_kind or type(error).__name__,
            "error": str(error),
            "retrying": True,
        }
        if details:
            payload.update(details)
        self._append([{
            "kind": "request_failed",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self.request_handle.release()
        self.current_request_id = None
        self.request_handle = None
        self.cancelling = False
        return ControllerAction(
            ActionKind.CONTINUE_TURN, turn_id=self.current_turn_id)

    def finish_aux_request(self, purpose, *, status="completed", details=None):
        """结束不产生 assistant message 的 provider request，保持当前 turn。"""
        if self.current_request_id is None:
            raise ControllerError("没有 active provider request")
        request_id = self.current_request_id
        status = str(status)
        payload = {
            "request_id": request_id,
            "purpose": str(purpose),
            "status": status,
        }
        if details:
            payload.update(details)
        self._append([{
            "kind": (
                "request_completed"
                if status == "completed" else "request_failed"),
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self.request_handle.release()
        self.current_request_id = None
        self.request_handle = None
        self.cancelling = False
        return ControllerAction(
            ActionKind.CONTINUE_TURN, turn_id=self.current_turn_id)


    def fail_turn(self, reason, *, details=None):
        """在尚无 active request 时终止 turn，并 dispatch 下一条 queue。"""
        if self.current_turn_id is None:
            raise ControllerError("没有 active turn")
        if self.current_request_id is not None:
            raise ControllerError("active request 应使用 fail_request")
        if self._interaction_request_id is not None:
            raise ControllerError(
                "decision gate 仍在等待；必须先 resolve 或 abort interaction")
        payload = {"reason": str(reason)}
        if details:
            payload.update(details)
        self._append([{
            "kind": "turn_failed",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self._pending_tools = []
        self._approved_tool = None
        self._running_tool = None
        self._running_task_id = None
        self._tool_cancelling = False
        self._interaction_request_id = None
        self._interaction_kind = None
        self._interaction_options = frozenset()
        self._interaction_questions = {}
        self._local_action = None
        self.current_turn_id = None
        self._transition(ControllerState.IDLE)
        return self.dispatch_ready()

    @staticmethod
    def _tool_id(call):
        return str(call.get("id") or "")

    @classmethod
    def _normalize_tool_calls(cls, calls):
        normalized = []
        seen = set()
        for call in calls:
            if not isinstance(call, dict):
                raise ControllerError("tool call 必须是 dict")
            item = dict(call)
            tool_call_id = cls._tool_id(item)
            function = item.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if not tool_call_id or not isinstance(name, str) or not name:
                raise ControllerError("tool call 缺少 id 或 function.name")
            if tool_call_id in seen:
                raise ControllerError(f"重复 tool_call_id: {tool_call_id}")
            seen.add(tool_call_id)
            normalized.append(item)
        return normalized


    def decide_tool(self, tool_call_id, *, allowed, decision, source,
                    denied_message="[用户拒绝了这次调用]",
                    denied_status="denied", dispatch_boundary=True):
        """Persist permission without claiming an allowed side effect started.

        M4 checkpoints must become durable after approval but before
        tool_started. Allowed calls therefore enter an explicit approved
        state; callers invoke start_tool only after every pre-side-effect
        durability gate succeeds.
        """
        if self.state != ControllerState.TOOL_WAITING_APPROVAL:
            raise ControllerError("当前不在工具审批状态")
        if self._approved_tool is not None:
            raise ControllerError("已有已批准但尚未启动的工具")
        call = next(
            (item for item in self._pending_tools
             if self._tool_id(item) == str(tool_call_id)),
            None)
        if call is None:
            raise ControllerError(f"未知 tool_call_id: {tool_call_id}")
        events = [{
            "kind": "permission_decided",
            "payload": {
                "tool_call_id": tool_call_id,
                "allowed": bool(allowed),
                "decision": str(decision),
                "source": str(source),
            },
            "turn_id": self.current_turn_id,
        }]
        if not allowed:
            events.append({
                "kind": "tool_finished",
                "payload": {
                    "tool_call_id": tool_call_id,
                    "status": str(denied_status),
                    "message": {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": denied_message,
                    },
                },
                "turn_id": self.current_turn_id,
            })
        self._append(events)
        self._pending_tools.remove(call)

        if allowed:
            self._approved_tool = call
            return ControllerAction(
                ActionKind.START_TOOL,
                turn_id=self.current_turn_id,
                payload={"tool_call": call})

        if self._pending_tools:
            return ControllerAction(
                ActionKind.REVIEW_TOOLS,
                turn_id=self.current_turn_id,
                payload={"tool_calls": list(self._pending_tools)})
        self._transition(ControllerState.MODEL_STREAMING)
        steer_action = (
            self.dispatch_at_boundary() if dispatch_boundary else None)
        return steer_action or ControllerAction(
            ActionKind.CONTINUE_TURN, turn_id=self.current_turn_id)

    def start_tool(self, tool_call_id, *, details=None):
        """Record tool intent after checkpoint/durability gates have passed."""
        if (self.state != ControllerState.TOOL_WAITING_APPROVAL
                or self._approved_tool is None):
            raise ControllerError("当前没有已批准、待启动的工具")
        expected = self._tool_id(self._approved_tool)
        if str(tool_call_id) != expected:
            raise ControllerError("启动的 tool_call 与已批准调用不匹配")
        payload = {"tool_call": self._approved_tool}
        if details:
            payload.update(details)
        self._append([{
            "kind": "tool_started",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self._running_tool = self._approved_tool
        self._approved_tool = None
        self._running_task_id = None
        self._tool_cancelling = False
        self._transition(ControllerState.TOOL_RUNNING_FOREGROUND)
        return ControllerAction(
            ActionKind.START_TOOL,
            turn_id=self.current_turn_id,
            payload={"tool_call": self._running_tool})

    def begin_interaction(self, tool_call_id, request_id, kind, payload=None):
        """Mark a running tool as waiting for an owner-thread interaction.

        This is a lifecycle/audit transition only.  It deliberately does not
        call a renderer or provider; the Session owner invokes it when it
        drains the lossless worker request event.
        """
        if (self.state != ControllerState.TOOL_RUNNING_FOREGROUND
                or self._running_tool is None):
            raise ControllerError("当前没有可等待交互的 foreground tool")
        expected = self._tool_id(self._running_tool)
        if str(tool_call_id) != expected:
            raise ControllerError("interaction tool_call 与当前工具不匹配")
        request_id = str(request_id or "").strip()
        kind = str(kind or "").strip()
        if not request_id or not kind:
            raise ControllerError("interaction 缺少 request_id/kind")
        if "\x00" in request_id or len(request_id) > 256:
            raise ControllerError("interaction request_id 无效")
        if "\x00" in kind or len(kind) > 96:
            raise ControllerError("interaction kind 无效")
        if not isinstance(payload, dict) and payload is not None:
            raise ControllerError("interaction payload 必须是 object")
        interaction_options = frozenset()
        interaction_questions = {}
        if kind == "decision_gate":
            if str((payload or {}).get("_interaction_role", "blocked")
                   ).strip().lower() != "main":
                raise ControllerError(
                    "只有当前窗口的主 agent 可以触发 decision_gate")
            raw_questions = payload.get("questions") if isinstance(
                payload, dict) else None
            if isinstance(raw_questions, (list, tuple)) and raw_questions:
                if len(raw_questions) > 4:
                    raise ControllerError(
                        "decision_gate 最多只能包含 4 个 questions")
                for index, question in enumerate(raw_questions):
                    if not isinstance(question, dict):
                        raise ControllerError(
                            f"decision_gate question[{index}] 必须是 object")
                    question_id = str(question.get("id") or "").strip()
                    if (not question_id
                            or not _DECISION_GATE_ID.fullmatch(question_id)
                            or question_id in interaction_questions):
                        raise ControllerError(
                            "decision_gate question id 必须是唯一的短标识")
                    raw_options = question.get("options")
                    if not isinstance(raw_options, (list, tuple)):
                        raise ControllerError(
                            f"decision_gate question[{index}] 缺少 options")
                    labels = []
                    for option in raw_options:
                        if not isinstance(option, dict):
                            raise ControllerError(
                                "decision_gate option 必须是 object")
                        label = str(option.get("label") or "").strip()
                        if not label:
                            raise ControllerError(
                                "decision_gate option 缺少 label")
                        labels.append(label)
                    if not labels or len(labels) != len(set(labels)):
                        raise ControllerError(
                            "decision_gate options 必须有唯一 label")
                    interaction_questions[question_id] = {
                        "options": frozenset(labels),
                        "multi_select": question.get("multi_select") is True,
                    }
            else:
                raw_options = payload.get("options") if isinstance(
                    payload, dict) else None
                if not isinstance(raw_options, (list, tuple)):
                    raise ControllerError("decision_gate interaction 缺少 options")
                labels = []
                for option in raw_options:
                    if not isinstance(option, dict):
                        raise ControllerError("decision_gate option 必须是 object")
                    label = str(option.get("label") or "").strip()
                    if not label:
                        raise ControllerError("decision_gate option 缺少 label")
                    labels.append(label)
                if not labels or len(labels) != len(set(labels)):
                    raise ControllerError("decision_gate options 必须有唯一 label")
                interaction_questions["decision"] = {
                    "options": frozenset(labels), "multi_select": False,
                }
            interaction_options = frozenset().union(*(
                item["options"] for item in interaction_questions.values()))
        if self._interaction_request_id is not None:
            raise ControllerError(
                f"已有 interaction 等待中：{self._interaction_request_id}")
        details = {
            "tool_call_id": expected,
            "request_id": request_id,
            "kind": kind,
            # Detach nested question/options data before it enters the audit;
            # the owner may mutate its event object while the modal is open.
            "payload": copy.deepcopy(payload or {}),
        }
        self._append([{
            "kind": "interaction_requested",
            "payload": details,
            "turn_id": self.current_turn_id,
        }])
        self._transition(ControllerState.TOOL_WAITING_DECISION)
        self._interaction_request_id = request_id
        self._interaction_kind = kind
        self._interaction_options = interaction_options
        self._interaction_questions = interaction_questions
        return details

    def resolve_interaction(self, tool_call_id, request_id, result):
        """Persist a result and return to foreground tool execution."""
        if self.state != ControllerState.TOOL_WAITING_DECISION:
            raise ControllerError("当前没有等待中的 interaction")
        expected = self._tool_id(self._running_tool)
        if str(tool_call_id) != expected:
            raise ControllerError("interaction tool_call 与当前工具不匹配")
        request_id = str(request_id or "").strip()
        if not request_id or not isinstance(result, dict):
            raise ControllerError("interaction result 无效")
        if request_id != self._interaction_request_id:
            raise ControllerError(
                "interaction request_id 与当前等待槽位不匹配")
        if self._interaction_kind == "decision_gate":
            status = str(result.get("status") or "").strip().lower()
            if status not in {
                    "resolved", "cancelled", "unattended", "expired",
                    "invalid", "persistence_error"}:
                raise ControllerError("decision_gate result status 无效")
            if (status == "resolved"
                    and result.get("attended") is not True):
                raise ControllerError(
                    "resolved decision_gate 必须带 attended=true")
            if status == "resolved":
                answers = result.get("answers")
                if not isinstance(answers, dict):
                    if len(self._interaction_questions) != 1:
                        raise ControllerError(
                            "多问题 decision_gate 必须带 answers object")
                    answers = {"decision": result.get("choice")}
                expected_ids = set(self._interaction_questions)
                supplied_ids = {str(key) for key in answers}
                if supplied_ids != expected_ids:
                    unknown = sorted(supplied_ids - expected_ids)
                    missing = sorted(expected_ids - supplied_ids)
                    detail = []
                    if missing:
                        detail.append("缺少 " + ", ".join(missing))
                    if unknown:
                        detail.append("未知 " + ", ".join(unknown))
                    raise ControllerError(
                        "decision_gate answers question id 不匹配："
                        + "；".join(detail))
                canonical_answers = {}
                for question_id, metadata in self._interaction_questions.items():
                    if question_id not in answers:
                        raise ControllerError(
                            f"decision_gate 缺少 question {question_id} 的答案")
                    value = answers[question_id]
                    if metadata["multi_select"]:
                        if not isinstance(value, (list, tuple)) or not value:
                            raise ControllerError(
                                f"question {question_id} 必须选择至少一个 option")
                        labels = [str(item or "").strip() for item in value]
                        if len(labels) != len(set(labels)):
                            raise ControllerError(
                                f"question {question_id} 不能重复选择")
                    else:
                        labels = [value]
                    if any(str(item or "").strip() not in metadata["options"]
                           for item in labels):
                        raise ControllerError(
                            f"question {question_id} 的答案不在原始 options 中")
                    canonical_answers[question_id] = (
                        labels if metadata["multi_select"] else labels[0])
                if not str(result.get("choice") or "").strip():
                    raise ControllerError(
                        "resolved decision_gate 必须带至少一个 choice")
                first_id = next(iter(self._interaction_questions))
                first_value = canonical_answers[first_id]
                first_choice = (
                    first_value[0] if isinstance(first_value, list)
                    else first_value)
                if str(result.get("choice") or "").strip() != first_choice:
                    raise ControllerError(
                        "resolved decision_gate choice 必须匹配第一个 question 的答案")
                # Store a detached JSON-shaped answer map even when a small
                # embedding supplied tuples or a custom mapping subclass.
                result = dict(result)
                result["answers"] = copy.deepcopy(canonical_answers)
            if (status != "resolved"
                    and (result.get("attended") is True
                         or str(result.get("choice") or "").strip())):
                raise ControllerError(
                    "未 resolved 的 decision_gate 不能带 attended=true 或 choice")
        clean = copy.deepcopy(dict(result))
        self._append([{
            "kind": "interaction_resolved",
            "payload": {
                "tool_call_id": expected,
                "request_id": request_id,
                "kind": self._interaction_kind,
                "result": clean,
            },
            "turn_id": self.current_turn_id,
        }])
        self._interaction_request_id = None
        self._interaction_kind = None
        self._interaction_options = frozenset()
        self._interaction_questions = {}
        self._transition(ControllerState.TOOL_RUNNING_FOREGROUND)
        return clean

    def abort_interaction(self, reason, *, status="cancelled", details=None):
        """Durably close a parked interaction without inventing a choice.

        This is an explicit recovery path for owner/UI failures.  The caller
        still has to wake/cancel the worker through TaskManager; the reducer
        only records the lifecycle and restores the foreground-tool state.
        """
        if self.state != ControllerState.TOOL_WAITING_DECISION:
            raise ControllerError("当前没有等待中的 interaction")
        if self._running_tool is None or self._interaction_request_id is None:
            raise ControllerError("等待中的 interaction 状态不完整")
        status = str(status or "cancelled").strip().lower()
        if status not in {
                "cancelled", "unattended", "expired", "invalid",
                "persistence_error"}:
            raise ControllerError("interaction abort status 无效")
        reason = str(reason or "interaction 已中止").strip()
        if not reason:
            reason = "interaction 已中止"
        if "\x00" in reason:
            raise ControllerError("interaction abort reason 无效")
        payload = {
            "tool_call_id": self._tool_id(self._running_tool),
            "request_id": self._interaction_request_id,
            "kind": self._interaction_kind,
            "status": str(status),
            "reason": reason[:512],
        }
        if details is not None and not isinstance(details, dict):
            raise ControllerError("interaction abort details 必须是 object")
        if details:
            reserved = set(payload)
            overlap = reserved.intersection(str(key) for key in details)
            if overlap:
                raise ControllerError(
                    "interaction abort details 不能覆盖保留字段: "
                    + ", ".join(sorted(overlap)))
            payload.update({str(key): value for key, value in details.items()})
        self._append([{
            "kind": "interaction_aborted",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self._interaction_request_id = None
        self._interaction_kind = None
        self._interaction_options = frozenset()
        self._interaction_questions = {}
        self._transition(ControllerState.TOOL_RUNNING_FOREGROUND)
        return payload

    def abort_approved_tool(self, message, *, status="failed", details=None):
        """Close an approved call when a pre-start durability gate fails."""
        if (self.state != ControllerState.TOOL_WAITING_APPROVAL
                or self._approved_tool is None):
            raise ControllerError("当前没有已批准、待终止的工具")
        call = self._approved_tool
        tool_call_id = self._tool_id(call)
        if isinstance(message, str):
            message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": message,
            }
        payload = {
            "tool_call_id": tool_call_id,
            "status": str(status),
            "message": message,
        }
        if details:
            payload.update(details)
        self._append([{
            "kind": "tool_finished",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self._approved_tool = None
        if self._pending_tools:
            return ControllerAction(
                ActionKind.REVIEW_TOOLS,
                turn_id=self.current_turn_id,
                payload={"tool_calls": list(self._pending_tools)})
        self._transition(ControllerState.MODEL_STREAMING)
        steer_action = self.dispatch_at_boundary()
        return steer_action or ControllerAction(
            ActionKind.CONTINUE_TURN, turn_id=self.current_turn_id)

    def register_tool_task(self, tool_call_id, task_id, *, details=None):
        """task metadata 先落 journal；调用方随后才可启动 worker。"""
        if (self.state != ControllerState.TOOL_RUNNING_FOREGROUND
                or self._running_tool is None):
            raise ControllerError("当前没有可绑定 task 的 foreground tool")
        expected = self._tool_id(self._running_tool)
        if str(tool_call_id) != expected:
            raise ControllerError("task 与当前 tool_call 不匹配")
        task_id = str(task_id or "").strip()
        if not task_id:
            raise ControllerError("task id 不能为空")
        if self._running_task_id is not None:
            if self._running_task_id == task_id:
                return task_id
            raise ControllerError(
                f"foreground tool 已绑定 task {self._running_task_id}")
        payload = {
            "task_id": task_id,
            "tool_call_id": expected,
            "name": self._running_tool["function"]["name"],
            "cancel_requested": bool(self._tool_cancelling),
        }
        if details:
            payload.update(details)
        self._append([{
            "kind": "task_started",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self._running_task_id = task_id
        return task_id

    def finish_tool(self, message, *, status="completed", details=None,
                    dispatch_boundary=True):
        """先提交 tool_finished，之后才允许下一次 provider request。

        ``dispatch_boundary=False`` is used for a fail-closed interactive
        result: queued steer input remains queued while the caller records the
        turn failure, instead of being silently delivered after cancellation.
        """
        if self._running_tool is None:
            raise ControllerError("当前没有 foreground tool")
        if self._interaction_request_id is not None:
            raise ControllerError(
                "foreground tool 仍在等待 interaction，不能提前结束")
        call = self._running_tool
        tool_call_id = self._tool_id(call)
        if isinstance(message, str):
            message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": message,
            }
        elif isinstance(message, dict):
            message = dict(message)
            if (message.get("role") != "tool"
                    or str(message.get("tool_call_id") or "")
                    != tool_call_id):
                raise ControllerError("tool message 与当前 tool_call 不匹配")
        else:
            raise ControllerError("tool message 格式无效")
        payload = {
            "tool_call_id": tool_call_id,
            "status": str(status),
            "message": message,
        }
        if details:
            payload.update(details)
        self._append([{
            "kind": "tool_finished",
            "payload": payload,
            "turn_id": self.current_turn_id,
        }])
        self._running_tool = None
        self._running_task_id = None
        self._tool_cancelling = False
        self._interaction_request_id = None
        self._interaction_kind = None

        if self._pending_tools:
            self._transition(ControllerState.TOOL_WAITING_APPROVAL)
            return ControllerAction(
                ActionKind.REVIEW_TOOLS,
                turn_id=self.current_turn_id,
                payload={"tool_calls": list(self._pending_tools)})
        self._transition(ControllerState.MODEL_STREAMING)
        steer_action = (
            self.dispatch_at_boundary() if dispatch_boundary else None)
        return steer_action or ControllerAction(
            ActionKind.CONTINUE_TURN, turn_id=self.current_turn_id)
