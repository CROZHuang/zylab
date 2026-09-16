"""仓库导航与架构派生层（Graft 路线）。

`/map` 从受限文件发现和 AST wiring 生成确定性导航，自动刷新且永不调用
provider。`/architecture` 是用户显式触发的模型派生层：逐文件摘要缓存、
分批合成、不可变 generation 与原子 current pointer。两层都写入
`<repo>/.zylab/map/`，但使用独立 schema 和独立 context authority。

设计骨架照抄 Graft 的 map 设计，适配点：

- **Tier-1 零依赖**：Python 用 stdlib ``ast`` 出符号与 import 边（确定性、$0、
  不联网）；非 Python 文件只到文件级节点，明确标注而非假装覆盖。
  本机没有 tree-sitter 可用（无 npm，原生依赖违背零依赖铁律）。
- **Pass-1 按内容哈希缓存**：`/architecture build` 只重摘要改过的文件。
- **自动路径永不调 LLM**：陈旧检测只比对哈希/指纹；LLM 只在用户显式
  ``/architecture build`` 时运行。
- **Notes 跨重建逐字保留**：生成块之外的人写内容永不覆盖（Graft 原样）。
- **关系动词封闭集**（Graft 原样，每个动词回答一个 reviewer 的问题）；
  集外动词直接丢弃 —— 禁止 "relates_to" 式糊话。

与 Graft 的一处刻意偏离：机器可读 architecture canonical 放在 generation
`manifest.json`，而非节点 frontmatter；节点 markdown 保持纯人读。
"""
from __future__ import annotations
from . import paths

import ast
import contextlib
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import attachments as _attach

MAP_DIRNAME = f"{paths.project_dirname()}/map"
MANIFEST = "manifest.json"
WIRING = ".wiring.json"
MAP_DATA = "map.json"
MAP_SCHEMA = 1
ARCH_DIRNAME = "architecture"
ARCH_POINTER = "current.json"
ARCH_MANIFEST = "manifest.json"
ARCH_SCHEMA = 2
ARCH_LOCK = "architecture.lock"
# v2：换文件名即作废 v1 缓存 —— v1 只按内容哈希做键，出过真实事故：
# 空文件的幻觉摘要（凭空发明 KC 类与 pydantic 依赖）被永久缓存，且换模型
# 换 prompt 都会继续复用（F4，REVIEW-repo-map-20260828）。
SUMMARY_CACHE = ".cache/summaries-v2.json"
CACHE_SCHEMA = 2
GEN_START = "<!-- map:generated:start -->"
GEN_END = "<!-- map:generated:end -->"
VERSION = 1

MAX_FILES = 400              # 超过则截断并显式记录，不静默
MAX_CODE_CHARS = 24_000      # 单文件送审上限（Graft 同值）
MAX_INDEX_CHARS = 6_000      # 注入索引的默认上限
MAX_INDEX_CHARS_HARD = 12_000
MAX_MAP_BYTES = 8 * 1024 * 1024
MAX_MAP_SYMBOLS = 5_000
MAX_MAP_EDGES = 12_000
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 256 * 1024 * 1024
MAX_ARCH_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARCH_NODES = 128
MAX_ARCH_FIELD_CHARS = 4_000
MAX_NODE_FILE_BYTES = 512 * 1024
MAX_GIT_EXCLUDE_BYTES = 1024 * 1024
ARCH_BATCH_FILES = 50
ARCH_BATCH_CHARS = 120_000
ARCH_LOCK_INIT_GRACE = 5.0
DEFAULT_MAX_DIRS = 16
DEFAULT_HUBS_PER_DIR = 3
DEFAULT_HOTSPOTS = 12
SPLIT_THRESHOLD = 0.60

# Graft 的关系动词封闭集 —— 每个动词回答一个 code reviewer 的问题。
RELATIONS = ("part_of", "uses", "depends_on", "produces",
             "configures", "validates", "implements")

SUMMARIZE_PROMPT = (
    "你在为团队知识库记录源代码。给你一个源文件，写一段紧凑的中文摘要，覆盖：\n"
    "1. 这个文件存在的目的；\n"
    "2. 关键的导出函数/类/类型，各自做什么；\n"
    "3. 重要依赖：它建立在哪些内部模块之上、和哪些外部库/服务交互；\n"
    "4. 代码里可见的设计决策、约束或坑。\n"
    "写 3-8 句连贯散文。点名具体标识符（模块、类、服务），它们会成为图里的实体。"
    "不要代码块，不要逐行复述，不要废话。")

SYNTHESIZE_PROMPT = (
    "你在用逐文件摘要构建一个代码库的**架构图**。读者是将要修改这份代码的 AI "
    "agent，所以必须按资深工程师讲解系统的层次来写 —— 不是按文件罗列。\n\n"
    "产出一组**精选**的节点，粒度混合：\n"
    "- \"system\"：把协作为一个组件的多个文件合成一个节点。这应是最常见的类型。\n"
    "- \"file\"：只给真正独立成章的重要模块。\n"
    "- \"concept\"：跨文件的设计决策、不变量、惯例（对 agent 最有价值的节点，"
    "务必给出几个）。\n\n"
    "规则：\n"
    "- 每句摘要必须用**非显而易见**的信息挣到它的 token：不变量、顺序约束、"
    "惯例、失败模式、\"Y 之后绝不能发生 X\"、设计背后的为什么。绝不复述 README "
    "或目录名已经说明的事。\n"
    "- 强烈偏向更少、更大、有含义的节点：N 个文件的仓库，节点数远少于 N。\n"
    "- 每个节点给出：name（人读名）、type（system|file|concept）、summary"
    "（1-3 句，说它在系统中的角色）、sources（它扎根的确切文件路径，取自输入）、"
    "links（指向你在本次回答中定义的其他节点）。\n"
    f"- links 的 relation 必须严格取自：{', '.join(RELATIONS)}。"
    "都不合适就删掉这条链接，不许发明模糊动词。\n"
    "- **每个节点给 1-4 条 links**（to 填目标节点的 name）。没有一条链接的"
    "节点几乎总是遗漏 —— 系统之间的依赖正是这张图存在的意义。首轮真机构建"
    "曾产出 28 个零链接的孤岛，图退化成了清单。\n"
    "- 只输出一个 JSON 对象：{\"nodes\": [...]}，不要任何其他文字。")


class RepoMapError(RuntimeError):
    pass


class RepoMapEmpty(RepoMapError):
    """The workspace has no eligible sources; valid for automatic projection."""


