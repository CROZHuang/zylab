"""M2 上下文投影的不变量：raw 可审计、协议完整、预算可解释。"""

import contextlib
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent as A, client, context as C, store, tools
import zylab as CLI


def mk_agent():
    ag = A.Agent.__new__(A.Agent)
    ag.model, ag.gateway, ag.session_id = "test-model", "test", "test-session"
    ag.messages = [{"role": "system", "content": "stable-system"},
                   {"role": "user", "content": "hi"}]
    ag.tokens_in = ag.tokens_out = ag.last_total = ag.turns = 0
    ag.cache_read = ag.cache_write = 0
    ag.cache_reported = False
    ag.compact_failed = None
    ag.context_summary = None
    ag.context_invalid_reason = None
    ag._compact_failed_key = None
    ag._last_age_notice_key = None
    ag.ctx_limit, ag.compact_at, ag.ctx_known = 100_000, 70_000, True
    ag.ctx_limit_source = "test-fixture"
    ag.confirm = lambda *args, **kwargs: True
    ag.started = 0
    return ag


def add_tool_turn(messages, index, content):
    call_id = f"call-{index}"
    messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "bash", "arguments": '{"command":"x"}'},
        }],
    })
    messages.append({"role": "tool", "tool_call_id": call_id,
                     "content": content})


def add_chat_turn(messages, index, width=80):
    messages.append({"role": "user", "content": f"user-{index} " + "u" * width})
    messages.append({"role": "assistant", "content": f"answer-{index} " + "a" * width})


