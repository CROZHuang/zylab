"""Verify and repair the pinned local Graft sidecar installation.

The component is optional to zylab, but when present it must be exactly the
audited source/runtime pair recorded in ``graft-component.lock.json``.  This
module deliberately performs no clone, download, ``npm install`` or source
mutation.  Pod bootstrap may only recreate the tiny launcher after every
executable and build artifact has passed its lock checks.
"""
from __future__ import annotations
from . import paths

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


LOCK_PATH = Path(__file__).resolve().parent.parent / "graft-component.lock.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ComponentError(RuntimeError):
    """The managed component lock or installation is unsafe/invalid."""


def _under(path, parent):
    child = Path(path).resolve(strict=False)
    root = Path(parent).resolve(strict=False)
    return child == root or root in child.parents


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root, *, workers=4):
    """Hash an installed dependency tree without following symlinks.

    Per-file hashing runs in a four-thread pool: the PVC contains thousands of
    small dependency files, and serial Python I/O made a bootstrap check take
    roughly four times longer in the measured installation.  ``workers`` is
    capped at four, below the pod's eight-CPU cgroup limit.
    """
    root = Path(root).resolve(strict=True)
    entries = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(current)
        dirs.sort()
        files.sort()
        descend = []
        for name in dirs:
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(("L", relative, path))
            else:
                descend.append(name)
        dirs[:] = descend
        for name in files:
            path = current / name
            entries.append((
                "L" if path.is_symlink() else "F",
                path.relative_to(root).as_posix(), path))
    entries.sort(key=lambda item: os.fsencode(item[1]))

    def one(entry):
        kind, relative, path = entry
        relative_bytes = os.fsencode(relative)
        if kind == "L":
            return kind, relative_bytes, os.fsencode(os.readlink(path)), 0, 0
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ComponentError(
                f"dependency tree 含不支持的特殊文件：{path}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return (
            kind, relative_bytes, digest.digest(),
            info.st_mode & 0o111, info.st_size)

    maximum_workers = max(1, min(4, int(workers)))
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=maximum_workers) as pool:
        results = list(pool.map(one, entries))
    combined = hashlib.sha256(b"sha256-tree-v2\0")
    total_bytes = 0
    for kind, relative, payload, mode, size in results:
        combined.update(kind.encode("ascii") + b"\0")
        combined.update(relative + b"\0")
        combined.update(str(mode).encode("ascii") + b"\0")
        combined.update(str(size).encode("ascii") + b"\0")
        combined.update(payload + b"\0")
        if kind == "F":
            total_bytes += size
    return {
        "algorithm": "sha256-tree-v2",
        "sha256": combined.hexdigest(),
        "entries": len(results),
        "bytes": total_bytes,
    }


def _required_mapping(value, name):
    if not isinstance(value, dict):
        raise ComponentError(f"manifest {name} 必须是 object")
    return value


def _required_text(mapping, name):
    value = mapping.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ComponentError(f"manifest {name} 必须是非空字符串")
    return value.strip()


def _required_sha(mapping, name):
    value = _required_text(mapping, name).lower()
    if not _SHA256.fullmatch(value):
        raise ComponentError(f"manifest {name} 不是 SHA-256")
    return value


def _component_path(base, persistent_root, value, name, *, preserve_leaf=False):
    raw = Path(value)
    if raw.is_absolute():
        raise ComponentError(f"manifest {name} 必须相对 zylab 仓库")
    candidate = base / raw
    path = (
        candidate.parent.resolve(strict=False) / candidate.name
        if preserve_leaf else candidate.resolve(strict=False))
    if not _under(path, persistent_root):
        raise ComponentError(f"manifest {name} 逃逸 persistent root")
    if paths.is_protected(str(path)):
        raise ComponentError(f"manifest {name} 不得位于受保护路径")
    return path


