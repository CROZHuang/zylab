"""上下文上限与压缩阈值：公式、免费学习、撞墙恢复。

这一层的 bug 全是**静默**的 —— 阈值定低了只表现为「模型好像忘性大」，
没有任何报错。所以每条规则都在这里钉死。

背景：早先阈值是 min(180_000, ctx*0.7)。那个 180k 硬上限让 1M 窗口的模型
只用到 18% 就压缩，而状态栏还显示着 1M，用户完全看不出来。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import agent as A, client, models as M
from test_agent_loop import mk


class Threshold(unittest.TestCase):
    def test_big_window_no_longer_capped_at_180k(self):
        """回归：1M 的模型必须真的用到 90 万上下文，而不是 18 万。"""
        self.assertEqual(A.compact_threshold(1_000_000), 936_000)
        self.assertGreater(A.compact_threshold(1_000_000), 900_000)

    def test_reserve_scales_down_for_small_windows(self):
        """固定 64k 余量会吃掉小模型一半窗口，所以要按比例缩。"""
        self.assertEqual(A.compact_threshold(128_000), 108_800)   # 15%
        self.assertEqual(A.compact_threshold(256_000), 217_600)

    def test_always_leaves_headroom_and_never_exceeds_limit(self):
        for n in (8_000, 64_000, 128_000, 200_000, 262_144, 1_048_576):
            ca = A.compact_threshold(n)
            self.assertLess(ca, n, f"{n} 没留余量，压缩永远来不及")
            self.assertGreater(ca, 0)

    def test_monotonic(self):
        vals = [A.compact_threshold(n) for n in
                (32_000, 64_000, 128_000, 256_000, 512_000, 1_000_000)]
        self.assertEqual(vals, sorted(vals))

    def test_override_wins(self):
        self.assertEqual(A.compact_threshold(1_000_000, 300_000), 300_000)

    def test_degenerate_input_does_not_crash(self):
        for bad in (0, -5, None):
            self.assertGreater(A.compact_threshold(bad or 1), 0)


class ErrorParsing(unittest.TestCase):
    def test_extracts_stated_limit(self):
        for msg, want in [
            ("This model's maximum context length is 131072 tokens, "
             "however you requested 200000", 131072),
            ("max_model_len (262144) exceeded", 262144),
            ("maximum context: 128000", 128000),
        ]:
            self.assertEqual(M.parse_limit(msg), want, msg)

    def test_returns_none_when_absent_or_absurd(self):
        self.assertIsNone(M.parse_limit("connection reset by peer"))
        self.assertIsNone(M.parse_limit(""))
        self.assertIsNone(M.parse_limit("maximum context length is 12"))

    def test_only_too_long_counts_as_a_limit_signal(self):
        """网络抖动绝不能被当成上限 —— 这个 bug 真实发生过一次。"""
        self.assertTrue(M.looks_too_long("Input too long"))
        self.assertTrue(M.looks_too_long("exceeds max length"))
        self.assertFalse(M.looks_too_long("502 Bad Gateway"))
        self.assertFalse(M.looks_too_long("timed out"))


class FreeLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "models.json"
        self.patch = mock.patch.object(M, "CACHE", self.p)
        self.patch.start()

    def tearDown(self):
        self.patch.stop(); self.tmp.cleanup()

    def test_seen_ok_is_monotonic(self):
        M.note_context_ok("g", "m", 50_000)
        M.note_context_ok("g", "m", 20_000)          # 更小的不该覆盖
        self.assertEqual(M.get("g", "m")["context_seen_ok"], 50_000)
        M.note_context_ok("g", "m", 90_000)
        self.assertEqual(M.get("g", "m")["context_seen_ok"], 90_000)

    def test_stated_limit_beats_estimate(self):
        lim = M.note_context_reject(
            "g", "m", "maximum context length is 131072 tokens",
            attempted_tokens=40_000)
        self.assertEqual(lim, 131072)
        self.assertEqual(M.get("g", "m")["context_source"], "learned-stated")

    def test_estimate_never_drops_below_proven_floor(self):
        """已经成功收过 200k，就不能因为一次保守估计把上限记成 40k。"""
        M.note_context_ok("g", "m", 200_000)
        lim = M.note_context_reject("g", "m", "input too long",
                                    attempted_tokens=40_000)
        self.assertGreaterEqual(lim, 200_000)

    def test_unparseable_and_no_estimate_writes_nothing(self):
        self.assertIsNone(M.note_context_reject("g", "m", "boom",
                                                attempted_tokens=0))
        self.assertNotIn("context_source", M.get("g", "m"))

    def test_concurrent_cache_updates_preserve_both_models(self):
        start = threading.Barrier(3)

        def record(model, base):
            start.wait()
            for offset in range(20):
                M.note_context_ok("g", model, base + offset)

        workers = [
            threading.Thread(target=record, args=("a", 10_000)),
            threading.Thread(target=record, args=("b", 20_000)),
        ]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(timeout=5)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(M.get("g", "a")["context_seen_ok"], 10_019)
        self.assertEqual(M.get("g", "b")["context_seen_ok"], 20_019)
        self.assertEqual(self.p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.p.parent.glob(".models.json.*.tmp")), [])


class SpecHarvest(unittest.TestCase):
    """HF config.json 的语义陷阱，真实踩过一次。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        import harvest_specs
        self.H = harvest_specs

    def test_yarn_factor_must_not_be_multiplied_again(self):
        """max_position_embeddings 已经是外推后的值。

        实测 DeepSeek-V3.2：original(4096) × factor(40) = 163,840 = max_pe。
        早先这里又乘了一次 factor，写进 6,553,600 —— 那会让压缩永远不触发、
        会话必定撑爆。这是「荒谬大值比没有值更危险」的典型。
        """
        cfg = {"max_position_embeddings": 163840,
               "rope_scaling": {"type": "yarn", "factor": 40,
                                "original_max_position_embeddings": 4096}}
        with mock.patch.object(self.H, "urllib") as u:
            u.request.Request = lambda *a, **k: None
            cm = mock.MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps(cfg).encode()
            u.request.urlopen.return_value = cm
            n, _ = self.H.fetch_ctx("x/y")
        self.assertEqual(n, 163_840)

    def test_absurd_value_is_refused_not_stored(self):
        cfg = {"max_position_embeddings": 6_553_600}
        with mock.patch.object(self.H, "urllib") as u:
            u.request.Request = lambda *a, **k: None
            cm = mock.MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps(cfg).encode()
            u.request.urlopen.return_value = cm
            n, why = self.H.fetch_ctx("x/y")
        self.assertIsNone(n)
        self.assertIn("不可信", why)

    def test_repo_of_strips_gateway_prefixes(self):
        self.assertEqual(self.H.repo_of("Pro/deepseek-ai/DeepSeek-V3"),
                         "deepseek-ai/DeepSeek-V3")
        self.assertEqual(self.H.repo_of("Qwen/Qwen3-8B"), "Qwen/Qwen3-8B")
        self.assertIsNone(self.H.repo_of("glm-5.2"))


