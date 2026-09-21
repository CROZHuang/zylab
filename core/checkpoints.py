"""Prepared file writes and durable M4 checkpoints.

The approval UI and the checkpoint store consume the same immutable
``PreparedWrite``.  Preparing reads the target once, renders a bounded local
preview, and captures the exact bytes/identity that execution must revalidate.

This module deliberately covers only ``write_file`` and ``edit_file``.  Bash
and external processes remain outside checkpoint coverage.
"""

from __future__ import annotations
from . import paths
from . import wincompat

import difflib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


PROTECTED_PATHS = tuple(paths.protected_paths())
MANIFEST_VERSION = 1
RESTORE_TRANSACTION_VERSION = 1
DEFAULT_PREVIEW_LINES = 80
DEFAULT_CONTEXT_LINES = 3
DEFAULT_LINE_CHARS = 320
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INCOMPLETE_RESTORE_STATUSES = frozenset({
    "prepared", "mutating", "recovering", "recovery_conflict",
})
CHECKPOINT_METADATA_SCAN_LIMIT = 4096


class CheckpointError(RuntimeError):
    """Base error for preparation, capture, execution, and restore."""


class UnsafePathError(CheckpointError):
    """The target is protected, outside the workspace, or symlinked."""


class PreparationError(CheckpointError):
    """Final post-hook tool arguments cannot form a prepared write."""


class InvalidEditError(PreparationError):
    """An edit has zero or ambiguous matches and must not execute."""


class ExternalChangeError(CheckpointError):
    """The file changed after the approval preview was prepared."""


class ManifestError(CheckpointError):
    """The durable checkpoint authority could not be read or updated."""


class RestoreConflict(CheckpointError):
    """A restore would overwrite an external or otherwise unknown change."""


class PartialRestoreError(RestoreConflict):
    """A restore failed and one or more compensating rollbacks also failed."""

    def __init__(self, message: str, *, details: Mapping[str, Any]):
        super().__init__(message)
        self.details = dict(details)


class AtomicReplaceError(CheckpointError):
    """A dirfd replace failed with an explicit commit boundary."""

    def __init__(self, message: str, *, committed: bool):
        super().__init__(message)
        self.committed = bool(committed)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mode(value: os.stat_result) -> int:
    return stat.S_IMODE(value.st_mode)


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _normalise_abs(value: os.PathLike[str] | str, *, base: str) -> str:
    raw = os.path.expanduser(os.fspath(value))
    if not raw or "\x00" in raw:
        raise UnsafePathError("文件路径为空或包含 NUL")
    if not os.path.isabs(raw):
        raw = os.path.join(base, raw)
    return os.path.normpath(os.path.abspath(raw))


def _check_protected(path: str, protected_paths: Iterable[str]) -> None:
    for protected in protected_paths:
        root = os.path.normpath(os.path.abspath(os.path.expanduser(protected)))
        if _is_within(path, root):
            raise UnsafePathError(f"拒绝写入受保护路径: {path}（{root} 只读）")


@dataclass(frozen=True)
class Workspace:
    canonical: str
    resolved: str
    device: int
    inode: int