class RepoMapCancelled(RepoMapError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def map_dir(root):
    root_path = Path(root)
    resolved_root = root_path.resolve()
    if paths.is_protected(str(resolved_root)):
        raise RepoMapError("受保护路径是只读的，不能在其中创建 repo map")
    path = root_path / MAP_DIRNAME
    for candidate in (root_path / paths.project_dirname(), path):
        if candidate.is_symlink():
            raise RepoMapError(
                f"repo map authority 不得经过 symlink：{candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise RepoMapError(
                f"repo map authority 必须是目录：{candidate}")
    try:
        path.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise RepoMapError("repo map authority 逃逸工作区") from exc
    return path


def _open_source_fd(root, rel):
    """Open one resolved in-repo source without following its final symlink."""
    root_path = Path(root)
    path = root_path / rel
    if (Path(rel).is_absolute() or ".." in Path(rel).parts
            or path.is_symlink()):
        raise RepoMapError(f"拒绝不安全 source path：{rel}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root_path.resolve())
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(resolved, flags)
    except (OSError, ValueError) as exc:
        raise RepoMapError(
            f"打开 source file 失败：{rel}: {exc}") from exc
    try:
        record = os.fstat(fd)
        if not stat.S_ISREG(record.st_mode):
            raise RepoMapError(f"source file 不是 regular file：{rel}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _source_fingerprint(root, rel):
    fd = _open_source_fd(root, rel)
    try:
        record = os.fstat(fd)
        if record.st_size > MAX_SOURCE_BYTES:
            raise RepoMapError(
                f"source file 超过 {MAX_SOURCE_BYTES:,} bytes：{rel}")
        return [int(record.st_size), int(record.st_mtime_ns)]
    finally:
        os.close(fd)


def _source_snapshot(root, rel):
    """Read one bounded regular file from a single coherent descriptor."""
    fd = _open_source_fd(root, rel)
    try:
        before = os.fstat(fd)
        if before.st_size > MAX_SOURCE_BYTES:
            raise RepoMapError(
                f"source file 超过 {MAX_SOURCE_BYTES:,} bytes：{rel}")
        chunks = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    except OSError as exc:
        raise RepoMapError(
            f"读取 source file 失败：{rel}: {exc}") from exc
    finally:
        os.close(fd)
    if len(data) > MAX_SOURCE_BYTES:
        raise RepoMapError(
            f"source file 在读取期间超过 {MAX_SOURCE_BYTES:,} bytes：{rel}")
    identity_before = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(data) != after.st_size:
        raise RepoMapError(
            f"source file 在构建期间发生变化，请重试：{rel}")
    return data, [int(after.st_size), int(after.st_mtime_ns)]


def slugify(name):
    slug = re.sub(r"[^a-z0-9一-鿿]+", "-",
                  str(name or "").lower()).strip("-")
    return slug or "node"


# ------------------------------------------------------------- 文件发现
def discover_files(root, *, max_files=MAX_FILES):
    """git 可见的源文件（尊重 .gitignore）；非 git 仓库退回受限 walk。

    返回 (相对路径列表, 备注列表)。截断必须显式记录 —— 静默截断会让地图
    看起来"覆盖了一切"（F1 教训）。
    """
    root = Path(root)
    notes = []
    files = []
    max_files = max(0, int(max_files))
    git_discovery_succeeded = False
    walk_truncated = False
    walk_reason = ""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others",
             "--exclude-standard"],
            cwd=root, capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            git_discovery_succeeded = True
            files = [line for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not git_discovery_succeeded:
        # 非 Git cwd 可能是整个 HOME —— 这是实际发生过的情况。
        # max_files 若只在 walk 完成后切片，启动会先遍历整个持久盘。这里
        # 同时限制合格候选和目录数；隐藏目录、环境与构建缓存不属于源码。
        skip_dirs = {
            ".git", paths.project_dirname(), ".mypy_cache", ".pytest_cache",
            ".ruff_cache", ".tox", ".venv", "__pycache__", "build",
            "dist", "node_modules", "venv", "venvs",
        }
        candidate_limit = max_files + 1
        directory_limit = max(64, max_files * 4)
        visited_dirs = 0
        for dirpath, dirnames, filenames in os.walk(root):
            visited_dirs += 1
            if visited_dirs > directory_limit:
                walk_truncated = True
                walk_reason = f"{directory_limit} 个目录"
                break
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in skip_dirs
                and not name.startswith(".")
                and not (Path(dirpath) / name).is_symlink())
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                if not _is_texty(rel):
                    continue
                files.append(rel)
                if len(files) >= candidate_limit:
                    walk_truncated = True
                    walk_reason = f"{candidate_limit} 个文本候选"
                    break
            if walk_truncated:
                break
    keep, excluded, unsafe, oversized = [], 0, 0, 0
    kept_bytes = 0
    resolved_root = root.resolve()
    for rel in sorted(files):
        if rel == paths.project_dirname() or rel.startswith(paths.project_dirname() + "/"):
            continue
        path = root / rel
        if (Path(rel).is_absolute() or ".." in Path(rel).parts
                or path.is_symlink()):
            unsafe += 1
            continue
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError):
            unsafe += 1
            continue
        if not (path.is_file() and _is_texty(rel)):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            unsafe += 1
            continue
        if (size > MAX_SOURCE_BYTES
                or kept_bytes + size > MAX_TOTAL_SOURCE_BYTES):
            oversized += 1
            continue
        # 敏感路径 fail-closed：git 跟踪 ≠ 允许送外部模型（F1，
        # REVIEW-repo-map-20260828）。与 @file 共用同一道闸。
        if _attach.is_sensitive_path(rel):
            excluded += 1
            continue
        keep.append(rel)
        kept_bytes += size
    if excluded:
        notes.append(f"排除 {excluded} 个敏感路径文件（凭据类，不送模型）")
    if unsafe:
        notes.append(
            f"排除 {unsafe} 个 symlink/工作区外来源（不读、不送模型）")
    if oversized:
        notes.append(
            f"排除 {oversized} 个超出 source byte budget 的文件"
            f"（单文件 {MAX_SOURCE_BYTES:,} / 合计 "
            f"{MAX_TOTAL_SOURCE_BYTES:,} bytes）")
    if walk_truncated:
        notes.append(
            f"非 Git 工作区发现达到硬上限（{walk_reason}），已截断遍历；"
            "地图只覆盖确定性的有界前缀")
    if len(keep) > max_files:
        notes.append(f"文件数超过 {max_files}，按路径序截断（丢弃 "
                     f"{len(keep) - max_files} 个）")
        keep = keep[:max_files]
    return keep, notes


_TEXT_SUFFIXES = (".py", ".md", ".toml", ".cfg", ".ini", ".sh", ".json",
                  ".yaml", ".yml", ".txt", ".ts", ".js", ".rs", ".go",
                  ".c", ".h", ".cpp", ".java")


def _is_texty(rel):
    return rel.endswith(_TEXT_SUFFIXES)


# ------------------------------------------------------------- Tier-1: ast
WIRING_SCHEMA = 2


def wiring_of(root, files, *, source_bytes=None):
    """Python 符号/import 边；语法错跳过并记录，绝不失败整个构建。

    v2 起 symbols 带行号 span、imports 带 (module, names, level) 结构 ——
    v1 只存字符串，导致 wiring 无法解析成仓库内依赖边，Tier-1 沦为
    无人消费的旁路产物（REVIEW-repo-map-20260828 F2）。
    """
    result = {}
    for rel in files:
        if not rel.endswith(".py"):
            continue
        try:
            data = (
                source_bytes[rel]
                if source_bytes is not None and rel in source_bytes
                else _source_snapshot(root, rel)[0])
            source = data.decode("utf-8", errors="replace")
            tree = ast.parse(source)
        except SyntaxError as exc:
            result[rel] = {"error": f"SyntaxError: {exc.msg} @ {exc.lineno}"}
            continue
        except (OSError, RepoMapError) as exc:
            result[rel] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        symbols, imports = [], []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                kind = ("class" if isinstance(node, ast.ClassDef)
                        else "def")
                symbols.append({"kind": kind, "name": node.name,
                                "line": int(node.lineno)})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({"module": alias.name, "names": [],
                                    "level": 0})
            elif isinstance(node, ast.ImportFrom):
                imports.append({"module": node.module or "",
                                "names": [a.name for a in node.names][:40],
                                "level": int(node.level or 0)})
        symbols.sort(key=lambda item: (
            int(item.get("line") or 0), str(item.get("kind") or ""),
            str(item.get("name") or "")))
        imports.sort(key=lambda item: (
            int(item.get("level") or 0), str(item.get("module") or ""),
            tuple(str(name) for name in (item.get("names") or []))))
        result[rel] = {"symbols": symbols[:400], "imports": imports[:200]}
    return result


def _module_to_file(dotted, file_set):
    if not dotted:
        return None
    base = str(dotted).replace(".", "/")
    for candidate in (base + ".py", base + "/__init__.py"):
        if candidate in file_set:
            return candidate
    return None


def _import_module(rel, item):
    """Return one repo-rooted dotted module for an import record."""
    level = int(item.get("level") or 0)
    module = str(item.get("module") or "")
    if not level:
        return module
    package = str(rel).split("/")[:-1]
    parent_count = max(0, level - 1)
    anchor = package[:max(0, len(package) - parent_count)]
    dotted = ".".join(anchor)
    return (dotted + "." + module).strip(".") if module else dotted


def resolve_import_edges(wiring, files):
    """把 import 结构解析成仓库内 file→file 依赖边（确定性，零模型）。

    覆盖三种形态：``import a.b``、``from a import b``（b 可能是模块）、
    相对导入 ``from . import x`` / ``from ..pkg import y``。
    解析不到的（外部库）直接丢弃 —— map 只关心仓库内部结构。
    """
    file_set = set(files)
    edges = set()
    for rel, info in wiring.items():
        if info.get("error"):
            continue
        for imp in info.get("imports") or []:
            module = _import_module(rel, imp)
            target = _module_to_file(module, file_set)
            if target and target != rel:
                edges.add((rel, target))
            # from X import a, b —— a/b 本身可能是模块文件
            for name in imp.get("names") or []:
                sub = _module_to_file(
                    (module + "." + str(name)).strip("."), file_set)
                if sub and sub != rel:
                    edges.add((rel, sub))
    return sorted(edges)


# ----------------------------------------------------- deterministic repo map
_LANGUAGES = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".rs": "rust", ".go": "go", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".java": "java", ".sh": "shell",
    ".md": "markdown", ".json": "json", ".toml": "toml",
    ".yaml": "yaml", ".yml": "yaml",
}
_SCOPE_MARKERS = {
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
}


def _language(path):
    return _LANGUAGES.get(Path(path).suffix.lower())


def _node_id(kind, path, name="", line=0):
    material = f"{kind}|{path}|{name}|{int(line or 0)}"
    return kind[:1] + "-" + _sha(material.encode("utf-8"))[:16]


def _graph_from_wiring(wiring, files):
    """Build a deterministic file/symbol graph from stdlib AST evidence."""
    file_ids = {
        rel: _node_id("file", rel)
        for rel in sorted(files)
    }
    nodes = [{
        "id": file_ids[rel], "name": Path(rel).name, "kind": "file",
        "path": rel, "span": "1",
    } for rel in sorted(files)]
    symbol_ids = {}
    symbol_count = 0
    dropped_symbols = 0
    for rel in sorted(wiring):
        for symbol in wiring[rel].get("symbols") or []:
            if symbol_count >= MAX_MAP_SYMBOLS:
                dropped_symbols += 1
                continue
            name = str(symbol.get("name") or "")[:256]
            line = int(symbol.get("line") or 0)
            node_id = _node_id(str(symbol.get("kind") or "symbol"),
                               rel, name, line)
            symbol_ids[(rel, name)] = node_id
            symbol_count += 1
            nodes.append({
                "id": node_id, "name": name,
                "kind": str(symbol.get("kind") or "symbol"),
                "path": rel, "span": str(line),
            })

    edge_set = set()
    for source, target in resolve_import_edges(wiring, files):
        edge_set.add((file_ids[source], file_ids[target], "imports"))

    file_set = set(files)
    for rel in sorted(wiring):
        source_id = file_ids.get(rel)
        if not source_id:
            continue
        for item in wiring[rel].get("imports") or []:
            module = _import_module(rel, item)
            target_file = _module_to_file(module, file_set)
            for name in item.get("names") or []:
                name = str(name)
                direct = symbol_ids.get((target_file, name))
                if direct:
                    edge_set.add((source_id, direct, "imports"))
                    continue
                sub_file = _module_to_file(
                    (module + "." + name).strip("."), file_set)
                if sub_file and sub_file != rel:
                    edge_set.add(
                        (source_id, file_ids[sub_file], "imports"))

    edges = [
        {"source": source, "target": target, "relation": relation}
        for source, target, relation in sorted(edge_set)
    ]
    dropped_edges = max(0, len(edges) - MAX_MAP_EDGES)
    return {
        "nodes": nodes,
        "edges": edges[:MAX_MAP_EDGES],
        "dropped_symbols": dropped_symbols,
        "dropped_edges": dropped_edges,
    }


def _scope_prefixes(files):
    candidates = sorted({
        str(Path(rel).parent).replace(os.sep, "/")
        for rel in files
        if Path(rel).name in _SCOPE_MARKERS
        and str(Path(rel).parent) not in {"", "."}
    })
    # One nested package is still a normal repository. Two or more distinct
    # package roots are the useful monorepo signal.
    return candidates if len(candidates) > 1 else []


def _scope_for(path, prefixes):
    matches = [
        prefix for prefix in prefixes
        if path == prefix or path.startswith(prefix + "/")
    ]
    return max(matches, key=len) if matches else ""


def _dir_key(path, depth):
    parts = str(path).split("/")
    return "/".join(parts[:min(max(1, int(depth)), len(parts))])


def _top_hubs(nodes, in_degree, cap):
    rows = [{
        "name": node["name"], "kind": node["kind"],
        "path": node["path"], "span": node["span"],
        "in_degree": int(in_degree.get(node["id"], 0)),
    } for node in nodes if node.get("kind") != "file"]
    rows = [row for row in rows if row["in_degree"] > 0]
    rows.sort(key=lambda row: (
        -row["in_degree"], row["name"], row["path"], row["span"]))
    return rows[:max(0, int(cap))]


def _directory_groups(nodes, in_degree, *, max_dirs, hubs_per_dir,
                      scope_prefix=""):
    def relative(path):
        if not scope_prefix:
            return path
        if path == scope_prefix:
            return ""
        return path[len(scope_prefix) + 1:]

    file_nodes = [node for node in nodes if node["kind"] == "file"]
    depth_one = {}
    for node in file_nodes:
        key = _dir_key(relative(node["path"]), 1)
        depth_one[key] = depth_one.get(key, 0) + 1
    split = None
    if file_nodes:
        for key in sorted(depth_one):
            if depth_one[key] / len(file_nodes) > SPLIT_THRESHOLD:
                split = key
                break

    groups = {}
    for node in nodes:
        rel = relative(node["path"])
        depth = 2 if split is not None and _dir_key(rel, 1) == split else 1
        key = _dir_key(rel, depth)
        group = groups.setdefault(key, {"files": [], "symbols": []})
        group["files" if node["kind"] == "file" else "symbols"].append(node)

    file_paths = {relative(node["path"]) for node in file_nodes}
    rows = []
    for key, group in groups.items():
        path = (
            key if not scope_prefix
            else scope_prefix if not key
            else f"{scope_prefix}/{key}")
        rows.append({
            "path": path,
            "files": len(group["files"]),
            "symbols": len(group["symbols"]),
            "languages": sorted(filter(None, {
                _language(node["path"]) for node in group["files"]
            })),
            "hubs": _top_hubs(
                group["symbols"], in_degree, hubs_per_dir),
            "is_file": key in file_paths,
        })
    rows.sort(key=lambda row: (-row["symbols"], row["path"]))
    dropped = max(0, len(rows) - max_dirs)
    return rows[:max_dirs], dropped


