"""异构模型 workflow：模型自由编排 + 白名单异构 + 可选对抗审查。

主会话仍是唯一写入者。这里的后台线程只启动 AgentWorkspace 中已经硬限制为
CHILD_TOOLS 的 child agents，并写 workflow/agent JSON；不碰 terminal、Controller
或主会话 transcript。

两种用法：
- ``start_plan()``：模型通过 ``workflow`` 工具提交自由 DAG（agents + depends_on），
  编排权在模型；席位白名单、缺席留空、并发上限、对抗审查注入仍由代码保证。
- ``start()``：兼容旧的三段式 preset（quick/standard/deep），内部翻译为固定 plan。
"""
from __future__ import annotations
from . import paths

import concurrent.futures
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import agents, memory as memory_db, models, store, tools, wincompat


VERSION = 1
ROOT = store.HOME / "workflows"
ACTIVE_STATES = {"queued", "running"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}
_ID = re.compile(r"^wf-[a-z0-9]{8,20}$")
_ID_PREFIX = re.compile(r"^wf-[a-z0-9]{3,20}$")
_NODE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
POLL_INTERVAL = 0.05
LAUNCH_STAGGER = 0.12
MAX_PARALLEL = 8
MAX_PER_GATEWAY = 4
MAX_PLAN_AGENTS = 5
MAX_ADAPTIVE_AGENTS = 3
MAX_DYNAMIC_ADD_NODES = 2
MAX_PLAN_NODES = 64
MAX_PLAN_REVIEWERS = 5
MAX_PLAN_REVIEW_ROUNDS = 2
MAX_PLAN_REQUESTS = 48
MAX_NODE_ATTEMPTS = 2
MAX_COMBINED_REPORT = 36_000
# 异构是这个工具的价值（不同模型交叉验证、不同观点碰撞），但用户 2026-09-04 定的是
# "75%"而不是门槛：≥ 这么多节点的 DAG 至少要用 2 个不同 model route；2 个节点允许
# 同席位（启动通知里提示"没有异构交叉验证"）。
HETEROGENEITY_FLOOR_NODES = 3
# 启动前探活：对将要用到的每个 route 发一次真实小请求，坏的换该席位下一候选。
PREFLIGHT_TIMEOUT = 45
PREFLIGHT_PARALLEL = 3

# DAG 是唯一拓扑（2026-09-04 批 2）：quick/standard/deep 模板、SEAT_FOCUS 分工文案与
# scout→review→synthesis 三段式已删除。mode 只剩名字，决定 auto_apply 语义与提示风味。
MODE_NAMES = ("implement", "review", "research", "debug")


class WorkflowError(RuntimeError):
    pass


def _key_slug(label):
    text = "".join(ch for ch in str(label or "").lower() if ch.isalnum())
    return text or "x"


def _review_task(goal, round_number):
    return (
        f"对 workflow“{str(goal or '')[:300]}”执行第 {round_number} 轮对抗审查。"
        "上游报告已作为 context 给你：定位互相矛盾、证据不足、遗漏边界和不可实施"
        "结论；用只读仓库工具复核关键争议，输出 accept/reject/needs-check 及精确"
        "文件证据。")


def _synthesis_task(goal):
    return (
        f"综合 workflow“{str(goal or '')[:300]}”的全部独立报告与审查（已作为 "
        "context 给你）。只保留可复核结论，显式裁决分歧和剩余未知；给主 coding "
        "agent 输出按优先级排序的动作、文件位置、测试与回滚点。你只读，禁止修改文件。")


def expand_plan_agents(agents_spec, *, review=None, synthesis=None, goal=""):
    """把 review / synthesis 配置展开成普通 DAG 节点——DAG 是唯一拓扑。

    reviewer → role=review，depends_on 全部基础节点（第 r 轮还依赖第 r-1 轮的
    reviewer）；synthesis → role=synthesis，depends_on 全部节点。模型侧 schema 早已
    没有这两个参数；这里服务配方与旧调用方，也给 workflow_recipe 工具展开配方用。
    key 只用字母数字下划线（配方 key 不允许连字符）。
    """
    base = [dict(item) for item in (agents_spec or []) if isinstance(item, dict)]
    if not base:
        return list(agents_spec or [])
    keys = [str(item.get("key") or f"a{index}").strip()
            for index, item in enumerate(base, 1)]
    used = set(keys)

    def unique(candidate):
        name, counter = candidate, 2
        while name in used:
            name = f"{candidate}{counter}"
            counter += 1
        used.add(name)
        return name

    def route_fields(item):
        if isinstance(item, str):
            return {"seat": item.strip()}, item
        if isinstance(item, dict):
            if item.get("seat"):
                return {"seat": str(item["seat"]).strip()}, item["seat"]
            fields = {"model": item.get("model")}
            if item.get("gateway"):
                fields["gateway"] = item.get("gateway")
            return fields, item.get("model") or "custom"
        return {}, "custom"

    nodes = list(base)
    all_keys = list(keys)
    previous_layer = []
    if isinstance(review, dict) and review.get("enabled"):
        try:
            rounds = max(1, int(review.get("rounds") or 1))
        except (TypeError, ValueError):
            rounds = 1
        reviewers = [item for item in (review.get("reviewers") or []) if item]
        for round_number in range(1, rounds + 1):
            layer = []
            for item in reviewers:
                fields, label = route_fields(item)
                spec = {
                    "key": unique(f"review_r{round_number}_{_key_slug(label)}"),
                    "role": "review",
                    "task": _review_task(goal, round_number),
                    "depends_on": list(keys) + list(previous_layer),
                    **fields,
                }
                nodes.append(spec)
                layer.append(spec["key"])
            all_keys.extend(layer)
            previous_layer = layer
    if isinstance(synthesis, dict) and (
            synthesis.get("seat") or synthesis.get("model")):
        fields, _label = route_fields(synthesis)
        nodes.append({
            "key": unique("final"),
            "role": "synthesis",
            "task": _synthesis_task(goal),
            "depends_on": list(all_keys),
            **fields,
        })
    return nodes


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _under_protected(path):
    return paths.under_protected(path)


def _pid_alive(pid):
    # os.kill(pid, 0) 在 Windows 上是 CTRL_C_EVENT，不是存活探测（见 wincompat）。
    return wincompat.pid_alive(pid)


def _owner_fields():
    return {
        "owner_pid": os.getpid(),
        "owner_boot_id": store._boot_id(),
        "owner_proc_start": store._proc_start(os.getpid()),
    }


def _owner_alive(record):
    pid = record.get("owner_pid")
    if not _pid_alive(pid):
        return False
    saved_boot = str(record.get("owner_boot_id") or "")
    current_boot = store._boot_id()
    if saved_boot and current_boot and saved_boot != current_boot:
        return False
    saved_start = str(record.get("owner_proc_start") or "")
    if saved_start:
        current_start = store._proc_start(pid)
        return bool(current_start and current_start == saved_start)
    return True


