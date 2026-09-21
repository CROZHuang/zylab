"""managed 路径（`Agent._run_managed`）的上下文与错误处理测试。

**为什么单独写这一组。** `core/agent.py` 并存两套 agent 循环：
`run()` 在没有 controller 时跑自己的 653 行循环，有 controller 时委托给
`_run_managed()`（1,073 行、127 个分支）。交互式会话走的是后者，
而实测测试调用点是 90 : 22 —— **更复杂、日常在用的那条覆盖反而少 4 倍**。

本文件补的是 managed 路径里完全没有断言的几项：撞墙学习（`ctx_learned`）、
偶发故障与「输入太长」的区分、免费测量（`note_context_ok`）、
以及 journal 事件。这些在 `run()` 那条路径有测试
（`tests/test_ctx_limits.py::RejectionRecovery`），managed 这条没有。
两边是各写一遍的实现（同一个补救在一边叫 `force_compact()`、
另一边叫 `compact_managed(force=True)`），所以不能假设一边过了另一边也过。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import agent as A, client, controller as C, models as M
from test_agent_loop import mk


TOO_LONG = "HTTP 400: This model's maximum context length is 131072 tokens"


class ManagedContextBase(unittest.TestCase):
    def start(self, text="go"):
        journal = C.MemoryJournal()
        ctrl = C.SessionController("t", journal)
        action = ctrl.submit(text, C.QueueMode.NEXT_TURN)
        self.assertEqual(action.kind, C.ActionKind.START_TURN)
        return journal, ctrl

    def agent_with_history(self, turns=8):
        """造一个足够长的会话，让强制压缩有东西可压。"""
        ag = mk()
        for i in range(turns):
            ag.messages.append({"role": "user", "content": f"u{i}"})
            ag.messages.append({"role": "assistant", "content": f"a{i}"})
        return ag


def is_summary_request(kwargs, args):
    """区分「压缩摘要请求」和「正常轮次请求」。

    managed 路径的强制压缩**复用同一个 stream_factory** 去生成摘要，
    所以撞墙恢复一共会发三次请求：失败的轮次、摘要、重试的轮次。
    只数原始次数会把摘要误算成重试 —— 这一点和 `run()` 那条路径不同，
    那边的测试把 `force_compact` 整个 mock 掉了，看不到这次摘要请求。
    """
    if kwargs.get("max_tokens") == 2000:
        return True
    msgs = kwargs.get("messages") or (args[1] if len(args) > 1 else None)
    if msgs and len(msgs) == 1:
        return "交接摘要" in str(msgs[0].get("content", ""))
    return False


class ContextLimitRecovery(ManagedContextBase):
    """撞到「输入太长」时：学到真值 → 强制压缩 → 重试，而不是把这轮报废。"""

    def test_learns_limit_compacts_and_retries(self):
        turns, summaries = [], []

        def provider(*args, **kwargs):
            if is_summary_request(kwargs, args):
                summaries.append(1)
                yield {"t": "text", "v": "## objective\nunknown\n## constraints\n- none\n## decisions\n- none\n## files_changed\n- none\n## evidence\n- none\n## pending\n- none\n## risks\n- none"}
                yield {"t": "done", "reason": "stop", "usage": {}}
                return
            turns.append(1)
            if len(turns) == 1:
                raise client.APIError(TOO_LONG, kind="invalid_request")
            yield {"t": "text", "v": "ok"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        ag = self.agent_with_history()
        journal, ctrl = self.start()
        with mock.patch.object(M, "note_context_reject", return_value=131072):
            events = list(ag.run("go", controller=ctrl,
                                 stream_factory=provider))

        kinds = [e["t"] for e in events]
        self.assertIn("ctx_learned", kinds,
                      "managed 路径撞墙后没有发出 ctx_learned")
        self.assertNotIn("error", kinds, "撞墙不该让这一轮报废")
        self.assertEqual(len(turns), 2, "轮次请求应为「失败一次 + 重试一次」")
        self.assertEqual(len(summaries), 1, "应该强制压缩一次")
        self.assertEqual(ag.ctx_limit, 131072)
        self.assertEqual(ag.compact_at, A.compact_threshold(131072))

    def test_limit_learning_is_journaled(self):
        """journal 必须留痕，否则事后无法解释「上限怎么变的」。"""
        calls = {"n": 0}

        def provider(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise client.APIError(TOO_LONG, kind="invalid_request")
            yield {"t": "done", "reason": "stop", "usage": {}}

        ag = self.agent_with_history()
        journal, ctrl = self.start()
        with mock.patch.object(M, "note_context_reject", return_value=131072):
            list(ag.run("go", controller=ctrl, stream_factory=provider))

        kinds = [e["kind"] for e in journal.events]
        self.assertIn("context_limit_learned", kinds)

    def test_gives_up_after_one_retry_instead_of_looping(self):
        """一直撞就得认输 —— 否则是无限循环烧钱。

        provider 全程抛错，所以摘要请求也会失败；此时压缩必须降级而不是崩，
        且轮次请求总数只能是 2（原始一次 + 重试一次）。
        """
        turns = []

        def provider(*args, **kwargs):
            if not is_summary_request(kwargs, args):
                turns.append(1)
            raise client.APIError(TOO_LONG, kind="invalid_request")
            yield  # pragma: no cover

        ag = self.agent_with_history()
        journal, ctrl = self.start()
        with mock.patch.object(M, "note_context_reject", return_value=131072):
            events = list(ag.run("go", controller=ctrl,
                                 stream_factory=provider))

        self.assertIn("error", [e["t"] for e in events])
        self.assertEqual(len(turns), 2, "只该重试一次")


class CompactionFallsBackToAnotherModel(ManagedContextBase):
    """轮次内的压缩：主模型写不了摘要时，必须真的轮到「换一家来写」。

    2026-09-20 实测：阶梯是 关思考 → 加大额度 → 换模型，循环对「这一步不适用」用的是
    break，于是主模型 503 时「加大额度」不适用 → 收工，换模型永远走不到。交互式会话走的
    正是这条路径（09-07 在上下文天花板上烧掉 84.9M token 的那次也是）。
    """

    FALLBACK = {"gateway": "deepinfer", "model": "glm-5.3", "seat": "GLM"}

    def run_turn(self, first_summary):
        turns, summaries = [], []

        def provider(*args, **kwargs):
            if is_summary_request(kwargs, args):
                summaries.append((kwargs.get("gateway"), args[0]))
                if len(summaries) == 1:
                    if isinstance(first_summary, Exception):
                        raise first_summary
                    yield from first_summary
                    return
                yield {"t": "text", "v": "## objective\nunknown\n## constraints\n- none\n## decisions\n- none\n## files_changed\n- none\n## evidence\n- none\n## pending\n- none\n## risks\n- none"}
                yield {"t": "done", "reason": "stop", "usage": {}}
                return
            turns.append(1)
            if len(turns) == 1:
                raise client.APIError(TOO_LONG, kind="invalid_request")
            yield {"t": "text", "v": "ok"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        ag = self.agent_with_history()
        journal, ctrl = self.start()
        with mock.patch.object(M, "note_context_reject", return_value=131072), \
             mock.patch.object(ag, "compaction_fallback_route",
                               return_value=dict(self.FALLBACK)):
            events = list(ag.run("go", controller=ctrl, stream_factory=provider))
        return ag, events, turns, summaries

    def assert_rescued(self, ag, events, turns, summaries):
        self.assertEqual(summaries, [("test", "m"), ("deepinfer", "glm-5.3")])
        self.assertEqual((ag.context_summary["gateway"], ag.context_summary["model"]),
                         ("deepinfer", "glm-5.3"))
        self.assertNotIn("error", [e["t"] for e in events], "压缩被救回，这一轮不该报废")
        self.assertEqual(len(turns), 2)
        notices = [e["v"] for e in events if e["t"] == "route_warning"]
        self.assertTrue(any("改用 fallback:GLM" in note for note in notices), notices)

    def test_a_provider_error_reaches_the_other_model(self):
        down = client.APIError("HTTP 503 service_error", kind="transient_http",
                               status=503)
        self.assert_rescued(*self.run_turn(down))

    def test_a_truly_empty_summary_reaches_the_other_model(self):
        empty = [{"t": "text", "v": "  "},
                 {"t": "done", "reason": "stop", "usage": {}}]
        self.assert_rescued(*self.run_turn(empty))


class DeepCompactionReachesTheManagedLoopToo(ManagedContextBase):
    """压缩链的「续压」标志必须两条循环都带上。

    `run()` 与 `_run_managed()` 是各写一遍的两套主循环，压缩链也各有一份。09-20 的教训正是
    这个形状：降级阶梯的 `break` → `continue` 在两边各错了一遍。交互式会话走的是 managed 这条，
    所以深度压缩（压到阈值一半以下）在这里必须同样生效。
    """

    def test_the_second_pass_of_a_managed_chain_is_a_deep_pass(self):
        seen = []
        real_plan = A.Agent._compaction_plan

        def spy(agent, force=False, preview=None, *, deep=False):
            seen.append({"force": force, "deep": deep})
            return real_plan(agent, force=force, preview=preview, deep=deep)

        def provider(*args, **kwargs):
            if is_summary_request(kwargs, args):
                yield {"t": "text", "v": "## objective\nunknown\n## constraints\n- none\n"
                                         "## decisions\n- none\n## files_changed\n- none\n"
                                         "## evidence\n- none\n## pending\n- none\n## risks\n- none"}
                yield {"t": "done", "reason": "stop", "usage": {}}
                return
            yield {"t": "text", "v": "ok"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        ag = self.agent_with_history(turns=400)
        ag.compact_at, ag.ctx_limit = 6_000, 40_000
        journal, ctrl = self.start()
        with mock.patch.object(A.Agent, "_compaction_plan", spy), \
             mock.patch.object(A, "COMPACT_SOURCE_TOKENS", 2_000):
            list(ag.run("go", controller=ctrl, stream_factory=provider))

        self.assertGreaterEqual(len(seen), 2, seen)
        self.assertFalse(seen[0]["deep"], "第一段不是续压")
        self.assertTrue(all(call["deep"] for call in seen[1:]),
                        f"managed 这条循环没把续压标志传下去：{seen}")


class TransientIsNotALimit(ManagedContextBase):
    """网络抖动绝不能被当成上下文上限 —— 这个 bug 真实发生过一次。

    早先 `_ctx_fits` 把任何失败都当作「装不下」，一次 5xx 就把 1M 的模型
    永久记成「连 32k 都不收」。managed 路径必须同样只认明确的「太长」。
    """

    def _run_with(self, message, kind="server_error"):
        def provider(*args, **kwargs):
            raise client.APIError(message, kind=kind)
            yield  # pragma: no cover

        ag = self.agent_with_history()
        before = ag.ctx_limit
        journal, ctrl = self.start()
        with mock.patch.object(M, "note_context_reject") as reject:
            events = list(ag.run("go", controller=ctrl,
                                 stream_factory=provider))
        return ag, before, events, reject, journal

    def test_5xx_does_not_change_limit_or_record_a_reject(self):
        ag, before, events, reject, journal = self._run_with(
            "HTTP 503: service temporarily unavailable")
        self.assertIn("error", [e["t"] for e in events])
        self.assertNotIn("ctx_learned", [e["t"] for e in events])
        self.assertEqual(ag.ctx_limit, before, "偶发故障改了上限 = 旧 bug 复发")
        reject.assert_not_called()
        self.assertNotIn("context_limit_learned",
                         [e["kind"] for e in journal.events])

    def test_timeout_does_not_change_limit(self):
        ag, before, events, reject, _ = self._run_with(
            "连接失败: timed out", kind="timeout")
        self.assertEqual(ag.ctx_limit, before)
        reject.assert_not_called()


class FreeMeasurement(ManagedContextBase):
    """每次成功调用都是一次零成本测量：prompt_tokens 是白送的已知下界。"""

    def test_prompt_tokens_recorded_as_known_floor(self):
        def provider(*args, **kwargs):
            yield {"t": "text", "v": "ok"}
            yield {"t": "done", "reason": "stop",
                   "usage": {"prompt_tokens": 4321,
                             "completion_tokens": 7,
                             "total_tokens": 4328}}

        ag = mk()
        journal, ctrl = self.start()
        with mock.patch.object(M, "note_context_ok") as ok:
            list(ag.run("go", controller=ctrl, stream_factory=provider))
        ok.assert_called()
        self.assertEqual(ok.call_args[0][2], 4321)

    def test_only_new_highs_are_persisted(self):
        """只在刷新纪录时落盘，否则正常使用会不停写文件。"""
        seq = [{"prompt_tokens": 5000}, {"prompt_tokens": 100}]
        state = {"i": 0}

        def provider(*args, **kwargs):
            usage = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            yield {"t": "done", "reason": "stop", "usage": usage}

        ag = mk()
        with mock.patch.object(M, "note_context_ok") as ok:
            journal, ctrl = self.start()
            list(ag.run("go", controller=ctrl, stream_factory=provider))
            journal2, ctrl2 = self.start("again")
            list(ag.run("again", controller=ctrl2, stream_factory=provider))
        recorded = [c[0][2] for c in ok.call_args_list]
        self.assertEqual(recorded, [5000],
                         f"第二轮更小的值不该再写一次：{recorded}")


if __name__ == "__main__":
    unittest.main()
