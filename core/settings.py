"""分层配置。

三层，后面的覆盖前面的：

  1. 内置默认值
  2. 用户级  ~/.zylab/settings.json
  3. 项目级  <repo-root-or-cwd>/.zylab/settings.json

**项目级只能把权限收紧，不能放宽。** 理由和 Claude Code 一样：项目目录可能
来自别人（clone 下来的仓库），让它把 bash 从「每次确认」改成「自动放行」，
等于给了任意仓库在你机器上静默执行命令的能力。收紧无害，放宽危险。

其余字段（模型、网关、参数）项目级可以自由覆盖 —— 那些不构成权限提升。
"""
from . import paths
import fcntl
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from . import model_health

USER_FILE = paths.state_home() / "settings.json"
PROJECT_DIRNAME = paths.project_dirname()

# 权限从松到紧，用于比较「项目级是否在放宽」
_PERM_RANK = {"allow": 0, "ask": 1, "deny": 2}
_SANDBOX_RANK = {"disabled": 0, "ask-unsandboxed": 1, "strict": 2}
_SANDBOX_EXECUTABLE_FIELDS = {
    "unshare_executable", "mount_executable", "setpriv_executable",
    "shell_executable", "bash_executable",
}
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class SettingsError(RuntimeError):
    """用户配置无法安全读取或更新。"""

