"""decision_gate 工具层测试。

决策门（decision gate）的契约：
- 只用于「猜错代价高」的分叉，schema 描述里写死了判据；
- 2-4 个选项，每个必须有 label，推荐项恰好一个（由调用方保证）；
- 用户选择后返回 {choice, notes, attended}；
- 非交互环境 fail-closed：返回推荐项并标记 attended=False。
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import tools  # noqa: E402


class DecisionGateSchemaTests(unittest.TestCase):
    def test_decision_gate_in_schema_and_impl(self):
        self.assertIn("decision_gate", tools.ALL)
        self.assertIn("decision_gate", tools.IMPL)

    def test_schema_has_anti_abuse_criteria(self):
        schema = next(
            item for item in tools.SCHEMA
            if item["function"]["name"] == "decision_gate")
        desc = schema["function"]["description"]
        # 防滥用判据必须在描述里
        self.assertIn("猜错代价高", desc)
        self.assertIn("低风险", desc)

    def test_schema_requires_gate_before_side_effect_batch(self):
        schema = next(
            item for item in tools.SCHEMA
            if item["function"]["name"] == "decision_gate")
        desc = schema["function"]["description"]
        self.assertIn("下一步的单独调用", desc)
        self.assertIn("不要与写入、workflow 或 subagent 同批", desc)

    def test_schema_supports_legacy_and_multi_question_forms(self):
        schema = next(
            item for item in tools.SCHEMA
            if item["function"]["name"] == "decision_gate")
        props = schema["function"]["parameters"]["properties"]
        self.assertIn("question", props)
        self.assertIn("options", props)
        self.assertIn("context", props)
        params = schema["function"]["parameters"]
        self.assertEqual(params["required"], [])
        self.assertIn({"required": ["question", "options"]},
                      params["oneOf"])
        self.assertIn({"required": ["questions"]}, params["oneOf"])
        self.assertIn("questions", props)


class DecisionGateValidationTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: tools.HOOK_CTX.get(k) for k in ("decision_gate",)}
        tools.HOOK_CTX["decision_gate"] = lambda payload: {
            "choice": payload["options"][0]["label"],
            "notes": "", "attended": True}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                tools.HOOK_CTX.pop(k, None)
            else:
                tools.HOOK_CTX[k] = v

    def test_rejects_empty_question(self):
        with self.assertRaises(ValueError):
            tools.t_decision_gate(question="", options=[
                {"label": "a"}, {"label": "b"}])

    def test_rejects_too_few_options(self):
        with self.assertRaises(ValueError):
            tools.t_decision_gate(question="q", options=[{"label": "a"}])

    def test_rejects_too_many_options(self):
        opts = [{"label": str(i)} for i in range(5)]
        with self.assertRaises(ValueError):
            tools.t_decision_gate(question="q", options=opts)

    def test_rejects_option_without_label(self):
        with self.assertRaises(ValueError):
            tools.t_decision_gate(
                question="q", options=[{"detail": "x"}, {"label": "b"}])

    def test_normalizes_options_and_returns_json(self):
        raw = tools.t_decision_gate(
            question="形状?",
            options=[
                {"label": "A", "detail": "选 A", "cost": "+1 MB",
                 "recommended": True},
                {"label": "B"},
            ],
            context="背景")
        result = json.loads(raw)
        self.assertEqual(result["choice"], "A")
        self.assertTrue(result["attended"])
        self.assertEqual(result["status"], "resolved")

    def test_missing_callback_raises(self):
        tools.HOOK_CTX.pop("decision_gate", None)
        with self.assertRaises(RuntimeError):
            tools.t_decision_gate(
                question="q", options=[
                    {"label": "a", "recommended": True},
                    {"label": "b", "recommended": False},
                ])
        tools.HOOK_CTX["decision_gate"] = lambda payload: {
            "choice": "ok", "notes": "", "attended": True}

    def test_multi_question_request_and_answers_round_trip(self):
        questions = [
            {
                "id": "shape",
                "question": "交付物形状？",
                "options": [
                    {"label": "兼容", "detail": "保留旧字段", "cost": "10 min",
                     "recommended": True},
                    {"label": "迁移", "detail": "切换新字段", "cost": "40 min",
                     "recommended": False},
                ],
            },
            {
                "id": "checks",
                "question": "需要哪些检查？",
                "multi_select": True,
                "options": [
                    {"label": "单测", "detail": "跑 focused tests", "cost": "2 min",
                     "recommended": True},
                    {"label": "PTY", "detail": "跑终端回归", "cost": "5 min",
                     "recommended": False},
                ],
            },
        ]
        request, labels = tools.normalize_decision_gate_request(
            context="背景", questions=questions)
        self.assertEqual([item["id"] for item in request["questions"]],
                         ["shape", "checks"])
        self.assertIn("PTY", labels)
        value = tools.normalize_decision_gate_result({
            "status": "resolved", "attended": True,
            "answers": {"shape": "兼容", "checks": ["单测", "PTY"]},
            "notes": "用户确认",
        }, request)
        self.assertEqual(value["status"], "resolved")
        self.assertEqual(value["choice"], "兼容")
        self.assertEqual(value["answers"]["checks"], ["单测", "PTY"])

    def test_multi_question_missing_or_unknown_answer_is_rejected(self):
        request, _ = tools.normalize_decision_gate_request(questions=[
            {"id": "a", "question": "a", "options": [
                {"label": "x", "recommended": True},
                {"label": "y", "recommended": False},
            ]},
            {"id": "b", "question": "b", "options": [
                {"label": "m", "recommended": True},
                {"label": "n", "recommended": False},
            ]},
        ])
        value = tools.normalize_decision_gate_result({
            "status": "resolved", "attended": True,
            "answers": {"a": "x"},
        }, request)
        self.assertEqual(value["status"], "invalid")
        self.assertFalse(value["attended"])

        value = tools.normalize_decision_gate_result({
            "status": "resolved", "attended": True,
            "answers": {"a": "x", "b": "m", "stale": "x"},
        }, request)
        self.assertEqual(value["status"], "invalid")
        self.assertFalse(value["attended"])

    def test_non_main_context_cannot_enqueue_gate(self):
        for role in ("workflow", "subagent"):
            with self.subTest(role=role):
                called = []
                tools.HOOK_CTX["decision_gate"] = lambda _payload: called.append(1)
                execution = tools.ExecutionContext.capture(
                    session="s", interaction_role=role)
                raw = tools.t_decision_gate(
                    question="q", options=[
                        {"label": "a", "recommended": True},
                        {"label": "b", "recommended": False},
                    ], _execution_context=execution)
                value = json.loads(raw)
                self.assertEqual(value["status"], "unattended")
                self.assertFalse(called)


class DecisionCandidateTests(unittest.TestCase):
    @staticmethod
    def call(name, args):
        return {
            "id": "call-1",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    def test_workflow_without_gate_is_a_candidate(self):
        candidate = tools.decision_gate_candidates([
            self.call("workflow", {"goal": "x", "agents": [{}, {}]})
        ])
        self.assertEqual(candidate["policy"], "decision_gate_required")
        self.assertEqual(candidate["reasons"][0]["kind"], "provider_fanout")

    def test_batched_gate_does_not_authorize_fanout(self):
        candidate = tools.decision_gate_candidates([
            self.call("workflow", {}),
            self.call("decision_gate", {}),
        ])
        self.assertEqual(candidate["policy"], "decision_gate_required")
        self.assertTrue(candidate["gate_batched"])

    def test_candidate_fingerprint_binds_semantic_arguments(self):
        first = tools.decision_gate_candidates([
            self.call("workflow", {"goal": "alpha", "agents": [{}, {}]}),
        ])
        second = tools.decision_gate_candidates([
            self.call("workflow", {"goal": "beta", "agents": [{}, {}]}),
        ])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(
            first["reasons"][0]["args_sha256"],
            second["reasons"][0]["args_sha256"],
        )

    def test_disabled_workflow_follows_session_policy_not_candidate_gate(self):
        self.assertIsNone(tools.decision_gate_candidates(
            [self.call("workflow", {"goal": "x"})],
            allowed_tools={"decision_gate", "read_file"}))

    def test_single_network_bash_uses_transport_gate_not_semantic_gate(self):
        self.assertIsNone(tools.decision_gate_candidates([
            self.call("bash", {"command": "curl https://example.invalid",
                                 "network": True}),
        ]))

    def test_permission_bearing_calls_use_their_own_gate(self):
        # The semantic decision gate must not pre-empt the final-argument
        # permission/evidence protocol for destructive Bash or memory writes.
        self.assertIsNone(tools.decision_gate_candidates([
            self.call("bash", {"command": "rm -rf build"}),
            self.call("memory_write", {"scope": "global", "content": "x"}),
            self.call("memory_forget", {"identifier": "m-1"}),
        ]))


if __name__ == "__main__":
    unittest.main()
