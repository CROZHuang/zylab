"""Focused tests for the independent M6 unshare sandbox adapter."""

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from unittest import mock as _mock
from core import sandbox
from tests.platform_support import requires_namespace_sandbox  # noqa: E402


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRuntime:
    def __init__(self, root, *, probe_result=None,
                 network_probe_result=None):
        self.root = Path(root)
        self.calls = []
        self.probe_result = probe_result or Result(
            0, "zylab-unshare-probe-ok\n", ""
        )
        self.network_probe_result = network_probe_result or Result(
            0, "zylab-network-probe-ok\n", ""
        )
        self.paths = {}
        for name in ("unshare", "mount", "setpriv", "sh", "bash"):
            path = self.root / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
            self.paths[name] = str(path)

    def result_for(self, argv):
        return (
            self.network_probe_result
            if "--net" in argv else self.probe_result
        )

    def runner(self, argv, **kwargs):
        self.calls.append((tuple(argv), dict(kwargs)))
        return self.result_for(argv)

    def adapter(self, **kwargs):
        values = {
            "unshare_executable": self.paths["unshare"],
            "mount_executable": self.paths["mount"],
            "setpriv_executable": self.paths["setpriv"],
            "shell_executable": self.paths["sh"],
            "bash_executable": self.paths["bash"],
            "runner": self.runner,
            "clock": lambda: 1234.5,
        }
        values.update(kwargs)
        return sandbox.UnshareSandboxAdapter(**values)


@requires_namespace_sandbox
class CapabilityTests(unittest.TestCase):
    def test_available_requires_readonly_and_command_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(tmp)
            adapter = runtime.adapter()
            capability = adapter.capabilities()

            self.assertTrue(capability.available)
            self.assertTrue(capability.protected_paths_read_only)
            self.assertTrue(capability.command_execution)
            self.assertTrue(capability.network_isolation)
            self.assertEqual(capability.network_probe_returncode, 0)
            self.assertEqual(capability.network_reason, "")
            self.assertEqual(capability.probed_at, 1234.5)
            self.assertEqual(len(runtime.calls), 2)
            base_call, network_call = runtime.calls
            self.assertNotIn("--net", base_call[0])
            self.assertIn("--net", network_call[0])
            for argv, kwargs in runtime.calls:
                self.assertNotIn("/protected/archive", argv)
                self.assertIn("zylab-sandbox-probe-", kwargs["cwd"])

    def test_network_probe_is_independent_from_base_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(
                tmp,
                network_probe_result=Result(
                    1, "", "unshare --net: Operation not permitted"),
            )
            adapter = runtime.adapter()
            capability = adapter.capabilities()

            self.assertTrue(capability.available)
            self.assertTrue(capability.protected_paths_read_only)
            self.assertTrue(capability.command_execution)
            self.assertFalse(capability.network_isolation)
            self.assertEqual(capability.network_probe_returncode, 1)
            self.assertIn("未证明独立 netns", capability.network_reason)
            self.assertEqual(len(runtime.calls), 2)

            prepared = adapter.prepare(
                "echo base-ok", tmp,
                network_rules=sandbox.NetworkRules(False))
            self.assertFalse(prepared.network_isolated)
            with self.assertRaisesRegex(
                    sandbox.SandboxUnavailable, "network namespace"):
                adapter.prepare(
                    "echo must-not-run", tmp,
                    network_rules=sandbox.NetworkRules(True))

    def test_probe_failure_is_unavailable_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(
                tmp, probe_result=Result(1, "", "unshare: denied")
            )
            adapter = runtime.adapter()
            first = adapter.capabilities()
            second = adapter.capabilities()

            self.assertFalse(first.available)
            self.assertEqual(first.probe_returncode, 1)
            self.assertIn("未证明", first.reason)
            self.assertIs(first, second)
            self.assertEqual(len(runtime.calls), 1)

    def test_missing_executable_is_unavailable_without_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(tmp)
            Path(runtime.paths["setpriv"]).chmod(0o600)
            adapter = runtime.adapter()

            capability = adapter.capabilities()
            self.assertFalse(capability.available)
            self.assertIn("setpriv", capability.reason)
            self.assertEqual(runtime.calls, [])

    def test_probe_runner_exception_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(tmp)

            def broken_runner(_argv, **_kwargs):
                raise RuntimeError("launcher panic")

            adapter = runtime.adapter(runner=broken_runner)
            capability = adapter.capabilities()
            self.assertFalse(capability.available)
            self.assertIn("RuntimeError: launcher panic", capability.reason)

    def test_refresh_repeats_probe_and_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(tmp)
            ticks = iter((1.0, 2.0))
            adapter = runtime.adapter(clock=lambda: next(ticks))
            self.assertEqual(adapter.capabilities().probed_at, 1.0)
            self.assertEqual(
                adapter.capabilities(refresh=True).probed_at, 2.0
            )
            self.assertEqual(len(runtime.calls), 4)

    def test_concurrent_first_use_runs_one_capability_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(tmp)
            entered = threading.Event()
            release = threading.Event()

            def runner(argv, **kwargs):
                runtime.calls.append((tuple(argv), dict(kwargs)))
                entered.set()
                release.wait(2)
                return runtime.result_for(argv)

            adapter = runtime.adapter(runner=runner)
            results = []
            threads = [threading.Thread(
                target=lambda: results.append(adapter.capabilities()))
                for _ in range(6)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(1))
            time.sleep(0.02)
            release.set()
            for thread in threads:
                thread.join(2)

            self.assertEqual(len(results), 6)
            self.assertTrue(all(item.available for item in results))
            self.assertTrue(all(item.network_isolation for item in results))
            self.assertEqual(len(runtime.calls), 2)


