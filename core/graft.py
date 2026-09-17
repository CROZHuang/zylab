"""Controlled adapter for a hosted Graft structural code index."""
from __future__ import annotations
from . import paths

import functools
import hashlib
import json
import os
import re
import shutil
import signal
import stat
from . import wincompat
import subprocess
import sys
from pathlib import Path

from . import attachments, store


# 两个集合，两件事，别再合并（2026-09-15 合并过一次，plan 模式与权限钉死测试当场红）：
#   TOOL_NAMES      只读查询工具 —— 同时是「SAFE / READ_ONLY / 默认 allow」的那一批，
#                   plan 模式下也放行；
#   ALL_TOOL_NAMES  sidecar 提供的**全部**工具，缺席时按这张表整体摘掉。
# graft_index 会写缓存、烧几十秒 CPU，刻意不进 SAFE，所以只属于后者。漏进后者
# 就等于给模型留了一个必然失败的工具（09-09 新增时就漏在外面，09-15 实测）。
TOOL_NAMES = frozenset({
    "graft_find_code", "graft_file_api", "graft_trace_calls",
    "graft_find_all", "graft_repo_map",
})
ALL_TOOL_NAMES = TOOL_NAMES | frozenset({"graft_index"})
ACTION_BY_TOOL = {
    # graft_index 的具体 action 由参数决定，不在这张表里；表只服务固定映射的查询工具
    "graft_find_code": "find_code",
    "graft_file_api": "file_api",
    "graft_trace_calls": "trace_calls",
    "graft_find_all": "find_all",
    "graft_repo_map": "repo_map",
}
SUPPORTED_EXTENSIONS = frozenset({
    ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".pyi", ".go", ".java", ".kt", ".kts", ".php", ".r",
    ".rs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh",
    ".rb", ".cs", ".scala", ".sc", ".swift", ".ex", ".exs", ".sol",
    ".ml", ".mli", ".zig", ".dart", ".clj", ".cljs", ".cljc", ".bb",
    ".nix", ".lua", ".vue",
})
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", paths.project_dirname(), paths.PORTABLE_DIRNAME,
    ".codex", ".claude",
    "node_modules", "vendor", "dist", "build", "target", "coverage",
    "__pycache__", ".venv", "venv", "env",
})
WORKER = Path(__file__).with_name("graft_worker.py")


class GraftError(RuntimeError):
    """The hosted Graft boundary could not produce a trustworthy result."""


def _replaceable_parent(path):
    for parent in Path(path).parents:
        try:
            parent_mode = parent.stat().st_mode
        except OSError:
            return True
        writable = parent_mode & (stat.S_IWGRP | stat.S_IWOTH)
        # A sticky directory such as /tmp protects another user's entry from
        # replacement; an ordinary shared-writable parent does not.
        if writable and not (parent_mode & stat.S_ISVTX):
            return True
    return False


def _under(path, parent):
    child = Path(path).resolve(strict=False)
    root = Path(parent).resolve(strict=False)
    return child == root or root in child.parents


def _protected(path):
    value = os.path.realpath(os.fspath(path))
    return paths.is_protected(value)


def _policy(cfg):
    from . import settings
    return settings.graft_policy(cfg)


def resolve_executable(policy):
    configured = str(policy.get("executable") or "").strip()
    candidates = []
    if configured:
        candidates.append(configured)
    else:
        candidates.append(str(Path.home() / ".local" / "bin" / "graft"))
        found = shutil.which("graft")
        if found:
            candidates.append(found)
    for candidate in dict.fromkeys(candidates):
        try:
            path = Path(candidate).expanduser().resolve(strict=True)
            info = path.stat()
        except OSError:
            continue
        if (not path.is_file() or not os.access(path, os.X_OK)
                or info.st_uid != wincompat.geteuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or _replaceable_parent(path)
                or _protected(path)):
            continue
        return path
    detail = configured or "~/.local/bin/graft / PATH"
    raise GraftError(f"找不到可信、可执行且不可被其他用户改写的 graft：{detail}")


def available(cfg):
    policy = _policy(cfg)
    if not policy["enabled"]:
        return False
    try:
        resolve_executable(policy)
        network_namespace_prefix()
    except GraftError:
        return False
    return True


