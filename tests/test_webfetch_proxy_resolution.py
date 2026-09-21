"""代理解析与墙钟上限：09-20 一次「上网搜索不丝滑」背后的两个缺陷。

1. 环境文件里 `_PROXY_CLEAR='unset http_proxy …'` 排在最前，旧实现取**第一个**
   `_PROXY_*` 匹配，把一条 shell 命令当代理交给 ProxyHandler → `InvalidURL`，
   代理这条腿当场失效、退到直连再超时。原有 36 个用例没有一条断言
   「选中了哪一条」或「值必须是 URL」，所以它一直活着。
2. urllib 的 timeout 只管单次 socket 操作：`timeout=30` 的调用实测跑了 270 秒。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import webfetch  # noqa: E402

REAL = "http://user:secret@proxy.example:3128/"
AMBIENT = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
           "all_proxy", "ALL_PROXY")


class ResolveProxyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_file = os.path.join(tmp.name, "env.sh")
        for name, value in (("_ENV_FILE", self.env_file),
                            ("_LEGACY_ENV_FILES", ())):
            patcher = mock.patch.object(webfetch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # 测试不得依赖跑测试那台机器的环境：清掉一切可能的代理声明。
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in AMBIENT + ("ZYLAB_WEB_PROXY",):
            os.environ.pop(name, None)

    def _write_env(self, text):
        with open(self.env_file, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_shell_command_line_is_skipped_for_the_first_real_url(self):
        """事故原形：清代理的 shell 命令排在真正的代理地址前面。"""
        self._write_env(
            "_PROXY_CLEAR='unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY'\n"
            f"_PROXY_MAIN='{REAL}'\n"
            "_PROXY_OTHER='http://other.example:8080/'\n")
        self.assertEqual(webfetch.resolve_proxy({}), REAL)

    def test_a_file_with_no_real_url_resolves_to_none(self):
        self._write_env("_PROXY_CLEAR='unset http_proxy https_proxy'\n")
        self.assertIsNone(webfetch.resolve_proxy({}))

    def test_garbage_falls_through_every_level(self):
        """settings 与 ZYLAB_WEB_PROXY 里的非 URL 值也不能交给 ProxyHandler。"""
        self._write_env(f"_PROXY_MAIN='{REAL}'\n")
        os.environ["ZYLAB_WEB_PROXY"] = "unset everything"
        cfg = {"web": {"proxy": "not a url"}}
        self.assertEqual(webfetch.resolve_proxy(cfg), REAL)

    def test_declared_levels_still_win_in_order(self):
        self._write_env(f"_PROXY_MAIN='{REAL}'\n")
        os.environ["ZYLAB_WEB_PROXY"] = "http://from-env.example:1/"
        self.assertEqual(webfetch.resolve_proxy({}), "http://from-env.example:1/")
        cfg = {"web": {"proxy": "http://from-settings.example:2/"}}
        self.assertEqual(webfetch.resolve_proxy(cfg),
                         "http://from-settings.example:2/")

    def test_ambient_standard_proxy_variables_are_ignored(self):
        """环境里「有代理」不等于「这条代理通得了目标」。

        实测：本机 http(s)_proxy 指向只通 AI 厂商域名的那条代理，拿它抓通用
        网页一律 `Tunnel connection failed: 403`。网页抓取用哪条必须显式声明。
        """
        for name in AMBIENT:
            os.environ[name] = "http://ambient.example:9/"
        self.assertIsNone(webfetch.resolve_proxy({}))

    def test_url_shape_check(self):
        for value, expected in (
                (REAL, True), ("https://p.example/", True),
                ("socks5://p.example:1080", True), ("socks5h://p.example", True),
                ("unset http_proxy https_proxy", False), ("p.example:3128", False),
                ("ftp://p.example/", False), ("", False), (None, False)):
            self.assertEqual(webfetch.looks_like_proxy_url(value), expected, value)


class WallClockDeadlineTests(unittest.TestCase):
    def test_a_hung_route_is_abandoned_at_the_deadline(self):
        class Hung:
            def __enter__(self):
                time.sleep(4)                 # 假装 socket 级超时管不住
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(webfetch, "resolve_proxy", return_value=None):
            started = time.monotonic()
            with self.assertRaises(webfetch.WebFetchError) as caught:
                webfetch.fetch("https://example.org/x", timeout=1,
                               opener=lambda *args: Hung())
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5, "timeout 必须是这条路由的墙钟上限")
        self.assertIn("墙钟上限", str(caught.exception))

    def test_results_and_errors_cross_the_thread_boundary_unchanged(self):
        self.assertEqual(webfetch._within_deadline(lambda: 42, 1), 42)
        with self.assertRaises(KeyError):
            webfetch._within_deadline(lambda: {}["missing"], 1)


if __name__ == "__main__":
    unittest.main()
