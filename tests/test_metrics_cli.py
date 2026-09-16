"""M5 operator commands backed by the authoritative metrics database."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab as CLI
from core import state


class EmptyTaskManager:
    def list(self):
        return []


class MetricsCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.metrics = state.MetricsFacade(
            Path(self.tmp.name) / "metrics.sqlite3")
        self.metrics_patch = mock.patch.object(
            CLI.store, "metrics_facade", return_value=self.metrics)
        self.metrics_patch.start()
        agent = SimpleNamespace(
            session_id="m5-session",
            compact_at=70_000,
            model="model-x",
            gateway="gateway-x",
        )
        controller = SimpleNamespace(
            state=SimpleNamespace(value="running"),
            queued=[],
            current_request_id="active-request",
            current_turn_id="turn-active",
        )
        self.session = SimpleNamespace(
            ag=agent,
            controller=controller,
            task_manager=EmptyTaskManager(),
            last_ctx=12_345,
            _title="metrics test",
            _session_cwd=self.tmp.name,
            _sandbox_status={
                "marker": "SANDBOXED:test",
                "network_marker": "NET ISOLATED",
            },
        )

    def tearDown(self):
        self.metrics_patch.stop()
        self.metrics.close()
        self.tmp.cleanup()

    @staticmethod
    def capture(function, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            function(*args)
        return output.getvalue()

    def populate(self):
        trace = self.metrics.begin_attempt(
            gateway="gateway-x",
            model="model-x",
            attempt=1,
            session_id="m5-session",
            turn_id="turn-123456",
            request_id="provider-attempt-abcdef",
            raw={"purpose": "chat"},
        )
        for phase in ("started", "headers", "first_delta", "last_delta"):
            trace.mark(phase)
        trace.finalize(
            "ok",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        )
        self.metrics.begin_tool_run({
            "id": "tool-run-abcdef",
            "tool_call_id": "call-1",
            "session_id": "m5-session",
            "turn_id": "turn-123456",
            "request_id": "provider-attempt-abcdef",
            "name": "list_dir",
            "args_redacted": {"path_sha256": "abc", "path_chars": 1},
            "approval_decision": "safe_tool",
            "approval_source": "builtin_policy",
        })
        self.metrics.finalize_tool_run(
            "tool-run-abcdef",
            status="completed",
            exit_code=0,
            stdout_path="/tmp/m5.stdout",
            stderr_path="/tmp/m5.stderr",
        )

    def test_status_and_trace_render_authoritative_rows(self):
        self.populate()

        status_output = self.capture(CLI.cmd_status, self.session, "")
        trace_output = self.capture(
            CLI.cmd_trace, self.session, "turn-123")

        self.assertIn("Status", status_output)
        self.assertIn("metrics test · m5-session", status_output)
        self.assertIn("model-x@gateway-x", status_output)
        self.assertIn("phase=ended", status_output)
        self.assertIn("SANDBOXED:test · NET ISOLATED", status_output)
        self.assertIn("Trace · session m5-session", trace_output)
        self.assertIn("purpose=chat", trace_output)
        self.assertIn("list_dir status=completed", trace_output)
        self.assertIn("request=provider-att", trace_output)
        self.assertIn("exit=0", trace_output)
        self.assertIn("stdout=/tmp/m5.stdout", trace_output)
        self.assertIn("stderr=/tmp/m5.stderr", trace_output)

    def test_usage_separates_measured_and_unknown_attempts(self):
        self.populate()
        failed = self.metrics.begin_attempt(
            gateway="gateway-x",
            model="model-x",
            attempt=2,
            session_id="m5-session",
            turn_id="turn-failed",
            request_id="failed-attempt",
            raw={"purpose": "chat"},
        )
        failed.mark("started")
        failed.finalize(
            "failed",
            error=RuntimeError("synthetic"),
            error_kind="transport",
            retryable=True,
        )

        with mock.patch.object(CLI, "show_usage") as legacy:
            output = self.capture(CLI.cmd_usage, self.session, "")

        legacy.assert_called_once_with(days=None, session=None)
        self.assertIn("调用 2", output)
        self.assertIn("usage measured 1", output)
        self.assertIn("usage unknown 1", output)
        self.assertIn("cache measured 0 calls", output)
        self.assertIn("cache unknown 1 calls", output)
        self.assertIn("ok=1 failed=1", output)
        self.assertIn("不与上表重复相加", output)

    def test_model_health_uses_persisted_health_and_models_is_removed(self):
        self.populate()

        with mock.patch.object(CLI.M, "load", return_value={"models": {}}):
            output = self.capture(
                CLI.cmd_model, self.session, "health model-x")

        self.assertIn("model-x@gateway-x", output)
        self.assertIn("1/1", output)
        for command in ("status", "trace", "usage", "model"):
            self.assertIn(command, CLI.REGISTRY)
        self.assertNotIn("models", CLI.REGISTRY)
        self.assertNotIn("/models", CLI.COMMANDS)

    def test_usage_flag_uses_the_same_m5_view_as_interactive_command(self):
        cases = (
            (["zylab", "--usage"], ""),
            (["zylab", "--usage", "7"], "7"),
            (["zylab", "--usage", "abc123"], "abc123"),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(
                        CLI.CFG, "load",
                        return_value=(CLI.CFG.DEFAULTS, ["built-in"])),
                    mock.patch.object(CLI.store, "configure_metrics_policy"),
                    mock.patch.object(CLI, "cmd_usage") as command,
                ):
                    CLI.main()
                    command.assert_called_once_with(None, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
