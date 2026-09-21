"""人回来了，在途的 recap 请求必须**立刻**放手——哪怕它正卡在等网关的首字节上。

慢网关上这个等待是一两分钟（2026-09-20 实测 glm-5.3@deepinfer 首字 127.9s）。光置位一个
Event 叫不醒阻塞在 recv 里的读线程；得 shutdown socket。这里走真实 HTTP 链路量它：
本地 SSE 服务器 3 秒后才回响应头，0.3 秒时中止，看 generate_away_recap 多久返回。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_interrupt_latency_pty import _serve   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DELAY = 3.0
BUDGET = 0.5

CHILD = r'''
import json, threading, time
from core import agent as A, client
client._ALLOW_INSECURE_HTTP = True     # 本地回环明文 SSE 服务器，仅测试
agent = A.Agent.__new__(A.Agent)
agent.model, agent.gateway, agent.session_id = "m", "deepinfer", "abort-latency"
agent.messages = [{"role": "system", "content": "s"}]
for index in range(3):
    agent.messages += [{"role": "user", "content": f"问题 {index}"},
                       {"role": "assistant", "content": f"回答 {index}"}]
agent.context_summary = None
agent.away_recap = None
agent.compaction_fallback_route = lambda: None
cancel = client.CancellationHandle()
box = {}
def work():
    box["result"] = agent.generate_away_recap(cancel=cancel)
    box["returned"] = time.monotonic()
worker = threading.Thread(target=work, daemon=True)
worker.start()
time.sleep(0.3)
aborted_at = time.monotonic()
cancel.cancel()
worker.join(10)
print("RESULT:" + json.dumps({
    "kind": (box.get("result") or {}).get("kind"),
    "abort_to_return": round(box.get("returned", 1e9) - aborted_at, 3)}))
'''


class AbortLatencyTests(unittest.TestCase):
    def measure(self, mode):
        server, port, state = _serve(mode, SERVER_DELAY)
        home = tempfile.TemporaryDirectory(prefix="zylab-recap-abort-")
        env = dict(os.environ, HOME=home.name,
                   ZYLAB_HOME=os.path.join(home.name, ".zylab"),
                   ZYLAB_BASE=f"http://127.0.0.1:{port}/v1",
                   DEEPINFER_API_KEY="local-test-key")
        env.pop("ZYLAB_APP_ROOT", None)
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                     "all_proxy", "ALL_PROXY"):
            env.pop(name, None)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", CHILD], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=30)
        finally:
            server.shutdown()
            server.server_close()
            home.cleanup()
        self.assertIn("RESULT:", proc.stdout, proc.stderr[-800:])
        result = json.loads(proc.stdout.rsplit("RESULT:", 1)[1].strip().splitlines()[0])
        self.assertEqual(state["hits"], 1, "中止之后不许再重试")
        return result

    def check(self, result):
        self.assertEqual(result["kind"], "aborted")
        self.assertLess(result["abort_to_return"], BUDGET,
                        f"中止 → 返回用了 {result['abort_to_return']}s（服务器要 {SERVER_DELAY}s 才回话）")

    def test_abort_while_the_gateway_has_not_even_sent_headers(self):
        self.check(self.measure("headers"))

    def test_abort_while_waiting_for_the_first_token(self):
        self.check(self.measure("body"))


if __name__ == "__main__":
    unittest.main()
