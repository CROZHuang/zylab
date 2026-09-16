"""Discover bounded prompt-data skills without executing them.

Skills are optional reference data, never executable plugins.  A cloned
project may contribute text that the model can choose to read, but it cannot
override a user's same-named skill.  The resulting trust order is therefore
``user > project > builtin``.  No skill can register a tool, permission, hook,
or command; scripts below a skill directory still go through ordinary Bash
permission and sandbox enforcement.
"""
from __future__ import annotations
from . import paths

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import store


BUILTIN_ROOT = Path(__file__).resolve().parent.parent / "skills"
USER_ROOT = store.HOME / "skills"
PROJECT_RELATIVE = Path(paths.project_dirname()) / "skills"
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ALLOWED_META = frozenset({"name", "description"})
MAX_SKILL_BYTES = 200_000
MAX_DIRECTORY_ENTRIES = 2_000
MAX_SKILL_FILES = 256
MAX_INDEX_ENTRIES = 64
SCOPE_PRIORITY = {"user": 0, "project": 1, "builtin": 2}


class SkillError(RuntimeError):
    """One skill source is malformed or outside its declared boundary."""


@dataclass(frozen=True)
class Skill:
    name: str
    scope: str
    path: str
    description: str
    bytes: int


@dataclass
class Catalog:
    skills: dict
    conflicts: list
    errors: list
    warnings: list
    roots: dict


def _under(path, parent):
    child = Path(path).resolve(strict=False)
    root = Path(parent).resolve(strict=False)
    return child == root or root in child.parents


def project_root(cwd=None):
    selected = Path(cwd or os.getcwd()).expanduser().resolve(strict=False)
    repo = str(store.repo_context(selected).get("repo_root") or "").strip()
    base = Path(repo).resolve(strict=False) if repo else selected
    return paths.project_dir(base) / "skills"


def roots(cwd=None):
    return {
        "user": Path(USER_ROOT),
        "project": project_root(cwd),
        "builtin": Path(BUILTIN_ROOT),
    }


def trusted_roots(cwd=None):
    """Return only real, non-symlink skill roots safe for read-tool access."""
    trusted = []
    for root in roots(cwd).values():
        try:
            if root.is_symlink() or not root.is_dir():
                continue
            trusted.append(str(root.resolve(strict=True)))
        except OSError:
            continue
    return tuple(dict.fromkeys(trusted))


