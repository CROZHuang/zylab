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
#
# **公开厂商的 base 预置，私有端点不预置。** 这两件事以前被混成一条规矩（「不预置任何
# 网关地址」），代价是陌生人 clone 下来第一条命令就撞墙：`zylab init --gateway openai`
# 回「未知网关 openai，可选：deepinfer, boyue」，而要新增网关又得先手写 settings.json
# —— 可 init 本来就该是写它的那一步。厂商官方 endpoint 是公开文档，不是谁的私有信息；
# 预置它才谈得上「下载下来填个 key 就能用」。内部网关（deepinfer / boyue）仍然只给
# key 变量名，地址由使用者自己填。
#
# 唯一的硬条件是 **OpenAI 兼容**：zylab 发 `{base}/chat/completions`、读
# `{base}/models`；网关明说某个模型要走 `{base}/responses` 时才改走那里（见
# _adaptation_for）。下面每一条都按厂商公开文档填；对不对不靠这张表打包票，靠
# `zylab init` 那次真实探测（列目录 + 调一次默认模型）当场验。
BUILTIN_GATEWAYS = {
    # 内部 / 自建：只给 key 名，地址自己填
    "deepinfer": {"keys": ("DEEPINFER_API_KEY",),
                  "note": "自建/内部网关 · 需自填 endpoint"},
    "boyue": {"keys": ("BOYUE_API_KEY",),
              "note": "自建/内部网关 · 需自填 endpoint"},
    # 公开厂商（OpenAI 兼容口）
    "openai": {"base": "https://api.openai.com/v1",
               "keys": ("OPENAI_API_KEY",), "note": "OpenAI 官方"},
    "anthropic": {"base": "https://api.anthropic.com/v1",
                  "keys": ("ANTHROPIC_API_KEY",),
                  "note": "Anthropic 的 OpenAI 兼容口"},
    "openrouter": {"base": "https://openrouter.ai/api/v1",
                   "keys": ("OPENROUTER_API_KEY",),
                   "note": "聚合上百家 · 目录不需要 key 就能列"},
    "deepseek": {"base": "https://api.deepseek.com/v1",
                 "keys": ("DEEPSEEK_API_KEY",), "note": "DeepSeek 官方"},
    "moonshot": {"base": "https://api.moonshot.cn/v1",
                 "keys": ("MOONSHOT_API_KEY",), "note": "月之暗面 Kimi"},
    "zhipu": {"base": "https://open.bigmodel.cn/api/paas/v4",
              "keys": ("ZHIPU_API_KEY", "GLM_API_KEY"), "note": "智谱 GLM"},
    "dashscope": {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "keys": ("DASHSCOPE_API_KEY",), "note": "阿里百炼 Qwen"},
    "siliconflow": {"base": "https://api.siliconflow.cn/v1",
                    "keys": ("SILICONFLOW_API_KEY",), "note": "硅基流动"},
    "groq": {"base": "https://api.groq.com/openai/v1",
             "keys": ("GROQ_API_KEY",), "note": "Groq"},
    "xai": {"base": "https://api.x.ai/v1",
            "keys": ("XAI_API_KEY",), "note": "xAI Grok"},
    "mistral": {"base": "https://api.mistral.ai/v1",
                "keys": ("MISTRAL_API_KEY",), "note": "Mistral"},
    "together": {"base": "https://api.together.xyz/v1",
                 "keys": ("TOGETHER_API_KEY",), "note": "Together"},
    # 本机推理：明文 HTTP，走 --allow-insecure-http 或那次确认
    "ollama": {"base": "http://localhost:11434/v1",
               "keys": ("OLLAMA_API_KEY",), "note": "本机 Ollama · 明文 HTTP"},
    "lmstudio": {"base": "http://localhost:1234/v1",
                 "keys": ("LMSTUDIO_API_KEY",), "note": "本机 LM Studio · 明文 HTTP"},
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
# 出厂常量。陌生人第一次跑 zylab 时**没选过任何网关**，落到的就是它——
# 于是第一屏会说「网关 deepinfer 还没有 endpoint」，一个他从没听说过的名字。
# 名字本身留着（换成别家等于替用户做一个可能要花钱的选择），但提示得认出
# 「这是出厂值不是你选的」，见 base_missing_hint。
FACTORY_GATEWAY = "deepinfer"
GATEWAY = paths.env_get("GATEWAY", FACTORY_GATEWAY).lower()
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


def is_unchosen_default(name):
    """`name` 是不是「谁也没选过的出厂常量」。

    三条同时成立才算：它就是出厂常量、环境变量没指定过、而且这个网关在本机
    根本没有地址（维护者自己把 deepinfer 配好了地址，那是**选过**，不该走这条）。
    """
    return (str(name).lower() == FACTORY_GATEWAY
            and not paths.env_get("GATEWAY")
            and not (GATEWAYS.get(FACTORY_GATEWAY) or {}).get("base"))


def base_missing_hint(name):
    """没配 endpoint 时的补救。说清配哪里，并给一条能直接粘的命令。

    公开厂商的地址是内置的（见 BUILTIN_GATEWAYS），所以走到这里的只有两种：
    自建/内部网关，或用户自己起的名字。两种都得由使用者填地址——顺带把
    「其实你可以直接挑一个内置的」说出来，否则新用户不知道有这条路。
    """
    ready = sorted(n for n, cfg in GATEWAYS.items() if cfg.get("base"))
    if is_unchosen_default(name):
        # 全新安装、什么都没配。别拿出厂常量的名字开场——他没选过 deepinfer，
        # 看到这个名字只会先问「这是什么、zylab 凭什么要连它」。
        return (
            "还没选网关。挑一个内置的厂商，填上自己的 key 就能用：\n"
            f"  zylab init --gateway <名字> --key-env <环境变量名>\n"
            f"    可选：{', '.join(ready) if ready else '（无）'}\n"
            "自建 / 公司内部网关则自己给地址：\n"
            "  zylab init --gateway mycorp --base https://<host>/v1 "
            "--key-env MYCORP_API_KEY")
    return (
        f"网关 {name} 还没有 endpoint 地址（自建/内部网关的地址不随仓库分发）。三选一：\n"
        f"  zylab init --gateway {name} --base https://<host>/v1\n"
        f"  export {paths.env_name('BASE_' + str(name).upper())}='https://<host>/v1'\n"
        f"  或换一个地址已内置的网关：zylab init --gateway <名字>\n"
        f"    可选：{', '.join(ready) if ready else '（无）'}")


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


# 直连 opener：`ProxyHandler({})` 是**显式**给空映射，等于关掉 urllib 对
# http(s)_proxy 的自动接管。README 有原委：企业网络里默认导出的代理常常只放行
# 一部分域名，打到自家网关上返回 403 —— 看起来像 key 失效，实际是路由错了。
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _SameOriginRedirects(),
    _CancellableHTTPHandler(), _CancellableHTTPSHandler())

