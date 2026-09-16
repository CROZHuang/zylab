"""OpenAI 兼容网关的流式客户端。

**网关地址不写死在代码里。** 一个网关 = 一个 OpenAI 兼容 endpoint + 它的 key
环境变量名；endpoint 是部署事实，随人随网络而变，所以由配置提供（见 `resolve_base`）。

**路由是这里的坑。** 企业网络里常见的情形：shell 默认导出的代理只放行一部分域名，
打到自家网关上返回 **403 —— 看起来像 key 无效，实际是路由错了**。所以本模块自己
清代理、按配置决定出网方式，不继承调用方的 shell 状态。

用 urllib 而不是 requests/openai：标准库能干的事就不引依赖，clone 下来就能跑，
不需要 pip、venv，也不会撞上 PEP 668 externally-managed 的系统 python。
"""
from . import paths
import json
import http.client
import os
import queue
import random
import time
import re
import sys
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass

# key 文件的候选链。key **不随仓库分发**，也不写进任何配置默认值 ——
# 这里只有路径。家目录因人而异（容器里常被重映射），所以按顺序找：
#   1. ZYLAB_KEYS_FILE 显式指定，优先级最高
#   2. ~/.zylab/keys.env —— 新装默认落点，和其余状态同目录
#   3. ~/.config/zylab/keys.env —— XDG 风格，换 HOME 即生效
#   4. ZYLAB_LEGACY_KEYS_FILE —— 老安装不因升级断掉
# 环境变量（DEEPINFER_API_KEY 等）比全部四条都优先，见 api_key()。
def _key_files():
    explicit = paths.env_get("KEYS_FILE")
    if explicit:
        return [os.path.expanduser(explicit)]
    out = []
    for p in (str(paths.state_home() / "keys.env"),
              str(paths.user_config_key_file()),
              *paths.legacy_key_files()):
        if p not in out:          # 某些 HOME 布局下后两条会重合
            out.append(p)
    return out


# 首选落点，用于报错时给一条能直接粘的补救命令。
KEYS = os.path.expanduser(
    paths.env_get("KEYS_FILE") or str(paths.state_home() / "keys.env"))

# 内置 profile 只给名字和 key 变量名，**不给地址** —— 地址是部署事实，不随仓库发布。
# base 的解析顺序（前面覆盖后面），见 `resolve_base`：
#   1. ZYLAB_BASE             覆盖当前网关，对付临时端点
#   2. ZYLAB_BASE_<GATEWAY>   按网关各自指定，如 ZYLAB_BASE_BOYUE
#   3. settings.json 的 gateways.<name>.base（`zylab init` 写的就是这里）
# settings 里的 `gateways` 还可以新增 profile：任何提供 /v1/chat/completions
# 且支持 tool calling 的服务都能接（厂商官方 API、公司内部网关、本地 vLLM）。
# 两个内置 profile 的实测差异（2026-08-21，供选型参考）：deepinfer 首 token 快，
# 但**不上报缓存命中**（prompt_tokens_details 为 null）；boyue **如实上报**
# （重复请求实测 hit=3584/3606）。
BUILTIN_GATEWAYS = {
    "deepinfer": {"keys": ("DEEPINFER_API_KEY",),
                  "note": "首 token 快 · 不报缓存命中"},
    "boyue": {"keys": ("BOYUE_API_KEY",),
              "note": "报缓存命中"},
}


