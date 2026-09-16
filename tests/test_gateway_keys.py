"""§2.6 key 体验：/gateway 列表标注 key 状态；切到无 key 网关当场给补救文案。"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zylab  # noqa: E402
from core import client  # noqa: E402


def _status(found):
    def fake(route=None):
        r = route or client.route_for()
        return {"found": found(r.name), "source": "环境变量 X" if found(r.name) else None,
                "route": r.name, "names": list(r.key_names), "candidates": ["/tmp/keys.env"]}
    return fake


class GatewayKeyTests(unittest.TestCase):
    def test_listing_marks_gateways_without_a_key(self):
        sess = mock.Mock()
        sess.route_selection.return_value = ("m", client.GATEWAY)
        buf = io.StringIO()
        with mock.patch.object(client, "key_status", side_effect=_status(lambda n: n == "deepinfer")), \
                mock.patch.object(zylab.tui, "supported", return_value=False), \
                contextlib.redirect_stdout(buf):
            zylab.cmd_gateway(sess, "")
        out = buf.getvalue()
        self.assertIn("key 已配置", out)
        self.assertIn("未配置 key", out)

    def test_switching_to_a_keyless_gateway_prints_the_remedy_immediately(self):
        sess = mock.Mock()
        sess.route_selection.return_value = ("m", "deepinfer")
        sess.request_route_change.return_value = {"staged": False, "gateway": "boyue", "base": "http://x"}
        buf = io.StringIO()
        with mock.patch.object(client, "key_status", side_effect=_status(lambda n: n == "deepinfer")), \
                contextlib.redirect_stdout(buf):
            zylab.cmd_gateway(sess, "boyue")
        out = buf.getvalue()
        self.assertIn("切到 boyue", out)
        self.assertIn("还没有 API key", out)
        self.assertIn("export BOYUE_API_KEY=", out)


if __name__ == "__main__":
    unittest.main()
