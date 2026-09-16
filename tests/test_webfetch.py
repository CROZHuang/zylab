"""web_fetch：路由选择、凭据脱敏、截断标记、失败换路由。

企业网络里几条出网路由互斥，选错路由的失败长得像目标挂了 —— 路由表是本工具的
灵魂，逐条钉死。第二重要的是**脱敏**：代理 URL 常内嵌凭据，任何输出
（正文、溯源行、报错）都不得泄漏。全部测试 mock，零真实网络。
"""
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import settings, tools, webfetch

CRED_PROXY = "http://user:S3CR3T@10.1.20.50:23128/"


class FakeResponse:
    def __init__(self, body=b"hello", ctype="text/plain", status=200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": ctype}

    def read(self, n=-1):
        out, self._body = self._body[:n], self._body[max(0, n):]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_ok(record, body=b"hello", ctype="text/plain"):
    def _open(url, route, proxy, timeout):
        record.append({"route": route, "proxy": proxy, "timeout": timeout})
        return FakeResponse(body, ctype)
    return _open


class RouteTable(unittest.TestCase):
    def test_builtin_direct_suffixes(self):
        """内置只有 GitHub —— 别的直连目标是部署事实，得由配置声明。"""
        for host in ("github.com", "api.github.com",
                     "raw.githubusercontent.com"):
            self.assertEqual(webfetch.route_of(host), "direct", host)

    def test_declared_suffix_switches_route_to_direct(self):
        self.assertEqual(webfetch.route_of("pkg.corp.example"), "proxy")
        with mock.patch.object(
                webfetch, "DIRECT_SUFFIXES",
                webfetch.BUILTIN_DIRECT_SUFFIXES + ("corp.example",)):
            self.assertEqual(webfetch.route_of("pkg.corp.example"), "direct")

    def test_general_internet_defaults_to_proxy(self):
        for host in ("huggingface.co", "arxiv.org", "example.com"):
            self.assertEqual(webfetch.route_of(host), "proxy", host)

    def test_no_host_is_blocked_by_default(self):
        """哪些 host 全路由不通是网络事实，因网络而异 —— 不替用户封任何东西。

        断言**内置**为空，不能断言 BLOCKED_HOSTS：后者是内置 ∪ 用户配置，在
        配过 web.blocked_hosts 的机器上必然非空。断言生效值就是把测试绑到开发者
        的配置上 —— 这条测试自己先踩了一次，所以把契约写在这里。
        """
        self.assertEqual(webfetch.BUILTIN_BLOCKED_HOSTS, {})

    def test_declared_blocked_host_fails_fast_with_hint_and_no_request(self):
        record = []
        with mock.patch.object(
                webfetch, "BLOCKED_HOSTS",
                {"pypi.org": "用镜像 https://mirror.example/simple"}):
            with self.assertRaises(webfetch.WebFetchError) as ctx:
                webfetch.fetch("https://pypi.org/simple/requests/",
                               opener=opener_ok(record))
        self.assertIn("mirror.example", str(ctx.exception))
        self.assertEqual(record, [], "死路 host 不该发出任何请求")

    def test_non_http_scheme_rejected(self):
        for url in ("ftp://x/y", "file:///etc/passwd", "nonsense"):
            with self.assertRaises(webfetch.WebFetchError):
                webfetch.fetch(url, opener=opener_ok([]))


class RouteFallback(unittest.TestCase):
    def _cfg(self):
        return {"web": {"proxy": CRED_PROXY}}

    def test_connection_failure_tries_other_route(self):
        calls = []

        def flaky(url, route, proxy, timeout):
            calls.append(route)
            if route == "direct":
                raise urllib.error.URLError("connection refused")
            return FakeResponse(b"ok")

        body, prov = webfetch.fetch(
            "https://github.com/x", cfg=self._cfg(), opener=flaky)
        self.assertEqual(calls, ["direct", "proxy"],
                         "github 主路由直连，失败后应换代理")
        self.assertIn("route=proxy", prov)
        self.assertIn("URLError", prov, "溯源应记录首路由为何失败")

    def test_http_error_does_not_switch_route(self):
        """HTTP 状态错误 = 已到达目标，换路由只会浪费且误导。"""
        calls = []

        def http404(url, route, proxy, timeout):
            calls.append(route)
            raise urllib.error.HTTPError(
                url, 404, "not found", None, io.BytesIO(b"missing"))

        with self.assertRaises(webfetch.WebFetchError) as ctx:
            webfetch.fetch("https://example.com/x",
                           cfg=self._cfg(), opener=http404)
        self.assertEqual(len(calls), 1)
        self.assertIn("HTTP 404", str(ctx.exception))

    def test_missing_proxy_notes_and_still_tries_direct(self):
        calls = []
        body, prov = webfetch.fetch(
            "https://example.com/x", cfg={"web": {"proxy": ""}},
            opener=opener_ok(calls))
        with mock.patch.object(webfetch, "resolve_proxy", return_value=None):
            calls2 = []
            body, prov = webfetch.fetch(
                "https://example.com/x", opener=opener_ok(calls2))
        self.assertEqual([c["route"] for c in calls2], ["direct"],
                         "无代理时唯一可试的是直连")
        self.assertIn("未配置", prov)


class Redaction(unittest.TestCase):
    def test_redact_strips_userinfo(self):
        self.assertNotIn("S3CR3T", webfetch.redact(CRED_PROXY))
        self.assertIn("…@", webfetch.redact(CRED_PROXY))

    def test_provenance_and_errors_never_leak_credentials(self):
        def leaky(url, route, proxy, timeout):
            raise urllib.error.URLError(f"cannot reach {proxy}")

        with mock.patch.object(
                webfetch, "resolve_proxy", return_value=CRED_PROXY):
            with self.assertRaises(webfetch.WebFetchError) as ctx:
                webfetch.fetch("https://example.com/x", opener=leaky)
        self.assertNotIn("S3CR3T", str(ctx.exception),
                         "报错泄漏了代理凭据")

    def test_redact_text_scrubs_embedded_urls(self):
        s = f"failed via {CRED_PROXY} retry"
        self.assertNotIn("S3CR3T", webfetch.redact_text(s))


class BodyRendering(unittest.TestCase):
    def test_html_to_text_drops_script_and_unescapes(self):
        html = ("<html><head><style>x{}</style></head><body>"
                "<script>alert(1)</script><h1>Ti&amp;tle</h1>"
                "<p>hello <b>world</b></p></body></html>")
        text = webfetch.html_to_text(html)
        self.assertIn("Ti&tle", text)
        self.assertIn("hello world", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("<", text)

    def test_json_is_prettified(self):
        record = []
        body, _ = webfetch.fetch(
            "https://api.github.com/x",
            opener=opener_ok(record, b'{"a":1}', "application/json"))
        self.assertIn('"a": 1', body)

    def test_char_truncation_has_explicit_marker(self):
        record = []
        body, _ = webfetch.fetch(
            "https://github.com/x", max_chars=10,
            opener=opener_ok(record, b"A" * 100))
        self.assertIn("正文截断", body)

    def test_byte_cap_has_explicit_marker(self):
        record = []
        body, _ = webfetch.fetch(
            "https://github.com/x", max_bytes=8,
            opener=opener_ok(record, b"B" * 100))
        self.assertIn("下载截断", body)


class WiringAndDefaults(unittest.TestCase):
    def test_permission_default_is_ask_and_not_safe(self):
        """AGENTS.md §1：安全默认值必须被测试钉住。ask 的理由是外渗 ——
        URL query 能携带任意本地内容出网，这是模型唯一的网络出口。"""
        self.assertEqual(
            settings.DEFAULTS["permissions"]["web_fetch"], "ask")
        self.assertNotIn("web_fetch", tools.SAFE)

    def test_schema_and_impl_registered(self):
        names = [t["function"]["name"] for t in tools.SCHEMA]
        self.assertIn("web_fetch", names)
        self.assertIn("web_fetch", tools.IMPL)

    def test_dispatch_surfaces_failure_and_appends_provenance(self):
        with mock.patch.object(
                webfetch, "fetch", return_value=("BODY", "[prov]")):
            out = tools.t_web_fetch("https://github.com/x")
        self.assertEqual(out, "BODY\n[prov]")
        with mock.patch.object(
                webfetch, "fetch",
                side_effect=webfetch.WebFetchError("no route")):
            out = tools.t_web_fetch("https://github.com/x")
        self.assertIn("[web_fetch 失败", out)


if __name__ == "__main__":
    unittest.main()


class PortablePaths(unittest.TestCase):
    """代理配置文件的路径必须跟着 HOME 走。

    内测转交时踩过的形态：研究者的 pod 没有维护者的 `.env-persistent.sh`，
    `resolve_proxy()` 静默返回 None，走代理的 host 全部失败，而错误只说
    「proxy 未配置」—— 不说缺什么、也不说去哪配。
    """

    def test_env_file_is_home_relative(self):
        """HOME 因机器而异，所以只能测机制不能测字面量。"""
        with mock.patch.dict(os.environ, {"HOME": "/tmp/somepod"}, clear=False):
            os.environ.pop("ZYLAB_ENV_FILE", None)
            import importlib
            reloaded = importlib.reload(webfetch)
            try:
                self.assertTrue(reloaded._ENV_FILE.startswith("/tmp/somepod"),
                                f"兜底路径要跟着 HOME 走，实际 {reloaded._ENV_FILE}")
            finally:
                importlib.reload(webfetch)

    def test_env_file_override(self):
        with mock.patch.dict(os.environ, {"ZYLAB_ENV_FILE": "/x/y.sh"}):
            import importlib
            reloaded = importlib.reload(webfetch)
            try:
                self.assertEqual(reloaded._ENV_FILE, "/x/y.sh")
            finally:
                importlib.reload(webfetch)

    def test_legacy_path_still_read(self):
        """老安装的 shell 环境文件由 ZYLAB_LEGACY_ENV_FILE 声明，不写死某台机器。"""
        import importlib
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, ".env-persistent.sh")
            open(legacy, "w").close()
            with mock.patch.dict(
                    os.environ, {"ZYLAB_LEGACY_ENV_FILE": legacy}, clear=False):
                reloaded = importlib.reload(webfetch)
                try:
                    self.assertIn(legacy, reloaded._LEGACY_ENV_FILES,
                                  "声明过的老路径要继续兜底")
                finally:
                    importlib.reload(webfetch)

    def test_missing_proxy_error_is_actionable(self):
        """报错要给出可粘贴的补救，而不只是「未配置」。"""
        hint = webfetch.proxy_setup_hint()
        self.assertIn("ZYLAB_WEB_PROXY", hint)
        self.assertIn("settings.json", hint)
        self.assertIn("直连", hint, "要说明哪些 host 不受影响，避免误判为全网断")

    def test_hint_reaches_the_user(self):
        with mock.patch.object(webfetch, "_ENV_FILE", "/nonexistent/x.sh"), \
             mock.patch.object(webfetch, "_LEGACY_ENV_FILES", ()), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZYLAB_WEB_PROXY", None)

            def dead(url, route, proxy, timeout):
                raise OSError("Network is unreachable")

            with self.assertRaises(webfetch.WebFetchError) as caught:
                webfetch.fetch("https://huggingface.co/x", opener=dead)
            self.assertIn("ZYLAB_WEB_PROXY", str(caught.exception))