def load_lock(lock_path=None):
    try:
        path = Path(lock_path or LOCK_PATH).resolve(strict=True)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComponentError(f"无法读取 Graft component lock：{exc}") from exc
    value = _required_mapping(value, "root")
    if value.get("schema_version") != 1:
        raise ComponentError("只支持 Graft component lock schema_version=1")
    if value.get("component") != "graft-hosted":
        raise ComponentError("manifest component 必须是 graft-hosted")

    base = path.parent
    persistent_root = base.parent.resolve()
    node = _required_mapping(value.get("node"), "node")
    graft = _required_mapping(value.get("graft"), "graft")
    dependencies = _required_mapping(
        graft.get("dependencies"), "graft.dependencies")
    launcher = _required_mapping(value.get("launcher"), "launcher")
    node_path = _component_path(
        base, persistent_root, _required_text(node, "path"), "node.path")
    repo_path = _component_path(
        base, persistent_root, _required_text(graft, "path"), "graft.path")
    cli_rel = Path(_required_text(graft, "cli"))
    if cli_rel.is_absolute() or ".." in cli_rel.parts:
        raise ComponentError("manifest graft.cli 必须是仓库内相对路径")
    cli_path = (repo_path / cli_rel).resolve(strict=False)
    if not _under(cli_path, repo_path):
        raise ComponentError("manifest graft.cli 逃逸 Graft 仓库")
    dependencies_rel = Path(_required_text(dependencies, "path"))
    installed_lock_rel = Path(_required_text(dependencies, "installed_lock"))
    for relative, name in (
            (dependencies_rel, "graft.dependencies.path"),
            (installed_lock_rel, "graft.dependencies.installed_lock")):
        if relative.is_absolute() or ".." in relative.parts:
            raise ComponentError(f"manifest {name} 必须是仓库内相对路径")
    dependencies_path = (repo_path / dependencies_rel).resolve(strict=False)
    installed_lock_path = (repo_path / installed_lock_rel).resolve(strict=False)
    if (not _under(dependencies_path, repo_path)
            or not _under(installed_lock_path, dependencies_path)):
        raise ComponentError("manifest dependency paths 逃逸 Graft/node_modules")
    tree_algorithm = _required_text(dependencies, "tree_algorithm")
    if tree_algorithm != "sha256-tree-v2":
        raise ComponentError("只支持 dependency tree_algorithm=sha256-tree-v2")
    expected_entries = dependencies.get("entries")
    expected_bytes = dependencies.get("bytes")
    if (isinstance(expected_entries, bool) or not isinstance(expected_entries, int)
            or expected_entries < 1):
        raise ComponentError("manifest dependency entries 必须是正整数")
    if (isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int)
            or expected_bytes < 0):
        raise ComponentError("manifest dependency bytes 必须是非负整数")
    launcher_path = _component_path(
        base, persistent_root, _required_text(launcher, "path"),
        "launcher.path", preserve_leaf=True)
    revision = _required_text(graft, "revision").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ComponentError("manifest graft.revision 必须是完整 Git object id")
    upstream_revision = _required_text(graft, "upstream_revision").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", upstream_revision):
        raise ComponentError(
            "manifest graft.upstream_revision 必须是完整 Git object id")

    return {
        "lock_path": path,
        "persistent_root": persistent_root,
        "required": value.get("required") is True,
        "node_path": node_path,
        "node_version": _required_text(node, "version"),
        "node_sha256": _required_sha(node, "sha256"),
        "repo_path": repo_path,
        "revision": revision,
        "upstream_revision": upstream_revision,
        "package_lock_sha256": _required_sha(
            graft, "package_lock_sha256"),
        "cli_path": cli_path,
        "cli_sha256": _required_sha(graft, "cli_sha256"),
        "dependencies_path": dependencies_path,
        "installed_lock_path": installed_lock_path,
        "installed_lock_sha256": _required_sha(
            dependencies, "installed_lock_sha256"),
        "tree_algorithm": tree_algorithm,
        "tree_sha256": _required_sha(dependencies, "tree_sha256"),
        "tree_entries": expected_entries,
        "tree_bytes": expected_bytes,
        "launcher_path": launcher_path,
    }