DEFAULTS = {
    "model": None,               # None = 用 agent.MODEL
    "gateway": None,             # None = 用 client 的默认（deepinfer）
    "max_tokens": 8192,
    "temperature": 0.3,
    "compact_at": None,          # None = 按模型上限自动算
    "auto_approve": False,       # 相当于常开 /auto
    "resume_replay": 20,         # session/consult preview 的有界消息数
    # 工具循环轮数上限。None/0 = 不设上限（像 Claude Code 交互模式：模型停下或
    # 用户 Esc 才结束）。无人值守（-p、cron）想要兜底就设个整数，或用 --max-turns。
    "max_turns": None,
    # 后台任务（Ctrl+B）结束后把结果排成一条 prompt 交回模型，让它自动接着干；
    # False 则只在界面通知，等你自己告诉模型。
    "background_task_handoff": True,
    # 平台的模型名单在变（双向：下架的会回来，在册的会消失）。启动时若目录
    # 超过 catalog_ttl_hours 没抓过，就在后台抓一次；只在有变化时才吭声。
    # False 则完全手动，靠 /model refresh 与 /model check。
    # 交接摘要（resume 时的 recap 素材）。和压缩摘要不是一件事：压缩是为了塞进
    # 上下文窗口（238K 才触发），交接是为了让 resume 的人知道到哪了。
    # 实测最近 5 个会话没有一个有摘要，recap 因此长期无米可炊。
    "recap_auto": True,
    "recap_min_tokens": 40_000,
    "recap_refresh_turns": 15,
    "catalog_refresh": True,
    "catalog_ttl_hours": 24,
    "model_health": {
        # Seconds. These control probe freshness and the automatic route
        # circuit; explicit route choices may still bypass an open circuit.
        "probe_success_ttl": 24 * 60 * 60,
        "probe_transient_base_ttl": 5 * 60,
        "probe_transient_max_ttl": 60 * 60,
        "circuit_failure_threshold": 3,
        "circuit_open_ttl": 10 * 60,
    },
    "transport": {
        # 明文 HTTP 永不默认信任。用户可持久授权精确 endpoint；项目配置
        # 不能增加该列表，ZYLAB_BASE 换地址后也不会继承授权。
        "allowed_insecure_endpoints": [],
    },
    # 网关地址。zylab 不预置任何 endpoint —— 那是部署事实，不随代码发布。
    # {"deepinfer": {"base": "https://<host>/v1", "keys": ["X_API_KEY"]}}
    # `zylab init` 写的就是这里。只有用户层可写：项目目录常来自 clone，让它
    # 改 endpoint 等于让任意仓库把你的 key 发到它选的服务器上。
    "gateways": {},
    "web": {
        # 走直连而非代理的 host 后缀（内置已含 GitHub）。内网域名加在这里。
        "direct_suffixes": [],
        # 全路由死路：{host: "换用什么"}。不发请求，直接给出路。
        "blocked_hosts": {},
        # 代理 URL，常内嵌凭据；任何输出前都经 webfetch.redact 脱敏。
        "proxy": None,
    },
    "notify": {
        # SPEC-CC-parity H1/H2。两者都只在交互式 tty 上生效；管道与 -p 模式静默。
        "bell": True,      # turn 完成响一声（\x07）
        "title": True,     # 终端标题随 session 状态变（OSC 0），退出时清空
    },
    "workflow": {
        # Model-triggered adaptive workflows are opt-in because each launch
        # spends multiple provider attempts. Manual /workflow remains available.
        "auto": False,
        # 启动前对将要用到的每个 model route 各发一次小请求探活，坏的换该席位的
        # 下一候选，全坏就在花预算之前失败（用户 2026-09-04：平台没那么稳定）。
        "preflight": True,
        # 用户可配席位（2026-09-04）：{席位名: null=移除 | ["gateway/model", ...] |
        # {"family": "...", "candidates": [...]}}。只写变化的部分，其余沿用内置四席。
        # /workflow seats 有二级指令可视化增删；项目级配置只能移除、不能新增。
        "seats": {},
        "max_agents": 5,
        "max_nodes": 16,
        "max_parallel": 3,
        "max_per_gateway": 2,
        "max_reviewers": 2,
        "max_review_rounds": 1,
        "max_requests": 24,
        # 单个 child 不能先于其他席位抢光全局 provider-attempt 预算。
        "max_requests_per_agent": 5,
        "max_node_attempts": 2,
        "max_tokens": 1_500_000,
        "max_elapsed_seconds": 1800,
    },
    "map": {
        # Deterministic repo-map 可自动刷新，永不调用 provider。LLM
        # architecture 是独立派生层，只在用户显式 /architecture build 时更新。
        "use": False,
        "use_architecture": False,
        "max_index_chars": 6_000,
        "max_architecture_chars": 6_000,
        "max_files": 400,
        "max_dirs": 16,
        "hubs_per_dir": 3,
        "hotspots": 12,
    },
    "graft": {
        # Graft is an on-demand external structural index, not a permanent
        # system-context projection.  A cloned project may turn it off or lower
        # its bounds, but only trusted user config may choose executable/cache.
        "enabled": True,
        "executable": None,
        "cache_root": None,
        "max_files": 5_000,
        "max_source_bytes": 128 * 1024 * 1024,
        "max_result_chars": 16_000,
        "timeout_seconds": 180,
    },
    "memory": {
        # Personal CLI 默认使用本地 memory；两项都可按 chat 覆盖。
        # ``generate`` 只做确定性 session handoff，不额外调用 provider。
        "use": True,
        "generate": True,
        "max_index_chars": 25_000,
        "max_entries": 24,
        "capsule_chars": 32_000,
    },
    "sandbox": {
        # 项目级只可把 mode/network_isolation/protected_paths 收紧；runtime binary
        # 只能来自可信用户层，不能由 clone 下来的仓库替换。
        "mode": "ask-unsandboxed",
        # True = 新 network namespace，禁止 outbound；旧配置字段
        # ``network`` 仍作为兼容别名读取，但不再用于展示，避免把
        # ``network: false`` 误读成“关闭网络”。
        "network_isolation": True,
        "protected_paths": list(paths.protected_paths()),
        # 对象存储远端（rclone 的 `remote:bucket`）。和挂载点是同一份数据的
        # 两道门，只守一道等于没守。
        "protected_remotes": list(paths.protected_remotes()),
        "unshare_executable": None,
        "mount_executable": None,
        "setpriv_executable": None,
        "shell_executable": None,
        "bash_executable": None,
    },
    # hook 只能在用户级注册（见 core/hooks.py 的安全说明）
    "hooks": {},
    "permissions": {
        "read_file": "allow", "list_dir": "allow", "glob": "allow",
        "grep": "allow", "todo_write": "allow", "goal_update": "allow",
        "subagent": "allow",
        "graft_find_code": "allow", "graft_file_api": "allow",
        "graft_trace_calls": "allow", "graft_find_all": "allow",
        "graft_repo_map": "allow",
        # Project memory 写入可自动执行；global 写入与所有删除由 hard guard
        # 逐次确认。这里的 ask 是配置基线，allow/auto/session grant 也不能绕过。
        "memory_write": "allow",
        "memory_write_global": "ask",
        "memory_forget": "ask",
        # workflow 是**唯一会自主花钱**的工具：一次 quick/standard/deep 分别是
        # 3/5/6 个 child runs；工具循环会产生更多 provider attempts，但每个
        # child 和整个 workflow 都有硬上限。席位跨网关分布，通常会有一部分
        # 落在计费的 boyue。所以启动必须经用户同意，不能由模型独断。
        # workflow_control 保持 allow：它的 status 是只读且模型会频繁用；
        # add 虽会追加节点，但 WorkflowManager 会在落盘前复用 max_nodes /
        # max_requests / max_tokens / max_elapsed_seconds 硬上限。
        "workflow": "ask", "workflow_control": "allow",
        # 只读：列出/展开配方给模型看，不发 provider 请求
        "workflow_recipe": "allow",
        "consult_session": "ask",
        # web_fetch 是模型唯一的网络出口（sandbox 内 bash 默认断网）。
        # ask 的理由是外渗而非成本：URL 的 query 能携带任意本地内容出网。
        # 用户可按需在用户级配置放宽为 allow。
        "web_fetch": "ask",
        "bash": "ask", "write_file": "ask", "edit_file": "ask",
    },
}


def _read(path):
    if not path.is_file():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [配置文件有问题，已忽略：{path} —— {e}]")
        return {}