# 但「永不走代理」曾经是**一条死路**：端点只能经代理到达的用户完全没法用
# （2026-09-22 实跑陌生人流程撞到 —— DeepSeek 官方端点在那台机器上只有一条代理
# 通，init 探测超时，而提示里只说「我不读你的环境变量」，没给任何出路）。
# `core/webfetch.py` 对同一个问题早有答案：**显式声明**一条代理，绝不从环境推断。
# 这里照它的形状补上入口，只是**只认显式声明这一级**——这条路携带 API key，
# 不该从 shell 环境里猜。
_PROXY_OPENERS = {}


def api_proxy(route=None):
    """模型请求要走的代理；没有显式声明就返回 None（直连）。

    只认 `ZYLAB_API_PROXY_<网关>`（只管这一个网关）与 `ZYLAB_API_PROXY`（其余
    所有网关），前者优先。**刻意不读 http(s)_proxy**（理由同 _OPENER），
    也刻意不复用 `web.proxy`：那条给网页抓取，信任面不同 —— 这条会把
    API key、prompt、代码与工具结果都送过去，必须由用户为这条路单独表态。

    为什么要按网关分（2026-09-24 实测，同一台机器）：DeepSeek 官方口**只有**经
    代理才通，DeepInfer **只有**直连才通。只有一个全局开关时两者不可兼得 ——
    开了 DeepInfer 断，不开 DeepSeek 的目录永远取不到。

    值要过 `looks_like_proxy_url`：09-20 有过事故，`_PROXY_CLEAR='unset …'`
    这种 shell 命令被当成代理交给 ProxyHandler，urllib 抛
    `InvalidURL: URL can't contain control characters`，代理这条腿当场失效。
    形态不对就当没声明（返回 None），不是报错 —— 直连仍然可用。
    """
    name = getattr(route, "name", None)
    value = paths.env_get(api_proxy_env(name), "").strip() if name else ""
    if not value:
        value = paths.env_get("API_PROXY", "").strip()
    if not value:
        return None
    from . import webfetch                  # 惰性 import：避免模块级环依赖
    return value if webfetch.looks_like_proxy_url(value) else None


def api_proxy_env(gateway):
    """某个网关专用的代理变量名后缀（`API_PROXY_DEEPSEEK`）；与 `BASE_<网关>` 同一套命名。"""
    return "API_PROXY_" + str(gateway).upper()


def _opener_for(proxy):
    """按代理 URL 取 opener；无代理时返回 `_OPENER` 本身（测试就 patch 它）。"""
    if not proxy:
        return _OPENER
    hit = _PROXY_OPENERS.get(proxy)
    if hit is None:
        hit = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            _SameOriginRedirects(),
            _CancellableHTTPHandler(), _CancellableHTTPSHandler())
        _PROXY_OPENERS[proxy] = hit
    return hit


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