class WorkflowStore:
    """每个 workflow 一个私有原子 JSON；支持唯一前缀查询。"""

    def __init__(self, root=None, *, id_factory=None):
        self.root = Path(root or ROOT)
        if _under_protected(self.root):
            raise WorkflowError(
                f"workflow runtime 不能写入受保护路径：{self.root}")
        self.id_factory = id_factory or (
            lambda: f"wf-{uuid.uuid4().hex[:10]}")

    def _ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise WorkflowError(
                f"workflow root 不能是符号链接：{self.root}")
        os.chmod(self.root, 0o700)

    def _path(self, identifier):
        identifier = str(identifier or "").strip()
        if not _ID_PREFIX.fullmatch(identifier):
            raise WorkflowError(f"无效 workflow id 或前缀：{identifier!r}")
        direct = self.root / f"{identifier}.json"
        if direct.is_symlink():
            raise WorkflowError(f"workflow state 不能是符号链接：{direct}")
        if direct.is_file():
            return direct
        hits = []
        if self.root.is_dir():
            hits = [
                path for path in self.root.glob(f"{identifier}*.json")
                if path.is_file() and not path.is_symlink()
            ]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise WorkflowError(
                f"workflow id 前缀 {identifier!r} 匹配 {len(hits)} 项")
        raise WorkflowError(f"没有 workflow {identifier!r}")

    @staticmethod
    def _read(path):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise WorkflowError(f"没有 workflow state：{path}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                f"workflow state 损坏：{type(exc).__name__}: {exc}") from None
        if not isinstance(value, dict) or value.get("version") != VERSION:
            raise WorkflowError(f"workflow state version 无效：{path}")
        return value

    @staticmethod
    def _write(path, record):
        path = Path(path)
        if path.parent.is_symlink() or path.is_symlink():
            raise WorkflowError(f"workflow state 不能经过符号链接：{path}")
        store._atomic_write_text(
            path,
            json.dumps(record, ensure_ascii=False, separators=(",", ":")))

    def create(self, record):
        self._ensure_root()
        for _ in range(20):
            workflow_id = str(self.id_factory())
            if not _ID.fullmatch(workflow_id):
                continue
            path = self.root / f"{workflow_id}.json"
            if path.exists() or path.is_symlink():
                continue
            value = _copy(record)
            value["id"] = workflow_id
            self._write(path, value)
            return _copy(value)
        raise WorkflowError("无法分配唯一 workflow id")

    def get(self, identifier):
        path = self._path(identifier)
        with store._exclusive_file_lock(path):
            return _copy(self._read(path))

    def update(self, identifier, mutator):
        path = self._path(identifier)
        with store._exclusive_file_lock(path):
            record = self._read(path)
            result = mutator(record)
            record["updated_at"] = _now()
            self._write(path, record)
            return _copy(result if result is not None else record)

    def list(self, *, parent_session_id=None, limit=100):
        if not self.root.is_dir():
            return []
        rows = []
        for path in self.root.glob("wf-*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                record = self._read(path)
            except WorkflowError:
                continue
            if (parent_session_id is not None
                    and record.get("parent_session_id") != parent_session_id):
                continue
            rows.append(record)
        rows.sort(
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True)
        return [_copy(item) for item in rows[:max(0, int(limit))]]


class _WorkflowDriver:
    def __init__(self, hook_config=None):
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.thread = None
        self.hook_config = _copy(hook_config or {})


class WorkflowManager:
    """在一个 AgentWorkspace 上编排异构只读 agents。"""

    def __init__(self, workspace, *, root=None, store_impl=None,
                 lineup_resolver=None, poll_interval=POLL_INTERVAL,
                 event_limit=256, preflight_probe=None, seat_routes=None):
        self.workspace = workspace
        self.store = store_impl or WorkflowStore(root)
        self.lineup_resolver = lineup_resolver or models.workflow_default_rows
        # 启动前探活：引擎默认**不**探活（None）——花 provider 请求的决定权在策略层。
        # Session 按 settings workflow.preflight 注入 models.probe（会刷新健康缓存）；
        # 席位候选路由默认 models.workflow_seat_routes，测试可注入假函数。
        self.preflight_probe = preflight_probe
        self.seat_routes = seat_routes or models.workflow_seat_routes
        self.poll_interval = max(0.01, float(poll_interval))
        self._events = queue.Queue(maxsize=max(8, int(event_limit)))
        self._drivers = {}
        self._lock = threading.RLock()
        self._active_total = 0
        self._active_by_gateway = {}
        self._closed = False
        self.owner_id = uuid.uuid4().hex

    def _emit(self, kind, record, **payload):
        value = {
            "id": record.get("id"),
            "parent_session_id": record.get("parent_session_id"),
            "state": record.get("state"),
            "stage": record.get("stage"),
            "completed_agents": record.get("completed_agents", 0),
            "expected_agents": record.get("expected_agents", 0),
            **payload,
        }
        event = {"kind": str(kind), "payload": _copy(value)}
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(event)
            except queue.Full:
                pass

    @staticmethod
    def _public_lineup(rows):
        return [{
            "seat": row["seat"],
            "family": row.get("family"),
            "model": row["id"],
            "gateway": row["gateway"],
            "quality_tier": row.get("quality_tier", "flagship"),
            "agent_id": None,
            "state": "queued",
        } for row in rows]

    @staticmethod
    def _agent_projection(record, *, phase, seat):
        transcript = record.get("transcript") or {}
        return {
            "id": record["id"],
            "phase": phase,
            "seat": seat,
            "name": record.get("name"),
            "model": record.get("model"),
            "gateway": record.get("gateway"),
            "state": record.get("state"),
            "result": agents.project_report(record.get("result")),
            "error": record.get("error"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "tokens_in": int(transcript.get("tokens_in") or 0),
            "tokens_out": int(transcript.get("tokens_out") or 0),
        }

    def _route_allowed(self, gateway, model):
        try:
            decision = store.metrics_facade().route_decision(
                gateway, model, explicit=False)
        except Exception:
            return True
        return bool(decision.allowed)

    def resolve_lineup(self):
        return self.lineup_resolver(route_allowed=self._route_allowed)

    # ------------------------------------------------------------------
    # 自由编排入口：模型提交 plan（agents + depends_on），代码保证异构与约束。
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_limits(limits):
        raw = dict(limits or {})

        def bounded(name, default, minimum, maximum):
            value = raw.get(name, default)
            if isinstance(value, bool):
                raise WorkflowError(f"{name} 必须是整数")
            try:
                value = int(value)
            except (TypeError, ValueError, OverflowError):
                raise WorkflowError(f"{name} 必须是整数") from None
            if not minimum <= value <= maximum:
                raise WorkflowError(
                    f"{name} 必须在 {minimum}..{maximum} 之间")
            return value

        return {
            "max_agents": bounded(
                "max_agents", MAX_PLAN_AGENTS, 2, MAX_PLAN_AGENTS),
            "max_nodes": bounded(
                "max_nodes", 16, 2, MAX_PLAN_NODES),
            "max_parallel": bounded(
                "max_parallel", 3, 1, MAX_PARALLEL),
            "max_per_gateway": bounded(
                "max_per_gateway", 2, 1, MAX_PER_GATEWAY),
            "max_reviewers": bounded(
                "max_reviewers", MAX_PLAN_REVIEWERS, 0,
                MAX_PLAN_REVIEWERS),
            "max_review_rounds": bounded(
                "max_review_rounds", 1, 1, MAX_PLAN_REVIEW_ROUNDS),
            "max_requests": bounded(
                "max_requests", MAX_PLAN_REQUESTS, 2, MAX_PLAN_REQUESTS),
            "max_requests_per_agent": bounded(
                "max_requests_per_agent", 5, 1, 16),
            "max_node_attempts": bounded(
                "max_node_attempts", MAX_NODE_ATTEMPTS, 1,
                MAX_NODE_ATTEMPTS),
            "max_tokens": bounded(
                "max_tokens", 1_500_000, 16_000, 8_000_000),
            "max_elapsed_seconds": bounded(
                "max_elapsed_seconds", 1800, 60, 14_400),
        }

    def start_plan(self, goal, agents_spec, *, parent_session_id,
                   parent_turn_id="", review=None, synthesis=None,
                   hook_config=None, limits=None, budget=None,
                   trigger="model", mode="review", preset="adaptive",
                   auto_apply=None, context_capsule=None, recipe=None):
        """启动一个自由编排的 workflow。

        ``agents_spec`` 是模型提交的列表，每项：
            {key?, seat?, model?, gateway?, task, role?, context?, depends_on?}
        - key：可选，供 depends_on 引用；缺省自动生成 a1/a2/...
        - seat：models.WORKFLOW_SEAT_NAMES 中的白名单席位；指定后从
          WORKFLOW_DEFAULT_SEATS 解析，缺席留空报错，不降级。
        - model/gateway：显式指定模型，绕过席位白名单（仍受 route_allowed 约束）。
        - depends_on：list[key]，构成 DAG；代码校验循环与悬空引用。

        ``review``：可选对抗审查 {enabled, reviewers:[seat/model], rounds}。
        启用时，代码把已完成 agent 的报告原文注入 reviewer context（与旧
        cross-review 同一套机制），reviewer 结果再作为 synthesis context。

        ``synthesis``：可选 {seat/model}；缺省时主模型自己汇总，不额外开席位。
        """
        goal = str(goal or "").strip()
        if not goal:
            raise WorkflowError("workflow goal 不能为空")
        if len(goal) > 12_000:
            raise WorkflowError("workflow goal 最多 12000 字符")
        # API callers may pass an explicit None/empty value. Keep that path
        # aligned with the signature default instead of silently enabling
        # implement + auto_apply.
        mode = str(mode or "review")
        if mode not in MODE_NAMES:
            raise WorkflowError(
                f"workflow mode 必须是：{', '.join(MODE_NAMES)}")
        if not isinstance(agents_spec, list) or not agents_spec:
            raise WorkflowError("workflow agents 不能为空")
        if context_capsule is not None and not isinstance(
                context_capsule, dict):
            raise WorkflowError("workflow context_capsule 必须是 object")
        if context_capsule:
            try:
                context_capsule = memory_db.validate_capsule(context_capsule)
            except memory_db.MemoryError as exc:
                raise WorkflowError(
                    f"workflow context_capsule integrity 失败：{exc}") from exc
        if len(json.dumps(
                context_capsule or {}, ensure_ascii=False)) > 100_000:
            raise WorkflowError("workflow context_capsule 超过 100000 字符")
        policy = self._plan_limits(limits)
        # review/synthesis（配方或旧调用方传来的）展开成普通节点：DAG 是唯一拓扑。
        # 先做上限校验再展开，展开出来的节点一并计入 max_nodes 与预算。
        if isinstance(review, dict) and review.get("enabled"):
            reviewers = [r for r in (review.get("reviewers") or []) if r]
            if not reviewers:
                raise WorkflowError("review.enabled=true 但未提供有效 reviewers")
            if len(reviewers) > policy["max_reviewers"]:
                raise WorkflowError(
                    f"reviewers 最多 {policy['max_reviewers']} 个")
            rounds = review.get("rounds") or 1
            if isinstance(rounds, bool):
                raise WorkflowError("review.rounds 必须是整数")
            try:
                rounds = int(rounds)
            except (TypeError, ValueError, OverflowError):
                raise WorkflowError("review.rounds 必须是整数") from None
            if not 1 <= rounds <= policy["max_review_rounds"]:
                raise WorkflowError(
                    "review.rounds 必须在 1.."
                    f"{policy['max_review_rounds']} 之间")
        agents_spec = expand_plan_agents(
            agents_spec, review=review, synthesis=synthesis, goal=goal)
        # 节点数按任务定（用户 2026-09-04）：只受 max_nodes 与预算约束，不再另设
        # 模型触发的小上限。
        initial_maximum = policy["max_nodes"]
        if not 2 <= len(agents_spec) <= initial_maximum:
            raise WorkflowError(
                "workflow agents 必须是 2.."
                f"{initial_maximum} 个独立任务")
        if len(agents_spec) > policy["max_nodes"]:
            raise WorkflowError(
                f"初始 agents 超过 max_nodes={policy['max_nodes']}")
        with self._lock:
            if self._closed:
                raise WorkflowError("workflow manager 已关闭")

        lineup = self.resolve_lineup()
        by_seat = {row["seat"]: row for row in lineup}

        # 1) 归一化 agents：分配 key、解析 seat/model、校验 depends_on
        nodes = {}
        order = []
        for i, raw in enumerate(agents_spec, 1):
            if not isinstance(raw, dict):
                raise WorkflowError(f"agents[{i}] 必须是对象")
            key = str(raw.get("key") or f"a{i}").strip()
            if not _NODE_KEY.fullmatch(key) or key in nodes:
                raise WorkflowError(
                    f"agents[{i}] key 无效或重复：{key!r}")
            task = str(raw.get("task") or "").strip()
            if not task:
                raise WorkflowError(f"agents[{i}] task 不能为空")
            if len(task) > 4000:
                raise WorkflowError(f"agents[{i}] task 最多 4000 字符")

            seat = raw.get("seat")
            model = raw.get("model")
            gateway = raw.get("gateway")
            resolved = None
            if seat:
                seat = str(seat).strip()
                resolved = by_seat.get(seat)
                if resolved is None:
                    raise WorkflowError(
                        f"席位 {seat!r} 当前不可用（缺席宁可留空，不降级）")
                model = resolved["id"]
                gateway = resolved["gateway"]
            elif model:
                model = str(model).strip()
                if len(model) > 256:
                    raise WorkflowError(f"agents[{i}].model 最多 256 字符")
                if gateway:
                    gateway = str(gateway).strip()
                else:
                    gateway, _ = models.resolve(model)
                if not gateway:
                    raise WorkflowError(f"模型 {model!r} 不在任何已知网关")
                if not self._route_allowed(gateway, model):
                    raise WorkflowError(
                        f"模型 route {model}@{gateway} 当前 health circuit open")
            else:
                # 未指定席位/模型：轮流分到不同席位——异构是默认期望，但同一席位
                # 派多个节点也允许，所以席位用完不再报错，而是回头再轮。
                if not lineup:
                    raise WorkflowError(
                        "当前没有可用的旗舰席位（Kimi/GLM/DeepSeek/Qwen 都缺席）")
                resolved = lineup[len(nodes) % len(lineup)]
                seat = resolved["seat"]
                model = resolved["id"]
                gateway = resolved["gateway"]

            raw_dependencies = raw.get("depends_on") or []
            if (not isinstance(raw_dependencies, list)
                    or len(raw_dependencies) > policy["max_nodes"]):
                raise WorkflowError(
                    f"agents[{i}].depends_on 必须是 key array")
            dependencies = [str(k).strip() for k in raw_dependencies]
            if (any(not _NODE_KEY.fullmatch(k) for k in dependencies)
                    or len(set(dependencies)) != len(dependencies)):
                raise WorkflowError(
                    f"agents[{i}].depends_on 含无效或重复 key")
            context = str(raw.get("context") or "")
            if len(context) > 12_000:
                raise WorkflowError(
                    f"agents[{i}].context 最多 12000 字符")
            nodes[key] = {
                "key": key,
                "seat": seat,
                "family": (
                    resolved.get("family") if resolved is not None else None),
                "model": model,
                "gateway": gateway,
                "task": task,
                "role": (
                    str(raw.get("role") or "scout").strip() or "scout")[:64],
                "context": context,
                "depends_on": dependencies,
                "state": "queued",
                "report": "",
                "error": "",
                "agent_id": "",
                "attempts": 0,
                "previous_agent_ids": [],
            }
            order.append(key)

        routes = {
            (node["gateway"], node["model"]) for node in nodes.values()}
        if (len(nodes) >= HETEROGENEITY_FLOOR_NODES and len(routes) < 2):
            raise WorkflowError(
                f"{len(nodes)} 个节点却只用了一个 model route："
                f"≥{HETEROGENEITY_FLOOR_NODES} 个节点的 DAG 至少要用 2 个不同席位"
                "——不同模型的交叉验证与观点碰撞是这个工具的价值；"
                "2 个节点才允许同席位")
        notes = []
        if len(routes) < 2:
            notes.append("本次 DAG 只用了一个 model route，没有异构交叉验证")

        # 2) DAG 校验：悬空引用 + 循环检测
        for key, node in nodes.items():
            for dep in node["depends_on"]:
                if dep not in nodes:
                    raise WorkflowError(f"{key}: depends_on 引用了不存在的 {dep!r}")
                if dep == key:
                    raise WorkflowError(f"{key}: 不能依赖自己")
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {k: WHITE for k in nodes}
        def visit(k, stack=()):
            color[k] = GRAY
            for dep in nodes[k]["depends_on"]:
                if color[dep] == GRAY:
                    cycle = " -> ".join(stack + (k, dep))
                    raise WorkflowError(f"depends_on 存在循环：{cycle}")
                if color[dep] == WHITE:
                    visit(dep, stack + (k,))
            color[k] = BLACK
        for k in order:
            if color[k] == WHITE:
                visit(k)

        # 3) 请求预算是所有 provider wire attempts 的硬上限，包括 tool loop、
        # compaction、temperature fallback、transient retry 和 crash recovery。
        # review/synthesis 已是普通节点，都在 nodes 里。
        estimated_requests = len(nodes)
        requested_budget = (
            policy["max_requests"] if budget is None else budget)
        if isinstance(requested_budget, bool):
            raise WorkflowError("workflow budget 必须是整数")
        try:
            requested_budget = int(requested_budget)
        except (TypeError, ValueError, OverflowError):
            raise WorkflowError("workflow budget 必须是整数") from None
        if not 2 <= requested_budget <= policy["max_requests"]:
            raise WorkflowError(
                "workflow budget 必须在 2.."
                f"{policy['max_requests']} requests 之间")
        if estimated_requests > requested_budget:
            raise WorkflowError(
                f"workflow 计划需要 {estimated_requests} requests，"
                f"超过 budget {requested_budget}；请减少节点或提高 budget")

        selected = []
        selected_routes = set()
        for node in nodes.values():
            route = (node["gateway"], node["model"])
            if route in selected_routes:
                continue
            selected_routes.add(route)
            selected.append({
                "seat": node.get("seat") or node["key"],
                "family": node.get("family"),
                "id": node["model"],
                "gateway": node["gateway"],
                "quality_tier": "flagship",
            })
        expected_seats = [
            item["seat"] for item in models.WORKFLOW_DEFAULT_SEATS]
        available_seats = {row["seat"] for row in lineup}
        selected_seats = {row["seat"] for row in selected}
        missing = [
            seat for seat in expected_seats if seat not in available_seats]
        standby = [
            seat for seat in expected_seats
            if seat in available_seats and seat not in selected_seats]

        # 6) 建 record 并启动驱动线程。必须采用 store.create() 实际分配的 id。
        now = _now()
        record = self.store.create({
            "version": VERSION,
            "id": None,
            "parent_session_id": str(parent_session_id or ""),
            "parent_turn_id": str(parent_turn_id or ""),
            "owner_id": self.owner_id,
            **_owner_fields(),
            "goal": goal,
            "context_capsule": _copy(context_capsule or {}) or None,
            "mode": mode,
            "preset": str(preset or "adaptive"),
            "trigger": str(trigger or "model"),
            "recipe": _copy(recipe or {}) or None,
            "state": "queued",
            "stage": "queued",
            "apply_status": "queued",
            "auto_apply": (
                bool(mode == "implement")
                if auto_apply is None else bool(auto_apply)),
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "ended_at": "",
            "lineup": self._public_lineup(selected),
            "candidate_lineup": self._public_lineup(lineup),
            "notes": notes,
            "preflight": [],
            "missing_seats": missing,
            "standby_seats": standby,
            "agents": [],
            "completed_agents": 0,
            "expected_agents": estimated_requests,
            "requests_started": 0,
            "request_counter_mode": "provider_attempt_v2",
            "tokens_used": 0,
            "budget": {
                "estimated_requests": estimated_requests,
                "planned_requests": estimated_requests,
                "max_requests": requested_budget,
                "max_requests_per_agent": (
                    policy["max_requests_per_agent"]),
                "max_node_attempts": policy["max_node_attempts"],
                "max_nodes": policy["max_nodes"],
                "max_parallel": policy["max_parallel"],
                "max_per_gateway": policy["max_per_gateway"],
                "max_tokens": policy["max_tokens"],
                "max_elapsed_seconds": policy["max_elapsed_seconds"],
            },
            "recovery_count": 0,
            "plan": {
                "agents": [nodes[k] for k in order],
                # 兼容旧读者：review/synthesis 不再是独立阶段，都是普通节点
                "review": {"enabled": False, "reviewers": [], "rounds": 1},
                "synthesis": None,
            },
            "scouts": [],
            "reviews": [],
            "review_rounds_completed": 0,
            "synthesis": None,
            "combined_report": "",
            "result": "",
            "error": "",
            "events": [],
        })
        workflow_id = record["id"]
        driver = _WorkflowDriver(hook_config)
        driver.thread = threading.Thread(
            target=self._drive_plan,
            args=(workflow_id, driver),
            name=f"zylab-workflow-{workflow_id}",
            daemon=True)
        with self._lock:
            if self._closed:
                driver.cancel.set()
                self.store.update(workflow_id, lambda item: item.update({
                    "state": "cancelled", "stage": "cancelled",
                    "error": "manager closed during start",
                    "ended_at": _now(),
                }))
                raise WorkflowError("workflow manager 已关闭")
            self._drivers[workflow_id] = driver
        self._emit("workflow_started", record, lineup=record["lineup"])
        driver.thread.start()
        return record

    def _reserve_request(self, workflow_id, details=None):
        """Atomically reserve one real provider attempt before network I/O.

        ``requests_started`` remains the public/backward-compatible field, but
        v2 increments here rather than at child spawn.  A recovered v1 record
        may therefore conservatively include earlier launch reservations; it
        can under-use the remaining budget but can never exceed it.
        """
        source = details if isinstance(details, dict) else {}
        projection = {
            key: str(source.get(key) or "")[:256]
            for key in (
                "purpose", "model", "gateway", "request_id", "turn_id", "actor")
            if source.get(key) is not None
        }
        try:
            projection["attempt"] = max(1, int(source.get("attempt") or 1))
        except (TypeError, ValueError, OverflowError):
            projection["attempt"] = 1

        def mutate(item):
            budget = item.get("budget") or {}
            actor = str(projection.get("actor") or "")
            actor_limit = int(
                budget.get("max_requests_per_agent") or 0)
            actor_started = sum(
                1 for event in item.get("events") or []
                if event.get("kind") == "provider_attempt_started"
                and str((event.get("payload") or {}).get("actor") or "")
                == actor)
            if actor and actor_limit and actor_started >= actor_limit:
                raise WorkflowError(
                    "workflow agent provider-attempt limit exhausted: "
                    f"{actor} {actor_started}/{actor_limit}")
            maximum = budget.get("max_requests")
            started = int(item.get("requests_started") or 0)
            if maximum is not None and started >= int(maximum):
                raise WorkflowError(
                    "workflow provider-attempt budget exhausted: "
                    f"{started}/{maximum}")
            item["requests_started"] = started + 1
            if not item.get("request_counter_mode"):
                item["request_counter_mode"] = (
                    "legacy_conservative_v1+provider_attempt_v2")
            self._append_control_event(
                item, "provider_attempt_started", projection)
            return {
                "started": item["requests_started"],
                "maximum": maximum,
                "counter_mode": item["request_counter_mode"],
            }
        return self.store.update(workflow_id, mutate)

    def _reserve_actor_request(
            self, workflow_id, actor, details=None):
        payload = dict(details or {}) if isinstance(details, dict) else {}
        payload["actor"] = str(actor or "")[:256]
        return self._reserve_request(workflow_id, payload)

    def _plan_node_update(self, workflow_id, key, **changes):
        def mutate(item):
            for node in (item.get("plan") or {}).get("agents") or []:
                if node.get("key") == key:
                    node.update(_copy(changes))
                    return item
            raise WorkflowError(f"workflow node 不存在：{key}")
        record = self.store.update(workflow_id, mutate)
        if "state" in changes:
            # J7：节点与子代理走同一套完成行/名册行，TUI 只认这个事件，不重读文件
            node = next((item for item in (record.get("plan") or {}).get("agents") or []
                         if item.get("key") == key), None)
            if node is not None:
                self._emit("workflow_node", record, node=_copy(node))
        return record

    @staticmethod
    def _append_control_event(record, kind, payload=None):
        events = record.setdefault("events", [])
        seq = int(events[-1].get("seq") or 0) + 1 if events else 1
        events.append({
            "seq": seq, "ts": _now(), "kind": str(kind),
            "payload": _copy(payload or {}),
        })
        if len(events) > 256:
            del events[:-256]

    @staticmethod
    def _validate_node_graph(nodes):
        by_key = {str(node.get("key") or ""): node for node in nodes}
        if "" in by_key or len(by_key) != len(nodes):
            raise WorkflowError("workflow node key 为空或重复")
        for key, node in by_key.items():
            dependencies = node.get("depends_on") or []
            if not isinstance(dependencies, list):
                raise WorkflowError(f"{key}.depends_on 必须是 array")
            for dependency in dependencies:
                if dependency not in by_key:
                    raise WorkflowError(
                        f"{key}: depends_on 引用了不存在的 {dependency!r}")
                if dependency == key:
                    raise WorkflowError(f"{key}: 不能依赖自己")
        white, gray, black = 0, 1, 2
        colors = {key: white for key in by_key}

        def visit(key, stack=()):
            colors[key] = gray
            for dependency in by_key[key].get("depends_on") or []:
                if colors[dependency] == gray:
                    cycle = " -> ".join(stack + (key, dependency))
                    raise WorkflowError(f"depends_on 存在循环：{cycle}")
                if colors[dependency] == white:
                    visit(dependency, stack + (key,))
            colors[key] = black

        for key in by_key:
            if colors[key] == white:
                visit(key)

    def add_nodes(self, identifier, nodes_spec, *, requested_by="user"):
        """Append validated queued nodes to a live durable DAG.

        This is deliberately data-only: no generated Python/shell is executed.
        The existing scheduler observes the atomic append on its next poll.
        """
        if not isinstance(nodes_spec, list) or not nodes_spec:
            raise WorkflowError("add_nodes 至少需要 1 个 node")
        if len(nodes_spec) > MAX_DYNAMIC_ADD_NODES:
            raise WorkflowError(
                f"单次 add_nodes 最多 {MAX_DYNAMIC_ADD_NODES} 个 node")
        lineup = self.resolve_lineup()
        if not lineup:
            raise WorkflowError("当前没有合格 workflow model route")
        by_seat = {row["seat"]: row for row in lineup}
        normalized = []
        for index, raw in enumerate(nodes_spec, 1):
            if not isinstance(raw, dict):
                raise WorkflowError(f"nodes[{index}] 必须是 object")
            key = str(raw.get("key") or "").strip()
            if not _NODE_KEY.fullmatch(key):
                raise WorkflowError(
                    f"nodes[{index}].key 必须是 1..32 位字母开头短标识")
            task = str(raw.get("task") or "").strip()
            if not task or len(task) > 4000:
                raise WorkflowError(
                    f"nodes[{index}].task 必须是 1..4000 字符")
            context = str(raw.get("context") or "")
            if len(context) > 12_000:
                raise WorkflowError(
                    f"nodes[{index}].context 最多 12000 字符")
            seat = str(raw.get("seat") or "").strip()
            model = str(raw.get("model") or "").strip()
            gateway = str(raw.get("gateway") or "").strip()
            resolved = None
            if seat:
                resolved = by_seat.get(seat)
                if resolved is None:
                    raise WorkflowError(
                        f"动态节点席位 {seat!r} 当前不可用")
                model, gateway = resolved["id"], resolved["gateway"]
            elif model:
                if len(model) > 256:
                    raise WorkflowError(
                        f"nodes[{index}].model 最多 256 字符")
                gateway = gateway or str(models.resolve(model)[0] or "")
                if not gateway or not self._route_allowed(gateway, model):
                    raise WorkflowError(
                        f"动态节点 route {model}@{gateway or '?'} 不可用")
            else:
                resolved = lineup[(index - 1) % len(lineup)]
                seat = resolved["seat"]
                model, gateway = resolved["id"], resolved["gateway"]
            dependencies = raw.get("depends_on") or []
            if (not isinstance(dependencies, list)
                    or len(dependencies) > MAX_PLAN_NODES):
                raise WorkflowError(
                    f"nodes[{index}].depends_on 必须是 array")
            dependencies = [str(item).strip() for item in dependencies]
            if (any(not _NODE_KEY.fullmatch(item) for item in dependencies)
                    or len(set(dependencies)) != len(dependencies)):
                raise WorkflowError(
                    f"nodes[{index}].depends_on 含无效或重复 key")
            normalized.append({
                "key": key,
                "seat": seat or None,
                "family": (
                    resolved.get("family") if resolved is not None else None),
                "model": model,
                "gateway": gateway,
                "task": task,
                "role": (
                    str(raw.get("role") or "scout").strip() or "scout")[:64],
                "context": context,
                "depends_on": dependencies,
                "state": "queued",
                "report": "",
                "error": "",
                "agent_id": "",
                "attempts": 0,
                "previous_agent_ids": [],
                "added_at": _now(),
                "added_by": str(requested_by or "user"),
            })

        def mutate(record):
            if record.get("state") not in ACTIVE_STATES:
                raise WorkflowError(
                    f"workflow {record.get('id')} 已是 {record.get('state')}，"
                    "不能追加节点")
            if record.get("stage") not in {
                    "queued", "agents", "recovering", "paused"}:
                raise WorkflowError(
                    f"workflow 已进入 {record.get('stage')}，不能再改变 DAG")
            plan = record.get("plan") or {}
            existing = list(plan.get("agents") or [])
            budget = record.get("budget") or {}
            maximum_nodes = int(
                budget.get("max_nodes") or MAX_PLAN_NODES)
            if len(existing) + len(normalized) > maximum_nodes:
                raise WorkflowError(
                    f"动态扩展超过 max_nodes={maximum_nodes}")
            existing_keys = {node.get("key") for node in existing}
            duplicate = [node["key"] for node in normalized
                         if node["key"] in existing_keys]
            if duplicate or len({node["key"] for node in normalized}) != len(
                    normalized):
                raise WorkflowError(
                    "动态 node key 重复：" + ", ".join(duplicate or [
                        node["key"] for node in normalized]))
            candidate = [*existing, *_copy(normalized)]
            self._validate_node_graph(candidate)
            planned = int(
                budget.get("planned_requests")
                or budget.get("estimated_requests") or len(existing))
            maximum_requests = int(
                budget.get("max_requests") or MAX_PLAN_REQUESTS)
            if planned + len(normalized) > maximum_requests:
                raise WorkflowError(
                    f"动态扩展需要 {planned + len(normalized)} planned requests，"
                    f"超过 max_requests={maximum_requests}")
            started = int(record.get("requests_started") or 0)
            if maximum_requests and started >= maximum_requests:
                raise WorkflowError(
                    "动态扩展被预算拒绝：workflow provider-attempt "
                    f"budget exhausted: {started}/{maximum_requests}")
            stopped = self._budget_stop_reason(record)
            if stopped:
                raise WorkflowError(f"动态扩展被预算拒绝：{stopped}")
            plan["agents"] = candidate
            record["plan"] = plan
            budget["planned_requests"] = planned + len(normalized)
            record["budget"] = budget
            record["expected_agents"] = int(
                record.get("expected_agents") or 0) + len(normalized)
            self._append_control_event(record, "nodes_added", {
                "keys": [node["key"] for node in normalized],
                "requested_by": str(requested_by or "user"),
            })
            return record

        updated = self.store.update(identifier, mutate)
        self._emit(
            "workflow_nodes_added", updated,
            nodes=[node["key"] for node in normalized],
            requested_by=str(requested_by or "user"))
        return updated

    def send_node(self, identifier, node_key, message, *, requested_by="user"):
        record = self.store.get(identifier)
        if record.get("state") not in ACTIVE_STATES:
            raise WorkflowError(
                f"workflow {record['id']} 已是 {record.get('state')}")
        node = next((item for item in (record.get("plan") or {}).get(
            "agents", []) if item.get("key") == str(node_key)), None)
        if node is None:
            raise WorkflowError(f"workflow node 不存在：{node_key}")
        if node.get("state") != "running" or not node.get("agent_id"):
            raise WorkflowError(
                f"node {node_key} 当前是 {node.get('state')}；"
                "只有 running node 能接收协作消息")
        text = str(message or "").strip()
        if not text or len(text) > 4000:
            raise WorkflowError("node message 必须是 1..4000 字符")
        queued = self.workspace.send(node["agent_id"], text)

        def mutate(item):
            self._append_control_event(item, "node_message", {
                "node": str(node_key),
                "agent_id": node["agent_id"],
                "message_id": queued.get("id"),
                "requested_by": str(requested_by or "user"),
            })
            return item

        updated = self.store.update(record["id"], mutate)
        self._emit(
            "workflow_node_message", updated,
            node=str(node_key), agent_id=node["agent_id"])
        return {"workflow": updated, "message": queued}

    def status_projection(self, identifier):
        record = self.get(identifier)
        nodes = []
        for item in (record.get("plan") or {}).get("agents") or []:
            nodes.append({key: _copy(item.get(key)) for key in (
                "key", "role", "seat", "model", "gateway", "depends_on",
                "state", "attempts", "agent_id", "error")})
        return {
            "id": record["id"],
            "state": record.get("state"),
            "stage": record.get("stage"),
            "goal": record.get("goal"),
            "requests_started": int(record.get("requests_started") or 0),
            "request_counter_mode": record.get("request_counter_mode"),
            "tokens_used": int(record.get("tokens_used") or 0),
            "completed_agents": int(record.get("completed_agents") or 0),
            "expected_agents": int(record.get("expected_agents") or 0),
            "budget": _copy(record.get("budget") or {}),
            "nodes": nodes,
        }

    @staticmethod
    def _budget_stop_reason(workflow):
        """Return an observed hard-stop reason before scheduling more work."""
        budget = workflow.get("budget") or {}
        used = int(workflow.get("tokens_used") or 0)
        maximum_tokens = int(budget.get("max_tokens") or 0)
        if maximum_tokens and used >= maximum_tokens:
            return (
                f"workflow token budget exhausted: "
                f"{used}/{maximum_tokens} observed tokens")
        maximum_elapsed = int(budget.get("max_elapsed_seconds") or 0)
        started = str(workflow.get("started_at") or "")
        if maximum_elapsed and started:
            try:
                started_at = datetime.fromisoformat(
                    started.replace("Z", "+00:00"))
                elapsed = (
                    datetime.now(timezone.utc) - started_at).total_seconds()
            except (TypeError, ValueError):
                elapsed = 0
            if elapsed >= maximum_elapsed:
                return (
                    f"workflow elapsed budget exhausted: "
                    f"{elapsed:.1f}/{maximum_elapsed}s")
        return None

    @staticmethod
    def _bounded_agent_task(task, budget):
        limit = int(
            (budget or {}).get("max_requests_per_agent") or 0)
        if not limit:
            return str(task)
        return (
            f"{task}\n\n执行预算：本 child 最多 {limit} 次 provider 回合。"
            "优先使用 repo map、grep 和小范围读取；必须预留最后一次回合"
            "输出可交接结论，不要无界探索。")

    @staticmethod
    def _plan_context(node, nodes):
        blocks = []
        base = str(node.get("context") or "").strip()
        if base:
            blocks.append(base)
        for dependency in node.get("depends_on") or []:
            source = nodes[dependency]
            report = agents.project_report(source.get("report"))
            if report:
                blocks.append(
                    f"## dependency {dependency} "
                    f"[{source.get('model')}@{source.get('gateway')}]\n"
                    + report)
        return "\n\n".join(blocks) or None

    @staticmethod
    def _plan_reports(workflow):
        blocks = []
        failures = []
        for node in (workflow.get("plan") or {}).get("agents") or []:
            report = agents.project_report(node.get("report"))
            if node.get("state") == "completed" and report:
                blocks.append(
                    f"## {node.get('key')} · "
                    f"{node.get('seat') or node.get('role')} "
                    f"[{node.get('model')}@{node.get('gateway')}]\n"
                    + report[:agents.MAX_REPORT])
            elif node.get("state") in {"failed", "cancelled"}:
                failures.append(
                    f"- {node.get('key')}: "
                    f"{node.get('error') or node.get('state')}")
        if failures:
            blocks.append("# Incomplete nodes\n\n" + "\n".join(failures))
        return "\n\n".join(blocks)

    @staticmethod
    def _raise_provider_budget_failure(records):
        """Do not label a budget-blocked required stage as completed."""
        for record in records or ():
            error = str(record.get("error") or "")
            if "provider-attempt budget exhausted" in error:
                raise WorkflowError(error)

    @staticmethod
    def _clip_combined(value):
        value = str(value or "").strip()
        if len(value) <= MAX_COMBINED_REPORT:
            return value
        keep = MAX_COMBINED_REPORT
        return (
            value[:keep * 2 // 3]
            + f"\n\n…[workflow combined report 截断 "
            f"{len(value) - keep} 字符]…\n\n"
            + value[-keep // 3:])

    def _run_plan_nodes(self, workflow_id, driver):
        active = {}
        try:
            while True:
                if driver.cancel.is_set():
                    return "cancelled"
                if driver.pause.is_set():
                    return "paused"

                changed = False
                for agent_id, spec in list(active.items()):
                    try:
                        child = self.workspace.get(agent_id)
                    except agents.AgentRuntimeError as exc:
                        child = {
                            "id": agent_id,
                            "name": spec["name"],
                            "model": spec["model"],
                            "gateway": spec["gateway"],
                            "state": "failed",
                            "result": None,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    if child.get("state") not in agents.TERMINAL_STATES:
                        continue
                    active.pop(agent_id, None)
                    self._release_slot(spec["gateway"])
                    self._refresh_agent(
                        workflow_id, child, phase="scout",
                        seat=spec["seat"])
                    self._plan_node_update(
                        workflow_id, spec["key"],
                        state=child.get("state"),
                        ended_at=_now(),          # 节点完成行要算耗时（PLAN-agent-visibility）
                        report=agents.project_report(
                            child.get("result"))[:agents.MAX_REPORT],
                        error=str(child.get("error") or ""),
                    )
                    changed = True

                workflow = self.store.get(workflow_id)
                stopped = self._budget_stop_reason(workflow)
                if stopped:
                    raise WorkflowError(stopped)
                node_list = (workflow.get("plan") or {}).get("agents") or []
                nodes = {node["key"]: node for node in node_list}

                # A failed prerequisite cannot silently turn into empty context.
                # Independent branches remain useful, blocked branches are
                # explicit in the final evidence.
                for node in node_list:
                    if node.get("state") != "queued":
                        continue
                    blocked = [
                        dep for dep in node.get("depends_on") or []
                        if nodes[dep].get("state") in {"failed", "cancelled"}]
                    if not blocked:
                        continue
                    self._plan_node_update(
                        workflow_id, node["key"], state="cancelled",
                        ended_at=_now(),
                        error="dependency failed: " + ", ".join(blocked))
                    changed = True

                workflow = self.store.get(workflow_id)
                node_list = (workflow.get("plan") or {}).get("agents") or []
                nodes = {node["key"]: node for node in node_list}
                queued = [
                    node for node in node_list
                    if node.get("state") == "queued"]
                if not queued and not active:
                    self._raise_provider_budget_failure(node_list)
                    return "completed"

                ready = [
                    node for node in queued
                    if all(
                        nodes[dep].get("state") == "completed"
                        for dep in node.get("depends_on") or [])]
                launched = False
                budget = workflow.get("budget") or {}
                for node in ready:
                    if len(active) >= int(
                            budget.get("max_parallel") or MAX_PARALLEL):
                        break
                    if driver.cancel.is_set() or driver.pause.is_set():
                        break
                    gateway = node["gateway"]
                    if not self._try_claim_slot(
                            gateway,
                            max_parallel=budget.get("max_parallel"),
                            max_per_gateway=budget.get(
                                "max_per_gateway")):
                        continue
                    try:
                        execution = self._execution_context(
                            workflow, node, driver.hook_config)
                        child = self.workspace.spawn(
                            self._bounded_agent_task(
                                node["task"], budget),
                            self._plan_context(node, nodes),
                            execution_context=execution,
                            name=(
                                f"{node.get('seat') or node['key']} "
                                f"{node.get('role') or 'scout'}"),
                            kind=f"workflow-{node.get('role') or 'scout'}",
                            parent_tool_call_id=workflow_id,
                            context_capsule=workflow.get(
                                "context_capsule"),
                            before_provider_attempt=(
                                lambda details,
                                workflow_id=workflow_id,
                                actor=f"node:{node['key']}":
                                self._reserve_actor_request(
                                    workflow_id, actor, details)))
                    except BaseException:
                        self._release_slot(gateway)
                        raise
                    spec = {
                        "key": node["key"],
                        "seat": node.get("seat") or node["key"],
                        "model": node["model"],
                        "gateway": gateway,
                        "name": child.get("name"),
                    }
                    # spawn 是外部提交点。必须先纳入 finally 清理表，再做
                    # node/agent projection 持久化；后者失败时不能留下孤儿 child。
                    active[child["id"]] = spec
                    previous = list(node.get("previous_agent_ids") or [])
                    if node.get("agent_id"):
                        previous.append(node["agent_id"])
                    self._plan_node_update(
                        workflow_id, node["key"], state="running",
                        started_at=_now(),
                        agent_id=child["id"],
                        previous_agent_ids=previous,
                        attempts=int(node.get("attempts") or 0) + 1,
                        error="")
                    self._add_agent(
                        workflow_id, child, phase="scout",
                        seat=spec["seat"])
                    launched = True
                    if (driver.cancel.wait(LAUNCH_STAGGER)
                            or driver.pause.is_set()):
                        break

                if (not active and queued and not ready
                        and not changed):
                    raise WorkflowError(
                        "workflow DAG 无可运行节点；依赖状态不一致")
                if active or queued:
                    if driver.pause.wait(
                            self.poll_interval
                            if (launched or changed) else 0.1):
                        return "paused"
        finally:
            if active:
                self._stop_active(active)

    def _preflight_plan(self, workflow_id, driver):
        """启动前探活：对 plan 将要用到的每个 model route 发一次真实小请求。

        坏的 route 按该席位的候选顺序换下一条（写回节点）；某席位全坏就在花任何
        预算之前把 workflow 标失败并说明是谁坏了。探活走 models.probe，顺带刷新
        健康缓存与 circuit。返回 False 表示不该继续。
        """
        workflow = self.store.get(workflow_id)
        nodes = (workflow.get("plan") or {}).get("agents") or []
        if self.preflight_probe is None or not nodes:
            return True
        pending = [
            node for node in nodes
            if node.get("state") == "queued" and not node.get("agent_id")]
        if not pending:
            return True                       # 恢复中的 workflow：节点已在跑
        self._set_stage(workflow_id, "preflight")
        routes = {}
        for node in pending:
            routes.setdefault(
                (str(node.get("gateway") or ""), str(node.get("model") or "")),
                node.get("seat"))
        results = {}

        def check(route):
            gateway, model = route
            started = time.monotonic()
            try:
                record = self.preflight_probe(
                    gateway, model, timeout=PREFLIGHT_TIMEOUT) or {}
                ok = record.get("status") == "ok"
                error = "" if ok else str(
                    record.get("error") or record.get("status") or "不可用")
            except Exception as exc:
                ok, error = False, f"{type(exc).__name__}: {exc}"
            return {
                "gateway": gateway, "model": model, "ok": bool(ok),
                "error": error[:200],
                "seconds": round(time.monotonic() - started, 1),
            }

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=PREFLIGHT_PARALLEL) as pool:
            for route, outcome in zip(routes, pool.map(check, list(routes))):
                results[route] = outcome
        replacements = {}
        failures = []
        for route, seat in routes.items():
            if results[route]["ok"]:
                continue
            replaced = False
            if seat:
                for candidate in self.seat_routes(
                        seat, route_allowed=self._route_allowed):
                    alt = (str(candidate.get("gateway") or ""),
                           str(candidate.get("id") or ""))
                    if alt == route:
                        continue
                    if alt not in results:
                        results[alt] = check(alt)
                    if results[alt]["ok"]:
                        replacements[route] = alt
                        replaced = True
                        break
            if not replaced:
                failures.append(
                    f"{seat or route[1]}@{route[0]}: "
                    f"{results[route]['error'] or '不可用'}")
        seat_of = dict(routes)

        def write(item):
            for node in (item.get("plan") or {}).get("agents") or []:
                key = (str(node.get("gateway") or ""), str(node.get("model") or ""))
                if key in replacements and node.get("state") == "queued":
                    node["gateway"], node["model"] = replacements[key]
            item["preflight"] = [
                dict(value, seat=seat_of.get(route),
                     replaced_by=(
                         f"{replacements[route][1]}@{replacements[route][0]}"
                         if route in replacements else None))
                for route, value in results.items()]
        workflow = self.store.update(workflow_id, write)
        if driver.cancel.is_set():
            self._finish(workflow_id, "cancelled", error="用户取消")
            return False
        if failures:
            self._finish(
                workflow_id, "failed",
                error="启动前探活失败，未花费预算：" + "；".join(failures))
            return False
        self._emit("workflow_progress", workflow)
        return True

    def _drive_plan(self, workflow_id, driver):
        """Run/re-run a durable DAG; completed node reports are checkpoints."""
        try:
            workflow = self.store.update(
                workflow_id, lambda item: item.update({
                    "state": "running",
                    "stage": (
                        item.get("stage")
                        if item.get("stage") not in {"queued", "paused"}
                        else "agents"),
                    "owner_id": self.owner_id,
                    **_owner_fields(),
                    "started_at": item.get("started_at") or _now(),
                    "error": "",
                }))
            self._emit("workflow_progress", workflow)
            if not self._preflight_plan(workflow_id, driver):
                return
            node_outcome = self._run_plan_nodes(workflow_id, driver)
            if node_outcome == "paused":
                return
            if node_outcome == "cancelled" or driver.cancel.is_set():
                self._finish(
                    workflow_id, "cancelled", error="用户取消")
                return

            workflow = self.store.get(workflow_id)
            combined = str(workflow.get("combined_report") or "").strip()
            if not combined:
                combined = self._plan_reports(workflow)
            successful = [
                node for node in (workflow.get("plan") or {}).get(
                    "agents", [])
                if node.get("state") == "completed"
                and str(node.get("report") or "").strip()]
            if not successful:
                raise WorkflowError("所有 DAG agents 均失败或没有报告")

            # DAG 是唯一拓扑：若有 role=synthesis 的节点完成，它的报告就是结果；
            # 否则结果是全部完成节点的合并报告（含 review 节点）。
            synthesis_node = next(
                (node for node in (workflow.get("plan") or {}).get("agents", [])
                 if str(node.get("role") or "") == "synthesis"
                 and node.get("state") == "completed"
                 and str(node.get("report") or "").strip()),
                None)
            combined = self._clip_combined(combined)
            self.store.update(
                workflow_id, lambda item: item.update({
                    "combined_report": combined}))
            result = (
                agents.project_report(synthesis_node.get("report")).strip()
                if synthesis_node else combined)
            self._finish(workflow_id, "completed", result=result)
        except BaseException as exc:
            if driver.pause.is_set():
                return
            if driver.cancel.is_set():
                self._finish(
                    workflow_id, "cancelled", error="用户取消")
            else:
                # 预算耗尽/部分节点失败：已产出的报告随失败一并带回，不白花
                partial = ""
                try:
                    partial = self._plan_reports(self.store.get(workflow_id))
                except Exception:
                    partial = ""
                self._finish(
                    workflow_id, "failed",
                    result=partial or None,
                    error=f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._drivers.pop(workflow_id, None)


    def _set_stage(self, workflow_id, stage):
        record = self.store.update(workflow_id, lambda item: item.update({
            "state": "running",
            "stage": str(stage),
        }))
        self._emit("workflow_progress", record)
        return record

    def _add_agent(self, workflow_id, record, *, phase, seat):
        projection = self._agent_projection(
            record, phase=phase, seat=seat)

        def mutate(item):
            item.setdefault("agents", []).append(projection)
            if phase == "scout":
                for row in item.get("lineup") or []:
                    if row.get("seat") == seat:
                        row["agent_id"] = record["id"]
                        row["state"] = record.get("state")
                        break

        current = self.store.update(workflow_id, mutate)
        self._emit(
            "workflow_agent_started", current,
            agent=projection)
        return current

    def _refresh_agent(self, workflow_id, record, *, phase, seat):
        projection = self._agent_projection(
            record, phase=phase, seat=seat)

        def mutate(item):
            replaced = False
            for index, row in enumerate(item.get("agents") or []):
                if row.get("id") == record["id"]:
                    item["agents"][index] = projection
                    replaced = True
                    break
            if not replaced:
                item.setdefault("agents", []).append(projection)
            if phase == "scout":
                for row in item.get("lineup") or []:
                    if row.get("seat") == seat:
                        row["agent_id"] = record["id"]
                        row["state"] = record.get("state")
                        break
            item["completed_agents"] = sum(
                1 for row in item.get("agents") or []
                if row.get("state") in agents.TERMINAL_STATES)
            item["tokens_used"] = sum(
                int(row.get("tokens_in") or 0)
                + int(row.get("tokens_out") or 0)
                for row in item.get("agents") or [])

        current = self.store.update(workflow_id, mutate)
        self._emit(
            "workflow_agent_finished", current,
            agent=projection)
        return current

    @staticmethod
    def _execution_context(workflow, spec, hook_config):
        return tools.ExecutionContext.capture(
            session=workflow.get("parent_session_id"),
            turn_id=workflow.get("id"),
            model=spec["model"],
            gateway=spec["gateway"],
            permission_mode="workflow-read-only",
            interaction_role="workflow",
            hook_config=hook_config,
            workspace_root=workflow.get("cwd") or None)

    def _try_claim_slot(self, gateway, *, max_parallel=None,
                        max_per_gateway=None):
        """原子领取进程级 provider slot；多个 workflow 共用同一配额。"""
        gateway = str(gateway or "unknown")
        with self._lock:
            if self._closed:
                return False
            active = int(self._active_by_gateway.get(gateway, 0))
            total_limit = min(
                MAX_PARALLEL, int(max_parallel or MAX_PARALLEL))
            gateway_limit = min(
                MAX_PER_GATEWAY,
                int(max_per_gateway or MAX_PER_GATEWAY))
            if (self._active_total >= total_limit
                    or active >= gateway_limit):
                return False
            self._active_total += 1
            self._active_by_gateway[gateway] = active + 1
            return True

    def _release_slot(self, gateway):
        gateway = str(gateway or "unknown")
        with self._lock:
            active = int(self._active_by_gateway.get(gateway, 0))
            if active <= 0:
                return
            if active == 1:
                self._active_by_gateway.pop(gateway, None)
            else:
                self._active_by_gateway[gateway] = active - 1
            self._active_total = max(0, self._active_total - 1)

    def _release_after_stop(self, agent_id, gateway):
        """取消超过同步等待窗口时，继续持有 slot 到 worker 真正退出。"""
        try:
            self.workspace.wait(agent_id)
        finally:
            self._release_slot(gateway)

    def _stop_active(self, active):
        """异常/取消边界必须收束已启动 child，并归还所有 provider slots。"""
        identifiers = list(active)
        for agent_id in identifiers:
            try:
                self.workspace.cancel(agent_id)
            except (agents.AgentRuntimeError, ValueError):
                pass
        for agent_id in identifiers:
            spec = active.pop(agent_id, None)
            if spec is None:
                continue
            try:
                stopped = self.workspace.wait(agent_id, timeout=1.0)
            except (agents.AgentRuntimeError, ValueError):
                stopped = False
            if stopped:
                self._release_slot(spec["gateway"])
                continue
            threading.Thread(
                target=self._release_after_stop,
                args=(agent_id, spec["gateway"]),
                name=f"zylab-workflow-reaper-{agent_id[:8]}",
                daemon=True,
            ).start()

    def _finish(self, workflow_id, state, *, result=None, error=None):
        projected_result = (
            agents.project_report(result)
            if result is not None else None)
        record = self.store.update(workflow_id, lambda item: item.update({
            "state": state,
            "stage": state,
            "result": projected_result,
            "error": error,
            "ended_at": _now(),
            "owner_id": None,
            "owner_pid": None,
            "owner_boot_id": None,
            "owner_proc_start": None,
        }))
        kind = (
            "workflow_completed" if state == "completed"
            else "workflow_cancelled" if state == "cancelled"
            else "workflow_failed")
        self._emit(kind, record, result=projected_result, error=error)
        return record

    def _recovery_node_changes(self, record):
        """Inspect child checkpoints without mutating workflow state."""
        changes = {}
        maximum_attempts = int(
            (record.get("budget") or {}).get(
                "max_node_attempts", MAX_NODE_ATTEMPTS))
        for node in (record.get("plan") or {}).get("agents") or []:
            if node.get("state") != "running":
                continue
            agent_id = str(node.get("agent_id") or "")
            child = None
            if agent_id:
                try:
                    child = self.workspace.get(agent_id)
                except agents.AgentRuntimeError:
                    child = None
            if (child is not None
                    and child.get("state") == "completed"
                    and child.get("result")):
                changes[node["key"]] = {
                    "state": "completed",
                    "report": agents.project_report(
                        child["result"])[:agents.MAX_REPORT],
                    "error": "",
                }
                continue
            if (child is not None
                    and child.get("state") == "running"
                    and agents._owner_alive(child)):
                raise WorkflowError(
                    f"child {agent_id} 仍由 live owner 运行，不能接管")
            if child is not None and child.get("state") == "running":
                try:
                    self.workspace.runtime.store.finish(
                        agent_id, "failed",
                        result="[workflow owner 退出；等待 bounded retry]",
                        error="stale workflow owner recovered")
                except agents.AgentRuntimeError:
                    pass
            attempts = int(node.get("attempts") or 0)
            if attempts < maximum_attempts:
                changes[node["key"]] = {
                    "state": "queued",
                    "agent_id": "",
                    "report": "",
                    "error": "recovered after interrupted owner",
                }
            else:
                changes[node["key"]] = {
                    "state": "failed",
                    "error": (
                        "interrupted owner; node retry limit exhausted "
                        f"({attempts}/{maximum_attempts})"),
                }
        return changes

    def recover(self, parent_session_id, *, hook_config=None):
        """Claim stale/paused durable DAGs and resume only unfinished stages."""
        recovered = []
        rows = self.store.list(
            parent_session_id=str(parent_session_id or ""), limit=100)
        for snapshot in rows:
            if snapshot.get("state") not in ACTIVE_STATES:
                continue
            workflow_id = snapshot["id"]
            with self._lock:
                live = self._drivers.get(workflow_id)
                if live is not None and live.thread.is_alive():
                    continue
            owner_pid = snapshot.get("owner_pid")
            owner_id = snapshot.get("owner_id")
            if owner_pid and _owner_alive(snapshot):
                if owner_id != self.owner_id:
                    continue
            if not isinstance(snapshot.get("plan"), dict):
                self._finish(
                    workflow_id, "failed",
                    error=(
                        "legacy active workflow 没有 durable DAG checkpoint；"
                        "为避免重复外部请求，未自动重放"))
                continue
            try:
                node_changes = self._recovery_node_changes(snapshot)

                def claim(item):
                    if item.get("state") not in ACTIVE_STATES:
                        raise WorkflowError(
                            f"workflow {workflow_id} 已不是 active")
                    current_pid = item.get("owner_pid")
                    current_owner = item.get("owner_id")
                    if (current_pid and _owner_alive(item)
                            and current_owner != self.owner_id):
                        raise WorkflowError(
                            f"workflow {workflow_id} 已被其他 live owner 接管")
                    for node in (item.get("plan") or {}).get("agents") or []:
                        if node.get("key") in node_changes:
                            node.update(_copy(node_changes[node["key"]]))
                    item.update({
                        "owner_id": self.owner_id,
                        **_owner_fields(),
                        "state": "queued",
                        "stage": "recovering",
                        "recovery_count": int(
                            item.get("recovery_count") or 0) + 1,
                        "error": "",
                    })
                    return item

                claimed = self.store.update(workflow_id, claim)
            except WorkflowError:
                continue
            driver = _WorkflowDriver(hook_config)
            driver.thread = threading.Thread(
                target=self._drive_plan,
                args=(workflow_id, driver),
                name=f"zylab-workflow-{workflow_id}-recovered",
                daemon=True)
            with self._lock:
                if self._closed:
                    break
                existing = self._drivers.get(workflow_id)
                if existing is not None and existing.thread.is_alive():
                    continue
                self._drivers[workflow_id] = driver
            self._emit(
                "workflow_recovered", claimed,
                recovery_count=claimed.get("recovery_count"))
            driver.thread.start()
            recovered.append(claimed)
        return recovered

    def get(self, identifier):
        return self.store.get(identifier)

    def list(self, *, parent_session_id=None, limit=100):
        rows = self.store.list(
            parent_session_id=parent_session_id, limit=limit)
        with self._lock:
            live = {
                workflow_id for workflow_id, driver in self._drivers.items()
                if driver.thread is not None and driver.thread.is_alive()
            }
        projected = []
        for row in rows:
            value = _copy(row)
            value["workspace_active"] = value["id"] in live
            value["owner_alive"] = _owner_alive(value)
            projected.append(value)
        return projected

    def current(self, parent_session_id):
        rows = self.list(parent_session_id=parent_session_id, limit=20)
        return next(
            (row for row in rows if row.get("state") in ACTIVE_STATES),
            rows[0] if rows else None)

    def mark_apply(self, identifier, status):
        """记录 synthesis 向主 agent 的交接，防止事件重复 drain 后重复排队。"""
        if status not in {"queued", "dispatched", "skipped"}:
            raise ValueError(f"无效 workflow apply 状态：{status}")
        return self.store.update(identifier, lambda item: item.update({
            "apply_status": status,
            "apply_updated_at": _now(),
        }))

    def pause_workflow(self, identifier, *, timeout=5.0):
        record = self.store.get(identifier)
        workflow_id = record["id"]
        if (record.get("state") == "queued"
                and record.get("stage") == "paused"):
            return record
        if record.get("state") not in ACTIVE_STATES:
            raise WorkflowError(
                f"workflow {workflow_id} 已是 {record.get('state')}，不能暂停")
        with self._lock:
            driver = self._drivers.get(workflow_id)
            if driver is not None and driver.thread.is_alive():
                driver.pause.set()
                self._emit("workflow_pause_requested", record)
            elif record.get("owner_pid") and _owner_alive(record):
                raise WorkflowError(
                    f"workflow {workflow_id} 由另一个 live owner 运行")
        if driver is not None and driver.thread.is_alive():
            if not self.wait(workflow_id, timeout=timeout):
                raise WorkflowError(
                    f"workflow {workflow_id} 暂停超时；未改变 durable state")

        def mark_paused(item):
            if item.get("state") not in ACTIVE_STATES:
                return item
            previous_stage = item.get("stage")
            for node in (item.get("plan") or {}).get("agents") or []:
                if node.get("state") != "running":
                    continue
                previous = list(node.get("previous_agent_ids") or [])
                if node.get("agent_id"):
                    previous.append(node["agent_id"])
                node.update({
                    "state": "queued", "agent_id": "",
                    "previous_agent_ids": previous,
                    "report": "", "error": "paused by user",
                })
            item.update({
                "state": "queued", "stage": "paused",
                "resume_stage": previous_stage,
                "owner_id": None, "owner_pid": None,
                "owner_boot_id": None, "owner_proc_start": None,
                "paused_at": _now(),
            })
            self._append_control_event(item, "paused", {
                "previous_stage": previous_stage})
            return item

        paused = self.store.update(workflow_id, mark_paused)
        self._emit("workflow_paused", paused)
        return paused

    def _launch_existing(self, record, *, hook_config=None):
        workflow_id = record["id"]
        driver = _WorkflowDriver(hook_config)
        driver.thread = threading.Thread(
            target=self._drive_plan,
            args=(workflow_id, driver),
            name=f"zylab-workflow-{workflow_id}-resumed",
            daemon=True)
        with self._lock:
            if self._closed:
                raise WorkflowError("workflow manager 已关闭")
            existing = self._drivers.get(workflow_id)
            if existing is not None and existing.thread.is_alive():
                raise WorkflowError(
                    f"workflow {workflow_id} 已有 live driver")
            self._drivers[workflow_id] = driver
        driver.thread.start()
        return record

    def resume_workflow(self, identifier, *, hook_config=None):
        snapshot = self.store.get(identifier)
        workflow_id = snapshot["id"]
        if snapshot.get("state") == "running":
            with self._lock:
                driver = self._drivers.get(workflow_id)
                if driver is not None and driver.thread.is_alive():
                    return snapshot
            if snapshot.get("owner_pid") and _owner_alive(snapshot):
                raise WorkflowError(
                    f"workflow {workflow_id} 由另一个 live owner 运行")
        if not (snapshot.get("state") == "queued"
                and snapshot.get("stage") in {"paused", "queued", "recovering"}):
            raise WorkflowError(
                f"workflow {workflow_id} 当前 {snapshot.get('state')}/"
                f"{snapshot.get('stage')}，不能 resume")

        def claim(item):
            if not (item.get("state") == "queued"
                    and item.get("stage") in {
                        "paused", "queued", "recovering"}):
                raise WorkflowError(
                    f"workflow {workflow_id} 状态已改变，不能 resume")
            item.update({
                "owner_id": self.owner_id,
                **_owner_fields(),
                "state": "queued",
                "stage": item.get("resume_stage") or "agents",
                "error": "", "ended_at": "",
                "resumed_at": _now(),
            })
            self._append_control_event(item, "resumed")
            return item

        claimed = self.store.update(workflow_id, claim)
        self._launch_existing(claimed, hook_config=hook_config)
        self._emit("workflow_resumed", claimed)
        return claimed

    def restart_node(self, identifier, node_key, *, cascade=False,
                     hook_config=None):
        """Reset one failed/cancelled node; completed nodes require cascade."""
        snapshot = self.store.get(identifier)
        workflow_id = snapshot["id"]
        with self._lock:
            driver = self._drivers.get(workflow_id)
            live = driver is not None and driver.thread.is_alive()
        if live:
            raise WorkflowError(
                "restart node 前请先 /workflow pause，避免与 scheduler 竞态")

        def mutate(item):
            plan = item.get("plan") or {}
            nodes = plan.get("agents") or []
            by_key = {node.get("key"): node for node in nodes}
            key = str(node_key or "")
            target = by_key.get(key)
            if target is None:
                raise WorkflowError(f"workflow node 不存在：{key}")
            if target.get("state") == "completed" and not cascade:
                raise WorkflowError(
                    f"node {key} 已 completed；重跑必须显式 cascade")
            descendants = set()
            frontier = {key}
            while frontier:
                parent = frontier.pop()
                for candidate in nodes:
                    if (candidate.get("key") not in descendants
                            and parent in (candidate.get("depends_on") or [])):
                        descendants.add(candidate["key"])
                        frontier.add(candidate["key"])
            started_descendants = [
                child for child in descendants
                if by_key[child].get("state") not in {"queued", "cancelled"}]
            if started_descendants and not cascade:
                raise WorkflowError(
                    "下游节点已有结果，需 cascade 重置："
                    + ", ".join(sorted(started_descendants)))
            reset = {key, *(descendants if cascade else set())}
            for selected in reset:
                node = by_key[selected]
                previous = list(node.get("previous_agent_ids") or [])
                if node.get("agent_id"):
                    previous.append(node["agent_id"])
                node.update({
                    "state": "queued", "report": "", "error": "",
                    "agent_id": "", "previous_agent_ids": previous,
                })
            item.update({
                "state": "queued", "stage": "paused",
                "resume_stage": "agents", "result": "", "error": "",
                "ended_at": "", "owner_id": None, "owner_pid": None,
                "owner_boot_id": None, "owner_proc_start": None,
                "apply_status": "queued",
            })
            self._append_control_event(item, "node_restarted", {
                "node": key, "cascade": bool(cascade),
                "reset": sorted(reset),
            })
            return item

        updated = self.store.update(workflow_id, mutate)
        self._emit(
            "workflow_node_restarted", updated,
            node=str(node_key), cascade=bool(cascade))
        return updated

    def cancel_node(self, identifier, node_key):
        record = self.store.get(identifier)
        workflow_id = record["id"]
        node = next((item for item in (record.get("plan") or {}).get(
            "agents", []) if item.get("key") == str(node_key)), None)
        if node is None:
            raise WorkflowError(f"workflow node 不存在：{node_key}")
        if node.get("state") in {"completed", "failed", "cancelled"}:
            return record
        if node.get("state") == "running" and node.get("agent_id"):
            try:
                self.workspace.cancel(node["agent_id"])
            except agents.AgentRuntimeError as exc:
                raise WorkflowError(str(exc)) from exc

        def mutate(item):
            for candidate in (item.get("plan") or {}).get("agents") or []:
                if candidate.get("key") == str(node_key):
                    candidate.update({
                        "state": "cancelled",
                        "error": "cancelled by user",
                    })
                    break
            self._append_control_event(item, "node_cancelled", {
                "node": str(node_key)})
            return item

        updated = self.store.update(workflow_id, mutate)
        self._emit(
            "workflow_node_cancelled", updated, node=str(node_key))
        return updated

    def cancel(self, identifier):
        record = self.store.get(identifier)
        workflow_id = record["id"]
        with self._lock:
            driver = self._drivers.get(workflow_id)
            if driver is not None and driver.thread.is_alive():
                driver.cancel.set()
                self._emit("workflow_cancel_requested", record)
                return record
        if record.get("state") in TERMINAL_STATES:
            return record
        owner_pid = record.get("owner_pid")
        if owner_pid is not None and _owner_alive(record):
            raise WorkflowError(
                f"workflow {workflow_id} 仍由 PID {owner_pid} 的 worker 运行；"
                "请在原 Zylab 进程中取消")
        return self._finish(
            workflow_id, "cancelled",
            error="原 workflow owner 已退出；已停止恢复")

    def drain(self, limit=None):
        if limit is not None:
            limit = max(0, int(limit))
        events = []
        while limit is None or len(events) < limit:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def wait(self, identifier=None, timeout=None):
        deadline = (
            None if timeout is None
            else time.monotonic() + max(0.0, float(timeout)))
        while True:
            with self._lock:
                if identifier is None:
                    threads = [
                        driver.thread for driver in self._drivers.values()
                        if driver.thread and driver.thread.is_alive()]
                else:
                    record = self.store.get(identifier)
                    driver = self._drivers.get(record["id"])
                    threads = (
                        [driver.thread]
                        if driver and driver.thread and driver.thread.is_alive()
                        else [])
            if not threads:
                return True
            for thread in threads:
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        return False
                thread.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                return False

    def close(self, timeout=2.0, *, preserve=True):
        with self._lock:
            self._closed = True
            drivers = list(self._drivers.values())
            for driver in drivers:
                if preserve:
                    driver.pause.set()
                else:
                    driver.cancel.set()
        stopped = self.wait(timeout=timeout)
        if preserve and stopped:
            for snapshot in self.store.list(limit=100):
                if (snapshot.get("state") not in ACTIVE_STATES
                        or snapshot.get("owner_id") != self.owner_id):
                    continue
                workflow_id = snapshot["id"]

                def release(item):
                    if (item.get("state") in ACTIVE_STATES
                            and item.get("owner_id") == self.owner_id):
                        item.update({
                            "state": "queued",
                            "stage": "paused",
                            "owner_id": None,
                            "owner_pid": None,
                            "owner_boot_id": None,
                            "owner_proc_start": None,
                            "paused_at": _now(),
                        })
                    return item

                self.store.update(workflow_id, release)
        return stopped