def _settings_gateways():
    """只读 settings.json 的 `gateways` 段。

    刻意不 import core.settings：本模块要在最早期可用，而且只需要这一个字段。
    读不到就当没有 —— 配置损坏不该让客户端连 import 都失败。
    """
    try:
        with open(paths.state_home() / "settings.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    value = data.get("gateways")
    return value if isinstance(value, dict) else {}


def _merged_gateways():
    """内置 profile ∪ settings 里的 gateways；后者可补全 base，也可新增网关。"""
    out = {name: dict(cfg) for name, cfg in BUILTIN_GATEWAYS.items()}
    for raw_name, cfg in _settings_gateways().items():
        if not isinstance(cfg, dict):
            continue
        entry = out.setdefault(str(raw_name).lower(), {"keys": (), "note": ""})
        if str(cfg.get("base") or "").strip():
            entry["base"] = str(cfg["base"]).strip()
        keys = [str(k).strip() for k in (cfg.get("keys") or []) if str(k).strip()]
        if keys:
            entry["keys"] = tuple(keys)
        if str(cfg.get("note") or "").strip():
            entry["note"] = str(cfg["note"]).strip()
    return out


GATEWAYS = _merged_gateways()
GATEWAY = paths.env_get("GATEWAY", "deepinfer").lower()
if GATEWAY not in GATEWAYS:
    sys.exit(f"未知网关 {GATEWAY}，可选：{', '.join(GATEWAYS)}")
_INITIAL_GATEWAY = GATEWAY
_BASE_OVERRIDE = paths.env_get("BASE")


def resolve_base(name):
    """网关的 base URL；没配置就返回空串，由发请求前的检查给出可操作的报错。"""
    name = str(name).lower()
    if _BASE_OVERRIDE and name == _INITIAL_GATEWAY:
        return _BASE_OVERRIDE.strip()
    per_gateway = paths.env_get("BASE_" + name.upper())
    if per_gateway and per_gateway.strip():
        return per_gateway.strip()
    return str((GATEWAYS.get(name) or {}).get("base") or "").strip()


def base_missing_hint(name):
    """没配 endpoint 时的补救。说清配哪里，并给一条能直接粘的命令。"""
    return (
        f"网关 {name} 还没有 endpoint 地址 —— zylab 不预置任何网关地址，"
        "它只是客户端。三选一：\n"
        f"  zylab init --gateway {name}\n"
        f"  export {paths.env_name('BASE_' + str(name).upper())}='https://<host>/v1'\n"
        f"  或在 {paths.state_home() / 'settings.json'} 里写 "
        f'{{"gateways": {{"{name}": {{"base": "https://<host>/v1"}}}}}}')


BASE = resolve_base(GATEWAY)
KEY_NAMES = tuple(GATEWAYS[GATEWAY].get("keys") or ())


@dataclass(frozen=True)
class GatewayRoute:
    """一次 provider 请求使用的不可变路由快照。"""

    name: str
    base: str
    key_names: tuple


def route_for(name=None):
    """解析路由但不修改进程全局；worker 必须持有返回的 snapshot。"""
    if name is None:
        return GatewayRoute(GATEWAY, BASE, tuple(KEY_NAMES))
    name = str(name).lower()
    if name not in GATEWAYS:
        raise ValueError(f"未知网关 {name}，可选：{', '.join(GATEWAYS)}")
    cfg = GATEWAYS[name]
    return GatewayRoute(name, resolve_base(name), tuple(cfg.get("keys") or ()))


def set_gateway(name):
    """运行时切换网关（/gateway 命令用）。"""
    global GATEWAY, BASE, KEY_NAMES
    route = route_for(name)
    GATEWAY, BASE, KEY_NAMES = route.name, route.base, route.key_names


def normalize_cache(usage):
    """把各家不同的缓存字段归一成 (read, write, reported)。

    四种见过的格式：
      OpenAI      prompt_tokens_details.cached_tokens
      DeepSeek    prompt_cache_hit_tokens / prompt_cache_miss_tokens
      DeepInfer   prompt_tokens_details.created_cache_tokens（只有写，没有读）
      Anthropic   cache_read_input_tokens / cache_creation_input_tokens
    """
    det = usage.get("prompt_tokens_details") or {}
    read = (det.get("cached_tokens")
            or usage.get("prompt_cache_hit_tokens")
            or usage.get("cache_read_input_tokens") or 0)
    write = (det.get("created_cache_tokens")
             or usage.get("cache_creation_input_tokens") or 0)
    reported = bool(det) or any(
        k in usage for k in ("prompt_cache_hit_tokens", "cache_read_input_tokens"))
    return read, write, reported


def normalize_reasoning(usage):
    """提取 reasoning token 数。各家位置不同：

      OpenAI      completion_tokens_details.reasoning_tokens
      DeepSeek    completion_tokens_details.reasoning_tokens（同 OpenAI 形状）
      其他        不上报 → 0
    """
    det = usage.get("completion_tokens_details") or {}
    return det.get("reasoning_tokens") or 0


class MissingKey(Exception):
    """没有可用的 API key。

    **刻意继承 Exception 而不是走 sys.exit。** 早先 api_key() 直接
    sys.exit，抛出的 SystemExit 继承 BaseException，于是 model_limit()
    里那句 `except Exception: pass` 根本接不住 —— 一个**可选的**模型目录
    查询把整个进程在 Agent 构造阶段带走了，/doctor、/model、/resume 全都
    进不去。现在它是普通异常：可选路径吞掉并退回本地能力表，真正要发请求
    的路径再向上冒泡到 CLI 边界统一提示。
    """


def key_status(route=None):
    """key 从哪来 —— **只报来源，永不返回 key 本体**。

    给 /doctor 和启动检查用。研究者第一次跑起来时最需要知道的两件事：
    到底找到没有，以及如果没有该往哪放。
    """
    route = route or route_for()
    for name in route.key_names:
        if os.environ.get(name):
            return {"found": True, "source": f"环境变量 {name}",
                    "route": route.name, "candidates": _key_files()}
    for path in _key_files():
        try:
            with open(path, encoding="utf-8") as key_file:
                for line in key_file:
                    line = line.strip()
                    for name in route.key_names:
                        if line.startswith(name + "="):
                            return {"found": True, "source": f"{path} 内的 {name}",
                                    "route": route.name,
                                    "candidates": _key_files()}
        except OSError:
            continue
    return {"found": False, "source": None, "route": route.name,
            "names": list(route.key_names), "candidates": _key_files()}


def api_key(route=None):
    route = route or route_for()
    for n in route.key_names:
        if os.environ.get(n):
            return os.environ[n]
    for path in _key_files():
        try:
            with open(path, encoding="utf-8") as key_file:
                for line in key_file:
                    line = line.strip()
                    for n in route.key_names:
                        if line.startswith(n + "="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    target = _key_files()[0]
    raise MissingKey(
        f"网关 {route.name} 没有 key。\n"
        f"  找过环境变量: {', '.join(route.key_names)}\n"
        f"  找过文件:     {', '.join(_key_files())}\n"
        f"两条路，任选一条 ——\n"
        f"  export {route.key_names[0]}='<key>'\n"
        f"  mkdir -p {os.path.dirname(target)} && touch {target} && chmod 600 {target} && "
        f"read -s -p 'key: ' K && printf '{route.key_names[0]}=%s\\n' \"$K\" >> {target} && unset K")


def _origin(url):
    """(scheme, host, port) —— 同源判断用；端口缺省时按 scheme 补齐，
    这样 https://h/x 和 https://h:443/y 算同源，https://h 和 http://h 不算。"""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    port = parts.port or {"https": 443, "http": 80}.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


def _display_url(url):
    """错误信息里的重定向目标：只留 scheme://host[:port]/path。
    query 和 userinfo 可能带 token 或签名，不进日志。"""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """只跟随**同源**重定向，最多 3 跳；其余一律拒绝。

    urllib 默认会跟随任何 30x，并且在跨 host 重定向时**保留 Authorization
    头** —— 这是它和 requests 的一个著名差异（requests 会剥掉，urllib 不会）。
    于是 provider 或任何中间层只要返回一个指向第三方的 30x，bearer token 就
    跟着过去了。

    chat completions / models 端点没有合法理由跨源重定向。同源（scheme、host、
    port 全部一致）留一点余地给负载均衡之类；HTTPS→HTTP 降级也算跨源。
    拒绝时抛 APIError(kind="redirect", retryable=False)：这不是瞬时故障，
    重试只会把 token 再发一次。
    """

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(target) != _origin(req.full_url):
            raise APIError(
                f"provider 返回 HTTP {code} 重定向到 {_display_url(target)}，"
                "与请求不同源，已拒绝跟随（跟过去会把 Authorization 发给第三方）",
                kind="redirect", status=code, retryable=False)
        # 真实路径里 http_error_302 已经把 Location 拼成绝对 URL 再传进来；
        # 这里统一传拼好的 target，直接调用（测试、别的 handler）也能处理相对路径。
        return super().redirect_request(req, fp, code, msg, headers, target)


# 清掉继承来的代理。build_opener 而非 urlopen，避免污染全局。
# 传入 _SameOriginRedirects 会让 build_opener 跳过默认的 HTTPRedirectHandler。
# Esc 到达时读线程多半正阻塞在 recv：Linux 上另一线程 close() 这个 fd 不会唤醒它，
# 只有 shutdown() 会；而响应头到达之前连 response 对象都还没有，更无从关起。
# 所以连接一建立就把 socket 交给当前请求的 CancellationHandle。opener 是进程全局的，
# 句柄靠 thread-local 传进 connect()（provider 流跑在自己的 worker 线程里）。
_ACTIVE_CANCEL = threading.local()


def _bind_active_socket(sock):
    handle = getattr(_ACTIVE_CANCEL, "handle", None)
    binder = getattr(handle, "bind_socket", None)
    if callable(binder):
        binder(sock)


class _CancellableHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _bind_active_socket(self.sock)


class _CancellableHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()          # 含 TLS 握手；握手期间的 Esc 只能等握手结束
        _bind_active_socket(self.sock)


class _CancellableHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_CancellableHTTPConnection, req)


class _CancellableHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _CancellableHTTPSConnection, req, context=self._context)