def project_root(cwd=None):
    """Resolve project config at the Git root, falling back to the exact cwd."""
    selected = Path(cwd or os.getcwd()).expanduser()
    absolute = Path(os.path.abspath(str(selected)))
    if not absolute.is_dir():
        return absolute
    try:
        probe = subprocess.run(
            ["git", "-C", str(absolute), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return absolute
    if probe.returncode != 0:
        return absolute
    lines = probe.stdout.splitlines()
    if not lines or not lines[0].strip():
        return absolute
    root = Path(os.path.abspath(lines[0].strip()))
    return root if root.is_dir() else absolute


def project_file(cwd=None):
    return paths.project_dir(project_root(cwd)) / "settings.json"


def load(cwd=None):
    """合并三层，返回 (配置, 来源列表)。"""
    cfg = json.loads(json.dumps(DEFAULTS))        # 深拷贝
    sources = ["内置默认"]

    user = _read(USER_FILE)
    if user:
        _merge(cfg, user, allow_permission_relax=True)
        sources.append(str(USER_FILE))

    pf = project_file(cwd)
    # HOME 本身可作为 workspace；此时用户层和项目层解析到同一个文件。
    # 不能把它再按“不可信项目配置”加载一遍，否则会重复来源、误报 hooks，
    # 还会让同一份配置同时服从两套权限语义。
    same_as_user = (
        os.path.realpath(os.fspath(pf))
        == os.path.realpath(os.fspath(USER_FILE)))
    proj = {} if same_as_user else _read(pf)
    if proj:
        # hook 执行任意命令，项目目录常来自 clone —— 允许它注册 hook 等于
        # 允许任意仓库在本机跑代码。这条边界比「不能放宽权限」更硬。
        if "hooks" in proj:
            proj.pop("hooks")
            print(f"  [已忽略 {pf} 里的 hooks —— hook 只能在用户级配置注册]")
        _merge(cfg, proj, allow_permission_relax=False)
        sources.append(str(pf))
    return cfg, sources


def _merge(cfg, patch, allow_permission_relax):
    for k, v in patch.items():
        if k == "permissions" and isinstance(v, dict):
            for tool, want in v.items():
                if want not in _PERM_RANK:
                    continue
                cur = cfg["permissions"].get(tool, "ask")
                if allow_permission_relax or _PERM_RANK[want] >= _PERM_RANK[cur]:
                    cfg["permissions"][tool] = want
                # 否则静默忽略：项目级不能放宽
        elif k == "hooks" and isinstance(v, dict):
            cfg["hooks"] = v
        elif k == "transport" and isinstance(v, dict):
            _merge_transport(
                cfg["transport"], v,
                trusted_user=bool(allow_permission_relax))
        elif k == "sandbox" and isinstance(v, dict):
            _merge_sandbox(
                cfg["sandbox"], v,
                trusted_user=bool(allow_permission_relax))
        elif k == "model_health" and isinstance(v, dict):
            _merge_model_health(cfg["model_health"], v)
        elif k == "workflow" and isinstance(v, dict):
            _merge_workflow(
                cfg["workflow"], v,
                trusted_user=bool(allow_permission_relax))
        elif k == "map" and isinstance(v, dict):
            _merge_map(
                cfg["map"], v,
                trusted_user=bool(allow_permission_relax))
        elif k == "graft" and isinstance(v, dict):
            _merge_graft(
                cfg["graft"], v,
                trusted_user=bool(allow_permission_relax))
        elif k == "memory" and isinstance(v, dict):
            _merge_memory(
                cfg["memory"], v,
                trusted_user=bool(allow_permission_relax))
        elif k == "gateways" and isinstance(v, dict):
            _merge_gateways(
                cfg["gateways"], v,
                trusted_user=bool(allow_permission_relax))
        elif k == "web" and isinstance(v, dict):
            _merge_web(
                cfg["web"], v,
                trusted_user=bool(allow_permission_relax))
        elif k == "notify" and isinstance(v, dict):
            _merge_notify(cfg["notify"], v)
        elif k == "auto_approve" and isinstance(v, bool):
            # auto_approve 等价于放宽 ask 权限。clone 下来的项目只能把它
            # 关掉，不能替用户打开。
            if allow_permission_relax or not v:
                cfg[k] = v
        elif k in cfg:
            cfg[k] = v


def _normalize_insecure_endpoint(value):
    """Canonicalize one exact, credential-free HTTP endpoint."""
    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        # Accessing port validates malformed values such as ":abc".
        _ = parsed.port
    except ValueError:
        return None
    if (parsed.scheme.casefold() != "http" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        return None
    return urllib.parse.urlunsplit((
        "http", parsed.netloc.casefold(), parsed.path.rstrip("/"), "", ""))


def _merge_gateways(current, patch, *, trusted_user):
    """网关地址**只认用户层**，项目层整段丢弃。

    这比「项目级不能放宽权限」更硬。项目目录常常来自 clone；让它改 endpoint
    等于让任意仓库把你的 API key、prompt 和代码发到它选的服务器上，而且界面上
    看不出任何异常 —— 模型照样有回复。所以这里不是「只能收紧」，是不许碰。
    """
    if not trusted_user:
        if patch:
            print("  [已忽略项目级 gateways —— 网关地址只能在用户级配置里设]")
        return
    for raw_name, value in patch.items():
        name = str(raw_name).strip().lower()
        if not name or not isinstance(value, dict):
            continue
        entry = dict(current.get(name) or {})
        base = str(value.get("base") or "").strip()
        if base:
            entry["base"] = base
        keys = [str(k).strip() for k in (value.get("keys") or [])
                if str(k).strip()]
        if keys:
            entry["keys"] = keys
        note = str(value.get("note") or "").strip()
        if note:
            entry["note"] = note
        if entry:
            current[name] = entry


def _merge_web(current, patch, *, trusted_user):
    """路由名单取并集；proxy 常内嵌凭据，只认用户层。

    名单取并集而不是覆盖：项目补充自己的内网域名是合理的，而且加进直连名单
    只影响「走不走代理」，不放宽任何边界 —— SSRF 的私网放行仍按同一份名单判定，
    所以这里的并集必须仍然是**显式声明**，不能从请求里学。
    """
    suffixes = patch.get("direct_suffixes")
    if isinstance(suffixes, (list, tuple)):
        current["direct_suffixes"] = list(dict.fromkeys(
            [*current.get("direct_suffixes", ()),
             *(str(s).strip().lower() for s in suffixes if str(s).strip())]))
    blocked = patch.get("blocked_hosts")
    if isinstance(blocked, dict):
        merged = dict(current.get("blocked_hosts") or {})
        for host, hint in blocked.items():
            if str(host).strip():
                merged[str(host).strip().lower()] = str(hint or "")
        current["blocked_hosts"] = merged
    if trusted_user and "proxy" in patch:
        value = patch.get("proxy")
        current["proxy"] = (
            str(value).strip() if value not in (None, "") else None)


def _merge_notify(current, patch):
    """只认 bool，逐键合并；用户写 {"bell": false} 不会把 title 一起冲掉。
    通知开关不涉及权限放宽，项目配置与用户配置同权。"""
    for name in ("bell", "title"):
        value = patch.get(name)
        if isinstance(value, bool):
            current[name] = value


def _merge_transport(current, patch, *, trusted_user):
    """Only trusted user config may persist exact plaintext endpoints."""
    if not trusted_user or "allowed_insecure_endpoints" not in patch:
        return
    values = patch.get("allowed_insecure_endpoints")
    if not isinstance(values, (list, tuple)):
        print(
            "  [已忽略无效 transport 配置："
            "allowed_insecure_endpoints 必须是 list]")
        return
    cleaned = []
    for value in values:
        endpoint = _normalize_insecure_endpoint(value)
        if endpoint is None:
            print(f"  [已忽略无效明文 HTTP endpoint：{value!r}]")
            continue
        if endpoint not in cleaned:
            cleaned.append(endpoint)
    current["allowed_insecure_endpoints"] = cleaned


def transport_policy(cfg):
    """Return the trusted, normalized plaintext endpoint allowlist."""
    baseline = dict(DEFAULTS["transport"])
    _merge_transport(
        baseline, dict((cfg or {}).get("transport") or {}),
        trusted_user=True)
    return {
        "allowed_insecure_endpoints": tuple(
            baseline["allowed_insecure_endpoints"]),
    }


def model_health_policy(cfg):
    """Build the validated effective M5 policy for one cwd config."""
    values = dict((cfg or {}).get("model_health") or {})
    try:
        return model_health.ModelHealthPolicy(**{
            name: values[name]
            for name in (
                "probe_success_ttl", "probe_transient_base_ttl",
                "probe_transient_max_ttl", "circuit_failure_threshold",
                "circuit_open_ttl")
        })
    except (KeyError, TypeError, ValueError) as exc:
        raise SettingsError(f"model_health 配置无效：{exc}") from exc


def _merge_model_health(current, patch):
    """Accept a health-policy layer only when the complete result is valid."""
    candidate = dict(current)
    duration_fields = {
        "probe_success_ttl", "probe_transient_base_ttl",
        "probe_transient_max_ttl", "circuit_open_ttl",
    }
    for name in duration_fields:
        if name not in patch:
            continue
        value = patch[name]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or float(value) <= 0):
            print(f"  [已忽略无效 model_health 配置：{name} 必须是正数秒]")
            return
        candidate[name] = value
    if "circuit_failure_threshold" in patch:
        value = patch["circuit_failure_threshold"]
        if (isinstance(value, bool) or not isinstance(value, int)
                or value < 1):
            print(
                "  [已忽略无效 model_health 配置："
                "circuit_failure_threshold 必须是 >= 1 的整数]")
            return
        candidate["circuit_failure_threshold"] = value
    try:
        model_health.ModelHealthPolicy(**candidate)
    except (TypeError, ValueError) as exc:
        print(f"  [已忽略无效 model_health 配置：{exc}]")
        return
    current.clear()
    current.update(candidate)


def _merge_workflow(current, patch, *, trusted_user):
    """Project config may reduce cost/capability, never enable or enlarge it."""
    auto = patch.get("auto")
    if isinstance(auto, bool) and (trusted_user or not auto):
        current["auto"] = auto
    preflight = patch.get("preflight")
    if isinstance(preflight, bool):
        current["preflight"] = preflight
    seats = patch.get("seats")
    if isinstance(seats, dict):
        merged = dict(current.get("seats") or {})
        for name, value in seats.items():
            name = str(name).strip()
            if not _SEAT_NAME.fullmatch(name):
                print(f"  [已忽略无效 workflow 席位名：{name!r}]")
                continue
            if value is None:
                merged[name] = None            # 移除是收紧，项目级也可以
                continue
            if not trusted_user:
                print(f"  [项目级配置不能新增/替换席位 {name}：已忽略]")
                continue
            normalized = normalize_seat_override(value)
            if normalized is None:
                print(f"  [已忽略无效 workflow 席位配置 {name}：应为 "
                      "[\"gateway/model\", ...] 或 {\"candidates\": [...]}]")
                continue
            merged[name] = normalized
        current["seats"] = merged
    bounds = {
        "max_agents": (2, 5),
        "max_nodes": (2, 64),
        "max_parallel": (1, 8),
        "max_per_gateway": (1, 4),
        "max_reviewers": (0, 5),
        "max_review_rounds": (1, 2),
        "max_requests": (2, 48),
        "max_requests_per_agent": (1, 16),
        "max_node_attempts": (1, 2),
        "max_tokens": (16_000, 8_000_000),
        "max_elapsed_seconds": (60, 14_400),
    }
    for name, (minimum, maximum) in bounds.items():
        if name not in patch:
            continue
        value = patch[name]
        if (isinstance(value, bool) or not isinstance(value, int)
                or not minimum <= value <= maximum):
            print(
                f"  [已忽略无效 workflow 配置：{name} "
                f"必须在 {minimum}..{maximum} 之间]")
            continue
        if trusted_user:
            current[name] = value
        else:
            current[name] = min(int(current[name]), value)


_SEAT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,23}")


def normalize_seat_override(value):
    """把一条席位配置归一成 {"family": str|None, "candidates": ["gateway/model", ...]}；
    形状不对返回 None。gateway 是否存在由 models 层在应用时校验。"""
    family = None
    raw = value
    if isinstance(value, dict):
        family = value.get("family")
        raw = value.get("candidates")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    candidates = []
    for item in raw:
        if isinstance(item, str):
            route = item.strip()
        elif (isinstance(item, (list, tuple)) and len(item) == 2
                and all(isinstance(part, str) for part in item)):
            route = f"{item[0].strip()}/{item[1].strip()}"
        else:
            return None
        gateway, slash, model = route.partition("/")
        if not slash or not gateway.strip() or not model.strip() or len(route) > 300:
            return None
        candidates.append(route)
    return {
        "family": (str(family).strip()[:32] if family else None),
        "candidates": candidates,
    }


def update_user_workflow_seats(changes):
    """原子修改用户级 workflow.seats。``changes`` 是 {席位名: 配置|None}；传 None 清空
    全部覆盖（恢复内置席位）。只动 seats 这一个键，不碰 workflow 段的其他配置。"""
    try:
        with _user_lock():
            cur = _read_user_for_update()
            workflow = cur.get("workflow") or {}
            if not isinstance(workflow, dict):
                raise SettingsError("用户配置 workflow 必须是 JSON object")
            workflow = dict(workflow)
            if changes is None:
                workflow.pop("seats", None)
            else:
                seats = dict(workflow.get("seats") or {})
                for name, value in changes.items():
                    name = str(name).strip()
                    if not _SEAT_NAME.fullmatch(name):
                        raise SettingsError(f"无效席位名：{name!r}")
                    if value is None:
                        seats[name] = None
                    else:
                        normalized = normalize_seat_override(value)
                        if normalized is None:
                            raise SettingsError(
                                f"席位 {name} 的配置无效：需要 gateway/model 列表")
                        seats[name] = normalized
                workflow["seats"] = seats
            if workflow:
                cur["workflow"] = workflow
            else:
                cur.pop("workflow", None)
            _write_user_unlocked(cur)
    except SettingsError:
        raise
    except OSError as exc:
        raise SettingsError(f"用户配置写入失败：{USER_FILE} —— {exc}") from exc
    return USER_FILE


def workflow_policy(cfg):
    """Return a validated copy used as the code-enforced launch boundary."""
    candidate = dict((cfg or {}).get("workflow") or {})
    baseline = dict(DEFAULTS["workflow"])
    _merge_workflow(baseline, candidate, trusted_user=True)
    return baseline


def _merge_map(current, patch, *, trusted_user):
    """Project config may disable/reduce repo context, never enlarge it."""
    for name in ("use", "use_architecture"):
        value = patch.get(name)
        if isinstance(value, bool) and (trusted_user or not value):
            current[name] = value
    bounds = {
        "max_index_chars": (0, 12_000),
        "max_architecture_chars": (0, 12_000),
        "max_files": (1, 400),
        "max_dirs": (1, 32),
        "hubs_per_dir": (0, 8),
        "hotspots": (0, 24),
    }
    for name, (minimum, maximum) in bounds.items():
        if name not in patch:
            continue
        value = patch[name]
        if (isinstance(value, bool) or not isinstance(value, int)
                or not minimum <= value <= maximum):
            print(
                f"  [已忽略无效 map 配置：{name} "
                f"必须在 {minimum}..{maximum} 之间]")
            continue
        if trusted_user:
            current[name] = value
        else:
            current[name] = min(int(current[name]), value)


def map_policy(cfg):
    """Return the bounded deterministic/architecture projection policy."""
    baseline = dict(DEFAULTS["map"])
    _merge_map(
        baseline, dict((cfg or {}).get("map") or {}),
        trusted_user=True)
    return baseline


def _merge_graft(current, patch, *, trusted_user):
    """Project config may disable/reduce Graft, never enable or redirect it."""
    enabled = patch.get("enabled")
    if isinstance(enabled, bool) and (trusted_user or not enabled):
        current["enabled"] = enabled
    bounds = {
        "max_files": (1, 50_000),
        "max_source_bytes": (1_048_576, 2 * 1024 * 1024 * 1024),
        "max_result_chars": (1_000, 30_000),
        "timeout_seconds": (10, 1_800),
    }
    for name, (minimum, maximum) in bounds.items():
        if name not in patch:
            continue
        value = patch[name]
        if (isinstance(value, bool) or not isinstance(value, int)
                or not minimum <= value <= maximum):
            print(
                f"  [已忽略无效 graft 配置：{name} "
                f"必须在 {minimum}..{maximum} 之间]")
            continue
        if trusted_user:
            current[name] = value
        else:
            current[name] = min(int(current[name]), value)
    if trusted_user:
        executable = patch.get("executable")
        if executable in (None, ""):
            if "executable" in patch:
                current["executable"] = None
        elif Path(str(executable)).expanduser().is_absolute():
            current["executable"] = str(executable)
        else:
            print("  [已忽略无效 graft.executable：必须是绝对路径]")
        cache_root = patch.get("cache_root")
        if cache_root in (None, ""):
            if "cache_root" in patch:
                current["cache_root"] = None
        elif Path(str(cache_root)).expanduser().is_absolute():
            resolved = os.path.realpath(os.path.expanduser(str(cache_root)))
            if paths.is_protected(resolved):
                print("  [已忽略无效 graft.cache_root：落在受保护路径内]")
            else:
                current["cache_root"] = str(cache_root)
        else:
            print("  [已忽略无效 graft.cache_root：必须是绝对路径]")


def graft_policy(cfg):
    """Return the trusted, bounded hosted-Graft policy."""
    baseline = dict(DEFAULTS["graft"])
    _merge_graft(
        baseline, dict((cfg or {}).get("graft") or {}),
        trusted_user=True)
    return baseline


def _merge_memory(current, patch, *, trusted_user):
    """A cloned project may disable/reduce memory, never enable or enlarge it."""
    for name in ("use", "generate"):
        value = patch.get(name)
        if isinstance(value, bool) and (trusted_user or not value):
            current[name] = value
    bounds = {
        "max_index_chars": (0, 100_000),
        "max_entries": (0, 100),
        "capsule_chars": (4_000, 100_000),
    }
    for name, (minimum, maximum) in bounds.items():
        if name not in patch:
            continue
        value = patch[name]
        if (isinstance(value, bool) or not isinstance(value, int)
                or not minimum <= value <= maximum):
            print(
                f"  [已忽略无效 memory 配置：{name} "
                f"必须在 {minimum}..{maximum} 之间]")
            continue
        if trusted_user:
            current[name] = value
        else:
            current[name] = min(int(current[name]), value)


def memory_policy(cfg):
    """Return the validated effective memory/capsule boundary."""
    baseline = dict(DEFAULTS["memory"])
    _merge_memory(
        baseline, dict((cfg or {}).get("memory") or {}),
        trusted_user=True)
    return baseline


def _merge_sandbox(current, patch, *, trusted_user):
    """Merge sandbox policy without letting a project weaken the boundary."""
    mode = str(patch.get("mode") or "").strip().lower()
    if mode == "off":
        mode = "disabled"
    if mode in _SANDBOX_RANK:
        existing = str(current.get("mode") or "ask-unsandboxed")
        if (trusted_user
                or _SANDBOX_RANK[mode] >= _SANDBOX_RANK[existing]):
            current["mode"] = mode

    network_value = patch.get("network_isolation")
    if not isinstance(network_value, bool):
        # Backward-compatible alias used by the first M6 draft.
        network_value = patch.get("network")
    if isinstance(network_value, bool):
        # A fresh network namespace is tighter. A project may enable it but
        # cannot turn off a user-enforced network boundary.
        if trusted_user or network_value:
            current["network_isolation"] = network_value

    values = patch.get("protected_paths")
    if isinstance(values, (list, tuple)):
        cleaned = [str(item) for item in values if str(item).strip()]
        if trusted_user:
            # The hard invariant remains even if a user accidentally omits it.
            current["protected_paths"] = list(dict.fromkeys(
                [*paths.protected_paths(), *cleaned]))
        else:
            current["protected_paths"] = list(dict.fromkeys(
                [*current.get("protected_paths", ()), *cleaned]))

    if trusted_user:
        for field in _SANDBOX_EXECUTABLE_FIELDS:
            if field in patch:
                value = patch[field]
                current[field] = (
                    str(value) if value not in (None, "") else None)


def max_turns_policy(cfg):
    """工具循环轮数上限：None = 不设上限（默认）。0、None、负数、非法值都当不设上限。"""
    try:
        value = int((cfg or {}).get("max_turns"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def recap_policy(cfg):
    """交接摘要：开关 + 起点 + 刷新间隔。非法值退回默认，不让配置弄崩会话。"""
    cfg = cfg or {}
    def _int(key):
        try:
            value = int(cfg.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            return int(DEFAULTS[key])
        return value if value > 0 else int(DEFAULTS[key])
    return {
        "enabled": bool(cfg.get("recap_auto", DEFAULTS["recap_auto"])),
        "min_tokens": _int("recap_min_tokens"),
        "refresh_turns": _int("recap_refresh_turns"),
    }


def catalog_policy(cfg):
    """目录自动刷新：开关 + TTL（秒）。非法值退回默认，不让配置弄崩启动。"""
    cfg = cfg or {}
    enabled = cfg.get("catalog_refresh", DEFAULTS["catalog_refresh"])
    try:
        hours = float(cfg.get("catalog_ttl_hours",
                              DEFAULTS["catalog_ttl_hours"]))
    except (TypeError, ValueError):
        hours = float(DEFAULTS["catalog_ttl_hours"])
    if hours <= 0:
        hours = float(DEFAULTS["catalog_ttl_hours"])
    return {"enabled": bool(enabled), "ttl_seconds": int(hours * 3600)}


def render(cfg, sources):
    out = ["  配置来源：" + " → ".join(sources), ""]
    for k in ("model", "gateway", "max_tokens", "temperature",
              "compact_at", "auto_approve", "resume_replay", "max_turns",
              "background_task_handoff", "catalog_refresh",
              "catalog_ttl_hours", "recap_auto", "recap_min_tokens",
              "recap_refresh_turns"):
        shown = cfg.get(k)
        if k == "max_turns":
            shown = max_turns_policy(cfg) or "不设上限"
        out.append(f"    {k:16} {shown}")
    transport = transport_policy(cfg)
    endpoints = transport["allowed_insecure_endpoints"]
    out.append("    transport:")
    out.append(
        "      allowed HTTP  "
        + (", ".join(endpoints) if endpoints else "(none)"))
    hk = cfg.get("hooks") or {}
    if hk:
        out.append("    hooks:")
        for ev, lst in hk.items():
            for h in lst:
                out.append(f"      {ev:14} {h.get('matcher','*'):16} {h.get('command','')[:44]}")
    else:
        out.append("    hooks:           (未配置)")
    workflow = workflow_policy(cfg)
    out.append("    workflow:")
    out.append(
        f"      auto           {str(bool(workflow['auto'])).lower()}")
    out.append(
        "      bounds         "
        f"agents≤{workflow['max_agents']} · "
        f"nodes≤{workflow['max_nodes']} · "
        f"parallel≤{workflow['max_parallel']} · "
        f"reviewers≤{workflow['max_reviewers']} · "
        f"rounds≤{workflow['max_review_rounds']} · "
        f"attempts≤{workflow['max_requests']} · "
        f"per-agent≤{workflow['max_requests_per_agent']}")
    out.append(
        "      budget         "
        f"tokens≤{workflow['max_tokens']} · "
        f"elapsed≤{workflow['max_elapsed_seconds']}s · "
        f"per-gateway≤{workflow['max_per_gateway']}")
    memory = memory_policy(cfg)
    out.append("    memory:")
    out.append(
        "      modes          "
        f"use={str(bool(memory['use'])).lower()} · "
        f"generate={str(bool(memory['generate'])).lower()}")
    out.append(
        "      bounds         "
        f"index≤{memory['max_index_chars']} chars · "
        f"entries≤{memory['max_entries']} · "
        f"capsule≤{memory['capsule_chars']} chars")
    graft = graft_policy(cfg)
    out.append("    graft:")
    out.append(
        "      hosted         "
        f"enabled={str(bool(graft['enabled'])).lower()} · "
        f"files≤{graft['max_files']} · "
        f"source≤{graft['max_source_bytes']} bytes · "
        f"result≤{graft['max_result_chars']} chars · "
        f"timeout≤{graft['timeout_seconds']}s")
    out.append(
        "      executable     "
        + str(graft.get("executable") or "trusted auto-discovery"))
    out.append(
        "      cache root     "
        + str(graft.get("cache_root") or str(paths.state_home() / "graft")))
    sandbox = cfg.get("sandbox") or {}
    out.append("    sandbox:")
    out.append(f"      mode           {sandbox.get('mode')}")
    isolated = bool(sandbox.get("network_isolation", True))
    out.append(
        "      network isolation "
        + ("true (outbound blocked)" if isolated else "false (outbound open)"))
    out.append(
        "      protected      "
        + ", ".join(sandbox.get("protected_paths") or ()))
    health = cfg.get("model_health") or {}
    out.append("    model_health:")
    for name in (
            "probe_success_ttl", "probe_transient_base_ttl",
            "probe_transient_max_ttl", "circuit_failure_threshold",
            "circuit_open_ttl"):
        out.append(f"      {name:28} {health.get(name)}")
    out.append("    permissions:")
    for t, p in sorted(cfg["permissions"].items()):
        out.append(f"      {t:14} {p}")
    return "\n".join(out)


@contextmanager
def _user_lock():
    USER_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(USER_FILE.parent, 0o700)
    except OSError:
        pass
    lock_path = USER_FILE.with_name(USER_FILE.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_user_for_update():
    """严格读取用户配置；损坏文件不能被一次 UI 操作静默覆盖。"""
    if USER_FILE.is_symlink():
        raise SettingsError(f"拒绝修改符号链接配置：{USER_FILE}")
    if not USER_FILE.exists():
        return {}
    try:
        value = json.loads(USER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"用户配置无法解析：{USER_FILE} —— {exc}") from exc
    if not isinstance(value, dict):
        raise SettingsError(f"用户配置顶层必须是 JSON object：{USER_FILE}")
    return value


def _write_user_unlocked(value):
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{USER_FILE.name}.", suffix=".tmp",
        dir=USER_FILE.parent, text=True)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, USER_FILE)
        os.chmod(USER_FILE, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def write_user(patch):
    """锁内合并用户配置；损坏 JSON fail-closed，不覆盖原文件。"""
    if not isinstance(patch, dict):
        raise SettingsError("配置更新必须是 object")
    try:
        with _user_lock():
            cur = _read_user_for_update()
            cur.update(patch)
            _write_user_unlocked(cur)
    except SettingsError:
        raise
    except OSError as exc:
        raise SettingsError(f"用户配置写入失败：{USER_FILE} —— {exc}") from exc
    return USER_FILE


def update_user_permission(tool, value):
    """原子设置一个用户级权限；value=None 表示移除用户覆盖。"""
    tool = str(tool or "").strip()
    if not _TOOL_NAME.fullmatch(tool):
        raise SettingsError(f"无效工具名：{tool!r}")
    if value is not None:
        value = str(value).lower()
        if value not in _PERM_RANK:
            raise SettingsError(
                f"无效权限 {value!r}；可选 allow / ask / deny / reset")
    try:
        with _user_lock():
            cur = _read_user_for_update()
            existing = cur.get("permissions", {})
            if existing is None:
                existing = {}
            if not isinstance(existing, dict):
                raise SettingsError(
                    "用户配置 permissions 必须是 JSON object")
            permissions = dict(existing)
            if value is None:
                permissions.pop(tool, None)
            else:
                permissions[tool] = value
            if permissions:
                cur["permissions"] = permissions
            else:
                cur.pop("permissions", None)
            _write_user_unlocked(cur)
    except SettingsError:
        raise
    except OSError as exc:
        raise SettingsError(f"用户配置写入失败：{USER_FILE} —— {exc}") from exc
    return USER_FILE


def permission_rows(cwd=None, cfg=None):
    """返回每个工具的 effective/user/project/default 权限及生效来源。"""
    if cfg is None:
        cfg, _ = load(cwd=cwd)
    user = _read(USER_FILE)
    project = _read(project_file(cwd))
    user_permissions = (
        user.get("permissions", {}) if isinstance(user, dict) else {})
    project_permissions = (
        project.get("permissions", {}) if isinstance(project, dict) else {})
    if not isinstance(user_permissions, dict):
        user_permissions = {}
    if not isinstance(project_permissions, dict):
        project_permissions = {}

    names = set(DEFAULTS["permissions"])
    names.update(cfg.get("permissions", {}))
    names.update(user_permissions)
    names.update(project_permissions)
    rows = []
    for tool in sorted(names):
        default = DEFAULTS["permissions"].get(tool, "ask")
        user_value = user_permissions.get(tool)
        if user_value not in _PERM_RANK:
            user_value = None
        before_project = user_value or default
        project_value = project_permissions.get(tool)
        if project_value not in _PERM_RANK:
            project_value = None
        project_applies = (
            project_value is not None
            and _PERM_RANK[project_value] >= _PERM_RANK[before_project])
        if project_applies:
            source = "project"
        elif user_value is not None:
            source = "user"
        else:
            source = "default"
        rows.append({
            "tool": tool,
            "effective": cfg.get("permissions", {}).get(tool, "ask"),
            "source": source,
            "default": default,
            "user": user_value,
            "project": project_value,
            "project_enforced": bool(
                project_applies and project_value != before_project),
        })
    return rows
