"""PTY harness 自己的单元测试——它坏了，18 个 PTY 用例一起说不清话。

2026-09-22 公开仓库 CI：Windows 两列**各 3 个 ERROR** 都出在同一行
`json.loads(payload)` 上（`Extra data: line 1 column N`）。真因不是被测的东西，
是提取方式：`json.loads` 要求整个字符串**恰好**是一个 JSON 值，而 ConPTY 是从
屏幕缓冲区重绘的，`RESULT:` 那条逻辑行后面常常紧跟着别的屏幕内容。

这份用例刻意在 **Linux 上**就把 ConPTY 那几种形态喂进去——harness 的解析是纯函数，
不需要那台机器才能验。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401,E402  —— 状态目录隔离

from tests.pty_harness import result_payload  # noqa: E402


class ResultExtraction(unittest.TestCase):
    def test_a_clean_posix_line_is_unchanged(self):
        out = 'blah\r\n› /exit\r\n\r\nRESULT:{"calls": 1, "draft": ""}\r\n'
        self.assertEqual(result_payload(out), {"calls": 1, "draft": ""})

    def test_trailing_screen_content_on_the_same_line_is_ignored(self):
        """ConPTY 的形态：JSON 后面直接跟着重绘出来的屏幕内容，没有换行。"""
        out = ('RESULT:{"calls": 1, "draft": ""}\x1b[K\x1b[19;10H\x1b[?25h'
               '› 输入消息 · /…  空闲 · chat')
        self.assertEqual(result_payload(out), {"calls": 1, "draft": ""})

    def test_control_sequences_between_the_marker_and_the_json(self):
        out = 'RESULT:\x1b[0m\x1b[?25l {"ok": true}  \x1b[K'
        self.assertEqual(result_payload(out), {"ok": True})

    def test_the_last_marker_wins(self):
        """重绘会把同一条 RESULT 再画一遍；取最后一条（原行为，不能改）。"""
        out = 'RESULT:{"n": 1}\r\n…重绘…\r\nRESULT:{"n": 2}\r\n'
        self.assertEqual(result_payload(out), {"n": 2})

    def test_a_list_payload_works_too(self):
        out = 'RESULT:[]\r\n'
        self.assertEqual(result_payload(out), [])
        out = 'RESULT:[1, 2]\x1b[K trailing'
        self.assertEqual(result_payload(out), [1, 2])

    def test_a_genuinely_broken_payload_still_raises(self):
        """容忍尾巴**不等于**容忍坏数据：child 没吐出 JSON 就该红。"""
        import json
        with self.assertRaises(json.JSONDecodeError):
            result_payload("RESULT:not json at all\r\n")


class ScalarPayloads(unittest.TestCase):
    """有 child 打的是 `json.dumps(picked)` —— 那可能是标量，没有 `{` 也没有 `[`。

    第一版的候选集只认 `{` / `[`，于是全量跑的时候有一条当场红。
    「容忍尾巴」不该以「只支持容器」为代价。
    """

    def test_a_string_payload(self):
        self.assertEqual(result_payload('RESULT:"picked-one"\r\n'), "picked-one")

    def test_null_and_numbers(self):
        self.assertIsNone(result_payload("RESULT:null\r\n"))
        self.assertEqual(result_payload("RESULT:3\r\n"), 3)
        self.assertIs(result_payload("RESULT:true\r\n"), True)

    def test_a_scalar_with_trailing_screen_content(self):
        self.assertEqual(
            result_payload('RESULT:"ok"\x1b[K trailing screen'), "ok")


if __name__ == "__main__":
    unittest.main()
