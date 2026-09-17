import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.agent import _repair_truncated_calls

class RepairTruncatedCallsTests(unittest.TestCase):
    def test_valid_args_untouched(self):
        calls = [{"id": "c1", "function": {"name": "bash",
                  "arguments": '{"command": "ls"}'}}]
        out = _repair_truncated_calls(calls)
        self.assertEqual(out[0]["function"]["arguments"], '{"command": "ls"}')

    def test_truncated_args_replaced_with_valid_json(self):
        # 模拟 09-17 实测的截断：第 13 列字符串未终止
        calls = [{"id": "c1", "function": {"name": "bash",
                  "arguments": '{"command": "cd /tmp/workspa'}}]
        out = _repair_truncated_calls(calls)
        args = json.loads(out[0]["function"]["arguments"])  # 必须可解析
        self.assertIn("_truncated", args)
        self.assertIn("cd /tmp/workspa", args["_truncated"])

    def test_repaired_calls_survive_roundtrip(self):
        # 修复后的 calls 能被 json.dumps/loads 完整往返（服务端解析不再 400）
        calls = [{"id": "c1", "function": {"name": "bash",
                  "arguments": '{"command": "unterminated'}}]
        out = _repair_truncated_calls(calls)
        payload = json.dumps({"messages": [{"tool_calls": out}]})
        json.loads(payload)  # 不抛异常
        self.assertIn("_truncated", payload)

    def test_empty_args_ok(self):
        calls = [{"id": "c1", "function": {"name": "bash",
                  "arguments": ""}}]
        out = _repair_truncated_calls(calls)
        json.loads(out[0]["function"]["arguments"])  # "" -> JSONDecodeError? "" 是坏 JSON
        # 空字符串也该被修复成合法占位
        self.assertIn("_truncated", out[0]["function"]["arguments"])

if __name__ == "__main__":
    unittest.main()