_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _SameOriginRedirects(),
    _CancellableHTTPHandler(), _CancellableHTTPSHandler())


def _safe_diagnostic(value, limit):
    if value in (None, ""):
        return None
    text = str(value)
    try:
        from .tasks import redact_known_secrets
        text = redact_known_secrets(text)
    except Exception:
        pass
    text = "".join(
        char if char in "\n\t" or ord(char) >= 32 else "�"
        for char in text)
    return text[:max(1, int(limit))]


class APIError(RuntimeError):
    """结构化 provider 错误；字符串形式保持 CLI 向后兼容。"""

    def __init__(self, message, *, kind="unknown", status=None, code=None,
                 request_id=None, gateway=None, model=None, retryable=None):
        super().__init__(_safe_diagnostic(message, 4000) or "")
        self.kind = _safe_diagnostic(kind or "unknown", 100) or "unknown"
        self.status = int(status) if status is not None else None
        self.code = _safe_diagnostic(code, 200)
        self.request_id = _safe_diagnostic(request_id, 300)
        self.gateway = _safe_diagnostic(gateway, 100)
        self.model = _safe_diagnostic(model, 300)
        self.retryable = retryable if retryable is None else bool(retryable)

    def details(self):
        return {
            "kind": self.kind,
            "http_status": self.status,
            "code": self.code,
            "provider_request_id": self.request_id,
            "gateway": self.gateway,
            "model": self.model,
            "retryable": self.retryable,
        }


_ALLOW_INSECURE_HTTP = False
_ALLOWED_INSECURE_ENDPOINTS = frozenset()
_TRANSPORT_AUTHORIZER = None


def configure_transport_policy(*, allow_insecure_http=False, authorizer=None,
                               allowed_insecure_endpoints=()):
    """Install the process-local transport policy for this CLI invocation.

    ``allow_insecure_http`` is the broad explicit CLI danger flag.  The
    endpoint list is narrower persistent user consent; entries are normalized
    and only exact credential-free HTTP URLs survive.  Interactive approval
    stays in ``authorizer`` and binds gateway/base/repo/session.
    """
    global _ALLOW_INSECURE_HTTP, _ALLOWED_INSECURE_ENDPOINTS
    global _TRANSPORT_AUTHORIZER
    previous = (
        _ALLOW_INSECURE_HTTP, _TRANSPORT_AUTHORIZER,
        _ALLOWED_INSECURE_ENDPOINTS)
    _ALLOW_INSECURE_HTTP = bool(allow_insecure_http)
    _ALLOWED_INSECURE_ENDPOINTS = frozenset(
        _normalize_allowed_insecure_endpoints(
            allowed_insecure_endpoints))
    _TRANSPORT_AUTHORIZER = authorizer if callable(authorizer) else None
    return previous


def restore_transport_policy(previous):
    """Restore a tuple returned by ``configure_transport_policy``."""
    global _ALLOW_INSECURE_HTTP, _ALLOWED_INSECURE_ENDPOINTS
    global _TRANSPORT_AUTHORIZER
    if len(previous) == 2:  # compatibility with pre-allowlist test fixtures
        allow, authorizer = previous
        endpoints = ()
    else:
        allow, authorizer, endpoints = previous
    _ALLOW_INSECURE_HTTP = bool(allow)
    _ALLOWED_INSECURE_ENDPOINTS = frozenset(endpoints)
    _TRANSPORT_AUTHORIZER = authorizer if callable(authorizer) else None


def clear_transport_authorizer(authorizer):
    """Drop a Session callback only if it still owns the process slot."""
    global _ALLOW_INSECURE_HTTP, _ALLOWED_INSECURE_ENDPOINTS
    global _TRANSPORT_AUTHORIZER
    if _TRANSPORT_AUTHORIZER is authorizer:
        _TRANSPORT_AUTHORIZER = None
        _ALLOW_INSECURE_HTTP = False
        _ALLOWED_INSECURE_ENDPOINTS = frozenset()


