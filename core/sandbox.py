"""Linux mount-namespace sandbox adapter for model-generated Bash.

The adapter deliberately has a narrow contract: it makes configured protected
paths read-only in a private mount namespace and can optionally unshare the
network namespace.  It does *not* claim that the rest of the filesystem is a
workspace sandbox.

The setup shell receives every path and the model command as positional
arguments.  Nothing caller-controlled is interpolated into the setup program.
After creating the bind mounts, ``setpriv`` drops the namespace's capabilities
and enables no-new-privileges before Bash starts; without that step, a command
could simply remount a protected bind read-write again.
"""

from __future__ import annotations
from . import paths
from . import wincompat

import enum
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


DEFAULT_PROTECTED_PATHS = tuple(paths.protected_paths())
_PROBE_MARKER = "zylab-unshare-probe-ok"
_NETWORK_PROBE_MARKER = "zylab-network-probe-ok"


class SandboxError(RuntimeError):
    """Base class for adapter and policy errors."""


class SandboxUnavailable(SandboxError):
    """The configured adapter cannot establish its promised boundary."""


class SandboxPrepareError(SandboxError):
    """A command or policy could not be converted into a safe launch plan."""


class SandboxMode(str, enum.Enum):
    STRICT = "strict"
    ASK_UNSANDBOXED = "ask-unsandboxed"
    DISABLED = "disabled"
    # ``off`` is accepted as a user-facing compatibility spelling, but all
    # persisted/evidence values remain the unambiguous canonical name.
    OFF = "disabled"

    @classmethod
    def parse(cls, value: "SandboxMode | str") -> "SandboxMode":
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower()
        if normalized == "off":
            normalized = "disabled"
        try:
            return cls(normalized)
        except ValueError as exc:
            raise SandboxPrepareError(
                "sandbox mode 必须是 strict / ask-unsandboxed / disabled"
            ) from exc


@dataclass(frozen=True)
class FilesystemRules:
    """Filesystem boundary currently supported by the unshare backend."""

    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS


@dataclass(frozen=True)
class NetworkRules:
    """When true, create a fresh network namespace (loopback stays down)."""

    isolate: bool = False


@dataclass(frozen=True)
class SandboxCapabilities:
    adapter: str
    available: bool
    executable: str | None
    setpriv_executable: str | None
    protected_paths_read_only: bool
    command_execution: bool
    network_isolation: bool
    probed_at: float
    reason: str = ""
    probe_returncode: int | None = None
    probe_stdout: str = ""
    probe_stderr: str = ""
    network_reason: str = ""
    network_probe_returncode: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SandboxDecision:
    mode: str
    allowed: bool
    sandboxed: bool
    requires_confirmation: bool
    decision: str
    marker: str
    reason: str = ""
    protected_paths_enforced: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SandboxPolicy:
    """Pure decision state machine; UI prompting remains the caller's job."""

    mode: SandboxMode | str = SandboxMode.ASK_UNSANDBOXED

    def decide(
        self,
        capabilities: SandboxCapabilities,
        *,
        interactive: bool,
        unsandboxed_approved: bool = False,
    ) -> SandboxDecision:
        mode = SandboxMode.parse(self.mode)
        # Disabled is an explicit request not to use the adapter.  Its marker
        # must remain honest even on a host where the runtime happens to work.
        if mode is SandboxMode.DISABLED:
            return SandboxDecision(
                mode=mode.value,
                allowed=True,
                sandboxed=False,
                requires_confirmation=False,
                decision="sandbox-disabled",
                marker="UNSANDBOXED",
                reason=(
                    "sandbox 已由配置禁用"
                    if capabilities.available
                    else (capabilities.reason or "sandbox runtime 不可用")
                ),
            )
        if capabilities.available:
            return SandboxDecision(
                mode=mode.value,
                allowed=True,
                sandboxed=True,
                requires_confirmation=False,
                decision="sandboxed",
                marker=f"SANDBOXED:{capabilities.adapter}",
                protected_paths_enforced=True,
            )

        reason = capabilities.reason or "sandbox runtime 不可用"
        if mode is SandboxMode.STRICT:
            return SandboxDecision(
                mode=mode.value,
                allowed=False,
                sandboxed=False,
                requires_confirmation=False,
                decision="sandbox-unavailable",
                marker="SANDBOX BLOCKED",
                reason=reason,
            )
        if unsandboxed_approved:
            return SandboxDecision(
                mode=mode.value,
                allowed=True,
                sandboxed=False,
                requires_confirmation=False,
                decision="unsandboxed-approved",
                marker="UNSANDBOXED",
                reason=reason,
            )
        return SandboxDecision(
            mode=mode.value,
            allowed=False,
            sandboxed=False,
            requires_confirmation=bool(interactive),
            decision=(
                "ask-unsandboxed"
                if interactive else "noninteractive-sandbox-deny"
            ),
            marker="UNSANDBOXED",
            reason=reason,
        )


