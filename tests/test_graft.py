"""Hosted Graft sidecar security, routing and UI regression tests."""
import contextlib
import io
import json
import os
import stat
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import zylab as CLI
from core import graft, settings, tools
from tests.platform_support import (  # noqa: E402
    requires_posix_modes, requires_symlinks)


class HostedGraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        (self.root / "main.py").write_text(
            "def hello():\n    return 'hello'\n", encoding="utf-8")
        self.cache = self.base / "cache"
        self.executable = self.base / "fake-graft"
        self.executable.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time
            from pathlib import Path

            args = sys.argv[1:]
            graph = Path(args[args.index('--dir') + 1])
            if 'build' in args:
                root = Path(args[args.index('build') + 1])
                graph.mkdir(parents=True, exist_ok=True)
                with (graph / 'build.log').open('a', encoding='utf-8') as stream:
                    stream.write('build\\n')
                time.sleep(0.2)
                target = graph / '.graph'
                target.mkdir(parents=True, exist_ok=True)
                (target / 'wiring.json').write_text('{}', encoding='utf-8')
                (root / '.stale').unlink(missing_ok=True)
                print(json.dumps({'built': True}))
            elif 'check' in args:
                root = Path(args[args.index('check') + 1])
                stale = (root / '.stale').exists()
                print(json.dumps({'graph': {'ok': not stale}}))
                raise SystemExit(1 if stale else 0)
            else:
                auto_refreshed = False
                if 'ask' in args:
                    root = Path(args[args.index('ask') + 2])
                    marker = root / '.stale'
                    auto_refreshed = marker.exists()
                    marker.unlink(missing_ok=True)
                    if (root / '.degraded').exists():
                        print('[graft] graph refresh skipped: lock busy; answering from the current graph', file=sys.stderr)
                    elif auto_refreshed:
                        print('[graft] refreshed the graph (1 file changed) before answering', file=sys.stderr)
                print(json.dumps({
                    'matches': [{'file': 'main.py', 'symbol': 'hello'}],
                    'saved': {'tokens': 999},
                    'hosted': os.environ.get('GRAFT_HOSTED'),
                    'home': os.environ.get('HOME'),
                    'provider_secret': os.environ.get('OPENAI_API_KEY'),
                    'text': 'safe\\u001b[31m\\u009b32m',
                    'auto_refreshed': auto_refreshed,
                }))
            """), encoding="utf-8")
        self.executable.chmod(0o700)
        self.cfg = json.loads(json.dumps(settings.DEFAULTS))
        self.cfg["graft"].update({
            "enabled": True,
            "executable": str(self.executable),
            "cache_root": str(self.cache),
            "timeout_seconds": 30,
        })
        self.network_patch = mock.patch.object(
            graft, "network_namespace_prefix", return_value=())
        self.network_patch.start()

    def tearDown(self):
        self.network_patch.stop()
        self.temp.cleanup()

    def test_defaults_tools_and_legacy_map_are_pinned(self):
        policy = settings.graft_policy(settings.DEFAULTS)
        self.assertTrue(policy["enabled"])
        self.assertFalse(settings.map_policy(settings.DEFAULTS)["use"])
        self.assertFalse(
            settings.map_policy(settings.DEFAULTS)["use_architecture"])
        for name in graft.TOOL_NAMES:
            with self.subTest(name=name):
                self.assertEqual(settings.DEFAULTS["permissions"][name], "allow")
                self.assertIn(name, tools.SAFE)
                self.assertIn(name, tools.READ_ONLY)
                self.assertIn(name, tools.IMPL)
                self.assertIn(name, tools.ALL)
        trace_schema = next(
            item["function"]["parameters"]["properties"]["depth"]
            for item in tools.SCHEMA
            if item["function"]["name"] == "graft_trace_calls")
        self.assertNotIn("oneOf", trace_schema)
        self.assertIn("all", trace_schema["enum"])

    def test_project_config_cannot_enable_redirect_or_enlarge_graft(self):
        cfg = json.loads(json.dumps(settings.DEFAULTS))
        settings._merge(cfg, {"graft": {
            "enabled": False,
            "executable": str(self.executable),
            "cache_root": str(self.cache),
            "max_files": 100,
            "max_result_chars": 4_000,
        }}, allow_permission_relax=True)
        settings._merge(cfg, {"graft": {
            "enabled": True,
            "executable": str(self.base / "project-controlled"),
            "cache_root": str(self.root / ".graft"),
            "max_files": 50_000,
            "max_result_chars": 30_000,
        }}, allow_permission_relax=False)
        policy = settings.graft_policy(cfg)
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["executable"], str(self.executable))
        self.assertEqual(policy["cache_root"], str(self.cache))
        self.assertEqual(policy["max_files"], 100)
        self.assertEqual(policy["max_result_chars"], 4_000)

    def test_network_namespace_failure_hides_graft_tools_fail_closed(self):
        with mock.patch.object(
                graft, "network_namespace_prefix",
                side_effect=graft.GraftError("netns unavailable")):
            self.assertFalse(graft.available(self.cfg))
            status = graft.local_status(
                self.cfg, workspace_root=self.root)
        self.assertFalse(status["available"])
        self.assertIn("netns unavailable", status["error"])

    @requires_symlinks
    def test_target_and_nested_scope_cannot_escape_workspace(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("TOKEN = 'fake'\n", encoding="utf-8")
        with self.assertRaisesRegex(graft.GraftError, "workspace 之外"):
            graft.resolve_target(outside, workspace_root=self.root)
        link = self.root / "outside-link.py"
        link.symlink_to(outside / "secret.py")
        with self.assertRaisesRegex(graft.GraftError, "target 之外"):
            graft.normalize_args(
                "file_api", {"file": str(link)}, root=self.root.resolve())
        sensitive = self.root / "keys.env"
        sensitive.write_text("TOKEN=fake\n", encoding="utf-8")
        with self.assertRaisesRegex(graft.GraftError, "疑似凭据"):
            graft.normalize_args(
                "file_api", {"file": "keys.env"}, root=self.root.resolve())
        with self.assertRaises(tools.Denied):
            tools.prepare(
                "graft_find_code", {"query": "secret", "path": str(outside)},
                workspace_root=str(self.root))

    def test_untrusted_executable_mode_is_rejected(self):
        self.executable.chmod(
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IWGRP)
        with self.assertRaisesRegex(graft.GraftError, "可信"):
            graft.resolve_executable(settings.graft_policy(self.cfg))

    def test_untrusted_executable_parent_is_rejected(self):
        shared = self.base / "shared-bin"
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        candidate = shared / "graft"
        candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        candidate.chmod(0o700)
        policy = settings.graft_policy(self.cfg)
        policy["executable"] = str(candidate)
        with self.assertRaisesRegex(graft.GraftError, "可信"):
            graft.resolve_executable(policy)

    # graft 的信任检查全靠 POSIX 权限位（属主 + group/other 不可写）。
    # Windows 上 `os.stat().st_mode` 是合成值（目录一律 0o777），
    # `S_IWGRP|S_IWOTH` 永远置位，于是这套检查**恒不成立** —— 夹具造不出
    # 「私有目录」这个载体。而 graft 本身要 unshare + /proc/self/ns/net，
    # 在 Windows 上根本不可用，所以这里是 skip 而不是放宽产品侧的判据。
    @requires_posix_modes
    def test_cache_root_mode_is_validated_without_chmodding_parent(self):
        broad = self.base / "existing-cache-root"
        broad.mkdir(mode=0o755)
        broad.chmod(0o755)
        policy = settings.graft_policy(self.cfg)
        policy["cache_root"] = str(broad)
        project_cache = graft.cache_dir(self.root.resolve(), policy)
        self.assertEqual(stat.S_IMODE(broad.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(project_cache.stat().st_mode), 0o700)
        project_cache.chmod(0o755)
        with self.assertRaisesRegex(graft.GraftError, "私有目录"):
            graft.cache_dir(self.root.resolve(), policy)
        project_cache.chmod(0o700)
        broad.chmod(0o775)
        with self.assertRaisesRegex(graft.GraftError, "私有目录"):
            graft.cache_dir(self.root.resolve(), policy)

    def test_inventory_does_not_hide_a_real_source_directory_named_graft(self):
        source_dir = self.root / "graft"
        source_dir.mkdir()
        (source_dir / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        stats = graft.inventory(
            self.root.resolve(), settings.graft_policy(self.cfg))
        self.assertEqual(stats["files"], 2)

    # graft 的信任检查全靠 POSIX 权限位（属主 + group/other 不可写）。
    # Windows 上 `os.stat().st_mode` 是合成值（目录一律 0o777），
    # `S_IWGRP|S_IWOTH` 永远置位，于是这套检查**恒不成立** —— 夹具造不出
    # 「私有目录」这个载体。而 graft 本身要 unshare + /proc/self/ns/net，
    # 在 Windows 上根本不可用，所以这里是 skip 而不是放宽产品侧的判据。
    @requires_posix_modes
    def test_lazy_hosted_worker_uses_external_cache_and_scrubbed_env(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "fictional-secret"}):
            envelope = graft.execute(
                "graft_find_code", {"query": "hello"}, cfg=self.cfg,
                workspace_root=self.root)
        result = envelope["result"]
        self.assertTrue(envelope["built"])
        self.assertEqual(result["hosted"], "1")
        self.assertIsNone(result["provider_secret"])
        self.assertNotIn("saved", result)
        self.assertNotIn("\x1b", result["text"])
        self.assertNotIn("\u009b", result["text"])
        self.assertEqual(envelope["inventory"], {"files": 1, "bytes": 32})
        graph = Path(envelope["graph"])
        self.assertTrue((graph / ".graph" / "wiring.json").is_file())
        self.assertFalse(graph.is_relative_to(self.root))
        self.assertEqual(stat.S_IMODE(graph.stat().st_mode), 0o700)

        # Hosted preAction skips standalone upkeep, while Graft's query gate
        # must still check freshness and refresh before the next answer.
        (self.root / ".stale").write_text("1", encoding="utf-8")
        refreshed = graft.execute(
            "graft_find_code", {"query": "hello"}, cfg=self.cfg,
            workspace_root=self.root)
        self.assertFalse(refreshed["built"])
        self.assertEqual(refreshed["freshness"], "refreshed")
        self.assertIn("refreshed the graph", refreshed["notice"])
        self.assertTrue(refreshed["result"]["auto_refreshed"])
        self.assertFalse((self.root / ".stale").exists())

        (self.root / ".degraded").write_text("1", encoding="utf-8")
        degraded = graft.execute(
            "graft_find_code", {"query": "hello"}, cfg=self.cfg,
            workspace_root=self.root)
        self.assertEqual(degraded["freshness"], "degraded")
        self.assertIn("answering from the current graph", graft.render(degraded))

    def test_tool_runtime_emits_compact_state_events(self):
        events = []
        task = SimpleNamespace(runtime_event=lambda event: events.append(event) or True)
        prepared = SimpleNamespace(workspace_root=str(self.root))
        fake = {
            "ok": True, "action": "find_code", "root": str(self.root),
            "built": False, "truncated": False,
            "inventory": {"files": 1, "bytes": 32},
            "result": {"matches": []},
        }
        with mock.patch.object(graft, "execute", return_value=fake):
            output = tools.t_graft_find_code(
                "hello", _task=task, _prepared=prepared,
                _execution_context=tools.ExecutionContext.capture(
                    hook_config=self.cfg))
        self.assertIn("repository-derived untrusted data", output)
        self.assertEqual(
            [event["payload"]["state"] for event in events],
            ["running", "ready"])

    # graft 的信任检查全靠 POSIX 权限位（属主 + group/other 不可写）。
    # Windows 上 `os.stat().st_mode` 是合成值（目录一律 0o777），
    # `S_IWGRP|S_IWOTH` 永远置位，于是这套检查**恒不成立** —— 夹具造不出
    # 「私有目录」这个载体。而 graft 本身要 unshare + /proc/self/ns/net，
    # 在 Windows 上根本不可用，所以这里是 skip 而不是放宽产品侧的判据。
    @requires_posix_modes
    def test_execute_prepends_the_mandatory_network_namespace(self):
        prefix = ("/usr/bin/unshare", "--net", "--")
        response = json.dumps({
            "ok": True, "action": "find_code", "root": str(self.root),
            "graph": str(self.cache / "graph"), "built": False,
            "freshness": "fresh", "notice": None, "truncated": False,
            "result": {"matches": []},
        }).encode()
        with (
                mock.patch.object(
                    graft, "network_namespace_prefix", return_value=prefix),
                mock.patch.object(
                    graft, "_direct_worker", return_value=(0, response, b""))
                as runner):
            envelope = graft.execute(
                "graft_find_code", {"query": "hello"}, cfg=self.cfg,
                workspace_root=self.root)
        argv = runner.call_args.args[0]
        self.assertEqual(tuple(argv[:len(prefix)]), prefix)
        self.assertTrue(envelope["network_isolated"])

    def test_two_terminals_share_one_locked_initial_build(self):
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def invoke():
            try:
                barrier.wait(timeout=2)
                results.append(graft.execute(
                    "graft_find_code", {"query": "hello"}, cfg=self.cfg,
                    workspace_root=self.root))
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        graph = Path(results[0]["graph"])
        self.assertEqual(
            (graph / "build.log").read_text(encoding="utf-8").splitlines(),
            ["build"])
        self.assertEqual(
            stat.S_IMODE((graph / ".zylab-hosted.lock").stat().st_mode),
            0o600)

    def test_zero_exit_without_graph_is_not_reported_as_success(self):
        no_graph = self.base / "fake-graft-no-graph"
        no_graph.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            print(json.dumps({'claimed': 'success'}))
            """), encoding="utf-8")
        no_graph.chmod(0o700)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["graft"]["executable"] = str(no_graph)
        with self.assertRaisesRegex(graft.GraftError, "未生成"):
            graft.execute(
                "graft_find_code", {"query": "hello"}, cfg=cfg,
                workspace_root=self.root)

    def test_direct_admin_interrupt_terminates_worker_process_group(self):
        class Process:
            pid = 424242
            returncode = None

            def __init__(self):
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt
                return b"", b""

            def wait(self, timeout=None):
                self.returncode = -15
                return self.returncode

        process = Process()
        # 钉的是**产品实际用的那个接缝**：graft 走 wincompat.signal_process_group，
        # 不再直接调 os.killpg。原来这里 patch 的是 `graft.os` 的 killpg——它在
        # Linux 上能过，只是因为 `graft.os` 就是 stdlib 的 os 模块对象，顺带拦到了
        # wincompat POSIX 分支里的那次调用；而 Windows 上 `os` **没有 killpg**，
        # `mock.patch.object` 当场 AttributeError（2026-09-22 CI 两列 Windows）。
        # 换成 patch 那个接缝之后，两个平台测的是同一件事。
        with (
                mock.patch.object(graft.subprocess, "Popen", return_value=process),
                mock.patch.object(graft.wincompat, "signal_process_group") as kill,
                self.assertRaises(KeyboardInterrupt)):
            graft._direct_worker(
                ["worker"], cwd=str(self.root), env={}, timeout=30)
        kill.assert_called_once_with(process.pid, graft.signal.SIGTERM)

    def test_no_graft_tool_escapes_the_hidden_set(self):
        """sidecar 缺席时靠 TOOL_NAMES 整体摘掉 graft 工具；漏一个就等于给模型
        留了一个必然失败的工具。graft_index 在 09-09 新增时就漏在外面（09-15 实测）。"""
        in_schema = {
            item["function"]["name"] for item in tools.SCHEMA
            if item["function"]["name"].startswith("graft_")
        }
        self.assertTrue(in_schema)
        self.assertEqual(in_schema - set(graft.ALL_TOOL_NAMES), set())
        # 两个集合各司其职：只读查询那批才享有 SAFE/plan 模式待遇
        self.assertTrue(graft.TOOL_NAMES.issubset(graft.ALL_TOOL_NAMES))
        self.assertNotIn("graft_index", graft.TOOL_NAMES)
        self.assertNotIn("graft_index", tools.SAFE)

    def test_session_only_offers_tools_when_trusted_graft_is_available(self):
        sess = object.__new__(CLI.Session)
        sess.plan_mode = False
        sess.workflow_auto = False
        sess.cfg = self.cfg
        with mock.patch.object(CLI.GRAFT, "available", return_value=False):
            # 缺席时必须摘掉**全部** sidecar 工具：只断言查询那几个的话，
            # graft_index 这种新增的写类工具会漏出去（09-15 实测）。
            self.assertTrue(
                graft.ALL_TOOL_NAMES.isdisjoint(sess.allowed_tool_names()))
        with mock.patch.object(CLI.GRAFT, "available", return_value=True):
            self.assertTrue(graft.TOOL_NAMES.issubset(sess.allowed_tool_names()))
            sess.plan_mode = True
            self.assertTrue(graft.TOOL_NAMES.issubset(sess.allowed_tool_names()))

    def test_provider_schema_prioritizes_graft_without_forcing_tool_choice(self):
        agent = object.__new__(CLI.A.Agent)
        agent.supports_tools = True
        offered = agent._offered_tools(tools.ALL)
        names = [item["function"]["name"] for item in offered]
        self.assertEqual(names[:5], [
            "graft_find_code", "graft_file_api", "graft_trace_calls",
            "graft_find_all", "graft_repo_map",
        ])
        self.assertIn("第一个导航工具", CLI.A.SYSTEM)
        self.assertIn("不要先用 list_dir/glob", CLI.A.SYSTEM)

    def test_graft_command_and_footer_expose_state_without_open_step(self):
        sess = SimpleNamespace(
            cfg=self.cfg, _session_cwd=str(self.root), _graft_current=None)
        status = {
            "enabled": True, "available": True, "indexed": False,
            "root": str(self.root), "executable": str(self.executable),
            "graph": str(self.cache / "graph"),
            "network_isolated": True,
        }
        output = io.StringIO()
        with (
                mock.patch.object(CLI.GRAFT, "local_status", return_value=status),
                mock.patch.object(CLI.GRAFT_COMPONENT, "inspect", return_value={
                    "ready": True, "state": "ready",
                    "node_version": "v24.19.0", "revision": "a" * 40,
                    "checks": {},
                }),
                contextlib.redirect_stdout(output)):
            CLI.cmd_graft(sess, "doctor")
        self.assertIn("not built (lazy)", output.getvalue())
        self.assertIn("network namespace isolated", output.getvalue())
        self.assertIn("component ready", output.getvalue())
        self.assertIn("revision aaaaaaaaaaaa", output.getvalue())
        self.assertIn("graft", CLI.REGISTRY)

        footer_session = SimpleNamespace(
            ag=SimpleNamespace(
                session_id="graft-session", model="test-model",
                gateway="deepinfer", compact_at=100_000,
                supports_tools=True),
            _title="graft test", _session_cwd=str(self.root),
            _graft_current={"state": "ready", "action": "find_code"},
            _sandbox_status={
                "marker": "SANDBOXED", "network_marker": "NET ISOLATED"},
        )
        with mock.patch.object(CLI.store, "repo_context", return_value={}):
            footer = CLI.status_line(footer_session)
        self.assertIn("graft ready/find_code", footer)


if __name__ == "__main__":
    unittest.main()