def deterministic_map(root, *, max_files=MAX_FILES,
                      max_dirs=DEFAULT_MAX_DIRS,
                      hubs_per_dir=DEFAULT_HUBS_PER_DIR,
                      hotspots=DEFAULT_HOTSPOTS):
    """Compute Graft-style repo orientation from wiring only; zero provider."""
    root = Path(root)
    files, notes = discover_files(root, max_files=max_files)
    if not files:
        raise RepoMapEmpty("没有可用源文件")
    source_bytes, source_rows = {}, {}
    for rel in files:
        data, fingerprint = _source_snapshot(root, rel)
        source_bytes[rel] = data
        source_rows[rel] = {
            "hash": _sha(data),
            "print": fingerprint,
        }
    wiring = wiring_of(root, files, source_bytes=source_bytes)
    graph = _graph_from_wiring(wiring, files)
    nodes = graph["nodes"]
    edges = graph["edges"]
    in_degree = {}
    for edge in edges:
        target = edge["target"]
        in_degree[target] = in_degree.get(target, 0) + 1

    prefixes = _scope_prefixes(files)
    scopes = None
    dirs, dropped = [], 0
    if prefixes:
        scope_rows = []
        labels = ["", *prefixes]
        for prefix in labels:
            subset = [
                node for node in nodes
                if _scope_for(node["path"], prefixes) == prefix
            ]
            if not subset:
                continue
            scope_dirs, scope_dropped = _directory_groups(
                subset, in_degree, max_dirs=max_dirs,
                hubs_per_dir=hubs_per_dir, scope_prefix=prefix)
            scope_rows.append({
                "scope": "(root)" if not prefix else prefix + "/",
                "dirs": scope_dirs, "dropped": scope_dropped,
            })
        scopes = sorted(scope_rows, key=lambda row: row["scope"])
    else:
        dirs, dropped = _directory_groups(
            nodes, in_degree, max_dirs=max_dirs,
            hubs_per_dir=hubs_per_dir)

    languages = sorted(filter(None, {_language(rel) for rel in files}))
    result = {
        "schema": MAP_SCHEMA,
        "wiring_schema": WIRING_SCHEMA,
        "totals": {
            "files": len(files),
            "symbols": sum(node["kind"] != "file" for node in nodes),
            "edges": len(edges),
            "languages": languages,
        },
        "dirs": dirs,
        "hotspots": _top_hubs(
            nodes, in_degree, max(0, int(hotspots))),
        "dropped": dropped,
        "notes": list(notes),
        "sources": source_rows,
        "graph": graph,
    }
    if scopes is not None:
        result["scopes"] = scopes
    return result, wiring


