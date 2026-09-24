"""/v1/responses 通道：翻译请求、解析流式事件、何时切过去、推理强度。

为什么要有这条通道（2026-09-24 实测 Boyue）：gpt-6-astra 在 chat/completions 上带工具
必须关推理、而它不接受关推理；gpt-6-sol 在 chat 上能跑，但推理是关着的。两者在
/v1/responses 上都能同时带工具和推理。

夹具 `tests/fixtures/responses/*.sse` 是当天从 Boyue 抓的**原样**流式返回（不是照解析器
的想象写的），解析器必须认得真实网关的写法。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import client, models

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "responses"
ROUTE = client.GatewayRoute(
    "deepinfer", "https://synthetic.invalid/v1", ("DEEPINFER_API_KEY",))
_patches = []


def setUpModule():
    # 与 test_client 同一套隔离：虚构 key、合成 https 端点、临时能力表、不写 metrics。
    tmp = tempfile.TemporaryDirectory()
    _patches.append(tmp)
    for patch in (
            mock.patch.dict(os.environ, {"DEEPINFER_API_KEY": "fictional-test-key"}),
            mock.patch.dict(client.GATEWAYS["deepinfer"],
                            {"base": "https://synthetic.invalid/v1"}),
            mock.patch.object(models, "CACHE", Path(tmp.name) / "models.json"),
            mock.patch.object(models, "SEED", Path(tmp.name) / "no-seed.json"),
            mock.patch.object(models, "_SEED_CACHE", None),
            mock.patch.object(client, "_default_metrics_facade", return_value=None),
            mock.patch.object(models, "_record_probe_health")):
        patch.start()
        _patches.append(patch)
    # 不传网关时 stream_chat 用的是 import 那一刻算好的 BASE：切一次让它重算。
    global _gateway_previous
    _gateway_previous = client.GATEWAY
    client.set_gateway("deepinfer")


_gateway_previous = None


def tearDownModule():
    while _patches:
        item = _patches.pop()
        (item.cleanup if isinstance(item, tempfile.TemporaryDirectory)
         else item.stop)()
    if _gateway_previous is not None:
        client.set_gateway(_gateway_previous)


class FakeResponse:
    def __init__(self, lines):
        self._lines = [line.encode() if isinstance(line, str) else line
                       for line in lines]

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fixture_lines(name):
    return (FIXTURES / f"{name}.sse").read_text(encoding="utf-8").splitlines(True)


def fixture_error(name):
    return (FIXTURES / f"{name}.sse").read_text(encoding="utf-8").strip()


def run_responses(lines, **kwargs):
    with mock.patch.object(client._OPENER, "open",
                           return_value=FakeResponse(lines)) as opener:
        events = list(client._stream_once_responses(
            "gpt-6-sol", [{"role": "user", "content": "x"}], route=ROUTE, **kwargs))
    return events, json.loads(opener.call_args.args[0].data)


class ParsesTheRealGateway(unittest.TestCase):
    def test_a_text_reply(self):
        events, _ = run_responses(fixture_lines("text_sol"))
        self.assertEqual("".join(e["v"] for e in events if e["t"] == "text"), "你好")
        done = events[-1]
        self.assertEqual(done["t"], "done")
        self.assertEqual(done["reason"], "stop")
        self.assertEqual(done["usage"]["prompt_tokens"], 24)
        self.assertEqual(done["usage"]["completion_tokens"], 5)
        self.assertEqual(done["effort"], "medium", "没指定强度时网关回报的就是默认")

    def test_a_tool_call(self):
        events, _ = run_responses(fixture_lines("tool_sol"))
        tools = [e for e in events if e["t"] == "tool"]
        self.assertEqual(len(tools), 1)
        call = tools[0]["v"][0]
        self.assertEqual(call["id"], "call_YXPWp9LG4PgcidmNqY3ZXUHc")
        self.assertEqual(call["function"]["name"], "get_file")
        self.assertEqual(json.loads(call["function"]["arguments"]),
                         {"path": "/tmp/a.txt"})
        self.assertEqual(events[-1]["reason"], "tool_calls")

    def test_the_turn_after_a_tool_result(self):
        """第二轮（带 function_call + function_call_output）真实网关照常回答。"""
        events, _ = run_responses(fixture_lines("second_turn_sol"))
        self.assertTrue("".join(e["v"] for e in events if e["t"] == "text"))
        self.assertEqual(events[-1]["reason"], "stop")

    def test_usage_feeds_the_existing_cache_accounting(self):
        events, _ = run_responses(fixture_lines("text_sol"))
        read, _write, reported = client.normalize_cache(events[-1]["usage"])
        self.assertEqual(read, 0)
        self.assertTrue(reported, "Boyue 如实上报了 cached_tokens")

    def test_a_stream_that_never_completes_is_a_retryable_error(self):
        lines = [line for line in fixture_lines("text_sol")
                 if "response.completed" not in line]
        with self.assertRaises(client.APIError) as caught:
            run_responses(lines)
        self.assertEqual(caught.exception.kind, "stream_eof")
        self.assertTrue(caught.exception.retryable)

    def test_a_failed_response_is_an_api_error(self):
        lines = ['data: {"type":"response.failed","response":{"error":'
                 '{"code":"server_error","message":"boom"}}}\n']
        with self.assertRaises(client.APIError) as caught:
            run_responses(lines)
        self.assertIn("boom", str(caught.exception))
        self.assertTrue(caught.exception.retryable)


class TranslatesTheHistory(unittest.TestCase):
    TOOLS = [{"type": "function", "function": {
        "name": "get_file", "description": "读取文件内容",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"]}}}]

    def test_a_tool_loop_history_becomes_items(self):
        messages = [
            {"role": "system", "content": "你是 zylab"},
            {"role": "user", "content": "读 a.txt"},
            {"role": "assistant", "content": "好的", "tool_calls": [{
                "id": "toolu_bdrk_1", "type": "function",
                "function": {"name": "get_file", "arguments": '{"path": "a.txt"}'}}]},
            {"role": "tool", "tool_call_id": "toolu_bdrk_1", "content": "hello"},
            {"role": "system", "content": "（上下文已压缩）"},
        ]
        instructions, items = client._responses_input(messages)
        self.assertEqual(instructions, "你是 zylab")
        self.assertEqual(items, [
            {"role": "user", "content": "读 a.txt"},
            {"role": "assistant", "content": "好的"},
            {"type": "function_call", "call_id": "toolu_bdrk_1",
             "name": "get_file", "arguments": '{"path": "a.txt"}'},
            {"type": "function_call_output", "call_id": "toolu_bdrk_1",
             "output": "hello"},
            {"role": "developer", "content": "（上下文已压缩）"},
        ])

    def test_images_become_input_images(self):
        _, items = client._responses_input([{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}])
        self.assertEqual(items[0]["content"], [
            {"type": "input_text", "text": "看图"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}])

    def test_the_request_body(self):
        _, body = run_responses(fixture_lines("text_sol"), tools=self.TOOLS,
                                max_tokens=321, effort="high")
        self.assertIs(body["stream"], True)
        self.assertIs(body["store"], False, "不让网关那边存对话")
        self.assertEqual(body["max_output_tokens"], 321)
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertNotIn("temperature", body)
        self.assertEqual(body["tools"], [{
            "type": "function", "name": "get_file", "description": "读取文件内容",
            "parameters": self.TOOLS[0]["function"]["parameters"], "strict": False}])

    def test_no_effort_means_the_model_default(self):
        _, body = run_responses(fixture_lines("text_sol"))
        self.assertNotIn("reasoning", body)


class SwitchingAndEffort(unittest.TestCase):
    """什么时候走 Responses、推理强度怎么落地 —— 走真实的 stream_chat。"""

    REJECT_CHAT = ('HTTP 400: {"error":{"message":"Function tools with reasoning_effort '
                   'are not supported for gpt-6-sol in /v1/chat/completions. To use '
                   'function tools, use /v1/responses or set reasoning_effort to '
                   '\'none\'."}}')

    def setUp(self):
        client._ADAPTATIONS.clear()
        self.addCleanup(client._ADAPTATIONS.clear)

    def stream(self, model, handler, **kwargs):
        calls = []

        def fake_open(req, timeout=None):
            body = json.loads(req.data)
            endpoint = req.full_url.rsplit("/", 1)[-1]
            calls.append((endpoint, body))
            return handler(endpoint, body)

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            events = list(client.stream_chat(
                model, [{"role": "user", "content": "x"}], retries=1, **kwargs))
        return events, calls

    def test_the_gateway_saying_so_switches_the_model_to_responses(self):
        def handler(endpoint, body):
            if endpoint == "completions":
                raise client.APIError(self.REJECT_CHAT)
            return FakeResponse(fixture_lines("tool_sol"))

        events, calls = self.stream("switch-me", handler, temperature=None)
        self.assertEqual([endpoint for endpoint, _ in calls], ["completions", "responses"])
        self.assertTrue([e for e in events if e["t"] == "tool"])
        self.assertEqual(models.get("deepinfer", "switch-me").get("api"), "responses")
        _, again = self.stream("switch-me", handler, temperature=None)
        self.assertEqual([endpoint for endpoint, _ in again], ["responses"],
                         "记住了：下一次直接走 Responses")

    def test_temperature_rejection_is_handled_on_responses_too(self):
        """真实原话：「Unsupported parameter: 'temperature' is not supported with this model.」"""
        models.update_record("deepinfer", "no-temp", lambda rec: rec.update(
            gateway="deepinfer", id="no-temp", api="responses"))

        def handler(endpoint, body):
            if "temperature" in body:
                raise client.APIError("HTTP 400: " + fixture_error("temperature_sol"))
            return FakeResponse(fixture_lines("text_sol"))

        events, calls = self.stream("no-temp", handler, temperature=0.3)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("temperature", calls[1][1])
        self.assertEqual("".join(e["v"] for e in events if e["t"] == "text"), "你好")

    def test_an_effort_the_model_rejects_falls_back_to_its_default(self):
        """真实原话：「Unsupported value: 'none' is not supported with the
        'gpt-6-astra-…' model. Supported values are: …」"""
        models.update_record("deepinfer", "astra-like", lambda rec: rec.update(
            gateway="deepinfer", id="astra-like", api="responses"))

        def handler(endpoint, body):
            if body.get("reasoning"):
                raise client.APIError("HTTP 400: " + fixture_error("effort_none_astra"))
            return FakeResponse(fixture_lines("text_sol"))

        warnings = []
        events, calls = self.stream("astra-like", handler, temperature=None,
                                    effort="none", route_warning=warnings.append)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("reasoning", calls[1][1])
        self.assertTrue(any("推理强度" in w for w in warnings), "用户该知道没按他选的跑")

    def test_effort_is_never_sent_on_chat_completions(self):
        """chat 上的 reasoning_effort 实测有害（README）：用户选了强度也不往 chat 发。"""
        def handler(endpoint, body):
            return FakeResponse([
                'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n',
                "data: [DONE]\n"])

        _, calls = self.stream("chat-model", handler, temperature=None, effort="high")
        self.assertEqual(calls[0][0], "completions")
        self.assertNotIn("reasoning", calls[0][1])
        self.assertNotIn("reasoning_effort", calls[0][1])


class EffortOptionsComeFromTheGateway(unittest.TestCase):
    def setUp(self):
        client._ADAPTATIONS.clear()
        self.addCleanup(client._ADAPTATIONS.clear)

    def test_the_supported_values_are_read_from_the_rejection(self):
        self.assertEqual(
            models.parse_supported_values(fixture_error("effort_none_astra")),
            ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(models.parse_supported_values("no list here"), [])

    ENUM = ("HTTP 400: Invalid value: 'zylab-effort-probe'. Supported values are: "
            "'none', 'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'.")

    def discover(self, model, rejects):
        """rejects: {强度: 这个模型拒绝它时报出的清单}；不在里面的强度就「收下」。"""
        models.update_record("deepinfer", model, lambda rec: rec.update(
            gateway="deepinfer", id=model, api="responses"))
        sent = []

        def fake_open(req, timeout=None):
            value = json.loads(req.data)["reasoning"]["effort"]
            sent.append(value)
            if value == "zylab-effort-probe":
                raise client.APIError(self.ENUM)
            if value in rejects:
                listed = ", ".join(f"'{v}'" for v in rejects[value])
                raise client.APIError(
                    f"HTTP 400: Unsupported value: '{value}' is not supported with "
                    f"the '{model}-2026-09-03' model. Supported values are: {listed}.")
            return FakeResponse(fixture_lines("text_sol"))

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            options = models.discover_effort_options("deepinfer", model)
        return options, sent

    def test_the_first_answer_is_only_the_interfaces_enum(self):
        """真实现场：不存在的值换来的是**通用枚举**（none … max），可 astra 并不收 none。
        只问一步，astra 的菜单里就会有一个选了必挂的 none。"""
        options, sent = self.discover(
            "astra-like", {"none": ["low", "medium", "high", "xhigh", "max"]})
        self.assertEqual(options, ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(sent, ["zylab-effort-probe", "none"])
        self.assertEqual(
            models.get("deepinfer", "astra-like").get("effort_options"), options)

    def test_values_are_tried_from_both_ends_until_one_is_rejected(self):
        options, sent = self.discover(
            "sol-like", {"max": ["none", "minimal", "low", "medium", "high", "xhigh"]})
        self.assertEqual(sent, ["zylab-effort-probe", "none", "max"])
        self.assertEqual(options, ["none", "minimal", "low", "medium", "high", "xhigh"])

    def test_a_model_that_takes_every_value_gets_the_whole_enum(self):
        options, sent = self.discover("takes-all", {})
        self.assertEqual(options,
                         ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
        self.assertEqual(len(sent), 8)

    def test_a_rejected_choice_corrects_the_recorded_options(self):
        """用户选的强度被模型拒了：拒绝原话里的清单顺手写回能力表。"""
        models.update_record("deepinfer", "self-heal", lambda rec: rec.update(
            gateway="deepinfer", id="self-heal", api="responses",
            effort_options=["none", "low", "medium"]))

        def fake_open(req, timeout=None):
            if json.loads(req.data).get("reasoning"):
                raise client.APIError("HTTP 400: " + fixture_error("effort_none_astra"))
            return FakeResponse(fixture_lines("text_sol"))

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            list(client.stream_chat("self-heal", [{"role": "user", "content": "x"}],
                                    temperature=None, effort="none", retries=1))
        self.assertEqual(models.get("deepinfer", "self-heal").get("effort_options"),
                         ["low", "medium", "high", "xhigh", "max"])

    def test_a_probe_of_a_responses_model_learns_its_default_and_options(self):
        def fake_open(req, timeout=None):
            body = json.loads(req.data)
            if req.full_url.endswith("/chat/completions"):
                raise client.APIError(SwitchingAndEffort.REJECT_CHAT)
            if "temperature" in body:
                raise client.APIError("HTTP 400: " + fixture_error("temperature_sol"))
            if body.get("reasoning"):
                raise client.APIError("HTTP 400: " + fixture_error("effort_none_astra"))
            return FakeResponse(fixture_lines("tool_sol"))

        with mock.patch.object(client._OPENER, "open", side_effect=fake_open):
            record = models.probe("deepinfer", "gpt-6-probe")
        self.assertEqual(record["status"], "ok")
        self.assertIs(record["supports_tools"], True)
        self.assertEqual(record["api"], "responses")
        self.assertEqual(record["effort_default"], "medium")
        self.assertEqual(record["effort_options"],
                         ["low", "medium", "high", "xhigh", "max"])


if __name__ == "__main__":
    unittest.main()