@dataclass(frozen=True)
class PreparedCommand:
    argv: tuple[str, ...]
    cwd: str
    adapter: str
    protected_paths: tuple[str, ...]
    network_isolated: bool
    sandboxed: bool = True

    def as_dict(self) -> dict:
        value = asdict(self)
        value["argv"] = list(self.argv)
        value["protected_paths"] = list(self.protected_paths)
        return value


# This program is intentionally constant.  The count, every path, cwd, helper
# executable and model-generated command arrive after $0 as separate argv.
_SETUP_PROGRAM = r"""
set -eu
count=$1
shift
mount_bin=$1
shift
setpriv_bin=$1
shift
bash_bin=$1
shift

"$mount_bin" --make-rprivate /
i=0
while [ "$i" -lt "$count" ]; do
    target=$1
    shift
    "$mount_bin" --bind "$target" "$target"
    "$mount_bin" -o remount,bind,ro "$target"
    i=$((i + 1))
done

workdir=$1
shift
model_command=$1
cd "$workdir"
exec "$setpriv_bin" \
    --nnp \
    --bounding-set=-all \
    --inh-caps=-all \
    --ambient-caps=-all \
    "$bash_bin" -lc "$model_command" zylab-bash
""".strip()


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _clip(value, limit=1000) -> str:
    value = _text(value)
    if len(value) <= limit:
        return value
    return value[:limit] + f"…[{len(value) - limit} chars clipped]"