def _atomic_write_text(path, text):
    """fsync + replace; readers see the complete old or complete new file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(str(text))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _fsync_directory(path):
    try:
        directory_fd = os.open(Path(path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def _read_bounded_file(path, *, maximum, label, missing_ok=False):
    """Read one stable regular file without following its final symlink."""
    path = Path(path)
    if path.is_symlink():
        raise RepoMapError(f"{label} authority 不得是 symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RepoMapError(f"{label} 不存在")
    except OSError as exc:
        raise RepoMapError(f"读取 {label} 失败：{exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RepoMapError(f"{label} 必须是 regular file")
        if before.st_size > int(maximum):
            raise RepoMapError(
                f"{label} 超过硬上限 {int(maximum):,} bytes")
        chunks = []
        remaining = int(maximum) + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
    except OSError as exc:
        raise RepoMapError(f"读取 {label} 失败：{exc}") from exc
    finally:
        os.close(fd)
    if len(raw) > int(maximum):
        raise RepoMapError(
            f"{label} 超过硬上限 {int(maximum):,} bytes")
    identity_before = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or len(raw) != after.st_size:
        raise RepoMapError(f"{label} 在读取期间发生变化，请重试")
    return raw


def build_map(root, **options):
    """Persist one deterministic repo map. No callback or provider is accepted."""
    result, wiring = deterministic_map(root, **options)
    out = map_dir(root)
    out.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(root)
    _atomic_write_text(
        out / WIRING,
        json.dumps(wiring, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")))
    _atomic_write_text(
        out / MAP_DATA,
        json.dumps(result, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")))
    return result


def _load_json_bounded(path, *, maximum):
    path = Path(path)
    raw = _read_bounded_file(
        path, maximum=maximum, label=path.name, missing_ok=True)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RepoMapError(f"{path.name} 不是有效 JSON：{exc}") from exc


def load_map(root):
    payload = _load_json_bounded(
        map_dir(root) / MAP_DATA, maximum=MAX_MAP_BYTES)
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != MAP_SCHEMA:
        raise RepoMapError("map.json schema 不受支持")
    totals = payload.get("totals")
    sources = payload.get("sources")
    if not isinstance(totals, dict):
        raise RepoMapError("map.json 缺少 totals")
    if not isinstance(sources, dict) or len(sources) > MAX_FILES:
        raise RepoMapError("map.json sources 超过硬上限或格式错误")
    for rel, record in sources.items():
        if (not isinstance(rel, str) or not rel
                or len(rel) > 4096 or Path(rel).is_absolute()
                or ".." in Path(rel).parts
                or not isinstance(record, dict)
                or not isinstance(record.get("hash"), str)
                or len(record["hash"]) > 128
                or not isinstance(record.get("print"), list)
                or len(record["print"]) != 2
                or not all(
                    isinstance(value, int)
                    for value in record["print"])):
            raise RepoMapError("map.json source record 无效")
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        raise RepoMapError("map.json 缺少 graph")
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if (not isinstance(nodes, list)
            or len(nodes) > MAX_FILES + MAX_MAP_SYMBOLS):
        raise RepoMapError("map.json nodes 超过硬上限")
    if not isinstance(edges, list) or len(edges) > MAX_MAP_EDGES:
        raise RepoMapError("map.json edges 超过硬上限")
    node_ids = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise RepoMapError("map.json node 格式错误")
        node_id = node.get("id")
        name = node.get("name")
        path = node.get("path")
        kind = node.get("kind")
        span = node.get("span")
        if (not isinstance(node_id, str) or not node_id
                or len(node_id) > 64 or node_id in node_ids
                or not isinstance(name, str) or len(name) > 512
                or not isinstance(path, str)
                or path not in sources
                or not isinstance(kind, str) or len(kind) > 32
                or not isinstance(span, str) or len(span) > 64):
            raise RepoMapError("map.json node 字段无效")
        node_ids.add(node_id)
    for edge in edges:
        if (not isinstance(edge, dict)
                or not isinstance(edge.get("source"), str)
                or not isinstance(edge.get("target"), str)
                or edge.get("source") not in node_ids
                or edge.get("target") not in node_ids
                or edge.get("relation") != "imports"):
            raise RepoMapError("map.json edge 无效")
    dirs = payload.get("dirs", [])
    scopes = payload.get("scopes")
    hotspots = payload.get("hotspots", [])
    if not isinstance(dirs, list) or len(dirs) > 32:
        raise RepoMapError("map.json dirs 超过硬上限或格式错误")
    if scopes is not None and (
            not isinstance(scopes, list) or len(scopes) > MAX_FILES):
        raise RepoMapError("map.json scopes 超过硬上限或格式错误")
    if not isinstance(hotspots, list) or len(hotspots) > 24:
        raise RepoMapError("map.json hotspots 超过硬上限或格式错误")
    languages = totals.get("languages", [])
    if (not all(
            isinstance(totals.get(name), int)
            for name in ("files", "symbols", "edges"))
            or any(
                totals.get(name, -1) < 0
                for name in ("files", "symbols", "edges"))
            or not isinstance(languages, list)
            or len(languages) > 32
            or not all(
                isinstance(value, str) and len(value) <= 64
                for value in languages)):
        raise RepoMapError("map.json totals 字段无效")
    expected_totals = {
        "files": len(sources),
        "symbols": sum(node["kind"] != "file" for node in nodes),
        "edges": len(edges),
    }
    if any(totals[name] != value for name, value in expected_totals.items()):
        raise RepoMapError("map.json totals 与 graph/source 不一致")

    def valid_hub(row):
        return (
            isinstance(row, dict)
            and isinstance(row.get("name"), str)
            and len(row["name"]) <= 512
            and isinstance(row.get("kind"), str)
            and len(row["kind"]) <= 32
            and isinstance(row.get("path"), str)
            and row.get("path") in sources
            and isinstance(row.get("span"), str)
            and len(row["span"]) <= 64
            and isinstance(row.get("in_degree"), int)
            and row["in_degree"] >= 0)

    def valid_dir(row):
        if not isinstance(row, dict):
            return False
        row_languages = row.get("languages", [])
        row_hubs = row.get("hubs", [])
        return (
            isinstance(row.get("path"), str)
            and len(row["path"]) <= 4096
            and isinstance(row.get("files"), int)
            and row["files"] >= 0
            and isinstance(row.get("symbols"), int)
            and row["symbols"] >= 0
            and isinstance(row_languages, list)
            and all(
                isinstance(value, str) and len(value) <= 64
                for value in row_languages)
            and isinstance(row_hubs, list)
            and len(row_hubs) <= 8
            and all(valid_hub(hub) for hub in row_hubs)
            and (
                "is_file" not in row
                or isinstance(row.get("is_file"), bool)))

    if not all(valid_dir(row) for row in dirs):
        raise RepoMapError("map.json directory row 无效")
    if not all(valid_hub(row) for row in hotspots):
        raise RepoMapError("map.json hotspot 无效")
    for scope in scopes or []:
        scope_dirs = scope.get("dirs") if isinstance(scope, dict) else None
        if (not isinstance(scope, dict)
                or not isinstance(scope.get("scope"), str)
                or len(scope["scope"]) > 4096
                or not isinstance(scope_dirs, list)
                or len(scope_dirs) > 32
                or not all(valid_dir(row) for row in scope_dirs)
                or not isinstance(scope.get("dropped"), int)):
            raise RepoMapError("map.json scope row 无效")
    return payload


def check_map(root, *, hash_check=True, max_files=MAX_FILES):
    payload = load_map(root)
    if not payload:
        return None
    root = Path(root)
    changed, removed = [], []
    for rel, record in sorted((payload.get("sources") or {}).items()):
        path = root / rel
        if path.is_symlink() or not path.is_file():
            removed.append(rel)
        elif hash_check:
            try:
                digest = _sha(_source_snapshot(root, rel)[0])
            except RepoMapError:
                changed.append(rel)
                continue
            if digest != record.get("hash"):
                changed.append(rel)
        else:
            try:
                fingerprint = _source_fingerprint(root, rel)
            except RepoMapError:
                changed.append(rel)
                continue
            if fingerprint != record.get("print"):
                changed.append(rel)
    known = set(payload.get("sources") or {})
    current, _ = discover_files(root, max_files=max_files)
    added = [rel for rel in current if rel not in known]
    return {
        "changed": changed, "removed": removed, "added": added,
        "fresh": not (changed or removed or added),
    }


def _format_hub(row):
    return (
        f"{row['name']} ({Path(row['path']).name}, "
        f"{int(row['in_degree'])}←)")


def format_map(payload, *, max_chars=MAX_INDEX_CHARS):
    """Deterministic, token-budgeted human orientation report."""
    maximum = max(0, min(int(max_chars), MAX_INDEX_CHARS_HARD))
    totals = payload.get("totals") or {}
    lines = [
        "repo map — "
        f"{int(totals.get('files') or 0)} files · "
        f"{int(totals.get('symbols') or 0)} symbols · "
        f"{int(totals.get('edges') or 0)} edges · "
        + ", ".join(totals.get("languages") or []),
        "",
    ]

    def append_dirs(rows, dropped):
        for row in rows:
            suffix = "" if row.get("is_file") else "/"
            hubs = row.get("hubs") or []
            hub_text = (
                "   hubs: " + ", ".join(_format_hub(hub) for hub in hubs)
                if hubs else "")
            lines.append(
                f"{row['path']}{suffix}  {int(row['files'])} files · "
                f"{int(row['symbols'])} symbols{hub_text}")
        if dropped:
            lines.append(f"… +{int(dropped)} more directories not shown")

    if payload.get("scopes") is not None:
        for scope in payload.get("scopes") or []:
            lines.append("## " + str(scope.get("scope") or "(root)"))
            append_dirs(scope.get("dirs") or [], scope.get("dropped") or 0)
            lines.append("")
    else:
        append_dirs(payload.get("dirs") or [], payload.get("dropped") or 0)
        lines.append("")
    hotspots = payload.get("hotspots") or []
    lines.append(
        "hotspots: " + "  ".join(
            f"{row['name']} · {row['kind']} · "
            f"{row['path']}:{row['span']} · {int(row['in_degree'])}←"
            for row in hotspots))
    text = "\n".join(lines).rstrip() + "\n"
    if maximum and len(text) > maximum:
        marker = "\n[repo map truncated]\n"
        text = text[:max(0, maximum - len(marker))].rstrip() + marker
    return text if maximum else ""


def map_index_text(root, *, max_chars=MAX_INDEX_CHARS):
    payload = load_map(root)
    return format_map(payload, max_chars=max_chars) if payload else ""


def _map_graph_manifest(payload):
    """Adapt deterministic graph evidence to the existing safe viz renderer."""
    graph = payload.get("graph") or {}
    by_source = {}
    for edge in graph.get("edges") or []:
        by_source.setdefault(edge["source"], []).append(edge)
    nodes = []
    for node in graph.get("nodes") or []:
        links = [{
            "to": edge["target"], "relation": edge["relation"],
            "description": "",
        } for edge in by_source.get(node["id"], [])]
        nodes.append({
            "name": node["name"], "slug": node["id"],
            "type": "file" if node["kind"] == "file" else "concept",
            "summary": (
                f"{node['kind']} · {node['path']}:{node['span']} · "
                "deterministic wiring evidence"),
            "sources": [{"path": node["path"], "hash": ""}],
            "links": links,
        })
    return {
        "built_at": "deterministic",
        "model": "zero-provider wiring",
        "nodes": nodes,
    }


def render_map_mermaid(payload):
    return render_mermaid(_map_graph_manifest(payload))


def render_map_viz_html(payload, drift=None):
    dirty = set()
    if drift:
        dirty.update(drift.get("changed") or [])
        dirty.update(drift.get("removed") or [])
    stale_nodes = [
        node["id"] for node in (payload.get("graph") or {}).get("nodes") or []
        if node.get("path") in dirty
    ]
    return render_viz_html(
        _map_graph_manifest(payload), {"stale_nodes": stale_nodes})


# ----------------------------------------------------- architecture generations
def _derived_child(parent, name, label):
    parent = Path(parent)
    path = parent / name
    if path.is_symlink():
        raise RepoMapError(f"{label} 不得是 symlink")
    if path.exists() and not path.is_dir():
        raise RepoMapError(f"{label} 必须是目录")
    try:
        path.resolve(strict=False).relative_to(parent.resolve())
    except ValueError as exc:
        raise RepoMapError(f"{label} 逃逸 authority") from exc
    return path


def architecture_root(root):
    return _derived_child(
        map_dir(root), ARCH_DIRNAME, "architecture authority")


def _architecture_generations(root):
    return _derived_child(
        architecture_root(root), "generations",
        "architecture generations authority")


def _architecture_cache_dir(root):
    return _derived_child(
        map_dir(root), ".cache", "architecture cache authority")


def architecture_node_dir(root, manifest=None):
    manifest = manifest or load_architecture_manifest(root)
    generation = str((manifest or {}).get("generation") or "")
    if generation == "legacy":
        return map_dir(root)
    if not generation:
        return None
    if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", generation):
        raise RepoMapError("architecture generation 名称无效")
    generation_dir = _derived_child(
        _architecture_generations(root), generation,
        "architecture generation authority")
    return _derived_child(
        generation_dir, "nodes", "architecture nodes authority")


def _proc_start(pid):
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text(
            encoding="utf-8").split()
        return fields[21]
    except (OSError, ValueError, IndexError):
        return None


def _owner_is_live(owner):
    try:
        pid = int(owner.get("pid"))
    except (TypeError, ValueError, AttributeError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    expected = owner.get("proc_start")
    observed = _proc_start(pid)
    return not expected or not observed or str(expected) == str(observed)


class RepoBuildLock:
    """One live architecture writer per repo, with stale-owner recovery."""

    def __init__(self, root):
        self.path = architecture_root(root) / ARCH_LOCK
        self.token = uuid.uuid4().hex
        self.owner = {
            "token": self.token,
            "pid": os.getpid(),
            "proc_start": _proc_start(os.getpid()),
            "created_at": _now(),
        }
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                fd = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                load_error = False
                try:
                    current = _load_json_bounded(
                        self.path, maximum=16 * 1024) or {}
                except RepoMapError:
                    current = {}
                    load_error = True
                if current and _owner_is_live(current):
                    raise RepoMapError(
                        "architecture build 已由 "
                        f"PID {current.get('pid')} 持有；请等待或取消该 terminal")
                if not current:
                    try:
                        age = max(
                            0.0, time.time() - self.path.stat().st_mtime)
                    except OSError:
                        age = 0.0
                    if age < ARCH_LOCK_INIT_GRACE:
                        detail = "正在初始化" if load_error else "owner 尚未写入"
                        raise RepoMapError(
                            "architecture build lock "
                            f"{detail}；请稍后重试")
                with contextlib.suppress(OSError):
                    self.path.unlink()
                continue
            try:
                payload = json.dumps(
                    self.owner, ensure_ascii=False, sort_keys=True)
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            self.acquired = True
            return self
        raise RepoMapError("无法取得 architecture build lock")

    def __exit__(self, *_args):
        if not self.acquired:
            return
        try:
            current = _load_json_bounded(
                self.path, maximum=16 * 1024) or {}
            if current.get("token") == self.token:
                self.path.unlink()
        except (OSError, RepoMapError):
            pass
        self.acquired = False


def _cancelled(cancel):
    if cancel is None:
        return False
    check = getattr(cancel, "is_set", None)
    if callable(check):
        return bool(check())
    return bool(cancel() if callable(cancel) else cancel)


def _check_cancel(cancel):
    if _cancelled(cancel):
        raise RepoMapCancelled("用户取消 architecture build；已完成摘要缓存保留")


def _invoke_complete(complete, messages, *, max_tokens, purpose, metadata):
    """Pass purpose metadata when the injected boundary supports it."""
    kwargs = {"max_tokens": max_tokens}
    try:
        signature = inspect.signature(complete)
        accepts_kwargs = any(
            item.kind == inspect.Parameter.VAR_KEYWORD
            for item in signature.parameters.values())
        if accepts_kwargs or "purpose" in signature.parameters:
            kwargs["purpose"] = purpose
        if accepts_kwargs or "metadata" in signature.parameters:
            kwargs["metadata"] = dict(metadata or {})
    except (TypeError, ValueError):
        pass
    return str(complete(messages, **kwargs))


def _architecture_cache(root):
    path = _architecture_cache_dir(root) / Path(SUMMARY_CACHE).name
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RepoMapError(
            "architecture summary cache 必须是普通文件或不存在")
    try:
        payload = _load_json_bounded(path, maximum=MAX_MAP_BYTES)
    except RepoMapError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _assert_replaceable_file(path, label):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RepoMapError(f"{label} 必须是普通文件或不存在")


def architecture_plan(root, *, model="?", gateway="?",
                      max_files=MAX_FILES):
    """Read-only request/cost preflight for one architecture build."""
    root = Path(root)
    files, notes = discover_files(root, max_files=max_files)
    cache = _architecture_cache(root)
    hits = misses = empty = 0
    hashes = {}
    estimated_items = []
    for rel in files:
        data, _fingerprint_value = _source_snapshot(root, rel)
        hashes[rel] = _sha(data)
        if not data.strip():
            empty += 1
            summary_chars = len(f"空文件（0 字节）：{rel}。")
            estimated_items.append(
                len(f"### {rel}\n") + summary_chars)
            continue
        key = _summary_cache_key(rel, hashes[rel], model, gateway)
        entry = cache.get(key)
        if isinstance(entry, dict) and entry.get("summary"):
            hits += 1
            summary_chars = min(
                len(str(entry["summary"])), MAX_ARCH_FIELD_CHARS)
        else:
            misses += 1
            # Unknown paid output is estimated at the enforced hard cap, so
            # preflight is an upper bound rather than a cost underestimate.
            summary_chars = MAX_ARCH_FIELD_CHARS
        estimated_items.append(
            len(f"### {rel}\n") + summary_chars)
    batches = 0
    current_files = current_chars = 0
    for size in estimated_items:
        if current_files and (
                current_files >= ARCH_BATCH_FILES
                or current_chars + size > ARCH_BATCH_CHARS):
            batches += 1
            current_files = current_chars = 0
        current_files += 1
        current_chars += size
    if current_files:
        batches += 1
    consolidation = 1 if batches > 1 else 0
    return {
        "files": files,
        "notes": notes,
        "cache_hits": hits,
        "cache_misses": misses,
        "empty_files": empty,
        "synthesis_batches": batches,
        "consolidation_requests": consolidation,
        "estimated_requests": misses + batches + consolidation,
        "estimate_kind": "upper_bound",
        "model": str(model),
        "gateway": str(gateway),
    }


def _summary_batches(files, summaries):
    batches, current, chars = [], [], 0
    for rel in files:
        item = f"### {rel}\n{summaries[rel]}"
        if current and (
                len(current) >= ARCH_BATCH_FILES
                or chars + len(item) > ARCH_BATCH_CHARS):
            batches.append(current)
            current, chars = [], 0
        current.append(rel)
        chars += len(item)
    if current:
        batches.append(current)
    return batches


def _merge_architecture_nodes(groups):
    """Deterministically merge batch nodes while preserving source coverage."""
    merged = {}
    for group in groups:
        for node in group:
            slug = node["slug"]
            if slug not in merged:
                merged[slug] = json.loads(json.dumps(
                    node, ensure_ascii=False))
                continue
            target = merged[slug]
            sources = {
                item["path"]: item for item in target.get("sources") or []}
            for item in node.get("sources") or []:
                sources.setdefault(item["path"], item)
            target["sources"] = [
                sources[key] for key in sorted(sources)]
            links = {
                (item["to"], item["relation"], item.get("description", "")):
                item for item in target.get("links") or []}
            for item in node.get("links") or []:
                links.setdefault((
                    item["to"], item["relation"],
                    item.get("description", "")), item)
            target["links"] = [
                links[key] for key in sorted(links)]
    return [merged[key] for key in sorted(merged)][:MAX_ARCH_NODES]


# ------------------------------------------------------------- 构建
def _summary_cache_key(rel, content_hash, model, gateway):
    """摘要不只依赖内容：path/prompt/model/gateway 任一变化都不得复用。"""
    material = "|".join((
        str(CACHE_SCHEMA), _sha(SUMMARIZE_PROMPT.encode("utf-8"))[:16],
        str(model), str(gateway), str(rel), str(content_hash)))
    return _sha(material.encode("utf-8"))


def _degenerate(text):
    """空产出或复读机式退化 —— 这些不配进缓存，更不配进架构图。"""
    t = str(text or "").strip()
    if len(t) < 5:
        return True
    window = t[:24]
    # 复读 = 同一窗口出现**多次**且覆盖过半。count>=3 是关键 ——
    # 少了它，窗口等于整串时 count==1 也会误判，把一切短摘要打成退化
    #（首版真踩过：全部正常摘要因此永不进缓存）。
    repeats = t.count(window)
    return (len(window) >= 8 and repeats >= 3
            and repeats * len(window) > len(t) * 0.5)


def _deterministic_summary(rel, wiring):
    info = (wiring or {}).get(rel) or {}
    symbols = info.get("symbols") or []
    names = [f"{x['kind']} {x['name']}" if isinstance(x, dict) else str(x)
             for x in symbols[:12]]
    if names:
        return (f"（模型摘要不可用，降级为结构描述）Python 文件，"
                f"符号：{'、'.join(names)}。")
    return f"（模型摘要不可用）{rel}，无符号信息。"


def build_architecture(root, *, complete, model="?", gateway="?",
                       on_note=None, on_progress=None, cancel=None,
                       max_files=MAX_FILES, before_commit=None):
    """Build provider-backed enrichment into one atomic immutable generation."""
    root = Path(root)
    note = on_note or (lambda *_: None)
    progress = on_progress or (lambda *_: None)
    plan = architecture_plan(
        root, model=model, gateway=gateway, max_files=max_files)
    files = plan["files"]
    notes = list(plan["notes"])
    if not files:
        raise RepoMapEmpty("没有可用源文件")
    for message in notes:
        note(message)
    progress({"stage": "preflight", **plan})

    # Keep the zero-provider orientation fresh regardless of enrichment fate.
    build_map(root, max_files=max_files)
    hashes, prints, source_bytes = {}, {}, {}
    for rel in files:
        data, fingerprint = _source_snapshot(root, rel)
        source_bytes[rel] = data
        hashes[rel] = _sha(data)
        prints[rel] = fingerprint
    wiring = wiring_of(root, files, source_bytes=source_bytes)
    out_dir = map_dir(root)
    cache_dir = _architecture_cache_dir(root)
    cache_path = cache_dir / Path(SUMMARY_CACHE).name
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(exist_ok=True)

    with RepoBuildLock(root):
        # Validate every late publication target before the first provider
        # request. A poisoned directory must not consume paid work and then
        # fail only at cache flush or generation commit.
        generations = _architecture_generations(root)
        _assert_replaceable_file(
            cache_path, "architecture summary cache")
        _assert_replaceable_file(
            cache_dir / "last-synthesis.txt",
            "architecture synthesis evidence")
        # Re-read after acquiring the writer lock. A concurrent build may have
        # completed paid summaries between preflight and lock acquisition.
        cache = _architecture_cache(root)

        def flush_cache():
            _atomic_write_text(
                cache_path,
                json.dumps(cache, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")))

        previous = load_architecture_manifest(root)
        previous_nodes = architecture_node_dir(root, previous)
        previous_notes = {}
        note_snapshots = {}
        if previous_nodes is not None:
            for node in (previous or {}).get("nodes", []):
                note_path = previous_nodes / f"{node['slug']}.md"
                notes_tail, raw = _read_node_notes(note_path)
                previous_notes[node["slug"]] = notes_tail
                if raw is not None:
                    note_snapshots[note_path] = raw

        def verify_notes_unchanged():
            for note_path, expected in note_snapshots.items():
                current = _read_bounded_file(
                    note_path, maximum=MAX_NODE_FILE_BYTES,
                    label=f"architecture Notes {note_path.name}")
                if current != expected:
                    raise RepoMapError(
                        "architecture Notes 在构建期间发生变化；"
                        "保留 current generation，请重试")

        summaries, fresh = {}, 0
        completed = 0
        for rel in files:
            _check_cancel(cancel)
            key = _summary_cache_key(rel, hashes[rel], model, gateway)
            entry = cache.get(key)
            if isinstance(entry, dict) and entry.get("summary"):
                summaries[rel] = str(entry["summary"])[
                    :MAX_ARCH_FIELD_CHARS]
                continue
            code = source_bytes[rel].decode(
                "utf-8", errors="replace")
            if not code.strip():
                summaries[rel] = f"空文件（0 字节）：{rel}。"
                continue
            fresh += 1
            note(f"摘要 {rel}")
            progress({
                "stage": "summary", "state": "request",
                "completed": completed, "total": plan["cache_misses"],
                "file": rel,
            })
            code = _attach.redact_secret_lines(code)
            if len(code) > MAX_CODE_CHARS:
                code = (
                    code[:MAX_CODE_CHARS]
                    + f"\n… (截断于 {MAX_CODE_CHARS} 字符)")
            raw_summary = _invoke_complete(
                complete, [
                    {"role": "system", "content": SUMMARIZE_PROMPT},
                    {"role": "user", "content": f"File: {rel}\n\n{code}"},
                ],
                max_tokens=1200,
                purpose="architecture_summary",
                metadata={"file": rel, "sequence": completed + 1,
                          "total": plan["cache_misses"]}).strip()
            raw_summary = raw_summary[:MAX_ARCH_FIELD_CHARS]
            if _degenerate(raw_summary):
                summaries[rel] = _deterministic_summary(rel, wiring)
                note(f"摘要退化，降级为结构描述：{rel}")
            else:
                summaries[rel] = raw_summary
                cache[key] = {
                    "summary": raw_summary, "model": str(model),
                    "gateway": str(gateway), "path": rel,
                    "content_hash": hashes[rel],
                    "prompt_hash": _sha(
                        SUMMARIZE_PROMPT.encode("utf-8")),
                    "schema": CACHE_SCHEMA, "ts": _now(),
                }
                # Paid work is durable before the next provider request.
                flush_cache()
            completed += 1
            progress({
                "stage": "summary", "state": "result",
                "completed": completed, "total": plan["cache_misses"],
                "file": rel,
            })
            _check_cancel(cancel)

        batches = _summary_batches(files, summaries)
        node_groups, raw_outputs = [], []
        for index, batch in enumerate(batches, 1):
            _check_cancel(cancel)
            note(f"合成 architecture {index}/{len(batches)}")
            progress({
                "stage": "synthesis", "state": "request",
                "completed": index - 1, "total": len(batches),
                "batch": index,
            })
            digest = "\n\n".join(
                f"### {rel}\n{summaries[rel]}" for rel in batch)
            raw = _invoke_complete(
                complete, [
                    {"role": "system", "content": SYNTHESIZE_PROMPT},
                    {"role": "user", "content": digest},
                ],
                max_tokens=8192,
                purpose="architecture_synthesis",
                metadata={"batch": index, "batches": len(batches),
                          "files": len(batch)})
            raw_outputs.append(f"## batch {index}\n{raw}")
            # Preserve the last paid response even when parsing or a later
            # batch fails; this is diagnostic evidence, not prompt context.
            _atomic_write_text(
                cache_dir / "last-synthesis.txt",
                "\n\n".join(raw_outputs))
            _check_cancel(cancel)
            parsed = _parse_nodes(
                raw, valid_paths=set(batch), hashes=hashes)
            if not parsed:
                hint = (
                    "（输出疑似被 max_tokens 截断）"
                    if raw.rstrip()[-1:] not in ("}", "`") else "")
                raise RepoMapError(
                    f"第 {index} 批合成结果解析不出节点{hint}；"
                    "原样返回开头：" + raw[:200])
            node_groups.append(parsed)
            progress({
                "stage": "synthesis", "state": "result",
                "completed": index, "total": len(batches),
                "batch": index,
            })

        nodes = _merge_architecture_nodes(node_groups)
        consolidation_requests = 0
        if len(batches) > 1:
            _check_cancel(cancel)
            progress({
                "stage": "consolidation", "state": "request",
                "completed": 0, "total": 1,
            })
            compact = json.dumps(
                {"nodes": nodes}, ensure_ascii=False,
                separators=(",", ":"))
            if len(compact) > ARCH_BATCH_CHARS:
                note(
                    "跨 batch consolidation 输入超过字符硬上限；"
                    "保留确定性合并结果，不发送截断 JSON")
            else:
                raw = _invoke_complete(
                    complete, [
                        {"role": "system", "content": SYNTHESIZE_PROMPT},
                        {"role": "user", "content": (
                            "下面是分批架构节点。去重并合成跨批次关系；"
                            "sources 必须保持输入中的真实路径。\n" + compact)},
                    ],
                    max_tokens=8192,
                    purpose="architecture_consolidation",
                    metadata={"batches": len(batches),
                              "candidate_nodes": len(nodes)})
                consolidation_requests = 1
                raw_outputs.append("## consolidation\n" + raw)
                _atomic_write_text(
                    cache_dir / "last-synthesis.txt",
                    "\n\n".join(raw_outputs))
                _check_cancel(cancel)
                consolidated = _parse_nodes(
                    raw, valid_paths=set(files), hashes=hashes)
                if consolidated:
                    nodes = consolidated[:MAX_ARCH_NODES]
                else:
                    note(
                        "最终 consolidation 解析失败，保留完整分批结果")
            progress({
                "stage": "consolidation", "state": "result",
                "completed": 1, "total": 1,
            })

        _atomic_write_text(
            cache_dir / "last-synthesis.txt",
            "\n\n".join(raw_outputs))
        claimed = sorted({
            source["path"] for node in nodes
            for source in node.get("sources") or []})
        manifest = {
            "schema": ARCH_SCHEMA,
            "version": VERSION,
            "built_at": _now(),
            "model": str(model),
            "gateway": str(gateway),
            "files": {
                rel: {"hash": hashes[rel], "print": prints[rel]}
                for rel in files},
            "notes": notes,
            "nodes": nodes[:MAX_ARCH_NODES],
            "coverage": {
                "claimed_files": len(claimed),
                "unclaimed_files": len(files) - len(claimed),
                "total_files": len(files),
            },
            "requests": {
                "summary": fresh,
                "synthesis": len(batches),
                "consolidation": consolidation_requests,
                "estimated": plan["estimated_requests"],
            },
        }
        generation = (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            + "-" + uuid.uuid4().hex[:12])
        manifest["generation"] = generation
        generations.mkdir(parents=True, exist_ok=True)
        temporary = generations / (
            f".tmp-{generation}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        final = generations / generation
        nodes_dir = temporary / "nodes"
        nodes_dir.mkdir(parents=True)
        try:
            for node_rec in manifest["nodes"]:
                _write_node(
                    nodes_dir, node_rec,
                    notes_tail=previous_notes.get(node_rec["slug"]))
            _atomic_write_text(
                temporary / ARCH_MANIFEST,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")))
            if callable(before_commit):
                before_commit(manifest, temporary)
            verify_notes_unchanged()
            os.replace(temporary, final)
            _fsync_directory(generations)
            _atomic_write_text(
                architecture_root(root) / ARCH_POINTER,
                json.dumps({
                    "schema": 1, "generation": generation,
                }, sort_keys=True, separators=(",", ":")))
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        _ensure_gitignore(root)
        progress({
            "stage": "done", "state": "done",
            "completed": plan["estimated_requests"],
            "total": plan["estimated_requests"],
            "generation": generation,
        })
        return manifest


def build(root, **kwargs):
    """Compatibility facade; product CLI exposes this as /architecture."""
    return build_architecture(root, **kwargs)


def _repair_truncated(blob):
    """max_tokens 截断的 JSON：退到最后一个完整节点对象再闭合。

    真机踩过：86 文件的合成图在 4000 tokens 处被砍，正文质量完好，只是
    尾巴缺闭合。丢整张图去重跑一次合成，不如把完整的前缀救回来。
    """
    for end in [m.start() for m in re.finditer(r"\}", blob)][::-1][:8]:
        prefix = blob[:end + 1]
        for tail in ("]}", "}]}", "]}}"):
            try:
                return json.loads(prefix + tail)
            except ValueError:
                continue
    return None


def _parse_nodes(raw, *, valid_paths, hashes):
    """从模型输出抠出 {\"nodes\": [...]}；关系动词与来源路径都要过闸。"""
    text = str(raw or "")
    match = re.search(r"\{.*\}", text, re.S)
    blob = match.group(0) if match else text[text.find("{"):] \
        if "{" in text else ""
    if not blob:
        return []
    try:
        payload = json.loads(blob)
    except ValueError:
        payload = _repair_truncated(blob)
        if payload is None:
            return []
    if not isinstance(payload, dict):
        return []
    raw_nodes = payload.get("nodes") or []
    if not isinstance(raw_nodes, list):
        return []
    nodes, slugs = [], set()
    for item in raw_nodes[:MAX_ARCH_NODES]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:256]
        if not name:
            continue
        slug = slugify(name)
        if slug in slugs:
            continue
        slugs.add(slug)
        raw_sources = item.get("sources") or []
        if not isinstance(raw_sources, list):
            raw_sources = []
        source_paths = sorted({
            p for p in raw_sources
            if isinstance(p, str) and p in valid_paths})[:MAX_FILES]
        sources = [
            {"path": p, "hash": hashes[p]} for p in source_paths]
        node_type = str(item.get("type") or "system")
        if node_type not in ("system", "file", "concept"):
            node_type = "system"
        nodes.append({
            "name": name, "slug": slug, "type": node_type,
            "summary": str(item.get("summary") or "").strip()[
                :MAX_ARCH_FIELD_CHARS],
            "sources": sources,
            "links": (item.get("links") or [])[:32]
            if isinstance(item.get("links") or [], list) else [],
        })
    for node_rec in nodes:
        cleaned = []
        for link in node_rec["links"]:
            if not isinstance(link, dict):
                continue
            to = slugify(str(link.get("to") or ""))
            relation = str(link.get("relation") or "")
            # 封闭集校验：集外动词整条丢弃，禁止 relates_to 式糊话。
            if relation in RELATIONS and to in slugs and to != node_rec["slug"]:
                cleaned.append({
                    "to": to, "relation": relation,
                    "description": str(link.get("description") or "")[:200]})
        node_rec["links"] = cleaned[:16]
    return nodes