class PolicyTests(unittest.TestCase):
    @staticmethod
    def capability(available):
        return sandbox.SandboxCapabilities(
            adapter="test",
            available=available,
            executable="/test/unshare",
            setpriv_executable="/test/setpriv",
            protected_paths_read_only=available,
            command_execution=available,
            network_isolation=available,
            probed_at=0.0,
            reason="missing" if not available else "",
        )

    def test_available_is_sandboxed_in_enforcing_modes(self):
        for mode in ("strict", "ask-unsandboxed"):
            with self.subTest(mode=mode):
                decision = sandbox.SandboxPolicy(mode).decide(
                    self.capability(True), interactive=False
                )
                self.assertTrue(decision.allowed)
                self.assertTrue(decision.sandboxed)
                self.assertTrue(decision.protected_paths_enforced)

    def test_disabled_stays_unsandboxed_when_runtime_is_available(self):
        for mode in ("disabled", "off"):
            with self.subTest(mode=mode):
                decision = sandbox.SandboxPolicy(mode).decide(
                    self.capability(True), interactive=False
                )
                self.assertTrue(decision.allowed)
                self.assertFalse(decision.sandboxed)
                self.assertEqual(decision.marker, "UNSANDBOXED")

    def test_strict_unavailable_denies(self):
        decision = sandbox.SandboxPolicy("strict").decide(
            self.capability(False), interactive=True
        )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.requires_confirmation)
        self.assertEqual(decision.marker, "SANDBOX BLOCKED")

    def test_ask_unavailable_prompts_only_interactively(self):
        policy = sandbox.SandboxPolicy("ask-unsandboxed")
        interactive = policy.decide(
            self.capability(False), interactive=True
        )
        noninteractive = policy.decide(
            self.capability(False), interactive=False
        )
        approved = policy.decide(
            self.capability(False), interactive=True,
            unsandboxed_approved=True,
        )

        self.assertFalse(interactive.allowed)
        self.assertTrue(interactive.requires_confirmation)
        self.assertFalse(noninteractive.allowed)
        self.assertFalse(noninteractive.requires_confirmation)
        self.assertTrue(approved.allowed)
        self.assertFalse(approved.sandboxed)
        self.assertEqual(approved.marker, "UNSANDBOXED")

    def test_disabled_unavailable_allows_but_is_explicit(self):
        decision = sandbox.SandboxPolicy("disabled").decide(
            self.capability(False), interactive=False
        )
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.sandboxed)
        self.assertFalse(decision.protected_paths_enforced)
        self.assertEqual(decision.decision, "sandbox-disabled")
        self.assertEqual(decision.marker, "UNSANDBOXED")

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(sandbox.SandboxPrepareError):
            sandbox.SandboxPolicy("magic").decide(
                self.capability(True), interactive=True
            )


