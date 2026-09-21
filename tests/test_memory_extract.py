"""轮末自动抽取跨会话记忆：解析要能抢救残缺输出，触发要克制，写入只进 project 域。

为什么要有这条通道：`memory_write` 一直都在、系统提示也写了该记什么，2026-09-20 统计用户的
7 个真实会话——2,925 条助手消息里调用 **0 次**。同日读 CC / Codex / ZCode 的实现，没有一家
靠主模型自觉。抽取的形状与 recap 同源（缓存安全的旁路请求，见 test_away_recap）。

真实网关实测（deepseek-v4-flash@deepinfer，4 个会话）：3.5–24 s、336–1,047 输出 token；
其中一次在最后一个 `}` 之前停住，glm-5.3 那次干脆写成散文并撞满额度。所以下面的解析用例
全部取自那几次的真实形态。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests  # noqa: F401,E402  —— 状态目录隔离

from core import agent as A                           # noqa: E402
from core import memory as M                          # noqa: E402
from core import memory_extract as E                  # noqa: E402

# 这两条是**合成的**，不是真实会话的抽取结果。第一版直接拿了开发期在真机上
# 抽到的两条（含本机路径与一个人名），而 tests/ 是要随仓库公开发布的——
# 测试夹具会跟着代码走到任何一台陌生机器上。形状（字段、长度、可截断性）
# 与真实输出一致，内容不指向任何真实的人或机器。
ENTRY = ('{"name": "wal-mode-probe", "type": "project", '
         '"description": "SQLite WAL 实测：跨进程可见性与 fsync 的代价", '
         '"content": "2026-09-20 在 tmp/wal-probe/ 下实测：WAL 模式下第一个打开者'
         '才建出 -wal 文件，并发读不阻塞写；把 synchronous 从 FULL 调到 NORMAL 后'
         '写入快 3.1 倍，代价是断电会丢最后一个事务。以后要复现直接进这个目录。", '
         '"updates": null}')
SECOND = ('{"name": "upstream-issue-4417", "type": "reference", '
          '"description": "上游把这个行为标成 wontfix", '
          '"content": "2026-09-20 查证：上游 issue 4417 已关成 wontfix，理由是该'
          '行为由 POSIX 规定，不是缺陷。以后再问直接引用此结论，别再翻一遍。", '
          '"updates": null}')


class SalvageTests(unittest.TestCase):
    def test_a_clean_answer_parses(self):
        got = E.parse('{"entries": [' + ENTRY + ", " + SECOND + "]}")
        self.assertEqual([e["name"] for e in got],
                         ["wal-mode-probe", "upstream-issue-4417"])
        self.assertEqual(got[0]["type"], "project")
        self.assertEqual(got[0]["stable_key"], "auto:wal-mode-probe")
        self.assertEqual(got[0]["updates"], "")

    def test_a_truncated_answer_keeps_the_entries_that_did_arrive(self):
        """实测形态：整段是合法 JSON，就差最后一个 `}`。"""
        text = '{"entries": [' + ENTRY + ", " + SECOND + "]"
        self.assertEqual(len(E.parse(text)), 2)

    def test_an_entry_cut_mid_string_is_dropped_not_guessed(self):
        text = '{"entries": [' + ENTRY + ", " + SECOND[:80]
        got = E.parse(text)
        self.assertEqual([e["name"] for e in got], ["wal-mode-probe"])

    def test_code_fences_and_prose_around_the_json_are_tolerated(self):
        text = "这是我要记的：\n```json\n{\"entries\": [" + ENTRY + "]}\n```\n以上。"
        self.assertEqual(len(E.parse(text)), 1)

    def test_prose_instead_of_json_yields_nothing(self):
        """glm-5.3 实测：写成散文并撞满额度。宁可不记，也不能记错。"""
        self.assertEqual(E.parse("好的，我来梳理一下这次会话值得记的内容：首先……"), [])

    def test_an_empty_list_is_a_valid_answer(self):
        self.assertEqual(E.parse('{"entries": []}'), [])

    def test_junk_entries_are_dropped(self):
        text = ('{"entries": ['
                '{"name": "Bad Name", "content": "' + "x" * 60 + '"},'
                '{"name": "too-short", "content": "太短"},'
                '{"name": "no-content"},'
                + ENTRY + "]}")
        self.assertEqual([e["name"] for e in E.parse(text)],
                         ["wal-mode-probe"])

    def test_an_unknown_type_falls_back_and_duplicates_collapse(self):
        text = ('{"entries": ['
                + ENTRY.replace('"type": "project"', '"type": "nonsense"') + ", "
                + ENTRY + "]}")
        got = E.parse(text)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["type"], M.DEFAULT_TYPE)

    def test_the_cap_bounds_one_pass(self):
        many = ", ".join(
            ENTRY.replace("wal-mode-probe", f"entry-{i}") for i in range(12))
        self.assertEqual(len(E.parse('{"entries": [' + many + "]}")), E.MAX_ENTRIES)

    def test_the_existing_block_is_only_added_when_there_is_an_index(self):
        self.assertEqual(E.existing_block(""), "")
        self.assertIn("m-1", E.existing_block("- m-1 · user · project · x — y"))
        self.assertIn("不要新建近似副本", E.existing_block("- m-1"))


class SideQueryShapeTests(unittest.TestCase):
    """抽取必须和主循环同前缀（工具照发不许用），否则 prompt cache 全废。"""

    def agent(self, script, *, messages=None):
        agent = A.Agent.__new__(A.Agent)
        agent.messages = messages if messages is not None else [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "帮我量一下 SQLite 的 WAL"},
            {"role": "assistant", "content": "搭好了"},
            {"role": "user", "content": "再验证一下无 GPU 能不能跑"},
            {"role": "assistant", "content": "可以，解释器模式 err=0"},
        ]
        agent.model, agent.gateway = "kimi-k3-256k", "deepinfer"
        agent.session_id = "extract"
        agent.context_summary = None
        agent.compact_failed = None
        agent._compact_failed_key = None
        agent.last_total = 0
        agent.compact_at, agent.ctx_limit = 200_000, 264_000
        agent._trace_context = lambda *a, **k: None
        agent._last_request_shape = ([{"type": "function", "function": {
            "name": "bash", "parameters": {}}}], None)
        self.sent = []

        def provider(model, messages, **kwargs):
            self.sent.append((model, messages, kwargs))
            item = script.pop(0) if script else []
            if isinstance(item, Exception):
                raise item
            return iter(item)

        return agent, provider

    def run_extract(self, script, **kwargs):
        agent, provider = self.agent(list(script))
        with mock.patch.object(A.client, "stream_chat", provider), \
             mock.patch.object(A.models_db, "thinking_off_rejected",
                               return_value=False), \
             mock.patch.object(A.models_db, "note_thinking_off_rejected"), \
             mock.patch.object(A.models_db, "supports_temperature",
                               return_value=True):
            return agent.extract_memories(**kwargs)

    def test_the_prefix_matches_the_main_loop_and_tools_are_offered(self):
        result = self.run_extract(
            [[{"t": "text", "v": '{"entries": [' + ENTRY + "]}"}]],
            index_text="- m-1 · user · project · 页面偏好 — 报告做成页面")
        self.assertEqual(result["kind"], "ok")
        self.assertEqual([e["name"] for e in result["entries"]],
                         ["wal-mode-probe"])
        model, messages, kwargs = self.sent[0]
        self.assertTrue(kwargs["tools"], "工具必须照发，否则前缀和主循环不同")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("值得跨会话长期保留", messages[-1]["content"])
        self.assertIn("m-1", messages[-1]["content"], "已有条目要附进去防止写重复")
        self.assertEqual(kwargs["thinking"], {"type": "disabled"})

    def test_a_tool_call_in_the_answer_is_a_failure_not_an_execution(self):
        result = self.run_extract([[{"t": "tool", "v": [{"id": "1"}]}]])
        self.assertEqual(result["kind"], "failed")
        self.assertEqual(result["entries"], [])
        self.assertIn("工具", result["text"])

    def test_thinking_starved_retries_once_with_a_bigger_budget(self):
        result = self.run_extract([
            [{"t": "reasoning", "v": "想" * 400}],
            [{"t": "text", "v": '{"entries": [' + ENTRY + "]}"}],
        ])
        self.assertEqual(result["kind"], "ok")
        self.assertEqual(len(self.sent), 2)
        self.assertIsNone(self.sent[1][2]["thinking"])
        self.assertGreater(self.sent[1][2]["max_tokens"],
                           self.sent[0][2]["max_tokens"])

    def test_a_provider_error_fails_silently(self):
        down = A.client.APIError("HTTP 503", kind="transient_http", status=503)
        result = self.run_extract([down])
        self.assertEqual(result["kind"], "api-error")
        self.assertEqual(result["entries"], [])

    def test_a_short_conversation_is_not_worth_extracting(self):
        agent, provider = self.agent([], messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ])
        with mock.patch.object(A.client, "stream_chat", provider):
            result = agent.extract_memories()
        self.assertEqual(result["kind"], "no-turn")
        self.assertEqual(self.sent, [], "不值得抽就别发请求")


class SessionTriggerTests(unittest.TestCase):
    """触发点是用户 2026-09-21 定的：空闲 / 会话结束 + 压缩时，不是每轮。"""

    def session(self, *, users=6, generate=True):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session.ag = mock.Mock()
        session.ag.messages = []
        for index in range(users):
            session.ag.messages.append(
                {"role": "user", "content": f"任务 {index}"})
            session.ag.messages.append(
                {"role": "assistant", "content": "好"})
        session.ag.context_summary = None
        session.memory_generate = generate
        session.memory_use = True
        session._turn_running = False
        session._memory_extract_thread = None
        session._memory_extract_cancel = None
        session._memory_extract_due = None
        session._memory_extract_users = 0
        session._memory_extract_summary = None
        return session

    def test_it_waits_for_the_idle_delay_after_a_turn(self):
        session = self.session()
        session.memory_extract_turn(False)
        self.assertFalse(session._memory_extract_ready_to_run())
        session._memory_extract_due = 0.0             # 时间到了
        self.assertTrue(session._memory_extract_ready_to_run())

    def test_a_running_turn_cancels_and_blocks_it(self):
        session = self.session()
        cancel = mock.Mock()
        session._memory_extract_cancel = cancel
        session.memory_extract_turn(True)
        cancel.set.assert_called_once_with()
        self.assertIsNone(session._memory_extract_due)
        self.assertFalse(session._memory_extract_ready_to_run())

    def test_it_does_not_re_extract_the_same_conversation(self):
        session = self.session()
        session._memory_extract_due = 0.0
        session._memory_extract_users = sum(
            1 for m in session.ag.messages if m["role"] == "user")
        self.assertFalse(session._memory_extract_ready_to_run())
        session.ag.messages.append({"role": "user", "content": "又一个任务"})
        session.ag.messages.append({"role": "user", "content": "再一个"})
        self.assertTrue(session._memory_extract_ready_to_run())

    def test_a_compaction_always_earns_one_pass(self):
        """压过一段就必抽一次：那段细节马上就只剩摘要了。"""
        session = self.session()
        session._memory_extract_due = 0.0
        session._memory_extract_users = sum(
            1 for m in session.ag.messages if m["role"] == "user")
        self.assertFalse(session._memory_extract_ready_to_run())
        session.ag.context_summary = {"covered_sha256": "abc"}
        self.assertTrue(session._memory_extract_ready_to_run())

    def test_generate_off_disables_it(self):
        session = self.session(generate=False)
        session._memory_extract_due = 0.0
        self.assertFalse(session._memory_extract_ready_to_run())


class SessionWriteTests(unittest.TestCase):
    def session(self, tmp):
        import zylab                                  # noqa: PLC0415

        session = zylab.Session.__new__(zylab.Session)
        session.ag = mock.Mock()
        session.ag.session_id = "s-1"
        session._session_cwd = tmp
        session.memory_store = M.MemoryStore(os.path.join(tmp, "memory"))
        session.memory_use = False
        session._memory_error = None
        session.save = lambda: None
        session.refresh_memory_context = lambda: None
        return session

    def result(self, entries):
        return {"kind": "ok", "model": "m", "gateway": "g", "entries": entries}

    def test_entries_land_in_project_scope_with_provenance(self):
        import tempfile                               # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            session = self.session(tmp)
            saved = session._store_extracted(
                self.result(E.parse('{"entries": [' + ENTRY + "]}")))
            self.assertEqual(len(saved), 1)
            row = saved[0]
            self.assertEqual(row["scope"], M.PROJECT, "自动抽取绝不写 global")
            self.assertEqual(row["kind"], "auto_extract")
            self.assertEqual(row["type"], "project")
            self.assertEqual(row["source_session"], "s-1")
            self.assertEqual(row["evidence"]["writer"], "auto_extract")
            self.assertIn("wal", row["description"].lower() + row["title"])

    def test_the_same_fact_updates_in_place_across_sessions(self):
        import tempfile                               # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            session = self.session(tmp)
            first = session._store_extracted(
                self.result(E.parse('{"entries": [' + ENTRY + "]}")))[0]
            session.ag.session_id = "s-2"
            later = ENTRY.replace("以后要复现直接进这个目录。",
                                  "2026-09-21 又加了 02_softmax.py。")
            second = session._store_extracted(
                self.result(E.parse('{"entries": [' + later + "]}")))[0]
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(
                len(session.memory_store.list(cwd=tmp)), 1, "不该留下近似副本")
            self.assertIn("02_softmax.py", second["content"])

    def test_it_refuses_to_overwrite_a_human_written_entry(self):
        import tempfile                               # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            session = self.session(tmp)
            mine = session.memory_store.add(
                "用户定：报告默认做成页面。", title="页面偏好", cwd=tmp,
                entry_type="user", stable_key="pages")
            entry = E.parse('{"entries": [' + ENTRY + "]}")[0]
            entry["updates"] = mine["id"]
            saved = session._store_extracted(self.result([entry]))
            rows = session.memory_store.list(cwd=tmp)
            self.assertEqual(len(rows), 2, "人写的那条必须原样还在")
            self.assertNotEqual(saved[0]["id"], mine["id"])
            kept = session.memory_store.get(mine["id"], cwd=tmp)
            self.assertIn("报告默认做成页面", kept["content"])


if __name__ == "__main__":
    unittest.main()