def _read_node_notes(path):
    """Snapshot one human-editable Notes tail before any provider request."""
    notes_tail = "\n## Notes\n"
    path = Path(path)
    if path.is_symlink():
        raise RepoMapError(
            f"architecture Notes source 不得是 symlink：{path.name}")
    if not path.exists():
        return notes_tail, None
    if not path.is_file():
        raise RepoMapError(
            f"architecture Notes source 必须是文件：{path.name}")
    raw = _read_bounded_file(
        path, maximum=MAX_NODE_FILE_BYTES,
        label=f"architecture Notes {path.name}")
    old = raw.decode("utf-8", errors="replace")
    idx = old.find(GEN_END)
    if idx >= 0:
        tail = old[idx + len(GEN_END):]
        if tail.strip():
            notes_tail = tail if tail.startswith("\n") else "\n" + tail
    return notes_tail, raw


def _write_node(out_dir, node_rec, *, notes_tail=None):
    path = out_dir / f"{node_rec['slug']}.md"
    notes_tail = (
        str(notes_tail) if notes_tail is not None
        else "\n## Notes\n")
    lines = [f"# {node_rec['name']}", "", GEN_START,
             f"type: {node_rec['type']}", ""]
    if node_rec["summary"]:
        lines += [node_rec["summary"], ""]
    if node_rec["sources"]:
        lines.append("Sources:")
        lines += [f"- {s['path']}" for s in node_rec["sources"]]
        lines.append("")
    if node_rec["links"]:
        lines.append("Links:")
        for link in node_rec["links"]:
            desc = f" — {link['description']}" if link["description"] else ""
            lines.append(
                f"- {link['relation']} [[{link['to']}]]{desc}")
        lines.append("")
    lines.append(GEN_END)
    _atomic_write_text(path, "\n".join(lines) + notes_tail)