class UnshareSandboxAdapter:
    """Protected-path sandbox implemented with unshare + private bind mounts."""

    name = "unshare-mountns"

    def __init__(
        self,
        *,
        unshare_executable: str | None = None,
        mount_executable: str | None = None,
        setpriv_executable: str | None = None,
        shell_executable: str | None = None,
        bash_executable: str | None = None,
        protected_paths: Sequence[str] = DEFAULT_PROTECTED_PATHS,
        runner: Callable = subprocess.run,
        clock: Callable[[], float] = time.time,
        probe_timeout: float = 8.0,
    ):
        self.unshare_executable = (
            unshare_executable or shutil.which("unshare")
        )
        self.mount_executable = mount_executable or shutil.which("mount")
        self.setpriv_executable = (
            setpriv_executable or shutil.which("setpriv")
        )
        self.shell_executable = shell_executable or shutil.which("sh")
        self.bash_executable = bash_executable or shutil.which("bash")
        self.protected_paths = tuple(str(path) for path in protected_paths)
        self._runner = runner
        self._clock = clock
        self.probe_timeout = max(0.1, float(probe_timeout))
        self._capabilities: SandboxCapabilities | None = None
        self._capabilities_lock = threading.Lock()

    @staticmethod
    def _usable_executable(path: str | None) -> bool:
        return bool(
            path
            and os.path.isfile(path)
            and os.access(path, os.X_OK)
        )

    def _missing_runtime(self) -> list[str]:
        rows = []
        for name, path in (
            ("unshare", self.unshare_executable),
            ("mount", self.mount_executable),
            ("setpriv", self.setpriv_executable),
            ("sh", self.shell_executable),
            ("bash", self.bash_executable),
        ):
            if not self._usable_executable(path):
                rows.append(name)
        return rows

    @staticmethod
    def _canonical_path(path: str, *, label: str) -> str:
        raw = str(path or "")
        if not raw:
            raise SandboxPrepareError(f"{label} 不能为空")
        expanded = os.path.abspath(os.path.expanduser(raw))
        resolved = os.path.realpath(expanded)
        if not os.path.isabs(resolved):
            raise SandboxPrepareError(f"{label} 必须是绝对路径：{raw!r}")
        if not os.path.exists(resolved):
            raise SandboxPrepareError(f"{label} 不存在：{resolved}")
        return resolved

    def _filesystem_paths(self, rules) -> tuple[str, ...]:
        if rules is None:
            values = self.protected_paths
        elif isinstance(rules, FilesystemRules):
            values = rules.protected_paths
        elif isinstance(rules, Mapping):
            values = rules.get("protected_paths", self.protected_paths)
        else:
            values = rules
        if isinstance(values, (str, bytes, os.PathLike)):
            values = (values,)
        canonical = []
        for value in values:
            path = self._canonical_path(value, label="protected path")
            if path not in canonical:
                canonical.append(path)
        # 零个受保护路径是合法的，而且是**默认状态** —— 不是每个人都有需要守的
        # 归档挂载。以前这里抛错，因为那时受保护路径是写死的常量、不可能为空；
        # 改成声明式之后，没声明的人会在这里连沙箱都起不来。setup program 拿到
        # count=0 时挂载循环直接不执行，其余参数位不受影响。
        return tuple(canonical)

    @staticmethod
    def _network_isolated(rules) -> bool:
        if rules is None:
            return False
        if isinstance(rules, NetworkRules):
            return bool(rules.isolate)
        if isinstance(rules, Mapping):
            return bool(rules.get("isolate", False))
        return bool(rules)

    def _prepare_unchecked(
        self,
        command: str,
        cwd: str,
        filesystem_rules=None,
        network_rules=None,
    ) -> PreparedCommand:
        if not isinstance(command, str) or not command.strip():
            raise SandboxPrepareError("Bash command 必须是非空字符串")
        workdir = self._canonical_path(cwd, label="cwd")
        if not os.path.isdir(workdir):
            raise SandboxPrepareError(f"cwd 不是目录：{workdir}")
        protected = self._filesystem_paths(filesystem_rules)
        network_isolated = self._network_isolated(network_rules)

        missing = self._missing_runtime()
        if missing:
            raise SandboxUnavailable(
                "sandbox runtime 缺少可执行文件：" + ", ".join(missing)
            )
        argv = [
            str(self.unshare_executable),
            "--user",
            "--map-root-user",
            "--mount",
            "--fork",
            "--kill-child=SIGKILL",
        ]
        if network_isolated:
            argv.append("--net")
        argv.extend(
            [
                str(self.shell_executable),
                "-c",
                _SETUP_PROGRAM,
                "zylab-sandbox",
                str(len(protected)),
                str(self.mount_executable),
                str(self.setpriv_executable),
                str(self.bash_executable),
                *protected,
                workdir,
                command,
            ]
        )
        return PreparedCommand(
            argv=tuple(argv),
            cwd=workdir,
            adapter=self.name,
            protected_paths=protected,
            network_isolated=network_isolated,
        )

    def capabilities(self, *, refresh: bool = False) -> SandboxCapabilities:
        with self._capabilities_lock:
            return self._capabilities_locked(refresh=refresh)

    def _capabilities_locked(self, *, refresh: bool) -> SandboxCapabilities:
        """Probe once even when several task/subagent threads arrive together."""
        if self._capabilities is not None and not refresh:
            return self._capabilities
        probed_at = float(self._clock())
        if wincompat.IS_WINDOWS:
            # 这个适配器建立在 Linux 用户命名空间 + 私有 bind mount 之上，
            # Windows 上没有对应物。早退并说清楚：否则这里报的是
            # 「缺少可执行文件：unshare, setpriv」，读起来像「装上就好了」，
            # 而实际上装不了。UNSANDBOXED 的逐次确认仍是唯一的出路。
            self._capabilities = SandboxCapabilities(
                adapter=self.name,
                available=False,
                executable=self.unshare_executable,
                setpriv_executable=self.setpriv_executable,
                protected_paths_read_only=False,
                command_execution=False,
                network_isolation=False,
                probed_at=probed_at,
                reason=("Windows 没有 Linux 命名空间沙箱（unshare + 私有 bind "
                        "mount）；Bash 只能走 UNSANDBOXED 逐次确认，"
                        "受保护路径的词法硬守卫仍然生效"),
            )
            return self._capabilities
        missing = self._missing_runtime()
        if missing:
            self._capabilities = SandboxCapabilities(
                adapter=self.name,
                available=False,
                executable=self.unshare_executable,
                setpriv_executable=self.setpriv_executable,
                protected_paths_read_only=False,
                command_execution=False,
                network_isolation=False,
                probed_at=probed_at,
                reason="缺少可执行文件：" + ", ".join(missing),
            )
            return self._capabilities

        try:
            with tempfile.TemporaryDirectory(prefix="zylab-sandbox-probe-") as tmp:
                root = Path(tmp)
                sentinel = root / "sentinel"
                sentinel.write_text("seed\n", encoding="utf-8")
                # A successful base probe proves command execution, the
                # protected bind rejects writes, and the post-setup process
                # cannot regain CAP_SYS_ADMIN to remount it.
                command = (
                    "if mount -o remount,bind,rw . 2>/dev/null; then exit 91; fi; "
                    "if printf changed > sentinel 2>/dev/null; then exit 92; fi; "
                    "test \"$(cat sentinel)\" = seed; "
                    f"printf '{_PROBE_MARKER}\\n'"
                )
                prepared = self._prepare_unchecked(
                    command,
                    str(root),
                    FilesystemRules((str(root),)),
                    NetworkRules(False),
                )
                result = self._runner(
                    list(prepared.argv),
                    cwd=prepared.cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.probe_timeout,
                )
                returncode = int(getattr(result, "returncode", 1))
                stdout = _text(getattr(result, "stdout", ""))
                stderr = _text(getattr(result, "stderr", ""))
                unchanged = sentinel.read_text(encoding="utf-8") == "seed\n"
                available = (
                    returncode == 0
                    and _PROBE_MARKER in stdout
                    and unchanged
                )
                reason = "" if available else (
                    "mount namespace probe 未证明只读边界和命令执行"
                )
                network_available = False
                network_returncode = None
                network_reason = "base sandbox unavailable"
                network_stdout = ""
                network_stderr = ""
                if available:
                    try:
                        parent_netns = os.readlink("/proc/self/ns/net")
                        network_command = (
                            "child=$(readlink /proc/self/ns/net); "
                            f"test \"$child\" != {shlex.quote(parent_netns)}; "
                            f"printf '{_NETWORK_PROBE_MARKER}\\n'"
                        )
                        network_prepared = self._prepare_unchecked(
                            network_command,
                            str(root),
                            FilesystemRules((str(root),)),
                            NetworkRules(True),
                        )
                        network_result = self._runner(
                            list(network_prepared.argv),
                            cwd=network_prepared.cwd,
                            capture_output=True,
                            text=True,
                            timeout=self.probe_timeout,
                        )
                        network_returncode = int(getattr(
                            network_result, "returncode", 1))
                        network_stdout = _text(getattr(
                            network_result, "stdout", ""))
                        network_stderr = _text(getattr(
                            network_result, "stderr", ""))
                        network_available = (
                            network_returncode == 0
                            and _NETWORK_PROBE_MARKER in network_stdout)
                        network_reason = "" if network_available else (
                            "network namespace probe 未证明独立 netns")
                    except Exception as exc:
                        network_reason = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # capability boundary is deliberately fail-closed
            returncode = None
            stdout = ""
            stderr = ""
            available = False
            reason = f"{type(exc).__name__}: {exc}"
            network_available = False
            network_returncode = None
            network_reason = "base sandbox unavailable"
            network_stdout = ""
            network_stderr = ""

        self._capabilities = SandboxCapabilities(
            adapter=self.name,
            available=available,
            executable=self.unshare_executable,
            setpriv_executable=self.setpriv_executable,
            protected_paths_read_only=available,
            command_execution=available,
            network_isolation=network_available,
            probed_at=probed_at,
            reason=reason,
            probe_returncode=returncode,
            probe_stdout=_clip(stdout + network_stdout),
            probe_stderr=_clip(stderr + network_stderr),
            network_reason=network_reason,
            network_probe_returncode=network_returncode,
        )
        return self._capabilities

    def prepare(
        self,
        command: str,
        cwd: str,
        filesystem_rules=None,
        network_rules=None,
    ) -> PreparedCommand:
        capabilities = self.capabilities()
        if not capabilities.available:
            raise SandboxUnavailable(
                capabilities.reason or "sandbox runtime 不可用"
            )
        if (self._network_isolated(network_rules)
                and not capabilities.network_isolation):
            raise SandboxUnavailable(
                capabilities.network_reason
                or "sandbox runtime 未证明 network isolation")
        # There is deliberately no unsandboxed fallback here.  Policy/UI may
        # choose an explicitly unsandboxed path before calling the adapter, but
        # a prepare failure after a sandbox decision is always terminal.
        return self._prepare_unchecked(
            command, cwd, filesystem_rules, network_rules
        )

    def run(
        self,
        prepared: PreparedCommand,
        *,
        process_runner: Callable | None = None,
        timeout=None,
        env=None,
        **runner_kwargs,
    ):
        if not isinstance(prepared, PreparedCommand) or not prepared.sandboxed:
            raise SandboxPrepareError("run() 只接受 sandboxed PreparedCommand")
        runner = process_runner or self._runner
        kwargs = dict(runner_kwargs)
        kwargs.update({"cwd": prepared.cwd})
        if timeout is not None:
            kwargs["timeout"] = timeout
        if env is not None:
            kwargs["env"] = env
        if process_runner is None:
            kwargs.setdefault("capture_output", True)
            kwargs.setdefault("text", True)
        return runner(list(prepared.argv), **kwargs)

    @staticmethod
    def explain_violation(returncode, stderr="", stdout="") -> str | None:
        text = "\n".join((_text(stderr), _text(stdout))).lower()
        if "read-only file system" in text or "readonly filesystem" in text:
            return "sandbox 拒绝写入只读 protected path"
        if "operation not permitted" in text:
            return "sandbox 拒绝了不允许的 mount、namespace 或权限操作"
        if "permission denied" in text:
            return "sandbox 内操作被内核权限边界拒绝"
        if int(returncode or 0) != 0 and (
            "unshare" in text or "mount" in text or "setpriv" in text
        ):
            return "sandbox runtime 启动或准备失败"
        return None


__all__ = [
    "DEFAULT_PROTECTED_PATHS",
    "FilesystemRules",
    "NetworkRules",
    "PreparedCommand",
    "SandboxCapabilities",
    "SandboxDecision",
    "SandboxError",
    "SandboxMode",
    "SandboxPolicy",
    "SandboxPrepareError",
    "SandboxUnavailable",
    "UnshareSandboxAdapter",
]
