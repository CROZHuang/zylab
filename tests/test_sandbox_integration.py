"""M6 wiring: final post-hook Bash -> hard guard -> policy -> process spawn."""

import os
import io
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from contextlib import redirect_stdout

import zylab as CLI
from unittest import mock as _mock
from core import sandbox, tools


def capability(available, reason="runtime unavailable"):
    return sandbox.SandboxCapabilities(
        adapter="test-adapter",
        available=available,
        executable="/test/unshare",
        setpriv_executable="/test/setpriv",
        protected_paths_read_only=available,
        command_execution=available,
        network_isolation=available,
        probed_at=123.0,
        reason="" if available else reason,
    )


def prepared_command(root, command="echo ok", *, network_isolated=True):
    return sandbox.PreparedCommand(
        argv=("/sandbox/launcher", "--", command),
        cwd=str(root),
        adapter="test-adapter",
        protected_paths=("/protected/archive",),
        network_isolated=network_isolated,
    )


class PreparedBashTests(unittest.TestCase):
    def test_hard_guard_checks_final_command_before_sandbox(self):
        context = tools.ExecutionContext.capture(hook_config={})
        with mock.patch.object(
                tools, "_prepare_bash_sandbox") as sandbox_prepare:
            with self.assertRaises(tools.Denied):
                tools.prepare(
                    "bash", {"command": "rm /protected/archive/canonical"},
                    context=context)
        sandbox_prepare.assert_not_called()

    def test_sandboxed_plan_is_used_by_managed_task_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = capability(True)
            decision = sandbox.SandboxPolicy("strict").decide(
                cap, interactive=False)
            launch = prepared_command(tmp)
            with mock.patch.object(
                    tools, "_prepare_bash_sandbox",
                    return_value=(decision, cap, launch)):
                prepared = tools.prepare(
                    "bash", {"command": "echo ok", "timeout": 7})

            task = mock.Mock()
            task.run_process.return_value = "ok\n"
            result = tools.run("bash", prepared, task=task)

        task.run_process.assert_called_once_with(
            list(launch.argv), cwd=launch.cwd, timeout=7)
        self.assertTrue(result.startswith(
            "[SANDBOXED:test-adapter][NET ISOLATED]"))
        self.assertTrue(prepared.sandbox_evidence()["protected_paths"])

    def test_network_request_requires_one_call_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = capability(True)
            decision = sandbox.SandboxPolicy("strict").decide(
                cap, interactive=False)
            launch = prepared_command(
                tmp, "curl https://example.invalid",
                network_isolated=False)
            with mock.patch.object(
                    tools, "_prepare_bash_sandbox",
                    return_value=(decision, cap, launch)):
                prepared = tools.prepare("bash", {
                    "command": "curl https://example.invalid",
                    "network": True,
                })

            self.assertTrue(prepared.requires_network_confirmation)
            self.assertFalse(prepared.network_approved)
            self.assertFalse(
                prepared.sandbox_evidence()["network_isolated"])
            self.assertTrue(
                tools.run("bash", prepared).startswith("[已拒绝]"))

            approved = prepared.approve_network()
            self.assertTrue(prepared.requires_network_confirmation)
            self.assertFalse(approved.requires_network_confirmation)
            completed = {
                "returncode": 0, "stdout": "ok\n", "stderr": "",
                "timed_out": False,
            }
            with mock.patch.object(
                    tools, "_run_bash_direct",
                    return_value=completed) as run:
                result = tools.run("bash", approved)

        run.assert_called_once_with(
            list(launch.argv), cwd=launch.cwd, timeout=120)
        self.assertEqual(
            result, "[SANDBOXED:test-adapter][NET OPEN]\nok")
        self.assertTrue(approved.sandbox_evidence()["network_approved"])

    def test_ask_unsandboxed_cannot_run_until_bound_to_one_call(self):
        cap = capability(False)
        decision = sandbox.SandboxPolicy("ask-unsandboxed").decide(
            cap, interactive=True)
        with mock.patch.object(
                tools, "_prepare_bash_sandbox",
                return_value=(decision, cap, None)):
            prepared = tools.prepare("bash", {"command": "echo ok"})

        self.assertTrue(prepared.requires_unsandboxed_confirmation)
        self.assertTrue(tools.run("bash", prepared).startswith("[已拒绝]"))

        approved = prepared.approve_unsandboxed()
        completed = {
            "returncode": 0, "stdout": "ok\n", "stderr": "",
            "timed_out": False,
        }
        with mock.patch.object(
                tools, "_run_bash_direct", return_value=completed) as run:
            result = tools.run("bash", approved)
        run.assert_called_once_with(
            ["bash", "-lc", "echo ok"], cwd=os.getcwd(), timeout=120)
        self.assertEqual(result, "[UNSANDBOXED][NET OPEN]\nok")

    def test_disabled_mode_is_explicitly_unsandboxed(self):
        cap = capability(True)
        decision = sandbox.SandboxPolicy("disabled").decide(
            cap, interactive=False)
        with mock.patch.object(
                tools, "_prepare_bash_sandbox",
                return_value=(decision, cap, None)):
            prepared = tools.prepare("bash", {"command": "true"})
        completed = {
            "returncode": 0, "stdout": "", "stderr": "",
            "timed_out": False,
        }
        with mock.patch.object(
                tools, "_run_bash_direct", return_value=completed):
            result = tools.run("bash", prepared)
        self.assertTrue(result.startswith("[UNSANDBOXED]"))

    def test_prepare_failure_after_available_decision_never_falls_back(self):
        cap = capability(True)
        decision = sandbox.SandboxPolicy("strict").decide(
            cap, interactive=False)
        status = {
            "decision": decision,
            "capabilities": cap,
            "settings": {
                "mode": "strict", "network_isolation": False,
                "protected_paths": ("/protected/archive",),
            },
        }
        adapter = mock.Mock()
        adapter.prepare.side_effect = sandbox.SandboxPrepareError("bad mount")
        with mock.patch.object(
                tools, "sandbox_status", return_value=status), \
                mock.patch.object(
                    tools, "_sandbox_adapter",
                    return_value=(adapter, status["settings"])):
            with self.assertRaisesRegex(tools.Denied, "prepare 失败"):
                tools._prepare_bash_sandbox(
                    "echo MUST_NOT_RUN", cwd=os.getcwd(), cfg={})