def _ensure_gitignore(root):
    """Keep derived map state local without editing the user's .gitignore."""
    root = Path(root)
    line = MAP_DIRNAME + "/"
    gi = root / ".gitignore"
    try:
        if gi.is_file() and not gi.is_symlink():
            raw = _read_bounded_file(
                gi, maximum=MAX_GIT_EXCLUDE_BYTES,
                label=".gitignore")
            if line in raw.decode(
                    "utf-8", errors="replace").splitlines():
                return
    except (OSError, RepoMapError):
        return
    git_dir = root / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        return
    try:
        git_dir.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return
    exclude = git_dir / "info" / "exclude"
    info_dir = exclude.parent
    if (info_dir.is_symlink() or exclude.is_symlink()
            or not info_dir.is_dir()):
        return
    try:
        info_dir.resolve().relative_to(git_dir.resolve())
    except (OSError, ValueError):
        return
    try:
        raw = (
            _read_bounded_file(
                exclude, maximum=MAX_GIT_EXCLUDE_BYTES,
                label=".git/info/exclude")
            if exclude.is_file() else b"")
        text = raw.decode("utf-8", errors="replace")
        if line not in text.splitlines():
            _atomic_write_text(
                exclude,
                (text.rstrip("\n") + "\n" if text else "")
                + line + "\n")
    except (OSError, RepoMapError):
        pass


# ------------------------------------------------------------- 陈旧检测
def _safe_relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _validate_architecture_manifest(payload, *, generation):
    if not isinstance(payload, dict):
        raise RepoMapError("architecture manifest 必须是 object")
    if generation != "legacy" and payload.get("schema") != ARCH_SCHEMA:
        raise RepoMapError("architecture manifest schema 不受支持")
    files = payload.get("files", {})
    nodes = payload.get("nodes", [])
    if generation != "legacy" and payload.get("generation") != generation:
        raise RepoMapError(
            "architecture manifest generation 与 pointer 不一致")
    for name in ("built_at", "model", "gateway"):
        value = payload.get(name, "")
        if not isinstance(value, str) or len(value) > 512:
            raise RepoMapError(
                f"architecture manifest {name} 字段无效")
    notes = payload.get("notes", [])
    if (not isinstance(notes, list) or len(notes) > MAX_FILES
            or not all(
                isinstance(value, str)
                and len(value) <= MAX_ARCH_FIELD_CHARS
                for value in notes)):
        raise RepoMapError("architecture manifest notes 无效")
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise RepoMapError(
            "architecture manifest files 超过硬上限或格式错误")
    if not isinstance(nodes, list) or len(nodes) > MAX_ARCH_NODES:
        raise RepoMapError(
            "architecture manifest nodes 超过硬上限或格式错误")
    for rel, record in files.items():
        if not _safe_relative_path(rel) or not isinstance(record, dict):
            raise RepoMapError("architecture manifest 包含无效来源路径")
        digest = record.get("hash")
        fingerprint = record.get("print")
        if not isinstance(digest, str) or len(digest) > 128:
            raise RepoMapError("architecture manifest source hash 无效")
        if (not isinstance(fingerprint, list) or len(fingerprint) != 2
                or not all(isinstance(item, int) for item in fingerprint)):
            raise RepoMapError(
                "architecture manifest source fingerprint 无效")
    known = set(files)
    slugs = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise RepoMapError("architecture manifest node 格式错误")
        name = node.get("name")
        slug = node.get("slug")
        summary = node.get("summary", "")
        node_type = node.get("type")
        if (not isinstance(name, str) or not name
                or len(name) > 256
                or not isinstance(slug, str) or not slug
                or len(slug) > 256 or slug != slugify(name)
                or not isinstance(summary, str)
                or len(summary) > MAX_ARCH_FIELD_CHARS
                or node_type not in {"system", "file", "concept"}):
            raise RepoMapError("architecture manifest node 字段无效")
        if slug in slugs:
            raise RepoMapError("architecture manifest node slug 重复")
        slugs.add(slug)
        sources = node.get("sources", [])
        links = node.get("links", [])
        if (not isinstance(sources, list) or len(sources) > MAX_FILES
                or not isinstance(links, list) or len(links) > 16):
            raise RepoMapError(
                "architecture manifest node 列表超过硬上限")
        for source in sources:
            if (not isinstance(source, dict)
                    or not isinstance(source.get("path"), str)
                    or source.get("path") not in known
                    or not isinstance(source.get("hash"), str)
                    or source.get("hash")
                    != files[source["path"]].get("hash")):
                raise RepoMapError(
                    "architecture manifest node 来源无效")
        for link in links:
            if (not isinstance(link, dict)
                    or not isinstance(link.get("to"), str)
                    or len(link.get("to", "")) > 256
                    or link.get("relation") not in RELATIONS
                    or not isinstance(
                        link.get("description", ""), str)
                    or len(link.get("description", "")) > 200):
                raise RepoMapError("architecture manifest link 无效")
    for node in nodes:
        for link in node.get("links", []):
            if link.get("to") not in slugs:
                raise RepoMapError(
                    "architecture manifest link 指向未知节点")
    result = dict(payload)
    result["generation"] = generation
    return result