# DeepSeek 偶发把 tool-call 语法写进 content 通道（实测 09-16 会话）。原生格式：
#   <｜DSML｜tool_calls>
#   <｜DSML｜invoke name="bash">
#   <｜DSML｜parameter name="command" string="true">ls</｜DSML｜parameter>
#   </｜DSML｜invoke>
#   </｜DSML｜tool_calls>
# GLM 走结构化 tool_calls 字段，从不泄漏。只对已知泄漏的模型族启用过滤，其他模型零开销直通。
#
# 2026-09-20 重写。第一版（09-17）在真实网关上 7 次里漏了 3 次，离线复现出三个缺陷：
#   1. 开标签被流式分片劈开就整段漏给用户——「先扣住半截 <」的那几行紧接着就被后面的放行
#      逻辑冲掉了，而单测恰好把分片切在完整开标签之后；
#   2. 回收只认 name="arguments" 包一整个 JSON 的写法，DeepSeek 原生的「一个参数一个元素」
#      被剥掉后**不回收**，模型的调用凭空消失，这一轮什么都没干就结束了；
#   3. 模型在正文里**谈论**这个标记时（开发 zylab 自己的会话里有 29 条）会被当成泄漏，
#      后面的正文被扣住、甚至丢掉。
_DSML_OUTER = ("tool_calls", "function_calls")
_DSML_OPENS = tuple(f"<｜DSML｜{name}>" for name in _DSML_OUTER)
_DSML_INVOKE_PREFIX = '<｜DSML｜invoke name="'
_DSML_INVOKE_RE = re.compile(
    r'<｜DSML｜invoke name="([^"]+)">(.*?)</｜DSML｜invoke>', re.DOTALL)
_DSML_PARAM_RE = re.compile(
    r'<｜DSML｜parameter name="([^"]+)"([^>]*)>(.*?)</｜DSML｜parameter>', re.DOTALL)


def dsml_may_leak(route, model):
    """是否对该模型启用 DSML 泄漏过滤。保守起见只认 deepseek 族。"""
    return "deepseek" in str(model or "").lower()


