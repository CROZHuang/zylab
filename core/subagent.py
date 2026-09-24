"""子代理：独立上下文的只读调研。

为什么需要它：大范围搜索会把主上下文撑爆。「在整个仓库里找出所有用到 X 的地方
并判断哪些需要改」这类任务，中间要读几十个文件，但主线只需要最后那份结论。
子代理拿一份**全新的上下文**去做，只把有界的报告交回来。

安全约束（编在代码里，不靠提示词）：
  - 只给只读工具，写工具连 schema 都不发给它
  - **不可嵌套**：子代理拿不到 subagent 工具，避免递归炸开
  - 报告有长度上限，防止它把主上下文顶掉
  - 主会话的对话历史不传给它 —— 独立上下文才有意义
"""
import threading
import time

from . import agents

MAX_REPORT = agents.MAX_REPORT
MAX_TURNS = agents.MAX_TURNS
SYSTEM = agents.SYSTEM
MAX_BATCH = 3
MAX_REQUESTS_PER_CHILD = 5
MAX_BATCH_REQUESTS = MAX_BATCH * MAX_REQUESTS_PER_CHILD
MAX_COMBINED_REPORT = agents.MAX_REPORT


class SubagentError(RuntimeError):
    """普通 delegation 的输入或预算不满足硬约束。"""


def _normalize_batch(tasks):
    if not isinstance(tasks, list) or not 2 <= len(tasks) <= MAX_BATCH:
        raise SubagentError(f"tasks 必须包含 2..{MAX_BATCH} 个独立任务")
    normalized = []
    for index, raw in enumerate(tasks, 1):
        if not isinstance(raw, dict):
            raise SubagentError(f"tasks[{index}] 必须是 object")
        task = str(raw.get("task") or "").strip()
        if not task or len(task) > 4000:
            raise SubagentError(
                f"tasks[{index}].task 必须是 1..4000 字符")
        context = str(raw.get("context") or "")
        if len(context) > 12_000:
            raise SubagentError(
                f"tasks[{index}].context 最多 12000 字符")
        name = " ".join(str(raw.get("name") or task).split())[:80]
        normalized.append({
            "task": task, "context": context or None,
            "name": name or f"Explore {index}",
            # 用户在决策门里给这个子代理选的模型（内部键，工具参数里没有）
            "model": str(raw.get("_model") or "") or None,
            "gateway": str(raw.get("_gateway") or "") or None,
        })
    return normalized


def _notify(callback, event):
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        pass


def run_batch(tasks, *, model=None, gateway=None, execution_context=None,
              cancel=None, on_event=None, on_lifecycle=None,
              parent_tool_call_id=None, context_capsule=None,
              workspace=None, extra_tools=()):
    """并行运行 2–3 个普通只读 child，等待后返回一个有界报告。"""
    specs = _normalize_batch(tasks)
    owned_workspace = workspace is None
    workspace = workspace or agents.AgentWorkspace()
    lock = threading.Lock()
    attempts = {index: 0 for index in range(len(specs))}
    total_attempts = 0
    records = []

    def reserve(index, details=None):
        del details
        nonlocal total_attempts
        with lock:
            used = attempts[index]
            if used >= MAX_REQUESTS_PER_CHILD:
                raise SubagentError(
                    "subagent provider-attempt limit exhausted: "
                    f"child {index + 1} {used}/{MAX_REQUESTS_PER_CHILD}")
            if total_attempts >= MAX_BATCH_REQUESTS:
                raise SubagentError(
                    "subagent batch provider-attempt limit exhausted: "
                    f"{total_attempts}/{MAX_BATCH_REQUESTS}")
            attempts[index] = used + 1
            total_attempts += 1

    def relay():
        for event in workspace.drain():
            if event.get("kind") == "agent_activity":
                payload = dict(event.get("payload") or {})
                _notify(on_event, {
                    "t": payload.get("event") or "tool",
                    "name": payload.get("tool") or "tool",
                    "agent_id": payload.get("id"),
                    "payload": payload,
                })
            else:
                _notify(on_lifecycle, event)

    try:
        try:
            for index, spec in enumerate(specs):
                record = workspace.spawn(
                    spec["task"], spec["context"],
                    execution_context=execution_context,
                    model=spec.get("model") or model,
                    gateway=spec.get("gateway") or gateway, name=spec["name"],
                    parent_tool_call_id=parent_tool_call_id,
                    kind="subagent", context_capsule=context_capsule,
                    extra_tools=extra_tools,
                    before_provider_attempt=(
                        lambda details, index=index:
                        reserve(index, details)))
                records.append(record)
        except BaseException:
            for record in records:
                try:
                    workspace.cancel(record["id"])
                except (agents.AgentRuntimeError, ValueError):
                    pass
            raise

        cancel_started = None
        while any(workspace.active(record["id"]) for record in records):
            relay()
            if cancel is not None and cancel.is_set():
                if cancel_started is None:
                    cancel_started = time.monotonic()
                    for record in records:
                        try:
                            workspace.cancel(record["id"])
                        except (agents.AgentRuntimeError, ValueError):
                            pass
                elif time.monotonic() - cancel_started >= 2.0:
                    break
            time.sleep(0.02)
        relay()
        final = [workspace.get(record["id"]) for record in records]
    finally:
        if owned_workspace:
            workspace.close(timeout=1.0)

    completed = sum(row.get("state") == "completed" for row in final)
    header = (
        f"[subagents {len(final)} · completed {completed}/{len(final)} · "
        f"provider attempts {total_attempts}/{MAX_BATCH_REQUESTS}]")
    allowance = max(
        500, (MAX_COMBINED_REPORT - len(header) - 64) // len(final))
    blocks = [header]
    for spec, row in zip(specs, final):
        report = agents.project_report(row.get("result"))
        if not report:
            report = f"[{row.get('state')}: {row.get('error') or 'no report'}]"
        blocks.append(
            f"## {spec['name']} [{row['id']} · {row.get('state')}]\n"
            + report[:allowance])
    combined = "\n\n".join(blocks)
    if len(combined) > MAX_COMBINED_REPORT:
        marker = "\n…[subagent batch report truncated]"
        combined = combined[:MAX_COMBINED_REPORT - len(marker)] + marker
    return combined




def run(task, context=None, model=None, gateway=None, on_event=None,
        execution_context=None, cancel=None, on_lifecycle=None,
        parent_tool_call_id=None, runtime=None, context_capsule=None,
        extra_tools=()):
    """创建并运行一个可恢复的只读 child agent，返回带稳定 id 的报告。

    on_event: 可选回调，用于把子代理的工具调用透出到 UI（否则用户会觉得卡住）。
    on_lifecycle: agent_spawned/state/result 事件，由 TaskManager 带回主线程提交。
    """
    runtime = runtime or agents.AgentRuntime()
    return runtime.run_new(
        task, context, model=model, gateway=gateway,
        execution_context=execution_context, cancel=cancel,
        on_event=on_event, on_lifecycle=on_lifecycle,
        parent_tool_call_id=parent_tool_call_id,
        context_capsule=context_capsule, extra_tools=extra_tools)