class RejectionRecovery(unittest.TestCase):
    """撞到「太长」时：学到真值 → 压缩 → 重试，而不是把这一轮报废。"""

    def _agent(self):
        ag = mk()
        ag.messages += [{"role": "user", "content": "x"},
                        {"role": "assistant", "content": "y"}] * 6
        return ag

    def test_learns_compacts_and_retries(self):
        calls = {"n": 0}

        def flaky(model, messages, tools=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise client.APIError(
                    "HTTP 400: maximum context length is 131072 tokens")
            yield {"t": "text", "v": "ok"}
            yield {"t": "done", "reason": "stop", "usage": {}}

        ag = self._agent()
        with mock.patch.object(client, "stream_chat", flaky), \
             mock.patch.object(M, "note_context_reject", return_value=131072), \
             mock.patch.object(ag, "force_compact", return_value="摘要"), \
             mock.patch.object(ag, "log_usage"):
            evs = [e["t"] for e in ag.run("go")]

        self.assertIn("ctx_learned", evs)
        self.assertNotIn("error", evs, "撞墙不该让这一轮报废")
        self.assertEqual(calls["n"], 2, "应该重试一次")
        self.assertEqual(ag.ctx_limit, 131072)
        self.assertEqual(ag.compact_at, A.compact_threshold(131072))

    def test_gives_up_after_one_retry_instead_of_looping(self):
        """一直撞就得认输 —— 否则是无限循环烧钱。"""
        calls = {"n": 0}

        def always(model, messages, tools=None, **kw):
            calls["n"] += 1
            raise client.APIError("input too long")
            yield  # pragma: no cover

        ag = self._agent()
        with mock.patch.object(client, "stream_chat", always), \
             mock.patch.object(ag, "force_compact", return_value=None), \
             mock.patch.object(ag, "log_usage"):
            evs = [e["t"] for e in ag.run("go")]

        self.assertIn("error", evs)
        self.assertEqual(calls["n"], 2, "只该重试一次")

    def test_transient_error_is_not_mistaken_for_a_limit(self):
        def boom(model, messages, tools=None, **kw):
            raise client.APIError("502 Bad Gateway")
            yield  # pragma: no cover

        ag = self._agent()
        before = ag.ctx_limit
        with mock.patch.object(client, "stream_chat", boom), \
             mock.patch.object(ag, "log_usage"):
            evs = [e["t"] for e in ag.run("go")]
        self.assertIn("error", evs)
        self.assertNotIn("ctx_learned", evs)
        self.assertEqual(ag.ctx_limit, before, "网络故障改了上限 = 旧 bug 复发")


if __name__ == "__main__":
    unittest.main()
