"""在 chat 里按 ↑，先翻到的必须是**这个 chat** 自己的输入。

09-20 用户报告：在某个 chat 里向上翻历史，遍历到的是整个 zylab 的输入，自己的和
别的 chat 的交错在一起。根因不在读取端——history.jsonl 的条目只有
{ts, cwd, input}，数据里压根没记「这条属于哪个 chat」。

修法是**分层而不是隔绝**：本 chat 的输入排在最近处（↑ 先走完它们），更早的位置
才是别的 chat 的输入。「冷启动的新 chat 按 ↑ / Ctrl+R 能找回上一个会话的提问」
是既有的 PTY 契约（test_history_and_browse_pty、parity A6），严格隔绝会废掉它。
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import store  # noqa: E402
import zylab  # noqa: E402


class _IsolatedHistory(unittest.TestCase):
    """把 store 的 HOME / HISTORY 指到临时目录：绝不碰真实状态目录。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        for name, value in (("HOME", home), ("HISTORY", home / "history.jsonl")):
            patcher = mock.patch.object(store, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _lines(self):
        return [json.loads(line) for line in
                store.HISTORY.read_text(encoding="utf-8").splitlines()]


class StoreScopeTests(_IsolatedHistory):
    def test_records_carry_their_chat(self):
        store.append_history("hello", session_id="chat-a")
        self.assertEqual(self._lines()[0]["session"], "chat-a")

    def test_untagged_call_keeps_the_legacy_shape(self):
        store.append_history("hello")
        self.assertEqual(set(self._lines()[0]), {"ts", "cwd", "input"})

    def test_read_is_scoped_to_one_chat(self):
        for text, chat in (("a1", "chat-a"), ("b1", "chat-b"), ("old", None),
                           ("/model", "chat-a"), ("b2", "chat-b")):
            store.append_history(text, session_id=chat)
        read = lambda chat: [r["input"] for r in
                             store.read_history(session_id=chat)]  # noqa: E731
        self.assertEqual(read("chat-a"), ["a1", "/model"])
        self.assertEqual(read("chat-b"), ["b1", "b2"])
        self.assertEqual(read("chat-never-seen"), [])

    def test_quiet_chat_is_not_starved_by_a_busy_one(self):
        """过滤必须先扫全文件再取末尾：只看最后 limit 行，安静的 chat 会读成空。"""
        store.append_history("早先的提问", session_id="quiet")
        for index in range(30):
            store.append_history(f"busy {index}", session_id="busy")
        records = store.read_history(limit=5, session_id="quiet")
        self.assertEqual([r["input"] for r in records], ["早先的提问"])

    def test_unscoped_read_keeps_the_old_global_window(self):
        for index in range(8):
            store.append_history(f"line {index}", session_id=f"chat-{index % 2}")
        records = store.read_history(limit=3)
        self.assertEqual([r["input"] for r in records],
                         ["line 5", "line 6", "line 7"])


class ChatHistoryTests(_IsolatedHistory):
    """列表末尾离 ↑ 最近：本 chat 的输入必须连续地排在末尾。"""

    @staticmethod
    def _user(*texts):
        return [{"role": "user", "content": text} for text in texts]

    def test_own_inputs_are_nearest_and_other_chats_come_before_them(self):
        """事故原形：自己的和别的 chat 的输入按时间交错在一起。"""
        for text, chat in (("a1", "chat-a"), ("b1", "chat-b"), ("a2", "chat-a"),
                           ("b2", "chat-b"), ("/model", "chat-a")):
            store.append_history(text, session_id=chat)
        history = zylab._history(
            session_id="chat-a", messages=self._user("a1", "a2"))
        self.assertEqual(history, ["b1", "b2", "a1", "a2", "/model"])

    def test_cold_start_recalls_the_previous_sessions_prompt(self):
        """新 chat 自己一条都没有时，↑ 仍要能找回上一个会话的提问。"""
        store.append_history("上一个会话的提问", session_id="chat-old")
        store.append_history("打标记之前的旧条目")
        self.assertEqual(
            zylab._history(session_id="chat-new", messages=[]),
            ["上一个会话的提问", "打标记之前的旧条目"])

    def test_legacy_chat_is_rebuilt_from_its_own_messages(self):
        """打标记之前的输入无法从全局文件归属；chat 自己的提问就是它的历史。

        那些提问同时以无标记形式躺在全局文件里，不能在两层各出现一次。
        """
        for text in ("别处的旧条目", "旧一", "旧二"):
            store.append_history(text)                 # 全部无标记
        messages = self._user("旧一", "旧二")
        messages.insert(1, {"role": "assistant", "content": "答"})
        self.assertEqual(
            zylab._history(session_id="legacy", messages=messages),
            ["别处的旧条目", "旧一", "旧二"])

    def test_legacy_prefix_and_tagged_tail_are_merged(self):
        for text in ("新一", "/keys", "!ls", "新二"):
            store.append_history(text, session_id="chat-a")
        messages = self._user("旧一", "旧二", "新一", "新二")
        self.assertEqual(
            zylab._history(session_id="chat-a", messages=messages),
            ["旧一", "旧二", "新一", "/keys", "!ls", "新二"])

    def test_runtime_notices_are_not_user_input(self):
        messages = self._user("真提问", "[已中断] 运行时合成的通知")
        self.assertEqual(
            zylab._history(session_id="chat-a", messages=messages), ["真提问"])

    def test_without_a_session_the_old_behaviour_is_kept(self):
        store.append_history("a", session_id="chat-a")
        store.append_history("b", session_id="chat-b")
        self.assertEqual(zylab._history(), ["a", "b"])


class LazySyncTests(_IsolatedHistory):
    """切换 chat 的路径很多（resume/new/clear/fork/回滚），同步只挂在一处。"""

    def _session(self, session_id, messages=()):
        editor = types.SimpleNamespace(target=None)
        return types.SimpleNamespace(
            ag=types.SimpleNamespace(session_id=session_id, messages=list(messages)),
            pump=types.SimpleNamespace(editor=editor)), editor

    def test_history_is_handed_over_once_per_chat(self):
        store.append_history("a1", session_id="chat-a")
        sess, _ = self._session("chat-a")
        sync = zylab.Session._chat_history_if_changed
        self.assertEqual(sync(sess), ["a1"])
        self.assertIsNone(sync(sess), "chat 没变就不该重复整表替换")

    def test_switching_chat_reorders_history_around_the_new_chat(self):
        store.append_history("a1", session_id="chat-a")
        store.append_history("b1", session_id="chat-b")
        sess, _ = self._session("chat-a")
        sync = zylab.Session._chat_history_if_changed
        self.assertEqual(sync(sess), ["b1", "a1"])
        sess.ag.session_id = "chat-b"
        self.assertEqual(sync(sess), ["a1", "b1"], "换了 chat，最近处要换成它的输入")

    def test_sync_is_deferred_not_lost_while_a_subagent_owns_the_input(self):
        store.append_history("a1", session_id="chat-a")
        store.append_history("b1", session_id="chat-b")
        sess, editor = self._session("chat-a")
        sync = zylab.Session._chat_history_if_changed
        sync(sess)
        sess.ag.session_id = "chat-b"
        editor.target = "run-1"                      # 输入目标指向子代理
        self.assertIsNone(sync(sess), "那张历史表属于子代理，不能覆盖")
        editor.target = None
        self.assertEqual(sync(sess), ["a1", "b1"], "回到主输入后必须补同步")


if __name__ == "__main__":
    unittest.main()
