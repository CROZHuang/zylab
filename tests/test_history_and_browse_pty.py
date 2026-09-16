"""用户可见的历史与浏览行为：Up 取回 prompt、PgUp 浏览 transcript。

**为什么存在这个文件。** 维护者两次报告这些功能「改着改着没了」：
Up 取回上一条 prompt、以及向上翻看历史输出。2026-08-27 排查时当前代码
两者实测均在 —— 丢失发生在开发**中间态**（用户恰好在那些时刻使用）。
中间态回退无法靠 review 拦住，只能靠测试当场抓：本文件把这几个行为
钉死在真实 PTY 上，第三次丢会直接红。

断言用容忍 CSI 的匹配器（同 test_session_picker_pty 的教训）：框式渲染
会在字符间插样式码，原始字节流里字面量不连续。
"""
import fcntl
import os
import pty
import re
import struct
import subprocess
import sys
import termios
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CSI_GAP = rb"(?:\x1b\[[0-9;?]*[A-Za-z])*"


def tolerant(text):
    """允许字符之间出现任意条 CSI 样式序列的匹配器。"""
    parts = [re.escape(ch.encode("utf-8")) for ch in text]
    return re.compile(_CSI_GAP.join(parts))


class HistoryBrowsePTYTests(unittest.TestCase):
    # 隔离 harness：临时 HOME（测试绝不写真实 ~/.zylab，见 AGENTS.md §3）、
    # 假 stream_chat（零网络）、预埋一条跨会话历史。
    CHILD = r"""
import os
import sys
import tempfile

repo = os.environ["ZYLAB_TEST_REPO"]
sys.path.insert(0, repo)

with tempfile.TemporaryDirectory(prefix="zylab-histpty-") as root:
    home = os.path.join(root, "home")
    workspace = os.path.join(root, "workspace")
    os.makedirs(home)
    os.makedirs(workspace)
    os.environ["HOME"] = home
    os.chdir(workspace)

    import zylab
    from core import agent as A, client, store

    store.ensure_home()
    # 跨会话历史：冷启动按 Up 应能取回上一个会话的输入。
    store.append_history("HISTORY-SENTINEL-PROMPT", cwd=workspace)

    def fake_stream(model, messages, tools=None, cancel=None, **kwargs):
        yield {"t": "text", "v": "FAKE-REPLY-SENTINEL"}
        yield {"t": "done", "reason": "stop",
               "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                         "total_tokens": 7}}

    client.stream_chat = fake_stream
    client.model_limit = lambda *a, **k: 100_000   # set_model 不出网

    ag = A.Agent(model="pty-model", load_md=False)
    session = zylab.Session(ag)
    ag.confirm = session.confirm
    zylab.repl(session, ag.model)
    print("CHILD-CLEAN-EXIT")
"""

    def setUp(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 40, 140, 0, 0))
        env = os.environ.copy()
        env["ZYLAB_TEST_REPO"] = str(ROOT)
        env.setdefault("TERM", "xterm-256color")
        self.master = master
        self.proc = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(self.CHILD)],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=ROOT, env=env, close_fds=True)
        os.close(slave)
        self.output = bytearray()

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=1.0)
        os.close(self.master)

    def _expect(self, text, *, start=0, timeout=8.0):
        import select
        pattern = tolerant(text)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = pattern.search(bytes(self.output), start)
            if match:
                return match.end()
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    self.output.extend(chunk)
                    continue
            if self.proc.poll() is not None:
                break
        self.fail(
            f"PTY did not render {text!r} after byte {start}; "
            f"exit={self.proc.poll()}\n"
            + self.output.decode("utf-8", "replace"))

    def _send(self, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        os.write(self.master, payload)

    def _exit(self, cursor):
        self._send(b"\x15/exit\r")
        self._expect("CHILD-CLEAN-EXIT", start=cursor)

    def test_cold_start_up_recalls_previous_session_prompt(self):
        """空闲时按 Up 取回上一个会话的输入 —— 用户报告丢过的功能之一。

        提示条现在明确写出「待机时 ↑↓=历史/菜单」；这条文案与 raw-mode
        初始化时序都很容易在重构中回退，所以必须在真实 PTY 中钉死。
        """
        cursor = self._expect("空闲")
        self._send(b"\x1b[A")
        cursor = self._expect("› HISTORY-SENTINEL-PROMPT", start=cursor)
        self._exit(cursor)

    def test_up_recalls_prompt_submitted_this_session(self):
        """本会话刚提交的 prompt 也要能按 Up 取回（add_history 即时生效）。"""
        cursor = self._expect("空闲")
        self._send("MY-TURN-PROMPT\r")
        cursor = self._expect("FAKE-REPLY-SENTINEL", start=cursor)
        cursor = self._expect("空闲", start=cursor)
        self._send(b"\x1b[A")
        cursor = self._expect("› MY-TURN-PROMPT", start=cursor)
        self._exit(cursor)

if __name__ == "__main__":
    unittest.main()
