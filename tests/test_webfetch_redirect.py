"""F2：web_fetch 的重定向每一跳都要重新过 host / 协议 / 解析后 IP 的检查。"""
import http.server
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import webfetch


def _serve(respond):
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            respond(self)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], hits


def _redirect(location):
    def r(h):
        h.send_response(302); h.send_header("Location", location); h.end_headers()
    return r


def _ok(h):
    h.send_response(200); h.send_header("Content-Type", "text/plain"); h.end_headers()
    h.wfile.write(b"INTERNAL_SECRET")


def _resolve_to(ip):
    """让 getaddrinfo 对任何名字都返回指定 IP —— 测试策略，不依赖真实 DNS。"""
    return mock.patch.object(
        webfetch.socket, "getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))])


class AddressPolicyTests(unittest.TestCase):
    def test_literal_internal_addresses_are_refused(self):
        for host in ("127.0.0.1", "::1", "169.254.169.254", "0.0.0.0",
                     "10.0.0.1", "192.168.1.1", "172.16.0.1", "fe80::1", "fd00::1"):
            with self.subTest(host=host), self.assertRaises(webfetch.WebFetchError):
                webfetch._check_address_policy(host)

    def test_public_literal_passes(self):
        webfetch._check_address_policy("8.8.8.8")
        webfetch._check_address_policy("2606:4700::1111")

    def test_declared_intranet_name_may_resolve_private(self):
        """内网域名常解析到 10.x —— 只有**声明过**的后缀放行，名单外拒绝。

        名单必须是显式声明的，不能从请求里学：它同时是路由表和「哪些私网目标
        合法」的唯一依据，一旦能被请求撑大，SSRF 边界就没了。
        """
        with mock.patch.object(webfetch, "DIRECT_SUFFIXES", ("corp.example",)), \
                _resolve_to("10.0.0.42"):
            webfetch._check_address_policy("pkg.corp.example")
            with self.assertRaises(webfetch.WebFetchError):
                webfetch._check_address_policy("evil.example")

    def test_declared_intranet_name_still_cannot_reach_loopback(self):
        with mock.patch.object(webfetch, "DIRECT_SUFFIXES", ("corp.example",)), \
                _resolve_to("127.0.0.1"), \
                self.assertRaises(webfetch.WebFetchError):
            webfetch._check_address_policy("pkg.corp.example")

    def test_unresolvable_name_is_not_blocked_here(self):
        """解析不到 = 无法判断；交给后续连接失败或代理策略，不在这里误杀。"""
        with mock.patch.object(webfetch.socket, "getaddrinfo",
                               side_effect=socket.gaierror("nx")):
            webfetch._check_address_policy("does-not-resolve.invalid")


class ValidateTargetTests(unittest.TestCase):
    def test_downgrade_is_refused(self):
        with self.assertRaises(webfetch.WebFetchError) as c:
            webfetch._validate_target("http://example.com/x", previous_scheme="https")
        self.assertIn("降级", str(c.exception))

    def test_blocked_host_applies_per_hop(self):
        """死路名单要逐跳生效 —— 否则一次重定向就能绕过它。"""
        with mock.patch.object(
                webfetch, "BLOCKED_HOSTS",
                {"pypi.org": "用镜像 https://mirror.example/simple"}):
            with self.assertRaises(webfetch.WebFetchError):
                webfetch._validate_target(
                    "https://pypi.org/simple", previous_scheme="https")

    def test_initial_url_is_checked_before_any_request(self):
        calls = []
        def opener(url, route, proxy, timeout):
            calls.append(url); raise AssertionError("不该发请求")
        with self.assertRaises(webfetch.WebFetchError):
            webfetch.fetch("http://127.0.0.1:9/x", opener=opener)
        self.assertEqual(calls, [])


class LiveHopGuardTests(unittest.TestCase):
    """真起两个本地服务器：A 302 到 B。B 收没收到请求是唯一判据。"""

    def setUp(self):
        self.b, self.b_port, self.b_hits = _serve(_ok)
        self.a, self.a_port, self.a_hits = _serve(
            _redirect(f"http://127.0.0.1:{self.b_port}/secret"))

    def tearDown(self):
        self.a.shutdown(); self.b.shutdown()

    def test_control_plain_opener_follows_into_loopback(self):
        """对照组：没有 _HopGuard 的 opener 会一路跟到 B 并拿到内部内容。"""
        # ProxyHandler({}) 与 _open_via 的 direct 路由一致；裸 build_opener() 会
        # 吃到环境里的 HTTP_PROXY，把 127.0.0.1 也送去代理。
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{self.a_port}/start", timeout=5) as r:
            self.assertEqual(r.read(), b"INTERNAL_SECRET")
        self.assertEqual(self.b_hits, ["/secret"])

    def test_open_via_refuses_hop_into_loopback(self):
        with self.assertRaises(webfetch.WebFetchError) as c:
            webfetch._open_via(f"http://127.0.0.1:{self.a_port}/start", "direct", None, 5)
        self.assertIn("127.0.0.1", str(c.exception))
        self.assertEqual(self.b_hits, [], "B 不该收到任何请求")
        self.assertEqual(self.a_hits, ["/start"])

    def test_refusal_is_not_retried_on_the_other_route(self):
        """fetch() 的 except Exception 不能把策略拒绝当路由故障重试。"""
        attempts = []
        def opener(url, route, proxy, timeout):
            attempts.append(route)
            raise webfetch.WebFetchError("拒绝访问 127.0.0.1")
        with mock.patch.object(webfetch, "_check_address_policy"):   # 放行初始 URL
            with self.assertRaises(webfetch.WebFetchError):
                webfetch.fetch("https://example.com/x", opener=opener,
                               cfg={"web": {"proxy": "http://p:1"}})
        self.assertEqual(attempts, [attempts[0]], "只该尝试一次")

    def test_redirect_loop_is_capped(self):
        srv, port, hits = _serve(lambda h: _redirect("/loop")(h))
        try:
            with mock.patch.object(webfetch, "_check_address_policy"):
                with self.assertRaises(urllib.error.HTTPError):
                    webfetch._open_via(f"http://127.0.0.1:{port}/loop", "direct", None, 5)
            self.assertLessEqual(len(hits), webfetch.MAX_REDIRECTS + 2)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
