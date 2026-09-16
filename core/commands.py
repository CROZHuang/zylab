"""Markdown-backed custom slash commands.

Templates are data, not executable plugins: invocation expands one bounded
prompt and re-enters the ordinary session queue.  It cannot register tools,
change permissions, run shell code, or bypass the controller.
"""
from __future__ import annotations
from . import paths

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import store


USER_ROOT = store.HOME / "commands"
MAX_TEMPLATE_CHARS = 100_000
MAX_TEMPLATE_BYTES = 200_000
MAX_DIRECTORY_ENTRIES = 2_000
MAX_COMMAND_FILES = 256
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ALLOWED_META = frozenset({"description", "argument-hint"})


class CommandError(RuntimeError):
    """A custom command source or invocation is invalid."""


@dataclass(frozen=True)
class Command:
    name: str
    scope: str
    path: str
    description: str
    argument_hint: str
    template: str
    sha256: str

    def expand(self, arguments=""):
        args = str(arguments or "").strip()
        if "$ARGUMENTS" in self.template:
            return self.template.replace("$ARGUMENTS", args)
        if not args:
            return self.template
        return self.template.rstrip() + "\n\nArguments:\n" + args


@dataclass
class Catalog:
    commands: dict
    conflicts: list
    errors: list
    roots: dict

    def resolve(self, line):
        value = str(line or "")
        if not value.startswith("/"):
            return None
        name, _, arguments = value[1:].partition(" ")
        command = self.commands.get(name.casefold())
        if command is None:
            return None
        expanded = command.expand(arguments)
        if not expanded.strip():
            raise CommandError(f"/{command.name} 展开后为空")
        return command, expanded

    def completion(self):
        return {
            f"/{name}": command.description
            for name, command in sorted(self.commands.items())
        }


def _under(path, parent):
    child = Path(path).resolve(strict=False)
    root = Path(parent).resolve(strict=False)
    return child == root or root in child.parents


def _parse_template(text, path):
    metadata = {}
    body = text
    if text.startswith("---\n"):
        marker = text.find("\n---\n", 4)
        if marker < 0:
            raise CommandError(f"frontmatter 未闭合：{path}")
        raw_meta = text[4:marker]
        body = text[marker + 5:]
        for number, raw in enumerate(raw_meta.splitlines(), 1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            key, separator, value = raw.partition(":")
            key = key.strip().casefold()
            if not separator or key not in ALLOWED_META:
                raise CommandError(
                    f"不支持的 frontmatter（{path}:{number}）；"
                    "只允许 description / argument-hint")
            metadata[key] = value.strip().strip("'\"")
    body = body.strip()
    if not body:
        raise CommandError(f"命令模板为空：{path}")
    if len(body) > MAX_TEMPLATE_CHARS:
        raise CommandError(
            f"命令模板超过 {MAX_TEMPLATE_CHARS:,} 字符：{path}")
    description = metadata.get("description")
    if not description:
        description = next((
            line.lstrip("# ").strip()
            for line in body.splitlines()
            if line.strip()), "自定义 prompt")
    return body, description[:120], metadata.get("argument-hint", "")[:120]


def _read_root(root, scope):
    root = Path(root)
    rows, errors = [], []
    if not root.exists():
        return rows, errors
    if root.is_symlink() or not root.is_dir():
        return rows, [f"{scope} command root 不安全：{root}"]
    boundary = root.resolve(strict=True)
    paths = []
    try:
        with os.scandir(root) as iterator:
            for scanned, entry in enumerate(iterator):
                if scanned >= MAX_DIRECTORY_ENTRIES:
                    errors.append(
                        f"{scope} command root 超过扫描上限 "
                        f"{MAX_DIRECTORY_ENTRIES}：{root}")
                    break
                if entry.name.casefold().endswith(".md"):
                    paths.append(Path(entry.path))
    except OSError as exc:
        return rows, [f"读取 {scope} command root 失败：{exc}"]
    paths.sort(key=lambda path: path.name.casefold())
    if len(paths) > MAX_COMMAND_FILES:
        errors.append(
            f"{scope} command files 超过上限 {MAX_COMMAND_FILES}：{root}")
        paths = paths[:MAX_COMMAND_FILES]
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                raise CommandError(f"命令文件不能是符号链接：{path}")
            resolved = path.resolve(strict=True)
            if not _under(resolved, boundary):
                raise CommandError(f"命令文件越界：{path}")
            name = path.stem.casefold()
            if not NAME.fullmatch(name):
                raise CommandError(f"命令名不合法：{path.name}")
            size = path.stat().st_size
            if size > MAX_TEMPLATE_BYTES:
                raise CommandError(
                    f"命令模板超过 {MAX_TEMPLATE_BYTES:,} bytes：{path}")
            raw = path.read_bytes()
            if len(raw) > MAX_TEMPLATE_BYTES:
                raise CommandError(
                    f"命令模板读取后超过 {MAX_TEMPLATE_BYTES:,} bytes：{path}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CommandError(f"命令模板必须是 UTF-8：{path}") from exc
            body, description, hint = _parse_template(text, path)
            rows.append(Command(
                name=name, scope=scope, path=str(resolved),
                description=description, argument_hint=hint,
                template=body,
                sha256=hashlib.sha256(raw).hexdigest(),
            ))
        except (OSError, CommandError) as exc:
            errors.append(str(exc))
    return rows, errors


def load(*, cwd=None, builtins=()):
    """Load direct ``*.md`` files; conflicts never silently shadow."""
    cwd = os.path.abspath(str(cwd or os.getcwd()))
    roots = {
        "user": USER_ROOT,
        "project": paths.project_dir(cwd) / "commands",
    }
    builtin_names = {
        str(name).removeprefix("/").casefold() for name in builtins}
    selected = {}
    conflicts, errors = [], []
    # User scope precedes project scope.  A project file therefore cannot
    # replace a user's personal command, and built-ins always win both.
    for scope in ("user", "project"):
        rows, root_errors = _read_root(roots[scope], scope)
        errors.extend(root_errors)
        for command in rows:
            if command.name in builtin_names:
                conflicts.append({
                    "name": command.name, "winner": "builtin",
                    "ignored_scope": scope, "path": command.path,
                })
                continue
            winner = selected.get(command.name)
            if winner is not None:
                conflicts.append({
                    "name": command.name, "winner": winner.scope,
                    "ignored_scope": scope, "path": command.path,
                })
                continue
            selected[command.name] = command
    return Catalog(selected, conflicts, errors, {
        name: str(path) for name, path in roots.items()})
