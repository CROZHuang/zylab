#!/usr/bin/env python3
"""Isolated Graft CLI bridge used by the managed task runtime.

The parent passes one bounded JSON request as argv.  This worker owns the
build-then-query sequence so TaskManager can cancel the whole process group,
while stdout remains exactly one small JSON envelope for the parent to parse.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path


MAX_CHILD_OUTPUT = 4 * 1024 * 1024
MAX_ERROR_CHARS = 2_000
# C0/C1 terminal controls are never useful in a machine envelope.  Repository
# text is untrusted and may contain the single-byte CSI (U+009B), not only ESC.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
DROP_KEYS = frozenset({"saved"})


class WorkerError(RuntimeError):
    pass


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise WorkerError("hosted Graft 操作超过总时限")
    return value


@contextlib.contextmanager
def _graph_lock(graph: str, *, deadline: float):
    """Serialize build/refresh/query for one external graph cache.

    Graft owns an internal refresh lock once a graph exists.  This outer lock
    also covers the missing-graph check, so two zylab terminals cannot both
    start the initial full build.  It lives in the private external cache and
    never writes into the user's repository.
    """
    directory = Path(graph)
    try:
        info = directory.stat()
    except OSError as exc:
        raise WorkerError(f"无法检查 Graft cache：{directory}") from exc
    if (not directory.is_dir() or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
        raise WorkerError(f"Graft cache 不是当前用户私有目录：{directory}")
    lock_path = directory / ".zylab-hosted.lock"
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WorkerError(f"无法打开 Graft cache 锁：{lock_path}") from exc
    try:
        lock_info = os.fstat(descriptor)
        if (not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != os.geteuid()
                or lock_info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise WorkerError(f"Graft cache 锁不可信：{lock_path}")
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = _remaining(deadline)
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _safe_env(executable: str, runtime_home: str) -> dict[str, str]:
    runtime = Path(runtime_home)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        runtime.chmod(0o700)
    except OSError:
        pass
    path = os.pathsep.join(dict.fromkeys((
        str(Path(executable).parent),
        str(Path(sys.executable).parent),
        "/usr/local/bin", "/usr/bin", "/bin",
    )))
    env = {
        "HOME": str(runtime),
        "PATH": path,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TMPDIR": "/tmp",
        "GRAFT_HOSTED": "1",
        "DO_NOT_TRACK": "1",
        "CI": "1",
    }
    return env


def _run(argv: list[str], *, cwd: str, env: dict[str, str],
         timeout: float, accepted=(0,)) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WorkerError(f"graft 子进程超过 {timeout:g}s") from exc
    except OSError as exc:
        raise WorkerError(f"无法启动 graft：{type(exc).__name__}: {exc}") from exc
    if len(result.stdout) > MAX_CHILD_OUTPUT or len(result.stderr) > MAX_CHILD_OUTPUT:
        raise WorkerError("graft 子进程输出超过 4 MiB 安全上限")
    if result.returncode not in accepted:
        detail = (result.stderr or result.stdout).decode(
            "utf-8", "replace").strip()
        detail = " ".join(
            line.strip() for line in CONTROL.sub("", detail).splitlines()
            if line.strip())[:MAX_ERROR_CHARS]
        raise WorkerError(
            f"graft exit {result.returncode}"
            + (f"：{detail}" if detail else ""))
    return result


def _clean(value, *, string_limit=4_000, list_limit=30, depth=0):
    if depth >= 7:
        return "[depth truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = CONTROL.sub("", value)
        if len(text) > string_limit:
            return text[:string_limit] + "\n[… string truncated …]"
        return text
    if isinstance(value, list):
        rows = [
            _clean(item, string_limit=string_limit,
                   list_limit=list_limit, depth=depth + 1)
            for item in value[:list_limit]
        ]
        if len(value) > list_limit:
            rows.append({"truncated_items": len(value) - list_limit})
        return rows
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:80]:
            name = CONTROL.sub("", str(key))[:160]
            if name.casefold() in DROP_KEYS:
                continue
            out[name] = _clean(
                item, string_limit=string_limit,
                list_limit=list_limit, depth=depth + 1)
        return out
    return CONTROL.sub("", str(value))[:string_limit]


def _bounded(value, maximum: int):
    for string_limit, list_limit in (
            (4_000, 30), (2_000, 20), (1_000, 12), (500, 8)):
        clean = _clean(
            value, string_limit=string_limit, list_limit=list_limit)
        encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= maximum:
            return clean, string_limit < 4_000 or list_limit < 30
    preview = json.dumps(
        _clean(value, string_limit=240, list_limit=4),
        ensure_ascii=False, separators=(",", ":"))
    return {
        "truncated": True,
        "preview": preview[:max(256, maximum - 200)],
    }, True


def _query_argv(request: dict, base: list[str]) -> list[str]:
    action = request["action"]
    args = request.get("args") or {}
    root = request["root"]
    if action == "find_code":
        query = str(args.get("query") or "")
        limit = max(1, min(10, int(args.get("limit") or 5)))
        argv = [*base, "ask", query, root, "--source", "--json",
                "--limit", str(limit)]
        if args.get("full") is True:
            argv.append("--full")
        if args.get("in"):
            argv.extend(("--in", str(args["in"])))
        return argv
    if action == "file_api":
        return [*base, "skeleton", str(args.get("file") or ""),
                root, "--json"]
    if action == "trace_calls":
        direction = "out" if args.get("direction") == "out" else "in"
        depth = str(args.get("depth") or "1")
        if depth not in {"all", "full"}:
            depth = str(max(1, min(12, int(depth))))
        argv = [*base, "callers", str(args.get("symbol") or ""), root,
                "--direction", direction, "--depth", depth, "--json"]
        if args.get("in"):
            argv.extend(("--in", str(args["in"])))
        return argv
    if action == "find_all":
        argv = [*base, "grep", str(args.get("pattern") or ""),
                root, "--json"]
        if args.get("ignore_case") is True:
            argv.append("--ignore-case")
        if args.get("fixed") is True:
            argv.append("--fixed")
        if args.get("in"):
            argv.extend(("--in", str(args["in"])))
        return argv
    if action == "repo_map":
        maximum = max(1, min(32, int(args.get("max_dirs") or 16)))
        return [*base, "map", root, "--max-dirs", str(maximum), "--json"]
    if action == "status":
        return [*base, "check", root, "--json"]
    raise WorkerError(f"未知 graft action：{action}")


def _run_locked(request: dict, *, deadline: float) -> dict:
    executable = str(request["executable"])
    root = str(request["root"])
    graph = str(request["graph"])
    runtime_home = str(request["runtime_home"])
    maximum = int(request.get("max_result_chars") or 16_000)
    action = str(request.get("action") or "")
    env = _safe_env(executable, runtime_home)
    base = [executable, "--hosted", "--dir", graph]
    graph_file = Path(graph) / ".graph" / "wiring.json"
    built = False
    notice = ""
    # A missing graph is the one case Graft's query-time freshness gate does
    # not auto-build.  Once present, each query owns a locked, incremental
    # freshness probe inside Graft itself; hosted mode does not disable it.
    if action in {"build", "rebuild"} or not graph_file.is_file():
        print("[graft] structural index build", file=sys.stderr, flush=True)
        build_argv = [*base, "build", root]
        if action == "rebuild":
            build_argv.append("--no-reuse")
        _run(build_argv, cwd=root, env=env, timeout=_remaining(deadline))
        if not graph_file.is_file():
            raise WorkerError(
                "graft build exit 0，但未生成 .graph/wiring.json")
        built = True
        print("[graft] structural index ready", file=sys.stderr, flush=True)
    if action in {"build", "rebuild"}:
        raw = {
            "built": True,
            "graph_exists": graph_file.is_file(),
        }
    else:
        accepted = (0, 1) if action == "status" else (0,)
        result = _run(
            _query_argv(request, base), cwd=root, env=env,
            timeout=_remaining(deadline), accepted=accepted)
        notice_text = CONTROL.sub(
            "", result.stderr.decode("utf-8", "replace"))
        notice = " ".join(
            line.strip() for line in notice_text.splitlines() if line.strip()
        )[:MAX_ERROR_CHARS]
        try:
            raw = json.loads(result.stdout.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError("graft 没有返回完整 UTF-8 JSON") from exc
    cleaned, truncated = _bounded(raw, maximum)
    if built:
        freshness = "built"
    elif "refreshed the graph" in notice:
        freshness = "refreshed"
    elif notice:
        # Graft deliberately degrades to an older graph when its locked
        # refresh cannot complete.  Surface that caveat to the parent/model.
        freshness = "degraded"
    else:
        freshness = "fresh"
    return {
        "ok": True,
        "action": action,
        "root": root,
        "graph": graph,
        "built": built,
        "freshness": freshness,
        "notice": notice or None,
        "truncated": truncated,
        "result": cleaned,
    }


def run(request: dict) -> dict:
    timeout = float(request.get("timeout_seconds") or 180)
    if timeout <= 0:
        raise WorkerError("timeout_seconds 必须大于 0")
    deadline = time.monotonic() + timeout
    graph = str(request["graph"])
    with _graph_lock(graph, deadline=deadline):
        return _run_locked(request, deadline=deadline)


def main(argv=None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1:
        print(json.dumps({"ok": False, "error": "worker 需要一个 JSON request"}))
        return 2
    try:
        request = json.loads(values[0])
        if not isinstance(request, dict):
            raise WorkerError("request 必须是 object")
        envelope = run(request)
    except (KeyError, TypeError, ValueError, WorkerError) as exc:
        envelope = {
            "ok": False,
            "error": CONTROL.sub("", f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS],
        }
        print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 1
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