class ExplicitUnsandboxedConsentTests(unittest.TestCase):
    @staticmethod
    def session(choice, *, permission="ask", auto=False, always=()):
        sess = CLI.Session.__new__(CLI.Session)
        sess.cfg = {
            "permissions": {"bash": permission},
        }
        sess.auto = auto
        sess.always = set(always)
        sess.renderer = None
        sess.pump = None
        sess.watcher = None
        sess.pick = lambda *_args, **_kwargs: (
            (choice, "selected") if choice else None)
        return sess

    @staticmethod
    def pending():
        cap = capability(False, "user namespace disabled")
        decision = sandbox.SandboxPolicy("ask-unsandboxed").decide(
            cap, interactive=True)
        return tools.PreparedArguments(
            tool_name="bash",
            arguments_json='{"command":"echo ok"}',
            sandbox_decision=decision,
            sandbox_capabilities=cap,
        )

    def test_permission_shortcuts_do_not_bypass_unsandboxed_consent(self):
        cases = (
            ("config allow", {"permission": "allow"}),
            ("auto/yolo", {"auto": True}),
            ("session grant", {"always": {"bash"}}),
        )
        for label, kwargs in cases:
            with self.subTest(path=label):
                sess = self.session("deny", **kwargs)
                with mock.patch.object(
                        CLI.sys.stdin, "isatty", return_value=True), \
                        mock.patch("builtins.print"):
                    result = sess.permission_decision(
                        "bash", self.pending())
                self.assertFalse(result["allowed"])
                self.assertEqual(
                    result["decision"], "unsandboxed_denied")

    def test_legacy_confirm_shortcuts_also_require_one_off_consent(self):
        cases = (
            ("config allow", {"permission": "allow"}),
            ("auto/yolo", {"auto": True}),
            ("session grant", {"always": {"bash"}}),
        )
        for label, kwargs in cases:
            with self.subTest(path=label):
                sess = self.session("deny", **kwargs)
                with mock.patch.object(
                        CLI.sys.stdin, "isatty", return_value=True), \
                        mock.patch("builtins.print"):
                    result = sess.confirm("bash", self.pending())
                self.assertFalse(result["allowed"])
                self.assertEqual(
                    result["decision"], "unsandboxed_denied")

    def test_explicit_once_binds_approval_to_prepared_call(self):
        sess = self.session("once", permission="allow")
        with mock.patch.object(CLI.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.print"):
            result = sess.permission_decision("bash", self.pending())
        self.assertTrue(result["allowed"])
        self.assertFalse(
            result["prepared"].requires_unsandboxed_confirmation)
        self.assertEqual(result["sandbox"]["marker"], "UNSANDBOXED")

    def test_noninteractive_ask_unsandboxed_is_denied(self):
        sess = self.session("once", permission="allow")
        with mock.patch.object(CLI.sys.stdin, "isatty", return_value=False):
            result = sess.permission_decision("bash", self.pending())
        self.assertFalse(result["allowed"])
        self.assertEqual(
            result["decision"], "noninteractive_unsandboxed_deny")


class ExplicitNetworkConsentTests(unittest.TestCase):
    session = staticmethod(ExplicitUnsandboxedConsentTests.session)

    def pending(self):
        cap = capability(True)
        decision = sandbox.SandboxPolicy("strict").decide(
            cap, interactive=False)
        return tools.PreparedArguments(
            tool_name="bash",
            arguments_json=(
                "{\"command\":\"curl https://example.invalid\","
                "\"network\":true}"),
            sandbox_decision=decision,
            sandbox_capabilities=cap,
            sandbox_command=prepared_command(
                os.getcwd(), "curl https://example.invalid",
                network_isolated=False),
            network_requested=True,
            network_approved=False,
        )

    def test_permission_shortcuts_do_not_bypass_network_consent(self):
        cases = (
            ("config allow", {"permission": "allow"}),
            ("auto/yolo", {"auto": True}),
            ("session grant", {"always": {"bash"}}),
        )
        for label, kwargs in cases:
            with self.subTest(path=label):
                sess = self.session("deny", **kwargs)
                with mock.patch.object(
                        CLI.sys.stdin, "isatty", return_value=True), \
                        mock.patch("builtins.print"):
                    result = sess.permission_decision(
                        "bash", self.pending())
                self.assertFalse(result["allowed"])
                self.assertEqual(result["decision"], "network_denied")

    def test_explicit_once_binds_network_to_prepared_call(self):
        pending = self.pending()
        sess = self.session("once", permission="allow")
        with mock.patch.object(CLI.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.print"):
            result = sess.permission_decision("bash", pending)
        self.assertTrue(result["allowed"])
        self.assertTrue(pending.requires_network_confirmation)
        self.assertFalse(
            result["prepared"].requires_network_confirmation)
        self.assertTrue(result["sandbox"]["network_approved"])
        self.assertIn("network_once", result["decision"])

    def test_noninteractive_network_request_is_denied(self):
        sess = self.session("once", permission="allow")
        with mock.patch.object(CLI.sys.stdin, "isatty", return_value=False):
            result = sess.permission_decision("bash", self.pending())
        self.assertFalse(result["allowed"])
        self.assertEqual(
            result["decision"], "noninteractive_network_deny")

    def test_approval_details_distinguish_isolated_and_requested(self):
        requested = CLI._approval_detail_lines("bash", self.pending())
        isolated = tools.PreparedArguments(
            tool_name="bash",
            arguments_json="{\"command\":\"true\"}",
            sandbox_decision=self.pending().sandbox_decision,
            sandbox_capabilities=capability(True),
            sandbox_command=prepared_command(os.getcwd()),
        )
        default = CLI._approval_detail_lines("bash", isolated)
        self.assertIn(
            "network: NET OPEN (separate confirmation required)",
            requested)
        self.assertIn("network: NET ISOLATED", default)


class SandboxVisibilityTests(unittest.TestCase):
    def test_default_and_legacy_network_settings_are_unambiguous(self):
        default = tools._sandbox_settings({})
        legacy_open = tools._sandbox_settings({
            "sandbox": {"network": False},
        })
        self.assertTrue(default["network_isolation"])
        self.assertFalse(legacy_open["network_isolation"])
        self.assertNotIn("network", default)

    def test_footer_keeps_sandbox_marker_next_to_chat_attributes(self):
        ag = SimpleNamespace(
            session_id="session-one", model="m", gateway="g",
            compact_at=100_000)
        sess = SimpleNamespace(
            ag=ag, _title="named chat", last_ctx=25_000,
            plan_mode=False, auto=False, attached_agent_id=None,
            attached_task_id=None,
            _sandbox_status={
                "marker": "SANDBOXED:test-adapter",
                "network_marker": "NET ISOLATED",
            },
        )
        footer = CLI.status_line(sess)
        self.assertIn("named chat", footer)
        self.assertIn("m@g", footer)
        self.assertIn("SANDBOXED:test-adapter", footer)
        self.assertIn("NET ISOLATED", footer)

    def test_status_fails_closed_for_missing_configured_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing-protected-path")
            status = tools.sandbox_status({
                "sandbox": {
                    "mode": "strict",
                    "network_isolation": True,
                    "protected_paths": [missing],
                },
            }, refresh=True)
        if not status["runtime_capabilities"].available:
            self.skipTest(status["runtime_capabilities"].reason)
        self.assertFalse(status["configuration_ready"])
        self.assertEqual(status["marker"], "SANDBOX BLOCKED")
        self.assertEqual(status["network_marker"], "NET BLOCKED")
        self.assertIn("missing-protected-path", status["configuration_error"])

    def test_doctor_reports_real_boundary_scope_and_http_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = capability(True)
            decision = sandbox.SandboxPolicy("strict").decide(
                cap, interactive=False)
            status = {
                "marker": decision.marker,
                "network_marker": "NET ISOLATED",
                "decision": decision,
                "capabilities": cap,
                "runtime_capabilities": cap,
                "configuration_ready": True,
                "configuration_error": "",
                "settings": {
                    "mode": "strict", "network_isolation": True,
                    "protected_paths": ("/protected/archive",),
                },
            }
            metrics = {
                "initialized": True,
                "integrity": ["ok"],
                "foreign_key_violations": [],
                "counts": {
                    "requests": 0, "tool_runs": 0, "model_health": 0,
                },
                "path": str(Path(tmp) / "metrics.sqlite3"),
            }
            sess = SimpleNamespace(
                cfg={}, ag=SimpleNamespace(gateway="deepinfer"))
            output = io.StringIO()
            with mock.patch.object(
                    CLI.tools, "sandbox_status", return_value=status), \
                    mock.patch.object(
                        CLI.client, "route_for", return_value=SimpleNamespace(
                            name="deepinfer",
                            base="http://internal.example/v1")), \
                    mock.patch.object(
                        CLI.store, "list_session_summaries",
                        side_effect=([{"id": "a"}], [])), \
                    mock.patch.object(
                        CLI.store, "shadow_status", return_value={
                            "enabled": False, "active": False,
                            "broken": None,
                        }), \
                    mock.patch.object(
                        CLI.store, "metrics_facade", return_value=SimpleNamespace(
                            diagnostics=lambda read_only: metrics)), \
                    mock.patch.object(CLI.store, "ARTIFACTS", Path(tmp)), \
                    mock.patch.object(CLI.tui, "supported", return_value=True), \
                    redirect_stdout(output):
                CLI.cmd_doctor(sess, "")
        rendered = output.getvalue()
        self.assertIn("SANDBOXED:test-adapter", rendered)
        self.assertIn("mount=True netns=True", rendered)
        self.assertIn("protected enforced", rendered)
        self.assertIn("NET ISOLATED", rendered)
        self.assertIn("HTTP (credential exposure risk)", rendered)
        self.assertIn("1 active / 0 archived", rendered)
        self.assertIn("不代表整个 workspace/filesystem", rendered)


class DirectBashProcessTests(unittest.TestCase):
    @staticmethod
    def process_running(pid):
        try:
            state = Path(f"/proc/{pid}/stat").read_text(
                encoding="utf-8").split()[2]
        except (FileNotFoundError, IndexError, OSError):
            return False
        return state != "Z"

    @classmethod
    def wait_process_stopped(cls, pid, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not cls.process_running(pid):
                return True
            time.sleep(0.01)
        return not cls.process_running(pid)

    @classmethod
    def cleanup_pid(cls, pid):
        if pid is not None and cls.process_running(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_normal_nohup_child_is_cleaned_after_shell_exit(self):
        child_pid = None
        try:
            result = tools._run_bash_direct(
                ["bash", "-lc",
                 "nohup sleep 30 >/dev/null 2>&1 & echo $!"],
                cwd=os.getcwd(), timeout=2)
            self.assertFalse(result["timed_out"])
            self.assertEqual(result["returncode"], 0)
            child_pid = int(result["stdout"].strip())
            self.assertTrue(self.wait_process_stopped(child_pid))
        finally:
            self.cleanup_pid(child_pid)

    def test_timeout_cleans_entire_direct_process_group(self):
        child_pid = None
        try:
            result = tools._run_bash_direct(
                ["bash", "-lc", "sleep 30 & echo $!; wait"],
                cwd=os.getcwd(), timeout=0.2)
            self.assertTrue(result["timed_out"])
            child_pid = int(result["stdout"].strip())
            self.assertTrue(self.wait_process_stopped(child_pid))
        finally:
            self.cleanup_pid(child_pid)

    def test_direct_capture_is_bounded_for_large_output(self):
        payload_size = tools.MAX_OUT * 8
        result = tools._run_bash_direct(
            [sys.executable, "-c",
             f"import sys; sys.stdout.write('x' * {payload_size})"],
            cwd=os.getcwd(), timeout=3)

        output = result["stdout"]
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["returncode"], 0)
        self.assertLessEqual(
            len(output.encode("utf-8")), tools.MAX_OUT * 4 + 512)
        self.assertIn("direct capture 省略", output)
        self.assertTrue(output.startswith("x"))
        self.assertTrue(output.endswith("x"))

    def test_direct_capture_redacts_known_environment_secret(self):
        secret = "m6-direct-secret-value-123456"
        with mock.patch.dict(os.environ, {"M6_API_KEY": secret}):
            result = tools._run_bash_direct(
                [sys.executable, "-c",
                 "import os; print(os.environ['M6_API_KEY'])"],
                cwd=os.getcwd(), timeout=2)

        self.assertEqual(result["returncode"], 0)
        self.assertNotIn(secret, result["stdout"])
        self.assertIn("[REDACTED_SECRET]", result["stdout"])



# 受保护路径默认为空 —— 测试必须自己声明要守的东西，否则会在维护者机器上因为
# 读到用户配置而意外变绿、在别人 clone 下来的仓库里变红。
#
# 只打 tools.PROTECTED（词法守卫），**不设环境变量**：环境变量会同时喂给沙箱层，
# 而 sandbox 要求受保护路径真实存在，合成路径必然不存在，会把沙箱打成 fail-closed。
# PROTECTED 虽是 import 时赋值，但守卫是调用时查模块全局，所以 patch 与 import 顺序无关。
# tools 在函数内 import：不是每个用到守卫的测试模块都在顶层 import 它。
_protected_patches = []


def setUpModule():
    from core import tools as _guarded
    for name, value in (("PROTECTED", ("/protected/archive",)),
                        ("PROTECTED_REMOTES", ("archive:bucket",))):
        patch = _mock.patch.object(_guarded, name, value)
        patch.start()
        _protected_patches.append(patch)


def tearDownModule():
    while _protected_patches:
        _protected_patches.pop().stop()

if __name__ == "__main__":
    unittest.main()