def normalized_base_url(base):
    """Stable endpoint identity used by transport approvals and audit text."""
    raw = str(base or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.casefold()
    netloc = parsed.netloc.casefold()
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(
        (scheme, netloc, path, parsed.query, ""))


def _normalize_allowed_insecure_endpoints(values):
    """Fail-closed canonicalization for exact persistent HTTP consent."""
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    cleaned = []
    for value in values:
        raw = str(value or "").strip()
        try:
            parsed = urllib.parse.urlsplit(raw)
            _ = parsed.port
        except ValueError:
            continue
        if (parsed.scheme.casefold() != "http" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            continue
        endpoint = normalized_base_url(raw)
        if endpoint not in cleaned:
            cleaned.append(endpoint)
    return tuple(cleaned)


def transport_request(route, *, trace_context=None, purpose=None):
    """Build the exact non-secret scope presented to a transport authorizer."""
    trace = trace_context if isinstance(trace_context, dict) else {}
    raw = trace.get("raw") if isinstance(trace.get("raw"), dict) else {}
    cwd = os.path.realpath(os.path.abspath(
        str(trace.get("cwd") or os.getcwd())))
    return {
        "gateway": str(route.name),
        "base": normalized_base_url(route.base),
        "repo": cwd,
        "session": str(
            trace.get("transport_session_id")
            or trace.get("session_id") or ""),
        "purpose": str(purpose or raw.get("purpose") or "provider_request"),
        "key_names": tuple(str(name) for name in route.key_names),
        "data_categories": (
            "Authorization header", "prompt/messages",
            "code and tool results"),
    }


def require_secure_transport(route, *, trace_context=None, purpose=None,
                             model=None):
    """Fail before key lookup/socket unless this exact HTTP scope is allowed."""
    request = transport_request(
        route, trace_context=trace_context, purpose=purpose)
    if not str(request["base"] or "").strip():
        # 没配 endpoint 时必须在这里岔开。空 base 的 scheme 是空串 —— 既不是
        # https 也不在白名单里，会一路掉进下面的明文 HTTP 分支，于是一个只是
        # 还没填地址的人收到「已拒绝明文 HTTP provider 传输」。这类「报错指向
        # 错误原因」的代价极高：它把五秒钟的事变成半小时的排查。
        raise APIError(
            base_missing_hint(route.name),
            kind="base_not_configured", gateway=route.name,
            model=model, retryable=False)
    scheme = urllib.parse.urlsplit(request["base"]).scheme.casefold()
    if scheme == "https":
        return request
    if _ALLOW_INSECURE_HTTP:
        return request
    if request["base"] in _ALLOWED_INSECURE_ENDPOINTS:
        return request
    allowed = False
    failure = None
    authorizer = _TRANSPORT_AUTHORIZER
    if callable(authorizer):
        try:
            allowed = bool(authorizer(dict(request)))
        except Exception as exc:  # confirmation failure must fail closed
            failure = f" ({type(exc).__name__}: {_safe_diagnostic(exc, 300)})"
    if not allowed:
        key_types = ", ".join(request["key_names"]) or "provider key"
        raise APIError(
            "已拒绝明文 HTTP provider 传输："
            f"{request['gateway']} · {request['base']}。"
            f"该请求会发送 {key_types}、prompt/messages、代码与工具结果。"
            "交互模式需逐 session 确认；非交互模式必须显式传入 "
            "--allow-insecure-http。"
            + (failure or ""),
            kind="insecure_transport", gateway=request["gateway"],
            model=model, retryable=False)
    return request


class Interrupted(RuntimeError):
    """用户在流式过程中按了 Esc。"""


_WORKER_DONE = object()


class ProviderWorker:
    """在单个后台线程运行阻塞 provider 流，主线程通过有界队列消费。

    worker 不读 stdin、不写 stdout，也不接触 session/SQLite。队列满时施加背压，
    避免 renderer 暂停期间把模型 delta 无界堆进内存。
    """

    def __init__(self, target, *, queue_size=256, name="zylab-provider"):
        self._target = target
        self._queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=name, daemon=True)

    def start(self):
        self._thread.start()
        return self

    @property
    def alive(self):
        return self._thread.is_alive()

    def _put(self, item):
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _run(self):
        try:
            for event in self._target():
                if not self._put(("event", event)):
                    return
        except BaseException as exc:  # 原样交回主线程，包括 KeyboardInterrupt
            self._put(("error", exc))
        finally:
            self._put(("done", _WORKER_DONE))

    def poll(self, timeout):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self, timeout=0.3):
        self._stop.set()
        self._thread.join(timeout=max(0.0, float(timeout)))