def _dsml_recover(block):
    """从一段 DSML 里回收**完整的** invoke；两种参数写法都认。回收不出来的那一个就丢。"""
    calls = []
    for name, body in _DSML_INVOKE_RE.findall(block):
        params = _DSML_PARAM_RE.findall(body)
        if not params:
            continue
        args = None
        if len(params) == 1 and params[0][0] == "arguments":
            try:
                parsed = json.loads(params[0][2])
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                args = parsed                         # 旧写法：整个参数对象是一段 JSON
        if args is None:
            args = {}
            for key, attrs, raw in params:
                value = raw
                if 'string="true"' not in attrs:      # string="false"：数字 / 布尔 / 对象
                    try:
                        value = json.loads(raw)
                    except (ValueError, TypeError):
                        value = raw
                args[key] = value
        calls.append({
            # id 要在整个会话里唯一：以前是 dsml-0 / dsml-1，同一个会话里每次泄漏都从 0 编起。
            "id": "dsml-" + os.urandom(4).hex(),
            "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls


class _DsmlFilter:
    """流式正文里的 DSML 泄漏过滤。feed() 返回此刻可以放行的正文；finish() 返回收尾的
    (正文, 回收出的 tool calls)。结果与流怎么分片无关。

    判定「这是泄漏而不是引用」要同时满足：不在 ``` 代码块里；从开头标记起，内容在**结构上**
    一直是调用——外层标签、invoke、invoke 里只有 parameter 元素（参数的**值**随便写什么都行，
    元素之间只能是空白）；调用之后再没有正文，模型吐完调用就停下等结果。调用之后允许再跟调用
    （并行），也允许跟一串闭标签碎屑。真实网关上抓到过三种残缺形状，都是网关自己的解析器吃掉了
    一部分标记：正文以裸 invoke 开头；正文只剩一个孤零零的 `</｜DSML｜tool_calls>`；正文是几百个
    重复的 `</｜DSML｜invoke>` / `</invoke>`（模型退化成循环）。所以裸 invoke 和孤儿闭标签也算
    开头。任何一条不满足，扣住的内容原样还给正文，一个字不丢。
    """

    _ORPHAN_CLOSE = "</｜DSML｜"
    _OPENERS = _DSML_OPENS + (_DSML_INVOKE_PREFIX, _ORPHAN_CLOSE)
    _INVOKE_CLOSE = "</｜DSML｜invoke>"
    _PARAM_PREFIX = '<｜DSML｜parameter name="'
    _PARAM_CLOSE = "</｜DSML｜parameter>"
    _HEADER_MAX = 300                 # `<｜DSML｜invoke name="…">` 这一截最长能有多长
    _DEBRIS = re.compile(r"</[^<>\n]{0,40}>|[A-Za-z_｜]{1,20}>")
    _PARTIAL_DEBRIS = re.compile(r"</?[^<>\n]{0,40}|[A-Za-z_｜]{1,20}")

    def __init__(self):
        self.held = ""              # 还没决定去向的内容
        self.in_block = False       # held 以某个开头标记起始
        self.fences = 0             # 已放行正文里 ``` 的个数
        self._ticks = 0             # 已放行正文末尾连续的 ` 个数（``` 会被分片劈开）

    def _emit(self, text):
        if self._ticks or "`" in text:
            for char in text:
                if char != "`":
                    self._ticks = 0
                    continue
                self._ticks += 1
                if self._ticks == 3:
                    self.fences += 1
                    self._ticks = 0
        return text

    def feed(self, chunk):
        self.held += chunk
        out = ""
        while self.held:
            if not self.in_block:
                hits = [self.held.find(tag) for tag in self._OPENERS if tag in self.held]
                if hits:
                    at = min(hits)
                    out += self._emit(self.held[:at])
                    self.held = self.held[at:]
                    if self.fences % 2:
                        # 代码块里的标记是模型在引用：放掉开头那个 "<"，继续往后找
                        out += self._emit(self.held[:1])
                        self.held = self.held[1:]
                        continue
                    self.in_block = True
                    continue
                # 没有完整的开头标记：只扣住「可能是它的开头」的那截尾巴
                keep = 0
                for tag in self._OPENERS:
                    for size in range(min(len(tag) - 1, len(self.held)), keep, -1):
                        if self.held.endswith(tag[:size]):
                            keep = size
                            break
                out += self._emit(self.held[:len(self.held) - keep])
                self.held = self.held[len(self.held) - keep:]
                break
            if self._judge() == "wait":
                break
            # 不是调用：正文里提了一下这个标记，或者引用完一整块之后接着讲解。原样放行——
            # 先放掉开头那个 "<"，剩下的回到普通模式继续扫（后面也许还有真的调用）。
            self.in_block = False
            out += self._emit(self.held[:1])
            self.held = self.held[1:]
        return out

    def _partial(self, rest, *tags):
        return any(tag.startswith(rest) for tag in tags)

    def _judge(self):
        """held 以开头标记起始。它在结构上还像不像「一串调用，后面除了碎屑什么都没有」？"""
        text, pos, size = self.held, 0, len(self.held)
        tags = _DSML_OPENS + tuple("</" + tag[1:] for tag in _DSML_OPENS)
        while True:
            while pos < size and text[pos].isspace():
                pos += 1
            if pos >= size:
                return "wait"                          # 到目前为止都像调用，等流结束再定
            outer = next((tag for tag in tags if text.startswith(tag, pos)), None)
            if outer is not None:
                pos += len(outer)                      # 外层的开 / 闭标签只是包装，跳过
                continue
            if text.startswith(_DSML_INVOKE_PREFIX, pos):
                header = text.find('">', pos, pos + self._HEADER_MAX)
                if header < 0:
                    return "wait" if size - pos < self._HEADER_MAX else "quote"
                pos = header + 2
                while True:                            # invoke 里只能是 parameter 元素
                    while pos < size and text[pos].isspace():
                        pos += 1
                    if pos >= size:
                        return "wait"
                    if text.startswith(self._INVOKE_CLOSE, pos):
                        pos += len(self._INVOKE_CLOSE)
                        break
                    if text.startswith(self._PARAM_PREFIX, pos):
                        close = text.find(self._PARAM_CLOSE, pos)
                        if close < 0:
                            return "wait"              # 参数的值还没收完，里面写什么都行
                        pos = close + len(self._PARAM_CLOSE)
                        continue
                    if self._partial(text[pos:], self._INVOKE_CLOSE, self._PARAM_PREFIX):
                        return "wait"
                    return "quote"
                continue
            rest = text[pos:]
            if self._partial(rest, *self._OPENERS, *tags):
                return "wait"                          # 也许是下一个调用的开头
            debris = self._DEBRIS.match(text, pos)
            if debris:
                pos = debris.end()
                continue
            if len(rest) <= 45 and self._PARTIAL_DEBRIS.fullmatch(rest):
                return "wait"                          # 半个闭标签，还没收完
            return "quote"

    def finish(self):
        held, self.held = self.held, ""
        if not self.in_block:
            return self._emit(held), []
        self.in_block = False
        # 没有一个完整的 invoke（多半是被 max_tokens 截断）：语法不给用户看，也没有可执行的调用。
        return "", _dsml_recover(held)


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


def _tool_args_ok(call):
    """这个 tool call 的 arguments 是不是合法 JSON。"""
    try:
        json.loads(str(((call or {}).get("function") or {}).get("arguments") or ""))
    except (ValueError, TypeError):
        return False
    return True


def repair_truncated_tool_calls(calls):
    """把流式截断产生的残缺 tool arguments 换成合法占位 JSON。

    这是**唯一**的规范实现：`core.agent._repair_truncated_calls` 与出站净化
    都委托到这里，避免两处逻辑漂移（09-20 事故的根因之一就是修复只发生在
    一条路径上）。占位形如 `{"_truncated": "<原始前 120 字符>"}`：JSON 合法、
    原始片段可追溯，模型看到占位会自行重发调用。`id` 保持不变，所以
    tool_call_id 与 tool 结果的配对不受影响。
    """
    repaired = []
    for call in calls or ():
        if _tool_args_ok(call):
            repaired.append(call)
            continue
        fn = dict((call or {}).get("function") or {})
        raw_args = str(fn.get("arguments") or "")
        fn["arguments"] = json.dumps(
            {"_truncated": raw_args[:120]}, ensure_ascii=False)
        call = dict(call or {})
        call["function"] = fn
        repaired.append(call)
    return repaired


def _sanitize_outgoing_messages(messages):
    """净化**发出去的那份副本**里残缺的 tool arguments。

    为什么必须在出站这一层再做一次：服务端每轮都重新解析整个 `messages`，
    所以历史里只要躺着一条未闭合的 arguments，此后**每一次**请求都会 400
    （实测 DeepInfer：`Unterminated string starting at: line 1 column 13`），
    重试与换模型都无效——因为历史没变。已经落盘的会话也靠这里自愈。

    只替换出问题的那几条消息，其余对象原样复用；**不修改调用方传进来的
    列表**，会话文件里的原始字节保持不变，便于事后取证。
    """
    if not messages:
        return messages
    out = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls")
        if not calls or all(_tool_args_ok(call) for call in calls):
            continue
        if out is None:
            out = list(messages)
        fixed = dict(message)
        fixed["tool_calls"] = repair_truncated_tool_calls(calls)
        out[index] = fixed
    return messages if out is None else out


def stream_chat(model, messages, tools=None, max_tokens=8192, temperature=0.3,
                timeout=600, cancel=None, retries=3, phases=None,
                gateway=None, route=None, temperature_fallback=True,
                metrics=_DEFAULT_METRICS, trace_context=None,
                route_explicit=False, route_warning=None,
                before_attempt=None, thinking=None, effort=None,
                effort_fallback=True):
    """流式请求，带两种自动降级。temperature=None 表示不发这个参数。

    `effort` 是用户在 /model 里给这个模型选的推理强度，只在走 /v1/responses 时发
    （chat/completions 上的 reasoning_effort 实测有害，见 README）。模型不收这个值就
    按模型默认重发并提示；`effort_fallback=False` 只给「问网关收哪些值」用。

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

    # 历史里可能躺着流式截断留下的未闭合 tool arguments。放在重试循环**之上**
    # 只做一次：每次 attempt 都用这份净化过的副本，且不会重复扫描。
    messages = _sanitize_outgoing_messages(messages)

    delay = 0.6
    max_attempts = max(1, int(retries))
    transient_failures = 0
    attempt_no = 0
    adapt = _adaptations(route, model)
    tried = set()                 # 同一次请求里不重复同一个适配：防来回切的死循环
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
                    thinking=thinking,
                    max_tokens_param=adapt["max_tokens_param"],
                    reasoning_effort=adapt["reasoning_effort"],
                    api=adapt["api"], effort=effort):
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
            # 网关指明了换法的参数适配：probe 也照样自动换（它关掉的只是
            # temperature 降级 —— 那是能力，这些只是请求的写法）。
            effort_retry = bool(
                not emitted and not temperature_retry and effort_fallback
                and effort is not None and adapt["api"] == "responses"
                and "reasoning.effort" in str(e))
            adaptation = (
                None if (emitted or temperature_retry or thinking_retry
                         or effort_retry)
                else _adaptation_for(e, adapt, tried))
            if (not emitted and adapt["api"] == "chat"
                    and adapt["reasoning_effort"] == "none"
                    and "does not support 'none'" in str(e)):
                # 记下的适配被网关当面否掉了（gpt-6-astra：要关推理才能带工具，
                # 可它不接受关推理）。忘掉它 —— 否则哪天网关支持了，是这条记忆
                # 本身把模型挡在门外。本次照常报错。
                adapt["reasoning_effort"] = None
                _remember_adaptation(route, model, "reasoning_effort", None)
            transient_retry = False
            if (not emitted and not temperature_retry and adaptation is None
                    and not effort_retry):
                transient_failures += 1
                transient_retry = bool(
                    transient_failures < max_attempts
                    and _is_transient(e))
            retry_reason = (
                "temperature_fallback" if temperature_retry
                else "effort_fallback" if effort_retry
                else f"{adaptation[0]}_fallback" if adaptation
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
            if effort_retry:
                if route_warning is not None:
                    route_warning(
                        f"{model} 不接受推理强度 {effort}，本次按模型默认")
                try:
                    # 拒绝原话里就是这个模型真正收的值：顺手更正能力表，
                    # /model 下次弹出的选项就对了。
                    from . import models as _models
                    options = _models.parse_supported_values(e)
                    if options:
                        _models.update_record(
                            route.name, model,
                            lambda rec: rec.update(effort_options=options))
                except Exception:                          # noqa: BLE001
                    pass
                effort = None
                continue          # 确定性修复：不算瞬时故障
            if adaptation is not None:
                field, value = adaptation
                tried.add(adaptation)
                adapt[field] = value
                _remember_adaptation(route, model, field, value)
                if route_warning is not None and field == "reasoning_effort":
                    route_warning(
                        f"{model} 在 chat/completions 上带工具时不支持推理，"
                        "已按网关提示设 reasoning_effort=none")
                if route_warning is not None and field == "api":
                    route_warning(
                        f"{model} 改走 /v1/responses（网关的要求）"
                        if value == "responses" else
                        f"{route.name} 没有 /v1/responses，{model} 退回 chat/completions")
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


# 每个模型要怎么发请求 —— **只收网关在拒绝原话里明确指出了做法的**，不猜。
# 2026-09-24 对真实网关实测新一代 OpenAI 模型，连着撞了两条：
#   gpt-6-sol / gpt-6-astra  `'max_tokens' is not supported with this model.
#                            Use 'max_completion_tokens' instead.`
#   同上（换完参数名之后）    `Function tools with reasoning_effort are not supported
#                            … set reasoning_effort to 'none'.`
# 而很多 OpenAI 兼容网关只认 `max_tokens`、不认 reasoning_effort。所以先按最通行的发，
# 被这样拒了就照做、重发，并记进能力表（下次、换个进程都直接用对的）。
_ADAPTATIONS = {}
_ADAPTATION_DEFAULTS = {"max_tokens_param": "max_tokens",
                        "reasoning_effort": None,
                        # 走哪个接口。同一条原则：网关在拒绝里说「use /v1/responses」才切。
                        "api": "chat"}


def _adaptations(route, model):
    key = (route.name, str(model))
    cached = _ADAPTATIONS.get(key)
    if cached is None:
        cached = dict(_ADAPTATION_DEFAULTS)
        try:
            from . import models as _models
            record = _models.get(route.name, model)
            if record.get("max_tokens_param") == "max_completion_tokens":
                cached["max_tokens_param"] = "max_completion_tokens"
            if record.get("reasoning_effort") == "none":
                cached["reasoning_effort"] = "none"
            if record.get("api") == "responses":
                cached["api"] = "responses"
        except Exception:                                  # noqa: BLE001
            pass                  # 能力表读不了就按最通行的发
        _ADAPTATIONS[key] = cached
    return dict(cached)


def _adaptation_for(error, current, tried=()):
    """拒绝原话里若指明了换法，返回 (字段, 新值)；否则 None。已试过的跳过、看下一条。

    同一句拒绝常常给出两条路（gpt-6-sol：「use /v1/responses or set
    reasoning_effort to 'none'」）。**先换接口**：关推理能让它跑起来，但那是让模型
    关着脑子干活；Responses 上工具与推理可以同时开（2026-09-24 实测两个 gpt-6 都是）。
    换接口试过而网关没有这个接口时，才轮到「关推理」。
    """
    text = str(error)
    candidates = []
    if current["api"] == "responses":
        # 网关根本没有这个接口：退回 chat，接着按 chat 的规矩适配。
        if getattr(error, "status", None) in (404, 405):
            candidates.append(("api", "chat"))
        # 下面几条都是 chat/completions 的写法，这里不适用
    else:
        if "/v1/responses" in text:
            candidates.append(("api", "responses"))
        if (current["max_tokens_param"] == "max_tokens"
                and "max_completion_tokens" in text):
            candidates.append(("max_tokens_param", "max_completion_tokens"))
        if (current["reasoning_effort"] is None and "reasoning_effort" in text
                and "'none'" in text):
            candidates.append(("reasoning_effort", "none"))
    for candidate in candidates:
        if candidate not in tried:
            return candidate
    return None


def _remember_adaptation(route, model, field, value):
    _ADAPTATIONS.setdefault(
        (route.name, str(model)), dict(_ADAPTATION_DEFAULTS))[field] = value
    try:
        from . import models as _models
        _models.update_record(
            route.name, model, lambda rec: rec.update({field: value}))
    except Exception:                                      # noqa: BLE001
        pass                      # 记不上不影响本次请求


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
                 thinking=None, max_tokens_param="max_tokens",
                 reasoning_effort=None, api="chat", effort=None):
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
    if api == "responses":
        yield from _stream_once_responses(
            model, messages, tools, max_tokens, temperature, timeout, cancel,
            attempt, phases, route=route, effort=effort)
        return
    payload = {"model": model, "messages": messages,
               max_tokens_param: max_tokens,
               "stream": True, "stream_options": {"include_usage": True}}
    if temperature is not None:
        payload["temperature"] = temperature
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
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
    # DSML 泄漏过滤必须在累积缓冲上做：标记会被 chunk 边界劈开。见 _DsmlFilter。
    dsml = _DsmlFilter() if dsml_may_leak(route, model) else None
    usage = {}
    reason = None
    terminal = False
    if _cancelled(cancel):
        _mark_phase(phases, "ended", attempt)
        raise Interrupted("用户中断")

    _mark_phase(phases, "started", attempt)
    _ACTIVE_CANCEL.handle = cancel      # connect() 里把 socket 绑到这个句柄上
    try:
        resp = _opener_for(api_proxy(route)).open(req, timeout=timeout)
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
                        if dsml is not None:
                            safe_text = dsml.feed(d["content"])
                            if safe_text:
                                yield {"t": "text", "v": safe_text}
                        else:
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
    # 流结束：扣住的内容此刻才能定性——是泄漏就回收成真正的 tool 事件，不是就原样放行。
    leaked_calls = []
    if dsml is not None:
        tail_text, leaked_calls = dsml.finish()
        if tail_text:
            yield {"t": "text", "v": tail_text}

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
    elif leaked_calls:
        # 调用方拿到 tool 事件是「整份替换」而不是追加：结构化字段里有调用时以它为准，
        # 正文里漏出来的那份只剥掉、不再执行一遍。
        yield {"t": "tool", "v": leaked_calls}
    yield {"t": "done", "reason": reason, "usage": usage}


# ------------------------------------------------------------ /v1/responses
# 一部分模型只有在 /v1/responses 上才能**同时**带工具和推理（2026-09-24 实测 Boyue：
# gpt-6-astra 在 chat/completions 上带工具必须关推理、而它不接受关推理；gpt-6-sol
# 能在 chat 上跑，但推理是关着的）。什么时候走这条路**不写名单**：网关在拒绝原话里
# 说「use /v1/responses」才切，记进能力表（见 _adaptation_for 的 api 字段）。
#
# 对上层透明：这里把 zylab 的 chat 形态历史翻成 Responses 的 input items，再把它的
# 流式事件翻回 zylab 的内部事件（text / reasoning / tool / done）。上层的代理循环、
# 计费、压缩一行都不用改。
#
# store=false：不让网关那边存对话（chat 路径本来也不存）。推理内容不跨轮保留 ——
# chat 路径同样不回传 reasoning_content，两边行为一致。

def _responses_text(content):
    """chat 的 content（字符串或 parts 列表）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and part.get("type") in (
                    "text", "input_text", "output_text"):
                pieces.append(str(part.get("text") or ""))
        return "".join(pieces)
    return "" if content is None else str(content)


def _responses_user_content(content):
    """用户消息：纯文本原样；带图片的 parts 换成 Responses 的 input_text / input_image。"""
    if not isinstance(content, list):
        return _responses_text(content)
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text"):
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                parts.append({"type": "input_image", "image_url": str(url)})
    return parts or ""


def _responses_input(messages):
    """chat 形态的历史 → (instructions, input items)。

    开头的 system 进 instructions；中途插进来的 system 保持原位，作 developer 消息。
    工具调用拆成独立的 function_call item，工具结果成 function_call_output，两者
    靠 call_id 对上 —— 别家模型留下的 id（toolu_…、call_…）照用，网关只要求一致。
    """
    instructions, items = [], []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            text = _responses_text(content)
            if not items:
                instructions.append(text)
            elif text:
                items.append({"role": "developer", "content": text})
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": _responses_text(content)})
        elif role == "assistant":
            text = _responses_text(content)
            if text:
                items.append({"role": "assistant", "content": text})
            for call in message.get("tool_calls") or ():
                function = (call or {}).get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str((call or {}).get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or "{}")})
        else:
            items.append({"role": "user",
                          "content": _responses_user_content(content)})
    return "\n\n".join(text for text in instructions if text), items


