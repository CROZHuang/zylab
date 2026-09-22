#!/usr/bin/env python3
"""zylab —— 基于 kimi-k3（DeepInfer 网关）的终端编码助理。

    zylab                      进入交互式会话
    zylab -p "把 x.py 并行化"    单次执行后退出（可管道，适合脚本）
    zylab --model kimi-k3      指定模型
    zylab --models             列出网关上可用的模型
    zylab --resume             接上一次会话

交互中的斜杠命令：/help /status /trace /usage /context /memory /skills /commands
                    /clear /compact /rewind /plan /goal /auto /permissions /cost
                    /model /probe /graft /architecture /agents /workflow /consult
                    /tasks /task /expand /kill /tools /exit

旧版 `/map` 不再出现在命令面板或主帮助中，但显式输入 `/map ...` 仍保留离线兼容路径。
"""
import sys

# ---- 入口前序，顺序不能动（BACKLOG-rename-zylab §2.0.5）----
# 1. 版本闸门 → 2. sys.path 自注入 → 3.（批次 2：状态目录迁移）→ 4. from core import
# 闸门只用 3.6 语法：老解释器要能跑到这里说人话，而不是在 core/tui.py 的 `X | None`
# 注解上抛一个看不懂的 TypeError。
def _python_gate(version_info=None, minimum=(3, 10)):
    version_info = tuple((version_info or sys.version_info)[:2])
    if version_info < minimum:
        sys.stderr.write(
            "zylab 需要 Python %d.%d+，当前是 %d.%d（%s）。\n"
            "  换一个解释器运行：python3.12 zylab.py …（先 `which -a python3` 看有哪些）\n"
            % (minimum[0], minimum[1], version_info[0], version_info[1],
               sys.executable))
        raise SystemExit(2)


_python_gate()

import argparse
import atexit
import dataclasses
import urllib.error
import json
import os
import subprocess
import re
import shlex
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import paths  # noqa: E402  —— 必须在 sys.path 自注入之后
if __name__ == "__main__":
    # 自包含目录被整体拷到别处：把目录里记的旧绝对路径改写到新位置（.location 记上次位置）。
    # 必须在下面那条整块 `from core import (...)` 之前——store 在 import 时就把路径常量绑死；
    # 只在作为主程序运行时做：测试进程 `import zylab` 不许碰真实状态目录。
    from core import homemigrate as _homemigrate  # noqa: E402
    try:
        _homemigrate.reconcile_location(_homemigrate.current_home())
    except OSError as _exc:                       # noqa: BLE001
        sys.stderr.write(f"  ! 状态目录位置核对失败：{_exc}\n")
from core import (agent as A, agent_events as AGENT_EVENTS, agents as AGENTS,
                attachments as ATTACHMENTS, wincompat,
                away_recap as AWAY_RECAP,
                thinking as THINKING,
                checkpoints as CHECKPOINTS,
                client, consults as CONSULTS, context as CONTEXT,
                controller as C, commands as CUSTOM_COMMANDS,
                graft as GRAFT,
                graft_component as GRAFT_COMPONENT,
                memory as MEMORY, memory_extract as MEMORY_EXTRACT,
                model_health as HEALTH, models as M,
                plans as PLANS, goals as GOALS, recipes as RECIPES, skills as SKILLS,
                settings as CFG, store, version as VERSION,
                tasks as TASKS, workflows as WORKFLOWS,
                tools, tui, repomap as REPOMAP)    # noqa: E402

HOME = store.HOME
SESSIONS = store.SESSIONS

# ---------------------------------------------------------------- 颜色
_TTY = sys.stdout.isatty()
# 两次「目录该不该刷新」判断之间的最短间隔。判断本身要读 models.json，
# 所以不能每个 turn 都做；真正的刷新还要再过 catalog_ttl_hours 那一关。
CATALOG_CHECK_SECONDS = 300


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if _TTY else s


DIM = lambda s: c("2", s)
BOLD = lambda s: c("1", s)
ORANGE = lambda s: c("38;5;209", s)
GREEN = lambda s: c("32", s)
RED = lambda s: c("31", s)
BLUE = lambda s: c("38;5;110", s)
YELLOW = lambda s: c("33", s)


def _table_layout(columns, *, indent=2, maximum=118, gap="  "):
    """按当前 viewport 固定一次列布局；header 与 row 必须复用同一实例。"""
    available = max(20, min(int(maximum), tui._cols() - max(0, int(indent))))
    return tui.TableLayout.fit(columns, available, gap=gap)


class Spinner:
    """同步命令的静态 busy 提示；动画由交互 Renderer 在主线程驱动。"""

    def __init__(self, label="思考中"):
        self.label = label
        self.active = False

    def __enter__(self):
        self.active = True
        if _TTY:
            sys.stdout.write(f"\r{ORANGE('⠋')} {DIM(self.label)}  ")
            sys.stdout.flush()
        return self

    def __exit__(self, *args):
        if self.active and _TTY:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        self.active = False


def _context_progress_line(event, width=18):
    """把 models.probe_context 的结构化事件渲染成单行确定性进度条。"""
    state = str(event.get("state") or "request")
    completed = max(0, int(event.get("completed") or 0))
    total = max(1, int(event.get("total") or 1))
    final = state in {"done", "blocked"}
    fraction = 1.0 if final else min(1.0, completed / total)
    filled = min(width, int(fraction * width))
    if not final and state in {"start", "request"} and filled < width:
        bar = "█" * filled + "▌" + "░" * (width - filled - 1)
    else:
        bar = "█" * filled + "░" * (width - filled)

    target = event.get("target")
    target_text = f"{int(target):,}" if target else "?"
    result = event.get("result")
    outcome = event.get("outcome")
    if state == "start":
        detail = f"准备 {target_text} tokens"
    elif state == "request":
        detail = f"正在探测 {target_text} tokens"
    elif state == "result" and result == "ok":
        observed = event.get("observed")
        suffix = f"（usage {int(observed):,}）" if observed else "（估算档位）"
        detail = f"{target_text} 可用{suffix}"
    elif state == "result" and result == "rejected":
        detail = f"{target_text} 明确拒绝"
    elif state == "blocked":
        detail = "探测受阻：" + str(event.get("error") or "无法判定")
    elif outcome == "bounded":
        detail = "完成：已找到明确拒绝边界"
    elif outcome == "ceiling":
        detail = "完成可测档位：模型顶仍未知"
    else:
        detail = str(outcome or state)

    step = int(event.get("step") or completed)
    shown_step = min(total, max(completed, step if state == "request" else completed))
    requests = int(event.get("requests") or 0)
    request_text = f" · {requests} req" if requests else ""
    return f"[{bar}] {shown_step}/{total} 档{request_text} · {detail}"


class ContextProbeProgress:
    """同步 context probe 的进度适配器；所有终端写入仍发生在主线程。"""

    def __init__(self, sess=None):
        self.renderer = getattr(sess, "renderer", None) if sess is not None else None
        self.started = time.monotonic()
        self.direct_tty = bool(_TTY and self.renderer is None)
        self.visible = False

    def __enter__(self):
        return self

    def __call__(self, event):
        line = _context_progress_line(event)
        elapsed = time.monotonic() - self.started
        if self.renderer is not None:
            self.renderer.spinner(line, elapsed)
            self.visible = True
        elif self.direct_tty:
            sys.stdout.write(f"\r\x1b[2K{line} · {elapsed:.0f}s")
            sys.stdout.flush()
            self.visible = True
        elif event.get("state") in {"result", "done", "blocked"}:
            print(DIM("  " + line))

    def __exit__(self, *_args):
        if self.renderer is not None:
            self.renderer.clear_spinner()
        elif self.direct_tty and self.visible:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        self.visible = False


def _architecture_progress_line(event):
    stage = str(event.get("stage") or "preflight")
    completed = max(0, int(event.get("completed") or 0))
    total = max(0, int(event.get("total") or 0))
    if stage == "preflight":
        misses = int(event.get("cache_misses") or 0)
        hits = int(event.get("cache_hits") or 0)
        requests = int(event.get("estimated_requests") or 0)
        return (
            f"architecture 预检 · cache {hits} hit / {misses} miss"
            f" · 预计上限 {requests} requests")
    if total:
        width = 18
        filled = min(width, int(width * completed / total))
        bar = "━" * filled + "─" * (width - filled)
        count = f"{completed}/{total}"
    else:
        bar = "─" * 18
        count = str(completed)
    if stage == "summary":
        detail = f"摘要 {str(event.get('file') or '')[:64]}"
    elif stage == "synthesis":
        detail = f"合成 batch {int(event.get('batch') or completed)}"
    elif stage == "consolidation":
        detail = "合并跨 batch 架构关系"
    elif stage == "done":
        detail = f"已提交 generation {event.get('generation', '')}"
    else:
        detail = stage
    return f"[{bar}] {count} · {detail}"


class ArchitectureBuildProgress:
    """One-line progress for explicit provider-backed architecture builds."""

    def __init__(self, sess=None):
        self.renderer = getattr(sess, "renderer", None) if sess is not None else None
        self.started = time.monotonic()
        self.direct_tty = bool(_TTY and self.renderer is None)
        self.visible = False
        self._last_non_tty = None

    def __enter__(self):
        return self

    def __call__(self, event):
        line = _architecture_progress_line(event)
        elapsed = time.monotonic() - self.started
        if self.renderer is not None:
            self.renderer.spinner(line, elapsed)
            self.visible = True
            return
        if self.direct_tty:
            sys.stdout.write(f"\r\x1b[2K{line} · {elapsed:.0f}s")
            sys.stdout.flush()
            self.visible = True
            return
        state = event.get("state")
        completed = int(event.get("completed") or 0)
        total = int(event.get("total") or 0)
        milestone = (
            event.get("stage") in {"preflight", "done"}
            or (state == "result" and total
                and (completed == total
                     or completed % max(1, total // 4) == 0)))
        key = (event.get("stage"), state, completed, total)
        if milestone and key != self._last_non_tty:
            print(DIM("  " + line))
            self._last_non_tty = key

    def __exit__(self, *_args):
        if self.renderer is not None:
            self.renderer.clear_spinner()
        elif self.direct_tty and self.visible:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        self.visible = False


class ArchitectureCancelWatcher:
    """Use the existing InputPump in managed REPLs; never race for stdin."""

    def __init__(self, sess=None):
        self.sess = sess
        self.pump = getattr(sess, "pump", None) if sess is not None else None
        self.event = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._fallback = None
        self._deferred = []

    def __enter__(self):
        if self.pump is None:
            self._fallback = tui.EscWatcher()
            self._fallback.__enter__()
            self.event = self._fallback.event
            return self
        self._thread = threading.Thread(
            target=self._loop, name="architecture-input-watch",
            daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            try:
                event = self.pump.get(0.1)
            except Exception:                           # noqa: BLE001
                return
            if event is None:
                continue
            if event.kind in {"cancel", "interrupt"}:
                self.event.set()
                continue
            # Redraw snapshots are superseded by refresh_composer after the
            # command. Semantic events (especially queued line input) must
            # survive exactly once.
            if event.kind != "redraw":
                self._deferred.append(event)

    def __exit__(self, *_args):
        if self._fallback is not None:
            self._fallback.__exit__(*_args)
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.3)
        target = getattr(self.sess, "_deferred_pump_events", None)
        if isinstance(target, list):
            target.extend(self._deferred)


def _skill_mark(path):
    """read_file 命中 SKILL.md 时给一个可见标记。

    zylab 的 skill 不是工具，是「索引 + read_file」（见 core/agent.py 的
    <skills-index>：索引只给 name|scope|description|path，正文靠模型自己去
    读）。省 token，但代价是模型"调用 skill"在终端上本来只是一次普通文件
    读取，用户完全看不出来 —— AGENTS.md §2：不可发现的功能等价于不存在的
    功能。这里只标注实际发生的动作，不替模型宣告意图。
    """
    parts = str(path).replace("\\", "/").rstrip("/").split("/")
    if len(parts) < 2 or parts[-1] != "SKILL.md" or not parts[-2]:
        return ""
    return f"⚑ skill: {parts[-2]}"


_TOOL_DISPLAY = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Edit", "bash": "Bash",
    "grep": "Grep", "glob": "Glob", "list_dir": "List", "web_fetch": "WebFetch",
    "memory_write": "Memory", "memory_forget": "Memory", "consult_session": "Consult",
    "workflow": "Workflow", "workflow_control": "Workflow", "spawn_agent": "Agent",
}


def tool_call_line(name, args):
    """Claude Code 形状的工具调用行：`⏺ Read(core/tools.py)`、`⏺ Bash(ls -la)`。"""
    label = _TOOL_DISPLAY.get(str(name), str(name))
    detail = preview(name, args)
    return f"{BLUE('⏺')} {BOLD(label)}({DIM(detail)})" if detail else f"{BLUE('⏺')} {BOLD(label)}"


def preview(name, args):
    """把工具调用压成一行，像 Claude Code 那样。"""
    if hasattr(args, "as_dict"):
        args = args.as_dict()
    elif not isinstance(args, dict):
        args = dict(args)
    if name == "bash":
        return args.get("command", "")[:100]
    if name == "read_file":
        path = str(args.get("path", ""))
        mark = _skill_mark(path)
        return f"{mark} · {path}" if mark else path
    if name in ("list_dir", "write_file", "edit_file"):
        return str(args.get("path", ""))
    if name == "grep":
        return f"{args.get('pattern','')}" + (f"  ({args['glob']})" if args.get("glob") else "")
    if name == "glob":
        return args.get("pattern", "")
    if name == "todo_write":
        return f"{len(args.get('todos', []))} 项"
    if name == "memory_write":
        label = (
            args.get("stable_key") or args.get("title")
            or " ".join(str(args.get("content") or "").split()))
        return f"{args.get('scope') or 'project'} · {str(label)[:68]}"
    if name == "memory_forget":
        return str(args.get("identifier") or "")
    if name == "workflow":
        return (
            f"{len(args.get('agents', []))} agents · "
            f"{str(args.get('goal', ''))[:64]}")
    if name == "subagent" and args.get("tasks"):
        tasks = list(args.get("tasks") or [])
        labels = [
            str(item.get("name") or item.get("task") or "child")[:28]
            for item in tasks if isinstance(item, dict)]
        return f"{len(tasks)} children · " + ", ".join(labels)
    return json.dumps(args, ensure_ascii=False)[:90]


def _render_updated_plan(record):
    """One stable scrollback block; the footer carries the live summary."""
    record = PLANS.normalize_record(record)
    rows = [f"  {BOLD('Updated Plan')}"]
    explanation = tui.sanitize_terminal_text(
        record.get("explanation") or "")
    if explanation:
        rows.append("    " + DIM(" ".join(explanation.split())))
    marks = {
        "pending": lambda value: DIM("□ " + value),
        "in_progress": lambda value: YELLOW("◩ " + value),
        "completed": lambda value: GREEN("✓ " + value),
    }
    items = record.get("items") or []
    if not items:
        rows.append("    └ " + DIM("(空)"))
    for index, item in enumerate(items):
        content = tui.sanitize_terminal_text(item["content"])
        content = " ".join(content.split())
        branch = "└" if index == 0 else " "
        rows.append(
            f"    {branch} " + marks[item["status"]](content))
    return "\r\n".join(rows) + "\r\n"


def _approval_detail_lines(name, args):
    """Return the single shared approval detail rendering for both UIs."""
    prepared_write = getattr(args, "prepared_write", None)
    if prepared_write is not None:
        return prepared_write.preview_text.splitlines()
    if name == "bash":
        lines = [str(args.get("command", ""))[:4000]]
        evidence = getattr(args, "sandbox_evidence", lambda: None)()
        if evidence:
            marker = evidence.get("marker") or "UNKNOWN"
            lines.append(f"sandbox: {marker}")
            if evidence.get("network_requested"):
                network = (
                    "NET OPEN (approved)"
                    if evidence.get("network_approved")
                    else "NET OPEN (separate confirmation required)")
            elif evidence.get("network_isolated"):
                network = "NET ISOLATED"
            else:
                network = "NET OPEN (configured)"
            lines.append(f"network: {network}")
            if evidence.get("reason"):
                lines.append(
                    "sandbox reason: " + str(evidence["reason"])[:240])
        return lines
    if name == "write_file":
        content = str(args.get("content", ""))
        return [
            f"写入 {len(content)} 字符 / {content.count(chr(10)) + 1} 行"]
    if name == "edit_file":
        return [
            f"- {str(args.get('old', ''))[:1000]}",
            f"+ {str(args.get('new', ''))[:1000]}",
        ]
    return []


def _print_approval_details(name, args):
    try:
        columns = max(40, os.get_terminal_size().columns - 6)
    except OSError:
        columns = 114
    for raw in _approval_detail_lines(name, args):
        line = str(raw)
        if line.startswith("+++ ") or " | +" in line:
            color = GREEN
        elif line.startswith("--- ") or " | -" in line:
            color = RED
        elif line.startswith(("[拒绝执行]", "[edit_file 预检]")):
            color = YELLOW
        else:
            color = DIM
        # 审批前必须看全：按终端宽度折行、续行缩两格。截断会藏住正要批准的内容。
        for index, part in enumerate(tui.wrap_display(line, columns - 2)):
            print(color(("    " if index == 0 else "      ") + part))


def _print_wrapped(text, *, indent="    ", first_prefix="", color=DIM, width=None):
    """按终端宽度折行打印一段说明/预览，续行对齐在前缀之后；不截断。"""
    if width is None:
        width = max(40, tui._cols() - 1)
    room = max(20, width - tui.display_width(indent) - tui.display_width(first_prefix))
    for index, part in enumerate(tui.wrap_display(str(text), room)):
        lead = indent + (
            first_prefix if index == 0 else " " * tui.display_width(first_prefix))
        print(color(lead + part))


def _result_field(task, name, default=None):
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default) if task is not None else default


def _result_failure(name, out, *, denied=False, status=None, task=None):
    text = str(out or "")
    task_status = _result_field(task, "status")
    task_status = getattr(task_status, "value", task_status)
    return bool(
        denied
        or str(status or "") in {
            "failed", "cancelled", "metrics_start_failed",
            "metrics_finalize_failed", "completed_after_cancel",
        }
        or str(task_status or "") in {"failed", "cancelled"}
        or bool(_result_field(task, "timed_out", False))
        or bool(_result_field(task, "error"))
        or _result_field(task, "returncode") not in (None, 0)
        or text.startswith((
            "[已拒绝]", "[执行失败", "[读取失败", "[不存在",
            "[用户拒绝", "[用户中断", "[超时", "[搜索超时",
            "[未执行：", "[取消请求", "[参数错误",
            "[参数不是合法 JSON", "[无权限", "[不是目录",
            "[未找到待替换内容", "[匹配到", "[glob 已取消",
            "[grep 已取消", "[checkpoint 创建失败",
            "[tool trace 创建失败"))
        or "[artifact index 写入失败:" in text
        or "[artifact 收尾失败:" in text
        or (name == "bash" and re.search(r"\[exit -?[1-9]\d*\]", text))
    )


def _result_content_lines(out):
    return [line for line in str(out or "").splitlines()
            if line.strip()]


def _result_line_count(out, task=None):
    measured = sum(max(0, int(_result_field(
        task, f"{stream}_lines", 0) or 0))
        for stream in ("stdout", "stderr"))
    return measured or len(_result_content_lines(out))


def tool_result_summary(name, out, *, secs=None, task=None):
    """One informative visual line; canonical/provider content is untouched."""
    lines = _result_content_lines(out)
    count = _result_line_count(out, task)
    if name == "bash":
        returncode = _result_field(task, "returncode")
        if returncode is None:
            matches = re.findall(r"\[exit (-?\d+)\]", str(out or ""))
            returncode = int(matches[-1]) if matches else 0
        parts = [f"exit {returncode}", f"{count:,} lines"]
        if secs is not None:
            parts.append(f"{max(0.0, float(secs)):.1f}s")
        return " · ".join(parts)
    if name in {"read_file", "write_file", "edit_file"}:
        written = re.search(r"([\d,]+)\s*行", str(out or ""))
        line_count = written.group(1) if written else f"{count:,}"
        return f"{line_count} lines"
    if name == "grep":
        matches = []
        for line in lines:
            found = re.match(r"^(.+?):\d+:", line)
            if found:
                matches.append(found.group(1))
        hits = len(matches) if matches else sum(
            not line.lstrip().startswith("[") for line in lines)
        files = len(set(matches))
        return f"{hits:,} hits · {files:,} files"
    if name in {"list_dir", "glob"}:
        entries = sum(
            not line.lstrip().startswith("[") for line in lines)
        return f"{entries:,} entries"
    first = " ".join((lines[0] if lines else "无输出").split())
    first = tui.truncate_display(first, 88)
    return first + (f" · {count:,} lines" if count > 1 else "")


def _result_reference(tool_call_id=None, task_id=None):
    return str(tool_call_id or task_id or "").strip()


def _tool_transition_prefix(streaming):
    """Keep one visual spacer row between assistant prose and a tool card."""
    return "\r\n" if streaming else ""


def show_result(name, out, denied=False, secs=None, *, status=None,
                task=None, task_id=None, tool_call_id=None):
    out = str(out or "")
    if name == "todo_write":
        clean = tui.sanitize_terminal_text(out)
        print(DIM("  " + clean.replace("\n", "\n  ")))
        return
    if _result_failure(
            name, out, denied=denied, status=status, task=task):
        clean = tui.sanitize_terminal_text(out)
        for line in clean.splitlines() or [""]:
            print(RED("  " + line))
        if secs is not None:
            print(RED(f"  ({max(0.0, float(secs)):.1f}s)"))
        return
    summary = tool_result_summary(name, out, secs=secs, task=task)
    reference = _result_reference(tool_call_id, task_id)
    if reference:
        summary += f" · Ctrl+O 展开 · /expand {reference}"
    width = max(24, tui._cols() - 4)
    print(DIM("  ⎿  " + tui.truncate_display(summary, width)))


def _memory_tool_changed(name, result):
    return (
        name in {"memory_write", "memory_forget"}
        and str(result or "").startswith(("[memory saved ",
                                          "[memory forgotten "))
    )


_SCOPED_PERMISSION_NAMES = ("memory_write_global",)
_HARD_MEMORY_PERMISSION_NAMES = frozenset({
    "memory_write_global", "memory_forget",
})


def _permission_policy_name(name, args):
    """Map one tool call to the permission key that governs its actual scope."""
    if name != "memory_write":
        return name
    try:
        scope = str(args.get("scope", "project") or "").strip().lower()
    except (AttributeError, TypeError):
        scope = "project"
    return "memory_write_global" if scope == "global" else name


def _memory_hard_policy_name(name, args):
    policy_name = _permission_policy_name(name, args)
    return (
        policy_name
        if policy_name in _HARD_MEMORY_PERMISSION_NAMES
        else None)


def _memory_policy_call(permission_name):
    if permission_name == "memory_write_global":
        return "memory_write", {"scope": "global"}
    if permission_name == "memory_forget":
        return "memory_forget", {}
    raise ValueError(f"未知 hard memory 权限：{permission_name}")


def _configured_permission(cfg, name, args):
    """Return the scope key and the strictest applicable configured value."""
    policy_name = _permission_policy_name(name, args)
    permissions = (cfg or {}).get("permissions", {})
    values = [permissions.get(name, "ask")]
    if policy_name != name:
        values.append(permissions.get(policy_name, "ask"))
    normalized = [
        value if value in {"allow", "ask", "deny"} else "ask"
        for value in values
    ]
    permission = max(
        normalized, key={"allow": 0, "ask": 1, "deny": 2}.__getitem__)
    return policy_name, permission


@dataclass(frozen=True)
class TurnOutcome:
    """Stable terminal result used by noninteractive CLI exit-code mapping."""

    status: str
    error_kind: str = ""
    message: str = ""

    @property
    def exit_code(self):
        if self.status in {"idle", "ok"}:
            return 0
        if self.status == "interrupted":
            return 130
        return 1


_PARTIAL_END_KINDS = frozenset(("max_turns", "empty_response"))


def _partial_end_details(event):
    """Return ``(kind, message)`` for a bounded/empty terminal event.

    Older agents only supplied ``reason``.  Recognising the old Chinese
    max-turn wording keeps the UI fail-visible while mixed-version workers
    drain, whereas ordinary provider ``stop`` events remain successful.
    """
    if not isinstance(event, dict):
        return None
    kind = str(event.get("kind") or event.get("error_kind") or "").strip()
    status = str(event.get("status") or "").strip().lower()
    reason = str(event.get("reason") or "").strip()
    if (kind not in _PARTIAL_END_KINDS and status != "partial"
            and "最大轮数" not in reason):
        return None
    kind = kind or ("max_turns" if "最大轮数" in reason else "partial")
    message = str(event.get("message") or reason).strip()
    if not message:
        message = "本轮未生成最终文本。"
    return kind, message


class Session:
    watcher = None          # 当前轮的 Esc 监听器，确认菜单期间要暂停它

    def __init__(self, ag, cfg=None):
        self.ag = ag
        self.cfg = cfg or CFG.DEFAULTS
        self.cfg_sources = []
        self.ag.hook_cfg = self.cfg
        self._auto_override = None
        self.auto = bool(self.cfg.get("auto_approve"))
        # 工具循环轮数上限：默认不设（CFG.max_turns_policy 把 None/0 归一成 None）
        self.max_turns = CFG.max_turns_policy(self.cfg)
        # 用户可配席位（settings workflow.seats）：应用后同步工具 schema 的席位枚举
        self._apply_workflow_seats()
        self._workflow_auto_override = None
        self.workflow_auto = bool(
            CFG.workflow_policy(self.cfg).get("auto"))
        memory_policy = CFG.memory_policy(self.cfg)
        self._memory_use_override = None
        self._memory_generate_override = None
        self.memory_use = bool(memory_policy["use"])
        self.memory_generate = bool(memory_policy["generate"])
        self.memory_store = MEMORY.MemoryStore()
        self.memory_index = {
            "text": "", "entries": [], "chars": 0,
            "available": 0, "sha256": None,
        }
        self._memory_error = None
        self.repo_map_index = ""
        self.architecture_index = ""
        self._repo_map_error = None
        # /model 与 /gateway 在 active turn 中只登记下一轮 route。Agent 的
        # provider/tool loop 会反复读取 self.model/self.gateway；中途直接修改
        # 会让同一 turn 跨模型或跨网关，破坏上下文与审计边界。
        self._pending_route = None
        self.always = set()             # 本次会话内已批准的工具
        # UNSANDBOXED Bash 的会话级放行：用户在本次会话内选过 "always" 后，
        # 后续同会话的 unsandboxed bash 不再逐条弹窗（仍走 approve_unsandboxed
        # 绑定、仍受 protected 路径硬守卫约束）。不持久化、不跨会话。
        self._unsandboxed_session_approved = False
        self.plan_mode = False          # plan mode：只读探索
        # SPEC-CC-parity B1：Shift+Tab 循环 default → accept-edits → plan。
        # accept-edits = write_file/edit_file 免确认，bash 等一切照旧问；/auto 仍是独立
        # 的"全放行"开关，不进这个循环；受保护路径硬守卫在所有模式之外。
        self.accept_edits = False
        self.task_plan = PLANS.empty()  # normal mode 的持久执行计划
        # /goal 的 durable definition 与进程内 activation 分离。恢复旧 chat
        # 时目标仍可见，但必须显式 /goal resume 才允许继续消费 provider 请求。
        self.goal = GOALS.empty()
        self._goal_activation = "disarmed"
        self._goal_proposal = None
        self._goal_round_items = {}
        self._goal_round_started = {}
        self.last_ctx = getattr(ag, "last_total", 0) or None
        self._title = None
        self.lease_owner_id = uuid.uuid4().hex
        self._leased_session_id = None
        self._pending_session_leases = set()
        self._last_lease_heartbeat = 0.0
        self.last_turn_outcome = TurnOutcome("idle")
        self._session_cwd = os.path.abspath(os.getcwd())
        self._ui_owner_thread_id = threading.get_ident()
        # Owner-thread decision requests currently visible or being resolved.
        # The map is process-local; durable lifecycle facts live in the
        # controller event journal and are reconciled on resume.
        self._pending_interactions = {}
        self._recovered_interactions = ()
        self._transport_policy_active = False
        self._allow_insecure_http = False
        self._insecure_transport_grants = set()
        self._insecure_transport_lock = threading.Lock()
        self._transport_authorizer = None
        self._queue_display = {}
        self._skip_queue_display = set()
        self._custom_catalog = CUSTOM_COMMANDS.Catalog({}, [], [], {})
        self._custom_command_error = None
        # The static menu is always available.  Once a TTY is bound, a small
        # cached projection of local IDs/recipe names is layered on top; its
        # refresh path is read-only and fail-soft.
        self._command_guidance_catalog = None
        self._command_guidance_signature = None
        self._command_guidance_at = 0.0
        self._skills_catalog = SKILLS.Catalog({}, [], [], [], {})
        self.skills_index = {
            "text": "", "entries": [], "available": 0, "warnings": [],
        }
        self._skills_error = None
        self.pump = None
        self.renderer = None
        self.controller = None
        self._deferred_pump_events = []
        self.agent_workspace = AGENTS.AgentWorkspace()
        self.workflow_manager = WORKFLOWS.WorkflowManager(
            self.agent_workspace)
        self._workflow_current = None
        self._graft_current = None
        self._consult_current = None
        self._workflow_apply_queue = []
        # 后台任务结束 → 交接 prompt 队列（像 Claude Code 的任务通知：模型自动接着干）
        self._task_handoff_queue = []
        self._task_handoff_ids = set()
        # PLAN-agent-visibility：归一事件日志（界面行与通知体同源）、各子代理最近一步、活跃数
        self._agent_event_log = []
        self._catalog_notices = []
        self._catalog_thread = None
        self._catalog_checked_at = 0.0
        # away recap：控制器在 bind_interactive 里建（非交互模式没有）。后台线程只往
        # _away_recap_ready 里放，渲染与落盘都在主循环里做。
        self.away_recap_ctl = None
        self._away_recap_lock = threading.Lock()
        self._away_recap_ready = []
        # 自动记忆抽取：一轮结束、静置够久就在后台抽一次；写盘与渲染只在主线程做。
        self._memory_extract_lock = threading.Lock()
        self._memory_extract_ready = []
        self._memory_extract_thread = None
        self._memory_extract_cancel = None
        self._memory_extract_due = None
        self._memory_extract_users = 0
        self._memory_extract_summary = None
        self._turn_running = False
        self._resume_recap_pending = False
        self._agent_activity = {}
        self._active_agent_count = 0
        self._workflow_apply_ids = set()
        self.attached_agent_id = None
        self.attached_task_id = None
        self._task_background_handoff_id = None
        # background task 完成时必须回写它原先所属的 controller/turn；/resume
        # 可能在它结束前切走当前 controller，不能把结果记到新 chat。
        self._background_task_context = {}
        self._agent_cache = {}
        self._agent_cache_at = {}
        self._agent_projection_changed = False
        self._main_draft = ""
        self._agent_drafts = {}
        self._checkpoint_store = None
        try:
            self._sandbox_status = tools.sandbox_status(self.cfg)
        except Exception as exc:
            self._sandbox_status = {
                "marker": "SANDBOX BLOCKED",
                "error": f"{type(exc).__name__}: {exc}",
            }
        self.task_manager = TASKS.TaskManager(
            store.ARTIFACTS, tools.run,
            session_getter=lambda: getattr(
                self.ag, "session_id", "unknown-session"),
            legacy_artifact_getter=store.legacy_tool_artifact_rows)
        tools.HOOK_CTX["cfg"] = self.cfg
        tools.HOOK_CTX["session"] = str(
            getattr(self.ag, "session_id", ""))
        tools.HOOK_CTX["note"] = lambda m: print(YELLOW(f"  [hook] {m}"))
        tools.HOOK_CTX["plan_update"] = self._direct_plan_update
        tools.HOOK_CTX["goal_update"] = self._goal_update_from_tool
        tools.HOOK_CTX["workflow_start"] = self._start_workflow_from_tool
        tools.HOOK_CTX["workflow_control"] = self._control_workflow_from_tool
        tools.HOOK_CTX["decision_gate"] = self.decision_gate
        tools.HOOK_CTX["goal_propose"] = self._goal_proposal_from_tool
        tools.HOOK_CTX["task_status"] = self.task_status_for_tool
        tools.HOOK_CTX["context_status"] = self.context_status_for_tool
        tools.HOOK_CTX["expand_output"] = self.expand_output_for_tool
        tools.HOOK_CTX["subagent_control"] = self.subagent_control_for_tool
        tools.HOOK_CTX["context_capsule"] = self.build_context_capsule
        tools.HOOK_CTX["workspace_changed"] = self._workspace_changed
        tools.HOOK_CTX["child_extra_tools"] = self.child_extra_tools
        self.ag.checkpoint_prepared = self.checkpoint_prepared
        self.ag.compaction_state = self._compaction_state
        try:
            self.refresh_memory_context()
        except MEMORY.MemoryError:
            # Memory 是派生交接层，不是 canonical transcript。损坏必须
            # fail-visible，但不能让用户连 chat 都打不开或失去修复入口。
            setter = getattr(self.ag, "set_memory_context", None)
            if callable(setter):
                setter("")
        self.refresh_skills_context()
        try:
            self.refresh_repo_context()
        except Exception as exc:                         # noqa: BLE001
            self._repo_map_error = f"{type(exc).__name__}: {exc}"
            setter = getattr(self.ag, "set_repo_context", None)
            if callable(setter):
                setter("", "")
        self.refresh_custom_commands(publish=False)
        self.refresh_goal_context()

    def refresh_skills_context(self):
        """Reload bounded skill metadata and inject only its routing index."""
        empty = {
            "text": "", "entries": [], "available": 0, "warnings": [],
        }
        try:
            catalog = SKILLS.load(cwd=self._session_cwd)
            index = SKILLS.render_index(catalog)
            self._skills_catalog = catalog
            self.skills_index = index
            self._skills_error = None
            self._publish_skill_names(index)
            setter = getattr(self.ag, "set_skills_context", None)
            if callable(setter):
                setter(
                    index["text"]
                    if bool(getattr(self.ag, "load_skills", True)) else "")
            return index
        except Exception as exc:                         # noqa: BLE001
            # A broken optional index must not make the canonical chat unusable.
            self._skills_error = f"{type(exc).__name__}: {exc}"
            self._skills_catalog = SKILLS.Catalog({}, [], [], [], {})
            self.skills_index = empty
            self._publish_skill_names(empty)
            setter = getattr(self.ag, "set_skills_context", None)
            if callable(setter):
                try:
                    setter("")
                except Exception:                       # noqa: BLE001
                    pass
            return empty

    def _publish_skill_names(self, index):
        """把 skill 名喂给编辑器，供 `$name` 点名补全。

        点名是 prompt 层的约定（模型认 `$name`），补全只保证用户能把名字
        打对 —— 打错名字等于没点名，模型只会看到一个陌生字符串。
        """
        # refresh_skills_context 会在 Session 完全构造好之前被调用，
        # 那时还没有 pump —— 用 getattr 而不是属性访问。
        pump = getattr(self, "pump", None)
        if pump is None:
            return
        try:
            names = {entry.name: entry.description
                     for entry in index.get("entries") or []}
            pump.set_skills(names, publish=False)
        except Exception:                                # noqa: BLE001
            pass

    def refresh_custom_commands(self, *, publish=True):
        """Reload prompt-only commands for the active cwd."""
        try:
            catalog = CUSTOM_COMMANDS.load(
                cwd=self._session_cwd,
                builtins=set(REGISTRY) | set(COMMAND_ALIASES))
            self._custom_catalog = catalog
            self._custom_command_error = None
        except Exception as exc:  # discovery cannot break a chat
            self._custom_command_error = f"{type(exc).__name__}: {exc}"
            return self._custom_catalog
        if self.pump is not None:
            completion = dict(COMMANDS)
            completion.update(catalog.completion())
            self.pump.set_cwd(self._session_cwd, publish=False)
            self.pump.set_commands(completion, publish=False)
            # Keep built-in second-level guidance in sync when a custom
            # command catalog is refreshed.  Custom prompt commands remain
            # free-form unless they explicitly gain metadata in a future
            # catalog version.
            set_subcommands = getattr(self.pump, "set_subcommands", None)
            if callable(set_subcommands):
                self.refresh_command_guidance(
                    publish=publish, force=True)
            elif publish:
                # Compatibility with lightweight test/fallback pumps that
                # predate the second-level catalog API.
                self.pump.set_commands(completion, publish=True)
        return catalog

    def refresh_command_guidance(self, *, publish=False, force=False):
        """Refresh the bounded local parameter catalog for the composer.

        This runs on the REPL owner thread.  Store/manager readers are
        individually bounded and catch their own errors; if a reader fails,
        the previous catalog remains in place and the static menu still works.
        ``reset_menu=False`` is important while a user is typing: an async
        workflow state update must not jump the selection back to row zero.
        """
        pump = getattr(self, "pump", None)
        if pump is None:
            return (getattr(self, "_command_guidance_catalog", None)
                    or COMMAND_SUBCOMMANDS)
        now = time.monotonic()
        if (not force
                and now - float(getattr(
                    self, "_command_guidance_at", 0.0))
                < _GUIDANCE_REFRESH_INTERVAL):
            return (getattr(self, "_command_guidance_catalog", None)
                    or COMMAND_SUBCOMMANDS)
        previous = getattr(self, "_command_guidance_catalog", None)
        try:
            catalog = _session_command_guidance(self)
        except Exception:  # noqa: BLE001 - guidance must never break the chat
            catalog = previous or _clone_command_guidance()
        signature = _guidance_signature(catalog)
        self._command_guidance_at = now
        if previous is None or catalog != previous:
            setter = getattr(pump, "set_subcommands", None)
            applied = not callable(setter)
            if callable(setter):
                try:
                    setter(catalog, publish=publish, reset_menu=False)
                    applied = True
                except TypeError:
                    # Lightweight embedding/test pumps may only implement the
                    # original ``publish`` keyword.
                    try:
                        setter(catalog, publish=publish)
                        applied = True
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    # Prompt guidance is optional UI state.  A broken custom
                    # pump must not make the canonical chat unavailable.
                    pass
            # Do not advance the cached projection when the pump rejected the
            # update: retaining the previous catalog lets a later lifecycle
            # refresh retry instead of silently leaving the UI stale forever.
            if applied:
                self._command_guidance_catalog = catalog
                self._command_guidance_signature = signature
        elif self._command_guidance_signature is None:
            self._command_guidance_signature = signature
        return self._command_guidance_catalog or catalog

    def resolve_custom_command(self, line):
        return self._custom_catalog.resolve(line)

    def remember_queue_display(self, item, text, *, skip=False):
        if item is None:
            return
        self._queue_display[item.id] = str(text)
        if skip:
            self._skip_queue_display.add(item.id)

    def take_queue_display(self, item, *, consume=True):
        value = self._queue_display.get(item.id, item.text)
        if consume:
            self._queue_display.pop(item.id, None)
        return value

    def refresh_memory_context(self):
        """Rebuild the bounded local index and re-inject the system prefix."""
        policy = CFG.memory_policy(self.cfg)
        try:
            index = (
                self.memory_store.render_index(
                    cwd=self._session_cwd,
                    max_chars=policy["max_index_chars"],
                    max_entries=policy["max_entries"])
                if self.memory_use else {
                    "text": "", "entries": [], "chars": 0,
                    "available": 0,
                    "sha256": MEMORY.EMPTY_SHA256,
                })
            self.memory_index = index
            self._memory_error = None
            setter = getattr(self.ag, "set_memory_context", None)
            if callable(setter):
                setter(index["text"] if self.memory_use else "")
            return index
        except MEMORY.MemoryError as exc:
            self._memory_error = str(exc)
            raise

    def refresh_repo_context(self, *, force=False):
        """Refresh deterministic map independently from memory/architecture."""
        policy = CFG.map_policy(self.cfg)
        map_text = ""
        architecture_text = ""
        errors = []
        if policy["use"]:
            drift = None
            try:
                drift = REPOMAP.check_map(
                    self._session_cwd, hash_check=False,
                    max_files=policy["max_files"])
            except REPOMAP.RepoMapError:
                drift = None
            if force or drift is None or not drift.get("fresh"):
                try:
                    REPOMAP.build_map(
                        self._session_cwd,
                        max_files=policy["max_files"],
                        max_dirs=policy["max_dirs"],
                        hubs_per_dir=policy["hubs_per_dir"],
                        hotspots=policy["hotspots"])
                except REPOMAP.RepoMapEmpty:
                    # Empty research/workspace directories are normal,
                    # especially across /resume and fork. They project no map
                    # rather than blocking session restoration.
                    map_text = ""
                else:
                    map_text = REPOMAP.map_index_text(
                        self._session_cwd,
                        max_chars=policy["max_index_chars"])
            else:
                map_text = REPOMAP.map_index_text(
                    self._session_cwd,
                    max_chars=policy["max_index_chars"])
        if policy["use_architecture"]:
            # Phase C keeps the existing LLM graph as an explicitly separate,
            # untrusted projection. Its own stale marker cannot suppress map.
            try:
                architecture_text = REPOMAP.architecture_index_text(
                    self._session_cwd,
                    max_chars=policy["max_architecture_chars"],
                    max_files=policy["max_files"])
            except REPOMAP.RepoMapError as exc:
                errors.append(f"architecture: {exc}")
        self.repo_map_index = map_text
        self.architecture_index = architecture_text
        self._repo_map_error = "; ".join(errors) or None
        setter = getattr(self.ag, "set_repo_context", None)
        if callable(setter):
            setter(map_text, architecture_text)
        return {
            "map": map_text,
            "architecture": architecture_text,
        }

    def _workspace_changed(self, _tool, _args, *, context=None):
        """Refresh map before the next provider request after a local edit."""
        try:
            self.refresh_repo_context(force=False)
        except Exception as exc:                         # noqa: BLE001
            self._repo_map_error = f"{type(exc).__name__}: {exc}"
            raise

    def set_memory_mode(self, name, enabled):
        if name == "use":
            self._memory_use_override = bool(enabled)
            self.memory_use = bool(enabled)
            self.refresh_memory_context()
        elif name == "generate":
            self._memory_generate_override = bool(enabled)
            self.memory_generate = bool(enabled)
        else:
            raise ValueError("memory mode 必须是 use 或 generate")
        self.save()
        return bool(getattr(self, f"memory_{name}"))

    def build_context_capsule(self, goal=""):
        policy = CFG.memory_policy(self.cfg)
        if not str(goal or "").strip():
            current_goal = getattr(self, "goal", None)
            if isinstance(current_goal, dict) and current_goal.get("phase") in {
                    "active", "paused", "blocked"}:
                # Child/workflow agents receive the objective as a bounded
                # clue, while the owner remains the only authority that can
                # change phase or spend an autonomous round.
                goal = current_goal.get("objective", "")
        return MEMORY.build_capsule(
            self.ag, goal=goal, task_plan=self.task_plan,
            cwd=self._session_cwd,
            memory_index=(self.memory_index if self.memory_use else None),
            max_chars=policy["capsule_chars"])

    def update_task_plan(self, payload):
        """Apply one validated worker projection on the owning main thread."""
        if not isinstance(payload, dict):
            raise PLANS.PlanError("plan update payload 必须是 object")
        record, changed = PLANS.updated(
            self.task_plan,
            payload.get("items"),
            explanation=payload.get("explanation"),
        )
        self.task_plan = record
        return record, changed

    def _direct_plan_update(self, payload, *, execution_context=None):
        if (execution_context is not None
                and str(execution_context.session or "")
                != str(self.ag.session_id)):
            raise PLANS.PlanError("plan update 属于另一个 session")
        return self.update_task_plan(payload)[0]

    # ------------------------------------------------------------ /goal
    # A goal is intentionally separate from todo_write: the latter describes
    # steps, while this state machine describes the user-authorized condition
    # under which the harness may spend another turn.  Activation is process
    # local and never serialized, so /resume cannot silently restart a costly
    # autonomous loop.
    def _goal_context_text(self):
        record = getattr(self, "goal", None)
        if record is None or record.get("phase") == "complete":
            return ""
        view = GOALS.view(record, activation=getattr(
            self, "_goal_activation", "disarmed"))
        lines = [
            f"id={view['id']} revision={view['revision']}",
            f"objective={view['objective']}",
            f"phase={view['phase']} activation={view['activation']}",
            f"rounds={view['rounds_started']}/{view['max_rounds']}",
        ]
        if view.get("last_evidence"):
            lines.append("last_evidence=" + view["last_evidence"])
        if view.get("last_error"):
            lines.append("last_error=" + view["last_error"])
        if view.get("blocked_reason"):
            reason = view["blocked_reason"]
            lines.append(
                f"blocked_reason={reason.get('code')}: {reason.get('message')}")
        return "\n".join(lines)

    def refresh_goal_context(self):
        text = self._goal_context_text()
        setter = getattr(self.ag, "set_goal_context", None)
        if callable(setter):
            setter(text)
        return text

    def _set_goal_runtime(self, record, activation=None):
        self.goal = GOALS.copy(record)
        if activation is not None:
            activation = str(activation).lower()
            if activation not in GOALS.ACTIVATIONS:
                raise GOALS.GoalError(f"goal activation 无效：{activation}")
            self._goal_activation = activation
        controller = getattr(self, "controller", None)
        if controller is not None and hasattr(
                controller, "internal_dispatch_enabled"):
            # Activation is necessary but not sufficient to dispatch an
            # internal row.  Keep the controller gate closed except for the
            # exact validated call inside dispatch_goal_ready().
            controller.internal_dispatch_enabled = False
        self.refresh_goal_context()

    def _goal_audit(self, kind, *, action=None, evidence="", error=None):
        controller = getattr(self, "controller", None)
        if controller is None or not callable(
                getattr(controller, "record_event", None)):
            return None
        payload = {
            "goal": GOALS.view(
                self.goal, activation=getattr(
                    self, "_goal_activation", "disarmed")),
        }
        if action:
            payload["action"] = action
        if evidence:
            payload["evidence"] = str(evidence)[:GOALS.MAX_EVIDENCE]
        if error:
            payload["error"] = str(error)[:GOALS.MAX_ERROR]
        try:
            return controller.record_event(kind, payload)
        except Exception as exc:  # audit failure must be visible, not fatal
            store.log("goal_audit_failed", session=self.ag.session_id,
                      kind=kind, error=f"{type(exc).__name__}: {exc}")
            return None

    def _commit_goal(self, record, *, activation=None, audit="goal_changed",
                     action=None, evidence="", persist=True):
        previous = GOALS.copy(getattr(self, "goal", None))
        self._set_goal_runtime(record, activation=activation)
        try:
            if persist:
                self.save()
        except BaseException:
            # Persistence failure must never reopen autonomous spending.  Keep
            # the durable definition rollback, but fail closed process-locally
            # until the user explicitly resumes after fixing storage.
            self._set_goal_runtime(previous, activation="disarmed")
            raise
        self._goal_audit(
            audit, action=action, evidence=evidence)
        return GOALS.view(
            self.goal, activation=getattr(self, "_goal_activation", "disarmed"))

    def _cancel_queued_goal_inputs(self):
        controller = getattr(self, "controller", None)
        cancel_internal = getattr(controller, "cancel_internal_inputs", None)
        if not callable(cancel_internal):
            return ()
        removed = cancel_internal()
        for item in removed:
            self._queue_display.pop(getattr(item, "id", ""), None)
            self._skip_queue_display.discard(getattr(item, "id", ""))
        return removed

    def create_goal(self, objective, *, max_rounds=GOALS.DEFAULT_MAX_ROUNDS):
        if getattr(self.ag, "supports_tools", None) is False:
            raise GOALS.GoalError(
                "当前模型是仅聊天模型，无法调用 goal_update；请先 /model 切到工具模型")
        current = getattr(self, "goal", None)
        if current is not None and current.get("phase") != "complete":
            raise GOALS.GoalError(
                "当前已有 goal；先用 /goal edit、/goal clear 或 /goal pause")
        self._cancel_queued_goal_inputs()
        # Any in-flight round belongs to the old definition.  Do not let its
        # eventual outcome or usage counters leak into a newly-created goal.
        self._goal_round_items.clear()
        self._goal_round_started.clear()
        record = GOALS.new(objective, max_rounds=max_rounds)
        return self._commit_goal(
            record, activation="armed", audit="goal_created", action="create")

    def edit_goal(self, objective=None, *, max_rounds=None):
        record = GOALS.edit(
            self.goal, objective=objective, max_rounds=max_rounds)
        activation = getattr(self, "_goal_activation", "disarmed")
        # Close the dispatch gate before touching the durable queue.  A journal
        # failure may leave the old definition intact, but must not let it run.
        self._set_goal_runtime(self.goal, activation="disarmed")
        self._cancel_queued_goal_inputs()
        return self._commit_goal(
            record, activation=activation,
            audit="goal_edited", action="edit")

    def pause_goal(self):
        record = GOALS.pause(self.goal)
        self._set_goal_runtime(self.goal, activation="disarmed")
        self._cancel_queued_goal_inputs()
        return self._commit_goal(
            record, activation="disarmed", audit="goal_paused", action="pause")

    def resume_goal(self):
        record = GOALS.resume(self.goal)
        return self._commit_goal(
            record, activation="armed", audit="goal_resumed", action="resume")

    def complete_goal(self, evidence=None):
        if isinstance(evidence, str) and not evidence.strip():
            evidence = None
        record = GOALS.complete(self.goal, evidence=evidence)
        self._set_goal_runtime(self.goal, activation="disarmed")
        self._cancel_queued_goal_inputs()
        return self._commit_goal(
            record, activation="disarmed", audit="goal_completed",
            action="complete", evidence=evidence or "")

    def block_goal(self, message, *, code="user-blocked", evidence=None):
        if isinstance(evidence, str) and not evidence.strip():
            evidence = None
        record = GOALS.block(
            self.goal, code=code, message=message, evidence=evidence)
        self._set_goal_runtime(self.goal, activation="disarmed")
        self._cancel_queued_goal_inputs()
        return self._commit_goal(
            record, activation="disarmed", audit="goal_blocked", action="blocked",
            evidence=evidence or "")

    def clear_goal(self):
        self._set_goal_runtime(self.goal, activation="disarmed")
        self._cancel_queued_goal_inputs()
        self._goal_round_items.clear()
        self._goal_round_started.clear()
        previous = self.goal
        self._set_goal_runtime(None, activation="disarmed")
        try:
            self.save()
        except BaseException:
            self._set_goal_runtime(previous, activation="disarmed")
            raise
        self._goal_audit("goal_cleared", action="clear")
        return None

    def _goal_update_from_tool(self, payload, *, execution_context=None):
        if bool(getattr(self, "plan_mode", False)):
            raise GOALS.GoalError("plan mode 不允许修改 goal 状态")
        if (execution_context is not None
                and str(execution_context.session or "")
                != str(self.ag.session_id)):
            raise GOALS.GoalError("goal update 属于另一个 session")
        if not isinstance(payload, dict):
            raise GOALS.GoalError("goal update payload 必须是 object")
        current = GOALS.assert_ref(
            self.goal, payload.get("goal_id"), payload.get("revision"))
        action = str(payload.get("action") or "progress").lower()
        evidence = payload.get("evidence") or ""
        if not isinstance(evidence, str):
            raise GOALS.GoalError("goal evidence 必须是 string")
        evidence = evidence.strip()
        if not evidence:
            raise GOALS.GoalError("goal evidence 不能为空")
        if action == "progress":
            next_step = payload.get("next_step")
            if next_step is not None and not isinstance(next_step, str):
                raise GOALS.GoalError("goal next_step 必须是 string")
            record = GOALS.note(
                current, evidence=evidence,
                next_step=(next_step.strip() or None) if next_step else None)
            activation = getattr(self, "_goal_activation", "armed")
        elif action == "complete":
            record = GOALS.complete(current, evidence=evidence)
            activation = "disarmed"
        elif action == "blocked":
            reason = payload.get("blocked_reason")
            if not isinstance(reason, dict):
                raise GOALS.GoalError(
                    "blocked goal 必须提供具体 blocked_reason")
            code = reason.get("code")
            message = reason.get("message")
            if (not isinstance(code, str) or not code.strip()
                    or not isinstance(message, str) or not message.strip()):
                raise GOALS.GoalError(
                    "blocked_reason 必须包含非空 code/message")
            record = GOALS.block(
                current, code=code.strip(), message=message.strip(),
                evidence=evidence)
            activation = "disarmed"
        else:
            raise GOALS.GoalError(
                "goal action 必须是 progress、complete 或 blocked")
        result = self._commit_goal(
            record, activation=activation, audit="goal_state_changed",
            action=action, evidence=evidence)
        # This mutation came from the currently executing goal round, so carry
        # its CAS reference forward.  User edits do not pass through this path
        # and therefore still invalidate an old in-flight result.
        for metadata in getattr(self, "_goal_round_items", {}).values():
            if (isinstance(metadata, dict)
                    and str(metadata.get("goal_id") or "") == current["id"]
                    and metadata.get("revision") == current["revision"]):
                metadata["revision"] = result["revision"]
        return result

    def _goal_proposal_from_tool(self, payload, execution_context=None):
        """收下模型起草的目标 —— **只暂存，不 arm**。

        arming 意味着模型在没有新 prompt 的情况下反复自己跑，那是权限升级，
        只能由用户拍板。这里唯一做的事是把草稿校验一遍存起来，等 /goal accept。
        """
        payload = dict(payload or {})
        objective = _goal_ui_text(payload.get("objective"))
        if not objective:
            raise GOALS.GoalError("goal 提案缺少 objective")
        if len(objective) > GOALS.MAX_OBJECTIVE:
            raise GOALS.GoalError(
                f"goal 提案 objective 最多 {GOALS.MAX_OBJECTIVE} 字符")
        raw_rounds = payload.get("max_rounds")
        if raw_rounds is None:
            rounds = GOALS.DEFAULT_MAX_ROUNDS
        else:
            # 显式的 0 是错误，不是「没填」—— `x or DEFAULT` 会把它悄悄变成 20
            try:
                rounds = int(raw_rounds)
            except (TypeError, ValueError):
                raise GOALS.GoalError("goal 提案 max_rounds 无效") from None
        if not 1 <= rounds <= GOALS.MAX_ROUNDS:
            raise GOALS.GoalError(
                f"goal 提案 max_rounds 必须在 1..{GOALS.MAX_ROUNDS}")
        proposal = {
            "objective": objective,
            "max_rounds": rounds,
            "rationale": _goal_ui_text(payload.get("rationale"))[:500],
            "proposed_at": GOALS.now(),
        }
        if payload.get("accepted") is True:
            # 用户已经在弹窗里点了「采纳」—— 那就是 arming 需要的那次拍板本身，
            # 不必再让他去敲 /goal accept。
            view = self.create_goal(objective, max_rounds=rounds)
            self._goal_proposal = None
            return {"status": "armed", "armed": True,
                    "objective": objective,
                    "max_rounds": view.get("max_rounds", rounds)}
        self._goal_proposal = proposal
        return {"status": "staged", "requires": "/goal accept",
                "objective": objective, "max_rounds": rounds,
                "armed": False}

    def take_goal_proposal(self):
        proposal = getattr(self, "_goal_proposal", None)
        self._goal_proposal = None
        return dict(proposal) if isinstance(proposal, dict) else None

    def _apply_goal_event(self, payload):
        """Apply one worker-originated goal event on the owner thread."""
        return self._goal_update_from_tool(payload)

    def goal_turn_finished(self, item, outcome):
        """Close a goal round and fail closed on provider/interruption errors."""
        if item is None or getattr(item, "origin", "user") != "goal":
            return None
        metadata = self._goal_round_items.pop(item.id, None)
        started = self._goal_round_started.pop(item.id, None)
        # If the user cleared/replaced a goal while this round was in flight,
        # the old turn is no longer allowed to mutate the new definition.
        # ``metadata`` is present for every round created by this version; the
        # tuple fallback keeps hand-written/library fixtures compatible.
        if metadata is None and started is None:
            return None
        if isinstance(started, dict):
            before_in = started.get("tokens_in", 0)
            before_out = started.get("tokens_out", 0)
            started_at = started.get("started_at", time.monotonic())
        elif isinstance(started, (tuple, list)) and len(started) >= 3:
            before_in, before_out, started_at = started[:3]
        else:
            before_in = before_out = 0
            started_at = time.monotonic()
        current_goal_id = (
            str((self.goal or {}).get("id") or "")
            if isinstance(self.goal, dict) else "")
        round_goal_id = str((metadata or {}).get("goal_id") or "")
        try:
            round_revision = int((metadata or {}).get("revision"))
            current_revision = int((self.goal or {}).get("revision"))
        except (TypeError, ValueError, OverflowError):
            round_revision = current_revision = -1
        same_goal = bool(
            self.goal is not None and round_goal_id
            and round_goal_id == current_goal_id)
        round_state_matches = bool(
            same_goal and round_revision >= 1
            and round_revision == current_revision)
        if started is not None and same_goal:
            try:
                before_in = max(0, int(before_in))
            except (TypeError, ValueError, OverflowError):
                before_in = 0
            try:
                before_out = max(0, int(before_out))
            except (TypeError, ValueError, OverflowError):
                before_out = 0
            try:
                started_at = float(started_at)
            except (TypeError, ValueError, OverflowError):
                started_at = time.monotonic()
            delta_in = max(0, int(getattr(self.ag, "tokens_in", 0)) - before_in)
            delta_out = max(0, int(getattr(self.ag, "tokens_out", 0)) - before_out)
            elapsed = max(0.0, time.monotonic() - started_at)
            try:
                self._set_goal_runtime(
                    GOALS.add_usage(
                        self.goal, tokens_in=delta_in,
                        tokens_out=delta_out, elapsed_seconds=elapsed),
                    activation=getattr(self, "_goal_activation", "disarmed"))
                self.save()
            except Exception as exc:
                store.log("goal_usage_update_failed", session=self.ag.session_id,
                          error=f"{type(exc).__name__}: {exc}")
        status = str(getattr(outcome, "status", outcome or ""))
        if status != "ok":
            if not round_state_matches:
                return None
            if (self.goal is not None
                    and self.goal.get("phase") == "active"
                    and self._goal_activation == "armed"):
                error_text = str(
                    getattr(outcome, "message", "")
                    or getattr(outcome, "error", "")
                    or "goal round failed")[:GOALS.MAX_ERROR]
                try:
                    failed_record = GOALS.note(
                        self.goal, last_error=error_text)
                    self._set_goal_runtime(
                        failed_record, activation="disarmed")
                except Exception:
                    # Even if recording the error is impossible, close the
                    # automatic gate before returning control to the user.
                    self._set_goal_runtime(self.goal, activation="disarmed")
                try:
                    self.save()
                except Exception as exc:  # preserve original turn outcome
                    store.log("goal_disarm_failed", session=self.ag.session_id,
                              error=f"{type(exc).__name__}: {exc}")
                self._goal_audit(
                    "goal_paused_after_error", action="disarm",
                    error=getattr(outcome, "message", "turn failed"))
                return "goal 已暂停：上一轮未成功；运行 /goal resume 重试"
            return None
        if (round_state_matches and self.goal is not None
                and self.goal.get("phase") == "active"
                and self.goal.get("rounds_started", 0)
                >= self.goal.get("max_rounds", 0)):
            try:
                self.block_goal(
                    "已达到自动 round 上限，请 edit 提高上限或创建新 goal",
                    code="round-limit")
            except Exception as exc:
                store.log("goal_round_limit_update_failed",
                          session=self.ag.session_id,
                          error=f"{type(exc).__name__}: {exc}")
            return "goal 已暂停：达到 round 上限"
        return None

    @staticmethod
    def _goal_queue_matches(item, goal):
        """Validate a recovered continuation before allowing a new request."""
        if getattr(item, "origin", "user") != "goal" or not isinstance(goal, dict):
            return False
        metadata = getattr(item, "metadata", None)
        if not isinstance(metadata, dict):
            return False
        def exact_int(value):
            if isinstance(value, bool):
                raise ValueError
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError from None
            if isinstance(value, float) and value != parsed:
                raise ValueError
            if isinstance(value, str) and value.strip() != str(parsed):
                raise ValueError
            return parsed

        try:
            round_number = exact_int(metadata.get("round"))
            revision = exact_int(metadata.get("revision"))
            current_revision = exact_int(goal.get("revision"))
            current_rounds = exact_int(goal.get("rounds_started"))
        except (TypeError, ValueError, OverflowError):
            return False
        return (
            str(metadata.get("goal_id") or "") == str(goal.get("id") or "")
            and revision == current_revision
            and round_number == current_rounds
            and revision >= 1
            and round_number >= 1
        )

    def _reconcile_goal_queue(self, goal_items, goal):
        """Drop malformed/stale internal rows without touching user queue."""
        valid = []
        failed = False
        for item in goal_items:
            if self._goal_queue_matches(item, goal):
                valid.append(item)
                continue
            try:
                self.controller.cancel_queued_item(item.id, origin="goal")
                self._queue_display.pop(item.id, None)
                self._skip_queue_display.discard(item.id)
                self._goal_audit(
                    "goal_queue_discarded", action="discard",
                    error="stale or malformed continuation metadata")
            except Exception as exc:
                # A failed cancellation must fail closed: leave the row in the
                # durable queue and do not spend a provider request.
                failed = True
                store.log(
                    "goal_queue_reconcile_failed", session=self.ag.session_id,
                    item=getattr(item, "id", ""),
                    error=f"{type(exc).__name__}: {exc}")
        return valid, failed

    def dispatch_goal_ready(self):
        """Queue at most one autonomous round at a safe idle boundary."""
        goal = getattr(self, "goal", None)
        controller = getattr(self, "controller", None)
        if (goal is None or goal.get("phase") != "active"
                or getattr(self, "_goal_activation", "disarmed") != "armed"
                or controller is None
                # Plan mode is an explicitly read-only inspection boundary;
                # it must never be turned into an autonomous execution loop
                # merely because an older goal was armed in this chat.
                or bool(getattr(self, "plan_mode", False))):
            return None
        if controller.current_turn_id is not None:
            return None
        if getattr(self.ag, "supports_tools", None) is False:
            try:
                self.block_goal(
                    "当前模型没有工具能力，无法可靠验证并结束自动 goal",
                    code="model-no-tools")
            except Exception as exc:
                self._set_goal_runtime(goal, activation="disarmed")
                store.log(
                    "goal_model_capability_block_failed",
                    session=getattr(self.ag, "session_id", ""),
                    error=f"{type(exc).__name__}: {exc}",
                )
            return None
        # A recovered queue row must obey the same idle boundary as a newly
        # reserved round; background shells/children may still be mutating the
        # workspace and their result is not yet in the main transcript.
        try:
            if self.cwd_switch_blocker():
                return None
        except Exception as exc:
            # The blocker is the safety boundary for process-global cwd and
            # managed workers.  If it cannot establish a safe snapshot, do
            # not spend another provider request on an autonomous round.
            store.log(
                "goal_dispatch_blocker_failed",
                session=getattr(self.ag, "session_id", ""),
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        if controller.queued:
            # A stale internal item is the only queue entry allowed here; it
            # was durably reserved before a crash and must be resumed, not
            # charged a second round.
            goal_items = [item for item in controller.queued
                          if item.origin == "goal"]
            user_items = [item for item in controller.queued
                          if item.origin != "goal"]
            goal_items, reconcile_failed = self._reconcile_goal_queue(
                goal_items, goal)
            if reconcile_failed:
                return None
            if user_items:
                return None
            if goal_items:
                # Multiple matching rows can only come from a crash/retry or a
                # corrupted journal.  Consume one and cancel the duplicates;
                # never charge the same reserved round twice.
                primary = goal_items[0]
                duplicate_cancel_failed = False
                for duplicate in goal_items[1:]:
                    try:
                        controller.cancel_queued_item(
                            duplicate.id, origin="goal")
                    except Exception as exc:
                        duplicate_cancel_failed = True
                        store.log(
                            "goal_queue_duplicate_cancel_failed",
                            session=self.ag.session_id,
                            item=duplicate.id,
                            error=f"{type(exc).__name__}: {exc}")
                if duplicate_cancel_failed:
                    return None
                controller.internal_dispatch_enabled = True
                try:
                    action = controller.dispatch_ready()
                finally:
                    controller.internal_dispatch_enabled = False
                if action is not None:
                    item = action.item
                    self._goal_round_items[item.id] = dict(item.metadata)
                    self._goal_round_started[item.id] = (
                        int(getattr(self.ag, "tokens_in", 0)),
                        int(getattr(self.ag, "tokens_out", 0)),
                        time.monotonic())
                    self.remember_queue_display(
                        item,
                        f"round {item.metadata.get('round', '?')}/"
                        f"{goal.get('max_rounds', '?')} · waiting",
                        skip=True)
                return action
            # Every internal row was stale and has been durably cancelled; the
            # normal reservation path below may safely allocate the next round.
        # Do not compete with foreground/background work or an unfinished
        # heterogeneous workflow whose results are not in the transcript yet.
        current_workflow = getattr(self, "_workflow_current", None)
        if isinstance(current_workflow, dict) and str(
                current_workflow.get("state") or "") in {
                    "queued", "running", "reviewing", "synthesizing",
                }:
            return None
        if goal.get("rounds_started", 0) >= goal.get("max_rounds", 0):
            # This can happen when the user edits an in-flight final round or
            # after a crash between provider completion and owner bookkeeping.
            # Never let an idle poll turn the exhausted cost boundary into a
            # traceback/retry loop.
            try:
                self.block_goal(
                    "已达到自动 round 上限，请 edit 提高上限或创建新 goal",
                    code="round-limit")
            except Exception as exc:
                self._set_goal_runtime(goal, activation="disarmed")
                store.log(
                    "goal_round_limit_update_failed",
                    session=getattr(self.ag, "session_id", ""),
                    error=f"{type(exc).__name__}: {exc}",
                )
            return None
        try:
            reserved = GOALS.start_round(goal)
            self._set_goal_runtime(reserved, activation="armed")
            self.save()
            metadata = {
                "goal_id": reserved["id"],
                "revision": reserved["revision"],
                "round": reserved["rounds_started"],
            }
            action = controller.submit_goal(
                GOALS.prompt(reserved, reserved["rounds_started"]),
                metadata=metadata)
            item = action.item if action is not None else controller.queued[-1]
            self.remember_queue_display(
                item,
                f"round {metadata['round']}/{reserved['max_rounds']} · waiting",
                skip=True)
            if action is None:
                controller.internal_dispatch_enabled = True
                try:
                    action = controller.dispatch_ready()
                finally:
                    controller.internal_dispatch_enabled = False
            if action is not None:
                item = action.item
                if not self._goal_queue_matches(item, reserved):
                    raise GOALS.GoalError(
                        "controller dispatch 了未通过校验的 goal item")
                self._goal_round_items[item.id] = dict(item.metadata)
                self._goal_round_started[item.id] = (
                    int(getattr(self.ag, "tokens_in", 0)),
                    int(getattr(self.ag, "tokens_out", 0)), time.monotonic())
            return action
        except Exception as exc:
            # Reservation is reversible only before the queue write succeeds;
            # restore its definition and close the process-local gate.  An
            # autonomous idle poll must never crash the whole REPL or retry a
            # provider spend after its state/journal boundary failed.
            self._set_goal_runtime(goal, activation="disarmed")
            try:
                self.save()
            except Exception as rollback_exc:
                store.log("goal_reservation_rollback_failed",
                          session=self.ag.session_id,
                          error=(f"{type(rollback_exc).__name__}: "
                                 f"{rollback_exc}"))
            self._goal_audit(
                "goal_dispatch_failed", action="disarm", error=exc)
            _session_notice(
                self,
                f"[goal 自动续轮失败并已停用：{type(exc).__name__}: {exc}]",
                error=True)
            return None
        except BaseException:
            self._set_goal_runtime(goal, activation="disarmed")
            raise

    def allowed_tool_names(self):
        allowed = set(
            tools.READ_ONLY if getattr(self, "plan_mode", False)
            else tools.ALL)
        # The model must not see tools whose trusted executable is absent or
        # whose user policy is disabled.  This avoids a doomed tool round-trip.
        cfg = getattr(self, "cfg", None)
        if cfg is None or not GRAFT.available(cfg):
            # 缺席时摘掉 sidecar 提供的**全部**工具，不只是只读查询那几个
            allowed.difference_update(GRAFT.ALL_TOOL_NAMES)
        # A model may report progress only for a goal that exists in this
        # session.  Hiding the tool when there is no active definition avoids
        # a pointless provider round-trip and keeps goal creation user-only.
        goal = getattr(self, "goal", None)
        if not isinstance(goal, dict) or goal.get("phase") != "active":
            allowed.discard("goal_update")
        if getattr(self, "plan_mode", False):
            allowed.discard("goal_update")
            return frozenset(allowed)
        # Fail closed for lightweight/library Session instances predating the
        # adaptive workflow field: model-triggered external requests require
        # an explicit per-chat opt-in.
        if not getattr(self, "workflow_auto", False):
            allowed.discard("workflow")
            allowed.discard("workflow_control")
            allowed.discard("workflow_recipe")
        return frozenset(allowed)

    def set_workflow_auto(self, enabled):
        before = (
            getattr(self, "_workflow_auto_override", None),
            bool(getattr(self, "workflow_auto", False)))
        self._workflow_auto_override = bool(enabled)
        self.workflow_auto = bool(enabled)
        try:
            self.save()
        except BaseException:
            (self._workflow_auto_override,
             self.workflow_auto) = before
            raise
        return self.workflow_auto

    def prepare_cwd_configuration(self, cwd=None):
        """Load and validate the complete effective config for one cwd."""
        cfg, sources = CFG.load(cwd or os.getcwd())
        sandbox_state = tools.sandbox_status(cfg)
        health_policy = CFG.model_health_policy(cfg)
        return {
            "cfg": cfg,
            "sources": list(sources),
            "sandbox_status": sandbox_state,
            "health_policy": health_policy,
        }

    def configuration_snapshot(self):
        return {
            "cfg": self.cfg,
            "sources": list(getattr(self, "cfg_sources", ())),
            "sandbox_status": getattr(self, "_sandbox_status", None),
            "auto": bool(getattr(self, "auto", False)),
            "auto_override": getattr(self, "_auto_override", None),
            "workflow_auto": bool(
                getattr(self, "workflow_auto", False)),
            "workflow_auto_override": getattr(
                self, "_workflow_auto_override", None),
            "memory_use": bool(getattr(self, "memory_use", False)),
            "memory_generate": bool(
                getattr(self, "memory_generate", False)),
            "memory_use_override": getattr(
                self, "_memory_use_override", None),
            "memory_generate_override": getattr(
                self, "_memory_generate_override", None),
            "compact_override": getattr(
                self.ag, "compact_override", None),
            "compact_at": getattr(self.ag, "compact_at", None),
        }

    def route_selection(self):
        """Return the effective route selected for the next provider turn."""
        pending = getattr(self, "_pending_route", None)
        if pending:
            return pending["model"], pending["gateway"]
        return (
            str(getattr(self.ag, "model", "")),
            str(getattr(self.ag, "gateway", None) or client.GATEWAY),
        )

    def install_transport_policy(self, *, allow_insecure_http=False):
        """Bind the process client to this Session's confirmation UI."""
        self._allow_insecure_http = bool(allow_insecure_http)
        self._transport_policy_active = True
        authorizer = self.authorize_insecure_transport
        self._transport_authorizer = authorizer
        transport = CFG.transport_policy(self.cfg)
        client.configure_transport_policy(
            allow_insecure_http=self._allow_insecure_http,
            authorizer=authorizer,
            allowed_insecure_endpoints=transport[
                "allowed_insecure_endpoints"])

    def _provider_tool_routes(self, name, args):
        """Resolve routes whose background requests need owner-thread consent."""
        if name not in tools.PROVIDER_SPAWNING:
            return ()
        if name == "subagent":
            model, gateway = self.route_selection()
            return ((client.route_for(gateway), model),)

        try:
            payload = dict(args or {})
        except (TypeError, ValueError):
            payload = {}
        if (name == "workflow_control"
                and str(payload.get("action") or "").strip().lower()
                != "add"):
            return ()
        lineup = self.workflow_manager.resolve_lineup()
        by_seat = {
            str(row.get("seat") or ""): row for row in lineup
            if row.get("seat") and row.get("gateway") and row.get("id")
        }
        selected = []
        taken = set()

        def choose(seat=None):
            key = str(seat or "").strip()
            row = by_seat.get(key) if key else next(
                (candidate for candidate in lineup
                 if candidate.get("seat") not in taken), None)
            if row is None:
                return
            taken.add(row.get("seat"))
            selected.append((
                client.route_for(row["gateway"]), str(row["id"])))

        specs = payload.get(
            "nodes" if name == "workflow_control" else "agents")
        if isinstance(specs, list):
            for spec in specs:
                choose(spec.get("seat") if isinstance(spec, dict) else None)
        review = payload.get("review")
        if isinstance(review, dict) and review.get("enabled"):
            reviewers = review.get("reviewers")
            if isinstance(reviewers, list):
                for reviewer in reviewers:
                    if isinstance(reviewer, str):
                        choose(reviewer)
                    elif isinstance(reviewer, dict):
                        choose(reviewer.get("seat"))
        synthesis = payload.get("synthesis")
        if isinstance(synthesis, dict) and synthesis.get("seat"):
            choose(synthesis.get("seat"))

        unique = []
        seen = set()
        for route, model in selected:
            key = (route.name, client.normalized_base_url(route.base))
            if key in seen:
                continue
            seen.add(key)
            unique.append((route, model))
        return tuple(unique)

    def _authorize_provider_transport(self, name, args, result):
        """Preflight child routes on the owner thread before worker spawn."""
        result = dict(result)
        if not result.get("allowed") or name not in tools.PROVIDER_SPAWNING:
            return result
        try:
            routes = self._provider_tool_routes(name, args)
            for route, model in routes:
                client.require_secure_transport(
                    route,
                    trace_context={
                        "session_id": getattr(self.ag, "session_id", ""),
                        "transport_session_id": getattr(
                            self.ag, "session_id", ""),
                        "cwd": self._session_cwd,
                        "raw": {"purpose": f"{name}_preflight"},
                    },
                    purpose=f"{name}_preflight", model=model)
        except client.APIError as exc:
            print(RED(f"  ✗ 后台 provider 传输预检失败：{exc}"))
            return {
                "allowed": False,
                "decision": "provider_transport_denied",
                "source": "transport_guard",
            }
        except Exception as exc:
            print(RED(
                "  ✗ 无法解析后台 provider route，已拒绝启动："
                f"{type(exc).__name__}: {exc}"))
            return {
                "allowed": False,
                "decision": "provider_transport_preflight_failed",
                "source": "transport_guard",
            }
        return result

    def _transport_scope_key(self, request):
        repo = os.path.realpath(os.path.abspath(str(
            request.get("repo") or self._session_cwd)))
        session_id = str(
            request.get("session")
            or getattr(self.ag, "session_id", ""))
        return (
            str(request.get("gateway") or ""),
            client.normalized_base_url(request.get("base")),
            repo,
            session_id,
        )

    def _confirm_insecure_transport(self, request):
        if self.renderer:
            self.renderer.clear_input()
        print()
        print(RED("  明文 HTTP provider 传输需要单独确认"))
        print(YELLOW(
            "  这项确认按 gateway + endpoint + repo + session 绑定，"
            "不会被 /auto、allow 或会话工具授权绕过。"))
        endpoint = tui.sanitize_terminal_text(str(request.get("base") or "?"))
        repo = tui.sanitize_terminal_text(str(request.get("repo") or "?"))
        key_types = ", ".join(request.get("key_names") or ()) or "provider key"
        print(DIM("    endpoint: " + tui.truncate_display(endpoint, 104)))
        print(DIM("    repo:     " + tui.truncate_display(repo, 104)))
        print(DIM("    key type: " + tui.truncate_display(key_types, 104)))
        print(DIM("    data: Authorization header、prompt/messages、代码与工具结果"))
        options = [
            ("deny", "拒绝（推荐）"),
            ("session", "仅对此 repo 的当前 session 允许"),
        ]
        if self.pump is not None:
            picked = self.pick(
                options, page=2, allow_filter=False,
                render=lambda option: option[1])
            choice = picked[0] if picked else "deny"
        elif tui.supported():
            watcher = self.watcher
            if watcher:
                watcher.pause()
            try:
                picked = tui.select(
                    options, title="", page=2, allow_filter=False,
                    render=lambda option: option[1])
            finally:
                if watcher:
                    watcher.resume()
            choice = picked[0] if picked else "deny"
        else:
            try:
                value = input(DIM(
                    "  输入 allow http 才允许当前 session；直接回车拒绝 > "))
            except (EOFError, KeyboardInterrupt):
                print()
                value = ""
            choice = "session" if value.strip().lower() == "allow http" else "deny"
        return choice == "session"

    def authorize_insecure_transport(self, request):
        """Authorize one exact HTTP scope; noninteractive calls fail closed."""
        request = dict(request or {})
        request.setdefault("repo", self._session_cwd)
        request.setdefault("session", getattr(self.ag, "session_id", ""))
        key = self._transport_scope_key(request)
        with self._insecure_transport_lock:
            if key in self._insecure_transport_grants:
                return True
            if self._allow_insecure_http:
                self._insecure_transport_grants.add(key)
                return True
            if threading.get_ident() != self._ui_owner_thread_id:
                return False
            if not sys.stdin.isatty():
                return False
            if not self._confirm_insecure_transport(request):
                print(DIM("  ✗ 已拒绝明文 HTTP provider 传输"))
                return False
            self._insecure_transport_grants.add(key)
            print(RED("  ! 已允许当前 repo/session 使用该 HTTP endpoint"))
            return True

    def _apply_route(self, model, gateway):
        """Atomically expose one validated route to future provider calls."""
        route = client.route_for(gateway)
        old_gateway = client.GATEWAY
        old_agent_gateway = getattr(self.ag, "gateway", old_gateway)
        old_model = getattr(self.ag, "model", "")
        try:
            client.set_gateway(route.name)
            self.ag.gateway = route.name
            limit = self.ag.set_model(model)
            self.ag.mark_route_explicit()
        except Exception:
            client.set_gateway(old_gateway)
            self.ag.gateway = old_agent_gateway
            try:
                self.ag.set_model(old_model)
            except Exception:
                self.ag.model = old_model
            raise
        return {
            "staged": False,
            "model": str(model),
            "gateway": route.name,
            "base": route.base,
            "context": limit,
            "context_known": bool(getattr(self.ag, "ctx_known", False)),
            "supports_tools": getattr(self.ag, "supports_tools", None),
        }

    def request_route_change(self, model=None, gateway=None):
        """Apply an idle route change or stage it until the active turn ends."""
        selected_model, selected_gateway = self.route_selection()
        model = str(model if model is not None else selected_model).strip()
        route = client.route_for(gateway or selected_gateway)
        if not model:
            raise ValueError("模型名不能为空")
        if getattr(self, "_transport_policy_active", False):
            try:
                client.require_secure_transport(
                    route,
                    trace_context={
                        "session_id": getattr(self.ag, "session_id", ""),
                        "cwd": self._session_cwd,
                        "raw": {"purpose": "route_change"},
                    },
                    purpose="route_change", model=model)
            except client.APIError as exc:
                if exc.kind == "insecure_transport":
                    raise ValueError(str(exc)) from None
                raise
        controller = getattr(self, "controller", None)
        turn_id = (
            getattr(controller, "current_turn_id", None)
            if controller is not None else None)
        if turn_id is not None:
            pending = {
                "model": model,
                "gateway": route.name,
                "base": route.base,
                "source_turn_id": turn_id,
            }
            # Match Controller's durable-intent-first contract: a failed
            # journal write must not leave an invisible in-memory route change.
            controller.record_event(
                "route_change_staged",
                {"model": model, "gateway": route.name},
                turn_id=turn_id,
            )
            self._pending_route = pending
            return {"staged": True, **pending}
        return self._apply_route(model, route.name)

    def apply_pending_route(self):
        """Commit the staged route at the turn boundary, before the next action."""
        pending = getattr(self, "_pending_route", None)
        if not pending:
            return None
        result = self._apply_route(
            pending["model"], pending["gateway"])
        self._pending_route = None
        controller = getattr(self, "controller", None)
        if controller is not None:
            controller.record_event(
                "route_change_applied",
                {"model": result["model"],
                 "gateway": result["gateway"]},
                turn_id=pending.get("source_turn_id"),
            )
        return result

    def apply_cwd_configuration(self, bundle):
        """Atomically replace every cwd-scoped runtime config snapshot."""
        if not isinstance(bundle, dict) or not isinstance(
                bundle.get("cfg"), dict):
            raise ValueError("cwd configuration bundle 无效")
        old = self.configuration_snapshot()
        try:
            self.cfg = bundle["cfg"]
            self.cfg_sources = list(bundle.get("sources") or ())
            self._sandbox_status = bundle["sandbox_status"]
            self.ag.hook_cfg = self.cfg
            tools.HOOK_CTX["cfg"] = self.cfg
            store.configure_metrics_policy(bundle["health_policy"])
            if hasattr(self.ag, "metrics"):
                self.ag.metrics = store.metrics_facade()
            if hasattr(self.ag, "compact_override"):
                self.ag.compact_override = self.cfg.get("compact_at")
                if hasattr(self.ag, "ctx_limit"):
                    self.ag.compact_at = A.compact_threshold(
                        self.ag.ctx_limit, self.ag.compact_override)
            self.auto = bool(
                getattr(self, "_auto_override", None)
                if getattr(self, "_auto_override", None) is not None
                else self.cfg.get("auto_approve"))
            workflow_policy = CFG.workflow_policy(self.cfg)
            self.workflow_auto = bool(
                getattr(self, "_workflow_auto_override", None)
                if getattr(
                    self, "_workflow_auto_override", None) is not None
                else workflow_policy.get("auto"))
            memory_policy = CFG.memory_policy(self.cfg)
            self.memory_use = bool(
                getattr(self, "_memory_use_override", None)
                if getattr(self, "_memory_use_override", None) is not None
                else memory_policy.get("use"))
            self.memory_generate = bool(
                getattr(self, "_memory_generate_override", None)
                if getattr(self, "_memory_generate_override", None) is not None
                else memory_policy.get("generate"))
            refresh_memory = getattr(self, "refresh_memory_context", None)
            if callable(refresh_memory):
                refresh_memory()
            refresh_skills = getattr(self, "refresh_skills_context", None)
            if callable(refresh_skills):
                refresh_skills()
            refresh_repo = getattr(self, "refresh_repo_context", None)
            if callable(refresh_repo):
                refresh_repo()
        except BaseException:
            self.restore_configuration_snapshot(old)
            raise
        return self.cfg

    def restore_configuration_snapshot(self, snapshot):
        self.cfg = snapshot["cfg"]
        self.cfg_sources = list(snapshot.get("sources") or ())
        self._sandbox_status = snapshot.get("sandbox_status")
        self._auto_override = snapshot.get("auto_override")
        self.auto = bool(snapshot.get("auto"))
        self._workflow_auto_override = snapshot.get(
            "workflow_auto_override")
        self.workflow_auto = bool(snapshot.get("workflow_auto"))
        self._memory_use_override = snapshot.get("memory_use_override")
        self._memory_generate_override = snapshot.get(
            "memory_generate_override")
        self.memory_use = bool(snapshot.get("memory_use"))
        self.memory_generate = bool(snapshot.get("memory_generate"))
        self.ag.hook_cfg = self.cfg
        tools.HOOK_CTX["cfg"] = self.cfg
        store.configure_metrics_policy(CFG.model_health_policy(self.cfg))
        if hasattr(self.ag, "metrics"):
            self.ag.metrics = store.metrics_facade()
        if hasattr(self.ag, "compact_override"):
            self.ag.compact_override = snapshot.get("compact_override")
        if (snapshot.get("compact_at") is not None
                and hasattr(self.ag, "compact_at")):
            self.ag.compact_at = snapshot["compact_at"]
        refresh_memory = getattr(self, "refresh_memory_context", None)
        if callable(refresh_memory):
            refresh_memory()
        refresh_skills = getattr(self, "refresh_skills_context", None)
        if callable(refresh_skills):
            refresh_skills()

    def checkpoint_authority(self):
        if self._checkpoint_store is None:
            self._checkpoint_store = CHECKPOINTS.CheckpointStore(store.HOME)
        return self._checkpoint_store

    def checkpoint_prepared(self, prepared, *, turn_id, tool_call_id,
                            message_count):
        """Capture before-image durably and bind it to one immutable call."""
        write = getattr(prepared, "prepared_write", None)
        if write is None:
            return prepared, {}
        authority = self.checkpoint_authority()
        capture = authority.capture_prepared(
            self.ag.session_id, str(turn_id), write,
            source_tool_run_id=str(tool_call_id),
            conversation_message_count=message_count)
        return (
            prepared.bind_checkpoint(authority, capture),
            capture.event_payload(),
        )

    def bind_interactive(self, pump, renderer):
        self.pump, self.renderer = pump, renderer
        self.refresh_skills_context()
        self.refresh_custom_commands(publish=False)
        # refresh_custom_commands normally publishes the same catalog.  Only
        # fall back here when custom-command discovery or an embedding pump
        # skipped that path; avoid scanning local metadata twice at startup.
        if self._command_guidance_catalog is None:
            self.refresh_command_guidance(publish=False, force=True)
        self.ag.permission_decision = self.permission_decision
        controller = self.reset_controller()
        self.start_away_recap()
        self.sync_transcript()
        return controller

    def sync_transcript(self):
        if self.renderer is not None:
            self.renderer.set_transcript(self.ag.messages)

    def handle_history_navigation(self, event):
        """Handle bounded transcript navigation on the renderer owner thread."""
        if self.pump is None or self.renderer is None:
            return False
        snapshot = self.pump.snapshot()
        if event.kind == "history_scroll":
            self.renderer.scroll_history(event.value, snapshot)
        elif event.kind == "history_click":
            self.renderer.history_click(event.value, snapshot)
        elif event.kind == "history_mouse":
            self.renderer.history_mouse(event.value, snapshot)
        elif event.kind == "history_copy":
            if not self.renderer.copy_history_selection(snapshot):
                self.renderer.close_history(snapshot)
        elif event.kind == "history_close":
            self.renderer.close_history(snapshot)
        else:
            return False
        self.pump.set_history_mode(self.renderer.history_active)
        return True

    def sync_composer_selection(self):
        """Keep the input thread's Ctrl-C routing aligned with the renderer."""
        if self.pump is None or self.renderer is None:
            return False
        active = bool(getattr(
            self.renderer, "composer_selection_active", False))
        self.pump.set_composer_selection_active(
            active)
        return active

    def handle_composer_selection(self, event):
        """Route app-owned live-composer mouse and clipboard events."""
        if self.pump is None or self.renderer is None:
            return False
        snapshot = self.pump.snapshot()
        if event.kind == "composer_mouse":
            cursor_screen_row = self.pump.cursor_screen_row
            if cursor_screen_row is None:
                wait_for_cursor = getattr(
                    self.pump, "wait_cursor_screen_row", None)
                if callable(wait_for_cursor):
                    # A render sends DSR immediately before the user can
                    # normally click.  The bounded wait closes the small PTY
                    # scheduling gap without blocking the REPL indefinitely
                    # when an emulator does not answer DSR.
                    cursor_screen_row = wait_for_cursor()
            result = self.renderer.composer_mouse(
                event.value, snapshot,
                cursor_screen_row=cursor_screen_row)
            if isinstance(result, tui.ComposerMouseResult):
                handled = result.handled
                cursor = result.cursor
            else:
                # Keep embeddings that returned the old boolean contract
                # source-compatible while the main CLI uses the richer result.
                handled = bool(result)
                cursor = None
            if cursor is not None:
                snapshot = self.pump.set_cursor(cursor, publish=False)
                self.renderer.render(snapshot)
            self.sync_composer_selection()
            return handled
        if event.kind == "composer_copy":
            handled = self.renderer.copy_composer_selection(snapshot)
            if not handled:
                # A stale Ctrl-C event can arrive after an asynchronous frame
                # invalidated the selection.  Do not leave the input thread
                # in a copy-only loop; the next Ctrl-C must regain its normal
                # interrupt meaning.
                self.renderer.clear_composer_selection(snapshot)
            self.sync_composer_selection()
            return handled
        if event.kind == "composer_clear_selection":
            handled = self.renderer.clear_composer_selection(snapshot)
            self.sync_composer_selection()
            return handled
        return False

    def reset_controller(self):
        self._queue_display.clear()
        self._skip_queue_display.clear()
        journal = store.controller_journal(self.ag)
        if journal is None:
            journal = C.MemoryJournal()
        self.controller = C.SessionController(
            self.ag.session_id, journal)
        self.controller.internal_dispatch_enabled = False
        self._recover_interaction_events()
        return self.controller

    def _recover_interaction_events(self):
        """Reconcile owner decisions that were left open by a prior runtime.

        A resumed transcript cannot safely reattach the old provider worker.
        Keep the durable request visible as a bounded diagnostic instead of
        replaying it or silently choosing its recommendation.
        """
        self._recovered_interactions = ()
        controller = getattr(self, "controller", None)
        journal = getattr(controller, "journal", None)
        rows = []
        try:
            getter = getattr(journal, "get_events", None)
            if callable(getter):
                rows = getter(self.ag.session_id)
            else:
                rows = list(getattr(journal, "events", ()) or ())
        except Exception as exc:  # diagnostic only; never block resume
            store.log(
                "interaction_recovery_failed",
                session=self.ag.session_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return ()
        pending = {}
        for row in rows:
            kind = str(row.get("kind") or "") if isinstance(row, dict) else ""
            payload = (row.get("payload") or {}) if isinstance(row, dict) else {}
            if kind == "interaction_requested":
                request_id = str(payload.get("request_id") or "")
                if request_id:
                    pending[request_id] = dict(payload)
            elif kind in {
                    "interaction_resolved", "interaction_aborted",
                    "interaction_expired"}:
                request_id = str(payload.get("request_id") or "")
                pending.pop(request_id, None)
        self._recovered_interactions = tuple(pending.values())
        return self._recovered_interactions

    def cancel_active_work(self):
        """先让 Controller 持久化 intent，再触发 provider/tool handle。"""
        handoff_id = self._task_background_handoff_id
        if (handoff_id
                and self.controller.current_task_id == handoff_id):
            # Ctrl+B 已完成 manager ownership handoff，但 Agent 还没来得及用
            # backgrounded result 闭合 tool protocol。此窗口里的 Esc 不能再按
            # foreground cancel 解释；显式终止仍可用 /kill <id>。
            return None, self.task_manager.get(handoff_id)
        action = self.controller.request_cancel()
        task = None
        if action is not None and action.kind == C.ActionKind.CANCEL_TOOL:
            task = self.task_manager.cancel(
                action.payload.get("tool_call_id"))
        return action, task

    # ------------------------------------------ 终端通知（SPEC-CC-parity H1/H2）
    # 只在交互式 tty 上生效（渲染器自己做 tty 门）；-p、管道、测试里的 StringIO 静默。
    _title_cleanup_registered = False

    def _notify_cfg(self):
        base = dict(CFG.DEFAULTS.get("notify") or {})
        cfg = self.cfg.get("notify") if isinstance(self.cfg, dict) else None
        if isinstance(cfg, dict):
            base.update(cfg)
        return base

    def _notify_turn_complete(self, outcome=None):
        """turn 收尾：响一声 + 标题回到空闲。

        用户自己按 Esc 中断的 turn 不响 —— 人就在键盘前。只有托管交互路径
        （有 pump）才响，非托管 turn() 只把标题复位。"""
        renderer = getattr(self, "renderer", None)
        if renderer is None:
            return
        status = getattr(outcome, "status", None)
        if (self._notify_cfg().get("bell") and status != "interrupted"
                and getattr(self, "pump", None) is not None):
            try:
                renderer.bell()
            except Exception:  # noqa: BLE001 - 通知失败不能改变 turn 结果
                pass
        self._set_terminal_title(idle=True)

    def _set_terminal_title(self, idle=None):
        """idle=True 空闲 ✳、False 工作中 ●、None 清空。首次设置时登记 atexit 清空。"""
        renderer = getattr(self, "renderer", None)
        if renderer is None or not self._notify_cfg().get("title"):
            return
        if idle is None:
            text = ""
        else:
            cwd = str(getattr(self, "_session_cwd", "") or os.getcwd())
            label = os.path.basename(cwd.rstrip("/")) or "zylab"
            text = f"{'✳' if idle else '●'} zylab · {label}"
        try:
            sent = renderer.set_title(text)
        except Exception:  # noqa: BLE001
            return
        if sent and text and not Session._title_cleanup_registered:
            def _clear_title(r=renderer):
                try:
                    r.set_title("")
                except Exception:  # noqa: BLE001 - 退出路径，stdout 可能已关
                    pass
            atexit.register(_clear_title)
            Session._title_cleanup_registered = True

    EDIT_TOOLS = frozenset({"write_file", "edit_file"})

    @property
    def permission_mode(self):
        if getattr(self, "plan_mode", False):
            return "plan"
        return "accept-edits" if getattr(self, "accept_edits", False) else "default"

    def _accept_edits_applies(self, name):
        return bool(getattr(self, "accept_edits", False)
                    and not getattr(self, "plan_mode", False)
                    and name in self.EDIT_TOOLS)

    def cycle_permission_mode(self):
        """Shift+Tab：default → accept-edits → plan → default。返回新模式名。

        进出 plan 走 cmd_plan（存档/退出的语义只在那一处）；accept-edits 只是一个
        开关。模式改的是未来的审批决定，对正在进行的审批无影响。"""
        mode = self.permission_mode
        if mode == "default":
            self.accept_edits = True
            print(GREEN("  ⏵⏵ 接受编辑：write_file / edit_file 免确认，其余照旧确认"
                        "（Shift+Tab 切换）"))
        elif mode == "accept-edits":
            self.accept_edits = False
            cmd_plan(self, "")                     # → plan
        else:
            cmd_plan(self, "")                     # plan → default
            print(DIM("  默认模式：每一步都确认（Shift+Tab 切换）"))
        return self.permission_mode

    def expand_last(self):
        """Ctrl+O（SPEC-CC-parity C2）：展开最近一段折叠输出。

        有工具输出就展开最近一条（等价于无参 /expand）；上一 turn 只有思考没有
        工具调用时展开思考。空闲与工作中都可用 —— /expand 本就是 live 命令。"""
        has_tool = _tool_message_for_expand(self.ag.messages, None) is not None
        if not has_tool and getattr(self, "_last_thinking", None):
            cmd_expand(self, "think")
        else:
            cmd_expand(self, "")

    def composer_prompt(self, *, busy=None):
        """当前输入目标的 prompt；child target 永远优先于 main busy 状态。"""
        if self.attached_agent_id:
            return BLUE(f"agent {self.attached_agent_id[:10]}›") + " "
        if busy is None and self.pump is not None:
            busy = self.pump.snapshot().busy
        accept = getattr(self, "accept_edits", False) and not self.plan_mode
        if busy:
            label = ("plan-steer›" if self.plan_mode
                     else "⏵⏵ steer›" if accept else "steer›")
        else:
            label = "plan›" if self.plan_mode else "⏵⏵›" if accept else "›"
        color = YELLOW if self.plan_mode else GREEN if accept else ORANGE
        return color(label) + " "

    def _cache_agent(self, record):
        if isinstance(record, dict) and record.get("id"):
            run_id = str(record["id"])
            current = dict(self._agent_cache.get(run_id) or {})
            current.update(record)
            self._agent_cache[run_id] = current
            self._agent_cache_at[run_id] = time.monotonic()
            return current
        return record

    def agent_record(self, identifier=None, *, refresh=False):
        identifier = str(identifier or self.attached_agent_id or "").strip()
        if not identifier:
            return None
        cached = self._agent_cache.get(identifier)
        checked = self._agent_cache_at.get(identifier, 0.0)
        if (not refresh and cached is not None
                # ``list_agents`` intentionally caches a lightweight summary
                # without the inbox.  Never let that projection satisfy a
                # read used by the attached-agent composer: a direct message
                # could otherwise be hidden for the short cache TTL.
                and isinstance(cached.get("inbox"), list)
                and time.monotonic() - checked < 0.25):
            return cached
        record = self.agent_workspace.get(identifier)
        cached = self._cache_agent(record)
        # 唯一前缀可能解析成完整 id；让两种 key 都命中同一份短期缓存。
        if identifier != record["id"]:
            self._agent_cache[identifier] = cached
            self._agent_cache_at[identifier] = time.monotonic()
        return cached

    def list_agents(self, limit=100):
        rows = self.agent_workspace.list(
            parent_session_id=self.ag.session_id, limit=limit)
        return [
            self._cache_agent(row) for row in rows
            if row.get("kind") != CONSULTS.KIND
        ]

    def list_consults(self, limit=100):
        rows = self.agent_workspace.list(
            parent_session_id=self.ag.session_id, limit=limit)
        return [
            self._cache_agent(row) for row in rows
            if row.get("kind") == CONSULTS.KIND
        ]

    def consult_record(self, identifier=None):
        hint = str(identifier or "").strip()
        if not hint and isinstance(self._consult_current, dict):
            hint = str(self._consult_current.get("id") or "")
        if hint:
            record = self.agent_workspace.get(hint)
            self._assert_agent_parent(record)
            if record.get("kind") != CONSULTS.KIND:
                run_id = record.get("id") or hint
                raise CONSULTS.ConsultError(
                    f"{run_id} 不是 consult thread")
            self._consult_current = dict(record)
            return self._cache_agent(record)
        rows = self.list_consults()
        if not rows:
            raise CONSULTS.ConsultError("当前会话还没有 consult")
        self._consult_current = dict(rows[0])
        return rows[0]

    def start_consult(self, target_session, question):
        turn_id = (
            getattr(self.controller, "current_turn_id", None)
            or f"consult-{uuid.uuid4().hex[:10]}")
        execution = tools.ExecutionContext.capture(
            session=self.ag.session_id,
            turn_id=turn_id,
            model=getattr(self.ag, "model", ""),
            gateway=getattr(self.ag, "gateway", client.GATEWAY),
            permission_mode="read_only",
            hook_config=self.cfg,
            workspace_root=getattr(self, "_session_cwd", None))
        record = CONSULTS.spawn(
            self.agent_workspace, target_session, question,
            execution_context=execution)
        self._consult_current = dict(record)
        self._cache_agent(record)
        return record

    def send_consult(self, identifier, question):
        record = self.consult_record(identifier)
        item = self.agent_workspace.send(record["id"], question)
        self._consult_current = dict(record)
        self._agent_cache_at.pop(record["id"], None)
        return item

    def cancel_consult(self, identifier=None):
        record = self.consult_record(identifier)
        cancelled = self.agent_workspace.cancel(record["id"])
        self._consult_current = dict(cancelled)
        self._cache_agent(cancelled)
        return cancelled

    def list_workflows(self, limit=100):
        return self.workflow_manager.list(
            parent_session_id=self.ag.session_id, limit=limit)

    def workflow_record(self, identifier=None):
        if identifier:
            record = self.workflow_manager.get(identifier)
            parent = str(record.get("parent_session_id") or "")
            if parent != str(self.ag.session_id):
                raise WORKFLOWS.WorkflowError(
                    f"workflow {record.get('id')} 属于 session "
                    f"{parent or '?'}，当前是 {self.ag.session_id}")
            return record
        active_id = (
            (self._workflow_current or {}).get("id")
            if isinstance(self._workflow_current, dict) else None)
        if active_id:
            try:
                return self.workflow_manager.get(active_id)
            except WORKFLOWS.WorkflowError:
                pass
        record = self.workflow_manager.current(self.ag.session_id)
        if record is None:
            raise WORKFLOWS.WorkflowError("当前会话还没有 workflow")
        return record

    def start_recipe(self, recipe, values=None):
        spec = RECIPES.instantiate(recipe, values)
        turn_id = (
            getattr(self.controller, "current_turn_id", None)
            or f"recipe-{uuid.uuid4().hex[:10]}")
        policy = CFG.workflow_policy(self.cfg)
        record = self.workflow_manager.start_plan(
            spec.get("goal"), spec.get("agents"),
            parent_session_id=self.ag.session_id,
            parent_turn_id=turn_id,
            review=spec.get("review"),
            synthesis=spec.get("synthesis"),
            hook_config=self.cfg,
            limits=policy,
            budget=spec.get("budget") or policy["max_requests"],
            trigger="recipe",
            mode=spec.get("mode") or "research",
            preset=f"recipe:{recipe.get('name')}",
            auto_apply=(spec.get("mode") == "implement"),
            context_capsule=self.build_context_capsule(spec.get("goal")),
            recipe=spec.get("recipe"),
        )
        self._workflow_current = dict(record)
        return record

    def _apply_workflow_seats(self):
        """按 settings workflow.seats 重建生效席位表，并同步工具 schema 的席位枚举。
        配置写错只报告不抛：一条坏配置不该让整个席位表失效。"""
        policy = CFG.workflow_policy(self.cfg)
        seats, problems = M.apply_workflow_seat_overrides(policy.get("seats"))
        tools.sync_workflow_seats()
        for problem in problems:
            print(YELLOW(f"  [workflow seats] {problem}"))
        return seats

    def _sync_workflow_preflight(self, policy=None):
        """按 settings workflow.preflight 决定启动前是否探活：开则注入 models.probe
        （引擎默认不探活——花 provider 请求的决定权在这里）。"""
        policy = policy or CFG.workflow_policy(self.cfg)
        manager = getattr(self, "workflow_manager", None)
        if manager is not None:
            manager.preflight_probe = (
                M.probe if policy.get("preflight", True) else None)

    def _start_workflow_from_tool(
            self, payload, *, execution_context=None):
        if not self.workflow_auto:
            raise WORKFLOWS.WorkflowError(
                "当前 chat 未启用 /workflow auto")
        if not isinstance(payload, dict):
            raise WORKFLOWS.WorkflowError(
                "workflow tool payload 必须是 object")
        session_id = str(self.ag.session_id)
        context_session = str(
            getattr(execution_context, "session", "") or "")
        if context_session != session_id:
            raise WORKFLOWS.WorkflowError(
                "workflow tool call 属于另一个 session")
        # The public WorkflowManager API accepts explicit model routes for
        # trusted callers, but the model-facing adaptive tool is deliberately
        # narrower: it may choose only from the audited flagship seats.  Tool
        # schemas are advisory on some OpenAI-compatible gateways, so enforce
        # nested fields here rather than trusting provider-side validation.
        agents_spec = payload.get("agents")
        if isinstance(agents_spec, list):
            for index, spec in enumerate(agents_spec, 1):
                if (isinstance(spec, dict)
                        and any(key in spec for key in ("model", "gateway"))):
                    raise WORKFLOWS.WorkflowError(
                        f"agents[{index}] 只能使用旗舰 seat，"
                        "不能指定 model/gateway")
        # review/synthesis 已从模型侧 schema 移除：审查与汇总都是普通 DAG 节点。
        turn_id = str(
            getattr(execution_context, "turn_id", "") or "")
        active = [
            row for row in self.workflow_manager.list(
                parent_session_id=session_id, limit=100)
            if row.get("state") in WORKFLOWS.ACTIVE_STATES]
        for row in active:
            if (row.get("trigger") == "model"
                    and str(row.get("parent_turn_id") or "") == turn_id):
                return row
        if active:
            raise WORKFLOWS.WorkflowError(
                f"当前 chat 已有 active workflow {active[0]['id']}；"
                "等待完成或显式取消后再启动")
        policy = CFG.workflow_policy(self.cfg)
        limits = {
            key: policy[key] for key in (
                "max_agents", "max_reviewers", "max_review_rounds",
                "max_requests", "max_requests_per_agent",
                "max_node_attempts", "max_nodes",
                "max_parallel", "max_per_gateway", "max_tokens",
                "max_elapsed_seconds")}
        # 节点数按任务定（用户 2026-09-04）：只受 max_nodes 与预算约束，不再夹到 3。
        self._sync_workflow_preflight(policy)
        return self.workflow_manager.start_plan(
            payload.get("goal"),
            agents_spec,
            parent_session_id=session_id,
            parent_turn_id=turn_id,
            review=None,
            synthesis=None,
            hook_config=self.cfg,
            limits=limits,
            budget=payload.get("budget"),
            trigger="model",
            mode="review",
            preset="adaptive",
            auto_apply=False,
            context_capsule=self.build_context_capsule(
                payload.get("goal")),
        )

    def _control_workflow_from_tool(
            self, payload, *, execution_context=None):
        """Model-facing dynamic controls; destructive controls stay user-only."""
        if not self.workflow_auto:
            raise WORKFLOWS.WorkflowError(
                "当前 chat 未启用 /workflow auto")
        if not isinstance(payload, dict):
            raise WORKFLOWS.WorkflowError(
                "workflow_control payload 必须是 object")
        if str(getattr(execution_context, "session", "") or "") != str(
                self.ag.session_id):
            raise WORKFLOWS.WorkflowError(
                "workflow_control 属于另一个 session")
        identifier = str(payload.get("workflow_id") or "").strip()
        record = self.workflow_record(identifier or None)
        action = str(payload.get("action") or "").strip().lower()
        if action == "status":
            return self.workflow_manager.status_projection(record["id"])
        if record.get("state") not in WORKFLOWS.ACTIVE_STATES:
            raise WORKFLOWS.WorkflowError(
                f"workflow {record['id']} 已是 {record.get('state')}")
        if action == "add":
            specs = payload.get("nodes")
            if isinstance(specs, list):
                if len(specs) > 2:
                    raise WORKFLOWS.WorkflowError(
                        "workflow_control add 每次最多追加 2 个节点；"
                        "只追加当前证据确实需要的新分支")
                for index, spec in enumerate(specs, 1):
                    if (isinstance(spec, dict)
                            and any(key in spec for key in (
                                "model", "gateway"))):
                        raise WORKFLOWS.WorkflowError(
                            f"nodes[{index}] 只能选择旗舰 seat")
            updated = self.workflow_manager.add_nodes(
                record["id"], specs, requested_by="main-agent")
            return self.workflow_manager.status_projection(updated["id"])
        if action == "send":
            result = self.workflow_manager.send_node(
                record["id"], payload.get("node"),
                payload.get("message"), requested_by="main-agent")
            return {
                "workflow_id": record["id"],
                "node": payload.get("node"),
                "message_id": (result.get("message") or {}).get("id"),
                "state": (result.get("workflow") or {}).get("state"),
            }
        raise WORKFLOWS.WorkflowError(
            "workflow_control action 只能是 status/add/send")

    def recover_workflows(self):
        recovered = self.workflow_manager.recover(
            self.ag.session_id, hook_config=self.cfg)
        if recovered:
            self._workflow_current = dict(recovered[-1])
        else:
            current = self.workflow_manager.current(self.ag.session_id)
            if current is not None:
                self._workflow_current = dict(current)
        return recovered

    def queue_workflow_apply(self, record, *, manual=False):
        workflow_id = str(record.get("id") or "")
        # failed 但带部分产出（预算耗尽/部分节点失败）也允许交接：已花的钱不白花
        if (record.get("state") not in {"completed", "failed"}
                or not record.get("result")):
            raise WORKFLOWS.WorkflowError(
                f"workflow {workflow_id or '?'} 尚无可交接的报告")
        if workflow_id in self._workflow_apply_ids:
            return False
        if (not manual
                and record.get("apply_status") in {"queued", "dispatched"}):
            return False
        result = tui.sanitize_terminal_text(
            AGENTS.project_report(record.get("result")))
        plan = record.get("plan") or {}
        nodes = plan.get("agents") or []
        node_count = len(nodes)
        review_nodes = sum(
            1 for node in nodes if str(node.get("role") or "") == "review")
        synthesized = any(
            str(node.get("role") or "") == "synthesis"
            and node.get("state") == "completed" for node in nodes)
        if node_count:
            report_shape = f"{node_count} 个异构只读 DAG 节点"
            if review_nodes:
                report_shape += f"（含 {review_nodes} 个审查节点）"
            report_shape += (
                "生成的独立 synthesis" if synthesized
                else "所形成的有界汇总报告")
        else:
            report_shape = "异构只读 workflow 的持久化报告"
        prompt = (
            AGENT_EVENTS.NOTICE_HEADER + "\n"
            f"[异构 workflow {workflow_id} 已完成]\n"
            f"原始目标：{record.get('goal') or '?'}\n\n"
            f"以下是{report_shape}。"
            "请作为唯一具有写权限的主 agent，先核验证据，再实施必要修改并运行测试；"
            "不要把报告中的推测冒充事实。\n\n"
            + result)
        self._workflow_apply_queue.append({
            "id": workflow_id,
            "prompt": prompt,
        })
        self._workflow_apply_ids.add(workflow_id)
        self.workflow_manager.mark_apply(workflow_id, "queued")
        return True

    def dispatch_workflow_ready(self):
        """仅由 idle 主线程调用；让交接像正常 queued prompt 一样进入 chat。"""
        if not self._workflow_apply_queue:
            return None
        item = self._workflow_apply_queue.pop(0)
        action = self.controller.submit(
            item["prompt"], C.QueueMode.NEXT_TURN)
        self.workflow_manager.mark_apply(item["id"], "dispatched")
        return action

    def _workflow_node_lines(self, current, node):
        """J7：workflow 节点完成/失败与子代理同一条永久行；running 只刷新 dock。"""
        if not isinstance(node, dict) or not node.get("key"):
            return []
        plan = dict(current.get("plan") or {})
        agents_list = [dict(item) for item in plan.get("agents") or []]
        for index, item in enumerate(agents_list):
            if item.get("key") == node.get("key"):
                agents_list[index] = dict(node)
                break
        else:
            agents_list.append(dict(node))
        plan["agents"] = agents_list
        current["plan"] = plan
        self._workflow_current = current
        state = str(node.get("state") or "")
        if state not in {"completed", "failed", "cancelled"}:
            return []
        tokens = None
        agent_id = str(node.get("agent_id") or "")
        if agent_id:
            try:
                tokens = self._agent_tokens(self.agent_workspace.get(agent_id))
            except Exception:
                tokens = None
        event_obj = AGENT_EVENTS.from_workflow_node(current, node, tokens=tokens)
        self._remember_agent_event(event_obj)
        if agent_id:
            self._agent_activity.pop(agent_id, None)
        line = AGENT_EVENTS.finished_line(event_obj)
        if state == "cancelled":
            return [DIM(line) + "\r\n"]
        hint = AGENT_EVENTS.finished_hint(event_obj, agent_id or None)
        return [line + "\r\n" + DIM(hint) + "\r\n"]

    def drain_workflow_events(self):
        """主线程渲染 workflow 事件，并把 implement synthesis 排入主 chat。"""
        notices = []
        for event in self.workflow_manager.drain():
            kind = str(event.get("kind") or "workflow_event")
            payload = dict(event.get("payload") or {})
            parent = str(payload.get("parent_session_id") or "")
            if parent and parent != str(self.ag.session_id):
                continue
            node = payload.pop("node", None) if kind == "workflow_node" else None
            current = dict(self._workflow_current or {})
            current.update(payload)
            self._workflow_current = current
            workflow_id = str(payload.get("id") or "?")
            if kind == "workflow_node":
                notices.extend(self._workflow_node_lines(current, node))
                continue
            if kind == "workflow_progress":
                notices.append(DIM(
                    f"  [workflow {workflow_id} → "
                    f"{payload.get('stage') or '?'}]\r\n"))
            elif kind == "workflow_recovered":
                notices.append(YELLOW(
                    f"  [workflow {workflow_id} 已从 durable checkpoint 恢复"
                    f" · recovery #{int(payload.get('recovery_count') or 1)}]"
                    "\r\n"))
            elif kind == "workflow_completed":
                try:
                    record = self.workflow_record(workflow_id)
                    result = tui.sanitize_terminal_text(
                        AGENTS.project_report(record.get("result"))).strip()
                    if len(result) > 4000:
                        result = (
                            result[:4000]
                            + "\n…[/workflow open 查看完整 synthesis]")
                    if record.get("auto_apply"):
                        self.queue_workflow_apply(record)
                        handoff = " · 已排入 main agent"
                    else:
                        handoff = " · /workflow apply 可交给 main agent"
                    notices.append(
                        f"  {GREEN('◆')} workflow {workflow_id} completed"
                        f"{handoff}\r\n"
                        + (result.replace("\n", "\r\n") + "\r\n"
                           if result else ""))
                except WORKFLOWS.WorkflowError as exc:
                    notices.append(RED(
                        f"  [workflow {workflow_id} 完成但读取失败：{exc}]\r\n"))
            elif kind == "workflow_failed":
                notices.append(RED(
                    f"  [workflow {workflow_id} failed] "
                    f"{payload.get('error') or 'unknown error'}\r\n"))
                partial = tui.sanitize_terminal_text(
                    AGENTS.project_report(payload.get("result"))).strip()
                if partial:
                    if len(partial) > 4000:
                        partial = partial[:4000] + "\n…[/workflow open 查看完整报告]"
                    notices.append(
                        DIM("  已产出的部分报告随失败带回 · /workflow apply 可交给 main agent\r\n")
                        + partial.replace("\n", "\r\n") + "\r\n")
            elif kind == "workflow_cancelled":
                notices.append(DIM(
                    f"  [workflow {workflow_id} 已取消]\r\n"))
        return notices

    def _assert_agent_parent(self, record):
        parent = str(record.get("parent_session_id") or "")
        if parent != str(self.ag.session_id):
            raise AGENTS.AgentRuntimeError(
                f"child agent {record.get('id')} 属于 session {parent or '?'}，"
                f"当前是 {self.ag.session_id}")

    @staticmethod
    def _agent_history(record):
        messages = (record.get("transcript") or {}).get("messages") or []
        rows = []
        for message in messages:
            if message.get("role") != "user":
                continue
            text = _agent_message_text(message)
            if text:
                rows.append(text)
        rows.extend(
            str(item.get("text") or "")
            for item in record.get("inbox") or []
            if item.get("state") == "queued" and item.get("text"))
        return rows

    def attach_agent(self, identifier):
        record = self.agent_record(identifier, refresh=True)
        self._assert_agent_parent(record)
        run_id = record["id"]
        snapshot = self.pump.snapshot() if self.pump is not None else None
        if snapshot is not None:
            if self.attached_agent_id:
                self._agent_drafts[self.attached_agent_id] = snapshot.text
            else:
                self._main_draft = snapshot.text
        self.attached_agent_id = run_id
        if self.pump is not None:
            busy = snapshot.busy
            self.pump.configure(
                target=run_id, target_history=self._agent_history(record),
                busy=busy, prompt=self.composer_prompt(busy=busy),
                queued=self._composer_queue(), status=status_line(self),
                activity=snapshot.activity, publish=False)
            self.pump.replace_buffer(
                self._agent_drafts.get(run_id, ""),
                active=True, publish=True)
        return record

    def detach_agent(self, *, draft=None, publish=True):
        run_id = self.attached_agent_id
        if not run_id:
            return None
        snapshot = self.pump.snapshot() if self.pump is not None else None
        self._agent_drafts[run_id] = (
            str(draft) if draft is not None
            else (snapshot.text if snapshot is not None else ""))
        self.attached_agent_id = None
        if self.pump is not None:
            busy = snapshot.busy
            self.pump.configure(
                target=None, busy=busy,
                prompt=self.composer_prompt(busy=busy),
                queued=self._composer_queue(), status=status_line(self),
                activity=snapshot.activity, publish=False)
            self.pump.replace_buffer(
                self._main_draft, active=True, publish=publish)
        return run_id

    def reset_agent_workspace(self, *, previous_session_id=None,
                              previous_controller=None):
        """切换主会话时关闭旧 target，避免 child route 泄漏到新 parent。"""
        # Allocate first: if a fresh workspace cannot be created, leave the
        # current one completely untouched.  Once cleanup starts, however, a
        # fresh workspace must be installed even when an old lifecycle event
        # cannot be journalled.
        fresh_workspace = AGENTS.AgentWorkspace()
        fresh_workflow_manager = WORKFLOWS.WorkflowManager(
            fresh_workspace)
        workspace = getattr(self, "agent_workspace", None)
        workflow_manager = getattr(self, "workflow_manager", None)
        old_session_id = str(
            previous_session_id
            if previous_session_id is not None
            else getattr(self.ag, "session_id", ""))
        old_controller = (
            previous_controller
            if previous_session_id is not None
            else getattr(self, "controller", None))

        def log_cleanup_failure(phase, error, **fields):
            store.log(
                "agent_workspace_reset_failed",
                session=old_session_id,
                phase=phase,
                error=f"{type(error).__name__}: {error}",
                **fields,
            )

        try:
            if self.attached_agent_id:
                try:
                    self.detach_agent(publish=False)
                except Exception as exc:  # noqa: BLE001
                    log_cleanup_failure("detach", exc)
            if workflow_manager is not None:
                try:
                    workflow_manager.close()
                except Exception as exc:  # noqa: BLE001
                    log_cleanup_failure("workflow_close", exc)
            if workspace is not None:
                try:
                    workspace.close()
                except Exception as exc:  # noqa: BLE001
                    log_cleanup_failure("close", exc)
                # close 可能让本 workspace 启动的 resume 转成 cancelled；仍由旧
                # parent 的主线程逐条 journal，单条失败不能丢掉后续 lifecycle。
                try:
                    events = workspace.drain()
                except Exception as exc:  # noqa: BLE001
                    log_cleanup_failure("drain", exc)
                    events = ()
                for event in events:
                    try:
                        kind = str(
                            event.get("kind") or "agent_workspace_event")
                        payload = dict(event.get("payload") or {})
                        parent = str(
                            payload.get("parent_session_id") or "")
                        if parent and parent != old_session_id:
                            continue
                        if (old_controller is not None
                                and kind != "agent_activity"):
                            old_controller.record_event(
                                kind, payload,
                                turn_id=(
                                    payload.get("parent_turn_id") or None))
                    except Exception as exc:  # noqa: BLE001
                        log_cleanup_failure(
                            "journal", exc,
                            event_kind=(
                                str(event.get("kind") or "")
                                if isinstance(event, dict) else "invalid"),
                        )
        finally:
            self.agent_workspace = fresh_workspace
            self.workflow_manager = fresh_workflow_manager
            self._workflow_current = None
            self._consult_current = None
            apply_queue = getattr(self, "_workflow_apply_queue", None)
            if apply_queue is None:
                self._workflow_apply_queue = []
            else:
                apply_queue.clear()
            apply_ids = getattr(self, "_workflow_apply_ids", None)
            if apply_ids is None:
                self._workflow_apply_ids = set()
            else:
                apply_ids.clear()
            # 切会话/新会话：旧 chat 的后台任务交接不能流进新 chat
            self._task_handoff_queue = []
            self._task_handoff_ids = set()
            self.attached_agent_id = None
            self._agent_cache.clear()
            self._agent_cache_at.clear()
            self._agent_projection_changed = False
            self._main_draft = ""
            self._agent_drafts.clear()
            self._pending_route = None

    def close(self):
        recap_controller = getattr(self, "away_recap_ctl", None)
        if recap_controller is not None:
            recap_controller.dispose()                # 定时器与在途请求都别拖住退出
        manager = getattr(self, "task_manager", None)
        workflow_manager = getattr(self, "workflow_manager", None)
        workspace = getattr(self, "agent_workspace", None)
        transport_authorizer = getattr(self, "_transport_authorizer", None)
        workspace_closed = True
        try:
            if manager is not None:
                manager.shutdown()
        finally:
            # 若 CLI 在 background protocol handoff 窗口退出，shutdown 已经
            # 收束 worker；释放 terminal gate，让 finally 中最后一次 drain
            # 仍能把 terminal 记回原 controller/turn。
            self._task_background_handoff_id = None
            try:
                if workflow_manager is not None:
                    workflow_manager.close()
            finally:
                try:
                    if workspace is not None:
                        workspace_closed = workspace.close()
                finally:
                    try:
                        self.release_all_session_leases()
                    finally:
                        if transport_authorizer is not None:
                            client.clear_transport_authorizer(
                                transport_authorizer)
        return workspace_closed

    def acquire_target_lease(self, session_id):
        """Hold a target lease without releasing the current session yet."""
        session_id = str(session_id)
        if session_id == self._leased_session_id:
            self.heartbeat_lease(force=True)
            return False
        store.acquire_session_lease(session_id, self.lease_owner_id)
        self._pending_session_leases.add(session_id)
        return True

    def commit_target_lease(self, session_id):
        """Adopt a previously acquired target, then release the old lease."""
        session_id = str(session_id)
        old = self._leased_session_id
        if old == session_id:
            return False
        # Verify the target before touching the old lease.  Keep the in-memory
        # pointer on old until old release succeeds, so the caller can still
        # roll back the provisional target on a release failure.
        store.heartbeat_session_lease(session_id, self.lease_owner_id)
        if old:
            store.release_session_lease(old, self.lease_owner_id)
        self._leased_session_id = session_id
        self._pending_session_leases.discard(session_id)
        self._last_lease_heartbeat = time.monotonic()
        return True

    def rollback_target_lease(self, session_id):
        session_id = str(session_id)
        if session_id == self._leased_session_id:
            return False
        try:
            released = store.release_session_lease(
                session_id, self.lease_owner_id)
        except store.SessionBusyError as exc:
            self._pending_session_leases.discard(session_id)
            store.log(
                "session_target_lease_release_failed",
                session=session_id,
                error=str(exc),
            )
            return False
        except OSError as exc:
            # Keep it tracked so close() or a later retry can release it.
            store.log(
                "session_target_lease_release_failed",
                session=session_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        else:
            self._pending_session_leases.discard(session_id)
            return released

    def ensure_current_lease(self):
        session_id = str(self.ag.session_id)
        if self._leased_session_id is None:
            store.acquire_session_lease(
                session_id, self.lease_owner_id)
            self._leased_session_id = session_id
            self._last_lease_heartbeat = time.monotonic()
        elif self._leased_session_id != session_id:
            raise store.SessionBusyError(
                session_id, {
                    "owner_id": "session-switch-not-committed",
                    "pid": os.getpid(),
                    "hostname": "local",
                })
        return self._leased_session_id

    def heartbeat_lease(self, *, force=False):
        session_ids = [
            session_id for session_id in (
                [self._leased_session_id]
                + sorted(self._pending_session_leases))
            if session_id
        ]
        if not session_ids:
            return None
        now = time.monotonic()
        if not force and now - self._last_lease_heartbeat < 5.0:
            return None
        records = [
            store.heartbeat_session_lease(
                session_id, self.lease_owner_id)
            for session_id in session_ids
        ]
        self._last_lease_heartbeat = now
        return records[0] if len(records) == 1 else records

    def release_current_lease(self):
        session_id = self._leased_session_id
        if not session_id:
            return False
        try:
            released = store.release_session_lease(
                session_id, self.lease_owner_id)
        except store.SessionBusyError as exc:
            self._leased_session_id = None
            store.log(
                "session_lease_release_failed",
                session=session_id,
                error=str(exc),
            )
            return False
        except OSError as exc:
            store.log(
                "session_lease_release_failed",
                session=session_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        else:
            self._leased_session_id = None
            return released

    def release_all_session_leases(self):
        """Release provisional targets as well as the adopted current lease."""
        released = False
        for session_id in list(self._pending_session_leases):
            released = self.rollback_target_lease(session_id) or released
        return self.release_current_lease() or released

    def cwd_switch_blocker(self):
        """Explain why process-global chdir is unsafe right now, if anything."""
        manager = getattr(self, "task_manager", None)
        if manager is not None:
            active = [
                task for task in manager.list()
                if task.status not in TASKS.TERMINAL_STATES
            ]
            if active:
                return f"{len(active)} 个 managed task 仍在运行"
        workspace = getattr(self, "agent_workspace", None)
        if workspace is not None and workspace.active():
            return "child agent worker 仍在运行"
        return None

    def background_active_task(self):
        """持久化 Ctrl+B intent，再把当前 foreground Bash 交回 manager。"""
        task = self.task_manager.foreground()
        if task is None:
            raise TASKS.TaskError("当前没有可转后台的 foreground Bash")
        if task.name != "bash":
            raise TASKS.TaskError(
                f"task {task.id} 是 {task.name}，只有 Bash 可转后台")
        controller = self.controller
        turn_id = (
            controller.current_turn_id if controller is not None else None)
        payload = {
            "task_id": task.id,
            "tool_call_id": task.key,
            "name": task.name,
        }
        if controller is not None:
            controller.record_event(
                "task_background_requested", payload, turn_id=turn_id)
        try:
            snapshot = self.task_manager.background(task.id)
        except Exception as exc:
            if controller is not None:
                controller.record_event(
                    "task_background_failed",
                    {**payload, "error": f"{type(exc).__name__}: {exc}"},
                    turn_id=turn_id)
            raise
        self._background_task_context[snapshot.id] = (
            controller, turn_id)
        self._task_background_handoff_id = snapshot.id
        if self.attached_task_id == snapshot.id:
            self.attached_task_id = None
        return snapshot

    def detach_task(self):
        task_id = self.attached_task_id
        self.attached_task_id = None
        return task_id

    def background_model_task(self, task_id):
        """模型请求把刚起的 bash 转后台。走和 Ctrl+B 完全相同的那条路 ——
        包括 controller 审计、handoff 上下文与完成后自动交回模型。"""
        return self.background_active_task()

    def task_status_for_tool(self, payload):
        """把 TaskManager 已有的 list/get/tail/cancel 暴露给模型。

        为什么值得单开一个入口：以前只有用户能用 /tasks /task /kill，模型起不了
        后台任务也看不了 —— 一个二十分钟的命令只能在前台干等。引擎一直都在。
        """
        payload = dict(payload or {})
        action = str(payload.get("action") or "status").lower()
        identifier = str(payload.get("task_id") or "").strip()
        manager = self.task_manager
        if action == "status" and not identifier:
            rows = []
            for snapshot in manager.list():
                item = snapshot.as_dict()
                rows.append({
                    "task_id": item.get("id"),
                    "name": item.get("name"),
                    "status": str(item.get("status")),
                    "background": bool(item.get("background")),
                    "stdout_bytes": item.get("stdout_bytes"),
                    "returncode": item.get("returncode"),
                })
            return {"tasks": rows, "count": len(rows)}
        snapshot = manager.get(identifier)
        if snapshot is None:
            return {"task_id": identifier, "status": "unknown",
                    "error": "没有这个 task；用不带参数的 status 列出全部"}
        if action == "stop":
            manager.cancel(snapshot.id)
            after = manager.get(snapshot.id)
            return {"task_id": snapshot.id, "stopped": True,
                    "status": (after.as_dict().get("status")
                               if after is not None else "cancelled")}
        item = snapshot.as_dict()
        result = {
            "task_id": item.get("id"), "name": item.get("name"),
            "status": str(item.get("status")),
            "returncode": item.get("returncode"),
            "stdout_bytes": item.get("stdout_bytes"),
            "stderr_bytes": item.get("stderr_bytes"),
        }
        if action == "output":
            limit = int(payload.get("tail") or 4000)
            try:
                view = manager.tail(snapshot.id, limit=limit)
            except Exception as exc:                  # noqa: BLE001
                result["error"] = f"{type(exc).__name__}: {exc}"
            else:
                for stream in ("stdout", "stderr"):
                    blob = (view or {}).get(stream) or {}
                    text = str(blob.get("text") or "")
                    if text:
                        result[stream] = text
                        result[f"{stream}_truncated"] = bool(
                            blob.get("truncated"))
        return result

    def child_extra_tools(self):
        """child 能额外继承哪些能力。**继承，不是授予。**

        用户 2026-09-22 定：child 要能联网。但 `web_fetch` 的权限默认是 `ask`
        （它是模型唯一的网络出口），而 child 的 `confirm` 是硬编码 True、
        又拿不到 decision_gate —— 无条件给它就等于让它无声地绕过那道 ask。

        所以判据是「**父会话对 web_fetch 已经是允许的**」：配置写了 allow、
        本次会话按过 always、或者在 yolo 里。三者都是用户已经对这一会话表过态，
        child 继承它不新增同意面、也不构成提权。
        没表过态时 child 照旧没有网络出口——它会在报告里说「需要联网但没有」，
        而主 agent 的工具描述里也写着这条（可发现性，见 2026-09-22 报告 §2）。
        """
        granted = []
        for name in sorted(AGENTS.CHILD_EXTRA_ALLOWED):
            policy_name, perm = _configured_permission(self.cfg, name, {})
            if perm == "deny":
                continue                               # 明确禁掉的，谁都不继承
            if perm == "allow" or self.auto or policy_name in self.always:
                granted.append(name)
        return tuple(granted)

    def subagent_control_for_tool(self, payload):
        """模型侧的 `/agents list|peek|send` —— 派得出去，也要能对话。

        走的是和用户命令完全相同的 `agent_record` / `send_agent`，包括
        `_assert_agent_parent`：只能碰自己派出去的 child，碰不到别人的。
        """
        payload = dict(payload or {})
        action = str(payload.get("action") or "list").lower()
        identifier = str(payload.get("agent_id") or "").strip()
        if action == "list":
            rows = []
            for row in self.list_agents(limit=50):
                rows.append({
                    "agent_id": row.get("id"),
                    "name": _goal_ui_text(row.get("name"))[:80],
                    "state": row.get("state"),
                    "seat": row.get("seat") or row.get("model"),
                    "kind": row.get("kind"),
                })
            return {"agents": rows, "count": len(rows)}
        try:
            record = self.agent_record(identifier, refresh=True)
            if record is None:
                raise AGENTS.AgentRuntimeError(f"没有这个 child agent：{identifier}")
            self._assert_agent_parent(record)
        except (AGENTS.AgentRuntimeError, ValueError) as exc:
            return {"agent_id": identifier, "error": str(exc)}
        if action == "send":
            try:
                self.send_agent(record["id"], payload.get("message") or "")
            except (AGENTS.AgentRuntimeError, ValueError) as exc:
                return {"agent_id": record["id"], "error": str(exc)}
            return {
                "agent_id": record["id"], "delivered": True,
                "state": record.get("state"),
                "note": "它会在自己的安全边界收到这条消息；结果照常回到你手上，"
                        "不要在这里空转等它",
            }
        return {
            "agent_id": record["id"],
            "name": _goal_ui_text(record.get("name"))[:80],
            "state": record.get("state"),
            "seat": record.get("seat") or record.get("model"),
            "task": _goal_ui_text(record.get("task"))[:400],
            "report": AGENTS.project_report(record.get("result")),
            "error": record.get("error") or None,
        }

    def context_status_for_tool(self, _payload=None):
        """把 `/context` 已经算出来的预算报告，做成模型能读的一小份摘要。"""
        report = self.ag.context_report()
        limit = int(getattr(self.ag, "ctx_limit", 0) or 0)
        used = int(report.get("estimated_request_tokens") or 0)
        budget = int(report.get("usable_budget") or 0)
        summary = report.get("summary") or {}
        previews = report.get("tool_previews") or {}
        remaining = max(0, budget - used)
        return {
            "used_tokens": used,
            "usable_budget": budget,
            "remaining_tokens": remaining,
            "model_limit": limit,
            "percent_used": (round(used * 100.0 / budget, 1)
                             if budget else None),
            "fits": bool(report.get("fits")),
            "summary_status": str(summary.get("status") or "none"),
            "aged_saved_chars": int(previews.get("saved_chars") or 0),
            "omitted_ranges": len(report.get("omitted_ranges") or []),
            "advice": (
                "余量充足" if budget and remaining > budget * 0.35 else
                "余量偏紧：把调研交给 subagent，或让长输出落盘再按需读"),
        }

    def expand_output_for_tool(self, payload):
        """按占位里的 sha256:… / task_id / tool_call_id 取回工具输出原文的一页。"""
        payload = dict(payload or {})
        target = str(payload.get("target") or "").strip()
        page = int(payload.get("page") or 1)
        # 占位里**写出来的**是 sha256:…——先拿它在会话原始记录里认人；认得出来就顺着它的
        # tool_call_id 去找落盘的完整输出（后台 / 前台 bash 都有），没有落盘的再退回原始记录。
        messages = getattr(getattr(self, "ag", None), "messages", None)
        message = CONTEXT.find_tool_result(messages, target)
        hint = str((message or {}).get("tool_call_id") or "") or target
        manager = self.task_manager
        try:
            view = manager.artifact_page(hint, page=page)
        except TASKS.TaskError as exc:
            if message is None:
                return {"target": target, "error": str(exc)}
            view = None
        if view is None and message is None:
            try:
                message = _tool_message_for_expand(messages, target)
            except TASKS.TaskError as exc:
                return {"target": target, "error": str(exc)}
        if view is None:
            if message is None:
                return {"target": target,
                        "error": "没有这个目标；照抄占位行里的 sha256:…，"
                                 "或给 task_id / tool_call_id"}
            try:
                chunk = _fallback_result_page(message, page)
            except TASKS.TaskError as exc:
                return {"target": target, "error": str(exc)}
            return {
                "target": target,
                "page": page,
                "text": chunk["text"],
                "total_bytes": chunk["total_bytes"],
                "has_more": bool(chunk["has_more"]),
                "next_page": page + 1 if chunk["has_more"] else None,
                "missing_streams": [],
            }
        # artifact_page 返回按流切分的 sections，拼起来给模型，并说清还有没有下一页
        sections = view.get("sections") or []
        parts = []
        for section in sections:
            text = str(section.get("text") or "")
            if not text:
                continue
            parts.append(
                f"[{section.get('stream')}]\n{text}"
                if len(sections) > 1 else text)
        return {
            "target": target,
            "page": view.get("page", page),
            "text": "\n".join(parts),
            "total_bytes": view.get("total_bytes"),
            "has_more": bool(view.get("has_more")),
            "next_page": (int(view.get("page") or page) + 1
                          if view.get("has_more") else None),
            "missing_streams": view.get("missing") or [],
        }

    def handle_background_key(self):
        """Ctrl+B：优先后台化 foreground Bash，否则 detach task tail。"""
        if self.task_manager.foreground() is not None:
            return "backgrounded", self.background_active_task()
        task_id = self.detach_task()
        if task_id:
            return "detached", task_id
        raise TASKS.TaskError("当前没有 foreground Bash 或 attached task")

    def drain_task_events(self):
        """主线程消费 background task 事件；后台线程绝不直接写 terminal。"""
        notices = []
        terminal = {"completed", "failed", "cancelled"}
        deferred = (
            {self._task_background_handoff_id}
            if self._task_background_handoff_id else set())
        for event in self.task_manager.drain_background(
                defer_terminal_ids=deferred):
            kind = str(event.get("t") or "")
            task_id = str(event.get("task_id") or "")
            if kind == "output":
                if task_id == self.attached_task_id:
                    notices.append({
                        "text": str(event.get("v") or ""),
                        "source": f"task:{task_id}",
                    })
                continue
            if kind == "note":
                if task_id == self.attached_task_id:
                    notices.append({
                        "text": YELLOW(
                            f"  [task {task_id} hook] "
                            f"{event.get('v', '')}\r\n"),
                        "source": "status",
                    })
                continue
            if kind == "runtime":
                runtime_event = dict(event.get("event") or {})
                if runtime_event.get("kind") == "workspace_changed":
                    try:
                        self.refresh_repo_context(force=False)
                    except Exception as exc:             # noqa: BLE001
                        notices.append({
                            "text": YELLOW(
                                "  [后台任务已修改工作区，但 repo projection "
                                "刷新失败："
                                f"{type(exc).__name__}: {exc}]\r\n"),
                            "source": "status",
                        })
                # 其他 background runtime payload 不直接写 terminal。
                continue
            if kind not in terminal:
                continue
            task = dict(event.get("task") or {})
            context = self._background_task_context.pop(task_id, None)
            controller, turn_id = context or (self.controller, None)
            if controller is not None:
                controller.record_event(
                    "task_finished",
                    {
                        "task_id": task_id,
                        "tool_call_id": task.get("key"),
                        "name": task.get("name"),
                        "status": kind,
                        "result": str(event.get("v") or "")[:20_000],
                        "task": task,
                    },
                    turn_id=turn_id)
            total = (
                int(task.get("stdout_bytes") or 0)
                + int(task.get("stderr_bytes") or 0))
            artifacts = [
                path for path in (
                    task.get("stdout_path"), task.get("stderr_path"))
                if path
            ]
            suffix = (
                " · " + " · ".join(artifacts) if artifacts else "")
            outcome = _task_outcome(task)
            detail = f" · {outcome}" if outcome else ""
            try:
                queued = self._queue_task_handoff(
                    task_id, kind, task, str(event.get("v") or ""), outcome)
            except Exception:                           # noqa: BLE001
                queued = False                          # 交接失败不能吞掉完成通知
            handoff_note = " · 结果已排入主 agent" if queued else ""
            notices.append({
                "text": DIM(
                    f"  [task {task_id} · {kind} · "
                    f"{total / 1024:.1f} KiB{detail}{suffix}{handoff_note}]\r\n"),
                "source": "status",
            })
            if self.attached_task_id == task_id:
                self.attached_task_id = None
        return notices

    def _task_handoff_enabled(self):
        return bool((self.cfg or {}).get("background_task_handoff", True))

    def _queue_task_handoff(self, task_id, kind, task, result, outcome):
        """后台任务结束 → 排一条交接 prompt，模型在下一个安全边界自动继续。

        以前只在界面打一行通知、日志记一条 task_finished，结果不回到模型手里，
        模型不会自己接着干（用户 2026-09-04 问"这个功能 zylab 有吗"）。取消的不交接；
        每个任务只交接一次；settings background_task_handoff=false 可关。
        """
        if kind not in {"completed", "failed"} or not task.get("background"):
            return False
        if not self._task_handoff_enabled():
            return False
        # 旧会话对象 / 测试夹具可能没有这两个属性：缺了就建，不让交接把通知带崩
        if getattr(self, "_task_handoff_queue", None) is None:
            self._task_handoff_queue = []
        if getattr(self, "_task_handoff_ids", None) is None:
            self._task_handoff_ids = set()
        if getattr(self, "controller", None) is None:
            return False
        if task_id in self._task_handoff_ids:
            return False
        event_obj = AGENT_EVENTS.from_task_event({
            "t": kind, "task_id": task_id,
            "v": tui.sanitize_terminal_text(str(result or "")), "task": task})
        self._remember_agent_event(event_obj)
        prompt = AGENT_EVENTS.notification(event_obj)
        self._task_handoff_queue.append({"id": task_id, "prompt": prompt})
        self._task_handoff_ids.add(task_id)
        return True

    def dispatch_task_handoff_ready(self):
        """仅由 idle 主线程调用；交接像正常 queued prompt 一样进入 chat。"""
        queue = getattr(self, "_task_handoff_queue", None)
        if not queue:
            return None
        item = queue.pop(0)
        return self.controller.submit(item["prompt"], C.QueueMode.NEXT_TURN)

    def send_agent(self, identifier, text):
        record = self.agent_record(identifier, refresh=True)
        self._assert_agent_parent(record)
        item = self.agent_workspace.send(record["id"], text)
        self._agent_cache_at.pop(record["id"], None)
        return item

    def _compaction_state(self):
        """压缩时随摘要带回的会话级状态：agent 自己看不到任务计划（它归 Session 管）。"""
        return {"task_plan": getattr(self, "task_plan", None)}

    # ---------------------------------------------------------- away recap
    # 照搬 Claude Code 的 awaySummary。何时写、何时不打扰在 core/away_recap.py；
    # 这里只把它接到会话上：状态从哪读、用哪条 route 写、结果怎么交回主循环。
    def start_away_recap(self):
        """交互会话才有：订阅终端焦点事件，离开够久就后台写一行 recap 等人回来。"""
        policy = CFG.recap_policy(getattr(self, "cfg", None))
        controller = AWAY_RECAP.AwayRecapController(
            generate=self._generate_away_recap, state=self._away_recap_state,
            deliver=self._deliver_away_recap, enabled=policy["enabled"],
            delay=policy["away_seconds"], hint=AWAY_RECAP_HINT,
            cancel_factory=client.CancellationHandle)
        self.away_recap_ctl = controller
        try:
            self.pump.on_focus = controller.focus_changed
        except AttributeError:                        # 嵌入方自带的 pump 不支持
            pass
        return controller

    def _away_recap_background(self):
        """还在跑的后台工作。有就不打扰：它们的完成通知更要紧，recap 也马上过时。"""
        count = 0
        try:
            count += int(self._count_active_agents(max_age=5.0))
        except Exception:                             # noqa: BLE001
            pass
        try:
            manager = getattr(self, "task_manager", None)
            if manager is not None:
                count += sum(
                    1 for task in manager.list()
                    if task.status not in TASKS.TERMINAL_STATES)
        except Exception:                             # noqa: BLE001
            pass
        return count

    def _away_recap_state(self):
        editor = getattr(getattr(self, "pump", None), "editor", None)
        last = getattr(self.ag, "away_recap", None) or {}
        return {
            "loading": bool(getattr(self, "_turn_running", False)),
            "draft": bool(str(getattr(editor, "text", "") or "").strip()),
            "background": self._away_recap_background(),
            "messages": list(self.ag.messages or ()),
            "last_recap_users": last.get("users"),
        }

    def _generate_away_recap(self, cancel=None):
        # 工具名单与主循环同源：刚 resume、本进程还没发过主请求时，旁路请求靠它
        # 拼出和下一次主请求相同的前缀。
        return self.ag.generate_away_recap(
            cancel=cancel, allowed_tools=self.allowed_tool_names())

    def _deliver_away_recap(self, text, raw):
        """后台线程的出口：只入队。写屏和写会话文件都不是线程安全的。"""
        with self._away_recap_lock:
            self._away_recap_ready.append((str(text), raw))

    def take_away_recap(self):
        """仅主线程：取走待显示的 recap 并记进会话。返回要显示的文字或 None。"""
        with self._away_recap_lock:
            ready, self._away_recap_ready = self._away_recap_ready, []
        if not ready:
            return None
        text, raw = ready[-1]                         # 同一段对话只留最新的一条
        if raw is not None:                           # None = 本来就是落盘的那条
            self.ag.record_away_recap(raw)
            try:
                self.save()
            except Exception:                         # noqa: BLE001
                pass
        return text

    def away_recap_turn(self, running):
        """一轮的起止。开始：取消定时器、中止在途生成、丢掉没来得及显示的那条
        （都已过时）；结束：重新按「离开多久」计时。"""
        self._turn_running = bool(running)
        controller = getattr(self, "away_recap_ctl", None)
        if running:
            lock = getattr(self, "_away_recap_lock", None)
            if lock is not None:
                with lock:
                    self._away_recap_ready.clear()
            if controller is not None:
                controller.turn_started()
        elif controller is not None:
            controller.turn_ended()

    # ---- 自动记忆抽取 ----------------------------------------------------
    # 为什么不靠主模型顺手写：2,925 条助手消息里 memory_write 调用 0 次（09-20 实测）。
    # 触发点是用户 09-21 定的：空闲 / 会话结束 + 压缩时，不是每轮。

    def memory_extract_turn(self, running):
        """一轮的起止。开始：中止在途抽取（上下文要变了）；结束：重新计时。"""
        if running:
            cancel = self._memory_extract_cancel
            if cancel is not None:
                cancel.set()
            self._memory_extract_due = None
            return
        self._memory_extract_due = time.monotonic() + MEMORY_EXTRACT.IDLE_SECONDS

    def _memory_extract_ready_to_run(self):
        """够不够条件抽一次：静置到点、没有别的事在跑、且确实有新东西。"""
        if not (self.memory_generate and self.memory_use):
            return False
        if self._turn_running or self._memory_extract_thread is not None:
            return False
        due = self._memory_extract_due
        if due is None or time.monotonic() < due:
            return False
        users = AWAY_RECAP.real_user_messages(self.ag.messages)
        if users < MEMORY_EXTRACT.MIN_USER_MESSAGES:
            return False
        summary = (getattr(self.ag, "context_summary", None) or {}).get(
            "covered_sha256")
        # 压缩过一段就一定抽一次：那段细节马上就只剩摘要了。
        compacted = summary is not None and summary != self._memory_extract_summary
        if not compacted and users - self._memory_extract_users < (
                MEMORY_EXTRACT.MIN_NEW_USER_MESSAGES):
            return False
        return True

    def memory_extract_tick(self):
        """仅主线程，空闲时调用：到点就起后台抽取；有结果就落盘并返回一行提示。"""
        if self._memory_extract_ready_to_run():
            self._memory_extract_due = None
            self._memory_extract_users = AWAY_RECAP.real_user_messages(
                self.ag.messages)
            self._memory_extract_summary = (
                getattr(self.ag, "context_summary", None) or {}).get(
                    "covered_sha256")
            cancel = client.CancellationHandle()
            self._memory_extract_cancel = cancel
            index = (getattr(self, "memory_index", {}) or {}).get("text") or ""
            thread = threading.Thread(
                target=self._memory_extract_background, args=(cancel, index),
                name="memory-extract", daemon=True)
            self._memory_extract_thread = thread
            thread.start()
        with self._memory_extract_lock:
            ready, self._memory_extract_ready = self._memory_extract_ready, []
        if not ready:
            return None
        saved = []
        for result in ready:
            saved.extend(self._store_extracted(result))
        if not saved:
            return None
        return saved

    def _memory_extract_background(self, cancel, index_text):
        """后台线程：只发请求、只入队。写盘和写屏都不是线程安全的。"""
        try:
            result = self.ag.extract_memories(
                index_text=index_text, cancel=cancel,
                allowed_tools=self.allowed_tool_names())
        except Exception as exc:                      # noqa: BLE001 - 抽取不能弄崩会话
            result = {"kind": "failed", "entries": [], "text": str(exc)}
        finally:
            self._memory_extract_thread = None
            self._memory_extract_cancel = None
        if result.get("kind") == "ok" and result.get("entries"):
            with self._memory_extract_lock:
                self._memory_extract_ready.append(result)

    def _store_extracted(self, result):
        """仅主线程：把抽出来的条目写进 project memory。

        永远只写 project 域——global 会跨项目注入未来所有会话，那必须由人逐次确认
        （现有规矩，见 tools._assess_memory_risk）。
        """
        saved = []
        for entry in result.get("entries") or ():
            stable_key = entry["stable_key"]
            target = str(entry.get("updates") or "")
            if target:
                try:
                    existing = self.memory_store.get(
                        target, cwd=self._session_cwd)
                except MEMORY.MemoryError:
                    existing = None
                # 只认「同样是自动抽出来的、且有稳定键」的条目：不去覆盖人写的记忆。
                if (existing and existing.get("stable_key")
                        and str(existing.get("kind") or "").startswith("auto")):
                    stable_key = existing["stable_key"]
            try:
                row = self.memory_store.add(
                    entry["content"], title=entry["name"],
                    scope=MEMORY.PROJECT, cwd=self._session_cwd,
                    source_session=self.ag.session_id,
                    evidence={
                        "writer": "auto_extract",
                        "model": result.get("model"),
                        "gateway": result.get("gateway"),
                    },
                    kind="auto_extract", stable_key=stable_key,
                    entry_type=entry["type"], description=entry["description"])
            except MEMORY.MemoryError as exc:
                self._memory_error = str(exc)
                continue
            saved.append(row)
        if saved:
            try:
                self.refresh_memory_context()
            except MEMORY.MemoryError:
                pass
            try:
                self.save()
            except Exception:                         # noqa: BLE001
                pass
        return saved

    def request_resume_recap(self):
        """resume 之后给一条 recap。上次那条之后没有新提问 → 直接用落盘的（零调用）；
        否则后台现写。启动时 --resume 那会儿 REPL 还没起来，先记下，起来后补。"""
        controller = getattr(self, "away_recap_ctl", None)
        if controller is None or getattr(self, "renderer", None) is None:
            self._resume_recap_pending = True
            return "pending"
        self._resume_recap_pending = False
        if not CFG.recap_policy(getattr(self, "cfg", None))["enabled"]:
            return None
        stored = getattr(self.ag, "away_recap", None) or {}
        users = AWAY_RECAP.real_user_messages(self.ag.messages)
        if stored.get("text") and stored.get("users") == users:
            self._deliver_away_recap(stored["text"], None)
            return "stored"
        controller.request()
        return "generating"

    def generate_recap_now(self):
        """/recap：后台线程写，主线程盯着 Esc / Ctrl+C。慢网关上一次要一两分钟
        （09-20 实测 glm-5.3@deepinfer 首字 128s），不能把 REPL 锁死在里面。"""
        return self._run_side_query("写 recap", self._generate_away_recap,
                                    {"kind": "failed", "text": ""})

    def extract_memories_now(self):
        """/memory capture：立刻抽一次跨会话记忆（与自动触发同一条通道）。"""
        index = (getattr(self, "memory_index", {}) or {}).get("text") or ""

        def work(cancel=None):
            return self.ag.extract_memories(
                index_text=index, cancel=cancel,
                allowed_tools=self.allowed_tool_names())

        result = self._run_side_query("抽记忆", work,
                                      {"kind": "failed", "entries": []})
        if result.get("kind") != "ok":
            return result, []
        return result, self._store_extracted(result)

    def _run_side_query(self, label, work, failure):
        """旁路请求的统一外壳：后台线程发，主线程盯着 Esc / Ctrl+C。"""
        pump = getattr(self, "pump", None)
        renderer = getattr(self, "renderer", None)
        if pump is None or renderer is None:
            with Spinner(label):
                return work()
        cancel = client.CancellationHandle()
        box = {}

        def run():
            try:
                box["result"] = work(cancel)
            except Exception as exc:                  # noqa: BLE001
                box["result"] = dict(
                    failure, text=f"{type(exc).__name__}: {exc}")

        worker = threading.Thread(target=run, name="side-query", daemon=True)
        started = time.monotonic()
        shown = None

        def show_wait():
            nonlocal shown
            elapsed = int(time.monotonic() - started)
            if elapsed != shown:
                shown = elapsed
                renderer.render(self.refresh_composer(
                    busy=True, prompt=self.composer_prompt(busy=True),
                    activity=f"{label} {elapsed}s · Esc 取消", publish=False))

        show_wait()                                   # 先说在干什么，再发请求
        worker.start()
        try:
            while worker.is_alive():
                show_wait()
                event = pump.get(0.2)
                if event is None:
                    self.heartbeat_lease()
                elif event.kind == "redraw":
                    renderer.render(event.snapshot)
                elif event.kind in ("cancel", "interrupt"):
                    cancel.cancel()
                elif event.kind == "error":
                    raise event.value
                else:
                    if event.kind == "eof":
                        cancel.cancel()               # 退出优先，别让人等旁路请求
                    self._deferred_pump_events.append(event)
        finally:
            if worker.is_alive():                     # 只有异常路径会走到这
                cancel.cancel()
            renderer.clear_input()
        worker.join(timeout=5)
        if cancel.is_set():
            return dict(failure, kind="aborted")
        return box.get("result") or dict(failure)

    # ---------------------------------------------------------- 模型目录自适应
    def maybe_refresh_catalog(self):
        """turn 边界上顺带判断目录是否过期 —— 不是定时器，是「要用了才看」。

        为什么不用定时线程：会话可能开着好几天（那个压缩坏掉的会话开了 12 天），
        只在启动时刷一次等于没刷；但定时器会在没人用的时候空转，还得处理睡眠、
        暂停、时钟跳变。turn 边界既是「马上要用模型」的时刻，也天然低频。
        这里再加一道内存节流，避免每次 turn 都去读 90 KB 的 models.json。
        """
        # 这是个可选功能：它绝不能让一个 turn 起不来。
        now = time.monotonic()
        last = float(getattr(self, "_catalog_checked_at", 0.0) or 0.0)
        try:
            # `last == 0` 是「还没查过」（也是 /model 切网关后清节流用的值），
            # **不能**拿它当一个时间戳去减。`time.monotonic()` 在 Linux 上是
            # 开机以来的秒数：机器刚起来不到 5 分钟时 `now - 0 < 300`，于是
            # 第一次刷新被静默跳过。维护者的机器开了 50 天，这个分支永远走不到；
            # 2026-09-21 公开仓库的第一次 CI（全新 VM）当场红了。
            if last and now - last < CATALOG_CHECK_SECONDS:
                return None
            self._catalog_checked_at = now
            return self.start_catalog_refresh()
        except Exception:                             # noqa: BLE001
            return None

    def start_catalog_refresh(self, *, force=False):
        """目录过期就在后台抓一次。平台名单每周都在变，而且是双向变化：
        下架的会回来、在册的会消失。以前只有人手动跑 /model refresh 才更新，
        缓存放了 18 天没人碰 —— 席位于是一直挑中一个已经 403 的 id。

        只发一次 HTTP，不做真实推理；线程失败一律吞掉，绝不影响会话。
        """
        policy = CFG.catalog_policy(getattr(self, "cfg", None))
        if not force and not policy["enabled"]:
            return None
        thread = getattr(self, "_catalog_thread", None)
        if thread is not None and thread.is_alive():
            return None
        if not hasattr(self, "_catalog_notices"):
            self._catalog_notices = []
        _, gateway = self.route_selection()
        if not force and not M.catalog_is_stale(
                gateway, policy["ttl_seconds"]):
            return None

        def work():
            try:
                result = M.refresh_catalog(gateway)
            except Exception:                         # noqa: BLE001
                return                                # 网关不通不是会话的事
            notice = _catalog_change_notice(result)
            if notice:
                self._catalog_notices.append(notice)

        thread = threading.Thread(
            target=work, name="catalog-refresh", daemon=True)
        self._catalog_thread = thread
        thread.start()
        return thread

    def drain_catalog_events(self):
        notices = getattr(self, "_catalog_notices", None) or []
        self._catalog_notices = []
        return list(notices)

    def _agent_tokens(self, record):
        """子代理的 token 数：优先记录里的 transcript，再问 workspace；拿不到就 None。"""
        transcript = (record or {}).get("transcript")
        if not isinstance(transcript, dict):
            getter = getattr(getattr(self, "agent_workspace", None), "transcript", None)
            try:
                transcript = getter(str(record.get("id"))) if callable(getter) else None
            except Exception:                           # noqa: BLE001
                transcript = None
        if not isinstance(transcript, dict):
            return None
        total = int(transcript.get("tokens_in") or 0) + int(transcript.get("tokens_out") or 0)
        return total or (int(transcript.get("last_context_tokens") or 0) or None)

    def _agent_event_from_payload(self, kind, record):
        run_id = str((record or {}).get("id") or "")
        return AGENT_EVENTS.from_agent_record(
            kind, record, tokens=self._agent_tokens(record),
            activity=self._agent_activity.get(run_id, ""))

    def _remember_agent_event(self, event_obj):
        log = getattr(self, "_agent_event_log", None)
        if log is None:
            self._agent_event_log = log = []
        log.append(event_obj)
        del log[:-200]

    def _count_active_agents(self, *, max_age=0.5):
        """活跃子代理数；0.5 s 节流——进度事件很密，不必每次都读 workspace。"""
        now = time.monotonic()
        stamp = getattr(self, "_active_agent_count_at", 0.0)
        if now - stamp < max_age and hasattr(self, "_active_agent_count"):
            return int(self._active_agent_count or 0)
        try:
            rows = self.list_agents(limit=20)
        except Exception:                               # noqa: BLE001
            rows = []
        count = sum(
            1 for row in rows
            if str(row.get("state") or "") not in AGENTS.TERMINAL_STATES)
        self._active_agent_count = count
        self._active_agent_count_at = now
        return count

    def _tool_activity_label(self, tool_name, suffix=None):
        """工具阶段的活动标签：有活跃子代理时是等待行（✻ 等待 N 个后台代理完成），
        否则 "<tool> 运行中" 或调用方给的后缀（task id、输出量）。"""
        count = self._count_active_agents()
        waiting = AGENT_EVENTS.waiting_line(count)
        if waiting:
            return waiting
        return f"{tool_name} {suffix}" if suffix else f"{tool_name} 运行中"

    def drain_agent_events(self):
        """主线程消费 workspace 事件，并返回适合输出的轻量通知。"""
        notices = []
        self._agent_projection_changed = False
        for event in self.agent_workspace.drain():
            kind = str(event.get("kind") or "agent_workspace_event")
            payload = dict(event.get("payload") or {})
            parent = str(payload.get("parent_session_id") or "")
            if parent and parent != str(self.ag.session_id):
                continue
            run_id = str(payload.get("id") or "")
            projected = payload
            if run_id:
                projected = self._cache_agent(payload)
                self._agent_cache_at.pop(run_id, None)
                if kind != "agent_activity":
                    self._agent_projection_changed = True
            runtime_kind = str((projected or {}).get("kind") or "")
            if kind == "agent_activity" and run_id:
                self._agent_activity[run_id] = _agent_activity_text(payload)
            if runtime_kind == CONSULTS.KIND:
                self._consult_current = dict(projected)
            if (self.controller is not None
                    and kind != "agent_activity"):
                self.controller.record_event(
                    kind, payload,
                    turn_id=payload.get("parent_turn_id") or None)
            if (kind == "agent_result_received"
                    and runtime_kind.startswith("workflow-")):
                continue
            state = str(payload.get("state") or "")
            short_id = run_id[:12] or "?"
            if kind == "agent_result_received":
                if runtime_kind == CONSULTS.KIND:
                    source = (projected or {}).get("source_snapshot") or {}
                    source_title = str(source.get("title") or "(无标题)")
                    source_id = str(source.get("session_id") or "?")
                    outcome = state or "completed"
                    lease_state = str(
                        source.get("source_lease_state") or "inactive")
                    if lease_state == "live":
                        live_note = " · source live at capture"
                    elif lease_state == "unknown":
                        live_note = " · source freshness unknown"
                    else:
                        live_note = ""
                    result = AGENTS.project_report(
                        payload.get("result"))
                    if len(result) > 4000:
                        result = (
                            result[:4000]
                            + "\n…[/consult open 查看完整 transcript]")
                    notices.append(
                        f"  [consult {short_id} ← {source_title} "
                        f"({source_id}) · {outcome}{live_note}]\r\n"
                        + (
                            result.replace("\n", "\r\n") + "\r\n"
                            if result else ""))
                    continue
                # PLAN-agent-visibility J1/J2：子代理正文不进父时间线，只落一条永久
                # 完成行（label · 动词 · 耗时 · token）和一行指向 /agents peek 的续行；
                # 报告本身已作为工具结果回给主模型，由它转述。
                event_obj = self._agent_event_from_payload(
                    "failed" if state == "failed" else "finished",
                    projected or payload)
                self._remember_agent_event(event_obj)
                self._agent_activity.pop(run_id, None)
                notices.append(
                    AGENT_EVENTS.finished_line(event_obj) + "\r\n"
                    + DIM(AGENT_EVENTS.finished_hint(event_obj, run_id)) + "\r\n")
            elif kind == "agent_workspace_error":
                notices.append(
                    f"  [agent {short_id} workspace error] "
                    f"{payload.get('error', 'unknown error')}\r\n")
        return notices


    def _composer_queue(self, limit=5):
        if self.attached_agent_id:
            try:
                record = self.agent_record()
                items = [
                    item for item in record.get("inbox") or []
                    if item.get("state") == "queued"]
            except AGENTS.AgentRuntimeError:
                items = []
            lines = []
            if len(items) > limit:
                lines.append(
                    f"… {len(items) - limit} earlier child messages")
                items = items[-limit:]
            for item in items:
                text = " ".join(
                    str(item.get("text") or "").splitlines())
                lines.append(f"→ agent  {text}")
            return tuple(lines)
        items = list(
            getattr(getattr(self, "controller", None), "queued", ()))
        labels = {
            C.QueueMode.STEER: "↪ steer",
            C.QueueMode.NEXT_TURN: "＋ next",
            C.QueueMode.COMMAND: "/ command",
            C.QueueMode.SHELL: "! shell",
        }
        lines = []
        if len(items) > limit:
            lines.append(f"… {len(items) - limit} earlier queued")
            items = items[-limit:]
        for item in items:
            is_goal = getattr(item, "origin", "user") == "goal"
            if is_goal:
                metadata = (
                    item.metadata if isinstance(item.metadata, dict) else {})
                round_number = metadata.get("round", "?")
                goal = getattr(self, "goal", None)
                maximum = (
                    goal.get("max_rounds", "?")
                    if isinstance(goal, dict) else "?")
                state = (
                    "resume required"
                    if getattr(self, "_goal_activation", "disarmed")
                    != "armed" else "waiting")
                display = self._queue_display.get(
                    item.id, f"round {round_number}/{maximum} · {state}")
            else:
                display = self._queue_display.get(item.id, item.text)
            text = " ".join(str(display).splitlines()).strip()
            label = "↻ goal" if is_goal else labels[item.mode]
            lines.append(f"{label}  {text}")
        return tuple(lines)

    def _chat_history_if_changed(self):
        """当前 chat 换了（resume / new / clear / fork / 回滚）就返回它的输入历史。

        惰性同步，而不是往各条切换路径里挂钩子：那些都是带回滚分支的事务性
        长函数，逐个挂既容易漏、又可能干扰回滚。`ag.session_id` 是唯一事实源，
        每次合成 composer 帧前比对一次即可；没变时只是一次字符串比较。
        输入目标正指向子代理时先不动（那张历史表属于子代理），回到主输入再同步。
        """
        ag = getattr(self, "ag", None)
        current = str(getattr(ag, "session_id", "") or "")
        if not current or current == getattr(self, "_input_history_session", None):
            return None
        editor = getattr(self.pump, "editor", None)
        if getattr(editor, "target", None) is not None:
            return None
        self._input_history_session = current
        return _history(
            session_id=current, messages=getattr(ag, "messages", None))

    def refresh_composer(self, *, busy=None, prompt=None, activity=None,
                         publish=True):
        """把会话属性、queue 与 editor 合成一帧；不直接写 stdout。"""
        if self.pump is None:
            return None
        # Resolve the attached thread's full inbox before list_agents() adds
        # lightweight summaries to the cache; otherwise a just-queued direct
        # message can disappear from one composer frame.
        queued = self._composer_queue()
        dock = _composer_dock_items(self)
        # Polling is throttled internally; this catches state created by a
        # background workflow/agent even when no lifecycle notice is pending.
        # It intentionally runs *after* _composer_queue(): list_agents() uses
        # lightweight summaries and must not overwrite the full inbox snapshot
        # needed to display a just-queued child message.
        self.refresh_command_guidance(publish=False)
        if (self.renderer is not None
                and hasattr(self.renderer, "set_actionable_dock")):
            self.renderer.set_actionable_dock(any(
                isinstance(item, tui.DockItem) and item.event
                for item in dock))
        kwargs = {
            "queued": queued,
            "dock": dock,
            "status": status_line(self),
        }
        chat_history = self._chat_history_if_changed()
        if chat_history is not None:
            # configure() 在持锁状态下整表替换**当前目标**的历史，不切换目标。
            kwargs["target_history"] = chat_history
        if busy is not None:
            kwargs["busy"] = busy
        if prompt is not None:
            kwargs["prompt"] = prompt
        if activity is not None:
            kwargs["activity"] = activity
        kwargs["publish"] = publish
        return self.pump.configure(**kwargs)
    def pick(self, rows, *, title="", render=str, page=12,
             allow_filter=True, actions=None, controls="",
             return_action=False, return_state=False, fullscreen=False,
             mouse=False, mouse_action=None, refresh=None,
             refresh_interval=1.0, **picker_options):
        """``refresh``：可选的取行函数，picker 开着时每 ``refresh_interval`` 秒调一次，
        行变了就原地替换（光标按 row_key 跟随）——看着 DAG 跑完，不必关了再开。"""
        if self.pump is None:
            selected = tui.select(
                rows, title=title, render=render, page=page,
                allow_filter=allow_filter)
            if return_state:
                return None, selected, {}
            return (None, selected) if return_action else selected
        self.renderer.clear_input()
        temporary_mouse_enabled = False
        if mouse and not self.renderer.mouse_enabled:
            temporary_mouse_enabled = self.renderer.enable_mouse(force=True)
        try:
            self.pump.open_picker(
                rows, title=title, render=render, page=page,
                allow_filter=allow_filter, actions=actions,
                controls=controls, fullscreen=fullscreen,
                mouse_action=mouse_action, **picker_options)
            last_refresh = time.monotonic()
            signature = _rows_signature(rows) if refresh is not None else None
            while True:
                event = self.pump.get(0.2)
                if event is None:
                    self.heartbeat_lease()
                    if (refresh is not None and time.monotonic() - last_refresh
                            >= max(0.0, float(refresh_interval))):
                        last_refresh = time.monotonic()
                        try:
                            fresh = list(refresh())
                        except Exception:               # noqa: BLE001
                            fresh = None
                        if fresh is not None:
                            digest = _rows_signature(fresh)
                            if digest != signature:
                                signature = digest
                                self.pump.replace_picker_rows(fresh)
                    continue
                if event.kind == "redraw":
                    self.renderer.render(event.snapshot)
                elif event.kind == "picker_result":
                    self.renderer.clear_input()
                    if return_state:
                        return event.mode, event.value, dict(event.state or {})
                    if return_action:
                        return event.mode, event.value
                    return event.value
                elif event.kind == "error":
                    raise event.value
                else:
                    self._deferred_pump_events.append(event)
        finally:
            # Picker errors and interrupts must restore the base terminal too;
            # the normal picker_result path already does this, and the second
            # call is intentionally idempotent.  In the fixed hybrid mode the
            # mouse reporting is session-scoped, so a picker must not turn it
            # off after returning to the composer.  The legacy startup-native
            # escape hatch keeps its old temporary behavior.
            self.renderer.clear_input()
            if (temporary_mouse_enabled
                    and getattr(self.renderer, "_mouse_preference", "hybrid")
                    == "native"):
                self.renderer.disable_mouse()

    def prompt_text(self, *, prompt, initial="", title="", hint=""):
        """Read an isolated modal draft without disturbing the main composer."""
        if self.pump is None:
            try:
                value = input(prompt)
            except (EOFError, KeyboardInterrupt):
                return None
            return value
        self.renderer.clear_input()
        self.pump.open_prompt(
            prompt=prompt, initial=initial, title=title, hint=hint)
        while True:
            event = self.pump.get(0.2)
            if event is None:
                self.heartbeat_lease()
                continue
            if event.kind == "redraw":
                self.renderer.render(event.snapshot)
            elif event.kind == "prompt_result":
                self.renderer.clear_input()
                return event.value
            elif event.kind == "error":
                raise event.value
            else:
                self._deferred_pump_events.append(event)

    def _drain_pump_events(self):
        events = list(self._deferred_pump_events)
        self._deferred_pump_events.clear()
        if self.pump is not None:
            events.extend(self.pump.drain())
        return events
    def _get_pump_event(self, timeout=None):
        if self._deferred_pump_events:
            return self._deferred_pump_events.pop(0)
        return self.pump.get(timeout)



    def _authorize_network(self, name, args, result):
        """Bind host-network access to one prepared Bash call."""
        result = dict(result)
        if not result.get("allowed") or name != "bash":
            return result
        if not getattr(args, "requires_network_confirmation", False):
            result["prepared"] = args
            return result
        if not sys.stdin.isatty():
            return {
                "allowed": False, "decision": "noninteractive_network_deny",
                "source": "sandbox_policy",
            }

        if self.renderer:
            self.renderer.clear_input()
        print()
        print(YELLOW("  Bash 请求访问主机网络"))
        print(DIM("  文件系统 sandbox 与受保护路径只读边界仍然保留。"))
        options = [
            ("once", "仅这一次允许联网"),
            ("deny", "保持 NET ISOLATED 并取消（推荐）"),
        ]
        picked = self.pick(
            options, page=2, allow_filter=False,
            render=lambda option: option[1])
        if not picked or picked[0] != "once":
            print(DIM("  ✗ 已取消联网 Bash"))
            return {
                "allowed": False, "decision": "network_denied",
                "source": "user",
            }
        try:
            approved = args.approve_network()
        except Exception as exc:
            print(RED(f"  ✗ 无法绑定 network approval：{exc}"))
            return {
                "allowed": False, "decision": "network_approval_failed",
                "source": "sandbox_policy",
            }
        result["prepared"] = approved
        result["sandbox"] = approved.sandbox_evidence()
        result["decision"] = str(result.get("decision") or "once") + \
            "+network_once"
        print(YELLOW("  ! 本次 Bash 将以 NET OPEN 运行"))
        return result

    def _authorize_unsandboxed(self, name, args, result):
        """Require a separate per-call consent when Bash cannot be sandboxed."""
        result = self._authorize_network(name, args, result)
        if not result.get("allowed"):
            return result
        args = result.get("prepared", args)
        result = dict(result)
        if name in tools.PROVIDER_SPAWNING:
            return self._authorize_provider_transport(
                name, args, result)
        if not result.get("allowed") or name != "bash":
            return result
        if not getattr(args, "requires_unsandboxed_confirmation", False):
            result["prepared"] = args
            evidence = getattr(args, "sandbox_evidence", lambda: None)()
            if evidence is not None:
                result["sandbox"] = evidence
            return result
        if not sys.stdin.isatty():
            return {
                "allowed": False,
                "decision": "noninteractive_unsandboxed_deny",
                "source": "sandbox_policy",
            }

        # 会话级放行：本次会话内已选过 "always" 的，直接绑定 approval，不再弹窗。
        # 仍调用 approve_unsandboxed() 把确认绑到本次 prepared args（沙箱证据、
        # protected 路径硬守卫照常生效），只是省掉交互。
        if getattr(self, "_unsandboxed_session_approved", False):
            try:
                approved = args.approve_unsandboxed()
            except Exception as exc:
                print(RED(f"  ✗ 无法绑定 unsandboxed approval：{exc}"))
                return {
                    "allowed": False,
                    "decision": "unsandboxed_approval_failed",
                    "source": "sandbox_policy",
                }
            result["prepared"] = approved
            result["sandbox"] = approved.sandbox_evidence()
            result["decision"] = str(result.get("decision") or "once") + \
                "+unsandboxed_always"
            return result

        evidence = args.sandbox_evidence() or {}
        reason = str(evidence.get("reason") or "sandbox runtime 不可用")
        if self.renderer:
            self.renderer.clear_input()
        print()
        print(RED("  UNSANDBOXED Bash 需要单独确认"))
        print(YELLOW(f"  {reason}"))
        print(DIM(
            "  受保护路径硬守卫仍会执行，但复杂 shell 不再有 OS 只读边界。"))
        options = [
            ("once", "仅这一次以 UNSANDBOXED 运行"),
            ("always", "本次会话内 UNSANDBOXED 都允许"),
            ("deny", "取消（推荐）"),
        ]
        picked = self.pick(
            options, page=3, allow_filter=False,
            render=lambda option: option[1])
        choice = picked[0] if picked else "deny"
        if choice == "deny":
            print(DIM("  ✗ 已取消 UNSANDBOXED Bash"))
            return {
                "allowed": False,
                "decision": "unsandboxed_denied",
                "source": "user",
            }
        try:
            approved = args.approve_unsandboxed()
        except Exception as exc:
            print(RED(f"  ✗ 无法绑定 unsandboxed approval：{exc}"))
            return {
                "allowed": False,
                "decision": "unsandboxed_approval_failed",
                "source": "sandbox_policy",
            }
        result["prepared"] = approved
        result["sandbox"] = approved.sandbox_evidence()
        if choice == "always":
            self._unsandboxed_session_approved = True
            result["decision"] = str(result.get("decision") or "once") + \
                "+unsandboxed_always"
            print(YELLOW("  ✓ 本次会话内 UNSANDBOXED Bash 不再逐条询问"
                         "（protected 路径硬守卫仍生效）"))
        else:
            result["decision"] = str(result.get("decision") or "once") + \
                "+unsandboxed_once"
            print(YELLOW("  ! 本次将明确以 UNSANDBOXED 运行"))
        return result

    def _authorize_memory_risk(self, name, args):
        """Require one explicit, non-sticky consent for risky memory changes."""
        policy_name = _memory_hard_policy_name(name, args)
        if policy_name is None:
            return None
        if not sys.stdin.isatty():
            return {
                "allowed": False,
                "decision": "noninteractive_memory_risk_deny",
                "source": "hard_guard",
            }
        if getattr(args, "memory_risk_approved", False):
            return None
        if not getattr(args, "requires_memory_confirmation", False):
            return {
                "allowed": False,
                "decision": "memory_risk_invalid_preparation",
                "source": "hard_guard",
            }
        risk = getattr(args, "memory_risk", None)
        if (risk is None
                or getattr(risk, "permission_name", None) != policy_name):
            return {
                "allowed": False,
                "decision": "memory_risk_mismatch",
                "source": "hard_guard",
            }

        if self.renderer:
            self.renderer.clear_input()
        print()
        print(RED("  高风险 memory 变更需要单独确认"))
        print(YELLOW(
            "  这项确认不会被 /auto、--yolo、allow 或本会话授权绕过。"))
        call_preview = tui.sanitize_terminal_text(preview(name, args))
        _print_wrapped(" ".join(call_preview.splitlines()), first_prefix=f"{name}: ")
        for raw in risk.detail_lines():
            _print_wrapped(tui.sanitize_terminal_text(str(raw)))

        options = [
            ("deny", "拒绝（推荐）"),
            ("once", "我了解上述影响，仅批准这一次"),
        ]
        if self.pump is not None:
            picked = self.pick(
                options, page=2, allow_filter=False,
                render=lambda option: option[1])
            choice = picked[0] if picked else "deny"
        elif tui.supported():
            watcher = self.watcher
            if watcher:
                watcher.pause()
            try:
                picked = tui.select(
                    options, title="", page=2, allow_filter=False,
                    render=lambda option: option[1])
            finally:
                if watcher:
                    watcher.resume()
            choice = picked[0] if picked else "deny"
        else:
            try:
                value = input(DIM(
                    "  输入 execute 才执行；直接回车拒绝 > ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                value = ""
            choice = "once" if value == "execute" else "deny"
        if choice != "once":
            print(DIM("  ✗ 已拒绝高风险 memory 变更"))
            return {
                "allowed": False,
                "decision": "memory_risk_denied",
                "source": "user",
            }
        try:
            approved = args.approve_memory_risk()
        except Exception as exc:
            print(RED(f"  ✗ 无法绑定 memory 批准：{exc}"))
            return {
                "allowed": False,
                "decision": "memory_risk_approval_failed",
                "source": "hard_guard",
            }
        print(RED("  ! 已批准本次高风险 memory 变更"))
        return self._authorize_unsandboxed(name, approved, {
            "allowed": True,
            "decision": "memory_risk_once",
            "source": "user",
            "prepared": approved,
        })

    def _authorize_workspace_risk(self, name, args):
        """Require one explicit, non-sticky consent for destructive work."""
        if not getattr(
                args, "requires_workspace_confirmation", False):
            return None
        if name not in {"bash", "write_file", "edit_file"}:
            return {
                "allowed": False,
                "decision": "workspace_risk_invalid_tool",
                "source": "hard_guard",
            }
        if not sys.stdin.isatty():
            return {
                "allowed": False,
                "decision": "noninteractive_workspace_risk_deny",
                "source": "hard_guard",
            }

        if self.renderer:
            self.renderer.clear_input()
        print()
        print(RED("  高风险工作区操作需要单独确认"))
        print(YELLOW(
            "  这项确认不会被 /auto、allow 或本会话授权绕过。"))
        if name == "bash":
            command = tui.sanitize_terminal_text(str(
                args.get("command", "")))
            _print_wrapped(" ".join(command.splitlines()), first_prefix="command: ")
        else:
            _print_wrapped(
                tui.sanitize_terminal_text(str(args.get("path", ""))),
                first_prefix=f"{name}: ")
        risk = getattr(args, "workspace_risk", None)
        detail_lines = (
            risk.detail_lines()
            if risk is not None and hasattr(risk, "detail_lines")
            else ["risk evidence unavailable"])
        for raw in detail_lines:
            _print_wrapped(tui.sanitize_terminal_text(str(raw)))

        options = [
            ("deny", "拒绝（推荐）"),
            ("once", "我了解上述影响，仅这一次执行"),
        ]
        if self.pump is not None:
            picked = self.pick(
                options, page=2, allow_filter=False,
                render=lambda option: option[1])
            choice = picked[0] if picked else "deny"
        elif tui.supported():
            watcher = self.watcher
            if watcher:
                watcher.pause()
            try:
                picked = tui.select(
                    options, title="", page=2, allow_filter=False,
                    render=lambda option: option[1])
            finally:
                if watcher:
                    watcher.resume()
            choice = picked[0] if picked else "deny"
        else:
            try:
                value = input(DIM(
                    "  输入 execute 才执行；直接回车拒绝 > ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                value = ""
            choice = "once" if value == "execute" else "deny"
        if choice != "once":
            print(DIM("  ✗ 已拒绝高风险工作区操作"))
            return {
                "allowed": False,
                "decision": "workspace_risk_denied",
                "source": "user",
            }
        try:
            approved = args.approve_workspace_risk()
        except Exception as exc:
            print(RED(f"  ✗ 无法绑定高风险操作批准：{exc}"))
            return {
                "allowed": False,
                "decision": "workspace_risk_approval_failed",
                "source": "hard_guard",
            }
        print(RED("  ! 已批准本次高风险操作"))
        return self._authorize_unsandboxed(name, approved, {
            "allowed": True,
            "decision": "workspace_risk_once",
            "source": "user",
            "prepared": approved,
        })

    def permission_decision(self, name, args):
        policy_name, perm = _configured_permission(
            self.cfg, name, args)
        if perm == "deny":
            return {
                "allowed": False, "decision": "deny",
                "source": "config",
            }
        memory_decision = self._authorize_memory_risk(name, args)
        if memory_decision is not None:
            return memory_decision
        workspace_decision = self._authorize_workspace_risk(name, args)
        if workspace_decision is not None:
            return workspace_decision
        if perm == "allow":
            return self._authorize_unsandboxed(name, args, {
                "allowed": True, "decision": "allow",
                "source": "config",
            })
        if self._accept_edits_applies(name):
            # 与 auto 同一位置：memory / workspace 风险与硬守卫都已在上面判过。
            return self._authorize_unsandboxed(name, args, {
                "allowed": True, "decision": "accept_edits",
                "source": "mode",
            })
        if self.auto:
            return self._authorize_unsandboxed(name, args, {
                "allowed": True, "decision": "auto_approve",
                "source": "session",
            })
        if policy_name in self.always:
            return self._authorize_unsandboxed(name, args, {
                "allowed": True, "decision": "always",
                "source": "session_grant",
            })
        if not sys.stdin.isatty():
            return {
                "allowed": False, "decision": "noninteractive_deny",
                "source": "safety_default",
            }

        if self.renderer:
            self.renderer.clear_input()
        print()
        print(
            f"  {YELLOW('需要确认')}  {BOLD(policy_name)}  "
            f"{preview(name, args)}")
        _print_approval_details(name, args)

        options = [
            ("once", "允许这一次"),
            ("always", f"本次会话都允许 {policy_name}"),
            ("deny", "拒绝"),
        ]
        picked = self.pick(
            options, page=3, allow_filter=False,
            render=lambda option: option[1])
        choice = picked[0] if picked else "deny"
        if choice == "always":
            if not self._remember_session_grant(policy_name):
                return {
                    "allowed": False,
                    "decision": "grant_persistence_failed",
                    "source": "safety_default",
                }
            print(DIM(f"  ✓ 本次会话内 {policy_name} 不再询问"))
            return self._authorize_unsandboxed(name, args, {
                "allowed": True, "decision": "always",
                "source": "user",
            })
        if choice == "deny":
            print(DIM("  ✗ 已拒绝"))
            return {
                "allowed": False, "decision": "deny",
                "source": "user",
            }
        return self._authorize_unsandboxed(name, args, {
            "allowed": True, "decision": "once",
            "source": "user",
        })

    @staticmethod
    def _decision_gate_unattended(options, *, status="unattended", reason=""):
        candidates = [
            tui.sanitize_terminal_text(
                str(item.get("label") or "").strip())[:160]
            for item in (options or ())
            if isinstance(item, dict) and item.get("recommended")
        ]
        # A malformed request with multiple recommendations must not acquire
        # a seemingly authoritative suggestion.  It remains an explicit
        # unattended/invalid outcome for the model to report.
        recommended = candidates[0] if len(candidates) == 1 else ""
        result = {
            "choice": "",
            "notes": "",
            "attended": False,
            "status": str(status),
        }
        if recommended:
            result["suggested_choice"] = recommended
        if reason:
            result["reason"] = tui.sanitize_terminal_text(
                str(reason))[:512]
        return result

    @staticmethod
    def _decision_candidate_notice(candidate):
        """Build a short, argument-free notice for the terminal owner.

        Candidate events intentionally omit model arguments (paths, prompts,
        and other sensitive data).  The UI can still explain why the batch was
        held by using the bounded tool/kind summaries emitted by the detector.
        """
        rows = []
        for item in (candidate or {}).get("reasons") or ():
            if not isinstance(item, dict):
                continue
            name = tui.sanitize_terminal_text(
                str(item.get("tool") or "tool"))[:48]
            summary = tui.sanitize_terminal_text(
                str(item.get("summary") or "高影响 provider 操作"))[:180]
            rows.append(f"{name}: {summary}")
        detail = "；".join(rows) or "本批次包含需要用户拍板的 provider fan-out"
        return (
            "  ⚑ 风险预检已暂停本批次：" + detail
            + "。主 agent 需先单独调用 decision_gate；未收到用户明确回答前不会启动。"
        )

    def _decision_gate_owner(self, payload):
        """Resolve a gate on the renderer owner thread only.

        This method is intentionally separate from ``t_decision_gate``.  The
        latter may run in a TaskManager worker and can only enqueue a request;
        this method is the sole place allowed to read the pump or paint UI.
        """
        payload = payload if isinstance(payload, dict) else {}
        if str(payload.get("_interaction_role", "blocked") or "blocked").strip().lower() != "main":
            return self._decision_gate_unattended(
                payload.get("options") or (), status="unattended",
                reason="只有当前窗口的主 agent 可以触发 decision_gate")
        raw_options = payload.get("options")
        try:
            normalized_request, _labels = (
                tools.normalize_decision_gate_request(
                    payload.get("question"), raw_options,
                    payload.get("context"),
                    questions=payload.get("questions")))
        except (TypeError, ValueError) as exc:
            # A replayed/corrupt event must never reach a picker with a
            # silently repaired option list.  Return an explicit failure so
            # the worker and audit can stop without a guessed choice.
            options = [
                item for item in raw_options if isinstance(item, dict)
            ] if isinstance(raw_options, (list, tuple)) else []
            return self._decision_gate_unattended(
                options, status="invalid",
                reason=f"decision gate 请求无效：{exc}")
        options = normalized_request["options"]
        question = normalized_request["question"]
        context = normalized_request["context"]
        questions = normalized_request.get("questions") or [{
            "id": "decision", "question": question,
            "options": options, "multi_select": False,
        }]
        owner = getattr(self, "_ui_owner_thread_id", None)
        if owner is not None and threading.get_ident() != owner:
            return self._decision_gate_unattended(
                options, status="invalid",
                reason="decision gate 必须由 terminal owner 解析")
        pump = getattr(self, "pump", None)
        stream = getattr(pump, "stream", None) if pump is not None else None
        try:
            interactive = bool(
                stream.isatty() if stream is not None else sys.stdin.isatty())
        except (AttributeError, OSError, ValueError):
            interactive = False
        if not interactive or pump is None:
            return self._decision_gate_unattended(
                options, reason="当前运行环境没有可交互 terminal")
        renderer = getattr(self, "renderer", None)
        if renderer is None:
            return self._decision_gate_unattended(
                options, status="persistence_error",
                reason="当前 session 没有 owner renderer")

        rows = []
        for index, option in enumerate(options):
            mark = " ★" if option.get("recommended") else ""
            detail = option.get("detail") or ""
            cost = option.get("cost") or ""
            suffix = " · ".join(value for value in (detail, cost) if value)
            label = f"{option['label']}{mark}"
            rows.append((str(index + 1), label, option, suffix))

        watcher = getattr(self, "watcher", None)
        if watcher:
            watcher.pause()
        try:
            chosen = None
            # The dedicated modal keeps question, context, selected
            # consequence and cost visible in one owner-rendered frame.
            if hasattr(pump, "open_decision_gate"):
                clear_input = getattr(renderer, "clear_input", None)
                if callable(clear_input):
                    clear_input()
                pump.open_decision_gate(
                    question=question, options=options, context=context,
                    questions=questions)
                while True:
                    if self._interaction_worker_is_gone():
                        return self._decision_gate_unattended(
                            options, status="cancelled",
                            reason="worker 已取消或 interaction 已过期")
                    event = pump.get(0.2)
                    if event is None:
                        self.heartbeat_lease()
                        continue
                    if event.kind == "redraw":
                        renderer.render(event.snapshot)
                    elif event.kind == "decision_gate_result":
                        chosen = event.value
                        break
                    elif event.kind == "error":
                        raise event.value
                    else:
                        self._deferred_pump_events.append(event)
            elif tui.supported():
                # Compatibility path for a test/frozen old pump.  Include the
                # consequence in the row instead of silently dropping it.
                picked = self.pick(
                    rows, page=4, allow_filter=False,
                    render=lambda row: f"{row[1]} · {row[3]}" if row[3]
                    else row[1])
                chosen = picked[2] if picked else None
            else:
                for index, (_number, label, _option, suffix) in enumerate(rows):
                    text = f"{label} · {suffix}" if suffix else label
                    print(f"  {index + 1}. {text}")
                while True:
                    raw = input(
                        "  选择 (1-{0}, 回车=推荐): ".format(len(rows))).strip()
                    recommended = next(
                        (item for item in options if item.get("recommended")),
                        options[0] if options else None)
                    if not raw and recommended:
                        chosen = recommended
                        break
                    if raw.isdigit() and 1 <= int(raw) <= len(rows):
                        chosen = options[int(raw) - 1]
                        break
            if not chosen:
                return {
                    "choice": "", "notes": "", "attended": True,
                    "status": "cancelled", "cancelled": True,
                    "reason": "用户取消了 decision gate",
                }
            notes = ""
            if pump is not None and callable(getattr(self, "prompt_text", None)):
                try:
                    notes = self.prompt_text(
                        prompt="备注（回车跳过）",
                        title="decision gate notes",
                        hint="Enter 保存 · Esc 跳过")
                    notes = str(notes or "").strip()
                except Exception:  # noqa: BLE001 - notes 是可选逃生门
                    notes = ""
            if (isinstance(chosen, dict)
                    and chosen.get("status") == "resolved"
                    and chosen.get("answers") is not None):
                # v2 InputPump already collected every question.  Preserve
                # its answer map instead of collapsing it to the first label.
                result = dict(chosen)
                result["notes"] = notes or str(result.get("notes") or "")
            else:
                result = {
                    "choice": str(chosen.get("label") or ""),
                    "notes": notes,
                    "attended": True,
                    "status": "resolved",
                }
            cost = str(chosen.get("cost") or "").strip()
            suffix = f" · {cost}" if cost else ""
            print(DIM(f"  ✓ {result['choice']}{suffix}"))
            return result
        finally:
            # A cancelled/expired worker can make the liveness branch return
            # before the input thread emits its normal close event.  Always
            # dismiss the owner modal without fabricating a choice.
            if chosen is None and pump is not None:
                dismiss = getattr(pump, "dismiss_decision_gate", None)
                if callable(dismiss):
                    try:
                        dismiss()
                    except Exception:
                        pass
            if watcher:
                watcher.resume()

    def _interaction_worker_is_gone(self):
        """Return whether the task currently owning the modal has ended.

        This is intentionally a best-effort liveness check.  The durable
        controller/journal remains authoritative; the check only prevents a
        visible modal from waiting forever after a second terminal cancels the
        worker.  Sessions without a task id (legacy direct callback/tests)
        conservatively return False.
        """
        task_id = str(getattr(self, "_interaction_task_id", "") or "")
        request_id = str(getattr(self, "_interaction_request_id", "") or "")
        manager = getattr(self, "task_manager", None)
        if not task_id or manager is None:
            return False
        pending_probe = getattr(manager, "pending_interactions", None)
        if callable(pending_probe) and request_id:
            try:
                if not any(
                        str(item.get("request_id") or "") == request_id
                        for item in (pending_probe(task_id) or ())):
                    return True
            except Exception:
                pass
        getter = getattr(manager, "get", None)
        if callable(getter):
            try:
                snapshot = getter(task_id)
                status = str(
                    getattr(getattr(snapshot, "status", None), "value", None)
                    or (snapshot.get("status") if isinstance(snapshot, dict)
                        else "") or "").lower()
                if status in {"completed", "failed", "cancelled"}:
                    return True
            except Exception:
                pass
        return False

    def decision_gate(self, payload):
        """Compatibility callback for direct (owner-thread) tool execution.

        Managed workers never reach this callback: ``t_decision_gate`` routes
        them through ``TaskHandle.request_interaction`` instead.  Rejecting a
        stray worker call is safer than trying to guess which thread owns the
        terminal.
        """
        return self._decision_gate_owner(payload)

    def handle_interaction_request(self, event):
        """Owner-side bridge: validate, audit, render, resolve, then wake.

        The order is intentionally part of the protocol.  A worker request is
        first correlated with one controller slot, then the owner response is
        normalized *before* ``interaction_resolved`` is appended, and only
        after that is the blocked worker released.  No recommendation is ever
        substituted for a missing owner response.
        """
        event = event if isinstance(event, dict) else {}
        request_id = str(event.get("request_id") or "").strip()
        task_id = str(event.get("task_id") or "").strip()
        kind = str(event.get("kind") or "").strip()
        raw_payload = event.get("payload")
        # A corrupt/replayed event must be handled as an explicit invalid
        # interaction, not crash the owner loop while coercing a string/list
        # with ``dict(...)``.  The worker will be cancelled or woken with the
        # fail-closed result below.
        payload_is_object = isinstance(raw_payload, dict)
        payload = dict(raw_payload) if payload_is_object else {}
        interaction_role = str(
            payload.get("_interaction_role", "blocked") or "blocked").strip().lower()
        raw_options = payload.get("options")
        options = raw_options if isinstance(raw_options, (list, tuple)) else []
        controller = getattr(self, "controller", None)
        manager = getattr(self, "task_manager", None)
        tool_call_id = str(
            event.get("tool_call_id") or payload.get("tool_call_id")
            or getattr(controller, "current_tool_call_id", None) or "")
        expected_task_id = str(
            getattr(controller, "current_task_id", None) or "").strip()
        task_mismatch = bool(
            expected_task_id and task_id and expected_task_id != task_id)

        # The owner loop is normally serialized, but a duplicate event can be
        # delivered by an embedding that retries a drain.  Return the cached
        # terminal result rather than opening a second modal or resolving a
        # request twice.
        cache_key = (task_id, request_id)
        cache = getattr(self, "_pending_interactions", None)
        if not isinstance(cache, dict):
            cache = {}
            self._pending_interactions = cache
        if request_id and cache_key in cache:
            return dict(cache[cache_key])

        result = self._decision_gate_unattended(
            options, status="invalid", reason="不支持的 interaction")
        begun = False
        normalized_request = None
        if kind == "decision_gate" and not payload_is_object:
            result = self._decision_gate_unattended(
                options, status="invalid",
                reason="decision gate interaction payload 必须是 object")
        elif kind == "decision_gate" and interaction_role != "main":
            result = self._decision_gate_unattended(
                options, status="unattended",
                reason="只有当前窗口的主 agent 可以触发 decision_gate")
        elif kind == "decision_gate":
            try:
                normalized_request, _labels = (
                    tools.normalize_decision_gate_request(
                        payload.get("question"), options,
                        payload.get("context"),
                        questions=payload.get("questions")))
                payload = normalized_request
                payload["_interaction_role"] = "main"
                options = normalized_request["options"]
            except (TypeError, ValueError) as exc:
                result = self._decision_gate_unattended(
                    options, status="invalid",
                    reason=f"decision gate 请求无效：{exc}")
        else:
            result = self._decision_gate_unattended(
                options, status="invalid",
                reason=f"未知 interaction kind: {kind or 'missing'}")
        if task_mismatch:
            # A task-bound request must not be replayed into whichever
            # foreground tool happens to be visible now.  Return a bounded
            # non-consent result to the originating manager; do not open a
            # modal or mutate the current controller slot.
            result = self._decision_gate_unattended(
                options, status="invalid",
                reason=("interaction task_id 与当前 foreground task 不匹配"))

        # If the task was cancelled or timed out before the owner got to the
        # event, do not flash a stale modal.  ``pending_interactions`` is an
        # optional API so small embedding/test managers remain compatible.
        pending_probe = getattr(manager, "pending_interactions", None)
        task_still_waiting = None
        if callable(pending_probe) and task_id and request_id:
            try:
                task_still_waiting = any(
                    str(item.get("request_id") or "") == request_id
                    for item in (pending_probe(task_id) or ()))
            except Exception:
                task_still_waiting = None
        if task_still_waiting is False:
            result = self._decision_gate_unattended(
                options, status="cancelled",
                reason="worker 已取消或 interaction 已过期")

        # Only the foreground/main agent owns an interaction lifecycle.  A
        # workflow/subagent request is deliberately answered unattended above
        # and must not touch the foreground controller (which may not even
        # expose resolve_interaction in lightweight embeddings).  Keeping the
        # role check here is the second, execution-context-level boundary in
        # addition to the child tool allow-list and t_decision_gate guard.
        if (controller is not None and kind and request_id
                and interaction_role == "main"
                and not task_mismatch
                and task_still_waiting is not False):
            try:
                # Persist the immutable request before any modal I/O.
                controller.begin_interaction(
                    tool_call_id, request_id, kind, payload)
                begun = True
                # Refresh the persistent footer before the modal's first
                # frame.  This keeps model/gateway/context and the explicit
                # "需要拍板" state visible alongside the gate, rather than
                # showing a stale "tool running" footer.
                if (getattr(self, "pump", None) is not None
                        and callable(getattr(self, "refresh_composer", None))):
                    self.refresh_composer(
                        busy=True, activity="需要拍板", publish=False)
            except Exception as exc:
                result = self._decision_gate_unattended(
                    options, status="persistence_error",
                    reason=f"interaction 请求无法记账：{type(exc).__name__}: {exc}")
            if (begun and kind == "decision_gate"
                    and normalized_request is not None):
                try:
                    previous_task_id = getattr(
                        self, "_interaction_task_id", None)
                    previous_request_id = getattr(
                        self, "_interaction_request_id", None)
                    self._interaction_task_id = task_id
                    self._interaction_request_id = request_id
                    try:
                        owner_result = self._decision_gate_owner(payload)
                    finally:
                        if previous_task_id is None:
                            self.__dict__.pop("_interaction_task_id", None)
                        else:
                            self._interaction_task_id = previous_task_id
                        if previous_request_id is None:
                            self.__dict__.pop("_interaction_request_id", None)
                        else:
                            self._interaction_request_id = previous_request_id
                except KeyboardInterrupt:
                    owner_result = self._decision_gate_unattended(
                        options, status="cancelled",
                        reason="用户中断了 decision gate")
                except Exception as exc:  # UI failure is fail-closed
                    owner_result = self._decision_gate_unattended(
                        options, status="persistence_error",
                        reason=f"owner UI 异常：{type(exc).__name__}: {exc}")
                # Normalize before writing the durable resolution.  This
                # keeps the audit truthful even for a malformed owner,
                # stale key, or an embedding callback returning extras.
                result = tools.normalize_decision_gate_result(
                    owner_result, normalized_request or payload)
        elif (controller is None and kind == "decision_gate"
              and normalized_request is not None):
            result = self._decision_gate_unattended(
                options, status="persistence_error",
                reason="当前 session 没有 controller")

        controller_resolved = False
        if begun and controller is not None:
            try:
                controller.resolve_interaction(
                    tool_call_id, request_id, result)
                controller_resolved = True
            except Exception as exc:
                # A failed append must not be relabelled as a successful
                # choice.  Try the explicit abort lifecycle if available;
                # otherwise leave the controller pending for recovery.
                abort = getattr(controller, "abort_interaction", None)
                if callable(abort):
                    try:
                        abort(
                            f"interaction 结果无法记账：{type(exc).__name__}: {exc}",
                            status="persistence_error")
                    except Exception:
                        pass
                result = self._decision_gate_unattended(
                    options, status="persistence_error",
                    reason=f"interaction 结果无法记账：{type(exc).__name__}: {exc}")

        # Waking the worker is the second half of the protocol.  A failed
        # resolve must never leave a worker parked forever: cancel by the
        # exact task id, and turn the returned value into a non-consent result.
        resolver = (getattr(manager, "resolve_interaction", None)
                    if manager is not None else None)
        bridge_error = None
        if not task_id or not request_id or not callable(resolver):
            bridge_error = "当前 interaction 缺少可用 task manager/request id"
        else:
            try:
                accepted = resolver(task_id, request_id, result)
                if accepted is False:
                    raise TASKS.TaskError("task manager 未接受 interaction result")
            except Exception as exc:
                bridge_error = f"{type(exc).__name__}: {exc}"
                cancel = getattr(manager, "cancel", None)
                if callable(cancel):
                    try:
                        # TaskManager.cancel intentionally supports a
                        # pre-cancel set for callers racing task creation.
                        # This bridge already received a task event, so do not
                        # leave a stale pre-cancel entry when the task has
                        # simply been pruned or reached a terminal state.
                        live = True
                        getter = getattr(manager, "get", None)
                        if callable(getter):
                            snapshot = getter(task_id)
                            if snapshot is None:
                                live = False
                            else:
                                status = str(
                                    getattr(
                                        getattr(snapshot, "status", None),
                                        "value", None)
                                    or (snapshot.get("status")
                                        if isinstance(snapshot, dict) else "")
                                    or "").lower()
                                live = status not in {
                                    "completed", "failed", "cancelled",
                                }
                        if live:
                            cancel(task_id)
                    except Exception as cancel_exc:
                        bridge_error += (
                            f"；取消 worker 也失败：{type(cancel_exc).__name__}: "
                            f"{cancel_exc}")

        if bridge_error:
            # If controller resolution was not committed (for example a
            # journal failure), close the durable waiting slot as well.  If it
            # was already committed, the audit remains truthful and the task
            # cancellation below prevents a stranded worker.
            if controller_resolved:
                record = getattr(controller, "record_event", None)
                if callable(record):
                    try:
                        record("interaction_bridge_failed", {
                            "task_id": task_id[:128],
                            "request_id": request_id[:256],
                            "tool_call_id": tool_call_id[:256],
                            "error": bridge_error[:512],
                        })
                    except Exception:
                        pass
            if (begun and controller is not None
                    and getattr(controller, "state", None)
                    == C.ControllerState.TOOL_WAITING_DECISION):
                abort = getattr(controller, "abort_interaction", None)
                if callable(abort):
                    try:
                        abort(
                            f"interaction bridge 失败：{bridge_error}",
                            status="persistence_error")
                    except Exception:
                        pass
            failed = self._decision_gate_unattended(
                options, status="persistence_error", reason=bridge_error)
            failed["bridge_error"] = bridge_error
            result = failed

        if request_id:
            cache[cache_key] = dict(result)
            # Bound process-local idempotency memory; durable journal remains
            # the source of truth for recovery and audit.
            while len(cache) > 64:
                cache.pop(next(iter(cache)))
        return result

    def _remember_session_grant(self, name):
        """Persist a session-scoped grant before relying on it.

        A failed save must not silently turn a one-time approval into an
        in-memory cross-turn bypass.  The old set is restored on failure.
        """
        name = str(name or "").strip()
        if not name:
            return False
        before = set(self.always)
        self.always.add(name)
        try:
            self.save()
        except Exception as exc:  # noqa: BLE001 - permission path fails closed
            self.always = before
            print(RED(
                "  ✗ 无法持久化 session grant，已拒绝本次调用："
                f"{type(exc).__name__}: {exc}"))
            return False
        return True

    def confirm(self, name, args):
        if self.pump is not None:
            result = self.permission_decision(name, args)
            return result if isinstance(args, tools.PreparedArguments) \
                else result["allowed"]
        policy_name, perm = _configured_permission(
            self.cfg, name, args)
        if perm == "deny":
            return False                # 配置层直接拒绝，不打扰用户
        memory_decision = self._authorize_memory_risk(name, args)
        if memory_decision is not None:
            return (
                memory_decision
                if isinstance(args, tools.PreparedArguments)
                else memory_decision["allowed"])
        workspace_decision = self._authorize_workspace_risk(name, args)
        if workspace_decision is not None:
            return (
                workspace_decision
                if isinstance(args, tools.PreparedArguments)
                else workspace_decision["allowed"])
        if (perm == "allow" or self.auto or policy_name in self.always
                or self._accept_edits_applies(name)):
            source = (
                "config" if perm == "allow" else
                "session_grant" if policy_name in self.always else
                "mode" if self._accept_edits_applies(name) and not self.auto
                else "session")
            decision = (
                "allow" if perm == "allow" else
                "always" if policy_name in self.always else
                "accept_edits" if self._accept_edits_applies(name) and not self.auto
                else "auto_approve")
            result = self._authorize_unsandboxed(name, args, {
                "allowed": True, "decision": decision, "source": source,
            })
            return result if isinstance(args, tools.PreparedArguments) \
                else result["allowed"]
        if not sys.stdin.isatty():
            # 没有 TTY 就不存在“询问用户”。只有配置 allow、auto_approve 或
            # 显式 --yolo 才能越过；ask 必须 fail-closed。
            return False
        print()
        print(f"  {YELLOW('需要确认')}  {BOLD(policy_name)}  {preview(name, args)}")
        _print_approval_details(name, args)
        # 方向键选择，而不是让用户记 y/a/n —— 这是 claude/codex 的做法，
        # 也是唯一不用记忆负担的形式：选项就摆在眼前，↑↓ 选、Enter 确认。
        opts = [
            ("once", "允许这一次"),
            ("always", f"本次会话都允许 {policy_name}"),
            ("deny", "拒绝"),
        ]
        if tui.supported():
            # Esc 监听线程正占着 stdin，不暂停它就会把方向键吞掉。
            w = self.watcher
            if w:
                w.pause()
            try:
                pick = tui.select(opts, title="", page=3, allow_filter=False,
                                  render=lambda o: o[1])
            finally:
                if w:
                    w.resume()
            # Esc/Ctrl-C 返回 None —— **fail-closed**，当作拒绝。
            choice = pick[0] if pick else "deny"
        else:
            try:
                a = input(f"  {DIM('[y] 允许  [a] 本次都允许  [n] 拒绝 > ')}").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            choice = {"a": "always", "all": "always", "n": "deny",
                      "no": "deny"}.get(a, "once")
        if choice == "always":
            if not self._remember_session_grant(policy_name):
                return False
            print(DIM(f"  ✓ 本次会话内 {policy_name} 不再询问"))
            result = self._authorize_unsandboxed(name, args, {
                "allowed": True, "decision": "always", "source": "user",
            })
            return result if isinstance(args, tools.PreparedArguments) \
                else result["allowed"]
        if choice == "deny":
            print(DIM("  ✗ 已拒绝"))
            return False
        result = self._authorize_unsandboxed(name, args, {
            "allowed": True, "decision": "once", "source": "user",
        })
        return result if isinstance(args, tools.PreparedArguments) \
            else result["allowed"]

    # ------------------------------------------------------------ 渲染一轮
    def _turn_managed(self, text):
        self._last_text = ""
        outcome = TurnOutcome("running")
        self._set_terminal_title(idle=False)
        streaming = False
        started_at = time.monotonic()
        last_flush = started_at
        pending = []
        pending_chars = 0
        # SPEC-CC-parity D1：思考不进正文。reasoning 事件与正文里的 <think> 都
        # 进 thinker；正文只喂 ThinkFilter 放出来的可见部分。
        thinker = THINKING.ThinkingTracker()
        think_filter = THINKING.ThinkFilter()
        self._last_thinking = thinker
        editor_active = True
        last_activity_frame = None
        active_tool = None
        active_task = None
        active_tool_call_id = None
        plan_event_task_ids = set()
        workflow_event_task_ids = set()
        next_action = None
        # turn 边界竞态：controller 已经把 turn 记为结束、本循环却还在 drain 输入时，
        # submit() 会直接分发并返回 action。以前这里 raise —— 整个 REPL 崩。
        # 现在把它们攒起来，经 next_action 交回 run_actions 顺序执行。
        late_actions = []
        markdown = tui.StreamingMarkdown(
            icon_hosted=True,   # C6：首行由 ⏺ 托管
            styled=tui.terminal_style_enabled(
                getattr(self.renderer, "stream", None)))
        snapshot = self.refresh_composer(
            busy=True,
            prompt=self.composer_prompt(busy=True),
            activity="思考中 0s", publish=False)
        self.renderer.render(snapshot)

        def flush_model(force=False, preserve_input=False):
            nonlocal pending, pending_chars, last_flush
            if not pending:
                return
            now = time.monotonic()
            if (not force and pending_chars < 4096
                    and now - last_flush < 0.025):
                return
            self.renderer.clear_input()
            self.renderer.write_output(
                "".join(pending), source="model")
            pending, pending_chars = [], 0
            last_flush = now
            snapshot = self.pump.snapshot()
            if snapshot.active:
                self.renderer.render(snapshot)

        def finish_markdown():
            nonlocal pending_chars
            tail = markdown.finish()
            if tail:
                pending.append(tail)
                pending_chars += len(tail)

        def render_dispatched_inputs():
            """Move durable queue dispatches into chat scrollback once."""
            nonlocal streaming
            items = self.controller.drain_dispatched_inputs()
            if not items:
                return
            # A steer/new turn is a semantic boundary between assistant
            # messages. Close open Markdown state before painting the user
            # prompt so the next response starts with a clean renderer.
            finish_markdown()
            flush_model(force=True, preserve_input=True)
            for item in items:
                self.renderer.commit_input(
                    self.composer_prompt(busy=False), item.text)
            streaming = False

        def emit_block(value, *, source="status"):
            flush_model(force=True, preserve_input=True)
            self.renderer.clear_input()
            self.renderer.write_output(value, source=source)
            snapshot = self.pump.snapshot()
            if snapshot.active:
                self.renderer.render(snapshot)

        def emit_thinking():
            """把累计的思考折叠成一行写进 transcript，一个 turn 只写一次。
            在第一段可见正文 / 第一个工具调用 / 收尾时调用。"""
            if thinker and not thinker.emitted:
                thinker.emitted = True
                emit_block(DIM(f"  {thinker.summary()}\r\n"))

        def busy_label():
            # 工具运行期间不要把活动行打回「工作中」：等待行/工具行才是当前真相（J3）。
            # 之前完成通知一落地就 refresh 成「工作中」，update_activity 的帧去重又让
            # 它到下一秒才恢复——"等待 1 个"就这样闪没了。
            if active_tool:
                return (f"{self._tool_activity_label(active_tool)} "
                        f"{int(time.monotonic() - started_at)}s")
            return "工作中"
        def update_activity(label, *, force=False, render=True):
            nonlocal last_activity_frame
            elapsed = int(time.monotonic() - started_at)
            frame = (str(label), elapsed)
            if not force and frame == last_activity_frame:
                return
            snapshot = self.refresh_composer(
                busy=True, activity=f"{label} {elapsed}s", publish=False)
            if render:
                self.renderer.render(snapshot)
            last_activity_frame = frame

        def input_mode(event):
            if event.text.startswith("/"):
                return C.QueueMode.COMMAND
            if event.text.startswith("!"):
                return C.QueueMode.SHELL
            return C.QueueMode(event.mode)

        def handle_input_events():
            nonlocal editor_active
            self.heartbeat_lease()
            for notice in self.drain_task_events():
                emit_block(
                    notice["text"], source=notice["source"])
            workspace_notices = self.drain_agent_events()
            workspace_notices.extend(self.drain_catalog_events())
            workflow_notices = self.drain_workflow_events()
            projection_changed = self._agent_projection_changed
            if projection_changed and active_tool:
                # 子代理起停 → 等待行的数字跟着变（J3），原地更新不进 scrollback
                self._active_agent_count_at = 0.0
                update_activity(self._tool_activity_label(active_tool), force=True)
            for notice in workspace_notices:
                emit_block(notice, source="status")
            for notice in workflow_notices:
                emit_block(notice, source="status")
            if projection_changed or workflow_notices:
                snapshot = self.refresh_composer(
                    busy=True,
                    prompt=self.composer_prompt(busy=True),
                    activity=busy_label(), publish=False)
                self.renderer.render(snapshot)
            for event in self._drain_pump_events():
                if event.kind == "redraw":
                    snapshot = event.snapshot
                    if snapshot.mode != "line":
                        self.renderer.render(snapshot)
                        self.sync_composer_selection()
                        continue
                    if snapshot.active:
                        if not editor_active:
                            flush_model(force=True)
                        editor_active = True
                        self.renderer.render(snapshot)
                        self.sync_composer_selection()
                    else:
                        editor_active = False
                        self.renderer.clear_input()
                        self.sync_composer_selection()
                elif event.kind in {
                        "history_scroll", "history_click", "history_mouse",
                        "history_copy", "history_close"}:
                    self.handle_history_navigation(event)
                elif event.kind in {
                        "composer_mouse", "composer_copy",
                        "composer_clear_selection"}:
                    self.handle_composer_selection(event)
                elif event.kind == "submit":
                    if event.text.strip() == "?":
                        event = dataclasses.replace(event, text="/keys")   # G2，忙时也可查
                    live_command = resolve_live_command(event.text)
                    if live_command is not None:
                        _, command_handler, command_rest = (
                            live_command)
                        store.append_history(
                        event.text, session_id=self.ag.session_id)
                        flush_model(force=True)
                        self.renderer.clear_input()
                        self.renderer.finish_output_line()
                        command_handler(self, command_rest)
                        snapshot = self.refresh_composer(
                            busy=True,
                            prompt=self.composer_prompt(busy=True),
                            activity=busy_label(), publish=False)
                        editor_active = True
                        self.renderer.render(snapshot)
                        continue
                    custom = None
                    if event.text.startswith("/"):
                        try:
                            custom = self.resolve_custom_command(event.text)
                        except CUSTOM_COMMANDS.CommandError as exc:
                            emit_block(RED(
                                f"  [custom command] {exc}\r\n"))
                            snapshot = self.refresh_composer(
                                busy=True,
                                prompt=self.composer_prompt(busy=True),
                                activity=busy_label(), publish=False)
                            editor_active = True
                            self.renderer.render(snapshot)
                            continue
                    if custom is not None:
                        _, expanded = custom
                        store.append_history(
                        event.text, session_id=self.ag.session_id)
                        action = self.controller.submit(
                            expanded, C.QueueMode.NEXT_TURN)
                        if action is not None:
                            # turn 已在 controller 侧结束：交回 run_actions，不 raise。
                            late_actions.append(action)
                            snapshot = self.refresh_composer(
                                busy=True, activity=busy_label(), publish=False)
                            editor_active = True
                            self.renderer.render(snapshot)
                            continue
                        item = self.controller.queued[-1]
                        self.remember_queue_display(item, event.text)
                        snapshot = self.refresh_composer(
                            busy=True, activity=busy_label(), publish=False)
                        editor_active = True
                        flush_model(force=True)
                        self.renderer.render(snapshot)
                        continue
                    if (self.attached_agent_id
                            and not event.text.startswith("/")):
                        try:
                            self.send_agent(
                                self.attached_agent_id, event.text)
                        except (AGENTS.AgentRuntimeError, ValueError) as exc:
                            emit_block(RED(
                                f"  [agent direct message 失败] {exc}\r\n"))
                        snapshot = self.refresh_composer(
                            busy=True,
                            prompt=self.composer_prompt(busy=True),
                            activity=busy_label(), publish=False)
                        editor_active = True
                        flush_model(force=True)
                        self.renderer.render(snapshot)
                        continue
                    mode = input_mode(event)
                    store.append_history(
                        event.text, session_id=self.ag.session_id)
                    try:
                        action = self.controller.submit(event.text, mode)
                    except C.ControllerError:
                        # 边界的第二种形态：Enter 在忙时是 steer，而 turn 恰好在这一瞬
                        # 结束 —— steer 没有对象。它不是错误，它就是下一个 turn。
                        if mode != C.QueueMode.STEER:
                            raise
                        action = self.controller.submit(
                            event.text, C.QueueMode.NEXT_TURN)
                    snapshot = self.refresh_composer(
                        busy=True, activity=busy_label(), publish=False)
                    editor_active = True
                    flush_model(force=True)
                    self.renderer.render(snapshot)
                    if action is not None:
                        # 同上：边界上到达的输入已被 controller 直接分发。
                        late_actions.append(action)
                elif event.kind == "cycle_mode":
                    flush_model(force=True)
                    self.renderer.clear_input()
                    self.renderer.finish_output_line()
                    self.cycle_permission_mode()
                    snapshot = self.refresh_composer(
                        busy=True,
                        prompt=self.composer_prompt(busy=True),
                        activity=busy_label(), publish=False)
                    editor_active = True
                    self.renderer.render(snapshot)
                elif event.kind == "expand_last":
                    flush_model(force=True)
                    self.renderer.clear_input()
                    self.renderer.finish_output_line()
                    self.expand_last()
                    snapshot = self.refresh_composer(
                        busy=True,
                        prompt=self.composer_prompt(busy=True),
                        activity=busy_label(), publish=False)
                    editor_active = True
                    self.renderer.render(snapshot)
                elif event.kind == "agents":
                    flush_model(force=True)
                    open_agents_panel(self)
                    snapshot = self.refresh_composer(
                        busy=True,
                        prompt=self.composer_prompt(busy=True),
                        activity=busy_label(), publish=False)
                    editor_active = True
                    self.renderer.render(snapshot)
                elif event.kind == "agent_attach":
                    flush_model(force=True)
                    try:
                        show_agent_preview(self, event.value)
                        self.attach_agent(event.value)
                    except (AGENTS.AgentRuntimeError, ValueError) as exc:
                        emit_block(RED(f"  [agents] {exc}\r\n"))
                    snapshot = self.refresh_composer(
                        busy=True,
                        prompt=self.composer_prompt(busy=True),
                        activity=busy_label(), publish=False)
                    editor_active = True
                    self.renderer.render(snapshot)
                elif event.kind == "agent_detach":
                    self.detach_agent(publish=False)
                    snapshot = self.refresh_composer(
                        busy=True,
                        prompt=self.composer_prompt(busy=True),
                        activity=busy_label(), publish=False)
                    editor_active = True
                    self.renderer.render(snapshot)
                elif event.kind == "detach":
                    self.detach_agent(
                        draft=event.text, publish=False)
                    snapshot = self.refresh_composer(
                        busy=True,
                        prompt=self.composer_prompt(busy=True),
                        activity=busy_label(), publish=False)
                    editor_active = True
                    self.renderer.render(snapshot)
                elif event.kind == "retrieve":
                    item = self.controller.retrieve_latest()
                    if item is not None:
                        display = self.take_queue_display(item)
                        self._skip_queue_display.discard(item.id)
                        self.refresh_composer(publish=False)
                        snapshot = self.pump.replace_buffer(
                            display, active=True, publish=False)
                        self.renderer.render(snapshot)
                    else:
                        self.renderer.bell()
                elif event.kind == "background":
                    try:
                        outcome, task = self.handle_background_key()
                    except TASKS.TaskError as exc:
                        self.renderer.bell()
                        emit_block(DIM(f"  [Ctrl+B] {exc}\r\n"))
                    else:
                        if outcome == "backgrounded":
                            update_activity(
                                f"task {task.id} 正在转后台", force=True)
                        else:
                            emit_block(DIM(
                                f"  [已 detach task {task}]\r\n"))
                            snapshot = self.refresh_composer(
                                busy=True,
                                prompt=self.composer_prompt(busy=True),
                                activity=busy_label(), publish=False)
                            self.renderer.render(snapshot)
                elif event.kind in ("cancel", "interrupt"):
                    action, task = self.cancel_active_work()
                    if action is not None:
                        if action.kind == C.ActionKind.CANCEL_TOOL:
                            tool_call_id = action.payload.get(
                                "tool_call_id")
                            task_label = (
                                task.id if task is not None
                                else tool_call_id)
                            label = f"正在取消工具 {task_label}"
                            message = (
                                f"  [正在取消 foreground tool {task_label}；"
                                "queue 与草稿保留]\r\n")
                        else:
                            label = "正在中断请求"
                            message = (
                                "  [正在中断当前请求；queue 与草稿保留]\r\n")
                        update_activity(label, force=True)
                        snapshot = self.pump.snapshot()
                        self.renderer.clear_input()
                        self.renderer.write_output(
                            DIM(message))
                        if snapshot.active:
                            self.renderer.render(snapshot)
                    elif task is not None and getattr(
                            task, "background", False):
                        self.renderer.bell()
                        emit_block(DIM(
                            f"  [task {task.id} 已转后台；"
                            f"用 /kill {task.id} 显式终止]\r\n"),
                            source="status")
                elif event.kind == "error":
                    raise event.value

            self.sync_composer_selection()

        try:
            for event in self.ag.run(
                    text, controller=self.controller,
                    max_turns=getattr(self, "max_turns", None),
                    stream_factory=client.stream_chat_background,
                    tool_runner=self.task_manager.run,
                    background_task=self.background_model_task,
                    plan_mode=self.plan_mode,
                    allowed_tools=self.allowed_tool_names()):
                handle_input_events()
                event_type = event["t"]
                if event_type not in {"interrupted", "error", "end"}:
                    render_dispatched_inputs()
                if event_type == "poll":
                    flush_model()
                    if active_tool:
                        total = (
                            int(event.get("stdout_bytes") or 0)
                            + int(event.get("stderr_bytes") or 0))
                        size = f" · {total / 1024:.1f} KiB" if total else ""
                        task = active_task or event.get("task_id")
                        suffix = f"[{task}] 运行中{size}" if task else f"运行中{size}"
                        # 有活跃子代理时这里是等待行（J3）；每 25 ms 一次，计数有节流
                        update_activity(
                            self._tool_activity_label(active_tool, suffix=suffix))
                    elif streaming:
                        update_activity("输出中")
                    elif thinker:
                        # 思考流的 chunk 间隙也会有 poll，别把 ✻ 思考中 · N 字 冲掉
                        update_activity(thinker.activity())
                    else:
                        update_activity(_phase_activity(
                            event.get("phase"), event.get("phase_age")))
                elif event_type == "reasoning":
                    thinker.add(event.get("v", ""))
                    update_activity(thinker.activity())
                elif event_type == "text":
                    visible, hidden = think_filter.feed(event["v"])
                    thinker.add(hidden)
                    if hidden and not visible:
                        update_activity(thinker.activity())
                    if not visible:
                        continue
                    emit_thinking()
                    self._last_text += visible
                    rendered = markdown.feed(visible)
                    if not streaming:
                        self.renderer.clear_spinner()
                        update_activity("输出中", force=True)
                        pending.append(ORANGE("⏺ "))
                        pending_chars += 2
                        streaming = True
                    if rendered:
                        pending.append(rendered)
                        pending_chars += len(rendered)
                    if pending_chars >= 1_048_576:
                        flush_model(force=True, preserve_input=editor_active)
                    else:
                        flush_model()
                elif event_type == "tool_start":
                    emit_thinking()
                    if self.renderer.history_active:
                        self.renderer.close_history(self.pump.snapshot())
                        self.pump.set_history_mode(False)
                    finish_markdown()
                    self.renderer.clear_spinner()
                    prefix = _tool_transition_prefix(streaming)
                    streaming = False
                    active_tool = event["name"]
                    active_task = None
                    active_tool_call_id = event.get("tool_call_id")
                    update_activity(self._tool_activity_label(event['name']), force=True)
                    emit_block(
                        prefix
                        + "  " + tool_call_line(event['name'], event['args']) + "\r\n",
                        source="tool")
                elif event_type == "tool_task_started":
                    active_task = event.get("task_id")
                    update_activity(
                        self._tool_activity_label(
                            event['name'], suffix=f"[{active_task}] 运行中"),
                        force=True)
                elif event_type == "tool_output":
                    total = (
                        int(event.get("stdout_bytes") or 0)
                        + int(event.get("stderr_bytes") or 0))
                    preview_lines = tui.sanitize_terminal_text(
                        str(event.get("v") or "")).splitlines()
                    live_preview = " ".join(
                        preview_lines[-1].split()) if preview_lines else ""
                    detail = (
                        " · " + tui.truncate_display(live_preview, 44)
                        if live_preview else "")
                    update_activity(self._tool_activity_label(
                        event['name'],
                        suffix=(f"[{event.get('task_id')}] 运行中 · "
                                f"{total / 1024:.1f} KiB{detail}")))
                elif event_type == "tool_task_backgrounded":
                    active_task = event.get("task_id") or active_task
                    emit_block(DIM(
                        f"    ↳ task {active_task} · background · "
                        f"/tasks 查看 · /task {active_task} attach\r\n"))
                    update_activity(
                        f"{event['name']} [{active_task}] 已转后台",
                        force=True)
                elif event_type == "tool_task_end":
                    update_activity(
                        f"{event['name']} [{event.get('task_id')}] "
                        f"{event.get('status')}",
                        force=True)
                elif event_type == "interaction_request":
                    # The request originated in a worker, but resolution and
                    # all terminal writes stay on this owner thread.
                    result = self.handle_interaction_request(event)
                    status = str(result.get("status") or "invalid")
                    if status == "resolved":
                        update_activity("已收到用户选择，继续执行", force=True)
                    else:
                        emit_block(YELLOW(
                            f"  [decision_gate {status}：本轮暂停，"
                            "未替你作决定]\r\n"), source="status")
                elif event_type == "decision_candidate":
                    # This is a preflight hold, not a user choice.  Keep it
                    # visible before the synthetic blocked tool results so a
                    # model retry cannot look like a silent no-op.
                    emit_block(YELLOW(
                        self._decision_candidate_notice(
                            event.get("candidate")) + "\r\n"),
                        source="status")
                    update_activity(
                        "等待主 agent 发起 decision_gate", force=True)
                elif event_type == "tool_note":
                    emit_block(YELLOW(
                        f"  [hook] {event.get('v', '')}\r\n"))
                elif event_type == "workspace_changed":
                    update_activity("刷新 repo map", force=True)
                    try:
                        self.refresh_repo_context(force=False)
                    except Exception as exc:             # noqa: BLE001
                        emit_block(YELLOW(
                            "  [工作区已修改，但 repo projection 刷新失败："
                            f"{type(exc).__name__}: {exc}]\r\n"))
                elif event_type == "route_warning":
                    emit_block(YELLOW(
                        f"  [route warning] {event.get('v', '')}\r\n"))
                elif event_type == "plan_event":
                    try:
                        record, changed = self.update_task_plan(
                            dict(event.get("payload") or {}))
                    except PLANS.PlanError as exc:
                        emit_block(RED(
                            f"  [plan update 无效：{exc}]\r\n"))
                    else:
                        task_id = str(event.get("task_id") or "")
                        if task_id:
                            plan_event_task_ids.add(task_id)
                        if changed:
                            emit_block(
                                _render_updated_plan(record),
                                source="plan")
                        update_activity("计划已更新", force=True)
                elif event_type == "goal_proposal":
                    try:
                        staged = self._goal_proposal_from_tool(
                            dict(event.get("payload") or {}))
                    except (GOALS.GoalError, ValueError, TypeError) as exc:
                        emit_block(RED(f"  [goal 提案无效：{exc}]\r\n"))
                    else:
                        if staged.get("armed"):
                            emit_block(GREEN(
                                f"  [goal 已采纳并 armed · 最多 "
                                f"{staged['max_rounds']} 轮 · 空闲时自动续轮"
                                "，/goal pause 可随时停用]\r\n"))
                        else:
                            emit_block(_render_goal_proposal(
                                getattr(self, "_goal_proposal", None)
                                or staged))
                elif event_type == "goal_event":
                    try:
                        view = self._apply_goal_event(
                            dict(event.get("payload") or {}))
                    except (GOALS.GoalError, ValueError, TypeError) as exc:
                        emit_block(RED(f"  [goal update 无效：{exc}]\r\n"))
                    else:
                        action_name = str(
                            (event.get("payload") or {}).get("action")
                            or "progress")
                        if action_name == "complete":
                            emit_block(GREEN("  [goal 已由模型验证并标记 complete]\r\n"))
                        elif action_name == "blocked":
                            emit_block(YELLOW("  [goal 已由模型标记 blocked]\r\n"))
                        else:
                            update_activity("goal 进度已记录", force=True)
                elif event_type == "workflow_event":
                    payload = dict(event.get("payload") or {})
                    workflow_id = str(payload.get("id") or "?")
                    current = dict(self._workflow_current or {})
                    current.update(payload)
                    self._workflow_current = current
                    task_id = str(event.get("task_id") or "")
                    if task_id:
                        workflow_event_task_ids.add(task_id)
                    budget = (payload.get("budget") or {}).get(
                        "max_requests")
                    emit_block(DIM(
                        f"  ◆ workflow {workflow_id} · adaptive DAG"
                        f" · provider-attempt budget {budget}\r\n"))
                    update_activity("workflow 已启动", force=True)
                elif event_type == "graft_event":
                    payload = dict(event.get("payload") or {})
                    self._graft_current = payload
                    state = str(payload.get("state") or "?")
                    action = str(payload.get("action") or "query")
                    if state == "running":
                        update_activity(f"graft {action}", force=True)
                    elif state == "ready":
                        files = int(payload.get("files") or 0)
                        detail = f" · {files} files" if files else ""
                        update_activity(
                            f"graft ready{detail}", force=True)
                    else:
                        update_activity("graft failed", force=True)
                elif event_type == "agent_event":
                    payload = dict(event.get("payload") or {})
                    run_id = str(payload.get("id") or "")
                    if run_id:
                        self._cache_agent(payload)
                        self._agent_cache_at.pop(run_id, None)
                    runtime_kind = str(payload.get("kind") or "")
                    if runtime_kind == CONSULTS.KIND:
                        self._consult_current = dict(payload)
                    kind = str(event.get("kind") or "")
                    if kind == "agent_spawned":
                        emit_block(DIM(_agent_spawn_notice(payload)))
                    update_activity("child agents", force=True)
                elif event_type == "tool_end":
                    if self.renderer.history_active:
                        self.renderer.close_history(self.pump.snapshot())
                        self.pump.set_history_mode(False)
                    closed_background_handoff = (
                        self._task_background_handoff_id
                        == event.get("task_id"))
                    if closed_background_handoff:
                        self._task_background_handoff_id = None
                    tool_call_id = (
                        event.get("tool_call_id") or active_tool_call_id)
                    active_tool = None
                    active_task = None
                    active_tool_call_id = None
                    self.renderer.clear_input()
                    self.renderer.finish_output_line()
                    task_info = event.get("task") or {}
                    plan_result_already_shown = (
                        event.get("name") == "todo_write"
                        and str(event.get("task_id") or "")
                        in plan_event_task_ids)
                    workflow_result_already_shown = (
                        event.get("name") == "workflow"
                        and str(event.get("task_id") or "")
                        in workflow_event_task_ids)
                    if _memory_tool_changed(
                            event.get("name"), event.get("result")):
                        try:
                            self.refresh_memory_context()
                        except MEMORY.MemoryError as exc:
                            emit_block(RED(
                                f"  [memory index 刷新失败：{exc}]\r\n"))
                    if (event.get("status") != "backgrounded"
                            and not plan_result_already_shown
                            and not workflow_result_already_shown):
                        show_result(
                            event["name"], event["result"],
                            event.get("denied"), event.get("secs"),
                            status=event.get("status"), task=task_info,
                            task_id=event.get("task_id"),
                            tool_call_id=tool_call_id)
                        print()
                    if plan_result_already_shown:
                        plan_event_task_ids.discard(str(
                            event.get("task_id") or ""))
                    if workflow_result_already_shown:
                        workflow_event_task_ids.discard(str(
                            event.get("task_id") or ""))
                    update_activity("思考中", force=True)
                    if closed_background_handoff:
                        # Agent 已在 yield tool_end 前 finish_tool；现在 release
                        # terminal，保证 task_finished/UI completed 不早于
                        # 唯一 backgrounded tool result。
                        for notice in self.drain_task_events():
                            emit_block(
                                notice["text"], source=notice["source"])
                elif event_type == "aged":
                    emit_block(DIM(
                        f"  [已把 {event.get('n', '?')} 条旧工具结果替换为摘要，"
                        f"释放约 {event['v']:,} 字符上下文]\r\n"))
                elif event_type == "compacted":
                    if str(event["v"]).startswith("[摘要生成失败"):
                        emit_block(YELLOW(f"  {event['v']}\r\n"))
                    else:
                        emit_block(DIM(
                            "  [上下文已自动压缩 —— "
                            + _compaction_effect(
                                event.get("before"), event.get("after"),
                                event.get("model"), self.ag.model)
                            + f"，摘要 {len(event['v']):,} 字符"
                            + _anchor_notice(
                                getattr(self.ag, "context_summary", None))
                            + "]\r\n"))
                elif event_type == "usage":
                    self.last_ctx = event.get("ctx", 0)
                    update_activity(
                        "输出中" if streaming else "思考中", force=True)
                elif event_type == "interrupted":
                    if event.get("continuing"):
                        outcome = TurnOutcome("running")
                    else:
                        outcome = TurnOutcome(
                            "interrupted", "interrupted",
                            str(event.get("v") or "用户中断"))
                    finish_markdown()
                    flush_model(
                        force=True, preserve_input=editor_active)
                    self.renderer.clear_spinner()
                    emit_block(DIM(
                        "\r\n  ⎿  已中断 · 直接输入下一步即可\r\n"))
                    streaming = False
                    render_dispatched_inputs()
                    if not event.get("continuing"):
                        next_action = event.get("action")
                        break
                elif event_type == "error":
                    outcome = TurnOutcome(
                        "error", str(event.get("kind") or "agent_error"),
                        str(event.get("v") or ""))
                    finish_markdown()
                    flush_model(
                        force=True, preserve_input=editor_active)
                    self.renderer.clear_spinner()
                    head, step = _error_lines(event.get("v"), event.get("kind"))
                    emit_block(head + "\r\n" + step + "\r\n")
                    streaming = False
                    next_action = event.get("action")
                    render_dispatched_inputs()
                    break
                elif event_type == "end":
                    partial = _partial_end_details(event)
                    if partial is not None:
                        kind, message = partial
                        outcome = TurnOutcome("error", kind, message)
                        finish_markdown()
                        flush_model(
                            force=True, preserve_input=editor_active)
                        self.renderer.clear_spinner()
                        emit_block(
                            YELLOW(f"  ⚠ {message}\r\n"),
                            source="status")
                        streaming = False
                    elif outcome.status == "running":
                        outcome = TurnOutcome("ok")
                    finish_markdown()
                    flush_model(
                        force=True, preserve_input=editor_active)
                    if streaming:
                        emit_block("\r\n")
                    streaming = False
                    next_action = event.get("action")
                    render_dispatched_inputs()
                    break
            handle_input_events()
        except KeyboardInterrupt:
            outcome = TurnOutcome(
                "interrupted", "keyboard_interrupt", "用户中断")
            finish_markdown()
            flush_model(force=True, preserve_input=editor_active)
            if self.controller.current_request_id is not None:
                self.controller.request_cancel()
                next_action = self.controller.acknowledge_interrupt(
                    self._last_text)
            elif self.controller.current_turn_id is not None:
                next_action = self.controller.fail_turn(
                    "keyboard_interrupt")
            emit_block(DIM(
                "\r\n  ⎿  已中断（Ctrl+C）· 直接输入下一步即可\r\n"))
        finally:
            if outcome.status == "running":
                outcome = TurnOutcome(
                    "protocol_error", "missing_terminal_event",
                    "Agent stream ended without end/error/interrupted")
            self.last_turn_outcome = outcome
            tail_visible, tail_hidden = think_filter.finish()
            thinker.add(tail_hidden)
            if tail_visible:
                self._last_text += tail_visible
                rendered = markdown.feed(tail_visible)
                if rendered:
                    pending.append(rendered)
                    pending_chars += len(rendered)
            emit_thinking()
            finish_markdown()
            flush_model(force=True, preserve_input=editor_active)
            self.renderer.clear_spinner()
            self.renderer.finish_output_line()
            snapshot = self.refresh_composer(
                busy=True, prompt=self.composer_prompt(busy=True),
                activity="完成", publish=False)
            self.renderer.render(snapshot)
            self._notify_turn_complete(outcome)

        if late_actions:
            if next_action is None:
                next_action = late_actions.pop(0)
            self.__dict__.setdefault("_late_actions", []).extend(late_actions)
        return next_action

    def turn(self, text, action=None):
        # 马上要用模型了：顺带看一眼目录是否过期（自带节流与 TTL 两道闸）
        self.maybe_refresh_catalog()
        if action is not None and self.pump is not None:
            return self._turn_managed(text)
        self._last_text = ""
        outcome = TurnOutcome("running")
        self._set_terminal_title(idle=False)
        streaming = False
        sp = Spinner()
        sp.__enter__()
        # D1：-p / 非托管路径同样不让思考进正文；这里不画折叠行（stdout 是数据）。
        think_filter = THINKING.ThinkFilter()
        thinker = THINKING.ThinkingTracker()
        self._last_thinking = thinker
        watcher = tui.EscWatcher()
        watcher.__enter__()
        self.watcher = watcher
        markdown_styled = tui.terminal_style_enabled(sys.stdout)
        markdown = (
            tui.StreamingMarkdown(styled=True, icon_hosted=True)
            if markdown_styled else None)
        active_tool_call_id = None

        def finish_markdown():
            if markdown is None:
                return
            tail = markdown.finish()
            if tail:
                sys.stdout.write(tail)
                sys.stdout.flush()

        watch = _SilenceWatch(
            f"{getattr(self.ag, 'gateway', '?')}/{getattr(self.ag, 'model', '?')}").start()
        try:
            for ev in self.ag.run(
                    text, cancel=watcher.event,
                    max_turns=getattr(self, "max_turns", None),
                    plan_mode=self.plan_mode,
                    allowed_tools=self.allowed_tool_names()):
                watch.touch()
                self.heartbeat_lease()
                t = ev["t"]
                if t == "reasoning":
                    thinker.add(ev.get("v", ""))
                    continue
                if t == "text":
                    visible, hidden = think_filter.feed(ev["v"])
                    thinker.add(hidden)
                    if not visible:
                        continue
                    self._last_text = getattr(self, "_last_text", "") + visible
                    rendered = (
                        markdown.feed(visible)
                        if markdown is not None else visible)
                    if not streaming:
                        sp.__exit__()
                        print(ORANGE("⏺ "), end="")
                        streaming = True
                    sys.stdout.write(rendered)
                    sys.stdout.flush()
                elif t == "tool_start":
                    finish_markdown()
                    if streaming:
                        print("\n")
                        streaming = False
                    else:
                        sp.__exit__()
                    active_tool_call_id = ev.get("tool_call_id")
                    print(tool_call_line(ev['name'], ev['args']))
                elif t == "decision_candidate":
                    sp.__exit__()
                    print(YELLOW(self._decision_candidate_notice(
                        ev.get("candidate"))))
                    sp = Spinner("等待 decision_gate")
                    sp.__enter__()
                elif t == "tool_end":
                    if _memory_tool_changed(ev["name"], ev["result"]):
                        try:
                            self.refresh_memory_context()
                        except MEMORY.MemoryError as exc:
                            print(RED(f"  [memory index 刷新失败：{exc}]"))
                    show_result(
                        ev["name"], ev["result"], ev.get("denied"),
                        ev.get("secs"), status=ev.get("status"),
                        task=ev.get("task"), task_id=ev.get("task_id"),
                        tool_call_id=(
                            ev.get("tool_call_id")
                            or active_tool_call_id))
                    active_tool_call_id = None
                    print()
                    sp = Spinner("执行中")
                    sp.__enter__()
                elif t == "aged":
                    sp.__exit__()
                    print(DIM(f"  [已把 {ev.get('n', '?')} 条旧工具结果替换为摘要，"
                              f"释放约 {ev['v']:,} 字符上下文]"))
                    sp = Spinner()
                    sp.__enter__()
                elif t == "compacted":
                    sp.__exit__()
                    if str(ev["v"]).startswith("[摘要生成失败"):
                        print(YELLOW(f"  {ev['v']}"))
                    else:
                        print(DIM(f"  [上下文已自动压缩 —— 摘要 {len(ev['v'])} 字符]"))
                    sp = Spinner()
                    sp.__enter__()
                elif t == "usage":
                    self.last_ctx = ev.get("ctx", 0)
                elif t == "route_warning":
                    sp.__exit__()
                    print(YELLOW(
                        f"  [route warning] {ev.get('v', '')}"))
                    sp = Spinner("执行中")
                    sp.__enter__()
                elif t == "interrupted":
                    outcome = TurnOutcome(
                        "interrupted", "interrupted",
                        str(ev.get("v") or "用户中断"))
                    finish_markdown()
                    sp.__exit__()
                    streaming = False
                    print(DIM("\n  ⎿  已中断 · 直接输入下一步即可"))
                    break
                elif t == "error":
                    outcome = TurnOutcome(
                        "error", str(ev.get("kind") or "agent_error"),
                        str(ev.get("v") or ""))
                    finish_markdown()
                    sp.__exit__()
                    streaming = False
                    head, step = _error_lines(ev.get("v"), ev.get("kind"))
                    print(head)
                    print(step)
                elif t == "end":
                    partial = _partial_end_details(ev)
                    if partial is not None:
                        kind, message = partial
                        outcome = TurnOutcome("error", kind, message)
                        finish_markdown()
                        if streaming:
                            print()
                        streaming = False
                        sp.__exit__()
                        print(YELLOW(f"  ⚠ {message}"))
                    elif outcome.status == "running":
                        outcome = TurnOutcome("ok")
                    finish_markdown()
                    if streaming:
                        print()
                    break
        except KeyboardInterrupt:
            outcome = TurnOutcome(
                "interrupted", "keyboard_interrupt", "用户中断")
            finish_markdown()
            print(DIM("\n  [已中断]"))
        finally:
            watch.stop()
            if outcome.status == "running":
                outcome = TurnOutcome(
                    "protocol_error", "missing_terminal_event",
                    "Agent stream ended without end/error/interrupted")
            self.last_turn_outcome = outcome
            finish_markdown()
            sp.__exit__()
            watcher.__exit__()
            self.watcher = None
            self._notify_turn_complete(outcome)
        ctx = getattr(self, "last_ctx", 0)
        # 上限未知时不显示分母和百分比 —— 拿保守默认值当分母算出的比例是假的，
        # 与其给个会误导的数字，不如只报确定知道的用量。
        # 分母一律用**压缩阈值**而不是模型窗口：窗口是能力，阈值才是真正被
        # 强制执行的天花板。早先这里用窗口，1M 的模型会显示「才用了 1%」，
        # 然后在 18% 处毫无预兆地被压缩 —— 显示的分母和生效的分母是两个数。
        ca = self.ag.compact_at
        if ctx is None:
            # 本轮没拿到 usage（provider 报错、请求被拒等）。**不能拿 0 冒充** ——
            # 「0/936,000 (0%)」看起来像上下文被清空了，比不显示更误导。
            # 早先这里直接 f"{ctx:,}"，遇到 None 会抛 TypeError，把一次网关
            # 503 变成一整段 Python traceback。
            head = "上下文 本轮未知"
        else:
            head = (f"{ctx:,}/{ca:,} tokens 上下文 "
                    f"({100 * ctx / ca if ctx else 0:.0f}%)")
        if not getattr(self.ag, "ctx_known", True):
            head += "（上限未测）"
        print(DIM(f"\n  {head}   "
                  f"in {self.ag.tokens_in:,} / out {self.ag.tokens_out:,}"))
        print(status_line(self))
        return outcome

    def save(self, title=None):
        self.ensure_current_lease()
        path = store.save_session(
            self.ag,
            title=title,
            owner_id=self.lease_owner_id,
            cwd=self._session_cwd,
            plan_mode=self.plan_mode,
            session_grants=sorted(self.always),
            task_plan=self.task_plan,
            workflow_auto=self.workflow_auto,
            memory_use=self.memory_use,
            memory_generate=self.memory_generate,
            goal_state=self.goal,
        )
        if title is not None:
            self._title = title
        elif not self._title:
            try:
                saved_title = store.load_session(
                    self.ag.session_id).get("title")
                if saved_title and saved_title != "(空会话)":
                    self._title = saved_title
            except (FileNotFoundError, ValueError, KeyError):
                pass
        return path


def _history(limit=200, *, session_id=None, messages=None):
    """给 InputPump 的输入历史。

    传了 `session_id` 就按 chat **分层**（09-20 用户报告：在 chat 里 ↑ 翻到的是
    整个 zylab 的历史，自己的和别的 chat 的交错在一起）：本 chat 的输入排在
    最近处，↑ 先走完它们；更早的位置才是别的 chat 的输入。分层而不是隔绝，
    是因为「冷启动的新 chat 按 ↑ / Ctrl+R 能找回上一个会话的提问」是既有的
    PTY 契约（test_history_and_browse_pty / parity A6），严格隔绝会废掉它。
    本 chat 的部分 = 旧段（从它自己的 user 消息重建——打标记之前的输入无法
    从全局文件归属）+ 带 `session` 标记的记录（含斜杠命令）。
    不传 `session_id` 时保持原行为：canonical JSONL 优先，旧 readline 文件兜底。
    """
    if session_id:
        tagged = [record["input"] for record in store.read_history(
            limit=limit, session_id=session_id)]
        asked = [
            message["content"] for message in (messages or ())
            if isinstance(message, dict) and message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and message["content"].strip()
            # "[…]" 开头的是运行时合成的通知（中断、后台任务等），不是用户敲的。
            and not message["content"].lstrip().startswith("[")]
        # 消息没有时间戳，无法与带标记的记录按时间对齐；但带标记的提问与消息
        # 尾部一一对应，消息里多出来的前缀就是打标记之前的旧段。
        # `/命令` 与 `!shell` 会进历史却不会变成 user 消息，不能算作提问。
        # （被中断的提问、自定义命令展开仍会让计数略偏——后果只是**旧 chat 的
        # 旧段**边界差几条，可接受；打标记之后的输入始终是完整的。）
        prompts = sum(1 for text in tagged if not text.startswith(("/", "!")))
        legacy = asked[:max(0, len(asked) - prompts)]
        own = legacy + tagged
        # 别的 chat 的输入垫在前面（更早的位置）。旧 chat 的提问同时以无标记形式
        # 躺在全局文件里，按文本去重，免得同一句话在两层各出现一次。
        mine = set(own)
        others = [record["input"] for record in store.read_history(limit=limit)
                  if str(record.get("session") or "") != str(session_id)
                  and record["input"] not in mine]
        return (others + own)[-limit:]
    records = store.read_history(limit=limit)
    if records:
        return [record["input"] for record in records]
    try:
        import readline
        n = readline.get_current_history_length()
        return [readline.get_history_item(i) for i in range(max(1, n - limit + 1), n + 1)]
    except Exception:
        return []


def status_line(sess):
    """一行会话信息，类比 codex 底部那条。

    只显示**确定知道**的：模型、网关、上下文（未知就不显示分母）、本会话用量、
    工作目录名。不确定的宁可不显示，也不编数字。
    """
    ag = sess.ag
    title = getattr(sess, "_title", None)
    if not title:
        try:
            title = store.load_session(ag.session_id).get("title")
        except (FileNotFoundError, ValueError, KeyError):
            pass
    if not title or title == "(空会话)":
        title = f"chat {ag.session_id[:8]}"
    gateway = getattr(ag, "gateway", None) or client.GATEWAY
    bits = [
        BOLD(title[:34]),
        BOLD(f"{ag.model}@{gateway}"),
    ]
    pending_route = getattr(sess, "_pending_route", None)
    if pending_route:
        bits.append(BLUE(
            f"next {pending_route['model']}@{pending_route['gateway']}"))
    if getattr(ag, "supports_tools", None) is False:
        bits.append(DIM("仅聊天"))
    context_tokens = getattr(sess, "last_ctx", None)
    if context_tokens is not None:
        usage = f"ctx {context_tokens/1000:.0f}K/{ag.compact_at/1000:.0f}K"
        breaker = getattr(ag, "compaction_breaker_open", None)
        if callable(breaker) and breaker():
            usage = YELLOW(usage + " · 自动压缩暂停")
        elif ag.compact_at and context_tokens >= 0.85 * ag.compact_at:
            usage = YELLOW(usage + " · 将压缩")     # 想自己挑时机就现在 /compact
        bits.append(usage)
    else:
        bits.append(f"ctx {ag.compact_at/1000:.0f}K usable")
    if getattr(sess, "plan_mode", False):
        bits.append(YELLOW("plan"))
    if getattr(sess, "auto", False):
        bits.append(YELLOW("auto"))
    if getattr(sess, "workflow_auto", False):
        bits.append(BLUE("workflow:auto"))
    active_agents = int(getattr(sess, "_active_agent_count", 0) or 0)
    if active_agents:
        bits.append(BLUE(f"{active_agents} agent{'s' if active_agents > 1 else ''}"))
    controller = getattr(sess, "controller", None)
    controller_state = str(getattr(
        getattr(controller, "state", None), "value",
        getattr(controller, "state", "")) or "")
    if controller_state == "TOOL_WAITING_DECISION":
        interaction_id = getattr(controller, "current_interaction_id", None)
        suffix = f" #{str(interaction_id)[-8:]}" if interaction_id else ""
        bits.append(YELLOW("需要拍板" + suffix))
    recovered = tuple(getattr(sess, "_recovered_interactions", ()) or ())
    if recovered:
        bits.append(YELLOW(f"gate recovery {len(recovered)}"))
    goal = getattr(sess, "goal", None)
    if isinstance(goal, dict):
        phase = str(goal.get("phase") or "?")
        try:
            rounds_started = max(0, int(goal.get("rounds_started") or 0))
        except (TypeError, ValueError, OverflowError):
            rounds_started = 0
        try:
            max_rounds = max(0, int(goal.get("max_rounds") or 0))
        except (TypeError, ValueError, OverflowError):
            max_rounds = 0
        rounds = f"{rounds_started}/{max_rounds}"
        activation = str(getattr(sess, "_goal_activation", "disarmed"))
        state_label = (
            phase if activation == "armed" and phase == "active"
            else f"{phase}/{activation}")
        label = f"goal {state_label} {rounds}"
        if activation == "armed" and phase == "active":
            bits.append(GREEN(label))
        elif phase == "complete":
            bits.append(BLUE(label))
        else:
            bits.append(YELLOW(label))
    graft_state = getattr(sess, "_graft_current", None)
    if isinstance(graft_state, dict) and graft_state.get("state"):
        state = str(graft_state["state"])
        action = str(graft_state.get("action") or "")
        label = "graft " + state + (f"/{action}" if action else "")
        if state == "failed":
            bits.append(RED(label))
        elif state == "running":
            bits.append(YELLOW(label))
        else:
            bits.append(BLUE(label))
    if getattr(sess, "_memory_error", None):
        bits.append(RED("memory error"))
    elif getattr(sess, "memory_use", False):
        count = len((getattr(sess, "memory_index", {}) or {}).get(
            "entries") or [])
        bits.append(BLUE(f"memory {count}"))
    completed, total, active = PLANS.progress(
        getattr(sess, "task_plan", None))
    if total:
        plan_label = f"plan {completed}/{total}"
        if active:
            plan_label += " " + tui.truncate_display(active, 22)
        bits.append(YELLOW(plan_label))
    session_cwd = getattr(sess, "_session_cwd", os.getcwd())
    try:
        repo = store.repo_context(session_cwd)
    except Exception:
        repo = {}
    branch = str(repo.get("git_branch") or "")
    if branch:
        branch_label = f"git {branch}"
        if repo.get("git_dirty") is True:
            bits.append(YELLOW(branch_label + " *"))
        else:
            bits.append(BLUE(branch_label))
    sandbox_state = getattr(sess, "_sandbox_status", {}) or {}
    sandbox_marker = str(
        sandbox_state.get("marker") or "SANDBOX UNKNOWN")
    if sandbox_marker.startswith("SANDBOXED"):
        bits.append(GREEN(sandbox_marker))
    elif sandbox_marker == "SANDBOX BLOCKED":
        bits.append(RED(sandbox_marker))
    else:
        bits.append(YELLOW(sandbox_marker))
    network_marker = str(
        sandbox_state.get("network_marker") or "NET UNKNOWN")
    if network_marker == "NET ISOLATED":
        bits.append(GREEN(network_marker))
    elif network_marker == "NET BLOCKED":
        bits.append(RED(network_marker))
    else:
        bits.append(YELLOW(network_marker))
    target = getattr(sess, "attached_agent_id", None)
    if target:
        try:
            record = sess.agent_record()
            state = record.get("state") or "?"
            route = (
                f"{record.get('model') or '?'}@"
                f"{record.get('gateway') or '?'}")
            bits.append(BLUE(
                f"target {record.get('id', target)[:12]} "
                f"{state} {route}"))
        except (AGENTS.AgentRuntimeError, AttributeError):
            bits.append(RED(f"target {str(target)[:12]} unavailable"))
    consult = getattr(sess, "_consult_current", None)
    if (isinstance(consult, dict)
            and consult.get("state") in CONSULTS.ACTIVE_STATES):
        source = consult.get("source_snapshot") or {}
        source_title = str(source.get("title") or source.get("session_id") or "?")
        bits.append(BLUE(
            f"◇ {consult.get('id', 'cs-?')} "
            f"{consult.get('state', 'queued')} ← {source_title[:18]}"))
    workflow = getattr(sess, "_workflow_current", None)
    if (isinstance(workflow, dict)
            and workflow.get("state") in WORKFLOWS.ACTIVE_STATES):
        bits.append(BLUE(
            f"◆ {workflow.get('id', 'wf-?')} "
            f"{workflow.get('stage', 'queued')} "
            f"{int(workflow.get('completed_agents') or 0)}/"
            f"{int(workflow.get('expected_agents') or 0)}"))
    task_target = getattr(sess, "attached_task_id", None)
    if task_target:
        task = sess.task_manager.get(task_target)
        if task is None:
            bits.append(RED(f"task {str(task_target)[:12]} unavailable"))
        else:
            state = (
                task.status.value
                if hasattr(task.status, "value") else str(task.status))
            total = task.stdout_bytes + task.stderr_bytes
            bits.append(BLUE(
                f"task {task.id[:12]} {state} {total / 1024:.1f}KiB"))
    bits.append(DIM(os.path.basename(session_cwd) or "/"))
    return "  " + DIM(" · ").join(bits)


def _goal_status_summary(sess):
    record = getattr(sess, "goal", None)
    if not isinstance(record, dict):
        return "none"
    phase = str(record.get("phase") or "?")
    activation = str(getattr(sess, "_goal_activation", "disarmed"))
    try:
        rounds_started = max(0, int(record.get("rounds_started") or 0))
    except (TypeError, ValueError, OverflowError):
        rounds_started = 0
    try:
        max_rounds = max(0, int(record.get("max_rounds") or 0))
    except (TypeError, ValueError, OverflowError):
        max_rounds = 0
    objective = " ".join(tui.sanitize_terminal_text(
        record.get("objective") or "").splitlines())
    return (f"{phase}/{activation} · "
            f"{rounds_started}/{max_rounds} · "
            f"{tui.truncate_display(objective, 72)}")


def fmt_age(iso):
    """把 ISO 时间显示成「3 分钟前」这种人能读的形式。"""
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso[:16]
    sec = (dt.datetime.now(dt.timezone.utc) - t).total_seconds()
    for lim, div, unit in ((60, 1, "秒"), (3600, 60, "分钟"),
                           (86400, 3600, "小时"), (7*86400, 86400, "天")):
        if sec < lim:
            return f"{int(sec/div)} {unit}前"
    return t.strftime("%m-%d")


_RESUME_REPLAY_MESSAGE_CHARS = 4_000
_RESUME_REPLAY_TOTAL_CHARS = 24_000


def _resume_message_text(message):
    """只取适合重新显示在 terminal 的 user/assistant 文本。"""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip("\r\n")
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts).strip("\r\n")


def _is_internal_goal_message(message, text=None):
    """Whether a canonical user row is an invisible goal continuation."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    value = _resume_message_text(message) if text is None else str(text)
    return GOALS.is_round_prompt(value)


def _agent_message_text(message):
    """投影 child transcript 文本；raw message 始终留在权威记录中。"""
    text = _resume_message_text(message)
    if text:
        if str(message.get("role") or "") == "assistant":
            return AGENTS.project_report(text)
        return text
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return ""
    rows = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "tool")
        args = str(function.get("arguments") or "")
        rows.append(f"{name}  {args[:500]}")
    return "\n".join(rows)


def _clip_resume_text(text, limit):
    if len(text) <= limit:
        return text
    head = max(1, limit * 2 // 3)
    tail = max(1, limit - head)
    omitted = max(0, len(text) - head - tail)
    return (
        text[:head]
        + f"\n… [省略 {omitted:,} 字符] …\n"
        + text[-tail:])


def format_resume_replay(
        messages, limit=20, *,
        message_chars=_RESUME_REPLAY_MESSAGE_CHARS,
        total_chars=_RESUME_REPLAY_TOTAL_CHARS):
    """构造有界的 session preview；真正 resume 不走这条路径。"""
    try:
        replay_limit = max(0, int(limit))
    except (TypeError, ValueError):
        replay_limit = 20
    if replay_limit == 0:
        return ""

    visible = []
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _resume_message_text(message)
        if _is_internal_goal_message(message, text):
            continue
        if text:
            visible.append((role, text))
    if not visible:
        return ""

    try:
        per_message = max(200, int(message_chars))
        total_budget = max(per_message, int(total_chars))
    except (TypeError, ValueError):
        per_message = _RESUME_REPLAY_MESSAGE_CHARS
        total_budget = _RESUME_REPLAY_TOTAL_CHARS

    selected = []
    used = 0
    for role, text in reversed(visible[-replay_limit:]):
        clipped = _clip_resume_text(text, per_message)
        if selected and used + len(clipped) > total_budget:
            break
        selected.append((role, clipped))
        used += len(clipped)
    selected.reverse()
    if not selected:
        return ""

    omitted = len(visible) - len(selected)
    header = f"  ── 会话预览：最近 {len(selected)} 条消息"
    if omitted:
        header += f"（另有 {omitted} 条未展开）"
    header += " ──\r\n"
    blocks = [DIM(header)]
    for role, text in selected:
        prefix = BOLD("›") if role == "user" else ORANGE("⏺")
        body = text.replace("\r\n", "\n").replace("\r", "\n")
        body = body.replace("\n", "\r\n  ")
        blocks.append(f"\r\n{prefix} {body}\r\n")
    blocks.append(DIM("  ── 预览结束 ──\r\n"))
    return "".join(blocks)


def show_session_preview(sess, messages):
    """picker 里的"预览"：不切换会话，只把最近几条按实时输出的形状放出来看一眼。"""
    limit = int((getattr(sess, "cfg", None) or {}).get("resume_replay", 20) or 20)
    renderer = getattr(sess, "renderer", None)
    if renderer is not None:
        renderer.write_output(DIM(f"  ── 预览：最近 {min(len(messages or ()), limit)} 条 ──") + "\r\n")
    return replay_transcript(sess, messages, limit=limit)


def _format_resume_tail(messages, limit=20):
    """Plain fallback for non-interactive renderers; no replay banners."""
    visible = []
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _resume_message_text(message)
        if _is_internal_goal_message(message, text):
            continue
        if text:
            visible.append((role, text))
    blocks = []
    for role, text in visible[-max(1, int(limit)):]:
        prefix = BOLD("›") if role == "user" else ORANGE("⏺")
        body = _clip_resume_text(
            text, _RESUME_REPLAY_MESSAGE_CHARS).replace(
                "\r\n", "\n").replace("\r", "\n")
        blocks.append(
            f"{prefix} " + body.replace("\n", "\r\n  ") + "\r\n")
    return "\r\n".join(blocks)


def replay_transcript(sess, messages, *, limit=None):
    """恢复会话：整段对话按**实时输出同一套形状**放回终端——用户行 commit_input、助手 ⏺ + Markdown、
    工具调用 tool_call_line、工具结果 show_result。没有单独的"回放格式"，像这个会话从没退出过。
    renderer 还没建（启动时 --resume）就先记下，repl() 起来后补放。"""
    renderer = getattr(sess, "renderer", None)
    if renderer is None:
        sess._replay_pending = list(messages or ())
        return False
    seq = [m for m in (messages or ()) if isinstance(m, dict)]
    if limit:
        seq = seq[-int(limit):]
    commit = getattr(renderer, "commit_input", None)
    finish = getattr(renderer, "finish_output_line", None)
    names = {}
    shown = 0
    for message in seq:
        role = message.get("role")
        if role == "user":
            text = _resume_message_text(message)
            if not text or _is_internal_goal_message(message, text):
                continue
            if callable(commit):
                commit("› ", text)
            else:
                renderer.write_output(DIM("› " + text) + "\r\n")
            shown += 1
        elif role == "assistant":
            text = _resume_message_text(message)
            if text:
                md = tui.StreamingMarkdown(styled=True, icon_hosted=True)
                renderer.write_output(ORANGE("⏺ ") + md.feed(text) + md.finish() + "\r\n")
                if callable(finish):
                    finish()
                shown += 1
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = str(fn.get("name") or "tool")
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except ValueError:
                    args = {"raw": str(raw)[:100]}
                if not isinstance(args, dict):
                    args = {"raw": str(args)[:100]}
                names[str(call.get("id") or "")] = name
                renderer.write_output(tool_call_line(name, args) + "\r\n")
                shown += 1
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            name = str(message.get("name") or names.get(call_id) or "tool")
            show_result(name, _resume_message_text(message), tool_call_id=call_id or None)
            shown += 1
    return shown > 0


def show_resumed_transcript(sess, messages):
    """Resume into the normal chat surface —— 与实时输出同一条渲染路径。"""
    return replay_transcript(sess, messages)

# ---------------------------------------------------------------- away recap
# 照搬 Claude Code 的 awaySummary：模型现写的一行「总目标 + 当前任务 + 下一步」。
# 以前这里是从落盘数据拼出来的多行结构块（目标/已定/已否/计划/未竟），零调用，
# 但应付不了开放式的会话——没有 task_plan、没撞到压缩阈值的会话它就无话可说。
AWAY_RECAP_HINT = "（不想要自动 recap：settings.json 里 recap_auto 设为 false）"


def render_away_recap(text):
    """一行、整体 dim：recap 是给人扫一眼的路标，不和模型正文抢注意力。"""
    return DIM(f"  ※ recap · {text}") + "\r\n"


def render_memory_saved(rows):
    """自动记忆落盘后的一行回执。写了什么必须看得见，否则等于偷偷改行为。"""
    names = "、".join(str(row.get("title") or row["id"]) for row in rows[:3])
    more = f" 等 {len(rows)} 条" if len(rows) > 3 else ""
    return DIM(f"  ※ 记忆 · 记下 {names}{more}（/memory list 查看，/memory forget 撤销）") + "\r\n"


def cmd_recap(sess, rest):
    """立刻让模型现写一行 recap（Claude Code 的 /recap）。"""
    action = str(rest or "").strip().lower()
    if action in {"help", "?"}:
        print(DIM("  /recap   让模型现写一行：总目标、当前任务、下一步"
                  "（一次模型调用，Esc 取消）"))
        print(DIM("  离开终端 5 分钟以上会自动写好等你回来；resume 后也会现写一条。"))
        print(DIM("  写 recap 的就是这个窗口此刻在用的模型（/model 切了它就跟着变）。"))
        print(DIM("  settings.json：recap_auto 关掉自动触发 · recap_away_seconds "
                  "改离开时长"))
        return
    if not AWAY_RECAP.real_user_messages(sess.ag.messages):
        print(DIM("  [还没有可 recap 的内容 —— 先发一条消息]"))
        return
    result = sess.generate_recap_now()
    kind = result.get("kind")
    if kind == "ok":
        sys.stdout.write(
            render_away_recap(result["text"]).replace("\r\n", "\n"))
        sess.ag.record_away_recap(result["text"])
        sess.save()
    elif kind == "aborted":
        print(DIM("  [recap 已取消]"))
    elif kind == "no-turn":
        print(DIM("  [还没有可 recap 的内容 —— 先发一条消息]"))
    else:
        reason = " ".join(str(result.get("text") or "").split())[:200]
        print(YELLOW("  [recap 没写出来" + (f"：{reason}" if reason else "：模型返回了空正文") + "]"))


def show_recap(sess, record):
    """resume 的回放/横幅之后给一条 recap：有现成的直接给，否则后台现写。"""
    request = getattr(sess, "request_resume_recap", None)
    return request() if callable(request) else None


def _agent_spawn_notice(payload):
    """Render the correct workspace affordance for one spawned runtime."""
    payload = payload if isinstance(payload, dict) else {}
    run_id = str(payload.get("id") or "?")[:12]
    state = str(payload.get("state") or "queued")
    if payload.get("kind") == CONSULTS.KIND:
        source = payload.get("source_snapshot") or {}
        title = " ".join(str(
            source.get("title") or source.get("session_id") or "?").split())
        return (
            f"    ↳ consult {run_id} ← {tui.truncate_display(title, 30)} · "
            f"{state} · /consult status\r\n")
    return f"    ↳ agent {run_id} · {state} · Ctrl+T 查看\r\n"

_AGENT_PEEK_MESSAGES = 18
_AGENT_PEEK_MESSAGE_CHARS = 3_000
_AGENT_PEEK_TOTAL_CHARS = 18_000


def format_agent_preview(record):
    """构造 bounded child transcript 预览；权威 raw transcript 不裁剪。"""
    transcript = record.get("transcript") or {}
    visible = []
    for message in transcript.get("messages") or ():
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role not in {"user", "assistant", "tool"}:
            continue
        text = _agent_message_text(message)
        if text:
            visible.append((role, text))

    selected = []
    used = 0
    for role, text in reversed(visible[-_AGENT_PEEK_MESSAGES:]):
        clipped = _clip_resume_text(text, _AGENT_PEEK_MESSAGE_CHARS)
        if selected and used + len(clipped) > _AGENT_PEEK_TOTAL_CHARS:
            break
        selected.append((role, clipped))
        used += len(clipped)
    selected.reverse()

    run_id = str(record.get("id") or "?")
    state = str(record.get("state") or "?")
    name = str(record.get("name") or record.get("task") or "child-agent")
    is_consult = record.get("kind") == CONSULTS.KIND
    route = (
        f"{record.get('model') or '?'}@{record.get('gateway') or '?'}")
    pending = sum(
        1 for item in record.get("inbox") or []
        if item.get("state") == "queued")
    kind_label = "consult" if is_consult else "agent"
    header = (
        f"  ── {kind_label} {run_id} · {state} · {route} · "
        f"{pending} pending ──\r\n")
    blocks = [DIM(header)]
    if is_consult:
        source = record.get("source_snapshot") or {}
        source_id = str(source.get("session_id") or "?")
        snapshot = str(source.get("snapshot_sha256") or "?")[:12]
        blocks.append(DIM(
            f"  source {source_id} · snapshot {snapshot}\r\n"))
    else:
        blocks.append(DIM(f"  {name}\r\n"))
    omitted = len(visible) - len(selected)
    if omitted:
        blocks.append(DIM(f"  … {omitted} 条更早记录未展开\r\n"))
    labels = {
        "user": BOLD("›"),
        "assistant": ORANGE("⏺"),
        "tool": BLUE("↳ tool"),
    }
    for role, text in selected:
        body = text.replace("\r\n", "\n").replace("\r", "\n")
        body = body.replace("\n", "\r\n  ")
        blocks.append(f"\r\n{labels[role]} {body}\r\n")
    if not selected:
        blocks.append(DIM("  (还没有可显示的 transcript)\r\n"))
    blocks.append(DIM(f"  ── {kind_label} preview 结束 ──\r\n"))
    return "".join(blocks)


def show_agent_preview(sess, identifier):
    record = sess.agent_record(identifier, refresh=True)
    sess._assert_agent_parent(record)
    block = format_agent_preview(record)
    if sess.renderer is not None:
        sess.renderer.clear_input()
        sess.renderer.write_output(block)
    else:
        sys.stdout.write(block)
        sys.stdout.flush()
    return record


_AGENT_STATE_MARK = {
    "queued": "…",
    "running": "●",
    "waiting": "◌",
    "completed": "✓",
    "failed": "!",
    "cancelled": "×",
}


def _rows_signature(rows):
    try:
        return json.dumps(rows, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(rows)


_ROLE_MARK = {"review": "⚖", "synthesis": "◆", "scout": "◇"}


def _workflow_panel_layout(record):
    """把 workflow 记录的 DAG 排成层：返回 (按层级排序的节点列表, key→层级)。"""
    nodes = (record.get("plan") or {}).get("agents") or []
    by_key = {str(node.get("key")): node for node in nodes}
    depth = {}

    def level(key, trail=()):
        if key in depth:
            return depth[key]
        node = by_key.get(key) or {}
        deps = [d for d in (node.get("depends_on") or []) if d in by_key and d not in trail]
        depth[key] = 0 if not deps else 1 + max(level(d, trail + (key,)) for d in deps)
        return depth[key]

    order = sorted(
        by_key, key=lambda key: (level(key), list(by_key).index(key)))
    return [by_key[key] for key in order], depth


def _workflow_panel_rows(sess):
    """/agents 面板的 workflow 部分：头行 + 节点信息（按 agent_id 关联 child 行）。"""
    manager = getattr(sess, "workflow_manager", None)
    session_id = getattr(getattr(sess, "ag", None), "session_id", None)
    if manager is None or not session_id:
        return None, {}, []
    try:
        record = manager.current(str(session_id))
    except Exception:                                   # noqa: BLE001
        return None, {}, []
    if not isinstance(record, dict):
        return None, {}, []
    nodes, depth = _workflow_panel_layout(record)
    if not nodes:
        return None, {}, []
    workflow_id = str(record.get("id") or "wf-?")
    header = {
        "id": f"workflow:{workflow_id}", "kind": "workflow",
        "state": str(record.get("state") or "?"), "workflow": record,
        "updated_at": record.get("updated_at") or "",
    }
    by_agent = {}
    placeholders = []
    for position, node in enumerate(nodes):
        key = str(node.get("key") or "?")
        info = {
            "key": key, "role": str(node.get("role") or "scout"),
            "level": int(depth.get(key, 0)), "order": position,
            "deps": list(node.get("depends_on") or []),
            "seat": node.get("seat"), "node_state": str(node.get("state") or "queued"),
            "workflow_id": workflow_id,
        }
        agent_id = str(node.get("agent_id") or "")
        if agent_id:
            by_agent[agent_id] = info
        else:
            placeholders.append({
                "id": f"node:{workflow_id}:{key}", "kind": "workflow-node",
                "state": info["node_state"], "name": str(node.get("task") or "")[:40],
                "model": node.get("model"), "gateway": node.get("gateway"),
                "updated_at": record.get("updated_at") or "", "node": info,
            })
    return header, by_agent, placeholders


def _agent_panel_rows(sess):
    """面板行：workflow 头行 → 节点行（按依赖层级）→ 未启动节点占位 → 其他 child。"""
    rows = [dict(row) for row in sess.list_agents()]
    header, by_agent, placeholders = _workflow_panel_rows(sess)
    if header is None:
        return rows
    node_rows, others = [], []
    for row in rows:
        info = by_agent.get(str(row.get("id") or ""))
        if info is not None:
            row["node"] = info
            node_rows.append(row)
        else:
            others.append(row)
    node_rows.sort(key=lambda row: (row["node"]["level"], row["node"]["order"]))
    placeholders.sort(key=lambda row: (row["node"]["level"], row["node"]["order"]))
    return [header, *node_rows, *placeholders, *others]


def _agent_panel_row(row):
    if row.get("kind") == "workflow":
        record = row.get("workflow") or {}
        nodes = (record.get("plan") or {}).get("agents") or []
        done = sum(1 for node in nodes if node.get("state") == "completed")
        budget = record.get("budget") or {}
        preflight = record.get("preflight") or []
        replaced = [item for item in preflight if item.get("replaced_by")]
        probe = ""
        if record.get("stage") == "preflight":
            probe = " · 探活中"
        elif replaced:
            probe = " · 探活换路 " + ",".join(
                f"{item.get('seat') or item.get('model')}→{item['replaced_by']}"
                for item in replaced)
        elif preflight:
            probe = " · 探活 ✓"
        return (
            f"◆ {tui.pad_display(str(record.get('state') or '?'), 9)} "
            f"{tui.pad_display(str(record.get('id') or 'wf-?'), 12)} "
            f"节点 {done}/{len(nodes)} · req "
            f"{int(record.get('requests_started') or 0)}/"
            f"{budget.get('max_requests') or '?'}{probe} · "
            f"{_workflow_elapsed_text(record)} · "
            "Enter console · p 暂停 · r 恢复 · x 取消")
    node = row.get("node")
    state = str(row.get("state") or "?")
    mark = _AGENT_STATE_MARK.get(state, "?")
    run_id = str(row.get("id") or "?")
    name = " ".join(
        str(row.get("name") or row.get("task") or "child-agent").split())
    route = f"{row.get('model') or '?'}@{row.get('gateway') or '?'}"
    pending = int(row.get("pending") or 0)
    active = " · active" if row.get("workspace_active") else ""
    queued = f" · {pending} pending" if pending else ""
    if node:
        # DAG 节点：按层级缩进、角色图标、行尾标依赖；节点行仍是 child 行，peek/attach 照常
        indent = "  " * min(int(node.get("level") or 0), 4)
        role_mark = _ROLE_MARK.get(str(node.get("role") or ""), "◇")
        deps = ",".join(node.get("deps") or [])
        label = f"{indent}{role_mark} {node.get('key')}"
        if name and name != "child-agent":
            label += f" · {name}"
        if row.get("kind") == "workflow-node":
            run_id = "—"
        return (
            f"{mark} {tui.pad_display(state, 9)} "
            f"{tui.pad_display(run_id, 12)} "
            f"{tui.pad_display(label, 30)} "
            f"{tui.pad_display(route, 28)}"
            + (f" ← {deps}" if deps else "")
            + f"{queued}{active}")
    return (
        f"{mark} {tui.pad_display(state, 9)} "
        f"{tui.pad_display(run_id, 12)} "
        f"{tui.pad_display(name, 30)} "
        f"{tui.pad_display(route, 28)} · "
        f"{fmt_age(row.get('updated_at') or '')}"
        f"{queued}{active}")


def _workflow_summary_line(sess):
    """/agents 顶部一行 workflow 摘要：统一观测入口（批 2）。"""
    manager = getattr(sess, "workflow_manager", None)
    session_id = getattr(getattr(sess, "ag", None), "session_id", None)
    if manager is None or not session_id:
        return ""
    try:
        record = manager.current(str(session_id))
    except Exception:                                   # noqa: BLE001
        return ""
    if (not isinstance(record, dict)
            or record.get("state") not in WORKFLOWS.ACTIVE_STATES):
        return ""
    nodes = (record.get("plan") or {}).get("agents") or []
    done = sum(1 for node in nodes if node.get("state") == "completed")
    budget = record.get("budget") or {}
    return DIM(
        f"  ◆ workflow {record.get('id')} · {record.get('state')} · "
        f"节点 {done}/{len(nodes) or '?'} · req "
        f"{int(record.get('requests_started') or 0)}/"
        f"{budget.get('max_requests') or '?'} · "
        "/agents workflow status|console|pause|resume|cancel")


def print_agents(sess, rows=None):
    rows = sess.list_agents() if rows is None else rows
    summary = _workflow_summary_line(sess)
    if summary:
        print(summary)
    if not rows:
        print(DIM("  (当前会话还没有 child agent)"))
        return False
    for row in rows:
        attached = (
            GREEN("*") if row.get("id") == sess.attached_agent_id else " ")
        print(f"{attached} {_agent_panel_row(row)}")
    print(DIM(
        "  (* = 当前 target · /agents peek|attach|send <id> · "
        "/agents detach)"))
    return True


def open_agents_panel(sess):
    """Ctrl+T 与裸 /agents 共用的 in-process workspace panel。

    workflow 有活动时是 DAG 视图：头行（节点 done/total、预算、探活）+ 按依赖层级排列
    的节点行（角色图标、← 依赖）+ 未启动节点占位 + 其他 child。开着时每秒刷新，光标
    按 id 跟随；头行 Enter 打开 console，p/r/x 暂停/恢复/取消；筛选按 / 进入。
    """
    state = None
    while True:
        rows = _agent_panel_rows(sess)
        if not rows:
            if sess.renderer is not None:
                sess.renderer.write_output(
                    DIM("  [当前会话还没有 child agent]\r\n"))
            else:
                print(DIM("  (当前会话还没有 child agent)"))
            return None
        has_workflow = rows[0].get("kind") == "workflow"
        actions = {"enter": "attach", "space": "peek"}
        controls = (
            "Click/Enter 打开线程 · Space 预览 · "
            "滚轮/↑↓ 导航 · Esc 返回")
        if has_workflow:
            actions.update({"c": "console", "p": "pause", "r": "resume", "x": "cancel"})
            controls = (
                "Enter 打开线程/console · Space 预览 · c console · "
                "p 暂停 · r 恢复 · x 取消 · / 筛选 · Esc 返回")
        action, picked, state = sess.pick(
            rows,
            title=f"Agent workspace · parent {sess.ag.session_id}",
            page=12, allow_filter=True,
            render=_agent_panel_row,
            actions=actions,
            controls=controls,
            fullscreen=True, mouse=True, mouse_action="attach",
            return_state=True,
            explicit_filter=has_workflow,
            row_key=lambda row: str(row.get("id") or ""),
            initial_state=state,
            refresh=(lambda: _agent_panel_rows(sess)) if has_workflow else None,
            refresh_interval=1.0)
        if picked is None:
            return None
        kind = str(picked.get("kind") or "")
        if action in {"console", "pause", "resume", "cancel"} or kind == "workflow":
            workflow_id = (
                str((picked.get("workflow") or {}).get("id") or "")
                if kind == "workflow"
                else str((picked.get("node") or {}).get("workflow_id") or ""))
            sub = action if action in {"pause", "resume", "cancel"} else "console"
            if action in {"peek", None} and kind == "workflow":
                sub = "console"
            cmd_workflow(sess, f"{sub} {workflow_id}".strip())
            continue
        if kind == "workflow-node":
            notice = f"  [节点 {(picked.get('node') or {}).get('key')} 尚未启动，没有线程可看]\r\n"
            if sess.renderer is not None:
                sess.renderer.write_output(DIM(notice))
            else:
                print(DIM(notice.strip()))
            continue
        try:
            if action == "peek":
                show_agent_preview(sess, picked["id"])
                continue
            show_agent_preview(sess, picked["id"])
            record = sess.attach_agent(picked["id"])
        except (AGENTS.AgentRuntimeError, ValueError) as exc:
            if sess.renderer is not None:
                sess.renderer.write_output(
                    RED(f"  [agents] {exc}\r\n"))
            else:
                print(RED(f"  [agents] {exc}"))
            continue
        return record


def print_sessions(rows, current=None):
    if not rows:
        print(DIM("  (还没有保存的会话)"))
        return
    for i, r in enumerate(rows, 1):
        mark = GREEN("*") if r["id"] == current else " "
        cwd = r["cwd"].replace(os.path.expanduser("~"), "~")
        print(
            f"{mark}{i:>3}. {tui.pad_display(BOLD(r['title']), 52)}  "
            f"{DIM(fmt_age(r['updated']))}")
        print(DIM(f"      {r['model']}@{r['gateway']} · {r['count']} 条 · {cwd} · {r['id']}"))
    print(DIM("  (* = 当前 · /resume <序号|id> 切换)"))


def _session_panel_row(row):
    live = "●" if row.get("live") else " "
    depth = max(0, int(row.get("branch_depth") or 0))
    branch_prefix = ("  " * min(depth - 1, 2) + "↳ ") if depth else ""
    title = branch_prefix + " ".join(
        str(row.get("title") or "(无标题)").split())
    place = Path(
        row.get("repo_root") or row.get("cwd") or "?").name or "/"
    branch = str(row.get("git_branch") or "-")
    return (
        f"{live} {tui.pad_display(title, 44)}  "
        f"{tui.pad_display(fmt_age(row.get('updated', '')), 3, align='right')}  "
        f"{tui.pad_display(place, 16)}  {tui.pad_display(branch, 14)}  "
        f"{int(row.get('count') or 0):>4} msg"
    )


def _session_search_text(row):
    return " ".join(str(row.get(name) or "") for name in (
        "title", "id", "cwd", "repo_root", "git_branch",
        "model", "gateway",
    ))


def _session_notice(sess, message, *, error=False):
    text = RED(message) if error else DIM(message)
    if getattr(sess, "renderer", None) is not None:
        sess.renderer.write_output(f"  {text}\r\n", source="status")
    else:
        print(f"  {text}")


def open_sessions_panel(sess):
    """Persistent M4a catalog browser; transcript reads happen only on action."""
    status = "active"
    scope = "current"
    branch_only = False
    state = {}
    while True:
        rows = store.list_session_summaries(
            limit=None,
            status=status,
            scope=scope,
            branch_only=branch_only,
        )
        scope_label = (
            "all projects" if scope == "all"
            else ("current branch" if branch_only else "current repo"))
        title = (
            f"Resume session · {scope_label} · "
            f"{'archived' if status == 'archived' else 'active'}"
        )
        if status == "active":
            controls = (
                "↑↓ · Enter resume · Space preview · R rename · "
                "A archive · F fork · Tab archived · Ctrl+B branch · "
                "Ctrl+A all · Esc"
            )
        else:
            controls = (
                "↑↓ · Space preview · R rename · U unarchive · F fork · "
                "Tab active · Ctrl+B branch · Ctrl+A all · Esc"
            )
        action, picked, state = sess.pick(
            rows,
            title=title,
            page=12,
            allow_filter=True,
            allow_empty=True,
            explicit_filter=True,
            initial_state=state,
            row_key=lambda row: row["id"],
            filter_text=_session_search_text,
            render=_session_panel_row,
            actions={
                "enter": "resume",
                "space": "preview",
                "R": "rename",
                "A": "archive",
                "U": "unarchive",
                "F": "fork",
                "tab": "toggle-status",
                "ctrl-a": "toggle-scope",
                "ctrl-b": "toggle-branch",
            },
            controls=controls,
            return_state=True,
        )
        if action is None and picked is None:
            return None
        if action == "toggle-status":
            status = "archived" if status == "active" else "active"
            continue
        if action == "toggle-scope":
            scope = "all" if scope == "current" else "current"
            if scope == "all":
                branch_only = False
            continue
        if action == "toggle-branch":
            branch_only = not branch_only
            if branch_only:
                scope = "current"
            continue
        if picked is None:
            _session_notice(sess, "当前范围没有会话；Ctrl+A 可切换所有项目")
            continue
        session_id = picked["id"]
        if action == "preview":
            try:
                record = store.load_session_preview(session_id)
                _session_notice(
                    sess,
                    f"[preview {session_id} · {record.get('title', '')}]",
                )
                show_session_preview(sess, record.get("messages") or [])
            except (OSError, ValueError, store.SessionStoreError) as exc:
                _session_notice(sess, str(exc), error=True)
            continue
        if action == "rename":
            title_value = sess.prompt_text(
                prompt="Rename › ",
                initial=str(picked.get("title") or ""),
                title=f"Rename {session_id}",
                hint="Enter 保存 · Esc 取消",
            )
            if title_value is None:
                continue
            try:
                owner = (
                    sess.lease_owner_id
                    if session_id == sess.ag.session_id else None)
                updated = store.rename_session(
                    session_id, title_value, owner_id=owner)
                if session_id == sess.ag.session_id:
                    sess._title = updated["title"]
                _session_notice(
                    sess, f"[已命名：{updated['title']}]")
            except (OSError, ValueError, store.SessionStoreError) as exc:
                _session_notice(sess, str(exc), error=True)
            continue
        if action == "archive":
            if session_id == sess.ag.session_id:
                _session_notice(
                    sess, "当前正在运行的会话不能归档；先切换到其他会话",
                    error=True,
                )
                continue
            try:
                store.archive_session(session_id)
                _session_notice(sess, f"[已归档 {session_id}]")
            except (OSError, ValueError, store.SessionStoreError) as exc:
                _session_notice(sess, str(exc), error=True)
            continue
        if action == "unarchive":
            try:
                store.unarchive_session(session_id)
                _session_notice(sess, f"[已取消归档 {session_id}]")
            except (OSError, ValueError, store.SessionStoreError) as exc:
                _session_notice(sess, str(exc), error=True)
            continue
        if action == "fork":
            try:
                child = fork_session_record(
                    sess, {"id": session_id}, cwd=os.getcwd(), replay=True)
            except (OSError, RuntimeError, ValueError,
                    store.SessionStoreError) as exc:
                _session_notice(sess, str(exc), error=True)
                continue
            return {"id": child["id"], "_already_resumed": True}
        if action == "resume":
            if status == "archived":
                _session_notice(
                    sess, "归档会话需先按 U unarchive 才能 resume")
                continue
            # Transcript must be read only after resume owns the target lease.
            return {"id": session_id}


def _fmt_ms(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{number / 1000:.2f}s" if number >= 1000 else f"{number:.0f}ms"


def _parse_utc(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _elapsed_ms(start, end):
    left, right = _parse_utc(start), _parse_utc(end)
    if left is None or right is None:
        return None
    return max(0.0, (right - left).total_seconds() * 1000)


def _show_model_health(rest=""):
    target = str(rest or "").strip()
    gateway = target if target in client.GATEWAYS else None
    needle = "" if gateway else target.lower()
    try:
        metrics = store.metrics_facade()
        rows = metrics.list_health(gateway=gateway, limit=1000)
    except Exception as exc:  # noqa: BLE001 - diagnostic command
        print(RED(f"  model health DB 不可用：{type(exc).__name__}: {exc}"))
        return
    if needle:
        rows = [row for row in rows if needle in (
            f"{row.get('model', '')}@{row.get('gateway', '')}".lower())]
    if not rows:
        print(DIM("  (没有匹配的 model health 记录)"))
        return

    health_layout = _table_layout((
        tui.TableColumn(38, min_width=18),
        tui.TableColumn(
            11, min_width=7, priority=3, optional=True),
        tui.TableColumn(8, min_width=6),
        tui.TableColumn(
            8, min_width=6, priority=4, optional=True),
        tui.TableColumn(
            8, min_width=6, priority=5, optional=True),
        tui.TableColumn(
            8, min_width=6, priority=7, optional=True),
        tui.TableColumn(
            9, min_width=6, priority=2, optional=True),
        tui.TableColumn(
            20, min_width=8, priority=6, optional=True),
    ), indent=2, maximum=120)
    print(DIM("\n  " + health_layout.row((
        "model@gateway", "probe", "ok/all", "TTFT", "total", "EWMA",
        "circuit", "context"))))
    now = time.time()
    catalog_rows = (M.load().get("models") or {})
    for row in rows[:80]:
        gw, model = str(row["gateway"]), str(row["model"])
        catalog = catalog_rows.get(M.key(gw, model)) or {
            "gateway": gw, "id": model, "context": None,
            "catalog_fingerprint": None,
        }
        try:
            freshness = HEALTH.probe_decision(
                row, catalog_fingerprint=catalog.get("catalog_fingerprint"),
                now=now)
            if freshness.due:
                probe = "due:" + freshness.reason
            else:
                probe = "fresh" if freshness.reason == "fresh" else "stable"
        except Exception:
            probe = "?"
        success = int(row.get("success_count") or 0)
        failure = int(row.get("failure_count") or 0)
        total = success + failure
        rate = f"{success}/{total}" if total else "?"
        try:
            route = HEALTH.route_decision(
                row, explicit=False, now=now)
            circuit = (
                "open" if route.circuit_open and not route.allowed else "closed")
        except Exception:
            circuit = "?"
        capability = row.get("capability") or {}
        context = (
            capability.get("context") if isinstance(capability, dict)
            else None) or catalog.get("context")
        context_source = (
            capability.get("context_source")
            if isinstance(capability, dict) else None
        ) or catalog.get("context_source") or "?"
        context_text = (
            f"{int(context) / 1000:.0f}k/{context_source}"
            if context else "?")
        target_text = f"{model}@{gw}"
        print("  " + health_layout.row((
            target_text,
            probe,
            rate,
            _fmt_ms(row.get("true_ttft_ms")),
            _fmt_ms(row.get("total_latency_ms")),
            _fmt_ms(row.get("ewma_latency_ms")),
            circuit,
            context_text,
        )))
    if len(rows) > 80:
        print(DIM(f"  … 另有 {len(rows) - 80} 条，请加 gateway/关键字筛选"))


def cmd_status(sess, rest):
    if str(rest or "").strip():
        print(DIM("  用法：/status"))
        return
    controller = getattr(sess, "controller", None)
    state = getattr(getattr(controller, "state", None), "value", None) or "idle"
    queue = list(getattr(controller, "queued", ()) or ())
    modes = {}
    for item in queue:
        mode = getattr(getattr(item, "mode", None), "value", None) or "?"
        modes[mode] = modes.get(mode, 0) + 1
    queue_text = (
        f"{len(queue)} · " + ", ".join(
            f"{name}={count}" for name, count in sorted(modes.items()))
        if queue else "0")
    active_tasks = []
    manager = getattr(sess, "task_manager", None)
    if manager is not None:
        active_tasks = [task for task in manager.list()
                        if task.status not in TASKS.TERMINAL_STATES]
    context = getattr(sess, "last_ctx", None)
    context_text = (
        f"{int(context):,} / {int(sess.ag.compact_at):,} tokens"
        if context is not None else f"unknown / {int(sess.ag.compact_at):,} usable")
    request = getattr(controller, "current_request_id", None)
    turn = getattr(controller, "current_turn_id", None)
    latest = None
    metrics_error = None
    try:
        traces = store.metrics_facade().list_recent_traces(
            session_id=sess.ag.session_id, limit=1)
        latest = traces[0] if traces else None
    except Exception as exc:  # noqa: BLE001 - diagnostic command
        metrics_error = f"{type(exc).__name__}: {exc}"
    latest_text = "none"
    if latest:
        phase = next((name.removesuffix("_at") for name in reversed((
            "queued_at", "started_at", "headers_at", "first_delta_at",
            "last_delta_at", "ended_at")) if latest.get(name)), "unknown")
        latest_text = (
            f"{latest['id'][:12]} · {latest.get('status')} · phase={phase} · "
            f"TTFT {_fmt_ms(latest.get('true_ttft_ms'))} · "
            f"total {_fmt_ms(latest.get('total_latency_ms'))}")
    sandbox_state = getattr(sess, "_sandbox_status", {}) or {}
    try:
        repo = store.repo_context(
            getattr(sess, "_session_cwd", os.getcwd()), refresh=True)
    except Exception:
        repo = {}
    if repo.get("git_branch"):
        dirty = repo.get("git_dirty")
        dirty_text = (
            "dirty" if dirty is True else "clean" if dirty is False
            else "unknown")
        repo_text = (
            f"{repo['git_branch']} · {dirty_text} · "
            f"staged={repo.get('git_staged', 0)} "
            f"unstaged={repo.get('git_unstaged', 0)} "
            f"untracked={repo.get('git_untracked', 0)}")
    else:
        repo_text = "not a git worktree"
    rows = [
        ("session", f"{getattr(sess, '_title', None) or '(unnamed)'} · {sess.ag.session_id}"),
        ("route", f"{sess.ag.model}@{getattr(sess.ag, 'gateway', client.GATEWAY)}"),
        ("goal", _goal_status_summary(sess)),
        ("cwd", getattr(sess, "_session_cwd", os.getcwd())),
        ("git", repo_text),
        ("controller", f"{state} · turn={turn or '-'} · request={request or '-'}"),
        ("decision gates", (
            f"active · {getattr(controller, 'current_interaction_id', None)}"
            if getattr(controller, "current_interaction_id", None)
            else (f"{len(getattr(sess, '_recovered_interactions', ()) or ())} stale"
                  if getattr(sess, "_recovered_interactions", ()) else "none"))),
        ("queue", queue_text),
        ("tasks", f"{len(active_tasks)} active"),
        ("context", context_text),
        ("latest trace", latest_text),
        ("sandbox", f"{sandbox_state.get('marker', 'UNKNOWN')} · {sandbox_state.get('network_marker', 'NET UNKNOWN')}"),
    ]
    if metrics_error:
        rows.append(("metrics", "ERROR " + metrics_error))
    print("  Status")
    for label, value in rows:
        print(f"    {label:<14} {value}")


def cmd_trace(sess, rest):
    target = str(rest or "").strip()
    if len(target.split()) > 1:
        print(DIM("  用法：/trace [turn-id-prefix]"))
        return
    try:
        metrics = store.metrics_facade()
        snapshot = metrics.trace_timeline(
            session_id=sess.ag.session_id,
            limit=(10_000 if target else 200),
            turn_id_prefix=(target or None))
        requests = snapshot["requests"]
        tools_rows = snapshot["tools"]
    except Exception as exc:  # noqa: BLE001 - diagnostic command
        print(RED(f"  trace DB 不可用：{type(exc).__name__}: {exc}"))
        return
    timeline = []
    for row in requests:
        stamp = row.get("queued_at") or row.get("started_at") or ""
        detail = (
            f"attempt={row.get('attempt') or '?'} "
            f"purpose={row.get('purpose') or '?'} "
            f"{row.get('model') or '?'}@{row.get('gateway') or '?'} "
            f"status={row.get('status') or '?'} "
            f"TTFT={_fmt_ms(row.get('true_ttft_ms'))} "
            f"total={_fmt_ms(row.get('total_latency_ms'))}")
        queued_ms = _elapsed_ms(row.get("queued_at"), row.get("started_at"))
        generation_ms = _elapsed_ms(
            row.get("first_delta_at"), row.get("last_delta_at"))
        detail += (
            f" queue={_fmt_ms(queued_ms)} gen={_fmt_ms(generation_ms)}")
        if row.get("retry_reason"):
            detail += f" retry={row['retry_reason']}"
        if row.get("error_kind"):
            detail += f" error={row['error_kind']}"
        if row.get("http_status") is not None:
            detail += f" HTTP={row['http_status']}"
        if row.get("error_code"):
            detail += f" code={row['error_code']}"
        if row.get("usage_reported"):
            detail += (
                f" usage={row.get('prompt_tokens') if row.get('prompt_tokens') is not None else '?'}"
                f"/{row.get('completion_tokens') if row.get('completion_tokens') is not None else '?'}")
        timeline.append((stamp, "request", row["id"], row.get("turn_id"), detail))
    for row in tools_rows:
        stamp = row.get("started_at") or ""
        duration = _elapsed_ms(row.get("started_at"), row.get("ended_at"))
        detail = (
            f"{row.get('name') or '?'} status={row.get('status') or '?'} "
            f"approval={row.get('approval_decision') or '?'}"
            f"/{row.get('approval_source') or '?'} "
            f"duration={_fmt_ms(duration)}")
        if row.get("request_id"):
            detail += f" request={str(row['request_id'])[:12]}"
        if row.get("exit_code") is not None:
            detail += f" exit={row['exit_code']}"
        if row.get("signal") is not None:
            detail += f" signal={row['signal']}"
        if row.get("stdout_path"):
            detail += f" stdout={row['stdout_path']}"
        if row.get("stderr_path"):
            detail += f" stderr={row['stderr_path']}"
        timeline.append((stamp, "tool", row["id"], row.get("turn_id"), detail))
    if not timeline:
        print(DIM("  (当前 session 没有匹配的 request/tool trace)"))
        return
    timeline.sort(key=lambda item: (item[0], item[1], item[2]))
    print(DIM(f"\n  Trace · session {sess.ag.session_id}" + (
        f" · turn {target}" if target else "")))
    for stamp, kind, row_id, turn_id, detail in timeline[-80:]:
        clock = str(stamp)[11:23] if stamp else "?"
        print(
            f"  {clock:<12}{kind:<8}{row_id[:12]:<14}"
            f"turn={str(turn_id or '-')[:12]:<13}{detail}")
    if len(timeline) > 80:
        print(DIM(f"  … 较早 {len(timeline) - 80} 条未显示"))


def show_cost(ag):
    """本次会话的用量明细。缓存字段来自 usage.prompt_tokens_details。"""
    el = time.time() - getattr(ag, "started", time.time())
    tot = ag.tokens_in + ag.tokens_out
    print()
    print(f"  {BOLD('本次会话')}  {DIM(ag.session_id)}  {DIM(ag.model)}  "
          f"{DIM('@' + client.GATEWAY)}")
    print(f"    输入        {ag.tokens_in:>12,} tokens")
    print(f"    输出        {ag.tokens_out:>12,} tokens")
    print(f"    合计        {tot:>12,} tokens   {DIM(f'{ag.turns} 次 API 调用')}")
    print()
    if not getattr(ag, "cache_reported", False):
        # 没上报 ≠ 没缓存。kimi-k3-256k 的 prompt_tokens_details 是 null，
        # 但延迟表现说明服务端缓存在工作 —— 只是这个后端不给数字。
        print(f"    缓存        {DIM('该模型不上报缓存字段（deepseek-v4-flash 会报）')}")
    else:
        print(f"    缓存写入    {ag.cache_write:>12,} tokens")
        if ag.cache_read:
            pct = 100 * ag.cache_read / max(ag.tokens_in, 1)
            print(f"    缓存命中    {GREEN(f'{ag.cache_read:>12,}')} tokens   {DIM(f'占输入 {pct:.0f}%')}")
        else:
            print(f"    缓存命中    {ag.cache_read:>12,} tokens   "
                  f"{DIM('（网关只报写入不报命中；延迟显示缓存实际生效）')}")
    print()
    if getattr(ag, "ctx_known", True):
        print(f"    上下文      {ag.last_total:>12,} / {ag.ctx_limit:,}"
              f"   {DIM(f'压缩阈值 {ag.compact_at:,}')}")
    else:
        print(f"    上下文      {ag.last_total:>12,} tokens"
              f"   {DIM(f'上限未知 · 压缩阈值 {ag.compact_at:,}')}")
    free = GREEN("DeepInfer 免费") if client.GATEWAY == "deepinfer" else DIM("Boyue 可报销")
    print(f"    时长        {DIM(f'{el/60:.1f} 分钟')}   {free}")
    print(DIM(f"    完整日志    {A.USAGE_LOG}"))
    print()


def show_context(ag, *, plan_mode=False):
    """解释下一次请求会发送什么，以及每一块预算从哪里来。"""
    controls = (
        [{"role": "system", "content": A.PLAN_PREAMBLE}]
        if plan_mode else None)
    report = ag.context_report(
        tools.SCHEMA, control_messages=controls)
    fit = GREEN("可发送") if report["fits"] else RED("超预算")
    print()
    print(f"  {BOLD('上下文投影')}  {DIM(ag.model)}  {fit}")
    print(f"    模型窗口      {report['model_limit']:>12,} tokens  "
          f"{DIM('source=' + report['model_limit_source'])}")
    print(f"    可用预算      {report['usable_budget']:>12,} tokens")
    print(f"    当前投影      {report['estimated_request_tokens']:>12,} tokens  "
          f"{DIM('estimated=' + report['token_estimator'])}")
    if report["last_provider_tokens"]:
        print(f"    上次实测      {report['last_provider_tokens']:>12,} tokens  "
              f"{DIM('source=provider usage')}")
    print(f"    原始消息      {report['raw_message_count']:>12,}")
    print(f"    投影消息      {report['projected_message_count']:>12,}")
    print()

    labels = (
        ("system", "system / instructions"),
        ("tools_schema", "tool schemas"),
        ("summary", "summary"),
        ("verbatim_users", "user verbatim"),
        ("attachments", "@file expanded text"),
        ("image_inputs", "image inputs (estimated)"),
        ("recent_messages", "assistant / recent"),
        ("tool_results", "tool results (full)"),
        ("tool_previews", "tool previews"),
        ("omission_marker", "omission markers"),
        ("reserved_output", "reserved: output"),
        ("reserved_tools", "reserved: tools"),
        ("reserved_safety", "reserved: safety"),
    )
    print(DIM(f"    {'组成':<24}{'tokens':>12}"))
    for key, label in labels:
        print(f"    {label:<24}{report['components'][key]:>12,}")

    summary = report["summary"]
    if summary["status"] == "valid":
        coverage = f"raw #{summary['covered_from']}..#{summary['covered_to'] - 1}"
        print(f"\n    摘要          {GREEN('valid')}  {coverage}")
    else:
        print(f"\n    摘要          {DIM('none')}  "
              f"{DIM(str(summary.get('invalid_reason') or ''))}")
    verbatim = report["verbatim_users"]
    print(f"    用户原话      {verbatim['count']} 条 · "
          f"{verbatim['tokens']:,} tokens · 未截断")
    attachment = report.get("attachments") or {}
    print(
        f"    附件投影      {int(attachment.get('count') or 0)} 个 · "
        f"text {int(attachment.get('text_count') or 0)} · "
        f"image {int(attachment.get('image_count') or 0)} · "
        f"{int(attachment.get('bytes') or 0):,} bytes")
    if attachment.get("image_count"):
        print(DIM(
            f"      图片预算 {int(attachment.get('image_token_estimate') or 0):,} "
            f"tokens · {attachment.get('image_token_estimator')}"))
    previews = report["tool_previews"]
    print(f"    工具预览      {previews['count']} 个 · "
          f"省 {previews['saved_chars']:,} chars")
    largest = previews["largest_artifacts"]
    print(f"    最大 artifact {len(largest)} 个")
    for artifact in largest[:3]:
        print(DIM(f"      {artifact['artifact']}  {artifact['tool']}  "
                  f"{artifact['tier']}  {artifact['status']}  "
                  f"{artifact['original_chars']:,} chars"))
    if report["incomplete_tool_pairs"]:
        print(YELLOW(f"    协议隔离      {len(report['incomplete_tool_pairs'])} 个不完整工具单元"))
    if report["omitted_ranges"]:
        print(f"    投影省略      {len(report['omitted_ranges'])} 段")
        for item in report["omitted_ranges"][:8]:
            print(DIM(f"      raw #{item['start']}..#{item['end'] - 1}  "
                      f"{item['reason']}"))
    else:
        print(f"    投影省略      0")
    print(f"    raw sha256    {DIM(report['raw_sha256'])}")
    print(f"    下一步        {report['next_action']}")
    print()


def show_usage(days=None, session=None):
    """跨会话汇总 ~/.zylab/usage.jsonl —— 与 cc-usage / codex-usage 对称。"""
    import collections
    log = A.USAGE_LOG
    if not log.is_file():
        print(f"  还没有用量记录（{log} 不存在）")
        return
    recs = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if session:
        recs = [r for r in recs if r.get("session", "").startswith(session)]
    if days:
        import datetime as _dt
        cut = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()
        recs = [r for r in recs if r.get("ts", "") >= cut]
    if not recs:
        print("  没有匹配的记录")
        return

    by_day = collections.defaultdict(lambda: collections.Counter())
    by_model = collections.defaultdict(lambda: collections.Counter())
    for r in recs:
        for key, agg in ((r["ts"][:10], by_day), (r.get("model", "?"), by_model)):
            agg[key]["in"] += r.get("prompt_tokens", 0)
            agg[key]["out"] += r.get("completion_tokens", 0)
            agg[key]["cr"] += r.get("cache_read", 0)
            agg[key]["cw"] += r.get("cache_write", 0)
            agg[key]["n"] += 1

    usage_columns = (
        tui.TableColumn(27, min_width=10, priority=4),
        tui.TableColumn(12, min_width=12, align="right"),
        tui.TableColumn(10, min_width=10, align="right"),
        tui.TableColumn(
            12, min_width=12, align="right", priority=2, optional=True),
        tui.TableColumn(
            10, min_width=10, align="right", priority=3, optional=True),
        tui.TableColumn(7, min_width=7, align="right"),
    )
    usage_layout = _table_layout(
        usage_columns, indent=2, maximum=100, gap="")

    def table(title, agg):
        print(f"\n  {BOLD(title)}")
        print(DIM("  " + usage_layout.row(
            ("", "输入", "输出", "缓存写", "缓存读", "调用"))))
        for key in sorted(agg, reverse=True):
            count = agg[key]
            print("  " + usage_layout.row((
                key,
                f"{count['in']:,}",
                f"{count['out']:,}",
                f"{count['cw']:,}",
                f"{count['cr']:,}",
                f"{count['n']:,}",
            )))

    table("按天", by_day)
    table("按模型", by_model)
    sessions = {r.get("session") for r in recs}
    tot_in = sum(r.get("prompt_tokens", 0) for r in recs)
    tot_out = sum(r.get("completion_tokens", 0) for r in recs)
    print(f"\n  {BOLD('合计')}  {len(sessions)} 个会话 · {len(recs)} 次调用 · "
          f"in {tot_in:,} / out {tot_out:,} tokens   {GREEN('DeepInfer 免费')}")
    print(DIM(f"  日志 {log}\n"))





# ---------------------------------------------------------------- 斜杠命令
# 每个命令一个函数，统一签名 (sess, rest)；返回 'quit' 表示退出 REPL。
# 注册表是**唯一事实源**：/help、命令面板、前缀匹配全部由它生成 ——


def restore_session_record(sess, record, *, refresh_environment=False):
    """把一个保存记录完整恢复到当前 Agent；旧 JSON 字段缺失时保守降级。"""
    messages = record["messages"]
    # A staged route belongs to the active turn of the previous chat and must
    # never leak across /resume or rollback restoration.
    sess._pending_route = None
    if refresh_environment and hasattr(sess.ag, "refresh_environment"):
        # Runtime instructions belong to the selected cwd, not to the saved
        # machine snapshot.  Copy before replacing system metadata so preview
        # callers and the loaded dict stay read-only.
        messages = json.loads(json.dumps(messages, ensure_ascii=False))
    session_id = record["id"]
    gateway = str(record.get("gateway") or "").strip()
    if gateway:
        # model limit 与 provider route 都是 gateway-scoped；必须先切 gateway。
        client.set_gateway(gateway)

    ag = sess.ag
    legacy_load_md = bool(
        record.get("load_md", getattr(ag, "load_md", True)))
    flags = A.instruction_flags(
        legacy_load_md,
        load_user_md=(
            record["load_user_md"]
            if "load_user_md" in record else None),
        load_project_md=(
            record["load_project_md"]
            if "load_project_md" in record else None),
        load_skills=(
            record["load_skills"]
            if "load_skills" in record else None))
    ag.load_md = legacy_load_md
    ag.load_user_md = flags["load_user_md"]
    ag.load_project_md = flags["load_project_md"]
    ag.load_skills = flags["load_skills"]
    ag.messages = messages
    ag.load_context(record.get("context"))
    ag.session_id = session_id
    ag.gateway = client.GATEWAY
    if record.get("model"):
        ag.set_model(record["model"])

    def saved_int(name):
        try:
            return max(0, int(record.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    ag.tokens_in = saved_int("tokens_in")
    ag.tokens_out = saved_int("tokens_out")
    if "last_context_tokens" in record:
        ag.last_total = saved_int("last_context_tokens")
        sess.last_ctx = ag.last_total
    else:
        # 旧 session 没有这个字段；宁可显示 usable，也不把未知冒充 0。
        ag.last_total = 0
        sess.last_ctx = None
    sess._title = record.get("title") or None
    sess._session_cwd = os.path.abspath(
        str(record.get("cwd") or os.getcwd()))
    if refresh_environment and hasattr(ag, "refresh_environment"):
        ag.refresh_environment()
        refresh_skills = getattr(sess, "refresh_skills_context", None)
        if callable(refresh_skills):
            refresh_skills()
    # Legacy records predate plan mode and must resume in the safe normal
    # default, not inherit the mode of whichever chat happened to be open.
    sess.plan_mode = bool(record.get("plan_mode", False))
    sess.task_plan = PLANS.normalize_record(record.get("task_plan"))
    # Goal definition is durable, activation is intentionally not.  A resumed
    # chat can inspect its objective immediately, but must explicitly opt back
    # in with /goal resume before another autonomous provider round.
    sess.goal = GOALS.normalize_record(record.get("goal"))
    # Activation is process-local and deliberately ignored for every loaded
    # record, including private rollback snapshots; otherwise a crafted or
    # stale JSON field could make /resume spend a request without consent.
    sess._goal_activation = "disarmed"
    sess._goal_round_items = {}
    sess._goal_round_started = {}
    restored_controller = getattr(sess, "controller", None)
    if restored_controller is not None and hasattr(
            restored_controller, "internal_dispatch_enabled"):
        restored_controller.internal_dispatch_enabled = False
    if "workflow_auto" in record:
        sess._workflow_auto_override = bool(record.get("workflow_auto"))
        sess.workflow_auto = bool(record.get("workflow_auto"))
    else:
        sess._workflow_auto_override = None
        sess.workflow_auto = bool(
            CFG.workflow_policy(
                getattr(sess, "cfg", CFG.DEFAULTS)).get("auto"))
    memory_policy = CFG.memory_policy(
        getattr(sess, "cfg", CFG.DEFAULTS))
    if "memory_use" in record:
        sess._memory_use_override = bool(record.get("memory_use"))
        sess.memory_use = bool(record.get("memory_use"))
    else:
        sess._memory_use_override = None
        sess.memory_use = bool(memory_policy.get("use"))
    if "memory_generate" in record:
        sess._memory_generate_override = bool(
            record.get("memory_generate"))
        sess.memory_generate = bool(record.get("memory_generate"))
    else:
        sess._memory_generate_override = None
        sess.memory_generate = bool(memory_policy.get("generate"))
    grants = record.get("session_grants") or []
    sess.always = {
        str(item) for item in grants
        if isinstance(item, str) and item.strip()
    }
    _announce_session_grants(sess)
    tools.HOOK_CTX["session"] = str(session_id)
    tools.HOOK_CTX["plan_update"] = getattr(
        sess, "_direct_plan_update", None)
    tools.HOOK_CTX["goal_update"] = getattr(
        sess, "_goal_update_from_tool", None)
    tools.HOOK_CTX["workflow_start"] = getattr(
        sess, "_start_workflow_from_tool", None)
    tools.HOOK_CTX["context_capsule"] = getattr(
        sess, "build_context_capsule", None)
    tools.HOOK_CTX["workspace_changed"] = getattr(
        sess, "_workspace_changed", None)
    refresh_memory = getattr(sess, "refresh_memory_context", None)
    if callable(refresh_memory):
        refresh_memory()
    refresh_goal = getattr(sess, "refresh_goal_context", None)
    if callable(refresh_goal):
        refresh_goal()
    refresh_repo = getattr(sess, "refresh_repo_context", None)
    if callable(refresh_repo):
        refresh_repo()
    refresh_commands = getattr(sess, "refresh_custom_commands", None)
    if callable(refresh_commands):
        refresh_commands(publish=False)
    recover = getattr(sess, "recover_workflows", None)
    if callable(recover):
        recover()
    return messages


def _runtime_session_record(sess):
    """Capture enough in-memory state to roll back a failed resume commit."""
    ag = sess.ag
    return {
        "id": ag.session_id,
        "title": getattr(sess, "_title", None),
        "model": getattr(ag, "model", ""),
        "gateway": getattr(ag, "gateway", client.GATEWAY),
        "load_md": bool(getattr(ag, "load_md", True)),
        "load_user_md": bool(getattr(
            ag, "load_user_md", getattr(ag, "load_md", True))),
        "load_project_md": bool(getattr(
            ag, "load_project_md", getattr(ag, "load_md", True))),
        "load_skills": bool(getattr(
            ag, "load_skills", getattr(ag, "load_md", True))),
        "cwd": getattr(sess, "_session_cwd", os.getcwd()),
        "tokens_in": getattr(ag, "tokens_in", 0),
        "tokens_out": getattr(ag, "tokens_out", 0),
        "last_context_tokens": getattr(ag, "last_total", 0),
        "messages": json.loads(json.dumps(
            getattr(ag, "messages", []), ensure_ascii=False)),
        "context": (
            ag.context_snapshot()
            if hasattr(ag, "context_snapshot") else None),
        "plan_mode": bool(getattr(sess, "plan_mode", False)),
        "task_plan": PLANS.normalize_record(
            getattr(sess, "task_plan", None)),
        "goal": GOALS.normalize_record(getattr(sess, "goal", None)),
        "workflow_auto": bool(
            getattr(sess, "workflow_auto", False)),
        "memory_use": bool(getattr(sess, "memory_use", False)),
        "memory_generate": bool(
            getattr(sess, "memory_generate", False)),
        "session_grants": sorted(getattr(sess, "always", set())),
    }


def fork_session_record(sess, record, *, cwd=None, message_count=None,
                        fork_seq=None, replay=True, save_current=True):
    """Create a canonical branch and transactionally switch into it.

    The parent is never changed.  If the later runtime switch fails, the
    already committed child remains discoverable instead of being silently
    deleted, while its provisional lease is released.
    """
    source_hint = str(
        record.get("id") if isinstance(record, dict) else record)
    source_id = store.resolve_session_id(
        source_hint, include_archived=True)
    if save_current and source_id == str(sess.ag.session_id):
        sess.save()

    child_id = store.new_id()
    acquire = getattr(sess, "acquire_target_lease", None)
    acquired = bool(acquire(child_id)) if callable(acquire) else False
    try:
        child = store.fork_session(
            source_id,
            target_id=child_id,
            message_count=message_count,
            fork_seq=fork_seq,
            cwd=(os.path.abspath(cwd) if cwd is not None else None),
            owner_id=(sess.lease_owner_id if acquired else None),
        )
        switched = resume_session_record(
            sess, {"id": child_id}, cwd_policy="current",
            replay=replay, save_current=save_current)
        if not switched:
            raise RuntimeError(
                f"branch {child_id} 已创建，但 runtime 未切换")
        acquired = False
    except BaseException:
        rollback = getattr(sess, "rollback_target_lease", None)
        if acquired and callable(rollback):
            try:
                rollback(child_id)
            except Exception as rollback_error:  # noqa: BLE001
                store.log(
                    "fork_target_lease_rollback_failed",
                    session=child_id,
                    error=(f"{type(rollback_error).__name__}: "
                           f"{rollback_error}"),
                )
        raise
    _session_notice(
        sess,
        f"[已从 {source_id} fork → {child_id}；session grant 未继承]",
    )
    return child


def _choose_resume_cwd(sess, record, explicit=None):
    current = os.path.abspath(os.getcwd())
    saved_raw = str(record.get("cwd") or "").strip()
    saved = (
        os.path.abspath(os.path.expanduser(saved_raw))
        if saved_raw else current)
    if os.path.realpath(current) == os.path.realpath(saved):
        return "same", current, saved

    aliases = {
        "current": "current",
        "here": "current",
        "saved": "saved",
        "cancel": "cancel",
        "fork": "fork",
    }
    if explicit:
        policy = aliases.get(str(explicit).lower())
        if policy is None:
            raise ValueError(
                "cwd policy 必须是 current、saved、fork 或 cancel")
        return policy, current, saved
    if not sys.stdin.isatty():
        raise ValueError(
            "resume 的 cwd 与当前目录不同；非交互模式必须显式给 "
            "--resume-cwd current、saved 或 fork")

    choices = [
        {
            "policy": "current",
            "label": f"使用当前 cwd · {current}",
        },
        {
            "policy": "saved",
            "label": f"切换到保存 cwd · {saved}",
        },
        {
            "policy": "fork",
            "label": "fork 到当前 cwd · 原会话保持不变",
        },
        {
            "policy": "cancel",
            "label": "取消",
        },
    ]
    picked = sess.pick(
        choices,
        title=f"cwd mismatch · session {record['id']}",
        page=4,
        allow_filter=False,
        render=lambda row: row["label"],
        controls="↑↓ 选择 · Enter 确认 · Esc 取消",
    )
    return (
        (picked["policy"] if picked else "cancel"),
        current,
        saved,
    )


def resume_session_record(sess, record, *, cwd_policy=None, replay=True,
                          save_current=True):
    """Lease first, re-read canonical, then atomically switch runtime."""
    target_hint = str(
        record.get("id") if isinstance(record, dict) else record)
    acquire = getattr(sess, "acquire_target_lease", None)
    target_id = (
        store.resolve_session_id(target_hint)
        if callable(acquire) else target_hint)
    if target_id == str(sess.ag.session_id):
        _session_notice(sess, f"[已经在会话 {target_id}]")
        return True

    previous = _runtime_session_record(sess)
    previous_controller = getattr(sess, "controller", None)
    previous_configuration = (
        sess.configuration_snapshot()
        if hasattr(sess, "configuration_snapshot") else None)
    target_acquired = False
    changed_cwd = False
    runtime_touched = False
    configuration_applied = False
    try:
        if callable(acquire):
            target_acquired = bool(acquire(target_id))
            # The picker/CLI may have inspected this chat before the other
            # terminal's final save.  Only a canonical read performed after
            # lease acquisition is safe to restore and later overwrite.
            record = store.load_session(target_id)
        elif not isinstance(record, dict) or "messages" not in record:
            record = store.load_session(target_id)

        gateway = str(record.get("gateway") or "").strip()
        if gateway:
            client.route_for(gateway)  # validate without mutating global route

        policy, current_cwd, saved_cwd = _choose_resume_cwd(
            sess, record, explicit=cwd_policy)
        if policy == "cancel":
            rollback = getattr(sess, "rollback_target_lease", None)
            if target_acquired and callable(rollback):
                rollback(target_id)
                target_acquired = False
            return False
        if policy == "fork":
            # Hold the source lease until the branch has captured its canonical
            # snapshot.  Releasing it first creates a second-terminal save gap.
            try:
                return bool(fork_session_record(
                    sess, record, cwd=current_cwd, replay=replay,
                    save_current=save_current))
            finally:
                rollback = getattr(sess, "rollback_target_lease", None)
                if target_acquired and callable(rollback):
                    rollback(target_id)
                    target_acquired = False
        if policy == "saved":
            if not os.path.isdir(saved_cwd):
                raise FileNotFoundError(f"保存的 cwd 不存在：{saved_cwd}")
            blocker = (
                sess.cwd_switch_blocker()
                if hasattr(sess, "cwd_switch_blocker") else None)
            if blocker:
                raise RuntimeError(
                    f"不能切换到保存 cwd：{blocker}；"
                    "先等待/终止它，或选择 current cwd")

        target_cwd = (
            saved_cwd if policy == "saved" else current_cwd)
        configuration_bundle = (
            sess.prepare_cwd_configuration(target_cwd)
            if hasattr(sess, "prepare_cwd_configuration") else None)

        if save_current:
            sess.save()
        if policy == "saved":
            os.chdir(saved_cwd)
            changed_cwd = True
        runtime_touched = True
        restore_session_record(
            sess, record, refresh_environment=True)
        sess._session_cwd = (
            current_cwd if policy == "current" else saved_cwd)
        if configuration_bundle is not None:
            sess.apply_cwd_configuration(configuration_bundle)
            configuration_applied = True
        sess.reset_controller()
        stale_gates = tuple(
            getattr(sess, "_recovered_interactions", ()) or ())
        if stale_gates:
            _session_notice(
                sess,
                f"[检测到 {len(stale_gates)} 个未完成 decision gate；"
                "上次运行已停止，未自动替你选择。可重新提交原任务]",
                error=True,
            )
        commit = getattr(sess, "commit_target_lease", None)
        if callable(commit):
            commit(target_id)
        target_acquired = False
    except BaseException:
        if changed_cwd:
            try:
                os.chdir(current_cwd)
            except OSError:
                pass
        rollback = getattr(sess, "rollback_target_lease", None)
        if target_acquired and callable(rollback):
            try:
                rollback(target_id)
            except Exception as lease_rollback_error:  # noqa: BLE001
                store.log(
                    "resume_lease_rollback_failed",
                    session=target_id,
                    error=(f"{type(lease_rollback_error).__name__}: "
                           f"{lease_rollback_error}"),
                )
        if runtime_touched:
            try:
                restore_session_record(
                    sess, previous, refresh_environment=True)
                sess.reset_controller()
            except Exception as rollback_error:  # noqa: BLE001
                store.log(
                    "resume_runtime_rollback_failed",
                    session=target_id,
                    error=f"{type(rollback_error).__name__}: {rollback_error}",
                )
        if (configuration_applied and previous_configuration is not None
                and hasattr(sess, "restore_configuration_snapshot")):
            try:
                sess.restore_configuration_snapshot(previous_configuration)
            except Exception as config_rollback_error:  # noqa: BLE001
                store.log(
                    "resume_config_rollback_failed",
                    session=target_id,
                    error=(f"{type(config_rollback_error).__name__}: "
                           f"{config_rollback_error}"),
                )
        raise

    # Runtime cleanup happens after the lease/controller commit.  A cleanup
    # failure must not turn a successfully switched target back into an old
    # Agent paired with the target lease.
    try:
        detach_task = getattr(sess, "detach_task", None)
        if callable(detach_task):
            detach_task()
        reset_agents = getattr(sess, "reset_agent_workspace", None)
        if callable(reset_agents):
            if isinstance(sess, Session):
                reset_agents(
                    previous_session_id=previous["id"],
                    previous_controller=previous_controller,
                )
            else:
                reset_agents()
    except Exception as cleanup_error:  # noqa: BLE001
        store.log(
            "resume_runtime_cleanup_failed",
            session=target_id,
            error=f"{type(cleanup_error).__name__}: {cleanup_error}",
        )
        _session_notice(
            sess,
            f"[已切换会话，但旧 runtime 清理失败：{cleanup_error}]",
            error=True,
        )

    # Explicit "current" is a durable rebind.  Failure here leaves the target
    # runtime/lease coherent and will be retried by the next normal save.
    if policy == "current" and hasattr(sess, "lease_owner_id"):
        try:
            store.update_session_metadata(
                target_id,
                owner_id=sess.lease_owner_id,
                cwd=current_cwd,
            )
        except (OSError, ValueError, store.SessionStoreError) as metadata_error:
            store.log(
                "resume_cwd_rebind_failed",
                session=target_id,
                error=f"{type(metadata_error).__name__}: {metadata_error}",
            )
            _session_notice(
                sess,
                f"[会话已切换；cwd 元数据将在下次保存时重试："
                f"{metadata_error}]",
                error=True,
            )

    if replay:
        shown = show_resumed_transcript(sess, sess.ag.messages)
        if not shown:
            _session_notice(
                sess,
                f"[接上 {record['id']} · {record.get('title', '')}]",
            )
        show_recap(sess, record)
    else:
        sync_transcript = getattr(sess, "sync_transcript", None)
        if callable(sync_transcript):
            sync_transcript()
        _session_notice(
            sess,
            f"[接上 {record['id']} · {record.get('title', '')}]",
        )
        show_recap(sess, record)
    return True


def _parse_resume_args(rest):
    target = ""
    policy = None
    parts = str(rest or "").split()
    index = 0
    while index < len(parts):
        part = parts[index]
        if part.startswith("--cwd="):
            policy = part.split("=", 1)[1]
        elif part == "--cwd" and index + 1 < len(parts):
            index += 1
            policy = parts[index]
        elif part in {"--current", "--saved", "--fork"}:
            policy = part[2:]
        elif not target:
            target = part
        else:
            raise ValueError(f"无法解析 /resume 参数：{part}")
        index += 1
    return target, policy
# 从前这三处各维护一份，加命令要改三个地方，已经开始不同步。

def cmd_new(sess, rest):
    previous_agent = sess.ag
    previous_controller = getattr(sess, "controller", None)
    previous_last_ctx = getattr(sess, "last_ctx", None)
    previous_title = getattr(sess, "_title", None)
    previous_cwd = getattr(
        sess, "_session_cwd", os.path.abspath(os.getcwd()))
    previous_always = set(getattr(sess, "always", set()))
    previous_task_plan = PLANS.normalize_record(
        getattr(sess, "task_plan", None))
    previous_workflow_auto = bool(
        getattr(sess, "workflow_auto", False))
    previous_workflow_override = getattr(
        sess, "_workflow_auto_override", None)
    previous_goal = GOALS.copy(getattr(sess, "goal", None))
    previous_goal_activation = getattr(sess, "_goal_activation", "disarmed")
    new_agent = A.Agent(
        model=previous_agent.model,
        load_md=bool(getattr(previous_agent, "load_md", True)),
        load_user_md=getattr(previous_agent, "load_user_md", None),
        load_project_md=getattr(previous_agent, "load_project_md", None),
        load_skills=getattr(previous_agent, "load_skills", None),
        compact_at=sess.cfg.get("compact_at"),
        gateway=previous_agent.gateway,
    )
    new_agent.hook_cfg = sess.cfg
    new_agent.confirm = sess.confirm
    new_agent.permission_decision = sess.permission_decision
    checkpoint_callback = getattr(sess, "checkpoint_prepared", None)
    if callable(checkpoint_callback):
        new_agent.checkpoint_prepared = checkpoint_callback
    state_callback = getattr(sess, "_compaction_state", None)
    if callable(state_callback):
        new_agent.compaction_state = state_callback
    acquire = getattr(sess, "acquire_target_lease", None)
    acquired = (
        bool(acquire(new_agent.session_id)) if callable(acquire) else False)
    try:
        sess.save()
        # These assignments are deliberately the only pre-commit mutation:
        # each is restored below if controller construction or lease transfer
        # fails.  Old tasks and child agents remain live until commit succeeds.
        sess.ag = new_agent
        sess.last_ctx = None
        sess._title = None
        sess._session_cwd = os.path.abspath(os.getcwd())
        sess.always = set()
        sess.task_plan = PLANS.empty()
        sess._workflow_auto_override = None
        sess.workflow_auto = bool(
            CFG.workflow_policy(sess.cfg).get("auto"))
        sess.goal = GOALS.empty()
        sess._goal_activation = "disarmed"
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        refresh_skills = getattr(sess, "refresh_skills_context", None)
        if callable(refresh_skills):
            refresh_skills()
        tools.HOOK_CTX["session"] = str(new_agent.session_id)
        sess.reset_controller()
        commit = getattr(sess, "commit_target_lease", None)
        if callable(commit):
            commit(new_agent.session_id)
        acquired = False
    except BaseException:
        sess.ag = previous_agent
        sess.controller = previous_controller
        sess.last_ctx = previous_last_ctx
        sess._title = previous_title
        sess._session_cwd = previous_cwd
        sess.always = previous_always
        sess.task_plan = previous_task_plan
        sess._workflow_auto_override = previous_workflow_override
        sess.workflow_auto = previous_workflow_auto
        sess.goal = previous_goal
        sess._goal_activation = previous_goal_activation
        sess._goal_round_items = {}
        sess._goal_round_started = {}
        tools.HOOK_CTX["session"] = str(previous_agent.session_id)
        rollback = getattr(sess, "rollback_target_lease", None)
        if acquired and callable(rollback):
            try:
                rollback(new_agent.session_id)
            except Exception as rollback_error:  # noqa: BLE001
                store.log(
                    "new_session_lease_rollback_failed",
                    session=new_agent.session_id,
                    error=(f"{type(rollback_error).__name__}: "
                           f"{rollback_error}"),
                )
        raise

    try:
        detach_task = getattr(sess, "detach_task", None)
        if callable(detach_task):
            detach_task()
        reset_agents = getattr(sess, "reset_agent_workspace", None)
        if callable(reset_agents):
            if isinstance(sess, Session):
                reset_agents(
                    previous_session_id=previous_agent.session_id,
                    previous_controller=previous_controller,
                )
            else:
                reset_agents()
    except Exception as cleanup_error:  # noqa: BLE001
        store.log(
            "new_session_runtime_cleanup_failed",
            session=new_agent.session_id,
            error=f"{type(cleanup_error).__name__}: {cleanup_error}",
        )
        _session_notice(
            sess,
            f"[新会话已建立，但旧 runtime 清理失败：{cleanup_error}]",
            error=True,
        )
    sync_transcript = getattr(sess, "sync_transcript", None)
    if callable(sync_transcript):
        sync_transcript()
    print(DIM(f"  [新会话 {sess.ag.session_id}]"))


def cmd_sessions(sess, rest):
    parts = str(rest or "").split()
    if parts and parts[0] == "repair-lease":
        if len(parts) != 2:
            _session_notice(
                sess, "用法：/sessions repair-lease <完整 session id>",
                error=True)
            return
        session_id = parts[1]
        try:
            lease = store.session_lease(session_id)
            if lease is None:
                raise FileNotFoundError(f"会话 {session_id} 没有 lease")
            if not lease.get("_corrupt"):
                raise store.SessionStoreError(
                    "lease 结构有效，拒绝使用损坏记录修复通路")
            confirmed = sess.prompt_text(
                prompt="输入完整 session id 确认隔离损坏 lease › ",
                title=f"Repair corrupt lease · {session_id}",
                hint="不会删除；原文件移入 session-leases/quarantine · Esc 取消",
            )
            if confirmed != session_id:
                _session_notice(sess, "[已取消 lease 修复]")
                return
            destination = store.quarantine_corrupt_session_lease(session_id)
            _session_notice(
                sess, f"[损坏 lease 已隔离到 {destination}]")
        except (OSError, ValueError, store.SessionStoreError) as exc:
            _session_notice(sess, str(exc), error=True)
        return
    print_sessions(
        store.list_sessions(limit=int(rest) if rest.isdigit() else 20),
        current=sess.ag.session_id)


def cmd_resume(sess, rest):
    rows = None
    try:
        target, policy = _parse_resume_args(rest)
        if not target:
            if sess.pump is not None:
                record = open_sessions_panel(sess)
                if record is None:
                    return
                if record.get("_already_resumed"):
                    return
                resume_session_record(
                    sess, record, cwd_policy=policy)
                return
            rows = store.list_sessions(limit=50)
            print_sessions(rows, current=sess.ag.session_id)
            try:
                target = input(
                    DIM("  选择 [序号|id，回车取消] > ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        if not target:
            return
        if target.isdigit():
            rows = rows or store.list_sessions(limit=50)
            number = int(target)
            if not 1 <= number <= len(rows):
                raise ValueError(f"会话序号超出范围：{target}")
            target = rows[number - 1]["id"]
        resume_session_record(
            sess, {"id": target}, cwd_policy=policy)
    except (OSError, RuntimeError, ValueError, KeyError,
            store.SessionStoreError) as exc:
        _session_notice(sess, str(exc), error=True)


def _checkpoint_row(rows, hint):
    if not hint:
        return None
    exact = next(
        (row for row in rows if row["checkpoint_id"] == hint), None)
    if exact is not None:
        return exact
    hits = [
        row for row in rows if row["checkpoint_id"].startswith(hint)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError(
            f"checkpoint 前缀 {hint!r} 匹配 {len(hits)} 项")
    raise FileNotFoundError(f"没有 checkpoint {hint}")


def _rewind_audit(sess, kind, payload, *, session_id=None):
    target_session_id = str(session_id or sess.ag.session_id)
    current_session_id = str(sess.ag.session_id)
    entry = store.append_checkpoint_audit(
        target_session_id, kind, payload,
        owner_id=(
            sess.lease_owner_id
            if target_session_id == current_session_id else None))
    if (sess.controller is not None
            and target_session_id == current_session_id):
        sess.controller.record_event(kind, payload)
    return entry


def _rewind_committed_audit(sess, kind, payload, *, completed_parts,
                            session_id=None):
    """Record an already-committed rewind outcome without misreporting it.

    Once a branch, code restore, or saved projection exists, an audit write
    failure must not fall through to the generic ``rewind failed`` handler: a
    retry could repeat a side effect that actually succeeded.  A second,
    best-effort event preserves the audit failure when the store can still
    accept a different record; the terminal warning remains authoritative
    when the whole audit store is unavailable.
    """
    parts = tuple(str(item) for item in completed_parts)
    try:
        _rewind_audit(
            sess, kind, payload, session_id=session_id)
        return True
    except Exception as exc:
        fallback = {
            "schema_version": 1,
            "committed_event": kind,
            "completed_parts": list(parts),
            "error_kind": type(exc).__name__,
            "error": str(exc),
        }
        try:
            _rewind_audit(
                sess, "rewind_audit_failed", fallback,
                session_id=session_id)
        except Exception:
            pass
        description = "、".join(parts) or "rewind side effect"
        _session_notice(
            sess,
            f"[rewind] {description}已完成，但 {kind} 完成审计失败：{exc}；"
            "请先检查当前状态，不要直接重试",
            error=True)
        return False


def _rewind_partial_restore_report(
        sess, exc, audit_payload, *, child_session_id=None,
        session_id=None):
    """Keep a durable-compensation failure primary even if audit also fails."""
    details = dict(getattr(exc, "details", {}) or {})
    payload = {
        **audit_payload,
        "child_session_id": child_session_id,
        **details,
        "error_kind": type(exc).__name__,
        "error": str(exc),
    }
    audit_error = None
    try:
        _rewind_audit(
            sess, "checkpoint_restore_partial", payload,
            session_id=session_id)
    except Exception as caught:
        audit_error = caught
        fallback = {
            "schema_version": 1,
            "failed_event": "checkpoint_restore_partial",
            "transaction_id": payload.get("transaction_id"),
            "paths": list(audit_payload.get("paths", ())),
            "error_kind": type(caught).__name__,
            "error": str(caught),
        }
        try:
            _rewind_audit(
                sess, "rewind_audit_failed", fallback,
                session_id=session_id)
        except Exception:
            pass

    failure_paths = sorted({
        str(row["path"])
        for row in payload.get("rollback_failures", ())
        if isinstance(row, dict) and row.get("path")
    })
    if not failure_paths:
        failure_paths = [
            str(path) for path in audit_payload.get("paths", ())]
    transaction_id = str(payload.get("transaction_id") or "unknown")
    paths_text = ", ".join(failure_paths) or "unknown"
    _session_notice(
        sess,
        f"[rewind] durable compensation 未完成：{exc}；"
        f"transaction={transaction_id}；paths={paths_text}；"
        "当前文件可能处于混合状态，勿重试 /rewind；"
        "请先检查 restore transaction 并执行显式 recovery",
        error=True)
    if audit_error is not None:
        _session_notice(
            sess,
            f"[rewind] checkpoint_restore_partial 审计失败：{audit_error}；"
            "该审计错误未替换上面的 durable partial 主故障",
            error=True)
    return payload


def _rewind_cutoff_status(messages, cutoff):
    """Validate a checkpoint chat cutoff before any rewind side effect."""
    if type(cutoff) is not int:
        return False, "checkpoint 没有有效 conversation cutoff"
    messages = list(messages or [])
    if cutoff < 1 or cutoff > len(messages):
        return False, (
            f"conversation cutoff {cutoff} 已超出当前 raw history "
            f"1..{len(messages)}；可能执行过 /clear")
    system_end = 0
    while (system_end < len(messages)
           and messages[system_end].get("role") == "system"):
        system_end += 1
    units, issues = CONTEXT.build_units(messages, system_end)
    boundaries = {system_end}
    boundaries.update(unit.end for unit in CONTEXT.group_turns(units))
    if cutoff not in boundaries:
        return False, "conversation cutoff 会切断完整用户轮次"
    if any(issue["start"] < cutoff for issue in issues):
        return False, "conversation cutoff 跨过不完整 tool call/result"
    return True, None


def cmd_rewind(sess, rest):
    """Restore checkpointed code and/or branch conversation history."""
    action_aliases = {
        "conversation": "conversation",
        "chat": "conversation",
        "code": "code",
        "both": "both",
        "branch": "branch",
        "summarize": "summarize",
        "summary": "summarize",
        "cancel": "cancel",
    }
    parts = str(rest or "").split()
    checkpoint_hint = None
    requested_action = None
    if parts and parts[0].lower() in action_aliases:
        requested_action = action_aliases[parts.pop(0).lower()]
    elif parts:
        checkpoint_hint = parts.pop(0)
        if parts:
            requested_action = action_aliases.get(parts.pop(0).lower())
            if requested_action is None:
                print(DIM(
                    "  用法：/rewind [checkpoint] "
                    "[conversation|code|both|branch|summarize|cancel]"))
                return
    if parts:
        print(DIM(
            "  用法：/rewind [checkpoint] "
            "[conversation|code|both|branch|summarize|cancel]"))
        return

    try:
        authority = sess.checkpoint_authority()
        rows = authority.list_checkpoints(sess.ag.session_id)
        if not rows:
            print(DIM(
                "  [当前会话还没有 write_file/edit_file checkpoint；"
                "Bash 与外部改动不在覆盖范围]"))
            return
        selected = _checkpoint_row(rows, checkpoint_hint)
        if selected is None:
            selected = sess.pick(
                rows, page=12, allow_filter=True,
                title="选择 checkpoint · 仅覆盖 write_file/edit_file",
                render=lambda row: (
                    f"{row['checkpoint_id']:<24} "
                    f"{row['files']:>2} files · "
                    f"{row['created_at']}"))
        if selected is None:
            return
        checkpoint_id = selected["checkpoint_id"]
        cutoff = selected.get("conversation_message_count")
        cutoff_valid, cutoff_error = _rewind_cutoff_status(
            getattr(sess.ag, "messages", ()), cutoff)
        summarize_available = (
            cutoff_valid
            and any(
                message.get("role") != "system"
                for message in getattr(sess.ag, "messages", ())[:cutoff]
                if isinstance(message, dict)))
        code_plan = None
        code_error = None
        try:
            code_plan = authority.plan_restore(
                sess.ag.session_id, checkpoint_id,
                workspace_root=os.getcwd())
        except CHECKPOINTS.CheckpointError as exc:
            code_error = str(exc)

        print()
        print(BOLD(f"  checkpoint {checkpoint_id}"))
        if code_plan is not None:
            for item in code_plan.actions:
                print(DIM(f"    {item.action:<8} {item.path}"))
        else:
            for path in selected.get("paths", ()):
                print(DIM(f"    blocked  {path}"))
            print(YELLOW(f"    code restore blocked: {code_error}"))
        print(DIM(
            "    注意：checkpoint 不覆盖 Bash、外部进程、owner/ACL/xattr"))

        options = []
        if cutoff_valid:
            options.append((
                "conversation", "Restore conversation only · 创建 branch"))
        if code_plan is not None:
            options.append(("code", "Restore code only"))
            if cutoff_valid:
                options.append((
                    "both", "Restore both · code + conversation branch"))
        if cutoff_valid:
            options.append((
                "branch", "Branch from checkpoint · 不恢复文件"))
        if summarize_available:
            options.append((
                "summarize", "Summarize up to here · raw 保持不变"))
        options.append(("cancel", "Cancel"))
        choice = requested_action
        available = {item[0] for item in options}
        if choice is not None and choice not in available:
            if choice == "code":
                reason = code_error
            elif choice == "both":
                reason = (
                    code_error if code_plan is None else cutoff_error)
            elif choice == "summarize":
                reason = "checkpoint 前没有可摘要的完整会话"
            else:
                reason = (
                    cutoff_error or "checkpoint 没有 conversation cutoff")
            raise CHECKPOINTS.RestoreConflict(
                f"{choice} 当前不可用：{reason}")
        if choice is None:
            picked = sess.pick(
                options, page=len(options), allow_filter=False,
                title=f"Rewind {checkpoint_id}",
                render=lambda item: item[1])
            choice = picked[0] if picked else "cancel"
        if choice == "cancel":
            print(DIM("  [已取消 rewind]"))
            return

        source_session_id = sess.ag.session_id
        audit_payload = {
            "schema_version": 1,
            "checkpoint_id": checkpoint_id,
            "action": choice,
            "paths": list(selected.get("paths", ())),
            "conversation_message_count": cutoff,
        }
        _rewind_audit(sess, "rewind_requested", audit_payload)

        child = None
        if choice in {"conversation", "both", "branch"}:
            try:
                child = fork_session_record(
                    sess, {"id": source_session_id},
                    message_count=cutoff, replay=True, save_current=True)
            except Exception as exc:
                _rewind_audit(sess, "rewind_branch_failed", {
                    **audit_payload,
                    "error_kind": type(exc).__name__,
                    "error": str(exc),
                }, session_id=source_session_id)
                raise
            _rewind_committed_audit(sess, "rewind_branch_created", {
                **audit_payload,
                "child_session_id": child["id"],
            }, completed_parts=("conversation branch",),
                session_id=source_session_id)
            print(DIM(
                f"  [conversation → branch {child['id']} · "
                f"原会话 {source_session_id} 保持不变]"))

        restore_result = None
        if choice in {"code", "both"}:
            try:
                restore_result = authority.restore(
                    source_session_id, checkpoint_id,
                    workspace_root=os.getcwd())
            except CHECKPOINTS.PartialRestoreError as exc:
                _rewind_partial_restore_report(
                    sess, exc, audit_payload,
                    child_session_id=(
                        child["id"] if child is not None else None),
                    session_id=source_session_id)
                return
            except Exception as exc:
                failed_payload = {
                    **audit_payload,
                    "child_session_id": (
                        child["id"] if child is not None else None),
                    "error_kind": type(exc).__name__,
                    "error": str(exc),
                }
                _rewind_audit(
                    sess, "checkpoint_restore_failed", failed_payload,
                    session_id=source_session_id)
                if child is not None:
                    _rewind_audit(sess, "rewind_partial", {
                        **failed_payload,
                        "completed_parts": ["conversation_branch"],
                        "failed_part": "code_restore",
                    }, session_id=source_session_id)
                raise
            completed = {
                **audit_payload,
                "child_session_id": (
                    child["id"] if child is not None else None),
                "restored": list(restore_result.restored),
                "already_restored": list(
                    restore_result.already_restored),
                "trashed": [
                    {"source": source, "trash": trash}
                    for source, trash in restore_result.trashed
                ],
            }
            _rewind_committed_audit(
                sess, "checkpoint_restored", completed,
                completed_parts=("代码恢复",),
                session_id=source_session_id)
            print(DIM(
                f"  [代码恢复：{len(restore_result.restored)} restored · "
                f"{len(restore_result.already_restored)} unchanged · "
                f"{len(restore_result.trashed)} moved to trash]"))

        if choice in {"conversation", "code", "both", "branch"}:
            completed_parts = []
            if child is not None:
                completed_parts.append("conversation branch")
            if restore_result is not None:
                completed_parts.append("代码恢复")
            _rewind_committed_audit(sess, "rewind_completed", {
                **audit_payload,
                "child_session_id": (
                    child["id"] if child is not None else None),
            }, completed_parts=completed_parts,
                session_id=source_session_id)
        elif choice == "summarize":
            previous_projection = {
                name: getattr(sess.ag, name, None)
                for name in (
                    "context_summary", "context_invalid_reason",
                    "_compact_failed_key", "compact_failed", "last_total")
            }
            with Spinner("摘要中"):
                summary = sess.ag.summarize_to(cutoff)
            failed = str(summary or "").startswith("[摘要生成失败")
            if failed:
                _rewind_audit(sess, "checkpoint_summarize_failed", {
                    **audit_payload, "error": str(summary),
                })
                print(YELLOW(f"  {summary}"))
            elif summary:
                try:
                    sess.save()
                except Exception:
                    for name, value in previous_projection.items():
                        setattr(sess.ag, name, value)
                    raise
                _rewind_committed_audit(sess, "checkpoint_summarized", {
                    **audit_payload,
                    "summary_chars": len(summary),
                    "covered_to": cutoff,
                }, completed_parts=("conversation 摘要保存",))
                print(DIM(
                    f"  [已摘要到 checkpoint {checkpoint_id} · "
                    f"{len(summary)} 字符；raw transcript 未改写]"))
            else:
                _rewind_audit(
                    sess, "checkpoint_summarize_noop", audit_payload)
                print(DIM(
                    "  [该 checkpoint 前缀已被等价摘要，无需重复生成]"))
    except (OSError, RuntimeError, ValueError,
            store.SessionStoreError,
            CHECKPOINTS.CheckpointError) as exc:
        _session_notice(sess, f"[rewind] {exc}", error=True)


def cmd_rename(sess, rest):
    if rest:
        sess.save(title=rest)
        print(DIM(f"  [已命名：{rest}]"))
    else:
        print(DIM("  用法：/rename <标题>"))


def _goal_usage():
    print(DIM(
        "  armed 的目标 = 会话空闲时自动开始下一轮，不需要你每次输入，"
        "直到完成、被阻塞或用满轮数。"))
    print(DIM(
        "  只有你能 armed；模型可以用 goal_propose 起草，等你 /goal accept。"))
    print(DIM(
        "  只在这个终端进程里有效：退出 zylab 或 pod 重启，自动续轮就停。"))
    print("  /goal [status]")
    print("  /goal <objective>                 创建并 armed 一个跨 turn 目标")
    print("  /goal set <objective> [--rounds N]")
    print("  /goal edit <objective> [--rounds N]")
    print("  /goal accept | reject             采纳/忽略模型起草的目标")
    print("  /goal pause | resume | clear")
    print("  /goal complete [evidence]")
    print("  /goal block <reason>")


def _render_goal_proposal(proposal):
    """把模型的目标草稿摆在用户面前，并给出唯一的采纳入口。"""
    proposal = dict(proposal or {})
    objective = _goal_ui_text(proposal.get("objective"))
    rounds = proposal.get("max_rounds") or GOALS.DEFAULT_MAX_ROUNDS
    lines = [YELLOW(
        f"  [模型建议一个自动续轮目标 · 最多 {rounds} 轮 · 尚未生效]")]
    for row in tui.wrap_display(objective, 74):
        lines.append(f"    {row}")
    rationale = _goal_ui_text(proposal.get("rationale"))
    if rationale:
        for row in tui.wrap_display(rationale, 74):
            lines.append(DIM(f"    {row}"))
    lines.append(DIM(
        "    /goal accept 采纳并 armed（空闲时自动续轮）· /goal reject 忽略"))
    return "\r\n".join(lines) + "\r\n"


def _goal_ui_text(value):
    """Flatten and sanitize persisted/model goal text before terminal output."""
    return " ".join(tui.sanitize_terminal_text(
        str(value or "")).splitlines()).strip()


def _print_goal(sess):
    record = getattr(sess, "goal", None)
    if record is None:
        print(DIM("  (没有 goal；/goal <objective> 创建)"))
        return
    view = GOALS.view(
        record, activation=getattr(sess, "_goal_activation", "disarmed"))
    print(f"  {BOLD('Goal')}  {view['phase']} · {view['activation']}")
    print(f"    objective      {_goal_ui_text(view['objective'])}")
    print(f"    progress       {view['rounds_started']}/{view['max_rounds']} rounds")
    print(f"    usage          in {view['tokens_in']:,} / out {view['tokens_out']:,} · "
          f"{view['elapsed_seconds']:.1f}s")
    print(f"    id/revision    {view['id']} / {view['revision']}")
    if view.get("last_evidence"):
        print(f"    evidence       {_goal_ui_text(view['last_evidence'])}")
    if view.get("last_error"):
        print(f"    last error     {_goal_ui_text(view['last_error'])}")
    if view.get("blocked_reason"):
        reason = view["blocked_reason"]
        print(f"    blocked        {_goal_ui_text(reason['code'])}: "
              f"{_goal_ui_text(reason['message'])}")
    if view["phase"] == "active" and view["activation"] == "disarmed":
        print(DIM("    自动续轮已停用；/goal resume 显式继续（不会隐式发请求）"))


def _parse_goal_payload(value):
    """Parse a human command while preserving objective whitespace safely."""
    try:
        parts = shlex.split(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"goal 参数引号无效：{exc}") from None
    rounds = None
    words = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part in {"--rounds", "--max-rounds"}:
            if index + 1 >= len(parts):
                raise ValueError(f"{part} 需要一个整数")
            rounds = parts[index + 1]
            index += 2
            continue
        if part.startswith("--rounds=") or part.startswith("--max-rounds="):
            rounds = part.split("=", 1)[1]
            index += 1
            continue
        words.append(part)
        index += 1
    return " ".join(words).strip(), rounds


def cmd_goal(sess, rest):
    """Show or mutate the user-armed cross-turn completion goal."""
    raw = str(rest or "").strip()
    if not raw or raw.casefold() in {"status", "show"}:
        _print_goal(sess)
        return
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        print(RED(f"  [goal] 参数引号无效：{exc}"))
        return
    action = parts[0].casefold()
    tail = raw[len(parts[0]):].strip() if parts else ""
    try:
        if action in {"help", "?"}:
            _goal_usage()
            return
        if action in {"set", "new", "create"}:
            objective, rounds = _parse_goal_payload(tail)
            if not objective:
                raise ValueError("用法：/goal set <objective> [--rounds N]")
            view = sess.create_goal(
                objective,
                max_rounds=(GOALS.DEFAULT_MAX_ROUNDS
                            if rounds is None else rounds),
            )
            print(GREEN(
                f"  [goal 已创建并 armed · {view['rounds_started']}/"
                f"{view['max_rounds']} rounds]"))
            print(DIM("  空闲边界会自动开始第一轮；/goal pause 可随时停用"))
            return
        if action == "edit":
            objective, rounds = _parse_goal_payload(tail)
            if not objective and rounds is None:
                raise ValueError("用法：/goal edit <objective> [--rounds N]")
            view = sess.edit_goal(
                objective or None,
                max_rounds=rounds,
            )
            print(DIM(
                f"  [goal 已更新 · {view['phase']} · "
                f"{view['rounds_started']}/{view['max_rounds']}]"))
            return
        if action == "accept":
            proposal = sess.take_goal_proposal()
            if proposal is None:
                print(DIM("  (没有待采纳的 goal 提案)"))
                return
            view = sess.create_goal(
                proposal["objective"],
                max_rounds=int(proposal.get("max_rounds")
                               or GOALS.DEFAULT_MAX_ROUNDS))
            print(GREEN(
                f"  [已采纳模型提案并 armed · {view['rounds_started']}/"
                f"{view['max_rounds']} rounds]"))
            print(DIM("  空闲边界会自动开始第一轮；/goal pause 可随时停用"))
            return
        if action == "reject":
            if sess.take_goal_proposal() is None:
                print(DIM("  (没有待采纳的 goal 提案)"))
            else:
                print(DIM("  [已忽略模型的 goal 提案]"))
            return
        if action == "pause":
            view = sess.pause_goal()
            print(DIM("  [goal 已暂停；/goal resume 继续]"))
            return
        if action == "resume":
            view = sess.resume_goal()
            print(GREEN(
                f"  [goal 已恢复并 armed · {view['rounds_started']}/"
                f"{view['max_rounds']} rounds]"))
            return
        if action in {"clear", "delete", "cancel"}:
            sess.clear_goal()
            print(DIM("  [goal 已清除]"))
            return
        if action in {"complete", "done"}:
            evidence = tail
            view = sess.complete_goal(evidence=evidence)
            print(GREEN("  [goal 已标记 complete]"))
            if evidence:
                print(DIM(f"    evidence: {_goal_ui_text(evidence)}"))
            return
        if action in {"block", "blocked"}:
            reason = tail or "用户报告了阻塞"
            view = sess.block_goal(reason)
            print(YELLOW(
                f"  [goal 已标记 blocked：{_goal_ui_text(reason)}]"))
            return
        objective, rounds = _parse_goal_payload(raw)
        if not objective:
            raise ValueError("目标不能为空")
        view = sess.create_goal(
            objective,
            max_rounds=(GOALS.DEFAULT_MAX_ROUNDS
                        if rounds is None else rounds),
        )
        print(GREEN(
            f"  [goal 已创建并 armed · {view['rounds_started']}/"
            f"{view['max_rounds']} rounds]"))
    except (GOALS.GoalError, ValueError, OSError, RuntimeError) as exc:
        print(RED(f"  [goal] {exc}"))


def cmd_plan(sess, rest):
    sess.plan_mode = not sess.plan_mode
    if sess.plan_mode:
        print(YELLOW("  [计划模式] 只读调研，不会改任何东西。"
                     "\n  说清你要做什么，它会给出计划；"
                     "再打 /plan 批准并退出该模式。"))
    else:
        txt = getattr(sess, "_last_text", "").strip()
        if txt:
            pf = store.PLANS / f"{sess.ag.session_id}-{int(time.time())}.md"
            try:
                store.PLANS.mkdir(parents=True, exist_ok=True)
                pf.write_text(txt, encoding="utf-8")
                print(GREEN(f"  [已批准计划，存档 {pf.name}]"))
            except OSError:
                print(GREEN("  [已退出计划模式]"))
            # 把计划作为约束带回执行阶段
            sess.ag.messages.append({
                "role": "user",
                "content": "用户已批准上面的计划。现在按计划执行，"
                           "遇到与计划不符的情况先说明再动手。"})
        else:
            print(DIM("  [已退出计划模式]"))


def cmd_hooks(sess, rest):
    hk = sess.cfg.get("hooks") or {}
    if not hk:
        print(DIM("  (未配置 hook)"))
        print(DIM(f"  在 {CFG.USER_FILE} 里加，例如："))
        print(DIM('  "hooks": {\n    "PreToolUse":  [{"matcher": "bash", "command": "my-check.sh"}],\n    "PostToolUse": [{"matcher": "edit_file",\n                     "command": "ruff check --fix \\"$ZYLAB_TOOL_PATH\\""}]\n  }'))
        print(DIM("  退出码 0=放行 · 2=拦截（stderr 作理由）"
                  " · 其他=hook 自身故障，放行"))
    else:
        for ev, lst in hk.items():
            print(f"  {BOLD(ev)}")
            for h in lst:
                print(f"    {h.get('matcher','*'):18} "
                      f"{DIM(h.get('command','')[:60])}")
    print(DIM("  hook 只能在用户级配置注册，项目级的会被忽略"))


def cmd_config(sess, rest):
    cfg, src = CFG.load()
    print(CFG.render(cfg, src))
    print(DIM(f"\n  用户级配置: {CFG.USER_FILE}"
              f"\n  项目级配置: {CFG.project_file()}"
              f"\n  全局指令:   {store.USER_MD}"))


def _doctor_session_payload(agent_obj):
    """活动会话的 payload 体检 —— 真正决定请求成败的东西。

    环境全绿而会话已废是可能的：只要历史里有一条 arguments 不是合法 JSON，
    服务端每轮都会 400，重试与换模型都无效。这一行就是为了让那种情况一眼可见。
    """
    import json as _json
    messages = getattr(agent_obj, "messages", None)
    if not isinstance(messages, list):
        return "-"
    bad, announced, answered = [], set(), set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or ():
            fn = (call or {}).get("function") or {}
            cid = str((call or {}).get("id") or "")
            if cid:
                announced.add(cid)
            try:
                _json.loads(str(fn.get("arguments") or ""))
            except (ValueError, TypeError):
                bad.append((index, str(fn.get("name") or "?")))
        if message.get("role") == "tool":
            answered.add(str(message.get("tool_call_id") or ""))
    parts = [f"{len(messages)} 条消息"]
    if bad:
        index, name = bad[0]
        parts.append(
            f"ERROR arguments 非法 JSON {len(bad)} 处"
            f"（首个：第 {index} 条 · {name}）——服务端每轮必 400")
    pending = len(announced - answered)
    if pending:
        # 末尾那一轮的调用可能正当地还没有结果，所以只报数、不判定为错误。
        parts.append(f"未配对 tool_call {pending}（末轮待执行属正常）")
    if not bad:
        parts.append("arguments OK")
    return " · ".join(parts)


def _doctor_recent_failures(limit=200):
    """最近的 provider 失败 —— 全部读 metrics 已记录的字段，无新增埋点。

    `requests` 表本来就存了 status/http_status/error_kind/error_text，
    但 doctor 过去只打印一句 `requests=N`，于是「同一个 400 复发 10 次」
    这种最有用的线索一直躺在它自己打开的库里没被读出来。
    """
    try:
        rows = store.metrics_facade().list_recent_traces(limit=limit)
    except Exception as exc:  # noqa: BLE001 - 诊断本身不能把会话搞崩
        return f"ERROR {type(exc).__name__}: {exc}"
    if not rows:
        return "无记录"
    failed = [r for r in rows if str(r.get("status") or "") not in ("ok", "running")]
    if not failed:
        return f"最近 {len(rows)} 次请求无失败"
    tally = {}
    for row in failed:
        key = str(row.get("http_status") or row.get("error_kind") or "?")
        tally[key] = tally.get(key, 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    newest = " ".join(str(failed[0].get("error_text") or "").split())[:88]
    return (f"最近 {len(rows)} 次中 {len(failed)} 次失败 · "
            + " ".join(f"{k}×{v}" for k, v in ranked)
            + (f" · 最新：{newest}" if newest else ""))


def _doctor_circuit(gateway, model):
    """当前路由的熔断状态 —— 环境全绿而请求被**本地**挡住时，这一行说明原因。

    09-20 实测过一次：网关直连三发全 200，zylab 却拒发，因为熔断还剩 8 分钟；
    而 doctor 当时 14 行全绿，没有任何一行提到熔断。
    """
    import datetime as _dt
    try:
        record = store.metrics_facade().get_health(str(gateway), str(model))
    except Exception as exc:  # noqa: BLE001 - 诊断本身不能把会话搞崩
        return f"ERROR {type(exc).__name__}: {exc}"
    if not record:
        return "无记录（该路由还没跑过请求）"
    fails = record.get("consecutive_failures") or 0
    kind = record.get("last_error_kind") or "-"
    tail = (f"连失 {fails} · 最近错误 {kind} · 累计 "
            f"{record.get('success_count') or 0} 成功/"
            f"{record.get('failure_count') or 0} 失败")
    until = record.get("circuit_open_until")
    if not until:
        return f"closed · {tail}"
    try:
        end = _dt.datetime.fromisoformat(str(until))
        left = int((end - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return f"ERROR circuit_open_until 无法解析：{until}"
    if left <= 0:
        return f"closed（熔断已过期）· {tail}"
    return (f"ERROR OPEN 还有 {left}s · {tail} · "
            f"/model 重选（同一个也行）即可立即越过")


def cmd_doctor(sess, rest):
    """Read-only runtime diagnosis; never prints credentials or probes archives."""
    action = str(rest or "").strip().lower()
    if action not in {"", "refresh"}:
        print(DIM("  用法：/doctor [refresh]"))
        return
    try:
        sandbox_state = tools.sandbox_status(
            sess.cfg, refresh=(action == "refresh"))
        sess._sandbox_status = sandbox_state
        sandbox_decision = sandbox_state["decision"]
        sandbox_capability = sandbox_state["capabilities"]
        runtime_capability = sandbox_state.get(
            "runtime_capabilities", sandbox_capability)
        sandbox_error = ""
    except Exception as exc:
        sandbox_state = {
            "marker": "SANDBOX BLOCKED",
            "error": f"{type(exc).__name__}: {exc}",
        }
        sess._sandbox_status = sandbox_state
        sandbox_decision = sandbox_capability = runtime_capability = None
        sandbox_error = sandbox_state["error"]

    route = client.route_for(getattr(sess.ag, "gateway", None))
    secure_transport = str(route.base).lower().startswith("https://")
    try:
        active = len(store.list_session_summaries(
            limit=None, status="active", scope="all"))
        archived = len(store.list_session_summaries(
            limit=None, status="archived", scope="all"))
        inventory = f"{active} active / {archived} archived"
    except Exception as exc:
        inventory = f"ERROR {type(exc).__name__}: {exc}"
    shadow = store.shadow_status()
    try:
        metrics_diag = store.metrics_facade().diagnostics(read_only=True)
        integrity = metrics_diag["integrity"]
        fk = metrics_diag["foreign_key_violations"]
        initialized = bool(metrics_diag.get("initialized"))
        metrics_ok = initialized and integrity == ["ok"] and not fk
        counts = metrics_diag["counts"]
        metrics_text = (
            f"{'OK' if metrics_ok else 'NOT INITIALIZED' if not initialized else 'ERROR'} · "
            f"{metrics_diag['path']} · "
            f"requests={counts['requests']} tools={counts['tool_runs']} "
            f"health={counts['model_health']} · integrity={integrity} · "
            f"fk={len(fk)}")
    except Exception as exc:  # noqa: BLE001 - diagnosis must remain usable
        metrics_text = f"ERROR {type(exc).__name__}: {exc}"
    artifact_ok = (
        store.ARTIFACTS.is_dir()
        and os.access(store.ARTIFACTS, os.W_OK | os.X_OK))
    external_tools, external_hints = _external_tools_row()
    instructions_row, instruction_hints = _instructions_row()
    rows = [
        ("sandbox", sandbox_state.get("marker") or "UNKNOWN"),
        ("sandbox adapter", getattr(
            runtime_capability, "adapter", "-") if runtime_capability else "-"),
        ("runtime capable", (
            f"mount={bool(getattr(runtime_capability, 'available', False))} "
            f"netns={bool(getattr(runtime_capability, 'network_isolation', False))}")),
        ("config ready", str(bool(
            sandbox_state.get("configuration_ready", False)))),
        ("protected enforced", str(bool(
            getattr(sandbox_decision, "protected_paths_enforced", False)))),
        ("network effective", sandbox_state.get(
            "network_marker") or "NET UNKNOWN"),
        ("sandbox reason", str(
            getattr(sandbox_decision, "reason", "")
            or sandbox_state.get("configuration_error")
            or sandbox_error or "-")),
        ("gateway", f"{route.name} · {route.base}"),
        ("API key", _key_row(route)),
        ("transport", "HTTPS" if secure_transport else "HTTP (credential exposure risk)"),
        ("SQLite shadow", (
            f"enabled={shadow['enabled']} active={shadow['active']} "
            f"broken={shadow['broken'] or '-'}")),
        ("metrics DB", metrics_text),
        ("artifacts", f"{'writable' if artifact_ok else 'NOT WRITABLE'} · {store.ARTIFACTS}"),
        ("terminal", (
            f"stdin_tty={sys.stdin.isatty()} stdout_tty={sys.stdout.isatty()} "
            f"tui={tui.supported()}")),
        ("external tools", external_tools),
        ("instructions", instructions_row),
        ("sessions", inventory),
        ("session payload", _doctor_session_payload(getattr(sess, "ag", None))),
        ("recent failures", _doctor_recent_failures()),
        ("circuit", _doctor_circuit(
            route.name, getattr(sess.ag, "model", ""))),
    ]
    print("  Doctor")
    for label, value in rows:
        color = RED if (
            "BLOCKED" in value or "NOT WRITABLE" in value
            or value.startswith("MISSING")
            or "exposure risk" in value or value.startswith("ERROR")) else DIM
        print(f"    {label:<18} {color(value)}")
    for hint in external_hints:
        print(RED(hint) if hint.startswith("!") else DIM(hint))
    for hint in instruction_hints:
        print(YELLOW(hint))
    if not secure_transport:
        print(RED(
            "  ! 当前 gateway 使用 HTTP；API key 与请求内容可能被链路观察者读取。"))
    print(DIM(
        "  sandbox 只在 protected enforced=True 时给列出的本地路径加 OS 只读边界；"
        "不代表整个 workspace/filesystem 被隔离。"))
    if sandbox_state.get("network_marker") == "NET OPEN":
        print(YELLOW(
            "  ! trusted 配置已显式开放 outbound；当前 Bash 没有网络边界。"
            "动态 remote API/rclone 不能由静态 guard 完整覆盖。"))
    elif sandbox_state.get("network_marker") == "NET OPEN IF APPROVED":
        print(YELLOW(
            "  ! sandbox 不可用时仅可通过本次 UNSANDBOXED 确认开放网络；"
            "确认前保持阻断。"))



# /doctor 的指令记忆体检。
#
# 指令是**每一次请求都要重发**的固定开销，而它的来源分散在从文件系统根到 cwd
# 的一串目录里——哪几份被读进来、加起来多大，以前在界面上一个字都看不到。
# 被拒的 @import（指向工作目录之外、或疑似凭据）尤其要让人看见：静默丢掉的话，
# 用户只会以为「我写的规则怎么不生效」。
def _instructions_row():
    from core import instructions as instruction_memory   # noqa: PLC0415
    try:
        bundle = instruction_memory.collect()
    except Exception as exc:                              # noqa: BLE001
        return f"ERROR {exc.__class__.__name__}", []
    if not bundle.blocks:
        return "（无 AGENTS.md / CLAUDE.md）", []
    names = ", ".join(
        f"{os.path.basename(b.path)}@{b.scope}" for b in bundle.blocks)
    row = f"{len(bundle.blocks)} 份 / {bundle.total_chars} 字符 · {names}"
    return row, [f"  ! {note}" for note in bundle.notes]


# /doctor 的外部工具体检。
#
# 为什么值得单独一行：2026-09-18 的真实排查里，用户机器上 Git **是装了的**，
# 但安装器默认只把 `<Git>\cmd` 加进 PATH，`usr\bin` 不在 —— 于是
# bash/sed/awk/ssh 全都找不到，Bash 工具整个不可用。这个故障当时是在模型
# 跑到一半时才以一句「找不到 bash」暴露出来的，而它本该在 /doctor 里一眼看见。
_DOCTOR_REQUIRED_TOOLS = (
    # bash：Bash 工具就是 `bash -lc`，没有它这个工具直接不可用。
    # git ：zylab 自己要用（repomap 的 git ls-files、状态栏、版本号、仓库根）。
    "bash", "git")
# 模型写的 bash 命令实际用到的程序，按 2115 条真实历史命令的频次排序。
# Windows 上它们全部来自 Git for Windows 的 usr\bin —— 少一个就少一片能力，
# 而且失败形式是「命令 127」，模型多半会反复重试而不是换路子。
_DOCTOR_SHELL_TOOLS = ("grep", "head", "sed", "tail", "cut", "awk", "sort", "wc")


def _external_tools_row():
    """返回 (行内容, 补救提示行列表)。缺东西时行首是 MISSING，好让上面的着色逻辑标红。"""
    import shutil                                       # noqa: PLC0415
    resolved = {}
    missing_required, missing_shell = [], []
    for name in _DOCTOR_REQUIRED_TOOLS:
        path = shutil.which(name)
        resolved[name] = path
        if not path:
            missing_required.append(name)
    for name in _DOCTOR_SHELL_TOOLS:
        if not shutil.which(name):
            missing_shell.append(name)

    hints = []
    if not missing_required and not missing_shell:
        return (f"OK · bash={resolved['bash']} · git 与 "
                f"{len(_DOCTOR_SHELL_TOOLS)} 个常用 shell 工具齐全"), hints

    parts = []
    if missing_required:
        parts.append("缺必需：" + ", ".join(missing_required))
    if missing_shell:
        parts.append(f"缺 shell 工具 {len(missing_shell)} 个："
                     + ", ".join(missing_shell))
    value = "MISSING · " + "；".join(parts)

    if "bash" in missing_required:
        hints.append("! 没有 bash：Bash 工具不可用（grep 工具是进程内实现，不受影响）。")
    if "git" in missing_required:
        hints.append("! 没有 git：repo map、状态栏分支、版本号都会退化。")
    if sys.platform == "win32":
        hints.append(
            "  装 Git for Windows（https://git-scm.com/download/win），"
            "然后把 <Git>\\usr\\bin 加进 PATH —— "
            "**安装器默认只加 <Git>\\cmd**，而 bash/grep/sed/ssh 都在 usr\\bin 里。")
        hints.append(
            "  PATH 改完只对新进程生效；已开着的终端要重开。")
    else:
        hints.append("  用系统包管理器补齐（Debian/Ubuntu: apt install git coreutils）。")
    return value, hints


def _key_row(route):
    """/doctor 的 key 行 —— 只说来源，不说内容。"""
    try:
        status = client.key_status(route)
    except Exception as exc:                            # noqa: BLE001
        return f"ERROR {type(exc).__name__}: {exc}"
    if status["found"]:
        return f"OK · {status['source']}"
    return ("MISSING · 找过环境变量 " + ", ".join(status["names"])
            + " 与 " + ", ".join(status["candidates"]))

_PERMISSION_MARK = {
    "allow": "✓ allow",
    "ask": "? ask",
    "hard": "! hard",
    "deny": "✕ deny",
}


def _permission_rows(sess):
    by_name = {
        row["tool"]: row
        for row in CFG.permission_rows(cfg=sess.cfg)
    }
    names = []
    for item in tools.SCHEMA:
        name = item["function"]["name"]
        names.append(name)
        if name == "memory_write":
            names.extend(_SCOPED_PERMISSION_NAMES)
    rows = []
    for name in names:
        if name not in by_name:
            continue
        row = dict(by_name[name])
        if name in _HARD_MEMORY_PERMISSION_NAMES:
            tool_name, args = _memory_policy_call(name)
            _, configured = _configured_permission(
                sess.cfg, tool_name, args)
            row["hard_guard"] = True
            row["project_enforced"] = False
            if configured == "deny":
                row["effective"] = "deny"
                row["source"] = "effective_config"
            else:
                row["effective"] = "hard"
                row["source"] = "hard_guard"
        rows.append(row)
    return rows


def _render_permission(row):
    source = {
        "default": "内置默认",
        "user": "用户配置",
        "project": "项目配置",
        "hard_guard": "不可绕过的 hard guard",
        "effective_config": "组合配置 deny",
    }.get(row["source"], row["source"])
    if row.get("project_enforced"):
        source += f"强制（用户层 {row.get('user') or row['default']}）"
    # 同上：替换字段里不能换行，否则 3.10/3.11 直接 SyntaxError。
    mark = _PERMISSION_MARK.get(row["effective"], row["effective"])
    return (
        f"{tui.pad_display(row['tool'], 20)} "
        f"{tui.pad_display(mark, 9)}"
        f" · {source}")


def _print_permissions(sess):
    print()
    print(f"  {BOLD('工具权限')}  {DIM('effective · source')}")
    for row in _permission_rows(sess):
        print("  " + _render_permission(row))
    if getattr(sess, "auto", False):
        print(YELLOW(
            "\n  当前 /auto 已开启：普通 ask 会自动批准；"
            "hard confirmation 与 deny 不受影响。"))
    print(DIM(
        f"\n  用户配置: {CFG.USER_FILE}\n"
        "  用法: /permissions <tool> <allow|ask|deny|reset>"))


def _set_permission(sess, tool, value):
    known = {item["function"]["name"] for item in tools.SCHEMA}
    known.update(_SCOPED_PERMISSION_NAMES)
    if tool not in known:
        print(RED(
            f"  未知工具 {tool!r}；可选：{', '.join(sorted(known))}"))
        return False
    if (tool in _HARD_MEMORY_PERMISSION_NAMES
            and value in {"allow", "ask"}):
        print(RED(
            f"  {tool} 使用不可绕过的 hard confirmation；"
            "只能设为 deny 或 reset"))
        return False
    # Slash commands are not followed by an automatic Session.save().  Revoke
    # a session-scoped bypass durably before changing the broader config, or a
    # later /exit + /resume would resurrect the old grant.
    if tool in sess.always:
        sess.always.discard(tool)
        try:
            sess.save()
        except Exception as exc:  # noqa: BLE001 - permission path fails closed
            sess.always.add(tool)
            print(RED(
                "  权限配置未修改：无法持久化 session grant 撤销："
                f"{type(exc).__name__}: {exc}"))
            return False
    stored = None if value == "reset" else value
    try:
        CFG.update_user_permission(tool, stored)
        cfg, _ = CFG.load()
    except CFG.SettingsError as exc:
        print(RED(f"  权限配置未修改：{exc}"))
        return False
    sess.cfg = cfg
    sess.ag.hook_cfg = cfg
    tools.HOOK_CTX["cfg"] = cfg
    row = next(
        item for item in _permission_rows(sess)
        if item["tool"] == tool)
    requested = "reset" if stored is None else stored
    print(DIM(
        f"  ✓ {tool}: 用户层 {requested} · "
        f"当前 effective={row['effective']} ({row['source']})"))
    return True


def _pick_permission_value(sess, row):
    if row.get("hard_guard"):
        choices = [
            ("deny", "Deny"),
            ("reset", "Reset to hard confirmation（推荐）"),
        ]
    else:
        choices = [
            ("ask", "Ask each time（推荐）"),
            ("allow", "Allow automatically"),
            ("deny", "Deny"),
            ("reset", "Reset user override"),
        ]
    picked = sess.pick(
        choices, title=f"{row['tool']} · 当前 {row['effective']}",
        page=len(choices), allow_filter=False,
        render=lambda choice: choice[1])
    return picked[0] if picked else None


def _announce_session_grants(sess):
    """恢复会话时把残留的「本会话都允许」说出来：它们随会话文件跨 /resume 持久，
    一个月前点的 bash 全放行今天仍然有效（2026-09-04 事故）。"""
    grants = sorted(getattr(sess, "always", None) or ())
    if not grants:
        return ""
    line = (
        f"  [已恢复本会话授权：{', '.join(grants)} · 这些工具不再逐次确认；"
        "/permissions revoke 可撤销]")
    print(YELLOW(line))
    return line


def cmd_permissions(sess, rest):
    """集中查看/修改工具权限；交互模式可连续编辑多个工具。"""
    if str(rest or "").strip().lower() in {"revoke", "revoke-session"}:
        # 撤销「本会话都允许」的授权；配置层 allow/ask/deny 不动
        grants = sorted(getattr(sess, "always", None) or ())
        sess.always = set()
        try:
            sess.save()
        except Exception as exc:                        # noqa: BLE001
            print(RED(f"  [permissions] 撤销已生效但会话未能保存：{exc}"))
        print(DIM(
            "  [已撤销本会话授权：" + (", ".join(grants) or "（本来没有）") + "]"))
        return
    # 面板打开时吸收其他 terminal 已提交的权限变化；只刷新配置快照，
    # 不替换当前 Agent 的 model/gateway。
    cfg, _ = CFG.load()
    sess.cfg = cfg
    sess.ag.hook_cfg = cfg
    tools.HOOK_CTX["cfg"] = cfg
    parts = rest.split()
    if len(parts) > 2:
        print(RED(
            "  用法: /permissions [tool [allow|ask|deny|reset]] · /permissions revoke 撤销本会话授权"))
        return
    if parts:
        tool = parts[0].lower()
        rows = {row["tool"]: row for row in _permission_rows(sess)}
        if tool not in rows:
            _set_permission(sess, tool, "ask")
            return
        if len(parts) == 2:
            value = parts[1].lower()
            if value not in ("allow", "ask", "deny", "reset"):
                print(RED(
                    "  权限必须是 allow / ask / deny / reset"))
                return
            _set_permission(sess, tool, value)
            return
        if getattr(sess, "pump", None) is None:
            print("  " + _render_permission(rows[tool]))
            return
        value = _pick_permission_value(sess, rows[tool])
        if value is not None:
            _set_permission(sess, tool, value)
        return

    if getattr(sess, "pump", None) is None:
        _print_permissions(sess)
        return

    while True:
        picked = sess.pick(
            _permission_rows(sess),
            title="工具权限 · Enter 修改 · Esc 关闭",
            page=12, allow_filter=True,
            render=_render_permission)
        if picked is None:
            return
        value = _pick_permission_value(sess, picked)
        if value is not None:
            _set_permission(sess, picked["tool"], value)


def cmd_backups(sess, rest):
    baks = sorted(store.BACKUPS.glob("*"), reverse=True)[:15]
    if not baks:
        print(DIM("  (还没有备份)"))
    for b in baks:
        orig = "/" + b.name.split("__", 1)[-1].replace("%", "/")
        print(f"  {DIM(b.name[:17])}  {orig}")
    print(DIM(f"  共 {len(list(store.BACKUPS.glob('*')))} 份 · {store.BACKUPS}"))


def cmd_stats(sess, rest):
    st = store.rebuild_stats()
    print(DIM(f"  [已重算 → {store.STATS_CACHE}]"))
    print(f"  {st['totalSessions']} 会话 · {st['totalCalls']} 次调用 · "
          f"{st['totalTokens']:,} tokens")
    for g, c in st["gatewayUsage"].items():
        print(DIM(f"    {g:12} in {c['inputTokens']:>10,} / "
                  f"out {c['outputTokens']:>8,} · {c['calls']} 次"))


def cmd_usage(sess, rest):
    """Legacy-compatible usage tables plus authoritative M5 coverage."""
    value = str(rest or "").strip()
    days = int(value) if value.isdigit() else None
    session = value if value and days is None else None
    show_usage(days=days, session=session)
    since = (
        datetime.now(timezone.utc) - timedelta(days=days)
        if days is not None else None)
    try:
        summary = store.metrics_facade().usage_summary(
            session_id=session, since=since)
    except Exception as exc:  # noqa: BLE001 - usage UI remains diagnostic
        print(RED(
            f"  M5 measurement coverage 不可用："
            f"{type(exc).__name__}: {exc}"))
        return
    print(f"  {BOLD('M5 provider attempts（不与上表重复相加）')}")
    print(
        f"  调用 {summary['attempts']:,} · "
        f"usage measured {summary['measured']:,} · "
        f"usage unknown {summary['unknown']:,}")
    print(
        f"  measured tokens  in {summary['prompt_tokens']:,} / "
        f"out {summary['completion_tokens']:,}")
    print(
        f"  cache measured {summary['cache_measured']:,} calls · "
        f"cache unknown {summary['cache_unknown']:,} calls · "
        f"read {summary['cache_read']:,} / write {summary['cache_write']:,}")
    print(DIM(
        f"  status ok={summary['ok']} failed={summary['failed']} "
        f"interrupted={summary['interrupted']} running={summary['running']}"))
    print(DIM(
        "  usage.jsonl 是兼容成功日志；M5 rows 覆盖 retry/error/no-usage，"
        "两者可能重叠，故分别展示。"))


def _task_status(snapshot):
    value = snapshot.status
    return value.value if hasattr(value, "value") else str(value)


def _task_field(snapshot, name, default=None):
    if isinstance(snapshot, dict):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def _task_outcome(snapshot, *, error_limit=240):
    """统一呈现进程退出与基础设施错误，不改变 task lifecycle 状态。"""
    parts = []
    if _task_field(snapshot, "timed_out", False):
        parts.append("timeout")
    signal_value = _task_field(snapshot, "signal")
    returncode = _task_field(snapshot, "returncode")
    if signal_value:
        parts.append(f"signal {signal_value}")
    elif returncode not in (None, 0):
        parts.append(f"exit {returncode}")
    error = " ".join(str(
        _task_field(snapshot, "error", "") or "").split())
    if error:
        limit = max(16, int(error_limit))
        if len(error) > limit:
            error = error[:limit - 3] + "..."
        parts.append(f"error {error}")
    return " · ".join(parts)


def _task_elapsed(snapshot):
    import datetime as dt
    start = snapshot.started_at or snapshot.created_at
    end = snapshot.ended_at
    try:
        started = dt.datetime.fromisoformat(start)
        ended = (
            dt.datetime.fromisoformat(end)
            if end else dt.datetime.now(dt.timezone.utc))
        return max(0.0, (ended - started).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _task_size(value):
    value = max(0, int(value or 0))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024


# 错误只有一种形状：「⏺ 错误 · 一句话」再一行「⎿  下一步怎么办」。下一步按 client 的
# error kind 给；用户看到错误时最需要的是出路，不是堆栈。命令名都必须真实存在。
_ERROR_NEXT_STEPS = {
    "authentication": "key 无效或过期 · 检查 keys.env，或 zylab init 重写",
    "permission": "该 key 无权用这个模型/网关 · /model 或 /gateway 换一个",
    "quota": "额度用尽 · /gateway 换网关，或等额度恢复",
    "rate_limit": "被限流 · 稍等再发；反复出现就 /model 换一个",
    "model_unavailable": "该网关没有这个模型 · /model 换一个",
    "transient_http": "网关暂时故障 · 再发一次；反复出现就 /gateway 换一个",
    "network": "连不上网关 · 检查网络路由/代理；/gateway 看当前地址",
    "stream_read": "流中途断了 · 再发一次；反复出现就 /model 换一个",
    "stream_eof": "网关提前收流 · 再发一次；反复出现就 /model 换一个",
    "insecure_transport": "改用 https 网关，或非交互模式加 --allow-insecure-http",
    # 「稍等再试」曾是假的：熔断检查发生在请求之前，重试必被挡在同一道门上。
    # 真正立刻有效的是显式改路由（走 _apply_route → mark_route_explicit）。
    "circuit_open": "该网关刚连续失败、已暂时熔断 · /model 重选（同一个也行）即可立即越过，或 /gateway 换一个",
    "context_length": "上下文超限 · /compact 压缩，或 /clear 开新会话",
    "capability_temperature": "该模型不接受 temperature · /model 换一个",
    "decision_gate_required": "先让主 agent 发起 decision_gate 再继续",
    "attachment_error": "检查附件路径与大小后重发",
}
_ERROR_NEXT_DEFAULT = "再发一次 · 反复出现请 /doctor 看诊断"


def _error_lines(message, kind=None):
    """错误行的唯一形状：返回 (``⏺ 错误 · 一句话``, ``  ⎿  下一步``)。"""
    text = str(message or "").strip()
    first = text.splitlines()[0].strip() if text else "未知错误"
    if len(first) > 200:
        first = first[:199] + "…"
    key = str(kind or "").strip()
    if not key and first.startswith("附件"):
        key = "attachment_error"
    step = _ERROR_NEXT_STEPS.get(key, _ERROR_NEXT_DEFAULT)
    return RED("⏺ 错误 · " + first), DIM("  ⎿  " + step)


def _build_id():
    """自包含目录里若带 .git 就报 short SHA；复制出去没有 .git 时不猜。"""
    root = str(paths.app_root())
    if not os.path.isdir(os.path.join(root, ".git")):
        return ""
    try:
        done = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, encoding="utf-8", errors="replace", text=True, timeout=1.0)
    except Exception:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _startup_banner(sess, model, width=None, cwd=None, build=None):
    """启动横幅：像 Claude Code 一样一个小方框——名字、模型@网关、目录、帮助行。

    注意横幅里**不能出现「空闲」二字**：PTY 测试用 _expect("空闲") 等第一帧状态栏来
    同步 cbreak 时机，横幅先于 pump 打印，撞词会让同步提前、按键落进死缓冲
    —— 2026-08-27 一字之差挂了 5 个 PTY 测试。「/help 看命令」是会话选择器 PTY 测试
    的同步锚点，必须保留。全部按键仍只在 ? / /keys 卡片里（SPEC-CC-parity G3）。
    """
    if width is None:
        width = tui._cols()
    gateway = getattr(getattr(sess, "agent", None), "gateway", None) or client.GATEWAY
    build = _build_id() if build is None else build
    cwd = os.getcwd() if cwd is None else str(cwd)
    inner = max(24, min(int(width) - 3, 78))     # 含左右各一格留白；最后一列留空
    limit = inner - 2
    path_room = limit - tui.display_width("  目录  ")
    if tui.display_width(cwd) > path_room:
        cwd = "…" + cwd[-max(1, path_room - 1):]
    title_plain = f"◆ zylab" + (f" · build {build}" if build else "")
    title = (f"{ORANGE('◆')} {BOLD('zylab')}"
             + (DIM(f" · build {build}") if build else ""))
    help_line = "  /help 看命令 · ? 看快捷键 · Shift/Alt+Enter=换行 · Esc=中断"
    if tui.display_width(help_line) > limit:       # 窄终端：短版，锚点不变
        help_line = "  /help 看命令 · ? 看快捷键 · Esc=中断"
    rows = [
        (title_plain, title),
        ("", ""),
        (f"  模型  {model} @ {gateway}", f"  模型  {BOLD(str(model))} @ {gateway}"),
        (f"  目录  {cwd}", f"  目录  {cwd}"),
        ("", ""),
        (help_line, DIM(help_line)),
    ]
    lines = [DIM("╭" + "─" * inner + "╮")]
    for plain, styled in rows:
        if tui.display_width(plain) > limit:
            plain = tui.truncate_display(plain, limit)
            styled = plain
        pad = " " * (limit - tui.display_width(plain))
        lines.append(DIM("│") + " " + styled + pad + " " + DIM("│"))
    lines.append(DIM("╰" + "─" * inner + "╯"))
    return lines


# SPEC-CC-parity D2：首字之前的等待要被解释，不能只是一个数秒的 spinner。
# phase 来自 client.stream_chat 的标记（queued → started → headers → first_delta）。
_PHASE_LABELS = {
    None: "思考中", "queued": "排队中", "started": "连接中", "headers": "等待首字",
    "first_delta": "输出中", "last_delta": "输出中", "ended": "思考中",
    "compaction": "压缩上下文中",
}
SLOW_FIRST_TOKEN_SECONDS = 8.0
# 有些模型/网关会一直不开口（用户 2026-09-04 反馈）。超过这个时长就把出路写进状态栏，
# 而不是让人对着 spinner 猜；换模型是用户的决定，这里不静默自动换。
UNRESPONSIVE_SECONDS = 30.0


def _phase_activity(phase, age=None):
    """把请求阶段翻译成状态栏标签；慢到一定程度时说明慢在哪一段。"""
    label = _PHASE_LABELS.get(phase, "思考中")
    try:
        age = float(age or 0.0)
    except (TypeError, ValueError):
        age = 0.0
    if phase in ("started", "headers") and age >= UNRESPONSIVE_SECONDS:
        return label + " · 可能无响应：esc 中断后 /model 换一个"
    if age >= SLOW_FIRST_TOKEN_SECONDS:
        if phase == "started":
            label += " · 慢：网关尚未应答"
        elif phase == "headers":
            label += " · 慢：网关已应答，模型尚未开口"
    return label


def _tool_message_for_expand(messages, identifier=None):
    rows = [message for message in messages or ()
            if isinstance(message, dict)
            and message.get("role") == "tool"
            and str(message.get("tool_call_id") or "")]
    hint = str(identifier or "").strip()
    if not hint:
        return rows[-1] if rows else None
    exact = [row for row in rows
             if str(row.get("tool_call_id") or "") == hint]
    if len(exact) == 1:
        return exact[0]
    matches = [row for row in rows
               if str(row.get("tool_call_id") or "").startswith(hint)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise TASKS.TaskError(
            f"tool_call_id 前缀 {hint!r} 匹配 {len(matches)} 项")
    return None


def _write_expanded_text(value):
    text = tui.sanitize_terminal_text(str(value or ""))
    sys.stdout.write(text)
    if text and not text.endswith(("\n", "\r")):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _fallback_result_page(message, page, *, page_bytes=None):
    limit = max(4_096, min(
        100_000, int(page_bytes or TASKS.ARTIFACT_PAGE_BYTES)))
    content = str((message or {}).get("content") or "")
    payload = content.encode("utf-8", "replace")
    offset = (int(page) - 1) * limit
    if payload and offset >= len(payload):
        raise TASKS.TaskError(
            f"会话记录只有 {(len(payload) + limit - 1) // limit} 页")
    chunk = payload[offset:offset + limit]
    return {
        "text": chunk.decode("utf-8", "replace"),
        "start": min(offset, len(payload)),
        "end": min(len(payload), offset + len(chunk)),
        "total_bytes": len(payload),
        "has_more": offset + len(chunk) < len(payload),
    }


def cmd_keys(sess, rest):
    """快捷键总览（SPEC-CC-parity G2）—— 一屏，只列稳定词汇；也可直接输入 ?。"""
    rows = [
        ("Enter", "提交；工作中 = steer（插话）"),
        ("Shift+Enter / Alt+Enter", "换行不提交"),
        ("Esc", "中断当前 turn"),
        ("Tab", "补全；工作中 = 排到下一轮"),
        ("Shift+Tab", "循环权限模式：默认 → ⏵⏵ 接受编辑 → plan"),
        ("Ctrl+O", "展开最近一段折叠输出（工具输出 / 思考）"),
        ("Ctrl+R", "反向搜索历史"),
        ("Ctrl+B", "把当前 Bash 转后台（/tasks 查看）"),
        ("Ctrl+T", "agents 面板"),
        ("Ctrl+C", "清空草稿；空草稿连按两次退出"),
        ("Ctrl+D", "退出"),
        ("↑ / ↓", "翻历史；工作中 ↑ = 取回 queue"),
        ("PgUp / PgDn", "滚对话（全屏）；鼠标：滚轮滚对话 · 拖选松手即复制 · Shift+拖拽=终端原生选择 · /mouse off 交还"),
        ("/  @  !  ?", "命令（/cmd + 空格看二/三级）· 文件 · 直跑 shell · 本总览"),
    ]
    width = max(len(k) for k, _ in rows)
    print(DIM("  快捷键"))
    for key, desc in rows:
        print(f"  {BOLD(key.ljust(width))}  {DIM(desc)}")


def cmd_expand(sess, rest):
    """Expand a saved tool artifact without mutating provider messages.

    `/expand think` 展开上一个 turn 折叠掉的思考（SPEC-CC-parity D1）。"""
    hint0 = str(rest or "").strip().split(" ")[0].lower()
    if hint0 in ("think", "thinking", "思考"):
        thinker = getattr(sess, "_last_thinking", None)
        if not thinker:
            print(DIM("  上一个 turn 没有思考内容。"))
            return
        text = thinker.text
        if thinker.chars > len(text):
            text += f"\n[…仅保留前 {len(text):,} 字，共 {thinker.chars:,}]"
        print(DIM("  ✻ 思考"))
        for line in text.splitlines():
            print(DIM("    " + line))
        return
    try:
        parts = shlex.split(str(rest or ""))
    except ValueError as exc:
        print(RED(f"  [expand] 参数有误：{exc}"))
        return
    if len(parts) > 2:
        print(DIM("  用法：/expand [tool_call_id|task_id] [page]"))
        return
    hint = parts[0] if parts else ""
    try:
        page = int(parts[1]) if len(parts) == 2 else 1
    except ValueError:
        print(RED("  [expand] page 必须是正整数"))
        return
    if page < 1:
        print(RED("  [expand] page 必须是正整数"))
        return

    manager = sess.task_manager
    entry = None
    index_error = None
    try:
        if hasattr(manager, "artifact_entry"):
            entry = manager.artifact_entry(hint) if hint else None
    except TASKS.TaskError as exc:
        index_error = str(exc)

    message_hint = (
        str((entry or {}).get("tool_call_id") or "")
        or hint)
    try:
        message = _tool_message_for_expand(
            sess.ag.messages, message_hint or None)
    except TASKS.TaskError as exc:
        print(RED(f"  [expand] {exc}"))
        return
    if not hint and message is not None and entry is None:
        message_hint = str(message.get("tool_call_id") or "")
        try:
            if hasattr(manager, "artifact_entry"):
                entry = manager.artifact_entry(message_hint)
        except TASKS.TaskError as exc:
            index_error = str(exc)
    if not hint and message is None and entry is None:
        try:
            rows = (
                manager.artifact_entries()
                if hasattr(manager, "artifact_entries") else [])
            entry = rows[-1] if rows else None
        except TASKS.TaskError as exc:
            index_error = str(exc)
        if entry is not None:
            message_hint = str(entry.get("tool_call_id") or "")
            message = _tool_message_for_expand(
                sess.ag.messages, message_hint)

    reference = str(
        (entry or {}).get("task_id")
        or message_hint or hint or "last")
    view = None
    if entry is not None and hasattr(manager, "artifact_page"):
        try:
            view = manager.artifact_page(
                entry.get("task_id") or entry.get("tool_call_id"),
                page=page)
        except TASKS.TaskError as exc:
            index_error = str(exc)
    if view is not None and view.get("sections"):
        current = int(view["page"])
        total_pages = max(1, (
            int(view["total_bytes"]) + int(view["page_bytes"]) - 1)
            // int(view["page_bytes"]))
        label = (
            f"  tool {(view.get('entry') or {}).get('tool_call_id') or '?'}"
            f" · task {(view.get('entry') or {}).get('task_id') or '?'}"
            f" · page {current}/{total_pages}"
            f" · bytes {view['start']:,}-{view['end']:,}/"
            f"{view['total_bytes']:,}")
        print(DIM(tui.sanitize_terminal_text(label)))
        for section in view["sections"]:
            if section.get("stream") == "stderr":
                print(DIM("  [stderr]"))
            _write_expanded_text(section.get("text"))
        if view.get("missing"):
            print(YELLOW(
                "  [完整输出已不可用：缺少 "
                + ", ".join(view["missing"]) + " artifact]"))
        if view.get("has_more"):
            print(DIM(
                f"  [还有下一页：/expand {reference} {current + 1}]"))
        return

    if message is None:
        detail = f"：{index_error}" if index_error else ""
        print(RED(f"  [expand] 找不到对应工具结果{detail}"))
        return
    try:
        fallback = _fallback_result_page(message, page)
    except TASKS.TaskError as exc:
        print(RED(f"  [expand] {exc}"))
        return
    detail = f"；{index_error}" if index_error else ""
    print(YELLOW(
        "  [完整输出已不可用；以下为会话记录中的有界结果"
        + detail + "]"))
    _write_expanded_text(fallback["text"])
    if fallback["has_more"]:
        print(DIM(
            f"  [还有下一页：/expand {message_hint} {page + 1}]"))


def cmd_tasks(sess, rest):
    """列出当前 CLI lifetime 内的 managed tasks。"""
    if rest.strip():
        print(DIM("  用法：/tasks"))
        return
    rows = sess.task_manager.list()
    if not rows:
        print(DIM("  (当前没有 managed tasks)"))
        return
    foreground = sess.task_manager.foreground()
    foreground_id = foreground.id if foreground is not None else None
    print(DIM(
        "  id        mode  status       elapsed   output    name / outcome"))
    for task in rows:
        mode = "bg" if task.background else (
            "fg" if task.id == foreground_id else "--")
        total = task.stdout_bytes + task.stderr_bytes
        outcome = _task_outcome(task)
        suffix = f" · {outcome}" if outcome else ""
        print(
            f"  {task.id[:8]:<8}  {mode:<4}  "
            f"{_task_status(task):<11}  {_task_elapsed(task):>6.1f}s  "
            f"{_task_size(total):>8}  {task.name}{suffix}")
    print(DIM(
        "  /task <id> tail/attach · /kill <id> terminate · "
        "Ctrl+B detach"))


def cmd_task(sess, rest):
    """显示 artifact tail；运行中的 background task 同时进入 live attach。"""
    value = rest.strip()
    if value.lower() == "detach":
        task_id = sess.detach_task()
        print(DIM(
            f"  [已 detach task {task_id}]"
            if task_id else "  [当前没有 attached task]"))
        return
    identifier, _, extra = value.partition(" ")
    if not identifier or extra.strip():
        print(DIM("  用法：/task <id>  或  /task detach"))
        return
    snapshot = sess.task_manager.get(identifier)
    if snapshot is None:
        print(RED(f"  [task] 未知 task：{identifier}"))
        return
    try:
        view = sess.task_manager.tail(snapshot.id, limit=8192)
    except TASKS.TaskError as exc:
        print(RED(f"  [task] {exc}"))
        return
    total = snapshot.stdout_bytes + snapshot.stderr_bytes
    outcome = _task_outcome(snapshot)
    suffix = f" · {outcome}" if outcome else ""
    print(DIM(
        f"  task {snapshot.id} · {_task_status(snapshot)} · "
        f"{_task_elapsed(snapshot):.1f}s · {_task_size(total)}{suffix}"))
    shown = False
    for stream in ("stdout", "stderr"):
        item = view.get(stream) or {}
        text = str(item.get("text") or "")
        if not text:
            continue
        shown = True
        if item.get("truncated"):
            print(DIM(
                f"  [{stream} tail {item.get('returned_bytes', 0):,}/"
                f"{item.get('total_bytes', 0):,} bytes]"))
        elif stream == "stderr":
            print(DIM("  [stderr]"))
        sys.stdout.write(text)
        if not text.endswith(("\n", "\r")):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if not shown:
        print(DIM("  (尚无输出)"))
    if (snapshot.background
            and _task_status(snapshot) not in {
                "completed", "failed", "cancelled"}):
        sess.attached_task_id = snapshot.id
        print(DIM(
            f"  [已 attach task {snapshot.id}；后续 chunk 实时显示，"
            "Ctrl+B detach]"))


def cmd_kill(sess, rest):
    """显式终止一个 managed task；foreground 仍走 Controller cancel intent。"""
    identifier, _, extra = rest.strip().partition(" ")
    if not identifier or extra.strip():
        print(DIM("  用法：/kill <id>"))
        return
    snapshot = sess.task_manager.get(identifier)
    if snapshot is None:
        print(RED(f"  [kill] 未知 task：{identifier}"))
        return
    if _task_status(snapshot) in {"completed", "failed", "cancelled"}:
        print(DIM(
            f"  [task {snapshot.id} 已是 {_task_status(snapshot)}]"))
        return
    foreground = sess.task_manager.foreground()
    if foreground is not None and foreground.id == snapshot.id:
        action, cancelled = sess.cancel_active_work()
        if action is None or cancelled is None:
            print(RED(
                f"  [kill] foreground task {snapshot.id} 无法经 Controller 取消"))
            return
    else:
        controller, turn_id = sess._background_task_context.get(
            snapshot.id, (sess.controller, None))
        if controller is not None:
            controller.record_event(
                "task_kill_requested",
                {"task_id": snapshot.id, "tool_call_id": snapshot.key},
                turn_id=turn_id)
        cancelled = sess.task_manager.cancel(snapshot.id)
    if cancelled is None:
        print(RED(f"  [kill] 未知 task：{identifier}"))
    else:
        print(DIM(f"  [正在终止 task {snapshot.id}]"))


def cmd_help(sess, rest):
    """帮助从注册表生成 —— 加命令不用再同步第二处。"""
    print()
    print("  斜杠命令")
    for name in sorted(REGISTRY):
        if name in HIDDEN_COMPAT_COMMANDS:
            continue
        print(f"    /{name:<14} {REGISTRY[name][1]}")
    custom = getattr(sess, "_custom_catalog", None)
    if custom and custom.commands:
        print()
        print("  自定义 prompt 命令（继承当前 session 权限）")
        for name, command in sorted(custom.commands.items()):
            print(f"    /{name:<14} {command.description} [{command.scope}]")
    print()
    print("  用法")
    print("    直接说要做什么。多步任务它会自己列清单、读代码、改、再跑起来验证。")
    print("    读类工具自动执行；写文件、改文件、跑命令要你确认（↑↓ 选择）。")
    print("    输入 / 弹命令面板；精确命令 Enter 立即提交，前缀可 Tab 补全。")
    print("    在 /cmd 后输入空格会弹出带说明的二级指令；有参数的指令")
    print("    会继续提示三级参数；也可直接输入自由参数。")
    print("    例如 /workflow auto 选择 on/off/status，/memory use 选择 on/off/status。")
    print("    Shift+Enter 或 Alt+Enter=换行；模型工作中 Enter=steer，Tab=下一轮，")
    print("    Up=取回 human queue；Esc=中断。")
    print("    工作中 /usage /cost /context /model health /auto 立即执行；")
    print("    /model <名字> /gateway 立即选择，但只在当前 turn 结束后切换。")
    print("    Bash 工作中：原始输出进入 scrollback；Ctrl+B 转后台，/tasks 查看。")
    print("    Ctrl+O 展开最近一段折叠的工具输出（没有工具输出时展开上一 turn 的思考）。")
    print("    Shift+Tab 循环权限模式：默认 → ⏵⏵ 接受编辑（改文件免确认）→ plan（只读）。")
    print("    Ctrl+R 反向搜索历史：打字过滤，再按 Ctrl+R 找更旧的，Enter 填入，Esc 取消。")
    print("    翻阅历史、拖拽选择、复制都是终端原生的（内联模式不接管鼠标）；")
    print("    --app 全屏模式下才由 zylab 接管鼠标，/mouse on|off 切换。")
    print("    空闲时 ↑↓ 翻历史，Ctrl-D 退出。")
    print()


def cmd_mouse(sess, rest):
    """全屏模式：/mouse on|off 开关鼠标接管（滚轮滚对话、拖选复制 vs 交还终端原生选择）；
    内联模式不接管鼠标，滚动与选择都是终端原生的。"""
    renderer = getattr(sess, "renderer", None)
    if renderer is None or not hasattr(renderer, "enable_mouse"):
        _session_notice(sess, "[当前没有 interactive renderer]", error=True)
        return
    if not getattr(sess, "app_mode", False) or type(renderer).__name__ != "AppRenderer":
        _session_notice(sess, "[内联模式不接管鼠标：滚动与选择都是终端原生的；全屏模式（默认）下 /mouse on|off 可切换]")
        return
    want = (rest or "").strip().lower()
    if want in {"off", "0", "false", "no", "native"}:
        renderer.disable_mouse()
        _session_notice(sess, "[鼠标已交还终端：拖选/滚动走终端原生；/mouse on 收回]")
        return
    if want in {"on", "1", "true", "yes"} or not want:
        if renderer.mouse_enabled and not want:
            _session_notice(sess, "[鼠标由 zylab 接管：滚轮滚对话 · 拖选松手即复制 · Shift+拖拽终端原生选择；"
                                  f"/mouse off 交还 · 本次已收到 {getattr(renderer, 'mouse_events', 0)} 个鼠标事件]")
            return
        if renderer.enable_mouse(force=True):
            _session_notice(sess, "[鼠标由 zylab 接管：滚轮滚对话 · 拖选松手即复制 · Shift+拖拽终端原生选择]")
        else:
            _session_notice(sess, "[当前终端不支持 SGR 鼠标报告]", error=True)
        return
    _session_notice(sess, "[用法：/mouse on | off]", error=True)


def cmd_skills(sess, rest):
    """List optional prompt-data skills without reading or executing bodies."""
    value = str(rest or "").strip().casefold()
    if value not in {"", "refresh"}:
        print(RED("  [skills] 用法：/skills [refresh]"))
        return
    index = (
        sess.refresh_skills_context()
        if value == "refresh"
        else getattr(sess, "skills_index", None))
    if not isinstance(index, dict):
        index = sess.refresh_skills_context()
    catalog = getattr(sess, "_skills_catalog", None)
    if catalog is None:
        catalog = SKILLS.Catalog({}, [], [], [], {})
    enabled = bool(getattr(
        getattr(sess, "ag", None), "load_skills", True))
    print("  Skills catalog")
    print(DIM(
        f"    prompt index {'enabled' if enabled else 'disabled'} · "
        f"{len(index.get('entries') or [])}/"
        f"{int(index.get('available') or 0)} indexed"))
    rows = sorted(
        catalog.skills.values(),
        key=lambda item: (
            SKILLS.SCOPE_PRIORITY.get(item.scope, 99), item.name))
    if rows:
        layout = _table_layout((
            tui.TableColumn(22, min_width=14),
            tui.TableColumn(9, min_width=7),
            tui.TableColumn(10, min_width=8),
            tui.TableColumn(66, min_width=24),
        ), indent=2, maximum=112)
        print(DIM(layout.row(("name", "scope", "bytes", "description"))))
        for item in rows:
            print(layout.row((
                item.name, item.scope, f"{item.bytes:,}",
                item.description), wrap_last=True))
    else:
        print(DIM("    (none found)"))
    for conflict in catalog.conflicts:
        print(YELLOW(
            f"    conflict {conflict['name']}: {conflict['winner']} wins; "
            f"ignored {conflict['ignored_scope']} {conflict['path']}"))
    for warning in index.get("warnings") or ():
        print(YELLOW(f"    warning: {warning}"))
    for error in catalog.errors:
        print(RED(f"    error: {error}"))
    if getattr(sess, "_skills_error", None):
        print(RED(f"    loader error: {sess._skills_error}"))


def cmd_commands(sess, rest):
    """Inspect or atomically refresh prompt-only markdown commands."""
    value = str(rest or "").strip()
    if value == "refresh":
        catalog = sess.refresh_custom_commands()
        print(DIM(
            f"  [custom commands 已刷新：{len(catalog.commands)} available · "
            f"{len(catalog.conflicts)} conflicts · {len(catalog.errors)} errors]"))
    else:
        catalog = getattr(sess, "_custom_catalog", None)
        if catalog is None:
            catalog = sess.refresh_custom_commands()
    if value and value != "refresh":
        name = value.removeprefix("/").casefold()
        command = catalog.commands.get(name)
        if command is None:
            print(RED(f"  [commands] 未找到 /{name}"))
            return
        print(f"  /{command.name} · {command.scope}")
        print(f"    description  {command.description}")
        print(f"    arguments    {command.argument_hint or '(free text)'}")
        print(f"    source       {command.path}")
        print(f"    sha256       {command.sha256}")
        print(DIM("    execution    prompt-only · inherits session permissions"))
        return
    print("  Custom commands")
    print(f"    user root     {catalog.roots.get('user', '?')}")
    print(f"    project root  {catalog.roots.get('project', '?')}")
    if not catalog.commands:
        print(DIM("    (none; add a direct *.md file, then /commands refresh)"))
    for name, command in sorted(catalog.commands.items()):
        print(
            f"    /{name:<20} {command.scope:<7} "
            f"{command.description}  {command.sha256[:12]}")
    for conflict in catalog.conflicts:
        print(YELLOW(
            f"    conflict /{conflict['name']}: {conflict['winner']} wins; "
            f"ignored {conflict['ignored_scope']} {conflict['path']}"))
    for error in catalog.errors:
        print(RED(f"    error: {error}"))


def cmd_clear(sess, rest):
    goal_was_armed = bool(
        isinstance(getattr(sess, "goal", None), dict)
        and sess.goal.get("phase") == "active"
        and getattr(sess, "_goal_activation", "disarmed") == "armed")
    if goal_was_armed:
        # Clearing the visible/model transcript removes the evidence trail the
        # autonomous loop was using.  Keep the durable objective, but require
        # an explicit resume before spending another request in the new context.
        sess._set_goal_runtime(sess.goal, activation="disarmed")
    sess.ag.messages = sess.ag.messages[:1]
    sess.ag.last_total = 0
    sess.ag.context_summary = None
    sess.ag.context_invalid_reason = None
    sess.ag.away_recap = None                # 那一行说的是被清掉的对话
    sess.ag._compact_failed_key = None
    sess.ag._last_age_notice_key = None
    sync_transcript = getattr(sess, "sync_transcript", None)
    if callable(sync_transcript):
        sync_transcript()
    suffix = "；goal 自动续轮已停用，/goal resume 可继续" if goal_was_armed else ""
    print(DIM(f"  [已清空{suffix}]"))


def _model_check_targets(sess):
    """要实测哪些 route：当前会话模型 + 席位池解析出来的模型。

    不是全表 —— 16 个模型就是 16 次真实请求。只测「真的会被用到」的那几个。
    """
    model, gateway = sess.route_selection()
    targets = [(gateway, model, "当前")]
    seen = {(gateway, model)}
    try:
        for row in M.workflow_default_rows():
            route = (str(row.get("gateway")), str(row.get("id")))
            if route not in seen:
                seen.add(route)
                targets.append((route[0], route[1], f"席位 {row.get('seat')}"))
    except Exception:                                 # noqa: BLE001
        pass
    return targets


def _cmd_model_check(sess, tail):
    """/model check —— 目录 + 实调两条证据都过一遍，报告与缓存的差异。

    为什么两条都要：2026-09-08 实测过两个方向的打脸 —— kimi-k3 在目录里却曾
    404，kimi-k3-256k 不在目录里却一直能调。任何一条单独都会得出错的结论。
    """
    if tail:
        print(RED("  /model check 不接受额外参数"))
        return
    _, gateway = sess.route_selection()
    with Spinner(f"抓取 {gateway} 目录"):
        try:
            result = M.refresh_catalog(gateway)
        except RuntimeError as exc:
            print(RED(f"  [目录抓取失败] {exc}"))
            result = None
    if result:
        print(DIM(f"  目录 {gateway}：{result['count']} 个模型在册"))
        notice = _catalog_change_notice(result)
        print(notice.rstrip("\r\n") if notice else DIM("  目录无变化"))

    targets = _model_check_targets(sess)
    print(DIM(f"  实测 {len(targets)} 个会被用到的 route（每个一次真实请求）："))
    changed = []
    for route_gateway, model, role in targets:
        before = M.get(route_gateway, model).get("status")
        with Spinner(f"实测 {model}"):
            try:
                record = M.probe(route_gateway, model)
            except Exception as exc:                  # noqa: BLE001
                print(f"    {model:28} {RED('探测失败')} {DIM(str(exc)[:50])}")
                continue
        status = record.get("status")
        if record.get("probe_blocked") == "insecure_transport":
            # 本地策略拦下的请求没出过这台机器，别冒充可用性结论
            print(f"    {model:28} {YELLOW('未实测')} "
                  f"{DIM('明文 HTTP 未授权，交互模式里确认一次即可')} {DIM(role)}")
            continue
        if status == "ok":
            tools_ok = (GREEN("工具") if record.get("supports_tools")
                        else YELLOW("无工具"))
            first = record.get("first_token_s")
            timing = DIM(f"{first:.1f}s") if first else ""
            print(f"    {model:28} {GREEN('可用')} {tools_ok} "
                  f"{timing} {DIM(role)}")
        else:
            print(f"    {model:28} {RED('不可用')} {DIM(str(record.get('error'))[:52])}"
                  f" {DIM(role)}")
        if before != status:
            changed.append(f"{model} {before} → {status}")
    if changed:
        print(YELLOW("  状态有变：" + "；".join(changed)))
    else:
        print(DIM("  实测结果与缓存一致"))


def _catalog_change_notice(result):
    """目录刷新只在**有变化**时吭声；没变化时的静默是功能，不是遗漏。"""
    if not result or not result.get("changed"):
        return ""
    parts = []
    for key, label in (("added", "新增"), ("delisted", "下架"),
                       ("restored", "回归")):
        names = result.get(key) or []
        if names:
            shown = "、".join(names[:4])
            more = f" 等 {len(names)} 个" if len(names) > 4 else ""
            parts.append(f"{label} {shown}{more}")
    if not parts:
        return ""
    return (f"  [模型目录 {result.get('gateway')} 已刷新："
            + "；".join(parts)
            + " · /model check 可逐个实测]\r\n")


def _anchor_notice(record):
    """摘要没复述的原文值有多少条。不改 _compaction_effect 的签名（它有测试钉着）。"""
    missing = (record or {}).get("missing_anchors") or []
    if not missing:
        return ""
    return (f"，{len(missing)} 条原文值未被摘要复述"
            "（已随 <pinned-facts> 原样带回）")


def _compaction_effect(before, after, summary_model=None, current_model=None):
    """把压缩前后的上下文 token 说清楚；拿不到数字就退回不吹牛的说法。"""
    swapped = (f"，由 {summary_model} 代写"
               if summary_model and summary_model != current_model else "")
    if not before or after is None:
        return "已生成摘要" + swapped
    return f"{before:,} → {after:,} tokens" + swapped


def cmd_compact(sess, rest):
    try:
        before = sess.ag.context_report()["estimated_request_tokens"]
    except Exception:                                 # noqa: BLE001
        before = None
    instructions = str(rest or "").strip() or None      # /compact 重点保留 X 的决定
    with Spinner("压缩中"):
        s = sess.ag.force_compact(instructions=instructions)
    failed = str(s or "").startswith("[摘要生成失败")
    if s:
        store.shadow_event(sess.ag,
                           "context_compaction_failed" if failed
                           else "context_compacted",
                           {"summary": s, "manual": True,
                            "summary_state": getattr(
                                sess.ag, "context_summary", None)})
    if failed:
        print(YELLOW(f"  {s}"))
    elif s:
        sess.save()
        try:
            after = sess.ag.context_report()["estimated_request_tokens"]
        except Exception:                             # noqa: BLE001
            after = None
        record = getattr(sess.ag, "context_summary", None) or {}
        used = record.get("model")
        print(DIM(
            "  [已压缩 —— "
            + _compaction_effect(before, after, used, sess.ag.model)
            + f"，摘要 {len(s):,} 字符"
            + _anchor_notice(record) + "]"))
    else:
        print(DIM("  [无需压缩]"))


def cmd_auto(sess, rest):
    sess.auto = not sess.auto
    sess._auto_override = sess.auto
    print(DIM(
        f"  [自动批准 {'开' if sess.auto else '关'} · "
        "仅影响尚未开始的后续审批]"))


def cmd_cost(sess, rest):
    show_cost(sess.ag)


def cmd_context(sess, rest):
    try:
        show_context(
            sess.ag, plan_mode=getattr(sess, "plan_mode", False))
    except ATTACHMENTS.AttachmentError as exc:
        print(RED(f"  Context attachment 投影失败：{exc}"))
        return
    index = getattr(sess, "memory_index", {}) or {}
    skills_index = getattr(sess, "skills_index", {}) or {}
    try:
        capsule = sess.build_context_capsule()
    except Exception as exc:
        print(RED(f"  Context Capsule 不可用：{type(exc).__name__}: {exc}"))
        return
    print(f"  {BOLD('长期上下文层')}")
    print(
        f"    memory use     {str(bool(sess.memory_use)).lower()} · "
        f"generate {str(bool(sess.memory_generate)).lower()}")
    print(
        f"    memory index   {len(index.get('entries') or [])}/"
        f"{int(index.get('available') or 0)} entries · "
        f"{int(index.get('chars') or 0):,} chars · "
        f"sha256 {str(index.get('sha256') or '?')[:16]}")
    skills_text = str(skills_index.get("text") or "")
    skills_tokens = (
        CONTEXT.estimate_tokens(skills_text) if skills_text else 0)
    print(
        f"    skills index   {len(skills_index.get('entries') or [])}/"
        f"{int(skills_index.get('available') or 0)} entries · "
        f"{len(skills_text):,} chars · "
        f"~{skills_tokens:,} tokens · "
        f"{'enabled' if bool(getattr(sess.ag, 'load_skills', True)) else 'disabled'}")
    print(
        f"    child capsule  {int(capsule.get('chars') or 0):,} chars · "
        f"sha256 {str(capsule.get('sha256') or '?')[:16]} · "
        f"source {capsule.get('source_session') or '?'}")
    print(
        f"    repo map       {len(getattr(sess, 'repo_map_index', '') or ''):,} "
        "chars · deterministic · untrusted repo data")
    print(
        f"    architecture   "
        f"{len(getattr(sess, 'architecture_index', '') or ''):,} "
        "chars · model-derived · untrusted")
    graft_status = GRAFT.local_status(
        getattr(sess, "cfg", CFG.DEFAULTS),
        workspace_root=getattr(sess, "_session_cwd", os.getcwd()))
    if graft_status.get("available"):
        graft_label = "indexed" if graft_status.get("indexed") else "lazy"
        print(
            f"    graft          0 injected chars · {graft_label} · "
            "hosted on-demand · untrusted repo data")
    elif graft_status.get("enabled"):
        print(
            "    graft          unavailable · "
            + str(graft_status.get("error") or "unknown"))
    else:
        print("    graft          disabled")
    if getattr(sess, "_repo_map_error", None):
        print(RED(f"    repo error     {sess._repo_map_error}"))
    print(DIM(
        "    transcript=事实权威 · compact=哈希绑定投影 · "
        "memory=带来源派生层 · capsule=子线程有界交接"))


def _memory_usage():
    print(DIM(
        "  /memory                              状态\n"
        "  /memory list                         global + 当前 project\n"
        "  /memory show <id>                    查看来源与正文\n"
        "  /memory remember [project|global] <text>  显式记住\n"
        "  /memory forget <id>                  删除一条派生记忆\n"
        "  /memory use on|off                   当前 chat 是否注入\n"
        "  /memory generate on|off              当前 chat 是否自动抽取\n"
        "  /memory capture                      立刻抽一次跨会话记忆\n"
        "  /memory refresh                      重新读取并注入索引\n"
        "  /memory capsule                      显示 child 交接元数据"))


def _map_usage():
    print(DIM(
        "  /map（兼容）         旧确定性导航（默认关闭；新任务优先用 Graft）\n"
        "  /map refresh|build   立即重算 wiring/map（零 provider）\n"
        "  /map check           精确哈希检查工作树漂移\n"
        "  /map viz             导出确定性 HTML + Mermaid\n"
        "  LLM 节点图已迁到 /architecture\n"
        "  该命令已从顶层菜单隐藏；显式输入仍可使用。"))


def _graft_usage():
    print(DIM(
        "  /graft [status] [path]    显示 hosted Graft 索引状态\n"
        "  /graft build [path]       首次/增量构建项目外结构索引\n"
        "  /graft rebuild [path]     丢弃复用提示并完整重建索引\n"
        "  /graft doctor [path]      检查 component lock、可信 executable、边界和源码规模\n"
        "  模型会按任务自动调用 graft_*；无需先运行 /graft open。\n"
        "  旧 /map 已从顶层入口隐藏；显式输入 /map ... 仍保留离线兼容。"))


def _render_graft_status(value):
    if not value.get("enabled"):
        return YELLOW("  graft disabled by effective user/project policy")
    if not value.get("available"):
        return RED("  graft unavailable · " + str(value.get("error") or "unknown"))
    state = "ready" if value.get("indexed") else "not built (lazy)"
    network = (
        "network namespace isolated" if value.get("network_isolated")
        else "network isolation unverified")
    return "\n".join((
        f"  graft hosted · {state}",
        DIM(f"    root        {value.get('root')}"),
        DIM(f"    executable  {value.get('executable')}"),
        DIM(f"    cache       {value.get('graph')}"),
        DIM(
            f"    provider 0 · {network} · "
            "repository-derived untrusted data"),
    ))


def cmd_graft(sess, rest):
    """Inspect or explicitly warm the controlled hosted-Graft sidecar."""
    try:
        parts = shlex.split(str(rest or ""))
    except ValueError as exc:
        print(RED(f"  [graft] 参数无法解析：{exc}"))
        return
    known = {"status", "build", "rebuild", "doctor", "help", "?"}
    if parts and parts[0].lower() in known:
        action = parts.pop(0).lower()
    else:
        action = "status"
    if action in {"help", "?"}:
        _graft_usage()
        return
    if len(parts) > 1:
        print(RED("  [graft] path 若含空格请用引号，且只能指定一个 target"))
        _graft_usage()
        return
    target = parts[0] if parts else "."
    if action in {"status", "doctor"}:
        value = GRAFT.local_status(
            sess.cfg, workspace_root=sess._session_cwd, path=target)
        print(_render_graft_status(value))
        if action == "doctor":
            try:
                component = GRAFT_COMPONENT.inspect()
            except GRAFT_COMPONENT.ComponentError as exc:
                print(RED(f"    component   unavailable · {exc}"))
            else:
                print(DIM("    component   " + GRAFT_COMPONENT.render(component)))
        if action == "doctor" and value.get("available"):
            try:
                policy = CFG.graft_policy(sess.cfg)
                stats = GRAFT.inventory(Path(value["root"]), policy)
            except GRAFT.GraftError as exc:
                print(RED(f"  [graft doctor] {exc}"))
            else:
                print(DIM(
                    f"    inventory   {stats['files']:,} source files · "
                    f"{stats['bytes']:,} bytes · within configured bounds"))
        return
    # 首次索引在这个仓库实测 20 多秒（164 文件），大仓库更久；只有一个 spinner 看起来
    # 像卡住。先把规模和预期说出来（2026-09-04 用户反馈 /graft 不顺）。
    try:
        preview = GRAFT.local_status(
            sess.cfg, workspace_root=sess._session_cwd, path=target)
        if preview.get("available") and (
                action == "rebuild" or not preview.get("indexed")):
            stats = GRAFT.inventory(
                Path(preview["root"]), CFG.graft_policy(sess.cfg))
            print(DIM(
                f"  {'重建' if action == 'rebuild' else '首次'}索引 "
                f"{stats['files']:,} 个源码文件（{stats['bytes'] / 1e6:.1f} MB）"
                "· 通常需要几十秒，请稍候"))
    except (GRAFT.GraftError, KeyError, TypeError, ValueError):
        pass
    try:
        with Spinner("Graft 结构索引"):
            envelope = GRAFT.execute(
                action, {"path": target}, cfg=sess.cfg,
                workspace_root=sess._session_cwd)
    except GRAFT.GraftError as exc:
        sess._graft_current = {
            "state": "failed", "action": action, "path": target}
        print(RED(f"  [graft] {exc}"))
        return
    sess._graft_current = {
        "state": "ready", "action": action,
        "root": envelope.get("root"),
        "files": (envelope.get("inventory") or {}).get("files", 0),
    }
    print(GRAFT.render(envelope))


def _architecture_usage():
    print(DIM(
        "  /architecture            显示 LLM 架构索引状态\n"
        "  /architecture build      先显示请求上限再显式构建；Esc 取消\n"
        "  /architecture check      检查来源漂移\n"
        "  /architecture viz        导出 LLM 节点图 HTML + Mermaid\n"
        "  只有 build 会调用 provider；其余命令零请求"))


def _build_deterministic_map(sess):
    policy = CFG.map_policy(sess.cfg)
    return REPOMAP.build_map(
        sess._session_cwd,
        max_files=policy["max_files"],
        max_dirs=policy["max_dirs"],
        hubs_per_dir=policy["hubs_per_dir"],
        hotspots=policy["hotspots"])


def cmd_map(sess, rest):
    """Deterministic Graft-style repository orientation; never calls a model."""
    print(DIM(
        "  [兼容命令] /map 已从顶层入口隐藏；新任务请使用 /graft。"
        " 当前路径仍为零 provider 的离线 fallback。"))
    value = str(rest or "").strip()
    action, _, tail = value.partition(" ")
    action = action.lower() or "status"
    tail = tail.strip()
    root = sess._session_cwd
    policy = CFG.map_policy(sess.cfg)
    if action in {"help", "?"}:
        _map_usage()
        return
    if tail or action not in {"status", "build", "refresh", "check", "viz"}:
        print(RED(f"  [map] 未知参数：{value}"))
        _map_usage()
        return
    try:
        if action in {"build", "refresh"}:
            payload = _build_deterministic_map(sess)
            sess.refresh_repo_context()
            totals = payload["totals"]
            print(GREEN(
                f"  ✓ deterministic map · {totals['files']} files · "
                f"{totals['symbols']} symbols · {totals['edges']} edges · "
                "0 provider requests"))
            return
        if action == "check":
            drift = REPOMAP.check_map(
                root, max_files=policy["max_files"])
            if drift is None:
                print(DIM("  还没有 deterministic map。/map build 生成。"))
                return
            if drift["fresh"]:
                print(DIM("  ✓ deterministic map 与工作树一致"))
                return
            for label, key in (("改动", "changed"), ("删除", "removed"),
                               ("新增", "added")):
                for rel in drift[key]:
                    print(DIM(f"  {label}  {rel}"))
            print(DIM("  /map refresh 零 provider 重算"))
            return
        if action == "viz":
            payload = REPOMAP.load_map(root)
            if payload is None:
                payload = _build_deterministic_map(sess)
            drift = REPOMAP.check_map(
                root, hash_check=False,
                max_files=policy["max_files"])
            html_path, mmd_path = REPOMAP.export_visualizations(
                root, kind="map",
                html=REPOMAP.render_map_viz_html(payload, drift),
                mermaid=REPOMAP.render_map_mermaid(payload))
            print(f"  ✓ {html_path}")
            print(DIM(f"    deterministic Mermaid：{mmd_path}"))
            return
        sess.refresh_repo_context()
        payload = REPOMAP.load_map(root)
        if not payload:
            print(DIM("  没有可用 deterministic map。/map refresh 重试。"))
            return
        print(REPOMAP.format_map(
            payload,
            max_chars=policy["max_index_chars"]).rstrip())
        drift = REPOMAP.check_map(
            root, hash_check=False,
            max_files=policy["max_files"]) or {}
        state = "fresh" if drift.get("fresh") else "stale"
        print(DIM(f"  [{state} · zero provider · {REPOMAP.MAP_DIRNAME}/"
                  f"{REPOMAP.MAP_DATA}]"))
    except REPOMAP.RepoMapError as exc:
        print(RED(f"  [map] {exc}"))


def cmd_architecture(sess, rest):
    """Explicit, provider-backed architecture enrichment."""
    value = str(rest or "").strip()
    action, _, tail = value.partition(" ")
    action = action.lower() or "status"
    tail = tail.strip()
    root = sess._session_cwd
    policy = CFG.map_policy(sess.cfg)
    if action in {"help", "?"}:
        _architecture_usage()
        return
    if tail or action not in {"status", "build", "check", "viz"}:
        print(RED(f"  [architecture] 未知参数：{value}"))
        _architecture_usage()
        return
    if action == "build":
        model, gateway = sess.route_selection()
        try:
            plan = REPOMAP.architecture_plan(
                root, model=model, gateway=gateway,
                max_files=policy["max_files"])
        except REPOMAP.RepoMapError as exc:
            print(RED(f"  [architecture] 预检失败：{exc}"))
            return
        print(DIM(
            f"  architecture 预检 · {len(plan['files'])} files · "
            f"cache {plan['cache_hits']} hit / "
            f"{plan['cache_misses']} miss · "
            f"预计上限 {plan['estimated_requests']} provider requests · "
            f"{model}@{gateway}"))

        def note(msg):
            if (str(msg).startswith("摘要 ")
                    or str(msg).startswith("合成 architecture ")):
                return
            print(DIM(f"  {msg}"))

        build_id = "architecture-" + uuid.uuid4().hex
        request_sequence = 0
        watcher = ArchitectureCancelWatcher(sess)

        def complete(messages, max_tokens=1200, purpose="architecture",
                     metadata=None):
            nonlocal request_sequence
            request_sequence += 1
            request_id = f"{build_id}-{request_sequence:04d}"
            trace_builder = getattr(sess.ag, "_trace_context", None)
            if callable(trace_builder):
                trace = trace_builder(
                    request_id, build_id, purpose=purpose,
                    raw={"projection": "architecture",
                         **dict(metadata or {})})
            else:
                trace = {
                    "request_id": request_id,
                    "session_id": str(
                        getattr(sess.ag, "session_id", "")),
                    "turn_id": build_id,
                    "cwd": root,
                    "raw": {
                        "purpose": purpose,
                        "projection": "architecture",
                        **dict(metadata or {}),
                    },
                }
            trace["purpose"] = purpose
            trace["cwd"] = root
            text = []
            for ev in client.stream_chat(
                    model, messages, max_tokens=max_tokens,
                    temperature=0.2, gateway=gateway,
                    cancel=watcher.event,
                    metrics=getattr(sess.ag, "metrics", None),
                    trace_context=trace):
                if ev["t"] == "text":
                    text.append(ev["v"])
            return "".join(text)

        try:
            with watcher, ArchitectureBuildProgress(sess) as progress:
                manifest = REPOMAP.build_architecture(
                    root, complete=complete, model=model,
                    gateway=gateway, on_note=note,
                    on_progress=progress, cancel=watcher.event,
                    max_files=policy["max_files"])
        except (REPOMAP.RepoMapCancelled, client.Interrupted):
            print(YELLOW(
                "  [architecture] 已取消；已完成的摘要缓存已保留，"
                "current generation 未切换"))
            return
        except client.APIError as exc:
            print(RED(f"  [architecture] provider 失败：{exc}"))
            print(DIM(
                "  已完成摘要缓存保留；可稍后重试，current generation 未切换。"))
            return
        except REPOMAP.RepoMapError as exc:
            print(RED(f"  [architecture] {exc}"))
            print(DIM("  合成失败不丢摘要缓存；可换用结构化输出更稳定的模型"
                      "后重试。原始输出在 "
                      f"{REPOMAP.MAP_DIRNAME}/.cache/last-synthesis.txt"))
            return
        nodes = manifest.get("nodes") or []
        concepts = sum(1 for n in nodes if n["type"] == "concept")
        print(f"  ✓ {len(nodes)} 节点（{concepts} concept）· "
              f"{len(manifest.get('files') or {})} 文件 · "
              f"generation {manifest.get('generation', '?')}")
        try:
            sess.refresh_repo_context()
        except Exception:                               # noqa: BLE001
            pass
        return
    if action == "viz":
        try:
            manifest = REPOMAP.load_architecture_manifest(root)
        except REPOMAP.RepoMapError as exc:
            print(RED(f"  [architecture] {exc}"))
            return
        if not manifest:
            print(DIM("  还没有 architecture。先 /architecture build。"))
            return
        drift = REPOMAP.check_architecture(
            root, hash_check=False,
            max_files=policy["max_files"])
        html_path, mmd_path = REPOMAP.export_visualizations(
            root, kind="architecture",
            html=REPOMAP.render_viz_html(manifest, drift),
            mermaid=REPOMAP.render_mermaid(manifest))
        print(f"  ✓ {html_path}")
        print(DIM("    交互视图（拖拽/缩放/搜索/点选亮边；入边 teal=谁依赖我，"
                  "出边 amber=我依赖谁；stale 呼吸红环）。"
                  "零外链，浏览器或 Cursor 直接打开。"))
        print(DIM(f"    mermaid 版：{mmd_path}（markdown 预览可渲染）"))
        return
    if action == "check":
        try:
            drift = REPOMAP.check_architecture(
                root, max_files=policy["max_files"])
        except REPOMAP.RepoMapError as exc:
            print(RED(f"  [architecture] {exc}"))
            return
        if drift is None:
            print(DIM("  还没有 architecture。/architecture build 生成。"))
            return
        if drift["fresh"]:
            print(DIM("  ✓ architecture 与工作树一致"))
            return
        for label, key in (("改动", "changed"), ("删除", "removed"),
                           ("新增", "added")):
            for rel in drift[key]:
                print(DIM(f"  {label}  {rel}"))
        if drift["stale_nodes"]:
            print(YELLOW("  陈旧节点: " + " ".join(drift["stale_nodes"])))
        print(DIM(
            "  /architecture build 重建（会调模型；只重摘要改过的文件）"))
        return
    try:
        manifest = REPOMAP.load_architecture_manifest(root)
    except REPOMAP.RepoMapError as exc:
        print(RED(f"  [architecture] {exc}"))
        return
    if not manifest:
        print(DIM(f"  还没有 architecture。/architecture build 会把仓库蒸馏成 "
                  f"{REPOMAP.MAP_DIRNAME}/ 下的互链节点，"
                  "此后每个会话自动注入索引。"))
        return
    nodes = manifest.get("nodes") or []
    drift = REPOMAP.check_architecture(
        root, hash_check=False,
        max_files=policy["max_files"]) or {}
    state = "✓ fresh" if drift.get("fresh") else "⚠ stale"
    print(f"  {len(nodes)} 节点 · {len(manifest.get('files') or {})} 文件 · "
          f"{state} · 构建于 {manifest.get('built_at', '?')} · "
          f"{manifest.get('model', '?')} · "
          f"generation {manifest.get('generation', '?')}")
    for n in nodes[:12]:
        print(DIM(f"    [[{n['slug']}]] ({n['type']})"))
    if len(nodes) > 12:
        print(DIM(f"    … 另 {len(nodes) - 12} 个"))


def cmd_memory(sess, rest):
    """Inspect and control the source-bound local memory layer."""
    value = str(rest or "").strip()
    action, _, tail = value.partition(" ")
    action = action.lower() or "status"
    tail = tail.strip()
    try:
        if action in {"help", "?"}:
            _memory_usage()
            return
        if action == "status":
            index = getattr(sess, "memory_index", {}) or {}
            print(f"  {BOLD('Memory')} · source-bound local derivation")
            print(
                f"    use={str(bool(sess.memory_use)).lower()} · "
                f"generate={str(bool(sess.memory_generate)).lower()} · "
                f"injected={len(index.get('entries') or [])}/"
                f"{int(index.get('available') or 0)} · "
                f"{int(index.get('chars') or 0):,} chars")
            print(f"    global  {sess.memory_store.root / 'global.json'}")
            identity = MEMORY.project_identity(sess._session_cwd)
            print(
                f"    project {identity['key']} · "
                f"{identity['kind']} {identity['root']}")
            if getattr(sess, "_memory_error", None):
                print(RED(f"    error   {sess._memory_error}"))
            print(DIM(
                "    索引里每条只有一行钩子；正文由模型用 memory_read 按需取。"
                "/memory help 查看命令。"))
            return
        if action == "list":
            rows = sess.memory_store.list(cwd=sess._session_cwd)
            if not rows:
                print(DIM("  (global 与当前 project 都没有 memory)"))
                return
            layout = _table_layout((
                tui.TableColumn(15, min_width=14),
                tui.TableColumn(9, min_width=7),
                tui.TableColumn(10, min_width=8),
                tui.TableColumn(60, min_width=20),
            ), indent=2, maximum=106)
            print(DIM(layout.row(("id", "scope", "type", "title / 钩子"))))
            for row in rows:
                hook = str(row.get("description") or "").strip()
                title = row.get("title") or "?"
                print(layout.row((
                    row["id"], row["scope"],
                    row.get("type") or MEMORY.DEFAULT_TYPE,
                    f"{title} — {hook}" if hook else title), wrap_last=True))
            return
        if action == "show":
            if not tail or " " in tail:
                raise ValueError("用法：/memory show <id>")
            row = sess.memory_store.get(tail, cwd=sess._session_cwd)
            print(
                f"  {BOLD(row['id'])} · {row['scope']} · "
                f"{row.get('type') or MEMORY.DEFAULT_TYPE} · "
                f"{row.get('kind') or '?'}")
            print(f"  {row.get('title') or '?'}")
            print(DIM(
                f"  source={row.get('source_session') or 'explicit'} · "
                f"updated={row.get('updated_at') or '?'} · "
                f"evidence={json.dumps(row.get('evidence') or {}, ensure_ascii=False)}"))
            print(str(row.get("content") or ""))
            return
        if action == "remember":
            scope = MEMORY.PROJECT
            first, _, body = tail.partition(" ")
            if first.lower() in MEMORY.SCOPES:
                scope, tail = first.lower(), body.strip()
            if not tail:
                raise ValueError(
                    "用法：/memory remember [project|global] <text>")
            row = sess.memory_store.add(
                tail, scope=scope, cwd=sess._session_cwd,
                source_session=sess.ag.session_id)
            if sess.memory_use:
                sess.refresh_memory_context()
            print(DIM(
                f"  [已记住 {row['id']} · {row['scope']} · "
                "内容已做本地 secret redaction]"))
            return
        if action == "forget":
            if not tail or " " in tail:
                raise ValueError("用法：/memory forget <id>")
            row = sess.memory_store.remove(tail, cwd=sess._session_cwd)
            if sess.memory_use:
                sess.refresh_memory_context()
            print(DIM(f"  [已删除 memory {row['id']}]"))
            return
        if action in {"use", "generate"}:
            choice = tail.lower() or "status"
            if choice not in {"on", "off", "status"}:
                raise ValueError(
                    f"用法：/memory {action} on|off|status")
            if choice != "status":
                sess.set_memory_mode(action, choice == "on")
            print(
                f"  memory {action} = "
                f"{str(bool(getattr(sess, 'memory_' + action))).lower()}")
            return
        if action == "capture":
            result, saved = sess.extract_memories_now()
            if saved:
                for row in saved:
                    print(DIM(
                        f"  [{row['id']} · {row.get('type')} · "
                        f"{row.get('title')}] "
                        + str(row.get("description") or "")))
                print(DIM(
                    f"  [记下 {len(saved)} 条 · {result.get('model')}@"
                    f"{result.get('gateway')} · {result.get('seconds')}s · "
                    "/memory forget <id> 撤销]"))
            elif result.get("kind") == "aborted":
                print(DIM("  (已取消)"))
            elif result.get("kind") == "no-turn":
                print(DIM("  (这次会话还没有值得记的东西)"))
            else:
                print(DIM(
                    f"  (没抽出可用条目：{result.get('text') or result.get('kind')})"))
            return
        if action == "refresh":
            index = sess.refresh_memory_context()
            print(DIM(
                f"  [memory index 已刷新：{len(index['entries'])}/"
                f"{index['available']} entries · {index['chars']:,} chars]"))
            return
        if action == "capsule":
            capsule = sess.build_context_capsule()
            print(
                f"  Context Capsule v{capsule['version']} · "
                f"{capsule['chars']:,} chars · "
                f"sha256 {capsule['sha256']}")
            print(DIM(
                f"  source={capsule['source_session']} · "
                f"raw={capsule['source_raw_sha256'][:16]} · "
                f"memory={len(capsule['memory']['entries'])} · "
                f"plan={len(capsule['task_plan'])}"))
            return
        raise ValueError(f"未知 memory 子命令：{action}")
    except (MEMORY.MemoryError, ValueError) as exc:
        print(RED(f"  [memory] {exc}"))


def _model_usage():
    print(DIM(
        "  /model                    打开唯一模型选择列表（含各网关 key 状态）\n"
        "  /model <名字>             切换模型；只在一个网关有就自动选网关\n"
        "  /model <名字>@<网关>       指定路由（两边都有的模型）\n"
        "  /model gateway [名字]     切换/查看网关（原 /gateway）\n"
        "  /model health [关键词]    查看持久健康度与 circuit\n"
        "  /model refresh            刷新当前 gateway 目录后重开选择器\n"
        "  /model check              目录 + 实调双证据校验（当前模型与各席位）"))


def cmd_model(sess, rest):
    value = str(rest or "").strip()
    action, _, tail = value.partition(" ")
    action = action.lower()
    tail = tail.strip()
    if action in {"help", "?"}:
        _model_usage()
        return
    if action == "health":
        _show_model_health(tail)
        return
    if action == "gateway":
        # /gateway 已并入 /model（2026-09-04）：切换/查看网关走这里
        cmd_gateway(sess, tail, quiet=True)
        return
    if action == "refresh":
        if tail:
            print(RED("  /model refresh 不接受额外参数"))
            return
        _, gateway = sess.route_selection()
        with Spinner("抓取模型目录"):
            result = M.refresh_catalog(gateway)
        print(DIM(f"  [{gateway} 已刷新，{result['count']} 个模型]"))
        notice = _catalog_change_notice(result)
        if notice:
            print(notice.rstrip("\r\n"))
        if tui.supported():
            cmd_model(sess, "")
        return
    if action == "check":
        _cmd_model_check(sess, tail)
        return
    if value:
        # <模型>@<网关>：两边都有的模型（glm-5.3 / kimi-k3 / deepseek）用这个指定路由
        explicit_gateway = None
        if "@" in value:
            value, _, explicit_gateway = value.partition("@")
            value = value.strip()
            explicit_gateway = explicit_gateway.strip().lower()
            if explicit_gateway not in client.GATEWAYS:
                print(RED(
                    f"  [model] 未知网关 {explicit_gateway!r}；"
                    f"可选：{', '.join(client.GATEWAYS)}"))
                return
        _, current_gateway = sess.route_selection()
        target_gateway = explicit_gateway or current_gateway
        gws = M.where(value)
        if explicit_gateway is not None:
            if gws and explicit_gateway not in gws:
                print(YELLOW(
                    f"  [{explicit_gateway} 的目录里没有 {value}；"
                    "仍按你指定的路由切换]"))
        elif gws and current_gateway not in gws:
            target_gateway, _ = M.resolve(value)
            print(DIM(f"  [自动选择网关 {target_gateway}]"))
        elif len(gws) > 1:
            print(DIM(
                f"  [{value} 两边都有，用已选择的 {current_gateway}；"
                f"要换边用 /model {value}@<网关>]"))
        rec = M.get(target_gateway, value)
        if rec.get("supports_tools") is None:
            print(DIM(f"  [{value} 未经实测，可能调不通。"
                      f"/probe {value} 可先验证]"))
        elif rec.get("status") == "error":
            print(YELLOW(f"  [{value} 上次实测失败："
                         f"{str(rec.get('error'))[:60]}]"))
        try:
            result = sess.request_route_change(value, target_gateway)
        except ValueError as exc:
            print(RED(f"  [model] {exc}"))
            return
        lim = result.get("context")
        ctx = (
            f" · 上下文 {lim/1000:.0f}k"
            if lim and result.get("context_known") else "")
        chat_only = rec.get("supports_tools") is False
        mode = " · 仅聊天（无工具）" if chat_only else ""
        prefix = "下一轮切到" if result["staged"] else "切到"
        notice = f"  [{prefix} {value}@{target_gateway}{ctx}{mode}]"
        print(YELLOW(notice) if chat_only else DIM(notice))
    elif tui.supported():
        # /model 是高频工作集，不是 400+ 条原始目录：DeepInfer 保留全部
        # 实测可用模型（无工具项灰显为仅聊天），Boyue 只保留当前旗舰；
        # 失败/未知能力仍不冒充可用项。
        rows = M.default_picker_rows()
        if not rows:
            print(YELLOW("  还没有实测可用的模型。先跑 "
                         "zylab --probe-all"))
            return
        # 先列 DeepInfer 的完整工作集，再列 Boyue 旗舰；各组内按家族扫读。
        fam_order = {
            "OpenAI": 0, "Anthropic": 1, "deepseek": 2, "moonshot": 3,
            "智谱": 4, "阿里": 5, "intern": 6, "MiniMax": 7, "other": 8,
        }
        gateway_order = {"deepinfer": 0, "boyue": 1}
        rows.sort(key=lambda r: (
            gateway_order.get(r.get("gateway"), 9),
            fam_order.get(r.get("family"), 9),
            r.get("id", "").lower()))

        model_layout = _table_layout((
            tui.TableColumn(9, min_width=6),
            tui.TableColumn(32, min_width=18),
            tui.TableColumn(
                9, min_width=8, priority=3, optional=True),
            tui.TableColumn(
                8, min_width=6, align="right", priority=4, optional=True),
            tui.TableColumn(
                7, min_width=5, align="right", priority=5, optional=True),
            tui.TableColumn(8, min_width=6),
        ), indent=2, maximum=110, gap=" ")

        def render(r):
            n = r.get("context")
            src = str(r.get("context_source") or "")
            if not n:
                ctx = ""
            else:
                # 来源可信度必须看得见：同一个模型在不同网关会被砍到不同
                # 大小（glm-5.2 厂商文档写 1M，DeepInfer 实际只给 256k）。
                #   *  实测：真在这个网关上试出来的
                #   空  网关自报 max_model_len，同样可信
                #   ~  规格推断（HF 原生 / 厂商文档 / 同模型别名），
                #      是能力上限，网关可能砍到更小
                mark = "*" if src == "probed" else ("~" if src else "")
                ctx = f"{n/1000:.0f}k{mark}"
            lat = (
                f"{r['first_token_s']:.1f}s"
                if r.get("first_token_s") else "未测")
            chat_only = r.get("supports_tools") is False
            line = model_layout.row((
                r.get("family", "?"),
                r["id"],
                r["gateway"],
                ctx,
                lat,
                "仅聊天" if chat_only else "",
            ))
            return DIM(line) if chat_only else line

        # 网关 key 状态原来只在 /gateway 列表里看得到；并入后放在选择器上方
        print(DIM("  网关 · " + " · ".join(
            f"{name} {_gateway_key_label(name)}" for name in client.GATEWAYS)))
        deepinfer_count = sum(
            row.get("gateway") == "deepinfer" for row in rows)
        boyue_count = len(rows) - deepinfer_count
        chat_only_count = sum(
            row.get("supports_tools") is False for row in rows)
        code_count = len(rows) - chat_only_count
        pick = sess.pick(
            rows, page=14, render=render,
            title=(
                f"选择模型（{len(rows)} 个：Code {code_count} · "
                f"仅聊天 {chat_only_count}；DeepInfer {deepinfer_count} · "
                f"Boyue 旗舰 {boyue_count}/{len(M.BOYUE_DEFAULT_MODELS)}）"
                f"    上下文：* 实测 · ~ 规格推断 · 无标记 网关自报"))
        if pick:
            gw = pick["gateway"]
            try:
                result = sess.request_route_change(pick["id"], gw)
            except ValueError as exc:
                print(RED(f"  [model] {exc}"))
                return
            lim = result.get("context")
            ctx = (
                f" · 上下文 {lim/1000:.0f}k"
                if lim and result.get("context_known") else "")
            chat_only = pick.get("supports_tools") is False
            mode = " · 仅聊天（无工具）" if chat_only else ""
            prefix = "下一轮切到" if result["staged"] else "切到"
            notice = f"  [{prefix} {pick['id']}@{gw}{ctx}{mode}]"
            print(YELLOW(notice) if chat_only else DIM(notice))
        else:
            print(DIM("  (取消)"))
    else:
        print(DIM(f"  当前 {sess.ag.model} · 上下文 {sess.ag.ctx_limit/1000:.0f}k"))


def _gateway_key_label(name):
    """网关的 key 状态标注（只报来源，永不报 key 本体）。BACKLOG-rename-zylab §2.6：
    只配一个网关也能用，列表要让人一眼看出哪个没配。"""
    try:
        status = client.key_status(client.route_for(name))
    except Exception:                                   # noqa: BLE001
        return ""
    if status.get("found"):
        return GREEN("key 已配置")
    return YELLOW("未配置 key")


def _after_gateway_switch(sess, name):
    """切换仍允许——也许用户接下来就要 export——但必须当场明示后果，而不是等发请求时 401/503。"""
    _warn_if_no_key(name)
    # 新网关的目录可能从来没抓过。这里只把节流清零，让**下一个 turn** 去判断：
    # 切换常常是 staged 的（下一轮才生效），此刻 route_selection 还是旧网关。
    try:
        sess._catalog_checked_at = 0.0
    except Exception:                                 # noqa: BLE001
        pass


def cmd_gateway(sess, rest, quiet=False):
    if not quiet:
        print(DIM("  [/gateway 已并入 /model：/model gateway [名字]，"
                  "或 /model <模型>@<网关>]"))
    if rest:
        try:
            model, _ = sess.route_selection()
            result = sess.request_route_change(model, rest.lower())
            prefix = "下一轮切到" if result["staged"] else "切到"
            print(DIM(
                f"  [{prefix} {result['gateway']} · {result['base']}]"))
            _after_gateway_switch(sess, result["gateway"])
        except ValueError as e:
            print(RED(f"  {e}"))
    elif tui.supported():
        rows = [{"id": g, **cfg} for g, cfg in client.GATEWAYS.items()]
        pick = sess.pick(
            rows, title="选择网关",
            render=lambda r: (
                f"{tui.pad_display(r['id'], 12)}{r['note']}  {_gateway_key_label(r['id'])}"),
            page=6, allow_filter=False)
        if pick:
            model, _ = sess.route_selection()
            result = sess.request_route_change(model, pick["id"])
            prefix = "下一轮切到" if result["staged"] else "切到"
            print(DIM(f"  [{prefix} {pick['id']}]"))
            _after_gateway_switch(sess, result["gateway"])
    else:
        _, selected_gateway = sess.route_selection()
        for g, cfg in client.GATEWAYS.items():
            cur = GREEN(" ←当前") if g == client.GATEWAY else ""
            if g == selected_gateway and g != client.GATEWAY:
                cur += BLUE(" ←下一轮")
            print(f"  {BOLD(g):20} {DIM(cfg['note'])}  {_gateway_key_label(g)}{cur}")


def _probe_usage():
    print(DIM(
        "  模型探测会发送真实 API 请求；status 只读缓存。\n"
        "  /probe                              打开交互菜单\n"
        "  /probe <model>                      当前 gateway 的指定模型\n"
        "  /probe <gateway>                    该 gateway 的当前模型\n"
        "  /probe <model>@<gateway>            明确指定路由\n"
        "  /probe <gateway> <model>            同上\n"
        "  /probe status [target]              查看缓存，不发请求\n"
        "  /probe context [target]             逐档探顶（多次大请求，有进度）\n"
        "                                      明确过长拒绝才算触顶\n"
        "  批量探测：zylab --probe-all [deepinfer|boyue|both]"))


def _probe_target_row(record):
    status = record.get("status")
    tools_ok = record.get("supports_tools")
    if status == "error":
        capability = RED("不可用")
    elif status == "ok" and tools_ok is True:
        capability = GREEN("工具")
    elif status == "ok" and tools_ok is False:
        capability = YELLOW("无工具")
    else:
        capability = DIM("未测")
    context = record.get("context")
    context_text = f"{int(context) / 1000:.0f}k" if context else "?k"
    return (
        f"{tui.pad_display(str(record.get('id') or '?'), 44)}"
        f"@{tui.pad_display(str(record.get('gateway') or '?'), 10)}"
        f"{tui.pad_display(context_text, 7, align='right')}  {capability}")


def _pick_probe_target(sess):
    """从本地能力表选择 model@gateway；不为打开 picker 发网络请求。"""
    rows = list(M.probed_rows())
    current_gateway = str(getattr(sess.ag, "gateway", "") or client.GATEWAY)
    current_model = str(getattr(sess.ag, "model", "") or "")
    if not any(
            row.get("gateway") == current_gateway
            and row.get("id") == current_model for row in rows):
        rows.insert(0, M.get(current_gateway, current_model))
    return sess.pick(
        rows,
        title="选择要探测的 model@gateway · 本页只读缓存 · Esc 返回",
        page=14, allow_filter=True, render=_probe_target_row)


def _pick_probe_request(sess):
    """裸 /probe 的 cost-explicit 交互入口。"""
    gateway = str(getattr(sess.ag, "gateway", "") or client.GATEWAY)
    model = str(getattr(sess.ag, "model", "") or "")
    target = f"{model}@{gateway}"
    choices = [
        {
            "mode": "capability", "target": "current",
            "label": f"探测当前 {target}",
            "cost": "真实请求 1+ 次（推荐）",
        },
        {
            "mode": "select", "target": "picker",
            "label": "选择其他 model@gateway 后探测",
            "cost": "选择阶段 0 请求；确认后 1+ 次",
        },
        {
            "mode": "status", "target": "current",
            "label": f"查看当前 {target} 的缓存状态",
            "cost": "0 请求",
        },
        {
            "mode": "context", "target": "current",
            "label": f"实测当前 {target} 的上下文上限",
            "cost": "最多 9 档 / 约 7.2M 目标输入；重试另计",
        },
        {
            "mode": "help", "target": "none",
            "label": "查看 /probe 完整用法",
            "cost": "0 请求",
        },
    ]
    picked = sess.pick(
        choices, title="模型探测 · Enter 选择 · Esc 取消",
        page=len(choices), allow_filter=False,
        render=lambda row: (
            f"{tui.pad_display(row['label'], 58)} {DIM(row['cost'])}"))
    if picked is None:
        return None
    if picked["mode"] == "select":
        selected = _pick_probe_target(sess)
        if selected is None:
            return None
        return (
            "capability", str(selected.get("gateway") or gateway),
            str(selected.get("id") or model))
    if picked["mode"] == "help":
        return "help", None, None
    return picked["mode"], gateway, model


def _parse_probe_request(sess, rest):
    """返回 (mode, gateway, model)；模型里的普通 '/' 不当成路由。"""
    parts = str(rest or "").split()
    mode = "capability"
    if parts and parts[0].lower() in {"help", "status", "context"}:
        mode = parts.pop(0).lower()
    if mode == "help":
        if parts:
            raise ValueError("/probe help 不接受其他参数")
        return mode, None, None
    if parts and parts[0].lower() == "all":
        raise ValueError(
            "批量探测必须显式使用 zylab --probe-all，"
            "避免在交互窗口误触发很多请求")

    gateway = str(
        getattr(getattr(sess, "ag", None), "gateway", "")
        or client.GATEWAY)
    model = str(getattr(getattr(sess, "ag", None), "model", "") or "")
    gateways = set(client.GATEWAYS)

    if len(parts) == 1:
        target = parts[0]
        if target.lower() in gateways:
            gateway = target.lower()
        elif "@" in target:
            target, marker, candidate = target.rpartition("@")
            candidate = candidate.lower()
            if not marker or candidate not in gateways:
                raise ValueError(
                    "@ 后必须是已知 gateway："
                    + " / ".join(sorted(gateways)))
            gateway, model = candidate, target
        else:
            prefix, marker, suffix = target.partition("/")
            normalized = prefix.lower()
            if marker and normalized in gateways:
                gateway, model = normalized, suffix
            else:
                model = target
    elif len(parts) == 2:
        candidate = parts[0].lower()
        if candidate not in gateways:
            raise ValueError(
                "两个参数时顺序是 /probe <gateway> <model>；"
                "也可写 <model>@<gateway>")
        gateway, model = candidate, parts[1]
    elif len(parts) > 2:
        raise ValueError("参数过多；使用 /probe help 查看格式")

    if gateway not in gateways:
        raise ValueError(
            f"未知 gateway {gateway!r}；可选："
            + " / ".join(sorted(gateways)))
    if not model:
        raise ValueError("没有可探测的模型；请用 /probe <model>")
    return mode, gateway, model


def _probe_seconds(value):
    try:
        return f"{float(value):.3f}s"
    except (TypeError, ValueError):
        return "未知"


def _render_probe_record(record):
    gateway = str(record.get("gateway") or "?")
    model = str(record.get("id") or "?")
    status = record.get("status")
    supports_tools = record.get("supports_tools")
    if status == "ok" and supports_tools is True:
        verdict = GREEN("可用于 Code CLI")
    elif status == "ok" and supports_tools is False:
        verdict = YELLOW("仅聊天可用；没有产生 tool_call")
    elif status == "error":
        verdict = RED("不可用")
    else:
        verdict = DIM("尚未实测")

    print(f"\n  {BOLD(model)}@{gateway}  {verdict}")
    tools_text = {
        True: "支持", False: "不支持", None: "未知",
    }.get(supports_tools, "未知")
    temperature = record.get("supports_temperature")
    temperature_text = {
        True: "支持", False: "不支持", None: "未知",
    }.get(temperature, "未知")
    print(f"    工具调用     {tools_text} · temperature {temperature_text}")

    timings = []
    if record.get("first_token_s") is not None:
        label = (
            "首响应" if record.get("probe_timing_version") == 2
            else "旧版整段耗时")
        timings.append(label + " " + _probe_seconds(record["first_token_s"]))
    if record.get("response_s") is not None:
        timings.append("完整响应 " + _probe_seconds(record["response_s"]))
    if record.get("probe_total_s") is not None:
        timings.append("探测总计 " + _probe_seconds(record["probe_total_s"]))
    if record.get("probe_attempts") is not None:
        timings.append(f"{int(record['probe_attempts'])} attempt(s)")
    if timings:
        print("    延迟         " + " · ".join(timings))

    context = record.get("context")
    if context:
        source = record.get("context_source") or "cached"
        upper = record.get("context_upper_bound")
        probe_state = record.get("context_probe_state")
        estimated = "约 " if record.get("context_probe_lower_estimated") else ""
        if probe_state == "ceiling":
            print(
                f"    上下文       ≥ {estimated}{int(context):,} tokens · "
                f"{source} · 模型顶未知")
        else:
            bound = f" · 拒绝边界 {int(upper):,}" if upper else ""
            print(
                f"    上下文       {estimated}{int(context):,} tokens"
                f"{bound} · {source}")
    context_state = record.get("context_probe_state")
    if context_state:
        state_text = {
            "bounded": "已找到拒绝边界",
            "ceiling": "到达可测上限，模型顶未知",
            "blocked": "受阻，未得到模型顶",
        }.get(context_state, str(context_state))
        counters = []
        if record.get("context_probe_steps") is not None:
            counters.append(f"{int(record['context_probe_steps'])} 档")
        if record.get("context_probe_requests") is not None:
            counters.append(f"{int(record['context_probe_requests'])} req")
        suffix = " · " + " · ".join(counters) if counters else ""
        print(f"    Context probe {state_text}{suffix}")
        if context_state in {"ceiling", "blocked"} and record.get(
                "context_probe_error"):
            print(YELLOW(
                "    探测说明     " + str(record["context_probe_error"])))
    if record.get("temperature_retried"):
        print(DIM("    兼容处理     temperature 被拒后已无参数重试"))

    if status == "error":
        tags = []
        if record.get("http_status") is not None:
            tags.append(f"HTTP {record['http_status']}")
        if record.get("error_code"):
            tags.append(str(record["error_code"]))
        if record.get("error_kind"):
            tags.append(str(record["error_kind"]))
        if record.get("provider_request_id"):
            tags.append("request " + str(record["provider_request_id"]))
        if record.get("retryable") is not None:
            tags.append(
                "可重试" if record["retryable"] else "不可重试")
        if tags:
            print("    错误分类     " + " · ".join(tags))
        print(RED("    原因         " + str(record.get("error") or "未知错误")))
    if record.get("probed"):
        print(DIM("    缓存时间     " + str(record["probed"])))
    try:
        health = store.metrics_facade().get_health(gateway, model)
        freshness = HEALTH.probe_decision(
            health,
            catalog_fingerprint=record.get("catalog_fingerprint"),
            now=time.time())
        route = HEALTH.route_decision(
            health, explicit=False, now=time.time())
    except Exception as exc:  # noqa: BLE001 - cached status stays available
        print(YELLOW(
            f"    Health DB    不可用：{type(exc).__name__}: {exc}"))
    else:
        if health:
            success = int(health.get("success_count") or 0)
            failure = int(health.get("failure_count") or 0)
            total = success + failure
            rate = f"{success}/{total}" if total else "?"
            probe_text = (
                f"due ({freshness.reason})" if freshness.due
                else f"fresh ({freshness.reason})")
            circuit = "open" if route.circuit_open else "closed"
            print(
                "    Health       "
                f"probe {probe_text} · success {rate} · circuit {circuit} · "
                f"TTFT {_fmt_ms(health.get('true_ttft_ms'))} · "
                f"total {_fmt_ms(health.get('total_latency_ms'))}")
        else:
            print(DIM(
                f"    Health       probe due ({freshness.reason}) · "
                "request history unknown"))


def cmd_probe(sess, rest):
    if not str(rest or "").strip() and getattr(sess, "pump", None) is not None:
        picked = _pick_probe_request(sess)
        if picked is None:
            print(DIM("  [已取消 probe · 0 请求]"))
            return
        mode, gateway, model = picked
    else:
        try:
            mode, gateway, model = _parse_probe_request(sess, rest)
        except ValueError as exc:
            print(RED(f"  {exc}"))
            print(DIM("  使用 /probe help 查看格式"))
            return

    if mode == "help":
        _probe_usage()
        return

    target = f"{model}@{gateway}"
    if mode == "status":
        print(DIM("  [只读本地缓存，没有发送 API 请求]"))
        _render_probe_record(M.get(gateway, model))
        return

    if mode == "context":
        print(YELLOW(
            f"  实测 {target} 的上下文上限；会逐档发送大请求，"
            "直到明确过长拒绝或达到网关可测上限；"
            "最多 9 档、约 7.2M 目标输入 tokens，网络重试另计；"
            "不会切换当前模型。"))
        try:
            with ContextProbeProgress(sess) as progress:
                lower, upper = M.probe_context(
                    gateway, model, on_progress=progress)
        except Exception as exc:                       # noqa: BLE001
            print(RED(f"  上下文探测失败：{type(exc).__name__}: {exc}"))
            return
        record = M.get(gateway, model)
        probe_state = record.get("context_probe_state")
        if probe_state == "bounded" and upper:
            rejected = record.get("context_probe_rejected_at") or upper
            if lower:
                print(GREEN(
                    f"  已触顶：实测可用下界 {lower:,} tokens；"
                    f"{int(rejected):,} 档明确拒绝（边界 {upper:,}）。"))
            else:
                print(RED(
                    f"  已触及拒绝边界：连 {int(rejected):,} tokens 档都不收。"))
        elif probe_state == "ceiling":
            print(YELLOW(
                f"  已完成全部可测档位：至少可用 {int(lower or 0):,} tokens；"
                "模型始终接受，但受 request-body 安全上限约束，真实模型顶未确认。"))
        elif probe_state == "blocked":
            print(RED(
                "  网络/网关状态不足以判定模型顶，未写入上限结论："
                + str(record.get("context_probe_error") or "未知错误")))
        elif lower:
            # 兼容旧缓存或测试替身；新实现不会把 upper=None 称为已触顶。
            bound = f"；{upper:,} 被拒" if upper else "；模型顶未确认"
            print(YELLOW(f"  实测可用下界 {lower:,} tokens{bound}"))
        elif record.get("context_probe_error"):
            print(RED(
                "  网络/服务状态不足以判定，未写入上下文结论："
                + str(record["context_probe_error"])))
        elif upper:
            print(RED(f"  连最小探测档 {upper:,} tokens 都被拒"))
        else:
            print(RED("  无法得到可重复的上下文结论，缓存未修改"))

        if lower and probe_state in {"bounded", "ceiling", None}:
            if (gateway == getattr(sess.ag, "gateway", None)
                    and model == getattr(sess.ag, "model", None)):
                sess.ag.set_model(model)
        _render_probe_record(record)
        return

    print(DIM(
        f"  探测 {target} · 会发送至少 1 次真实 API 请求；"
        "失败或参数兼容时可能重试。"))
    with Spinner("能力探测中"):
        record = M.probe(gateway, model)
    if (gateway == getattr(sess.ag, "gateway", None)
            and model == getattr(sess.ag, "model", None)):
        sess.ag.set_model(model)
    _render_probe_record(record)
    print(DIM("  [只更新能力缓存，不切换当前 model@gateway]"))


def cmd_tools(sess, rest):
    for t in tools.SCHEMA:
        f = t["function"]
        permission = sess.cfg.get("permissions", {}).get(f["name"], "ask")
        tag = {
            "allow": GREEN("allow"),
            "ask": YELLOW("ask"),
            "deny": RED("deny"),
        }.get(permission, permission)
        head = f"  {tui.pad_display(BOLD(f['name']), 22)} {tag}  "
        room = max(20, tui._cols() - 1 - tui.display_width(head))
        parts = tui.wrap_display(str(f.get("description") or ""), room)
        print(head + DIM(parts[0]))
        for part in parts[1:]:
            print(" " * tui.display_width(head) + DIM(part))
    print(DIM("  /permissions 可查看来源并修改用户级规则"))


def cmd_agents(sess, rest):
    """列出、预览、attach 或给当前 parent 的 child agent 发消息。"""
    parts = str(rest or "").strip().split(maxsplit=2)
    action = parts[0].lower() if parts else ""
    try:
        if not action:
            if sess.pump is not None:
                record = open_agents_panel(sess)
                if record is not None:
                    print(DIM(
                        f"  [已 attach {record['id']}；"
                        "Esc 或行首 Left 返回 main]"))
            else:
                print_agents(sess)
            return
        if action == "list":
            print_agents(sess)
            return
        if action == "workflow":
            # workflow 的观测与控制并入 /agents（批 2）；/workflow 同名子命令仍可用
            sub = " ".join(parts[1:]).strip() or "status"
            head = sub.split(" ", 1)[0].lower()
            if head not in {"status", "console", "open", "list",
                            "pause", "resume", "cancel"}:
                print(DIM(
                    "  用法：/agents workflow "
                    "[status|console|open|list|pause|resume|cancel] [id]"))
                return
            cmd_workflow(sess, sub)
            return
        if action == "detach":
            run_id = sess.detach_agent()
            if run_id:
                print(DIM(f"  [已 detach {run_id}]"))
            else:
                print(DIM("  [当前没有 attached agent]"))
            return
        if action in {"peek", "attach"}:
            if len(parts) < 2:
                print(DIM(f"  用法：/agents {action} <id>"))
                return
            if action == "peek":
                show_agent_preview(sess, parts[1])
            else:
                record = sess.attach_agent(parts[1])
                print(DIM(
                    f"  [已 attach {record['id']}；"
                    "Esc 或行首 Left 返回 main]"))
            return
        if action == "send":
            if len(parts) < 3 or not parts[2].strip():
                print(DIM("  用法：/agents send <id> <message>"))
                return
            item = sess.send_agent(parts[1], parts[2])
            print(DIM(
                f"  [已发送到 {parts[1]} inbox · {item.get('id')}]"))
            return
        print(DIM(
            "  用法：/agents [list|peek <id>|attach <id>|"
            "detach|send <id> <message>|workflow <sub> [id]]"))
    except (AGENTS.AgentRuntimeError, ValueError) as exc:
        print(RED(f"  [agents] {exc}"))


def _consult_panel_row(row):
    source = row.get("source_snapshot") or {}
    state = str(row.get("state") or "?")
    mark = _AGENT_STATE_MARK.get(state, "?")
    run_id = str(row.get("id") or "?")
    title = " ".join(str(source.get("title") or "(无标题)").split())
    source_id = str(source.get("session_id") or "?")
    route = f"{row.get('model') or '?'}@{row.get('gateway') or '?'}"
    pending = int(row.get("pending") or 0)
    suffix = f" · {pending} pending" if pending else ""
    if row.get("workspace_active"):
        suffix += " · active"
    lease_state = str(source.get("source_lease_state") or "inactive")
    if lease_state == "live":
        suffix += " · live snapshot"
    elif lease_state == "unknown":
        suffix += " · freshness unknown"
    return (
        f"{mark} {tui.pad_display(state, 9)} "
        f"{tui.pad_display(run_id, 14)} "
        f"{tui.pad_display(title, 30)} "
        f"{tui.pad_display(route, 28)} · "
        f"{tui.truncate_display(source_id, 12)} · "
        f"{fmt_age(row.get('updated_at') or '')}{suffix}")


def _print_consult(record):
    source = record.get("source_snapshot") or {}
    run_id = str(record.get("id") or "?")
    state = str(record.get("state") or "?")
    source_id = str(source.get("session_id") or "?")
    title = str(source.get("title") or "(无标题)")
    route = f"{record.get('model') or '?'}@{record.get('gateway') or '?'}"
    snapshot = str(source.get("snapshot_sha256") or "?")
    print()
    print(f"  {BOLD(run_id)} · {state} · {route}")
    print(f"  来源  {title} · {source_id}")
    print(
        f"  快照  {snapshot} · "
        f"{int(source.get('message_count') or 0)} messages · "
        f"saved {source.get('source_updated') or '?'}")
    if source.get("source_live"):
        print(YELLOW(
            "  注意  捕获时来源 chat 正在运行；这是最后已保存快照，"
            "可能不含尚未落盘的新消息。"))
    elif source.get("source_lease_state") == "unknown":
        print(YELLOW(
            "  注意  无法核验来源 chat 的 lease；快照内容一致，"
            "但无法确认捕获时是否仍有尚未落盘的新消息。"))
    if record.get("error"):
        print(RED(f"  错误  {record.get('error')}"))
    if record.get("result"):
        print(f"\n  {BOLD('Answer')}")
        print(AGENTS.project_report(record.get("result")))
    print(DIM(
        "  /consult open [id] 看完整副本 · "
        "/consult followup <id> <question> · /consult cancel [id]"))


def _consult_usage():
    print(DIM(
        "  /consult                              选择 Chat B 并输入问题\n"
        "  /consult <session-id> <question>       启动只读快照咨询\n"
        "  /consult list                         当前 Chat A 的咨询线程\n"
        "  /consult status [cs-id]               查看状态与快照身份\n"
        "  /consult open [cs-id]                 查看独立咨询 transcript\n"
        "  /consult followup <cs-id> <question>  继续同一咨询\n"
        "  /consult cancel [cs-id]               取消本 workspace 的咨询"))


def _pick_consult_target(sess):
    while True:
        rows = [
            row for row in store.list_session_summaries(
                limit=None, status=None, scope="all")
            if row.get("id") != sess.ag.session_id
        ]
        if not rows:
            _session_notice(sess, "没有其他已保存会话可供 consult")
            return None
        action, picked = sess.pick(
            rows,
            title="Consult saved chat · read-only snapshot",
            page=12,
            allow_filter=True,
            render=_session_panel_row,
            filter_text=_session_search_text,
            actions={"enter": "select", "space": "preview"},
            controls="↑↓ · Enter 选择 · Space preview · Esc",
            return_action=True)
        if picked is None:
            return None
        session_id = str(picked.get("id") or "")
        if action == "preview":
            try:
                source = store.load_session_preview(session_id)
                show_session_preview(sess, source.get("messages") or [])
            except (OSError, ValueError, store.SessionStoreError) as exc:
                _session_notice(sess, str(exc), error=True)
            continue
        return session_id


def cmd_consult(sess, rest):
    """Start and manage read-only cross-session snapshot consultations."""
    value = str(rest or "").strip()
    target = ""
    question = ""
    if not value:
        if sess.pump is None:
            _consult_usage()
            return
        target = _pick_consult_target(sess)
        if not target:
            return
        question = sess.prompt_text(
            prompt="consult› ",
            title=f"Ask saved chat {target}",
            hint="只读快照 · Chat A 保持前台 · Esc 取消")
        if not question or not question.strip():
            print(DIM("  (取消)"))
            return
        question = question.strip()
    else:
        parts = value.split(maxsplit=2)
        action = parts[0].lower()
        if action in {
                "help", "list", "status", "open", "followup", "cancel",
                "start"}:
            try:
                if action == "help":
                    _consult_usage()
                    return
                if action == "list":
                    rows = sess.list_consults()
                    if not rows:
                        print(DIM("  (当前 chat 还没有 consult)"))
                        return
                    for row in rows:
                        print("  " + _consult_panel_row(row))
                    return
                if action == "followup":
                    if len(parts) < 3 or not parts[2].strip():
                        print(DIM(
                            "  用法：/consult followup <cs-id> <question>"))
                        return
                    item = sess.send_consult(parts[1], parts[2].strip())
                    print(DIM(
                        f"  [follow-up 已排入 {parts[1]} · "
                        f"{item.get('id') or '?'}]"))
                    return
                if action == "start":
                    if len(parts) < 3 or not parts[2].strip():
                        print(DIM(
                            "  用法：/consult start <session-id> <question>"))
                        return
                    target, question = parts[1], parts[2].strip()
                else:
                    hint = parts[1] if len(parts) > 1 else None
                    record = sess.consult_record(hint)
                    if action == "status":
                        _print_consult(record)
                    elif action == "open":
                        show_agent_preview(sess, record.get("id"))
                    elif action == "cancel":
                        cancelled = sess.cancel_consult(record.get("id"))
                        print(DIM(
                            f"  [consult {cancelled.get('id')} "
                            "cancel requested]"))
                    return
            except (CONSULTS.ConsultError, AGENTS.AgentRuntimeError,
                    OSError, ValueError, store.SessionStoreError) as exc:
                print(RED(f"  [consult] {exc}"))
                return
        else:
            target = parts[0]
            question = value[len(target):].strip()
            if not question:
                print(DIM("  用法：/consult <session-id> <question>"))
                return

    try:
        record = sess.start_consult(target, question)
    except (CONSULTS.ConsultError, AGENTS.AgentRuntimeError,
            OSError, ValueError, store.SessionStoreError) as exc:
        print(RED(f"  [consult] {exc}"))
        return
    source = record.get("source_snapshot") or {}
    run_id = str(record.get("id") or "?")
    source_title = str(source.get("title") or "(无标题)")
    source_id = str(source.get("session_id") or "?")
    snapshot = str(source.get("snapshot_sha256") or "?")[:12]
    route = f"{record.get('model') or '?'}@{record.get('gateway') or '?'}"
    print(DIM(
        f"  [consult {run_id} 已启动 ← {source_title} ({source_id}) · "
        f"{route} · snapshot {snapshot}]"))
    if source.get("source_live"):
        print(YELLOW(
            "  [来源 chat 正在另一个 runtime 中；本次只读取最后已保存快照]"))
    elif source.get("source_lease_state") == "unknown":
        print(YELLOW(
            "  [无法核验来源 chat 的 lease；本次仍只读已保存快照，"
            "freshness 未知]"))


_WORKFLOW_SEAT_COLORS = {
    "Qwen": BLUE,
    "GLM": lambda value: c("38;5;141", value),
    "DeepSeek": GREEN,
    "MiniMax": YELLOW,
    "Kimi": ORANGE,
    "Claude": lambda value: c("38;5;208", value),
    "GPT": lambda value: c("38;5;45", value),
}


def _agent_activity_text(payload):
    """agent_activity 载荷 → 名册里的一句"正在做什么"。"""
    payload = dict(payload or {})
    tool = str(payload.get("tool") or payload.get("name") or payload.get("phase") or "").strip()
    args = str(payload.get("args") or "").strip()
    return " ".join(part for part in (tool, args[:60]) if part)


def _agent_dock_row(record, *, activity="", tokens=None):
    """名册行（PLAN-agent-visibility 纪律 2/3）：状态点 · 席位 · label · 最近一步 · 耗时 · token。"""
    state = str(record.get("state") or "")
    kind = {"completed": "finished", "failed": "failed", "cancelled": "cancelled"}.get(
        state, "activity")
    event_obj = AGENT_EVENTS.from_agent_record(
        kind, record, tokens=tokens, activity=activity)
    return AGENT_EVENTS.roster_row(event_obj)


def _composer_dock_items(sess):
    """Project the current ordinary child group into clickable composer rows."""
    try:
        rows = [
            row for row in sess.list_agents(limit=20)
            if not str(row.get("kind") or "subagent").startswith("workflow-")
        ]
    except (AGENTS.AgentRuntimeError, OSError, ValueError):
        rows = []
    current_turn = str(
        getattr(getattr(sess, "controller", None), "current_turn_id", "")
        or "")
    active = [
        row for row in rows
        if str(row.get("state") or "") not in AGENTS.TERMINAL_STATES]
    sess._active_agent_count = len(active)        # 状态栏 "N agents"（J5）
    if current_turn:
        selected = [
            row for row in rows
            if str(row.get("parent_turn_id") or "") == current_turn]
        seen = {str(row.get("id") or "") for row in selected}
        selected.extend(
            row for row in active
            if str(row.get("id") or "") not in seen)
    elif rows:
        latest_turn = str(
            (active[0] if active else rows[0]).get("parent_turn_id") or "")
        selected = [
            row for row in rows
            if latest_turn
            and str(row.get("parent_turn_id") or "") == latest_turn]
        if not selected:
            selected = active or rows[:1]
        # 终态（failed/completed/cancelled）的 agent 不常驻 dock：
        # 历史状态在 /agents 面板里查，composer 只显示还在跑的。
        # 否则重启后「最近一轮」回退会把整轮 failed subagent 捞回来。
        selected = [
            row for row in selected
            if str(row.get("state") or "") not in AGENTS.TERMINAL_STATES]
        if not selected:
            selected = []
    else:
        selected = []

    if selected:
        selected = selected[:4]
        attached = bool(getattr(sess, "attached_agent_id", None))
        header = tui.DockItem(
            f"● main · {len(selected)} child agents · click/Ctrl+T open",
            event="agent_detach" if attached else None)
        activity_map = getattr(sess, "_agent_activity", {}) or {}
        return (
            header,
            *(tui.DockItem(
                _agent_dock_row(
                    row, activity=activity_map.get(str(row.get("id") or ""), "")),
                event="agent_attach",
                value=str(row.get("id") or ""))
              for row in selected),
        )

    return tuple(
        tui.DockItem(row, event="agents")
        for row in _workflow_dock_rows(
            getattr(sess, "_workflow_current", None),
            activity_map=getattr(sess, "_agent_activity", None)))


_WORKFLOW_ROSTER_LIMIT = 4


def _workflow_dock_rows(record, activity_map=None):
    """Project the active workflow into a small composer-adjacent dock.

    J7：running 节点在 DAG 行下面各占一条名册行，与子代理名册同一格式。
    """
    if (not isinstance(record, dict)
            or record.get("state") not in WORKFLOWS.ACTIVE_STATES):
        return ()

    workflow_id = str(record.get("id") or "wf-?")
    state = str(record.get("state") or "queued")
    stage = str(record.get("stage") or "queued")
    completed = int(record.get("completed_agents") or 0)
    expected = int(record.get("expected_agents") or 0)
    budget = record.get("budget") or {}
    started = int(record.get("requests_started") or 0)
    maximum = int(budget.get("max_requests") or 0)
    request_progress = f"req {started}/{maximum or '?'}"
    header = (
        f"◆ {workflow_id} · {state}/{stage} · agents "
        f"{completed}/{expected or '?'} · {request_progress} · "
        "Ctrl+T 打开线程")

    plan = record.get("plan") or {}
    nodes = plan.get("agents") or []
    counts = {}
    for node in nodes:
        node_state = str(node.get("state") or "queued")
        counts[node_state] = counts.get(node_state, 0) + 1
    marks = " ".join(
        f"{mark}{counts[key]}" for key, mark in (
            ("completed", "✓"), ("running", "●"), ("waiting", "◌"),
            ("queued", "○"), ("failed", "!"), ("cancelled", "×"))
        if counts.get(key))
    note = ""
    if stage == "preflight":
        note = " · 探活中"
    elif state == "paused":
        note = " · 已暂停"
    elif state == "recovering" or stage == "recovering":
        note = " · 恢复中"
    # DAG 是唯一拓扑：不再有 agents→review→synthesis 三段进度，只看节点计数
    dag = (
        f"DAG  节点 {counts.get('completed', 0)}/{len(nodes) or '?'}"
        + (f"  {marks}" if marks else "") + note)

    state_mark = {
        "queued": "○", "running": "●", "waiting": "◌",
        "completed": "✓", "failed": "!", "cancelled": "×",
    }
    seat_states = {}
    for source in (
            record.get("lineup") or (), plan.get("agents") or (),
            record.get("agents") or ()):
        for item in source:
            seat = str(item.get("seat") or "").strip()
            if seat:
                seat_states[seat] = str(item.get("state") or "queued")
    seats = []
    for item in record.get("lineup") or ():
        seat = str(item.get("seat") or "?")
        gateway = str(item.get("gateway") or "?")
        marker = state_mark.get(seat_states.get(seat, "queued"), "?")
        seats.append(f"{marker} {seat}@{gateway}")
    lineup = "Seats  " + (" · ".join(seats) if seats else "等待 agent 分配")
    rows = [header, dag, lineup]
    activity_map = activity_map or {}
    running = [node for node in nodes if str(node.get("state") or "") == "running"]
    for node in running[:_WORKFLOW_ROSTER_LIMIT]:
        agent_id = str(node.get("agent_id") or "")
        rows.append(AGENT_EVENTS.roster_row(AGENT_EVENTS.from_workflow_node(
            record, node, activity=str(activity_map.get(agent_id) or ""))))
    if len(running) > _WORKFLOW_ROSTER_LIMIT:
        rows.append(f"  … 还有 {len(running) - _WORKFLOW_ROSTER_LIMIT} 个节点运行中")
    return tuple(rows)


def _workflow_motion_enabled():
    value = str(paths.env_get("MOTION", "1")).strip().lower()
    term = str(os.environ.get("TERM", "")).strip().lower()
    return (
        _TTY and "NO_COLOR" not in os.environ
        and value not in {"0", "off", "false", "no"}
        and term != "dumb")


def _play_workflow_launch(sess, record):
    """短暂组装异构棱镜，并在 scrollback 留下一行完整链条。"""
    lineup = list(record.get("lineup") or [])
    seats = [str(row.get("seat") or "?") for row in lineup]
    gateways = {
        str(row.get("gateway") or "?") for row in lineup}
    preset = str(record.get("preset") or "standard")
    chain = " → ".join(seats) or "waiting for seats"
    final = (
        f"  ◆ {record.get('id')} · {chain} · "
        f"{len(seats)} flagship models · {len(gateways)} gateways · {preset}")
    if not _workflow_motion_enabled():
        print(DIM(final))
        return
    if getattr(sess, "renderer", None) is not None:
        sess.renderer.clear_input()
    frames = max(3, len(seats) + 2)
    for frame in range(frames):
        nodes = []
        lit = min(len(seats), max(0, frame))
        for index, seat in enumerate(seats):
            mark = "●" if index < lit else "○"
            color = _WORKFLOW_SEAT_COLORS.get(seat, DIM)
            nodes.append(color(f"{mark} {seat}"))
        center = ORANGE("◆") if frame >= len(seats) + 1 else DIM("◇")
        line = (
            f"  {center} 异构棱镜  "
            + DIM(" ─ ").join(nodes))
        sys.stdout.write("\r\x1b[2K" + line)
        sys.stdout.flush()
        time.sleep(0.07)
    sys.stdout.write("\r\x1b[2K" + DIM(final) + "\r\n")
    sys.stdout.flush()


def _workflow_lineup_rows(sess):
    selected = {
        row["seat"]: row
        for row in sess.workflow_manager.resolve_lineup()}
    rows = []
    for config in M.WORKFLOW_DEFAULT_SEATS:
        seat = config["seat"]
        row = selected.get(seat)
        if row is None:
            rows.append({
                "seat": seat, "id": "—", "gateway": "—",
                "status": "缺席（无合格可用旗舰）",
            })
        else:
            rows.append({
                **row, "status": "flagship · tools ok · route healthy",
            })
    return rows


def _render_workflow_model(row):
    layout = _table_layout((
        tui.TableColumn(10, min_width=8),
        tui.TableColumn(32, min_width=18),
        tui.TableColumn(10, min_width=8),
        tui.TableColumn(36, min_width=12),
    ), indent=2, maximum=96)
    missing = row.get("id") == "—"
    line = layout.row((
        row.get("seat", "?"),
        row.get("id", "?"),
        row.get("gateway", "?"),
        row.get("status", "?"),
    ))
    return DIM(line) if missing else line


def _workflow_panel_row(record):
    return (
        f"{tui.pad_display(record.get('id', '?'), 14)} "
        f"{tui.pad_display(record.get('state', '?'), 10)} "
        f"{tui.pad_display(record.get('stage', '?'), 11)} "
        f"{int(record.get('completed_agents') or 0):>2}/"
        f"{int(record.get('expected_agents') or 0):<2}  "
        f"{tui.pad_display(record.get('preset', '?'), 9)} "
        f"{tui.truncate_display(record.get('goal', ''), 42)}")


def _print_workflow(record):
    print()
    print(f"  {BOLD(record.get('id', '?'))} · "
          f"{record.get('state', '?')} · {record.get('stage', '?')}")
    print(f"  目标  {record.get('goal') or '?'}")
    print(
        f"  模式  {record.get('mode')} · {record.get('preset')} · "
        f"agents {int(record.get('completed_agents') or 0)}/"
        f"{int(record.get('expected_agents') or 0)}")
    for row in record.get("lineup") or []:
        marker = {
            "queued": "○", "running": "●", "completed": "✓",
            "failed": "!", "cancelled": "×",
        }.get(row.get("state"), "○")
        route = f"{row.get('model')}@{row.get('gateway')}"
        print(f"    {marker} {tui.pad_display(row.get('seat', '?'), 10)} "
              f"{route}")
    if record.get("missing_seats"):
        print(YELLOW(
            "  缺席  " + ", ".join(record["missing_seats"])
            + "（未用低质量模型补位）"))
    if record.get("error"):
        print(RED(f"  错误  {record['error']}"))
    if record.get("result"):
        print(f"\n  {BOLD('Synthesis')}")
        print(AGENTS.project_report(record["result"]))
    print(DIM(
        "  /workflow console [id] 看 DAG/预算 · /workflow open [id] 看子线程 · "
        "/workflow pause|resume [id]"))


def _workflow_elapsed_text(record):
    start = _parse_utc(record.get("started_at") or record.get("created_at"))
    end = _parse_utc(record.get("ended_at")) or datetime.now(timezone.utc)
    if start is None:
        return "?"
    seconds = max(0.0, (end - start).total_seconds())
    return f"{seconds:.1f}s"


def _print_workflow_console(record):
    """Dense, stable console view; no polling or alternate-screen UI."""
    budget = record.get("budget") or {}
    started = int(record.get("requests_started") or 0)
    request_max = int(budget.get("max_requests") or 0)
    tokens = int(record.get("tokens_used") or 0)
    token_max = int(budget.get("max_tokens") or 0)
    nodes = (record.get("plan") or {}).get("agents") or []
    print()
    print(
        f"  {BOLD('Workflow Console')} · {record.get('id')} · "
        f"{record.get('state')}/{record.get('stage')} · "
        f"elapsed {_workflow_elapsed_text(record)}")
    print(f"  {record.get('goal') or '?'}")
    print(
        f"  provider attempts {started}/{request_max or '?'} · "
        f"planned {int(budget.get('planned_requests') or 0)} · "
        f"tokens {tokens:,}/{token_max or '?'} · "
        f"parallel≤{budget.get('max_parallel') or '?'} · "
        f"gateway≤{budget.get('max_per_gateway') or '?'} · "
        f"per-agent≤{budget.get('max_requests_per_agent') or '?'} · "
        f"nodes {len(nodes)}/{budget.get('max_nodes') or '?'}")
    capsule = record.get("context_capsule") or {}
    if capsule:
        print(DIM(
            f"  capsule {str(capsule.get('sha256') or '?')[:16]} · "
            f"{int(capsule.get('chars') or 0):,} chars · "
            f"source {capsule.get('source_session') or '?'}"))
    layout = _table_layout((
        tui.TableColumn(16, min_width=8),
        tui.TableColumn(10, min_width=8),
        tui.TableColumn(10, min_width=8),
        tui.TableColumn(28, min_width=15),
        tui.TableColumn(20, min_width=10),
    ), indent=2, maximum=110)
    print(DIM(layout.row(("node", "state", "seat", "route", "depends"))))
    markers = {
        "queued": "○", "running": "●", "completed": "✓",
        "failed": "!", "cancelled": "×",
    }
    for node in nodes:
        state = str(node.get("state") or "?")
        route = f"{node.get('model') or '?'}@{node.get('gateway') or '?'}"
        depends = ",".join(node.get("depends_on") or []) or "—"
        print(layout.row((
            f"{markers.get(state, '?')} {node.get('key') or '?'}",
            state, node.get("seat") or node.get("role") or "?",
            route, depends)))
        if node.get("error"):
            _print_wrapped(
                str(node["error"]), first_prefix=f"↳ {node.get('key')}: ", color=RED)
    events = record.get("events") or []
    if events:
        print(DIM("  recent controls:"))
        for event in events[-5:]:
            print(DIM(
                f"    {str(event.get('ts') or '?')[11:19]} "
                f"{event.get('kind')} "
                f"{json.dumps(event.get('payload') or {}, ensure_ascii=False)[:90]}"))
    print(DIM(
        "  controls: pause/resume · restart <node> [cascade] · "
        "cancel-node <node> · send <node> <message> · open"))


def _open_workflow_agents(sess, record):
    rows = []
    seen = set()
    for item in record.get("agents") or []:
        agent_id = str(item.get("id") or "")
        if not agent_id or agent_id in seen:
            continue
        seen.add(agent_id)
        try:
            rows.append(sess.agent_record(agent_id, refresh=True))
        except AGENTS.AgentRuntimeError:
            continue
    if not rows:
        print(DIM("  (该 workflow 还没有可打开的 agent 线程)"))
        return None
    action, picked = sess.pick(
        rows,
        title=f"{record['id']} · agent workspace",
        page=12,
        allow_filter=True,
        render=_agent_panel_row,
        actions={"enter": "attach", "space": "peek"},
        controls=(
            "Click/Enter 打开线程 · Space 预览 · "
            "滚轮/↑↓ 导航 · Esc 返回"),
        fullscreen=True, mouse=True, mouse_action="attach",
        return_action=True)
    if picked is None:
        return None
    if action == "peek":
        return show_agent_preview(sess, picked["id"])
    show_agent_preview(sess, picked["id"])
    return sess.attach_agent(picked["id"])


def _set_session_seats(sess, seats):
    """把新的 workflow.seats 写进会话 cfg（浅拷贝，不污染 DEFAULTS）并应用。"""
    workflow_cfg = dict((sess.cfg or {}).get("workflow") or {})
    workflow_cfg["seats"] = dict(seats)
    sess.cfg = {**(sess.cfg or {}), "workflow": workflow_cfg}
    return sess._apply_workflow_seats()


def _print_workflow_seats(sess):
    live = {}
    manager = getattr(sess, "workflow_manager", None)
    if manager is not None:
        try:
            live = {row["seat"]: row for row in manager.resolve_lineup()}
        except Exception:
            live = {}
    print(f"\n  {BOLD('Workflow seats')}  "
          + DIM("可配：/workflow seats add|remove|reset · 写入用户级 settings.json，立即生效"))
    layout = _table_layout((
        tui.TableColumn(12, min_width=8),
        tui.TableColumn(16, min_width=10),
        tui.TableColumn(30, min_width=16),
        tui.TableColumn(44, min_width=18),
    ), indent=2, maximum=112)
    print(DIM(layout.row(("seat", "source", "live route", "candidates"))))
    for config in M.WORKFLOW_DEFAULT_SEATS:
        seat = config["seat"]
        row = live.get(seat)
        route = (f"{row['id']}@{row['gateway']}" if row
                 else "缺席（无合格可用 route）")
        candidates = ", ".join(
            f"{gateway}/{model}" for gateway, model in config["candidates"])
        print(layout.row(
            (seat, M.workflow_seat_source(seat), route, candidates),
            wrap_last=True))
    removed = [
        name for name, value in M.WORKFLOW_SEAT_OVERRIDES.items()
        if value is None]
    if removed:
        print(DIM("  已移除（用户配置）：" + ", ".join(removed)))


def _suggest_seat_name(row):
    base = str(row.get("family") or row.get("id") or "Seat")
    base = base.split("/")[-1].split("-")[0].split("_")[0]
    name = "".join(ch for ch in base if ch.isalnum() or ch in "_.-")[:24]
    if not name or not name[0].isalpha():
        name = "Seat" + name
    return name[0].upper() + name[1:]


def _workflow_seat_add_interactive(sess, seat=None):
    rows = [row for row in M.probed_rows(only_ok=True) if row.get("supports_tools")]
    if not rows:
        print(RED("  [workflow seats] 没有已探测可用（tools ok）的模型；先 /probe 或 /model 刷新"))
        return None
    layout = _table_layout((
        tui.TableColumn(10, min_width=8),
        tui.TableColumn(36, min_width=18),
        tui.TableColumn(12, min_width=8),
        tui.TableColumn(8, min_width=6),
    ), indent=2, maximum=90)
    picked = sess.pick(
        rows,
        title="选择要加入席位的模型（只列 capability tools ok 的）",
        controls="↑↓ 选择 · 打字筛选 · Enter 确认 · Esc 取消",
        page=12,
        render=lambda r: layout.row((
            r.get("gateway", "?"), r.get("id", "?"), r.get("family", "?"),
            (f"{r['first_token_s']:.1f}s" if r.get("first_token_s") else ""))))
    if not picked:
        return None
    route = f"{picked['gateway']}/{picked['id']}"
    if not seat:
        suggested = _suggest_seat_name(picked)
        seat = sess.prompt_text(
            prompt="席位名› ", initial=suggested,
            title="给这个席位起名（DAG 里引用的名字）",
            hint="字母开头 ≤24 字符；同名会替换该席位的候选路由")
        seat = str(seat or "").strip()
        if not seat:
            return None
    return seat, [route]


def _workflow_seat_remove_interactive(sess):
    rows = [{"id": name} for name in M.WORKFLOW_SEAT_NAMES]
    if not rows:
        print(RED("  [workflow seats] 席位表是空的"))
        return None
    picked = sess.pick(
        rows, title="选择要移除的席位",
        controls="↑↓ 选择 · Enter 移除 · Esc 取消", page=8, allow_filter=False,
        render=lambda r: f"  {tui.pad_display(r['id'], 14)}{M.workflow_seat_source(r['id'])}")
    return picked["id"] if picked else None


def _workflow_seats_command(sess, rest):
    """/workflow seats [list|add <Seat> <gateway/model>...|remove <Seat>|reset]

    add/remove 不带参数时走选择器（像 /statusline 那样选想要的项）；带参数可脚本化。
    改动写入用户级 settings.json（只动 workflow.seats）并立即生效。
    """
    head, _, tail = str(rest or "").strip().partition(" ")
    action = head.strip().lower() or "list"
    tail = tail.strip()
    current = dict(CFG.workflow_policy(sess.cfg).get("seats") or {})
    try:
        if action in {"list", "show"}:
            _print_workflow_seats(sess)
            return
        if action == "reset":
            CFG.update_user_workflow_seats(None)
            _set_session_seats(sess, {})
            print(DIM("  [workflow seats] 已清除用户覆盖，恢复内置席位"))
            _print_workflow_seats(sess)
            return
        if action == "add":
            parts = tail.split()
            seat = parts[0] if parts else None
            routes = parts[1:]
            if not routes:
                picked = _workflow_seat_add_interactive(sess, seat)
                if picked is None:
                    print(DIM("  (取消)"))
                    return
                seat, routes = picked
            parsed = [M.parse_seat_route(route) for route in routes]
            bad = [route for route, ok in zip(routes, parsed) if ok is None]
            if bad or not M._SEAT_NAME.fullmatch(str(seat or "")):
                print(RED(
                    "  [workflow seats] 无效：席位名需字母开头 ≤24 字符；"
                    f"route 形如 deepinfer/minimax-m2.7（问题：{', '.join(bad) or '席位名'}）"))
                return
            value = {"candidates": [f"{gateway}/{model}" for gateway, model in parsed]}
            CFG.update_user_workflow_seats({seat: value})
            current[seat] = value
            _set_session_seats(sess, current)
            print(DIM(f"  [workflow seats] {seat} ← " + ", ".join(value["candidates"])))
            _print_workflow_seats(sess)
            return
        if action == "remove":
            seat = tail.split()[0] if tail else _workflow_seat_remove_interactive(sess)
            if not seat:
                print(DIM("  (取消)"))
                return
            if seat not in M.WORKFLOW_SEAT_NAMES:
                print(RED(f"  [workflow seats] 没有席位 {seat}"))
                return
            CFG.update_user_workflow_seats({seat: None})
            current[seat] = None
            _set_session_seats(sess, current)
            print(DIM(f"  [workflow seats] 已移除 {seat}"))
            _print_workflow_seats(sess)
            return
    except CFG.SettingsError as exc:
        print(RED(f"  [workflow seats] {exc}"))
        return
    print(RED(
        "  [workflow seats] 用法：/workflow seats "
        "[list | add <Seat> <gateway/model>... | remove <Seat> | reset]"))


def _workflow_usage():
    print(DIM(
        "  编排归模型：直接告诉它要做什么，它会用 workflow 工具编排异构 DAG\n"
        "  /workflow auto on|off|status           允许主模型自主启动 DAG（按 chat）\n"
        "  （观测与控制也并入 /agents：/agents workflow status|console|pause|resume|cancel）\n"
        "  /workflow status [id]                  查看状态\n"
        "  /workflow console [id]                 DAG、预算与事件面板\n"
        "  /workflow list                         当前 chat 的历史\n"
        "  /workflow open [id]                    打开 child threads\n"
        "  /workflow models                       查看席位候选与实际路由\n"
        "  /workflow seats [add|remove|reset]      查看/修改席位（用户可配，写入 settings）\n"
        "  /workflow recipes                      列出内置/用户/项目 recipes\n"
        "  /workflow validate <name|path>         静态校验 recipe\n"
        "  /workflow run <name> key=value...      参数化运行 recipe\n"
        "  /workflow save [id] <name> [scope]     保存已运行 DAG\n"
        "  /workflow pause|resume [id]            暂停或恢复整组\n"
        "  /workflow restart <node> [cascade]     重跑节点（必要时级联下游）\n"
        "  /workflow cancel-node <node>           取消一个节点\n"
        "  /workflow send <node> <message>        给 running 节点追加线索\n"
        "  /workflow add <json-array>             动态追加只读 DAG 节点\n"
        "  /workflow cancel [id]                  取消整组\n"
        "  /workflow apply [id]                   synthesis 交给主 agent"))


def cmd_workflow(sess, rest):
    """启动/管理异构模型 workflow；所有 child agents 在代码层只读。"""
    value = str(rest or "").strip()
    if not value:
        _workflow_usage()
        print(DIM(
            "  异构编排归模型：直接告诉它要做什么，它会用 workflow 工具编排"
            "（需 /workflow auto on）。"))
        return
    if True:
        head, _, tail = value.partition(" ")
        action = head.lower()
        if action in {"help", "models", "recipes", "validate", "run",
                      "save", "list", "status", "console",
                      "open", "auto", "pause", "resume", "restart",
                      "cancel-node", "send", "add", "cancel", "apply",
                      "seats"}:
            try:
                if action == "help":
                    _workflow_usage()
                    return
                if action == "auto":
                    choice = tail.strip().lower() or "status"
                    if choice not in {
                            "on", "off", "status", "enable", "disable"}:
                        raise ValueError(
                            "用法：/workflow auto on|off|status")
                    if choice in {"on", "enable"}:
                        sess.set_workflow_auto(True)
                    elif choice in {"off", "disable"}:
                        sess.set_workflow_auto(False)
                    policy = CFG.workflow_policy(sess.cfg)
                    state = "ON" if sess.workflow_auto else "OFF"
                    print(
                        f"  {BOLD('Adaptive workflow')}  {state}")
                    print(DIM(
                        f"  bounds: agents≤{policy['max_agents']} · "
                        f"nodes≤{policy['max_nodes']} · "
                        f"parallel≤{policy['max_parallel']} · "
                        f"reviewers≤{policy['max_reviewers']} · "
                        f"rounds≤{policy['max_review_rounds']} · "
                        f"attempts≤{policy['max_requests']} · "
                        f"per-agent≤{policy['max_requests_per_agent']}"))
                    print(DIM(
                        "  仅复杂且可并行拆分的任务由模型触发；"
                        "同一 chat 同时最多一组。"))
                    return
                if action == "seats":
                    _workflow_seats_command(sess, tail)
                    return
                if action == "models":
                    rows = _workflow_lineup_rows(sess)
                    print(f"\n  {BOLD('Workflow flagship candidates')}")
                    for row in rows:
                        print("  " + _render_workflow_model(row))
                    print(DIM(
                        "  只接受席位白名单 + capability tools ok + "
                        "health circuit closed；缺席不降质。席位可配：/workflow seats。"))
                    return
                if action == "recipes":
                    rows = RECIPES.list_recipes(sess._session_cwd)
                    layout = _table_layout((
                        tui.TableColumn(24, min_width=16),
                        tui.TableColumn(10, min_width=8),
                        tui.TableColumn(12, min_width=8),
                        tui.TableColumn(58, min_width=20),
                    ), indent=2, maximum=108)
                    print(DIM(layout.row((
                        "name", "scope", "params", "description / error"))))
                    for row in rows:
                        params = ",".join((row.get("params") or {}).keys()) or "—"
                        detail = row.get("error") or row.get("description") or "—"
                        line = layout.row((
                            row.get("name") or "?", row.get("scope") or "?",
                            params, detail))
                        print(RED(line) if row.get("error") else line)
                    print(DIM(
                        "  同名来源必须用 builtin:name / user:name / "
                        "project:name 显式消歧。"))
                    return
                if action == "validate":
                    if not tail.strip():
                        raise ValueError(
                            "用法：/workflow validate <name|path>")
                    recipe = RECIPES.resolve(tail.strip(), sess._session_cwd)
                    workflow = recipe["workflow"]
                    print(
                        f"  {GREEN('valid')} recipe {recipe['scope']}:"
                        f"{recipe['name']} · "
                        f"{len(workflow.get('agents') or [])} nodes · "
                        f"mode={workflow.get('mode') or 'research'} · "
                        f"budget={workflow.get('budget') or 'policy'}")
                    for key, spec in (recipe.get("params") or {}).items():
                        required = "required" if spec.get("required") else (
                            f"default={spec.get('default')}"
                            if "default" in spec else "optional")
                        print(DIM(
                            f"    {key:<20} {required} · "
                            f"{spec.get('description') or ''}"))
                    print(DIM(f"  source {recipe.get('source')}"))
                    return
                if action == "run":
                    try:
                        parts = shlex.split(tail)
                    except ValueError as exc:
                        raise ValueError(f"run 参数引号无效：{exc}") from None
                    if not parts:
                        raise ValueError(
                            "用法：/workflow run <name> key=value...")
                    values = {}
                    for item in parts[1:]:
                        key, sep, value_part = item.partition("=")
                        if not sep or not key:
                            raise ValueError(
                                f"recipe 参数必须是 key=value：{item!r}")
                        if key in values:
                            raise ValueError(f"重复 recipe 参数：{key}")
                        values[key] = value_part
                    recipe = RECIPES.resolve(parts[0], sess._session_cwd)
                    record = sess.start_recipe(recipe, values)
                    _play_workflow_launch(sess, record)
                    print(DIM(
                        f"  [recipe {recipe['scope']}:{recipe['name']} 已启动 · "
                        f"workflow {record['id']} · "
                        f"{len((record.get('plan') or {}).get('agents') or [])} nodes]"))
                    return
                if action == "save":
                    try:
                        parts = shlex.split(tail)
                    except ValueError as exc:
                        raise ValueError(f"save 参数引号无效：{exc}") from None
                    if not parts:
                        raise ValueError(
                            "用法：/workflow save [id] <name> "
                            "[user|project] [overwrite]")
                    if parts[0].startswith("wf-"):
                        if len(parts) < 2:
                            raise ValueError(
                                "指定 id 后还需要 recipe name")
                        record = sess.workflow_record(parts.pop(0))
                    else:
                        record = sess.workflow_record()
                    name = parts.pop(0)
                    scope = "user"
                    overwrite = False
                    for item in parts:
                        if item in {"user", "project"}:
                            scope = item
                        elif item in {"overwrite", "--overwrite"}:
                            overwrite = True
                        else:
                            raise ValueError(f"未知 save 参数：{item}")
                    recipe = RECIPES.from_workflow(record, name)
                    path = RECIPES.save(
                        recipe, scope=scope, cwd=sess._session_cwd,
                        overwrite=overwrite)
                    print(DIM(
                        f"  [已保存 recipe {scope}:{name} · {path}]"))
                    return
                if action == "list":
                    rows = sess.list_workflows()
                    if not rows:
                        print(DIM("  (当前 chat 还没有 workflow)"))
                        return
                    for row in rows:
                        print("  " + _workflow_panel_row(row))
                    return
                if action == "add":
                    if not tail.strip():
                        raise ValueError(
                            "用法：/workflow add <json-array>")
                    try:
                        specs = json.loads(tail)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"add JSON 无效：{exc}") from None
                    record = sess.workflow_record()
                    updated = sess.workflow_manager.add_nodes(
                        record["id"], specs, requested_by="user")
                    sess._workflow_current = dict(updated)
                    print(DIM(
                        f"  [workflow {record['id']} 已追加 "
                        f"{len(specs) if isinstance(specs, list) else 0} nodes]"))
                    return
                if action in {"restart", "cancel-node", "send"}:
                    node_key, _, argument = tail.partition(" ")
                    if not node_key:
                        raise ValueError(
                            f"用法：/workflow {action} <node>"
                            + (" <message>" if action == "send" else " [cascade]"))
                    record = sess.workflow_record()
                    if action == "restart":
                        cascade = argument.strip().lower() in {
                            "cascade", "--cascade"}
                        if argument.strip() and not cascade:
                            raise ValueError(
                                "用法：/workflow restart <node> [cascade]")
                        updated = sess.workflow_manager.restart_node(
                            record["id"], node_key, cascade=cascade,
                            hook_config=sess.cfg)
                        updated = sess.workflow_manager.resume_workflow(
                            updated["id"], hook_config=sess.cfg)
                        sess._workflow_current = dict(updated)
                        print(DIM(
                            f"  [node {node_key} 已重置并恢复 workflow]"))
                    elif action == "cancel-node":
                        if argument.strip():
                            raise ValueError(
                                "用法：/workflow cancel-node <node>")
                        updated = sess.workflow_manager.cancel_node(
                            record["id"], node_key)
                        sess._workflow_current = dict(updated)
                        print(DIM(f"  [node {node_key} cancel requested]"))
                    else:
                        if not argument.strip():
                            raise ValueError(
                                "用法：/workflow send <node> <message>")
                        sess.workflow_manager.send_node(
                            record["id"], node_key, argument,
                            requested_by="user")
                        print(DIM(f"  [消息已排入 node {node_key}]"))
                    return
                record = sess.workflow_record(tail.strip() or None)
                sess._workflow_current = dict(record)
                if action == "status":
                    _print_workflow(record)
                elif action == "console":
                    _print_workflow_console(record)
                elif action == "open":
                    opened = _open_workflow_agents(sess, record)
                    if opened is not None:
                        print(DIM(
                            f"  [已 attach {opened.get('id', '?')}]"))
                elif action == "cancel":
                    cancelled = sess.workflow_manager.cancel(record["id"])
                    sess._workflow_current = dict(cancelled)
                    print(DIM(
                        f"  [workflow {record['id']} cancel requested]"))
                elif action == "pause":
                    paused = sess.workflow_manager.pause_workflow(
                        record["id"])
                    sess._workflow_current = dict(paused)
                    print(DIM(f"  [workflow {record['id']} 已暂停]"))
                elif action == "resume":
                    resumed = sess.workflow_manager.resume_workflow(
                        record["id"], hook_config=sess.cfg)
                    sess._workflow_current = dict(resumed)
                    print(DIM(f"  [workflow {record['id']} 已恢复]"))
                elif action == "apply":
                    queued = sess.queue_workflow_apply(
                        record, manual=True)
                    print(DIM(
                        f"  [workflow {record['id']} "
                        + ("已排入 main agent]" if queued
                           else "已经排队或执行过]")))
                return
            except (WORKFLOWS.WorkflowError, AGENTS.AgentRuntimeError,
                    ValueError) as exc:
                print(RED(f"  [workflow] {exc}"))
                return

        # 手动模板启动（/workflow <goal>、quick|deep、review|research|debug）已退役
        # （2026-09-04）：编排归模型。配方仍可 /workflow run <name>。
        print(RED("  [workflow] 手动模板启动已退役：异构编排归模型。"))
        print(DIM(
            "  直接告诉模型要做什么（需 /workflow auto on）；配方仍可 "
            "/workflow run <name>；运行中的 DAG 用 status|console|cancel 管理。"))
        return


REGISTRY = {
    "new": (cmd_new, '开新会话'),
    "status": (cmd_status, '显示 session、queue、task、context 与 request 状态'),
    "trace": (cmd_trace, '显示 provider attempt 与 tool timeline'),
    "sessions": (cmd_sessions, '列出会话'),
    "resume": (cmd_resume, '切换会话'),
    "rewind": (cmd_rewind, '恢复 checkpoint 的 code/chat 或创建 branch'),
    "rename": (cmd_rename, '给会话命名'),
    "goal": (cmd_goal, '设置/查看跨 turn 完成目标'),
    "recap": (cmd_recap, '让模型现写一行 recap：总目标、当前任务、下一步'),
    "plan": (cmd_plan, '计划模式（只读调研）'),
    "hooks": (cmd_hooks, "查看已注册的 hook"),
    "config": (cmd_config, '显示配置'),
    "doctor": (cmd_doctor, '检查 sandbox、DB、gateway、terminal 与本地状态'),
    "permissions": (cmd_permissions, '查看或修改工具权限'),
    "backups": (cmd_backups, '列出文件备份'),
    "stats": (cmd_stats, '重算并显示总用量'),
    "usage": (cmd_usage, '显示用量汇总与 measured/unknown 覆盖'),
    "help": (cmd_help, '显示帮助'),
    "mouse": (cmd_mouse, '兼容入口：mouse 已固定为 hybrid（已废弃）'),
    "skills": (cmd_skills, '列出可选知识 skill（prompt-data only）'),
    "commands": (cmd_commands, '管理 prompt-only Markdown 自定义命令'),
    "clear": (cmd_clear, '清空对话'),
    "compact": (cmd_compact, '压缩上下文（可附要求：/compact 重点保留 X 的决定）'),
    "auto": (cmd_auto, '切换自动批准'),
    "cost": (cmd_cost, '本次用量明细'),
    "context": (cmd_context, '解释当前上下文预算与投影'),
    "graft": (cmd_graft, "hosted 结构索引状态、构建与诊断"),
    "map": (cmd_map, "隐藏的旧确定性仓库导航（显式兼容保留）"),
    "architecture": (cmd_architecture, "LLM 架构 enrichment（显式构建）"),
    "memory": (cmd_memory, '管理分层长期记忆与 Context Capsule'),
    "model": (cmd_model, '选择模型/路由；gateway/health/refresh 管理网关、健康度与目录'),
    "gateway": (cmd_gateway, '切换网关（已并入 /model gateway）'),
    "probe": (cmd_probe, '探测当前/指定模型能力、缓存或上下文'),
    "agents": (cmd_agents, '查看或 attach child agent（Ctrl+T）'),
    "consult": (cmd_consult, '只读咨询另一个已保存 chat 的快照'),
    "workflow": (cmd_workflow, '异构旗舰模型协作、审查与综合'),
    "tasks": (cmd_tasks, '列出 managed tasks 的状态、耗时与输出大小'),
    "task": (cmd_task, 'tail/attach 一个 task（/task <id>）'),
    "expand": (cmd_expand, '展开工具完整输出（/expand [id] [page]）'),
    "keys": (cmd_keys, '快捷键总览（直接输入 ? 也行）'),
    "kill": (cmd_kill, '终止一个 managed task（/kill <id>）'),
    "tools": (cmd_tools, '列出工具'),
    "exit": (lambda s, r: "quit", "退出"),
}

# `/map` 的实现和注册必须继续存在：旧脚本、显式输入和 `/map` 的二级/三级
# 参数提示仍依赖它；`/mouse` 同样保留注册以兼容显式旧输入。两者都只是软弃用，
# 不再是普通用户的顶层发现入口。
HIDDEN_COMPAT_COMMANDS = frozenset({"map", "mouse", "gateway"})

# 命令面板和 /help 都从注册表生成，但隐藏兼容命令不参与发现。
COMMANDS = {
    f"/{k}": v[1] for k, v in REGISTRY.items()
    if k not in HIDDEN_COMPAT_COMMANDS
}
COMMAND_ALIASES = {"permission": "permissions"}
COMMANDS["/permission"] = "权限管理（/permissions 别名）"

# Inline second-level guidance for the main composer.  This is deliberately
# prompt metadata rather than command parsing: selecting a row only inserts a
# draft, while the existing command handlers remain the single authority for
# validation and side effects.  A user can always ignore the list and type a
# free-form argument (notably ``/goal <objective>`` and ``/workflow <goal>``).
COMMAND_SUBCOMMANDS = {
    "/goal": {
        "hint": "也可直接输入用户目标；若目标恰为保留字请用 set",
        "items": (
            ("status", "查看 phase、round、用量与最近证据"),
            ("set", "创建目标；后接 <objective> [--rounds N]"),
            ("edit", "修改目标或 round 上限"),
            ("accept", "采纳模型起草的目标并 armed"),
            ("reject", "忽略模型起草的目标"),
            ("pause", "暂停自动续轮"),
            ("resume", "明确恢复并允许下一轮 provider 请求"),
            ("complete", "手动验收；可追加 evidence"),
            ("block", "标记外部阻塞；后接 reason"),
            ("clear", "清除目标与未 dispatch 的续轮"),
            ("help", "显示完整 goal 用法"),
        ),
        "params": {
            "set": {
                "hint": "可选轮数限制；参数后继续输入 objective",
                "items": (
                    ("--rounds", "设置最大自动续轮次数"),
                    ("--max-rounds", "设置最大自动续轮次数（别名）"),
                ),
            },
            "edit": {
                "hint": "可只改轮数，也可继续输入新的 objective",
                "items": (
                    ("--rounds", "设置新的最大自动续轮次数"),
                    ("--max-rounds", "设置新的最大自动续轮次数（别名）"),
                ),
            },
        },
    },
    "/model": {
        "hint": "也可直接输入模型名",
        "items": (
            ("gateway", "切换/查看网关（原 /gateway）；可追加名字"),
            ("health", "查看持久健康度；可追加模型关键词"),
            ("refresh", "刷新当前 gateway 模型目录"),
            ("check", "目录 + 实调双证据校验当前模型与各席位"),
            ("help", "显示模型选择用法"),
        ),
    },
    "/recap": {
        "hint": "一次模型调用；Esc 取消",
        "items": (
            ("help", "显示 recap 用法"),
        ),
    },
    "/memory": {
        "hint": "remember/show 等操作会继续要求参数",
        "items": (
            ("status", "查看 use/generate 与注入数量"),
            ("list", "列出 global 与当前 project memory"),
            ("show", "查看一条 memory；后接 <id>"),
            ("remember", "显式记忆；后接 [project|global] <text>"),
            ("forget", "删除一条 memory；后接 <id>"),
            ("use", "切换注入；后接 on|off|status"),
            ("generate", "切换 handoff 生成；后接 on|off|status"),
            ("capture", "立即生成当前会话 handoff"),
            ("refresh", "重新读取并注入 memory 索引"),
            ("capsule", "查看 Context Capsule 元数据"),
            ("help", "显示完整 memory 用法"),
        ),
        "params": {
            "use": {
                "hint": "按当前 chat 保存；不会改写 raw transcript",
                "items": (
                    ("on", "开启 memory 注入"),
                    ("off", "关闭 memory 注入"),
                    ("status", "查看当前注入开关"),
                ),
            },
            "generate": {
                "hint": "只更新本地 handoff，不调用 provider",
                "items": (
                    ("on", "开启会话 handoff 自动生成"),
                    ("off", "关闭会话 handoff 自动生成"),
                    ("status", "查看当前生成开关"),
                ),
            },
            "remember": {
                "hint": "选 scope 后继续输入要记住的正文",
                "items": (
                    ("project", "写入当前项目 memory（默认）"),
                    ("global", "写入全局 memory（会再确认）"),
                ),
            },
        },
    },
    "/map": {
        "hint": "map 默认关闭；新任务优先使用 /graft",
        "items": (
            ("status", "显示 deterministic map 状态"),
            ("build", "构建仓库 map；零 provider"),
            ("refresh", "重算 map；零 provider"),
            ("check", "检查工作树漂移"),
            ("viz", "导出 HTML + Mermaid 视图"),
            ("help", "显示 map 用法"),
        ),
    },
    "/graft": {
        "hint": "build/rebuild 会修改本地索引并受边界策略约束",
        "items": (
            ("status", "查看 hosted Graft 索引状态"),
            ("build", "首次或增量构建结构索引"),
            ("rebuild", "丢弃提示并完整重建索引"),
            ("doctor", "检查 executable、边界与源码规模"),
            ("help", "显示 Graft 用法"),
        ),
    },
    "/architecture": {
        "hint": "只有 build 会调用 provider",
        "items": (
            ("status", "查看 LLM 架构索引状态"),
            ("build", "显式构建架构 enrichment"),
            ("check", "检查来源与工作树漂移"),
            ("viz", "导出 HTML + Mermaid 视图"),
            ("help", "显示 architecture 用法"),
        ),
    },
    "/probe": {
        "hint": "context/能力探测会发送真实 API 请求",
        "items": (
            ("status", "只读本地能力缓存"),
            ("context", "逐档探测上下文上限"),
            ("help", "显示 probe 路由与模式"),
        ),
    },
    "/consult": {
        "hint": "也可直接输入 Chat B id 与问题",
        "items": (
            ("list", "列出当前 chat 的咨询线程"),
            ("status", "查看咨询状态与快照身份"),
            ("open", "打开独立咨询 transcript"),
            ("start", "启动咨询；后接 <session-id> <question>"),
            ("followup", "继续咨询；后接 <cs-id> <question>"),
            ("cancel", "取消咨询；可追加 <cs-id>"),
            ("help", "显示 consult 用法"),
        ),
    },
    "/agents": {
        "hint": "id/message 等参数可在选中后继续输入",
        "items": (
            ("list", "列出当前 parent 的 child agents"),
            ("workflow", "查看/管理当前 workflow 的 DAG 与节点"),
            ("peek", "预览线程；后接 <id>"),
            ("attach", "进入线程；后接 <id>"),
            ("detach", "返回 main composer"),
            ("send", "发送消息；后接 <id> <message>"),
        ),
        "params": {
            "workflow": {
                "hint": "workflow 的观测与控制并入 /agents；可再接 <id>",
                "items": (
                    ("status", "当前 workflow 的 DAG 与节点状态"),
                    ("console", "打开 workflow 控制台"),
                    ("open", "打开某个 workflow；后接 <id>"),
                    ("list", "列出本会话的 workflow"),
                    ("pause", "暂停；后接 <id>"),
                    ("resume", "恢复；后接 <id>"),
                    ("cancel", "取消；后接 <id>"),
                ),
            },
        },
    },
    "/sessions": {
        "hint": "普通历史浏览请使用 /resume",
        "items": (
            ("repair-lease", "隔离损坏 lease；后接完整 session id"),
        ),
    },
    "/rewind": {
        "hint": "选择 checkpoint 后再确认作用范围",
        "items": (
            ("conversation", "恢复会话消息到 checkpoint"),
            ("code", "恢复 write/edit 文件到 checkpoint"),
            ("both", "同时恢复代码与会话"),
            ("branch", "从 checkpoint 创建新分支会话"),
            ("summarize", "把 checkpoint 前缀压成摘要"),
            ("cancel", "取消当前 rewind 操作"),
        ),
    },
    "/task": {
        "hint": "也可直接输入 task id",
        "items": (
            ("detach", "退出当前 task attach"),
        ),
    },
    "/commands": {
        "hint": "也可直接输入自定义命令名查看详情",
        "items": (("refresh", "重新扫描 user/project Markdown 命令"),),
    },
    "/skills": {
        "hint": "skill 正文只由模型按需读取",
        "items": (("refresh", "重新扫描可用 skill 索引"),),
    },
    "/doctor": {
        "hint": "只读诊断，不会发起 provider 请求",
        "items": (("refresh", "刷新 sandbox、DB、gateway 与 terminal 状态"),),
    },
    "/workflow": {
        "hint": "编排归模型：直接告诉它要做什么；这里只管开关、观测与席位",
        "items": (
            ("status", "查看 workflow 状态"),
            ("console", "打开 DAG、预算与事件面板"),
            ("open", "打开 child threads"),
            ("list", "列出当前 chat 的 workflow"),
            ("models", "查看席位候选与实际路由"),
            ("seats", "查看/添加/移除席位（用户可配）"),
            ("recipes", "列出可运行 recipes"),
            ("validate", "静态校验 recipe；后接 <name|path>"),
            ("run", "运行 recipe；后接 <name> key=value..."),
            ("auto", "切换 adaptive workflow；后接 on|off|status"),
            ("pause", "暂停整组 workflow"),
            ("resume", "恢复整组 workflow"),
            ("restart", "重跑节点；后接 <node> [cascade]"),
            ("cancel-node", "取消一个节点；后接 <node>"),
            ("send", "给节点追加线索；后接 <node> <message>"),
            ("add", "追加只读 DAG 节点；后接 JSON array"),
            ("cancel", "取消整组 workflow"),
            ("apply", "把 synthesis 交给主 agent"),
            ("save", "保存为 recipe"),
            ("help", "显示完整 workflow 用法"),
        ),
        "params": {
            "auto": {
                "hint": "只改变当前 chat 的开关；不会因补全而启动请求",
                "items": (
                    ("on", "开启主模型自适应 workflow"),
                    ("off", "关闭主模型自适应 workflow"),
                    ("status", "查看开关与 agents/请求硬上限"),
                    ("enable", "开启（on 的兼容别名）"),
                    ("disable", "关闭（off 的兼容别名）"),
                ),
            },
            "seats": {
                "hint": "席位改动写入用户级 settings.json，立即生效",
                "items": (
                    ("list", "查看席位、来源、实际路由与候选"),
                    ("add", "添加/替换席位：从已探测可用的模型里选；也可 add <Seat> <gateway/model>"),
                    ("remove", "移除席位：从当前席位里选；也可 remove <Seat>"),
                    ("reset", "清除用户覆盖，恢复内置席位"),
                ),
            },
        },
    },
}
# `/permissions`/`/permission` are picker-only commands: their next step is
# a dynamic tool list, not a fixed textual subcommand.  They intentionally do
# not appear in this static catalog, so the inline menu never suggests a token
# that the command handler would interpret as a tool name.

# Dynamic values are deliberately kept separate from the static command
# catalogue.  They are prompt metadata only: discovering them performs no
# provider request and selecting one still goes through the existing command
# parser/permission gates.  In particular, do not put titles, task text, or
# memory contents in this catalogue; those fields may contain private or
# terminal-sensitive material.  IDs, validated recipe names, and DAG keys are
# sufficient to make the parameter menus useful without turning them into a
# second transcript viewer.
_GUIDANCE_DYNAMIC_LIMIT = 32
_GUIDANCE_REFRESH_INTERVAL = 0.35
_GUIDANCE_TOKEN = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63}|"
    r"(?:builtin|user|project):[A-Za-z0-9][A-Za-z0-9_-]{1,47})\Z")


def _guidance_token(value):
    """Return a safe one-token completion label, or ``None``.

    The editor applies the same validation again.  Keeping a local check here
    means malformed records are discarded before they affect signatures or
    tests, and lets us distinguish the namespaced recipe form explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    token = str(value or "").strip().lstrip("/")
    if len(token) > 80:
        return None
    if _GUIDANCE_TOKEN.fullmatch(token) is None:
        return None
    return token


def _guidance_text(*parts, limit=120):
    values = []
    for part in parts:
        raw = str(part or "")
        # State files are user-controlled and may be damaged or unexpectedly
        # large.  Guidance is a small UI projection, not a second transcript;
        # bound work before sanitizing while retaining the ordinary text path.
        if len(raw) > 4096:
            raw = raw[:4096]
        text = " ".join(tui.sanitize_terminal_text(raw).split())
        if text:
            values.append(text)
    if not values:
        return ""
    return tui.truncate_display(" · ".join(values), max(1, int(limit)))


def _guidance_row(token, *description, limit=120):
    token = _guidance_token(token)
    if token is None:
        return None
    return token, _guidance_text(*description, limit=limit)


def _clone_command_guidance():
    """Make a detached, normalized copy of the static prompt catalogue."""
    # Going through the same normalizer used by LineEditor ensures the static
    # and dynamic paths have one grammar and prevents accidental mutation of
    # the module-level tuple/dict constants.
    return tui.LineEditor._normalize_subcommands(COMMAND_SUBCOMMANDS)


def _append_guidance_rows(catalog, command, rows, *, parent=None, hint=None):
    """Append bounded, de-duplicated dynamic rows to a detached catalogue."""
    command = str(command or "").strip().lower()
    if not command.startswith("/"):
        command = "/" + command
    spec = catalog.get(command)
    if spec is None:
        spec = {"items": (), "hint": "", "params": {}}
        catalog[command] = spec
    cleaned = []
    for row in rows or ():
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            continue
        token = _guidance_token(row[0])
        if token is None:
            continue
        cleaned.append((token, _guidance_text(row[1])))
        if len(cleaned) >= _GUIDANCE_DYNAMIC_LIMIT:
            break
    if not cleaned:
        return

    if parent is None:
        existing = list(spec.get("items") or ())
        seen = {str(name).casefold() for name, _ in existing}
        for row in cleaned:
            if row[0].casefold() in seen:
                continue
            existing.append(row)
            seen.add(row[0].casefold())
            if len(existing) >= 64:
                break
        spec["items"] = tuple(existing)
        if hint and not spec.get("hint"):
            spec["hint"] = _guidance_text(hint, limit=160)
        return

    parent = str(parent or "").strip().lstrip("/").casefold()
    known = {str(name).casefold() for name, _ in spec.get("items") or ()}
    if parent not in known:
        return
    params = spec.setdefault("params", {})
    child = params.setdefault(parent, {"items": (), "hint": ""})
    existing = list(child.get("items") or ())
    seen = {str(name).casefold() for name, _ in existing}
    for row in cleaned:
        if row[0].casefold() in seen:
            continue
        existing.append(row)
        seen.add(row[0].casefold())
        if len(existing) >= 64:
            break
    child["items"] = tuple(existing)
    if hint and not child.get("hint"):
        child["hint"] = _guidance_text(hint, limit=160)


def _guidance_records(method, *, limit=_GUIDANCE_DYNAMIC_LIMIT, **kwargs):
    """Call one local metadata reader without making guidance a hard gate."""
    if not callable(method):
        return []
    try:
        rows = method(limit=limit, **kwargs)
    except TypeError:
        try:
            rows = method(**kwargs)
        except Exception:  # noqa: BLE001 - completion is best effort
            return []
    except Exception:  # noqa: BLE001 - completion is best effort
        return []
    try:
        iterator = iter(rows or ())
    except TypeError:
        return []
    try:
        maximum = max(0, int(limit))
    except (TypeError, ValueError):
        maximum = _GUIDANCE_DYNAMIC_LIMIT
    result = []
    try:
        for row in iterator:
            if isinstance(row, dict):
                result.append(row)
                if len(result) >= maximum:
                    break
    except Exception:  # noqa: BLE001 - a broken optional iterator is best effort
        pass
    return result


def _workflow_guidance_rows(sess):
    rows = _guidance_records(
        getattr(sess, "list_workflows", None), limit=_GUIDANCE_DYNAMIC_LIMIT)
    current = getattr(sess, "_workflow_current", None)
    if isinstance(current, dict):
        rows = [current] + [
            row for row in rows
            if str(row.get("id") or "") != str(current.get("id") or "")]
    result = []
    for row in rows:
        token = _guidance_token(row.get("id"))
        if token is None:
            continue
        result.append(_guidance_row(
            token, row.get("state") or "unknown",
            row.get("stage") or "", limit=100))
        if len(result) >= _GUIDANCE_DYNAMIC_LIMIT:
            break
    return [row for row in result if row is not None]


def _current_workflow_for_guidance(sess, workflow_rows):
    try:
        record = sess.workflow_record()
    except Exception:  # noqa: BLE001 - no active workflow is normal
        record = None
    if isinstance(record, dict):
        return record
    current = getattr(sess, "_workflow_current", None)
    if isinstance(current, dict):
        return current
    return workflow_rows[0] if workflow_rows else None


def _node_guidance_rows(record):
    if not isinstance(record, dict):
        return []
    plan = record.get("plan") or {}
    nodes = plan.get("agents") or record.get("agents") or ()
    result = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        token = _guidance_token(node.get("key"))
        if token is None:
            continue
        result.append(_guidance_row(
            token, node.get("state") or "queued", node.get("seat") or "",
            limit=100))
        if len(result) >= _GUIDANCE_DYNAMIC_LIMIT:
            break
    return [row for row in result if row is not None]


def _recipe_guidance_rows(cwd):
    try:
        rows = RECIPES.list_recipes(cwd)
    except Exception:  # noqa: BLE001 - a broken optional catalog is fail-soft
        return []
    valid = [
        row for row in rows
        if isinstance(row, dict) and not row.get("error")
        and _guidance_token(row.get("name")) is not None
    ]
    counts = {}
    for row in valid:
        name = str(row.get("name") or "")
        counts[name] = counts.get(name, 0) + 1
    result = []
    for row in valid:
        name = str(row.get("name") or "")
        scope = str(row.get("scope") or "")
        token = name if counts.get(name) == 1 else f"{scope}:{name}"
        token = _guidance_token(token)
        if token is None:
            continue
        params = row.get("params")
        count = len(params) if isinstance(params, dict) else 0
        result.append(_guidance_row(
            token, scope or "recipe", f"{count} params", limit=100))
        if len(result) >= _GUIDANCE_DYNAMIC_LIMIT:
            break
    return [row for row in result if row is not None]


def _session_id_guidance_rows(rows, *, exclude=None):
    excluded = {str(value) for value in (exclude or ())}
    result = []
    for row in rows or ():
        token = _guidance_token(row.get("id"))
        if token is None or token in excluded:
            continue
        result.append(_guidance_row(
            token, row.get("status") or "active",
            f"{row.get('model') or '?'}@{row.get('gateway') or '?'}",
            limit=100))
        if len(result) >= _GUIDANCE_DYNAMIC_LIMIT:
            break
    return [row for row in result if row is not None]


def _session_command_guidance(sess):
    """Return static + safe local dynamic parameter completions for a session.

    Every reader is bounded and independently fail-soft.  A stale or broken
    optional state file can therefore remove a suggestion, but can never
    prevent the ordinary command from opening or alter its validation.
    """
    catalog = _clone_command_guidance()

    workflow_rows = _workflow_guidance_rows(sess)
    workflow_ids = workflow_rows
    for action in (
            "status", "console", "open", "pause", "resume", "cancel",
            "apply", "save"):
        _append_guidance_rows(
            catalog, "/workflow", workflow_ids, parent=action,
            hint="当前 chat 的 workflow id；也可省略以使用当前项")
    workflow = _current_workflow_for_guidance(sess, workflow_rows)
    nodes = _node_guidance_rows(workflow)
    for action in ("restart", "cancel-node", "send"):
        _append_guidance_rows(
            catalog, "/workflow", nodes, parent=action,
            hint="当前 workflow 的 DAG node key")
    recipe_rows = _recipe_guidance_rows(
        getattr(sess, "_session_cwd", os.getcwd()))
    for action in ("validate", "run"):
        _append_guidance_rows(
            catalog, "/workflow", recipe_rows, parent=action,
            hint="本地 recipe 名；同名来源自动显示 scope:name")

    agents = _guidance_records(
        getattr(sess, "list_agents", None), limit=_GUIDANCE_DYNAMIC_LIMIT)
    agent_rows = []
    for row in agents:
        item = _guidance_row(
            row.get("id"), row.get("state") or "unknown",
            f"{row.get('model') or '?'}@{row.get('gateway') or '?'}",
            limit=100)
        if item is not None:
            agent_rows.append(item)
    for action in ("peek", "attach", "send"):
        _append_guidance_rows(
            catalog, "/agents", agent_rows, parent=action,
            hint="当前 parent 的 child-agent id")

    consults = _guidance_records(
        getattr(sess, "list_consults", None), limit=_GUIDANCE_DYNAMIC_LIMIT)
    consult_rows = []
    for row in consults:
        source = row.get("source_snapshot") or {}
        item = _guidance_row(
            row.get("id"), row.get("state") or "unknown",
            f"source {source.get('session_id') or '?'}", limit=100)
        if item is not None:
            consult_rows.append(item)
    for action in ("status", "open", "followup", "cancel"):
        _append_guidance_rows(
            catalog, "/consult", consult_rows, parent=action,
            hint="当前 chat 的 consult id")
    session_rows = _guidance_records(
        getattr(store, "list_session_summaries", None),
        limit=_GUIDANCE_DYNAMIC_LIMIT, status=None, scope="all",
        cwd=getattr(sess, "_session_cwd", os.getcwd()))
    session_ids = _session_id_guidance_rows(
        session_rows, exclude={getattr(getattr(sess, "ag", None),
                                       "session_id", "")})
    _append_guidance_rows(
        catalog, "/consult", session_ids, hint="只读快照来源 session id")
    _append_guidance_rows(
        catalog, "/consult", session_ids, parent="start",
        hint="只读快照来源 session id")

    memory_rows = []
    memory_store = getattr(sess, "memory_store", None)
    if memory_store is not None:
        try:
            source_rows = memory_store.list(
                cwd=getattr(sess, "_session_cwd", os.getcwd()))
        except Exception:  # noqa: BLE001
            source_rows = []
        for row in source_rows[:_GUIDANCE_DYNAMIC_LIMIT]:
            item = _guidance_row(
                row.get("id"), row.get("scope") or "memory",
                row.get("kind") or "entry", limit=100)
            if item is not None:
                memory_rows.append(item)
    for action in ("show", "forget"):
        _append_guidance_rows(
            catalog, "/memory", memory_rows, parent=action,
            hint="当前 global/project memory id")

    task_manager = getattr(sess, "task_manager", None)
    task_rows = []
    if task_manager is not None:
        try:
            source_tasks = task_manager.list()
        except Exception:  # noqa: BLE001
            source_tasks = []
        for task in source_tasks[:_GUIDANCE_DYNAMIC_LIMIT]:
            item = _guidance_row(
                getattr(task, "id", None),
                getattr(task, "status", None) or "task", limit=100)
            if item is not None:
                task_rows.append(item)
    _append_guidance_rows(
        catalog, "/task", task_rows,
        hint="当前 CLI lifetime 的 task id")
    _append_guidance_rows(
        catalog, "/kill", task_rows,
        hint="可终止的 managed task id")
    _append_guidance_rows(
        catalog, "/expand", task_rows,
        hint="task id 也可展开其完整 artifact")

    # Checkpoint guidance uses a metadata-only reader.  Constructing the full
    # CheckpointStore would secure/create directories and per-manifest locks,
    # which is appropriate for rewind but not for an idle completion poll.
    try:
        checkpoints = CHECKPOINTS.list_checkpoint_ids_readonly(
            store.HOME, sess.ag.session_id, limit=_GUIDANCE_DYNAMIC_LIMIT)
    except Exception:  # noqa: BLE001
        checkpoints = []
    checkpoint_rows = []
    for row in checkpoints[:_GUIDANCE_DYNAMIC_LIMIT]:
        file_count = row.get("files")
        detail = (
            f"{file_count} files"
            if isinstance(file_count, int) and not isinstance(file_count, bool)
            else "checkpoint")
        item = _guidance_row(
            row.get("checkpoint_id"), detail, limit=100)
        if item is not None:
            checkpoint_rows.append(item)
    _append_guidance_rows(
        catalog, "/rewind", checkpoint_rows,
        hint="checkpoint id；选择后仍会显示确认范围")

    # Only corrupt lease records are valid repair targets.  Read the lease
    # directory once instead of calling ``session_lease`` for every session:
    # the latter creates a per-session lock file even for a missing lease and
    # would turn this best-effort UI poll into a write-heavy O(sessions) path.
    # ``active_session_leases`` is a read-only directory scan and deliberately
    # retains corrupt records so they can be offered to /sessions repair-lease.
    corrupt_rows = []
    try:
        leases = store.active_session_leases()
    except Exception:  # noqa: BLE001
        leases = {}
    for session_id, lease in (leases or {}).items():
        if isinstance(lease, dict) and lease.get("_corrupt"):
            item = _guidance_row(session_id, "corrupt lease", limit=100)
            if item is not None:
                corrupt_rows.append(item)
            if len(corrupt_rows) >= _GUIDANCE_DYNAMIC_LIMIT:
                break
    _append_guidance_rows(
        catalog, "/sessions", corrupt_rows, parent="repair-lease",
        hint="仅列出检测为 corrupt 的 lease")

    return catalog


def _guidance_signature(catalog):
    """Stable candidate-name signature used to avoid menu resets/redraw storms."""
    rows = []
    for command in sorted((catalog or {})):
        spec = catalog[command] or {}
        rows.append((
            command,
            tuple(str(name) for name, _ in spec.get("items") or ()),
            tuple(sorted((str(parent), tuple(str(name) for name, _ in (
                child or {}).get("items") or ()))
                         for parent, child in (spec.get("params") or {}).items())),
        ))
    return tuple(rows)

# Busy 输入分三类：后台控制、只读观察和只影响未来边界的 session 控制。
# 这里是唯一分类源，busy handler 与测试共用，避免命令又退回 queue。
LIVE_BACKGROUND_COMMANDS = frozenset({
    "agents", "consult", "workflow", "tasks", "task", "kill",
    "status", "trace",
})
LIVE_INSPECT_COMMANDS = frozenset({
    "usage", "cost", "context", "expand", "keys",
})
LIVE_CONTROL_COMMANDS = frozenset({
    # "mouse" 已随 1aaf879 从产品命令面删除（Persistent Workspace：不抓鼠标）；
    # 这里曾漏删，被 test_offered_set_equals_the_whitelist 抓到 —— 那条测试的
    # 用处正是逼两份清单保持一致。
    # "gateway" 于 2026-09-04 并入 /model gateway；/gateway 只是隐藏兼容别名，
    # 忙时菜单从 COMMANDS 生成，隐藏别名不在其中，白名单也不能有它。
    "model", "auto", "goal",
})
LIVE_COMMANDS = (
    LIVE_BACKGROUND_COMMANDS
    | LIVE_INSPECT_COMMANDS
    | LIVE_CONTROL_COMMANDS
)


def resolve_command(command):
    """把 exact alias/唯一前缀解析为 canonical registry entry。"""
    command = str(command or "").lower()
    exit_alias = command in ("exit", "quit", "q")
    canonical = (
        "exit" if exit_alias else COMMAND_ALIASES.get(command, command))
    entry = None if exit_alias else REGISTRY.get(canonical)
    if entry is None and not exit_alias:
        # Discovery is hidden in COMMANDS, but the parser keeps its historical
        # unique-prefix behavior so old callers (including `/ma`) continue to
        # resolve the compatibility command.
        names = set(REGISTRY) | set(COMMAND_ALIASES)
        hits = {
            COMMAND_ALIASES.get(name, name)
            for name in names if name.startswith(command)
        }
        if len(hits) == 1:
            canonical = next(iter(hits))
            entry = REGISTRY[canonical]
    return canonical, entry, exit_alias


def resolve_live_command(line):
    """Resolve one slash command that is safe to execute during a turn."""
    line = str(line or "")
    if not line.startswith("/"):
        return None
    command, _, rest = line[1:].partition(" ")
    canonical, entry, exit_alias = resolve_command(command.lower())
    if exit_alias or entry is None or canonical not in LIVE_COMMANDS:
        return None
    return canonical, entry[0], rest.strip()


def _switch_blocking_queue(controller):
    """Return only human-authored rows that must block new/resume/exit.

    A crash-safe goal continuation belongs to its saved session and is held by
    the closed internal dispatch gate.  It must remain recoverable there, but
    must not trap the user in that chat when they want to switch or exit.
    """
    return tuple(
        item for item in (getattr(controller, "queued", ()) or ())
        if getattr(item, "origin", "user") != "goal")


class _AppStdout:
    """应用模式下的 sys.stdout：可见文本进 transcript，纯控制序列（括号粘贴开关、OSC 标题、
    光标查询）原样透传给真终端。isatty/fileno 照实回答，别让 tui.supported() 之类误判。"""

    def __init__(self, renderer, real):
        self.renderer = renderer
        self.real = real
        self.encoding = getattr(real, "encoding", "utf-8")
        self.errors = getattr(real, "errors", "strict")

    def write(self, text):
        text = str(text)
        if not text:
            return 0
        if text.startswith("\x1b") and not tui._ANSI.sub("", text).strip("\r\n"):
            self.real.write(text)
        elif threading.get_ident() == self.renderer.owner_ident:
            self.renderer.write_output(text)
        else:
            self.renderer.post_output(text)     # 后台线程的 print：排队，主线程排干
        return len(text)

    def flush(self):
        self.real.flush()

    def isatty(self):
        return self.real.isatty()

    def fileno(self):
        return self.real.fileno()

    def __getattr__(self, name):
        return getattr(self.real, name)


def repl(sess, model):
    try:
        import readline
        hist = HOME / "history"
        HOME.mkdir(parents=True, exist_ok=True)
        try:
            readline.read_history_file(hist)
        except OSError:
            pass
        readline.set_history_length(2000)
        # 新历史以 history.jsonl 为唯一事实源。两个 terminal 若各自在退出时
        # 整体 write_history_file，会把另一个进程的新输入覆盖掉。
    except ImportError:
        pass

    # 目录过期就在后台抓一次：平台名单在变，等人想起来手动刷新是等不到的。
    # 之后每个 turn 边界还会再判一次（maybe_refresh_catalog），所以长会话也跟得上。
    sess.maybe_refresh_catalog()

    app_mode = bool(getattr(sess, "app_mode", False)) and tui.supported()
    caps = None
    if app_mode:
        from core import apprender, termcaps
        caps = termcaps.detect()
        if not (caps.tty and caps.alt_screen):
            app_mode = False              # 终端不认备用屏（或不应答查询）：回落内联，别在主屏上乱画
    if app_mode:
        renderer = apprender.AppRenderer(caps=caps)
        # 斜杠命令里的 print 不能绕过缓冲直接写进备用屏：可见文本进 transcript，纯控制序列透传
        sys.stdout = _AppStdout(renderer, sys.stdout)
    else:
        renderer = tui.TerminalRenderer()
    # 横幅的约束（「空闲」禁词、「/help 看命令」锚点）写在 _startup_banner 的 docstring 里。
    print("\n" + "\n".join(_startup_banner(sess, model)) + "\n")

    quit_requested = False

    def prompt():
        return sess.composer_prompt(busy=False)

    def record_line(line):
        store.append_history(line, session_id=sess.ag.session_id)

    def execute_command(line):
        renderer.clear_input()
        command, _, rest = line[1:].partition(" ")
        command, rest = command.lower(), rest.strip()
        canonical, entry, exit_alias = resolve_command(command)
        blocking_queue = _switch_blocking_queue(sess.controller)
        if canonical in {"new", "resume", "exit"} and blocking_queue:
            renderer.write_output(YELLOW(
                f"  [/{canonical} 暂缓：后面还有 "
                f"{len(blocking_queue)} 条用户 queue；"
                "先执行或在工作时按 Up 取回]\r\n"))
            return False
        if exit_alias:
            return True
        if entry is None:
            print(DIM(
                f"  未知命令 /{command}，/help 看列表"))
            return False
        return entry[0](sess, rest) == "quit"

    def run_actions(action):
        nonlocal quit_requested
        while not quit_requested:
            if action is None:
                late = getattr(sess, "_late_actions", None)
                if not late:
                    break
                action = late.pop(0)          # turn 边界上迟到的输入，按序补执行
            for item in sess.controller.drain_dispatched_inputs():
                display = sess.take_queue_display(item)
                if item.id in sess._skip_queue_display:
                    sess._skip_queue_display.discard(item.id)
                    continue
                renderer.commit_input(
                    sess.composer_prompt(busy=False), display)
            kind = action.kind
            if kind == C.ActionKind.START_TURN:
                turn_item = action.item
                sess.away_recap_turn(True)
                sess.memory_extract_turn(True)
                try:
                    action = sess.turn(turn_item.text, action=action)
                finally:
                    sess.away_recap_turn(False)
                    sess.memory_extract_turn(False)
                goal_notice = sess.goal_turn_finished(
                    turn_item, sess.last_turn_outcome)
                if goal_notice:
                    renderer.write_output(DIM(f"  [{goal_notice}]\r\n"),
                                          source="status")
                applied_route = sess.apply_pending_route()
                if applied_route:
                    renderer.write_output(DIM(
                        f"  [下一轮 route 已生效："
                        f"{applied_route['model']}@"
                        f"{applied_route['gateway']}]\r\n"),
                        source="status")
                sess.save()
            elif kind == C.ActionKind.RUN_COMMAND:
                active_controller = sess.controller
                quit_requested = execute_command(action.item.text)
                following = active_controller.finish_local_action()
                action = (
                    following
                    if sess.controller is active_controller else None)
            elif kind == C.ActionKind.RUN_SHELL:
                active_controller = sess.controller
                renderer.write_output(DIM(
                    "  [! shell queue 已保存，但执行属于 M3c TaskManager；"
                    "本版未执行该命令]\r\n"))
                active_controller.record_event(
                    "shell_not_executed",
                    {"text": action.item.text, "reason": "M3c pending"})
                action = active_controller.finish_local_action()
            else:
                raise RuntimeError(
                    f"REPL 收到无法执行的 action: {kind.value}")
        if not quit_requested:
            sess.refresh_composer(
                busy=False, prompt=prompt(), activity="空闲")

    def render_async_notices():
        task_notices = sess.drain_task_events()
        agent_notices = sess.drain_agent_events()
        workflow_notices = sess.drain_workflow_events()
        agent_notices.extend(sess.drain_catalog_events())
        projection_changed = sess._agent_projection_changed
        if (not task_notices and not agent_notices
                and not workflow_notices and not projection_changed):
            return False
        if task_notices or agent_notices or workflow_notices:
            renderer.clear_input()
            for notice in task_notices:
                renderer.write_output(
                    notice["text"], source=notice["source"])
            for notice in agent_notices:
                renderer.write_output(notice, source="status")
            for notice in workflow_notices:
                renderer.write_output(notice, source="status")
        snapshot = sess.refresh_composer(
            busy=False, prompt=prompt(),
            activity="空闲", publish=False)
        renderer.render(snapshot)
        sess.sync_composer_selection()
        return True

    def render_away_recap_ready():
        text = sess.take_away_recap()
        if not text:
            return False
        renderer.clear_input()
        renderer.write_output(render_away_recap(text), source="status")
        snapshot = sess.refresh_composer(
            busy=False, prompt=prompt(), activity="空闲", publish=False)
        renderer.render(snapshot)
        sess.sync_composer_selection()
        return True

    def render_memory_extract_ready():
        saved = sess.memory_extract_tick()
        if not saved:
            return False
        renderer.clear_input()
        renderer.write_output(render_memory_saved(saved), source="status")
        snapshot = sess.refresh_composer(
            busy=False, prompt=prompt(), activity="空闲", publish=False)
        renderer.render(snapshot)
        sess.sync_composer_selection()
        return True

    command_choices = dict(COMMANDS)
    command_choices.update(sess._custom_catalog.completion())
    # 工作中打 `/` 只列当场执行的命令。判据直接复用 resolve_live_command ——
    # 也就是分发器自己用的那个 —— 于是菜单不可能和实际行为各说各话。名单变了
    # 菜单自动跟着变，没有第二份需要同步的清单。
    live_choices = frozenset(
        name for name in command_choices
        if resolve_live_command(name) is not None)
    pump = tui.InputPump(
            prompt=prompt(), commands=command_choices,
            subcommands=COMMAND_SUBCOMMANDS, live_commands=live_choices,
            history=_history(
                session_id=sess.ag.session_id, messages=sess.ag.messages),
            cwd=sess._session_cwd)
    _trace = _startup_tracer()
    _trace("repl entry")
    sess.bind_interactive(pump, renderer)
    _trace("bind_interactive")
    sess._set_terminal_title(idle=True)
    _trace("set_terminal_title")
    sess.refresh_composer(
        busy=False, prompt=prompt(), activity="空闲", publish=False)
    _trace("post-refresh_composer")
    with pump:
        _trace("pump entered")
        if app_mode:
            renderer.enter()
            _trace("renderer.enter")
        pending_replay = getattr(sess, "_replay_pending", None)
        if pending_replay:
            sess._replay_pending = None
            replay_transcript(sess, pending_replay)      # 启动时 --resume：像从没退出过
        if getattr(sess, "_resume_recap_pending", False):
            sess.request_resume_recap()
        recovered = sess.controller.dispatch_ready()
        if recovered is not None:
            renderer.write_output(DIM(
                "  [恢复并 dispatch 上次未完成的 queue]\r\n"))
            run_actions(recovered)

        try:
            _trace("main-loop entry")
            renderer.enable_mouse()
            _trace("enable_mouse")
            while not quit_requested:
                event = sess._get_pump_event(0.2)
                if event is None:
                    if app_mode:
                        renderer.drain_posted()
                    sess.heartbeat_lease()
                    render_async_notices()
                    render_away_recap_ready()
                    render_memory_extract_ready()
                    workflow_action = sess.dispatch_workflow_ready()
                    if workflow_action is not None:
                        run_actions(workflow_action)
                    task_action = sess.dispatch_task_handoff_ready()
                    if task_action is not None:
                        run_actions(task_action)
                    goal_action = sess.dispatch_goal_ready()
                    if goal_action is not None:
                        run_actions(goal_action)
                    # A child/workflow may be created by another worker
                    # between lifecycle notices.  The throttled refresh keeps
                    # its IDs discoverable without polling stores on every
                    # keystroke or resetting the user's menu selection.
                    sess.refresh_command_guidance(publish=True)
                    continue
                # An async redraw may have invalidated a live selection before
                # this queued key arrived. Reconcile the routing flag first so
                # Ctrl-C cannot copy a stale range.
                sess.sync_composer_selection()
                if event.kind == "redraw":
                    renderer.render(event.snapshot)
                    sess.sync_composer_selection()
                elif event.kind in {
                        "history_scroll", "history_click", "history_mouse",
                        "history_copy", "history_close"}:
                    sess.handle_history_navigation(event)
                elif event.kind in {
                        "composer_mouse", "composer_copy",
                        "composer_clear_selection"}:
                    sess.handle_composer_selection(event)
                elif event.kind == "cycle_mode":
                    renderer.clear_input()
                    renderer.finish_output_line()
                    sess.cycle_permission_mode()
                    snapshot = sess.refresh_composer(
                        busy=False, prompt=prompt(),
                        activity="空闲", publish=False)
                    renderer.render(snapshot)
                elif event.kind == "expand_last":
                    renderer.clear_input()
                    renderer.finish_output_line()
                    sess.expand_last()
                    snapshot = sess.refresh_composer(
                        busy=False, prompt=prompt(),
                        activity="空闲", publish=False)
                    renderer.render(snapshot)
                elif event.kind == "agents":
                    open_agents_panel(sess)
                    snapshot = sess.refresh_composer(
                        busy=False, prompt=prompt(),
                        activity="空闲", publish=False)
                    renderer.render(snapshot)
                elif event.kind == "agent_attach":
                    try:
                        show_agent_preview(sess, event.value)
                        sess.attach_agent(event.value)
                    except (AGENTS.AgentRuntimeError, ValueError) as exc:
                        renderer.write_output(
                            RED(f"  [agents] {exc}\r\n"))
                    snapshot = sess.refresh_composer(
                        busy=False, prompt=prompt(),
                        activity="空闲", publish=False)
                    renderer.render(snapshot)
                elif event.kind == "agent_detach":
                    sess.detach_agent(publish=False)
                    snapshot = sess.refresh_composer(
                        busy=False, prompt=prompt(),
                        activity="空闲", publish=False)
                    renderer.render(snapshot)
                elif event.kind == "detach":
                    run_id = sess.detach_agent(
                        draft=event.text, publish=False)
                    if run_id:
                        renderer.write_output(
                            DIM(f"  [已 detach {run_id}]\r\n"))
                    snapshot = sess.refresh_composer(
                        busy=False, prompt=prompt(),
                        activity="空闲", publish=False)
                    renderer.render(snapshot)
                elif event.kind == "background":
                    try:
                        outcome, task = sess.handle_background_key()
                    except TASKS.TaskError:
                        renderer.bell()
                    else:
                        renderer.clear_input()
                        if outcome == "backgrounded":
                            renderer.write_output(DIM(
                                f"  [task {task.id} 已转后台]\r\n"))
                        else:
                            renderer.write_output(DIM(
                                f"  [已 detach task {task}]\r\n"))
                        snapshot = sess.refresh_composer(
                            busy=False, prompt=prompt(),
                            activity="空闲", publish=False)
                        renderer.render(snapshot)
                elif event.kind == "submit":
                    line = event.text.strip()
                    if line == "?":
                        line = "/keys"                  # SPEC-CC-parity G2
                    if not line:
                        continue
                    if sess.attached_agent_id and not line.startswith("/"):
                        renderer.commit_input(
                            event.value.get("prompt", ""), line)
                        try:
                            sess.send_agent(
                                sess.attached_agent_id, line)
                        except (AGENTS.AgentRuntimeError, ValueError) as exc:
                            renderer.write_output(RED(
                                f"  [agent direct message 失败] {exc}\r\n"))
                        sess.refresh_composer(
                            busy=False, prompt=prompt(), activity="空闲")
                    elif line.startswith("/"):
                        renderer.commit_input(
                            event.value.get("prompt", ""), line)
                        record_line(line)
                        try:
                            custom = sess.resolve_custom_command(line)
                        except CUSTOM_COMMANDS.CommandError as exc:
                            renderer.write_output(RED(
                                f"  [custom command] {exc}\r\n"))
                            sess.refresh_composer(
                                busy=False, prompt=prompt(), activity="空闲")
                            continue
                        if custom is not None:
                            _, expanded = custom
                            action = sess.controller.submit(
                                expanded, C.QueueMode.NEXT_TURN)
                            if action is None:
                                raise RuntimeError(
                                    "idle custom command 未 dispatch")
                            sess.remember_queue_display(
                                action.item, line, skip=True)
                            run_actions(action)
                        else:
                            quit_requested = execute_command(line)
                            if not quit_requested:
                                sess.refresh_composer(
                                    busy=False, prompt=prompt(), activity="空闲")
                    else:
                        record_line(line)
                        mode = (
                            C.QueueMode.SHELL if line.startswith("!")
                            else C.QueueMode.NEXT_TURN)
                        action = sess.controller.submit(line, mode)
                        run_actions(action)
                elif event.kind == "interrupt":
                    renderer.clear_input()
                    renderer.write_output("\r\n")
                elif event.kind == "eof":
                    _trace("EOF event received")
                    quit_requested = True
                elif event.kind == "error":
                    _trace("pump error event")
                    raise event.value
        finally:
            _trace("repl loop exited")
            if renderer.history_active:
                renderer.close_history(pump.snapshot())
            pump.set_history_mode(False)
            renderer.disable_mouse()
            if app_mode:
                renderer.exit()
                sys.stdout = getattr(sys.stdout, "real", sys.stdout)
            renderer.clear_input()
            renderer.clear_spinner()
            sess.close()
            for notice in sess.drain_task_events():
                renderer.write_output(
                    notice["text"], source=notice["source"])
            for notice in sess.drain_agent_events():
                renderer.write_output(notice, source="status")
            for notice in sess.drain_workflow_events():
                renderer.write_output(notice, source="status")
            sess._set_terminal_title(None)
            print()



_KEY_WARNED = False


class _SilenceWatch:
    """非交互（-p / 管道）模式没有状态栏：首字迟迟不来时往 stderr 说一声，别让人以为卡死。

    2026-09-04 实测：kimi-k3-256k 一次 `-p 好` 首字 102s，期间零输出；每次尝试最长 600s、
    还会退避重试，最坏是半小时的沉默。只在 stderr 是 TTY 时开口 —— 脚本管道保持干净。
    """
    FIRST = 8.0
    EVERY = 20.0

    def __init__(self, label, stream=None):
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        try:
            tty = bool(self.stream.isatty())
        except Exception:                                   # noqa: BLE001
            tty = False
        if tty:
            self._thread = threading.Thread(
                target=self._run, name="silence-watch", daemon=True)
            self._thread.start()
        return self

    def touch(self):
        self._last = time.monotonic()

    def stop(self):
        self._stop.set()

    def _run(self):
        next_at = self.FIRST
        seen = self._last
        while not self._stop.wait(0.25):
            if self._last != seen:          # 有事件：重新计时
                seen = self._last
                next_at = self.FIRST
                continue
            silent = time.monotonic() - self._last
            if silent >= next_at:
                self.stream.write(
                    f"  · 等 {self.label} 已 {int(silent)}s 没有响应"
                    "（单次尝试最长 600s，网关 5xx 会自动退避重试；Ctrl+C 放弃）\n")
                self.stream.flush()
                next_at += self.EVERY


def _write_key(path, name, value):
    """把 NAME=value 写进 keys.env：已有同名行就替换，否则追加；0600；不回显。"""
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f]
    lines = [ln for ln in lines if not ln.strip().startswith(name + "=")]
    lines.append(f"{name}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _init_portable(ok, bad):
    """自包含模式：建 <应用目录>/.zylab-home/，或把已有的 ~/.zylab 整体收编进去（活会话则拒绝）。"""
    from core import homemigrate
    if paths.env_get("HOME"):
        bad(f"设置了 {paths.env_name('HOME')}，与自包含模式冲突：先 unset 再跑 init --portable")
        return 2
    target = paths.portable_home()
    default = paths.default_home()
    if target.is_dir():
        ok(f"自包含状态目录已在：{target}")
    elif default.is_dir():
        rc = homemigrate.relocate(default, target, verb="已收编")
        if rc:
            return rc
        ok(f"自包含状态目录：{target}（原 {default} 已整体搬入）")
    else:
        target.mkdir(mode=0o700, parents=True)
        homemigrate.record_location(target)
        ok(f"已创建自包含状态目录 {target}")
    print(DIM(f"    整个 {paths.app_root()} 可整体拷贝；拷到别处后首次启动会自动把目录里记的绝对路径改到新位置"))
    return 0


_INIT_MODEL_SKIP = re.compile(
    r"embed|rerank|ocr|tts|whisper|audio|image|vision|vl\b|guard|moderation",
    re.I)


def _init_pick_candidates(rows, limit=4):
    """按「像不像能干活的主力模型」排候选：上下文大的优先，明显非对话的排除。

    按目录顺序取第一个会挑出按字母序最靠前的那个（实测挑中 Intern-S2-Preview-397B），
    对一个编码 agent 不合适。窗口大小是目录里唯一一个对所有网关都可比的信号。
    """
    scored = []
    for row in rows or ():
        name = str(row.get("id") or "").strip()
        if not name or _INIT_MODEL_SKIP.search(name):
            continue
        scored.append((-(row.get("max_model_len") or 0), name))
    scored.sort()
    return [name for _ctx, name in scored[:limit]]


def _init_pick_model(route, ids, ok, warn, *, limit=4):
    """从这把 key 的目录里挑一个真能答话的模型，写进 settings。

    只试前几个：init 是冷启动闸门，不是选型工具；试不出来就如实说、让人手动挑。
    """
    for candidate in ids[:limit]:
        try:
            list(client.stream_chat(
                candidate, [{"role": "user", "content": "只回复 ok"}],
                max_tokens=8, temperature=None, route=route, retries=1,
                thinking={"type": "disabled"}))
        except Exception:                                 # noqa: BLE001
            continue
        ok(f"改用 {candidate}（出厂默认那个这把 key 用不了）")
        try:
            CFG.write_user({"model": candidate, "gateway": route.name})
            ok(f"已写入 settings：model = {candidate}、gateway = {route.name}")
        except CFG.SettingsError as exc:
            warn(f"模型可用，但写入 settings 失败：{exc}")
        return candidate
    return None


def cmd_init_cli(a, cfg):
    """`zylab init`：冷启动闸门，每步失败都自带下一步动作（BACKLOG-rename-zylab §2.0.2）。

    非交互（--yes 或无 TTY）绝不 input()。退出码：0 全通过；1 网关探测失败；2 没拿到 key。
    复用现成件：key_status / _key_files / KEYS / list_models 与 client 的三分诊断，不重写。
    """
    import getpass
    import shutil
    interactive = sys.stdin.isatty() and not a.yes

    def ok(msg):
        print(f"  ✓ {msg}")

    def warn(msg):
        print(YELLOW(f"  ! {msg}"))

    def bad(msg):
        print(RED(f"  ✗ {msg}"))

    print(f"\n  {ORANGE('◆')} {BOLD('zylab init')}")
    if getattr(a, "portable", False):
        rc = _init_portable(ok, bad)
        if rc:
            return rc
        # 本进程的 store 常量在 import 时已绑到旧目录：去掉 --portable 重新执行自己，让后面的步骤用新目录
        argv = [os.path.abspath(sys.argv[0])] + [x for x in sys.argv[1:] if x != "--portable"]
        sys.stdout.flush()
        if wincompat.IS_WINDOWS:
            # **Windows 没有 exec 语义。** CPython 的 os.execv 在这里是「spawn 一个
            # 新进程，然后把自己以退出码 0 结束掉」—— 于是接力那一半的退出码
            # （比如「没有 endpoint → 2」）全部丢失，调用方只看到 0，脚本无法判断
            # init 到底成没成功。改成同步跑完再把退出码原样传出去。
            return subprocess.run([sys.executable] + argv).returncode
        os.execv(sys.executable, [sys.executable] + argv)
    # 1. Python（真正的闸门在文件顶部；能跑到这里就是过了）
    ok(f"Python {sys.version_info[0]}.{sys.version_info[1]}（{sys.executable}）")
    # 2. 终端能力：警告不中止
    term = os.environ.get("TERM") or ""
    size = shutil.get_terminal_size((0, 0))
    if not sys.stdin.isatty() or term in ("", "dumb"):
        warn(f"终端能力有限（TERM={term or '未设'}，stdin tty={sys.stdin.isatty()}）"
             "—— 交互界面可能画不全；单次模式 zylab -p '…' 不受影响")
    else:
        ok(f"终端 {term} {size.columns}×{size.lines}")
    # 3. 状态目录
    try:
        store.ensure_home()
        ok(f"状态目录 {store.HOME}" + ("（自包含：在应用目录里）" if paths.is_portable() else ""))
    except OSError as exc:
        bad(f"状态目录建不了：{store.HOME}（{exc}）")
        print(DIM(f"    看权限：namei -l {store.HOME}    或换个家目录：HOME=/some/writable zylab init"))
        return 1
    # 4. 网关 + key
    name = str(a.gateway or cfg.get("gateway") or client.GATEWAY).lower()
    try:
        route = client.route_for(name)
    except ValueError:
        # 未知网关不是死路：init 本来就是写 settings 的那一步，那就让它把 profile 建出来。
        # 以前这里直接 return 2，于是「clone 下来 → zylab init --gateway openai」
        # 第一条命令就撞墙，而要新增网关又得先手写 settings.json。
        base = str(getattr(a, "base", None) or "").strip()
        if not base and interactive:
            print(DIM(f"    没有内置的 {name} profile。给个 OpenAI 兼容的 endpoint "
                      "就地建一个（形如 https://<host>/v1）"))
            base = input(f"  {name} 的 endpoint: ").strip()
        if not base:
            bad(f"没有内置的 {name} profile，也没给 endpoint")
            print(DIM(f"    zylab init --gateway {name} --base https://<host>/v1"))
            print(DIM(f"    内置可选：{', '.join(sorted(client.GATEWAYS))}"))
            return 2
        keys = [a.key_env] if a.key_env else [
            re.sub(r"[^A-Z0-9]+", "_", name.upper()) + "_API_KEY"]
        CFG.write_user({"gateways": {name: {"base": base, "keys": keys}}})
        client.GATEWAYS[name] = {"base": base, "keys": tuple(keys),
                                 "note": "用户自建"}
        ok(f"已新建网关 profile {name} → {base}（key 变量 {keys[0]}）")
        route = client.route_for(name)
    # 4a. endpoint。公开厂商的地址内置、自建网关的不内置，所以这是 key 之前的一道闸 ——
    #     否则第一次提问才以 URLError 的形式暴露，读起来像网络坏了。
    base = str(getattr(a, "base", None) or "").strip() or client.resolve_base(route.name)
    if not base and interactive:
        print(DIM("    endpoint 形如 https://<host>/v1。zylab 只是客户端，"
                  "不预置也不转发任何网关地址"))
        base = input(f"  {route.name} 的 endpoint: ").strip()
    if not base:
        # 复用 client 那条提示：它会连「其实你可以直接挑一个地址已内置的网关」一起说。
        # 两处各写一份的后果实测过——这里只说「自己填地址」，新用户不知道还有别的路。
        bad(f"网关 {route.name} 没有 endpoint 地址")
        for line in client.base_missing_hint(route.name).splitlines()[1:]:
            print(DIM(f"  {line}"))
        return 2
    if base != client.resolve_base(route.name):
        CFG.write_user({"gateways": {route.name: {"base": base}}})
        ok(f"endpoint 已写入 {CFG.USER_FILE}（{route.name} → {base}）")
    else:
        ok(f"网关 {route.name} endpoint {base}")
    # 本进程的 GATEWAYS 是 import 时的快照，落盘后同步一下，后面的探测才用新地址。
    client.GATEWAYS.setdefault(route.name, {})["base"] = base
    route = client.route_for(route.name)
    status = client.key_status(route)
    if a.key_env:
        value = os.environ.get(a.key_env, "")
        if not value:
            bad(f"环境变量 {a.key_env} 是空的")
            print(DIM(f"    先 export {a.key_env}='<key>' 再跑；或不带 --key-env 交互输入"))
            return 2
        _write_key(client.KEYS, route.key_names[0], value)
        ok(f"key 已写入 {client.KEYS}（来源：环境变量 {a.key_env}；不回显）")
    elif status["found"]:
        ok(f"网关 {route.name} 已有 key（{status.get('source') or '文件'}）")
    elif interactive:
        value = getpass.getpass(f"  粘贴 {route.name} 的 key（不回显）: ").strip()
        if not value:
            bad("没有输入 key")
            return 2
        _write_key(client.KEYS, route.key_names[0], value)
        ok(f"key 已写入 {client.KEYS}（来源：交互输入；不回显）")
    else:
        bad(f"网关 {route.name} 没有 key，而且是非交互运行")
        print(DIM(f"    export {route.key_names[0]}='<key>'    或    "
                  f"zylab init --gateway {route.name} --key-env {route.key_names[0]} --yes"))
        print(DIM(f"    找过环境变量 {', '.join(route.key_names)}；"
                  f"找过文件 {', '.join(client._key_files())}"))
        return 2
    # 5. 一次真实探测：三种失败读起来不一样
    proxies = {k: v for k, v in os.environ.items()
               if k.lower() in ("http_proxy", "https_proxy", "all_proxy") and v}
    other = next((n for n in sorted(client.GATEWAYS) if n != route.name),
                 route.name)
    try:
        listing = client.list_models(route=route)
        n = len(listing)
        ok(f"网关 {route.name} 可达，目录里 {n} 个模型")
        # 立刻落盘。否则 init 说「可达、15 个模型」，而 models.json 要等到第一个
        # turn 边界才生成——「填了 key，模型列表就该跟着刷新」是这条命令的本分。
        try:
            changed = M.refresh_catalog(route.name)
            added = len(changed.get("added") or [])
            ok(f"模型目录已写入本地（新增 {added} 条）"
               if added else "模型目录已写入本地")
        except Exception as exc:                          # noqa: BLE001
            warn(f"目录探测成功，但写入本地失败：{type(exc).__name__}: {exc}")
    except client.MissingKey as exc:
        bad(str(exc))
        return 2
    except client.APIError as exc:
        bad(f"网关 {route.name} 拒绝了探测：{exc}")
        print(DIM("    （请求由 zylab 直连发出，不经过你 shell 里的代理变量）"))
        return 1
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        bad(f"网关 {route.name} 不可达：{exc} —— 与 key 无关")
        print(DIM(f"    直连 {client._display_url(route.base)}；"
                  f"本机代理变量：{proxies or '未设'}（zylab 直连，不走它们）"))
        print(DIM(f"    稍后重试，或换网关：zylab init --gateway {other}"))
        return 1
    # 6. 默认模型能不能答话 —— 目录里列得出 ≠ 这把 key 调得通。
    #    每把 key 开放的模型不一样，所以写死的默认值对别人不一定成立；
    #    2026-09-15 实测出厂默认 kimi-k3-256k 已 403，而 init 当时只探测目录，
    #    同事会看到「全部通过」然后第一次提问才失败。
    want = str(a.model or (cfg or {}).get("model") or A.default_model(route.name))
    try:
        answered = "".join(
            event["v"] for event in client.stream_chat(
                want, [{"role": "user", "content": "只回复 ok"}],
                max_tokens=8, temperature=None, route=route, retries=1,
                thinking={"type": "disabled"})
            if event["t"] == "text")
        ok(f"默认模型 {want} 可调用"
           + (f"（回复 {answered.strip()[:12]!r}）" if answered.strip() else ""))
        if a.model:
            try:
                CFG.write_user({"model": want})
                ok(f"已写入 settings：model = {want}")
            except CFG.SettingsError as exc:
                warn(f"模型可用，但写入 settings 失败：{exc}")
    except client.APIError as exc:
        bad(f"默认模型 {want} 这把 key 调不通：{exc}")
        try:
            ids = [str(row.get("id")) for row in client.list_models(route=route)]
        except Exception:                                  # noqa: BLE001
            ids = []
        # 换模型的命令由 client 的 model_unavailable 提示统一给出，这里只补
        # 它给不出的东西：这把 key 实际能选哪些。两处都印就成了重复文案。
        if ids:
            shown = "、".join(ids[:12]) + ("…" if len(ids) > 12 else "")
            print(DIM(f"    这把 key 的目录里有：{shown}"))
        # 用户没有指定 --model 时，别停在「你自己挑一个」：出厂默认是按维护者的 key
        # 定的，对别人本来就不成立。这里当场挑一个真能答话的写进 settings —— 这正是
        # 「下载下来填个 key 就能用」与「填完还得再查一轮文档」的分界。
        if a.model or not ids:
            return 2
        picked = _init_pick_model(
            route, _init_pick_candidates(listing) or ids, ok, warn)
        if picked is None:
            print(DIM("    都没答上来。交互模式 /model 手动挑，"
                      f"或 zylab init --gateway {route.name} --model <名字> --yes"))
            return 2
        want = picked
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        bad(f"探测默认模型时网关不可达：{exc} —— 与 key 无关")
        return 1

    # 7. 下一步
    # 替换字段里不能有反斜杠（同样是 3.12 才放开的 PEP 701），先算出来再插值。
    one_shot = BOLD('zylab -p "你好"')
    print(f"\n  下一步：{BOLD('zylab')}（交互）或 {one_shot}（单次）\n")
    return 0


def _warn_if_no_key(gateway=None):
    """启动即告知缺 key，而不是等第一次请求发出时才失败。

    转交内测时的实际形态：研究者装好、敲完 prompt、等了几秒，才被踹出来。
    这里只警告不退出 —— /model、/doctor、/resume 等本地命令不需要 key，
    他应该能先进去看看（配合 client.MissingKey 不再是 SystemExit）。
    """
    try:
        status = client.key_status(client.route_for(gateway))
    except Exception:                                   # noqa: BLE001
        return
    if status["found"]:
        return
    target = status["candidates"][0]
    global _KEY_WARNED
    _KEY_WARNED = True
    print(YELLOW(
        f"  ! 网关 {status['route']} 还没有 API key —— 模型请求会失败，"
        "本地命令（/doctor /model /resume）仍可用。"))
    print(DIM(f"    找过环境变量 {', '.join(status['names'])}"))
    print(DIM(f"    找过文件 {', '.join(status['candidates'])}"))
    print(DIM(f"    export {status['names'][0]}='<key>'   或写入 {target}"
              "（建议 chmod 600）"))
    # 只有另一个网关的 key 也很常见（boyue 可报销，deepinfer 免费），
    # 别让用户以为必须去申请当前这个。
    for other in ("boyue", "deepinfer"):
        if other == status["route"]:
            continue
        try:
            alt = client.key_status(client.route_for(other))
        except Exception:                               # noqa: BLE001
            continue
        if alt["found"]:
            print(DIM(f"    （检测到 {other} 的 key 可用 —— "
                      f"设置 settings.json 的 \"gateway\": \"{other}\" 即可换过去）"))
        else:
            print(DIM(f"    只有 {other} 的 key 也行："
                      f"export {alt['names'][0]}='<key>' 并把 settings.json 的 "
                      f"\"gateway\" 设为 \"{other}\""))
        break


def _startup_tracer():
    """ZYLAB_TRACE_STARTUP=1 时把启动序列执行点打到 stderr（诊断 Windows 退出用）。

    不设环境变量时是零开销 no-op，不影响任何正常路径。"""
    import os as _os
    if not _os.environ.get("ZYLAB_TRACE_STARTUP"):
        return lambda *a, **k: None
    import sys as _s
    import time as _t

    def _mark(label):
        print(f"[startup-trace] {_t.monotonic():.3f} {label}",
              file=_s.stderr, flush=True)
    return _mark


def _wincheck():
    """--wincheck：Windows 兼容层逐项自检（也可在 POSIX 上跑，对照行为）。

    每项独立 try/except，一项失败不挡后面；任何 traceback 都完整打印，
    退出码 = 失败项数。这是 win32 分支的第一手诊断出口——不再依赖「盲修往返」。
    """
    from core import wincompat
    failures = 0

    def check(name, fn):
        nonlocal failures
        try:
            detail = fn()
            print(f"  ok  {name}" + (f" · {detail}" if detail else ""))
        except BaseException as exc:  # 诊断模式：全都要打出来
            failures += 1
            import traceback
            print(f"  FAIL {name}: {exc!r}")
            traceback.print_exc()

    print("wincheck · 平台:", sys.platform)

    def _probe_raw():
        if not sys.stdin.isatty():
            return "stdin 非 tty，跳过"
        if wincompat.IS_WINDOWS and not wincompat.is_console(sys.stdin.fileno()):
            # Git Bash / MSYS 的 pty：isatty 为真但没有控制台模式可设，
            # raw_mode 在这里**应该**大声失败（core/tui.supported() 会先判掉）。
            return "stdin 是终端但非 Win32 控制台，raw 模式不适用，跳过"
        ctx = wincompat.raw_mode(0, cbreak=True, capture_signals=True)
        entered = ctx.__enter__()
        try:
            assert entered is ctx, "raw_mode __enter__ 未返回自身"
            return type(ctx).__name__
        finally:
            ctx.__exit__(None, None, None)

    check("raw_mode 进出对称", _probe_raw)

    def _probe_console_read():
        if not wincompat.IS_WINDOWS:
            return "POSIX 跳过（read_console 仅 win32）"
        import sys as _s
        if not wincompat.is_console(_s.stdin.fileno()):
            return "stdin 不是 Win32 控制台，跳过"
        # read_console 交的是 **bytes**；原来这里拿它和 str "" 比，
        # 于是这项自检在任何情况下都报红。
        timeout_result = wincompat.read_console(1, 0.05)
        assert timeout_result == b"", f"超时应返回空字节串，得到 {timeout_result!r}"
        return "超时返回空字节串（符合预期）"

    check("read_console 超时语义", _probe_console_read)

    def _probe_wait_fd():
        import os as _os
        r, w = _os.pipe()
        try:
            _os.write(w, b"x")
            assert wincompat.wait_fd(r, 0.5) is True, "有数据应可读"
            assert _os.read(r, 1) == b"x"
            assert wincompat.wait_fd(r, 0.05) is False, "读空后应超时 False"
            _os.close(w)
            assert wincompat.wait_fd(r, 0.05) is True, "EOF 可读（select 原语义，上层靠空读判 EOF）"
            assert _os.read(r, 1) == b""
            return "可读/超时/EOF 判定正确"
        finally:
            _os.close(r)

    check("wait_fd 管道语义", _probe_wait_fd)

    def _probe_ismode():
        if not wincompat.IS_WINDOWS:
            return "POSIX 跳过"
        # isatty() 不够：Git Bash / MSYS 的 pty 让它为真，但那不是 Win32 控制台，
        # GetConsoleMode 必失败。这里问的就是「是不是真控制台」本身。
        import sys as _s
        fd = _s.stdin.fileno()
        if wincompat.is_console(fd):
            return "stdin 是 Win32 控制台，fd->HANDLE 转换正确"
        if _s.stdin.isatty():
            return ("stdin 是终端但不是 Win32 控制台（Git Bash / MSYS pty）："
                    "整屏 TUI 会回落到行输入模式")
        return "stdin 非 tty（管道/重定向），跳过"

    check("_is_console fd->HANDLE", _probe_ismode)

    def _probe_pump():
        from core import tui as _tui
        if not _tui.supported():
            return "非交互 tty，跳过"
        pump = _tui.InputPump(prompt="")
        with pump:
            pass
        return "InputPump 进出（含 raw_mode + 线程启停）"

    check("InputPump 生命周期", _probe_pump)

    def _probe_repl_smoke():
        from core import tui as _tui
        if not _tui.supported():
            return "非交互 tty，跳过"
        # read_key 空闲 100ms 应返回 None（不抛、不退出）
        result = _tui.read_key(timeout=0.1)
        assert result is None or isinstance(result, str), f"read_key 返回 {result!r}"
        return f"read_key(0.1) -> {result!r}"

    check("read_key 空闲语义", _probe_repl_smoke)

    def _probe_termcaps():
        from core import termcaps as _ter
        caps = _ter.detect()
        return f"termcaps.detect OK · tty={caps.tty} cols={caps.cols} rows={caps.rows}"

    check("termcaps.detect", _probe_termcaps)

    def _probe_renderer():
        import sys as _s
        if not _s.stdin.isatty() or not _s.stdout.isatty():
            return "非交互 tty，跳过"
        from core import tui as _t
        r = _t.TerminalRenderer()
        # 只测渲染器的纯输出路径（with pump: 之后立刻执行的三件事），
        # render(Snap) 依赖真会话状态，不在探针里伪造。
        r.write_output("wincheck renderer probe", source="status")
        r.enable_mouse()
        return "write_output/enable_mouse OK"

    check("TerminalRenderer", _probe_renderer)

    print(f"wincheck 完成：{'全部通过' if failures == 0 else f'{failures} 项失败'}")
    return 1 if failures else 0


def main():
    # Windows 两件事必须在任何输出之前做：控制台按 ANSI 解释转义序列
    # （经典 conhost 默认不开，不开就是满屏可见的 ←[0m），以及把重定向出去的
    # stdout 改成 UTF-8（本地代码页编不出 ✓/─，会直接 UnicodeEncodeError）。
    # POSIX 上两个都是 no-op。
    wincompat.configure_stdio()
    wincompat.enable_vt_output()
    ap = argparse.ArgumentParser(prog="zylab", add_help=True)
    ap.add_argument("-p", "--print", metavar="PROMPT", help="单次执行后退出")
    ap.add_argument("--max-turns", type=int, default=None, metavar="N",
                    help="工具循环最多 N 轮后停下并给出 partial 结果；默认不设上限"
                         "（像 Claude Code），0 也表示不设上限。给 -p 无人值守用")
    ap.add_argument("--model", default=None)
    ap.add_argument("--models", action="store_true", help="列出可用模型")
    ap.add_argument("--version", action="store_true",
                    help="打印版本（报 bug 时贴这一行）")
    ap.add_argument("--resume", nargs="?", const="last", metavar="ID",
                    help="接上一次会话；给 id 则接指定会话")
    ap.add_argument(
        "--resume-cwd",
        choices=("current", "saved", "fork"),
        help=("cwd 不同时使用当前目录、切换到保存目录，或 fork 到当前目录"
              "（非 TTY 必需）"),
    )
    ap.add_argument("--sessions", action="store_true", help="列出保存的会话后退出")
    ap.add_argument("--probe-all", metavar="GATEWAY", nargs="?", const="both",
                    help="批量探测模型能力（deepinfer|boyue|both）")
    ap.add_argument("--probe-context", metavar="GATEWAY", nargs="?", const="boyue",
                    help="逐档探测上下文，直到明确拒绝或网关可测上限")
    ap.add_argument(
        "--no-md", action="store_true",
        help="不加载用户 ZYLAB.md 和项目 CLAUDE.md（兼容别名）")
    ap.add_argument(
        "--no-user-md", action="store_true",
        help=f"只禁用用户级 {paths.state_home()}/{paths.user_md_name()}")
    ap.add_argument(
        "--no-project-md", action="store_true",
        help="只禁用项目级 CLAUDE.md")
    ap.add_argument(
        "--no-skills", action="store_true",
        help="禁用渐进 skills 索引")
    ap.add_argument(
        "--yolo", action="store_true",
        help=("显式自动批准普通 ask 工具；"
              "不绕过 hard confirmation 或 deny"))
    ap.add_argument(
        "--allow-insecure-http", action="store_true",
        help=("显式允许本次进程向明文 HTTP provider 发送 key、prompt、"
              "代码与工具结果；非交互模式默认拒绝"))
    ap.add_argument("--usage", nargs="?", const="all", metavar="DAYS|SESSION",
                    help="汇总历史用量：--usage / --usage 7 / --usage <session前缀>")
    ap.add_argument("command", nargs="?", choices=["init"], metavar="init",
                    help="init：初始化 —— 选网关、收 key、探测一次（--gateway/--key-env/--yes 可非交互）")
    ap.add_argument("--gateway", default=None, metavar="NAME",
                    help="init 用：网关名（内置 deepinfer / boyue，也可自定义）")
    ap.add_argument("--base", default=None, metavar="URL",
                    help="init 用：网关 endpoint，形如 https://<host>/v1；写入 settings.json")
    ap.add_argument("--key-env", default=None, metavar="NAME",
                    help="init 用：从环境变量 NAME 取 key 写入 keys.env（不回显）")
    ap.add_argument("--yes", action="store_true",
                    help="init 用：不提问；无 TTY 时自动生效")
    ap.add_argument("--app", action="store_true",
                    help="全屏应用模式（可选）：备用屏里终端自己的滚动条无效、滚轮由 zylab 接管——"
                         "要像 Claude Code 那样用终端滚动条翻阅历史，用默认的内联模式")
    ap.add_argument("--terminal-caps", action="store_true",
                    help="强制重新探测终端能力（忽略缓存）并打印结果后退出；排查全屏模式回落/鼠标问题用")
    ap.add_argument("--wincheck", action="store_true",
                    help="Windows 兼容自检：逐项检查 raw mode/读键/管道等待，定位 win32 分支问题后退出")
    ap.add_argument("--inline", action="store_true",
                    help="内联模式（默认）：对话在终端自己的 scrollback 里，滚轮/滚动条/复制全是终端原生")
    ap.add_argument("--portable", action="store_true",
                    help="init 用：自包含——状态目录放进应用目录的 .zylab-home/，整个目录可整体拷贝；"
                         "家目录下已有的默认状态目录会被整体收编进去")
    a = ap.parse_args()
    # 每次 main() 都先回到 fail-closed；测试内重复调用 main 也不会继承
    # 上一次 invocation 的危险开关或 Session callback。
    client.configure_transport_policy(
        allow_insecure_http=a.allow_insecure_http, authorizer=None)

    # Probe/usage subcommands run before the interactive Session exists, so
    # install the effective M5 policy immediately. Configuration alone does
    # not create metrics.sqlite3; commands that query metrics open it on demand.
    cfg, cfg_src = CFG.load()
    transport = CFG.transport_policy(cfg)
    # Probe/catalog subcommands run before Session exists, so persistent exact
    # endpoint consent must already be active here. Session later rebinds the
    # same list together with its interactive one-session authorizer.
    client.configure_transport_policy(
        allow_insecure_http=a.allow_insecure_http, authorizer=None,
        allowed_insecure_endpoints=transport[
            "allowed_insecure_endpoints"])
    store.configure_metrics_policy(CFG.model_health_policy(cfg))

    if a.probe_context:
        gw = a.probe_context
        rows = [r for r in M.probed_rows(gw)
                if r.get("status") == "ok" and r.get("supports_tools")
                and not r.get("context")]
        print(
            f"  {gw}: {len(rows)} 个模型上下文未知；逐档探到明确拒绝或"
            " request-body 可测上限（每模型最多 9 档 / 约 7.2M 目标输入，"
            "网络重试另计）")
        for r in rows:
            try:
                with ContextProbeProgress() as progress:
                    lo, hi = M.probe_context(
                        gw, r["id"], on_progress=progress)
            except Exception as e:                       # noqa: BLE001
                print(f"    {r['id']:34} {RED('失败')} {str(e)[:50]}")
                continue
            record = M.get(gw, r["id"])
            state = record.get("context_probe_state")
            if state == "bounded" and hi:
                rejected = record.get("context_probe_rejected_at") or hi
                print(
                    f"    {r['id']:34} {GREEN(f'{int(lo or 0):>9,}')} tokens，"
                    f"{int(rejected):,} 档明确拒绝")
            elif state == "ceiling":
                print(
                    f"    {r['id']:34} {YELLOW(f'≥ {int(lo or 0):,}')} tokens，"
                    "模型顶未知（request-body ceiling）")
            elif state == "blocked":
                print(
                    f"    {r['id']:34} {RED('受阻')} "
                    f"{str(record.get('context_probe_error') or '')[:70]}")
            elif lo:
                bound = f"，{hi:,} 被拒" if hi else "（模型顶未确认）"
                print(f"    {r['id']:34} {GREEN(f'{lo:>9,}')} tokens{bound}")
            else:
                print(f"    {r['id']:34} {YELLOW('连最小档都不收')}")
        return

    if a.probe_all:
        gws = (["deepinfer", "boyue"] if a.probe_all == "both" else [a.probe_all])
        for gw in gws:
            print(f"\n  {BOLD(gw)} 抓目录…")
            try:
                n = M.fetch_catalog(gw)
                print(DIM(f"  {n} 个模型在册"))
            except RuntimeError as e:
                print(RED(f"  {e}")); continue

            def report(r, _gw=gw):
                if r.get("status") == "ok":
                    t = GREEN("工具") if r["supports_tools"] else RED("无工具")
                    tp = "" if r["supports_temperature"] else DIM(" 不收temp")
                    print(f"    {r['id']:36} {t} {r.get('first_token_s',0):>5.1f}s{tp}")
                else:
                    print(f"    {r['id']:36} {RED('不可用')} {DIM(str(r.get('error',''))[:56])}")
            ok, bad, sk = M.probe_many(gw, on_result=report)
            print(DIM(f"  → 可用 {ok} · 不可用 {bad} · 跳过 {sk}"))
        return

    if a.sessions:
        store.migrate_legacy()
        print_sessions(store.list_sessions(limit=30))
        return

    if a.usage:
        cmd_usage(None, "" if a.usage == "all" else a.usage)
        return

    if a.models:
        try:
            rows = [(m["id"], m.get("max_model_len") or 0,
                     m.get("supports_reasoning"), m.get("supports_image_in"))
                    for m in client.list_models()]
            source = None
        except (client.MissingKey, client.APIError,
                urllib.error.HTTPError, OSError) as exc:
            # 没 key、没配 endpoint、key 无效、网络不通 —— 都该能浏览模型，本地能力表
            # 就是为此存在的。如实标注来源，别拿缓存冒充实时目录。
            # （`base_not_configured` 以前没被接住：全新安装跑 `zylab --models`
            #   直接吐 Python traceback，那是陌生人见到的第一屏。）
            if getattr(exc, "kind", None) == "base_not_configured":
                # 连 endpoint 都没有 = 这个网关从没配过。本地能力表里那些行是
                # 出厂 seed（别人机器上的观测），列出来只会让人以为它们可用。
                print(RED(f"  {exc}"))
                return 2
            elif isinstance(exc, client.MissingKey):
                why = "没有 key"
            elif getattr(exc, "code", None) in (401, 403):
                st = client.key_status()
                where = st["source"] or "未知来源"
                print(RED(f"  网关拒绝了这个 key（HTTP {exc.code}）。"
                          f"当前 key 来自：{where}"))
                why = f"key 被拒 HTTP {exc.code}"
            else:
                print(RED(f"  查询网关目录失败：{type(exc).__name__}: {exc}"))
                why = f"{type(exc).__name__}"
            rows = [(r["id"], r.get("context") or 0,
                     r.get("supports_reasoning"), r.get("supports_image_in"))
                    for r in M.probed_rows(client.GATEWAY)
                    if r.get("status") == "ok"]
            source = f"本地能力表（{why}，未查询网关）"
        for model_id, ctx, reasoning, image in sorted(rows, key=lambda x: -x[1]):
            print(f"  {model_id:26}{ctx/1000:>7.0f}k"
                  f"{'  推理' if reasoning else ''}"
                  f"{'  图' if image else ''}")
        if source:
            print(DIM(f"  ({source})"))
        return

    if a.version:
        print(VERSION.render())
        return 0
    if a.terminal_caps:
        from core import termcaps as _termcaps
        caps = _termcaps.detect(force=True)
        for key, value in caps.as_dict().items():
            print(f"  {key}: {value}")
        print("  （已写入缓存；全屏模式需要 tty 与 alt_screen 为 True，鼠标需要 sgr_mouse）")
        return 0
    if a.wincheck:
        return _wincheck()
    if a.command == "init":
        return cmd_init_cli(a, cfg)
    store.ensure_home()
    if cfg.get("gateway"):
        try:
            client.set_gateway(cfg["gateway"])
        except ValueError as e:
            print(RED(f"  配置里的 gateway 无效：{e}"))
    model = a.model or cfg.get("model") or A.default_model()
    # 必须在 Agent 构造之前 —— 构造会走 set_model → model_limit → list_models，
    # 那是第一处真正需要 key 的地方。放在之后就永远来不及。
    _warn_if_no_key()
    ag = A.Agent(
        model=model,
        # The legacy aggregate remains true so --no-md is precisely the
        # documented alias for the two markdown layers, not for skills.
        load_md=True,
        load_user_md=not (a.no_md or a.no_user_md),
        load_project_md=not (a.no_md or a.no_project_md),
        load_skills=not a.no_skills,
        compact_at=cfg.get("compact_at"),
        gateway=client.GATEWAY)
    sess = Session(ag, cfg)
    if a.max_turns is not None:
        sess.max_turns = a.max_turns if a.max_turns > 0 else None
    sess.install_transport_policy(
        allow_insecure_http=a.allow_insecure_http)
    sess.cfg_sources = list(cfg_src)
    ag.confirm = sess.confirm
    if a.yolo:
        sess._auto_override = True
        sess.auto = True

    if a.resume:
        store.migrate_legacy()
        try:
            sid = a.resume if isinstance(a.resume, str) and a.resume != "last" \
                  else store.most_recent_id()
            if sid:
                resumed = resume_session_record(
                    sess,
                    {"id": sid},
                    cwd_policy=a.resume_cwd,
                    replay=bool(sys.stdin.isatty() and not a.print),
                    save_current=False,
                )
                if not resumed:
                    sess.ensure_current_lease()
            else:
                print(DIM("  (没有可接的会话，开新的)"))
                sess.ensure_current_lease()
        except (OSError, RuntimeError, ValueError,
                store.SessionStoreError) as e:
            print(RED(f"  {e}"))
            sess.close()
            return 1
    else:
        try:
            sess.ensure_current_lease()
        except store.SessionStoreError as exc:
            print(RED(f"  {exc}"))
            sess.close()
            return 1

    # --model 是显式 route：resume 后仍覆盖保存模型，并允许在 circuit open
    # 时带 warning 越过；配置文件里的 model 则仍按自动 route 处理。
    if a.model is not None:
        ag.set_model(a.model)
        ag.mark_route_explicit()

    if a.print:
        try:
            outcome = sess.turn(a.print)
            # -p 单次模式：主模型可能刚启动异步 workflow 就返回了。若在拿到
            # workflow id 后直接 close，driver 线程随进程死亡，workflow 变孤儿
            # paused。退出前阻塞等待本会话活跃 workflow 跑完（带超时兜底），
            # 让 driver 完成并落盘，而不是依赖事后 recover() 收养。
            try:
                active = [
                    row for row in sess.workflow_manager.list(
                        parent_session_id=sess.ag.session_id, limit=100)
                    if row.get("state") in WORKFLOWS.ACTIVE_STATES]
                if active:
                    ids = ", ".join(str(r.get("id")) for r in active)
                    print(YELLOW(
                        f"  等待 {len(active)} 个 workflow 完成：{ids}"))
                    sess.workflow_manager.wait(timeout=1800)
                    # 把完成事件 drain 出来，让最终报告落进 transcript。
                    sess.drain_workflow_events()
                    sess.save()
            except Exception as exc:  # noqa: BLE001
                print(YELLOW(f"  [workflow 等待异常：{exc}]"))
            sess.save()
        finally:
            sess.close()
        return int(getattr(outcome, "exit_code", 0) or 0)
    if not sys.stdin.isatty():
        try:
            outcome = sess.turn(sys.stdin.read())
            sess.save()
        finally:
            sess.close()
        return int(getattr(outcome, "exit_code", 0) or 0)
    sess.app_mode = bool(a.app) and not a.inline   # 内联是默认（Claude Code 模型）；--app 可选
    repl(sess, model)


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    except KeyboardInterrupt:
        print()
        raise SystemExit(130) from None
    except client.MissingKey as exc:
        # MissingKey 现在是普通异常（好让可选路径能吞掉它并降级），
        # 所以真正需要 key 的路径要在这里收口 —— 给提示，不给 traceback。
        # 启动时已经完整提示过就只留一行，别把同一段补救说两遍。
        if _KEY_WARNED:
            print(RED("  没有 key，无法发起模型请求（补救见上方提示）。"))
        else:
            print(RED(f"  {exc}"))
        raise SystemExit(1) from None