def _workspace(value: os.PathLike[str] | str,
               protected_paths: tuple[str, ...]) -> Workspace:
    canonical = _normalise_abs(value, base=os.getcwd())
    _check_protected(canonical, protected_paths)
    try:
        lst = os.lstat(canonical)
    except OSError as exc:
        raise UnsafePathError(f"workspace 不可访问: {canonical}: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode):
        raise UnsafePathError(f"workspace 不能是符号链接: {canonical}")
    if not stat.S_ISDIR(lst.st_mode):
        raise UnsafePathError(f"workspace 不是目录: {canonical}")
    resolved = os.path.realpath(canonical)
    _check_protected(resolved, protected_paths)
    st = os.stat(canonical, follow_symlinks=False)
    return Workspace(canonical, resolved, st.st_dev, st.st_ino)


def _reject_symlink_components(path: str, workspace: Workspace) -> None:
    """Reject symlinks below the already-validated workspace root."""
    relative = os.path.relpath(path, workspace.canonical)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise UnsafePathError(f"目标越出 workspace: {path}")
    current = workspace.canonical
    for component in Path(relative).parts:
        if component in ("", "."):
            continue
        current = os.path.join(current, component)
        try:
            lst = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise UnsafePathError(f"无法检查目标路径: {current}: {exc}") from exc
        if stat.S_ISLNK(lst.st_mode):
            raise UnsafePathError(f"目标路径含符号链接，拒绝写入: {current}")


@dataclass(frozen=True)
class PathIdentity:
    canonical_path: str
    resolved_path: str
    existed: bool
    device: Optional[int]
    inode: Optional[int]
    size: Optional[int]
    mtime_ns: Optional[int]
    parent_resolved: str
    parent_device: int
    parent_inode: int


@dataclass(frozen=True)
class _Snapshot:
    identity: PathIdentity
    data: Optional[bytes]
    mode: Optional[int]


def _read_regular_once(path: str, first_lstat: os.stat_result) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UnsafePathError(f"无法安全打开目标文件: {path}: {exc}") from exc
    try:
        before = wincompat.fstat(fd)
        if (before.st_dev, before.st_ino) != (first_lstat.st_dev, first_lstat.st_ino):
            raise ExternalChangeError(f"读取前目标身份已变化: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = wincompat.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = wincompat.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ExternalChangeError(f"读取期间目标文件发生变化: {path}")
        return b"".join(chunks), after
    finally:
        wincompat.close(fd)


def _snapshot(path: str, workspace: Workspace,
              protected_paths: tuple[str, ...]) -> _Snapshot:
    _check_protected(path, protected_paths)
    if not _is_within(path, workspace.canonical):
        raise UnsafePathError(f"目标越出 workspace: {path}")
    _reject_symlink_components(path, workspace)
    resolved = os.path.realpath(path)
    _check_protected(resolved, protected_paths)
    if not _is_within(resolved, workspace.resolved):
        raise UnsafePathError(f"解析后的目标越出 workspace: {resolved}")

    parent = os.path.dirname(path)
    try:
        parent_st = os.lstat(parent)
    except OSError as exc:
        raise UnsafePathError(
            f"目标父目录必须已存在且可访问: {parent}: {exc}") from exc
    if stat.S_ISLNK(parent_st.st_mode) or not stat.S_ISDIR(parent_st.st_mode):
        raise UnsafePathError(f"目标父路径不是安全目录: {parent}")
    parent_resolved = os.path.realpath(parent)
    if not _is_within(parent_resolved, workspace.resolved):
        raise UnsafePathError(f"目标父目录越出 workspace: {parent_resolved}")

    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        identity = PathIdentity(
            canonical_path=path,
            resolved_path=resolved,
            existed=False,
            device=None,
            inode=None,
            size=None,
            mtime_ns=None,
            parent_resolved=parent_resolved,
            parent_device=parent_st.st_dev,
            parent_inode=parent_st.st_ino,
        )
        return _Snapshot(identity, None, None)
    except OSError as exc:
        raise UnsafePathError(f"无法检查目标文件: {path}: {exc}") from exc

    if stat.S_ISLNK(lst.st_mode):
        raise UnsafePathError(f"目标是符号链接，拒绝写入: {path}")
    if not stat.S_ISREG(lst.st_mode):
        raise UnsafePathError(f"目标不是普通文件: {path}")
    data, stable = _read_regular_once(path, lst)
    identity = PathIdentity(
        canonical_path=path,
        resolved_path=resolved,
        existed=True,
        device=stable.st_dev,
        inode=stable.st_ino,
        size=stable.st_size,
        mtime_ns=stable.st_mtime_ns,
        parent_resolved=parent_resolved,
        parent_device=parent_st.st_dev,
        parent_inode=parent_st.st_ino,
    )
    return _Snapshot(identity, data, _mode(stable))


@dataclass(frozen=True)
class MatchLocation:
    offset: int
    line: int
    column: int


def _match_locations(text: str, needle: str) -> tuple[MatchLocation, ...]:
    if not needle:
        return ()
    rows: list[MatchLocation] = []
    start = 0
    while True:
        offset = text.find(needle, start)
        if offset < 0:
            break
        line_start = text.rfind("\n", 0, offset) + 1
        rows.append(MatchLocation(
            offset=offset,
            line=text.count("\n", 0, offset) + 1,
            column=offset - line_start + 1,
        ))
        start = offset + len(needle)
    return tuple(rows)


@dataclass(frozen=True)
class PreparedWrite:
    """Immutable approval/execution contract made from final hook arguments."""

    tool_name: str
    canonical_path: str
    workspace_root: str
    protected_paths: tuple[str, ...]
    identity: PathIdentity
    existed_before: bool
    before_bytes: Optional[bytes]
    before_sha256: Optional[str]
    after_bytes: bytes
    after_sha256: str
    mode_before: Optional[int]
    match_count: Optional[int]
    match_locations: tuple[MatchLocation, ...]
    replace_all: bool
    executable: bool
    failure_reason: Optional[str]
    preview_text: str

    @property
    def path(self) -> Path:
        return Path(self.canonical_path)

    def checkpoint_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "tool_name": self.tool_name,
            "path": self.canonical_path,
            "existed_before": self.existed_before,
            "before_sha256": self.before_sha256,
            "expected_after_sha256": self.after_sha256,
            "mode_before": self.mode_before,
            "match_count": self.match_count,
        }


def prepare_write(tool_name: str, arguments: Mapping[str, Any], *,
                  cwd: Optional[os.PathLike[str] | str] = None,
                  workspace_root: Optional[os.PathLike[str] | str] = None,
                  protected_paths: Iterable[str] = PROTECTED_PATHS,
                  preview_lines: int = DEFAULT_PREVIEW_LINES,
                  context_lines: int = DEFAULT_CONTEXT_LINES,
                  line_chars: int = DEFAULT_LINE_CHARS) -> PreparedWrite:
    """Prepare final post-PreToolUse args without mutating the target.

    The target parent must already exist.  Parent-directory creation is not in
    the P0 checkpoint contract because it would itself need rewind semantics.
    """
    if tool_name not in ("write_file", "edit_file"):
        raise PreparationError(f"不支持 prepared write 的工具: {tool_name}")
    if not isinstance(arguments, Mapping):
        raise PreparationError("工具参数必须是 mapping")
    raw_path = arguments.get("path")
    if not isinstance(raw_path, (str, os.PathLike)):
        raise PreparationError("path 必须是字符串")
    base = _normalise_abs(cwd or os.getcwd(), base=os.getcwd())
    protected = tuple(str(item) for item in protected_paths)
    work = _workspace(workspace_root or base, protected)
    path = _normalise_abs(raw_path, base=base)
    # Lexical protection happens before lstat/read, including nonexistent paths.
    _check_protected(path, protected)
    snap = _snapshot(path, work, protected)

    before_bytes = snap.data
    before_sha = _sha(before_bytes) if before_bytes is not None else None
    before_text: Optional[str]
    if before_bytes is None:
        before_text = None
    else:
        try:
            before_text = before_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PreparationError(
                f"{tool_name} P0 只支持 UTF-8 文本: {path}") from exc

    # 换行风格是文件的属性，不是平台的属性。模型写的 old/new 一律用 `\n`，
    # 所以匹配前把 CRLF 归一化；写回时再按原样式还原，字节级保持不变。
    # 不归一化的话，CRLF 文件上的 edit_file 永远「匹配 0 处」——
    # Windows 上新建的文件基本都是 CRLF，等于 edit 工具直接不可用。
    newline = "\n"
    if before_text is not None and "\r\n" in before_text:
        crlf = before_text.count("\r\n")
        if crlf > before_text.count("\n") - crlf:
            newline = "\r\n"
        before_text = before_text.replace("\r\n", "\n")

    match_count: Optional[int] = None
    locations: tuple[MatchLocation, ...] = ()
    replace_all = False
    executable = True
    failure_reason: Optional[str] = None

    if tool_name == "write_file":
        content = arguments.get("content")
        if not isinstance(content, str):
            raise PreparationError("write_file.content 必须是 UTF-8 字符串")
        after_text = content
    else:
        if before_text is None:
            raise PreparationError(f"edit_file 目标不存在: {path}")
        old = arguments.get("old")
        new = arguments.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise PreparationError("edit_file.old/new 必须是 UTF-8 字符串")
        if old == "":
            raise PreparationError("edit_file.old 不能为空")
        replace_all = arguments.get("replace_all", False) is True
        locations = _match_locations(before_text, old)
        match_count = len(locations)
        if match_count == 0:
            executable = False
            failure_reason = "匹配 0 处；该编辑将失败"
            after_text = before_text
        elif match_count > 1 and not replace_all:
            executable = False
            failure_reason = f"匹配 {match_count} 处且 replace_all=false；该编辑将失败"
            after_text = before_text
        else:
            after_text = before_text.replace(
                old, new, -1 if replace_all else 1)

    try:
        # 还原成文件本来的换行风格再落字节：编辑一个词不该把整份文件的
        # 换行改掉（git 里是全文件 diff，CRLF 的 `#!/bin/sh` 还会跑不起来）。
        after_bytes = (
            after_text if newline == "\n" else after_text.replace("\n", newline)
        ).encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PreparationError(f"输出不能编码为 UTF-8: {path}") from exc
    draft = PreparedWrite(
        tool_name=tool_name,
        canonical_path=path,
        workspace_root=work.canonical,
        protected_paths=protected,
        identity=snap.identity,
        existed_before=snap.identity.existed,
        before_bytes=before_bytes,
        before_sha256=before_sha,
        after_bytes=after_bytes,
        after_sha256=_sha(after_bytes),
        mode_before=snap.mode,
        match_count=match_count,
        match_locations=locations,
        replace_all=replace_all,
        executable=executable,
        failure_reason=failure_reason,
        preview_text="",
    )
    return replace(draft, preview_text=render_preview(
        draft, max_lines=preview_lines, context_lines=context_lines,
        max_line_chars=line_chars))


def _clip_line(value: str, limit: int) -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    keep = max(1, (limit - 24) // 2)
    omitted = len(value) - keep * 2
    return (value[:keep] + f" …[行内截断 {omitted} 字符]… " +
            value[-keep:])


def _bounded(lines: list[str], max_lines: int) -> list[str]:
    if max_lines <= 0 or len(lines) <= max_lines:
        return lines
    if max_lines < 5:
        max_lines = 5
    head = (max_lines - 1 + 1) // 2
    tail = max_lines - 1 - head
    omitted = len(lines) - head - tail
    marker = f"… [预览截断：省略 {omitted} 行；保留头尾] …"
    return lines[:head] + [marker] + (lines[-tail:] if tail else [])


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _numbered_unified(before: str, after: str, path: str,
                      context_lines: int) -> list[str]:
    raw = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        n=max(0, int(context_lines)),
        lineterm="",
    )
    output: list[str] = []
    old_line = new_line = 0
    for item in raw:
        line = item.rstrip("\r\n")
        if line.startswith(("--- ", "+++ ")):
            output.append(line)
            continue
        match = _HUNK.match(line)
        if match:
            old_line, new_line = int(match.group(1)), int(match.group(2))
            output.append(line)
            continue
        if line.startswith("-"):
            output.append(f"{old_line:>6} {'':>6} | {line}")
            old_line += 1
        elif line.startswith("+"):
            output.append(f"{'':>6} {new_line:>6} | {line}")
            new_line += 1
        elif line.startswith(" "):
            output.append(f"{old_line:>6} {new_line:>6} | {line}")
            old_line += 1
            new_line += 1
        else:
            output.append(line)
    return output


def _new_file_preview(text: str, path: str) -> list[str]:
    rows = text.split("\n") if text else []
    output = [
        f"[新建文件] {path}",
        f"[内容] {len(text)} 字符 / {len(rows)} 行",
    ]
    output.extend(f"{number:>6} | +{line}"
                  for number, line in enumerate(rows, 1))
    return output


def render_preview(prepared: PreparedWrite, *, max_lines: int = DEFAULT_PREVIEW_LINES,
                   context_lines: int = DEFAULT_CONTEXT_LINES,
                   max_line_chars: int = DEFAULT_LINE_CHARS) -> str:
    """Pure, bounded, ANSI-free preview suitable for approval and tests."""
    lines: list[str] = []
    if prepared.tool_name == "edit_file":
        positions = ", ".join(
            f"第{item.line}行:{item.column}列"
            for item in prepared.match_locations[:12])
        if len(prepared.match_locations) > 12:
            positions += f", …另 {len(prepared.match_locations) - 12} 处"
        lines.append(
            f"[edit_file 预检] 匹配 {prepared.match_count} 处" +
            (f"（{positions}）" if positions else ""))
        if prepared.failure_reason:
            lines.append(f"[拒绝执行] {prepared.failure_reason}")

    after = prepared.after_bytes.decode("utf-8", errors="strict")
    if not prepared.existed_before:
        lines.extend(_new_file_preview(after, prepared.canonical_path))
    elif prepared.executable:
        before = (prepared.before_bytes or b"").decode("utf-8", errors="strict")
        diff = _numbered_unified(
            before, after, prepared.canonical_path, context_lines)
        lines.extend(diff or ["[无内容变化]"])
    else:
        lines.append("[未生成 diff：预检已判定该编辑不可执行]")

    clipped = [_clip_line(line, max_line_chars) for line in lines]
    return "\n".join(_bounded(clipped, int(max_lines)))


def _assert_parent_identity(identity: PathIdentity) -> None:
    parent = os.path.dirname(identity.canonical_path)
    try:
        st = os.lstat(parent)
    except OSError as exc:
        raise ExternalChangeError(f"目标父目录已不可用: {parent}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ExternalChangeError(f"目标父目录身份已变化: {parent}")
    if (st.st_dev, st.st_ino, os.path.realpath(parent)) != (
            identity.parent_device, identity.parent_inode,
            identity.parent_resolved):
        raise ExternalChangeError(f"目标父目录身份已变化: {parent}")


def _open_stable_parent(identity: PathIdentity) -> int:
    """Open the approved parent without following a swapped pathname."""
    parent = os.path.dirname(identity.canonical_path)
    if wincompat.IS_WINDOWS:
        return wincompat.fd_open_dir(parent)
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(parent, flags)
    except OSError as exc:
        raise ExternalChangeError(
            f"无法安全打开目标父目录: {parent}: {exc}") from exc
    try:
        info = wincompat.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != (
                    identity.parent_device, identity.parent_inode)
                or os.path.realpath(parent) != identity.parent_resolved):
            raise ExternalChangeError(f"目标父目录身份已变化: {parent}")
        return fd
    except BaseException:
        wincompat.close(fd)
        raise


def _read_regular_once_at(parent_fd: int, name: str,
                          first_stat: os.stat_result) -> tuple[bytes, os.stat_result]:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = wincompat.fd_open(parent_fd, name, flags)
    except OSError as exc:
        raise ExternalChangeError(
            f"无法安全重读目标文件 {name}: {exc}") from exc
    try:
        before = wincompat.fstat(fd)
        if (not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (
                    first_stat.st_dev, first_stat.st_ino)):
            raise ExternalChangeError(f"目标文件身份已变化: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = wincompat.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = wincompat.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns):
            raise ExternalChangeError(f"读取期间目标文件发生变化: {name}")
        return b"".join(chunks), after
    finally:
        wincompat.close(fd)


def _assert_entry_at(parent_fd: int, identity: PathIdentity, *,
                     expected_sha256: Optional[str],
                     expected_mode: Optional[int]) -> None:
    """CAS check one entry through a stable approved directory descriptor."""
    name = os.path.basename(identity.canonical_path)
    try:
        current = wincompat.fd_stat_nofollow(parent_fd, name)
    except FileNotFoundError:
        if expected_sha256 is None:
            return
        raise ExternalChangeError(
            f"目标在执行前被删除: {identity.canonical_path}")
    except OSError as exc:
        raise ExternalChangeError(
            f"无法检查目标: {identity.canonical_path}: {exc}") from exc
    if expected_sha256 is None:
        raise ExternalChangeError(
            f"新文件路径在执行前被占用: {identity.canonical_path}")
    if not stat.S_ISREG(current.st_mode):
        raise ExternalChangeError(
            f"目标类型已变化: {identity.canonical_path}")
    if (identity.device is not None
            and (current.st_dev, current.st_ino) != (
                identity.device, identity.inode)):
        raise ExternalChangeError(
            f"目标身份已变化: {identity.canonical_path}")
    data, stable = _read_regular_once_at(parent_fd, name, current)
    if _sha(data) != expected_sha256:
        raise ExternalChangeError(
            f"目标 SHA-256 已变化: {identity.canonical_path}")
    if expected_mode is not None and _mode(stable) != expected_mode:
        raise ExternalChangeError(
            f"目标 mode 已变化: {identity.canonical_path}")


def _atomic_replace_snapshot(identity: PathIdentity, *,
                             expected_sha256: Optional[str],
                             expected_mode: Optional[int],
                             replacement: bytes,
                             replacement_mode: int,
                             temp_tag: str) -> None:
    """CAS + replace entirely through one verified parent dirfd."""
    parent_fd = _open_stable_parent(identity)
    name = os.path.basename(identity.canonical_path)
    # Never include the target basename: a valid 220-byte name must still fit
    # under NAME_MAX after adding our temporary suffix.
    tag = hashlib.sha256(str(temp_tag).encode("utf-8")).hexdigest()[:8]
    temp_name = f".kcw-{tag}-{uuid.uuid4().hex}.tmp"
    temp_fd = -1
    created = False
    replaced = False
    try:
        _assert_entry_at(
            parent_fd, identity,
            expected_sha256=expected_sha256,
            expected_mode=expected_mode)
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        temp_fd = wincompat.fd_open(
            parent_fd, temp_name, flags, replacement_mode)
        created = True
        wincompat.fchmod(temp_fd, replacement_mode)
        with wincompat.fdopen(temp_fd, "wb", closefd=True) as handle:
            temp_fd = -1
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        # The pathname binding and entry are checked again immediately before
        # rename, while the actual rename remains pinned to this safe dirfd.
        _assert_parent_identity(identity)
        _assert_entry_at(
            parent_fd, identity,
            expected_sha256=expected_sha256,
            expected_mode=expected_mode)
        wincompat.fd_replace(parent_fd, temp_name, parent_fd, name)
        replaced = True
        created = False
        wincompat.fsync(parent_fd)
        final = wincompat.fd_stat_nofollow(parent_fd, name)
        data, _ = _read_regular_once_at(parent_fd, name, final)
        if _sha(data) != _sha(replacement):
            raise CheckpointError(
                f"原子替换后 SHA-256 不一致: {identity.canonical_path}")
        _assert_parent_identity(identity)
    except ExternalChangeError as exc:
        if replaced:
            raise AtomicReplaceError(
                f"原子替换已提交，但路径复核失败 "
                f"{identity.canonical_path}: {exc}",
                committed=True) from exc
        raise
    except CheckpointError as exc:
        if replaced and not isinstance(exc, AtomicReplaceError):
            raise AtomicReplaceError(
                f"原子替换已提交，但验证失败 "
                f"{identity.canonical_path}: {exc}",
                committed=True) from exc
        raise
    except OSError as exc:
        raise AtomicReplaceError(
            f"原子写入失败 {identity.canonical_path}: {exc}",
            committed=replaced) from exc
    finally:
        if temp_fd >= 0:
            wincompat.close(temp_fd)
        if created:
            try:
                wincompat.fd_unlink(parent_fd, temp_name)
            except FileNotFoundError:
                pass
        wincompat.close(parent_fd)
def assert_prepared_unchanged(prepared: PreparedWrite) -> None:
    """Recheck lstat/resolved identity and raw SHA against approval bytes."""
    _check_protected(prepared.canonical_path, prepared.protected_paths)
    _assert_parent_identity(prepared.identity)
    resolved = os.path.realpath(prepared.canonical_path)
    _check_protected(resolved, prepared.protected_paths)
    if resolved != prepared.identity.resolved_path:
        raise ExternalChangeError(
            f"批准后目标解析位置已变化: {prepared.canonical_path}")
    try:
        lst = os.lstat(prepared.canonical_path)
    except FileNotFoundError:
        if prepared.existed_before:
            raise ExternalChangeError(
                f"批准后目标被删除: {prepared.canonical_path}")
        return
    except OSError as exc:
        raise ExternalChangeError(
            f"批准后无法检查目标: {prepared.canonical_path}: {exc}") from exc

    if not prepared.existed_before:
        raise ExternalChangeError(
            f"批准后新文件路径已被占用: {prepared.canonical_path}")
    if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
        raise ExternalChangeError(
            f"批准后目标类型已变化: {prepared.canonical_path}")
    if (lst.st_dev, lst.st_ino) != (
            prepared.identity.device, prepared.identity.inode):
        raise ExternalChangeError(
            f"批准后目标身份已变化: {prepared.canonical_path}")
    data, _ = _read_regular_once(prepared.canonical_path, lst)
    if _sha(data) != prepared.before_sha256:
        raise ExternalChangeError(
            f"批准后目标内容已变化（SHA-256 不一致）: {prepared.canonical_path}")


def _fsync_dir(path: os.PathLike[str] | str) -> None:
    # Windows 上目录条目的 fsync 无等价 API；NTFS 元数据日志已提供
    # 崩溃后 rename 可见性的近似保证。这里只做存在性检查，不静默扩大语义。
    if wincompat.IS_WINDOWS:
        if not os.path.isdir(os.fspath(path)):
            raise NotADirectoryError(str(path))
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(os.fspath(path), flags)
    try:
        os.fsync(fd)
    finally:
        wincompat.close(fd)


def _open_verified_directory(path: Path) -> int:
    """Open a non-symlink directory and pin the lstat identity."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise CheckpointError(f"无法检查目录 {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise CheckpointError(f"目录路径不安全: {path}")
    if wincompat.IS_WINDOWS:
        return wincompat.fd_open_dir(str(path))
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CheckpointError(f"无法安全打开目录 {path}: {exc}") from exc
    try:
        after = wincompat.fstat(fd)
        if (not stat.S_ISDIR(after.st_mode)
                or (after.st_dev, after.st_ino) != (
                    before.st_dev, before.st_ino)):
            raise CheckpointError(f"目录身份已变化: {path}")
        return fd
    except BaseException:
        wincompat.close(fd)
        raise


@dataclass(frozen=True)
class _DirectoryAnchor:
    path: Path
    resolved: str
    device: int
    inode: int


def _capture_directory_anchor(path: Path,
                              protected_paths: tuple[str, ...]) -> _DirectoryAnchor:
    _check_protected(str(path), protected_paths)
    _check_protected(os.path.realpath(path), protected_paths)
    fd = _open_verified_directory(path)
    try:
        info = wincompat.fstat(fd)
        return _DirectoryAnchor(
            path=path,
            resolved=os.path.realpath(path),
            device=info.st_dev,
            inode=info.st_ino,
        )
    finally:
        wincompat.close(fd)


def _open_directory_anchor(anchor: _DirectoryAnchor,
                           protected_paths: tuple[str, ...]) -> int:
    """Open a stored directory identity without following a replacement.

    Both the pathname and the opened inode are checked.  A rename/symlink or
    mount replacement therefore fails before any openat(O_CREAT) can run.
    """
    _check_protected(str(anchor.path), protected_paths)
    resolved = os.path.realpath(anchor.path)
    _check_protected(resolved, protected_paths)
    if wincompat.IS_WINDOWS:
        return wincompat.fd_open_dir(resolved)
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(anchor.path, flags)
    except OSError as exc:
        raise ManifestError(
            f"无法打开已锚定目录 {anchor.path}: {exc}") from exc
    try:
        info = wincompat.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != (
                    anchor.device, anchor.inode)
                or resolved != anchor.resolved):
            raise ManifestError(f"目录身份已被替换: {anchor.path}")
        return fd
    except BaseException:
        wincompat.close(fd)
        raise


def _read_private_at(directory_fd: int, name: str) -> bytes:
    if os.path.basename(name) != name or name in ("", ".", ".."):
        raise ManifestError(f"非法私有文件名: {name!r}")
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = wincompat.fd_open(directory_fd, name, flags)
    except OSError as exc:
        raise ManifestError(f"无法安全读取私有文件 {name}: {exc}") from exc
    try:
        info = wincompat.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ManifestError(f"私有文件类型不安全: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = wincompat.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        wincompat.close(fd)


def _atomic_private_at(directory_fd: int, name: str, data: bytes) -> None:
    """0600 atomic write anchored to a verified directory descriptor."""
    if os.path.basename(name) != name or name in ("", ".", ".."):
        raise ManifestError(f"非法私有文件名: {name!r}")
    try:
        current = wincompat.fd_stat_nofollow(directory_fd, name)
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise ManifestError(f"无法检查私有文件 {name}: {exc}") from exc
    if current is not None and not stat.S_ISREG(current.st_mode):
        raise ManifestError(f"私有文件类型不安全: {name}")
    temp_name = f".kcp-{uuid.uuid4().hex}.tmp"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = -1
    created = False
    try:
        fd = wincompat.fd_open(directory_fd, temp_name, flags, 0o600)
        created = True
        wincompat.fchmod(fd, 0o600)
        with wincompat.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            wincompat.fsync(handle.fileno())
        wincompat.fd_replace(directory_fd, temp_name, directory_fd, name)
        created = False
        wincompat.fsync(directory_fd)
    except OSError as exc:
        raise ManifestError(f"无法持久化私有文件 {name}: {exc}") from exc
    finally:
        if fd >= 0:
            wincompat.close(fd)
        if created:
            try:
                wincompat.fd_unlink(directory_fd, temp_name)
            except FileNotFoundError:
                pass


def _secure_dir(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not os.path.lexists(cursor):
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    try:
        base = cursor.lstat()
    except OSError as exc:
        raise ManifestError(f"无法检查 checkpoint 目录祖先 {cursor}: {exc}") from exc
    if stat.S_ISLNK(base.st_mode) or not stat.S_ISDIR(base.st_mode):
        raise ManifestError(f"checkpoint 路径祖先不是安全目录: {cursor}")
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
            os.chmod(directory, 0o700, follow_symlinks=False)
            _fsync_dir(directory.parent)
        except FileExistsError:
            # A concurrent creator is acceptable only after the same strict
            # type check below.
            pass
        except OSError as exc:
            raise ManifestError(f"无法创建 checkpoint 目录 {directory}: {exc}") from exc
    try:
        lst = path.lstat()
    except OSError as exc:
        raise ManifestError(f"无法检查 checkpoint 目录 {path}: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode) or not stat.S_ISDIR(lst.st_mode):
        raise ManifestError(f"checkpoint 路径不是安全目录: {path}")
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except OSError as exc:
        raise ManifestError(f"无法收紧 checkpoint 目录权限 {path}: {exc}") from exc


def _validate_id(value: str, label: str) -> str:
    text = str(value)
    if not _SAFE_ID.fullmatch(text) or text in (".", ".."):
        raise ManifestError(f"非法 {label}: {text!r}")
    return text


def _read_private(path: Path) -> bytes:
    try:
        lst = path.lstat()
    except OSError as exc:
        raise ManifestError(f"无法读取 checkpoint 文件 {path}: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
        raise ManifestError(f"checkpoint 文件类型不安全: {path}")
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb", closefd=True) as handle:
            data = handle.read()
    except OSError as exc:
        raise ManifestError(f"无法读取 checkpoint 文件 {path}: {exc}") from exc
    return data


def list_checkpoint_ids_readonly(
        root: os.PathLike[str] | str, session_id: str, *, limit: int = 32
        ) -> list[dict[str, Any]]:
    """List bounded checkpoint file metadata without creating store state.

    ``CheckpointStore`` is intentionally write-capable: construction secures
    its directory tree and ``list_checkpoints`` acquires per-manifest locks.
    Inline command guidance needs a weaker operation.  This reader never
    creates/chmods/locks anything and never opens manifest contents; the
    canonical handler still validates the selected manifest before rewind.
    """
    session_id = _validate_id(session_id, "session_id")
    try:
        maximum = max(0, int(limit))
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"checkpoint metadata limit 非法: {limit!r}") from exc
    if maximum == 0:
        return []

    canonical_root = Path(os.path.normpath(os.path.abspath(
        os.path.expanduser(os.fspath(root)))))
    directory = canonical_root / "checkpoints" / session_id

    # Validate every existing component before scandir.  A symlinked metadata
    # root could otherwise turn a benign completion poll into an unexpected
    # read outside the local state authority.
    cursor = Path(directory.anchor)
    for component in directory.parts[1:]:
        cursor /= component
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ManifestError(
                f"无法检查 checkpoint metadata 目录 {cursor}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ManifestError(
                f"checkpoint metadata 路径不是安全目录: {cursor}")

    rows: list[dict[str, Any]] = []
    try:
        with os.scandir(directory) as entries:
            for scanned, entry in enumerate(entries):
                if scanned >= CHECKPOINT_METADATA_SCAN_LIMIT:
                    break
                if not entry.name.endswith(".json"):
                    continue
                if entry.is_symlink():
                    raise ManifestError(
                        f"checkpoint manifest 不能是符号链接: {entry.path}")
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ManifestError(
                        f"无法检查 checkpoint manifest {entry.path}: {exc}") from exc
                checkpoint_id = _validate_id(
                    entry.name[:-len(".json")], "checkpoint_id")
                rows.append({
                    "checkpoint_id": checkpoint_id,
                    "modified_ns": int(info.st_mtime_ns),
                    "bytes": int(info.st_size),
                })
    except OSError as exc:
        raise ManifestError(
            f"无法扫描 checkpoint metadata 目录 {directory}: {exc}") from exc

    rows.sort(
        key=lambda row: (row["modified_ns"], row["checkpoint_id"]),
        reverse=True)
    return rows[:maximum]


def _atomic_private(path: Path, data: bytes) -> None:
    _secure_dir(path.parent)
    if path.exists() or path.is_symlink():
        try:
            lst = path.lstat()
        except OSError as exc:
            raise ManifestError(f"无法检查 checkpoint 文件 {path}: {exc}") from exc
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
            raise ManifestError(f"checkpoint 文件类型不安全: {path}")
    fd = -1
    temp_name: Optional[str] = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".kcp-", suffix=".tmp",
                                         dir=path.parent)
        wincompat.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_dir(path.parent)
    except OSError as exc:
        raise ManifestError(f"无法持久化 checkpoint 文件 {path}: {exc}") from exc
    finally:
        if fd >= 0:
            wincompat.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class CheckpointCapture:
    session_id: str
    checkpoint_id: str
    manifest_path: str
    path: str
    existed_before: bool
    before_sha256: Optional[str]
    expected_before_sha256: Optional[str]
    pending_after_sha256: str
    source_tool_run_id: Optional[str]
    conversation_message_count: Optional[int] = None

    def event_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "checkpoint_id": self.checkpoint_id,
            "manifest_path": self.manifest_path,
            "path": self.path,
            "existed_before": self.existed_before,
            "before_sha256": self.before_sha256,
            "expected_after_sha256": self.pending_after_sha256,
            "source_tool_run_id": self.source_tool_run_id,
            "conversation_message_count": self.conversation_message_count,
        }


@dataclass(frozen=True)
class ExecutionResult:
    path: str
    created: bool
    before_sha256: Optional[str]
    after_sha256: str
    capture: CheckpointCapture


@dataclass(frozen=True)
class RestoreAction:
    path: str
    action: str
    existed_before: bool
    current_sha256: Optional[str]
    expected_after_sha256: Optional[str]
    before_sha256: Optional[str]
    mode_before: Optional[int]
    current_identity: PathIdentity
    current_bytes: Optional[bytes]
    current_mode: Optional[int]
    restore_bytes: Optional[bytes]


@dataclass(frozen=True)
class RestorePlan:
    session_id: str
    checkpoint_id: str
    manifest_path: str
    actions: tuple[RestoreAction, ...]


@dataclass(frozen=True)
class RestoreResult:
    session_id: str
    checkpoint_id: str
    restored: tuple[str, ...]
    already_restored: tuple[str, ...]
    trashed: tuple[tuple[str, str], ...]


class CheckpointStore:
    """Filesystem authority for content-addressed before-images/manifests."""

    def __init__(self, root: os.PathLike[str] | str,
                 *, protected_paths: Iterable[str] = PROTECTED_PATHS):
        self.root = Path(os.path.normpath(os.path.abspath(
            os.path.expanduser(os.fspath(root)))))
        self.protected_paths = tuple(str(item) for item in protected_paths)
        _check_protected(str(self.root), self.protected_paths)
        # Validate both lexical and resolved forms before mkdir.  Otherwise an
        # innocuous-looking root below a symlink could create files in
        # a protected path before the final directory exists and can be lstat'ed.
        _check_protected(os.path.realpath(self.root), self.protected_paths)
        cursor = Path(self.root.anchor)
        for component in self.root.parts[1:]:
            cursor /= component
            try:
                lst = cursor.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ManifestError(
                    f"无法检查 checkpoint root 祖先 {cursor}: {exc}") from exc
            if stat.S_ISLNK(lst.st_mode):
                raise ManifestError(
                    f"checkpoint root 路径含符号链接: {cursor}")
        _secure_dir(self.root)
        self.checkpoints_dir = self.root / "checkpoints"
        self.blobs_dir = self.root / "checkpoint-files"
        self.trash_dir = self.root / "trash"
        self.path_locks_dir = self.root / "path-locks"
        self.restore_transactions_dir = self.root / "restore-transactions"
        for directory in (
                self.checkpoints_dir, self.blobs_dir, self.trash_dir,
                self.path_locks_dir, self.restore_transactions_dir):
            _secure_dir(directory)
        self._checkpoints_anchor = _capture_directory_anchor(
            self.checkpoints_dir, self.protected_paths)
        self._trash_anchor = _capture_directory_anchor(
            self.trash_dir, self.protected_paths)
        self._path_locks_anchor = _capture_directory_anchor(
            self.path_locks_dir, self.protected_paths)
        self._restore_transactions_anchor = _capture_directory_anchor(
            self.restore_transactions_dir, self.protected_paths)

    @contextmanager
    def _session_directory(self, session_id: str):
        """Yield a session dirfd created/opened below the anchored parent."""
        name = _validate_id(session_id, "session_id")
        parent_fd = _open_directory_anchor(
            self._checkpoints_anchor, self.protected_paths)
        session_fd = -1
        try:
            try:
                wincompat.fd_mkdir(parent_fd, name, 0o700)
                wincompat.fsync(parent_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ManifestError(
                    f"无法创建 checkpoint session 目录 {name}: {exc}") from exc
            flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
            try:
                session_fd = wincompat.fd_open(parent_fd, name, flags)
            except OSError as exc:
                raise ManifestError(
                    f"无法安全打开 checkpoint session 目录 {name}: {exc}") from exc
            info = wincompat.fstat(session_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise ManifestError(
                    f"checkpoint session 不是目录: {name}")
            wincompat.fchmod(session_fd, 0o700)
            yield session_fd
            # Detect a parent-entry replacement before reporting success.  All
            # writes above still targeted the pinned original directory.
            current = wincompat.fd_stat_nofollow(parent_fd, name)
            if (not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != (
                        info.st_dev, info.st_ino)):
                raise ManifestError(
                    f"checkpoint session 目录在操作期间被替换: {name}")
        finally:
            if session_fd >= 0:
                wincompat.close(session_fd)
            wincompat.close(parent_fd)

    def _session_dir(self, session_id: str) -> Path:
        name = _validate_id(session_id, "session_id")
        with self._session_directory(name):
            pass
        result = self.checkpoints_dir / name
        return result

    def manifest_path(self, session_id: str, checkpoint_id: str) -> Path:
        return self._session_dir(session_id) / (
            _validate_id(checkpoint_id, "checkpoint_id") + ".json")

    @contextmanager
    def _lock(self, session_id: str, checkpoint_id: str):
        session_id = _validate_id(session_id, "session_id")
        checkpoint_id = _validate_id(checkpoint_id, "checkpoint_id")
        lock_name = "." + checkpoint_id + ".lock"
        flags = (os.O_RDWR | os.O_CREAT
                 | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        with self._session_directory(session_id) as directory_fd:
            try:
                fd = wincompat.fd_open(
                    directory_fd, lock_name, flags, 0o600)
            except OSError as exc:
                raise ManifestError(
                    f"无法安全 openat checkpoint lock {lock_name}: {exc}") from exc
            try:
                info = wincompat.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or not wincompat.fd_owned_by_current_user(fd)):
                    raise ManifestError(
                        f"checkpoint lock 类型或 owner 不安全: {lock_name}")
                wincompat.fchmod(fd, 0o600)
                wincompat.flock(fd, wincompat.LOCK_EX)
                yield
            finally:
                try:
                    wincompat.flock(fd, wincompat.LOCK_UN)
                finally:
                    wincompat.close(fd)

    @contextmanager
    def _path_locks(self, paths: Iterable[str]):
        """Serialize cooperative writes/restores by canonical target path."""
        descriptors: list[int] = []
        directory_fd = _open_directory_anchor(
            self._path_locks_anchor, self.protected_paths)
        try:
            for path in sorted(set(str(item) for item in paths)):
                digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
                lock_name = f"{digest}.lock"
                flags = (os.O_RDWR | os.O_CREAT
                         | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
                try:
                    fd = wincompat.fd_open(
                        directory_fd, lock_name, flags, 0o600)
                except OSError as exc:
                    raise ManifestError(
                        f"无法安全 openat path lock {lock_name}: {exc}") from exc
                try:
                    info = wincompat.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or not wincompat.fd_owned_by_current_user(fd)):
                        raise ManifestError(
                            f"path lock 类型或 owner 不安全: {lock_name}")
                    wincompat.fchmod(fd, 0o600)
                    wincompat.flock(fd, wincompat.LOCK_EX)
                except BaseException:
                    wincompat.close(fd)
                    raise
                descriptors.append(fd)
            yield
        finally:
            for fd in reversed(descriptors):
                try:
                    wincompat.flock(fd, wincompat.LOCK_UN)
                finally:
                    wincompat.close(fd)
            wincompat.close(directory_fd)

    def _blob_path(self, digest: str) -> Path:
        if not _SHA256.fullmatch(str(digest)):
            raise ManifestError(f"非法 SHA-256: {digest!r}")
        return self.blobs_dir / digest[:2] / digest

    def _write_blob(self, digest: str, data: bytes) -> Path:
        if _sha(data) != digest:
            raise ManifestError("before-image 内容与 SHA-256 不一致")
        path = self._blob_path(digest)
        _secure_dir(path.parent)
        if path.exists() or path.is_symlink():
            actual = _read_private(path)
            if _sha(actual) != digest:
                raise ManifestError(f"content-addressed blob 已损坏: {path}")
            os.chmod(path, 0o600, follow_symlinks=False)
            return path
        _atomic_private(path, data)
        return path

    def _read_blob(self, digest: str) -> bytes:
        path = self._blob_path(digest)
        data = _read_private(path)
        if _sha(data) != digest:
            raise ManifestError(f"content-addressed blob 校验失败: {path}")
        return data

    @staticmethod
    def _new_manifest(session_id: str, checkpoint_id: str, *,
                      conversation_message_count: Optional[int] = None
                      ) -> dict[str, Any]:
        return {
            "schema_version": MANIFEST_VERSION,
            "session_id": session_id,
            "checkpoint_id": checkpoint_id,
            "created_at": _utc_now(),
            "conversation_message_count": conversation_message_count,
            "files": {},
        }

    def _validate_manifest(self, value: Any, *, session_id: str,
                           checkpoint_id: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ManifestError("checkpoint manifest 根节点不是 object")
        if value.get("schema_version") != MANIFEST_VERSION:
            raise ManifestError(
                f"不支持 checkpoint manifest schema: {value.get('schema_version')!r}")
        if value.get("session_id") != session_id or value.get("checkpoint_id") != checkpoint_id:
            raise ManifestError("checkpoint manifest 身份与路径不一致")
        files = value.get("files")
        if not isinstance(files, dict):
            raise ManifestError("checkpoint manifest.files 不是 object")
        message_count = value.get("conversation_message_count")
        if (message_count is not None
                and (type(message_count) is not int or message_count < 0)):
            raise ManifestError(
                "checkpoint conversation_message_count 不是非负整数")
        for path, row in files.items():
            if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
                raise ManifestError(f"manifest 含非法绝对路径: {path!r}")
            if not isinstance(row, dict) or not isinstance(row.get("existed_before"), bool):
                raise ManifestError(f"manifest 文件记录损坏: {path}")
            for key in ("before_sha256", "expected_after_sha256", "pending_after_sha256"):
                digest = row.get(key)
                if digest is not None and not _SHA256.fullmatch(str(digest)):
                    raise ManifestError(f"manifest {path} 的 {key} 非法")
            if row["existed_before"] and row.get("before_sha256") is None:
                raise ManifestError(f"manifest 缺少 before_sha256: {path}")
            if not row["existed_before"] and row.get("before_sha256") is not None:
                raise ManifestError(f"新文件记录不应有 before_sha256: {path}")
        return value

    def _load_unlocked(self, session_id: str, checkpoint_id: str,
                       *, missing_ok: bool = False) -> Optional[dict[str, Any]]:
        path = self.manifest_path(session_id, checkpoint_id)
        try:
            raw = _read_private(path)
        except ManifestError:
            if missing_ok and not path.exists() and not path.is_symlink():
                return None
            raise
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"checkpoint manifest JSON 损坏: {path}: {exc}") from exc
        return self._validate_manifest(
            value, session_id=session_id, checkpoint_id=checkpoint_id)

    def load_manifest(self, session_id: str, checkpoint_id: str) -> dict[str, Any]:
        session_id = _validate_id(session_id, "session_id")
        checkpoint_id = _validate_id(checkpoint_id, "checkpoint_id")
        with self._lock(session_id, checkpoint_id):
            value = self._load_unlocked(session_id, checkpoint_id)
        assert value is not None
        # JSON round-trip gives callers a detached value they cannot use to
        # mutate the store's in-memory authority (there is no shared cache).
        return json.loads(json.dumps(value))

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """List validated checkpoint manifests newest first."""
        session_id = _validate_id(session_id, "session_id")
        directory = self._session_dir(session_id)
        rows: list[dict[str, Any]] = []
        for path in directory.glob("*.json"):
            if path.is_symlink():
                raise ManifestError(f"checkpoint manifest 不能是符号链接: {path}")
            checkpoint_id = path.stem
            value = self.load_manifest(session_id, checkpoint_id)
            rows.append({
                "checkpoint_id": checkpoint_id,
                "created_at": str(value.get("created_at") or ""),
                "files": len(value["files"]),
                "paths": tuple(sorted(value["files"])),
                "conversation_message_count": value.get(
                    "conversation_message_count"),
                "manifest_path": str(path),
            })
        rows.sort(
            key=lambda row: (row["created_at"], row["checkpoint_id"]),
            reverse=True)
        return rows

    def _write_manifest(self, session_id: str, checkpoint_id: str,
                        value: dict[str, Any]) -> Path:
        self._validate_manifest(
            value, session_id=session_id, checkpoint_id=checkpoint_id)
        path = self.manifest_path(session_id, checkpoint_id)
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
        _atomic_private(path, data)
        return path

    def capture_prepared(self, session_id: str, checkpoint_id: str,
                         prepared: PreparedWrite, *,
                         source_tool_run_id: Optional[str] = None,
                         conversation_message_count: Optional[int] = None
                         ) -> CheckpointCapture:
        """Durably capture before-image + pending after before tool_started.

        Any blob/manifest failure raises before the target is changed.  A
        pending digest makes crash recovery unambiguous on either side of the
        atomic target replace.
        """
        if not prepared.executable:
            raise InvalidEditError(prepared.failure_reason or "编辑预检失败")
        session_id = _validate_id(session_id, "session_id")
        checkpoint_id = _validate_id(checkpoint_id, "checkpoint_id")
        if (conversation_message_count is not None
                and (type(conversation_message_count) is not int
                     or conversation_message_count < 0)):
            raise ManifestError(
                "conversation_message_count 必须是非负整数或 None")
        work = _workspace(prepared.workspace_root, self.protected_paths)
        target_paths = (prepared.canonical_path,)
        # A restore that starts after the first recovery scan must acquire this
        # same path lock before it can publish its transaction journal.  The
        # read-only second scan therefore closes the scan-to-lock TOCTOU.  We
        # never recover while holding a subset of a transaction's path locks.
        for _attempt in range(8):
            self.recover_incomplete_transactions(
                workspace_root=work.canonical)
            with self._path_locks(target_paths):
                if self._incomplete_transactions_affecting_paths(
                        work, target_paths):
                    continue
                return self._capture_prepared_after_restore_gate(
                    session_id, checkpoint_id, prepared,
                    source_tool_run_id=source_tool_run_id,
                    conversation_message_count=conversation_message_count)
        raise ManifestError(
            "restore transaction 持续影响目标，无法安全 capture prepared write")

    def _capture_prepared_after_restore_gate(
            self, session_id: str, checkpoint_id: str,
            prepared: PreparedWrite, *,
            source_tool_run_id: Optional[str],
            conversation_message_count: Optional[int]) -> CheckpointCapture:
        """Capture while the caller holds the prepared path lock."""
        assert_prepared_unchanged(prepared)
        with self._lock(session_id, checkpoint_id):
            manifest = self._load_unlocked(
                session_id, checkpoint_id, missing_ok=True)
            if manifest is None:
                manifest = self._new_manifest(
                    session_id, checkpoint_id,
                    conversation_message_count=conversation_message_count)
            elif (conversation_message_count is not None
                  and manifest.get("conversation_message_count")
                  != conversation_message_count):
                raise ManifestError(
                    "同一 checkpoint 的 conversation cutoff 不一致")
            files = manifest["files"]
            row = files.get(prepared.canonical_path)
            if row is None:
                if prepared.existed_before:
                    assert prepared.before_sha256 is not None
                    assert prepared.before_bytes is not None
                    self._write_blob(prepared.before_sha256, prepared.before_bytes)
                row = {
                    "path": prepared.canonical_path,
                    "existed_before": prepared.existed_before,
                    "before_sha256": prepared.before_sha256,
                    "expected_after_sha256": prepared.before_sha256,
                    "pending_after_sha256": None,
                    "mode_before": prepared.mode_before,
                    "source_tool_run_id": source_tool_run_id,
                    "first_tool_name": prepared.tool_name,
                }
                files[prepared.canonical_path] = row
            else:
                pending = row.get("pending_after_sha256")
                current = prepared.before_sha256
                if pending is not None:
                    if current == pending:
                        # Prior atomic replace happened but finalize did not.
                        row["expected_after_sha256"] = pending
                    elif current != row.get("expected_after_sha256"):
                        raise ExternalChangeError(
                            f"checkpoint 存在未决写入且当前内容未知: {prepared.canonical_path}")
                    row["pending_after_sha256"] = None
                if current != row.get("expected_after_sha256"):
                    raise ExternalChangeError(
                        f"同一 checkpoint 的写入链与当前 SHA-256 不一致: {prepared.canonical_path}")
            expected_before = row.get("expected_after_sha256")
            row["pending_after_sha256"] = prepared.after_sha256
            row["last_tool_name"] = prepared.tool_name
            row["last_tool_run_id"] = source_tool_run_id
            row["updated_at"] = _utc_now()
            path = self._write_manifest(session_id, checkpoint_id, manifest)
        return CheckpointCapture(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            manifest_path=str(path),
            path=prepared.canonical_path,
            existed_before=bool(row["existed_before"]),
            before_sha256=row.get("before_sha256"),
            expected_before_sha256=expected_before,
            pending_after_sha256=prepared.after_sha256,
            source_tool_run_id=source_tool_run_id,
            conversation_message_count=manifest.get(
                "conversation_message_count"),
        )

    def _complete_prepared(self, capture: CheckpointCapture,
                           prepared: PreparedWrite) -> None:
        with self._lock(capture.session_id, capture.checkpoint_id):
            manifest = self._load_unlocked(
                capture.session_id, capture.checkpoint_id)
            assert manifest is not None
            row = manifest["files"].get(prepared.canonical_path)
            if not row or row.get("pending_after_sha256") != prepared.after_sha256:
                raise ManifestError(
                    f"checkpoint pending write 丢失或被替换: {prepared.canonical_path}")
            row["expected_after_sha256"] = prepared.after_sha256
            row["pending_after_sha256"] = None
            row["updated_at"] = _utc_now()
            self._write_manifest(capture.session_id, capture.checkpoint_id, manifest)

    def plan_restore(self, session_id: str, checkpoint_id: str, *,
                     workspace_root: os.PathLike[str] | str) -> RestorePlan:
        """Fully preflight every path; return no plan if any path conflicts."""
        session_id = _validate_id(session_id, "session_id")
        checkpoint_id = _validate_id(checkpoint_id, "checkpoint_id")
        work = _workspace(workspace_root, self.protected_paths)
        with self._lock(session_id, checkpoint_id):
            manifest = self._load_unlocked(session_id, checkpoint_id)
        assert manifest is not None
        actions: list[RestoreAction] = []
        for path in sorted(manifest["files"]):
            row = manifest["files"][path]
            _check_protected(path, self.protected_paths)
            if not _is_within(path, work.canonical):
                raise RestoreConflict(f"恢复目标越出 workspace: {path}")
            try:
                current = _snapshot(path, work, self.protected_paths)
            except (UnsafePathError, ExternalChangeError) as exc:
                raise RestoreConflict(str(exc)) from exc
            current_sha = _sha(current.data) if current.data is not None else None
            before_sha = row.get("before_sha256")
            expected = row.get("expected_after_sha256")
            pending = row.get("pending_after_sha256")
            allowed_after = {item for item in (expected, pending) if item is not None}
            existed_before = bool(row["existed_before"])
            if existed_before:
                if current.data is None:
                    raise RestoreConflict(f"恢复目标已被外部删除: {path}")
                before_data = self._read_blob(before_sha)
                if current_sha == before_sha and current.mode == row.get("mode_before"):
                    action = "noop"
                elif current_sha == before_sha or current_sha in allowed_after:
                    action = "restore"
                else:
                    raise RestoreConflict(
                        f"当前 SHA-256 既不是 expected_after 也不是 before-image: {path}")
            else:
                before_data = None
                if current.data is None:
                    action = "noop"
                elif current_sha in allowed_after:
                    action = "trash"
                else:
                    raise RestoreConflict(
                        f"新建文件含外部变化，拒绝移动到 trash: {path}")
            actions.append(RestoreAction(
                path=path,
                action=action,
                existed_before=existed_before,
                current_sha256=current_sha,
                expected_after_sha256=pending or expected,
                before_sha256=before_sha,
                mode_before=row.get("mode_before"),
                current_identity=current.identity,
                current_bytes=current.data,
                current_mode=current.mode,
                restore_bytes=before_data,
            ))
        return RestorePlan(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            manifest_path=str(self.manifest_path(session_id, checkpoint_id)),
            actions=tuple(actions),
        )

    @staticmethod
    def _restore_transaction_name(transaction_id: str) -> str:
        return _validate_id(transaction_id, "restore transaction id") + ".json"

    def _validate_restore_transaction(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ManifestError("restore transaction 根节点不是 object")
        if value.get("schema_version") != RESTORE_TRANSACTION_VERSION:
            raise ManifestError("不支持 restore transaction schema")
        _validate_id(value.get("transaction_id", ""), "restore transaction id")
        _validate_id(value.get("session_id", ""), "session_id")
        _validate_id(value.get("checkpoint_id", ""), "checkpoint_id")
        statuses = {
            "prepared", "mutating", "recovering", "recovery_conflict",
            "rolled_back", "committed",
        }
        if value.get("status") not in statuses:
            raise ManifestError(
                f"非法 restore transaction status: {value.get('status')!r}")
        workspace_root = value.get("workspace_root")
        if (not isinstance(workspace_root, str)
                or not os.path.isabs(workspace_root)
                or os.path.normpath(workspace_root) != workspace_root):
            raise ManifestError("restore transaction workspace_root 非法")
        for key in ("workspace_device", "workspace_inode"):
            if type(value.get(key)) is not int or value[key] < 0:
                raise ManifestError(f"restore transaction {key} 非法")
        actions = value.get("actions")
        if not isinstance(actions, list):
            raise ManifestError("restore transaction.actions 不是 list")
        seen: set[str] = set()
        for row in actions:
            if not isinstance(row, dict):
                raise ManifestError("restore transaction action 不是 object")
            path = row.get("path")
            if (not isinstance(path, str) or not os.path.isabs(path)
                    or os.path.normpath(path) != path or path in seen):
                raise ManifestError(f"restore transaction path 非法: {path!r}")
            seen.add(path)
            if row.get("action") not in {"noop", "restore", "trash"}:
                raise ManifestError(f"restore transaction action 非法: {path}")
            if row.get("progress") not in {
                    "pending", "skipped", "mutated", "rolled_back"}:
                raise ManifestError(f"restore transaction progress 非法: {path}")
            if not isinstance(row.get("current_existed"), bool):
                raise ManifestError(f"restore transaction existed 非法: {path}")
            identity = row.get("source_identity")
            if not isinstance(identity, dict):
                raise ManifestError(
                    f"restore transaction source_identity 非法: {path}")
            for key in ("parent_device", "parent_inode"):
                if type(identity.get(key)) is not int or identity[key] < 0:
                    raise ManifestError(
                        f"restore transaction identity.{key} 非法: {path}")
            if not isinstance(identity.get("parent_resolved"), str):
                raise ManifestError(
                    f"restore transaction parent_resolved 非法: {path}")
            for key in (
                    "current_snapshot_sha256", "result_sha256"):
                digest = row.get(key)
                if digest is not None and not _SHA256.fullmatch(str(digest)):
                    raise ManifestError(
                        f"restore transaction {key} 非法: {path}")
            if (row["current_existed"]
                    and row.get("current_snapshot_sha256") is None):
                raise ManifestError(
                    f"restore transaction 缺少 current snapshot: {path}")
            destination = row.get("destination")
            if destination is not None:
                if (not isinstance(destination, str)
                        or not os.path.isabs(destination)
                        or not _is_within(destination, str(self.trash_dir))):
                    raise ManifestError(
                        f"restore transaction trash destination 非法: {path}")
        return value

    def _write_restore_transaction(self, value: dict[str, Any]) -> Path:
        self._validate_restore_transaction(value)
        name = self._restore_transaction_name(value["transaction_id"])
        data = (json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")) + "\n").encode("utf-8")
        directory_fd = _open_directory_anchor(
            self._restore_transactions_anchor, self.protected_paths)
        try:
            _atomic_private_at(directory_fd, name, data)
        finally:
            wincompat.close(directory_fd)
        return self.restore_transactions_dir / name

    def _read_restore_transaction(self, transaction_id: str) -> dict[str, Any]:
        name = self._restore_transaction_name(transaction_id)
        directory_fd = _open_directory_anchor(
            self._restore_transactions_anchor, self.protected_paths)
        try:
            raw = _read_private_at(directory_fd, name)
        finally:
            wincompat.close(directory_fd)
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"restore transaction JSON 损坏: {name}: {exc}") from exc
        return self._validate_restore_transaction(value)

    def list_restore_transactions(self) -> list[dict[str, Any]]:
        """Return detached, validated restore journals newest first."""
        directory_fd = _open_directory_anchor(
            self._restore_transactions_anchor, self.protected_paths)
        try:
            names = sorted(
                name for name in wincompat.listdir(directory_fd)
                if name.endswith(".json") and not name.startswith("."))
        finally:
            wincompat.close(directory_fd)
        rows = [self._read_restore_transaction(name[:-5]) for name in names]
        rows.sort(
            key=lambda row: (row.get("created_at", ""), row["transaction_id"]),
            reverse=True)
        return json.loads(json.dumps(rows))

    def _incomplete_transactions_affecting_paths(
            self, work: Workspace,
            paths: Iterable[str]) -> tuple[str, ...]:
        """Read-only scan used only after all proposed target locks are held."""
        wanted = set(str(path) for path in paths)
        hits: list[str] = []
        for transaction in self.list_restore_transactions():
            if transaction["status"] not in _INCOMPLETE_RESTORE_STATUSES:
                continue
            if transaction["workspace_root"] != work.canonical:
                continue
            if any(row["path"] in wanted
                   for row in transaction["actions"]):
                hits.append(transaction["transaction_id"])
        return tuple(sorted(hits))

    def _prepare_restore_transaction(
            self, plan: RestorePlan, work: Workspace) -> dict[str, Any]:
        transaction_id = "rt-" + uuid.uuid4().hex
        run_dir = (
            self.trash_dir / plan.session_id / plan.checkpoint_id /
            transaction_id)
        rows: list[dict[str, Any]] = []
        for index, action in enumerate(plan.actions):
            if action.current_bytes is not None:
                current_digest = _sha(action.current_bytes)
                # Every pre-mutation current snapshot is durable before the
                # transaction journal can authorize its first mutation.
                self._write_blob(current_digest, action.current_bytes)
            else:
                current_digest = None
            destination = None
            if action.action == "trash":
                destination = str(self._trash_destination(
                    run_dir, action.path))
            result_sha = (
                _sha(action.restore_bytes)
                if action.action == "restore"
                and action.restore_bytes is not None else None)
            rows.append({
                "index": index,
                "path": action.path,
                "action": action.action,
                "destination": destination,
                "progress": "pending",
                "current_existed": action.current_bytes is not None,
                "current_snapshot_sha256": current_digest,
                "current_mode": action.current_mode,
                "result_sha256": result_sha,
                "result_mode": (
                    action.mode_before if action.action == "restore" else None),
                "source_identity": {
                    "device": action.current_identity.device,
                    "inode": action.current_identity.inode,
                    "parent_device": action.current_identity.parent_device,
                    "parent_inode": action.current_identity.parent_inode,
                    "parent_resolved": action.current_identity.parent_resolved,
                },
            })
        value = {
            "schema_version": RESTORE_TRANSACTION_VERSION,
            "transaction_id": transaction_id,
            "session_id": plan.session_id,
            "checkpoint_id": plan.checkpoint_id,
            "workspace_root": work.canonical,
            "workspace_resolved": work.resolved,
            "workspace_device": work.device,
            "workspace_inode": work.inode,
            "status": "prepared",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "actions": rows,
        }
        self._write_restore_transaction(value)
        return value

    def _set_restore_transaction_status(
            self, value: dict[str, Any], status: str, **fields: Any) -> None:
        value["status"] = status
        value["updated_at"] = _utc_now()
        value.update(fields)
        self._write_restore_transaction(value)

    def _cleanup_transaction_destination(
            self, destination: Optional[str], *,
            expected_sha256: str, expected_mode: Optional[int]) -> None:
        if destination is None:
            return
        path = Path(destination)
        directory_fd = self._open_trash_parent(path, create=False)
        if directory_fd is None:
            return
        try:
            try:
                info = wincompat.fd_stat_nofollow(directory_fd, path.name)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode):
                raise RestoreConflict(f"transaction trash 类型变化: {path}")
            data, stable = _read_regular_once_at(
                directory_fd, path.name, info)
            if (_sha(data) != expected_sha256
                    or (expected_mode is not None
                        and _mode(stable) != expected_mode)):
                raise RestoreConflict(f"transaction trash 内容变化: {path}")
            wincompat.fd_unlink(directory_fd, path.name)
            wincompat.fsync(directory_fd)
        finally:
            wincompat.close(directory_fd)

    @staticmethod
    def _cross_filesystem_temp_name(
            prefix: str, destination: Path) -> str:
        """Return a journal-derived temp name that crash recovery can find."""
        digest = hashlib.sha256(
            os.fspath(destination).encode("utf-8")).hexdigest()
        return f".{prefix}-{digest}.tmp"

    @staticmethod
    def _cleanup_verified_temp_at(
            directory_fd: int, name: str, *,
            expected_bytes: bytes, expected_mode: int,
            label: str) -> None:
        """Delete only a journal-owned complete copy or crash prefix."""
        try:
            info = wincompat.fd_stat_nofollow(directory_fd, name)
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not wincompat.st_owned_by_current_user(info)):
            raise RestoreConflict(f"{label} temp 类型或 owner 变化: {name}")
        try:
            data, stable = _read_regular_once_at(directory_fd, name, info)
        except ExternalChangeError as exc:
            raise RestoreConflict(str(exc)) from exc
        # copyfileobj writes the source in order.  A process death can leave
        # any prefix, including zero bytes, before the temp-to-final rename.
        # Requiring that prefix plus type/owner/link/mode prevents an unrelated
        # same-user file from being silently treated as transaction residue.
        if (len(data) > len(expected_bytes)
                or data != expected_bytes[:len(data)]
                or _mode(stable) != expected_mode):
            raise RestoreConflict(f"{label} temp 内容或 mode 变化: {name}")
        wincompat.fd_unlink(directory_fd, name)
        wincompat.fsync(directory_fd)

    def _cleanup_cross_filesystem_temps(
            self, row: Mapping[str, Any], identity: PathIdentity) -> None:
        """Remove complete crash-left copy temps before marking rollback done."""
        if row.get("action") != "trash" or not row.get("current_existed"):
            return
        destination_value = row.get("destination")
        digest = row.get("current_snapshot_sha256")
        mode = row.get("current_mode")
        if (not isinstance(destination_value, str)
                or not _SHA256.fullmatch(str(digest))
                or type(mode) is not int):
            raise ManifestError(
                f"trash transaction 缺少 temp cleanup authority: "
                f"{row.get('path')}")
        destination = Path(destination_value)
        expected_bytes = self._read_blob(digest)

        destination_fd = self._open_trash_parent(
            destination, create=False)
        if destination_fd is not None:
            try:
                self._cleanup_verified_temp_at(
                    destination_fd,
                    self._cross_filesystem_temp_name("kct", destination),
                    expected_bytes=expected_bytes,
                    expected_mode=mode,
                    label="trash copy")
            finally:
                wincompat.close(destination_fd)

        source_fd = _open_stable_parent(identity)
        try:
            self._cleanup_verified_temp_at(
                source_fd,
                self._cross_filesystem_temp_name("kcr", destination),
                expected_bytes=expected_bytes,
                expected_mode=mode,
                label="trash rollback")
        finally:
            wincompat.close(source_fd)

    def _recover_restore_transaction_locked(
            self, value: dict[str, Any], work: Workspace) -> None:
        """Idempotently compensate an incomplete restore to its start state."""
        self._set_restore_transaction_status(value, "recovering")
        failures: list[dict[str, str]] = []
        for row in reversed(value["actions"]):
            try:
                path = row["path"]
                _check_protected(path, self.protected_paths)
                if not _is_within(path, work.canonical):
                    raise RestoreConflict(
                        f"recovery 目标越出 workspace: {path}")
                current = _snapshot(path, work, self.protected_paths)
                source_identity = row["source_identity"]
                if (current.identity.parent_device,
                        current.identity.parent_inode,
                        current.identity.parent_resolved) != (
                            source_identity["parent_device"],
                            source_identity["parent_inode"],
                            source_identity["parent_resolved"]):
                    raise RestoreConflict(
                        f"recovery 目标父目录身份已变化: {path}")
                original_sha = row.get("current_snapshot_sha256")
                original_mode = row.get("current_mode")
                self._cleanup_cross_filesystem_temps(
                    row, current.identity)
                if row["current_existed"]:
                    original = self._read_blob(original_sha)
                    destination_consumed = False
                    if current.data is None:
                        if row["action"] != "trash":
                            raise RestoreConflict(
                                f"recovery 目标意外消失: {path}")
                        destination = row.get("destination")
                        if destination is None:
                            raise RestoreConflict(
                                f"recovery 缺少 trash destination: {path}")
                        # Move/copy only after the durable trash entry is
                        # verified.  source+destination both missing is a
                        # conflict, never permission to resurrect from blob.
                        recovery_action = RestoreAction(
                            path=path,
                            action="trash",
                            existed_before=False,
                            current_sha256=original_sha,
                            expected_after_sha256=None,
                            before_sha256=None,
                            mode_before=None,
                            current_identity=current.identity,
                            current_bytes=original,
                            current_mode=original_mode,
                            restore_bytes=None,
                        )
                        self._rollback_trashed(
                            recovery_action, Path(destination), work)
                        destination_consumed = True
                    else:
                        current_sha = _sha(current.data)
                        original_state = (original_sha, original_mode)
                        result_state = (
                            row.get("result_sha256"),
                            row.get("result_mode"))
                        current_state = (current_sha, current.mode)
                        allowed_states = {original_state}
                        if row["action"] == "restore":
                            allowed_states.add(result_state)
                        if current_state not in allowed_states:
                            raise RestoreConflict(
                                f"recovery 前目标内容或 mode 含外部变化: {path}")
                        if current_state != original_state:
                            _atomic_replace_snapshot(
                                current.identity,
                                expected_sha256=current_sha,
                                expected_mode=current.mode,
                                replacement=original,
                            replacement_mode=(
                                original_mode
                                if original_mode is not None else 0o600),
                                temp_tag="recovery-restore")
                    if not destination_consumed:
                        self._cleanup_transaction_destination(
                            row.get("destination"),
                            expected_sha256=original_sha,
                            expected_mode=original_mode)
                else:
                    if current.data is not None:
                        raise RestoreConflict(
                            f"recovery 前原本缺失的路径出现文件: {path}")
                row["progress"] = "rolled_back"
                row["recovered_at"] = _utc_now()
                value["updated_at"] = _utc_now()
                self._write_restore_transaction(value)
            except BaseException as exc:
                failure = {
                    "path": str(row.get("path") or ""),
                    "kind": type(exc).__name__,
                    "message": str(exc),
                }
                failures.append(failure)
                row["recovery_error"] = failure
                value["updated_at"] = _utc_now()
                self._write_restore_transaction(value)
        if failures:
            value["status"] = "recovery_conflict"
            value["updated_at"] = _utc_now()
            value["recovery_failures"] = failures
            self._write_restore_transaction(value)
            raise PartialRestoreError(
                "restore transaction 补偿存在冲突",
                details={
                    "schema_version": 1,
                    "transaction_id": value["transaction_id"],
                    "rollback_failures": failures,
                })
        self._set_restore_transaction_status(
            value, "rolled_back", recovered_at=_utc_now())

    def recover_incomplete_transactions(
            self, *, workspace_root: os.PathLike[str] | str) -> tuple[str, ...]:
        """Recover every incomplete transaction for this workspace."""
        work = _workspace(workspace_root, self.protected_paths)
        recovered: list[str] = []
        for listed in reversed(self.list_restore_transactions()):
            if listed["status"] not in _INCOMPLETE_RESTORE_STATUSES:
                continue
            if listed["workspace_root"] != work.canonical:
                continue
            if (listed.get("workspace_resolved") != work.resolved
                    or listed.get("workspace_device") != work.device
                    or listed.get("workspace_inode") != work.inode):
                listed["status"] = "recovery_conflict"
                listed["updated_at"] = _utc_now()
                listed["last_recovery_error"] = {
                    "kind": "WorkspaceIdentityChanged",
                    "message": "workspace 同路径的 dev/inode/resolved 已变化",
                }
                self._write_restore_transaction(listed)
                raise RestoreConflict(
                    "incomplete restore 的 workspace 身份已变化，拒绝在同路径恢复")
            paths = tuple(sorted(row["path"] for row in listed["actions"]))
            with self._path_locks(paths):
                current = self._read_restore_transaction(
                    listed["transaction_id"])
                if current["status"] not in _INCOMPLETE_RESTORE_STATUSES:
                    continue
                self._recover_restore_transaction_locked(current, work)
                recovered.append(current["transaction_id"])
        return tuple(recovered)

    def restore(self, session_id: str, checkpoint_id: str, *,
                workspace_root: os.PathLike[str] | str,
                _after_mutation=None) -> RestoreResult:
        """Restore with a crash-durable, auditable compensation journal."""
        session_id = _validate_id(session_id, "session_id")
        checkpoint_id = _validate_id(checkpoint_id, "checkpoint_id")
        work = _workspace(workspace_root, self.protected_paths)
        for _attempt in range(8):
            # Recovery must be outside target path locks: an incomplete
            # multi-path transaction may need a lock we do not yet hold.
            self.recover_incomplete_transactions(
                workspace_root=work.canonical)
            manifest = self.load_manifest(session_id, checkpoint_id)
            wanted_paths = tuple(sorted(manifest["files"]))
            with self._path_locks(wanted_paths):
                if self._incomplete_transactions_affecting_paths(
                        work, wanted_paths):
                    continue
                plan = self.plan_restore(
                    session_id, checkpoint_id,
                    workspace_root=work.canonical)
                actual_paths = tuple(
                    sorted(action.path for action in plan.actions))
                if actual_paths != wanted_paths:
                    continue
                for action in plan.actions:
                    self._assert_restore_snapshot(action)
                mutating = [
                    action for action in plan.actions
                    if action.action in {"restore", "trash"}]
                if not mutating:
                    return RestoreResult(
                        session_id=plan.session_id,
                        checkpoint_id=plan.checkpoint_id,
                        restored=(),
                        already_restored=tuple(
                            action.path for action in plan.actions),
                        trashed=(),
                    )
                transaction = self._prepare_restore_transaction(plan, work)
                self._set_restore_transaction_status(transaction, "mutating")
                restored: list[str] = []
                already: list[str] = []
                trashed: list[tuple[str, str]] = []
                mutation_count = 0
                by_path = {action.path: action for action in plan.actions}
                try:
                    for row in transaction["actions"]:
                        action = by_path[row["path"]]
                        if action.action == "noop":
                            self._assert_restore_snapshot(action)
                            already.append(action.path)
                            row["progress"] = "skipped"
                        elif action.action == "restore":
                            self._replace_for_restore(action)
                            restored.append(action.path)
                            mutation_count += 1
                            if _after_mutation is not None:
                                _after_mutation(mutation_count, dict(row))
                            row["progress"] = "mutated"
                        elif action.action == "trash":
                            destination = Path(row["destination"])
                            self._move_to_trash(action, destination)
                            trashed.append((action.path, str(destination)))
                            mutation_count += 1
                            if _after_mutation is not None:
                                _after_mutation(mutation_count, dict(row))
                            row["progress"] = "mutated"
                        row["updated_at"] = _utc_now()
                        transaction["updated_at"] = _utc_now()
                        self._write_restore_transaction(transaction)
                    # The commit marker is part of the mutation transaction.
                    # A write/fsync error here must enter compensation rather
                    # than return a misleading failed-but-mutated result.
                    self._set_restore_transaction_status(
                        transaction, "committed", committed_at=_utc_now())
                except BaseException as exc:
                    try:
                        self._recover_restore_transaction_locked(
                            transaction, work)
                    except BaseException as recovery_exc:
                        details = {
                            "schema_version": 1,
                            "transaction_id": transaction[
                                "transaction_id"],
                            "error_kind": type(exc).__name__,
                            "error": str(exc),
                            "recovery_error_kind": type(
                                recovery_exc).__name__,
                            "recovery_error": str(recovery_exc),
                        }
                        if isinstance(recovery_exc, PartialRestoreError):
                            # Keep per-path residue visible to the caller's
                            # checkpoint_restore_partial audit event.
                            details.update(recovery_exc.details)
                        raise PartialRestoreError(
                            "restore 失败且 durable compensation 未完成",
                            details=details) from exc
                    raise
                return RestoreResult(
                    session_id=plan.session_id,
                    checkpoint_id=plan.checkpoint_id,
                    restored=tuple(restored),
                    already_restored=tuple(already),
                    trashed=tuple(trashed),
                )
        raise ManifestError(
            "checkpoint 文件集合或 restore transaction 持续变化，"
            "无法取得安全 path lock")

    @staticmethod
    def _assert_restore_snapshot(action: RestoreAction) -> None:
        path = action.path
        try:
            _assert_parent_identity(action.current_identity)
        except ExternalChangeError as exc:
            raise RestoreConflict(str(exc)) from exc
        try:
            lst = os.lstat(path)
        except FileNotFoundError:
            if action.current_sha256 is None:
                return
            raise RestoreConflict(f"恢复执行前目标被删除: {path}")
        if action.current_sha256 is None:
            raise RestoreConflict(f"恢复执行前目标被创建: {path}")
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
            raise RestoreConflict(f"恢复执行前目标类型变化: {path}")
        if (lst.st_dev, lst.st_ino) != (
                action.current_identity.device, action.current_identity.inode):
            raise RestoreConflict(f"恢复执行前目标身份变化: {path}")
        data, _ = _read_regular_once(path, lst)
        if _sha(data) != action.current_sha256:
            raise RestoreConflict(f"恢复执行前目标内容变化: {path}")
        if (action.current_mode is not None
                and _mode(lst) != action.current_mode):
            raise RestoreConflict(f"恢复执行前目标 mode 变化: {path}")

    def _rollback_restore(self, plan: RestorePlan,
                          restore_mutations: set[str],
                          trash_attempts: Mapping[str, Path], *,
                          workspace_root: os.PathLike[str] | str
                          ) -> list[dict[str, str]]:
        """Best-effort compensation back to every pre-restore snapshot."""
        work = _workspace(workspace_root, self.protected_paths)
        failures: list[dict[str, str]] = []
        for action in reversed(plan.actions):
            try:
                if (action.action == "restore"
                        and action.path in restore_mutations):
                    self._rollback_replaced(action, work)
                elif (action.action == "trash"
                      and action.path in trash_attempts):
                    self._rollback_trashed(
                        action, trash_attempts[action.path], work)
            except BaseException as exc:
                failures.append({
                    "path": action.path,
                    "error_kind": type(exc).__name__,
                    "error": str(exc),
                })
        return failures

    def _rollback_replaced(self, action: RestoreAction,
                           work: Workspace) -> None:
        if action.current_bytes is None or action.current_mode is None:
            raise RestoreConflict(
                f"缺少 restore rollback snapshot: {action.path}")
        current = _snapshot(action.path, work, self.protected_paths)
        if current.data is None:
            raise RestoreConflict(
                f"补偿回滚时目标已消失: {action.path}")
        current_sha = _sha(current.data)
        original_sha = _sha(action.current_bytes)
        if current_sha == original_sha and current.mode == action.current_mode:
            return
        applied_sha = _sha(action.restore_bytes or b"")
        if current_sha != applied_sha:
            raise RestoreConflict(
                f"补偿回滚前目标含外部变化: {action.path}")
        try:
            _atomic_replace_snapshot(
                current.identity,
                expected_sha256=current_sha,
                expected_mode=current.mode,
                replacement=action.current_bytes,
                replacement_mode=action.current_mode,
                temp_tag="rewind-rollback")
        except ExternalChangeError as exc:
            raise RestoreConflict(str(exc)) from exc

    def _replace_for_restore(self, action: RestoreAction) -> None:
        data = action.restore_bytes
        assert data is not None
        try:
            _atomic_replace_snapshot(
                action.current_identity,
                expected_sha256=action.current_sha256,
                expected_mode=action.current_mode,
                replacement=data,
                replacement_mode=(
                    action.mode_before
                    if action.mode_before is not None else 0o600),
                temp_tag="rewind")
        except ExternalChangeError as exc:
            raise RestoreConflict(str(exc)) from exc

    @staticmethod
    def _trash_destination(run_dir: Path, source: str) -> Path:
        # The transaction journal maps this fixed short name back to source.
        # Never append an arbitrary legal basename (up to NAME_MAX bytes).
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return run_dir / f"file-{digest}"

    def _open_trash_parent(self, destination: Path, *,
                           create: bool) -> Optional[int]:
        """Open a trash destination parent through anchored, nofollow dirfds."""
        parent = destination.parent
        relative = os.path.relpath(parent, self.trash_dir)
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            raise ManifestError(f"trash 目标越出私有目录: {destination}")
        components = () if relative == "." else Path(relative).parts
        current_fd = _open_directory_anchor(
            self._trash_anchor, self.protected_paths)
        try:
            for component in components:
                if (component in ("", ".", "..")
                        or os.path.basename(component) != component):
                    raise ManifestError(
                        f"trash 目录组件非法: {component!r}")
                if create:
                    try:
                        wincompat.fd_mkdir(current_fd, component, 0o700)
                        wincompat.fsync(current_fd)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise ManifestError(
                            f"无法创建 trash 目录 {component}: {exc}") from exc
                flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
                try:
                    next_fd = wincompat.fd_open(current_fd, component, flags)
                except FileNotFoundError:
                    if not create:
                        wincompat.close(current_fd)
                        return None
                    raise
                except OSError as exc:
                    raise ManifestError(
                        f"无法安全打开 trash 目录 {component}: {exc}") from exc
                info = wincompat.fstat(next_fd)
                if not stat.S_ISDIR(info.st_mode):
                    wincompat.close(next_fd)
                    raise ManifestError(
                        f"trash 路径组件不是目录: {component}")
                wincompat.fchmod(next_fd, 0o700)
                wincompat.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            wincompat.close(current_fd)
            raise

    def _move_to_trash(self, action: RestoreAction, destination: Path) -> None:
        self._assert_restore_snapshot(action)
        source_fd = _open_stable_parent(action.current_identity)
        destination_fd = self._open_trash_parent(destination, create=True)
        assert destination_fd is not None
        source_name = Path(action.path).name
        destination_name = destination.name
        try:
            _assert_parent_identity(action.current_identity)
            _assert_entry_at(
                source_fd, action.current_identity,
                expected_sha256=action.current_sha256,
                expected_mode=action.current_mode)
            try:
                wincompat.fd_stat_nofollow(destination_fd, destination_name)
            except FileNotFoundError:
                pass
            else:
                raise CheckpointError(f"trash 目标已存在: {destination}")
            try:
                wincompat.fd_replace(source_fd, source_name, destination_fd, destination_name)
                wincompat.fsync(source_fd)
                wincompat.fsync(destination_fd)
                return
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise CheckpointError(
                        f"无法把新建文件移入可恢复 trash: "
                        f"{action.path}: {exc}") from exc

            # Cross-filesystem fallback: copy durably through both pinned
            # dirfds, revalidate source, then unlink through the source dirfd.
            temp_name = self._cross_filesystem_temp_name(
                "kct", destination)
            incoming_fd = outgoing_fd = -1
            temp_created = False
            try:
                flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
                incoming_fd = wincompat.fd_open(source_fd, source_name, flags)
                out_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
                outgoing_fd = wincompat.fd_open(
                    destination_fd, temp_name, out_flags,
                    (action.current_mode
                     if action.current_mode is not None else 0o600))
                temp_created = True
                wincompat.fchmod(
                    outgoing_fd,
                    (action.current_mode
                     if action.current_mode is not None else 0o600))
                with wincompat.fdopen(incoming_fd, "rb", closefd=True) as incoming, \
                        wincompat.fdopen(outgoing_fd, "wb", closefd=True) as outgoing:
                    incoming_fd = outgoing_fd = -1
                    shutil.copyfileobj(
                        incoming, outgoing, length=1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                wincompat.fd_replace(destination_fd, temp_name, destination_fd, destination_name)
                temp_created = False
                wincompat.fsync(destination_fd)
                _assert_entry_at(
                    source_fd, action.current_identity,
                    expected_sha256=action.current_sha256,
                    expected_mode=action.current_mode)
                wincompat.fd_unlink(source_fd, source_name)
                wincompat.fsync(source_fd)
            finally:
                if incoming_fd >= 0:
                    wincompat.close(incoming_fd)
                if outgoing_fd >= 0:
                    wincompat.close(outgoing_fd)
                if temp_created:
                    try:
                        wincompat.fd_unlink(destination_fd, temp_name)
                    except FileNotFoundError:
                        pass
        finally:
            wincompat.close(destination_fd)
            wincompat.close(source_fd)

    def _rollback_trashed(self, action: RestoreAction, destination: Path,
                          work: Workspace) -> None:
        if action.current_bytes is None or action.current_mode is None:
            raise RestoreConflict(
                f"缺少 trash rollback snapshot: {action.path}")
        source = _snapshot(action.path, work, self.protected_paths)
        original_sha = _sha(action.current_bytes)
        if source.data is not None:
            if (_sha(source.data) == original_sha
                    and source.mode == action.current_mode):
                return
            raise RestoreConflict(
                f"补偿 trash 时原路径含外部变化: {action.path}")

        source_fd = _open_stable_parent(action.current_identity)
        destination_fd = self._open_trash_parent(destination, create=False)
        if destination_fd is None:
            wincompat.close(source_fd)
            raise RestoreConflict(
                f"补偿 trash 时私有目录不存在: {destination.parent}")
        source_name = Path(action.path).name
        destination_name = destination.name
        try:
            _assert_entry_at(
                source_fd, action.current_identity,
                expected_sha256=None, expected_mode=None)
            try:
                destination_stat = wincompat.fd_stat_nofollow(destination_fd, destination_name)
            except FileNotFoundError as exc:
                raise RestoreConflict(
                    f"补偿 trash 时文件与 trash 均不存在: {action.path}") from exc
            if not stat.S_ISREG(destination_stat.st_mode):
                raise RestoreConflict(f"trash 类型已变化: {destination}")
            destination_data, stable = _read_regular_once_at(
                destination_fd, destination_name, destination_stat)
            if (_sha(destination_data) != original_sha
                    or _mode(stable) != action.current_mode):
                raise RestoreConflict(f"trash 内容已变化: {destination}")
            _assert_parent_identity(action.current_identity)
            try:
                wincompat.fd_replace(destination_fd, destination_name, source_fd, source_name)
                wincompat.fsync(destination_fd)
                wincompat.fsync(source_fd)
                return
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise RestoreConflict(
                        f"无法补偿 trash {destination}: {exc}") from exc

            temp_name = self._cross_filesystem_temp_name(
                "kcr", destination)
            incoming_fd = outgoing_fd = -1
            temp_created = False
            try:
                in_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0))
                incoming_fd = wincompat.fd_open(
                    destination_fd, destination_name, in_flags)
                out_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
                outgoing_fd = wincompat.fd_open(
                    source_fd, temp_name, out_flags, action.current_mode)
                temp_created = True
                wincompat.fchmod(outgoing_fd, action.current_mode)
                with wincompat.fdopen(incoming_fd, "rb", closefd=True) as incoming, \
                        wincompat.fdopen(outgoing_fd, "wb", closefd=True) as outgoing:
                    incoming_fd = outgoing_fd = -1
                    shutil.copyfileobj(
                        incoming, outgoing, length=1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                _assert_parent_identity(action.current_identity)
                _assert_entry_at(
                    source_fd, action.current_identity,
                    expected_sha256=None, expected_mode=None)
                wincompat.fd_replace(source_fd, temp_name, source_fd, source_name)
                temp_created = False
                wincompat.fsync(source_fd)
                # Remove trash only after the source copy is durable.
                wincompat.fd_unlink(destination_fd, destination_name)
                wincompat.fsync(destination_fd)
            finally:
                if incoming_fd >= 0:
                    wincompat.close(incoming_fd)
                if outgoing_fd >= 0:
                    wincompat.close(outgoing_fd)
                if temp_created:
                    try:
                        wincompat.fd_unlink(source_fd, temp_name)
                    except FileNotFoundError:
                        pass
        finally:
            wincompat.close(destination_fd)
            wincompat.close(source_fd)


def _write_target(prepared: PreparedWrite) -> None:
    target_mode = (
        prepared.mode_before
        if prepared.mode_before is not None else 0o600)
    _atomic_replace_snapshot(
        prepared.identity,
        expected_sha256=prepared.before_sha256,
        expected_mode=prepared.mode_before,
        replacement=prepared.after_bytes,
        replacement_mode=target_mode,
        temp_tag="zylab")


def execute_prepared(prepared: PreparedWrite, store: CheckpointStore, *,
                     session_id: Optional[str] = None,
                     checkpoint_id: Optional[str] = None,
                     source_tool_run_id: Optional[str] = None,
                     capture: Optional[CheckpointCapture] = None) -> ExecutionResult:
    """Execute an approved prepared write with durable checkpoint authority.

    Controllers that must emit ``checkpoint_created`` before ``tool_started``
    call ``store.capture_prepared(...)`` first and pass that capture here.
    Simpler callers may provide session/checkpoint ids and let this function do
    both phases.
    """
    if not prepared.executable:
        raise InvalidEditError(prepared.failure_reason or "编辑预检失败")
    if capture is None:
        if session_id is None or checkpoint_id is None:
            raise CheckpointError("缺少 capture 或 session_id/checkpoint_id")
        capture = store.capture_prepared(
            session_id, checkpoint_id, prepared,
            source_tool_run_id=source_tool_run_id)
    elif (capture.path != prepared.canonical_path or
          capture.pending_after_sha256 != prepared.after_sha256):
        raise ManifestError("capture 与 prepared write 不匹配")

    work = _workspace(prepared.workspace_root, store.protected_paths)
    target_paths = (prepared.canonical_path,)
    # Capture and execution can be separated by controller/event work.  Use
    # the same outside-recover / inside-read-only-scan protocol as capture and
    # restore so a partial restore cannot appear between scan and path lock.
    for _attempt in range(8):
        store.recover_incomplete_transactions(
            workspace_root=work.canonical)
        with store._path_locks(target_paths):
            if store._incomplete_transactions_affecting_paths(
                    work, target_paths):
                continue
            assert_prepared_unchanged(prepared)
            _write_target(prepared)
            store._complete_prepared(capture, prepared)
            return ExecutionResult(
                path=prepared.canonical_path,
                created=not prepared.existed_before,
                before_sha256=prepared.before_sha256,
                after_sha256=prepared.after_sha256,
                capture=capture,
            )
    raise ManifestError(
        "restore transaction 持续影响目标，无法安全 execute prepared write")


__all__ = [
    "CheckpointCapture", "CheckpointError", "CheckpointStore",
    "ExecutionResult", "ExternalChangeError", "InvalidEditError",
    "ManifestError", "MatchLocation", "PathIdentity", "PreparedWrite",
    "PartialRestoreError", "PreparationError", "RestoreAction",
    "RestoreConflict", "RestorePlan",
    "RestoreResult", "UnsafePathError", "assert_prepared_unchanged",
    "execute_prepared", "prepare_write", "render_preview",
]