def load_architecture_manifest(root):
    """Load the committed generation through a bounded validated pointer."""
    root = Path(root)
    pointer_path = architecture_root(root) / ARCH_POINTER
    if pointer_path.is_symlink():
        raise RepoMapError("architecture current pointer 不得是 symlink")
    pointer = _load_json_bounded(pointer_path, maximum=16 * 1024)
    if pointer is not None:
        generation = (
            pointer.get("generation")
            if isinstance(pointer, dict) else None)
        if (not isinstance(pointer, dict) or pointer.get("schema") != 1
                or not isinstance(generation, str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", generation)):
            raise RepoMapError("architecture current pointer 无效")
        generation_dir = _derived_child(
            _architecture_generations(root), generation,
            "architecture generation authority")
        manifest_path = generation_dir / ARCH_MANIFEST
        if generation_dir.is_symlink() or manifest_path.is_symlink():
            raise RepoMapError(
                "architecture generation 不得经过 symlink")
        payload = _load_json_bounded(
            manifest_path, maximum=MAX_ARCH_MANIFEST_BYTES)
        if payload is None:
            raise RepoMapError(
                "architecture current generation 不完整")
        validated = _validate_architecture_manifest(
            payload, generation=generation)
        nodes_dir = _derived_child(
            generation_dir, "nodes", "architecture nodes authority")
        if not nodes_dir.is_dir():
            raise RepoMapError(
                "architecture current generation 缺少 nodes 目录")
        for node in validated["nodes"]:
            node_path = nodes_dir / f"{node['slug']}.md"
            if node_path.is_symlink() or not node_path.is_file():
                raise RepoMapError(
                    "architecture current generation 缺少安全节点文件："
                    f"{node['slug']}.md")
        return validated

    # One-way compatibility for the pre-generation cache layout.
    legacy_path = map_dir(root) / MANIFEST
    if legacy_path.is_symlink():
        raise RepoMapError(
            "legacy architecture manifest 不得是 symlink")
    legacy = _load_json_bounded(
        legacy_path, maximum=MAX_ARCH_MANIFEST_BYTES)
    if legacy is None:
        return None
    return _validate_architecture_manifest(
        legacy, generation="legacy")


def load_manifest(root):
    """Compatibility alias for callers written before /architecture split."""
    return load_architecture_manifest(root)


def check_architecture(root, *, hash_check=True, max_files=MAX_FILES):
    """漂移报告。O(files)，不调模型、不读节点正文。

    hash_check=False 时只比 size+mtime 指纹（session 启动用的快路径）。
    """
    manifest = load_architecture_manifest(root)
    if not manifest:
        return None
    root = Path(root)
    changed, removed = [], []
    for rel, rec in (manifest.get("files") or {}).items():
        path = root / rel
        if path.is_symlink() or not path.is_file():
            removed.append(rel)
            continue
        if hash_check:
            try:
                digest = _sha(_source_snapshot(root, rel)[0])
            except RepoMapError:
                changed.append(rel)
                continue
            if digest != rec.get("hash"):
                changed.append(rel)
        else:
            try:
                fingerprint = _source_fingerprint(root, rel)
            except RepoMapError:
                changed.append(rel)
                continue
            if fingerprint != rec.get("print"):
                changed.append(rel)
    known = set(manifest.get("files") or {})
    current, _ = discover_files(root, max_files=max_files)
    added = [rel for rel in current if rel not in known]
    dirty = set(changed) | set(removed)
    stale_nodes = sorted(
        node["slug"] for node in (manifest.get("nodes") or [])
        if any(s["path"] in dirty for s in node.get("sources") or []))
    return {"changed": changed, "removed": removed, "added": added,
            "stale_nodes": stale_nodes,
            "fresh": not (changed or removed or added)}


def check(root, *, hash_check=True, max_files=MAX_FILES):
    """Compatibility alias for the provider-backed architecture graph."""
    return check_architecture(
        root, hash_check=hash_check, max_files=max_files)


# ------------------------------------------------------------- 注入索引
def architecture_index_text(root, *, max_chars=MAX_INDEX_CHARS,
                            max_files=MAX_FILES):
    """有界的地图索引。地图不存在 → 空串（零注入零噪音）。

    只用 manifest + 指纹快路径 —— 这里在 session 启动热路径上，
    绝不逐文件读内容算哈希，更不调模型。
    """
    manifest = load_architecture_manifest(root)
    if not manifest or not manifest.get("nodes"):
        return ""
    drift = check_architecture(
        root, hash_check=False, max_files=max_files) or {}
    stale = set(drift.get("stale_nodes") or [])
    nodes_dir = architecture_node_dir(root, manifest)
    relative_nodes = os.path.relpath(nodes_dir, Path(root))
    lines = [f"# architecture（{len(manifest['nodes'])} 节点 · "
             f"构建于 {manifest.get('built_at', '?')}）",
             f"节点文件在 {relative_nodes}/，"
             "用 read_file 顺 [[wikilink]] 走。"]
    if drift and not drift.get("fresh"):
        lines.append("⚠ 代码已领先于地图（结构指纹漂移）；"
                     "陈旧节点已标注，必要时提醒用户 "
                     "/architecture build。")
    for node_rec in manifest["nodes"]:
        first = node_rec.get("summary", "").split("。")[0][:120]
        mark = " ⚠stale" if node_rec["slug"] in stale else ""
        lines.append(
            f"- [[{node_rec['slug']}]] ({node_rec['type']}){mark}: {first}")
    text = "\n".join(lines)
    budget = min(max(0, int(max_chars)), MAX_INDEX_CHARS_HARD)
    if budget == 0:
        return ""
    if len(text) > budget:
        marker = "\n[architecture 索引截断]"
        text = text[:max(0, budget - len(marker))] + marker
    return text


def index_text(root, *, max_chars=MAX_INDEX_CHARS, max_files=MAX_FILES):
    """Compatibility alias for the architecture context projection."""
    return architecture_index_text(
        root, max_chars=max_chars, max_files=max_files)


# ------------------------------------------------------------- 可视化
def render_mermaid(manifest):
    """mermaid 流程图导出（Cursor/GitHub 的 markdown 预览原生渲染）。"""
    lines = ["flowchart LR"]
    for n in manifest.get("nodes") or []:
        shape = ("([{}])" if n["type"] == "concept"
                 else "[{}]" if n["type"] == "system" else "[[{}]]")
        label = re.sub(r'[\[\]{}()|"<>`]', "'", n["name"])
        lines.append(f'    {n["slug"]}{shape.format(label)}')
    for n in manifest.get("nodes") or []:
        for l in n.get("links") or []:
            lines.append(
                f'    {n["slug"]} -- {l["relation"]} --> {l["to"]}')
    return "\n".join(lines) + "\n"


def render_viz_html(manifest, drift=None):
    """自足单文件 HTML：canvas 力导向图，零外链（无 CDN，file:// 直开）。

    交互语言照抄 graft viz：节点按类型着色、按连接度定大小；选中节点时
    入边 teal（谁依赖我）、出边 amber（我依赖谁）；陈旧节点红色虚线环 +
    呼吸动画；搜索即跳转；侧栏显示摘要/来源/链接。
    """
    stale = set((drift or {}).get("stale_nodes") or [])
    data = {
        "built": manifest.get("built_at", "?"),
        "model": manifest.get("model", "?"),
        "nodes": [{
            "slug": n["slug"], "name": n["name"], "type": n["type"],
            "summary": n.get("summary", ""),
            "sources": [s["path"] for s in n.get("sources") or []],
            "stale": n["slug"] in stale,
        } for n in manifest.get("nodes") or []],
        "edges": [{
            "from": n["slug"], "to": l["to"], "rel": l["relation"],
        } for n in manifest.get("nodes") or [] for l in n.get("links") or []],
    }
    payload = json.dumps(data, ensure_ascii=False)
    # `<` 转义成 \u003c：JSON 字符串内合法，且使 `</script>` 无法在
    # 浏览器解析层出现 —— 节点名/摘要来自仓库内容与模型输出，都不可信
    # （F3，REVIEW-repo-map-20260828：曾可被 </script><script> 逃逸执行）。
    payload = payload.replace("<", "\\u003c")
    return _VIZ_TEMPLATE.replace("__DATA__", payload)


_VIZ_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>zylab repo-map</title>
<style>
  html,body{margin:0;height:100%;background:#12141a;color:#d8dce6;
    font:13px/1.5 -apple-system,'Segoe UI',sans-serif;overflow:hidden}
  #bar{position:fixed;top:0;left:0;right:0;padding:8px 14px;display:flex;
    gap:14px;align-items:center;background:#181b23cc;backdrop-filter:blur(4px);
    border-bottom:1px solid #262b38;z-index:2}
  #bar b{color:#ffb454}  #bar .dim{color:#6a7183;font-size:12px}
  #q{background:#0f1116;border:1px solid #2c3242;color:#d8dce6;border-radius:6px;
    padding:4px 10px;width:200px;outline:none}
  #legend{margin-left:auto;display:flex;gap:12px;font-size:12px;color:#8a92a6}
  .sw{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
  canvas{display:block}
  #panel{position:fixed;top:46px;right:0;bottom:0;width:330px;overflow:auto;
    background:#161923ee;border-left:1px solid #262b38;padding:16px;
    transform:translateX(100%);transition:transform .18s;z-index:2}
  #panel.open{transform:none}
  #panel h2{margin:0 0 2px;font-size:16px;color:#ffb454}
  #panel .type{font-size:11px;color:#6a7183;text-transform:uppercase}
  #panel .stale{color:#ff6b6b;font-size:12px}
  #panel p{color:#c3c9d6}
  #panel h3{font-size:12px;color:#8a92a6;margin:14px 0 4px}
  #panel li{list-style:none;font-size:12px;color:#9aa2b5;margin:2px 0}
  #panel a{color:#73d0ff;cursor:pointer;text-decoration:none}
  #hint{position:fixed;left:14px;bottom:10px;font-size:11px;color:#565d70;z-index:2}
</style></head><body>
<div id="bar"><b>repo-map</b>
  <input id="q" placeholder="搜索节点… (Enter 跳转)">
  <span class="dim" id="meta"></span>
  <div id="legend">
    <span><span class="sw" style="background:#73d0ff"></span>system</span>
    <span><span class="sw" style="background:#ffb454"></span>concept</span>
    <span><span class="sw" style="background:#8bd49c"></span>file</span>
    <span><span class="sw" style="background:none;border:1px dashed #ff6b6b"></span>stale</span>
    <span style="color:#5ccfe6">→入边=谁依赖我</span>
    <span style="color:#ffb454">出边=我依赖谁→</span>
  </div></div>
<canvas id="c"></canvas>
<div id="panel"></div>
<div id="hint">拖节点移动 · 拖背景平移 · 滚轮缩放 · 点击看详情 · Esc 关闭</div>
<script type="application/json" id="mapdata">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById("mapdata").textContent);
const TYPE_COLOR = {system:"#73d0ff", concept:"#ffb454", file:"#8bd49c"};
const cv = document.getElementById("c"), ctx = cv.getContext("2d");
const meta = document.getElementById("meta");
meta.textContent = `${DATA.nodes.length} 节点 · ${DATA.edges.length} 边 · ` +
  `构建于 ${DATA.built} · ${DATA.model}`;
