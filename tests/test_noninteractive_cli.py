"""Noninteractive CLI outcome and exit-code regression tests."""
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zylab as CLI


class _QuietSpinner:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _QuietWatcher:
    def __init__(self):
        self.event = object()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PrintModeExitCodeTests(unittest.TestCase):
    @staticmethod
    def run_turn(event):
        session = object.__new__(CLI.Session)
        agent = SimpleNamespace(
            run=mock.Mock(return_value=iter([event])),
            compact_at=100_000,
            ctx_known=True,
            tokens_in=0,
            tokens_out=0,
            model="synthetic-model",
            gateway="deepinfer",
        )
        session.ag = agent
        session.plan_mode = False
        session.last_ctx = None
        session.allowed_tool_names = lambda: ()
        session.heartbeat_lease = lambda: None
        with (
                mock.patch.object(CLI, "Spinner", _QuietSpinner),
                mock.patch.object(CLI.tui, "EscWatcher", _QuietWatcher),
                mock.patch.object(CLI, "status_line", return_value="status"),
                mock.patch.object(sys, "stdout", io.StringIO())):
            return CLI.Session.turn(session, "hello")

    def test_provider_error_events_are_nonzero_and_preserve_kind(self):
        for kind in (
                "authentication", "rate_limit", "transient_http",
                "timeout", "insecure_transport"):
            with self.subTest(kind=kind):
                outcome = self.run_turn({
                    "t": "error", "v": "synthetic provider failure",
                    "kind": kind,
                })
                self.assertEqual(outcome.exit_code, 1)
                self.assertEqual(outcome.status, "error")
                self.assertEqual(outcome.error_kind, kind)

    def test_success_and_interrupt_have_stable_exit_codes(self):
        self.assertEqual(self.run_turn({"t": "end"}).exit_code, 0)
        self.assertEqual(
            self.run_turn({"t": "interrupted"}).exit_code, 130)

    def test_partial_terminal_event_is_visible_and_nonzero(self):
        session = object.__new__(CLI.Session)
        agent = SimpleNamespace(
            run=mock.Mock(return_value=iter([{
                "t": "end",
                "kind": "max_turns",
                "status": "partial",
                "reason": "达到最大轮数 40",
                "message": "[zylab] 工具循环达到最大轮数 40；尚未生成最终总结。",
            }])),
            compact_at=100_000,
            ctx_known=True,
            tokens_in=0,
            tokens_out=0,
            model="synthetic-model",
            gateway="deepinfer",
        )
        session.ag = agent
        session.plan_mode = False
        session.last_ctx = None
        session.allowed_tool_names = lambda: ()
        session.heartbeat_lease = lambda: None
        output = io.StringIO()
        with (
                mock.patch.object(CLI, "Spinner", _QuietSpinner),
                mock.patch.object(CLI.tui, "EscWatcher", _QuietWatcher),
                mock.patch.object(CLI, "status_line", return_value="status"),
                mock.patch.object(sys, "stdout", output)):
            outcome = CLI.Session.turn(session, "hello")

        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error_kind, "max_turns")
        self.assertEqual(outcome.exit_code, 1)
        self.assertIn("达到最大轮数", output.getvalue())
        self.assertIn("尚未生成最终总结", output.getvalue())

    def test_print_mode_returns_the_turn_failure_code(self):
        agent = mock.MagicMock()
        session = mock.MagicMock()
        session.turn.return_value = SimpleNamespace(exit_code=7)
        cfg = dict(CLI.CFG.DEFAULTS)
        cfg["gateway"] = None
        with (
                mock.patch.object(sys, "argv", ["zylab", "-p", "hello"]),
                mock.patch.object(
                    CLI.CFG, "load", return_value=(cfg, ["built-in"])),
                mock.patch.object(CLI.store, "configure_metrics_policy"),
                mock.patch.object(CLI.store, "ensure_home"),
                mock.patch.object(CLI.client, "configure_transport_policy"),
                mock.patch.object(CLI.A, "Agent", return_value=agent),
                mock.patch.object(CLI, "Session", return_value=session)):
            code = CLI.main()
        self.assertEqual(code, 7)
        session.turn.assert_called_once_with("hello")
        session.save.assert_called_once_with()
        session.close.assert_called_once_with()

    def test_instruction_disable_flags_are_independent(self):
        cases = (
            (["--no-project-md"], (True, False, True)),
            (["--no-user-md"], (False, True, True)),
            (["--no-skills"], (True, True, False)),
            (["--no-md"], (False, False, True)),
            (["--no-user-md", "--no-skills"], (False, True, False)),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                runtime_agent = mock.MagicMock()
                session = mock.MagicMock()
                session.turn.return_value = SimpleNamespace(exit_code=0)
                cfg = dict(CLI.CFG.DEFAULTS)
                cfg["gateway"] = None
                with (
                        mock.patch.object(
                            sys, "argv",
                            ["zylab", "-p", "hello", *flags]),
                        mock.patch.object(
                            CLI.CFG, "load",
                            return_value=(cfg, ["built-in"])),
                        mock.patch.object(
                            CLI.store, "configure_metrics_policy"),
                        mock.patch.object(CLI.store, "ensure_home"),
                        mock.patch.object(
                            CLI.client, "configure_transport_policy"),
                        mock.patch.object(
                            CLI.A, "Agent", return_value=runtime_agent)
                            as agent_factory,
                        mock.patch.object(
                            CLI, "Session", return_value=session)):
                    self.assertEqual(CLI.main(), 0)
                kwargs = agent_factory.call_args.kwargs
                self.assertEqual(
                    (kwargs["load_user_md"],
                     kwargs["load_project_md"],
                     kwargs["load_skills"]),
                    expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
