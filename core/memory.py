"""可审计的分层 memory 与子代理 Context Capsule。

设计目标不是把 chat transcript 再复制一份，而是提供三个明确边界：

* canonical transcript 仍是事实权威；memory 只保存带来源的派生交接或用户显式条目；
* global/project 两层独立，项目层按真实 repo/cwd 身份隔离；
* 给 child/workflow 的 capsule 有长度上限、来源摘要和内容哈希，绝不偷偷复制整段历史。

本模块不调用 provider。自动 capture 只从已经落盘的会话状态生成确定性 handoff，
因此不会为了“记忆”额外消费 API，也不会在退出时卡住终端。
"""
from __future__ import annotations
from . import paths

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import (context as context_projection, goals, store,
               tasks as task_runtime)


VERSION = 1
ROOT = store.HOME / "memory"
GLOBAL = "global"
PROJECT = "project"
SCOPES = {GLOBAL, PROJECT}
MAX_ENTRY_CHARS = 12_000
MAX_TITLE_CHARS = 120
MAX_STABLE_KEY_CHARS = 160
DEFAULT_INDEX_CHARS = 25_000
DEFAULT_INDEX_ENTRIES = 24
MAX_CAPSULE_CHARS = 32_000
MAX_CAPSULE_WIRE_CHARS = 100_000
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_ID = re.compile(r"^m-[a-f0-9]{12}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+\-/=]{12,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)"
        r"(\s*[:=]\s*)['\"]?([^\s'\"]{8,})"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


class MemoryError(RuntimeError):
    """Memory authority or caller input is unsafe."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _is_internal_goal_message(message):
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and goals.is_round_prompt(message.get("content"))
    )


def _under_protected(path):
    return paths.under_protected(path)


def _private(path, mode=0o600):
    try:
        target = Path(path)
        if not target.is_symlink():
            os.chmod(target, mode)
    except OSError:
        pass


def redact(value):
    """Best-effort secret redaction before derived text reaches memory."""
    text = task_runtime.redact_known_secrets(str(value or ""))
    text = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", text)
    text = _SECRET_PATTERNS[1].sub(r"\1\2[REDACTED]", text)
    text = _SECRET_PATTERNS[2].sub("[REDACTED]", text)
    return text


def project_identity(cwd=None):
    """Stable project key plus human-readable, non-secret provenance."""
    selected = os.path.abspath(os.path.expanduser(str(cwd or os.getcwd())))
    try:
        repo = store.repo_context(selected).get("repo_root")
    except Exception:
        repo = None
    authority = os.path.realpath(repo or selected)
    digest = hashlib.sha256(authority.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return {
        "key": f"project-{digest}",
        "root": authority,
        "kind": "repo" if repo else "cwd",
    }


def _entry_id(scope, title, content, source_session=""):
    material = "\0".join((scope, title, content, str(source_session or "")))
    return "m-" + hashlib.sha256(material.encode(
        "utf-8", "surrogatepass")).hexdigest()[:12]


class MemoryStore:
    """Atomic JSON authority for global/project memory entries."""

    def __init__(self, root=None):
        self.root = Path(root or ROOT)
        if _under_protected(self.root):
            raise MemoryError(
                f"memory runtime 不能写入受保护路径：{self.root}")

    def _ensure_root(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise MemoryError(f"memory root 不能是符号链接：{self.root}")
        _private(self.root, 0o700)

    def _scope_path(self, scope, cwd=None):
        scope = str(scope or "").strip().lower()
        if scope not in SCOPES:
            raise MemoryError("memory scope 必须是 global 或 project")
        if scope == GLOBAL:
            return self.root / "global.json", None
        identity = project_identity(cwd)
        return self.root / "projects" / f"{identity['key']}.json", identity

    @staticmethod
    def _empty(scope, identity=None):
        return {
            "version": VERSION,
            "scope": scope,
            "project": identity,
            "updated_at": None,
            "entries": [],
        }

    def _read(self, path, scope, identity=None):
        if path.is_symlink():
            raise MemoryError(f"memory authority 不能是符号链接：{path}")
        if not path.is_file():
            return self._empty(scope, identity)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryError(
                f"memory authority 损坏：{type(exc).__name__}: {exc}") from None
        if (not isinstance(value, dict)
                or value.get("version") != VERSION
                or value.get("scope") != scope
                or not isinstance(value.get("entries"), list)):
            raise MemoryError(f"memory authority schema 无效：{path}")
        if scope == PROJECT:
            project = value.get("project")
            if (not isinstance(project, dict)
                    or project.get("key") != (identity or {}).get("key")
                    or project.get("root") != (identity or {}).get("root")):
                raise MemoryError(f"memory project identity 不匹配：{path}")
        seen = set()
        for index, entry in enumerate(value["entries"]):
            if (not isinstance(entry, dict)
                    or not _ID.fullmatch(str(entry.get("id") or ""))
                    or not isinstance(entry.get("title"), str)
                    or not isinstance(entry.get("content"), str)
                    or len(entry.get("content") or "") > MAX_ENTRY_CHARS
                    or not isinstance(entry.get("evidence") or {}, dict)):
                raise MemoryError(
                    f"memory entry schema 无效：{path} entries[{index}]")
            if entry["id"] in seen:
                raise MemoryError(
                    f"memory entry id 重复：{path} {entry['id']}")
            seen.add(entry["id"])
        return value

    def _write(self, path, value):
        self._ensure_root()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink():
            raise MemoryError(f"memory authority 不能经过符号链接：{path}")
        _private(path.parent, 0o700)
        store._atomic_write_text(
            path, json.dumps(value, ensure_ascii=False,
                             separators=(",", ":"), sort_keys=True))
        _private(path)

    def list(self, *, scope="all", cwd=None, include_session=True):
        scopes = (GLOBAL, PROJECT) if scope == "all" else (scope,)
        rows = []
        for selected in scopes:
            path, identity = self._scope_path(selected, cwd)
            value = self._read(path, selected, identity)
            for raw in value["entries"]:
                if not isinstance(raw, dict) or not _ID.fullmatch(
                        str(raw.get("id") or "")):
                    continue
                if not include_session and raw.get("kind") == "session_handoff":
                    continue
                rows.append({**_copy(raw), "scope": selected})
        rows.sort(key=lambda item: (
            item.get("kind") != "explicit",
            str(item.get("updated_at") or "")), reverse=False)
        # Explicit entries keep insertion semantics; handoffs are newest first.
        explicit = [row for row in rows if row.get("kind") == "explicit"]
        handoffs = sorted(
            [row for row in rows if row.get("kind") != "explicit"],
            key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return explicit + handoffs

    def add(self, content, *, title=None, scope=PROJECT, cwd=None,
            source_session=None, evidence=None, kind="explicit",
            stable_key=None):
        text = redact(str(content or "").strip())
        if not text:
            raise MemoryError("memory 内容不能为空")
        if len(text) > MAX_ENTRY_CHARS:
            raise MemoryError(
                f"memory 单条最多 {MAX_ENTRY_CHARS:,} 字符")
        selected_title = redact(str(title or "").strip())
        if not selected_title:
            selected_title = " ".join(text.split())[:80]
        selected_title = selected_title[:MAX_TITLE_CHARS]
        selected_stable_key = (
            " ".join(redact(str(stable_key or "").strip()).split()) or None)
        if (selected_stable_key is not None
                and len(selected_stable_key) > MAX_STABLE_KEY_CHARS):
            raise MemoryError(
                f"memory stable_key 最多 {MAX_STABLE_KEY_CHARS} 字符")
        path, identity = self._scope_path(scope, cwd)
        now = _now()
        source_session = str(source_session or "")
        identifier = _entry_id(
            scope, selected_stable_key or selected_title,
            "" if selected_stable_key else text,
            selected_stable_key or source_session)
        with store._exclusive_file_lock(path):
            authority = self._read(path, scope, identity)
            match = None
            if selected_stable_key:
                match = next((item for item in authority["entries"]
                              if item.get("stable_key")
                              == selected_stable_key), None)
            if match is None:
                match = next((item for item in authority["entries"]
                              if item.get("id") == identifier), None)
            created = match.get("created_at") if match else now
            record = {
                "id": identifier,
                "kind": str(kind or "explicit"),
                "stable_key": selected_stable_key,
                "title": selected_title,
                "content": text,
                "source_session": source_session or None,
                "evidence": _copy(evidence or {}),
                "created_at": created,
                "updated_at": now,
            }
            if match is None:
                authority["entries"].append(record)
            else:
                match.clear()
                match.update(record)
            authority["updated_at"] = now
            self._write(path, authority)
        return {**_copy(record), "scope": scope}

    def get(self, identifier, *, cwd=None):
        hint = str(identifier or "").strip()
        matches = [row for row in self.list(cwd=cwd)
                   if row["id"] == hint or row["id"].startswith(hint)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MemoryError(f"没有 memory {hint!r}")
        raise MemoryError(f"memory 前缀 {hint!r} 匹配 {len(matches)} 项")

    def remove(self, identifier, *, cwd=None):
        record = self.get(identifier, cwd=cwd)
        path, identity = self._scope_path(record["scope"], cwd)
        with store._exclusive_file_lock(path):
            authority = self._read(path, record["scope"], identity)
            before = len(authority["entries"])
            authority["entries"] = [
                item for item in authority["entries"]
                if item.get("id") != record["id"]]
            if len(authority["entries"]) == before:
                raise MemoryError(f"memory {record['id']} 已不存在")
            authority["updated_at"] = _now()
            self._write(path, authority)
        return record

    def render_index(self, *, cwd=None, max_chars=DEFAULT_INDEX_CHARS,
                     max_entries=DEFAULT_INDEX_ENTRIES):
        max_chars = max(0, min(100_000, int(max_chars)))
        max_entries = max(0, min(100, int(max_entries)))
        all_rows = self.list(cwd=cwd)
        rows = all_rows[:max_entries]
        blocks = []
        included = []
        for row in rows:
            stable_key = (
                f" · stable_key={row['stable_key']}"
                if row.get("stable_key") else "")
            block = (
                f"## {row['id']} · {row['scope']} · {row.get('title') or '?'}\n"
                f"source_session={row.get('source_session') or 'explicit'} · "
                f"updated={row.get('updated_at') or '?'}{stable_key}\n"
                + str(row.get("content") or "").strip())
            separator = "\n\n" if blocks else ""
            used = len(separator.join(blocks))
            remaining = max_chars - used - len(separator)
            if len(block) > remaining:
                marker = "\n…[memory index budget reached]"
                if remaining >= 240 + len(marker):
                    block = block[:remaining - len(marker)] + marker
                else:
                    break
            blocks.append(block)
            included.append(row["id"])
            if len("\n\n".join(blocks)) >= max_chars:
                break
        text = "\n\n".join(blocks)
        if len(text) > max_chars:
            raise MemoryError("memory index 内部预算计算错误")
        return {
            "text": text,
            "entries": included,
            "chars": len(text),
            "available": len(all_rows),
            "sha256": hashlib.sha256(
                text.encode("utf-8", "surrogatepass")).hexdigest(),
        }

    def capture_session(self, agent, *, title=None, cwd=None,
                        task_plan=None):
        """Upsert one project-scoped, source-bound deterministic handoff."""
        messages = list(getattr(agent, "messages", ()) or ())
        users = [str(item.get("content") or "").strip()
                 for item in messages
                 if item.get("role") == "user"
                 and not _is_internal_goal_message(item)
                 and isinstance(item.get("content"), str)
                 and str(item.get("content") or "").strip()]
        assistants = [str(item.get("content") or "").strip()
                      for item in messages
                      if item.get("role") == "assistant"
                      and isinstance(item.get("content"), str)
                      and str(item.get("content") or "").strip()]
        summary = getattr(agent, "context_summary", None) or {}
        blocks = []
        if summary.get("status") == "valid" and summary.get("content"):
            blocks.append("## Compact handoff\n" + str(summary["content"])[:8000])
        if users:
            blocks.append("## Latest user objective\n" + users[-1][:2500])
        if assistants:
            blocks.append("## Latest reported result\n" + assistants[-1][:2500])
        if isinstance(task_plan, dict):
            pending = [item for item in task_plan.get("items") or []
                       if item.get("status") != "completed"]
            if pending:
                blocks.append("## Pending plan\n" + "\n".join(
                    f"- [{item.get('status')}] {item.get('content')}"
                    for item in pending[:20]))
        if not blocks:
            return None
        content = "\n\n".join(blocks)[:MAX_ENTRY_CHARS]
        raw_sha = context_projection.sha256(messages)
        session_id = str(getattr(agent, "session_id", ""))
        return self.add(
            content,
            title=title or (users[0][:80] if users else f"session {session_id[:8]}"),
            scope=PROJECT,
            cwd=cwd,
            source_session=session_id,
            evidence={
                "raw_sha256": raw_sha,
                "message_count": len(messages),
                "summary_sha256": summary.get("covered_sha256"),
            },
            kind="session_handoff",
            stable_key=f"session:{session_id}",
        )


def _recent_user_messages(agent, limit=4, chars=5000):
    values = []
    for message in reversed(list(getattr(agent, "messages", ()) or ())):
        if message.get("role") != "user":
            continue
        if _is_internal_goal_message(message):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        values.append(content.strip())
        if len(values) >= limit:
            break
    values.reverse()
    joined = "\n\n".join(values)
    return joined[-chars:]


def build_capsule(agent, *, goal="", task_plan=None, cwd=None,
                  memory_index=None, max_chars=MAX_CAPSULE_CHARS):
    """Build a bounded, inspectable handoff for child/workflow agents."""
    selected_cwd = os.path.abspath(str(cwd or os.getcwd()))
    repo = store.repo_context(selected_cwd)
    summary = getattr(agent, "context_summary", None) or {}
    plan_rows = []
    if isinstance(task_plan, dict):
        plan_rows = [{
            "content": redact(str(item.get("content") or ""))[:500],
            "status": str(item.get("status") or "pending"),
        } for item in (task_plan.get("items") or [])[:30]]
    instructions = []
    for label, value in (
            ("user", getattr(agent, "memory_user_instructions", "")),
            ("project", getattr(agent, "memory_project_instructions", ""))):
        if value:
            instructions.append({
                "scope": label,
                "sha256": hashlib.sha256(str(value).encode(
                    "utf-8", "surrogatepass")).hexdigest(),
                "content": redact(str(value))[:6000],
            })
    capsule = {
        "version": VERSION,
        "generated_from": "deterministic-local-v1",
        "source_session": str(getattr(agent, "session_id", "")),
        "source_raw_sha256": context_projection.sha256(
            list(getattr(agent, "messages", ()) or ())),
        "goal": redact(str(goal or "").strip())[:4000],
        "environment": {
            "cwd": selected_cwd,
            "repo_root": repo.get("repo_root"),
            "git_branch": repo.get("git_branch"),
        },
        "task_plan": plan_rows,
        "compact_summary": {
            "status": summary.get("status") or "none",
            "covered_sha256": summary.get("covered_sha256"),
            "content": redact(str(summary.get("content") or ""))[:8000],
        },
        "recent_user_messages": redact(_recent_user_messages(agent)),
        "instructions": instructions,
        "memory": {
            "entries": list((memory_index or {}).get("entries") or []),
            "sha256": (memory_index or {}).get("sha256"),
            "content": redact(str((memory_index or {}).get("text") or ""))[:8000],
        },
    }
    # Bound by shrinking lowest-priority derived sections in a deterministic order.
    maximum = max(4000, min(MAX_CAPSULE_WIRE_CHARS, int(max_chars)))
    # sha256 + chars are appended after content hashing; reserve their exact
    # worst-case envelope so the final serialized capsule also respects max.
    payload_maximum = maximum - 128
    encoded = json.dumps(capsule, ensure_ascii=False, sort_keys=True)
    if len(encoded) > payload_maximum:
        capsule["memory"]["content"] = capsule["memory"]["content"][:2000]
        capsule["compact_summary"]["content"] = capsule[
            "compact_summary"]["content"][:4000]
        capsule["recent_user_messages"] = capsule[
            "recent_user_messages"][-2500:]
        for item in capsule["instructions"]:
            item["content"] = item["content"][:2000]
        encoded = json.dumps(capsule, ensure_ascii=False, sort_keys=True)
    if len(encoded) > payload_maximum:
        capsule["truncated"] = True
        capsule["task_plan"] = capsule["task_plan"][:10]
        encoded = json.dumps(capsule, ensure_ascii=False, sort_keys=True)
    if len(encoded) > payload_maximum:
        # Preserve identity and digests; derived prose is the first thing to go.
        capsule["compact_summary"]["content"] = ""
        capsule["recent_user_messages"] = ""
        capsule["memory"]["content"] = ""
        capsule["instructions"] = [{
            "scope": item["scope"], "sha256": item["sha256"],
            "content": "",
        } for item in capsule["instructions"]]
        capsule["task_plan"] = []
        capsule["goal"] = capsule["goal"][:1000]
        encoded = json.dumps(capsule, ensure_ascii=False, sort_keys=True)
    if len(encoded) > payload_maximum:
        capsule["goal"] = capsule["goal"][:200]
        encoded = json.dumps(capsule, ensure_ascii=False, sort_keys=True)
    if len(encoded) > payload_maximum:
        raise MemoryError(
            f"Context Capsule 元数据本身超过预算："
            f"{len(encoded)} > {payload_maximum}")
    capsule["sha256"] = hashlib.sha256(
        encoded.encode("utf-8", "surrogatepass")).hexdigest()
    capsule["chars"] = 0
    for _ in range(4):
        actual = len(json.dumps(capsule, ensure_ascii=False, sort_keys=True))
        if capsule["chars"] == actual:
            break
        capsule["chars"] = actual
    if capsule["chars"] > maximum:
        raise MemoryError(
            f"Context Capsule 最终大小超过预算："
            f"{capsule['chars']} > {maximum}")
    return capsule


def validate_capsule(capsule):
    """Verify a persisted capsule before it is injected into another agent."""
    if not isinstance(capsule, dict) or capsule.get("version") != VERSION:
        raise MemoryError("Context Capsule schema/version 无效")
    value = _copy(capsule)
    digest = str(value.pop("sha256", ""))
    claimed_chars = value.pop("chars", None)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    actual_digest = hashlib.sha256(
        encoded.encode("utf-8", "surrogatepass")).hexdigest()
    if digest != actual_digest:
        raise MemoryError("Context Capsule sha256 不匹配")
    full_chars = len(json.dumps(capsule, ensure_ascii=False, sort_keys=True))
    # Capsules created before deterministic-local-v1 stored payload chars;
    # accept that verified legacy representation without weakening the hash.
    if claimed_chars not in (len(encoded), full_chars):
        raise MemoryError("Context Capsule chars 不匹配")
    if full_chars > MAX_CAPSULE_WIRE_CHARS:
        raise MemoryError(
            f"Context Capsule 超过 wire 上限："
            f"{full_chars} > {MAX_CAPSULE_WIRE_CHARS}")
    if not isinstance(capsule.get("source_session"), str):
        raise MemoryError("Context Capsule source_session 无效")
    return _copy(capsule)


def render_capsule(capsule):
    if not isinstance(capsule, dict):
        return ""
    verified = validate_capsule(capsule)
    body = json.dumps(verified, ensure_ascii=False, sort_keys=True, indent=2)
    return (
        "<context-capsule authority=\"derived-read-only\" "
        f"sha256=\"{capsule.get('sha256') or '?'}\">\n"
        "这是一份来自父会话的有界交接。它用于线索和约束，不替代仓库实证；"
        "发现冲突时以当前文件、工具结果和上级指令为准。\n"
        + body + "\n</context-capsule>"
    )
