import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import zylab as CLI
from core import agent, context, skills, tools
from tests.platform_support import requires_symlinks  # noqa: E402


def write_skill(root, name, description, body="REFERENCE BODY", extra=""):
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{extra}"
        "---\n"
        f"{body}\n"
    )
    path = directory / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


class SkillCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.builtin = self.root / "builtin"
        self.user = self.root / "user"
        self.repo = self.root / "repo"
        self.nested = self.repo / "src"
        self.project = self.repo / ".zylab" / "skills"
        for path in (self.builtin, self.user, self.project, self.nested):
            path.mkdir(parents=True)
        self.patches = (
            mock.patch.object(skills, "BUILTIN_ROOT", self.builtin),
            mock.patch.object(skills, "USER_ROOT", self.user),
            mock.patch.object(
                skills.store, "repo_context",
                return_value={"repo_root": str(self.repo)}),
        )
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def load(self):
        return skills.load(cwd=self.nested)

    def test_frontmatter_unknown_keys_warn_and_body_stays_out_of_index(self):
        marker = "BODY-MUST-NOT-BE-IN-SYSTEM-PROMPT"
        path = write_skill(
            self.builtin, "review-code",
            "Use when reviewing a diff, PR, or uncommitted changes",
            body=marker, extra="license: internal\n")

        catalog = self.load()
        item = catalog.skills["review-code"]
        index = skills.render_index(catalog)

        self.assertEqual(item.scope, "builtin")
        self.assertEqual(item.path, str(path.resolve()))
        self.assertIn("unknown frontmatter key 'license'", "\n".join(
            catalog.warnings))
        self.assertIn("review-code | builtin | Use when", index["text"])
        self.assertNotIn(marker, index["text"])

    def test_missing_metadata_and_oversize_are_isolated_errors(self):
        good = write_skill(
            self.user, "good-skill",
            "Use when checking a diff, patch, or regression")
        broken = self.user / "broken-skill"
        broken.mkdir()
        (broken / "SKILL.md").write_text(
            "---\nname: broken-skill\n---\nbody\n", encoding="utf-8")
        catalog = self.load()
        self.assertIn("good-skill", catalog.skills)
        self.assertEqual(catalog.skills["good-skill"].path, str(good.resolve()))
        self.assertNotIn("broken-skill", catalog.skills)
        self.assertTrue(any("description" in row for row in catalog.errors))

        with mock.patch.object(skills, "MAX_SKILL_BYTES", 8):
            oversized = self.load()
        self.assertFalse(oversized.skills)
        self.assertTrue(any("超过 8 bytes" in row for row in oversized.errors))

    def test_user_then_project_then_builtin_precedence_is_visible(self):
        for root, label in (
                (self.builtin, "builtin"),
                (self.project, "project"),
                (self.user, "user")):
            write_skill(
                root, "same-skill",
                f"Use when {label} diff, patch, or review is requested")

        catalog = self.load()

        self.assertEqual(catalog.skills["same-skill"].scope, "user")
        self.assertEqual(
            [(row["winner"], row["ignored_scope"])
             for row in catalog.conflicts],
            [("user", "project"), ("user", "builtin")])

    def test_index_truncates_by_scope_priority_and_never_exceeds_64(self):
        write_skill(
            self.user, "z-user",
            "Use when user diff, patch, or review knowledge applies")
        write_skill(
            self.project, "z-project",
            "Use when project diff, patch, or review knowledge applies")
        write_skill(
            self.builtin, "a-builtin",
            "Use when builtin diff, patch, or review knowledge applies")
        short = skills.render_index(self.load(), max_entries=2)
        self.assertEqual(
            [row.name for row in short["entries"]],
            ["z-user", "z-project"])
        self.assertTrue(any("截断" in row for row in short["warnings"]))

        for number in range(65):
            write_skill(
                self.builtin, f"bulk-{number:02d}",
                "Use when checking bulk diff, patch, or review evidence")
        bounded = skills.render_index(self.load())
        self.assertEqual(len(bounded["entries"]), 64)
        self.assertEqual(len(bounded["text"].splitlines()), 64)


