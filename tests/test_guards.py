"""守卫逻辑的回归测试。

这些用例全部来自 2026-08-21 的实测：当时发现 rclone 守卫会误拒合法的
「拷出来」，而 README 明确承诺放行 —— 正是没有测试才让它活到现在。
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest import mock as _mock
from core import agent, settings as S, tools


class GuardFileLayer(unittest.TestCase):
    """文件工具层：用 realpath 解析，符号链接绕不过去。"""

    def test_direct_write_denied(self):
        for p in ("/protected/archive/x.txt", "/protected/archive/sub/deep/y"):
            with self.subTest(path=p):
                self.assertRaises(tools.Denied, tools._guard, p)

    def test_outside_paths_allowed(self):
        for p in ("/tmp/workspace/x.txt", "/tmp/y"):
            with self.subTest(path=p):
                tools._guard(p)          # 不抛就算过

    def test_symlink_cannot_bypass(self):
        link = os.path.join(tempfile.gettempdir(), "zylab_test_protected_link")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink("/protected/archive", link)
        try:
            self.assertRaises(tools.Denied, tools._guard, link + "/x")
        finally:
            os.remove(link)

    def test_dotdot_resolves_before_check(self):
        # /tmp/workspace/../protected/archive 解析成 /tmp/protected/archive，
        # 落在受保护路径之外，应放行 —— 前缀像不等于真的在里面
        tools._guard("/tmp/workspace/../protected/archive/z")


class GuardBashLayer(unittest.TestCase):
    """bash 层是 best-effort：拦住常见写法，拦不住任意解释器。"""

    def assertDenied(self, cmd):
        with self.subTest(cmd=cmd):
            self.assertRaises(tools.Denied, tools._guard_bash, cmd)

    def assertAllowed(self, cmd):
        with self.subTest(cmd=cmd):
            tools._guard_bash(cmd)

    def test_mutators_denied(self):
        self.assertDenied("rm -rf /protected/archive/x")
        self.assertDenied("mv a /protected/archive/b")

    def test_redirect_denied(self):
        self.assertDenied("> /protected/archive/out.txt")
        self.assertDenied("echo hi >> /protected/archive/log")

    def test_legit_writes_allowed(self):
        self.assertAllowed("echo hi > /tmp/workspace/x")
        self.assertAllowed("rm -rf /tmp/junk")

    def test_copy_out_allowed(self):
        """README 承诺：从 /protected/archive 拷出来始终放行。"""
        self.assertAllowed("cp /protected/archive/a /tmp/workspace/b")
        self.assertAllowed("cp -r /protected/archive/dir /tmp/workspace/")

    def test_rclone_write_denied(self):
        self.assertDenied("rclone copy /tmp/workspace/x archive:bucket/dst")
        self.assertDenied("rclone sync /tmp/workspace/x archive:bucket/dst")

    def test_rclone_copy_out_allowed(self):
        """archive:bucket 作为**源**是合法的 —— 这条曾经是 bug。"""
        self.assertAllowed("rclone copy archive:bucket/src /tmp/workspace/x")
        self.assertAllowed("rclone sync archive:bucket/a /tmp/workspace/b")

    def test_rclone_readonly_allowed(self):
        self.assertAllowed("rclone ls archive:bucket/dir")
        self.assertAllowed("rclone lsjson archive:bucket/")

    def test_sed_inplace_denied_all_forms(self):
        """`-i` 和 `-i.bak` 都是原地改写；只匹配 '-i' 会漏掉后缀形式。"""
        self.assertDenied("sed -i 's/a/b/' /protected/archive/f")
        self.assertDenied("sed -i.bak 's/a/b/' /protected/archive/f")


class WorkspaceProjectionInvalidation(unittest.TestCase):
    def test_mutation_classifier_covers_editing_shell_forms(self):
        mutating = (
            "echo changed > a.py",
            "touch a.py",
            "sed -i 's/a/b/' a.py",
            "perl -pi -e 's/a/b/' a.py",
            "git apply change.patch",
            "git restore a.py",
            "bash -lc 'apply_patch < change.patch'",
        )
        for command in mutating:
            with self.subTest(command=command):
                self.assertTrue(
                    tools._bash_may_change_workspace(command))
        for command in (
                "git status", "git diff", "ls -la", "python3 -m unittest"):
            with self.subTest(command=command):
                self.assertFalse(
                    tools._bash_may_change_workspace(command))

    def test_successful_mutating_tool_notifies_once(self):
        callback = mock.Mock()

        def fake_tool(**_kwargs):
            return "[ok]"

        for name, arguments in (
                ("bash", {"command": "touch a.py"}),
                ("write_file", {"path": "a.py", "content": "x"})):
            with self.subTest(name=name):
                callback.reset_mock()
                prepared = tools.PreparedArguments(
                    tool_name=name,
                    arguments_json=json.dumps(arguments))
                with (
                        mock.patch.dict(
                            tools.IMPL, {name: fake_tool}),
                        mock.patch.dict(
                            tools.HOOK_CTX,
                            {"workspace_changed": callback, "cfg": {}},
                            clear=False),
                        mock.patch(
                            "core.hooks.run_hooks", return_value=None),
                ):
                    self.assertEqual(tools.run(name, prepared), "[ok]")
                callback.assert_called_once()

    def test_read_only_bash_does_not_notify(self):
        callback = mock.Mock()
        prepared = tools.PreparedArguments(
            tool_name="bash",
            arguments_json=json.dumps({"command": "git status"}))
        with (
                mock.patch.dict(
                    tools.IMPL, {"bash": lambda **_kwargs: "[ok]"}),
                mock.patch.dict(
                    tools.HOOK_CTX,
                    {"workspace_changed": callback, "cfg": {}},
                    clear=False),
                mock.patch("core.hooks.run_hooks", return_value=None),
        ):
            self.assertEqual(tools.run("bash", prepared), "[ok]")
        callback.assert_not_called()

    def test_workspace_refresh_runs_after_post_tool_hook(self):
        events = []
        prepared = tools.PreparedArguments(
            tool_name="bash",
            arguments_json=json.dumps({"command": "touch a.py"}))
        with (
                mock.patch.dict(
                    tools.IMPL, {"bash": lambda **_kwargs: "[ok]"}),
                mock.patch.dict(
                    tools.HOOK_CTX,
                    {
                        "workspace_changed": (
                            lambda *_args, **_kwargs:
                            events.append("refresh")),
                        "cfg": {},
                    },
                    clear=False),
                mock.patch(
                    "core.hooks.run_hooks",
                    side_effect=lambda *_args, **_kwargs:
                    events.append("post-hook")),
        ):
            self.assertEqual(tools.run("bash", prepared), "[ok]")
        self.assertEqual(events, ["post-hook", "refresh"])

    def test_task_worker_routes_change_to_main_thread_event(self):
        callback = mock.Mock()

        class FakeTask:
            def __init__(self):
                self.events = []

            def runtime_event(self, event):
                self.events.append(event)
                return True

            def record_error(self, _error):
                pass

        task = FakeTask()
        prepared = tools.PreparedArguments(
            tool_name="bash",
            arguments_json=json.dumps({"command": "touch a.py"}))
        with (
                mock.patch.dict(
                    tools.IMPL, {"bash": lambda **_kwargs: "[ok]"}),
                mock.patch.dict(
                    tools.HOOK_CTX,
                    {"workspace_changed": callback, "cfg": {}},
                    clear=False),
                mock.patch("core.hooks.run_hooks", return_value=None),
        ):
            self.assertEqual(
                tools.run("bash", prepared, task=task), "[ok]")
        callback.assert_not_called()
        self.assertEqual(task.events, [{
            "kind": "workspace_changed",
            "payload": {"tool": "bash"},
        }])


class WorkspacePreservation(unittest.TestCase):
    def test_system_prompt_pins_workspace_preservation_rules(self):
        for required in (
                "未提交的改动属于用户",
                "不要清理、不要 stash、不要回滚",
                "reset --hard",
                "checkout --",
                "push --force",
                "覆盖或删除任何已有数据文件前"):
            with self.subTest(required=required):
                self.assertIn(required, agent.SYSTEM)

    def test_only_destructive_git_forms_are_upgraded(self):
        dangerous = {
            "git reset --hard": "git_reset_hard",
            "git checkout -- tracked.py": "git_checkout_paths",
            "git checkout -f main": "git_checkout_force",
            "git restore tracked.py": "git_restore_worktree",
            "git restore --worktree tracked.py": "git_restore_worktree",
            "git switch --discard-changes main": "git_switch_force",
            "git clean -fdx": "git_clean_force",
            "git branch -D experiment": "git_branch_delete_force",
            "git branch --delete --force experiment": "git_branch_delete_force",
            "git branch -d -f experiment": "git_branch_delete_force",
            "git push -f origin main": "git_push_force",
            "git push origin +HEAD:main": "git_push_force",
            "git stash drop": "git_stash_delete",
            "git stash clear": "git_stash_delete",
            "git reflog expire --expire=now --all": "git_reflog_expire",
            "git gc --prune=now": "git_gc_prune_now",
            "bash -lc 'git reset --hard'": "git_reset_hard",
        }
        with mock.patch.object(
                tools, "_git_status_evidence",
                return_value=("git status --porcelain=v1:", "(clean)",)):
            for command, expected in dangerous.items():
                with self.subTest(command=command):
                    risk = tools._guard_bash(command, cwd=os.getcwd())
                    self.assertIsNotNone(risk)
                    self.assertIn(expected, risk.kinds)

        for command in (
                "git status", "git log -1", "git diff",
                "git add tracked.py", "git commit -m checkpoint",
                "git clean -nfdx",
                "git restore --staged tracked.py",
                "git branch -d experiment",
                "git push --dry-run --force origin main",
                "git push --dry-run origin +HEAD:main",
                "git reflog expire --dry-run --all",
                "echo git reset --hard",
                "sh -c 'echo git reset --hard'"):
            with self.subTest(command=command):
                self.assertIsNone(
                    tools._guard_bash(command, cwd=os.getcwd()))

    def test_git_risk_contains_actual_target_worktree_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(
                ["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Zylab Test"],
                cwd=root, check=True)
            target = root / "tracked.txt"
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "baseline"],
                cwd=root, check=True)
            target.write_text("after\n", encoding="utf-8")
            command = f"cd {json.dumps(str(root))} && git reset --hard"

            with mock.patch.object(
                    tools, "_prepare_bash_sandbox",
                    return_value=(None, None, None)):
                prepared = tools.prepare(
                    "bash", {"command": command},
                    workspace_root=root)

            evidence = "\n".join(prepared.workspace_risk.evidence)
            self.assertIn("git status --porcelain=v1", evidence)
            self.assertIn("tracked.txt", evidence)
            self.assertTrue(prepared.requires_workspace_confirmation)

            denied = tools.run("bash", prepared)
            self.assertTrue(denied.startswith("[已拒绝]"), denied)
            self.assertIn("高风险工作区操作", denied)
            with self.assertRaises(tools.Denied):
                tools.t_bash(
                    command, _prepared=prepared)
            self.assertEqual(
                target.read_text(encoding="utf-8"), "after\n")

    def test_post_hook_command_is_the_one_risk_assessment_sees(self):
        with mock.patch(
                "core.hooks.run_hooks",
                return_value={"command": "git reset --hard"}), \
             mock.patch.object(
                tools, "_git_status_evidence",
                return_value=("git status --porcelain=v1:", "(clean)",)), \
             mock.patch.object(
                tools, "_prepare_bash_sandbox",
                return_value=(None, None, None)):
            prepared = tools.prepare(
                "bash", {"command": "git status"},
                workspace_root=os.getcwd())

        self.assertEqual(
            prepared.as_dict()["command"], "git reset --hard")
        self.assertIn(
            "git_reset_hard", prepared.workspace_risk.kinds)

    def test_large_recursive_delete_is_upgraded_but_small_one_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            large = root / "large"
            loose = root / "loose"
            small = root / "small"
            large.mkdir()
            loose.mkdir()
            small.mkdir()
            for index in range(
                    tools.BULK_DELETE_FILE_THRESHOLD + 1):
                (large / f"{index:03d}.txt").write_text(
                    "x", encoding="utf-8")
            for index in range(
                    tools.BULK_DELETE_FILE_THRESHOLD + 1):
                (loose / f"{index:03d}.txt").write_text(
                    "x", encoding="utf-8")
            for index in range(2):
                (small / f"{index}.txt").write_text(
                    "x", encoding="utf-8")

            large_risk = tools._guard_bash(
                f"rm -rf {large}", cwd=root)
            glob_risk = tools._guard_bash(
                f"rm -rf {loose}/*.txt", cwd=root)
            dynamic_risk = tools._guard_bash(
                'rm -rf "$HOME"', cwd=root)
            small_risk = tools._guard_bash(
                f"rm -rf {small}", cwd=root)

        self.assertIn("bulk_rm_rf", large_risk.kinds)
        self.assertIn("bulk_rm_rf", glob_risk.kinds)
        self.assertIn("bulk_rm_rf", dynamic_risk.kinds)
        self.assertTrue(any(
            "dynamic shell expansion" in row
            for row in dynamic_risk.evidence))
        self.assertTrue(any(
            str(large) in row for row in large_risk.evidence))
        self.assertIsNone(small_risk)


class ReadBoundary(unittest.TestCase):
    """SEC-001: read tools stay inside their bound workspace and hide secrets."""

    @staticmethod
    def _run(name, args, root):
        prepared = tools.prepare(name, args, workspace_root=root)
        return tools.run(name, prepared)

    def test_prepared_workspace_root_allows_all_read_tools(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "note.txt").write_text(
                "workspace needle\n", encoding="utf-8")
            cases = (
                ("read_file", {"path": str(root / "note.txt")}, "needle"),
                ("list_dir", {"path": str(root)}, "note.txt"),
                ("glob", {"pattern": "*.txt", "path": str(root)}, "note.txt"),
                ("grep", {"pattern": "needle", "path": str(root)}, "needle"),
            )
            for name, args, expected in cases:
                with self.subTest(tool=name):
                    self.assertIn(expected, self._run(name, args, root))

    def test_recursive_read_tools_hide_nested_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "note.txt").write_text("ordinary\n", encoding="utf-8")
            (root / "keys.env").write_text(
                "SIMULATED_API_KEY=not-a-real-secret\n", encoding="utf-8")
            with mock.patch.object(tools.os, "getcwd", return_value=str(root)):
                listed = tools.t_list_dir(str(root))
                globbed = tools.t_glob("*", str(root))
                grepped = tools.t_grep("SIMULATED_API_KEY", str(root))
                with self.assertRaises(tools.Denied):
                    tools.t_read_file(str(root / "keys.env"))
            for output in (listed, globbed, grepped):
                self.assertNotIn("keys.env", output)
                self.assertNotIn("not-a-real-secret", output)

    def test_realpath_blocks_symlink_and_dotdot_escape(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d)
            root = parent / "workspace"
            outside = parent / "outside"
            root.mkdir()
            outside.mkdir()
            target = outside / "note.txt"
            target.write_text("outside\n", encoding="utf-8")
            (root / "escape").symlink_to(outside, target_is_directory=True)
            for candidate in (
                    root / "escape" / "note.txt",
                    root / ".." / "outside" / "note.txt"):
                with self.subTest(path=candidate):
                    with self.assertRaises(tools.Denied):
                        tools._guard_read(candidate, cwd=root)


class GrepBehaviour(unittest.TestCase):
    """本机没有 ripgrep 二进制；exit 127 必须和「没搜到」区分开。"""

    def test_no_match_is_not_reported_as_missing_command(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("hello\n", encoding="utf-8")
            prepared = tools.prepare(
                "grep", {"pattern": "zzz_no_such_pattern_zzz", "path": d},
                workspace_root=d)
            out = tools.run("grep", prepared)
            self.assertIn("没有匹配", out)
            self.assertNotIn("命令不存在", out)

    def test_match_found(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.txt").write_text("needle here\n", encoding="utf-8")
            prepared = tools.prepare(
                "grep", {"pattern": "needle", "path": d},
                workspace_root=d)
            self.assertIn("needle", tools.run("grep", prepared))


class ToolDispatch(unittest.TestCase):
    def test_denied_surfaces_as_message_not_exception(self):
        out = tools.run("write_file", {"path": "/protected/archive/x", "content": "y"})
        self.assertTrue(out.startswith("[已拒绝]"), out)

    def test_unknown_tool(self):
        self.assertIn("未知工具", tools.run("nope", {}))

    def test_edit_requires_unique_match(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "a.py")
            Path(f).write_text("x=1\nx=1\n", encoding="utf-8")
            out = tools.t_edit_file(f, "x=1", "x=2")
            self.assertIn("不唯一", out)



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


class SubagentIsolation(unittest.TestCase):
    """子代理必须拿不到写工具，也不能再派子代理。"""

    def test_read_only_set_excludes_mutators(self):
        for t in ("bash", "write_file", "edit_file"):
            self.assertNotIn(t, tools.READ_ONLY)

    def test_subagent_cannot_nest(self):
        self.assertNotIn("subagent", tools.READ_ONLY)

    def test_subagent_is_auto_approved(self):
        # 它只读，不该每次弹确认
        self.assertIn("subagent", tools.SAFE)

    def test_main_prompt_teaches_proactive_bounded_delegation(self):
        self.assertIn("普通聊天中可直接调用 subagent", agent.SYSTEM)
        self.assertIn("2–3 个互相独立", agent.SYSTEM)
        self.assertIn("不要为简单任务启动 child", agent.SYSTEM)

    def test_subagent_schema_supports_a_bounded_parallel_batch(self):
        schema = next(
            row for row in tools.SCHEMA
            if row["function"]["name"] == "subagent")
        parameters = schema["function"]["parameters"]
        tasks = parameters["properties"]["tasks"]
        self.assertEqual(tasks["minItems"], 2)
        self.assertEqual(tasks["maxItems"], 3)
        self.assertEqual(
            parameters["oneOf"],
            [{"required": ["task"]}, {"required": ["tasks"]}])



class SettingsLayering(unittest.TestCase):
    """项目级配置只能收紧权限，不能放宽 —— 否则任意 clone 来的仓库能提权。"""

    def _load_with_project(self, patch):
        import json as _j
        from core import settings as S
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".zylab"))
            with open(os.path.join(d, ".zylab", "settings.json"), "w") as f:
                _j.dump(patch, f)
            with mock.patch.object(
                    S, "USER_FILE", Path(d) / "user" / "settings.json"):
                return S.load(cwd=d)[0]

    def test_project_cannot_relax(self):
        cfg = self._load_with_project({"permissions": {"bash": "allow"}})
        self.assertEqual(cfg["permissions"]["bash"], "ask")

    def test_project_can_tighten(self):
        cfg = self._load_with_project({"permissions": {"read_file": "deny"}})
        self.assertEqual(cfg["permissions"]["read_file"], "deny")

    def test_non_permission_fields_are_free(self):
        cfg = self._load_with_project({"model": "glm-5.2", "max_tokens": 4096})
        self.assertEqual(cfg["model"], "glm-5.2")
        self.assertEqual(cfg["max_tokens"], 4096)

    def test_nested_cwd_loads_config_from_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            nested = root / "src" / "pkg"
            nested.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".zylab").mkdir()
            (root / ".zylab" / "settings.json").write_text(
                json.dumps({"model": "repo-root-model"}),
                encoding="utf-8")
            user_file = Path(tmp) / "home" / "settings.json"
            with mock.patch.object(S, "USER_FILE", user_file):
                cfg, sources = S.load(cwd=nested)
                project_file = S.project_file(nested)

        self.assertEqual(cfg["model"], "repo-root-model")
        self.assertIn(
            str(root / ".zylab" / "settings.json"), sources)
        self.assertEqual(
            project_file,
            root / ".zylab" / "settings.json")

    def test_non_git_project_config_falls_back_to_exact_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "nested"
            cwd.mkdir()
            self.assertEqual(
                S.project_file(cwd),
                cwd / ".zylab" / "settings.json")

    def test_user_file_is_not_loaded_again_as_project_file(self):
        """HOME 本身作 cwd 时，同一个配置只能保留用户级语义。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_file = root / ".zylab" / "settings.json"
            user_file.parent.mkdir()
            hook_cfg = {
                "PreToolUse": [{"matcher": "bash", "command": "true"}],
            }
            user_file.write_text(json.dumps({
                "model": "user-model",
                "hooks": hook_cfg,
            }), encoding="utf-8")
            with (
                    mock.patch.object(S, "USER_FILE", user_file),
                    mock.patch("builtins.print") as printer,
            ):
                cfg, sources = S.load(cwd=root)

        self.assertEqual(cfg["model"], "user-model")
        self.assertEqual(cfg["hooks"], hook_cfg)
        self.assertEqual(sources.count(str(user_file)), 1)
        self.assertFalse(any(
            "hook 只能在用户级配置注册" in str(call)
            for call in printer.call_args_list))

    def test_only_user_config_can_allow_exact_http_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_file = root / "home" / "settings.json"
            project = root / "project"
            user_file.parent.mkdir()
            (project / ".zylab").mkdir(parents=True)
            user_file.write_text(json.dumps({
                "transport": {
                    "allowed_insecure_endpoints": [
                        "HTTP://TRUSTED.INVALID/v1/",
                    ],
                },
            }), encoding="utf-8")
            (project / ".zylab" / "settings.json").write_text(
                json.dumps({
                    "transport": {
                        "allowed_insecure_endpoints": [
                            "http://evil.invalid/v1",
                        ],
                    },
                }),
                encoding="utf-8")
            with mock.patch.object(S, "USER_FILE", user_file):
                cfg, _ = S.load(cwd=project)

        self.assertEqual(
            S.transport_policy(cfg)["allowed_insecure_endpoints"],
            ("http://trusted.invalid/v1",))

    def test_model_health_policy_has_valid_layered_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_file = root / "home" / "settings.json"
            project = root / "project"
            (project / ".zylab").mkdir(parents=True)
            user_file.parent.mkdir()
            user_file.write_text(json.dumps({
                "model_health": {
                    "probe_success_ttl": 120,
                    "circuit_failure_threshold": 5,
                },
            }), encoding="utf-8")
            (project / ".zylab" / "settings.json").write_text(json.dumps({
                "model_health": {
                    "probe_success_ttl": 60,
                    "circuit_open_ttl": 15,
                },
            }), encoding="utf-8")
            with mock.patch.object(S, "USER_FILE", user_file):
                cfg, _ = S.load(cwd=project)
        policy = S.model_health_policy(cfg)
        self.assertEqual(policy.probe_success_ttl, 60)
        self.assertEqual(policy.circuit_failure_threshold, 5)
        self.assertEqual(policy.circuit_open_ttl, 15)

    def test_invalid_model_health_layer_is_ignored_as_a_unit(self):
        cfg = self._load_with_project({
            "model_health": {
                "probe_transient_base_ttl": 600,
                "probe_transient_max_ttl": 10,
            },
        })
        self.assertEqual(
            cfg["model_health"],
            S.DEFAULTS["model_health"])

    def test_project_cannot_enable_auto_approve(self):
        cfg = self._load_with_project({"auto_approve": True})
        self.assertFalse(cfg["auto_approve"])

    def test_project_enforcement_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_file = root / "home" / "settings.json"
            project = root / "project"
            (project / ".zylab").mkdir(parents=True)
            user_file.parent.mkdir()
            user_file.write_text(
                json.dumps({"permissions": {"bash": "allow"}}),
                encoding="utf-8")
            (project / ".zylab" / "settings.json").write_text(
                json.dumps({"permissions": {"bash": "deny"}}),
                encoding="utf-8")
            with mock.patch.object(S, "USER_FILE", user_file):
                cfg, _ = S.load(cwd=project)
                row = next(
                    item for item in S.permission_rows(cwd=project, cfg=cfg)
                    if item["tool"] == "bash")
        self.assertEqual(row["effective"], "deny")
        self.assertEqual(row["source"], "project")
        self.assertTrue(row["project_enforced"])
        self.assertEqual(row["user"], "allow")

    def test_project_can_tighten_sandbox_but_cannot_disable_it(self):
        cfg = self._load_with_project({
            "sandbox": {
                "mode": "strict",
                "network_isolation": True,
                "protected_paths": ["/project/archive"],
            },
        })
        self.assertEqual(cfg["sandbox"]["mode"], "strict")
        self.assertTrue(cfg["sandbox"]["network_isolation"])
        # 不断言精确列表：基线里已有哪些受保护路径取决于跑测试的机器
        # （环境变量 ∪ 用户配置），干净 clone 上基线就是空的。这条要验的是
        # **项目层只能追加、不能删减**，所以逐条核对基线还在、且项目那条加进来了。
        from core import paths as _paths
        for baseline in _paths.protected_paths():
            self.assertIn(baseline, cfg["sandbox"]["protected_paths"])
        self.assertIn("/project/archive", cfg["sandbox"]["protected_paths"])

        relaxed = self._load_with_project({
            "sandbox": {"mode": "disabled", "network": False},
        })
        self.assertEqual(
            relaxed["sandbox"]["mode"], "ask-unsandboxed")
        self.assertTrue(relaxed["sandbox"]["network_isolation"])

    def test_secure_network_default_and_trusted_legacy_opt_out(self):
        self.assertTrue(S.DEFAULTS["sandbox"]["network_isolation"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_file = root / "home" / "settings.json"
            project = root / "project"
            user_file.parent.mkdir()
            project.mkdir()
            user_file.write_text(json.dumps({
                "sandbox": {"network": False},
            }), encoding="utf-8")
            with mock.patch.object(S, "USER_FILE", user_file):
                cfg, sources = S.load(cwd=project)

        self.assertFalse(cfg["sandbox"]["network_isolation"])
        self.assertNotIn("network", cfg["sandbox"])
        self.assertIn(
            "network isolation false (outbound open)",
            S.render(cfg, sources))

    def test_project_cannot_replace_sandbox_executables(self):
        cfg = self._load_with_project({
            "sandbox": {
                "unshare_executable": "/tmp/repo-owned-unshare",
                "bash_executable": "/tmp/repo-owned-bash",
            },
        })
        self.assertIsNone(cfg["sandbox"]["unshare_executable"])
        self.assertIsNone(cfg["sandbox"]["bash_executable"])

    def test_user_can_choose_binary_and_project_cannot_override_or_loosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_file = root / "home" / "settings.json"
            project = root / "project"
            user_file.parent.mkdir()
            (project / ".zylab").mkdir(parents=True)
            user_file.write_text(json.dumps({
                "sandbox": {
                    "mode": "strict",
                    "network_isolation": True,
                    "unshare_executable": "/trusted/unshare",
                },
            }), encoding="utf-8")
            (project / ".zylab" / "settings.json").write_text(
                json.dumps({
                    "sandbox": {
                        "mode": "disabled",
                        "network": False,
                        "unshare_executable": "/untrusted/unshare",
                    },
                }), encoding="utf-8")
            with mock.patch.object(S, "USER_FILE", user_file):
                cfg, _ = S.load(cwd=project)
        self.assertEqual(cfg["sandbox"]["mode"], "strict")
        self.assertTrue(cfg["sandbox"]["network_isolation"])
        self.assertEqual(
            cfg["sandbox"]["unshare_executable"], "/trusted/unshare")


class SettingsWrites(unittest.TestCase):
    def test_permission_update_preserves_fields_and_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zylab" / "settings.json"
            path.parent.mkdir()
            path.write_text(json.dumps({
                "model": "m1", "permissions": {"bash": "ask"},
            }), encoding="utf-8")
            with mock.patch.object(S, "USER_FILE", path):
                S.update_user_permission("write_file", "deny")
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["model"], "m1")
            self.assertEqual(value["permissions"], {
                "bash": "ask", "write_file": "deny"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                list(path.parent.glob(".settings.json.*.tmp")), [])

    def test_concurrent_process_permission_updates_preserve_every_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".zylab" / "settings.json"
            tools_and_values = {
                "bash": "deny", "write_file": "ask", "edit_file": "allow",
                "read_file": "deny", "list_dir": "ask", "glob": "allow",
                "grep": "deny", "subagent": "ask",
            }
            script = (
                "import sys\n"
                "from pathlib import Path\n"
                "from core import settings as S\n"
                "S.USER_FILE = Path(sys.argv[1])\n"
                "S.update_user_permission(sys.argv[2], sys.argv[3])\n"
            )
            root = Path(__file__).resolve().parents[1]
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(path), tool, value],
                    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True)
                for tool, value in tools_and_values.items()
            ]
            failures = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                if process.returncode:
                    failures.append(
                        (process.returncode, stdout, stderr))
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(failures, [])
            self.assertEqual(value["permissions"], tools_and_values)

    def test_malformed_user_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            original = b'{"permissions": '
            path.write_bytes(original)
            with mock.patch.object(S, "USER_FILE", path):
                with self.assertRaisesRegex(S.SettingsError, "无法解析"):
                    S.update_user_permission("bash", "allow")
            self.assertEqual(path.read_bytes(), original)

    def test_dangling_symlink_user_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.symlink_to(Path(tmp) / "missing-target.json")
            with mock.patch.object(S, "USER_FILE", path):
                with self.assertRaisesRegex(S.SettingsError, "符号链接"):
                    S.update_user_permission("bash", "allow")
            self.assertTrue(path.is_symlink())

    def test_file_error_is_reported_as_settings_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with mock.patch.object(S, "USER_FILE", path), \
                 mock.patch.object(
                    S, "_write_user_unlocked",
                    side_effect=PermissionError("read-only")):
                with self.assertRaisesRegex(S.SettingsError, "写入失败"):
                    S.update_user_permission("bash", "allow")


class BackupSafety(unittest.TestCase):
    """写/改文件前必须留下原文备份，且同一秒内多次编辑不能互相覆盖。"""

    def test_write_and_edit_create_distinct_backups(self):
        from core import store
        with tempfile.TemporaryDirectory() as d:
            backup_dir = Path(d) / "backups"
            f = Path(d) / "x.py"
            f.write_text("v1\n", encoding="utf-8")
            with mock.patch.object(store, "BACKUPS", backup_dir):
                tools.t_write_file(f, "v2\n")
                tools.t_edit_file(f, "v2", "v3")
                new = set(backup_dir.glob("*"))
                self.assertEqual(
                    len(new), 2, "两次修改应留下两份不同的备份")
                self.assertEqual(
                    {p.read_text().strip() for p in new}, {"v1", "v2"})