@requires_namespace_sandbox
class PrepareAndRunTests(unittest.TestCase):
    def test_prepare_uses_positional_paths_and_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(tmp)
            weird = Path(tmp) / "protected $(touch NO) ; quote'"
            weird.mkdir()
            adapter = runtime.adapter(protected_paths=(str(weird),))
            prepared = adapter.prepare(
                "printf '%s\\n' \"hello; world\"",
                tmp,
                network_rules=sandbox.NetworkRules(True),
            )

            self.assertIn("--net", prepared.argv)
            script_index = prepared.argv.index("-c") + 1
            setup_script = prepared.argv[script_index]
            canonical = os.path.realpath(weird)
            self.assertNotIn(canonical, setup_script)
            self.assertNotIn("touch NO", setup_script)
            self.assertIn(canonical, prepared.argv)
            self.assertEqual(
                prepared.argv[-1], "printf '%s\\n' \"hello; world\""
            )
            self.assertNotIn("--pid", prepared.argv)
            self.assertNotIn("--mount-proc", prepared.argv)
            self.assertIn("--bounding-set=-all", setup_script)
            self.assertIn("--nnp", setup_script)

    def test_prepare_failure_has_no_unsandboxed_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(tmp)
            adapter = runtime.adapter()
            adapter.capabilities()
            calls_before = len(runtime.calls)

            with self.assertRaises(sandbox.SandboxPrepareError):
                adapter.prepare(
                    "echo should-not-run",
                    tmp,
                    sandbox.FilesystemRules((str(Path(tmp) / "missing"),)),
                )
            self.assertEqual(len(runtime.calls), calls_before)

    def test_run_delegates_prepared_argv_to_process_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime(tmp)
            protected = Path(tmp) / "protected"
            protected.mkdir()
            adapter = runtime.adapter(protected_paths=(str(protected),))
            prepared = adapter.prepare("echo ok", tmp)
            seen = {}

            def process_runner(argv, **kwargs):
                seen["argv"] = argv
                seen["kwargs"] = kwargs
                return "RESULT"

            result = adapter.run(
                prepared,
                process_runner=process_runner,
                timeout=7,
                env={"PATH": "/usr/bin"},
            )
            self.assertEqual(result, "RESULT")
            self.assertEqual(tuple(seen["argv"]), prepared.argv)
            self.assertEqual(seen["kwargs"]["cwd"], prepared.cwd)
            self.assertEqual(seen["kwargs"]["timeout"], 7)
            self.assertEqual(seen["kwargs"]["env"], {"PATH": "/usr/bin"})

    def test_explain_violation(self):
        self.assertIn(
            "protected path",
            sandbox.UnshareSandboxAdapter.explain_violation(
                1, "Read-only file system"
            ),
        )
        self.assertIn(
            "mount",
            sandbox.UnshareSandboxAdapter.explain_violation(
                1, "mount: Operation not permitted"
            ),
        )
        self.assertIsNone(
            sandbox.UnshareSandboxAdapter.explain_violation(2, "bad syntax")
        )


class RealRuntimeTests(unittest.TestCase):
    def test_real_unshare_enforces_temp_readonly_when_host_permits(self):
        # Never use /protected/archive as a write probe.  Both the capability check and
        # this integration exercise an isolated disposable directory only.
        adapter = sandbox.UnshareSandboxAdapter(protected_paths=())
        capability = adapter.capabilities(refresh=True)
        if not capability.available:
            self.skipTest(capability.reason or "unshare sandbox unavailable")

        with tempfile.TemporaryDirectory(prefix="zylab-sandbox-test-") as tmp:
            sentinel = Path(tmp) / "sentinel"
            sentinel.write_text("original\n", encoding="utf-8")
            prepared = adapter.prepare(
                "if printf changed > sentinel 2>/dev/null; then exit 81; fi; "
                "test \"$(cat sentinel)\" = original; printf 'ok\\n'",
                tmp,
                sandbox.FilesystemRules((tmp,)),
                sandbox.NetworkRules(False),
            )
            result = adapter.run(prepared, timeout=8)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ok", result.stdout)
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"), "original\n"
            )



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
    unittest.main(verbosity=2)