def _frontmatter(text, path):
    normalized = str(text).replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise SkillError(f"skill 缺少 frontmatter：{path}")
    marker = normalized.find("\n---\n", 4)
    if marker < 0:
        raise SkillError(f"frontmatter 未闭合：{path}")
    raw_meta = normalized[4:marker]
    body = normalized[marker + 5:].strip()
    metadata = {}
    warnings = []
    for number, raw in enumerate(raw_meta.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, separator, value = raw.partition(":")
        key = key.strip().casefold()
        if not separator or not key:
            raise SkillError(f"frontmatter 无效：{path}:{number}")
        if key not in ALLOWED_META:
            warnings.append(
                f"{path}:{number}: unknown frontmatter key {key!r}; ignored")
            continue
        if key in metadata:
            raise SkillError(f"frontmatter 重复键 {key!r}：{path}:{number}")
        metadata[key] = value.strip().strip("'\"")
    for key in ("name", "description"):
        if not metadata.get(key):
            raise SkillError(f"skill 缺少 {key}：{path}")
    name = metadata["name"].casefold()
    if not NAME.fullmatch(name):
        raise SkillError(f"skill name 必须匹配 {NAME.pattern}：{path}")
    description = " ".join(metadata["description"].split())
    if len(description) > 400:
        raise SkillError(f"skill description 超过 400 字符：{path}")
    if not body:
        raise SkillError(f"skill 正文为空：{path}")
    return name, description, warnings


def _read_skill(directory, *, scope, boundary):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise SkillError(f"skill 目录不能是符号链接或非目录：{directory}")
    path = directory / "SKILL.md"
    if path.is_symlink() or not path.is_file():
        raise SkillError(f"skill 缺少普通文件 SKILL.md：{directory}")
    resolved = path.resolve(strict=True)
    if not _under(resolved, boundary):
        raise SkillError(f"skill 文件越界：{path}")
    size = path.stat().st_size
    if size > MAX_SKILL_BYTES:
        raise SkillError(f"skill 超过 {MAX_SKILL_BYTES:,} bytes：{path}")
    raw = path.read_bytes()
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillError(
            f"skill 读取后超过 {MAX_SKILL_BYTES:,} bytes：{path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"skill 必须是 UTF-8：{path}") from exc
    name, description, warnings = _frontmatter(text, path)
    expected = directory.name.casefold()
    if name != expected:
        raise SkillError(
            f"skill name {name!r} 与目录 {directory.name!r} 不一致：{path}")
    return Skill(
        name=name, scope=scope, path=str(resolved),
        description=description, bytes=len(raw)), warnings


def _read_root(root, scope):
    root = Path(root)
    rows, errors, warnings = [], [], []
    if not root.exists():
        return rows, errors, warnings
    if root.is_symlink() or not root.is_dir():
        return rows, [f"{scope} skill root 不安全：{root}"], warnings
    try:
        boundary = root.resolve(strict=True)
    except OSError as exc:
        return rows, [f"读取 {scope} skill root 失败：{exc}"], warnings
    directories = []
    try:
        with os.scandir(root) as iterator:
            for scanned, entry in enumerate(iterator):
                if scanned >= MAX_DIRECTORY_ENTRIES:
                    errors.append(
                        f"{scope} skill root 超过扫描上限 "
                        f"{MAX_DIRECTORY_ENTRIES}：{root}")
                    break
                if entry.name.startswith("."):
                    continue
                if entry.is_symlink():
                    errors.append(f"skill 目录不能是符号链接：{entry.path}")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(Path(entry.path))
    except OSError as exc:
        return rows, [f"读取 {scope} skill root 失败：{exc}"], warnings
    directories.sort(key=lambda path: path.name.casefold())
    if len(directories) > MAX_SKILL_FILES:
        errors.append(
            f"{scope} skill files 超过上限 {MAX_SKILL_FILES}：{root}")
        directories = directories[:MAX_SKILL_FILES]
    for directory in directories:
        try:
            item, item_warnings = _read_skill(
                directory, scope=scope, boundary=boundary)
            rows.append(item)
            warnings.extend(item_warnings)
        except (OSError, SkillError) as exc:
            errors.append(str(exc))
    return rows, errors, warnings


def load(*, cwd=None):
    """Load all scopes; one bad skill cannot break the remaining catalog."""
    catalog_roots = roots(cwd)
    selected = {}
    conflicts, errors, warnings = [], [], []
    for scope in ("user", "project", "builtin"):
        rows, root_errors, root_warnings = _read_root(
            catalog_roots[scope], scope)
        errors.extend(root_errors)
        warnings.extend(root_warnings)
        for item in rows:
            winner = selected.get(item.name)
            if winner is not None:
                conflicts.append({
                    "name": item.name,
                    "winner": winner.scope,
                    "ignored_scope": scope,
                    "path": item.path,
                })
                continue
            selected[item.name] = item
    return Catalog(
        selected, conflicts, errors, warnings,
        {name: str(path) for name, path in catalog_roots.items()})


def render_index(catalog, *, max_entries=MAX_INDEX_ENTRIES):
    """Render a one-line-per-skill routing index; never include the body."""
    limit = min(MAX_INDEX_ENTRIES, max(0, int(max_entries)))
    ordered = sorted(
        catalog.skills.values(),
        key=lambda item: (SCOPE_PRIORITY.get(item.scope, 99), item.name))
    selected = ordered[:limit]
    warnings = list(catalog.warnings)
    if len(ordered) > limit:
        warnings.append(
            f"skills-index 已按 scope 优先级截断："
            f"{len(ordered)} available / {limit} shown")
    lines = []
    for item in selected:
        description = item.description.replace("|", "/")
        lines.append(
            f"{item.name} | {item.scope} | {description} | {item.path}")
    return {
        "text": "\n".join(lines),
        "entries": selected,
        "available": len(ordered),
        "warnings": warnings,
    }