let W, H, dpr = devicePixelRatio || 1;
function resize(){ W=innerWidth; H=innerHeight; cv.width=W*dpr; cv.height=H*dpr;
  cv.style.width=W+"px"; cv.style.height=H+"px"; ctx.setTransform(dpr,0,0,dpr,0,0); }
addEventListener("resize", resize); resize();

const nodes = DATA.nodes.map((n,i)=>({...n,
  x: W/2 + Math.cos(i/DATA.nodes.length*6.283)*Math.min(W,H)*0.32,
  y: H/2 + Math.sin(i/DATA.nodes.length*6.283)*Math.min(W,H)*0.32,
  vx:0, vy:0, deg:0}));
const bySlug = Object.fromEntries(nodes.map(n=>[n.slug,n]));
const edges = DATA.edges.filter(e=>bySlug[e.from]&&bySlug[e.to]);
edges.forEach(e=>{bySlug[e.from].deg++; bySlug[e.to].deg++;});
nodes.forEach(n=>{ n.r = 7 + Math.min(14, n.deg*1.6); });

let cam = {x:0, y:0, k:1}, selected=null, hover=null, dragN=null, panning=null;
function sim(){
  for (const a of nodes){ for (const b of nodes){ if(a===b) continue;
    let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy+40, f=2600/d2;
    a.vx+=dx*f/Math.sqrt(d2); a.vy+=dy*f/Math.sqrt(d2); }}
  for (const e of edges){ const s=bySlug[e.from], t=bySlug[e.to];
    let dx=t.x-s.x, dy=t.y-s.y, d=Math.hypot(dx,dy)||1, f=(d-150)*0.004;
    s.vx+=dx/d*f; s.vy+=dy/d*f; t.vx-=dx/d*f; t.vy-=dy/d*f; }
  for (const n of nodes){ n.vx+=(W/2-n.x)*0.0009; n.vy+=(H/2-n.y)*0.0009;
    if(n!==dragN){ n.x+=n.vx*=0.86; n.y+=n.vy*=0.86; } }
}
function draw(t){
  ctx.clearRect(0,0,W,H); ctx.save();
  ctx.translate(cam.x,cam.y); ctx.scale(cam.k,cam.k);
  for (const e of edges){ const s=bySlug[e.from], d=bySlug[e.to];
    let col="#2c3242", w=1;
    if (selected){ if(e.from===selected.slug){col="#ffb454";w=1.8;}
      else if(e.to===selected.slug){col="#5ccfe6";w=1.8;}
      else col="#20242f"; }
    ctx.strokeStyle=col; ctx.lineWidth=w;
    ctx.beginPath(); ctx.moveTo(s.x,s.y); ctx.lineTo(d.x,d.y); ctx.stroke();
    const mx=(s.x+d.x)/2, my=(s.y+d.y)/2;
    if (selected && (e.from===selected.slug||e.to===selected.slug) && cam.k>0.7){
      ctx.fillStyle="#8a92a6"; ctx.font="10px sans-serif";
      ctx.fillText(e.rel, mx+4, my-4); }
    const ang=Math.atan2(d.y-s.y,d.x-s.x);
    const ax=d.x-Math.cos(ang)*(d.r+4), ay=d.y-Math.sin(ang)*(d.r+4);
    ctx.fillStyle=col; ctx.beginPath();
    ctx.moveTo(ax,ay);
    ctx.lineTo(ax-Math.cos(ang-0.4)*7, ay-Math.sin(ang-0.4)*7);
    ctx.lineTo(ax-Math.cos(ang+0.4)*7, ay-Math.sin(ang+0.4)*7);
    ctx.fill(); }
  for (const n of nodes){
    const dimmed = selected && n!==selected &&
      !edges.some(e=>(e.from===selected.slug&&e.to===n.slug)||
                     (e.to===selected.slug&&e.from===n.slug));
    ctx.globalAlpha = dimmed? 0.25 : 1;
    if (n.stale){ ctx.strokeStyle="#ff6b6b"; ctx.setLineDash([4,3]);
      ctx.lineWidth=1.5; ctx.beginPath();
      ctx.arc(n.x,n.y,n.r+4+Math.sin(t/400)*1.6,0,6.283); ctx.stroke();
      ctx.setLineDash([]); }
    ctx.fillStyle = TYPE_COLOR[n.type]||"#9aa2b5";
    ctx.beginPath(); ctx.arc(n.x,n.y,n.r,0,6.283); ctx.fill();
    if (n===selected||n===hover){ ctx.strokeStyle="#fff";
      ctx.lineWidth=1.5; ctx.stroke(); }
    ctx.globalAlpha = dimmed? 0.35 : 1;
    ctx.fillStyle="#d8dce6"; ctx.font="11px sans-serif";
    ctx.fillText(n.name, n.x+n.r+5, n.y+4);
    ctx.globalAlpha = 1; }
  ctx.restore();
}
function loop(t){ sim(); draw(t); requestAnimationFrame(loop); }
requestAnimationFrame(loop);
function pick(mx,my){ const x=(mx-cam.x)/cam.k, y=(my-cam.y)/cam.k;
  return nodes.find(n=>Math.hypot(n.x-x,n.y-y)<n.r+4); }
cv.onmousedown=e=>{ const n=pick(e.clientX,e.clientY);
  if(n) dragN=n; else panning={x:e.clientX-cam.x,y:e.clientY-cam.y}; };
addEventListener("mousemove",e=>{
  if(dragN){ dragN.x=(e.clientX-cam.x)/cam.k; dragN.y=(e.clientY-cam.y)/cam.k; }
  else if(panning){ cam.x=e.clientX-panning.x; cam.y=e.clientY-panning.y; }
  else hover=pick(e.clientX,e.clientY); });
addEventListener("mouseup",e=>{
  if(dragN && Math.hypot(dragN.vx,dragN.vy)<3){ select(dragN); }
  else if(panning){ const n=pick(e.clientX,e.clientY);
    if(!n && Math.abs(e.clientX-(panning.x+cam.x))<3) select(null); }
  dragN=null; panning=null; });
cv.onwheel=e=>{ e.preventDefault();
  const k=Math.min(3,Math.max(0.3,cam.k*(e.deltaY<0?1.12:0.9)));
  cam.x=e.clientX-(e.clientX-cam.x)*k/cam.k;
  cam.y=e.clientY-(e.clientY-cam.y)*k/cam.k; cam.k=k; };
const panel=document.getElementById("panel");
// 节点字段一律 textContent，不进任何 HTML 解析路径（F3）。
function el(tag, cls, text){ const d=document.createElement(tag);
  if(cls) d.className=cls; if(text!==undefined) d.textContent=text; return d; }
function jumpLink(slug){ const a=el("a", "", bySlug[slug].name);
  a.onclick=()=>{ const m=bySlug[slug]; select(m);
    cam.x=W/2-m.x*cam.k; cam.y=H/2-m.y*cam.k; }; return a; }
function select(n){ selected=n;
  if(!n){ panel.classList.remove("open"); return; }
  const outs=edges.filter(e=>e.from===n.slug),
        ins=edges.filter(e=>e.to===n.slug);
  panel.replaceChildren();
  const type=el("div","type",n.type);
  if(n.stale) type.appendChild(el("span","stale"," · ⚠ stale"));
  panel.appendChild(type);
  panel.appendChild(el("h2","",n.name));
  panel.appendChild(el("p","",n.summary));
  panel.appendChild(el("h3","","我依赖谁（amber）"));
  let ul=el("ul");
  if(outs.length) outs.forEach(e=>{ const li=el("li","",e.rel+" → ");
    li.appendChild(jumpLink(e.to)); ul.appendChild(li); });
  else ul.appendChild(el("li","","—"));
  panel.appendChild(ul);
  panel.appendChild(el("h3","","谁依赖我（teal）"));
  ul=el("ul");
  if(ins.length) ins.forEach(e=>{ const li=el("li");
    li.appendChild(jumpLink(e.from));
    li.appendChild(document.createTextNode(" "+e.rel+" →"));
    ul.appendChild(li); });
  else ul.appendChild(el("li","","—"));
  panel.appendChild(ul);
  panel.appendChild(el("h3","","Sources"));
  ul=el("ul");
  n.sources.forEach(src=>ul.appendChild(el("li","",src)));
  panel.appendChild(ul);
  panel.classList.add("open"); }
document.getElementById("q").addEventListener("keydown",e=>{
  if(e.key!=="Enter") return;
  const q=e.target.value.toLowerCase();
  const n=nodes.find(n=>n.name.toLowerCase().includes(q)||n.slug.includes(q));
  if(n){ select(n); cam.x=W/2-n.x*cam.k; cam.y=H/2-n.y*cam.k; } });
addEventListener("keydown",e=>{ if(e.key==="Escape") select(null); });
</script></body></html>
"""


def export_visualizations(root, *, kind, html, mermaid):
    """Atomically replace fixed derived outputs; never follow output symlinks."""
    names = {
        "map": ("map.html", "map.mmd"),
        "architecture": ("viz.html", "graph.mmd"),
    }
    if kind not in names:
        raise RepoMapError(f"未知 visualization kind：{kind}")
    out = map_dir(root)
    out.mkdir(parents=True, exist_ok=True)
    html_name, mermaid_name = names[kind]
    html_path = out / html_name
    mermaid_path = out / mermaid_name
    _atomic_write_text(html_path, html)
    _atomic_write_text(mermaid_path, mermaid)
    return html_path, mermaid_path
