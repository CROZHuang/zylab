"""路由感知的 web_fetch。

企业网络里常见的形态：几条出网路由互斥，每条放行的域名都不一样，**选错路由的
失败长得像目标挂了**。所以本模块按 host 决定走直连还是走代理，而不是照搬环境
变量里的代理设置 —— 那个通常是给别的用途配的（只放行某几个域名，连 GitHub 都封）。

两份名单都是配置，默认只有最普适的几条：
  - ``web.direct_suffixes``：这些 host 只走直连（默认 GitHub 及其 raw 域名）；
    其余走代理。内网域名往往只有直连能通，加进来。
  - ``web.blocked_hosts``：全路由死路。不发请求，直接告诉用户替代品
    （典型例子：某些网络里 ``pypi.org`` 不通，只有镜像能用）。

安全红线：代理 URL 常内嵌凭据。它**不硬编码进仓库**（运行时从 settings →
环境变量 → shell 环境文件兜底解析），且任何输出、报错、溯源行里的 URL 一律经
:func:`redact` 脱敏。
"""
from __future__ import annotations
from pathlib import Path
from . import paths

import html
import io
import json
import os
import re
import urllib.error
import urllib.parse
import ipaddress
import socket
import urllib.request
from html.parser import HTMLParser

MAX_CHARS = 30_000            # 与工具结果上限 MAX_OUT 对齐
MAX_BYTES = 2 * 1024 * 1024   # 下载体积上限；超过即停并显式标记
MAX_TIMEOUT = 60.0
DEFAULT_TIMEOUT = 20.0
USER_AGENT = "zylab-webfetch/1 (personal research CLI)"

# host 后缀 → 只走直连，其余走代理。内置默认只保留最普适的一条：GitHub 在
# 多数企业代理后面反而更慢或直接被封。内网域名用 settings 的
# ``web.direct_suffixes`` 追加（`ZYLAB_DIRECT_SUFFIXES` 也可，逗号分隔）。
BUILTIN_DIRECT_SUFFIXES = ("github.com", "githubusercontent.com")


