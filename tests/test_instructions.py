"""指令记忆：层级合并、@import、以及那条把 import 关在工作目录里的闸（P4）。

以前只有「从 cwd 往上找到第一个 CLAUDE.md 就停」。这里钉住换掉它之后的三件事：
认 AGENTS.md、逐层全收、@import 能用——外加一条比功能更要紧的：

**项目级指令文件跟着 clone 来，是不可信数据。** 一份恶意的 CLAUDE.md 只要写上
`@~/.ssh/id_rsa`，密钥就会进 system prompt 发给网关，而界面上什么都看不出来。
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

import tests  # noqa: F401,E402  —— 状态目录隔离

from core import instructions as I  # noqa: E402


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


class Hierarchy(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_both_conventional_names_are_read(self):
        """仓库自己的规矩写在哪个文件名里，不该决定它能不能被读到。"""
        write(self.root / "AGENTS.md", "规矩甲")
        write(self.root / "CLAUDE.md", "规矩乙")
        bundle = I.collect(self.root, root=self.root)
        text = I.render(bundle)
        self.assertIn("规矩甲", text)
        self.assertIn("规矩乙", text)

    def test_every_level_from_root_down_is_collected_nearest_last(self):
        """单体仓库：根上一条、包里一条，以前只有一条能生效。"""
        write(self.root / "CLAUDE.md", "全仓：绝不 git add -A")
        deep = self.root / "packages" / "web"
        write(deep / "AGENTS.md", "本包：用 pnpm")
        bundle = I.collect(deep, root=self.root)
        self.assertEqual(len(bundle.blocks), 2)
        # 最近的排最后 —— 与 system_prompt 里「后者更具体可以覆盖前者」同一套约定
        self.assertTrue(bundle.blocks[0].path.endswith("CLAUDE.md"))
        self.assertTrue(bundle.blocks[-1].path.endswith("web/AGENTS.md"))

    def test_a_symlinked_pair_is_not_read_twice(self):
        """`ln -s AGENTS.md CLAUDE.md` 是常见写法，不该让指令翻倍。"""
        write(self.root / "AGENTS.md", "只此一份" * 20)
        try:
            os.symlink(self.root / "AGENTS.md", self.root / "CLAUDE.md")
        except (OSError, NotImplementedError):
            self.skipTest("这台机器造不出符号链接")
        bundle = I.collect(self.root, root=self.root)
        self.assertEqual(len(bundle.blocks), 1)

    def test_two_copies_with_identical_content_collapse(self):
        """复制（而非软链）出来的两份：realpath 不同，内容一样。"""
        write(self.root / "AGENTS.md", "一模一样的内容" * 10)
        write(self.root / "CLAUDE.md", "一模一样的内容" * 10)
        self.assertEqual(len(I.collect(self.root, root=self.root).blocks), 1)

    def test_an_empty_file_contributes_nothing(self):
        write(self.root / "AGENTS.md", "   \n\n  ")
        self.assertEqual(I.collect(self.root, root=self.root).blocks, [])


class Imports(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def collect(self, cwd=None):
        return I.collect(cwd or self.root, root=self.root)

    def test_a_relative_import_is_expanded_in_place(self):
        write(self.root / "docs" / "style.md", "四空格缩进")
        write(self.root / "AGENTS.md", "先读风格：\n@docs/style.md\n然后动手")
        text = I.render(self.collect())
        self.assertIn("四空格缩进", text)
        self.assertIn("然后动手", text)

    def test_imports_nest_and_stop_at_the_depth_cap(self):
        for n in range(I.MAX_IMPORT_DEPTH + 3):
            write(self.root / f"r{n}.md", f"第{n}层\n@r{n + 1}.md")
        write(self.root / "AGENTS.md", "@r0.md")
        text = I.render(self.collect())
        self.assertIn("第0层", text)
        self.assertIn(f"第{I.MAX_IMPORT_DEPTH - 1}层", text)
        self.assertIn("import 深度超过", text)

    def test_a_cycle_terminates(self):
        write(self.root / "a.md", "甲\n@b.md")
        write(self.root / "b.md", "乙\n@a.md")
        write(self.root / "AGENTS.md", "@a.md")
        text = I.render(self.collect())          # 不挂就是通过
        self.assertIn("甲", text)
        self.assertIn("乙", text)

    def test_a_missing_import_says_so_instead_of_vanishing(self):
        write(self.root / "AGENTS.md", "@nope.md")
        self.assertIn("不存在", I.render(self.collect()))

    def test_only_a_whole_line_counts_as_an_import(self):
        """`联系 @someone`、装饰器、邮箱都不是 import —— 行内 @ 一律不认。"""
        write(self.root / "secret.md", "不该出现")
        write(self.root / "AGENTS.md",
              "有问题找 @secret.md 问\n"
              "@decorator 是 Python 语法\n"
              "a@secret.md 是邮箱\n")
        text = I.render(self.collect())
        self.assertNotIn("不该出现", text)

    def test_a_list_item_import_works(self):
        write(self.root / "x.md", "列表项里的内容")
        write(self.root / "AGENTS.md", "读这些：\n- @x.md\n")
        self.assertIn("列表项里的内容", I.render(self.collect()))


class ImportsAreConfinedForRepoFiles(unittest.TestCase):
    """仓库里的指令文件不能靠 @import 把工作目录外的东西读进 system prompt。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.outside = base / "outside"
        self.work = base / "work"
        write(self.outside / "private.md", "工作目录之外的内容")
        self.work.mkdir(parents=True, exist_ok=True)

    def test_a_repo_file_cannot_import_from_outside_the_workspace(self):
        write(self.work / "AGENTS.md", f"@{self.outside / 'private.md'}")
        bundle = I.collect(self.work, root=self.work)
        text = I.render(bundle)
        self.assertNotIn("工作目录之外的内容", text)
        self.assertIn("已拒绝", text, "拒绝要留痕，不能静默丢掉")
        self.assertTrue(any("拒绝 import" in n for n in bundle.notes))

    def test_a_file_above_the_workspace_may_import_freely(self):
        """`~/CLAUDE.md` 是这台机器的主人写的，不是 clone 带来的。"""
        write(self.work.parent / "CLAUDE.md",
              f"@{self.outside / 'private.md'}")
        text = I.render(I.collect(self.work, root=self.work))
        self.assertIn("工作目录之外的内容", text)

    def test_credential_shaped_paths_are_refused_from_any_scope(self):
        """凭据那道闸与 @file、repo-map 共用 —— 分头各写一套必然漂移。"""
        write(self.outside / ".ssh" / "id_rsa", "-----BEGIN PRIVATE KEY-----")
        for holder, root in ((self.work, self.work),
                             (self.work.parent, self.work)):
            with self.subTest(holder=str(holder)):
                write(holder / "AGENTS.md",
                      f"@{self.outside / '.ssh' / 'id_rsa'}")
                text = I.render(I.collect(self.work, root=root))
                self.assertNotIn("BEGIN PRIVATE KEY", text)
                self.assertIn("已拒绝", text)
                os.unlink(holder / "AGENTS.md")

    def test_scope_is_decided_by_the_workspace_boundary(self):
        self.assertEqual(
            I.scope_of(self.work / "AGENTS.md", str(self.work)), "project")
        self.assertEqual(
            I.scope_of(self.work.parent / "CLAUDE.md", str(self.work)),
            "ancestor")