def _responses_tools(tools):
    """chat 的 {"type":"function","function":{…}} → Responses 的扁平写法。"""
    converted = []
    for tool in tools or ():
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        converted.append({
            "type": "function",
            "name": function["name"],
            "description": function.get("description") or "",
            "parameters": (function.get("parameters")
                           or {"type": "object", "properties": {}}),
            # 显式关掉：strict 模式要求每个参数都必填，zylab 的工具有可选参数。
            "strict": False})
    return converted


def _responses_usage(usage):
    """Responses 的用量字段 → zylab 统一用的 chat 形态（normalize_cache 等照读）。"""
    usage = usage if isinstance(usage, dict) else {}
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    return {
        "prompt_tokens": int(usage.get("input_tokens") or 0),
        "completion_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "prompt_tokens_details": {"cached_tokens": int(cached or 0)},
        "completion_tokens_details": {"reasoning_tokens": int(reasoning or 0)},
    }


def _stream_once_responses(model, messages, tools=None, max_tokens=8192,
                           temperature=None, timeout=600, cancel=None,
                           attempt=1, phases=None, route=None, effort=None):
    """/v1/responses 的一次流式请求；yield 的事件与 _stream_once（chat）完全同形。

    连接、取消、阶段打点与 _stream_once 一一对应；改一边时对照另一边。
    """
    route = route or route_for()
    instructions, items = _responses_input(messages)
    payload = {"model": model, "input": items, "stream": True,
               "store": False, "max_output_tokens": max_tokens}
    if instructions:
        payload["instructions"] = instructions
    if temperature is not None:
        payload["temperature"] = temperature
    if effort:
        payload["reasoning"] = {"effort": effort}
    provider_tools = _responses_tools(_provider_tool_schemas(tools, route, model))
    if provider_tools:
        payload["tools"] = provider_tools
        payload["tool_choice"] = "auto"

    req = urllib.request.Request(
        f"{route.base}/responses", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key(route)}",
                 "Content-Type": "application/json", "Accept": "text/event-stream"})

    calls = {}        # item id -> {call_id, name, args, index}
    usage = {}
    reason = None
    used_effort = None
    terminal = False
    if _cancelled(cancel):
        _mark_phase(phases, "ended", attempt)
        raise Interrupted("用户中断")

    _mark_phase(phases, "started", attempt)
    _ACTIVE_CANCEL.handle = cancel
    try:
        resp = _opener_for(api_proxy(route)).open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        _mark_phase(phases, "ended", attempt)
        raise _http_error(e, route, model) from None
    except APIError as e:
        _mark_phase(phases, "ended", attempt)
        if e.gateway is None:
            e.gateway = route.name
        if e.model is None:
            e.model = model
        raise
    except Exception as e:
        _mark_phase(phases, "ended", attempt)
        if _cancelled(cancel):
            raise Interrupted("用户中断") from None
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
                if not line.startswith("data:"):
                    continue          # `event:` 行与 data 里的 type 重复
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    delta = event.get("delta") or ""
                    if delta:
                        _mark_phase(phases, "first_delta", attempt)
                        _mark_phase(phases, "last_delta", attempt)
                        yield {"t": "text", "v": delta}
                elif kind in ("response.reasoning_summary_text.delta",
                              "response.reasoning_text.delta"):
                    delta = event.get("delta") or ""
                    if delta:
                        _mark_phase(phases, "first_delta", attempt)
                        _mark_phase(phases, "last_delta", attempt)
                        yield {"t": "reasoning", "v": delta}
                elif kind in ("response.output_item.added",
                              "response.output_item.done"):
                    item = event.get("item") or {}
                    if item.get("type") != "function_call":
                        continue
                    _mark_phase(phases, "first_delta", attempt)
                    _mark_phase(phases, "last_delta", attempt)
                    slot = calls.setdefault(
                        str(item.get("id") or event.get("output_index")),
                        {"call_id": None, "name": "", "args": "",
                         "index": event.get("output_index", len(calls))})
                    slot["call_id"] = item.get("call_id") or slot["call_id"]
                    slot["name"] = item.get("name") or slot["name"]
                    # 完成态里的 arguments 是整份，以它为准；added 时通常是空串。
                    if item.get("arguments"):
                        slot["args"] = item["arguments"]
                elif kind == "response.function_call_arguments.delta":
                    slot = calls.get(str(event.get("item_id")))
                    if slot is not None:
                        slot["args"] += event.get("delta") or ""
                        _mark_phase(phases, "last_delta", attempt)
                elif kind in ("response.completed", "response.incomplete"):
                    body = event.get("response") or {}
                    usage = _responses_usage(body.get("usage"))
                    used_effort = (body.get("reasoning") or {}).get("effort")
                    if kind == "response.incomplete":
                        why = (body.get("incomplete_details") or {}).get("reason")
                        reason = ("length" if why == "max_output_tokens"
                                  else str(why or "incomplete"))
                    terminal = True
                elif kind == "response.failed":
                    error = ((event.get("response") or {}).get("error") or {})
                    code = str(error.get("code") or "")
                    raise APIError(
                        f"response.failed: {error.get('message') or error}",
                        kind="provider", code=code or None,
                        gateway=route.name, model=model,
                        retryable=code in ("server_error", "rate_limit_exceeded"))
                elif kind == "error":
                    raise APIError(
                        f"responses error: {event.get('message') or event}",
                        kind="provider", code=event.get("code"),
                        gateway=route.name, model=model, retryable=False)
    except Interrupted:
        raise
    except APIError:
        raise                     # 上面自己抛的，别被下面包成「读取失败」
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
            raise Interrupted("用户中断")
        raise APIError(
            "流式响应在 response.completed 前结束",
            kind="stream_eof", gateway=route.name, model=model,
            retryable=True)

    if calls:
        ordered = sorted(
            calls.values(),
            key=lambda slot: slot["index"] if isinstance(slot["index"], int) else 0)
        yield {"t": "tool", "v": [
            {"id": slot["call_id"] or f"{slot['name']}:{index}",
             "type": "function",
             "function": {"name": slot["name"], "arguments": slot["args"] or "{}"}}
            for index, slot in enumerate(ordered)]}
    if reason is None:
        reason = "tool_calls" if calls else "stop"
    done = {"t": "done", "reason": reason, "usage": usage}
    if used_effort:
        done["effort"] = used_effort
    yield done


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


def list_models(gateway=None, route=None, timeout=60):
    route = route or route_for(gateway)
    require_secure_transport(route, purpose="model_catalog")
    req = urllib.request.Request(
        f"{route.base}/models",
        headers={"Authorization": f"Bearer {api_key(route)}"})
    with _opener_for(api_proxy(route)).open(req, timeout=timeout) as r:
        return json.loads(r.read()).get("data", [])