def expected_launcher(lock):
    return (
        "#!/bin/sh\n"
        f"exec {shlex.quote(str(lock['node_path']))} "
        f"{shlex.quote(str(lock['cli_path']))} \"$@\"\n"
    )


def _run(argv, *, cwd=None, timeout=5):
    try:
        return subprocess.run(
            list(map(str, argv)), cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            check=False, env={
                "HOME": str(Path.home()),
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            })
    except (OSError, subprocess.TimeoutExpired):
        return None


def inspect(lock_path=None, *, full=False):
    """Return structured, side-effect-free evidence for the pinned component."""
    lock = load_lock(lock_path)
    checks = {}

    def record(name, ok, detail):
        checks[name] = {"ok": bool(ok), "detail": str(detail)}
        return bool(ok)

    node = lock["node_path"]
    node_present = record(
        "node_present", node.is_file() and os.access(node, os.X_OK), str(node))
    node_hash_ok = False
    if node_present:
        actual = _sha256(node)
        node_hash_ok = record(
            "node_sha256", actual == lock["node_sha256"],
            f"actual={actual} expected={lock['node_sha256']}")
    else:
        record("node_sha256", False, "skipped: node executable missing")
    if node_hash_ok:
        result = _run([node, "--version"])
        actual_version = result.stdout.strip() if result and result.returncode == 0 else ""
        record(
            "node_version", actual_version == lock["node_version"],
            f"actual={actual_version or 'unavailable'} expected={lock['node_version']}")
    else:
        record("node_version", False, "skipped: node SHA-256 mismatch")

    repo = lock["repo_path"]
    repo_present = record(
        "graft_repo", repo.is_dir() and (repo / ".git").exists(), str(repo))
    git = shutil.which("git", path="/usr/local/bin:/usr/bin:/bin")
    if repo_present and git:
        head = _run([git, "-C", repo, "rev-parse", "HEAD"])
        actual_head = head.stdout.strip().lower() if head and head.returncode == 0 else ""
        record(
            "graft_revision", actual_head == lock["revision"],
            f"actual={actual_head or 'unavailable'} expected={lock['revision']}")
        dirty = _run([
            git, "-C", repo, "status", "--porcelain", "--untracked-files=no"])
        clean = dirty is not None and dirty.returncode == 0 and not dirty.stdout.strip()
        record(
            "graft_worktree", clean,
            "tracked files clean" if clean else "tracked source/build drift")
    else:
        detail = "git unavailable" if not git else "Graft checkout missing"
        record("graft_revision", False, detail)
        record("graft_worktree", False, detail)

    package_lock = repo / "package-lock.json"
    if package_lock.is_file():
        actual = _sha256(package_lock)
        record(
            "package_lock", actual == lock["package_lock_sha256"],
            f"actual={actual} expected={lock['package_lock_sha256']}")
    else:
        record("package_lock", False, f"missing: {package_lock}")

    cli = lock["cli_path"]
    if cli.is_file():
        actual = _sha256(cli)
        record(
            "graft_cli", actual == lock["cli_sha256"],
            f"actual={actual} expected={lock['cli_sha256']}")
    else:
        record("graft_cli", False, f"missing: {cli}")

    dependencies = lock["dependencies_path"]
    dependencies_present = record(
        "dependencies_dir", dependencies.is_dir(), str(dependencies))
    installed_lock = lock["installed_lock_path"]
    if installed_lock.is_file():
        actual = _sha256(installed_lock)
        installed_lock_ok = record(
            "dependencies_lock", actual == lock["installed_lock_sha256"],
            f"actual={actual} expected={lock['installed_lock_sha256']}")
    else:
        installed_lock_ok = record(
            "dependencies_lock", False, f"missing: {installed_lock}")
    full_verified = False
    if full and dependencies_present and installed_lock_ok:
        try:
            actual_tree = tree_digest(dependencies)
        except (OSError, ComponentError) as exc:
            record(
                "dependencies_tree", False,
                f"hash failed: {type(exc).__name__}: {exc}")
        else:
            full_verified = record(
                "dependencies_tree",
                actual_tree["algorithm"] == lock["tree_algorithm"]
                and actual_tree["sha256"] == lock["tree_sha256"]
                and actual_tree["entries"] == lock["tree_entries"]
                and actual_tree["bytes"] == lock["tree_bytes"],
                "actual=" + json.dumps(actual_tree, sort_keys=True)
                + " expected=" + json.dumps({
                    "algorithm": lock["tree_algorithm"],
                    "sha256": lock["tree_sha256"],
                    "entries": lock["tree_entries"],
                    "bytes": lock["tree_bytes"],
                }, sort_keys=True))
    elif full:
        record(
            "dependencies_tree", False,
            "skipped: dependency directory or installed lock mismatch")

    launcher = lock["launcher_path"]
    expected = expected_launcher(lock)
    try:
        launcher_text = launcher.read_text(encoding="utf-8")
        launcher_mode = stat.S_IMODE(launcher.stat().st_mode)
    except OSError:
        launcher_text, launcher_mode = "", 0
    launcher_ok = (
        launcher.is_file() and not launcher.is_symlink()
        and launcher_text == expected and launcher_mode & stat.S_IXUSR)
    record(
        "launcher", launcher_ok,
        f"path={launcher} mode={launcher_mode:o} "
        f"content={'ok' if launcher_text == expected else 'mismatch'}")

    core_names = tuple(name for name in checks if name != "launcher")
    core_ready = all(checks[name]["ok"] for name in core_names)
    ready = core_ready and checks["launcher"]["ok"]
    return {
        "component": "graft-hosted",
        "required": lock["required"],
        "ready": ready,
        "repairable": core_ready and not checks["launcher"]["ok"],
        "state": "ready" if ready else ("repairable" if core_ready else "drift"),
        "revision": lock["revision"],
        "node_version": lock["node_version"],
        "full_requested": bool(full),
        "full_verified": full_verified,
        "paths": {
            "node": str(node), "repo": str(repo), "cli": str(cli),
            "launcher": str(launcher), "lock": str(lock["lock_path"]),
        },
        "checks": checks,
    }


