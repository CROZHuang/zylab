"""Real-PTY regression coverage for the M4a session picker workflow."""

import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class SessionPickerPTYTests(unittest.TestCase):
    CHILD = r"""
import json
import os
import tempfile
import time

repo = os.environ["ZYLAB_TEST_REPO"]
import sys
sys.path.insert(0, repo)

with tempfile.TemporaryDirectory(prefix="zylab-picker-") as root:
    home = os.path.join(root, "home")
    workspace = os.path.join(root, "workspace")
    os.makedirs(home)
    os.makedirs(workspace)
    os.environ["HOME"] = home
    os.chdir(workspace)

    import zylab
    from core import client, store, tui

    store.ensure_home()

    class FakeAgent:
        def __init__(self, session_id, messages):
            self.session_id = session_id
            self.model = "pty-model"
            self.gateway = client.GATEWAY
            self.messages = messages
            self.tokens_in = 0
            self.tokens_out = 0
            self.last_total = 0
            self.turns = 0
            self.cache_read = 0
            self.cache_write = 0
            self.cache_reported = False
            self.compact_failed = None
            self.ctx_limit = 100_000
            self.compact_at = 70_000
            self.ctx_known = True
            self.ctx_limit_source = "test"
            self.context_summary = None
            self.context_invalid_reason = None
            self._compact_failed_key = None
            self._last_age_notice_key = None
            self._seen_ok_hi = 0
            self.started = time.time()

        def context_snapshot(self):
            return {"summary": self.context_summary}

        def load_context(self, context):
            self.context_summary = (context or {}).get("summary")

        def set_model(self, model):
            self.model = model

    target_id = "target-raf"
    target = FakeAgent(target_id, [
        {"role": "system", "content": "fixture system"},
        {"role": "user", "content": "TARGET-QUESTION"},
        {"role": "assistant", "content": "TARGET-REPLAY-SENTINEL"},
    ])
    store.save_session(target, title="draft raf target", cwd=workspace)

    preview_calls = []
    original_preview = store.load_session_preview

    def tracked_preview(session_id):
        preview_calls.append(session_id)
        return original_preview(session_id)

    store.load_session_preview = tracked_preview

    class TrackingPump(tui.InputPump):
        picker_opens = []
        prompt_main_drafts = []
        injected = False

        def open_picker(self, rows, **kwargs):
            title = str(kwargs.get("title") or "")
            if title.startswith("Resume session"):
                if not type(self).injected:
                    # A modal must not share this backing editor.  Injecting a
                    # non-empty draft here makes contamination observable even
                    # though submitting /resume normally leaves an empty line.
                    self.editor.replace("MAIN-DRAFT-SENTINEL", active=True)
                    type(self).injected = True
                state = dict(kwargs.get("initial_state") or {})
                type(self).picker_opens.append({
                    "title": title,
                    "filter": state.get("filter") or "",
                    "selected_key": state.get("selected_key"),
                    "main_draft": self.editor.text,
                    "preview_count": len(preview_calls),
                })
            return super().open_picker(rows, **kwargs)

        def _handle_prompt(self, key):
            before = self.editor.text
            events = super()._handle_prompt(key)
            if key in ("enter", "esc", "ctrl-c"):
                type(self).prompt_main_drafts.append(
                    [before, self.editor.text])
            return events

    tui.InputPump = TrackingPump

    def no_network(*_args, **_kwargs):
        raise AssertionError("session picker PTY test attempted network I/O")

    client.stream_chat = no_network
    current = FakeAgent(
        "current-pty",
        [{"role": "system", "content": "current system"}],
    )
    session = zylab.Session(current)
    current.confirm = session.confirm
    zylab.repl(session, current.model)

    record = store.load_session(target_id)
    active_record = store.load_session(session.ag.session_id)
    all_summaries = store.list_session_summaries(
        limit=None, status=None, scope="all")
    summary = next(
        row for row in all_summaries
        if row["id"] == target_id
    )
    result = {
        "session_id": session.ag.session_id,
        "active_parent": active_record.get("parent_session_id"),
        "active_grants": active_record.get("session_grants"),
        "active_messages": active_record.get("messages"),
        "target_parent": record.get("parent_session_id"),
        "target_grants": record.get("session_grants"),
        "branch_count": sum(
            row.get("parent_session_id") == target_id
            for row in all_summaries),
        "title": record["title"],
        "status": record["status"],
        "summary_status": summary["status"],
        "preview_calls": preview_calls,
        "prompt_main_drafts": TrackingPump.prompt_main_drafts,
        "picker_opens": TrackingPump.picker_opens,
        "final_main_draft": session.pump.editor.text,
    }
print("RESULT:" + json.dumps(result, ensure_ascii=False))
"""

    def setUp(self):
        master, slave = pty.openpty()
        # A stable, reasonably wide viewport avoids testing clipping here; the
        # picker renderer has separate narrow-terminal unit coverage.
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 40, 140, 0, 0),
        )
        env = os.environ.copy()
        env["ZYLAB_TEST_REPO"] = str(ROOT)
        # This fixture asserts an explicit deepinfer footer and must not inherit
        # the gateway/key-file choices of a parent zylab smoke session.
        env["ZYLAB_GATEWAY"] = "deepinfer"
        env["DEEPINFER_API_KEY"] = "fictional-test-key"
        env["BOYUE_API_KEY"] = "fictional-test-key"
        env.pop("ZYLAB_BASE", None)
        env.pop("ZYLAB_KEYS_FILE", None)
        env.setdefault("TERM", "xterm-256color")
        self.master = master
        self.proc = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(self.CHILD)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=ROOT,
            env=env,
            close_fds=True,
        )
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

    def _decoded(self):
        return self.output.decode("utf-8", "replace")

    def _read_available(self):
        while True:
            ready, _, _ = select.select([self.master], [], [], 0)
            if not ready:
                return
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.output.extend(chunk)

    # 字符之间允许出现任意条 CSI 样式序列。断言的语义是「用户在屏幕上看到
    # 这段文字」，而框式渲染会在字符中间插颜色码 —— 实测回放帧的原始字节是
    # `\x1b[38;5;240m  │\x1b[0m › TARGET-QUESTION`，字面量 `│ › TARGET-QUESTION`
    # 在流里根本不连续，导致本文件的断言随任何调色改动而碎。
    # 游标仍在**原始字节空间**（match.end()），单调且精确，不受剥码影响。
    _CSI_GAP = rb"(?:\x1b\[[0-9;?]*[A-Za-z])*"

    def _tolerant(self, text):
        parts = [re.escape(ch.encode("utf-8")) for ch in text]
        return re.compile(self._CSI_GAP.join(parts))

    def _expect(self, text, *, start=0, timeout=5.0):
        pattern = self._tolerant(text)
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
                self._read_available()
                break
        self.fail(
            f"PTY did not render {text!r} after byte {start}; "
            f"exit={self.proc.poll()}\n{self._decoded()}"
        )

    def _send(self, payload):
        os.write(
            self.master,
            payload if isinstance(payload, bytes) else payload.encode("utf-8"),
        )

    def test_resume_picker_full_workflow_preserves_state_and_main_draft(self):
        cursor = self._expect("/help 看命令")
        # The banner is printed before InputPump enters cbreak mode.  Wait for
        # its first complete composer frame so bytes cannot land in the old
        # canonical terminal buffer.
        cursor = self._expect("空闲", start=cursor)

        self._send("/resume\r")
        cursor = self._expect(
            "Resume session · current repo · active", start=cursor)

        # Verify the real Ctrl+A byte is routed to picker scope, not Home.
        self._send(b"\x01")
        cursor = self._expect(
            "Resume session · all projects · active", start=cursor)
        self._send(b"\x01")
        cursor = self._expect(
            "Resume session · current repo · active", start=cursor)

        # Lowercase action letters remain searchable after explicit '/'.
        self._send("/raf")
        cursor = self._expect("筛选: raf", start=cursor)
        self._send(b"\x1b")
        cursor = self._expect("筛选: raf · / 编辑", start=cursor)

        self._send(b" ")
        cursor = self._expect(
            "[preview target-raf · draft raf target]", start=cursor)
        cursor = self._expect("TARGET-REPLAY-SENTINEL", start=cursor)
        cursor = self._expect("筛选: raf · / 编辑", start=cursor)

        self._send(b"R")
        cursor = self._expect("Rename › draft raf target", start=cursor)
        self._send(b"\x15" + "renamed raf session".encode("utf-8") + b"\r")
        cursor = self._expect("[已命名：renamed raf session]", start=cursor)
        cursor = self._expect("筛选: raf · / 编辑", start=cursor)

        self._send(b"A")
        cursor = self._expect("[已归档 target-raf]", start=cursor)
        cursor = self._expect(
            "Resume session · current repo · active", start=cursor)
        self._send(b"\t")
        cursor = self._expect(
            "Resume session · current repo · archived", start=cursor)
        self._send(b"U")
        cursor = self._expect("[已取消归档 target-raf]", start=cursor)
        cursor = self._expect(
            "Resume session · current repo · archived", start=cursor)
        self._send(b"\t")
        cursor = self._expect(
            "Resume session · current repo · active", start=cursor)

        self._send(b"\r")
        cursor = self._expect("› TARGET-QUESTION", start=cursor)
        cursor = self._expect("TARGET-REPLAY-SENTINEL", start=cursor)
        cursor = self._expect(
            "renamed raf session · pty-model@deepinfer", start=cursor)

        # The injected draft should still be present after the modal.  Clear it
        # explicitly before sending /exit so the test also detects leakage.
        self._send(b"\x15/exit\r")
        self._expect("RESULT:", start=cursor)
        self.proc.wait(timeout=2.0)
        self._read_available()

        decoded = self._decoded()
        self.assertNotIn("[接上 target-raf", decoded)
        payload = decoded.rsplit("RESULT:", 1)[1].strip().splitlines()[0]
        result = json.loads(payload)

        self.assertEqual(result["session_id"], "target-raf")
        self.assertEqual(result["title"], "renamed raf session")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["summary_status"], "active")
        self.assertEqual(result["preview_calls"], ["target-raf"])
        self.assertEqual(
            result["prompt_main_drafts"],
            [["MAIN-DRAFT-SENTINEL", "MAIN-DRAFT-SENTINEL"]],
        )
        self.assertNotIn(
            "renamed raf session",
            result["prompt_main_drafts"][0][1],
        )

        restored = [
            opened for opened in result["picker_opens"]
            if opened["filter"] == "raf"
            and opened["selected_key"] == "target-raf"
        ]
        self.assertGreaterEqual(len(restored), 3, result["picker_opens"])
        after_preview = next(
            opened for opened in result["picker_opens"]
            if opened["preview_count"] == 1
        )
        self.assertEqual(after_preview["filter"], "raf")
        self.assertEqual(after_preview["selected_key"], "target-raf")
        self.assertTrue(all(
            opened["main_draft"] == "MAIN-DRAFT-SENTINEL"
            for opened in result["picker_opens"]
        ))

    def test_picker_f_creates_and_enters_isolated_branch(self):
        cursor = self._expect("/help 看命令")
        cursor = self._expect("空闲", start=cursor)

        self._send("/resume\r")
        cursor = self._expect(
            "Resume session · current repo · active", start=cursor)
        self._send("/raf")
        cursor = self._expect("筛选: raf", start=cursor)
        self._send(b"\x1b")
        cursor = self._expect("筛选: raf · / 编辑", start=cursor)

        self._send(b"F")
        cursor = self._expect("TARGET-REPLAY-SENTINEL", start=cursor)
        cursor = self._expect("session grant 未继承", start=cursor)
        self._send(b"\x15/exit\r")
        self._expect("RESULT:", start=cursor)
        self.proc.wait(timeout=2.0)
        self._read_available()

        payload = self._decoded().rsplit(
            "RESULT:", 1)[1].strip().splitlines()[0]
        result = json.loads(payload)

        self.assertNotEqual(result["session_id"], "target-raf")
        self.assertEqual(result["active_parent"], "target-raf")
        self.assertEqual(result["active_grants"], [])
        self.assertEqual(result["target_parent"], None)
        self.assertEqual(result["target_grants"], [])
        self.assertEqual(result["branch_count"], 1)
        self.assertEqual(
            result["active_messages"][1]["content"],
            "TARGET-QUESTION",
        )
        self.assertEqual(
            result["active_messages"][2]["content"],
            "TARGET-REPLAY-SENTINEL",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