class LivePhases:
    """给 TUI 看的实时请求阶段：只记最近一个 phase 与它的时刻（SPEC-CC-parity D2）。

    durable trace 记全量给事后分析；这个只回答"此刻卡在哪一步、卡了多久"。
    worker 线程写、主线程读，一把锁足够。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.phase = None
        self.at = None

    def mark(self, phase, attempt=1):
        with self._lock:
            self.phase = str(phase)
            self.at = time.monotonic()

    def snapshot(self):
        with self._lock:
            age = time.monotonic() - self.at if self.at is not None else 0.0
            return self.phase, age


class _PhaseChain:
    """调用方自带 phases 观察者时，两边都喂。"""

    def __init__(self, *sinks):
        self.sinks = [sink for sink in sinks if sink is not None]

    def mark(self, phase, attempt=1):
        for sink in self.sinks:
            sink.mark(phase, attempt)


def stream_chat_background(*args, poll_interval=0.025, queue_size=256,
                           **kwargs):
    """主线程可轮询的 ``stream_chat`` 适配器。

    没有 provider 事件的 poll 周期 yield ``{"t": "poll"}``，让 controller
    处理键盘、spinner 和取消；真实 provider 事件与异常保持原顺序。
    """
    live = LivePhases()
    provided = kwargs.get("phases")
    kwargs["phases"] = live if provided is None else _PhaseChain(provided, live)
    worker = ProviderWorker(
        lambda: stream_chat(*args, **kwargs), queue_size=queue_size).start()
    cancel = kwargs.get("cancel")
    completed = False
    try:
        while True:
            # Esc 的即时性由消费侧保证：不等 worker 从阻塞读里醒来（那可能要等
            # 网关下一个字节），也不把取消之后才到的增量交给上层。worker 由
            # finally 里的 cancel()/close() 收尾——cancel() 会 shutdown socket。
            if _cancelled(cancel):
                raise Interrupted("用户中断")
            item = worker.poll(max(0.001, float(poll_interval)))
            if _cancelled(cancel):
                raise Interrupted("用户中断")
            if item is None:
                # 空转 poll 带上"卡在哪一步、多久了"，TUI 据此把等待解释出来
                # （连接中 / 等待首字），而不是一个不说话的 spinner。
                phase, age = live.snapshot()
                yield {"t": "poll", "phase": phase, "phase_age": age}
                continue
            kind, value = item
            if kind == "event":
                yield value
            elif kind == "error":
                raise value
            else:
                completed = True
                return
    finally:
        if not completed:
            cancel = kwargs.get("cancel")
            cancel_fn = getattr(cancel, "cancel", None)
            if not callable(cancel_fn):
                cancel_fn = getattr(cancel, "set", None)
            if callable(cancel_fn):
                cancel_fn()
        worker.close()


class RequestPhases:
    """线程安全地收集每次 provider attempt 的阶段时间（Unix 秒）。"""

    FIELDS = ("queued_at", "started_at", "headers_at", "first_delta_at",
              "last_delta_at", "ended_at")

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._attempts = {}

    def mark(self, phase, attempt=1):
        field = phase if phase.endswith("_at") else f"{phase}_at"
        if field not in self.FIELDS:
            raise ValueError(f"未知 request phase: {phase}")
        attempt = int(attempt)
        if attempt < 1:
            raise ValueError("attempt 必须 >= 1")
        at = float(self._clock())
        with self._lock:
            row = self._attempts.setdefault(
                attempt, {"attempt": attempt, **dict.fromkeys(self.FIELDS)})
            # first_delta 取第一次；last_delta 随生成进展刷新。
            # 其余阶段只记首次，避免重复 callback 把真实边界向后推。
            if field == "last_delta_at" or row[field] is None:
                row[field] = at
        return at

    def snapshot(self):
        with self._lock:
            return [dict(self._attempts[key]) for key in sorted(self._attempts)]

    def attempt(self, number=1):
        with self._lock:
            row = self._attempts.get(int(number))
            return dict(row) if row else None


class CancellationHandle:
    """兼容 threading.Event，但还能主动 close 当前 response 解除阻塞读。"""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._response = None
        self._socket = None
        self._requested_at = None
        self._close_errors = []

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)

    @property
    def requested_at(self):
        with self._lock:
            return self._requested_at

    @property
    def close_errors(self):
        with self._lock:
            return tuple(self._close_errors)

    def set(self):
        """保留 Event.set() 形状；新代码可用语义更清楚的 cancel()。"""
        self.cancel()

    def cancel(self):
        with self._lock:
            if self._event.is_set():
                return False
            self._requested_at = time.time()
            self._event.set()
            response, sock = self._response, self._socket
        if sock is not None:
            self._shutdown_socket(sock)
        if response is not None:
            self._close_response(response)
        return True

    def bind_socket(self, sock):
        """连接一建立就绑定 socket；若 Esc 更早到达，立即 shutdown 唤醒阻塞读。"""
        with self._lock:
            self._socket = sock
            cancelled = self._event.is_set()
        if cancelled:
            self._shutdown_socket(sock)
        return sock

    def bind(self, response):
        """绑定已建立的 response；若 Esc 更早到达，立即关闭它。"""
        with self._lock:
            self._response = response
            cancelled = self._event.is_set()
        if cancelled:
            self._close_response(response)
        return response

    def release(self, response=None):
        with self._lock:
            if response is None or self._response is response:
                self._response = None
                self._socket = None

    def _shutdown_socket(self, sock):
        # close() 只是减引用计数，另一线程阻塞中的 recv 不会醒；shutdown 让内核给它 EOF。
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass                       # 已断开/已关闭：目的已达成
        except Exception as exc:
            with self._lock:
                self._close_errors.append(
                    f"{type(exc).__name__}: {exc}"[:200])

    def _close_response(self, response):
        sock = _response_socket(response)
        if sock is not None:
            self._shutdown_socket(sock)
        closer = getattr(response, "close", None)
        if not callable(closer):
            closer = getattr(response, "shutdown", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as exc:  # close 是 best-effort；ack 由 worker 另行上报
            with self._lock:
                self._close_errors.append(
                    f"{type(exc).__name__}: {exc}"[:200])


def _response_socket(response):
    """http.client.HTTPResponse 底下的 socket（fp 是 BufferedReader(SocketIO)）。"""
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", fp)
    return getattr(raw, "_sock", None)


def _cancelled(cancel):
    return cancel is not None and cancel.is_set()


def _wait_retry(delay, cancel):
    waiter = getattr(cancel, "wait", None) if cancel is not None else None
    if callable(waiter):
        if waiter(delay):
            raise Interrupted("用户中断")
    else:
        time.sleep(delay)


def _mark_phase(phases, phase, attempt):
    if phases is not None:
        phases.mark(phase, attempt)


class _TracePhaseError(RuntimeError):
    def __init__(self, error):
        super().__init__(str(error))
        self.error = error


class _PhaseFanout:
    """Send phase edges to durable trace first, optional observer second."""

    def __init__(self, trace, observer=None):
        self.trace = trace
        self.observer = observer

    def mark(self, phase, attempt=1):
        try:
            at = self.trace.mark(phase, attempt)
        except BaseException as exc:
            raise _TracePhaseError(exc) from exc
        if self.observer is not None:
            self.observer.mark(phase, attempt)
        return at


_DEFAULT_METRICS = object()


def _default_metrics_facade():
    # Lazy import keeps core.store's session layer out of lightweight model-list
    # imports while making provider traces always-on in normal operation.
    from . import store
    return store.metrics_facade()


def _resolve_metrics(value):
    if value is _DEFAULT_METRICS:
        return _default_metrics_facade()
    # ``None`` is an explicit escape hatch for isolated unit tests/embedders;
    # the CLI never passes it.
    return None if value is False else value


def _trace_usage(usage):
    if not isinstance(usage, dict) or not usage:
        return None
    cache_read, cache_write, cache_reported = normalize_cache(usage)
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cache_reported": cache_reported,
    }


def _trace_id(trace_context, attempt):
    base = (trace_context or {}).get("request_id")
    if not base:
        return None
    base = str(base)
    return base if int(attempt) == 1 else f"{base}-attempt-{attempt}"


def _begin_attempt(metrics, route, model, attempt, trace_context):
    if metrics is None:
        return None
    context = dict(trace_context or {})
    raw = dict(context.get("raw") or {})
    if context.get("request_id"):
        raw.setdefault("logical_request_id", str(context["request_id"]))
    return metrics.begin_attempt(
        gateway=route.name, model=model, attempt=attempt,
        session_id=context.get("session_id"),
        turn_id=context.get("turn_id"),
        request_id=_trace_id(context, attempt),
        context_tokens_before=context.get("context_tokens_before"),
        context_limit=context.get("context_limit"),
        summary_id=context.get("summary_id"),
        cwd=context.get("cwd") or os.getcwd(), raw=raw or None,
    )


# 这些 HTTP 码是「再试一次可能就好」，不是「请求本身错了」。
# **404 也在内**：DeepInfer 的 kimi 系列有间歇性 404 窗口 —— 2026-08-21 实测，
# 同一个 `kimi-k3-256k` 在一次扫描里 1/3 成功，十分钟后连测 10 次全通。
# 后端多实例、只有一部分挂载了该模型，所以 404 在这个网关上是瞬时故障而非「模型不存在」。
# 裸 401/403/400 不重试：那通常是认证、权限或请求本身的问题。例外由结构化
# error code 决定，例如 DeepInfer 的 403 model_not_available 是可恢复后端状态。
TRANSIENT_HTTP = (404, 408, 409, 425, 429, 500, 502, 503, 504)


def _is_transient(err):
    if isinstance(err, APIError) and err.retryable is not None:
        return err.retryable
    s = str(err)
    if "timed out" in s or "连接失败" in s or "Connection" in s:
        return True
    m = re.search(r"HTTP (\d+)", s)
    return bool(m) and int(m.group(1)) in TRANSIENT_HTTP


def _http_error(e, route, model):
    """把 OpenAI-compatible error body 归一为可判定的 APIError。"""
    try:
        raw = e.read().decode("utf-8", "replace")[:600]
    except Exception:  # noqa: BLE001 - error body 本身也可能读取失败
        raw = str(getattr(e, "reason", ""))[:600]
    # Gateways sometimes echo request fragments or credentials in their error
    # body. Redact known environment secrets before both terminal rendering
    # and durable request tracing.
    try:
        from .tasks import redact_known_secrets
        raw = redact_known_secrets(raw)
    except Exception:
        # Redaction setup must not hide the HTTP classification itself; the
        # state persistence boundary performs the same defense in depth.
        pass
    payload = None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        pass
    item = payload.get("error", payload) if isinstance(payload, dict) else {}
    code = item.get("code") if isinstance(item, dict) else None
    message = item.get("message") if isinstance(item, dict) else None
    request_id = item.get("request_id") if isinstance(item, dict) else None
    if request_id is None and isinstance(payload, dict):
        request_id = payload.get("request_id")
    if request_id is None:
        headers = getattr(e, "headers", None)
        request_id = headers.get("x-request-id") if headers else None
    low = " ".join(str(v or "").lower() for v in (code, message, raw))

    retryable = False
    if any(token in low for token in (
            "maximum context", "context_length", "max_model_len",
            "input too long", "exceeds max length")):
        kind = "context_length"
    elif "temperature" in low:
        kind = "capability_temperature"
    elif (str(code or "").lower() == "model_not_available"
          or "model is not available" in low):
        # DeepInfer 在并发或后端实例未挂载模型时会返回 403 + 此 code；
        # 这是可恢复的模型路由/容量状态，不应误报为 key 或权限错误。
        kind, retryable = "model_unavailable", True
    elif e.code == 401:
        kind = "authentication"
    elif e.code == 403 and any(
            word in low for word in (
                "额度", "余额", "quota", "balance", "insufficient", "credit")):
        # SPEC-CC-parity D3：额度耗尽和路由/权限 403 长得一样，动作完全不同。
        # 2026-09-03 实测 Boyue 余额为负时三个模型家族全部 403「用户额度不足」。
        kind = "quota"
    elif e.code == 403:
        kind = "permission"
    elif e.code == 404:
        kind = "model_unavailable"
        retryable = route.name == "deepinfer"
    elif e.code == 429:
        kind, retryable = "rate_limit", True
    elif e.code in (408, 409, 425, 500, 502, 503, 504):
        kind, retryable = "transient_http", True
    elif e.code == 400:
        kind = "invalid_request"
    else:
        kind = "http_error"

    hint = ""
    if kind == "model_unavailable" and retryable:
        # 只说「会自动重试」是误导：已下架 / 不对这把 key 开放的模型，重试多久
        # 都不会回来（2026-09-15 实测出厂默认 kimi-k3-256k 403，init 里这句
        # 紧跟在「换一个模型」后面，自相矛盾）。两种成因都说，并给出动作。
        hint = ("\n  模型不可用。要么是 " + route.name + " 后端实例临时没挂载它"
                "（未产生输出时会自动退避重试），要么是它已下架或不对这把 key 开放"
                " —— 后者重试多久都不会好。"
                "\n  换一个：交互模式 /model；非交互 "
                "zylab init --gateway " + route.name + " --model <模型名> --yes。")
    elif kind == "quota":
        hint = (f"\n  {route.name} 额度不足 —— key 与路由都没问题，充值后自动恢复；"
                "或 /gateway 切到另一个网关。")
    elif kind == "authentication":
        hint = ("\n  认证失败 —— key 无效、过期或不属于这个网关；/doctor 查看 key "
                "来源与候选文件。")
    elif e.code == 403 and kind == "permission":
        hint = ("\n  请求已使用 Zylab 的直连 opener；请检查模型权限、网关账号"
                "或服务端路由，不要把所有 403 都当成 key 错误。")
    elif kind == "rate_limit":
        hint = "\n  限流 —— 与 key 无关，会自动退避重试。"
    elif kind == "transient_http":
        hint = (f"\n  {route.name} 暂时不可用（HTTP {e.code}）—— 与 key 无关，会自动"
                "退避重试；持续失败可 /gateway 切到另一个网关。")
    elif e.code == 404 and not retryable:
        hint = (f"\n  模型在 {route.name} 上不存在；网关目录中存在不保证"
                " chat/completions 可调用。")
    rendered = f"HTTP {e.code}: {raw}{hint}"
    return APIError(
        rendered, kind=kind, status=e.code, code=code,
        request_id=request_id, gateway=route.name, model=model,
        retryable=retryable)


def stream_chat(model, messages, tools=None, max_tokens=8192, temperature=0.3,
                timeout=600, cancel=None, retries=3, phases=None,
                gateway=None, route=None, temperature_fallback=True,
                metrics=_DEFAULT_METRICS, trace_context=None,
                route_explicit=False, route_warning=None,
                before_attempt=None, thinking=None):
    """流式请求，带两种自动降级。temperature=None 表示不发这个参数。

    **1. temperature 降级。** 当前世代 Claude 已废弃 temperature（claude-opus-4-8
    直接 400："`temperature` is deprecated for this model"）。被拒就去掉重发，
    并把这个事实记进能力表，下次直接不带。

    **2. 瞬时故障重试。** 见 TRANSIENT_HTTP 上方的说明。

    `temperature_fallback=False` 只给 capability probe 使用：让调用方亲自看到
    参数拒绝并把它记录为模型能力。普通聊天保持默认自动降级。

    `before_attempt` 在每次真正准备发出 HTTP 请求前同步调用，包括 temperature
    fallback 和瞬时故障重试。回调抛错时本次请求不会出网；workflow 用这个边界
    实现跨并发 child 的原子 provider-attempt 硬预算。

    **重试的硬约束：只有在还没 yield 出任何事件时才能重发。** 一旦已经向调用方
    流式吐出过内容，重发就会让用户看到重复的半截回答 —— 那比直接报错更糟。
    所以 `emitted` 一旦为真，异常一律上抛，不再重试。
    """
    route = route or route_for(gateway)
    require_secure_transport(
        route, trace_context=trace_context, model=model)
    metrics = _resolve_metrics(metrics)
    if metrics is not None:
        decision = metrics.route_decision(
            route.name, model, explicit=bool(route_explicit))
        if not decision.allowed:
            raise APIError(
                decision.warning or "model route circuit is open",
                kind="circuit_open", gateway=route.name, model=model,
                retryable=False)
        if decision.warning:
            if callable(route_warning):
                route_warning(decision.warning)
            else:
                warnings.warn(decision.warning, RuntimeWarning, stacklevel=2)

    delay = 0.6
    max_attempts = max(1, int(retries))
    transient_failures = 0
    attempt_no = 0
    while transient_failures < max_attempts:
        attempt_no += 1
        # A cancelled child never reached the provider boundary, so it must
        # not consume a workflow attempt merely because its worker woke up.
        if before_attempt is not None and _cancelled(cancel):
            raise Interrupted("用户中断")
        if before_attempt is not None:
            trace_value = (
                trace_context if isinstance(trace_context, dict) else {})
            before_attempt({
                "attempt": attempt_no,
                "model": str(model),
                "gateway": route.name,
                "purpose": str(trace_value.get("purpose") or "chat"),
                "request_id": str(
                    trace_value.get("request_id") or ""),
                "turn_id": str(trace_value.get("turn_id") or ""),
            })
        _mark_phase(phases, "queued", attempt_no)
        trace = _begin_attempt(
            metrics, route, model, attempt_no, trace_context)
        phase_sink = _PhaseFanout(trace, phases) if trace is not None else phases
        emitted = False
        response_usage = None
        try:
            for ev in _stream_once(
                    model, messages, tools, max_tokens, temperature,
                    timeout, cancel, attempt_no, phase_sink, route=route,
                    thinking=thinking):
                trace_id = trace.id if trace is not None else None
                if ev.get("t") == "done":
                    response_usage = _trace_usage(ev.get("usage"))
                    # Commit success before exposing the terminal event.  A
                    # consumer may reasonably stop/close immediately after
                    # done; that must not be reclassified as interrupted.
                    if trace is not None:
                        # Detach first: a finalize/SQLite failure is the root
                        # error and must not fall into the generic handler and
                        # be finalized a second time as a failed request.
                        completed_trace, trace = trace, None
                        completed_trace.finalize(
                            "ok", usage=response_usage)
                emitted = True
                # The successful provider attempt can differ from the
                # controller's logical request id after a transient retry or
                # temperature fallback.  Expose the durable attempt id so
                # downstream tool traces link to the request that actually
                # produced the tool call, not merely to attempt 1.
                if trace_id is not None:
                    ev = {**ev, "trace_id": trace_id}
                yield ev
        except Interrupted as exc:
            if trace is not None:
                trace.finalize(
                    "interrupted", error=exc, error_kind="interrupted")
                trace = None
            raise
        except APIError as e:
            # 已经吐过内容 → 绝不重试，否则输出会重复。
            temperature_retry = bool(
                not emitted and temperature_fallback
                and temperature is not None
                and "temperature" in str(e).lower())
            # 不认识 thinking 的网关会 400。和 temperature 一样是确定性修复：
            # 去掉再发，不消耗瞬时故障预算。代价只是这次请求会带着思考跑。
            thinking_retry = bool(
                not emitted and not temperature_retry
                and thinking is not None
                and "thinking" in str(e).lower())
            transient_retry = False
            if not emitted and not temperature_retry:
                transient_failures += 1
                transient_retry = bool(
                    transient_failures < max_attempts
                    and _is_transient(e))
            retry_reason = (
                "temperature_fallback" if temperature_retry
                else "transient_retry" if transient_retry else None)
            error_kind = (
                "capability_temperature" if temperature_retry else e.kind)
            if trace is not None:
                # finalize 失败必须阻止下面两个自动 retry 分支。
                trace.finalize(
                    "failed", error=e, error_kind=error_kind,
                    retry_reason=retry_reason,
                    retryable=bool(e.retryable))
                trace = None

            if emitted:
                raise
            if thinking_retry:
                if route_warning is not None:
                    route_warning(
                        f"{route.name} 不接受 thinking 参数，本次请求带思考重发")
                thinking = None
                continue          # 确定性修复：不算瞬时故障
            if temperature_retry:
                try:
                    from . import models as _models
                    db = _models.load()
                    _models.update_record(
                        route.name, model,
                        lambda rec: rec.update(
                            supports_temperature=False))
                except Exception:
                    pass          # 记不上不影响本次请求
                temperature = None
                continue          # 确定性修复不消耗瞬时故障的 attempt 预算
            if not transient_retry:
                raise
            if metrics is not None:
                # Internal retries are automatic, even if the initial route
                # choice explicitly bypassed an already-open circuit.
                health = metrics.route_decision(
                    route.name, model, explicit=False)
                if not health.allowed:
                    raise
            _wait_retry(delay * random.uniform(0.85, 1.15), cancel)
            delay *= 2.2
        except _TracePhaseError as exc:
            # The durable phase sink itself is unhealthy.  Do not attempt a
            # second write through it and do not obscure the original error.
            if trace is not None:
                trace.close()
                trace = None
            raise exc.error
        except GeneratorExit as exc:
            if trace is not None:
                trace.finalize(
                    "interrupted", error=exc, error_kind="interrupted")
                trace = None
            raise
        except BaseException as exc:
            if trace is not None:
                trace.finalize(
                    "failed", error=exc,
                    error_kind=type(exc).__name__.lower())
                trace = None
            raise
        else:
            if trace is not None:
                trace.finalize("ok", usage=response_usage)
                trace = None
            return
        finally:
            if trace is not None:
                trace.close()


_TOP_LEVEL_SCHEMA_COMBINATORS = frozenset(("oneOf", "allOf", "anyOf"))


def _provider_tool_schemas(tools, route, model):
    """Adapt tool schemas only where the upstream provider requires it.

    Boyue's Bedrock-backed Claude route rejects top-level JSON Schema
    combinators in ``input_schema``.  Keep the canonical schema intact for
    every other route, and rely on the existing tool implementation validation
    after removing those three declarative constraints for this route.
    """
    model_id = str(model or "").lower()
    if (not tools or route.name != "boyue"
            or ("claude" not in model_id
                and not model_id.startswith("anthropic/"))):
        return tools
    compatible = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        parameters = (
            function.get("parameters")
            if isinstance(function, dict) else None)
        if (not isinstance(parameters, dict)
                or not (_TOP_LEVEL_SCHEMA_COMBINATORS & parameters.keys())):
            compatible.append(tool)
            continue
        compatible.append({
            **tool,
            "function": {
                **function,
                "parameters": {
                    key: value for key, value in parameters.items()
                    if key not in _TOP_LEVEL_SCHEMA_COMBINATORS
                },
            },
        })
    return compatible


def _stream_once(model, messages, tools=None, max_tokens=8192, temperature=0.3,
                 timeout=600, cancel=None, attempt=1, phases=None, route=None,
                 thinking=None):
    """流式请求，yield 事件字典。

    事件类型：
      {"t":"text",      "v": str}          增量文本
      {"t":"reasoning", "v": str}          增量思考（k3 会给 reasoning_content）
      {"t":"tool",      "v": [toolcall]}   本轮攒齐的工具调用
      {"t":"done",      "reason": str, "usage": dict}

    OpenAI 的流式 tool_calls 是按 `index` 分片投递的：name 通常只在第一片出现，
    arguments 是逐片拼接的 JSON 字符串。必须按 index 累加，不能按到达顺序。
    """
    route = route or route_for()
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "stream": True, "stream_options": {"include_usage": True}}
    if temperature is not None:
        payload["temperature"] = temperature
    # thinking={"type": "disabled"} 关掉思考。为什么需要它：reasoning 模型把
    # reasoning_content 也算进 max_tokens，额度小的请求（摘要 2000）会被思考吃光，
    # 正文一个字都出不来，看起来像「provider 返回空响应」。实测 kimi-k3 必现。
    if thinking is not None:
        payload["thinking"] = thinking
    provider_tools = _provider_tool_schemas(tools, route, model)
    if provider_tools:
        payload["tools"] = provider_tools
        payload["tool_choice"] = "auto"

    req = urllib.request.Request(
        f"{route.base}/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key(route)}",
                 "Content-Type": "application/json", "Accept": "text/event-stream"})

    acc = {}          # index -> {id, name, args}
    usage = {}
    reason = None
    terminal = False
    if _cancelled(cancel):
        _mark_phase(phases, "ended", attempt)
        raise Interrupted("用户中断")

    _mark_phase(phases, "started", attempt)
    _ACTIVE_CANCEL.handle = cancel      # connect() 里把 socket 绑到这个句柄上
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        _mark_phase(phases, "ended", attempt)
        raise _http_error(e, route, model) from None
    except APIError as e:
        # 重定向被 _SameOriginRedirects 拒绝时从 opener 内部抛出。它不是
        # 瞬时故障，绝不能落到下面那个 except 里被包成 retryable=True。
        _mark_phase(phases, "ended", attempt)
        if e.gateway is None:
            e.gateway = route.name
        if e.model is None:
            e.model = model
        raise
    except Exception as e:
        _mark_phase(phases, "ended", attempt)
        if _cancelled(cancel):
            raise Interrupted("用户中断") from None   # 是 Esc 把连接 shutdown 了
        raise APIError(
            f"连接失败: {e}", kind="network", gateway=route.name,
            model=model, retryable=True) from None
    finally:
        _ACTIVE_CANCEL.handle = None

    _mark_phase(phases, "headers", attempt)
    binder = getattr(cancel, "bind", None) if cancel is not None else None
    try:
        if callable(binder):
            binder(resp)
        if _cancelled(cancel):
            raise Interrupted("用户中断")
        with resp:
            for raw in resp:
                if _cancelled(cancel):
                    raise Interrupted("用户中断")
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    terminal = True
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    if ch.get("finish_reason"):
                        reason = ch["finish_reason"]
                        terminal = True
                    d = ch.get("delta") or {}
                    if (d.get("reasoning_content") or d.get("content")
                            or d.get("tool_calls")):
                        _mark_phase(phases, "first_delta", attempt)
                        _mark_phase(phases, "last_delta", attempt)
                    if d.get("reasoning_content"):
                        yield {"t": "reasoning", "v": d["reasoning_content"]}
                    if d.get("content"):
                        yield {"t": "text", "v": d["content"]}
                    for tc in d.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        slot = acc.setdefault(
                            i, {"id": None, "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
    except Interrupted:
        raise
    except Exception as e:
        if _cancelled(cancel):
            raise Interrupted("用户中断") from None
        retryable = isinstance(
            e, (OSError, TimeoutError, urllib.error.URLError,
                http.client.IncompleteRead, http.client.RemoteDisconnected))
        raise APIError(
            f"流式读取失败: {e}", kind="stream_read",
            gateway=route.name, model=model, retryable=retryable) from None
    finally:
        releaser = getattr(cancel, "release", None) if cancel is not None else None
        if callable(releaser):
            releaser(resp)
        _mark_phase(phases, "ended", attempt)

    if not terminal:
        if _cancelled(cancel):
            raise Interrupted("用户中断")   # shutdown 让读到 EOF，不是网关掉线
        raise APIError(
            "流式响应在 [DONE]/finish_reason 前结束",
            kind="stream_eof", gateway=route.name, model=model,
            retryable=True)

    if acc:
        calls = []
        for i in sorted(acc):
            s = acc[i]
            calls.append({"id": s["id"] or f"{s['name']}:{i}",
                          "type": "function",
                          "function": {"name": s["name"], "arguments": s["args"] or "{}"}})
        yield {"t": "tool", "v": calls}
    yield {"t": "done", "reason": reason, "usage": usage}


_LIMITS = {}


def model_limit(model, default=128_000, gateway=None):
    """查模型的上下文上限（max_model_len）。结果缓存，失败退回 default。

    写死一个全局 CTX_LIMIT 是错的：本网关上 minimax-m2.7 只有 166k、
    glm-5.2 256k，而 deepseek/kimi 是 1049k。切模型不跟着换，
    压缩阈值就可能高于上限 —— 那样压缩永远不触发，直接撑爆。
    """
    route = route_for(gateway)
    cache_key = (route.name, model)
    if cache_key in _LIMITS:
        return _LIMITS[cache_key]
    try:
        for m in list_models(route=route):
            if m.get("max_model_len"):
                _LIMITS[(route.name, m["id"])] = int(m["max_model_len"])
    except Exception:
        pass
    return _LIMITS.get(cache_key, default)


def list_models(gateway=None, route=None):
    route = route or route_for(gateway)
    require_secure_transport(route, purpose="model_catalog")
    req = urllib.request.Request(
        f"{route.base}/models",
        headers={"Authorization": f"Bearer {api_key(route)}"})
    with _OPENER.open(req, timeout=60) as r:
        return json.loads(r.read()).get("data", [])