class Budget(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_a_large_but_sane_total_only_warns(self):
        write(self.root / "AGENTS.md", "规" * (I.WARN_TOTAL_CHARS + 10))
        bundle = I.collect(self.root, root=self.root)
        self.assertEqual(len(bundle.blocks), 1)
        self.assertTrue(any("每一次请求都要带上" in n for n in bundle.notes))

    def test_over_budget_drops_the_least_specific_first(self):
        """离 cwd 最近的规则最不能丢 —— 要丢就从最外层丢。"""
        write(self.root / "CLAUDE.md", "外" * I.MAX_TOTAL_CHARS)
        deep = self.root / "pkg"
        write(deep / "AGENTS.md", "内层的规矩")
        bundle = I.collect(deep, root=self.root)
        text = I.render(bundle)
        self.assertIn("内层的规矩", text)
        self.assertNotIn("外外外", text)
        self.assertTrue(any("已丢掉最外层" in n for n in bundle.notes))

    def test_a_single_oversized_file_is_truncated_not_dropped(self):
        """只有一份时丢掉等于什么规矩都没有；截断至少留住开头。"""
        write(self.root / "AGENTS.md",
              "开头就写最要紧的\n" + "填" * (I.MAX_TOTAL_CHARS * 2))
        bundle = I.collect(self.root, root=self.root)
        self.assertEqual(len(bundle.blocks), 1)
        self.assertLessEqual(bundle.total_chars, I.MAX_TOTAL_CHARS)
        self.assertIn("开头就写最要紧的", I.render(bundle))
        self.assertTrue(any("已截断" in n for n in bundle.notes))


class AgentWiring(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.here = os.getcwd()
        self.addCleanup(os.chdir, self.here)

    def test_project_instructions_renders_every_level_and_keeps_notes(self):
        from core import agent as A
        write(self.root / "CLAUDE.md", "外层规矩")
        deep = self.root / "pkg"
        write(deep / "AGENTS.md", "内层规矩")
        os.chdir(deep)
        text = A.project_instructions()
        self.assertIn("外层规矩", text)
        self.assertIn("内层规矩", text)
        self.assertLess(text.index("外层规矩"), text.index("内层规矩"))
        self.assertIsInstance(A.INSTRUCTION_NOTES, list)

    def test_no_instruction_file_is_not_an_error(self):
        from core import agent as A
        os.chdir(self.root)
        self.assertEqual(A.project_instructions(cwd=str(self.root)), "")


if __name__ == "__main__":
    unittest.main()
