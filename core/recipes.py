"""可保存、可校验、无代码求值的 workflow recipes。"""
from __future__ import annotations
from . import paths

import json
import os
import re
from pathlib import Path

from . import models, store


VERSION = 1
USER_ROOT = store.HOME / "workflow-recipes"
PROJECT_RELATIVE = Path(paths.project_dirname()) / "workflows"
NAME = re.compile(r"^[a-z][a-z0-9_-]{1,47}$")
PARAM = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
PLACEHOLDER = re.compile(r"\$\{([A-Za-z][A-Za-z0-9_]{0,31})\}")
SEATS = set(models.WORKFLOW_SEAT_NAMES)
SCOPES = {"builtin", "user", "project"}
MAX_RECIPE_BYTES = 200_000
MAX_DIRECTORY_ENTRIES = 2_000
MAX_RECIPE_FILES = 256


BUILTINS = {
    "research-review": {
        "version": VERSION,
        "name": "research-review",
        "description": "三路独立研究、两席对抗审查、Kimi 综合",
        "params": {
            "goal": {"required": True, "description": "研究问题"},
        },
        "workflow": {
            "goal": "${goal}", "mode": "research",
            "agents": [
                {"key": "evidence", "seat": "Qwen",
                 "role": "evidence", "task": "收集可复核证据：${goal}"},
                {"key": "counter", "seat": "GLM",
                 "role": "counterexample", "task": "寻找反例与遗漏：${goal}"},
                {"key": "mechanism", "seat": "DeepSeek",
                 "role": "mechanism", "task": "分析机制和判别测试：${goal}"},
            ],
            "review": {"enabled": True,
                       "reviewers": ["Kimi", "GLM"], "rounds": 1},
            "synthesis": {"seat": "Kimi"},
            "budget": 8,
        },
    },
    "debug-triangulate": {
        "version": VERSION,
        "name": "debug-triangulate",
        "description": "复现、代码路径与边界条件三角定位",
        "params": {
            "goal": {"required": True, "description": "故障与验收目标"},
        },
        "workflow": {
            "goal": "${goal}", "mode": "debug",
            "agents": [
                {"key": "repro", "seat": "Qwen", "role": "reproduction",
                 "task": "设计最小复现并收集原始证据：${goal}"},
                {"key": "path", "seat": "DeepSeek", "role": "root-cause",
                 "task": "独立追踪执行路径与根因候选：${goal}"},
                {"key": "edges", "seat": "GLM", "role": "edge-cases",
                 "task": "检查并发、恢复、安全与兼容边界：${goal}"},
                {"key": "verdict", "seat": "Kimi", "role": "verifier",
                 "task": "交叉核验三路证据并提出最小修复门槛：${goal}",
                 "depends_on": ["repro", "path", "edges"]},
            ],
            "synthesis": {"seat": "Kimi"},
            "budget": 6,
        },
    },
    "implementation-audit": {
        "version": VERSION,
        "name": "implementation-audit",
        "description": "实现前并行审计代码、测试、安全与性能",
        "params": {
            "goal": {"required": True, "description": "实施目标"},
        },
        "workflow": {
            "goal": "${goal}", "mode": "implement",
            "agents": [
                {"key": "code", "seat": "Qwen", "role": "code",
                 "task": "定位最小可维护实现路径：${goal}"},
                {"key": "tests", "seat": "DeepSeek", "role": "tests",
                 "task": "设计失败判据与回归矩阵：${goal}"},
                {"key": "security", "seat": "GLM", "role": "security",
                 "task": "审计权限、输入与持久化边界：${goal}"},
                {"key": "performance", "seat": "Kimi", "role": "performance",
                 "task": "审计延迟、资源与可简化点：${goal}"},
            ],
            "review": {"enabled": True,
                       "reviewers": ["DeepSeek"], "rounds": 1},
            "synthesis": {"seat": "Kimi"},
            "budget": 7,
        },
    },
}


class RecipeError(RuntimeError):
    pass


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _under(path, root):
    path = Path(path).resolve(strict=False)
    root = Path(root).resolve(strict=False)
    return path == root or root in path.parents


