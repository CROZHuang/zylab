"""Esc 必须即时——走真实 HTTP 链路量两种"卡住"：

* ``headers``：网关排队，响应头迟迟不来（``_OPENER.open`` 阻塞在 getresponse）；
* ``body``：头已到、首字未到（读线程阻塞在 socket recv）。

Linux 上另一线程 ``close()`` 一个 fd 不会唤醒阻塞中的 ``recv``，只有 ``shutdown``
会；而头到之前根本没有 response 可关。所以这里不用假 generator，而是本地起一个
可控延迟的 SSE 服务器，让 zylab 真的发请求，再在 child 里量 Esc → 回到空闲的间隔，
并在空闲后 0.35 s 采样 provider 线程是否还活着（读线程真被解阻塞的证据）。
"""
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.pty_harness import PTYSend, run_pty_child
from tests.test_thinking_pty import HEAD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DELAY = 3.0
BUDGET = 0.5            # Esc → 空闲的上限；实测应远小于此


def _serve(mode, delay):
    """127.0.0.1 随机端口的 SSE 服务器：``mode`` 决定延迟放在头前还是头后。"""
    state = {"hits": 0, "write_failed": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"      # 服务器写完即关，SSE 读到 EOF 结束

        def log_message(self, *args):
            pass

        def do_POST(self):
            state["hits"] += 1
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if mode == "headers":
                time.sleep(delay)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            if mode == "body":
                time.sleep(delay)
            try:
                for chunk in (
                        {"choices": [{"index": 0, "delta": {"content": "LATE-TEXT"}}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {}}):
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except OSError:
                state["write_failed"] = True

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], state


MEASURE_TAIL = r'''
client._ALLOW_INSECURE_HTTP = True     # 本地回环明文 SSE 服务器，仅测试
session = zylab.Session(agent)
agent.confirm = session.confirm
marks = {}
_orig_cancel = session.cancel_active_work
def timed_cancel():
    marks.setdefault("esc", time.monotonic())
    return _orig_cancel()
session.cancel_active_work = timed_cancel
def provider_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith("zylab-provider")]
def idle_after_esc():
    if ("esc" in marks and session.controller.current_turn_id is None
            and not session.pump.snapshot().busy
            and "空闲" in session.pump.snapshot().activity):
        if "idle" not in marks:
            marks["idle"] = time.monotonic()
            def sample():
                time.sleep(0.35)
                marks["threads_after_idle"] = provider_threads()
            threading.Thread(target=sample, daemon=True).start()
        return True
    return False
announce_when(idle_after_esc, "ZYLAB_TEST_TURN_DONE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({
    "esc_to_idle": round(marks["idle"] - marks["esc"], 3),
    "threads_after_idle": marks.get("threads_after_idle"),
    "last_text": session._last_text}))
'''


class InterruptLatencyPTYTests(unittest.TestCase):
    def _measure(self, mode, busy_marker):
        server, port, state = _serve(mode, SERVER_DELAY)
        prefix = (f"import os; os.environ['ZYLAB_BASE'] = 'http://127.0.0.1:{port}/v1'; "
                  "os.environ['DEEPINFER_API_KEY'] = 'local-test-key'\n")
        try:
            out, result = run_pty_child(
                HEAD + MEASURE_TAIL,
                [PTYSend(b"go\r", after="ZYLAB_TEST_PUMP_READY"),
                 # 看到阶段提示后再等 0.2 s，确保 Esc 落在连接已建立、服务器正在睡的窗口里
                 PTYSend(b"\x1b", after=busy_marker, delay=0.2),
                 # 等服务器把迟到的 LATE-TEXT 发完再退出：晚到的增量不许上屏
                 PTYSend(b"/exit\r", after="ZYLAB_TEST_TURN_DONE",
                         delay=SERVER_DELAY + 0.5)],
                cwd=ROOT, timeout=30.0, prefix=prefix)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(state["hits"], 1, "应只发出一次请求")
        return out, result

    def _check(self, out, result):
        self.assertLess(result["esc_to_idle"], BUDGET,
                        f"Esc → 空闲用了 {result['esc_to_idle']} s")
        self.assertEqual(result["threads_after_idle"], [],
                         "provider 读线程在空闲后 0.35 s 仍活着：socket 没被解阻塞")
        self.assertNotIn("LATE-TEXT", out, "取消之后到达的增量不许上屏")
        self.assertIn("⎿  已中断", out, "中断标记要像工具结果一样挂在 ⏺ 下面")

    def test_esc_is_instant_while_gateway_queues_before_headers(self):
        out, result = self._measure("headers", "连接中")
        self._check(out, result)

    def test_esc_is_instant_while_waiting_for_first_token(self):
        out, result = self._measure("body", "等待首字")
        self._check(out, result)


if __name__ == "__main__":
    unittest.main()
