"""F1：provider 端点返回 30x 时，Authorization 不能跟着跨源过去。

urllib 默认跟随任何重定向且在跨 host 时保留 Authorization（requests 会剥掉，
urllib 不会）。provider 或中间层返回一个指向第三方的 30x，bearer token 就泄了。
"""
import http.server
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import client


def _serve(make_handler):
    """在 127.0.0.1 随机端口起一个 HTTPServer；返回 (server, port, hits)。"""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append({"path": self.path,
                         "auth": self.headers.get("Authorization")})
            make_handler(self)

        do_POST = do_GET

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], hits


def _redirect_to(location, code=302):
    def respond(h):
        h.send_response(code)
        h.send_header("Location", location)
        h.end_headers()
    return respond


def _ok(h):
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.end_headers()
    h.wfile.write(b'{"ok":true}')


class OriginTests(unittest.TestCase):
    def test_default_ports_are_normalized(self):
        self.assertEqual(client._origin("https://h/x"), client._origin("https://h:443/y"))
        self.assertEqual(client._origin("http://h/x"), client._origin("http://h:80/y"))

    def test_scheme_downgrade_is_a_different_origin(self):
        self.assertNotEqual(client._origin("https://h/x"), client._origin("http://h/x"))

    def test_display_url_drops_query_and_userinfo(self):
        self.assertEqual(
            client._display_url("https://u:p@h:8443/a/b?token=SECRET#f"),
            "https://h:8443/a/b")


class HandlerUnitTests(unittest.TestCase):
    """直接调 redirect_request，不起服务器。"""

    def _req(self, url):
        return urllib.request.Request(url, headers={"Authorization": "Bearer s"})

    def _refused(self, from_url, location):
        h = client._SameOriginRedirects()
        with self.assertRaises(client.APIError) as ctx:
            h.redirect_request(self._req(from_url), None, 302, "Found", {}, location)
        err = ctx.exception
        self.assertEqual(err.kind, "redirect")
        self.assertIs(err.retryable, False)
        self.assertEqual(err.status, 302)
        return str(err)

    def test_cross_host_is_refused_and_secret_not_in_message(self):
        msg = self._refused("https://gw.example/v1/chat",
                            "https://evil.example/collect?k=SECRET")
        self.assertIn("evil.example", msg)
        self.assertNotIn("SECRET", msg)

    def test_https_to_http_same_host_is_refused(self):
        self._refused("https://gw.example/v1/chat", "http://gw.example/v1/chat")

    def test_scheme_relative_location_is_refused(self):
        self._refused("https://gw.example/v1/chat", "//evil.example/x")

    def test_different_port_is_refused(self):
        self._refused("https://gw.example/v1/chat", "https://gw.example:8443/v1/chat")

    def test_same_origin_relative_location_is_followed(self):
        h = client._SameOriginRedirects()
        out = h.redirect_request(
            self._req("https://gw.example/v1/chat"), None, 302, "Found", {}, "/v1/other")
        self.assertIsInstance(out, urllib.request.Request)
        self.assertEqual(out.full_url, "https://gw.example/v1/other")

    def test_same_origin_with_explicit_default_port_is_followed(self):
        h = client._SameOriginRedirects()
        out = h.redirect_request(
            self._req("https://gw.example/v1/chat"), None, 307, "Temp", {},
            "https://gw.example:443/v1/chat")
        self.assertIsInstance(out, urllib.request.Request)


class LiveOpenerTests(unittest.TestCase):
    """真起两个本地服务器：A 把请求 30x 到 B。B 收没收到 Authorization 是唯一判据。"""

    def setUp(self):
        self.b, self.b_port, self.b_hits = _serve(_ok)
        self.a, self.a_port, self.a_hits = _serve(
            _redirect_to(f"http://127.0.0.1:{{B}}/secret"))

    def tearDown(self):
        self.a.shutdown(); self.b.shutdown()

    def _a_redirecting_to_b(self):
        self.a.shutdown()
        self.a, self.a_port, self.a_hits = _serve(
            _redirect_to(f"http://127.0.0.1:{self.b_port}/secret"))

    def test_control_the_old_opener_really_leaks(self):
        """对照组：没有 _SameOriginRedirects 的 opener（修复前的形状）会把
        Authorization 带到 B。这条证明测试能区分修复前后，不是空转。"""
        self._a_redirecting_to_b()
        old = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.a_port}/start",
            headers={"Authorization": "Bearer leaked-secret"})
        with old.open(req, timeout=5) as r:
            r.read()
        self.assertEqual(len(self.b_hits), 1)
        self.assertEqual(self.b_hits[0]["auth"], "Bearer leaked-secret")

    def test_fixed_opener_refuses_and_b_sees_nothing(self):
        self._a_redirecting_to_b()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.a_port}/start",
            headers={"Authorization": "Bearer must-not-leak"})
        with self.assertRaises(client.APIError) as ctx:
            client._OPENER.open(req, timeout=5)
        self.assertEqual(ctx.exception.kind, "redirect")
        self.assertEqual(self.b_hits, [], "B 不该收到任何请求")
        self.assertEqual(len(self.a_hits), 1)

    def test_same_origin_redirect_is_followed_with_auth(self):
        """同源（同 host 同 port）重定向照常跟随，Authorization 保留 —— 这是
        给负载均衡留的余地，不是漏洞。"""
        srv, port, hits = _serve(lambda h: (
            _redirect_to(f"http://127.0.0.1:{port_box[0]}/final")(h)
            if h.path == "/start" else _ok(h)))
        port_box = [port]
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/start",
                headers={"Authorization": "Bearer same-origin"})
            with client._OPENER.open(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual([h["path"] for h in hits], ["/start", "/final"])
            self.assertEqual(hits[1]["auth"], "Bearer same-origin")
        finally:
            srv.shutdown()

    def test_redirect_loop_is_capped(self):
        srv, port, hits = _serve(lambda h: _redirect_to("/loop")(h))
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/loop")
            with self.assertRaises((urllib.error.HTTPError, client.APIError)):
                client._OPENER.open(req, timeout=5)
            self.assertLessEqual(len(hits), client._SameOriginRedirects.max_redirections + 2)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
