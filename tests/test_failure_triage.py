"""D3（SPEC-CC-parity）：没 key / 额度 403 / 路由 403 / 网关 5xx 必须读起来不一样，
且各自指向不同的动作。"""
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import client


def http_error(code, body, msg="err"):
    raw = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, dict) else body.encode()
    return urllib.error.HTTPError("http://gw/v1/chat/completions", code, msg, {}, io.BytesIO(raw))


ROUTE = client.route_for("boyue")


class FailureTriageTests(unittest.TestCase):
    def test_quota_403_is_its_own_kind_with_topup_action(self):
        err = client._http_error(http_error(403, {"error": {"message":
            "用户额度不足, 剩余额度: ＄-0.057738 (request id: abc)", "type": "n"}}), ROUTE, "m")
        self.assertEqual(err.kind, "quota")
        self.assertFalse(err.retryable)
        self.assertIn("额度不足", str(err)); self.assertIn("充值", str(err))
        self.assertNotIn("模型权限", str(err))

    def test_route_or_permission_403_keeps_the_route_hint(self):
        err = client._http_error(http_error(403, {"error": {"message": "forbidden"}}), ROUTE, "m")
        self.assertEqual(err.kind, "permission")
        self.assertIn("不要把所有 403 都当成 key 错误", str(err))
        self.assertNotIn("充值", str(err))

    def test_gateway_5xx_says_not_your_key(self):
        err = client._http_error(http_error(503, {"error": {"code": "service_error",
            "message": "Service is temporarily unable to complete the request."}}), ROUTE, "m")
        self.assertEqual(err.kind, "transient_http"); self.assertTrue(err.retryable)
        self.assertIn("与 key 无关", str(err)); self.assertIn("/gateway", str(err))

    def test_401_points_at_doctor(self):
        err = client._http_error(http_error(401, {"error": {"message": "invalid api key"}}), ROUTE, "m")
        self.assertEqual(err.kind, "authentication"); self.assertIn("/doctor", str(err))

    def test_the_three_failures_read_differently(self):
        texts = {
            "quota": str(client._http_error(http_error(403, {"error": {"message": "quota exceeded"}}), ROUTE, "m")),
            "route": str(client._http_error(http_error(403, {"error": {"message": "forbidden"}}), ROUTE, "m")),
            "down": str(client._http_error(http_error(503, "unavailable"), ROUTE, "m")),
        }
        hints = {k: v.split("\n", 1)[1] if "\n" in v else "" for k, v in texts.items()}
        self.assertEqual(len(set(hints.values())), 3, hints)
        for h in hints.values():
            self.assertTrue(h.strip(), "每种失败都要有动作提示")


if __name__ == "__main__":
    unittest.main()