def repair_launcher(lock_path=None):
    """Atomically recreate only the launcher after all core checks pass."""
    before = inspect(lock_path, full=True)
    if before["ready"]:
        return before
    if not before["repairable"]:
        raise ComponentError("Graft launcher 修复前置校验未通过")
    lock = load_lock(lock_path)
    target = lock["launcher_path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_symlink():
        raise ComponentError("拒绝覆盖 symlink Graft launcher")
    fd, temporary = tempfile.mkstemp(
        prefix=".graft-launcher-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(expected_launcher(lock))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    after = inspect(lock_path, full=True)
    if not after["ready"]:
        raise ComponentError("Graft launcher 原子修复后仍未通过校验")
    return after


def render(report):
    if report.get("ready"):
        dependency_state = (
            "deps full" if report.get("full_verified") else "deps quick")
        return (
            "graft component ready · "
            f"node {report.get('node_version')} · "
            f"revision {str(report.get('revision') or '')[:12]} · "
            f"{dependency_state}")
    failures = [
        name for name, check in (report.get("checks") or {}).items()
        if not check.get("ok")]
    return (
        f"graft component {report.get('state', 'unavailable')} · "
        f"failed: {', '.join(failures) or 'unknown'}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify or repair zylab's pinned optional Graft component")
    parser.add_argument(
        "action", nargs="?", choices=("check", "repair"), default="check")
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument(
        "--full", action="store_true",
        help="hash every installed dependency file (repair always does this)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = (
            repair_launcher(args.lock)
            if args.action == "repair" else inspect(args.lock, full=args.full))
    except ComponentError as exc:
        if args.json:
            print(json.dumps({"ready": False, "error": str(exc)},
                             ensure_ascii=False, sort_keys=True))
        else:
            print(f"graft component error · {exc}")
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(render(report))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