def _under_protected(path):
    return paths.under_protected(path)


def project_root(cwd=None):
    return paths.project_dir(Path(cwd or os.getcwd()).resolve(strict=False)) / "workflows"


def _read_file(path, *, scope):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RecipeError(f"recipe 不能是符号链接或非文件：{path}")
    if path.suffix.lower() != ".json":
        raise RecipeError(f"recipe 必须是 .json：{path}")
    try:
        size = path.stat().st_size
        if size > MAX_RECIPE_BYTES:
            raise RecipeError(
                f"recipe 超过 {MAX_RECIPE_BYTES:,} bytes：{path}")
        raw = path.read_bytes()
        if len(raw) > MAX_RECIPE_BYTES:
            raise RecipeError(
                f"recipe 读取后超过 {MAX_RECIPE_BYTES:,} bytes：{path}")
        value = json.loads(raw.decode("utf-8"))
    except RecipeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecipeError(
            f"recipe 无法读取：{type(exc).__name__}: {exc}") from None
    return validate(value, source=str(path), scope=scope)


def validate(value, *, source="<memory>", scope="user"):
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise RecipeError(f"{source}: recipe version 必须是 {VERSION}")
    name = str(value.get("name") or "")
    if not NAME.fullmatch(name):
        raise RecipeError(f"{source}: name 必须匹配 {NAME.pattern}")
    params = value.get("params") or {}
    if not isinstance(params, dict) or len(params) > 32:
        raise RecipeError(f"{source}: params 必须是最多 32 项的 object")
    for key, spec in params.items():
        if not PARAM.fullmatch(str(key)) or not isinstance(spec, dict):
            raise RecipeError(f"{source}: 无效 param {key!r}")
        if "default" in spec and len(str(spec["default"])) > 4000:
            raise RecipeError(f"{source}: param {key} default 超过 4000 字符")
    workflow = value.get("workflow")
    if not isinstance(workflow, dict):
        raise RecipeError(f"{source}: workflow 必须是 object")
    goal = workflow.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise RecipeError(f"{source}: workflow.goal 不能为空")
    agents = workflow.get("agents")
    if not isinstance(agents, list) or not 2 <= len(agents) <= 64:
        raise RecipeError(f"{source}: workflow.agents 必须是 2..64 项")
    keys = []
    for index, item in enumerate(agents, 1):
        if not isinstance(item, dict):
            raise RecipeError(f"{source}: agents[{index}] 必须是 object")
        key = str(item.get("key") or "")
        if not PARAM.fullmatch(key) or key in keys:
            raise RecipeError(f"{source}: agents[{index}].key 无效或重复")
        keys.append(key)
        if not str(item.get("task") or "").strip():
            raise RecipeError(f"{source}: agents[{index}].task 不能为空")
        seat = str(item.get("seat") or "")
        if seat not in set(models.WORKFLOW_SEAT_NAMES):   # 席位可配，动态取
            raise RecipeError(f"{source}: agents[{index}].seat 无效：{seat!r}")
        dependencies = item.get("depends_on") or []
        if not isinstance(dependencies, list) or len(dependencies) > 64:
            raise RecipeError(f"{source}: agents[{index}].depends_on 必须是 array")
        if (any(not isinstance(dependency, str)
                or not PARAM.fullmatch(dependency)
                for dependency in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise RecipeError(
                f"{source}: agents[{index}].depends_on 含无效或重复 key")
    by_key = {str(item["key"]): item for item in agents}
    colors = {key: 0 for key in by_key}

    def visit(key):
        colors[key] = 1
        for dependency in by_key[key].get("depends_on") or []:
            if dependency not in by_key:
                raise RecipeError(
                    f"{source}: {key} 依赖不存在的 {dependency!r}")
            if colors[dependency] == 1:
                raise RecipeError(f"{source}: DAG 存在循环")
            if colors[dependency] == 0:
                visit(dependency)
        colors[key] = 2

    for key in by_key:
        if colors[key] == 0:
            visit(key)
    placeholders = set(PLACEHOLDER.findall(json.dumps(
        workflow, ensure_ascii=False)))
    unknown = placeholders - set(params)
    if unknown:
        raise RecipeError(
            f"{source}: 未声明 placeholders：{', '.join(sorted(unknown))}")
    record = _copy(value)
    record["source"] = str(source)
    record["scope"] = str(scope)
    return record


def _files(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return [], []
    paths, errors = [], []
    try:
        with os.scandir(root) as iterator:
            for scanned, entry in enumerate(iterator):
                if scanned >= MAX_DIRECTORY_ENTRIES:
                    errors.append(
                        f"recipe root 超过扫描上限 "
                        f"{MAX_DIRECTORY_ENTRIES}：{root}")
                    break
                path = Path(entry.path)
                if (entry.name.casefold().endswith(".json")
                        and not entry.is_symlink()
                        and entry.is_file(follow_symlinks=False)):
                    paths.append(path)
    except OSError as exc:
        return [], [f"recipe root 无法读取：{type(exc).__name__}: {exc}"]
    paths.sort(key=lambda path: path.name.casefold())
    if len(paths) > MAX_RECIPE_FILES:
        errors.append(
            f"recipe files 超过上限 {MAX_RECIPE_FILES}：{root}")
        paths = paths[:MAX_RECIPE_FILES]
    return paths, errors


def list_recipes(cwd=None):
    rows = []
    for name, value in BUILTINS.items():
        rows.append(validate(value, source=f"builtin:{name}", scope="builtin"))
    for scope, root in (("user", USER_ROOT), ("project", project_root(cwd))):
        paths, scan_errors = _files(root)
        for error in scan_errors:
            rows.append({
                "version": VERSION, "name": f"[{scope}-catalog]",
                "description": "", "params": {}, "workflow": {},
                "source": str(root), "scope": scope, "error": error,
            })
        for path in paths:
            try:
                rows.append(_read_file(path, scope=scope))
            except RecipeError as exc:
                rows.append({
                    "version": VERSION, "name": path.stem,
                    "description": "", "params": {}, "workflow": {},
                    "source": str(path), "scope": scope, "error": str(exc),
                })
    rows.sort(key=lambda item: (item.get("name", ""), item.get("scope", "")))
    return rows


def resolve(name_or_path, cwd=None):
    hint = str(name_or_path or "").strip()
    if not hint:
        raise RecipeError("recipe 名称不能为空")
    candidate = Path(os.path.expanduser(hint))
    if candidate.suffix.lower() == ".json" or "/" in hint:
        candidate = candidate.resolve(strict=False)
        allowed = (Path(cwd or os.getcwd()).resolve(strict=False),
                   USER_ROOT.resolve(strict=False),
                   project_root(cwd).resolve(strict=False))
        if not any(_under(candidate, root) for root in allowed):
            raise RecipeError("显式 recipe 路径必须位于 cwd 或 recipe 目录")
        return _read_file(candidate, scope="path")
    scope = None
    name = hint
    if ":" in hint:
        prefix, name = hint.split(":", 1)
        if prefix in SCOPES:
            scope = prefix
        else:
            name = hint
    matches = [row for row in list_recipes(cwd)
               if row.get("name") == name
               and (scope is None or row.get("scope") == scope)]
    if len(matches) == 1:
        if matches[0].get("error"):
            raise RecipeError(matches[0]["error"])
        return matches[0]
    if not matches:
        raise RecipeError(f"没有 recipe {hint!r}")
    raise RecipeError(
        f"recipe {name!r} 有 {len(matches)} 个来源；请用 scope:name 消歧")


def instantiate(recipe, values=None):
    recipe = validate(
        {key: _copy(recipe[key]) for key in (
            "version", "name", "description", "params", "workflow")
         if key in recipe},
        source=recipe.get("source", "<memory>"),
        scope=recipe.get("scope", "user"))
    supplied = dict(values or {})
    unknown = set(supplied) - set(recipe["params"])
    if unknown:
        raise RecipeError("未知参数：" + ", ".join(sorted(unknown)))
    resolved = {}
    for key, spec in recipe["params"].items():
        if key in supplied:
            value = str(supplied[key])
        elif "default" in spec:
            value = str(spec["default"])
        elif spec.get("required", False):
            raise RecipeError(f"缺少必需参数：{key}")
        else:
            value = ""
        if len(value) > 4000:
            raise RecipeError(f"参数 {key} 超过 4000 字符")
        resolved[key] = value

    def substitute(value):
        if isinstance(value, str):
            return PLACEHOLDER.sub(lambda match: resolved[match.group(1)], value)
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        return value

    workflow = substitute(recipe["workflow"])
    encoded = json.dumps(workflow, ensure_ascii=False)
    if len(encoded) > 100_000:
        raise RecipeError("实例化后的 workflow 超过 100000 字符")
    workflow["recipe"] = {
        "name": recipe["name"], "scope": recipe["scope"],
        "source": recipe["source"], "params": sorted(resolved),
    }
    return workflow


def from_workflow(record, name, *, description=""):
    if not NAME.fullmatch(str(name or "")):
        raise RecipeError(f"name 必须匹配 {NAME.pattern}")
    plan = record.get("plan") or {}
    agents = []
    for node in plan.get("agents") or []:
        agents.append({key: _copy(node.get(key)) for key in (
            "key", "seat", "task", "role", "context", "depends_on")})
    review = plan.get("review") or {}
    reviewers = []
    for item in review.get("reviewers") or []:
        if isinstance(item, dict) and item.get("seat"):
            reviewers.append(item["seat"])
        elif isinstance(item, str):
            reviewers.append(item)
    synthesis = plan.get("synthesis") or None
    recipe = {
        "version": VERSION,
        "name": str(name),
        "description": str(description or record.get("goal") or "")[:240],
        "params": {},
        "workflow": {
            "goal": str(record.get("goal") or ""),
            "mode": str(record.get("mode") or "research"),
            "agents": agents,
            "review": {
                "enabled": bool(review.get("enabled")),
                "reviewers": reviewers,
                "rounds": int(review.get("rounds") or 1),
            },
            "synthesis": (
                {"seat": synthesis.get("seat")}
                if isinstance(synthesis, dict) and synthesis.get("seat")
                else None),
            "budget": int((record.get("budget") or {}).get(
                "max_requests") or max(2, len(agents))),
        },
    }
    return validate(recipe, source="<workflow>", scope="user")


def save(recipe, *, scope="user", cwd=None, overwrite=False):
    if scope not in {"user", "project"}:
        raise RecipeError("save scope 必须是 user 或 project")
    checked = validate(
        {key: _copy(recipe[key]) for key in (
            "version", "name", "description", "params", "workflow")},
        source="<save>", scope=scope)
    root = USER_ROOT if scope == "user" else project_root(cwd)
    if _under_protected(root):
        raise RecipeError(f"不能把 recipe 写入受保护路径：{root}")
    boundary = (
        store.HOME.resolve(strict=False)
        if scope == "user"
        else Path(cwd or os.getcwd()).resolve(strict=False))
    existing = root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    resolved_existing = existing.resolve(strict=False)
    if (existing.is_symlink()
            or not (_under(boundary, resolved_existing)
                    or _under(resolved_existing, boundary))):
        raise RecipeError(
            f"recipe root 的已存在父目录越过安全边界：{existing}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not _under(root.resolve(strict=False), boundary):
        raise RecipeError(f"recipe root 越过安全边界：{root}")
    path = root / f"{checked['name']}.json"
    if path.is_symlink():
        raise RecipeError(f"recipe 不能覆盖符号链接：{path}")
    if path.exists() and not overwrite:
        raise RecipeError(f"recipe 已存在：{path}；显式 overwrite 才能替换")
    payload = {key: checked[key] for key in (
        "version", "name", "description", "params", "workflow")}
    store._atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n")
    return path