def _settings_web():
    """只读 settings.json 的 `web` 段。

    刻意不 import core.settings：本模块被工具层在很早期 import，而这里只需要
    两份名单。读不到就当没有 —— 配置损坏不该让 import 失败。
    """
    try:
        with open(paths.state_home() / "settings.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    value = data.get("web")
    return value if isinstance(value, dict) else {}


_WEB = _settings_web()
DIRECT_SUFFIXES = tuple(dict.fromkeys(
    [*BUILTIN_DIRECT_SUFFIXES,
     *(str(s).strip().lower() for s in (_WEB.get("direct_suffixes") or [])
       if str(s).strip()),
     *(s.strip().lower()
       for s in str(paths.env_get("DIRECT_SUFFIXES") or "").split(",")
       if s.strip())]))
# 全路由死路：不发请求，直接给出路。**内置为空** —— 哪些 host 不通是网络事实，
# 因网络而异，由 settings 的 ``web.blocked_hosts``（host → 替代方案）声明。
# 内置与生效值分开暴露：测试必须能断言「默认不封任何东西」，而生效值在配过的
# 机器上必然非空，断言它等于把测试绑到某个人的配置上。
BUILTIN_BLOCKED_HOSTS = {}
BLOCKED_HOSTS = {
    **BUILTIN_BLOCKED_HOSTS,
    **{str(host).strip().lower(): str(hint or "")
       for host, hint in (_WEB.get("blocked_hosts") or {}).items()
       if str(host).strip()},
}

# 兜底的 shell 环境文件：有些部署把代理连同凭据写在一个 source 进来的脚本里。
# 家目录因人而异，所以路径可覆盖。找不到不是错误 —— 直连路由仍可用；
# 但要让用户知道缺什么。
_ENV_FILE = paths.env_get("ENV_FILE") or str(
    paths.home_dir() / ".env-persistent.sh")
_LEGACY_ENV_FILES = paths.legacy_env_files()
_PROXY_LINE = re.compile(r"^_PROXY_[A-Z0-9_]*='([^']+)'", re.M)


class WebFetchError(RuntimeError):
    """可解释的抓取失败；message 已脱敏。"""


def redact(url):
    """去掉 URL 里的 userinfo —— 代理凭据绝不能进任何输出。"""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return "<无法解析的 URL>"
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        parts = parts._replace(netloc=f"…@{host}")
    return urllib.parse.urlunsplit(parts)


def looks_like_proxy_url(value):
    """值是否真是一个代理 URL（scheme://host），而不是别的什么东西。

    09-20 实测的事故：环境文件里 `_PROXY_CLEAR='unset http_proxy …'` 排在最前，
    旧实现取**第一个** `_PROXY_*` 匹配，于是把一条 shell 命令当代理交给
    ProxyHandler，urllib 抛 `InvalidURL: URL can't contain control characters`，
    代理这条腿当场失效、退到直连再超时——用户看到的只是「两条路由都失败」。
    """
    try:
        parts = urllib.parse.urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https", "socks5", "socks5h") and bool(parts.netloc)


def resolve_proxy(cfg=None):
    """代理 URL 的解析链：settings → ZYLAB_WEB_PROXY → shell 环境文件。

    每一级都要过 `looks_like_proxy_url`：形态不对就继续往下找，绝不把一个
    不是 URL 的值交给 ProxyHandler。找不到返回 None（直连仍可用）；
    带凭据的值从不落日志。

    **刻意不读标准的 http(s)_proxy 环境变量。** 09-20 实测：本机那四个变量
    全部指向「只通 AI 厂商域名」的那条代理，拿它抓通用网页一律
    `Tunnel connection failed: 403`——环境里「有代理」不等于「这条代理通得了
    目标」。网页抓取要用哪条代理必须显式声明（上面三级之一）。
    """
    web = (cfg or {}).get("web") if isinstance(cfg, dict) else None
    if isinstance(web, dict) and looks_like_proxy_url(web.get("proxy")):
        return str(web["proxy"]).strip()
    env = paths.env_get("WEB_PROXY", "").strip()
    if looks_like_proxy_url(env):
        return env
    for candidate in (_ENV_FILE,) + _LEGACY_ENV_FILES:
        try:
            with open(candidate, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        # 取**第一个形态合法的**，而不是第一个匹配：同一个文件里既有
        # `_PROXY_CLEAR`（清代理的 shell 命令）也有真正的代理地址。
        for found in _PROXY_LINE.finditer(text):
            value = found.group(1).strip()
            if looks_like_proxy_url(value):
                return value
    return None


def proxy_setup_hint():
    """代理缺失时给出**可操作**的补救，而不是只说「未配置」。

    新装的人最常撞这条：直连路由能通，走代理的 host 全挂，而错误只说
    「两条路由都失败」——他不知道缺的是什么、更不知道去哪配。
    """
    return (
        "未找到代理配置，需要走代理的 host 无法访问。"
        f"直连的 host（{', '.join(DIRECT_SUFFIXES)}）不受影响。三选一：\n"
        f"  export {paths.env_name('WEB_PROXY')}='http://<user>:<pass>@<host>:<port>/'\n"
        f"  或在 {_ENV_FILE} 写一行 _PROXY_<名字>='http://…'\n"
        f"  或在 {paths.state_home() / 'settings.json'} 里设 {{\"web\": {{\"proxy\": \"...\"}}}}")


def route_of(host):
    host = str(host or "").lower().rstrip(".")
    for blocked, hint in BLOCKED_HOSTS.items():
        if host == blocked or host.endswith("." + blocked):
            raise WebFetchError(hint)
    for suffix in DIRECT_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return "direct"
    return "proxy"


class _TextExtractor(HTMLParser):
    _DROP = {"script", "style", "noscript", "template", "svg"}
    _BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4",
              "h5", "h6", "section", "article", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = io.StringIO()
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._DROP:
            self._skip += 1
        elif tag in self._BREAK:
            self.out.write("\n")

    def handle_endtag(self, tag):
        if tag in self._DROP and self._skip:
            self._skip -= 1
        elif tag in self._BREAK:
            self.out.write("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.write(data)


def html_to_text(markup):
    parser = _TextExtractor()
    try:
        parser.feed(str(markup or ""))
        parser.close()
    except Exception:                                  # noqa: BLE001
        # 残缺 HTML 不该让抓取失败；退回粗剥标签。
        stripped = re.sub(r"<[^>]+>", " ", str(markup or ""))
        return html.unescape(stripped)
    text = parser.out.getvalue()
    text = re.sub(r"[ \t\r\f]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def render_body(content_type, raw):
    ctype = str(content_type or "").split(";")[0].strip().lower()
    charset = "utf-8"
    match = re.search(r"charset=([\w\-]+)", str(content_type or ""), re.I)
    if match:
        charset = match.group(1)
    try:
        text = raw.decode(charset, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    if ctype.endswith("/json") or ctype.endswith("+json"):
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except ValueError:
            return text
    if ctype in ("text/html", "application/xhtml+xml"):
        return html_to_text(text)
    return text


MAX_REDIRECTS = 5


def _intranet_declared(host):
    """host 是否落在 DIRECT_SUFFIXES 声明的域名下。

    这份名单本来是选路由用的，但它同时是**唯一一份**「哪些内网目标是合法的」
    声明 —— 企业内网域名常解析到 10.x / 192.168.x 私网地址。私网 IP 一刀切会把
    这些合法目标全封掉，所以私网只对名单内的域名放行。"""
    host = str(host or "").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in DIRECT_SUFFIXES)


def _check_address_policy(host):
    """SSRF 边界：按解析后的地址拒绝本机 / 链路本地 / 云 metadata / 未指定 /
    组播 / 保留地址；RFC1918 私网仅对已声明的内网域名放行。

    已知局限（不假装没有）：这是「先解析再连接」，DNS rebinding 可以在两次
    解析之间换答案；经代理时由代理解析，本地解析不到的 host 这里放行，交给
    代理自己的策略。两者都不是这一层能闭合的。"""
    name = str(host or "").strip("[]")
    try:
        addrs = [ipaddress.ip_address(name)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError):
            return
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    for addr in addrs:
        if (addr.is_loopback or addr.is_link_local or addr.is_unspecified
                or addr.is_multicast or addr.is_reserved):
            raise WebFetchError(
                f"拒绝访问 {host} —— 解析到 {addr}（本机 / 链路本地 / 保留地址）；"
                "web_fetch 不做内网与 metadata 探测")
        if addr.is_private and not _intranet_declared(host):
            raise WebFetchError(
                f"拒绝访问 {host} —— 解析到私网地址 {addr}，且不在已声明的内网"
                "域名（DIRECT_SUFFIXES）内")


def _validate_target(url, *, previous_scheme=None):
    """每一跳都要过的检查（第一跳也不例外）。返回 urlsplit 结果。"""
    parts = urllib.parse.urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https"):
        raise WebFetchError(f"只支持 http/https，收到 {parts.scheme or '空'}")
    if previous_scheme == "https" and parts.scheme == "http":
        raise WebFetchError(f"拒绝 HTTPS→HTTP 降级重定向 @ {redact(url)}")
    if not parts.hostname:
        raise WebFetchError("URL 缺少主机名")
    route_of(parts.hostname)            # BLOCKED_HOSTS 也逐跳生效
    _check_address_policy(parts.hostname)
    return parts


class _HopGuard(urllib.request.HTTPRedirectHandler):
    """重定向的每一跳重新过 _validate_target。

    没有它的话，一次对 web_fetch 的批准等于批准了目标站愿意重定向到的任何
    地方 —— 包括 127.0.0.1、10.x 和 169.254.169.254。"""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_target(
            target,
            previous_scheme=urllib.parse.urlsplit(req.full_url).scheme)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_via(url, route, proxy, timeout):
    handler = urllib.request.ProxyHandler(
        {} if route == "direct" or not proxy
        else {"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler, _HopGuard())
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "*/*"})
    return opener.open(request, timeout=timeout)


def _within_deadline(fn, seconds):
    """在**墙钟** seconds 秒内跑完 fn，否则抛 TimeoutError。

    urllib 的 timeout 只约束**单次 socket 操作**：connect、TLS 握手、代理的
    CONNECT、每一次 read 各拿一份完整预算，重定向的每一跳再来一轮。09-20 实测：
    `timeout=30` 的调用跑了 270 秒，`timeout=10` 跑了 90 秒——交互式会话里
    一个工具调用沉默四分半，模型和用户都只能干等。

    阻塞中的 socket 操作无法从外部打断，所以放进守护线程、到点就不再等它；
    被放弃的线程会在自己的 socket 超时后自然结束。异常原样带回调用方，
    于是 HTTPError「不换路由」、_HopGuard 拒绝「不重试」两条既有契约不变。
    """
    import threading
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:                   # noqa: BLE001 - 原样带回
            box["error"] = exc

    worker = threading.Thread(target=run, name="zylab-webfetch", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"超过墙钟上限 {seconds:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def fetch(url, *, timeout=DEFAULT_TIMEOUT, cfg=None, max_bytes=MAX_BYTES,
          max_chars=MAX_CHARS, opener=_open_via):
    """抓取一个 URL，按 host 选路由，主路由连接层失败换另一条重试一次。

    返回 (正文文本, 溯源行)。凡进入返回值/异常的 URL 均已脱敏。
    """
    url = str(url or "").strip()
    parts = _validate_target(url)
    try:
        timeout = min(MAX_TIMEOUT, max(1.0, float(timeout)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    primary = route_of(parts.hostname)
    order = [primary, "proxy" if primary == "direct" else "direct"]
    proxy = resolve_proxy(cfg)
    notes = []
    proxy_missing = False
    last_error = None
    for route in order:
        if route == "proxy" and not proxy:
            # 短句进 provenance —— 直连若成功，用户不需要一整段配置教程。
            # 完整补救只在两条路由都失败时给出（见函数末尾）。
            proxy_missing = True
            notes.append("proxy 未配置(settings web.proxy / ZYLAB_WEB_PROXY)，跳过")
            continue
        def attempt(route=route):
            with opener(url, route, proxy, timeout) as resp:
                raw = resp.read(max_bytes + 1)
                clipped = len(raw) > max_bytes
                if clipped:
                    raw = raw[:max_bytes]
                return (raw, clipped,
                        render_body(resp.headers.get("Content-Type"), raw),
                        getattr(resp, "status", 200))

        try:
            # timeout 同时是 socket 级超时与**这条路由的墙钟上限**；
            # 两条路由最坏合计 2×timeout，而不是此前实测的 9×。
            raw, clipped_bytes, body, status = _within_deadline(
                attempt, timeout)
        except urllib.error.HTTPError as exc:
            # HTTP 状态错误 = 已经到达目标，不是路由问题；不换路由。
            detail = exc.read(400).decode("utf-8", "replace")
            raise WebFetchError(
                f"HTTP {exc.code} @ {redact(url)}"
                + (f" —— {detail.strip()}" if detail.strip() else "")
            ) from None
        except WebFetchError:
            # 重定向被 _HopGuard 拒绝：这是策略结论，不是路由故障，换一条路由
            # 再试只会再拒一次，还把原因埋进「两条路由都失败」里。直接上抛。
            raise
        except Exception as exc:                       # noqa: BLE001
            last_error = f"{route}: {type(exc).__name__}: {exc}"
            notes.append(last_error)
            continue
        clipped_chars = len(body) > max_chars
        if clipped_chars:
            body = body[:max_chars]
        marks = []
        if clipped_bytes:
            marks.append(f"下载截断于 {max_bytes:,} 字节")
        if clipped_chars:
            marks.append(f"正文截断于 {max_chars:,} 字符")
        if marks:
            body += "\n[" + "；".join(marks) + "]"
        provenance = (
            f"[web_fetch {redact(url)} · route={route} · "
            f"HTTP {status} · {len(raw):,} bytes"
            + (" · " + "；".join(notes) if notes else "") + "]")
        return body, redact_text(provenance)
    if proxy_missing:
        # 全部失败了才值得给出完整补救 —— 直连若成功，用户不需要这段。
        notes.append(proxy_setup_hint())
    raise WebFetchError(
        f"两条路由都失败 @ {redact(url)} —— " + "；".join(
            redact_text(note) for note in notes))


def redact_text(text):
    """兜底脱敏：正文/报错里意外出现的带凭据 URL 也要清掉。"""
    return re.sub(r"://[^/\s:@]+:[^/\s@]+@", "://…@", str(text or ""))