class SkillPromptTests(unittest.TestCase):
    def test_system_prompt_marks_index_optional_and_excludes_skill_body(self):
        index = (
            "code-review | builtin | Use when reviewing a diff, PR, or "
            "uncommitted changes | /tmp/skills/code-review/SKILL.md")
        prompt = agent.system_prompt(
            load_md=True, load_user_md=False, load_project_md=False,
            load_skills=True, skills_context=index)
        self.assertIn("<skills-index", prompt)
        self.assertIn("可选知识索引", prompt)
        self.assertIn("不是用户指令", prompt)
        self.assertIn("project scope", prompt)
        self.assertIn(index, prompt)
        self.assertNotIn("REFERENCE BODY", prompt)

        disabled = agent.system_prompt(
            load_md=True, load_user_md=False, load_project_md=False,
            load_skills=False, skills_context=index)
        self.assertNotIn("<skills-index", disabled)

    def test_session_refresh_injects_index_only_when_enabled(self):
        class StubAgent:
            load_skills = True

            def __init__(self):
                self.received = None

            def set_skills_context(self, value):
                self.received = value

        catalog = skills.Catalog({}, [], [], [], {})
        rendered = {
            "text": "skill | builtin | Use when a, b, or c | /x/SKILL.md",
            "entries": [], "available": 1, "warnings": [],
        }
        session = object.__new__(CLI.Session)
        session.ag = StubAgent()
        session._session_cwd = "/tmp/project"
        session._skills_catalog = catalog
        session.skills_index = {}
        session._skills_error = None
        with (
                mock.patch.object(CLI.SKILLS, "load", return_value=catalog),
                mock.patch.object(
                    CLI.SKILLS, "render_index", return_value=rendered)):
            result = CLI.Session.refresh_skills_context(session)
        self.assertIs(result, rendered)
        self.assertEqual(session.ag.received, rendered["text"])

        session.ag.load_skills = False
        with (
                mock.patch.object(CLI.SKILLS, "load", return_value=catalog),
                mock.patch.object(
                    CLI.SKILLS, "render_index", return_value=rendered)):
            CLI.Session.refresh_skills_context(session)
        self.assertEqual(session.ag.received, "")