class RawImmutability(unittest.TestCase):
    def test_checkpoint_summary_uses_exact_complete_turn_boundary(self):
        ag = mk_agent()
        add_chat_turn(ag.messages, 1)
        add_chat_turn(ag.messages, 2)
        ag.messages.append({"role": "user", "content": "later prompt"})
        cutoff = len(ag.messages) - 1
        raw_before = copy.deepcopy(ag.messages)

        def summarize(*args, **kwargs):
            yield {"t": "text", "v": "## objective\nexact checkpoint\n## constraints\n- none\n## decisions\n- none\n## files_changed\n- none\n## evidence\n- none\n## pending\n- none\n## risks\n- none"}
            yield {"t": "done", "usage": {}, "reason": "stop"}

        with mock.patch.object(client, "stream_chat", summarize):
            result = ag.summarize_to(cutoff)

        self.assertIn("exact checkpoint", result)
        self.assertEqual(ag.messages, raw_before)
        self.assertEqual(ag.context_summary["covered_to"], cutoff)
        self.assertEqual(
            ag.context_summary["covered_sha256"],
            C.sha256(ag.messages[1:cutoff]))

    def test_checkpoint_summary_rejects_split_turn_and_incomplete_tool_pair(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        with self.assertRaisesRegex(ValueError, "切断"):
            C.plan_compaction_to(messages, 2)

        messages.extend([
            {"role": "user", "content": "tool please"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "missing", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }]},
        ])
        with self.assertRaisesRegex(ValueError, "不完整 tool"):
            C.plan_compaction_to(messages, len(messages))

    def test_aging_changes_projection_not_raw_sha(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(8):
            add_tool_turn(messages, i, f"RAW-{i} " + "x" * 4_000 + "\n[exit 7]")
        before = C.sha256(messages)
        projection = C.materialize(messages, tools_schema=[],
                                   model_limit=100_000, usable_budget=80_000)
        self.assertEqual(C.sha256(messages), before)
        self.assertNotEqual(projection.report["projected_sha256"], before)
        self.assertGreater(projection.report["tool_previews"]["saved_chars"], 0)
        self.assertTrue(any(a["status"] == "exit:7" for a in
                            projection.report["tool_previews"]["largest_artifacts"]))
        self.assertIn("RAW-0", json.dumps(projection.messages, ensure_ascii=False))

        # UI notice 应按实际 preview 集合去重，而不是按每轮都会变化的 raw hash。
        messages[0]["content"] = "changed-system"
        same_previews = C.materialize(
            messages, tools_schema=[], model_limit=100_000,
            usable_budget=80_000)
        self.assertNotEqual(
            projection.report["raw_sha256"],
            same_previews.report["raw_sha256"])
        self.assertEqual(
            projection.report["tool_previews"]["fingerprint_sha256"],
            same_previews.report["tool_previews"]["fingerprint_sha256"])

    def test_control_messages_do_not_mutate_raw(self):
        messages = [{"role": "system", "content": "stable"},
                    {"role": "user", "content": "verbatim"}]
        before = copy.deepcopy(messages)
        raw_sha = C.sha256(messages)
        baseline = C.materialize(messages, tools_schema=[])
        projection = C.materialize(
            messages,
            control_messages=[{"role": "system", "content": "plan-control"}],
            tools_schema=[])
        self.assertEqual(messages, before)
        self.assertEqual(projection.report["raw_sha256"], raw_sha)
        self.assertEqual(projection.report["raw_message_count"], 2)
        self.assertEqual([m["content"] for m in projection.messages
                          if m["role"] == "user"], ["verbatim"])
        self.assertIn("plan-control", [m["content"] for m in projection.messages
                                       if m["role"] == "system"])
        self.assertGreater(projection.report["components"]["system"],
                           baseline.report["components"]["system"])

    def test_control_messages_reject_non_system_roles(self):
        with self.assertRaisesRegex(ValueError, "role=system"):
            C.materialize(
                [{"role": "system", "content": "sys"}],
                control_messages=[{"role": "user", "content": "unsafe"}],
                tools_schema=[])

    def test_successful_compaction_keeps_raw_and_records_coverage(self):
        ag = mk_agent()
        for i in range(10):
            add_chat_turn(ag.messages, i)
        ag.last_total = 99_000
        raw_before = copy.deepcopy(ag.messages)
        raw_sha = C.sha256(ag.messages)

        def summarize(*args, **kwargs):
            yield {"t": "text", "v": "## objective\nkeep working\n## constraints\nnone\n"
                                           "## decisions\nnone\n## files_changed\nnone\n"
                                           "## evidence\nnone\n## pending\ncontinue\n## risks\nnone"}
            yield {"t": "done", "usage": {}, "reason": "stop"}

        with mock.patch.object(client, "stream_chat", summarize), \
             mock.patch.object(A.models_db, "supports_temperature", return_value=True):
            summary = ag.maybe_compact()

        self.assertIn("## objective", summary)
        self.assertEqual(ag.messages, raw_before)
        self.assertEqual(C.sha256(ag.messages), raw_sha)
        self.assertEqual(ag.context_summary["status"], "valid")
        self.assertGreater(ag.context_summary["covered_to"], 1)
        projection = ag.project_context([])
        self.assertEqual(projection.report["summary"]["status"], "valid")
        self.assertTrue(any("conversation-summary" in str(m.get("content"))
                            for m in projection.messages))

    def test_summary_failure_keeps_previous_valid_summary(self):
        ag = mk_agent()
        for i in range(12):
            add_chat_turn(ag.messages, i)
        first_plan = C.plan_compaction(ag.messages, keep_tail=6)
        ag.context_summary = C.make_summary(
            "## objective\nold", first_plan, model=ag.model, gateway=ag.gateway)
        old_summary = copy.deepcopy(ag.context_summary)
        for i in range(12, 20):
            add_chat_turn(ag.messages, i)
        raw_sha = C.sha256(ag.messages)
        with mock.patch.object(client, "stream_chat",
                               side_effect=client.APIError("HTTP 503")):
            result = ag.force_compact()
        self.assertIn("摘要生成失败", result)
        self.assertEqual(ag.context_summary, old_summary)
        self.assertEqual(C.sha256(ag.messages), raw_sha)
        self.assertEqual(ag.project_context([]).report["summary"]["status"], "valid")

    def test_partial_compaction_stream_is_not_accepted(self):
        ag = mk_agent()
        for i in range(10):
            add_chat_turn(ag.messages, i)
        ag.last_total = 99_000
        raw_sha = C.sha256(ag.messages)

        def partial(*args, **kwargs):
            yield {"t": "text", "v": "## objective\ntruncated"}
            raise client.APIError("stream broke")

        with mock.patch.object(client, "stream_chat", partial):
            result = ag.maybe_compact()

        self.assertIn("摘要生成失败", result)
        self.assertIn("stream broke", result)
        self.assertIsNone(ag.context_summary)
        self.assertEqual(C.sha256(ag.messages), raw_sha)

    def test_stale_summary_is_rejected_on_restore(self):
        ag = mk_agent()
        for i in range(10):
            add_chat_turn(ag.messages, i)
        plan = C.plan_compaction(ag.messages, keep_tail=6)
        summary = C.make_summary("## objective\nx", plan,
                                 model=ag.model, gateway=ag.gateway)
        ag.messages[2]["content"] = "prefix was edited"
        self.assertFalse(ag.load_context({"summary": summary}))
        self.assertIsNone(ag.context_summary)
        self.assertEqual(ag.context_invalid_reason, "covered_prefix_changed")


class ToolProtocolIntegrity(unittest.TestCase):
    def test_incomplete_tool_pair_never_reaches_provider_projection(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "partial", "tool_calls": [{
                "id": "missing", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }]},
            {"role": "user", "content": "recover here"},
        ]
        raw_before = copy.deepcopy(messages)
        projection = C.materialize(messages, tools_schema=[],
                                   model_limit=10_000, usable_budget=8_000)
        sent_calls = [c for m in projection.messages for c in m.get("tool_calls", [])]
        self.assertEqual(sent_calls, [])
        self.assertEqual(messages, raw_before)
        self.assertEqual(len(projection.report["incomplete_tool_pairs"]), 1)
        self.assertIn("incomplete_tool_pair",
                      json.dumps(projection.messages, ensure_ascii=False))
        self.assertIn("recover here", json.dumps(projection.messages, ensure_ascii=False))

    def test_complete_parallel_pair_is_kept_as_one_unit(self):
        messages = [{"role": "system", "content": "sys"}, {
            "role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}},
                {"id": "b", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "A"},
            {"role": "tool", "tool_call_id": "b", "content": "B"},
        ]
        projection = C.materialize(messages, tools_schema=[],
                                   model_limit=10_000, usable_budget=8_000)
        called = {c["id"] for m in projection.messages for c in m.get("tool_calls", [])}
        answered = {m["tool_call_id"] for m in projection.messages
                    if m.get("role") == "tool"}
        self.assertEqual(called, {"a", "b"})
        self.assertEqual(answered, called)

    def test_agent_run_sends_projection_instead_of_raw(self):
        ag = mk_agent()
        ag.messages.append({
            "role": "assistant", "content": "partial", "tool_calls": [{
                "id": "lost", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }],
        })
        seen = {}

        def fake(model, messages, **kwargs):
            seen["messages"] = copy.deepcopy(messages)
            yield {"t": "text", "v": "recovered"}
            yield {"t": "done", "usage": {}, "reason": "stop"}

        with mock.patch.object(client, "stream_chat", fake), \
             mock.patch.object(ag, "log_usage"):
            list(ag.run("continue"))
        sent_calls = [c for m in seen["messages"] for c in m.get("tool_calls", [])]
        self.assertEqual(sent_calls, [])
        self.assertIn("context-projection-omissions",
                      json.dumps(seen["messages"], ensure_ascii=False))
        self.assertTrue(any(m.get("tool_calls") for m in ag.messages),
                        "raw 中的崩溃证据必须保留")


class BudgetAndExplainability(unittest.TestCase):
    def test_budget_omits_old_complete_units_with_explicit_marker(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(12):
            messages.append({"role": "user", "content": f"user-{i}"})
            messages.append({"role": "assistant",
                             "content": f"answer-{i} " + "a" * 900})
        messages.append({"role": "user", "content": "LATEST-REQUEST"})
        raw_sha = C.sha256(messages)
        projection = C.materialize(messages, tools_schema=[],
                                   model_limit=4_000, usable_budget=2_000)
        self.assertTrue(projection.fits)
        self.assertTrue(any(r["reason"] == "budget_omission"
                            for r in projection.report["omitted_ranges"]))
        for omitted in projection.report["omitted_ranges"]:
            if omitted["reason"] != "budget_omission":
                continue
            self.assertEqual(messages[omitted["start"]]["role"], "user")
            if omitted["end"] < len(messages):
                self.assertEqual(messages[omitted["end"]]["role"], "user")
        serialized = json.dumps(projection.messages, ensure_ascii=False)
        self.assertIn("context-projection-omissions", serialized)
        self.assertIn("LATEST-REQUEST", serialized)
        self.assertEqual(
            [m["content"] for m in projection.messages if m["role"] == "user"],
            [m["content"] for m in messages if m["role"] == "user"],
        )
        self.assertTrue(all(r.get("preserved_user_count", 0) > 0
                            for r in projection.report["omitted_ranges"]
                            if r["reason"] == "budget_omission"))
        self.assertEqual(C.sha256(messages), raw_sha)

    def test_oversized_latest_input_fails_closed(self):
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "x" * 20_000}]
        projection = C.materialize(messages, tools_schema=[],
                                   model_limit=1_000, usable_budget=500)
        self.assertFalse(projection.fits)
        self.assertIn("cannot fit", projection.report["next_action"])

    def test_report_components_reconcile_to_current_estimate(self):
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"}]
        projection = C.materialize(
            messages, tools_schema=[{"type": "function"}],
            model_limit=100_000, usable_budget=85_000,
            model_limit_source="probed", last_provider_tokens=321)
        c = projection.report["components"]
        request_keys = ("system", "tools_schema", "summary", "verbatim_users",
                        "recent_messages", "tool_results", "tool_previews",
                        "omission_marker")
        self.assertEqual(sum(c[key] for key in request_keys),
                         projection.report["estimated_request_tokens"])
        self.assertEqual(projection.report["model_limit_source"], "probed")
        self.assertEqual(projection.report["last_provider_tokens_source"],
                         "provider_usage")
        self.assertEqual(c["reserved_output"] + c["reserved_tools"]
                         + c["reserved_safety"], 15_000)

    def test_context_command_explains_sources_and_components(self):
        ag = mk_agent()
        ag.last_total = 321
        ag.messages.append({"role": "user", "content": "hello"})
        session = type("SessionStub", (), {"ag": ag})()
        output = StringIO()
        with contextlib.redirect_stdout(output):
            CLI.cmd_context(session, "")
        rendered = output.getvalue()
        for label in ("模型窗口", "source=test-fixture", "可用预算",
                      "当前投影", "上次实测", "tool schemas",
                      "user verbatim", "用户原话", "reserved: output",
                      "最大 artifact", "raw sha256", "下一步"):
            self.assertIn(label, rendered)
        self.assertIn("context", CLI.REGISTRY)

    def test_compaction_boundary_keeps_whole_user_tool_turns(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(9):
            messages.append({"role": "user", "content": f"request-{i}"})
            add_tool_turn(messages, i, f"tool-{i}")
            messages.append({"role": "assistant", "content": f"final-{i}"})
        plan = C.plan_compaction(messages, keep_tail=3)
        self.assertEqual(plan["source_turns"], 6)
        self.assertEqual(messages[plan["covered_to"]]["role"], "user")
        self.assertEqual(plan["source_messages"][-1]["content"], "final-5")
        summary = C.make_summary("## objective\ncontinue", plan,
                                 model="m", gateway="g")
        projection = C.materialize(
            messages, summary=summary, tools_schema=[],
            model_limit=100_000, usable_budget=80_000)
        # summary 后先给被覆盖的 user 原话，再接幸存的新 user turn。
        summary_index = next(i for i, message in enumerate(projection.messages)
                             if "conversation-summary" in str(message.get("content")))
        user_contents = [m["content"] for m in projection.messages
                         if m["role"] == "user"]
        self.assertEqual(user_contents,
                         [m["content"] for m in messages if m["role"] == "user"])
        request_5_index = next(i for i, message in enumerate(projection.messages)
                               if message.get("content") == "request-5")
        request_6_index = next(i for i, message in enumerate(projection.messages)
                               if message.get("content") == "request-6")
        self.assertGreater(next(i for i, message in enumerate(projection.messages)
                                if message.get("content") == "request-0"),
                           summary_index)
        self.assertLess(request_5_index, request_6_index)

    def test_summary_prompt_has_required_structured_fields(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(10):
            add_chat_turn(messages, i)
        prompt = C.summary_prompt(C.plan_compaction(messages, keep_tail=6))
        for field in ("objective", "constraints", "decisions", "files_changed",
                      "evidence", "pending", "risks"):
            self.assertIn(f"## {field}", prompt)


class VerbatimUserRetention(unittest.TestCase):
    def test_compacted_user_messages_are_exact_and_not_synthetic(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(10):
            add_chat_turn(messages, i, width=120)
        plan = C.plan_compaction(messages, keep_tail=4)
        summary = C.make_summary("## objective\ncontinue", plan,
                                 model="m", gateway="g")

        projection = C.materialize(
            messages, summary=summary, tools_schema=[],
            model_limit=100_000, usable_budget=80_000)

        raw_users = [m for m in messages if m["role"] == "user"]
        sent_users = [m for m in projection.messages if m["role"] == "user"]
        self.assertEqual(sent_users, raw_users)
        self.assertEqual(projection.report["verbatim_users"]["count"], 10)
        self.assertTrue(projection.report["verbatim_users"]["preserved"])
        self.assertEqual(projection.report["verbatim_users"]["truncated"], 0)
        controls = [m for m in projection.messages
                    if "conversation-summary" in str(m.get("content"))]
        self.assertEqual([m["role"] for m in controls], ["assistant"])

    def test_two_compactions_keep_first_generation_user_messages(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(10):
            add_chat_turn(messages, i)
        first_plan = C.plan_compaction(messages, keep_tail=4)
        first = C.make_summary("## objective\nfirst", first_plan,
                               model="m", gateway="g")
        for i in range(10, 18):
            add_chat_turn(messages, i)
        second_plan = C.plan_compaction(messages, first, keep_tail=4)
        second = C.make_summary("## objective\nsecond", second_plan,
                                model="m", gateway="g")

        projection = C.materialize(
            messages, summary=second, tools_schema=[],
            model_limit=100_000, usable_budget=80_000)

        self.assertGreater(second["covered_to"], first["covered_to"])
        self.assertEqual(
            [m["content"] for m in projection.messages if m["role"] == "user"],
            [m["content"] for m in messages if m["role"] == "user"],
        )
        self.assertIn("user-0", projection.messages[2]["content"])

    def test_oversized_user_is_never_truncated_even_when_projection_cannot_fit(self):
        content = "BEGIN-" + "x" * 40_000 + "-END"
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": content}]

        projection = C.materialize(
            messages, tools_schema=[], model_limit=2_000, usable_budget=1_000)

        self.assertFalse(projection.fits)
        self.assertEqual(
            [m["content"] for m in projection.messages if m["role"] == "user"],
            [content],
        )
        self.assertEqual(projection.report["verbatim_users"]["truncated"], 0)
        self.assertIn("verbatim context cannot fit",
                      projection.report["next_action"])

    def test_three_hundred_turn_projection_is_stable_linear_and_raw_immutable(self):
        messages = [{"role": "system", "content": "stable"}]
        expected_users = []
        for index in range(300):
            user = f"user-{index:03d} " + "长期研究约束" * 8
            expected_users.append(user)
            call_id = f"call-{index:03d}"
            messages.extend([
                {"role": "user", "content": user},
                {"role": "assistant", "content": "",
                 "tool_calls": [{
                     "id": call_id, "type": "function",
                     "function": {"name": "read_file",
                                  "arguments": '{"path":"evidence"}'},
                 }]},
                {"role": "tool", "tool_call_id": call_id,
                 "content": f"evidence-{index}\n" + "x" * 8_000},
            ])
        raw_before = copy.deepcopy(messages)
        plan = C.plan_compaction_to(messages, 1 + 250 * 3)
        summary = C.make_summary(
            "## objective\ncontinue long research\n## pending\n50 turns",
            plan, model="m", gateway="g")

        started = time.monotonic()
        first = C.materialize(
            messages, summary=summary, tools_schema=[],
            model_limit=240_000, usable_budget=200_000)
        elapsed = time.monotonic() - started
        second = C.materialize(
            messages, summary=summary, tools_schema=[],
            model_limit=240_000, usable_budget=200_000)

        self.assertEqual(messages, raw_before)
        self.assertEqual(first.report["projected_sha256"],
                         second.report["projected_sha256"])
        self.assertEqual(
            [message["content"] for message in first.messages
             if message["role"] == "user"], expected_users)
        self.assertTrue(first.fits)
        self.assertLess(elapsed, 5.0)


class Persistence(unittest.TestCase):
    def test_session_json_round_trips_context_metadata(self):
        ag = mk_agent()
        ag.context_summary = {"status": "valid", "content": "summary"}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(store, "SESSIONS", Path(td)), \
             mock.patch.object(store, "ensure_home"), \
             mock.patch.object(store, "_shadow_sync_session"):
            path = store.save_session(ag)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["context"], ag.context_snapshot())


if __name__ == "__main__":
    unittest.main(verbosity=2)