@functools.lru_cache(maxsize=1)
def _network_namespace_probe():
    """Return a cached fail-closed probe result for Linux net isolation."""
    candidate = shutil.which("unshare")
    if not candidate:
        return {"error": "缺少 unshare，无法建立 Graft network namespace"}
    try:
        executable = Path(candidate).resolve(strict=True)
        info = executable.stat()
    except OSError as exc:
        return {"error": f"无法检查 unshare：{type(exc).__name__}"}
    if (not executable.is_file() or not os.access(executable, os.X_OK)
            or info.st_uid not in {0, wincompat.geteuid()}
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or _replaceable_parent(executable)):
        return {"error": f"unshare executable 不可信：{executable}"}
    try:
        parent_netns = os.readlink("/proc/self/ns/net")
    except OSError as exc:
        return {"error": f"无法读取 parent netns：{type(exc).__name__}"}
    prefix = (
        str(executable), "--user", "--map-root-user", "--net", "--fork",
        "--kill-child=SIGKILL",
    )
    probe_code = (
        "import json,os,socket;"
        "s=socket.socket();s.settimeout(0.5);"
        "print(json.dumps({'netns':os.readlink('/proc/self/ns/net'),"
        "'routes':open('/proc/net/route').read().splitlines()[1:],"
        "'connect_ex':s.connect_ex(('1.1.1.1',53))}))"
    )
    env = {
        "HOME": str(Path.home()),
        "PATH": os.pathsep.join((
            str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin")),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    try:
        result = subprocess.run(
            [*prefix, sys.executable, "-c", probe_code],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"Graft netns probe 无法运行：{type(exc).__name__}"}
    try:
        payload = json.loads(result.stdout.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    routes = payload.get("routes")
    connect_result = payload.get("connect_ex")
    isolated = (
        result.returncode == 0
        and payload.get("netns")
        and payload.get("netns") != parent_netns
        and isinstance(routes, list) and not routes
        and isinstance(connect_result, int) and connect_result != 0
    )
    if not isolated:
        return {
            "error": "unshare probe 未证明独立且无外部路由的 network namespace",
        }
    return {
        "prefix": prefix,
        "parent_netns": parent_netns,
        "child_netns": str(payload["netns"]),
        "routes": tuple(map(str, routes)),
        "connect_ex": int(connect_result),
    }


def network_namespace_prefix():
    """Return the mandatory launcher; never fall back to network-open Graft."""
    result = _network_namespace_probe()
    if result.get("error"):
        raise GraftError(str(result["error"]))
    prefix = tuple(result.get("prefix") or ())
    if not prefix:
        raise GraftError("Graft network namespace launcher 为空")
    return prefix


def resolve_target(path, *, workspace_root):
    workspace = Path(workspace_root or os.getcwd()).expanduser().resolve()
    raw = Path(path or ".").expanduser()
    target = raw if raw.is_absolute() else workspace / raw
    try:
        target = target.resolve(strict=True)
    except OSError as exc:
        raise GraftError(f"Graft target 不存在：{target}") from exc
    if target.is_file():
        target = target.parent
    if not target.is_dir():
        raise GraftError(f"Graft target 不是目录：{target}")
    if not _under(target, workspace):
        raise GraftError(
            f"拒绝索引 workspace 之外的目录：{target}（workspace {workspace}）")
    if _protected(target):
        raise GraftError("拒绝索引受保护路径；先把需要的工作副本拷到可写目录再索引")
    relative = target.relative_to(workspace)
    if any(part.startswith(".") for part in relative.parts):
        raise GraftError(f"拒绝把隐藏状态目录作为 Graft target：{target}")
    return target


def inventory(root, policy):
    maximum_files = int(policy["max_files"])
    maximum_bytes = int(policy["max_source_bytes"])
    files = 0
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIP_DIRS and not name.startswith(".")
            and not Path(current, name).is_symlink())
        for name in sorted(names):
            path = Path(current, name)
            if path.is_symlink() or path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files += 1
            total += size
            if files > maximum_files:
                raise GraftError(
                    f"源码文件超过安全上限 {maximum_files:,}；"
                    "请缩小 path 或在用户配置中显式调整 graft.max_files")
            if total > maximum_bytes:
                raise GraftError(
                    f"源码体积超过安全上限 {maximum_bytes:,} bytes；"
                    "请缩小 path 或在用户配置中显式调整 graft.max_source_bytes")
    if files == 0:
        raise GraftError(f"没有发现 Graft 支持的源码文件：{root}")
    return {"files": files, "bytes": total}


def _required_text(args, name, *, maximum):
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise GraftError(f"Graft {name} 必须是非空字符串")
    value = value.strip()
    if len(value) > maximum:
        raise GraftError(f"Graft {name} 超过 {maximum:,} 字符上限")
    return value


def _scope_path(value, root, *, name, must_be_file=False):
    raw = Path(str(value or "")).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise GraftError(f"Graft {name} 不存在：{value}") from exc
    if not _under(candidate, root):
        raise GraftError(f"拒绝 Graft {name} 指向 target 之外：{candidate}")
    relative = candidate.relative_to(root)
    if any(part.startswith(".") for part in relative.parts):
        raise GraftError(f"拒绝 Graft {name} 指向隐藏状态路径：{relative}")
    if must_be_file and not candidate.is_file():
        raise GraftError(f"Graft {name} 不是文件：{relative}")
    if not must_be_file and not (candidate.is_file() or candidate.is_dir()):
        raise GraftError(f"Graft {name} 不是文件或目录：{relative}")
    sensitive = attachments.is_sensitive_path(candidate)
    if sensitive:
        raise GraftError(
            f"拒绝 Graft {name} 读取疑似凭据路径：{relative}（{sensitive}）")
    return relative.as_posix() or "."


def normalize_args(action, args, *, root):
    """Validate every model-controlled value before crossing into Node."""
    source = dict(args or {})
    out = {}
    if action == "find_code":
        out["query"] = _required_text(source, "query", maximum=2_000)
        value = source.get("limit", 5)
        if isinstance(value, bool) or not isinstance(value, int):
            raise GraftError("Graft limit 必须是 1..10 的整数")
        out["limit"] = max(1, min(10, value))
        out["full"] = source.get("full") is True
    elif action == "file_api":
        out["file"] = _scope_path(
            _required_text(source, "file", maximum=2_000), root,
            name="file", must_be_file=True)
    elif action == "trace_calls":
        out["symbol"] = _required_text(source, "symbol", maximum=500)
        direction = str(source.get("direction") or "in")
        if direction not in {"in", "out"}:
            raise GraftError("Graft direction 只能是 in 或 out")
        out["direction"] = direction
        depth = source.get("depth", 1)
        if isinstance(depth, str) and depth in {"all", "full"}:
            out["depth"] = depth
        elif isinstance(depth, str) and depth.isdigit():
            out["depth"] = max(1, min(12, int(depth)))
        elif isinstance(depth, int) and not isinstance(depth, bool):
            out["depth"] = max(1, min(12, depth))
        else:
            raise GraftError("Graft depth 必须是 1..12 或 all/full")
    elif action == "find_all":
        out["pattern"] = _required_text(source, "pattern", maximum=2_000)
        out["ignore_case"] = source.get("ignore_case") is True
        out["fixed"] = source.get("fixed") is True
    elif action == "repo_map":
        value = source.get("max_dirs", 16)
        if isinstance(value, bool) or not isinstance(value, int):
            raise GraftError("Graft max_dirs 必须是 1..32 的整数")
        out["max_dirs"] = max(1, min(32, value))
    elif action not in {"build", "rebuild", "status"}:
        raise GraftError(f"未知 Graft action：{action}")
    if source.get("in") not in (None, ""):
        if action not in {"find_code", "trace_calls", "find_all"}:
            raise GraftError(f"Graft {action} 不支持 in scope")
        out["in"] = _scope_path(source["in"], root, name="in")
    return out


def cache_dir(root, policy, *, create=True):
    configured = policy.get("cache_root")
    base = Path(configured).expanduser() if configured else store.HOME / "graft"
    base = base.resolve(strict=False)
    # 自包含模式下状态目录（.zylab-home）可能就在被索引的仓库里：它被 gitignore、
    # 不参与 inventory（dot 目录 + SKIP_DIRS），缓存放在里面不会索引自己。
    # 2026-09-04：默认缓存因这条规则被拒，模型转而去改用户 settings 绕过——教训。
    inside_state = _under(base, Path(store.HOME).resolve(strict=False))
    if _protected(base) or (_under(base, root) and not inside_state):
        raise GraftError("Graft cache 必须位于项目外，且不能落在受保护路径内")
    if not base.exists() and create:
        created = False
        try:
            base.mkdir(parents=True, exist_ok=False, mode=0o700)
            created = True
        except FileExistsError:
            # Another zylab terminal may have won the first-use race.
            pass
        if created:
            base.chmod(0o700)
    if base.exists():
        try:
            info = base.stat()
        except OSError as exc:
            raise GraftError(f"无法检查 Graft cache root：{base}") from exc
        if (not base.is_dir() or info.st_uid != wincompat.geteuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise GraftError(
                f"Graft cache root 必须是当前用户私有目录（0700/0755）：{base}")
    digest = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:20]
    # Repository names are reflected in a cache directory and later in UI.
    # Keep the human hint, but never reproduce terminal/control characters.
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("._-")
    label = (label[:32] or "repo")
    path = (base / f"{label}-{digest}").resolve(strict=False)
    if not _under(path, base):
        raise GraftError(f"Graft cache project path 逃逸 cache root：{path}")
    if create:
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existed:
            path.chmod(0o700)
    if path.exists():
        try:
            info = path.stat()
        except OSError as exc:
            raise GraftError(f"无法检查 Graft project cache：{path}") from exc
        if (not path.is_dir() or info.st_uid != wincompat.geteuid()
                or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise GraftError(
                f"Graft project cache 必须是当前用户私有目录：{path}")
    return path


def local_status(cfg, *, workspace_root, path="."):
    policy = _policy(cfg)
    value = {"enabled": bool(policy["enabled"]), "available": False}
    if not policy["enabled"]:
        return value
    try:
        executable = resolve_executable(policy)
        network_prefix = network_namespace_prefix()
        root = resolve_target(path, workspace_root=workspace_root)
        graph = cache_dir(root, policy, create=False)
    except GraftError as exc:
        value["error"] = str(exc)
        return value
    value.update({
        "available": True,
        "executable": str(executable),
        "root": str(root),
        "graph": str(graph),
        "indexed": (graph / ".graph" / "wiring.json").is_file(),
        "network_isolated": bool(network_prefix),
    })
    return value


def _worker_env():
    return {
        "HOME": str(Path.home()),
        "PATH": os.pathsep.join((
            str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin")),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
    }


def _direct_worker(argv, *, cwd, env, timeout):
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **wincompat.popen_group_kwargs())
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            wincompat.signal_process_group(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                wincompat.signal_process_group(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            stdout, stderr = proc.communicate()
    except BaseException:
        # /graft build is a synchronous admin command.  Ctrl-C must not leave
        # the worker or any Node/git descendant running after the CLI returns.
        try:
            wincompat.signal_process_group(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.communicate(timeout=2)
        except BaseException:
            try:
                wincompat.signal_process_group(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=2)
            except BaseException:
                pass
        raise
    if timed_out:
        raise GraftError(f"hosted Graft worker 超过 {timeout:g}s")
    if proc.returncode not in (None, 0):
        # The worker may have timed out a grandchild and returned an error
        # envelope.  Clean any remaining member of its process group.
        try:
            wincompat.signal_process_group(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    return proc.returncode, stdout, stderr


def _parse_envelope(stdout):
    text = stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else str(stdout)
    first = next((line for line in text.splitlines() if line.strip()), "")
    try:
        value = json.loads(first)
    except json.JSONDecodeError as exc:
        raise GraftError("hosted Graft worker 没有返回完整 JSON envelope") from exc
    if not isinstance(value, dict):
        raise GraftError("hosted Graft worker envelope 不是 object")
    if not value.get("ok"):
        raise GraftError(str(value.get("error") or "hosted Graft worker 失败"))
    return value


def execute(tool_or_action, args=None, *, cfg=None, workspace_root=None,
            task=None):
    policy = _policy(cfg or {})
    if not policy["enabled"]:
        raise GraftError("Graft 已关闭；用用户级配置 graft.enabled=true 开启")
    action = ACTION_BY_TOOL.get(tool_or_action, tool_or_action)
    args = dict(args or {})
    root = resolve_target(args.pop("path", "."), workspace_root=workspace_root)
    args = normalize_args(action, args, root=root)
    stats = inventory(root, policy)
    executable = resolve_executable(policy)
    graph = cache_dir(root, policy)
    runtime_home = graph.parent / ".runtime"
    request = {
        "action": action,
        "args": args,
        "root": str(root),
        "graph": str(graph),
        "runtime_home": str(runtime_home),
        "executable": str(executable),
        "timeout_seconds": policy["timeout_seconds"],
        "max_result_chars": policy["max_result_chars"],
    }
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 32_000:
        raise GraftError("Graft request 超过 32,000 字符")
    network_prefix = network_namespace_prefix()
    argv = [*network_prefix, sys.executable, str(WORKER), encoded]
    timeout = float(policy["timeout_seconds"]) + 5.0
    if task is not None:
        combined = task.run_process(
            argv, cwd=str(root), timeout=timeout, env=_worker_env())
        if getattr(task, "timed_out", False):
            raise GraftError(f"hosted Graft worker 超过 {timeout:g}s")
        stdout = combined.split("\n[stderr]\n", 1)[0]
        envelope = _parse_envelope(stdout)
    else:
        _code, stdout, _stderr = _direct_worker(
            argv, cwd=str(root), env=_worker_env(), timeout=timeout)
        envelope = _parse_envelope(stdout)
    envelope["inventory"] = stats
    envelope["network_isolated"] = bool(network_prefix)
    return envelope


def render(envelope):
    state = str(envelope.get("freshness") or (
        "built" if envelope.get("built") else "fresh"))
    if envelope.get("truncated"):
        state += " · truncated"
    network = (
        "net isolated" if envelope.get("network_isolated")
        else "net isolation unverified")
    header = (
        f"[graft hosted · {state} · {network} · "
        "repository-derived untrusted data · "
        f"{envelope.get('inventory', {}).get('files', '?')} files · "
        f"root {envelope.get('root', '?')}]"
    )
    body = json.dumps(
        envelope.get("result"), ensure_ascii=False,
        indent=2, sort_keys=True)
    notice = str(envelope.get("notice") or "").strip()
    notice_line = f"\n[graft freshness] {notice}" if notice else ""
    return header + notice_line + "\n" + body