class SkillReadBoundaryTests(unittest.TestCase):
    @requires_symlinks
    def test_all_three_skill_roots_are_readable_but_credentials_stay_denied(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace = root / "repo" / "src"
            workspace.mkdir(parents=True)
            builtin = root / "builtin"
            user = root / "user"
            project = root / "repo" / ".zylab" / "skills"
            paths = [
                write_skill(
                    location, f"skill-{number}",
                    "Use when reading a diff, patch, or review")
                for number, location in enumerate(
                    (builtin, user, project), 1)
            ]
            with (
                    mock.patch.object(skills, "BUILTIN_ROOT", builtin),
                    mock.patch.object(skills, "USER_ROOT", user),
                    mock.patch.object(
                        skills.store, "repo_context",
                        return_value={"repo_root": str(root / "repo")})):
                for path in paths:
                    with self.subTest(path=path):
                        self.assertEqual(
                            tools._guard_read(path, cwd=workspace),
                            str(path.resolve()))
                        prepared = tools.prepare(
                            "read_file", {"path": str(path)},
                            workspace_root=workspace)
                        self.assertIn(
                            "REFERENCE BODY",
                            tools.run("read_file", prepared))
                secret = user / "keys.env"
                secret.write_text(
                    "FAKE_TOKEN=not-a-real-secret\n", encoding="utf-8")
                with self.assertRaises(tools.Denied) as denied:
                    tools._guard_read(secret, cwd=workspace)
                self.assertEqual(
                    denied.exception.decision, "credential_path_denied")
                outside = root / "outside.md"
                outside.write_text("outside\n", encoding="utf-8")
                escape = user / "escape.md"
                escape.symlink_to(outside)
                with self.assertRaises(tools.Denied) as denied:
                    tools._guard_read(escape, cwd=workspace)
                self.assertEqual(
                    denied.exception.decision, "outside_workspace_denied")


class SkillCliAndBuiltinTests(unittest.TestCase):
    def test_skills_command_is_read_only_and_avoids_pty_sentinels(self):
        item = skills.Skill(
            name="code-review", scope="builtin", path="/x/SKILL.md",
            description="Use when reviewing a diff, PR, or changes",
            bytes=123)
        catalog = skills.Catalog(
            {item.name: item}, [], [], [], {"builtin": "/x"})

        class Session:
            _skills_catalog = catalog
            skills_index = {
                "entries": [item], "available": 1, "warnings": [],
            }

            def refresh_skills_context(self):
                raise AssertionError("plain /skills must not rebuild prompt")

        with contextlib.redirect_stdout(io.StringIO()) as output:
            CLI.cmd_skills(Session(), "")
        rendered = output.getvalue()
        self.assertIn("code-review", rendered)
        self.assertIn("builtin", rendered)
        self.assertIn("123", rendered)
        for sentinel in ("空闲", "Up=", "/help 看命令"):
            self.assertNotIn(sentinel, rendered)
        self.assertIs(CLI.REGISTRY["skills"][0], CLI.cmd_skills)

    def test_builtin_descriptions_route_and_index_has_stable_snapshot(self):
        expected = {
            "pod-environment": (
                "Use when", "installing packages", "creating venvs",
                "worker counts", "network/proxy failures"),
            "code-review": (
                "Use when", "diff", "PR", "uncommitted changes"),
            "test-ratchet": (
                "Use when", "adding a feature", "fixing a bug",
                "regression", "coverage"),
            "quantify-first": (
                "Use when", "counts", "runtime", "storage", "throughput"),
            "adversarial-review": (
                "Use when", "evidence", "assumptions", "conclusions",
                "edge cases"),
            "experiment-rigor": (
                "Use when", "experiments", "baselines", "ablations",
                "zero results"),
            "literature-scout": (
                "Use when", "papers", "documentation", "benchmarks",
                "citations"),
            "compute-job-submit": (
                "Use when", "sizing", "submitting", "monitoring",
                "cluster"),
            "experiment-log": (
                "Use when", "runs", "benchmark numbers", "experiment IDs",
                "provenance"),
            "jsonl-data-safety": (
                "Use when", "joining", "transforming", "rewriting",
                "JSONL"),
            "vendor-docs": (
                "Use when", "gateways", "model names", "tool support",
                "provider routing"),
            # ES1-lite（2026-08-31）。只加三条不与既有条目抢触发词的；
            # tdd / codebase-design 与 test-ratchet / safe-refactor 重叠，
            # 等 ES0a 的负向触发评测能证明不搞坏既有路由后再加。
            "diagnosing-bugs": (
                "Use when", "intermittent", "survived a first fix",
                "regressed performance", "reproduce before editing"),
            "trace-change-impact": (
                "Use when", "public interface", "persisted format",
                "permission tier", "producers and consumers"),
            "domain-modeling": (
                "Use when", "means different things", "naming a new concept",
                "expensive to reverse"),
            # ES1 剩下两条里只加了这一条。`tdd` 被判定为与 test-ratchet 重复 ——
            # test-ratchet 的 Workflow 第 2–4 步本身就是红绿循环，
            # 「测公共行为、拒绝实现耦合」也已在它的 Judgment rules 里。
            "codebase-design": (
                "Use when", "module boundary", "designing a new interface",
                "abstraction earns its keep", "two places"),
            "safe-refactor": (
                "Use when", "refactoring", "legacy code", "renaming APIs",
                "preserving behavior"),
            "git-workspace-safety": (
                "Use when", "staging", "committing", "reverting",
                "dirty Git tree"),
            "pty-tui-testing": (
                "Use when", "terminal UI", "PTY input", "mouse selection",
                "redraws"),
        }
        with tempfile.TemporaryDirectory() as d:
            isolated = Path(d)
            repo = isolated / "repo"
            repo.mkdir()
            with (
                    mock.patch.object(skills, "USER_ROOT", isolated / "user"),
                    mock.patch.object(
                        skills.store, "repo_context",
                        return_value={"repo_root": str(repo)})):
                catalog = skills.load(cwd=repo)
        self.assertEqual(catalog.errors, [])
        self.assertEqual(catalog.warnings, [])
        self.assertEqual(set(catalog.skills), set(expected))
        for name, phrases in expected.items():
            description = catalog.skills[name].description
            self.assertTrue(description.startswith("Use when"))
            for phrase in phrases:
                self.assertIn(phrase, description)
            self.assertLess(
                len(Path(catalog.skills[name].path).read_text(
                    encoding="utf-8").splitlines()),
                200,
                f"{name} must stay progressively loadable",
            )
        rendered = skills.render_index(catalog)
        self.assertEqual(
            [line.split(" | ", 1)[0]
             for line in rendered["text"].splitlines()],
            sorted(expected),
        )
        self.assertNotIn("data-pipeline-preflight", rendered["text"])
        self.assertNotIn("repo-onboarding", rendered["text"])
        # The original batch-two estimate assumed paths and trigger text would
        # cost about 250 tokens.  With the required absolute SKILL.md paths,
        # the complete 14-entry catalog measures 652 tokens.  Keep useful
        # routing descriptions and ratchet the observed budget instead of
        # reducing them to ambiguous keyword fragments.
        #
        # 预算必须与**安装路径长度**无关。索引里带的是绝对路径，所以同一份
        # 目录装在 /root/data/zylab 与装在一条很深的路径下，token 数能差
        # 300 以上（2026-08-28 用 bundle clone 到深路径实测：652 → 971）。
        # 这里量的是"描述文本"的预算，把路径归一成固定长度占位再算。
        normalized = "\n".join(
            " | ".join(field if index != 3 else "<path>"
                       for index, field in enumerate(line.split(" | ")))
            for line in rendered["text"].splitlines())
        # 盯**每条**的成本，而不是绝对总量：目录增长是正常的，描述膨胀不是。
        # 2026-08-31 实测：14 条时归一化 415 tokens（约 30/条）；ES1-lite 加到
        # 17 条后 630（约 37/条）—— 新描述略长于既有平均值 14 词，但仍同一量级。
        entries = len(rendered["text"].splitlines())
        per_entry = context.estimate_tokens(normalized) / entries
        self.assertLessEqual(per_entry, 45,
                             f"每条 description 的归一化成本涨到 {per_entry:.0f}"
                             " tokens —— 描述在膨胀，不是目录在增长")
        # 同时保留一个绝对上限，接住无节制的目录增长。
        self.assertLessEqual(context.estimate_tokens(normalized), 1000)
        # 路径本身的开销单独盯住：每条一个路径，别悄悄多带字段。
        self.assertEqual(
            [len(line.split(" | ")) for line in rendered["text"].splitlines()],
            [4] * len(expected))

    def test_context_reports_measured_skill_index_budget(self):
        text = "a | builtin | Use when a, b, or c | /x/SKILL.md"
        session = SimpleNamespace(
            ag=SimpleNamespace(load_skills=True),
            memory_index={},
            memory_use=False,
            memory_generate=False,
            skills_index={
                "text": text, "entries": [object()],
                "available": 1, "warnings": [],
            },
            repo_map_index="",
            architecture_index="",
            build_context_capsule=lambda: {
                "chars": 0, "sha256": "0" * 64,
                "source_session": "test",
            },
        )
        with (
                mock.patch.object(CLI, "show_context"),
                contextlib.redirect_stdout(io.StringIO()) as output):
            CLI.cmd_context(session, "")
        rendered = output.getvalue()
        self.assertIn("skills index", rendered)
        self.assertIn("1/1 entries", rendered)
        self.assertIn(f"{len(text)} chars", rendered)
        self.assertIn(
            f"~{context.estimate_tokens(text)} tokens", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SkillInvocationIsVisible(unittest.TestCase):
    """skill 走「索引 + read_file」，默认对用户不可见。

    AGENTS.md §2：不可发现的功能等价于不存在的功能。两道保险 ——
    终端标记（不依赖模型配合）+ prompt 要求模型说明（跨 /resume 留在正文里）。
    """

    def test_call_line_marks_skill_reads(self):
        import zylab
        line = zylab.preview("read_file", {"path": "skills/test-ratchet/SKILL.md"})
        self.assertIn("⚑ skill: test-ratchet", line)
        self.assertIn("skills/test-ratchet/SKILL.md", line, "路径仍要可见")

    def test_ordinary_reads_unmarked(self):
        import zylab
        self.assertEqual(zylab.preview("read_file", {"path": "core/agent.py"}),
                         "core/agent.py")

    def test_bare_skill_md_not_marked(self):
        """没有父目录就说不出 skill 名，不要瞎标。"""
        import zylab
        self.assertEqual(zylab.preview("read_file", {"path": "SKILL.md"}),
                         "SKILL.md")

    def test_prompt_asks_model_to_say_which_skill(self):
        prompt = agent.system_prompt(skills_context="demo | builtin | d | p",
                                     load_skills=True)
        self.assertIn("用了哪条", prompt)
        self.assertIn("没读就不要提", prompt, "不能诱导模型宣称没发生的读取")


class SkillTriggerStrength(unittest.TestCase):
    """触发指令的语气必须是"要求"，不是"劝阻"。

    2026-08-31 实测：旧文案写的是「仅在明确相关时才读」，读起来像在劝阻。
    三次试验里，任务同时命中 safe-refactor 与 test-ratchet 两条 description，
    模型一条 SKILL.md 都没读，直接去读源码了。对照 codex 的同位置文案是
    「you must use that skill for that turn」。
    """

    def _prompt(self):
        return agent.system_prompt(
            skills_context="demo | builtin | Use when demoing | /p/SKILL.md",
            load_skills=True)

    def test_named_skill_is_mandatory(self):
        prompt = self._prompt()
        self.assertIn("必须使用它", prompt, "点名必须是强制的")
        self.assertIn("$skill-name", prompt, "要告诉模型点名语法长什么样")

    def test_description_match_requires_reading_before_acting(self):
        prompt = self._prompt()
        self.assertIn("动手之前先用 read_file", prompt,
                      "命中就得先读，不能边做边补")

    def test_no_carry_across_turns(self):
        """上一轮用过不等于这一轮还要用，反之亦然。"""
        self.assertIn("不跨轮沿用", self._prompt())

    def test_security_boundary_outranks_the_trigger(self):
        """强化触发不得松开安全边界 —— 这两件事在同一段文字里有张力。"""
        prompt = self._prompt()
        self.assertIn("不是用户指令", prompt)
        self.assertIn("不是用户授权", prompt)
        self.assertIn("project scope", prompt)
        self.assertIn("不能扩大你的权限", prompt)
        # 顺序也要对：安全边界必须写在触发规则之后，并自称优先。
        self.assertLess(prompt.index("触发规则"), prompt.index("安全边界"))
        self.assertIn("优先于以上全部", prompt)

    def test_index_still_excludes_bodies(self):
        self.assertNotIn("REFERENCE BODY", self._prompt())


class DollarSkillCompletion(unittest.TestCase):
    """`$name` 点名补全 —— 打错名字等于没点名。"""

    def _editor(self):
        from core import tui
        editor = tui.LineEditor()
        editor.set_skills({"test-ratchet": "Use when fixing a bug",
                           "safe-refactor": "Use when refactoring",
                           "code-review": "Use when reviewing a diff"})
        return editor

    def _matches(self, typed):
        editor = self._editor()
        editor.text = typed
        editor.cursor = len(typed)
        return [row[0] for row in editor._dollar_matches()]

    def test_bare_dollar_lists_all(self):
        self.assertEqual(len(self._matches("$")), 3)

    def test_prefix_narrows(self):
        self.assertEqual(self._matches("$te"), ["$test-ratchet"])

    def test_mid_sentence_completion_keeps_the_prefix(self):
        self.assertEqual(self._matches("帮我看看 $co"), ["帮我看看 $code-review"])

    def test_unknown_prefix_matches_nothing(self):
        self.assertEqual(self._matches("$zzz"), [])

    def test_plain_text_is_untouched(self):
        self.assertEqual(self._matches("这个价格是 5 dollars"), [])

    def test_dollar_inside_a_word_is_not_a_mention(self):
        """`PATH=$HOME/x` 之类不该弹 skill 菜单。"""
        self.assertEqual(self._matches("PATH=$HO"), [])

    def test_empty_catalog_is_safe(self):
        from core import tui
        editor = tui.LineEditor()
        editor.text = "$"
        editor.cursor = 1
        self.assertEqual(editor._dollar_matches(), [])


class EngineeringSkillsBatchOne(unittest.TestCase):
    """ES1-lite：三条不与现有条目竞争的新 skill。

    PLAN 的 ES1 原本要加五条，其中 `tdd` 与 `test-ratchet`、`codebase-design`
    与 `safe-refactor` 描述高度重叠。zylab 是全量注入、没有检索层，描述重叠
    直接把路由判断搞糊 —— 2026-08-31 实测过这个形态：任务同时跨 safe-refactor
    与 test-ratchet 两条描述时，模型**两条都没读**。那两条等 ES0a 的负向触发
    评测能证明它们没搞坏既有路由之后再加。
    """

    NEW = ("diagnosing-bugs", "trace-change-impact", "domain-modeling")

    @classmethod
    def setUpClass(cls):
        cls.catalog = skills.load(cwd=str(Path(CLI.__file__).parent))

    def test_all_three_load_cleanly(self):
        for name in self.NEW:
            self.assertIn(name, self.catalog.skills)
        self.assertEqual(self.catalog.errors, [])
        self.assertEqual(self.catalog.warnings, [])

    def test_batch_one_skills_are_all_present(self):
        """不钉死总数 —— 目录还会继续长；钉的是这一批确实都在。

        （总数由 test_builtin_descriptions_route_and_index_has_stable_snapshot
        的快照负责，那里改动会强制更新期望，正是我们要的 ratchet。）
        """
        for name in self.NEW:
            self.assertIn(name, self.catalog.skills)
        self.assertGreaterEqual(len(self.catalog.skills), 17)

    def test_descriptions_state_when_not_what(self):
        """删掉 name 之后仍要能判断何时触发。"""
        for name in self.NEW:
            description = self.catalog.skills[name].description
            self.assertTrue(description.startswith("Use when"),
                            f"{name} 的 description 不是触发条件式")
            self.assertGreaterEqual(
                description.count(","), 2,
                f"{name} 至少要给三种触发语境，实际 {description!r}")

    def test_no_hardcoded_install_path(self):
        """交叉引用按名字，不按绝对路径。

        既有 5 条 skill 写死了 /root/data/zylab/skills/... —— 研究者装在
        别的目录时那些引用全是死链（2026-08-31 发现，归 ES2 修）。新增的不许
        再引入。
        """
        for name in self.NEW:
            body = Path(self.catalog.skills[name].path).read_text(
                encoding="utf-8")
            self.assertNotIn("/root/data", body,
                             f"{name} 写死了维护者的安装路径")

    def test_bodies_stay_progressively_loadable(self):
        for name in self.NEW:
            lines = Path(self.catalog.skills[name].path).read_text(
                encoding="utf-8").splitlines()
            self.assertLessEqual(len(lines), 200, f"{name} 正文过长")

    def test_each_declares_stop_conditions(self):
        """PLAN §ES1 退出条件：正文要声明适用范围、步骤、停止条件、证据。

        停止条件是最容易漏、也最有价值的一节 —— 它决定模型什么时候该收手。
        """
        for name in self.NEW:
            body = Path(self.catalog.skills[name].path).read_text(
                encoding="utf-8")
            self.assertIn("Stop condition", body, f"{name} 缺少停止条件")

    def test_bodies_are_not_injected(self):
        index = skills.render_index(self.catalog)
        for name in self.NEW:
            self.assertIn(name, index["text"])
        self.assertNotIn("Enumerate before editing", index["text"],
                         "索引只带 description，不能带正文")

    def test_descriptions_do_not_duplicate_existing_ones(self):
        """新条目不得与既有条目抢同一批触发词。"""
        overlaps = {
            "diagnosing-bugs": ("refactor", "review a diff"),
            "trace-change-impact": ("refactoring legacy", "estimating counts"),
            "domain-modeling": ("regression test", "reviewing a diff"),
        }
        for name, forbidden in overlaps.items():
            description = self.catalog.skills[name].description.lower()
            for phrase in forbidden:
                self.assertNotIn(phrase, description,
                                 f"{name} 与既有 skill 的触发词重叠：{phrase}")


class SkillsArePortableAcrossDeployments(unittest.TestCase):
    """ES2：skill 正文不得钉死维护者的安装路径。

    2026-08-31 发现：5 条 skill 用 `/root/data/zylab/skills/<name>/SKILL.md`
    互相引用，另有 25 处指向 `/root/data/docs/*`、`/root/data/.codex/*` 等
    维护者私有文档。研究者装在别的目录、用自己的 pod 时，这些引用全是死链 ——
    而 `pod-environment` 会把人指向一个不存在的 environment.md，比没有引用更糟。
    """

    @classmethod
    def setUpClass(cls):
        cls.catalog = skills.load(cwd=str(Path(CLI.__file__).parent))
        cls.bodies = {
            name: Path(item.path).read_text(encoding="utf-8")
            for name, item in cls.catalog.skills.items()}

    def test_no_maintainer_absolute_paths(self):
        for name, body in self.bodies.items():
            self.assertNotIn("/root/data", body,
                             f"{name} 钉死了维护者的路径；用 $HOME 或 skill 名")

    def test_cross_references_use_names_not_paths(self):
        """按名字引用可移植；按路径引用换个安装目录就断。"""
        for name, body in self.bodies.items():
            self.assertNotIn("skills/", body.replace("$HOME/.codex/skills/", ""),
                             f"{name} 仍在用路径引用兄弟 skill")

    def test_external_docs_are_marked_conditional(self):
        """引用 $HOME 下的文档时必须说明它可能不存在。

        否则模型会在别人的 pod 上照着读一个不存在的文件，然后报告读失败 ——
        或者更糟，凭记忆编造内容。
        """
        # 第一版断言的是**精确措辞**列表（"when it exists" 之类），结果被
        # "when they exist" 这种单复数差异绊倒 —— 和 AGENTS.md §5 那类
        # 「grep 模式写窄了」是同一个毛病。改成检查语义标记：引用附近必须出现
        # 「可能不在」的意思，措辞自由。
        markers = ("exist", "absent", "missing", "not shipped", "differ")
        for name, body in self.bodies.items():
            paragraphs = body.split("\n\n")
            for index, paragraph in enumerate(paragraphs):
                if "$HOME/docs/" not in paragraph and \
                        "$HOME/.codex/" not in paragraph:
                    continue
                # 限定语常写在清单的标题段里，所以连上一段一起看。
                window = (paragraphs[index - 1] if index else "") + paragraph
                self.assertTrue(
                    any(m in window.lower() for m in markers),
                    f"{name} 有一段引用了 $HOME 下的文档却没说明它可能不存在：\n"
                    f"{paragraph[:160]}")


class SkillLayerBoundaries(unittest.TestCase):
    """ES2 退出条件：同一请求不该无差别加载所有工程 skill。

    机制是让每条 skill 自己说清「我不管什么、谁管」。2026-08-31 实测里
    `submit-a-gpu-job` 一次读了三条（compute-job-submit + quantify-first +
    experiment-log，正文合计约 5k tokens），就是缺这层边界的形态。
    """

    FOUR = ("test-ratchet", "safe-refactor", "code-review", "adversarial-review")

    @classmethod
    def setUpClass(cls):
        catalog = skills.load(cwd=str(Path(CLI.__file__).parent))
        cls.bodies = {
            name: Path(catalog.skills[name].path).read_text(encoding="utf-8")
            for name in cls.FOUR}

    def test_each_declares_its_boundary(self):
        for name in self.FOUR:
            self.assertIn("## Boundaries", self.bodies[name],
                          f"{name} 没声明自己不管什么")

    def test_each_points_elsewhere_for_what_it_excludes(self):
        """只说「我不管」不够，还要说「谁管」，否则模型无处可去。"""
        for name in self.FOUR:
            section = self.bodies[name].split("## Boundaries", 1)[1]
            self.assertIn("skill", section,
                          f"{name} 的边界没有指向兄弟 skill")

    def test_no_reference_cycle_between_reviewers(self):
        """code-review 与 adversarial-review 不能互相把活推给对方。"""
        code = self.bodies["code-review"].split("## Boundaries", 1)[1]
        adversarial = self.bodies["adversarial-review"].split(
            "## Boundaries", 1)[1]
        self.assertIn("adversarial-review", code)
        self.assertIn("code-review", adversarial)
        # 单向的分工语义：各自说明对方管什么，而不是都说"去找对方"
        self.assertIn("line by line", adversarial,
                      "adversarial-review 要说明自己不做逐行 diff")
        self.assertIn("concrete diff", code,
                      "code-review 要说明自己只管具体 diff")
